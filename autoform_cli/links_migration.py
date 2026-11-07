"""Planning, and applying, the move of articles' frontmatter facts into ``blueprint/links/`` (#601).

Each article no annotation counts for gets one written through :mod:`autoform_cli.annotation_writer`, its
``article_id`` copied, or the one ``migrate article-ids`` would propose; the graph is loaded again, and only an article
that reads back the same, under :func:`~autoform_cli.annotation_writer.canon`, loses its frontmatter facts. Any other
keeps them and is reported as kept, as is an article whose frontmatter beside its annotation says something else.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from . import annotation_writer as writer
from .annotations import read_links
from .article_identity import _proposed_id, _read_regular, _replace_if_unchanged
from .graph import Graph, GraphValidationError, load_graph

LINKS_PLAN_SCHEMA = "autoform-links-plan/v1"
#: The first Autoform commit that reads ``blueprint/links/`` (#660).
FIRST_READER = "3ea366ae"


@dataclass(frozen=True, slots=True)
class LinksEntry:
    """What the migration does for one article: ``annotate`` it, ``strip`` frontmatter its annotation already
    holds, leave it ``kept`` for the reasons given, or nothing, as ``done``."""

    path_id: str
    article_path: str
    action: str
    reasons: tuple[str, ...]
    #: The files, below the blueprint, that annotating it writes; a template is listed for the first article only.
    files: tuple[str, ...]
    source_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "article_path": self.article_path,
            "files": list(self.files),
            "path_id": self.path_id,
            "reasons": list(self.reasons),
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class LinksPlan:
    """A deterministic, read-only plan of the move."""

    schema: str
    blueprint_path: str
    entries: tuple[LinksEntry, ...]

    @property
    def pending(self) -> tuple[LinksEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action in {"annotate", "strip"})

    @property
    def kept(self) -> tuple[LinksEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action == "kept")

    @property
    def complete(self) -> bool:
        return not self.pending and not self.kept

    def as_dict(self) -> dict[str, object]:
        return {
            "blueprint_path": self.blueprint_path,
            "complete": self.complete,
            "entries": [entry.as_dict() for entry in self.entries],
            "kept_count": len(self.kept),
            "pending_count": len(self.pending),
            "schema": self.schema,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class _Prepared:
    entry: LinksEntry
    facts: dict[str, str]
    changes: dict[Path, bytes | None]


def plan_links(blueprint_dir: str | Path) -> LinksPlan:
    """Validate a blueprint and plan the move of every article's frontmatter facts."""

    graph = load_graph(blueprint_dir)
    return LinksPlan(LINKS_PLAN_SCHEMA, graph.blueprint_dir.name, tuple(item.entry for item in _prepare(graph)))


def _prepare(graph: Graph) -> list[_Prepared]:
    blueprint = graph.blueprint_dir
    links = read_links(blueprint, {node.path: node.id for node in graph.nodes.values()})
    prepared: list[_Prepared] = []
    listed: set[Path] = set()
    for node in sorted(graph.nodes.values(), key=lambda candidate: candidate.id):
        if node.source_sha256 is None:
            raise GraphValidationError([f"{node.id}: article source hash is unavailable"])
        article_path = node.path.relative_to(blueprint).as_posix()
        entry = LinksEntry(node.id, article_path, "done", (), (), node.source_sha256)
        content = node.path.read_bytes()
        if hashlib.sha256(content).hexdigest() != node.source_sha256:
            raise GraphValidationError([f"{article_path}: changed while the plan was made; run the command again"])
        facts = writer.frontmatter_facts(node.id, content.decode("utf-8"))
        if links is not None and node.path in links.files:
            # Annotated already: its frontmatter facts, ignored now, go only where the annotation holds them all.
            if facts:
                annotated = links.facts[node.path]
                # An ID is never taken away, so frontmatter without one says nothing against the annotation's.
                if "article_id" in annotated:
                    facts.setdefault("article_id", annotated["article_id"])
                beside = writer.differences(facts, annotated)
                entry = replace(
                    entry,
                    action="kept" if beside else "strip",
                    reasons=tuple(f"beside its annotation, {line}" for line in beside),
                )
            prepared.append(_Prepared(entry, facts, {}))
            continue
        facts.setdefault("article_id", node.article_id or _proposed_id(node.id, node.source_sha256))
        try:
            changes = writer.encode(blueprint, node.path, facts)
        except writer.Unencodable as error:
            prepared.append(_Prepared(replace(entry, action="kept", reasons=error.reasons), facts, {}))
            continue
        # Templates are in the changes only while absent, so any file here that exists annotates something else.
        taken = sorted(path.relative_to(blueprint).as_posix() for path in changes if os.path.lexists(path))
        if taken:
            reasons = tuple(f"{name} exists but does not annotate it; move or remove it" for name in taken)
            prepared.append(_Prepared(replace(entry, action="kept", reasons=reasons), facts, {}))
            continue
        written = {path: data for path, data in changes.items() if data is not None}
        files = tuple(sorted(path.relative_to(blueprint).as_posix() for path in written if path not in listed))
        listed.update(written)
        prepared.append(_Prepared(replace(entry, action="annotate", files=files), facts, written))
    return prepared


@dataclass(frozen=True, slots=True)
class LinksResult:
    """What a write did: the files it wrote, below the blueprint; the articles it stripped; and the plan after it."""

    written: tuple[str, ...]
    stripped: tuple[str, ...]
    plan: LinksPlan


class LinksWriteError(GraphValidationError):
    """Applying a plan stopped; ``written`` and ``stripped`` name what it had already changed."""

    def __init__(
        self, issues: list[str] | tuple[str, ...], written: tuple[str, ...] = (), stripped: tuple[str, ...] = ()
    ) -> None:
        super().__init__(issues)
        self.written = written
        self.stripped = stripped


def write_links(blueprint_dir: str | Path) -> LinksResult:
    """Annotate every article the plan annotates, then remove the frontmatter facts of each that reads back the same.

    Every annotation is written first, in one pass whose files must not exist yet, and undone if that pass stops;
    then the graph is loaded once more, which catches what no single file shows, such as an ID another article
    holds or a defaults file above that changes a record's meaning. An article that reads back otherwise loses the
    files written for it and keeps its frontmatter. Only then are articles stripped, each while its bytes still
    have the hash the plan was made from. The plan is deterministic, so after a stop, running the command again
    completes it.
    """

    graph = load_graph(blueprint_dir)
    blueprint = graph.blueprint_dir
    prepared = _prepare(graph)
    annotating = [item for item in prepared if item.entry.action == "annotate"]
    changes: dict[Path, bytes | None] = {}
    for item in annotating:
        changes.update(item.changes)
    try:
        steps = writer.write(changes, {})
    except writer.AnnotationWriteError as error:
        raise _undone(blueprint, error.written, error.issues) from None
    try:
        reloaded = load_graph(blueprint)
    except GraphValidationError as error:
        raise _undone(blueprint, steps, error.issues) from None

    mismatched: dict[str, tuple[str, ...]] = {}
    for item in annotating:
        # Node against node, as both read areas from atlas.json too, and the ID written for one that had none.
        before = {**writer.node_facts(graph.nodes[item.entry.path_id]), "article_id": item.facts["article_id"]}
        node = reloaded.nodes.get(item.entry.path_id)
        lines = writer.differences(before, writer.node_facts(node)) if node else ["the article no longer loads"]
        if lines:
            mismatched[item.entry.path_id] = tuple(lines)
    # Each mismatched article loses the files written for it, a template among them once no other article uses it.
    used = {path for item in annotating if item.entry.path_id not in mismatched for path in item.changes}
    unused = {path for item in annotating if item.entry.path_id in mismatched for path in item.changes} - used
    undone = [step for step in steps if step.path in unused]
    try:
        writer.undo(undone)
    except writer.AnnotationWriteError as error:
        left = [step for step in steps if step not in undone or step in error.written]
        raise LinksWriteError(error.issues, _names(blueprint, left)) from None
    undone_paths = {step.path for step in undone}

    stripped: list[str] = []
    for item in prepared:
        if item.entry.action == "strip" or (item.entry.action == "annotate" and item.entry.path_id not in mismatched):
            path = blueprint / item.entry.article_path
            try:
                content, mode = _read_regular(path)
                if hashlib.sha256(content).hexdigest() != item.entry.source_sha256:
                    raise OSError("the article changed while the plan was applied; run the command again")
                if (bare := writer.strip_facts(content)) == content:
                    continue  # no frontmatter facts to remove
                _replace_if_unchanged(path, item.entry.source_sha256, bare, mode)
            except OSError as error:
                raise LinksWriteError(
                    [f"{item.entry.article_path}: frontmatter facts not removed: {error}"],
                    _names(blueprint, [step for step in steps if step.path not in undone_paths]),
                    tuple(stripped),
                ) from None
            stripped.append(item.entry.article_path)
    plan = plan_links(blueprint)
    if mismatched:
        # A mismatch shows only after the write, so the plan made again reports it beside the kept ones.
        plan = replace(
            plan,
            entries=tuple(
                replace(entry, action="kept", reasons=mismatched[entry.path_id], files=())
                if entry.path_id in mismatched
                else entry
                for entry in plan.entries
            ),
        )
    left_written = _names(blueprint, [step for step in steps if step.path not in undone_paths])
    return LinksResult(left_written, tuple(stripped), plan)


def _undone(blueprint: Path, steps: tuple[writer.Written, ...], issues: tuple[str, ...]) -> LinksWriteError:
    """The error for a write that could not go on, once its *steps* are undone, naming any that could not be."""
    try:
        writer.undo(steps)
    except writer.AnnotationWriteError as stuck:
        return LinksWriteError([*issues, *stuck.issues], _names(blueprint, stuck.written))
    return LinksWriteError(issues)


def _names(blueprint: Path, steps: list[writer.Written] | tuple[writer.Written, ...]) -> tuple[str, ...]:
    return tuple(step.path.relative_to(blueprint).as_posix() for step in steps)


def pin_notes(revision: str) -> tuple[str, ...]:
    """What a project must know once its facts are in ``blueprint/links/``, one line each."""

    running = f", such as this version's {revision}" if revision else ""
    return (
        f"CI reads blueprint/links/ only at an Autoform commit from {FIRST_READER} on{running}; at an older "
        "AUTOFORM_REF the site shows no Lean progress while CI passes",
        "every migrated article's revision changes, so a worker that read `autoform work context` reads it again",
        "this version's skills still write frontmatter, which a migrated project ignores, so a project that agents "
        "work in waits for the release whose skills write annotations",
    )


__all__ = [
    "FIRST_READER",
    "LINKS_PLAN_SCHEMA",
    "LinksEntry",
    "LinksPlan",
    "LinksResult",
    "LinksWriteError",
    "pin_notes",
    "plan_links",
    "write_links",
]
