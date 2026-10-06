"""Project router for Autoform's Lean REPL server."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from servers import resolve_lean_project_dir

from .pool import LeanReplPool, LeanReplPoolStartupError


class LeanReplProjects:
    """Lazily keep one REPL pool per explicit Lean project."""

    def __init__(self, pool_factory: Callable[[Path], LeanReplPool]) -> None:
        self._pool_factory = pool_factory
        self._pools: dict[Path, LeanReplPool] = {}
        self._failed: dict[Path, LeanReplPoolStartupError] = {}
        self._lock = threading.Lock()
        self._closed = False

    def get(self, project_dir: str) -> LeanReplPool:
        """Return the pool for a validated absolute Lake project."""
        root = resolve_lean_project_dir(project_dir)
        with self._lock:
            if self._closed:
                raise RuntimeError("Lean REPL project router is closed")
            failed = self._failed.get(root)
            if failed is not None:
                raise RuntimeError("Lean REPL project startup cleanup is pending") from failed
            pool = self._pools.get(root)
            if pool is None:
                try:
                    pool = self._pool_factory(root)
                except LeanReplPoolStartupError as error:
                    # The exception transfers any cleanup survivors to this
                    # router so a later shutdown can retry them.
                    self._failed[root] = error
                    raise
                self._pools[root] = pool
            return pool

    def shutdown(self) -> None:
        """Shut down all pools created by this router."""
        with self._lock:
            self._closed = True
            first_error: BaseException | None = None
            for root, pool in list(self._pools.items()):
                try:
                    pool.shutdown()
                except BaseException as error:
                    if first_error is None:
                        first_error = error
                else:
                    self._pools.pop(root, None)
            for root, error in list(self._failed.items()):
                try:
                    error.pool.shutdown()
                except BaseException as cleanup_error:
                    if first_error is None:
                        first_error = cleanup_error
                else:
                    self._failed.pop(root, None)
            if first_error is not None:
                raise RuntimeError("one or more Lean REPL project pools remain owned") from first_error
