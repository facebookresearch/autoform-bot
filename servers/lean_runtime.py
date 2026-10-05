"""Persistent, node-local owner of Autoform's Lean REPL and LSP processes.

This is an internal runtime rather than a third MCP server.  The public
``autoform-repl`` and ``autoform-lsp`` stdio servers proxy their four tools to
this process through a private Unix-domain socket.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shlex
import signal
import socketserver
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Generic, TypeVar

from servers import resolve_lean_file, resolve_lean_project_dir
from servers.lean_client import (
    BUILD_GENERATION,
    DEFAULT_REPL_REQUEST_TIMEOUT,
    INSTALL_ID,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    DEFAULT_RESPONSE_TIMEOUT,
    REPL_RESPONSE_GRACE_SECONDS,
    LeanRuntimeClient,
    LeanRuntimeError,
    LeanRuntimeUnavailable,
    RuntimePaths,
    default_runtime_paths,
    runtime_paths_for_socket,
)
from servers.lsp.server import (
    LspConfig,
    LspBusyError,
    LeanLspSession,
    LspProtocolError,
    format_lsp_diagnostics,
)
from servers.repl.core import format_repl_response
from servers.repl.pool import (
    DEFAULT_RAM_FRACTION,
    LeanReplPool,
    LeanReplPoolConfig,
    ReplPoolBusyError,
    ReplPoolUnavailableError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_MAX_PROJECTS = 4
DEFAULT_IDLE_SECONDS = 30 * 60
DEFAULT_LSP_TIMEOUT = 60.0
DEFAULT_MAX_LSP_REQUEST_SECONDS = 600.0
DEFAULT_MAX_REPL_REQUEST_SECONDS = 240.0
DEFAULT_RPC_READ_TIMEOUT = 10.0
DEFAULT_MAX_CONNECTIONS = 64
RUNTIME_SAFETY_SECONDS = 30.0
TERMINAL_CLEANUP_RETRY_SECONDS = 0.05
MAX_TERMINAL_CLEANUP_RETRY_SECONDS = 1.0
MAX_REQUEST_ID_CHARS = 128
# Conservative bounds for cleanup/startup work that surrounds one LSP call.
# They keep the daemon's work inside the client's response deadline even when
# an inactive project must be replaced first.
LSP_STARTUP_BUDGET = 60.0
LSP_CLOSE_BUDGET = 67.0


class ProjectResourceBusyError(TimeoutError):
    """A shared project slot could not be admitted within the RPC budget."""


def _decode_runtime_request(raw: bytes) -> Any:
    """Decode strict JSON for the runtime's private request boundary."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"request contains nonstandard JSON constant {value!r}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"request contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    def parse_finite_float(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"request contains non-finite JSON number {value!r}")
        return result

    try:
        return json.loads(
            raw,
            parse_constant=reject_constant,
            parse_float=parse_finite_float,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ValueError("request is not valid strict JSON") from error


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    raw = str(default) if raw is None or not raw.strip() else raw
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from error
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


def _nonnegative_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    raw = str(default) if raw is None or not raw.strip() else raw
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number, got {raw!r}") from error
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number, got {value}")
    return value


def _positive_float(name: str, default: float) -> float:
    value = _nonnegative_float(name, default)
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _default_total_repl_workers() -> int:
    try:
        import psutil

        total_gb = psutil.virtual_memory().total / (1024**3)
        return max(1, int(total_gb * DEFAULT_RAM_FRACTION / 16))
    except ImportError:  # pragma: no cover - psutil is a runtime dependency
        return 1


@dataclass(frozen=True)
class LeanRuntimeConfig:
    """Node-wide resource limits; the first starter owns these until stop."""

    max_projects: int
    idle_seconds: float
    total_repl_workers: int
    repl_workers_per_project: int
    repl_project_limit: int
    repl_command: tuple[str, ...]
    lsp_command: tuple[str, ...]
    lsp_timeout: float
    max_lsp_request_seconds: float
    repl_request_timeout: float
    max_repl_request_seconds: float
    rpc_read_timeout: float
    max_connections: int
    response_timeout: float

    @classmethod
    def from_environment(cls) -> "LeanRuntimeConfig":
        max_projects = _positive_int("AUTOFORM_MAX_LEAN_PROJECTS", DEFAULT_MAX_PROJECTS)
        idle_seconds = _nonnegative_float("AUTOFORM_LEAN_IDLE_SECONDS", DEFAULT_IDLE_SECONDS)
        total_workers = _positive_int(
            "AUTOFORM_REPL_TOTAL_WORKERS",
            _default_total_repl_workers(),
        )
        legacy_raw = os.environ.get("LEAN_NUM_REPLS", "0") or "0"
        try:
            legacy_workers = int(legacy_raw)
        except ValueError as error:
            raise ValueError(
                f"LEAN_NUM_REPLS must be a nonnegative integer, got {legacy_raw!r}"
            ) from error
        if legacy_workers < 0:
            raise ValueError(
                f"LEAN_NUM_REPLS must be a nonnegative integer, got {legacy_workers}"
            )
        workers_per_project = _positive_int(
            "AUTOFORM_REPL_WORKERS_PER_PROJECT",
            legacy_workers or 1,
        )
        if workers_per_project > total_workers:
            raise ValueError(
                "AUTOFORM_REPL_WORKERS_PER_PROJECT cannot exceed "
                "AUTOFORM_REPL_TOTAL_WORKERS"
            )
        repl_project_limit = min(max_projects, total_workers // workers_per_project)
        repl_command = tuple(shlex.split(os.environ.get("LEAN_REPL_CMD", "lake exe @repl/repl")))
        lsp_command = tuple(shlex.split(os.environ.get("LEAN_LSP_CMD", "lake serve")))
        if not repl_command:
            raise ValueError("LEAN_REPL_CMD must not be empty")
        if not lsp_command:
            raise ValueError("LEAN_LSP_CMD must not be empty")
        repl_request_timeout = _positive_float(
            "AUTOFORM_REPL_REQUEST_TIMEOUT",
            DEFAULT_REPL_REQUEST_TIMEOUT,
        )
        max_repl_request_seconds = _positive_float(
            "AUTOFORM_MAX_REPL_REQUEST_SECONDS",
            DEFAULT_MAX_REPL_REQUEST_SECONDS,
        )
        if repl_request_timeout > max_repl_request_seconds:
            raise ValueError(
                "AUTOFORM_REPL_REQUEST_TIMEOUT cannot exceed "
                "AUTOFORM_MAX_REPL_REQUEST_SECONDS"
            )
        lsp_timeout = _positive_float("LEAN_LSP_TIMEOUT", DEFAULT_LSP_TIMEOUT)
        max_lsp_request_seconds = _positive_float(
            "AUTOFORM_MAX_LSP_REQUEST_SECONDS",
            DEFAULT_MAX_LSP_REQUEST_SECONDS,
        )
        if lsp_timeout > max_lsp_request_seconds:
            raise ValueError(
                "LEAN_LSP_TIMEOUT cannot exceed AUTOFORM_MAX_LSP_REQUEST_SECONDS"
            )
        response_timeout = _positive_float(
            "AUTOFORM_RUNTIME_RESPONSE_TIMEOUT",
            DEFAULT_RESPONSE_TIMEOUT,
        )
        if (
            max_repl_request_seconds
            + REPL_RESPONSE_GRACE_SECONDS
            >= response_timeout
        ):
            raise ValueError(
                "AUTOFORM_RUNTIME_RESPONSE_TIMEOUT is too small for the configured "
                "REPL request and cleanup limits"
            )
        if (
            LSP_CLOSE_BUDGET
            + LSP_STARTUP_BUDGET
            + max_lsp_request_seconds
            + RUNTIME_SAFETY_SECONDS
            > response_timeout
        ):
            raise ValueError(
                "AUTOFORM_RUNTIME_RESPONSE_TIMEOUT is too small for "
                "AUTOFORM_MAX_LSP_REQUEST_SECONDS"
            )
        return cls(
            max_projects=max_projects,
            idle_seconds=idle_seconds,
            total_repl_workers=total_workers,
            repl_workers_per_project=workers_per_project,
            repl_project_limit=max(1, repl_project_limit),
            repl_command=repl_command,
            lsp_command=lsp_command,
            lsp_timeout=lsp_timeout,
            max_lsp_request_seconds=max_lsp_request_seconds,
            repl_request_timeout=repl_request_timeout,
            max_repl_request_seconds=max_repl_request_seconds,
            rpc_read_timeout=_positive_float(
                "AUTOFORM_RUNTIME_READ_TIMEOUT",
                DEFAULT_RPC_READ_TIMEOUT,
            ),
            max_connections=_positive_int(
                "AUTOFORM_RUNTIME_MAX_CONNECTIONS",
                DEFAULT_MAX_CONNECTIONS,
            ),
            response_timeout=response_timeout,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_projects": self.max_projects,
            "idle_seconds": self.idle_seconds,
            "total_repl_workers": self.total_repl_workers,
            "repl_workers_per_project": self.repl_workers_per_project,
            "repl_project_limit": self.repl_project_limit,
            "repl_command": list(self.repl_command),
            "lsp_command": list(self.lsp_command),
            "lsp_timeout": self.lsp_timeout,
            "max_lsp_request_seconds": self.max_lsp_request_seconds,
            "repl_request_timeout": self.repl_request_timeout,
            "max_repl_request_seconds": self.max_repl_request_seconds,
            "rpc_read_timeout": self.rpc_read_timeout,
            "max_connections": self.max_connections,
            "response_timeout": self.response_timeout,
        }


def lean_project_fingerprint(project_dir: Path) -> tuple[tuple[str, int, int], ...]:
    """Return the project metadata that makes a resident Lean process stale."""
    files = ("lean-toolchain", "lake-manifest.json", "lakefile.toml", "lakefile.lean")
    fingerprint: list[tuple[str, int, int]] = []
    for name in files:
        path = project_dir / name
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        fingerprint.append((name, info.st_mtime_ns, info.st_size))
    return tuple(fingerprint)


@dataclass
class _CacheEntry(Generic[T]):
    resource: T
    fingerprint: tuple[tuple[str, int, int], ...]
    last_used: float
    active: int = 0
    invalid: bool = False


class ProjectResourceCache(Generic[T]):
    """Bounded project cache with active leases and idle/LRU eviction."""

    def __init__(
        self,
        factory: Callable[[Path], T],
        close_resource: Callable[[T], None],
        *,
        max_entries: int,
        idle_seconds: float,
        is_valid: Callable[[T], bool] | None = None,
        start_sweeper: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._factory = factory
        self._close_resource = close_resource
        self._max_entries = max_entries
        self._idle_seconds = idle_seconds
        self._is_valid = is_valid
        self._clock = clock
        self._entries: dict[Path, _CacheEntry[T]] = {}
        self._retiring: dict[Path, T] = {}
        self._retiring_active: set[Path] = set()
        self._creating: set[Path] = set()
        self._condition = threading.Condition()
        self._closed = False
        self._stop_sweeper = threading.Event()
        self._sweeper: threading.Thread | None = None
        if start_sweeper and idle_seconds > 0:
            interval = min(60.0, max(1.0, idle_seconds / 2))
            self._sweeper = threading.Thread(
                target=self._sweep,
                args=(interval,),
                name="autoform-project-eviction",
                daemon=True,
            )
            self._sweeper.start()

    @contextmanager
    def lease(
        self,
        project_dir: str,
        *,
        create: bool = True,
        acquisition_timeout: float | None = None,
        deadline: float | None = None,
        creation_budget: float = 0.0,
    ) -> Iterator[T | None]:
        """Keep a project resource alive for the complete operation."""
        if acquisition_timeout is not None and deadline is not None:
            raise TypeError("pass acquisition_timeout or deadline, not both")
        if acquisition_timeout is not None:
            if acquisition_timeout <= 0:
                raise ProjectResourceBusyError(
                    "no response budget remains for a shared Lean project slot"
                )
            deadline = self._clock() + acquisition_timeout
        root = resolve_lean_project_dir(project_dir)
        resource = self._acquire(
            root,
            create=create,
            deadline=deadline,
            creation_budget=creation_budget,
        )
        operation_error: BaseException | None = None
        try:
            yield resource
        except BaseException as error:
            operation_error = error
        try:
            if resource is not None:
                self._release(root, resource)
        except BaseException as cleanup_error:
            if operation_error is None:
                raise
            note = f"Lean project resource release also failed: {cleanup_error}"
            add_note = getattr(operation_error, "add_note", None)
            if add_note is not None:
                add_note(note)
            else:  # pragma: no cover - Python 3.10 compatibility
                logger.error("%s", note)
        if operation_error is not None:
            raise operation_error.with_traceback(operation_error.__traceback__)

    def stats(self) -> dict[str, Any]:
        with self._condition:
            now = self._clock()
            return {
                "limit": self._max_entries,
                "resident": [
                    {
                        "project_dir": str(root),
                        "active": entry.active,
                        "valid": (
                            not entry.invalid
                            and (
                                self._is_valid is None
                                or self._is_valid(entry.resource)
                            )
                        ),
                        "idle_seconds": round(max(0.0, now - entry.last_used), 3),
                    }
                    for root, entry in sorted(
                        self._entries.items(), key=lambda item: str(item[0])
                    )
                ],
                "retiring": sorted(str(root) for root in self._retiring),
                "creating": sorted(str(root) for root in self._creating),
            }

    def state(self, project_dir: str) -> str:
        """Return the current project-resource lifecycle state."""
        root = resolve_lean_project_dir(project_dir)
        with self._condition:
            entry = self._entries.get(root)
            if entry is not None:
                valid = not entry.invalid and (
                    self._is_valid is None or self._is_valid(entry.resource)
                )
                return "warm" if valid else "retiring"
            if root in self._retiring:
                return "retiring"
            if root in self._creating:
                return "warming"
            return "cold"

    def invalidate(self, project_dir: str, resource: T) -> None:
        """Arrange to replace a failed resource after its active calls finish."""
        root = resolve_lean_project_dir(project_dir)
        with self._condition:
            entry = self._entries.get(root)
            if entry is not None and entry.resource is resource:
                entry.invalid = True
                self._condition.notify_all()

    def evict_idle(self) -> int:
        """Close every inactive entry older than the configured TTL."""
        if self._idle_seconds <= 0:
            return 0
        with self._condition:
            if self._closed:
                return 0
            now = self._clock()
            victims = [
                root
                for root, entry in self._entries.items()
                if entry.active == 0 and now - entry.last_used >= self._idle_seconds
            ]
            for root in victims:
                self._retiring[root] = self._entries.pop(root).resource
            retiring = [
                (root, resource)
                for root, resource in self._retiring.items()
                if root not in self._retiring_active
            ]
            if victims:
                self._condition.notify_all()
        for root, resource in retiring:
            with self._condition:
                if (
                    self._retiring.get(root) is not resource
                    or root in self._retiring_active
                ):
                    continue
                self._retiring_active.add(root)
            self._retire(root, resource)
        return len(victims)

    def close(self) -> None:
        """Stop admission, wait for active leases, then close all resources."""
        self._stop_sweeper.set()
        with self._condition:
            self._closed = True
            while (
                self._creating
                or self._retiring_active
                or any(entry.active for entry in self._entries.values())
            ):
                self._condition.wait(timeout=0.5)
            for root, entry in self._entries.items():
                self._retiring[root] = entry.resource
            self._entries.clear()
            retiring = list(self._retiring.items())
            self._condition.notify_all()
        first_error: BaseException | None = None
        try:
            for root, resource in retiring:
                with self._condition:
                    self._condition.wait_for(
                        lambda: root not in self._retiring_active
                    )
                    if self._retiring.get(root) is not resource:
                        continue
                    self._retiring_active.add(root)
                try:
                    self._retire(root, resource)
                except BaseException as error:
                    if first_error is None:
                        first_error = error
        finally:
            if self._sweeper and self._sweeper is not threading.current_thread():
                self._sweeper.join()
        if first_error is not None:
            raise first_error.with_traceback(first_error.__traceback__)
        with self._condition:
            failed = len(self._retiring)
        if failed:
            raise RuntimeError(
                f"failed to retire {failed} Lean project resource(s)"
            )

    def _acquire(
        self,
        root: Path,
        *,
        create: bool,
        deadline: float | None,
        creation_budget: float,
    ) -> T | None:
        if deadline is not None and self._clock() >= deadline:
            raise ProjectResourceBusyError(
                "no response budget remains for a shared Lean project slot"
            )
        if creation_budget < 0:
            raise ValueError("creation_budget must be nonnegative")
        fingerprint = lean_project_fingerprint(root)
        while True:
            wait = False
            retirement: tuple[Path, T] | None = None
            with self._condition:
                if self._closed:
                    raise RuntimeError("project resource cache is closed")

                if root in self._retiring:
                    if not create:
                        return None
                    if root in self._retiring_active:
                        wait = True
                    else:
                        self._require_creation_budget(
                            root,
                            deadline=deadline,
                            creation_budget=creation_budget,
                        )
                        self._retiring_active.add(root)
                        retirement = (root, self._retiring[root])
                    entry = None
                else:
                    entry = self._entries.get(root)
                    entry_is_stale = (
                        entry is not None
                        and (
                            entry.invalid
                            or entry.fingerprint != fingerprint
                            or (
                                self._is_valid is not None
                                and not self._is_valid(entry.resource)
                            )
                        )
                    )
                    if entry_is_stale:
                        assert entry is not None
                        if entry.active:
                            if not create:
                                return None
                            self._require_creation_budget(
                                root,
                                deadline=deadline,
                                creation_budget=creation_budget,
                            )
                            wait = True
                        else:
                            self._require_creation_budget(
                                root,
                                deadline=deadline,
                                creation_budget=creation_budget,
                            )
                            resource = self._entries.pop(root).resource
                            self._retiring[root] = resource
                            self._retiring_active.add(root)
                            retirement = (root, resource)
                            self._condition.notify_all()
                            entry = None

                if retirement is None and not wait and entry is not None:
                    if deadline is not None and self._clock() >= deadline:
                        raise ProjectResourceBusyError(
                            f"timed out waiting for a shared Lean project slot: {root}"
                        )
                    entry.active += 1
                    entry.last_used = self._clock()
                    return entry.resource

                if retirement is None and not wait and entry is None and not create:
                    return None

                if retirement is None and not wait and root in self._creating:
                    wait = True

                if retirement is None and not wait:
                    self._require_creation_budget(
                        root,
                        deadline=deadline,
                        creation_budget=creation_budget,
                    )
                    occupied = (
                        len(self._entries)
                        + len(self._creating)
                        + len(self._retiring)
                    )
                    if occupied >= self._max_entries:
                        inactive = [
                            (candidate.last_used, path)
                            for path, candidate in self._entries.items()
                            if candidate.active == 0
                        ]
                        if inactive:
                            _, victim = min(inactive)
                            resource = self._entries.pop(victim).resource
                            self._retiring[victim] = resource
                            self._retiring_active.add(victim)
                            retirement = (victim, resource)
                            self._condition.notify_all()
                        elif any(
                            path not in self._retiring_active
                            for path in self._retiring
                        ):
                            retiring_root = next(
                                path
                                for path in self._retiring
                                if path not in self._retiring_active
                            )
                            self._retiring_active.add(retiring_root)
                            retirement = (
                                retiring_root,
                                self._retiring[retiring_root],
                            )
                        else:
                            wait = True

                if retirement is None and not wait:
                    self._creating.add(root)
                    self._condition.notify_all()
                    break

                if retirement is None:
                    wait_seconds = 0.5
                    if deadline is not None:
                        remaining = deadline - self._clock()
                        if remaining <= 0:
                            raise ProjectResourceBusyError(
                                f"timed out waiting for a shared Lean project slot: {root}"
                            )
                        wait_seconds = min(wait_seconds, remaining)
                    self._condition.wait(timeout=wait_seconds)

            if retirement is not None:
                retiring_root, retiring_resource = retirement
                if not self._retire(retiring_root, retiring_resource):
                    raise ProjectResourceBusyError(
                        "failed to retire a stale Lean project resource: "
                        f"{retiring_root}"
                    )

        if deadline is not None and self._clock() >= deadline:
            with self._condition:
                self._creating.discard(root)
                self._condition.notify_all()
            raise ProjectResourceBusyError(
                f"shared Lean project startup deadline expired: {root}"
            )

        try:
            created = self._factory(root)
        except BaseException:
            with self._condition:
                self._creating.discard(root)
                self._condition.notify_all()
            raise

        close_created = False
        startup_expired = False
        with self._condition:
            self._creating.discard(root)
            if self._closed:
                close_created = True
            elif deadline is not None and self._clock() >= deadline:
                close_created = True
                startup_expired = True
            else:
                self._entries[root] = _CacheEntry(
                    resource=created,
                    fingerprint=fingerprint,
                    last_used=self._clock(),
                    active=1,
                )
            if close_created:
                self._retiring[root] = created
                self._retiring_active.add(root)
            self._condition.notify_all()
        if close_created:
            cleaned = self._retire(root, created)
            if not cleaned:
                raise ProjectResourceBusyError(
                    f"failed to retire a late Lean project resource: {root}"
                )
            if startup_expired:
                raise ProjectResourceBusyError(
                    f"shared Lean project startup exceeded its response budget: {root}"
                )
            raise RuntimeError("project resource cache closed during startup")
        return created

    def _require_creation_budget(
        self,
        root: Path,
        *,
        deadline: float | None,
        creation_budget: float,
    ) -> None:
        if deadline is None:
            return
        if deadline - self._clock() <= creation_budget:
            raise ProjectResourceBusyError(
                f"not enough response budget to start a shared Lean project slot: {root}"
            )

    def _release(self, root: Path, resource: T) -> None:
        retirement: tuple[Path, T] | None = None
        validation_error: BaseException | None = None
        with self._condition:
            entry = self._entries.get(root)
            if entry is None or entry.resource is not resource:
                raise RuntimeError("project resource lease is no longer registered")
            entry.active -= 1
            entry.last_used = self._clock()
            invalid = entry.invalid
            if not invalid and self._is_valid is not None:
                try:
                    invalid = not self._is_valid(resource)
                except BaseException as error:
                    validation_error = error
                    invalid = True
            if entry.active == 0 and invalid:
                self._entries.pop(root)
                self._retiring[root] = resource
                self._retiring_active.add(root)
                retirement = (root, resource)
            self._condition.notify_all()
        if retirement is not None:
            try:
                self._retire(*retirement)
            except BaseException as cleanup_error:
                if validation_error is None:
                    raise
                note = f"Lean project resource cleanup also failed: {cleanup_error}"
                add_note = getattr(validation_error, "add_note", None)
                if add_note is not None:
                    add_note(note)
                else:  # pragma: no cover - Python 3.10 compatibility
                    logger.error("%s", note)
        if validation_error is not None:
            raise validation_error.with_traceback(validation_error.__traceback__)

    def _sweep(self, interval: float) -> None:
        while not self._stop_sweeper.wait(interval):
            try:
                self.evict_idle()
            except Exception:
                logger.exception("failed to evict idle Lean project resources")

    def _retire(self, root: Path, resource: T) -> bool:
        """Try one bounded close while retaining failed ownership in quarantine."""
        succeeded = False
        try:
            self._close_resource(resource)
        except Exception:
            logger.exception("failed to close Lean project resource")
            return False
        else:
            succeeded = True
            return True
        finally:
            with self._condition:
                self._retiring_active.discard(root)
                if succeeded and self._retiring.get(root) is resource:
                    self._retiring.pop(root)
                self._condition.notify_all()


class LeanRuntimeServices:
    """Runtime dispatch and ownership for all shared Lean subprocesses."""

    def __init__(
        self,
        config: LeanRuntimeConfig | None = None,
        *,
        repl_factory: Callable[[Path], LeanReplPool] | None = None,
        lsp_factory: Callable[[Path], LeanLspSession] | None = None,
        start_sweepers: bool = True,
    ) -> None:
        self.config = config or LeanRuntimeConfig.from_environment()
        self.started_at = time.monotonic()
        self.lsp_creation_budget = LSP_STARTUP_BUDGET + LSP_CLOSE_BUDGET

        def default_repl_factory(project_dir: Path) -> LeanReplPool:
            return LeanReplPool(
                LeanReplPoolConfig(
                    cwd=str(project_dir),
                    repl_command=list(self.config.repl_command),
                    num_repls=self.config.repl_workers_per_project,
                    max_retries=0,
                )
            )

        def default_lsp_factory(project_dir: Path) -> LeanLspSession:
            session = LeanLspSession(
                LspConfig(
                    cwd=str(project_dir),
                    lake_command=list(self.config.lsp_command),
                    timeout=self.config.lsp_timeout,
                )
            )
            session.start()
            return session

        def close_repl_pool(pool: LeanReplPool) -> None:
            pool.shutdown()

        self.repl_projects = ProjectResourceCache(
            repl_factory or default_repl_factory,
            close_repl_pool,
            max_entries=self.config.repl_project_limit,
            idle_seconds=self.config.idle_seconds,
            is_valid=lambda pool: getattr(pool, "is_usable", lambda: True)(),
            start_sweeper=start_sweepers,
        )
        self.lsp_projects = ProjectResourceCache(
            lsp_factory or default_lsp_factory,
            lambda session: session.close(),
            max_entries=self.config.max_projects,
            idle_seconds=self.config.idle_seconds,
            is_valid=lambda session: session.is_alive(),
            start_sweeper=start_sweepers,
        )

    def dispatch(
        self,
        method: str,
        params: dict[str, Any],
        *,
        client_deadline: float | None = None,
    ) -> Any:
        if method == "daemon.ping":
            return self.status(include_projects=False)
        if method == "daemon.status":
            return self.status(include_projects=True)
        if method == "repl.run":
            project_dir = self._string_param(params, "project_dir")
            code = self._string_param(params, "code", allow_empty=True)
            timeout = params.get("timeout")
            if timeout is None:
                effective_timeout = self.config.repl_request_timeout
            else:
                if (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout)
                    or timeout <= 0
                ):
                    raise ValueError("timeout must be a finite positive number or null")
                effective_timeout = float(timeout)
            if effective_timeout > self.config.max_repl_request_seconds:
                raise ValueError(
                    "timeout exceeds the node-wide limit of "
                    f"{self.config.max_repl_request_seconds:g} seconds"
                )
            if client_deadline is not None and (
                isinstance(client_deadline, bool)
                or not isinstance(client_deadline, (int, float))
                or not math.isfinite(client_deadline)
            ):
                raise ValueError("deadline must be a finite number or null")
            server_deadline = time.monotonic() + effective_timeout
            deadline = (
                server_deadline
                if client_deadline is None
                else min(server_deadline, float(client_deadline))
            )
            if time.monotonic() >= deadline:
                raise ProjectResourceBusyError(
                    "Lean REPL request deadline expired before admission"
                )
            repl_result: Any = None
            repl_attempted = False
            repl_completed = False
            try:
                with self.repl_projects.lease(
                    project_dir,
                    deadline=deadline,
                    creation_budget=0.0,
                ) as pool:
                    assert pool is not None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProjectResourceBusyError(
                            "Lean REPL request deadline expired before execution"
                        )
                    repl_attempted = True
                    repl_result = pool.run(code, deadline=deadline)
                    repl_completed = True
            except (ReplPoolBusyError, ReplPoolUnavailableError):
                raise
            except Exception as error:
                if not repl_attempted:
                    raise
                phase = (
                    "project resource release"
                    if repl_completed
                    else "REPL execution"
                )
                return format_repl_response(
                    {
                        "repl_error": (
                            f"Lean {phase} failed after the command may have been "
                            f"dispatched: {error}. The request must not be replayed."
                        ),
                        "outcome_unknown": True,
                    }
                )
            try:
                return format_repl_response(repl_result)
            except Exception as error:
                return format_repl_response(
                    {
                        "repl_error": (
                            "Lean REPL command produced an invalid result: "
                            f"{error}. The request must not be replayed."
                        ),
                        "outcome_unknown": True,
                    }
                )
        if method == "repl.status":
            project_dir = self._string_param(params, "project_dir")
            with self.repl_projects.lease(project_dir, create=False) as pool:
                state = (
                    "warm"
                    if pool is not None
                    else self.repl_projects.state(project_dir)
                )
                return {
                    "state": state,
                    "capacity": (
                        pool.capacity
                        if pool is not None
                        else self.config.repl_workers_per_project
                    ),
                    "memory_usage_gb": (
                        round(pool.get_memory_usage(), 2) if pool is not None else 0.0
                    ),
                    "shutdown": (
                        pool._shutdown if pool is not None else state == "retiring"
                    ),
                    "daemon_pid": os.getpid(),
                    "node_total_workers": self.config.total_repl_workers,
                }
        if method == "lsp.diagnostics":
            project_dir = self._string_param(params, "project_dir")
            file_path = self._string_param(params, "file_path")
            root, path = resolve_lean_file(project_dir, file_path)
            with self.lsp_projects.lease(
                str(root),
                acquisition_timeout=self._acquisition_timeout(self.config.lsp_timeout),
                creation_budget=self.lsp_creation_budget,
            ) as session:
                assert session is not None
                try:
                    diagnostics = session.get_diagnostics(str(path))
                except LspBusyError:
                    raise
                except (LspProtocolError, TimeoutError, OSError):
                    self.lsp_projects.invalidate(str(root), session)
                    session.abort()
                    raise
            return format_lsp_diagnostics(diagnostics)
        if method == "lsp.hover":
            project_dir = self._string_param(params, "project_dir")
            file_path = self._string_param(params, "file_path")
            line = self._integer_param(params, "line")
            character = self._integer_param(params, "character")
            if line < 0 or character < 0:
                raise ValueError("line and character must be nonnegative")
            root, path = resolve_lean_file(project_dir, file_path)
            with self.lsp_projects.lease(
                str(root),
                acquisition_timeout=self._acquisition_timeout(self.config.lsp_timeout),
                creation_budget=self.lsp_creation_budget,
            ) as session:
                assert session is not None
                try:
                    result = session.hover(str(path), line, character)
                except LspBusyError:
                    raise
                except (LspProtocolError, TimeoutError, OSError):
                    self.lsp_projects.invalidate(str(root), session)
                    session.abort()
                    raise
            return result or "No hover information at this position."
        raise ValueError(f"unknown Lean runtime method: {method}")

    def status(self, *, include_projects: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "running": True,
            "pid": os.getpid(),
            "protocol": PROTOCOL_VERSION,
            "install_id": INSTALL_ID,
            "build_generation": BUILD_GENERATION,
            "uptime_seconds": round(time.monotonic() - self.started_at, 3),
            "config": self.config.as_dict(),
        }
        if include_projects:
            result["repl_projects"] = self.repl_projects.stats()
            result["lsp_projects"] = self.lsp_projects.stats()
        return result

    def close(self) -> None:
        repl_error: BaseException | None = None
        try:
            self.repl_projects.close()
        except BaseException as error:
            repl_error = error
        try:
            self.lsp_projects.close()
        except BaseException as lsp_error:
            if repl_error is None:
                raise
            add_note = getattr(repl_error, "add_note", None)
            if add_note is not None:
                add_note(f"Lean LSP cleanup also failed: {lsp_error}")
            else:  # pragma: no cover - Python 3.10 compatibility
                logger.error("Lean LSP cleanup also failed: %s", lsp_error)
        if repl_error is not None:
            raise repl_error.with_traceback(repl_error.__traceback__)

    def _acquisition_timeout(self, operation_timeout: float) -> float:
        """Reserve enough of the RPC deadline for the admitted tool operation."""
        return (
            self.config.response_timeout
            - operation_timeout
            - RUNTIME_SAFETY_SECONDS
        )

    @staticmethod
    def _string_param(
        params: dict[str, Any],
        name: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = params.get(name)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _integer_param(params: dict[str, Any], name: str) -> int:
        value = params.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        return value


class _ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    block_on_close = True


class LeanRuntimeServer(_ThreadingUnixServer):
    """Bounded JSON-lines RPC server on a user-private Unix socket."""

    def __init__(
        self,
        socket_path: Path,
        services: LeanRuntimeServices,
    ) -> None:
        self.socket_path = socket_path
        self.services = services
        self._shutdown_started = threading.Event()
        self._connection_slots = threading.BoundedSemaphore(
            services.config.max_connections
        )
        super().__init__(str(socket_path), LeanRuntimeRequestHandler)
        socket_path.chmod(0o600)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

    def request_shutdown(self) -> None:
        if self._shutdown_started.is_set():
            return
        self._shutdown_started.set()
        threading.Thread(
            target=self.shutdown,
            name="autoform-runtime-shutdown",
            daemon=True,
        ).start()


class LeanRuntimeRequestHandler(socketserver.StreamRequestHandler):
    """Decode exactly one request and return exactly one response."""

    server: LeanRuntimeServer

    def _read_request(self) -> bytes:
        deadline = (
            time.monotonic() + self.server.services.config.rpc_read_timeout
        )
        data = bytearray()
        while len(data) <= MAX_MESSAGE_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("runtime request read deadline expired")
            self.request.settimeout(remaining)
            chunk = self.request.recv(
                min(65536, MAX_MESSAGE_BYTES + 1 - len(data))
            )
            if time.monotonic() >= deadline:
                raise TimeoutError("runtime request read deadline expired")
            if not chunk:
                raise ValueError("request is empty or unterminated")
            data.extend(chunk)
            newline = data.find(b"\n")
            if newline >= 0:
                if len(data) > MAX_MESSAGE_BYTES:
                    raise ValueError("request exceeds the message limit")
                if newline == 0:
                    raise ValueError("request is empty")
                if data[newline + 1 :]:
                    raise ValueError("request contains trailing data")
                return bytes(data[:newline])
        raise ValueError("request exceeds the message limit")

    def handle(self) -> None:
        request_id: Any = None
        method: Any = None
        client_deadline: float | None = None
        shutdown = False
        try:
            raw = self._read_request()
            request = _decode_runtime_request(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            candidate_id = request.get("id")
            if (
                type(candidate_id) is not str
                or not candidate_id
                or len(candidate_id) > MAX_REQUEST_ID_CHARS
            ):
                raise ValueError("id must be a bounded non-empty string")
            request_id = candidate_id
            version = request.get("v")
            if type(version) is not int or version != PROTOCOL_VERSION:
                raise ValueError(
                    f"protocol mismatch: expected {PROTOCOL_VERSION}, got {version!r}"
                )
            method = request.get("method")
            params = request.get("params")
            if not isinstance(method, str) or not method:
                raise ValueError("method must be a non-empty string")
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            base_keys = {"v", "id", "method", "params"}
            expected_keys = (
                base_keys | {"deadline"}
                if method == "repl.run" and "deadline" in request
                else base_keys
            )
            if set(request) != expected_keys:
                raise ValueError("request has an invalid envelope")
            candidate_deadline = request.get("deadline")
            if "deadline" in request and (
                isinstance(candidate_deadline, bool)
                or not isinstance(candidate_deadline, (int, float))
                or not math.isfinite(candidate_deadline)
            ):
                raise ValueError("deadline must be a finite number or null")
            if candidate_deadline is not None:
                client_deadline = float(candidate_deadline)

            if method == "daemon.shutdown":
                result = {"stopping": True, "pid": os.getpid()}
                shutdown = True
            else:
                result = self.server.services.dispatch(
                    method,
                    params,
                    client_deadline=client_deadline,
                )
            response = {
                "v": PROTOCOL_VERSION,
                "id": request_id,
                "ok": True,
                "result": result,
            }
        except Exception as error:
            logger.exception("Lean runtime request failed")
            response = {
                "v": PROTOCOL_VERSION,
                "id": request_id,
                "ok": False,
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }

        encoded = json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_MESSAGE_BYTES:
            if method == "repl.run" and response.get("ok") is True:
                response = {
                    "v": PROTOCOL_VERSION,
                    "id": request_id,
                    "ok": True,
                    "result": (
                        "REPL error (execution outcome unknown; request not retried): "
                        "the completed response exceeded the runtime message limit; "
                        "the request must not be replayed."
                    ),
                }
            else:
                response = {
                    "v": PROTOCOL_VERSION,
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "type": "ValueError",
                        "message": "response exceeds the message limit",
                    },
                }
            encoded = json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
        write_timeout = min(
            getattr(
                self.server.services.config,
                "response_timeout",
                DEFAULT_RESPONSE_TIMEOUT,
            ),
            RUNTIME_SAFETY_SECONDS,
        )
        write_deadline = time.monotonic() + write_timeout
        if method == "repl.run" and client_deadline is not None:
            write_deadline = min(
                write_deadline,
                client_deadline + REPL_RESPONSE_GRACE_SECONDS,
            )
        try:
            remaining = write_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("runtime response write deadline expired")
            self.request.settimeout(remaining)
            self.wfile.write(encoded)
            self.wfile.flush()
        except (TimeoutError, OSError):
            logger.warning(
                "Lean runtime client disconnected before receiving its response",
                exc_info=True,
            )
        if shutdown:
            self.server.request_shutdown()


def _close_services_until_clean(services: LeanRuntimeServices) -> None:
    """Keep terminal ownership until every quarantined child is gone."""
    delay = TERMINAL_CLEANUP_RETRY_SECONDS
    while True:
        try:
            services.close()
            return
        except Exception:
            logger.exception(
                "Lean runtime cleanup remains incomplete; retaining ownership"
            )
            time.sleep(delay)
            delay = min(delay * 2, MAX_TERMINAL_CLEANUP_RETRY_SECONDS)


def _configure_logging(log_path: Path | None) -> None:
    handlers: list[logging.Handler] = []
    if log_path is not None:
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_path,
                maxBytes=2 * 1024 * 1024,
                backupCount=2,
            )
        )
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=os.environ.get("AUTOFORM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def serve(paths: RuntimePaths) -> None:
    """Run the internal runtime in the foreground until stop or a signal."""
    _configure_logging(paths.log)
    import fcntl

    lifetime_fd = os.open(paths.lifetime_lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lifetime_fd, fcntl.LOCK_EX)
    services: LeanRuntimeServices | None = None
    server: LeanRuntimeServer | None = None
    bound_identity: tuple[int, int] | None = None
    previous_handlers: dict[int, Any] = {}
    try:
        try:
            info = paths.socket.lstat()
        except FileNotFoundError:
            pass
        else:
            kind = "socket" if stat.S_ISSOCK(info.st_mode) else "non-socket"
            raise LeanRuntimeError(
                f"runtime {kind} already exists at {paths.socket}; use start/status/stop"
            )

        services = LeanRuntimeServices()
        server = LeanRuntimeServer(paths.socket, services)
        info = paths.socket.lstat()
        bound_identity = (info.st_dev, info.st_ino)

        def request_shutdown(signum: int, frame: Any) -> None:
            logger.info("received signal %s; stopping Lean runtime", signum)
            assert server is not None
            server.request_shutdown()

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, request_shutdown)

        logger.info("Lean runtime %s listening at %s", os.getpid(), paths.socket)
        server.serve_forever(poll_interval=0.25)
    finally:
        try:
            if server is not None:
                server.server_close()
        finally:
            try:
                if services is not None:
                    _close_services_until_clean(services)
            finally:
                try:
                    info = paths.socket.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if bound_identity == (info.st_dev, info.st_ino):
                        paths.socket.unlink()
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
                logger.info("Lean runtime %s stopped", os.getpid())
                os.close(lifetime_fd)


def _paths_from_args(
    socket_path: str | None,
    log_path: str | None,
    lifetime_lock_path: str | None = None,
) -> RuntimePaths:
    paths = (
        runtime_paths_for_socket(socket_path)
        if socket_path is not None
        else default_runtime_paths()
    )
    if log_path is None and lifetime_lock_path is None:
        return paths
    log = paths.log if log_path is None else Path(log_path).expanduser()
    lifetime_lock = (
        paths.lifetime_lock
        if lifetime_lock_path is None
        else Path(lifetime_lock_path).expanduser()
    )
    if not log.is_absolute():
        raise LeanRuntimeError("Lean runtime log path must be absolute")
    if not lifetime_lock.is_absolute():
        raise LeanRuntimeError("Lean runtime lifetime lock path must be absolute")
    return RuntimePaths(
        directory=paths.directory,
        socket=paths.socket,
        lock=paths.lock,
        lifetime_lock=lifetime_lock,
        log=log,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", help="override the Unix socket path")
    parser.add_argument("--log", help="override the rotating log path")
    parser.add_argument("--lifetime-lock", help="override the runtime lifetime lock")
    parser.add_argument(
        "command",
        choices=("serve", "start", "status", "stop"),
        nargs="?",
        default="status",
    )
    args = parser.parse_args(argv)
    paths = _paths_from_args(args.socket, args.log, args.lifetime_lock)

    if args.command == "serve":
        serve(paths)
        return

    # Preserve default-path semantics so start/stop also discover and replace
    # another code generation from this same Autoform installation.
    client = LeanRuntimeClient(socket_path=args.socket)
    client.paths = paths
    if args.command == "start":
        print(json.dumps(client.ensure_running(), indent=2, sort_keys=True))
        return
    if args.command == "status":
        try:
            result = client.request("daemon.status", autostart=False)
        except LeanRuntimeUnavailable:
            print(json.dumps({"running": False, "socket": str(paths.socket)}, indent=2))
            raise SystemExit(1)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.command == "stop":
        try:
            result = client.stop()
        except LeanRuntimeUnavailable:
            result = {"stopping": False, "running": False}
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
