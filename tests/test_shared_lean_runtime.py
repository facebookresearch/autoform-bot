"""Sharing, lifecycle, and resource-boundary tests for the Lean runtime."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import json
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from servers import lean_client, lean_project_fingerprint
from servers import lean_runtime as lean_runtime_module
from servers.lean_client import (
    INSTALL_PATH_ID,
    PROTOCOL_VERSION,
    LeanRuntimeClient,
    LeanRuntimeError,
    LeanRuntimeProtocolError,
    LeanRuntimeUnavailable,
)
from servers.lean_runtime import (
    LeanRuntimeConfig,
    LeanRuntimeServer,
    LeanRuntimeServices,
    ProjectResourceCache,
    ProjectResourceBusyError,
)


def test_runtime_identity_tracks_all_behavior_affecting_modules():
    relative_paths = [
        path.relative_to(lean_client.PACKAGE_ROOT).as_posix()
        for path in lean_client._RUNTIME_FILES
    ]

    assert relative_paths[:2] == ["pyproject.toml", "uv.lock"]
    assert relative_paths[2:] == sorted(
        path.relative_to(lean_client.PACKAGE_ROOT).as_posix()
        for path in (lean_client.PACKAGE_ROOT / "servers").rglob("*.py")
    )


def test_default_runtime_ownership_paths_are_protocol_independent(runtime_dir, monkeypatch):
    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))

    paths = lean_client.default_runtime_paths()

    assert paths.lock.name == f"lean-{INSTALL_PATH_ID}.lock"
    assert paths.lifetime_lock.name == f"lean-{INSTALL_PATH_ID}.lifetime.lock"
    assert f"lean-v{PROTOCOL_VERSION}-" in paths.socket.name


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
        "repl_command": ("lake", "exe", "repl"),
        "lsp_command": ("lake", "serve"),
        "lsp_timeout": 60.0,
        "max_lsp_request_seconds": 600.0,
        "repl_request_timeout": 180.0,
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
        assert all(set(call[1]) == {"deadline"} for call in pools[0].calls)
        assert pools[0].calls[0][1]["deadline"] > time.monotonic()
        assert pools[0].calls[1][1]["deadline"] > time.monotonic()
        warm = services.dispatch("repl.status", {"project_dir": str(project)})
        assert warm["state"] == "warm"
        assert warm["memory_usage_gb"] == 0.25
    finally:
        services.close()
    assert pools[0]._shutdown is True


def test_repl_status_observes_a_poisoned_pool_without_cleaning_it(tmp_path):
    project = make_lake_project(tmp_path, "poisoned-status")
    pools = []

    class PoisonablePool(FakePool):
        def __init__(self, root):
            super().__init__(root)
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1
            super().shutdown()

    def create_pool(root):
        pool = PoisonablePool(root)
        pools.append(pool)
        return pool

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=create_pool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#check Nat", "timeout": None},
        )
        pools[0]._shutdown = True

        status = services.dispatch("repl.status", {"project_dir": str(project)})

        assert status["state"] == "warm"
        assert status["shutdown"] is True
        assert pools[0].shutdown_calls == 0
        assert len(pools) == 1
    finally:
        services.close()


def test_runtime_formats_unknown_repl_outcomes_without_hiding_them(tmp_path):
    project = make_lake_project(tmp_path, "unknown-outcome")

    class UnknownPool(FakePool):
        def run(self, code, **kwargs):
            return {
                "repl_error": "response frame was malformed",
                "outcome_unknown": True,
            }

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=UnknownPool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        result = services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#eval 1", "timeout": 3},
        )
    finally:
        services.close()

    assert result == (
        "REPL error (execution outcome unknown; request not retried): "
        "response frame was malformed"
    )


def test_requested_timeout_covers_project_slot_admission(tmp_path):
    first = make_lake_project(tmp_path, "deadline-first")
    second = make_lake_project(tmp_path, "deadline-second")
    pools = []
    services = LeanRuntimeServices(
        runtime_config(repl_project_limit=1),
        repl_factory=lambda root: pools.append(FakePool(root)) or pools[-1],
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        with services.repl_projects.lease(str(first)):
            started = time.monotonic()
            with pytest.raises(ProjectResourceBusyError, match="response budget"):
                services.dispatch(
                    "repl.run",
                    {
                        "project_dir": str(second),
                        "code": "#check Nat",
                        "timeout": 0.01,
                    },
                )
            assert time.monotonic() - started < 0.5
    finally:
        services.close()

    assert len(pools) == 1


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


def test_root_replacement_invalidates_a_warm_project_resource(tmp_path):
    project = make_lake_project(tmp_path, "replace-root")
    created = []
    closed = []

    def factory(root):
        resource = object()
        created.append((root, resource))
        return resource

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)) as first:
        pass

    moved = project.with_name("replaced-root")
    project.rename(moved)
    project.mkdir()
    (project / "lakefile.toml").write_bytes((moved / "lakefile.toml").read_bytes())

    with cache.lease(str(project)) as second:
        assert second is not first

    assert len(created) == 2
    assert closed == [first]
    cache.close()
    assert closed == [first, second]


def test_root_replacement_during_startup_discards_the_resource(tmp_path):
    project = make_lake_project(tmp_path, "replace-during-startup")
    moved = project.with_name("startup-original")
    resource = object()
    closed = []

    def factory(root):
        root.rename(moved)
        root.mkdir()
        (root / "lakefile.toml").write_bytes(
            (moved / "lakefile.toml").read_bytes()
        )
        return resource

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with pytest.raises(ProjectResourceBusyError, match="changed during startup"):
        with cache.lease(str(project)):
            pytest.fail("a resource bound to the replaced root must not be leased")

    cache.close()
    assert closed == [resource]
    assert cache.state(str(project)) == "cold"


def test_factory_created_derived_directory_does_not_invalidate_startup(tmp_path):
    project = make_lake_project(tmp_path, "derived-during-startup")
    resource = object()
    closed = []

    def factory(root):
        (root / ".lake").mkdir()
        return resource

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with cache.lease(str(project)) as leased:
        assert leased is resource

    assert closed == []
    cache.close()
    assert closed == [resource]


def test_continuous_fingerprint_churn_honors_the_acquisition_deadline(
    tmp_path, monkeypatch
):
    project = make_lake_project(tmp_path, "fingerprint-churn")
    created = []
    cache = ProjectResourceCache(
        lambda root: created.append(root) or root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)):
        pass
    stable = cache._entries[project.resolve()].fingerprint
    fingerprints = iter((stable, object()) * 100_000)
    monkeypatch.setattr(
        lean_runtime_module,
        "lean_project_fingerprint",
        lambda root: next(fingerprints),
    )

    started = time.monotonic()
    with pytest.raises(ProjectResourceBusyError, match="timed out waiting"):
        with cache.lease(
            str(project),
            acquisition_timeout=0.05,
            creation_budget=0,
        ):
            pytest.fail("unstable project fingerprint reached the request")

    assert time.monotonic() - started < 0.5
    assert len(created) == 1
    cache.close()


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


def test_cache_close_forces_cleanup_then_waits_for_an_active_lease(tmp_path):
    project = make_lake_project(tmp_path, "active-close")
    leased = threading.Event()
    release = threading.Event()
    cleanup_started = threading.Event()
    closed = []
    errors = []

    def close_resource(resource):
        closed.append(resource)
        cleanup_started.set()

    cache = ProjectResourceCache(
        lambda root: root,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    def hold_lease():
        try:
            with cache.lease(str(project)):
                leased.set()
                release.wait(timeout=2)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=hold_lease)
    thread.start()
    assert leased.wait(timeout=1)
    cache_closed = threading.Event()

    def close_cache():
        cache.close(timeout=0.05)
        cache_closed.set()

    closer = threading.Thread(target=close_cache)
    closer.start()
    try:
        assert cleanup_started.wait(timeout=1)
        assert not cache_closed.wait(timeout=0.1)
        assert closed == [project.resolve()]
    finally:
        release.set()
        thread.join(timeout=2)
        closer.join(timeout=2)

    assert not thread.is_alive()
    assert not closer.is_alive()
    assert cache_closed.is_set()
    assert errors == []


def test_cache_close_retains_ownership_until_resource_cleanup_finishes(tmp_path):
    project = make_lake_project(tmp_path, "blocked-cleanup")
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def close_resource(resource):
        cleanup_started.set()
        release_cleanup.wait(timeout=2)

    cache = ProjectResourceCache(
        lambda root: root,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)):
        pass

    closed = threading.Event()

    def close_cache():
        cache.close(timeout=0.05)
        closed.set()

    thread = threading.Thread(target=close_cache)
    thread.start()
    try:
        assert cleanup_started.wait(timeout=1)
        assert not closed.wait(timeout=0.1)
    finally:
        release_cleanup.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert closed.is_set()


def test_concurrent_cache_close_waits_for_single_owned_cleanup(tmp_path):
    project = make_lake_project(tmp_path, "concurrent-close")
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    second_started = threading.Event()
    close_calls = []

    def close_resource(resource):
        close_calls.append(resource)
        cleanup_started.set()
        release_cleanup.wait(timeout=2)

    cache = ProjectResourceCache(
        lambda root: root,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)):
        pass

    first = threading.Thread(target=cache.close, kwargs={"timeout": 0.05})

    def close_second():
        second_started.set()
        cache.close(timeout=0.05)

    second = threading.Thread(target=close_second)
    first.start()
    assert cleanup_started.wait(timeout=1)
    second.start()
    assert second_started.wait(timeout=1)
    release_cleanup.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert close_calls == [project.resolve()]


def test_cache_close_with_sweeper_honors_a_timeout(tmp_path):
    project = make_lake_project(tmp_path, "sweeper-close")
    closed = []
    cache = ProjectResourceCache(
        lambda root: root,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
    )
    with cache.lease(str(project)):
        pass

    cache.close(timeout=0.05)

    assert closed == [project.resolve()]


def test_cache_close_waits_for_inflight_startup_cleanup(tmp_path):
    project = make_lake_project(tmp_path, "startup-close")
    startup_started = threading.Event()
    release_startup = threading.Event()
    cleanup_finished = threading.Event()
    caller_errors = []

    def factory(root):
        startup_started.set()
        release_startup.wait(timeout=2)
        return root

    def close_resource(resource):
        cleanup_finished.set()

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    def acquire():
        try:
            with cache.lease(str(project)):
                pytest.fail("startup completed after cache shutdown")
        except RuntimeError as error:
            caller_errors.append(error)

    caller = threading.Thread(target=acquire)
    caller.start()
    assert startup_started.wait(timeout=1)

    cache_closed = threading.Event()

    def close_cache():
        cache.close(timeout=0.05)
        cache_closed.set()

    closer = threading.Thread(target=close_cache)
    closer.start()
    try:
        assert not cache_closed.wait(timeout=0.1)
    finally:
        release_startup.set()
        caller.join(timeout=2)
        closer.join(timeout=2)

    assert not caller.is_alive()
    assert not closer.is_alive()
    assert cache_closed.is_set()
    assert cleanup_finished.is_set()
    assert len(caller_errors) == 1
    assert "closed during startup" in str(caller_errors[0])


def test_cold_startup_returns_at_the_acquisition_deadline(tmp_path):
    project = make_lake_project(tmp_path, "deadline-startup")
    startup_started = threading.Event()
    release_startup = threading.Event()
    cleanup_finished = threading.Event()

    def factory(root):
        startup_started.set()
        release_startup.wait(timeout=2)
        return root

    cache = ProjectResourceCache(
        factory,
        lambda resource: cleanup_finished.set(),
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    started = time.monotonic()
    with pytest.raises(ProjectResourceBusyError, match="startup exceeded"):
        with cache.lease(
            str(project),
            acquisition_timeout=0.05,
            creation_budget=0,
        ):
            pytest.fail("late startup reached the request")
    assert time.monotonic() - started < 0.5
    assert startup_started.is_set()

    release_startup.set()
    deadline = time.monotonic() + 2
    while cache.state(str(project)) != "warm" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache.state(str(project)) == "warm"
    assert not cleanup_finished.is_set()
    with cache.lease(str(project), acquisition_timeout=0.1) as resource:
        assert resource == project.resolve()
    cache.close()
    assert cleanup_finished.is_set()


def test_stale_victim_cleanup_crossing_budget_does_not_start_replacement(tmp_path):
    project = make_lake_project(tmp_path, "stale-budget")
    created = []
    closed = []
    cache = ProjectResourceCache(
        lambda root: created.append(object()) or created[-1],
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
        clock=lambda: 0.0,
    )
    with cache.lease(str(project)) as first:
        pass
    cache.invalidate(str(project), first)

    ticks = iter((0.0, 0.0, 0.0, 1.0))
    cache._clock = lambda: next(ticks, 1.0)
    with pytest.raises(ProjectResourceBusyError, match="startup exceeded"):
        with cache.lease(
            str(project),
            acquisition_timeout=1.0,
            creation_budget=0.5,
        ):
            pytest.fail("late startup reached the request")

    cache.close()
    assert len(created) == 1
    assert closed == created


def test_victim_cleanup_timeout_never_starts_replacement(tmp_path):
    first = make_lake_project(tmp_path, "cleanup-first")
    second = make_lake_project(tmp_path, "cleanup-second")
    created = []
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_finished = threading.Event()

    def factory(root):
        created.append(root)
        return root

    def close_resource(resource):
        cleanup_started.set()
        release_cleanup.wait(timeout=2)
        cleanup_finished.set()

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    started = time.monotonic()
    with pytest.raises(ProjectResourceBusyError, match="closing a displaced"):
        with cache.lease(str(second), deadline=time.monotonic() + 0.05):
            pytest.fail("replacement must not start after victim cleanup times out")
    assert time.monotonic() - started < 0.5
    assert cleanup_started.is_set()
    assert created == [first.resolve()]

    release_cleanup.set()
    assert cleanup_finished.wait(timeout=2)
    deadline = time.monotonic() + 2
    while cache.state(str(second)) != "cold" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cache.state(str(second)) == "cold"
    cache.close()


def test_cache_close_during_victim_cleanup_prevents_replacement_startup(tmp_path):
    first = make_lake_project(tmp_path, "closing-first")
    second = make_lake_project(tmp_path, "closing-second")
    created = []
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    replacement_started = threading.Event()
    errors = []

    def factory(root):
        created.append(root)
        if root == second.resolve():
            replacement_started.set()
        return root

    def close_resource(resource):
        if resource == first.resolve():
            cleanup_started.set()
            release_cleanup.wait(timeout=2)

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    def acquire_replacement():
        try:
            with cache.lease(str(second), deadline=time.monotonic() + 2):
                pytest.fail("replacement reached the caller during shutdown")
        except BaseException as error:
            errors.append(error)

    acquisition = threading.Thread(target=acquire_replacement)
    acquisition.start()
    assert cleanup_started.wait(timeout=1)

    closed = threading.Event()
    closer = threading.Thread(
        target=lambda: (cache.close(timeout=0), closed.set())
    )
    closer.start()
    try:
        assert not closed.wait(timeout=0.1)
        release_cleanup.set()
    finally:
        acquisition.join(timeout=2)
        closer.join(timeout=2)

    assert not acquisition.is_alive()
    assert not closer.is_alive()
    assert closed.is_set()
    assert not replacement_started.is_set()
    assert created == [first.resolve()]
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "closed during startup" in str(errors[0])


def test_project_change_during_victim_cleanup_blocks_factory(tmp_path):
    first = make_lake_project(tmp_path, "mutation-first")
    second = make_lake_project(tmp_path, "mutation-second")
    created = []

    def factory(root):
        created.append(root)
        return root

    def close_resource(resource):
        (second / "lakefile.toml").write_text(
            'name = "ChangedDuringCleanup"\n',
            encoding="utf-8",
        )

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass
    fingerprint = lean_project_fingerprint(second.resolve())

    with pytest.raises(ProjectResourceBusyError, match="changed before startup"):
        with cache.lease(
            str(second),
            deadline=time.monotonic() + 1,
            required_fingerprint=fingerprint,
        ):
            pytest.fail("changed project must not reach replacement factory")

    assert created == [first.resolve()]
    cache.close()


def test_project_startup_that_misses_its_budget_is_published_for_later_callers(
    tmp_path,
):
    project = make_lake_project(tmp_path, "slow-startup")
    clock = {"now": 0.0}
    closed = []
    cleanup_finished = threading.Event()

    def slow_factory(root):
        clock["now"] = 11.0
        return root

    def close_resource(resource):
        closed.append(resource)
        cleanup_finished.set()

    cache = ProjectResourceCache(
        slow_factory,
        close_resource,
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

    assert cache.state(str(project)) == "warm"
    assert not cleanup_finished.is_set()
    with cache.lease(str(project)) as resource:
        assert resource == project.resolve()
    cache.close()
    assert cleanup_finished.is_set()
    assert closed == [project.resolve()]


def test_boundary_timeout_publishes_without_cleanup_on_the_request_thread(
    tmp_path, monkeypatch
):
    project = make_lake_project(tmp_path, "boundary-timeout")
    factory_released = threading.Event()
    future_completed = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    class BoundaryFuture(Future):
        def __init__(self):
            super().__init__()
            self._reported_not_done = False

        def result(self, timeout=None):
            if timeout is not None:
                factory_released.set()
                assert future_completed.wait(timeout=1)
                raise FutureTimeoutError
            return super().result(timeout=timeout)

        def done(self):
            if not self._reported_not_done:
                self._reported_not_done = True
                return False
            return super().done()

        def set_result(self, result):
            super().set_result(result)
            future_completed.set()

    def factory(root):
        assert factory_released.wait(timeout=1)
        return root

    def close_resource(resource):
        cleanup_started.set()
        release_cleanup.wait(timeout=2)

    monkeypatch.setattr(lean_runtime_module, "Future", BoundaryFuture)
    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    started = time.monotonic()
    with pytest.raises(ProjectResourceBusyError, match="startup exceeded"):
        with cache.lease(
            str(project),
            acquisition_timeout=0.05,
            creation_budget=0,
        ):
            pytest.fail("late project startup reached the request")
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert cache.state(str(project)) == "warm"
    assert not cleanup_started.is_set()
    release_cleanup.set()
    cache.close()
    assert cleanup_started.is_set()


def test_expired_startup_publish_does_not_extend_the_response_deadline(tmp_path):
    project = make_lake_project(tmp_path, "expired-cleanup")
    clock = {"now": 0.0}
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def factory(root):
        clock["now"] = 2.0
        return root

    def close_resource(resource):
        cleanup_started.set()
        release_cleanup.wait(timeout=2)

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )

    started = time.monotonic()
    with pytest.raises(ProjectResourceBusyError, match="startup exceeded"):
        with cache.lease(
            str(project),
            acquisition_timeout=1.0,
            creation_budget=0,
        ):
            pytest.fail("expired project startup reached the request")
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert cache.state(str(project)) == "warm"
    assert not cleanup_started.is_set()
    release_cleanup.set()
    cache.close()
    assert cleanup_started.is_set()


def test_factory_timeout_is_not_misreported_as_acquisition_timeout(tmp_path):
    project = make_lake_project(tmp_path, "factory-timeout")

    def fail_factory(root):
        raise TimeoutError("factory timed out early")

    cache = ProjectResourceCache(
        fail_factory,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with pytest.raises(TimeoutError, match="factory timed out early"):
        with cache.lease(
            str(project),
            acquisition_timeout=1,
            creation_budget=0,
        ):
            pytest.fail("failed startup reached the request")


def test_project_startup_past_an_absolute_deadline_is_reused(tmp_path):
    project = make_lake_project(tmp_path, "late-startup")
    clock = {"now": 0.0}
    closed = []
    cleanup_finished = threading.Event()

    def slow_factory(root):
        clock["now"] = 2.0
        return root

    def close_resource(resource):
        closed.append(resource)
        cleanup_finished.set()

    cache = ProjectResourceCache(
        slow_factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )

    with pytest.raises(ProjectResourceBusyError, match="startup exceeded"):
        with cache.lease(str(project), deadline=1.0):
            pytest.fail("a worker created after the caller deadline reached that caller")

    assert cache.state(str(project)) == "warm"
    assert not cleanup_finished.is_set()
    with cache.lease(str(project)) as resource:
        assert resource == project.resolve()
    cache.close()
    assert cleanup_finished.is_set()
    assert closed == [project.resolve()]


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


def test_idle_eviction_reserves_its_slot_until_cleanup_finishes(tmp_path):
    first = make_lake_project(tmp_path, "idle-first")
    second = make_lake_project(tmp_path, "idle-second")
    clock = {"now": 0.0}
    created = []
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    second_started = threading.Event()
    errors = []

    def factory(root):
        created.append(root)
        if root == second.resolve():
            second_started.set()
        return root

    def close_resource(resource):
        if resource == first.resolve():
            cleanup_started.set()
            release_cleanup.wait(timeout=2)

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=10,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )
    with cache.lease(str(first)):
        pass
    clock["now"] = 20.0

    evicted = []
    eviction = threading.Thread(target=lambda: evicted.append(cache.evict_idle()))

    def lease_second():
        try:
            with cache.lease(str(second)):
                pass
        except BaseException as error:
            errors.append(error)

    acquisition = threading.Thread(target=lease_second)
    eviction.start()
    assert cleanup_started.wait(timeout=1)
    acquisition.start()
    try:
        assert str(first.resolve()) in cache.stats()["creating"]
        assert not second_started.wait(timeout=0.1)
    finally:
        release_cleanup.set()
        eviction.join(timeout=2)
        acquisition.join(timeout=2)

    assert not eviction.is_alive()
    assert not acquisition.is_alive()
    assert evicted == [1]
    assert second_started.is_set()
    assert errors == []
    cache.close()


def test_failed_victim_cleanup_blocks_replacement_until_retry_succeeds(tmp_path):
    first = make_lake_project(tmp_path, "retry-cleanup-first")
    second = make_lake_project(tmp_path, "retry-cleanup-second")
    cleanup_failed = threading.Event()
    allow_retry = threading.Event()
    second_started = threading.Event()
    errors = []
    close_attempts = 0

    def factory(root):
        if root == second.resolve():
            second_started.set()
        return root

    def close_resource(resource):
        nonlocal close_attempts
        if resource != first.resolve():
            return
        close_attempts += 1
        if close_attempts == 1:
            cleanup_failed.set()
            raise RuntimeError("transient cleanup failure")
        assert allow_retry.wait(timeout=2)

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    def lease_second():
        try:
            with cache.lease(str(second)):
                pass
        except BaseException as error:
            errors.append(error)

    acquisition = threading.Thread(target=lease_second)
    acquisition.start()
    try:
        assert cleanup_failed.wait(timeout=1)
        assert not second_started.wait(timeout=0.1)
    finally:
        allow_retry.set()
        acquisition.join(timeout=2)

    assert not acquisition.is_alive()
    assert close_attempts == 2
    assert second_started.is_set()
    assert errors == []
    cache.close()


def test_victim_cleanup_thread_start_failure_closes_synchronously(
    tmp_path, monkeypatch
):
    first = make_lake_project(tmp_path, "thread-failure-first")
    second = make_lake_project(tmp_path, "thread-failure-second")
    created = []
    closed = []
    cache = ProjectResourceCache(
        lambda root: created.append(root) or root,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass

    start_thread = threading.Thread.start

    def fail_victim_cleanup(thread):
        if thread.name == "autoform-project-victim-cleanup":
            raise RuntimeError("cannot start victim cleanup")
        return start_thread(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_victim_cleanup)
    with pytest.raises(RuntimeError, match="cannot start victim cleanup"):
        with cache.lease(str(second), deadline=time.monotonic() + 1):
            pytest.fail("a replacement must not start after cleanup startup fails")

    assert created == [first.resolve()]
    assert closed == [first.resolve()]
    assert cache.stats()["creating"] == []
    assert cache.state(str(second)) == "cold"
    cache.close()


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
            {
                "project_dir": "/lean",
                "code": "#check Nat",
                "timeout": None,
            },
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


@pytest.mark.daemon
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


@pytest.mark.daemon
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


@pytest.mark.daemon
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


@pytest.mark.daemon
def test_new_build_replaces_previous_runtime_at_same_install_path(runtime_dir, monkeypatch):
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
    finally:
        current.stop()


@pytest.mark.daemon
def test_new_protocol_retires_old_protocol_before_owning_runtime(
    runtime_dir,
    repo_root,
    monkeypatch,
    request,
):
    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    current = LeanRuntimeClient(startup_timeout=15)
    old_protocol = PROTOCOL_VERSION - 1
    assert old_protocol >= 0
    old_socket = runtime_dir / f"lean-v{old_protocol}-{INSTALL_PATH_ID}-old.sock"
    draining = runtime_dir / "old-draining"
    release = runtime_dir / "release-old"
    old_script = """
import fcntl
import json
import os
import socket
import sys
import time
from pathlib import Path

socket_path = Path(sys.argv[1])
lifetime_path = Path(sys.argv[2])
draining_path = Path(sys.argv[3])
release_path = Path(sys.argv[4])
protocol = int(sys.argv[5])
lifetime_fd = os.open(lifetime_path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lifetime_fd, fcntl.LOCK_EX)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(str(socket_path))
server.listen()
try:
    while True:
        connection, _ = server.accept()
        with connection:
            request = json.loads(connection.makefile("rb").readline())
            assert request["v"] == protocol
            stopping = request["method"] == "daemon.shutdown"
            result = (
                {"stopping": True, "pid": os.getpid()}
                if stopping
                else {
                    "pid": os.getpid(),
                    "protocol": protocol,
                    "install_id": sys.argv[6],
                    "build_generation": 0,
                }
            )
            connection.sendall(
                json.dumps(
                    {"v": protocol, "id": request["id"], "ok": True, "result": result}
                ).encode()
                + b"\\n"
            )
        if stopping:
            server.close()
            socket_path.unlink()
            draining_path.touch()
            while not release_path.exists():
                time.sleep(0.01)
            break
finally:
    server.close()
    socket_path.unlink(missing_ok=True)
    os.close(lifetime_fd)
"""
    old_process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            old_script,
            str(old_socket),
            str(current.paths.lifetime_lock),
            str(draining),
            str(release),
            str(old_protocol),
            f"{INSTALL_PATH_ID}-old",
        ],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def stop_old_process():
        release.touch(exist_ok=True)
        if old_process.poll() is None:
            old_process.terminate()
            old_process.wait(timeout=5)

    request.addfinalizer(stop_old_process)
    old_client = LeanRuntimeClient(socket_path=old_socket, startup_timeout=15)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if old_process.poll() is not None:
            pytest.fail(f"old protocol runtime exited with {old_process.returncode}")
        try:
            old_status = old_client._request_once(
                "daemon.ping",
                {},
                protocol_version=old_protocol,
            )
            break
        except LeanRuntimeUnavailable:
            time.sleep(0.025)
    else:
        pytest.fail("old protocol runtime did not become ready")

    statuses = []
    errors = []

    def start_current():
        try:
            statuses.append(current.ensure_running())
        except BaseException as error:
            errors.append(error)

    starter = threading.Thread(target=start_current)
    try:
        starter.start()
        deadline = time.monotonic() + 10
        while not draining.exists() and time.monotonic() < deadline:
            time.sleep(0.025)
        assert draining.exists()
        starter.join(timeout=0.1)
        assert starter.is_alive()
        assert old_process.poll() is None
        assert not current.paths.socket.exists()

        release.touch()
        starter.join(timeout=20)
        assert not starter.is_alive()
        assert errors == []
        assert len(statuses) == 1
        current_status = statuses[0]
        assert current_status["protocol"] == PROTOCOL_VERSION
        assert current_status["pid"] != old_status["pid"]
        assert old_process.wait(timeout=5) == 0

        import fcntl

        lifetime_fd = os.open(current.paths.lifetime_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lifetime_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lifetime_fd)
    finally:
        release.touch(exist_ok=True)
        starter.join(timeout=5)
        try:
            current.stop()
        except LeanRuntimeUnavailable:
            pass
        if old_process.poll() is None:
            old_process.terminate()
            old_process.wait(timeout=5)


@pytest.mark.daemon
def test_new_protocol_removes_stale_old_protocol_socket(runtime_dir, monkeypatch):
    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    stale_path = runtime_dir / f"lean-v0-{INSTALL_PATH_ID}-stale.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(stale_path))
    stale.close()

    current = LeanRuntimeClient(startup_timeout=15)
    try:
        current.ensure_running()
        assert not stale_path.exists()
    finally:
        current.stop()


def test_newer_protocol_socket_fails_closed(runtime_dir, monkeypatch):
    monkeypatch.setenv("AUTOFORM_RUNTIME_DIR", str(runtime_dir))
    newer_path = runtime_dir / f"lean-v{PROTOCOL_VERSION + 1}-{INSTALL_PATH_ID}-new.sock"
    newer_path.touch()
    current = LeanRuntimeClient(startup_timeout=0.1)

    with pytest.raises(LeanRuntimeProtocolError, match="newer than supported"):
        current.ensure_running()

    assert newer_path.is_file()


@pytest.mark.daemon
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


@pytest.mark.daemon
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


def test_server_close_waits_for_an_admitted_request(runtime_dir):
    entered = threading.Event()
    release = threading.Event()

    class BlockingServices:
        config = SimpleNamespace(max_connections=2, rpc_read_timeout=1.0)

        def dispatch(self, method, params):
            entered.set()
            assert release.wait(timeout=2)
            return {"finished": True}

    socket_path = runtime_dir / "drain.sock"
    server = LeanRuntimeServer(socket_path, BlockingServices())
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.sendall(
        b'{"v":1,"id":"slow","method":"repl.run","params":{}}\n'
    )
    assert entered.wait(timeout=1)

    server.shutdown()
    closing = threading.Thread(target=server.server_close)
    closing.start()
    try:
        closing.join(timeout=0.05)
        assert closing.is_alive()
        release.set()
        response = b""
        while not response.endswith(b"\n"):
            response += client.recv(4096)
        assert json.loads(response) == {
            "v": 1,
            "id": "slow",
            "ok": True,
            "result": {"finished": True},
        }
        closing.join(timeout=1)
        serving.join(timeout=1)
        assert not closing.is_alive()
        assert not serving.is_alive()
    finally:
        release.set()
        client.close()
        server.server_close()
        serving.join(timeout=1)


def test_stop_uses_the_configured_response_deadline(runtime_dir, monkeypatch):
    client = LeanRuntimeClient(
        socket_path=runtime_dir / "bounded-stop.sock",
        response_timeout=0.05,
    )
    observed = []

    def request(method, params=None, *, autostart=None, response_timeout=None):
        observed.append((method, autostart, response_timeout))
        return {"stopping": True}

    monkeypatch.setattr(client, "request", request)
    assert client.stop() == {"stopping": True}
    assert observed == [("daemon.shutdown", False, 0.05)]


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

    with pytest.raises(LeanRuntimeError, match="after request dispatch"):
        client.request("repl.run", {"project_dir": "/lean", "code": "#check Nat"})


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


def test_repl_response_budget_uses_the_end_to_end_request_limit(monkeypatch):
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "4")
    monkeypatch.setenv("AUTOFORM_REPL_WORKERS_PER_PROJECT", "4")
    monkeypatch.setenv("AUTOFORM_RUNTIME_RESPONSE_TIMEOUT", "300")
    monkeypatch.setenv("AUTOFORM_MAX_LSP_REQUEST_SECONDS", "60")

    config = LeanRuntimeConfig.from_environment()

    assert config.repl_workers_per_project == 4
    assert config.response_timeout == 300


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
    assert pools == []


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


@pytest.mark.parametrize("method", ["lsp.diagnostics", "lsp.hover"])
@pytest.mark.parametrize("changed_file", ["lakefile.toml", "Main.lean"])
def test_lsp_result_is_rejected_when_project_changes_during_operation(
    tmp_path, method, changed_file
):
    project = make_lake_project(tmp_path, f"changed-{method.rsplit('.', 1)[1]}")
    source = project / "Main.lean"
    source.write_text("#check Nat\n")
    sessions = []

    class Session(FakeLsp):
        def __init__(self, root):
            super().__init__(root)
            self.number = len(sessions) + 1
            sessions.append(self)

        def _result(self):
            if self.number == 1:
                target = project / changed_file
                original_mtime = target.stat().st_mtime_ns
                replacement = project / f"{changed_file}.replacement"
                replacement.write_text(target.read_text() + "-- changed\n")
                os.utime(replacement, ns=(original_mtime, original_mtime))
                replacement.replace(target)

        def get_diagnostics(self, file_path):
            self._result()
            return []

        def hover(self, file_path, line, character):
            self._result()
            return "Nat : Type"

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=Session,
        start_sweepers=False,
    )
    params = {"project_dir": str(project), "file_path": "Main.lean"}
    if method == "lsp.hover":
        params.update({"line": 0, "character": 7})
    try:
        with pytest.raises(ProjectResourceBusyError, match="changed during LSP request"):
            services.dispatch(method, params)
        assert sessions[0].closed is True

        result = services.dispatch(method, params)
        assert result in {"No diagnostics — file compiles cleanly.", "Nat : Type"}
        assert len(sessions) == 2
    finally:
        services.close()
