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
import shlex
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from . import annotation_writer as writer
from .annotations import moved, read_links
from .article_identity import _proposed_id, _read_regular, _replace_if_unchanged
from .graph import Graph, GraphValidationError, _parse_frontmatter, load_graph
from .links_set import LinksSetError, _unread, parse_arguments

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
    owners = {node.article_id: node.path for node in graph.nodes.values() if node.article_id}
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
        if not unicodedata.is_normalized("NFC", article_path):
            # Git on macOS records names composed, so in a clone the annotation would name no article.
            reason = (
                "its path is not composed (NFC) as Git records it; rename it (in Git: git mv through a temporary name)"
            )
            prepared.append(_Prepared(replace(entry, action="kept", reasons=(reason,)), facts, {}))
            continue
        if links is not None and node.path in links.files:
            # Annotated already: its frontmatter facts, ignored now, go only where the annotation holds them all.
            given = writer.fact_keys(content)
            # A fact line the loader would refuse, or a key's second line, says something no annotation holds, so it
            # stays to be read.
            unread = [f"its frontmatter's {key} line does not parse" for key in sorted(set(given) - facts.keys())]
            unread += [f"its frontmatter gives {key} more than once" for key in sorted(facts) if given.count(key) > 1]
            if unread:
                reasons = tuple(f"beside its annotation, {line}" for line in unread)
                entry = replace(entry, action="kept", reasons=reasons)
            elif facts:
                annotated = links.facts[node.path]
                # Only what the frontmatter states counts, read beside the annotation's other facts: a key it leaves
                # out, the ID among them, says nothing against the annotation, which the loader reads instead.
                beside = writer.differences({**annotated, **facts}, annotated)
                reasons = tuple(f"beside its annotation, {line}" for line in beside)
                if beside and moved(blueprint, links):
                    proposed = _proposed_id(node.id, node.source_sha256)
                    annotating = links.files[node.path]
                    reasons += _folding_call(blueprint, node.path, facts, annotated, annotating, owners, proposed)
                entry = replace(entry, action="kept" if beside else "strip", reasons=reasons)
            prepared.append(_Prepared(entry, facts, {}))
            continue
        if content.startswith(b"\xef\xbb\xbf"):
            # The loader reads no frontmatter after the mark, so no fact would move.
            reason = "starts with a byte-order mark; remove it and run again"
            prepared.append(_Prepared(replace(entry, action="kept", reasons=(reason,)), facts, {}))
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


def _folding_call(
    blueprint: Path,
    page: Path,
    facts: dict[str, str],
    annotated: dict[str, str],
    annotating: tuple[str, ...],
    owners: Mapping[str, Path],
    proposed: str,
) -> tuple[str, ...]:
    """The ``links set`` call that folds into an article's annotation the keys only its frontmatter holds, when that
    is all the frontmatter adds and ``links set`` would make it: only the writer's own files annotate the article,
    holding just what it writes back, and the facts so folded go together, fit an annotation and change it; nothing
    otherwise."""

    paths = writer.annotation_paths(blueprint, page)
    owned = {path.relative_to(blueprint).as_posix(): path for path in (paths.own, paths.mathlib)}
    try:
        rendered = writer.encode(blueprint, page, annotated)
        files = {path: path.read_bytes() for path in owned.values() if os.path.lexists(path)}
    except (writer.Unencodable, OSError):
        return ()
    if not set(annotating) <= owned.keys() or any(rendered.get(path) != data for path, data in files.items()):
        return ()
    held, written = writer.canon(annotated), writer.canon({**annotated, **facts})
    extra = [key for key in facts if key in written and key not in held]
    # A key both hold with different values is not a fold: which value stands is for the user to say.
    if not extra or any(held[key] != value for key, value in written.items() if key in held):
        return ()
    folded = {**annotated, **{key: facts[key] for key in extra}}
    # links set refuses an ID another article holds, *proposed* included, the one it gives an article without one;
    # and its read-back undoes a fold that pairs a container's navigation with a declaration, since the loader drops
    # navigation from a declaration article.
    if owners.get(folded.get("article_id") or proposed, page) != page:
        return ()
    paired = "declaration" in folded and folded.get("navigation", "topic").casefold() != "topic"
    if paired and {"navigation", "declaration"} & set(extra):
        return ()
    # Quoted, as links set checks them, so each value is judged as held.
    if _parse_frontmatter(page.name, ["---", *(f'{key}: "{value}"' for key, value in folded.items()), "---"])[2]:
        return ()
    try:
        # A fold written as the same bytes changes nothing, and the plan would name it again.
        if writer.encode(blueprint, page, folded) == rendered:
            return ()
    except writer.Unencodable:
        return ()
    article = f"{blueprint.name}/{page.relative_to(blueprint).as_posix()}"
    # links set keeps the frontmatter's ID beside an annotation without one, and never takes it as an edit.
    edits = [f"{key}={facts[key]}" for key in extra if key != "article_id"]
    # It reads each edit as an argument, so a value it refuses or reads otherwise, such as one padded inside its
    # quotes, folds nothing.
    try:
        _, parsed = parse_arguments([article, *edits], ())
    except LinksSetError:
        return ()
    if parsed.values != {key: facts[key] for key in extra if key != "article_id"}:
        return ()
    command = shlex.join(["autoform", "links", "set", blueprint.name, article, *edits])
    # It gives an article neither holds an ID for the one migrate would, a change the call makes as well.
    given = "" if "article_id" in folded else ", and gives the article an article_id"
    return (f"`{command}` records its frontmatter's {', '.join(extra)} in the annotation{given}",)


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

    Every annotation is written first, in one pass whose files must not exist yet; then the graph is loaded once
    more and every article compared, which catches what no single file shows: an annotation the loader does not
    read, an ID another article holds, or a defaults file that changes what a record means, the plan's or another
    article's. An article of the plan that reads back otherwise loses the files written for it and keeps its
    frontmatter; any other article that reads otherwise undoes the whole write, as does a stop before those files
    are gone. Only then are articles stripped, each while its bytes still have the hash the plan was made from, and
    one that cannot be loses its files too. The plan is deterministic, so after a stop, running the command again
    completes it, reading any record that could not be undone as its article's annotation.
    """

    graph = load_graph(blueprint_dir)
    blueprint = graph.blueprint_dir
    prepared = _prepare(graph)
    annotating = [item for item in prepared if item.entry.action == "annotate"]
    changes: dict[Path, bytes | None] = {}
    for item in annotating:
        changes.update(item.changes)
    # The writer records each step here before taking it, so a stop before the mismatches are undone finds them, and
    # each undo records each step it finishes, which a stop then leaves alone.
    steps: list[writer.Written] = []
    finished: set[writer.Written] = set()
    stuck: writer.AnnotationWriteError | None = None
    try:
        writer.write(changes, {}, steps, finished)
        mismatched = _read_back(graph, annotating)
        # A mismatched article loses the files written for it, a template once no other article uses it.
        used = {path for item in annotating if item.entry.path_id not in mismatched for path in item.changes}
        unused = {path for item in annotating if item.entry.path_id in mismatched for path in item.changes} - used
        undone = [step for step in steps if step.path in unused]
        try:
            writer.undo(undone, finished)
        except writer.AnnotationWriteError as error:
            stuck = error
    except GraphValidationError as error:  # a stopped write pass among them
        raise _undone(blueprint, tuple(steps), error.issues) from None
    except BaseException as interrupt:
        # Interrupted before the check, or before the records it showed to read otherwise are gone: undo the whole
        # write rather than leave one of them to be read. Every step no undo finished, here or in the writer, is undone
        # here, so the files this names replace the interrupt's notes, which name only such steps.
        interrupt.__notes__ = list(_undone(blueprint, tuple(steps), (), finished).issues)
        raise
    if stuck is not None:
        left = [step for step in steps if step not in undone or step in stuck.written]
        raise LinksWriteError(stuck.issues, _names(blueprint, left))
    in_effect = [step for step in steps if step not in undone]

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
                issues = [f"{item.entry.article_path}: frontmatter facts not removed: {error}"]
                # Its own files go, so the loader reads its frontmatter, edited or not, and a rerun plans it afresh.
                own = [step for step in in_effect if step.path in item.changes and step.path.name != writer.DEFAULTS]
                try:
                    writer.undo(own)
                    in_effect = [step for step in in_effect if step not in own]
                except writer.AnnotationWriteError as stuck:
                    issues.extend(stuck.issues)
                    in_effect = [step for step in in_effect if step not in own or step in stuck.written]
                raise LinksWriteError(issues, _names(blueprint, in_effect), tuple(stripped)) from None
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
    return LinksResult(_names(blueprint, in_effect), tuple(stripped), plan)


def _read_back(graph: Graph, annotating: list[_Prepared]) -> dict[str, tuple[str, ...]]:
    """Why each article of *annotating* reads back otherwise from the graph loaded again; a refusal, naming them,
    when any other article does."""

    blueprint = graph.blueprint_dir
    reloaded = load_graph(blueprint)
    links = read_links(blueprint, {node.path: node.id for node in reloaded.nodes.values()})
    planned = {item.entry.path_id: item for item in annotating}
    mismatched: dict[str, tuple[str, ...]] = {}
    others: list[str] = []
    for node_id, node in sorted(graph.nodes.items()):
        # Node against node, as both read areas from atlas.json too, with the ID written for one that had none.
        before = writer.node_facts(node)
        if item := planned.get(node_id):
            before["article_id"] = item.facts["article_id"]
        after = reloaded.nodes.get(node_id)
        lines = writer.differences(before, writer.node_facts(after)) if after else ["the article no longer loads"]
        if item is None:
            if lines:
                where = node.path.relative_to(blueprint).as_posix()
                others.append(f"{where}: reads otherwise once the plan's files are written: {'; '.join(lines)}")
            continue
        # Unread, a record leaves the frontmatter in charge, which reads the same until it is stripped.
        own = writer.annotation_paths(blueprint, node.path).own.relative_to(blueprint).as_posix()
        if links is None or own not in links.files.get(node.path, ()):
            lines = [_unread(own, links), *lines]
        if lines:
            mismatched[node_id] = tuple(lines)
    if others:
        raise GraphValidationError([*others, "nothing written"])
    return mismatched


def _undone(
    blueprint: Path,
    steps: tuple[writer.Written, ...],
    issues: tuple[str, ...],
    finished: set[writer.Written] | None = None,
) -> LinksWriteError:
    """The error for a write that could not go on, once its *steps* are undone but those in *finished*, naming any
    that could not be."""
    try:
        writer.undo(steps, finished)
    except writer.AnnotationWriteError as stuck:
        return LinksWriteError([*issues, *stuck.issues], _names(blueprint, stuck.written))
    return LinksWriteError(issues)


def _names(blueprint: Path, steps: list[writer.Written] | tuple[writer.Written, ...]) -> tuple[str, ...]:
    return tuple(step.path.relative_to(blueprint).as_posix() for step in steps)


def pin_notes(revision: str, blueprint: Path) -> tuple[str, ...]:
    """What a project must know once its facts are in ``blueprint/links/``, one line each."""

    running = f", such as this version's {revision}" if revision else ""
    notes = [
        f"CI reads blueprint/links/ only at an Autoform commit from {FIRST_READER} on{running}; at an older "
        "AUTOFORM_REF the site shows no Lean progress, and CI may still pass",
        "every migrated article's revision changes, so a worker that read `autoform work context` reads it again",
        "the project's own tools that read articles' frontmatter, such as CI scripts, find no facts in a migrated "
        "article until they read blueprint/links/",
    ]
    path = blueprint.parent / ".github" / "workflows" / "autoform-verify.yml"
    try:
        workflow = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        workflow = None
    # Only a workflow that looks for approvals has a step to edit, whatever its name: not one 0.6.0 wrote, nor
    # the stub `init --no-verify-workflow` writes.
    if workflow is not None and "review_approved" in workflow and "blueprint/links" not in workflow:
        # `init` keeps an existing workflow, so a moved approval would no longer require the review marker.
        notes.append(
            "the project's verify workflow looks for approvals only in blueprint/roadmap; add `git grep -q "
            "'\"review_approved\"' -- blueprint/links` to its Detect statement review step, as `init` now writes it"
        )
    return tuple(notes)


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
