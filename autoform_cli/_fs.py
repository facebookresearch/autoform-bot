"""Filesystem path helpers shared by the blueprint readers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

#: Each path resolved inside :func:`resolving_once`, keyed by its spelling:
#: Windows paths compare without case, and a missing file keeps the case it was given.
_RESOLVED: ContextVar[dict[str, Path] | None] = ContextVar("_RESOLVED", default=None)


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
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True
