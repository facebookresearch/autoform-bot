"""Find a locked Lake package's checkout by reading files only.

Lake clones a Git dependency into ``<packagesDir>/<name>`` and records the
commit in ``lake-manifest.json``. Nothing here runs Lake or Git, so what they
would report is read from the files they read.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from ..project import _snapshot
from ..project._lake_metadata import (
    _JsonInteger,
    _canonical_toml_name,
    _decode_package_entry,
    _json_default,
    _json_optional,
    _manifest_layout,
    _reject_json_constant,
    _validate_manifest_root,
)
from ..project._snapshot import _DecisionSnapshot, _MANIFEST, _OVERRIDES
from ..project.inspect import _trim_elan_whitespace
from .index import relative_path, shorten

_DEFAULT_PACKAGES_DIR = ".lake/packages"
_SNAPSHOT_ATTEMPTS = 3
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REFTABLE = re.compile(r"(?im)^\s*refstorage\s*=\s*reftable\s*$")
_MAX_REF_BYTES = 4096
_MAX_GIT_FILE_BYTES = 16 * 1024 * 1024


class WorkspaceError(ValueError):
    """The project's own Lake files cannot tell where its packages are."""


class LibraryError(ValueError):
    """One named library cannot be searched; the message says which and why."""

    def __init__(self, library: str, reason: str) -> None:
        super().__init__(f"library {library}: {reason}")
        self.library = library
        self.reason = reason


class HeadError(ValueError):
    """A checkout's commit cannot be read from its files."""

    def __init__(self, reason: str, *, needs_git: bool = False) -> None:
        super().__init__(reason)
        self.needs_git = needs_git


@dataclass(frozen=True, slots=True)
class LockedPackage:
    #: As the project's ``require`` names it, without Lean's quoting marks.
    name: str
    type: str
    rev: str | None
    sub_dir: str | None
    inherited: bool


@dataclass(frozen=True, slots=True)
class LakeWorkspace:
    root: Path
    lean_toolchain: str | None
    packages_dir: str
    packages: tuple[LockedPackage, ...]
    #: Packages ``.lake/package-overrides.json`` replaces. The manifest does not record this.
    overridden: frozenset[str]


def read_workspace(root: str | Path) -> LakeWorkspace:
    """Read the files that decide which packages ``root`` builds against, as one generation."""

    directory = Path(root).expanduser().resolve()
    if not directory.is_dir():
        raise WorkspaceError("Lean root does not exist or is not a directory")
    for _attempt in range(_SNAPSHOT_ATTEMPTS):
        snapshot = _snapshot._capture_decision_snapshot(directory)
        if snapshot.stable and snapshot == _snapshot._capture_decision_snapshot(directory):
            return _workspace(directory, snapshot)
    raise WorkspaceError("project configuration changed while it was being read; retry after the project is idle")


def _workspace(root: Path, snapshot: _DecisionSnapshot) -> LakeWorkspace:
    decoded = locked_packages(snapshot, _MANIFEST)
    if decoded is None:
        raise WorkspaceError("the Lean root has no lake-manifest.json; run `lake update` first")
    payload, packages = decoded
    try:
        _validate_manifest_root(payload)
        lake_dir = _json_default(payload, "lakeDir", ".lake", str)
        packages_dir = _json_optional(payload, "packagesDir", str) or _DEFAULT_PACKAGES_DIR
    except ValueError:
        raise WorkspaceError(f"{_MANIFEST} is not a Lake manifest Autoform reads") from None
    if lake_dir not in (".lake", "./.lake"):
        raise WorkspaceError(f"{_MANIFEST} sets lakeDir to {shorten(repr(lake_dir))}; only .lake is supported")
    try:
        packages_dir.encode("utf-8")
    except UnicodeError:
        raise WorkspaceError(f"{_MANIFEST} is not a Lake manifest Autoform reads") from None
    if relative_path(packages_dir) is None:
        raise WorkspaceError(
            f"{_MANIFEST} sets packagesDir to {shorten(repr(packages_dir))}, which is not inside the project"
        )
    overrides = locked_packages(snapshot, _OVERRIDES)
    return LakeWorkspace(
        root=root,
        lean_toolchain=toolchain(snapshot),
        packages_dir=packages_dir,
        packages=packages,
        overridden=frozenset(package.name for package in overrides[1]) if overrides is not None else frozenset(),
    )


def locked_packages(
    snapshot: _DecisionSnapshot, relative: str
) -> tuple[dict[str, object], tuple[LockedPackage, ...]] | None:
    """Decode a Lake manifest or package-overrides file from a snapshot, or ``None`` when there is none."""

    file = snapshot.file(relative)
    if file.state == "missing":
        return None
    try:
        if file.state != "regular" or file.content is None:
            raise ValueError(relative)
        payload = json.loads(file.content.decode("utf-8"), parse_constant=_reject_json_constant, parse_int=_JsonInteger)
        if type(payload) is not dict:
            raise ValueError(relative)
        # A package-overrides file names its version schemaVersion.
        if _manifest_layout(payload.get("version", payload.get("schemaVersion"))) != "current":
            raise ValueError(relative)
        entries = payload.get("packages")
        entries = [] if entries is None else entries
        if type(entries) is not list:
            raise ValueError(relative)
        # Lake inserts entries into a name map in order, so the last duplicate wins.
        packages: dict[str, LockedPackage] = {}
        for item in entries:
            name, lock = _decode_package_entry(item, relative)
            spelled = ".".join(text for _kind, text in name)
            # Every path built from these must be encodable, so a lone surrogate makes the manifest unusable.
            spelled.encode("utf-8")
            if lock.sub_dir is not None:
                lock.sub_dir.encode("utf-8")
            packages[spelled] = LockedPackage(spelled, lock.type, lock.rev, lock.sub_dir, lock.inherited)
    except (AttributeError, RecursionError, UnicodeError, ValueError):
        raise WorkspaceError(f"{relative} is not a Lake manifest Autoform reads") from None
    return payload, tuple(packages[name] for name in sorted(packages))


def toolchain(snapshot: _DecisionSnapshot) -> str | None:
    """Return the toolchain elan reads from ``lean-toolchain``: its trimmed first line."""

    file = snapshot.file("lean-toolchain")
    if file.state != "regular" or file.content is None:
        return None
    try:
        text = file.content.decode("utf-8")
    except UnicodeError:
        return None
    return _trim_elan_whitespace(text.split("\n", 1)[0]) or None


def canonical_name(library: str) -> str:
    """Return ``library`` in the form the manifest lookup compares."""

    return ".".join(text for _kind, text in _canonical_toml_name(library))


def find_package(workspace: LakeWorkspace, library: str) -> LockedPackage:
    """Return the locked Git package ``library`` names, refusing one whose sources the lock does not fix."""

    wanted = canonical_name(library)
    package = next((package for package in workspace.packages if package.name == wanted), None)
    if package is None:
        locked = ", ".join(package.name for package in workspace.packages) or "none"
        raise LibraryError(library, f"it is not a package in {_MANIFEST}; the locked packages are: {shorten(locked)}")
    if package.name in workspace.overridden:
        raise LibraryError(library, f"{_OVERRIDES} replaces it, so the build does not use the locked checkout")
    if package.type != "git":
        raise LibraryError(library, "it is a path dependency, which has no locked revision")
    return package


def checkout_paths(workspace: LakeWorkspace, package: LockedPackage) -> tuple[Path, Path]:
    """Return a package's Git checkout and its package root, which ``subDir`` may place below it."""

    name = package.name
    if not name or name in (".", "..") or any(character in name for character in "/\\\0"):
        raise LibraryError(name, "its name is not a directory name")
    checkout = _descend(workspace.root, f"{workspace.packages_dir}/{name}", name, "it is not checked out")
    if package.sub_dir in (None, "", ".", "./"):
        return checkout, checkout
    if relative_path(package.sub_dir) is None:
        raise LibraryError(name, f"its subDir {shorten(repr(package.sub_dir))} is not inside the checkout")
    return checkout, _descend(
        checkout, package.sub_dir, name, f"its subDir {shorten(repr(package.sub_dir))} is missing"
    )


def _descend(base: Path, relative: str, library: str, missing: str) -> Path:
    """Join ``relative`` to ``base`` one component at a time, following no symbolic link."""

    path, walked = base, []
    for part in relative.split("/"):
        path = path / part
        walked.append(part)
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            raise LibraryError(library, missing) from None
        if stat.S_ISLNK(mode):
            raise LibraryError(
                library, f"its checkout is reached through the symbolic link {shorten('/'.join(walked))}"
            )
        if not stat.S_ISDIR(mode):
            raise LibraryError(library, missing)
    return path


def resolve_head(checkout: Path) -> str:
    """Return the commit ``checkout`` is at, as ``git rev-parse HEAD`` would, by reading files."""

    git = checkout / ".git"
    if git.is_file():
        # A linked worktree or a submodule keeps its Git directory elsewhere.
        pointer = _small(git, _MAX_REF_BYTES) or ""
        if not pointer.startswith("gitdir: "):
            raise HeadError("its .git file names no Git directory")
        try:
            git = (checkout / pointer.removeprefix("gitdir: ").strip()).resolve()
        except (OSError, RuntimeError, ValueError):
            raise HeadError("its .git file names no Git directory") from None
    if not git.is_dir():
        raise HeadError("it is not a Git checkout")
    common = git
    shared = _small(git / "commondir", _MAX_REF_BYTES)
    if shared is not None:
        try:
            common = (git / shared.strip()).resolve()
        except (OSError, RuntimeError, ValueError):
            raise HeadError("its Git directory names no common directory") from None
    if (common / "reftable").is_dir() or _REFTABLE.search(_small(common / "config", _MAX_GIT_FILE_BYTES) or ""):
        raise HeadError("its HEAD cannot be read without Git (reftable ref storage)", needs_git=True)
    value = _small(git / "HEAD", _MAX_REF_BYTES)
    if value is None:
        raise HeadError("it has no readable Git HEAD")
    value = value.strip()
    # A symbolic ref may name another; Git itself stops after a few.
    for _hop in range(5):
        if _OBJECT_ID.fullmatch(value):
            return value
        if not value.startswith("ref: "):
            raise HeadError("its HEAD is neither a commit nor a ref")
        ref = value.removeprefix("ref: ").strip()
        if relative_path(ref) is None:
            raise HeadError("its HEAD names an unsafe ref")
        loose = _small(common / ref, _MAX_REF_BYTES)
        value = loose.strip() if loose is not None else _packed(common, ref)
    raise HeadError("its HEAD is a chain of symbolic refs")


def _packed(common: Path, ref: str) -> str:
    packed = _small(common / "packed-refs", _MAX_GIT_FILE_BYTES) or ""
    for line in packed.splitlines():
        if line[:1] in ("#", "^"):
            continue
        commit, _space, name = line.partition(" ")
        if name == ref:
            return commit
    raise HeadError("its HEAD names a ref that does not exist")


def _small(path: Path, limit: int) -> str | None:
    """Return a small Git metadata file's text, or ``None`` when there is no such file."""

    try:
        with open(path, "rb") as stream:
            data = stream.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        raise HeadError("its Git metadata is larger than Autoform reads")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise HeadError("its Git metadata is not UTF-8") from None


__all__ = [
    "HeadError",
    "LakeWorkspace",
    "LibraryError",
    "LockedPackage",
    "WorkspaceError",
    "checkout_paths",
    "find_package",
    "locked_packages",
    "read_workspace",
    "resolve_head",
    "toolchain",
]
