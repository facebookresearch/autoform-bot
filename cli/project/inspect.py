"""Inspect a local Lean project's release pair without running Lake, Lean, or Git.

Compatibility is decided by `lean-toolchain` and the Mathlib entry that
`lake-manifest.json` locks, which is what `lake build` materializes; a
`.lake/package-overrides.json` entry replaces it. `lakefile.toml` is read for
the package name, targets, and whether the lock is used and current.
`lakefile.lean` is never evaluated, so those projects stay indeterminate.
This is deliberately not a complete Lake configuration validator.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import tomli

from . import _snapshot
from ._lake_metadata import (
    LakeProject,
    LakeTarget,
    MathlibLock,
    _JsonInteger,
    _LockedPackages,
    _MATHLIB_NAME,
    _Requirements,
    _canonical_toml_name,
    _decode_package_entry,
    _is_stale,
    _lakefile_problem,
    _manifest_layout,
    _reject_json_constant,
    _validate_manifest_root,
)
from .catalog import ReleaseCatalog, load_release_catalog
from ._snapshot import (
    _DecisionSnapshot,
    _MANIFEST,
    _OVERRIDES,
    _ROOT_MARKERS,
    _path_present,
)

PROJECT_INSPECTION_SCHEMA = "autoform-project-inspection/v1"
_SNAPSHOT_ATTEMPTS = 3
# Rust's ``char::is_whitespace`` set, which ``str::trim`` uses in elan.
# Python additionally treats U+001C..U+001F as whitespace; accepting those
# would disagree with elan because they remain control characters there.
_ELAN_WHITESPACE = frozenset(
    "\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
_AUTOFORM_PATHS: dict[str, Callable[[Path], bool]] = {
    "blueprint": os.path.isdir,
    "mkdocs.yml": os.path.isfile,
    ".github/workflows/autoform-verify.yml": os.path.isfile,
    ".github/workflows/blueprint-pages.yml": os.path.isfile,
}


@dataclass(frozen=True, slots=True)
class ProjectDiagnostic:
    severity: str
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectCompatibility:
    status: str
    release: str | None
    recommended_release: str


@dataclass(frozen=True, slots=True)
class ProjectInspection:
    project_root: str | None
    lake: LakeProject | None
    lean_toolchain: str | None
    mathlib: MathlibLock | None
    autoform_paths: tuple[str, ...]
    compatibility: ProjectCompatibility
    diagnostics: tuple[ProjectDiagnostic, ...]

    @property
    def ok(self) -> bool:
        return not any(diagnostic.severity == "error" for diagnostic in self.diagnostics)

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "ok": self.ok, "schema": PROJECT_INSPECTION_SCHEMA}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def inspect_project(target: str | Path, *, catalog: ReleaseCatalog | None = None) -> ProjectInspection:
    catalog = catalog or load_release_catalog()
    diagnostics: list[ProjectDiagnostic] = []
    try:
        start = Path(target).expanduser().resolve(strict=True)
        if not start.is_dir():
            start = start.parent
        root = _find_project_root(start)
    except (OSError, RuntimeError, ValueError):
        diagnostics.append(ProjectDiagnostic("error", "target-unreadable", "The inspection target cannot be resolved."))
        return _result(catalog, diagnostics)
    if root is None:
        diagnostics.append(
            ProjectDiagnostic("error", "project-not-found", "No enclosing Lean project (lakefile or lean-toolchain).")
        )
        return _result(catalog, diagnostics)

    project_root = "/".join([".."] * (len(start.parts) - len(root.parts))) or "."
    autoform_paths: tuple[str, ...] = ()
    for _attempt in range(_SNAPSHOT_ATTEMPTS):
        candidate = _find_project_root(start)
        if candidate is None:
            continue
        root = candidate
        project_root = "/".join([".."] * (len(start.parts) - len(root.parts))) or "."
        autoform_paths = _inspect_autoform_paths(root)
        snapshot = _snapshot._capture_decision_snapshot(root)
        attempt_diagnostics: list[ProjectDiagnostic] = []
        result = _inspect_snapshot(
            catalog,
            snapshot,
            attempt_diagnostics,
            project_root=project_root,
            autoform_paths=autoform_paths,
        )
        verified = _snapshot._capture_decision_snapshot(root)
        if (
            snapshot.stable
            and snapshot == verified
            and _inspect_autoform_paths(root) == autoform_paths
            and _find_project_root(start) == root
        ):
            return result

    diagnostics.append(
        ProjectDiagnostic(
            "error",
            "project-changed-during-inspection",
            "Project configuration changed while it was being inspected.",
        )
    )
    return _result(catalog, diagnostics, project_root=project_root, autoform_paths=autoform_paths)


def _find_project_root(start: Path) -> Path | None:
    return next(
        (
            directory
            for directory in (start, *start.parents)
            if any(_path_present(directory / marker) for marker in _ROOT_MARKERS)
        ),
        None,
    )


def _inspect_autoform_paths(root: Path) -> tuple[str, ...]:
    # The os.path predicates read a path they cannot stat as absent, as
    # _exists_exactly reads an unlistable directory. Before Python 3.14,
    # Path.is_file and Path.is_dir raise PermissionError for an entry under
    # a listable but unsearchable directory.
    return tuple(
        path for path, kind in _AUTOFORM_PATHS.items() if _exists_exactly(root, path) and kind(root / path)
    )


def _inspect_snapshot(
    catalog: ReleaseCatalog,
    snapshot: _DecisionSnapshot,
    diagnostics: list[ProjectDiagnostic],
    *,
    project_root: str,
    autoform_paths: tuple[str, ...],
) -> ProjectInspection:
    lake, requirements = _inspect_lake(snapshot, diagnostics)
    requirement = requirements.mathlib if requirements is not None else None
    toolchain = _inspect_toolchain(snapshot, diagnostics)
    has_manifest = snapshot.file(_MANIFEST).state != "missing"
    if not has_manifest:
        diagnostics.append(
            ProjectDiagnostic(
                "warning",
                "missing-lake-manifest",
                "There is no lake-manifest.json, so the locked Mathlib is unknown.",
                _MANIFEST,
            )
        )
    manifest_valid, manifest_packages = _locked_mathlib(snapshot, _MANIFEST, diagnostics)
    overrides_valid, override_packages = (True, None)
    # Lake reads workspace overrides only on the manifest-loading path. With no
    # usable root manifest it updates instead and never parses this file.
    if has_manifest and manifest_valid:
        overrides_valid, override_packages = _locked_mathlib(snapshot, _OVERRIDES, diagnostics)
    locked = manifest_packages.mathlib if manifest_packages is not None else None
    override = override_packages.mathlib if override_packages is not None else None
    if manifest_packages is not None and overrides_valid and requirements is not None:
        recorded = manifest_packages.names | (override_packages.names if override_packages is not None else frozenset())
        if (incomplete := _unrecorded_requirements(requirements, recorded)) is not None:
            diagnostics.append(ProjectDiagnostic("error", "lake-manifest-incomplete", incomplete, _MANIFEST))
    mathlib = locked if manifest_valid and overrides_valid else None
    # Lake validates direct requirements against the root manifest before it
    # loads workspace overrides, so an override does not suppress this warning.
    if locked is not None and requirement is not None and _is_stale(requirement, locked):
        diagnostics.append(
            ProjectDiagnostic(
                "warning",
                "lake-manifest-stale",
                "lakefile.toml requests a different Mathlib than the manifest locks; Lake builds the locked one.",
                _MANIFEST,
            )
        )
    if override is not None:  # Lake applies overrides to a manifest's packages
        diagnostics.append(
            ProjectDiagnostic(
                "warning",
                "mathlib-overridden",
                "package-overrides.json selects this Mathlib entry whenever Mathlib is an active dependency.",
                _OVERRIDES,
            )
        )
        mathlib = override
    if lake is not None and lake.config == "lakefile.lean":
        mathlib = None
    elif mathlib is not None and requirements is not None and (unused := _unused_mathlib(requirements)):
        diagnostics.append(ProjectDiagnostic("warning", "mathlib-manifest-unused", unused, mathlib.source))
        mathlib = None
    return _result(
        catalog,
        diagnostics,
        project_root=project_root,
        lake=lake,
        lean_toolchain=toolchain,
        mathlib=mathlib,
        autoform_paths=autoform_paths,
    )


def _result(
    catalog: ReleaseCatalog,
    diagnostics: list[ProjectDiagnostic],
    *,
    project_root: str | None = None,
    lake: LakeProject | None = None,
    lean_toolchain: str | None = None,
    mathlib: MathlibLock | None = None,
    autoform_paths: tuple[str, ...] = (),
) -> ProjectInspection:
    errors = any(diagnostic.severity == "error" for diagnostic in diagnostics)
    release = None
    if errors or lean_toolchain is None or mathlib is None or mathlib.type != "git":
        status = "indeterminate"
        if not errors:
            diagnostics.append(
                ProjectDiagnostic(
                    "warning",
                    "release-indeterminate",
                    "Autoform could not establish a catalog-comparable Lean/Mathlib release identity.",
                )
            )
    else:
        if mathlib.loads_like_a_release:
            release = catalog.match(lean_toolchain, mathlib.url, mathlib.rev)
        status = "supported" if release is not None else "unlisted"
        if release is None:
            diagnostics.append(
                ProjectDiagnostic(
                    "warning",
                    "release-unlisted",
                    "The inspected Lean toolchain, Mathlib Git lock, and loading layout do not identify a "
                    "bundled release.",
                )
            )
    return ProjectInspection(
        project_root=project_root,
        lake=lake,
        lean_toolchain=lean_toolchain,
        mathlib=mathlib,
        autoform_paths=autoform_paths,
        compatibility=ProjectCompatibility(
            status, release.id if release is not None else None, catalog.recommended.id
        ),
        diagnostics=tuple(sorted(diagnostics, key=lambda item: (item.severity, item.code, item.path or ""))),
    )


def _inspect_lake(
    snapshot: _DecisionSnapshot, diagnostics: list[ProjectDiagnostic]
) -> tuple[LakeProject | None, _Requirements | None]:
    if snapshot.file("lakefile.lean").state != "missing":
        if _read_text(snapshot, "lakefile.lean", diagnostics) is None:
            return None, None
        diagnostics.append(
            ProjectDiagnostic(
                "warning",
                "lakefile-lean-not-evaluated",
                "lakefile.lean takes precedence and is not evaluated, so its package, targets, "
                "and Mathlib cannot be confirmed.",
                "lakefile.lean",
            )
        )
        return LakeProject("lakefile.lean", None, None, ()), None
    if snapshot.file("lakefile.toml").state == "missing":
        diagnostics.append(
            ProjectDiagnostic("error", "missing-lake-config", "The project has no lakefile.toml or lakefile.lean.")
        )
        return None, None
    text = _read_text(snapshot, "lakefile.toml", diagnostics)
    if text is None:
        return None, None
    try:
        config = tomli.loads(text)
    except tomli.TOMLDecodeError:
        config = None
    except (RecursionError, ValueError):
        diagnostics.append(
            ProjectDiagnostic(
                "error",
                "invalid-lakefile-toml",
                "Autoform could not safely decode lakefile.toml because it exceeds the parser's limits.",
                "lakefile.toml",
            )
        )
        return None, None
    problem = "it is not valid TOML" if config is None else _lakefile_problem(config)
    if problem is not None:
        diagnostics.append(
            ProjectDiagnostic("error", "invalid-lakefile-toml", f"Lake cannot load lakefile.toml: {problem}.", "lakefile.toml")
        )
        return None, None
    targets = tuple(
        LakeTarget(kind, entry["name"]) for kind in ("lean_lib", "lean_exe") for entry in config.get(kind, [])
    )
    # Lake keeps the last of several requirements with the same name.
    requirement = next(
        (
            entry
            for entry in reversed(config.get("require", []))
            if _canonical_toml_name(entry["name"]) == _MATHLIB_NAME
        ),
        None,
    )
    lake = LakeProject("lakefile.toml", config["name"], config.get("version"), targets)
    # Lake's resolver reuses an already loaded package of the required name
    # before it consults the manifest, and the root is loaded first, so a
    # requirement of the root's own name never reaches the manifest. A root
    # named mathlib thus satisfies every Mathlib requirement, direct or
    # transitive, and the manifest's Mathlib entry is never materialized.
    # Only requirements Lake looks up could pull Mathlib in transitively. We
    # do not inspect their current configurations, so that possibility alone
    # never proves that the root manifest's Mathlib entry is active.
    root_name = _canonical_toml_name(config["name"])
    declared = bool(config.get("require"))
    lookups = tuple(
        (name, entry["name"])
        for entry in config.get("require", [])
        if (name := _canonical_toml_name(entry["name"])) != root_name
    )
    if root_name == _MATHLIB_NAME:
        return lake, _Requirements(None, transitive=False, declared=declared, lookups=lookups)
    return lake, _Requirements(requirement, transitive=bool(lookups), declared=declared, lookups=lookups)


def _unrecorded_requirements(requirements: _Requirements, recorded: frozenset) -> str | None:
    """Why Lake refuses to resolve lakefile.toml's requirements from the manifest, if it does.

    ``Workspace.materializeDeps`` stops with "missing manifest" when the
    manifest and overrides record no packages but lakefile.toml requires
    some, and with "dependency ... not in manifest" when a requirement Lake
    looks up there is absent; both ask for `lake update`.
    """

    if requirements.declared and not recorded:
        return (
            "lake-manifest.json records no packages, but lakefile.toml has requirements; Lake asks for `lake update`."
        )
    missing = list(dict.fromkeys(spelling for name, spelling in requirements.lookups if name not in recorded))
    if missing:
        return (
            f"lakefile.toml requires {', '.join(map(repr, missing))}, which neither lake-manifest.json nor "
            "package-overrides.json records; Lake asks for `lake update`."
        )
    return None


def _unused_mathlib(requirements: _Requirements) -> str | None:
    """Why Lake may not materialize the Mathlib the manifest or overrides select, if so.

    A direct root requirement proves Mathlib is active. Other requirements may
    pull it in, but an ``inherited`` manifest entry is only prior resolver
    state and can be stale after a dependency drops Mathlib. Autoform does not
    inspect dependency configurations, and an override selects the source of
    an active package but does not itself make that package active.
    """

    if requirements.mathlib is not None:
        return None
    if not requirements.transitive:
        return (
            "Lake resolves no Mathlib from the manifest: lakefile.toml requires no package other than "
            "its own, or its own package is named mathlib."
        )
    return (
        "lakefile.toml does not directly require Mathlib; a manifest or override entry, including an inherited "
        "entry, does not prove that a dependency still requires it, and Autoform does not read dependency lakefiles."
    )


def _trim_elan_whitespace(value: str) -> str:
    """Mirror Rust's Unicode whitespace trim without Python's extra C0 separators."""

    start = 0
    while start < len(value) and value[start] in _ELAN_WHITESPACE:
        start += 1
    end = len(value)
    while end > start and value[end - 1] in _ELAN_WHITESPACE:
        end -= 1
    return value[start:end]


def _inspect_toolchain(snapshot: _DecisionSnapshot, diagnostics: list[ProjectDiagnostic]) -> str | None:
    if snapshot.file("lean-toolchain").state == "missing":
        diagnostics.append(ProjectDiagnostic("error", "missing-lean-toolchain", "The project has no lean-toolchain."))
        return None
    text = _read_text(snapshot, "lean-toolchain", diagnostics)
    if text is None:
        return None
    # elan reads only the trimmed first line and rejects an existing file when
    # that line is empty or malformed.
    toolchain = _trim_elan_whitespace(text.split("\n", 1)[0])
    if not toolchain or not toolchain.isprintable() or any(character.isspace() for character in toolchain):
        diagnostics.append(
            ProjectDiagnostic(
                "error",
                "invalid-lean-toolchain",
                "elan rejects this lean-toolchain because its first line is empty or malformed.",
                "lean-toolchain",
            )
        )
        return None
    return toolchain


def _locked_mathlib(
    snapshot: _DecisionSnapshot, relative: str, diagnostics: list[ProjectDiagnostic]
) -> tuple[bool, _LockedPackages | None]:
    """Read the package names and Mathlib entry of a Lake manifest or package-overrides file, if any."""

    if snapshot.file(relative).state == "missing":
        return True, None
    text = _read_text(snapshot, relative, diagnostics)
    if text is None:
        return False, None
    try:
        payload = json.loads(text, parse_constant=_reject_json_constant, parse_int=_JsonInteger)
        if type(payload) is not dict:
            raise ValueError(relative)
        version = payload.get("version", payload.get("schemaVersion"))  # overrides use schemaVersion
        layout = _manifest_layout(version)
        if layout is None:
            raise ValueError(relative)
    except (AttributeError, RecursionError, ValueError):
        kind = "Lake manifest" if relative == _MANIFEST else "Lake package-overrides file"
        diagnostics.append(
            ProjectDiagnostic("error", "invalid-lake-manifest", f"{relative} is not a {kind} Autoform reads.", relative)
        )
        return False, None
    if layout == "legacy":
        if relative == _MANIFEST:
            message = (
                f"Lake still reads the legacy layout of {relative}, but Autoform does not; "
                "`lake update` rewrites it."
            )
        else:
            message = f"Lake still reads the legacy layout of {relative}, but Autoform does not decode it."
        diagnostics.append(ProjectDiagnostic("warning", "unsupported-lake-manifest", message, relative))
        return False, None
    try:
        if relative == _MANIFEST:
            _validate_manifest_root(payload)
        packages = payload.get("packages")
        packages = [] if packages is None else packages  # Lake's getD treats JSON null like an absent field.
        if type(packages) is not list:
            raise ValueError(relative)
        decoded = [_decode_package_entry(item, relative) for item in packages]
    except (AttributeError, RecursionError, ValueError):
        kind = "Lake manifest" if relative == _MANIFEST else "Lake package-overrides file"
        diagnostics.append(
            ProjectDiagnostic("error", "invalid-lake-manifest", f"{relative} is not a {kind} Autoform reads.", relative)
        )
        return False, None
    # Lake inserts entries into a NameMap in order, so the last duplicate wins.
    match = next((lock for name, lock in reversed(decoded) if name == _MATHLIB_NAME), None)
    return True, _LockedPackages(frozenset(name for name, _lock in decoded), match)


def _read_text(
    snapshot: _DecisionSnapshot, relative: str, diagnostics: list[ProjectDiagnostic]
) -> str | None:
    """Decode bytes already captured from the coherent project snapshot."""

    entry = snapshot.file(relative)
    try:
        if entry.state != "regular" or entry.content is None:
            raise OSError(relative)
        return entry.content.decode("utf-8")
    except (OSError, UnicodeError):
        diagnostics.append(
            ProjectDiagnostic(
                "error", "unreadable-file", f"{relative} is not a readable UTF-8 file of at most 1 MiB.", relative
            )
        )
        return None


def _exists_exactly(root: Path, relative: str) -> bool:
    """Whether a path exists with exactly this spelling, even on a case-insensitive filesystem.

    A Lean library named `Blueprint` must not be mistaken for the `blueprint` vault.
    """

    directory = root
    for part in relative.split("/"):
        try:
            if part not in os.listdir(directory):
                return False
        except OSError:
            return False
        directory = directory / part
    return True
