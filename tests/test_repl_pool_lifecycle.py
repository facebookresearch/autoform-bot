"""Lifecycle regression tests for transactional REPL pool startup."""

from __future__ import annotations

import os
import threading

import pytest

from servers.repl import core as repl_core
from servers.repl import pool as repl_pool


def test_partial_pool_startup_closes_all_constructed_workers(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.number = len(workers) + 1
            self.started = False
            self.closed = False
            workers.append(self)

        def start(self):
            self.started = True
            if self.number == 2:
                raise RuntimeError("second worker failed")

        def close(self):
            self.closed = True

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    config = repl_pool.LeanReplPoolConfig(num_repls=3, startup_stagger=0)

    with pytest.raises(RuntimeError, match="second worker failed"):
        repl_pool.LeanReplPool(config)

    assert len(workers) == 2
    assert workers[0].started is True
    assert workers[0].closed is True
    assert workers[1].closed is True


def test_partial_pool_startup_retries_failed_cleanup(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.number = len(workers) + 1
            self.close_calls = 0
            workers.append(self)

        def start(self):
            if self.number == 2:
                raise RuntimeError("second worker failed")

        def close(self):
            self.close_calls += 1
            if self.number == 1 and self.close_calls == 1:
                raise RuntimeError("transient cleanup failure")

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    monkeypatch.setattr(repl_pool.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="second worker failed"):
        repl_pool.LeanReplPool(
            repl_pool.LeanReplPoolConfig(num_repls=2, startup_stagger=0)
        )

    assert workers[0].close_calls == 2
    assert workers[1].close_calls == 1


def test_shutdown_closes_every_worker_and_drains_idle_queue(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.closed = False
            workers.append(self)

        def start(self):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=2, startup_stagger=0)
    )

    pool.shutdown()

    assert all(worker.closed for worker in workers)
    assert pool._workers == []
    assert pool._idle.empty()


def test_request_timeout_includes_waiting_for_an_idle_worker(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            pass

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )
    borrowed = pool._idle.get_nowait()
    try:
        with pytest.raises(TimeoutError, match="waiting for an idle Lean REPL"):
            pool.run("#check Nat", timeout=0.01)
    finally:
        pool._idle.put(borrowed)
        pool.shutdown()


def test_expired_deadline_after_admission_returns_worker(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            pass

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(repl_pool.time, "monotonic", lambda: next(clock))
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    try:
        with pytest.raises(TimeoutError, match="waiting for an idle Lean REPL"):
            pool.run("#check Nat", deadline=1)
        assert pool._idle.qsize() == 1
    finally:
        pool.shutdown()


def test_pool_retires_a_worker_before_requeue_after_request_exception(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.close_calls = 0
            workers.append(self)

        def start(self):
            pass

        def run(self, code, **kwargs):
            raise OSError("stdout failed")

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )
    try:
        with pytest.raises(OSError, match="stdout failed"):
            pool.run("#check Nat")

        assert workers[0].close_calls == 1
        assert pool._idle.qsize() == 1
    finally:
        pool.shutdown()


def test_pool_never_requeues_a_worker_that_repeatedly_fails_to_close(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.close_calls = 0
            workers.append(self)

        def start(self):
            pass

        def run(self, code, **kwargs):
            raise OSError("stdout failed")

        def close(self):
            self.close_calls += 1
            if self.close_calls < 3:
                raise RuntimeError("cleanup failed")

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        pool.run("#check Nat")

    assert workers[0].close_calls == 1
    assert pool._idle.empty()
    assert pool._active_calls == 0
    assert pool.is_usable() is False
    with pytest.raises(RuntimeError, match="pool is shut down"):
        pool.run("#check Bool")

    with pytest.raises(RuntimeError, match="cleanup failed"):
        pool.shutdown()
    pool.shutdown()

    assert workers[0].close_calls == 3
    assert pool._workers == []
    assert pool._closed is True


def test_pool_forwards_the_absolute_deadline(monkeypatch):
    calls = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def start(self):
            pass

        def run(self, code, **kwargs):
            calls.append((code, kwargs))
            return {"env": 0}

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    monkeypatch.setattr(repl_pool.time, "monotonic", lambda: 10.0)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    try:
        assert pool.run("#check Nat", deadline=13.0) == {"env": 0}
    finally:
        pool.shutdown()

    assert calls == [("#check Nat", {"deadline": 13.0})]


def test_pool_rejects_ambiguous_deadlines_before_worker_admission(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            pass

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    try:
        with pytest.raises(TypeError, match="timeout or deadline"):
            pool.run("#check Nat", timeout=1.0, deadline=2.0)
        assert pool._active_calls == 0
        assert pool._idle.qsize() == 1
    finally:
        pool.shutdown()


def test_shutdown_never_requeues_a_borrowed_worker(monkeypatch):
    running = threading.Event()
    release = threading.Event()
    shutdown_done = threading.Event()
    calls = []

    class FakeRepl:
        def __init__(self, config):
            self.closed = False

        def start(self):
            pass

        def run(self, code, **kwargs):
            calls.append(code)
            running.set()
            release.wait(timeout=2)
            return {"env": 0}

        def close(self):
            self.closed = True
            release.set()

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )
    first = threading.Thread(target=pool.run, args=("first",), kwargs={"timeout": 1})
    first.start()
    assert running.wait(timeout=1)

    errors = []

    def wait_for_worker():
        try:
            pool.run("second", timeout=0.1)
        except (RuntimeError, TimeoutError) as error:
            errors.append(error)

    second = threading.Thread(target=wait_for_worker)
    second.start()
    def shut_down():
        pool.shutdown()
        shutdown_done.set()

    shutdown = threading.Thread(target=shut_down)
    shutdown.start()
    with pool._condition:
        assert pool._condition.wait_for(lambda: pool._shutdown, timeout=1)
    assert not shutdown_done.is_set()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    shutdown.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not shutdown.is_alive()
    assert shutdown_done.is_set()
    assert calls == ["first"]
    assert len(errors) == 1
    assert pool._idle.empty()


def test_concurrent_shutdown_closes_each_worker_once(monkeypatch):
    close_started = threading.Event()
    release_close = threading.Event()
    second_started = threading.Event()
    close_calls = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def start(self):
            pass

        def close(self):
            close_calls.append(self)
            close_started.set()
            release_close.wait(timeout=2)

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    first = threading.Thread(target=pool.shutdown)

    def shut_down_second():
        second_started.set()
        pool.shutdown()

    second = threading.Thread(target=shut_down_second)
    first.start()
    assert close_started.wait(timeout=1)
    second.start()
    assert second_started.wait(timeout=1)
    release_close.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(close_calls) == 1


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
    repl._project_fingerprint = repl_core.lean_project_fingerprint(
        repl._project_identity
    )

    monkeypatch.setattr(repl_core.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    monkeypatch.setattr(repl, "close", lambda: closed.append(True))

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
