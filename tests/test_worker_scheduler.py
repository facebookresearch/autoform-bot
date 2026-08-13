from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from autoform_cli.claims import author_claim_key
from autoform_cli.runtime import (
    RuntimeAssertions,
    RuntimeGraph,
    RuntimeNode,
    RuntimeStatus,
)
from autoform_worker import (
    AttemptResult,
    LifecycleStatus,
    Scheduler,
    WorkPhase,
)


def _node(
    node_id: str,
    *,
    stated: bool = False,
    proved: bool = False,
    can_state: bool = True,
    can_prove: bool = False,
    dependencies: tuple[str, ...] = (),
    dispatchable: bool = True,
    not_ready: bool = False,
    mathlib: bool = False,
) -> RuntimeNode:
    return RuntimeNode(
        id=node_id,
        title=node_id.title(),
        article_path=f"blueprint/roadmap/{node_id}.md",
        parent=None,
        depth=0,
        declaration="theorem",
        formalizable=True,
        dispatchable=dispatchable,
        statement_dependencies=dependencies,
        proof_dependencies=(),
        dependencies=dependencies,
        assertions=RuntimeAssertions(
            statement_formalized=stated,
            proof_formalized=proved,
            not_ready=not_ready,
        ),
        status=RuntimeStatus(
            state="proved" if proved else "can_prove" if can_prove else "can_state",
            can_state=can_state,
            can_prove=can_prove,
            stated=stated,
            proved=proved,
            fully_proved=proved,
            defined=False,
        ),
        origin=None,
        source_targets=(),
        lean_targets=(),
        mathlib=mathlib,
        mathlib_declarations=(),
        mathlib_file=None,
    )


def _runtime(*nodes: RuntimeNode) -> RuntimeGraph:
    return RuntimeGraph(
        schema="autoform-runtime/v1",
        authority="markdown-articles",
        source_revision="revision-1",
        blueprint_path="blueprint",
        nodes=nodes,
        article_count=len(nodes),
        formalizable_count=sum(node.formalizable for node in nodes),
        dispatchable_count=sum(node.dispatchable for node in nodes),
        dependency_count=sum(len(node.dependencies) for node in nodes),
        maximum_depth=max((node.depth for node in nodes), default=0),
    )


class FakeHeartbeat:
    def __init__(self, *, lose_on_exit: bool = False) -> None:
        self.lost = threading.Event()
        self.lose_on_exit = lose_on_exit
        self.entered = False
        self.exited = False

    def __enter__(self) -> FakeHeartbeat:
        self.entered = True
        return self

    def __exit__(self, *exc: object) -> None:
        if self.lose_on_exit:
            self.lost.set()
        self.exited = True


@dataclass
class FakeBoard:
    unavailable: set[str] | None = None
    lose_heartbeat: bool = False

    def __post_init__(self) -> None:
        self.unavailable = set(self.unavailable or ())
        self.acquired: list[tuple[str, int | float, str]] = []
        self.released: list[str] = []
        self.heartbeats: list[FakeHeartbeat] = []

    def acquire(self, key: str, ttl: int | float = 1500, steal: bool = False, note: str = "") -> bool:
        self.acquired.append((key, ttl, note))
        return key not in self.unavailable

    def release(self, key: str) -> bool:
        self.released.append(key)
        return True

    def heartbeat(self, key: str, *, interval: float = 300, ttl: int | float = 1500) -> FakeHeartbeat:
        heartbeat = FakeHeartbeat(lose_on_exit=self.lose_heartbeat)
        self.heartbeats.append(heartbeat)
        return heartbeat


def test_ready_items_are_sorted_and_distinguish_statement_from_proof() -> None:
    runtime = _runtime(
        _node("z-statement"),
        _node("a-proof", stated=True, can_prove=True),
        _node("not-ready", not_ready=True),
        _node("chapter", dispatchable=False),
        _node("complete", stated=True, proved=True, can_prove=True),
        _node("mathlib", stated=True, proved=True, mathlib=True),
    )
    scheduler = Scheduler(lambda: runtime, FakeBoard(), lambda item, cancelled: AttemptResult.succeeded())

    items = scheduler.ready_items()

    assert [(item.node.id, item.phase, item.attempt) for item in items] == [
        ("a-proof", WorkPhase.PROOF, 1),
        ("z-statement", WorkPhase.STATEMENT, 1),
    ]
    assert all(item.source_revision == runtime.source_revision for item in items)


def test_fresh_projection_advances_successful_statement_to_proof() -> None:
    runtimes = iter(
        (
            _runtime(_node("advance")),
            _runtime(_node("advance", stated=True, can_prove=True)),
        )
    )
    current = [next(runtimes)]
    phases = []

    def load_runtime():
        return current[0]

    def execute(item, cancelled):
        phases.append((item.phase, item.attempt))
        return AttemptResult.succeeded()

    scheduler = Scheduler(
        load_runtime,
        FakeBoard(),
        execute,
        claim_ttl=60,
        heartbeat_interval=5,
    )

    statement = scheduler.run_once()
    current[0] = next(runtimes)
    proof = scheduler.run_once()
    unchanged = scheduler.run_once()

    assert statement.item is not None and statement.item.phase is WorkPhase.STATEMENT
    assert proof.item is not None and proof.item.phase is WorkPhase.PROOF
    assert proof.record is not None and proof.record.attempts == 1
    assert phases == [(WorkPhase.STATEMENT, 1), (WorkPhase.PROOF, 1)]
    assert not unchanged.progressed


def test_run_once_skips_contended_claim_and_executes_one_ready_leaf() -> None:
    runtime = _runtime(_node("b"), _node("a"))
    first_key = author_claim_key("a")
    board = FakeBoard(unavailable={first_key})
    executed = []

    def execute(item, cancelled):
        executed.append((item.node.id, cancelled.is_set()))
        return AttemptResult.succeeded("landed")

    scheduler = Scheduler(
        lambda: runtime,
        board,
        execute,
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()

    second_key = author_claim_key("b")
    assert result.progressed
    assert result.item is not None and result.item.node.id == "b"
    assert result.record == scheduler.record("b")
    assert result.record is not None and result.record.status is LifecycleStatus.SUCCEEDED
    assert executed == [("b", False)]
    assert [key for key, _, _ in board.acquired] == [first_key, second_key]
    assert board.released == [second_key]
    assert board.heartbeats[0].entered and board.heartbeats[0].exited
    assert "revision-1" in board.acquired[-1][2]


def test_retry_is_requeued_then_exhaustion_becomes_terminal_failure() -> None:
    runtime = _runtime(_node("retry-me"))
    board = FakeBoard()
    attempts = []

    def execute(item, cancelled):
        attempts.append(item.attempt)
        return AttemptResult.retry("temporary prover failure")

    scheduler = Scheduler(
        lambda: runtime,
        board,
        execute,
        max_attempts=2,
        claim_ttl=60,
        heartbeat_interval=5,
    )

    first = scheduler.run_once()
    second = scheduler.run_once()
    third = scheduler.run_once()

    assert first.record is not None and first.record.status is LifecycleStatus.RETRYING
    assert second.record is not None and second.record.status is LifecycleStatus.FAILED
    assert second.record.attempts == 2
    assert second.record.detail == "temporary prover failure"
    assert attempts == [1, 2]
    assert not third.progressed
    assert third.detail == "no ready work"


def test_exception_is_retryable_and_claim_is_always_released() -> None:
    runtime = _runtime(_node("raises"))
    board = FakeBoard()

    def execute(item, cancelled):
        raise OSError("tool disappeared")

    scheduler = Scheduler(
        lambda: runtime,
        board,
        execute,
        max_attempts=2,
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()

    assert result.record is not None
    assert result.record.status is LifecycleStatus.RETRYING
    assert "OSError: tool disappeared" in result.record.detail
    assert board.released == [author_claim_key("raises")]


def test_cancellation_and_failure_propagate_through_dependencies() -> None:
    runtime = _runtime(
        _node("root"),
        _node("child", dependencies=("root",), can_state=False),
        _node("grandchild", dependencies=("child",), can_state=False),
        _node("independent"),
    )
    board = FakeBoard()
    scheduler = Scheduler(
        lambda: runtime,
        board,
        lambda item, cancelled: AttemptResult.succeeded(),
        claim_ttl=60,
        heartbeat_interval=5,
    )

    cancelled = scheduler.cancel("root", "operator stopped work")
    ready = scheduler.ready_items()

    assert cancelled.status is LifecycleStatus.CANCELLED
    assert scheduler.record("child").status is LifecycleStatus.BLOCKED
    assert scheduler.record("child").blocked_by == ("root",)
    assert scheduler.record("grandchild").status is LifecycleStatus.BLOCKED
    assert scheduler.record("grandchild").blocked_by == ("child",)
    assert [item.node.id for item in ready] == ["independent"]


def test_executor_cancellation_is_terminal_and_blocks_dependents() -> None:
    runtime = _runtime(
        _node("a-root"),
        _node("dependent", dependencies=("a-root",), can_state=False),
    )
    board = FakeBoard()
    scheduler = Scheduler(
        lambda: runtime,
        board,
        lambda item, cancelled: AttemptResult.cancelled("shutdown requested"),
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()
    scheduler.ready_items()

    assert result.record is not None and result.record.status is LifecycleStatus.CANCELLED
    assert scheduler.record("dependent").status is LifecycleStatus.BLOCKED
    assert scheduler.record("dependent").blocked_by == ("a-root",)


def test_lost_heartbeat_overrides_success_and_retries_fail_closed() -> None:
    runtime = _runtime(_node("lease-sensitive"))
    board = FakeBoard(lose_heartbeat=True)
    scheduler = Scheduler(
        lambda: runtime,
        board,
        lambda item, cancelled: AttemptResult.succeeded("executor completed"),
        max_attempts=2,
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()

    assert result.record is not None
    assert result.record.status is LifecycleStatus.RETRYING
    assert result.record.detail == "claim ownership was lost during execution"


def test_preselection_cancellation_does_not_claim_or_execute() -> None:
    runtime = _runtime(_node("ready"))
    board = FakeBoard()
    cancel = threading.Event()
    cancel.set()
    executed = False

    def execute(item, cancelled):
        nonlocal executed
        executed = True
        return AttemptResult.succeeded()

    scheduler = Scheduler(lambda: runtime, board, execute)

    result = scheduler.run_once(cancel)

    assert not result.progressed
    assert result.detail == "scheduler cancelled before selection"
    assert board.acquired == []
    assert not executed


def test_constructor_rejects_invalid_retry_and_heartbeat_settings() -> None:
    runtime = _runtime()
    with pytest.raises(ValueError, match="max_attempts"):
        Scheduler(lambda: runtime, FakeBoard(), lambda item, cancelled: AttemptResult.succeeded(), max_attempts=0)
    with pytest.raises(ValueError, match="heartbeat_interval"):
        Scheduler(
            lambda: runtime,
            FakeBoard(),
            lambda item, cancelled: AttemptResult.succeeded(),
            claim_ttl=5,
            heartbeat_interval=5,
        )
