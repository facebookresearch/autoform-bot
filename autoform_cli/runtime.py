"""Build a versioned, immutable runtime projection of a Markdown roadmap.

The roadmap Markdown remains authoritative.  This module copies the validated
canonical graph into a stable in-memory contract for read-only consumers; it
never reads or writes a second graph artifact or any operational state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from . import workspace as workspace_module
from .graph import ARTICLE_ID_PATTERN, Graph, GraphValidationError, load_graph
from .lean import SourceIndex, declaration_names, index_project
from .status import derive, is_definition
from .workspace import (
    Workspace,
    _WorkspaceRootBinding,
    _DIRECTORY_FLAGS,
    _open_workspace_root,
    _path_contains_symlink,
    _path_is_reparse_point,
    _portable_directory_chain,
    discover_workspace,
    resolve_blueprint,
)
from .workspace_manifest import WorkspaceError

RUNTIME_SCHEMA = "autoform-runtime/v1"
RUNTIME_AUTHORITY = "markdown-articles"


class RuntimeProjectionError(ValueError):
    """The canonical graph could not be represented safely at runtime."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(sorted(set(issues)))
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Resolved local paths used while constructing a runtime projection."""

    project_root: Path
    blueprint_dir: Path
    workspace_project_id: str | None = None
    workspace_project_binding_sha256: str | None = None
    workspace_managed: bool = False
    _workspace: Workspace | None = field(default=None, repr=False, compare=False)
    _blueprint_binding: _WorkspaceRootBinding | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _roadmap_identity: tuple[int, int] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _portable_blueprint_identities: tuple[tuple[int, int], ...] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def blueprint_identity(self) -> tuple[int, int] | None:
        """Return the retained blueprint inode identity, when paths are bound."""

        if self._blueprint_binding is None:
            if self._portable_blueprint_identities:
                return self._portable_blueprint_identities[-1]
            return None
        return self._blueprint_binding.identity

    @property
    def roadmap_identity(self) -> tuple[int, int] | None:
        """Return the retained roadmap inode identity, when paths are bound."""

        return self._roadmap_identity

    @property
    def workspace_root_identity(self) -> tuple[int, int] | None:
        """Return the selected workspace root generation, when managed."""

        return self._workspace.root_identity if self._workspace is not None else None

    @property
    def workspace_manifest_sha256(self) -> str | None:
        """Return the manifest digest that selected this runtime project."""

        return self._workspace.manifest_sha256 if self._workspace is not None else None

    @property
    def strongly_bound(self) -> bool:
        """Whether pathname consumers can be tied to retained directories."""

        return self._blueprint_binding is not None

    def require_strong_binding(self, *, operation: str) -> None:
        """Reject mutations and control-plane work on the portable read tier."""

        if not self.strongly_bound:
            raise RuntimeProjectionError(
                [
                    f"{operation} requires descriptor-relative filesystem support; "
                    "this platform supports read-only inspection only"
                ]
            )
        self.verify()

    def verify(self) -> None:
        try:
            if self._workspace is not None:
                self._workspace.verify_root_binding()
            if self._blueprint_binding is not None:
                self._blueprint_binding.verify()
                roadmap = os.stat(
                    "roadmap",
                    dir_fd=self._blueprint_binding.descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(roadmap.st_mode)
                    or (roadmap.st_dev, roadmap.st_ino) != self._roadmap_identity
                ):
                    raise OSError("roadmap directory changed")
            elif self._portable_blueprint_identities is not None:
                if (
                    _portable_directory_chain(self.blueprint_dir)
                    != self._portable_blueprint_identities
                ):
                    raise OSError("blueprint directory changed")
                roadmap = (self.blueprint_dir / "roadmap").stat(follow_symlinks=False)
                if (
                    not stat.S_ISDIR(roadmap.st_mode)
                    or _path_is_reparse_point(self.blueprint_dir / "roadmap", roadmap)
                    or (roadmap.st_dev, roadmap.st_ino) != self._roadmap_identity
                ):
                    raise OSError("roadmap directory changed")
        except WorkspaceError as error:
            raise RuntimeProjectionError(list(error.issues)) from None
        except OSError:
            raise RuntimeProjectionError(["blueprint directory changed during use"]) from None

    def duplicate_blueprint_descriptor(self) -> int:
        """Return a checked descriptor for one blueprint-relative operation."""

        if self._blueprint_binding is None:
            raise RuntimeProjectionError(["runtime paths are not retained"])
        self.verify()
        descriptor: int | None = None
        try:
            descriptor = os.dup(self._blueprint_binding.descriptor)
            opened = os.fstat(descriptor)
        except OSError:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise RuntimeProjectionError(["blueprint directory changed during use"]) from None
        if (opened.st_dev, opened.st_ino) != self._blueprint_binding.identity:
            os.close(descriptor)
            raise RuntimeProjectionError(["blueprint directory changed during use"])
        try:
            self.verify()
        except RuntimeProjectionError:
            os.close(descriptor)
            raise
        return descriptor

    def close(self) -> None:
        if self._blueprint_binding is not None:
            self._blueprint_binding.close()
        if self._workspace is not None:
            self._workspace.close()

    def __del__(self) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class RuntimeAssertions:
    """Authored facts copied from article frontmatter."""

    statement_formalized: bool
    proof_formalized: bool
    not_ready: bool

    def as_dict(self) -> dict[str, bool]:
        return {
            "not_ready": self.not_ready,
            "proof_formalized": self.proof_formalized,
            "statement_formalized": self.statement_formalized,
        }


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Progress recomputed from assertions and typed dependencies."""

    state: str
    can_state: bool
    can_prove: bool
    stated: bool
    proved: bool
    fully_proved: bool
    defined: bool

    def as_dict(self) -> dict[str, bool | str]:
        return {
            "can_prove": self.can_prove,
            "can_state": self.can_state,
            "defined": self.defined,
            "fully_proved": self.fully_proved,
            "proved": self.proved,
            "state": self.state,
            "stated": self.stated,
        }


@dataclass(frozen=True, slots=True)
class RuntimeLeanTarget:
    """One authored Lean declaration and its optional local source file."""

    declaration: str
    source_file: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "declaration": self.declaration,
            "source_file": self.source_file,
        }


@dataclass(frozen=True, slots=True)
class RuntimeNode:
    """One immutable article record exposed to runtime consumers."""

    id: str
    title: str
    article_path: str
    parent: str | None
    depth: int
    declaration: str | None
    formalizable: bool
    dispatchable: bool
    statement_dependencies: tuple[str, ...]
    proof_dependencies: tuple[str, ...]
    dependencies: tuple[str, ...]
    assertions: RuntimeAssertions
    status: RuntimeStatus
    origin: str | None
    source_targets: tuple[str, ...]
    lean_targets: tuple[RuntimeLeanTarget, ...]
    mathlib: bool
    mathlib_declarations: tuple[str, ...]
    mathlib_file: str | None
    article_id: str | None = None
    source_sha256: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "article_id": self.article_id,
            "article_path": self.article_path,
            "assertions": self.assertions.as_dict(),
            "declaration": self.declaration,
            "dependencies": list(self.dependencies),
            "depth": self.depth,
            "dispatchable": self.dispatchable,
            "formalizable": self.formalizable,
            "id": self.id,
            "lean_targets": [target.as_dict() for target in self.lean_targets],
            "mathlib": self.mathlib,
            "mathlib_declarations": list(self.mathlib_declarations),
            "mathlib_file": self.mathlib_file,
            "origin": self.origin,
            "parent": self.parent,
            "proof_dependencies": list(self.proof_dependencies),
            "source_sha256": self.source_sha256,
            "source_targets": list(self.source_targets),
            "statement_dependencies": list(self.statement_dependencies),
            "status": self.status.as_dict(),
            "title": self.title,
        }


class _RuntimeGraphCache:
    __slots__ = ("_nodes_by_id",)

    _nodes_by_id: Mapping[str, RuntimeNode]


@dataclass(frozen=True, slots=True)
class RuntimeGraph(_RuntimeGraphCache):
    """The complete versioned runtime view of an authored roadmap."""

    schema: str
    authority: str
    source_revision: str
    blueprint_path: str
    nodes: tuple[RuntimeNode, ...]
    article_count: int
    formalizable_count: int
    dispatchable_count: int
    dependency_count: int
    maximum_depth: int

    def __post_init__(self) -> None:
        index: dict[str, RuntimeNode] = {}
        for node in self.nodes:
            index.setdefault(node.id, node)
        object.__setattr__(self, "_nodes_by_id", MappingProxyType(index))

    def get(self, node_id: str) -> RuntimeNode | None:
        """Return a node without exposing mutable lookup state."""

        try:
            index = self._nodes_by_id
        except AttributeError:
            self.__post_init__()
            index = self._nodes_by_id
        return index.get(node_id)

    def as_dict(self) -> dict[str, object]:
        """Return a canonical JSON-compatible compatibility snapshot."""

        return {
            "article_count": self.article_count,
            "authority": self.authority,
            "blueprint_path": self.blueprint_path,
            "dependency_count": self.dependency_count,
            "dispatchable_count": self.dispatchable_count,
            "formalizable_count": self.formalizable_count,
            "maximum_depth": self.maximum_depth,
            "nodes": [node.as_dict() for node in self.nodes],
            "schema": self.schema,
            "source_revision": self.source_revision,
        }

    def to_json(self) -> str:
        """Serialize the compatibility snapshot deterministically."""

        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def resolve_runtime_paths(
    project_or_blueprint: str | Path,
    *,
    project_id: str | None = None,
    _retain_workspace: bool = False,
) -> RuntimePaths:
    """Resolve a workspace project, legacy project, or explicit blueprint.

    A root ``.autoform.toml`` is authoritative for project selection. Explicit
    vault paths remain usable, including unregistered vaults, while repository-
    wide operations see only projects registered in the manifest.
    """

    supplied = Path(project_or_blueprint).expanduser()
    retained = False
    blueprint_binding: _WorkspaceRootBinding | None = None
    portable_blueprint_identities: tuple[tuple[int, int], ...] | None = None
    try:
        if _path_contains_symlink(supplied.absolute()):
            raise RuntimeProjectionError(["project or blueprint path contains a symbolic link"])
        candidate = supplied.resolve()
    except (OSError, RuntimeError, ValueError, WorkspaceError):
        raise RuntimeProjectionError(["project or blueprint path cannot be resolved safely"]) from None
    if not _is_real_directory(candidate):
        raise RuntimeProjectionError(["project or blueprint directory does not exist"])

    try:
        workspace = discover_workspace(candidate)
    except WorkspaceError as error:
        if error.issues != ("no enclosing .autoform.toml was found",):
            raise RuntimeProjectionError(list(error.issues)) from None
        workspace = None
    try:
        workspace_project_id: str | None = None
        workspace_project_binding_sha256: str | None = None
        is_blueprint = _is_real_directory(candidate / "roadmap")
        is_project = _is_real_directory(candidate / "blueprint") and _is_real_directory(
            candidate / "blueprint" / "roadmap"
        )
        if workspace is not None and project_id is not None:
            previous_workspace = workspace
            workspace, project, blueprint_dir = resolve_blueprint(
                candidate,
                project_id=project_id,
            )
            previous_workspace.close()
            project_root = workspace.root
            workspace_project_id = project.id
            workspace_project_binding_sha256 = workspace.project_binding_sha256(project)
        elif workspace is not None and is_blueprint:
            project_root = workspace.root
            blueprint_dir = candidate
            matches = tuple(
                project
                for project in workspace.manifest.projects
                if workspace.blueprint_path(project).resolve() == candidate
            )
            if len(matches) == 1:
                relative = workspace.manifest.blueprint_relative(matches[0])
                workspace.bind_managed_directory(relative)
                workspace.bind_managed_directory(relative / "roadmap")
                workspace_project_id = matches[0].id
                workspace_project_binding_sha256 = workspace.project_binding_sha256(matches[0])
        elif workspace is not None:
            previous_workspace = workspace
            workspace, project, blueprint_dir = resolve_blueprint(candidate)
            previous_workspace.close()
            project_root = workspace.root
            workspace_project_id = project.id
            workspace_project_binding_sha256 = workspace.project_binding_sha256(project)
        elif project_id is not None:
            raise RuntimeProjectionError(["--project requires an enclosing .autoform.toml"])
        elif is_blueprint and is_project:
            raise RuntimeProjectionError(["input is ambiguous between a project and blueprint directory"])
        elif is_project:
            project_root = candidate
            blueprint_dir = candidate / "blueprint"
        elif is_blueprint:
            blueprint_dir = candidate
            project_root = candidate.parent.resolve()
        else:
            raise RuntimeProjectionError(["roadmap directory does not exist"])

        try:
            blueprint_dir.relative_to(project_root)
        except ValueError as error:
            raise RuntimeProjectionError(["blueprint directory escapes the project root"]) from error
        if not _retain_workspace:
            _reject_roadmap_symlinks(blueprint_dir)
        if workspace is not None:
            workspace.verify_root_binding()
        roadmap_identity: tuple[int, int] | None = None
        if _retain_workspace:
            if workspace_module._DIRECTORY_BINDING_SUPPORTED:
                blueprint_binding = _open_workspace_root(blueprint_dir)
                blueprint_descriptor = blueprint_binding.descriptor
            else:
                portable_blueprint_identities = _portable_directory_chain(blueprint_dir)
                blueprint_descriptor = None
            try:
                roadmap = (
                    os.stat(
                        "roadmap",
                        dir_fd=blueprint_descriptor,
                        follow_symlinks=False,
                    )
                    if blueprint_descriptor is not None
                    else (blueprint_dir / "roadmap").stat(follow_symlinks=False)
                )
            except OSError:
                raise RuntimeProjectionError(["roadmap directory changed during selection"]) from None
            if not stat.S_ISDIR(roadmap.st_mode) or (
                blueprint_descriptor is None
                and _path_is_reparse_point(blueprint_dir / "roadmap", roadmap)
            ):
                raise RuntimeProjectionError(["roadmap directory changed during selection"])
            roadmap_identity = (roadmap.st_dev, roadmap.st_ino)
            if blueprint_binding is not None:
                blueprint_binding.verify()
            elif _portable_directory_chain(blueprint_dir) != portable_blueprint_identities:
                raise RuntimeProjectionError(["blueprint directory changed during selection"])
            if workspace is not None:
                workspace.verify_root_binding()
        result = RuntimePaths(
            project_root=project_root,
            blueprint_dir=blueprint_dir,
            workspace_project_id=workspace_project_id,
            workspace_project_binding_sha256=workspace_project_binding_sha256,
            workspace_managed=workspace is not None,
            _workspace=workspace if _retain_workspace else None,
            _blueprint_binding=blueprint_binding,
            _roadmap_identity=roadmap_identity,
            _portable_blueprint_identities=portable_blueprint_identities,
        )
        retained = _retain_workspace
        return result
    except WorkspaceError as error:
        raise RuntimeProjectionError(list(error.issues)) from None
    except OSError:
        raise RuntimeProjectionError(["blueprint directory changed during selection"]) from None
    finally:
        if not retained:
            if blueprint_binding is not None:
                blueprint_binding.close()
            if workspace is not None:
                workspace.close()


@contextmanager
def bind_runtime_paths(
    project_or_blueprint: str | Path,
    *,
    project_id: str | None = None,
) -> Iterator[RuntimePaths]:
    """Retain the selected workspace generation while its paths are consumed."""

    paths = resolve_runtime_paths(
        project_or_blueprint,
        project_id=project_id,
        _retain_workspace=True,
    )
    try:
        yield paths
    finally:
        paths.close()


def load_runtime_graph(
    project_or_blueprint: str | Path,
    *,
    lean_root: str | Path | None = None,
    project_id: str | None = None,
    _paths: RuntimePaths | None = None,
) -> RuntimeGraph:
    """Load the canonical graph once and return its immutable runtime view."""

    if _paths is not None:
        try:
            graph = load_bound_graph(_paths)
        except GraphValidationError as error:
            raise RuntimeProjectionError(list(error.issues)) from error
        _validate_source_target_paths(graph, _paths)
        runtime = build_runtime_graph(
            graph,
            project_root=_paths.project_root,
            lean_root=lean_root,
        )
        _paths.verify()
        return runtime
    try:
        with bind_runtime_paths(project_or_blueprint, project_id=project_id) as paths:
            try:
                graph = load_bound_graph(paths)
            except GraphValidationError as error:
                raise RuntimeProjectionError(list(error.issues)) from error
            _validate_source_target_paths(graph, paths)
            runtime = build_runtime_graph(
                graph,
                project_root=paths.project_root,
                lean_root=lean_root,
            )
            paths.verify()
            return runtime
    except WorkspaceError as error:
        raise RuntimeProjectionError(list(error.issues)) from None


def load_bound_graph(paths: RuntimePaths) -> Graph:
    """Load a graph tied to the exact retained RuntimePaths generation."""

    if paths.blueprint_identity is None or paths.roadmap_identity is None:
        raise RuntimeProjectionError(["runtime paths are not retained"])
    paths.verify()
    try:
        graph = load_graph(
            paths.blueprint_dir,
            _expected_blueprint_identity=paths.blueprint_identity,
            _expected_roadmap_identity=paths.roadmap_identity,
        )
    except GraphValidationError:
        paths.verify()
        raise
    paths.verify()
    return graph


def build_runtime_graph(
    graph: Graph,
    *,
    project_root: str | Path,
    lean_root: str | Path | None = None,
    _lean_index: SourceIndex | None = None,
) -> RuntimeGraph:
    """Copy an already validated canonical graph into the runtime contract."""

    project = Path(os.path.abspath(Path(project_root).expanduser()))
    blueprint = Path(os.path.abspath(graph.blueprint_dir))
    issues: list[str] = []
    try:
        blueprint_path = blueprint.relative_to(project).as_posix()
    except ValueError:
        raise RuntimeProjectionError(["blueprint directory escapes the project root"]) from None

    legacy_source_paths = any(graph.source_bytes(node_id) is None for node_id in graph.nodes)
    if legacy_source_paths:
        _reject_roadmap_symlinks(blueprint)
    node_ids = set(graph.nodes)
    article_paths: dict[str, str] = {}
    seen_article_paths: set[str] = set()
    revision_paths: dict[str, str] = {}
    article_bytes: dict[str, bytes] = {}

    for key in sorted(graph.nodes):
        node = graph.nodes[key]
        if key != node.id:
            issues.append(f"node key does not match node id: {key}")
        if node.parent is not None and node.parent not in node_ids:
            issues.append(f"{node.id}: parent does not name a runtime node")
        expected_dependencies = _ordered_union(node.statement_dependencies, node.proof_dependencies)
        if node.dependencies != expected_dependencies:
            issues.append(f"{node.id}: dependency union does not match typed dependencies")
        for dependency in (*node.statement_dependencies, *node.proof_dependencies, *node.dependencies):
            if dependency not in node_ids:
                issues.append(f"{node.id}: dependency does not name a runtime node: {dependency}")

        captured = graph.source_bytes(node.id)
        try:
            article = (
                Path(os.path.abspath(node.path))
                if captured is not None
                else node.path.resolve()
            )
            relative_blueprint = article.relative_to(blueprint)
            relative_project = article.relative_to(project)
        except ValueError:
            issues.append(f"{node.id}: article path escapes the project or blueprint")
            continue
        if not relative_blueprint.parts or relative_blueprint.parts[0] != "roadmap":
            issues.append(f"{node.id}: article is not under the canonical roadmap directory")
            continue
        if _article_id(relative_blueprint) != node.id:
            issues.append(f"{node.id}: article path does not match its path-derived id")
        if captured is None and node.path.is_symlink():
            issues.append(f"{node.id}: article path is a symbolic link")
        content = captured
        if content is None and node.source_sha256 is not None:
            # Graphs deserialized from the pre-snapshot pickle format have no
            # captured bytes. Retain their compatibility path without making
            # normal graph loads reopen a pathname after validation.
            try:
                content = node.path.read_bytes()
            except OSError:
                issues.append(f"{node.id}: article cannot be read")
                continue
        if node.source_sha256 is None:
            issues.append(f"{node.id}: article source digest is unavailable")
        elif content is None:
            issues.append(f"{node.id}: article source bytes are unavailable")
        elif hashlib.sha256(content).hexdigest() != node.source_sha256:
            issues.append(f"{node.id}: captured article bytes do not match their digest")
        if content is None:
            continue
        article_path = relative_project.as_posix()
        if article_path in seen_article_paths:
            issues.append(f"{node.id}: article path is duplicated")
        seen_article_paths.add(article_path)
        article_paths[node.id] = article_path
        revision_paths[node.id] = relative_blueprint.as_posix()
        article_bytes[node.id] = content

        for target in node.sources:
            if not _source_target_is_confined(node.path, target, blueprint):
                issues.append(f"{node.id}: source target escapes the blueprint or uses an unsupported location")
        if node.mathlib_file is not None and not _is_portable_relative_path(node.mathlib_file):
            issues.append(f"{node.id}: mathlib file must be a portable relative path")

    _validate_depths(graph, issues)
    if issues:
        raise RuntimeProjectionError(issues)

    statuses = derive(graph)
    lean_index = _lean_index
    if lean_index is None and lean_root is not None:
        try:
            root = Path(lean_root).expanduser().resolve()
            if not root.is_dir():
                raise RuntimeProjectionError(
                    ["Lean root does not exist or is not a directory"]
                )
            lean_index = index_project(root)
        except RuntimeProjectionError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise RuntimeProjectionError(
                ["Lean sources cannot be indexed safely"]
            ) from error

    runtime_nodes: list[RuntimeNode] = []
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        node_status = statuses[node_id]
        can_state = all(statuses[dependency].stated for dependency in node.statement_dependencies)
        can_prove = (
            node_status.stated
            and can_state
            and all(statuses[dependency].proved for dependency in node.proof_dependencies)
        )
        children = graph.children(node_id)
        lean_targets: list[RuntimeLeanTarget] = []
        for name in declaration_names(node.lean or ""):
            declaration = lean_index.find(name) if lean_index is not None else None
            source_file = declaration.path.as_posix() if declaration is not None else None
            if source_file is not None and not _is_portable_relative_path(source_file):
                raise RuntimeProjectionError([f"{node.id}: Lean source file escapes the Lean root"])
            lean_targets.append(RuntimeLeanTarget(name, source_file))

        runtime_nodes.append(
            RuntimeNode(
                id=node.id,
                article_id=node.article_id,
                title=node.title,
                article_path=article_paths[node.id],
                parent=node.parent,
                depth=node.depth,
                declaration=node.declaration,
                formalizable=node.formalizable,
                dispatchable=node.formalizable and not children,
                statement_dependencies=node.statement_dependencies,
                proof_dependencies=node.proof_dependencies,
                dependencies=node.dependencies,
                assertions=RuntimeAssertions(
                    statement_formalized=node.statement_formalized,
                    proof_formalized=node.proof_formalized,
                    not_ready=node.not_ready,
                ),
                status=RuntimeStatus(
                    state=node_status.key,
                    can_state=can_state,
                    can_prove=can_prove,
                    stated=node_status.stated,
                    proved=node_status.proved,
                    fully_proved=node_status.fully_proved,
                    defined=is_definition(node) and node_status.stated,
                ),
                origin=node.origin,
                source_targets=node.sources,
                lean_targets=tuple(lean_targets),
                mathlib=node.mathlib,
                mathlib_declarations=tuple(declaration_names(node.mathlib_declaration or "")),
                mathlib_file=node.mathlib_file.replace("\\", "/") if node.mathlib_file else None,
                source_sha256=node.source_sha256,
            )
        )

    nodes = tuple(runtime_nodes)
    runtime = RuntimeGraph(
        schema=RUNTIME_SCHEMA,
        authority=RUNTIME_AUTHORITY,
        source_revision=_source_revision(revision_paths, article_bytes),
        blueprint_path=blueprint_path,
        nodes=nodes,
        article_count=len(nodes),
        formalizable_count=sum(node.formalizable for node in nodes),
        dispatchable_count=sum(node.dispatchable for node in nodes),
        dependency_count=sum(len(node.dependencies) for node in nodes),
        maximum_depth=max((node.depth for node in nodes), default=0),
    )
    _validate_runtime(runtime)
    return runtime


def _reject_roadmap_symlinks(blueprint: Path) -> None:
    roadmap = blueprint / "roadmap"
    try:
        roadmap_metadata = roadmap.stat(follow_symlinks=False)
    except OSError:
        raise RuntimeProjectionError(["roadmap directory does not exist"])
    if _path_is_reparse_point(roadmap, roadmap_metadata):
        raise RuntimeProjectionError(["roadmap contains a symbolic link or reparse point: roadmap"])
    if not stat.S_ISDIR(roadmap_metadata.st_mode):
        raise RuntimeProjectionError(["roadmap directory does not exist"])
    pending = [roadmap]
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError:
            raise RuntimeProjectionError(["roadmap directory cannot be inspected safely"]) from None
        for entry in entries:
            path = directory / entry.name
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                raise RuntimeProjectionError(["roadmap directory cannot be inspected safely"]) from None
            if _path_is_reparse_point(path, metadata):
                label = path.relative_to(blueprint).as_posix()
                raise RuntimeProjectionError(
                    [f"roadmap contains a symbolic link or reparse point: {label}"]
                )
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)


def _is_real_directory(path: Path) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not _path_is_reparse_point(path, metadata)


def _ordered_union(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    values = list(first)
    seen = set(values)
    for value in second:
        if value not in seen:
            values.append(value)
            seen.add(value)
    return tuple(values)


def _article_id(relative_blueprint: Path) -> str:
    relative = relative_blueprint.relative_to("roadmap")
    if relative.name.casefold() == "readme.md":
        parent = relative.parent.as_posix()
        return parent if parent != "." else "roadmap"
    return relative.with_suffix("").as_posix()


def _source_target_is_confined(article: Path, target: str, blueprint: Path) -> bool:
    relative = _source_target_relative(article, target, blueprint)
    walk = _source_target_walk(article, target, blueprint)
    if relative is False or walk is False:
        return False
    if relative is None or walk is None:
        return True
    try:
        return not _relative_path_traverses_link_portably(blueprint, walk)
    except OSError:
        return False


def _source_target_relative(
    article: Path,
    target: str,
    blueprint: Path,
) -> PurePosixPath | None | bool:
    """Resolve a source link lexically, without consulting mutable pathnames."""

    components = _source_target_walk(article, target, blueprint)
    if components is None or components is False:
        return components
    parts: list[str] = []
    for part in components.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return False
            parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts)


def _source_target_walk(
    article: Path,
    target: str,
    blueprint: Path,
) -> PurePosixPath | None | bool:
    """Return every lexical component traversed by a local source target."""

    try:
        split = urlsplit(target)
    except ValueError:
        return False
    if split.scheme.casefold() in {"http", "https"}:
        return None
    if split.scheme or split.netloc:
        return False
    value = unquote(split.path)
    if not value:
        try:
            return PurePosixPath(article.relative_to(blueprint).as_posix())
        except ValueError:
            return False
    path = Path(value)
    windows = PureWindowsPath(value)
    if (
        path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or "\\" in value
        or "\x00" in value
    ):
        return False
    try:
        article_relative = PurePosixPath(article.relative_to(blueprint).as_posix())
    except ValueError:
        return False
    return PurePosixPath(*article_relative.parent.parts, *PurePosixPath(value).parts)


def _validate_source_target_paths(graph: Graph, paths: RuntimePaths) -> None:
    """Reject local source targets whose selected path traverses a link."""

    if not paths.strongly_bound:
        try:
            for node in graph.nodes.values():
                for target in node.sources:
                    walk = _source_target_walk(node.path, target, graph.blueprint_dir)
                    if walk is None or walk is False:
                        continue
                    if _relative_path_traverses_link_portably(paths.blueprint_dir, walk):
                        raise RuntimeProjectionError(
                            [
                                f"{node.id}: source target escapes the blueprint "
                                "through a symbolic link or reparse point"
                            ]
                        )
            paths.verify()
        except OSError:
            raise RuntimeProjectionError(["source target path changed during use"]) from None
        return

    root_descriptor = paths.duplicate_blueprint_descriptor()
    try:
        try:
            for node in graph.nodes.values():
                for target in node.sources:
                    walk = _source_target_walk(node.path, target, graph.blueprint_dir)
                    if walk is None or walk is False:
                        continue
                    if _relative_path_traverses_link(root_descriptor, walk):
                        raise RuntimeProjectionError(
                            [
                                f"{node.id}: source target escapes the blueprint "
                                "through a symbolic link"
                            ]
                        )
            paths.verify()
        except OSError:
            raise RuntimeProjectionError(["source target path changed during use"]) from None
    finally:
        os.close(root_descriptor)


def _relative_path_traverses_link_portably(
    root: Path,
    relative: PurePosixPath,
) -> bool:
    """Best-effort no-follow source validation for read-only portable clients."""

    def inspect() -> tuple[bool, tuple[tuple[str, tuple[int, ...]], ...]]:
        stack = [root]
        observed: list[tuple[str, tuple[int, ...]]] = []
        for index, part in enumerate(relative.parts):
            if part == "..":
                if len(stack) == 1:
                    return True, tuple(observed)
                stack.pop()
                continue
            current = stack[-1] / part
            try:
                metadata = current.stat(follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                break
            signature = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            observed.append((current.relative_to(root).as_posix(), signature))
            if _path_is_reparse_point(current, metadata):
                return True, tuple(observed)
            if index == len(relative.parts) - 1:
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                break
            stack.append(current)
        return False, tuple(observed)

    first = inspect()
    second = inspect()
    if first != second:
        raise OSError("source target path changed")
    return first[0]


def _relative_path_traverses_link(
    root_descriptor: int,
    relative: PurePosixPath,
) -> bool:
    descriptors: list[int] = []
    observed: list[tuple[int, str, tuple[int, int, int]]] = []
    stack = [root_descriptor]
    try:
        for index, part in enumerate(relative.parts):
            if part == "..":
                if len(stack) == 1:
                    return True
                stack.pop()
                continue
            parent = stack[-1]
            try:
                metadata = os.stat(part, dir_fd=parent, follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                break
            signature = (metadata.st_dev, metadata.st_ino, metadata.st_mode)
            observed.append((parent, part, signature))
            if stat.S_ISLNK(metadata.st_mode):
                return True
            if index == len(relative.parts) - 1 or not stat.S_ISDIR(metadata.st_mode):
                break
            descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_mode) != signature:
                os.close(descriptor)
                raise OSError("source target path changed")
            descriptors.append(descriptor)
            stack.append(descriptor)
        for observed_parent, part, signature in observed:
            current = os.stat(part, dir_fd=observed_parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_mode) != signature:
                raise OSError("source target path changed")
        return False
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _is_portable_relative_path(value: str) -> bool:
    path = Path(value)
    windows = PureWindowsPath(value)
    return (
        not path.is_absolute()
        and not windows.is_absolute()
        and not windows.drive
        and ".." not in path.parts
        and ".." not in windows.parts
    )


def _validate_depths(graph: Graph, issues: list[str]) -> None:
    resolved: dict[str, int] = {}
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        if node_id not in resolved:
            trail: list[str] = []
            seen: set[str] = set()
            current = node_id
            valid = True
            while current not in resolved:
                if current in seen or current not in graph.nodes:
                    valid = False
                    break
                seen.add(current)
                trail.append(current)
                parent = graph.nodes[current].parent
                if parent is None:
                    depth = -1
                    break
                current = parent
            else:
                depth = resolved[current]

            if valid:
                for candidate in reversed(trail):
                    depth += 1
                    resolved[candidate] = depth

        if node_id in resolved:
            depth = resolved[node_id]
        else:
            seen = set()
            parent = node.parent
            depth = 0
            while parent is not None:
                if parent in seen or parent not in graph.nodes:
                    break
                seen.add(parent)
                depth += 1
                parent = graph.nodes[parent].parent
        if node.depth != depth:
            issues.append(f"{node.id}: depth does not match the parent chain")


def _source_revision(article_paths: dict[str, str], article_bytes: dict[str, bytes]) -> str:
    digest = hashlib.sha256(b"autoform-runtime-source/v1\0")
    for node_id in sorted(article_paths):
        path = os.fsencode(article_paths[node_id])
        content = article_bytes[node_id]
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _validate_runtime(runtime: RuntimeGraph) -> None:
    issues: list[str] = []
    nodes = {node.id: node for node in runtime.nodes}
    article_ids: dict[str, str] = {}
    parents_with_children = {
        node.parent for node in runtime.nodes if node.parent is not None
    }
    if len(nodes) != len(runtime.nodes):
        issues.append("runtime node ids are not unique")
    for node in runtime.nodes:
        if node.article_id is not None:
            if ARTICLE_ID_PATTERN.fullmatch(node.article_id) is None:
                issues.append(f"{node.id}: runtime article_id is malformed")
            previous = article_ids.get(node.article_id)
            if previous is not None:
                issues.append(f"{node.id}: runtime article_id also names {previous}")
            article_ids[node.article_id] = node.id
        if node.source_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", node.source_sha256) is None:
            issues.append(f"{node.id}: runtime source_sha256 is malformed")
        if node.parent is not None and node.parent not in nodes:
            issues.append(f"{node.id}: runtime parent does not resolve")
        if node.dependencies != _ordered_union(node.statement_dependencies, node.proof_dependencies):
            issues.append(f"{node.id}: runtime dependency union is inconsistent")
        if any(dependency not in nodes for dependency in node.dependencies):
            issues.append(f"{node.id}: runtime dependency does not resolve")
        has_children = node.id in parents_with_children
        if node.dispatchable and (not node.formalizable or has_children):
            issues.append(f"{node.id}: dispatchable node is not a formalizable leaf")
        if Path(node.article_path).is_absolute() or PureWindowsPath(node.article_path).is_absolute():
            issues.append(f"{node.id}: runtime article path is absolute")
    if runtime.article_count != len(runtime.nodes):
        issues.append("runtime article count is inconsistent")
    if runtime.formalizable_count != sum(node.formalizable for node in runtime.nodes):
        issues.append("runtime formalizable count is inconsistent")
    if runtime.dispatchable_count != sum(node.dispatchable for node in runtime.nodes):
        issues.append("runtime dispatchable count is inconsistent")
    if runtime.dependency_count != sum(len(node.dependencies) for node in runtime.nodes):
        issues.append("runtime dependency count is inconsistent")
    if runtime.maximum_depth != max((node.depth for node in runtime.nodes), default=0):
        issues.append("runtime maximum depth is inconsistent")
    if issues:
        raise RuntimeProjectionError(issues)


__all__ = [
    "RUNTIME_AUTHORITY",
    "RUNTIME_SCHEMA",
    "RuntimeAssertions",
    "RuntimeGraph",
    "RuntimeLeanTarget",
    "RuntimeNode",
    "RuntimePaths",
    "RuntimeProjectionError",
    "RuntimeStatus",
    "bind_runtime_paths",
    "build_runtime_graph",
    "load_bound_graph",
    "load_runtime_graph",
    "resolve_runtime_paths",
]
