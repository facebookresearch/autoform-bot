"""Pure schema and portability rules for Autoform workspace manifests."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


WORKSPACE_FILE = ".autoform.toml"
WORKSPACE_SCHEMA = "autoform-workspace/v1"
WORKSPACE_INSPECTION_SCHEMA = "autoform-workspace-inspection/v1"
WORKSPACE_INIT_SCHEMA = "autoform-workspace-init/v1"
WORKSPACE_CHECK_SCHEMA = "autoform-workspace-check/v1"
WORKSPACE_ERROR_SCHEMA = "autoform-workspace-error/v1"
BLUEPRINT_CHANGE_SCHEMA = "autoform-blueprint-change/v2"
BLUEPRINT_LIST_SCHEMA = "autoform-blueprint-list/v1"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024

_IDENTIFIER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_RESERVED_REPOSITORY_ROOTS = frozenset(
    {WORKSPACE_FILE.casefold(), ".git", ".hg", ".jj", ".svn"}
)


class WorkspaceError(ValueError):
    """A workspace manifest or requested workspace operation is invalid."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class WorkspaceLocation:
    """One repository-relative location advertising Autoform capabilities."""

    id: str
    path: str
    provides: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "path": self.path, "provides": list(self.provides)}


@dataclass(frozen=True, slots=True)
class WorkspaceProject:
    """One managed project and its blueprint binding."""

    id: str
    title: str | None
    blueprint_location: str
    blueprint_path: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "blueprint_location": self.blueprint_location,
            "blueprint_path": self.blueprint_path,
            "id": self.id,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceManifest:
    """Validated, path-independent contents of ``.autoform.toml``."""

    schema: str
    locations: tuple[WorkspaceLocation, ...]
    projects: tuple[WorkspaceProject, ...]

    def location(self, location_id: str) -> WorkspaceLocation:
        for location in self.locations:
            if location.id == location_id:
                return location
        raise WorkspaceError([f"unknown workspace location: {location_id}"])

    def project(self, project_id: str) -> WorkspaceProject:
        for project in self.projects:
            if project.id == project_id:
                return project
        choices = ", ".join(project.id for project in self.projects) or "none"
        raise WorkspaceError(
            [f"unknown Autoform project {project_id!r}; registered projects: {choices}"]
        )

    def blueprint_relative(self, project: WorkspaceProject) -> PurePosixPath:
        location = self.location(project.blueprint_location)
        return PurePosixPath(location.path, project.blueprint_path)


def parse_workspace(text: str) -> WorkspaceManifest:
    """Parse and validate a workspace manifest without touching the filesystem."""

    try:
        payload = tomllib.loads(text)
    except (ValueError, RecursionError, MemoryError):
        raise WorkspaceError([f"{WORKSPACE_FILE} is not valid TOML"]) from None
    if not isinstance(payload, dict):
        raise WorkspaceError([f"{WORKSPACE_FILE} must contain a TOML table"])

    issues: list[str] = []
    _reject_unknown_keys(
        payload,
        allowed=frozenset({"schema", "locations", "projects"}),
        label=WORKSPACE_FILE,
        issues=issues,
    )
    schema = payload.get("schema")
    if schema != WORKSPACE_SCHEMA:
        issues.append(f"schema must be exactly {WORKSPACE_SCHEMA!r}")

    raw_locations = payload.get("locations")
    if not isinstance(raw_locations, dict) or not raw_locations:
        issues.append("locations must be a non-empty table")
        raw_locations = {}
    locations: list[WorkspaceLocation] = []
    folded_location_ids: set[str] = set()
    folded_location_paths: dict[tuple[str, ...], str] = {}
    for location_id, raw in sorted(raw_locations.items()):
        label = f"locations.{location_id}"
        if not valid_identifier(location_id):
            issues.append(f"{label}: location id is not portable")
            continue
        folded = portable_name_key(location_id)
        if folded in folded_location_ids:
            issues.append(f"{label}: location id differs only by case from another location")
            continue
        folded_location_ids.add(folded)
        if not isinstance(raw, dict):
            issues.append(f"{label} must be a table")
            continue
        _reject_unknown_keys(
            raw,
            allowed=frozenset({"path", "provides"}),
            label=label,
            issues=issues,
        )
        path = raw.get("path")
        provides = raw.get("provides")
        if not isinstance(path, str) or not valid_relative_path(path, allow_dot=True):
            issues.append(f"{label}.path must be a portable repository-relative path")
            continue
        if uses_reserved_repository_root(path):
            issues.append(f"{label}.path uses a reserved repository path")
            continue
        if (
            not isinstance(provides, list)
            or not provides
            or any(not isinstance(item, str) or not valid_identifier(item) for item in provides)
        ):
            issues.append(f"{label}.provides must be a non-empty array of capability names")
            continue
        if len({portable_name_key(item) for item in provides}) != len(provides):
            issues.append(f"{label}.provides contains duplicate capabilities")
            continue
        path_key = portable_path_key(path)
        previous_location = folded_location_paths.get(path_key)
        if previous_location is not None:
            issues.append(
                f"{label}.path resolves to the same portable path as locations.{previous_location}"
            )
            continue
        folded_location_paths[path_key] = location_id
        locations.append(WorkspaceLocation(location_id, path, tuple(provides)))

    locations_by_id = {location.id: location for location in locations}
    raw_projects = payload.get("projects", {})
    if not isinstance(raw_projects, dict):
        issues.append("projects must be a table")
        raw_projects = {}
    projects: list[WorkspaceProject] = []
    folded_project_ids: set[str] = set()
    blueprint_paths: list[tuple[str, tuple[str, ...]]] = []
    for project_id, raw in sorted(raw_projects.items()):
        label = f"projects.{project_id}"
        if not valid_identifier(project_id):
            issues.append(f"{label}: project id is not portable")
            continue
        folded = portable_name_key(project_id)
        if folded in folded_project_ids:
            issues.append(f"{label}: project id differs only by case from another project")
            continue
        folded_project_ids.add(folded)
        if not isinstance(raw, dict):
            issues.append(f"{label} must be a table")
            continue
        _reject_unknown_keys(
            raw,
            allowed=frozenset({"title", "blueprint"}),
            label=label,
            issues=issues,
        )
        title = raw.get("title")
        if title is not None and (not isinstance(title, str) or not title.strip()):
            issues.append(f"{label}.title must be a non-empty string when present")
            continue
        blueprint = raw.get("blueprint")
        if not isinstance(blueprint, dict):
            issues.append(f"{label}.blueprint must be a table")
            continue
        _reject_unknown_keys(
            blueprint,
            allowed=frozenset({"location", "path"}),
            label=f"{label}.blueprint",
            issues=issues,
        )
        location_id = blueprint.get("location")
        path = blueprint.get("path")
        if not isinstance(location_id, str) or location_id not in locations_by_id:
            issues.append(f"{label}.blueprint.location must name a declared location")
            continue
        if not isinstance(path, str) or not valid_blueprint_member(path):
            issues.append(
                f"{label}.blueprint.path must name one portable immediate child directory"
            )
            continue
        location = locations_by_id[location_id]
        if "blueprints" not in location.provides:
            issues.append(
                f"{label}.blueprint.location must provide the 'blueprints' capability"
            )
            continue
        combined = PurePosixPath(location.path, path).as_posix()
        if uses_reserved_repository_root(combined):
            issues.append(f"{label}.blueprint uses a reserved repository path")
            continue
        combined_key = portable_path_key(combined)
        overlap = next(
            (
                (existing_id, existing_key)
                for existing_id, existing_key in blueprint_paths
                if path_keys_overlap(combined_key, existing_key)
            ),
            None,
        )
        if overlap is not None:
            existing_id, existing_key = overlap
            if combined_key == existing_key:
                issues.append(f"{label}.blueprint resolves to the same path as another project")
            else:
                issues.append(
                    f"{label}.blueprint overlaps the managed vault for projects.{existing_id}"
                )
            continue
        blueprint_paths.append((project_id, combined_key))
        projects.append(WorkspaceProject(project_id, title, location_id, path))

    if issues:
        raise WorkspaceError(issues)
    return WorkspaceManifest(WORKSPACE_SCHEMA, tuple(locations), tuple(projects))


def _reject_unknown_keys(
    table: dict[str, object],
    *,
    allowed: frozenset[str],
    label: str,
    issues: list[str],
) -> None:
    for key in sorted(set(table) - allowed):
        issues.append(f"{label}: unknown key {key!r}")


def valid_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(_IDENTIFIER.fullmatch(value))
        and valid_path_component(value)
    )


def valid_relative_path(value: str, *, allow_dot: bool) -> bool:
    if not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if path.is_absolute() or windows.is_absolute() or windows.drive:
        return False
    if any(part in {"", ".."} or not valid_path_component(part) for part in path.parts):
        return False
    if path.as_posix() == ".":
        return allow_dot
    return path.as_posix() == value and all(part != "." for part in path.parts)


def valid_blueprint_member(value: str) -> bool:
    return valid_relative_path(value, allow_dot=False) and len(PurePosixPath(value).parts) == 1


def valid_path_component(value: str) -> bool:
    if not value or value != value.strip() or value.endswith("."):
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    if any(character in _WINDOWS_FORBIDDEN for character in value):
        return False
    stem = value.split(".", 1)[0].upper()
    return stem not in _WINDOWS_RESERVED


def portable_name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def portable_path_key(value: str | PurePosixPath) -> tuple[str, ...]:
    return tuple(portable_name_key(part) for part in PurePosixPath(value).parts if part != ".")


def path_keys_overlap(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    common = min(len(first), len(second))
    return first[:common] == second[:common]


def uses_reserved_repository_root(value: str | PurePosixPath) -> bool:
    parts = PurePosixPath(value).parts
    return bool(parts) and portable_name_key(parts[0]) in _RESERVED_REPOSITORY_ROOTS


__all__ = [
    "BLUEPRINT_CHANGE_SCHEMA",
    "BLUEPRINT_LIST_SCHEMA",
    "MAX_MANIFEST_BYTES",
    "WORKSPACE_CHECK_SCHEMA",
    "WORKSPACE_ERROR_SCHEMA",
    "WORKSPACE_FILE",
    "WORKSPACE_INIT_SCHEMA",
    "WORKSPACE_INSPECTION_SCHEMA",
    "WORKSPACE_SCHEMA",
    "WorkspaceError",
    "WorkspaceLocation",
    "WorkspaceManifest",
    "WorkspaceProject",
    "parse_workspace",
    "uses_reserved_repository_root",
]
