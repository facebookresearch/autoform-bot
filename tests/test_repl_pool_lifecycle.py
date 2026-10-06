"""Lifecycle regression tests for transactional REPL pool startup."""

from __future__ import annotations

import os
import threading
import time

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


def test_pool_retries_cleanup_and_returns_the_already_produced_result(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.dirty = False
            self.close_calls = 0
            workers.append(self)

        def run_disposable(self, code, **kwargs):
            self.dirty = True
            raise repl_core.ReplCleanupError("cleanup failed", {"messages": []})

        def is_clean(self):
            return not self.dirty

        def close(self):
            self.close_calls += 1
            if self.dirty and self.close_calls == 1:
                raise RuntimeError("transient cleanup failure")
            self.dirty = False

        def get_memory_usage(self):
            return 0.0

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    monkeypatch.setattr(repl_pool.time, "sleep", lambda delay: None)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=2))

    assert pool.run("#check Nat", timeout=1) == {"messages": []}
    assert workers[0].close_calls == 2
    assert workers[0].is_clean()
    assert pool._idle.qsize() == 2
    pool.shutdown()


def test_pool_withholds_a_dirty_slot_until_a_cleanup_retry_succeeds(monkeypatch):
    workers = []
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()

    class FakeRepl:
        def __init__(self, config):
            self.dirty = False
            self.close_calls = 0
            self.runs = []
            workers.append(self)

        def run_disposable(self, code, **kwargs):
            self.runs.append((code, self.is_clean()))
            if len(self.runs) == 1:
                self.dirty = True
                raise repl_core.ReplCleanupError("cleanup failed", {"messages": ["first"]})
            return {"messages": ["second"]}

        def is_clean(self):
            return not self.dirty

        def close(self):
            if self.dirty:
                self.close_calls += 1
                if self.close_calls == 1:
                    cleanup_started.set()
                    assert allow_cleanup.wait(timeout=2)
                    raise RuntimeError("transient cleanup failure")
                self.dirty = False

        def get_memory_usage(self):
            return 0.0

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    outcomes = {}

    def call(name, code):
        try:
            outcomes[name] = pool.run(code, timeout=5)
        except BaseException as error:
            outcomes[name] = error

    first = threading.Thread(target=call, args=("first", "#check Nat"))
    first.start()
    try:
        assert cleanup_started.wait(timeout=2)
        second = threading.Thread(target=call, args=("second", "#check Int"))
        second.start()
        second.join(timeout=0.2)
        assert len(workers[0].runs) == 1, "a dirty slot was handed to a queued call"
    finally:
        allow_cleanup.set()
        first.join(timeout=2)
    second.join(timeout=2)

    assert outcomes == {"first": {"messages": ["first"]}, "second": {"messages": ["second"]}}
    assert len(workers) == 1
    assert workers[0].runs == [("#check Nat", True), ("#check Int", True)]
    assert workers[0].close_calls == 2
    assert pool._active_calls == 0
    pool.shutdown()


def test_pool_withholds_a_slot_whose_cleanup_retry_was_interrupted(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            pass

        def run_disposable(self, code, **kwargs):
            raise repl_core.ReplCleanupError("cleanup failed", {"messages": []})

        def is_clean(self):
            return False

        def close(self):
            raise RuntimeError("cleanup failed")

        def get_memory_usage(self):
            return 0.0

    def interrupt(delay):
        raise KeyboardInterrupt("retry interrupted")

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    monkeypatch.setattr(repl_pool.time, "sleep", interrupt)

    with pytest.raises(KeyboardInterrupt, match="retry interrupted"):
        pool.run("#check Nat", timeout=1)

    assert pool._idle.empty()
    assert pool._active_calls == 0


def test_shutdown_waits_for_an_active_cleanup_retry(monkeypatch):
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()

    class FakeRepl:
        def __init__(self, config):
            self.dirty = False

        def run_disposable(self, code, **kwargs):
            self.dirty = True
            raise repl_core.ReplCleanupError("cleanup failed", {"messages": []})

        def is_clean(self):
            return not self.dirty

        def close(self):
            if self.dirty:
                cleanup_started.set()
                assert allow_cleanup.wait(timeout=2)
                self.dirty = False

        def get_memory_usage(self):
            return 0.0

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    results = []
    first = threading.Thread(target=lambda: results.append(pool.run("#check Nat", timeout=1)))
    first.start()
    stopper = threading.Thread(target=pool.shutdown)
    try:
        assert cleanup_started.wait(timeout=1)
        stopper.start()
        stopper.join(timeout=0.1)
        assert stopper.is_alive(), "shutdown ignored the active cleanup"
    finally:
        allow_cleanup.set()
        first.join(timeout=2)
        if stopper.is_alive():
            stopper.join(timeout=2)

    assert results == [{"messages": []}]
    assert not stopper.is_alive()


def test_pool_preserves_cleanup_cancellation_after_verified_retry(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.dirty = False
            self.close_calls = 0
            workers.append(self)

        def run_disposable(self, code, **kwargs):
            self.dirty = True
            raise repl_core.ReplCleanupError("cleanup failed", {"messages": []})

        def is_clean(self):
            return not self.dirty

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise KeyboardInterrupt("cleanup cancelled")
            self.dirty = False

        def get_memory_usage(self):
            return 0.0

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    monkeypatch.setattr(repl_pool.time, "sleep", lambda delay: None)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))

    with pytest.raises(KeyboardInterrupt, match="cleanup cancelled"):
        pool.run("#check Nat", timeout=1)

    assert workers[0].close_calls == 2
    assert workers[0].is_clean()
    assert pool._active_calls == 0
    assert pool._idle.qsize() == 1
    pool.shutdown()


def test_pool_preserves_original_cancellation_until_cleanup_is_verified(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.dirty = False
            self.close_calls = 0
            workers.append(self)

        def run_disposable(self, code, **kwargs):
            self.dirty = True
            raise KeyboardInterrupt("request cancelled")

        def is_clean(self):
            return not self.dirty

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise SystemExit("cleanup cancelled")
            self.dirty = False

        def get_memory_usage(self):
            return 0.0

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    monkeypatch.setattr(repl_pool.time, "sleep", lambda delay: None)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))

    with pytest.raises(KeyboardInterrupt, match="request cancelled") as error:
        pool.run("#check Nat", timeout=1)

    assert str(error.value) == "request cancelled"
    assert workers[0].close_calls == 2
    assert workers[0].is_clean()
    assert pool._active_calls == 0
    assert pool._idle.qsize() == 1
    pool.shutdown()


def test_shutdown_unblocks_a_call_waiting_for_a_cold_slot(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            pass

        def is_clean(self):
            return True

        def close(self):
            pass

        def get_memory_usage(self):
            return 0.0

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    pool._idle.get_nowait()
    errors = []
    waiter = threading.Thread(
        target=lambda: errors.append(
            pytest.raises(RuntimeError, pool.run, "#check Nat").value
        )
    )
    waiter.start()
    deadline = time.monotonic() + 2
    while pool._active_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    stopper = threading.Thread(target=pool.shutdown)
    stopper.start()
    waiter.join(timeout=2)
    stopper.join(timeout=2)

    assert not waiter.is_alive()
    assert not stopper.is_alive()
    assert len(errors) == 1
    assert "pool is shut down" in str(errors[0])


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
