"""Capture one immutable directory-tree generation for internal consumers."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

from . import _directory_binding as directory_binding
from ._directory_binding import (
    DirectoryChangedError,
    RetainedDirectory,
    lexical_absolute_path,
    open_directory,
)

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
    | getattr(os, "O_BINARY", 0)
)
_WRITE_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)
_DESCRIPTOR_MATERIALIZATION_SUPPORTED = all(
    operation in getattr(os, "supports_dir_fd", ())
    for operation in (os.mkdir, os.open, os.rmdir, os.stat, os.unlink)
)
_READ_CHUNK_BYTES = 1024 * 1024
_DESCRIPTOR_CAPTURE_SUPPORTED = (
    os.listdir in getattr(os, "supports_fd", ())
    and os.readlink in getattr(os, "supports_dir_fd", ())
)
_DESCRIPTOR_SCANDIR_SUPPORTED = os.scandir in getattr(os, "supports_fd", ())
_WINDOWS_STAT_VIEWS = os.name == "nt"
_WINDOWS_DEVICE_NAMES = frozenset(
    {
        "AUX",
        "CON",
        "CONIN$",
        "CONOUT$",
        "NUL",
        "PRN",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
    }
)


class TreeSnapshotError(ValueError):
    """A directory tree could not be captured as one stable generation."""


class TreeChangedError(TreeSnapshotError):
    """The directory tree changed while it was bound or captured; a retry may succeed."""


class TreeCaptureLimitError(TreeSnapshotError):
    """A named resource limit stopped a directory-tree capture."""

    def __init__(self, limit: str, maximum: int) -> None:
        self.limit = limit
        self.maximum = maximum
        super().__init__(f"directory tree exceeds {limit}={maximum}")


def _materialization_parts(relative: str, *, allow_root: bool = False) -> tuple[str, ...]:
    if not isinstance(relative, str):
        raise TreeSnapshotError("materialization paths must be strings")
    if allow_root and relative == "":
        return ()
    path = PurePosixPath(relative)
    if (
        not relative
        or relative in {".", ".."}
        or path.is_absolute()
        or path.as_posix() != relative
        or any(not _portable_materialization_component(part) for part in path.parts)
    ):
        raise TreeSnapshotError(f"unsafe materialization path: {relative!r}")
    return path.parts


def _portable_materialization_component(part: str) -> bool:
    if (
        part in {"", ".", ".."}
        or part.endswith((".", " "))
        or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in part)
    ):
        return False
    device_stem = part.split(".", 1)[0].rstrip(" ").upper()
    return device_stem not in _WINDOWS_DEVICE_NAMES


def _validate_materialization_layout(
    directories: tuple[tuple[str, tuple[str, ...]], ...],
    files: tuple[tuple[str, tuple[str, ...]], ...],
    placeholders: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    spellings: dict[tuple[str, ...], tuple[str, ...]] = {}
    kinds: dict[tuple[str, ...], str] = {}
    for kind, records in (
        ("directory", directories),
        ("file", files),
        ("placeholder", placeholders),
    ):
        for relative, parts in records:
            for length in range(1, len(parts) + 1):
                prefix = parts[:length]
                key = tuple(_normalized_name(part) for part in prefix)
                prior = spellings.setdefault(key, prefix)
                if prior != prefix:
                    raise TreeSnapshotError(
                        f"materialization paths collide: {relative!r}"
                    )
            key = tuple(_normalized_name(part) for part in parts)
            if key in kinds:
                raise TreeSnapshotError(f"duplicate materialization path: {relative!r}")
            kinds[key] = kind
    for key in kinds:
        for length in range(len(key)):
            if kinds.get(key[:length]) != "directory":
                raise TreeSnapshotError("materialization path has a non-directory parent")


def _materialize_regular_files(
    destination: Path,
    directories: tuple[tuple[str, tuple[str, ...]], ...],
    files: tuple[tuple[str, tuple[str, ...], bytes], ...],
    placeholders: tuple[tuple[str, tuple[str, ...]], ...],
    *,
    verify_bytes: bool,
) -> None:
    """Create a snapshot using only retained directory descriptors."""

    if not (
        directory_binding.DIRECTORY_BINDING_SUPPORTED
        and _DESCRIPTOR_MATERIALIZATION_SUPPORTED
    ):
        raise TreeSnapshotError(
            "safe descriptor-relative materialization is unavailable on this platform"
        )
    absolute = lexical_absolute_path(destination)
    name = absolute.name
    if not _portable_materialization_component(name):
        raise TreeSnapshotError("unsafe materialization destination")

    parent: RetainedDirectory | None = None
    descriptors: dict[tuple[str, ...], int] = {}
    directory_identities: dict[tuple[str, ...], tuple[int, int, int]] = {}
    directory_permissions: dict[tuple[str, ...], int] = {}
    created_directories: list[tuple[str, ...]] = []
    created_files: list[tuple[tuple[str, ...], tuple[int, ...]]] = []
    root_created = False
    succeeded = False
    try:
        parent = open_directory(absolute.parent)
        os.mkdir(name, 0o700, dir_fd=parent.descriptor)
        root_created = True
        root_metadata = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
        root_identity = _directory_entry_identity(root_metadata)
        directory_identities[()] = root_identity
        directory_permissions[()] = stat.S_IMODE(root_metadata.st_mode)
        root_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent.descriptor)
        descriptors[()] = root_descriptor
        if _directory_entry_identity(os.fstat(root_descriptor)) != root_identity:
            raise TreeSnapshotError("materialization directory changed while it was opened")
        _verify_named_directory(parent.descriptor, name, root_identity)
        _tree_snapshot_checkpoint("after-materialization-directory-open", "")

        for relative, parts in sorted(directories, key=lambda item: len(item[1])):
            if not relative:
                continue
            parent_parts = parts[:-1]
            parent_descriptor = descriptors.get(parent_parts)
            if parent_descriptor is None:
                raise TreeSnapshotError("materialization path has a missing parent")
            _verify_materialization_parent(
                parent_parts,
                descriptors,
                directory_identities,
            )
            os.mkdir(parts[-1], 0o700, dir_fd=parent_descriptor)
            child_metadata = os.stat(
                parts[-1],
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            child_identity = _directory_entry_identity(child_metadata)
            directory_identities[parts] = child_identity
            directory_permissions[parts] = stat.S_IMODE(child_metadata.st_mode)
            created_directories.append(parts)
            child_descriptor = os.open(
                parts[-1],
                _DIRECTORY_FLAGS,
                dir_fd=parent_descriptor,
            )
            descriptors[parts] = child_descriptor
            if _directory_entry_identity(os.fstat(child_descriptor)) != child_identity:
                raise TreeSnapshotError(
                    "materialization directory changed while it was opened"
                )
            _verify_named_directory(parent_descriptor, parts[-1], child_identity)
            _tree_snapshot_checkpoint(
                "after-materialization-directory-open",
                "/".join(parts),
            )

        for _relative, parts, data in files:
            created_files.append(
                (
                    parts,
                    _materialize_file(
                        descriptors,
                        directory_identities,
                        parts,
                        data,
                    ),
                )
            )
        for _relative, parts in placeholders:
            created_files.append(
                (
                    parts,
                    _materialize_file(
                        descriptors,
                        directory_identities,
                        parts,
                        b"",
                    ),
                )
            )

        _tree_snapshot_checkpoint("before-materialization-final-verification", "")
        if verify_bytes:
            _verify_materialized_snapshot(
                descriptors[()],
                root_identity,
                directories,
                files,
                placeholders,
                directory_identities,
                directory_permissions,
                created_files,
            )
        else:
            _verify_materialized_metadata(
                descriptors,
                directories,
                files,
                placeholders,
                directory_identities,
                created_files,
            )
        parent.verify()
        _verify_named_directory(parent.descriptor, name, root_identity)
        succeeded = True
    except TreeSnapshotError:
        raise
    except (OSError, ValueError) as error:
        raise TreeSnapshotError("snapshot could not be materialized safely") from error
    finally:
        if not succeeded:
            _cleanup_materialization(
                parent,
                name,
                descriptors,
                directory_identities,
                created_directories,
                created_files,
                root_created,
            )
        for descriptor in reversed(tuple(descriptors.values())):
            _close_descriptor(descriptor)
        if parent is not None:
            parent.close()


def _directory_entry_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    if not stat.S_ISDIR(metadata.st_mode):
        raise TreeSnapshotError("materialization parent is not a directory")
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _file_entry_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    if not stat.S_ISREG(metadata.st_mode):
        raise TreeSnapshotError("materialized entry is not a regular file")
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _verify_named_directory(
    parent_descriptor: int,
    name: str,
    expected: tuple[int, int, int],
) -> None:
    observed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if _directory_entry_identity(observed) != expected:
        raise TreeSnapshotError("materialization directory changed while it was used")


def _verify_materialization_parent(
    parts: tuple[str, ...],
    descriptors: dict[tuple[str, ...], int],
    identities: dict[tuple[str, ...], tuple[int, int, int]],
) -> None:
    descriptor = descriptors.get(parts)
    expected = identities.get(parts)
    if descriptor is None or expected is None:
        raise TreeSnapshotError("materialization path has a missing parent")
    if _directory_entry_identity(os.fstat(descriptor)) != expected:
        raise TreeSnapshotError("materialization parent changed while it was used")
    if parts:
        parent_descriptor = descriptors.get(parts[:-1])
        if parent_descriptor is None:
            raise TreeSnapshotError("materialization path has a missing parent")
        _verify_named_directory(parent_descriptor, parts[-1], expected)


def _materialize_file(
    descriptors: dict[tuple[str, ...], int],
    identities: dict[tuple[str, ...], tuple[int, int, int]],
    parts: tuple[str, ...],
    data: bytes,
) -> tuple[int, ...]:
    parent_parts = parts[:-1]
    _tree_snapshot_checkpoint("before-materialization-file-open", "/".join(parts))
    _verify_materialization_parent(parent_parts, descriptors, identities)
    parent_descriptor = descriptors[parent_parts]
    descriptor: int | None = None
    identity: tuple[int, int, int] | None = None
    completed = False
    try:
        descriptor = os.open(
            parts[-1],
            _WRITE_FILE_FLAGS,
            0o600,
            dir_fd=parent_descriptor,
        )
        identity = _file_entry_identity(os.fstat(descriptor))
        view = memoryview(data)
        written = 0
        while written < len(view):
            try:
                count = os.write(descriptor, view[written:])
            except InterruptedError:
                continue
            if count <= 0:
                raise OSError(errno.EIO, "short materialization write")
            written += count
        final_metadata = os.fstat(descriptor)
        named_metadata = os.stat(
            parts[-1],
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _file_entry_identity(final_metadata) != identity
            or _stat_signature(named_metadata) != _stat_signature(final_metadata)
        ):
            raise TreeSnapshotError("materialized file changed while it was written")
        _verify_materialization_parent(parent_parts, descriptors, identities)
        completed = True
        return _stat_signature(final_metadata)
    finally:
        if descriptor is not None:
            _close_descriptor(descriptor)
        if not completed and identity is not None:
            try:
                named = _file_entry_identity(
                    os.stat(parts[-1], dir_fd=parent_descriptor, follow_symlinks=False)
                )
                if named == identity:
                    os.unlink(parts[-1], dir_fd=parent_descriptor)
            except (OSError, TreeSnapshotError, ValueError):
                pass


def _verify_materialized_snapshot(
    root_descriptor: int,
    root_identity: tuple[int, int, int],
    directories: tuple[tuple[str, tuple[str, ...]], ...],
    files: tuple[tuple[str, tuple[str, ...], bytes], ...],
    placeholders: tuple[tuple[str, tuple[str, ...]], ...],
    directory_identities: dict[tuple[str, ...], tuple[int, int, int]],
    directory_permissions: dict[tuple[str, ...], int],
    created_files: list[tuple[tuple[str, ...], tuple[int, ...]]],
) -> None:
    """Verify exact names, kinds and bytes at the successful commit boundary."""

    expected_directories = tuple(sorted(relative for relative, _parts in directories))
    expected_files = tuple(
        sorted(
            [
                *((relative, data) for relative, _parts, data in files),
                *((relative, b"") for relative, _parts in placeholders),
            ]
        )
    )
    all_parts = [
        *(parts for _relative, parts in directories),
        *(parts for _relative, parts, _data in files),
        *(parts for _relative, parts in placeholders),
    ]
    limits = TreeCaptureLimits(
        max_entries=max(0, len(expected_directories) - 1) + len(expected_files),
        max_depth=max((len(parts) for parts in all_parts), default=0),
        max_file_bytes=max((len(data) for _relative, data in expected_files), default=0),
        max_total_bytes=sum(len(data) for _relative, data in expected_files),
    )
    observed = capture_directory_descriptor(
        root_descriptor,
        expected_identity=root_identity[:2],
        selection=TreeSelection(
            include=lambda _path, _mode: True,
            descend=lambda _path: True,
            limits=limits,
        ),
    )
    if (
        observed.directories != expected_directories
        or observed.files != expected_files
        or observed.symlinks
        or observed.special
        or observed.placeholders
        or observed.omitted
        or observed.opaque_directories
    ):
        raise TreeSnapshotError("materialized tree changed before commit")
    observed_identities = dict(observed.identities)
    for relative, parts in directories:
        signature = observed_identities.get(relative)
        if signature is None or (
            _stable_entry_identity(signature) != directory_identities.get(parts)
            or stat.S_IMODE(signature[2]) != directory_permissions.get(parts)
        ):
            raise TreeSnapshotError("materialized directory metadata changed before commit")
    for parts, expected_signature in created_files:
        signature = observed_identities.get("/".join(parts))
        if signature != expected_signature:
            raise TreeSnapshotError("materialized file metadata changed before commit")


def _verify_materialized_metadata(
    descriptors: dict[tuple[str, ...], int],
    directories: tuple[tuple[str, tuple[str, ...]], ...],
    files: tuple[tuple[str, tuple[str, ...], bytes], ...],
    placeholders: tuple[tuple[str, tuple[str, ...]], ...],
    directory_identities: dict[tuple[str, ...], tuple[int, int, int]],
    created_files: list[tuple[tuple[str, ...], tuple[int, ...]]],
) -> None:
    """Verify exact names, kinds and metadata without rereading captured bytes."""

    expected_names: dict[tuple[str, ...], set[str]] = {
        parts: set() for _relative, parts in directories
    }
    for _relative, parts in directories:
        if parts:
            expected_names.setdefault(parts[:-1], set()).add(parts[-1])
    for _relative, parts, _data in files:
        expected_names.setdefault(parts[:-1], set()).add(parts[-1])
    for _relative, parts in placeholders:
        expected_names.setdefault(parts[:-1], set()).add(parts[-1])

    for parts, names in expected_names.items():
        descriptor = descriptors.get(parts)
        expected_identity = directory_identities.get(parts)
        if descriptor is None or expected_identity is None:
            raise TreeSnapshotError("materialized tree has a missing directory")
        if _directory_entry_identity(os.fstat(descriptor)) != expected_identity:
            raise TreeSnapshotError("materialized directory metadata changed before commit")
        if tuple(sorted(os.listdir(descriptor))) != tuple(sorted(names)):
            raise TreeSnapshotError("materialized tree changed before commit")
        if parts:
            parent_descriptor = descriptors.get(parts[:-1])
            if parent_descriptor is None:
                raise TreeSnapshotError("materialized tree has a missing directory")
            _verify_named_directory(parent_descriptor, parts[-1], expected_identity)

    for parts, expected_signature in created_files:
        parent_descriptor = descriptors.get(parts[:-1])
        if parent_descriptor is None:
            raise TreeSnapshotError("materialized file has a missing parent")
        observed = os.stat(
            parts[-1],
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _stat_signature(observed) != expected_signature:
            raise TreeSnapshotError("materialized file metadata changed before commit")


def _cleanup_materialization(
    parent: RetainedDirectory | None,
    root_name: str,
    descriptors: dict[tuple[str, ...], int],
    identities: dict[tuple[str, ...], tuple[int, int, int]],
    directories: list[tuple[str, ...]],
    files: list[tuple[tuple[str, ...], tuple[int, ...]]],
    root_created: bool,
) -> None:
    """Best-effort cleanup that never follows or removes a substituted entry."""

    for parts, expected in reversed(files):
        parent_descriptor = descriptors.get(parts[:-1])
        if parent_descriptor is None:
            continue
        try:
            observed = os.stat(
                parts[-1],
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISREG(observed.st_mode) and _stat_signature(observed) == expected:
                os.unlink(parts[-1], dir_fd=parent_descriptor)
        except (OSError, TreeSnapshotError, ValueError):
            pass
    for parts in sorted(directories, key=len, reverse=True):
        descriptor = descriptors.pop(parts, None)
        if descriptor is not None:
            _close_descriptor(descriptor)
        parent_descriptor = descriptors.get(parts[:-1])
        expected = identities.get(parts)
        if parent_descriptor is None or expected is None:
            continue
        try:
            _verify_named_directory(parent_descriptor, parts[-1], expected)
            os.rmdir(parts[-1], dir_fd=parent_descriptor)
        except (OSError, TreeSnapshotError, ValueError):
            pass
    root_descriptor = descriptors.pop((), None)
    if root_descriptor is not None:
        _close_descriptor(root_descriptor)
    if parent is not None and root_created:
        expected = identities.get(())
        try:
            if expected is not None:
                _verify_named_directory(parent.descriptor, root_name, expected)
                os.rmdir(root_name, dir_fd=parent.descriptor)
        except (OSError, TreeSnapshotError, ValueError):
            pass


@dataclass(frozen=True, slots=True)
class TreeCaptureLimits:
    """Optional bounds on observed entries and captured regular-file bytes.

    The retained root has depth zero. Every observed child counts as an entry,
    including omitted and placeholder entries.
    """

    max_entries: int | None = None
    max_depth: int | None = None
    max_file_bytes: int | None = None
    max_total_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "max_entries",
            "max_depth",
            "max_file_bytes",
            "max_total_bytes",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class TreeSelection:
    """Select which paths are descended into and captured as bytes."""

    include: Callable[[PurePosixPath, int], bool]
    descend: Callable[[PurePosixPath], bool]
    placeholder: Callable[[PurePosixPath, int], bool] = lambda _path, _mode: False
    byte_limit: Callable[[PurePosixPath], int | None] = lambda _path: None
    record_omitted: bool = True
    limits: TreeCaptureLimits = TreeCaptureLimits()
    opaque_markers: tuple["OpaqueDirectoryMarker", ...] = ()


@dataclass(frozen=True, slots=True)
class OpaqueDirectoryMarker:
    """A bounded marker that makes its containing directory opaque.

    The capture engine reads this small, immutable surface before it visits any
    descendants.  A recognized marker is then verified by identity, without
    replaying ``recognizes`` or depending on churn elsewhere in that output
    directory.  A presence-only marker accepts any entry kind without reading
    it; this is for directory sentinels such as a nested checkout's ``.git``.
    """

    name: str
    max_bytes: int
    recognizes: Callable[[bytes], bool]
    presence_only: bool = False

    def __post_init__(self) -> None:
        if not _valid_name(self.name):
            raise ValueError("opaque marker name must be one portable path component")
        if not isinstance(self.presence_only, bool):
            raise ValueError("opaque marker presence_only must be a boolean")
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or self.max_bytes < 0
        ):
            raise ValueError("opaque marker max_bytes must be a non-negative integer")


ALL_ENTRIES = TreeSelection(
    include=lambda _path, _mode: True,
    descend=lambda _path: True,
)


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    """Immutable names, types, and bytes captured below one directory root."""

    root_identity: tuple[int, int]
    directories: tuple[str, ...]
    files: tuple[tuple[str, bytes], ...]
    symlinks: tuple[tuple[str, str], ...]
    special: tuple[tuple[str, int], ...]
    placeholders: tuple[str, ...]
    omitted: tuple[tuple[str, str], ...]
    identities: tuple[tuple[str, tuple[int, ...]], ...]
    opaque_directories: tuple[str, ...] = ()

    @property
    def revision(self) -> str:
        """Return a framed digest of every captured entry and regular-file byte."""

        digest = hashlib.sha256(b"autoform-directory-snapshot/v1\0")
        for relative in self.directories:
            _update_digest(digest, b"directory", relative, b"")
        for relative, data in self.files:
            _update_digest(digest, b"file", relative, data)
        for relative, target in self.symlinks:
            _update_digest(digest, b"symlink", relative, os.fsencode(target))
        for relative, mode in self.special:
            _update_digest(digest, b"special", relative, str(mode).encode("ascii"))
        for relative in self.placeholders:
            _update_digest(digest, b"placeholder", relative, b"")
        for relative, kind in self.omitted:
            _update_digest(digest, b"omitted", relative, kind.encode("ascii"))
        for relative in self.opaque_directories:
            _update_digest(digest, b"opaque-directory", relative, b"")
        return digest.hexdigest()

    @property
    def generation_revision(self) -> str:
        """Return a digest that also distinguishes filesystem generations."""

        digest = hashlib.sha256(self.revision.encode("ascii"))
        directory_paths = set(self.directories)
        for relative, identity in self.identities:
            stable_identity = (
                _stable_entry_identity(identity)
                if relative in directory_paths
                else identity
            )
            encoded = ",".join(str(field) for field in stable_identity).encode("ascii")
            _update_digest(digest, b"identity", relative, encoded)
        return digest.hexdigest()

    def materialize(self, destination: Path, *, verify_bytes: bool = True) -> None:
        """Write captured regular files below a fresh private directory."""

        issues = self.unsupported_entries()
        if issues:
            relative, reason = issues[0]
            raise TreeSnapshotError(f"{relative}: {reason}")
        self.materialize_regular_files(destination, verify_bytes=verify_bytes)

    def materialize_regular_files(
        self,
        destination: Path,
        *,
        verify_bytes: bool = True,
    ) -> None:
        """Materialize safe content after a caller has recorded invalid entries."""

        directory_parts = tuple(
            (relative, _materialization_parts(relative, allow_root=True))
            for relative in self.directories
        )
        file_parts = tuple(
            (relative, _materialization_parts(relative)) for relative, _data in self.files
        )
        placeholder_parts = tuple(
            (relative, _materialization_parts(relative))
            for relative in self.placeholders
        )
        _validate_materialization_layout(directory_parts, file_parts, placeholder_parts)
        _materialize_regular_files(
            destination,
            directory_parts,
            tuple(
                (relative, parts, data)
                for (relative, data), (_validated, parts) in zip(self.files, file_parts)
            ),
            placeholder_parts,
            verify_bytes=verify_bytes,
        )

    def unsupported_entries(self) -> tuple[tuple[str, str], ...]:
        """Return path-specific reasons for entries that cannot be copied safely."""

        issues = [
            (relative, "symbolic links are not supported")
            for relative, _target in self.symlinks
        ]
        issues.extend(
            (
                relative,
                f"{_special_file_kind(mode)} is not a regular file or directory",
            )
            for relative, mode in self.special
        )
        return tuple(sorted(issues))


@dataclass(frozen=True, slots=True)
class _DirectoryRecord:
    relative: str
    identity: tuple[int, ...]
    names: tuple[str, ...]
    marker: "_OpaqueMarkerRecord | None" = None


@dataclass(frozen=True, slots=True)
class _OpaqueMarkerRecord:
    name: str
    identity: tuple[int, ...]
    data: bytes | None
    max_bytes: int


@dataclass(frozen=True, slots=True)
class _EntryRecord:
    relative: str
    identity: tuple[int, ...]
    ignored: bool = False


@dataclass(slots=True)
class _CaptureBudget:
    limits: TreeCaptureLimits
    entries: int = 0
    total_bytes: int = 0

    def add_entries(self, count: int, *, depth: int) -> None:
        if (
            count
            and self.limits.max_depth is not None
            and depth > self.limits.max_depth
        ):
            raise TreeCaptureLimitError("max_depth", self.limits.max_depth)
        self.entries += count
        if (
            self.limits.max_entries is not None
            and self.entries > self.limits.max_entries
        ):
            raise TreeCaptureLimitError("max_entries", self.limits.max_entries)

    def file_read_limit(self, size: int, selected_limit: int | None) -> int | None:
        if selected_limit is not None and (
            isinstance(selected_limit, bool)
            or not isinstance(selected_limit, int)
            or selected_limit < 0
        ):
            raise TreeSnapshotError(
                "tree selection byte limit must be a non-negative integer or None"
            )
        selected_bytes = (
            size if selected_limit is None else min(size, selected_limit + 1)
        )
        if (
            self.limits.max_file_bytes is not None
            and selected_bytes > self.limits.max_file_bytes
        ):
            raise TreeCaptureLimitError(
                "max_file_bytes",
                self.limits.max_file_bytes,
            )
        if (
            self.limits.max_total_bytes is not None
            and self.total_bytes + selected_bytes > self.limits.max_total_bytes
        ):
            raise TreeCaptureLimitError(
                "max_total_bytes",
                self.limits.max_total_bytes,
            )
        limits = [selected_limit] if selected_limit is not None else []
        if self.limits.max_file_bytes is not None:
            limits.append(self.limits.max_file_bytes)
        if self.limits.max_total_bytes is not None:
            limits.append(self.limits.max_total_bytes - self.total_bytes)
        return min(limits) if limits else None

    def add_file_bytes(self, count: int) -> None:
        if (
            self.limits.max_file_bytes is not None
            and count > self.limits.max_file_bytes
        ):
            raise TreeCaptureLimitError(
                "max_file_bytes",
                self.limits.max_file_bytes,
            )
        self.total_bytes += count
        if (
            self.limits.max_total_bytes is not None
            and self.total_bytes > self.limits.max_total_bytes
        ):
            raise TreeCaptureLimitError(
                "max_total_bytes",
                self.limits.max_total_bytes,
            )


def _requires_bounded_enumeration(limits: TreeCaptureLimits) -> bool:
    return limits.max_entries is not None or limits.max_depth is not None


def _capture_directory_names(
    descriptor: int,
    *,
    budget: _CaptureBudget,
    depth: int,
) -> tuple[str, ...]:
    if _DESCRIPTOR_SCANDIR_SUPPORTED:
        names: list[str] = []
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                if not _valid_name(entry.name):
                    raise TreeSnapshotError("directory tree contains an unsupported entry name")
                budget.add_entries(1, depth=depth)
                names.append(entry.name)
        return tuple(sorted(names))
    if _requires_bounded_enumeration(budget.limits):
        raise TreeSnapshotError(
            "bounded directory enumeration is unavailable on this platform"
        )
    names = tuple(sorted(os.listdir(descriptor)))
    if any(not _valid_name(name) for name in names):
        raise TreeSnapshotError("directory tree contains an unsupported entry name")
    budget.add_entries(len(names), depth=depth)
    return names


def _capture_path_names(
    path: Path,
    *,
    budget: _CaptureBudget,
    depth: int,
) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(path) as iterator:
        for entry in iterator:
            if not _valid_name(entry.name):
                raise TreeSnapshotError("directory tree contains an unsupported entry name")
            budget.add_entries(1, depth=depth)
            names.append(entry.name)
    return tuple(sorted(names))


def _directory_names_match(
    descriptor: int,
    expected: tuple[str, ...],
    *,
    limits: TreeCaptureLimits,
) -> bool:
    if _DESCRIPTOR_SCANDIR_SUPPORTED:
        names: list[str] = []
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                if len(names) == len(expected):
                    return False
                names.append(entry.name)
        return tuple(sorted(names)) == expected
    if _requires_bounded_enumeration(limits):
        raise TreeSnapshotError(
            "bounded directory enumeration is unavailable on this platform"
        )
    return tuple(sorted(os.listdir(descriptor))) == expected


class BoundDirectoryTree:
    """One retained directory generation that can be recaptured and compared."""

    def __init__(
        self,
        root: Path,
        *,
        expected_identity: tuple[int, int] | None = None,
        expected_children: dict[str, tuple[int, int]] | None = None,
        selection: TreeSelection = ALL_ENTRIES,
        require_descriptor: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self.root = lexical_absolute_path(root)
        self.expected_identity = expected_identity
        self.expected_children = dict(expected_children or {})
        self.selection = selection
        self._binding: RetainedDirectory | None = None
        self._portable_identity: tuple[int, int] | None = None
        self._portable_path_identities: tuple[tuple[int, int], ...] | None = None
        self._verification_limits: TreeCaptureLimits | None = None
        self._closed = False
        if (
            directory_binding.DIRECTORY_BINDING_SUPPORTED
            and _DESCRIPTOR_CAPTURE_SUPPORTED
        ):
            try:
                binding = open_directory(self.root)
            except DirectoryChangedError as error:
                raise TreeChangedError("directory tree changed before it was captured") from error
            except OSError as error:
                raise TreeSnapshotError(f"directory tree cannot be inspected safely: {error}") from error
            if expected_identity is not None and binding.identity != expected_identity:
                binding.close()
                raise TreeChangedError("directory tree changed before it was captured")
            self._binding = binding
            try:
                self._verify_expected_children(
                    binding.descriptor,
                    limits=self.selection.limits,
                )
            except BaseException:
                binding.close()
                self._binding = None
                raise
            return
        if require_descriptor:
            raise TreeSnapshotError(
                "safe directory traversal is unavailable on this platform"
            )
        try:
            path_identities = _portable_directory_identities(self.root)
            metadata = os.lstat(self.root)
        except OSError as error:
            raise TreeSnapshotError("directory tree cannot be inspected safely") from error
        identity = (metadata.st_dev, metadata.st_ino)
        if not stat.S_ISDIR(metadata.st_mode) or (
            expected_identity is not None and identity != expected_identity
        ):
            raise TreeChangedError("directory tree changed before it was captured")
        self._portable_identity = identity
        self._portable_path_identities = path_identities
        self._verify_expected_children(None, limits=self.selection.limits)

    @property
    def identity(self) -> tuple[int, int]:
        with self._lock:
            if self._closed:
                raise TreeSnapshotError("directory tree binding is closed")
            if self._binding is not None:
                return self._binding.identity
            assert self._portable_identity is not None
            return self._portable_identity

    @property
    def descriptor(self) -> int:
        """Return the retained root descriptor or fail at the capability boundary."""

        with self._lock:
            if self._closed:
                raise TreeSnapshotError("directory tree binding is closed")
            if self._binding is None:
                raise TreeSnapshotError(
                    "safe directory traversal is unavailable on this platform"
                )
            return self._binding.descriptor

    def _verify_expected_children(
        self,
        descriptor: int | None,
        *,
        limits: TreeCaptureLimits,
    ) -> None:
        if not self.expected_children:
            return
        try:
            names = (
                _capture_directory_names(
                    descriptor,
                    budget=_CaptureBudget(limits),
                    depth=1,
                )
                if descriptor is not None
                else _capture_path_names(
                    self.root,
                    budget=_CaptureBudget(limits),
                    depth=1,
                )
            )
            folded_names: set[str] = set()
            for name, expected in self.expected_children.items():
                if not _valid_name(name):
                    raise TreeSnapshotError("invalid bound child name")
                folded = _normalized_name(name)
                if folded in folded_names:
                    raise TreeSnapshotError("invalid bound child name")
                folded_names.add(folded)
                matches = [actual for actual in names if _normalized_name(actual) == folded]
                if len(matches) != 1:
                    raise TreeChangedError("directory tree changed before it was captured")
                actual = matches[0]
                metadata = (
                    os.stat(actual, dir_fd=descriptor, follow_symlinks=False)
                    if descriptor is not None
                    else os.lstat(self.root / actual)
                )
                if not stat.S_ISDIR(metadata.st_mode) or (
                    metadata.st_dev,
                    metadata.st_ino,
                ) != expected:
                    raise TreeChangedError("directory tree changed before it was captured")
            final_names = (
                _capture_directory_names(
                    descriptor,
                    budget=_CaptureBudget(limits),
                    depth=1,
                )
                if descriptor is not None
                else _capture_path_names(
                    self.root,
                    budget=_CaptureBudget(limits),
                    depth=1,
                )
            )
            if final_names != names:
                raise TreeChangedError("directory tree changed before it was captured")
        except (OSError, _TreeChanged) as error:
            raise TreeChangedError("directory tree changed before it was captured") from error

    def verify(self) -> None:
        """Verify the retained generation is still selected by its public path."""

        with self._lock:
            self._verify(self._verification_limits or self.selection.limits)

    def _verify(self, limits: TreeCaptureLimits) -> None:
        """Verify the retained generation under the active capture bounds."""

        if self._closed:
            raise TreeSnapshotError("directory tree binding is closed")
        try:
            if self._binding is not None:
                self._binding.verify()
                self._verify_expected_children(
                    self._binding.descriptor,
                    limits=limits,
                )
                return
            if (
                self._portable_path_identities is None
                or _portable_directory_identities(self.root)
                != self._portable_path_identities
            ):
                raise TreeChangedError("directory tree changed while it was in use")
            metadata = os.lstat(self.root)
            if not stat.S_ISDIR(metadata.st_mode) or (
                metadata.st_dev,
                metadata.st_ino,
            ) != self.identity:
                raise TreeChangedError("directory tree changed while it was in use")
            self._verify_expected_children(None, limits=limits)
        except OSError as error:
            raise TreeChangedError("directory tree changed while it was in use") from error

    def capture(self, *, selection: TreeSelection | None = None) -> TreeSnapshot:
        """Capture one stable tree through the retained directory generation."""

        with self._lock:
            return self._capture_locked(selection)

    def _capture_locked(self, selection: TreeSelection | None) -> TreeSnapshot:
        active_selection = self.selection if selection is None else selection
        previous_limits = self._verification_limits
        self._verification_limits = active_selection.limits
        try:
            self.verify()
            try:
                if self._binding is not None:
                    snapshot = capture_directory_descriptor(
                        self._binding.descriptor,
                        expected_identity=self.identity,
                        expected_children=self.expected_children,
                        selection=active_selection,
                    )
                else:
                    first = _capture_portable(
                        self.root,
                        expected_identity=self.identity,
                        expected_children=self.expected_children,
                        selection=active_selection,
                    )
                    _tree_snapshot_checkpoint("between-portable-captures", "")
                    _tree_snapshot_checkpoint("before-final-verification", "")
                    snapshot = _capture_portable(
                        self.root,
                        expected_identity=self.identity,
                        expected_children=self.expected_children,
                        selection=active_selection,
                    )
                    if first != snapshot:
                        raise TreeChangedError(
                            "directory tree changed while it was captured"
                        )
            except (_PermissionDenied, OSError, RuntimeError) as error:
                raise _capture_failure(error) from error
            self.verify()
            return snapshot
        finally:
            self._verification_limits = previous_limits

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._binding is not None:
                self._binding.close()
                self._binding = None
            self._closed = True


@contextmanager
def bind_directory_tree(
    root: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    expected_children: dict[str, tuple[int, int]] | None = None,
    selection: TreeSelection = ALL_ENTRIES,
    require_descriptor: bool = False,
) -> Iterator[BoundDirectoryTree]:
    """Retain *root* while callers capture and verify its content generation."""

    bound = BoundDirectoryTree(
        root,
        expected_identity=expected_identity,
        expected_children=expected_children,
        selection=selection,
        require_descriptor=require_descriptor,
    )
    try:
        yield bound
    finally:
        bound.close()


def capture_directory_descriptor(
    descriptor: int,
    *,
    expected_identity: tuple[int, int] | None = None,
    expected_children: dict[str, tuple[int, int]] | None = None,
    selection: TreeSelection = ALL_ENTRIES,
) -> TreeSnapshot:
    """Capture a tree below an already retained directory descriptor."""

    try:
        root = os.fstat(descriptor)
    except OSError as error:
        raise TreeSnapshotError("directory tree cannot be inspected safely") from error
    if not stat.S_ISDIR(root.st_mode) or (
        expected_identity is not None
        and (root.st_dev, root.st_ino) != expected_identity
    ):
        raise TreeChangedError("directory tree changed before it was captured")
    directories: list[_DirectoryRecord] = []
    entries: list[_EntryRecord] = []
    files: list[tuple[str, bytes]] = []
    symlinks: list[tuple[str, str]] = []
    special: list[tuple[str, int]] = []
    placeholders: list[str] = []
    omitted: list[tuple[str, str]] = []
    opaque_directories: list[str] = []
    budget = _CaptureBudget(selection.limits)
    try:
        _scan_directory(
            descriptor,
            relative="",
            depth=0,
            identity=_stat_signature(root),
            directories=directories,
            entries=entries,
            files=files,
            symlinks=symlinks,
            special=special,
            placeholders=placeholders,
            omitted=omitted,
            opaque_directories=opaque_directories,
            selection=selection,
            budget=budget,
        )
        _verify_captured_children(directories, entries, expected_children or {})
        _tree_snapshot_checkpoint("before-final-verification", "")
        _verify_snapshot(
            descriptor,
            directories,
            entries,
            limits=selection.limits,
        )
    except (_PermissionDenied, _TreeChanged, OSError, RecursionError) as error:
        raise _capture_failure(error) from error
    return TreeSnapshot(
        root_identity=(root.st_dev, root.st_ino),
        directories=tuple(sorted(record.relative for record in directories)),
        files=tuple(sorted(files)),
        symlinks=tuple(sorted(symlinks)),
        special=tuple(sorted(special)),
        placeholders=tuple(sorted(placeholders)),
        omitted=tuple(sorted(omitted)),
        identities=_included_identities(directories, entries),
        opaque_directories=tuple(sorted(opaque_directories)),
    )


class _TreeChanged(Exception):
    """The retained directory did not remain one stable generation."""


class _PermissionDenied(Exception):
    """An entry selected for capture could not be opened or listed."""

    def __init__(self, relative: str) -> None:
        super().__init__(relative)
        self.relative = relative


# Errors that a concurrent rename, removal, or type change produces mid-capture.
_CHANGED_ERRNOS = frozenset(
    getattr(errno, name)
    for name in (
        "ENOENT",
        "ENOTDIR",
        "EISDIR",
        "ELOOP",
        "EINVAL",
        "ENXIO",
        "EOPNOTSUPP",
        "ESTALE",
        "EMLINK",
        "EFTYPE",
    )
    if hasattr(errno, name)
)


def _capture_failure(error: BaseException) -> TreeSnapshotError:
    """Classify a capture failure as a retryable change or a lasting error."""

    if isinstance(error, _PermissionDenied):
        return TreeSnapshotError(f"permission denied: {error.relative}")
    if isinstance(error, PermissionError):
        return TreeSnapshotError("permission denied while the directory tree was captured")
    if isinstance(error, _TreeChanged) or (
        isinstance(error, OSError) and error.errno in _CHANGED_ERRNOS
    ):
        return TreeChangedError("directory tree changed while it was captured")
    if isinstance(error, RecursionError):
        return TreeSnapshotError("directory tree is nested too deeply to capture")
    if isinstance(error, OSError) and error.errno is not None:
        return TreeSnapshotError(f"directory tree could not be read: {os.strerror(error.errno)}")
    return TreeSnapshotError("directory tree could not be read")


def _tree_snapshot_checkpoint(_event: str, _relative: str) -> None:
    """Deterministic concurrency boundary used by adversarial tests."""


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


def _cross_interface_signature(signature: tuple[int, ...]) -> tuple[int, ...]:
    """Return fields shared by path stat and descriptor fstat views.

    On Windows, CPython gives path ``stat`` birth-time ``st_ctime_ns`` while
    descriptor ``fstat`` keeps the filesystem change time.  Path stat can also
    add executable permission bits based on the filename.  Normalize only those
    Windows differences; callers still compare full signatures within each
    interface, and other platforms require the complete signatures to agree.
    """

    if not _WINDOWS_STAT_VIEWS:
        return signature
    executable_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    return (
        signature[0],
        signature[1],
        signature[2] & ~executable_bits,
        signature[3],
        signature[4],
        signature[5],
    )


def _valid_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and "\0" not in name
    )


def _normalized_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _stable_entry_identity(signature: tuple[int, ...]) -> tuple[int, int, int]:
    """Identity fields unaffected by writes below a directory."""

    return signature[0], signature[1], stat.S_IFMT(signature[2])


def _opaque_marker(
    descriptor: int,
    relative: str,
    selection: TreeSelection,
    *,
    budget: _CaptureBudget,
    depth: int,
) -> tuple[_OpaqueMarkerRecord | None, bytes | None, tuple[str, ...]]:
    """Recognize one bounded marker without reading any sibling descendants."""

    names = _capture_directory_names(
        descriptor,
        budget=budget,
        depth=depth + 1,
    )
    by_folded_name: dict[str, list[str]] = {}
    for name in names:
        by_folded_name.setdefault(_normalized_name(name), []).append(name)
    for marker in selection.opaque_markers:
        matches = by_folded_name.get(_normalized_name(marker.name), [])
        if len(matches) != 1:
            continue
        name = matches[0]
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        identity = _stat_signature(metadata)
        if marker.presence_only:
            return (
                _OpaqueMarkerRecord(name, identity, None, marker.max_bytes),
                None,
                names,
            )
        if not stat.S_ISREG(metadata.st_mode):
            continue
        max_bytes = budget.file_read_limit(metadata.st_size, marker.max_bytes)
        try:
            data = _read_file(
                descriptor,
                name,
                identity,
                max_bytes=max_bytes,
            )
        except PermissionError as error:
            marker_path = f"{relative}/{name}" if relative else name
            raise _PermissionDenied(marker_path) from error
        if len(data) <= marker.max_bytes and marker.recognizes(data):
            budget.add_file_bytes(len(data))
            return (
                _OpaqueMarkerRecord(name, identity, data, marker.max_bytes),
                data,
                names,
            )
    return None, None, names


def _scan_directory(
    descriptor: int,
    *,
    relative: str,
    depth: int,
    identity: tuple[int, ...],
    directories: list[_DirectoryRecord],
    entries: list[_EntryRecord],
    files: list[tuple[str, bytes]],
    symlinks: list[tuple[str, str]],
    special: list[tuple[str, int]],
    placeholders: list[str],
    omitted: list[tuple[str, str]],
    opaque_directories: list[str],
    selection: TreeSelection,
    budget: _CaptureBudget,
) -> bool:
    # The root is the directory the caller explicitly asked to inspect.  A
    # marker name in that directory must not turn the entire requested input
    # into generated output.
    if relative and selection.opaque_markers:
        marker, marker_data, names = _opaque_marker(
            descriptor,
            relative,
            selection,
            budget=budget,
            depth=depth,
        )
    else:
        marker = None
        marker_data = None
        names = _capture_directory_names(
            descriptor,
            budget=budget,
            depth=depth + 1,
        )
    if marker is not None:
        directories.append(_DirectoryRecord(relative, identity, (), marker))
        marker_relative = f"{relative}/{marker.name}"
        entries.append(
            _EntryRecord(
                marker_relative,
                marker.identity,
                ignored=marker_data is None,
            )
        )
        if marker_data is not None:
            files.append((marker_relative, marker_data))
        opaque_directories.append(relative)
        return True
    directories.append(_DirectoryRecord(relative, identity, names))
    _tree_snapshot_checkpoint("after-directory-list", relative)
    for name in names:
        child_relative = f"{relative}/{name}" if relative else name
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        child_identity = _stat_signature(metadata)
        relative_path = PurePosixPath(child_relative)
        if stat.S_ISDIR(metadata.st_mode):
            if not selection.descend(relative_path):
                entries.append(_EntryRecord(child_relative, child_identity, ignored=True))
                if selection.record_omitted:
                    omitted.append((child_relative, "directory"))
                continue
            child_descriptor: int | None = None
            try:
                try:
                    child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except PermissionError as error:
                    raise _PermissionDenied(child_relative) from error
                opened = os.fstat(child_descriptor)
                opened_identity = _stat_signature(opened)
                if _stable_entry_identity(opened_identity) != _stable_entry_identity(
                    child_identity
                ):
                    raise _TreeChanged
                _scan_directory(
                    child_descriptor,
                    relative=child_relative,
                    depth=depth + 1,
                    identity=opened_identity,
                    directories=directories,
                    entries=entries,
                    files=files,
                    symlinks=symlinks,
                    special=special,
                    placeholders=placeholders,
                    omitted=omitted,
                    opaque_directories=opaque_directories,
                    selection=selection,
                    budget=budget,
                )
                final_identity = _stat_signature(
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                )
                if _stable_entry_identity(final_identity) != _stable_entry_identity(
                    opened_identity
                ):
                    raise _TreeChanged
            finally:
                if child_descriptor is not None:
                    _close_descriptor(child_descriptor)
            continue
        if not selection.include(relative_path, metadata.st_mode):
            if stat.S_ISREG(metadata.st_mode) and selection.placeholder(
                relative_path,
                metadata.st_mode,
            ):
                entries.append(_EntryRecord(child_relative, child_identity))
                placeholders.append(child_relative)
                continue
            entries.append(_EntryRecord(child_relative, child_identity, ignored=True))
            kind = (
                "file"
                if stat.S_ISREG(metadata.st_mode)
                else "symlink"
                if stat.S_ISLNK(metadata.st_mode)
                else "special"
            )
            if selection.record_omitted:
                omitted.append((child_relative, kind))
            continue
        entries.append(_EntryRecord(child_relative, child_identity))
        if stat.S_ISREG(metadata.st_mode):
            max_bytes = budget.file_read_limit(
                metadata.st_size,
                selection.byte_limit(relative_path),
            )
            try:
                data = _read_file(
                    descriptor,
                    name,
                    child_identity,
                    max_bytes=max_bytes,
                )
            except PermissionError as error:
                raise _PermissionDenied(child_relative) from error
            budget.add_file_bytes(len(data))
            files.append((child_relative, data))
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(name, dir_fd=descriptor)
            if _stat_signature(
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            ) != child_identity:
                raise _TreeChanged
            symlinks.append((child_relative, target))
        else:
            special.append((child_relative, stat.S_IFMT(metadata.st_mode)))
    if (
        _stable_entry_identity(
            _stat_signature(os.fstat(descriptor))
        ) != _stable_entry_identity(identity)
        or not _directory_names_match(
            descriptor,
            names,
            limits=budget.limits,
        )
    ):
        raise _TreeChanged
    return False


def _read_file(
    parent_descriptor: int,
    name: str,
    expected: tuple[int, ...],
    *,
    max_bytes: int | None = None,
) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _stat_signature(opened) != expected:
            raise _TreeChanged
        stream = os.fdopen(descriptor, "rb", buffering=0, closefd=False)
        try:
            data = stream.read() if max_bytes is None else _read_prefix(stream, max_bytes + 1)
        finally:
            stream.close()
        if (
            _stat_signature(os.fstat(descriptor)) != expected
            or _stat_signature(
                os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            )
            != expected
        ):
            raise _TreeChanged
        return data
    finally:
        if descriptor is not None:
            _close_descriptor(descriptor)


def _read_prefix(stream, length: int) -> bytes:
    """Read through *length* bytes or EOF despite legal short reads."""

    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _verify_snapshot(
    root_descriptor: int,
    directories: list[_DirectoryRecord],
    entries: list[_EntryRecord],
    *,
    limits: TreeCaptureLimits,
) -> None:
    expected_directories = {record.relative: record for record in directories}
    expected_entries = {record.relative: record for record in entries}
    visited_directories: set[str] = set()
    visited_entries: set[str] = set()

    def verify_marker(
        descriptor: int,
        relative: str,
        marker: _OpaqueMarkerRecord,
    ) -> None:
        metadata = os.stat(marker.name, dir_fd=descriptor, follow_symlinks=False)
        observed = _stat_signature(metadata)
        if marker.data is None:
            if _entry_kind(observed[2]) != _entry_kind(marker.identity[2]):
                raise _TreeChanged
        else:
            if observed != marker.identity:
                raise _TreeChanged
            if (
                _read_file(
                    descriptor,
                    marker.name,
                    marker.identity,
                    max_bytes=marker.max_bytes,
                )
                != marker.data
            ):
                raise _TreeChanged
        marker_relative = f"{relative}/{marker.name}" if relative else marker.name
        expected_entry = expected_entries.get(marker_relative)
        if expected_entry is None or (
            not expected_entry.ignored and expected_entry.identity != marker.identity
        ):
            raise _TreeChanged
        visited_entries.add(marker_relative)

    def verify_directory(descriptor: int, relative: str) -> None:
        expected = expected_directories.get(relative)
        if expected is None:
            raise _TreeChanged
        visited_directories.add(relative)
        if expected.marker is not None:
            if _stable_entry_identity(
                _stat_signature(os.fstat(descriptor))
            ) != _stable_entry_identity(expected.identity):
                raise _TreeChanged
            verify_marker(descriptor, relative, expected.marker)
            return
        names_match = _directory_names_match(
            descriptor,
            expected.names,
            limits=limits,
        )
        names = expected.names
        if (
            _stable_entry_identity(_stat_signature(os.fstat(descriptor)))
            != _stable_entry_identity(expected.identity)
            or not names_match
        ):
            raise _TreeChanged
        for name in names:
            child_relative = f"{relative}/{name}" if relative else name
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            directory = expected_directories.get(child_relative)
            if directory is None:
                entry = expected_entries.get(child_relative)
                if entry is None or (
                    not entry.ignored and _stat_signature(metadata) != entry.identity
                ):
                    raise _TreeChanged
                if entry.ignored and _entry_kind(metadata.st_mode) != _entry_kind(
                    entry.identity[2]
                ):
                    raise _TreeChanged
                visited_entries.add(child_relative)
                continue
            observed_identity = _stat_signature(metadata)
            expected_identity = directory.identity
            if not stat.S_ISDIR(metadata.st_mode) or _stable_entry_identity(
                observed_identity
            ) != _stable_entry_identity(expected_identity):
                raise _TreeChanged
            child_descriptor: int | None = None
            try:
                child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                opened_identity = _stat_signature(os.fstat(child_descriptor))
                if _stable_entry_identity(opened_identity) != _stable_entry_identity(
                    directory.identity
                ):
                    raise _TreeChanged
                verify_directory(child_descriptor, child_relative)
                final_identity = _stat_signature(
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                )
                if _stable_entry_identity(final_identity) != _stable_entry_identity(
                    directory.identity
                ):
                    raise _TreeChanged
            finally:
                if child_descriptor is not None:
                    _close_descriptor(child_descriptor)
        if (
            _stable_entry_identity(
                _stat_signature(os.fstat(descriptor))
            ) != _stable_entry_identity(expected.identity)
            or not _directory_names_match(
                descriptor,
                expected.names,
                limits=limits,
            )
        ):
            raise _TreeChanged

    verify_directory(root_descriptor, "")
    if visited_directories != set(expected_directories) or visited_entries != set(
        expected_entries
    ):
        raise _TreeChanged


def _verify_captured_children(
    directories: list[_DirectoryRecord],
    entries: list[_EntryRecord],
    expected_children: dict[str, tuple[int, int]],
) -> None:
    if not expected_children:
        return
    root_children = [
        (record.relative, record.identity[:2])
        for record in [*directories, *entries]
        if record.relative and "/" not in record.relative
    ]
    for name, identity in expected_children.items():
        matches = [
            (actual, observed_identity)
            for actual, observed_identity in root_children
            if _normalized_name(actual) == _normalized_name(name)
        ]
        if len(matches) != 1 or matches[0][1] != identity:
            raise _TreeChanged


def _capture_portable(
    root: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    expected_children: dict[str, tuple[int, int]] | None = None,
    selection: TreeSelection,
) -> TreeSnapshot:
    """Best-effort double-captured fallback for read-only non-POSIX clients.

    Paths are re-resolved on every access, so this is not a generation
    boundary.  On Windows, path ``stat`` reports birth time as ``st_ctime_ns``;
    a directory junction swapped in for a subdirectory, read through, and
    swapped back out can pass both captures unnoticed if the parent's
    modification time is reset.  One swapped in for the root needs no reset,
    because the root's ancestors are compared only by identity.
    """

    path_identities = _portable_directory_identities(root)
    root_before = os.lstat(root)
    root_identity = (root_before.st_dev, root_before.st_ino)
    if (
        not stat.S_ISDIR(root_before.st_mode)
        or _is_reparse_point(root_before)
        or (expected_identity is not None and root_identity != expected_identity)
    ):
        raise TreeSnapshotError("directory tree cannot be inspected safely")
    required_children = expected_children or {}
    observed_children: set[str] = set()
    directories: list[str] = [""]
    files: list[tuple[str, bytes]] = []
    symlinks: list[tuple[str, str]] = []
    special: list[tuple[str, int]] = []
    placeholders: list[str] = []
    omitted: list[tuple[str, str]] = []
    opaque_directories: list[str] = []
    identities: list[tuple[str, tuple[int, ...]]] = [("", _stat_signature(root_before))]
    budget = _CaptureBudget(selection.limits)

    def visit(directory: Path, relative: str, depth: int) -> None:
        try:
            names = _capture_path_names(
                directory,
                budget=budget,
                depth=depth + 1,
            )
        except PermissionError as error:
            raise _PermissionDenied(relative or ".") from error
        _tree_snapshot_checkpoint("after-directory-list", relative)
        if relative and selection.opaque_markers:
            by_folded_name: dict[str, list[str]] = {}
            for name in names:
                by_folded_name.setdefault(_normalized_name(name), []).append(name)
            for marker in selection.opaque_markers:
                matches = by_folded_name.get(_normalized_name(marker.name), [])
                if len(matches) != 1:
                    continue
                name = matches[0]
                marker_path = directory / name
                metadata = os.lstat(marker_path)
                before = _stat_signature(metadata)
                if marker.presence_only:
                    opaque_directories.append(relative)
                    return
                if not stat.S_ISREG(metadata.st_mode) or _is_reparse_point(metadata):
                    continue
                max_bytes = budget.file_read_limit(metadata.st_size, marker.max_bytes)
                try:
                    data = _read_portable_file(
                        marker_path,
                        before,
                        max_bytes=max_bytes,
                    )
                except PermissionError as error:
                    marker_relative = f"{relative}/{name}"
                    raise _PermissionDenied(marker_relative) from error
                if len(data) <= marker.max_bytes and marker.recognizes(data):
                    budget.add_file_bytes(len(data))
                    marker_relative = f"{relative}/{name}"
                    files.append((marker_relative, data))
                    identities.append((marker_relative, before))
                    opaque_directories.append(relative)
                    return
        for name in names:
            child_relative = f"{relative}/{name}" if relative else name
            path = directory / name
            metadata = os.lstat(path)
            if relative == "":
                folded = _normalized_name(name)
                for required_name, required_identity in required_children.items():
                    if folded != _normalized_name(required_name):
                        continue
                    if (metadata.st_dev, metadata.st_ino) != required_identity:
                        raise TreeChangedError(
                            "directory tree changed while it was captured"
                        )
                    observed_children.add(required_name)
            relative_path = PurePosixPath(child_relative)
            if stat.S_ISDIR(metadata.st_mode) and not _is_reparse_point(metadata):
                if not selection.descend(relative_path):
                    if selection.record_omitted:
                        omitted.append((child_relative, "directory"))
                    continue
                directories.append(child_relative)
                identities.append((child_relative, _stat_signature(metadata)))
                visit(path, child_relative, depth + 1)
                final = os.lstat(path)
                if (
                    not stat.S_ISDIR(final.st_mode)
                    or _is_reparse_point(final)
                    or _stat_signature(final) != _stat_signature(metadata)
                ):
                    raise TreeChangedError(
                        "directory tree changed while it was captured"
                    )
            elif stat.S_ISREG(metadata.st_mode) and not _is_reparse_point(metadata):
                if not selection.include(relative_path, metadata.st_mode):
                    if selection.placeholder(relative_path, metadata.st_mode):
                        placeholders.append(child_relative)
                        identities.append((child_relative, _stat_signature(metadata)))
                    else:
                        if selection.record_omitted:
                            omitted.append((child_relative, "file"))
                    continue
                before = _stat_signature(metadata)
                max_bytes = budget.file_read_limit(
                    metadata.st_size,
                    selection.byte_limit(relative_path),
                )
                try:
                    data = _read_portable_file(
                        path,
                        before,
                        max_bytes=max_bytes,
                    )
                except PermissionError as error:
                    raise _PermissionDenied(child_relative) from error
                budget.add_file_bytes(len(data))
                files.append((child_relative, data))
                identities.append((child_relative, before))
            elif stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
                if selection.include(relative_path, metadata.st_mode):
                    try:
                        target = os.readlink(path)
                    except ValueError as error:
                        raise TreeSnapshotError(
                            "directory tree contains an unsupported reparse point"
                        ) from error
                    symlinks.append((child_relative, target))
                    identities.append((child_relative, _stat_signature(metadata)))
                else:
                    if selection.record_omitted:
                        omitted.append((child_relative, "symlink"))
            else:
                if selection.include(relative_path, metadata.st_mode):
                    special.append((child_relative, stat.S_IFMT(metadata.st_mode)))
                    identities.append((child_relative, _stat_signature(metadata)))
                else:
                    if selection.record_omitted:
                        omitted.append((child_relative, "special"))

    visit(root, "", 0)
    root_after = os.lstat(root)
    if (
        observed_children != set(required_children)
        or _stat_signature(root_before) != _stat_signature(root_after)
        or _portable_directory_identities(root) != path_identities
    ):
        raise TreeChangedError("directory tree changed while it was captured")
    return TreeSnapshot(
        root_identity=(root_after.st_dev, root_after.st_ino),
        directories=tuple(sorted(directories)),
        files=tuple(sorted(files)),
        symlinks=tuple(sorted(symlinks)),
        special=tuple(sorted(special)),
        placeholders=tuple(sorted(placeholders)),
        omitted=tuple(sorted(omitted)),
        identities=tuple(sorted(identities)),
        opaque_directories=tuple(sorted(opaque_directories)),
    )


def _read_portable_file(
    path: Path,
    expected: tuple[int, ...],
    *,
    max_bytes: int | None = None,
) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _FILE_FLAGS)
        opened = os.fstat(descriptor)
        opened_signature = _stat_signature(opened)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _cross_interface_signature(opened_signature)
            != _cross_interface_signature(expected)
        ):
            raise TreeChangedError("directory tree changed while it was captured")
        stream = os.fdopen(descriptor, "rb", buffering=0, closefd=False)
        try:
            data = stream.read() if max_bytes is None else _read_prefix(stream, max_bytes + 1)
        finally:
            stream.close()
        final_opened = os.fstat(descriptor)
        final_named = os.lstat(path)
        if (
            _stat_signature(final_opened) != opened_signature
            or _stat_signature(final_named) != expected
        ):
            raise TreeChangedError("directory tree changed while it was captured")
        return data
    finally:
        if descriptor is not None:
            _close_descriptor(descriptor)


def _portable_directory_identities(root: Path) -> tuple[tuple[int, int], ...]:
    """Inspect every lexical ancestor when descriptor traversal is unavailable."""

    if not root.is_absolute() or not root.anchor:
        raise TreeSnapshotError("directory tree path is not absolute")
    try:
        current = Path(root.anchor)
        identities: list[tuple[int, int]] = []
        for index, part in enumerate(root.parts):
            if index:
                if part == "..":
                    raise TreeSnapshotError(
                        "parent path components require directory descriptor support"
                    )
                current = current / part
            metadata = os.lstat(current)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse_point(metadata)
            ):
                raise TreeSnapshotError("directory tree path contains an unsafe component")
            identities.append((metadata.st_dev, metadata.st_ino))
        return tuple(identities)
    except TreeSnapshotError:
        raise
    except (OSError, ValueError) as error:
        raise TreeSnapshotError("directory tree cannot be inspected safely") from error


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _special_file_kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "named pipe"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block device"
    if stat.S_ISCHR(mode):
        return "character device"
    return "special filesystem entry"


def _entry_kind(mode: int) -> int:
    return stat.S_IFMT(mode)


def _update_digest(digest, kind: bytes, relative: str, data: bytes) -> None:
    path = os.fsencode(relative)
    for field in (kind, path, data):
        digest.update(len(field).to_bytes(8, "big"))
        digest.update(field)


def _included_identities(
    directories: list[_DirectoryRecord],
    entries: list[_EntryRecord],
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    return tuple(
        sorted(
            [
                *((record.relative, record.identity) for record in directories),
                *(
                    (record.relative, record.identity)
                    for record in entries
                    if not record.ignored
                ),
            ]
        )
    )
