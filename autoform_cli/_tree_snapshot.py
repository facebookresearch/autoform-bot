"""Capture one immutable directory-tree generation for internal consumers."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

from . import workspace as workspace_module
from .workspace import _WorkspaceRootBinding, _open_workspace_root
from .workspace_manifest import WorkspaceError

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


class TreeSnapshotError(ValueError):
    """A directory tree could not be captured as one stable generation."""


@dataclass(frozen=True, slots=True)
class TreeSelection:
    """Select which paths are descended into and captured as bytes."""

    include: Callable[[PurePosixPath, int], bool]
    descend: Callable[[PurePosixPath], bool]
    placeholder: Callable[[PurePosixPath, int], bool] = lambda _path, _mode: False
    byte_limit: Callable[[PurePosixPath], int | None] = lambda _path: None
    record_omitted: bool = True


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
        return digest.hexdigest()

    @property
    def generation_revision(self) -> str:
        """Return a digest that also distinguishes filesystem generations."""

        digest = hashlib.sha256(self.revision.encode("ascii"))
        directory_paths = set(self.directories)
        for relative, identity in self.identities:
            stable_identity = identity[:3] if relative in directory_paths else identity
            encoded = ",".join(str(field) for field in stable_identity).encode("ascii")
            _update_digest(digest, b"identity", relative, encoded)
        return digest.hexdigest()

    def materialize(self, destination: Path) -> None:
        """Write captured regular files below a fresh private directory."""

        issues = self.unsupported_entries()
        if issues:
            relative, reason = issues[0]
            raise TreeSnapshotError(f"{relative}: {reason}")
        self.materialize_regular_files(destination)

    def materialize_regular_files(self, destination: Path) -> None:
        """Materialize safe content after a caller has recorded invalid entries."""

        destination.mkdir(parents=True, exist_ok=False)
        for relative in self.directories:
            if relative:
                destination.joinpath(*PurePosixPath(relative).parts).mkdir()
        for relative, data in self.files:
            target = destination.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for relative in self.placeholders:
            target = destination.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()

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


@dataclass(frozen=True, slots=True)
class _EntryRecord:
    relative: str
    identity: tuple[int, ...]
    ignored: bool = False


class BoundDirectoryTree:
    """One retained directory generation that can be recaptured and compared."""

    def __init__(
        self,
        root: Path,
        *,
        expected_identity: tuple[int, int] | None = None,
        expected_children: dict[str, tuple[int, int]] | None = None,
        selection: TreeSelection = ALL_ENTRIES,
    ) -> None:
        self.root = root
        self.expected_identity = expected_identity
        self.expected_children = expected_children or {}
        self.selection = selection
        self._binding: _WorkspaceRootBinding | None = None
        self._portable_identity: tuple[int, int] | None = None
        self._closed = False
        if workspace_module._DIRECTORY_BINDING_SUPPORTED:
            try:
                binding = _open_workspace_root(root)
            except WorkspaceError as error:
                raise TreeSnapshotError("directory tree cannot be inspected safely") from error
            if expected_identity is not None and binding.identity != expected_identity:
                binding.close()
                raise TreeSnapshotError("directory tree changed before it was captured")
            self._binding = binding
            try:
                self._verify_expected_children(binding.descriptor)
            except BaseException:
                binding.close()
                self._binding = None
                raise
            return
        try:
            metadata = root.stat(follow_symlinks=False)
        except OSError as error:
            raise TreeSnapshotError("directory tree cannot be inspected safely") from error
        identity = (metadata.st_dev, metadata.st_ino)
        if not stat.S_ISDIR(metadata.st_mode) or (
            expected_identity is not None and identity != expected_identity
        ):
            raise TreeSnapshotError("directory tree changed before it was captured")
        self._portable_identity = identity
        self._verify_expected_children(None)

    @property
    def identity(self) -> tuple[int, int]:
        if self._closed:
            raise TreeSnapshotError("directory tree binding is closed")
        if self._binding is not None:
            return self._binding.identity
        assert self._portable_identity is not None
        return self._portable_identity

    def _verify_expected_children(self, descriptor: int | None) -> None:
        try:
            for name, expected in self.expected_children.items():
                if Path(name).name != name or name in {"", ".", ".."}:
                    raise TreeSnapshotError("invalid bound child name")
                metadata = (
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if descriptor is not None
                    else (self.root / name).stat(follow_symlinks=False)
                )
                if not stat.S_ISDIR(metadata.st_mode) or (
                    metadata.st_dev,
                    metadata.st_ino,
                ) != expected:
                    raise TreeSnapshotError("directory tree changed before it was captured")
        except OSError as error:
            raise TreeSnapshotError("directory tree changed before it was captured") from error

    def verify(self) -> None:
        """Verify the retained generation is still selected by its public path."""

        if self._closed:
            raise TreeSnapshotError("directory tree binding is closed")
        try:
            if self._binding is not None:
                self._binding.verify()
                self._verify_expected_children(self._binding.descriptor)
                return
            metadata = self.root.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode) or (
                metadata.st_dev,
                metadata.st_ino,
            ) != self.identity:
                raise TreeSnapshotError("directory tree changed while it was in use")
            self._verify_expected_children(None)
        except (OSError, WorkspaceError) as error:
            raise TreeSnapshotError("directory tree changed while it was in use") from error

    def capture(self) -> TreeSnapshot:
        """Capture one stable tree through the retained directory generation."""

        self.verify()
        try:
            if self._binding is not None:
                snapshot = capture_directory_descriptor(
                    self._binding.descriptor,
                    expected_identity=self.identity,
                    selection=self.selection,
                )
            else:
                first = _capture_portable(self.root, selection=self.selection)
                _tree_snapshot_checkpoint("between-portable-captures", "")
                snapshot = _capture_portable(self.root, selection=self.selection)
                if first != snapshot:
                    raise TreeSnapshotError("directory tree changed while it was captured")
        except (OSError, RuntimeError) as error:
            raise TreeSnapshotError("directory tree changed while it was captured") from error
        self.verify()
        return snapshot

    def close(self) -> None:
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
) -> Iterator[BoundDirectoryTree]:
    """Retain *root* while callers capture and verify its content generation."""

    bound = BoundDirectoryTree(
        root,
        expected_identity=expected_identity,
        expected_children=expected_children,
        selection=selection,
    )
    try:
        yield bound
    finally:
        bound.close()


def capture_directory_descriptor(
    descriptor: int,
    *,
    expected_identity: tuple[int, int] | None = None,
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
        raise TreeSnapshotError("directory tree changed before it was captured")
    directories: list[_DirectoryRecord] = []
    entries: list[_EntryRecord] = []
    files: list[tuple[str, bytes]] = []
    symlinks: list[tuple[str, str]] = []
    special: list[tuple[str, int]] = []
    placeholders: list[str] = []
    omitted: list[tuple[str, str]] = []
    try:
        _scan_directory(
            descriptor,
            relative="",
            identity=_stat_signature(root),
            directories=directories,
            entries=entries,
            files=files,
            symlinks=symlinks,
            special=special,
            placeholders=placeholders,
            omitted=omitted,
            selection=selection,
        )
        _tree_snapshot_checkpoint("before-final-verification", "")
        _verify_snapshot(descriptor, directories, entries)
    except (OSError, _TreeChanged) as error:
        raise TreeSnapshotError("directory tree changed while it was captured") from error
    return TreeSnapshot(
        root_identity=(root.st_dev, root.st_ino),
        directories=tuple(record.relative for record in directories),
        files=tuple(sorted(files)),
        symlinks=tuple(sorted(symlinks)),
        special=tuple(sorted(special)),
        placeholders=tuple(sorted(placeholders)),
        omitted=tuple(sorted(omitted)),
        identities=_included_identities(directories, entries),
    )


class _TreeChanged(Exception):
    """The retained directory did not remain one stable generation."""


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


def _valid_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
    )


def _scan_directory(
    descriptor: int,
    *,
    relative: str,
    identity: tuple[int, ...],
    directories: list[_DirectoryRecord],
    entries: list[_EntryRecord],
    files: list[tuple[str, bytes]],
    symlinks: list[tuple[str, str]],
    special: list[tuple[str, int]],
    placeholders: list[str],
    omitted: list[tuple[str, str]],
    selection: TreeSelection,
) -> None:
    names = tuple(sorted(os.listdir(descriptor)))
    if any(not _valid_name(name) for name in names):
        raise _TreeChanged
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
                child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                opened = os.fstat(child_descriptor)
                if _stat_signature(opened) != child_identity:
                    raise _TreeChanged
                _scan_directory(
                    child_descriptor,
                    relative=child_relative,
                    identity=child_identity,
                    directories=directories,
                    entries=entries,
                    files=files,
                    symlinks=symlinks,
                    special=special,
                    placeholders=placeholders,
                    omitted=omitted,
                    selection=selection,
                )
                if _stat_signature(
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                ) != child_identity:
                    raise _TreeChanged
            finally:
                if child_descriptor is not None:
                    os.close(child_descriptor)
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
            files.append(
                (
                    child_relative,
                    _read_file(
                        descriptor,
                        name,
                        child_identity,
                        max_bytes=selection.byte_limit(relative_path),
                    ),
                )
            )
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
        _stat_signature(os.fstat(descriptor)) != identity
        or tuple(sorted(os.listdir(descriptor))) != names
    ):
        raise _TreeChanged


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
            os.close(descriptor)


def _read_prefix(stream, length: int) -> bytes:
    """Read through *length* bytes or EOF despite legal short reads."""

    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _verify_snapshot(
    root_descriptor: int,
    directories: list[_DirectoryRecord],
    entries: list[_EntryRecord],
) -> None:
    expected_directories = {record.relative: record for record in directories}
    expected_entries = {record.relative: record for record in entries}
    visited_directories: set[str] = set()
    visited_entries: set[str] = set()

    def verify_directory(descriptor: int, relative: str) -> None:
        expected = expected_directories.get(relative)
        if expected is None:
            raise _TreeChanged
        visited_directories.add(relative)
        names = tuple(sorted(os.listdir(descriptor)))
        if _stat_signature(os.fstat(descriptor)) != expected.identity or names != expected.names:
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
            if not stat.S_ISDIR(metadata.st_mode) or _stat_signature(metadata) != directory.identity:
                raise _TreeChanged
            child_descriptor: int | None = None
            try:
                child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                if _stat_signature(os.fstat(child_descriptor)) != directory.identity:
                    raise _TreeChanged
                verify_directory(child_descriptor, child_relative)
                if _stat_signature(
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                ) != directory.identity:
                    raise _TreeChanged
            finally:
                if child_descriptor is not None:
                    os.close(child_descriptor)
        if (
            _stat_signature(os.fstat(descriptor)) != expected.identity
            or tuple(sorted(os.listdir(descriptor))) != expected.names
        ):
            raise _TreeChanged

    verify_directory(root_descriptor, "")
    if visited_directories != set(expected_directories) or visited_entries != set(
        expected_entries
    ):
        raise _TreeChanged


def _capture_portable(
    root: Path,
    *,
    selection: TreeSelection,
) -> TreeSnapshot:
    """Best-effort double-captured fallback for read-only non-POSIX clients."""

    root_before = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(root_before.st_mode) or _is_reparse_point(root_before):
        raise TreeSnapshotError("directory tree cannot be inspected safely")
    directories: list[str] = [""]
    files: list[tuple[str, bytes]] = []
    symlinks: list[tuple[str, str]] = []
    special: list[tuple[str, int]] = []
    placeholders: list[str] = []
    omitted: list[tuple[str, str]] = []
    identities: list[tuple[str, tuple[int, ...]]] = [("", _stat_signature(root_before))]

    def visit(directory: Path, relative: str) -> None:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        for entry in entries:
            if not _valid_name(entry.name):
                raise TreeSnapshotError("directory tree cannot be inspected safely")
            child_relative = f"{relative}/{entry.name}" if relative else entry.name
            metadata = entry.stat(follow_symlinks=False)
            path = directory / entry.name
            relative_path = PurePosixPath(child_relative)
            if stat.S_ISDIR(metadata.st_mode) and not _is_reparse_point(metadata):
                if not selection.descend(relative_path):
                    if selection.record_omitted:
                        omitted.append((child_relative, "directory"))
                    continue
                directories.append(child_relative)
                identities.append((child_relative, _stat_signature(metadata)))
                visit(path, child_relative)
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
                with path.open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    limit = selection.byte_limit(relative_path)
                    data = stream.read() if limit is None else _read_prefix(stream, limit + 1)
                    after = os.fstat(stream.fileno())
                final = path.stat(follow_symlinks=False)
                if not (before == _stat_signature(opened) == _stat_signature(after) == _stat_signature(final)):
                    raise TreeSnapshotError("directory tree changed while it was captured")
                files.append((child_relative, data))
                identities.append((child_relative, before))
            elif stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
                if selection.include(relative_path, metadata.st_mode):
                    symlinks.append((child_relative, os.readlink(path)))
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

    visit(root, "")
    root_after = root.stat(follow_symlinks=False)
    if _stat_signature(root_before) != _stat_signature(root_after):
        raise TreeSnapshotError("directory tree changed while it was captured")
    return TreeSnapshot(
        root_identity=(root_after.st_dev, root_after.st_ino),
        directories=tuple(sorted(directories)),
        files=tuple(sorted(files)),
        symlinks=tuple(sorted(symlinks)),
        special=tuple(sorted(special)),
        placeholders=tuple(sorted(placeholders)),
        omitted=tuple(sorted(omitted)),
        identities=tuple(sorted(identities)),
    )


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
