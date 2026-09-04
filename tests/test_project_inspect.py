from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.project import (
    PROJECT_INSPECTION_SCHEMA,
    RELEASE_CATALOG_SCHEMA,
    inspect_project,
    load_release_catalog,
    parse_release_catalog,
)
from autoform_cli.project.catalog import ProjectCatalogError


def _project(tmp_path: Path, *, revision: str = "v4.32.2") -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        'version = "0.1.0"\n'
        'defaultTargets = ["Example"]\n\n'
        '[[require]]\nname = "mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
        f'rev = "{revision}"\n\n'
        '[[lean_lib]]\nname = "Example"\nsrcDir = "src"\n',
        encoding="utf-8",
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
    return root


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.removeprefix("v").split("."))


def test_release_catalog_is_canonical() -> None:
    catalog = load_release_catalog()
    assert catalog.schema == RELEASE_CATALOG_SCHEMA
    assert [release.id for release in catalog.releases] == sorted(
        release.id for release in catalog.releases
    )
    assert sum(release.recommended for release in catalog.releases) == 1
    assert catalog.to_json() == catalog.to_json()


def test_recommended_release_is_the_newest_stable_pair() -> None:
    catalog = load_release_catalog()
    stable = [release for release in catalog.releases if release.channel == "stable"]
    newest = max(stable, key=lambda release: _version(release.lean.version))
    assert catalog.recommended is newest
    assert _version(catalog.recommended.mathlib.revision) == _version(
        catalog.recommended.lean.version
    )


def test_recommended_release_matches_a_project_pinned_to_it(tmp_path: Path) -> None:
    recommended = load_release_catalog().recommended
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\n'
        f'git = "{recommended.mathlib.git}"\nrev = "{recommended.mathlib.revision}"\n',
        encoding="utf-8",
    )
    (root / "lean-toolchain").write_text(f"{recommended.lean.toolchain}\n", encoding="utf-8")

    result = inspect_project(root)
    assert result.ok
    assert result.compatibility.status == "supported"
    assert result.compatibility.release == recommended.id


def test_lean_4_33_0_is_supported_without_being_recommended(tmp_path: Path) -> None:
    catalog = load_release_catalog()
    release = next(
        release
        for release in catalog.releases
        if release.id == "lean-v4.33.0-mathlib-v4.33.0"
    )
    assert not release.recommended
    assert catalog.recommended.id == "lean-v4.33.1-mathlib-v4.33.1"

    root = _project(tmp_path, revision="v4.33.0")
    (root / "lean-toolchain").write_text(
        "leanprover/lean4:v4.33.0\n", encoding="utf-8"
    )

    result = inspect_project(root)
    assert result.ok
    assert result.compatibility.status == "supported"
    assert result.compatibility.release == release.id


def test_catalog_loader_converts_decode_and_recursion_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli.project import catalog as catalog_module

    class InvalidResource:
        def joinpath(self, _name: str):
            return self

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            raise UnicodeDecodeError("utf-8", b"x", 0, 1, "invalid")

    monkeypatch.setattr(catalog_module, "files", lambda _package: InvalidResource())
    with pytest.raises(ProjectCatalogError):
        load_release_catalog()

    monkeypatch.setattr(catalog_module, "files", lambda _package: type("R", (), {
        "joinpath": lambda self, _name: self,
        "read_text": lambda self, **_kwargs: "{}",
    })())
    monkeypatch.setattr(catalog_module.json, "loads", lambda _text: (_ for _ in ()).throw(RecursionError()))
    with pytest.raises(ProjectCatalogError):
        load_release_catalog()


def test_release_catalog_rejects_invalid_contract() -> None:
    with pytest.raises(ProjectCatalogError):
        parse_release_catalog({"schema": RELEASE_CATALOG_SCHEMA, "releases": []})
    with pytest.raises(ProjectCatalogError):
        parse_release_catalog(
            {
                "schema": RELEASE_CATALOG_SCHEMA,
                "releases": [
                    {
                        "id": "x",
                        "channel": "stable",
                        "recommended": "yes",
                        "lean": {"toolchain": "x", "version": "x"},
                        "mathlib": {"git": "x", "revision": "x"},
                    }
                ],
            }
        )


def test_inspects_bundled_example_without_host_paths(repo_root: Path) -> None:
    example = repo_root / "skills/setup/assets/cabannes-thesis-project"
    result = inspect_project(example)
    payload = result.as_dict()

    assert result.ok
    assert payload["schema"] == PROJECT_INSPECTION_SCHEMA
    assert payload["project_root"] == "."
    assert payload["lake"]["name"] == "CabannesThesis"
    assert payload["lake"]["targets"] == [
        {
            "kind": "lean_lib",
            "name": "CabannesThesis",
            "root": None,
            "roots": ["CabannesThesis"],
            "src_dir": "src",
        }
    ]
    assert payload["lean"]["version"] == "v4.32.2"
    assert payload["mathlib"]["revision"] == "v4.32.2"
    assert payload["compatibility"]["status"] == "supported"
    assert payload["autoform"]["detected"] is True
    assert str(repo_root) not in result.to_json()


def test_inspects_manifest_managed_blueprints_without_scanning_siblings(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "Blueprint/SyntheticHomotopy/roadmap").mkdir(parents=True)
    (root / "Blueprint/HartshorneCh2Sec5").mkdir()
    (root / ".autoform.toml").write_text(
        'schema = "autoform-workspace/v1"\n'
        "[locations.plans]\n"
        'path = "Blueprint"\n'
        'provides = ["blueprints"]\n'
        "[projects.synthetic-homotopy]\n"
        'blueprint = { location = "plans", path = "SyntheticHomotopy" }\n',
        encoding="utf-8",
    )

    result = inspect_project(root / "Blueprint/SyntheticHomotopy")

    assert result.ok
    assert result.autoform.detected
    assert result.autoform.manifest_path == ".autoform.toml"
    assert result.autoform.manifest_sha256 is not None
    assert result.autoform.blueprint_path is None
    assert result.autoform.blueprint_paths == ("Blueprint/SyntheticHomotopy",)
    assert "Blueprint/HartshorneCh2Sec5" not in result.to_json()
    assert not any(
        diagnostic.code == "autoform-mkdocs-missing" for diagnostic in result.diagnostics
    )


def test_inspects_unregistered_workspace_locations_for_path_safety(tmp_path: Path) -> None:
    root = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "Blueprint").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    (root / ".autoform.toml").write_text(
        'schema = "autoform-workspace/v1"\n'
        "[locations.plans]\n"
        'path = "Blueprint"\n'
        'provides = ["blueprints"]\n'
        "[projects]\n",
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert not result.ok
    assert any(
        diagnostic.code == "autoform-location-is-symlink"
        for diagnostic in result.diagnostics
    )


def test_workspace_manifest_ignores_unregistered_legacy_blueprint_file(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "Plans").mkdir()
    (root / "blueprint").write_text("unregistered sibling\n", encoding="utf-8")
    (root / ".autoform.toml").write_text(
        'schema = "autoform-workspace/v1"\n'
        '[locations.plans]\npath = "Plans"\nprovides = ["blueprints"]\n'
        '[projects]\n',
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert result.ok
    assert result.autoform.manifest_path == ".autoform.toml"
    assert result.autoform.blueprint_path is None
    assert not any(
        diagnostic.path == "blueprint" and diagnostic.severity == "error"
        for diagnostic in result.diagnostics
    )


def test_inspects_workspace_location_at_repository_root(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "Example/roadmap").mkdir(parents=True)
    (root / ".autoform.toml").write_text(
        'schema = "autoform-workspace/v1"\n'
        "[locations.root]\n"
        'path = "."\n'
        'provides = ["blueprints"]\n'
        "[projects.example]\n"
        'blueprint = { location = "root", path = "Example" }\n',
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert result.ok
    assert result.autoform.blueprint_paths == ("Example",)


def test_discovers_nearest_project_from_nested_file(tmp_path: Path) -> None:
    outer = _project(tmp_path)
    inner = outer / "nested"
    inner.mkdir()
    (inner / "lakefile.toml").write_text('name = "Inner"\n', encoding="utf-8")
    (inner / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
    source = inner / "src" / "Main.lean"
    source.parent.mkdir()
    source.write_text("theorem ok : True := by trivial\n", encoding="utf-8")

    result = inspect_project(source)
    assert result.project_root == "."
    assert result.lake is not None
    assert result.lake.name == "Inner"


def test_toml_depth_limit_is_independent_of_python_recursion_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    nested = "[" * 129 + "0" + "]" * 129
    (root / "lakefile.toml").write_text(
        f'name = "Example"\nvalue = {nested}\n', encoding="utf-8"
    )
    monkeypatch.setattr(sys, "getrecursionlimit", lambda: 10_000)
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-toml" for diagnostic in result.diagnostics)


def test_deep_toml_is_a_path_free_failure(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\nvalue = ' + "[" * 1500 + "0" + "]" * 1500 + "\n",
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-toml" for diagnostic in result.diagnostics)
    assert str(tmp_path) not in result.to_json()


def test_dotted_table_header_depth_is_bounded(tmp_path: Path) -> None:
    root = _project(tmp_path)
    header = "[" + ".".join(["a"] * 200) + "]"
    (root / "lakefile.toml").write_text(
        f'name = "Example"\n{header}\nvalue = 0\n', encoding="utf-8"
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-toml" for diagnostic in result.diagnostics)


def test_dotted_keys_within_the_limit_still_parse(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        "[leanOptions]\n"
        "weak.linter.mathlibStandardSet = true\n"
        '# a.b.c.d.e comment must not leak into the next line\n'
        "pp.unicode.fun = true\n",
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None and result.lake.name == "Example"


def test_multiline_string_terminator_cannot_hide_excessive_toml_depth(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    nested = "[" * 130 + "0" + "]" * 130
    (root / "lakefile.toml").write_text(
        'name = "Example"\nx = """abc""""\ny = ' + nested + "\n",
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-toml" for diagnostic in result.diagnostics)


def test_malformed_toml_is_a_path_free_failure(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text("name = [\n", encoding="utf-8")

    result = inspect_project(root)
    assert not result.ok
    assert [diagnostic.code for diagnostic in result.diagnostics if diagnostic.severity == "error"] == [
        "invalid-lake-toml"
    ]
    assert str(tmp_path) not in result.to_json()


@pytest.mark.parametrize(
    "src_dir",
    [
        "../outside",
        "/absolute",
        "C:\\outside",
        "..\\outside",
        "foo\\..\\outside",
        "C:outside",
        "\\outside",
    ],
)
def test_rejects_nonportable_lake_paths(tmp_path: Path, src_dir: str) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        f'name = "Example"\n[[lean_lib]]\nname = "Example"\nsrcDir = "{src_dir.replace(chr(92), chr(92) * 2)}"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "nonportable-lake-path" for diagnostic in result.diagnostics)


def test_parses_library_roots_and_executable_root(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\nsrcDir = "pkg"\n'
        '[[lean_lib]]\nname = "Library"\nroots = ["A", "B.C"]\nsrcDir = "lib"\n'
        '[[lean_exe]]\nname = "Runner"\nroot = "Main"\nsrcDir = "exe"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None
    assert result.lake.targets[0].roots == ("A", "B.C")
    assert result.lake.targets[0].root is None
    assert result.lake.targets[1].root == "Main"
    assert result.lake.targets[1].roots == ()


def test_default_target_roots_are_effective(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[lean_lib]]\nname = "Library"\n'
        '[[lean_exe]]\nname = "Runner"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None
    assert result.lake.targets[0].roots == ("Library",)
    assert result.lake.targets[1].root == "Runner"


def test_noncanonical_target_name_uses_lake_simple_name_fallback(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[lean_lib]]\nname = "my-module"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None
    assert result.lake.targets[0].roots == ("«my-module»",)
    assert not any(
        diagnostic.code == "lake-target-names-indeterminate"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize(
    ("name", "root"),
    [
        ("foo«", "«foo«»"),
        ("foo»", "foo»"),
        ("#foo", "#foo"),
        ("?foo", "?foo"),
        ("«#foo».«my-module»", "#foo.my-module"),
    ],
)
def test_target_name_rendering_matches_lake_escape_fallback(
    tmp_path: Path, name: str, root: str
) -> None:
    project = _project(tmp_path)
    (project / "lakefile.toml").write_text(
        f'name = "Example"\n[[lean_lib]]\nname = "{name}"\n', encoding="utf-8"
    )

    result = inspect_project(project)

    assert result.ok
    assert result.lake is not None
    assert result.lake.targets[0].roots == (root,)


@pytest.mark.parametrize(
    "version", ["wat", "v1.2.3", "1.2", "1.2.3.4", "1.2.3+build", "١.٢.٣"]
)
def test_invalid_lake_versions_are_rejected(tmp_path: Path, version: str) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        f'name = "Example"\nversion = "{version}"\n', encoding="utf-8"
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_prerelease_lake_version_is_accepted(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\nversion = "1.2.3-rc1"\n', encoding="utf-8"
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None and result.lake.version == "1.2.3-rc1"


def test_numeric_roots_are_canonicalized_before_duplicate_detection(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[lean_exe]]\nname = "First"\nroot = "01"\n'
        '[[lean_exe]]\nname = "Second"\nroot = "1"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        ('[[lean_lib]]\nname = "{numeric}"\n', ("1",)),
        ('[[lean_exe]]\nname = "Runner"\nroot = "{numeric}"\n', "1"),
    ],
)
def test_large_numeric_lean_names_are_normalized_lexically(
    tmp_path: Path, declaration: str, expected: str | tuple[str, ...]
) -> None:
    root = _project(tmp_path)
    numeric = "0" * 4_999 + "1"
    (root / "lakefile.toml").write_text(
        'name = "Example"\n' + declaration.format(numeric=numeric), encoding="utf-8"
    )

    result = inspect_project(root)

    assert result.ok
    assert result.lake is not None
    target = result.lake.targets[0]
    assert (target.roots if target.kind == "lean_lib" else target.root) == expected


def test_letter_like_unicode_target_names_are_canonical(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[lean_lib]]\nname = "Ω"\nroots = ["Ω.x₁", "α"]\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None
    assert result.lake.targets[0].roots == ("Ω.x₁", "α")
    assert not any(
        diagnostic.code == "lake-target-names-indeterminate"
        for diagnostic in result.diagnostics
    )


def test_duplicate_target_names_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[lean_lib]]\nname = "Duplicate"\n'
        '[[lean_exe]]\nname = "Duplicate"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_duplicate_simple_fallback_target_names_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[lean_lib]]\nname = "my-module"\n'
        '[[lean_exe]]\nname = "«my-module»"\n',
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_duplicate_executable_roots_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[lean_exe]]\nname = "First"\nroot = "Main"\n'
        '[[lean_exe]]\nname = "Second"\nroot = "Main"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_duplicate_mathlib_requirements_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    lakefile = root / "lakefile.toml"
    lakefile.write_text(
        lakefile.read_text(encoding="utf-8")
        + '\n[[require]]\nname = "mathlib"\ngit = "https://example.com/mathlib4.git"\nrev = "v4.32.2"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(
        diagnostic.code == "duplicate-mathlib-requirement"
        for diagnostic in result.diagnostics
    )


def test_escaped_mathlib_requirement_matches_catalog(tmp_path: Path) -> None:
    recommended = load_release_catalog().recommended
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "«mathlib»"\n'
        f'git = "{recommended.mathlib.git}"\n'
        f'rev = "{recommended.mathlib.revision}"\n',
        encoding="utf-8",
    )
    (root / "lean-toolchain").write_text(
        f"{recommended.lean.toolchain}\n", encoding="utf-8"
    )

    result = inspect_project(root)

    assert result.ok
    assert result.mathlib is not None
    assert result.compatibility.release == recommended.id


def test_equivalent_mathlib_requirement_spellings_are_duplicates(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        '[[require]]\nname = "mathlib"\nrev = "v4.33.1"\n'
        '[[require]]\nname = "«mathlib»"\nrev = "v4.33.1"\n',
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert not result.ok
    assert any(
        diagnostic.code == "duplicate-mathlib-requirement"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize(
    "requirement",
    [
        'name = "mathlib"\ngit = "https://github.com/leanprover-community/mathlib4.git"\nscope = "leanprover-community"\nrev = "v4.32.2"',
        'name = "mathlib"\ngit = { url = "https://github.com/leanprover-community/mathlib4.git" }\nrev = "v4.32.2"',
    ],
)
def test_supported_mathlib_dependency_forms_match_catalog(
    tmp_path: Path, requirement: str
) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        f'name = "Example"\n[[require]]\n{requirement}\n', encoding="utf-8"
    )
    result = inspect_project(root)
    assert result.ok
    assert result.compatibility.status == "supported"


def test_lake_generated_scope_requirement_matches_catalog(tmp_path: Path) -> None:
    """The exact `lake new <pkg> math` lakefile.toml: a scope, no `git` field."""
    recommended = load_release_catalog().recommended
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text(
        'name = "Example"\n'
        'version = "0.1.0"\n'
        'keywords = ["math"]\n'
        'defaultTargets = ["Example"]\n\n'
        "[leanOptions]\n"
        "pp.unicode.fun = true\n"
        "relaxedAutoImplicit = false\n"
        "weak.linter.mathlibStandardSet = true\n"
        "maxSynthPendingDepth = 3\n\n"
        "[[require]]\n"
        'name = "mathlib"\n'
        'scope = "leanprover-community"\n'
        f'rev = "{recommended.mathlib.revision}"\n\n'
        "[[lean_lib]]\n"
        'name = "Example"\n',
        encoding="utf-8",
    )
    (root / "lean-toolchain").write_text(f"{recommended.lean.toolchain}\n", encoding="utf-8")

    result = inspect_project(root)
    assert result.ok
    assert result.mathlib is not None
    assert result.mathlib.git == recommended.mathlib.git
    assert result.compatibility.status == "supported"
    assert result.compatibility.release == recommended.id


def test_unusable_mathlib_scope_is_valid_but_indeterminate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\n'
        'scope = "not a scope/../elsewhere"\nrev = "v4.32.2"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"


@pytest.mark.parametrize(
    "source",
    [
        'source = { type = "path", dir = "vendor/mathlib" }',
        'source = { type = "git", url = "https://github.com/leanprover-community/mathlib4.git", rev = "v4.32.2" }',
    ],
)
def test_generic_mathlib_sources_are_valid_but_indeterminate(
    tmp_path: Path, source: str
) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        f'name = "Example"\n[[require]]\nname = "mathlib"\n{source}\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"


def test_mathlib_git_subdirectory_is_valid_but_indeterminate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
        'rev = "v4.32.2"\nsubDir = "Mathlib"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"


def test_malformed_requirements_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\nrequire = ["mathlib"]\n', encoding="utf-8"
    )
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_missing_package_name_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text('version = "0.1.0"\n', encoding="utf-8")
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "invalid-lake-field" for diagnostic in result.diagnostics)


def test_path_precedes_mathlib_git_and_is_indeterminate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\n[[require]]\nname = "mathlib"\n'
        'path = "vendor/mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\n'
        'rev = "v4.32.2"\n',
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"


def test_credentialed_mathlib_url_is_rejected_and_redacted(tmp_path: Path) -> None:
    root = _project(tmp_path)
    lakefile = root / "lakefile.toml"
    lakefile.write_text(
        lakefile.read_text(encoding="utf-8").replace(
            "https://github.com/", "https://secret@example.com/"
        ),
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert result.mathlib is None
    assert "secret" not in result.to_json()
    assert any(diagnostic.code == "credentialed-mathlib-url" for diagnostic in result.diagnostics)


@pytest.mark.parametrize(
    "git_source, secret",
    [
        ("/private/home/project/mathlib", "/private/home"),
        ("https://github.com:bad/mathlib4.git", "github.com:bad"),
        ("https://github.com/mathlib4.git?token=secret", "token=secret"),
    ],
)
def test_invalid_mathlib_sources_are_rejected_and_redacted(
    tmp_path: Path, git_source: str, secret: str
) -> None:
    root = _project(tmp_path)
    lakefile = root / "lakefile.toml"
    lakefile.write_text(
        lakefile.read_text(encoding="utf-8").replace(
            "https://github.com/leanprover-community/mathlib4.git", git_source
        ),
        encoding="utf-8",
    )
    result = inspect_project(root)
    assert not result.ok
    assert result.mathlib is None
    assert secret not in result.to_json()
    assert any(diagnostic.code == "invalid-mathlib-url" for diagnostic in result.diagnostics)


def test_unlisted_release_is_advisory(tmp_path: Path) -> None:
    root = _project(tmp_path, revision="v4.31.0")
    result = inspect_project(root)
    assert result.ok
    assert result.compatibility.status == "unlisted"
    assert any(diagnostic.code == "release-unlisted" for diagnostic in result.diagnostics)


def test_copied_projects_have_identical_json(tmp_path: Path) -> None:
    first = _project(tmp_path)
    second = tmp_path / "copy"
    shutil.copytree(first, second)
    assert inspect_project(first).to_json() == inspect_project(second).to_json()


def test_deep_lake_manifest_is_a_stable_warning(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lake-manifest.json").write_text(
        "[" * 1500 + "0" + "]" * 1500, encoding="utf-8"
    )
    result = inspect_project(root)
    assert result.ok
    assert any(diagnostic.code == "invalid-lake-manifest" for diagnostic in result.diagnostics)
    assert str(tmp_path) not in result.to_json()


def test_human_output_composes_package_and_target_source_dirs(
    tmp_path: Path, capsys
) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").write_text(
        'name = "Example"\nsrcDir = "pkg"\n'
        '[[lean_lib]]\nname = "Library"\nroots = ["A"]\nsrcDir = "lib"\n',
        encoding="utf-8",
    )
    assert main(["project", "inspect", str(root)]) == 0
    captured = capsys.readouterr()
    assert "srcDir: pkg/lib, roots: A" in captured.out


def test_lakefile_lean_is_never_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    marker = tmp_path / "executed"
    (root / "lakefile.lean").write_text(
        f'unsafe def attempt := IO.FS.writeFile "{marker}" "bad"\n', encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("offline inspection invoked a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    result = inspect_project(root)
    assert result.ok
    assert result.lake is not None and result.lake.format == "lean"
    assert result.compatibility.status == "indeterminate"
    assert not marker.exists()


def test_fifo_lakefile_fails_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable")
    root = _project(tmp_path)
    (root / "lakefile.toml").unlink()
    os.mkfifo(root / "lakefile.toml")
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "lake-config-not-regular" for diagnostic in result.diagnostics)


def test_rejects_broken_symlinked_lakefile(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.toml").unlink()
    try:
        (root / "lakefile.toml").symlink_to(root / "missing.toml")
    except OSError:
        pytest.skip("symlinks are unavailable")
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "lake-config-is-symlink" for diagnostic in result.diagnostics)


def test_rejects_symlinked_decision_files(tmp_path: Path) -> None:
    root = _project(tmp_path)
    real = root / "real-toolchain"
    real.write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
    (root / "lean-toolchain").unlink()
    try:
        (root / "lean-toolchain").symlink_to(real)
    except OSError:
        pytest.skip("symlinks are unavailable")

    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "lean-toolchain-is-symlink" for diagnostic in result.diagnostics)


def test_rejects_symlinked_project_root(tmp_path: Path) -> None:
    root = _project(tmp_path)
    link = tmp_path / "project-link"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    result = inspect_project(link / "lakefile.toml")
    assert not result.ok
    assert any(diagnostic.code == "project-path-is-symlink" for diagnostic in result.diagnostics)


def test_rejects_target_below_symlinked_directory(tmp_path: Path) -> None:
    root = _project(tmp_path)
    real = root / "real-src"
    real.mkdir()
    source = real / "Main.lean"
    source.write_text("theorem ok : True := by trivial\n", encoding="utf-8")
    link = root / "src"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    result = inspect_project(link / "Main.lean")
    assert not result.ok
    assert any(diagnostic.code == "project-path-is-symlink" for diagnostic in result.diagnostics)


def test_json_catalog_failure_is_machine_readable(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import __main__ as cli
    from autoform_cli.project.catalog import ProjectCatalogError

    def invalid_catalog():
        raise ProjectCatalogError("internal details")

    monkeypatch.setattr(cli, "load_release_catalog", invalid_catalog)
    assert main(["project", "versions", "--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "error": {
            "code": "project-catalog-invalid",
            "message": "The bundled project release catalog is invalid.",
        },
        "ok": False,
    }
    assert captured.err == ""


def test_cli_outputs_stable_json_and_failures(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    assert main(["project", "inspect", str(root), "--json"]) == 0
    first = capsys.readouterr()
    assert json.loads(first.out)["ok"] is True
    assert first.err == ""

    assert main(["project", "versions", "--json"]) == 0
    versions = capsys.readouterr()
    assert json.loads(versions.out)["schema"] == RELEASE_CATALOG_SCHEMA
    assert versions.err == ""

    assert main(["project", "inspect", str(root / "missing"), "--json"]) == 1
    failure = capsys.readouterr()
    assert json.loads(failure.out)["diagnostics"][0]["code"] == "target-does-not-exist"
    assert failure.err == ""


def test_rejects_symlinked_scaffold_parent(tmp_path: Path) -> None:
    root = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "workflows").mkdir()
    github = root / ".github"
    try:
        github.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    result = inspect_project(root)
    assert not result.ok
    assert any(diagnostic.code == "scaffold-path-is-symlink" for diagnostic in result.diagnostics)


@pytest.mark.parametrize(
    "relative, node",
    [("blueprint", "file"), ("mkdocs.yml", "directory")],
)
def test_scaffold_paths_require_their_expected_node_type(
    tmp_path: Path, relative: str, node: str
) -> None:
    root = _project(tmp_path)
    if node == "file":
        (root / relative).write_text("not a scaffold\n", encoding="utf-8")
    else:
        (root / relative).mkdir()

    result = inspect_project(root)
    assert not result.ok
    assert result.autoform.detected is False
    assert any(
        diagnostic.code == "scaffold-path-unexpected-type" and diagnostic.path == relative
        for diagnostic in result.diagnostics
    )


def test_root_discovery_stays_bound_to_the_directory_it_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing the discovered root with a symlink must not redirect inspection."""
    from autoform_cli.project import inspect as inspect_module

    root = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "lakefile.toml").write_text('name = "Outside"\n', encoding="utf-8")
    (outside / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
    swapped = False

    def swap() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        root.rename(tmp_path / "moved")
        try:
            root.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable")

    original_status = inspect_module._relative_status
    original_resolve = Path.resolve

    def swapping_status(descriptor: int, relative: str) -> str:
        status = original_status(descriptor, relative)
        swap()
        return status

    def swapping_resolve(self: Path, *args, **kwargs) -> Path:
        if self == root:
            swap()
        return original_resolve(self, *args, **kwargs)

    # Whichever step discovery reaches first performs the swap: canonicalizing a
    # pathname after checking it would hand back the outside project instead.
    monkeypatch.setattr(inspect_module, "_relative_status", swapping_status)
    monkeypatch.setattr(Path, "resolve", swapping_resolve)
    result = inspect_project(root, catalog=load_release_catalog())
    assert swapped
    assert result.ok
    assert result.lake is not None and result.lake.name == "Example"


def test_root_discovery_resolves_parent_components_from_retained_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli.project import inspect as inspect_module

    base = tmp_path / "base"
    base.mkdir()
    _project(base)
    child = base / "child"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_root = _project(outside)
    (outside_root / "lakefile.toml").write_text('name = "Outside"\n', encoding="utf-8")
    swapped = False
    original_open = inspect_module._open_directory

    def moving_open(name: str, parent_descriptor: int | None) -> int:
        nonlocal swapped
        descriptor = original_open(name, parent_descriptor)
        if name == "child" and not swapped:
            swapped = True
            child.rename(outside / "child")
        return descriptor

    monkeypatch.setattr(inspect_module, "_open_directory", moving_open)
    result = inspect_project(child / ".." / "project")

    assert swapped
    assert result.ok
    assert result.lake is not None and result.lake.name == "Example"


def test_non_ascii_digits_do_not_match_stable_toolchain_versions(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lean-toolchain").write_text(
        "leanprover/lean4:v١.٢.٣\n", encoding="utf-8"
    )

    result = inspect_project(root)

    assert result.ok
    assert result.lean is not None and result.lean.version is None
    assert any(
        diagnostic.code == "unrecognized-lean-toolchain"
        for diagnostic in result.diagnostics
    )


def test_tilde_expansion_failure_is_a_stable_diagnostic() -> None:
    result = inspect_project("~autoform-user-that-does-not-exist/project")
    assert not result.ok
    assert result.diagnostics[0].code == "target-unreadable"


def test_reports_git_metadata_without_invoking_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / ".git").mkdir()

    def forbidden(*args, **kwargs):
        raise AssertionError("offline inspection invoked a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    result = inspect_project(root)
    assert result.git_path == ".git"


def test_claim_help_describes_git_refs(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit):
        main(["--help"])
    captured = capsys.readouterr()
    assert "coordinate temporary article and resource ownership through Git refs" in captured.out


def test_inspection_does_not_write_project(tmp_path: Path) -> None:
    root = _project(tmp_path)
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    inspect_project(root)
    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (root / ".lake").exists()
    assert not (root / ".git").exists()
