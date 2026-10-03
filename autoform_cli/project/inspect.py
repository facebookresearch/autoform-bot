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
import re
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import tomli

from .catalog import ReleaseCatalog, canonical_git_url, load_release_catalog

PROJECT_INSPECTION_SCHEMA = "autoform-project-inspection/v1"
_MAX_FILE_BYTES = 1024 * 1024
_SNAPSHOT_ATTEMPTS = 3
_ROOT_MARKERS = ("lakefile.lean", "lakefile.toml", "lean-toolchain")
_MANIFEST = "lake-manifest.json"
_OVERRIDES = ".lake/package-overrides.json"
_DECISION_FILES = (*_ROOT_MARKERS, _MANIFEST, _OVERRIDES)
_TARGET_KINDS = ("lean_lib", "lean_exe", "input_file", "input_dir")  # the kinds lakefile.toml declares
_MATHLIB_NAME = (("str", "mathlib"),)
_LAKE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[^ \t\r\n]+)?")  # Lake's StdVer
_MANIFEST_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:-[^ \t\r\n]+)?")
_URL_CREDENTIALS = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@")
_LEAN_ID_BEGIN_ESCAPE = "«"
_LEAN_ID_END_ESCAPE = "»"
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


class _JsonInteger(str):
    """A JSON integer kept as bounded input text instead of materialized as a Python bigint."""


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    """One bounded decision-file read, including its filesystem generation."""

    state: str
    identity: tuple[int, ...] | None = None
    content: bytes | None = None


@dataclass(frozen=True, slots=True)
class _DecisionSnapshot:
    """The small set of bytes from which an inspection answer is derived."""

    root_identity: tuple[int, ...] | None
    lake_directory: tuple[str, tuple[int, ...] | None]
    aliases: tuple[tuple[str, tuple[str, ...]], ...]
    files: tuple[tuple[str, _FileSnapshot], ...]

    @property
    def stable(self) -> bool:
        return (
            self.root_identity is not None
            and self.lake_directory[0] != "changed"
            and all(name != "<root>" or "<unreadable>" not in entries for name, entries in self.aliases)
            and all(entry.state != "changed" for _, entry in self.files)
        )

    def file(self, relative: str) -> _FileSnapshot:
        return dict(self.files)[relative]


@dataclass(frozen=True, slots=True)
class LakeTarget:
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class LakeProject:
    config: str
    name: str | None
    version: str | None
    targets: tuple[LakeTarget, ...]


@dataclass(frozen=True, slots=True)
class _Requirements:
    """lakefile.toml's require entries, as Lake resolves them against the root manifest.

    ``mathlib`` is the direct Mathlib requirement Lake keeps, if Lake resolves it from the manifest.
    ``transitive`` is whether another requirement could pull Mathlib in; it is not evidence that the
    dependency's current configuration actually does so, because dependency lakefiles are not read.
    ``declared`` is whether there is any require entry, and ``lookups`` holds the Lean name and
    spelling of each requirement Lake looks up in the manifest and overrides rather than satisfying
    with the root package itself.
    """

    mathlib: dict | None
    transitive: bool
    declared: bool = False
    lookups: tuple[tuple[tuple[tuple[str, str | int], ...], str], ...] = ()


@dataclass(frozen=True, slots=True)
class _LockedPackages:
    """The package names a manifest or package-overrides file records, and its Mathlib entry."""

    names: frozenset[tuple[tuple[str, str | int], ...]]
    mathlib: MathlibLock | None


@dataclass(frozen=True, slots=True)
class MathlibLock:
    """A Lake manifest or package-overrides entry for Mathlib."""

    type: str
    source: str
    inherited: bool
    url: str | None = None
    input_rev: str | None = None
    rev: str | None = None
    dir: str | None = None
    sub_dir: str | None = None
    config_file: str | None = None
    manifest_file: str | None = None

    @property
    def loads_like_a_release(self) -> bool:
        """Whether Lake loads Mathlib from its repository root with its own lakefile and manifest."""

        return (
            self.type == "git"
            and self.sub_dir in (None, "", ".")
            # Extensionless `lakefile` is Lake's default and resolves by
            # preferring lakefile.lean before lakefile.toml.
            and self.config_file in ("lakefile", "lakefile.lean")
            and self.manifest_file == "lake-manifest.json"
        )


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
        snapshot = _capture_decision_snapshot(root)
        attempt_diagnostics: list[ProjectDiagnostic] = []
        result = _inspect_snapshot(
            catalog,
            snapshot,
            attempt_diagnostics,
            project_root=project_root,
            autoform_paths=autoform_paths,
        )
        verified = _capture_decision_snapshot(root)
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
    elif overrides_valid and locked is not None and requirement is not None and _is_stale(requirement, locked):
        diagnostics.append(
            ProjectDiagnostic(
                "warning",
                "lake-manifest-stale",
                "lakefile.toml requests a different Mathlib than the manifest locks; Lake builds the locked one.",
                _MANIFEST,
            )
        )
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


def _lakefile_problem(config: dict) -> str | None:
    """Why Lake would refuse the fields Autoform reads, if it would."""

    if not _is_name(config.get("name")):
        return "it has no package name"
    version = config.get("version")
    if version is not None and not (isinstance(version, str) and _LAKE_VERSION.fullmatch(version)):
        return "its version is not major.minor.patch"
    if not all(_are_named_tables(config.get(key, [])) for key in ("require", *_TARGET_KINDS)):
        return "a require or target entry has no name"
    for requirement in config.get("require", []):
        problem = _requirement_problem(requirement)
        if problem is not None:
            return problem
    # Lake's decodeTargetDecls keeps one name map for every target kind, so a
    # reported lean_lib or lean_exe also clashes with an input target.
    targets = [
        _canonical_toml_name(entry["name"])
        for key in _TARGET_KINDS
        for entry in config.get(key, [])
    ]
    if len(set(targets)) != len(targets):
        return "two targets share a name"
    return None


def _requirement_problem(requirement: dict[str, object]) -> str | None:
    """Validate the fields Lake 4.32's ``Dependency.decodeToml`` consumes."""

    for key in ("rev", "scope"):
        if key in requirement and type(requirement[key]) is not str:
            return f"a require entry has a non-string {key}"
    if "options" in requirement and not (
        type(requirement["options"]) is dict
        and all(type(key) is str and type(value) is str for key, value in requirement["options"].items())
    ):
        return "a require entry has malformed options"
    if "version" in requirement:
        version = requirement["version"]
        if type(version) is not str or not _input_version_is_supported(version):
            return "a require entry has an invalid version constraint"

    # Lake gives `path` precedence over `git`, and `git` precedence over
    # `source`; fields in shadowed source forms are not decoded.
    if "path" in requirement:
        return None if type(requirement["path"]) is str else "a require entry has a non-string path"
    if "git" in requirement:
        git = requirement["git"]
        if type(git) is str:
            if "subDir" in requirement and type(requirement["subDir"]) is not str:
                return "a require entry has a non-string subDir"
            return None
        if type(git) is dict and type(git.get("url")) is str:
            # The table form reads subDir from the inner table but, somewhat
            # surprisingly, reads rev only from the enclosing requirement.
            if "subDir" in git and type(git["subDir"]) is not str:
                return "a require git table has a non-string subDir"
            return None
        return "a require entry has a malformed git source"
    if "source" not in requirement:
        return None
    source = requirement["source"]
    if type(source) is not dict or type(source.get("type")) is not str:
        return "a require entry has a malformed source"
    if source["type"] == "path":
        return None if type(source.get("dir")) is str else "a require path source has no string dir"
    if source["type"] == "git":
        if type(source.get("url")) is not str:
            return "a require git source has no string url"
        for key in ("rev", "subDir"):
            if key in source and type(source[key]) is not str:
                return f"a require git source has a non-string {key}"
        return None
    return "a require source has an unknown type"


def _input_version_is_supported(value: str) -> bool:
    """Recognize Lake 4.32's ``InputVer.parse`` / ``VerRange.parse`` grammar."""

    if value.startswith("git#"):
        return True
    index = 0
    clause_has_term = False
    needs_term = True
    while index < len(value):
        if _lake_whitespace(value[index]):
            index += 1
            continue
        if value[index] == ",":
            if needs_term:
                return False
            needs_term = True
            index += 1
            continue
        if value.startswith("||", index):
            # Lake checks whether the current conjunction has a term, but not
            # its `needsRange` flag here (so even `term, || term` is accepted).
            if not clause_has_term:
                return False
            clause_has_term = False
            needs_term = True
            index += 2
            continue
        end = _version_range_term_end(value, index)
        if end is None:
            return False
        clause_has_term = True
        needs_term = False
        index = end
    return clause_has_term and not needs_term


def _version_range_term_end(value: str, index: int) -> int | None:
    for operator in ("<=", ">=", "!=", "<", "≤", ">", "≥", "=", "≠"):
        if value.startswith(operator, index):
            match = re.match(r"[0-9]+\.[0-9]+\.[0-9]+", value[index + len(operator) :])
            if match is None:
                return None
            end = index + len(operator) + match.end()
            if end < len(value) and value[end] == "-":
                end += 1
                while end < len(value) and not _lake_whitespace(value[end]):
                    end += 1
            return end

    prefix = value[index] if value[index] in "^~" else None
    start = index + 1 if prefix is not None else index
    end = start
    while end < len(value) and (value[end].isascii() and value[end].isalnum() or value[end] in ".*"):
        end += 1
    if end == start:
        return None
    components = value[start:end].split(".")
    if not 1 <= len(components) <= 3 or any(not component for component in components):
        return None
    if prefix is not None:
        if any(not component.isascii() or not component.isdigit() for component in components):
            return None
        suffix = ""
        if end < len(value) and value[end] == "-":
            suffix_start = end + 1
            end = suffix_start
            while end < len(value) and not _lake_whitespace(value[end]):
                end += 1
            suffix = value[suffix_start:end]
            if not suffix:
                return None
        if (
            prefix == "^"
            and len(components) == 3
            and all(_normalize_decimal(component) == "0" for component in components)
            and not suffix
        ):
            return None
        return end

    if end < len(value) and value[end] == "-":
        return None
    wildcards = {"x", "X", "*"}
    first_wild = next((position for position, component in enumerate(components) if component in wildcards), None)
    if first_wild is None:
        return None
    if any(
        component not in wildcards and (not component.isascii() or not component.isdigit())
        for component in components
    ):
        return None
    if any(component not in wildcards for component in components[first_wild:]):
        return None
    return end


def _lake_whitespace(character: str) -> bool:
    return character in " \t\r\n"


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
        payload = json.loads(text, parse_constant=_reject_json_constant, parse_int=_parse_json_integer)
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


def _validate_manifest_root(payload: dict[str, object]) -> None:
    """Validate every field decoded by ``Lake.Manifest.fromJson?`` in 4.32."""

    name = payload.get("name")
    if name is not None and _canonical_manifest_name(_json_string(name)) is None:
        raise ValueError("name")
    _json_default(payload, "lakeDir", ".lake", str)
    _json_default(payload, "fixedToolchain", False, bool)
    _json_optional(payload, "packagesDir", str)


def _decode_package_entry(
    entry: object, source: str
) -> tuple[tuple[tuple[str, str], ...], MathlibLock]:
    """Mirror ``Lake.PackageEntry.fromJson?`` for the current manifest layout."""

    if type(entry) is not dict:
        raise ValueError("package entry")
    name = _canonical_manifest_name(_json_required(entry, "name", str))
    if name is None:
        raise ValueError("package name")
    _json_default(entry, "scope", "", str)
    inherited = _json_required(entry, "inherited", bool)
    config_file = _json_default(entry, "configFile", "lakefile", str)
    manifest_file = _json_default(entry, "manifestFile", "lake-manifest.json", str)
    package_type = _json_required(entry, "type", str)
    common = {
        "source": source,
        "inherited": inherited,
        "config_file": config_file,
        "manifest_file": manifest_file,
    }
    if package_type == "path":
        return name, MathlibLock("path", dir=_json_required(entry, "dir", str), **common)
    if package_type != "git":
        raise ValueError("package type")
    return name, MathlibLock(
        "git",
        url=_redact(_json_required(entry, "url", str)),
        input_rev=_json_optional(entry, "inputRev", str),
        rev=_json_required(entry, "rev", str),
        sub_dir=_json_optional(entry, "subDir", str),
        **common,
    )


def _json_required(mapping: dict[str, object], key: str, expected: type):
    if key not in mapping or type(mapping[key]) is not expected:
        raise ValueError(key)
    return mapping[key]


def _json_default(mapping: dict[str, object], key: str, default, expected: type):
    value = mapping.get(key)
    if value is None:
        return default
    if type(value) is not expected:
        raise ValueError(key)
    return value


def _json_optional(mapping: dict[str, object], key: str, expected: type):
    value = mapping.get(key)
    if value is None:
        return None
    if type(value) is not expected:
        raise ValueError(key)
    return value


def _manifest_layout(version: object) -> str | None:
    """Lake reads versions from 0.5.0 through any 1.x; versions before 0.7 are legacy."""

    if type(version) is int:
        if version < 5:
            return None
        return "legacy" if version < 7 else "current"
    if isinstance(version, _JsonInteger):
        numeric_version = _normalize_unsigned_decimal(version)
        if numeric_version is None or _decimal_less_than(numeric_version, "5"):
            return None
        return "legacy" if _decimal_less_than(numeric_version, "7") else "current"
    if type(version) is not str or (match := _MANIFEST_VERSION.fullmatch(version)) is None:
        return None
    major, minor, _patch = (_normalize_decimal(part) for part in match.groups())
    if major == "1":
        return "current"
    if major != "0" or _decimal_less_than(minor, "5"):
        return None
    return "legacy" if _decimal_less_than(minor, "7") else "current"


def _parse_json_integer(value: str) -> _JsonInteger:
    """Keep Lake's arbitrary-precision JSON naturals lexical and size-bounded by the file cap."""

    return _JsonInteger(value)


def _normalize_unsigned_decimal(value: str) -> str | None:
    if not value or any(character < "0" or character > "9" for character in value):
        return None
    return _normalize_decimal(value)


def _normalize_decimal(value: str) -> str:
    """Canonicalize known ASCII digits without constructing an unbounded integer."""

    return value.lstrip("0") or "0"


def _decimal_less_than(left: str, right: str) -> bool:
    return (len(left), left) < (len(right), right)


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"Lake's JSON parser rejects {constant}")


def _is_stale(requirement: dict, locked: MathlibLock) -> bool:
    """Whether lakefile.toml asks for a different Mathlib source than the lock records."""

    kind, git, revision = _requirement_source(requirement)
    if kind is not None and (kind == "path") != (locked.type == "path"):
        return True
    return locked.type == "git" and (
        (revision is not None and revision != locked.input_rev)
        or (git is not None and canonical_git_url(_redact(git)) != canonical_git_url(locked.url))
    )


def _requirement_source(requirement: dict) -> tuple[str | None, str | None, str | None]:
    """Return the source fields selected by Lake's precedence rules."""

    if "path" in requirement:
        return "path", None, None
    if "git" in requirement:
        git = requirement["git"]
        url = git if isinstance(git, str) else git["url"]
        revision = requirement.get("rev")
        return "git", url, revision if isinstance(revision, str) else None
    source = requirement.get("source")
    if isinstance(source, dict):
        if source.get("type") == "path":
            return "path", None, None
        if source.get("type") == "git":
            revision = source.get("rev")
            return "git", source["url"], revision if isinstance(revision, str) else None
    revision = requirement.get("rev")
    return None, None, revision if isinstance(revision, str) else None


def _redact(url: str | None) -> str | None:
    """Hide credentials embedded in a Git URL, since reports end up in logs."""

    return None if url is None else _URL_CREDENTIALS.sub(r"\1***@", url)


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


def _capture_decision_snapshot(root: Path) -> _DecisionSnapshot:
    """Read all decision files once; callers re-read and compare the full value.

    POSIX opens use ``O_NONBLOCK`` and ``O_NOFOLLOW``.  The descriptor is
    verified as a regular file before any read, the read is bounded, and both
    descriptor and pathname identities are checked afterwards.  Platforms
    without those flags still get the same pre/open/post identity checks.
    """

    try:
        root_before = os.stat(root, follow_symlinks=False)
        root_identity = _node_identity(root_before) if stat.S_ISDIR(root_before.st_mode) else None
    except (OSError, TypeError, ValueError):
        root_identity = None
    lake_before = _directory_generation(root / ".lake")

    aliases = _decision_aliases(root)
    files = tuple((relative, _capture_file(root, relative)) for relative in _DECISION_FILES)
    try:
        root_after = os.stat(root, follow_symlinks=False)
        if _node_identity(root_after) != root_identity:
            root_identity = None
    except (OSError, TypeError, ValueError):
        root_identity = None
    lake_after = _directory_generation(root / ".lake")
    lake_directory = lake_before if lake_before == lake_after else ("changed", None)
    return _DecisionSnapshot(root_identity, lake_directory, aliases, files)


def _capture_file(root: Path, relative: str) -> _FileSnapshot:
    """Capture one decision file without ever reading a non-regular node."""

    path = root / relative
    parent_tokens: list[tuple[Path, tuple[int, ...]]] = []
    try:
        parent = root
        for part in Path(relative).parts[:-1]:
            parent = parent / part
            metadata = os.stat(parent, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                return _FileSnapshot("unreadable", _node_identity(metadata))
            parent_tokens.append((parent, _node_identity(metadata)))
        before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return _FileSnapshot("missing")
    except (OSError, TypeError, ValueError):
        return _FileSnapshot("unreadable")
    before_token = _file_identity(before)
    if not stat.S_ISREG(before.st_mode):
        return _FileSnapshot("unreadable", before_token)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = _open_beneath(root, relative, flags)
        opened = os.fstat(descriptor)
        opened_token = _file_identity(opened)
        if not stat.S_ISREG(opened.st_mode) or opened_token != before_token:
            return _FileSnapshot("changed", opened_token)
        chunks: list[bytes] = []
        remaining = _MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after_token = _file_identity(os.fstat(descriptor))
        try:
            path_token = _file_identity(os.stat(path, follow_symlinks=False))
            parents_unchanged = all(
                _node_identity(os.stat(parent, follow_symlinks=False)) == token
                for parent, token in parent_tokens
            )
        except (OSError, TypeError, ValueError):
            return _FileSnapshot("changed", after_token)
        if before_token != after_token or after_token != path_token or not parents_unchanged:
            return _FileSnapshot("changed", after_token)
        if len(data) > _MAX_FILE_BYTES:
            return _FileSnapshot("unreadable", after_token)
        return _FileSnapshot("regular", after_token, data)
    except (OSError, TypeError, ValueError):
        # A stable permission failure remains comparable across snapshots.  A
        # replacement is caught by the pathname identity in the second pass.
        try:
            current = os.stat(path, follow_symlinks=False)
            current_token = _file_identity(current)
        except (OSError, TypeError, ValueError):
            return _FileSnapshot("changed")
        return _FileSnapshot("unreadable", current_token) if current_token == before_token else _FileSnapshot("changed")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_beneath(root: Path, relative: str, flags: int) -> int:
    """Open a file below ``root`` without following parent links when POSIX supports it."""

    if os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"):
        return os.open(root / relative, flags)
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent = os.open(root, directory_flags)
    try:
        for part in Path(relative).parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(Path(relative).name, flags, dir_fd=parent)
    finally:
        os.close(parent)


def _decision_aliases(root: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Snapshot relevant directory entries so case-only/presence changes retry."""

    top_names = (*_ROOT_MARKERS, _MANIFEST, ".lake")
    try:
        top_entries = os.listdir(root)
    except (OSError, TypeError, ValueError):
        return (("<root>", ("<unreadable>",)),)
    aliases = [(name, tuple(sorted(entry for entry in top_entries if entry.casefold() == name.casefold()))) for name in top_names]
    lake_aliases = next(values for name, values in aliases if name == ".lake")
    if lake_aliases:
        try:
            lake_entries = _list_directory_beneath(root, ".lake")
            override = Path(_OVERRIDES).name
            aliases.append(
                (_OVERRIDES, tuple(sorted(entry for entry in lake_entries if entry.casefold() == override.casefold())))
            )
        except (OSError, TypeError, ValueError):
            aliases.append((_OVERRIDES, ("<unreadable>",)))
    else:
        aliases.append((_OVERRIDES, ()))
    return tuple(aliases)


def _list_directory_beneath(root: Path, relative: str) -> list[str]:
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"):
        metadata = os.stat(root / relative, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError(relative)
        return os.listdir(root / relative)
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = _open_beneath(root, relative, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(relative)
        return os.listdir(descriptor)
    finally:
        os.close(descriptor)


def _node_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return _node_identity(metadata)


def _directory_generation(path: Path) -> tuple[str, tuple[int, ...] | None]:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return "missing", None
    except (OSError, TypeError, ValueError):
        return "unreadable", None
    return ("directory" if stat.S_ISDIR(metadata.st_mode) else "other", _node_identity(metadata))


def _path_present(path: Path) -> bool:
    try:
        os.stat(path, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except (OSError, TypeError, ValueError):
        # An unreadable marker must keep this directory in consideration; a
        # later safe capture will report the specific failure. Python 3.14's
        # Path.exists()/is_symlink() suppress these errors, so do not use them.
        return True


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


def _is_name(value: object) -> bool:
    # Lake's stringToLegalOrSimpleName accepts even the empty string by
    # falling back to a simple escaped Name.
    return type(value) is str


def _are_named_tables(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(entry, dict) and _is_name(entry.get("name")) for entry in value)


def _json_string(value: object) -> str:
    if type(value) is not str:
        raise ValueError("expected string")
    return value


def _canonical_manifest_name(value: str) -> tuple[tuple[str, str], ...] | None:
    """Return the structural Name produced by JSON's strict ``String.toName``."""

    if value == "[anonymous]":
        return ()
    parts = _split_lean_name(value)
    if parts is None:
        return None
    return tuple((kind, _normalize_decimal(text) if kind == "num" else text) for kind, text in parts)


def _canonical_toml_name(value: str) -> tuple[tuple[str, str], ...]:
    """Return Lake's Name, including TOML's simple-name fallback."""

    parts = _split_lean_name(value)
    if parts is None:
        return (("str", value),)
    return tuple((kind, _normalize_decimal(text) if kind == "num" else text) for kind, text in parts)


def _split_lean_name(value: str) -> list[tuple[str, str]] | None:
    parts: list[tuple[str, str]] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == _LEAN_ID_BEGIN_ESCAPE:
            end = value.find(_LEAN_ID_END_ESCAPE, index + 1)
            if end < 0:
                return None
            parts.append(("str", value[index + 1 : end]))
            index = end + 1
        elif _lean_is_id_first(character):
            start = index
            index += 1
            while index < len(value) and _lean_is_id_rest(value[index]):
                index += 1
            parts.append(("str", value[start:index]))
        elif "0" <= character <= "9":
            start = index
            while index < len(value) and "0" <= value[index] <= "9":
                index += 1
            parts.append(("num", value[start:index]))
        else:
            return None
        if index == len(value):
            return parts
        if value[index] != ".":
            return None
        index += 1
    return None


def _lean_is_id_first(character: str) -> bool:
    return character == "_" or "a" <= character <= "z" or "A" <= character <= "Z" or _lean_is_letter_like(character)


def _lean_is_id_rest(character: str) -> bool:
    return (
        "a" <= character <= "z"
        or "A" <= character <= "Z"
        or "0" <= character <= "9"
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
    return 0x2080 <= code <= 0x2089 or 0x2090 <= code <= 0x209C or 0x1D62 <= code <= 0x1D6A or code == 0x2C7C
