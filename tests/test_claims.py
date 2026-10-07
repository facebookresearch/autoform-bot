"""Tests for host-neutral Git-ref claim leases."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from autoform_cli import claims


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


def test_acquire_read_list_and_release_round_trip(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.acquire("author/node", ttl=600, note="proof")
    lease = board.read("author/node")
    assert lease is not None
    assert lease["schema"] == claims.CLAIM_SCHEMA
    assert lease["owner"] == "worker-a"
    assert lease["resource"] == "author/node"
    assert lease["note"] == "proof"
    assert board.holds("author/node")

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
    assert not first.holds("expired")
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
        lambda: board.acquire("malformed", ttl=600, steal=True),
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
    assert board.holds("k")
    assert board.renew("k", ttl=600)
    assert board.release("k")
    assert peer.holds("a/refs/autoform-claims/k")


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


def test_heartbeat_verifies_ownership_immediately_on_entry() -> None:
    class LostBoard:
        def renew(self, key: str, ttl: int | float) -> bool:
            return False

    heartbeat = claims.Heartbeat(LostBoard(), "key", interval=1, ttl=30)  # type: ignore[arg-type]
    with pytest.raises(claims.ClaimTransportError, match="lost before"):
        with heartbeat:
            pytest.fail("unowned work must not enter the protected context")

    assert heartbeat.lost.is_set()
    assert heartbeat._thread is None


def test_heartbeat_rejects_interval_that_can_outlive_lease() -> None:
    with pytest.raises(ValueError, match="shorter than"):
        claims.Heartbeat(object(), "key", interval=30, ttl=30)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("interval", "ttl", "message"),
    [
        (float("nan"), 30, "heartbeat interval must be a finite positive number"),
        (float("inf"), 30, "heartbeat interval must be a finite positive number"),
        (1, float("nan"), "claim TTL must be a finite positive number"),
        (1, float("inf"), "claim TTL must be a finite positive number"),
        (1, float("-inf"), "claim TTL must be a finite positive number"),
    ],
)
def test_heartbeat_rejects_nonfinite_timing(interval: float, ttl: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        claims.Heartbeat(object(), "key", interval=interval, ttl=ttl)  # type: ignore[arg-type]


def test_heartbeat_marks_ownership_lost_on_transport_failure() -> None:
    attempted = threading.Event()

    class FailingBoard:
        calls = 0

        def renew(self, key: str, ttl: int | float) -> bool:
            self.calls += 1
            if self.calls == 1:
                return True
            attempted.set()
            raise claims.ClaimTransportError("board unavailable")

    heartbeat = claims.Heartbeat(FailingBoard(), "key", interval=0.01, ttl=30)  # type: ignore[arg-type]
    with heartbeat:
        assert attempted.wait(timeout=2)
        assert heartbeat.lost.wait(timeout=2)

    assert isinstance(heartbeat.error, claims.ClaimTransportError)


def test_heartbeat_marks_ownership_lost_when_renew_is_refused() -> None:
    attempted = threading.Event()

    class LostBoard:
        calls = 0

        def renew(self, key: str, ttl: int | float) -> bool:
            self.calls += 1
            if self.calls == 1:
                return True
            attempted.set()
            return False

    heartbeat = claims.Heartbeat(LostBoard(), "key", interval=0.01, ttl=30)  # type: ignore[arg-type]
    with heartbeat:
        assert attempted.wait(timeout=2)
        assert heartbeat.lost.wait(timeout=2)

    assert heartbeat.error is None


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
        board.holds("wrong")
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
        assert board.holds(key)


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
    assert peer.holds("contested")


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
    peer = _board(tmp_path, board_repo, "worker-b")
    assert board.acquire_many(["kept", "stolen"], ttl=600)
    push = board._cas_push_refs
    observed: dict[str, dict[str, str]] = {}

    def peer_steals_first(updates: Sequence[tuple[str, str | None, str]], *, atomic: bool = False):
        assert peer.acquire("stolen", ttl=600, steal=True)
        observed["refs"] = _refs(board_repo)
        return push(updates, atomic=atomic)

    monkeypatch.setattr(board, "_cas_push_refs", peer_steals_first)
    result = board.release_many(["kept", "stolen"])

    assert result == claims.ClaimBatchResult(False, ("stolen",), "changed concurrently")
    assert _refs(board_repo) == observed["refs"]
    assert board.holds("kept")


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
