"""Autoform's bundled list of Lean and Mathlib release pairs."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from importlib.resources import files

RELEASE_CATALOG_SCHEMA = "autoform-project-release-catalog/v1"
_ELAN_LEAN4_RELEASE = re.compile(
    r"(?:(?:leanprover/lean4:)?v?)([0-9]+\.[0-9]+\.[0-9]+(?:-[^ \t\r\n]+)?)"
)
_GIT_URL_AUTHORITY = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://"
    r"(?P<userinfo>[^/@]*@)?"
    r"(?P<host>\[[^]]+\]|[^/:?#]+)"
    r"(?P<rest>.*)"
)


class ProjectCatalogError(ValueError):
    """The bundled release catalog is missing or malformed."""


@dataclass(frozen=True, slots=True)
class SupportedRelease:
    id: str
    recommended: bool
    lean_toolchain: str
    mathlib_git: str
    mathlib_rev: str
    mathlib_commit: str


@dataclass(frozen=True, slots=True)
class ReleaseCatalog:
    releases: tuple[SupportedRelease, ...]

    @property
    def recommended(self) -> SupportedRelease:
        return next(release for release in self.releases if release.recommended)

    def match(self, lean_toolchain: str, mathlib_git: str | None, mathlib_commit: str | None) -> SupportedRelease | None:
        commit = None if mathlib_commit is None else mathlib_commit.lower()  # Git reads either case
        toolchain = canonical_lean_toolchain(lean_toolchain)
        return next(
            (
                release
                for release in self.releases
                if canonical_lean_toolchain(release.lean_toolchain) == toolchain
                and release.mathlib_commit == commit
                and canonical_git_url(release.mathlib_git) == canonical_git_url(mathlib_git)
            ),
            None,
        )

    def as_dict(self) -> dict[str, object]:
        return {"releases": [asdict(release) for release in self.releases], "schema": RELEASE_CATALOG_SCHEMA}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def canonical_lean_toolchain(toolchain: str) -> str:
    """Expand the stable Lean release aliases that elan resolves identically."""

    match = _ELAN_LEAN4_RELEASE.fullmatch(toolchain)
    return f"leanprover/lean4:v{match.group(1)}" if match is not None else toolchain


def canonical_git_url(url: str | None) -> str | None:
    """Normalize Git URL spellings without changing repository-path identity."""

    if url is None:
        return None
    value = url.rstrip("/").removesuffix(".git")
    match = _GIT_URL_AUTHORITY.fullmatch(value)
    if match is None:
        return value
    return (
        f"{match.group('scheme').lower()}://"
        f"{match.group('userinfo') or ''}"
        f"{match.group('host').lower()}"
        f"{match.group('rest')}"
    )


def load_release_catalog() -> ReleaseCatalog:
    try:
        payload = json.loads(files(__package__).joinpath("releases.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProjectCatalogError("the bundled release catalog is unreadable") from error
    return parse_release_catalog(payload)


def parse_release_catalog(payload: object) -> ReleaseCatalog:
    try:
        if payload["schema"] != RELEASE_CATALOG_SCHEMA:
            raise ProjectCatalogError("the release catalog has an unsupported schema")
        releases = tuple(SupportedRelease(**entry) for entry in payload["releases"])
    except (KeyError, TypeError) as error:
        raise ProjectCatalogError("the release catalog is malformed") from error
    # ReleaseCatalog.recommended needs exactly one; `tests/test_project_create.py`
    # checks the rest of the bundled data.
    if sum(release.recommended is True for release in releases) != 1:
        raise ProjectCatalogError("the release catalog needs exactly one recommended release")
    return ReleaseCatalog(releases)


LIBRARY_CATALOG_SCHEMA = "autoform-project-library-catalog/v1"


@dataclass(frozen=True, slots=True)
class SharedLibrary:
    """A shared Lean library `project new --library` can require, at its pinned commit."""

    name: str
    git: str
    commit: str
    release: str
    offered: bool
    module_roots: tuple[str, ...]
    manifest_packages: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class LibraryCatalog:
    libraries: tuple[SharedLibrary, ...]

    def get(self, name: object) -> SharedLibrary | None:
        return next((library for library in self.libraries if library.name == name), None)


def load_library_catalog() -> LibraryCatalog:
    try:
        payload = json.loads(files(__package__).joinpath("libraries.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProjectCatalogError("the bundled library catalog is unreadable") from error
    return parse_library_catalog(payload)


def parse_library_catalog(payload: object) -> LibraryCatalog:
    try:
        if payload["schema"] != LIBRARY_CATALOG_SCHEMA:
            raise ProjectCatalogError("the library catalog has an unsupported schema")
        libraries = tuple(_parse_library(entry) for entry in payload["libraries"])
    except (KeyError, TypeError) as error:
        raise ProjectCatalogError("the library catalog is malformed") from error
    return LibraryCatalog(libraries)


def _parse_library(entry: object) -> SharedLibrary:
    roots, packages = tuple(entry["module_roots"]), tuple(entry["manifest_packages"])
    return SharedLibrary(**{**entry, "module_roots": roots, "manifest_packages": packages})
