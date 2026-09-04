"""Lifecycle regression tests for transactional REPL pool startup."""

from __future__ import annotations

import os
import threading

import pytest

from servers.repl import core as repl_core
from servers.repl import pool as repl_pool
from servers.repl.imports import resolve_project_imports


def test_pool_config_runs_base_post_init(monkeypatch):
    configured = []

    monkeypatch.setattr(
        repl_pool.LeanReplConfig,
        "__post_init__",
        lambda config: configured.append(config),
    )
    config = repl_pool.LeanReplPoolConfig(num_repls=1)

    assert configured == [config]


def test_repl_config_rejects_invalid_context_limits():
    with pytest.raises(ValueError, match="positive integer"):
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            max_contexts_per_process=True,
        )
    with pytest.raises(ValueError, match="startup, import, and request"):
        repl_pool.LeanReplPoolConfig(max_contexts_per_process=3)
    with pytest.raises(ValueError, match="startup, import, and request"):
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            max_contexts_per_process=2,
        )


def test_close_clears_context_state_even_after_process_exit(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            max_contexts_per_process=4,
        )
    )

    class ExitedProcess:
        def poll(self):
            return 1

    repl.process = ExitedProcess()
    repl._process_generation = 1
    repl._base_environment = repl._make_environment_handle((), 11)
    repl._import_environments[("Fixture",)] = repl._make_environment_handle(
        ("Fixture",), 22
    )
    repl._contexts_created = 3

    repl.close()

    assert repl.process is None
    assert repl._base_environment is None
    assert repl._import_environments == {}
    assert repl._contexts_created == 0
    assert repl._process_generation == 1


def test_successful_start_publishes_one_generation_after_warmup(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset({"Mathlib"}),
            max_contexts_per_process=4,
        )
    )

    class RunningProcess:
        def poll(self):
            return None

    monkeypatch.setattr(repl_core.subprocess, "Popen", lambda *args, **kwargs: RunningProcess())
    responses = iter(
        [
            {"env": 11, "messages": []},
            {"env": 12, "messages": []},
        ]
    )
    monkeypatch.setattr(repl, "_run", lambda code, env_id, timeout: next(responses))

    repl.start()

    assert repl._process_generation == 1
    assert repl._contexts_created == 2
    assert repl._base_environment is not None
    assert repl._base_environment.process_generation == 1
    assert repl._base_environment.import_context == ("Mathlib",)
    assert repl._base_environment.env_id == 11


def test_failed_start_does_not_publish_a_generation(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset({"Mathlib"}),
            max_contexts_per_process=4,
        )
    )

    class RunningProcess:
        def poll(self):
            return None

    monkeypatch.setattr(repl_core.subprocess, "Popen", lambda *args, **kwargs: RunningProcess())
    monkeypatch.setattr(
        repl,
        "_run",
        lambda code, env_id, timeout: {
            "env": 11,
            "messages": [
                {
                    "severity": "error",
                    "data": "bad import",
                    "pos": {"line": 1, "column": 0},
                }
            ],
        },
    )
    monkeypatch.setattr(repl_core, "_kill_subprocesses", lambda process: None)

    with pytest.raises(RuntimeError, match="Import preloading failed"):
        repl.start()

    assert repl._process_generation == 0
    assert repl._base_environment is None
    assert repl._contexts_created == 0
    assert repl.process is None


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


def test_pool_rejects_raw_environment_forwarding(monkeypatch):
    pool = object.__new__(repl_pool.LeanReplPool)

    with pytest.raises(TypeError, match="env_id"):
        pool.run("#check Nat", env_id=22)


def test_same_import_context_is_initialized_once_per_worker(tmp_path, monkeypatch):
    workers = []

    class RunningProcess:
        def poll(self):
            return None

    class InstrumentedRepl(repl_core.LeanRepl):
        def __init__(self, config):
            super().__init__(config)
            self.calls = []
            self.next_env = 20 + 10 * len(workers)
            workers.append(self)

        def start(self):
            self.process = RunningProcess()
            self._process_generation += 1
            self._base_environment = self._make_environment_handle((), self.next_env)
            self._project_fingerprint = repl_core.lean_project_fingerprint(
                self._project_identity
            )
            self._contexts_created = 1

        def close(self):
            self.process = None
            self._base_environment = None
            self._import_environments.clear()
            self._contexts_created = 0

        def _check_memory_and_maybe_restart(self, timeout=None):
            pass

        def _run(self, code, env_id, timeout):
            self.calls.append((code, env_id))
            self.next_env += 1
            return {"env": self.next_env, "messages": []}

    monkeypatch.setattr(repl_pool, "LeanRepl", InstrumentedRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(
            cwd=str(tmp_path),
            num_repls=2,
            startup_stagger=0,
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )
    imports = resolve_project_imports(tmp_path, (), timeout=1)
    try:
        for _ in range(4):
            pool.run("#check Nat", imports=imports, timeout=1)
    finally:
        pool.shutdown()

    assert [worker.calls for worker in workers] == [
        [("", None), ("#check Nat", 21), ("#check Nat", 21)],
        [("", None), ("#check Nat", 31), ("#check Nat", 31)],
    ]


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


def test_pool_forwards_resolved_imports_and_absolute_deadline(tmp_path, monkeypatch):
    calls = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def start(self):
            pass

        def run(self, code, *, imports, deadline):
            calls.append((code, imports, deadline))
            return {"env": 0}

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    monkeypatch.setattr(repl_pool.time, "monotonic", lambda: 10.0)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )
    imports = resolve_project_imports(tmp_path, (), timeout=1)

    try:
        assert pool.run("#check Nat", imports=imports, deadline=13.0) == {"env": 0}
    finally:
        pool.shutdown()

    assert calls == [("#check Nat", imports, 13.0)]


def test_pool_retires_a_worker_before_requeue_after_request_exception(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.close_calls = 0
            workers.append(self)

        def start(self):
            pass

        def run(self, code, *, imports, deadline):
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


def test_pool_converts_relative_timeout_to_one_absolute_deadline(monkeypatch):
    deadlines = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def start(self):
            pass

        def run(self, code, *, imports, deadline):
            deadlines.append(deadline)
            return {"env": 0}

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    monkeypatch.setattr(repl_pool.time, "monotonic", lambda: 10.0)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    try:
        pool.run("#check Nat", timeout=3.0)
    finally:
        pool.shutdown()

    assert deadlines == [13.0]


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


@pytest.mark.parametrize("internal", [{"env_id": 1}, {"unexpected": True}])
def test_pool_rejects_internal_worker_keywords(monkeypatch, internal):
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
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            pool.run("#check Nat", **internal)
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

    monkeypatch.setattr(repl_core.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    monkeypatch.setattr(repl, "_assert_project_current", lambda deadline: None)
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
