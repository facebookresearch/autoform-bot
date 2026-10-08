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

DEFAULT_RAM_FRACTION = 0.5
DEFAULT_STARTUP_STAGGER_SECONDS = 2.0


@dataclass
class LeanReplPoolConfig(LeanReplConfig):
    """Configuration for a pool of Lean REPL instances."""

    num_repls: int | None = None
    startup_stagger: float = DEFAULT_STARTUP_STAGGER_SECONDS

    def __post_init__(self) -> None:
        if self.num_repls is None:
            try:
                import psutil

                total_gb = psutil.virtual_memory().total / (1024**3)
                self.num_repls = max(1, int(total_gb * DEFAULT_RAM_FRACTION / self.instance_mem_limit_gb))
            except ImportError:
                self.num_repls = 1


class LeanReplPoolStartupError(RuntimeError):
    """Pool construction failed while this object still owns workers."""

    def __init__(self, pool: LeanReplPool, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.pool = pool
        self.cause = cause


class LeanReplPool:
    """Pool of Lean REPL instances with queue-based load balancing.

    Each worker thread owns its own LeanRepl subprocess. Tasks are
    distributed to idle workers via a FIFO queue.
    """

    def __init__(self, config: LeanReplPoolConfig) -> None:
        self.config = config
        self.capacity = config.num_repls or 1
        self._shutdown = False

        self._workers: list[LeanRepl] = []
        self._idle: queue.Queue[LeanRepl] = queue.Queue()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._active = 0

        try:
            for i in range(self.capacity):
                if i > 0:
                    time.sleep(config.startup_stagger)
                repl = LeanRepl(config)
                self._workers.append(repl)
                repl.start()
                self._idle.put(repl)
        except BaseException as error:
            self._shutdown = True
            try:
                self._close_workers()
            except BaseException as cleanup_error:
                raise LeanReplPoolStartupError(self, error) from cleanup_error
            raise

    def _close_workers(self) -> None:
        """Close every constructed worker, preserving cleanup after one failure."""
        survivors: list[LeanRepl] = []
        first_error: BaseException | None = None
        for worker in reversed(self._workers):
            try:
                worker.close()
            except BaseException as error:
                logger.exception("failed to close REPL worker")
                survivors.append(worker)
                if first_error is None:
                    first_error = error
        self._workers = list(reversed(survivors))
        while True:
            try:
                self._idle.get_nowait()
            except queue.Empty:
                break
        if first_error is not None:
            raise RuntimeError("one or more Lean REPL workers remain owned") from first_error

    def run(self, code: str, **kwargs: Any) -> dict[str, Any]:
        """Run code on an idle REPL within one queue-and-execution timeout."""
        timeout = kwargs.pop("timeout", None)
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._condition:
            if self._shutdown:
                raise RuntimeError("Lean REPL pool is shut down")
            self._active += 1
        repl: LeanRepl | None = None
        try:
            try:
                repl = self._idle.get(timeout=timeout)
            except queue.Empty as error:
                raise TimeoutError(
                    f"timed out after {timeout:g}s waiting for an idle Lean REPL"
                ) from error

            call_kwargs = dict(kwargs)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out after {timeout:g}s waiting for an idle Lean REPL"
                    )
                call_kwargs["timeout"] = remaining
            return repl.run(code, **call_kwargs)
        finally:
            with self._condition:
                if repl is not None:
                    self._idle.put(repl)
                self._active -= 1
                self._condition.notify_all()

    def get_memory_usage(self) -> float:
        """Total memory usage across all REPL instances in GB."""
        with self._lock:
            workers = tuple(self._workers)
        return sum(worker.get_memory_usage() for worker in workers)

    def shutdown(self) -> None:
        """Shut down all REPL instances."""
        with self._condition:
            self._shutdown = True
            while self._active:
                self._condition.wait(timeout=0.5)
            self._close_workers()
