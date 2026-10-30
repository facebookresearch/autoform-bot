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
from autoform_cli.__main__ import main

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


def _admin(worktree: Path) -> Path:
    return Path(_git("rev-parse", "--path-format=absolute", "--git-dir", cwd=worktree))


def _age(repo: Path) -> None:
    """Make every linked worktree look untouched for an hour, so `recent` protects none."""
    hour_ago = time.time() - 3600
    for admin in (repo / ".git" / "worktrees").iterdir():
        worktree = Path((admin / "gitdir").read_text(encoding="utf-8").strip()).parent
        for path in (admin / "index", admin / "logs" / "HEAD", worktree / ".lake"):
            if path.exists():
                os.utime(path, (hour_ago, hour_ago))


def _codes(entry: dict[str, object]) -> dict[str, str]:
    return {item["code"]: item["detail"] for item in entry["protected_by"]}


def _entries(target: Path, **options: object) -> dict[str, dict[str, object]]:
    report = worktrees.report_worktrees(target, **options).as_dict()
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
    _age(repo)

    report = worktrees.report_worktrees(repo).as_dict()
    [entry] = report["worktrees"]

    assert report["board"] is None
    assert report["integration"] == _git("rev-parse", "HEAD", cwd=repo)
    assert entry["protected_by"] == []
    assert entry["branch"] == "clean"
    assert entry["head"] == report["integration"]
    assert entry["ignored"] == [".lake/"]
    assert entry["logical_bytes"] >= 250_000
    # The hardlink into the cache counts as logical bytes but frees nothing.
    assert 50_000 <= entry["freeable_bytes"] < 200_000
    assert entry["recover"] == f"git worktree add {clean} clean"
    assert (entry["removed"], entry["freed_bytes"]) == (False, None)
    assert worktrees.format_worktrees(worktrees.report_worktrees(repo))[-1] == (
        "claims: not checked, since there is no origin remote and no --repo"
    )


def test_main_worktree_and_the_targets_own_are_never_candidates(repo: Path) -> None:
    lead = _add_worktree(repo, "lead")
    _add_worktree(repo, "worker")

    assert set(_entries(lead)) == {"worker"}
    assert set(_entries(repo)) == {"lead", "worker"}


def test_a_worktree_touched_within_a_lease_lifetime_is_recent(repo: Path) -> None:
    fresh = _add_worktree(repo, "fresh")
    _age(repo)
    (fresh / ".lake").mkdir()

    assert _codes(_entries(repo)["fresh"]) == {"recent": "its .lake changed 0 min ago"}


def test_dirty_worktree_and_evidence_written_into_it_are_protected(repo: Path) -> None:
    edited = _add_worktree(repo, "edited")
    (edited / "Main.lean").write_text("theorem t : 1 = 1 := rfl\n", encoding="utf-8")
    evidence = _add_worktree(repo, "evidence")
    (evidence / "skeleton.json").write_text("{}", encoding="utf-8")
    _age(repo)

    entries = _entries(repo)

    assert _codes(entries["edited"]) == {"dirty": "1 changed, 0 untracked: Main.lean"}
    assert _codes(entries["evidence"]) == {"dirty": "0 changed, 1 untracked: skeleton.json"}


def test_unintegrated_commits_protect_unless_an_equivalent_patch_landed(repo: Path) -> None:
    ahead = _add_worktree(repo, "ahead")
    _commit(ahead, "Ahead.lean")
    picked = _add_worktree(repo, "picked")
    _git("cherry-pick", _commit(picked, "Picked.lean"), cwd=repo)
    detached = _add_worktree(repo, "detached", "--detach")
    _commit(detached, "Detached.lean")
    _age(repo)

    entries = _entries(repo)

    integration = _git("rev-parse", "HEAD", cwd=repo)[:12]
    assert _codes(entries["ahead"]) == {"unintegrated": f"1 commit(s) the integration HEAD {integration} lacks"}
    assert entries["picked"]["protected_by"] == []
    assert _codes(entries["detached"]) == {"unintegrated": "its HEAD reflog alone reaches 1 commit(s)"}
    assert entries["detached"]["branch"] is None
    assert entries["detached"]["recover"].startswith("git worktree add --detach ")


def test_a_merge_the_integration_head_cannot_reach_stays_unintegrated(repo: Path) -> None:
    merged = _add_worktree(repo, "merged")
    side = _add_worktree(repo, "side")
    patch = _commit(side, "Side.lean")
    _git("merge", "--quiet", "--no-ff", "side", "-m", "merge side", cwd=merged)
    _git("cherry-pick", patch, cwd=repo)
    _age(repo)

    entries = _entries(repo)

    integration = _git("rev-parse", "HEAD", cwd=repo)[:12]
    assert entries["side"]["protected_by"] == []
    assert _codes(entries["merged"]) == {"unintegrated": f"1 commit(s) the integration HEAD {integration} lacks"}


def test_a_detached_worktree_whose_reflog_alone_reaches_a_commit_is_protected(repo: Path) -> None:
    detached = _add_worktree(repo, "detached", "--detach")
    _commit(detached, "Abandoned.lean")
    _git("checkout", "--quiet", "--detach", "main", cwd=detached)
    _age(repo)

    assert _codes(_entries(repo)["detached"]) == {"unintegrated": "its HEAD reflog alone reaches 1 commit(s)"}

    _git("branch", "keep", "HEAD@{1}", cwd=detached)
    assert _entries(repo)["detached"]["protected_by"] == []


def test_locked_worktrees_are_protected_and_a_missing_one_is_not(repo: Path) -> None:
    locked = _add_worktree(repo, "locked")
    _git("worktree", "lock", "--reason", "on a removable disk", str(locked), cwd=repo)
    missing = _add_worktree(repo, "missing")
    for name in (".git", "Main.lean", ".gitignore"):
        (missing / name).unlink()
    missing.rmdir()
    _age(repo)

    entries = _entries(repo)

    assert _codes(entries["locked"]) == {"locked": "on a removable disk"}
    assert entries["missing"]["protected_by"] == []
    assert (entries["missing"]["logical_bytes"], entries["missing"]["freeable_bytes"]) == (0, 0)


def test_a_git_lock_file_protects_however_old_as_does_an_unfinished_rebase(repo: Path) -> None:
    stale = _add_worktree(repo, "stale")
    rebasing = _add_worktree(repo, "rebasing")
    _age(repo)
    lock = _admin(stale) / "index.lock"
    lock.write_text("", encoding="utf-8")
    os.utime(lock, (0, 0))
    (_admin(rebasing) / "rebase-merge").mkdir()

    entries = _entries(repo)

    assert _codes(entries["stale"]) == {"git-operation": "index.lock"}
    assert _codes(entries["rebasing"]) == {"git-operation": "rebase-merge"}


def test_a_worktree_containing_another_is_protected(repo: Path) -> None:
    outer = _add_worktree(repo, "outer")
    inner = outer / ".lake" / "inner"
    _git("worktree", "add", "--quiet", "-b", "inner", str(inner), cwd=repo)
    _age(repo)

    entries = _entries(repo)

    assert _codes(entries["outer"]) == {"contains-worktree": str(inner)}
    assert entries["inner"]["protected_by"] == []


def test_a_running_process_in_a_worktree_marks_it_in_use(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worktrees.psutil, "process_iter", _REAL_PROCESS_ITER)
    busy = _add_worktree(repo, "busy")
    child = subprocess.Popen(["sleep", "60"], cwd=busy)
    try:
        evidence = _codes(_entries(repo)["busy"])
    finally:
        child.kill()
        child.wait()

    assert f"pid {child.pid} (sleep)" in evidence["in-use"]
    assert "working directory" in evidence["in-use"]


def test_an_uninspectable_process_protects_only_worktrees_created_before_it_started(
    repo: Path, processes: list[object]
) -> None:
    _add_worktree(repo, "worker")
    _age(repo)
    processes.append(_Process(41, "/elsewhere", euid=os.geteuid() + 1, error=psutil.AccessDenied(41)))
    # A daemon such as ssh-agent, started before the worktree existed, cannot have started in it.
    processes.append(_Process(42, "/elsewhere", started=1_000.0, error=psutil.AccessDenied(42)))
    assert _entries(repo)["worker"]["protected_by"] == []

    started = time.time() + 60
    processes.append(_Process(43, "/elsewhere", started=started, error=psutil.AccessDenied(43)))
    evidence = _codes(_entries(repo)["worker"])

    detail = f"could not inspect pid 43 (fake-43) started {worktrees._timestamp(started)}"
    assert evidence == {"liveness-unknown": detail}


def test_a_reused_pid_is_a_new_process_and_refuses_removal(
    repo: Path, tmp_path: Path, processes: list[object]
) -> None:
    worker = _add_worktree(repo, "worker")
    _age(repo)
    processes.append(_Process(77, tmp_path / "elsewhere", started=1_000.0))
    manifest = _manifest(repo, tmp_path)
    # The PID now belongs to a later process working in the worktree.
    processes[:] = [_Process(77, worker, started=2_000.0)]

    result = worktrees.remove_worktrees(repo, manifest)

    [refused] = result.refused
    assert _codes(refused.as_dict()) == {"in-use": "pid 77 (fake-77) started 1970-01-01T00:33:20Z: working directory"}
    assert worker.is_dir()


def test_a_live_lease_from_this_host_protects_every_worktree(repo: Path, board: Path) -> None:
    _add_worktree(repo, "first")
    _add_worktree(repo, "second")
    _age(repo)
    _plant_lease(board, "af_live", note="feat/first")
    _plant_lease(board, "af_expired", expires_at=time.time() - 1)
    _plant_lease(board, "af_elsewhere", host="another-host")

    report = worktrees.report_worktrees(repo).as_dict()

    assert report["board"] == str(board.resolve())
    for entry in report["worktrees"]:
        evidence = _codes(entry)
        assert list(evidence) == ["claimed"]
        assert evidence["claimed"].startswith("af_live held by worker-1 until ")
        assert evidence["claimed"].endswith(" (note: feat/first)")


def test_repo_names_the_board_when_there_is_no_origin(repo: Path, tmp_path: Path) -> None:
    _add_worktree(repo, "worker")
    _age(repo)
    board = tmp_path / "elsewhere.git"
    _git("init", "--bare", "--quiet", str(board), cwd=tmp_path)
    _plant_lease(board, "af_live")

    assert list(_codes(_entries(repo, repo=str(board))["worker"])) == ["claimed"]


def test_an_unreadable_board_or_lease_leaves_liveness_unknown(repo: Path, board: Path) -> None:
    _add_worktree(repo, "worker")
    _age(repo)
    _plant_lease(board, "af_hostless", host=None)
    assert _codes(_entries(repo)["worker"]) == {
        "liveness-unknown": "claim(s) naming no host or unreadable: af_hostless"
    }

    _git("remote", "set-url", "origin", str(repo.parent / "no-such-board.git"), cwd=repo)
    evidence = _codes(_entries(repo)["worker"])

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
    _age(repo)
    manifest = _manifest(repo, tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["worktrees"] = [entry for entry in data["worktrees"] if Path(entry["path"]).name != "second"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    later = _add_worktree(repo, "later")  # removable soon, but not in the manifest

    result = worktrees.remove_worktrees(repo, manifest)

    entries = {Path(entry["path"]).name: entry for entry in result.as_dict()["worktrees"]}
    assert result.as_dict()["schema"] == worktrees.WORKTREES_SCHEMA
    assert result.refused == ()
    assert entries["first"]["removed"] is True
    assert isinstance(entries["first"]["freed_bytes"], int)
    assert entries["first"]["recover"] == f"git worktree add {first} first"
    assert [name for name, entry in entries.items() if entry["removed"]] == ["first"]
    assert set(entries) == {"first", "second", "ahead", "later"}
    assert not first.exists()
    assert second.is_dir() and ahead.is_dir() and later.is_dir()
    assert _git("branch", "--list", "first", cwd=repo) != ""


def test_remove_refuses_a_worktree_that_changed_since_the_report(repo: Path, tmp_path: Path) -> None:
    moved = _add_worktree(repo, "moved")
    dirtied = _add_worktree(repo, "dirtied")
    gone = _add_worktree(repo, "gone")
    _age(repo)
    manifest = _manifest(repo, tmp_path)
    _git("checkout", "--quiet", "--detach", cwd=moved)
    (dirtied / "notes.md").write_text("new evidence", encoding="utf-8")
    _git("worktree", "remove", str(gone), cwd=repo)
    _age(repo)

    result = worktrees.remove_worktrees(repo, manifest)

    refused = {entry.path.name: _codes(entry.as_dict()) for entry in result.refused}
    head = _git("rev-parse", "HEAD", cwd=repo)[:12]
    assert refused == {
        "moved": {"changed": f"now detached at {head}, not moved at {head}"},
        "dirtied": {"dirty": "0 changed, 1 untracked: notes.md"},
        "gone": {"changed": "no longer a linked worktree of this repository"},
    }
    assert moved.is_dir() and dirtied.is_dir()


def test_remove_refuses_everything_once_the_integration_head_moves(repo: Path, tmp_path: Path) -> None:
    worker = _add_worktree(repo, "worker")
    _age(repo)
    manifest = _manifest(repo, tmp_path)
    before = _git("rev-parse", "HEAD", cwd=repo)
    after = _commit(repo, "Later.lean")

    [refused] = worktrees.remove_worktrees(repo, manifest).refused

    assert _codes(refused.as_dict()) == {
        "changed": f"the integration HEAD is now {after[:12]}, not {before[:12]}"
    }
    assert worker.is_dir()


def test_remove_refuses_the_main_worktree_and_the_targets_own(repo: Path, tmp_path: Path) -> None:
    lead = _add_worktree(repo, "lead")
    _age(repo)
    manifest = _manifest(lead, tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    head = _git("rev-parse", "HEAD", cwd=repo)
    data["worktrees"] = [
        {"path": str(path), "head": head, "branch": branch, "protected_by": []}
        for path, branch in ((repo, "main"), (lead, "lead"))
    ]
    manifest.write_text(json.dumps(data), encoding="utf-8")

    refused = {entry.path.name: _codes(entry.as_dict()) for entry in worktrees.remove_worktrees(lead, manifest).refused}

    assert refused == {"main": {"changed": "the main worktree"}, "lead": {"changed": "the worktree the target is in"}}
    assert lead.is_dir()


def test_remove_refuses_after_a_lease_is_renewed(repo: Path, board: Path, tmp_path: Path) -> None:
    worker = _add_worktree(repo, "worker")
    _age(repo)
    _plant_lease(board, "af_worker", expires_at=time.time() - 1, note="worker")
    manifest = _manifest(repo, tmp_path)
    _plant_lease(board, "af_worker", note="worker")

    [refused] = worktrees.remove_worktrees(repo, manifest).refused

    assert list(_codes(refused.as_dict())) == ["claimed"]
    assert worker.is_dir()


def test_remove_rejects_a_manifest_for_another_repository_or_a_malformed_one(repo: Path, tmp_path: Path) -> None:
    _add_worktree(repo, "worker")
    _age(repo)
    manifest = _manifest(repo, tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["repository"] = str(tmp_path / "other" / ".git")
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(worktrees.WorktreeError, match="describes the repository"):
        worktrees.remove_worktrees(repo, manifest)

    data["repository"] = str(Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo)))
    data["worktrees"][0]["protected_by"] = "none"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(worktrees.WorktreeError, match="malformed worktree entry"):
        worktrees.remove_worktrees(repo, manifest)

    manifest.write_text('{"schema": "autoform-project-worktrees/v1", "schema": "x"}', encoding="utf-8")
    with pytest.raises(worktrees.WorktreeError, match="cannot read the manifest"):
        worktrees.remove_worktrees(repo, manifest)
    assert (repo.parent / "worker").is_dir()


def test_a_target_outside_a_checkout_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(worktrees.WorktreeError, match="not in a Git checkout"):
        worktrees.report_worktrees(tmp_path)


def test_cli_reports_removes_and_sets_the_exit_status(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    worker = _add_worktree(repo, "worker")
    _age(repo)

    assert main(["project", "worktrees", str(repo), "--json"]) == 0
    report = capsys.readouterr().out
    assert json.loads(report)["worktrees"][0]["protected_by"] == []
    manifest = tmp_path / "manifest.json"
    manifest.write_text(report, encoding="utf-8")

    assert main(["project", "worktrees", str(repo)]) == 0
    assert capsys.readouterr().out.startswith(f"removable {worker} (worker at ")

    (worker / "late.md").write_text("late", encoding="utf-8")
    assert main(["project", "worktrees", str(repo), "--remove", str(manifest)]) == 1
    assert capsys.readouterr().out.startswith(f"refused {worker} (worker at ")

    (worker / "late.md").unlink()
    _age(repo)
    assert main(["project", "worktrees", str(repo), "--remove", str(manifest), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["worktrees"][0]["removed"] is True
    assert not worker.exists()

    assert main(["project", "worktrees", str(tmp_path / "nowhere")]) == 1
    assert "error:" in capsys.readouterr().err


def test_only_the_worktrees_module_removes_a_worktree(repo_root: Path) -> None:
    """No command cleans up implicitly: `git worktree remove` appears in one module only."""
    users = sorted(
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / "autoform_cli").rglob("*.py")
        if '"worktree", "remove"' in path.read_text(encoding="utf-8")
    )
    assert users == ["autoform_cli/worktrees.py"]
