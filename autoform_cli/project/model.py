"""Immutable schemas for offline Autoform project inspection."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

PROJECT_INSPECTION_SCHEMA = "autoform-project-inspection/v2"
RELEASE_CATALOG_SCHEMA = "autoform-project-release-catalog/v1"


@dataclass(frozen=True, order=True, slots=True)
class ProjectDiagnostic:
    severity: str
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LeanRelease:
    toolchain: str
    version: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MathlibRelease:
    git: str
    revision: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SupportedRelease:
    id: str
    channel: str
    recommended: bool
    lean: LeanRelease
    mathlib: MathlibRelease

    def as_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "id": self.id,
            "lean": self.lean.as_dict(),
            "mathlib": self.mathlib.as_dict(),
            "recommended": self.recommended,
        }


@dataclass(frozen=True, slots=True)
class ReleaseCatalog:
    schema: str
    releases: tuple[SupportedRelease, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "releases": [release.as_dict() for release in self.releases],
            "schema": self.schema,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def recommended(self) -> SupportedRelease:
        return next(release for release in self.releases if release.recommended)


@dataclass(frozen=True, slots=True)
class LakeTarget:
    kind: str
    name: str
    root: str | None
    roots: tuple[str, ...] | None
    src_dir: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "root": self.root,
            "roots": list(self.roots) if self.roots is not None else None,
            "src_dir": self.src_dir,
        }


@dataclass(frozen=True, slots=True)
class LakeProject:
    format: str
    path: str
    sha256: str
    name: str | None
    version: str | None
    default_targets: tuple[str, ...]
    package_src_dir: str | None
    targets: tuple[LakeTarget, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "default_targets": list(self.default_targets),
            "format": self.format,
            "name": self.name,
            "package_src_dir": self.package_src_dir,
            "path": self.path,
            "sha256": self.sha256,
            "targets": [target.as_dict() for target in self.targets],
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class LeanProject:
    path: str
    sha256: str
    toolchain: str
    version: str | None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MathlibProject:
    git: str | None
    revision: str | None
    source: str

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AutoformProject:
    detected: bool
    blueprint_path: str | None
    mkdocs_path: str | None
    verification_workflow_path: str | None
    pages_workflow_path: str | None
    blueprint_paths: tuple[str, ...] = ()
    manifest_path: str | None = None
    manifest_sha256: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "blueprint_path": self.blueprint_path,
            "blueprint_paths": list(self.blueprint_paths),
            "detected": self.detected,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "mkdocs_path": self.mkdocs_path,
            "pages_workflow_path": self.pages_workflow_path,
            "verification_workflow_path": self.verification_workflow_path,
        }


@dataclass(frozen=True, slots=True)
class ProjectCompatibility:
    catalog: str
    status: str
    release: str | None
    recommended_release: str

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProjectInspection:
    schema: str
    project_root: str | None
    git_path: str | None
    lake: LakeProject | None
    lake_manifest_path: str | None
    lake_manifest_sha256: str | None
    lean: LeanProject | None
    mathlib: MathlibProject | None
    autoform: AutoformProject
    compatibility: ProjectCompatibility
    diagnostics: tuple[ProjectDiagnostic, ...]

    @property
    def ok(self) -> bool:
        return not any(diagnostic.severity == "error" for diagnostic in self.diagnostics)

    def as_dict(self) -> dict[str, object]:
        return {
            "autoform": self.autoform.as_dict(),
            "compatibility": self.compatibility.as_dict(),
            "diagnostics": [diagnostic.as_dict() for diagnostic in self.diagnostics],
            "git_path": self.git_path,
            "lake": self.lake.as_dict() if self.lake is not None else None,
            "lake_manifest_path": self.lake_manifest_path,
            "lake_manifest_sha256": self.lake_manifest_sha256,
            "lean": self.lean.as_dict() if self.lean is not None else None,
            "mathlib": self.mathlib.as_dict() if self.mathlib is not None else None,
            "ok": self.ok,
            "project_root": self.project_root,
            "schema": self.schema,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
