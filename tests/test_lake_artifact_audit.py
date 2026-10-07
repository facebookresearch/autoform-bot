from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


_TEMPLATE = Path("autoform_cli/templates/github/autoform_audit.py")


def _load_helper(repo_root: Path) -> ModuleType:
    path = repo_root / _TEMPLATE
    spec = importlib.util.spec_from_file_location("autoform_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


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
    assert 'Name.str (Name.anonymous) "Fixture", Name.str (Name.str (Name.anonymous) "Fixture") "Basic"' in probe
    assert 'throwError "kernel-trust audit found no root-package declarations"' in probe
    assert "info.isUnsafe || info.isPartial" in probe
    assert "Lean.collectAxioms" not in probe
    assert "autoformAuditAxioms env declName" in probe


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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(project: Path, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=180)


def _run_probe(project: Path, probe: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run *probe* as the audit step does: plain lean, given Lake's search path."""

    clean = {key: value for key, value in os.environ.items() if key != "LEAN_PATH"}
    lean_path = subprocess.run(
        ["lake", "env", "printenv", "LEAN_PATH"], cwd=project, env=clean, capture_output=True, text=True, timeout=180
    )
    assert lean_path.returncode == 0, lean_path.stdout + lean_path.stderr
    clean["AUTOFORM_AUDIT_LEAN_PATH"] = lean_path.stdout.rstrip("\n")
    return subprocess.run(
        ["lean", str(probe)], cwd=project, env={**clean, **env}, capture_output=True, text=True, timeout=180
    )


@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_toml_build_uses_target_src_dir_globs_and_import_closure(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "toml-project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
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


@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_root_package_clean_excludes_stale_custom_artifacts(
    helper: ModuleType, tmp_path: Path
) -> None:
    project = tmp_path / "stale-project"
    project.mkdir()
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
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


@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_lean_manifest_supports_custom_build_dir(helper: ModuleType, tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    _write(dependency / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
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
    _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
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


def test_example_and_template_helpers_are_identical(repo_root: Path) -> None:
    template = repo_root / _TEMPLATE
    example = repo_root / "skills/setup/assets/cabannes-thesis-project/.github/autoform_audit.py"

    assert example.read_bytes() == template.read_bytes()


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


def _audit_step(workflow: Path) -> str:
    text = workflow.read_text(encoding="utf-8")
    _, found, step = text.partition("      - name: Audit every root-package declaration\n        run: |\n")
    assert found
    lines = []
    for line in step.splitlines():
        if line and not line.startswith(" " * 10):
            break
        lines.append(line[10:])
    return "\n".join(lines) + "\n"


# Stubs for the step's commands: uvx answers as an AUTOFORM_REF with or without
# `work assumptions`, or as a failed fetch; python3 stands in for the audit, and
# lean for the probe, which prints STUB_PROBE_OUTPUT.
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
printf '%s\\n' "${LEAN_PATH-unset}" "$AUTOFORM_AUDIT_LEAN_PATH" "$@" > "$STUB_LOG/lean"
printf '%s' "$STUB_PROBE_OUTPUT"
"""
_CLEAN = "kernel trust clean (1 root-package declaration(s) audited)\n"


def _run_audit_step(
    repo_root: Path, tmp_path: Path, ref: str, policy: str, probe_output: str = _CLEAN
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
    }

    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
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
    result, runner_temp, log = _run_audit_step(repo_root, tmp_path, ref, policy)

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
    # Lake only reports the search path; plain lean runs the probe with it.
    assert (log / "lake").read_text(encoding="utf-8").splitlines() == ["env", "printenv", "LEAN_PATH"]
    assert (log / "lean").read_text(encoding="utf-8").splitlines() == ["unset", "/stub/lean/path", recorded[-1]]
    assert result.stdout.endswith(_CLEAN)


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

    assert [(entry.name, entry.article, entry.is_open, entry.allowed) for entry in entries] == [
        ("Fixture.open_stmt", "open", True, ("Fixture.open_stmt",)),
        ("Fixture.uses", "uses", False, ("Fixture.open_stmt",)),
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

    table = next(line for line in probe.splitlines() if "ReadTargets " in line and " with" in line)
    literal = table.split("ReadTargets ", 1)[1].rsplit(" with", 1)[0]
    assert json.loads(json.loads(literal)) == [
        [["Fixture", "a.b c"], 'odd "id"'],
        [["Fixture", "x", 1], 'odd "id"'],
    ]
    assert "is not a declaration of the Lean build" in probe
    old_form = helper.render_probe(("Fixture",))
    assert 'autoformAuditReadTargets "[]" with' in old_form
    for text in (probe, old_form, helper.render_open_probe(("Fixture",), ())):
        assert "Lean.Environment.replay rootConstants base" in text
        assert "kernel replay of the root package failed" in text
        assert "kernel replay of the root package skipped" in text
        assert "searchPathRef.set (libDir :: System.SearchPath.parse projectPath)" in text
        assert "(loadExts := false)" in text
        assert "is declared by an imported module" not in text
        # The replay runs compiled code, so it comes after every check.
        assert text.rindex("depends on unexpected axiom") < text.index("Lean.Environment.replay rootConstants")


def test_open_probe_spells_names_as_components(helper: ModuleType, tmp_path: Path) -> None:
    contract = _contract_file(
        tmp_path / "contract.json",
        _contract(
            _article("open", ["Fixture.«a.b c»"], is_open=True, allowed=["Fixture.«a.b c»"]),
            _article('odd "id" \\', ["Fixture.x.1"], allowed=["Fixture.«a.b c»"]),
        ),
    )

    probe = helper.render_open_probe(("Fixture",), helper.load_assumption_contract(contract))

    table = next(line for line in probe.splitlines() if "ReadArticles " in line and " with" in line)
    literal = table.split("ReadArticles ", 1)[1].rsplit(" with", 1)[0]
    assert json.loads(json.loads(literal)) == [
        [["Fixture", "a.b c"], "open", True, [["Fixture", "a.b c"]]],
        [["Fixture", "x", 1], 'odd "id" \\', False, [["Fixture", "a.b c"]]],
    ]
    assert probe.startswith("import Lean.Elab.Command\n")
    assert "import Fixture" not in probe
    assert "kernel trust clean except declared open statements" in probe
    assert "kernel trust clean (" not in probe
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
    assert "autoformOpenAuditReadArticles" in probe.read_text(encoding="utf-8")

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
    assert "autoformAuditReadTargets" in probe.read_text(encoding="utf-8")

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

# Sorry'd theorems the lexical `autoform check --lean-root` finds but Lean never
# compiles: one in a string, one after #exit, one in a file nothing imports.
_STRICT_LEAN = """import Dep

theorem Fixture.clean : True := Dep.dep_clean

def Fixture.text : String := "theorem Fixture.in_string : False := sorry"

#exit

theorem Fixture.after_exit : False := sorry
"""

_UNIMPORTED_LEAN = "theorem Fixture.unimported : False := sorry\n"

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


@pytest.fixture(scope="module")
def open_projects(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    if shutil.which("lake") is None:
        pytest.skip("Lake is not installed")
    root = tmp_path_factory.mktemp("open-statements")
    dependency = root / "dependency"
    _write(dependency / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
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
    ):
        project = root / name
        _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
        _write(
            project / "lakefile.toml",
            'name = "Fixture"\ndefaultTargets = ["Fixture"]\n\n'
            '[[require]]\nname = "Dep"\npath = "../dependency"\n\n'
            '[[lean_lib]]\nname = "Fixture"\n',
        )
        _write(project / "Fixture.lean", source)
        _write(project / "Fixture/Unimported.lean", _UNIMPORTED_LEAN)
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
    assert _strict_audit(helper, open_projects["strict"], None).returncode == 0
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
    walk_helpers = ["autoformAuditBlockOf", "autoformAuditEdges", "autoformAuditAxioms"]
    strict_helpers = ["autoformAuditNameOf", "autoformAuditReadTargets", *walk_helpers]
    open_helpers = [
        "autoformOpenAuditNameOf",
        "autoformOpenAuditReadArticles",
        *walk_helpers,
        "autoformOpenAuditOpenHits",
        "autoformOpenAuditReachesFailed",
        "autoformOpenAuditNameList",
    ]
    for audited, names in (
        (_strict_audit(helper, open_projects["strict"], [_article("helper", strict_helpers)]), strict_helpers),
        (_audit(helper, open_projects["open"], _contract(_OPEN_ARTICLE, _article("helper", open_helpers))), open_helpers),
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
            _article("sorry", ["Dep.dep_sorry"]),
        ),
    )

    output = audited.stdout + audited.stderr
    assert audited.returncode != 0, output
    for message in (
        "sorry-free: Nat.add_comm [core]",
        "Dep.dep_native [native] is outside the root package and depends on unexpected axiom ",
        "Dep.dep_unsafe [unsafe] is outside the root package and is unsafe or partial",
        "Dep.dep_sorry [sorry] is outside the root package and depends on sorry",
        "root-package declarations failed the open-statement audit",
    ):
        assert message in output
    assert "sorry-free: Dep." not in output
    assert "unexpected axiom sorryAx" not in output


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


# Each writes MARKER and exits 0 once the probe sets the trigger, so a probe
# that ran it would pass without a word.
_TRIGGER = "AUTOFORM_AUDIT_TEST_TRIGGER"

_INITIALIZER_LEAN = """initialize do
  if (← IO.getEnv "AUTOFORM_AUDIT_TEST_TRIGGER").isSome then
    IO.FS.writeFile MARKER "initializer ran\\n"
    IO.Process.exit 0
"""

# The kernel decides `Lean.reduceBool Fixture.evil = true` by running this code.
_NATIVE_DEP_LEAN = """unsafe def Fixture.evilImpl (_ : Unit) : Bool :=
  match unsafeIO (do
      if (← IO.getEnv "AUTOFORM_AUDIT_TEST_TRIGGER").isSome then
        IO.FS.writeFile MARKER "native code ran\\n"
        IO.Process.exit 0
      pure true) with
  | .ok value => value
  | .error _ => true

@[implemented_by Fixture.evilImpl] opaque Fixture.evilSpec : Unit → Bool

def Fixture.evil : Bool := Fixture.evilSpec ()
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
    if shutil.which("lake") is None:
        pytest.skip("Lake is not installed")
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
            {"Fixture.lean": "import Fixture.Dep\ntheorem Fixture.t : Lean.reduceBool Fixture.evil = true := rfl\n",
             "Fixture/Dep.lean": _NATIVE_DEP_LEAN},
            'name = "Fixture"\n',
        ),
        (
            "core-named",
            {"Lean/Hack.lean": _INITIALIZER_LEAN + "theorem Lean.Hack.cheat : 2 + 2 = 5 := sorry\n"},
            'name = "Fixture"\nroots = ["Lean.Hack"]\n',
        ),
        ("dangling", {"Fixture.lean": _DANGLING_LEAN}, 'name = "Fixture"\n'),
    ):
        project = root / name
        marker = json.dumps(str(project / "marker"))
        _write(project / "lean-toolchain", "leanprover/lean4:v4.32.2\n")
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
        assert not (project[0] / "marker").exists()


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
