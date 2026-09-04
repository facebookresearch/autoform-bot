"""Build the immutable input contract for CLI-backed Autoform work."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from ._tree_snapshot import (
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
    capture_directory_descriptor,
)

from .coverage import (
    COVERAGE_V2_SCHEMA,
    CoverageSummary,
    _artifact_relative_path,
    _coverage_frontmatter,
    _roadmap_source_provenance,
    load_coverage,
)
from .graph import GraphValidationError, load_graph
from .lean import (
    BoundProjectSources,
    SourceIndex,
    open_project_sources,
)
from .runtime import (
    RuntimeGraph,
    RuntimeLeanTarget,
    RuntimePaths,
    RuntimeProjectionError,
    _source_target_relative,
    _source_target_walk,
    load_runtime_graph,
    resolve_runtime_paths,
)
from .workspace import _path_contains_symlink, _path_is_reparse_point
from .workspace_manifest import WorkspaceError

# V3 replaces V2's whole-manifest workspace digest with a selected-project
# binding. A controller must not reinterpret a V2 digest under the new meaning.
EXECUTION_INPUT_SCHEMA = "autoform-execution-input/v3"
_EXECUTION_INPUT_READ_ATTEMPTS = 3
_EXECUTION_CONTRACT_ROOTS = frozenset({"coverage", "roadmap"})


def _portable_path_key(path: PurePosixPath) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def _execution_contract_path(path: PurePosixPath) -> bool:
    if not path.parts:
        return False
    root = unicodedata.normalize("NFC", path.parts[0]).casefold()
    if root not in _EXECUTION_CONTRACT_ROOTS:
        return False
    return root != "roadmap" or not any(
        part.startswith(".") for part in path.parts[1:]
    )


_EXECUTION_CONTRACT_SELECTION = TreeSelection(
    include=lambda path, _mode: _execution_contract_path(path),
    descend=_execution_contract_path,
    record_omitted=False,
)


@dataclass(frozen=True, order=True, slots=True)
class ExecutionInputIssue:
    """One stable reason a ready-work snapshot could not be built."""

    code: str
    reason: str


class ExecutionInputError(ValueError):
    """The authored project cannot supply a safe ready-work snapshot."""

    def __init__(self, issues: tuple[ExecutionInputIssue, ...] | list[ExecutionInputIssue]) -> None:
        self.issues = tuple(sorted(set(issues)))
        super().__init__("; ".join(f"{issue.code}: {issue.reason}" for issue in self.issues))


@dataclass(frozen=True, slots=True)
class _ExecutionAuthorityRevision:
    """Digests needed to prove loader results came from this generation."""

    sha256: str
    runtime_source_revision: str
    roadmap_sha256: str
    coverage_sha256: str | None
    source_sha256s: tuple[tuple[str, str], ...]
    lean_source_revision: str | None


@dataclass(frozen=True, slots=True)
class ExecutionSourceUnit:
    """One source unit copied from the validated v2 coverage contract."""

    unit: str
    area: str
    start_line: int
    end_line: int
    locator: str
    unit_sha256: str
    disposition: str
    evidence: str
    roadmap_nodes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["roadmap_nodes"] = list(self.roadmap_nodes)
        return result


@dataclass(frozen=True, order=True, slots=True)
class ExecutionNodeBinding:
    """One validated reciprocal roadmap binding."""

    node_id: str
    unit: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExecutionInput:
    """A deterministic snapshot for ready-work discovery and validation."""

    schema: str
    runtime: RuntimeGraph
    runtime_sha256: str
    coverage_schema: str
    coverage_path: str
    coverage_sha256: str
    artifact_path: str
    artifact_sha256: str
    units: tuple[ExecutionSourceUnit, ...]
    node_bindings: tuple[ExecutionNodeBinding, ...]
    authority_sha256: str | None = None
    lean_source_revision: str | None = None
    workspace_project_id: str | None = None
    workspace_project_binding_sha256: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact": {
                "path": self.artifact_path,
                "sha256": self.artifact_sha256,
            },
            "authority_sha256": self.authority_sha256,
            "coverage": {
                "path": self.coverage_path,
                "schema": self.coverage_schema,
                "sha256": self.coverage_sha256,
            },
            "node_bindings": [binding.as_dict() for binding in self.node_bindings],
            "runtime": self.runtime.as_dict(),
            "runtime_sha256": self.runtime_sha256,
            "lean_source_revision": self.lean_source_revision,
            "schema": self.schema,
            "units": [unit.as_dict() for unit in self.units],
            "workspace": {
                "blueprint_path": self.runtime.blueprint_path,
                "project_binding_sha256": self.workspace_project_binding_sha256,
                "project_id": self.workspace_project_id,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @property
    def source_contract_sha256(self) -> str:
        """Hash the immutable source-coverage contract without progress state."""

        payload = {
            "artifact": {"path": self.artifact_path, "sha256": self.artifact_sha256},
            "coverage": {
                "path": self.coverage_path,
                "schema": self.coverage_schema,
                "sha256": self.coverage_sha256,
            },
            "node_bindings": [binding.as_dict() for binding in self.node_bindings],
            "units": [unit.as_dict() for unit in self.units],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def load_execution_input(
    project_or_blueprint: str | Path,
    *,
    lean_root: str | Path | None = None,
    project_id: str | None = None,
) -> ExecutionInput:
    """Read a stable runtime and exhaustive coverage snapshot, or fail closed."""

    if lean_root is None:
        resolved_lean_root = None
    else:
        requested_lean_root = Path(os.path.abspath(Path(lean_root).expanduser()))
        try:
            metadata = requested_lean_root.stat(follow_symlinks=False)
            if _path_contains_symlink(requested_lean_root) or _path_is_reparse_point(
                requested_lean_root,
                metadata,
            ):
                raise WorkspaceError(["Lean root path contains a symbolic link"])
            resolved_lean_root = requested_lean_root
        except (OSError, RuntimeError, ValueError, WorkspaceError):
            raise ExecutionInputError(
                [ExecutionInputIssue("lean-root-unsafe", "Lean root path cannot be resolved safely")]
            ) from None
        if not stat.S_ISDIR(metadata.st_mode):
            raise ExecutionInputError(
                [ExecutionInputIssue("lean-root-unsafe", "Lean root path is not a directory")]
            )
    runtime: RuntimeGraph | None = None
    coverage: CoverageSummary | None = None
    authority: _ExecutionAuthorityRevision | None = None
    workspace_project_id: str | None = None
    workspace_project_binding_sha256: str | None = None
    for _ in range(_EXECUTION_INPUT_READ_ATTEMPTS):
        paths = None
        final_paths = None
        lean_sources: BoundProjectSources | None = None
        try:
            try:
                paths = resolve_runtime_paths(
                    project_or_blueprint,
                    project_id=project_id,
                    _retain_workspace=True,
                )
            except (GraphValidationError, RuntimeProjectionError) as error:
                raise ExecutionInputError(
                    [ExecutionInputIssue("runtime-invalid", reason) for reason in error.issues]
                ) from error
            except OSError:
                continue
            paths.verify()
            if not paths.strongly_bound:
                raise ExecutionInputError(
                    [
                        ExecutionInputIssue(
                            "strong-binding-required",
                            "ready-work discovery requires descriptor-relative filesystem support",
                        )
                    ]
                )
            if paths.workspace_managed and paths.workspace_project_id is None:
                raise ExecutionInputError(
                    [
                        ExecutionInputIssue(
                            "workspace-project-required",
                            "ready-work discovery requires a registered workspace project",
                        )
                    ]
                )
            binding = _runtime_binding(paths)
            try:
                blueprint_snapshot = _capture_execution_authority(paths)
                paths.verify()
            except (OSError, TreeSnapshotError):
                continue
            try:
                if resolved_lean_root is not None:
                    lean_sources = open_project_sources(resolved_lean_root)
                    lean_snapshot = lean_sources.capture()
                else:
                    lean_snapshot = None
            except TreeSnapshotError:
                continue
            except OSError as error:
                raise ExecutionInputError(
                    [ExecutionInputIssue("lean-source-unsafe", str(error))]
                ) from error
            authority = _execution_authority_revision(
                blueprint_snapshot,
                lean_snapshot.revision if lean_snapshot is not None else None,
            )
            blueprint_generation = blueprint_snapshot.generation_revision
            lean_generation = (
                lean_snapshot.generation_revision if lean_snapshot is not None else None
            )
            unsafe_issues = _unsafe_snapshot_issues(blueprint_snapshot)
            if unsafe_issues:
                if _execution_generation_changed(
                    paths,
                    blueprint_generation,
                    lean_sources,
                    lean_generation,
                ):
                    continue
                raise ExecutionInputError(unsafe_issues)

            deferred_error: ExecutionInputError | None = None
            try:
                with tempfile.TemporaryDirectory(prefix="autoform-execution-") as temporary:
                    temporary_project = Path(temporary).resolve()
                    temporary_blueprint = temporary_project / "blueprint"
                    blueprint_snapshot.materialize(temporary_blueprint)
                    runtime = load_runtime_graph(
                        temporary_blueprint,
                        lean_root=None,
                    )
                    runtime = _rebase_runtime(
                        runtime,
                        paths.blueprint_dir.relative_to(paths.project_root).as_posix(),
                    )
                    runtime = _apply_lean_index(
                        runtime,
                        lean_snapshot.index if lean_snapshot is not None else None,
                    )
                    coverage = _require_v2_coverage(temporary_blueprint)
            except (GraphValidationError, RuntimeProjectionError) as error:
                deferred_error = ExecutionInputError(
                    [ExecutionInputIssue("runtime-invalid", reason) for reason in error.issues]
                )
            except ExecutionInputError as error:
                deferred_error = error
            except TreeSnapshotError:
                deferred_error = ExecutionInputError(
                    [
                        ExecutionInputIssue(
                            "execution-authority-unsafe",
                            "execution authority cannot be materialized safely",
                        )
                    ]
                )
            except OSError:
                deferred_error = ExecutionInputError(
                    [
                        ExecutionInputIssue(
                            "execution-snapshot-unavailable",
                            "private execution snapshot could not be materialized safely",
                        )
                    ]
                )

            del blueprint_snapshot
            if deferred_error is not None:
                if _execution_generation_changed(
                    paths,
                    blueprint_generation,
                    lean_sources,
                    lean_generation,
                ):
                    continue
                raise deferred_error
            assert runtime is not None and coverage is not None
            if (
                runtime.source_revision != authority.runtime_source_revision
                or not _runtime_matches_lean_index(
                    runtime,
                    lean_snapshot.index if lean_snapshot is not None else None,
                )
                or not _coverage_matches_authority(coverage, authority)
            ):
                continue
            try:
                final_paths = resolve_runtime_paths(
                    project_or_blueprint,
                    project_id=project_id,
                    _retain_workspace=True,
                )
            except (GraphValidationError, RuntimeProjectionError, OSError):
                continue
            final_paths.verify()
            paths.verify()
            final_binding = _runtime_binding(final_paths)
            expected_blueprint_path = paths.blueprint_dir.relative_to(paths.project_root).as_posix()
            if (
                binding == final_binding
                and runtime.blueprint_path == expected_blueprint_path
                and not _execution_generation_changed(
                    paths,
                    blueprint_generation,
                    lean_sources,
                    lean_generation,
                )
            ):
                workspace_project_id = paths.workspace_project_id
                workspace_project_binding_sha256 = paths.workspace_project_binding_sha256
                paths.verify()
                final_paths.verify()
                break
        except (RuntimeProjectionError, WorkspaceError):
            continue
        finally:
            if final_paths is not None:
                final_paths.close()
            if paths is not None:
                paths.close()
            if lean_sources is not None:
                lean_sources.close()
    else:
        raise _changed_execution_input()

    assert runtime is not None and coverage is not None and authority is not None
    missing_article_ids = tuple(node.id for node in runtime.nodes if node.article_id is None)
    if missing_article_ids:
        raise ExecutionInputError(
            [
                ExecutionInputIssue(
                    "article-id-required",
                    "ready-work discovery requires durable article_id frontmatter on every "
                    f"roadmap article; missing: {', '.join(missing_article_ids)}",
                )
            ]
        )
    runtime_json = runtime.to_json()
    return ExecutionInput(
        schema=EXECUTION_INPUT_SCHEMA,
        runtime=runtime,
        authority_sha256=authority.sha256,
        runtime_sha256=hashlib.sha256(runtime_json.encode("utf-8")).hexdigest(),
        lean_source_revision=authority.lean_source_revision,
        coverage_schema=coverage.schema,
        coverage_path=coverage.source_path,
        coverage_sha256=coverage.source_sha256,
        artifact_path=_required(coverage.artifact_path),
        artifact_sha256=_required(coverage.artifact_sha256),
        units=tuple(
            ExecutionSourceUnit(
                unit=unit.unit,
                area=unit.area,
                start_line=unit.start_line,
                end_line=unit.end_line,
                locator=unit.locator,
                unit_sha256=unit.unit_sha256,
                disposition=unit.disposition,
                evidence=unit.evidence,
                roadmap_nodes=unit.roadmap_nodes,
            )
            for unit in coverage.units
        ),
        node_bindings=tuple(
            ExecutionNodeBinding(binding.node_id, binding.unit)
            for binding in coverage.node_bindings
        ),
        workspace_project_id=workspace_project_id,
        workspace_project_binding_sha256=workspace_project_binding_sha256,
    )


def _changed_execution_input() -> ExecutionInputError:
    return ExecutionInputError(
        [
            ExecutionInputIssue(
                "execution-input-changed",
                "runtime or coverage authority kept changing while ready work was read",
            )
        ]
    )


def _execution_authority_revision(
    snapshot: TreeSnapshot,
    lean_source_revision: str | None,
) -> _ExecutionAuthorityRevision:
    """Identify every runtime and coverage input in one captured generation."""

    digest = hashlib.sha256(b"autoform-execution-authority/v2\0")
    runtime_digest = hashlib.sha256(b"autoform-runtime-source/v1\0")
    roadmap_sources: list[tuple[str, str]] = []
    roadmap_bytes: dict[str, bytes] = {}
    source_sha256s: list[tuple[str, str]] = []
    coverage_sha256: str | None = None
    for relative in snapshot.directories:
        if relative:
            _update_authority_digest(digest, b"directory", relative, b"")
    for relative, file_bytes in snapshot.files:
        _update_authority_digest(digest, b"file", relative, file_bytes)
        relative_path = PurePosixPath(relative)
        portable_path = _portable_path_key(relative_path)
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        if portable_path == ("coverage", "readme.md"):
            coverage_sha256 = file_sha256
        if portable_path[:1] == ("sources",):
            source_sha256s.append((relative, file_sha256))
        if portable_path[:1] == ("roadmap",) and relative_path.suffix == ".md":
            canonical = PurePosixPath("roadmap", *relative_path.parts[1:]).as_posix()
            roadmap_sources.append((canonical, file_sha256))
            roadmap_bytes[canonical] = file_bytes
    for relative, target in snapshot.symlinks:
        _update_authority_digest(
            digest,
            b"symlink",
            relative,
            target.encode("utf-8", errors="surrogateescape"),
        )
    for relative, mode in snapshot.special:
        _update_authority_digest(digest, b"special", relative, str(mode).encode("ascii"))
    for relative in snapshot.placeholders:
        _update_authority_digest(digest, b"placeholder", relative, b"")

    for relative, _sha256 in sorted(
        roadmap_sources,
        key=lambda entry: _authority_entry_sort_key(PurePosixPath(entry[0])),
    ):
        file_bytes = roadmap_bytes[relative]
        article_path = os.fsencode(relative)
        runtime_digest.update(len(article_path).to_bytes(8, "big"))
        runtime_digest.update(article_path)
        runtime_digest.update(len(file_bytes).to_bytes(8, "big"))
        runtime_digest.update(file_bytes)
    if lean_source_revision is not None:
        lean_revision = lean_source_revision.encode("ascii")
        _update_authority_digest(digest, b"lean", "", lean_revision)
    return _ExecutionAuthorityRevision(
        sha256=digest.hexdigest(),
        runtime_source_revision=runtime_digest.hexdigest(),
        roadmap_sha256=_roadmap_source_provenance(roadmap_sources),
        coverage_sha256=coverage_sha256,
        source_sha256s=tuple(sorted(source_sha256s)),
        lean_source_revision=lean_source_revision,
    )


def _authority_entry_sort_key(relative: PurePosixPath) -> tuple[int, str]:
    """Put roadmap files in the node order used by RuntimeGraph provenance."""

    if relative.parts[:1] != ("roadmap",) or relative.suffix != ".md":
        return 1, relative.as_posix()
    article = relative.relative_to("roadmap")
    if article.name == "README.md":
        node_id = article.parent.as_posix()
        if node_id == ".":
            node_id = "roadmap"
    else:
        node_id = article.with_suffix("").as_posix()
    return 0, node_id


def _capture_execution_authority(paths: RuntimePaths) -> TreeSnapshot:
    contract = _capture_execution_tree(paths, _EXECUTION_CONTRACT_SELECTION)
    _reject_portable_snapshot_collisions(contract)
    artifact = _coverage_artifact_from_snapshot(contract)
    source_targets = _local_source_targets_from_snapshot(contract)
    bound_artifact = (
        _bind_existing_target_path(paths, artifact) if artifact is not None else None
    )
    bound_source_paths = _bind_existing_source_target_paths(paths, source_targets)
    selection = _execution_authority_selection(bound_artifact, bound_source_paths)
    authority = _capture_execution_tree(paths, selection)
    if _execution_contract_projection(authority).generation_revision != (
        contract.generation_revision
    ):
        raise TreeSnapshotError("execution contract changed while its artifact was selected")
    return authority


def _capture_execution_tree(paths: RuntimePaths, selection: TreeSelection) -> TreeSnapshot:
    descriptor = paths.duplicate_blueprint_descriptor()
    try:
        return capture_directory_descriptor(
            descriptor,
            expected_identity=paths.blueprint_identity,
            selection=selection,
        )
    finally:
        os.close(descriptor)


def _coverage_artifact_from_snapshot(snapshot: TreeSnapshot) -> PurePosixPath | None:
    contracts = [
        data
        for relative, data in snapshot.files
        if _portable_path_key(PurePosixPath(relative)) == ("coverage", "readme.md")
    ]
    if len(contracts) > 1:
        raise TreeSnapshotError("coverage contract path is portably ambiguous")
    if not contracts:
        return None
    contract = contracts[0]
    try:
        text = contract.decode("utf-8")
    except UnicodeError:
        return None
    schemas, frontmatter, _end, issues = _coverage_frontmatter(text)
    if issues or len(schemas) != 1 or schemas[0][1] != COVERAGE_V2_SCHEMA:
        return None
    artifact = frontmatter.get("artifact")
    if artifact is None:
        return None
    relative, issue = _artifact_relative_path(artifact[1])
    return relative if issue is None else None


def _execution_authority_selection(
    artifact: PurePosixPath | None,
    source_targets: tuple[PurePosixPath, ...],
) -> TreeSelection:
    byte_paths = {artifact} if artifact is not None else set()
    validation_paths: set[PurePosixPath] = set()
    for path in (*byte_paths, *source_targets):
        validation_paths.add(path)
        validation_paths.update(parent for parent in path.parents if parent.parts)

    def contract_path(path: PurePosixPath) -> bool:
        return _execution_contract_path(path)

    def traversed(path: PurePosixPath) -> bool:
        return contract_path(path) or path in validation_paths

    return TreeSelection(
        include=lambda path, mode: contract_path(path)
        or path in byte_paths
        or (path in validation_paths and not stat.S_ISREG(mode)),
        descend=traversed,
        placeholder=lambda path, mode: path in validation_paths and stat.S_ISREG(mode),
        record_omitted=False,
    )


def _local_source_targets_from_snapshot(
    snapshot: TreeSnapshot,
) -> tuple[PurePosixPath, ...]:
    """Return local source paths named by the captured roadmap bytes."""

    if snapshot.unsupported_entries():
        return ()
    try:
        with tempfile.TemporaryDirectory(prefix="autoform-source-targets-") as temporary:
            blueprint = Path(temporary) / "blueprint"
            snapshot.materialize(blueprint)
            graph = load_graph(blueprint)
            targets: set[PurePosixPath] = set()
            for node in graph.nodes.values():
                for target in node.sources:
                    relative = _source_target_relative(
                        node.path,
                        target,
                        graph.blueprint_dir,
                    )
                    walk = _source_target_walk(
                        node.path,
                        target,
                        graph.blueprint_dir,
                    )
                    if isinstance(relative, PurePosixPath):
                        targets.add(relative)
                    if isinstance(walk, PurePosixPath):
                        targets.update(_source_walk_prefixes(walk))
    except (GraphValidationError, OSError, TreeSnapshotError):
        return ()
    return tuple(sorted(targets))


def _source_walk_prefixes(walk: PurePosixPath) -> tuple[PurePosixPath, ...]:
    """Retain every path reached before lexical parent components are applied."""

    parts: list[str] = []
    visited: list[PurePosixPath] = []
    for part in walk.parts:
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
        visited.append(PurePosixPath(*parts))
    return tuple(visited)


def _bind_existing_source_target_paths(
    paths: RuntimePaths,
    targets: tuple[PurePosixPath, ...],
) -> tuple[PurePosixPath, ...]:
    """Map local href components to names selected by the host filesystem."""

    trie: dict[str, dict] = {}
    for target in targets:
        branch = trie
        for part in target.parts:
            branch = branch.setdefault(part, {})
    selected: set[PurePosixPath] = set()
    root = paths.duplicate_blueprint_descriptor()

    def merge(target: dict[str, dict], source: dict[str, dict]) -> None:
        for name, children in source.items():
            merge(target.setdefault(name, {}), children)

    def visit(
        parent: int,
        requested_children: dict[str, dict],
        actual_parts: tuple[str, ...],
    ) -> None:
        names = tuple(sorted(os.listdir(parent)))
        resolved: dict[str, tuple[tuple[int, int, int], dict[str, dict]]] = {}
        for requested, descendants in sorted(requested_children.items()):
            try:
                metadata = os.stat(requested, dir_fd=parent, follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                continue
            signature = (metadata.st_dev, metadata.st_ino, metadata.st_mode)
            actual = _selected_directory_entry(parent, names, requested, signature)
            selected.add(PurePosixPath(*actual_parts, actual))
            existing = resolved.get(actual)
            if existing is None:
                resolved[actual] = (signature, dict(descendants))
            else:
                if existing[0] != signature:
                    raise TreeSnapshotError(
                        "source target path changed while it was selected"
                    )
                merge(existing[1], descendants)
        if tuple(sorted(os.listdir(parent))) != names:
            raise TreeSnapshotError("source target path changed while it was selected")
        for actual, (signature, descendants) in sorted(resolved.items()):
            current = os.stat(actual, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_mode) != signature:
                raise TreeSnapshotError("source target path changed while it was selected")
            if not descendants or not stat.S_ISDIR(current.st_mode):
                continue
            descriptor = os.open(
                actual,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino, opened.st_mode) != signature:
                    raise TreeSnapshotError(
                        "source target path changed while it was selected"
                    )
                visit(descriptor, descendants, (*actual_parts, actual))
            finally:
                os.close(descriptor)
            final = os.stat(actual, dir_fd=parent, follow_symlinks=False)
            if (final.st_dev, final.st_ino, final.st_mode) != signature:
                raise TreeSnapshotError("source target path changed while it was selected")

    try:
        visit(root, trie, ())
    except OSError as error:
        raise TreeSnapshotError(
            "source target path changed while it was selected"
        ) from error
    finally:
        os.close(root)
    return tuple(sorted(selected))


def _bind_existing_target_path(
    paths: RuntimePaths,
    target: PurePosixPath,
) -> PurePosixPath | None:
    selected = _bind_existing_source_target_paths(paths, (target,))
    matches = [path for path in selected if len(path.parts) == len(target.parts)]
    if len(matches) > 1:
        raise TreeSnapshotError("selected path has no unique filesystem spelling")
    return matches[0] if matches else None


def _selected_directory_entry(
    descriptor: int,
    names: tuple[str, ...],
    requested: str,
    signature: tuple[int, int, int],
) -> str:
    if requested in names:
        return requested
    folded = unicodedata.normalize("NFC", requested).casefold()
    matches: list[str] = []
    for name in names:
        if unicodedata.normalize("NFC", name).casefold() != folded:
            continue
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino, metadata.st_mode) == signature:
            matches.append(name)
    if len(matches) != 1:
        raise TreeSnapshotError("source target path has no stable directory entry")
    return matches[0]


def _execution_contract_projection(snapshot: TreeSnapshot) -> TreeSnapshot:
    def selected(relative: str) -> bool:
        return not relative or _execution_contract_path(PurePosixPath(relative))

    return TreeSnapshot(
        root_identity=snapshot.root_identity,
        directories=tuple(path for path in snapshot.directories if selected(path)),
        files=tuple(entry for entry in snapshot.files if selected(entry[0])),
        symlinks=tuple(entry for entry in snapshot.symlinks if selected(entry[0])),
        special=tuple(entry for entry in snapshot.special if selected(entry[0])),
        placeholders=tuple(path for path in snapshot.placeholders if selected(path)),
        omitted=tuple(entry for entry in snapshot.omitted if selected(entry[0])),
        identities=tuple(entry for entry in snapshot.identities if selected(entry[0])),
    )


def _reject_portable_snapshot_collisions(snapshot: TreeSnapshot) -> None:
    paths = [
        *snapshot.directories,
        *(relative for relative, _data in snapshot.files),
        *(relative for relative, _target in snapshot.symlinks),
        *(relative for relative, _mode in snapshot.special),
        *snapshot.placeholders,
    ]
    seen: dict[tuple[str, ...], str] = {}
    for relative in paths:
        if not relative:
            continue
        key = _portable_path_key(PurePosixPath(relative))
        previous = seen.setdefault(key, relative)
        if previous != relative:
            raise TreeSnapshotError("execution contract contains portably ambiguous paths")


def _execution_generation_changed(
    paths: RuntimePaths,
    expected_generation: str,
    lean_sources: BoundProjectSources | None,
    expected_lean_generation: str | None,
) -> bool:
    try:
        paths.verify()
        current = _capture_execution_authority(paths)
        paths.verify()
        if current.generation_revision != expected_generation:
            return True
        if lean_sources is None:
            return expected_lean_generation is not None
        if expected_lean_generation is None:
            return True
        lean_sources.verify()
        current_lean = lean_sources.capture()
        lean_sources.verify()
        return current_lean.generation_revision != expected_lean_generation
    except (OSError, RuntimeProjectionError, TreeSnapshotError, WorkspaceError):
        return True


def _unsafe_snapshot_issues(snapshot: TreeSnapshot) -> list[ExecutionInputIssue]:
    issues = [
        ExecutionInputIssue(
            "execution-authority-unsafe",
            f"execution authority contains a symbolic link: {relative}",
        )
        for relative, _target in snapshot.symlinks
    ]
    issues.extend(
        ExecutionInputIssue(
            "execution-authority-unsafe",
            f"execution authority contains a special file: {relative}",
        )
        for relative, _mode in snapshot.special
    )
    return issues


def _runtime_binding(paths: RuntimePaths) -> tuple[object, ...]:
    return (
        paths.project_root,
        paths.blueprint_dir,
        paths.workspace_project_id,
        paths.workspace_project_binding_sha256,
        paths.workspace_root_identity,
        paths.workspace_manifest_sha256,
        paths.blueprint_identity,
        paths.roadmap_identity,
    )


def _rebase_runtime(runtime: RuntimeGraph, blueprint_path: str) -> RuntimeGraph:
    source = PurePosixPath(runtime.blueprint_path)
    destination = PurePosixPath(blueprint_path)
    nodes = tuple(
        replace(
            node,
            article_path=(
                destination / PurePosixPath(node.article_path).relative_to(source)
            ).as_posix(),
        )
        for node in runtime.nodes
    )
    return replace(runtime, blueprint_path=destination.as_posix(), nodes=nodes)


def _apply_lean_index(runtime: RuntimeGraph, index: SourceIndex | None) -> RuntimeGraph:
    if index is None:
        return runtime
    nodes = tuple(
        replace(
            node,
            lean_targets=tuple(
                RuntimeLeanTarget(
                    target.declaration,
                    declaration.path.as_posix() if declaration is not None else None,
                )
                for target in node.lean_targets
                for declaration in (index.find(target.declaration),)
            ),
        )
        for node in runtime.nodes
    )
    return replace(runtime, nodes=nodes)


def _update_authority_digest(
    digest,
    kind: bytes,
    relative: str,
    data: bytes,
) -> None:
    path = os.fsencode(relative)
    for field in (kind, path, data):
        digest.update(len(field).to_bytes(8, "big"))
        digest.update(field)


def _coverage_matches_authority(
    coverage: CoverageSummary,
    authority: _ExecutionAuthorityRevision,
) -> bool:
    artifact_key = (
        _portable_path_key(PurePosixPath(coverage.artifact_path))
        if coverage.artifact_path is not None
        else None
    )
    artifact_matches = [
        sha256
        for relative, sha256 in authority.source_sha256s
        if artifact_key is not None
        and _portable_path_key(PurePosixPath(relative)) == artifact_key
    ]
    return (
        coverage.source_sha256 == authority.coverage_sha256
        and coverage._roadmap_sha256 == authority.roadmap_sha256
        and len(artifact_matches) == 1
        and coverage.artifact_sha256 == artifact_matches[0]
    )


def _runtime_matches_lean_index(runtime: RuntimeGraph, index: SourceIndex | None) -> bool:
    if index is None:
        return True
    for node in runtime.nodes:
        for target in node.lean_targets:
            declaration = index.find(target.declaration)
            expected = declaration.path.as_posix() if declaration is not None else None
            if target.source_file != expected:
                return False
    return True


def _require_v2_coverage(blueprint: Path) -> CoverageSummary:
    coverage, issues = load_coverage(blueprint)
    if issues:
        raise ExecutionInputError(
            [ExecutionInputIssue(issue.code, issue.reason) for issue in issues]
        )
    if coverage is None or coverage.schema != COVERAGE_V2_SCHEMA:
        raise ExecutionInputError(
            [
                ExecutionInputIssue(
                    "coverage-v2-required",
                    "ready-work discovery requires an exhaustive autoform-coverage/v2 contract",
                )
            ]
        )
    if not coverage.complete:
        mapped_count = coverage.counts["MAPPED"]
        subject = "unit remains" if mapped_count == 1 else "units remain"
        raise ExecutionInputError(
            [
                ExecutionInputIssue(
                    "coverage-incomplete",
                    "ready-work discovery requires a terminal coverage disposition for every "
                    f"v2 source unit; {mapped_count} {subject} MAPPED",
                )
            ]
        )
    return coverage


def _required(value: str | None) -> str:
    if value is None:  # Defensive: a valid v2 summary always carries both.
        raise ExecutionInputError(
            [ExecutionInputIssue("coverage-v2-invalid", "v2 coverage binding is incomplete")]
        )
    return value


__all__ = [
    "EXECUTION_INPUT_SCHEMA",
    "ExecutionInput",
    "ExecutionInputError",
    "ExecutionInputIssue",
    "ExecutionNodeBinding",
    "ExecutionSourceUnit",
    "load_execution_input",
]
