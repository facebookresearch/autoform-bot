"""Capture a directory tree for internal consumers.

One walk reads the selected entries by path, never following a symbolic link,
and records each entry's ``stat`` signature and each folder's names.  A second
walk reads no bytes; it must find the same folders, names and signatures, and
any difference raises ``TreeChangedError``, which callers retry.
``TreeSelection`` says what must stay put.  ``BoundDirectoryTree`` holds the
root by its ``(dev, ino)`` and checks before and after each capture that the
root's path still names that directory.  The root's ancestors may be symbolic
links; the root and everything below it may not.

A capture is not one atomic generation.  An edit made after the first walk
looked at an entry and undone before the second walk reached it is not seen,
and coordinated moves between those looks can yield a set of entries that no
single state had.  A writer able to do that can edit the captured files as
well, so the capture does not defend against one.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

from ._directory_binding import close_quietly, lexical_absolute_path

_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)
_READ_CHUNK_BYTES = 1024 * 1024
_RETRYABLE_BIND_ERRNOS = frozenset(
    getattr(errno, name) for name in ("ENOENT", "ENOTDIR", "ELOOP", "ESTALE") if hasattr(errno, name)
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

    Every observed child counts as an entry, including omitted entries.  The
    capturing walk and the verifying walk each have their own budget under the
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
    on other mode bits.  This mode does not record omitted entries, and is the
    only one that takes ``opaque_markers``.
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
    """A marker that makes its containing directory opaque.

    The capture engine reads this surface, through ``max_bytes`` or whole when
    that is ``None``, before it visits any descendants.  A recognized marker is
    not kept: each later look recognizes it afresh.  A presence-only marker accepts any entry kind without reading
    it; this is for directory sentinels such as a nested checkout's ``.git``.
    """

    name: str
    max_bytes: int | None
    recognizes: Callable[[bytes], bool]
    presence_only: bool = False

    def __post_init__(self) -> None:
        if not _valid_name(self.name):
            raise ValueError("opaque marker name must be one portable path component")
        if not isinstance(self.presence_only, bool):
            raise ValueError("opaque marker presence_only must be a boolean")
        if self.max_bytes is not None and _invalid_limit(self.max_bytes):
            raise ValueError("opaque marker max_bytes must be a non-negative integer or None")


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
    directory: int | str | Path,
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


class BoundDirectoryTree:
    """A directory root held by its identity, which every capture rechecks."""

    def __init__(
        self,
        root: Path,
        *,
        expected_identity: tuple[int, int] | None = None,
        selection: TreeSelection = ALL_ENTRIES,
    ) -> None:
        self.root = lexical_absolute_path(root)
        self.selection = selection
        self._closed = False
        self._identity = _bind_root(self.root)
        if expected_identity is not None and self._identity != expected_identity:
            raise TreeChangedError("directory tree changed before it was captured")

    @property
    def identity(self) -> tuple[int, int]:
        if self._closed:
            raise TreeSnapshotError("directory tree binding is closed")
        return self._identity

    def verify(self) -> None:
        """Verify that the root's path still names the bound directory."""

        if self._closed:
            raise TreeSnapshotError("directory tree binding is closed")
        try:
            metadata = os.stat(self.root, follow_symlinks=False)
        except (OSError, ValueError) as error:
            raise TreeChangedError("directory tree changed while it was in use") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or _is_reparse_point(metadata)
            or (metadata.st_dev, metadata.st_ino) != self._identity
        ):
            raise TreeChangedError("directory tree changed while it was in use")

    def capture(self, *, digests: bool = False) -> TreeSnapshot:
        """Capture the bound tree once and verify it with a second walk.

        With *digests*, each file is hashed as it is read and only its SHA-256
        digest is kept, so memory does not grow with file sizes.
        """

        self.verify()
        try:
            snapshot = _capture(self.root, self._identity, self.selection, digests=digests)
        except (_PermissionDenied, _TreeChanged, OSError, RuntimeError) as error:
            raise _capture_failure(error) from error
        self.verify()
        return snapshot

    def close(self) -> None:
        self._closed = True


@contextmanager
def bind_directory_tree(
    root: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    selection: TreeSelection = ALL_ENTRIES,
) -> Iterator[BoundDirectoryTree]:
    """Hold *root* while callers capture and verify its content generation."""

    bound = BoundDirectoryTree(
        root,
        expected_identity=expected_identity,
        selection=selection,
    )
    try:
        yield bound
    finally:
        bound.close()


def _bind_root(root: Path) -> tuple[int, int]:
    """The ``(dev, ino)`` of the directory *root* names, which must not be a symbolic link."""

    try:
        metadata = os.stat(root, follow_symlinks=False)
    except OSError as error:
        # A concurrent rename or type change shows up as one of these.
        if error.errno in _RETRYABLE_BIND_ERRNOS:
            raise TreeChangedError("directory tree changed before it was captured") from error
        if isinstance(error, PermissionError):
            raise TreeSnapshotError(
                "directory tree cannot be inspected safely: permission denied while opening the directory root"
            ) from error
        raise TreeSnapshotError("directory tree cannot be inspected safely: directory root cannot be opened") from error
    except ValueError as error:
        raise TreeSnapshotError("directory tree cannot be inspected safely: directory root cannot be opened") from error
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
        raise TreeSnapshotError("directory tree cannot be inspected safely: directory root must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise TreeChangedError("directory tree changed before it was captured")
    return metadata.st_dev, metadata.st_ino


@dataclass(slots=True)
class _Walk:
    """What one walk saw: each recorded folder's signature and names, and each entry's signature.

    ``visited`` has every folder the walk listed, recorded or not, and
    ``hidden`` the folders a marker hid.  ``ignored`` keeps only the file type
    of an entry the selection leaves out, so a change to its contents is not a
    change to the tree.
    """

    directories: dict[str, tuple[tuple[int, ...], tuple[str, ...]]] = field(default_factory=dict)
    visited: dict[str, tuple[tuple[int, ...], tuple[str, ...]]] = field(default_factory=dict)
    hidden: set[str] = field(default_factory=set)
    entries: dict[str, tuple[int, ...]] = field(default_factory=dict)
    ignored: dict[str, int] = field(default_factory=dict)
    files: list[tuple[str, bytes]] = field(default_factory=list)
    symlinks: list[tuple[str, str]] = field(default_factory=list)
    special: list[tuple[str, int]] = field(default_factory=list)
    omitted: list[tuple[str, str]] = field(default_factory=list)


def _capture(
    root: Path,
    identity: tuple[int, int],
    selection: TreeSelection,
    *,
    digests: bool,
) -> TreeSnapshot:
    first = _walk(root, _root_signature(root, identity), selection, read=True, digests=digests)
    _tree_snapshot_checkpoint("before-final-verification", "")
    try:
        second = _walk(root, _root_signature(root, identity), selection, read=False, digests=False)
    except _PermissionDenied as error:
        # The first walk passed where the second was refused or ran over a
        # limit, so the tree changed; the retry's first walk decides whether
        # the condition lasts.
        raise _TreeChanged(error.relative) from error
    except (TreeSnapshotError, PermissionError) as error:
        raise _TreeChanged() from error
    changed = _first_difference(first, second, selected_only=selection.unselected_may_change)
    if changed is not None:
        raise _TreeChanged(changed)
    return TreeSnapshot(
        directories=tuple(sorted(first.directories)),
        files=tuple(sorted(first.files)),
        symlinks=tuple(sorted(first.symlinks)),
        special=tuple(sorted(first.special)),
        omitted=tuple(sorted(first.omitted)),
        identities=tuple(
            sorted(
                [(relative, signature) for relative, (signature, _names) in first.directories.items()]
                + list(first.entries.items())
            )
        ),
    )


def _root_signature(root: Path, identity: tuple[int, int]) -> tuple[int, ...]:
    metadata = os.stat(root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or _is_reparse_point(metadata)
        or (metadata.st_dev, metadata.st_ino) != identity
    ):
        raise _TreeChanged("")
    return _stat_signature(metadata)


def _walk(
    root: Path,
    root_signature: tuple[int, ...],
    selection: TreeSelection,
    *,
    read: bool,
    digests: bool,
) -> _Walk:
    """Walk *root* once; with *read*, keep the selected files' bytes and links' targets.

    By default every descended folder is recorded with all its names.  In
    selected-entry mode only the root and the folders that hold a recorded
    entry are, with only those entries' names, and an entry that vanishes
    before it is examined is absent.
    """

    selected_only = selection.unselected_may_change
    budget = _CaptureBudget(selection.limits)
    seen = _Walk()

    def visit(directory: str, relative: str, signature: tuple[int, ...]) -> bool:
        try:
            names = _capture_directory_names(directory, budget=budget)
        except PermissionError as error:
            raise _PermissionDenied(relative or ".") from error
        except FileNotFoundError:
            if selected_only and relative:
                return False
            raise
        # The listing must come from the folder its parent examined, not a link
        # or another folder put in its place since.  An unrecorded folder in
        # selected-entry mode may change, so only a recorded one is refused.
        try:
            listed = os.stat(directory, follow_symlinks=False)
        except FileNotFoundError:
            if selected_only and relative:
                return False
            raise
        moved = (
            not stat.S_ISDIR(listed.st_mode)
            or _is_reparse_point(listed)
            or _stable_entry_identity(_stat_signature(listed)) != _stable_entry_identity(signature)
        )
        if moved and not selected_only:
            raise _TreeChanged(relative)
        # The root is the directory the caller asked for: a marker in it does
        # not turn the whole input into generated output.
        if relative and selection.opaque_markers and _hidden_by_marker(directory, relative, names, selection, budget):
            seen.hidden.add(relative)
            return False
        _tree_snapshot_checkpoint("after-directory-list", relative)
        kept: list[str] = []
        for name in names:
            child_relative = _child(relative, name)
            child = os.path.join(directory, name)
            try:
                metadata = os.stat(child, follow_symlinks=False)
            except FileNotFoundError:
                if selected_only:
                    continue
                raise
            except PermissionError as error:
                raise _PermissionDenied(relative or ".") from error
            mode = metadata.st_mode
            link = stat.S_ISLNK(mode) or _is_reparse_point(metadata)
            child_signature = _stat_signature(metadata)
            relative_path = PurePosixPath(child_relative)
            if stat.S_ISDIR(mode) and not link:
                if selection.descend(relative_path):
                    if visit(child, child_relative, child_signature):
                        kept.append(name)
                    continue
                kind = "directory"
            elif not selection.include(relative_path, mode):
                kind = "symlink" if link else "file" if stat.S_ISREG(mode) else "special"
            else:
                if read:
                    try:
                        if link:
                            seen.symlinks.append((child_relative, _read_link(child, child_relative, child_signature)))
                        elif stat.S_ISREG(mode):
                            max_bytes = budget.file_read_limit(metadata.st_size)
                            data, size = _read_file(
                                child, child_relative, child_signature, max_bytes=max_bytes, digest=digests
                            )
                            budget.add_file_bytes(size)
                            seen.files.append((child_relative, data))
                        else:
                            seen.special.append((child_relative, stat.S_IFMT(mode)))
                    except FileNotFoundError:
                        if selected_only:
                            continue
                        raise
                seen.entries[child_relative] = child_signature
                kept.append(name)
                continue
            if not selected_only:
                seen.ignored[child_relative] = stat.S_IFMT(mode)
                if selection.record_omitted:
                    seen.omitted.append((child_relative, kind))
        if moved and kept:
            raise _TreeChanged(relative)
        recorded_names = tuple(kept) if selected_only else names
        seen.visited[relative] = (signature, recorded_names)
        if selected_only and relative and not kept:
            return False
        seen.directories[relative] = (signature, recorded_names)
        return True

    try:
        # Plain strings, not Path objects: on a large tree, building and
        # rendering a Path per entry costs more than the stat calls.
        visit(os.fspath(root), "", root_signature)
    finally:
        # visit holds itself through its closure. Breaking that cycle lets
        # reference counting free the captured bytes along with the snapshot.
        visit = None  # type: ignore[assignment]
    return seen


def _first_difference(first: _Walk, second: _Walk, *, selected_only: bool) -> str | None:
    """The path that names the first disagreement between two walks, or ``None``.

    A folder whose listing changed is named by default.  In selected-entry
    mode the change itself is named: a recorded entry that is gone, the first
    selected entry under a name that now holds one, or the deepest recorded
    folder that a marker now hides or that is gone.
    """

    for relative, (signature, names) in sorted(first.directories.items()):
        other = second.visited.get(relative)
        if other is None or relative in second.hidden:
            return relative
        if _stable_entry_identity(signature) != _stable_entry_identity(other[0]):
            return relative
        if names == other[1]:
            continue
        if not selected_only:
            return relative
        # A recorded folder that lost its entries is examined at its own turn.
        removed = [name for name in sorted(set(names) - set(other[1])) if _child(relative, name) not in first.directories]
        if removed:
            return _child(relative, removed[0])
        added = sorted(set(other[1]) - set(names))
        if added:
            found = _child(relative, added[0])
            while found in second.directories and second.directories[found][1]:
                found = _child(found, second.directories[found][1][0])
            return found
    for recorded, other_entries in ((first.entries, second.entries), (first.ignored, second.ignored)):
        for relative, value in sorted(recorded.items()):
            if other_entries.get(relative) != value:
                return relative
    return None


def _child(relative: str, name: str) -> str:
    return f"{relative}/{name}" if relative else name


def _hidden_by_marker(
    directory: str,
    relative: str,
    names: tuple[str, ...],
    selection: TreeSelection,
    budget: _CaptureBudget,
) -> bool:
    """Whether a recognized marker among *names* hides the folder *relative*."""

    by_folded_name: dict[str, list[str]] = {}
    for name in names:
        by_folded_name.setdefault(_normalized_name(name), []).append(name)
    for marker in selection.opaque_markers:
        matches = by_folded_name.get(_normalized_name(marker.name), [])
        if len(matches) != 1:
            continue
        path = os.path.join(directory, matches[0])
        marker_relative = f"{relative}/{matches[0]}"
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except PermissionError as error:
            raise _PermissionDenied(relative) from error
        if marker.presence_only:
            return True
        if not stat.S_ISREG(metadata.st_mode):
            continue
        max_bytes = budget.file_read_limit(metadata.st_size, marker.max_bytes)
        try:
            data, size = _read_file(path, marker_relative, _stat_signature(metadata), max_bytes=max_bytes)
        except FileNotFoundError:
            continue
        if (marker.max_bytes is None or size <= marker.max_bytes) and marker.recognizes(data):
            budget.add_file_bytes(size)
            return True
    return False


def _read_file(
    path: str | Path,
    relative: str,
    expected: tuple[int, ...],
    *,
    max_bytes: int | None,
    digest: bool = False,
) -> tuple[bytes, int]:
    """Read the regular file at *path*, which must keep the signature *expected* throughout."""

    descriptor: int | None = None
    try:
        descriptor = os.open(path, _FILE_FLAGS)
        opened = os.fstat(descriptor)
        opened_signature = _stat_signature(opened)
        if not stat.S_ISREG(opened.st_mode) or _cross_interface_signature(
            opened_signature
        ) != _cross_interface_signature(expected):
            raise _TreeChanged(relative)
        stream = os.fdopen(descriptor, "rb", buffering=0, closefd=False)
        try:
            data, size = _read_stream(stream, max_bytes, digest=digest)
        finally:
            stream.close()
        if (
            _stat_signature(os.fstat(descriptor)) != opened_signature
            or _stat_signature(os.stat(path, follow_symlinks=False)) != expected
        ):
            raise _TreeChanged(relative)
        return data, size
    except PermissionError as error:
        # Refused, or gone by the time it was refused: only the latter is a change.
        try:
            os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            raise _TreeChanged(relative) from error
        raise _PermissionDenied(relative) from error
    finally:
        if descriptor is not None:
            close_quietly(descriptor)


def _read_link(path: str | Path, relative: str, expected: tuple[int, ...]) -> str:
    try:
        target = os.readlink(path)
    except ValueError as error:
        raise TreeSnapshotError("directory tree contains an unsupported reparse point") from error
    except PermissionError as error:
        raise _PermissionDenied(relative) from error
    if _stat_signature(os.stat(path, follow_symlinks=False)) != expected:
        raise _TreeChanged(relative)
    return target


class _TreeChanged(Exception):
    """The tree changed between the capturing walk and the verifying walk."""

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
    for part in (kind, path, data):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
