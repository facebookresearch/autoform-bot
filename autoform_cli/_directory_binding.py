"""Retain and verify one directory root generation."""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path


DIRECTORY_BINDING_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_follow_symlinks", ())
)

_ROOT_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_RETRYABLE_OPEN_ERRNOS = frozenset(
    value
    for value in (
        errno.ENOENT,
        errno.ENOTDIR,
        errno.ELOOP,
        getattr(errno, "ESTALE", None),
    )
    if value is not None
)


class DirectoryChangedError(OSError):
    """The directory root changed while it was opened or verified; a retry may succeed."""


@dataclass(slots=True)
class RetainedDirectory:
    """A directory root retained by one open descriptor."""

    path: Path
    _descriptor: int
    _identity: tuple[int, int]
    _closed: bool = False

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise OSError("directory binding is closed")
        return self._descriptor

    @property
    def identity(self) -> tuple[int, int]:
        if self._closed:
            raise OSError("directory binding is closed")
        return self._identity

    def verify(self) -> None:
        """Verify that the path still names the retained directory."""

        if self._closed:
            raise OSError("directory binding is closed")
        if not _same_directory(
            os.fstat(self._descriptor),
            os.stat(self.path, follow_symlinks=False),
            self._identity,
        ):
            raise DirectoryChangedError("directory root changed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _close_quietly(self._descriptor)

    def __del__(self) -> None:
        self.close()


def lexical_absolute_path(path: str | Path) -> Path:
    """Make *path* absolute without erasing ``..`` before validation."""

    expanded = Path(path).expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def open_directory(path: str | Path) -> RetainedDirectory:
    """Open *path* as a retained directory root.

    The kernel resolves the ancestors, so they may be symbolic links and need
    only search permission.  The root itself must not be a symbolic link.
    """

    if not DIRECTORY_BINDING_SUPPORTED:
        raise OSError("this platform cannot retain directory descriptors safely")
    absolute = lexical_absolute_path(path)
    try:
        descriptor = os.open(absolute, _ROOT_FLAGS)
    except ValueError as error:
        raise OSError("directory path is invalid") from error
    except OSError as error:
        # Concurrent rename/type changes commonly present as one of these at
        # the first bind.  A stable symlink at the root remains a lasting,
        # specifically diagnosed refusal rather than a retryable race.
        if error.errno in _RETRYABLE_OPEN_ERRNOS:
            if _root_is_symlink(absolute):
                raise OSError("directory root must not be a symbolic link") from error
            raise DirectoryChangedError(
                "directory root changed while it was opened"
            ) from error
        raise _open_failure(absolute, error) from error
    binding: RetainedDirectory | None = None
    try:
        opened = os.fstat(descriptor)
        named = os.stat(absolute, follow_symlinks=False)
        identity = (opened.st_dev, opened.st_ino)
        if _same_directory(opened, named, identity):
            binding = RetainedDirectory(absolute, descriptor, identity)
    except OSError as error:
        raise DirectoryChangedError("directory root changed while it was opened") from error
    finally:
        if binding is None:
            _close_quietly(descriptor)
    if binding is None:
        raise DirectoryChangedError("directory root changed while it was opened")
    return binding


def _same_directory(
    opened: os.stat_result,
    named: os.stat_result,
    identity: tuple[int, int],
) -> bool:
    return (
        stat.S_ISDIR(opened.st_mode)
        and stat.S_ISDIR(named.st_mode)
        and (opened.st_dev, opened.st_ino) == identity
        and (named.st_dev, named.st_ino) == identity
    )


def _open_failure(path: Path, error: OSError) -> OSError:
    """Name why the root was refused without echoing the host path."""

    if _root_is_symlink(path):
        return OSError("directory root must not be a symbolic link")
    if isinstance(error, PermissionError):
        return OSError("permission denied while opening the directory root")
    if isinstance(error, FileNotFoundError):
        return OSError("directory root does not exist")
    if isinstance(error, NotADirectoryError):
        return OSError("directory root is not a directory")
    return OSError("directory root cannot be opened")


def _root_is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(os.stat(path, follow_symlinks=False).st_mode)
    except (OSError, ValueError):
        return False


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


__all__ = [
    "DIRECTORY_BINDING_SUPPORTED",
    "DirectoryChangedError",
    "RetainedDirectory",
    "lexical_absolute_path",
    "open_directory",
]
