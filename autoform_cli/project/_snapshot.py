"""Coherent bounded snapshots of project-inspection decision files."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

_MAX_FILE_BYTES = 1024 * 1024
_ROOT_MARKERS = ("lakefile.lean", "lakefile.toml", "lean-toolchain")
_MANIFEST = "lake-manifest.json"
_OVERRIDES = ".lake/package-overrides.json"
_DECISION_FILES = (*_ROOT_MARKERS, _MANIFEST, _OVERRIDES)
_WINDOWS_STAT_VIEWS = os.name == "nt"


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    """One bounded decision-file read, including its filesystem generation."""

    state: str
    identity: tuple[int, ...] | None = None
    content: bytes | None = None


@dataclass(frozen=True, slots=True)
class _DecisionSnapshot:
    """The small set of bytes from which an inspection answer is derived."""

    root_identity: tuple[int, ...] | None
    lake_directory: tuple[str, tuple[int, ...] | None]
    aliases: tuple[tuple[str, tuple[str, ...]], ...]
    files: tuple[tuple[str, _FileSnapshot], ...]

    @property
    def stable(self) -> bool:
        return (
            self.root_identity is not None
            and self.lake_directory[0] != "changed"
            and all(name != "<root>" or "<unreadable>" not in entries for name, entries in self.aliases)
            and all(entry.state != "changed" for _, entry in self.files)
        )

    def file(self, relative: str) -> _FileSnapshot:
        return dict(self.files)[relative]


def _capture_decision_snapshot(root: Path) -> _DecisionSnapshot:
    """Read all decision files once; callers re-read and compare the full value.

    POSIX opens use ``O_NONBLOCK`` and ``O_NOFOLLOW``.  The descriptor is
    verified as a regular file before any read, the read is bounded, and both
    descriptor and pathname identities are checked afterwards.  Platforms
    without those flags still get the same pre/open/post identity checks.
    """

    try:
        root_before = os.stat(root, follow_symlinks=False)
        root_identity = _node_identity(root_before) if stat.S_ISDIR(root_before.st_mode) else None
    except (OSError, TypeError, ValueError):
        root_identity = None
    lake_before = _directory_generation(root / ".lake")

    aliases = _decision_aliases(root)
    files = tuple((relative, _capture_file(root, relative)) for relative in _DECISION_FILES)
    try:
        root_after = os.stat(root, follow_symlinks=False)
        if _node_identity(root_after) != root_identity:
            root_identity = None
    except (OSError, TypeError, ValueError):
        root_identity = None
    lake_after = _directory_generation(root / ".lake")
    lake_directory = lake_before if lake_before == lake_after else ("changed", None)
    return _DecisionSnapshot(root_identity, lake_directory, aliases, files)


def _capture_file(root: Path, relative: str) -> _FileSnapshot:
    """Capture one decision file without ever reading a non-regular node."""

    path = root / relative
    parent_tokens: list[tuple[Path, tuple[int, ...]]] = []
    try:
        parent = root
        for part in Path(relative).parts[:-1]:
            parent = parent / part
            metadata = os.stat(parent, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                return _FileSnapshot("unreadable", _node_identity(metadata))
            parent_tokens.append((parent, _node_identity(metadata)))
        before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return _FileSnapshot("missing")
    except (OSError, TypeError, ValueError):
        return _FileSnapshot("unreadable")
    before_token = _node_identity(before)
    if not stat.S_ISREG(before.st_mode):
        return _FileSnapshot("unreadable", before_token)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = _open_beneath(root, relative, flags)
        opened = os.fstat(descriptor)
        opened_token = _node_identity(opened)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _cross_interface_identity(opened_token)
            != _cross_interface_identity(before_token)
        ):
            return _FileSnapshot("changed", opened_token)
        chunks: list[bytes] = []
        remaining = _MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after_token = _node_identity(os.fstat(descriptor))
        try:
            path_token = _node_identity(os.stat(path, follow_symlinks=False))
            parents_unchanged = all(
                _node_identity(os.stat(parent, follow_symlinks=False)) == token for parent, token in parent_tokens
            )
        except (OSError, TypeError, ValueError):
            return _FileSnapshot("changed", after_token)
        if (
            opened_token != after_token
            or before_token != path_token
            or not parents_unchanged
        ):
            return _FileSnapshot("changed", after_token)
        if len(data) > _MAX_FILE_BYTES:
            return _FileSnapshot("unreadable", after_token)
        return _FileSnapshot("regular", after_token, data)
    except (OSError, TypeError, ValueError):
        # A stable permission failure remains comparable across snapshots.  A
        # replacement is caught by the pathname identity in the second pass.
        try:
            current = os.stat(path, follow_symlinks=False)
            current_token = _node_identity(current)
        except (OSError, TypeError, ValueError):
            return _FileSnapshot("changed")
        return _FileSnapshot("unreadable", current_token) if current_token == before_token else _FileSnapshot("changed")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_beneath(root: Path, relative: str, flags: int) -> int:
    """Open a file below ``root`` without following parent links when POSIX supports it."""

    if os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"):
        return os.open(root / relative, flags)
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent = os.open(root, directory_flags)
    try:
        for part in Path(relative).parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(Path(relative).name, flags, dir_fd=parent)
    finally:
        os.close(parent)


def _decision_aliases(root: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Snapshot relevant directory entries so case-only/presence changes retry."""

    top_names = (*_ROOT_MARKERS, _MANIFEST, ".lake")
    try:
        top_entries = os.listdir(root)
    except (OSError, TypeError, ValueError):
        return (("<root>", ("<unreadable>",)),)
    aliases = [
        (name, tuple(sorted(entry for entry in top_entries if entry.casefold() == name.casefold())))
        for name in top_names
    ]
    lake_aliases = next(values for name, values in aliases if name == ".lake")
    if lake_aliases:
        try:
            lake_entries = _list_directory_beneath(root, ".lake")
            override = Path(_OVERRIDES).name
            aliases.append(
                (_OVERRIDES, tuple(sorted(entry for entry in lake_entries if entry.casefold() == override.casefold())))
            )
        except (OSError, TypeError, ValueError):
            aliases.append((_OVERRIDES, ("<unreadable>",)))
    else:
        aliases.append((_OVERRIDES, ()))
    return tuple(aliases)


def _list_directory_beneath(root: Path, relative: str) -> list[str]:
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"):
        metadata = os.stat(root / relative, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(relative)
        return os.listdir(root / relative)
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = _open_beneath(root, relative, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(relative)
        return os.listdir(descriptor)
    finally:
        os.close(descriptor)


def _node_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _cross_interface_identity(identity: tuple[int, ...]) -> tuple[int, ...]:
    """Normalize fields Windows exposes differently through stat and fstat.

    Path ``stat`` reports birth time as ``st_ctime_ns`` while descriptor
    ``fstat`` reports filesystem change time. Comparisons within either
    interface retain the complete identity; only the admission comparison
    between those two views omits that field.
    """

    return identity[:-1] if _WINDOWS_STAT_VIEWS else identity


def _directory_generation(path: Path) -> tuple[str, tuple[int, ...] | None]:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return "missing", None
    except (OSError, TypeError, ValueError):
        return "unreadable", None
    return ("directory" if stat.S_ISDIR(metadata.st_mode) else "other", _node_identity(metadata))


def _path_present(path: Path) -> bool:
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
