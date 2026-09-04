"""List work that is ready for a host agent to formalize."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .execution_input import (
    ExecutionInput,
    ExecutionInputError,
    ExecutionInputIssue,
    load_execution_input,
)
from .runtime import RuntimeNode, RuntimeStatus


READY_SCHEMA = "autoform-ready/v1"


@dataclass(frozen=True, order=True, slots=True)
class ReadyItem:
    """One formalization phase whose authored prerequisites are satisfied."""

    node_id: str
    article_id: str
    article_path: str
    title: str
    phase: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, order=True, slots=True)
class ReadyBlock:
    """One dispatchable phase that cannot start from the current graph state."""

    node_id: str
    article_id: str
    article_path: str
    title: str
    phase: str
    blocked_by: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "article_id": self.article_id,
            "article_path": self.article_path,
            "blocked_by": list(self.blocked_by),
            "node_id": self.node_id,
            "phase": self.phase,
            "reasons": list(self.reasons),
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class ReadyResult:
    """A deterministic work projection over one immutable execution input."""

    source_revision: str
    source_contract_sha256: str
    items: tuple[ReadyItem, ...]
    blocked_items: tuple[ReadyBlock, ...]
    blocked: int
    complete: int
    workspace_project_id: str | None
    workspace_project_binding_sha256: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "blocked": self.blocked,
            "blocked_items": [item.as_dict() for item in self.blocked_items],
            "complete": self.complete,
            "items": [item.as_dict() for item in self.items],
            "schema": READY_SCHEMA,
            "source_contract_sha256": self.source_contract_sha256,
            "source_revision": self.source_revision,
            "workspace": {
                "project_binding_sha256": self.workspace_project_binding_sha256,
                "project_id": self.workspace_project_id,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def list_ready_work(
    project_or_blueprint: str | Path,
    *,
    lean_root: str | Path | None = None,
    project_id: str | None = None,
) -> ReadyResult:
    """Load the exhaustive source contract and list its ready leaf phases."""

    execution = load_execution_input(
        project_or_blueprint,
        lean_root=lean_root,
        project_id=project_id,
    )
    completion_issues = _completion_issues(execution)
    if completion_issues:
        raise ExecutionInputError(completion_issues)

    items: list[ReadyItem] = []
    blocked_items: list[ReadyBlock] = []
    complete = 0
    statuses = {node.id: node.status for node in execution.runtime.nodes}
    for node in execution.runtime.nodes:
        if not node.dispatchable:
            continue
        phase = _ready_phase(node)
        if phase is not None:
            assert node.article_id is not None
            items.append(
                ReadyItem(
                    node_id=node.id,
                    article_id=node.article_id,
                    article_path=node.article_path,
                    title=node.title,
                    phase=phase,
                )
            )
        elif node.mathlib or node.status.proved:
            complete += 1
        else:
            blocked_items.append(_blocked_item(node, statuses))
    return ReadyResult(
        source_revision=execution.runtime.source_revision,
        source_contract_sha256=execution.source_contract_sha256,
        items=tuple(sorted(items)),
        blocked_items=tuple(sorted(blocked_items)),
        blocked=len(blocked_items),
        complete=complete,
        workspace_project_id=execution.workspace_project_id,
        workspace_project_binding_sha256=execution.workspace_project_binding_sha256,
    )


def _ready_phase(node: RuntimeNode) -> str | None:
    if not node.dispatchable or node.assertions.not_ready or node.mathlib:
        return None
    if not node.status.stated:
        return "statement" if node.status.can_state else None
    if not node.status.proved:
        return "proof" if node.status.can_prove else None
    return None


def _completion_issues(execution: ExecutionInput) -> list[ExecutionInputIssue]:
    issues: list[ExecutionInputIssue] = []
    for node in execution.runtime.nodes:
        if not node.dispatchable:
            continue
        if node.mathlib:
            if not node.mathlib_declarations or not node.mathlib_file:
                issues.append(
                    ExecutionInputIssue(
                        "mathlib-evidence-missing",
                        f"{node.article_path}: mathlib completion lacks declaration or file evidence",
                    )
                )
            continue
        if not (
            node.assertions.statement_formalized
            or node.assertions.proof_formalized
        ):
            continue
        if execution.lean_source_revision is None:
            issues.append(
                ExecutionInputIssue(
                    "lean-root-required",
                    f"{node.article_path}: formalized local work requires --lean-root",
                )
            )
            continue
        if not node.lean_targets:
            issues.append(
                ExecutionInputIssue(
                    "missing-lean-target",
                    f"{node.article_path}: formalized local work has no Lean declaration target",
                )
            )
            continue
        missing = tuple(
            target.declaration
            for target in node.lean_targets
            if target.source_file is None
        )
        if missing:
            issues.append(
                ExecutionInputIssue(
                    "lean-target-not-found",
                    f"{node.article_path}: Lean declaration target was not found: {', '.join(missing)}",
                )
            )
    return issues


def _blocked_item(
    node: RuntimeNode,
    statuses: dict[str, RuntimeStatus],
) -> ReadyBlock:
    phase = "statement" if not node.status.stated else "proof"
    reasons: list[str] = []
    blocked_by: set[str] = set()
    if node.assertions.not_ready:
        reasons.append("authored-not-ready")

    missing_statements = tuple(
        dependency
        for dependency in node.statement_dependencies
        if not statuses[dependency].stated
    )
    if missing_statements:
        reasons.append("statement-dependency-not-stated")
        blocked_by.update(missing_statements)

    if phase == "proof":
        missing_proofs = tuple(
            dependency
            for dependency in node.proof_dependencies
            if not statuses[dependency].proved
        )
        if missing_proofs:
            reasons.append("proof-dependency-not-proved")
            blocked_by.update(missing_proofs)

    if not reasons:
        reasons.append("prerequisites-not-satisfied")
    assert node.article_id is not None
    return ReadyBlock(
        node_id=node.id,
        article_id=node.article_id,
        article_path=node.article_path,
        title=node.title,
        phase=phase,
        blocked_by=tuple(sorted(blocked_by)),
        reasons=tuple(reasons),
    )


__all__ = [
    "READY_SCHEMA",
    "ReadyBlock",
    "ReadyItem",
    "ReadyResult",
    "list_ready_work",
]
