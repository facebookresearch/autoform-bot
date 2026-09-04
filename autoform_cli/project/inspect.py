"""Deterministically inspect local Lean project configuration without executing it."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .catalog import load_release_catalog
from .model import (
    PROJECT_INSPECTION_SCHEMA,
    AutoformProject,
    LakeProject,
    LakeTarget,
    LeanProject,
    MathlibProject,
    ProjectCompatibility,
    ProjectDiagnostic,
    ProjectInspection,
    ReleaseCatalog,
)
from ..workspace_manifest import WORKSPACE_FILE, WorkspaceError, parse_workspace

_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_MAX_STRUCTURAL_DEPTH = 128
_PROJECT_MARKERS = (
    "lakefile.toml",
    "lakefile.lean",
    "lean-toolchain",
    "blueprint",
    WORKSPACE_FILE,
)
_TOOLCHAIN = re.compile(r"leanprover/lean4:(?P<version>v[0-9]+\.[0-9]+\.[0-9]+)")
_RESERVOIR_SCOPE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
# Lake's StdVer: a major.minor.patch triple with an optional `-` suffix that
# runs to the end of the string.
_LAKE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[^ \t\r\n]+)?")
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
_LEAN_ID_BEGIN_ESCAPE = "«"
_LEAN_ID_END_ESCAPE = "»"


class _InvalidLakeField(ValueError):
    pass


class _NonportableLakePath(ValueError):
    pass


class _DuplicateMathlibRequirement(ValueError):
    pass


def inspect_project(target: str | Path, *, catalog: ReleaseCatalog | None = None) -> ProjectInspection:
    release_catalog = catalog or load_release_catalog()
    diagnostics: list[ProjectDiagnostic] = []
    root_descriptor = _discover_root(target, diagnostics)
    if root_descriptor is None:
        return _inspection(diagnostics, release_catalog)
    try:
        lake, mathlib = _inspect_lake(root_descriptor, diagnostics)
        lean = _inspect_toolchain(root_descriptor, diagnostics)
        manifest_path, manifest_digest = _optional_digest(
            root_descriptor, "lake-manifest.json", diagnostics
        )
        autoform = _inspect_autoform(root_descriptor, diagnostics)
        git_path = _inspect_git(root_descriptor, diagnostics)
    finally:
        os.close(root_descriptor)
    compatibility = _compatibility(release_catalog, lean, mathlib, diagnostics)
    return ProjectInspection(
        schema=PROJECT_INSPECTION_SCHEMA,
        project_root=".",
        git_path=git_path,
        lake=lake,
        lake_manifest_path=manifest_path,
        lake_manifest_sha256=manifest_digest,
        lean=lean,
        mathlib=mathlib,
        autoform=autoform,
        compatibility=compatibility,
        diagnostics=_ordered(diagnostics),
    )


def _inspection(
    diagnostics: list[ProjectDiagnostic],
    catalog: ReleaseCatalog,
) -> ProjectInspection:
    return ProjectInspection(
        schema=PROJECT_INSPECTION_SCHEMA,
        project_root=None,
        git_path=None,
        lake=None,
        lake_manifest_path=None,
        lake_manifest_sha256=None,
        lean=None,
        mathlib=None,
        autoform=AutoformProject(False, None, None, None, None),
        compatibility=ProjectCompatibility(
            catalog=catalog.schema,
            status="indeterminate",
            release=None,
            recommended_release=catalog.recommended.id,
        ),
        diagnostics=_ordered(diagnostics),
    )


def _discover_root(target: str | Path, diagnostics: list[ProjectDiagnostic]) -> int | None:
    """Return a no-follow descriptor for the nearest enclosing project root.

    Every ancestor is opened with O_NOFOLLOW as it is traversed and the chosen
    descriptor is retained, so no pathname is ever re-resolved after being
    checked. Replacing a directory with a symlink mid-walk fails the open
    instead of redirecting inspection to another project.
    """
    if not _secure_inspection_available(diagnostics):
        return None
    try:
        candidate = Path(target).expanduser().absolute()
    except (OSError, RuntimeError, ValueError):
        _issue(diagnostics, "error", "target-unreadable", "The inspection target cannot be resolved.")
        return None

    descriptors: list[int] = []
    chosen: int | None = None
    try:
        try:
            descriptors.append(_open_directory(candidate.anchor, None))
        except OSError:
            _issue(
                diagnostics,
                "error",
                "project-root-unreadable",
                "The project root cannot be opened safely.",
            )
            return None
        parts = candidate.parts[1:]
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            if part == ".":
                continue
            if part == "..":
                if len(descriptors) > 1:
                    os.close(descriptors.pop())
                continue
            status = _entry_status(descriptors[-1], part)
            if status == "missing":
                _issue(
                    diagnostics,
                    "error",
                    "target-does-not-exist",
                    "The inspection target does not exist.",
                )
                return None
            if status == "symlink":
                if last:
                    _issue(
                        diagnostics, "error", "target-is-symlink", "The inspection target is a symlink."
                    )
                else:
                    _issue(
                        diagnostics,
                        "error",
                        "project-path-is-symlink",
                        "The target path contains a symlink.",
                    )
                return None
            if status == "directory":
                try:
                    descriptors.append(_open_directory(part, descriptors[-1]))
                except OSError:
                    _issue(
                        diagnostics,
                        "error",
                        "project-root-unreadable",
                        "The project root cannot be opened safely.",
                    )
                    return None
                continue
            if last and status == "file":
                break
            if last and status == "other":
                _issue(
                    diagnostics,
                    "error",
                    "target-not-file-or-directory",
                    "The inspection target is unsupported.",
                )
            else:
                _issue(
                    diagnostics,
                    "error",
                    "project-root-unreadable",
                    "The project root cannot be opened safely.",
                )
            return None

        for descriptor in reversed(descriptors):
            if any(
                _relative_status(descriptor, marker) != "missing" for marker in _PROJECT_MARKERS
            ):
                chosen = descriptor
                return chosen
        _issue(diagnostics, "error", "project-not-found", "No enclosing Lean or Autoform project was found.")
        return None
    finally:
        for descriptor in descriptors:
            if descriptor != chosen:
                os.close(descriptor)


def _secure_inspection_available(diagnostics: list[ProjectDiagnostic]) -> bool:
    if (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
    ):
        return True
    _issue(
        diagnostics,
        "error",
        "secure-file-inspection-unavailable",
        "This platform cannot safely inspect project files without following links.",
    )
    return False


def _open_directory(name: str, parent_descriptor: int | None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    if parent_descriptor is None:
        return os.open(name, flags)
    return os.open(name, flags, dir_fd=parent_descriptor)


def _entry_status(parent_descriptor: int, name: str) -> str:
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "other"


def _inspect_lake(
    root_descriptor: int, diagnostics: list[ProjectDiagnostic]
) -> tuple[LakeProject | None, MathlibProject | None]:
    toml_status = _relative_status(root_descriptor, "lakefile.toml")
    lean_status = _relative_status(root_descriptor, "lakefile.lean")
    if toml_status != "missing" and lean_status != "missing":
        _issue(
            diagnostics,
            "error",
            "conflicting-lake-configs",
            "Both lakefile.toml and lakefile.lean exist.",
        )
        return None, None
    if toml_status != "missing":
        content = _read_file(root_descriptor, "lakefile.toml", "lake-config", diagnostics)
        if content is None:
            return None, None
        try:
            text = content.decode("utf-8")
            if _toml_nesting_exceeds(text, _MAX_STRUCTURAL_DEPTH):
                raise ValueError("TOML nesting limit exceeded")
            payload = tomllib.loads(text)
            if _semantic_nesting_exceeds(payload, _MAX_STRUCTURAL_DEPTH):
                raise ValueError("TOML nesting limit exceeded")
        except (UnicodeError, ValueError, tomllib.TOMLDecodeError, RecursionError, MemoryError):
            _issue(diagnostics, "error", "invalid-lake-toml", "lakefile.toml is not valid UTF-8 TOML.", "lakefile.toml")
            return None, None
        lake = _parse_lake_toml(payload, content, diagnostics)
        return lake, _parse_mathlib(payload, diagnostics) if lake is not None else None
    if lean_status != "missing":
        content = _read_file(root_descriptor, "lakefile.lean", "lake-config", diagnostics)
        if content is None:
            return None, None
        _issue(
            diagnostics,
            "warning",
            "lakefile-lean-not-evaluated",
            "lakefile.lean was detected but is not executed by offline inspection.",
            "lakefile.lean",
        )
        return LakeProject(
            format="lean",
            path="lakefile.lean",
            sha256=hashlib.sha256(content).hexdigest(),
            name=None,
            version=None,
            default_targets=(),
            package_src_dir=None,
            targets=(),
        ), None
    _issue(diagnostics, "error", "missing-lake-config", "The project has no Lake source configuration.")
    return None, None


def _parse_lake_toml(
    payload: dict[str, Any], content: bytes, diagnostics: list[ProjectDiagnostic]
) -> LakeProject | None:
    try:
        name = _required_string(payload.get("name"), "name")
        version = _lake_version(payload.get("version"))
        default_targets = _string_list(payload.get("defaultTargets", []), "defaultTargets")
        package_src_dir = _portable_path(payload.get("srcDir"), "srcDir")
        targets: list[LakeTarget] = []
        canonical_target_names: list[str] = []
        for kind in ("lean_lib", "lean_exe"):
            entries = payload.get(kind, [])
            if not isinstance(entries, list):
                raise _InvalidLakeField(kind)
            for entry in entries:
                if not isinstance(entry, dict):
                    raise _InvalidLakeField(kind)
                target_name = _required_string(entry.get("name"), f"{kind}.name")
                canonical_name = _canonical_target_name(target_name)
                if kind == "lean_lib" and "root" in entry:
                    raise _InvalidLakeField("lean_lib.root")
                if kind == "lean_exe" and "roots" in entry:
                    raise _InvalidLakeField("lean_exe.roots")
                if kind == "lean_lib":
                    roots = (
                        _module_list(entry["roots"], "lean_lib.roots")
                        if "roots" in entry
                        else (canonical_name,)
                    )
                else:
                    roots = ()
                targets.append(
                    LakeTarget(
                        kind=kind,
                        name=target_name,
                        root=(
                            (
                                _module(entry["root"], "lean_exe.root")
                                if "root" in entry
                                else canonical_name
                            )
                            if kind == "lean_exe"
                            else None
                        ),
                        roots=roots,
                        src_dir=_portable_path(entry.get("srcDir"), f"{kind}.srcDir"),
                    )
                )
                canonical_target_names.append(canonical_name)
        if len(set(canonical_target_names)) != len(canonical_target_names):
            raise _InvalidLakeField("duplicate target name")
        exe_roots = [target.root for target in targets if target.kind == "lean_exe" and target.root]
        if len(set(exe_roots)) != len(exe_roots):
            raise _InvalidLakeField("duplicate executable root")
        _validate_mathlib_requirements(payload)
    except _NonportableLakePath:
        _issue(
            diagnostics,
            "error",
            "nonportable-lake-path",
            "lakefile.toml contains an absolute or parent-relative path.",
            "lakefile.toml",
        )
        return None
    except _DuplicateMathlibRequirement:
        _issue(
            diagnostics,
            "error",
            "duplicate-mathlib-requirement",
            "lakefile.toml contains multiple direct Mathlib requirements.",
            "lakefile.toml",
        )
        return None
    except _InvalidLakeField:
        _issue(
            diagnostics,
            "error",
            "invalid-lake-field",
            "lakefile.toml contains an invalid field used by Autoform.",
            "lakefile.toml",
        )
        return None
    return LakeProject(
        format="toml",
        path="lakefile.toml",
        sha256=hashlib.sha256(content).hexdigest(),
        name=name,
        version=version,
        default_targets=default_targets,
        package_src_dir=package_src_dir,
        targets=tuple(targets),
    )


def _inspect_toolchain(root_descriptor: int, diagnostics: list[ProjectDiagnostic]) -> LeanProject | None:
    if _relative_status(root_descriptor, "lean-toolchain") == "missing":
        _issue(diagnostics, "error", "missing-lean-toolchain", "The project has no lean-toolchain file.")
        return None
    content = _read_file(root_descriptor, "lean-toolchain", "lean-toolchain", diagnostics)
    if content is None:
        return None
    try:
        text = content.decode("utf-8").strip()
    except UnicodeError:
        text = ""
    if not text or "\n" in text or "\r" in text:
        _issue(diagnostics, "error", "invalid-lean-toolchain", "lean-toolchain must contain one UTF-8 value.", "lean-toolchain")
        return None
    match = _TOOLCHAIN.fullmatch(text)
    if match is None:
        _issue(
            diagnostics,
            "warning",
            "unrecognized-lean-toolchain",
            "The Lean toolchain is outside Autoform's recognized stable form.",
            "lean-toolchain",
        )
    return LeanProject(
        path="lean-toolchain",
        sha256=hashlib.sha256(content).hexdigest(),
        toolchain=text,
        version=match.group("version") if match is not None else None,
    )


def _open_parent_descriptor(root_descriptor: int, relative: str) -> tuple[int, str]:
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError(errno.EINVAL, "invalid relative path")
    current = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            next_descriptor = _open_directory(part, current)
            os.close(current)
            current = next_descriptor
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _relative_status(root_descriptor: int, relative: str) -> str:
    if relative == ".":
        try:
            metadata = os.fstat(root_descriptor)
        except OSError:
            return "unsafe"
        return "directory" if stat.S_ISDIR(metadata.st_mode) else "unsafe"
    try:
        parent, name = _open_parent_descriptor(root_descriptor, relative)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unsafe"
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unsafe"
    finally:
        os.close(parent)
    if stat.S_ISLNK(metadata.st_mode):
        return "unsafe"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "unsafe"


def _inspect_git(root_descriptor: int, diagnostics: list[ProjectDiagnostic]) -> str | None:
    status = _relative_status(root_descriptor, ".git")
    if status == "unsafe":
        _issue(
            diagnostics,
            "error",
            "git-path-is-symlink",
            "The project's .git metadata path cannot be inspected safely.",
            ".git",
        )
        return None
    return ".git" if status in {"file", "directory"} else None


def _inspect_autoform(root_descriptor: int, diagnostics: list[ProjectDiagnostic]) -> AutoformProject:
    paths = {
        "mkdocs_path": ("mkdocs.yml", "file"),
        "verification_workflow_path": (".github/workflows/autoform-verify.yml", "file"),
        "pages_workflow_path": (".github/workflows/blueprint-pages.yml", "file"),
    }
    values: dict[str, str | None] = {"blueprint_path": None}
    for field, (relative, expected) in paths.items():
        status = _relative_status(root_descriptor, relative)
        if status == "unsafe":
            _issue(
                diagnostics,
                "error",
                "scaffold-path-is-symlink",
                "An Autoform scaffold path cannot be inspected safely.",
                relative,
            )
            values[field] = None
        elif status == "missing":
            values[field] = None
        elif status != expected:
            _issue(
                diagnostics,
                "error",
                "scaffold-path-unexpected-type",
                "An Autoform scaffold path is not the expected file or directory.",
                relative,
            )
            values[field] = None
        else:
            values[field] = relative
    manifest_path: str | None = None
    manifest_sha256: str | None = None
    blueprint_paths: tuple[str, ...] = ()
    manifest_status = _relative_status(root_descriptor, WORKSPACE_FILE)
    if manifest_status == "unsafe":
        _issue(
            diagnostics,
            "error",
            "autoform-manifest-is-symlink",
            "The Autoform workspace manifest cannot be inspected safely.",
            WORKSPACE_FILE,
        )
    elif manifest_status not in {"missing", "file"}:
        _issue(
            diagnostics,
            "error",
            "autoform-manifest-unexpected-type",
            "The Autoform workspace manifest is not a regular file.",
            WORKSPACE_FILE,
        )
    elif manifest_status == "file":
        # A root workspace manifest owns blueprint selection. On a
        # case-insensitive filesystem, probing the legacy lowercase path can
        # otherwise alias a registered location such as ``Blueprint``.
        values["blueprint_path"] = None
        content = _read_file(root_descriptor, WORKSPACE_FILE, "autoform-manifest", diagnostics)
        if content is not None:
            manifest_sha256 = hashlib.sha256(content).hexdigest()
            try:
                manifest = parse_workspace(content.decode("utf-8"))
            except (UnicodeDecodeError, WorkspaceError) as error:
                detail = (
                    "; ".join(error.issues)
                    if isinstance(error, WorkspaceError)
                    else f"{WORKSPACE_FILE} is not valid UTF-8 TOML"
                )
                _issue(
                    diagnostics,
                    "error",
                    "autoform-manifest-invalid",
                    detail,
                    WORKSPACE_FILE,
                )
            else:
                manifest_path = WORKSPACE_FILE
                locations = {location.id: location for location in manifest.locations}
                for location in manifest.locations:
                    status = _relative_status(root_descriptor, location.path)
                    if status == "unsafe":
                        _issue(
                            diagnostics,
                            "error",
                            "autoform-location-is-symlink",
                            "A declared Autoform location cannot be inspected safely.",
                            location.path,
                        )
                    elif status == "missing":
                        _issue(
                            diagnostics,
                            "warning",
                            "autoform-location-missing",
                            "A declared Autoform location does not exist.",
                            location.path,
                        )
                    elif status != "directory":
                        _issue(
                            diagnostics,
                            "error",
                            "autoform-location-unexpected-type",
                            "A declared Autoform location is not a directory.",
                            location.path,
                        )
                resolved: list[str] = []
                for project in manifest.projects:
                    location = locations[project.blueprint_location]
                    relative = PurePosixPath(location.path, project.blueprint_path).as_posix()
                    resolved.append(relative)
                    status = _relative_status(root_descriptor, relative)
                    if status == "unsafe":
                        _issue(
                            diagnostics,
                            "error",
                            "autoform-blueprint-is-symlink",
                            "A registered blueprint cannot be inspected safely.",
                            relative,
                        )
                    elif status == "missing":
                        _issue(
                            diagnostics,
                            "error",
                            "autoform-blueprint-missing",
                            "A registered blueprint directory is missing.",
                            relative,
                        )
                    elif status != "directory":
                        _issue(
                            diagnostics,
                            "error",
                            "autoform-blueprint-unexpected-type",
                            "A registered blueprint path is not a directory.",
                            relative,
                        )
                blueprint_paths = tuple(sorted(resolved))

    if manifest_status == "missing":
        blueprint_status = _relative_status(root_descriptor, "blueprint")
        if blueprint_status == "unsafe":
            _issue(
                diagnostics,
                "error",
                "scaffold-path-is-symlink",
                "An Autoform scaffold path cannot be inspected safely.",
                "blueprint",
            )
        elif blueprint_status == "directory":
            values["blueprint_path"] = "blueprint"
        elif blueprint_status != "missing":
            _issue(
                diagnostics,
                "error",
                "scaffold-path-unexpected-type",
                "An Autoform scaffold path is not the expected file or directory.",
                "blueprint",
            )

    workflow_count = sum(
        values[field] is not None
        for field in ("verification_workflow_path", "pages_workflow_path")
    )
    if values["blueprint_path"] is not None and values["mkdocs_path"] is None:
        _issue(diagnostics, "warning", "autoform-mkdocs-missing", "The blueprint has no mkdocs.yml.")
    if workflow_count == 1:
        _issue(diagnostics, "warning", "autoform-workflows-partial", "Only one standard Autoform workflow exists.")
    return AutoformProject(
        detected=values["blueprint_path"] is not None or manifest_path is not None,
        blueprint_path=values["blueprint_path"],
        blueprint_paths=blueprint_paths,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        mkdocs_path=values["mkdocs_path"],
        verification_workflow_path=values["verification_workflow_path"],
        pages_workflow_path=values["pages_workflow_path"],
    )


def _compatibility(
    catalog: ReleaseCatalog,
    lean: LeanProject | None,
    mathlib: MathlibProject | None,
    diagnostics: list[ProjectDiagnostic],
) -> ProjectCompatibility:
    matched = None
    if lean is not None and mathlib is not None:
        matched = next(
            (
                release
                for release in catalog.releases
                if release.lean.toolchain == lean.toolchain
                and release.mathlib.git == mathlib.git
                and release.mathlib.revision == mathlib.revision
            ),
            None,
        )
    if matched is not None:
        status = "supported"
        release_id = matched.id
    elif lean is not None and mathlib is not None:
        status = "unlisted"
        release_id = None
        _issue(
            diagnostics,
            "warning",
            "release-unlisted",
            "The configured Lean and Mathlib revisions are not in the bundled release catalog.",
        )
    else:
        status = "indeterminate"
        release_id = None
        _issue(
            diagnostics,
            "warning",
            "release-indeterminate",
            "Offline inspection cannot determine a Lean and Mathlib release pair.",
        )
    return ProjectCompatibility(catalog.schema, status, release_id, catalog.recommended.id)


def _optional_digest(
    root_descriptor: int, relative: str, diagnostics: list[ProjectDiagnostic]
) -> tuple[str | None, str | None]:
    if _relative_status(root_descriptor, relative) == "missing":
        return None, None
    content = _read_file(
        root_descriptor, relative, "lake-manifest", diagnostics, severity="warning"
    )
    if content is None:
        return None, None
    try:
        text = content.decode("utf-8")
        if _json_nesting_exceeds(text, _MAX_STRUCTURAL_DEPTH):
            raise ValueError("JSON nesting limit exceeded")
        json.loads(text)
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        _issue(diagnostics, "warning", "invalid-lake-manifest", "lake-manifest.json is not valid UTF-8 JSON.", relative)
        return relative, hashlib.sha256(content).hexdigest()
    return relative, hashlib.sha256(content).hexdigest()


def _read_file(
    root_descriptor: int,
    relative: str,
    kind: str,
    diagnostics: list[ProjectDiagnostic],
    *,
    severity: str = "error",
) -> bytes | None:
    try:
        parent, name = _open_parent_descriptor(root_descriptor, relative)
    except OSError:
        _issue(
            diagnostics,
            severity,
            f"{kind}-is-symlink",
            "A decision-bearing project path cannot be traversed safely.",
            relative,
        )
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as error:
        code = (
            f"{kind}-is-symlink"
            if error.errno in {errno.ELOOP, errno.ENOTDIR}
            else f"{kind}-unreadable"
        )
        message = (
            "A decision-bearing project file cannot be opened without following links."
            if code.endswith("-is-symlink")
            else "A project configuration file cannot be read."
        )
        _issue(diagnostics, severity, code, message, relative)
        os.close(parent)
        return None
    os.close(parent)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _issue(
                diagnostics,
                severity,
                f"{kind}-not-regular",
                "A decision-bearing project path is not a regular file.",
                relative,
            )
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(content) > _MAX_CONFIG_BYTES:
            _issue(
                diagnostics,
                severity,
                f"{kind}-too-large",
                "A project configuration file exceeds the inspection limit.",
                relative,
            )
            return None
        return content
    except OSError:
        _issue(
            diagnostics,
            severity,
            f"{kind}-unreadable",
            "A project configuration file cannot be read.",
            relative,
        )
        return None
    finally:
        os.close(descriptor)


def _validate_mathlib_requirements(payload: dict[str, Any]) -> None:
    requirements = payload.get("require", [])
    if not isinstance(requirements, list):
        raise _InvalidLakeField("require")
    canonical_names: list[str] = []
    for entry in requirements:
        if not isinstance(entry, dict):
            raise _InvalidLakeField("require")
        canonical_names.append(
            _canonical_target_name(_required_string(entry.get("name"), "require.name"))
        )
    matches = [
        entry
        for entry, canonical_name in zip(requirements, canonical_names, strict=True)
        if canonical_name == "mathlib"
    ]
    if len(matches) > 1:
        raise _DuplicateMathlibRequirement("duplicate mathlib")
    for entry in matches:
        if "scope" in entry:
            _required_string(entry["scope"], "mathlib.scope")
        if "rev" in entry:
            _required_string(entry["rev"], "mathlib.rev")
        if "path" in entry:
            _portable_path(entry["path"], "mathlib.path")
        elif "git" in entry:
            _git_url(entry["git"], "mathlib.git")
            if "subDir" in entry:
                _portable_path(entry["subDir"], "mathlib.subDir")
        elif "source" in entry:
            _validate_dependency_source(entry["source"])


def _parse_mathlib(
    payload: dict[str, Any], diagnostics: list[ProjectDiagnostic]
) -> MathlibProject | None:
    requirements = payload.get("require", [])
    if not isinstance(requirements, list):
        return None
    matches = [
        entry
        for entry in requirements
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and _canonical_target_name(entry["name"]) == "mathlib"
    ]
    if len(matches) != 1:
        return None
    entry = matches[0]
    if (
        "path" in entry
        or "source" in entry
        or entry.get("subDir") not in {None, "", "."}
    ):
        return None
    revision = entry.get("rev")
    if not isinstance(revision, str):
        return None
    git = _mathlib_git_source(entry)
    if git is None:
        return None
    try:
        parsed = urlsplit(git)
        port = parsed.port
    except ValueError:
        _issue(
            diagnostics,
            "error",
            "invalid-mathlib-url",
            "The direct Mathlib Git URL is invalid.",
            "lakefile.toml",
        )
        return None
    if parsed.username is not None or parsed.password is not None:
        _issue(
            diagnostics,
            "error",
            "credentialed-mathlib-url",
            "The direct Mathlib Git URL must not contain credentials.",
            "lakefile.toml",
        )
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.lower() != parsed.hostname.lower()
    ):
        _issue(
            diagnostics,
            "error",
            "invalid-mathlib-url",
            "The direct Mathlib Git URL must be credential-free HTTPS.",
            "lakefile.toml",
        )
        return None
    return MathlibProject(git=git, revision=revision, source="lakefile.toml")


def _validate_dependency_source(value: Any) -> None:
    if not isinstance(value, dict):
        raise _InvalidLakeField("mathlib.source")
    source_type = _required_string(value.get("type"), "mathlib.source.type")
    if source_type == "path":
        if set(value) != {"type", "dir"}:
            raise _InvalidLakeField("mathlib.source")
        _portable_path(value["dir"], "mathlib.source.dir")
    elif source_type == "git":
        if not set(value) <= {"type", "url", "rev", "subDir"} or "url" not in value:
            raise _InvalidLakeField("mathlib.source")
        _required_string(value["url"], "mathlib.source.url")
        if "rev" in value:
            _required_string(value["rev"], "mathlib.source.rev")
        if "subDir" in value:
            _portable_path(value["subDir"], "mathlib.source.subDir")
    else:
        raise _InvalidLakeField("mathlib.source.type")


def _mathlib_git_source(entry: dict[str, Any]) -> str | None:
    git = entry.get("git")
    if isinstance(git, dict):
        git = git.get("url")
    if isinstance(git, str):
        return git
    # `lake new <pkg> math` emits a scope-only Reservoir requirement with no
    # `git` field; Reservoir serves that scope from GitHub.
    scope = entry.get("scope")
    if isinstance(scope, str) and _RESERVOIR_SCOPE.fullmatch(scope):
        return f"https://github.com/{scope}/mathlib4.git"
    return None


def _git_url(value: Any, field: str) -> str:
    if isinstance(value, dict):
        if set(value) != {"url"}:
            raise _InvalidLakeField(field)
        value = value["url"]
    return _required_string(value, field)


def _required_string(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _InvalidLakeField(field)
    return value


def _lake_version(value: Any) -> str | None:
    if value is None:
        return None
    text = _required_string(value, "version")
    if _LAKE_VERSION.fullmatch(text) is None:
        raise _InvalidLakeField("version")
    return text


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _InvalidLakeField(field)
    return tuple(_required_string(item, field) for item in value)


def _module_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _InvalidLakeField(field)
    return tuple(_module(item, field) for item in value)


def _module(value: Any, field: str) -> str:
    canonical = _canonical_module_name(_required_string(value, field))
    if canonical is None:
        raise _InvalidLakeField(field)
    return canonical


def _canonical_module_name(value: str) -> str | None:
    """Render a Lake name the way Lean's `String.toName` reads it.

    Numeric components are decoded as naturals, so `01` and `1` name the same
    module, and letter-like Unicode components such as `Ω` are accepted.
    Returns None when Lean would reject the string outright.
    """
    components = _split_lean_name(value)
    if components is None:
        return None
    root_kind, root_text = components[0]
    escape = not (
        root_kind == "str"
        and (root_text.startswith("#") or root_text.startswith("?"))
    )
    return ".".join(
        _render_lean_component(kind, text, escape=escape)
        for kind, text in components
    )


def _canonical_target_name(value: str) -> str:
    """Apply Lake's `stringToLegalOrSimpleName` fallback for target names."""
    canonical = _canonical_module_name(value)
    if canonical is not None:
        return canonical
    escape = not (value.startswith("#") or value.startswith("?"))
    return _render_lean_component("str", value, escape=escape)


def _split_lean_name(value: str) -> list[tuple[str, str]] | None:
    components: list[tuple[str, str]] = []
    index = 0
    while True:
        if index >= len(value):
            return None
        character = value[index]
        if character == _LEAN_ID_BEGIN_ESCAPE:
            end = value.find(_LEAN_ID_END_ESCAPE, index + 1)
            if end < 0:
                return None
            components.append(("str", value[index + 1 : end]))
            index = end + 1
        elif _lean_is_id_first(character):
            start = index
            index += 1
            while index < len(value) and _lean_is_id_rest(value[index]):
                index += 1
            components.append(("str", value[start:index]))
        elif _lean_is_digit(character):
            start = index
            while index < len(value) and _lean_is_digit(value[index]):
                index += 1
            digits = value[start:index]
            components.append(("num", digits.lstrip("0") or "0"))
        else:
            return None
        if index == len(value):
            return components
        if value[index] != ".":
            return None
        index += 1


def _render_lean_component(kind: str, text: str, *, escape: bool = True) -> str:
    if kind == "num":
        return text
    if not escape:
        return text
    # Lean's `Name.escapePart` cannot round-trip a closing guillemet, so it
    # leaves the complete simple component unescaped in that case.
    if _LEAN_ID_END_ESCAPE in text:
        return text
    if text and _lean_is_id_first(text[0]) and all(_lean_is_id_rest(c) for c in text[1:]):
        return text
    return f"{_LEAN_ID_BEGIN_ESCAPE}{text}{_LEAN_ID_END_ESCAPE}"


def _lean_is_digit(character: str) -> bool:
    return "0" <= character <= "9"


def _lean_is_alpha(character: str) -> bool:
    return "a" <= character <= "z" or "A" <= character <= "Z"


def _lean_is_id_first(character: str) -> bool:
    return _lean_is_alpha(character) or character == "_" or _lean_is_letter_like(character)


def _lean_is_id_rest(character: str) -> bool:
    return (
        _lean_is_alpha(character)
        or _lean_is_digit(character)
        or character in "_'!?"
        or _lean_is_letter_like(character)
        or _lean_is_subscript_alnum(character)
    )


def _lean_is_letter_like(character: str) -> bool:
    code = ord(character)
    return (
        (0x3B1 <= code <= 0x3C9 and code != 0x3BB)
        or (0x391 <= code <= 0x3A9 and code not in {0x3A0, 0x3A3})
        or 0x3CA <= code <= 0x3FB
        or 0x1F00 <= code <= 0x1FFE
        or 0x2100 <= code <= 0x214F
        or 0x1D49C <= code <= 0x1D59F
        or (0xC0 <= code <= 0xFF and code not in {0xD7, 0xF7})
        or 0x100 <= code <= 0x17F
    )


def _lean_is_subscript_alnum(character: str) -> bool:
    code = ord(character)
    return (
        0x2080 <= code <= 0x2089
        or 0x2090 <= code <= 0x209C
        or 0x1D62 <= code <= 0x1D6A
        or code == 0x2C7C
    )


def _portable_path(value: Any, field: str) -> str | None:
    if value is None:
        return None
    text = _required_string(value, field)
    posix = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or ".." in posix.parts
        or "." in posix.parts
        or ".." in windows.parts
        or "." in windows.parts
    ):
        raise _NonportableLakePath(field)
    return posix.as_posix()


def _toml_nesting_exceeds(text: str, limit: int) -> bool:
    """Bound both bracket nesting and dotted-key nesting before parsing.

    A table header or dotted key names one table per component, so a flat
    document such as `[a.b.c...]` nests as deeply as `[[[...]]]` would while
    using only one bracket pair.
    """
    depth = 0
    key_components = 0
    in_key = True
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif quote[0] == '"' and character == "\\":
                escaped = True
            elif len(quote) == 3 and character == quote[0]:
                run_end = index
                while run_end < len(text) and text[run_end] == quote[0]:
                    run_end += 1
                if run_end - index >= 3:
                    index = run_end - 1
                    quote = None
            elif len(quote) == 1 and character == quote:
                quote = None
        elif character == "#":
            newline = text.find("\n", index)
            # Stop before the newline so it still resets the key context.
            index = len(text) if newline < 0 else newline
            continue
        elif text.startswith("'''", index) or text.startswith('\"\"\"', index):
            quote = text[index : index + 3]
            index += 2
        elif character in "'\"":
            quote = character
        elif character in "[{":
            depth += 1
            if depth > limit:
                return True
            if character == "{":
                in_key, key_components = True, 0
        elif character in "]}":
            depth = max(0, depth - 1)
        elif character in "\n,":
            in_key, key_components = True, 0
        elif character == "=":
            in_key = False
        elif character == "." and in_key:
            key_components += 1
            if depth + key_components > limit:
                return True
        index += 1
    return False


def _semantic_nesting_exceeds(value: Any, limit: int) -> bool:
    """Check parsed container depth iteratively as a second depth boundary."""

    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            if depth > limit:
                return True
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            if depth > limit:
                return True
            stack.extend((child, depth + 1) for child in current)
    return False


def _json_nesting_exceeds(text: str, limit: int) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > limit:
                return True
        elif character in "]}":
            depth = max(0, depth - 1)
    return False


def _issue(
    diagnostics: list[ProjectDiagnostic],
    severity: str,
    code: str,
    message: str,
    path: str | None = None,
) -> None:
    diagnostics.append(ProjectDiagnostic(severity, code, message, path))


def _ordered(diagnostics: list[ProjectDiagnostic]) -> tuple[ProjectDiagnostic, ...]:
    unique = set(diagnostics)
    return tuple(
        sorted(
            unique,
            key=lambda diagnostic: (
                _SEVERITY_ORDER[diagnostic.severity],
                diagnostic.code,
                diagnostic.path or "",
                diagnostic.message,
            ),
        )
    )
