from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from autoform_cli import abstraction, debrief
from tests.test_debrief import _cli, _git, _repo
from tests.test_work import _edit

BASE = "af_000000000000000000000001"
PROVE = "af_000000000000000000000003"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path: Path):
    for name in (debrief.SUBAGENT_DEBRIEF_ENV, debrief.DEBRIEF_DIR_ENV, "AUTOFORM_WORKER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _copy(source_file, source, copy_file, copy, kind="private-declaration", what="a helper"):
    return {
        "kind": kind,
        "source_file": source_file,
        "source_declaration": source,
        "copy_file": copy_file,
        "copy_declaration": copy,
        "what": what,
    }


def _answer(*copies) -> str:
    return json.dumps({"copied_code": list(copies), "nothing_to_report": False})


def _proved(project: Path) -> None:
    _edit(project, "prove.md", "statement: formalized", "statement: formalized\nproof: formalized")


def test_the_form_separates_copied_code_from_wished_for_lemmas(monkeypatch, tmp_path: Path) -> None:
    project = _repo(tmp_path)
    monkeypatch.setenv(debrief.SUBAGENT_DEBRIEF_ENV, "1")
    code, out, _ = _cli(["debrief", "subagent", "form", PROVE, str(project), "--phase", "proof"], monkeypatch)
    assert code == 0
    assert '"copied_code"' in out and "Rules for `copied_code`" in out
    assert "is a `helper-lemma` in `infrastructure_proposals` instead" in out


def test_copies_of_copies_form_one_cluster_and_failed_attempts_are_left_out(tmp_path: Path) -> None:
    project = _repo(tmp_path)
    slot = project / ".wt" / "slot1"
    _git(project, "worktree", "add", "-q", str(slot))
    debrief.record(
        project,
        BASE,
        _answer(
            _copy(str(slot / "A.lean"), "foo_le", str(slot / "B.lean"), "foo_le'"),
            _copy("./A.lean", None, "B.lean", None, kind="inline"),
        ),
        phase="statement",
        worker_id="w1",
    )
    failed, _ = debrief.record(
        project, PROVE, _answer(_copy("A.lean", "foo_le", "D.lean", "foo_le")), phase="proof", worker_id="w2"
    )
    assert failed.outcome == "not-succeeded"
    _proved(project)
    debrief.record(
        project, PROVE, _answer(_copy("B.lean", "`foo_le'`", "C.lean", "foo_le")), phase="proof", worker_id="w3"
    )

    root = debrief.debrief_root(project)
    found = abstraction.clusters(root)

    chain, inline = sorted(found, key=lambda cluster: -len(cluster.copies))
    assert (len(chain.copies), len(inline.copies)) == (2, 1)
    # A slot inside the main checkout is its own worktree: its paths are relative to the slot.
    assert sorted(copy.copy_file for copy in chain.copies) == ["B.lean", "C.lean"]
    assert inline.copies[0].source_file == "A.lean" and inline.copies[0].kind == "inline"
    assert all(cluster.status == "open" for cluster in found)


def test_paths_reported_from_a_worktree_match_paths_reported_from_another(tmp_path: Path) -> None:
    project = _repo(tmp_path)
    slot1, slot2 = tmp_path / "slot1", tmp_path / "slot2"
    for slot in (slot1, slot2):
        _git(project, "worktree", "add", "-q", str(slot))
    debrief.record(slot1, BASE, _answer(_copy(str(slot1 / "A.lean"), "foo", str(slot1 / "B.lean"), "bar")),
                   phase="statement", worker_id="w1")
    debrief.record(slot2, BASE, _answer(_copy(str(slot2 / "B.lean"), "bar", str(slot2 / "C.lean"), "baz")),
                   phase="statement", worker_id="w2")
    (cluster,) = abstraction.clusters(debrief.debrief_root(project))
    assert sorted(copy.copy_file for copy in cluster.copies) == ["B.lean", "C.lean"]


def test_decisions_choose_a_target_and_close_a_cluster(monkeypatch, tmp_path: Path) -> None:
    project = _repo(tmp_path)
    (project / "B.lean").write_text("private theorem foo_le' : True := trivial\n")
    debrief.record(project, BASE, _answer(_copy("A.lean", "foo_le", "B.lean", "foo_le'")),
                   phase="statement", worker_id="w1")
    debrief.record(project, BASE, _answer(_copy("A.lean", "foo_le", "C.lean", "foo_le")),
                   phase="statement", worker_id="w2")
    root = debrief.debrief_root(project)
    (cluster,) = abstraction.clusters(root)
    # Records within one second are ordered by attempt ID, so either copy may come first.
    first, second = (copy.entry_id for copy in cluster.copies)
    assert cluster.cluster_id == first

    code, out, _ = _cli(["abstraction", "list", str(project), "--json"], monkeypatch)
    listed = json.loads(out)["clusters"]
    assert code == 0 and [entry["cluster_id"] for entry in listed] == [first]
    assert {copy["copy_file"]: copy["present"] for copy in listed[0]["copies"]} == {"B.lean": True, "C.lean": False}
    assert listed[0]["file"] is None

    code, out, _ = _cli(["abstraction", "target", second, str(project), "--file", "Shared/Foo.lean"], monkeypatch)
    assert code == 0 and f"cluster {first} will be raised into Shared/Foo.lean" in out
    assert abstraction.select(root, first).file == "Shared/Foo.lean"
    todo = (root / "abstractions" / "TODO.md").read_text()
    assert "Shared\\/Foo\\.lean" in todo and "no longer present" in todo

    code, _, err = _cli(["abstraction", "close", first, str(project), "--dismissed"], monkeypatch)
    assert code == 2 and "note" in err
    code, _, err = _cli(["abstraction", "close", first, str(project), "--raised", "", "--note", "x"], monkeypatch)
    assert code == 2 and "declaration" in err and abstraction.select(root, first).status == "open"
    code, out, _ = _cli(
        ["abstraction", "close", first, str(project), "--raised", "Shared.foo_le", "--file", "Shared/Foo.lean"],
        monkeypatch,
    )
    assert code == 0 and "raised" in out
    code, out, _ = _cli(["abstraction", "list", str(project), "--json"], monkeypatch)
    assert json.loads(out) == {"clusters": []}
    code, out, _ = _cli(["abstraction", "list", str(project), "--all", "--json"], monkeypatch)
    (closed,) = json.loads(out)["clusters"]
    assert (closed["status"], closed["declaration"], closed["file"]) == ("raised", "Shared.foo_le", "Shared/Foo.lean")
    code, out, _ = _cli(["abstraction", "list", str(project), "--all"], monkeypatch)
    assert code == 0 and f"{first} [raised] target: Shared/Foo.lean raised as Shared.foo_le" in out
    assert "Nothing to raise." in (root / "abstractions" / "TODO.md").read_text()

    # A later copy of the raised code opens its cluster again.
    debrief.record(project, BASE, _answer(_copy("C.lean", "foo_le", "E.lean", "foo_le")),
                   phase="statement", worker_id="w3")
    (reopened,) = abstraction.clusters(root)
    assert reopened.status == "open" and len(reopened.copies) == 3


def test_record_refreshes_the_to_do_list_and_unknown_clusters_are_refused(monkeypatch, tmp_path: Path) -> None:
    project = _repo(tmp_path)
    code, _, _ = _cli(
        ["debrief", "subagent", "record", BASE, str(project), "--phase", "statement", "--worker-id", "w1"],
        monkeypatch,
        stdin=_answer(_copy("A.lean", "foo", "B.lean", "foo")),
    )
    assert code == 0
    root = debrief.debrief_root(project)
    assert "B\\.lean foo copies A\\.lean foo" in (root / "abstractions" / "TODO.md").read_text()

    code, _, err = _cli(["abstraction", "target", "nope", str(project), "--file", "X.lean"], monkeypatch)
    assert code == 2 and "no cluster or copy" in err


def test_tampered_decisions_are_skipped(tmp_path: Path) -> None:
    project = _repo(tmp_path)
    debrief.record(project, BASE, _answer(_copy("A.lean", "foo", "B.lean", "foo")), phase="statement",
                   worker_id="w1")
    root = debrief.debrief_root(project)
    (cluster,) = abstraction.clusters(root)
    _, decision = abstraction.decide(root, cluster.cluster_id, "dismissed", note="diverged")
    directory = root / "abstractions"
    (directory / ("0" * 32 + ".json")).write_text(json.dumps({**decision.to_json(), "action": "raised"}))
    (directory / "junk.json").write_text("not json")
    assert [entry.decision_id for entry in abstraction.load_decisions(root)] == [decision.decision_id]
    assert abstraction.select(root, cluster.cluster_id).status == "dismissed"


def _record(project: Path, *copies, worker: str = "w1") -> None:
    debrief.record(project, BASE, _answer(*copies), phase="statement", worker_id=worker)


def test_a_reopened_cluster_keeps_what_it_was_raised_as_or_why_it_was_dismissed(monkeypatch, tmp_path: Path) -> None:
    project = _repo(tmp_path)
    _record(project, _copy("A.lean", "foo", "B.lean", "foo"))
    _record(project, _copy("X.lean", "bar", "Y.lean", "bar"), worker="w2")
    root = debrief.debrief_root(project)
    # Entry IDs, not cluster IDs: a later record within the same second may hold a cluster's oldest copy.
    entries = {copy.copy_file: copy.entry_id for cluster in abstraction.clusters(root) for copy in cluster.copies}
    foo, bar = entries["B.lean"], entries["Y.lean"]
    abstraction.decide(root, foo, "raised", file="Shared/Foo.lean", declaration="Shared.foo")
    abstraction.decide(root, bar, "dismissed", note="the copies diverged")

    _record(project, _copy("B.lean", "foo", "C.lean", "foo"), _copy("Y.lean", "bar", "Z.lean", "bar"), worker="w3")

    raised, dismissed = abstraction.select(root, foo), abstraction.select(root, bar)
    assert (raised.status, raised.declaration, raised.file) == ("open", "Shared.foo", "Shared/Foo.lean")
    assert (dismissed.status, dismissed.note, len(dismissed.copies)) == ("open", "the copies diverged", 2)
    code, out, _ = _cli(["abstraction", "list", str(project)], monkeypatch)
    assert code == 0 and "[open] target: Shared/Foo.lean last raised as Shared.foo" in out
    todo = (root / "abstractions" / "TODO.md").read_text()
    assert "**Last raised as:** Shared\\.foo" in todo and "**Note:** the copies diverged" in todo
    # A newer decision without a note keeps the older one's note, as it keeps the older file.
    abstraction.decide(root, bar, "target", file="Shared/Bar.lean")
    assert (abstraction.select(root, bar).file, abstraction.select(root, bar).note) == (
        "Shared/Bar.lean", "the copies diverged")


def test_a_decision_names_the_copies_it_covers_including_one_recorded_since_list(monkeypatch, tmp_path: Path) -> None:
    project = _repo(tmp_path)
    _record(project, _copy("A.lean", "foo", "B.lean", "foo"))
    root = debrief.debrief_root(project)
    (listed,) = abstraction.clusters(root)
    _record(project, _copy("A.lean", "foo", "C.lean", "foo"), worker="w2")

    code, out, _ = _cli(["abstraction", "target", listed.cluster_id, str(project), "--file", "S.lean"], monkeypatch)

    (cluster,) = abstraction.clusters(root)
    assert code == 0 and len(cluster.copies) == 2
    assert f"covering {', '.join(copy.entry_id for copy in cluster.copies)}" in out


def test_a_to_do_list_that_cannot_be_rewritten_only_warns(monkeypatch, tmp_path: Path) -> None:
    project = _repo(tmp_path)
    _record(project, _copy("A.lean", "foo", "B.lean", "foo"))
    root = debrief.debrief_root(project)
    (cluster,) = abstraction.clusters(root)
    # A directory where the to-do list should be makes every rewrite fail.
    (root / "abstractions" / "TODO.md").mkdir(parents=True)

    code, out, err = _cli(["abstraction", "list", str(project)], monkeypatch)
    assert code == 0 and cluster.cluster_id in out and "to-do list:" not in out
    assert "to-do list was not refreshed" in err
    code, out, err = _cli(["abstraction", "close", cluster.cluster_id, str(project), "--dismissed", "--note", "n"],
                          monkeypatch)
    assert code == 0 and "dismissed" in out and "to-do list was not refreshed" in err
    assert abstraction.select(root, cluster.cluster_id).status == "dismissed"


def test_copies_join_by_file_and_last_name_component_and_unknown_files_or_names_join_nothing(tmp_path: Path) -> None:
    project = _repo(tmp_path)
    # The same declaration, named in full once and briefly once, in one file spelled two ways.
    _record(project, _copy("src\\..\\A.lean", "Proj.A.foo", "B.lean", "foo'"))
    _record(project, _copy("A.lean", "`foo`", "C.lean", "foo"), worker="w2")
    # Two unrelated `bar`s whose files the agents did not know.
    _record(project, _copy("", "bar", "D.lean", "bar"), _copy("null", "bar", "E.lean", "bar"), worker="w3")
    _record(project, _copy("None", "bar", "F.lean", "bar"), worker="w4")
    # Two copies from one file whose names the agents did not know.
    _record(project, _copy("A.lean", "null", "G.lean", "g"), worker="w5")
    _record(project, _copy("A.lean", "NULL", "H.lean", "h"), worker="w6")
    # A dot inside «» is part of the name, so these two do not share `c»`.
    _record(project, _copy("P.lean", "X.«b.c»", "Q.lean", "p"), _copy("P.lean", "Y.«a.c»", "R.lean", "q"),
            worker="w7")

    groups = sorted(sorted(copy.copy_file for copy in cluster.copies) for cluster in abstraction.clusters(
        debrief.debrief_root(project)))

    assert groups == [["B.lean", "C.lean"], ["D.lean"], ["E.lean"], ["F.lean"], ["G.lean"], ["H.lean"], ["Q.lean"],
                      ["R.lean"]]


def test_a_copy_is_present_when_its_file_declares_it_and_unknown_when_its_file_cannot_tell(tmp_path: Path) -> None:
    project = _repo(tmp_path)
    (project / "B.lean").write_text("private theorem Foo.bar : True := trivial\ntheorem X.«b.c» : True := trivial\n")
    (project / "C.lean").write_text("theorem uses : True := Shared.baz\n")
    if hasattr(os, "mkfifo"):
        os.mkfifo(project / "pipe.lean")
    (project / "loop.lean").symlink_to("loop.lean")
    _record(
        project,
        _copy("A.lean", "bar", "B.lean", "Foo.bar"),
        _copy("A.lean", "c", "B.lean", "X.«b.c»"),
        _copy("A.lean", "d", "src\\..\\B.lean", "Foo.bar"),
        _copy("A.lean", "e", "None", "e"),
        _copy("A.lean", "baz", "C.lean", "baz"),
        _copy("A.lean", "f", "pipe.lean", "f"),
        _copy("A.lean", "g", "loop.lean", "g"),
    )
    root = debrief.debrief_root(project)
    state = {}
    for cluster in abstraction.clusters(root):
        state.update(abstraction.present(project, cluster))
    by_index = [state[entry] for entry in sorted(state, key=lambda entry: int(entry.rpartition(".")[2]))]

    # Declared under a qualified name, a «» name with a dot, and a backslash path: present. An unknown file: unknown.
    # Only a use of `Shared.baz` left: gone. A FIFO cannot tell (and is not waited on); a symlink loop is never present.
    assert by_index[:5] == [True, True, True, None, False]
    assert by_index[5] is None or not hasattr(os, "mkfifo")
    assert by_index[6] is not True


def test_merged_clusters_take_the_latest_target_and_prefer_a_raise_to_a_dismissal(tmp_path: Path) -> None:
    project = _repo(tmp_path)
    _record(project, _copy("A.lean", "foo", "B.lean", "foo"))
    _record(project, _copy("X.lean", "bar", "Y.lean", "bar"), worker="w2")
    root = debrief.debrief_root(project)
    first, second = (cluster.cluster_id for cluster in abstraction.clusters(root))
    abstraction.decide(root, first, "target", file="Old.lean")
    abstraction.decide(root, second, "target", file="New.lean")
    # A copy of foo's copy into bar's file joins the two targeted clusters; the latest target names the file.
    _record(project, _copy("B.lean", "foo", "Y.lean", "bar"), worker="w3")
    (merged,) = abstraction.clusters(root)
    assert (merged.status, merged.file) == ("open", "New.lean")
    abstraction.decide(root, merged.cluster_id, "raised", declaration="Shared.foo", file="Shared.lean")
    (closed,) = abstraction.clusters(root)
    assert (closed.status, closed.declaration, closed.file) == ("raised", "Shared.foo", "Shared.lean")

    # A raised cluster and a later dismissed one, joined by a new copy, show the raise.
    _record(project, _copy("P.lean", "baz", "Q.lean", "baz"), worker="w4")
    _record(project, _copy("R.lean", "qux", "S.lean", "qux"), worker="w5")
    baz, qux = (cluster.cluster_id for cluster in abstraction.clusters(root) if cluster.status == "open")
    abstraction.decide(root, baz, "raised", declaration="Shared.baz", file="Shared.lean")
    abstraction.decide(root, qux, "dismissed", note="diverged")
    _record(project, _copy("Q.lean", "baz", "S.lean", "qux"), worker="w6")
    (joined,) = (cluster for cluster in abstraction.clusters(root) if cluster.status == "open")
    assert (joined.declaration, joined.file) == ("Shared.baz", "Shared.lean")


@pytest.mark.parametrize(
    ("action", "fields", "message"),
    [
        ("target", {}, "needs the shared file"),
        ("raised", {"file": "S.lean"}, "needs the shared declaration"),
        ("dismissed", {}, "needs a note"),
        ("merged", {"note": "n"}, "action must be one of"),
    ],
)
def test_decide_refuses_a_decision_without_what_it_needs(tmp_path: Path, action: str, fields: dict[str, str],
                                                          message: str) -> None:
    project = _repo(tmp_path)
    _record(project, _copy("A.lean", "foo", "B.lean", "foo"))
    root = debrief.debrief_root(project)
    (cluster,) = abstraction.clusters(root)

    with pytest.raises(abstraction.AbstractionError, match=message):
        abstraction.decide(root, cluster.cluster_id, action, **fields)
    assert abstraction.load_decisions(root) == []


def test_the_debrief_bounds_copied_code_and_escapes_it_in_its_report(tmp_path: Path) -> None:
    project = _repo(tmp_path)
    many = [_copy("A.lean", f"foo{index}", "B.lean", f"foo{index}") for index in range(debrief.MAX_ITEMS + 5)]
    many[0] = _copy("A.lean", "x" * 2000, "B.lean", "*bold* [link](u)", what="`code` | pipe")
    entry, _ = debrief.record(project, BASE, _answer(*many), phase="statement", worker_id="w1")

    assert len(entry.answer.copied_code) == debrief.MAX_ITEMS
    assert len(entry.answer.copied_code[0].source_declaration) <= 512
    report = debrief.render_record_markdown(entry)
    assert "\\*bold\\* \\[link\\]\\(u\\)" in report and "\\`code\\` \\| pipe" in report
    assert "*bold*" not in report


def test_list_prints_each_cluster_and_its_copies(monkeypatch, tmp_path: Path) -> None:
    project = _repo(tmp_path)
    _record(project, _copy("A.lean", "foo", "B.lean", "foo'"), _copy("A.lean", None, "C.lean", None, kind="inline"))
    root = debrief.debrief_root(project)

    code, out, _ = _cli(["abstraction", "list", str(project)], monkeypatch)

    lines = out.splitlines()
    assert code == 0 and sum(" [open] target: not chosen" in line for line in lines) == 2
    assert any(line.startswith("  ") and "B.lean foo' <- A.lean foo [private-declaration, " in line for line in lines)
    assert any("C.lean (inline) <- A.lean (inline) [inline, " in line for line in lines)
    assert lines[-1] == f"to-do list: {root / 'abstractions' / 'TODO.md'}"
    code, out, _ = _cli(["abstraction", "list", str(project), "--all"], monkeypatch)
    assert code == 0
    for cluster in abstraction.clusters(root):
        abstraction.decide(root, cluster.cluster_id, "dismissed", note="n")
    code, out, _ = _cli(["abstraction", "list", str(project)], monkeypatch)
    assert code == 0 and out.splitlines()[0] == "no copied code to raise"
