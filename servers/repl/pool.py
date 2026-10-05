"""Pooled Lean REPL instances with load balancing."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from .core import ReplCleanupError, LeanRepl, LeanReplConfig

logger = getLogger(__name__)

DEFAULT_RAM_FRACTION = 0.5
_CLEANUP_RETRY_INITIAL_SECONDS = 0.01
_CLEANUP_RETRY_MAX_SECONDS = 1.0


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
        self._condition = threading.Condition(self._lock)
        self._active_calls = 0

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
        for worker in reversed(self._workers):
            try:
                worker.close()
            except Exception:
                logger.exception("failed to close REPL worker")
        self._workers.clear()
        while True:
            try:
                self._idle.get_nowait()
            except queue.Empty:
                break

    def is_usable(self) -> bool:
        """Return whether this pool may admit another public call."""
        return not self._shutdown

    def _settle(self, worker: LeanRepl) -> BaseException | None:
        """Stop admission and retry cleanup until the worker owns no process.

        Returns the first cancellation-like cleanup error, for the caller to
        raise once ownership is settled.
        """
        cancelled: BaseException | None = None
        delay = _CLEANUP_RETRY_INITIAL_SECONDS
        while not worker.is_clean():
            self._shutdown = True
            try:
                worker.close()
            except BaseException as error:
                logger.exception("failed to retire Lean REPL worker; retrying")
                if cancelled is None and not isinstance(error, Exception):
                    cancelled = error
                time.sleep(delay)
                delay = min(delay * 2, _CLEANUP_RETRY_MAX_SECONDS)
        return cancelled

    def run(self, code: str, timeout: float | None = None) -> dict[str, Any]:
        """Run code on a cold slot within one queue-and-execution timeout."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._condition:
            if self._shutdown:
                raise RuntimeError("Lean REPL pool is shut down")
            self._active_calls += 1
        repl: LeanRepl | None = None
        try:
            while True:
                if self._shutdown:
                    raise RuntimeError("Lean REPL pool is shut down")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(
                        f"timed out after {timeout:g}s waiting for an idle Lean REPL"
                    )
                if repl is not None:
                    break
                try:
                    repl = self._idle.get(timeout=0.1 if remaining is None else min(0.1, remaining))
                except queue.Empty:
                    pass
            try:
                result = repl.run_disposable(code, timeout=remaining)
            except ReplCleanupError as error:
                result = error.result
            finally:
                cancelled = self._settle(repl)
            if cancelled is not None:
                raise cancelled
            return result
        finally:
            with self._condition:
                if repl is not None and not self._shutdown:
                    self._idle.put(repl)
                self._active_calls -= 1
                self._condition.notify_all()

    def get_memory_usage(self) -> float:
        """Total memory usage across all REPL instances in GB."""
        return sum(w.get_memory_usage() for w in self._workers)

    def shutdown(self) -> None:
        """Stop admission, wait for active calls to settle, then close every slot."""
        with self._condition:
            self._shutdown = True
            while self._active_calls:
                self._condition.wait()
        self._close_workers()
