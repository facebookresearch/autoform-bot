"""Read-only planning for durable roadmap article identifiers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .graph import Graph, GraphValidationError, load_graph

IDENTITY_PLAN_SCHEMA = "autoform-article-id-plan/v1"


@dataclass(frozen=True, slots=True)
class ArticleIdentityEntry:
    """The current or proposed durable identity for one article."""

    path_id: str
    article_path: str
    article_id: str
    assigned: bool
    source_sha256: str

    def as_dict(self) -> dict[str, bool | str]:
        return {
            "article_id": self.article_id,
            "article_path": self.article_path,
            "assigned": self.assigned,
            "path_id": self.path_id,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class ArticleIdentityPlan:
    """A deterministic read-only inventory of article identity metadata."""

    schema: str
    blueprint_path: str
    entries: tuple[ArticleIdentityEntry, ...]

    @property
    def complete(self) -> bool:
        return all(entry.assigned for entry in self.entries)

    @property
    def missing_count(self) -> int:
        return sum(not entry.assigned for entry in self.entries)

    def as_dict(self) -> dict[str, object]:
        return {
            "blueprint_path": self.blueprint_path,
            "complete": self.complete,
            "entries": [entry.as_dict() for entry in self.entries],
            "missing_count": self.missing_count,
            "schema": self.schema,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def plan_article_ids(
    blueprint_dir: str | Path,
    *,
    _graph: Graph | None = None,
) -> ArticleIdentityPlan:
    """Validate a blueprint and propose IDs for articles that do not have one."""

    graph = _graph if _graph is not None else load_graph(blueprint_dir)
    entries = []
    owners: dict[str, str] = {}
    for node in sorted(graph.nodes.values(), key=lambda candidate: candidate.id):
        if node.source_sha256 is None:
            raise GraphValidationError([f"{node.id}: article source hash is unavailable"])
        article_id = node.article_id or _proposed_id(node.id, node.source_sha256)
        previous = owners.get(article_id)
        if previous is not None:
            raise GraphValidationError(
                [f"{node.id}: article_id {article_id!r} also names article {previous}"]
            )
        owners[article_id] = node.id
        entries.append(
            ArticleIdentityEntry(
                path_id=node.id,
                article_path=node.path.relative_to(graph.blueprint_dir).as_posix(),
                article_id=article_id,
                assigned=node.article_id is not None,
                source_sha256=node.source_sha256,
            )
        )
    return ArticleIdentityPlan(
        schema=IDENTITY_PLAN_SCHEMA,
        blueprint_path=graph.blueprint_dir.name,
        entries=tuple(entries),
    )


def _proposed_id(path_id: str, source_sha256: str) -> str:
    digest = hashlib.sha256(b"autoform-article-id/v1\0")
    encoded_path = path_id.encode("utf-8")
    encoded_source = source_sha256.encode("ascii")
    digest.update(len(encoded_path).to_bytes(8, "big"))
    digest.update(encoded_path)
    digest.update(encoded_source)
    return f"af_{digest.hexdigest()[:24]}"


__all__ = [
    "IDENTITY_PLAN_SCHEMA",
    "ArticleIdentityEntry",
    "ArticleIdentityPlan",
    "GraphValidationError",
    "plan_article_ids",
]
