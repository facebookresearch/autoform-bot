"""Lifecycle regression tests for transactional REPL pool startup."""

from __future__ import annotations

import os

import pytest

from servers.repl import core as repl_core
from servers.repl import pool as repl_pool


def test_partial_pool_construction_closes_all_constructed_workers(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.number = len(workers) + 1
            self.closed = False
            workers.append(self)
            if self.number == 2:
                raise RuntimeError("second slot failed")

        def close(self):
            self.closed = True

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    config = repl_pool.LeanReplPoolConfig(num_repls=3)

    with pytest.raises(RuntimeError, match="second slot failed"):
        repl_pool.LeanReplPool(config)

    assert len(workers) == 2
    assert workers[0].closed is True


def test_shutdown_closes_every_worker_and_drains_idle_queue(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.closed = False
            workers.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=2))

    pool.shutdown()

    assert all(worker.closed for worker in workers)
    assert pool._workers == []
    assert pool._idle.empty()


def test_request_timeout_includes_waiting_for_an_idle_worker(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            pass

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    borrowed = pool._idle.get_nowait()
    try:
        with pytest.raises(TimeoutError, match="waiting for an idle Lean REPL"):
            pool.run("#check Nat", timeout=0.01)
    finally:
        pool._idle.put(borrowed)
        pool.shutdown()


def test_pool_runs_each_call_disposably_and_reuses_only_a_clean_slot(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.calls = []
            workers.append(self)

        def run_disposable(self, code, **kwargs):
            self.calls.append((code, kwargs))
            return {"messages": []}

        def is_clean(self):
            return True

        def close(self):
            pass

        def get_memory_usage(self):
            return 0.0

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    try:
        assert pool.run("#check Nat", timeout=2) == {"messages": []}
        assert pool.run("#check Int", timeout=2) == {"messages": []}
    finally:
        pool.shutdown()

    assert len(workers) == 1
    assert [call[0] for call in workers[0].calls] == ["#check Nat", "#check Int"]


def test_pool_quarantines_every_slot_after_unverified_cleanup(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            self.dirty = False

        def run_disposable(self, code, **kwargs):
            self.dirty = True
            raise repl_core.ReplCleanupError("cleanup failed", {"messages": []})

        def is_clean(self):
            return not self.dirty

        def close(self):
            self.dirty = False

        def get_memory_usage(self):
            return 0.0

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=2))

    with pytest.raises(repl_core.ReplCleanupError, match="cleanup failed"):
        pool.run("#check Nat", timeout=1)
    with pytest.raises(RuntimeError, match="pool is shut down"):
        pool.run("#check Int", timeout=1)

    assert pool._idle.qsize() == 1
    pool.shutdown()


def test_repl_retry_recovery_uses_the_original_deadline(monkeypatch):
    clock = {"now": 0.0}
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            max_retries=1,
            validate_imports=False,
            warmup_imports=frozenset(),
        )
    )
    calls = []
    closed = []

    monkeypatch.setattr(repl_core.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    monkeypatch.setattr(repl, "close", lambda **kwargs: closed.append(True))

    def consume_deadline(code, env_id, timeout):
        calls.append(timeout)
        clock["now"] += timeout
        raise TimeoutError("ambiguous timeout")

    monkeypatch.setattr(repl, "_run", consume_deadline)
    response = repl.run("#check Nat", timeout=1)

    assert calls == [1]
    assert closed == [True]
    assert "timed out" in response["repl_error"]


def test_repl_request_write_uses_the_operation_deadline():
    read_fd, write_fd = os.pipe()
    stdout_read_fd, stdout_write_fd = os.pipe()
    stderr_read_fd, stderr_write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    while True:
        try:
            os.write(write_fd, b"x" * 65536)
        except BlockingIOError:
            break

    stdin = os.fdopen(write_fd, "wb", buffering=0)

    class StalledProcess:
        stdout = None
        stderr = None

        def poll(self):
            return None

    process = StalledProcess()
    process.stdin = stdin
    # These streams are checked before the bounded write but never read in
    # this test because the deliberately full stdin pipe times out first.
    process.stdout = os.fdopen(stdout_read_fd, "rb", buffering=0)
    process.stderr = os.fdopen(stderr_read_fd, "rb", buffering=0)

    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            validate_imports=False,
            warmup_imports=frozenset(),
        )
    )
    repl.process = process
    try:
        with pytest.raises(TimeoutError, match="while writing"):
            repl._run("#check Nat", env_id=None, timeout=0.02)
    finally:
        stdin.close()
        process.stdout.close()
        process.stderr.close()
        os.close(read_fd)
        os.close(stdout_write_fd)
        os.close(stderr_write_fd)
