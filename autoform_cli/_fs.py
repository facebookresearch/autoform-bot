"""Filesystem helpers shared by the blueprint readers and writers."""

from __future__ import annotations

import errno
import os
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path

#: Each path resolved inside :func:`resolving_once`, keyed by its spelling:
#: Windows paths compare without case, and a missing file keeps the case it was given.
_RESOLVED: ContextVar[dict[str, Path] | None] = ContextVar("_RESOLVED", default=None)

#: The errors pathlib's ``is_dir()``, ``is_file()`` and ``is_symlink()`` read as
#: "no such path" before Python 3.14, which raised every other one; Windows' are
#: a device not ready, an invalid name and a name it cannot resolve.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})
_ABSENT_WINERRORS = frozenset({21, 123, 1921})


def resolved_path(path: Path) -> Path | None:
    """``path`` with its symbolic links resolved, or None when the system cannot open it.

    The kernel follows the links first, so a missing part, a symlink loop or a
    chain past its link limit gives None on every Python, and that limit bounds
    the work `os.path.realpath` then does. Strict `realpath` also gives None at
    a link it cannot read, which macOS follows anyway. Windows applies `..` as
    text before it looks anything up. Callers still check where the result
    points and whether it is a file.
    """

    try:
        os.stat(path)
        return Path(os.path.realpath(path, strict=True))
    except (OSError, ValueError):  # ValueError: a NUL in the path
        return None


def path_is(path: Path, kind: Callable[[int], bool], *, follow_symlinks: bool = True) -> bool:
    """Whether ``kind``, such as `stat.S_ISDIR`, holds for the mode of ``path``,
    as pathlib's ``is_dir()``, ``is_file()`` and ``is_symlink()`` answered before Python 3.14.

    A missing path, or one the system cannot encode, gives False, and any other
    error raises. From 3.14 those checks give False for a path they cannot
    examine too, as below a folder that cannot be searched, so a caller would
    call an unreadable path missing. ``follow_symlinks=False`` examines a
    symbolic link itself, as ``is_symlink()`` does.
    """

    try:
        return kind(os.stat(path, follow_symlinks=follow_symlinks).st_mode)
    except OSError as error:
        if getattr(error, "winerror", None) in _ABSENT_WINERRORS or error.errno in _ABSENT_ERRNOS:
            return False
        raise
    except ValueError:  # a NUL in the path, or one the system cannot encode
        return False


@contextmanager
def resolving_once() -> Iterator[None]:
    """Resolve each path once until the block ends.

    For a render or a check, which resolve the same pages and nodes many times
    and create no symbolic links meanwhile. A path that does not exist yet
    resolves to its spelling, so writing it later changes nothing. A block
    inside another shares the outer one's memory.
    """

    if _RESOLVED.get() is not None:
        yield
        return
    token = _RESOLVED.set({})
    try:
        yield
    finally:
        _RESOLVED.reset(token)


def resolve(path: Path) -> Path:
    """``path.resolve()``, remembered inside :func:`resolving_once`."""

    memory = _RESOLVED.get()
    if memory is None:
        return path.resolve()
    key = os.fspath(path)
    resolved = memory.get(key)
    if resolved is None:
        resolved = memory[key] = path.resolve()
    return resolved


def directory_path(path: Path) -> Path | None:
    """``path`` as `resolved_path` gives it, or, for a directory it gives None
    for, the path the system gives the opened directory; None when neither works.

    For a blueprint root. `resolved_path` gives None at a link the system
    follows but will not let anyone read, which macOS allows for another
    user's symlink; above a blueprint such a link changes nothing inside it,
    so the system is asked for the path of the directory it opened. Links
    under that path then resolve as usual, and one inside the blueprint that
    cannot be read still gives None.
    """

    resolved = resolved_path(path)
    if resolved is not None or os.name == "nt":
        return resolved
    import fcntl

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except (OSError, ValueError):
        return None
    try:
        if hasattr(fcntl, "F_GETPATH"):  # macOS
            opened = os.fsdecode(fcntl.fcntl(descriptor, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
        else:  # Linux; elsewhere, without /proc, this fails and gives None
            opened = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return Path(opened)


def is_within(path: Path, directory: Path) -> bool:
    """Whether ``path`` is ``directory`` or below it, as spelled."""

    if os.name == "nt":  # Windows compares names without case, as relative_to does there
        try:
            path.relative_to(directory)
        except ValueError:
            return False
        return True
    # From Python 3.12, relative_to compares the directory with each of the
    # path's parents, which is slow enough to show up in loading a large blueprint.
    parts = directory.parts
    return path.parts[: len(parts)] == parts if parts else not path.anchor


def relative(path: Path, directory: Path) -> Path:
    """``path.relative_to(directory)``, read off the parts as :func:`is_within` compares them.

    A path outside ``directory`` still raises relative_to's ValueError."""

    if os.name == "nt" or not is_within(path, directory):
        return path.relative_to(directory)
    return Path(*path.parts[len(directory.parts) :])


def atomic_write(
    path: Path,
    data: bytes,
    *,
    mode: int,
    directory: int | None = None,
    prefix: str | None = None,
    check: Callable[[], object] | None = None,
) -> None:
    """Replace ``path`` with a file holding ``data``, in one step.

    The new file is staged beside ``path`` under a random name starting with
    ``prefix`` (by default ``path``'s name between dots), written in full,
    given ``mode``, flushed to disk and renamed over ``path``: the name holds
    the old complete file until it holds the new one, and other links to the
    old file keep it. ``check`` runs just before the rename and can stop it by
    raising. With ``directory``, an open descriptor of ``path``'s directory,
    every step goes by name in it, so a directory swapped for a link after it
    was opened is not followed. A failure or interruption before the rename
    removes the staging file if it can; a crash can leave it. The directory is
    not flushed, so a power loss can still undo the rename.
    """

    # Named before it exists, so the cleanup below sees the staging file from
    # the moment it is created. The name is too random to be anyone else's.
    name = f"{f'.{path.name}.' if prefix is None else prefix}{secrets.token_hex(12)}.tmp"
    staged: str | None = name if directory is not None else os.path.join(path.parent, name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        try:
            descriptor = os.open(staged, flags, 0o600, dir_fd=directory)
        except OSError:
            # A failed exclusive create makes no file, and a name already
            # taken is not this write's file, so there is nothing to remove.
            staged = None
            raise
        try:
            written = 0
            while written < len(data):
                written += os.write(descriptor, data[written:])
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if not hasattr(os, "fchmod"):  # Windows before Python 3.13
            os.chmod(staged, mode, dir_fd=directory)
        if check is not None:
            check()
        os.replace(staged, path.name if directory is not None else path, src_dir_fd=directory, dst_dir_fd=directory)
        staged = None
    finally:
        if staged is not None:
            with suppress(OSError):
                os.unlink(staged, dir_fd=directory)
