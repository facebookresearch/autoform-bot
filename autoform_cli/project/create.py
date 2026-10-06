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

from .. import scaffold
from ..graph import _parse_node
from ..scaffold import (
    DEFAULT_AUTOFORM_SOURCE,
    ScaffoldError,
    _TEMPLATES,
    _normalize_autoform_source,
    _read_templates,
    _require_complete_templates,
    _ScaffoldFile,
    _scaffold_plan,
)
from .catalog import SupportedRelease, load_release_catalog

_PACKAGE_NAME = re.compile(r"[A-Z][A-Za-z0-9]*")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_RESERVED_PACKAGE_NAMES = frozenset({"Prop", "Sort", "Type"})
_CREATION_RELEASE_SCHEMA = "autoform-project-creation-release/v1"
PROJECT_CREATION_SCHEMA = "autoform-project-creation/v1"
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
# `: ^ ~ @ { ? * [` revision operators. Additional checks below enforce the
# structural restrictions Git applies to ref names.
_MATHLIB_REV = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")
_LONGEST_LAKE_ARTIFACT_SUFFIX = ".olean.private.hash"
_MINIMUM_LEAN = (4, 27, 0)
_LOCK_WAIT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05
_UNSAFE_PARENT_MESSAGE = (
    "The target parent is group- or world-writable and is not a sticky directory owned by you "
    "or root. Remove group and world write access (chmod g-w,o-w) or choose another parent."
)
# Mathlib library roots that no creation descriptor lists: `docs` (every tag from
# v4.27.0, and outside the descriptor root grammar), LongestPole (v4.27.0), and
# Wanted (v4.34.1 on).
_MATHLIB_EXTRA_ROOTS = frozenset({"docs", "LongestPole", "Wanted"})
_MANIFEST_FIELDS = frozenset(
    {"version", "packagesDir", "packages", "name", "lakeDir", "fixedToolchain"}
)
_MANIFEST_PACKAGE_FIELDS = frozenset(
    {
        "url",
        "type",
        "subDir",
        "scope",
        "rev",
        "name",
        "manifestFile",
        "inputRev",
        "inherited",
        "configFile",
    }
)


class ProjectCreateError(ValueError):
    """A new project could not be created without risking existing data."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        return {"error": {"code": self.code, "message": self.message}, "ok": False}

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )


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
        return json.dumps(
            self.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )


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
        workflow_source, workflow_ref = _resolve_workflow_pin(
            autoform_source,
            autoform_ref,
            templates=templates,
        )
        plan, workflows_pinned = _build_project_plan(
            package_name,
            version,
            release_bundle,
            templates=templates,
            autoform_source=workflow_source,
            autoform_ref=workflow_ref,
        )
        _plan_tree(plan)
        _validate_roadmap_plan(plan)
    except (OSError, ScaffoldError, UnicodeError):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The generated project did not satisfy Autoform's project contracts.",
        ) from None
    parent = requested.parent
    parent_descriptor = _open_parent(parent)
    parent_identity = _descriptor_identity(parent_descriptor)
    publish_parent_descriptor: int | None = None
    confirmed_parent_descriptor: int | None = None
    stage_name: str | None = None
    stage_descriptor: int | None = None
    publication_started = False
    published = False
    parent_synced = False
    parent_rebound = False
    parent_recheck_failed = False
    try:
        _require_package_filename_fit(package_name, parent_descriptor)
        _lock_parent(parent_descriptor)
        _require_absent(parent_descriptor, requested.name)
        stage_name = _create_stage(parent_descriptor)
        stage_metadata = os.stat(stage_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(stage_metadata.st_mode):
            raise OSError(errno.ENOTDIR, "staging path is not a directory")
        stage_descriptor = _open_stage(parent_descriptor, stage_name)
        _require_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        _materialize_project(stage_descriptor, plan)
        _require_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        _validate_staged_project(
            stage_descriptor,
            plan,
            package_name,
            version,
            release_bundle,
        )
        _require_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        os.fchmod(stage_descriptor, 0o755)
        os.fsync(stage_descriptor)
        _verify_project_plan(stage_descriptor, plan, root_mode=0o755)
        _require_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        # Reopen the original requested parent path with O_NOFOLLOW at the last
        # possible moment. Publication uses this fresh descriptor only after
        # its device, inode, and owner match the directory we locked and staged
        # in. A mismatch leaves the complete stage untouched.
        publish_parent_descriptor = _reopen_bound_parent(parent, parent_identity)
        _require_stage_identity(publish_parent_descriptor, stage_name, stage_descriptor)
        publication_started = True
        try:
            _rename_noreplace(
                parent_descriptor,
                stage_name,
                publish_parent_descriptor,
                requested.name,
            )
        except FileExistsError:
            raise ProjectCreateError(
                "project-target-exists",
                "The target already exists; project new never overwrites it.",
            ) from None
        published = True
        _require_stage_identity(publish_parent_descriptor, requested.name, stage_descriptor)
        os.fsync(publish_parent_descriptor)
        parent_synced = True
        try:
            confirmed_parent_descriptor = _reopen_bound_parent(parent, parent_identity)
        except ProjectCreateError as error:
            if error.code == "project-parent-changed":
                parent_rebound = True
            else:
                parent_recheck_failed = True
            raise
        _require_stage_identity(confirmed_parent_descriptor, requested.name, stage_descriptor)
        return ProjectCreateResult(
            package=package_name,
            release=None if version.release is None else version.release.id,
            lean_toolchain=version.lean_toolchain,
            mathlib_rev=version.mathlib_rev,
            target=requested.name,
            written=tuple(item.relative for item in plan),
            workflows_pinned=workflows_pinned,
            warnings=warnings,
        )
    except ProjectCreateError as error:
        state = _publication_state(
            parent_descriptor,
            requested.name,
            stage_name,
            stage_descriptor,
        )
        if published or (publication_started and state != "stage"):
            raise ProjectCreateError(
                "project-create-commit-uncertain",
                _commit_uncertain_message(
                    state,
                    parent_synced=parent_synced,
                    parent_rebound=parent_rebound,
                    parent_recheck_failed=parent_recheck_failed,
                ),
            ) from None
        if stage_name is not None:
            raise ProjectCreateError(error.code, _with_preserved_stage(error.message)) from None
        raise
    except OSError:
        state = _publication_state(
            parent_descriptor,
            requested.name,
            stage_name,
            stage_descriptor,
        )
        if published or (publication_started and state != "stage"):
            raise ProjectCreateError(
                "project-create-commit-uncertain",
                _commit_uncertain_message(
                    state,
                    parent_synced=parent_synced,
                    parent_rebound=parent_rebound,
                    parent_recheck_failed=parent_recheck_failed,
                ),
            ) from None
        message = "Project creation failed; no project was created."
        if stage_name is not None:
            message = _with_preserved_stage(message)
        raise ProjectCreateError("project-create-failed", message) from None
    except BaseException:
        state = _publication_state(
            parent_descriptor,
            requested.name,
            stage_name,
            stage_descriptor,
        )
        if published or (publication_started and state != "stage"):
            raise ProjectCreateError(
                "project-create-commit-uncertain",
                _commit_uncertain_message(
                    state,
                    parent_synced=parent_synced,
                    parent_rebound=parent_rebound,
                    parent_recheck_failed=parent_recheck_failed,
                ),
            ) from None
        raise
    finally:
        state = _publication_state(
            parent_descriptor,
            requested.name,
            stage_name,
            stage_descriptor,
        )
        publication_uncertain = not published and publication_started and state != "stage"
        close_failed = False
        for descriptor in (
            confirmed_parent_descriptor,
            publish_parent_descriptor,
            stage_descriptor,
            parent_descriptor,
        ):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError:
                close_failed = True
        if publication_uncertain:
            raise ProjectCreateError(
                "project-create-commit-uncertain",
                _commit_uncertain_message(
                    state,
                    parent_synced=parent_synced,
                    parent_rebound=parent_rebound,
                    parent_recheck_failed=parent_recheck_failed,
                ),
            )
        if close_failed and published:
            raise ProjectCreateError(
                "project-create-commit-uncertain",
                _commit_uncertain_message(
                    state,
                    parent_synced=parent_synced,
                    parent_rebound=parent_rebound,
                    parent_recheck_failed=parent_recheck_failed,
                    cleanup_failed=True,
                ),
            )


def _with_preserved_stage(message: str) -> str:
    return f"{message} An .autoform-new-* stage may remain; inspect it before removal."


def _commit_uncertain_message(
    state: str,
    *,
    parent_synced: bool,
    parent_rebound: bool,
    parent_recheck_failed: bool,
    cleanup_failed: bool = False,
) -> str:
    if parent_rebound:
        return (
            "The project was published and its original parent directory was synced, but the "
            "requested parent path no longer names that directory. The requested target may not "
            "name the project; locate it before retrying."
        )
    if parent_recheck_failed:
        return (
            "The project was published and its original parent directory was synced, but Autoform "
            "could not reopen the requested parent path to confirm that it still names that "
            "directory. The target was observed through the original parent descriptor; verify "
            "the requested path before retrying."
        )
    if state == "target":
        durability = (
            "its parent directory was synced"
            if parent_synced
            else "the parent-directory sync was not confirmed"
        )
        suffix = " and final descriptor cleanup failed" if cleanup_failed else ""
        return (
            f"The target names the published project and {durability}{suffix}. "
            "Do not retry project creation."
        )
    return (
        "Publication started, but neither the target nor the preserved stage names the project "
        "directory held open by Autoform. It may have been moved; locate it before retrying."
    )


def _validate_package(package: str | None, parent: Path) -> str:
    if (
        not isinstance(package, str)
        or _PACKAGE_NAME.fullmatch(package) is None
        or package in _RESERVED_PACKAGE_NAMES
    ):
        raise ProjectCreateError(
            "project-name-invalid",
            "Project name must be an UpperCamelCase Lean identifier.",
        )
    try:
        name_limit = os.pathconf(parent, "PC_NAME_MAX")
    except (AttributeError, OSError, ValueError):
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "This platform cannot validate the generated Lean filename safely.",
        ) from None
    if not _package_artifact_fits(package, name_limit):
        raise ProjectCreateError(
            "project-name-invalid",
            "Project name is too long for Lean and Lake artifacts on the target filesystem.",
        )
    return package


def _package_artifact_fits(package: str, name_limit: int) -> bool:
    return len(f"{package}{_LONGEST_LAKE_ARTIFACT_SUFFIX}".encode("ascii")) <= name_limit


def _require_package_filename_fit(package: str, parent_descriptor: int) -> None:
    try:
        name_limit = os.fpathconf(parent_descriptor, "PC_NAME_MAX")
    except (AttributeError, OSError, ValueError):
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "This platform cannot validate the generated Lean filename safely.",
        ) from None
    if not _package_artifact_fits(package, name_limit):
        raise ProjectCreateError(
            "project-name-invalid",
            "Project name is too long for Lean and Lake artifacts on the target filesystem.",
        )


def _find_release(release_id: str | None) -> SupportedRelease:
    catalog = load_release_catalog()
    release = next((item for item in catalog.releases if item.id == release_id), None)
    if release is None:
        raise ProjectCreateError(
            "project-release-unknown",
            "The requested release is not in the bundled release catalog.",
        )
    return release


def _resolve_version(
    release_id: str | None,
    lean_toolchain: str | None,
    mathlib_rev: str | None,
) -> _ProjectVersion:
    """Choose the catalog release or the unlisted version pair the options name.

    A toolchain and revision equal to a catalog entry resolve to that entry, so
    they get its bundled manifest exactly as `release_id` would.
    """

    if release_id is not None and (lean_toolchain is not None or mathlib_rev is not None):
        raise ProjectCreateError(
            "project-version-invalid",
            "Choose a catalog release or a Lean toolchain and Mathlib revision, not both.",
        )
    if lean_toolchain is None:
        if mathlib_rev is not None:
            raise ProjectCreateError(
                "project-version-invalid",
                "A Mathlib revision requires a Lean toolchain.",
            )
        release = (
            load_release_catalog().recommended if release_id is None else _find_release(release_id)
        )
        return _ProjectVersion(release.lean_toolchain, release.mathlib_git, release.mathlib_rev, release)
    match = _LEAN_TOOLCHAIN.fullmatch(lean_toolchain) if isinstance(lean_toolchain, str) else None
    if match is None:
        raise ProjectCreateError(
            "project-version-invalid",
            "The Lean toolchain must be a Lean release tag such as v4.30.0, v4.30.0-rc1, "
            "or leanprover/lean4:v4.30.0.",
        )
    toolchain = f"leanprover/lean4:{match['tag']}"
    revision = match["tag"] if mathlib_rev is None else mathlib_rev
    if not isinstance(revision, str) or not _valid_mathlib_revision(revision):
        raise ProjectCreateError(
            "project-version-invalid",
            "The Mathlib revision must be a valid Git tag, branch, or commit spelling: 1 to 255 "
            "ASCII letters, digits, dots, underscores, hyphens, or slashes, starting with a "
            "letter or digit.",
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

    if (
        _MATHLIB_REV.fullmatch(revision) is None
        or ".." in revision
        or "@{" in revision
        or revision.endswith(".")
    ):
        return False
    parts = revision.split("/")
    return all(
        part
        and not part.startswith(".")
        and not part.endswith(".lock")
        for part in parts
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
                "Run `lake update` in "
                "the project to resolve and lock Mathlib; it needs network access and also "
                "downloads the Mathlib build cache. The project's lean-toolchain must match the "
                "lean-toolchain of that Mathlib revision.",
            )
        )
    return tuple(sorted(warnings))


def _validate_package_for_release(package: str, bundle: _ReleaseBundle) -> None:
    reserved = bundle.module_roots | _MATHLIB_EXTRA_ROOTS
    if package.casefold() in {root.casefold() for root in reserved}:
        raise ProjectCreateError(
            "project-name-invalid",
            "Project name must not shadow a module root used by Lean, Mathlib, or Mathlib's dependencies.",
        )


def _resolve_workflow_pin(
    source: str,
    ref: str,
    *,
    templates: tuple[tuple[str, bytes, int], ...],
) -> tuple[str, str]:
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
            "project-workflow-pin-invalid",
            "The Autoform workflow source and ref must be strings.",
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
    # Looked up on the module so one replacement governs `init` and `project new`.
    pinned_source, pinned_ref = scaffold.plugin_pin(templates)
    safe_pinned_source = _normalize_autoform_source(pinned_source, allow_github_scp=True)
    if safe_pinned_source is None or _FULL_SHA.fullmatch(pinned_ref.lower()) is None:
        safe_pinned_source, pinned_ref = None, ""
    return safe_pinned_source or DEFAULT_AUTOFORM_SOURCE, given_ref or pinned_ref.lower()


def _validate_target(target: str | Path | None) -> Path:
    try:
        if target is None:
            raise ValueError
        encoded = os.fspath(target)
        if not isinstance(encoded, str) or "\0" in encoded:
            raise ValueError
        # Reject strings that the host filesystem codec cannot round-trip and
        # all surrogate spellings. Some POSIX kernels accept surrogate-escaped
        # bytes for lookup but reject them in atomic rename syscalls; rejecting
        # them here avoids leaving a complete stage after that late failure.
        if (
            any(0xD800 <= ord(character) <= 0xDFFF for character in encoded)
            or os.fsdecode(os.fsencode(encoded)) != encoded
        ):
            raise ValueError
        selected = Path(encoded).expanduser()
        if not selected.parts or selected.name in {"", ".", ".."}:
            raise ValueError
        raw = selected.absolute()
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ProjectCreateError("project-target-invalid", "The project target cannot be resolved safely.") from None
    if raw.name in {"", ".", ".."}:
        raise ProjectCreateError("project-target-invalid", "The project target must name a new directory.")
    parent = raw.parent
    try:
        metadata = parent.stat()
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
    shared_writable = bool(mode & (stat.S_IWGRP | stat.S_IWOTH))
    if not shared_writable:
        return False
    if not mode & stat.S_ISVTX:
        return True
    # Sticky directories protect entries only from peers, not from their owner.
    # Trust the invoking user and the system administrator, as conventional
    # root-owned temporary directories require.
    return not hasattr(os, "geteuid") or owner not in {0, os.geteuid()}


def _open_parent(parent: Path) -> int:
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NONBLOCK")
        or any(function not in os.supports_dir_fd for function in (os.mkdir, os.open, os.stat))
        or os.stat not in os.supports_follow_symlinks
        or os.listdir not in os.supports_fd
        or not hasattr(os, "geteuid")
    ):
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "This platform cannot create the project with the required path safety.",
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    absolute = parent.absolute()
    try:
        descriptor = os.open(absolute.anchor, flags)
        try:
            for part in absolute.parts[1:]:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except NotADirectoryError:
                    # macOS reports a symbolic link as ENOTDIR too, so look
                    # before calling the component a link.
                    if not stat.S_ISLNK(os.stat(part, dir_fd=descriptor, follow_symlinks=False).st_mode):
                        raise ProjectCreateError(
                            "project-parent-invalid",
                            "The target parent or one of its ancestors is not a directory.",
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
            raise ProjectCreateError(
                "project-path-is-symlink", "The target path contains a symbolic link."
            ) from None
        if error.errno in {errno.ENOENT, errno.EACCES, errno.EPERM}:
            raise _parent_access_error(error) from None
        raise ProjectCreateError("project-create-failed", "Project creation failed; no project was created.") from None
    except UnicodeError:
        raise ProjectCreateError("project-target-invalid", "The project target cannot be resolved safely.") from None
    return descriptor


def _reopen_bound_parent(parent: Path, expected_identity: tuple[int, int, int]) -> int:
    """Open *parent* afresh and require the same device, inode, and owner."""

    try:
        descriptor = _open_parent(parent)
    except ProjectCreateError:
        raise ProjectCreateError(
            "project-parent-unverifiable",
            "The requested parent path could not be reverified safely.",
        ) from None
    try:
        if _descriptor_identity(descriptor) != expected_identity:
            raise ProjectCreateError(
                "project-parent-changed",
                "The requested parent path changed while the project was being created.",
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
        raise ProjectCreateError("project-create-failed", "Project creation failed; no project was created.") from None
    raise ProjectCreateError("project-target-exists", "The target already exists; project new never overwrites it.")


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
    raise ProjectCreateError("project-create-failed", "Project creation failed; no project was created.")


def _open_stage(parent_descriptor: int, stage_name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    return os.open(stage_name, flags, dir_fd=parent_descriptor)


def _open_planned_file(parent_descriptor: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    return os.open(name, flags, dir_fd=parent_descriptor)


def _lock_parent(parent_descriptor: int) -> None:
    """Serialize creation in the parent, giving up if another holder keeps the lock."""

    unavailable = ProjectCreateError(
        "project-create-safety-unavailable",
        "This platform cannot serialize concurrent project creation safely.",
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


def _list_directory(directory_descriptor: int) -> list[str]:
    """List through a fresh descriptor so earlier scans cannot leave it at EOF."""

    fresh = _open_stage(directory_descriptor, ".")
    try:
        return os.listdir(fresh)
    finally:
        os.close(fresh)


def _descriptor_identity(descriptor: int) -> tuple[int, int, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(errno.ENOTDIR, "staging path is not a directory")
    return metadata.st_dev, metadata.st_ino, metadata.st_uid


def _require_stage_identity(workspace_descriptor: int, stage_name: str, stage_descriptor: int) -> None:
    expected = _descriptor_identity(stage_descriptor)
    metadata = os.stat(stage_name, dir_fd=workspace_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or (metadata.st_dev, metadata.st_ino, metadata.st_uid) != expected
    ):
        raise ProjectCreateError("project-create-failed", "Project creation failed; no project was created.")


def _entry_matches_descriptor(parent_descriptor: int, name: str, descriptor: int) -> bool:
    try:
        expected = _descriptor_identity(descriptor)
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except (OSError, UnicodeError):
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and (metadata.st_dev, metadata.st_ino, metadata.st_uid) == expected
    )


def _publication_state(
    parent_descriptor: int,
    target_name: str,
    stage_name: str | None,
    stage_descriptor: int | None,
) -> str:
    if stage_name is None or stage_descriptor is None:
        return "none"
    target_matches = _entry_matches_descriptor(parent_descriptor, target_name, stage_descriptor)
    stage_matches = _entry_matches_descriptor(parent_descriptor, stage_name, stage_descriptor)
    if target_matches and not stage_matches:
        return "target"
    if stage_matches and not target_matches:
        return "stage"
    return "detached"


def _build_project_plan(
    package: str,
    version: _ProjectVersion,
    release_bundle: _ReleaseBundle | None,
    *,
    templates: tuple[tuple[str, bytes, int], ...],
    autoform_source: str,
    autoform_ref: str,
) -> tuple[tuple[_ScaffoldFile, ...], bool]:
    files = list(_core_project_plan(package, version, release_bundle))
    scaffold_files, _ = _scaffold_plan(
        templates,
        title=package,
        repository_url="",
        autoform_source=autoform_source or DEFAULT_AUTOFORM_SOURCE,
        autoform_ref=autoform_ref,
    )
    # The shared plan has already reduced installer modes to Git's executable
    # bit, so `init` and `project new` publish the same canonical modes.
    files.extend(scaffold_files)
    return tuple(sorted(files, key=lambda item: item.relative)), bool(autoform_ref)


def _core_project_plan(
    package: str,
    version: _ProjectVersion,
    release_bundle: _ReleaseBundle | None,
) -> tuple[_ScaffoldFile, ...]:
    # Only a catalog release has a resolved manifest; `lake update` writes one
    # for any other pair.
    manifest = (
        ()
        if release_bundle is None
        else (_ScaffoldFile("lake-manifest.json", _lake_manifest(package, release_bundle), 0o644),)
    )
    return (
        _ScaffoldFile("lean-toolchain", f"{version.lean_toolchain}\n".encode(), 0o644),
        _ScaffoldFile(
            "lakefile.toml",
            (
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
            ).encode(),
            0o644,
        ),
        *manifest,
        _ScaffoldFile(
            f"src/{package}.lean",
            (
                "import Mathlib\n\n"
                f"namespace {package}\n\n"
                "/-- Marker declaration for the initial project build. -/\n"
                "def autoformProjectInitialized : Bool := true\n\n"
                f"end {package}\n"
            ).encode(),
            0o644,
        ),
    )


def _load_release_bundle(release: SupportedRelease) -> _ReleaseBundle:
    try:
        descriptor = _load_creation_release_descriptor(release)
        manifest_bytes = (
            files("autoform_cli.project").joinpath(descriptor.manifest_resource).read_bytes()
        )
        return _parse_release_bundle(manifest_bytes, release, descriptor.module_roots)
    except ProjectCreateError:
        raise
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError, MemoryError):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The bundled release manifest is invalid.",
        ) from None


def _load_creation_release_descriptor(
    release: SupportedRelease,
) -> _CreationReleaseDescriptor:
    if _RELEASE_ID.fullmatch(release.id) is None:
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The bundled project-creation release metadata is invalid.",
        )
    resource = f"creation-release-{release.id}.json"
    try:
        payload = json.loads(
            files("autoform_cli.project").joinpath(resource).read_bytes(),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError, MemoryError):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The bundled project-creation release metadata is invalid.",
        ) from None
    expected_release = {
        "id": release.id,
        "lean_toolchain": release.lean_toolchain,
        "mathlib_git": release.mathlib_git,
        "mathlib_rev": release.mathlib_rev,
        "mathlib_commit": release.mathlib_commit,
    }
    roots = payload.get("production_module_roots") if type(payload) is dict else None
    manifest_resource = payload.get("lake_manifest") if type(payload) is dict else None
    if (
        type(payload) is not dict
        or set(payload)
        != {"schema", "release", "lake_manifest", "production_module_roots"}
        or payload.get("schema") != _CREATION_RELEASE_SCHEMA
        or payload.get("release") != expected_release
        or type(manifest_resource) is not str
        or not _safe_resource_name(manifest_resource)
        or type(roots) is not list
        or not roots
        or any(
            type(root) is not str or _PACKAGE_NAME.fullmatch(root) is None
            for root in roots
        )
        or len({root.casefold() for root in roots}) != len(roots)
    ):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The bundled project-creation release metadata is invalid.",
        )
    return _CreationReleaseDescriptor(manifest_resource, tuple(roots))


def _safe_resource_name(value: str) -> bool:
    return (
        value.endswith(".json")
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\0" not in value
        and all(0x20 <= ord(character) < 0xD800 for character in value)
    )


def _parse_release_bundle(
    manifest_bytes: bytes,
    release: SupportedRelease,
    module_roots: tuple[str, ...],
) -> _ReleaseBundle:
    try:
        payload = json.loads(
            manifest_bytes,
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, TypeError, UnicodeError, ValueError, RecursionError, MemoryError):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The bundled release manifest is invalid.",
        ) from None
    roots = frozenset(module_roots)
    packages = payload.get("packages") if type(payload) is dict else None
    if (
        type(payload) is not dict
        or set(payload) != _MANIFEST_FIELDS
        or payload.get("version") != "1.2.0"
        or payload.get("packagesDir") != ".lake/packages"
        or payload.get("name") != ""
        or payload.get("lakeDir") != ".lake"
        or payload.get("fixedToolchain") is not False
        or type(packages) is not list
        or not packages
    ):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The bundled release manifest is invalid.",
        )
    names: list[str] = []
    for entry in packages:
        if not _valid_manifest_package(entry):
            raise ProjectCreateError(
                "project-create-validation-failed",
                "The bundled release manifest is invalid.",
            )
        names.append(entry["name"])
    direct = [entry for entry in packages if entry.get("inherited") is False]
    folded_roots = {root.casefold() for root in roots}
    if (
        len({name.casefold() for name in names}) != len(names)
        or len(direct) != 1
        or direct[0].get("name") != "mathlib"
        or direct[0].get("url") != release.mathlib_git
        or direct[0].get("inputRev") != release.mathlib_rev
        or direct[0].get("rev") != release.mathlib_commit
        or direct[0].get("subDir") is not None  # releases load Mathlib from its repository root
        or direct[0].get("configFile") != "lakefile.lean"
        or direct[0].get("manifestFile") != "lake-manifest.json"
        or not _TOOLCHAIN_MODULE_ROOTS <= roots
        or not _MATHLIB_PRODUCTION_ROOTS <= roots
        or any(name.casefold() not in folded_roots for name in names)
    ):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The bundled release manifest does not match the release catalog.",
        )
    return _ReleaseBundle(manifest_bytes=manifest_bytes, module_roots=roots)


def _valid_manifest_package(entry: object) -> bool:
    if type(entry) is not dict or set(entry) != _MANIFEST_PACKAGE_FIELDS:
        return False
    url = entry["url"]
    name = entry["name"]
    scope = entry["scope"]
    revision = entry["rev"]
    input_revision = entry["inputRev"]
    config_file = entry["configFile"]
    manifest_file = entry["manifestFile"]
    subdirectory = entry["subDir"]
    return (
        type(url) is str
        and _safe_https_git_url(url)
        and entry["type"] == "git"
        and type(name) is str
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name) is not None
        and type(scope) is str
        and type(revision) is str
        and _FULL_SHA.fullmatch(revision) is not None
        and type(input_revision) is str
        and bool(input_revision)
        and all(ord(character) >= 0x20 for character in input_revision)
        and type(entry["inherited"]) is bool
        and _safe_manifest_relative(config_file)
        and _safe_manifest_relative(manifest_file)
        and (subdirectory is None or _safe_manifest_relative(subdirectory))
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


def _safe_manifest_relative(value: object) -> bool:
    if (
        type(value) is not str
        or value in {"", ".", ".."}
        or "\\" in value
        or "\0" in value
        or any(
            ord(character) < 0x20 or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _lake_manifest(package: str, bundle: _ReleaseBundle) -> bytes:
    payload = json.loads(bundle.manifest_bytes)
    payload["name"] = package
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _plan_tree(plan: tuple[_ScaffoldFile, ...]) -> dict[str, object]:
    if type(plan) is not tuple:
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The generated project did not satisfy Autoform's project contracts.",
        )
    tree: dict[str, object] = {}
    for item in plan:
        if (
            type(item) is not _ScaffoldFile
            or type(item.relative) is not str
            or type(item.content) is not bytes
            or type(item.mode) is not int
            or item.mode != item.mode & 0o777
            or item.mode & 0o022
            or not item.mode & 0o400
        ):
            raise ProjectCreateError(
                "project-create-validation-failed",
                "The generated project did not satisfy Autoform's project contracts.",
            )
        path = PurePosixPath(item.relative)
        if (
            not item.relative
            or "\\" in item.relative
            or "\0" in item.relative
            or any(
                ord(character) < 0x20 or 0xD800 <= ord(character) <= 0xDFFF
                for character in item.relative
            )
            or path.is_absolute()
            or path.as_posix() != item.relative
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ProjectCreateError(
                "project-create-validation-failed",
                "The generated project did not satisfy Autoform's project contracts.",
            )
        branch = tree
        for part in path.parts[:-1]:
            child = branch.setdefault(part, {})
            if not isinstance(child, dict):
                raise ProjectCreateError(
                    "project-create-validation-failed",
                    "The generated project did not satisfy Autoform's project contracts.",
                )
            branch = child
        if path.name in branch:
            raise ProjectCreateError(
                "project-create-validation-failed",
                "The generated project did not satisfy Autoform's project contracts.",
            )
        branch[path.name] = item
    return tree


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short project file write")
        offset += written


def _materialize_project(root_descriptor: int, plan: tuple[_ScaffoldFile, ...]) -> None:
    tree = _plan_tree(plan)

    def write_directory(descriptor: int, entries: dict[str, object]) -> None:
        for name, entry in sorted(entries.items()):
            if isinstance(entry, dict):
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                child = _open_stage(descriptor, name)
                try:
                    if _descriptor_identity(child) != (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_uid,
                    ):
                        raise OSError(errno.ESTALE, "project directory changed")
                    write_directory(child, entry)
                    os.fchmod(child, 0o755)
                    os.fsync(child)
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (current.st_dev, current.st_ino, current.st_uid) != (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_uid,
                    ):
                        raise OSError(errno.ESTALE, "project directory changed")
                finally:
                    os.close(child)
                continue
            if not isinstance(entry, _ScaffoldFile):
                raise OSError(errno.EINVAL, "invalid project plan")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            child = os.open(name, flags, 0o600, dir_fd=descriptor)
            try:
                _write_all(child, entry.content)
                os.fchmod(child, entry.mode)
                os.fsync(child)
            finally:
                os.close(child)
        if set(_list_directory(descriptor)) != set(entries):
            raise OSError(errno.ESTALE, "project directory changed")

    write_directory(root_descriptor, tree)


def _verify_project_plan(
    root_descriptor: int,
    plan: tuple[_ScaffoldFile, ...],
    *,
    root_mode: int = 0o700,
) -> None:
    tree = _plan_tree(plan)
    root = os.fstat(root_descriptor)
    if (
        not stat.S_ISDIR(root.st_mode)
        or root.st_uid != os.geteuid()
        or stat.S_IMODE(root.st_mode) != root_mode
    ):
        raise OSError(errno.ESTALE, "project root changed")

    def stable_metadata(metadata: os.stat_result) -> tuple[int, ...]:
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

    def verify_directory(descriptor: int, entries: dict[str, object]) -> None:
        directory_before = os.fstat(descriptor)
        if set(_list_directory(descriptor)) != set(entries):
            raise OSError(errno.ESTALE, "project directory changed")
        for name, entry in sorted(entries.items()):
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if isinstance(entry, dict):
                if not stat.S_ISDIR(metadata.st_mode):
                    raise OSError(errno.ESTALE, "project directory changed")
                child = _open_stage(descriptor, name)
                try:
                    opened = os.fstat(child)
                    if (
                        stable_metadata(opened) != stable_metadata(metadata)
                        or opened.st_uid != os.geteuid()
                        or stat.S_IMODE(opened.st_mode) != 0o755
                    ):
                        raise OSError(errno.ESTALE, "project directory changed")
                    verify_directory(child, entry)
                    after = os.fstat(child)
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if not (stable_metadata(opened) == stable_metadata(after) == stable_metadata(current)):
                        raise OSError(errno.ESTALE, "project directory changed")
                finally:
                    os.close(child)
                continue
            if not isinstance(entry, _ScaffoldFile) or not stat.S_ISREG(metadata.st_mode):
                raise OSError(errno.ESTALE, "project file changed")
            child = _open_planned_file(descriptor, name)
            try:
                opened = os.fstat(child)
                content = bytearray()
                while len(content) <= len(entry.content):
                    chunk = os.read(
                        child,
                        min(1024 * 1024, len(entry.content) + 1 - len(content)),
                    )
                    if not chunk:
                        break
                    content.extend(chunk)
                after = os.fstat(child)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or stable_metadata(metadata) != stable_metadata(opened)
                    or stable_metadata(opened) != stable_metadata(after)
                    or stable_metadata(after) != stable_metadata(current)
                    or after.st_nlink != 1
                    or after.st_uid != os.geteuid()
                    or stat.S_IMODE(after.st_mode) != entry.mode
                    or bytes(content) != entry.content
                ):
                    raise OSError(errno.ESTALE, "project file changed")
            finally:
                os.close(child)
        directory_after = os.fstat(descriptor)
        if stable_metadata(directory_before) != stable_metadata(directory_after) or set(
            _list_directory(descriptor)
        ) != set(entries):
            raise OSError(errno.ESTALE, "project directory changed")

    verify_directory(root_descriptor, tree)


def _validate_staged_project(
    stage_descriptor: int,
    plan: tuple[_ScaffoldFile, ...],
    package: str,
    version: _ProjectVersion,
    release_bundle: _ReleaseBundle | None,
) -> None:
    _verify_project_plan(stage_descriptor, plan)
    indexed = {item.relative: item for item in plan}
    expected_core = _core_project_plan(package, version, release_bundle)
    if ("lake-manifest.json" in indexed) != (release_bundle is not None) or any(
        indexed.get(expected.relative) != expected for expected in expected_core
    ):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The staged project did not satisfy Autoform's project contracts.",
        )
    _validate_roadmap_plan(plan)
    _verify_project_plan(stage_descriptor, plan)


def _validate_roadmap_plan(plan: tuple[_ScaffoldFile, ...]) -> None:
    roadmap = [
        item for item in plan if item.relative.startswith("blueprint/roadmap/") and item.relative.endswith(".md")
    ]
    if len(roadmap) != 1 or roadmap[0].relative != "blueprint/roadmap/README.md":
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The staged project did not satisfy Autoform's project contracts.",
        )
    try:
        text = roadmap[0].content.decode("utf-8")
    except UnicodeError:
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The staged project did not satisfy Autoform's project contracts.",
        ) from None
    parsed, issues = _parse_node("roadmap", Path("roadmap/README.md"), text)
    if issues or parsed is None or parsed.statement_targets or parsed.proof_targets:
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The staged project did not satisfy Autoform's project contracts.",
        )


def _rename_noreplace(
    source_parent_descriptor: int,
    source: str,
    target_parent_descriptor: int,
    target: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(
            source_parent_descriptor,
            source_bytes,
            target_parent_descriptor,
            target_bytes,
            0x00000004,
        )
    elif hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(
            source_parent_descriptor,
            source_bytes,
            target_parent_descriptor,
            target_bytes,
            1,
        )
    else:
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "This platform cannot atomically publish a new project without replacement.",
        )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, os.strerror(error), target)
    if error in {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP}:
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "This platform cannot atomically publish a new project without replacement.",
        )
    raise OSError(error, os.strerror(error), target)


__all__ = ["ProjectCreateError", "ProjectCreateResult", "create_project"]
