"""Compile an Autoform dependency graph from its Markdown blueprint.

Markdown is both the human wiki and the sole authored graph representation:
node paths are stable ids, frontmatter carries checked facts, and links under
the two dependency headings are typed edges. ``Graph`` is only a validated
in-memory projection. It rejects broken links and cycles instead of persisting
a second graph file that could drift from the book.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from .workspace import (
    _DIRECTORY_BINDING_SUPPORTED,
    _WorkspaceRootBinding,
    _open_workspace_root,
    _path_is_reparse_point,
)
from .workspace_manifest import WorkspaceError


_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"(`+).*?\1")
ARTICLE_ID_PATTERN = re.compile(r"af_[0-9a-f]{24}\Z")
SOURCE_UNIT_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_FRONTMATTER_KEYS = frozenset(
    {
        "article_id",
        "declaration",
        "lean",
        "statement",
        "proof",
        "mathlib",
        "mathlib_declaration",
        "mathlib_file",
        "not_ready",
        "origin",
        "discussion",
        "source_units",
    }
)
_FORMALIZED = "formalized"
_TRUE = frozenset({"true", "yes"})
_FALSE = frozenset({"false", "no"})
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)

#: ``## Depends on`` carries the prerequisites needed to *state* a node;
#: ``## Proof depends on`` carries the extra prerequisites its *proof* needs.
#: Both are graph edges, mirroring where leanblueprint places ``\uses``.
_STATEMENT_SECTION = "depends on"
_PROOF_SECTION = "proof depends on"
_SOURCES_SECTION = "sources"


class GraphValidationError(ValueError):
    """A blueprint could not be interpreted as a valid dependency graph."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class Node:
    """One Markdown article in a blueprint.

    Only the ``statement``/``proof``/``mathlib``/``not_ready`` assertions are
    recorded here. Everything a reader thinks of as progress -- ready to state,
    ready to prove, fully proved -- is derived from the graph by
    :mod:`autoform_cli.status`, so it can never go stale.
    """

    id: str
    title: str
    path: Path
    dependencies: tuple[str, ...]
    statement_dependencies: tuple[str, ...] = ()
    proof_dependencies: tuple[str, ...] = ()
    kind: str = "node"
    lean: str | None = None
    declaration: str | None = None
    statement_formalized: bool = False
    proof_formalized: bool = False
    mathlib: bool = False
    mathlib_declaration: str | None = None
    mathlib_file: str | None = None
    not_ready: bool = False
    discussion: str | None = None
    origin: str | None = None
    sources: tuple[str, ...] = ()
    parent: str | None = None
    depth: int = 0
    article_id: str | None = None
    source_sha256: str | None = None
    source_units: tuple[str, ...] = ()

    @property
    def formalizable(self) -> bool:
        """Whether this article names a concrete Lean declaration."""
        return self.declaration is not None


def _restore_node(node: Node, state: list[object]) -> None:
    """Restore both pre-coverage and current slotted ``Node`` pickles."""

    field_names = tuple(Node.__dataclass_fields__)
    if len(state) == len(field_names) - 1:
        state = [*state, ()]
    if len(state) != len(field_names):
        raise ValueError("unsupported Node pickle state")
    for name, value in zip(field_names, state):
        object.__setattr__(node, name, value)


# Python generates its own slotted-frozen dataclass hook. Assign after the
# decorator has run so every supported interpreter uses the compatibility hook.
Node.__setstate__ = _restore_node  # type: ignore[attr-defined]


class _TrackedNodeDict(dict[str, Node]):
    """A normal mutable node dictionary with a cheap structural revision."""

    __slots__ = ("_revision",)

    def __init__(self, *args, **kwargs) -> None:
        self._revision = getattr(self, "_revision", -1) + 1
        super().__init__(*args, **kwargs)

    @property
    def revision(self) -> int:
        return getattr(self, "_revision", 0)

    def _touch(self) -> None:
        self._revision = getattr(self, "_revision", 0) + 1

    def __setitem__(self, key: str, value: Node) -> None:
        self._touch()
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        self._touch()
        super().__delitem__(key)

    def clear(self) -> None:
        self._touch()
        super().clear()

    def pop(self, key, *args):
        self._touch()
        return super().pop(key, *args)

    def popitem(self):
        self._touch()
        return super().popitem()

    def setdefault(self, key, default=None):
        self._touch()
        return super().setdefault(key, default)

    def update(self, *args, **kwargs) -> None:
        self._touch()
        super().update(*args, **kwargs)

    def __ior__(self, other):
        self._touch()
        return super().__ior__(other)

    def __getstate__(self) -> int:
        return self._revision

    def __setstate__(self, state: int) -> None:
        self._revision = max(self.revision, state)


class _GraphCache:
    __slots__ = ("_children_by_parent", "_children_revision", "_source_bytes")

    _children_by_parent: Mapping[str | None, tuple[str, ...]]
    _children_revision: int


@dataclass(frozen=True, slots=True)
class Graph(_GraphCache):
    """A validated blueprint graph, keyed by stable node id."""

    blueprint_dir: Path
    nodes: dict[str, Node]

    def __post_init__(self) -> None:
        if not isinstance(self.nodes, _TrackedNodeDict):
            object.__setattr__(self, "nodes", _TrackedNodeDict(self.nodes))
        if not hasattr(self, "_source_bytes"):
            object.__setattr__(self, "_source_bytes", {})
        self._refresh_children()

    def _refresh_children(self) -> None:
        children: dict[str | None, list[str]] = {}
        for node in self.nodes.values():
            children.setdefault(node.parent, []).append(node.id)
        object.__setattr__(
            self,
            "_children_by_parent",
            MappingProxyType({parent: tuple(node_ids) for parent, node_ids in children.items()}),
        )
        object.__setattr__(self, "_children_revision", self.nodes.revision)

    @property
    def edge_count(self) -> int:
        return sum(len(node.dependencies) for node in self.nodes.values())

    def children(self, node_id: str) -> tuple[str, ...]:
        """Return the direct contained articles of *node_id*."""
        if getattr(self, "_children_revision", -1) != self.nodes.revision:
            self._refresh_children()
        return self._children_by_parent.get(node_id, ())

    def source_bytes(self, node_id: str) -> bytes | None:
        """Return immutable source bytes captured with this graph, when available."""

        return self._source_bytes.get(node_id)


def _restore_graph_state(graph: Graph, state: list[object]) -> None:
    """Restore legacy slot pickles through the current cache initializer."""
    if len(state) != 2:
        raise ValueError("unsupported Graph pickle state")
    blueprint_dir, nodes = state
    object.__setattr__(graph, "blueprint_dir", blueprint_dir)
    object.__setattr__(graph, "nodes", nodes)
    graph.__post_init__()


# Python 3.10's ``dataclass(slots=True, frozen=True)`` replaces a class-defined
# pickle hook. Installing it after decoration keeps old Graph pickles compatible
# on every supported interpreter.
setattr(Graph, "__setstate__", _restore_graph_state)


@dataclass(frozen=True, slots=True)
class _ParsedNode:
    id: str
    title: str
    path: Path
    statement_targets: tuple[str, ...]
    proof_targets: tuple[str, ...]
    source_targets: tuple[str, ...]
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class _NodeSource:
    id: str
    path: Path
    text: str
    source_sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _BoundRoadmapDirectory:
    relative: str
    identity: tuple[int, ...]
    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _BoundRoadmapEntry:
    relative: str
    identity: tuple[int, ...]
    ignored: bool = False


@dataclass(frozen=True, slots=True)
class _PortableRoadmapSnapshot:
    root_identity: tuple[int, ...]
    entries: tuple[tuple[str, tuple[int, ...]], ...]
    directories: tuple[str, ...]
    sources: tuple[_NodeSource, ...]
    issues: tuple[str, ...]


def load_graph(
    blueprint_dir: str | Path,
    *,
    _expected_blueprint_identity: tuple[int, int] | None = None,
    _expected_roadmap_identity: tuple[int, int] | None = None,
) -> Graph:
    """Load and validate Markdown nodes beneath *blueprint_dir*."""

    blueprint = Path(blueprint_dir).expanduser().resolve()
    if not blueprint.is_dir():
        raise GraphValidationError([f"blueprint directory does not exist: {blueprint}"])

    issues: list[str] = []
    parsed: list[_ParsedNode] = []
    canonical_ids: dict[Path, str] = {}
    node_ids: dict[str, Path] = {}
    sources, discovery_issues = _discover_nodes(
        blueprint,
        expected_blueprint_identity=_expected_blueprint_identity,
        expected_roadmap_identity=_expected_roadmap_identity,
    )
    issues.extend(discovery_issues)
    article_ids: dict[str, str] = {}
    source_hashes = {source.id: source.source_sha256 for source in sources}
    source_bytes = {source.id: source.content for source in sources}

    for source in sources:
        canonical = source.path
        if canonical in canonical_ids:
            issues.append(f"{source.id}: duplicates node {canonical_ids[canonical]!r}")
            continue
        if source.id in node_ids:
            issues.append(f"{source.id}: duplicate node id also used by {node_ids[source.id]}")
            continue
        canonical_ids[canonical] = source.id
        node_ids[source.id] = canonical
        node, node_issues = _parse_node(source.id, canonical, source.text)
        issues.extend(node_issues)
        if node is not None:
            article_id = node.metadata.get("article_id")
            if article_id is not None:
                previous = article_ids.get(article_id)
                if previous is not None:
                    issues.append(
                        f"{node.id}: duplicate article_id {article_id!r} also used by {previous}"
                    )
                else:
                    article_ids[article_id] = node.id
            parsed.append(node)

    if issues:
        raise GraphValidationError(issues)

    parents = _article_parents(parsed)
    nodes: dict[str, Node] = {}
    for parsed_node in parsed:

        def resolve(targets: tuple[str, ...], node: _ParsedNode = parsed_node) -> list[str]:
            resolved: list[str] = []
            for target in targets:
                dependency, issue = _resolve_target(node, target, blueprint, canonical_ids)
                if issue:
                    issues.append(issue)
                elif dependency == node.id:
                    issues.append(f"{node.id}: dependency on itself")
                elif dependency not in resolved:
                    resolved.append(dependency)
            return resolved

        statement_dependencies = resolve(parsed_node.statement_targets)
        proof_dependencies = resolve(parsed_node.proof_targets)
        dependencies = list(statement_dependencies)
        dependencies.extend(
            dependency for dependency in proof_dependencies if dependency not in dependencies
        )
        metadata = parsed_node.metadata
        nodes[parsed_node.id] = Node(
            id=parsed_node.id,
            title=parsed_node.title,
            path=parsed_node.path,
            dependencies=tuple(dependencies),
            statement_dependencies=tuple(statement_dependencies),
            proof_dependencies=tuple(proof_dependencies),
            kind="article",
            declaration=metadata.get("declaration"),
            lean=metadata.get("lean"),
            statement_formalized=metadata.get("statement") == _FORMALIZED,
            proof_formalized=metadata.get("proof") == _FORMALIZED,
            mathlib=metadata.get("mathlib") in _TRUE,
            mathlib_declaration=metadata.get("mathlib_declaration"),
            mathlib_file=metadata.get("mathlib_file"),
            not_ready=metadata.get("not_ready") in _TRUE,
            discussion=metadata.get("discussion"),
            origin=metadata.get("origin"),
            sources=parsed_node.source_targets,
            parent=parents[parsed_node.id],
            depth=_article_depth(parsed_node.id, parents),
            article_id=metadata.get("article_id"),
            source_sha256=source_hashes[parsed_node.id],
            source_units=tuple(metadata.get("source_units", "").split(","))
            if metadata.get("source_units")
            else (),
        )

    if not issues:
        issues.extend(_find_cycles(nodes))
    if not issues:
        issues.extend(_find_rollup_cycles(nodes))
    if issues:
        raise GraphValidationError(issues)
    graph = Graph(blueprint_dir=blueprint, nodes=nodes)
    object.__setattr__(graph, "_source_bytes", source_bytes)
    return graph


def _discover_nodes(
    blueprint: Path,
    *,
    expected_blueprint_identity: tuple[int, int] | None = None,
    expected_roadmap_identity: tuple[int, int] | None = None,
) -> tuple[list[_NodeSource], list[str]]:
    roadmap_root = blueprint / "roadmap"
    if not roadmap_root.is_dir():
        return [], [f"roadmap directory does not exist: {roadmap_root}"]
    if not _DIRECTORY_BINDING_SUPPORTED or os.listdir not in getattr(os, "supports_fd", ()):
        return _discover_nodes_portably(
            blueprint,
            roadmap_root,
            expected_blueprint_identity=expected_blueprint_identity,
            expected_roadmap_identity=expected_roadmap_identity,
        )

    try:
        binding = _open_workspace_root(blueprint)
    except WorkspaceError:
        return [], ["blueprint directory cannot be inspected safely"]

    issues: list[str] = []
    sources: list[_NodeSource] = []
    directories: list[_BoundRoadmapDirectory] = []
    entries: list[_BoundRoadmapEntry] = []
    roadmap_descriptor: int | None = None
    try:
        if (
            expected_blueprint_identity is not None
            and binding.identity != expected_blueprint_identity
        ):
            return [], ["blueprint changed while the graph was loaded"]
        try:
            roadmap_identity = os.stat(
                "roadmap",
                dir_fd=binding.descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(roadmap_identity.st_mode):
                return [], [f"roadmap directory does not exist: {roadmap_root}"]
            roadmap_descriptor = os.open(
                "roadmap",
                _DIRECTORY_FLAGS,
                dir_fd=binding.descriptor,
            )
            opened = os.fstat(roadmap_descriptor)
        except (OSError, ValueError):
            return [], [f"roadmap directory does not exist: {roadmap_root}"]
        if _stat_signature(opened) != _stat_signature(roadmap_identity):
            return [], ["roadmap changed while the graph was loaded"]
        if (
            expected_roadmap_identity is not None
            and (opened.st_dev, opened.st_ino) != expected_roadmap_identity
        ):
            return [], ["roadmap changed while the graph was loaded"]
        _scan_bound_roadmap_directory(
            roadmap_descriptor,
            relative="",
            identity=_stat_signature(opened),
            roadmap_root=roadmap_root,
            directories=directories,
            entries=entries,
            sources=sources,
            issues=issues,
        )
        _graph_snapshot_checkpoint("before-final-verification", "")
        _verify_roadmap_snapshot(binding, roadmap_descriptor, directories, entries)
    except (_RoadmapChanged, WorkspaceError):
        return [], ["roadmap changed while the graph was loaded"]
    finally:
        if roadmap_descriptor is not None:
            try:
                os.close(roadmap_descriptor)
            except OSError:
                pass
        binding.close()

    sources.sort(key=lambda source: source.path.as_posix())
    issues.extend(
        _chapter_issues(
            roadmap_root,
            [directory.relative for directory in directories],
            sources,
        )
    )
    return sources, issues


class _RoadmapChanged(Exception):
    """The bound roadmap tree did not remain one filesystem generation."""


def _graph_snapshot_checkpoint(_event: str, _relative: str) -> None:
    """Deterministic roadmap-substitution boundary used by adversarial tests."""


def _stat_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _scan_bound_roadmap_directory(
    descriptor: int,
    *,
    relative: str,
    identity: tuple[int, ...],
    roadmap_root: Path,
    directories: list[_BoundRoadmapDirectory],
    entries: list[_BoundRoadmapEntry],
    sources: list[_NodeSource],
    issues: list[str],
) -> None:
    """Capture one roadmap subtree while retaining only its ancestor descriptors."""

    try:
        names = tuple(sorted(os.listdir(descriptor)))
    except OSError:
        raise _RoadmapChanged from None
    if any(
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        for name in names
    ):
        raise _RoadmapChanged
    directories.append(_BoundRoadmapDirectory(relative, identity, names))
    _graph_snapshot_checkpoint("after-directory-list", relative)
    for name in names:
        child_relative = f"{relative}/{name}" if relative else name
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            raise _RoadmapChanged from None
        child_identity = _stat_signature(metadata)
        if name.startswith("."):
            entries.append(_BoundRoadmapEntry(child_relative, child_identity, ignored=True))
            continue
        if stat.S_ISDIR(metadata.st_mode):
            child_descriptor: int | None = None
            try:
                child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                opened = os.fstat(child_descriptor)
                if _stat_signature(opened) != child_identity:
                    raise _RoadmapChanged
                _scan_bound_roadmap_directory(
                    child_descriptor,
                    relative=child_relative,
                    identity=child_identity,
                    roadmap_root=roadmap_root,
                    directories=directories,
                    entries=entries,
                    sources=sources,
                    issues=issues,
                )
                named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _stat_signature(named) != child_identity:
                    raise _RoadmapChanged
            except OSError:
                raise _RoadmapChanged from None
            finally:
                if child_descriptor is not None:
                    try:
                        os.close(child_descriptor)
                    except OSError:
                        pass
            continue
        entries.append(_BoundRoadmapEntry(child_relative, child_identity))
        if stat.S_ISLNK(metadata.st_mode):
            issues.append(f"{child_relative}: roadmap paths must not be symbolic links")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        if name.casefold() == "readme.md" and name != "README.md":
            issues.append(
                f"{child_relative}: noncanonical README filename; container pages must be named "
                "exactly README.md for portable behavior on case-sensitive filesystems"
            )
        if Path(name).suffix != ".md":
            continue
        try:
            content = _read_bound_roadmap_file(descriptor, name, child_identity)
            text = content.decode("utf-8")
        except UnicodeError as error:
            issues.append(f"{child_relative}: cannot read roadmap page: {error}")
            continue
        source_path = roadmap_root.joinpath(*PurePosixPath(child_relative).parts)
        node_id = _article_id(source_path, roadmap_root)
        sources.append(
            _NodeSource(
                node_id,
                source_path,
                text,
                hashlib.sha256(content).hexdigest(),
                content,
            )
        )
    try:
        if _stat_signature(os.fstat(descriptor)) != identity:
            raise _RoadmapChanged
        if tuple(sorted(os.listdir(descriptor))) != names:
            raise _RoadmapChanged
    except OSError:
        raise _RoadmapChanged from None


def _read_bound_roadmap_file(
    parent_descriptor: int,
    name: str,
    expected: tuple[int, ...],
) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _stat_signature(opened) != expected:
            raise _RoadmapChanged
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _stat_signature(after) != expected or _stat_signature(named) != expected:
            raise _RoadmapChanged
        return b"".join(chunks)
    except (OSError, WorkspaceError):
        raise _RoadmapChanged from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _discover_nodes_portably(
    blueprint: Path,
    roadmap_root: Path,
    *,
    expected_blueprint_identity: tuple[int, int] | None = None,
    expected_roadmap_identity: tuple[int, int] | None = None,
) -> tuple[list[_NodeSource], list[str]]:
    """Keep read-only graph commands usable where descriptor traversal is absent."""

    try:
        blueprint_before = blueprint.stat(follow_symlinks=False)
        first = _portable_roadmap_snapshot(roadmap_root)
        _graph_snapshot_checkpoint("between-portable-snapshots", "")
        second = _portable_roadmap_snapshot(roadmap_root)
        blueprint_after = blueprint.stat(follow_symlinks=False)
    except (OSError, RuntimeError, ValueError, _RoadmapChanged):
        return [], ["roadmap changed while the graph was loaded"]
    if (
        first != second
        or _stat_signature(blueprint_before) != _stat_signature(blueprint_after)
        or (
            expected_blueprint_identity is not None
            and (blueprint_after.st_dev, blueprint_after.st_ino) != expected_blueprint_identity
        )
        or (
            expected_roadmap_identity is not None
            and second.root_identity[:2] != expected_roadmap_identity
        )
    ):
        return [], ["roadmap changed while the graph was loaded"]
    sources = list(second.sources)
    issues = list(second.issues)
    issues.extend(_chapter_issues(roadmap_root, list(second.directories), sources))
    return sources, issues


def _portable_roadmap_snapshot(roadmap_root: Path) -> _PortableRoadmapSnapshot:
    root_before = roadmap_root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(root_before.st_mode) or _path_is_reparse_point(
        roadmap_root, root_before
    ):
        raise _RoadmapChanged
    paths = _portable_roadmap_paths(roadmap_root)
    entries: list[tuple[str, tuple[int, ...]]] = []
    directories: list[str] = []
    sources: list[_NodeSource] = []
    issues: list[str] = []
    for path in paths:
        relative = path.relative_to(roadmap_root).as_posix()
        metadata = path.stat(follow_symlinks=False)
        identity = _stat_signature(metadata)
        entries.append((relative, identity))
        if _path_is_reparse_point(path, metadata):
            issues.append(
                f"{relative}: roadmap paths must not be symbolic links or reparse points"
            )
            continue
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(relative)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        if path.name.casefold() == "readme.md" and path.name != "README.md":
            issues.append(
                f"{relative}: noncanonical README filename; container pages must be named exactly "
                "README.md for portable behavior on case-sensitive filesystems"
            )
        if path.suffix != ".md":
            continue
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            content = stream.read()
            after = os.fstat(stream.fileno())
        final = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or not (
            _stat_signature(opened)
            == _stat_signature(after)
            == _stat_signature(final)
            == identity
        ):
            raise _RoadmapChanged
        try:
            text = content.decode("utf-8")
        except UnicodeError as error:
            issues.append(f"{relative}: cannot read roadmap page: {error}")
            continue
        source_path = roadmap_root.joinpath(*PurePosixPath(relative).parts)
        sources.append(
            _NodeSource(
                _article_id(source_path, roadmap_root),
                source_path,
                text,
                hashlib.sha256(content).hexdigest(),
                content,
            )
        )
    root_after = roadmap_root.stat(follow_symlinks=False)
    if _stat_signature(root_before) != _stat_signature(root_after):
        raise _RoadmapChanged
    return _PortableRoadmapSnapshot(
        root_identity=_stat_signature(root_after),
        entries=tuple(entries),
        directories=tuple(directories),
        sources=tuple(sources),
        issues=tuple(issues),
    )


def _portable_roadmap_paths(roadmap_root: Path) -> tuple[Path, ...]:
    """Enumerate without traversing links or Windows reparse-point directories."""

    paths: list[Path] = []

    def visit(directory: Path) -> None:
        before = directory.stat(follow_symlinks=False)
        names = tuple(sorted(path.name for path in directory.iterdir()))
        for name in names:
            path = directory / name
            metadata = path.stat(follow_symlinks=False)
            if name.startswith("."):
                continue
            paths.append(path)
            if stat.S_ISDIR(metadata.st_mode) and not _path_is_reparse_point(path, metadata):
                visit(path)
        after = directory.stat(follow_symlinks=False)
        final_names = tuple(sorted(path.name for path in directory.iterdir()))
        if _stat_signature(before) != _stat_signature(after) or names != final_names:
            raise _RoadmapChanged

    visit(roadmap_root)
    return tuple(sorted(paths))


def _verify_roadmap_snapshot(
    binding: _WorkspaceRootBinding,
    roadmap_descriptor: int,
    directories: list[_BoundRoadmapDirectory],
    entries: list[_BoundRoadmapEntry],
) -> None:
    expected_directories = {directory.relative: directory for directory in directories}
    expected_entries = {entry.relative: entry for entry in entries}
    visited_directories: set[str] = set()
    visited_entries: set[str] = set()

    def verify_directory(descriptor: int, relative: str) -> None:
        expected = expected_directories.get(relative)
        if expected is None:
            raise _RoadmapChanged
        visited_directories.add(relative)
        opened = os.fstat(descriptor)
        names = tuple(sorted(os.listdir(descriptor)))
        if _stat_signature(opened) != expected.identity or names != expected.names:
            raise _RoadmapChanged
        for name in names:
            child_relative = f"{relative}/{name}" if relative else name
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            directory = expected_directories.get(child_relative)
            if directory is not None:
                if not stat.S_ISDIR(current.st_mode) or _stat_signature(current) != directory.identity:
                    raise _RoadmapChanged
                child_descriptor: int | None = None
                try:
                    child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    child_opened = os.fstat(child_descriptor)
                    if _stat_signature(child_opened) != directory.identity:
                        raise _RoadmapChanged
                    verify_directory(child_descriptor, child_relative)
                    named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if _stat_signature(named) != directory.identity:
                        raise _RoadmapChanged
                finally:
                    if child_descriptor is not None:
                        try:
                            os.close(child_descriptor)
                        except OSError:
                            pass
                continue
            entry = expected_entries.get(child_relative)
            if entry is None or (
                not entry.ignored and _stat_signature(current) != entry.identity
            ):
                raise _RoadmapChanged
            if entry.ignored and stat.S_IFMT(current.st_mode) != stat.S_IFMT(
                entry.identity[2]
            ):
                raise _RoadmapChanged
            visited_entries.add(child_relative)
        after = os.fstat(descriptor)
        if (
            _stat_signature(after) != expected.identity
            or tuple(sorted(os.listdir(descriptor))) != expected.names
        ):
            raise _RoadmapChanged

    try:
        roadmap = expected_directories.get("")
        if roadmap is None:
            raise _RoadmapChanged
        named = os.stat("roadmap", dir_fd=binding.descriptor, follow_symlinks=False)
        if _stat_signature(named) != roadmap.identity:
            raise _RoadmapChanged
        verify_directory(roadmap_descriptor, "")
        if visited_directories != set(expected_directories) or visited_entries != set(
            expected_entries
        ):
            raise _RoadmapChanged
        binding.verify()
    except OSError:
        raise _RoadmapChanged from None


def _chapter_issues(
    roadmap_root: Path,
    directories: list[str],
    sources: list[_NodeSource],
) -> list[str]:
    """Reject a chapter directory that names no chapter.

    Containment is inferred from nested ``README.md`` articles, so a directory
    without one is invisible to the hierarchy: its pages attach to the root and
    the published book has no chapters at all. Every node still parses and
    every link still resolves, which is why this has to be asserted separately
    -- a real project reached publication with 71 of 72 articles at the root
    and a clean ``autoform check``.

    A load failure rather than an audit finding: audit is advisory, the
    generated CI never runs it, and it reports after the fact. The layout
    decides what the book is, so it belongs at the gate every author and both
    workflows already pass through.

    Only directories directly under ``roadmap/`` are chapters. Deeper ones --
    the ``definitions/`` and ``theorems/`` buckets a chapter files its articles
    into -- are a filing convention, and need no chapter page of their own.

    The count is recursive even though the chapter page is not. A chapter whose
    articles all sit in those buckets, ``orphan/theorems/leaf.md`` with nothing
    beside it, has no direct Markdown at all; counting only direct children
    read that as an empty directory and let exactly the layout this rejects
    through, with the articles attaching to the root and never reaching the
    generated nav.
    """

    chapters = sorted(
        relative
        for relative in directories
        if relative and "/" not in relative
    )
    article_counts: dict[str, int] = {}
    chapter_readmes: set[str] = set()
    for source in sources:
        relative = source.path.relative_to(roadmap_root)
        if len(relative.parts) < 2:
            continue
        chapter = relative.parts[0]
        article_counts[chapter] = article_counts.get(chapter, 0) + 1
        if relative.parts == (chapter, "README.md"):
            chapter_readmes.add(chapter)
    issues = []
    for chapter in chapters:
        article_count = article_counts.get(chapter, 0)
        if not article_count:
            continue
        if chapter in chapter_readmes:
            continue
        issues.append(
            f"{chapter}: chapter directory holds {article_count} article(s) but no "
            "README.md, so they attach to the roadmap root instead of a chapter; "
            f"add {chapter}/README.md with the chapter's H1 title"
        )
    return issues


def _article_id(path: Path, roadmap_root: Path) -> str:
    relative = path.relative_to(roadmap_root)
    if relative.name == "README.md":
        parent = relative.parent.as_posix()
        return parent if parent != "." else "roadmap"
    return relative.with_suffix("").as_posix()


def _article_parents(parsed: list[_ParsedNode]) -> dict[str, str | None]:
    """Infer strict single-parent containment from nested README articles."""
    by_path = {node.path: node.id for node in parsed}
    parents: dict[str, str | None] = {}
    for node in parsed:
        candidate = node.path.parent
        if node.path.name == "README.md":
            candidate = candidate.parent
        parent: str | None = None
        while candidate != candidate.parent:
            readme = candidate / "README.md"
            if readme in by_path:
                parent = by_path[readme]
                break
            candidate = candidate.parent
        parents[node.id] = parent
    return parents


def _article_depth(node_id: str, parents: dict[str, str | None]) -> int:
    depth = 0
    parent = parents[node_id]
    while parent is not None:
        depth += 1
        parent = parents[parent]
    return depth


def _parse_node(node_id: str, path: Path, text: str) -> tuple[_ParsedNode | None, list[str]]:
    lines = text.splitlines()
    metadata, body_start, issues = _parse_frontmatter(node_id, lines)
    title: str | None = None
    title_count = 0
    targets: dict[str, list[str]] = {
        _STATEMENT_SECTION: [],
        _PROOF_SECTION: [],
        _SOURCES_SECTION: [],
    }
    section: str | None = None
    fence: tuple[str, int] | None = None
    body = _HTML_COMMENT.sub("", "\n".join(lines[body_start:]))

    for line in body.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            marker_kind = marker[0]
            if fence is None:
                fence = (marker_kind, len(marker))
            elif marker_kind == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            heading_text = heading.group(2).strip()
            if level == 1:
                title_count += 1
                if title is None:
                    title = heading_text
            if level <= 2:
                heading_key = heading_text.casefold()
                section = heading_key if level == 2 and heading_key in targets else None
            continue
        if section is not None:
            for match in _LINK.finditer(_INLINE_CODE.sub("", line)):
                target = match.group(1)
                if target.startswith("<") and target.endswith(">"):
                    target = target[1:-1]
                targets[section].append(target)

    if title is None:
        issues.append(f"{node_id}: missing H1 title")
    elif title_count > 1:
        issues.append(f"{node_id}: multiple H1 titles")
    if issues:
        return None, issues
    parsed = _ParsedNode(
        node_id,
        title,
        path,
        tuple(targets[_STATEMENT_SECTION]),
        tuple(targets[_PROOF_SECTION]),
        tuple(targets[_SOURCES_SECTION]),
        metadata,
    )
    return parsed, []


def _parse_frontmatter(node_id: str, lines: list[str]) -> tuple[dict[str, str], int, list[str]]:
    if not lines or lines[0].strip() != "---":
        return {}, 0, []

    issues: list[str] = []
    metadata: dict[str, str] = {}
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return {}, len(lines), [f"{node_id}: unterminated frontmatter"]

    for line_number, raw in enumerate(lines[1:end], start=2):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            issues.append(f"{node_id}:{line_number}: expected 'key: value' in frontmatter")
            continue
        key, value = (part.strip() for part in stripped.split(":", 1))
        if key not in _FRONTMATTER_KEYS:
            issues.append(f"{node_id}:{line_number}: unsupported frontmatter key {key!r}")
            continue
        if key in metadata:
            issues.append(f"{node_id}:{line_number}: duplicate frontmatter key {key!r}")
            continue
        value = _unquote_scalar(value)
        if not value:
            issues.append(f"{node_id}:{line_number}: empty frontmatter value for {key!r}")
            continue
        value, issue = _normalize_value(node_id, line_number, key, value)
        if issue:
            issues.append(issue)
            continue
        metadata[key] = value

    return metadata, end + 1, issues


def _normalize_value(node_id: str, line_number: int, key: str, value: str) -> tuple[str, str | None]:
    """Canonicalize an assertion value, or explain why it is not one."""
    location = f"{node_id}:{line_number}"
    folded = value.casefold()
    if key == "article_id":
        if not ARTICLE_ID_PATTERN.fullmatch(value):
            return value, f"{location}: malformed article_id {value!r}"
        return value, None
    if key in {"statement", "proof"}:
        if folded != _FORMALIZED:
            return value, f"{location}: {key!r} accepts only {_FORMALIZED!r}; omit the key otherwise"
        return folded, None
    if key in {"mathlib", "not_ready"}:
        if folded not in _TRUE | _FALSE:
            return value, f"{location}: {key!r} accepts only true or false"
        return folded, None
    if key == "origin":
        if folded not in {"cited", "bridged", "background"}:
            return value, f"{location}: 'origin' accepts cited, bridged, or background"
        return folded, None
    if key == "source_units":
        if not (value.startswith("[") and value.endswith("]")):
            return value, (
                f"{location}: 'source_units' must be an inline list such as "
                "[chapter-one, theorem-two]"
            )
        items = tuple(item.strip() for item in value[1:-1].split(","))
        if not items or any(not item for item in items):
            return value, f"{location}: 'source_units' must contain at least one unit id"
        malformed = next((item for item in items if not SOURCE_UNIT_PATTERN.fullmatch(item)), None)
        if malformed is not None:
            return value, f"{location}: malformed source unit id {malformed!r}"
        if len(set(items)) != len(items):
            return value, f"{location}: duplicate source unit id in 'source_units'"
        return ",".join(items), None
    return value, None


def _unquote_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _resolve_target(
    node: _ParsedNode,
    target: str,
    blueprint: Path,
    canonical_ids: dict[Path, str],
) -> tuple[str | None, str | None]:
    split = urlsplit(target)
    if split.scheme or split.netloc or split.query:
        return None, f"{node.id}: dependency target must be a relative Markdown path: {target!r}"
    raw_path = unquote(split.path)
    if not raw_path:
        return None, f"{node.id}: dependency target must name a Markdown file: {target!r}"
    relative = Path(raw_path)
    if relative.is_absolute() or relative.suffix != ".md":
        return None, f"{node.id}: dependency target must be a relative .md file: {target!r}"

    resolved = Path(os.path.abspath(node.path.parent / relative))
    if not _is_within(resolved, blueprint):
        return None, f"{node.id}: dependency target escapes the blueprint directory: {target!r}"
    dependency = canonical_ids.get(resolved)
    if dependency is None:
        return None, f"{node.id}: dependency target does not exist: {target!r}"
    return dependency, None


def _find_cycles(nodes: dict[str, Node]) -> list[str]:
    state: dict[str, int] = {}
    stack: list[str] = []
    stack_indexes: dict[str, int] = {}
    issues: list[str] = []
    seen_issues: set[str] = set()

    for root_id in sorted(nodes):
        if state.get(root_id, 0) != 0:
            continue
        state[root_id] = 1
        stack_indexes[root_id] = len(stack)
        stack.append(root_id)
        frames = [(root_id, 0)]
        while frames:
            node_id, dependency_index = frames[-1]
            dependencies = nodes[node_id].dependencies
            if dependency_index == len(dependencies):
                frames.pop()
                stack.pop()
                stack_indexes.pop(node_id)
                state[node_id] = 2
                continue

            dependency = dependencies[dependency_index]
            frames[-1] = (node_id, dependency_index + 1)
            dependency_state = state.get(dependency, 0)
            if dependency_state == 0:
                state[dependency] = 1
                stack_indexes[dependency] = len(stack)
                stack.append(dependency)
                frames.append((dependency, 0))
            elif dependency_state == 1:
                cycle = stack[stack_indexes[dependency] :] + [dependency]
                message = f"dependency cycle: {' -> '.join(cycle)}"
                if message not in seen_issues:
                    seen_issues.add(message)
                    issues.append(message)
    return issues


def _find_rollup_cycles(nodes: dict[str, Node]) -> list[str]:
    """Reject cycles introduced by contracting articles at any hierarchy level."""
    children: dict[str | None, list[str]] = {}
    parents: dict[str, str | None] = {}
    for node in nodes.values():
        children.setdefault(node.parent, []).append(node.id)
        parents[node.id] = node.parent

    depths: dict[str, int] = {}
    roots: dict[str, str] = {}
    for node_id in nodes:
        if node_id in depths:
            continue
        trail: list[str] = []
        seen: set[str] = set()
        current: str | None = node_id
        while current is not None and current not in depths:
            if current in seen or current not in parents:
                raise ValueError("article containment is not a forest")
            seen.add(current)
            trail.append(current)
            current = parents[current]
        depth = depths[current] if current is not None else -1
        root = roots[current] if current is not None else trail[-1]
        for descendant in reversed(trail):
            depth += 1
            depths[descendant] = depth
            roots[descendant] = root

    ancestors: list[dict[str, str | None]] = [parents]
    maximum_depth = max(depths.values(), default=0)
    while 1 << len(ancestors) <= maximum_depth:
        previous = ancestors[-1]
        ancestors.append(
            {node_id: previous[parent] if parent is not None else None for node_id, parent in previous.items()}
        )

    def lift(node_id: str, distance: int) -> str:
        level = 0
        while distance:
            if distance & 1:
                parent = ancestors[level][node_id]
                if parent is None:
                    raise ValueError("article containment depth is inconsistent")
                node_id = parent
            distance >>= 1
            level += 1
        return node_id

    def lowest_common_ancestor(first: str, second: str) -> str | None:
        if roots[first] != roots[second]:
            return None
        if depths[first] < depths[second]:
            first, second = second, first
        first = lift(first, depths[first] - depths[second])
        if first == second:
            return first
        for level in range(len(ancestors) - 1, -1, -1):
            first_parent = ancestors[level][first]
            second_parent = ancestors[level][second]
            if first_parent != second_parent:
                if first_parent is None or second_parent is None:
                    continue
                first = first_parent
                second = second_parent
        return parents[first]

    def direct_child(scope: str | None, node_id: str) -> str:
        scope_depth = depths[scope] if scope is not None else -1
        return lift(node_id, depths[node_id] - scope_depth - 1)

    projections: dict[str | None, dict[str, set[str]]] = {}
    for target in nodes.values():
        for dependency in target.dependencies:
            scope = lowest_common_ancestor(target.id, dependency)
            if scope == target.id or scope == dependency:
                continue
            target_child = direct_child(scope, target.id)
            source_child = direct_child(scope, dependency)
            projections.setdefault(scope, {}).setdefault(target_child, set()).add(source_child)

    issues: list[str] = []
    seen_issues: set[str] = set()
    for scope, siblings in children.items():
        if len(siblings) < 2:
            continue
        projected = projections.get(scope)
        if not projected:
            continue
        dependencies = {sibling: projected.get(sibling, set()) for sibling in siblings}
        state: dict[str, int] = {}
        stack: list[str] = []
        stack_indexes: dict[str, int] = {}
        ordered_dependencies = {
            article_id: tuple(sorted(prerequisites)) for article_id, prerequisites in dependencies.items()
        }

        for root_id in sorted(dependencies):
            if state.get(root_id, 0) != 0:
                continue
            state[root_id] = 1
            stack_indexes[root_id] = len(stack)
            stack.append(root_id)
            frames = [(root_id, 0)]
            while frames:
                article_id, dependency_index = frames[-1]
                prerequisites = ordered_dependencies[article_id]
                if dependency_index == len(prerequisites):
                    frames.pop()
                    stack.pop()
                    stack_indexes.pop(article_id)
                    state[article_id] = 2
                    continue

                prerequisite = prerequisites[dependency_index]
                frames[-1] = (article_id, dependency_index + 1)
                prerequisite_state = state.get(prerequisite, 0)
                if prerequisite_state == 0:
                    state[prerequisite] = 1
                    stack_indexes[prerequisite] = len(stack)
                    stack.append(prerequisite)
                    frames.append((prerequisite, 0))
                elif prerequisite_state == 1:
                    cycle = stack[stack_indexes[prerequisite] :] + [prerequisite]
                    label = scope or "root"
                    message = f"rolled-up dependency cycle in {label}: {' -> '.join(cycle)}"
                    if message not in seen_issues:
                        seen_issues.add(message)
                        issues.append(message)
    return issues


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


__all__ = [
    "Graph",
    "GraphValidationError",
    "Node",
    "SOURCE_UNIT_PATTERN",
    "load_graph",
]
