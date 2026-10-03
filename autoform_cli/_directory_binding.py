"""Retain and verify one lexical absolute directory generation."""

from __future__ import annotations

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

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


@dataclass(slots=True)
class RetainedDirectory:
    """A lexical absolute directory chain retained by open descriptors."""

    path: Path
    descriptors: tuple[int, ...]
    identities: tuple[tuple[int, int], ...]
    _closed: bool = False

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise OSError("directory binding is closed")
        return self.descriptors[-1]

    @property
    def identity(self) -> tuple[int, int]:
        if self._closed:
            raise OSError("directory binding is closed")
        return self.identities[-1]

    def verify(self) -> None:
        """Verify that every retained component still selects its bound inode."""

        if self._closed or len(self.descriptors) != len(self.identities):
            raise OSError("directory binding is incomplete")
        anchor = self.path.anchor
        if not anchor:
            raise OSError("directory path is not absolute")
        opened = os.fstat(self.descriptors[0])
        named = os.stat(anchor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != self.identities[0]
            or (named.st_dev, named.st_ino) != self.identities[0]
        ):
            raise OSError("directory anchor changed")
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
                raise OSError("directory component changed")

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


def lexical_absolute_path(path: str | Path) -> Path:
    """Make *path* absolute without erasing ``..`` before validation."""

    expanded = Path(path).expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def open_directory(path: str | Path) -> RetainedDirectory:
    """Bind every component of *path* without following symbolic links."""

    if not DIRECTORY_BINDING_SUPPORTED:
        raise OSError("this platform cannot retain directory descriptors safely")
    absolute = lexical_absolute_path(path)
    descriptors: list[int] = []
    identities: list[tuple[int, int]] = []
    try:
        anchor = absolute.anchor
        if not anchor:
            raise OSError("directory path is not absolute")
        descriptor = os.open(anchor, _DIRECTORY_FLAGS)
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        named = os.stat(anchor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("directory anchor changed")
        identities.append((opened.st_dev, opened.st_ino))
        for part in absolute.parts[1:]:
            expected = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(expected.st_mode):
                raise OSError("directory component is not a directory")
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
                raise OSError("directory component changed")
            identities.append(identity)
            descriptor = child
        binding = RetainedDirectory(absolute, tuple(descriptors), tuple(identities))
        binding.verify()
        return binding
    except BaseException as error:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if isinstance(error, (OSError, ValueError, NotImplementedError)):
            raise OSError(
                "directory path must not contain a symbolic link or change while opening"
            ) from error
        raise


__all__ = [
    "DIRECTORY_BINDING_SUPPORTED",
    "RetainedDirectory",
    "lexical_absolute_path",
    "open_directory",
]
