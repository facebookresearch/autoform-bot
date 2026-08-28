"""Build a versioned, immutable runtime projection of a Markdown roadmap.

The roadmap Markdown remains authoritative.  This module copies the validated
canonical graph into a stable in-memory contract for read-only consumers; it
never reads or writes a second graph artifact or any operational state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

from .graph import Graph, load_graph
from .lean import declaration_names, index_project
from .status import derive, is_definition

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

    def as_dict(self) -> dict[str, object]:
        return {
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
            "source_targets": list(self.source_targets),
            "statement_dependencies": list(self.statement_dependencies),
            "status": self.status.as_dict(),
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class RuntimeGraph:
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

    def get(self, node_id: str) -> RuntimeNode | None:
        """Return a node without exposing mutable lookup state."""

        return next((node for node in self.nodes if node.id == node_id), None)

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


def resolve_runtime_paths(project_or_blueprint: str | Path) -> RuntimePaths:
    """Resolve an Autoform project root or its ``blueprint`` directory.

    A directory that simultaneously looks like both forms is rejected rather
    than choosing an interpretation that could change project-relative paths.
    """

    supplied = Path(project_or_blueprint).expanduser()
    if supplied.is_symlink():
        raise RuntimeProjectionError(["project or blueprint path is a symbolic link"])
    candidate = supplied.resolve()
    if not candidate.is_dir():
        raise RuntimeProjectionError(["project or blueprint directory does not exist"])

    is_blueprint = (candidate / "roadmap").is_dir()
    is_project = (candidate / "blueprint" / "roadmap").is_dir()
    if is_blueprint and is_project:
        raise RuntimeProjectionError(["input is ambiguous between a project and blueprint directory"])
    if is_project:
        project_root = candidate
        blueprint_dir = (candidate / "blueprint").resolve()
    elif is_blueprint:
        blueprint_dir = candidate
        project_root = candidate.parent.resolve()
    else:
        raise RuntimeProjectionError(["roadmap directory does not exist"])

    try:
        blueprint_dir.relative_to(project_root)
    except ValueError as error:
        raise RuntimeProjectionError(["blueprint directory escapes the project root"]) from error
    _reject_roadmap_symlinks(blueprint_dir)
    return RuntimePaths(project_root=project_root, blueprint_dir=blueprint_dir)


def load_runtime_graph(
    project_or_blueprint: str | Path,
    *,
    lean_root: str | Path | None = None,
) -> RuntimeGraph:
    """Load the canonical graph once and return its immutable runtime view."""

    paths = resolve_runtime_paths(project_or_blueprint)
    graph = load_graph(paths.blueprint_dir)
    return build_runtime_graph(graph, project_root=paths.project_root, lean_root=lean_root)


def build_runtime_graph(
    graph: Graph,
    *,
    project_root: str | Path,
    lean_root: str | Path | None = None,
) -> RuntimeGraph:
    """Copy an already validated canonical graph into the runtime contract."""

    project = Path(project_root).expanduser().resolve()
    blueprint = graph.blueprint_dir.resolve()
    issues: list[str] = []
    try:
        blueprint_path = blueprint.relative_to(project).as_posix()
    except ValueError:
        raise RuntimeProjectionError(["blueprint directory escapes the project root"]) from None

    _reject_roadmap_symlinks(blueprint)
    node_ids = set(graph.nodes)
    article_paths: dict[str, str] = {}
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

        try:
            relative_blueprint = node.path.resolve().relative_to(blueprint)
            relative_project = node.path.resolve().relative_to(project)
        except ValueError:
            issues.append(f"{node.id}: article path escapes the project or blueprint")
            continue
        if not relative_blueprint.parts or relative_blueprint.parts[0] != "roadmap":
            issues.append(f"{node.id}: article is not under the canonical roadmap directory")
            continue
        if _article_id(relative_blueprint) != node.id:
            issues.append(f"{node.id}: article path does not match its path-derived id")
        if node.path.is_symlink():
            issues.append(f"{node.id}: article path is a symbolic link")
        try:
            content = node.path.read_bytes()
        except OSError:
            issues.append(f"{node.id}: article cannot be read")
            continue
        article_path = relative_project.as_posix()
        if article_path in article_paths.values():
            issues.append(f"{node.id}: article path is duplicated")
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
    lean_index = None
    if lean_root is not None:
        root = Path(lean_root).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeProjectionError(["Lean root does not exist or is not a directory"])
        lean_index = index_project(root)

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
    if not roadmap.is_dir():
        raise RuntimeProjectionError(["roadmap directory does not exist"])
    for path in (roadmap, *sorted(roadmap.rglob("*"))):
        if path.is_symlink():
            try:
                label = path.relative_to(blueprint).as_posix()
            except ValueError:
                label = "roadmap"
            raise RuntimeProjectionError([f"roadmap contains a symbolic link: {label}"])


def _ordered_union(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    values = list(first)
    values.extend(value for value in second if value not in values)
    return tuple(values)


def _article_id(relative_blueprint: Path) -> str:
    relative = relative_blueprint.relative_to("roadmap")
    if relative.name.casefold() == "readme.md":
        parent = relative.parent.as_posix()
        return parent if parent != "." else "roadmap"
    return relative.with_suffix("").as_posix()


def _source_target_is_confined(article: Path, target: str, blueprint: Path) -> bool:
    split = urlsplit(target)
    if split.scheme.casefold() in {"http", "https"}:
        return True
    if split.scheme or split.netloc:
        return False
    value = unquote(split.path)
    if not value:
        return True
    path = Path(value)
    windows = PureWindowsPath(value)
    if path.is_absolute() or windows.is_absolute():
        return False
    try:
        (article.parent / path).resolve().relative_to(blueprint)
    except (OSError, ValueError):
        return False
    return True


def _is_portable_relative_path(value: str) -> bool:
    path = Path(value)
    windows = PureWindowsPath(value)
    return (
        not path.is_absolute()
        and not windows.is_absolute()
        and ".." not in path.parts
        and ".." not in windows.parts
    )


def _validate_depths(graph: Graph, issues: list[str]) -> None:
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        seen: set[str] = set()
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
        path = article_paths[node_id].encode("utf-8")
        content = article_bytes[node_id]
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _validate_runtime(runtime: RuntimeGraph) -> None:
    issues: list[str] = []
    nodes = {node.id: node for node in runtime.nodes}
    if len(nodes) != len(runtime.nodes):
        issues.append("runtime node ids are not unique")
    for node in runtime.nodes:
        if node.parent is not None and node.parent not in nodes:
            issues.append(f"{node.id}: runtime parent does not resolve")
        if node.dependencies != _ordered_union(node.statement_dependencies, node.proof_dependencies):
            issues.append(f"{node.id}: runtime dependency union is inconsistent")
        if any(dependency not in nodes for dependency in node.dependencies):
            issues.append(f"{node.id}: runtime dependency does not resolve")
        has_children = any(other.parent == node.id for other in runtime.nodes)
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
    "build_runtime_graph",
    "load_runtime_graph",
    "resolve_runtime_paths",
]
