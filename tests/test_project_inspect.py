from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import autoform_cli.project._lake_metadata as project_lake_metadata
import autoform_cli.project.catalog as project_catalog
import autoform_cli.project.inspect as project_inspect
import autoform_cli.project._snapshot as project_snapshot
from autoform_cli.__main__ import _human_text, main
from autoform_cli.project import (
    PROJECT_INSPECTION_SCHEMA,
    RELEASE_CATALOG_SCHEMA,
    ProjectCatalogError,
    inspect_project,
    load_release_catalog,
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

    assert result.lake.name == "Example"
    assert [(target.kind, target.name) for target in result.lake.targets] == [
        ("lean_lib", "Example"),
        ("lean_exe", "example"),
    ]
    assert result.lean_toolchain == "leanprover/lean4:v4.32.2"
    assert result.diagnostics == ()
    # The JSON report keeps a stable shape.
    payload = json.loads(result.to_json())
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
    assert (payload["mathlib"]["input_rev"], payload["mathlib"]["rev"]) == ("v4.32.2", COMMIT)


@pytest.mark.parametrize(
    "project",
    [
        pytest.param({"toolchain": "v4.32.2\n"}, id="elan-v-prefix"),
        pytest.param({"toolchain": "4.32.2\n"}, id="elan-bare-version"),
        pytest.param({"toolchain": "leanprover/lean4:4.32.2\n"}, id="elan-origin-without-v"),
        pytest.param(
            {"manifest": (_mathlib(url="HTTPS://GITHUB.COM/leanprover-community/mathlib4"),)}, id="url-scheme-host-case"
        ),
        pytest.param({"manifest": (_mathlib(url=MATHLIB_URL + ".git"),)}, id="url-dot-git"),
        pytest.param({"manifest": (_mathlib(url=MATHLIB_URL + "/"),)}, id="url-trailing-slash"),
        pytest.param({"manifest": (_mathlib(subDir="./"),)}, id="subdir-current-directory"),
        pytest.param({"manifest": (_mathlib(rev=COMMIT.upper()),)}, id="uppercase-commit"),
        pytest.param({"manifest": (_mathlib(rev=OTHER_COMMIT), _mathlib())}, id="last-duplicate-manifest-entry-wins"),
        pytest.param(
            {"lakefile": 'name = "Example"\n\n[[require]]\nname = "mathlib"\nrev = "v4.31.0"\n'},
            id="source-less-requirement-is-not-compared-with-the-lock",
        ),
    ],
)
def test_projects_lake_resolves_to_the_catalog_pair_are_supported(tmp_path: Path, project: dict) -> None:
    result = inspect_project(_project(tmp_path, **project))

    assert result.compatibility.status == "supported"
    assert result.compatibility.release == "lean-v4.32.2-mathlib-v4.32.2"
    assert "lake-manifest-stale" not in _codes(result)


def test_other_toolchain_is_unlisted(tmp_path: Path) -> None:
    result = inspect_project(_project(tmp_path, toolchain="leanprover/lean4:v4.33.0\n"))

    assert result.ok
    assert result.compatibility.status == "unlisted"
    assert "release-unlisted" in _codes(result)


@pytest.mark.parametrize(
    ("requirement", "lock", "status"),
    [
        (f'git = "{MATHLIB_URL}"\nrev = "v4.32.2"', _mathlib(rev=OTHER_COMMIT, input_rev="v4.31.0"), "unlisted"),
        (f'git = "{MATHLIB_URL}"\nrev = "v4.31.0"', _mathlib(), "supported"),
        (f'git = "{MATHLIB_URL}"', _mathlib(), "supported"),
        ('git = "https://github.com/someone/mathlib4"\nrev = "v4.32.2"', _mathlib(), "supported"),
        ('path = "../mathlib4"\nrev = "v4.32.2"', _mathlib(), "supported"),
    ],
    ids=["lakefile-moved-ahead", "older-revision", "git-without-revision", "other-git-url", "path-requirement"],
)
def test_stale_requirement_reports_the_locked_pair(tmp_path: Path, requirement: str, lock: dict, status: str) -> None:
    # Lake builds the lock, so the manifest decides the pair whatever Mathlib the lakefile asks for.
    lakefile = LAKEFILE.replace('scope = "leanprover-community"\nrev = "v4.32.2"', requirement)
    result = inspect_project(_project(tmp_path, lakefile=lakefile, manifest=(lock,)))

    assert result.compatibility.status == status
    assert "lake-manifest-stale" in _codes(result)


LOOM_LAKEFILE = 'name = "Example"\n\n[[require]]\nname = "loom"\ngit = "https://example.com/loom"\n'
LOOM = _mathlib(name="loom", url="https://example.com/loom", rev=OTHER_COMMIT, input_rev=None)


def _assert_mathlib_unused(result) -> None:
    """A locked Mathlib that no requirement pulls in decides no release pair."""

    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert "mathlib-manifest-unused" in _codes(result)


@pytest.mark.parametrize("inherited", [False, True], ids=["direct-lock", "inherited-lock"])
def test_mathlib_under_other_requirements_is_not_proof_of_use(tmp_path: Path, inherited: bool) -> None:
    manifest = (LOOM, {**_mathlib(), "inherited": inherited})
    result = inspect_project(_project(tmp_path, lakefile=LOOM_LAKEFILE, manifest=manifest))

    _assert_mathlib_unused(result)
    (unused,) = [d for d in result.diagnostics if d.code == "mathlib-manifest-unused"]
    assert "does not build" not in unused.message and "dependency" in unused.message


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


@pytest.mark.parametrize(
    ("locked", "override_inherited"),
    [
        pytest.param((), True, id="unrecorded-inherited-override"),
        pytest.param((_mathlib(rev=OTHER_COMMIT, inherited=True),), False, id="inherited-lock-direct-override"),
        pytest.param((_mathlib(rev=OTHER_COMMIT, inherited=True),), True, id="inherited-lock-inherited-override"),
    ],
)
def test_override_is_not_proof_of_use(tmp_path: Path, locked: tuple[dict, ...], override_inherited: bool) -> None:
    root = _project(tmp_path, lakefile=LOOM_LAKEFILE, manifest=(LOOM, *locked))
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
    [
        {"subDir": "Archive"},
        {"configFile": "alternate.lean"},
        {"manifestFile": "other.json"},
        {"url": "https://github.com/someone/mathlib4"},
    ],
    ids=["sub-dir", "config-file", "manifest-file", "fork-with-the-catalog-commit"],
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


@pytest.mark.parametrize(
    ("url", "redacted"),
    [
        (MATHLIB_URL.replace("://", "://user:hunter2@"), MATHLIB_URL.replace("://", "://***@")),
        (MATHLIB_URL + "?token=hunter2#hunter2", MATHLIB_URL + "?***"),
        (MATHLIB_URL + "#hunter2", MATHLIB_URL + "#***"),
        ("https://example.com?user=alice@corp.example&token=hunter2", "https://***"),
        ("https://example.com#me@corp.example&token=hunter2", "https://***"),
        ("https://alice:hun#ter2@github.com/leanprover-community/mathlib4", "https://***"),
        ("https://alice:hun?ter2@github.com/leanprover-community/mathlib4?token=hunter2", "https://***"),
    ],
    ids=[
        "credentials",
        "query",
        "fragment",
        "query-with-at",
        "fragment-with-at",
        "fragment-in-password",
        "query-in-password",
    ],
)
@pytest.mark.parametrize("source", ["lake-manifest.json", ".lake/package-overrides.json"], ids=["manifest", "override"])
def test_url_secrets_are_redacted_and_never_match(tmp_path: Path, capsys, source: str, url: str, redacted: str) -> None:
    # For the catalog's URL, dropping the credentials, query or fragment would leave a URL that reports `supported`.
    # Masking the credentials first would swallow the marker, and masking the query first would expose `alice:hun`.
    if source == "lake-manifest.json":
        root = _project(tmp_path, manifest=(_mathlib(url=url),))
    else:
        root = _project(tmp_path)
        _write_overrides(root, _mathlib(url=url))

    result = inspect_project(root)
    main(["project", "inspect", str(root)])
    main(["project", "inspect", str(root), "--json"])
    captured = capsys.readouterr()

    assert result.mathlib.source == source
    assert result.mathlib.url == redacted
    assert result.compatibility.status == "unlisted"
    assert f"Mathlib: v4.32.2 @ {COMMIT} ({redacted}) [{source}]" in captured.out
    output = result.to_json() + captured.out + captured.err
    assert not any(secret in output for secret in ("hunter2", "ter2", "alice"))


@pytest.mark.parametrize(
    ("requested", "locked", "stale"),
    [
        (MATHLIB_URL + "?token=a", MATHLIB_URL + "?token=b", True),
        (MATHLIB_URL.replace("://", "://token=a@"), MATHLIB_URL.replace("://", "://token=b@"), True),
        (MATHLIB_URL.replace("://", "://token=a@"), MATHLIB_URL.replace("://", "://token=a@"), False),
    ],
    ids=["query", "credentials", "same-credentials"],
)
def test_requirement_git_url_is_compared_with_the_lock_before_redaction(
    tmp_path: Path, capsys, requested: str, locked: str, stale: bool
) -> None:
    # Lake compares the URLs as written: equal redacted URLs do not make the lock current, and equal secret URLs do.
    lakefile = LAKEFILE.replace('scope = "leanprover-community"', f'git = "{requested}"')
    root = _project(tmp_path, lakefile=lakefile, manifest=(_mathlib(url=locked),))

    result = inspect_project(root)
    main(["project", "inspect", str(root)])
    main(["project", "inspect", str(root), "--json"])
    captured = capsys.readouterr()

    assert result.compatibility.status == "unlisted"
    assert ("lake-manifest-stale" in _codes(result)) is stale
    output = result.to_json() + captured.out + captured.err
    assert "token=a" not in output and "token=b" not in output


def test_escaped_mathlib_spelling_is_the_same_package(tmp_path: Path) -> None:
    lakefile = LAKEFILE.replace('name = "mathlib"', 'name = "«mathlib»"')
    result = inspect_project(_project(tmp_path, lakefile=lakefile, manifest=(_mathlib(name="«mathlib»"),)))

    assert result.compatibility.status == "supported"


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


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "other", "type": "path", "inherited": False},
        {"name": "other", "type": "path", "inherited": True, "dir": None},
        {"name": "other", "type": "path", "inherited": True, "dir": 7},
        {"name": 7},
    ],
    ids=["path-without-dir", "dir-null", "dir-not-a-string", "name-not-a-string"],
)
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


@pytest.mark.parametrize(
    ("value", "parts"),
    [
        # As in Lean, and unlike lean.py's source grammar, a quoted component may hold `«`.
        ("««a»", (("str", "«a"),)),
        ("«»", (("str", ""),)),
        ("xⱼ", (("str", "xⱼ"),)),
        ("Qq.01", (("str", "Qq"), ("num", "1"))),
        ("λ", None),
        ("a.Π", None),
        ("a.", None),
        ("a..b", None),
        ("«a", None),
        ("«a»b", None),
        ("1a", None),
    ],
)
def test_lake_names_split_as_lean_splits_them(value: str, parts) -> None:
    assert project_lake_metadata._split_lean_name(value) == parts


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


@pytest.mark.parametrize("version", [7, "1.3.0", "1.9.0"])
def test_integer_and_newer_1x_manifest_versions_are_read(tmp_path: Path, version: object) -> None:
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


def test_override_is_not_parsed_on_lakes_no_manifest_update_path(tmp_path: Path) -> None:
    root = _project(tmp_path, manifest=None)
    _write_overrides(root, _mathlib())

    assert inspect_project(root).compatibility.status == "indeterminate"

    (root / ".lake/package-overrides.json").write_text("{", encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "indeterminate"
    assert "invalid-package-overrides" not in _codes(result)


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


def test_invalid_override_file_blocks_a_supported_answer(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".lake").mkdir()
    (root / ".lake/package-overrides.json").write_text("{", encoding="utf-8")

    result = inspect_project(root)

    assert not result.ok
    assert result.compatibility.status == "indeterminate"
    assert "invalid-package-overrides" in _codes(result)
    assert "invalid-lake-manifest" not in _codes(result)


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
    assert "unsupported-package-overrides" in _codes(result)
    warning = next(
        diagnostic for diagnostic in result.diagnostics if diagnostic.code == "unsupported-package-overrides"
    )
    assert "package-overrides.json" in warning.message
    assert "lake update" not in warning.message


@pytest.mark.parametrize(
    ("lakefile", "inherited"),
    [(LAKEFILE, False), (None, True)],
    ids=["beside-lakefile-toml", "alone-it-cannot-confirm-an-inherited-mathlib"],
)
def test_lakefile_lean_takes_precedence_and_is_not_evaluated(
    tmp_path: Path, lakefile: str | None, inherited: bool
) -> None:
    root = _project(tmp_path, lakefile=lakefile, manifest=(_mathlib(inherited=inherited),))
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
        # Python's str.strip() trims the C0 separators \x1c-\x1f, but elan does not.
        ("\x1cleanprover/lean4:v4.32.2\n", False),
        ("leanprover/lean4:v4.32.2\x1c\n", False),
        ("\x1fleanprover/lean4:v4.32.2\n", False),
        ("leanprover/lean4:v4.32.2\x1f\n", False),
    ],
)
def test_toolchain_follows_elans_first_line_rule(tmp_path: Path, toolchain: str, ok: bool) -> None:
    # Checked against pinned elan 4.2.3: it trims the first line and rejects the file when that line is malformed.
    result = inspect_project(_project(tmp_path, toolchain=toolchain))

    assert result.ok is ok
    assert (result.lean_toolchain == "leanprover/lean4:v4.32.2") is ok
    if not ok:
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
        # Input targets share Lake's target namespace (implB corpus tgt-dup-lib-dir, tgt-dup-exe-dir,
        # e-input-dup-file-dir, tgt-file-no-name, tgt-file-not-array).
        pytest.param('name = "E"\n[[lean_lib]]\nname = "A"\n[[input_dir]]\nname = "A"\npath = "d"\n', id="lib-dir"),
        pytest.param('name = "E"\n[[lean_exe]]\nname = "a"\n[[input_file]]\nname = "a"\npath = "x"\n', id="exe-file"),
        pytest.param(
            'name = "E"\n[[input_file]]\nname = "x"\npath = "x"\n[[input_dir]]\nname = "x"\npath = "d"\n', id="file-dir"
        ),
        pytest.param('name = "E"\n[[lean_lib]]\nname = "A"\n[[input_file]]\npath = "x"\n', id="unnamed-input"),
        pytest.param('name = "E"\ninput_dir = 5\n[[lean_lib]]\nname = "A"\n', id="input-not-tables"),
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


@pytest.mark.parametrize(
    ("plain", "escaped"), [("A", "«A»"), ("", "«»"), ("a b", "«a b»"), ("[anonymous]", "«[anonymous]»")]
)
def test_duplicate_target_names_are_compared_as_lean_names(tmp_path: Path, plain: str, escaped: str) -> None:
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


def test_decision_files_are_captured_byte_for_byte(tmp_path: Path) -> None:
    # Windows opens a file in text mode unless asked otherwise, which turns
    # CRLF into LF and stops reading at 0x1A.
    root = _project(tmp_path)
    data = b"leanprover/lean4:v4.32.2\r\n\x1a\n"
    (root / "lean-toolchain").write_bytes(data)

    assert project_snapshot.capture_decision_snapshot(root).file("lean-toolchain").content == data


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
    original = project_snapshot.os.open

    def deny_one(path, *args, **kwargs):
        if Path(path) == root / denied:
            raise PermissionError(denied)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(project_snapshot.os, "open", deny_one)

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


@pytest.mark.skipif(not hasattr(os, "mkfifo") or sys.platform == "win32", reason="needs FIFOs and symlinks")
@pytest.mark.parametrize("linked", [False, True], ids=["fifo", "link-to-fifo"])
def test_fifo_configuration_is_never_opened(tmp_path: Path, linked: bool) -> None:
    root = _project(tmp_path, lakefile=None)
    fifo = tmp_path / "lakefile-fifo" if linked else root / "lakefile.toml"
    os.mkfifo(fifo)
    if linked:
        (root / "lakefile.toml").symlink_to(fifo)

    result = _run_fifo_probe(root, "inspect")["result"]

    assert not result["ok"]
    assert any(item["code"] == "unreadable-file" and item["path"] == "lakefile.toml" for item in result["diagnostics"])
    assert "project-changed-during-inspection" not in {item["code"] for item in result["diagnostics"]}


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


def test_a_project_that_keeps_changing_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    original = project_snapshot._capture_file
    writes = 0

    def capture_and_rewrite(captured_root: Path, relative: str):
        nonlocal writes
        if relative == "lean-toolchain":
            writes += 1
            (root / "lean-toolchain").write_text(f"leanprover/lean4:v4.32.{writes}\n", encoding="utf-8")
        return original(captured_root, relative)

    monkeypatch.setattr(project_snapshot, "_capture_file", capture_and_rewrite)

    result = inspect_project(root)

    assert result.compatibility.status == "indeterminate"
    assert "project-changed-during-inspection" in _codes(result)


def test_new_nearer_project_root_forces_a_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outer = _project(tmp_path)
    inner = outer / "nested"
    inner.mkdir()
    original = project_snapshot.capture_decision_snapshot
    inserted = False

    def capture_after_inserting_root(root: Path):
        nonlocal inserted
        if not inserted:
            inserted = True
            (inner / "lakefile.toml").write_text(LAKEFILE, encoding="utf-8")
            (inner / "lean-toolchain").write_text("leanprover/lean4:v4.31.0\n", encoding="utf-8")
            _write_manifest(inner, _mathlib())
        return original(root)

    monkeypatch.setattr(project_snapshot, "capture_decision_snapshot", capture_after_inserting_root)

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
        return False if path.parent == inner and path.name in project_snapshot.ROOT_MARKERS else original_exists(path)

    def suppressed_is_symlink(path: Path) -> bool:
        return False if path.parent == inner and path.name in project_snapshot.ROOT_MARKERS else original_is_symlink(path)

    monkeypatch.setattr(Path, "exists", suppressed_exists)
    monkeypatch.setattr(Path, "is_symlink", suppressed_is_symlink)

    result = inspect_project(inner)

    assert result.project_root == "."
    assert result.lean_toolchain == "leanprover/lean4:v4.31.0"
    assert result.compatibility.status == "unlisted"


def test_autoform_paths_are_revalidated_with_the_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    (root / "blueprint").mkdir()
    original = project_snapshot.capture_decision_snapshot
    removed = False

    def capture_after_removing_blueprint(captured_root: Path):
        nonlocal removed
        if not removed:
            removed = True
            (root / "blueprint").rmdir()
        return original(captured_root)

    monkeypatch.setattr(project_snapshot, "capture_decision_snapshot", capture_after_removing_blueprint)

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
    # fstat refuses the FIFO the open reached, so not even the first read takes it for an empty file.
    assert probe["captures"][0] == "unreadable"
    assert not result["ok"]
    assert result["compatibility"]["status"] == "indeterminate"
    assert "unreadable-file" in {item["code"] for item in result["diagnostics"]}


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
@pytest.mark.parametrize(
    ("moved", "with_override"),
    [(".lake", False), (".lake", True), (".lake/package-overrides.json", True)],
    ids=["lake-directory", "lake-directory-with-override", "overrides-file"],
)
def test_symlinked_lake_paths_are_followed_like_lake(tmp_path: Path, moved: str, with_override: bool) -> None:
    root = _project(tmp_path)
    if with_override:
        _write_overrides(root, {"name": "mathlib", "type": "path", "dir": "../mathlib4", "inherited": False})
    else:
        (root / ".lake").mkdir()
    _move_behind_symlink(root, moved)

    result = inspect_project(root)

    assert "unreadable-file" not in _codes(result)
    if with_override:
        assert result.mathlib.source == ".lake/package-overrides.json"
        assert "mathlib-overridden" in _codes(result)
    else:
        assert result.ok, result.diagnostics
        assert result.compatibility.status == "supported"


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


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions that apply")
def test_a_root_that_can_be_searched_but_not_listed_is_inspected(tmp_path: Path) -> None:
    # Every run used to report project-changed-during-inspection here.
    root = _project(tmp_path)
    root.chmod(0o311)
    try:
        result = inspect_project(root)
    finally:
        root.chmod(0o755)

    assert result.ok, result.diagnostics
    assert result.compatibility.status == "supported"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions that apply")
def test_a_root_that_cannot_be_searched_is_unreadable_not_missing(tmp_path: Path) -> None:
    # Nothing under it can be looked up, so its decision files may exist.
    root = _project(tmp_path)
    root.chmod(0)
    try:
        result = inspect_project(root)
    finally:
        root.chmod(0o755)

    assert not result.ok
    assert "unreadable-file" in _codes(result)
    assert "project-not-found" not in _codes(result)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions that apply")
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
def test_decision_file_link_the_kernel_refuses_is_present_but_unreadable(tmp_path: Path) -> None:
    # A dangling link is present, not missing.
    root = _project(tmp_path)
    target = _move_behind_symlink(root, "lean-toolchain")
    link = root / "lean-toolchain"
    link.unlink()
    os.symlink(f"{target}-absent", link)
    with pytest.raises(FileNotFoundError):
        link.read_bytes()

    result = inspect_project(root)

    assert not result.ok
    assert _unreadable(result) == ["lean-toolchain"]
    assert "missing-lean-toolchain" not in _codes(result)
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
        {"schema": RELEASE_CATALOG_SCHEMA, "releases": [_release(recommended="yes")]},
        {"schema": RELEASE_CATALOG_SCHEMA, "releases": [_release(recommended=False)]},
    ],
)
def test_malformed_catalogs_are_rejected(payload: object) -> None:
    with pytest.raises(ProjectCatalogError):
        project_catalog.parse_release_catalog(payload)


def test_cli_reports_an_unreadable_release_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    (tmp_path / "releases.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(project_catalog, "files", lambda _package: tmp_path)

    assert main(["project", "versions", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": {"code": "project-catalog-invalid", "message": "the bundled release catalog is unreadable"},
        "ok": False,
    }


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


@pytest.mark.parametrize("input_rev", [None, ""], ids=["missing", "empty"])
def test_human_report_omits_a_missing_input_revision(tmp_path: Path, capsys, input_rev: str | None) -> None:
    root = _project(tmp_path, manifest=(_mathlib(input_rev=input_rev),))

    assert main(["project", "inspect", str(root)]) == 0

    captured = capsys.readouterr()
    assert f"Mathlib: {COMMIT} ({MATHLIB_URL}) [lake-manifest.json]" in captured.out
    assert "None" not in captured.out + captured.err


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
