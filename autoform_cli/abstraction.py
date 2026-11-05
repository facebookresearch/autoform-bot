"""Auto-abstraction: copied code waiting to be raised into one shared declaration.

Workers keep the helpers they write private to their own article's file, so a
later worker that needs one copies it. The subagent debrief asks for every
such copy (``copied_code``). This module folds the copies that succeeded
attempts reported into clusters, one per piece of code that now exists more
than once, and keeps the lead's decisions about them: the shared file each
cluster should be raised into, and whether it was raised or dismissed. At the
end of a Formalize run the lead raises every open cluster and refactors its
copies to use the shared declaration.

The copies themselves are never stored twice: they are read from the debrief
records each time. Only decisions are written, beside the records::

    abstractions/<decision_id>.json   one Decision each, schema_version 1, never overwritten
    abstractions/TODO.md              the open clusters, rendered from records and decisions (derived)
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import debrief
from . import debrief_store as store

SCHEMA_VERSION = 1
DIRECTORY = "abstractions"
TODO = "TODO.md"
ACTIONS = frozenset({"target", "raised", "dismissed"})
CLOSING = frozenset({"raised", "dismissed"})

_DECISION_ID = re.compile(r"\A[0-9a-f]{32}\Z")
# What an agent writes for a file or name it does not know.
_UNKNOWN = frozenset({"", "null", "none"})
# A copy's file larger than this is not searched for its name.
_MAX_SOURCE = 8 * 1024 * 1024
# A declaration keyword and any namespace prefix, so `theorem Foo.bar` names a copy `Foo.bar`.
_DECLARED = r"\b(?:theorem|lemma|def|abbrev|instance|structure|inductive|class|opaque|axiom)\s+(?:[^\s.]+\.)*"


class AbstractionError(ValueError):
    """The to-do list cannot be read or changed as asked."""


@dataclass(frozen=True, slots=True)
class Copy:
    """One reported copy, from the debrief of a succeeded attempt."""

    entry_id: str
    attempt_id: str
    node_id: str
    recorded_at: str
    kind: str
    source_file: str
    source_declaration: str
    copy_file: str
    copy_declaration: str
    what: str


@dataclass(frozen=True, slots=True)
class Decision:
    """The lead's decision about the copies of one cluster."""

    decision_id: str
    entries: tuple[str, ...]
    action: str
    file: str
    declaration: str
    note: str
    recorded_at: str

    def __post_init__(self) -> None:
        if not _DECISION_ID.match(self.decision_id):
            raise debrief.RecordError(f"decision_id must be 32 lowercase hex digits: {self.decision_id!r}")
        if self.action not in ACTIONS:
            raise debrief.RecordError(f"unknown action: {self.action!r}")

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            **{name: getattr(self, name) for name in self.__slots__},
            "entries": list(self.entries),
        }

    @classmethod
    def from_json(cls, data: Any) -> Decision:
        if not isinstance(data, dict):
            raise debrief.RecordError("decision must be a JSON object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise debrief.RecordError(f"unsupported schema_version: {data.get('schema_version')!r}")
        entries = data.get("entries")
        if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
            raise debrief.RecordError("entries must be a list of strings")
        fields = {}
        for name in ("decision_id", "action", "file", "declaration", "note", "recorded_at"):
            value = data.get(name)
            if not isinstance(value, str):
                raise debrief.RecordError(f"{name} must be a string")
            fields[name] = debrief._text(value, debrief.MAX_NOTE if name == "note" else 512)
        return cls(**fields, entries=tuple(debrief._text(entry, 64) for entry in entries[:1000]))


@dataclass(frozen=True, slots=True)
class Cluster:
    """Copies of one piece of code, joined through the declarations they copy."""

    cluster_id: str
    copies: tuple[Copy, ...]
    status: str
    file: str
    declaration: str
    note: str

    def to_json(self, present: dict[str, bool | None] | None = None) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "status": self.status,
            "file": self.file or None,
            "declaration": self.declaration or None,
            "note": self.note or None,
            "copies": [
                {
                    **{name: getattr(copy, name) or None for name in Copy.__slots__},
                    "present": None if present is None else present.get(copy.entry_id),
                }
                for copy in self.copies
            ],
        }


def copies(root: Path) -> list[Copy]:
    """Every copy that a succeeded attempt reported, oldest first.

    A failed attempt's copy never reached the shared branch, so it is left out.
    """
    found: list[Copy] = []
    for entry in debrief.load_records(root):
        if entry.outcome != "succeeded":
            continue
        for index, item in enumerate(entry.answer.copied_code, 1):
            found.append(
                Copy(
                    entry_id=f"{entry.attempt_id[:12]}.{index}",
                    attempt_id=entry.attempt_id,
                    node_id=entry.node_id,
                    recorded_at=entry.recorded_at,
                    kind=item.kind,
                    source_file=item.source_file,
                    source_declaration=item.source_declaration,
                    copy_file=item.copy_file,
                    copy_declaration=item.copy_declaration,
                    what=item.what,
                )
            )
    return found


def load_decisions(root: Path) -> list[Decision]:
    """Every valid decision, oldest first. Invalid or mismatched files are skipped."""
    decisions: list[Decision] = []
    with store.open_root(root) as root_fd, store.open_subdir(root_fd, DIRECTORY) as directory:
        for name in store.list_names(directory, ".json"):
            try:
                decision = Decision.from_json(debrief._decode(store.read_bounded(directory, name)))
            except (OSError, debrief.RecordError):
                continue
            if f"{decision.decision_id}.json" == name:
                decisions.append(decision)
    return sorted(decisions, key=lambda decision: (decision.recorded_at, decision.decision_id))


def _declaration(name: str) -> str:
    name = name.strip().strip("`").removeprefix("_root_.")
    return "" if name.lower() in _UNKNOWN else name


def _last_component(name: str) -> str:
    """The last component of a reported name; a dot inside «» does not split it."""
    return re.split(r"\.(?![^«]*»)", _declaration(name))[-1]


def _file(path: str) -> str:
    """A reported file as a key: empty when unknown, else a normalised relative path."""
    path = path.strip().replace("\\", "/")
    return "" if path.lower() in _UNKNOWN else posixpath.normpath(path)


def _keys(copy: Copy) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The code a copy was taken from, and the copy itself, as union-find keys.

    Named code is keyed by its file and the last component of its name, so a
    copy of a copy joins the first copy's cluster however the name was
    qualified. The price: same-named declarations in different namespaces of
    one file share a cluster. Inline code, or code whose file is unknown, has
    no key to share, so it only joins the copy made from it.
    """
    keys = []
    for side, file, name in (
        ("source", copy.source_file, copy.source_declaration),
        ("copy", copy.copy_file, copy.copy_declaration),
    ):
        file, name = _file(file), _last_component(name)
        keys.append(("declaration", file, name) if file and name else (side, copy.entry_id))
    return keys[0], keys[1]


def clusters(root: Path) -> list[Cluster]:
    """All clusters, open and closed, ordered by their oldest copy."""
    found = copies(root)
    parent: dict[tuple[str, ...], tuple[str, ...]] = {}

    def find(key: tuple[str, ...]) -> tuple[str, ...]:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for copy in found:
        source_key, copy_key = _keys(copy)
        parent[find(source_key)] = find(copy_key)
    groups: dict[tuple[str, ...], list[Copy]] = {}
    for copy in found:
        groups.setdefault(find(_keys(copy)[1]), []).append(copy)

    target: dict[str, Decision] = {}
    closed: dict[str, Decision] = {}
    for decision in load_decisions(root):
        for entry in decision.entries:
            (closed if decision.action in CLOSING else target)[entry] = decision
    result = []
    for members in groups.values():
        ids = [copy.entry_id for copy in members]
        targets = [target[entry] for entry in ids if entry in target]
        closings = [closed[entry] for entry in ids if entry in closed]
        latest_target = max(targets, key=lambda decision: decision.recorded_at, default=None)
        raised = [decision for decision in closings if decision.action == "raised"]
        latest = max(raised or closings, key=lambda decision: decision.recorded_at, default=None)
        # A copy no decision covers opens the cluster again, which keeps its last raise, else its last
        # dismissal, so the lead sees what was raised or why it was dismissed.
        status = latest.action if latest and len(closings) == len(ids) else "open"
        decided = sorted(filter(None, (latest_target, latest)), key=lambda decision: decision.recorded_at)
        if status == "open":
            file = next((decision.file for decision in reversed(decided) if decision.file), "")
            note = next((decision.note for decision in reversed(decided) if decision.note), "")
        else:
            file = latest.file or (latest_target.file if latest_target else "")
            note = latest.note
        result.append(
            Cluster(
                cluster_id=members[0].entry_id,
                copies=tuple(members),
                status=status,
                file=file,
                declaration=latest.declaration if latest else "",
                note=note,
            )
        )
    return sorted(result, key=lambda cluster: (cluster.copies[0].recorded_at, cluster.cluster_id))


def present(project: Path, cluster: Cluster) -> dict[str, bool | None]:
    """Whether each copy is still in the project: its file exists and names the copy.

    ``None`` when the copy's file is unknown, lies outside the project, or is not
    a regular file of bounded size. A copy that is gone was already cleaned up,
    by hand or by a later edit.
    """
    project = project.resolve()
    result: dict[str, bool | None] = {}
    for copy in cluster.copies:
        file = _file(copy.copy_file)
        try:
            path = (project / file).resolve() if file else None
        except (OSError, RuntimeError):  # A symlink loop; Python before 3.13 raises RuntimeError.
            path = None
        if path is None or project not in path.parents:
            result[copy.entry_id] = None
            continue
        try:
            text = _source_text(path)
        except OSError:
            result[copy.entry_id] = False
            continue
        name = re.escape(_last_component(copy.copy_declaration))
        result[copy.entry_id] = None if text is None else not name or re.search(
            rf"(?:(?<![\w.'])|{_DECLARED}){name}(?![\w'])", text
        ) is not None
    return result


def _source_text(path: Path) -> str | None:
    """A regular file's text; ``None`` for a FIFO, device or directory, or past the bound. Never blocks."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        data = handle.read(_MAX_SOURCE + 1)
    return None if len(data) > _MAX_SOURCE else data.decode("utf-8", errors="replace")


def select(root: Path, selector: str) -> Cluster:
    """The cluster with this ID, or the one that holds the copy with this entry ID."""
    for cluster in clusters(root):
        if selector == cluster.cluster_id or any(copy.entry_id == selector for copy in cluster.copies):
            return cluster
    raise AbstractionError(f"no cluster or copy {selector!r}; run `autoform abstraction list --all`")


def decide(
    root: Path,
    selector: str,
    action: str,
    *,
    file: str = "",
    declaration: str = "",
    note: str = "",
) -> tuple[Cluster, Decision]:
    """Record a decision for every copy now in the selected cluster."""
    if action not in ACTIONS:
        raise AbstractionError(f"action must be one of {sorted(ACTIONS)}: {action!r}")
    if action == "target" and not file.strip():
        raise AbstractionError("a target needs the shared file it names")
    if action == "raised" and not declaration.strip():
        raise AbstractionError("a raised cluster needs the shared declaration its copies now use")
    if action == "dismissed" and not note.strip():
        raise AbstractionError("a dismissed cluster needs a note saying why")
    cluster = select(root, selector)
    decision = Decision(
        decision_id=uuid.uuid4().hex,
        entries=tuple(copy.entry_id for copy in cluster.copies),
        action=action,
        file=debrief._text(file, 512),
        declaration=debrief._text(declaration, 512),
        note=debrief._text(note, debrief.MAX_NOTE),
        recorded_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    with store.open_root(root) as root_fd, store.open_subdir(root_fd, DIRECTORY) as directory:
        store.create_exclusive(directory, f"{decision.decision_id}.json", debrief._encode(decision.to_json()))
    return cluster, decision


def render_todo(root: Path, project: Path | None = None) -> Path:
    """Rewrite ``abstractions/TODO.md`` from the records and decisions; return its path."""
    every = clusters(root)
    open_clusters = [cluster for cluster in every if cluster.status == "open"]
    md = debrief._md
    lines = [
        "# Code to raise into shared declarations",
        "",
        "Rendered from subagent debrief records and the lead's decisions; "
        "`autoform abstraction list` is the source of truth.",
        "",
        f"- Open: {len(open_clusters)}",
        f"- Raised: {sum(cluster.status == 'raised' for cluster in every)}",
        f"- Dismissed: {sum(cluster.status == 'dismissed' for cluster in every)}",
        "",
    ]
    for cluster in open_clusters:
        state = present(project, cluster) if project is not None else {}
        lines += [f"## {md(cluster.cluster_id)}", "", f"- **Target file:** {md(cluster.file, 'not chosen')}"]
        # A cluster a new copy opened again shows what it was last raised as, or why it was dismissed.
        if cluster.declaration:
            lines.append(f"- **Last raised as:** {md(cluster.declaration)}")
        if cluster.note:
            lines.append(f"- **Note:** {md(cluster.note)}")
        lines.append("")
        for copy in cluster.copies:
            source = f"{copy.source_file or '?'} {copy.source_declaration or '(inline)'}"
            copied = f"{copy.copy_file or '?'} {copy.copy_declaration or '(inline)'}"
            gone = " (no longer present)" if state.get(copy.entry_id) is False else ""
            lines.append(
                f"- {md(copy.entry_id)}: {md(copied)} copies {md(source)}, "
                f"{md(copy.kind)}, by {md(copy.node_id)}{gone}: {md(copy.what)}"
            )
        lines.append("")
    if not open_clusters:
        lines.append("Nothing to raise.")
    path = root / DIRECTORY
    with store.open_root(path) as directory:
        store.replace_atomic(directory, TODO, ("\n".join(lines) + "\n").encode("utf-8"))
    return path / TODO


def to_json(project: Path | None, selected: list[Cluster]) -> str:
    return json.dumps(
        {
            "clusters": [
                cluster.to_json(present(project, cluster) if project is not None else None) for cluster in selected
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "AbstractionError",
    "Cluster",
    "Copy",
    "Decision",
    "clusters",
    "copies",
    "decide",
    "load_decisions",
    "present",
    "render_todo",
    "select",
]
