"""Crash-recoverable population of an existing empty current directory.

The absent-target creator can publish one directory with one rename.  An
existing directory cannot use that trick without changing its inode, so this
module publishes one top-level entry at a time behind a durable write-ahead
journal.  Every operation beneath the target is descriptor-relative.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - imported on unsupported Windows only
    fcntl = None  # type: ignore[assignment]


MARKER = ".autoform-project-new"
STAGE = "project"
METADATA = "transaction.json"
MANIFEST = "manifest.json"
JOURNAL = "journal.jsonl"
SCHEMA = 1
CONTROL_FILE_LIMIT = 32 * 1024 * 1024
_CHUNK = 1024 * 1024
_DIRECTORY_MODE = 0o700
_CONTROL_MODE = 0o600
_RECOVERY_MESSAGE = (
    "An interrupted or changed project transaction requires recovery; "
    "no unverified data was removed."
)
_SAFETY_MESSAGE = (
    "This platform cannot create the project with the required path and "
    "durability safety."
)
_LINUX_LOCAL_FILESYSTEMS = {
    0xEF53,  # ext2/ext3/ext4
    0x58465342,  # XFS
    0x9123683E,  # Btrfs
    0x01021994,  # tmpfs, useful for process-crash tests
    0x794C7630,  # overlayfs with a local upper layer
}
_DARWIN_LOCAL_FILESYSTEMS = {"apfs", "hfs"}


class InPlaceCreateError(ValueError):
    """The current directory could not be populated safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class InPlaceResult:
    written: tuple[str, ...]
    workflows_pinned: bool


@dataclass(frozen=True, slots=True)
class _Node:
    path: str
    kind: str
    dev: int
    ino: int
    mode: int
    nlink: int
    size: int | None
    sha256: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "dev": self.dev,
            "ino": self.ino,
            "kind": self.kind,
            "mode": self.mode,
            "nlink": self.nlink,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }

    def content_key(self) -> tuple[object, ...]:
        return (self.path, self.kind, self.mode, self.size, self.sha256)


@dataclass(frozen=True, slots=True)
class _Control:
    name: str
    descriptor: int
    dev: int
    ino: int

    def as_dict(self) -> dict[str, int]:
        return {"dev": self.dev, "ino": self.ino, "mode": _CONTROL_MODE}


@dataclass(slots=True)
class _Transaction:
    transaction_id: str
    marker_descriptor: int
    marker_identity: tuple[int, int]
    stage_descriptor: int | None
    stage_identity: tuple[int, int]
    metadata: _Control | None
    manifest_control: _Control
    journal_control: _Control | None
    manifest: tuple[_Node, ...]
    manifest_document: dict[str, object]
    manifest_checksum: str
    journal: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class _JournalState:
    published: tuple[str, ...]
    pending: str | None
    committed: bool
    rollback_started: bool


class _LinuxStatFs(ctypes.Structure):
    _fields_ = [
        ("f_type", ctypes.c_long),
        ("f_bsize", ctypes.c_long),
        ("f_blocks", ctypes.c_ulong),
        ("f_bfree", ctypes.c_ulong),
        ("f_bavail", ctypes.c_ulong),
        ("f_files", ctypes.c_ulong),
        ("f_ffree", ctypes.c_ulong),
        ("f_fsid", ctypes.c_int * 2),
        ("f_namelen", ctypes.c_long),
        ("f_frsize", ctypes.c_long),
        ("f_flags", ctypes.c_long),
        ("f_spare", ctypes.c_long * 4),
    ]


class _DarwinStatFs(ctypes.Structure):
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_reserved", ctypes.c_uint32 * 8),
    ]


def create_in_current_directory(
    *,
    package: str,
    release: str,
    autoform_source: str,
    autoform_ref: str,
    build: Callable[[Path], tuple[tuple[str, ...], bool]],
    validate: Callable[[Path], None],
) -> InPlaceResult:
    """Build outside the target, then populate the existing current directory."""

    parent_descriptor: int | None = None
    target_descriptor: int | None = None
    transaction: _Transaction | None = None
    try:
        (
            parent_descriptor,
            target_name,
            target_descriptor,
            target_identity,
            target_mode,
        ) = _open_current_target()
        _preflight(target_descriptor)
        _lock_target(target_descriptor)
        _require_target(
            parent_descriptor,
            target_name,
            target_descriptor,
            target_identity,
            target_mode,
        )
        with tempfile.TemporaryDirectory(prefix="autoform-project-render-") as scratch:
            rendered = Path(scratch).resolve() / STAGE
            rendered.mkdir(mode=_DIRECTORY_MODE)
            written, workflows_pinned = build(rendered)
            source_parent_descriptor = _open_absolute_directory(rendered.parent)
            source_descriptor: int | None = None
            try:
                source_descriptor = _open_directory(source_parent_descriptor, rendered.name)
                source_metadata = os.fstat(source_descriptor)
                source_identity = _stat_identity(source_metadata)
                source_mode = stat.S_IMODE(source_metadata.st_mode)
                _require_directory_entry(
                    source_parent_descriptor,
                    rendered.name,
                    source_descriptor,
                    source_identity,
                    source_mode,
                )
                before_validation = _snapshot_tree(source_descriptor)
                _require_expected_files(before_validation, written)
                validate(rendered)
                _require_directory_entry(
                    source_parent_descriptor,
                    rendered.name,
                    source_descriptor,
                    source_identity,
                    source_mode,
                )
                if _snapshot_tree(source_descriptor) != before_validation:
                    raise InPlaceCreateError(
                        "project-create-validation-failed",
                        "The staged project changed while it was being validated.",
                    )
                invocation = {
                    "autoform_ref": autoform_ref,
                    "autoform_source": autoform_source,
                    "package": package,
                    "release": release,
                    "workflows_pinned": workflows_pinned,
                    "written": list(written),
                }
                _require_target(
                    parent_descriptor,
                    target_name,
                    target_descriptor,
                    target_identity,
                    target_mode,
                )
                names = set(_list_directory(target_descriptor))
                if not names:
                    transaction = _start_transaction(
                        target_descriptor,
                        target_identity,
                        target_mode,
                        source_descriptor,
                        before_validation,
                        invocation,
                    )
                elif MARKER in names:
                    transaction, rollback_pending = _load_transaction(
                        target_descriptor,
                        target_identity,
                        target_mode,
                        before_validation,
                        invocation,
                    )
                    if rollback_pending:
                        if transaction.stage_descriptor is None:
                            _cleanup_marker(
                                parent_descriptor,
                                target_name,
                                target_descriptor,
                                target_identity,
                                target_mode,
                                transaction,
                                expected_roots=set(),
                            )
                        elif not _rollback(
                            parent_descriptor,
                            target_name,
                            target_descriptor,
                            target_identity,
                            target_mode,
                            transaction,
                        ):
                            raise _recovery_required()
                        _close_transaction(transaction)
                        transaction = None
                        _require_target(
                            parent_descriptor,
                            target_name,
                            target_descriptor,
                            target_identity,
                            target_mode,
                        )
                        if _list_directory(target_descriptor):
                            raise _recovery_required()
                        transaction = _start_transaction(
                            target_descriptor,
                            target_identity,
                            target_mode,
                            source_descriptor,
                            before_validation,
                            invocation,
                        )
                elif _tree_has_same_content(target_descriptor, before_validation):
                    return InPlaceResult(written, workflows_pinned)
                else:
                    raise InPlaceCreateError(
                        "project-target-not-empty",
                        "The current directory must be completely empty before project creation.",
                    )

                assert transaction is not None
                try:
                    _publish(
                        parent_descriptor,
                        target_name,
                        target_descriptor,
                        target_identity,
                        target_mode,
                        transaction,
                    )
                    _finish(
                        parent_descriptor,
                        target_name,
                        target_descriptor,
                        target_identity,
                        target_mode,
                        transaction,
                    )
                except InPlaceCreateError as error:
                    if error.code in {
                        "project-recovery-required",
                        "project-target-changed",
                        "project-target-not-empty",
                    }:
                        raise
                    if not _rollback(
                        parent_descriptor,
                        target_name,
                        target_descriptor,
                        target_identity,
                        target_mode,
                        transaction,
                    ):
                        raise _recovery_required() from None
                    raise
                except OSError:
                    if not _rollback(
                        parent_descriptor,
                        target_name,
                        target_descriptor,
                        target_identity,
                        target_mode,
                        transaction,
                    ):
                        raise _recovery_required() from None
                    raise InPlaceCreateError(
                        "project-create-failed",
                        "Project creation failed; no project was created.",
                    ) from None
                return InPlaceResult(written, workflows_pinned)
            finally:
                if source_descriptor is not None:
                    os.close(source_descriptor)
                os.close(source_parent_descriptor)
    finally:
        if transaction is not None:
            _close_transaction(transaction)
        if target_descriptor is not None:
            os.close(target_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _checkpoint(name: str) -> None:
    """A no-op boundary used by process-crash tests."""


def _recovery_required() -> InPlaceCreateError:
    return InPlaceCreateError("project-recovery-required", _RECOVERY_MESSAGE)


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_absolute_directory(path: Path) -> int:
    absolute = path.resolve(strict=True)
    descriptor = os.open(absolute.anchor, _directory_flags())
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_directory(parent_descriptor: int, name: str) -> int:
    return os.open(name, _directory_flags(), dir_fd=parent_descriptor)


def _list_directory(directory_descriptor: int) -> list[str]:
    """List through a fresh descriptor so a prior scan cannot leave it at EOF."""

    fresh = _open_directory(directory_descriptor, ".")
    try:
        return os.listdir(fresh)
    finally:
        os.close(fresh)


def _open_current_target() -> tuple[int, str, int, tuple[int, int], int]:
    try:
        target_descriptor = os.open(".", _directory_flags())
    except (AttributeError, OSError):
        raise InPlaceCreateError(
            "project-create-safety-unavailable", _SAFETY_MESSAGE
        ) from None
    try:
        current = Path.cwd()
        if current.parent == current or not current.name:
            raise InPlaceCreateError(
                "project-target-invalid",
                "The filesystem root cannot be used as a project target.",
            )
        parent_descriptor = _open_absolute_directory(current.parent)
        metadata = os.fstat(target_descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        mode = stat.S_IMODE(metadata.st_mode)
        try:
            _require_target(
                parent_descriptor,
                current.name,
                target_descriptor,
                identity,
                mode,
            )
        except BaseException:
            os.close(parent_descriptor)
            raise
    except BaseException:
        os.close(target_descriptor)
        raise
    return parent_descriptor, current.name, target_descriptor, identity, mode


def _require_target(
    parent_descriptor: int,
    target_name: str,
    target_descriptor: int,
    identity: tuple[int, int],
    mode: int,
) -> None:
    try:
        opened = os.fstat(target_descriptor)
        named = os.stat(
            target_name, dir_fd=parent_descriptor, follow_symlinks=False
        )
    except OSError:
        raise InPlaceCreateError(
            "project-target-changed",
            "The current directory changed while the project was being created.",
        ) from None
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or (opened.st_dev, opened.st_ino) != identity
        or (named.st_dev, named.st_ino) != identity
        or stat.S_IMODE(opened.st_mode) != mode
        or stat.S_IMODE(named.st_mode) != mode
    ):
        raise InPlaceCreateError(
            "project-target-changed",
            "The current directory changed while the project was being created.",
        )


def _preflight(target_descriptor: int) -> None:
    required = (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_NONBLOCK")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and os.mkdir in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
        and fcntl is not None
    )
    if sys.platform not in {"linux", "darwin"} or not required:
        raise InPlaceCreateError("project-create-safety-unavailable", _SAFETY_MESSAGE)
    if not _filesystem_supported(target_descriptor) or _noreplace_function() is None:
        raise InPlaceCreateError("project-create-safety-unavailable", _SAFETY_MESSAGE)
    try:
        flags = os.fstatvfs(target_descriptor).f_flag
        if flags & getattr(os, "ST_RDONLY", 1):
            raise OSError(errno.EROFS, "read-only filesystem")
        _list_directory(target_descriptor)
        os.fsync(target_descriptor)
    except OSError:
        raise InPlaceCreateError("project-create-safety-unavailable", _SAFETY_MESSAGE) from None


def _filesystem_supported(descriptor: int) -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.fstatfs
    except (AttributeError, OSError):
        return False
    if sys.platform == "linux":
        value = _LinuxStatFs()
        function.argtypes = [ctypes.c_int, ctypes.POINTER(_LinuxStatFs)]
        function.restype = ctypes.c_int
        if function(descriptor, ctypes.byref(value)) != 0:
            return False
        bits = ctypes.sizeof(ctypes.c_long) * 8
        filesystem_type = int(value.f_type) & ((1 << bits) - 1)
        return filesystem_type in _LINUX_LOCAL_FILESYSTEMS
    if sys.platform == "darwin":
        value = _DarwinStatFs()
        function.argtypes = [ctypes.c_int, ctypes.POINTER(_DarwinStatFs)]
        function.restype = ctypes.c_int
        if function(descriptor, ctypes.byref(value)) != 0:
            return False
        filesystem_type = bytes(value.f_fstypename).split(b"\0", 1)[0]
        try:
            name = filesystem_type.decode("ascii")
        except UnicodeDecodeError:
            return False
        return name in _DARWIN_LOCAL_FILESYSTEMS
    return False


def _lock_target(target_descriptor: int) -> None:
    assert fcntl is not None
    try:
        fcntl.flock(target_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise InPlaceCreateError(
            "project-target-not-empty",
            "Another project creation transaction is active in this directory.",
        ) from None
    except OSError:
        raise InPlaceCreateError("project-create-safety-unavailable", _SAFETY_MESSAGE) from None


def _noreplace_function() -> tuple[Any, int] | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        flag = 0x00000004
    elif sys.platform == "linux" and hasattr(libc, "renameat2"):
        function = libc.renameat2
        flag = 1
    else:
        return None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return function, flag


def _rename_noreplace(
    source_parent: int, source: str, target_parent: int, target: str
) -> None:
    implementation = _noreplace_function()
    if implementation is None:
        raise InPlaceCreateError("project-create-safety-unavailable", _SAFETY_MESSAGE)
    function, flag = implementation
    result = function(
        source_parent,
        os.fsencode(source),
        target_parent,
        os.fsencode(target),
        flag,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, os.strerror(error), target)
    if error in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL, errno.EXDEV}:
        raise InPlaceCreateError("project-create-safety-unavailable", _SAFETY_MESSAGE)
    raise OSError(error, os.strerror(error), target)


def _snapshot_tree(directory_descriptor: int) -> tuple[_Node, ...]:
    nodes: list[_Node] = []
    _snapshot_children(directory_descriptor, "", nodes)
    return tuple(nodes)


def _snapshot_children(
    directory_descriptor: int, prefix: str, nodes: list[_Node]
) -> None:
    before_names = sorted(_list_directory(directory_descriptor))
    for name in before_names:
        if not name or name in {".", ".."} or "/" in name or "\0" in name:
            raise OSError(errno.EINVAL, "unsafe directory entry")
        path = f"{prefix}/{name}" if prefix else name
        metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = _open_directory(directory_descriptor, name)
            try:
                opened = os.fstat(child)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or _stat_identity(opened) != _stat_identity(metadata)
                ):
                    raise OSError(errno.ESTALE, "directory changed during snapshot")
                node = _node_from_directory(path, opened)
                nodes.append(node)
                _snapshot_children(child, path, nodes)
                current = os.fstat(child)
                if _directory_signature(current) != _directory_signature(opened):
                    raise OSError(errno.ESTALE, "directory changed during snapshot")
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            nodes.append(_snapshot_file(directory_descriptor, name, path, metadata))
        else:
            raise InPlaceCreateError(
                "project-create-validation-failed",
                "The staged project contains an unsupported filesystem entry.",
            )
    if sorted(_list_directory(directory_descriptor)) != before_names:
        raise OSError(errno.ESTALE, "directory changed during snapshot")


def _snapshot_file(
    parent_descriptor: int,
    name: str,
    path: str,
    metadata: os.stat_result,
) -> _Node:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or _file_signature(before) != _file_signature(metadata)
            or before.st_nlink != 1
        ):
            raise OSError(errno.ESTALE, "file changed during snapshot")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _CHUNK):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _file_signature(after) != _file_signature(before):
            raise OSError(errno.ESTALE, "file changed during snapshot")
        return _Node(
            path=path,
            kind="file",
            dev=after.st_dev,
            ino=after.st_ino,
            mode=stat.S_IMODE(after.st_mode),
            nlink=after.st_nlink,
            size=after.st_size,
            sha256=digest.hexdigest(),
        )
    finally:
        os.close(descriptor)


def _node_from_directory(path: str, metadata: os.stat_result) -> _Node:
    return _Node(
        path=path,
        kind="directory",
        dev=metadata.st_dev,
        ino=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        nlink=metadata.st_nlink,
        size=None,
        sha256=None,
    )


def _stat_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _directory_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
    )


def _file_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_expected_files(nodes: Sequence[_Node], written: Sequence[str]) -> None:
    actual_files = {node.path for node in nodes if node.kind == "file"}
    expected_files = set(written)
    if actual_files != expected_files or MARKER in {
        node.path.split("/", 1)[0] for node in nodes
    }:
        raise InPlaceCreateError(
            "project-create-validation-failed",
            "The staged project did not match the files reported by the project builder.",
        )


def _copy_tree(
    source_descriptor: int,
    destination_descriptor: int,
    expected: Sequence[_Node],
) -> None:
    expected_by_path = {node.path: node for node in expected}
    _copy_children(source_descriptor, destination_descriptor, "", expected_by_path)


def _copy_children(
    source_descriptor: int,
    destination_descriptor: int,
    prefix: str,
    expected: Mapping[str, _Node],
) -> None:
    names = sorted(_list_directory(source_descriptor))
    for name in names:
        path = f"{prefix}/{name}" if prefix else name
        node = expected.get(path)
        if node is None:
            raise OSError(errno.ESTALE, "source tree changed during copy")
        metadata = os.stat(name, dir_fd=source_descriptor, follow_symlinks=False)
        if node.kind == "directory":
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or _stat_identity(metadata) != (node.dev, node.ino)
            ):
                raise OSError(errno.ESTALE, "source directory changed during copy")
            os.mkdir(name, mode=_DIRECTORY_MODE, dir_fd=destination_descriptor)
            source_child = _open_directory(source_descriptor, name)
            destination_child = _open_directory(destination_descriptor, name)
            try:
                if _stat_identity(os.fstat(source_child)) != (node.dev, node.ino):
                    raise OSError(errno.ESTALE, "source directory changed during copy")
                _copy_children(source_child, destination_child, path, expected)
                os.fchmod(destination_child, node.mode)
                os.fsync(destination_child)
            finally:
                os.close(destination_child)
                os.close(source_child)
        elif node.kind == "file":
            _copy_file(
                source_descriptor,
                destination_descriptor,
                name,
                node,
            )
        else:  # pragma: no cover - manifest validation rejects this
            raise OSError(errno.EINVAL, "unsupported staged entry")
    if sorted(_list_directory(source_descriptor)) != names:
        raise OSError(errno.ESTALE, "source tree changed during copy")
    os.fsync(destination_descriptor)


def _copy_file(
    source_parent: int,
    destination_parent: int,
    name: str,
    expected: _Node,
) -> None:
    source_flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    source = os.open(name, source_flags, dir_fd=source_parent)
    destination: int | None = None
    try:
        before = os.fstat(source)
        if (
            not stat.S_ISREG(before.st_mode)
            or _stat_identity(before) != (expected.dev, expected.ino)
            or before.st_nlink != 1
        ):
            raise OSError(errno.ESTALE, "source file changed during copy")
        destination = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            _CONTROL_MODE,
            dir_fd=destination_parent,
        )
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(source, _CHUNK):
            digest.update(chunk)
            size += len(chunk)
            _write_all(destination, chunk)
        after = os.fstat(source)
        if (
            _file_signature(after) != _file_signature(before)
            or size != expected.size
            or digest.hexdigest() != expected.sha256
        ):
            raise OSError(errno.ESTALE, "source file changed during copy")
        os.fchmod(destination, expected.mode)
        os.fsync(destination)
    finally:
        if destination is not None:
            os.close(destination)
        os.close(source)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "short filesystem write")
        view = view[written:]


def _same_content(left: Sequence[_Node], right: Sequence[_Node]) -> bool:
    return tuple(node.content_key() for node in left) == tuple(
        node.content_key() for node in right
    )


def _tree_matches(directory_descriptor: int, expected: Sequence[_Node]) -> bool:
    try:
        return _snapshot_tree(directory_descriptor) == tuple(expected)
    except (InPlaceCreateError, OSError):
        return False


def _tree_has_same_content(
    directory_descriptor: int, expected: Sequence[_Node]
) -> bool:
    try:
        return _same_content(_snapshot_tree(directory_descriptor), expected)
    except (InPlaceCreateError, OSError):
        return False


def _entry_matches(
    parent_descriptor: int, name: str, expected: Sequence[_Node]
) -> bool:
    subtree = tuple(
        node
        for node in expected
        if node.path == name or node.path.startswith(f"{name}/")
    )
    if not subtree:
        return False
    adjusted = tuple(
        _Node(
            path=node.path,
            kind=node.kind,
            dev=node.dev,
            ino=node.ino,
            mode=node.mode,
            nlink=node.nlink,
            size=node.size,
            sha256=node.sha256,
        )
        for node in subtree
    )
    actual: list[_Node] = []
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = _open_directory(parent_descriptor, name)
            try:
                actual.append(_node_from_directory(name, os.fstat(child)))
                _snapshot_children(child, name, actual)
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            actual.append(_snapshot_file(parent_descriptor, name, name, metadata))
        else:
            return False
    except (InPlaceCreateError, OSError):
        return False
    return tuple(actual) == adjusted


def _entry_is_owned_remainder(
    parent_descriptor: int, name: str, expected: Sequence[_Node]
) -> bool:
    index = {node.path: node for node in expected}
    try:
        return _node_is_owned_remainder(parent_descriptor, name, index)
    except (InPlaceCreateError, OSError):
        return False


def _node_is_owned_remainder(
    parent_descriptor: int, path: str, expected: Mapping[str, _Node]
) -> bool:
    node = expected.get(path)
    if node is None:
        return False
    name = path.rsplit("/", 1)[-1]
    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if node.kind == "file":
        return _snapshot_file(parent_descriptor, name, path, metadata) == node
    if (
        node.kind != "directory"
        or not stat.S_ISDIR(metadata.st_mode)
        or _stat_identity(metadata) != (node.dev, node.ino)
        or stat.S_IMODE(metadata.st_mode) != node.mode
    ):
        return False
    child = _open_directory(parent_descriptor, name)
    try:
        opened = os.fstat(child)
        if (
            _stat_identity(opened) != (node.dev, node.ino)
            or stat.S_IMODE(opened.st_mode) != node.mode
        ):
            return False
        actual_names = set(_list_directory(child))
        expected_names = {
            candidate.path.rsplit("/", 1)[-1]
            for candidate in expected.values()
            if candidate.path.startswith(f"{path}/")
            and "/" not in candidate.path[len(path) + 1 :]
        }
        if not actual_names.issubset(expected_names):
            return False
        if not all(
            _node_is_owned_remainder(child, f"{path}/{child_name}", expected)
            for child_name in actual_names
        ):
            return False
        current = os.fstat(child)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        return (
            _stat_identity(current) == (node.dev, node.ino)
            and _stat_identity(named) == (node.dev, node.ino)
            and stat.S_IMODE(current.st_mode) == node.mode
            and stat.S_IMODE(named.st_mode) == node.mode
        )
    finally:
        os.close(child)


def _exists(parent_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _create_control(marker_descriptor: int, name: str) -> _Control:
    descriptor = os.open(
        name,
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0),
        _CONTROL_MODE,
        dir_fd=marker_descriptor,
    )
    os.fchmod(descriptor, _CONTROL_MODE)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise OSError(errno.ESTALE, "control file changed")
    return _Control(name, descriptor, metadata.st_dev, metadata.st_ino)


def _open_control(
    marker_descriptor: int,
    name: str,
    expected: Mapping[str, object] | None = None,
) -> _Control:
    descriptor = os.open(
        name,
        os.O_RDWR
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=marker_descriptor,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != _CONTROL_MODE
    ):
        os.close(descriptor)
        raise _recovery_required()
    if expected is not None and (
        metadata.st_dev != expected.get("dev")
        or metadata.st_ino != expected.get("ino")
        or stat.S_IMODE(metadata.st_mode) != expected.get("mode")
    ):
        os.close(descriptor)
        raise _recovery_required()
    return _Control(name, descriptor, metadata.st_dev, metadata.st_ino)


def _control_matches(marker_descriptor: int, control: _Control) -> bool:
    try:
        opened = os.fstat(control.descriptor)
        named = os.stat(
            control.name, dir_fd=marker_descriptor, follow_symlinks=False
        )
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(named.st_mode)
        and opened.st_nlink == 1
        and named.st_nlink == 1
        and stat.S_IMODE(opened.st_mode) == _CONTROL_MODE
        and stat.S_IMODE(named.st_mode) == _CONTROL_MODE
        and _stat_identity(opened) == (control.dev, control.ino)
        and _stat_identity(named) == (control.dev, control.ino)
    )


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _write_control(control: _Control, document: object) -> None:
    payload = _canonical_json(document)
    if len(payload) > CONTROL_FILE_LIMIT:
        raise OSError(errno.EFBIG, "control file too large")
    os.ftruncate(control.descriptor, 0)
    os.lseek(control.descriptor, 0, os.SEEK_SET)
    _write_all(control.descriptor, payload)
    os.fsync(control.descriptor)


def _strict_json(payload: bytes) -> object:
    if not _json_depth_within_limit(payload):
        raise ValueError("JSON nesting is too deep")

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(
        payload,
        object_pairs_hook=object_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"invalid JSON constant {value}")
        ),
    )


def _json_depth_within_limit(payload: bytes, *, limit: int = 64) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in payload:
        if in_string:
            if escaped:
                escaped = False
            elif byte == ord("\\"):
                escaped = True
            elif byte == ord('"'):
                in_string = False
            continue
        if byte == ord('"'):
            in_string = True
        elif byte in {ord("["), ord("{")}:
            depth += 1
            if depth > limit:
                return False
        elif byte in {ord("]"), ord("}")}:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


def _read_control_bytes(control: _Control) -> bytes:
    before = os.fstat(control.descriptor)
    if before.st_size > CONTROL_FILE_LIMIT:
        raise _recovery_required()
    os.lseek(control.descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = CONTROL_FILE_LIMIT + 1
    while remaining:
        chunk = os.read(control.descriptor, min(_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    after = os.fstat(control.descriptor)
    if (
        len(payload) > CONTROL_FILE_LIMIT
        or _file_signature(after) != _file_signature(before)
        or len(payload) != after.st_size
    ):
        raise _recovery_required()
    return payload


def _start_transaction(
    target_descriptor: int,
    target_identity: tuple[int, int],
    target_mode: int,
    source_descriptor: int,
    source_manifest: Sequence[_Node],
    invocation: dict[str, object],
) -> _Transaction:
    marker_descriptor: int | None = None
    stage_descriptor: int | None = None
    controls: list[_Control] = []
    try:
        if _list_directory(target_descriptor):
            raise InPlaceCreateError(
                "project-target-not-empty",
                "The current directory must be completely empty before project creation.",
            )
        try:
            os.mkdir(MARKER, mode=_DIRECTORY_MODE, dir_fd=target_descriptor)
        except FileExistsError:
            raise InPlaceCreateError(
                "project-target-not-empty",
                "Another project creation transaction is active in this directory.",
            ) from None
        os.fsync(target_descriptor)
        marker_descriptor = _open_directory(target_descriptor, MARKER)
        os.fchmod(marker_descriptor, _DIRECTORY_MODE)
        marker_metadata = os.fstat(marker_descriptor)
        marker_identity = _stat_identity(marker_metadata)
        _require_directory_entry(
            target_descriptor,
            MARKER,
            marker_descriptor,
            marker_identity,
            _DIRECTORY_MODE,
        )

        os.mkdir(STAGE, mode=_DIRECTORY_MODE, dir_fd=marker_descriptor)
        stage_descriptor = _open_directory(marker_descriptor, STAGE)
        os.fchmod(stage_descriptor, _DIRECTORY_MODE)
        stage_metadata = os.fstat(stage_descriptor)
        stage_identity = _stat_identity(stage_metadata)
        _require_directory_entry(
            marker_descriptor,
            STAGE,
            stage_descriptor,
            stage_identity,
            _DIRECTORY_MODE,
        )
        os.fsync(marker_descriptor)
        if set(_list_directory(target_descriptor)) != {MARKER}:
            raise _recovery_required()
        _copy_tree(source_descriptor, stage_descriptor, source_manifest)
        if _snapshot_tree(source_descriptor) != tuple(source_manifest):
            raise OSError(errno.ESTALE, "rendered project changed during copy")
        staged_manifest = _snapshot_tree(stage_descriptor)
        if not _same_content(source_manifest, staged_manifest):
            raise OSError(errno.ESTALE, "copied project differs from rendered project")
        if any(node.dev != target_identity[0] for node in staged_manifest):
            raise OSError(errno.EXDEV, "staged project crossed a filesystem boundary")

        manifest_control = _create_control(marker_descriptor, MANIFEST)
        controls.append(manifest_control)
        journal_control = _create_control(marker_descriptor, JOURNAL)
        controls.append(journal_control)
        metadata_control = _create_control(marker_descriptor, METADATA)
        controls.append(metadata_control)
        transaction_id = secrets.token_hex(16)
        control_identities = {
            control.name: control.as_dict() for control in controls
        }
        manifest_document: dict[str, object] = {
            "controls": control_identities,
            "invocation": invocation,
            "marker": {
                "dev": marker_identity[0],
                "ino": marker_identity[1],
                "mode": _DIRECTORY_MODE,
            },
            "nodes": [node.as_dict() for node in staged_manifest],
            "schema": SCHEMA,
            "stage": {
                "dev": stage_identity[0],
                "ino": stage_identity[1],
                "mode": _DIRECTORY_MODE,
            },
            "target": {
                "dev": target_identity[0],
                "ino": target_identity[1],
                "mode": target_mode,
            },
            "transaction_id": transaction_id,
        }
        manifest_payload = _canonical_json(manifest_document)
        manifest_checksum = hashlib.sha256(manifest_payload).hexdigest()
        _write_control(manifest_control, manifest_document)
        metadata_document = {
            "controls": control_identities,
            "invocation": invocation,
            "manifest_sha256": manifest_checksum,
            "schema": SCHEMA,
            "transaction_id": transaction_id,
        }
        _write_control(metadata_control, metadata_document)
        transaction = _Transaction(
            transaction_id=transaction_id,
            marker_descriptor=marker_descriptor,
            marker_identity=marker_identity,
            stage_descriptor=stage_descriptor,
            stage_identity=stage_identity,
            metadata=metadata_control,
            manifest_control=manifest_control,
            journal_control=journal_control,
            manifest=staged_manifest,
            manifest_document=manifest_document,
            manifest_checksum=manifest_checksum,
            journal=[],
        )
        begin = _journal_record(
            transaction,
            "begin",
            manifest_sha256=manifest_checksum,
        )
        _append_journal(transaction, begin)
        os.fsync(marker_descriptor)
        os.fsync(stage_descriptor)
        os.fsync(target_descriptor)
        return transaction
    except InPlaceCreateError:
        for control in controls:
            _safe_close(control.descriptor)
        if stage_descriptor is not None:
            _safe_close(stage_descriptor)
        if marker_descriptor is not None:
            _safe_close(marker_descriptor)
        raise
    except OSError:
        for control in controls:
            _safe_close(control.descriptor)
        if stage_descriptor is not None:
            _safe_close(stage_descriptor)
        if marker_descriptor is not None:
            _safe_close(marker_descriptor)
        raise _recovery_required() from None


def _load_transaction(
    target_descriptor: int,
    target_identity: tuple[int, int],
    target_mode: int,
    source_manifest: Sequence[_Node],
    invocation: dict[str, object],
) -> tuple[_Transaction | None, bool]:
    transaction: _Transaction | None = None
    try:
        marker_descriptor = _open_directory(target_descriptor, MARKER)
    except OSError:
        raise _recovery_required() from None
    marker_metadata = os.fstat(marker_descriptor)
    marker_identity = _stat_identity(marker_metadata)
    controls: list[_Control] = []
    stage_descriptor: int | None = None
    try:
        _require_directory_entry(
            target_descriptor,
            MARKER,
            marker_descriptor,
            marker_identity,
            _DIRECTORY_MODE,
        )
        marker_names = set(_list_directory(marker_descriptor))
        if not marker_names:
            raise _recovery_required()
        if MANIFEST not in marker_names:
            raise _recovery_required()

        manifest_control = _open_control(marker_descriptor, MANIFEST)
        controls.append(manifest_control)
        manifest_payload = _read_control_bytes(manifest_control)
        try:
            manifest_raw = _strict_json(manifest_payload)
        except (UnicodeDecodeError, ValueError):
            raise _recovery_required() from None
        manifest_document, manifest = _validate_manifest_document(
            manifest_raw,
            target_identity,
            target_mode,
            invocation,
            source_manifest,
        )
        if manifest_payload != _canonical_json(manifest_document):
            raise _recovery_required()
        expected_controls = _mapping(manifest_document.get("controls"))
        _require_control_identity(manifest_control, expected_controls, MANIFEST)
        manifest_checksum = hashlib.sha256(
            _canonical_json(manifest_document)
        ).hexdigest()
        transaction_id = _string(manifest_document.get("transaction_id"))
        marker_identity_document = _mapping(manifest_document.get("marker"))
        if (
            _integer(marker_identity_document.get("dev")),
            _integer(marker_identity_document.get("ino")),
        ) != marker_identity:
            raise _recovery_required()
        stage_identity_document = _mapping(manifest_document.get("stage"))
        stage_identity = (
            _integer(stage_identity_document.get("dev")),
            _integer(stage_identity_document.get("ino")),
        )

        if STAGE in marker_names:
            stage_descriptor = _open_directory(marker_descriptor, STAGE)
            _require_directory_entry(
                marker_descriptor,
                STAGE,
                stage_descriptor,
                stage_identity,
                _DIRECTORY_MODE,
            )

        metadata_control: _Control | None = None
        journal_control: _Control | None = None
        if METADATA in marker_names:
            metadata_control = _open_control(
                marker_descriptor,
                METADATA,
                _mapping(expected_controls.get(METADATA)),
            )
            controls.append(metadata_control)
        if JOURNAL in marker_names:
            journal_control = _open_control(
                marker_descriptor,
                JOURNAL,
                _mapping(expected_controls.get(JOURNAL)),
            )
            controls.append(journal_control)

        transaction = _Transaction(
            transaction_id=transaction_id,
            marker_descriptor=marker_descriptor,
            marker_identity=marker_identity,
            stage_descriptor=stage_descriptor,
            stage_identity=stage_identity,
            metadata=metadata_control,
            manifest_control=manifest_control,
            journal_control=journal_control,
            manifest=manifest,
            manifest_document=manifest_document,
            manifest_checksum=manifest_checksum,
            journal=[],
        )
        marker_descriptor = -1
        stage_descriptor = None
        controls.clear()

        root_names = _root_names(manifest)
        complete = _all_published(target_descriptor, transaction)
        if transaction.stage_descriptor is None:
            allowed_cleanup_names = (
                {MANIFEST, METADATA, JOURNAL},
                {MANIFEST, JOURNAL},
                {MANIFEST},
            )
            if marker_names not in allowed_cleanup_names:
                raise _recovery_required()
            if transaction.metadata is not None:
                _validate_metadata(transaction)
            if transaction.journal_control is not None:
                transaction.journal = _read_journal(transaction)
                state = _journal_state(transaction, root_names)
                if complete and not state.committed:
                    raise _recovery_required()
                if not complete and not state.rollback_started:
                    raise _recovery_required()
            elif marker_names != {MANIFEST}:
                raise _recovery_required()
            if complete:
                return transaction, False
            if set(_list_directory(target_descriptor)) == {MARKER}:
                return transaction, True
            raise _recovery_required()

        if marker_names != {STAGE, METADATA, MANIFEST, JOURNAL}:
            raise _recovery_required()
        if transaction.stage_descriptor is None:
            raise _recovery_required()
        _validate_metadata(transaction)
        if transaction.journal_control is None:
            raise _recovery_required()
        transaction.journal = _read_journal(transaction)
        state = _require_transaction_namespace(target_descriptor, transaction)
        return transaction, state.rollback_started
    except BaseException as error:
        if transaction is not None:
            _close_transaction(transaction)
            transaction = None
        for control in controls:
            _safe_close(control.descriptor)
        if stage_descriptor is not None:
            _safe_close(stage_descriptor)
        if marker_descriptor >= 0:
            _safe_close(marker_descriptor)
        if isinstance(error, OSError):
            raise _recovery_required() from None
        raise


def _validate_manifest_document(
    raw: object,
    target_identity: tuple[int, int],
    target_mode: int,
    invocation: dict[str, object],
    source_manifest: Sequence[_Node],
) -> tuple[dict[str, object], tuple[_Node, ...]]:
    document = _mapping(raw)
    if set(document) != {
        "controls",
        "invocation",
        "marker",
        "nodes",
        "schema",
        "stage",
        "target",
        "transaction_id",
    }:
        raise _recovery_required()
    if _integer(document.get("schema")) != SCHEMA:
        raise _recovery_required()
    transaction_id = _string(document.get("transaction_id"))
    if len(transaction_id) != 32 or any(
        character not in "0123456789abcdef" for character in transaction_id
    ):
        raise _recovery_required()
    if _mapping(document.get("invocation")) != invocation:
        raise _recovery_required()
    target = _mapping(document.get("target"))
    if (
        _integer(target.get("dev")),
        _integer(target.get("ino")),
        _integer(target.get("mode")),
    ) != (*target_identity, target_mode):
        raise _recovery_required()
    marker = _mapping(document.get("marker"))
    stage = _mapping(document.get("stage"))
    for identity_document in (marker, stage):
        if set(identity_document) != {"dev", "ino", "mode"}:
            raise _recovery_required()
        _integer(identity_document.get("dev"))
        _integer(identity_document.get("ino"))
        if _integer(identity_document.get("mode")) != _DIRECTORY_MODE:
            raise _recovery_required()
    controls = _mapping(document.get("controls"))
    if set(controls) != {MANIFEST, JOURNAL, METADATA}:
        raise _recovery_required()
    for name in (MANIFEST, JOURNAL, METADATA):
        value = _mapping(controls.get(name))
        if set(value) != {"dev", "ino", "mode"}:
            raise _recovery_required()
        _integer(value.get("dev"))
        _integer(value.get("ino"))
        if _integer(value.get("mode")) != _CONTROL_MODE:
            raise _recovery_required()
    raw_nodes = document.get("nodes")
    if not isinstance(raw_nodes, list):
        raise _recovery_required()
    nodes = tuple(_parse_node(value) for value in raw_nodes)
    _validate_nodes(nodes, target_identity[0])
    if not _same_content(nodes, source_manifest):
        raise _recovery_required()
    return document, nodes


def _parse_node(raw: object) -> _Node:
    value = _mapping(raw)
    if set(value) != {
        "dev",
        "ino",
        "kind",
        "mode",
        "nlink",
        "path",
        "sha256",
        "size",
    }:
        raise _recovery_required()
    path = _string(value.get("path"))
    kind = _string(value.get("kind"))
    digest = value.get("sha256")
    size = value.get("size")
    if kind == "file":
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise _recovery_required()
        parsed_size: int | None = _integer(size)
    elif kind == "directory":
        if digest is not None or size is not None:
            raise _recovery_required()
        parsed_size = None
        digest = None
    else:
        raise _recovery_required()
    return _Node(
        path=path,
        kind=kind,
        dev=_integer(value.get("dev")),
        ino=_integer(value.get("ino")),
        mode=_integer(value.get("mode")),
        nlink=_integer(value.get("nlink")),
        size=parsed_size,
        sha256=digest,
    )


def _validate_nodes(nodes: Sequence[_Node], target_device: int) -> None:
    if not nodes or tuple(node.path for node in nodes) != tuple(
        sorted(node.path for node in nodes)
    ):
        raise _recovery_required()
    paths: set[str] = set()
    identities: set[tuple[int, int]] = set()
    for node in nodes:
        parts = node.path.split("/")
        if (
            not parts
            or any(not part or part in {".", ".."} for part in parts)
            or any("\0" in part for part in parts)
            or parts[0] == MARKER
            or node.path in paths
            or node.dev != target_device
            or node.mode < 0
            or node.mode > 0o777
            or node.nlink < 1
            or (node.dev, node.ino) in identities
        ):
            raise _recovery_required()
        if len(parts) > 1 and "/".join(parts[:-1]) not in paths:
            raise _recovery_required()
        if node.kind == "file" and node.nlink != 1:
            raise _recovery_required()
        paths.add(node.path)
        identities.add((node.dev, node.ino))


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise _recovery_required()
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise _recovery_required()
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _recovery_required()
    return value


def _require_control_identity(
    control: _Control, controls: Mapping[str, object], name: str
) -> None:
    expected = _mapping(controls.get(name))
    if (
        control.dev != expected.get("dev")
        or control.ino != expected.get("ino")
        or expected.get("mode") != _CONTROL_MODE
    ):
        raise _recovery_required()


def _validate_metadata(transaction: _Transaction) -> None:
    if transaction.metadata is None:
        raise _recovery_required()
    payload = _read_control_bytes(transaction.metadata)
    try:
        raw = _strict_json(payload)
    except (UnicodeDecodeError, ValueError):
        raise _recovery_required() from None
    expected_controls = transaction.manifest_document["controls"]
    expected = {
        "controls": expected_controls,
        "invocation": transaction.manifest_document["invocation"],
        "manifest_sha256": transaction.manifest_checksum,
        "schema": SCHEMA,
        "transaction_id": transaction.transaction_id,
    }
    if raw != expected or payload != _canonical_json(expected):
        raise _recovery_required()


def _journal_record(
    transaction: _Transaction, kind: str, **fields: object
) -> dict[str, object]:
    previous = transaction.journal[-1]["hash"] if transaction.journal else ""
    record: dict[str, object] = {
        "kind": kind,
        "previous": previous,
        "seq": len(transaction.journal),
        "transaction_id": transaction.transaction_id,
        **fields,
    }
    record["hash"] = hashlib.sha256(_canonical_json(record)).hexdigest()
    return record


def _append_journal(
    transaction: _Transaction, record: dict[str, object]
) -> None:
    control = transaction.journal_control
    if control is None or not _control_matches(transaction.marker_descriptor, control):
        raise _recovery_required()
    payload = _canonical_json(record)
    current_size = os.fstat(control.descriptor).st_size
    if current_size + len(payload) > CONTROL_FILE_LIMIT:
        raise _recovery_required()
    os.lseek(control.descriptor, 0, os.SEEK_END)
    _write_all(control.descriptor, payload)
    os.fsync(control.descriptor)
    os.fsync(transaction.marker_descriptor)
    transaction.journal.append(record)


def _read_journal(transaction: _Transaction) -> list[dict[str, object]]:
    control = transaction.journal_control
    if control is None:
        raise _recovery_required()
    payload = _read_control_bytes(control)
    if not payload or len(payload) > CONTROL_FILE_LIMIT:
        raise _recovery_required()
    complete_payload = payload
    incomplete_tail = b""
    if not payload.endswith(b"\n"):
        boundary = payload.rfind(b"\n")
        complete_payload = payload[: boundary + 1]
        incomplete_tail = payload[boundary + 1 :]
    records: list[dict[str, object]] = []
    previous = ""
    for sequence, line in enumerate(complete_payload.splitlines()):
        try:
            parsed = _strict_json(line)
        except (UnicodeDecodeError, ValueError):
            raise _recovery_required() from None
        record = _mapping(parsed)
        if line + b"\n" != _canonical_json(record):
            raise _recovery_required()
        digest = record.get("hash")
        unhashed = dict(record)
        unhashed.pop("hash", None)
        expected_hash = hashlib.sha256(_canonical_json(unhashed)).hexdigest()
        if (
            not isinstance(digest, str)
            or digest != expected_hash
            or record.get("previous") != previous
            or record.get("seq") != sequence
            or record.get("transaction_id") != transaction.transaction_id
        ):
            raise _recovery_required()
        previous = digest
        records.append(record)
    if incomplete_tail:
        if not records:
            begin = _journal_record(
                transaction,
                "begin",
                manifest_sha256=transaction.manifest_checksum,
            )
            successors = (begin,)
        else:
            successors = _legal_journal_successors(transaction, records)
        if not any(
            _canonical_json(candidate).startswith(incomplete_tail)
            for candidate in successors
        ):
            raise _recovery_required()
        if not _control_matches(transaction.marker_descriptor, control):
            raise _recovery_required()
        if not records:
            repaired = _canonical_json(begin)
            os.lseek(control.descriptor, 0, os.SEEK_SET)
            _write_all(control.descriptor, repaired)
            os.ftruncate(control.descriptor, len(repaired))
        else:
            os.ftruncate(control.descriptor, len(complete_payload))
        os.fsync(control.descriptor)
        os.fsync(transaction.marker_descriptor)
        _checkpoint("journal-tail-truncated")
        if not records:
            return [begin]
    return records


def _legal_journal_successors(
    transaction: _Transaction, records: list[dict[str, object]]
) -> tuple[dict[str, object], ...]:
    previous = transaction.journal
    transaction.journal = records
    try:
        roots = _root_names(transaction.manifest)
        state = _journal_state(transaction, roots)
        if state.committed or state.rollback_started:
            return ()
        candidates = [_journal_record(transaction, "rollback-started")]
        if state.pending is not None:
            candidates.append(
                _journal_record(
                    transaction,
                    "published",
                    destination=state.pending,
                )
            )
        elif len(state.published) < len(roots):
            root = roots[len(state.published)]
            candidates.append(
                _journal_record(
                    transaction,
                    "prepared",
                    destination=root,
                    nodes=[
                        node.as_dict()
                        for node in _subtree(transaction.manifest, root)
                    ],
                )
            )
        else:
            candidates.append(_journal_record(transaction, "committed"))
        return tuple(candidates)
    finally:
        transaction.journal = previous


def _journal_state(
    transaction: _Transaction, roots: Sequence[str]
) -> _JournalState:
    records = transaction.journal
    if not records:
        raise _recovery_required()
    begin = records[0]
    if (
        begin.get("kind") != "begin"
        or begin.get("manifest_sha256") != transaction.manifest_checksum
        or set(begin)
        != {
            "hash",
            "kind",
            "manifest_sha256",
            "previous",
            "seq",
            "transaction_id",
        }
    ):
        raise _recovery_required()
    published: list[str] = []
    pending: str | None = None
    committed = False
    rollback_started = False
    for record in records[1:]:
        kind = record.get("kind")
        if committed or rollback_started:
            raise _recovery_required()
        if kind == "prepared":
            if pending is not None or len(published) >= len(roots):
                raise _recovery_required()
            destination = roots[len(published)]
            if (
                record.get("destination") != destination
                or record.get("nodes")
                != [node.as_dict() for node in _subtree(transaction.manifest, destination)]
                or set(record)
                != {
                    "destination",
                    "hash",
                    "kind",
                    "nodes",
                    "previous",
                    "seq",
                    "transaction_id",
                }
            ):
                raise _recovery_required()
            pending = destination
        elif kind == "published":
            if pending is None or record.get("destination") != pending:
                raise _recovery_required()
            if set(record) != {
                "destination",
                "hash",
                "kind",
                "previous",
                "seq",
                "transaction_id",
            }:
                raise _recovery_required()
            published.append(pending)
            pending = None
        elif kind == "committed":
            if pending is not None or published != list(roots):
                raise _recovery_required()
            if set(record) != {
                "hash",
                "kind",
                "previous",
                "seq",
                "transaction_id",
            }:
                raise _recovery_required()
            committed = True
        elif kind == "rollback-started":
            if set(record) != {
                "hash",
                "kind",
                "previous",
                "seq",
                "transaction_id",
            }:
                raise _recovery_required()
            rollback_started = True
        else:
            raise _recovery_required()
    return _JournalState(tuple(published), pending, committed, rollback_started)


def _root_names(nodes: Sequence[_Node]) -> tuple[str, ...]:
    return tuple(node.path for node in nodes if "/" not in node.path)


def _subtree(nodes: Sequence[_Node], root: str) -> tuple[_Node, ...]:
    return tuple(
        node for node in nodes if node.path == root or node.path.startswith(f"{root}/")
    )


def _require_directory_entry(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    identity: tuple[int, int],
    mode: int,
) -> None:
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        raise _recovery_required() from None
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or _stat_identity(opened) != identity
        or _stat_identity(named) != identity
        or stat.S_IMODE(opened.st_mode) != mode
        or stat.S_IMODE(named.st_mode) != mode
    ):
        raise _recovery_required()


def _require_transaction_controls(
    target_descriptor: int, transaction: _Transaction
) -> None:
    _require_directory_entry(
        target_descriptor,
        MARKER,
        transaction.marker_descriptor,
        transaction.marker_identity,
        _DIRECTORY_MODE,
    )
    expected_marker_names = {MANIFEST}
    if transaction.stage_descriptor is not None:
        expected_marker_names.add(STAGE)
    if transaction.metadata is not None:
        expected_marker_names.add(METADATA)
    if transaction.journal_control is not None:
        expected_marker_names.add(JOURNAL)
    if set(_list_directory(transaction.marker_descriptor)) != expected_marker_names:
        raise _recovery_required()
    if transaction.stage_descriptor is not None:
        _require_directory_entry(
            transaction.marker_descriptor,
            STAGE,
            transaction.stage_descriptor,
            transaction.stage_identity,
            _DIRECTORY_MODE,
        )
    if not _control_matches(
        transaction.marker_descriptor, transaction.manifest_control
    ):
        raise _recovery_required()
    if _read_control_bytes(transaction.manifest_control) != _canonical_json(
        transaction.manifest_document
    ):
        raise _recovery_required()
    if transaction.metadata is not None:
        if not _control_matches(transaction.marker_descriptor, transaction.metadata):
            raise _recovery_required()
        _validate_metadata(transaction)
    if transaction.journal_control is not None:
        if not _control_matches(
            transaction.marker_descriptor, transaction.journal_control
        ):
            raise _recovery_required()
        if _read_journal(transaction) != transaction.journal:
            raise _recovery_required()


def _entry_state(
    target_descriptor: int,
    stage_descriptor: int,
    root: str,
    manifest: Sequence[_Node],
) -> tuple[bool, bool, bool, bool]:
    source_exists = _exists(stage_descriptor, root)
    target_exists = _exists(target_descriptor, root)
    source_matches = source_exists and _entry_matches(stage_descriptor, root, manifest)
    target_matches = target_exists and _entry_matches(target_descriptor, root, manifest)
    return source_exists, source_matches, target_exists, target_matches


def _require_transaction_namespace(
    target_descriptor: int, transaction: _Transaction
) -> _JournalState:
    _require_transaction_controls(target_descriptor, transaction)
    if transaction.stage_descriptor is None:
        raise _recovery_required()
    roots = _root_names(transaction.manifest)
    state = _journal_state(transaction, roots)
    if state.committed or state.rollback_started:
        return state
    target_roots: set[str] = set()
    source_roots: set[str] = set()
    for index, root in enumerate(roots):
        source_exists, source_matches, target_exists, target_matches = _entry_state(
            target_descriptor,
            transaction.stage_descriptor,
            root,
            transaction.manifest,
        )
        if root in state.published:
            if source_exists or not target_matches:
                raise _recovery_required()
            target_roots.add(root)
        elif root == state.pending:
            if source_matches and not target_exists:
                source_roots.add(root)
            elif target_matches and not source_exists:
                target_roots.add(root)
            else:
                raise _recovery_required()
        else:
            if index < len(state.published) or not source_matches or target_exists:
                raise _recovery_required()
            source_roots.add(root)
    if set(_list_directory(target_descriptor)) != {MARKER, *target_roots}:
        raise _recovery_required()
    if set(_list_directory(transaction.stage_descriptor)) != source_roots:
        raise _recovery_required()
    if set(_list_directory(transaction.marker_descriptor)) != {
        STAGE,
        METADATA,
        MANIFEST,
        JOURNAL,
    }:
        raise _recovery_required()
    return state


def _publish(
    parent_descriptor: int,
    target_name: str,
    target_descriptor: int,
    target_identity: tuple[int, int],
    target_mode: int,
    transaction: _Transaction,
) -> None:
    if transaction.stage_descriptor is None:
        if not _all_published(target_descriptor, transaction):
            raise _recovery_required()
        return
    roots = _root_names(transaction.manifest)
    while True:
        _require_target(
            parent_descriptor,
            target_name,
            target_descriptor,
            target_identity,
            target_mode,
        )
        state = _require_transaction_namespace(target_descriptor, transaction)
        if state.rollback_started:
            raise _recovery_required()
        if state.committed or len(state.published) == len(roots):
            return
        root = roots[len(state.published)]
        if state.pending is None:
            record = _journal_record(
                transaction,
                "prepared",
                destination=root,
                nodes=[node.as_dict() for node in _subtree(transaction.manifest, root)],
            )
            _append_journal(transaction, record)
            _checkpoint(f"prepared:{root}")
            state = _require_transaction_namespace(target_descriptor, transaction)
        if state.pending != root:
            raise _recovery_required()
        source_exists, source_matches, target_exists, target_matches = _entry_state(
            target_descriptor,
            transaction.stage_descriptor,
            root,
            transaction.manifest,
        )
        if source_matches and not target_exists:
            try:
                _rename_noreplace(
                    transaction.stage_descriptor,
                    root,
                    target_descriptor,
                    root,
                )
            except FileExistsError:
                raise _recovery_required() from None
            _checkpoint(f"renamed:{root}")
            source_exists, source_matches, target_exists, target_matches = _entry_state(
                target_descriptor,
                transaction.stage_descriptor,
                root,
                transaction.manifest,
            )
        if source_exists or not target_matches:
            raise _recovery_required()
        _require_target(
            parent_descriptor,
            target_name,
            target_descriptor,
            target_identity,
            target_mode,
        )
        os.fsync(transaction.stage_descriptor)
        os.fsync(target_descriptor)
        record = _journal_record(
            transaction,
            "published",
            destination=root,
        )
        _append_journal(transaction, record)
        os.fsync(target_descriptor)
        _checkpoint(f"published:{root}")


def _all_published(target_descriptor: int, transaction: _Transaction) -> bool:
    roots = _root_names(transaction.manifest)
    if set(_list_directory(target_descriptor)) != {MARKER, *roots}:
        return False
    return all(
        _entry_matches(target_descriptor, root, transaction.manifest)
        for root in roots
    )


def _finish(
    parent_descriptor: int,
    target_name: str,
    target_descriptor: int,
    target_identity: tuple[int, int],
    target_mode: int,
    transaction: _Transaction,
) -> None:
    _require_target(
        parent_descriptor,
        target_name,
        target_descriptor,
        target_identity,
        target_mode,
    )
    if not _all_published(target_descriptor, transaction):
        raise _recovery_required()
    if transaction.journal_control is not None:
        state = _journal_state(
            transaction, _root_names(transaction.manifest)
        )
        if state.rollback_started:
            raise _recovery_required()
        if not state.committed:
            if state.pending is not None or len(state.published) != len(
                _root_names(transaction.manifest)
            ):
                raise _recovery_required()
            _append_journal(transaction, _journal_record(transaction, "committed"))
            os.fsync(target_descriptor)
            _checkpoint("committed")
    _cleanup_marker(
        parent_descriptor,
        target_name,
        target_descriptor,
        target_identity,
        target_mode,
        transaction,
        expected_roots=set(_root_names(transaction.manifest)),
    )


def _cleanup_marker(
    parent_descriptor: int,
    target_name: str,
    target_descriptor: int,
    target_identity: tuple[int, int],
    target_mode: int,
    transaction: _Transaction,
    *,
    expected_roots: set[str],
) -> None:
    _require_target(
        parent_descriptor,
        target_name,
        target_descriptor,
        target_identity,
        target_mode,
    )
    if set(_list_directory(target_descriptor)) != {MARKER, *expected_roots}:
        raise _recovery_required()
    _require_directory_entry(
        target_descriptor,
        MARKER,
        transaction.marker_descriptor,
        transaction.marker_identity,
        _DIRECTORY_MODE,
    )
    expected_marker_names = {MANIFEST}
    if transaction.stage_descriptor is not None:
        expected_marker_names.add(STAGE)
    if transaction.metadata is not None:
        expected_marker_names.add(METADATA)
    if transaction.journal_control is not None:
        expected_marker_names.add(JOURNAL)
    if set(_list_directory(transaction.marker_descriptor)) != expected_marker_names:
        raise _recovery_required()
    if transaction.stage_descriptor is not None:
        _require_directory_entry(
            transaction.marker_descriptor,
            STAGE,
            transaction.stage_descriptor,
            transaction.stage_identity,
            _DIRECTORY_MODE,
        )
        if _list_directory(transaction.stage_descriptor):
            raise _recovery_required()
        os.rmdir(STAGE, dir_fd=transaction.marker_descriptor)
        os.fsync(transaction.marker_descriptor)
        os.close(transaction.stage_descriptor)
        transaction.stage_descriptor = None
        _checkpoint("stage-removed")
    if transaction.metadata is not None:
        _require_directory_entry(
            target_descriptor,
            MARKER,
            transaction.marker_descriptor,
            transaction.marker_identity,
            _DIRECTORY_MODE,
        )
        _unlink_control(transaction, transaction.metadata)
        transaction.metadata = None
        _checkpoint("metadata-removed")
    if transaction.journal_control is not None:
        _require_directory_entry(
            target_descriptor,
            MARKER,
            transaction.marker_descriptor,
            transaction.marker_identity,
            _DIRECTORY_MODE,
        )
        _unlink_control(transaction, transaction.journal_control)
        transaction.journal_control = None
        _checkpoint("journal-removed")
    _require_directory_entry(
        target_descriptor,
        MARKER,
        transaction.marker_descriptor,
        transaction.marker_identity,
        _DIRECTORY_MODE,
    )
    if not _control_matches(
        transaction.marker_descriptor, transaction.manifest_control
    ):
        raise _recovery_required()
    os.unlink(MANIFEST, dir_fd=transaction.marker_descriptor)
    os.fsync(transaction.marker_descriptor)
    os.close(transaction.manifest_control.descriptor)
    transaction.manifest_control = _Control(MANIFEST, -1, -1, -1)
    _checkpoint("manifest-removed")
    if _list_directory(transaction.marker_descriptor):
        raise _recovery_required()
    _require_directory_entry(
        target_descriptor,
        MARKER,
        transaction.marker_descriptor,
        transaction.marker_identity,
        _DIRECTORY_MODE,
    )
    os.rmdir(MARKER, dir_fd=target_descriptor)
    os.fsync(target_descriptor)
    _checkpoint("marker-removed")
    _require_target(
        parent_descriptor,
        target_name,
        target_descriptor,
        target_identity,
        target_mode,
    )
    if set(_list_directory(target_descriptor)) != expected_roots:
        raise _recovery_required()
    if expected_roots and not all(
        _entry_matches(target_descriptor, root, transaction.manifest)
        for root in expected_roots
    ):
        raise _recovery_required()


def _unlink_control(transaction: _Transaction, control: _Control) -> None:
    if not _control_matches(transaction.marker_descriptor, control):
        raise _recovery_required()
    os.unlink(control.name, dir_fd=transaction.marker_descriptor)
    os.fsync(transaction.marker_descriptor)
    os.close(control.descriptor)


def _rollback(
    parent_descriptor: int,
    target_name: str,
    target_descriptor: int,
    target_identity: tuple[int, int],
    target_mode: int,
    transaction: _Transaction,
) -> bool:
    try:
        _require_target(
            parent_descriptor,
            target_name,
            target_descriptor,
            target_identity,
            target_mode,
        )
        if transaction.stage_descriptor is None or transaction.journal_control is None:
            return False
        state = _require_transaction_namespace(target_descriptor, transaction)
        if state.committed:
            return False
        locations: dict[str, str] = {}
        source_roots: set[str] = set()
        target_roots: set[str] = set()
        for root in _root_names(transaction.manifest):
            source_exists, source_matches, target_exists, target_matches = _entry_state(
                target_descriptor,
                transaction.stage_descriptor,
                root,
                transaction.manifest,
            )
            if (
                source_exists
                and not target_exists
                and (
                    source_matches
                    or (
                        state.rollback_started
                        and _entry_is_owned_remainder(
                            transaction.stage_descriptor,
                            root,
                            transaction.manifest,
                        )
                    )
                )
            ):
                locations[root] = "stage"
                source_roots.add(root)
            elif (
                target_exists
                and not source_exists
                and (
                    target_matches
                    or (
                        state.rollback_started
                        and _entry_is_owned_remainder(
                            target_descriptor,
                            root,
                            transaction.manifest,
                        )
                    )
                )
            ):
                locations[root] = "target"
                target_roots.add(root)
            elif (
                state.rollback_started
                and not source_exists
                and not target_exists
            ):
                locations[root] = "missing"
            else:
                return False
        if set(_list_directory(target_descriptor)) != {MARKER, *target_roots}:
            return False
        if set(_list_directory(transaction.stage_descriptor)) != source_roots:
            return False
        if set(_list_directory(transaction.marker_descriptor)) != {
            STAGE,
            METADATA,
            MANIFEST,
            JOURNAL,
        }:
            return False
        if not state.rollback_started:
            _append_journal(
                transaction, _journal_record(transaction, "rollback-started")
            )
            _checkpoint("rollback-started")
        _require_directory_entry(
            target_descriptor,
            MARKER,
            transaction.marker_descriptor,
            transaction.marker_identity,
            _DIRECTORY_MODE,
        )
        for root, location in locations.items():
            if location != "target":
                continue
            if not _entry_is_owned_remainder(
                target_descriptor, root, transaction.manifest
            ):
                return False
            _rename_noreplace(
                target_descriptor,
                root,
                transaction.stage_descriptor,
                root,
            )
            os.fsync(target_descriptor)
            os.fsync(transaction.stage_descriptor)
            _checkpoint(f"rollback-restored:{root}")
            if _exists(target_descriptor, root) or not _entry_is_owned_remainder(
                transaction.stage_descriptor, root, transaction.manifest
            ):
                if not _exists(target_descriptor, root) and _exists(
                    transaction.stage_descriptor, root
                ):
                    _rename_noreplace(
                        transaction.stage_descriptor,
                        root,
                        target_descriptor,
                        root,
                    )
                    os.fsync(transaction.stage_descriptor)
                    os.fsync(target_descriptor)
                return False
            source_roots.add(root)
            target_roots.discard(root)
        if set(_list_directory(target_descriptor)) != {MARKER}:
            return False
        if set(_list_directory(transaction.stage_descriptor)) != source_roots:
            return False
        for root in _root_names(transaction.manifest):
            if not _exists(transaction.stage_descriptor, root):
                continue
            if not _entry_is_owned_remainder(
                transaction.stage_descriptor, root, transaction.manifest
            ):
                return False
            _remove_manifest_entry(
                transaction.stage_descriptor, root, transaction.manifest
            )
            os.fsync(transaction.stage_descriptor)
            _checkpoint(f"rollback-removed:{root}")
        if _list_directory(transaction.stage_descriptor):
            return False
        _cleanup_marker(
            parent_descriptor,
            target_name,
            target_descriptor,
            target_identity,
            target_mode,
            transaction,
            expected_roots=set(),
        )
        return True
    except (InPlaceCreateError, OSError):
        return False


def _remove_manifest_entry(
    parent_descriptor: int,
    root: str,
    manifest: Sequence[_Node],
) -> None:
    index = {node.path: node for node in manifest}
    _remove_manifest_node(parent_descriptor, root, index)


def _remove_manifest_node(
    parent_descriptor: int, path: str, index: Mapping[str, _Node]
) -> None:
    node = index[path]
    name = path.rsplit("/", 1)[-1]
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if node.kind == "file":
        actual = _snapshot_file(parent_descriptor, name, path, metadata)
        if actual != node:
            raise OSError(errno.ESTALE, "owned file changed during rollback")
        os.unlink(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        _checkpoint(f"rollback-node-removed:{path}")
        return
    child = _open_directory(parent_descriptor, name)
    try:
        opened = os.fstat(child)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _stat_identity(opened) != (node.dev, node.ino)
            or stat.S_IMODE(opened.st_mode) != node.mode
        ):
            raise OSError(errno.ESTALE, "owned directory changed during rollback")
        expected_children = {
            candidate.path.rsplit("/", 1)[-1]: candidate.path
            for candidate in index.values()
            if candidate.path.startswith(f"{path}/")
            and "/" not in candidate.path[len(path) + 1 :]
        }
        actual_children = sorted(_list_directory(child))
        if not set(actual_children).issubset(expected_children):
            raise OSError(errno.ESTALE, "owned directory changed during rollback")
        for child_name in actual_children:
            _remove_manifest_node(child, expected_children[child_name], index)
        os.fsync(child)
    finally:
        os.close(child)
    current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISDIR(current.st_mode)
        or _stat_identity(current) != (node.dev, node.ino)
        or stat.S_IMODE(current.st_mode) != node.mode
    ):
        raise OSError(errno.ESTALE, "owned directory changed during rollback")
    os.rmdir(name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)
    _checkpoint(f"rollback-node-removed:{path}")


def _close_transaction(transaction: _Transaction) -> None:
    descriptors = [
        transaction.stage_descriptor,
        transaction.metadata.descriptor if transaction.metadata is not None else None,
        (
            transaction.manifest_control.descriptor
            if transaction.manifest_control.descriptor >= 0
            else None
        ),
        (
            transaction.journal_control.descriptor
            if transaction.journal_control is not None
            else None
        ),
        transaction.marker_descriptor,
    ]
    for descriptor in descriptors:
        if descriptor is not None:
            _safe_close(descriptor)


def _safe_close(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


__all__ = ["InPlaceCreateError", "InPlaceResult", "create_in_current_directory"]
