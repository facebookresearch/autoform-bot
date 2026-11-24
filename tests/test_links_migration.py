"""`migrate links`: each article's frontmatter facts moved into its own annotation, read back the same, or kept."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import stat
import unicodedata
from pathlib import Path

import pytest

from autoform_cli import annotation_writer as writer
from autoform_cli import links_migration
from autoform_cli import scaffold as scaffold_module
from autoform_cli.__main__ import main
from autoform_cli.annotations import read_links
from autoform_cli.article_identity import _proposed_id
from autoform_cli.graph import GraphValidationError, load_graph
from autoform_cli.lean import declaration_names
from autoform_cli.links_migration import FIRST_READER, LinksWriteError, write_links
from autoform_cli.status import derive

ARTICLE_ID = "af_f8d7b54996f51594f707d44f"
#: What moving facts changes by design: the article's bytes, and the hashes of them.
VOLATILE = {"source_sha256", "article_revision"}


def _blueprint(tmp_path: Path, articles: dict[str, str | None]) -> Path:
    """A blueprint with an article below roadmap/ per frontmatter given; None writes none."""

    blueprint = tmp_path / "blueprint"
    for stem, frontmatter in articles.items():
        page = blueprint / "roadmap" / f"{stem}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        head = "" if frontmatter is None else f"---\n{frontmatter}\n---\n\n"
        page.write_text(f"{head}# {stem}\n\nA statement.\n", encoding="utf-8")
    return blueprint


def _fields(blueprint: Path, *, frontmatter_facts: bool = False) -> dict[str, dict[str, object]]:
    """Every article's fields, its Lean and Mathlib names as names, and its derived status; *frontmatter_facts* reads
    them as migrate does before it moves them."""

    graph = load_graph(blueprint, frontmatter_facts=frontmatter_facts)
    statuses = derive(graph)
    fields: dict[str, dict[str, object]] = {}
    for node_id, node in graph.nodes.items():
        record = {key: value for key, value in dataclasses.asdict(node).items() if key not in VOLATILE}
        for key in ("lean", "mathlib_declaration"):
            record[key] = list(dict.fromkeys(declaration_names(record[key] or "")))
        record["status"] = statuses[node_id].state
        fields[node_id] = record
    return fields


def _bytes(blueprint: Path) -> dict[str, bytes]:
    files = (path for path in blueprint.rglob("*") if path.is_file())
    return {path.relative_to(blueprint).as_posix(): path.read_bytes() for path in files}


ROUND_TRIPS = {
    "proved": "declaration: theorem\nlean: P.a P.b\nstatement: formalized\nproof: formalized",
    "stated": "declaration: lemma\nlean: P.s\nstatement: formalized",
    "definition": "declaration: def\nlean: P.d\nstatement: formalized",
    "axiom": "declaration: axiom\nlean: P.x\nstatement: formalized",
    "retracted": "declaration: theorem\nlean: P.r\nstatement: retracted",
    "catalog": "catalog: module\nlean: P.c1, P.c2\nstatement: formalized\nproof: formalized",
    "mathlib": (
        "declaration: theorem\nmathlib: true\nmathlib_declaration: Nat.add_comm Nat.mul_comm\n"
        "mathlib_file: Mathlib/Algebra/Group/Defs.lean"
    ),
    "keys": (
        "origin: bridged\nnot_ready: yes\ndiscussion: 7\narea: Topology\nnavigation: collection\n"
        f"review_approved: {'0' * 64}"
    ),
    "false flag": "declaration: theorem\nnot_ready: false",
    "identified": f"article_id: {ARTICLE_ID}\ndeclaration: theorem\nlean: P.t",
    "bare": None,
}


@pytest.mark.parametrize("frontmatter", ROUND_TRIPS.values(), ids=ROUND_TRIPS)
def test_each_fact_reads_back_the_same_from_the_annotation(tmp_path: Path, frontmatter: str | None) -> None:
    """Every field and the derived status are unchanged, and the article keeps its ID or gets the proposed one."""

    blueprint = _blueprint(tmp_path, {"result": frontmatter})
    unmoved = load_graph(blueprint, frontmatter_facts=True)
    (node,) = unmoved.nodes.values()
    planned = node.article_id or _proposed_id(node.id, node.source_sha256)
    before, warnings = _fields(blueprint, frontmatter_facts=True), unmoved.warnings

    result = write_links(blueprint)

    assert result.plan.complete
    before["result"]["article_id"] = planned
    assert _fields(blueprint) == before
    assert (blueprint / "roadmap/result.md").read_text(encoding="utf-8") == "# result\n\nA statement.\n"
    assert load_graph(blueprint).warnings == warnings


def test_what_reads_back_alike_though_spelled_otherwise(tmp_path: Path) -> None:
    """The accepted differences: proof on a definition, which no annotation records and status never needed, and Lean
    names written another way."""

    blueprint = _blueprint(
        tmp_path,
        {
            "definition": "declaration: def\nlean: P.d\nstatement: formalized\nproof: formalized",
            "names": "declaration: theorem\nlean: P.a P.a P.b\nstatement: formalized",
        },
    )
    before = _fields(blueprint, frontmatter_facts=True)

    assert write_links(blueprint).plan.complete

    after = _fields(blueprint)
    graph = load_graph(blueprint)
    assert (before["definition"]["proof_formalized"], after["definition"]["proof_formalized"]) == (True, False)
    assert after["definition"]["status"] == before["definition"]["status"]
    assert graph.nodes["names"].lean == "P.a, P.b"
    assert after["names"]["lean"] == before["names"]["lean"]


def test_an_article_no_annotation_can_hold_keeps_its_frontmatter(tmp_path: Path) -> None:
    blueprint = _blueprint(
        tmp_path,
        {
            "axiom": "declaration: axiom\nlean: P.x\nstatement: formalized\nproof: formalized",
            "file": "declaration: theorem\nmathlib_file: Mathlib/Logic/Basic.lean",
            "module": "declaration: theorem\nmathlib_declaration: Nat.succ_ne_zero\nmathlib_file: Mathlib.Logic.Basic",
            "fine": "declaration: theorem",
            "bom": None,
        },
    )
    # The loader reads no frontmatter after a byte-order mark, so these facts would not move.
    (blueprint / "roadmap/bom.md").write_bytes(b"\xef\xbb\xbf---\ndeclaration: theorem\n---\n\n# bom\n")
    before = _bytes(blueprint)

    result = write_links(blueprint)

    assert {entry.path_id: entry.reasons for entry in result.plan.kept} == {
        "axiom": ("proof: formalized on an axiom, which no annotation records: it would stop counting as proved",),
        "bom": ("starts with a byte-order mark; remove it and run again",),
        "file": ("mathlib_file without mathlib_declaration, whose first Mathlib end would carry the module",),
        "module": ("mathlib_file 'Mathlib.Logic.Basic', which is not a module's file",),
    }
    assert result.stripped == ("roadmap/fine.md",)
    after = _bytes(blueprint)
    for stem in ("axiom", "bom", "file", "module"):
        assert after[f"roadmap/{stem}.md"] == before[f"roadmap/{stem}.md"]
    assert sorted(name for name in after if name.startswith("links/")) == [
        "links/roadmap/_defaults.json",
        "links/roadmap/fine.jsonl",
    ]


def test_an_article_whose_path_git_spells_otherwise_keeps_its_frontmatter(tmp_path: Path) -> None:
    """Git on macOS records names composed, so in a clone a record naming the page decomposed names no article."""

    decomposed = unicodedata.normalize("NFD", "théorème")
    blueprint = _blueprint(tmp_path, {decomposed: "declaration: theorem", "plain": "declaration: lemma"})
    if f"{decomposed}.md" not in os.listdir(blueprint / "roadmap"):
        pytest.skip("this file system composes names")

    result = write_links(blueprint)

    (kept,) = result.plan.kept
    assert kept.reasons == (
        "its path is not composed (NFC) as Git records it; rename it (in Git: git mv through a temporary name)",
    )
    assert (result.written, result.stripped) == (
        ("links/roadmap/plain.jsonl", "links/roadmap/_defaults.json"),
        ("roadmap/plain.md",),
    )


def test_frontmatter_beside_an_annotation_goes_only_where_the_annotation_holds_it_all(tmp_path: Path) -> None:
    """Report-only: frontmatter that says more or otherwise than the annotation is kept, naming each key, as is a fact
    line the loader would refuse or a key's second line; frontmatter that says less, the annotation holding all it
    states, goes. Lines that hold no fact, which the loader ignores there, stay, and an ID only the annotation holds
    is no difference."""

    blueprint = _blueprint(
        tmp_path,
        {
            "same": "declaration: theorem\nlean: P.s\nstatement: formalized",
            "other": "declaration: theorem\norigin: cited",
            "more": "declaration: theorem\nnot_ready: true",
            "less": "declaration: theorem",
            "unparsed": "declaration: theorem\nstatement: maybe",
            "twice": "declaration: theorem\ndeclaration: lemma",
            "tagged": "declaration: theorem\ntags:\n  - a\n  - b\naliases: [x]",
            "identified": "declaration: theorem",
            "taken": "declaration: theorem",
        },
    )

    def about(stem: str, **fields: object) -> dict[str, object]:
        return {"subject": [{"type": "generic", "uri": f"../roadmap/{stem}.md"}], **fields}

    stated = [{"type": "lean", "uri": "../..", "location": {"declaration": "P.s"}, "role": "states"}]
    rows = [
        about("same", kind="theorem", status="stated", object=stated),
        about("other", kind="theorem", extensions={"org.autoform": {"origin": "background"}}),
        about("more", kind="theorem"),
        about("less", kind="theorem", extensions={"org.autoform": {"not_ready": True}}),
        about("unparsed", kind="theorem"),
        about("twice", kind="theorem"),
        about("tagged", kind="theorem"),
        about("identified", kind="theorem", id=ARTICLE_ID),
    ]
    (blueprint / "links/roadmap").mkdir(parents=True)
    (blueprint / "links/notes.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    # About part of the article, so it annotates nothing, and where the writer would put the article's.
    part = {"subject": [{"type": "generic", "uri": "../../roadmap/taken.md#part"}]}
    (blueprint / "links/roadmap/taken.jsonl").write_text(json.dumps(part) + "\n", encoding="utf-8")
    before = _bytes(blueprint)

    result = write_links(blueprint)

    assert {entry.path_id: entry.reasons for entry in result.plan.kept} == {
        "other": ("beside its annotation, origin: cited reads back as background",),
        "more": ("beside its annotation, not_ready: true reads back as nothing",),
        "unparsed": ("beside its annotation, its frontmatter's statement line does not parse",),
        "twice": ("beside its annotation, its frontmatter gives declaration more than once",),
        "taken": ("links/roadmap/taken.jsonl exists but does not annotate it; move or remove it",),
    }
    stripped = {
        "roadmap/identified.md": b"# identified\n\nA statement.\n",
        "roadmap/less.md": b"# less\n\nA statement.\n",
        "roadmap/same.md": b"# same\n\nA statement.\n",
        "roadmap/tagged.md": b"---\ntags:\n  - a\n  - b\naliases: [x]\n---\n\n# tagged\n\nA statement.\n",
    }
    assert (result.written, result.stripped) == ((), tuple(stripped))
    after = _bytes(blueprint)
    assert {name: after.pop(name) for name in stripped} == stripped
    assert after == {name: data for name, data in before.items() if name not in stripped}


def test_a_record_a_defaults_file_above_reads_otherwise_is_undone_and_kept(tmp_path: Path) -> None:
    """A root template carrying a status and extensions changes what the writer's record says, which only the graph
    loaded again shows: that article loses the files written for it, and the template stays for the other."""

    blueprint = _blueprint(
        tmp_path,
        {
            "named": "declaration: theorem\nlean: P.n",
            "flagged": "declaration: theorem\nlean: P.f\nstatement: formalized\nproof: formalized\nnot_ready: true",
        },
    )
    (blueprint / "links").mkdir()
    root = {"status": "formalized", "extensions": {"org.autoform": {"not_ready": True}}}
    (blueprint / "links/_defaults.json").write_text(json.dumps(root), encoding="utf-8")
    before = _bytes(blueprint)

    result = write_links(blueprint)

    (kept,) = result.plan.kept
    assert (kept.path_id, kept.reasons) == (
        "named",
        (
            "not_ready: nothing reads back as true",
            "proof: nothing reads back as formalized",
            "statement: nothing reads back as formalized",
        ),
    )
    assert (result.written, result.stripped) == (
        ("links/roadmap/flagged.jsonl", "links/roadmap/_defaults.json"),
        ("roadmap/flagged.md",),
    )
    assert _bytes(blueprint)["roadmap/named.md"] == before["roadmap/named.md"]
    assert not (blueprint / "links/roadmap/named.jsonl").exists()


def test_a_record_the_loader_does_not_read_keeps_the_article(tmp_path: Path) -> None:
    """Below a defaults file that cannot be used the loader leaves the record out and still reads the frontmatter,
    which compares the same, so only the files it read show the record is not the article's annotation."""

    proved = f"article_id: {ARTICLE_ID}\ndeclaration: theorem\nlean: P.t\nstatement: formalized\nproof: formalized"
    blueprint = _blueprint(tmp_path, {"result": proved})
    (blueprint / "links").mkdir()
    (blueprint / "links/_defaults.json").write_text("[]", encoding="utf-8")
    before = _bytes(blueprint)

    result = write_links(blueprint)

    reason = (
        "links/roadmap/result.jsonl is not read as its annotation (loading it again warned: defaults files that do not "
        "hold one JSON object are left out (1): links/_defaults.json; annotation files below a defaults file that "
        "cannot be used are left out (1): links/roadmap/result.jsonl)"
    )
    assert [(entry.path_id, entry.reasons) for entry in result.plan.kept] == [("result", (reason,))]
    assert (result.written, result.stripped) == ((), ())
    assert _bytes(blueprint) == before
    assert [path.name for path in (blueprint / "links").iterdir()] == ["_defaults.json"]


def test_an_id_another_article_holds_keeps_the_article(tmp_path: Path) -> None:
    """Loading leaves out an ID an annotated article already uses, so the one the record carries reads back as
    nothing."""

    blueprint = _blueprint(tmp_path, {"a": None, "b": f"article_id: {ARTICLE_ID}\ndeclaration: theorem"})
    (blueprint / "links").mkdir()
    annotation = {"subject": [{"type": "generic", "uri": "../roadmap/a.md"}], "id": ARTICLE_ID}
    (blueprint / "links/notes.jsonl").write_text(json.dumps(annotation) + "\n", encoding="utf-8")
    before = _bytes(blueprint)

    result = write_links(blueprint)

    assert [(entry.path_id, entry.reasons) for entry in result.plan.kept] == [
        ("b", (f"article_id: {ARTICLE_ID} reads back as nothing",))
    ]
    assert (result.written, result.stripped) == ((), ())
    assert _bytes(blueprint) == before


def test_a_template_that_changes_another_articles_record_undoes_the_write(tmp_path: Path) -> None:
    """The project template the write adds is nearer than a root one naming Mathlib, so a hand-written end below
    both would turn from a Mathlib name into a Lean one: the whole write is undone, naming that article."""

    blueprint = _blueprint(tmp_path, {"new": "declaration: theorem", "old": None})
    (blueprint / "links/roadmap").mkdir(parents=True)
    (blueprint / "links/_defaults.json").write_text(json.dumps(writer.MATHLIB_TEMPLATE), encoding="utf-8")
    old = {
        "subject": [{"type": "generic", "uri": "../../roadmap/old.md"}],
        "object": [{"location": {"declaration": "Nat.succ_ne_zero"}}],
    }
    (blueprint / "links/roadmap/old.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
    assert load_graph(blueprint).nodes["old"].mathlib_declaration == "Nat.succ_ne_zero"
    before = _bytes(blueprint)

    changed = "roadmap/old.md: reads otherwise once the plan's files are written: lean: nothing reads back as Nat."
    with pytest.raises(LinksWriteError, match=re.escape(changed)) as stopped:
        write_links(blueprint)

    assert (stopped.value.issues[-1], stopped.value.written, stopped.value.stripped) == ("nothing written", (), ())
    assert _bytes(blueprint) == before


def test_a_stop_in_the_undo_after_a_refused_check_still_undoes_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C at the first removal of the undo that follows a refused check still undoes every file written: left,
    the template would read the other article's Mathlib end as a Lean name, and running again would report the
    migration complete."""

    blueprint = _blueprint(tmp_path, {"new": "declaration: theorem", "old": None})
    (blueprint / "links/roadmap").mkdir(parents=True)
    (blueprint / "links/_defaults.json").write_text(json.dumps(writer.MATHLIB_TEMPLATE), encoding="utf-8")
    old = {
        "subject": [{"type": "generic", "uri": "../../roadmap/old.md"}],
        "object": [{"location": {"declaration": "Nat.succ_ne_zero"}}],
    }
    (blueprint / "links/roadmap/old.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
    before = _bytes(blueprint)
    real = writer._remove_if
    calls: list[Path] = []

    def stop_the_first(path: Path, want: str | None) -> None:
        calls.append(path)
        if len(calls) == 1:
            raise KeyboardInterrupt
        real(path, want)

    monkeypatch.setattr(writer, "_remove_if", stop_the_first)

    with pytest.raises(KeyboardInterrupt):
        write_links(blueprint)

    assert len(calls) == 3
    assert _bytes(blueprint) == before
    assert load_graph(blueprint).nodes["old"].mathlib_declaration == "Nat.succ_ne_zero"


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="needs symbolic links")
def test_a_link_where_the_annotation_would_go_keeps_the_article(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem", "b": "declaration: lemma"})
    (blueprint / "links/roadmap").mkdir(parents=True)
    (blueprint / "links/roadmap/a.jsonl").symlink_to(tmp_path / "missing.jsonl")

    result = write_links(blueprint)

    assert {entry.path_id: entry.reasons for entry in result.plan.kept} == {
        "a": ("links/roadmap/a.jsonl exists but does not annotate it; move or remove it",),
    }
    assert result.stripped == ("roadmap/b.md",)


def test_a_template_goes_with_the_last_article_that_used_it(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"named": "declaration: theorem\nlean: P.n"})
    (blueprint / "links").mkdir()
    (blueprint / "links/_defaults.json").write_text(json.dumps({"status": "formalized"}), encoding="utf-8")
    before = _bytes(blueprint)

    result = write_links(blueprint)

    assert (result.written, result.stripped, [entry.path_id for entry in result.plan.kept]) == ((), (), ["named"])
    assert _bytes(blueprint) == before
    assert sorted(path.name for path in (blueprint / "links").iterdir()) == ["_defaults.json"]


def test_a_second_write_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem\nlean: P.a\nstatement: formalized", "b": None})
    write_links(blueprint)
    snapshot = _bytes(blueprint)

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("nothing is left to write")

    monkeypatch.setattr(writer, "atomic_write", refuse)
    monkeypatch.setattr(links_migration, "_replace_if_unchanged", refuse)
    result = write_links(blueprint)

    assert (result.written, result.stripped, result.plan.complete) == ((), (), True)
    assert _bytes(blueprint) == snapshot


@pytest.mark.parametrize(
    ("failing", "stop"),
    [(1, OSError), (3, OSError), (3, KeyboardInterrupt)],
    ids=["first-file", "third-file", "interrupted"],
)
def test_a_write_pass_that_stops_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: int, stop: type[BaseException]
) -> None:
    """Not even an empty links/, which loading would read, nor, after Ctrl-C, a record that a defaults file with a
    status could make read as proved."""

    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem", "b": "declaration: lemma"})
    before = _bytes(blueprint)
    real = writer.atomic_write
    calls: list[Path] = []

    def fail_one(path: Path, data: bytes, **kwargs: object) -> None:
        calls.append(path)
        if len(calls) == failing:
            raise stop("no space left")
        real(path, data, **kwargs)

    monkeypatch.setattr(writer, "atomic_write", fail_one)

    with pytest.raises(LinksWriteError if stop is OSError else stop, match="no space left") as stopped:
        write_links(blueprint)

    if stop is OSError:
        assert (stopped.value.written, stopped.value.stripped) == ((), ())
    assert _bytes(blueprint) == before
    assert not (blueprint / "links").exists()
    monkeypatch.setattr(writer, "atomic_write", real)
    assert write_links(blueprint).plan.complete


def test_a_folder_made_before_a_stop_is_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Making links/roadmap/ stops after links/ is made, and the folder never made is no reason to keep that one."""

    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem"})
    before = _bytes(blueprint)

    def make_the_first(self: Path, *args: object, **kwargs: object) -> None:
        os.mkdir(blueprint / "links")
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", make_the_first)

    with pytest.raises(LinksWriteError, match="not written: read-only file system"):
        write_links(blueprint)

    assert _bytes(blueprint) == before
    assert not (blueprint / "links").exists()


@pytest.mark.parametrize("same", [False, True], ids=["other-bytes", "same-bytes"])
def test_a_file_another_run_writes_first_stays_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same: bool
) -> None:
    """The check refuses a file another run wrote after this one read it, a step that changed nothing of this run's:
    the error names nothing written, and the undo leaves the other run's file, even one with this run's bytes, as a
    second run of the same plan writes. Deleting that one loses the article's facts once that run strips them."""

    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem"})
    real = writer.atomic_write
    theirs: dict[Path, bytes] = {}

    def another_run_first(path: Path, data: bytes, **kwargs: object) -> None:
        theirs[path] = data if same else b"{}\n"
        path.write_bytes(theirs[path])
        real(path, data, **kwargs)

    monkeypatch.setattr(writer, "atomic_write", another_run_first)

    with pytest.raises(LinksWriteError, match="not written: it changed while it was written") as stopped:
        write_links(blueprint)

    assert (stopped.value.written, stopped.value.stripped) == ((), ())
    [(path, data)] = theirs.items()
    assert path.read_bytes() == data


@pytest.mark.parametrize(
    "stop",
    [GraphValidationError(["roadmap/a.md: edited meanwhile"]), KeyboardInterrupt("edited meanwhile")],
    ids=["refused", "interrupted"],
)
def test_a_graph_that_stops_loading_after_the_write_is_undone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: BaseException
) -> None:
    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem"})
    before = _bytes(blueprint)
    real = links_migration.load_graph
    calls: list[object] = []

    def break_the_second(path: object, *, frontmatter_facts: bool) -> object:
        calls.append(path)
        if len(calls) == 2:
            raise stop
        return real(path, frontmatter_facts=frontmatter_facts)

    monkeypatch.setattr(links_migration, "load_graph", break_the_second)

    refused = isinstance(stop, GraphValidationError)
    with pytest.raises(LinksWriteError if refused else KeyboardInterrupt, match="edited meanwhile") as stopped:
        write_links(blueprint)

    if refused:
        assert stopped.value.written == ()
    assert _bytes(blueprint) == before
    assert not (blueprint / "links").exists()


def test_a_stop_just_after_the_write_pass_returns_is_undone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The list the writer fills is the caller's, so a stop after the writes and before the check finds every file."""

    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem", "b": "declaration: lemma"})
    before = _bytes(blueprint)
    real = writer.write

    def write_then_stop(*args: object, **kwargs: object) -> tuple[writer.Written, ...]:
        real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(writer, "write", write_then_stop)

    with pytest.raises(KeyboardInterrupt):
        write_links(blueprint)

    assert _bytes(blueprint) == before
    assert not (blueprint / "links").exists()


@pytest.mark.parametrize("undone_first", [0, 1], ids=["before", "midway"])
def test_a_stop_before_the_mismatches_are_undone_undoes_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, undone_first: int
) -> None:
    """Ctrl-C after the check and before the mismatched articles' files are all gone, before the first or between
    the two, undoes every file written: left, a record the check showed to read otherwise would be read in place of
    the frontmatter beside it, here as proved, and running the command again could not repair it."""

    blueprint = _blueprint(
        tmp_path,
        {
            "named": "declaration: theorem\nlean: P.n",
            "other": "declaration: theorem\nlean: P.o",
            "flagged": "declaration: theorem\nlean: P.f\nstatement: formalized\nproof: formalized\nnot_ready: true",
        },
    )
    (blueprint / "links").mkdir()
    root = {"status": "formalized", "extensions": {"org.autoform": {"not_ready": True}}}
    (blueprint / "links/_defaults.json").write_text(json.dumps(root), encoding="utf-8")
    before = _bytes(blueprint)
    real = writer.undo
    calls: list[int] = []

    def stop_the_first(written: list[writer.Written], finished: set[writer.Written] | None = None) -> None:
        calls.append(len(written))
        if len(calls) == 1:
            real(written[len(written) - undone_first :], finished)  # undo takes the last step first
            raise KeyboardInterrupt
        real(written, finished)

    monkeypatch.setattr(writer, "undo", stop_the_first)

    with pytest.raises(KeyboardInterrupt):
        write_links(blueprint)

    assert calls[0] == 2
    assert _bytes(blueprint) == before
    assert not (blueprint / "links/roadmap").exists()


def test_a_record_the_writers_undo_removed_is_left_to_another_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C inside the write pass: the writer's own undo removes the first record, which another run of the same
    plan then writes again with this run's bytes, and write_links' cleanup leaves it: the file may be that run's."""

    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem", "b": "declaration: lemma"})
    record = blueprint / "links/roadmap/a.jsonl"
    real_write, real_remove = writer.atomic_write, writer._remove_if
    theirs: list[bytes] = []

    def stop_before_b(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "b.jsonl":
            raise KeyboardInterrupt
        real_write(path, *args, **kwargs)

    def another_run_after_a(path: Path, want: str | None) -> None:
        data = path.read_bytes()
        real_remove(path, want)
        if path == record and not theirs:
            theirs.append(data)
            path.write_bytes(data)  # another run writes the record this undo has just removed

    monkeypatch.setattr(writer, "atomic_write", stop_before_b)
    monkeypatch.setattr(writer, "_remove_if", another_run_after_a)

    with pytest.raises(KeyboardInterrupt):
        write_links(blueprint)

    assert theirs
    assert record.read_bytes() == theirs[0]


def test_records_the_undo_of_the_mismatches_removed_are_left_to_another_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C in the undo of the mismatched articles' records leaves the cleanup neither record that undo removed, in
    either pass, though another run of the same plan has written each again with this run's bytes: the files may be
    that run's by then."""

    blueprint = _blueprint(
        tmp_path,
        {
            "named": "declaration: theorem\nlean: P.n",
            "other": "declaration: theorem\nlean: P.o",
            "flagged": "declaration: theorem\nlean: P.f\nstatement: formalized\nproof: formalized\nnot_ready: true",
        },
    )
    (blueprint / "links").mkdir()
    root = {"status": "formalized", "extensions": {"org.autoform": {"not_ready": True}}}
    (blueprint / "links/_defaults.json").write_text(json.dumps(root), encoding="utf-8")
    real = writer._remove_if
    calls: list[str] = []
    theirs: dict[Path, bytes] = {}

    def another_run_then_stop(path: Path, want: str | None) -> None:
        calls.append(path.name)
        if len(calls) == 2:  # named's, after other's: undo takes the last step first
            raise KeyboardInterrupt
        data = path.read_bytes()
        real(path, want)
        if len(calls) <= 3:  # the records the undo removes, in its first pass and its second
            theirs[path] = data
            path.write_bytes(data)

    monkeypatch.setattr(writer, "_remove_if", another_run_then_stop)

    with pytest.raises(KeyboardInterrupt):
        write_links(blueprint)

    assert calls == ["other.jsonl", "named.jsonl", "named.jsonl", "flagged.jsonl", "_defaults.json"]
    assert [path.read_bytes() for path in theirs if path.exists()] == list(theirs.values())
    assert sorted(path.name for path in theirs) == ["named.jsonl", "other.jsonl"]


def test_a_mismatched_record_that_cannot_be_undone_is_named_with_what_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mismatched article's record edited after the check stays, and the error names it beside the files of the
    article that read back the same, which stay in effect with nothing stripped."""

    blueprint = _blueprint(
        tmp_path,
        {
            "named": "declaration: theorem\nlean: P.n",
            "flagged": "declaration: theorem\nlean: P.f\nstatement: formalized\nproof: formalized\nnot_ready: true",
        },
    )
    (blueprint / "links").mkdir()
    root = {"status": "formalized", "extensions": {"org.autoform": {"not_ready": True}}}
    (blueprint / "links/_defaults.json").write_text(json.dumps(root), encoding="utf-8")
    before = _bytes(blueprint)
    real = links_migration._read_back

    def edit_after_the_check(*args: object, **kwargs: object) -> object:
        mismatched = real(*args, **kwargs)
        (blueprint / "links/roadmap/named.jsonl").write_bytes(b"{}\n")
        return mismatched

    monkeypatch.setattr(links_migration, "_read_back", edit_after_the_check)

    with pytest.raises(LinksWriteError, match="named.jsonl: not undone") as stopped:
        write_links(blueprint)

    assert sorted(stopped.value.written) == [
        "links/roadmap/_defaults.json",
        "links/roadmap/flagged.jsonl",
        "links/roadmap/named.jsonl",
    ]
    after = _bytes(blueprint)
    assert after["roadmap/flagged.md"] == before["roadmap/flagged.md"]  # read back the same, and not stripped
    assert after["roadmap/named.md"] == before["roadmap/named.md"]


def test_a_stop_while_stripping_is_completed_by_running_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An article edited after the reload is not stripped, and loses its own record, so the loader reads the edit
    and running again moves it."""

    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem", "b": "declaration: lemma"})
    real = links_migration._replace_if_unchanged
    calls: list[Path] = []

    def edit_the_second(path: Path, expected: str, content: bytes, mode: int) -> None:
        calls.append(path)
        if len(calls) == 2:
            path.write_text("---\ndeclaration: theorem\n---\n\n# b\n", encoding="utf-8")
        real(path, expected, content, mode)

    monkeypatch.setattr(links_migration, "_replace_if_unchanged", edit_the_second)

    assert main(["migrate", "links", str(blueprint), "--write"]) == 2
    err = capsys.readouterr().err
    for line in (
        "links/roadmap/a.jsonl: written",
        "links/roadmap/_defaults.json: written",
        "roadmap/a.md: frontmatter facts removed",
        "error: roadmap/b.md: frontmatter facts not removed: the article changed while the plan was applied; run the "
        "command again",
        "error: made 3 change(s) before stopping",
    ):
        assert line in err
    assert not (blueprint / "links/roadmap/b.jsonl").exists()
    assert load_graph(blueprint, frontmatter_facts=True).nodes["b"].declaration == "theorem"
    monkeypatch.setattr(links_migration, "_replace_if_unchanged", real)
    result = write_links(blueprint)
    assert (result.written, result.stripped, result.plan.complete) == (
        ("links/roadmap/b.jsonl",),
        ("roadmap/b.md",),
        True,
    )
    assert load_graph(blueprint).nodes["b"].declaration == "theorem"


def test_a_stop_while_an_unstripped_article_loses_its_record_still_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C as an article edited after the reload loses its own record still removes the record, so migrate reads
    the edit: left, the record would be read in its place."""

    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem", "b": "declaration: lemma"})
    real_replace, real_remove = links_migration._replace_if_unchanged, writer._remove_if
    replaced: list[Path] = []
    removed: list[Path] = []

    def edit_the_second(path: Path, expected: str, content: bytes, mode: int) -> None:
        replaced.append(path)
        if len(replaced) == 2:
            path.write_text("---\ndeclaration: theorem\n---\n\n# b\n", encoding="utf-8")
        real_replace(path, expected, content, mode)

    def stop_the_first(path: Path, want: str | None) -> None:
        removed.append(path)
        if len(removed) == 1:
            raise KeyboardInterrupt
        real_remove(path, want)

    monkeypatch.setattr(links_migration, "_replace_if_unchanged", edit_the_second)
    monkeypatch.setattr(writer, "_remove_if", stop_the_first)

    with pytest.raises(KeyboardInterrupt):
        write_links(blueprint)

    assert [path.name for path in removed] == ["b.jsonl", "b.jsonl"]
    assert not (blueprint / "links/roadmap/b.jsonl").exists()
    assert load_graph(blueprint, frontmatter_facts=True).nodes["b"].declaration == "theorem"


@pytest.mark.parametrize("stop", ["inside-the-write", "after-the-write", "again-in-its-undo"])
def test_cli_names_each_file_an_interrupted_write_could_not_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stop: str
) -> None:
    """Ctrl-C exits 130 and names each file the undo could not restore once, here a record edited meanwhile, as a
    refused undo names it: for a stop inside the write pass, whose file both the writer's cleanup and write_links'
    meet; for one just after it; and for a second Ctrl-C as the cleanup's undo begins, which goes once more over
    what that undo had not finished."""

    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem", "b": "declaration: lemma"})
    edited = blueprint / "links/roadmap/a.jsonl"
    real_write, real_remove = writer.atomic_write, writer._remove_if
    removed: list[Path] = []

    def edit_then_stop_before_b(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "b.jsonl":
            edited.write_bytes(b"{}\n")
            raise KeyboardInterrupt
        real_write(path, *args, **kwargs)

    def stop_after_the_write(*args: object, **kwargs: object) -> object:
        if stop == "after-the-write":
            edited.write_bytes(b"{}\n")
        raise KeyboardInterrupt

    def edit_then_stop(path: Path, want: str | None) -> None:
        removed.append(path)
        if len(removed) == 1:
            edited.write_bytes(b"{}\n")
            raise KeyboardInterrupt
        real_remove(path, want)

    if stop == "inside-the-write":
        monkeypatch.setattr(writer, "atomic_write", edit_then_stop_before_b)
    else:
        monkeypatch.setattr(links_migration, "_read_back", stop_after_the_write)
    if stop == "again-in-its-undo":
        monkeypatch.setattr(writer, "_remove_if", edit_then_stop)

    try:
        status = main(["migrate", "links", str(blueprint), "--write"])
    except KeyboardInterrupt:  # past main, it would stop the test run
        pytest.fail("the interrupt went past main")
    assert status == 130
    err = capsys.readouterr().err
    assert err.count("error: ") == 1
    assert "links/roadmap/a.jsonl: not undone: it changed while it was written; run the command again\n" in err
    assert edited.read_bytes() == b"{}\n"
    assert not (blueprint / "links/roadmap/b.jsonl").exists()


def test_cli_names_what_a_twice_interrupted_undo_after_a_refused_check_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second Ctrl-C during the second pass of the undo after a refused check, where no outer cleanup runs, exits
    130 naming the template that pass did not finish, which goes on changing what the existing record means."""

    blueprint = _blueprint(tmp_path, {"new": "declaration: theorem", "old": None})
    (blueprint / "links/roadmap").mkdir(parents=True)
    (blueprint / "links/_defaults.json").write_text(json.dumps(writer.MATHLIB_TEMPLATE), encoding="utf-8")
    old = {
        "subject": [{"type": "generic", "uri": "../../roadmap/old.md"}],
        "object": [{"location": {"declaration": "Nat.succ_ne_zero"}}],
    }
    (blueprint / "links/roadmap/old.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
    real = writer._remove_if
    calls: list[Path] = []

    def stop_twice(path: Path, want: str | None) -> None:
        calls.append(path)
        if len(calls) in (1, 3):  # the first pass's first removal, then the second pass's second, the template's
            raise KeyboardInterrupt
        real(path, want)

    monkeypatch.setattr(writer, "_remove_if", stop_twice)

    try:
        status = main(["migrate", "links", str(blueprint), "--write"])
    except KeyboardInterrupt:  # past main, it would stop the test run
        pytest.fail("the interrupt went past main")
    assert status == 130
    assert [path.name for path in calls] == ["new.jsonl", "new.jsonl", "_defaults.json"]
    assert capsys.readouterr().err.endswith("links/roadmap/_defaults.json: not undone: interrupted\n")
    assert (blueprint / "links/roadmap/_defaults.json").exists()
    assert not (blueprint / "links/roadmap/new.jsonl").exists()


@pytest.mark.parametrize("busy_name", ["a.jsonl", "_defaults.json"], ids=["record", "template"])
def test_cli_names_no_file_the_last_cleanup_undid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], busy_name: str
) -> None:
    """A file the writer's own cleanup could not undo, a record or the template beside it, and write_links' cleanup
    then did, goes unnamed: the last cleanup's notes replace the earlier ones. links/ goes too, even when the record
    that made it was undone while the template still filled it: left empty, it would still be read."""

    blueprint = _blueprint(tmp_path, {"a": "declaration: theorem", "b": "declaration: lemma"})
    before = _bytes(blueprint)
    real_write, real_remove = writer.atomic_write, writer._remove_if
    busy: list[Path] = []

    def stop_before_b(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "b.jsonl":
            raise KeyboardInterrupt
        real_write(path, *args, **kwargs)

    def busy_once(path: Path, want: str | None) -> None:
        if path.name == busy_name and not busy:
            busy.append(path)
            raise OSError("busy")
        real_remove(path, want)

    monkeypatch.setattr(writer, "atomic_write", stop_before_b)
    monkeypatch.setattr(writer, "_remove_if", busy_once)

    try:
        status = main(["migrate", "links", str(blueprint), "--write"])
    except KeyboardInterrupt:  # past main, it would stop the test run
        pytest.fail("the interrupt went past main")
    assert status == 130
    assert busy
    assert capsys.readouterr().err == ""
    assert _bytes(blueprint) == before
    assert not (blueprint / "links").exists()


def test_stripping_keeps_settings_comments_line_endings_and_modes(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"crlf": None, "bare": None, "rule": None})
    readme = blueprint / "roadmap/README.md"
    readme.write_text(f"---\nopen_statements: allowed\narticle_id: {ARTICLE_ID}\n---\n\n# Roadmap\n", encoding="utf-8")
    crlf = blueprint / "roadmap/crlf.md"
    crlf.write_bytes(b"---\r\n# why\r\ndeclaration: theorem\r\n---\r\n\r\n# Result\r\n")
    crlf.chmod(0o640)
    bare = blueprint / "roadmap/bare.md"
    bare.write_bytes(b"---\r\ndeclaration: lemma\r\n---\r\n\r\n# Bare\r\n")
    rule = blueprint / "roadmap/rule.md"
    rule.write_bytes(b"---\ndeclaration: lemma\n---\n\n---\n\n# Rule\n")
    mask = os.umask(0)
    os.umask(mask)

    assert write_links(blueprint).plan.complete

    assert readme.read_text(encoding="utf-8") == "---\nopen_statements: allowed\n---\n\n# Roadmap\n"
    assert crlf.read_bytes() == b"---\r\n# why\r\n---\r\n\r\n# Result\r\n"
    assert stat.S_IMODE(crlf.stat().st_mode) == 0o640
    assert bare.read_bytes() == b"# Bare\r\n"
    # Without its block, the body's rule would open one that never closes.
    assert rule.read_bytes() == b"---\n---\n\n---\n\n# Rule\n"
    assert stat.S_IMODE((blueprint / "links/roadmap/crlf.jsonl").stat().st_mode) == 0o666 & ~mask
    graph = load_graph(blueprint)
    assert graph.open_statements is True
    assert graph.nodes["roadmap"].article_id == ARTICLE_ID


def test_the_bundled_example_migrates_with_its_graph_unchanged(tmp_path: Path, repo_root: Path) -> None:
    blueprint = tmp_path / "blueprint"
    shutil.copytree(repo_root / "skills/setup/assets/cabannes-thesis-project/blueprint", blueprint)
    planned = {
        node.id: node.article_id or _proposed_id(node.id, node.source_sha256)
        for node in load_graph(blueprint).nodes.values()
    }
    before, warnings = _fields(blueprint), load_graph(blueprint).warnings

    result = write_links(blueprint)

    assert result.plan.complete
    for node_id, record in before.items():
        record["article_id"] = planned[node_id]
    assert _fields(blueprint) == before
    graph = load_graph(blueprint)
    assert graph.warnings == warnings
    for node in graph.nodes.values():
        assert writer.frontmatter_facts(node.id, node.path.read_text(encoding="utf-8")) == {}, node.id


def test_the_reader_names_each_file_that_annotates_an_article(tmp_path: Path) -> None:
    """What `links set` refuses over, naming the file: another file that annotates the article."""

    facts = "declaration: theorem\nmathlib: true\nmathlib_declaration: Nat.succ_ne_zero"
    blueprint = _blueprint(tmp_path, {"result": facts, "plain": None})
    write_links(blueprint)
    note = {"subject": [{"type": "generic", "uri": "../roadmap/result.md"}], "note": "seen"}
    (blueprint / "links/notes.jsonl").write_text(json.dumps(note) + "\n", encoding="utf-8")
    graph = load_graph(blueprint)

    links = read_links(graph.blueprint_dir, {node.path: node.id for node in graph.nodes.values()})

    assert links is not None
    assert {node_id: links.files[node.path] for node_id, node in graph.nodes.items()} == {
        "result": ("links/mathlib/roadmap/result.jsonl", "links/notes.jsonl", "links/roadmap/result.jsonl"),
        "plain": ("links/roadmap/plain.jsonl",),
    }


def test_cli_plans_checks_and_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    axiom = "declaration: axiom\nlean: P.x\nstatement: formalized"
    articles = {"a": "declaration: theorem", "b": f"{axiom}\nproof: formalized", "c": "declaration: lemma"}
    blueprint = _blueprint(tmp_path, articles)

    assert main(["migrate", "links", str(blueprint), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["schema"], payload["pending_count"], payload["kept_count"]) == ("autoform-links-plan/v1", 2, 1)
    # The template is written once, so only the first article that needs it lists it.
    assert {entry["path_id"]: (entry["action"], entry["files"]) for entry in payload["entries"]} == {
        "a": ("annotate", ["links/roadmap/_defaults.json", "links/roadmap/a.jsonl"]),
        "b": ("kept", []),
        "c": ("annotate", ["links/roadmap/c.jsonl"]),
    }
    assert main(["migrate", "links", str(blueprint), "--check"]) == 1
    out = capsys.readouterr().out
    assert "2 article(s) to move, 1 kept in frontmatter" in out
    assert "  roadmap/a.md: annotate in links/roadmap/_defaults.json, links/roadmap/a.jsonl" in out
    assert "  roadmap/b.md: kept: proof: formalized on an axiom" in out
    assert f"note: CI reads blueprint/links/ only at an Autoform commit from {FIRST_READER} on" in out
    # At an older pin CI need not fail, so it cannot be what catches the missing progress.
    assert "at an older AUTOFORM_REF the site shows no Lean progress, and CI may still pass\n" in out

    # A workflow `init` wrote before annotations, which it keeps, looks for approvals only in frontmatter.
    workflow = tmp_path / ".github/workflows/autoform-verify.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("git grep -q 'review_approved[[:space:]]*:' -- blueprint/roadmap\n", encoding="utf-8")
    assert main(["migrate", "links", str(blueprint), "--write", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["kept_count"] == 1
    assert "links/roadmap/a.jsonl: written" in captured.err
    assert "roadmap/a.md: frontmatter facts removed" in captured.err
    assert "skills still write frontmatter" not in captured.err
    assert "note: the project's own tools that read articles' frontmatter" in captured.err
    assert "note: the project's verify workflow looks for approvals only in blueprint/roadmap" in captured.err
    assert main(["migrate", "links", str(blueprint), "--check"]) == 1

    (blueprint / "roadmap/b.md").write_text(f"---\n{axiom}\n---\n\n# b\n", encoding="utf-8")
    assert main(["migrate", "links", str(blueprint), "--write"]) == 0
    capsys.readouterr()
    assert main(["migrate", "links", str(blueprint), "--check"]) == 0
    assert capsys.readouterr().out == "OK: no article of 3 has frontmatter facts to move\n"


def test_a_verify_workflow_init_writes_draws_no_note(tmp_path: Path, repo_root: Path) -> None:
    """The generated workflow finds approvals in blueprint/links/, and neither a workflow 0.6.0 wrote nor the stub
    `init --no-verify-workflow` writes has a review step to edit."""

    blueprint = _blueprint(tmp_path, {"a": None})
    workflow = tmp_path / ".github/workflows/autoform-verify.yml"
    workflow.parent.mkdir(parents=True)

    def noted() -> bool:
        return any("verify workflow" in note for note in links_migration.pin_notes("", blueprint))

    shutil.copyfile(repo_root / "autoform_cli/templates/github/workflows/autoform-verify.yml", workflow)
    assert not noted()
    workflow.write_bytes(scaffold_module._VERIFY_WORKFLOW_STUB)
    assert not noted()
    workflow.write_text(
        "name: autoform verify\njobs:\n  verify:\n    steps:\n      - run: autoform check blueprint\n", encoding="utf-8"
    )
    assert not noted()
