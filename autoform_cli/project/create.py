"""Create a complete Autoform Lean project and publish it atomically."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from ..graph import GraphValidationError, load_graph
from ..scaffold import ScaffoldError, _normalize_autoform_source, scaffold_project
from .catalog import load_release_catalog
from .inplace import InPlaceCreateError, create_in_current_directory
from .inspect import inspect_project
from .model import SupportedRelease

_PACKAGE_NAME = re.compile(r"[A-Z][A-Za-z0-9]*")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_RESERVED_PACKAGE_NAMES = frozenset({"Mathlib", "Prop", "Sort", "Type"})
_STAGE_ATTEMPTS = 32


class ProjectCreateError(ValueError):
    """A new project could not be created without risking existing data."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        return {"error": {"code": self.code, "message": self.message}, "ok": False}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ProjectCreateResult:
    package: str
    release: str
    target: str
    written: tuple[str, ...]
    workflows_pinned: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "package": self.package,
            "release": self.release,
            "target": self.target,
            "workflows_pinned": self.workflows_pinned,
            "written": list(self.written),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def create_project(
    target: str | Path | None,
    *,
    package: str | None,
    release_id: str | None,
    autoform_source: str = "",
    autoform_ref: str = "",
) -> ProjectCreateResult:
    """Create a project at an absent target or in an empty current directory."""

    current_directory = _is_current_directory_target(target)
    requested = None if current_directory else _validate_target(target)
    package_name = _validate_package(package)
    release = _find_release(release_id)
    workflow_source, workflow_ref = _validate_workflow_pin(autoform_source, autoform_ref)
    if current_directory:
        return _create_project_in_current_directory(
            package_name,
            release,
            autoform_source=workflow_source,
            autoform_ref=workflow_ref,
        )
    assert requested is not None
    return _create_project_at_absent_target(
        requested,
        package_name,
        release,
        autoform_source=workflow_source,
        autoform_ref=workflow_ref,
    )


def _create_project_at_absent_target(
    requested: Path,
    package_name: str,
    release: SupportedRelease,
    *,
    autoform_source: str,
    autoform_ref: str,
) -> ProjectCreateResult:
    """Keep the whole-directory publication used for absent targets."""

    parent = requested.parent
    parent_descriptor = _open_parent(parent)
    workspace_name: str | None = None
    workspace_path: Path | None = None
    workspace_descriptor: int | None = None
    workspace_identity: tuple[int, int] | None = None
    published = False
    try:
        _require_absent(parent_descriptor, requested.name)
        workspace_name = _create_stage(parent_descriptor, requested.name)
        workspace_path = parent / workspace_name
        workspace_metadata = os.stat(
            workspace_name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if not stat.S_ISDIR(workspace_metadata.st_mode):
            raise OSError(errno.ENOTDIR, "staging path is not a directory")
        workspace_identity = workspace_metadata.st_dev, workspace_metadata.st_ino
        workspace_descriptor = _open_stage(parent_descriptor, workspace_name)
        _require_stage_identity(parent_descriptor, workspace_name, workspace_descriptor)
        stage_path = workspace_path / "project"
        stage_path.mkdir(mode=0o700)
        stage_descriptor = _open_stage(workspace_descriptor, "project")
        try:
            written, workflows_pinned = _build_staged_project(
                stage_path,
                package_name,
                release,
                autoform_source=autoform_source,
                autoform_ref=autoform_ref,
            )
            _require_stage_identity(parent_descriptor, workspace_name, workspace_descriptor)
            _validate_staged_project(stage_path, release)
            _require_stage_identity(parent_descriptor, workspace_name, workspace_descriptor)
            os.fchmod(stage_descriptor, 0o755)
            os.fsync(stage_descriptor)
            _require_stage_identity(workspace_descriptor, "project", stage_descriptor)
            try:
                _rename_noreplace(
                    workspace_descriptor,
                    "project",
                    parent_descriptor,
                    requested.name,
                )
            except FileExistsError:
                raise ProjectCreateError(
                    "project-target-exists",
                    "The target already exists; project new never overwrites it.",
                ) from None
            published = True
        finally:
            os.close(stage_descriptor)
        try:
            os.rmdir(workspace_name, dir_fd=parent_descriptor)
        except OSError:
            pass
        workspace_name = None
        workspace_path = None
        workspace_identity = None
        return ProjectCreateResult(
            package=package_name,
            release=release.id,
            target=requested.name,
            written=written,
            workflows_pinned=workflows_pinned,
        )
    except ProjectCreateError:
        raise
    except (GraphValidationError, ScaffoldError):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The staged project did not satisfy Autoform's project contracts.",
        ) from None
    except OSError:
        raise ProjectCreateError(
            "project-create-failed",
            "Project creation failed; no project was created.",
        ) from None
    finally:
        cleanup_failed = False
        if not published and workspace_name is not None:
            if workspace_identity is None:
                cleanup_failed = True
            elif workspace_descriptor is None:
                cleanup_failed = not _remove_owned_empty_stage(
                    parent_descriptor, workspace_name, workspace_identity
                )
            else:
                cleanup_failed = not _remove_owned_stage(
                    parent_descriptor,
                    workspace_name,
                    workspace_descriptor,
                    workspace_identity,
                )
        if workspace_descriptor is not None:
            os.close(workspace_descriptor)
        os.close(parent_descriptor)
        if cleanup_failed:
            raise ProjectCreateError(
                "project-cleanup-failed",
                "Project creation failed and owned temporary files could not be completely removed.",
            )


def _create_project_in_current_directory(
    package_name: str,
    release: SupportedRelease,
    *,
    autoform_source: str,
    autoform_ref: str,
) -> ProjectCreateResult:
    try:
        result = create_in_current_directory(
            package=package_name,
            release=release.id,
            autoform_source=autoform_source,
            autoform_ref=autoform_ref,
            build=lambda stage: _build_staged_project(
                stage,
                package_name,
                release,
                autoform_source=autoform_source,
                autoform_ref=autoform_ref,
            ),
            validate=lambda stage: _validate_staged_project(stage, release),
        )
    except ProjectCreateError:
        raise
    except InPlaceCreateError as error:
        raise ProjectCreateError(error.code, error.message) from None
    except (GraphValidationError, ScaffoldError):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The staged project did not satisfy Autoform's project contracts.",
        ) from None
    except OSError:
        raise ProjectCreateError(
            "project-create-failed",
            "Project creation failed; no project was created.",
        ) from None
    return ProjectCreateResult(
        package=package_name,
        release=release.id,
        target=".",
        written=result.written,
        workflows_pinned=result.workflows_pinned,
    )


def _is_current_directory_target(target: str | Path | None) -> bool:
    try:
        return os.fspath(target) == "."
    except TypeError:
        return False


def _validate_package(package: str | None) -> str:
    if (
        package is None
        or _PACKAGE_NAME.fullmatch(package) is None
        or package in _RESERVED_PACKAGE_NAMES
    ):
        raise ProjectCreateError(
            "project-name-invalid",
            "Project name must be an UpperCamelCase Lean identifier.",
        )
    return package


def _find_release(release_id: str | None) -> SupportedRelease:
    catalog = load_release_catalog()
    release = next((item for item in catalog.releases if item.id == release_id), None)
    if release is None:
        raise ProjectCreateError(
            "project-release-unknown",
            "The requested release is not in the bundled release catalog.",
        )
    return release


def _validate_workflow_pin(source: str, ref: str) -> tuple[str, str]:
    """Validate explicit provenance before creating filesystem state."""

    if not isinstance(source, str) or not isinstance(ref, str):
        raise ProjectCreateError(
            "project-provenance-invalid",
            "Autoform workflow provenance must include a safe Git source and full commit SHA.",
        )
    if not source and not ref:
        return "", ""
    safe_source = _normalize_autoform_source(source)
    normalized_ref = ref.strip().lower()
    if (
        not source
        or not ref
        or safe_source is None
        or _FULL_SHA.fullmatch(normalized_ref) is None
    ):
        raise ProjectCreateError(
            "project-provenance-invalid",
            "Autoform workflow provenance must include a safe Git source and full commit SHA.",
        )
    return safe_source, normalized_ref


def _validate_target(target: str | Path | None) -> Path:
    try:
        if target is None:
            raise ValueError
        raw = Path(target).expanduser().absolute()
    except (OSError, RuntimeError, ValueError):
        raise ProjectCreateError(
            "project-target-invalid", "The project target cannot be resolved safely."
        ) from None
    if raw.name in {"", ".", ".."}:
        raise ProjectCreateError(
            "project-target-invalid", "The project target must name a new directory."
        )
    parent = raw.parent
    if not parent.exists():
        raise ProjectCreateError(
            "project-parent-missing", "The target parent directory does not exist."
        )
    if not parent.is_dir():
        raise ProjectCreateError(
            "project-parent-invalid", "The target parent is not a directory."
        )
    try:
        metadata = parent.stat()
    except OSError:
        raise ProjectCreateError(
            "project-parent-invalid", "The target parent is not a directory."
        ) from None
    writable_by_others = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if writable_by_others and not metadata.st_mode & stat.S_ISVTX:
        raise ProjectCreateError(
            "project-parent-unsafe",
            "The target parent must not be group- or world-writable unless it is sticky.",
        )
    try:
        canonical_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ProjectCreateError(
            "project-parent-invalid", "The target parent is not a directory."
        ) from None
    return canonical_parent / raw.name


def _open_parent(parent: Path) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY") or os.open not in os.supports_dir_fd:
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
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except BaseException:
            os.close(descriptor)
            raise
    except OSError:
        raise ProjectCreateError(
            "project-path-is-symlink", "The target path contains a symbolic link."
        ) from None
    return descriptor


def _require_absent(parent_descriptor: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise ProjectCreateError(
            "project-create-failed", "Project creation failed; no project was created."
        ) from None
    raise ProjectCreateError(
        "project-target-exists", "The target already exists; project new never overwrites it."
    )


def _create_stage(parent_descriptor: int, target_name: str) -> str:
    for _ in range(_STAGE_ATTEMPTS):
        name = f".{target_name}.autoform-new-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            return name
        except FileExistsError:
            continue
    raise ProjectCreateError(
        "project-create-failed", "Project creation failed; no project was created."
    )


def _open_stage(parent_descriptor: int, stage_name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    return os.open(stage_name, flags, dir_fd=parent_descriptor)


def _list_directory(directory_descriptor: int) -> list[str]:
    """List through a fresh descriptor so earlier scans cannot leave it at EOF."""

    fresh = _open_stage(directory_descriptor, ".")
    try:
        return os.listdir(fresh)
    finally:
        os.close(fresh)


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(errno.ENOTDIR, "staging path is not a directory")
    return metadata.st_dev, metadata.st_ino


def _require_stage_identity(
    workspace_descriptor: int, stage_name: str, stage_descriptor: int
) -> None:
    expected = _descriptor_identity(stage_descriptor)
    metadata = os.stat(stage_name, dir_fd=workspace_descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != expected:
        raise ProjectCreateError(
            "project-create-failed", "Project creation failed; no project was created."
        )


def _build_staged_project(
    stage: Path,
    package: str,
    release: SupportedRelease,
    *,
    autoform_source: str,
    autoform_ref: str,
) -> tuple[tuple[str, ...], bool]:
    source = stage / "src" / f"{package}.lean"
    source.parent.mkdir()
    files = {
        stage / "lean-toolchain": f"{release.lean.toolchain}\n",
        stage / "lakefile.toml": (
            f'name = "{package}"\n'
            'version = "0.1.0"\n'
            f'defaultTargets = ["{package}"]\n\n'
            '[[require]]\n'
            'name = "mathlib"\n'
            f'git = "{release.mathlib.git}"\n'
            f'rev = "{release.mathlib.revision}"\n\n'
            '[[lean_lib]]\n'
            f'name = "{package}"\n'
            'srcDir = "src"\n'
        ),
        source: (
            "import Mathlib\n\n"
            f"namespace {package}\n\n"
            "/-- Marker declaration for the initial project build. -/\n"
            "def autoformProjectInitialized : Bool := true\n\n"
            f"end {package}\n"
        ),
    }
    for destination, content in files.items():
        with destination.open("x", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    scaffold = scaffold_project(
        stage,
        title=package,
        autoform_source=autoform_source,
        autoform_ref=autoform_ref,
        discover_plugin_pin=False,
    )
    written = tuple(sorted((*scaffold.written, "lakefile.toml", "lean-toolchain", f"src/{package}.lean")))
    return written, not scaffold.unpinned


def _validate_staged_project(stage: Path, release: SupportedRelease) -> None:
    inspection = inspect_project(stage)
    if (
        not inspection.ok
        or inspection.compatibility.status != "supported"
        or inspection.compatibility.release != release.id
        or inspection.lake is None
    ):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The staged project did not satisfy Autoform's project contracts.",
        )
    load_graph(stage / "blueprint")


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
    if error in {errno.ENOSYS, errno.ENOTSUP}:
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "This platform cannot atomically publish a new project without replacement.",
        )
    raise OSError(error, os.strerror(error), target)


def _remove_owned_stage(
    parent_descriptor: int,
    stage_name: str,
    stage_descriptor: int,
    identity: tuple[int, int],
) -> bool:
    try:
        if _descriptor_identity(stage_descriptor) != identity:
            return False
        metadata = os.stat(stage_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != identity:
            return False
        _remove_directory_contents(stage_descriptor)
        os.rmdir(stage_name, dir_fd=parent_descriptor)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _remove_owned_empty_stage(
    parent_descriptor: int,
    stage_name: str,
    identity: tuple[int, int],
) -> bool:
    """Remove a just-created stage that could not be opened, if still empty."""

    try:
        metadata = os.stat(stage_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            return False
        os.rmdir(stage_name, dir_fd=parent_descriptor)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _remove_directory_contents(directory_descriptor: int) -> None:
    for name in _list_directory(directory_descriptor):
        metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_descriptor,
            )
            try:
                opened = os.fstat(child)
                expected = opened.st_dev, opened.st_ino
                if expected != (metadata.st_dev, metadata.st_ino):
                    raise OSError(errno.ESTALE, "directory changed during cleanup")
                _remove_directory_contents(child)
                current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != expected:
                    raise OSError(errno.ESTALE, "directory changed during cleanup")
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory_descriptor)
        else:
            os.unlink(name, dir_fd=directory_descriptor)


__all__ = ["ProjectCreateError", "ProjectCreateResult", "create_project"]
