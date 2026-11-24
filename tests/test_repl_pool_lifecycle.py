"""Lifecycle regression tests for the cold Lean REPL slot pool."""

from __future__ import annotations

import functools
import threading
import time

import pytest

from servers.repl import core as repl_core
from servers.repl import pool as repl_pool

_SHUTDOWN_REFUSAL = "Lean REPL pool is shut down; this call did not start and can be retried"


def _park_in_order(monkeypatch, pool, targets) -> list[threading.Thread]:
    """Start each target as a daemon thread and wait for it to park before starting the next.

    A call counts as active before it parks on the pool's idle queue, so the
    wait watches the queue itself. Queue.get waits with the queue's mutex
    held, and Condition.wait queues the waiter before releasing that mutex,
    so the threads park in the order they start.
    """
    parked: list[threading.Thread] = []
    wait_on_queue = pool._idle.not_empty.wait

    def park(timeout=None):
        parked.append(threading.current_thread())
        return wait_on_queue(timeout)

    monkeypatch.setattr(pool._idle.not_empty, "wait", park)
    waiters: list[threading.Thread] = []
    for target in targets:
        waiter = threading.Thread(target=target, daemon=True)
        waiter.start()
        waiters.append(waiter)
        deadline = time.monotonic() + 10
        while len(parked) < len(waiters) and time.monotonic() < deadline:
            time.sleep(0.001)
        assert parked == waiters
    return waiters


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


def test_shutdown_retains_failed_workers_for_a_retry(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.close_calls = 0
            workers.append(self)

        def close(self):
            self.close_calls += 1
            if self is workers[0] and self.close_calls == 1:
                raise RuntimeError("injected close failure")

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=2))

    with pytest.raises(RuntimeError, match="workers remain owned"):
        pool.shutdown()

    assert pool._workers == [workers[0]]
    assert pool._idle.empty()
    assert [worker.close_calls for worker in workers] == [1, 1]

    pool.shutdown()

    assert pool._workers == []
    assert [worker.close_calls for worker in workers] == [2, 1]


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
        with pytest.raises(TimeoutError, match=r"timed out after 0\.01 seconds waiting for an idle Lean REPL slot"):
            pool.run("#check Nat", timeout=0.01)
    finally:
        pool._idle.put(borrowed)
        pool.shutdown()


def test_slot_that_arrives_at_the_deadline_times_out_before_the_call_starts(monkeypatch):
    clock = {"now": 0.0}

    class FakeRepl:
        def __init__(self, config):
            pass

        def run_disposable(self, code, **kwargs):
            pytest.fail("a call with no budget left must not start")

        def is_clean(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    monkeypatch.setattr(repl_pool.time, "monotonic", lambda: clock["now"])
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    take_slot = pool._idle.get

    def wait_for_slot(*args, **kwargs):
        # The wait for an idle slot takes the caller's whole 10 seconds.
        clock["now"] += 10
        return take_slot(*args, **kwargs)

    monkeypatch.setattr(pool._idle, "get", wait_for_slot)

    with pytest.raises(TimeoutError, match=r"timed out after 10 seconds waiting for an idle Lean REPL slot"):
        pool.run("#check Nat", timeout=10)
    pool.shutdown()


def test_spent_budget_times_out_before_waiting_for_a_slot(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            pass

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    # The whole budget is spent between taking the deadline and checking it,
    # so the call must not wait: Queue.get would refuse a negative timeout.
    readings = iter([0.0])
    monkeypatch.setattr(repl_pool.time, "monotonic", lambda: next(readings, 10.0))

    with pytest.raises(TimeoutError, match=r"timed out after 5 seconds waiting for an idle Lean REPL slot"):
        pool.run("#check Nat", timeout=5)
    assert pool._active_calls == 0
    pool.shutdown()


def test_queued_call_timeout_states_the_callers_budget(monkeypatch):
    command_timeouts = []
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(
            num_repls=1,
            validate_imports=False,
            warmup_imports=frozenset(),
        )
    )
    (repl,) = pool._workers

    def run(code, timeout):
        command_timeouts.append(timeout)
        raise repl_core.ReplOutcomeUnknown("transport failed") from TimeoutError()

    monkeypatch.setattr(repl, "start", lambda: None)
    monkeypatch.setattr(repl, "_run", run)
    # The wait for an idle slot takes 2 of the caller's 10 seconds.
    clock = {"now": 0.0}
    take_slot = pool._idle.get

    def wait_for_slot(*args, **kwargs):
        clock["now"] += 2
        return take_slot(*args, **kwargs)

    monkeypatch.setattr(repl_pool.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pool._idle, "get", wait_for_slot)
    try:
        assert pool.run("#check Nat", timeout=10) == {
            "repl_error": "Lean REPL combined command execution timed out after 10 seconds",
            "outcome_unknown": True,
        }
    finally:
        pool.shutdown()

    assert command_timeouts == [8]


def test_pool_runs_each_call_disposably_and_reuses_a_clean_slot(monkeypatch):
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

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    try:
        assert pool.run("#check Nat", timeout=2) == {"messages": []}
        assert pool.run("#check Int", timeout=2) == {"messages": []}
    finally:
        pool.shutdown()

    assert len(workers) == 1
    assert [call[0] for call in workers[0].calls] == ["#check Nat", "#check Int"]


def test_dirty_result_poisoning_transfers_cleanup_to_shutdown(monkeypatch):
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
            self.dirty = False

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))

    assert pool.run("#check Nat", timeout=1) == {"messages": []}
    assert workers[0].close_calls == 0
    assert not workers[0].is_clean()
    assert pool.requires_retirement
    with pytest.raises(RuntimeError, match=_SHUTDOWN_REFUSAL):
        pool.run("#check Int", timeout=1)

    pool.shutdown()
    assert workers[0].close_calls == 1
    assert workers[0].is_clean()


def test_request_cancellation_keeps_precedence_and_poisons_a_dirty_pool(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            self.dirty = False

        def run_disposable(self, code, **kwargs):
            self.dirty = True
            raise KeyboardInterrupt("request cancelled")

        def is_clean(self):
            return not self.dirty

        def close(self):
            self.dirty = False

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))

    with pytest.raises(KeyboardInterrupt, match="request cancelled"):
        pool.run("#check Nat", timeout=1)

    assert pool.requires_retirement
    assert pool._active_calls == 0
    pool.shutdown()


def test_capacity_one_waiters_keep_arrival_order(monkeypatch):
    service_order = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def run_disposable(self, code, **kwargs):
            service_order.append(int(code))
            return {"messages": []}

        def is_clean(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    held = pool._idle.get_nowait()
    waiters = _park_in_order(
        monkeypatch, pool, [functools.partial(pool.run, str(index), 30) for index in range(6)]
    )
    pool._idle.put(held)
    for waiter in waiters:
        waiter.join(timeout=2)

    assert not any(waiter.is_alive() for waiter in waiters)
    assert service_order == list(range(6))
    pool.shutdown()


def test_shutdown_waits_for_the_request_owned_cleanup(monkeypatch):
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    close_threads = []

    class FakeRepl:
        def __init__(self, config):
            self.dirty = False

        def run_disposable(self, code, **kwargs):
            self.dirty = True
            self.close()
            return {"messages": []}

        def is_clean(self):
            return not self.dirty

        def close(self):
            close_threads.append(threading.current_thread())
            if self.dirty:
                cleanup_started.set()
                assert allow_cleanup.wait(timeout=2)
                self.dirty = False

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    results = []
    request_thread = threading.Thread(
        target=lambda: results.append(pool.run("#check Nat", timeout=1))
    )
    request_thread.start()
    stopper = threading.Thread(target=pool.shutdown)
    try:
        assert cleanup_started.wait(timeout=1)
        stopper.start()
        stopper.join(timeout=0.1)
        assert stopper.is_alive(), "shutdown ignored the active cleanup"
        assert close_threads == [request_thread]
        assert pool._active_calls == 1
    finally:
        allow_cleanup.set()
        request_thread.join(timeout=2)
        if stopper.is_alive():
            stopper.join(timeout=2)

    assert results == [{"messages": []}]
    assert not stopper.is_alive()


def test_shutdown_unblocks_every_call_waiting_for_a_cold_slot(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            pass

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    pool._idle.get_nowait()
    errors = []

    # Each call's budget outlasts the joins below, so a waiter that misses the
    # shutdown wakeup is still parked when the test checks it.
    def refuse():
        errors.append(pytest.raises(RuntimeError, pool.run, "#check Nat", timeout=30).value)

    waiters = _park_in_order(monkeypatch, pool, [refuse] * 2)
    stopper = threading.Thread(target=pool.shutdown, daemon=True)
    stopper.start()
    for waiter in waiters:
        waiter.join(timeout=5)
    stopper.join(timeout=5)

    assert not any(waiter.is_alive() for waiter in waiters)
    assert not stopper.is_alive()
    assert [str(error) for error in errors] == [_SHUTDOWN_REFUSAL] * 2


def test_shutdown_refuses_a_call_that_already_took_a_slot(monkeypatch):
    calls = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def run_disposable(self, code, **kwargs):
            calls.append(code)
            return {"messages": []}

        def is_clean(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    take_slot = pool._idle.get

    def take_slot_as_shutdown_begins(*args, **kwargs):
        slot = take_slot(*args, **kwargs)
        # Shutdown stops admission after the call has left the queue.
        with pool._condition:
            pool._stop_admission_locked()
        return slot

    # The patch ends with the call: shutdown drains the queue through get too.
    with monkeypatch.context() as patch:
        patch.setattr(pool._idle, "get", take_slot_as_shutdown_begins)
        with pytest.raises(RuntimeError) as refused:
            pool.run("#check Nat", timeout=1)

    assert str(refused.value) == _SHUTDOWN_REFUSAL
    assert calls == []
    assert pool._active_calls == 0
    pool.shutdown()


def test_shutdown_waits_for_checked_out_worker_before_closing_or_draining(monkeypatch):
    run_started = threading.Event()
    release_run = threading.Event()
    close_started = threading.Event()

    class FakeRepl:
        def __init__(self, config):
            pass

        def run_disposable(self, code, **kwargs):
            run_started.set()
            assert release_run.wait(timeout=2)
            return {"messages": []}

        def is_clean(self):
            return True

        def close(self):
            close_started.set()

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(repl_pool.LeanReplPoolConfig(num_repls=1))
    run_thread = threading.Thread(target=lambda: pool.run("#check Nat", timeout=1))
    run_thread.start()
    assert run_started.wait(timeout=1)
    shutdown_thread = threading.Thread(target=pool.shutdown)
    shutdown_thread.start()

    assert not close_started.wait(timeout=0.1)
    release_run.set()
    run_thread.join(timeout=2)
    shutdown_thread.join(timeout=2)

    assert not run_thread.is_alive()
    assert not shutdown_thread.is_alive()
    assert close_started.is_set()
    assert pool._workers == []
    assert pool._idle.empty()
    with pytest.raises(RuntimeError, match=_SHUTDOWN_REFUSAL):
        pool.run("#check Int", timeout=0.1)
