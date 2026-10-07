"""Read-only formalization frontier derived from the Markdown runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .runtime import RuntimeNode, load_runtime_graph
from .status import is_definition


WORK_SCHEMA = "autoform-work/v2"
ASSUMPTIONS_SCHEMA = "autoform-assumptions/v1"


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
    assumes: tuple[str, ...] = ()
    open_statements: bool = False
    #: The article records ``statement: retracted``: a revision retracted its
    #: statement, so the work starts from ``autoform work impact``.
    revision: bool = False

    @property
    def ready(self) -> bool:
        return self.phase is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "article_id": self.article_id,
            "article_path": self.article_path,
            "article_revision": self.article_revision,
            "assumes": list(self.assumes),
            "blockers": list(self.blockers),
            "claim_target": self.claim_target,
            "dependencies": list(self.dependencies),
            "lean_targets": [target.as_dict() for target in self.lean_targets],
            "node_id": self.node_id,
            "open_statements": self.open_statements,
            "phase": self.phase,
            "ready": self.ready,
            "revision": self.revision,
            "source_targets": list(self.source_targets),
            "state": self.state,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class WorkFrontier:
    source_revision: str
    items: tuple[WorkItem, ...]
    open_statements: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": WORK_SCHEMA,
            "open_statements": self.open_statements,
            "source_revision": self.source_revision,
            "items": [item.as_dict() for item in self.items],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def _phase(node: RuntimeNode, blockers: tuple[str, ...]) -> str | None:
    if blockers or node.status.proved:
        return None
    return "proof" if node.status.stated else "statement"


def _blockers(node: RuntimeNode) -> tuple[str, ...]:
    # Report each reason where `list_ready_work` enforces it: finished articles
    # need no metadata, and an unfinished leaf needs it even when not ready.
    if not node.dispatchable:
        return ("roadmap:not-a-formalizable-leaf",)
    if node.status.proved:
        return ()
    metadata_blockers: list[str] = []
    if node.article_id is None:
        metadata_blockers.append("roadmap:missing-article-id")
    if node.source_sha256 is None:
        metadata_blockers.append("roadmap:missing-article-revision")
    if metadata_blockers:
        return tuple(metadata_blockers)
    if node.assertions.not_ready:
        return ("roadmap:not-ready",)
    # Readiness comes from the derived status, which applies the project's
    # open-statement policy: strict projects wait for proof prerequisites to be
    # proved, open-statement projects only for prerequisites to be stated.
    return node.status.waiting_on


def _item(node: RuntimeNode, *, open_statements: bool) -> WorkItem:
    blockers = _blockers(node)
    return WorkItem(
        node_id=node.id,
        article_id=node.article_id,
        title=node.title,
        article_path=node.article_path,
        article_revision=node.source_sha256,
        phase=_phase(node, blockers),
        state=node.status.state,
        claim_target=node.article_id or node.id,
        blockers=blockers,
        dependencies=node.dependencies,
        source_targets=node.source_targets,
        lean_targets=tuple(
            WorkLeanTarget(target.declaration, target.source_file)
            for target in node.lean_targets
        ),
        assumes=node.status.assumes,
        open_statements=open_statements,
        revision=node.assertions.statement_retracted,
    )


def list_ready_work(
    project_or_blueprint: str | Path,
    *,
    lean_root: str | Path | None = None,
) -> WorkFrontier:
    runtime = load_runtime_graph(project_or_blueprint, lean_root=lean_root)
    unfinished = [
        node
        for node in runtime.nodes
        if node.dispatchable and not node.mathlib and not node.status.proved
    ]
    missing = tuple(node.id for node in unfinished if node.article_id is None)
    if missing:
        raise WorkError(
            "formalizable leaves need durable article_id metadata: "
            + ", ".join(missing)
            + " (plan IDs with `autoform migrate article-ids <blueprint> --json`, then"
            " add each article_id to its article's frontmatter)"
        )
    unversioned = tuple(node.id for node in unfinished if node.source_sha256 is None)
    if unversioned:
        raise WorkError(
            "formalizable leaves need durable article revision metadata: "
            + ", ".join(unversioned)
        )
    items = tuple(
        item
        for node in runtime.nodes
        if (item := _item(node, open_statements=runtime.open_statements)).ready
    )
    return WorkFrontier(runtime.source_revision, items, open_statements=runtime.open_statements)


def work_context(
    project_or_blueprint: str | Path,
    selector: str,
    *,
    lean_root: str | Path | None = None,
) -> tuple[str, WorkItem]:
    runtime = load_runtime_graph(project_or_blueprint, lean_root=lean_root)
    matches = [
        node
        for node in runtime.nodes
        if node.id == selector or (node.article_id is not None and node.article_id == selector)
    ]
    if not matches:
        raise WorkError(
            f"no article matches {selector!r}; pass a path-derived node id such as"
            " chapter/article, without .md, or an article_id"
        )
    if len(matches) > 1:
        raise WorkError(
            f"{selector!r} matches more than one article: "
            + ", ".join(node.id for node in matches)
        )
    return runtime.source_revision, _item(matches[0], open_statements=runtime.open_statements)


@dataclass(frozen=True, slots=True)
class AssumptionArticle:
    id: str
    article_id: str | None
    state: str
    declarations: tuple[str, ...]
    open: bool
    assumes: tuple[str, ...]
    allowed_open_declarations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed_open_declarations": list(self.allowed_open_declarations),
            "article_id": self.article_id,
            "assumes": list(self.assumes),
            "declarations": list(self.declarations),
            "id": self.id,
            "open": self.open,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class AssumptionContract:
    open_statements: bool
    source_revision: str
    articles: tuple[AssumptionArticle, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": ASSUMPTIONS_SCHEMA,
            "open_statements": self.open_statements,
            "source_revision": self.source_revision,
            "articles": [article.as_dict() for article in self.articles],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def assumption_contract(project_or_blueprint: str | Path) -> AssumptionContract:
    """Record which open statements each article's Lean code may reach.

    CI audits the Lean build against this: an open article's own declarations
    may keep a ``sorry`` proof, and every other article may reach only the open
    statements its Markdown dependencies declare. A theorem recording
    ``statement: retracted`` stays open while its ``lean:`` names the old
    declaration; a theorem that was never stated is not open, even with a
    ``lean:`` name, so CI rejects its ``sorry``. Mathlib articles are listed too,
    never open and allowed nothing, so CI can check that their names exist and
    reach no open statement. Under the strict policy no article is open and
    nothing is allowed.
    """
    runtime = load_runtime_graph(project_or_blueprint)
    declarations = {
        node.id: tuple(target.declaration for target in node.lean_targets)
        for node in runtime.nodes
    }
    articles: list[AssumptionArticle] = []
    for node in sorted(runtime.nodes, key=lambda candidate: candidate.id):
        if not declarations[node.id]:
            continue
        is_open = (
            runtime.open_statements
            and not node.status.proved
            and not is_definition(node)
            and (node.status.stated or node.assertions.statement_retracted)
        )
        allowed = {name for assumed in node.status.assumes for name in declarations.get(assumed, ())}
        if is_open:
            allowed.update(declarations[node.id])
        articles.append(
            AssumptionArticle(
                id=node.id,
                article_id=node.article_id,
                state=node.status.state,
                declarations=declarations[node.id],
                open=is_open,
                assumes=node.status.assumes,
                allowed_open_declarations=tuple(sorted(allowed)),
            )
        )
    return AssumptionContract(runtime.open_statements, runtime.source_revision, tuple(articles))


__all__ = [
    "ASSUMPTIONS_SCHEMA",
    "WORK_SCHEMA",
    "AssumptionArticle",
    "AssumptionContract",
    "WorkError",
    "WorkFrontier",
    "WorkItem",
    "WorkLeanTarget",
    "assumption_contract",
    "list_ready_work",
    "work_context",
]
