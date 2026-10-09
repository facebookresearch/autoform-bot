"""Planning, and applying, durable roadmap article identifiers."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .graph import GraphValidationError, load_graph

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


def plan_article_ids(blueprint_dir: str | Path) -> ArticleIdentityPlan:
    """Validate a blueprint and propose IDs for articles that do not have one."""

    graph = load_graph(blueprint_dir)
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


class ArticleIdWriteError(GraphValidationError):
    """Applying a plan stopped; ``written`` names the articles it had already changed."""

    def __init__(self, issues: list[str] | tuple[str, ...], written: tuple[ArticleIdentityEntry, ...] = ()) -> None:
        super().__init__(issues)
        self.written = written


def write_article_ids(blueprint_dir: str | Path) -> tuple[ArticleIdentityEntry, ...]:
    """Add each planned ID to the frontmatter of the article that lacks one.

    The ID goes in as the first frontmatter line; an article without
    frontmatter gets a block holding only the ID. Nothing else in the file
    changes. An article is written only while its bytes still have the hash
    the plan was made from, so an edit made meanwhile is never overwritten:
    every article is checked before the first is written, and again just
    before its own replacement. The plan is deterministic, so running the
    command again after a refusal completes it.
    """

    plan = plan_article_ids(blueprint_dir)
    blueprint = Path(blueprint_dir).expanduser().resolve()
    pending = [entry for entry in plan.entries if not entry.assigned]
    prepared: list[tuple[ArticleIdentityEntry, Path, bytes, int]] = []
    issues: list[str] = []
    for entry in pending:
        path = blueprint / entry.article_path
        try:
            content, mode = _read_regular(path)
        except OSError as error:
            issues.append(f"{entry.article_path}: cannot read article: {error}")
            continue
        if hashlib.sha256(content).hexdigest() != entry.source_sha256:
            issues.append(f"{entry.article_path}: changed while the plan was made; run the command again")
            continue
        if content.startswith(b"\xef\xbb\xbf"):
            issues.append(f"{entry.article_path}: starts with a byte-order mark; remove it and run again")
            continue
        prepared.append((entry, path, _with_article_id(content, entry.article_id), mode))
    if issues:
        raise ArticleIdWriteError(issues)

    written: list[ArticleIdentityEntry] = []
    for entry, path, content, mode in prepared:
        try:
            _replace_if_unchanged(path, entry.source_sha256, content, mode)
        except OSError as error:
            raise ArticleIdWriteError(
                [f"{entry.article_path}: not written: {error}; run the command again"], tuple(written)
            ) from None
        written.append(entry)

    after = {entry.path_id: entry for entry in plan_article_ids(blueprint).entries}
    wrong = [
        entry.article_path
        for entry in written
        if not after[entry.path_id].assigned or after[entry.path_id].article_id != entry.article_id
    ]
    if wrong:
        raise ArticleIdWriteError(
            [f"{path}: article_id did not load back as written" for path in wrong], tuple(written)
        )
    return tuple(written)


def _read_regular(path: Path) -> tuple[bytes, int]:
    """The bytes and permission bits of a regular file, refusing anything else."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read(), stat.S_IMODE(metadata.st_mode)
    finally:
        os.close(descriptor)


def _with_article_id(content: bytes, article_id: str) -> bytes:
    """Insert ``article_id`` as the first frontmatter key, keeping the file's line endings."""

    text = content.decode("utf-8")
    first, separator, rest = text.partition("\n")
    newline = "\r\n" if first.endswith("\r") else "\n"
    line = f"article_id: {article_id}{newline}"
    if first.strip() == "---" and separator:
        return (first + separator + line + rest).encode("utf-8")
    return (f"---{newline}{line}---{newline}{newline}" + text).encode("utf-8")


def _replace_if_unchanged(path: Path, expected_sha256: str, content: bytes, mode: int) -> None:
    """Replace *path* from a same-directory temporary file, unless it changed since it was planned."""

    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        current, _ = _read_regular(path)
        if hashlib.sha256(current).hexdigest() != expected_sha256:
            raise OSError("the article changed while the plan was applied")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
    "ArticleIdWriteError",
    "ArticleIdentityEntry",
    "ArticleIdentityPlan",
    "GraphValidationError",
    "plan_article_ids",
    "write_article_ids",
]
