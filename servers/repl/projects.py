"""Project router for Autoform's Lean REPL server."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from servers import resolve_lean_project_dir

from .pool import LeanReplPool


class LeanReplProjects:
    """Lazily keep one REPL pool per explicit Lean project."""

    def __init__(self, pool_factory: Callable[[Path], LeanReplPool]) -> None:
        self._pool_factory = pool_factory
        self._pools: dict[Path, LeanReplPool] = {}
        self._lock = threading.Lock()
        self._closed = False

    def get(self, project_dir: str) -> LeanReplPool:
        """Return the pool for a validated absolute Lake project."""
        root = resolve_lean_project_dir(project_dir)
        with self._lock:
            if self._closed:
                raise RuntimeError("Lean REPL project router is closed")
            pool = self._pools.get(root)
            if pool is None:
                pool = self._pool_factory(root)
                self._pools[root] = pool
            return pool

    def shutdown(self) -> None:
        """Shut down all pools created by this router."""
        with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
            self._closed = True
        for pool in pools:
            pool.shutdown()
