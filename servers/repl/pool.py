"""Pooled Lean REPL instances with load balancing."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from .core import LeanRepl, LeanReplConfig

logger = getLogger(__name__)

DEFAULT_PORT = 8990
DEFAULT_RAM_FRACTION = 0.5


@dataclass
class LeanReplPoolConfig(LeanReplConfig):
    """Configuration for a pool of Lean REPL instances."""

    num_repls: int | None = None

    def __post_init__(self) -> None:
        if self.num_repls is None:
            try:
                import psutil

                total_gb = psutil.virtual_memory().total / (1024**3)
                self.num_repls = max(1, int(total_gb * DEFAULT_RAM_FRACTION / self.instance_mem_limit_gb))
            except ImportError:
                self.num_repls = 1


class LeanReplPool:
    """Pool of cold Lean REPL slots with queue-based load balancing.

    Slots retain Python wrappers, never Lean subprocesses, between calls.
    """

    def __init__(self, config: LeanReplPoolConfig) -> None:
        self.config = config
        self.capacity = config.num_repls or 1
        self._shutdown = False

        self._workers: list[LeanRepl] = []
        self._idle: queue.Queue[LeanRepl] = queue.Queue()
        self._lock = threading.Lock()

        try:
            for _ in range(self.capacity):
                repl = LeanRepl(config)
                self._workers.append(repl)
                self._idle.put(repl)
        except BaseException:
            self._close_workers()
            raise

    def _close_workers(self) -> None:
        """Close every constructed worker, preserving cleanup after one failure."""
        failed_workers = []
        first_error: BaseException | None = None
        for worker in reversed(self._workers):
            try:
                worker.close()
            except BaseException as error:
                logger.exception("failed to close REPL worker")
                failed_workers.append(worker)
                if first_error is None:
                    first_error = error
        self._workers = list(reversed(failed_workers))
        while True:
            try:
                self._idle.get_nowait()
            except queue.Empty:
                break
        if first_error is not None:
            raise first_error

    def run(self, code: str, **kwargs: Any) -> dict[str, Any]:
        """Run code on an idle REPL within one queue-and-execution timeout."""
        if self._shutdown:
            raise RuntimeError("Lean REPL pool is shut down")
        timeout = kwargs.pop("timeout", None)
        deadline = time.monotonic() + timeout if timeout is not None else None
        try:
            repl = self._idle.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError(
                f"timed out after {timeout:g}s waiting for an idle Lean REPL"
            ) from error

        def run_once() -> dict[str, Any]:
            if kwargs:
                names = ", ".join(sorted(kwargs))
                raise TypeError(f"unsupported Lean REPL pool arguments: {names}")
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out after {timeout:g}s waiting for an idle Lean REPL"
                    )
                return repl.run_disposable(code, timeout=remaining)
            return repl.run_disposable(code)

        try:
            if self._shutdown:
                raise RuntimeError("Lean REPL pool is shut down")
            return run_once()
        finally:
            if repl.is_clean() and not self._shutdown:
                self._idle.put(repl)
            else:
                # A cleanup failure keeps its wrapper (and process handle) in
                # this pool but permanently stops new admission. Runtime
                # shutdown will retry cleanup; no replacement generation starts.
                self._shutdown = True

    def get_memory_usage(self) -> float:
        """Total memory usage across all REPL instances in GB."""
        return sum(w.get_memory_usage() for w in self._workers)

    def shutdown(self) -> None:
        """Shut down all REPL instances."""
        self._shutdown = True
        self._close_workers()
