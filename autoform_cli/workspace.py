"""Resolve and inspect manifest-managed Autoform blueprint workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .workspace_manifest import (
    MAX_MANIFEST_BYTES,
    WORKSPACE_FILE,
    WORKSPACE_INSPECTION_SCHEMA,
    WorkspaceError,
    WorkspaceManifest,
    WorkspaceProject,
    parse_workspace,
    portable_name_key,
)


_DIRECTORY_BINDING_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_dir_fd", ())
)
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_DirectorySnapshot = tuple[str, tuple[tuple[int, int], ...]]
_PortableDirectoryChain = tuple[tuple[int, int], ...]


@dataclass(slots=True)
class _WorkspaceRootBinding:
    """A lexical absolute directory chain retained for a workspace lifetime."""

    path: Path
    descriptors: tuple[int, ...]
    identities: tuple[tuple[int, int], ...]
    _closed: bool = False

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise OSError("workspace root binding is closed")
        return self.descriptors[-1]

    @property
    def identity(self) -> tuple[int, int]:
        return self.identities[-1]

    def verify(self) -> None:
        """Verify every retained component name still names its bound inode."""

        if self._closed or len(self.descriptors) != len(self.identities):
            raise OSError("workspace root binding is incomplete")
        anchor = self.path.anchor
        if not anchor:
            raise OSError("workspace root is not absolute")
        opened = os.fstat(self.descriptors[0])
        named = os.stat(anchor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != self.identities[0]
            or (named.st_dev, named.st_ino) != self.identities[0]
        ):
            raise OSError("workspace root anchor changed")
        for index, part in enumerate(self.path.parts[1:], start=1):
            opened = os.fstat(self.descriptors[index])
            named = os.stat(
                part,
                dir_fd=self.descriptors[index - 1],
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or (opened.st_dev, opened.st_ino) != self.identities[index]
                or (named.st_dev, named.st_ino) != self.identities[index]
            ):
                raise OSError("workspace root component changed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in reversed(self.descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __del__(self) -> None:
        self.close()


@dataclass(slots=True)
class _WorkspaceManifestBinding:
    """The exact manifest file generation used to select workspace paths."""

    descriptor: int
    signature: tuple[int, int, int, int, int]
    sha256: str
    _closed: bool = False

    def verify(self, root_descriptor: int) -> None:
        if self._closed:
            raise OSError("workspace manifest binding is closed")
        before = os.fstat(self.descriptor)
        named = os.stat(WORKSPACE_FILE, dir_fd=root_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or _file_signature(before) != self.signature
            or _file_signature(named) != self.signature
        ):
            raise OSError("workspace manifest changed")
        content = _read_descriptor_at_offset(self.descriptor, before.st_size)
        after = os.fstat(self.descriptor)
        final_named = os.stat(WORKSPACE_FILE, dir_fd=root_descriptor, follow_symlinks=False)
        if (
            _file_signature(after) != self.signature
            or _file_signature(final_named) != self.signature
            or hashlib.sha256(content).hexdigest() != self.sha256
        ):
            raise OSError("workspace manifest changed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.descriptor)
        except OSError:
            pass

    def __del__(self) -> None:
        self.close()


@dataclass(slots=True)
class _WorkspaceRelativeBinding:
    """One retained directory chain below a bound workspace root."""

    relative: PurePosixPath
    descriptors: tuple[int, ...]
    identities: tuple[tuple[int, int], ...]
    _closed: bool = False

    def verify(self, root_descriptor: int) -> None:
        if self._closed or len(self.descriptors) != len(self.identities):
            raise OSError("workspace relative binding is incomplete")
        parent = root_descriptor
        for part, descriptor, identity in zip(
            self.relative.parts,
            self.descriptors,
            self.identities,
            strict=True,
        ):
            opened = os.fstat(descriptor)
            named = os.stat(part, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or (opened.st_dev, opened.st_ino) != identity
                or (named.st_dev, named.st_ino) != identity
            ):
                raise OSError("workspace managed directory changed")
            parent = descriptor

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in reversed(self.descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __del__(self) -> None:
        self.close()


def _open_workspace_root(path: Path) -> _WorkspaceRootBinding:
    """Bind every component of a lexical absolute root without following links."""

    if not _DIRECTORY_BINDING_SUPPORTED:
        raise WorkspaceError(
            ["this platform cannot inspect a workspace with the required path safety"]
        )
    absolute = Path(os.path.abspath(path))
    descriptors: list[int] = []
    identities: list[tuple[int, int]] = []
    try:
        anchor = absolute.anchor
        if not anchor:
            raise OSError("workspace root is not absolute")
        descriptor = os.open(anchor, _DIRECTORY_FLAGS)
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        named = os.stat(anchor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("workspace root anchor changed")
        identities.append((opened.st_dev, opened.st_ino))
        for part in absolute.parts[1:]:
            expected = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(expected.st_mode):
                raise OSError("workspace root component is not a directory")
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.append(child)
            opened = os.fstat(child)
            named = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or identity != (expected.st_dev, expected.st_ino)
                or identity != (named.st_dev, named.st_ino)
            ):
                raise OSError("workspace root component changed")
            identities.append(identity)
            descriptor = child
        binding = _WorkspaceRootBinding(absolute, tuple(descriptors), tuple(identities))
        binding.verify()
        return binding
    except OSError:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise WorkspaceError(
            ["workspace root path must not contain a symbolic link or change while opening"]
        ) from None


def _open_workspace_relative_directory(
    root_descriptor: int,
    relative: PurePosixPath,
    *,
    label: str,
) -> _WorkspaceRelativeBinding:
    """Open a relative directory chain without following any component links."""

    descriptors: list[int] = []
    identities: list[tuple[int, int]] = []
    parent = root_descriptor
    try:
        for part in relative.parts:
            expected = os.stat(part, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(expected.st_mode):
                raise OSError(f"{label} component is not a directory")
            descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
            descriptors.append(descriptor)
            opened = os.fstat(descriptor)
            named = os.stat(part, dir_fd=parent, follow_symlinks=False)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or identity != (expected.st_dev, expected.st_ino)
                or identity != (named.st_dev, named.st_ino)
            ):
                raise OSError(f"{label} component changed")
            identities.append(identity)
            parent = descriptor
        return _WorkspaceRelativeBinding(relative, tuple(descriptors), tuple(identities))
    except OSError:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise WorkspaceError([f"{label} changed or cannot be opened safely: {relative.as_posix()}"]) from None


def _portable_directory_chain(path: Path) -> _PortableDirectoryChain:
    """Capture a real-directory path when dirfd traversal is unavailable."""

    absolute = Path(os.path.abspath(path))
    identities: list[tuple[int, int]] = []
    anchor = absolute.anchor
    if not anchor:
        raise WorkspaceError(["workspace root is not absolute"])
    current = Path(anchor)
    for part in (anchor, *absolute.parts[1:]):
        if part != anchor:
            current /= part
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError:
            raise WorkspaceError(["workspace root path cannot be inspected safely"]) from None
        if _path_is_reparse_point(current, metadata):
            raise WorkspaceError(["workspace root path contains a symbolic link or reparse point"])
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceError(["workspace root path contains a non-directory component"])
        identities.append((metadata.st_dev, metadata.st_ino))
    return tuple(identities)


def _path_is_reparse_point(path: Path, metadata: os.stat_result) -> bool:
    """Recognize links and Windows directory junctions without following them."""

    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_reparse_tag", 0):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction is not None and is_junction(path))


def _snapshot_workspace_directories(
    workspace: Workspace,
) -> tuple[tuple[str, _DirectorySnapshot], ...]:
    """Capture existing managed directory generations from the loaded manifest."""

    paths: set[PurePosixPath] = set()
    for location in workspace.manifest.locations:
        paths.add(PurePosixPath(location.path))
    for project in workspace.manifest.projects:
        blueprint = workspace.manifest.blueprint_relative(project)
        paths.add(blueprint)
        paths.add(blueprint / "roadmap")
    snapshots: list[tuple[str, _DirectorySnapshot]] = []
    for relative in sorted(paths, key=lambda path: os.fsencode(path.as_posix())):
        snapshots.append(
            (relative.as_posix(), _snapshot_workspace_relative_directory(workspace, relative))
        )
    return tuple(snapshots)


def _snapshot_workspace_relative_directory(
    workspace: Workspace,
    relative: PurePosixPath,
) -> _DirectorySnapshot:
    if workspace._root_binding is None:
        identities: list[tuple[int, int]] = []
        current = workspace.root
        for part in relative.parts:
            current /= part
            try:
                metadata = current.stat(follow_symlinks=False)
            except FileNotFoundError:
                return "missing", tuple(identities)
            except OSError:
                raise WorkspaceError(
                    [f"managed directory cannot be inspected safely: {relative.as_posix()}"]
                ) from None
            identities.append((metadata.st_dev, metadata.st_ino))
            if _path_is_reparse_point(current, metadata):
                return "symlink", tuple(identities)
            if not stat.S_ISDIR(metadata.st_mode):
                return "nondirectory", tuple(identities)
        return "directory", tuple(identities)

    descriptors: list[int] = []
    identities: list[tuple[int, int]] = []
    parent = workspace.root_descriptor
    try:
        for part in relative.parts:
            try:
                metadata = os.stat(part, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return "missing", tuple(identities)
            except OSError:
                raise WorkspaceError(
                    [f"managed directory cannot be inspected safely: {relative.as_posix()}"]
                ) from None
            identity = (metadata.st_dev, metadata.st_ino)
            identities.append(identity)
            if stat.S_ISLNK(metadata.st_mode):
                return "symlink", tuple(identities)
            if not stat.S_ISDIR(metadata.st_mode):
                return "nondirectory", tuple(identities)
            try:
                descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
                descriptors.append(descriptor)
                opened = os.fstat(descriptor)
                named = os.stat(part, dir_fd=parent, follow_symlinks=False)
            except OSError:
                raise WorkspaceError(
                    [f"managed directory cannot be opened safely: {relative.as_posix()}"]
                ) from None
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != identity
                or (named.st_dev, named.st_ino) != identity
            ):
                raise WorkspaceError(
                    [f"managed directory changed during inspection: {relative.as_posix()}"]
                )
            parent = descriptor
        return "directory", tuple(identities)
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _workspace_read_checkpoint(_event: str, _binding: _WorkspaceRootBinding) -> None:
    """Deterministic root-substitution boundary used by adversarial tests."""


def _workspace_discovery_checkpoint(_event: str, _binding: _WorkspaceRootBinding) -> None:
    """Deterministic discovery boundary used by adversarial tests."""


def _workspace_inspection_checkpoint(_event: str, _workspace: Workspace) -> None:
    """Deterministic inspection boundary used by adversarial tests."""


def _portable_workspace_snapshot_checkpoint(_event: str, _workspace: Workspace) -> None:
    """Deterministic portable snapshot boundary used by adversarial tests."""


@dataclass(frozen=True, slots=True)
class Workspace:
    """A validated manifest anchored to its repository root."""

    root: Path
    manifest: WorkspaceManifest
    manifest_sha256: str
    _root_binding: _WorkspaceRootBinding | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _manifest_binding: _WorkspaceManifestBinding | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _portable_root_identities: _PortableDirectoryChain | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _managed_directory_snapshots: tuple[tuple[str, _DirectorySnapshot], ...] = field(
        default=(), repr=False, compare=False
    )
    _selected_bindings: list[_WorkspaceRelativeBinding] = field(
        default_factory=list, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self._root_binding is not None or self._portable_root_identities is not None:
            return
        normalized = Path(os.path.abspath(self.root))
        object.__setattr__(self, "root", normalized)
        if _DIRECTORY_BINDING_SUPPORTED:
            object.__setattr__(self, "_root_binding", _open_workspace_root(normalized))
        else:
            object.__setattr__(
                self,
                "_portable_root_identities",
                _portable_directory_chain(normalized),
            )

    @property
    def root_descriptor(self) -> int:
        if self._root_binding is None:
            raise WorkspaceError(
                ["workspace mutation requires descriptor-relative filesystem support"]
            )
        return self._root_binding.descriptor

    @property
    def root_identity(self) -> tuple[int, int]:
        if self._root_binding is not None:
            return self._root_binding.identity
        if self._portable_root_identities:
            return self._portable_root_identities[-1]
        raise WorkspaceError(["workspace root binding is unavailable"])

    def verify_root_binding(self) -> None:
        try:
            if self._root_binding is not None:
                self._root_binding.verify()
                if self._manifest_binding is None:
                    raise OSError("workspace manifest binding is unavailable")
                self._manifest_binding.verify(self.root_descriptor)
                for binding in self._selected_bindings:
                    binding.verify(self.root_descriptor)
                self._root_binding.verify()
            else:
                if _portable_directory_chain(self.root) != self._portable_root_identities:
                    raise OSError("workspace root changed")
                manifest = _read_workspace_manifest_portably(
                    self.path,
                    expected_parent_identity=self.root_identity,
                )
                if hashlib.sha256(manifest).hexdigest() != self.manifest_sha256:
                    raise OSError("workspace manifest changed")
                if _portable_directory_chain(self.root) != self._portable_root_identities:
                    raise OSError("workspace root changed")
        except (OSError, WorkspaceError):
            raise WorkspaceError(["workspace root changed during use"]) from None

    def close(self) -> None:
        """Release retained root descriptors when the workspace is no longer used."""

        for binding in self._selected_bindings:
            binding.close()
        self._selected_bindings.clear()
        if self._manifest_binding is not None:
            self._manifest_binding.close()
            object.__setattr__(self, "_manifest_binding", None)
        if self._root_binding is not None:
            self._root_binding.close()

    def __del__(self) -> None:
        self.close()

    def bind_managed_directory(self, relative: PurePosixPath) -> None:
        """Retain the exact loaded generation of one managed directory."""

        key = relative.as_posix()
        snapshots = dict(self._managed_directory_snapshots)
        expected = snapshots.get(key)
        if expected is None or expected[0] != "directory":
            raise WorkspaceError([f"managed directory changed during use: {key}"])
        if self._root_binding is None:
            if _snapshot_workspace_relative_directory(self, relative) != expected:
                raise WorkspaceError([f"managed directory changed during use: {key}"])
            self.verify_root_binding()
            return
        binding = _open_workspace_relative_directory(
            self.root_descriptor,
            relative,
            label="managed directory",
        )
        if binding.identities != expected[1]:
            binding.close()
            raise WorkspaceError([f"managed directory changed during use: {key}"])
        self._selected_bindings.append(binding)
        self.verify_root_binding()

    def verify_managed_directory_snapshots(self) -> None:
        """Verify every managed path still has its loaded directory generation."""

        self.verify_root_binding()
        for key, expected in self._managed_directory_snapshots:
            observed = _snapshot_workspace_relative_directory(self, PurePosixPath(key))
            if observed != expected:
                raise WorkspaceError([f"managed directory changed during use: {key}"])
        self.verify_root_binding()

    def duplicate_root_descriptor(self) -> int:
        """Return a checked duplicate suitable for one bounded mutation."""

        self.verify_root_binding()
        descriptor: int | None = None
        try:
            descriptor = os.dup(self.root_descriptor)
            opened = os.fstat(descriptor)
        except OSError:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise WorkspaceError(["workspace root changed during use"]) from None
        if (opened.st_dev, opened.st_ino) != self.root_identity:
            os.close(descriptor)
            raise WorkspaceError(["workspace root changed during use"])
        try:
            self.verify_root_binding()
        except WorkspaceError:
            os.close(descriptor)
            raise
        return descriptor

    @property
    def path(self) -> Path:
        return self.root / WORKSPACE_FILE

    def blueprint_path(self, project: WorkspaceProject) -> Path:
        return self.root / self.manifest.blueprint_relative(project)

    def project_binding_sha256(self, project: WorkspaceProject) -> str:
        """Digest only the selected project entry and its referenced location."""

        location = self.manifest.location(project.blueprint_location)
        payload = {
            "blueprint_path": self.manifest.blueprint_relative(project).as_posix(),
            "location": location.as_dict(),
            "project": project.as_dict(),
            "schema": "autoform-workspace-project-binding/v1",
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkspaceDiagnostic:
    severity: str
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceInspection:
    workspace: Workspace
    diagnostics: tuple[WorkspaceDiagnostic, ...]

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    def as_dict(self) -> dict[str, object]:
        return {
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "locations": [item.as_dict() for item in self.workspace.manifest.locations],
            "manifest": WORKSPACE_FILE,
            "ok": self.ok,
            "projects": [
                {
                    **item.as_dict(),
                    "resolved_blueprint_path": self.workspace.blueprint_path(item)
                    .relative_to(self.workspace.root)
                    .as_posix(),
                }
                for item in self.workspace.manifest.projects
            ],
            "root": ".",
            "schema": WORKSPACE_INSPECTION_SCHEMA,
            "workspace_schema": self.workspace.manifest.schema,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def discover_workspace(start: str | Path = ".") -> Workspace:
    """Find and load the nearest enclosing ``.autoform.toml``."""

    try:
        candidate = Path(start).expanduser().absolute()
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError(["workspace search path cannot be resolved"]) from None
    if _path_contains_symlink(candidate):
        raise WorkspaceError(["workspace search path contains a symbolic link"])
    if candidate.is_file():
        candidate = candidate.parent
    if not _DIRECTORY_BINDING_SUPPORTED:
        return _discover_workspace_portably(candidate)
    search_binding = _open_workspace_root(candidate)
    search_transferred = False
    try:
        for root in (candidate, *candidate.parents):
            search_binding.verify()
            binding = _open_workspace_root(root)
            root_transferred = False
            try:
                binding.verify()
                _reject_case_collisions(root, PurePosixPath(WORKSPACE_FILE))
                binding.verify()
                search_binding.verify()
                try:
                    metadata = os.stat(
                        WORKSPACE_FILE,
                        dir_fd=binding.descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    binding.verify()
                    search_binding.verify()
                    continue
                except OSError:
                    raise WorkspaceError([f"cannot inspect {WORKSPACE_FILE} safely"]) from None
                if stat.S_ISLNK(metadata.st_mode):
                    raise WorkspaceError([f"{WORKSPACE_FILE} must not be a symbolic link"])
                if not stat.S_ISREG(metadata.st_mode):
                    raise WorkspaceError([f"{WORKSPACE_FILE} must be a regular file"])
                _workspace_discovery_checkpoint("manifest-found", binding)
                binding.verify()
                search_binding.verify()
                root_transferred = True
                search_transferred = True
                return _load_workspace_binding(
                    root,
                    binding,
                    discovery_binding=search_binding,
                )
            finally:
                if not root_transferred:
                    binding.close()
    except OSError:
        raise WorkspaceError(["workspace root changed during discovery"]) from None
    finally:
        if not search_transferred:
            search_binding.close()
    raise WorkspaceError([f"no enclosing {WORKSPACE_FILE} was found"])


def _discover_workspace_portably(candidate: Path) -> Workspace:
    """Read-only workspace discovery for platforms without safe dirfd APIs."""

    search_identities = _portable_directory_chain(candidate)
    for root in (candidate, *candidate.parents):
        if _portable_directory_chain(candidate) != search_identities:
            raise WorkspaceError(["workspace root changed during discovery"])
        root_identities = _portable_directory_chain(root)
        _reject_case_collisions(root, PurePosixPath(WORKSPACE_FILE))
        manifest_path = root / WORKSPACE_FILE
        try:
            metadata = manifest_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            raise WorkspaceError([f"cannot inspect {WORKSPACE_FILE} safely"]) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceError([f"{WORKSPACE_FILE} must not be a symbolic link"])
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceError([f"{WORKSPACE_FILE} must be a regular file"])
        workspace = _load_workspace_portably(root, root_identities)
        if _portable_directory_chain(candidate) != search_identities:
            workspace.close()
            raise WorkspaceError(["workspace root changed during discovery"])
        return workspace
    raise WorkspaceError([f"no enclosing {WORKSPACE_FILE} was found"])


def load_workspace(root: str | Path) -> Workspace:
    """Load a workspace rooted at *root* without searching its parents."""

    try:
        requested = Path(os.path.abspath(Path(root).expanduser()))
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError(["workspace root path cannot be resolved"]) from None
    if not _DIRECTORY_BINDING_SUPPORTED:
        return _load_workspace_portably(requested, _portable_directory_chain(requested))
    binding = _open_workspace_root(requested)
    return _load_workspace_binding(requested, binding)


def _load_workspace_portably(
    requested: Path,
    root_identities: _PortableDirectoryChain,
) -> Workspace:
    """Load a stable read-only manifest without descriptor-relative APIs."""

    _reject_case_collisions(requested, PurePosixPath(WORKSPACE_FILE))
    content = _read_workspace_manifest_portably(
        requested / WORKSPACE_FILE,
        expected_parent_identity=root_identities[-1],
    )
    if len(content) > MAX_MANIFEST_BYTES:
        raise WorkspaceError([f"{WORKSPACE_FILE} exceeds the {MAX_MANIFEST_BYTES}-byte limit"])
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkspaceError([f"{WORKSPACE_FILE} is not valid UTF-8 TOML"]) from None
    manifest = parse_workspace(text)
    if _portable_directory_chain(requested) != root_identities:
        raise WorkspaceError(["workspace root changed during use"])
    workspace = Workspace(
        requested,
        manifest,
        hashlib.sha256(content).hexdigest(),
        _portable_root_identities=root_identities,
    )
    try:
        _validate_workspace_paths(workspace)
        snapshots = _snapshot_workspace_directories(workspace)
        object.__setattr__(workspace, "_managed_directory_snapshots", snapshots)
        _validate_workspace_paths(workspace)
        workspace.verify_root_binding()
        _portable_workspace_snapshot_checkpoint("before-final-snapshot", workspace)
        final_content = _read_workspace_manifest_portably(
            requested / WORKSPACE_FILE,
            expected_parent_identity=root_identities[-1],
        )
        final_snapshots = _snapshot_workspace_directories(workspace)
        if final_content != content or final_snapshots != snapshots:
            raise WorkspaceError(["workspace changed during portable inspection"])
        _validate_workspace_paths(workspace)
        workspace.verify_root_binding()
        return workspace
    except BaseException:
        workspace.close()
        raise


def _load_workspace_binding(
    requested: Path,
    binding: _WorkspaceRootBinding,
    *,
    discovery_binding: _WorkspaceRootBinding | None = None,
) -> Workspace:
    """Load a workspace through an already retained root generation."""

    manifest_binding: _WorkspaceManifestBinding | None = None
    workspace: Workspace | None = None
    try:
        binding.verify()
        _reject_case_collisions(requested, PurePosixPath(WORKSPACE_FILE))
        binding.verify()
        manifest_path = requested / WORKSPACE_FILE
        content, manifest_binding = _read_workspace_manifest(binding, manifest_path)
        if len(content) > MAX_MANIFEST_BYTES:
            raise WorkspaceError(
                [f"{WORKSPACE_FILE} exceeds the {MAX_MANIFEST_BYTES}-byte limit"]
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError([f"{WORKSPACE_FILE} is not valid UTF-8 TOML"]) from None
        manifest = parse_workspace(text)
        workspace = Workspace(
            requested,
            manifest,
            hashlib.sha256(content).hexdigest(),
            binding,
            manifest_binding,
        )
        workspace.verify_root_binding()
        _validate_workspace_paths(workspace)
        object.__setattr__(
            workspace,
            "_managed_directory_snapshots",
            _snapshot_workspace_directories(workspace),
        )
        _validate_workspace_paths(workspace)
        workspace.verify_root_binding()
        if discovery_binding is not None:
            discovery_binding.verify()
            discovery_binding.close()
        return workspace
    except OSError:
        if workspace is not None:
            workspace.close()
        else:
            if manifest_binding is not None:
                manifest_binding.close()
            binding.close()
        if discovery_binding is not None and discovery_binding is not binding:
            discovery_binding.close()
        raise WorkspaceError(["workspace root changed during use"]) from None
    except BaseException:
        if workspace is not None:
            workspace.close()
        else:
            if manifest_binding is not None:
                manifest_binding.close()
            binding.close()
        if discovery_binding is not None and discovery_binding is not binding:
            discovery_binding.close()
        raise


def _read_workspace_manifest(
    root_binding: _WorkspaceRootBinding,
    manifest_path: Path,
) -> tuple[bytes, _WorkspaceManifestBinding]:
    """Read one exact regular manifest generation through a bound root dirfd."""

    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    retained = False
    try:
        root_binding.verify()
        _workspace_read_checkpoint("before-manifest-open", root_binding)
        root_binding.verify()
        root_descriptor = root_binding.descriptor
        descriptor = os.open(WORKSPACE_FILE, file_flags, dir_fd=root_descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("workspace manifest is not regular")
        chunks: list[bytes] = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        named = os.stat(WORKSPACE_FILE, dir_fd=root_descriptor, follow_symlinks=False)
        if _file_signature(before) != _file_signature(after) or _file_signature(
            after
        ) != _file_signature(named):
            raise OSError("workspace manifest changed")
        _workspace_read_checkpoint("after-manifest-read", root_binding)
        root_binding.verify()
        content = b"".join(chunks)
        manifest_binding = _WorkspaceManifestBinding(
            descriptor,
            _file_signature(after),
            hashlib.sha256(content).hexdigest(),
        )
        manifest_binding.verify(root_descriptor)
        retained = True
        return content, manifest_binding
    except OSError:
        raise WorkspaceError([f"cannot read {manifest_path.name} safely"]) from None
    finally:
        if descriptor is not None and not retained:
            os.close(descriptor)


def _read_workspace_manifest_portably(
    manifest_path: Path,
    *,
    expected_parent_identity: tuple[int, int],
) -> bytes:
    """Read one stable manifest generation without descriptor-relative APIs."""

    try:
        parent = manifest_path.parent.stat(follow_symlinks=False)
        before = manifest_path.stat(follow_symlinks=False)
        if (parent.st_dev, parent.st_ino) != expected_parent_identity:
            raise OSError("workspace root changed")
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise OSError("workspace manifest is not a regular file")
        with manifest_path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            content = stream.read(MAX_MANIFEST_BYTES + 1)
            after = os.fstat(stream.fileno())
        named = manifest_path.stat(follow_symlinks=False)
        final_parent = manifest_path.parent.stat(follow_symlinks=False)
        if not (
            _file_signature(before)
            == _file_signature(opened)
            == _file_signature(after)
            == _file_signature(named)
        ):
            raise OSError("workspace manifest changed")
        if (final_parent.st_dev, final_parent.st_ino) != expected_parent_identity:
            raise OSError("workspace root changed")
        return content
    except OSError:
        raise WorkspaceError([f"cannot read {manifest_path.name} safely"]) from None


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_descriptor_at_offset(descriptor: int, size: int) -> bytes:
    """Read a retained regular file without changing its shared offset."""

    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    if offset != size:
        raise OSError("workspace manifest changed")
    return b"".join(chunks)


def inspect_workspace(start: str | Path = ".") -> WorkspaceInspection:
    """Inspect registered paths without treating unregistered directories as managed."""

    workspace = discover_workspace(start)
    try:
        _workspace_inspection_checkpoint("before-path-inspection", workspace)
        workspace.verify_root_binding()
        diagnostics: list[WorkspaceDiagnostic] = []
        for location in workspace.manifest.locations:
            path = workspace.root / PurePosixPath(location.path)
            relative = path.relative_to(workspace.root).as_posix()
            if not path.exists():
                diagnostics.append(
                    WorkspaceDiagnostic(
                        "warning", "location-missing", "Declared location does not exist.", relative
                    )
                )
            elif not path.is_dir():
                diagnostics.append(
                    WorkspaceDiagnostic(
                        "error",
                        "location-not-directory",
                        "Declared location is not a directory.",
                        relative,
                    )
                )
        for project in workspace.manifest.projects:
            path = workspace.blueprint_path(project)
            relative = path.relative_to(workspace.root).as_posix()
            if not path.exists():
                diagnostics.append(
                    WorkspaceDiagnostic(
                        "error", "blueprint-missing", "Registered blueprint does not exist.", relative
                    )
                )
            elif not path.is_dir():
                diagnostics.append(
                    WorkspaceDiagnostic(
                        "error",
                        "blueprint-not-directory",
                        "Registered blueprint is not a directory.",
                        relative,
                    )
                )
            elif not (path / "roadmap").is_dir():
                diagnostics.append(
                    WorkspaceDiagnostic(
                        "error",
                        "roadmap-missing",
                        "Registered blueprint has no roadmap directory.",
                        relative,
                    )
                )
        workspace.verify_managed_directory_snapshots()
        return WorkspaceInspection(workspace, tuple(diagnostics))
    except BaseException:
        workspace.close()
        raise


def resolve_blueprint(
    start: str | Path = ".",
    *,
    project_id: str | None = None,
) -> tuple[Workspace, WorkspaceProject, Path]:
    """Resolve one registered blueprint from a path and optional project id."""

    workspace = discover_workspace(start)
    try:
        if project_id is not None:
            project = workspace.manifest.project(project_id)
            path = workspace.blueprint_path(project)
            relative = workspace.manifest.blueprint_relative(project)
            workspace.bind_managed_directory(relative)
            workspace.bind_managed_directory(relative / "roadmap")
            _require_blueprint(path)
            workspace.verify_root_binding()
            return workspace, project, path

        try:
            candidate = Path(start).expanduser().absolute()
        except (OSError, RuntimeError, ValueError):
            raise WorkspaceError(["workspace project path cannot be resolved"]) from None
        if candidate.is_file():
            candidate = candidate.parent
        matches = [
            project
            for project in workspace.manifest.projects
            if _is_within(candidate, workspace.blueprint_path(project))
        ]
        if len(matches) == 1:
            project = matches[0]
        elif (
            not matches
            and len(workspace.manifest.projects) == 1
            and _same_path(candidate, workspace.root)
        ):
            project = workspace.manifest.projects[0]
        else:
            choices = ", ".join(project.id for project in workspace.manifest.projects) or "none"
            raise WorkspaceError([f"cannot choose an Autoform project; pass --project from: {choices}"])
        path = workspace.blueprint_path(project)
        relative = workspace.manifest.blueprint_relative(project)
        workspace.bind_managed_directory(relative)
        workspace.bind_managed_directory(relative / "roadmap")
        _require_blueprint(path)
        workspace.verify_root_binding()
        return workspace, project, path
    except BaseException:
        workspace.close()
        raise


def _validate_workspace_paths(workspace: Workspace) -> None:
    issues: list[str] = []
    try:
        _reject_case_collisions(workspace.root, PurePosixPath(WORKSPACE_FILE))
    except WorkspaceError as error:
        issues.extend(error.issues)
    for location in workspace.manifest.locations:
        path = workspace.root / PurePosixPath(location.path)
        try:
            _reject_case_collisions(workspace.root, PurePosixPath(location.path))
            _reject_existing_symlink_chain(path, workspace.root)
        except WorkspaceError as error:
            issues.extend(f"locations.{location.id}: {item}" for item in error.issues)
    for project in workspace.manifest.projects:
        path = workspace.blueprint_path(project)
        try:
            relative = workspace.manifest.blueprint_relative(project)
            _reject_case_collisions(workspace.root, relative)
            _reject_existing_symlink_chain(path, workspace.root)
        except WorkspaceError as error:
            issues.extend(f"projects.{project.id}: {item}" for item in error.issues)
    if issues:
        raise WorkspaceError(issues)


def _reject_existing_symlink_chain(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise WorkspaceError(["managed path escapes the workspace root"]) from None
    try:
        probe = root
        for part in relative.parts:
            probe /= part
            try:
                metadata = probe.stat(follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                return
            if _path_is_reparse_point(probe, metadata):
                raise WorkspaceError(
                    [
                        "managed path contains a symbolic link or reparse point: "
                        f"{relative.as_posix()}"
                    ]
                )
    except OSError:
        raise WorkspaceError([f"managed path cannot be inspected: {relative.as_posix()}"]) from None


def _reject_case_collisions(root: Path, relative: PurePosixPath) -> None:
    parent = root
    for part in relative.parts:
        try:
            metadata = parent.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise WorkspaceError([f"cannot inspect managed path parent: {parent.name or '.'}"]) from None
        if _path_is_reparse_point(parent, metadata):
            raise WorkspaceError(
                [f"managed path contains a symbolic link or reparse point: {relative.as_posix()}"]
            )
        if not stat.S_ISDIR(metadata.st_mode):
            return
        for sibling in _directory_entries(parent, "managed path parent"):
            if portable_name_key(sibling.name) == portable_name_key(part) and sibling.name != part:
                raise WorkspaceError(
                    [f"managed path is not portable beside existing path: {sibling.name}"]
                )
        parent /= part


def _path_contains_symlink(path: Path) -> bool:
    try:
        probe = Path(path.anchor)
        for part in path.parts[1:]:
            probe /= part
            try:
                metadata = probe.stat(follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                return False
            if _path_is_reparse_point(probe, metadata):
                return True
        return False
    except OSError:
        raise WorkspaceError(["workspace path cannot be inspected safely"]) from None


def _require_blueprint(path: Path) -> None:
    if not path.is_dir():
        raise WorkspaceError(["registered blueprint directory does not exist"])
    if not (path / "roadmap").is_dir():
        raise WorkspaceError(["registered blueprint has no roadmap directory"])


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _same_path(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return False


def _directory_entries(path: Path, label: str) -> tuple[Path, ...]:
    try:
        return tuple(path.iterdir())
    except OSError:
        raise WorkspaceError([f"cannot inspect {label}: {path.name or '.'}"]) from None


__all__ = [
    "Workspace",
    "WorkspaceDiagnostic",
    "WorkspaceInspection",
    "discover_workspace",
    "inspect_workspace",
    "load_workspace",
    "resolve_blueprint",
]
