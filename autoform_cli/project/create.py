"""Create a complete Autoform Lean project and publish it atomically."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from ..claims import _reject_json_constant, _strict_json_object
from ..graph import _parse_node
from ..scaffold import (
    ScaffoldError,
    _FULL_SHA,
    _TEMPLATES,
    _checkout_pin,
    _normalize_autoform_source,
    _read_templates,
    _require_complete_templates,
    _ScaffoldFile,
    _scaffold_plan,
)
from .catalog import SupportedRelease, load_release_catalog

PROJECT_CREATION_SCHEMA = "autoform-project-creation/v1"
_CREATION_RELEASE_SCHEMA = "autoform-project-creation-release/v1"
_PACKAGE_NAME = re.compile(r"[A-Z][A-Za-z0-9]*")
_RESERVED_PACKAGE_NAMES = frozenset({"Prop", "Sort", "Type"})
_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_STAGE_ATTEMPTS = 32
_TOOLCHAIN_MODULE_ROOTS = frozenset({"Init", "Lake", "Lean", "Std"})
_MATHLIB_PRODUCTION_ROOTS = frozenset({"Archive", "Counterexamples", "Mathlib"})
# Bounded components keep int() conversion and the derived default revision small.
_LEAN_TOOLCHAIN = re.compile(
    r"(?:leanprover/lean4:)?(?P<tag>v(?P<major>0|[1-9][0-9]{0,8})\.(?P<minor>0|[1-9][0-9]{0,8})"
    r"\.(?P<patch>0|[1-9][0-9]{0,8})(?:-rc[1-9][0-9]{0,8})?)"
)
# Safe inside a TOML basic string, never a Git option, and free of the
# `: ^ ~ @ { ? * [` revision operators. `_valid_mathlib_revision` enforces the
# structural restrictions Git applies to ref names.
_MATHLIB_REV = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")
_LONGEST_LAKE_ARTIFACT_SUFFIX = ".olean.private.hash"
_MINIMUM_LEAN = (4, 27, 0)
_LOCK_WAIT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05
# Mathlib library roots that no creation descriptor lists: `docs` (every tag from
# v4.27.0, and outside the descriptor root grammar), LongestPole (v4.27.0), and
# Wanted (v4.34.1 on).
_MATHLIB_EXTRA_ROOTS = frozenset({"docs", "LongestPole", "Wanted"})
_MANIFEST_FIELDS = frozenset({"version", "packagesDir", "packages", "name", "lakeDir", "fixedToolchain"})
_MANIFEST_PACKAGE_FIELDS = frozenset(
    {"url", "type", "subDir", "scope", "rev", "name", "manifestFile", "inputRev", "inherited", "configFile"}
)
_UNSAFE_PARENT_MESSAGE = (
    "The target parent is group- or world-writable and is not a sticky directory owned by you "
    "or root. Remove group and world write access (chmod g-w,o-w) or choose another parent."
)
_FAILED_MESSAGE = "Project creation failed; no project was created."
_EXISTS_MESSAGE = "The target already exists; project new never overwrites it."
_CONTRACTS_MESSAGE = "The generated project did not satisfy Autoform's project contracts."
_STAGED_MESSAGE = "The staged project did not satisfy Autoform's project contracts."
_DESCRIPTOR_MESSAGE = "The bundled project-creation release metadata is invalid."
_MANIFEST_MESSAGE = "The bundled release manifest is invalid."
_NO_RENAME_MESSAGE = "This platform cannot atomically publish a new project without replacement."


class ProjectCreateError(ValueError):
    """A new project could not be created without risking existing data."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        return {"error": {"code": self.code, "message": self.message}, "ok": False}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ProjectCreateResult:
    package: str
    release: str | None
    lean_toolchain: str
    mathlib_rev: str
    target: str
    written: tuple[str, ...]
    workflows_pinned: bool
    warnings: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "lean_toolchain": self.lean_toolchain,
            "mathlib_rev": self.mathlib_rev,
            "ok": True,
            "package": self.package,
            "release": self.release,
            "schema": PROJECT_CREATION_SCHEMA,
            "target": self.target,
            "warnings": [{"code": code, "message": message} for code, message in self.warnings],
            "workflows_pinned": self.workflows_pinned,
            "written": list(self.written),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class _ReleaseBundle:
    """Immutable release bytes and generated production-module metadata."""

    manifest_bytes: bytes
    module_roots: frozenset[str]


@dataclass(frozen=True, slots=True)
class _CreationReleaseDescriptor:
    manifest_resource: str
    module_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ProjectVersion:
    """The versions a new project requires; *release* is None outside the catalog."""

    lean_toolchain: str
    mathlib_git: str
    mathlib_rev: str
    release: SupportedRelease | None


def create_project(
    target: str | Path | None,
    *,
    package: str | None,
    release_id: str | None,
    lean_toolchain: str | None = None,
    mathlib_rev: str | None = None,
    autoform_source: str = "",
    autoform_ref: str = "",
) -> ProjectCreateResult:
    """Create and atomically publish a new project at an absent *target*.

    Without *release_id* or *lean_toolchain* the project uses the catalog's
    recommended release. A version pair outside the catalog is written without
    lake-manifest.json and reported in the result's warnings.
    """

    requested = _validate_target(target)
    package_name = _validate_package(package, requested.parent)
    version = _resolve_version(release_id, lean_toolchain, mathlib_rev)
    # Unlisted pairs still reserve the recommended release's module roots.
    roots_bundle = _load_release_bundle(version.release or load_release_catalog().recommended)
    _validate_package_for_release(package_name, roots_bundle)
    release_bundle = roots_bundle if version.release is not None else None
    warnings = _version_warnings(version)
    try:
        # Pin discovery validates this exact stable snapshot, and the plan keeps
        # these retained bytes rather than reopening the template tree.
        templates = _read_templates(_TEMPLATES)
        _require_complete_templates(templates)
        workflow_source, workflow_ref = _resolve_workflow_pin(autoform_source, autoform_ref, templates=templates)
        plan = _build_project_plan(package_name, version, release_bundle, templates, workflow_source, workflow_ref)
        tree = _plan_tree(plan)
        _validate_roadmap_plan(plan)
    except (OSError, ScaffoldError, UnicodeError):
        raise ProjectCreateError("project-create-validation-failed", _CONTRACTS_MESSAGE) from None
    parent_descriptor = _open_parent(requested.parent)
    parent_identity = _descriptor_identity(parent_descriptor)
    publish_descriptor: int | None = None
    confirmed_descriptor: int | None = None
    stage_descriptor: int | None = None
    stage_name: str | None = None
    publication_started = published = parent_synced = False
    parent_recheck: str | None = None
    try:
        _require_package_filename_fit(package_name, parent_descriptor)
        _lock_parent(parent_descriptor)
        _require_absent(parent_descriptor, requested.name)
        stage_name = _create_stage(parent_descriptor)
        stage_descriptor = _open_directory(parent_descriptor, stage_name)
        _require_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        # A directory renamed into the stage name before the open is not the private, empty stage we made.
        if stat.S_IMODE(os.fstat(stage_descriptor).st_mode) != 0o700 or _list_directory(stage_descriptor):
            raise ProjectCreateError("project-create-failed", _FAILED_MESSAGE)
        _materialize_project(stage_descriptor, tree)
        _require_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        # Templates may carry a root manifest; only a catalog release may publish one.
        # Refuse it before the chmod, while the populated stage is still private.
        if release_bundle is None and "lake-manifest.json" in tree:
            raise ProjectCreateError("project-create-validation-failed", _STAGED_MESSAGE)
        os.fchmod(stage_descriptor, 0o755)
        os.fsync(stage_descriptor)
        _verify_project_tree(stage_descriptor, tree)
        # Reopen the requested parent path with O_NOFOLLOW at the last possible
        # moment, and publish through it only if it still names the directory
        # we locked and staged in. A mismatch leaves the complete stage untouched.
        publish_descriptor = _reopen_bound_parent(requested.parent, parent_identity)
        _require_stage_identity(publish_descriptor, stage_name, stage_descriptor)
        publication_started = True
        try:
            _rename_noreplace(parent_descriptor, stage_name, publish_descriptor, requested.name)
        except FileExistsError:
            raise ProjectCreateError("project-target-exists", _EXISTS_MESSAGE) from None
        published = True
        _require_stage_identity(publish_descriptor, requested.name, stage_descriptor)
        os.fsync(publish_descriptor)
        parent_synced = True
        try:
            confirmed_descriptor = _reopen_bound_parent(requested.parent, parent_identity)
        except ProjectCreateError as error:
            parent_recheck = error.code
            raise
        _require_stage_identity(confirmed_descriptor, requested.name, stage_descriptor)
        return ProjectCreateResult(
            package=package_name,
            release=None if version.release is None else version.release.id,
            lean_toolchain=version.lean_toolchain,
            mathlib_rev=version.mathlib_rev,
            target=requested.name,
            written=tuple(item.relative for item in plan),
            workflows_pinned=bool(workflow_ref),
            warnings=warnings,
        )
    except BaseException as error:
        state = _publication_state(parent_descriptor, requested.name, stage_name, stage_descriptor)
        if published or (publication_started and state != "stage"):
            raise _commit_uncertain(state, parent_synced, parent_recheck) from None
        if isinstance(error, OSError):
            code, message = "project-create-failed", _FAILED_MESSAGE
        elif isinstance(error, ProjectCreateError):
            code, message = error.code, error.message
        else:
            raise
        if stage_name is not None:
            message += " An .autoform-new-* stage may remain; inspect it before removal."
        raise ProjectCreateError(code, message) from None
    finally:
        state = _publication_state(parent_descriptor, requested.name, stage_name, stage_descriptor)
        close_failed = False
        for descriptor in (confirmed_descriptor, publish_descriptor, stage_descriptor, parent_descriptor):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    close_failed = True
        if not published and publication_started and state != "stage":
            raise _commit_uncertain(state, parent_synced, parent_recheck)
        if close_failed and published:
            raise _commit_uncertain(state, parent_synced, parent_recheck, cleanup_failed=True)


def _commit_uncertain(
    state: str, parent_synced: bool, parent_recheck: str | None, cleanup_failed: bool = False
) -> ProjectCreateError:
    if parent_recheck == "project-parent-changed":
        message = (
            "The project was published and its original parent directory was synced, but the requested parent "
            "path no longer names that directory. The requested target may not name the project; locate it "
            "before retrying."
        )
    elif parent_recheck is not None:
        message = (
            "The project was published and its original parent directory was synced, but Autoform could not "
            "reopen the requested parent path to confirm that it still names that directory. The target was "
            "observed through the original parent descriptor; verify the requested path before retrying."
        )
    elif state == "target":
        synced = "its parent directory was synced" if parent_synced else "the parent-directory sync was not confirmed"
        cleanup = " and final descriptor cleanup failed" if cleanup_failed else ""
        message = f"The target names the published project and {synced}{cleanup}. Do not retry project creation."
    else:
        message = (
            "Publication started, but neither the target nor the preserved stage names the project directory "
            "held open by Autoform. It may have been moved; locate it before retrying."
        )
    return ProjectCreateError("project-create-commit-uncertain", message)


def _validate_package(package: str | None, parent: Path) -> str:
    if not isinstance(package, str) or _PACKAGE_NAME.fullmatch(package) is None or package in _RESERVED_PACKAGE_NAMES:
        raise ProjectCreateError("project-name-invalid", "Project name must be an UpperCamelCase Lean identifier.")
    _require_package_filename_fit(package, parent)
    return package


def _require_package_filename_fit(package: str, directory: Path | int) -> None:
    """Leave room in *directory* for the longest artifact name Lake derives from *package*."""

    try:
        name_limit = os.pathconf(directory, "PC_NAME_MAX")
    except (AttributeError, OSError, TypeError, ValueError):
        raise ProjectCreateError(
            "project-create-safety-unavailable", "This platform cannot validate the generated Lean filename safely."
        ) from None
    if len(package) + len(_LONGEST_LAKE_ARTIFACT_SUFFIX) > name_limit:
        raise ProjectCreateError(
            "project-name-invalid", "Project name is too long for Lean and Lake artifacts on the target filesystem."
        )


def _resolve_version(release_id: str | None, lean_toolchain: str | None, mathlib_rev: str | None) -> _ProjectVersion:
    """Choose the catalog release or the unlisted version pair the options name.

    A toolchain and revision equal to a catalog entry resolve to that entry, so
    they get its bundled manifest exactly as `release_id` would.
    """

    if release_id is not None and (lean_toolchain is not None or mathlib_rev is not None):
        raise ProjectCreateError(
            "project-version-invalid", "Choose a catalog release or a Lean toolchain and Mathlib revision, not both."
        )
    if lean_toolchain is None:
        if mathlib_rev is not None:
            raise ProjectCreateError("project-version-invalid", "A Mathlib revision requires a Lean toolchain.")
        catalog = load_release_catalog()
        release = (
            catalog.recommended
            if release_id is None
            else next((item for item in catalog.releases if item.id == release_id), None)
        )
        if release is None:
            raise ProjectCreateError(
                "project-release-unknown", "The requested release is not in the bundled release catalog."
            )
        return _ProjectVersion(release.lean_toolchain, release.mathlib_git, release.mathlib_rev, release)
    match = _LEAN_TOOLCHAIN.fullmatch(lean_toolchain) if isinstance(lean_toolchain, str) else None
    if match is None:
        raise ProjectCreateError(
            "project-version-invalid",
            "The Lean toolchain must be a Lean release tag such as v4.30.0, v4.30.0-rc1, or leanprover/lean4:v4.30.0.",
        )
    toolchain = f"leanprover/lean4:{match['tag']}"
    revision = match["tag"] if mathlib_rev is None else mathlib_rev
    if not isinstance(revision, str) or not _valid_mathlib_revision(revision):
        raise ProjectCreateError(
            "project-version-invalid",
            "The Mathlib revision must be a valid Git tag, branch, or commit spelling: 1 to 255 ASCII letters, "
            "digits, dots, underscores, hyphens, or slashes, starting with a letter or digit.",
        )
    catalog = load_release_catalog()
    for release in catalog.releases:
        # Git reads a commit in either case, as `ReleaseCatalog.match` does.
        if release.lean_toolchain == toolchain and (
            revision == release.mathlib_rev or revision.lower() == release.mathlib_commit
        ):
            return _ProjectVersion(release.lean_toolchain, release.mathlib_git, release.mathlib_rev, release)
    return _ProjectVersion(toolchain, catalog.recommended.mathlib_git, revision, None)


def _valid_mathlib_revision(revision: str) -> bool:
    """Whether *revision* fits the safe grammar and Git's ref-name rules."""

    return (
        _MATHLIB_REV.fullmatch(revision) is not None
        and ".." not in revision
        and "@{" not in revision
        and not revision.endswith(".")
        and all(part and not part.startswith(".") and not part.endswith(".lock") for part in revision.split("/"))
    )


def _version_warnings(version: _ProjectVersion) -> tuple[tuple[str, str], ...]:
    """Warnings as `(code, message)` pairs, sorted by code like inspect's diagnostics."""

    warnings: list[tuple[str, str]] = []
    match = _LEAN_TOOLCHAIN.fullmatch(version.lean_toolchain)
    if match is not None and (int(match["major"]), int(match["minor"]), int(match["patch"])) < _MINIMUM_LEAN:
        warnings.append(
            (
                "project-lean-below-minimum",
                f"Autoform needs Lean v4.27.0 or newer; on {version.lean_toolchain} its skeleton "
                "probe and the generated CI declaration audit will fail.",
            )
        )
    if version.release is None:
        warnings.append(
            (
                "project-release-unlisted",
                f"{version.lean_toolchain} with Mathlib {version.mathlib_rev} does not name a bundled "
                "known-good release by its tag or full commit, so no lake-manifest.json was written. "
                "Run `lake update` in the project to resolve and lock Mathlib; it needs network access and "
                "also downloads the Mathlib build cache. The project's lean-toolchain must match the "
                "lean-toolchain of that Mathlib revision.",
            )
        )
    return tuple(sorted(warnings))


def _validate_package_for_release(package: str, bundle: _ReleaseBundle) -> None:
    if package.casefold() in {root.casefold() for root in bundle.module_roots | _MATHLIB_EXTRA_ROOTS}:
        raise ProjectCreateError(
            "project-name-invalid",
            "Project name must not shadow a module root used by Lean, Mathlib, or Mathlib's dependencies.",
        )


def _resolve_workflow_pin(source: str, ref: str, *, templates: tuple[tuple[str, bytes, int], ...]) -> tuple[str, str]:
    """Choose the Autoform source and commit the generated workflows install.

    The rules are `autoform init`'s and run before any filesystem state
    exists. An explicit source carries its own ref or none, because another
    repository's commit does not resolve there. Otherwise the source and ref
    default to the Autoform checkout this CLI runs from, read from local Git
    by `plugin_pin` and never from the network. An empty ref means the
    workflows are omitted and reported as unpinned.
    """

    if not isinstance(source, str) or not isinstance(ref, str):
        raise ProjectCreateError(
            "project-workflow-pin-invalid", "The Autoform workflow source and ref must be strings."
        )
    given_ref = ref.strip().lower()
    if given_ref and _FULL_SHA.fullmatch(given_ref) is None:
        raise ProjectCreateError(
            "project-workflow-pin-invalid",
            "The Autoform workflow ref must be a full 40-character commit SHA; "
            "branches and abbreviated SHAs do not stay put.",
        )
    if source:
        given_source = _normalize_autoform_source(source)
        if given_source is None:
            raise ProjectCreateError(
                "project-workflow-pin-invalid",
                "The Autoform workflow source must be a safe credential-free HTTPS Git URL ending in .git.",
            )
        return given_source, given_ref
    pinned_source, pinned_ref = _checkout_pin(templates)
    return pinned_source, given_ref or pinned_ref


def _validate_target(target: str | Path | None) -> Path:
    try:
        if target is None:
            raise ValueError
        encoded = os.fspath(target)
        # Reject strings that the host filesystem codec cannot round-trip and
        # all surrogate spellings. Some POSIX kernels accept surrogate-escaped
        # bytes for lookup but reject them in atomic rename syscalls; rejecting
        # them here avoids leaving a complete stage after that late failure.
        if (
            not isinstance(encoded, str)
            or "\0" in encoded
            or any(0xD800 <= ord(character) <= 0xDFFF for character in encoded)
            or os.fsdecode(os.fsencode(encoded)) != encoded
        ):
            raise ValueError
        selected = Path(encoded).expanduser()
        if not selected.parts or selected.name in {"", ".", ".."}:
            raise ValueError
        raw = selected.absolute()
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ProjectCreateError("project-target-invalid", "The project target cannot be resolved safely.") from None
    try:
        metadata = raw.parent.stat()
    except OSError as error:
        raise _parent_access_error(error) from None
    except ValueError:
        raise ProjectCreateError("project-parent-invalid", "The target parent cannot be inspected.") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProjectCreateError("project-parent-invalid", "The target parent is not a directory.")
    if _unsafe_parent_metadata(metadata.st_mode, metadata.st_uid):
        raise ProjectCreateError("project-parent-unsafe", _UNSAFE_PARENT_MESSAGE)
    # Keep every caller-supplied path component. `_open_parent` traverses this
    # exact absolute spelling with O_NOFOLLOW, so no alias can be resolved here
    # and later retargeted while publication continues in the old directory.
    return raw


def _parent_access_error(error: OSError) -> ProjectCreateError:
    """Classify a failure to inspect or open the target parent or an ancestor."""

    if error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
        return ProjectCreateError("project-parent-missing", "The target parent directory does not exist.")
    if error.errno in {errno.EACCES, errno.EPERM}:
        return ProjectCreateError(
            "project-parent-inaccessible",
            "The target parent or one of its ancestors is not accessible; project new needs read "
            "and search permission on each of them.",
        )
    return ProjectCreateError("project-parent-invalid", "The target parent cannot be inspected.")


def _unsafe_parent_metadata(mode: int, owner: int) -> bool:
    # Sticky directories protect entries only from peers, not from their owner.
    # Trust the invoking user and the system administrator, as conventional
    # root-owned temporary directories require. ACLs are not read: on macOS,
    # an inheritable parent ACL can grant another uid access to the stage.
    return bool(mode & (stat.S_IWGRP | stat.S_IWOTH)) and (
        not mode & stat.S_ISVTX or not hasattr(os, "geteuid") or owner not in {0, os.geteuid()}
    )


def _open_parent(parent: Path) -> int:
    if (
        not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "geteuid"))
        or any(function not in os.supports_dir_fd for function in (os.mkdir, os.open, os.stat))
        or os.stat not in os.supports_follow_symlinks
        or os.listdir not in os.supports_fd
    ):
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "This platform cannot create the project with the required path safety.",
        )
    try:
        descriptor = _open_directory(None, parent.anchor)
        try:
            for part in parent.parts[1:]:
                try:
                    child = _open_directory(descriptor, part)
                except NotADirectoryError:
                    # macOS reports a symbolic link as ENOTDIR too, so look
                    # before calling the component a link.
                    if not stat.S_ISLNK(os.stat(part, dir_fd=descriptor, follow_symlinks=False).st_mode):
                        raise ProjectCreateError(
                            "project-parent-invalid", "The target parent or one of its ancestors is not a directory."
                        ) from None
                    raise
                os.close(descriptor)
                descriptor = child
            metadata = os.fstat(descriptor)
            if _unsafe_parent_metadata(metadata.st_mode, metadata.st_uid):
                raise ProjectCreateError("project-parent-unsafe", _UNSAFE_PARENT_MESSAGE)
        except BaseException:
            os.close(descriptor)
            raise
    except OSError as error:
        # Depending on the platform, O_DIRECTORY|O_NOFOLLOW reports a symbolic
        # link as ELOOP, EMLINK, or ENOTDIR (macOS).
        if error.errno in {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}:
            raise ProjectCreateError("project-path-is-symlink", "The target path contains a symbolic link.") from None
        if error.errno in {errno.ENOENT, errno.EACCES, errno.EPERM}:
            raise _parent_access_error(error) from None
        raise ProjectCreateError("project-create-failed", _FAILED_MESSAGE) from None
    except UnicodeError:
        raise ProjectCreateError("project-target-invalid", "The project target cannot be resolved safely.") from None
    return descriptor


def _reopen_bound_parent(parent: Path, expected_identity: tuple[int, int, int]) -> int:
    """Open *parent* afresh and require the same device, inode, and owner."""

    try:
        descriptor = _open_parent(parent)
    except ProjectCreateError:
        raise ProjectCreateError(
            "project-parent-unverifiable", "The requested parent path could not be reverified safely."
        ) from None
    try:
        if _descriptor_identity(descriptor) != expected_identity:
            raise ProjectCreateError(
                "project-parent-changed", "The requested parent path changed while the project was being created."
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _require_absent(parent_descriptor: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except PermissionError as error:
        # Opening the parent needs only read permission; a lookup inside it also needs search.
        raise _parent_access_error(error) from None
    except OSError:
        raise ProjectCreateError("project-create-failed", _FAILED_MESSAGE) from None
    raise ProjectCreateError("project-target-exists", _EXISTS_MESSAGE)


def _create_stage(parent_descriptor: int) -> str:
    for _ in range(_STAGE_ATTEMPTS):
        name = f".autoform-new-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            return name
        except FileExistsError:
            continue
        except PermissionError:
            raise ProjectCreateError(
                "project-parent-inaccessible",
                "The target parent is not writable; project new needs write permission on it.",
            ) from None
    raise ProjectCreateError("project-create-failed", _FAILED_MESSAGE)


def _open_directory(parent_descriptor: int | None, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    return os.open(name, flags, dir_fd=parent_descriptor)


def _open_planned_file(parent_descriptor: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    return os.open(name, flags, dir_fd=parent_descriptor)


def _lock_parent(parent_descriptor: int) -> None:
    """Serialize creation in the parent, giving up if another holder keeps the lock."""

    unavailable = ProjectCreateError(
        "project-create-safety-unavailable", "This platform cannot serialize concurrent project creation safely."
    )
    try:
        import fcntl
    except ImportError:
        raise unavailable from None
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(parent_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ProjectCreateError(
                    "project-parent-busy",
                    f"Another process kept the target parent locked for {_LOCK_WAIT_SECONDS:g} "
                    "seconds; retry when it finishes.",
                ) from None
        except OSError:
            raise unavailable from None
        time.sleep(_LOCK_POLL_SECONDS)


def _list_directory(directory_descriptor: int) -> set[str]:
    """List through a fresh descriptor so earlier scans cannot leave it at EOF."""

    fresh = _open_directory(directory_descriptor, ".")
    try:
        return set(os.listdir(fresh))
    finally:
        os.close(fresh)


def _descriptor_identity(descriptor: int) -> tuple[int, int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino, metadata.st_uid


def _entry_matches_descriptor(parent_descriptor: int, name: str, descriptor: int) -> bool:
    try:
        expected = _descriptor_identity(descriptor)
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except (OSError, UnicodeError):
        return False
    return metadata.st_uid == os.geteuid() and (metadata.st_dev, metadata.st_ino, metadata.st_uid) == expected


def _require_stage_identity(parent_descriptor: int, name: str, stage_descriptor: int) -> None:
    if not _entry_matches_descriptor(parent_descriptor, name, stage_descriptor):
        raise ProjectCreateError("project-create-failed", _FAILED_MESSAGE)


def _publication_state(
    parent_descriptor: int, target_name: str, stage_name: str | None, stage_descriptor: int | None
) -> str:
    if stage_name is None or stage_descriptor is None:
        return "none"
    target_matches = _entry_matches_descriptor(parent_descriptor, target_name, stage_descriptor)
    stage_matches = _entry_matches_descriptor(parent_descriptor, stage_name, stage_descriptor)
    if target_matches != stage_matches:
        return "target" if target_matches else "stage"
    return "detached"


def _build_project_plan(
    package: str,
    version: _ProjectVersion,
    release_bundle: _ReleaseBundle | None,
    templates: tuple[tuple[str, bytes, int], ...],
    autoform_source: str,
    autoform_ref: str,
) -> tuple[_ScaffoldFile, ...]:
    lakefile = (
        f'name = "{package}"\n'
        'version = "0.1.0"\n'
        f'defaultTargets = ["{package}"]\n\n'
        "[[require]]\n"
        'name = "mathlib"\n'
        f'git = "{version.mathlib_git}"\n'
        f'rev = "{version.mathlib_rev}"\n\n'
        "[[lean_lib]]\n"
        f'name = "{package}"\n'
        'srcDir = "src"\n'
    )
    module = (
        "import Mathlib\n\n"
        f"namespace {package}\n\n"
        "/-- Marker declaration for the initial project build. -/\n"
        "def autoformProjectInitialized : Bool := true\n\n"
        f"end {package}\n"
    )
    files = [
        _ScaffoldFile("lean-toolchain", f"{version.lean_toolchain}\n".encode(), 0o644),
        _ScaffoldFile("lakefile.toml", lakefile.encode(), 0o644),
        _ScaffoldFile(f"src/{package}.lean", module.encode(), 0o644),
    ]
    # Only a catalog release has a resolved manifest; `lake update` writes one
    # for any other pair.
    if release_bundle is not None:
        files.append(_ScaffoldFile("lake-manifest.json", _lake_manifest(package, release_bundle), 0o644))
    # The shared plan has already reduced installer modes to Git's executable
    # bit, so `init` and `project new` publish the same canonical modes.
    scaffold_files, _ = _scaffold_plan(
        templates, title=package, repository_url="", autoform_source=autoform_source, autoform_ref=autoform_ref
    )
    files.extend(scaffold_files)
    return tuple(sorted(files, key=lambda item: item.relative))


def _load_release_bundle(release: SupportedRelease) -> _ReleaseBundle:
    try:
        descriptor = _load_creation_release_descriptor(release)
        manifest_bytes = files("autoform_cli.project").joinpath(descriptor.manifest_resource).read_bytes()
        return _parse_release_bundle(manifest_bytes, release, descriptor.module_roots)
    except ProjectCreateError:
        raise
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError, MemoryError):
        raise ProjectCreateError("project-create-validation-failed", _MANIFEST_MESSAGE) from None


def _load_creation_release_descriptor(release: SupportedRelease) -> _CreationReleaseDescriptor:
    invalid = ProjectCreateError("project-create-validation-failed", _DESCRIPTOR_MESSAGE)
    if _RELEASE_ID.fullmatch(release.id) is None:
        raise invalid
    try:
        payload = _load_strict_json(
            files("autoform_cli.project").joinpath(f"creation-release-{release.id}.json").read_bytes()
        )
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError, MemoryError):
        raise invalid from None
    expected_release = {
        "id": release.id,
        "lean_toolchain": release.lean_toolchain,
        "mathlib_git": release.mathlib_git,
        "mathlib_rev": release.mathlib_rev,
        "mathlib_commit": release.mathlib_commit,
    }
    if type(payload) is not dict or set(payload) != {"schema", "release", "lake_manifest", "production_module_roots"}:
        raise invalid
    resource, roots = payload["lake_manifest"], payload["production_module_roots"]
    if (
        payload["schema"] != _CREATION_RELEASE_SCHEMA
        or payload["release"] != expected_release
        or type(resource) is not str
        or not resource.endswith(".json")
        or not _safe_relative(resource)
        or any(ord(character) >= 0xD800 for character in resource)
        or "/" in resource
        or type(roots) is not list
        or not roots
        or any(type(root) is not str or _PACKAGE_NAME.fullmatch(root) is None for root in roots)
        or len({root.casefold() for root in roots}) != len(roots)
    ):
        raise invalid
    return _CreationReleaseDescriptor(resource, tuple(roots))


def _parse_release_bundle(
    manifest_bytes: bytes, release: SupportedRelease, module_roots: tuple[str, ...]
) -> _ReleaseBundle:
    invalid = ProjectCreateError("project-create-validation-failed", _MANIFEST_MESSAGE)
    payload = _load_strict_json(manifest_bytes)
    if (
        type(payload) is not dict
        or set(payload) != _MANIFEST_FIELDS
        or payload["version"] != "1.2.0"
        or payload["packagesDir"] != ".lake/packages"
        or payload["name"] != ""
        or payload["lakeDir"] != ".lake"
        or payload["fixedToolchain"] is not False
        or type(payload["packages"]) is not list
        or not payload["packages"]
        or not all(_valid_manifest_package(entry) for entry in payload["packages"])
    ):
        raise invalid
    packages = payload["packages"]
    roots = frozenset(module_roots)
    folded_roots = {root.casefold() for root in roots}
    names = [entry["name"].casefold() for entry in packages]
    direct = [entry for entry in packages if entry["inherited"] is False]
    if (
        len(set(names)) != len(names)
        or any(name not in folded_roots for name in names)
        or not _TOOLCHAIN_MODULE_ROOTS | _MATHLIB_PRODUCTION_ROOTS <= roots
        or len(direct) != 1
        or direct[0]["name"] != "mathlib"
        or direct[0]["url"] != release.mathlib_git
        or direct[0]["inputRev"] != release.mathlib_rev
        or direct[0]["rev"] != release.mathlib_commit
        or direct[0]["subDir"] is not None  # releases load Mathlib from its repository root
        or direct[0]["configFile"] != "lakefile.lean"
        or direct[0]["manifestFile"] != "lake-manifest.json"
    ):
        raise ProjectCreateError(
            "project-create-validation-failed", "The bundled release manifest does not match the release catalog."
        )
    return _ReleaseBundle(manifest_bytes=manifest_bytes, module_roots=roots)


def _valid_manifest_package(entry: object) -> bool:
    if type(entry) is not dict or set(entry) != _MANIFEST_PACKAGE_FIELDS:
        return False
    input_revision = entry["inputRev"]
    return (
        type(entry["url"]) is str
        and _safe_https_git_url(entry["url"])
        and entry["type"] == "git"
        and type(entry["name"]) is str
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", entry["name"]) is not None
        and type(entry["scope"]) is str
        and type(entry["rev"]) is str
        and _FULL_SHA.fullmatch(entry["rev"]) is not None
        and type(input_revision) is str
        and bool(input_revision)
        and all(ord(character) >= 0x20 for character in input_revision)
        and type(entry["inherited"]) is bool
        and _safe_relative(entry["configFile"])
        and _safe_relative(entry["manifestFile"])
        and (entry["subDir"] is None or _safe_relative(entry["subDir"]))
    )


def _safe_https_git_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path not in {"", "/"}
    )


def _safe_relative(value: object) -> bool:
    """Whether *value* is a normalized relative POSIX path with printable components."""

    if (
        type(value) is not str
        or "\\" in value
        or any(ord(character) < 0x20 or 0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        return False
    path = PurePosixPath(value)
    return (
        value not in {"", ".", ".."}
        and not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _load_strict_json(data: bytes) -> object:
    """Parse JSON, rejecting duplicate keys and NaN or Infinity."""

    return json.loads(data, object_pairs_hook=_strict_json_object, parse_constant=_reject_json_constant)


def _lake_manifest(package: str, bundle: _ReleaseBundle) -> bytes:
    payload = json.loads(bundle.manifest_bytes)
    payload["name"] = package
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _plan_tree(plan: tuple[_ScaffoldFile, ...]) -> dict[str, object]:
    """Nest the plan by path component, rejecting unsafe paths and file/directory collisions."""

    tree: dict[str, object] = {}
    for item in plan:
        if not _safe_relative(item.relative) or item.mode not in {0o644, 0o755}:
            raise ProjectCreateError("project-create-validation-failed", _CONTRACTS_MESSAGE)
        *directories, name = PurePosixPath(item.relative).parts
        branch = tree
        for part in directories:
            branch = branch.setdefault(part, {})
            if not isinstance(branch, dict):
                raise ProjectCreateError("project-create-validation-failed", _CONTRACTS_MESSAGE)
        if name in branch:
            raise ProjectCreateError("project-create-validation-failed", _CONTRACTS_MESSAGE)
        branch[name] = item
    return tree


def _materialize_project(root_descriptor: int, tree: dict[str, object]) -> None:
    def write_directory(descriptor: int, entries: dict[str, object]) -> None:
        for name, entry in sorted(entries.items()):
            if isinstance(entry, dict):
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
                child = _open_directory(descriptor, name)
                try:
                    write_directory(child, entry)
                    os.fchmod(child, 0o755)
                    os.fsync(child)
                finally:
                    os.close(child)
                continue
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            child = os.open(name, flags, 0o600, dir_fd=descriptor)
            try:
                content = memoryview(entry.content)
                while content:
                    written = os.write(child, content)
                    if written <= 0:
                        raise OSError(errno.EIO, "short project file write")
                    content = content[written:]
                os.fchmod(child, entry.mode)
                os.fsync(child)
            finally:
                os.close(child)

    write_directory(root_descriptor, tree)


def _stable_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _verify_project_tree(root_descriptor: int, tree: dict[str, object]) -> None:
    """Re-read the stage through descriptors and require exactly the planned entries, modes, and bytes."""

    root = os.fstat(root_descriptor)
    if root.st_uid != os.geteuid() or stat.S_IMODE(root.st_mode) != 0o755:
        raise OSError(errno.ESTALE, "project root changed")

    def verify_directory(descriptor: int, entries: dict[str, object]) -> None:
        before = os.fstat(descriptor)
        if _list_directory(descriptor) != set(entries):
            raise OSError(errno.ESTALE, "project directory changed")
        for name, entry in sorted(entries.items()):
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            is_directory = isinstance(entry, dict)
            if not (stat.S_ISDIR if is_directory else stat.S_ISREG)(metadata.st_mode):
                raise OSError(errno.ESTALE, "project entry changed")
            child = (_open_directory if is_directory else _open_planned_file)(descriptor, name)
            try:
                opened = os.fstat(child)
                if is_directory:
                    verify_directory(child, entry)
                else:
                    content = b""
                    while len(content) <= len(entry.content):
                        chunk = os.read(child, len(entry.content) + 1 - len(content))
                        if not chunk:
                            break
                        content += chunk
                after = os.fstat(child)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (
                    len({_stable_metadata(item) for item in (metadata, opened, after, current)}) != 1
                    or after.st_uid != os.geteuid()
                    or stat.S_IMODE(after.st_mode) != (0o755 if is_directory else entry.mode)
                    or (not is_directory and (after.st_nlink != 1 or content != entry.content))
                ):
                    raise OSError(errno.ESTALE, "project entry changed")
            finally:
                os.close(child)
        unchanged = _stable_metadata(os.fstat(descriptor)) == _stable_metadata(before)
        if not unchanged or _list_directory(descriptor) != set(entries):
            raise OSError(errno.ESTALE, "project directory changed")

    verify_directory(root_descriptor, tree)


def _validate_roadmap_plan(plan: tuple[_ScaffoldFile, ...]) -> None:
    invalid = ProjectCreateError("project-create-validation-failed", _STAGED_MESSAGE)
    roadmap = [
        item for item in plan if item.relative.startswith("blueprint/roadmap/") and item.relative.endswith(".md")
    ]
    if len(roadmap) != 1 or roadmap[0].relative != "blueprint/roadmap/README.md":
        raise invalid
    try:
        text = roadmap[0].content.decode("utf-8")
    except UnicodeError:
        raise invalid from None
    parsed, issues = _parse_node("roadmap", Path("roadmap/README.md"), text)
    if issues or parsed is None or parsed.statement_targets or parsed.proof_targets:
        raise invalid


def _rename_noreplace(source_parent_descriptor: int, source: str, target_parent_descriptor: int, target: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    # The no-replace flag is RENAME_EXCL for macOS renameatx_np and RENAME_NOREPLACE for Linux renameat2.
    name, flag = ("renameatx_np", 0x4) if hasattr(libc, "renameatx_np") else ("renameat2", 0x1)
    if not hasattr(libc, name):
        raise ProjectCreateError("project-create-safety-unavailable", _NO_RENAME_MESSAGE)
    function = getattr(libc, name)
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if (
        function(source_parent_descriptor, os.fsencode(source), target_parent_descriptor, os.fsencode(target), flag)
        == 0
    ):
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, os.strerror(error), target)
    if error in {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP}:
        raise ProjectCreateError("project-create-safety-unavailable", _NO_RENAME_MESSAGE)
    raise OSError(error, os.strerror(error), target)


__all__ = ["ProjectCreateError", "ProjectCreateResult", "create_project"]
