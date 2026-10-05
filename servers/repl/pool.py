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
        self._closing = False
        self._closed = False

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

    def is_usable(self) -> bool:
        """Return whether this pool may admit another public call."""
        with self._condition:
            return not self._shutdown and not self._closed

    def _stop_admission(self) -> None:
        with self._condition:
            self._shutdown = True
            self._condition.notify_all()

    @staticmethod
    def _remember_cleanup_base_error(
        remembered: BaseException | None,
        error: BaseException,
    ) -> BaseException | None:
        """Retain cancellation-like cleanup failures until ownership is settled."""
        if isinstance(error, Exception):
            return remembered
        if remembered is None:
            return error
        add_note = getattr(remembered, "add_note", None)
        if add_note is not None:
            add_note(f"Lean REPL cleanup also raised: {error}")
        return remembered

    def _stop_admission_with_retry(
        self,
        cleanup_base_error: BaseException | None,
        delay: float,
    ) -> tuple[BaseException | None, float]:
        """Stop admission before cleanup despite cancellation-like interruptions."""
        while True:
            try:
                self._stop_admission()
            except BaseException as error:
                cleanup_base_error = self._remember_cleanup_base_error(
                    cleanup_base_error,
                    error,
                )
                logger.error(
                    "failed to stop Lean REPL pool admission; retrying",
                    exc_info=(type(error), error, error.__traceback__),
                )
            else:
                return cleanup_base_error, delay

            try:
                time.sleep(delay)
            except BaseException as error:
                cleanup_base_error = self._remember_cleanup_base_error(
                    cleanup_base_error,
                    error,
                )
            delay = min(delay * 2, _CLEANUP_RETRY_MAX_SECONDS)

    def _settle_worker_once(
        self,
        worker: LeanRepl,
        *,
        quarantine: bool,
    ) -> BaseException | None:
        """Retain and retry a worker until it verifiably owns no process."""
        cleanup_base_error: BaseException | None = None
        delay = _CLEANUP_RETRY_INITIAL_SECONDS
        if quarantine:
            cleanup_base_error, delay = self._stop_admission_with_retry(
                cleanup_base_error,
                delay,
            )
        while True:
            try:
                clean = worker.is_clean()
            except BaseException as error:
                cleanup_base_error = self._remember_cleanup_base_error(
                    cleanup_base_error,
                    error,
                )
                logger.error(
                    "failed to verify Lean REPL worker cleanup",
                    exc_info=(type(error), error, error.__traceback__),
                )
                clean = False

            if clean:
                return cleanup_base_error

            if not quarantine:
                cleanup_base_error, delay = self._stop_admission_with_retry(
                    cleanup_base_error,
                    delay,
                )
                quarantine = True

            try:
                worker.close()
            except BaseException as error:
                cleanup_base_error = self._remember_cleanup_base_error(
                    cleanup_base_error,
                    error,
                )
                logger.error(
                    "failed to retire quarantined Lean REPL worker; retrying",
                    exc_info=(type(error), error, error.__traceback__),
                )

            try:
                clean = worker.is_clean()
            except BaseException as error:
                cleanup_base_error = self._remember_cleanup_base_error(
                    cleanup_base_error,
                    error,
                )
                logger.error(
                    "failed to verify quarantined Lean REPL worker cleanup",
                    exc_info=(type(error), error, error.__traceback__),
                )
                clean = False
            if clean:
                return cleanup_base_error

            try:
                time.sleep(delay)
            except BaseException as error:
                cleanup_base_error = self._remember_cleanup_base_error(
                    cleanup_base_error,
                    error,
                )
            delay = min(delay * 2, _CLEANUP_RETRY_MAX_SECONDS)

    def _settle_worker(
        self,
        worker: LeanRepl,
        *,
        quarantine: bool,
    ) -> BaseException | None:
        """Retry the complete settlement protocol until cleanup is verified."""
        cleanup_base_error: BaseException | None = None
        delay = _CLEANUP_RETRY_INITIAL_SECONDS
        while True:
            try:
                settled_error = self._settle_worker_once(
                    worker,
                    quarantine=quarantine,
                )
            except BaseException as error:
                cleanup_base_error = self._remember_cleanup_base_error(
                    cleanup_base_error,
                    error,
                )
                logger.error(
                    "Lean REPL settlement failed unexpectedly; retrying",
                    exc_info=(type(error), error, error.__traceback__),
                )
                quarantine = True
                try:
                    time.sleep(delay)
                except BaseException as sleep_error:
                    cleanup_base_error = self._remember_cleanup_base_error(
                        cleanup_base_error,
                        sleep_error,
                    )
                delay = min(delay * 2, _CLEANUP_RETRY_MAX_SECONDS)
                continue
            if settled_error is not None and settled_error is not cleanup_base_error:
                cleanup_base_error = self._remember_cleanup_base_error(
                    cleanup_base_error,
                    settled_error,
                )
            return cleanup_base_error

    def run(self, code: str, **kwargs: Any) -> dict[str, Any]:
        """Run code on an idle REPL within one queue-and-execution timeout."""
        timeout = kwargs.pop("timeout", None)
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._condition:
            if self._shutdown:
                raise RuntimeError("Lean REPL pool is shut down")
            self._active_calls += 1
        repl: LeanRepl | None = None

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
                assert repl is not None
                return repl.run_disposable(code, timeout=remaining)
            assert repl is not None
            return repl.run_disposable(code)

        worker_settled = False
        try:
            while repl is None:
                with self._condition:
                    if self._shutdown:
                        raise RuntimeError("Lean REPL pool is shut down")
                wait = 0.1
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"timed out after {timeout:g}s waiting for an idle Lean REPL"
                        )
                    wait = min(wait, remaining)
                try:
                    repl = self._idle.get(timeout=wait)
                except queue.Empty:
                    continue
            call_result: dict[str, Any] | None = None
            call_error: BaseException | None = None
            try:
                with self._condition:
                    if self._shutdown:
                        raise RuntimeError("Lean REPL pool is shut down")
                call_result = run_once()
            except BaseException as error:
                call_error = error

            cleanup_base_error = self._settle_worker(
                repl,
                quarantine=isinstance(call_error, ReplCleanupError),
            )
            worker_settled = True

            if call_error is not None and not isinstance(call_error, Exception):
                if cleanup_base_error is not None and cleanup_base_error is not call_error:
                    add_note = getattr(call_error, "add_note", None)
                    if add_note is not None:
                        add_note(
                            "Lean REPL cleanup also raised: "
                            f"{cleanup_base_error}"
                        )
                raise call_error.with_traceback(call_error.__traceback__)
            if cleanup_base_error is not None:
                if call_error is not None:
                    add_note = getattr(cleanup_base_error, "add_note", None)
                    if add_note is not None:
                        add_note(f"Lean REPL call also failed: {call_error}")
                raise cleanup_base_error.with_traceback(
                    cleanup_base_error.__traceback__
                )
            if isinstance(call_error, ReplCleanupError):
                return call_error.result
            if call_error is not None:
                raise call_error.with_traceback(call_error.__traceback__)
            if call_result is None:
                raise RuntimeError("Lean REPL worker returned no result")
            return call_result
        finally:
            emergency_cleanup_error: BaseException | None = None
            try:
                if repl is not None and not worker_settled:
                    emergency_cleanup_error = self._settle_worker(
                        repl,
                        quarantine=True,
                    )
                    worker_settled = True
            finally:
                with self._condition:
                    if repl is not None and not worker_settled:
                        self._shutdown = True
                    else:
                        if repl is not None and not self._shutdown:
                            self._idle.put(repl)
                        self._active_calls -= 1
                    self._condition.notify_all()
            if emergency_cleanup_error is not None:
                raise emergency_cleanup_error.with_traceback(
                    emergency_cleanup_error.__traceback__
                )

    def get_memory_usage(self) -> float:
        """Total memory usage across all REPL instances in GB."""
        return sum(w.get_memory_usage() for w in self._workers)

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
        try:
            self._close_workers()
        finally:
            with self._condition:
                self._closing = False
                self._closed = not self._workers
                self._condition.notify_all()
