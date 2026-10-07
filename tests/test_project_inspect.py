from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import autoform_cli.project.inspect as project_inspect
import autoform_cli.project._snapshot as project_snapshot
from autoform_cli.__main__ import _human_text, main
from autoform_cli.project import (
    PROJECT_INSPECTION_SCHEMA,
    RELEASE_CATALOG_SCHEMA,
    ProjectCatalogError,
    inspect_project,
    load_release_catalog,
    parse_release_catalog,
)

MATHLIB_URL = "https://github.com/leanprover-community/mathlib4"
COMMIT = "905b95818eb32af7874a58b427f50c1711a5e96c"
OTHER_COMMIT = "2" * 40
LAKEFILE = (
    'name = "Example"\nversion = "0.1.0"\n\n'
    '[[require]]\nname = "mathlib"\nscope = "leanprover-community"\nrev = "v4.32.2"\n\n'
    '[[lean_lib]]\nname = "Example"\n\n'
    '[[lean_exe]]\nname = "example"\n'
)


def _mathlib(rev: str = COMMIT, input_rev: str = "v4.32.2", url: str = MATHLIB_URL, **fields: object) -> dict:
    """A Mathlib entry spelled the way Lake 4.32 writes it."""

    entry = {
        "url": url,
        "type": "git",
        "subDir": None,
        "scope": "",
        "rev": rev,
        "name": "mathlib",
        "manifestFile": "lake-manifest.json",
        "inputRev": input_rev,
        "inherited": False,
        "configFile": "lakefile.lean",
    }
    return {**entry, **fields}


def _write_manifest(root: Path, *packages: dict, version: object = "1.1.0") -> None:
    (root / "lake-manifest.json").write_text(
        json.dumps({"version": version, "packagesDir": ".lake/packages", "packages": list(packages)}),
        encoding="utf-8",
    )


def _project(
    tmp_path: Path,
    *,
    lakefile: str | None = LAKEFILE,
    toolchain: str | None = "leanprover/lean4:v4.32.2\n",
    manifest: tuple[dict, ...] | None = (_mathlib(),),
) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    if lakefile is not None:
        (root / "lakefile.toml").write_text(lakefile, encoding="utf-8")
    if toolchain is not None:
        (root / "lean-toolchain").write_text(toolchain, encoding="utf-8")
    if manifest is not None:
        _write_manifest(root, *manifest)
    return root


def _codes(result) -> set[str]:
    return {diagnostic.code for diagnostic in result.diagnostics}


def _unreadable(result) -> list[str]:
    return [diagnostic.path for diagnostic in result.diagnostics if diagnostic.code == "unreadable-file"]


def test_catalog_pair_from_lakes_math_template_is_supported(tmp_path: Path) -> None:
    result = inspect_project(_project(tmp_path))

    assert result.ok
    assert result.compatibility.status == "supported"
    assert result.compatibility.release == "lean-v4.32.2-mathlib-v4.32.2"
    assert result.lake.name == "Example"
    assert [(target.kind, target.name) for target in result.lake.targets] == [
        ("lean_lib", "Example"),
        ("lean_exe", "example"),
    ]
    assert result.lean_toolchain == "leanprover/lean4:v4.32.2"
    assert result.mathlib.rev == COMMIT
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "toolchain",
    ["v4.32.2\n", "4.32.2\n", "leanprover/lean4:4.32.2\n"],
)
def test_elan_release_aliases_match_the_catalog(tmp_path: Path, toolchain: str) -> None:
    result = inspect_project(_project(tmp_path, toolchain=toolchain))

    assert result.compatibility.status == "supported"
    assert result.compatibility.release == "lean-v4.32.2-mathlib-v4.32.2"


def test_git_url_scheme_and_host_case_do_not_change_the_repository(tmp_path: Path) -> None:
    result = inspect_project(
        _project(tmp_path, manifest=(_mathlib(url="HTTPS://GITHUB.COM/leanprover-community/mathlib4"),))
    )

    assert result.compatibility.status == "supported"
    assert result.compatibility.release == "lean-v4.32.2-mathlib-v4.32.2"


def test_json_report_has_a_stable_shape(tmp_path: Path) -> None:
    payload = json.loads(inspect_project(_project(tmp_path)).to_json())

    assert payload["schema"] == PROJECT_INSPECTION_SCHEMA
    assert payload["ok"] is True
    assert sorted(payload) == [
        "autoform_paths",
        "compatibility",
        "diagnostics",
        "lake",
        "lean_toolchain",
        "mathlib",
        "ok",
        "project_root",
        "schema",
    ]
    assert payload["compatibility"] == {
        "recommended_release": "lean-v4.32.2-mathlib-v4.32.2",
        "release": "lean-v4.32.2-mathlib-v4.32.2",
        "status": "supported",
    }


@pytest.mark.parametrize("url", [MATHLIB_URL + ".git", MATHLIB_URL + "/"])
def test_equivalent_mathlib_url_spellings_match(tmp_path: Path, url: str) -> None:
    result = inspect_project(_project(tmp_path, manifest=(_mathlib(url=url),)))

    assert result.compatibility.status == "supported"


def test_fork_with_the_catalog_commit_is_unlisted(tmp_path: Path) -> None:
    fork = _mathlib(url="https://github.com/someone/mathlib4")
    result = inspect_project(_project(tmp_path, manifest=(fork,)))

    assert result.compatibility.status == "unlisted"


def test_other_toolchain_is_unlisted(tmp_path: Path) -> None:
    result = inspect_project(_project(tmp_path, toolchain="leanprover/lean4:v4.33.0\n"))

    assert result.ok
    assert result.compatibility.status == "unlisted"
    assert "release-unlisted" in _codes(result)


def test_manifest_lock_decides_when_the_lakefile_moved_ahead(tmp_path: Path) -> None:
    # The lakefile asks for v4.32.2 but the manifest still locks v4.31.0; Lake builds the lock.
    old = _mathlib(rev=OTHER_COMMIT, input_rev="v4.31.0")
    lakefile = LAKEFILE.replace('scope = "leanprover-community"', f'git = "{MATHLIB_URL}"')
    result = inspect_project(_project(tmp_path, lakefile=lakefile, manifest=(old,)))

    assert result.compatibility.status == "unlisted"
    assert "lake-manifest-stale" in _codes(result)


def test_stale_requirement_still_reports_the_locked_catalog_pair(tmp_path: Path) -> None:
    lakefile = LAKEFILE.replace(
        'scope = "leanprover-community"\nrev = "v4.32.2"',
        f'git = "{MATHLIB_URL}"\nrev = "v4.31.0"',
    )
    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert result.compatibility.status == "supported"
    assert "lake-manifest-stale" in _codes(result)


def test_requirement_git_url_is_compared_with_the_lock(tmp_path: Path) -> None:
    lakefile = LAKEFILE.replace('scope = "leanprover-community"', 'git = "https://github.com/someone/mathlib4"')
    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert "lake-manifest-stale" in _codes(result)


def test_explicit_git_requirement_without_a_revision_is_compared_with_the_lock(tmp_path: Path) -> None:
    lakefile = LAKEFILE.replace(
        'scope = "leanprover-community"\nrev = "v4.32.2"',
        f'git = "{MATHLIB_URL}"',
    )
    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert "lake-manifest-stale" in _codes(result)


def test_source_less_requirement_is_not_compared_with_the_lock(tmp_path: Path) -> None:
    lakefile = 'name = "Example"\n\n[[require]]\nname = "mathlib"\nrev = "v4.31.0"\n'
    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert result.compatibility.status == "supported"
    assert "lake-manifest-stale" not in _codes(result)


def test_last_duplicate_manifest_entry_wins(tmp_path: Path) -> None:
    manifest = (_mathlib(rev=OTHER_COMMIT), _mathlib())
    result = inspect_project(_project(tmp_path, manifest=manifest))

    assert result.compatibility.status == "supported"


LOOM_LAKEFILE = 'name = "Example"\n\n[[require]]\nname = "loom"\ngit = "https://example.com/loom"\n'
LOOM = _mathlib(name="loom", url="https://example.com/loom", rev=OTHER_COMMIT, input_rev=None)


def _assert_mathlib_unused(result) -> None:
    """A locked Mathlib that no requirement pulls in decides no release pair."""

    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert "mathlib-manifest-unused" in _codes(result)


def test_inherited_mathlib_under_other_requirements_is_not_proof_of_use(tmp_path: Path) -> None:
    inherited = {**_mathlib(), "inherited": True}
    result = inspect_project(_project(tmp_path, lakefile=LOOM_LAKEFILE, manifest=(LOOM, inherited)))

    _assert_mathlib_unused(result)


@pytest.mark.skipif(shutil.which("lake") is None, reason="needs Lake 4.32.2")
def test_stale_inherited_mathlib_is_ignored_by_real_lake(tmp_path: Path) -> None:
    """A Lake-generated inherited entry can outlive the dependency that required it."""

    dependency = tmp_path / "dep"
    dependency.mkdir()
    (dependency / "lakefile.toml").write_text(
        'name = "dep"\nversion = "0.1.0"\n\n[[lean_lib]]\nname = "Dep"\n', encoding="utf-8"
    )
    (dependency / "Dep.lean").write_text("def depMarker : Nat := 41\n", encoding="utf-8")

    root = tmp_path / "root"
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
    (root / "lakefile.toml").write_text(
        'name = "root"\nversion = "0.1.0"\n\n'
        '[[require]]\nname = "dep"\npath = "../dep"\n\n'
        '[[lean_lib]]\nname = "Root"\n',
        encoding="utf-8",
    )
    (root / "Root.lean").write_text("import Dep\n\ndef rootMarker : Nat := depMarker\n", encoding="utf-8")
    _write_manifest(
        root,
        {
            "type": "path",
            "scope": "",
            "name": "dep",
            "manifestFile": "lake-manifest.json",
            "inherited": False,
            "dir": "../dep",
            "configFile": "lakefile.toml",
        },
        {
            "type": "path",
            "scope": "",
            "name": "mathlib",
            "manifestFile": "lake-manifest.json",
            "inherited": True,
            "dir": "../dep/../mathlib",
            "configFile": "lakefile.toml",
        },
        version="1.2.0",
    )
    manifest_before = (root / "lake-manifest.json").read_bytes()

    build = subprocess.run(
        ["lake", "build", "Root"], cwd=root, capture_output=True, text=True, timeout=120, check=False
    )

    assert build.returncode == 0, build.stdout + build.stderr
    assert not (tmp_path / "mathlib").exists()
    assert (root / "lake-manifest.json").read_bytes() == manifest_before
    inspection = inspect_project(root)
    _assert_mathlib_unused(inspection)


def test_direct_lock_without_a_requirement_is_unused(tmp_path: Path) -> None:
    result = inspect_project(_project(tmp_path, lakefile=LOOM_LAKEFILE, manifest=(LOOM, _mathlib())))

    _assert_mathlib_unused(result)
    (unused,) = [d for d in result.diagnostics if d.code == "mathlib-manifest-unused"]
    assert "does not build" not in unused.message and "dependency" in unused.message


@pytest.mark.parametrize("inherited", [False, True])
def test_lock_without_any_requirement_is_unused(tmp_path: Path, inherited: bool) -> None:
    manifest = ({**_mathlib(), "inherited": inherited},)
    result = inspect_project(_project(tmp_path, lakefile='name = "Example"\n', manifest=manifest))

    _assert_mathlib_unused(result)


def test_root_named_mathlib_also_satisfies_transitive_requirements(tmp_path: Path) -> None:
    inherited = {**_mathlib(), "inherited": True}
    lakefile = LOOM_LAKEFILE.replace('"Example"', '"mathlib"')
    result = inspect_project(_project(tmp_path, lakefile=lakefile, manifest=(LOOM, inherited)))

    _assert_mathlib_unused(result)


def test_self_requirement_alone_pulls_in_no_mathlib(tmp_path: Path) -> None:
    inherited = {**_mathlib(), "inherited": True}
    lakefile = 'name = "selfy"\n\n[[require]]\nname = "selfy"\npath = "."\n'
    result = inspect_project(_project(tmp_path, lakefile=lakefile, manifest=(LOOM, inherited)))

    _assert_mathlib_unused(result)


def test_inherited_override_does_not_make_an_unrecorded_mathlib_used(tmp_path: Path) -> None:
    root = _project(tmp_path, lakefile=LOOM_LAKEFILE, manifest=(LOOM,))
    _write_overrides(root, {**_mathlib(), "inherited": True})

    result = inspect_project(root)

    _assert_mathlib_unused(result)


@pytest.mark.parametrize("override_inherited", [False, True])
def test_override_of_an_inherited_lock_does_not_prove_use(tmp_path: Path, override_inherited: bool) -> None:
    root = _project(tmp_path, lakefile=LOOM_LAKEFILE, manifest=(LOOM, _mathlib(rev=OTHER_COMMIT, inherited=True)))
    _write_overrides(root, {**_mathlib(), "inherited": override_inherited})

    result = inspect_project(root)

    _assert_mathlib_unused(result)


def _write_overrides(root: Path, *packages: dict, schema_version: object = "1.1.0") -> None:
    (root / ".lake").mkdir()
    (root / ".lake/package-overrides.json").write_text(
        json.dumps({"schemaVersion": schema_version, "packages": list(packages)}), encoding="utf-8"
    )


def test_requirement_the_manifest_does_not_record_is_an_error(tmp_path: Path) -> None:
    lakefile = LAKEFILE + '\n[[require]]\nname = "batteries"\nscope = "leanprover-community"\n'
    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert "lake-manifest-incomplete" in _codes(result)


@pytest.mark.parametrize(
    "lakefile", [LAKEFILE, 'name = "mathlib"\n\n[[require]]\nname = "mathlib"\n'], ids=["mathlib", "root-named"]
)
def test_requirements_against_a_manifest_without_packages_are_an_error(tmp_path: Path, lakefile: str) -> None:
    result = inspect_project(_project(tmp_path, lakefile=lakefile, manifest=()))

    assert not result.ok
    assert "lake-manifest-incomplete" in _codes(result)


def test_requirement_recorded_only_by_an_override_resolves(tmp_path: Path) -> None:
    root = _project(tmp_path, manifest=())
    _write_overrides(root, _mathlib())

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "supported"
    assert "mathlib-overridden" in _codes(result)


def test_requirement_of_the_roots_own_name_is_not_looked_up(tmp_path: Path) -> None:
    root = _project(tmp_path, lakefile='name = "mathlib"\n\n[[require]]\nname = "mathlib"\n', manifest=())
    _write_overrides(root, _mathlib(name="loom"))

    result = inspect_project(root)

    assert result.ok
    assert "lake-manifest-incomplete" not in _codes(result)


@pytest.mark.parametrize(
    "fields",
    [{"subDir": "Archive"}, {"configFile": "alternate.lean"}, {"manifestFile": "other.json"}],
)
def test_lock_must_load_mathlib_the_way_releases_do(tmp_path: Path, fields: dict) -> None:
    result = inspect_project(_project(tmp_path, manifest=(_mathlib(**fields),)))

    assert result.compatibility.status == "unlisted"


@pytest.mark.parametrize("config_file", [None, "lakefile"])
def test_lakes_default_extensionless_config_resolves_to_mathlibs_lakefile_lean(
    tmp_path: Path, config_file: object
) -> None:
    result = inspect_project(_project(tmp_path, manifest=(_mathlib(configFile=config_file),)))

    assert result.ok
    assert result.mathlib.config_file == "lakefile"
    assert result.compatibility.status == "supported"


def test_lakes_explicit_current_directory_subdir_is_the_repository_root(tmp_path: Path) -> None:
    result = inspect_project(_project(tmp_path, manifest=(_mathlib(subDir="./"),)))

    assert result.compatibility.status == "supported"
    assert result.compatibility.release == "lean-v4.32.2-mathlib-v4.32.2"


def test_uppercase_commit_is_the_same_commit(tmp_path: Path) -> None:
    result = inspect_project(_project(tmp_path, manifest=(_mathlib(rev=COMMIT.upper()),)))

    assert result.compatibility.status == "supported"


def test_url_credentials_are_redacted_and_never_match(tmp_path: Path, capsys) -> None:
    secret = "https://user:hunter2@github.com/leanprover-community/mathlib4"
    lakefile = LAKEFILE.replace('scope = "leanprover-community"', f'git = "{secret}"')
    root = _project(tmp_path, lakefile=lakefile, manifest=(_mathlib(url=secret),))

    result = inspect_project(root)
    main(["project", "inspect", str(root)])

    assert result.mathlib.url == "https://***@github.com/leanprover-community/mathlib4"
    assert result.compatibility.status == "unlisted"
    assert "lake-manifest-stale" not in _codes(result)
    assert "hunter2" not in result.to_json() + capsys.readouterr().out


def test_escaped_mathlib_spelling_is_the_same_package(tmp_path: Path) -> None:
    lakefile = LAKEFILE.replace('name = "mathlib"', 'name = "«mathlib»"')
    result = inspect_project(_project(tmp_path, lakefile=lakefile, manifest=(_mathlib(name="«mathlib»"),)))

    assert result.compatibility.status == "supported"


def test_path_requirement_with_a_git_lock_is_stale(tmp_path: Path) -> None:
    lakefile = LAKEFILE.replace('scope = "leanprover-community"', 'path = "../mathlib4"')
    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert "lake-manifest-stale" in _codes(result)


def test_project_without_mathlib_is_indeterminate(tmp_path: Path) -> None:
    plausible = {**_mathlib(), "name": "plausible"}
    lakefile = 'name = "Example"\n\n[[require]]\nname = "plausible"\nscope = "leanprover-community"\n'
    result = inspect_project(_project(tmp_path, lakefile=lakefile, manifest=(plausible,)))

    assert result.ok
    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert "release-indeterminate" in _codes(result)


def test_missing_manifest_is_indeterminate(tmp_path: Path) -> None:
    result = inspect_project(_project(tmp_path, manifest=None))

    assert result.ok
    assert result.compatibility.status == "indeterminate"
    assert {"missing-lake-manifest", "release-indeterminate"} <= _codes(result)


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        '{"packages": []}',
        '{"version": "1.1.0", "packages": {}}',
        '{"version": "2.0.0", "packages": []}',
        '{"version": "2.0.0-rc1", "packages": []}',
        '{"version": 4, "packages": []}',
        '{"version": "1.1.0", "packages": [], "lakeDir": NaN}',
        '{"version": "1.1.0", "packages": [{"name": "mathlib", "type": "zip"}]}',
    ],
)
def test_manifests_lake_would_refuse_fail_inspection(tmp_path: Path, content: str) -> None:
    root = _project(tmp_path)
    (root / "lake-manifest.json").write_text(content, encoding="utf-8")

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert "invalid-lake-manifest" in _codes(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", 7),
        ("name", ""),
        ("lakeDir", 7),
        ("fixedToolchain", "false"),
        ("packagesDir", 7),
    ],
)
def test_every_decoded_manifest_root_field_is_type_checked(tmp_path: Path, field: str, value: object) -> None:
    root = _project(tmp_path)
    payload = json.loads((root / "lake-manifest.json").read_text(encoding="utf-8"))
    payload[field] = value
    (root / "lake-manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert "invalid-lake-manifest" in _codes(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", 7),
        ("name", ""),
        ("scope", 7),
        ("inherited", "false"),
        ("inherited", None),
        ("configFile", 7),
        ("manifestFile", 7),
        ("type", 7),
        ("type", "zip"),
        ("url", 7),
        ("rev", 7),
        ("inputRev", 7),
        ("subDir", 7),
    ],
)
def test_every_decoded_git_package_field_is_type_checked(tmp_path: Path, field: str, value: object) -> None:
    root = _project(tmp_path, manifest=(_mathlib(**{field: value}),))

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert "invalid-lake-manifest" in _codes(result)


@pytest.mark.parametrize("field", ["name", "inherited", "type", "url", "rev"])
def test_required_git_package_fields_cannot_be_missing(tmp_path: Path, field: str) -> None:
    entry = _mathlib()
    entry.pop(field)

    result = inspect_project(_project(tmp_path, manifest=(entry,)))

    assert not result.ok
    assert "invalid-lake-manifest" in _codes(result)


@pytest.mark.parametrize("directory", [None, 7])
def test_path_package_dir_is_a_required_string(tmp_path: Path, directory: object) -> None:
    entry = {"name": "other", "type": "path", "inherited": True, "dir": directory}
    result = inspect_project(_project(tmp_path, manifest=(_mathlib(), entry)))

    assert not result.ok
    assert "invalid-lake-manifest" in _codes(result)


@pytest.mark.parametrize("entry", [{"name": "other", "type": "path", "inherited": False}, {"name": 7}])
def test_malformed_non_mathlib_package_also_invalidates_the_manifest(tmp_path: Path, entry: dict) -> None:
    result = inspect_project(_project(tmp_path, manifest=(_mathlib(), entry)))

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert "invalid-lake-manifest" in _codes(result)


def test_fields_lake_accepts_are_still_read(tmp_path: Path) -> None:
    siblings = [_mathlib(name=name) for name in ("«doc-gen4»", "x₁", "αβ.γ", "Qq.1", "[anonymous]")]
    mathlib = {key: value for key, value in _mathlib(scope=None, manifestFile=None).items() if key != "subDir"}
    root = _project(tmp_path, manifest=None)
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "fixedToolchain": None,
                "name": "«my-project»",
                "lakeDir": None,
                "packagesDir": None,
                "packages": [*siblings, mathlib],
            }
        ),
        encoding="utf-8",
    )
    (root / ".lake").mkdir()
    (root / ".lake/package-overrides.json").write_text(  # Lake reads only the packages of this file
        json.dumps({"schemaVersion": "1.1.0", "name": 1, "lakeDir": 2, "fixedToolchain": "x", "packages": [mathlib]}),
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "supported"


@pytest.mark.parametrize("version", [5, 6, "0.6.0"])
def test_legacy_manifests_are_advisory(tmp_path: Path, version: object) -> None:
    root = _project(tmp_path)
    _write_manifest(root, _mathlib(), version=version)

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "indeterminate"
    assert "unsupported-lake-manifest" in _codes(result)


@pytest.mark.parametrize("packages", ['"packages": null', '"packages": []', '"name": "Example"'])
def test_null_or_absent_packages_mean_no_packages(tmp_path: Path, packages: str) -> None:
    root = _project(tmp_path, lakefile='name = "Example"\n')
    (root / "lake-manifest.json").write_text(f'{{"version": "1.1.0", {packages}}}', encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "indeterminate"


def test_integer_manifest_versions_are_read(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_manifest(root, _mathlib(), version=7)

    assert inspect_project(root).compatibility.status == "supported"


@pytest.mark.parametrize("version", ["1.3.0", "1.9.0"])
def test_newer_1x_manifest_versions_are_read(tmp_path: Path, version: str) -> None:
    root = _project(tmp_path)
    _write_manifest(root, _mathlib(), version=version)

    assert inspect_project(root).compatibility.status == "supported"


def test_arbitrary_precision_manifest_versions_are_compared_lexically(tmp_path: Path) -> None:
    root = _project(tmp_path)
    packages = json.dumps([_mathlib()])
    huge_integer = "9" * 5_000
    (root / "lake-manifest.json").write_text(
        f'{{"version": {huge_integer}, "packages": {packages}}}', encoding="utf-8"
    )

    assert inspect_project(root).compatibility.status == "supported"

    padded_minor = "0" * 5_000 + "7"
    (root / "lake-manifest.json").write_text(
        json.dumps({"version": f"0.{padded_minor}.0", "packages": [_mathlib()]}), encoding="utf-8"
    )

    assert inspect_project(root).compatibility.status == "supported"


def test_package_override_replaces_the_locked_mathlib(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_overrides(root, {"name": "mathlib", "type": "path", "dir": "../mathlib4", "inherited": False})

    result = inspect_project(root)

    assert result.mathlib.type == "path"
    assert result.mathlib.source == ".lake/package-overrides.json"
    assert result.compatibility.status == "indeterminate"
    assert "mathlib-overridden" in _codes(result)


def test_override_does_not_suppress_root_manifest_freshness_warning(tmp_path: Path) -> None:
    old = _mathlib(rev=OTHER_COMMIT, input_rev="v4.31.0")
    lakefile = LAKEFILE.replace('scope = "leanprover-community"', f'git = "{MATHLIB_URL}"')
    root = _project(tmp_path, lakefile=lakefile, manifest=(old,))
    _write_overrides(root, _mathlib())

    result = inspect_project(root)

    assert result.compatibility.status == "supported"
    assert {"lake-manifest-stale", "mathlib-overridden"} <= _codes(result)


def test_override_needs_a_manifest_to_replace(tmp_path: Path) -> None:
    root = _project(tmp_path, manifest=None)
    _write_overrides(root, _mathlib())

    assert inspect_project(root).compatibility.status == "indeterminate"


def test_override_is_not_parsed_on_lakes_no_manifest_update_path(tmp_path: Path) -> None:
    root = _project(tmp_path, manifest=None)
    (root / ".lake").mkdir()
    (root / ".lake/package-overrides.json").write_text("{", encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "indeterminate"
    assert "invalid-lake-manifest" not in _codes(result)


def test_override_without_mathlib_leaves_the_manifest_in_charge(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_overrides(root)

    assert inspect_project(root).compatibility.status == "supported"


def test_nondirectory_lake_path_fails_closed_without_hiding_the_manifest(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".lake").write_text("not a directory\n", encoding="utf-8")

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert result.mathlib is None
    assert any(
        diagnostic.code == "unreadable-file"
        and diagnostic.severity == "error"
        and diagnostic.path == ".lake/package-overrides.json"
        for diagnostic in result.diagnostics
    )


def test_windows_style_missing_child_of_nondirectory_lake_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / ".lake").write_text("not a directory\n", encoding="utf-8")
    override = root / ".lake/package-overrides.json"
    real_stat = project_snapshot.os.stat

    def windows_stat(path, *args, **kwargs):
        if Path(path) == override and kwargs.get("follow_symlinks", True) is False:
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(project_snapshot.os, "stat", windows_stat)

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert ".lake/package-overrides.json" in _unreadable(result)


def test_lakefile_lean_cannot_confirm_an_inherited_mathlib(tmp_path: Path) -> None:
    root = _project(tmp_path, lakefile=None, manifest=(_mathlib(inherited=True),))
    (root / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")

    result = inspect_project(root)

    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"


def test_invalid_override_file_blocks_a_supported_answer(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".lake").mkdir()
    (root / ".lake/package-overrides.json").write_text("{", encoding="utf-8")

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"


@pytest.mark.parametrize("version", [5, 6, "0.6.0"])
def test_legacy_override_of_mathlib_is_not_reported_as_the_lock(tmp_path: Path, version: object) -> None:
    # Lake 4.32.2 applies a legacy override (Manifest.getPackages decodes it as
    # PackageEntryV6), so its Mathlib replaces the manifest's; Autoform does not decode it.
    root = _project(tmp_path)
    legacy = {"name": "mathlib", "opts": {}, "inherited": False, "url": MATHLIB_URL, "rev": OTHER_COMMIT}
    _write_overrides(root, {"git": {**legacy, "inputRev?": "master"}}, schema_version=version)

    result = inspect_project(root)

    assert result.compatibility.status == "indeterminate"
    assert result.mathlib is None


def test_requirement_recorded_only_by_a_legacy_override_is_not_an_error(tmp_path: Path) -> None:
    # Lake applies a legacy override through Manifest.getPackages (PackageEntry.ofV6),
    # so it records the requirement even though Autoform does not decode the file.
    lakefile = LAKEFILE + '\n[[require]]\nname = "batteries"\nscope = "leanprover-community"\n'
    root = _project(tmp_path, lakefile=lakefile)
    legacy = {"name": "batteries", "opts": {}, "inherited": False, "url": "https://example.com/b", "rev": OTHER_COMMIT}
    _write_overrides(root, {"git": legacy}, schema_version=6)

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "indeterminate"
    assert "lake-manifest-incomplete" not in _codes(result)


def test_legacy_override_file_cannot_fall_through_to_supported_manifest(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_overrides(root, schema_version=6)

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "indeterminate"
    assert "unsupported-lake-manifest" in _codes(result)
    warning = next(diagnostic for diagnostic in result.diagnostics if diagnostic.code == "unsupported-lake-manifest")
    assert "package-overrides.json" in warning.message
    assert "lake update" not in warning.message


def test_lakefile_lean_takes_precedence_and_is_not_evaluated(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.lean").write_text('#eval IO.println "never run"\n', encoding="utf-8")

    result = inspect_project(root)

    assert result.lake.config == "lakefile.lean"
    assert result.lake.name is None
    assert result.mathlib is None
    assert "lakefile-lean-not-evaluated" in _codes(result)
    assert result.compatibility.status == "indeterminate"


def test_case_variant_lakefile_lean_still_takes_precedence_on_case_insensitive_filesystems(tmp_path: Path) -> None:
    probe = tmp_path / "case-probe"
    probe.write_text("", encoding="utf-8")
    if not (tmp_path / "CASE-PROBE").exists():
        pytest.skip("needs a case-insensitive filesystem")
    root = _project(tmp_path)
    (root / "Lakefile.lean").write_text('#eval IO.println "never run"\n', encoding="utf-8")

    result = inspect_project(root)

    assert result.lake.config == "lakefile.lean"
    assert result.compatibility.status == "indeterminate"
    assert "lakefile-lean-not-evaluated" in _codes(result)


@pytest.mark.parametrize(
    ("toolchain", "ok"),
    [
        ("leanprover/lean4:v4.32.2", True),
        ("leanprover/lean4:v4.32.2\n\n", True),
        ("leanprover/lean4:v4.32.2\r\n", True),
        (" leanprover/lean4:v4.32.2\t\n", True),
        ("\u2000leanprover/lean4:v4.32.2\u3000\n", True),
        ("leanprover/lean4:v4.32.2\nleanprover/lean4:v4.31.0\n", True),
        ("", False),
        ("\nleanprover/lean4:v4.32.2\n", False),
        ("leanprover/lean4:v4.32.2 extra\n", False),
        ("leanprover/lean4:v4.32.2\x1b\n", False),
        ("leanprover/lean4:v4.32.2\x00\n", False),
        ("leanprover/lean4:v4.32.2\u009b\n", False),
    ],
)
def test_toolchain_follows_elans_first_line_rule(tmp_path: Path, toolchain: str, ok: bool) -> None:
    # Checked against pinned elan 4.2.3: it trims the first line and rejects the file when that line is malformed.
    result = inspect_project(_project(tmp_path, toolchain=toolchain))

    assert result.ok is ok
    assert (result.lean_toolchain == "leanprover/lean4:v4.32.2") is ok


@pytest.mark.parametrize("separator", [chr(codepoint) for codepoint in range(0x1C, 0x20)])
@pytest.mark.parametrize("side", ["before", "after"])
def test_python_only_c0_whitespace_is_not_trimmed_like_elan(tmp_path: Path, separator: str, side: str) -> None:
    toolchain = "leanprover/lean4:v4.32.2"
    text = separator + toolchain if side == "before" else toolchain + separator

    result = inspect_project(_project(tmp_path, toolchain=text + "\n"))

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert any(
        diagnostic.code == "invalid-lean-toolchain" and diagnostic.severity == "error"
        for diagnostic in result.diagnostics
    )


def test_missing_toolchain_and_lakefile_are_errors(tmp_path: Path) -> None:
    root = _project(tmp_path, lakefile=None, toolchain=None)
    (root / "lakefile.toml").mkdir()  # still marks the root, but cannot be read

    result = inspect_project(root)

    assert not result.ok
    assert {"missing-lean-toolchain", "unreadable-file"} <= _codes(result)
    assert result.compatibility.status == "indeterminate"


def test_missing_lake_configuration_is_an_error_verdict(tmp_path: Path) -> None:
    result = inspect_project(_project(tmp_path, lakefile=None))

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert result.compatibility.release is None
    assert any(
        diagnostic.code == "missing-lake-config" and diagnostic.severity == "error"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize(
    "lakefile",
    [
        "name = \n",
        'version = "0.1.0"\n',
        'name = "E"\nversion = "wat"\n',
        'name = "E"\nrequire = 5\n',
        'name = "E"\n[[lean_lib]]\n',
        'name = "E"\n[[lean_lib]]\nname = "A"\n[[lean_exe]]\nname = "A"\n',
        pytest.param('name = "E"\nleanOptions = { autoImplicit = false, }\n', id="inline-trailing-comma"),
        pytest.param('name = "E"\nleanOptions = {\n  autoImplicit = false\n}\n', id="inline-multiline"),
        pytest.param('name = "E"\nnote = "\\e"\n', id="escape-e"),
        pytest.param('name = "E"\nnote = "\\x41"\n', id="escape-x"),
    ],
)
def test_lakefiles_lake_refuses_are_errors(tmp_path: Path, lakefile: str) -> None:
    # Each case checked against Lake 4.32.2, which refuses to load it.
    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert not result.ok
    assert "invalid-lakefile-toml" in _codes(result)


def test_empty_toml_names_use_lakes_simple_name_fallback(tmp_path: Path) -> None:
    lakefile = 'name = ""\n[[require]]\nname = ""\n[[lean_lib]]\nname = ""\n'

    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert result.ok
    assert result.lake.name == ""
    assert result.lake.targets[0].name == ""


def test_duplicate_target_names_are_compared_as_lean_names(tmp_path: Path) -> None:
    lakefile = 'name = "E"\n[[lean_lib]]\nname = "A"\n[[lean_exe]]\nname = "«A»"\n'

    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert not result.ok
    assert "invalid-lakefile-toml" in _codes(result)


@pytest.mark.parametrize(
    "targets",
    [
        '[[lean_lib]]\nname = "A"\n[[input_dir]]\nname = "A"\npath = "d"\n',
        '[[lean_exe]]\nname = "a"\n[[input_file]]\nname = "a"\npath = "x"\n',
        '[[input_file]]\nname = "x"\npath = "x"\n[[input_dir]]\nname = "x"\npath = "d"\n',
        '[[lean_lib]]\nname = "A"\n[[input_file]]\npath = "x"\n',
        'input_dir = 5\n[[lean_lib]]\nname = "A"\n',
    ],
    ids=["lib-dir", "exe-file", "file-dir", "unnamed-input", "input-not-tables"],
)
def test_input_targets_share_lakes_target_namespace(tmp_path: Path, targets: str) -> None:
    # Lake 4.32.2 refuses each (implB corpus tgt-dup-lib-dir, tgt-dup-exe-dir,
    # e-input-dup-file-dir, tgt-file-no-name, tgt-file-not-array).
    result = inspect_project(_project(tmp_path, lakefile='name = "E"\n' + targets))

    assert not result.ok
    assert "invalid-lakefile-toml" in _codes(result)


def test_distinct_input_targets_are_left_out_of_the_report(tmp_path: Path) -> None:
    targets = (
        '[[lean_lib]]\nname = "A"\n[[input_file]]\nname = "b"\npath = "x"\n[[input_dir]]\nname = "c"\npath = "d"\n'
    )
    result = inspect_project(_project(tmp_path, lakefile=LAKEFILE.split("\n\n[[lean_lib]]")[0] + "\n" + targets))

    assert result.compatibility.status == "supported"
    assert result.lake.targets == (project_inspect.LakeTarget("lean_lib", "A"),)


def test_numeric_and_escaped_numeric_target_names_are_distinct(tmp_path: Path) -> None:
    lakefile = 'name = "E"\n[[lean_lib]]\nname = "1"\n[[lean_exe]]\nname = "«1»"\n'

    assert inspect_project(_project(tmp_path, lakefile=lakefile)).ok


def test_arbitrary_precision_numeric_names_are_compared_without_python_ints(tmp_path: Path) -> None:
    padded_one = "0" * 5_000 + "1"
    lakefile = f'name = "E"\n[[lean_lib]]\nname = "{padded_one}"\n[[lean_exe]]\nname = "1"\n'

    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert "invalid-lakefile-toml" in _codes(result)


@pytest.mark.parametrize(("plain", "escaped"), [("", "«»"), ("a b", "«a b»"), ("[anonymous]", "«[anonymous]»")])
def test_toml_simple_name_fallback_matches_the_equivalent_escape(
    tmp_path: Path, plain: str, escaped: str
) -> None:
    lakefile = f'name = "E"\n[[lean_lib]]\nname = "{plain}"\n[[lean_exe]]\nname = "{escaped}"\n'

    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert not result.ok
    assert "invalid-lakefile-toml" in _codes(result)


def test_root_package_reuses_itself_instead_of_the_same_name_manifest_entry(tmp_path: Path) -> None:
    lakefile = 'name = "mathlib"\n[[require]]\nname = "«mathlib»"\nscope = "leanprover-community"\n'

    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert result.ok
    assert result.compatibility.status == "indeterminate"
    assert "mathlib-manifest-unused" in _codes(result)


@pytest.mark.parametrize(
    "requirement",
    [
        'name = "mathlib"\nscope = 7',
        'name = "mathlib"\nrev = 7',
        'name = "mathlib"\npath = 7',
        'name = "mathlib"\ngit = 7',
        'name = "mathlib"\ngit = {subDir = "x"}',
        'name = "mathlib"\ngit = {url = 7}',
        'name = "mathlib"\ngit = {url = "https://example.com/x", subDir = 7}',
        'name = "mathlib"\nsource = 7',
        'name = "mathlib"\nsource = {type = "zip"}',
        'name = "mathlib"\nsource = {type = "path"}',
        'name = "mathlib"\nsource = {type = "git", url = 7}',
        'name = "mathlib"\noptions = 7',
        'name = "mathlib"\noptions = {foo = 7}',
        'name = "mathlib"\nversion = "1.2.3"',
    ],
)
def test_every_active_requirement_field_lake_decodes_is_validated(tmp_path: Path, requirement: str) -> None:
    lakefile = f'name = "Example"\n\n[[require]]\n{requirement}\n'

    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert "invalid-lakefile-toml" in _codes(result)


def test_malformed_shadowed_requirement_source_is_ignored_as_lake_ignores_it(tmp_path: Path) -> None:
    lakefile = (
        'name = "Example"\n\n[[require]]\nname = "mathlib"\npath = "../mathlib"\n'
        'git = 7\nsource = 7\nscope = ""\n'
    )

    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert result.ok
    assert "lake-manifest-stale" in _codes(result)


@pytest.mark.parametrize(
    "version",
    [
        "*",
        "1.*",
        "1.2.x",
        "^1",
        "^0.2.3",
        "~1.2",
        ">=1.0.0 <2.0.0",
        ">=1.0.0, || <2.0.0",
        "=1.2.3",
        "git#main",
    ],
)
def test_lake_version_constraint_spellings_are_accepted(tmp_path: Path, version: str) -> None:
    lakefile = f'name = "Example"\n\n[[require]]\nname = "other"\nversion = "{version}"\n'

    assert inspect_project(_project(tmp_path, lakefile=lakefile, manifest=(_mathlib(name="other"),))).ok


def test_huge_toml_numbers_return_stable_diagnostics(tmp_path: Path) -> None:
    huge = "0" * 5_000
    raw_integer = f'name = "Example"\nunknown = {"9" * 5_000}\n'
    zero_constraint = f'name = "Example"\n[[require]]\nname = "other"\nversion = "^{huge}.0.0"\n'

    for index, (lakefile, message) in enumerate(
        (
            (raw_integer, "Autoform could not safely decode lakefile.toml because it exceeds the parser's limits."),
            (zero_constraint, "Lake cannot load lakefile.toml: a require entry has an invalid version constraint."),
        )
    ):
        case = tmp_path / str(index)
        case.mkdir()
        result = inspect_project(_project(case, lakefile=lakefile))

        assert not result.ok
        assert result.compatibility.status == "indeterminate"
        diagnostic = next(item for item in result.diagnostics if item.code == "invalid-lakefile-toml")
        assert diagnostic.severity == "error"
        assert diagnostic.message == message


@pytest.mark.parametrize(
    "version",
    [
        "1",
        "1.2",
        "1.2.3",
        "^0.0.0",
        "^00.00.00",
        ">=1.2",
        "1.*.3",
        "|| 1.*",
        "1.*, ",
        ">=1.0.0\u00a0<2.0.0",
    ],
)
def test_lake_rejected_version_constraint_spellings_are_errors(tmp_path: Path, version: str) -> None:
    lakefile = f'name = "Example"\n\n[[require]]\nname = "other"\nversion = "{version}"\n'

    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert not result.ok
    assert "invalid-lakefile-toml" in _codes(result)


def test_git_table_ignores_its_inner_rev_field_like_lake(tmp_path: Path) -> None:
    lakefile = (
        'name = "Example"\n\n[[require]]\nname = "other"\n'
        'git = {url = "https://example.com/other", rev = 7}\n'
    )

    assert inspect_project(_project(tmp_path, lakefile=lakefile, manifest=(_mathlib(name="other"),))).ok


@pytest.mark.parametrize("lakefile", ['name = "my-project"\n', 'name = "E"\nsrcDir = "../outside"\n'])
def test_lakefiles_lake_accepts_are_fine(tmp_path: Path, lakefile: str) -> None:
    assert inspect_project(_project(tmp_path, lakefile=lakefile + LAKEFILE.split("\n\n", 1)[1])).ok


def test_target_options_outside_the_release_pair_contract_are_left_to_lake(tmp_path: Path) -> None:
    target = '[[lean_lib]]\nname = "Example"\n'
    lakefile = LAKEFILE.replace(target, target + "srcDir = 7\n")
    assert lakefile != LAKEFILE

    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert result.ok
    assert result.compatibility.status == "supported"


def test_oversized_and_non_utf8_files_are_errors(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lake-manifest.json").write_bytes(b" " * (1024 * 1024 + 1))
    (root / "lean-toolchain").write_bytes(b"\xff\n")

    result = inspect_project(root)

    assert _unreadable(result) == ["lake-manifest.json", "lean-toolchain"]


@pytest.mark.parametrize(
    ("relative", "failure"),
    [
        ("lakefile.toml", "non-utf8"),
        ("lakefile.toml", "oversized"),
        (".lake/package-overrides.json", "oversized"),
    ],
)
def test_lake_decision_file_read_failures_are_error_verdicts(
    tmp_path: Path, relative: str, failure: str
) -> None:
    root = _project(tmp_path)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff" if failure == "non-utf8" else b" " * (1024 * 1024 + 1))

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert result.compatibility.release is None
    assert any(
        diagnostic.code == "unreadable-file"
        and diagnostic.severity == "error"
        and diagnostic.path == relative
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize(
    "denied", ["lakefile.toml", "lean-toolchain", "lake-manifest.json", ".lake/package-overrides.json"]
)
def test_stably_unreadable_decision_file_is_not_misreported_as_changing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: str
) -> None:
    root = _project(tmp_path)
    if denied == ".lake/package-overrides.json":
        _write_overrides(root)
    original = project_snapshot._open_resolved

    def deny_one(path: Path, *args):
        if path == root / denied:
            raise PermissionError(denied)
        return original(path, *args)

    monkeypatch.setattr(project_snapshot, "_open_resolved", deny_one)

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert result.compatibility.release is None
    assert any(
        diagnostic.code == "unreadable-file"
        and diagnostic.severity == "error"
        and diagnostic.path == denied
        for diagnostic in result.diagnostics
    )
    assert "project-changed-during-inspection" not in _codes(result)


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="chmod 000 must make files unreadable",
)
@pytest.mark.parametrize("denied", ["lakefile.toml", "lean-toolchain", "lake-manifest.json"])
def test_chmod_unreadable_decision_file_keeps_its_specific_diagnostic(tmp_path: Path, denied: str) -> None:
    root = _project(tmp_path)
    path = root / denied
    path.chmod(0)
    try:
        result = inspect_project(root)
    finally:
        path.chmod(0o600)

    assert not result.ok
    assert denied in _unreadable(result)
    assert "project-changed-during-inspection" not in _codes(result)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_fifo_configuration_is_never_opened(tmp_path: Path) -> None:
    root = _project(tmp_path, lakefile=None)
    os.mkfifo(root / "lakefile.toml")

    probe = _run_fifo_probe(root, "inspect")
    result = probe["result"]

    assert "unreadable-file" in {item["code"] for item in result["diagnostics"]}


def test_decision_files_are_retried_as_one_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Neither real state is supported: A has the new Lean with the old lock,
    # and B has the old Lean with the new lock. A sequential read could invent
    # a supported new-Lean/new-lock pair that never existed.
    root = _project(tmp_path, manifest=(_mathlib(rev=OTHER_COMMIT, input_rev="v4.31.0"),))
    original = project_snapshot._capture_file
    switched = False

    def capture_and_switch(captured_root: Path, relative: str):
        nonlocal switched
        entry = original(captured_root, relative)
        if relative == "lean-toolchain" and not switched:
            switched = True
            (root / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n", encoding="utf-8")
            _write_manifest(root, _mathlib())
        return entry

    monkeypatch.setattr(project_snapshot, "_capture_file", capture_and_switch)

    result = inspect_project(root)

    assert switched
    assert result.lean_toolchain == "leanprover/lean4:v4.31.0"
    assert result.mathlib.rev == COMMIT
    assert result.compatibility.status == "unlisted"


def test_rename_aba_cannot_repeat_a_synthetic_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path, manifest=(_mathlib(rev=OTHER_COMMIT, input_rev="v4.31.0"),))
    lean = root / "lean-toolchain"
    manifest = root / "lake-manifest.json"
    lean_b = root / ".lean-b"
    manifest_b = root / ".manifest-b"
    lean_b.write_text("leanprover/lean4:v4.31.0\n", encoding="utf-8")
    manifest_b.write_text(
        json.dumps({"version": "1.1.0", "packagesDir": ".lake/packages", "packages": [_mathlib()]}),
        encoding="utf-8",
    )
    original = project_snapshot._capture_file
    state = "a"

    def swap(first: Path, second: Path, saved: Path) -> None:
        first.replace(saved)
        second.replace(first)

    def capture_and_cycle(captured_root: Path, relative: str):
        nonlocal state
        entry = original(captured_root, relative)
        if relative == "lean-toolchain" and state == "a":
            swap(lean, lean_b, root / ".lean-a")
            swap(manifest, manifest_b, root / ".manifest-a")
            state = "b"
        elif relative == "lake-manifest.json" and state == "b":
            swap(lean, root / ".lean-a", lean_b)
            swap(manifest, root / ".manifest-a", manifest_b)
            state = "a"
        return entry

    monkeypatch.setattr(project_snapshot, "_capture_file", capture_and_cycle)

    result = inspect_project(root)

    assert result.compatibility.status == "indeterminate"
    assert "project-changed-during-inspection" in _codes(result)


def test_override_parent_generation_is_part_of_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    lake_dir = root / ".lake"
    lake_dir.mkdir()
    override = lake_dir / "package-overrides.json"
    override.write_text(json.dumps({"schemaVersion": "1.1.0", "packages": []}), encoding="utf-8")
    original = project_snapshot._capture_file

    def capture_and_rename_aba(captured_root: Path, relative: str):
        entry = original(captured_root, relative)
        if relative == ".lake/package-overrides.json":
            temporary = lake_dir / ".override-aba"
            override.replace(temporary)
            temporary.replace(override)
        return entry

    monkeypatch.setattr(project_snapshot, "_capture_file", capture_and_rename_aba)

    result = inspect_project(root)

    assert result.compatibility.status == "indeterminate"
    assert "project-changed-during-inspection" in _codes(result)


def test_new_nearer_project_root_forces_a_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outer = _project(tmp_path)
    inner = outer / "nested"
    inner.mkdir()
    original = project_snapshot._capture_decision_snapshot
    inserted = False

    def capture_after_inserting_root(root: Path):
        nonlocal inserted
        if not inserted:
            inserted = True
            (inner / "lakefile.toml").write_text(LAKEFILE, encoding="utf-8")
            (inner / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n", encoding="utf-8")
            _write_manifest(inner, _mathlib())
        return original(root)

    monkeypatch.setattr(project_snapshot, "_capture_decision_snapshot", capture_after_inserting_root)

    result = inspect_project(inner)

    assert inserted
    assert result.project_root == "."
    assert result.lean_toolchain == "leanprover/lean4:v4.31.0"
    assert result.compatibility.status == "unlisted"


def test_suppressed_path_predicate_errors_cannot_hide_a_nearer_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = _project(tmp_path)
    inner = outer / "nested"
    inner.mkdir()
    (inner / "lakefile.toml").write_text(LAKEFILE, encoding="utf-8")
    (inner / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n", encoding="utf-8")
    _write_manifest(inner, _mathlib())
    original_exists = Path.exists
    original_is_symlink = Path.is_symlink

    def suppressed_exists(path: Path) -> bool:
        return False if path.parent == inner and path.name in project_snapshot._ROOT_MARKERS else original_exists(path)

    def suppressed_is_symlink(path: Path) -> bool:
        return False if path.parent == inner and path.name in project_snapshot._ROOT_MARKERS else original_is_symlink(path)

    monkeypatch.setattr(Path, "exists", suppressed_exists)
    monkeypatch.setattr(Path, "is_symlink", suppressed_is_symlink)

    result = inspect_project(inner)

    assert result.project_root == "."
    assert result.lean_toolchain == "leanprover/lean4:v4.31.0"
    assert result.compatibility.status == "unlisted"


def test_autoform_paths_are_revalidated_with_the_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    (root / "blueprint").mkdir()
    original = project_snapshot._capture_decision_snapshot
    removed = False

    def capture_after_removing_blueprint(captured_root: Path):
        nonlocal removed
        if not removed:
            removed = True
            (root / "blueprint").rmdir()
        return original(captured_root)

    monkeypatch.setattr(project_snapshot, "_capture_decision_snapshot", capture_after_removing_blueprint)

    result = inspect_project(root)

    assert removed
    assert result.compatibility.status == "supported"
    assert result.autoform_paths == ()


@pytest.mark.skipif(not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"), reason="needs POSIX FIFOs")
def test_file_replaced_by_fifo_between_stat_and_open_never_blocks(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    probe = _run_fifo_probe(root, "swap")
    result = probe["result"]

    assert probe["switched"]
    # The swap lands in the descriptor-relative open wherever real runs use one.
    assert probe["dir_fd"] == (os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY"))
    assert not result["ok"]
    assert result["compatibility"]["status"] == "indeterminate"
    assert "unreadable-file" in {item["code"] for item in result["diagnostics"]}


def test_decision_file_accepts_windows_path_and_handle_stat_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    original_fstat = project_snapshot.os.fstat

    class WindowsHandleStat:
        def __init__(self, metadata: os.stat_result) -> None:
            self.st_dev = metadata.st_dev
            self.st_ino = metadata.st_ino
            self.st_mode = metadata.st_mode
            self.st_size = metadata.st_size
            self.st_mtime_ns = metadata.st_mtime_ns
            self.st_ctime_ns = metadata.st_ctime_ns + 1

    monkeypatch.setattr(
        project_snapshot.os,
        "fstat",
        lambda descriptor: WindowsHandleStat(original_fstat(descriptor)),
    )
    monkeypatch.setattr(project_snapshot, "_WINDOWS_STAT_VIEWS", True)

    captured = project_snapshot._capture_file(root, "lakefile.toml")

    assert captured.state == "regular"
    assert captured.content is not None
    assert captured.content.decode("utf-8").splitlines() == LAKEFILE.splitlines()


def _run_fifo_probe(root: Path, mode: str) -> dict:
    fixture = Path(__file__).parent / "fixtures/project_inspect_fifo_probe.py"
    try:
        completed = subprocess.run(
            [sys.executable, str(fixture), mode, str(root)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"project inspection blocked on the {mode} FIFO probe: {error}")
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_nearest_root_is_reported_relative_to_the_target(tmp_path: Path) -> None:
    root = _project(tmp_path)
    nested = root / "Example" / "Algebra"
    nested.mkdir(parents=True)
    (nested / "Basic.lean").write_text("", encoding="utf-8")

    assert inspect_project(nested).project_root == "../.."
    assert inspect_project(nested / "Basic.lean").project_root == "../.."
    assert inspect_project(root).project_root == "."


def test_nested_package_is_its_own_root(tmp_path: Path) -> None:
    root = _project(tmp_path)
    package = root / ".lake" / "packages" / "inner"
    package.mkdir(parents=True)
    (package / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")

    result = inspect_project(package)

    assert result.project_root == "."
    assert "missing-lake-config" in _codes(result)


def test_missing_target_and_no_project_are_errors(tmp_path: Path) -> None:
    assert "target-unreadable" in _codes(inspect_project(tmp_path / "absent"))
    empty = tmp_path / "empty"
    empty.mkdir()
    result = inspect_project(empty)
    assert not result.ok
    assert result.project_root is None
    assert "project-not-found" in _codes(result)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_projects_behind_symlinked_directories_are_inspected(tmp_path: Path) -> None:
    _project(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)

    result = inspect_project(link / "project")

    assert result.ok
    assert result.compatibility.status == "supported"


def _move_behind_symlink(root: Path, relative: str) -> Path:
    """Move a project entry into a shared directory and leave a symlink in its place."""

    target = root.parent / "shared" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    (root / relative).rename(target)
    (root / relative).symlink_to(target, target_is_directory=target.is_dir())
    return target


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("relative", ["lean-toolchain", "lakefile.toml", "lake-manifest.json"])
def test_symlinked_decision_files_are_followed_like_lake(tmp_path: Path, relative: str) -> None:
    root = _project(tmp_path)
    _move_behind_symlink(root, relative)

    result = inspect_project(root)

    assert result.ok, result.diagnostics
    assert result.compatibility.status == "supported"
    assert result.compatibility.release == "lean-v4.32.2-mathlib-v4.32.2"


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX path-buffer semantics")
@pytest.mark.parametrize("absolute_first_target", [False, True])
def test_nested_symlink_expansion_matches_the_native_kernel(
    tmp_path: Path, absolute_first_target: bool
) -> None:
    root = _project(tmp_path)
    (root / "d").mkdir()
    actual = root / "actual.toml"
    (root / "lakefile.toml").rename(actual)
    limit = os.pathconf(root, "PC_PATH_MAX")
    unit = "d/../"
    next_target = unit * max(1, (3 * limit // 4) // len(os.fsencode(unit)))
    (root / "next").symlink_to(next_target, target_is_directory=True)
    first_prefix = f"{root}/next/" if absolute_first_target else "next/"
    padding = "./" * max(
        1,
        (limit // 2 - len(os.fsencode(first_prefix)) - len(os.fsencode(actual.name)))
        // len(os.fsencode("./")),
    )
    first_target = f"{first_prefix}{padding}{actual.name}"
    assert len(os.fsencode(next_target)) < limit
    assert len(os.fsencode(first_target)) < limit
    assert len(os.fsencode(f"{next_target}{padding}{actual.name}")) >= limit
    assert len(os.fsencode(os.path.join(next_target, actual.name))) < limit
    (root / "lakefile.toml").symlink_to(first_target)
    assert stat.S_ISLNK(os.stat(root / "lakefile.toml", follow_symlinks=False).st_mode)
    resolved, _, _ = project_snapshot._resolve(root, "lakefile.toml")
    assert resolved.samefile(actual)

    try:
        os.stat(root / "lakefile.toml")
    except OSError as error:
        assert error.errno == errno.ENAMETOOLONG
        native_state = "unreadable"
    else:
        native_state = "regular"

    captured = project_snapshot._capture_file(root, "lakefile.toml")
    result = inspect_project(root)

    assert captured.state == native_state
    if native_state == "unreadable":
        assert not result.ok
        assert result.compatibility.status == "indeterminate"
        assert "lakefile.toml" in _unreadable(result)
    else:
        assert result.ok, result.diagnostics
        assert result.compatibility.status == "supported"
        assert "unreadable-file" not in _codes(result)


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX path-buffer semantics")
@pytest.mark.parametrize("ending", ["/", "/."])
def test_directory_symlink_at_kernel_path_limit_remains_readable(tmp_path: Path, ending: str) -> None:
    root = _project(tmp_path)
    actual_lake = root / "actual-lake"
    actual_lake.mkdir()
    (actual_lake / "package-overrides.json").write_text(
        json.dumps({"schemaVersion": "1.1.0", "packages": []}),
        encoding="utf-8",
    )
    limit = os.pathconf(root, "PC_PATH_MAX")
    pending = "package-overrides.json"
    unit = "d/../"
    (root / "d").mkdir()
    link = root / ".lake"
    desired = limit - (2 if ending == "/" else 1)
    ending_and_join = 1 if ending == "/" else 3
    budget = desired - len(os.fsencode(pending)) - ending_and_join
    repeats = (budget - 1) // len(os.fsencode(unit))
    name = "a" * (budget - repeats * len(os.fsencode(unit)))
    native_directory = root / name
    actual_lake.rename(native_directory)
    raw_target = f"{unit * repeats}{name}{ending}"
    link.symlink_to(raw_target, target_is_directory=True)

    native_expansion = os.path.join(raw_target, pending)
    reconstructed = os.path.join(raw_target, ".", pending)
    assert len(os.fsencode(native_expansion)) == desired
    assert len(os.fsencode(reconstructed)) >= limit
    assert stat.S_ISREG(os.stat(link / pending).st_mode)
    resolved, _, _ = project_snapshot._resolve(root, ".lake/package-overrides.json")
    assert resolved.samefile(native_directory / pending)
    assert project_snapshot._capture_file(root, ".lake/package-overrides.json").state == "regular"

    result = inspect_project(root)

    assert result.ok, result.diagnostics
    assert result.compatibility.status == "supported"
    assert "unreadable-file" not in _codes(result)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinked_lakefile_lean_takes_precedence_without_an_error(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
    _move_behind_symlink(root, "lakefile.lean")

    result = inspect_project(root)

    assert result.ok, result.diagnostics
    assert result.lake.config == "lakefile.lean"
    assert "lakefile-lean-not-evaluated" in _codes(result)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("with_override", [False, True])
def test_symlinked_lake_directory_is_followed_like_lake(tmp_path: Path, with_override: bool) -> None:
    root = _project(tmp_path)
    if with_override:
        _write_overrides(root, {"name": "mathlib", "type": "path", "dir": "../mathlib4", "inherited": False})
    else:
        (root / ".lake").mkdir()
    _move_behind_symlink(root, ".lake")

    result = inspect_project(root)

    assert "unreadable-file" not in _codes(result)
    if with_override:
        assert result.mathlib.source == ".lake/package-overrides.json"
        assert "mathlib-overridden" in _codes(result)
    else:
        assert result.ok, result.diagnostics
        assert result.compatibility.status == "supported"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinked_package_overrides_file_is_followed_like_lake(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_overrides(root, {"name": "mathlib", "type": "path", "dir": "../mathlib4", "inherited": False})
    _move_behind_symlink(root, ".lake/package-overrides.json")

    result = inspect_project(root)

    assert "unreadable-file" not in _codes(result)
    assert result.mathlib.source == ".lake/package-overrides.json"
    assert "mathlib-overridden" in _codes(result)


@pytest.mark.skipif(not hasattr(os, "mkfifo") or sys.platform == "win32", reason="needs FIFOs and symlinks")
def test_symlink_to_a_fifo_is_never_opened(tmp_path: Path) -> None:
    root = _project(tmp_path)
    fifo = tmp_path / "lakefile-fifo"
    os.mkfifo(fifo)
    (root / "lakefile.toml").unlink()
    (root / "lakefile.toml").symlink_to(fifo)

    result = _run_fifo_probe(root, "inspect")["result"]

    assert not result["ok"]
    assert any(item["code"] == "unreadable-file" and item["path"] == "lakefile.toml" for item in result["diagnostics"])
    assert "project-changed-during-inspection" not in {item["code"] for item in result["diagnostics"]}


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_dangling_symlinked_decision_file_is_present_but_unreadable(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lean-toolchain").unlink()
    (root / "lean-toolchain").symlink_to(tmp_path / "absent-toolchain")

    result = inspect_project(root)

    assert not result.ok
    assert "lean-toolchain" in _unreadable(result)
    assert "missing-lean-toolchain" not in _codes(result)
    assert "project-changed-during-inspection" not in _codes(result)


_UNUSABLE_LINK_TARGETS = ("fifo", "/dev/zero", "socket", "dangling", "loop")


def _link_to_unusable_target(link: Path, target: str, elsewhere: Path) -> None:
    """Make ``link`` a symlink to a FIFO, a device, a socket, nothing, or a link back to itself."""

    link.unlink(missing_ok=True)
    elsewhere.mkdir()
    if target == "fifo":
        os.mkfifo(elsewhere / "fifo")
        link.symlink_to(elsewhere / "fifo")
    elif target == "socket":
        # A short relative name stays under the AF_UNIX path length limit.
        previous = os.getcwd()
        os.chdir(elsewhere)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind("socket")
        finally:
            os.chdir(previous)
        link.symlink_to(elsewhere / "socket")
    elif target == "dangling":
        link.symlink_to(elsewhere / "absent")
    elif target == "loop":
        (elsewhere / "loop").symlink_to(link)
        link.symlink_to(elsewhere / "loop")
    else:
        link.symlink_to(target)


def _inspect_link_target(root: Path, target: str) -> dict:
    """Inspect in the deadline-bound child when a regression could block or read forever."""

    if target in ("fifo", "/dev/zero"):
        return _run_fifo_probe(root, "inspect")["result"]
    return inspect_project(root).as_dict()


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs, devices and symlinks")
@pytest.mark.parametrize("target", _UNUSABLE_LINK_TARGETS)
@pytest.mark.parametrize("relative", ["lake-manifest.json", ".lake/package-overrides.json"])
def test_symlink_that_ends_on_no_regular_file_is_unreadable(tmp_path: Path, relative: str, target: str) -> None:
    root = _project(tmp_path)
    (root / ".lake").mkdir()
    _link_to_unusable_target(root / relative, target, tmp_path / "elsewhere")

    result = _inspect_link_target(root, target)

    assert not result["ok"]
    assert [item["path"] for item in result["diagnostics"] if item["code"] == "unreadable-file"] == [relative]
    assert "project-changed-during-inspection" not in {item["code"] for item in result["diagnostics"]}


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs, devices and symlinks")
@pytest.mark.parametrize("target", [target for target in _UNUSABLE_LINK_TARGETS if target != "dangling"])
def test_lake_link_that_ends_on_no_directory_fails_closed(tmp_path: Path, target: str) -> None:
    root = _project(tmp_path)
    _link_to_unusable_target(root / ".lake", target, tmp_path / "elsewhere")

    result = _inspect_link_target(root, target)

    assert not result["ok"]
    assert [item["path"] for item in result["diagnostics"] if item["code"] == "unreadable-file"] == [
        ".lake/package-overrides.json"
    ]
    assert "project-changed-during-inspection" not in {item["code"] for item in result["diagnostics"]}


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_dangling_lake_link_has_no_overrides_as_in_lake(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".lake").symlink_to(tmp_path / "absent-lake", target_is_directory=True)

    result = inspect_project(root)

    assert result.ok, result.diagnostics
    assert result.compatibility.status == "supported"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(
    ("link", "relative"), [("lakefile.toml", "lakefile.toml"), (".lake", ".lake/package-overrides.json")]
)
def test_unusable_symlink_is_identified_by_the_link_not_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link: str, relative: str
) -> None:
    # Writes change /dev/null's times on macOS, so a link to a device must not
    # read as a project that keeps changing.
    root = _project(tmp_path)
    target = tmp_path / "busy"
    if link == ".lake":
        target.write_text("", encoding="utf-8")
    else:
        target.mkdir()
    (root / link).unlink(missing_ok=True)
    (root / link).symlink_to(target)
    original = project_snapshot._capture_file
    touches = 0

    def capture_and_touch_target(captured_root: Path, captured: str):
        nonlocal touches
        entry = original(captured_root, captured)
        if captured == relative:
            touches += 1
            os.utime(target, ns=(touches * 10**9, touches * 10**9))
        return entry

    monkeypatch.setattr(project_snapshot, "_capture_file", capture_and_touch_target)

    result = inspect_project(root)

    assert touches >= 2
    assert _unreadable(result) == [relative]
    assert "project-changed-during-inspection" not in _codes(result)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("swap", ["relink", "rename"])
@pytest.mark.parametrize("where", ["subdirectory", "outside"])
def test_lockstep_swaps_behind_a_linked_path_cannot_repeat_a_synthetic_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str, swap: str
) -> None:
    # Both decision files are links into "current", which holds state A for
    # every lean-toolchain read and state B for every other read.  Neither
    # decision file nor the root changes; the swaps show only on the way to
    # the files.  The holder's times are set explicitly so the result does
    # not depend on timestamp granularity.
    root = _project(tmp_path)
    holder = root / "sub" if where == "subdirectory" else tmp_path / "outside"
    holder.mkdir()
    for name, toolchain, mathlib in (
        ("a", "leanprover/lean4:v4.32.2\n", _mathlib(rev=OTHER_COMMIT, input_rev="v4.31.0")),
        ("b", "leanprover/lean4:v4.31.0\n", _mathlib()),
    ):
        (holder / name).mkdir()
        (holder / name / "lean-toolchain").write_text(toolchain, encoding="utf-8")
        _write_manifest(holder / name, mathlib)
    current = holder / "current"
    if swap == "relink":
        current.symlink_to(holder / "a", target_is_directory=True)
    else:
        (holder / "a").rename(current)
    for relative in ("lean-toolchain", "lake-manifest.json"):
        (root / relative).unlink()
        (root / relative).symlink_to(current / relative)
    state = "a"
    swaps = 0

    def switch(to: str) -> None:
        nonlocal state, swaps
        if to == state:
            return
        if swap == "relink":
            (holder / "next").symlink_to(holder / to, target_is_directory=True)
            (holder / "next").replace(current)
        else:
            current.rename(holder / state)
            (holder / to).rename(current)
        state = to
        swaps += 1
        os.utime(holder, ns=(swaps * 10**9, swaps * 10**9))

    original = project_snapshot._capture_file

    def capture_in_lockstep(captured_root: Path, relative: str):
        if relative == "lean-toolchain":
            switch("a")
        entry = original(captured_root, relative)
        if relative == "lean-toolchain":
            switch("b")
        return entry

    monkeypatch.setattr(project_snapshot, "_capture_file", capture_in_lockstep)

    result = inspect_project(root)

    assert result.compatibility.status == "indeterminate"
    assert "project-changed-during-inspection" in _codes(result)


@pytest.mark.skipif(
    os.name != "posix" or os.open not in os.supports_dir_fd or not os.path.exists("/dev/zero"),
    reason="needs POSIX directory descriptors, symlinks and /dev/zero",
)
def test_parent_swapped_for_a_link_before_the_open_never_opens_a_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # lakefile.toml resolves to the regular file elsewhere/dev/zero.  Just
    # before the open, "elsewhere" becomes a link to "/", so the same path
    # names the /dev/zero device.
    root = _project(tmp_path)
    elsewhere = tmp_path.resolve() / "elsewhere"
    (elsewhere / "dev").mkdir(parents=True)
    (elsewhere / "dev" / "zero").write_text(LAKEFILE, encoding="utf-8")
    (root / "lakefile.toml").unlink()
    (root / "lakefile.toml").symlink_to(elsewhere / "dev" / "zero")
    original_open = os.open
    triggers = {str(root.resolve() / "lakefile.toml"), str(elsewhere / "dev")}
    swapped = False
    opened: list[int] = []

    def swap_then_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and str(path) in triggers:
            swapped = True
            elsewhere.rename(tmp_path / "moved")
            elsewhere.symlink_to("/", target_is_directory=True)
        descriptor = original_open(path, flags, *args, **kwargs)
        opened.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
        return descriptor

    monkeypatch.setattr(project_snapshot.os, "open", swap_then_open)
    # A wrapped os.open is not in os.supports_dir_fd; keep the descriptor-relative opens.
    monkeypatch.setattr(project_snapshot.os, "supports_dir_fd", {*os.supports_dir_fd, swap_then_open})
    try:
        result = inspect_project(root)
    finally:
        if elsewhere.is_symlink():
            elsewhere.unlink()

    assert swapped
    assert stat.S_IFCHR not in opened
    assert not result.ok
    assert "lakefile.toml" in _unreadable(result)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0 or not (hasattr(os, "O_PATH") or hasattr(os, "O_SEARCH")),
    reason="needs POSIX permissions that apply and search-only directory opens",
)
def test_decision_file_linked_into_a_search_only_directory_is_read_like_lake(tmp_path: Path) -> None:
    # Lake and elan need search permission, not read permission, on the
    # directories on the way to a file.
    root = _project(tmp_path)
    target = _move_behind_symlink(root, "lean-toolchain")
    target.parent.chmod(0o311)
    try:
        with pytest.raises(PermissionError):
            os.listdir(target.parent)
        result = inspect_project(root)
    finally:
        target.parent.chmod(0o755)

    assert result.ok, result.diagnostics
    assert result.compatibility.status == "supported"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(("suffix", "absolute"), [("/", True), ("/.", False)])
def test_link_target_that_must_be_a_directory_never_reads_a_file(tmp_path: Path, suffix: str, absolute: bool) -> None:
    # A trailing "/" or "/." asks for a directory, so the kernel, and elan
    # with it, refuses the regular file there.
    root = _project(tmp_path)
    target = _move_behind_symlink(root, "lean-toolchain")
    link = root / "lean-toolchain"
    link.unlink()
    os.symlink(f"{target if absolute else os.path.relpath(target, root)}{suffix}", link)
    with pytest.raises(NotADirectoryError):
        link.read_bytes()

    result = inspect_project(root)

    assert _unreadable(result) == ["lean-toolchain"]
    assert "project-changed-during-inspection" not in _codes(result)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize(("suffix", "absolute"), [("/", True), ("/.", False)])
def test_directory_link_target_ending_in_a_slash_is_followed(tmp_path: Path, suffix: str, absolute: bool) -> None:
    # Shell completion writes directory links as "../shared/.lake/", and the
    # kernel follows them like links without the slash.
    root = _project(tmp_path)
    _write_overrides(root, {"name": "mathlib", "type": "path", "dir": "../mathlib4", "inherited": False})
    target = _move_behind_symlink(root, ".lake")
    link = root / ".lake"
    link.unlink()
    os.symlink(f"{target if absolute else os.path.relpath(target, root)}{suffix}", link)
    assert (link / "package-overrides.json").is_file()

    result = inspect_project(root)

    assert "unreadable-file" not in _codes(result)
    assert result.mathlib.source == ".lake/package-overrides.json"
    assert "mathlib-overridden" in _codes(result)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_link_through_a_link_with_an_empty_target_is_unreadable(tmp_path: Path) -> None:
    # macOS creates a link with an empty target, and the kernel fails every
    # lookup through it; Linux refuses to create one.
    root = _project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        os.symlink("", elsewhere / "empty")
    except OSError:
        pytest.skip("this platform does not create links with an empty target")
    (elsewhere / "real-toolchain").write_bytes((root / "lean-toolchain").read_bytes())
    link = root / "lean-toolchain"
    link.unlink()
    link.symlink_to(elsewhere / "empty" / "real-toolchain")
    with pytest.raises(FileNotFoundError):
        link.read_bytes()

    result = inspect_project(root)

    assert _unreadable(result) == ["lean-toolchain"]
    assert "project-changed-during-inspection" not in _codes(result)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("extra", [0, 1])
def test_link_chains_are_followed_as_far_as_the_kernel_follows_them(tmp_path: Path, extra: int) -> None:
    root = _project(tmp_path)
    target = _move_behind_symlink(root, "lean-toolchain")
    # lean-toolchain and the chain links below it, all relative.
    links = project_snapshot._MAX_SYMLINKS + extra
    chain = tmp_path / "chain"
    chain.mkdir()
    (chain / "0").symlink_to(os.path.relpath(target, chain))
    for index in range(1, links - 1):
        (chain / str(index)).symlink_to(str(index - 1))
    link = root / "lean-toolchain"
    link.unlink()
    link.symlink_to(os.path.relpath(chain / str(links - 2), root))
    if extra:
        with pytest.raises(OSError) as error:
            link.read_bytes()
        assert error.value.errno == errno.ELOOP
    else:
        link.read_bytes()

    result = inspect_project(root)

    assert _unreadable(result) == (["lean-toolchain"] if extra else [])
    assert "project-changed-during-inspection" not in _codes(result)


def test_autoform_paths_need_their_exact_spelling(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "Blueprint").mkdir()  # a Lean library, not the vault
    (root / "mkdocs.yml").write_text("", encoding="utf-8")

    assert inspect_project(root).autoform_paths == ("mkdocs.yml",)

    (root / "Blueprint").rename(root / "blueprint")
    assert inspect_project(root).autoform_paths == ("blueprint", "mkdocs.yml")

    (root / "blueprint").rmdir()
    (root / "blueprint").write_text("", encoding="utf-8")
    assert inspect_project(root).autoform_paths == ("mkdocs.yml",)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions that apply")
def test_unsearchable_autoform_directory_reads_as_absent(tmp_path: Path) -> None:
    root = _project(tmp_path)
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "autoform-verify.yml").write_text("", encoding="utf-8")
    (workflows / "blueprint-pages.yml").write_text("", encoding="utf-8")
    workflows.chmod(0o644)  # listable, but its entries cannot be stat'ed
    try:
        result = inspect_project(root)
    finally:
        workflows.chmod(0o755)

    assert result.autoform_paths == ()
    assert result.compatibility.status == "supported"


def test_bundled_catalog_lists_the_recommended_release() -> None:
    catalog = load_release_catalog()

    assert catalog.recommended.id == "lean-v4.32.2-mathlib-v4.32.2"
    assert catalog.recommended.mathlib_commit == COMMIT
    assert json.loads(catalog.to_json())["schema"] == RELEASE_CATALOG_SCHEMA


def _release(**changes: object) -> dict:
    release = {
        "id": "a",
        "recommended": True,
        "lean_toolchain": "leanprover/lean4:v4.32.2",
        "mathlib_git": MATHLIB_URL,
        "mathlib_rev": "v4.32.2",
        "mathlib_commit": COMMIT,
    }
    return {**release, **changes}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema": "other", "releases": [_release()]},
        {"schema": RELEASE_CATALOG_SCHEMA, "releases": []},
        {"schema": RELEASE_CATALOG_SCHEMA, "releases": [_release(extra=1)]},
        {"schema": RELEASE_CATALOG_SCHEMA, "releases": [_release(mathlib_commit="v4.32.2")]},
        {"schema": RELEASE_CATALOG_SCHEMA, "releases": [_release(recommended="yes")]},
        {"schema": RELEASE_CATALOG_SCHEMA, "releases": [_release(), _release(id="b", recommended=False)]},
        {"schema": RELEASE_CATALOG_SCHEMA, "releases": [_release(recommended=False)]},
    ],
)
def test_malformed_catalogs_are_rejected(payload: object) -> None:
    with pytest.raises(ProjectCatalogError):
        parse_release_catalog(payload)


def test_cli_reports_and_exit_codes(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)

    assert main(["project", "inspect", str(root)]) == 0
    captured = capsys.readouterr()
    assert "Compatibility: supported (lean-v4.32.2-mathlib-v4.32.2)" in captured.out
    assert "lean_lib Example" in captured.out

    assert main(["project", "inspect", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert main(["project", "versions"]) == 0
    assert "lean-v4.32.2-mathlib-v4.32.2 [recommended]" in capsys.readouterr().out

    (root / "lean-toolchain").unlink()
    assert main(["project", "inspect", str(root)]) == 1
    assert "error[missing-lean-toolchain]" in capsys.readouterr().err


def test_human_report_escapes_characters_that_could_forge_lines(tmp_path: Path, capsys) -> None:
    lakefile = LAKEFILE.replace('name = "Example"', 'name = "Ex\\u202eample\\u009b\\n"')
    root = _project(tmp_path, lakefile=lakefile)

    assert main(["project", "inspect", str(root)]) == 0

    assert "Lake: Ex\\u202eample\\x9b\\n 0.1.0 (lakefile.toml)" in capsys.readouterr().out
    assert _human_text("\U000e0001") == "\\U000e0001"
    # Readable names stay readable; only `project new` escapes to ASCII.
    assert _human_text("Th\N{LATIN SMALL LETTER E WITH ACUTE}or\N{LATIN SMALL LETTER E WITH GRAVE}me") == (
        "Th\N{LATIN SMALL LETTER E WITH ACUTE}or\N{LATIN SMALL LETTER E WITH GRAVE}me"
    )
