from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

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
    result = inspect_project(_project(tmp_path, manifest=(old,)))

    assert result.compatibility.status == "unlisted"
    assert "lake-manifest-stale" in _codes(result)


def test_stale_requirement_still_reports_the_locked_catalog_pair(tmp_path: Path) -> None:
    lakefile = LAKEFILE.replace('rev = "v4.32.2"', 'rev = "v4.31.0"')
    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert result.compatibility.status == "supported"
    assert "lake-manifest-stale" in _codes(result)


def test_requirement_git_url_is_compared_with_the_lock(tmp_path: Path) -> None:
    lakefile = LAKEFILE.replace('scope = "leanprover-community"', 'git = "https://github.com/someone/mathlib4"')
    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert "lake-manifest-stale" in _codes(result)


def test_last_duplicate_manifest_entry_wins(tmp_path: Path) -> None:
    manifest = (_mathlib(rev=OTHER_COMMIT), _mathlib())
    result = inspect_project(_project(tmp_path, manifest=manifest))

    assert result.compatibility.status == "supported"


def test_transitive_mathlib_still_decides_compatibility(tmp_path: Path) -> None:
    inherited = {**_mathlib(), "inherited": True}
    lakefile = 'name = "Example"\n\n[[require]]\nname = "loom"\ngit = "https://example.com/loom"\n'
    result = inspect_project(_project(tmp_path, lakefile=lakefile, manifest=(inherited,)))

    assert result.compatibility.status == "supported"


def test_direct_lock_without_a_requirement_is_unused(tmp_path: Path) -> None:
    result = inspect_project(_project(tmp_path, lakefile='name = "Example"\n'))

    assert result.mathlib is None
    assert result.compatibility.status == "indeterminate"
    assert "mathlib-manifest-unused" in _codes(result)


@pytest.mark.parametrize(
    "fields",
    [{"subDir": "Archive"}, {"configFile": "alternate.lean"}, {"configFile": None}, {"manifestFile": "other.json"}],
)
def test_lock_must_load_mathlib_the_way_releases_do(tmp_path: Path, fields: dict) -> None:
    result = inspect_project(_project(tmp_path, manifest=(_mathlib(**fields),)))

    assert result.compatibility.status == "unlisted"


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
    result = inspect_project(_project(tmp_path, manifest=(plausible,)))

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
    root = _project(tmp_path)
    (root / "lake-manifest.json").write_text(f'{{"version": "1.1.0", {packages}}}', encoding="utf-8")

    result = inspect_project(root)

    assert result.ok
    assert result.compatibility.status == "indeterminate"


def test_integer_manifest_versions_are_read(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write_manifest(root, _mathlib(), version=7)

    assert inspect_project(root).compatibility.status == "supported"


def test_package_override_replaces_the_locked_mathlib(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".lake").mkdir()
    (root / ".lake/package-overrides.json").write_text(
        json.dumps(
            {
                "schemaVersion": "1.1.0",
                "packages": [{"name": "mathlib", "type": "path", "dir": "../mathlib4", "inherited": False}],
            }
        ),
        encoding="utf-8",
    )

    result = inspect_project(root)

    assert result.mathlib.type == "path"
    assert result.mathlib.source == ".lake/package-overrides.json"
    assert result.compatibility.status == "indeterminate"
    assert "mathlib-overridden" in _codes(result)


def test_override_needs_a_manifest_to_replace(tmp_path: Path) -> None:
    root = _project(tmp_path, manifest=None)
    (root / ".lake").mkdir()
    (root / ".lake/package-overrides.json").write_text(
        json.dumps({"schemaVersion": "1.1.0", "packages": [_mathlib()]}), encoding="utf-8"
    )

    assert inspect_project(root).compatibility.status == "indeterminate"


def test_override_without_mathlib_leaves_the_manifest_in_charge(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".lake").mkdir()
    (root / ".lake/package-overrides.json").write_text(
        json.dumps({"schemaVersion": "1.1.0", "packages": []}), encoding="utf-8"
    )

    assert inspect_project(root).compatibility.status == "supported"


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


def test_lakefile_lean_takes_precedence_and_is_not_evaluated(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lakefile.lean").write_text('#eval IO.println "never run"\n', encoding="utf-8")

    result = inspect_project(root)

    assert result.lake.config == "lakefile.lean"
    assert result.lake.name is None
    assert result.mathlib is None
    assert "lakefile-lean-not-evaluated" in _codes(result)
    assert result.compatibility.status == "indeterminate"


@pytest.mark.parametrize(
    ("toolchain", "ok"),
    [
        ("leanprover/lean4:v4.32.2", True),
        ("leanprover/lean4:v4.32.2\n\n", True),
        ("leanprover/lean4:v4.32.2\r\n", True),
        (" leanprover/lean4:v4.32.2\t\n", True),
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
    # Checked against elan 4.2.0: it trims the first line and ignores the file when that line is malformed.
    result = inspect_project(_project(tmp_path, toolchain=toolchain))

    assert result.ok is ok
    assert (result.lean_toolchain == "leanprover/lean4:v4.32.2") is ok


def test_missing_toolchain_and_lakefile_are_errors(tmp_path: Path) -> None:
    root = _project(tmp_path, lakefile=None, toolchain=None)
    (root / "lakefile.toml").mkdir()  # still marks the root, but cannot be read

    result = inspect_project(root)

    assert not result.ok
    assert {"missing-lean-toolchain", "unreadable-file"} <= _codes(result)
    assert result.compatibility.status == "indeterminate"


@pytest.mark.parametrize(
    "lakefile",
    [
        "name = \n",
        'version = "0.1.0"\n',
        'name = ""\n',
        'name = "E"\nversion = "wat"\n',
        'name = "E"\nrequire = 5\n',
        'name = "E"\n[[lean_lib]]\n',
        'name = "E"\n[[lean_lib]]\nname = "A"\n[[lean_exe]]\nname = "A"\n',
    ],
)
def test_lakefiles_lake_refuses_are_errors(tmp_path: Path, lakefile: str) -> None:
    # Each case checked against Lake 4.32.0, which refuses to load it.
    result = inspect_project(_project(tmp_path, lakefile=lakefile))

    assert not result.ok
    assert "invalid-lakefile-toml" in _codes(result)


@pytest.mark.parametrize("lakefile", ['name = "my-project"\n', 'name = "E"\nsrcDir = "../outside"\n'])
def test_lakefiles_lake_accepts_are_fine(tmp_path: Path, lakefile: str) -> None:
    assert inspect_project(_project(tmp_path, lakefile=lakefile + LAKEFILE.split("\n\n", 1)[1])).ok


def test_oversized_and_non_utf8_files_are_errors(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lake-manifest.json").write_bytes(b" " * (1024 * 1024 + 1))
    (root / "lean-toolchain").write_bytes(b"\xff\n")

    result = inspect_project(root)

    assert [diagnostic.path for diagnostic in result.diagnostics if diagnostic.code == "unreadable-file"] == [
        "lake-manifest.json",
        "lean-toolchain",
    ]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_fifo_configuration_is_never_opened(tmp_path: Path) -> None:
    root = _project(tmp_path, lakefile=None)
    os.mkfifo(root / "lakefile.toml")

    result = inspect_project(root)  # would block forever if opened

    assert "unreadable-file" in _codes(result)


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
