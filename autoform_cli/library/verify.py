"""Check that a library's index describes the checkout the project builds against.

The index comes from another repository. It is used only when the checkout is
at the locked commit, its sources and pin files are the ones the index was
generated from, and the index as a whole is one generation.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .._tree_snapshot import TreeCaptureLimits, TreeSelection, TreeSnapshotError, bind_directory_tree
from ..project import _snapshot
from ..project._snapshot import _MANIFEST
from ..project.catalog import canonical_lean_toolchain
from .index import (
    INDEX_FILE,
    MAX_INDEX_BYTES,
    TREE_FILES,
    LibraryIndex,
    LibraryIndexError,
    covers,
    module_digest,
    parse_index,
    shorten,
    tree_digest,
)
from .locate import (
    HeadError,
    LakeWorkspace,
    LibraryError,
    WorkspaceError,
    checkout_paths,
    find_package,
    locked_packages,
    resolve_head,
    toolchain,
)

#: Far above any library today; a bound so a hostile checkout cannot hold the reader.
_SOURCE_LIMITS = TreeCaptureLimits(max_entries=200_000, max_total_bytes=1024 * 1024 * 1024)
_NAMED = 5


@dataclass(frozen=True, slots=True)
class Difference:
    """One pin the library was indexed with that the project does not share."""

    what: str
    name: str | None
    library: str | None
    project: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {"what": self.what, "name": self.name, "library": self.library, "project": self.project}


@dataclass(frozen=True, slots=True)
class VerifiedLibrary:
    name: str
    revision: str
    index: LibraryIndex
    differences: tuple[Difference, ...]


def load_library(workspace: LakeWorkspace, library: str, index_path: Path | None = None) -> VerifiedLibrary:
    """Return ``library``'s index once every check against its checkout has passed."""

    try:
        return _load_library(workspace, library, index_path)
    except OSError:
        raise LibraryError(library, "its checkout cannot be read") from None


def _load_library(workspace: LakeWorkspace, library: str, index_path: Path | None) -> VerifiedLibrary:
    package = find_package(workspace, library)
    name = package.name

    def refuse(reason: str) -> LibraryError:
        return LibraryError(library, reason)

    try:
        checkout, root = checkout_paths(workspace, package)
    except LibraryError as error:
        raise refuse(error.reason) from None
    try:
        head = resolve_head(checkout)
    except HeadError as error:
        raise refuse(str(error)) from None
    if head.lower() != (package.rev or "").lower():
        raise refuse(f"its checkout is at {head[:12]}, not the locked revision {(package.rev or '')[:12]}")

    path = root / INDEX_FILE if index_path is None else Path(index_path)
    try:
        index = parse_index(_index_bytes(path))
    except FileNotFoundError:
        raise refuse(f"it has no index ({path.name})") from None
    except OSError:
        raise refuse("its index is not a regular file") from None
    except LibraryIndexError as error:
        raise refuse(f"its index is not usable: {error}") from None

    pins = _snapshot._capture_decision_snapshot(root)
    pinned = toolchain(pins)
    if pinned != index.header.lean_toolchain:
        generated, pins_to = shorten(index.header.lean_toolchain), shorten(pinned or "nothing")
        raise refuse(f"its index was generated for {generated}, but the checkout pins {pins_to}")
    try:
        decoded = locked_packages(pins, _MANIFEST)
    except WorkspaceError:
        raise refuse(f"its {_MANIFEST} is not a Lake manifest Autoform reads") from None
    direct = sorted(
        (item.name, item.type, item.rev) for item in (decoded[1] if decoded is not None else ()) if not item.inherited
    )
    if direct != [(item.name, item.type, item.rev) for item in index.header.dependencies]:
        raise refuse(f"its index does not list the dependencies its {_MANIFEST} locks")

    found = _sources(root, index.header.source_dirs, refuse)
    expected = {module.source_file: module.sha256 for module in index.modules}
    _same(
        sorted(found.keys() - expected.keys()),
        "Lean file is not in its index",
        "Lean files are not in its index",
        refuse,
    )
    _same(
        sorted(expected.keys() - found.keys()),
        "indexed source file is missing",
        "indexed source files are missing",
        refuse,
    )
    _same(
        sorted(path for path in expected if found[path] != expected[path]),
        "source file differs from its index",
        "source files differ from its index",
        refuse,
    )
    files = [
        (relative, pins.file(relative).content)
        for relative in TREE_FILES
        if pins.file(relative).state == "regular" and pins.file(relative).content is not None
    ]
    if tree_digest(((module.name, module.sha256) for module in index.modules), files) != index.header.tree:
        raise refuse("its index does not match its sources and pins as a whole; it was merged or edited, not generated")
    return VerifiedLibrary(name, head.lower(), index, _differences(workspace, index))


def _index_bytes(path: Path) -> bytes:
    """Read an index that is a regular file, following no symbolic link."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("not a regular file")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            # One byte over the limit is enough for the parser to refuse by size.
            return stream.read(MAX_INDEX_BYTES + 1)
    finally:
        os.close(descriptor)


def _sources(root: Path, source_dirs: tuple[str, ...], refuse) -> dict[str, str]:
    """Return the digest of every Lean file the source directories hold."""

    def inside(path: PurePosixPath) -> bool:
        return covers(source_dirs, path.as_posix())

    def reaches(path: PurePosixPath) -> bool:
        # A source directory may be nested, so its parents are walked as well.
        text = path.as_posix()
        return any(
            text == directory or text.startswith(f"{directory}/") or directory.startswith(f"{text}/")
            for directory in source_dirs
        )

    def include(path: PurePosixPath, mode: int) -> bool:
        # Captured so they can be refused: a link on, above or under a source directory, and a special file under one.
        if stat.S_ISLNK(mode):
            return inside(path) or reaches(path)
        return inside(path) and (path.suffix == ".lean" or not stat.S_ISREG(mode))

    selection = TreeSelection(
        include=include,
        descend=reaches,
        record_omitted=False,
        limits=_SOURCE_LIMITS,
    )
    try:
        with bind_directory_tree(root, selection=selection) as bound:
            snapshot = bound.capture()
    except TreeSnapshotError as error:
        raise refuse(f"its sources cannot be read safely: {shorten(error)}") from None
    if snapshot.symlinks:
        relative = snapshot.symlinks[0][0]
        if inside(PurePosixPath(relative)):
            raise refuse(f"a source directory holds the symbolic link {shorten(relative)}")
        raise refuse(f"the source directory {shorten(relative)} is a symbolic link")
    if snapshot.special:
        raise refuse(f"a source directory holds {shorten(snapshot.special[0][0])}, which is not a regular file")
    return {relative: module_digest(data) for relative, data in snapshot.files if inside(PurePosixPath(relative))}


def _same(paths: list[str], one: str, many: str, refuse) -> None:
    if not paths:
        return
    named = ", ".join(shorten(path) for path in paths[:_NAMED]) + (", ..." if len(paths) > _NAMED else "")
    raise refuse(f"{len(paths)} {one if len(paths) == 1 else many}: {named}")


def _differences(workspace: LakeWorkspace, index: LibraryIndex) -> tuple[Difference, ...]:
    """Where the project's pins differ from the ones the library was indexed with.

    Lake builds a dependency with the project's toolchain and the project's
    own requirement wins over the library's lock, so a difference does not
    mean the library fails to build. It is reported, never refused.
    """

    differences: list[Difference] = []
    theirs, ours = index.header.lean_toolchain, workspace.lean_toolchain
    if ours is None or canonical_lean_toolchain(theirs) != canonical_lean_toolchain(ours):
        differences.append(Difference("lean_toolchain", None, theirs, ours))
    locked = {package.name: package for package in workspace.packages}
    for dependency in index.header.dependencies:
        package = locked.get(dependency.name)
        project = package.rev if package is not None else None
        if package is None or package.type != dependency.type or project != dependency.rev:
            differences.append(Difference("dependency", dependency.name, dependency.rev, project))
    return tuple(differences)


__all__ = ["Difference", "VerifiedLibrary", "load_library"]
