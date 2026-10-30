"""Tests for host-neutral Git-ref claim leases."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Sequence
from pathlib import Path

import pytest

from autoform_cli import claims
from autoform_cli.__main__ import main


def _git(*args: str, cwd: Path | None = None, input_text: str | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    return proc.stdout.strip()


@pytest.fixture
def board_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "claims.git"
    _git("init", "--bare", "--quiet", str(repo))
    return repo


def _board(tmp_path: Path, repo: Path, owner: str) -> claims.ClaimBoard:
    return claims.ClaimBoard(repo, owner, tmp_path / f"scratch-{owner}")


def test_claim_board_recognizes_scp_remote_without_explicit_user(tmp_path: Path) -> None:
    board = claims.ClaimBoard(
        "github-work:org/repo.git",
        "worker",
        tmp_path / "scratch",
    )

    assert board.repo_url == "github-work:org/repo.git"


@pytest.mark.parametrize(
    "remote",
    [
        "git@example.com:org/repo.git",
        "github-work:org/repo.git",
        "[2001:db8::1]:org/repo.git",
        "g:repo.git",
    ],
)
def test_repository_remote_grammar_matches_scp_forms(remote: str) -> None:
    assert claims._repository_is_remote(remote, windows=False)


@pytest.mark.parametrize(
    "path",
    [
        r"C:\projects\claims.git",
        "C:/projects/claims.git",
        r"\\?\C:\projects\claims.git",
        r"\\.\C:\projects\claims.git",
        r"\\server\share\claims.git",
    ],
)
def test_repository_remote_grammar_rejects_rooted_windows_paths(path: str) -> None:
    assert not claims._repository_is_remote(path, windows=False)


def test_drive_relative_path_is_local_only_on_windows() -> None:
    assert claims._repository_is_remote("C:claims.git", windows=False)
    assert not claims._repository_is_remote("C:claims.git", windows=True)


def test_claim_board_does_not_treat_windows_drive_as_scp_remote(tmp_path: Path) -> None:
    raw = r"C:\projects\claims.git"
    board = claims.ClaimBoard(
        raw,
        "worker",
        tmp_path / "scratch",
    )

    assert board.repo_url == str(Path(raw).expanduser().resolve())
    assert Path(board.repo_url).is_absolute()


def test_non_directory_scratch_is_a_clean_transport_error(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.write_text("not a directory", encoding="utf-8")
    board = claims.ClaimBoard(board_repo, "worker", scratch)

    with pytest.raises(claims.ClaimTransportError, match="prepare.*scratch"):
        board.read("author/node")


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user and mode-bit permissions",
)
def test_inaccessible_scratch_parent_is_a_clean_transport_error(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    board = claims.ClaimBoard(board_repo, "worker", locked / "scratch")
    locked.chmod(0)
    try:
        with pytest.raises(claims.ClaimTransportError, match="prepare.*scratch"):
            board.read("author/node")
    finally:
        locked.chmod(0o755)


def test_concurrent_first_use_initializes_one_shared_scratch(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "scratch"
    workers = 12
    start = threading.Barrier(workers)
    failures: list[BaseException] = []

    def read_missing_claim() -> None:
        try:
            start.wait(timeout=5)
            board = claims.ClaimBoard(board_repo, "worker", scratch)
            assert board.read("author/missing") is None
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=read_missing_claim) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert _git("rev-parse", "--is-bare-repository", cwd=scratch) == "true"


def _plant_message(repo: Path, key: str, message: str) -> str:
    tree = _git("mktree", cwd=repo, input_text="")
    commit = _git("commit-tree", tree, "-m", message, cwd=repo)
    _git("update-ref", claims.CLAIM_REF_PREFIX + key, commit, cwd=repo)
    return commit


def _plant_lease(repo: Path, key: str, **changes: object) -> str:
    lease: dict[str, object] = {
        "schema": claims.CLAIM_SCHEMA,
        "owner": "original-owner",
        "host": "test-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": key,
    }
    lease.update(changes)
    return _plant_message(repo, key, json.dumps(lease))


def _holds(board: claims.ClaimBoard, key: str) -> bool:
    """Whether *board*'s worker owns the live lease at *key*."""
    lease = board.read(key)
    return lease is not None and lease["owner"] == board.worker_id and not board.expired(lease)


def test_acquire_read_list_and_release_round_trip(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.acquire("author/node", ttl=600, note="proof")
    lease = board.read("author/node")
    assert lease is not None
    assert lease["schema"] == claims.CLAIM_SCHEMA
    assert lease["owner"] == "worker-a"
    assert lease["resource"] == "author/node"
    assert lease["note"] == "proof"
    assert _holds(board, "author/node")

    listed = board.list()
    assert [(item["_key"], item["_expired"]) for item in listed] == [("author/node", False)]
    assert board.release("author/node")
    assert board.read("author/node") is None
    assert board.release("author/node")


def test_cas_acquire_race_has_exactly_one_winner(tmp_path: Path, board_repo: Path) -> None:
    boards = [_board(tmp_path, board_repo, owner) for owner in ("worker-a", "worker-b")]
    barrier = threading.Barrier(2)
    original_remote_oid = claims.ClaimBoard._remote_oid

    def synchronized_remote_oid(self: claims.ClaimBoard, key: str) -> str | None:
        oid = original_remote_oid(self, key)
        barrier.wait(timeout=5)
        return oid

    for board in boards:
        board._remote_oid = synchronized_remote_oid.__get__(board, claims.ClaimBoard)  # type: ignore[method-assign]

    results: list[bool] = []
    errors: list[BaseException] = []

    def acquire(board: claims.ClaimBoard) -> None:
        try:
            results.append(board.acquire("race", ttl=600))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=acquire, args=(board,)) for board in boards]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]
    for board in boards:
        board._remote_oid = original_remote_oid.__get__(board, claims.ClaimBoard)  # type: ignore[method-assign]
    assert boards[0].read("race")["owner"] in {"worker-a", "worker-b"}


def test_expired_lease_can_be_taken_over(tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr(claims.time, "time", lambda: now)
    first = _board(tmp_path, board_repo, "worker-a")
    second = _board(tmp_path, board_repo, "worker-b")

    assert first.acquire("expired", ttl=10)
    monkeypatch.setattr(claims.time, "time", lambda: now + 11)
    assert not _holds(first, "expired")
    assert second.acquire("expired", ttl=60)
    assert second.read("expired")["owner"] == "worker-b"


def test_malformed_lease_is_unverifiable_and_not_takeover_eligible(tmp_path: Path, board_repo: Path) -> None:
    _plant_message(board_repo, "malformed", "not json")
    board = _board(tmp_path, board_repo, "worker-a")

    for operation in (
        lambda: board.read("malformed"),
        lambda: board.renew("malformed"),
        lambda: board.release("malformed"),
        lambda: board.acquire("malformed", ttl=600),
    ):
        with pytest.raises(claims.MalformedLeaseError):
            operation()

    listed = board.list()
    assert listed[0]["schema"] == "unreadable"
    assert listed[0]["_malformed"] is True
    assert listed[0]["_expired"] is False
    assert board.cleanup() == 0


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), float("-inf")])
def test_acquire_rejects_nonfinite_ttl_without_mutating_remote(
    tmp_path: Path, board_repo: Path, ttl: float
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(ValueError, match="finite positive number"):
        board.acquire("nonfinite", ttl=ttl)

    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX + "nonfinite", cwd=board_repo) == ""


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), float("-inf")])
def test_renew_rejects_nonfinite_ttl_without_replacing_lease(
    tmp_path: Path, board_repo: Path, ttl: float
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("owned", ttl=30)
    oid = board._remote_oid("owned")

    with pytest.raises(ValueError, match="finite positive number"):
        board.renew("owned", ttl=ttl)

    assert board._remote_oid("owned") == oid


def test_owner_only_renew_and_release(tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 2_000.0
    monkeypatch.setattr(claims.time, "time", lambda: now)
    owner = _board(tmp_path, board_repo, "owner")
    peer = _board(tmp_path, board_repo, "peer")
    assert owner.acquire("owned", ttl=30)
    first_expiry = owner.read("owned")["expires_at"]

    assert not peer.renew("owned")
    assert not peer.release("owned")
    assert not peer.acquire("owned", ttl=30)
    monkeypatch.setattr(claims.time, "time", lambda: now + 5)
    assert owner.renew("owned", ttl=30)
    assert owner.read("owned")["expires_at"] > first_expiry
    assert owner.release("owned")


def test_single_key_operations_read_only_the_exact_claim_ref(tmp_path: Path, board_repo: Path) -> None:
    # ls-remote for refs/autoform-claims/k also lists the peer's
    # refs/autoform-claims/a/refs/autoform-claims/k, which ends in the same path.
    peer = _board(tmp_path, board_repo, "peer")
    assert peer.acquire("a/refs/autoform-claims/k", ttl=600)
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.read("k") is None
    assert board.acquire("k", ttl=600)
    assert _holds(board, "k")
    assert board.renew("k", ttl=600)
    assert board.release("k")
    assert _holds(peer, "a/refs/autoform-claims/k")


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_expired_rejects_nonfinite_explicit_comparison_clock(now: float) -> None:
    lease = {"expires_at": 200.0}

    with pytest.raises(ValueError, match="comparison clock must be finite"):
        claims.ClaimBoard.expired(lease, now=now)


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_expired_rejects_nonfinite_default_comparison_clock(
    monkeypatch: pytest.MonkeyPatch, now: float
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: now)

    with pytest.raises(ValueError, match="comparison clock must be finite"):
        claims.ClaimBoard.expired({"expires_at": 200.0})


def test_cleanup_removes_only_expired_snapshot_entries(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("dead", ttl=5)
    assert board.acquire("live", ttl=500)
    monkeypatch.setattr(claims.time, "time", lambda: 1_010.0)

    assert board.cleanup() == 1
    assert [lease["_key"] for lease in board.list()] == ["live"]


def test_cleanup_cas_does_not_delete_renewed_lease(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    cleaner = _board(tmp_path, board_repo, "worker-a")
    assert cleaner.acquire("lease", ttl=5)
    monkeypatch.setattr(claims.time, "time", lambda: 1_010.0)

    original_list = cleaner.list
    owner = _board(tmp_path, board_repo, "worker-a")

    def list_then_renew() -> list[dict[str, object]]:
        snapshot = original_list()
        assert owner.renew("lease", ttl=500)
        return snapshot

    monkeypatch.setattr(cleaner, "list", list_then_renew)
    assert cleaner.cleanup() == 0
    assert cleaner.read("lease")["expires_at"] == 1_510.0


def test_author_claim_keys_are_ref_safe_and_resist_slug_collisions() -> None:
    node_ids = ["a b", "a-b", "A/B", "A B", "Évariste Galois", "!!!", "x" * 200]
    keys = [claims.author_claim_key(node_id) for node_id in node_ids]

    assert len(keys) == len(set(keys))
    assert all(key.startswith("author/") for key in keys)
    assert all(claims.CLAIM_KEY_RE.fullmatch(key) for key in keys)
    assert all(".." not in key for key in keys)


@pytest.mark.parametrize(
    "key",
    ["has space", "a/../b", "/leading", "trailing/", "refs/heads/x@{1}", ".hidden", "ends.", "lease.lock"],
)
def test_invalid_keys_are_rejected(tmp_path: Path, board_repo: Path, key: str) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    with pytest.raises(ValueError, match="invalid claim key"):
        board.acquire(key)


def test_relative_local_repo_path_is_resolved_before_entering_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "claims.git"
    _git("init", "--bare", "--quiet", str(repo))
    monkeypatch.chdir(tmp_path)
    board = claims.ClaimBoard("claims.git", "worker-a", tmp_path / "scratch")

    assert board.acquire("relative", ttl=600)
    assert board.read("relative")["owner"] == "worker-a"


def test_transport_failure_raises_without_local_fallback(tmp_path: Path) -> None:
    board = claims.ClaimBoard(tmp_path / "missing" / "claims.git", "worker-a", tmp_path / "scratch")

    with pytest.raises(claims.ClaimTransportError):
        board.acquire("key")
    assert not (board.scratch / claims.CLAIM_REF_PREFIX / "key").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"schema": "other"},
        {"resource": "different"},
        {"owner": ""},
        {"expires_at": "later"},
        {"acquired_at": float("nan")},
        {"acquired_at": float("inf")},
        {"acquired_at": float("-inf")},
        {"expires_at": float("nan")},
        {"expires_at": float("inf")},
        {"expires_at": float("-inf")},
    ],
)
def test_schema_resource_or_required_field_mismatch_is_malformed(
    tmp_path: Path,
    board_repo: Path,
    changes: dict[str, object],
) -> None:
    _plant_lease(board_repo, "wrong", **changes)
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.MalformedLeaseError):
        board.read("wrong")
    with pytest.raises(claims.MalformedLeaseError):
        board.renew("wrong")
    with pytest.raises(claims.MalformedLeaseError):
        board.release("wrong")
    with pytest.raises(claims.MalformedLeaseError):
        board.acquire("wrong", ttl=600)
    assert board.list()[0]["_malformed"] is True
    assert board.cleanup() == 0


@pytest.mark.parametrize("field", ["schema", "owner", "resource", "acquired_at", "expires_at"])
def test_planted_lease_with_duplicate_decision_field_is_rejected_by_strict_json_parser(
    tmp_path: Path, board_repo: Path, field: str
) -> None:
    values = {
        "schema": '"autoform-claim/v1"',
        "owner": '"worker-a"',
        "resource": '"duplicate"',
        "acquired_at": "100.0",
        "expires_at": "200.0",
    }
    pairs = [f'"{name}":{value}' for name, value in values.items()]
    pairs.append(f'"{field}":{values[field]}')
    _plant_message(board_repo, "duplicate", "{" + ",".join(pairs) + "}")
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.MalformedLeaseError, match="invalid lease JSON"):
        board.read("duplicate")

    assert board.list()[0]["_malformed"] is True


def test_planted_nonfinite_lease_is_rejected_by_strict_json_parser(
    tmp_path: Path, board_repo: Path
) -> None:
    message = (
        '{"schema":"autoform-claim/v1","owner":"worker-a","resource":"strict-json",'
        '"acquired_at":0,"expires_at":NaN}'
    )
    _plant_message(board_repo, "strict-json", message)
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.MalformedLeaseError, match="invalid lease JSON"):
        board.read("strict-json")


def test_acquire_rejects_nonfinite_clock_before_commit_or_push(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    monkeypatch.setattr(claims.time, "time", lambda: float("nan"))

    with pytest.raises(ValueError, match="claim timestamp must be finite"):
        board.acquire("bad-clock", ttl=30)

    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX + "bad-clock", cwd=board_repo) == ""


def test_acquire_rejects_nonfinite_expiry_before_commit_or_push(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    monkeypatch.setattr(claims.time, "time", lambda: 1e308)

    with pytest.raises(ValueError, match="claim expiry must be finite"):
        board.acquire("bad-expiry", ttl=1e308)

    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX + "bad-expiry", cwd=board_repo) == ""


def _refs(repo: Path) -> dict[str, str]:
    listing = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo)
    return dict(line.split(" ", 1) for line in listing.splitlines())


@pytest.mark.parametrize(("method", "key"), [("renew", "a"), ("release", "a"), ("acquire", "c")])
def test_list_sees_a_claim_change_made_just_before_its_fetch(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, method: str, key: str
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b"], ttl=600)
    before = _refs(board_repo)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    changed: list[bool] = []

    def change_before_the_fetch(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch" and not changed:
            changed.append(getattr(owner, method)(key))
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", change_before_the_fetch)
    listed = reader.list()

    assert changed == [True]
    assert _refs(board_repo) != before
    assert {claims.CLAIM_REF_PREFIX + lease["_key"]: lease["_oid"] for lease in listed} == _refs(board_repo)
    assert not any(lease["_malformed"] for lease in listed)


def test_list_returns_its_verified_snapshot_when_refs_change_while_leases_are_parsed(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b"], ttl=600)
    before = _refs(board_repo)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git

    def renew_while_parsing(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "cat-file":
            assert owner.renew("b")
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", renew_while_parsing)
    listed = reader.list()

    assert _refs(board_repo) != before
    assert {claims.CLAIM_REF_PREFIX + lease["_key"]: lease["_oid"] for lease in listed} == before
    assert not any(lease["_malformed"] for lease in listed)


def test_list_lists_every_claim_while_a_peer_keeps_renewing_one(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b", "c"], ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git

    def renew_after_each_git_command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        proc = git(args, **kwargs)
        assert owner.renew("a")
        return proc

    monkeypatch.setattr(reader, "_git", renew_after_each_git_command)
    listed = reader.list()

    assert [lease["_key"] for lease in listed] == ["a", "b", "c"]
    assert not any(lease["_malformed"] for lease in listed)


def _list_git_commands(tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, count: int) -> int:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many([f"k{index}" for index in range(count)], ttl=600)
    reader = _board(tmp_path, board_repo, f"reader-{count}")
    git = reader._git
    commands: list[list[str]] = []

    def counting_git(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", counting_git)
    assert len(reader.list()) == count
    return len(commands)


def test_list_runs_as_many_git_commands_for_many_claims_as_for_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small, large = tmp_path / "small.git", tmp_path / "large.git"
    for repo in (small, large):
        _git("init", "--bare", "--quiet", str(repo))

    assert _list_git_commands(tmp_path, small, monkeypatch, 1) == _list_git_commands(
        tmp_path, large, monkeypatch, 30
    )


def test_list_leaves_no_refs_behind_for_a_claim_ref_with_an_invalid_key(tmp_path: Path, board_repo: Path) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    oid = _refs(board_repo)[claims.CLAIM_REF_PREFIX + "a"]
    _git("update-ref", claims.CLAIM_REF_PREFIX + "not+a+key", oid, cwd=board_repo)
    reader = _board(tmp_path, board_repo, "worker-b")

    assert [lease["_key"] for lease in reader.list()] == ["a"]
    assert reader._git(["for-each-ref", "refs/"]).stdout == ""


def test_list_leaves_no_refs_behind_when_reading_them_fails(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    failed: list[bool] = []

    def first_listing_fails(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "for-each-ref" and not failed:
            failed.append(True)
            raise claims.ClaimTransportError("simulated for-each-ref failure")
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", first_listing_fails)
    with pytest.raises(claims.ClaimTransportError, match="simulated for-each-ref failure"):
        reader.list()

    assert git(["for-each-ref", "refs/"]).stdout == ""


def test_list_ignores_claim_refs_another_process_writes_into_the_shared_scratch(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b"], ttl=600)
    stale = _refs(board_repo)[claims.CLAIM_REF_PREFIX + "b"]
    reader = _board(tmp_path, board_repo, "worker-b")
    assert [lease["_key"] for lease in reader.list()] == ["a", "b"]
    assert owner.release("b")
    git = reader._git

    def peer_writes_after_fetch(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        proc = git(args, **kwargs)
        if args[0] == "fetch":
            # Another process on this scratch, such as `read`, writes the released lease's ref meanwhile.
            git(["update-ref", claims.CLAIM_REF_PREFIX + "b", stale])
        return proc

    monkeypatch.setattr(reader, "_git", peer_writes_after_fetch)

    assert [lease["_key"] for lease in reader.list()] == ["a"]


def test_list_retries_a_fetch_the_board_refused_because_a_ref_moved(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    fetches: list[list[str]] = []

    def refused_once(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch":
            fetches.append(args)
            if len(fetches) == 1:
                # A stateless protocol v0 server refuses a want its refs no longer reach.
                return subprocess.CompletedProcess(
                    ["git", *args], 128, "", "fatal: remote error: upload-pack: not our ref 1234\n"
                )
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", refused_once)

    assert [lease["_key"] for lease in reader.list()] == ["a"]
    assert len(fetches) == 2


def test_list_offers_the_board_no_local_refs_to_negotiate_from(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many([f"k{index}" for index in range(20)], ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    reader._ensure_scratch()
    # The scratch an older `list` used keeps a copy of every claim ref. Under
    # protocol v0 offering them all takes rounds in which a renewal fails the fetch.
    reader._git(["fetch", "--quiet", str(board_repo), f"+{claims.CLAIM_REF_PREFIX}*:{claims.CLAIM_REF_PREFIX}*"])
    assert owner.renew_many([f"k{index}" for index in range(20)])
    monkeypatch.setenv("GIT_TRACE_PACKET", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.version")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "0")
    git = reader._git
    traces: list[str] = []

    def traced(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        proc = git(args, **kwargs)
        if args[0] == "fetch":
            traces.append(proc.stderr)
        return proc

    monkeypatch.setattr(reader, "_git", traced)

    assert len(reader.list()) == 20
    assert len(traces) == 1 and "packet:" in traces[0] and "> have " not in traces[0]


def test_read_offers_the_board_no_local_refs_to_negotiate_from(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many([f"k{index}" for index in range(20)], ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    reader._ensure_scratch()
    # A scratch keeps the claim refs an older `list` mirrored and the private
    # refs of a `list` killed before its cleanup ran.
    reader._git(["fetch", "--quiet", str(board_repo), f"+{claims.CLAIM_REF_PREFIX}*:{claims.CLAIM_REF_PREFIX}*"])
    reader._git(["fetch", "--quiet", str(board_repo), f"+{claims.CLAIM_REF_PREFIX}*:refs/autoform-list/killed/*"])
    assert owner.renew_many([f"k{index}" for index in range(20)])
    monkeypatch.setenv("GIT_TRACE_PACKET", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.version")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "0")
    git = reader._git
    traces: list[str] = []

    def traced(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        proc = git(args, **kwargs)
        if args[0] == "fetch":
            traces.append(proc.stderr)
        return proc

    monkeypatch.setattr(reader, "_git", traced)

    lease = reader.read("k0")
    assert lease is not None and lease["owner"] == "worker-a"
    assert len(traces) == 1 and "packet:" in traces[0] and "> have " not in traces[0]


def test_list_reads_a_lease_through_an_annotated_tag_as_read_does(tmp_path: Path, board_repo: Path) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    lease = _refs(board_repo)[claims.CLAIM_REF_PREFIX + "a"]
    tag = _git("mktag", cwd=board_repo, input_text=f"object {lease}\ntype commit\ntag t\ntagger t <t@t> 0 +0000\n\nt\n")
    _git("update-ref", claims.CLAIM_REF_PREFIX + "a", tag, cwd=board_repo)
    reader = _board(tmp_path, board_repo, "worker-b")

    assert reader.read("a") is not None
    [listed] = reader.list()
    assert listed["_malformed"] is False
    assert listed["_oid"] == tag


def test_list_drops_a_released_claim_it_listed_before(tmp_path: Path, board_repo: Path) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b"], ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    assert [lease["_key"] for lease in reader.list()] == ["a", "b"]

    assert owner.release("b")

    assert [lease["_key"] for lease in reader.list()] == ["a"]
    assert reader._git(["for-each-ref", "refs/"]).stdout == ""


def test_list_reports_a_claim_ref_to_a_non_commit_as_malformed(tmp_path: Path, board_repo: Path) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("live", ttl=600)
    blob = _git("hash-object", "-w", "--stdin", cwd=board_repo, input_text="not a commit")
    _git("update-ref", claims.CLAIM_REF_PREFIX + "blob", blob, cwd=board_repo)

    listed = {lease["_key"]: lease for lease in _board(tmp_path, board_repo, "worker-b").list()}

    assert listed["blob"]["_malformed"] is True
    assert listed["blob"]["_oid"] == blob
    assert listed["live"]["_malformed"] is False


def _plant_packed_leases(repo: Path, keys: Sequence[str]) -> dict[str, str]:
    """Plant leases under packed refs, which hold names that differ only in case on any filesystem."""
    oids = {}
    for key in keys:
        oids[key] = _plant_lease(repo, key)
        _git("update-ref", "-d", claims.CLAIM_REF_PREFIX + key, cwd=repo)
    (repo / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted \n"
        + "".join(f"{oid} {claims.CLAIM_REF_PREFIX}{key}\n" for key, oid in sorted(oids.items()))
    )
    return oids


@pytest.mark.parametrize("ignorecase", ["true", "false"])
def test_list_lists_claim_keys_that_differ_only_in_case_on_any_filesystem(
    tmp_path: Path, board_repo: Path, ignorecase: str
) -> None:
    # A case-sensitive board can hold both refs; a case-insensitive scratch cannot store them under their names.
    oids = _plant_packed_leases(board_repo, ["lean/Foo", "lean/foo"])
    reader = _board(tmp_path, board_repo, "worker-b")
    reader._ensure_scratch()
    reader._git(["config", "core.ignorecase", ignorecase])

    listed = reader.list()

    assert [(item["_key"], item["_oid"], item["_malformed"]) for item in listed] == [
        ("lean/Foo", oids["lean/Foo"], False),
        ("lean/foo", oids["lean/foo"], False),
    ]
    assert reader._git(["for-each-ref", "refs/"]).stdout == ""


def test_list_on_a_case_insensitive_scratch_lists_both_keys_when_git_keeps_one_ref_silently(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oids = _plant_packed_leases(board_repo, ["lean/Foo", "lean/foo"])
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git

    def keeps_one_ref_without_a_word(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        proc = git(args, **kwargs)
        if args[0] == "fetch" and "--stdin" not in args and proc.returncode == 0:
            # What Apple's Git 2.50 leaves on a case-insensitive filesystem: exit 0, one ref, the other's lease.
            namespace = args[-1].rpartition(":")[2].removesuffix("*")
            git(["update-ref", f"{namespace}lean/Foo", oids["lean/foo"]])
            if f"{namespace}lean/foo" in git(["for-each-ref", "--format=%(refname)", namespace]).stdout.split():
                git(["update-ref", "-d", f"{namespace}lean/foo"])
        return proc

    monkeypatch.setattr(reader, "_git", keeps_one_ref_without_a_word)
    monkeypatch.setattr(reader, "_scratch_ignores_case", lambda: True)

    assert [(item["_key"], item["_oid"], item["_malformed"]) for item in reader.list()] == [
        ("lean/Foo", oids["lean/Foo"], False),
        ("lean/foo", oids["lean/foo"], False),
    ]
    assert git(["for-each-ref", "refs/"]).stdout == ""


def test_scratch_ignores_case_as_its_filesystem_does(tmp_path: Path) -> None:
    board = _scratch_board(tmp_path)
    (tmp_path / "Probe").touch()

    assert board._scratch_ignores_case() is (tmp_path / "probe").exists()


@pytest.mark.parametrize(
    ("keys", "collide"),
    [
        (["lean/Foo", "lean/foo"], True),
        (["Lean/Foo", "lean/Bar"], True),
        (["lean/Foo", "lean/foo/bar"], True),
        (["lean/foo", "lean/bar", "other"], False),
        (["lean/foo"], False),
    ],
)
def test_keys_collide_ignoring_case_when_they_or_their_folders_differ_only_in_case(
    keys: list[str], collide: bool
) -> None:
    assert claims._collide_ignoring_case(keys) is collide


@pytest.mark.parametrize("ignores_case", [False, True])
def test_list_names_the_claims_after_its_fetch_only_on_a_case_insensitive_scratch(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, ignores_case: bool
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b"], ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    monkeypatch.setattr(reader, "_scratch_ignores_case", lambda: ignores_case)
    calls = _counting(reader, monkeypatch)

    assert [lease["_key"] for lease in reader.list()] == ["a", "b"]

    commands = [args[0] for args, _ in calls if args[0] in {"fetch", "ls-remote"}]
    # Without colliding keys the fetch's result stands: no second fetch by name.
    assert commands == (["fetch", "ls-remote"] if ignores_case else ["fetch"])


def test_list_that_cannot_name_the_claims_after_its_fetch_leaves_no_refs(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git

    def unreachable_after_the_fetch(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "ls-remote":
            raise claims.ClaimTransportError("git ls-remote failed against claim board: gone")
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", unreachable_after_the_fetch)
    monkeypatch.setattr(reader, "_scratch_ignores_case", lambda: True)

    with pytest.raises(claims.ClaimTransportError, match="gone"):
        reader.list()
    assert git(["for-each-ref", "refs/"]).stdout == ""


def test_list_of_keys_that_differ_only_in_case_drops_a_claim_released_after_naming_it(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oids = _plant_packed_leases(board_repo, ["lean/Foo", "lean/foo", "other"])
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    named: list[bool] = []

    def released_after_ls_remote(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch" and "--stdin" not in args:
            # What a case-insensitive scratch answers; a case-sensitive one would store both names.
            stderr = "error: the remote has references that only differ in casing\n"
            return subprocess.CompletedProcess(["git", *args], 1, "", stderr)
        proc = git(args, **kwargs)
        if args[0] == "ls-remote" and not named:
            named.append(True)
            _git("update-ref", "-d", claims.CLAIM_REF_PREFIX + "other", cwd=board_repo)
        return proc

    monkeypatch.setattr(reader, "_git", released_after_ls_remote)

    assert [(item["_key"], item["_oid"]) for item in reader.list()] == [
        ("lean/Foo", oids["lean/Foo"]),
        ("lean/foo", oids["lean/foo"]),
    ]
    assert git(["for-each-ref", "refs/"]).stdout == ""


def test_list_reports_why_a_fetch_failed_without_git_s_negotiation_warning(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    warning = "warning: ignoring --negotiation-tip=refs/autoform-list/x/* because it does not match any refs\n"
    reason = "fatal: unable to access the board: " + "x" * 250

    def failing_fetch(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch":
            return subprocess.CompletedProcess(["git", *args], 128, "", warning + reason + "\n")
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", failing_fetch)
    with pytest.raises(claims.ClaimTransportError) as failed:
        reader.list()
    assert str(failed.value).startswith("cannot fetch the claim board: fatal: unable to access the board: ")
    assert "negotiation-tip" not in str(failed.value)


def test_list_reports_a_failed_fetch_without_retrying(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    fetches: list[list[str]] = []

    def failing_fetch(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch":
            fetches.append(args)
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: simulated fetch failure\n")
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", failing_fetch)
    with pytest.raises(
        claims.ClaimTransportError, match=r"cannot fetch the claim board.*simulated fetch failure"
    ) as failed:
        reader.list()
    assert not isinstance(failed.value, claims.ClaimContentionError)
    assert len(fetches) == 1


def test_acquire_many_is_all_or_nothing_when_a_peer_holds_one_key(tmp_path: Path, board_repo: Path) -> None:
    peer = _board(tmp_path, board_repo, "worker-b")
    assert peer.acquire("held", ttl=600)
    before = _refs(board_repo)
    board = _board(tmp_path, board_repo, "worker-a")

    result = board.acquire_many(["first", "held", "last"], ttl=600)

    assert result == claims.ClaimBatchResult(False, ("held",), "held by another worker")
    assert not result
    assert _refs(board_repo) == before


def test_acquire_many_takes_over_an_expired_key(tmp_path: Path, board_repo: Path) -> None:
    _plant_lease(board_repo, "expired", owner="worker-b")
    board = _board(tmp_path, board_repo, "worker-a")

    result = board.acquire_many(["fresh", "expired"], ttl=600, note="revision")

    assert result == claims.ClaimBatchResult(True)
    for key in ("fresh", "expired"):
        lease = board.read(key)
        assert lease["owner"] == "worker-a"
        assert lease["note"] == "revision"
        assert _holds(board, key)


def test_acquire_many_changes_no_ref_when_one_lease_goes_stale_before_the_push(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_lease(board_repo, "contested", owner="worker-b")
    board = _board(tmp_path, board_repo, "worker-a")
    peer = _board(tmp_path, board_repo, "worker-b")
    push = board._cas_push_refs
    observed: dict[str, dict[str, str]] = {}

    def peer_renews_first(updates: Sequence[tuple[str, str | None, str]], *, atomic: bool = False):
        # The expired lease passed the ownership check; its owner renews it
        # before the push, so exactly one of the two leases is stale.
        assert peer.renew("contested", ttl=600)
        observed["refs"] = _refs(board_repo)
        return push(updates, atomic=atomic)

    monkeypatch.setattr(board, "_cas_push_refs", peer_renews_first)
    result = board.acquire_many(["free", "contested"], ttl=600)

    assert result == claims.ClaimBatchResult(False, ("contested",), "changed concurrently")
    assert _refs(board_repo) == observed["refs"]
    assert claims.CLAIM_REF_PREFIX + "free" not in observed["refs"]
    assert _holds(peer, "contested")


def test_acquire_many_changes_no_ref_when_the_remote_aborts_the_transaction(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    board._ensure_scratch()
    rival = _git("commit-tree", _git("mktree", cwd=board_repo, input_text=""), "-m", "rival", cwd=board_repo)
    contested = claims.CLAIM_REF_PREFIX + "contested"
    # Git runs pre-push after checking every lease against the remote's
    # advertisement, so this rival claim meets the remote's own check inside
    # the ref transaction instead.
    hooks = board.scratch / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(f'#!/bin/sh\ncat >/dev/null\nexec git --git-dir="{board_repo}" update-ref {contested} {rival}\n')
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))

    result = board.acquire_many(["free", "contested"], ttl=600)

    assert result == claims.ClaimBatchResult(False, ("contested",), "changed concurrently")
    assert _refs(board_repo) == {contested: rival}


def test_overlapping_batch_race_has_one_winner_and_no_partial_loser(tmp_path: Path, board_repo: Path) -> None:
    boards = {owner: _board(tmp_path, board_repo, owner) for owner in ("worker-a", "worker-b")}
    barrier = threading.Barrier(2)
    original_remote_oids = claims.ClaimBoard._remote_oids

    def synchronized_remote_oids(self: claims.ClaimBoard, keys: Sequence[str]) -> dict[str, str]:
        oids = original_remote_oids(self, keys)
        barrier.wait(timeout=60)
        return oids

    for board in boards.values():
        board._remote_oids = synchronized_remote_oids.__get__(board, claims.ClaimBoard)  # type: ignore[method-assign]

    results: dict[str, claims.ClaimBatchResult] = {}
    errors: list[BaseException] = []

    def acquire(owner: str) -> None:
        try:
            results[owner] = boards[owner].acquire_many([f"only-{owner}", "shared"], ttl=600)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=acquire, args=(owner,)) for owner in boards]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    winners = [owner for owner, result in results.items() if result]
    assert len(winners) == 1
    winner = winners[0]
    loser = next(owner for owner in boards if owner != winner)
    assert results[loser] == claims.ClaimBatchResult(False, ("shared",), "changed concurrently")
    assert sorted(_refs(board_repo)) == [claims.CLAIM_REF_PREFIX + key for key in (f"only-{winner}", "shared")]
    assert boards[loser].read("shared")["owner"] == winner


def test_renew_many_renews_every_owned_key_and_keeps_notes(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_000.0
    monkeypatch.setattr(claims.time, "time", lambda: now)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire_many(["first", "second"], ttl=60, note="revision")
    monkeypatch.setattr(claims.time, "time", lambda: now + 30)

    assert board.renew_many(["first", "second"], ttl=600) == claims.ClaimBatchResult(True)
    for key in ("first", "second"):
        lease = board.read(key)
        assert lease["expires_at"] == now + 30 + 600
        assert lease["note"] == "revision"


def test_renew_many_renews_nothing_unless_every_key_is_owned(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    peer = _board(tmp_path, board_repo, "worker-b")
    assert board.acquire("mine", ttl=600)
    assert peer.acquire("theirs", ttl=600)
    before = _refs(board_repo)

    result = board.renew_many(["mine", "theirs", "absent"], ttl=900)

    assert result == claims.ClaimBatchResult(False, ("theirs", "absent"), "not held by this worker")
    assert _refs(board_repo) == before


def test_release_many_deletes_owned_keys_and_treats_absent_keys_as_released(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire_many(["first", "second"], ttl=600)

    assert board.release_many(["first", "absent", "second"]) == claims.ClaimBatchResult(True)
    assert _refs(board_repo) == {}
    assert board.release_many(["first", "second"]) == claims.ClaimBatchResult(True)


def test_release_many_releases_nothing_when_one_key_is_foreign(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    peer = _board(tmp_path, board_repo, "worker-b")
    assert board.acquire("mine", ttl=600)
    assert peer.acquire("theirs", ttl=600)
    before = _refs(board_repo)

    result = board.release_many(["mine", "theirs"])

    assert result == claims.ClaimBatchResult(False, ("theirs",), "not held by this worker")
    assert _refs(board_repo) == before


def test_release_many_deletes_no_ref_when_one_lease_goes_stale_before_the_push(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire_many(["kept", "stolen"], ttl=600)
    push = board._cas_push_refs
    observed: dict[str, dict[str, str]] = {}

    def peer_steals_first(updates: Sequence[tuple[str, str | None, str]], *, atomic: bool = False):
        _plant_lease(board_repo, "stolen")
        observed["refs"] = _refs(board_repo)
        return push(updates, atomic=atomic)

    monkeypatch.setattr(board, "_cas_push_refs", peer_steals_first)
    result = board.release_many(["kept", "stolen"])

    assert result == claims.ClaimBatchResult(False, ("stolen",), "changed concurrently")
    assert _refs(board_repo) == observed["refs"]
    assert _holds(board, "kept")


@pytest.mark.parametrize("method", ["acquire_many", "renew_many", "release_many"])
def test_batch_names_a_malformed_lease_as_blocking_and_changes_nothing(
    tmp_path: Path, board_repo: Path, method: str
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("mine", ttl=600)
    _plant_message(board_repo, "broken", "not json")
    before = _refs(board_repo)

    result = getattr(board, method)(["mine", "broken"])

    assert result == claims.ClaimBatchResult(False, ("broken",), "malformed lease")
    assert _refs(board_repo) == before


def test_acquire_many_names_every_blocking_key_in_batch_order(tmp_path: Path, board_repo: Path) -> None:
    peer = _board(tmp_path, board_repo, "worker-b")
    assert peer.acquire("held", ttl=600)
    _plant_message(board_repo, "broken", "not json")
    before = _refs(board_repo)
    board = _board(tmp_path, board_repo, "worker-a")

    result = board.acquire_many(["broken", "free", "held"], ttl=600)

    assert result == claims.ClaimBatchResult(False, ("broken", "held"), "malformed lease or held by another worker")
    assert _refs(board_repo) == before


@pytest.mark.parametrize("method", ["acquire_many", "renew_many", "release_many"])
@pytest.mark.parametrize(
    ("keys", "error", "match"),
    [
        (["first", "second", "first"], ValueError, "duplicate claim key 'first'"),
        ([], ValueError, "at least one claim key is required"),
        (["first", "../escape"], ValueError, "invalid claim key"),
        ("first", TypeError, "not one string"),
    ],
)
def test_batch_methods_reject_bad_keys_before_touching_the_board(
    tmp_path: Path, board_repo: Path, method: str, keys: object, error: type[Exception], match: str
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(error, match=match):
        getattr(board, method)(keys)

    assert not board.scratch.exists()
    assert _refs(board_repo) == {}


def test_batch_refuses_a_board_without_atomic_push_support(tmp_path: Path, board_repo: Path) -> None:
    with (board_repo / "config").open("a", encoding="utf-8") as config:
        config.write("[receive]\n\tadvertiseAtomic = false\n")
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.ClaimTransportError, match="does not support atomic pushes"):
        board.acquire_many(["first", "second"], ttl=600)

    assert _refs(board_repo) == {}
    assert board.acquire("first", ttl=600)


def test_single_key_push_is_unchanged_and_batches_push_atomically(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    git = board._git
    pushes: list[list[str]] = []

    def recording_git(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "push":
            pushes.append(args)
        return git(args, **kwargs)

    monkeypatch.setattr(board, "_git", recording_git)
    assert board.acquire("single", ttl=600)
    assert board.acquire_many(["one", "two"], ttl=600)

    single, one, two = (claims.CLAIM_REF_PREFIX + key for key in ("single", "one", "two"))
    refs = _refs(board_repo)
    assert pushes == [
        ["push", "--quiet", "--porcelain", f"--force-with-lease={single}:", board.repo_url, f"{refs[single]}:{single}"],
        [
            "push",
            "--quiet",
            "--porcelain",
            "--atomic",
            f"--force-with-lease={one}:",
            f"--force-with-lease={two}:",
            board.repo_url,
            f"{refs[one]}:{one}",
            f"{refs[two]}:{two}",
        ],
    ]


# Old enough to outlast any grace; it formats as 2023-11-14T22:13:20Z.
_STALE_LOCK_MTIME = 1_700_000_000


def _plant_ref_lock(repo: Path, ref: str, *, modified: float = _STALE_LOCK_MTIME) -> Path:
    """Leave the lock file a Git process that stopped while updating ``ref`` would leave."""
    lock = repo / f"{ref}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    os.utime(lock, (modified, modified))
    return lock


def _new_board_repo(tmp_path: Path, ref_format: str) -> Path:
    repo = tmp_path / "claims.git"
    init = subprocess.run(
        ["git", "init", "--bare", "--quiet", f"--ref-format={ref_format}", str(repo)], capture_output=True, text=True
    )
    if init.returncode != 0:
        pytest.skip(f"this Git cannot create a {ref_format} repository: {init.stderr.strip()}")
    return repo


def _install_pre_push_hook(board: claims.ClaimBoard, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    """Run ``command`` after our lease check and before the remote's ref transaction."""
    hooks = board.scratch / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\ncat >/dev/null\nexec {command}\n")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))


@pytest.mark.parametrize(
    ("operation", "locked"),
    [
        (lambda board: board.acquire("free", ttl=600), "free"),
        (lambda board: board.renew("held", ttl=600), "held"),
        (lambda board: board.release("held"), "held"),
        (lambda board: board.acquire_many(["other", "free"], ttl=600), "free"),
        (lambda board: board.renew_many(["held", "held-too"], ttl=600), "held"),
        (lambda board: board.release_many(["held", "held-too"]), "held"),
    ],
    ids=["acquire", "renew", "release", "acquire_many", "renew_many", "release_many"],
)
def test_a_stale_ref_lock_refuses_the_update_names_its_file_and_stays(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, operation, locked: str
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire_many(["held", "held-too"], ttl=600)
    lock = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + locked)
    before = _refs(board_repo)

    with pytest.raises(claims.ClaimRefLockedError) as raised:
        operation(board)

    assert str(raised.value) == (
        f"Git lock file {lock.resolve()} (modified 2023-11-14T22:13:20Z) blocks updates of "
        f"{claims.CLAIM_REF_PREFIX}{locked}. Git leaves a lock file behind when a process stops while "
        "updating a ref; if no Git process is using the claim board, delete it and retry"
    )
    assert [found.path for found in raised.value.locks] == [lock.resolve()]
    assert lock.exists()
    assert _refs(board_repo) == before


def test_once_the_operator_deletes_a_stale_ref_lock_the_claim_proceeds(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    board = _board(tmp_path, board_repo, "worker-a")
    lock = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "node")
    with pytest.raises(claims.ClaimRefLockedError):
        board.acquire("node", ttl=600)
    assert board.read("node") is None

    lock.unlink()

    assert board.acquire("node", ttl=600)
    assert _holds(board, "node")


@pytest.mark.parametrize("change", ["create", "update", "delete", "delete-packed", "batch"])
def test_a_lock_released_within_the_grace_is_a_lost_race(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 60.0)
    board = _board(tmp_path, board_repo, "worker-a")
    if change != "create":
        assert board.acquire_many(["other", "node"], ttl=600)
    before = _refs(board_repo)
    lock = _plant_ref_lock(board_repo, "packed-refs" if change == "delete-packed" else claims.CLAIM_REF_PREFIX + "node")
    existing_locks = claims.ClaimBoard._existing_locks
    seen: list[list[claims.ClaimRefLock]] = []

    def peer_push_finishes(paths: dict[Path, str]) -> list[claims.ClaimRefLock]:
        # The peer whose update held the lock when our push failed lets go
        # after the first look.
        seen.append(existing_locks(paths))
        lock.unlink(missing_ok=True)
        return seen[-1]

    monkeypatch.setattr(claims.ClaimBoard, "_existing_locks", staticmethod(peer_push_finishes))

    if change == "create":
        assert not board.acquire("node", ttl=600)
    elif change == "update":
        assert not board.renew("node", ttl=600)
    elif change == "batch":
        assert board.renew_many(["other", "node"], ttl=600) == claims.ClaimBatchResult(
            False, ("node",), "changed concurrently"
        )
    else:
        assert not board.release("node")
    assert [[found.path for found in locks] for locks in seen] == [[lock.resolve()], []]
    assert _refs(board_repo) == before


@pytest.mark.parametrize("change", ["update", "delete", "delete-packed"])
def test_a_lock_gone_before_the_first_look_is_a_lost_race(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("node", ttl=600)
    before = _refs(board_repo)
    lock = _plant_ref_lock(board_repo, "packed-refs" if change == "delete-packed" else claims.CLAIM_REF_PREFIX + "node")
    ref_store = board._ref_store

    def peer_push_finishes() -> Path | None:
        # The peer lets go between Git's refusal and the board's first look.
        lock.unlink(missing_ok=True)
        return ref_store()

    monkeypatch.setattr(board, "_ref_store", peer_push_finishes)

    assert not (board.renew("node", ttl=600) if change == "update" else board.release("node"))
    assert _refs(board_repo) == before


@pytest.mark.parametrize(
    ("visible", "old", "lost"),
    [(True, None, True), (True, "1" * 40, True), (False, None, True), (False, "1" * 40, False)],
    ids=["visible-create", "visible-update", "unseen-create", "unseen-update"],
)
def test_reference_already_exists_is_a_lost_race_unless_an_unseen_lock_may_explain_it(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, visible: bool, old: str | None, lost: bool
) -> None:
    # Git 2.53's whole refusal, whether a lost race or a lock file caused it.
    board = _board(tmp_path, board_repo, "worker-a")
    ref = claims.CLAIM_REF_PREFIX + "node"
    new = "2" * 40
    git = board._git

    def refused_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != "push":
            return git(args, **kwargs)
        out = f"To board\n!\t{new}:{ref}\t[remote rejected] (reference already exists)\nDone\n"
        return subprocess.CompletedProcess(["git", *args], 1, out, "error: failed to push some refs to 'board'\n")

    monkeypatch.setattr(board, "_git", refused_push)
    if not visible:
        monkeypatch.setattr(board, "_ref_store", lambda: None)

    if lost:
        assert board._cas_push_refs([("node", old, new)]) == claims.ClaimBatchResult(
            False, ("node",), "changed concurrently"
        )
    else:
        with pytest.raises(claims.ClaimTransportError, match="reference already exists"):
            board._cas_push_refs([("node", old, new)])


@pytest.mark.parametrize("change", ["create", "update", "delete"])
def test_a_lost_race_is_refused_rather_than_an_error(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    # Git 2.53 words each of these losses differently from Git 2.50. As in the
    # atomic test above, the rival change passes our lease check and meets the
    # remote's own check inside the ref transaction.
    board = _board(tmp_path, board_repo, "worker-a")
    board._ensure_scratch()
    ref = claims.CLAIM_REF_PREFIX + "node"
    if change != "create":
        assert board.acquire("node", ttl=600)
    rival = _git("commit-tree", _git("mktree", cwd=board_repo, input_text=""), "-m", "rival", cwd=board_repo)

    if change == "delete":
        _install_pre_push_hook(board, monkeypatch, f'git --git-dir="{board_repo}" update-ref -d {ref}')
        assert not board.release("node")
        assert _refs(board_repo) == {}
    else:
        _install_pre_push_hook(board, monkeypatch, f'git --git-dir="{board_repo}" update-ref {ref} {rival}')
        assert not (board.acquire("node", ttl=600) if change == "create" else board.renew("node", ttl=600))
        assert _refs(board_repo) == {ref: rival}


def test_a_packed_refs_lock_blocks_releasing_claims(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("node", ttl=600)
    before = _refs(board_repo)
    lock = _plant_ref_lock(board_repo, "packed-refs")

    with pytest.raises(claims.ClaimRefLockedError, match="blocks releasing claims"):
        board.release("node")

    assert lock.exists()
    assert _refs(board_repo) == before


def test_a_stale_reftable_lock_blocks_every_claim_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    repo = _new_board_repo(tmp_path, "reftable")
    board = _board(tmp_path, repo, "worker-a")
    assert board.acquire("held", ttl=600)
    before = _refs(repo)
    lock = _plant_ref_lock(repo, "reftable/tables.list")

    for operation in (
        lambda: board.acquire("free", ttl=600),
        lambda: board.renew("held", ttl=600),
        lambda: board.release("held"),
    ):
        with pytest.raises(claims.ClaimRefLockedError, match="blocks every claim update"):
            operation()

    assert [(found.path, found.ref) for found in board.ref_locks()] == [(lock.resolve(), "reftable")]
    assert lock.exists()
    assert _refs(repo) == before


def test_a_stale_ref_lock_never_lets_a_takeover_replace_a_lease(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    _plant_lease(board_repo, "expired")
    _plant_lease(board_repo, "live", expires_at=2_000.0)
    before = _refs(board_repo)
    for key in ("expired", "live"):
        _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + key)
    board = _board(tmp_path, board_repo, "worker-a")

    assert not board.acquire("live", ttl=600)
    with pytest.raises(claims.ClaimRefLockedError):
        board.acquire("expired", ttl=600)

    assert _refs(board_repo) == before


def test_a_remote_board_neither_names_nor_deletes_a_lock_file(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    assert _board(tmp_path, board_repo, "worker-a").acquire("held", ttl=600)
    remote = claims.ClaimBoard(board_repo.as_uri(), "worker-a", tmp_path / "scratch-remote")
    # Over a network transport the board's files are not visible.
    monkeypatch.setattr(remote, "_ref_store", lambda: None)
    locks = [_plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + key) for key in ("held", "free")]
    before = _refs(board_repo)

    for operation in (
        lambda: remote.acquire("free", ttl=600),
        lambda: remote.renew("held", ttl=600),
        lambda: remote.release("held"),
    ):
        # Git's own error or a lost race, depending on the Git version.
        try:
            outcome = operation()
        except claims.ClaimTransportError as error:
            assert not isinstance(error, claims.ClaimRefLockedError)
        else:
            assert outcome is False

    assert all(lock.exists() for lock in locks)
    assert _refs(board_repo) == before


@pytest.mark.parametrize(
    "board_name",
    ["claims.git", "cl\u00e2ims.git ", "claims #1?.git"],
    ids=["plain", "non-ascii-trailing-space", "hash-and-question-mark"],
)
def test_a_file_url_or_unusual_path_board_names_its_stale_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, board_name: str
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    repo = tmp_path / board_name
    _git("init", "--bare", "--quiet", str(repo))
    lock = _plant_ref_lock(repo, claims.CLAIM_REF_PREFIX + "node")

    url_path = repo.as_uri()[len("file://") :]
    # Git ignores a file URL's host, decodes it, keeps "?" and "#", drops
    # trailing slashes and finds a board named without ".git".
    locations = [
        repo,
        f"file://{url_path}",
        f"file://localhost{url_path}",
        f"file://elsewhere{url_path}",
        f"file://{repo.as_posix()}",
        f"file://{url_path}/",
    ]
    if board_name.endswith(".git"):
        locations += [str(repo)[: -len(".git")], f"file://{url_path[: -len('.git')]}/"]
    for location in locations:
        board = claims.ClaimBoard(location, "worker-a", tmp_path / "scratch")
        with pytest.raises(claims.ClaimRefLockedError, match="blocks updates of"):
            board.acquire("node", ttl=600)
        assert [found.path for found in board.ref_locks()] == [lock.resolve()]
    assert lock.exists()


def test_an_inherited_git_common_dir_does_not_hide_a_stale_lock(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    other = tmp_path / "other.git"
    _git("init", "--bare", "--quiet", str(other))
    _plant_ref_lock(other, claims.CLAIM_REF_PREFIX + "node")
    lock = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "node")
    board = _board(tmp_path, board_repo, "worker-a")
    board._ensure_scratch()
    # An inherited variable, as inside a Git hook, must not redirect the lock
    # lookup. GIT_DIR cannot, since the lookup names --git-dir itself.
    monkeypatch.setenv("GIT_COMMON_DIR", str(other))

    with pytest.raises(claims.ClaimRefLockedError) as raised:
        board.acquire("node", ttl=600)

    assert [found.path for found in raised.value.locks] == [lock.resolve()]
    assert [found.path for found in board.ref_locks()] == [lock.resolve()]


def test_a_hook_refusal_stays_an_error_even_if_a_lock_came_and_went(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 60.0)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("node", ttl=600)
    lock = _plant_ref_lock(board_repo, "packed-refs")
    existing_locks = claims.ClaimBoard._existing_locks

    def peer_lets_go(paths: dict[Path, str]) -> list[claims.ClaimRefLock]:
        found = existing_locks(paths)
        lock.unlink(missing_ok=True)
        return found

    monkeypatch.setattr(claims.ClaimBoard, "_existing_locks", staticmethod(peer_lets_go))
    git = board._git
    ref = claims.CLAIM_REF_PREFIX + "node"

    def declined_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != "push":
            return git(args, **kwargs)
        # The hook's own words mention a lock and an existing reference.
        err = (
            "remote: fatal: Unable to create '/srv/mirror/.git/index.lock': File exists.\n"
            "remote: the mirror says: reference already exists\n"
            "error: failed to push some refs to 'board'\n"
        )
        out = f"To board\n!\t:{ref}\t[remote rejected] (pre-receive hook declined)\nDone\n"
        return subprocess.CompletedProcess(["git", *args], 1, out, err)

    monkeypatch.setattr(board, "_git", declined_push)

    with pytest.raises(claims.ClaimTransportError, match="pre-receive hook declined") as raised:
        board.release("node")
    assert not isinstance(raised.value, claims.ClaimRefLockedError)


def test_reference_already_exists_on_an_unseen_board_is_an_error_for_a_batch_with_an_update(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    monkeypatch.setattr(board, "_ref_store", lambda: None)
    new = "2" * 40
    created, updated = (claims.CLAIM_REF_PREFIX + key for key in ("created", "updated"))
    git = board._git

    def refused_push(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != "push":
            return git(args, **kwargs)
        out = (
            f"To board\n!\t{new}:{created}\t[remote rejected] (atomic transaction failed)\n"
            f"!\t{new}:{updated}\t[remote rejected] (reference already exists)\nDone\n"
        )
        return subprocess.CompletedProcess(["git", *args], 1, out, "error: failed to push some refs to 'board'\n")

    monkeypatch.setattr(board, "_git", refused_push)

    with pytest.raises(claims.ClaimTransportError, match="reference already exists"):
        board._cas_push_refs([("created", None, new), ("updated", "1" * 40, new)], atomic=True)


def test_a_packed_refs_lock_leaves_a_lost_create_race_a_refusal(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    board = _board(tmp_path, board_repo, "worker-a")
    board._ensure_scratch()
    lock = _plant_ref_lock(board_repo, "packed-refs")
    rival = _git("commit-tree", _git("mktree", cwd=board_repo, input_text=""), "-m", "rival", cwd=board_repo)
    ref = claims.CLAIM_REF_PREFIX + "node"
    _install_pre_push_hook(board, monkeypatch, f'git --git-dir="{board_repo}" update-ref {ref} {rival}')

    # packed-refs.lock blocks only deletions, so a lost race to create stays one.
    assert not board.acquire("node", ttl=600)
    assert _refs(board_repo) == {ref: rival}
    assert lock.exists()


def test_a_bad_gitfile_ends_the_board_lookup_as_it_ends_git_s(tmp_path: Path) -> None:
    named = tmp_path / "board"
    named.write_text("gitdir: /nonexistent\n")
    beside = tmp_path / "board.git"
    _git("init", "--bare", "--quiet", str(beside))
    _plant_ref_lock(beside, claims.CLAIM_REF_PREFIX + "node")
    board = claims.ClaimBoard(named, "worker-a", tmp_path / "scratch")

    # Git stops at the bad gitfile instead of trying board.git.
    assert board._ref_store() is None
    assert board.ref_locks() == []


def test_a_file_url_is_decoded_before_its_host_is_dropped(tmp_path: Path) -> None:
    decoy = tmp_path / "c.git"
    real = tmp_path / f"sub{tmp_path}" / "c.git"
    for repo in (decoy, real):
        _git("init", "--bare", "--quiet", str(repo))
        _plant_ref_lock(repo, claims.CLAIM_REF_PREFIX + "node")
    # Escaped slashes in the host part become part of the path Git pushes to.
    url = "file://" + urllib.parse.quote(f"/{tmp_path}/sub", safe="") + f"{tmp_path}/c.git"
    board = claims.ClaimBoard(url, "worker-a", tmp_path / "scratch")

    assert [found.path for found in board.ref_locks()] == [(real / f"{claims.CLAIM_REF_PREFIX}node.lock").resolve()]


def test_cleanup_skips_a_lease_whose_release_a_lock_file_blocks(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    for key in ("a-locked", "b-free"):
        _plant_lease(board_repo, key)
    _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "a-locked")
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.cleanup() == 1
    assert list(_refs(board_repo)) == [claims.CLAIM_REF_PREFIX + "a-locked"]


@pytest.mark.parametrize(("ref_format", "lock_ref"), [("files", "packed-refs"), ("reftable", "reftable/tables.list")])
def test_a_lock_that_blocks_every_release_ends_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref_format: str, lock_ref: str
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    repo = _new_board_repo(tmp_path, ref_format)
    for key in ("a", "b"):
        _plant_lease(repo, key)
    _plant_ref_lock(repo, lock_ref)
    board = _board(tmp_path, repo, "worker-a")
    push = board._cas_push
    pushed: list[str] = []

    def recording_push(key: str, old: str | None, new: str) -> bool:
        pushed.append(key)
        return push(key, old, new)

    monkeypatch.setattr(board, "_cas_push", recording_push)

    assert board.cleanup() == 0
    assert pushed == ["a"]
    assert sorted(_refs(repo)) == [claims.CLAIM_REF_PREFIX + "a", claims.CLAIM_REF_PREFIX + "b"]


def test_ref_locks_lists_the_lock_files_that_can_block_claims(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 3_600.0)
    board = _board(tmp_path, board_repo, "worker-a")
    valid = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "author/node")
    malformed = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "bad..key")
    packed = _plant_ref_lock(board_repo, "packed-refs")
    # A lock younger than the grace may still belong to a push in progress;
    # one dated ahead of the clock does not.
    _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "author/fresh", modified=time.time())
    future = _plant_ref_lock(board_repo, claims.CLAIM_REF_PREFIX + "author/future", modified=time.time() + 7_200)
    (board_repo / claims.CLAIM_REF_PREFIX / "directory.lock").mkdir()
    _plant_ref_lock(board_repo, "refs/heads/main")

    assert [(lock.path, lock.ref) for lock in board.ref_locks()] == [
        (packed.resolve(), "packed-refs"),
        (future.resolve(), claims.CLAIM_REF_PREFIX + "author/future"),
        (valid.resolve(), claims.CLAIM_REF_PREFIX + "author/node"),
        (malformed.resolve(), claims.CLAIM_REF_PREFIX + "bad..key"),
    ]
    assert board.acquire("author/other", ttl=600)
    # Git reads "file:/path" as SSH to a host named "file".
    for remote in ("https://example.invalid/claims.git", f"file:{board_repo}"):
        assert claims.ClaimBoard(remote, "worker-a", tmp_path / "remote").ref_locks() == []


def _alias(name: str, command: str) -> list[str]:
    """Git arguments that run ``command`` through sh as git alias ``name``."""
    return ["-c", f"alias.{name}=!{command}", name]


def _scratch_board(tmp_path: Path) -> claims.ClaimBoard:
    board = claims.ClaimBoard(tmp_path / "unused.git", "worker", tmp_path / "scratch")
    board._ensure_scratch()
    return board


def _assert_gone(pid: int) -> None:
    gone_by = time.monotonic() + 5
    while time.monotonic() < gone_by:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"process {pid} that git started is still running")


def test_git_runs_without_terminal_prompts(tmp_path: Path) -> None:
    board = _scratch_board(tmp_path)

    assert board._git(_alias("env", "printenv GIT_TERMINAL_PROMPT")).stdout.strip() == "0"


@pytest.mark.skipif(os.name != "posix", reason="controlling terminals are POSIX")
def test_git_runs_without_a_controlling_terminal(tmp_path: Path) -> None:
    board = _scratch_board(tmp_path)

    probe = _alias("tty", "(exec 3</dev/tty) 2>/dev/null && echo tty || echo none")
    assert board._git(probe, isolated=True).stdout.strip() == "none"


def test_git_reads_no_input_it_was_not_given(tmp_path: Path) -> None:
    board = _scratch_board(tmp_path)

    assert board._git(_alias("read", "cat"), deadline=time.monotonic() + 10).stdout == ""


def test_git_stops_at_the_deadline_and_names_the_step(tmp_path: Path) -> None:
    board = _scratch_board(tmp_path)
    started = time.monotonic()

    with pytest.raises(claims.ClaimTimeoutError) as timed_out:
        board._git(_alias("hang", "sleep 30"), deadline=started + 0.5, step="hanging")

    assert timed_out.value.step == "hanging"
    assert str(timed_out.value) == "no answer from the claim board while hanging"
    assert time.monotonic() - started < 10


def test_git_does_not_start_once_the_deadline_has_passed(tmp_path: Path) -> None:
    board = _scratch_board(tmp_path)
    marker = tmp_path / "ran"

    with pytest.raises(claims.ClaimTimeoutError):
        board._git(_alias("touch", f"touch '{marker}'"), deadline=time.monotonic() - 1, step="late")

    assert not marker.exists()


def test_git_reports_its_own_time_limit_as_a_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _scratch_board(tmp_path)
    monkeypatch.setattr(claims, "_GIT_CALL_TIMEOUT_S", 0.5)

    with pytest.raises(claims.ClaimTransportError) as failed:
        board._git(_alias("hang", "sleep 30"))

    assert not isinstance(failed.value, claims.ClaimTimeoutError)


def test_git_lets_a_caller_deadline_outlast_its_own_time_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _scratch_board(tmp_path)
    monkeypatch.setattr(claims, "_GIT_CALL_TIMEOUT_S", 0.5)
    started = time.monotonic()

    with pytest.raises(claims.ClaimTimeoutError) as timed_out:
        board._git(_alias("hang", "sleep 30"), deadline=started + 1.5, step="hanging")

    assert timed_out.value.step == "hanging"
    assert time.monotonic() - started >= 1.4


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_git_kills_what_it_started_when_it_times_out(tmp_path: Path) -> None:
    board = _scratch_board(tmp_path)
    pid_file = tmp_path / "pid"

    with pytest.raises(claims.ClaimTimeoutError):
        board._git(
            _alias("spawn", f"sleep 30 & echo $! > '{pid_file}'; wait"),
            deadline=time.monotonic() + 1,
            step="spawning",
            isolated=True,
        )

    _assert_gone(int(pid_file.read_text()))


def test_git_names_a_slow_step_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board = _scratch_board(tmp_path)
    monkeypatch.setattr(claims, "_PROGRESS_AFTER_S", 0.2)
    steps: list[str] = []

    board._git(_alias("slow", "sleep 1"), step="sleeping", progress=steps.append)

    assert steps == ["sleeping"]


def test_git_sends_its_input_once_to_a_slow_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board = _scratch_board(tmp_path)
    monkeypatch.setattr(claims, "_PROGRESS_AFTER_S", 0.2)

    echoed = board._git(_alias("echo", "sleep 1; cat"), input_text="once\n", progress=lambda step: None)

    assert echoed.stdout == "once\n"


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_git_kills_its_command_when_progress_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board = _scratch_board(tmp_path)
    # Long enough for sh to write the pid file before the callback runs.
    monkeypatch.setattr(claims, "_PROGRESS_AFTER_S", 0.5)
    pid_file = tmp_path / "pid"

    def broken(step: str) -> None:
        raise BrokenPipeError("stderr closed")

    with pytest.raises(BrokenPipeError):
        board._git(_alias("spawn", f"sleep 30 & echo $! > '{pid_file}'; wait"), progress=broken, isolated=True)

    _assert_gone(int(pid_file.read_text()))


@pytest.fixture
def hanging_board(monkeypatch: pytest.MonkeyPatch) -> str:
    """A board URL whose transport never answers."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.ext.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    return "ext::sleep 30"


def test_list_stops_at_its_timeout_and_names_the_step(tmp_path: Path, hanging_board: str) -> None:
    reader = claims.ClaimBoard(hanging_board, "worker-b", tmp_path / "scratch")
    started = time.monotonic()

    with pytest.raises(claims.ClaimTimeoutError) as timed_out:
        reader.list(timeout=1)

    assert timed_out.value.step == "fetching the claim board"
    assert time.monotonic() - started < 10
    assert _git("for-each-ref", "refs/autoform-list/", cwd=tmp_path / "scratch") == ""


def test_list_deletes_its_refs_after_a_timeout_later_in_the_listing(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git

    def times_out_reading_leases(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "cat-file":
            raise claims.ClaimTimeoutError(str(kwargs["step"]))
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", times_out_reading_leases)
    with pytest.raises(claims.ClaimTimeoutError) as timed_out:
        reader.list(timeout=60)

    assert timed_out.value.step == "reading 1 leases"
    assert _git("for-each-ref", "refs/autoform-list/", cwd=tmp_path / "scratch-worker-b") == ""


def test_list_gives_every_git_command_its_deadline_except_cleanup(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    deadlines: dict[str, object] = {}

    def recording_git(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        deadlines[args[0]] = kwargs.get("deadline")
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", recording_git)
    reader.list(timeout=60)

    assert deadlines.pop("update-ref") is None
    assert deadlines and all(isinstance(deadline, float) for deadline in deadlines.values())


def test_list_names_no_step_when_every_step_is_fast(tmp_path: Path, board_repo: Path) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    steps: list[str] = []

    assert [lease["_key"] for lease in _board(tmp_path, board_repo, "worker-b").list(progress=steps.append)] == ["a"]
    assert steps == []


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_list_refuses_a_timeout_that_is_not_a_positive_finite_number(
    tmp_path: Path, board_repo: Path, timeout: float
) -> None:
    with pytest.raises(ValueError, match="timeout"):
        _board(tmp_path, board_repo, "worker-b").list(timeout=timeout)


def test_list_reports_contention_when_every_fetch_is_refused_because_refs_moved(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    fetches: list[list[str]] = []

    def always_refused(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch":
            fetches.append(args)
            return subprocess.CompletedProcess(
                ["git", *args], 128, "", "fatal: remote error: upload-pack: not our ref 1234\n"
            )
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", always_refused)
    with pytest.raises(claims.ClaimContentionError, match="not our ref"):
        reader.list()
    assert len(fetches) == 3


def _counting(reader: claims.ClaimBoard, monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, object]]]:
    git = reader._git
    calls: list[tuple[list[str], dict[str, object]]] = []

    def counting_git(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", counting_git)
    return calls


def test_list_of_targets_returns_only_their_leases_as_a_full_listing_does(
    tmp_path: Path, board_repo: Path
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b", "c"], ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")

    full = {lease["_key"]: lease for lease in reader.list()}
    targeted = reader.list(["c", "a", "absent"])

    assert [lease["_key"] for lease in targeted] == ["a", "c"]
    assert targeted == [full["a"], full["c"]]


def test_list_of_absent_targets_is_empty_without_fetching(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    calls = _counting(reader, monkeypatch)

    assert reader.list(["absent"]) == []
    assert not any(args[0] == "fetch" for args, _ in calls)


def test_list_of_targets_fetches_only_their_refs_whatever_the_board_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = []
    for size in (1, 30):
        repo = tmp_path / f"board-{size}.git"
        _git("init", "--bare", "--quiet", str(repo))
        owner = claims.ClaimBoard(repo, "worker-a", tmp_path / f"owner-{size}")
        assert owner.acquire_many([f"k{index}" for index in range(size)], ttl=600)
        reader = claims.ClaimBoard(repo, "worker-b", tmp_path / f"reader-{size}")
        calls = _counting(reader, monkeypatch)

        assert [lease["_key"] for lease in reader.list(["k0"])] == ["k0"]
        fetches = [kwargs for args, kwargs in calls if args[0] == "fetch"]
        assert len(fetches) == 1
        [refspec] = str(fetches[0]["input_text"]).splitlines()
        assert refspec.startswith(f"+{claims.CLAIM_REF_PREFIX}k0:")
        assert not any(f"{claims.CLAIM_REF_PREFIX}*" in args for args, _ in calls)
        counts.append(len(calls))

    assert counts[0] == counts[1]


def test_list_of_targets_ignores_a_peer_renewing_another_claim(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b"], ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    fetches = 0

    def renew_b_after_each_git_command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal fetches
        fetches += args[0] == "fetch"
        proc = git(args, **kwargs)
        assert owner.renew("b")
        return proc

    monkeypatch.setattr(reader, "_git", renew_b_after_each_git_command)

    assert [lease["_key"] for lease in reader.list(["a"])] == ["a"]
    assert fetches == 1


def test_list_of_targets_names_only_the_targets_again_when_one_is_released_meanwhile(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire_many(["a", "b"], ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    git = reader._git
    calls: list[list[str]] = []

    def release_b_before_the_first_fetch(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch" and not any(call[0] == "fetch" for call in calls):
            assert owner.release("b")
        calls.append(args)
        return git(args, **kwargs)

    monkeypatch.setattr(reader, "_git", release_b_before_the_first_fetch)

    assert [lease["_key"] for lease in reader.list(["a", "b"])] == ["a"]
    assert sum(call[0] == "fetch" for call in calls) == 2
    assert not any(f"{claims.CLAIM_REF_PREFIX}*" in call for call in calls)


def test_list_of_targets_reports_a_malformed_target_as_an_entry(tmp_path: Path, board_repo: Path) -> None:
    blob = _git("hash-object", "-w", "--stdin", cwd=board_repo, input_text="not a lease")
    _git("update-ref", f"{claims.CLAIM_REF_PREFIX}m", blob, cwd=board_repo)

    [lease] = _board(tmp_path, board_repo, "worker-b").list(["m"])

    assert lease["_key"] == "m"
    assert lease["_malformed"] is True


@pytest.mark.parametrize("ignorecase", ["true", "false"])
def test_list_of_targets_lists_keys_that_differ_only_in_case_on_any_filesystem(
    tmp_path: Path, board_repo: Path, ignorecase: str
) -> None:
    # A case-sensitive board can hold both refs; a case-insensitive scratch cannot store them under their names.
    oids = _plant_packed_leases(board_repo, ["lean/Foo", "lean/foo"])
    reader = _board(tmp_path, board_repo, "worker-b")
    reader._ensure_scratch()
    reader._git(["config", "core.ignorecase", ignorecase])

    listed = reader.list(["lean/Foo", "lean/foo"])

    assert [(item["_key"], item["_oid"], item["_malformed"]) for item in listed] == [
        ("lean/Foo", oids["lean/Foo"], False),
        ("lean/foo", oids["lean/foo"], False),
    ]


def test_list_of_targets_refuses_invalid_targets_before_running_git(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _board(tmp_path, board_repo, "worker-b")
    calls = _counting(reader, monkeypatch)

    with pytest.raises(ValueError, match="duplicate"):
        reader.list(["a", "a"])
    with pytest.raises(ValueError, match="at least one"):
        reader.list([])
    with pytest.raises(ValueError, match="invalid claim key"):
        reader.list(["../x"])
    with pytest.raises(TypeError):
        reader.list("a")
    assert calls == []


def test_list_of_targets_stops_at_its_timeout_while_naming_them(tmp_path: Path, hanging_board: str) -> None:
    reader = claims.ClaimBoard(hanging_board, "worker-b", tmp_path / "scratch")

    with pytest.raises(claims.ClaimTimeoutError) as timed_out:
        reader.list(["a"], timeout=1)

    assert timed_out.value.step == "naming the claim refs"


@pytest.mark.skipif(os.name != "posix", reason="process groups and SIGTERM are POSIX")
def test_git_ends_with_its_caller_when_the_caller_is_terminated(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = (
        "import pathlib\n"
        "from autoform_cli import claims\n"
        f"board = claims.ClaimBoard(pathlib.Path({str(tmp_path / 'unused.git')!r}), 'w', "
        f"pathlib.Path({str(tmp_path / 'scratch')!r}))\n"
        "board._ensure_scratch()\n"
        f"board._git({_alias('spawn', f'sleep 30 & echo $! > {pid_file}; wait')!r}, isolated=True)\n"
    )
    caller = subprocess.Popen([sys.executable, "-c", script], cwd=Path(claims.__file__).parents[1])
    started_by = time.monotonic() + 10
    while not (pid_file.exists() and pid_file.read_text().strip()):
        assert time.monotonic() < started_by, "git never started its command"
        time.sleep(0.05)

    caller.send_signal(signal.SIGTERM)

    assert caller.wait(timeout=10) == -signal.SIGTERM
    _assert_gone(int(pid_file.read_text()))


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_git_shares_its_callers_process_group_unless_isolated(tmp_path: Path) -> None:
    board = _scratch_board(tmp_path)
    probe = _alias("pgid", "ps -o pgid= -p $$")

    assert int(board._git(probe).stdout) == os.getpgrp()
    assert int(board._git(probe, isolated=True).stdout) != os.getpgrp()


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_git_off_the_main_thread_shares_its_callers_process_group_even_isolated(tmp_path: Path) -> None:
    board = _scratch_board(tmp_path)
    probe = _alias("pgid", "ps -o pgid= -p $$")
    groups: list[int] = []
    worker = threading.Thread(target=lambda: groups.append(int(board._git(probe, isolated=True).stdout)), daemon=True)
    worker.start()
    worker.join(timeout=30)

    assert groups == [os.getpgrp()]


@pytest.mark.skipif(os.name != "posix", reason="process groups and signals are POSIX")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_git_ends_with_its_caller_when_a_worker_thread_runs_it(tmp_path: Path, signum: int) -> None:
    # As in `autoform dashboard`, whose request threads list claims.
    pid_file = tmp_path / "pid"
    script = (
        "import pathlib, threading\n"
        "from autoform_cli import claims\n"
        f"board = claims.ClaimBoard(pathlib.Path({str(tmp_path / 'unused.git')!r}), 'w', "
        f"pathlib.Path({str(tmp_path / 'scratch')!r}))\n"
        "board._ensure_scratch()\n"
        f"run = lambda: board._git({_alias('spawn', f'echo $$ > {pid_file}; exec sleep 30')!r}, isolated=True)\n"
        "worker = threading.Thread(target=run, daemon=True)\n"
        "worker.start()\n"
        "worker.join()\n"
    )
    caller = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(claims.__file__).parents[1],
        start_new_session=True,
        stderr=subprocess.DEVNULL,
    )
    try:
        started_by = time.monotonic() + 10
        while not (pid_file.exists() and pid_file.read_text().strip()):
            assert time.monotonic() < started_by, "git never started its command"
            time.sleep(0.05)

        os.killpg(caller.pid, signum)

        assert caller.wait(timeout=10) != 0
        _assert_gone(int(pid_file.read_text()))
    except BaseException:
        # A failed run leaves the caller, or git and its command in their own session.
        with contextlib.suppress(OSError):
            os.killpg(caller.pid, signal.SIGKILL)
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        caller.wait(timeout=10)
        raise


def test_list_runs_every_git_command_isolated_except_cleanup(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _board(tmp_path, board_repo, "worker-a")
    assert owner.acquire("a", ttl=600)
    reader = _board(tmp_path, board_repo, "worker-b")
    calls = _counting(reader, monkeypatch)

    reader.list()

    isolated = {args[0]: bool(kwargs.get("isolated")) for args, kwargs in calls}
    assert isolated.pop("update-ref") is False
    assert {"fetch", "for-each-ref", "cat-file"} <= isolated.keys()
    assert all(isolated.values())


@pytest.mark.skipif(os.name != "posix", reason="signals are POSIX")
def test_git_reports_a_termination_its_caller_survives_as_a_transport_error(tmp_path: Path) -> None:
    board = _scratch_board(tmp_path)
    seen: list[int] = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: seen.append(signum))
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM))
    try:
        timer.start()
        with pytest.raises(claims.ClaimTransportError, match="SIGTERM"):
            board._git(_alias("hang", "sleep 30"), isolated=True)
    finally:
        timer.cancel()
        signal.signal(signal.SIGTERM, previous)

    assert seen == [signal.SIGTERM]


@pytest.mark.skipif(os.name != "posix", reason="process groups and signals are POSIX")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_interrupting_an_acquire_leaves_no_ref_lock_on_the_board(
    tmp_path: Path, board_repo: Path, signum: int
) -> None:
    marker = tmp_path / "locked"
    hook = board_repo / "hooks" / "reference-transaction"
    hook.parent.mkdir(exist_ok=True)
    # Hold the claim ref's lock, as a push interrupted mid-update does.
    hook.write_text(f'#!/bin/sh\nif [ "$1" = prepared ]; then touch \'{marker}\'; sleep 30; fi\n')
    hook.chmod(0o755)
    script = (
        "import pathlib\n"
        "from autoform_cli import claims\n"
        f"board = claims.ClaimBoard(pathlib.Path({str(board_repo)!r}), 'w', pathlib.Path({str(tmp_path / 'scratch')!r}))\n"
        "board.acquire('x', ttl=600)\n"
    )
    # Its own session stands in for a terminal's foreground process group.
    caller = subprocess.Popen(
        [sys.executable, "-c", script], cwd=Path(claims.__file__).parents[1], start_new_session=True
    )
    try:
        started_by = time.monotonic() + 20
        while not marker.exists():
            assert time.monotonic() < started_by, "the push never took the claim ref's lock"
            time.sleep(0.05)

        os.killpg(caller.pid, signum)
        caller.wait(timeout=10)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(caller.pid, signal.SIGKILL)

    lock = board_repo / "refs" / "autoform-claims" / "x.lock"
    gone_by = time.monotonic() + 5
    while lock.exists() and time.monotonic() < gone_by:
        time.sleep(0.05)
    assert not lock.exists()


def test_list_of_targets_reads_only_them_on_a_board_of_thousands(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Thousands of claims and unrelated branches, as a busy project's origin holds.
    first, last = _plant_lease(board_repo, "k0000"), _plant_lease(board_repo, "k3549")
    refs = {f"{claims.CLAIM_REF_PREFIX}k{index:04d}": first for index in range(1, 3549)}
    refs.update({f"refs/heads/branch-{index}": first for index in range(2000)})
    refs[f"{claims.CLAIM_REF_PREFIX}k0000"] = first
    refs[f"{claims.CLAIM_REF_PREFIX}k3549"] = last
    for ref in (f"{claims.CLAIM_REF_PREFIX}k0000", f"{claims.CLAIM_REF_PREFIX}k3549"):
        _git("update-ref", "-d", ref, cwd=board_repo)
    (board_repo / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted \n" + "".join(f"{oid} {ref}\n" for ref, oid in sorted(refs.items()))
    )
    reader = _board(tmp_path, board_repo, "worker-b")
    calls = _counting(reader, monkeypatch)
    started = time.monotonic()

    listed = reader.list(["k3549", "k0000", "absent"], timeout=30)

    assert [(lease["_key"], lease["_oid"], lease["_malformed"]) for lease in listed] == [
        ("k0000", first, False),
        ("k3549", last, False),
    ]
    [fetch] = [kwargs for args, kwargs in calls if args[0] == "fetch"]
    assert len(str(fetch["input_text"]).splitlines()) == 2
    assert len(calls) <= 6
    assert time.monotonic() - started < 15


def _args(repo: Path, scratch: Path, *command: str) -> list[str]:
    return [
        "claim",
        *command,
        "--repo",
        str(repo),
        "--worker-id",
        "worker-a",
        "--scratch",
        str(scratch),
    ]


def test_claim_cli_acquire_renew_list_release_round_trip(tmp_path: Path, board_repo: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    node_id = "chapter/main theorem"

    assert main(_args(board_repo, scratch, "acquire", node_id, "--ttl", "600")) == 0
    assert "acquired chapter/main theorem" in capsys.readouterr().out
    assert main(_args(board_repo, scratch, "renew", node_id, "--ttl", "600")) == 0
    assert "renewed chapter/main theorem" in capsys.readouterr().out
    assert main(_args(board_repo, scratch, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    assert leases[0]["_key"] == claims.author_claim_key(node_id)
    assert leases[0]["owner"] == "worker-a"
    assert main(_args(board_repo, scratch, "release", node_id)) == 0
    assert "released chapter/main theorem" in capsys.readouterr().out


def test_claim_cli_refuses_live_peer_and_requires_identity(tmp_path: Path, board_repo: Path, capsys) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert main(_args(board_repo, first, "acquire", "node")) == 0
    capsys.readouterr()

    peer = [
        "claim",
        "acquire",
        "node",
        "--repo",
        str(board_repo),
        "--worker-id",
        "worker-b",
        "--scratch",
        str(second),
    ]
    assert main(peer) == 1
    assert "ownership is held or unverifiable" in capsys.readouterr().out

    assert main(["claim", "list", "--repo", str(board_repo), "--scratch", str(second)]) == 1
    assert "--worker-id" in capsys.readouterr().err


def test_claim_cli_transport_failure_is_nonzero(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing" / "claims.git"
    assert main(_args(missing, tmp_path / "scratch", "acquire", "node")) == 1
    assert "error:" in capsys.readouterr().out


def test_claim_cli_refuses_malformed_remote_lease(tmp_path: Path, board_repo: Path, capsys) -> None:
    node_id = "node"
    _plant_message(board_repo, claims.author_claim_key(node_id), "not json")

    assert main(_args(board_repo, tmp_path / "scratch", "acquire", node_id)) == 1
    assert "invalid lease JSON" in capsys.readouterr().out


def _claim_refs(repo: Path) -> list[str]:
    return sorted(_git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX, cwd=repo).split())


def _peer_args(repo: Path, scratch: Path, *command: str) -> list[str]:
    return ["claim", *command, "--repo", str(repo), "--worker-id", "worker-b", "--scratch", str(scratch)]


def test_claim_cli_single_node_output_is_unchanged(tmp_path: Path, board_repo: Path, capsys, monkeypatch) -> None:
    def batch_path(*args: object, **kwargs: object) -> None:
        raise AssertionError("one target must take the single-key path")

    for name in ("acquire_many", "renew_many", "release_many"):
        monkeypatch.setattr(claims.ClaimBoard, name, batch_path)
    scratch = tmp_path / "scratch"
    key = claims.author_claim_key("node")

    assert main(_args(board_repo, scratch, "acquire", "node", "--ttl", "600")) == 0
    assert capsys.readouterr().out == f"acquired node ({key})\n"
    assert main(_args(board_repo, scratch, "renew", "node", "--ttl", "600")) == 0
    assert capsys.readouterr().out == f"renewed node ({key})\n"
    assert main(_peer_args(board_repo, tmp_path / "peer", "acquire", "node")) == 1
    assert capsys.readouterr().out == "error: could not acquire node; ownership is held or unverifiable\n"
    assert main(_args(board_repo, scratch, "release", "node")) == 0
    assert capsys.readouterr().out == f"released node ({key})\n"


def test_claim_cli_multi_node_round_trip_prints_one_line_per_target(tmp_path: Path, board_repo: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    nodes = ["chapter/main theorem", "chapter/helper"]

    def expected(past_tense: str) -> str:
        return "".join(f"{past_tense} {node} ({claims.author_claim_key(node)})\n" for node in nodes)

    assert main(_args(board_repo, scratch, "acquire", *nodes, "--ttl", "600", "--note", "revision")) == 0
    assert capsys.readouterr().out == expected("acquired")
    assert main(_args(board_repo, scratch, "renew", *nodes, "--ttl", "600")) == 0
    assert capsys.readouterr().out == expected("renewed")
    assert main(_args(board_repo, scratch, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    assert sorted(lease["_key"] for lease in leases) == sorted(claims.author_claim_key(node) for node in nodes)
    assert {(lease["owner"], lease["note"]) for lease in leases} == {("worker-a", "revision")}
    assert main(_args(board_repo, scratch, "release", *nodes)) == 0
    assert capsys.readouterr().out == expected("released")
    assert _claim_refs(board_repo) == []


def test_claim_cli_acquire_without_a_note_drops_the_note(tmp_path: Path, board_repo: Path, capsys) -> None:
    """The owner's next acquire replaces its lease, note included, so one without `--note` leaves none."""
    scratch = tmp_path / "scratch"

    def listed() -> dict:
        capsys.readouterr()
        assert main(_args(board_repo, scratch, "list")) == 0
        [lease] = json.loads(capsys.readouterr().out)
        assert lease["owner"] == "worker-a"
        return lease

    assert main(_args(board_repo, scratch, "acquire", "chapter/result", "--note", "branch-a")) == 0
    assert listed()["note"] == "branch-a"
    assert main(_args(board_repo, scratch, "acquire", "chapter/result")) == 0
    assert "note" not in listed()


def test_claim_cli_multi_node_failure_changes_no_claim(tmp_path: Path, board_repo: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    assert main(_peer_args(board_repo, tmp_path / "peer", "acquire", "b")) == 0
    capsys.readouterr()

    assert main(_args(board_repo, scratch, "acquire", "a", "b", "c")) == 1
    captured = capsys.readouterr()
    assert captured.out == "error: could not acquire a, b, c; no claim was acquired: held by another worker: b\n"
    assert captured.err == ""
    assert _claim_refs(board_repo) == [claims.CLAIM_REF_PREFIX + claims.author_claim_key("b")]

    assert main(_args(board_repo, scratch, "acquire", "a")) == 0
    capsys.readouterr()
    assert main(_args(board_repo, scratch, "renew", "a", "b")) == 1
    assert capsys.readouterr().out == (
        "error: could not renew a, b; no claim was renewed: not held by this worker: b\n"
    )
    assert main(_args(board_repo, scratch, "release", "a", "b")) == 1
    assert capsys.readouterr().out == (
        "error: could not release a, b; no claim was released: not held by this worker: b\n"
    )
    assert _claim_refs(board_repo) == sorted(
        claims.CLAIM_REF_PREFIX + claims.author_claim_key(node) for node in ("a", "b")
    )


def test_claim_cli_multi_node_names_a_malformed_claim(tmp_path: Path, board_repo: Path, capsys) -> None:
    _plant_message(board_repo, claims.author_claim_key("b"), "not json")

    assert main(_args(board_repo, tmp_path / "scratch", "acquire", "a", "b")) == 1
    assert capsys.readouterr().out == "error: could not acquire a, b; no claim was acquired: malformed lease: b\n"
    assert _claim_refs(board_repo) == [claims.CLAIM_REF_PREFIX + claims.author_claim_key("b")]


def test_claim_cli_rejects_duplicate_targets_before_touching_the_board(
    tmp_path: Path, board_repo: Path, capsys
) -> None:
    scratch = tmp_path / "scratch"

    assert main(_args(board_repo, scratch, "acquire", "a", "b", "a")) == 2
    captured = capsys.readouterr()
    assert captured.err == "error: duplicate claim target: a\n"
    assert captured.out == ""
    assert _claim_refs(board_repo) == []
    assert not scratch.exists()


def test_claim_cli_multi_node_transport_failure_is_nonzero(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing" / "claims.git"
    assert main(_args(missing, tmp_path / "scratch", "acquire", "a", "b")) == 1
    assert capsys.readouterr().out.startswith("error: ")


def test_claim_cli_names_a_stale_ref_lock_and_list_and_cleanup_warn_about_it(
    tmp_path: Path, board_repo: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    scratch = tmp_path / "scratch"
    ref = claims.CLAIM_REF_PREFIX + claims.author_claim_key("a")
    lock = _plant_ref_lock(board_repo, ref)
    described = f"Git lock file {lock.resolve()} (modified 2023-11-14T22:13:20Z) blocks updates of {ref}"

    assert main(_args(board_repo, scratch, "acquire", "a")) == 1
    assert capsys.readouterr().out == (
        f"error: {described}. Git leaves a lock file behind when a process stops while updating a ref; "
        "if no Git process is using the claim board, delete it and retry\n"
    )

    for command, stdout in (("list", "[]\n"), ("cleanup", "removed 0 expired claim(s)\n")):
        assert main(_args(board_repo, scratch, command)) == 0
        captured = capsys.readouterr()
        assert captured.out == stdout
        assert captured.err == f"warning: {described}; if no Git process is using the claim board, delete it\n"
    assert lock.exists()


def test_claim_cli_lists_only_the_named_targets(tmp_path: Path, board_repo: Path, capsys) -> None:
    scratch = tmp_path / "scratch"
    assert main(_args(board_repo, scratch, "acquire", "node-a", "node-b")) == 0
    capsys.readouterr()

    assert main(_args(board_repo, scratch, "list", "node-b", "node-absent")) == 0
    captured = capsys.readouterr()

    assert [lease["_key"] for lease in json.loads(captured.out)] == [claims.author_claim_key("node-b")]
    assert captured.err == ""


def test_claim_cli_list_refuses_a_duplicate_target(tmp_path: Path, board_repo: Path, capsys) -> None:
    assert main(_args(board_repo, tmp_path / "scratch", "list", "node-a", "node-a")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: duplicate claim target: node-a\n"


def test_claim_cli_list_labels_a_timeout(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.ext.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    args = ["claim", "list", "--repo", "ext::sleep 30", "--worker-id", "worker-a"]

    assert main([*args, "--scratch", str(tmp_path / "scratch"), "--timeout", "1"]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == (
        "error: timeout: no answer from the claim board within 1s while fetching the claim board\n"
    )


def test_claim_cli_list_labels_a_transport_failure(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing" / "claims.git"

    assert main(_args(missing, tmp_path / "scratch", "list")) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err.startswith("error: transport: ")


def test_claim_cli_list_labels_contention(tmp_path: Path, board_repo: Path, capsys, monkeypatch) -> None:
    def contended(self: claims.ClaimBoard, *args: object, **kwargs: object) -> list[dict[str, object]]:
        raise claims.ClaimContentionError("the claim board refused 3 fetches because claim refs kept moving")

    monkeypatch.setattr(claims.ClaimBoard, "list", contended)

    assert main(_args(board_repo, tmp_path / "scratch", "list")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: contention: the claim board refused 3 fetches because claim refs kept moving\n"
    )


def test_claim_cli_list_names_a_slow_step_on_stderr(tmp_path: Path, board_repo: Path, capsys, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def slow(self: claims.ClaimBoard, keys: object = None, *, timeout: object = None, progress=None) -> list[object]:
        seen.update(keys=keys, timeout=timeout)
        progress("fetching 1 claim refs")
        return []

    monkeypatch.setattr(claims.ClaimBoard, "list", slow)

    assert main([*_args(board_repo, tmp_path / "scratch", "list", "node-a"), "--timeout", "7"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "[]\n"
    assert captured.err == "claim list: fetching 1 claim refs...\n"
    assert seen == {"keys": [claims.author_claim_key("node-a")], "timeout": 7.0}


def test_claim_cli_list_without_targets_passes_none_and_the_default_timeout(
    tmp_path: Path, board_repo: Path, capsys, monkeypatch
) -> None:
    seen: dict[str, object] = {}

    def record(self: claims.ClaimBoard, keys: object = None, *, timeout: object = None, progress=None) -> list[object]:
        seen.update(keys=keys, timeout=timeout)
        return []

    monkeypatch.setattr(claims.ClaimBoard, "list", record)

    assert main(_args(board_repo, tmp_path / "scratch", "list")) == 0
    assert seen == {"keys": None, "timeout": 120.0}
