"""Sharing, lifecycle, and resource-boundary tests for the Lean runtime."""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from servers.lean_client import (
    INSTALL_PATH_ID,
    PROTOCOL_VERSION,
    LeanRuntimeClient,
    LeanRuntimeError,
    LeanRuntimeOutcomeUnknown,
    LeanRuntimeProtocolError,
    LeanRuntimeRemoteError,
    LeanRuntimeUnavailable,
)
from servers.lean_runtime import (
    LeanRuntimeConfig,
    LeanRuntimeServices,
    ProjectResourceCache,
    ProjectResourceBusyError,
)


def make_lake_project(tmp_path, name: str):
    project = tmp_path / name
    project.mkdir()
    (project / "lakefile.toml").write_text(f'[package]\nname = "{name}"\n')
    return project


def runtime_config(**overrides):
    values = {
        "max_projects": 2,
        "idle_seconds": 1800.0,
        "total_repl_workers": 2,
        "repl_workers_per_project": 1,
        "repl_project_limit": 2,
        "repl_command": ("lake", "exe", "@repl/repl"),
        "lsp_command": ("lake", "serve"),
        "lsp_timeout": 60.0,
        "max_lsp_request_seconds": 600.0,
        "repl_request_timeout": 30.0,
        "max_repl_request_seconds": 240.0,
        "rpc_read_timeout": 1.0,
        "max_connections": 8,
        "response_timeout": 900.0,
    }
    values.update(overrides)
    return LeanRuntimeConfig(**values)


class FakePool:
    def __init__(self, root):
        self.root = root
        self.capacity = 1
        self._shutdown = False
        self.calls = []

    def run(self, code, **kwargs):
        self.calls.append((code, kwargs))
        return {"messages": []}

    def get_memory_usage(self):
        return 0.25

    def is_usable(self):
        return not self._shutdown

    def shutdown(self):
        self._shutdown = True


class FakeLsp:
    def __init__(self, root):
        self.root = root
        self.closed = False

    def close(self):
        self.closed = True

    def abort(self):
        self.closed = True

    def is_alive(self):
        return not self.closed


def test_runtime_reuses_one_project_pool_and_status_stays_lazy(tmp_path):
    project = make_lake_project(tmp_path, "shared")
    pools = []

    def create_pool(root):
        pool = FakePool(root)
        pools.append(pool)
        return pool

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=create_pool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        cold = services.dispatch("repl.status", {"project_dir": str(project)})
        assert cold["state"] == "cold"
        assert pools == []

        first = services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#check Nat", "timeout": None},
        )
        second = services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#check Int", "timeout": 3},
        )

        assert first == second == "Compiles successfully"
        assert len(pools) == 1
        assert [call[0] for call in pools[0].calls] == ["#check Nat", "#check Int"]
        now = time.monotonic()
        default_deadline = pools[0].calls[0][1]["deadline"]
        explicit_deadline = pools[0].calls[1][1]["deadline"]
        assert 0 < default_deadline - now <= 30.0
        assert 0 < explicit_deadline - now <= 3.0
        warm = services.dispatch("repl.status", {"project_dir": str(project)})
        assert warm["state"] == "warm"
        assert warm["memory_usage_gb"] == 0.25
    finally:
        services.close()

    assert pools[0]._shutdown is True


def test_repl_release_failure_after_result_is_outcome_unknown(tmp_path):
    project = make_lake_project(tmp_path, "release-failure")
    pools = []

    class InvalidAfterRunPool(FakePool):
        def is_usable(self):
            if self.calls:
                raise RuntimeError("post-result validation failed")
            return True

    def create_pool(root):
        pool = InvalidAfterRunPool(root)
        pools.append(pool)
        return pool

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=create_pool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        response = services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#check Nat", "timeout": 3},
        )
    finally:
        services.close()

    assert pools[0].calls
    assert "execution outcome unknown" in response
    assert "must not be replayed" in response
    assert "post-result validation failed" in response


def test_unexpected_pool_failure_after_attempt_is_outcome_unknown(tmp_path):
    project = make_lake_project(tmp_path, "pool-failure")
    calls = []

    class FailingPool(FakePool):
        def run(self, code, **kwargs):
            calls.append((code, kwargs))
            raise RuntimeError("unexpected pool failure")

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FailingPool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        response = services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#check Nat", "timeout": 3},
        )
    finally:
        services.close()

    assert calls
    assert "execution outcome unknown" in response
    assert "must not be replayed" in response
    assert "unexpected pool failure" in response


def test_pool_admission_failure_remains_retryable(tmp_path):
    from servers.repl.pool import ReplPoolBusyError

    project = make_lake_project(tmp_path, "pool-busy")

    class BusyPool(FakePool):
        def run(self, code, **kwargs):
            raise ReplPoolBusyError("worker queue expired before dispatch")

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=BusyPool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        with pytest.raises(ReplPoolBusyError, match="before dispatch"):
            services.dispatch(
                "repl.run",
                {"project_dir": str(project), "code": "#check Nat", "timeout": 3},
            )
    finally:
        services.close()


def test_status_reports_an_active_poisoned_pool_as_retiring(tmp_path):
    project = make_lake_project(tmp_path, "retiring-status")
    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        with services.repl_projects.lease(str(project)) as pool:
            assert pool is not None
            pool._shutdown = True

            status = services.dispatch(
                "repl.status",
                {"project_dir": str(project)},
            )

            assert status["state"] == "retiring"
            assert status["shutdown"] is True
    finally:
        services.close()


def test_shared_runtime_disables_ambiguous_repl_retries(tmp_path, monkeypatch):
    from servers import lean_runtime

    project = make_lake_project(tmp_path, "at-most-once")
    configs = []

    class CapturingPool(FakePool):
        def __init__(self, config):
            configs.append(config)
            super().__init__(config.cwd)

    monkeypatch.setattr(lean_runtime, "LeanReplPool", CapturingPool)
    services = LeanRuntimeServices(
        runtime_config(),
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#check Nat", "timeout": 1},
        )
        assert configs[0].max_retries == 0
    finally:
        services.close()


def test_lru_limit_never_evicts_an_active_project(tmp_path):
    first = make_lake_project(tmp_path, "first")
    second = make_lake_project(tmp_path, "second")
    closed = []
    second_attempted = threading.Event()
    second_created = threading.Event()

    def factory(root):
        if root == second.resolve():
            second_created.set()
        return root

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    def use_second():
        second_attempted.set()
        with cache.lease(str(second)) as resource:
            assert resource == second.resolve()

    with cache.lease(str(first)) as resource:
        assert resource == first.resolve()
        thread = threading.Thread(target=use_second)
        thread.start()
        assert second_attempted.wait(timeout=1)
        assert not second_created.wait(timeout=0.1)
        assert closed == []

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert second_created.is_set()
    assert closed == [first.resolve()]
    cache.close()
    assert closed == [first.resolve(), second.resolve()]


def test_project_slot_admission_stops_before_the_response_budget(tmp_path):
    first = make_lake_project(tmp_path, "busy-first")
    second = make_lake_project(tmp_path, "busy-second")
    created = []
    cache = ProjectResourceCache(
        lambda root: created.append(root) or root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with cache.lease(str(first)):
        started = time.monotonic()
        with pytest.raises(ProjectResourceBusyError, match="response budget"):
            with cache.lease(
                str(second),
                acquisition_timeout=0.05,
                creation_budget=0.02,
            ):
                pytest.fail("a busy project slot must not be admitted late")
        assert time.monotonic() - started < 0.5

    assert created == [first.resolve()]
    cache.close()


def test_project_startup_that_misses_its_budget_is_discarded(tmp_path):
    project = make_lake_project(tmp_path, "slow-startup")
    clock = {"now": 0.0}
    closed = []

    def slow_factory(root):
        clock["now"] = 11.0
        return root

    cache = ProjectResourceCache(
        slow_factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )

    with pytest.raises(ProjectResourceBusyError, match="startup exceeded"):
        with cache.lease(
            str(project),
            acquisition_timeout=10.0,
            creation_budget=1.0,
        ):
            pytest.fail("late project startup must never execute a tool request")

    assert closed == [project.resolve()]
    assert cache.state(str(project)) == "cold"
    cache.close()


def test_idle_ttl_never_closes_an_active_resource(tmp_path):
    project = make_lake_project(tmp_path, "idle")
    clock = {"now": 0.0}
    closed = []
    cache = ProjectResourceCache(
        lambda root: root,
        closed.append,
        max_entries=1,
        idle_seconds=10,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )

    with cache.lease(str(project)):
        clock["now"] = 20
        assert cache.evict_idle() == 0
        assert closed == []

    clock["now"] = 31
    assert cache.evict_idle() == 1
    assert closed == [project.resolve()]
    cache.close()


def test_failed_retirement_blocks_replacement_without_losing_ownership(tmp_path):
    first = make_lake_project(tmp_path, "retiring-first")
    second = make_lake_project(tmp_path, "retiring-second")
    created = []
    allow_close = False

    def factory(root):
        created.append(root)
        return root

    def close(resource):
        if not allow_close:
            raise RuntimeError("cleanup failed")

    cache = ProjectResourceCache(
        factory,
        close,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    with pytest.raises(ProjectResourceBusyError, match="failed to retire"):
        with cache.lease(str(second)):
            pytest.fail("replacement must wait for confirmed cleanup")

    assert created == [first.resolve()]
    assert cache.state(str(first)) == "retiring"
    assert cache.stats()["retiring"] == [str(first.resolve())]

    allow_close = True
    with cache.lease(str(second)) as resource:
        assert resource == second.resolve()
    assert created == [first.resolve(), second.resolve()]
    cache.close()


def test_concurrent_replacement_has_only_one_retirement_owner(tmp_path):
    first = make_lake_project(tmp_path, "single-closer-first")
    second = make_lake_project(tmp_path, "single-closer-second")
    close_started = threading.Event()
    release_close = threading.Event()
    concurrent_close = threading.Event()
    close_active = 0
    first_close_calls = 0

    def close(resource):
        nonlocal close_active, first_close_calls
        if resource != first.resolve():
            return
        first_close_calls += 1
        close_active += 1
        if close_active > 1:
            concurrent_close.set()
        close_started.set()
        release_close.wait(timeout=2)
        close_active -= 1

    cache = ProjectResourceCache(
        lambda root: root,
        close,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    errors = []

    def replace():
        try:
            with cache.lease(str(second)):
                pass
        except BaseException as error:
            errors.append(error)

    callers = [threading.Thread(target=replace) for _ in range(2)]
    callers[0].start()
    assert close_started.wait(timeout=1)
    callers[1].start()
    assert not concurrent_close.wait(timeout=0.1)
    release_close.set()
    for caller in callers:
        caller.join(timeout=2)

    assert all(not caller.is_alive() for caller in callers)
    assert errors == []
    assert first_close_calls == 1
    cache.close()


def test_cache_close_retains_failed_resources_for_a_later_retry(tmp_path):
    project = make_lake_project(tmp_path, "close-retry")
    close_calls = 0

    def close(resource):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError("cleanup failed")

    cache = ProjectResourceCache(
        lambda root: root,
        close,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)):
        pass

    with pytest.raises(RuntimeError, match="failed to retire 1"):
        cache.close()

    assert cache.stats()["retiring"] == [str(project.resolve())]
    cache.close()
    assert close_calls == 2
    assert cache.stats()["retiring"] == []


def test_lease_preserves_operation_cancellation_when_release_also_fails(tmp_path):
    project = make_lake_project(tmp_path, "release-cancellation")

    def is_valid(resource):
        raise asyncio.CancelledError("release")

    cache = ProjectResourceCache(
        lambda root: root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        is_valid=is_valid,
        start_sweeper=False,
    )

    with pytest.raises(KeyboardInterrupt, match="operation") as raised:
        with cache.lease(str(project)):
            raise KeyboardInterrupt("operation")

    if hasattr(raised.value, "add_note"):
        assert raised.value.__notes__ == [
            "Lean project resource release also failed: release"
        ]
    cache.close()


def test_services_attempt_lsp_cleanup_after_repl_cleanup_failure(monkeypatch):
    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    lsp_closed = []
    monkeypatch.setattr(
        services.repl_projects,
        "close",
        lambda: (_ for _ in ()).throw(RuntimeError("REPL cleanup failed")),
    )
    monkeypatch.setattr(
        services.lsp_projects,
        "close",
        lambda: lsp_closed.append(True),
    )

    with pytest.raises(RuntimeError, match="REPL cleanup failed"):
        services.close()

    assert lsp_closed == [True]


def test_terminal_cleanup_retries_without_releasing_ownership(monkeypatch):
    from servers import lean_runtime

    close_calls = 0
    delays = []

    class Services:
        def close(self):
            nonlocal close_calls
            close_calls += 1
            if close_calls < 3:
                raise RuntimeError("cleanup failed")

    monkeypatch.setattr(lean_runtime.time, "sleep", delays.append)

    lean_runtime._close_services_until_clean(Services())

    assert close_calls == 3
    assert delays == [
        lean_runtime.TERMINAL_CLEANUP_RETRY_SECONDS,
        lean_runtime.TERMINAL_CLEANUP_RETRY_SECONDS * 2,
    ]


def test_stdio_mcp_adapters_delegate_without_owning_lean_state():
    from servers.lsp.server import create_lsp_server
    from servers.repl.server import create_repl_server

    class FakeRuntime:
        def __init__(self):
            self.calls = []

        def request(self, method, params):
            self.calls.append((method, params))
            return "delegated"

    repl_runtime = FakeRuntime()
    repl = create_repl_server(repl_runtime)
    asyncio.run(
        repl.call_tool(
            "run_lean_code",
            {"project_dir": "/lean", "code": "#check Nat", "timeout": None},
        )
    )
    asyncio.run(repl.call_tool("get_repl_status", {"project_dir": "/lean"}))
    assert repl_runtime.calls == [
        (
            "repl.run",
            {"project_dir": "/lean", "code": "#check Nat", "timeout": None},
        ),
        ("repl.status", {"project_dir": "/lean"}),
    ]

    lsp_runtime = FakeRuntime()
    lsp = create_lsp_server(lsp_runtime)
    asyncio.run(
        lsp.call_tool(
            "lean_hover",
            {
                "project_dir": "/lean",
                "file_path": "Main.lean",
                "line": 0,
                "character": 3,
            },
        )
    )
    asyncio.run(
        lsp.call_tool(
            "lean_diagnostic_messages",
            {"project_dir": "/lean", "file_path": "Main.lean"},
        )
    )
    assert lsp_runtime.calls == [
        (
            "lsp.hover",
            {
                "project_dir": "/lean",
                "file_path": "Main.lean",
                "line": 0,
                "character": 3,
            },
        ),
        (
            "lsp.diagnostics",
            {"project_dir": "/lean", "file_path": "Main.lean"},
        ),
    ]


@pytest.mark.parametrize(
    ("timeout", "configured_default", "expected_deadline"),
    [(5, None, 105.0), (None, "7", 107.0)],
)
def test_repl_client_reuses_one_deadline_across_autostart_retry(
    runtime_dir,
    monkeypatch,
    timeout,
    configured_default,
    expected_deadline,
):
    from servers import lean_client

    now = [100.0]
    attempts = []
    startup_deadlines = []
    client = LeanRuntimeClient(socket_path=runtime_dir / "deadline.sock")
    if configured_default is not None:
        monkeypatch.setenv("AUTOFORM_REPL_REQUEST_TIMEOUT", configured_default)

    def request_once(method, params, **kwargs):
        attempts.append(kwargs["deadline"])
        if len(attempts) == 1:
            raise LeanRuntimeUnavailable("not listening")
        return "Compiles successfully"

    def ensure_running(*, deadline):
        startup_deadlines.append(deadline)
        now[0] = 102.0
        return {"running": True}

    monkeypatch.setattr(lean_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(client, "_request_once", request_once)
    monkeypatch.setattr(client, "ensure_running", ensure_running)

    assert client.request(
        "repl.run",
        {"project_dir": "/lean", "code": "#check Nat", "timeout": timeout},
    ) == "Compiles successfully"
    assert attempts == [expected_deadline, expected_deadline]
    assert startup_deadlines == [expected_deadline]


def test_repl_wire_deadline_is_internal_and_response_allows_cleanup(
    runtime_dir,
    monkeypatch,
):
    from servers import lean_client

    sent = []
    socket_timeouts = []

    class RespondingSocket:
        def settimeout(self, timeout):
            socket_timeouts.append(timeout)

        def connect(self, path):
            pass

        def sendall(self, payload):
            sent.append(json.loads(payload))

        def recv(self, size):
            return json.dumps(
                {
                    "v": PROTOCOL_VERSION,
                    "id": sent[0]["id"],
                    "ok": True,
                    "result": "Compiles successfully",
                }
            ).encode() + b"\n"

        def close(self):
            pass

    monkeypatch.setattr(lean_client.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(lean_client.socket, "socket", lambda *args: RespondingSocket())
    client = LeanRuntimeClient(
        socket_path=runtime_dir / "wire-deadline.sock",
        response_timeout=900,
    )
    params = {"project_dir": "/lean", "code": "#check Nat", "timeout": 5}

    assert client.request("repl.run", params, autostart=False) == "Compiles successfully"
    assert sent[0]["deadline"] == 105.0
    assert sent[0]["params"] == params
    assert "deadline" not in sent[0]["params"]
    assert socket_timeouts == [
        2.0,
        5.0,
        5.0 + lean_client.REPL_RESPONSE_GRACE_SECONDS,
    ]


def test_repl_response_budget_must_cover_operation_and_cleanup_before_dispatch(
    runtime_dir,
    monkeypatch,
):
    from servers import lean_client

    monkeypatch.setattr(
        lean_client.socket,
        "socket",
        lambda *args: pytest.fail("an invalid response budget must not connect"),
    )
    client = LeanRuntimeClient(
        socket_path=runtime_dir / "short-response.sock",
        response_timeout=36,
    )

    with pytest.raises(LeanRuntimeError, match="response timeout must exceed"):
        client.request(
            "repl.run",
            {"project_dir": "/lean", "code": "#check Nat", "timeout": 5},
        )


@pytest.mark.parametrize("deadline", [True, "soon", float("nan"), float("inf")])
def test_invalid_repl_client_deadline_never_connects(
    runtime_dir,
    monkeypatch,
    deadline,
):
    from servers import lean_client

    monkeypatch.setattr(
        lean_client.socket,
        "socket",
        lambda *args: pytest.fail("an invalid deadline must not connect"),
    )
    client = LeanRuntimeClient(socket_path=runtime_dir / "invalid-deadline.sock")

    with pytest.raises(LeanRuntimeError, match="deadline must be a finite number"):
        client.request(
            "repl.run",
            {"project_dir": "/lean", "code": "#check Nat", "timeout": 5},
            deadline=deadline,
        )


def test_repl_response_read_rechecks_one_absolute_deadline(
    runtime_dir,
    monkeypatch,
):
    from servers import lean_client

    now = [100.0]
    receives = []

    class DribblingSocket:
        def settimeout(self, timeout):
            pass

        def connect(self, path):
            pass

        def sendall(self, payload):
            pass

        def recv(self, size):
            receives.append(size)
            now[0] = 134.0
            return b"{"

        def close(self):
            pass

    monkeypatch.setattr(lean_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        lean_client.socket,
        "socket",
        lambda *args: DribblingSocket(),
    )
    client = LeanRuntimeClient(socket_path=runtime_dir / "dribble.sock")

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed"):
        client.request(
            "repl.run",
            {"project_dir": "/lean", "code": "#check Nat", "timeout": 1},
            autostart=False,
        )

    assert len(receives) == 1


def test_repl_response_arriving_after_deadline_is_not_accepted(
    runtime_dir,
    monkeypatch,
):
    from servers import lean_client

    now = [100.0]
    sent = []

    class LateResponseSocket:
        def settimeout(self, timeout):
            pass

        def connect(self, path):
            pass

        def sendall(self, payload):
            sent.append(json.loads(payload))

        def recv(self, size):
            now[0] = 134.0
            return json.dumps(
                {
                    "v": PROTOCOL_VERSION,
                    "id": sent[0]["id"],
                    "ok": True,
                    "result": "late result",
                }
            ).encode() + b"\n"

        def close(self):
            pass

    monkeypatch.setattr(lean_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        lean_client.socket,
        "socket",
        lambda *args: LateResponseSocket(),
    )
    client = LeanRuntimeClient(socket_path=runtime_dir / "late-response.sock")

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed"):
        client.request(
            "repl.run",
            {"project_dir": "/lean", "code": "#check Nat", "timeout": 1},
            autostart=False,
        )


def test_expired_repl_deadline_is_not_dispatched_after_autostart(
    runtime_dir,
    monkeypatch,
):
    from servers import lean_client

    now = [100.0]
    connects = []
    sends = []

    class MissingThenForbiddenSocket:
        def __init__(self, number):
            self.number = number

        def settimeout(self, timeout):
            pass

        def connect(self, path):
            connects.append(self.number)
            if self.number == 1:
                raise FileNotFoundError(path)
            pytest.fail("an expired request must not reconnect")

        def sendall(self, payload):
            sends.append(payload)

        def close(self):
            pass

    sockets = []

    def create_socket(*args):
        candidate = MissingThenForbiddenSocket(len(sockets) + 1)
        sockets.append(candidate)
        return candidate

    def ensure_running(*, deadline):
        assert deadline == 105.0
        now[0] = 106.0
        return {"running": True}

    monkeypatch.setattr(lean_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(lean_client.socket, "socket", create_socket)
    client = LeanRuntimeClient(socket_path=runtime_dir / "expired.sock")
    monkeypatch.setattr(client, "ensure_running", ensure_running)

    with pytest.raises(LeanRuntimeUnavailable, match="expired before dispatch"):
        client.request(
            "repl.run",
            {"project_dir": "/lean", "code": "#check Nat", "timeout": 5},
        )

    assert connects == [1]
    assert sends == []


@pytest.mark.parametrize(
    ("client_deadline", "server_timeout", "expected_deadline"),
    [
        (110.0, 30.0, 110.0),
        (200.0, 5.0, 105.0),
    ],
)
def test_runtime_caps_client_deadline_and_spends_admission_time(
    tmp_path,
    monkeypatch,
    client_deadline,
    server_timeout,
    expected_deadline,
):
    from servers import lean_runtime

    project = make_lake_project(tmp_path, "runtime-deadline")
    now = [100.0]
    pool = FakePool(project.resolve())
    lease_deadlines = []

    class Lease:
        def __enter__(self):
            now[0] = 103.0
            return pool

        def __exit__(self, *args):
            return False

    class Projects:
        def lease(self, project_dir, **kwargs):
            lease_deadlines.append(kwargs["deadline"])
            return Lease()

    monkeypatch.setattr(lean_runtime.time, "monotonic", lambda: now[0])
    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    original_projects = services.repl_projects
    services.repl_projects = Projects()
    try:
        assert services.dispatch(
            "repl.run",
            {
                "project_dir": str(project),
                "code": "#check Nat",
                "timeout": server_timeout,
            },
            client_deadline=client_deadline,
        ) == "Compiles successfully"
    finally:
        services.repl_projects = original_projects
        services.close()

    assert lease_deadlines == [expected_deadline]
    assert pool.calls == [
        ("#check Nat", {"deadline": expected_deadline})
    ]


def test_expired_runtime_deadline_never_warms_or_dispatches_a_pool(tmp_path):
    project = make_lake_project(tmp_path, "expired-runtime")
    pools = []
    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=lambda root: pools.append(FakePool(root)) or pools[-1],
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        with pytest.raises(ProjectResourceBusyError, match="expired before admission"):
            services.dispatch(
                "repl.run",
                {"project_dir": str(project), "code": "#check Nat", "timeout": 30},
                client_deadline=time.monotonic() - 1,
            )
        assert pools == []
    finally:
        services.close()


def test_runtime_does_not_dispatch_when_deadline_expires_during_admission(
    tmp_path,
    monkeypatch,
):
    from servers import lean_runtime

    project = make_lake_project(tmp_path, "expired-admission")
    now = [100.0]
    pool = FakePool(project.resolve())

    class Lease:
        def __enter__(self):
            now[0] = 106.0
            return pool

        def __exit__(self, *args):
            return False

    class Projects:
        def lease(self, project_dir, **kwargs):
            assert kwargs["deadline"] == 105.0
            return Lease()

    monkeypatch.setattr(lean_runtime.time, "monotonic", lambda: now[0])
    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    original_projects = services.repl_projects
    services.repl_projects = Projects()
    try:
        with pytest.raises(ProjectResourceBusyError, match="expired before execution"):
            services.dispatch(
                "repl.run",
                {"project_dir": str(project), "code": "#check Nat", "timeout": 30},
                client_deadline=105.0,
            )
    finally:
        services.repl_projects = original_projects
        services.close()

    assert pool.calls == []


def test_cache_does_not_start_a_resource_after_its_absolute_deadline(tmp_path):
    project = make_lake_project(tmp_path, "expired-cache-start")
    now = iter((0.0, 2.0))
    created = []
    cache = ProjectResourceCache(
        lambda root: created.append(root) or root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
        clock=lambda: next(now),
    )

    with pytest.raises(ProjectResourceBusyError, match="not enough response budget"):
        with cache.lease(str(project), deadline=1.0):
            pytest.fail("an expired lease must not start a project resource")

    assert created == []
    cache.close()


class _RuntimeRequestSocket:
    def __init__(self, payload):
        self.payload = payload
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self, size):
        payload, self.payload = self.payload, b""
        return payload


@pytest.mark.parametrize("include_deadline", [False, True])
def test_runtime_wire_deadline_is_optional_and_outside_params(include_deadline):
    from servers.lean_runtime import LeanRuntimeRequestHandler

    calls = []

    class Services:
        def dispatch(self, method, params, *, client_deadline=None):
            calls.append((method, params, client_deadline))
            return "Compiles successfully"

    request = {
        "v": PROTOCOL_VERSION,
        "id": "request-id",
        "method": "repl.run",
        "params": {"project_dir": "/lean", "code": "#check Nat", "timeout": 5},
    }
    deadline = time.monotonic() + 60 if include_deadline else None
    if include_deadline:
        request["deadline"] = deadline
    handler = object.__new__(LeanRuntimeRequestHandler)
    request_socket = _RuntimeRequestSocket(json.dumps(request).encode() + b"\n")
    handler.request = request_socket
    handler.wfile = io.BytesIO()
    services = Services()
    services.config = SimpleNamespace(rpc_read_timeout=1.0)
    handler.server = SimpleNamespace(
        services=services,
        request_shutdown=lambda: None,
    )

    handler.handle()

    assert calls == [("repl.run", request["params"], deadline)]
    response = json.loads(handler.wfile.getvalue())
    assert response["ok"] is True
    assert request_socket.timeouts[-1] > request_socket.timeouts[0]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"v":1,"v":1,"id":"request-id","method":"daemon.ping","params":{}}\n',
        b'{"v":true,"id":"request-id","method":"daemon.ping","params":{}}\n',
        b'{"v":1,"id":1,"method":"daemon.ping","params":{}}\n',
        b'{"v":1,"id":"","method":"daemon.ping","params":{}}\n',
        b'{"v":1,"id":"request-id","method":"daemon.ping","params":{},"extra":1}\n',
        b'{"v":1,"id":"request-id","method":"daemon.ping","params":{},"deadline":1}\n',
        b'{"v":1,"id":"request-id","method":"repl.run","params":{},"deadline":NaN}\n',
    ],
)
def test_runtime_server_rejects_malformed_request_envelopes(raw):
    from servers.lean_runtime import LeanRuntimeRequestHandler

    class Services:
        config = SimpleNamespace(rpc_read_timeout=1.0)

        def dispatch(self, *args, **kwargs):
            pytest.fail("a malformed request must not be dispatched")

    handler = object.__new__(LeanRuntimeRequestHandler)
    handler.request = _RuntimeRequestSocket(raw)
    handler.wfile = io.BytesIO()
    handler.server = SimpleNamespace(
        services=Services(),
        request_shutdown=lambda: None,
    )

    handler.handle()

    response = json.loads(handler.wfile.getvalue())
    assert response["ok"] is False
    assert response["error"]["type"] == "ValueError"


def test_oversized_repl_result_preserves_unknown_outcome(
    monkeypatch,
):
    from servers import lean_runtime

    class Services:
        config = SimpleNamespace(rpc_read_timeout=1.0)

        def dispatch(self, method, params, *, client_deadline=None):
            return "x" * 1_000

    request = {
        "v": PROTOCOL_VERSION,
        "id": "request-id",
        "method": "repl.run",
        "params": {"project_dir": "/lean", "code": "#check Nat"},
    }
    handler = object.__new__(lean_runtime.LeanRuntimeRequestHandler)
    handler.request = _RuntimeRequestSocket(json.dumps(request).encode() + b"\n")
    handler.wfile = io.BytesIO()
    handler.server = SimpleNamespace(
        services=Services(),
        request_shutdown=lambda: None,
    )
    monkeypatch.setattr(lean_runtime, "MAX_MESSAGE_BYTES", 512)

    handler.handle()

    response = json.loads(handler.wfile.getvalue())
    assert response["ok"] is True
    assert "execution outcome unknown" in response["result"]
    assert "must not be replayed" in response["result"]


def test_runtime_server_request_read_rechecks_one_absolute_deadline(monkeypatch):
    from servers import lean_runtime

    now = [100.0]
    receives = []

    class DribblingRequest:
        def settimeout(self, timeout):
            pass

        def recv(self, size):
            receives.append(size)
            now[0] = 101.0
            return b"{"

    handler = object.__new__(lean_runtime.LeanRuntimeRequestHandler)
    handler.request = DribblingRequest()
    handler.server = SimpleNamespace(
        services=SimpleNamespace(
            config=SimpleNamespace(rpc_read_timeout=0.5)
        )
    )
    monkeypatch.setattr(lean_runtime.time, "monotonic", lambda: now[0])

    with pytest.raises(TimeoutError, match="read deadline expired"):
        handler._read_request()

    assert len(receives) == 1


def test_runtime_server_resets_write_timeout_after_a_dribbling_request(
    monkeypatch,
    caplog,
):
    from servers import lean_runtime

    now = [100.0]
    request = json.dumps(
        {
            "v": PROTOCOL_VERSION,
            "id": "request-id",
            "method": "daemon.ping",
            "params": {},
        }
    ).encode() + b"\n"

    class DribblingRequest:
        def __init__(self):
            self.chunks = [request[:1], request[1:]]
            self.timeouts = []

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def recv(self, size):
            chunk = self.chunks.pop(0)
            if self.chunks:
                now[0] = 100.99
            return chunk

    class TimingOutWriter:
        def __init__(self, request_socket):
            self.request_socket = request_socket
            self.response = None
            self.write_timeout = None

        def write(self, encoded):
            self.response = json.loads(encoded)
            self.write_timeout = self.request_socket.timeouts[-1]
            raise socket.timeout("blocked")

        def flush(self):
            pytest.fail("flush must not run after a failed write")

    calls = []

    class Services:
        config = SimpleNamespace(rpc_read_timeout=1.0, response_timeout=900.0)

        def dispatch(self, method, params, *, client_deadline=None):
            calls.append((method, params, client_deadline))
            return {"pid": 1}

    request_socket = DribblingRequest()
    writer = TimingOutWriter(request_socket)
    handler = object.__new__(lean_runtime.LeanRuntimeRequestHandler)
    handler.request = request_socket
    handler.wfile = writer
    handler.server = SimpleNamespace(
        services=Services(),
        request_shutdown=lambda: None,
    )
    monkeypatch.setattr(lean_runtime.time, "monotonic", lambda: now[0])

    handler.handle()

    assert calls == [("daemon.ping", {}, None)]
    assert request_socket.timeouts[-2] == pytest.approx(0.01)
    assert writer.write_timeout == pytest.approx(lean_runtime.RUNTIME_SAFETY_SECONDS)
    assert writer.response == {
        "v": PROTOCOL_VERSION,
        "id": "request-id",
        "ok": True,
        "result": {"pid": 1},
    }
    assert "disconnected before receiving" in caplog.text


def test_lsp_diagnostic_formatting_remains_stable():
    from servers.lsp.server import format_lsp_diagnostics

    assert format_lsp_diagnostics([]).startswith("No diagnostics")
    formatted = format_lsp_diagnostics(
        [
            {
                "severity": 1,
                "message": "unknown identifier",
                "range": {"start": {"line": 2, "character": 4}},
            }
        ]
    )
    assert formatted == (
        "Diagnostics: 1 error(s), 0 warning(s)\n"
        "3:4: error: unknown identifier"
    )


def test_startup_times_out_while_previous_runtime_retains_lifetime_lock(
    runtime_dir,
    monkeypatch,
):
    import fcntl

    socket_path = runtime_dir / "retiring.sock"
    client = LeanRuntimeClient(socket_path=socket_path, startup_timeout=0.01)
    lock_calls = 0

    def flock(fd, operation):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 1:
            return
        raise BlockingIOError

    monkeypatch.setattr(fcntl, "flock", flock)

    with pytest.raises(LeanRuntimeUnavailable, match="still be cleaning up"):
        client.ensure_running()


def test_startup_does_not_acquire_a_free_lock_after_its_deadline(
    runtime_dir,
    monkeypatch,
):
    from servers import lean_client

    client = LeanRuntimeClient(
        socket_path=runtime_dir / "expired.sock",
        startup_timeout=1,
    )
    now = [100.0]
    lock_calls = []

    def unavailable_ping(*, autostart=False, deadline=None):
        now[0] = 102.0
        raise LeanRuntimeUnavailable("not listening")

    monkeypatch.setattr(lean_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(client, "ping", unavailable_ping)
    monkeypatch.setattr(
        "fcntl.flock",
        lambda *args: lock_calls.append(args),
    )

    with pytest.raises(LeanRuntimeUnavailable, match="startup coordination"):
        client.ensure_running()

    assert lock_calls == []


def test_previous_build_shutdown_uses_the_startup_deadline(runtime_dir, monkeypatch):
    from servers import lean_client

    current_socket = runtime_dir / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}-new.sock"
    old_socket = runtime_dir / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}-old.sock"
    old_socket.touch()
    client = LeanRuntimeClient(socket_path=current_socket)
    client._uses_default_paths = True
    calls = []

    class PreviousClient:
        def __init__(self, *, socket_path, **kwargs):
            assert socket_path == old_socket

        def request(self, method, *, autostart, deadline):
            calls.append((method, deadline))
            return {"build_generation": 0}

        def stop(self, *, deadline):
            calls.append(("stop", deadline))
            return {"pid": 7}

    monkeypatch.setattr(lean_client, "LeanRuntimeClient", PreviousClient)

    assert client._stop_previous_builds(deadline=123.0) == [7]
    assert calls == [("daemon.ping", 123.0), ("stop", 123.0)]


def test_build_generation_includes_shared_environment_code(monkeypatch):
    from servers import lean_client

    environment_module = lean_client.PACKAGE_ROOT / "servers" / "__init__.py"

    def fake_stat(path):
        return SimpleNamespace(
            st_mtime_ns=2 if path == environment_module else 1
        )

    monkeypatch.setattr(lean_client.Path, "stat", fake_stat)

    assert lean_client._build_generation() == 2


def test_failed_start_never_hard_kills_a_daemon_that_may_own_work(runtime_dir):
    client = LeanRuntimeClient(socket_path=runtime_dir / "failed-start.sock")

    class Process:
        returncode = None
        terminate_calls = 0
        kill_calls = 0

        def poll(self):
            return None

        def terminate(self):
            self.terminate_calls += 1

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("runtime", timeout)

        def kill(self):
            self.kill_calls += 1

    process = Process()
    client._terminate_failed_start(process)

    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_concurrent_clients_boot_one_daemon_that_outlives_each_client(runtime_dir, monkeypatch):
    socket_path = runtime_dir / "lean.sock"
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_MAX_LEAN_PROJECTS", "1")
    clients = [
        LeanRuntimeClient(socket_path=socket_path, startup_timeout=15),
        LeanRuntimeClient(socket_path=socket_path, startup_timeout=15),
    ]
    barrier = threading.Barrier(3)
    pids = []
    errors = []

    def start(client):
        barrier.wait()
        try:
            pids.append(client.ensure_running()["pid"])
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=start, args=(client,)) for client in clients]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=20)

    try:
        assert errors == []
        assert all(not thread.is_alive() for thread in threads)
        assert len(pids) == 2
        assert len(set(pids)) == 1

        # Clients own no process handle or shutdown hook. Losing the client that
        # happened to bootstrap the daemon cannot stop shared Lean state.
        del clients[0]
        assert clients[0].ping()["pid"] == pids[0]
    finally:
        try:
            clients[-1].stop()
        except LeanRuntimeUnavailable:
            pass

    deadline = time.monotonic() + 5
    while socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.025)
    assert not socket_path.exists()


def test_daemon_outlives_the_separate_process_that_started_it(
    tmp_path,
    runtime_dir,
    repo_root,
    monkeypatch,
):
    socket_path = runtime_dir / "owner.sock"
    project = make_lake_project(tmp_path, "cold")
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_MAX_LEAN_PROJECTS", "1")
    helper = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from servers.lean_client import LeanRuntimeClient; "
                "print(LeanRuntimeClient(socket_path=sys.argv[1]).ensure_running()['pid'])"
            ),
            str(socket_path),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert helper.returncode == 0, helper.stderr
    owner_pid = int(helper.stdout.strip())

    client = LeanRuntimeClient(socket_path=socket_path)
    try:
        assert client.ping()["pid"] == owner_pid
        status = client.request("daemon.status", autostart=False)
        assert status["repl_projects"]["resident"] == []
        assert status["lsp_projects"]["resident"] == []

        repl_status = client.request(
            "repl.status",
            {"project_dir": str(project)},
            autostart=False,
        )
        assert repl_status["state"] == "cold"
        assert client.request("daemon.status", autostart=False)["repl_projects"][
            "resident"
        ] == []
    finally:
        client.stop()


def test_stop_then_immediate_start_is_serialized(runtime_dir, monkeypatch):
    socket_path = runtime_dir / "restart.sock"
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    client = LeanRuntimeClient(socket_path=socket_path, startup_timeout=15)
    first_pid = client.ensure_running()["pid"]
    client.stop()
    second_pid = client.ensure_running()["pid"]
    try:
        assert second_pid != first_pid
        assert client.ping()["pid"] == second_pid
    finally:
        client.stop()


def test_new_build_replaces_previous_runtime_at_same_install_path(runtime_dir, monkeypatch):
    import fcntl

    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    old_socket = runtime_dir / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}-old.sock"
    old_client = LeanRuntimeClient(socket_path=old_socket, startup_timeout=15)
    old_pid = old_client.ensure_running()["pid"]

    current = LeanRuntimeClient(startup_timeout=15)
    try:
        current_pid = current.ensure_running()["pid"]
        assert current_pid != old_pid
        assert not old_socket.exists()
        lifetime_fd = os.open(current.paths.lifetime_lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lifetime_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lifetime_fd)
    finally:
        current.stop()


def test_default_cli_stop_finds_a_previous_build(runtime_dir, monkeypatch, capsys):
    from servers import lean_runtime

    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    old_socket = runtime_dir / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}-old.sock"
    old_client = LeanRuntimeClient(socket_path=old_socket, startup_timeout=15)
    old_client.ensure_running()

    lean_runtime.main(["stop"])

    result = capsys.readouterr().out
    assert "stopped_previous" in result
    assert not old_socket.exists()


def test_silent_connection_cannot_block_graceful_stop(runtime_dir, monkeypatch):
    socket_path = runtime_dir / "silent.sock"
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_RUNTIME_READ_TIMEOUT", "0.2")
    client = LeanRuntimeClient(socket_path=socket_path, startup_timeout=15)
    client.ensure_running()
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent.connect(str(socket_path))
    silent.sendall(b'{"v":1')
    errors = []

    def stop():
        try:
            client.stop()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=stop)
    thread.start()
    thread.join(timeout=3)
    try:
        assert not thread.is_alive()
        assert errors == []
        assert not socket_path.exists()
    finally:
        silent.close()
        if thread.is_alive():
            thread.join(timeout=3)


def test_connected_send_failure_is_never_retried(runtime_dir, monkeypatch):
    from servers import lean_client

    class FailingSocket:
        def settimeout(self, timeout):
            pass

        def connect(self, path):
            pass

        def sendall(self, payload):
            raise OSError("uncertain delivery")

        def close(self):
            pass

    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")
    monkeypatch.setattr(lean_client.socket, "socket", lambda *args: FailingSocket())
    monkeypatch.setattr(
        client,
        "ensure_running",
        lambda: pytest.fail("an ambiguously dispatched request must not be retried"),
    )

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed"):
        client.request("repl.run", {"project_dir": "/lean", "code": "#check Nat"})


def test_post_dispatch_timeout_is_explicitly_outcome_unknown(runtime_dir, monkeypatch):
    from servers import lean_client

    class TimingOutSocket:
        def settimeout(self, timeout):
            pass

        def connect(self, path):
            pass

        def sendall(self, payload):
            pass

        def recv(self, size):
            raise socket.timeout

        def close(self):
            pass

    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")
    monkeypatch.setattr(lean_client.socket, "socket", lambda *args: TimingOutSocket())
    monkeypatch.setattr(
        client,
        "ensure_running",
        lambda **kwargs: pytest.fail("a dispatched request must not be retried"),
    )

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed"):
        client.request("repl.run", {"project_dir": "/lean", "code": "#check Nat"})


@pytest.mark.parametrize("response", [b"", b"{\n"])
def test_post_dispatch_invalid_response_is_explicitly_outcome_unknown(
    runtime_dir,
    monkeypatch,
    response,
):
    from servers import lean_client

    class InvalidResponseSocket:
        def settimeout(self, timeout):
            pass

        def connect(self, path):
            pass

        def sendall(self, payload):
            pass

        def recv(self, size):
            return response

        def close(self):
            pass

    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")
    monkeypatch.setattr(
        lean_client.socket,
        "socket",
        lambda *args: InvalidResponseSocket(),
    )

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed"):
        client.request("repl.run", {"project_dir": "/lean", "code": "#check Nat"})


class _RuntimeResponseSocket:
    def __init__(self, response):
        self.response = response

    def settimeout(self, timeout):
        pass

    def connect(self, path):
        pass

    def sendall(self, payload):
        pass

    def recv(self, size):
        response, self.response = self.response, b""
        return response

    def close(self):
        pass


def test_runtime_response_limit_counts_the_frame_delimiter(monkeypatch):
    from servers import lean_client

    monkeypatch.setattr(lean_client, "MAX_MESSAGE_BYTES", 8)
    connection = _RuntimeResponseSocket(b"12345678\n")

    with pytest.raises(LeanRuntimeProtocolError, match="message limit"):
        LeanRuntimeClient._read_line(
            connection,
            deadline=time.monotonic() + 1,
        )


def _runtime_response(client, monkeypatch, response, *, method="repl.run"):
    from servers import lean_client

    monkeypatch.setattr(lean_client.uuid, "uuid4", lambda: type("UUID", (), {"hex": "request-id"})())
    monkeypatch.setattr(
        lean_client.socket,
        "socket",
        lambda *args: _RuntimeResponseSocket(response),
    )
    return client.request(method, {"project_dir": "/lean", "code": "#check Nat"})


@pytest.mark.parametrize(
    ("method", "encoded_result", "expected"),
    [
        ("repl.run", b'"Compiles successfully"', "Compiles successfully"),
        ("lsp.diagnostics", b'"No diagnostics"', "No diagnostics"),
        ("lsp.hover", b'"Nat"', "Nat"),
        ("daemon.ping", b'{"running":true}', {"running": True}),
        ("daemon.status", b'{"running":true}', {"running": True}),
        ("daemon.shutdown", b'{"stopping":true}', {"stopping": True}),
        ("repl.status", b'{"state":"cold"}', {"state": "cold"}),
    ],
)
def test_runtime_response_accepts_declared_method_result(
    runtime_dir,
    monkeypatch,
    method,
    encoded_result,
    expected,
):
    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")

    response = (
        b'{"v":1,"id":"request-id","ok":true,"result":'
        + encoded_result
        + b"}\n"
    )

    assert _runtime_response(client, monkeypatch, response, method=method) == expected


@pytest.mark.parametrize(
    ("method", "encoded_result"),
    [
        ("repl.run", b"null"),
        ("repl.run", b"false"),
        ("repl.run", b"0"),
        ("repl.run", b"[1,2]"),
        ("repl.run", b'{"nested":true}'),
        ("daemon.ping", b'"running"'),
    ],
)
def test_runtime_response_rejects_wrong_method_result_type(
    runtime_dir,
    monkeypatch,
    method,
    encoded_result,
):
    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")
    response = (
        b'{"v":1,"id":"request-id","ok":true,"result":'
        + encoded_result
        + b"}\n"
    )

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed") as caught:
        _runtime_response(client, monkeypatch, response, method=method)

    assert isinstance(caught.value.__cause__, LeanRuntimeProtocolError)


def test_runtime_response_accepts_exact_error_envelope(runtime_dir, monkeypatch):
    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")

    with pytest.raises(LeanRuntimeRemoteError, match="ValueError: bad request"):
        _runtime_response(
            client,
            monkeypatch,
            b'{"v":1,"id":"request-id","ok":false,'
            b'"error":{"type":"ValueError","message":"bad request"}}\n',
        )


@pytest.mark.parametrize(
    "response",
    [
        b'{"v":true,"id":"request-id","ok":true,"result":null}\n',
        b'{"v":1.0,"id":"request-id","ok":true,"result":null}\n',
        b'{"v":1,"id":1,"ok":true,"result":null}\n',
        b'{"v":1,"id":"wrong-id","ok":true,"result":null}\n',
        b'{"v":1,"id":"request-id","ok":1,"result":null}\n',
        b'{"v":1,"id":"request-id","ok":true}\n',
        b'{"v":1,"id":"request-id","ok":true,"result":null,"error":null}\n',
        b'{"v":1,"id":"request-id","ok":true,"result":null,"extra":null}\n',
        b'{"v":1,"id":"request-id","ok":false}\n',
        b'{"v":1,"id":"request-id","ok":false,"error":{},"result":null}\n',
        b'{"v":1,"id":"request-id","ok":false,"error":{"type":"ValueError"}}\n',
        b'{"v":1,"id":"request-id","ok":false,'
        b'"error":{"type":"ValueError","message":"bad","extra":null}}\n',
        b'{"v":1,"id":"request-id","ok":false,'
        b'"error":{"type":1,"message":"bad"}}\n',
        b'{"v":1,"id":"request-id","ok":false,'
        b'"error":{"type":"ValueError","message":null}}\n',
    ],
)
def test_runtime_response_rejects_malformed_envelopes(
    runtime_dir,
    monkeypatch,
    response,
):
    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed") as caught:
        _runtime_response(client, monkeypatch, response)

    assert isinstance(caught.value.__cause__, LeanRuntimeProtocolError)


@pytest.mark.parametrize(
    "response",
    [
        b'{"v":1,"v":1,"id":"request-id","ok":true,"result":null}\n',
        b'{"v":1,"id":"request-id","ok":true,"result":NaN}\n',
        b'{"v":1,"id":"request-id","ok":true,"result":1e100000}\n',
        b'{"v":1,"id":"request-id","ok":true,"result":'
        + b"9" * 4_301
        + b"}\n",
        b'{"v":1,"id":"request-id","ok":true,"result":'
        + b"[" * 2_000
        + b"0"
        + b"]" * 2_000
        + b"}\n",
    ],
)
def test_runtime_response_rejects_noncanonical_json(
    runtime_dir,
    monkeypatch,
    response,
):
    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed") as caught:
        _runtime_response(client, monkeypatch, response)

    assert isinstance(caught.value.__cause__, LeanRuntimeProtocolError)


def test_runtime_response_rejects_invalid_utf8_as_unknown_outcome(
    runtime_dir,
    monkeypatch,
):
    client = LeanRuntimeClient(socket_path=runtime_dir / "fake.sock")

    with pytest.raises(LeanRuntimeOutcomeUnknown, match="must not be replayed") as caught:
        _runtime_response(client, monkeypatch, b"\xff\n")

    assert isinstance(caught.value.__cause__, LeanRuntimeProtocolError)


def test_default_repl_command_uses_the_declared_dependency(monkeypatch):
    monkeypatch.delenv("LEAN_REPL_CMD", raising=False)

    assert LeanRuntimeConfig.from_environment().repl_command == (
        "lake",
        "exe",
        "@repl/repl",
    )


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("LEAN_NUM_REPLS", "-1", "nonnegative integer"),
        ("LEAN_REPL_CMD", "   ", "must not be empty"),
        ("AUTOFORM_LEAN_IDLE_SECONDS", "nan", "finite nonnegative"),
        ("LEAN_LSP_TIMEOUT", "601", "cannot exceed"),
        ("AUTOFORM_RUNTIME_RESPONSE_TIMEOUT", "100", "too small"),
    ],
)
def test_invalid_node_configuration_fails_fast(monkeypatch, name, value, match):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=match):
        LeanRuntimeConfig.from_environment()


def test_per_project_workers_cannot_exceed_node_budget(monkeypatch):
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_REPL_WORKERS_PER_PROJECT", "2")
    with pytest.raises(ValueError, match="cannot exceed"):
        LeanRuntimeConfig.from_environment()


def test_response_budget_does_not_scale_with_cold_repl_pool_size(monkeypatch):
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "3")
    monkeypatch.setenv("AUTOFORM_REPL_WORKERS_PER_PROJECT", "3")
    monkeypatch.setenv("AUTOFORM_RUNTIME_RESPONSE_TIMEOUT", "860")

    config = LeanRuntimeConfig.from_environment()

    assert config.repl_workers_per_project == 3
    assert config.response_timeout == 860


def test_client_repl_response_grace_matches_daemon_cleanup_reserve():
    from servers import lean_client, lean_runtime
    from servers.repl.pool import DEFAULT_POOL_CLEANUP_SECONDS

    assert lean_client.REPL_RESPONSE_GRACE_SECONDS == (
        DEFAULT_POOL_CLEANUP_SECONDS + lean_runtime.RUNTIME_SAFETY_SECONDS
    )


def test_response_budget_must_leave_room_for_repl_cleanup(monkeypatch):
    monkeypatch.setenv("AUTOFORM_RUNTIME_RESPONSE_TIMEOUT", "272")
    monkeypatch.setenv("LEAN_LSP_TIMEOUT", "1")
    monkeypatch.setenv("AUTOFORM_MAX_LSP_REQUEST_SECONDS", "1")

    with pytest.raises(ValueError, match="REPL request and cleanup limits"):
        LeanRuntimeConfig.from_environment()


@pytest.mark.parametrize("timeout", [-1, 0, True, float("nan"), float("inf"), 241])
def test_invalid_repl_timeout_never_warms_a_pool(tmp_path, timeout):
    project = make_lake_project(tmp_path, "timeout")
    pools = []
    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=lambda root: pools.append(FakePool(root)) or pools[-1],
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        with pytest.raises(ValueError, match="timeout"):
            services.dispatch(
                "repl.run",
                {"project_dir": str(project), "code": "#check Nat", "timeout": timeout},
            )
        assert pools == []
    finally:
        services.close()


def test_failed_lsp_session_is_replaced_on_the_next_call(tmp_path):
    from servers.lsp.server import LspProtocolError

    project = make_lake_project(tmp_path, "lsp-restart")
    source = project / "Main.lean"
    source.write_text("#check Nat\n")
    sessions = []

    class Session(FakeLsp):
        def __init__(self, root):
            super().__init__(root)
            self.number = len(sessions) + 1
            self.alive = True
            sessions.append(self)

        def is_alive(self):
            return self.alive and not self.closed

        def get_diagnostics(self, file_path):
            if self.number == 1:
                self.alive = False
                raise LspProtocolError("broken shared stream")
            return []

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=Session,
        start_sweepers=False,
    )
    try:
        params = {"project_dir": str(project), "file_path": "Main.lean"}
        with pytest.raises(LspProtocolError, match="broken shared stream"):
            services.dispatch("lsp.diagnostics", params)
        assert services.dispatch("lsp.diagnostics", params).startswith("No diagnostics")
        assert len(sessions) == 2
        assert sessions[0].closed is True
    finally:
        services.close()


def test_failed_lsp_session_is_invalidated_before_abort_cleanup(tmp_path, monkeypatch):
    from servers.lsp.server import LspProtocolError

    project = make_lake_project(tmp_path, "lsp-abort-order")
    source = project / "Main.lean"
    source.write_text("#check Nat\n")
    events = []

    class Session(FakeLsp):
        def get_diagnostics(self, file_path):
            raise LspProtocolError("broken shared stream")

        def abort(self):
            events.append("abort")
            raise RuntimeError("cleanup failed")

        def close(self):
            events.append("close")
            self.closed = True

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=Session,
        start_sweepers=False,
    )
    original_invalidate = services.lsp_projects.invalidate

    def invalidate(project_dir, session):
        events.append("invalidate")
        original_invalidate(project_dir, session)

    monkeypatch.setattr(services.lsp_projects, "invalidate", invalidate)
    try:
        params = {"project_dir": str(project), "file_path": "Main.lean"}
        with pytest.raises(RuntimeError, match="cleanup failed"):
            services.dispatch("lsp.diagnostics", params)
        assert events[:3] == ["invalidate", "abort", "close"]
        assert services.lsp_projects.state(str(project)) == "cold"
    finally:
        services.close()
