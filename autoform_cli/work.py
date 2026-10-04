"""Read-only formalization frontier derived from the Markdown runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .runtime import RuntimeNode, load_runtime_graph


WORK_SCHEMA = "autoform-work/v1"


class WorkError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkLeanTarget:
    declaration: str
    source_file: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "declaration": self.declaration,
            "source_file": self.source_file,
        }


@dataclass(frozen=True, slots=True)
class WorkItem:
    node_id: str
    article_id: str | None
    title: str
    article_path: str
    article_revision: str | None
    phase: str | None
    state: str
    claim_target: str
    blockers: tuple[str, ...]
    dependencies: tuple[str, ...]
    source_targets: tuple[str, ...]
    lean_targets: tuple[WorkLeanTarget, ...]

    @property
    def ready(self) -> bool:
        return self.phase is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "article_id": self.article_id,
            "article_path": self.article_path,
            "article_revision": self.article_revision,
            "blockers": list(self.blockers),
            "claim_target": self.claim_target,
            "dependencies": list(self.dependencies),
            "lean_targets": [target.as_dict() for target in self.lean_targets],
            "node_id": self.node_id,
            "phase": self.phase,
            "ready": self.ready,
            "source_targets": list(self.source_targets),
            "state": self.state,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class WorkFrontier:
    source_revision: str
    items: tuple[WorkItem, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": WORK_SCHEMA,
            "source_revision": self.source_revision,
            "items": [item.as_dict() for item in self.items],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def _phase(node: RuntimeNode) -> str | None:
    if (
        node.article_id is None
        or node.source_sha256 is None
        or not node.dispatchable
        or node.assertions.not_ready
        or node.mathlib
    ):
        return None
    if not node.status.stated:
        return "statement" if node.status.can_state else None
    if not node.status.proved:
        return "proof" if node.status.can_prove else None
    return None


def _blockers(nodes: dict[str, RuntimeNode], node: RuntimeNode) -> tuple[str, ...]:
    metadata_blockers: list[str] = []
    if node.article_id is None:
        metadata_blockers.append("roadmap:missing-article-id")
    if node.source_sha256 is None:
        metadata_blockers.append("roadmap:missing-article-revision")
    if metadata_blockers:
        return tuple(metadata_blockers)
    if node.assertions.not_ready:
        return ("roadmap:not-ready",)
    if not node.dispatchable:
        return ("roadmap:not-a-formalizable-leaf",)
    if node.mathlib or node.status.proved:
        return ()
    if not node.status.stated:
        return tuple(
            dependency
            for dependency in node.statement_dependencies
            if (resolved := nodes.get(dependency)) is None or not resolved.status.stated
        )
    blocked = [
        dependency
        for dependency in node.statement_dependencies
        if (resolved := nodes.get(dependency)) is None or not resolved.status.stated
    ]
    blocked.extend(
        dependency
        for dependency in node.proof_dependencies
        if dependency not in blocked
        and ((resolved := nodes.get(dependency)) is None or not resolved.status.proved)
    )
    return tuple(blocked)


def _item(nodes: dict[str, RuntimeNode], node: RuntimeNode) -> WorkItem:
    return WorkItem(
        node_id=node.id,
        article_id=node.article_id,
        title=node.title,
        article_path=node.article_path,
        article_revision=node.source_sha256,
        phase=_phase(node),
        state=node.status.state,
        claim_target=node.article_id or node.id,
        blockers=_blockers(nodes, node),
        dependencies=node.dependencies,
        source_targets=node.source_targets,
        lean_targets=tuple(
            WorkLeanTarget(target.declaration, target.source_file)
            for target in node.lean_targets
        ),
    )


def list_ready_work(
    project_or_blueprint: str | Path,
    *,
    lean_root: str | Path | None = None,
) -> WorkFrontier:
    runtime = load_runtime_graph(project_or_blueprint, lean_root=lean_root)
    missing = tuple(
        node.id
        for node in runtime.nodes
        if node.dispatchable
        and not node.mathlib
        and not node.status.proved
        and node.article_id is None
    )
    if missing:
        raise WorkError(
            "formalizable leaves need durable article_id metadata: " + ", ".join(missing)
        )
    unversioned = tuple(
        node.id
        for node in runtime.nodes
        if node.dispatchable
        and not node.mathlib
        and not node.status.proved
        and node.source_sha256 is None
    )
    if unversioned:
        raise WorkError(
            "formalizable leaves need durable article revision metadata: "
            + ", ".join(unversioned)
        )
    nodes = {node.id: node for node in runtime.nodes}
    items = tuple(
        item
        for node in runtime.nodes
        if (item := _item(nodes, node)).ready
    )
    return WorkFrontier(runtime.source_revision, items)


def work_context(
    project_or_blueprint: str | Path,
    selector: str,
    *,
    lean_root: str | Path | None = None,
) -> tuple[str, WorkItem]:
    runtime = load_runtime_graph(project_or_blueprint, lean_root=lean_root)
    nodes = {node.id: node for node in runtime.nodes}
    matches = [
        node
        for node in runtime.nodes
        if node.id == selector or (node.article_id is not None and node.article_id == selector)
    ]
    if len(matches) != 1:
        raise WorkError(f"work selector does not identify one article: {selector!r}")
    return runtime.source_revision, _item(nodes, matches[0])


__all__ = [
    "WORK_SCHEMA",
    "WorkError",
    "WorkFrontier",
    "WorkItem",
    "WorkLeanTarget",
    "list_ready_work",
    "work_context",
]
