"""Capture a directory tree for internal consumers.

With descriptor capture, only the root descriptor is retained: each descended
directory is opened by name through its parent when it is walked, listed again
when its walk ends, and reopened the same way by a final verification.
Captured files are rechecked by their full ``stat`` signature and directories
by ``(dev, ino, file type)``; ``TreeSelection`` says what else must stay put.

A capture is not one atomic generation.  An entry present at a path only after
the walk last looked at that path (when it listed or relisted the parent, or
examined the entry) and before verification reached it is not seen, and
verification lists each directory once, before it walks that directory's
children, so an entry that appears in a directory after that listing is not
seen either.  Coordinated moves across directories between those checks can
therefore yield a set of entries that no single state had.  A recognized
marker is subject to the same windows.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Sequence

from . import _directory_binding as directory_binding
from ._directory_binding import (
    DirectoryChangedError,
    RetainedDirectory,
    close_quietly,
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
_READ_CHUNK_BYTES = 1024 * 1024
_DESCRIPTOR_CAPTURE_SUPPORTED = (
    os.scandir in getattr(os, "supports_fd", ())
    and os.readlink in getattr(os, "supports_dir_fd", ())
)
_WINDOWS_STAT_VIEWS = os.name == "nt"


class TreeSnapshotError(ValueError):
    """A directory tree could not be captured as one stable generation."""


class TreeChangedError(TreeSnapshotError):
    """The directory tree changed while it was bound or captured; a retry may succeed.

    ``relative`` is the root-relative POSIX path of the entry whose recheck
    failed (``.`` for the root), or ``None`` when no single entry is known.  It
    is kept out of the message so callers decide how to show it.
    """

    def __init__(self, message: str, *, relative: str | None = None) -> None:
        super().__init__(message)
        self.relative = relative


class TreeCaptureLimitError(TreeSnapshotError):
    """A named resource limit stopped a directory-tree capture."""

    def __init__(self, limit: str, maximum: int) -> None:
        super().__init__(f"directory tree exceeds {limit}={maximum}")


def _invalid_limit(value: object) -> bool:
    return isinstance(value, bool) or not isinstance(value, int) or value < 0


@dataclass(frozen=True, slots=True)
class TreeCaptureLimits:
    """Optional bounds on observed entries and captured regular-file bytes.

    Every observed child counts as an entry, including omitted entries.  With
    ``TreeSelection.unselected_may_change`` the scan, its end-of-folder
    relists and the final verification each have their own budget under the
    same limits.
    """

    max_entries: int | None = None
    max_total_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "max_entries",
            "max_total_bytes",
        ):
            value = getattr(self, name)
            if value is not None and _invalid_limit(value):
                raise ValueError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class TreeSelection:
    """Select which paths are descended into and captured as bytes.

    By default a capture proves the whole descended tree stable: every
    descended folder keeps its identity and full list of names.

    With ``unselected_may_change`` it proves stable only what it selected: the
    entries ``include`` captures and the folders that contain them.  Any other
    descended folder, including one hidden by a recognized marker, may change,
    vanish or be rebuilt during the capture, provided that whatever is at its
    path when it is rechecked is hidden by a recognized marker or holds no
    selected entry.  Such folders leave no trace in the snapshot or its
    revisions.  The end-of-folder recheck skips an unselected entry that keeps
    the ``(dev, ino, file type)`` the scan saw, so ``include`` must not depend
    on other mode bits.  This mode needs directory-descriptor traversal, does
    not record omitted entries, and is the only one that takes
    ``opaque_markers``.
    """

    include: Callable[[PurePosixPath, int], bool]
    descend: Callable[[PurePosixPath], bool]
    record_omitted: bool = True
    limits: TreeCaptureLimits = TreeCaptureLimits()
    opaque_markers: tuple["OpaqueDirectoryMarker", ...] = ()
    unselected_may_change: bool = False

    def __post_init__(self) -> None:
        if self.unselected_may_change and self.record_omitted:
            raise ValueError("unselected_may_change requires record_omitted=False")
        if self.opaque_markers and not self.unselected_may_change:
            raise ValueError("opaque_markers require unselected_may_change=True")


@dataclass(frozen=True, slots=True)
class OpaqueDirectoryMarker:
    """A bounded marker that makes its containing directory opaque.

    The capture engine reads this small, bounded surface before it visits any
    descendants.  A recognized marker is not kept: each later look recognizes
    it afresh.  A presence-only marker accepts any entry kind without reading
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
        if _invalid_limit(self.max_bytes):
            raise ValueError("opaque marker max_bytes must be a non-negative integer")


ALL_ENTRIES = TreeSelection(
    include=lambda _path, _mode: True,
    descend=lambda _path: True,
)


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    """Immutable names, types, and bytes captured below one directory root.

    A capture with ``digests=True`` holds each file's SHA-256 digest in place
    of its bytes.
    """

    directories: tuple[str, ...]
    files: tuple[tuple[str, bytes], ...]
    symlinks: tuple[tuple[str, str], ...]
    special: tuple[tuple[str, int], ...]
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
        for relative, kind in self.omitted:
            _update_digest(digest, b"omitted", relative, kind.encode("ascii"))
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


@dataclass(slots=True)
class _CaptureBudget:
    limits: TreeCaptureLimits
    entries: int = 0
    total_bytes: int = 0

    def add_entry(self) -> None:
        self.entries += 1
        if (
            self.limits.max_entries is not None
            and self.entries > self.limits.max_entries
        ):
            raise TreeCaptureLimitError("max_entries", self.limits.max_entries)

    def file_read_limit(self, size: int, max_bytes: int | None = None) -> int | None:
        needed = size if max_bytes is None else min(size, max_bytes + 1)
        if (
            self.limits.max_total_bytes is not None
            and self.total_bytes + needed > self.limits.max_total_bytes
        ):
            raise TreeCaptureLimitError(
                "max_total_bytes",
                self.limits.max_total_bytes,
            )
        limits = [max_bytes] if max_bytes is not None else []
        if self.limits.max_total_bytes is not None:
            limits.append(self.limits.max_total_bytes - self.total_bytes)
        return min(limits) if limits else None

    def add_file_bytes(self, count: int) -> None:
        self.total_bytes += count
        if (
            self.limits.max_total_bytes is not None
            and self.total_bytes > self.limits.max_total_bytes
        ):
            raise TreeCaptureLimitError(
                "max_total_bytes",
                self.limits.max_total_bytes,
            )


def _capture_directory_names(
    directory: int | Path,
    *,
    budget: _CaptureBudget,
) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if not _valid_name(entry.name):
                raise TreeSnapshotError("directory tree contains an unsupported entry name")
            budget.add_entry()
            names.append(entry.name)
    return tuple(sorted(names))


def _directory_names_match(
    descriptor: int,
    expected: tuple[str, ...],
) -> bool:
    names: list[str] = []
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            if len(names) == len(expected):
                return False
            names.append(entry.name)
    return tuple(sorted(names)) == expected


def descriptor_capture_supported() -> bool:
    """Whether this host can bind and capture directory trees through descriptors."""

    return directory_binding.DIRECTORY_BINDING_SUPPORTED and _DESCRIPTOR_CAPTURE_SUPPORTED


class BoundDirectoryTree:
    """One retained directory generation that can be recaptured and compared."""

    def __init__(
        self,
        root: Path,
        *,
        expected_identity: tuple[int, int] | None = None,
        selection: TreeSelection = ALL_ENTRIES,
        require_descriptor: bool = False,
    ) -> None:
        self.root = lexical_absolute_path(root)
        self.selection = selection
        self._binding: RetainedDirectory | None = None
        self._portable_identity: tuple[int, int] | None = None
        self._portable_path_identities: tuple[tuple[int, int], ...] | None = None
        self._closed = False
        if descriptor_capture_supported():
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

    @property
    def identity(self) -> tuple[int, int]:
        if self._closed:
            raise TreeSnapshotError("directory tree binding is closed")
        if self._binding is not None:
            return self._binding.identity
        assert self._portable_identity is not None
        return self._portable_identity

    def verify(self) -> None:
        """Verify the retained generation is still selected by its public path."""

        if self._closed:
            raise TreeSnapshotError("directory tree binding is closed")
        try:
            if self._binding is not None:
                self._binding.verify()
                return
            if _portable_directory_identities(self.root) != self._portable_path_identities:
                raise TreeChangedError("directory tree changed while it was in use")
            metadata = os.lstat(self.root)
            if not stat.S_ISDIR(metadata.st_mode) or (
                metadata.st_dev,
                metadata.st_ino,
            ) != self.identity:
                raise TreeChangedError("directory tree changed while it was in use")
        except OSError as error:
            raise TreeChangedError("directory tree changed while it was in use") from error

    def capture(self, *, digests: bool = False) -> TreeSnapshot:
        """Capture one stable tree through the retained directory generation.

        With *digests*, each file is hashed as it is read and only its SHA-256
        digest is kept, so memory does not grow with file sizes.
        """

        self.verify()
        try:
            if self._binding is not None:
                snapshot = capture_directory_descriptor(
                    self._binding.descriptor,
                    selection=self.selection,
                    digests=digests,
                )
            else:
                first = _capture_portable(
                    self.root,
                    expected_identity=self.identity,
                    selection=self.selection,
                    digests=digests,
                )
                _tree_snapshot_checkpoint("between-portable-captures", "")
                _tree_snapshot_checkpoint("before-final-verification", "")
                snapshot = _capture_portable(
                    self.root,
                    expected_identity=self.identity,
                    selection=self.selection,
                    digests=digests,
                )
                if first != snapshot:
                    raise TreeChangedError(
                        "directory tree changed while it was captured"
                    )
        except (_PermissionDenied, OSError, RuntimeError) as error:
            raise _capture_failure(error) from error
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
    selection: TreeSelection = ALL_ENTRIES,
) -> Iterator[BoundDirectoryTree]:
    """Retain *root* while callers capture and verify its content generation."""

    bound = BoundDirectoryTree(
        root,
        expected_identity=expected_identity,
        selection=selection,
    )
    try:
        yield bound
    finally:
        bound.close()


def capture_directory_descriptor(
    descriptor: int,
    *,
    selection: TreeSelection,
    digests: bool,
) -> TreeSnapshot:
    """Capture a tree below an already retained directory descriptor."""

    try:
        root = os.fstat(descriptor)
    except OSError as error:
        raise TreeSnapshotError("directory tree cannot be inspected safely") from error
    directories: list[_DirectoryRecord] = []
    entries: list[_EntryRecord] = []
    files: list[tuple[str, bytes]] = []
    symlinks: list[tuple[str, str]] = []
    special: list[tuple[str, int]] = []
    omitted: list[tuple[str, str]] = []
    budget = _CaptureBudget(selection.limits)
    recheck_budget = _CaptureBudget(selection.limits)

    def scan(descriptor: int, relative: str, identity: tuple[int, ...]) -> bool:
        """Record one folder and its descendants; return whether it was recorded.

        By default every descended folder is recorded.  In selected-entry mode
        only the root and folders that hold a recorded entry or a recorded
        folder are, and an entry that vanishes before it is examined is absent.
        """

        selected_only = selection.unselected_may_change
        # The root is the directory the caller explicitly asked to inspect.  A
        # marker name in that directory must not turn the entire requested input
        # into generated output.
        if relative and selection.opaque_markers:
            names = _opaque_marker(
                descriptor,
                relative,
                selection,
                budget=budget,
            )
            if names is None:
                # A folder hidden by a recognized marker contributes no input, so
                # nothing about it is kept; the rechecks only require whatever
                # is at its path to be hidden by a marker or to hold no input.
                return False
        else:
            names = _capture_directory_names(
                descriptor,
                budget=budget,
            )
        if not selected_only:
            directories.append(_DirectoryRecord(relative, identity, names))
        _tree_snapshot_checkpoint("after-directory-list", relative)
        relevant_names: list[str] = []
        # The (dev, ino, file type) the scan saw for each listed name it did not
        # keep.  The relist at the end of this folder's scan examines again any
        # unkept name whose entry differs, or has no record (new since the
        # listing, or dropped because it had vanished).
        unkept: dict[str, tuple[int, int, int]] = {}
        for name in names:
            child_relative = f"{relative}/{name}" if relative else name
            try:
                metadata = _lookup(descriptor, name, relative)
            except OSError as error:
                if _vanished_in_selected_capture(error, selection):
                    continue
                raise
            child_identity = _stat_signature(metadata)
            relative_path = PurePosixPath(child_relative)
            if stat.S_ISDIR(metadata.st_mode):
                if not selection.descend(relative_path):
                    if selected_only:
                        unkept[name] = _stable_entry_identity(child_identity)
                        continue
                    entries.append(_EntryRecord(child_relative, child_identity, ignored=True))
                    if selection.record_omitted:
                        omitted.append((child_relative, "directory"))
                    continue
                child_descriptor: int | None = None
                try:
                    try:
                        child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    except PermissionError as error:
                        raise _access_denied(descriptor, name, relative) from error
                    except OSError as error:
                        if _vanished_in_selected_capture(error, selection):
                            continue
                        raise
                    opened = os.fstat(child_descriptor)
                    opened_identity = _stat_signature(opened)
                    moved = _stable_entry_identity(opened_identity) != _stable_entry_identity(
                        child_identity
                    )
                    if moved and not selected_only:
                        raise _TreeChanged(child_relative)
                    # Selected mode records a folder by the identity it opened,
                    # and the lookup below checks that its name still leads there;
                    # a folder it does not record is examined again where it is now.
                    if not scan(child_descriptor, child_relative, opened_identity):
                        unkept[name] = _stable_entry_identity(opened_identity)
                        continue
                    final_identity = _stat_signature(_lookup(descriptor, name, relative))
                    if _stable_entry_identity(final_identity) != _stable_entry_identity(
                        opened_identity
                    ):
                        raise _TreeChanged(child_relative)
                    relevant_names.append(name)
                finally:
                    if child_descriptor is not None:
                        close_quietly(child_descriptor)
                continue
            if not selection.include(relative_path, metadata.st_mode):
                if selected_only:
                    unkept[name] = _stable_entry_identity(child_identity)
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
            if stat.S_ISREG(metadata.st_mode):
                max_bytes = budget.file_read_limit(metadata.st_size)
                try:
                    data, size = _read_file(
                        descriptor,
                        name,
                        child_identity,
                        relative=relative,
                        max_bytes=max_bytes,
                        digest=digests,
                    )
                except PermissionError as error:
                    raise _access_denied(descriptor, name, relative) from error
                except OSError as error:
                    if _vanished_in_selected_capture(error, selection):
                        continue
                    raise
                budget.add_file_bytes(size)
                files.append((child_relative, data))
            elif stat.S_ISLNK(metadata.st_mode):
                try:
                    target = os.readlink(name, dir_fd=descriptor)
                except PermissionError as error:
                    raise _access_denied(descriptor, name, relative) from error
                except OSError as error:
                    if _vanished_in_selected_capture(error, selection):
                        continue
                    raise
                try:
                    link_identity = _stat_signature(_lookup(descriptor, name, relative))
                except OSError as error:
                    if _vanished_in_selected_capture(error, selection):
                        continue
                    raise
                if link_identity != child_identity:
                    raise _TreeChanged(child_relative)
                symlinks.append((child_relative, target))
            else:
                special.append((child_relative, stat.S_IFMT(metadata.st_mode)))
            entries.append(_EntryRecord(child_relative, child_identity))
            relevant_names.append(name)
        if selected_only:
            relevant = not relative or bool(relevant_names)
            _recheck_scanned_directory(
                descriptor,
                relative=relative,
                kept=relevant_names,
                unkept=unkept,
                selection=selection,
                budget=recheck_budget,
            )
            if relevant:
                directories.append(_DirectoryRecord(relative, identity, tuple(relevant_names)))
            return relevant
        if (
            _stable_entry_identity(
                _stat_signature(os.fstat(descriptor))
            ) != _stable_entry_identity(identity)
            or not _directory_names_match(
                descriptor,
                names,
            )
        ):
            raise _TreeChanged(relative)
        return True

    try:
        scan(descriptor, "", _stat_signature(root))
        _tree_snapshot_checkpoint("before-final-verification", "")
        _verify_snapshot(
            descriptor,
            directories,
            entries,
            selection=selection,
        )
    except (_PermissionDenied, _TreeChanged, OSError, RecursionError) as error:
        raise _capture_failure(error) from error
    finally:
        # scan holds itself through its closure. Breaking that cycle lets
        # reference counting free the captured bytes along with the snapshot.
        scan = None  # type: ignore[assignment]
    return TreeSnapshot(
        directories=tuple(sorted(record.relative for record in directories)),
        files=tuple(sorted(files)),
        symlinks=tuple(sorted(symlinks)),
        special=tuple(sorted(special)),
        omitted=tuple(sorted(omitted)),
        identities=tuple(
            sorted(
                [(record.relative, record.identity) for record in directories]
                + [(record.relative, record.identity) for record in entries if not record.ignored]
            )
        ),
    )


class _TreeChanged(Exception):
    """The retained directory did not remain one stable generation."""

    def __init__(self, relative: str | None = None) -> None:
        super().__init__(relative)
        self.relative = relative


class _PermissionDenied(Exception):
    """An entry selected for capture could not be opened, listed, or searched."""

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
    if isinstance(error, _TreeChanged):
        return TreeChangedError(
            "directory tree changed while it was captured",
            relative=None if error.relative is None else error.relative or ".",
        )
    if isinstance(error, OSError) and error.errno in _CHANGED_ERRNOS:
        return TreeChangedError("directory tree changed while it was captured")
    if isinstance(error, RecursionError):
        return TreeSnapshotError("directory tree is nested too deeply to capture")
    if isinstance(error, OSError) and error.errno is not None:
        return TreeSnapshotError(f"directory tree could not be read: {os.strerror(error.errno)}")
    return TreeSnapshotError("directory tree could not be read")


def _vanished_in_selected_capture(error: OSError, selection: TreeSelection) -> bool:
    """Whether a selected-entry capture treats this lookup failure as an absent entry."""

    return selection.unselected_may_change and error.errno == errno.ENOENT


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


def _stable_entry_identity(signature: tuple[int, ...]) -> tuple[int, int, int]:
    """Identity fields unaffected by writes below a directory."""

    return signature[0], signature[1], stat.S_IFMT(signature[2])


def _lookup(descriptor: int, name: str, relative: str) -> os.stat_result:
    """Look up *name* in the directory *relative* held open by *descriptor*.

    A lookup needs only search permission on that directory, so a refusal
    names the directory, not the entry.
    """

    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except PermissionError as error:
        raise _PermissionDenied(relative or ".") from error


def _access_denied(descriptor: int, name: str, relative: str) -> _PermissionDenied:
    """Name what refused an open, read or readlink of *name* in *relative*.

    Either the directory denies search permission or the entry denies access.
    Repeating only the lookup tells which: if that is refused too, the
    directory is at fault. Any other lookup failure propagates, so an entry
    that vanished in between reads as a change.
    """

    try:
        _lookup(descriptor, name, relative)
    except _PermissionDenied as denied:
        return denied
    return _PermissionDenied(f"{relative}/{name}" if relative else name)


def _opaque_marker(
    descriptor: int,
    relative: str,
    selection: TreeSelection,
    *,
    budget: _CaptureBudget,
) -> tuple[str, ...] | None:
    """List a folder, or return ``None`` if a bounded marker hides it."""

    names = _capture_directory_names(
        descriptor,
        budget=budget,
    )
    by_folded_name: dict[str, list[str]] = {}
    for name in names:
        by_folded_name.setdefault(_normalized_name(name), []).append(name)
    for marker in selection.opaque_markers:
        matches = by_folded_name.get(_normalized_name(marker.name), [])
        if len(matches) != 1:
            continue
        name = matches[0]
        try:
            metadata = _lookup(descriptor, name, relative)
        except OSError as error:
            if _vanished_in_selected_capture(error, selection):
                continue
            raise
        if marker.presence_only:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            continue
        max_bytes = budget.file_read_limit(metadata.st_size, marker.max_bytes)
        try:
            data, size = _read_file(
                descriptor,
                name,
                _stat_signature(metadata),
                relative=relative,
                max_bytes=max_bytes,
            )
        except PermissionError as error:
            raise _access_denied(descriptor, name, relative) from error
        except OSError as error:
            if _vanished_in_selected_capture(error, selection):
                continue
            raise
        if size <= marker.max_bytes and marker.recognizes(data):
            budget.add_file_bytes(size)
            return None
    return names


def _recheck_scanned_directory(
    descriptor: int,
    *,
    relative: str,
    kept: Sequence[str],
    unkept: dict[str, tuple[int, int, int]],
    selection: TreeSelection,
    budget: _CaptureBudget,
    verify_kept: Callable[[int, str, str], None] | None = None,
) -> None:
    """Relist a folder in selected-entry mode, at the end of its scan or at verification.

    Its kept names must still be present and, if it kept any, no recognized
    marker may now hide it.  Every other name must still be the entry the scan
    saw and did not keep, by its ``(dev, ino, file type)`` in *unkept*, or must
    now hold no selected entry.  At the end of a scan, like the strict
    end-of-folder relist, this bounds the folder's undetected window to the
    time between this relist and verification.  Verification passes an empty
    *unkept* and checks each kept name the listing has with
    ``verify_kept(descriptor, relative, name)``.
    """

    names = _relist_directory(
        descriptor,
        relative,
        selection=selection,
        budget=budget,
    )
    if names is None:
        if kept:
            raise _TreeChanged(relative)
        return
    current = set(names)
    for name in kept:
        if name not in current:
            raise _TreeChanged(f"{relative}/{name}" if relative else name)
    recorded = set(kept)
    for name in names:
        if name in recorded:
            if verify_kept is not None:
                verify_kept(descriptor, relative, name)
            continue
        examined = unkept.get(name)
        if examined is not None:
            try:
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                # _require_no_inputs decides what this failure means.
                pass
            else:
                if _stable_entry_identity(_stat_signature(metadata)) == examined:
                    continue
        _require_no_inputs(
            descriptor,
            name,
            relative=relative,
            selection=selection,
            budget=budget,
        )


def _relist_directory(
    descriptor: int,
    relative: str,
    *,
    selection: TreeSelection,
    budget: _CaptureBudget,
) -> tuple[str, ...] | None:
    """List a folder again as the scan lists it, or return ``None`` if a marker now hides it.

    Limit, name and permission failures are changes: the scan already passed
    this folder, so the retry's scan decides whether such a condition lasts.
    """

    try:
        if relative and selection.opaque_markers:
            return _opaque_marker(
                descriptor,
                relative,
                selection,
                budget=budget,
            )
        return _capture_directory_names(descriptor, budget=budget)
    except _PermissionDenied as error:
        raise _TreeChanged(error.relative) from error
    except (TreeSnapshotError, PermissionError) as error:
        raise _TreeChanged(relative) from error


def _require_no_inputs(
    descriptor: int,
    name: str,
    *,
    relative: str,
    selection: TreeSelection,
    budget: _CaptureBudget,
) -> None:
    """Prove that whatever is now at *name* in the folder *relative* holds no selected entry.

    Nothing is compared with the scan: a missing or unselected entry, a folder
    the scan does not descend into and a folder hidden by a recognized marker
    hold nothing; any other folder is walked.
    """

    child_relative = f"{relative}/{name}" if relative else name
    relative_path = PurePosixPath(child_relative)
    child_descriptor: int | None = None
    for _look in range(2):
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        except PermissionError as error:
            raise _TreeChanged(child_relative) from error
        if not stat.S_ISDIR(metadata.st_mode):
            if selection.include(relative_path, metadata.st_mode):
                raise _TreeChanged(child_relative)
            return
        if not selection.descend(relative_path):
            return
        try:
            child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
        except FileNotFoundError:
            return
        except NotADirectoryError:
            # Replaced by a non-directory since the stat: look at that instead.
            continue
        except PermissionError as error:
            raise _TreeChanged(child_relative) from error
        break
    if child_descriptor is None:
        raise _TreeChanged(child_relative)
    try:
        names = _relist_directory(
            child_descriptor,
            child_relative,
            selection=selection,
            budget=budget,
        )
        if names is None:
            return
        for child_name in names:
            _require_no_inputs(
                child_descriptor,
                child_name,
                relative=child_relative,
                selection=selection,
                budget=budget,
            )
    finally:
        close_quietly(child_descriptor)


def _read_file(
    parent_descriptor: int,
    name: str,
    expected: tuple[int, ...],
    *,
    relative: str,
    max_bytes: int | None,
    digest: bool = False,
) -> tuple[bytes, int]:
    """Read the regular file *name* in the directory *relative*."""

    descriptor: int | None = None
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _stat_signature(opened) != expected:
            raise _TreeChanged(f"{relative}/{name}" if relative else name)
        stream = os.fdopen(descriptor, "rb", buffering=0, closefd=False)
        try:
            data, size = _read_stream(stream, max_bytes, digest=digest)
        finally:
            stream.close()
        if (
            _stat_signature(os.fstat(descriptor)) != expected
            or _stat_signature(
                os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            )
            != expected
        ):
            raise _TreeChanged(f"{relative}/{name}" if relative else name)
        return data, size
    finally:
        if descriptor is not None:
            close_quietly(descriptor)


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


def _read_stream(stream, max_bytes: int | None, *, digest: bool) -> tuple[bytes, int]:
    """Read through ``max_bytes + 1`` bytes or EOF: the bytes, or their SHA-256 digest, and how many there were."""

    if not digest:
        data = stream.read() if max_bytes is None else _read_prefix(stream, max_bytes + 1)
        return data, len(data)
    hasher = hashlib.sha256()
    size = 0
    while max_bytes is None or size <= max_bytes:
        chunk = stream.read(_READ_CHUNK_BYTES if max_bytes is None else min(max_bytes + 1 - size, _READ_CHUNK_BYTES))
        if not chunk:
            break
        hasher.update(chunk)
        size += len(chunk)
    return hasher.digest(), size


def _verify_snapshot(
    root_descriptor: int,
    directories: list[_DirectoryRecord],
    entries: list[_EntryRecord],
    *,
    selection: TreeSelection,
) -> None:
    expected_directories = {record.relative: record for record in directories}
    expected_entries = {record.relative: record for record in entries}
    visited_directories: set[str] = set()
    visited_entries: set[str] = set()
    # Selected mode lists every recorded folder and walks every unrecorded
    # child, charged once against the capture limits for the whole pass.
    budget = _CaptureBudget(selection.limits)

    def verify_directory(descriptor: int, relative: str) -> None:
        expected = expected_directories.get(relative)
        if expected is None:
            raise _TreeChanged(relative)
        visited_directories.add(relative)
        if selection.unselected_may_change:
            _recheck_scanned_directory(
                descriptor,
                relative=relative,
                kept=expected.names,
                unkept={},
                selection=selection,
                budget=budget,
                # Passed as it is, not wrapped in a lambda or a partial, so each
                # folder level costs three Python frames and no C-level call,
                # which bounds how deep a tree can be.
                verify_kept=verify_recorded,
            )
            return
        names_match = _directory_names_match(
            descriptor,
            expected.names,
        )
        names = expected.names
        if (
            _stable_entry_identity(_stat_signature(os.fstat(descriptor)))
            != _stable_entry_identity(expected.identity)
            or not names_match
        ):
            raise _TreeChanged(relative)
        for name in names:
            verify_recorded(descriptor, relative, name)
        if (
            _stable_entry_identity(
                _stat_signature(os.fstat(descriptor))
            ) != _stable_entry_identity(expected.identity)
            or not _directory_names_match(
                descriptor,
                expected.names,
            )
        ):
            raise _TreeChanged(relative)

    def verify_recorded(descriptor: int, relative: str, name: str) -> None:
        child_relative = f"{relative}/{name}" if relative else name
        metadata = _lookup(descriptor, name, relative)
        directory = expected_directories.get(child_relative)
        if directory is None:
            entry = expected_entries.get(child_relative)
            if entry is None or (
                not entry.ignored and _stat_signature(metadata) != entry.identity
            ):
                raise _TreeChanged(child_relative)
            if entry.ignored and stat.S_IFMT(metadata.st_mode) != stat.S_IFMT(entry.identity[2]):
                raise _TreeChanged(child_relative)
            visited_entries.add(child_relative)
            return
        if not stat.S_ISDIR(metadata.st_mode) or _stable_entry_identity(
            _stat_signature(metadata)
        ) != _stable_entry_identity(directory.identity):
            raise _TreeChanged(child_relative)
        child_descriptor: int | None = None
        try:
            try:
                child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except PermissionError as error:
                raise _access_denied(descriptor, name, relative) from error
            opened_identity = _stat_signature(os.fstat(child_descriptor))
            if _stable_entry_identity(opened_identity) != _stable_entry_identity(
                directory.identity
            ):
                raise _TreeChanged(child_relative)
            verify_directory(child_descriptor, child_relative)
            final_identity = _stat_signature(_lookup(descriptor, name, relative))
            if _stable_entry_identity(final_identity) != _stable_entry_identity(
                directory.identity
            ):
                raise _TreeChanged(child_relative)
        finally:
            if child_descriptor is not None:
                close_quietly(child_descriptor)

    try:
        verify_directory(root_descriptor, "")
    finally:
        # verify_directory and verify_recorded hold each other through their
        # closures. Breaking that cycle lets reference counting free the
        # selection they hold once verification ends.
        verify_directory = None  # type: ignore[assignment]
    if visited_directories != set(expected_directories) or visited_entries != set(
        expected_entries
    ):
        raise _TreeChanged


def _capture_portable(
    root: Path,
    *,
    expected_identity: tuple[int, int],
    selection: TreeSelection,
    digests: bool,
) -> TreeSnapshot:
    """Best-effort double-captured fallback for read-only non-POSIX clients.

    Paths are re-resolved on every access, so this is not a generation
    boundary.  On Windows, path ``stat`` reports birth time as ``st_ctime_ns``;
    a directory junction swapped in for a subdirectory, read through, and
    swapped back out can pass both captures unnoticed if the parent's
    modification time is reset.  One swapped in for the root needs no reset,
    because the root's ancestors are compared only by identity.
    """

    if selection.unselected_may_change:
        # Comparing two whole captures is this path's only stability check, so
        # it cannot let unselected folders change between them.
        raise TreeSnapshotError(
            "selected-entry capture requires directory-descriptor traversal"
        )
    path_identities = _portable_directory_identities(root)
    root_before = os.lstat(root)
    if (
        not stat.S_ISDIR(root_before.st_mode)
        or _is_reparse_point(root_before)
        or (root_before.st_dev, root_before.st_ino) != expected_identity
    ):
        raise TreeSnapshotError("directory tree cannot be inspected safely")
    directories: list[str] = [""]
    files: list[tuple[str, bytes]] = []
    symlinks: list[tuple[str, str]] = []
    special: list[tuple[str, int]] = []
    omitted: list[tuple[str, str]] = []
    identities: list[tuple[str, tuple[int, ...]]] = [("", _stat_signature(root_before))]
    budget = _CaptureBudget(selection.limits)

    def visit(directory: Path, relative: str) -> None:
        try:
            names = _capture_directory_names(
                directory,
                budget=budget,
            )
        except PermissionError as error:
            raise _PermissionDenied(relative or ".") from error
        _tree_snapshot_checkpoint("after-directory-list", relative)
        for name in names:
            child_relative = f"{relative}/{name}" if relative else name
            path = directory / name
            metadata = os.lstat(path)
            relative_path = PurePosixPath(child_relative)
            if stat.S_ISDIR(metadata.st_mode) and not _is_reparse_point(metadata):
                if not selection.descend(relative_path):
                    if selection.record_omitted:
                        omitted.append((child_relative, "directory"))
                    continue
                directories.append(child_relative)
                identities.append((child_relative, _stat_signature(metadata)))
                visit(path, child_relative)
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
                    if selection.record_omitted:
                        omitted.append((child_relative, "file"))
                    continue
                before = _stat_signature(metadata)
                max_bytes = budget.file_read_limit(metadata.st_size)
                try:
                    data, size = _read_portable_file(
                        path,
                        before,
                        max_bytes=max_bytes,
                        digest=digests,
                    )
                except PermissionError as error:
                    raise _PermissionDenied(child_relative) from error
                budget.add_file_bytes(size)
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

    try:
        visit(root, "")
    finally:
        # visit holds itself through its closure. Breaking that cycle lets
        # reference counting free the capture state along with the snapshot.
        visit = None  # type: ignore[assignment]
    root_after = os.lstat(root)
    if (
        _stat_signature(root_before) != _stat_signature(root_after)
        or _portable_directory_identities(root) != path_identities
    ):
        raise TreeChangedError("directory tree changed while it was captured")
    return TreeSnapshot(
        directories=tuple(sorted(directories)),
        files=tuple(sorted(files)),
        symlinks=tuple(sorted(symlinks)),
        special=tuple(sorted(special)),
        omitted=tuple(sorted(omitted)),
        identities=tuple(sorted(identities)),
    )


def _read_portable_file(
    path: Path,
    expected: tuple[int, ...],
    *,
    max_bytes: int | None,
    digest: bool,
) -> tuple[bytes, int]:
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
            data, size = _read_stream(stream, max_bytes, digest=digest)
        finally:
            stream.close()
        final_opened = os.fstat(descriptor)
        final_named = os.lstat(path)
        if (
            _stat_signature(final_opened) != opened_signature
            or _stat_signature(final_named) != expected
        ):
            raise TreeChangedError("directory tree changed while it was captured")
        return data, size
    finally:
        if descriptor is not None:
            close_quietly(descriptor)


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


def _update_digest(digest, kind: bytes, relative: str, data: bytes) -> None:
    path = os.fsencode(relative)
    for field in (kind, path, data):
        digest.update(len(field).to_bytes(8, "big"))
        digest.update(field)
