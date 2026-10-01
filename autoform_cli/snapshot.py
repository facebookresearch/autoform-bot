"""Read a blueprint's inputs once, into bytes everything else derives from.

A command that parses, judges, or publishes several files describes one state
of them only if it never reads a file twice. A file read again to show it, copy
it, or hash it can hold bytes the earlier parse never saw, and the result then
pairs facts from two states that never coexisted on disk. So inputs are read
once, through ``read_regular_file``, into a ``BlueprintSnapshot``, and the graph,
source passages, published pages, and revision hashes are all computed from
those bytes rather than from the tree.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

#: The largest single input read into memory.
FILE_LIMIT = 64 * 1024 * 1024


class SnapshotError(ValueError):
    """An input could not be captured as one stable regular file."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class BlueprintSnapshot:
    """Captured file bytes, keyed by canonical absolute path."""

    files: Mapping[Path, bytes] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))

    def text(self, path: Path) -> str:
        """Return a captured file as UTF-8 text."""

        return self.files[path].decode("utf-8")


def read_regular_file(path: Path, *, label: str) -> bytes | None:
    """Read a bounded regular file twice, refusing symlinks and concurrent change.

    Returns ``None`` when nothing is at ``path``. ``label`` names the kind of
    input in error messages.
    """

    captured = _read_pass(path, label=label, keep_content=True)
    verified = _read_pass(path, label=label, keep_content=False)
    if (None if captured is None else captured[1]) != (None if verified is None else verified[1]):
        raise SnapshotError([f"{label} changed while it was read: {path}"])
    return None if captured is None else captured[0]


def _read_pass(path: Path, *, label: str, keep_content: bool) -> tuple[bytes, tuple[int, str]] | None:
    """Read one bounded regular-file pass."""

    try:
        path_metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SnapshotError([f"cannot inspect {label} {path}: {exc}"]) from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise SnapshotError([f"{label} is not a regular file: {path}"])
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SnapshotError([f"{label} is not a regular file: {path}"]) from exc
        raise SnapshotError([f"cannot open {label} {path}: {exc}"]) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SnapshotError([f"{label} is not a regular file: {path}"])
        # The file opened must be the one inspected, not whatever replaced it.
        if (metadata.st_dev, metadata.st_ino) != (path_metadata.st_dev, path_metadata.st_ino):
            raise SnapshotError([f"{label} changed while it was read: {path}"])
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        size = 0
        while True:
            block = os.read(descriptor, 64 * 1024)
            if not block:
                break
            size += len(block)
            if size > FILE_LIMIT:
                raise SnapshotError([f"{label} exceeds the {FILE_LIMIT}-byte limit: {path}"])
            digest.update(block)
            if keep_content:
                chunks.append(block)
    except OSError as exc:
        raise SnapshotError([f"cannot read {label} {path}: {exc}"]) from exc
    finally:
        os.close(descriptor)
    return b"".join(chunks), (size, digest.hexdigest())


__all__ = ["FILE_LIMIT", "BlueprintSnapshot", "SnapshotError", "read_regular_file"]
