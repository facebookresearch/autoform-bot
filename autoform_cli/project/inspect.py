"""Inspect a local Lean project's configuration without running Lake, Lean, or Git.

Compatibility is decided by `lean-toolchain` and the Mathlib entry that
`lake-manifest.json` locks, which is what `lake build` materializes; a
`.lake/package-overrides.json` entry replaces it. `lakefile.toml` is read for
the package name, targets, and whether the lock is used and current.
`lakefile.lean` is never evaluated, so those projects stay indeterminate.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .catalog import ReleaseCatalog, canonical_git_url, load_release_catalog

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

PROJECT_INSPECTION_SCHEMA = "autoform-project-inspection/v1"
_MAX_FILE_BYTES = 1024 * 1024
_ROOT_MARKERS = ("lakefile.lean", "lakefile.toml", "lean-toolchain")
_MANIFEST = "lake-manifest.json"
_OVERRIDES = ".lake/package-overrides.json"
_MATHLIB = ("mathlib", "«mathlib»")  # Lake reads both spellings as the same name
_LAKE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[^ \t\r\n]+)?")  # Lake's StdVer
_MANIFEST_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:-\S+)?")
_URL_CREDENTIALS = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@")
_AUTOFORM_PATHS: dict[str, Callable[[Path], bool]] = {
    "blueprint": Path.is_dir,
    "mkdocs.yml": Path.is_file,
    ".github/workflows/autoform-verify.yml": Path.is_file,
    ".github/workflows/blueprint-pages.yml": Path.is_file,
}


@dataclass(frozen=True, slots=True)
class ProjectDiagnostic:
    severity: str
    code: str
    message: str
    path: str | None = None


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
            and self.config_file == "lakefile.lean"
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
        root = next(
            (
                directory
                for directory in (start, *start.parents)
                if any(_present(directory / marker) for marker in _ROOT_MARKERS)
            ),
            None,
        )
    except (OSError, RuntimeError, ValueError):
        diagnostics.append(ProjectDiagnostic("error", "target-unreadable", "The inspection target cannot be resolved."))
        return _result(catalog, diagnostics)
    if root is None:
        diagnostics.append(
            ProjectDiagnostic("error", "project-not-found", "No enclosing Lean project (lakefile or lean-toolchain).")
        )
        return _result(catalog, diagnostics)

    lake, requirement = _inspect_lake(root, diagnostics)
    toolchain = _inspect_toolchain(root, diagnostics)
    has_manifest = _present(root / _MANIFEST)
    if not has_manifest:
        diagnostics.append(
            ProjectDiagnostic(
                "warning",
                "missing-lake-manifest",
                "There is no lake-manifest.json, so the locked Mathlib is unknown.",
                _MANIFEST,
            )
        )
    locked = _locked_mathlib(root, _MANIFEST, diagnostics)
    override = _locked_mathlib(root, _OVERRIDES, diagnostics)
    mathlib = locked
    if override is not None and has_manifest:  # Lake applies overrides to a manifest's packages
        diagnostics.append(
            ProjectDiagnostic(
                "warning",
                "mathlib-overridden",
                "Lake uses the Mathlib from package-overrides.json instead of the manifest's.",
                _OVERRIDES,
            )
        )
        mathlib = override
    elif locked is not None and requirement is not None and _is_stale(requirement, locked):
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
    elif mathlib is not None and lake is not None and requirement is None and not mathlib.inherited:
        diagnostics.append(
            ProjectDiagnostic(
                "warning",
                "mathlib-manifest-unused",
                "The manifest locks Mathlib directly, but lakefile.toml does not require it, so Lake will not build it.",
                mathlib.source,
            )
        )
        mathlib = None
    return _result(
        catalog,
        diagnostics,
        project_root="/".join([".."] * (len(start.parts) - len(root.parts))) or ".",
        lake=lake,
        lean_toolchain=toolchain,
        mathlib=mathlib,
        autoform_paths=tuple(
            path for path, kind in _AUTOFORM_PATHS.items() if _exists_exactly(root, path) and kind(root / path)
        ),
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
                    "The Lean toolchain or the Mathlib Lake will build is unknown, so compatibility cannot be checked.",
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
                    "This Lean and Mathlib pair is not in the bundled release catalog.",
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


def _inspect_lake(root: Path, diagnostics: list[ProjectDiagnostic]) -> tuple[LakeProject | None, dict | None]:
    if _present(root / "lakefile.lean"):
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
    if not _present(root / "lakefile.toml"):
        diagnostics.append(
            ProjectDiagnostic("error", "missing-lake-config", "The project has no lakefile.toml or lakefile.lean.")
        )
        return None, None
    text = _read_text(root, "lakefile.toml", diagnostics)
    if text is None:
        return None, None
    try:
        config = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        config = None
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
    requirement = next((entry for entry in reversed(config.get("require", [])) if entry["name"] in _MATHLIB), None)
    return LakeProject("lakefile.toml", config["name"], config.get("version"), targets), requirement


def _lakefile_problem(config: dict) -> str | None:
    """Why Lake would refuse the fields Autoform reads, if it would."""

    if not _is_name(config.get("name")):
        return "it has no package name"
    version = config.get("version")
    if version is not None and not (isinstance(version, str) and _LAKE_VERSION.fullmatch(version)):
        return "its version is not major.minor.patch"
    if not all(_are_named_tables(config.get(key, [])) for key in ("require", "lean_lib", "lean_exe")):
        return "a require, lean_lib, or lean_exe entry has no name"
    targets = [entry["name"] for key in ("lean_lib", "lean_exe") for entry in config.get(key, [])]
    if len(set(targets)) != len(targets):
        return "two targets share a name"
    return None


def _inspect_toolchain(root: Path, diagnostics: list[ProjectDiagnostic]) -> str | None:
    if not _present(root / "lean-toolchain"):
        diagnostics.append(ProjectDiagnostic("error", "missing-lean-toolchain", "The project has no lean-toolchain."))
        return None
    text = _read_text(root, "lean-toolchain", diagnostics)
    if text is None:
        return None
    # elan reads only the trimmed first line, and silently uses the default
    # toolchain when that line is empty or malformed.
    toolchain = text.split("\n", 1)[0].strip()
    if not toolchain or not toolchain.isprintable() or any(character.isspace() for character in toolchain):
        diagnostics.append(
            ProjectDiagnostic(
                "error",
                "invalid-lean-toolchain",
                "elan ignores this lean-toolchain because its first line is empty or malformed.",
                "lean-toolchain",
            )
        )
        return None
    return toolchain


def _locked_mathlib(root: Path, relative: str, diagnostics: list[ProjectDiagnostic]) -> MathlibLock | None:
    """Read the Mathlib entry of a Lake manifest or package-overrides file, if any."""

    if not _present(root / relative):
        return None
    text = _read_text(root, relative, diagnostics)
    if text is None:
        return None
    try:
        payload = json.loads(text, parse_constant=_reject_json_constant)
        layout = _manifest_layout(payload.get("version", payload.get("schemaVersion")))  # overrides use schemaVersion
        packages = payload.get("packages")
        packages = [] if packages is None else packages  # Lake reads null as no packages
        if layout is None or not isinstance(packages, list) or not all(isinstance(item, dict) for item in packages):
            raise ValueError(relative)
        # Lake keeps the last of several packages with the same name.
        entry = next((package for package in reversed(packages) if package.get("name") in _MATHLIB), None)
        if entry is not None and layout == "current" and entry.get("type") not in ("git", "path"):
            raise ValueError(relative)
    except (AttributeError, RecursionError, ValueError):
        diagnostics.append(
            ProjectDiagnostic("error", "invalid-lake-manifest", f"{relative} is not a Lake manifest Autoform reads.", relative)
        )
        return None
    if layout == "legacy":
        diagnostics.append(
            ProjectDiagnostic(
                "warning",
                "unsupported-lake-manifest",
                f"Lake still reads the legacy layout of {relative}, but Autoform does not; `lake update` rewrites it.",
                relative,
            )
        )
        return None
    if entry is None:
        return None
    common = {"source": relative, "inherited": entry.get("inherited") is True}
    if entry["type"] == "path":
        return MathlibLock("path", dir=_string(entry.get("dir")), **common)
    return MathlibLock(
        "git",
        url=_redact(_string(entry.get("url"))),
        input_rev=_string(entry.get("inputRev")),
        rev=_string(entry.get("rev")),
        sub_dir=_string(entry.get("subDir")),
        config_file=_string_or(entry.get("configFile"), "lakefile"),
        manifest_file=_string_or(entry.get("manifestFile"), "lake-manifest.json"),
        **common,
    )


def _manifest_layout(version: object) -> str | None:
    """Lake reads manifest versions 5 (0.5.0) up to 2.0.0; those before 7 (0.7.0) use a legacy layout."""

    if isinstance(version, int) and not isinstance(version, bool):
        parts = (0, version, 0)
    elif isinstance(version, str) and (match := _MANIFEST_VERSION.fullmatch(version)):
        parts = tuple(int(part) for part in match.groups())
    else:
        return None
    if parts < (0, 5, 0) or parts[0] > 1:
        return None
    return "legacy" if parts < (0, 7, 0) else "current"


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"Lake's JSON parser rejects {constant}")


def _is_stale(requirement: dict, locked: MathlibLock) -> bool:
    """Whether lakefile.toml asks for a different Mathlib source than the lock records."""

    if ("path" in requirement) != (locked.type == "path"):
        return True
    revision = requirement.get("rev")
    git = requirement.get("git")
    return locked.type == "git" and (
        (isinstance(revision, str) and revision != locked.input_rev)
        or (isinstance(git, str) and canonical_git_url(_redact(git)) != canonical_git_url(locked.url))
    )


def _redact(url: str | None) -> str | None:
    """Hide credentials embedded in a Git URL, since reports end up in logs."""

    return None if url is None else _URL_CREDENTIALS.sub(r"\1***@", url)


def _read_text(root: Path, relative: str, diagnostics: list[ProjectDiagnostic]) -> str | None:
    """Read a small UTF-8 regular file; never opens FIFOs or devices."""

    path = root / relative
    try:
        if not path.is_file():
            raise OSError(relative)
        with path.open("rb") as handle:
            data = handle.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise OSError(relative)
        return data.decode("utf-8")
    except (OSError, UnicodeError):
        diagnostics.append(
            ProjectDiagnostic(
                "error", "unreadable-file", f"{relative} is not a readable UTF-8 file of at most 1 MiB.", relative
            )
        )
        return None


def _present(path: Path) -> bool:
    try:
        return path.exists() or path.is_symlink()
    except OSError:
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
    return isinstance(value, str) and bool(value)


def _are_named_tables(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(entry, dict) and _is_name(entry.get("name")) for entry in value)


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _string_or(value: object, default: str) -> str | None:
    """Lake's default for an absent or null field; any other non-string is malformed."""

    return default if value is None else _string(value)
