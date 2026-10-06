"""Sharing, lifecycle, and resource-boundary tests for the Lean runtime."""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

from servers.lean_client import (
    INSTALL_PATH_ID,
    PROTOCOL_VERSION,
    LeanRuntimeClient,
    LeanRuntimeError,
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
        "repl_command": ("lake", "exe", "repl"),
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

    def retire(self):
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
        assert pools[0].calls == [
            ("#check Nat", {"timeout": 30.0}),
            ("#check Int", {"timeout": 3.0}),
        ]
        warm = services.dispatch("repl.status", {"project_dir": str(project)})
        assert warm["state"] == "warm"
        assert warm["memory_usage_gb"] == 0.25
    finally:
        services.close()

    assert pools[0]._shutdown is True


def test_cache_observation_ignores_staleness_invalidity_and_validation(tmp_path, monkeypatch):
    from servers import lean_runtime as lean_runtime_module

    project = make_lake_project(tmp_path, "observed-stale")
    alias = tmp_path / "observed-stale-alias"
    try:
        alias.symlink_to(project, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    created = []
    closed = []
    validation_calls = 0

    def factory(root):
        resource = object()
        created.append(resource)
        return resource

    def is_valid(resource):
        nonlocal validation_calls
        validation_calls += 1
        return False

    cache = ProjectResourceCache(
        factory,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        is_valid=is_valid,
        start_sweeper=False,
    )
    with cache.lease(str(project)) as resource:
        pass
    cache.invalidate(str(project), resource)
    (project / "lakefile.toml").write_text('[package]\nname = "changed"\n')
    monkeypatch.setattr(
        lean_runtime_module,
        "lean_project_fingerprint",
        lambda root: pytest.fail("observation fingerprinted the project"),
    )

    with cache.observe(str(alias)) as (observed, state):
        assert observed is resource
        assert state == "warm"
        resident = cache.stats()["resident"]
        assert resident[0]["active"] == 1
        assert resident[0]["valid"] is False
        assert closed == []

    assert created == [resource]
    assert closed == []
    assert validation_calls == 0
    cache.close()
    assert closed == [resource]


def test_cache_observation_does_not_refresh_ttl_and_pins_idle_eviction(tmp_path):
    project = make_lake_project(tmp_path, "observed-ttl")
    clock = {"now": 0.0}
    closed = []
    closed_event = threading.Event()

    def close_resource(resource):
        closed.append(resource)
        closed_event.set()

    cache = ProjectResourceCache(
        lambda root: root,
        close_resource,
        max_entries=1,
        idle_seconds=5,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )
    with cache.lease(str(project)):
        pass
    clock["now"] = 10.0

    with cache.observe(str(project)) as (resource, state):
        assert resource == project.resolve()
        assert state == "warm"
        resident = cache.stats()["resident"][0]
        assert resident["active"] == 1
        assert resident["idle_seconds"] == 10.0
        assert cache.evict_idle() == 0

    resident = cache.stats()["resident"][0]
    assert resident["active"] == 0
    assert resident["idle_seconds"] == 10.0
    assert cache.evict_idle() == 1
    assert closed_event.wait(timeout=1)
    assert closed == [project.resolve()]
    cache.close()


def test_cache_close_waits_for_an_active_observation(tmp_path):
    project = make_lake_project(tmp_path, "observed-close")
    closed = []
    close_started = threading.Event()
    close_finished = threading.Event()
    cache = ProjectResourceCache(
        lambda root: root,
        closed.append,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)):
        pass

    def close_cache():
        close_started.set()
        cache.close()
        close_finished.set()

    with cache.observe(str(project)) as (resource, state):
        assert resource == project.resolve()
        assert state == "warm"
        thread = threading.Thread(target=close_cache)
        thread.start()
        assert close_started.wait(timeout=1)
        assert not close_finished.wait(timeout=0.1)
        assert closed == []

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert close_finished.is_set()
    assert closed == [project.resolve()]


def test_invalid_pool_replacement_waits_for_active_observation(tmp_path):
    project = make_lake_project(tmp_path, "observed-replacement")
    resources = []
    events = []
    replacement_started = threading.Event()
    lease_attempted = threading.Event()
    errors = []

    def factory(root):
        resource = object()
        resources.append(resource)
        events.append(f"create:{len(resources)}")
        if len(resources) == 2:
            replacement_started.set()
        return resource

    def close_resource(resource):
        events.append(f"close:{resources.index(resource) + 1}")

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)) as first:
        pass
    cache.invalidate(str(project), first)

    def replace():
        lease_attempted.set()
        try:
            with cache.lease(str(project)) as resource:
                assert resource is resources[1]
        except BaseException as error:
            errors.append(error)

    with cache.observe(str(project)) as (observed, state):
        assert observed is first
        assert state == "warm"
        thread = threading.Thread(target=replace)
        thread.start()
        assert lease_attempted.wait(timeout=1)
        assert not replacement_started.wait(timeout=0.1)
        assert events == ["create:1"]

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == []
    assert events[:3] == ["create:1", "close:1", "create:2"]
    cache.close()


def test_invalidation_retires_after_a_concurrent_observer_releases(tmp_path):
    project = make_lake_project(tmp_path, "observed-deferred-retirement")
    observer_started = threading.Event()
    release_observer = threading.Event()
    retired = threading.Event()
    errors = []
    cache = ProjectResourceCache(
        lambda root: object(),
        lambda resource: retired.set(),
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    def observe():
        try:
            with cache.observe(str(project)) as (resource, state):
                assert resource is first
                assert state == "warm"
                observer_started.set()
                assert release_observer.wait(timeout=2)
        except BaseException as error:
            errors.append(error)

    with cache.lease(str(project)) as first:
        thread = threading.Thread(target=observe)
        thread.start()
        assert observer_started.wait(timeout=1)
        cache.invalidate_resolved(project.resolve(), first)

    assert not retired.wait(timeout=0.1)
    release_observer.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert retired.wait(timeout=1)
    cache.close()


def test_cache_observation_rejects_a_closed_cache(tmp_path):
    project = make_lake_project(tmp_path, "observed-closed")
    cache = ProjectResourceCache(
        lambda root: root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    cache.close()

    with pytest.raises(RuntimeError, match="cache is closed"):
        with cache.observe(str(project)):
            pytest.fail("a closed cache must not expose an observation")


def test_cache_observation_reports_warming_without_waiting_or_creating(tmp_path):
    project = make_lake_project(tmp_path, "observed-warming")
    factory_started = threading.Event()
    release_factory = threading.Event()
    errors = []

    def factory(root):
        factory_started.set()
        assert release_factory.wait(timeout=2)
        return root

    cache = ProjectResourceCache(
        factory,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    def create():
        try:
            with cache.lease(str(project)):
                pass
        except BaseException as error:
            errors.append(error)

    creator = threading.Thread(target=create)
    creator.start()
    try:
        assert factory_started.wait(timeout=1)
        with cache.observe(str(project)) as (resource, state):
            assert resource is None
            assert state == "warming"
    finally:
        release_factory.set()
        creator.join(timeout=2)

    assert not creator.is_alive()
    assert errors == []
    cache.close()


def test_repl_status_reports_a_stale_shutdown_pool_without_replacing_it(tmp_path):
    project = make_lake_project(tmp_path, "observed-status")
    pools = []

    class StatusPool(FakePool):
        def __init__(self, root):
            super().__init__(root)
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1
            super().shutdown()

    def create_pool(root):
        pool = StatusPool(root)
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
        pool = pools[0]
        pool._shutdown = True
        (project / "lakefile.toml").write_text('[package]\nname = "changed"\n')

        status = services.dispatch("repl.status", {"project_dir": str(project)})

        assert status["state"] == "warm"
        assert status["shutdown"] is True
        assert status["memory_usage_gb"] == 0.25
        assert pools == [pool]
        assert pool.shutdown_calls == 0
    finally:
        services.close()

    assert pools[0].shutdown_calls == 1


def test_repl_status_reports_retirement_as_shutdown_in_progress(tmp_path):
    project = make_lake_project(tmp_path, "retiring-status")
    clock = {"now": 0.0}
    close_started = threading.Event()
    release_close = threading.Event()

    class BlockingPool(FakePool):
        def shutdown(self):
            self._shutdown = True
            close_started.set()
            assert release_close.wait(timeout=2)

    services = LeanRuntimeServices(
        runtime_config(idle_seconds=1),
        repl_factory=BlockingPool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    services.repl_projects._clock = lambda: clock["now"]
    try:
        services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#check Nat", "timeout": None},
        )
        clock["now"] = 2.0
        assert services.repl_projects.evict_idle() == 1
        assert close_started.wait(timeout=1)

        status = services.dispatch("repl.status", {"project_dir": str(project)})

        assert status["state"] == "retiring"
        assert status["shutdown"] is True
        assert status["memory_usage_gb"] == 0.0
    finally:
        release_close.set()
        services.close()


def test_service_close_starts_lsp_retirement_before_blocked_repl_cleanup_finishes(
    tmp_path,
):
    project = make_lake_project(tmp_path, "parallel-service-close")
    source = project / "Main.lean"
    source.write_text("#check Nat\n")
    repl_close_started = threading.Event()
    release_repl_close = threading.Event()
    lsp_closed = threading.Event()
    services_closed = threading.Event()

    class BlockingPool(FakePool):
        def shutdown(self):
            self._shutdown = True
            repl_close_started.set()
            assert release_repl_close.wait(timeout=2)

    class ClosingLsp(FakeLsp):
        def get_diagnostics(self, file_path):
            return []

        def close(self):
            super().close()
            lsp_closed.set()

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=BlockingPool,
        lsp_factory=ClosingLsp,
        start_sweepers=False,
    )
    services.dispatch(
        "repl.run",
        {"project_dir": str(project), "code": "#check Nat", "timeout": None},
    )
    services.dispatch(
        "lsp.diagnostics",
        {"project_dir": str(project), "file_path": "Main.lean"},
    )

    thread = threading.Thread(target=lambda: (services.close(), services_closed.set()))
    thread.start()
    assert repl_close_started.wait(timeout=1)
    assert lsp_closed.wait(timeout=1)
    assert not services_closed.is_set()

    release_repl_close.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert services_closed.is_set()


def test_repl_status_field_cancellation_releases_pin_without_refreshing_ttl(tmp_path):
    project = make_lake_project(tmp_path, "observed-field-cancellation")
    clock = {"now": 0.0}
    services = None

    class Cancellation(BaseException):
        pass

    class CancellingPool(FakePool):
        def get_memory_usage(self):
            resident = services.repl_projects.stats()["resident"][0]
            assert resident["active"] == 1
            assert resident["idle_seconds"] == 10.0
            raise Cancellation("cancel pool field read")

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=CancellingPool,
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    services.repl_projects._clock = lambda: clock["now"]
    try:
        services.dispatch(
            "repl.run",
            {"project_dir": str(project), "code": "#check Nat", "timeout": None},
        )
        clock["now"] = 10.0

        with pytest.raises(Cancellation, match="cancel pool field read"):
            services.dispatch("repl.status", {"project_dir": str(project)})

        resident = services.repl_projects.stats()["resident"][0]
        assert resident["active"] == 0
        assert resident["idle_seconds"] == 10.0
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
    closed_event = threading.Event()

    def close_resource(resource):
        closed.append(resource)
        closed_event.set()

    def slow_factory(root):
        clock["now"] = 11.0
        return root

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

    assert closed_event.wait(timeout=1)
    assert closed == [project.resolve()]
    with cache.observe(str(project)) as (resource, state):
        assert resource is None and state == "cold"
    cache.close()


def test_idle_ttl_never_closes_an_active_resource(tmp_path):
    project = make_lake_project(tmp_path, "idle")
    clock = {"now": 0.0}
    closed = []
    closed_event = threading.Event()

    def close_resource(resource):
        closed.append(resource)
        closed_event.set()

    cache = ProjectResourceCache(
        lambda root: root,
        close_resource,
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
    assert closed_event.wait(timeout=1)
    assert closed == [project.resolve()]
    cache.close()


def test_idle_retirement_reserves_the_root_until_cleanup_finishes(tmp_path):
    project = make_lake_project(tmp_path, "retiring-root")
    created = []
    close_started = threading.Event()
    release_close = threading.Event()

    def factory(root):
        resource = object()
        created.append(resource)
        return resource

    def close_resource(resource):
        close_started.set()
        assert release_close.wait(timeout=2)

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=0.01,
        start_sweeper=False,
    )
    with cache.lease(str(project)) as first:
        pass
    time.sleep(0.02)

    assert cache.evict_idle() == 1
    assert close_started.wait(timeout=1)
    assert cache.stats()["retiring"] == [str(project.resolve())]
    with cache.observe(str(project)) as (resource, state):
        assert resource is None and state == "retiring"
    with pytest.raises(ProjectResourceBusyError, match="shared Lean project slot"):
        with cache.lease(str(project), acquisition_timeout=0.05):
            pytest.fail("a replacement overlapped its retiring predecessor")
    assert created == [first]

    release_close.set()
    with cache.lease(str(project), acquisition_timeout=1.0) as second:
        assert second is not first
    assert created == [first, second]
    cache.close()


def test_retiring_resource_counts_against_capacity_for_other_roots(tmp_path):
    first = make_lake_project(tmp_path, "retiring-capacity-first")
    second = make_lake_project(tmp_path, "retiring-capacity-second")
    created = []
    close_started = threading.Event()
    release_close = threading.Event()

    def factory(root):
        created.append(root)
        return root

    def close_resource(resource):
        if resource == first.resolve():
            close_started.set()
            assert release_close.wait(timeout=2)

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=0.01,
        start_sweeper=False,
    )
    with cache.lease(str(first)):
        pass
    time.sleep(0.02)
    assert cache.evict_idle() == 1
    assert close_started.wait(timeout=1)

    with pytest.raises(ProjectResourceBusyError, match="shared Lean project slot"):
        with cache.lease(str(second), acquisition_timeout=0.05):
            pytest.fail("a retiring slot was treated as free capacity")
    assert created == [first.resolve()]

    release_close.set()
    with cache.lease(str(second), acquisition_timeout=1.0):
        pass
    assert created == [first.resolve(), second.resolve()]
    cache.close()


def test_lru_retirement_never_blocks_the_request_thread_past_its_deadline(tmp_path):
    first = make_lake_project(tmp_path, "lru-retirement-first")
    second = make_lake_project(tmp_path, "lru-retirement-second")
    created = []
    close_started = threading.Event()
    release_close = threading.Event()

    def factory(root):
        created.append(root)
        return root

    def close_resource(resource):
        if resource == first.resolve():
            close_started.set()
            assert release_close.wait(timeout=2)

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
    with pytest.raises(ProjectResourceBusyError, match="shared Lean project slot"):
        with cache.lease(str(second), acquisition_timeout=0.05):
            pytest.fail("LRU cleanup ran synchronously or freed capacity early")
    assert time.monotonic() - started < 0.5
    assert close_started.is_set()
    assert created == [first.resolve()]

    release_close.set()
    with cache.lease(str(second), acquisition_timeout=1.0):
        pass
    assert created == [first.resolve(), second.resolve()]
    cache.close()


def test_invalid_last_release_transfers_ownership_to_the_reaper(tmp_path):
    project = make_lake_project(tmp_path, "invalid-retirement")
    close_started = threading.Event()
    release_close = threading.Event()

    def close_resource(resource):
        close_started.set()
        assert release_close.wait(timeout=2)

    cache = ProjectResourceCache(
        lambda root: object(),
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    root = project.resolve()

    with cache.lease_resolved(root) as resource:
        cache.invalidate_resolved(root, resource)

    assert close_started.wait(timeout=1)
    assert cache.stats()["resident"] == []
    assert cache.stats()["retiring"] == [str(root)]
    with cache.observe(str(project)) as (observed, state):
        assert observed is None and state == "retiring"

    release_close.set()
    cache.close()


def test_failed_retirement_retries_without_parallel_reapers(tmp_path):
    project = make_lake_project(tmp_path, "retirement-retry")
    attempts = 0
    active = 0
    maximum_active = 0
    lock = threading.Lock()
    retired = threading.Event()
    clock = {"now": 0.0}

    def close_resource(resource):
        nonlocal attempts, active, maximum_active
        with lock:
            attempts += 1
            active += 1
            maximum_active = max(maximum_active, active)
            attempt = attempts
        try:
            if attempt < 3:
                raise RuntimeError("injected cleanup failure")
            retired.set()
        finally:
            with lock:
                active -= 1

    cache = ProjectResourceCache(
        lambda root: root,
        close_resource,
        max_entries=1,
        idle_seconds=1,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )
    with cache.lease(str(project)):
        pass
    clock["now"] = 2.0
    assert cache.evict_idle() == 1
    assert retired.wait(timeout=2)
    cache.close()

    assert attempts == 3
    assert maximum_active == 1


def test_retirement_pool_can_drain_every_occupied_slot_in_parallel(tmp_path):
    projects = [
        make_lake_project(tmp_path, "parallel-retirement-first"),
        make_lake_project(tmp_path, "parallel-retirement-second"),
    ]
    clock = {"now": 0.0}
    started = 0
    both_started = threading.Event()
    release_close = threading.Event()
    lock = threading.Lock()

    def close_resource(resource):
        nonlocal started
        with lock:
            started += 1
            if started == 2:
                both_started.set()
        assert release_close.wait(timeout=2)

    cache = ProjectResourceCache(
        lambda root: root,
        close_resource,
        max_entries=2,
        idle_seconds=1,
        start_sweeper=False,
        clock=lambda: clock["now"],
    )
    for project in projects:
        with cache.lease(str(project)):
            pass
    clock["now"] = 2.0

    assert cache.evict_idle() == 2
    assert both_started.wait(timeout=1)
    assert len(cache.stats()["retiring"]) == 2
    release_close.set()
    cache.close()


def test_late_startup_moves_directly_to_retirement(tmp_path):
    project = make_lake_project(tmp_path, "late-retirement")
    close_started = threading.Event()
    release_close = threading.Event()
    created = []

    def factory(root):
        time.sleep(0.03)
        resource = object()
        created.append(resource)
        return resource

    def close_resource(resource):
        close_started.set()
        assert release_close.wait(timeout=2)

    cache = ProjectResourceCache(
        factory,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with pytest.raises(ProjectResourceBusyError, match="startup exceeded"):
        with cache.lease(str(project), acquisition_timeout=0.01):
            pytest.fail("late startup entered a request")
    assert close_started.wait(timeout=1)
    assert cache.stats()["retiring"] == [str(project.resolve())]
    with cache.observe(str(project)) as (resource, state):
        assert resource is None and state == "retiring"
    with pytest.raises(ProjectResourceBusyError):
        with cache.lease(str(project), acquisition_timeout=0.05):
            pytest.fail("late-start cleanup overlapped a replacement")
    assert len(created) == 1

    release_close.set()
    cache.close()


def test_partial_repl_startup_transfers_cleanup_without_masking_the_error(
    tmp_path, monkeypatch
):
    from servers.repl import pool as repl_pool_module

    project = make_lake_project(tmp_path, "partial-repl-startup")
    startup_error = ValueError("injected REPL startup failure")
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.close_calls = 0
            workers.append(self)

        def start(self):
            raise startup_error

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("injected first cleanup failure")
            cleanup_started.set()
            if not release_cleanup.wait(timeout=5):
                raise RuntimeError("test did not release cleanup")

    monkeypatch.setattr(repl_pool_module, "LeanRepl", FakeRepl)
    services = LeanRuntimeServices(
        runtime_config(),
        lsp_factory=FakeLsp,
        start_sweepers=False,
    )
    try:
        started = time.monotonic()
        with pytest.raises(ValueError, match="injected REPL startup failure") as raised:
            services.dispatch(
                "repl.run",
                {"project_dir": str(project), "code": "#check Nat", "timeout": None},
            )
        assert raised.value is startup_error
        assert time.monotonic() - started < 0.5
        assert cleanup_started.wait(timeout=1)
        assert services.repl_projects.stats()["retiring"] == [str(project.resolve())]

        with pytest.raises(ProjectResourceBusyError):
            with services.repl_projects.lease(str(project), acquisition_timeout=0.05):
                pytest.fail("partial cleanup released project capacity early")
        assert len(workers) == 1
    finally:
        release_cleanup.set()
        services.close()

    assert workers[0].close_calls == 2


def test_partial_lsp_startup_transfers_cleanup_without_masking_the_error(
    tmp_path, monkeypatch
):
    from servers.lsp import server as lsp_module

    project = make_lake_project(tmp_path, "partial-lsp-startup")
    (project / "Main.lean").write_text("#check Nat\n")
    startup_error = ValueError("injected LSP startup failure")
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    terminate_calls = 0

    class Stream:
        def close(self):
            pass

    class Process:
        pid = 43210
        returncode = None
        stdin = Stream()
        stdout = Stream()

    process = Process()

    def terminate(process_group_id):
        nonlocal terminate_calls
        terminate_calls += 1
        if terminate_calls == 1:
            raise RuntimeError("injected first cleanup failure")
        cleanup_started.set()
        if not release_cleanup.wait(timeout=5):
            raise RuntimeError("test did not release cleanup")

    def reap(owned):
        owned.returncode = 0

    monkeypatch.setattr(lsp_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(lsp_module, "_terminate_process_group", terminate)
    monkeypatch.setattr(lsp_module, "_reap_process", reap)
    monkeypatch.setattr(
        lsp_module.LeanLspSession,
        "_send_request",
        lambda self, method, params, **kwargs: (_ for _ in ()).throw(startup_error),
    )
    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        start_sweepers=False,
    )
    try:
        started = time.monotonic()
        with pytest.raises(ValueError, match="injected LSP startup failure") as raised:
            services.dispatch(
                "lsp.diagnostics",
                {"project_dir": str(project), "file_path": "Main.lean"},
            )
        assert raised.value is startup_error
        assert time.monotonic() - started < 0.5
        assert cleanup_started.wait(timeout=1)
        assert services.lsp_projects.stats()["retiring"] == [str(project.resolve())]

        with pytest.raises(ProjectResourceBusyError):
            with services.lsp_projects.lease(str(project), acquisition_timeout=0.05):
                pytest.fail("partial LSP cleanup released project capacity early")
    finally:
        release_cleanup.set()
        services.close()

    assert terminate_calls == 2
    assert process.returncode == 0


def test_cache_close_waits_for_retirement_before_returning(tmp_path):
    project = make_lake_project(tmp_path, "retiring-shutdown")
    close_started = threading.Event()
    release_close = threading.Event()
    close_finished = threading.Event()
    def close_resource(resource):
        close_started.set()
        assert release_close.wait(timeout=2)

    cache = ProjectResourceCache(
        lambda root: root,
        close_resource,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    with cache.lease(str(project)):
        pass

    thread = threading.Thread(target=lambda: (cache.close(), close_finished.set()))
    thread.start()
    assert close_started.wait(timeout=1)
    assert not close_finished.wait(timeout=0.1)
    release_close.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert close_finished.is_set()


def test_resolved_invalidation_survives_project_deletion(tmp_path):
    project = make_lake_project(tmp_path, "deleted-before-invalidation")
    root = project.resolve()
    retired = threading.Event()
    cache = ProjectResourceCache(
        lambda resolved: object(),
        lambda resource: retired.set(),
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )

    with cache.lease_resolved(root) as resource:
        shutil.rmtree(root)
        cache.invalidate_resolved(root, resource)

    assert retired.wait(timeout=1)
    cache.close()


def test_resolved_lease_rejects_noncanonical_aliases(tmp_path):
    project = make_lake_project(tmp_path, "canonical-lease")
    cache = ProjectResourceCache(
        lambda root: root,
        lambda resource: None,
        max_entries=1,
        idle_seconds=1800,
        start_sweeper=False,
    )
    lexical_alias = project / ".." / project.name

    with pytest.raises(ValueError, match="canonical spelling"):
        with cache.lease_resolved(lexical_alias):
            pytest.fail("a second spelling created a duplicate project generation")

    cache.close()


@pytest.mark.parametrize("failure_point", ["before", "after"])
def test_cache_constructor_drains_started_reapers_on_start_failure(
    monkeypatch, failure_point
):
    real_start = threading.Thread.start
    started = []

    def fail_second_reaper(worker):
        if worker.name == "autoform-project-retirement-2":
            if failure_point == "after":
                result = real_start(worker)
                started.append(worker)
                raise RuntimeError("injected post-start interruption")
            raise RuntimeError("injected thread-start failure")
        result = real_start(worker)
        started.append(worker)
        return result

    monkeypatch.setattr(threading.Thread, "start", fail_second_reaper)
    with pytest.raises(RuntimeError, match="injected"):
        ProjectResourceCache(
            lambda root: root,
            lambda resource: None,
            max_entries=2,
            idle_seconds=1800,
            start_sweeper=False,
        )

    assert started
    assert all(not worker.is_alive() for worker in started)


def test_cache_constructor_drains_reapers_when_sweeper_start_is_interrupted(monkeypatch):
    real_start = threading.Thread.start
    started = []

    def interrupt_sweeper(worker):
        result = real_start(worker)
        started.append(worker)
        if worker.name == "autoform-project-eviction":
            raise RuntimeError("injected sweeper-start interruption")
        return result

    monkeypatch.setattr(threading.Thread, "start", interrupt_sweeper)
    with pytest.raises(RuntimeError, match="sweeper-start"):
        ProjectResourceCache(
            lambda root: root,
            lambda resource: None,
            max_entries=2,
            idle_seconds=1,
            start_sweeper=True,
        )

    assert len(started) == 3
    assert all(not worker.is_alive() for worker in started)


def test_service_constructor_closes_first_cache_when_second_cache_start_fails(
    monkeypatch,
):
    real_start = threading.Thread.start
    retirement_starts = 0
    started = []

    def fail_in_second_cache(worker):
        nonlocal retirement_starts
        if worker.name.startswith("autoform-project-retirement-"):
            retirement_starts += 1
            if retirement_starts == 3:
                raise RuntimeError("injected second-cache start failure")
        result = real_start(worker)
        started.append(worker)
        return result

    monkeypatch.setattr(threading.Thread, "start", fail_in_second_cache)
    with pytest.raises(RuntimeError, match="second-cache start"):
        LeanRuntimeServices(
            runtime_config(max_projects=2, repl_project_limit=2),
            repl_factory=FakePool,
            lsp_factory=FakeLsp,
            start_sweepers=False,
        )

    assert retirement_starts == 3
    assert started
    assert all(not worker.is_alive() for worker in started)


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


def test_response_budget_includes_replacement_and_failed_pool_cleanup(monkeypatch):
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "3")
    monkeypatch.setenv("AUTOFORM_REPL_WORKERS_PER_PROJECT", "3")
    monkeypatch.setenv("AUTOFORM_RUNTIME_RESPONSE_TIMEOUT", "860")
    with pytest.raises(ValueError, match="REPL worker startup"):
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


def test_failed_lsp_request_returns_while_verified_cleanup_runs(tmp_path):
    from servers.lsp.server import LspProtocolError

    project = make_lake_project(tmp_path, "lsp-background-retirement")
    (project / "Main.lean").write_text("#check Nat\n")
    close_started = threading.Event()
    release_close = threading.Event()

    class Session(FakeLsp):
        def __init__(self, root):
            super().__init__(root)
            self.retiring = False

        def get_diagnostics(self, file_path):
            raise LspProtocolError("broken shared stream")

        def retire(self):
            self.retiring = True

        def is_alive(self):
            return not self.retiring and not self.closed

        def close(self):
            close_started.set()
            assert release_close.wait(timeout=2)
            self.closed = True

    services = LeanRuntimeServices(
        runtime_config(max_projects=1),
        repl_factory=FakePool,
        lsp_factory=Session,
        start_sweepers=False,
    )
    try:
        started = time.monotonic()
        with pytest.raises(LspProtocolError, match="broken shared stream"):
            services.dispatch(
                "lsp.diagnostics",
                {"project_dir": str(project), "file_path": "Main.lean"},
            )
        assert time.monotonic() - started < 0.5
        assert close_started.wait(timeout=1)
        assert services.lsp_projects.stats()["retiring"] == [str(project.resolve())]

        with pytest.raises(ProjectResourceBusyError):
            with services.lsp_projects.lease(str(project), acquisition_timeout=0.05):
                pytest.fail("replacement overlapped LSP cleanup")
    finally:
        release_close.set()
        services.close()


@pytest.mark.parametrize("method", ["lsp.diagnostics", "lsp.hover"])
def test_failed_lsp_invalidation_survives_project_deletion(tmp_path, method):
    from servers.lsp.server import LspProtocolError

    project = make_lake_project(tmp_path, "lsp-deleted-root")
    source = project / "Main.lean"
    source.write_text("#check Nat\n")
    sessions = []

    class Session(FakeLsp):
        def __init__(self, root):
            super().__init__(root)
            self.alive = True
            sessions.append(self)

        def is_alive(self):
            return self.alive and not self.closed

        def fail(self):
            self.alive = False
            shutil.rmtree(self.root)
            raise LspProtocolError("original protocol failure")

        def get_diagnostics(self, file_path):
            return self.fail()

        def hover(self, file_path, line, character):
            return self.fail()

    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=Session,
        start_sweepers=False,
    )
    try:
        with pytest.raises(LspProtocolError, match="original protocol failure"):
            params = {"project_dir": str(project), "file_path": "Main.lean"}
            if method == "lsp.hover":
                params.update({"line": 0, "character": 0})
            services.dispatch(method, params)
    finally:
        services.close()

    assert sessions[0].closed is True
