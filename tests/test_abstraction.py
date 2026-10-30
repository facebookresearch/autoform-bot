from __future__ import annotations

import json
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
