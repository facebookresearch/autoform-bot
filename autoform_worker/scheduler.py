"""Deterministic, claim-backed scheduling over the Markdown runtime projection.

The scheduler owns only ephemeral lifecycle state. The authoritative work graph
is reloaded from :mod:`autoform_cli.runtime` for every round, while cooperative
ownership is delegated to :class:`autoform_cli.claims.ClaimBoard`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from autoform_cli.claims import (
    CLAIM_HEARTBEAT_S,
    CLAIM_TTL_S,
    ClaimBoard,
    ClaimTransportError,
    author_claim_key,
)
from autoform_cli.runtime import RuntimeGraph, RuntimeNode, load_runtime_graph


class WorkPhase(str, Enum):
    """The authored fact an executor must establish next."""

    STATEMENT = "statement"
    PROOF = "proof"


class AttemptOutcome(str, Enum):
    """The executor's result for one bounded attempt."""

    SUCCEEDED = "succeeded"
    RETRY = "retry"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LifecycleStatus(str, Enum):
    """Local scheduling state layered over an immutable runtime graph."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One immutable executor input selected from a runtime projection."""

    node: RuntimeNode
    phase: WorkPhase
    attempt: int
    source_revision: str


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """One executor outcome with an optional operator-facing explanation."""

    outcome: AttemptOutcome
    detail: str = ""

    @classmethod
    def succeeded(cls, detail: str = "") -> AttemptResult:
        return cls(AttemptOutcome.SUCCEEDED, detail)

    @classmethod
    def retry(cls, detail: str = "") -> AttemptResult:
        return cls(AttemptOutcome.RETRY, detail)

    @classmethod
    def failed(cls, detail: str = "") -> AttemptResult:
        return cls(AttemptOutcome.FAILED, detail)

    @classmethod
    def cancelled(cls, detail: str = "") -> AttemptResult:
        return cls(AttemptOutcome.CANCELLED, detail)


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    """Observed lifecycle for one node within this scheduler instance."""

    status: LifecycleStatus = LifecycleStatus.PENDING
    attempts: int = 0
    detail: str = ""
    blocked_by: tuple[str, ...] = ()
    phase: WorkPhase | None = None


@dataclass(frozen=True, slots=True)
class RoundResult:
    """The result of a round, which executes at most one claimed work item."""

    item: WorkItem | None
    record: LifecycleRecord | None
    detail: str

    @property
    def progressed(self) -> bool:
        return self.item is not None


class CancellationSignal(Protocol):
    """Minimal cancellation interface accepted by worker executors."""

    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class Executor(Protocol):
    """Execution seam implemented by the prover or another bounded worker."""

    def __call__(self, item: WorkItem, cancelled: CancellationSignal) -> AttemptResult: ...


class ClaimHeartbeat(Protocol):
    lost: threading.Event

    def __enter__(self) -> ClaimHeartbeat: ...

    def __exit__(self, *exc: object) -> None: ...


class ClaimBoardLike(Protocol):
    def acquire(self, key: str, ttl: int | float = CLAIM_TTL_S, steal: bool = False, note: str = "") -> bool: ...

    def release(self, key: str) -> bool: ...

    def heartbeat(
        self,
        key: str,
        *,
        interval: float = CLAIM_HEARTBEAT_S,
        ttl: int | float = CLAIM_TTL_S,
    ) -> ClaimHeartbeat: ...


class _CombinedCancellation:
    def __init__(self, *signals: CancellationSignal) -> None:
        self._signals = signals

    def is_set(self) -> bool:
        return any(signal.is_set() for signal in self._signals)

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        if timeout is None:
            while not self.is_set():
                time.sleep(0.05)
            return True
        deadline = time.monotonic() + max(timeout, 0.0)
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
        return True


RuntimeLoader = Callable[[], RuntimeGraph]


class Scheduler:
    """Run one deterministic ready leaf per round under an author lease."""

    def __init__(
        self,
        runtime_loader: RuntimeLoader,
        board: ClaimBoardLike,
        executor: Executor,
        *,
        max_attempts: int = 3,
        claim_ttl: int | float = CLAIM_TTL_S,
        heartbeat_interval: float = CLAIM_HEARTBEAT_S,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if heartbeat_interval <= 0 or heartbeat_interval >= claim_ttl:
            raise ValueError("heartbeat_interval must be positive and shorter than claim_ttl")
        self._runtime_loader = runtime_loader
        self._board = board
        self._executor = executor
        self.max_attempts = max_attempts
        self.claim_ttl = claim_ttl
        self.heartbeat_interval = heartbeat_interval
        self._records: dict[str, LifecycleRecord] = {}
        self._lock = threading.Lock()

    @classmethod
    def for_project(
        cls,
        project_or_blueprint: str | Path,
        *,
        claim_repo: str | Path,
        worker_id: str,
        claim_scratch: str | Path,
        executor: Executor,
        lean_root: str | Path | None = None,
        max_attempts: int = 3,
        claim_ttl: int | float = CLAIM_TTL_S,
        heartbeat_interval: float = CLAIM_HEARTBEAT_S,
    ) -> Scheduler:
        """Build a scheduler using the shared runtime loader and claim board."""

        def runtime_loader() -> RuntimeGraph:
            return load_runtime_graph(project_or_blueprint, lean_root=lean_root)

        board = ClaimBoard(claim_repo, worker_id, claim_scratch)
        return cls(
            runtime_loader,
            board,
            executor,
            max_attempts=max_attempts,
            claim_ttl=claim_ttl,
            heartbeat_interval=heartbeat_interval,
        )

    def record(self, node_id: str) -> LifecycleRecord:
        """Return a snapshot of local lifecycle state for ``node_id``."""

        with self._lock:
            return self._records.get(node_id, LifecycleRecord())

    def records(self) -> dict[str, LifecycleRecord]:
        """Return a detached snapshot of every observed lifecycle record."""

        with self._lock:
            return dict(self._records)

    def cancel(self, node_id: str, detail: str = "cancelled") -> LifecycleRecord:
        """Cancel pending work; dependents become blocked on the next round."""

        with self._lock:
            current = self._records.get(node_id, LifecycleRecord())
            if current.status is LifecycleStatus.RUNNING:
                raise RuntimeError(f"cannot synchronously cancel running node {node_id!r}")
            if current.status in {LifecycleStatus.SUCCEEDED, LifecycleStatus.FAILED}:
                return current
            cancelled = replace(
                current,
                status=LifecycleStatus.CANCELLED,
                detail=detail,
                blocked_by=(),
            )
            self._records[node_id] = cancelled
            return cancelled

    def ready_items(self, runtime: RuntimeGraph | None = None) -> tuple[WorkItem, ...]:
        """Return deterministically ordered, unclaimed-candidate work items.

        Claims are intentionally not read here. Acquisition is the authoritative
        race-safe readiness check and happens in :meth:`run_once`.
        """

        runtime = runtime or self._runtime_loader()
        with self._lock:
            self._propagate_blocked(runtime)
            items: list[WorkItem] = []
            for node in runtime.nodes:
                phase = _ready_phase(node)
                if phase is None:
                    continue
                record = self._records.get(node.id, LifecycleRecord())
                if record.status is LifecycleStatus.SUCCEEDED and record.phase is not phase:
                    record = LifecycleRecord()
                    self._records[node.id] = record
                if record.status not in {LifecycleStatus.PENDING, LifecycleStatus.RETRYING}:
                    continue
                items.append(
                    WorkItem(
                        node=node,
                        phase=phase,
                        attempt=record.attempts + 1,
                        source_revision=runtime.source_revision,
                    )
                )
            return tuple(sorted(items, key=lambda item: item.node.id))

    def run_once(
        self,
        cancelled: CancellationSignal | None = None,
        *,
        node_id: str | None = None,
    ) -> RoundResult:
        """Claim and execute at most one ready leaf from a fresh projection.

        ``node_id`` restricts selection to an earlier retry target so callers can
        exhaust that work item's attempt budget without drifting to other work.
        """

        cancelled = cancelled or threading.Event()
        if cancelled.is_set():
            return RoundResult(None, None, "scheduler cancelled before selection")

        runtime = self._runtime_loader()
        candidates = self.ready_items(runtime)
        if node_id is not None:
            candidates = tuple(item for item in candidates if item.node.id == node_id)
        if not candidates:
            return RoundResult(None, None, "no ready work")

        for item in candidates:
            if cancelled.is_set():
                return RoundResult(None, None, "scheduler cancelled before claim")
            key = author_claim_key(item.node.id)
            note = f"{item.phase.value} {item.source_revision} attempt {item.attempt}"
            if not self._board.acquire(key, ttl=self.claim_ttl, note=note):
                continue
            try:
                refreshed = self._refresh_claimed(item)
                if isinstance(refreshed, RoundResult):
                    return refreshed
                return self._run_claimed(refreshed, key, cancelled)
            finally:
                self._board.release(key)
        return RoundResult(None, None, "ready work is claimed by other workers")

    def _refresh_claimed(self, item: WorkItem) -> WorkItem | RoundResult:
        runtime = self._runtime_loader()
        node = next((candidate for candidate in runtime.nodes if candidate.id == item.node.id), None)
        if node is None:
            return RoundResult(None, None, f"claimed node {item.node.id!r} no longer exists")

        phase = _ready_phase(node)
        if phase is None:
            return RoundResult(None, None, f"claimed node {item.node.id!r} is no longer ready")
        if phase is not item.phase:
            return RoundResult(
                None,
                None,
                f"claimed node {item.node.id!r} phase changed from {item.phase.value} to {phase.value}",
            )

        with self._lock:
            record = self._records.get(node.id, LifecycleRecord())
            if record.status not in {LifecycleStatus.PENDING, LifecycleStatus.RETRYING}:
                return RoundResult(None, None, f"claimed node {item.node.id!r} is no longer locally eligible")
            attempt = record.attempts + 1
        return WorkItem(
            node=node,
            phase=phase,
            attempt=attempt,
            source_revision=runtime.source_revision,
        )

    def _run_claimed(self, item: WorkItem, key: str, cancelled: CancellationSignal) -> RoundResult:
        with self._lock:
            current = self._records.get(item.node.id, LifecycleRecord())
            running = LifecycleRecord(
                status=LifecycleStatus.RUNNING,
                attempts=current.attempts + 1,
                detail="",
                phase=item.phase,
            )
            self._records[item.node.id] = running

        try:
            heartbeat = self._board.heartbeat(
                key,
                interval=self.heartbeat_interval,
                ttl=self.claim_ttl,
            )
            with heartbeat:
                signal = _CombinedCancellation(cancelled, heartbeat.lost)
                if signal.is_set():
                    result = AttemptResult.cancelled("cancelled before execution")
                else:
                    result = self._executor(item, signal)
                if not isinstance(result, AttemptResult):
                    raise TypeError("executor must return AttemptResult")
            if heartbeat.lost.is_set():
                result = AttemptResult.retry("claim ownership was lost during execution")
        except ClaimTransportError as error:
            result = AttemptResult.retry(str(error))
        except Exception as error:
            result = AttemptResult.retry(f"executor raised {type(error).__name__}: {error}")

        record = self._finish(item.node.id, item.phase, running.attempts, result)
        return RoundResult(item, record, record.detail or record.status.value)

    def _finish(
        self,
        node_id: str,
        phase: WorkPhase,
        attempts: int,
        result: AttemptResult,
    ) -> LifecycleRecord:
        if result.outcome is AttemptOutcome.SUCCEEDED:
            status = LifecycleStatus.SUCCEEDED
        elif result.outcome is AttemptOutcome.CANCELLED:
            status = LifecycleStatus.CANCELLED
        elif result.outcome is AttemptOutcome.FAILED:
            status = LifecycleStatus.FAILED
        elif attempts < self.max_attempts:
            status = LifecycleStatus.RETRYING
        else:
            status = LifecycleStatus.FAILED

        detail = result.detail
        if result.outcome is AttemptOutcome.RETRY and attempts >= self.max_attempts:
            detail = detail or f"retry limit reached after {attempts} attempts"
        record = LifecycleRecord(status=status, attempts=attempts, detail=detail, phase=phase)
        with self._lock:
            self._records[node_id] = record
        return record

    def _propagate_blocked(self, runtime: RuntimeGraph) -> None:
        terminal = {LifecycleStatus.FAILED, LifecycleStatus.CANCELLED, LifecycleStatus.BLOCKED}
        changed = True
        while changed:
            changed = False
            for node in runtime.nodes:
                current = self._records.get(node.id, LifecycleRecord())
                if current.status in {
                    LifecycleStatus.RUNNING,
                    LifecycleStatus.SUCCEEDED,
                    LifecycleStatus.FAILED,
                    LifecycleStatus.CANCELLED,
                }:
                    continue
                blocked_by = tuple(
                    dependency
                    for dependency in node.dependencies
                    if self._records.get(dependency, LifecycleRecord()).status in terminal
                )
                if blocked_by and (
                    current.status is not LifecycleStatus.BLOCKED or current.blocked_by != blocked_by
                ):
                    self._records[node.id] = replace(
                        current,
                        status=LifecycleStatus.BLOCKED,
                        detail="blocked by terminal dependency: " + ", ".join(blocked_by),
                        blocked_by=blocked_by,
                    )
                    changed = True


def _ready_phase(node: RuntimeNode) -> WorkPhase | None:
    """Return the next authored fact for an unfinished dispatchable leaf."""

    if not node.dispatchable or node.assertions.not_ready or node.mathlib:
        return None
    if not node.status.stated:
        return WorkPhase.STATEMENT if node.status.can_state else None
    if not node.status.proved:
        return WorkPhase.PROOF if node.status.can_prove else None
    return None


__all__ = [
    "AttemptOutcome",
    "AttemptResult",
    "CancellationSignal",
    "Executor",
    "LifecycleRecord",
    "LifecycleStatus",
    "RoundResult",
    "Scheduler",
    "WorkItem",
    "WorkPhase",
]
