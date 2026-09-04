"""Structured project-import contracts for the shared Lean REPL."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from servers.repl import core as repl_core
from servers.repl import imports as repl_imports
from servers.repl.pool import LeanReplPool, LeanReplPoolConfig
from servers.repl.imports import (
    LeanImportHeaderError,
    LeanImportError,
    ResolvedImports,
    clean_lake_environment,
    lean_project_fingerprint,
    resolve_project_imports,
    split_imports_and_body,
    validate_imports,
)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "Fixture"\n', encoding="utf-8")
    (project / "lake-manifest.json").write_text('{"version": "1.1.0", "packages": []}\n')
    return project


def _lake_environment(*roots: Path) -> bytes:
    artifact_roots = os.pathsep.join(str(root / "artifacts") for root in roots)
    source_roots = os.pathsep.join(str(root / "sources") for root in roots)
    return f"LEAN_PATH={artifact_roots}\nLEAN_SRC_PATH={source_roots}\n".encode()


def _module(root: Path, name: str, *, source_time: int = 1, artifact_time: int = 2) -> None:
    relative = Path(*name.split("."))
    source = root / "sources" / relative.with_suffix(".lean")
    artifact = root / "artifacts" / relative.with_suffix(".olean")
    artifact_hash = artifact.with_suffix(".olean.hash")
    source.parent.mkdir(parents=True, exist_ok=True)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("theorem fixture : True := by trivial\n")
    artifact.write_bytes(b"olean")
    artifact_hash.write_bytes(b"hash")
    os.utime(source, ns=(source_time, source_time))
    os.utime(artifact, ns=(artifact_time, artifact_time))


def test_structured_import_validation_preserves_order_and_duplicates():
    assert validate_imports(["Fixture.B", "Fixture.A", "Fixture.B"]) == (
        "Fixture.B",
        "Fixture.A",
        "Fixture.B",
    )


@pytest.mark.parametrize(
    "value",
    ["Fixture", [""], [1], ["import Fixture"], ["Fixture/Outside"], ["Fixture..A"]],
)
def test_structured_import_validation_rejects_malformed_values(value):
    with pytest.raises(LeanImportError, match="imports"):
        validate_imports(value)


def test_resolver_rejects_malformed_module_before_running_lake(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    monkeypatch.setattr(
        repl_imports,
        "_run_lake",
        lambda *args, **kwargs: pytest.fail("malformed modules must not reach Lake"),
    )

    with pytest.raises(LeanImportError, match="imports"):
        resolve_project_imports(project, ("../Outside",), timeout=1)


def test_resolver_rejects_a_symlink_loop_project_root(tmp_path):
    loop = tmp_path / "project-loop"
    loop.symlink_to(loop, target_is_directory=True)

    with pytest.raises(LeanImportError, match="invalid Lean project root"):
        resolve_project_imports(loop, (), timeout=1)


def test_resolved_imports_cannot_be_constructed_without_resolution(tmp_path):
    with pytest.raises(TypeError):
        ResolvedImports()

    with pytest.raises(TypeError):
        ResolvedImports(
            project_root=tmp_path,
            modules=("Fixture",),
            project_fingerprint=lean_project_fingerprint(tmp_path),
        )

    assert not hasattr(ResolvedImports, "_create")


@pytest.mark.parametrize(
    ("code", "imports", "body", "line_count"),
    [
        ("import Mathlib\n#check Nat", ["Mathlib"], "#check Nat", 1),
        (
            "  /- lead -/ import /- gap -/ Mathlib -- tail\r\n#check Nat",
            ["Mathlib"],
            "#check Nat",
            1,
        ),
        (
            "-- lead\n/- outer\n /- nested -/\n-/\nimport Mathlib\n\n-- body\n#check Nat",
            ["Mathlib"],
            "\n-- body\n#check Nat",
            5,
        ),
        (
            "import Mathlib\n\nimport Aesop\n-- body\n#check Nat",
            ["Mathlib", "Aesop"],
            "-- body\n#check Nat",
            3,
        ),
    ],
)
def test_source_import_scanner_accepts_only_plain_physical_headers(
    code, imports, body, line_count
):
    assert split_imports_and_body(code) == (imports, body, line_count)


@pytest.mark.parametrize(
    "code",
    [
        "import\nMathlib",
        "import -- continued\nMathlib",
        "import /- continued\n-/ Mathlib",
        "module Fixture",
        "prelude",
        "public import Mathlib",
        "meta import Mathlib",
        "import all Mathlib",
        "import mathlib",
        "\timport Mathlib",
        "\N{NO-BREAK SPACE}import Mathlib",
        "import Mathlib Aesop",
        "import Mathlib #check Nat",
        "import Math/- gap -/lib",
        "import Mathlib\r#check Nat",
        "\rimport Mathlib\n#check Nat",
        "-- lead\rimport Mathlib\n#check Nat",
        "/- lead -/\rimport Mathlib\n#check Nat",
        "/- lead\r-/\nimport Mathlib\n#check Nat",
    ],
)
def test_source_import_scanner_rejects_unsupported_headers(code):
    with pytest.raises(LeanImportHeaderError, match="pass module names with imports"):
        split_imports_and_body(code)


@pytest.mark.parametrize(
    "code",
    [
        "/-- docs -/\nimport Mathlib",
        "/-! docs -/\nimport Mathlib",
        "#check Nat\nimport Mathlib",
        "im/- gap -/port Mathlib",
        "meta def fixture : Nat := 1",
        "public def fixture : Nat := 1",
        "\r#check Nat",
    ],
)
def test_source_import_scanner_preserves_non_header_source(code):
    assert split_imports_and_body(code) == ([], code, 0)


def test_source_import_scanner_preserves_unterminated_trailing_comment():
    code = "import Mathlib /- unterminated"
    assert split_imports_and_body(code) == (["Mathlib"], code, 0)


def test_source_import_scanner_preserves_bare_cr_after_the_final_import():
    code = "import Mathlib\n\r#check Nat"
    assert split_imports_and_body(code) == (["Mathlib"], "\r#check Nat", 1)


def test_clean_lake_environment_removes_ambient_path_overrides(monkeypatch):
    for name in (
        "ELAN_TOOLCHAIN",
        "LAKE_CONFIG",
        "LAKE_HOME",
        "LAKE_PKG_URL_MAP",
        "LEAN_PATH",
        "LEAN_SRC_PATH",
        "LEAN_SYSROOT",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "host-value")
    monkeypatch.setenv("HOME", "/safe-home")

    environment = clean_lake_environment()

    assert environment["HOME"] == "/safe-home"
    assert all(environment.get(name) is None for name in (
        "ELAN_TOOLCHAIN",
        "LAKE_HOME",
        "LEAN_PATH",
        "LEAN_SRC_PATH",
        "LEAN_SYSROOT",
        "PYTHONPATH",
    ))


def test_resolver_uses_non_building_lake_environment_and_preserves_order(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture.B")
    _module(root, "Fixture.A")
    calls = []

    def run(command, project_root, *, deadline):
        calls.append((command, project_root, deadline))
        stdout = _lake_environment(root) if command[-1] == "env" else b""
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    started = time.monotonic()
    resolved = resolve_project_imports(
        project,
        ("Fixture.B", "Fixture.A", "Fixture.B"),
        timeout=1,
    )

    assert resolved.project_root == project.resolve()
    assert resolved.modules == ("Fixture.B", "Fixture.A", "Fixture.B")
    assert resolved.project_fingerprint == lean_project_fingerprint(project.resolve())
    assert tuple(selection.module for selection in resolved._selections) == (
        "Fixture.B",
        "Fixture.A",
    )
    assert calls[0][0] == ["lake", "--no-build", "env"]
    assert calls[0][1] == project
    assert 0 < calls[0][2] - started <= 1.01
    assert calls[1][2] == calls[0][2]
    assert calls[1][0] == [
        "lake",
        "--rehash",
        "--no-build",
        "build",
        "+Fixture.B:olean",
        "+Fixture.A:olean",
    ]


def test_lake_runner_stops_oversized_output(tmp_path):
    with pytest.raises(LeanImportError, match="exceeded the size limit"):
        repl_imports._run_lake(
            [
                os.sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * (1024 * 1024 + 1))",
            ],
            tmp_path,
            timeout=5,
        )


def test_lake_runner_stops_on_timeout(tmp_path):
    with pytest.raises(TimeoutError, match="timed out"):
        repl_imports._run_lake(
            [os.sys.executable, "-c", "import time; time.sleep(10)"],
            tmp_path,
            timeout=0.1,
        )


def test_lake_runner_charges_environment_setup_to_its_timeout(tmp_path, monkeypatch):
    clock = {"now": 0.0}
    clean_environment = repl_imports.clean_lake_environment

    def delayed_environment():
        clock["now"] = 2.0
        return clean_environment()

    monkeypatch.setattr(repl_imports.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(repl_imports, "clean_lake_environment", delayed_environment)

    with pytest.raises(TimeoutError, match="timed out|no request time"):
        repl_imports._run_lake(
            [os.sys.executable, "-c", "import time; time.sleep(10)"],
            tmp_path,
            timeout=1.0,
        )


def test_lake_runner_kills_descendants_without_exceeding_deadline(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)']); "
        "open(sys.argv[1], 'w').write(str(child.pid))"
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out"):
        repl_imports._run_lake(
            [os.sys.executable, "-c", script, str(pid_file)],
            tmp_path,
            timeout=1.0,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("Lake descendant survived timed-out process-group cleanup")


def test_resolver_requires_an_existing_manifest_before_running_lake(tmp_path, monkeypatch):
    project = _project(tmp_path)
    (project / "lake-manifest.json").unlink()
    monkeypatch.setattr(
        repl_imports,
        "_run_lake",
        lambda *args, **kwargs: pytest.fail("Lake must not run without a manifest"),
    )

    with pytest.raises(LeanImportError, match="lake update.*lake build"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


@pytest.mark.parametrize(
    "failure",
    ["missing", "stale", "ambiguous", "escape", "loop"],
)
def test_resolver_rejects_untrusted_or_unusable_artifacts(tmp_path, monkeypatch, failure):
    project = _project(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    roots = [first]
    (first / "artifacts").mkdir(parents=True)
    (first / "sources").mkdir(parents=True)
    if failure != "missing":
        _module(first, "Fixture", source_time=3 if failure == "stale" else 1)
    if failure == "ambiguous":
        _module(second, "Fixture")
        roots.append(second)
    if failure == "escape":
        artifact = first / "artifacts" / "Fixture.olean"
        artifact.unlink()
        outside = tmp_path / "outside.olean"
        outside.write_bytes(b"outside")
        artifact.symlink_to(outside)
    if failure == "loop":
        artifact = first / "artifacts" / "Fixture.olean"
        artifact.unlink()
        artifact.symlink_to(artifact)
    def run(command, project_root, *, deadline):
        if command[-1] == "env":
            return subprocess.CompletedProcess(command, 0, _lake_environment(*roots), b"")
        returncode = 3 if failure == "stale" else 0
        return subprocess.CompletedProcess(command, returncode, b"", b"out of date")

    monkeypatch.setattr(repl_imports, "_run_lake", run)

    expected = {
        "missing": "not built",
        "stale": "stale",
        "ambiguous": "ambiguous",
        "escape": "escapes",
        "loop": "escapes",
    }[failure]
    with pytest.raises(LeanImportError, match=expected):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolver_rejects_manifest_mutation_during_lake_discovery(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")

    def run(command, project_root, *, deadline):
        (project / "lake-manifest.json").write_text("changed\n")
        return subprocess.CompletedProcess(command, 0, _lake_environment(root), b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(LeanImportError, match="changed during import discovery"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolver_rejects_oversized_manifest_before_running_lake(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    with (project / "lake-manifest.json").open("wb") as stream:
        stream.truncate(repl_imports._MAX_MANIFEST_BYTES + 1)
    monkeypatch.setattr(
        repl_imports,
        "_run_lake",
        lambda *args, **kwargs: pytest.fail("Lake must not read an oversized manifest"),
    )

    with pytest.raises(LeanImportError, match="manifest.*size limit"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


@pytest.mark.parametrize("mutation_stage", ["env", "build"])
def test_resolver_rejects_config_replacement_during_discovery(
    tmp_path, monkeypatch, mutation_stage
):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")
    config = project / "lakefile.toml"
    original_mtime = config.stat().st_mtime_ns

    def run(command, project_root, *, deadline):
        stage = "env" if command[-1] == "env" else "build"
        if stage == mutation_stage:
            replacement = project / "lakefile.replacement"
            replacement.write_text('name = "Changed"\n', encoding="utf-8")
            os.utime(replacement, ns=(original_mtime, original_mtime))
            replacement.replace(config)
        stdout = _lake_environment(root) if stage == "env" else b""
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(LeanImportError, match="project changed during import discovery"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolver_rejects_root_replacement_during_discovery(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")

    def run(command, project_root, *, deadline):
        moved = tmp_path / "original-project"
        project.rename(moved)
        replacement = _project(tmp_path)
        assert replacement == project
        return subprocess.CompletedProcess(command, 0, _lake_environment(root), b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(LeanImportError, match="project changed during import discovery"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolver_rejects_artifact_replacement_after_lake_check(
    tmp_path, monkeypatch
):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")
    artifact = root / "artifacts" / "Fixture.olean"
    outside = tmp_path / "outside.olean"
    outside.write_bytes(b"outside")

    def run(command, project_root, *, deadline):
        if command[-1] == "env":
            return subprocess.CompletedProcess(command, 0, _lake_environment(root), b"")
        artifact.unlink()
        artifact.symlink_to(outside)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(LeanImportError, match="escapes|changed"):
        resolve_project_imports(project, ("Fixture",), timeout=1)


def test_resolved_imports_reject_artifact_replacement(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")

    def run(command, project_root, *, deadline):
        stdout = _lake_environment(root) if command[-1] == "env" else b""
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    artifact = root / "artifacts" / "Fixture.olean"
    artifact.write_bytes(b"replaced")

    with pytest.raises(repl_imports.StaleResolvedImportsError, match="stale"):
        resolved.assert_current(time.monotonic() + 1)


def test_resolved_imports_reject_retargeted_lake_root_alias(tmp_path, monkeypatch):
    project = _project(tmp_path)
    first = tmp_path / "first-root"
    second = tmp_path / "second-root"
    _module(first, "Fixture")
    _module(second, "Fixture")
    alias = tmp_path / "lake-root"
    alias.symlink_to(first, target_is_directory=True)

    def run(command, project_root, *, deadline):
        stdout = _lake_environment(alias) if command[-1] == "env" else b""
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)

    with pytest.raises(
        repl_imports.StaleResolvedImportsError,
        match="Lake import root changed",
    ):
        resolved.assert_current(time.monotonic() + 1)


def test_resolved_imports_reject_new_earlier_lake_root(tmp_path, monkeypatch):
    project = _project(tmp_path)
    missing = tmp_path / "missing-root"
    existing = tmp_path / "existing-root"
    _module(existing, "Fixture")

    def run(command, project_root, *, deadline):
        stdout = (
            _lake_environment(missing, existing)
            if command[-1] == "env"
            else b""
        )
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run)
    resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    _module(missing, "Fixture")

    with pytest.raises(
        repl_imports.StaleResolvedImportsError,
        match="Lake import root changed",
    ):
        resolved.assert_current(time.monotonic() + 1)


def test_resolver_checks_deadline_after_final_lake_call(tmp_path, monkeypatch):
    project = _project(tmp_path)
    root = tmp_path / "root"
    _module(root, "Fixture")
    clock = {"now": 0.0}

    def run(command, project_root, *, deadline):
        if command[-1] == "env":
            return subprocess.CompletedProcess(command, 0, _lake_environment(root), b"")
        clock["now"] = 2.0
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(repl_imports.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(repl_imports, "_run_lake", run)
    with pytest.raises(TimeoutError, match="timed out"):
        resolve_project_imports(project, ("Fixture",), deadline=1.0)


def test_resolved_imports_reject_changed_project_config(tmp_path):
    project = _project(tmp_path).resolve()
    root = tmp_path / "root"
    _module(root, "Fixture")

    def run(command, project_root, *, deadline):
        stdout = _lake_environment(root) if command[-1] == "env" else b""
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(repl_imports, "_run_lake", run)
        resolved = resolve_project_imports(project, ("Fixture",), timeout=1)
    (project / "lakefile.toml").write_text('name = "Changed"\n', encoding="utf-8")

    with pytest.raises(repl_imports.StaleResolvedImportsError, match="stale"):
        resolved.assert_current(time.monotonic() + 1)


def _ready_repl(
    monkeypatch,
    project=None,
    *,
    max_contexts_per_process=256,
):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            cwd=str(project) if project is not None else ".",
            warmup_imports=frozenset(),
            validate_imports=False,
            max_retries=0,
            max_contexts_per_process=max_contexts_per_process,
        )
    )
    repl._process_generation = 1
    repl._base_environment = repl._make_environment_handle((), 11)
    repl._project_fingerprint = lean_project_fingerprint(repl._project_identity)
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    return repl


def _resolved(repl, tmp_path, monkeypatch, *modules: str) -> ResolvedImports:
    root = tmp_path / "resolved-imports"
    for module in dict.fromkeys(modules):
        relative = Path(*module.split("."))
        source = root / "sources" / relative.with_suffix(".lean")
        artifact = root / "artifacts" / relative.with_suffix(".olean")
        if not source.exists() or not artifact.exists():
            _module(root, module)

    def run(command, project_root, *, deadline):
        stdout = _lake_environment(root) if command[-1] == "env" else b""
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    with monkeypatch.context() as patch:
        patch.setattr(repl_imports, "_run_lake", run)
        return resolve_project_imports(
            repl._project_identity,
            tuple(modules),
            timeout=1,
        )


def test_structured_imports_reuse_an_immutable_request_base(tmp_path, monkeypatch):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    calls = []

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        if code.startswith("import "):
            return {"env": 22, "messages": []}
        return {"env": 31, "messages": []}

    monkeypatch.setattr(repl, "_run", run)
    imports = _resolved(repl, tmp_path, monkeypatch, "Fixture.B", "Fixture.A")
    assert repl.run(
        "def RequestLocal : Nat := 1",
        imports=imports,
        timeout=1,
    ) == {"env": 31, "messages": []}
    assert repl.run(
        "#check RequestLocal",
        imports=imports,
        timeout=1,
    ) == {"env": 31, "messages": []}
    assert calls == [
        ("import Fixture.B\nimport Fixture.A", None),
        ("def RequestLocal : Nat := 1", 22),
        ("#check RequestLocal", 22),
    ]
    assert repl._base_environment is not None
    assert repl._base_environment.env_id == 11

    calls.clear()
    assert repl.run("#check Nat", timeout=1) == {"env": 31, "messages": []}
    assert calls == [("#check Nat", 11)]


def test_independently_resolved_unchanged_imports_reuse_the_cached_context(
    tmp_path, monkeypatch
):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    first = _resolved(repl, tmp_path, monkeypatch, "Fixture")
    second = _resolved(repl, tmp_path, monkeypatch, "Fixture")
    calls = []

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        if code.startswith("import "):
            return {"env": 22, "messages": []}
        return {"env": 31, "messages": []}

    monkeypatch.setattr(repl, "_run", run)

    assert first is not second
    assert first == second
    repl.run("#check Fixture.fixture", imports=first, timeout=1)
    repl.run("#check Fixture.fixture", imports=second, timeout=1)

    assert calls == [
        ("import Fixture", None),
        ("#check Fixture.fixture", 22),
        ("#check Fixture.fixture", 22),
    ]


def test_malformed_cached_body_response_retires_worker_without_retry(
    tmp_path, monkeypatch
):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    imports = _resolved(repl, tmp_path, monkeypatch, "Fixture")
    repl._import_environments[imports.modules] = repl._make_environment_handle(
        imports.modules,
        22,
        resolved_imports=imports,
    )
    calls = []
    monkeypatch.setattr(
        repl,
        "_run",
        lambda code, env_id, timeout: calls.append((code, env_id)) or {},
    )

    result = repl.run("#check Fixture.fixture", imports=imports, timeout=1)

    assert "valid environment" in result["repl_error"]
    assert result["outcome_unknown"] is True
    assert calls == [("#check Fixture.fixture", 22)]
    assert repl._import_environments == {}


def test_cached_body_unknown_outcome_is_not_retried(tmp_path, monkeypatch):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    imports = _resolved(repl, tmp_path, monkeypatch, "Fixture")
    repl._import_environments[imports.modules] = repl._make_environment_handle(
        imports.modules,
        22,
        resolved_imports=imports,
    )
    calls = []

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        raise repl_core.ReplOutcomeUnknown("request outcome is unknown")

    monkeypatch.setattr(repl, "_run", run)

    result = repl.run("#check Fixture.fixture", imports=imports, timeout=1)

    assert result["outcome_unknown"] is True
    assert calls == [("#check Fixture.fixture", 22)]
    assert repl._import_environments == {}


def test_ordered_import_tuple_is_the_context_key(tmp_path, monkeypatch):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    calls = []
    next_env = iter((21, 22))

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        if code.startswith("import "):
            return {"env": next(next_env), "messages": []}
        return {"env": 31, "messages": []}

    monkeypatch.setattr(repl, "_run", run)
    forward = _resolved(repl, tmp_path, monkeypatch, "Fixture.A", "Fixture.B")
    reverse = _resolved(repl, tmp_path, monkeypatch, "Fixture.B", "Fixture.A")
    repl.run("#check Nat", imports=forward, timeout=1)
    repl.run("#check Nat", imports=reverse, timeout=1)
    repl.run("#check Nat", imports=forward, timeout=1)

    assert calls == [
        ("import Fixture.A\nimport Fixture.B", None),
        ("#check Nat", 21),
        ("import Fixture.B\nimport Fixture.A", None),
        ("#check Nat", 22),
        ("#check Nat", 21),
    ]


def test_private_environment_handles_are_worker_and_generation_scoped(monkeypatch):
    first = _ready_repl(monkeypatch)
    second = _ready_repl(monkeypatch)
    handle = first._make_environment_handle(("Fixture",), 22)

    with pytest.raises(repl_core.ReplProcessRestarted, match="another worker"):
        second._environment_id(handle, ("Fixture",))

    first._process_generation += 1
    with pytest.raises(repl_core.ReplProcessRestarted, match="process generation"):
        first._environment_id(handle, ("Fixture",))


def test_concurrent_identical_context_initialization_is_coalesced(
    tmp_path, monkeypatch
):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    imports = _resolved(repl, tmp_path, monkeypatch, "Fixture")
    import_started = threading.Event()
    release_import = threading.Event()
    calls = []
    results = []

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        if code.startswith("import "):
            import_started.set()
            assert release_import.wait(timeout=1)
            return {"env": 22, "messages": []}
        return {"env": 31, "messages": []}

    monkeypatch.setattr(repl, "_run", run)

    def invoke():
        results.append(repl.run("#check Nat", imports=imports, timeout=2))

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    assert import_started.wait(timeout=1)
    second.start()
    release_import.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert results == [
        {"env": 31, "messages": []},
        {"env": 31, "messages": []},
    ]
    assert calls.count(("import Fixture", None)) == 1
    assert calls.count(("#check Nat", 22)) == 2


def test_changed_import_artifacts_restart_before_cached_environment_reuse(
    tmp_path, monkeypatch
):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    first = _resolved(repl, tmp_path, monkeypatch, "Fixture")
    calls = []
    restarts = []
    imported_envs = iter((22, 42))

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        if code.startswith("import "):
            return {"env": next(imported_envs), "messages": []}
        return {"env": env_id + 1, "messages": []}

    def restart(timeout):
        restarts.append(timeout)
        repl._import_environments.clear()
        repl._contexts_created = 1
        repl._process_generation += 1
        repl._base_environment = repl._make_environment_handle((), 12)

    monkeypatch.setattr(repl, "_run", run)
    monkeypatch.setattr(repl, "restart", restart)

    assert repl.run("#check Fixture.fixture", imports=first, timeout=1)["env"] == 23

    root = tmp_path / "resolved-imports"
    _module(root, "Fixture", source_time=3, artifact_time=4)
    second = _resolved(repl, tmp_path, monkeypatch, "Fixture")
    assert first != second

    assert repl.run("#check Fixture.fixture", imports=second, timeout=1)["env"] == 43
    assert len(restarts) == 1
    assert calls == [
        ("import Fixture", None),
        ("#check Fixture.fixture", 22),
        ("import Fixture", None),
        ("#check Fixture.fixture", 42),
    ]


def test_context_limit_restarts_instead_of_evicting_one_cached_context(
    tmp_path, monkeypatch
):
    repl = _ready_repl(
        monkeypatch,
        _project(tmp_path),
        max_contexts_per_process=4,
    )
    repl._contexts_created = 2
    calls = []
    restarts = []
    next_env = iter((21, 22))

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        if code.startswith("import "):
            return {"env": next(next_env), "messages": []}
        return {"env": 30, "messages": []}

    def restart(timeout):
        restarts.append(timeout)
        repl._import_environments.clear()
        repl._contexts_created = 1
        repl._process_generation += 1
        repl._base_environment = repl._make_environment_handle((), 12)

    monkeypatch.setattr(repl, "_run", run)
    monkeypatch.setattr(repl, "restart", restart)

    first = _resolved(repl, tmp_path, monkeypatch, "Fixture.A")
    second = _resolved(repl, tmp_path, monkeypatch, "Fixture.B")
    repl.run("#check Nat", imports=first, timeout=1)
    assert restarts == []
    assert repl._contexts_created == 4

    repl.run("#check Nat", imports=second, timeout=1)
    assert len(restarts) == 1
    assert ("Fixture.A",) not in repl._import_environments
    assert repl._contexts_created == 3
    assert calls[-2:] == [
        ("import Fixture.B", None),
        ("#check Nat", 22),
    ]


def test_context_limit_restart_uses_the_original_request_deadline(monkeypatch):
    clock = {"now": 0.0}
    repl = _ready_repl(monkeypatch, max_contexts_per_process=3)
    repl._contexts_created = 3
    restarts = []
    body_timeouts = []
    monkeypatch.setattr(repl_core.time, "monotonic", lambda: clock["now"])

    def restart(timeout):
        restarts.append(timeout)
        clock["now"] = 0.6
        repl._process_generation += 1
        repl._contexts_created = 1
        repl._base_environment = repl._make_environment_handle((), 12)

    def run(code, env_id, timeout):
        body_timeouts.append(timeout)
        return {"env": 31, "messages": []}

    monkeypatch.setattr(repl, "restart", restart)
    monkeypatch.setattr(repl, "_run", run)

    assert repl.run("#check Nat", timeout=1) == {"env": 31, "messages": []}
    assert restarts == [1]
    assert body_timeouts == [pytest.approx(0.4)]


def test_context_limit_rejects_an_impossible_request_without_dispatch(
    tmp_path, monkeypatch
):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    repl.config.max_contexts_per_process = 1
    restarts = []

    def restart(timeout):
        restarts.append(timeout)
        repl._import_environments.clear()
        repl._contexts_created = 0
        repl._process_generation += 1
        repl._base_environment = None

    monkeypatch.setattr(repl, "restart", restart)
    monkeypatch.setattr(
        repl,
        "_run",
        lambda *args, **kwargs: pytest.fail("an impossible request must not execute"),
    )

    response = repl.run(
        "#check Nat",
        imports=_resolved(repl, tmp_path, monkeypatch, "Fixture"),
        timeout=1,
    )

    assert "context limit is too small" in response["repl_error"]
    assert len(restarts) == 1


def test_counted_run_counts_integer_environments_and_unique_proof_states(monkeypatch):
    repl = _ready_repl(monkeypatch)
    responses = iter(
        [
            {
                "env": 12,
                "proofState": 4,
                "messages": [],
                "sorries": [
                    {"proofState": 2},
                    {"proofState": 3},
                    {"proofState": 3},
                    {"proofState": None},
                ],
                "tactics": [{"proofState": 5}, {"proofState": 2}],
            },
            {"env": True, "messages": []},
            {"env": -1, "messages": []},
            {"env": "13", "messages": []},
            {"messages": []},
        ]
    )
    monkeypatch.setattr(repl, "_run", lambda code, env_id, timeout: next(responses))

    for _ in range(5):
        repl._run_counted("#check Nat", env_id=None, timeout=1)

    assert repl._contexts_created == 5


def test_response_over_context_limit_retires_worker_after_returning_diagnostics(
    monkeypatch,
):
    repl = _ready_repl(monkeypatch, max_contexts_per_process=3)
    repl._contexts_created = 1
    response = {
        "env": 31,
        "messages": [],
        "sorries": [
            {
                "goal": "False",
                "proofState": 2,
                "pos": {"line": 1, "column": 0},
            },
            {
                "goal": "True",
                "proofState": 3,
                "pos": {"line": 2, "column": 0},
            },
        ],
    }
    monkeypatch.setattr(repl, "_run", lambda code, env_id, timeout: response)

    result = repl.run("example : False := by sorry", timeout=1)

    assert result["messages"] == []
    assert len(result["sorries"]) == 2
    assert "env" not in result
    assert all("proofState" not in sorry for sorry in result["sorries"])
    assert repl._base_environment is None
    assert repl._contexts_created == 0


def test_environment_scoped_response_over_context_limit_reports_lost_state(
    monkeypatch,
):
    repl = _ready_repl(monkeypatch, max_contexts_per_process=3)
    repl._contexts_created = 1
    monkeypatch.setattr(
        repl,
        "_run",
        lambda code, env_id, timeout: {
            "env": 31,
            "messages": [],
            "sorries": [
                {
                    "goal": "False",
                    "proofState": 2,
                    "pos": {"line": 1, "column": 0},
                },
                {
                    "goal": "True",
                    "proofState": 3,
                    "pos": {"line": 2, "column": 0},
                },
            ],
        },
    )

    with pytest.raises(repl_core.ReplProcessRestarted, match="state was lost"):
        repl.run("example : False := by sorry", env_id=11, timeout=1)

    assert repl._base_environment is None
    assert repl._contexts_created == 0


def test_plain_capacity_restart_backlog_cannot_masquerade_as_body_response(
    monkeypatch,
):
    repl = _ready_repl(monkeypatch, max_contexts_per_process=3)
    repl._contexts_created = 3
    monkeypatch.setattr(
        repl,
        "restart",
        lambda timeout: (_ for _ in ()).throw(
            repl_core.ReplStderrBacklog(
                "startup stderr could not be drained",
                {"env": 12, "messages": []},
            )
        ),
    )
    monkeypatch.setattr(
        repl,
        "_run",
        lambda *args, **kwargs: pytest.fail("the request body must not execute"),
    )

    result = repl.run("#check DefinitelyMissing", timeout=1)

    assert "setup failed before the requested code ran" in result["repl_error"]
    assert "env" not in result
    assert "messages" not in result


def test_plain_capacity_restart_unknown_setup_is_not_the_body_outcome(monkeypatch):
    repl = _ready_repl(monkeypatch, max_contexts_per_process=3)
    repl._contexts_created = 3
    monkeypatch.setattr(
        repl,
        "restart",
        lambda timeout: (_ for _ in ()).throw(
            repl_core.ReplOutcomeUnknown("startup request outcome is unknown")
        ),
    )
    monkeypatch.setattr(
        repl,
        "_run",
        lambda *args, **kwargs: pytest.fail("the request body must not execute"),
    )

    result = repl.run("#check DefinitelyMissing", timeout=1)

    assert "setup failed before the requested code ran" in result["repl_error"]
    assert "outcome_unknown" not in result


def test_resolved_imports_reject_a_different_project_root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    for root in (project, other):
        (root / "lakefile.toml").write_text('name = "Fixture"\n', encoding="utf-8")
        (root / "lake-manifest.json").write_text(
            '{"version": "1.1.0", "packages": []}\n', encoding="utf-8"
        )
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            cwd=str(project),
            warmup_imports=frozenset(),
            validate_imports=False,
            max_retries=0,
        )
    )
    monkeypatch.setattr(
        repl,
        "_run",
        lambda *args, **kwargs: pytest.fail("mismatched imports must not execute"),
    )

    other_repl = _ready_repl(monkeypatch, other)
    descriptor = _resolved(other_repl, tmp_path, monkeypatch, "Fixture")
    response = repl.run(
        "#check Fixture.value",
        imports=descriptor,
        timeout=1,
    )

    assert "different Lean project root" in response["repl_error"]
    assert repl.process is None


def test_structured_import_failure_does_not_execute_the_body(tmp_path, monkeypatch):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    calls = []
    failure = {
        "env": 22,
        "messages": [
            {
                "severity": "error",
                "data": "unknown module",
                "pos": {"line": 1, "column": 0},
            }
        ],
    }

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        return failure

    monkeypatch.setattr(repl, "_run", run)
    assert repl.run(
        "#check Missing",
        imports=_resolved(repl, tmp_path, monkeypatch, "Fixture"),
        timeout=1,
    ) is failure
    assert calls == [("import Fixture", None)]


def test_structured_import_backlog_does_not_masquerade_as_body_response(
    tmp_path, monkeypatch
):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    calls = []

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        raise repl_core.ReplStderrBacklog(
            "stderr could not be drained",
            {"env": 22, "messages": []},
        )

    monkeypatch.setattr(repl, "_run", run)
    response = repl.run(
        "#check Fixture",
        imports=_resolved(repl, tmp_path, monkeypatch, "Fixture"),
        timeout=1,
    )

    assert "before the requested code ran" in response["repl_error"]
    assert "env" not in response
    assert "messages" not in response
    assert calls == [("import Fixture", None)]


def test_structured_imports_reject_an_explicit_environment(tmp_path, monkeypatch):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    monkeypatch.setattr(
        repl,
        "_run",
        lambda *args, **kwargs: pytest.fail("ambiguous request must not be dispatched"),
    )

    response = repl.run(
        "#check Fixture",
        env_id=11,
        imports=_resolved(repl, tmp_path, monkeypatch, "Fixture"),
        timeout=1,
    )

    assert "cannot be combined" in response["repl_error"]


@pytest.mark.parametrize(
    ("imported", "message"),
    [([], "malformed response"), ({"messages": {}}, "malformed diagnostics")],
)
def test_malformed_import_response_does_not_execute_the_body(
    tmp_path, monkeypatch, imported, message
):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    calls = []

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        return imported

    monkeypatch.setattr(repl, "_run", run)
    response = repl.run(
        "#check Fixture",
        imports=_resolved(repl, tmp_path, monkeypatch, "Fixture"),
        timeout=1,
    )

    assert message in response["repl_error"]
    assert calls == [("import Fixture", None)]


def test_omitted_imports_keep_the_legacy_source_header_path(monkeypatch):
    repl = _ready_repl(monkeypatch)
    calls = []

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        return {"env": 12, "messages": []}

    monkeypatch.setattr(repl, "_run", run)
    assert repl.run("import Mathlib\n#check Nat", timeout=1) == {
        "env": 12,
        "messages": [],
    }
    assert calls == [("#check Nat", 11)]


@pytest.mark.parametrize(
    "header",
    [
        "import Mathlib",
        "/- header -/ import Mathlib",
        "/- header -/\nimport Mathlib",
        "/- outer\n  /- nested -/\n-/\nimport Mathlib",
    ],
)
def test_structured_and_source_header_imports_are_rejected_before_execution(
    tmp_path, monkeypatch, header
):
    repl = _ready_repl(monkeypatch, _project(tmp_path))
    monkeypatch.setattr(
        repl,
        "_run",
        lambda *args, **kwargs: pytest.fail("ambiguous imports must not execute"),
    )

    response = repl.run(
        f"{header}\n#check Nat",
        imports=_resolved(repl, tmp_path, monkeypatch, "Fixture"),
        timeout=1,
    )

    assert "cannot be combined" in response["repl_error"]


@pytest.mark.real_lean
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_repl_imports_a_built_local_module(tmp_path):
    project = tmp_path / "fixture"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    (project / "lakefile.toml").write_text(
        '''name = "Fixture"
version = "0.1.0"
defaultTargets = ["Fixture"]

[[require]]
name = "repl"
git = "https://github.com/leanprover-community/repl.git"
rev = "68a3b3a059787a7db44fb1e6281e4a657efee470"

[[lean_lib]]
name = "Fixture"
srcDir = "src"
'''
    )
    source = project / "src" / "Fixture.lean"
    source.parent.mkdir()
    dependency = project / "src" / "Fixture" / "Dependency.lean"
    dependency.parent.mkdir()
    dependency.write_text(
        "namespace Fixture\n\ndef dependencyValue : Nat := 5\n\nend Fixture\n"
    )
    source.write_text(
        "import Fixture.Dependency\n\n"
        "namespace Fixture\n\ndef localValue : Nat := dependencyValue + 32\n\nend Fixture\n"
    )
    setup = subprocess.run(
        ["lake", "update"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert setup.returncode == 0, setup.stdout + setup.stderr
    built = subprocess.run(
        ["lake", "build", "Fixture", "repl"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    dependency_timestamp = dependency.stat().st_mtime_ns
    dependency.write_text(
        "namespace Fixture\n\ndef dependencyValue : Nat := 6\n\nend Fixture\n"
    )
    os.utime(dependency, ns=(dependency_timestamp, dependency_timestamp))
    with pytest.raises(LeanImportError, match="dependencies are stale"):
        resolve_project_imports(project, ("Fixture",), timeout=30)
    rebuilt = subprocess.run(
        ["lake", "build", "Fixture"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr

    located = subprocess.run(
        ["lake", "env", "which", "repl"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert located.returncode == 0, located.stdout + located.stderr
    repl_binary = Path(located.stdout.strip())
    assert repl_binary.is_absolute() and repl_binary.is_file()

    def make_pool(target: Path) -> LeanReplPool:
        return LeanReplPool(
            LeanReplPoolConfig(
                cwd=str(target),
                repl_command=["lake", "env", str(repl_binary)],
                num_repls=1,
                startup_stagger=0,
                warmup_imports=frozenset(),
                validate_imports=False,
                max_retries=0,
            )
        )

    imports = resolve_project_imports(project, ("Fixture",), timeout=60)
    pool = make_pool(project)
    worker = pool._workers[0]
    original_run = worker._run
    imported_headers = 0

    def count_imports(code, env_id, timeout):
        nonlocal imported_headers
        if code == "import Fixture":
            imported_headers += 1
        return original_run(code, env_id, timeout)

    worker._run = count_imports
    try:
        first = pool.run(
            "#check Fixture.localValue\ndef RequestOnly : Nat := Fixture.localValue",
            imports=imports,
            timeout=120,
        )
        assert first["messages"][0]["data"] == "Fixture.localValue : Nat"

        second = pool.run(
            "#check Fixture.localValue\n#check RequestOnly",
            imports=imports,
            timeout=120,
        )
        assert second["messages"][0]["data"] == "Fixture.localValue : Nat"
        assert second["messages"][1]["severity"] == "error"
        assert "Unknown identifier `RequestOnly`" in second["messages"][1]["data"]
        assert imported_headers == 1

        generation = worker._process_generation
        dependency.write_text(
            "namespace Fixture\n\ndef dependencyValue : Nat := 7\n\nend Fixture\n"
        )
        rebuilt_again = subprocess.run(
            ["lake", "build", "Fixture"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert rebuilt_again.returncode == 0, rebuilt_again.stdout + rebuilt_again.stderr
        refreshed_imports = resolve_project_imports(
            project,
            ("Fixture",),
            timeout=60,
        )
        assert refreshed_imports != imports

        refreshed = pool.run(
            "#eval Fixture.localValue",
            imports=refreshed_imports,
            timeout=120,
        )
        assert any(message["data"] == "39" for message in refreshed["messages"])
        assert worker._process_generation == generation + 1
        assert imported_headers == 2
    finally:
        pool.shutdown()

    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    (sibling / "lakefile.toml").write_text(
        '''name = "Sibling"
version = "0.1.0"
defaultTargets = ["Fixture"]

[[lean_lib]]
name = "Fixture"
srcDir = "src"
'''
    )
    sibling_source = sibling / "src" / "Fixture.lean"
    sibling_source.parent.mkdir()
    sibling_source.write_text(
        "namespace Fixture\n\ndef siblingValue : Nat := 41\n\nend Fixture\n"
    )
    for command in (["lake", "update"], ["lake", "build", "Fixture"]):
        result = subprocess.run(
            command,
            cwd=sibling,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    sibling_imports = resolve_project_imports(sibling, ("Fixture",), timeout=60)
    sibling_pool = make_pool(sibling)
    try:
        sibling_response = sibling_pool.run(
            "#check Fixture.siblingValue\n#check Fixture.localValue",
            imports=sibling_imports,
            timeout=120,
        )
    finally:
        sibling_pool.shutdown()
    messages = sibling_response["messages"]
    assert messages[0]["data"] == "Fixture.siblingValue : Nat"
    assert messages[1]["severity"] == "error"
    assert "Unknown identifier `Fixture.localValue`" in messages[1]["data"]
