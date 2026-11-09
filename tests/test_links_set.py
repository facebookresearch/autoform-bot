"""`links set`: an article's facts recorded where the project keeps them, read back the same, or nothing written."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shlex
from pathlib import Path

import pytest

from autoform_cli import annotation_writer as writer
from autoform_cli import links_set
from autoform_cli.__main__ import main
from autoform_cli.annotations import read_links
from autoform_cli.article_identity import _proposed_id
from autoform_cli.graph import load_graph
from autoform_cli.links_migration import plan_links, write_links
from autoform_cli.links_set import KEYS, Edits, LinksSetError, LinksSetResult, parse_arguments, set_facts

ARTICLE_ID = "af_f8d7b54996f51594f707d44f"
RENAME = "rename it (in Git: git mv through a temporary name)"


def _blueprint(tmp_path: Path, articles: dict[str, str | None]) -> Path:
    """A blueprint with an article below roadmap/ per frontmatter given; None writes none."""

    blueprint = tmp_path / "blueprint"
    for stem, frontmatter in articles.items():
        page = blueprint / "roadmap" / f"{stem}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        head = "" if frontmatter is None else f"---\n{frontmatter}\n---\n\n"
        page.write_text(f"{head}# {stem}\n\nA statement.\n", encoding="utf-8")
    return blueprint


def _set(blueprint: Path, *items: str, unset: tuple[str, ...] = ()) -> LinksSetResult:
    articles, edits = parse_arguments(items, unset)
    return set_facts(blueprint, articles, edits)


def _refused(blueprint: Path, *items: str, unset: tuple[str, ...] = ()) -> list[str]:
    with pytest.raises(LinksSetError) as refused:
        _set(blueprint, *items, unset=unset)
    return list(refused.value.issues)


def _bytes(blueprint: Path) -> dict[str, bytes]:
    files = (path for path in blueprint.rglob("*") if path.is_file())
    return {path.relative_to(blueprint).as_posix(): path.read_bytes() for path in files}


def _mark(blueprint: Path) -> None:
    """Give *blueprint* the template the writer leaves in a project whose facts moved into links/."""
    (blueprint / "links/roadmap").mkdir(parents=True, exist_ok=True)
    (blueprint / "links/roadmap/_defaults.json").write_bytes(writer.dumps(writer.PROJECT_TEMPLATE))


def _about(page: str, **fields: object) -> bytes:
    """A row about *page*, written from ``links/roadmap/``."""
    return writer.dumps({"subject": [{"type": "generic", "uri": f"../../roadmap/{page}"}], **fields})


def test_an_argument_holding_an_equals_sign_is_an_edit() -> None:
    items = ["a.md", "origin=Cited", "lean=P.a, P.b", "area='Group theory'", "discussion=https://x.invalid/?a=b"]

    articles, edits = parse_arguments([*items, "b.md"], ["not_ready"])

    assert articles == ("a.md", "b.md")
    # Values read as frontmatter reads them: normalized, unquoted, and split at the first '='.
    values = {"origin": "cited", "lean": "P.a, P.b", "area": "Group theory", "discussion": "https://x.invalid/?a=b"}
    assert edits == Edits(values, frozenset({"not_ready"}))


def test_every_edit_that_cannot_be_made_is_named_and_none_is_made(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "area=x.md").write_text("# An article named with '='\n", encoding="utf-8")
    items = [
        "odd=name.md",
        "area=x.md",
        "discussion=https://example.invalid/thread.md",
        "open_statements=allowed",
        "article_id=af_0",
        "bogus=1",
        "origin=cited",
        "origin=bridged",
        "area=",
        "summary=''",
        "statement=bogus",
    ]

    with pytest.raises(LinksSetError) as refused:
        parse_arguments(items, ["proof", "proof", "origin", "tracks_lean"])

    assert list(refused.value.issues) == [
        f"odd=name.md: an argument holding '=' is an edit, so no article's path may hold one; {RENAME}",
        f"area=x.md: an argument holding '=' is an edit, so no article's path may hold one; {RENAME}",
        "open_statements: a setting, which stays in README frontmatter",
        "article_id: an article's ID is never set or removed; naming the article with no edit gives it one",
        f"bogus: not a fact; the facts are {', '.join(sorted(KEYS))}",
        "origin: given twice",
        "area: an empty value; --unset area removes it",
        f"summary: not a fact; the facts are {', '.join(sorted(KEYS))}",
        "statement=bogus: 'statement' accepts only 'formalized' or 'retracted'; omit the key otherwise",
        "--unset proof: given twice",
        "origin: both set and unset",
        "tracks_lean: a setting, which stays in README frontmatter",
        "no article named; name each by its path",
    ]


def test_a_value_frontmatter_could_not_hold_on_one_line_is_refused() -> None:
    with pytest.raises(LinksSetError) as refused:
        parse_arguments(["a.md", "discussion=one\u2028two", "area=''"], [])

    assert list(refused.value.issues) == [
        "discussion: a value with a line break or another unprintable character",
        "area: an empty value; --unset area removes it",
    ]


def test_without_the_writer_template_frontmatter_is_edited_in_place(tmp_path: Path) -> None:
    """A key's line is replaced where it stands, a new key goes last, and comments and line endings stay."""

    frontmatter = f'article_id: {ARTICLE_ID}\n# keep this comment\ndeclaration: theorem\nlean: "P.a"\norigin: Cited'
    blueprint = _blueprint(tmp_path, {"result": frontmatter, "other": "declaration: lemma"})
    page = blueprint / "roadmap/result.md"
    page.write_bytes(page.read_bytes().replace(b"\n", b"\r\n"))
    other = (blueprint / "roadmap/other.md").read_bytes()

    edits = ("lean=P.b", "statement=formalized", "area=Algebra")
    result = _set(blueprint, str(page), *edits, unset=("declaration",))

    assert result == LinksSetResult(("roadmap/result.md",), ())
    assert page.read_bytes() == (
        f"---\r\narticle_id: {ARTICLE_ID}\r\n# keep this comment\r\nlean: P.b\r\norigin: Cited\r\n"
        "statement: formalized\r\narea: Algebra\r\n---\r\n\r\n# result\r\n\r\nA statement.\r\n"
    ).encode()
    assert (blueprint / "roadmap/other.md").read_bytes() == other
    node = load_graph(blueprint).nodes["result"]
    assert (node.declaration, node.lean, node.statement_formalized, node.area, node.origin) == (
        None,
        "P.b",
        True,
        "Algebra",
        "cited",
    )


def test_a_key_status_reads_the_same_without_is_still_removed_from_frontmatter(tmp_path: Path) -> None:
    frontmatter = f"article_id: {ARTICLE_ID}\ndeclaration: def\nlean: P.d\nstatement: formalized\nproof: formalized"
    blueprint = _blueprint(tmp_path, {"result": frontmatter})
    page = blueprint / "roadmap/result.md"

    assert _set(blueprint, str(page), unset=("proof",)) == LinksSetResult(("roadmap/result.md",), ())
    assert "proof:" not in page.read_text(encoding="utf-8")


def test_an_article_named_alone_gets_the_id_migrate_would_give_it(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"plain": None})
    page = blueprint / "roadmap/plain.md"
    article_id = _proposed_id("plain", hashlib.sha256(page.read_bytes()).hexdigest())

    assert _set(blueprint, str(page)) == LinksSetResult(("roadmap/plain.md",), ())

    assert page.read_text(encoding="utf-8") == f"---\narticle_id: {article_id}\n---\n\n# plain\n\nA statement.\n"
    assert load_graph(blueprint).nodes["plain"].article_id == article_id
    assert _set(blueprint, str(page)) == LinksSetResult((), ())


@pytest.mark.parametrize(
    ("frontmatter", "edit", "rule"),
    [
        (
            "declaration: theorem",
            "statement=retracted",
            "statement: retracted needs the lean: declaration it retracts; without lean:, omit statement",
        ),
        (
            "declaration: def",
            "statement=formalized",
            "statement: formalized needs the lean: declaration that formalizes it",
        ),
        (
            "declaration: theorem",
            "catalog=module",
            "'catalog' cannot be combined with declaration-specific frontmatter key 'declaration'",
        ),
        ("lean: P.a", "proof=formalized", "proof: formalized needs statement: formalized"),
        (
            "lean: P.a\nstatement: formalized\nproof: formalized",
            "statement=retracted",
            "proof: formalized needs statement: formalized, not retracted",
        ),
        (
            "lean: P.a\nmathlib: true",
            "statement=retracted",
            "a mathlib: true article cannot record statement: retracted",
        ),
    ],
)
def test_facts_the_loader_refuses_together_write_nothing(tmp_path: Path, frontmatter: str, edit: str, rule: str):
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\n{frontmatter}"})
    before = _bytes(blueprint)

    issues = _refused(blueprint, str(blueprint / "roadmap/result.md"), edit)

    assert issues == [f"roadmap/result.md: with {edit}, {rule}"]
    assert _bytes(blueprint) == before


def test_one_refused_article_leaves_every_other_unwritten(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"good": f"article_id: {ARTICLE_ID}\nlean: P.g", "bad": "declaration: def"})
    before = _bytes(blueprint)

    pages = (str(blueprint / "roadmap/good.md"), str(blueprint / "roadmap/bad.md"))
    issues = _refused(blueprint, *pages, "statement=formalized")

    assert issues == [
        "roadmap/bad.md: with statement=formalized, statement: formalized needs the lean: declaration that "
        "formalizes it"
    ]
    assert _bytes(blueprint) == before


def test_what_reads_back_otherwise_is_undone(tmp_path: Path) -> None:
    """A declaration's article drops navigation when loaded, which only the graph loaded again shows."""

    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    before = _bytes(blueprint)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "navigation=collection") == [
        "roadmap/result.md: navigation: collection reads back as nothing",
        "nothing recorded",
    ]
    assert _bytes(blueprint) == before


def test_an_interrupted_read_back_is_undone_and_names_what_it_could_not_restore(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    page = blueprint / "roadmap/result.md"
    before = page.read_bytes()
    real_load = links_set.load_graph
    loads: list[object] = []

    def interrupt_the_reload(blueprint_dir):
        loads.append(blueprint_dir)
        if len(loads) == 2:
            raise KeyboardInterrupt
        return real_load(blueprint_dir)

    monkeypatch.setattr(links_set, "load_graph", interrupt_the_reload)
    with pytest.raises(KeyboardInterrupt):
        _set(blueprint, str(page), "origin=cited")
    assert page.read_bytes() == before

    def stuck(steps):
        raise writer.AnnotationWriteError([f"{page}: not undone: it changed"], steps)

    loads.clear()
    monkeypatch.setattr(writer, "undo", stuck)
    with pytest.raises(LinksSetError) as refused:
        _set(blueprint, str(page), "origin=cited")
    assert refused.value.issues == (
        "interrupted before what was written was checked",
        f"{page}: not undone: it changed",
    )
    assert refused.value.left == ("roadmap/result.md",)

    loads.clear()
    assert main(["links", "set", str(blueprint), str(page), "origin=bridged"]) == 2
    assert capsys.readouterr().err.splitlines()[-1] == "error: could not restore roadmap/result.md"


def test_an_interrupted_undo_of_a_failed_check_is_finished(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    before = _bytes(blueprint)
    real_undo = writer.undo
    calls: list[object] = []

    def interrupt_the_first_undo(steps):
        calls.append(steps)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real_undo(steps)

    monkeypatch.setattr(writer, "undo", interrupt_the_first_undo)

    with pytest.raises(KeyboardInterrupt):
        _set(blueprint, str(blueprint / "roadmap/result.md"), "navigation=collection")
    assert len(calls) == 2
    assert _bytes(blueprint) == before


@pytest.mark.parametrize("stops", [(1,), (1, 2)], ids=["once", "again-in-the-second-pass"])
def test_a_stop_in_the_undo_of_a_failed_check_exits_130_with_the_page_restored(
    tmp_path: Path, monkeypatch, capsys, stops: tuple[int, ...]
) -> None:
    """Ctrl-C at the undo's first removal, and again in the writer's second pass, ends with the article's bytes back,
    exit 130 and nothing named: the command's own undo finishes what the writer's left, so the note the second stop
    put on the interrupt no longer holds."""

    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    _mark(blueprint)
    before = _bytes(blueprint)
    real = writer._remove_if
    calls: list[Path] = []

    def stop(path: Path, want: str | None) -> None:
        calls.append(path)
        if len(calls) in stops:
            raise KeyboardInterrupt
        real(path, want)

    monkeypatch.setattr(writer, "_remove_if", stop)
    monkeypatch.setattr(links_set, "_read_back", lambda graph, targets: ["roadmap/result.md: reads back otherwise"])

    try:
        status = main(["links", "set", str(blueprint), str(blueprint / "roadmap/result.md"), "origin=cited"])
    except KeyboardInterrupt:  # past main, it would stop the test run
        pytest.fail("the interrupt went past main")
    assert status == 130
    assert [path.name for path in calls] == ["result.jsonl"] * (len(stops) + 1)
    assert capsys.readouterr().err == ""
    assert _bytes(blueprint) == before


def test_a_second_stop_in_the_undo_of_a_refused_write_names_the_annotation_it_left(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Ctrl-C twice in the undo of a write the page refused, where no cleanup follows, exits 130 naming the annotation
    the writer's second pass did not reach, which stays."""

    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    _mark(blueprint)
    page = blueprint / "roadmap/result.md"
    real_strip, real_remove = writer.strip_facts, writer._remove_if
    calls: list[Path] = []

    def strip_then_edit(content: bytes) -> bytes:
        page.write_bytes(content + b"Edited meanwhile.\n")
        return real_strip(content)

    def stop_twice(path: Path, want: str | None) -> None:
        calls.append(path)
        if len(calls) <= 2:
            raise KeyboardInterrupt
        real_remove(path, want)

    monkeypatch.setattr(writer, "strip_facts", strip_then_edit)
    monkeypatch.setattr(writer, "_remove_if", stop_twice)

    try:
        status = main(["links", "set", str(blueprint), str(page), "origin=cited"])
    except KeyboardInterrupt:  # past main, it would stop the test run
        pytest.fail("the interrupt went past main")
    assert status == 130
    assert capsys.readouterr().err.endswith("links/roadmap/result.jsonl: not undone: interrupted\n")
    assert (blueprint / "links/roadmap/result.jsonl").exists()


def test_in_a_moved_project_an_unannotated_article_moves_into_its_annotation(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem\nlean: P.a"})
    _mark(blueprint)
    page = blueprint / "roadmap/result.md"

    result = _set(blueprint, str(page), "statement=formalized")

    assert result == LinksSetResult(("links/roadmap/result.jsonl", "roadmap/result.md"), ())
    record = {
        "subject": [{"type": "generic", "uri": "../../roadmap/result.md"}],
        "object": [{"location": {"declaration": "P.a"}, "role": "states"}],
        "status": "stated",
        "kind": "theorem",
        "id": ARTICLE_ID,
    }
    assert (blueprint / "links/roadmap/result.jsonl").read_bytes() == writer.dumps(record)
    assert page.read_text(encoding="utf-8") == "# result\n\nA statement.\n"
    graph = load_graph(blueprint)
    node = graph.nodes["result"]
    assert (node.lean, node.statement_formalized, node.article_id) == ("P.a", True, ARTICLE_ID)
    assert graph.warnings == ()


def test_a_page_with_no_facts_to_strip_keeps_its_bytes(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"plain": None})
    _mark(blueprint)
    page = blueprint / "roadmap/plain.md"
    article_id = _proposed_id("plain", hashlib.sha256(page.read_bytes()).hexdigest())

    assert _set(blueprint, str(page)) == LinksSetResult(("links/roadmap/plain.jsonl",), ())
    assert json.loads((blueprint / "links/roadmap/plain.jsonl").read_text(encoding="utf-8"))["id"] == article_id


def test_a_write_that_stops_partway_is_undone(tmp_path: Path, monkeypatch) -> None:
    """The annotation is written first; the page, edited meanwhile, refuses the strip, so the annotation goes."""

    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    _mark(blueprint)
    page = blueprint / "roadmap/result.md"
    real_strip = writer.strip_facts

    def strip_then_edit(content: bytes) -> bytes:
        page.write_bytes(content + b"Edited meanwhile.\n")
        return real_strip(content)

    monkeypatch.setattr(writer, "strip_facts", strip_then_edit)

    assert _refused(blueprint, str(page), "origin=cited") == [
        f"{page}: not written: it changed after it was read; run the command again",
        "nothing recorded",
    ]
    assert not (blueprint / "links/roadmap/result.jsonl").exists()
    assert page.read_bytes().endswith(b"Edited meanwhile.\n")


def test_an_annotation_the_reload_does_not_read_as_the_articles_is_undone(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    _mark(blueprint)
    before = _bytes(blueprint)
    real_read = links_set.read_links
    reads: list[object] = []

    def unread_on_reload(blueprint_dir, articles):
        reads.append(blueprint_dir)
        links = real_read(blueprint_dir, articles)
        return dataclasses.replace(links, files={}) if len(reads) == 2 else links

    monkeypatch.setattr(links_set, "read_links", unread_on_reload)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "origin=cited") == [
        "roadmap/result.md: links/roadmap/result.jsonl is not read as its annotation; the warnings about "
        "blueprint/links/ say why",
        "nothing recorded",
    ]
    assert _bytes(blueprint) == before


def test_an_unread_mathlib_annotation_is_undone(tmp_path: Path, monkeypatch) -> None:
    """Every annotation file written must be read as the article's, the Mathlib one too, not only the facts."""

    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem\nlean: P.r"})
    write_links(blueprint)
    before = _bytes(blueprint)
    real_read = links_set.read_links
    reads: list[object] = []

    def drop_mathlib_on_reload(blueprint_dir, articles):
        reads.append(blueprint_dir)
        links = real_read(blueprint_dir, articles)
        if len(reads) < 2:
            return links
        kept = {path: tuple(name for name in names if "mathlib" not in name) for path, names in links.files.items()}
        return dataclasses.replace(links, files=kept)

    monkeypatch.setattr(links_set, "read_links", drop_mathlib_on_reload)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "mathlib_declaration=Foo.bar") == [
        "roadmap/result.md: links/mathlib/roadmap/result.jsonl is not read as its annotation; the warnings about "
        "blueprint/links/ say why",
        "nothing recorded",
    ]
    assert _bytes(blueprint) == before


def test_a_page_the_graph_does_not_load_or_that_changed_meanwhile_is_refused(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path, {"result": "declaration: lemma"})
    page = blueprint / "roadmap/result.md"

    assert _refused(blueprint, str(blueprint / "roadmap/missing.md"), "origin=cited") == [
        "roadmap/missing.md: not an article the graph loads"
    ]

    real_read = links_set.read_links

    def edit_then_read(blueprint_dir, articles):
        page.write_bytes(page.read_bytes() + b"Edited meanwhile.\n")
        return real_read(blueprint_dir, articles)

    monkeypatch.setattr(links_set, "read_links", edit_then_read)

    assert _refused(blueprint, str(page), "origin=cited") == [
        "roadmap/result.md: changed while the graph was loaded; run the command again"
    ]
    assert b"origin" not in page.read_bytes()


def test_another_article_reading_otherwise_after_the_write_undoes_it(tmp_path: Path, monkeypatch) -> None:
    """Every article is compared, not only those edited; what another writer did to one is not this call's to undo."""

    blueprint = _blueprint(tmp_path, {"result": "declaration: lemma", "other": "declaration: lemma"})
    page, other = blueprint / "roadmap/result.md", blueprint / "roadmap/other.md"
    before = page.read_bytes()
    real_write = writer.write

    def write_and_edit_another(changes, expected, done=None):
        other.write_text("---\ndeclaration: lemma\norigin: cited\n---\n\n# other\n", encoding="utf-8")
        return real_write(changes, expected, done)

    monkeypatch.setattr(writer, "write", write_and_edit_another)

    assert _refused(blueprint, str(page), "origin=bridged") == [
        "roadmap/other.md: origin: nothing reads back as cited",
        "nothing recorded",
    ]
    assert page.read_bytes() == before
    assert "origin: cited" in other.read_text(encoding="utf-8")


def test_a_blueprint_that_does_not_load_is_refused(tmp_path: Path, capsys) -> None:
    articles = {"result": "declaration: lemma", "broken": "declaration: lemma\ndeclaration: lemma"}
    blueprint = _blueprint(tmp_path, articles)
    before = _bytes(blueprint)

    assert main(["links", "set", str(blueprint), str(blueprint / "roadmap/result.md"), "origin=cited"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "duplicate frontmatter key 'declaration'" in err
    assert _bytes(blueprint) == before


def test_an_id_another_article_holds_is_refused(tmp_path: Path) -> None:
    """The loader leaves out the copy, so keeping it would leave the article without an ID, with an edit or none."""

    blueprint = _blueprint(tmp_path, {"a": f"article_id: {ARTICLE_ID}\ndeclaration: lemma"})
    write_links(blueprint)
    (blueprint / "roadmap/b.md").write_text(f"---\narticle_id: {ARTICLE_ID}\n---\n\n# b\n", encoding="utf-8")
    before = _bytes(blueprint)

    for edits in ((), ("origin=cited",)):
        assert _refused(blueprint, str(blueprint / "roadmap/b.md"), *edits) == [
            f"roadmap/b.md: its article_id {ARTICLE_ID} is a's too; remove one copy"
        ]
    assert _bytes(blueprint) == before


def test_several_articles_are_written_in_one_call(tmp_path: Path) -> None:
    """One moves out of its frontmatter and one is rewritten in its annotation; both need the new Mathlib template."""

    blueprint = _blueprint(tmp_path, {"annotated": "declaration: theorem\nlean: P.a"})
    write_links(blueprint)
    page = blueprint / "roadmap/moved.md"
    page.write_text(f"---\narticle_id: {ARTICLE_ID}\ndeclaration: lemma\nlean: P.m\n---\n\n# moved\n", encoding="utf-8")

    result = _set(blueprint, str(page), str(blueprint / "roadmap/annotated.md"), "mathlib_declaration=Foo.bar")

    assert sorted(result.written) == [
        "links/mathlib/_defaults.json",
        "links/mathlib/roadmap/annotated.jsonl",
        "links/mathlib/roadmap/moved.jsonl",
        "links/roadmap/moved.jsonl",
        "roadmap/moved.md",
    ]
    graph = load_graph(blueprint)
    assert [graph.nodes[name].mathlib_declaration for name in ("annotated", "moved")] == ["Foo.bar", "Foo.bar"]
    assert graph.nodes["moved"].article_id == ARTICLE_ID


def test_one_article_reading_back_otherwise_undoes_every_article(tmp_path: Path) -> None:
    """A declaration's article drops navigation when loaded; the topic beside it, written in the same call, goes too."""

    blueprint = _blueprint(tmp_path, {"lemma": "declaration: lemma", "topic": "origin: cited"})
    before = _bytes(blueprint)
    pages = (str(blueprint / "roadmap/lemma.md"), str(blueprint / "roadmap/topic.md"))

    assert _refused(blueprint, *pages, "navigation=collection") == [
        "roadmap/lemma.md: navigation: collection reads back as nothing",
        "nothing recorded",
    ]
    assert _bytes(blueprint) == before


def test_a_value_the_loader_leaves_out_is_set_or_unset_first(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    _mark(blueprint)
    page = blueprint / "roadmap/result.md"
    own = blueprint / "links/roadmap/result.jsonl"
    facts = {"declaration": "lemma", "origin": "foo", "article_id": ARTICLE_ID}
    own.write_bytes(writer.encode(blueprint, page, facts)[own])

    assert _refused(blueprint, str(page), "not_ready=true") == [
        "roadmap/result.md: its origin 'foo' is left out when read ('origin' accepts cited, bridged, or background); "
        "set or unset origin"
    ]
    assert _set(blueprint, str(page), "origin=cited").written == ("links/roadmap/result.jsonl",)
    assert load_graph(blueprint).nodes["result"].origin == "cited"


def test_a_fact_no_annotation_holds_is_blamed_on_the_frontmatter_not_the_edits(tmp_path: Path) -> None:
    frontmatter = f"article_id: {ARTICLE_ID}\ndeclaration: theorem\nmathlib_file: Mathlib/Foo.lean"
    blueprint = _blueprint(tmp_path, {"result": frontmatter})
    _mark(blueprint)
    before = _bytes(blueprint)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "review_approved=abc") == [
        "roadmap/result.md: as its frontmatter records it, mathlib_file without mathlib_declaration, whose first "
        "Mathlib end would carry the module"
    ]
    assert _bytes(blueprint) == before


def test_an_annotated_article_is_rewritten_in_the_writers_files(tmp_path: Path, capsys) -> None:
    frontmatter = (
        f"article_id: {ARTICLE_ID}\ndeclaration: theorem\nlean: P.a\nstatement: formalized\n"
        "mathlib_declaration: Foo.bar\nmathlib_file: Mathlib/Foo.lean"
    )
    blueprint = _blueprint(tmp_path, {"result": frontmatter})
    write_links(blueprint)
    page = blueprint / "roadmap/result.md"
    mathlib = blueprint / "links/mathlib/roadmap/result.jsonl"
    assert mathlib.exists()

    assert _refused(blueprint, str(page), unset=("mathlib_declaration",)) == [
        "roadmap/result.md: with --unset mathlib_declaration, mathlib_file without mathlib_declaration, whose first "
        "Mathlib end would carry the module"
    ]

    edits = ["proof=formalized", "origin=cited", "--unset", "mathlib_declaration", "--unset", "mathlib_file"]
    assert main(["links", "set", str(blueprint), str(page), *edits]) == 0

    written = "links/roadmap/result.jsonl: written\nlinks/mathlib/roadmap/result.jsonl: removed\n"
    assert capsys.readouterr().out == written
    assert not mathlib.exists()
    record = json.loads((blueprint / "links/roadmap/result.jsonl").read_text(encoding="utf-8"))
    assert (record["status"], record["extensions"], record["id"]) == (
        "formalized",
        {"org.autoform": {"origin": "cited"}},
        ARTICLE_ID,
    )
    node = load_graph(blueprint).nodes["result"]
    assert (node.proof_formalized, node.origin, node.mathlib_declaration, node.article_id) == (
        True,
        "cited",
        None,
        ARTICLE_ID,
    )


def test_an_annotated_article_keeps_its_annotations_id_over_one_beside_it(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    write_links(blueprint)
    page = blueprint / "roadmap/result.md"
    page.write_text(f"---\narticle_id: af_{'0' * 24}\n---\n\n# result\n", encoding="utf-8")

    assert _set(blueprint, str(page), "origin=cited").written == ("links/roadmap/result.jsonl",)
    assert json.loads((blueprint / "links/roadmap/result.jsonl").read_text(encoding="utf-8"))["id"] == ARTICLE_ID
    assert load_graph(blueprint).nodes["result"].article_id == ARTICLE_ID


def test_an_annotation_the_writer_could_not_write_back_is_refused(tmp_path: Path) -> None:
    """A module the writer cannot encode reads back as a file no annotation could carry, so nothing is rewritten."""

    frontmatter = "declaration: theorem\nmathlib_declaration: Foo.bar\nmathlib_file: Mathlib/Foo.lean"
    blueprint = _blueprint(tmp_path, {"result": frontmatter})
    write_links(blueprint)
    mathlib = blueprint / "links/mathlib/roadmap/result.jsonl"
    mathlib.write_bytes(mathlib.read_bytes().replace(b'"Mathlib.Foo"', b'"Mathlib.Foo."'))
    before = _bytes(blueprint)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "origin=cited") == [
        "roadmap/result.md: links/roadmap/result.jsonl is not what this command would write back; edit it by hand",
        "roadmap/result.md: links/mathlib/roadmap/result.jsonl is not what this command would write back; edit it "
        "by hand",
    ]
    assert _bytes(blueprint) == before


def test_a_malformed_id_in_an_annotation_is_replaced(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    _mark(blueprint)
    page = blueprint / "roadmap/result.md"
    own = blueprint / "links/roadmap/result.jsonl"
    own.write_bytes(writer.encode(blueprint, page, {"declaration": "lemma", "article_id": "af_F8D7"})[own])
    article_id = _proposed_id("result", hashlib.sha256(page.read_bytes()).hexdigest())

    assert _set(blueprint, str(page)) == LinksSetResult(("links/roadmap/result.jsonl",), ())
    assert json.loads(own.read_text(encoding="utf-8"))["id"] == article_id
    assert load_graph(blueprint).nodes["result"].article_id == article_id


def test_before_a_project_moves_its_facts_an_annotated_article_is_refused(tmp_path: Path) -> None:
    """Writing would add the writer's template, which marks a moved project, so the next call would move another."""

    blueprint = _blueprint(tmp_path, {"result": None, "other": "declaration: lemma"})
    (blueprint / "links/roadmap").mkdir(parents=True)
    full = [{"type": "lean", "uri": "../../..", "location": {"declaration": "P.r"}, "role": "states"}]
    (blueprint / "links/roadmap/result.jsonl").write_bytes(_about("result.md", object=full, kind="theorem"))
    before = _bytes(blueprint)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "origin=cited") == [
        "roadmap/result.md: annotated in links/roadmap/result.jsonl, in a project whose facts are not in "
        "blueprint/links/; record its facts there by hand"
    ]
    assert _bytes(blueprint) == before

    # Without the template an unannotated article stays in frontmatter, and no template appears.
    assert _set(blueprint, str(blueprint / "roadmap/other.md"), "origin=cited") == LinksSetResult(
        ("roadmap/other.md",), ()
    )
    assert not (blueprint / "links/roadmap/_defaults.json").exists()
    assert "origin: cited" in (blueprint / "roadmap/other.md").read_text(encoding="utf-8")


def test_an_annotation_this_command_does_not_own_is_refused(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    _mark(blueprint)
    about = {"subject": [{"type": "generic", "uri": "../roadmap/result.md"}], "kind": "theorem"}
    (blueprint / "links/notes.jsonl").write_text(json.dumps(about) + "\n", encoding="utf-8")
    before = _bytes(blueprint)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "origin=cited") == [
        "roadmap/result.md: annotated in links/notes.jsonl, which this command does not write; record its facts "
        "there by hand"
    ]
    assert _bytes(blueprint) == before


def test_a_writer_file_holding_more_than_it_writes_back_is_refused(tmp_path: Path) -> None:
    """A row about part of the article, here with a PDF end, is read by no command but would be lost."""

    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    write_links(blueprint)
    own = blueprint / "links/roadmap/result.jsonl"
    pdf = [{"type": "pdf", "uri": "../../sources/book.pdf"}]
    own.write_bytes(own.read_bytes() + _about("result.md#proof", object=pdf))
    before = _bytes(blueprint)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "origin=cited") == [
        "roadmap/result.md: links/roadmap/result.jsonl is not what this command would write back; edit it by hand"
    ]
    assert _bytes(blueprint) == before


def test_a_writer_file_another_article_shares_is_refused(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": "declaration: theorem", "other": "declaration: lemma"})
    write_links(blueprint)
    own = blueprint / "links/roadmap/result.jsonl"
    own.write_bytes(own.read_bytes() + _about("other.md", kind="lemma"))
    before = _bytes(blueprint)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "origin=cited") == [
        "roadmap/result.md: links/roadmap/result.jsonl also annotates other; move that row out first"
    ]
    assert _bytes(blueprint) == before


@pytest.mark.parametrize("annotated", [True, False], ids=["annotated", "unannotated"])
def test_a_writer_path_that_does_not_annotate_the_article_is_refused(tmp_path: Path, annotated: bool) -> None:
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    if annotated:
        write_links(blueprint)
    _mark(blueprint)
    # About part of the article, so it annotates nothing, where the writer keeps the article's Mathlib ends.
    mathlib = blueprint / "links/mathlib/roadmap/result.jsonl"
    mathlib.parent.mkdir(parents=True)
    mathlib.write_bytes(writer.dumps({"subject": [{"type": "generic", "uri": "../../../roadmap/result.md#part"}]}))
    before = _bytes(blueprint)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "origin=cited") == [
        "roadmap/result.md: links/mathlib/roadmap/result.jsonl exists but does not annotate it; move or remove it"
    ]
    assert _bytes(blueprint) == before


def test_a_writer_file_changed_while_the_graph_loads_is_refused(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    write_links(blueprint)
    own = blueprint / "links/roadmap/result.jsonl"
    real_load = links_set.load_graph

    def edit_then_load(blueprint_dir):
        own.write_bytes(own.read_bytes().replace(b'"kind": "theorem"', b'"kind": "lemma"'))
        return real_load(blueprint_dir)

    monkeypatch.setattr(links_set, "load_graph", edit_then_load)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "origin=cited") == [
        "roadmap/result.md: links/roadmap/result.jsonl changed while the graph was loaded; run the command again"
    ]
    assert b'"kind": "lemma"' in own.read_bytes()


def test_a_writer_file_changed_before_the_write_is_refused_and_kept(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    write_links(blueprint)
    own = blueprint / "links/roadmap/result.jsonl"
    real_write = writer.write

    def edit_then_write(changes, expected, done=None):
        own.write_bytes(own.read_bytes().replace(b'"kind": "theorem"', b'"kind": "lemma"'))
        return real_write(changes, expected, done)

    monkeypatch.setattr(writer, "write", edit_then_write)

    assert _refused(blueprint, str(blueprint / "roadmap/result.md"), "origin=cited") == [
        f"{own}: not written: it changed after it was read; run the command again",
        "nothing recorded",
    ]
    assert b'"kind": "lemma"' in own.read_bytes()
    assert b"origin" not in own.read_bytes()


def test_a_page_the_loader_or_git_would_read_otherwise_is_refused(tmp_path: Path) -> None:
    decomposed = "e\u0301"  # an e and a combining accent
    blueprint = _blueprint(tmp_path, {"marked": "declaration: lemma", decomposed: "declaration: lemma"})
    marked = blueprint / "roadmap/marked.md"
    marked.write_bytes(b"\xef\xbb\xbf" + marked.read_bytes())
    _mark(blueprint)

    assert _refused(blueprint, str(marked), "origin=cited") == [
        "roadmap/marked.md: starts with a byte-order mark; remove it and run again"
    ]
    plain = _blueprint(tmp_path / "plain", {"marked": "declaration: lemma"})
    (plain / "roadmap/marked.md").write_bytes(b"\xef\xbb\xbf" + (plain / "roadmap/marked.md").read_bytes())
    assert _refused(plain, str(plain / "roadmap/marked.md"), "origin=cited") == [
        "roadmap/marked.md: starts with a byte-order mark; remove it and run again"
    ]
    assert _refused(blueprint, str(blueprint / f"roadmap/{decomposed}.md"), "origin=cited") == [
        f"roadmap/{decomposed}.md: its path is not composed (NFC) as Git records it; {RENAME}"
    ]


def test_an_article_outside_the_blueprints_roadmap_is_refused(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": "declaration: lemma"})
    other = _blueprint(tmp_path / "other", {"result": "declaration: lemma"})
    (blueprint / "notes.md").write_text("# Notes\n", encoding="utf-8")
    (blueprint / "roadmap/data.txt").write_text("data\n", encoding="utf-8")
    outside = [blueprint / "notes.md", blueprint / "roadmap/data.txt", other / "roadmap/result.md"]
    before = _bytes(blueprint)

    assert _refused(blueprint, *map(str, outside), "origin=cited") == [
        f"{page}: not a Markdown article below {blueprint / 'roadmap'}" for page in outside
    ]
    missing = tmp_path / "missing"
    assert _refused(missing, str(blueprint / "roadmap/result.md"), "origin=cited") == [
        f"blueprint directory does not exist: {missing}"
    ]
    assert _bytes(blueprint) == before


def test_the_area_atlas_json_assigns_is_neither_written_nor_compared(tmp_path: Path) -> None:
    blueprint = _blueprint(
        tmp_path,
        {"listed": f"article_id: {ARTICLE_ID}\narea: Algebra", "unset": "declaration: lemma\nlean: P.u"},
    )
    atlas = {"schema": "autoform-atlas/v1", "areas": {"Geometry": ["listed", "unset"]}}
    (blueprint / "atlas.json").write_text(json.dumps(atlas), encoding="utf-8")
    _mark(blueprint)
    unset = blueprint / "roadmap/unset.md"
    article_id = _proposed_id("unset", hashlib.sha256(unset.read_bytes()).hexdigest())

    assert _set(blueprint, str(blueprint / "roadmap/listed.md"), unset=("area",)).written == (
        "links/roadmap/listed.jsonl",
        "roadmap/listed.md",
    )
    assert _set(blueprint, str(unset), "origin=cited").written == ("links/roadmap/unset.jsonl", "roadmap/unset.md")

    graph = load_graph(blueprint)
    assert (graph.nodes["listed"].area, graph.nodes["listed"].recorded_area) == ("Geometry", None)
    assert graph.nodes["unset"].article_id == article_id
    links = read_links(blueprint, {node.path: node.id for node in graph.nodes.values()})
    assert all("area" not in facts for facts in links.facts.values())


def test_the_cli_prints_what_it_wrote_and_exits_2_on_a_refusal(tmp_path: Path, capsys) -> None:
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    command = ["links", "set", str(blueprint), str(blueprint / "roadmap/result.md")]

    assert main([*command, "origin=cited", "area=Algebra", "--unset", "declaration"]) == 0
    assert capsys.readouterr().out == "roadmap/result.md: written\n"
    assert main([*command, "origin=cited"]) == 0
    assert capsys.readouterr().out == "unchanged\n"
    assert main([*command, "statement=retracted"]) == 2
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (
        "",
        "error: roadmap/result.md: with statement=retracted, statement: retracted needs the lean: declaration it "
        "retracts; without lean:, omit statement\n",
    )


def test_check_warns_once_about_frontmatter_facts_until_the_project_moves(tmp_path: Path, capsys) -> None:
    blueprint = _blueprint(tmp_path, {stem: "declaration: lemma" for stem in "abcd"} | {"plain": None})

    assert main(["check", str(blueprint), "--no-site-warnings"]) == 0
    assert capsys.readouterr().err == (
        "warning[deprecated]: reading articles' facts from frontmatter (4 articles: a, b, c and 1 more) is "
        "deprecated and will be removed in 0.8.0; once every agent working on this project runs Autoform 0.7.0 or "
        "later, move them into blueprint/links/ (autoform migrate links blueprint --write does it for a "
        "hand-authored vault)\n"
    )

    assert main(["migrate", "links", str(blueprint), "--write"]) == 0
    capsys.readouterr()
    assert main(["check", str(blueprint), "--no-site-warnings"]) == 0
    assert capsys.readouterr().err == ""
    (blueprint / "roadmap/late.md").write_text("---\ndeclaration: lemma\n---\n\n# late\n", encoding="utf-8")
    assert main(["check", str(blueprint), "--no-site-warnings"]) == 0
    captured = capsys.readouterr()
    assert (captured.err, "late" in captured.out) == ("", True)


def test_migrate_names_the_call_that_folds_a_kept_articles_extra_keys(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}\ndeclaration: theorem\norigin: cited"})
    write_links(blueprint)
    page = blueprint / "roadmap/result.md"
    page.write_text("---\nnot_ready: true\n---\n\n# result\n\nA statement.\n", encoding="utf-8")

    [entry] = plan_links(blueprint).kept

    call = "autoform links set blueprint blueprint/roadmap/result.md not_ready=true"
    assert entry.reasons == (
        "beside its annotation, declaration: nothing reads back as theorem",
        "beside its annotation, not_ready: true reads back as nothing",
        "beside its annotation, origin: nothing reads back as cited",
        f"`{call}` records its frontmatter's not_ready in the annotation",
    )
    # The call runs as printed, from the folder above the blueprint.
    monkeypatch.chdir(tmp_path)
    assert main(shlex.split(call)[1:]) == 0
    assert load_graph(blueprint).nodes["result"].not_ready

    # No call where links set would not make the fold: a key both hold with different values, the user's to settle;
    # facts the loader refuses together; a writer file holding more than links set writes back.
    for frontmatter in ("origin: bridged\ndiscussion: https://example.invalid/t", "proof: formalized"):
        page.write_text(f"---\n{frontmatter}\n---\n\n# result\n", encoding="utf-8")
        [entry] = plan_links(blueprint).kept
        assert entry.reasons and not any("autoform links set" in reason for reason in entry.reasons)
    own = blueprint / "links/roadmap/result.jsonl"
    own.write_bytes(own.read_bytes() + _about("result.md#proof", kind="lemma"))
    page.write_text("---\ndiscussion: https://example.invalid/t\n---\n\n# result\n", encoding="utf-8")
    [entry] = plan_links(blueprint).kept
    assert entry.reasons and not any("autoform links set" in reason for reason in entry.reasons)


def test_migrate_names_no_folding_call_that_no_annotation_could_hold(tmp_path: Path) -> None:
    """An axiom's proof: formalized is a fact no annotation records, so links set would refuse the fold."""

    blueprint = _blueprint(tmp_path, {"axiom": None})
    _mark(blueprint)
    page = blueprint / "roadmap/axiom.md"
    own = blueprint / "links/roadmap/axiom.jsonl"
    facts = {"declaration": "axiom", "lean": "P.ax", "statement": "formalized", "article_id": ARTICLE_ID}
    own.write_bytes(writer.encode(blueprint, page, facts)[own])
    page.write_text("---\nproof: formalized\n---\n\n# axiom\n", encoding="utf-8")

    [entry] = plan_links(blueprint).kept

    assert entry.reasons
    assert not any("autoform links set" in reason for reason in entry.reasons)


def test_migrate_never_passes_an_article_id_in_the_folding_call(tmp_path: Path, monkeypatch) -> None:
    """links set keeps the frontmatter's ID beside an annotation without one; it refuses article_id as an edit."""

    blueprint = _blueprint(tmp_path, {"result": None})
    _mark(blueprint)
    page = blueprint / "roadmap/result.md"
    own = blueprint / "links/roadmap/result.jsonl"
    own.write_bytes(writer.encode(blueprint, page, {"declaration": "theorem"})[own])
    page.write_text(f"---\narticle_id: {ARTICLE_ID}\nnot_ready: true\n---\n\n# result\n", encoding="utf-8")

    [entry] = plan_links(blueprint).kept

    call = "autoform links set blueprint blueprint/roadmap/result.md not_ready=true"
    assert entry.reasons[-1] == f"`{call}` records its frontmatter's article_id, not_ready in the annotation"
    monkeypatch.chdir(tmp_path)
    assert main(shlex.split(call)[1:]) == 0
    node = load_graph(blueprint).nodes["result"]
    assert (node.article_id, node.not_ready) == (ARTICLE_ID, True)


def test_migrate_says_when_the_folding_call_gives_the_article_an_id(tmp_path: Path, monkeypatch) -> None:
    """With no ID in the annotation or the frontmatter, links set gives the article one, so the plan says so."""

    blueprint = _blueprint(tmp_path, {"result": None})
    _mark(blueprint)
    page = blueprint / "roadmap/result.md"
    own = blueprint / "links/roadmap/result.jsonl"
    own.write_bytes(writer.encode(blueprint, page, {"declaration": "theorem"})[own])
    page.write_text("---\nnot_ready: true\n---\n\n# result\n", encoding="utf-8")

    [entry] = plan_links(blueprint).kept

    call = "autoform links set blueprint blueprint/roadmap/result.md not_ready=true"
    assert entry.reasons[-1] == (
        f"`{call}` records its frontmatter's not_ready in the annotation, and gives the article an article_id"
    )
    monkeypatch.chdir(tmp_path)
    assert main(shlex.split(call)[1:]) == 0
    node = load_graph(blueprint).nodes["result"]
    assert node.not_ready and node.article_id is not None


def test_migrate_names_no_folding_call_whose_new_id_another_article_holds(tmp_path: Path) -> None:
    """links set refuses the ID it would give an article without one when another article already holds it."""

    blueprint = _blueprint(tmp_path, {"result": None})
    _mark(blueprint)
    page = blueprint / "roadmap/result.md"
    own = blueprint / "links/roadmap/result.jsonl"
    own.write_bytes(writer.encode(blueprint, page, {"declaration": "theorem"})[own])
    page.write_text("---\nnot_ready: true\n---\n\n# result\n", encoding="utf-8")
    proposed = _proposed_id("result", hashlib.sha256(page.read_bytes()).hexdigest())
    (blueprint / "roadmap/other.md").write_text(f"---\narticle_id: {proposed}\n---\n\n# other\n", encoding="utf-8")

    [entry] = plan_links(blueprint).kept

    assert entry.reasons
    assert not any("autoform links set" in reason for reason in entry.reasons)


@pytest.mark.parametrize(
    ("annotation", "frontmatter", "others", "notes"),
    [
        # A definition's proof: formalized changes no byte of its annotation, so the plan would name it again.
        (
            {"declaration": "def", "lean": "P.d", "statement": "formalized", "article_id": ARTICLE_ID},
            "proof: formalized",
            {},
            False,
        ),
        # The loader drops a container's navigation from a declaration article, so links set's read-back refuses it.
        ({"declaration": "theorem", "article_id": ARTICLE_ID}, "navigation: collection", {}, False),
        # links set refuses an ID another article holds.
        (
            {"declaration": "theorem"},
            f"article_id: {ARTICLE_ID}\nnot_ready: true",
            {"other": f"article_id: {ARTICLE_ID}"},
            False,
        ),
        # links set refuses an article a file it does not write also annotates.
        ({"declaration": "theorem", "article_id": ARTICLE_ID}, "not_ready: true", {}, True),
        # links set's arguments strip a value padded inside its quotes, and refuse an unprintable one.
        ({"declaration": "theorem", "article_id": ARTICLE_ID}, 'area: " Number theory"', {}, False),
        ({"declaration": "theorem", "article_id": ARTICLE_ID}, "area: Number\u00a0theory", {}, False),
        # links set refuses facts the loader refuses together, though an annotation could hold them.
        (
            {"declaration": "theorem", "lean": "P.a", "mathlib": "true", "article_id": ARTICLE_ID},
            "statement: retracted",
            {},
            False,
        ),
    ],
    ids=["unchanged", "navigation", "taken-id", "other-file", "padded", "unprintable", "refused-together"],
)
def test_migrate_names_no_folding_call_that_changes_nothing_or_that_links_set_refuses(
    tmp_path: Path, annotation: dict[str, str], frontmatter: str, others: dict[str, str], notes: bool
) -> None:
    blueprint = _blueprint(tmp_path, {"result": None, **others})
    _mark(blueprint)
    page = blueprint / "roadmap/result.md"
    own = blueprint / "links/roadmap/result.jsonl"
    own.write_bytes(writer.encode(blueprint, page, annotation)[own])
    if notes:
        about = {"subject": [{"type": "generic", "uri": "../roadmap/result.md"}], "kind": "theorem"}
        (blueprint / "links/notes.jsonl").write_text(json.dumps(about) + "\n", encoding="utf-8")
    page.write_text(f"---\n{frontmatter}\n---\n\n# result\n", encoding="utf-8")

    [entry] = plan_links(blueprint).kept

    assert entry.reasons
    assert not any("autoform links set" in reason for reason in entry.reasons)


def test_migrate_names_a_fold_beside_navigation_its_annotation_already_holds(tmp_path: Path, monkeypatch) -> None:
    """The loader drops a declaration article's navigation before the write and after it, so a fold adding neither
    reads back as written."""

    blueprint = _blueprint(tmp_path, {"result": None})
    _mark(blueprint)
    page = blueprint / "roadmap/result.md"
    own = blueprint / "links/roadmap/result.jsonl"
    facts = {"declaration": "theorem", "navigation": "collection", "article_id": ARTICLE_ID}
    own.write_bytes(writer.encode(blueprint, page, facts)[own])
    frontmatter = "declaration: theorem\nnavigation: collection\ndiscussion: https://example.invalid/t"
    page.write_text(f"---\n{frontmatter}\n---\n\n# result\n", encoding="utf-8")

    [entry] = plan_links(blueprint).kept

    call = "autoform links set blueprint blueprint/roadmap/result.md discussion=https://example.invalid/t"
    assert entry.reasons[-1] == f"`{call}` records its frontmatter's discussion in the annotation"
    monkeypatch.chdir(tmp_path)
    assert main(shlex.split(call)[1:]) == 0
    assert not plan_links(blueprint).kept
