from __future__ import annotations

import copy
import json
import re
from importlib.resources import files

import pytest

from autoform_cli.project import (
    LIBRARY_CATALOG_SCHEMA,
    ProjectCatalogError,
    load_library_catalog,
    load_release_catalog,
)
from autoform_cli.project.catalog import parse_library_catalog

_BUNDLED = json.loads(files("autoform_cli.project").joinpath("libraries.json").read_text(encoding="utf-8"))
_RELEASES = load_release_catalog()


def _release_package_names(release_id: str) -> set[str]:
    manifest = json.loads(
        files("autoform_cli.project").joinpath(f"release-manifest-{release_id}.json").read_text(encoding="utf-8")
    )
    return {entry["name"].casefold() for entry in manifest["packages"]}


def test_bundled_atlas_entry_is_offered_at_its_published_index() -> None:
    atlas = load_library_catalog().get("atlas")

    assert atlas is not None
    assert atlas.git == "https://github.com/facebookresearch/atlas-lean"
    # The commit the tag autoform-index/64c2773ffad3 adds the index in.
    assert atlas.commit == "d2d6fc8d34f246eb9acdcca2c1c4085800db6d74"
    assert atlas.release == "lean-v4.34.1-mathlib-v4.34.1"
    assert atlas.offered is True
    assert set(atlas.module_roots) == {
        "MathlibExt", "CSLibExt", "WantedExt", "MathlibExtTest", "CSLibExtTest", "Cslib", "CslibTests"
    }
    assert [entry["name"] for entry in atlas.manifest_packages] == ["atlas", "cslib"]


def test_every_entry_is_pinned_on_a_listed_release_and_locks_its_own_require() -> None:
    # `project new` reads the catalog as it is; `tests/test_project_create.py` checks what it writes from it.
    release_ids = {release.id for release in _RELEASES.releases}
    libraries = load_library_catalog().libraries
    assert len({library.name for library in libraries}) == len(libraries)
    for library in libraries:
        assert library.release in release_ids
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", library.name)
        assert re.fullmatch(r"[0-9a-f]{40}", library.commit)
        assert type(library.offered) is bool and library.module_roots
        assert all(re.fullmatch(r"[A-Z][A-Za-z0-9]*", root) for root in library.module_roots)
        assert len({root.casefold() for root in library.module_roots}) == len(library.module_roots)
        direct = [entry for entry in library.manifest_packages if not entry["inherited"]]
        assert direct == [entry for entry in library.manifest_packages if entry["name"] == library.name]
        assert direct[0]["url"] == library.git
        assert direct[0]["rev"] == direct[0]["inputRev"] == library.commit
        # Offline proxy for "every package its require brings in": the entries the
        # release does not already lock. Task 5's real build proves completeness.
        added = {entry["name"].casefold() for entry in library.manifest_packages}
        assert not added & _release_package_names(library.release)


def _mutated(path: tuple[object, ...], value: object) -> dict:
    payload = copy.deepcopy(_BUNDLED)
    target = payload
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return payload


_ATLAS = ("libraries", 0)


@pytest.mark.parametrize(
    "payload",
    [
        _mutated(("schema",), "autoform-project-library-catalog/v0"),
        _mutated(("libraries",), ["atlas"]),
        _mutated((*_ATLAS, "extra"), 1),
        _mutated((*_ATLAS, "offered"), KeyError),
    ],
)
def test_malformed_catalog_is_refused(payload: dict) -> None:
    with pytest.raises(ProjectCatalogError):
        parse_library_catalog(payload)


def test_bundled_catalog_uses_the_current_schema() -> None:
    assert _BUNDLED["schema"] == LIBRARY_CATALOG_SCHEMA
