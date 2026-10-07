"""Coherent bounded snapshots of project-inspection decision files."""

from __future__ import annotations

import errno
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

_MAX_FILE_BYTES = 1024 * 1024
# Links one lookup may follow (MAXSYMLINKS): 40 on Linux, 32 on macOS and the BSDs.
_MAX_SYMLINKS = 40 if sys.platform.startswith("linux") else 32
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
    # Every directory searched and every link followed to reach a regular file.
    route: tuple[tuple[int, ...], ...] = ()


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

    Decision files and ``.lake`` are read through symlinks, as Lake and elan
    read them.  Links are followed one at a time, and a regular file's capture
    records every directory searched and every link followed, so a change on
    the way is a change to the snapshot, as it is for the root and ``.lake``
    (the root's own ancestors count only if they are replaced).  POSIX opens
    use ``O_NONBLOCK`` and ``O_NOFOLLOW`` beneath the
    directory that walk found.  The file must be regular before it is opened,
    the descriptor is verified as a regular file before any read, the read is
    bounded, and the descriptor identity and the walk are checked afterwards.
    Platforms without those flags still get the same pre/open/post checks.
    """

    try:
        root_before = os.stat(root, follow_symlinks=False)
        root_identity = _node_identity(root_before) if stat.S_ISDIR(root_before.st_mode) else None
    except (OSError, TypeError, ValueError):
        root_identity = None
    lake_before = _directory_generation(root / ".lake")

    aliases = _decision_aliases(root)
    captured = []
    for relative in _DECISION_FILES:
        file = _capture_file(root, relative)
        # Windows reports a child of a non-directory parent as not found. Lake
        # cannot treat that path as absent: a present non-directory `.lake`
        # makes the override path unreadable.
        if (
            relative == _OVERRIDES
            and file.state == "missing"
            and lake_before[0] in {"other", "unreadable"}
        ):
            file = _FileSnapshot("unreadable", lake_before[1])
        captured.append((relative, file))
    files = tuple(captured)
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
    """Capture one decision file through symlinks without ever reading a non-regular node.

    An entry that does not resolve to a regular file, including a dangling or
    looping link, is present but unreadable.  It is identified by the entry
    itself, because a device's times can change while the link to it does not
    (writes touch ``/dev/null`` on macOS).  As in Lake, a file beneath a
    dangling parent link is missing.
    """

    try:
        entry = os.stat(root / relative, follow_symlinks=False)
    except FileNotFoundError:
        return _FileSnapshot("missing")
    except (OSError, TypeError, ValueError):
        return _FileSnapshot("unreadable")
    try:
        # Let the kernel decide native link expansion first. `stat` reads no
        # FIFO/device content, but faithfully enforces raw pending-path limits,
        # repeated separators, `.` components, link loops, and platform rules
        # that a normalized Python component walk cannot reconstruct.
        followed = os.stat(root / relative)
    except (OSError, TypeError, ValueError):
        return _FileSnapshot("unreadable", _node_identity(entry))
    if not stat.S_ISREG(followed.st_mode):
        return _FileSnapshot("unreadable", _node_identity(entry))
    try:
        path, before, route = _resolve(root, relative)
    except (OSError, TypeError, ValueError):
        return _FileSnapshot("unreadable", _node_identity(entry))
    if not stat.S_ISREG(before.st_mode):
        return _FileSnapshot("unreadable", _node_identity(entry))
    before_token = _node_identity(before)
    if _node_identity(followed) != before_token:
        return _FileSnapshot("changed", before_token, route=route)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0)
    descriptor: int | None = None
    try:
        descriptor = _open_resolved(path, flags, route[-1])
        if descriptor is None:
            return _FileSnapshot("changed")
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
        if opened_token != after_token or not _walk_unchanged(root, relative, path, before_token, route):
            return _FileSnapshot("changed", after_token)
        if len(data) > _MAX_FILE_BYTES:
            return _FileSnapshot("unreadable", after_token, route=route)
        return _FileSnapshot("regular", after_token, data, route)
    except (OSError, TypeError, ValueError):
        # A stable permission failure remains comparable across snapshots.  A
        # replacement is caught by repeating the walk, and again in the second pass.
        if _walk_unchanged(root, relative, path, before_token, route):
            return _FileSnapshot("unreadable", before_token, route=route)
        return _FileSnapshot("changed")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _resolve(root: Path, relative: str) -> tuple[Path, os.stat_result, tuple[tuple[int, ...], ...]]:
    """Follow ``relative`` below the resolved ``root`` one component and one link at a time.

    Returns the link-free path of the entry it names, that entry's own
    metadata, and the identities of every directory searched and every link
    followed, ending with the directory that holds the entry.  Lookups follow
    the kernel's rules: a relative target continues from the link's directory,
    an absolute one from ``/``, ``..`` is the parent of the directory reached
    so far, a target ending in ``/`` or ``/.`` must be a directory, an empty
    target names nothing, and one lookup follows at most ``_MAX_SYMLINKS``
    links.  Ancestors of ``root`` are identified by device and inode alone:
    inspection already trusts them to lead to the same root, and entries
    coming and going in a home or temporary directory are not a project
    change.
    """

    ancestors = set(root.parents)
    route: list[tuple[int, ...]] = []
    directory = root
    pending = list(Path(relative).parts)
    links = 0
    while pending:
        holder = os.stat(directory, follow_symlinks=False)
        if not stat.S_ISDIR(holder.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(directory))
        identity = _node_identity(holder)
        route.append(identity[:3] if directory in ancestors else identity)
        name = pending.pop(0)
        if name == ".":
            continue
        if name == "..":
            directory = directory.parent
            continue
        candidate = directory / name
        metadata = os.stat(candidate, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            links += 1
            if links > _MAX_SYMLINKS:
                raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(root / relative))
            route.append(_node_identity(metadata))
            raw = os.readlink(candidate)
            if not raw:
                # Path("") reads as ".", but the kernel finds nothing there.
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(candidate))
            target = Path(raw)
            # Path drops a trailing "/" or "/.", after which only a directory resolves.
            must_be_directory = ["."] if raw.endswith(("/", "/.")) else []
            if target.is_absolute():
                directory = Path(target.anchor)
                pending[:0] = [*target.parts[1:], *must_be_directory]
            else:
                pending[:0] = [*target.parts, *must_be_directory]
            continue
        if not pending:
            return candidate, metadata, tuple(route)
        directory = candidate
    # Only ``.``, ``..`` or a link to ``/`` ends on the directory reached so far.
    raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(directory))


def _walk_unchanged(
    root: Path, relative: str, path: Path, token: tuple[int, ...], route: tuple[tuple[int, ...], ...]
) -> bool:
    try:
        again, metadata, again_route = _resolve(root, relative)
    except (OSError, TypeError, ValueError):
        return False
    return again == path and _node_identity(metadata) == token and again_route == route


def _open_resolved(path: Path, flags: int, holder: tuple[int, ...]) -> int | None:
    """Open a link-free path from ``_resolve`` without following anything swapped in since.

    With POSIX ``dir_fd`` support the directory is opened first and must be
    the one the walk searched (the same device and inode), so a parent
    replaced by a link cannot lead the open anywhere else, and ``O_NOFOLLOW``
    refuses a final link.  Where the platform can, the directory is opened
    for search only (``O_PATH`` or ``O_SEARCH``), so it needs search
    permission and not read permission, as in a lookup by path.  Returns
    ``None`` when the directory changed.
    """

    if os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"):
        return os.open(path, flags)
    directory_flags = (
        (getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0) or os.O_RDONLY)
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory = os.open(path.parent, directory_flags)
    try:
        if _node_identity(os.fstat(directory))[:3] != holder[:3]:
            return None
        return os.open(path.name, flags, dir_fd=directory)
    finally:
        os.close(directory)


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
            lake_entries = _list_directory(root / ".lake")
            override = Path(_OVERRIDES).name
            aliases.append(
                (_OVERRIDES, tuple(sorted(entry for entry in lake_entries if entry.casefold() == override.casefold())))
            )
        except (OSError, TypeError, ValueError):
            aliases.append((_OVERRIDES, ("<unreadable>",)))
    else:
        aliases.append((_OVERRIDES, ()))
    return tuple(aliases)


def _list_directory(path: Path) -> list[str]:
    """List a directory through symlinks without opening any other kind of node."""

    if os.listdir not in os.supports_fd or not hasattr(os, "O_DIRECTORY"):
        if not stat.S_ISDIR(os.stat(path).st_mode):
            raise OSError(path)
        return os.listdir(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(path)
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
    """Identify a directory through symlinks, and anything else by its own entry."""

    try:
        metadata = os.stat(path)
        if not stat.S_ISDIR(metadata.st_mode):
            return "other", _node_identity(os.stat(path, follow_symlinks=False))
    except FileNotFoundError:
        return "missing", None
    except (OSError, TypeError, ValueError):
        return "unreadable", None
    return "directory", _node_identity(metadata)


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
