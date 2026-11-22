"""The annotation writer: the bytes it writes, each step recorded before its file changes, and an undo that leaves
what another run holds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autoform_cli import annotation_writer as writer
from autoform_cli.graph import load_graph
from autoform_cli.lean import declaration_names
from tests.write_faults import Faults

ARTICLE_ID = "af_f8d7b54996f51594f707d44f"


def _bytes(root: Path) -> dict[str, bytes]:
    files = (path for path in root.rglob("*") if path.is_file())
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in files}


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


def test_the_loader_reads_each_fact_the_writer_wrote(tmp_path: Path) -> None:
    """What the writer writes for a page, read by the graph loader with the page's frontmatter gone: each fact as it
    was given."""

    blueprint = tmp_path / "blueprint"
    page = blueprint / "roadmap/chapter/result.md"
    page.parent.mkdir(parents=True)
    (blueprint / "roadmap/README.md").write_text("# Roadmap\n", encoding="utf-8")
    (blueprint / "roadmap/chapter/README.md").write_text("# Chapter\n\n- [Result](result.md)\n", encoding="utf-8")
    page.write_text("# Result\n\nA statement.\n", encoding="utf-8")
    facts = {
        "article_id": ARTICLE_ID,
        "declaration": "theorem",
        "lean": "P.a P.b",
        "statement": "formalized",
        "proof": "formalized",
        "area": "Topology",
        "origin": "cited",
        "not_ready": "true",
        "discussion": "7",
        "mathlib": "true",
        "mathlib_declaration": "Nat.succ_ne_zero",
        "mathlib_file": "Mathlib/Logic/Basic.lean",
    }

    writer.write(writer.encode(blueprint, page, facts), {})
    graph = load_graph(blueprint)

    node = graph.nodes["chapter/result"]
    assert graph.warnings == ()
    assert (node.article_id, node.declaration) == (ARTICLE_ID, "theorem")
    assert list(declaration_names(node.lean or "")) == ["P.a", "P.b"]
    assert (node.statement_formalized, node.proof_formalized, node.not_ready) == (True, True, True)
    assert (node.area, node.origin, node.discussion) == ("Topology", "cited", "7")
    assert (node.mathlib, node.mathlib_declaration, node.mathlib_file) == (
        True,
        "Nat.succ_ne_zero",
        "Mathlib/Logic/Basic.lean",
    )


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
    Faults(monkeypatch).at(change, raises_after=KeyboardInterrupt)
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

    Faults(monkeypatch).at("remove" if change == "remove" else "write", raises=KeyboardInterrupt)
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
    ("step", "after"),
    [("remove", False), ("remove", True), ("remove folders", True), ("write", False)],
    ids=["before-a-removal", "after-a-removal", "after-its-folder-went", "before-a-restore"],
)
def test_an_undo_stopped_midway_goes_once_more_over_what_it_had_not_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str, after: bool
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
    stop = {"raises_after" if after else "raises": KeyboardInterrupt}
    stopped = Faults(monkeypatch).at(step, when=bool, nth=1, **stop)  # a file, or folders to remove

    with pytest.raises(KeyboardInterrupt) as interrupted:
        writer.undo(done)

    assert stopped.fired
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
    # Another run writes the file this undo has just removed, and Ctrl-C stops the undo as it puts theirs back.
    Faults(monkeypatch).at(
        "write", path=theirs, nth=1, before=lambda: ours.write_bytes(b"a\n"), raises=KeyboardInterrupt
    )

    with pytest.raises(KeyboardInterrupt):
        writer.undo(done)

    assert (theirs.read_bytes(), ours.read_bytes()) == (b"theirs\n", b"a\n")


@pytest.mark.parametrize("stopped_in", ["write", "undo"])
def test_a_file_a_stopped_undo_cannot_restore_is_named_on_the_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped_in: str
) -> None:
    """Ctrl-C inside a write, or midway through an undo, undoes what it can and goes on, naming on the interrupt a
    file edited since it was written, as a refused undo names it, rather than hiding it."""

    edited, other = tmp_path / "links/roadmap/a.jsonl", tmp_path / "links/roadmap/b.jsonl"
    changes = {edited: b"a\n", other: b"b\n"}

    def edit() -> None:
        edited.write_bytes(b"edited\n")

    # Stopped at the other file: the write's second, once it is written, or the undo's first, before it goes.
    if stopped_in == "write":
        Faults(monkeypatch).at("write", path=other, nth=1, before=edit, raises_after=KeyboardInterrupt)
        with pytest.raises(KeyboardInterrupt) as stopped:
            writer.write(changes, {})
    else:
        done = writer.write(changes, {})
        Faults(monkeypatch).at("remove", path=other, nth=1, before=edit, raises=KeyboardInterrupt)
        with pytest.raises(KeyboardInterrupt) as stopped:
            writer.undo(done)

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
        f"{own}: not undone: {page} stays, and this holds its facts too",
        f"{shared}: not undone: {page} stays, and that article's records need this",
    )
    assert (own.read_bytes(), shared.read_bytes(), other.exists()) == (b"a\n", b"t\n", False)


def test_a_template_that_stays_leaves_the_article_s_other_files_to_be_undone(tmp_path: Path) -> None:
    """A template holds no article's facts, so one that cannot be undone keeps nothing else: the article's record
    goes once its page is restored, and so does the other template the article needed."""

    page = tmp_path / "roadmap/a.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"---\nfacts\n---\n")
    own = tmp_path / "links/roadmap/a.jsonl"
    other, template = tmp_path / "links/roadmap/_defaults.json", tmp_path / "links/mathlib/_defaults.json"
    done = writer.write(
        {own: b"a\n", other: b"o\n", template: b"t\n", page: b"stripped\n"},
        {page: hashlib.sha256(b"---\nfacts\n---\n").hexdigest()},
        articles={own: {page}, other: {page}, template: {page}, page: {page}},
    )
    template.write_bytes(b"edited\n")  # undone before the other template, and refused

    with pytest.raises(writer.AnnotationWriteError) as stuck:
        writer.undo(done)

    assert [step.path for step in stuck.value.written] == [template]
    assert (page.read_bytes(), own.exists(), other.exists()) == (b"---\nfacts\n---\n", False, False)


def test_a_file_another_run_changed_keeps_every_step_of_its_article(tmp_path: Path) -> None:
    """A record another run changed since the write keeps the steps of its article written after it too, and the
    template they need. Undone first, the Mathlib record went and the page was restored, which left that record
    without them."""

    page = tmp_path / "roadmap/a.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"---\nfacts\n---\n")
    own, mathlib = tmp_path / "links/roadmap/a.jsonl", tmp_path / "links/mathlib/roadmap/a.jsonl"
    template = tmp_path / "links/mathlib/_defaults.json"
    done = writer.write(
        {own: b"a\n", mathlib: b"m\n", template: b"t\n", page: b"stripped\n"},
        {page: hashlib.sha256(b"---\nfacts\n---\n").hexdigest()},
        articles={own: {page}, mathlib: {page}, template: {page}, page: {page}},
    )
    own.write_bytes(b"theirs\n")

    with pytest.raises(writer.AnnotationWriteError) as stuck:
        writer.undo(done)

    assert [step.path for step in stuck.value.written] == [own, mathlib, template, page]
    kept = [path.read_bytes() for path in (own, mathlib, template, page)]
    assert kept == [b"theirs\n", b"m\n", b"t\n", b"stripped\n"]


@pytest.mark.parametrize(
    ("name", "before", "after", "kept"),
    [
        ("b.jsonl", None, b"b\n", True),
        ("b.jsonl", b"b\n", None, False),
        ("b.jsonl", b"b\n", b"c\n", True),
        (".b.jsonl", None, b"b\n", False),
        (".hidden/b.jsonl", None, b"b\n", False),
        ("notes.txt", None, b"n\n", False),
        ("_defaults.json", None, b"{}\n", False),
    ],
    ids=[
        "another-run-s-record",
        "a-record-there-before",
        "a-record-another-run-changed",
        "a-hidden-file",
        "a-hidden-folder",
        "not-an-annotation",
        "defaults",
    ],
)
def test_a_template_the_write_made_stays_while_another_run_s_records_need_it(
    tmp_path: Path, name: str, before: bytes | None, after: bytes | None, kept: bool
) -> None:
    """A template the write made stays, with nothing to name, once a record it did not write comes or changes below
    its folder, since that record was written reading its defaults. A record there before the template and unchanged
    since, a file the loader does not read (hidden, in a hidden folder, or not JSON), or another defaults file, which
    holds no record, is no reason to keep it."""

    folder = tmp_path / "links/mathlib"
    other = folder / "roadmap" / name
    if before is not None:
        other.parent.mkdir(parents=True)
        other.write_bytes(before)
    own, template = folder / "roadmap/a.jsonl", folder / "_defaults.json"
    done = writer.write({own: b"a\n", template: b"t\n"}, {})
    if after is not None:
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(after)

    writer.undo(done)

    assert (own.exists(), template.exists(), other.exists()) == (False, kept, True)


def test_a_defaults_file_a_move_carries_goes_whatever_records_come_beside_it(tmp_path: Path) -> None:
    """A ``_defaults.json`` in *moved* is no template, so a record another run wrote below its folder since is no
    reason to keep it: undone, it goes like any other file the write made."""

    folder = tmp_path / "links/mathlib"
    own, defaults = folder / "roadmap/a.jsonl", folder / "_defaults.json"
    done = writer.write({own: b"a\n", defaults: b"t\n"}, {}, moved={defaults})
    (folder / "roadmap/b.jsonl").write_bytes(b"b\n")

    writer.undo(done)

    assert (own.exists(), defaults.exists()) == (False, False)


def test_a_new_copy_another_run_changed_still_has_its_old_path_put_back(tmp_path: Path) -> None:
    """A file a move carries, tied to its old path as the move ties it, whose new copy another run changed: the old
    path is put back, as the rest of the move goes back to the old names, and the changed copy stays, named."""

    old, new = tmp_path / "links/roadmap/a.jsonl", tmp_path / "links/roadmap/b.jsonl"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"a\n")
    pair = {new: {old}, old: {old}}
    done = writer.write({new: b"a\n", old: None}, {old: hashlib.sha256(b"a\n").hexdigest()}, articles=pair, moved=pair)
    new.write_bytes(b"theirs\n")

    with pytest.raises(writer.AnnotationWriteError) as stuck:
        writer.undo(done)

    assert (old.read_bytes(), new.read_bytes()) == (b"a\n", b"theirs\n")
    assert [step.path for step in stuck.value.written] == [new]
    assert stuck.value.issues == (f"{new}: not undone: it changed while it was written; run the command again",)


def test_an_old_path_another_run_changed_after_an_undo_keeps_the_new_copy(tmp_path: Path) -> None:
    """A file a move carries whose old path an earlier undo put back, and which another run changed since: that file
    is theirs, so the new copy, not undone yet, keeps the moved bytes rather than going."""

    old, new = tmp_path / "links/roadmap/a.jsonl", tmp_path / "links/roadmap/b.jsonl"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"a\n")
    pair = {new: {old}, old: {old}}
    done = writer.write({new: b"a\n", old: None}, {old: hashlib.sha256(b"a\n").hexdigest()}, articles=pair, moved=pair)
    finished: set[writer.Written] = set()
    writer.undo(done[1:], finished)  # an earlier undo, cut short once it put the old path back
    old.write_bytes(b"theirs\n")

    with pytest.raises(writer.AnnotationWriteError) as stuck:
        writer.undo(done, finished)

    assert (old.read_bytes(), new.read_bytes()) == (b"theirs\n", b"a\n")
    assert [step.path for step in stuck.value.written] == [new]


@pytest.mark.parametrize("again", [False, True], ids=["refused", "interrupted-again"])
def test_an_interrupted_write_hands_its_caller_each_step_its_undo_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, again: bool
) -> None:
    """Ctrl-C inside a write puts in the caller's set each step the write's own undo finished, whose file may be
    another run's by the time the caller undoes, and not one it did not: refused, or stopped by Ctrl-C twice."""

    a, b = tmp_path / "links/roadmap/a.jsonl", tmp_path / "links/roadmap/b.jsonl"
    faults = Faults(monkeypatch)
    faults.at("write", path=b, raises=KeyboardInterrupt)
    faults.at("remove", raises=KeyboardInterrupt if again else OSError("busy"))
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
    Faults(monkeypatch).at("remove", path=b, nth=1, raises=KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt) as stopped:
        writer.undo(done)

    assert stopped.value.__notes__ == [f"{edited}: not undone: it changed while it was written; run the command again"]
    assert (edited.read_bytes(), a.exists(), b.exists()) == (b"edited\n", False, False)


def test_a_second_interrupt_names_every_step_the_second_pass_had_not_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C again during the second pass stops it, and the new interrupt names each step that pass had not
    finished, since any of them may still be in effect."""

    a, b, c = (tmp_path / f"links/roadmap/{name}.jsonl" for name in "abc")
    done = writer.write({a: b"a\n", b: b"b\n", c: b"c\n"}, {})
    faults = Faults(monkeypatch)
    faults.at("remove", path=c, nth=1, raises=KeyboardInterrupt)  # in the first pass
    faults.at("remove", path=b, nth=1, raises=KeyboardInterrupt)  # in the second

    with pytest.raises(KeyboardInterrupt) as stopped:
        writer.undo(done)

    assert stopped.value.__notes__ == [f"{a}: not undone: interrupted", f"{b}: not undone: interrupted"]
    assert (a.exists(), b.exists(), c.exists()) == (True, True, False)
