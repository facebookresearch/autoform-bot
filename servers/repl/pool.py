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
DEFAULT_STARTUP_STAGGER_SECONDS = 2.0
DEFAULT_CLEANUP_RETRY_SECONDS = 0.05
MAX_CLEANUP_RETRY_SECONDS = 1.0


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
        self._active_calls = 0
        self._closing = False
        self._closed = False

        try:
            for i in range(self.capacity):
                if i > 0:
                    import time

                    time.sleep(config.startup_stagger)
                repl = LeanRepl(config)
                self._workers.append(repl)
                repl.start()
                self._idle.put(repl)
        except BaseException:
            self._close_workers_until_clean()
            raise

    def _close_workers(self) -> None:
        """Close every worker and retain any whose cleanup failed."""
        failed_workers = []
        first_error: Exception | None = None
        for worker in self._workers:
            try:
                worker.close()
            except Exception as error:
                logger.exception("failed to close REPL worker")
                failed_workers.append(worker)
                if first_error is None:
                    first_error = error
        self._workers = failed_workers
        while True:
            try:
                self._idle.get_nowait()
            except queue.Empty:
                break
        if first_error is not None:
            raise first_error

    def _close_workers_until_clean(self) -> None:
        """Retry idempotent worker cleanup without releasing ownership."""
        delay = DEFAULT_CLEANUP_RETRY_SECONDS
        while self._workers:
            try:
                self._close_workers()
            except Exception:
                logger.exception("REPL cleanup failed; retrying")
                time.sleep(delay)
                delay = min(delay * 2, MAX_CLEANUP_RETRY_SECONDS)

    def run(
        self,
        code: str,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run code on an idle REPL within one queue-and-execution timeout."""
        if deadline is None and timeout is not None:
            deadline = time.monotonic() + timeout
        elif deadline is not None and timeout is not None:
            raise TypeError("pass timeout or deadline, not both")
        with self._condition:
            if self._shutdown:
                raise RuntimeError("Lean REPL pool is shut down")
            self._active_calls += 1
        repl: LeanRepl | None = None
        reusable = False

        try:
            while repl is None:
                with self._condition:
                    if self._shutdown:
                        raise RuntimeError("Lean REPL pool is shut down")
                wait = 0.1
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("timed out waiting for an idle Lean REPL")
                    wait = min(wait, remaining)
                try:
                    repl = self._idle.get(timeout=wait)
                except queue.Empty:
                    continue
            with self._condition:
                if self._shutdown:
                    raise RuntimeError("Lean REPL pool is shut down")
            reusable = True

            if deadline is not None:
                if deadline - time.monotonic() <= 0:
                    raise TimeoutError("timed out waiting for an idle Lean REPL")
            try:
                response = repl.run(code, deadline=deadline, **kwargs)
                return response
            except BaseException as request_error:
                try:
                    repl.close()
                except BaseException as cleanup_error:
                    reusable = False
                    logger.exception("failed to retire REPL worker after request error")
                    with self._condition:
                        self._shutdown = True
                        self._condition.notify_all()
                    raise cleanup_error from request_error
                raise
        finally:
            if repl is not None:
                with self._condition:
                    if reusable and not self._shutdown:
                        self._idle.put(repl)
            with self._condition:
                self._active_calls -= 1
                self._condition.notify_all()

    def get_memory_usage(self) -> float:
        """Total memory usage across all REPL instances in GB."""
        return sum(w.get_memory_usage() for w in self._workers)

    def is_usable(self) -> bool:
        """Return whether the pool can admit another request."""
        with self._condition:
            return not self._shutdown and not self._closed

    def shutdown(self) -> None:
        """Shut down all REPL instances."""
        with self._condition:
            self._shutdown = True
            self._condition.notify_all()
            while self._active_calls:
                self._condition.wait()
            while self._closing:
                self._condition.wait()
            if self._closed:
                return
            self._closing = True
        error: BaseException | None = None
        try:
            self._close_workers()
        except BaseException as caught:
            error = caught
        finally:
            with self._condition:
                self._closing = False
                self._closed = not self._workers
                self._condition.notify_all()
        if error is not None:
            raise error
