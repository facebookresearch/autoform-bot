from __future__ import annotations

import errno
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli import scaffold as scaffold_module
from autoform_cli.__main__ import main
from autoform_cli.graph import load_graph
from autoform_cli.project import ProjectCreateError, create_project, inspect_project, load_release_catalog
from autoform_cli.project import create as create_module
from autoform_cli.scaffold import DEFAULT_AUTOFORM_SOURCE

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

_RELEASE = "lean-v4.32.2-mathlib-v4.32.2"
_SOURCE = "https://example.test/owner/autoform.git"
_CORE_FILES = ("lean-toolchain", "lakefile.toml", "lake-manifest.json", "src/Project.lean")
_UNLISTED_LAKEFILE = (
    'name = "Project"\n'
    'version = "0.1.0"\n'
    'defaultTargets = ["Project"]\n\n'
    "[[require]]\n"
    'name = "mathlib"\n'
    'git = "https://github.com/leanprover-community/mathlib4"\n'
    'rev = "v4.30.0"\n\n'
    "[[lean_lib]]\n"
    'name = "Project"\n'
    'srcDir = "src"\n'
)


@pytest.fixture(autouse=True)
def _no_checkout_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without explicit flags, creation pins workflows to the running checkout
    # like `autoform init`; default to no pin so results do not depend on
    # whether this tree has an `origin` remote.
    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda _templates=None: ("", ""))


def _fails(target: str | Path, code: str, **options: object) -> ProjectCreateError:
    """Expect *code* from creating *target*, whatever the attempt left behind."""

    with pytest.raises(ProjectCreateError) as raised:
        create_project(target, **{"package": "Project", "release_id": _RELEASE, **options})
    assert raised.value.code == code
    return raised.value


def _refused(target: Path, code: str, **options: object) -> ProjectCreateError:
    """Expect *code* from creating *target*, leaving its parent exactly as it was: no target, no stage."""

    before = sorted(target.parent.iterdir())
    error = _fails(target, code, **options)
    assert sorted(target.parent.iterdir()) == before
    return error


@pytest.mark.parametrize(
    ("pin", "options", "expected"),
    [
        ((_SOURCE, "b" * 40), {}, (_SOURCE, "b" * 40)),
        ((_SOURCE, "b" * 40), {"autoform_ref": "1" * 40}, (_SOURCE, "1" * 40)),
        (("", ""), {"autoform_ref": "1" * 40}, (DEFAULT_AUTOFORM_SOURCE, "1" * 40)),
        (("", ""), {"autoform_source": _SOURCE, "autoform_ref": "A" * 40}, (_SOURCE, "a" * 40)),
    ],
    ids=["checkout-pin", "ref-keeps-checkout-source", "ref-uses-default-source", "explicit-pin"],
)
def test_workflow_pin_resolution(
    pin: tuple[str, str],
    options: dict[str, str],
    expected: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda _templates=None: pin)
    target = tmp_path / "Project"

    result = create_project(target, package="Project", release_id=_RELEASE, **options)

    assert result.workflows_pinned
    workflow = (target / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert f'AUTOFORM_SOURCE: "{expected[0]}"' in workflow
    assert f'AUTOFORM_REF: "{expected[1]}"' in workflow


@pytest.mark.parametrize(
    ("source", "revision"),
    [("", ""), ("https://user:secret@example.test/owner/autoform.git", "b" * 40), (_SOURCE, "main"), ("", "b" * 40)],
)
def test_creation_omits_workflows_without_a_usable_checkout_pin(
    source: str, revision: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda _templates=None: (source, revision))
    target = tmp_path / "Project"

    result = create_project(target, package="Project", release_id=_RELEASE)

    assert not result.workflows_pinned
    assert (target / ".github/CODEOWNERS.autoform.example").is_file()
    assert not (target / ".github/autoform_audit.py").exists()
    assert not (target / ".github/workflows").exists()


def test_an_explicit_source_never_inherits_the_checkout_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("an explicit source consulted the checkout pin")

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
    target = tmp_path / "Project"

    result = create_project(target, package="Project", release_id=_RELEASE, autoform_source=_SOURCE)

    assert not result.workflows_pinned
    assert (target / ".github/CODEOWNERS.autoform.example").is_file()
    assert not (target / ".github/autoform_audit.py").exists()
    assert not (target / ".github/workflows").exists()


@pytest.mark.parametrize(
    ("source", "revision"),
    [
        ("https://example.test/owner/autoform.git", "main"),
        ("https://user:secret@example.test/autoform.git", "1" * 40),
        ("http://example.test/owner/autoform.git", "1" * 40),
        ("https://example.test/owner/autoform.git?", "1" * 40),
        ("https://example.test/owner/autoform.git#", "1" * 40),
        ("", "1" * 12),
    ],
)
def test_creation_rejects_an_invalid_workflow_pin_before_writing(source: str, revision: str, tmp_path: Path) -> None:
    _refused(tmp_path / "Project", "project-workflow-pin-invalid", autoform_source=source, autoform_ref=revision)


@pytest.mark.parametrize("versions", [{"release_id": _RELEASE}, {"release_id": None, "lean_toolchain": "v4.30.0"}])
def test_creation_with_an_explicit_pin_stays_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, versions: dict[str, str | None]
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("project new crossed its offline boundary")

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    for name in (
        "system",
        "fork",
        "posix_spawn",
        "posix_spawnp",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "execl",
        "execle",
        "execlp",
        "execlpe",
    ):
        monkeypatch.setattr(os, name, forbidden, raising=False)

    result = create_project(
        tmp_path / "Project",
        package="Project",
        **versions,
        autoform_source="https://example.test/owner/autoform.git",
        autoform_ref="1" * 40,
    )

    assert result.workflows_pinned


def test_unsafe_local_templates_use_the_project_error_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "escape").symlink_to(tmp_path / "missing")
    monkeypatch.setattr(create_module, "_TEMPLATES", templates)

    _refused(tmp_path / "Project", "project-create-validation-failed")


def test_incomplete_local_templates_are_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(create_module._TEMPLATES, templates)
    (templates / "theme/main.html").unlink()
    monkeypatch.setattr(create_module, "_TEMPLATES", templates)

    _refused(tmp_path / "Project", "project-create-validation-failed")


@pytest.mark.parametrize("relative", ["lake-manifest.json", "lake-manifest.json/note.md"], ids=["file", "directory"])
def test_an_unlisted_pair_never_publishes_a_template_manifest(
    relative: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(create_module._TEMPLATES, templates)
    (templates / relative).parent.mkdir(exist_ok=True)
    (templates / relative).write_text('{"version": "1.1.0", "packages": []}\n', encoding="utf-8")
    target = tmp_path / "Project"
    monkeypatch.setattr(create_module, "_TEMPLATES", templates)

    error = _fails(target, "project-create-validation-failed", release_id=None, lean_toolchain="v4.30.0")
    assert error.message == (
        create_module._STAGED_MESSAGE + " An .autoform-new-* stage may remain; inspect it before removal."
    )
    assert not target.exists()
    stages = list(tmp_path.glob(".autoform-new-*"))
    assert len(stages) == 1
    assert (stages[0] / relative).is_file()
    assert stat.S_IMODE(stages[0].stat().st_mode) == 0o700


@pytest.mark.parametrize("relative", _CORE_FILES)
def test_a_template_cannot_replace_a_core_project_file(
    relative: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(create_module._TEMPLATES, templates)
    (templates / relative).parent.mkdir(exist_ok=True)
    (templates / relative).write_text("template\n", encoding="utf-8")
    monkeypatch.setattr(create_module, "_TEMPLATES", templates)

    error = _refused(tmp_path / "Project", "project-create-validation-failed")

    assert error.message == create_module._CONTRACTS_MESSAGE


def test_group_writable_installed_templates_publish_canonical_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A checkout or install under umask 002 leaves templates 0o664 and 0o775.
    templates = tmp_path / "templates"
    shutil.copytree(create_module._TEMPLATES, templates)
    for template in templates.rglob("*"):
        if template.is_file():
            template.chmod(0o775 if template.stat().st_mode & 0o100 else 0o664)
    target = tmp_path / "Project"
    monkeypatch.setattr(create_module, "_TEMPLATES", templates)

    result = create_project(
        target, package="Project", release_id=_RELEASE, autoform_source=_SOURCE, autoform_ref="a" * 40
    )

    modes = {relative: stat.S_IMODE((target / relative).stat().st_mode) for relative in result.written}
    assert modes.pop(".github/autoform_audit.py") == 0o755
    assert set(modes.values()) == {0o644}


def test_missing_release_manifest_is_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = load_release_catalog().recommended
    descriptor = create_module._load_creation_release_descriptor(release)
    monkeypatch.setattr(
        create_module,
        "_load_creation_release_descriptor",
        lambda _release: replace(descriptor, manifest_resource="missing-release-manifest.json"),
    )

    _refused(tmp_path / "Project", "project-create-validation-failed")


def test_creates_complete_supported_project(tmp_path: Path) -> None:
    target = tmp_path / "FiniteFlat"
    result = create_project(target, package="FiniteFlat", release_id=_RELEASE)

    assert result.package == "FiniteFlat"
    assert result.release == _RELEASE
    assert result.target == "FiniteFlat"
    assert result.as_dict()["schema"] == "autoform-project-creation/v1"
    assert (target / ".gitignore").read_text(encoding="utf-8").splitlines() == [
        ".lake/",
        "site/",
        "site-src/",
        "*.log",
        ".claude/worktrees/",
    ]
    assert (target / "lean-toolchain").read_text(encoding="utf-8") == ("leanprover/lean4:v4.32.2\n")
    assert (target / "lakefile.toml").read_text(encoding="utf-8") == (
        'name = "FiniteFlat"\n'
        'version = "0.1.0"\n'
        'defaultTargets = ["FiniteFlat"]\n\n'
        "[[require]]\n"
        'name = "mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4"\n'
        'rev = "v4.32.2"\n\n'
        "[[lean_lib]]\n"
        'name = "FiniteFlat"\n'
        'srcDir = "src"\n'
    )
    manifest = json.loads((target / "lake-manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "1.2.0"
    assert manifest["name"] == "FiniteFlat"
    assert {entry["name"]: entry["rev"] for entry in manifest["packages"]} == {
        "Cli": "88679d088c9720c27ebdf2ba4dafe17341747f94",
        "LeanSearchClient": "c5d5b8fe6e5158def25cd28eb94e4141ad97c843",
        "Qq": "38d591e778f100aec9762bb582f9c7f55f50e9dc",
        "aesop": "a7dbf0c63b694e47f425f3dcddbc0e178bb432d3",
        "batteries": "023ce7d62a0531e22a5331e20b587817a80d49ff",
        "importGraph": "7e9612bf0b9ee66db3cb5b9988a35afc706f5a12",
        "mathlib": "905b95818eb32af7874a58b427f50c1711a5e96c",
        "plausible": "e12c1910fe855cbfc38803cd4e55543906d5fa62",
        "proofwidgets": "6e311e2a844da9b2cc3971187df2fe0066947b93",
    }
    direct = [entry for entry in manifest["packages"] if not entry["inherited"]]
    assert len(direct) == 1
    assert direct[0]["name"] == "mathlib"
    assert direct[0]["inputRev"] == "v4.32.2"
    assert (target / "src/FiniteFlat.lean").read_text(encoding="utf-8") == (
        "import Mathlib\n\n"
        "namespace FiniteFlat\n\n"
        "/-- Marker declaration for the initial project build. -/\n"
        "def autoformProjectInitialized : Bool := true\n\n"
        "end FiniteFlat\n"
    )
    inspection = inspect_project(target)
    assert inspection.ok
    assert inspection.compatibility.status == "supported"
    assert inspection.compatibility.release == _RELEASE
    assert inspection.mathlib is not None
    assert inspection.mathlib.rev == "905b95818eb32af7874a58b427f50c1711a5e96c"
    assert set(load_graph(target / "blueprint").nodes) == {"roadmap"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not list(tmp_path.glob(".autoform-new-*"))


@pytest.mark.parametrize(
    "package",
    [
        "",
        "A" * 256,
        "finiteFlat",
        "Finite_Flat",
        "Finite.Flat",
        "../FiniteFlat",
        "Finite Flat",
        'Finite"Flat',
        "Aesop",
        "Archive",
        "Batteries",
        "Cache",
        "Cli",
        "Counterexamples",
        "ImportGraph",
        "Lean",
        "LeanSearchClient",
        "Lake",
        "Plausible",
        "ProofWidgets",
        "Qq",
        "Std",
        "Type",
        "Sort",
        "Prop",
        "Init",
        "Mathlib",
        "MathlibTest",
        "MATHLIB",
        "MathLib",
        "LEAN",
        "STD",
        123,
    ],
)
def test_rejects_invalid_package_before_writing(tmp_path: Path, package: object) -> None:
    _refused(tmp_path / "project", "project-name-invalid", package=package)


def test_every_release_has_creation_contracts() -> None:
    public_catalog = load_release_catalog()
    public_json = public_catalog.to_json()
    assert "production_module_roots" not in public_json
    assert "project_manifest" not in public_json
    for release in public_catalog.releases:
        descriptor = create_module._load_creation_release_descriptor(release)
        bundle = create_module._load_release_bundle(release)
        assert descriptor.manifest_resource.endswith(".json")
        assert bundle.module_roots == {
            "Aesop",
            "Archive",
            "Batteries",
            "Cache",
            "Cli",
            "Counterexamples",
            "ImportGraph",
            "Init",
            "Lake",
            "Lean",
            "LeanSearchClient",
            "Mathlib",
            "MathlibTest",
            "Plausible",
            "ProofWidgets",
            "Qq",
            "Std",
        }


@pytest.mark.parametrize("resource", ["manifest\ue000.json", "manifest\U0001f600.json"])
def test_release_metadata_names_its_manifest_below_the_surrogate_range(
    monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    release = load_release_catalog().recommended
    name = f"creation-release-{release.id}.json"
    payload = json.loads(create_module.files("autoform_cli.project").joinpath(name).read_bytes())
    payload["lake_manifest"] = resource

    class Resources:
        def joinpath(self, _name: str) -> Resources:
            return self

        def read_bytes(self) -> bytes:
            return json.dumps(payload).encode()

    monkeypatch.setattr(create_module, "files", lambda _package: Resources())

    with pytest.raises(ProjectCreateError) as raised:
        create_module._load_creation_release_descriptor(release)

    assert raised.value.code == "project-create-validation-failed"


@pytest.mark.parametrize("missing", ["Aesop", "Archive", "Counterexamples"])
def test_release_metadata_must_cover_manifest_and_mathlib_production_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    release = load_release_catalog().recommended
    descriptor = create_module._load_creation_release_descriptor(release)
    changed = replace(descriptor, module_roots=tuple(root for root in descriptor.module_roots if root != missing))
    monkeypatch.setattr(create_module, "_load_creation_release_descriptor", lambda _release: changed)

    _refused(tmp_path / "Project", "project-create-validation-failed")


@pytest.mark.parametrize("corruption", ["top-level-key", "revision", "credentials", "traversal", "inherited-type"])
def test_release_bundle_rejects_non_generated_manifest_shapes(corruption: str) -> None:
    release = load_release_catalog().recommended
    descriptor = create_module._load_creation_release_descriptor(release)
    bundle = create_module._load_release_bundle(release)
    payload = json.loads(bundle.manifest_bytes)
    if corruption == "top-level-key":
        payload["unexpected"] = True
    elif corruption == "revision":
        payload["packages"][1]["rev"] = "main"
    elif corruption == "credentials":
        payload["packages"][1]["url"] = "https://user:secret@example.test/repo"
    elif corruption == "traversal":
        payload["packages"][1]["manifestFile"] = "../lake-manifest.json"
    else:
        payload["packages"][1]["inherited"] = 1

    with pytest.raises(ProjectCreateError) as raised:
        create_module._parse_release_bundle(json.dumps(payload).encode(), release, descriptor.module_roots)

    assert raised.value.code == "project-create-validation-failed"


def test_package_name_reserves_the_longest_lake_artifact_filename(tmp_path: Path) -> None:
    name_limit = os.pathconf(tmp_path, "PC_NAME_MAX")
    suffix_bytes = len(create_module._LONGEST_LAKE_ARTIFACT_SUFFIX.encode("ascii"))
    if name_limit <= suffix_bytes:
        pytest.skip("filesystem name limit is too small for a Lean package")
    boundary = "A" * (name_limit - suffix_bytes)

    result = create_project(tmp_path / "Accepted", package=boundary, release_id=_RELEASE)

    assert result.package == boundary
    _refused(tmp_path / "Rejected", "project-name-invalid", package=f"{boundary}A")


def test_open_parent_descriptor_rechecks_the_generated_module_filename_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name_limit = os.pathconf(tmp_path, "PC_NAME_MAX")
    suffix_bytes = len(create_module._LONGEST_LAKE_ARTIFACT_SUFFIX.encode("ascii"))
    package = "A" * (name_limit - suffix_bytes + 1)
    monkeypatch.setattr(create_module, "_validate_package", lambda _package, _parent: package)

    _refused(tmp_path / "project", "project-name-invalid", package=package)


def test_rejects_unknown_release_before_writing(tmp_path: Path) -> None:
    _refused(tmp_path / "project", "project-release-unknown", release_id="unknown")


def test_omitted_release_uses_the_recommended_release(tmp_path: Path) -> None:
    recommended = load_release_catalog().recommended

    result = create_project(tmp_path / "Default", package="Project", release_id=None)
    create_project(tmp_path / "Explicit", package="Project", release_id=recommended.id)

    assert result.release == recommended.id
    assert result.lean_toolchain == recommended.lean_toolchain
    assert result.mathlib_rev == recommended.mathlib_rev
    assert result.warnings == ()
    for relative in _CORE_FILES:
        assert (tmp_path / "Default" / relative).read_bytes() == (tmp_path / "Explicit" / relative).read_bytes()


def test_unlisted_pair_is_written_without_a_lock(tmp_path: Path) -> None:
    target = tmp_path / "Project"

    result = create_project(target, package="Project", release_id=None, lean_toolchain="v4.30.0")

    assert result.release is None
    assert result.lean_toolchain == "leanprover/lean4:v4.30.0"
    assert result.mathlib_rev == "v4.30.0"
    assert [code for code, _message in result.warnings] == ["project-release-unlisted"]
    assert "by its tag or full commit" in result.warnings[0][1]
    assert (target / "lean-toolchain").read_text(encoding="utf-8") == "leanprover/lean4:v4.30.0\n"
    assert (target / "lakefile.toml").read_text(encoding="utf-8") == _UNLISTED_LAKEFILE
    assert not (target / "lake-manifest.json").exists()
    assert "lake-manifest.json" not in result.written
    inspection = inspect_project(target)
    assert inspection.ok
    assert inspection.compatibility.status == "indeterminate"
    assert {diagnostic.code for diagnostic in inspection.diagnostics} == {
        "missing-lake-manifest",
        "release-indeterminate",
    }


@pytest.mark.parametrize(
    ("toolchain", "revision"),
    [
        ("v4.30.0", "master"),
        ("v4.30.0", "bump/v4.30.0"),
        ("v4.30.0", "0123456789abcdef0123456789abcdef01234567"),
        ("v4.30.0", "a" * 255),
        ("v4.30.0", "Feature/fix_x"),
        ("v4.32.0-rc1", "v4.32.0-rc1-patch1"),
    ],
)
def test_unlisted_pair_threads_the_mathlib_revision(tmp_path: Path, toolchain: str, revision: str) -> None:
    target = tmp_path / "Project"

    result = create_project(target, package="Project", release_id=None, lean_toolchain=toolchain, mathlib_rev=revision)

    assert result.release is None
    assert result.mathlib_rev == revision
    with (target / "lakefile.toml").open("rb") as lakefile:
        assert tomllib.load(lakefile)["require"] == [
            {"name": "mathlib", "git": "https://github.com/leanprover-community/mathlib4", "rev": revision}
        ]


@pytest.mark.parametrize(
    ("toolchain", "revision"),
    [
        ("v4.32.2", None),
        ("leanprover/lean4:v4.32.2", "v4.32.2"),
        ("v4.32.2", "905b95818eb32af7874a58b427f50c1711a5e96c"),
        ("v4.32.2", "905B95818EB32AF7874A58B427F50C1711A5E96C"),
    ],
)
def test_catalog_pair_given_as_versions_uses_the_bundled_lock(
    tmp_path: Path, toolchain: str, revision: str | None
) -> None:
    result = create_project(
        tmp_path / "Versions", package="Project", release_id=None, lean_toolchain=toolchain, mathlib_rev=revision
    )
    create_project(tmp_path / "Release", package="Project", release_id=_RELEASE)

    assert result.release == _RELEASE
    assert result.warnings == ()
    for relative in _CORE_FILES:
        assert (tmp_path / "Versions" / relative).read_bytes() == (tmp_path / "Release" / relative).read_bytes()


@pytest.mark.parametrize(
    "versions",
    [
        {"release_id": _RELEASE, "lean_toolchain": "v4.30.0"},
        {"release_id": _RELEASE, "mathlib_rev": "v4.30.0"},
        {"release_id": None, "mathlib_rev": "v4.30.0"},
    ],
)
def test_rejects_conflicting_or_incomplete_version_options_before_writing(
    tmp_path: Path, versions: dict[str, str | None]
) -> None:
    _refused(tmp_path / "Project", "project-version-invalid", **versions)


@pytest.mark.parametrize(
    "toolchain",
    [
        "",
        "4.30.0",
        "v4",
        "v4.30",
        "v04.30.0",
        "v4.030.0",
        "v4.30.00",
        "V4.30.0",
        "v4.30.0\n",
        "v4.30.0 ",
        " v4.30.0",
        "v4.30.0-rc",
        "v4.30.0-rc0",
        "v4.30.0-rc01",
        "v4.30.0-patch1",
        "v4.30.0.1",
        "v\N{ARABIC-INDIC DIGIT FOUR}.30.0",
        "nightly-2025-01-01",
        "leanprover/lean4:nightly-2025-01-01",
        "leanprover/lean4-nightly:nightly-2025-01-01",
        "leanprover/lean4:stable",
        "lean4:v4.30.0",
        "leanprover/lean4:4.30.0",
        7,
        b"v4.30.0",
    ],
)
def test_rejects_invalid_lean_toolchains_before_writing(tmp_path: Path, toolchain: object) -> None:
    _refused(tmp_path / "Project", "project-version-invalid", release_id=None, lean_toolchain=toolchain)


@pytest.mark.parametrize(
    "revision",
    [
        "",
        "-x",
        "--upload-pack=x",
        "a b",
        "a\tb",
        'a"b',
        "a\\b",
        "a\nb",
        "a:b",
        "a^",
        "a~1",
        "a@{1}",
        "@",
        "a..b",
        "a/.b",
        "a/b.lock",
        "a//b",
        "a/",
        "a.",
        ".a",
        "/a",
        "_a",
        "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
        "a" * 256,
        7,
    ],
)
def test_rejects_invalid_mathlib_revisions_before_writing(tmp_path: Path, revision: object) -> None:
    _refused(
        tmp_path / "Project", "project-version-invalid", release_id=None, lean_toolchain="v4.30.0", mathlib_rev=revision
    )


@pytest.mark.parametrize(
    ("toolchain", "codes"),
    [
        ("v4.26.0", ["project-lean-below-minimum", "project-release-unlisted"]),
        ("v4.9.1", ["project-lean-below-minimum", "project-release-unlisted"]),
        ("v4.27.0-rc1", ["project-release-unlisted"]),
        ("v4.27.0", ["project-release-unlisted"]),
        ("v5.0.0", ["project-release-unlisted"]),
    ],
)
def test_warns_below_the_lean_floor(tmp_path: Path, toolchain: str, codes: list[str]) -> None:
    result = create_project(tmp_path / "Project", package="Project", release_id=None, lean_toolchain=toolchain)

    assert [code for code, _message in result.warnings] == codes


@pytest.mark.parametrize("package", ["Docs", "DOCS", "Wanted", "LongestPole", "MATHLIB"])
@pytest.mark.parametrize("toolchain", [None, "v4.30.0"])
def test_reserved_mathlib_roots_are_refused(tmp_path: Path, package: str, toolchain: str | None) -> None:
    _refused(tmp_path / "Project", "project-name-invalid", package=package, release_id=None, lean_toolchain=toolchain)


def test_catalog_releases_meet_the_lean_floor() -> None:
    for release in load_release_catalog().releases:
        version = create_module._resolve_version(None, release.lean_toolchain, None)
        assert version.release == release
        assert create_module._version_warnings(version) == ()


def test_long_valid_target_name_does_not_expand_the_stage_name(tmp_path: Path) -> None:
    name_limit = os.pathconf(tmp_path, "PC_NAME_MAX")
    if name_limit < 64:
        pytest.skip("filesystem name limit is too small for this boundary test")
    target = tmp_path / ("p" * name_limit)

    result = create_project(target, package="Project", release_id=_RELEASE)

    assert result.target == target.name
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_embedded_nul_target_uses_the_stable_error_contract(tmp_path: Path, capsys) -> None:
    target = os.fspath(tmp_path / "bad\0name")

    _fails(target, "project-target-invalid")

    assert main(["project", "new", target, "--package", "Project", "--release", _RELEASE, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "project-target-invalid"


@pytest.mark.parametrize("target", ["", ".", "..", "/"])
def test_target_must_name_a_directory(target: str) -> None:
    _fails(target, "project-target-invalid")


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "broken-symlink"])
def test_never_overwrites_existing_target(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "project"
    if kind == "file":
        target.write_bytes(b"authored\n")
    elif kind == "directory":
        target.mkdir()
        (target / "authored").write_bytes(b"authored\n")
    else:
        real = tmp_path / "real"
        if kind == "symlink":
            real.mkdir()
        target.symlink_to(real, target_is_directory=True)

    def snapshot() -> list[tuple[str, bytes]]:
        return sorted(
            (path.relative_to(tmp_path).as_posix(), path.read_bytes())
            for path in tmp_path.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    before = snapshot()
    _refused(target, "project-target-exists")
    assert snapshot() == before


def test_macos_tmp_alias_is_rejected_but_private_tmp_is_supported() -> None:
    if not Path("/tmp").is_symlink():
        pytest.skip("platform has no /tmp alias")
    canonical_root = Path("/private/tmp")
    assert Path("/tmp").resolve() == canonical_root
    name = f"autoform-new-test-{os.getpid()}"
    parent = canonical_root / name
    parent.mkdir(mode=0o700)
    parent.chmod(0o755)
    try:
        _refused(Path("/tmp") / name / "Project", "project-path-is-symlink")

        create_project(parent / "Project", package="Project", release_id=_RELEASE)
        assert inspect_project(parent / "Project").ok
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_rejects_nonsticky_shared_parent(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    _refused(parent / "Project", "project-parent-unsafe")


def test_rechecks_parent_mode_on_the_open_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    target = parent / "Project"
    monkeypatch.setattr(create_module, "_validate_target", lambda _target: target)

    assert "chmod g-w,o-w" in _refused(target, "project-parent-unsafe").message


def test_sticky_shared_parent_requires_a_trusted_descriptor_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(create_module.os, "geteuid", lambda: 1000)
    mode = stat.S_IFDIR | stat.S_ISVTX | 0o777

    assert not create_module._unsafe_parent_metadata(mode, 0)
    assert not create_module._unsafe_parent_metadata(mode, 1000)
    assert create_module._unsafe_parent_metadata(mode, 2000)


def test_fresh_parent_binding_compares_owner_as_well_as_device_and_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = create_module._open_parent(tmp_path)
    expected = create_module._descriptor_identity(descriptor)
    monkeypatch.setattr(
        create_module, "_descriptor_identity", lambda _descriptor: (expected[0], expected[1], expected[2] + 1)
    )
    try:
        with pytest.raises(ProjectCreateError) as raised:
            create_module._reopen_bound_parent(tmp_path, expected)
    finally:
        os.close(descriptor)

    assert raised.value.code == "project-parent-changed"


def test_missing_directory_capability_uses_stable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    monkeypatch.setattr(
        create_module.os, "supports_dir_fd", create_module.os.supports_dir_fd - {create_module.os.mkdir}
    )

    _refused(target, "project-create-safety-unavailable")


def test_injected_build_failure_preserves_the_empty_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(create_module, "_materialize_project", fail)
    error = _fails(target, "project-create-failed")
    assert ".autoform-new-* stage may remain" in error.message
    assert not target.exists()
    stages = list(tmp_path.glob(".autoform-new-*"))
    assert len(stages) == 1
    assert not list(stages[0].iterdir())


def test_invalid_planned_roadmap_is_never_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    original = create_module._build_project_plan

    def corrupt(*args, **kwargs):
        return tuple(
            type(item)(item.relative, b"No H1 title.\n", item.mode)
            if item.relative == "blueprint/roadmap/README.md"
            else item
            for item in original(*args, **kwargs)
        )

    monkeypatch.setattr(create_module, "_build_project_plan", corrupt)

    _refused(target, "project-create-validation-failed")


@pytest.mark.parametrize("corruption", ["relative", "mode"])
def test_plan_requires_safe_paths_and_file_modes_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    original = create_module._build_project_plan

    def corrupt(*args, **kwargs):
        first, *rest = original(*args, **kwargs)
        changed = {
            "relative": type(first)(Path(first.relative), first.content, first.mode),
            "mode": type(first)(first.relative, first.content, 0o666),
        }[corruption]
        return (changed, *rest)

    monkeypatch.setattr(create_module, "_build_project_plan", corrupt)

    _refused(tmp_path / "Project", "project-create-validation-failed")


def test_close_failure_after_publish_reports_that_the_target_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original_rename = create_module._rename_noreplace
    original_reopen = create_module._reopen_bound_parent
    original_close = create_module.os.close
    published = False
    cleanup_ready = False
    reopen_calls = 0
    failed = False

    def publish(*args):
        nonlocal published
        original_rename(*args)
        published = True

    def reopen(*args):
        nonlocal cleanup_ready, reopen_calls
        descriptor = original_reopen(*args)
        reopen_calls += 1
        if reopen_calls == 2:
            cleanup_ready = True
        return descriptor

    def close_after_publish(descriptor):
        nonlocal failed
        original_close(descriptor)
        if published and cleanup_ready and not failed:
            failed = True
            raise OSError("injected close failure")

    monkeypatch.setattr(create_module, "_rename_noreplace", publish)
    monkeypatch.setattr(create_module, "_reopen_bound_parent", reopen)
    monkeypatch.setattr(create_module.os, "close", close_after_publish)

    error = _fails(target, "project-create-commit-uncertain")
    assert "target names the published project" in error.message
    assert "parent directory was synced" in error.message
    assert "final descriptor cleanup failed" in error.message
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_fsync_failure_after_publish_reports_that_the_target_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original_rename = create_module._rename_noreplace
    original_fsync = create_module.os.fsync
    published = False
    failed = False

    def publish(*args):
        nonlocal published
        original_rename(*args)
        published = True

    def fail_after_publish(descriptor):
        nonlocal failed
        if published and not failed:
            failed = True
            raise OSError("injected fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(create_module, "_rename_noreplace", publish)
    monkeypatch.setattr(create_module.os, "fsync", fail_after_publish)

    error = _fails(target, "project-create-commit-uncertain")
    assert "target names the published project" in error.message
    assert "parent-directory sync was not confirmed" in error.message
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_interrupt_after_publish_reports_that_the_target_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original_rename = create_module._rename_noreplace
    original_fsync = create_module.os.fsync
    published = False

    def publish(*args):
        nonlocal published
        original_rename(*args)
        published = True

    def interrupt_after_publish(descriptor):
        if published:
            raise KeyboardInterrupt
        original_fsync(descriptor)

    monkeypatch.setattr(create_module, "_rename_noreplace", publish)
    monkeypatch.setattr(create_module.os, "fsync", interrupt_after_publish)

    _fails(target, "project-create-commit-uncertain")
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_rename_exception_after_commit_reports_that_the_target_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original = create_module._rename_noreplace

    def commit_then_fail(*args):
        original(*args)
        raise OSError("injected post-rename failure")

    monkeypatch.setattr(create_module, "_rename_noreplace", commit_then_fail)

    _fails(target, "project-create-commit-uncertain")
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".autoform-new-*"))


def test_detached_stage_after_publication_attempt_reports_uncertain_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    moved = tmp_path / "moved"
    original = create_module._rename_noreplace

    def commit_move_then_fail(*args):
        original(*args)
        target.rename(moved)
        raise OSError("injected post-rename failure")

    monkeypatch.setattr(create_module, "_rename_noreplace", commit_move_then_fail)

    error = _fails(target, "project-create-commit-uncertain")
    assert "neither the target nor the preserved stage names the project" in error.message
    assert not target.exists()
    assert inspect_project(moved).ok


def test_publication_capability_error_remains_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"

    def unavailable(*args):
        raise ProjectCreateError("project-create-safety-unavailable", "Atomic no-replace rename is unavailable.")

    monkeypatch.setattr(create_module, "_rename_noreplace", unavailable)

    error = _fails(target, "project-create-safety-unavailable")
    assert ".autoform-new-* stage may remain" in error.message
    assert not target.exists()
    assert len(list(tmp_path.glob(".autoform-new-*"))) == 1


def test_unsupported_rename_flag_uses_capability_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingRename:
        argtypes = None
        restype = None

        def __call__(self, *args):
            return -1

    class Libc:
        renameat2 = FailingRename()

    monkeypatch.setattr(create_module.ctypes, "CDLL", lambda *args, **kwargs: Libc())
    monkeypatch.setattr(create_module.ctypes, "get_errno", lambda: errno.EINVAL)

    with pytest.raises(ProjectCreateError) as raised:
        create_module._rename_noreplace(3, "stage", 3, "target")

    assert raised.value.code == "project-create-safety-unavailable"


def test_late_target_race_remains_distinguishable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"

    def lose_race(*args):
        target.mkdir()
        (target / "KEEP").write_text("keep\n", encoding="utf-8")
        raise FileExistsError

    monkeypatch.setattr(create_module, "_rename_noreplace", lose_race)

    error = _fails(target, "project-target-exists")
    assert ".autoform-new-* stage may remain" in error.message
    assert (target / "KEEP").read_text(encoding="utf-8") == "keep\n"
    assert len(list(tmp_path.glob(".autoform-new-*"))) == 1


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_DIRECTORY"), reason="atomic no-replace publication is POSIX-only"
)
def test_real_noreplace_syscall_preserves_a_late_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "stage"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "FROM_STAGE").write_text("stage\n", encoding="utf-8")
    (target / "KEEP").write_text("target\n", encoding="utf-8")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            create_module._rename_noreplace(descriptor, source.name, descriptor, target.name)
    finally:
        os.close(descriptor)

    assert (source / "FROM_STAGE").read_text(encoding="utf-8") == "stage\n"
    assert (target / "KEEP").read_text(encoding="utf-8") == "target\n"


def test_requested_parent_rebind_before_publish_preserves_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    moved = tmp_path / "moved-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o755)
    target = parent / "Project"
    original = create_module._materialize_project

    def rebind(*args, **kwargs) -> None:
        original(*args, **kwargs)
        parent.rename(moved)
        parent.mkdir(mode=0o700)
        parent.chmod(0o755)

    monkeypatch.setattr(create_module, "_materialize_project", rebind)

    error = _fails(target, "project-parent-changed")
    assert ".autoform-new-* stage may remain" in error.message
    assert not (parent / "Project").exists()
    stages = list(moved.glob(".autoform-new-*"))
    assert len(stages) == 1
    assert (stages[0] / "lean-toolchain").is_file()


def test_requested_parent_rebind_after_parent_sync_reports_exact_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    moved = tmp_path / "moved-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o755)
    target = parent / "Project"
    original = create_module._reopen_bound_parent
    calls = 0

    def rebind(path: Path, expected_identity: tuple[int, int, int]) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            parent.rename(moved)
            parent.mkdir(mode=0o700)
            parent.chmod(0o755)
        return original(path, expected_identity)

    monkeypatch.setattr(create_module, "_reopen_bound_parent", rebind)

    error = _fails(target, "project-create-commit-uncertain")

    assert calls == 2
    assert "was published and its original parent directory was synced" in error.message
    assert "requested parent path no longer names that directory" in error.message
    assert not (parent / "Project").exists()
    assert inspect_project(moved / "Project").ok


def test_postpublish_parent_recheck_failure_does_not_claim_a_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Project"
    original = create_module._reopen_bound_parent
    calls = 0

    def fail_second(path: Path, expected_identity: tuple[int, int, int]) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ProjectCreateError(
                "project-parent-unverifiable", "The requested parent path could not be reverified safely."
            )
        return original(path, expected_identity)

    monkeypatch.setattr(create_module, "_reopen_bound_parent", fail_second)

    error = _fails(target, "project-create-commit-uncertain")
    assert "could not reopen the requested parent path" in error.message
    assert "no longer names" not in error.message
    assert inspect_project(target).ok


@pytest.mark.parametrize("template_manifest", [False, True], ids=["catalog-release", "unlisted-template-manifest"])
def test_workspace_substitution_fails_before_publication(
    template_manifest: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    versions: dict[str, str | None] = {"release_id": _RELEASE}
    if template_manifest:
        # The identity check runs before the manifest refusal, so the code stays main's.
        templates = tmp_path / "templates"
        shutil.copytree(create_module._TEMPLATES, templates)
        (templates / "lake-manifest.json").write_text('{"version": "1.1.0", "packages": []}\n', encoding="utf-8")
        monkeypatch.setattr(create_module, "_TEMPLATES", templates)
        versions = {"release_id": None, "lean_toolchain": "v4.30.0"}
    original = create_module._materialize_project

    def substitute(*args, **kwargs) -> None:
        original(*args, **kwargs)
        stage = next(tmp_path.glob(".autoform-new-*"))
        moved = stage.with_name(f"{stage.name}-owned")
        stage.rename(moved)
        stage.mkdir(mode=0o700)
        (stage / "FOREIGN").write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(create_module, "_materialize_project", substitute)
    _fails(target, "project-create-failed", **versions)
    assert not target.exists()
    assert any(path.name == "FOREIGN" for path in tmp_path.rglob("FOREIGN"))
    # The renamed stage is refused before its chmod, so the written tree stays private.
    assert stat.S_IMODE(next(tmp_path.glob(".autoform-new-*-owned")).stat().st_mode) == 0o700


def test_stage_path_substitution_never_writes_to_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "KEEP").write_text("keep\n", encoding="utf-8")
    original = create_module._materialize_project

    def substitute(stage_descriptor, plan) -> None:
        stage = next(tmp_path.glob(".autoform-new-*"))
        stage.rename(stage.with_name(f"{stage.name}-owned"))
        stage.symlink_to(victim, target_is_directory=True)
        original(stage_descriptor, plan)

    monkeypatch.setattr(create_module, "_materialize_project", substitute)

    _fails(target, "project-create-failed")
    assert not target.exists()
    assert (victim / "KEEP").read_text(encoding="utf-8") == "keep\n"
    assert sorted(path.name for path in victim.iterdir()) == ["KEEP"]


def test_stage_open_failure_preserves_the_owned_empty_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    original = create_module._open_directory

    def fail_first_open(parent_descriptor: int, stage_name: str) -> int:
        if stage_name.startswith(".autoform-new-"):
            raise OSError("injected stage open failure")
        return original(parent_descriptor, stage_name)

    monkeypatch.setattr(create_module, "_open_directory", fail_first_open)

    _fails(target, "project-create-failed")
    assert not target.exists()
    stages = list(tmp_path.glob(".autoform-new-*"))
    assert len(stages) == 1
    assert not list(stages[0].iterdir())


def test_stage_substitution_is_refused_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    original = create_module._open_directory

    def substitute_after_open(parent_descriptor: int, name: str) -> int:
        descriptor = original(parent_descriptor, name)
        if name.startswith(".autoform-new-"):
            (tmp_path / name).rename(tmp_path / f"{name}-owned")
            (tmp_path / name).mkdir(mode=0o700)
        return descriptor

    monkeypatch.setattr(create_module, "_open_directory", substitute_after_open)

    error = _fails(target, "project-create-failed")
    assert ".autoform-new-* stage may remain" in error.message
    assert not target.exists()
    # Neither the replacement nor the opened stage, now under its -owned name, received a file.
    stages = list(tmp_path.glob(".autoform-new-*"))
    assert len(stages) == 2
    assert not any(list(stage.iterdir()) for stage in stages)


@pytest.mark.parametrize(
    ("mode", "entries"), [(0o700, {"private.txt"}), (0o755, set())], ids=["nonempty-0700", "empty-0755"]
)
def test_swapped_in_stage_directory_is_refused_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int, entries: set[str]
) -> None:
    target = tmp_path / "project"
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    for name in entries:
        (decoy / name).write_text("private\n", encoding="utf-8")
    decoy.chmod(mode)
    original = create_module._open_directory

    def swap_before_open(parent_descriptor: int, name: str) -> int:
        if name.startswith(".autoform-new-") and decoy.exists():
            (tmp_path / name).rename(tmp_path / "original-stage")
            decoy.rename(tmp_path / name)
        return original(parent_descriptor, name)

    monkeypatch.setattr(create_module, "_open_directory", swap_before_open)

    error = _fails(target, "project-create-failed")
    assert ".autoform-new-* stage may remain" in error.message
    assert not target.exists()
    (swapped,) = tmp_path.glob(".autoform-new-*")
    assert stat.S_IMODE(swapped.stat().st_mode) == mode
    assert {path.name for path in swapped.iterdir()} == entries
    assert not list((tmp_path / "original-stage").iterdir())


def test_foreign_owned_stage_is_refused_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    original = create_module._create_stage
    euid = os.geteuid()

    def foreign_stage(parent_descriptor: int) -> str:
        name = original(parent_descriptor)
        # From here on the stage looks like a directory another uid put in its place.
        monkeypatch.setattr(create_module.os, "geteuid", lambda: euid + 1)
        return name

    monkeypatch.setattr(create_module, "_create_stage", foreign_stage)

    error = _fails(target, "project-create-failed")
    assert ".autoform-new-* stage may remain" in error.message
    assert not target.exists()
    stages = list(tmp_path.glob(".autoform-new-*"))
    assert len(stages) == 1
    assert not list(stages[0].iterdir())


def test_failure_path_never_attempts_recursive_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    original = create_module._materialize_project

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("injected")

    def forbidden(*args, **kwargs):
        raise AssertionError("project creation attempted destructive cleanup")

    monkeypatch.setattr(create_module, "_materialize_project", fail)
    monkeypatch.setattr(create_module.os, "unlink", forbidden)
    monkeypatch.setattr(create_module.os, "rmdir", forbidden)

    _fails(target, "project-create-failed")
    assert not target.exists()


def test_failure_cleanup_never_recurses_into_a_foreign_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "KEEP").write_text("keep\n", encoding="utf-8")
    original = create_module._materialize_project

    def substitute(*args, **kwargs):
        original(*args, **kwargs)
        stage = next(tmp_path.glob(".autoform-new-*"))
        (stage / "blueprint").rename(stage / "owned-blueprint")
        victim.rename(stage / "blueprint")
        raise OSError("injected")

    monkeypatch.setattr(create_module, "_materialize_project", substitute)
    _fails(target, "project-create-failed")
    assert not target.exists()
    stages = list(tmp_path.glob(".autoform-new-*"))
    assert len(stages) == 1
    assert (stages[0] / "blueprint/KEEP").read_text(encoding="utf-8") == "keep\n"


def test_hard_linked_planned_file_is_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    alias = tmp_path / "alias"
    original = create_module._materialize_project

    def add_alias(*args, **kwargs):
        original(*args, **kwargs)
        stage = next(tmp_path.glob(".autoform-new-*"))
        os.link(stage / "lean-toolchain", alias)

    monkeypatch.setattr(create_module, "_materialize_project", add_alias)

    _fails(target, "project-create-failed")
    assert not target.exists()
    assert alias.stat().st_nlink == 2


def test_noncanonical_generated_directory_mode_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    original = create_module._materialize_project

    def make_world_writable(*args, **kwargs):
        original(*args, **kwargs)
        stage = next(tmp_path.glob(".autoform-new-*"))
        (stage / "blueprint").chmod(0o777)

    monkeypatch.setattr(create_module, "_materialize_project", make_world_writable)

    _fails(target, "project-create-failed")
    assert not target.exists()
    stage = next(tmp_path.glob(".autoform-new-*"))
    assert stat.S_IMODE((stage / "blueprint").stat().st_mode) == 0o777


def test_mutated_stage_is_not_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "project"
    original = create_module._materialize_project

    def mutate(*args, **kwargs):
        original(*args, **kwargs)
        stage = next(tmp_path.glob(".autoform-new-*"))
        (stage / "lean-toolchain").write_text("mutated\n", encoding="utf-8")

    monkeypatch.setattr(create_module, "_materialize_project", mutate)

    _fails(target, "project-create-failed")
    assert not target.exists()


def test_verifier_opens_regular_files_nonblocking(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def capture(name, flags, *, dir_fd):
        captured.update(name=name, flags=flags, dir_fd=dir_fd)
        return 17

    monkeypatch.setattr(create_module.os, "open", capture)

    assert create_module._open_planned_file(9, "lean-toolchain") == 17
    assert captured == {
        "name": "lean-toolchain",
        "flags": os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
        "dir_fd": 9,
    }


def test_concurrent_creation_has_exactly_one_winner(tmp_path: Path) -> None:
    target = tmp_path / "project"
    barrier = threading.Barrier(2)
    results: list[str] = []

    def run() -> None:
        barrier.wait(timeout=10)
        try:
            create_project(target, package="Project", release_id=_RELEASE)
            results.append("created")
        except ProjectCreateError as error:
            results.append(error.code)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["created", "project-target-exists"]
    assert inspect_project(target).ok
    assert not list(tmp_path.glob(".autoform-new-*"))


@pytest.mark.parametrize(
    "arguments, code",
    [
        (["project", "new", "--json"], "project-target-invalid"),
        (["project", "new", "project", "--release", _RELEASE, "--json"], "project-name-invalid"),
        (
            ["project", "new", "project", "--package", "Project", "--mathlib-rev", "v4.30.0", "--json"],
            "project-version-invalid",
        ),
        (
            [
                "project",
                "new",
                "project",
                "--package",
                "Project",
                "--release",
                _RELEASE,
                "--lean-toolchain",
                "v4.30.0",
                "--json",
            ],
            "project-version-invalid",
        ),
    ],
)
def test_cli_missing_creation_options_are_json(
    arguments: list[str], code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # The relative target's parent must pass the safety checks whatever the checkout's mode is.
    monkeypatch.chdir(tmp_path)
    assert main(arguments) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["code"] == code
    assert captured.err == ""


def test_cli_defaults_to_the_recommended_release(tmp_path: Path, capsys) -> None:
    recommended = load_release_catalog().recommended

    assert main(["project", "new", os.fspath(tmp_path / "Project"), "--package", "Project", "--json"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["release"] == recommended.id
    assert payload["lean_toolchain"] == recommended.lean_toolchain
    assert payload["mathlib_rev"] == recommended.mathlib_rev
    assert payload["warnings"] == []
    assert captured.err == ""


def test_cli_unlisted_json_reports_warnings(tmp_path: Path, capsys) -> None:
    arguments = ["project", "new", os.fspath(tmp_path / "Project"), "--package", "Project"]

    assert main([*arguments, "--lean-toolchain", "v4.30.0", "--json"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["release"] is None
    assert payload["lean_toolchain"] == "leanprover/lean4:v4.30.0"
    assert payload["mathlib_rev"] == "v4.30.0"
    assert [warning["code"] for warning in payload["warnings"]] == ["project-release-unlisted"]
    assert "lake update" in payload["warnings"][0]["message"]
    assert "lake-manifest.json" not in payload["written"]
    assert captured.err == ""


def test_cli_unlisted_human_output_warns_on_stderr(tmp_path: Path, capsys) -> None:
    target = tmp_path / "Project"
    arguments = ["project", "new", os.fspath(target), "--package", "Project"]

    assert main([*arguments, "--lean-toolchain", "v4.30.0"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "Created Project at Project (unlisted: leanprover/lean4:v4.30.0, Mathlib v4.30.0)\n"
    assert captured.err.startswith("warning[project-release-unlisted]: ")
    assert captured.err.endswith(
        "warning: workflows were omitted because no immutable Autoform pin was available; "
        'add them with: uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform init '
        f"{shlex.quote(os.fspath(target))} --autoform-ref <40-char-sha>\n"
    )


def test_cli_omitted_workflows_hint_keeps_an_explicit_source(tmp_path: Path, capsys) -> None:
    source = "https://example.com/~team/autoform-bot.git"
    target = tmp_path / "Project with ' shell syntax"
    arguments = ["project", "new", os.fspath(target), "--package", "Project"]

    assert main([*arguments, "--release", _RELEASE, "--autoform-source", source]) == 0

    captured = capsys.readouterr()
    assert captured.err.endswith(
        'add them with: uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform init '
        f"{shlex.quote(os.fspath(target))} --autoform-source "
        "'https://example.com/~team/autoform-bot.git' --autoform-ref <40-char-sha>\n"
    )


def test_cli_omitted_workflows_hint_cannot_become_a_multiline_diagnostic(tmp_path: Path, capsys) -> None:
    target = tmp_path / "Project\nforged-warning"

    assert main(["project", "new", os.fspath(target), "--package", "Project", "--release", _RELEASE]) == 0

    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1
    assert "\\x0a" in captured.err
    assert "\nforged-warning" not in captured.err


def test_cli_json_is_stable_and_path_free(tmp_path: Path, capsys) -> None:
    target = tmp_path / "project"
    assert main(["project", "new", str(target), "--package", "Project", "--release", _RELEASE, "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["target"] == "project"
    assert str(tmp_path) not in captured.out
    assert captured.err == ""

    duplicate = tmp_path / "project"
    assert main(["project", "new", str(duplicate), "--package", "Project", "--release", _RELEASE, "--json"]) == 1
    failed = capsys.readouterr()
    assert json.loads(failed.out)["error"]["code"] == "project-target-exists"
    assert failed.err == ""


@pytest.mark.skipif(os.name != "posix", reason="project creation is POSIX-only")
def test_cli_postcommit_output_is_ascii_and_backslash_safe(tmp_path: Path) -> None:
    target = tmp_path / "cr\N{LATIN SMALL LETTER E WITH ACUTE}ation\\line\nbreak"
    environment = {**os.environ, "PYTHONIOENCODING": "ascii:strict", "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoform_cli",
            "project",
            "new",
            os.fspath(target),
            "--package",
            "Project",
            "--release",
            _RELEASE,
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        encoding="ascii",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "\\xe9" in completed.stdout
    assert "\\\\line\\nbreak" in completed.stdout
    assert target.is_dir()

    json_target = tmp_path / "d\N{LATIN SMALL LETTER O WITH CIRCUMFLEX}nn\N{LATIN SMALL LETTER E WITH ACUTE}es"
    machine = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoform_cli",
            "project",
            "new",
            os.fspath(json_target),
            "--package",
            "Project",
            "--release",
            _RELEASE,
            "--json",
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        encoding="ascii",
        check=False,
    )
    assert machine.returncode == 0, machine.stderr
    assert all(ord(character) < 128 for character in machine.stdout)
    payload = json.loads(machine.stdout)
    assert payload["schema"] == "autoform-project-creation/v1"
    assert payload["target"] == json_target.name


@pytest.mark.parametrize("name", [os.fsdecode(b"project-\xff"), "project-\ud800"])
def test_surrogate_target_is_rejected_before_writing(tmp_path: Path, name: str) -> None:
    _refused(tmp_path / name, "project-target-invalid")


def test_cli_threads_the_explicit_workflow_pin(tmp_path: Path, capsys) -> None:
    target = tmp_path / "Pinned"
    source = "https://example.test/owner/autoform.git"
    revision = "5" * 40

    assert (
        main(
            [
                "project",
                "new",
                os.fspath(target),
                "--package",
                "Pinned",
                "--release",
                _RELEASE,
                "--autoform-source",
                source,
                "--autoform-ref",
                revision,
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["workflows_pinned"] is True
    workflow = (target / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert f'AUTOFORM_SOURCE: "{source}"' in workflow
    assert f'AUTOFORM_REF: "{revision}"' in workflow


@pytest.mark.parametrize(
    ("toolchain", "revision"),
    [
        ("v4.32.2", "master"),
        ("v4.32.2", "0123456789abcdef0123456789abcdef01234567"),
        ("v4.30.0", "v4.32.2"),
        ("v4.30.0", "905b95818eb32af7874a58b427f50c1711a5e96c"),
    ],
)
def test_pair_matching_the_catalog_in_one_component_stays_unlisted(
    tmp_path: Path, toolchain: str, revision: str
) -> None:
    target = tmp_path / "Project"

    result = create_project(target, package="Project", release_id=None, lean_toolchain=toolchain, mathlib_rev=revision)

    assert result.release is None
    assert [code for code, _message in result.warnings] == ["project-release-unlisted"]
    assert not (target / "lake-manifest.json").exists()
    assert (target / "lean-toolchain").read_text(encoding="utf-8") == f"leanprover/lean4:{toolchain}\n"
    with (target / "lakefile.toml").open("rb") as lakefile:
        assert tomllib.load(lakefile)["require"][0]["rev"] == revision


@pytest.mark.parametrize(
    ("versions", "code"),
    [
        ({"release_id": ""}, "project-release-unknown"),
        ({"release_id": None, "mathlib_rev": ""}, "project-version-invalid"),
    ],
)
def test_empty_version_options_are_not_defaults(tmp_path: Path, versions: dict[str, str | None], code: str) -> None:
    _refused(tmp_path / "Project", code, **versions)


def test_version_conflict_and_floor_messages_name_the_problem(tmp_path: Path) -> None:
    error = _refused(tmp_path / "Project", "project-version-invalid", mathlib_rev="master")
    assert error.message == "Choose a catalog release or a Lean toolchain and Mathlib revision, not both."

    result = create_project(tmp_path / "Old", package="Project", release_id=None, lean_toolchain="v4.26.0")
    message = dict(result.warnings)["project-lean-below-minimum"]
    assert "v4.27.0" in message
    assert "leanprover/lean4:v4.26.0" in message


@pytest.mark.parametrize("toolchain", ["v4." + "1" * 4301 + ".0", "v4.1234567890.0", "v4.30.0-rc1234567890"])
def test_oversized_version_components_are_invalid_not_a_crash(tmp_path: Path, toolchain: str) -> None:
    error = _refused(
        tmp_path / "Project", "project-version-invalid", release_id=None, lean_toolchain=toolchain, mathlib_rev="master"
    )
    assert "Lean toolchain" in error.message


def test_version_components_accept_nine_digits() -> None:
    assert create_module._LEAN_TOOLCHAIN.fullmatch("v123456789.123456789.123456789-rc123456789")


def test_group_writable_parent_is_refused_with_a_remedy(tmp_path: Path) -> None:
    parent = tmp_path / "group"
    parent.mkdir(mode=0o700)
    parent.chmod(0o775)

    assert "chmod g-w,o-w" in _refused(parent / "Project", "project-parent-unsafe").message


_NEEDS_PERMISSIONS = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="permission checks do not apply"
)


@_NEEDS_PERMISSIONS
def test_untraversable_ancestor_is_a_stable_error(tmp_path: Path, capsys) -> None:
    locked = tmp_path / "locked"
    (locked / "sub").mkdir(parents=True)
    target = os.fspath(locked / "sub" / "Project")
    locked.chmod(0o600)
    try:
        _fails(target, "project-parent-inaccessible")

        assert main(["project", "new", target, "--package", "Project", "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "project-parent-inaccessible"
    finally:
        locked.chmod(0o700)
    assert not (locked / "sub" / "Project").exists()


@_NEEDS_PERMISSIONS
def test_unreadable_parent_is_inaccessible_not_a_symlink(tmp_path: Path) -> None:
    parent = tmp_path / "write-only"
    parent.mkdir(mode=0o700)
    parent.chmod(0o300)
    try:
        _fails(parent / "Project", "project-parent-inaccessible")
    finally:
        parent.chmod(0o700)

    assert not list(parent.iterdir())


@_NEEDS_PERMISSIONS
@pytest.mark.parametrize(
    ("mode", "needed"),
    [(0o600, "read and search permission"), (0o400, "read and search permission"), (0o500, "write permission")],
    ids=["unsearchable", "read-only-unsearchable", "unwritable"],
)
def test_parent_permission_failures_name_the_missing_permission(tmp_path: Path, capsys, mode: int, needed: str) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = os.fspath(parent / "Project")
    parent.chmod(mode)
    try:
        error = _fails(target, "project-parent-inaccessible")
        assert main(["project", "new", target, "--package", "Project", "--json"]) == 1
    finally:
        parent.chmod(0o700)

    assert needed in error.message
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "project-parent-inaccessible"
    assert not list(parent.iterdir())


@pytest.mark.parametrize(
    ("relative", "code"),
    [
        ("missing/Project", "project-parent-missing"),
        ("file/sub/Project", "project-parent-missing"),
        ("file/Project", "project-parent-invalid"),
    ],
)
def test_missing_or_non_directory_parent_is_a_stable_error(tmp_path: Path, relative: str, code: str) -> None:
    (tmp_path / "file").write_text("", encoding="utf-8")

    _fails(tmp_path / relative, code)


@pytest.mark.parametrize(
    ("component", "code"), [("missing", "project-parent-missing"), ("overlong", "project-create-failed")]
)
def test_open_parent_classifies_lookup_failures(tmp_path: Path, component: str, code: str) -> None:
    name = "p" * (os.pathconf(tmp_path, "PC_NAME_MAX") + 1) if component == "overlong" else component

    with pytest.raises(ProjectCreateError) as raised:
        create_module._open_parent(tmp_path / name)

    assert raised.value.code == code


def test_overlong_parent_component_is_a_stable_error(tmp_path: Path) -> None:
    name_limit = os.pathconf(tmp_path, "PC_NAME_MAX")
    target = tmp_path / ("p" * (name_limit + 1)) / "Project"

    _fails(target, "project-parent-invalid")


def test_symlinked_parent_at_open_is_reported_as_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    target = link / "Project"
    monkeypatch.setattr(create_module, "_validate_target", lambda _target: target)

    _refused(target, "project-path-is-symlink")


def test_static_parent_alias_is_rejected_without_resolving_it(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    _refused(alias / "Project", "project-path-is-symlink")


def test_alias_cannot_be_swapped_after_resolution_before_link_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    first.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    original_resolve = Path.resolve
    stale_resolution_observed = False

    def resolve_then_replace(path: Path, *args, **kwargs):
        nonlocal stale_resolution_observed
        resolved = original_resolve(path, *args, **kwargs)
        if path == alias:
            stale_resolution_observed = True
            alias.unlink()
            alias.mkdir(mode=0o700)
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_then_replace)

    _fails(alias / "Project", "project-path-is-symlink")
    assert not stale_resolution_observed
    assert not (first / "Project").exists()
    assert not (alias / "Project").exists()


@pytest.mark.skipif(os.name != "posix", reason="project creation requires POSIX path binding")
def test_retargeted_parent_alias_never_publishes_at_the_old_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    target = alias / "Project"
    validate_package = create_module._validate_package

    def validate_then_retarget(package, parent):
        validated = validate_package(package, parent)
        alias.unlink()
        alias.symlink_to(second, target_is_directory=True)
        return validated

    monkeypatch.setattr(create_module, "_validate_package", validate_then_retarget)

    _fails(target, "project-path-is-symlink")
    assert not (first / "Project").exists()
    assert not (second / "Project").exists()
    assert not list(first.glob(".autoform-new-*"))
    assert not list(second.glob(".autoform-new-*"))


@pytest.mark.parametrize("component", ["parent", "ancestor"])
def test_file_swapped_in_before_open_is_not_called_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    ancestor = tmp_path / "ancestor"
    parent = ancestor / "parent"
    parent.mkdir(parents=True)
    swapped = parent if component == "parent" else ancestor
    validate_package = create_module._validate_package

    def validate_then_swap(package, directory):
        validated = validate_package(package, directory)
        shutil.rmtree(swapped)
        swapped.write_text("", encoding="utf-8")
        return validated

    monkeypatch.setattr(create_module, "_validate_package", validate_then_swap)

    error = _fails(parent / "Project", "project-parent-invalid")

    assert error.message == "The target parent or one of its ancestors is not a directory."
    assert swapped.is_file()


def test_parent_lock_held_elsewhere_fails_as_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fcntl = pytest.importorskip("fcntl")
    monkeypatch.setattr(create_module, "_LOCK_WAIT_SECONDS", 0.2)
    holder = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        _refused(tmp_path / "Project", "project-parent-busy")
    finally:
        os.close(holder)


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_interrupt_reports_the_possible_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, json_mode: bool
) -> None:
    def interrupt(*_args, **_kwargs) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(create_module, "_materialize_project", interrupt)
    arguments = ["project", "new", os.fspath(tmp_path / "Project"), "--package", "Project"]

    assert main([*arguments, "--json"] if json_mode else arguments) == 130

    captured = capsys.readouterr()
    if json_mode:
        assert json.loads(captured.out)["error"]["code"] == "project-create-interrupted"
        assert captured.err == ""
    else:
        assert captured.out == ""
        assert captured.err.startswith("error[project-create-interrupted]: ")
        assert ".autoform-new-*" in captured.err
    assert not (tmp_path / "Project").exists()
    assert len(list(tmp_path.glob(".autoform-new-*"))) == 1


@pytest.mark.parametrize(
    ("existing", "interrupted", "expected"),
    [
        (False, "_publication_state", "may be this run's complete project"),
        (True, "_build_project_plan", "no project was published"),
    ],
    ids=["after-publication", "target-already-existed"],
)
def test_cli_interrupt_says_whether_the_target_appeared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, existing: bool, interrupted: str, expected: str
) -> None:
    def interrupt(*_args, **_kwargs) -> None:
        raise KeyboardInterrupt

    target = tmp_path / "Project"
    if existing:
        target.mkdir()
    # On success, create_project checks the publication state only in its final cleanup.
    monkeypatch.setattr(create_module, interrupted, interrupt)

    assert main(["project", "new", os.fspath(target), "--package", "Project"]) == 130

    captured = capsys.readouterr()
    assert captured.err.startswith("error[project-create-interrupted]: ")
    assert expected in captured.err
    assert not list(tmp_path.glob(".autoform-new-*"))
    assert (target / "lakefile.toml").is_file() is not existing


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["project", "new"], "error[project-target-invalid]: A new project directory is required.\n"),
        (
            ["project", "new", "Project"],
            "error[project-name-invalid]: --package is required (an UpperCamelCase Lean package name).\n",
        ),
    ],
)
def test_cli_names_missing_required_options(arguments: list[str], expected: str, capsys) -> None:
    assert main(arguments) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == expected


def test_cli_human_errors_go_to_stderr(tmp_path: Path, capsys) -> None:
    arguments = ["project", "new", os.fspath(tmp_path / "Project"), "--package", "Project"]

    assert main([*arguments, "--release", _RELEASE, "--mathlib-rev", "master"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error[project-version-invalid]: Choose a catalog release or a Lean toolchain and Mathlib revision, not both.\n"
    )


def test_cli_human_output_for_a_pinned_catalog_release(tmp_path: Path, capsys) -> None:
    arguments = ["project", "new", os.fspath(tmp_path / "Project"), "--package", "Project"]

    assert main([*arguments, "--release", _RELEASE, "--autoform-source", _SOURCE, "--autoform-ref", "5" * 40]) == 0

    captured = capsys.readouterr()
    assert captured.out == f"Created Project at Project ({_RELEASE})\n"
    assert captured.err == ""


def test_cli_threads_the_toolchain_and_revision_together(tmp_path: Path, capsys) -> None:
    target = tmp_path / "Project"
    arguments = ["project", "new", os.fspath(target), "--package", "Project"]

    assert main([*arguments, "--lean-toolchain", "v4.30.0", "--mathlib-rev", "master", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["lean_toolchain"] == "leanprover/lean4:v4.30.0"
    assert payload["mathlib_rev"] == "master"
    with (target / "lakefile.toml").open("rb") as lakefile:
        assert tomllib.load(lakefile)["require"][0]["rev"] == "master"


@pytest.mark.skipif(os.name != "posix", reason="project creation is POSIX-only")
def test_cli_warnings_follow_the_created_line_on_a_shared_pipe(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoform_cli",
            "project",
            "new",
            os.fspath(tmp_path / "Project"),
            "--package",
            "Project",
            "--lean-toolchain",
            "v4.26.0",
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    lines = completed.stdout.splitlines()
    assert lines[0].startswith("Created Project at Project (unlisted: ")
    assert [line.partition("]")[0] for line in lines[1:3]] == [
        "warning[project-lean-below-minimum",
        "warning[project-release-unlisted",
    ]
