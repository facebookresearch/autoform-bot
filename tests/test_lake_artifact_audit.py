from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

from autoform_cli import status

from tests.audit_fixture import (
    _TEMPLATE,
    _audit_step,
    _ends_with_the_success_line,
    _load_helper,
    _run,
    _run_probe,
    _write,
)
from tests.conftest import _SKELETON_FIXTURE, _lean_toolchain_available


_PROBE_SOURCE = Path("autoform_cli/probes/audit_probe.lean")
# The toolchain the real-Lean CI job installs.
_TOOLCHAIN = (_SKELETON_FIXTURE / "lean-toolchain").read_text(encoding="utf-8").strip() + "\n"
# Under AUTOFORM_REQUIRE_REAL_LEAN_TESTS=1 this raises instead of returning False.
_REAL_LEAN = _lean_toolchain_available()


@pytest.fixture
def helper(repo_root: Path) -> ModuleType:
    return _load_helper(repo_root)


def _metadata(module: str, *, declarations: dict[str, object] | None = None) -> bytes:
    return json.dumps(
        {
            "decls": declarations if declarations is not None else {"proof": []},
            "directImports": [],
            "module": module,
            "references": {},
            "version": 5,
        },
        separators=(",", ":"),
    ).encode()


def _trace(module: str, package: str = "Fixture") -> bytes:
    return json.dumps(
        {
            "synthetic": False,
            "inputs": [
                ["Module.name: " + module, "hash"],
                ["Package.id?: (some " + package + ")", "hash"],
            ],
        },
        separators=(",", ":"),
    ).encode()


def _module_members(module: str, *, package: str = "Fixture") -> list[tuple[str, bytes]]:
    stem = "./lib/lean/" + module.replace(".", "/")
    return [
        (f"{stem}.ilean", _metadata(module)),
        (f"{stem}.olean", b"olean"),
        (f"{stem}.trace", _trace(module, package)),
    ]


def _archive(path: Path, members: list[tuple[str, bytes | None]]) -> Path:
    with tarfile.open(path, "w:gz") as packed:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "elsewhere"
                packed.addfile(info)
            else:
                info.size = len(content)
                packed.addfile(info, io.BytesIO(content))
    return path


def test_root_package_comes_from_top_level_evaluated_config(
    helper: ModuleType, tmp_path: Path
) -> None:
    config = tmp_path / "evaluated.toml"
    _write(
        config,
        'name = "RootPackage"\nversion = "0.1.0"\n\n[[lean_lib]]\nname = "TargetName"\n',
    )

    assert helper.root_package_from_config(config) == "RootPackage"


@pytest.mark.parametrize(
    "text",
    [
        "version = \"0.1.0\"\n",
        'name = "One"\nname = "Two"\n',
        'name = "bad name"\n',
        '[[lean_lib]]\nname = "OnlyTarget"\n',
    ],
)
def test_invalid_evaluated_config_fails_closed(
    helper: ModuleType, tmp_path: Path, text: str
) -> None:
    config = tmp_path / "evaluated.toml"
    _write(config, text)

    with pytest.raises(helper.AuditInputError, match="root package name|package name"):
        helper.root_package_from_config(config)


def test_archive_modules_are_sorted_and_probe_fails_on_zero_declarations(
    helper: ModuleType, tmp_path: Path
) -> None:
    archive = _archive(
        tmp_path / "root.tgz",
        [*_module_members("Fixture.Basic"), *_module_members("Fixture")],
    )

    modules = helper.modules_from_archive(archive, "Fixture")
    probe = helper.render_probe(modules)

    assert modules == ("Fixture", "Fixture.Basic")
    assert probe.startswith("import Lean.Elab.Command\nimport Lean.Data.Json\nimport Lean.Replay\n\n")
    assert "import Fixture" not in probe
    assert _modules(probe) == [["Fixture"], ["Fixture", "Basic"]]
    assert 'throwError "kernel-trust audit found no root-package declarations"' in probe
    assert "isRoot declName && !autoformAuditCompiledCopy declName info" in probe
    assert "if autoformAuditCompiledCopy declName info then" in probe
    assert "autoformAuditUnsafeOrPartial env declName info" in probe
    assert "info.isUnsafe || info.isPartial" in probe
    assert "Lean.collectAxioms" not in probe
    assert "autoformAuditAxioms env declName" in probe


@pytest.mark.parametrize("character", [chr(0x7F), chr(0x85), chr(0xDCFF)])
def test_module_names_with_control_characters_or_lone_surrogates_are_refused(
    helper: ModuleType, character: str
) -> None:
    # Such a component would reach the probe's Lean source as it is.
    with pytest.raises(helper.AuditInputError, match="invalid module name"):
        helper._module_parts(f"Fixture.«{character}»", "lib/lean/Fixture.ilean")


def test_both_probes_spell_module_names_outside_the_basic_multilingual_plane(helper: ModuleType) -> None:
    # Lean reads a JSON surrogate pair such as \ud835\udd3d as two NUL characters.
    for probe in (helper.render_probe(("Fixture.𝔽",)), helper.render_open_probe(("Fixture.𝔽",), ())):
        assert _modules(probe) == [["Fixture", "𝔽"]]
        assert "\\ud835" not in probe


def _arguments(probe: str) -> tuple[str, object, object]:
    """The audit a rendered probe's last line runs, and the module list and table it passes."""

    last = probe.rstrip("\n").rpartition("\n")[2]
    call = re.fullmatch(r'run_cmd (\w+) ("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*")', last)
    assert call is not None and probe.endswith(f"{last}\n")
    entry, modules, table = call.groups()
    return entry, json.loads(json.loads(modules)), json.loads(json.loads(table))


def _modules(probe: str) -> object:
    """The JSON module list a rendered probe decodes."""

    return _arguments(probe)[1]


def _definition(source: str, name: str) -> str:
    """One top-level definition of the probe source, up to the next line that starts a command."""

    start = source.index(f"\ndef {name} ") + 1
    following = re.search(r"\n(?=\S)", source[start:])
    return source[start:] if following is None else source[start : start + following.start()]


def test_the_helper_carries_the_probe_source_verbatim(helper: ModuleType, repo_root: Path) -> None:
    source = (repo_root / _PROBE_SOURCE).read_text(encoding="utf-8")

    assert helper._PROBE == source, "the helper's copy of the probe is stale; run python3 scripts/sync_audit_probe.py"


def _toolchain_installed(pinned: str) -> bool:
    """Whether `lean`, run in a directory pinned to *pinned*, runs that toolchain without a download."""

    elan = shutil.which("elan")
    if elan is None:
        # Without elan, `lean` ignores lean-toolchain files.
        version = subprocess.run(["lean", "--version"], capture_output=True, text=True, timeout=30, check=False)
        return f"version {pinned.rpartition(':v')[2]}," in version.stdout
    listed = subprocess.run([elan, "toolchain", "list"], capture_output=True, text=True, timeout=30, check=False)
    return any(line.split()[:1] == [pinned] for line in listed.stdout.splitlines())


@pytest.mark.skipif(not _REAL_LEAN, reason="needs lake and the fixture's Lean toolchain")
@pytest.mark.parametrize("fixture", ["skeleton-project", "library-project"])
def test_the_probe_source_elaborates_cleanly_on_each_fixture_toolchain(
    repo_root: Path, tmp_path: Path, fixture: str
) -> None:
    # The probe imports only the toolchain's Lean, so Lean checks its source with no project.
    pinned = (repo_root / "tests/fixtures" / fixture / "lean-toolchain").read_text(encoding="utf-8")
    if not _toolchain_installed(pinned.strip()):
        if os.environ.get("AUTOFORM_REQUIRE_REAL_LEAN_TESTS") == "1":
            pytest.fail(f"{pinned.strip()} is not installed")
        pytest.skip(f"{pinned.strip()} is not installed")
    _write(tmp_path / "lean-toolchain", pinned)
    shutil.copyfile(repo_root / _PROBE_SOURCE, tmp_path / "audit_probe.lean")
    without = {key: value for key, value in os.environ.items() if key not in {"LEAN_PATH", "AUTOFORM_AUDIT_LEAN_PATH"}}

    run = subprocess.run(
        ["lean", "audit_probe.lean"], cwd=tmp_path, env=without, capture_output=True, text=True, timeout=300
    )

    # No error or warning, the v4.34 deprecation included.
    assert (run.returncode, run.stdout) == (0, ""), run.stdout + run.stderr


@pytest.mark.skipif(not _REAL_LEAN, reason="needs lake and the fixture's Lean toolchain")
def test_both_probes_read_more_modules_than_lean_compiles_in_a_list(helper: ModuleType, tmp_path: Path) -> None:
    # Lean's code generator gives up on a list literal of more than about 1,500
    # names. Outside the audit step, each probe reads its module list and then
    # stops at its first check.
    _write(tmp_path / "lean-toolchain", _TOOLCHAIN)
    modules = tuple(f"Fixture.M{index}" for index in range(3000))
    without = {key: value for key, value in os.environ.items() if key not in {"LEAN_PATH", "AUTOFORM_AUDIT_LEAN_PATH"}}
    probe = tmp_path / "probe.lean"
    for text in (helper.render_probe(modules), helper.render_open_probe(modules, ())):
        probe.write_text(text, encoding="utf-8")
        run = subprocess.run(
            ["lean", str(probe)], cwd=tmp_path, env=without, capture_output=True, text=True, timeout=180
        )
        assert "AUTOFORM_AUDIT_LEAN_PATH is not set" in run.stdout, run.stdout + run.stderr


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([], "contains no ILean artifacts"),
        ([("./lib/lean/Fixture.ilean", b"not json")], "malformed ILean metadata"),
        ([("../Fixture.ilean", _metadata("Fixture"))], "unsafe ILean archive member path"),
        ([("./lib/lean/Fixture.ilean", None)], "not a regular file"),
        (
            [
                *_module_members("Fixture"),
                ("./other/Fixture.ilean", _metadata("Fixture")),
                ("./other/Fixture.olean", b"olean"),
                ("./other/Fixture.trace", _trace("Fixture")),
            ],
            "duplicate ILean artifacts",
        ),
        (
            [("./lib/lean/Wrong.ilean", _metadata("Fixture"))],
            "does not match its archive path",
        ),
    ],
)
def test_archive_validation_fails_closed(
    helper: ModuleType,
    tmp_path: Path,
    members: list[tuple[str, bytes | None]],
    message: str,
) -> None:
    archive = _archive(tmp_path / "root.tgz", members)

    with pytest.raises(helper.AuditInputError, match=message):
        helper.modules_from_archive(archive, "Fixture")


def test_orphan_ilean_cannot_resolve_from_dependency(helper: ModuleType, tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "root.tgz",
        [("./lib/lean/Dependency.ilean", _metadata("Dependency"))],
    )

    with pytest.raises(helper.AuditInputError, match="no matching OLean"):
        helper.modules_from_archive(archive, "Fixture")


def test_dependency_trace_cannot_claim_root_ownership(helper: ModuleType, tmp_path: Path) -> None:
    archive = _archive(tmp_path / "root.tgz", _module_members("Dependency", package="Dependency"))

    with pytest.raises(helper.AuditInputError, match="does not identify root package"):
        helper.modules_from_archive(archive, "Fixture")


def test_duplicate_member_path_fails_closed(helper: ModuleType, tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "root.tgz",
        [
            ("./lib/lean/Fixture.ilean", _metadata("Fixture")),
            ("./lib/lean/Fixture.ilean", _metadata("Fixture")),
        ],
    )

    with pytest.raises(helper.AuditInputError, match="duplicate build archive member"):
        helper.modules_from_archive(archive, "Fixture")


def test_helper_runs_on_python_310(repo_root: Path, tmp_path: Path) -> None:
    python = shutil.which("python3.10")
    if python is None:
        pytest.skip("python3.10 is not installed")
    helper_path = repo_root / _TEMPLATE
    config = tmp_path / "evaluated.toml"
    _write(config, 'name = "Fixture"\n')
    identified = subprocess.run(
        [python, str(helper_path), "--root-package", str(config)],
        capture_output=True,
        text=True,
    )
    assert identified.returncode == 0, identified.stderr
    assert identified.stdout == "Fixture\n"

    archive = _archive(tmp_path / "root.tgz", _module_members("Fixture"))
    probe = tmp_path / "probe.lean"
    result = subprocess.run(
        [python, str(helper_path), "Fixture", str(archive), str(probe)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "prepared kernel-trust audit for 1 root-package module" in result.stdout
    assert probe.is_file()


@pytest.mark.skipif(not _REAL_LEAN, reason="needs lake and the fixture's Lean toolchain")
def test_real_toml_build_uses_target_src_dir_globs_and_import_closure(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "toml-project"
    project.mkdir()
    _write(project / "lean-toolchain", _TOOLCHAIN)
    _write(
        project / "lakefile.toml",
        '''name = "TomlFixture"
version = "0.1.0"
defaultTargets = ["runner"]
srcDir = "package-src"

[[lean_lib]]
name = "Chosen"
srcDir = "library-src"
globs = ["Chosen.+"]

[[lean_exe]]
name = "runner"
root = "Main"
srcDir = "app-src"
''',
    )
    _write(project / "package-src/library-src/Chosen/Entry.lean", "import Chosen.Helper\n")
    _write(
        project / "package-src/library-src/Chosen/Helper.lean",
        "theorem helper_ok : True := by trivial\n",
    )
    _write(
        project / "package-src/library-src/Outside.lean",
        "theorem omitted : True := by trivial\n",
    )
    _write(
        project / "package-src/app-src/Main.lean",
        "import Chosen.Entry\ndef main : IO Unit := pure ()\n",
    )
    _write(project / "package-src/PackageOnly.lean", "theorem package_only : True := by trivial\n")

    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    archive = project / "root.tgz"
    packed = _run(project, "lake", "pack", str(archive))
    assert packed.returncode == 0, packed.stdout + packed.stderr

    modules = helper.modules_from_archive(archive, "TomlFixture")
    assert modules == ("Chosen.Entry", "Chosen.Helper", "Main")
    assert "Outside" not in modules
    assert "PackageOnly" not in modules

    probe = project / "probe.lean"
    probe.write_text(helper.render_probe(modules), encoding="utf-8")
    audited = _run_probe(project, probe)
    assert audited.returncode == 0, audited.stdout + audited.stderr
    assert _ends_with_the_success_line(audited.stdout), audited.stdout


@pytest.mark.skipif(not _REAL_LEAN, reason="needs lake and the fixture's Lean toolchain")
def test_root_package_clean_excludes_stale_custom_artifacts(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "stale-project"
    project.mkdir()
    _write(project / "lean-toolchain", _TOOLCHAIN)
    _write(
        project / "lakefile.lean",
        '''import Lake
open Lake DSL
package «StaleFixture» where
  buildDir := "custom-output"
@[default_target]
lean_lib «Fresh»
''',
    )
    _write(project / "Fresh.lean", "theorem fresh_ok : True := by trivial\n")
    stale = project / "custom-output/lib/lean"
    _write(stale / "Stale.ilean", _metadata("Stale").decode())
    _write(stale / "Stale.olean", "stale")
    _write(stale / "Stale.trace", _trace("Stale", "StaleFixture").decode())

    loaded = _run(project, "lake", "env", "true")
    assert loaded.returncode == 0, loaded.stdout + loaded.stderr
    cleaned = _run(project, "lake", "clean", "StaleFixture")
    assert cleaned.returncode == 0, cleaned.stdout + cleaned.stderr
    assert not (project / "custom-output").exists()
    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    archive = project / "root.tgz"
    packed = _run(project, "lake", "pack", str(archive))
    assert packed.returncode == 0, packed.stdout + packed.stderr

    assert helper.modules_from_archive(archive, "StaleFixture") == ("Fresh",)


@pytest.mark.skipif(not _REAL_LEAN, reason="needs lake and the fixture's Lean toolchain")
def test_real_lean_manifest_supports_custom_build_dir(helper: ModuleType, tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    _write(dependency / "lean-toolchain", _TOOLCHAIN)
    _write(
        dependency / "lakefile.lean",
        '''import Lake
open Lake DSL
package «Dependency»
lean_lib «Dependency»
''',
    )
    _write(dependency / "Dependency.lean", "theorem dependency_ok : True := by trivial\n")

    project = tmp_path / "lean-project"
    project.mkdir()
    _write(project / "lean-toolchain", _TOOLCHAIN)
    _write(
        project / "lakefile.lean",
        '''import Lake
open Lake DSL

package «LeanFixture» where
  buildDir := "custom-output"
  srcDir := "package-src"

require «Dependency» from "../dependency"

@[default_target]
lean_lib «PublicApi» where
  srcDir := "sources"
  globs := #[.submodules `PublicApi]
''',
    )
    _write(
        project / "package-src/sources/PublicApi/Entry.lean",
        "import PublicApi.Internal\nimport Dependency\n",
    )
    _write(
        project / "package-src/sources/PublicApi/Internal.lean",
        "theorem internal_ok : True := by trivial\n",
    )
    _write(
        project / "package-src/sources/Outside.lean",
        "theorem omitted : True := by trivial\n",
    )

    built = _run(project, "lake", "build")
    assert built.returncode == 0, built.stdout + built.stderr
    archive = project / "root.tgz"
    packed = _run(project, "lake", "pack", str(archive))
    assert packed.returncode == 0, packed.stdout + packed.stderr

    assert (project / "custom-output/lib/lean/PublicApi/Entry.ilean").is_file()
    modules = helper.modules_from_archive(archive, "LeanFixture")
    assert modules == ("PublicApi.Entry", "PublicApi.Internal")
    assert "Dependency" not in modules

    probe = project / "probe.lean"
    probe.write_text(helper.render_probe(modules), encoding="utf-8")
    audited = _run_probe(project, probe)
    assert audited.returncode == 0, audited.stdout + audited.stderr
    assert _ends_with_the_success_line(audited.stdout), audited.stdout


def test_workflows_audit_open_statements_only_when_the_roadmap_allows_them(repo_root: Path) -> None:
    for workflow in (
        repo_root / "autoform_cli/templates/github/workflows/autoform-verify.yml",
        repo_root / "skills/setup/assets/cabannes-thesis-project/.github/workflows/autoform-verify.yml",
    ):
        text = workflow.read_text(encoding="utf-8")
        assert 'policy="$(python3 .github/autoform_audit.py --policy blueprint)"' in text
        assert 'if [ "$policy" = "allowed" ]; then' in text
        assert "autoform work assumptions blueprint --json > \"$contract\"" in text
        assert 'python3 .github/autoform_audit.py --open-statements "$contract"' in text
        assert 'python3 .github/autoform_audit.py --targets "$contract"' in text
        # Both policies need the contract, so it is written before the branch.
        assert text.index("autoform work assumptions") < text.index('if [ "$policy" = "allowed" ]; then')


# Stubs for the step's commands: uvx answers as an AUTOFORM_REF with or without
# `work assumptions`, or as a failed fetch; python3 stands in for the audit, and
# lean for the probe, which prints STUB_PROBE_OUTPUT and exits STUB_PROBE_EXIT.
_STUB_UVX = """#!/bin/sh
case "$STUB_REF" in
  old)
    echo "usage: autoform work [-h] {claim,release} ..." >&2
    echo "autoform work: error: argument command: invalid choice: 'assumptions' (choose from claim, release)" >&2
    exit 2 ;;
  offline) echo "error: Failed to fetch the Autoform source" >&2; exit 1 ;;
esac
case " $* " in
  *" --help "*) echo "usage: autoform work assumptions" ;;
  *) echo '{"articles": []}' ;;
esac
"""
_STUB_LAKE = """#!/bin/sh
case "$1" in
  pack) : > "$2" ;;
  env) printf '%s\\n' "$@" > "$STUB_LOG/lake"; echo "/stub/lean/path" ;;
esac
"""
_STUB_PYTHON = """#!/bin/sh
shift
if [ "$1" = "--policy" ]; then echo "$STUB_POLICY"; exit 0; fi
printf '%s\\n' "$@" > "$STUB_LOG/audit"
case "$1" in --*) cp "$2" "$STUB_LOG/contract" ;; esac
"""
_STUB_LEAN = """#!/bin/sh
printf '%s\\n' "${LEAN_PATH-unset}" "${LEAN_ABORT_ON_PANIC-unset}" "$AUTOFORM_AUDIT_LEAN_PATH" "$@" > "$STUB_LOG/lean"
printf '%s' "$STUB_PROBE_OUTPUT"
exit "${STUB_PROBE_EXIT:-0}"
"""
_CLEAN = "kernel trust clean (1 root-package declaration(s) audited)\n"
_OPEN_CLEAN = (
    "kernel trust clean except declared open statements "
    "(1 root-package declaration(s) audited; 0 open statement(s), 0 conditional declaration(s))\n"
)


def _run_audit_step(
    repo_root: Path, tmp_path: Path, ref: str, policy: str, probe_output: str = _CLEAN, probe_exit: int = 0
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    workflow = repo_root / "skills/setup/assets/cabannes-thesis-project/.github/workflows/autoform-verify.yml"
    script = tmp_path / "step.sh"
    script.write_text(_audit_step(workflow), encoding="utf-8")
    stubs = tmp_path / "bin"
    for name, text in (("uvx", _STUB_UVX), ("lake", _STUB_LAKE), ("python3", _STUB_PYTHON), ("lean", _STUB_LEAN)):
        _write(stubs / name, text)
        (stubs / name).chmod(0o755)
    project, runner_temp, log = tmp_path / "project", tmp_path / "runner-temp", tmp_path / "log"
    for directory in (project, runner_temp, log):
        directory.mkdir()
    env = {
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
        "AUTOFORM_SOURCE": "https://example.invalid/autoform-bot.git",
        "AUTOFORM_REF": "0" * 40,
        "AUTOFORM_ROOT_PACKAGE": "Root",
        "STUB_REF": ref,
        "STUB_POLICY": policy,
        "STUB_LOG": str(log),
        "STUB_PROBE_OUTPUT": probe_output,
        "STUB_PROBE_EXIT": str(probe_exit),
    }

    # The step sets no `shell:`, so GitHub runs it with `bash -e {0}` and only
    # the step's own `set -euo pipefail` turns pipefail on.
    result = subprocess.run(
        ["bash", "-e", str(script)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return result, runner_temp, log


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
@pytest.mark.parametrize(
    ("ref", "policy", "audit"),
    [
        ("new", "forbidden", ["--targets", ".json", "Root", ".tgz", ".lean"]),
        ("new", "allowed", ["--open-statements", ".json", "Root", ".tgz", ".lean"]),
        ("old", "forbidden", ["Root", ".tgz", ".lean"]),
        ("old", "allowed", None),
        ("offline", "forbidden", None),
    ],
)
def test_audit_step_falls_back_only_for_a_ref_without_work_assumptions(
    repo_root: Path, tmp_path: Path, ref: str, policy: str, audit: list[str] | None
) -> None:
    success = _OPEN_CLEAN if policy == "allowed" else _CLEAN
    result, runner_temp, log = _run_audit_step(repo_root, tmp_path, ref, policy, success)

    warned = "::warning::AUTOFORM_REF predates autoform work assumptions" in result.stdout
    assert warned == (audit is not None and ref == "old")
    assert list(runner_temp.iterdir()) == []
    if audit is None:
        assert result.returncode == 1
        assert "error:" in result.stderr
        assert not (log / "audit").exists()
        assert not (log / "lake").exists()
        assert not (log / "lean").exists()
        return
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = (log / "audit").read_text(encoding="utf-8").splitlines()
    assert [Path(arg).suffix or arg for arg in recorded] == audit
    assert (log / "contract").exists() == (ref == "new")
    if ref == "new":
        assert json.loads((log / "contract").read_text(encoding="utf-8")) == {"articles": []}
    # Lake only reports the search path; plain lean runs the probe with it, and
    # a panic aborts the probe.
    assert (log / "lake").read_text(encoding="utf-8").splitlines() == ["env", "printenv", "LEAN_PATH"]
    assert (log / "lean").read_text(encoding="utf-8").splitlines() == ["unset", "1", "/stub/lean/path", recorded[-1]]
    assert result.stdout.endswith(success)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
@pytest.mark.parametrize(
    "probe_output",
    ["", "sorry-free: Fixture.a [a]\n", _CLEAN + "later\n", "not " + _CLEAN, "kernel trust clean\n"],
)
def test_audit_step_fails_when_the_probe_exits_0_without_its_success_line(
    repo_root: Path, tmp_path: Path, probe_output: str
) -> None:
    result, runner_temp, log = _run_audit_step(repo_root, tmp_path, "new", "forbidden", probe_output)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "the audit probe did not end with its success line" in result.stderr
    assert (log / "lean").exists()
    assert list(runner_temp.iterdir()) == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_audit_step_fails_when_the_probe_exits_nonzero_after_its_success_line(
    repo_root: Path, tmp_path: Path
) -> None:
    # 134 is the status of a probe that LEAN_ABORT_ON_PANIC aborted.
    result, runner_temp, _ = _run_audit_step(repo_root, tmp_path, "new", "forbidden", probe_exit=134)

    assert result.returncode == 134, result.stdout + result.stderr
    assert result.stdout.endswith(_CLEAN)
    assert "the audit probe did not end with its success line" not in result.stderr
    assert list(runner_temp.iterdir()) == []


def _roadmap(blueprint: Path, text: str | bytes) -> None:
    path = blueprint / "roadmap/README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, "forbidden"),
        ("# Roadmap\n", "forbidden"),
        ("---\ntitle: Roadmap\n---\n# Roadmap\n", "forbidden"),
        ("---\nopen_statements: allowed\n---\n", "allowed"),
        ("---\nopen_statements: forbidden\n---\n", "forbidden"),
        ('---\nopen_statements: "allowed"\n---\n', "allowed"),
        ("---\n# policy\n\nopen_statements: 'Allowed'\n---\n", "allowed"),
        ("---\nopen_statements: ALLOWED\n---\n", "allowed"),
        ("# Roadmap\n---\nopen_statements: allowed\n---\n", "forbidden"),
    ],
)
def test_policy_reads_the_roadmap_frontmatter(
    helper: ModuleType, tmp_path: Path, text: str | None, expected: str
) -> None:
    blueprint = tmp_path / "blueprint"
    blueprint.mkdir()
    if text is not None:
        _roadmap(blueprint, text)

    assert helper.blueprint_policy(blueprint) == expected


def test_policy_ignores_every_page_but_the_roadmap(helper: ModuleType, tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write(blueprint / "README.md", "---\nopen_statements: allowed\n---\n")
    _write(blueprint / "roadmap/part/README.md", "---\nopen_statements: allowed\n---\n")
    _roadmap(blueprint, "---\ntitle: Roadmap\n---\n")

    assert helper.blueprint_policy(blueprint) == "forbidden"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("---\nopen_statements: allowed\nopen_statements: allowed\n---\n", "duplicate frontmatter key"),
        ("---\nopen_statements: maybe\n---\n", "accepts allowed or forbidden"),
        ("---\nopen_statements:\n---\n", "empty frontmatter value"),
        ('---\nopen_statements: ""\n---\n', "empty frontmatter value"),
        ("---\nopen_statements: allowed\n", "unterminated frontmatter"),
        (b"---\nopen_statements: allowed\xff\n---\n", "cannot read"),
    ],
)
def test_policy_refuses_ambiguous_frontmatter(
    helper: ModuleType, tmp_path: Path, text: str | bytes, message: str
) -> None:
    blueprint = tmp_path / "blueprint"
    _roadmap(blueprint, text)

    with pytest.raises(helper.AuditInputError, match=message):
        helper.blueprint_policy(blueprint)


def _article(
    article_id: str,
    declarations: list[str],
    *,
    is_open: bool = False,
    allowed: list[str] | None = None,
    state: str | None = None,
) -> dict[str, object]:
    return {
        "allowed_open_declarations": allowed or [],
        "article_id": None,
        "assumes": [],
        "declarations": declarations,
        "id": article_id,
        "open": is_open,
        "state": state or ("stated" if is_open else "proved"),
    }


def _contract(*articles: dict[str, object], open_statements: object = True) -> dict[str, object]:
    return {
        "articles": list(articles),
        "open_statements": open_statements,
        "schema": "autoform-assumptions/v1",
        "source_revision": "fixture",
    }


def _contract_file(path: Path, contract: object) -> Path:
    path.write_text(json.dumps(contract), encoding="utf-8")
    return path


def test_contract_lists_each_article_declaration(helper: ModuleType, tmp_path: Path) -> None:
    contract = _contract_file(
        tmp_path / "contract.json",
        _contract(
            _article("open", ["Fixture.open_stmt"], is_open=True, allowed=["Fixture.open_stmt"]),
            _article("uses", ["Fixture.uses", "Fixture.uses"], allowed=["Fixture.open_stmt"]),
        ),
    )

    entries = helper.load_assumption_contract(contract)

    assert [(entry.name, entry.article, entry.is_open, entry.allowed, entry.state) for entry in entries] == [
        ("Fixture.open_stmt", "open", True, ("Fixture.open_stmt",), "stated"),
        ("Fixture.uses", "uses", False, ("Fixture.open_stmt",), "proved"),
    ]


@pytest.mark.parametrize(
    ("contract", "message"),
    [
        ("not json", "cannot read the assumption contract"),
        ('{"schema": "a", "schema": "b"}', "duplicate key"),
        ('{"value": NaN}', "unsupported JSON constant"),
        ([], "not an autoform-assumptions/v1 object"),
        ({**_contract(), "schema": "autoform-assumptions/v2"}, "not an autoform-assumptions/v1 object"),
        (_contract(open_statements=False), "does not allow open statements"),
        (_contract(open_statements="true"), "does not allow open statements"),
        ({**_contract(), "articles": {}}, "no articles list"),
        (_contract("article"), "not an object"),  # type: ignore[arg-type]
        (_contract(_article("", ["Fixture.a"])), "invalid article id"),
        (_contract(_article("bad\nid", ["Fixture.a"])), "invalid article id"),
        (_contract(_article("a", ["Fixture.a"]), _article("a", ["Fixture.b"])), "lists a twice"),
        (_contract({**_article("a", ["Fixture.a"]), "open": "no"}), "malformed entry for a"),
        (_contract(_article("a", [])), "malformed entry for a"),
        (_contract({**_article("a", ["Fixture.a"]), "allowed_open_declarations": "x"}), "malformed entry"),
        (_contract({**_article("a", ["Fixture.a"]), "assumes": [1]}), "malformed entry for a"),
        (_contract({**_article("a", ["Fixture.a"]), "state": None}), "malformed entry for a"),
        (_contract({**_article("a", ["Fixture.a"]), "article_id": 7}), "malformed entry for a"),
        (_contract(_article("a", ["Fixture..a"])), "invalid Lean declaration"),
        (_contract(_article("a", ["Fixture.«a"])), "invalid Lean declaration"),
        (_contract(_article("a", ["Fixture.a b"])), "invalid Lean declaration"),
        (_contract(_article("a", [7])), "invalid Lean declaration"),  # type: ignore[list-item]
        (_contract(_article("a", ["Fixture.a"], allowed=["Fixture.\u0000"])), "invalid Lean declaration"),
        (
            _contract(
                _article("open", ["Fixture.s"], is_open=True, allowed=["Fixture.s"]),
                _article("proved", ["Fixture.s"]),
            ),
            "Fixture.s is an open statement of open but proved records it as proved",
        ),
        (
            _contract(_article("uses", ["Fixture.uses"], allowed=["Fixture.unknown"])),
            "lets uses rest on Fixture.unknown, which no open article declares",
        ),
    ],
)
def test_contract_validation_fails_closed(
    helper: ModuleType, tmp_path: Path, contract: object, message: str
) -> None:
    path = tmp_path / "contract.json"
    if isinstance(contract, str):
        path.write_text(contract, encoding="utf-8")
    else:
        _contract_file(path, contract)

    with pytest.raises(helper.AuditInputError, match=message):
        helper.load_assumption_contract(path)


def test_target_contract_lists_each_strict_article_declaration(helper: ModuleType, tmp_path: Path) -> None:
    contract = _contract_file(
        tmp_path / "contract.json",
        _contract(_article("a", ["Fixture.a", "Fixture.a"]), _article("m", ["Nat.add_comm"]), open_statements=False),
    )

    entries = helper.load_target_contract(contract)

    assert [(entry.name, entry.article, entry.is_open, entry.allowed) for entry in entries] == [
        ("Fixture.a", "a", False, ()),
        ("Nat.add_comm", "m", False, ()),
    ]


@pytest.mark.parametrize(
    ("contract", "message"),
    [
        (_contract(), "allows open statements; the strict audit accepts only a contract under the strict policy"),
        (_contract(open_statements="false"), "allows open statements"),
        ({**_contract(open_statements=False), "schema": "x"}, "not an autoform-assumptions/v1 object"),
        (
            _contract(_article("a", ["Fixture.a"], is_open=True), open_statements=False),
            "records a as open or resting on open statements, which the strict policy does not allow",
        ),
        (
            _contract(_article("a", ["Fixture.a"], allowed=["Fixture.a"]), open_statements=False),
            "records a as open or resting on open statements",
        ),
        (
            _contract({**_article("a", ["Fixture.a"]), "assumes": ["b"]}, open_statements=False),
            "records a as open or resting on open statements",
        ),
        (_contract(_article("a", ["Fixture..a"]), open_statements=False), "invalid Lean declaration"),
    ],
)
def test_target_contract_refuses_anything_but_the_strict_policy(
    helper: ModuleType, tmp_path: Path, contract: object, message: str
) -> None:
    path = _contract_file(tmp_path / "contract.json", contract)

    with pytest.raises(helper.AuditInputError, match=message):
        helper.load_target_contract(path)


def test_strict_probe_embeds_its_targets_and_imports_the_build_itself(helper: ModuleType, tmp_path: Path) -> None:
    contract = _contract_file(
        tmp_path / "contract.json",
        _contract(_article('odd "id"', ["Fixture.«a.b c»", "Fixture.x.1"]), open_statements=False),
    )

    probe = helper.render_probe(("Fixture",), helper.load_target_contract(contract))

    entry, _, table = _arguments(probe)
    assert entry == "autoformAuditStrict"
    # Each target carries its article's state and whether a wanted placeholder may back it.
    assert table == [
        [["Fixture", "a.b c"], 'odd "id"', "proved", False],
        [["Fixture", "x", 1], 'odd "id"', "proved", False],
    ]
    old_form = helper.render_probe(("Fixture",))
    assert _arguments(old_form) == ("autoformAuditStrict", [["Fixture"]], [])
    # Every render is the probe source and one line that runs an audit.
    for text in (probe, old_form, helper.render_open_probe(("Fixture",), ())):
        assert text.startswith(helper._PROBE) and text.count("\n", len(helper._PROBE)) == 2
    source = helper._PROBE
    assert "is not a declaration of the Lean build" in source
    assert "Lean.Environment.replay rootConstants base" in source
    assert "kernel replay of the root package failed" in source
    assert "searchPathRef.set (libDir :: projectEntries)" in source
    assert "with another library on the search path" in source
    assert "(loadExts := false)" in source
    assert "({ module := `Init } : Import)" in source
    assert "is declared by an imported module" not in source
    # The replay runs compiled code, so each audit calls it once every other check has passed.
    for entry in ("autoformAuditStrict", "autoformAuditOpen"):
        body = _definition(source, entry)
        replay = body.index("autoformAuditKernelReplay env")
        assert body.rindex("depends on unexpected axiom") < replay
        assert body.index("kernel replay of the root package skipped") < replay


def test_open_probe_spells_names_as_components(helper: ModuleType, tmp_path: Path) -> None:
    contract = _contract_file(
        tmp_path / "contract.json",
        _contract(
            _article("open", ["Fixture.«a.b c»"], is_open=True, allowed=["Fixture.«a.b c»"]),
            _article('odd "id" \\', ["Fixture.x.1"], allowed=["Fixture.«a.b c»"]),
        ),
    )

    probe = helper.render_open_probe(("Fixture",), helper.load_assumption_contract(contract))

    entry, _, table = _arguments(probe)
    assert entry == "autoformAuditOpen"
    assert table == [
        [["Fixture", "a.b c"], "open", True, [["Fixture", "a.b c"]], "stated", True],
        [["Fixture", "x", 1], 'odd "id" \\', False, [["Fixture", "a.b c"]], "proved", False],
    ]
    assert probe.startswith("import Lean.Elab.Command\n")
    assert "import Fixture" not in probe
    assert "kernel trust clean except declared open statements" in probe
    with pytest.raises(helper.AuditInputError, match="empty open-statement audit"):
        helper.render_open_probe((), ())


def test_helper_cli_forms(repo_root: Path, tmp_path: Path) -> None:
    helper_path = str(repo_root / _TEMPLATE)
    blueprint = tmp_path / "blueprint"
    _roadmap(blueprint, "---\nopen_statements: allowed\n---\n")

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, helper_path, *arguments], capture_output=True, text=True
        )

    policy = run("--policy", str(blueprint))
    assert policy.returncode == 0, policy.stderr
    assert policy.stdout == "allowed\n"
    _roadmap(blueprint, "---\nopen_statements: sometimes\n---\n")
    refused = run("--policy", str(blueprint))
    assert refused.returncode == 1
    assert refused.stderr.startswith("error: ") and "accepts allowed or forbidden" in refused.stderr

    archive = _archive(tmp_path / "root.tgz", _module_members("Fixture"))
    contract = _contract_file(
        tmp_path / "contract.json",
        _contract(_article("open", ["Fixture.s"], is_open=True, allowed=["Fixture.s"])),
    )
    probe = tmp_path / "probe.lean"
    prepared = run("--open-statements", str(contract), "Fixture", str(archive), str(probe))
    assert prepared.returncode == 0, prepared.stderr
    assert prepared.stdout == (
        "prepared open-statement audit for 1 root-package module(s) and 1 open statement candidate(s)\n"
    )
    assert _arguments(probe.read_text(encoding="utf-8"))[0] == "autoformAuditOpen"

    targeted = run("--targets", str(contract), "Fixture", str(archive), str(probe))
    assert targeted.returncode == 1
    assert targeted.stderr.startswith("error: ") and "allows open statements" in targeted.stderr

    _contract_file(contract, _contract(open_statements=False))
    forbidden = run("--open-statements", str(contract), "Fixture", str(archive), str(probe))
    assert forbidden.returncode == 1
    assert "does not allow open statements" in forbidden.stderr

    _contract_file(contract, _contract(_article("a", ["Fixture.a", "Fixture.b"]), open_statements=False))
    targeted = run("--targets", str(contract), "Fixture", str(archive), str(probe))
    assert targeted.returncode == 0, targeted.stderr
    assert targeted.stdout == "prepared kernel-trust audit for 1 root-package module(s) and 2 lean: target(s)\n"
    assert _arguments(probe.read_text(encoding="utf-8"))[0] == "autoformAuditStrict"

    for arguments in (
        (),
        ("--policy",),
        ("--policy", "a", "b"),
        ("--open-statements", "contract", "Fixture", "archive"),
        ("--open-statements", "contract", "Fixture", "archive", "probe", "extra"),
        ("--targets", "contract", "Fixture", "archive"),
        ("--targets", "contract", "--targets", "archive", "probe"),
    ):
        usage = run(*arguments)
        assert usage.returncode == 2, arguments
        assert usage.stderr.count("autoform_audit.py") == 5
        assert "--open-statements CONTRACT ROOT_PACKAGE ROOT_BUILD_ARCHIVE OUTPUT_PROBE" in usage.stderr
        assert "--targets CONTRACT ROOT_PACKAGE ROOT_BUILD_ARCHIVE OUTPUT_PROBE" in usage.stderr


_DEPENDENCY_LEAN = """theorem Dep.dep_sorry : True := sorry
theorem Dep.dep_clean : True := trivial
theorem Dep.dep_native : 10 + 10 = 20 := by native_decide
unsafe def Dep.dep_unsafe : Nat := 0
partial def Dep.dep_partial (n : Nat) : Nat := Dep.dep_partial (n + 1)
"""

_OPEN_LEAN = """import Dep

namespace Fixture

theorem open_stmt : 1 + 1 = 2 := sorry

theorem reduction : 1 + 1 = 2 ∧ True := ⟨open_stmt, trivial⟩

theorem clean : True := Dep.dep_clean

theorem done_stmt : True := trivial

end Fixture
"""

_BAD_LEAN = _OPEN_LEAN + """
namespace Fixture

theorem helper_sorry : True := sorry

def failed_open_type : Prop := sorry

theorem open_reaches_failed : failed_open_type := sorry

theorem where_sorry : True := aux
where aux : True := sorry

theorem type_sorry : (sorry : Prop) := sorry

theorem uses_dependency_sorry : True := Dep.dep_sorry

mutual
theorem evenA : ∀ n : Nat, n + 0 = n
  | 0 => rfl
  | n + 1 => by have := oddB n; sorry
theorem oddB : ∀ n : Nat, 0 + n = n
  | 0 => rfl
  | n + 1 => by have := evenA n; omega
end

theorem native : 10 + 10 = 20 := by native_decide

theorem uses_native : 10 + 10 = 20 := native

theorem native_reduction : 1 + 1 = 2 := by have := native; exact open_stmt

theorem native_open : 3 + 3 = 6 := by native_decide

theorem where_conditional : 1 + 1 = 2 ∧ True := ⟨open_stmt, aux⟩
where aux : True := sorry

theorem helper_conditional : 1 + 1 = 2 ∧ True := ⟨open_stmt, helper_sorry⟩

end Fixture
"""

# A sorry theorem that would pass as open if the probe read this forged table.
_FORGED_TABLE = json.dumps(json.dumps([[["Fixture", "cheat"], "x", True, [["Fixture", "cheat"]]]]))

_FORGED_LEAN = """import Lean.Data.Json

namespace Fixture

theorem cheat : 2 + 2 = 5 := sorry

theorem fully : 2 + 2 = 5 := cheat

end Fixture

def {parser} (_ : String) : Except String Lean.Json :=
  Lean.Json.parse {table}
"""

# A root constant with a helper's name, which once made the probe's own
# definition fail.
_CLASH_LEAN = """import Lean.Data.Json

namespace Fixture

theorem cheat : 2 + 2 = 5 := sorry

theorem fully : 2 + 2 = 5 := cheat

end Fixture

def autoformOpenAuditReadArticles (_ : String) :
    Except String (Array (Lean.Name × String × Bool × Array Lean.Name)) :=
  .ok #[(`Fixture.cheat, "x", true, #[`Fixture.cheat])]

def autoformAuditReadTargets (_ : String) : Except String (Array (Lean.Name × String)) :=
  .ok #[]
"""

# Sorry'd theorems Lean never compiles: one in a string, one after #exit, one in a
# file nothing imports.
_STRICT_LEAN = """import Dep

theorem Fixture.clean : True := Dep.dep_clean

def Fixture.text : String := "theorem Fixture.in_string : False := sorry"

#exit

theorem Fixture.after_exit : False := sorry
"""

_UNIMPORTED_LEAN = "theorem Fixture.unimported : False := sorry\n"

# Batteries' wanted commands elaborate to a private def whose type ends in one of
# these root-level wrappers (Batteries/Util/ProofWanted.lean). The fixture
# declares them itself, so the build needs no Batteries.
_WANTED_WRAPPERS_LEAN = """universe u

structure ProofWanted (α : Sort u) : Type where
  mk ::

structure DefWanted (α : Sort u) : Type where
  mk ::

structure DerivedWanted (α : Sort u) : Sort (max 1 u) where
  ofVal ::
  val : α
"""

# A placeholder named like one in the root module, which is private there too.
_WANTED_TWIN_LEAN = """import Fixture.Wrappers

namespace Fixture

private def twin : ProofWanted True := ⟨⟩

end Fixture
"""

# A placeholder whose body uses sorry, as `theorem_wanted broken : 1 = 2 := sorry`
# elaborates, fails its own checks, so it must get no wanted line.
_WANTED_BROKEN_LEAN = """import Fixture.Wrappers

namespace Fixture

private def broken : ProofWanted (1 = 2) := have : 1 = 2 := sorry; ⟨⟩

theorem clean : True := trivial

end Fixture
"""

# What `theorem_wanted wish (n : Nat) : n + 0 = n`, `def_wanted shape : Nat` and
# `def_wanted derived : Nat := 3` elaborate to, `alias ofWish := wish`, which
# Batteries makes a public def of the placeholder's type, a second `twin`, and a
# private theorem, which no wanted command made.
_WANTED_LEAN = """import Fixture.Twin

namespace Fixture

private def wish (n : Nat) : ProofWanted (n + 0 = n) := ⟨⟩

def ofWish (n : Nat) : ProofWanted (n + 0 = n) := wish n

private def shape : DefWanted Nat := ⟨⟩

@[reducible] private noncomputable def derived : DerivedWanted Nat := ⟨3⟩

private def twin : ProofWanted True := ⟨⟩

private theorem hidden : True := trivial

theorem clean : True := trivial

end Fixture
"""

# A declaration the kernel never checked. Neither the axiom walk nor the safety
# scan can tell; the workflow's lexical bypass check is not part of the probe.
_UNCHECKED_LEAN = """import Lean.Elab.Command

open Lean Elab Command

theorem Fixture.honest : True := trivial

set_option debug.skip""" + """KernelTC true in
run_cmd liftCoreM <| addDecl (.thmDecl
  { name := `Fixture.bogus, levelParams := [], type := mkConst ``False, value := mkConst ``True.intro })

theorem Fixture.uses_bogus : False := Fixture.bogus
"""

# Lean compiles each of these definitions through a partial `_unsafe_rec` copy,
# which both probes once refused as an unsafe or partial declaration.
_RECURSIVE_LEAN = """namespace Fixture

def countdown : Nat → Nat
  | 0 => 0
  | n + 1 => countdown n

def halve (n : Nat) : Nat :=
  if h : n < 2 then 0 else 1 + halve (n - 2)
termination_by n
decreasing_by omega

mutual
def isEven : Nat → Bool
  | 0 => true
  | n + 1 => isOdd n
def isOdd : Nat → Bool
  | 0 => false
  | n + 1 => isEven n
end

theorem countdown_zero : countdown 0 = 0 := rfl

end Fixture
"""

# A `partial def` is a safe opaque whose `_unsafe_rec` copy is partial, and the
# probes once named only the copy. An `unsafe def`, an unsafe constant with a
# copy's name, and a partial constant with an ordinary name, added by a root
# run_cmd with kernel checking on, must fail under their own names too. An
# opaque whose `_unsafe_rec` companion is unsafe, not partial, must not.
_PARTIAL_LEAN = """import Lean.Elab.Command

namespace Fixture

partial def loop (n : Nat) : Nat := loop (n + 1)

unsafe def raw : Nat := 0

opaque ordinaryWithNamedCompanion : Nat

unsafe def ordinaryWithNamedCompanion._unsafe_rec : Nat := 1

theorem fine : True := trivial

end Fixture

open Lean Elab Command

run_cmd liftCoreM <| addDecl (.mutualDefnDecl [
  { name := `Fixture.spin, levelParams := [], type := mkConst ``False,
    value := mkConst `Fixture.spin, hints := .opaque, safety := .partial }])
"""

# Two compiled copies the root scan skips: Lean's own for `countdown`, and a
# partial `False` that refers to itself, which a root run_cmd can add with
# kernel checking on. Each must still fail as a lean: target. A theorem of
# `False` whose proof is the second, added with kernel checking off, passes
# every other check; only the kernel replay, which leaves the copies out,
# refuses it.
_COPY_LEAN = """import Lean.Elab.Command

namespace Fixture

def countdown : Nat → Nat
  | 0 => 0
  | n + 1 => countdown n

end Fixture

open Lean Elab Command

run_cmd liftCoreM <| addDecl (.mutualDefnDecl [
  { name := `Fixture.cheat._unsafe_rec, levelParams := [], type := mkConst ``False,
    value := mkConst `Fixture.cheat._unsafe_rec, hints := .opaque, safety := .partial }])

set_option debug.skip""" + """KernelTC true in
run_cmd liftCoreM <| addDecl (.thmDecl
  { name := `Fixture.uses_copy, levelParams := [], type := mkConst ``False,
    value := mkConst `Fixture.cheat._unsafe_rec })
"""


@pytest.fixture(scope="module")
def open_projects(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    if not _REAL_LEAN:
        pytest.skip("needs lake and the fixture's Lean toolchain")
    root = tmp_path_factory.mktemp("open-statements")
    dependency = root / "dependency"
    _write(dependency / "lean-toolchain", _TOOLCHAIN)
    _write(dependency / "lakefile.toml", 'name = "Dep"\ndefaultTargets = ["Dep"]\n\n[[lean_lib]]\nname = "Dep"\n')
    _write(dependency / "Dep.lean", _DEPENDENCY_LEAN)
    projects: dict[str, tuple[Path, Path]] = {}
    for name, source in (
        ("open", _OPEN_LEAN),
        ("bad", _BAD_LEAN),
        # The probe's helpers once lived in this namespace, where the name took
        # precedence over the library parser.
        ("hijack", _FORGED_LEAN.format(parser="AutoformOpenStatementAudit.Json.parse", table=_FORGED_TABLE)),
        ("forged", _FORGED_LEAN.format(parser="Json.parse", table=_FORGED_TABLE)),
        ("clash", _CLASH_LEAN),
        ("strict", _STRICT_LEAN),
        ("unchecked", _UNCHECKED_LEAN),
        ("recursive", _RECURSIVE_LEAN),
        ("partial", _PARTIAL_LEAN),
        ("copy", _COPY_LEAN),
        ("wanted", _WANTED_LEAN),
        ("wanted-broken", _WANTED_BROKEN_LEAN),
    ):
        project = root / name
        _write(project / "lean-toolchain", _TOOLCHAIN)
        _write(
            project / "lakefile.toml",
            'name = "Fixture"\ndefaultTargets = ["Fixture"]\n\n'
            '[[require]]\nname = "Dep"\npath = "../dependency"\n\n'
            '[[lean_lib]]\nname = "Fixture"\n',
        )
        _write(project / "Fixture.lean", source)
        _write(project / "Fixture/Unimported.lean", _UNIMPORTED_LEAN)
        if name.startswith("wanted"):
            _write(project / "Fixture/Wrappers.lean", _WANTED_WRAPPERS_LEAN)
        if name == "wanted":
            _write(project / "Fixture/Twin.lean", _WANTED_TWIN_LEAN)
        built = _run(project, "lake", "build")
        assert built.returncode == 0, built.stdout + built.stderr
        archive = project / "root.tgz"
        packed = _run(project, "lake", "pack", str(archive))
        assert packed.returncode == 0, packed.stdout + packed.stderr
        projects[name] = (project, archive)
    return projects


def _audit(
    helper: ModuleType, project: tuple[Path, Path], contract: dict[str, object] | None
) -> subprocess.CompletedProcess[str]:
    directory, archive = project
    modules = helper.modules_from_archive(archive, "Fixture")
    if contract is None:
        text = helper.render_probe(modules)
    else:
        path = _contract_file(directory / "contract.json", contract)
        text = helper.render_open_probe(modules, helper.load_assumption_contract(path))
    probe = directory / "probe.lean"
    probe.write_text(text, encoding="utf-8")
    return _run_probe(directory, probe)


_OPEN_ARTICLE = _article("open", ["Fixture.open_stmt"], is_open=True, allowed=["Fixture.open_stmt"])


def test_open_probe_accepts_declared_open_statements_and_reductions(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    audited = _audit(
        helper,
        open_projects["open"],
        _contract(
            _OPEN_ARTICLE,
            _article("reduction", ["Fixture.reduction"], allowed=["Fixture.open_stmt"]),
            _article('clean "odd" \\ id', ["Fixture.clean"]),
            _article("mathlib", ["Dep.dep_clean"], state="mathlib"),
            _article("done", ["Fixture.done_stmt"], is_open=True, allowed=["Fixture.done_stmt"]),
        ),
    )

    output = audited.stdout + audited.stderr
    assert audited.returncode == 0, output
    assert "open statement (proof is sorry): Fixture.open_stmt [open]" in output
    assert "conditional: Fixture.reduction [reduction] rests on open statement(s) Fixture.open_stmt" in output
    assert 'sorry-free: Fixture.clean [clean "odd" \\ id]' in output
    assert "sorry-free: Dep.dep_clean [mathlib]" in output
    assert (
        "open statement (proof is sorry-free; restate it if retracted, then record proof: formalized): "
        "Fixture.done_stmt [done]"
    ) in output
    assert "kernel trust clean except declared open statements (" in output
    assert "; 2 open statement(s), 1 conditional declaration(s))" in output
    assert "kernel trust clean (" not in output
    assert _ends_with_the_success_line(audited.stdout), audited.stdout


@pytest.mark.parametrize(
    ("articles", "message"),
    [
        (
            [_OPEN_ARTICLE, _article("reduction", ["Fixture.reduction"])],
            "Fixture.reduction [reduction] rests on open statement(s) Fixture.open_stmt, "
            "which its article's Markdown dependencies do not reach",
        ),
        (
            [_article("open", ["Fixture.open_stmt"])],
            "Fixture.open_stmt contains sorry but is not an open statement",
        ),
        (
            [_OPEN_ARTICLE, _article("mathlib", ["Fixture.reduction"], state="mathlib")],
            "Fixture.reduction [mathlib] rests on open statement(s) Fixture.open_stmt, "
            "which its article's Markdown dependencies do not reach",
        ),
    ],
)
def test_open_probe_rejects_sorry_the_markdown_does_not_declare(
    helper: ModuleType,
    open_projects: dict[str, tuple[Path, Path]],
    articles: list[dict[str, object]],
    message: str,
) -> None:
    audited = _audit(helper, open_projects["open"], _contract(*articles))

    output = audited.stdout + audited.stderr
    assert audited.returncode != 0, output
    assert message in output
    assert "root-package declarations failed the open-statement audit" in output


def test_open_probe_rejects_sorry_outside_an_open_statement_body(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    audited = _audit(
        helper,
        open_projects["bad"],
        _contract(
            _OPEN_ARTICLE,
            _article("where", ["Fixture.where_sorry"], is_open=True, allowed=["Fixture.where_sorry"]),
            _article("type", ["Fixture.type_sorry"], is_open=True, allowed=["Fixture.type_sorry"]),
            _article("missing", ["Fixture.missing"]),
            _article("even", ["Fixture.evenA"], is_open=True, allowed=["Fixture.evenA"]),
        ),
    )

    output = audited.stdout + audited.stderr
    assert audited.returncode != 0, output
    for message in (
        "Fixture.helper_sorry contains sorry but is not an open statement",
        "Fixture.evenA._f contains sorry but is not an open statement: only a theorem that an open "
        "article's lean: names may keep a sorry, written directly in its own proof, not in a helper, "
        "where clause or definition; Lean compiles a recursive proof into auxiliaries such as _f and "
        "_unary, so a recursive open statement's proof must be exactly sorry",
        "Fixture.where_sorry.aux contains sorry but is not an open statement",
        "Fixture.type_sorry has sorry in its statement",
        "Fixture.uses_dependency_sorry uses Dep.dep_sorry, which is outside the root package and depends on sorry",
        "Fixture.missing [missing] is not a declaration of the Lean build",
        "root-package declarations failed the open-statement audit",
    ):
        assert message in output
    assert not any(
        line.startswith("open statement (") and "Fixture.type_sorry [type]" in line
        for line in output.splitlines()
    )


def test_open_probe_logs_no_clean_result_for_a_failing_declaration(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    audited = _audit(
        helper,
        open_projects["bad"],
        _contract(
            _OPEN_ARTICLE,
            _article("reduction", ["Fixture.reduction"]),
            _article("uses-native", ["Fixture.uses_native"]),
            _article("native-reduction", ["Fixture.native_reduction"], allowed=["Fixture.open_stmt"]),
            _article("native-open", ["Fixture.native_open"], is_open=True, allowed=["Fixture.native_open"]),
            _article(
                "open-reaches-failed",
                ["Fixture.open_reaches_failed"],
                is_open=True,
                allowed=["Fixture.open_reaches_failed"],
            ),
            _article("where-conditional", ["Fixture.where_conditional"], allowed=["Fixture.open_stmt"]),
            _article("helper-conditional", ["Fixture.helper_conditional"], allowed=["Fixture.open_stmt"]),
            _article("clean", ["Fixture.clean"]),
        ),
    )

    output = audited.stdout + audited.stderr
    assert audited.returncode != 0, output
    for message in (
        "Fixture.reduction [reduction] rests on open statement(s) Fixture.open_stmt, which",
        "Fixture.uses_native depends on unexpected axiom",
        "Fixture.native_reduction depends on unexpected axiom",
        "Fixture.native_open depends on unexpected axiom",
        "Fixture.failed_open_type contains sorry but is not an open statement",
        "Fixture.where_conditional.aux contains sorry but is not an open statement",
        "Fixture.helper_sorry contains sorry but is not an open statement",
        "open statement (proof is sorry): Fixture.open_stmt [open]",
        "sorry-free: Fixture.clean [clean]",
    ):
        assert message in output
    names = (
        "reduction",
        "uses_native",
        "native_reduction",
        "native_open",
        "open_reaches_failed",
        "where_conditional",
        "helper_conditional",
    )
    for name in names:
        for line in output.splitlines():
            if f"Fixture.{name} [" in line:
                assert not line.startswith(("sorry-free:", "conditional:", "open statement (")), line


@pytest.mark.parametrize("project", ["hijack", "forged"])
def test_open_probe_reads_its_table_with_the_library_parser(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]], project: str
) -> None:
    audited = _audit(
        helper,
        open_projects[project],
        _contract(_article("fully", ["Fixture.fully"]), _article("cheat", ["Fixture.cheat"])),
    )

    output = audited.stdout + audited.stderr
    assert audited.returncode != 0, output
    assert "Fixture.cheat contains sorry but is not an open statement" in output
    assert "Fixture.fully depends on sorry outside every declared open statement" in output
    assert "kernel trust clean" not in output


def test_open_probe_audits_a_build_that_declares_its_helper_names(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    audited = _audit(
        helper,
        open_projects["clash"],
        _contract(_article("fully", ["Fixture.fully"]), _article("cheat", ["Fixture.cheat"])),
    )

    output = audited.stdout + audited.stderr
    assert audited.returncode != 0, output
    assert "Fixture.cheat contains sorry but is not an open statement" in output
    assert "Fixture.fully depends on sorry outside every declared open statement" in output
    assert "is declared by an imported module" not in output
    assert "kernel trust clean" not in output


def test_strict_probe_still_rejects_every_sorry(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    audited = _audit(helper, open_projects["open"], None)

    output = audited.stdout + audited.stderr
    assert audited.returncode != 0, output
    assert "Fixture.open_stmt depends on unexpected axiom sorryAx" in output
    assert "root-package declarations failed the kernel-trust audit" in output


def _strict_audit(
    helper: ModuleType, project: tuple[Path, Path], targets: list[dict[str, object]] | None
) -> subprocess.CompletedProcess[str]:
    directory, archive = project
    modules = helper.modules_from_archive(archive, "Fixture")
    if targets is None:
        text = helper.render_probe(modules)
    else:
        path = _contract_file(directory / "contract.json", _contract(*targets, open_statements=False))
        text = helper.render_probe(modules, helper.load_target_contract(path))
    probe = directory / "probe.lean"
    probe.write_text(text, encoding="utf-8")
    return _run_probe(directory, probe)


def test_strict_probe_requires_every_target_and_checks_non_root_ones(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    # Without targets the strict audit passes: every sorry it can see is gone.
    passed = _strict_audit(helper, open_projects["strict"], None)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert _ends_with_the_success_line(passed.stdout), passed.stdout
    audited = _strict_audit(
        helper,
        open_projects["strict"],
        [
            _article("clean", ["Fixture.clean", "Dep.dep_clean", "Nat.add_comm"]),
            _article("string", ["Fixture.in_string"]),
            _article("exit", ["Fixture.after_exit"]),
            _article("unimported", ["Fixture.unimported"]),
            _article("native", ["Dep.dep_native"]),
            _article("unsafe", ["Dep.dep_unsafe"]),
            _article("partial", ["Dep.dep_partial"]),
            _article("copy", ["Dep.dep_partial._unsafe_rec"]),
            _article("sorry", ["Dep.dep_sorry"]),
        ],
    )

    output = audited.stdout + audited.stderr
    assert audited.returncode != 0, output
    for name, article in (
        ("Fixture.in_string", "string"),
        ("Fixture.after_exit", "exit"),
        ("Fixture.unimported", "unimported"),
    ):
        assert (
            f"{name} [{article}] is not a declaration of the Lean build; "
            "fix the article's lean: name or build the module that declares it"
        ) in output
    for message in (
        "Dep.dep_native [native] is outside the root package and depends on unexpected axiom ",
        "Dep.dep_unsafe [unsafe] is outside the root package and is unsafe or partial",
        "Dep.dep_partial [partial] is outside the root package and is unsafe or partial",
        "Dep.dep_partial._unsafe_rec [copy] is Lean's compiled copy of a recursive definition; name the definition itself",
        "Dep.dep_sorry [sorry] is outside the root package and depends on unexpected axiom sorryAx",
        "root-package declarations failed the kernel-trust audit",
    ):
        assert message in output
    assert "kernel replay of the root package skipped: it runs once every other check passes" in output
    for name in ("Fixture.clean", "Dep.dep_clean", "Nat.add_comm", "kernel replay of the root package failed"):
        assert name not in output

    passed = _strict_audit(
        helper, open_projects["strict"], [_article("clean", ["Fixture.clean", "Dep.dep_clean", "Nat.add_comm"])]
    )
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert "kernel trust clean (2 root-package declaration(s) audited)" in passed.stdout + passed.stderr
    assert _ends_with_the_success_line(passed.stdout), passed.stdout


_NOT_IN_BUILD = "is not a declaration of the Lean build; fix the article's lean: name or build the module that declares it"


def _wanted_refusal(name: str, article: str, wrapper: str, state: str) -> str:
    return (
        f"{name} [{article}] is a wanted placeholder ({wrapper}), which defers its proof or construction, "
        f"so it cannot back an article recorded as {state}"
    )


def test_strict_probe_resolves_wanted_targets_to_their_placeholders(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    # Batteries declares a wanted command's placeholder private, so env.find? on
    # the written name misses it, and every wanted target once failed CI.
    passed = _strict_audit(
        helper,
        open_projects["wanted"],
        [
            _article("wish", ["Fixture.wish"], state="stated"),
            _article("derived", ["Fixture.derived"], state="can_prove"),
            _article("alias", ["Fixture.ofWish"], state="planned"),
            _article("clean", ["Fixture.clean"]),
        ],
    )

    output = passed.stdout + passed.stderr
    assert passed.returncode == 0, output
    assert "wanted: Fixture.wish [wish]" in output
    assert "wanted: Fixture.derived [derived]" in output
    assert "wanted: Fixture.ofWish [alias]" in output
    assert _ends_with_the_success_line(passed.stdout), passed.stdout

    refused = _strict_audit(
        helper,
        open_projects["wanted"],
        [
            _article("proved", ["Fixture.wish"]),
            _article("defined", ["Fixture.shape"], state="defined"),
            _article("alias", ["Fixture.ofWish"], state="fully_proved"),
            _article("mathlib", ["Fixture.derived"], state="mathlib"),
            _article("twin", ["Fixture.twin"], state="stated"),
            _article("hidden", ["Fixture.hidden"], state="stated"),
            _article("typo", ["Fixture.wishes"], state="stated"),
        ],
    )

    output = refused.stdout + refused.stderr
    assert refused.returncode != 0, output
    for message in (
        _wanted_refusal("Fixture.wish", "proved", "ProofWanted", "proved"),
        _wanted_refusal("Fixture.shape", "defined", "DefWanted", "defined"),
        _wanted_refusal("Fixture.ofWish", "alias", "ProofWanted", "fully_proved"),
        _wanted_refusal("Fixture.derived", "mathlib", "DerivedWanted", "mathlib"),
        "Fixture.twin [twin] names a wanted placeholder in each of 2 modules; give them different names",
        f"Fixture.hidden [hidden] {_NOT_IN_BUILD}",
        f"Fixture.wishes [typo] {_NOT_IN_BUILD}",
    ):
        assert message in output
    assert "wanted:" not in output
    assert "kernel trust clean" not in output


def test_open_probe_resolves_wanted_targets_to_their_placeholders(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    passed = _audit(
        helper,
        open_projects["wanted"],
        _contract(
            _article("wish", ["Fixture.wish"], is_open=True, allowed=["Fixture.wish"]),
            _article("clean", ["Fixture.clean"]),
        ),
    )

    output = passed.stdout + passed.stderr
    assert passed.returncode == 0, output
    assert "wanted: Fixture.wish [wish]" in output
    assert "sorry-free: Fixture.clean [clean]" in output
    assert _ends_with_the_success_line(passed.stdout), passed.stdout

    refused = _audit(
        helper,
        open_projects["wanted"],
        _contract(
            _article("proved", ["Fixture.wish"]),
            _article("defined", ["Fixture.derived"], state="defined"),
            _article("alias", ["Fixture.ofWish"], state="conditional"),
            _article("twin", ["Fixture.twin"], is_open=True, allowed=["Fixture.twin"]),
        ),
    )

    output = refused.stdout + refused.stderr
    assert refused.returncode != 0, output
    for message in (
        _wanted_refusal("Fixture.wish", "proved", "ProofWanted", "proved"),
        _wanted_refusal("Fixture.derived", "defined", "DerivedWanted", "defined"),
        _wanted_refusal("Fixture.ofWish", "alias", "ProofWanted", "conditional"),
        "Fixture.twin [twin] names a wanted placeholder in each of 2 modules; give them different names",
    ):
        assert message in output
    assert "kernel trust clean" not in output


def test_only_unproved_states_may_be_backed_by_a_wanted_placeholder(helper: ModuleType, tmp_path: Path) -> None:
    # A deny list once missed `mathlib`, and any state a newer CLI adds; refuse all but these.
    may_be_wanted = {"stated", "can_prove", "can_state", "planned", "not_ready"}
    states = [state.key for state in status.STATES]
    assert may_be_wanted <= set(states)
    assert set(states) - may_be_wanted == {"mathlib", "fully_proved", "proved", "defined", "conditional"}
    contract = _contract_file(
        tmp_path / "contract.json",
        _contract(
            *(_article(state, [f"Fixture.{state}"], state=state) for state in [*states, "not_yet_known"]),
            open_statements=False,
        ),
    )

    probe = helper.render_probe(("Fixture",), helper.load_target_contract(contract))

    flags = {article: flag for _name, article, _state, flag in _arguments(probe)[2]}
    assert flags == {state: state in may_be_wanted for state in [*states, "not_yet_known"]}


def test_both_probes_give_no_wanted_line_to_a_placeholder_that_fails_its_own_checks(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    for audited in (
        _strict_audit(helper, open_projects["wanted-broken"], [_article("broken", ["Fixture.broken"], state="stated")]),
        _audit(helper, open_projects["wanted-broken"], _contract(
            _article("broken", ["Fixture.broken"], is_open=True, allowed=["Fixture.broken"]),
        )),
    ):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert "Fixture.broken" in output
        assert "wanted:" not in output
        assert "kernel trust clean" not in output


def test_strict_probe_audits_a_build_that_declares_its_helper_names(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    audited = _strict_audit(helper, open_projects["clash"], [_article("fully", ["Fixture.fully"])])

    output = audited.stdout + audited.stderr
    assert audited.returncode != 0, output
    assert "Fixture.cheat depends on unexpected axiom sorryAx" in output
    assert "Fixture.fully depends on unexpected axiom sorryAx" in output
    assert "is declared by an imported module" not in output
    assert "kernel trust clean" not in output


def test_both_probes_refuse_a_target_that_names_one_of_their_own_helpers(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    # The probe declares its helpers itself, so the build never compiled them.
    # A lean: name that matched one once passed as a clean declaration outside
    # the root package.
    names = re.findall(
        r"^(?:(?:private|protected|partial|unsafe|noncomputable) )*(?:def|abbrev|theorem|instance|opaque) (\w+)",
        helper._PROBE,
        re.MULTILINE,
    )
    assert {"autoformAuditStrict", "autoformAuditOpen", "autoformAuditKernelReplay"} <= set(names)
    for audited in (
        _strict_audit(helper, open_projects["strict"], [_article("helper", names)]),
        _audit(helper, open_projects["open"], _contract(_OPEN_ARTICLE, _article("helper", names))),
    ):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        for name in names:
            assert (
                f"{name} [helper] is not a declaration of the Lean build; "
                "fix the article's lean: name or build the module that declares it"
            ) in output
        assert "kernel trust clean" not in output


def test_open_probe_checks_the_axioms_of_a_target_outside_the_root_package(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    audited = _audit(
        helper,
        open_projects["open"],
        _contract(
            _OPEN_ARTICLE,
            _article("core", ["Nat.add_comm"]),
            _article("native", ["Dep.dep_native"]),
            _article("unsafe", ["Dep.dep_unsafe"]),
            _article("partial", ["Dep.dep_partial"]),
            _article("copy", ["Dep.dep_partial._unsafe_rec"]),
            _article("sorry", ["Dep.dep_sorry"]),
        ),
    )

    output = audited.stdout + audited.stderr
    assert audited.returncode != 0, output
    for message in (
        "sorry-free: Nat.add_comm [core]",
        "Dep.dep_native [native] is outside the root package and depends on unexpected axiom ",
        "Dep.dep_unsafe [unsafe] is outside the root package and is unsafe or partial",
        "Dep.dep_partial [partial] is outside the root package and is unsafe or partial",
        "Dep.dep_partial._unsafe_rec [copy] is Lean's compiled copy of a recursive definition; name the definition itself",
        "Dep.dep_sorry [sorry] is outside the root package and depends on sorry",
        "root-package declarations failed the open-statement audit",
    ):
        assert message in output
    assert "sorry-free: Dep." not in output
    assert "unexpected axiom sorryAx" not in output


def test_both_probes_accept_computable_recursive_definitions(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    project = open_projects["recursive"]
    for audited, messages in (
        (_strict_audit(helper, project, None), ["kernel trust clean ("]),
        (
            _audit(helper, project, _contract(_article("countdown-zero", ["Fixture.countdown_zero"]))),
            [
                "sorry-free: Fixture.countdown_zero [countdown-zero]",
                "kernel trust clean except declared open statements (",
            ],
        ),
    ):
        output = audited.stdout + audited.stderr
        assert audited.returncode == 0, output
        assert _ends_with_the_success_line(audited.stdout), audited.stdout
        for message in messages:
            assert message in output
        assert "_unsafe_rec" not in output


def test_both_probes_report_a_partial_def_under_its_own_name(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    project = open_projects["partial"]
    for audited, summary in (
        (_strict_audit(helper, project, None), "root-package declarations failed the kernel-trust audit"),
        (
            _audit(helper, project, _contract(_article("fine", ["Fixture.fine"]))),
            "root-package declarations failed the open-statement audit",
        ),
    ):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert "unsafe or partial declaration: Fixture.loop" in output
        assert "unsafe or partial declaration: Fixture.raw" in output
        assert "unsafe or partial declaration: Fixture.spin" in output
        assert "unsafe or partial declaration: Fixture.ordinaryWithNamedCompanion._unsafe_rec" in output
        assert not any(
            line.rstrip().endswith("unsafe or partial declaration: Fixture.ordinaryWithNamedCompanion")
            for line in output.splitlines()
        ), output
        assert summary in output
        assert "Fixture.loop._unsafe_rec" not in output


def test_both_probes_refuse_a_target_that_names_a_compiled_copy(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    project = open_projects["copy"]
    targets = [
        _article("countdown", ["Fixture.countdown._unsafe_rec"]),
        _article("cheat", ["Fixture.cheat._unsafe_rec"]),
    ]
    for audited, summary in (
        (_strict_audit(helper, project, targets), "root-package declarations failed the kernel-trust audit"),
        (_audit(helper, project, _contract(*targets)), "root-package declarations failed the open-statement audit"),
    ):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        for name, article in (
            ("Fixture.countdown._unsafe_rec", "countdown"),
            ("Fixture.cheat._unsafe_rec", "cheat"),
        ):
            assert (
                f"{name} [{article}] is Lean's compiled copy of a recursive definition; name the definition itself"
            ) in output
        assert summary in output
        assert "sorry-free:" not in output


def test_both_probes_fail_a_build_whose_only_problem_is_a_copy_target(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    # The recursive project passes both probes, so the copy-target refusal alone has to fail the run.
    targets = [_article("countdown", ["Fixture.countdown._unsafe_rec"])]
    for audited, summary in _both_probes(helper, open_projects["recursive"], targets):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert (
            "Fixture.countdown._unsafe_rec [countdown] is Lean's compiled copy of a recursive definition; "
            "name the definition itself"
        ) in output
        assert summary in output
        assert "kernel trust clean" not in output


def test_both_probes_refuse_an_unchecked_theorem_that_uses_a_compiled_copy(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    rejected = "kernel replay of the root package failed: while replaying declaration 'Fixture.uses_copy'"
    for audited, summary in _both_probes(helper, open_projects["copy"], [_article("uses-copy", ["Fixture.uses_copy"])]):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert rejected in output
        assert summary in output
        assert "sorry-free:" not in output
        assert "kernel trust clean" not in output


def test_both_probes_replay_the_root_package_through_the_kernel(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    rejected = "kernel replay of the root package failed: while replaying declaration 'Fixture.bogus'"
    for audited, summary in (
        (_strict_audit(helper, open_projects["unchecked"], None), "failed the kernel-trust audit"),
        (
            _audit(
                helper,
                open_projects["unchecked"],
                _contract(_article("bogus", ["Fixture.uses_bogus"]), _article("honest", ["Fixture.honest"])),
            ),
            "failed the open-statement audit",
        ),
    ):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert rejected in output
        assert "declaration type mismatch" in output
        # The older checks pass: the replay is the only error.
        errors = [line for line in output.splitlines() if ": error: " in line]
        assert len(errors) == 2, output
        assert rejected in errors[0] and summary in errors[1]
        assert "sorry-free:" not in output


# Each writes MARKER and exits 0 once the test sets the trigger, so a probe
# that ran it would pass without a word.
_TRIGGER = "AUTOFORM_AUDIT_TEST_TRIGGER"

_INITIALIZER_LEAN = """initialize do
  if (← IO.getEnv "AUTOFORM_AUDIT_TEST_TRIGGER").isSome then
    IO.FS.writeFile MARKER "initializer ran\\n"
    IO.Process.exit 0
"""

# The kernel decides `Lean.reduceBool Dep.evil = true` by running this code. It
# sits in a dependency because a replay runs it only from the replay's base,
# which holds the modules outside the root package.
_NATIVE_DEP_LEAN = """unsafe def Dep.evilImpl (_ : Unit) : Bool :=
  match unsafeIO (do
      if (← IO.getEnv "AUTOFORM_AUDIT_TEST_TRIGGER").isSome then
        IO.FS.writeFile MARKER "native code ran\\n"
        IO.Process.exit 0
      pure true) with
  | .ok value => value
  | .error _ => true

@[implemented_by Dep.evilImpl] opaque Dep.evilSpec : Unit → Bool

def Dep.evil : Bool := Dep.evilSpec ()
"""

_MUTUAL_DEP_LEAN = """axiom MDep.Bad : Type

mutual
inductive MDep.A0 : Type where
  | mk : MDep.Bad → MDep.B0 → MDep.A0
  | base : MDep.A0
inductive MDep.B0 : Type where
  | mk : MDep.A0 → MDep.B0
end
"""

# A declaration whose proof names a constant that no module declares.
_DANGLING_LEAN = """import Lean.Elab.Command

open Lean Elab Command

set_option debug.skip""" + """KernelTC true in
run_cmd liftCoreM <| addDecl (.thmDecl
  { name := `Fixture.dangling, levelParams := [], type := mkConst ``True, value := mkConst `Fixture.ghost })
"""


@pytest.fixture(scope="module")
def hostile_projects(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    if not _REAL_LEAN:
        pytest.skip("needs lake and the fixture's Lean toolchain")
    root = tmp_path_factory.mktemp("hostile")
    projects: dict[str, tuple[Path, Path]] = {}
    for name, files, lib in (
        (
            "initializer",
            {"Fixture.lean": "import Fixture.Init\ntheorem Fixture.cheat : 2 + 2 = 5 := sorry\n",
             "Fixture/Init.lean": _INITIALIZER_LEAN},
            'name = "Fixture"\n',
        ),
        (
            "native",
            {"Fixture.lean": "import EvilDep\ntheorem Fixture.t : Lean.reduceBool Dep.evil = true := rfl\n",
             "evildep/lean-toolchain": _TOOLCHAIN,
             "evildep/lakefile.toml": 'name = "evildep"\ndefaultTargets = ["EvilDep"]\n\n[[lean_lib]]\nname = "EvilDep"\n',
             "evildep/EvilDep.lean": _NATIVE_DEP_LEAN},
            'name = "Fixture"\n\n[[require]]\nname = "evildep"\npath = "evildep"\n',
        ),
        (
            "core-named",
            {"Lean/Hack.lean": _INITIALIZER_LEAN + "theorem Lean.Hack.cheat : 2 + 2 = 5 := sorry\n"},
            'name = "Fixture"\nroots = ["Lean.Hack"]\n',
        ),
        (
            # A dependency that declares an axiom and a mutual inductive using it.
            "mutual-axiom",
            {"Fixture.lean": "import MDep\ntheorem Fixture.t0 : ∀ _ : MDep.B0, True := fun _ => trivial\n",
             "mdep/lean-toolchain": _TOOLCHAIN,
             "mdep/lakefile.toml": 'name = "mdep"\ndefaultTargets = ["MDep"]\n\n[[lean_lib]]\nname = "MDep"\n',
             "mdep/MDep.lean": _MUTUAL_DEP_LEAN},
            'name = "Fixture"\n\n[[require]]\nname = "mdep"\npath = "mdep"\n',
        ),
        ("dangling", {"Fixture.lean": _DANGLING_LEAN}, 'name = "Fixture"\n'),
        (
            # A theorem of `True` in place of Init's `Lean.trustCompiler`, which
            # `Lean.reduceBool` depends on.
            "trust-theorem",
            {"Fixture.lean": "import Fixture.Trust\nimport EvilDep\ntheorem Fixture.t : Lean.reduceBool Dep.evil = true := rfl\n",
             "Fixture/Trust.lean": "prelude\nimport Init.Prelude\ntheorem Lean.trustCompiler : True := True.intro\n",
             "evildep/lean-toolchain": _TOOLCHAIN,
             "evildep/lakefile.toml": 'name = "evildep"\ndefaultTargets = ["EvilDep"]\n\n[[lean_lib]]\nname = "EvilDep"\n',
             "evildep/EvilDep.lean": _NATIVE_DEP_LEAN},
            'name = "Fixture"\n\n[[require]]\nname = "evildep"\npath = "evildep"\n',
        ),
        (
            "prelude",
            {"Fixture.lean": "prelude\nimport Fixture.Core\ntheorem Fixture.cheat (p : Prop) : p := propext p\n",
             "Fixture/Core.lean": "prelude\naxiom propext (p : Prop) : p\n"},
            'name = "Fixture"\n',
        ),
        (
            # A dependency library whose clean `Fixture` comes first on the search path.
            "shadow",
            {"Fixture.lean": "theorem Fixture.cheat : 2 + 2 = 5 := sorry\n",
             "evildep/lean-toolchain": _TOOLCHAIN,
             "evildep/lakefile.toml": 'name = "evildep"\n\n[[lean_lib]]\nname = "Shadow"\nroots = ["Fixture"]\nsrcDir = "src"\n',
             "evildep/src/Fixture.lean": "theorem Fixture.cheat : 2 + 2 = 4 := rfl\n"},
            'name = "Fixture"\nneeds = ["evildep/Shadow"]\n\n[[require]]\nname = "evildep"\npath = "evildep"\n',
        ),
    ):
        project = root / name
        marker = json.dumps(str(project / "marker"))
        _write(project / "lean-toolchain", _TOOLCHAIN)
        _write(project / "lakefile.toml", f'name = "Fixture"\ndefaultTargets = ["Fixture"]\n\n[[lean_lib]]\n{lib}')
        for path, source in files.items():
            _write(project / path, source.replace("MARKER", marker))
        built = _run(project, "lake", "build")
        assert built.returncode == 0, built.stdout + built.stderr
        archive = project / "root.tgz"
        packed = _run(project, "lake", "pack", str(archive))
        assert packed.returncode == 0, packed.stdout + packed.stderr
        projects[name] = (project, archive)
    return projects


def _both_probes(
    helper: ModuleType, project: tuple[Path, Path], articles: list[dict[str, object]]
) -> list[tuple[subprocess.CompletedProcess[str], str]]:
    return [
        (_strict_audit(helper, project, articles), "root-package declarations failed the kernel-trust audit"),
        (_audit(helper, project, _contract(*articles)), "root-package declarations failed the open-statement audit"),
    ]


def test_both_probes_run_no_initializer_of_the_build(
    helper: ModuleType, hostile_projects: dict[str, tuple[Path, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_TRIGGER, "1")
    project = hostile_projects["initializer"]
    for audited, summary in _both_probes(helper, project, [_article("cheat", ["Fixture.cheat"])]):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert (
            "Fixture.cheat depends on unexpected axiom sorryAx" in output
            or "Fixture.cheat contains sorry but is not an open statement" in output
        ), output
        assert summary in output
        assert "kernel trust clean" not in output
        assert not (project[0] / "marker").exists()


def test_both_probes_reject_native_reduction_without_running_it(
    helper: ModuleType, hostile_projects: dict[str, tuple[Path, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_TRIGGER, "1")
    project = hostile_projects["native"]
    for audited, summary in _both_probes(helper, project, [_article("t", ["Fixture.t"])]):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert "Fixture.t depends on unexpected axiom Lean.trustCompiler" in output
        assert "kernel replay of the root package skipped: it runs once every other check passes" in output
        assert summary in output
        assert "kernel trust clean" not in output
        assert not (project[0] / "marker").exists()


def test_both_probes_refuse_a_root_module_named_after_the_toolchain_library(
    helper: ModuleType, hostile_projects: dict[str, tuple[Path, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_TRIGGER, "1")
    project = hostile_projects["core-named"]
    assert helper.modules_from_archive(project[1], "Fixture") == ("Lean.Hack",)
    for audited, _ in _both_probes(helper, project, [_article("cheat", ["Lean.Hack.cheat"])]):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert (
            "root module Lean.Hack shares its first component Lean with the toolchain's library; "
            "rename the module so the audit can run"
        ) in output
        assert "kernel trust clean" not in output
        assert not (project[0] / "marker").exists()


def test_both_probes_refuse_a_root_module_another_library_also_provides(
    helper: ModuleType, hostile_projects: dict[str, tuple[Path, Path]]
) -> None:
    project = hostile_projects["shadow"]
    assert helper.modules_from_archive(project[1], "Fixture") == ("Fixture",)
    for audited, _ in _both_probes(helper, project, [_article("cheat", ["Fixture.cheat"])]):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        message = next((line for line in output.splitlines() if "with another library on the search path" in line), "")
        assert (
            "root module Fixture shares its first component Fixture with another library on the search path ("
            in message
        ), output
        assert message.endswith("); rename the module so the audit can run"), output
        # The dependency's library comes first, as Lake lists it.
        dependency = str((project[0] / "evildep/.lake/build/lib/lean").resolve())
        root = str((project[0] / ".lake/build/lib/lean").resolve())
        assert f"({dependency}, {root})" in message, output
        assert "kernel trust clean" not in output


def test_both_probes_refuse_a_build_that_declares_a_core_name_of_its_own(
    helper: ModuleType, hostile_projects: dict[str, tuple[Path, Path]]
) -> None:
    # The build never imports Init, so its `propext` would match the allowlist by name.
    for audited, _ in _both_probes(helper, hostile_projects["prelude"], [_article("cheat", ["Fixture.cheat"])]):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert "environment already contains 'propext'" in output
        assert "kernel trust clean" not in output


def test_both_probes_refuse_a_theorem_in_place_of_an_axiom(
    helper: ModuleType, hostile_projects: dict[str, tuple[Path, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lean keeps the theorem under the axiom's module, so without the refusal the
    # axiom check passes `Fixture.t` and the replay runs the dependency's code.
    monkeypatch.setenv(_TRIGGER, "1")
    project = hostile_projects["trust-theorem"]
    for audited, _ in _both_probes(helper, project, [_article("t", ["Fixture.t"])]):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert (
            "Fixture.Trust declares a theorem Lean.trustCompiler in place of the axiom Init.Core declares; "
            "rename one of them so the audit can run"
        ) in output
        assert "kernel trust clean" not in output
        assert not (project[0] / "marker").exists()


def test_both_probes_find_an_axiom_a_dependency_reaches_through_a_mutual_inductive(
    helper: ModuleType, hostile_projects: dict[str, tuple[Path, Path]]
) -> None:
    # On v4.32.2 `collectAxioms` reads the axiom tables the modules store, and
    # `MDep`'s leaves `MDep.Bad` out of `MDep.B0`'s, so the probe before #190 passed this.
    for audited, summary in _both_probes(helper, hostile_projects["mutual-axiom"], [_article("t0", ["Fixture.t0"])]):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert "Fixture.t0 depends on unexpected axiom MDep.Bad" in output
        assert summary in output
        assert "kernel trust clean" not in output


def test_both_probes_fail_closed_on_a_constant_no_module_declares(
    helper: ModuleType, hostile_projects: dict[str, tuple[Path, Path]]
) -> None:
    for audited, summary in _both_probes(helper, hostile_projects["dangling"], [_article("d", ["Fixture.dangling"])]):
        output = audited.stdout + audited.stderr
        assert audited.returncode != 0, output
        assert "Fixture.dangling depends on Fixture.ghost, which no module of the build declares" in output
        assert summary in output
        assert "kernel trust clean" not in output


def test_both_probes_refuse_to_run_outside_the_audit_step(
    helper: ModuleType, open_projects: dict[str, tuple[Path, Path]]
) -> None:
    directory, archive = open_projects["strict"]
    modules = helper.modules_from_archive(archive, "Fixture")
    probe = directory / "refused.lean"
    without = {key: value for key, value in os.environ.items() if key not in {"LEAN_PATH", "AUTOFORM_AUDIT_LEAN_PATH"}}
    for text in (helper.render_probe(modules), helper.render_open_probe(modules, ())):
        probe.write_text(text, encoding="utf-8")
        through_lake = _run(directory, "lake", "env", "lean", str(probe))
        assert through_lake.returncode != 0
        assert "LEAN_PATH is set, so this probe's imports may come from the project" in through_lake.stdout
        bare = subprocess.run(
            ["lean", str(probe)], cwd=directory, env=without, capture_output=True, text=True, timeout=180
        )
        assert bare.returncode != 0
        assert "AUTOFORM_AUDIT_LEAN_PATH is not set" in bare.stdout
