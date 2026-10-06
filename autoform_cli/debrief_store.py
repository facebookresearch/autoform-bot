"""Symlink-safe, exclusive, atomic file operations for the debrief store.

Every operation below the store root goes through a directory file descriptor
opened with ``O_NOFOLLOW``, so a symlink planted at any directory or file name
inside the store is refused instead of followed. Records are published with
exclusive creation (a hard link from a private temporary, which fails if the
name exists), so a record is either absent or complete and is never
overwritten. Only derived views use replace-on-write.
"""

from __future__ import annotations

import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024

_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class UnsafeStoreError(OSError):
    """A store path was not a plain file or directory, or a record broke a bound."""


def _check_name(name: str) -> str:
    if not _NAME.match(name):
        raise UnsafeStoreError(f"invalid store entry name: {name!r}")
    return name


@contextmanager
def open_root(root: Path) -> Iterator[int]:
    """Create ``root`` if needed and yield a no-follow descriptor for it.

    ``root`` must already be resolved: its ancestors may legitimately be
    symlinks (``/tmp`` on macOS), but its final component may not.
    """
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root, _DIR_FLAGS)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_subdir(parent_fd: int, name: str) -> Iterator[int]:
    _check_name(name)
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _write_temporary(dir_fd: int, name: str, data: bytes) -> str:
    if len(data) > MAX_FILE_BYTES:
        raise UnsafeStoreError(f"{name} is {len(data)} bytes, over the {MAX_FILE_BYTES}-byte bound")
    temporary = f".tmp-{uuid.uuid4().hex}"
    fd = os.open(temporary, _CREATE_FLAGS, 0o600, dir_fd=dir_fd)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(temporary, dir_fd=dir_fd)
        raise
    os.close(fd)
    return temporary


def create_exclusive(dir_fd: int, name: str, data: bytes) -> None:
    """Atomically publish ``data`` as ``name``; raise ``FileExistsError`` if it exists."""
    _check_name(name)
    temporary = _write_temporary(dir_fd, name, data)
    try:
        os.link(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    finally:
        os.unlink(temporary, dir_fd=dir_fd)


def replace_atomic(dir_fd: int, name: str, data: bytes) -> None:
    """Atomically replace ``name`` (a derived view). A symlink at ``name`` is replaced, not followed."""
    _check_name(name)
    temporary = _write_temporary(dir_fd, name, data)
    try:
        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        os.unlink(temporary, dir_fd=dir_fd)
        raise


def open_exclusive(dir_fd: int, name: str) -> int:
    """Return a write descriptor for a new file ``name`` (for logs)."""
    return os.open(_check_name(name), _CREATE_FLAGS, 0o600, dir_fd=dir_fd)


def read_bounded(dir_fd: int, name: str) -> bytes:
    """Read a regular file without following symlinks, refusing anything over the bound."""
    fd = os.open(_check_name(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafeStoreError(f"{name} is not a regular file")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, 65536):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise UnsafeStoreError(f"{name} exceeds the {MAX_FILE_BYTES}-byte bound")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(_check_name(name), dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def list_names(dir_fd: int, suffix: str) -> list[str]:
    return sorted(name for name in os.listdir(dir_fd) if name.endswith(suffix) and _NAME.match(name))


def move(src_fd: int, name: str, dst_fd: int) -> bool:
    """Atomically move ``name`` between store directories; ``False`` if another process took it."""
    try:
        os.rename(_check_name(name), name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
    except FileNotFoundError:
        return False
    return True


def remove(dir_fd: int, name: str) -> None:
    try:
        os.unlink(_check_name(name), dir_fd=dir_fd)
    except FileNotFoundError:
        pass
