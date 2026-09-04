"""Focused contracts salvaged from the retired standalone REPL implementation."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from contextlib import ExitStack

import pytest

from servers.repl import core as repl_core
from servers.repl import imports as repl_imports


def _diagnostic(severity: str = "info") -> dict:
    return {
        "severity": severity,
        "data": "diagnostic",
        "pos": {"line": 1, "column": 0},
        "endPos": {"line": 1, "column": 1},
    }


def _mark_ready(repl):
    repl._project_fingerprint = repl_imports.lean_project_fingerprint(
        repl._project_identity
    )


def _install_fake_process(repl, process):
    repl.process = process
    _mark_ready(repl)


def _ready_repl_with_imports(tmp_path, monkeypatch):
    project = tmp_path.resolve()
    (project / "lakefile.toml").write_text('name = "Fixture"\n', encoding="utf-8")
    (project / "lake-manifest.json").write_text(
        '{"version": "1.1.0", "packages": []}\n',
        encoding="utf-8",
    )
    discovery_root = project / "discovery"
    source = discovery_root / "sources" / "Fixture.lean"
    artifact = discovery_root / "artifacts" / "Fixture.olean"
    source.parent.mkdir(parents=True)
    artifact.parent.mkdir(parents=True)
    source.write_text("theorem fixture : True := by trivial\n", encoding="utf-8")
    artifact.write_bytes(b"olean")

    def run_lake(command, project_root, *, deadline):
        stdout = b""
        if command[-1] == "env":
            stdout = (
                f"LEAN_PATH={artifact.parent}\nLEAN_SRC_PATH={source.parent}\n"
            ).encode()
        return subprocess.CompletedProcess(command, 0, stdout, b"")

    monkeypatch.setattr(repl_imports, "_run_lake", run_lake)
    descriptor = repl_imports.resolve_project_imports(
        project,
        ("Fixture",),
        timeout=1,
    )
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            cwd=str(project),
            warmup_imports=frozenset(),
            validate_imports=False,
            max_retries=0,
        )
    )
    repl._base_env_id = 11
    repl._project_fingerprint = repl_imports.lean_project_fingerprint(project)
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    return repl, descriptor


def test_split_imports_preserves_body_offset_after_comments_and_blank_lines():
    code = """-- preface
import Mathlib.Data.Nat.Basic

-- body comment
#check Nat
"""

    imports, body, offset = repl_core._split_imports_and_body(code)

    assert imports == ["Mathlib.Data.Nat.Basic"]
    assert body == "\n-- body comment\n#check Nat\n"
    assert offset == 2


def test_split_imports_stops_at_the_first_body_statement():
    imports, body, offset = repl_core._split_imports_and_body(
        "import Mathlib\n#check Nat\nimport Aesop\n"
    )

    assert imports == ["Mathlib"]
    assert body == "#check Nat\nimport Aesop\n"
    assert offset == 1


def test_split_imports_preserves_an_unterminated_block_comment_for_lean():
    code = "import Mathlib /- unterminated\n#check Missing"

    imports, body, offset = repl_core._split_imports_and_body(code)

    assert imports == ["Mathlib"]
    assert body == code
    assert offset == 0


def test_run_rejects_disallowed_import_roots_before_touching_the_process():
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            allowed_imports=frozenset({"Mathlib"}),
            warmup_imports=frozenset(),
        )
    )

    response = repl.run("import Unsafe.Module\n#check Nat")

    assert response == {
        "repl_error": "Disallowed imports: Unsafe. Allowed roots: Mathlib."
    }
    assert repl.process is None


@pytest.mark.parametrize("raw_imports", [("Fixture",), ["Fixture"]])
def test_run_rejects_unresolved_structured_imports(raw_imports, monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )
    monkeypatch.setattr(
        repl,
        "_run",
        lambda *args, **kwargs: pytest.fail("unresolved imports must not execute"),
    )

    with pytest.raises(TypeError, match="ResolvedImports descriptor"):
        repl.run("#check Fixture", imports=raw_imports, timeout=1)


def test_start_without_warmups_retains_an_init_environment_for_scanner_misses(
    monkeypatch,
):
    class Process:
        def poll(self):
            return None

    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )
    process = Process()
    calls = []

    monkeypatch.setattr(repl_core.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        return {"env": len(calls)}

    monkeypatch.setattr(repl, "_run", run)

    repl.start()
    scanner_miss = "/-! module documentation -/\nimport Mathlib\n#check Nat"
    assert repl.run(scanner_miss, timeout=1) == {"env": 2}
    assert calls == [
        ("#check Nat", None),
        (scanner_miss, 1),
    ]


def test_absolute_deadline_error_does_not_report_the_default_timeout(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            request_timeout=30,
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )
    monkeypatch.setattr(repl_core.time, "monotonic", lambda: 10.0)

    assert repl.run("#check Nat", deadline=9.0) == {
        "repl_error": "REPL command deadline exceeded"
    }


def test_resolved_imports_are_rechecked_immediately_before_dispatch(
    tmp_path,
    monkeypatch,
):
    repl, descriptor = _ready_repl_with_imports(tmp_path, monkeypatch)
    checks = 0

    def assert_current(self, deadline):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise repl_imports.StaleResolvedImportsError("descriptor changed")

    monkeypatch.setattr(repl_imports.ResolvedImports, "assert_current", assert_current)
    monkeypatch.setattr(
        repl,
        "_run",
        lambda *args, **kwargs: pytest.fail("stale imports must not be dispatched"),
    )

    with pytest.raises(repl_imports.StaleResolvedImportsError, match="changed"):
        repl.run("#check Fixture", imports=descriptor, timeout=1)

    assert checks == 2


def test_stale_resolved_imports_retire_the_worker(tmp_path, monkeypatch):
    repl, descriptor = _ready_repl_with_imports(tmp_path, monkeypatch)
    retired = []
    (tmp_path / "lakefile.toml").write_text('name = "Changed"\n', encoding="utf-8")
    monkeypatch.setattr(repl, "close", lambda: retired.append(True))
    monkeypatch.setattr(
        repl,
        "_run",
        lambda *args, **kwargs: pytest.fail("stale imports must not execute"),
    )

    with pytest.raises(repl_imports.StaleResolvedImportsError, match="stale"):
        repl.run("#check Fixture", imports=descriptor, timeout=1)

    assert retired == [True]


def test_closed_worker_restarts_for_current_resolved_imports(tmp_path, monkeypatch):
    repl, descriptor = _ready_repl_with_imports(tmp_path, monkeypatch)
    repl.process = None
    repl._base_env_id = None
    repl._project_fingerprint = None
    calls = []
    monkeypatch.setattr(repl, "is_alive", lambda: repl.process is not None)

    def restart(timeout=None):
        repl.process = object()
        repl._base_env_id = 11
        _mark_ready(repl)

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        if code.startswith("import "):
            return {"env": 22}
        return {"env": 23}

    monkeypatch.setattr(repl, "restart", restart)
    monkeypatch.setattr(repl, "_run", run)

    assert repl.run("#check Fixture", imports=descriptor, timeout=1) == {"env": 23}
    assert calls == [("import Fixture", None), ("#check Fixture", 22)]


@pytest.mark.parametrize(
    ("imported", "message"),
    [
        ([], "malformed response"),
        ({"env": 1, "messages": {}}, "malformed diagnostics"),
        ({"env": 1, "messages": ["error"]}, "malformed diagnostics"),
        (
            {"env": 1, "messages": [{**_diagnostic(), "severity": "fatal"}]},
            "malformed diagnostics",
        ),
        (
            {"env": 1, "messages": [{**_diagnostic(), "severity": []}]},
            "malformed diagnostics",
        ),
        (
            {"env": 1, "messages": [{**_diagnostic(), "data": None}]},
            "malformed diagnostics",
        ),
        (
            {
                "env": 1,
                "messages": [
                    {**_diagnostic(), "pos": {"line": True, "column": 0}}
                ],
            },
            "malformed diagnostics",
        ),
        ({"env": 1, "sorries": {}}, "malformed sorries"),
        (
            {
                "env": 1,
                "sorries": [{"goal": "False", "proofState": 0, "pos": 1}],
            },
            "malformed sorries",
        ),
        ({"env": 1, "sorries": [{"goal": "False"}]}, "malformed sorries"),
        ({"env": True}, "valid environment"),
        ({"env": -1}, "valid environment"),
    ],
)
def test_malformed_import_responses_retire_the_worker(
    tmp_path,
    monkeypatch,
    imported,
    message,
):
    repl, descriptor = _ready_repl_with_imports(tmp_path, monkeypatch)
    calls = []
    retired = []

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        return imported

    def close():
        retired.append(True)
        repl.process = None
        repl._base_env_id = None

    monkeypatch.setattr(repl, "_run", run)
    monkeypatch.setattr(repl, "close", close)

    response = repl.run("#check Fixture", imports=descriptor, timeout=1)

    assert message in response["repl_error"]
    assert calls == [("import Fixture", None)]
    assert retired == [True]


def test_import_response_accepts_zero_environment_and_trace_diagnostic(
    tmp_path,
    monkeypatch,
):
    repl, descriptor = _ready_repl_with_imports(tmp_path, monkeypatch)
    calls = []

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        if code.startswith("import "):
            return {"env": 0, "messages": [_diagnostic("trace")]}
        return {"env": 1, "messages": []}

    monkeypatch.setattr(repl, "_run", run)

    assert repl.run("#check Fixture", imports=descriptor, timeout=1) == {
        "env": 1,
        "messages": []
    }
    assert calls == [
        ("import Fixture", None),
        ("#check Fixture", 0),
    ]


@pytest.mark.parametrize(
    "sorry",
    [
        {
            "goal": "False",
            "proofState": 0,
            "pos": {"line": 1, "column": 0},
            "endPos": {"line": 1, "column": 5},
        },
        {"goal": "False", "proofState": None},
    ],
)
def test_command_response_accepts_the_pinned_sorry_schema(sorry):
    response = {"messages": [], "sorries": [sorry]}

    assert repl_core._validate_command_response(
        response,
        context="a test command",
        require_environment=False,
    ) == (None, [])


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"messages": {}},
        {"messages": ["not-an-object"]},
        {"messages": [{**_diagnostic(), "severity": "fatal"}]},
        {"messages": [{**_diagnostic(), "data": None}]},
        {"messages": [{**_diagnostic(), "endPos": {"line": -1, "column": 0}}]},
        {"sorries": [1]},
        {"sorries": [{"goal": None, "proofState": 0}]},
        {"sorries": [{"goal": "False", "proofState": True}]},
        {"env": True, "messages": []},
        {"message": 1},
    ],
)
def test_malformed_body_response_is_not_retried(monkeypatch, response):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
            max_retries=2,
        )
    )
    calls = []
    retired = []
    _mark_ready(repl)
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(
        repl,
        "_check_memory_and_maybe_restart",
        lambda timeout: None,
    )
    monkeypatch.setattr(
        repl,
        "_run",
        lambda code, env_id, timeout: calls.append((code, env_id)) or response,
    )
    monkeypatch.setattr(repl, "close", lambda: retired.append(True))

    result = repl.run("#eval 1", timeout=1)

    assert any(
        fragment in result["repl_error"]
        for fragment in ("malformed", "invalid", "valid environment")
    )
    assert result["outcome_unknown"] is True
    assert calls == [("#eval 1", None)]
    assert retired == [True]


def test_pinned_repl_error_response_is_not_reported_as_success(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
            max_retries=2,
        )
    )
    _mark_ready(repl)
    calls = []
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    monkeypatch.setattr(
        repl,
        "_run",
        lambda code, env_id, timeout: calls.append((code, env_id))
        or {"message": "Unknown environment."},
    )

    assert repl.run("#eval 1", timeout=1) == {
        "repl_error": "Unknown environment."
    }
    assert calls == [("#eval 1", None)]


@pytest.mark.parametrize(
    ("raw_response", "message"),
    [
        ({"env": 0, "messages": [], "sorries": [1]}, "malformed sorries"),
        ([["env", 0]], "malformed response"),
    ],
)
def test_malformed_backlog_response_is_reported_as_unknown(
    monkeypatch, raw_response, message
):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
            max_retries=2,
        )
    )
    _mark_ready(repl)
    retired = []
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)

    def backlog(*args, **kwargs):
        raise repl_core.ReplStderrBacklog(
            "stderr backlog",
            raw_response,
        )

    monkeypatch.setattr(repl, "_run", backlog)
    monkeypatch.setattr(repl, "close", lambda: retired.append(True))

    response = repl.run("#eval 1", timeout=1)

    assert response["outcome_unknown"] is True
    assert message in response["repl_error"]
    assert retired == [True]


def test_stale_worker_project_is_rejected_before_body_dispatch(tmp_path, monkeypatch):
    (tmp_path / "lakefile.toml").write_text('name = "Fixture"\n', encoding="utf-8")
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            cwd=str(tmp_path),
            warmup_imports=frozenset(),
            validate_imports=False,
            max_retries=0,
        )
    )
    _mark_ready(repl)
    (tmp_path / "lakefile.toml").write_text('name = "Changed"\n', encoding="utf-8")
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    monkeypatch.setattr(
        repl,
        "_run",
        lambda *args, **kwargs: pytest.fail("a stale worker must not execute code"),
    )

    response = repl.run("#eval 1", timeout=1)

    assert "project changed" in response["repl_error"]
    assert repl._project_fingerprint is None


def test_project_change_during_plain_body_is_not_retried(tmp_path, monkeypatch):
    config = tmp_path / "lakefile.toml"
    config.write_text('name = "Fixture"\n', encoding="utf-8")
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            cwd=str(tmp_path),
            warmup_imports=frozenset(),
            validate_imports=False,
            max_retries=2,
        )
    )
    _mark_ready(repl)
    calls = []
    retired = []
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        config.write_text('name = "Changed"\n', encoding="utf-8")
        return {"env": 12, "messages": []}

    def close():
        if repl._project_fingerprint is not None:
            retired.append(True)
        repl.process = None
        repl._base_env_id = None
        repl._project_fingerprint = None

    monkeypatch.setattr(repl, "_run", run)
    monkeypatch.setattr(repl, "close", close)

    response = repl.run("#eval 1", timeout=1)

    assert response["outcome_unknown"] is True
    assert "freshness changed" in response["repl_error"]
    assert calls == [("#eval 1", None)]
    assert retired == [True]


def test_project_change_during_structured_body_is_not_retried(
    tmp_path, monkeypatch
):
    repl, descriptor = _ready_repl_with_imports(tmp_path, monkeypatch)
    config = tmp_path / "lakefile.toml"
    calls = []
    retired = []

    def run(code, env_id, timeout):
        calls.append((code, env_id))
        if code.startswith("import "):
            return {"env": 22, "messages": []}
        config.write_text('name = "Changed"\n', encoding="utf-8")
        return {"env": 23, "messages": []}

    def close():
        if repl._project_fingerprint is not None:
            retired.append(True)
        repl.process = None
        repl._base_env_id = None
        repl._project_fingerprint = None

    monkeypatch.setattr(repl, "_run", run)
    monkeypatch.setattr(repl, "close", close)

    response = repl.run("#eval 1", imports=descriptor, timeout=1)

    assert response["outcome_unknown"] is True
    assert "freshness changed" in response["repl_error"]
    assert calls == [("import Fixture", None), ("#eval 1", 22)]
    assert retired == [True]


def test_run_offsets_diagnostics_after_stripping_import_header(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )
    _mark_ready(repl)
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    monkeypatch.setattr(
        repl,
        "_run",
        lambda code, env_id, timeout: {
            "env": 12,
            "messages": [
                {
                    "severity": "error",
                    "data": "boom",
                    "pos": {"line": 2, "column": 2},
                    "endPos": {"line": 2, "column": 3},
                }
            ],
            "sorries": [
                {
                    "goal": "False",
                    "proofState": None,
                    "pos": {"line": 3, "column": 1},
                    "endPos": {"line": 3, "column": 2},
                }
            ],
        },
    )

    response = repl.run("import Mathlib\n\n#check Missing", timeout=1)

    assert response["messages"][0]["pos"]["line"] == 3
    assert response["messages"][0]["endPos"]["line"] == 3
    assert response["sorries"][0]["pos"]["line"] == 4
    assert response["sorries"][0]["endPos"]["line"] == 4


def test_run_resolves_the_base_environment_after_restart(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(validate_imports=False, warmup_imports=frozenset())
    )
    dispatched_envs = []

    monkeypatch.setattr(repl, "is_alive", lambda: repl.process is not None)

    def restart(timeout=None):
        repl.process = object()
        repl._base_env_id = 73
        _mark_ready(repl)

    monkeypatch.setattr(repl, "restart", restart)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    monkeypatch.setattr(
        repl,
        "_run",
        lambda code, env_id, timeout: dispatched_envs.append(env_id) or {"env": 74},
    )

    assert repl.run("#check Nat", timeout=1) == {"env": 74}
    assert dispatched_envs == [73]


def test_explicit_environment_is_not_sent_after_an_entry_restart(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(validate_imports=False, warmup_imports=frozenset())
    )
    monkeypatch.setattr(
        repl,
        "restart",
        lambda timeout=None: pytest.fail("a stale explicit environment must not restart"),
    )
    monkeypatch.setattr(
        repl,
        "_run",
        lambda code, env_id, timeout: pytest.fail("a stale environment must not be sent"),
    )

    with pytest.raises(repl_core.ReplProcessRestarted, match="environment state was lost"):
        repl.run("#check Nat", env_id=7, timeout=1)


def test_run_refreshes_the_base_environment_after_retry(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            max_retries=1,
            validate_imports=False,
            warmup_imports=frozenset(),
        )
    )
    repl.process = object()
    repl._base_env_id = 11
    _mark_ready(repl)
    dispatched_envs = []

    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    monkeypatch.setattr(repl_core.time, "sleep", lambda delay: None)
    monkeypatch.setattr(repl_core.random, "uniform", lambda low, high: 0)

    def run_once(code, env_id, timeout):
        dispatched_envs.append(env_id)
        if len(dispatched_envs) == 1:
            raise RuntimeError("retry me")
        return {"env": 23}

    def restart(timeout=None):
        repl.process = object()
        repl._base_env_id = 22
        _mark_ready(repl)

    monkeypatch.setattr(repl, "_run", run_once)
    monkeypatch.setattr(repl, "restart", restart)

    assert repl.run("#check Nat", timeout=5) == {"env": 23}
    assert dispatched_envs == [11, 22]


def test_explicit_environment_is_not_sent_after_a_memory_restart(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(validate_imports=False, warmup_imports=frozenset())
    )
    repl.process = object()
    _mark_ready(repl)

    monkeypatch.setattr(repl, "is_alive", lambda: True)

    def restart_during_memory_check(timeout):
        repl.process = object()
        repl._base_env_id = 22

    monkeypatch.setattr(
        repl,
        "_check_memory_and_maybe_restart",
        restart_during_memory_check,
    )
    monkeypatch.setattr(
        repl,
        "_run",
        lambda code, env_id, timeout: pytest.fail("a stale environment must not be sent"),
    )

    with pytest.raises(repl_core.ReplProcessRestarted, match="environment state was lost"):
        repl.run("#check Nat", env_id=7, timeout=1)


def test_format_repl_response_prioritizes_errors_and_keeps_sorries():
    formatted = repl_core.format_repl_response(
        {
            "messages": [
                {"severity": "warning", "data": "unused"},
                {
                    "severity": "error",
                    "data": "unknown identifier",
                    "pos": {"line": 3, "column": 5},
                },
                "malformed message",
            ],
            "sorries": [{"goal": "Nat = Nat", "pos": {"line": 7}}],
        }
    )

    assert formatted == (
        "Compilation Errors (1)\n"
        "  - 3:5: error: unknown identifier\n"
        "\nSorries (1)\n"
        "  - Line 7: Nat = Nat"
    )
    assert "unused" not in formatted


def test_format_repl_response_truncates_diagnostics():
    formatted = repl_core.format_repl_response(
        {
            "messages": [
                {"severity": "warning", "data": f"warning {index}"}
                for index in range(repl_core.DEFAULT_MAX_DIAGNOSTICS + 2)
            ]
        }
    )

    assert "Warnings (12)" in formatted
    assert "warning 9" in formatted
    assert "warning 10" not in formatted
    assert "... and 2 more" in formatted


def test_format_repl_response_reports_explicit_repl_error():
    assert (
        repl_core.format_repl_response({"repl_error": "worker unavailable"})
        == "REPL error: worker unavailable"
    )


def test_format_repl_response_preserves_unknown_outcome_warning():
    assert repl_core.format_repl_response(
        {"repl_error": "response was malformed", "outcome_unknown": True}
    ) == (
        "REPL error (execution outcome unknown; request not retried): "
        "response was malformed"
    )


class _PipeProcess:
    def __init__(self, stack: ExitStack, stdout_chunks: list[bytes], stderr: bytes = b""):
        stdin_read, stdin_write = os.pipe()
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        self.stdin = stack.enter_context(os.fdopen(stdin_write, "wb", buffering=0))
        self.stdout = stack.enter_context(os.fdopen(stdout_read, "rb", buffering=0))
        self.stderr = stack.enter_context(os.fdopen(stderr_read, "rb", buffering=0))
        self._stdin_read = stack.enter_context(os.fdopen(stdin_read, "rb", buffering=0))
        self._stdout_write = stack.enter_context(os.fdopen(stdout_write, "wb", buffering=0))
        self._stderr_write = stack.enter_context(os.fdopen(stderr_write, "wb", buffering=0))
        self.stdout_chunks = list(stdout_chunks)
        self.stderr_bytes = stderr

    def poll(self):
        return None


def _repl_with_process(process: _PipeProcess, *, chunk_size: int = 4096, max_buffer_bytes: int = 1024):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            chunk_size=chunk_size,
            max_buffer_bytes=max_buffer_bytes,
            validate_imports=False,
            warmup_imports=frozenset(),
        )
    )
    repl.process = process
    _mark_ready(repl)
    return repl


def _patch_pipe_reads(monkeypatch, process: _PipeProcess):
    real_read = os.read

    def take_chunk(chunks: list[bytes], size: int) -> bytes:
        if not chunks:
            return b""
        result = chunks[0][:size]
        chunks[0] = chunks[0][size:]
        if not chunks[0]:
            chunks.pop(0)
        return result

    def fake_read(fd: int, size: int) -> bytes:
        if fd == process.stdout.fileno():
            return take_chunk(process.stdout_chunks, size)
        if fd == process.stderr.fileno():
            if not process.stderr_bytes:
                raise BlockingIOError
            result = process.stderr_bytes[:size]
            process.stderr_bytes = process.stderr_bytes[size:]
            return result
        return real_read(fd, size)

    def fake_select(readable, writable, exceptional, timeout=None):
        if writable:
            return [], writable, []
        ready = []
        if process.stderr_bytes:
            ready.append(process.stderr.fileno())
        if process.stdout_chunks:
            ready.append(process.stdout.fileno())
        return ready, [], []

    monkeypatch.setattr(repl_core.os, "read", fake_read)
    monkeypatch.setattr(repl_core.select, "select", fake_select)


def test_response_timeout_after_full_write_is_not_retried(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [])
        repl = _repl_with_process(process)
        _patch_pipe_reads(monkeypatch, process)
        monkeypatch.setattr(
            repl,
            "restart",
            lambda timeout=None: pytest.fail("a sent request must not be retried"),
        )

        response = repl.run("#eval 1", timeout=1)
        request = process._stdin_read.read(4096)

    assert response["outcome_unknown"] is True
    assert "fully sent" in response["repl_error"]
    assert request.count(b"\n\n") == 1
    assert repl.process is None


def test_wire_protocol_accepts_response_split_across_reads(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages":', b" []}\n", b"\n"])
        repl = _repl_with_process(process, chunk_size=8)
        _patch_pipe_reads(monkeypatch, process)

        assert repl._run("#check Nat", env_id=3, timeout=1) == {"messages": []}

        request = process._stdin_read.read(4096)
        assert json.loads(request.decode().strip()) == {"cmd": "#check Nat", "env": 3}


def test_wire_protocol_preserves_utf8_split_across_reads(monkeypatch):
    response = json.dumps({"messages": [{"data": "Nat → Nat"}]}, ensure_ascii=False).encode()
    arrow = "→".encode()
    split = response.index(arrow) + 1
    with ExitStack() as stack:
        process = _PipeProcess(
            stack,
            [response[:split], response[split:] + b"\n\n"],
        )
        repl = _repl_with_process(process, chunk_size=7)
        _patch_pipe_reads(monkeypatch, process)

        result = repl._run("#check Nat", env_id=None, timeout=1)

    assert result["messages"][0]["data"] == "Nat → Nat"


def test_wire_protocol_reports_complete_stderr_on_premature_eof(monkeypatch):
    stderr = (b"x" * 5000) + b"lean crashed"
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b""], stderr=stderr)
        repl = _repl_with_process(process, max_buffer_bytes=len(stderr))
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplOutcomeUnknown) as error:
            repl._run("#check Nat", env_id=None, timeout=1)

    assert str(error.value).endswith(stderr.decode())


def test_wire_protocol_services_stdout_while_stderr_remains_readable(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"x")
        repl = _repl_with_process(process, chunk_size=8)
        _patch_pipe_reads(monkeypatch, process)

        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            if fd == process.stderr.fileno():
                return b"x" * size
            return real_read(fd, size)

        monkeypatch.setattr(repl_core.os, "read", fake_read)

        # Endlessly readable stderr never starves stdout, so the response is
        # captured. It cannot be drained to a boundary though, so the process is
        # reported as unusable rather than silently reused.
        with pytest.raises(repl_core.ReplStderrBacklog) as error:
            repl._run("#check Nat", env_id=None, timeout=1)

    assert error.value.response == {"messages": []}


def test_wire_protocol_times_out_while_stderr_remains_readable(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [], stderr=b"x")
        repl = _repl_with_process(process)
        _patch_pipe_reads(monkeypatch, process)

        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            if fd == process.stderr.fileno():
                return b"x"
            return real_read(fd, size)

        now = 0.0

        def fake_monotonic() -> float:
            nonlocal now
            now += 0.25
            return now

        monkeypatch.setattr(repl_core.os, "read", fake_read)
        monkeypatch.setattr(repl_core.time, "monotonic", fake_monotonic)

        with pytest.raises(repl_core.ReplOutcomeUnknown, match="timed out"):
            repl._run("#check Nat", env_id=None, timeout=1)


def test_wire_protocol_retires_a_process_generation_that_exceeds_its_stderr_quota(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b"{}\n\n"], stderr=b"0123456789")
        repl = _repl_with_process(process, chunk_size=10, max_buffer_bytes=8)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplStderrBacklog, match="process-generation stderr") as error:
            repl._run("#check Nat", env_id=None, timeout=1)

    assert error.value.response == {}
    assert repl.process is None
    # A bounded tail survives even though the process-wide quota was exceeded.
    assert "Tail: " in str(error.value)
    assert "23456789" in str(error.value)


def test_process_stderr_overflow_without_a_response_is_not_retried(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [], stderr=b"x" * 32)
        repl = _repl_with_process(process, max_buffer_bytes=16)
        _patch_pipe_reads(monkeypatch, process)
        monkeypatch.setattr(
            repl,
            "restart",
            lambda timeout=None: pytest.fail("an uncertain command must not be retried"),
        )

        response = repl.run("#check Nat", timeout=1)

        assert "execution outcome is unknown and was not retried" in response["repl_error"]
        assert response["outcome_unknown"] is True
        assert repl.process is None


def test_explicit_environment_preserves_an_unknown_outcome(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [], stderr=b"x" * 32)
        repl = _repl_with_process(process, max_buffer_bytes=16)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplOutcomeUnknown) as error:
            repl.run("#check Nat", env_id=7, timeout=1)

        assert isinstance(error.value, repl_core.ReplProcessRestarted)
        assert repl.process is None


def test_wire_protocol_drains_stderr_while_waiting_to_write(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b"{}\n\n"], stderr=b"x" * 8)
        repl = _repl_with_process(process, chunk_size=4, max_buffer_bytes=16)
        _patch_pipe_reads(monkeypatch, process)
        normal_select = repl_core.select.select

        def block_stdin_until_stderr_is_drained(
            readable,
            writable,
            exceptional,
            timeout=None,
        ):
            if writable and process.stderr_bytes:
                assert process.stderr.fileno() in readable
                return [process.stderr.fileno()], [], []
            return normal_select(readable, writable, exceptional, timeout)

        monkeypatch.setattr(
            repl_core.select,
            "select",
            block_stdin_until_stderr_is_drained,
        )

        assert repl._run("#check Nat", env_id=None, timeout=1) == {}
        assert process.stderr_bytes == b""
        assert repl._stderr_bytes == 8


def test_wire_protocol_retires_a_process_with_closed_stderr_before_writing():
    with ExitStack() as stack:
        process = _PipeProcess(stack, [])
        process._stderr_write.close()
        repl = _repl_with_process(process)

        with pytest.raises(repl_core.ReplProcessExited, match="before the request frame"):
            repl._run("#check Nat", env_id=None, timeout=1)

        assert repl.process is None


def test_wire_protocol_drains_queued_stderr_after_the_response_frame_completes(monkeypatch):
    # One stdout read completes the frame while stderr still holds several chunks.
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 40)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=1024)
        _patch_pipe_reads(monkeypatch, process)

        assert repl._run("#check Nat", env_id=None, timeout=5) == {"messages": []}

        # Nothing is left to be charged against, or misattributed to, the next command.
        assert process.stderr_bytes == b""


def test_wire_protocol_reports_a_backlog_when_the_cap_ends_the_post_response_drain(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 40)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplStderrBacklog) as error:
            repl._run("#check Nat", env_id=None, timeout=5)

    # The response survives, so the caller need not recompute it...
    assert error.value.response == {"messages": []}
    # ...but the over-budget process generation must not serve another request.
    assert process.stderr_bytes == b"e" * 4


def test_wire_protocol_accepts_stderr_that_ends_exactly_at_the_cap(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 20)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)

        assert repl._run("#check Nat", env_id=None, timeout=5) == {"messages": []}

        assert repl.process is process
        assert process.stderr_bytes == b""


def test_stderr_quota_is_cumulative_across_a_process_generation(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b"{}\n\n"], stderr=b"a" * 6)
        repl = _repl_with_process(process, chunk_size=6, max_buffer_bytes=10)
        _patch_pipe_reads(monkeypatch, process)

        assert repl._run("first", env_id=None, timeout=1) == {}
        assert repl._stderr_bytes == 6

        process.stdout_chunks.append(b"{}\n\n")
        process.stderr_bytes = b"b" * 6
        with pytest.raises(repl_core.ReplStderrBacklog) as error:
            repl._run("second", env_id=None, timeout=1)

        assert error.value.response == {}
        assert repl.process is None


def test_wire_protocol_keeps_the_response_when_the_deadline_ends_the_stderr_drain(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"x")
        repl = _repl_with_process(process, max_buffer_bytes=1_000_000)
        _patch_pipe_reads(monkeypatch, process)

        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            if fd == process.stderr.fileno():
                return b"x" * size
            return real_read(fd, size)

        now = 0.0

        def fake_monotonic() -> float:
            nonlocal now
            now += 0.25
            return now

        monkeypatch.setattr(repl_core.os, "read", fake_read)
        monkeypatch.setattr(repl_core.time, "monotonic", fake_monotonic)

        # Endlessly readable stderr must not hang the post-response drain, and the
        # deadline must not discard a response that was already captured.
        with pytest.raises(repl_core.ReplStderrBacklog) as error:
            repl._run("#check Nat", env_id=None, timeout=1)

    assert error.value.response == {"messages": []}


def _patch_reads_across_processes(monkeypatch, processes: list[_PipeProcess]):
    """Serve reads and readiness for several processes, keyed by descriptor."""
    real_read = os.read

    def take_chunk(chunks: list[bytes], size: int) -> bytes:
        if not chunks:
            return b""
        result = chunks[0][:size]
        chunks[0] = chunks[0][size:]
        if not chunks[0]:
            chunks.pop(0)
        return result

    def fake_read(fd: int, size: int) -> bytes:
        for process in processes:
            if fd == process.stdout.fileno():
                return take_chunk(process.stdout_chunks, size)
            if fd == process.stderr.fileno():
                if not process.stderr_bytes:
                    raise BlockingIOError
                result = process.stderr_bytes[:size]
                process.stderr_bytes = process.stderr_bytes[size:]
                return result
        return real_read(fd, size)

    def fake_select(readable, writable, exceptional, timeout=None):
        if writable:
            return [], writable, []
        ready = []
        for process in processes:
            if process.stderr_bytes:
                ready.append(process.stderr.fileno())
            if process.stdout_chunks:
                ready.append(process.stdout.fileno())
        return [fd for fd in ready if fd in readable], [], []

    monkeypatch.setattr(repl_core.os, "read", fake_read)
    monkeypatch.setattr(repl_core.select, "select", fake_select)


def test_backlog_recycles_the_process_so_two_commands_cannot_share_stderr(monkeypatch):
    with ExitStack() as stack:
        first = _PipeProcess(
            stack,
            [b'{"env":0}\n\n'],
            stderr=b"e" * 40,
        )
        second = _PipeProcess(stack, [b'{"env": 1}\n\n'])
        repl = _repl_with_process(first, chunk_size=18, max_buffer_bytes=20)
        _patch_reads_across_processes(monkeypatch, [first, second])

        # Command one completes, but its stderr cannot be drained within budget.
        assert repl.run("#check Nat", timeout=5) == {}

        # The process holding the remainder is gone, so nothing can inherit it.
        assert repl.process is None
        assert first.stderr_bytes == b"e" * 4

        monkeypatch.setattr(
            repl,
            "restart",
            lambda timeout=None: _install_fake_process(repl, second),
        )

        # Command two runs on a clean process and sees only its own streams.
        assert repl.run("#check Nat", timeout=5) == {"env": 1}

        assert second.stderr_bytes == b""
        # Command one's stderr was never consumed by, or charged against, command two.
        assert first.stderr_bytes == b"e" * 4


def test_backlog_response_drops_environment_owned_by_the_recycled_process(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(
            stack,
            [b'{"env":9,"messages":[]}\n\n'],
            stderr=b"e" * 64,
        )
        repl = _repl_with_process(process, chunk_size=30, max_buffer_bytes=32)
        _patch_pipe_reads(monkeypatch, process)

        response = repl.run("#check Nat", timeout=5)

        assert response == {"messages": []}
        assert repl.process is None


def test_env_scoped_request_refuses_to_outlive_the_recycled_process(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(
            stack,
            [b'{"env":0,"messages": []}\n\n'],
            stderr=b"e" * 40,
        )
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)

        # An explicit environment cannot transparently survive the recycle, so the
        # caller is told rather than handed a response tied to a dead process.
        with pytest.raises(repl_core.ReplProcessRestarted):
            repl.run("#check Nat", env_id=7, timeout=5)

        assert repl.process is None


def test_deadline_ended_drain_recycles_the_process_so_two_commands_cannot_share_stderr(monkeypatch):
    with ExitStack() as stack:
        first = _PipeProcess(
            stack,
            [b'{"env":0,"messages": []}\n\n'],
            stderr=b"x",
        )
        second = _PipeProcess(stack, [b'{"env": 1}\n\n'])
        # A ceiling far out of reach, so the deadline is what ends the drain.
        repl = _repl_with_process(first, max_buffer_bytes=1_000_000)
        _patch_reads_across_processes(monkeypatch, [first, second])

        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            # first's stderr never empties, so no clean boundary is ever reached.
            if fd == first.stderr.fileno():
                return b"x" * size
            return real_read(fd, size)

        now = 0.0

        def fake_monotonic() -> float:
            nonlocal now
            now += 0.25
            return now

        monkeypatch.setattr(repl_core.os, "read", fake_read)
        monkeypatch.setattr(repl_core.time, "monotonic", fake_monotonic)

        # Command one still gets its response: the deadline must not starve stdout.
        assert repl.run("#check Nat", timeout=5) == {"messages": []}

        # But the process that still holds unread stderr is out of service.
        assert repl.process is None
        assert first.stderr_bytes

        monkeypatch.setattr(
            repl,
            "restart",
            lambda timeout=None: _install_fake_process(repl, second),
        )
        monkeypatch.setattr(repl_core.os, "read", real_read)

        # Command two runs on a clean process, unaffected by command one's stderr.
        assert repl.run("#check Nat", timeout=5) == {"env": 1}
        assert second.stderr_bytes == b""


def test_run_never_leaves_a_reusable_process_when_a_backlog_stops_the_drain(monkeypatch):
    # The invariant holds at the source, so no caller of _run() can skip it.
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 40)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplStderrBacklog):
            repl._run("#check Nat", env_id=None, timeout=5)

        assert repl.process is None
        assert repl.is_alive() is False


def test_stderr_arriving_during_the_next_write_is_process_scoped(monkeypatch):
    with ExitStack() as stack:
        first = _PipeProcess(stack, [])
        second = _PipeProcess(stack, [])
        repl = _repl_with_process(first, max_buffer_bytes=16)
        requests: list[bytes] = []

        def serve(process: _PipeProcess, responses: list[bytes]) -> None:
            for response in responses:
                request = bytearray()
                while b"\n\n" not in request:
                    request.extend(os.read(process._stdin_read.fileno(), 4096))
                requests.append(bytes(request))
                os.write(process._stdout_write.fileno(), response)

        first_worker = threading.Thread(
            target=serve,
            args=(first, [b'{"env":1}\n\n', b'{"env":2}\n\n']),
            daemon=True,
        )
        second_worker = threading.Thread(
            target=serve,
            args=(second, [b'{"env":3}\n\n']),
            daemon=True,
        )
        first_worker.start()
        second_worker.start()

        normal_select = repl_core.select.select
        writes = 0

        def inject_during_second_write(readable, writable, exceptional, timeout=None):
            nonlocal writes
            result = normal_select(readable, writable, exceptional, timeout)
            if writable:
                writes += 1
                if writes == 2:
                    # These bytes are emitted by command one after command two's
                    # old preflight window, while command two is being written.
                    os.write(first._stderr_write.fileno(), b"x" * 32)
            return result

        monkeypatch.setattr(repl_core.select, "select", inject_during_second_write)

        assert repl.run("first", timeout=1) == {"env": 1}
        # The process-generation quota is exceeded, but command two's captured
        # response survives without the dead environment identifier or a retry.
        assert repl.run("second", timeout=1) == {}
        assert writes == 2
        assert repl.process is None

        monkeypatch.setattr(
            repl,
            "restart",
            lambda timeout=None: _install_fake_process(repl, second),
        )

        # A third command starts on a clean process generation; command one's
        # delayed stderr was neither consumed nor charged as command-three output.
        assert repl.run("third", timeout=1) == {"env": 3}
        assert repl._stderr_bytes == 0
        assert len(requests) == 3
        first_worker.join(timeout=1)
        second_worker.join(timeout=1)


def test_invalid_json_with_a_stderr_backlog_is_not_retried(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b"not-json\n\n"], stderr=b"e" * 40)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)
        monkeypatch.setattr(
            repl,
            "restart",
            lambda timeout=None: pytest.fail("an uncertain command must not be retried"),
        )

        response = repl.run("#check Nat", timeout=5)

        assert response["outcome_unknown"] is True
        assert "response frame was malformed" in response["repl_error"]
        assert repl.process is None
        assert process.stderr_bytes == b"e" * 4


def test_wire_protocol_rejects_invalid_json(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b"not-json\n\n"])
        repl = _repl_with_process(process)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplOutcomeUnknown, match="fully sent"):
            repl._run("#check Nat", env_id=None, timeout=1)


def test_wire_protocol_rejects_oversized_response(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"data":"0123456789"}\n\n'])
        repl = _repl_with_process(process, max_buffer_bytes=8)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplOutcomeUnknown, match="response exceeded 8 bytes"):
            repl._run("#check Nat", env_id=None, timeout=1)
