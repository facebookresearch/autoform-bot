"""Autoform's bundled list of known-good Lean and Mathlib release pairs."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from importlib.resources import files

RELEASE_CATALOG_SCHEMA = "autoform-project-release-catalog/v1"
_COMMIT = re.compile(r"[0-9a-f]{40}")


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
        return next(
            (
                release
                for release in self.releases
                if release.lean_toolchain == lean_toolchain
                and release.mathlib_commit == commit
                and canonical_git_url(release.mathlib_git) == canonical_git_url(mathlib_git)
            ),
            None,
        )

    def as_dict(self) -> dict[str, object]:
        return {"releases": [asdict(release) for release in self.releases], "schema": RELEASE_CATALOG_SCHEMA}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def canonical_git_url(url: str | None) -> str | None:
    """Drop the trailing `/` and `.git`, which do not change the repository a URL names."""

    return None if url is None else url.rstrip("/").removesuffix(".git")


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
    for release in releases:
        strings = (release.id, release.lean_toolchain, release.mathlib_git, release.mathlib_rev, release.mathlib_commit)
        if (
            not all(isinstance(value, str) and value for value in strings)
            or not isinstance(release.recommended, bool)
            or not _COMMIT.fullmatch(release.mathlib_commit)
        ):
            raise ProjectCatalogError(f"release {release.id!r} is malformed")
    pairs = {(release.lean_toolchain, release.mathlib_commit) for release in releases}
    if (
        not releases
        or len({release.id for release in releases}) != len(releases)
        or len(pairs) != len(releases)
        or sum(release.recommended for release in releases) != 1
    ):
        raise ProjectCatalogError("releases need unique ids and pairs, with exactly one recommended")
    return ReleaseCatalog(releases)
