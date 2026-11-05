"""Bounded reads of the decision files a project-inspection answer comes from."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

_MAX_FILE_BYTES = 1024 * 1024
ROOT_MARKERS = ("lakefile.lean", "lakefile.toml", "lean-toolchain")
MANIFEST = "lake-manifest.json"
OVERRIDES = ".lake/package-overrides.json"
_DECISION_FILES = (*ROOT_MARKERS, MANIFEST, OVERRIDES)


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    """One decision file: missing, unreadable, or regular with its bytes."""

    state: str
    content: bytes | None = None


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    """The small set of bytes from which an inspection answer is derived."""

    files: tuple[tuple[str, _FileSnapshot], ...]

    def file(self, relative: str) -> _FileSnapshot:
        return dict(self.files)[relative]


def capture_decision_snapshot(root: Path) -> DecisionSnapshot:
    """Read every decision file once; inspect and read_workspace capture again and compare the two."""

    return DecisionSnapshot(tuple((relative, _capture_file(root, relative)) for relative in _DECISION_FILES))


def _capture_file(root: Path, relative: str) -> _FileSnapshot:
    """Capture one decision file through symlinks, as Lake and elan read it, never reading a non-regular node.

    An entry that does not resolve to a regular file, including a dangling or
    looping link, is present but unreadable. As in Lake, a file beneath a
    dangling parent link is missing.
    """

    path = root / relative
    if not path_present(path):
        # Windows reports a child of a non-directory parent as not found. Lake
        # cannot treat that path as absent: a present non-directory `.lake`
        # makes the override path unreadable.
        return _FileSnapshot("unreadable" if relative == OVERRIDES and _not_a_directory(root / ".lake") else "missing")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOCTTY", 0)
    try:
        # `stat` reads no FIFO or device content. O_NONBLOCK keeps the open of a
        # node swapped in since from blocking, and fstat refuses it before any read.
        if not stat.S_ISREG(os.stat(path).st_mode):
            return _FileSnapshot("unreadable")
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return _FileSnapshot("unreadable")
            chunks: list[bytes] = []
            remaining = _MAX_FILE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except (OSError, TypeError, ValueError):
        return _FileSnapshot("unreadable")
    data = b"".join(chunks)
    return _FileSnapshot("regular", data) if len(data) <= _MAX_FILE_BYTES else _FileSnapshot("unreadable")


def _not_a_directory(path: Path) -> bool:
    """Whether *path* exists, following symlinks, but does not stat as a directory."""

    try:
        return not stat.S_ISDIR(os.stat(path).st_mode)
    except FileNotFoundError:
        return False
    except (OSError, TypeError, ValueError):
        return True


def path_present(path: Path) -> bool:
    try:
        os.stat(path, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except (OSError, TypeError, ValueError):
        # An unreadable marker must keep this directory in consideration; a
        # later safe capture will report the specific failure. Python 3.14's
        # Path.exists()/is_symlink() suppress these errors, so do not use them.
        return True
