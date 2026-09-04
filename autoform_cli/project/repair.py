"""Conservatively add unambiguous missing files to an existing project."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..scaffold import ScaffoldError, scaffold_project
from .create import ProjectCreateError, _open_parent, _rename_noreplace
from .inplace import _filesystem_supported, _noreplace_function
from .inspect import inspect_project

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import compatibility
    fcntl = None  # type: ignore[assignment]

PROJECT_REPAIR_SCHEMA = "autoform-project-repair/v1"
_RENDER_SOURCE = "https://github.com/facebookresearch/autoform-bot.git"
_RENDER_REF = "0" * 40
_REQUIRED_INPUTS = {
    "README.md": ("title",),
    "blueprint/README.md": ("title",),
    "blueprint/roadmap/README.md": ("title",),
    "mkdocs.yml": ("title", "repository-url"),
    ".github/workflows/autoform-verify.yml": ("autoform-source", "autoform-ref"),
    ".github/workflows/blueprint-pages.yml": ("autoform-source", "autoform-ref"),
}
_WORKFLOW_PATHS = (
    ".github/workflows/autoform-verify.yml",
    ".github/workflows/blueprint-pages.yml",
)


@dataclass(frozen=True, order=True, slots=True)
class ProjectRepairConflict:
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "path": self.path}


class ProjectRepairError(ValueError):
    """Repair would require changing or guessing existing project content."""

    def __init__(
        self,
        conflicts: tuple[ProjectRepairConflict, ...],
        *,
        code: str = "project-repair-conflict",
        written: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.conflicts = conflicts
        self.written = written
        self.message = "The project cannot be repaired without changing or guessing existing content."
        super().__init__(self.message)

    def as_dict(self) -> dict[str, object]:
        return {
            "error": {
                "code": self.code,
                "conflicts": [conflict.as_dict() for conflict in self.conflicts],
                "message": self.message,
            },
            "ok": False,
            "schema": PROJECT_REPAIR_SCHEMA,
            "written": list(self.written),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ProjectRepairResult:
    dry_run: bool
    package: str
    release: str
    planned: tuple[str, ...]
    written: tuple[str, ...]
    converged: tuple[str, ...]
    preserved: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "converged": list(self.converged),
            "dry_run": self.dry_run,
            "ok": True,
            "package": self.package,
            "planned": list(self.planned),
            "preserved": list(self.preserved),
            "release": self.release,
            "schema": PROJECT_REPAIR_SCHEMA,
            "target": ".",
            "written": list(self.written),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class _PlannedFile:
    path: str
    content: bytes
    mode: int
    required_inputs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ParentIdentity:
    name: str
    path: str
    identity: tuple[int, int]


def repair_project(
    target: str | Path,
    *,
    dry_run: bool = False,
    title: str | None = None,
    repository_url: str | None = None,
    autoform_source: str | None = None,
    autoform_ref: str | None = None,
) -> ProjectRepairResult:
    """Add only absent, canonically generated Autoform overlay files."""

    root = _project_root(target)
    root_descriptor = _open_root(root)
    written: list[str] = []
    converged: list[str] = []
    try:
        try:
            if fcntl is None:
                raise OSError(errno.ENOSYS, "advisory locks are unavailable")
            fcntl.flock(root_descriptor, fcntl.LOCK_EX)
        except (AttributeError, OSError):
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-safety-unavailable",
                        "The project root cannot be locked for conservative repair.",
                        ".",
                    ),
                ),
                code="project-repair-safety-unavailable",
            ) from None
        if not dry_run and (
            not _filesystem_supported(root_descriptor)
            or _noreplace_function() is None
        ):
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-safety-unavailable",
                        "The project filesystem cannot publish repair files atomically.",
                        ".",
                    ),
                ),
                code="project-repair-safety-unavailable",
            )
        root_identity = _descriptor_identity(root_descriptor)
        _require_root_identity(root_descriptor, root_identity, root)
        _require_private_directory(root_descriptor, ".")
        inspection = inspect_project(root)
        _require_root_identity(root_descriptor, root_identity, root)
        conflicts = _inspection_conflicts(inspection)
        if conflicts:
            raise ProjectRepairError(tuple(sorted(conflicts)))
        assert inspection.lake is not None
        assert inspection.lake.name is not None
        assert inspection.compatibility.release is not None
        _require_config_identity(root_descriptor, inspection)

        try:
            desired = _render_overlay(
                title=title,
                repository_url=repository_url,
                autoform_source=autoform_source,
                autoform_ref=autoform_ref,
            )
        except ProjectRepairError:
            raise
        except (OSError, ValueError):
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-render-failed",
                        "The canonical repair overlay could not be rendered.",
                    ),
                ),
                code="project-repair-failed",
            ) from None
        _require_root_identity(root_descriptor, root_identity, root)
        _require_config_identity(root_descriptor, inspection)
        provided_inputs = frozenset(
            name
            for name, value in (
                ("title", title),
                ("repository-url", repository_url),
                ("autoform-source", autoform_source),
                ("autoform-ref", autoform_ref),
            )
            if value is not None
        )
        recovery_conflicts = _find_recovery_conflicts(root_descriptor, desired)
        if recovery_conflicts:
            raise ProjectRepairError(tuple(sorted(recovery_conflicts)))
        desired = _scope_workflow_files(
            root_descriptor, desired, provided_inputs
        )
        planned, preserved, path_conflicts = _plan(
            root_descriptor, desired, provided_inputs
        )
        if path_conflicts:
            raise ProjectRepairError(tuple(sorted(path_conflicts)))
        planned_paths = tuple(item.path for item in planned)
        for item in planned:
            _validate_parent_chain(root_descriptor, item.path)
        if dry_run or not planned:
            return ProjectRepairResult(
                dry_run=dry_run,
                package=inspection.lake.name,
                release=inspection.compatibility.release,
                planned=planned_paths,
                written=(),
                converged=(),
                preserved=preserved,
            )
        for item in planned:
            try:
                _require_root_identity(root_descriptor, root_identity, root)
                _require_config_identity(root_descriptor, inspection)
                outcome = _publish(
                    root,
                    root_descriptor,
                    root_identity,
                    item,
                    inspection,
                )
            except OSError:
                raise ProjectRepairError(
                    (
                        ProjectRepairConflict(
                            "project-repair-write-failed",
                            "A managed path could not be traversed or published safely.",
                            item.path,
                        ),
                    ),
                    code="project-repair-failed",
                    written=tuple(written),
                ) from None
            except ProjectRepairError as error:
                published = tuple((*written, *error.written))
                raise ProjectRepairError(
                    error.conflicts,
                    code=error.code,
                    written=published,
                ) from None
            (written if outcome == "written" else converged).append(item.path)
        return ProjectRepairResult(
            dry_run=False,
            package=inspection.lake.name,
            release=inspection.compatibility.release,
            planned=planned_paths,
            written=tuple(written),
            converged=tuple(converged),
            preserved=preserved,
        )
    except OSError:
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-io-failed",
                    "A project path could not be inspected or repaired safely.",
                    ".",
                ),
            ),
            code="project-repair-failed",
            written=tuple(written),
        ) from None
    finally:
        pending_error = sys.exc_info()[1]
        try:
            os.close(root_descriptor)
        except OSError:
            conflict = ProjectRepairConflict(
                "project-repair-close-failed",
                "The project root descriptor could not be closed.",
                ".",
            )
            if isinstance(pending_error, ProjectRepairError):
                raise ProjectRepairError(
                    (*pending_error.conflicts, conflict),
                    code=pending_error.code,
                    written=pending_error.written,
                ) from None
            raise ProjectRepairError(
                (conflict,),
                code="project-repair-failed",
                written=tuple(written),
            ) from None


def _path_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


def _open_root(root: Path) -> int:
    try:
        return _open_parent(root)
    except ProjectCreateError as error:
        if error.code == "project-create-safety-unavailable":
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-safety-unavailable",
                        "The platform cannot traverse the project with the required path safety.",
                        ".",
                    ),
                ),
                code="project-repair-safety-unavailable",
            ) from None
        if error.code == "project-path-is-symlink":
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-target-invalid",
                        "The repair target path must not contain a symbolic link.",
                        ".",
                    ),
                )
            ) from None
        raise _race_conflict(".", "The project root changed during repair.") from None


def _require_root_identity(
    descriptor: int, expected: tuple[int, int], path: Path
) -> None:
    metadata = os.fstat(descriptor)
    try:
        named = path.stat(follow_symlinks=False)
    except OSError:
        raise _race_conflict(".", "The project root changed during repair.") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or (metadata.st_dev, metadata.st_ino) != expected
        or (named.st_dev, named.st_ino) != expected
    ):
        raise _race_conflict(".", "The project root changed during repair.")
    _require_private_directory(descriptor, ".")


def _project_root(target: str | Path) -> Path:
    try:
        requested = Path(target).expanduser()
        if requested.is_symlink():
            raise OSError
        root = requested.absolute()
    except (OSError, RuntimeError, ValueError):
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-target-invalid",
                    "The repair target must be an existing project directory.",
                ),
            )
        ) from None
    if not root.is_dir() or not (root / "lakefile.toml").is_file():
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-target-invalid",
                    "The repair target must be the existing project root.",
                    "lakefile.toml",
                ),
            )
        )
    return root


def _require_config_identity(root_descriptor: int, inspection) -> None:
    assert inspection.lake is not None
    assert inspection.lean is not None
    expected = {
        inspection.lake.path: inspection.lake.sha256,
        inspection.lean.path: inspection.lean.sha256,
    }
    for relative, digest in expected.items():
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(relative, flags, dir_fd=root_descriptor)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise OSError(errno.EINVAL, "project configuration is not a regular file")
                content = os.read(descriptor, 2 * 1024 * 1024 + 1)
            finally:
                os.close(descriptor)
        except OSError:
            raise _race_conflict(relative, "Project configuration changed during repair.") from None
        if not stat.S_ISREG(metadata.st_mode) or hashlib.sha256(content).hexdigest() != digest:
            raise _race_conflict(relative, "Project configuration changed during repair.")


def _inspection_conflicts(inspection) -> list[ProjectRepairConflict]:
    conflicts = [
        ProjectRepairConflict(
            "project-repair-inspection-failed",
            diagnostic.message,
            diagnostic.path,
        )
        for diagnostic in inspection.diagnostics
        if diagnostic.severity == "error"
    ]
    if inspection.autoform.manifest_path is not None:
        conflicts.append(
            ProjectRepairConflict(
                "project-repair-workspace-unsupported",
                "Use workspace and blueprint commands for a manifest-managed repository; legacy project repair would create an unrelated blueprint/ vault.",
                inspection.autoform.manifest_path,
            )
        )
    if inspection.lake is None or inspection.lake.name is None:
        conflicts.append(
            ProjectRepairConflict(
                "project-repair-package-indeterminate",
                "The existing Lake package name is required for repair.",
                "lakefile.toml",
            )
        )
    if inspection.lean is None:
        conflicts.append(
            ProjectRepairConflict(
                "project-repair-toolchain-indeterminate",
                "An existing lean-toolchain is required for repair.",
                "lean-toolchain",
            )
        )
    if inspection.compatibility.status != "supported" or inspection.compatibility.release is None:
        conflicts.append(
            ProjectRepairConflict(
                "project-repair-release-indeterminate",
                "Repair requires an existing Lean/Mathlib pair from the bundled release catalog.",
            )
        )
    return conflicts


def _render_overlay(
    *,
    title: str | None,
    repository_url: str | None,
    autoform_source: str | None,
    autoform_ref: str | None,
) -> tuple[_PlannedFile, ...]:
    if (autoform_source is None) != (autoform_ref is None):
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-input-invalid",
                    "--autoform-source and --autoform-ref must be supplied together.",
                ),
            ),
            code="project-repair-input-invalid",
        )
    if autoform_source is not None and (
        not autoform_source.strip() or not autoform_ref or not autoform_ref.strip()
    ):
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-input-invalid",
                    "--autoform-source and --autoform-ref must both be nonempty.",
                ),
            ),
            code="project-repair-input-invalid",
        )
    with tempfile.TemporaryDirectory(prefix="autoform-project-repair-") as temporary:
        root = Path(temporary)
        try:
            result = scaffold_project(
                root,
                title=title if title is not None else "Autoform repair placeholder",
                repository_url=repository_url if repository_url is not None else "",
                autoform_source=(
                    autoform_source if autoform_source is not None else _RENDER_SOURCE
                ),
                autoform_ref=autoform_ref if autoform_ref is not None else _RENDER_REF,
                discover_plugin_pin=False,
            )
        except ScaffoldError as error:
            raise ProjectRepairError(
                tuple(
                    ProjectRepairConflict("project-repair-input-invalid", issue)
                    for issue in error.issues
                ),
                code="project-repair-input-invalid",
            ) from None
        if result.unpinned:
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-input-invalid",
                        "The supplied workflow provenance did not produce pinned workflows.",
                    ),
                ),
                code="project-repair-input-invalid",
            )
        rendered = []
        for relative in sorted(result.written):
            path = root / relative
            rendered.append(
                _PlannedFile(
                    relative,
                    path.read_bytes(),
                    stat.S_IMODE(path.stat().st_mode),
                    _REQUIRED_INPUTS.get(relative, ()),
                )
            )
        return tuple(rendered)


def _managed_path_state(root_descriptor: int, path: str) -> str:
    root_device = os.fstat(root_descriptor).st_dev
    descriptor = os.dup(root_descriptor)
    try:
        for part in PurePosixPath(path).parts[:-1]:
            flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                return "absent"
            except OSError:
                return "unsafe"
            try:
                child_device = os.fstat(child).st_dev
            except BaseException:
                os.close(child)
                raise
            if child_device != root_device:
                os.close(child)
                return "unsafe"
            os.close(descriptor)
            descriptor = child
        try:
            os.stat(
                PurePosixPath(path).name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unsafe"
        return "exists"
    finally:
        os.close(descriptor)


def _scope_workflow_files(
    root_descriptor: int,
    desired: tuple[_PlannedFile, ...],
    provided_inputs: frozenset[str],
) -> tuple[_PlannedFile, ...]:
    if {"autoform-source", "autoform-ref"} <= provided_inputs:
        return desired
    states = tuple(
        _managed_path_state(root_descriptor, path) for path in _WORKFLOW_PATHS
    )
    if states != ("absent", "absent"):
        return desired
    return tuple(item for item in desired if not item.path.startswith(".github/"))


def _find_recovery_conflicts(
    root_descriptor: int, desired: tuple[_PlannedFile, ...]
) -> list[ProjectRepairConflict]:
    conflicts: list[ProjectRepairConflict] = []
    root_device = os.fstat(root_descriptor).st_dev
    for item in desired:
        descriptor = os.dup(root_descriptor)
        try:
            safe_parent = True
            for part in PurePosixPath(item.path).parts[:-1]:
                flags = (
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0)
                )
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except OSError:
                    safe_parent = False
                    break
                try:
                    child_device = os.fstat(child).st_dev
                except BaseException:
                    os.close(child)
                    raise
                if child_device != root_device:
                    os.close(child)
                    safe_parent = False
                    break
                os.close(descriptor)
                descriptor = child
            if not safe_parent:
                continue
            name = PurePosixPath(item.path).name
            try:
                entries = os.listdir(descriptor)
            except OSError:
                continue
            parent = PurePosixPath(item.path).parent
            for entry in sorted(entries):
                if not re.fullmatch(
                    rf"\.{re.escape(name)}\.autoform-repair-[0-9a-f]{{16}}",
                    entry,
                ):
                    continue
                orphan_path = (
                    entry if parent == PurePosixPath(".") else f"{parent}/{entry}"
                )
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-recovery-required",
                        "An unverified repair temporary file requires manual recovery.",
                        orphan_path,
                    )
                )
        finally:
            os.close(descriptor)
    return conflicts


def _plan(
    root_descriptor: int,
    desired: tuple[_PlannedFile, ...],
    provided_inputs: frozenset[str],
) -> tuple[tuple[_PlannedFile, ...], tuple[str, ...], list[ProjectRepairConflict]]:
    planned: list[_PlannedFile] = []
    preserved: list[str] = []
    conflicts: list[ProjectRepairConflict] = []
    root_device = os.fstat(root_descriptor).st_dev
    for item in desired:
        parent_descriptor = os.dup(root_descriptor)
        try:
            walked: list[str] = []
            blocked = False
            for part in PurePosixPath(item.path).parts[:-1]:
                walked.append(part)
                path = "/".join(walked)
                try:
                    parent_descriptor = _open_existing_directory(
                        parent_descriptor,
                        part,
                        path,
                        expected_device=root_device,
                    )
                except ProjectRepairError as error:
                    conflicts.extend(error.conflicts)
                    blocked = True
                    break
            if blocked:
                continue
            name = PurePosixPath(item.path).name
            try:
                orphaned = sorted(
                    entry
                    for entry in os.listdir(parent_descriptor)
                    if re.fullmatch(
                        rf"\.{re.escape(name)}\.autoform-repair-[0-9a-f]{{16}}",
                        entry,
                    )
                )
            except OSError:
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-destination-invalid",
                        "A managed destination could not be inspected safely.",
                        item.path,
                    )
                )
                continue
            for orphan in orphaned:
                parent = PurePosixPath(item.path).parent
                orphan_path = (
                    orphan if parent == PurePosixPath(".") else f"{parent}/{orphan}"
                )
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-recovery-required",
                        "An unverified repair temporary file requires manual recovery.",
                        orphan_path,
                    )
                )
            try:
                metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                missing_inputs = tuple(
                    value for value in item.required_inputs if value not in provided_inputs
                )
                if missing_inputs:
                    flags = ", ".join(f"--{value}" for value in missing_inputs)
                    conflicts.append(
                        ProjectRepairConflict(
                            "project-repair-input-required",
                            f"Repair requires explicit {flags} input to reconstruct this file.",
                            item.path,
                        )
                    )
                else:
                    planned.append(item)
                continue
            except OSError:
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-destination-invalid",
                        "A managed destination could not be inspected safely.",
                        item.path,
                    )
                )
                continue
            if stat.S_ISLNK(metadata.st_mode):
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-destination-symlink",
                        "A managed destination is a symbolic link.",
                        item.path,
                    )
                )
            elif not stat.S_ISREG(metadata.st_mode):
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-destination-not-file",
                        "A managed destination exists and is not a regular file.",
                        item.path,
                    )
                )
            else:
                preserved.append(item.path)
        finally:
            os.close(parent_descriptor)
    return tuple(planned), tuple(sorted(preserved)), conflicts


def _require_private_directory(descriptor: int, path: str) -> None:
    mode = os.fstat(descriptor).st_mode
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-parent-unsafe",
                    "A managed parent directory is group- or world-writable.",
                    path,
                ),
            )
        )


def _validate_parent_chain(root_descriptor: int, path: str) -> None:
    root_device = os.fstat(root_descriptor).st_dev
    parent_descriptor = os.dup(root_descriptor)
    try:
        walked: list[str] = []
        for part in PurePosixPath(path).parts[:-1]:
            walked.append(part)
            parent_descriptor = _open_existing_directory(
                parent_descriptor,
                part,
                "/".join(walked),
                expected_device=root_device,
            )
    finally:
        os.close(parent_descriptor)


def _publish(
    root: Path,
    root_descriptor: int,
    root_identity: tuple[int, int],
    item: _PlannedFile,
    inspection,
) -> str:
    root_device = os.fstat(root_descriptor).st_dev
    parent_descriptor = os.dup(root_descriptor)
    outcome: str | None = None
    try:
        parts = PurePosixPath(item.path).parts
        walked: list[str] = []
        parent_chain: list[_ParentIdentity] = []
        for part in parts[:-1]:
            walked.append(part)
            parent_descriptor = _open_existing_directory(
                parent_descriptor,
                part,
                "/".join(walked),
                expected_device=root_device,
            )
            parent_chain.append(
                _ParentIdentity(
                    name=part,
                    path="/".join(walked),
                    identity=_descriptor_identity(parent_descriptor),
                )
            )
        outcome = _publish_file(
            root_descriptor,
            root,
            root_identity,
            parent_descriptor,
            tuple(parent_chain),
            parts[-1],
            item,
            inspection,
        )
        return outcome
    finally:
        pending_error = sys.exc_info()[1]
        try:
            os.close(parent_descriptor)
        except OSError:
            conflict = ProjectRepairConflict(
                "project-repair-close-failed",
                "A managed parent descriptor could not be closed.",
                item.path,
            )
            if isinstance(pending_error, ProjectRepairError):
                raise ProjectRepairError(
                    (*pending_error.conflicts, conflict),
                    code=pending_error.code,
                    written=pending_error.written,
                ) from None
            raise ProjectRepairError(
                (conflict,),
                code="project-repair-failed",
                written=(item.path,) if outcome == "written" else (),
            ) from None


def _require_parent_chain(
    root_descriptor: int, expected: tuple[_ParentIdentity, ...]
) -> None:
    root_device = os.fstat(root_descriptor).st_dev
    descriptor = os.dup(root_descriptor)
    try:
        for link in expected:
            descriptor = _open_existing_directory(
                descriptor,
                link.name,
                link.path,
                expected_device=root_device,
            )
            if _descriptor_identity(descriptor) != link.identity:
                raise _race_conflict(
                    link.path, "A managed parent directory changed during repair."
                )
    finally:
        os.close(descriptor)


def _open_existing_directory(
    parent_descriptor: int,
    name: str,
    path: str,
    *,
    expected_device: int,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        child = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-parent-missing",
                    "A required managed parent directory is missing.",
                    path,
                ),
            )
        ) from None
    except OSError:
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-parent-not-directory",
                    "A required parent path is not a safe directory.",
                    path,
                ),
            )
        ) from None
    try:
        _require_private_directory(child, path)
        if os.fstat(child).st_dev != expected_device:
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-parent-filesystem",
                        "A managed parent directory is on a different filesystem.",
                        path,
                    ),
                )
            )
    except BaseException:
        os.close(child)
        raise
    os.close(parent_descriptor)
    return child


def _require_temporary_identity(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    expected: tuple[int, int],
) -> None:
    opened = os.fstat(descriptor)
    named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or opened.st_nlink != 1
        or named.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != expected
        or (named.st_dev, named.st_ino) != expected
    ):
        raise OSError(errno.ESTALE, "temporary file changed during repair")


def _require_file_manifest(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    expected_identity: tuple[int, int],
    item: _PlannedFile,
) -> None:
    _require_temporary_identity(
        parent_descriptor, name, descriptor, expected_identity
    )
    metadata = os.fstat(descriptor)
    if metadata.st_size != len(item.content) or stat.S_IMODE(metadata.st_mode) != item.mode:
        raise OSError(errno.ESTALE, "repair file metadata changed")
    offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = len(item.content) + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.lseek(descriptor, offset, os.SEEK_SET)
    if b"".join(chunks) != item.content:
        raise OSError(errno.ESTALE, "repair file content changed")


def _publish_file(
    root_descriptor: int,
    root: Path,
    root_identity: tuple[int, int],
    parent_descriptor: int,
    parent_chain: tuple[_ParentIdentity, ...],
    name: str,
    item: _PlannedFile,
    inspection,
) -> str:
    temporary = f".{name}.autoform-repair-{secrets.token_hex(8)}"
    parent = PurePosixPath(item.path).parent
    temporary_path = (
        temporary if parent == PurePosixPath(".") else f"{parent}/{temporary}"
    )
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    temporary_identity: tuple[int, int] | None = None
    temporary_created = False
    published = False
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_descriptor)
        temporary_created = True
        temporary_metadata = os.fstat(descriptor)
        temporary_identity = temporary_metadata.st_dev, temporary_metadata.st_ino
        view = memoryview(item.content)
        while view:
            count = os.write(descriptor, view)
            if count == 0:
                raise OSError(errno.EIO, "short write")
            view = view[count:]
        os.fchmod(descriptor, item.mode)
        os.fsync(descriptor)
        _require_root_identity(root_descriptor, root_identity, root)
        _require_config_identity(root_descriptor, inspection)
        _require_parent_chain(root_descriptor, parent_chain)
        _require_root_identity(root_descriptor, root_identity, root)
        _require_file_manifest(
            parent_descriptor, temporary, descriptor, temporary_identity, item
        )
        try:
            _rename_noreplace(parent_descriptor, temporary, parent_descriptor, name)
        except FileExistsError:
            winner_descriptor, winner_identity = _concurrent_result(
                parent_descriptor, name, item
            )
            try:
                _require_root_identity(root_descriptor, root_identity, root)
                _require_config_identity(root_descriptor, inspection)
                _require_parent_chain(root_descriptor, parent_chain)
                _require_root_identity(root_descriptor, root_identity, root)
                _require_file_manifest(
                    parent_descriptor,
                    name,
                    winner_descriptor,
                    winner_identity,
                    item,
                )
            finally:
                pending_winner_error = sys.exc_info()[1]
                try:
                    os.close(winner_descriptor)
                except OSError:
                    conflict = ProjectRepairConflict(
                        "project-repair-close-failed",
                        "A concurrent destination descriptor could not be closed.",
                        item.path,
                    )
                    if isinstance(pending_winner_error, ProjectRepairError):
                        raise ProjectRepairError(
                            (*pending_winner_error.conflicts, conflict),
                            code=pending_winner_error.code,
                            written=pending_winner_error.written,
                        ) from None
                    if isinstance(pending_winner_error, OSError):
                        validation_error = _race_conflict(
                            item.path,
                            "A concurrent destination changed during repair.",
                        )
                        raise ProjectRepairError(
                            (*validation_error.conflicts, conflict),
                            code=validation_error.code,
                        ) from None
                    raise ProjectRepairError(
                        (conflict,),
                        code="project-repair-failed",
                    ) from None
            raise _temporary_recovery_error(temporary_path)
        except ProjectCreateError:
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-safety-unavailable",
                        "Atomic no-replace publication is unavailable.",
                        item.path,
                    ),
                ),
                code="project-repair-safety-unavailable",
            ) from None
        published = True
        try:
            _require_file_manifest(
                parent_descriptor, name, descriptor, temporary_identity, item
            )
            _require_root_identity(root_descriptor, root_identity, root)
            _require_config_identity(root_descriptor, inspection)
            _require_parent_chain(root_descriptor, parent_chain)
        except (OSError, ProjectRepairError):
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-recovery-required",
                        "A published file was retained after it or its parent changed; inspect it before retrying.",
                        item.path,
                    ),
                ),
                code="project-repair-recovery-required",
                written=(item.path,),
            ) from None
        try:
            os.fsync(parent_descriptor)
        except OSError:
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-durability-failed",
                        "A managed file was published but its directory could not be synchronized.",
                        item.path,
                    ),
                ),
                code="project-repair-failed",
                written=(item.path,),
            ) from None
        try:
            _require_root_identity(root_descriptor, root_identity, root)
            _require_config_identity(root_descriptor, inspection)
            _require_parent_chain(root_descriptor, parent_chain)
            _require_root_identity(root_descriptor, root_identity, root)
            _require_file_manifest(
                parent_descriptor, name, descriptor, temporary_identity, item
            )
        except (OSError, ProjectRepairError):
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-recovery-required",
                        "A published file was retained after the project changed; inspect it before retrying.",
                        item.path,
                    ),
                ),
                code="project-repair-recovery-required",
                written=(item.path,),
            ) from None
        return "written"
    except ProjectRepairError as error:
        if temporary_created and not published:
            if error.code == "project-repair-recovery-required":
                raise
            raise _temporary_recovery_error(
                temporary_path,
                conflicts=error.conflicts,
                written=error.written,
            ) from None
        raise
    except OSError:
        conflicts = (
            ProjectRepairConflict(
                "project-repair-write-failed",
                "A managed file could not be published safely.",
                item.path,
            ),
        )
        if temporary_created and not published:
            raise _temporary_recovery_error(
                temporary_path,
                conflicts=conflicts,
            ) from None
        raise ProjectRepairError(conflicts, code="project-repair-failed") from None
    finally:
        pending_error = sys.exc_info()[1]
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                conflict = ProjectRepairConflict(
                    "project-repair-close-failed",
                    "A staged file descriptor could not be closed.",
                    item.path,
                )
                if isinstance(pending_error, ProjectRepairError):
                    raise ProjectRepairError(
                        (*pending_error.conflicts, conflict),
                        code=pending_error.code,
                        written=pending_error.written,
                    ) from None
                if temporary_created and not published:
                    raise _temporary_recovery_error(
                        temporary_path,
                        conflicts=(conflict,),
                    ) from None
                raise ProjectRepairError(
                    (conflict,),
                    code="project-repair-failed",
                    written=(item.path,) if published else (),
                ) from None


def _temporary_recovery_error(
    path: str,
    *,
    conflicts: tuple[ProjectRepairConflict, ...] = (),
    written: tuple[str, ...] = (),
) -> ProjectRepairError:
    recovery = ProjectRepairConflict(
        "project-repair-recovery-required",
        "A repair temporary was retained after publication did not complete; inspect it before retrying.",
        path,
    )
    return ProjectRepairError(
        (*conflicts, recovery),
        code="project-repair-recovery-required",
        written=written,
    )


def _concurrent_result(
    parent_descriptor: int, name: str, item: _PlannedFile
) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(errno.EINVAL, "managed destination is not a regular file")
        content = os.read(descriptor, len(item.content) + 1)
    except OSError:
        error = _race_conflict(item.path, "A managed destination changed during repair.")
        if descriptor is not None:
            error = _close_concurrent_descriptor(descriptor, item, error)
        raise error from None
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    if content == item.content:
        assert descriptor is not None
        return descriptor, (metadata.st_dev, metadata.st_ino)
    assert descriptor is not None
    error = _race_conflict(item.path, "A different managed file appeared during repair.")
    raise _close_concurrent_descriptor(descriptor, item, error)


def _close_concurrent_descriptor(
    descriptor: int,
    item: _PlannedFile,
    error: ProjectRepairError,
) -> ProjectRepairError:
    try:
        os.close(descriptor)
    except OSError:
        conflict = ProjectRepairConflict(
            "project-repair-close-failed",
            "A concurrent destination descriptor could not be closed.",
            item.path,
        )
        return ProjectRepairError(
            (*error.conflicts, conflict),
            code=error.code,
            written=error.written,
        )
    return error


def _race_conflict(path: str, message: str) -> ProjectRepairError:
    return ProjectRepairError(
        (ProjectRepairConflict("project-repair-race-conflict", message, path),),
        code="project-repair-race-conflict",
    )


__all__ = [
    "PROJECT_REPAIR_SCHEMA",
    "ProjectRepairConflict",
    "ProjectRepairError",
    "ProjectRepairResult",
    "repair_project",
]
