"""Check that a library's index describes the checkout the project builds against.

The index comes from another repository. It is used only when the checkout is
at the locked commit, its sources and pin files are the ones the index was
generated from, and the index as a whole is one generation.

Only each module's ``name``, ``source_file`` and ``sha256`` and the header's
toolchain, dependencies and source directories are checked. An edit to the
index file that leaves those alone is not detected: a declaration record, a
module's ``module_doc`` or ``module_system``, or the header's ``generator``,
``probe`` or ``package``. Neither is a package replaced with
``lake --packages=``.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .._tree_snapshot import TreeCaptureLimits, TreeSelection, TreeSnapshotError, bind_directory_tree
from ..project import _snapshot
from ..project._snapshot import MANIFEST
from ..project.catalog import canonical_lean_toolchain
from .index import (
    INDEX_FILE,
    MAX_INDEX_BYTES,
    TREE_FILES,
    IndexDependency,
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
    PinError,
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


@dataclass(frozen=True, slots=True)
class CheckoutPins:
    """What a package root pins: the files the tree digest covers, decoded once."""

    lean_toolchain: str | None
    #: The direct dependencies, in name order.
    dependencies: tuple[IndexDependency, ...]
    #: The tree files that exist, as ``tree_digest`` takes them.
    files: tuple[tuple[str, bytes], ...]
    #: The package's own name, from its manifest.
    package: str | None


def checkout_pins(root: Path) -> CheckoutPins:
    """Read the pins of the package at ``root``; ``WorkspaceError`` when its manifest cannot be used."""

    for relative in TREE_FILES:
        _require_plain_file(root, relative)
    pins = _snapshot.capture_decision_snapshot(root)
    for relative in TREE_FILES:
        # Only a file that is not there is absent; one that cannot be read must not leave the digest.
        if pins.file(relative).state not in ("missing", "regular"):
            raise PinError(f"its {relative} cannot be read (it is over 1 MiB or unreadable)")
    decoded = locked_packages(pins, MANIFEST)
    payload, packages = decoded if decoded is not None else ({}, ())
    name = payload.get("name")
    return CheckoutPins(
        lean_toolchain=toolchain(pins),
        dependencies=tuple(
            IndexDependency(item.name, item.type, item.rev)
            for item in sorted(packages, key=lambda item: item.name)
            if not item.inherited
        ),
        files=tuple(
            (relative, pins.file(relative).content)
            for relative in TREE_FILES
            if pins.file(relative).state == "regular" and pins.file(relative).content is not None
        ),
        package=name if type(name) is str and name else None,
    )


def _require_plain_file(root: Path, relative: str) -> None:
    """Refuse a pin file that is a link or not a regular file; the checkout is not trusted to link elsewhere."""

    try:
        mode = os.lstat(root / relative).st_mode
    except FileNotFoundError:
        return
    except OSError:
        raise PinError(f"its {relative} cannot be read (it is over 1 MiB or unreadable)") from None
    if stat.S_ISLNK(mode):
        raise PinError(f"its {relative} is a symbolic link")
    if not stat.S_ISREG(mode):
        raise PinError(f"its {relative} is not a regular file")


def load_library(
    workspace: LakeWorkspace, library: str, index_path: Path | None = None, *, index: LibraryIndex | None = None
) -> VerifiedLibrary:
    """Return ``library``'s index once every check against its checkout has passed.

    ``index`` is the checkout's index when the caller has already read and parsed it, so it is not read
    again; every other check still runs.
    """

    try:
        return _load_library(workspace, library, index_path, index)
    except OSError:
        raise LibraryError(library, "its checkout cannot be read") from None


def _load_library(
    workspace: LakeWorkspace, library: str, index_path: Path | None, index: LibraryIndex | None = None
) -> VerifiedLibrary:
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
        index = parse_index(_index_bytes(path)) if index is None else index
    except FileNotFoundError:
        raise refuse(f"it has no index ({path.name})") from None
    except PermissionError:
        raise refuse("its index cannot be read") from None
    except OSError:
        raise refuse("its index is not a regular file") from None
    except LibraryIndexError as error:
        raise refuse(f"its index is not usable: {error}") from None

    try:
        pins = checkout_pins(root)
    except PinError as error:
        raise refuse(str(error)) from None
    except WorkspaceError:
        raise refuse(f"its {MANIFEST} is not a Lake manifest Autoform reads") from None
    if pins.lean_toolchain != index.header.lean_toolchain:
        generated, pins_to = shorten(index.header.lean_toolchain), shorten(pins.lean_toolchain or "nothing")
        raise refuse(f"its index was generated for {generated}, but the checkout pins {pins_to}")
    if pins.dependencies != index.header.dependencies:
        raise refuse(f"its index does not list the dependencies its {MANIFEST} locks")

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
    if tree_digest(((module.name, module.sha256) for module in index.modules), pins.files) != index.header.tree:
        raise refuse("its index does not match its sources and pins as a whole; it was merged or edited, not generated")
    return VerifiedLibrary(name, head.lower(), index, _differences(workspace, index))


def _index_bytes(path: Path) -> bytes:
    """Read an index that is a regular file, following no symbolic link."""

    descriptor = os.open(path, os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOCTTY", 0)
        | getattr(os, "O_BINARY", 0),
    )
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


__all__ = ["CheckoutPins", "Difference", "VerifiedLibrary", "checkout_pins", "load_library"]
