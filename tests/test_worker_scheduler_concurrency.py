from __future__ import annotations

import threading

from autoform_cli.runtime import RuntimeAssertions, RuntimeGraph, RuntimeNode, RuntimeStatus
from autoform_worker.scheduler import AttemptResult, Scheduler, WorkPhase


def _node(
    node_id: str,
    *,
    stated: bool = False,
    can_state: bool = True,
    can_prove: bool = False,
) -> RuntimeNode:
    return RuntimeNode(
        id=node_id,
        title=node_id.title(),
        article_path=f"blueprint/roadmap/{node_id}.md",
        parent=None,
        depth=0,
        declaration="theorem",
        formalizable=True,
        dispatchable=True,
        statement_dependencies=(),
        proof_dependencies=(),
        dependencies=(),
        assertions=RuntimeAssertions(
            statement_formalized=stated,
            proof_formalized=False,
            not_ready=False,
        ),
        status=RuntimeStatus(
            state="can_prove" if can_prove else "can_state",
            can_state=can_state,
            can_prove=can_prove,
            stated=stated,
            proved=False,
            fully_proved=False,
            defined=False,
        ),
        origin=None,
        source_targets=(),
        lean_targets=(),
        mathlib=False,
        mathlib_declarations=(),
        mathlib_file=None,
    )


def _runtime(revision: str, *nodes: RuntimeNode) -> RuntimeGraph:
    return RuntimeGraph(
        schema="autoform-runtime/v1",
        authority="markdown-articles",
        source_revision=revision,
        blueprint_path="blueprint",
        nodes=nodes,
        article_count=len(nodes),
        formalizable_count=len(nodes),
        dispatchable_count=len(nodes),
        dependency_count=0,
        maximum_depth=0,
    )


class _Heartbeat:
    def __init__(self) -> None:
        self.lost = threading.Event()

    def __enter__(self) -> _Heartbeat:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class _Board:
    def __init__(self, on_acquire) -> None:
        self._on_acquire = on_acquire
        self.released: list[str] = []
        self.heartbeat_keys: list[str] = []

    def acquire(self, key: str, ttl: int | float = 1500, steal: bool = False, note: str = "") -> bool:
        self._on_acquire()
        return True

    def release(self, key: str) -> bool:
        self.released.append(key)
        return True

    def heartbeat(self, key: str, *, interval: float = 300, ttl: int | float = 1500) -> _Heartbeat:
        self.heartbeat_keys.append(key)
        return _Heartbeat()


def test_run_once_executes_refreshed_node_after_claim_acquisition() -> None:
    original = _node("target")
    refreshed = _node("target")
    current = [_runtime("revision-1", original)]
    executed = []

    def refresh_during_acquire() -> None:
        current[0] = _runtime("revision-2", refreshed)

    scheduler = Scheduler(
        lambda: current[0],
        _Board(refresh_during_acquire),
        lambda item, cancelled: executed.append(item) or AttemptResult.succeeded(),
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()

    assert result.item is not None
    assert result.item.node is refreshed
    assert result.item.source_revision == "revision-2"
    assert executed == [result.item]


def test_run_once_does_not_execute_when_claimed_node_disappears() -> None:
    current = [_runtime("revision-1", _node("target"))]
    board = _Board(lambda: current.__setitem__(0, _runtime("revision-2")))
    executed = []
    scheduler = Scheduler(
        lambda: current[0],
        board,
        lambda item, cancelled: executed.append(item) or AttemptResult.succeeded(),
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()

    assert not result.progressed
    assert "no longer exists" in result.detail
    assert executed == []
    assert board.heartbeat_keys == []
    assert len(board.released) == 1


def test_run_once_does_not_execute_when_claimed_phase_changes() -> None:
    current = [_runtime("revision-1", _node("target"))]
    board = _Board(
        lambda: current.__setitem__(
            0,
            _runtime("revision-2", _node("target", stated=True, can_state=False, can_prove=True)),
        )
    )
    executed = []
    scheduler = Scheduler(
        lambda: current[0],
        board,
        lambda item, cancelled: executed.append(item) or AttemptResult.succeeded(),
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()

    assert not result.progressed
    assert "phase changed from statement to proof" in result.detail
    assert executed == []
    assert scheduler.record("target").attempts == 0
    assert scheduler.ready_items(current[0])[0].phase is WorkPhase.PROOF
