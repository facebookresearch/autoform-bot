"""`migrate links`: each article's frontmatter facts moved into its own annotation, read back the same, or kept."""

from __future__ import annotations

import dataclasses
import hashlib
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
from autoform_cli.article_identity import plan_article_ids
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


def _fields(blueprint: Path) -> dict[str, dict[str, object]]:
    """Every article's fields, its Lean and Mathlib names as names, and its derived status."""

    graph = load_graph(blueprint)
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


def test_the_writer_writes_these_bytes(tmp_path: Path) -> None:
    """The bytes `links set` shares: keys in the spec's order, flags as booleans, each end only its declaration under
    folder defaults that name the project and Mathlib, and one status, formalized, for a proved theorem, a
    definition and an axiom."""

    blueprint = tmp_path / "blueprint"
    page = blueprint / "roadmap/chapter/result.md"
    facts = {
        "article_id": ARTICLE_ID,
        "declaration": "theorem",
        "lean": "P.a P.b",
        "statement": "formalized",
        "proof": "formalized",
        "area": "Théorie des groupes",
        "origin": "cited",
        "not_ready": "false",
        "mathlib": "true",
        "mathlib_declaration": "Nat.succ_ne_zero",
        "mathlib_file": "Mathlib/Logic/Basic.lean",
    }

    changes = writer.encode(blueprint, page, facts)

    links = blueprint / "links"
    own = links / "roadmap/chapter/result.jsonl"
    assert changes == {
        own: (
            '{"subject": [{"type": "generic", "uri": "../../../roadmap/chapter/result.md"}], "object": [{"location": '
            '{"declaration": "P.a"}, "role": "states"}, {"location": {"declaration": "P.b"}}], "status": "formalized", '
            f'"kind": "theorem", "id": "{ARTICLE_ID}", "extensions": {{"org.autoform": {{"area": "Théorie des '
            'groupes", "mathlib": true, "origin": "cited"}}}\n'
        ).encode(),
        links / "mathlib/roadmap/chapter/result.jsonl": (
            '{"subject": [{"type": "generic", "uri": "../../../../roadmap/chapter/result.md"}], "object": '
            '[{"location": {"declaration": "Nat.succ_ne_zero", "module": "Mathlib.Logic.Basic"}}], '
            f'"id": "{ARTICLE_ID}"}}\n'
        ).encode(),
        links / "roadmap/_defaults.json": b'{"object": {"type": "lean", "uri": "../../.."}}\n',
        links / "mathlib/_defaults.json": (
            b'{"object": {"type": "lean", "uri": "https://github.com/leanprover-community/mathlib4"}}\n'
        ),
    }
    for kind in ("def", "noncomputable def", "axiom"):
        record = writer.encode(blueprint, page, {"declaration": kind, "lean": "P.d", "statement": "formalized"})
        assert json.loads(record[own])["status"] == "formalized"
    stated = writer.encode(blueprint, page, {"declaration": "lemma", "lean": "P.s", "statement": "formalized"})
    assert json.loads(stated[own])["status"] == "stated"
    # A module catalog's status covers every name it inventories, so no end has a role.
    catalog = writer.encode(blueprint, page, {"catalog": "module", "lean": "P.c1 P.c2", "statement": "formalized"})
    assert json.loads(catalog[own])["object"] == [{"location": {"declaration": name}} for name in ("P.c1", "P.c2")]
    # Without Mathlib names the Mathlib record goes, and a page's path is a reference.
    spaced = writer.encode(blueprint, blueprint / "roadmap/a b.md", {})
    assert spaced[links / "mathlib/roadmap/a b.jsonl"] is None
    subject = b'{"subject": [{"type": "generic", "uri": "../../roadmap/a%20b.md"}], '
    assert spaced[links / "roadmap/a b.jsonl"] == subject + b'"object": []}\n'


def test_the_writer_refuses_what_no_annotation_holds(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    page = blueprint / "roadmap/result.md"

    with pytest.raises(writer.Unencodable, match="without a lean: name"):
        writer.encode(blueprint, page, {"statement": "formalized"})
    with pytest.raises(writer.Unencodable, match="on an axiom"):
        proved = {"declaration": "axiom", "lean": "P.x", "statement": "formalized", "proof": "formalized"}
        writer.encode(blueprint, page, proved)
    with pytest.raises(ValueError, match="not a page the writer annotates"):
        writer.annotation_paths(blueprint, blueprint / "links/roadmap/result.md")
    # A fact a later loader reads but the writer does not record would be stripped and never written.
    with pytest.raises(writer.Unencodable, match="later, which the writer does not record"):
        writer.encode(blueprint, page, {"declaration": "theorem", "later": "x"})


def test_the_writer_replaces_only_the_bytes_it_expected(tmp_path: Path) -> None:
    """What `links set` relies on past the plan's own check: a file that appeared since stays as it is."""

    path = tmp_path / "links/roadmap/result.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"theirs\n")

    with pytest.raises(writer.AnnotationWriteError, match="changed after it was read") as stopped:
        writer.write({path: b"ours\n"}, {})

    assert (stopped.value.written, path.read_bytes()) == ((), b"theirs\n")
    (step,) = writer.write({path: b"ours\n"}, {path: hashlib.sha256(b"theirs\n").hexdigest()})
    assert (step.previous, path.read_bytes()) == (b"theirs\n", b"ours\n")


@pytest.mark.parametrize("change", ["write", "remove"])
def test_a_stop_just_after_a_file_changes_is_undone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Each step is recorded before its file changes, so Ctrl-C right after the rename or the unlink still undoes it."""

    theirs = tmp_path / "links/roadmap/theirs.jsonl"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"theirs\n")
    ours = tmp_path / "links/roadmap/new/ours.jsonl"
    name = "atomic_write" if change == "write" else "_remove_if"
    real = getattr(writer, name)

    def change_then_stop(*args: object, **kwargs: object) -> None:
        real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(writer, name, change_then_stop)
    changes = {ours: b"ours\n"} if change == "write" else {theirs: None}
    expected = {} if change == "write" else {theirs: hashlib.sha256(b"theirs\n").hexdigest()}

    with pytest.raises(KeyboardInterrupt):
        writer.write(changes, expected)

    assert theirs.read_bytes() == b"theirs\n"
    assert not ours.parent.exists()


@pytest.mark.parametrize("change", ["create", "replace", "remove"])
def test_a_stop_before_a_file_changes_leaves_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    """Ctrl-C after a step is recorded and before its file changes leaves a step whose file holds what it held, as
    for the records `links set` will replace and remove: undoing it is no error and removes only its folders."""

    path = tmp_path / "links/roadmap/a.jsonl"
    old = None if change == "create" else b"old\n"
    if old is not None:
        path.parent.mkdir(parents=True)
        path.write_bytes(old)

    def stop(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(writer, "_remove_if" if change == "remove" else "atomic_write", stop)
    changes = {path: None if change == "remove" else b"new\n"}
    expected = {} if old is None else {path: hashlib.sha256(old).hexdigest()}
    done: list[writer.Written] = []

    with pytest.raises(KeyboardInterrupt):
        writer.write(changes, expected, done)

    monkeypatch.undo()
    assert [step.path for step in done] == [path]
    writer.undo(done)
    if old is None:
        assert not (tmp_path / "links").exists()
    else:
        assert path.read_bytes() == old


@pytest.mark.parametrize(
    ("name", "after"),
    [("_remove_if", False), ("_remove_if", True), ("_remove_folders", True), ("atomic_write", False)],
    ids=["before-a-removal", "after-a-removal", "after-its-folder-went", "before-a-restore"],
)
def test_an_undo_stopped_midway_goes_once_more_over_what_it_had_not_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, after: bool
) -> None:
    """Ctrl-C midway through an undo, before or after a file changes, or once the folder a step made is gone, undoes
    the rest before it goes on: the step it cut short may have lost its file or its folders already."""

    theirs = tmp_path / "links/roadmap/theirs.jsonl"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"theirs\n")
    before = _bytes(tmp_path)
    new = tmp_path / "links/roadmap/new"
    changes = {theirs: b"ours\n", new / "a.jsonl": b"a\n", new / "b.jsonl": b"b\n"}
    done = writer.write(changes, {theirs: hashlib.sha256(b"theirs\n").hexdigest()})
    real = getattr(writer, name)
    stopped: list[object] = []

    def stop_the_first(first: object, *args: object, **kwargs: object) -> None:
        if first and not stopped:  # a file, or folders to remove
            stopped.append(first)
            if after:
                real(first, *args, **kwargs)
            raise KeyboardInterrupt
        real(first, *args, **kwargs)

    monkeypatch.setattr(writer, name, stop_the_first)

    with pytest.raises(KeyboardInterrupt) as interrupted:
        writer.undo(done)

    assert stopped
    assert _bytes(tmp_path) == before
    assert not new.exists()
    assert not hasattr(interrupted.value, "__notes__")


def test_a_step_an_interrupted_undo_finished_is_not_undone_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once an undo removed a file, the file there may be another run's, here with this run's bytes, as a second run
    of the same plan writes it, so the pass after Ctrl-C leaves it."""

    theirs = tmp_path / "links/roadmap/theirs.jsonl"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"theirs\n")
    ours = tmp_path / "links/roadmap/a.jsonl"
    done = writer.write({theirs: b"ours\n", ours: b"a\n"}, {theirs: hashlib.sha256(b"theirs\n").hexdigest()})
    real = writer.atomic_write
    calls: list[Path] = []

    def another_run_then_stop(path: Path, *args: object, **kwargs: object) -> None:
        calls.append(path)
        if len(calls) == 1:
            ours.write_bytes(b"a\n")  # another run writes the file this undo has just removed
            raise KeyboardInterrupt
        real(path, *args, **kwargs)

    monkeypatch.setattr(writer, "atomic_write", another_run_then_stop)

    with pytest.raises(KeyboardInterrupt):
        writer.undo(done)

    assert calls == [theirs, theirs]
    assert (theirs.read_bytes(), ours.read_bytes()) == (b"theirs\n", b"a\n")


@pytest.mark.parametrize("stopped_in", ["write", "undo"])
def test_a_file_a_stopped_undo_cannot_restore_is_named_on_the_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped_in: str
) -> None:
    """Ctrl-C inside a write, or midway through an undo, undoes what it can and goes on, naming on the interrupt a
    file edited since it was written, as a refused undo names it, rather than hiding it."""

    edited, other = tmp_path / "links/roadmap/a.jsonl", tmp_path / "links/roadmap/b.jsonl"
    changes = {edited: b"a\n", other: b"b\n"}
    name = "atomic_write" if stopped_in == "write" else "_remove_if"
    real = getattr(writer, name)
    calls: list[Path] = []
    stop_at = 2 if stopped_in == "write" else 1  # the write's second file, or the undo's first: the other

    def edit_then_stop(path: Path, *args: object, **kwargs: object) -> None:
        calls.append(path)
        if len(calls) == stop_at:
            edited.write_bytes(b"edited\n")
            if stopped_in == "write":
                real(path, *args, **kwargs)
            raise KeyboardInterrupt
        real(path, *args, **kwargs)

    if stopped_in == "write":
        monkeypatch.setattr(writer, name, edit_then_stop)
        with pytest.raises(KeyboardInterrupt) as stopped:
            writer.write(changes, {})
    else:
        done = writer.write(changes, {})
        monkeypatch.setattr(writer, name, edit_then_stop)
        with pytest.raises(KeyboardInterrupt) as stopped:
            writer.undo(done)

    assert calls[stop_at - 1] == other
    refused = f"{edited}: not undone: it changed while it was written; run the command again"
    assert stopped.value.__notes__ == [refused]
    assert (edited.read_bytes(), other.exists()) == (b"edited\n", False)


def test_a_step_that_stays_leaves_the_earlier_steps_of_its_articles(tmp_path: Path) -> None:
    """An undo that cannot restore a stripped page leaves the files written before it for that article, a template
    it shares with another article among them, so the article's facts stay on disk; the other article's own file is
    undone."""

    page, other_page = tmp_path / "roadmap/a.md", tmp_path / "roadmap/b.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"---\nfacts\n---\n")
    own, shared, other = (tmp_path / f"links/roadmap/{name}" for name in ("a.jsonl", "_defaults.json", "b.jsonl"))
    done = writer.write(
        {own: b"a\n", shared: b"t\n", other: b"b\n", page: b"stripped\n"},
        {page: hashlib.sha256(b"---\nfacts\n---\n").hexdigest()},
        articles={own: {page}, shared: {page, other_page}, other: {other_page}, page: {page}},
    )
    page.write_bytes(b"edited\n")  # undone first, and refused

    with pytest.raises(writer.AnnotationWriteError) as stuck:
        writer.undo(done)

    assert [step.path for step in stuck.value.written] == [own, shared, page]
    assert stuck.value.issues[1:] == (
        f"{shared}: not undone: {page} stays, and that article's records need this",
        f"{own}: not undone: {page} stays, and this holds its facts too",
    )
    assert (own.read_bytes(), shared.read_bytes(), other.exists()) == (b"a\n", b"t\n", False)


def test_a_template_that_stays_leaves_the_article_s_other_files_to_be_undone(tmp_path: Path) -> None:
    """A template holds no article's facts, so one that cannot be undone keeps nothing else: the article's record
    goes once its page is restored."""

    page = tmp_path / "roadmap/a.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"---\nfacts\n---\n")
    own, template = tmp_path / "links/roadmap/a.jsonl", tmp_path / "links/mathlib/_defaults.json"
    done = writer.write(
        {own: b"a\n", template: b"t\n", page: b"stripped\n"},
        {page: hashlib.sha256(b"---\nfacts\n---\n").hexdigest()},
        articles={own: {page}, template: {page}, page: {page}},
    )
    template.write_bytes(b"edited\n")  # its undo is refused

    with pytest.raises(writer.AnnotationWriteError) as stuck:
        writer.undo(done)

    assert [step.path for step in stuck.value.written] == [template]
    assert (page.read_bytes(), own.exists()) == (b"---\nfacts\n---\n", False)


@pytest.mark.parametrize("again", [False, True], ids=["refused", "interrupted-again"])
def test_an_interrupted_write_hands_its_caller_each_step_its_undo_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, again: bool
) -> None:
    """Ctrl-C inside a write puts in the caller's set each step the write's own undo finished, whose file may be
    another run's by the time the caller undoes, and not one it did not: refused, or stopped by Ctrl-C twice."""

    a, b = tmp_path / "links/roadmap/a.jsonl", tmp_path / "links/roadmap/b.jsonl"
    real = writer.atomic_write

    def stop_at_b(path: Path, *args: object, **kwargs: object) -> None:
        if path == b:
            raise KeyboardInterrupt
        real(path, *args, **kwargs)

    def refuse_or_stop(path: Path, want: str | None) -> None:
        if again:
            raise KeyboardInterrupt
        raise OSError("busy")

    monkeypatch.setattr(writer, "atomic_write", stop_at_b)
    monkeypatch.setattr(writer, "_remove_if", refuse_or_stop)
    done: list[writer.Written] = []
    finished: set[writer.Written] = set()

    with pytest.raises(KeyboardInterrupt) as stopped:
        writer.write({a: b"a\n", b: b"b\n"}, {}, done, finished)

    assert ([step.path for step in done], [step.path for step in finished]) == ([a, b], [b])
    assert stopped.value.__notes__ == [f"{a}: not undone: {'interrupted' if again else 'busy'}"]


def test_a_step_the_interrupted_pass_could_not_undo_is_tried_again_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step the first pass could not undo before Ctrl-C is not finished, so the second pass tries it again and,
    still refused, names it on the interrupt."""

    a, b, edited = (tmp_path / f"links/roadmap/{name}.jsonl" for name in "abc")
    done = writer.write({a: b"a\n", b: b"b\n", edited: b"c\n"}, {})
    edited.write_bytes(b"edited\n")  # undone first, and refused
    real = writer._remove_if
    calls: list[Path] = []

    def stop_at_b(path: Path, want: str | None) -> None:
        calls.append(path)
        if path == b and calls.count(b) == 1:
            raise KeyboardInterrupt
        real(path, want)

    monkeypatch.setattr(writer, "_remove_if", stop_at_b)

    with pytest.raises(KeyboardInterrupt) as stopped:
        writer.undo(done)

    assert calls == [edited, b, edited, b, a]
    assert stopped.value.__notes__ == [f"{edited}: not undone: it changed while it was written; run the command again"]
    assert (edited.read_bytes(), a.exists(), b.exists()) == (b"edited\n", False, False)


def test_a_second_interrupt_names_every_step_the_second_pass_had_not_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C again during the second pass stops it, and the new interrupt names each step that pass had not
    finished, since any of them may still be in effect."""

    a, b, c = (tmp_path / f"links/roadmap/{name}.jsonl" for name in "abc")
    done = writer.write({a: b"a\n", b: b"b\n", c: b"c\n"}, {})
    real = writer._remove_if
    calls: list[Path] = []

    def stop_twice(path: Path, want: str | None) -> None:
        calls.append(path)
        if len(calls) in (1, 3):  # c in the first pass, then b in the second
            raise KeyboardInterrupt
        real(path, want)

    monkeypatch.setattr(writer, "_remove_if", stop_twice)

    with pytest.raises(KeyboardInterrupt) as stopped:
        writer.undo(done)

    assert calls == [c, c, b]
    assert stopped.value.__notes__ == [f"{a}: not undone: interrupted", f"{b}: not undone: interrupted"]
    assert (a.exists(), b.exists(), c.exists()) == (True, True, False)


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
    """Every field and the derived status are unchanged, and the article gets the ID `migrate article-ids` plans."""

    blueprint = _blueprint(tmp_path, {"result": frontmatter})
    (planned,) = (entry.article_id for entry in plan_article_ids(blueprint).entries)
    before, warnings = _fields(blueprint), load_graph(blueprint).warnings

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
    before = _fields(blueprint)

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
    """Report-only: frontmatter that says more, less or otherwise than the annotation is kept, naming each key, as is
    a fact line the loader would refuse. Lines that hold no fact, which the loader ignores there, stay, and an ID only
    the annotation holds is no difference."""

    blueprint = _blueprint(
        tmp_path,
        {
            "same": "declaration: theorem\nlean: P.s\nstatement: formalized",
            "other": "declaration: theorem\norigin: cited",
            "more": "declaration: theorem\nnot_ready: true",
            "less": "declaration: theorem",
            "unparsed": "declaration: theorem\nstatement: maybe",
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
        "less": ("beside its annotation, not_ready: nothing reads back as true",),
        "unparsed": ("beside its annotation, its frontmatter's statement line does not parse",),
        "taken": ("links/roadmap/taken.jsonl exists but does not annotate it; move or remove it",),
    }
    stripped = {
        "roadmap/identified.md": b"# identified\n\nA statement.\n",
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

    reason = "links/roadmap/result.jsonl is not read as its annotation; the warnings about blueprint/links/ say why"
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

    def break_the_second(path: object) -> object:
        calls.append(path)
        if len(calls) == 2:
            raise stop
        return real(path)

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

    assert calls == ["other.jsonl", "named.jsonl", "named.jsonl", "_defaults.json", "flagged.jsonl"]
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
    assert load_graph(blueprint).nodes["b"].declaration == "theorem"
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
    """Ctrl-C as an article edited after the reload loses its own record still removes the record, so the loader
    reads the edit: left, the record would be read in its place."""

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
    assert load_graph(blueprint).nodes["b"].declaration == "theorem"


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
    130 naming the record that pass did not finish: a rerun would otherwise report the migration complete over it."""

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
        if len(calls) in (1, 3):  # the first pass's first removal, then the second pass's second
            raise KeyboardInterrupt
        real(path, want)

    monkeypatch.setattr(writer, "_remove_if", stop_twice)

    try:
        status = main(["migrate", "links", str(blueprint), "--write"])
    except KeyboardInterrupt:  # past main, it would stop the test run
        pytest.fail("the interrupt went past main")
    assert status == 130
    assert [path.name for path in calls] == ["_defaults.json", "_defaults.json", "new.jsonl"]
    assert capsys.readouterr().err.endswith("links/roadmap/new.jsonl: not undone: interrupted\n")
    assert (blueprint / "links/roadmap/new.jsonl").exists()
    assert not (blueprint / "links/roadmap/_defaults.json").exists()


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
    planned = {entry.path_id: entry.article_id for entry in plan_article_ids(blueprint).entries}
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

    # A workflow `init` wrote before annotations, which it keeps, looks for approvals only in frontmatter.
    workflow = tmp_path / ".github/workflows/autoform-verify.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("git grep -q 'review_approved[[:space:]]*:' -- blueprint/roadmap\n", encoding="utf-8")
    assert main(["migrate", "links", str(blueprint), "--write", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["kept_count"] == 1
    assert "links/roadmap/a.jsonl: written" in captured.err
    assert "roadmap/a.md: frontmatter facts removed" in captured.err
    assert "note: this version's skills still write frontmatter" in captured.err
    assert "note: the project's own tools that read articles' frontmatter" in captured.err
    assert "note: the project's verify workflow looks for approvals only in blueprint/roadmap" in captured.err
    assert main(["migrate", "links", str(blueprint), "--check"]) == 1

    (blueprint / "roadmap/b.md").write_text(f"---\n{axiom}\n---\n\n# b\n", encoding="utf-8")
    assert main(["migrate", "links", str(blueprint), "--write"]) == 0
    capsys.readouterr()
    assert main(["migrate", "links", str(blueprint), "--check"]) == 0
    assert capsys.readouterr().out == "OK: no article of 3 has frontmatter facts to move\n"


def test_a_verify_workflow_init_writes_draws_no_note(tmp_path: Path, repo_root: Path) -> None:
    """The generated workflow finds approvals in blueprint/links/, and the stub `init --no-verify-workflow` writes
    has no review step to edit."""

    blueprint = _blueprint(tmp_path, {"a": None})
    workflow = tmp_path / ".github/workflows/autoform-verify.yml"
    workflow.parent.mkdir(parents=True)

    def noted() -> bool:
        return any("verify workflow" in note for note in links_migration.pin_notes("", blueprint))

    shutil.copyfile(repo_root / "autoform_cli/templates/github/workflows/autoform-verify.yml", workflow)
    assert not noted()
    workflow.write_bytes(scaffold_module._VERIFY_WORKFLOW_STUB)
    assert not noted()
