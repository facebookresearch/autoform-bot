"""Tests for reporting and removing a repository's linked worktrees."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from autoform_cli import claims, worktrees

_REAL_PROCESS_ITER = psutil.process_iter


def _git(*args: str, cwd: Path, input_text: str | None = None) -> str:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(
        GIT_AUTHOR_NAME="test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    return subprocess.run(
        ["git", *args], cwd=cwd, input=input_text, capture_output=True, text=True, check=True, env=environment
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=main)
    (main / ".gitignore").write_text(".lake/\n", encoding="utf-8")
    (main / "Main.lean").write_text("theorem t : True := trivial\n", encoding="utf-8")
    _git("add", "-A", cwd=main)
    _git("commit", "--quiet", "-m", "start", cwd=main)
    return main


@pytest.fixture(autouse=True)
def processes(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """The process table every scan sees; empty unless a test fills it."""
    table: list[object] = []
    monkeypatch.setattr(worktrees.psutil, "process_iter", lambda attrs=None: list(table))
    return table


class _Process:
    def __init__(
        self,
        pid: int,
        cwd: Path | str,
        *,
        started: float = 1_000.0,
        euid: int | None = None,
        error: Exception | None = None,
    ) -> None:
        euid = os.geteuid() if euid is None else euid
        self.pid = pid
        self.info = {
            "pid": pid,
            "name": f"fake-{pid}",
            "create_time": started,
            "uids": SimpleNamespace(real=euid, effective=euid, saved=euid),
        }
        self._cwd = str(cwd)
        self._error = error

    def cwd(self) -> str:
        if self._error is not None:
            raise self._error
        return self._cwd

    def exe(self) -> str:
        return "/usr/bin/fake"


def _add_worktree(repo: Path, name: str, *arguments: str) -> Path:
    path = repo.parent / name
    _git("worktree", "add", "--quiet", *(arguments or ("-b", name)), str(path), cwd=repo)
    return path


def _commit(worktree: Path, name: str) -> str:
    (worktree / name).write_text(name, encoding="utf-8")
    _git("add", name, cwd=worktree)
    _git("commit", "--quiet", "-m", name, cwd=worktree)
    return _git("rev-parse", "HEAD", cwd=worktree)


def _entries(repo: Path) -> dict[str, dict[str, object]]:
    report = worktrees.report_worktrees(repo).as_dict()
    assert report["schema"] == worktrees.WORKTREES_SCHEMA
    return {Path(entry["path"]).name: entry for entry in report["worktrees"]}


def _manifest(repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(worktrees.report_worktrees(repo).to_json(), encoding="utf-8")
    return path


def _plant_lease(board: Path, key: str, **changes: object) -> None:
    lease: dict[str, object] = {
        "schema": claims.CLAIM_SCHEMA,
        "owner": "worker-1",
        "host": socket.gethostname(),
        "pid": 1,
        "acquired_at": time.time() - 60,
        "expires_at": time.time() + 600,
        "resource": key,
    }
    lease.update(changes)
    tree = _git("mktree", cwd=board, input_text="")
    commit = _git("commit-tree", tree, "-m", json.dumps(lease), cwd=board)
    _git("update-ref", claims.CLAIM_REF_PREFIX + key, commit, cwd=board)


@pytest.fixture
def board(repo: Path) -> Path:
    path = repo.parent / "board.git"
    _git("init", "--bare", "--quiet", str(path), cwd=repo.parent)
    _git("remote", "add", "origin", str(path), cwd=repo)
    return path


def test_clean_integrated_worktree_is_removable_and_names_what_removal_deletes(repo: Path) -> None:
    clean = _add_worktree(repo, "clean")
    (clean / ".lake").mkdir()
    (clean / ".lake" / "own.olean").write_bytes(b"x" * 50_000)
    cache = repo.parent / "lake-cache"
    cache.mkdir()
    (cache / "shared.olean").write_bytes(b"y" * 200_000)
    os.link(cache / "shared.olean", clean / ".lake" / "shared.olean")

    entry = _entries(repo)["clean"]

    assert entry["removable"] is True
    assert entry["protected_by"] == {}
    assert entry["branch"] == "clean"
    assert entry["head"] == _git("rev-parse", "HEAD", cwd=repo)
    assert entry["ignored"] == [".lake/"]
    assert entry["logical_bytes"] >= 250_000
    # The hardlink into the cache counts as logical bytes but frees nothing.
    assert 50_000 <= entry["freeable_bytes"] < 200_000
    assert entry["recover"] == f"git worktree add {clean} clean"


def test_main_worktree_and_the_targets_own_are_never_candidates(repo: Path) -> None:
    lead = _add_worktree(repo, "lead")
    _add_worktree(repo, "worker")

    assert set(_entries(lead)) == {"worker"}
    assert set(_entries(repo)) == {"lead", "worker"}


def test_dirty_worktree_and_evidence_written_into_it_are_protected(repo: Path) -> None:
    edited = _add_worktree(repo, "edited")
    (edited / "Main.lean").write_text("theorem t : 1 = 1 := rfl\n", encoding="utf-8")
    evidence = _add_worktree(repo, "evidence")
    (evidence / "skeleton.json").write_text("{}", encoding="utf-8")

    entries = _entries(repo)

    assert entries["edited"]["protected_by"] == {"dirty": "1 changed, 0 untracked: Main.lean"}
    assert entries["evidence"]["protected_by"] == {"dirty": "0 changed, 1 untracked: skeleton.json"}


def test_unintegrated_commits_protect_unless_an_equivalent_patch_landed(repo: Path) -> None:
    ahead = _add_worktree(repo, "ahead")
    _commit(ahead, "Ahead.lean")
    picked = _add_worktree(repo, "picked")
    commit = _commit(picked, "Picked.lean")
    _git("cherry-pick", commit, cwd=repo)
    detached = _add_worktree(repo, "detached", "--detach")
    _commit(detached, "Detached.lean")

    entries = _entries(repo)

    integration = _git("rev-parse", "HEAD", cwd=repo)[:12]
    assert entries["ahead"]["protected_by"] == {
        "unintegrated": f"1 commit(s) the integration HEAD {integration} lacks"
    }
    assert entries["picked"]["removable"] is True
    assert set(entries["detached"]["protected_by"]) == {"unintegrated"}
    assert entries["detached"]["branch"] is None
    assert entries["detached"]["recover"].startswith("git worktree add --detach ")


def test_locked_and_missing_worktrees_are_protected(repo: Path, tmp_path: Path) -> None:
    locked = _add_worktree(repo, "locked")
    _git("worktree", "lock", "--reason", "on a removable disk", str(locked), cwd=repo)
    missing = _add_worktree(repo, "missing")
    (missing / ".git").unlink()
    (missing / "Main.lean").unlink()
    (missing / ".gitignore").unlink()
    missing.rmdir()

    entries = _entries(repo)

    assert entries["locked"]["protected_by"] == {"locked": "on a removable disk"}
    assert set(entries["missing"]["protected_by"]) == {"missing"}


def test_a_git_lock_file_protects_however_old_as_does_an_unfinished_rebase(repo: Path) -> None:
    stale = _add_worktree(repo, "stale")
    lock = Path(_git("rev-parse", "--path-format=absolute", "--git-dir", cwd=stale)) / "index.lock"
    lock.write_text("", encoding="utf-8")
    os.utime(lock, (0, 0))
    rebasing = _add_worktree(repo, "rebasing")
    (Path(_git("rev-parse", "--path-format=absolute", "--git-dir", cwd=rebasing)) / "rebase-merge").mkdir()

    entries = _entries(repo)

    assert entries["stale"]["protected_by"] == {"git-operation": "index.lock"}
    assert entries["rebasing"]["protected_by"] == {"git-operation": "rebase-merge"}


def test_a_worktree_containing_another_is_protected(repo: Path) -> None:
    outer = _add_worktree(repo, "outer")
    inner = outer / ".lake" / "inner"
    _git("worktree", "add", "--quiet", "-b", "inner", str(inner), cwd=repo)

    entries = _entries(repo)

    assert entries["outer"]["protected_by"] == {"contains-worktree": str(inner)}
    assert entries["inner"]["removable"] is True


def test_a_running_process_in_a_worktree_marks_it_in_use(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worktrees.psutil, "process_iter", _REAL_PROCESS_ITER)
    busy = _add_worktree(repo, "busy")
    child = subprocess.Popen(["sleep", "60"], cwd=busy)
    try:
        evidence = _entries(repo)["busy"]["protected_by"]
    finally:
        child.kill()
        child.wait()

    assert f"pid {child.pid} (sleep)" in evidence["in-use"]
    assert "working directory" in evidence["in-use"]


def test_an_uninspectable_process_protects_every_worktree_but_another_users_is_skipped(
    repo: Path, processes: list[object]
) -> None:
    _add_worktree(repo, "first")
    _add_worktree(repo, "second")
    processes.append(_Process(41, "/elsewhere", euid=os.geteuid() + 1, error=psutil.AccessDenied(41)))
    assert all(entry["removable"] for entry in _entries(repo).values())

    processes.append(_Process(42, "/elsewhere", error=psutil.AccessDenied(42)))
    entries = _entries(repo)

    for entry in entries.values():
        assert entry["protected_by"] == {"liveness-unknown": "could not inspect pid 42 (fake-42)"}


def test_a_reused_pid_is_a_new_process_and_refuses_removal(
    repo: Path, tmp_path: Path, processes: list[object]
) -> None:
    worker = _add_worktree(repo, "worker")
    processes.append(_Process(77, tmp_path / "elsewhere", started=1_000.0))
    manifest = _manifest(repo, tmp_path)
    # The PID now belongs to a later process working in the worktree.
    processes[:] = [_Process(77, worker, started=2_000.0)]

    result = worktrees.remove_worktrees(repo, manifest)

    assert result.removed == ()
    [refused] = result.refused
    assert refused.protected_by == {"in-use": "pid 77 (fake-77) started 1970-01-01T00:33:20Z: working directory"}
    assert worker.is_dir()


def test_a_live_lease_from_this_host_protects_every_worktree(repo: Path, board: Path) -> None:
    _add_worktree(repo, "first")
    _add_worktree(repo, "second")
    _plant_lease(board, "af_live", note="first")
    _plant_lease(board, "af_expired", expires_at=time.time() - 1)
    _plant_lease(board, "af_elsewhere", host="another-host")

    entries = _entries(repo)

    for entry in entries.values():
        assert list(entry["protected_by"]) == ["claimed"]
        assert entry["protected_by"]["claimed"].startswith("af_live held by worker-1 until ")
        assert entry["protected_by"]["claimed"].endswith(" (note: first)")


def test_an_unreadable_board_or_lease_leaves_liveness_unknown(repo: Path, board: Path) -> None:
    _add_worktree(repo, "worker")
    _plant_lease(board, "af_hostless", host=None)
    assert _entries(repo)["worker"]["protected_by"] == {
        "liveness-unknown": "claim(s) naming no host or unreadable: af_hostless"
    }

    _git("remote", "set-url", "origin", str(repo.parent / "no-such-board.git"), cwd=repo)
    evidence = _entries(repo)["worker"]["protected_by"]

    assert list(evidence) == ["liveness-unknown"]
    assert evidence["liveness-unknown"].startswith("the claim board could not be read: ")


def test_remove_removes_only_the_manifests_removable_worktrees_and_keeps_branches(
    repo: Path, tmp_path: Path
) -> None:
    first = _add_worktree(repo, "first")
    (first / ".lake").mkdir()
    (first / ".lake" / "build.olean").write_bytes(b"x" * 10_000)
    second = _add_worktree(repo, "second")
    ahead = _add_worktree(repo, "ahead")
    _commit(ahead, "Ahead.lean")
    manifest = _manifest(repo, tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    for entry in data["worktrees"]:
        if Path(entry["path"]).name == "second":
            entry["removable"] = False  # the reviewer kept it
    manifest.write_text(json.dumps(data), encoding="utf-8")
    later = _add_worktree(repo, "later")  # removable now, but not in the manifest

    result = worktrees.remove_worktrees(repo, manifest)

    assert [entry.path.name for entry in result.removed] == ["first"]
    assert result.refused == ()
    assert result.removed[0].recover == f"git worktree add {first} first"
    assert not first.exists()
    assert second.is_dir() and ahead.is_dir() and later.is_dir()
    assert _git("branch", "--list", "first", cwd=repo) != ""
    assert result.as_dict()["schema"] == worktrees.REMOVAL_SCHEMA


def test_remove_refuses_a_worktree_that_changed_since_the_report(repo: Path, tmp_path: Path) -> None:
    moved = _add_worktree(repo, "moved")
    dirtied = _add_worktree(repo, "dirtied")
    gone = _add_worktree(repo, "gone")
    manifest = _manifest(repo, tmp_path)
    _git("checkout", "--quiet", "--detach", cwd=moved)
    (dirtied / "notes.md").write_text("new evidence", encoding="utf-8")
    _git("worktree", "remove", str(gone), cwd=repo)

    result = worktrees.remove_worktrees(repo, manifest)

    assert result.removed == ()
    refused = {entry.path.name: entry.protected_by for entry in result.refused}
    head = _git("rev-parse", "HEAD", cwd=repo)[:12]
    assert refused == {
        "moved": {"changed": f"now detached at {head}, not moved at {head}"},
        "dirtied": {"dirty": "0 changed, 1 untracked: notes.md"},
        "gone": {"changed": "no longer a linked worktree of this repository"},
    }
    assert moved.is_dir() and dirtied.is_dir()


def test_remove_refuses_after_a_lease_is_renewed(repo: Path, board: Path, tmp_path: Path) -> None:
    worker = _add_worktree(repo, "worker")
    _plant_lease(board, "af_worker", expires_at=time.time() - 1, note="worker")
    manifest = _manifest(repo, tmp_path)
    _plant_lease(board, "af_worker", note="worker")

    result = worktrees.remove_worktrees(repo, manifest)

    assert result.removed == ()
    assert list(result.refused[0].protected_by) == ["claimed"]
    assert worker.is_dir()


def test_remove_refuses_a_manifest_for_another_repository_or_a_malformed_one(repo: Path, tmp_path: Path) -> None:
    _add_worktree(repo, "worker")
    manifest = _manifest(repo, tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["repository"] = str(tmp_path / "other" / ".git")
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(worktrees.WorktreeError, match="describes the repository"):
        worktrees.remove_worktrees(repo, manifest)

    data["repository"] = str(Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo)))
    data["worktrees"][0]["removable"] = "yes"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(worktrees.WorktreeError, match="malformed worktree entry"):
        worktrees.remove_worktrees(repo, manifest)

    manifest.write_text('{"schema": "autoform-worktrees/v1", "schema": "x"}', encoding="utf-8")
    with pytest.raises(worktrees.WorktreeError, match="cannot read the manifest"):
        worktrees.remove_worktrees(repo, manifest)
    assert (repo.parent / "worker").is_dir()


def test_a_target_outside_a_checkout_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(worktrees.WorktreeError, match="not in a Git checkout"):
        worktrees.report_worktrees(tmp_path)


def test_only_the_worktrees_module_removes_a_worktree(repo_root: Path) -> None:
    """No command cleans up implicitly: `git worktree remove` appears in one module only."""
    users = sorted(
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / "autoform_cli").rglob("*.py")
        if '"worktree", "remove"' in path.read_text(encoding="utf-8")
    )
    assert users == ["autoform_cli/worktrees.py"]
