from __future__ import annotations

import signal
import threading
from pathlib import Path

import pytest

from autoform_cli.runtime import (
    RuntimeAssertions,
    RuntimeLeanTarget,
    RuntimeNode,
    RuntimeStatus,
)
from servers.prover import Event, EventKind, ProofResult, ProverAdapter, Run
from servers.prover import _cli_common
from servers.prover.claude_adapter import ClaudeAdapter, DEFAULT_AUTONOMY_ARGS as CLAUDE_ARGS
from servers.prover.codex_adapter import CodexAdapter, DEFAULT_AUTONOMY_ARGS as CODEX_ARGS
from servers.prover import driver as prover_driver
from servers.prover.driver import prove
from servers.prover.muse_adapter import MuseAdapter
from servers.prover.verify import (
    Baseline,
    VerifyResult,
    capture_baseline,
    restore_baseline,
    verify_proof,
)


def runtime_node(
    *,
    source_file: str = "Main.lean",
    dispatchable: bool = True,
    can_prove: bool = True,
    not_ready: bool = False,
) -> RuntimeNode:
    return RuntimeNode(
        id="chapter/result",
        title="Result",
        article_path="blueprint/roadmap/chapter/result.md",
        parent="chapter",
        depth=1,
        declaration="theorem result",
        formalizable=True,
        dispatchable=dispatchable,
        statement_dependencies=(),
        proof_dependencies=(),
        dependencies=(),
        assertions=RuntimeAssertions(True, False, not_ready),
        status=RuntimeStatus("ready_to_prove", True, can_prove, True, False, False, False),
        origin=None,
        source_targets=(),
        lean_targets=(RuntimeLeanTarget("result", source_file),),
        mathlib=False,
        mathlib_declarations=(),
        mathlib_file=None,
    )


def lake_project(tmp_path: Path, source: str = "theorem result : True := by trivial\n") -> Path:
    (tmp_path / "lakefile.toml").write_text('[package]\nname = "test"\n')
    (tmp_path / "Main.lean").write_text(source)
    return tmp_path


class FakeRuntime:
    def __init__(
        self,
        response: object = "No diagnostics — file compiles cleanly.",
        *,
        hovers: tuple[object, ...] = ("theorem result : True",),
    ) -> None:
        self.response = response
        self.hovers = iter(hovers)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def request(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        if method == "lsp.hover":
            return next(self.hovers)
        return self.response


class RejectingAxiomRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.audit_source = ""

    def request(self, method, params=None, **kwargs):
        if method == "lsp.diagnostics" and str(params["file_path"]).startswith(
            ".lake/autoform-verify/AutoformVerify_"
        ):
            self.audit_source = (Path(params["project_dir"]) / params["file_path"]).read_text()
            self.calls.append((method, params))
            return "Diagnostics: 1 error(s), 0 warning(s)\n1:1: error: unexpected axiom"
        return super().request(method, params, **kwargs)


def test_verify_rejects_noop_claim_and_uses_shared_runtime_for_changed_target(tmp_path: Path) -> None:
    project = lake_project(tmp_path)
    node = runtime_node()
    runtime = FakeRuntime(hovers=("theorem result : True", "theorem result : True"))
    baseline = capture_baseline(node, str(project), runtime=runtime)
    runtime.calls.clear()

    unchanged = verify_proof

    unchanged = verify_proof(node, str(project), baseline=baseline, runtime=runtime)
    assert not unchanged.ok
    assert "did not change" in unchanged.reason
    assert runtime.calls == []

    (project / "Main.lean").write_text("theorem result : True := by\n  exact True.intro\n")
    verified = verify_proof(node, str(project), baseline=baseline, runtime=runtime)
    assert verified.ok
    assert [method for method, _ in runtime.calls] == [
        "lsp.hover",
        "lsp.diagnostics",
        "lsp.diagnostics",
    ]
    assert runtime.calls[1] == (
        "lsp.diagnostics",
        {"project_dir": str(project), "file_path": "Main.lean"},
    )
    assert runtime.calls[2][1]["file_path"].startswith(
        ".lake/autoform-verify/AutoformVerify_"
    )
    assert verified.checks["changed_targets"] == ["Main.lean"]


def test_verify_rejects_target_statement_drift_and_missing_declaration(tmp_path: Path) -> None:
    project = lake_project(tmp_path, "theorem result : False := by sorry\n")
    node = runtime_node()
    baseline = capture_baseline(node, str(project), runtime=FakeRuntime())

    (project / "Main.lean").write_text("theorem result : True := by trivial\n")
    drift = verify_proof(node, str(project), baseline=baseline, runtime=FakeRuntime())
    assert not drift.ok
    assert "header changed" in drift.reason

    (project / "Main.lean").write_text("theorem unrelated : True := by trivial\n")
    missing = verify_proof(node, str(project), baseline=baseline, runtime=FakeRuntime())
    assert not missing.ok
    assert "does not resolve" in missing.reason


def test_verify_rejects_statement_drift_after_assignment_inside_theorem_type(
    tmp_path: Path,
) -> None:
    project = lake_project(
        tmp_path,
        "theorem result : (let n := 1; n = n) := by rfl\n",
    )
    runtime = FakeRuntime(
        hovers=(
            "theorem result : let n := 1; n = n",
            "theorem result : let n := 1; True",
        )
    )
    baseline = capture_baseline(runtime_node(), str(project), runtime=runtime)
    runtime.calls.clear()

    (project / "Main.lean").write_text(
        "theorem result : (let n := 1; True) := by trivial\n"
    )
    result = verify_proof(
        runtime_node(),
        str(project),
        baseline=baseline,
        runtime=runtime,
    )

    assert not result.ok
    assert "elaborated type changed" in result.reason
    assert [method for method, _ in runtime.calls] == ["lsp.hover"]


def test_verify_rejects_unrelated_declaration_changes_in_target_file(tmp_path: Path) -> None:
    project = lake_project(
        tmp_path,
        "def helper : Nat := 1\n\ntheorem result : True := by trivial\n",
    )
    runtime = FakeRuntime(hovers=("theorem result : True", "theorem result : True"))
    baseline = capture_baseline(runtime_node(), str(project), runtime=runtime)
    runtime.calls.clear()

    (project / "Main.lean").write_text(
        "def helper : Nat := 2\n\ntheorem result : True := by\n  exact True.intro\n"
    )
    result = verify_proof(
        runtime_node(),
        str(project),
        baseline=baseline,
        runtime=runtime,
    )

    assert not result.ok
    assert "outside target declarations" in result.reason
    assert runtime.calls == []


def test_verify_rejects_top_level_commands_after_target_declaration(tmp_path: Path) -> None:
    project = lake_project(
        tmp_path,
        "theorem result : True := by trivial\nset_option pp.universes false\n",
    )
    runtime = FakeRuntime(hovers=("theorem result : True",))
    baseline = capture_baseline(runtime_node(), str(project), runtime=runtime)
    runtime.calls.clear()

    (project / "Main.lean").write_text(
        "theorem result : True := by\n  exact True.intro\nset_option pp.universes true\n"
    )
    result = verify_proof(
        runtime_node(),
        str(project),
        baseline=baseline,
        runtime=runtime,
    )

    assert not result.ok
    assert "outside target declarations" in result.reason
    assert runtime.calls == []


def test_baseline_hover_targets_declaration_name_not_attribute_text(tmp_path: Path) -> None:
    source = "@[inherit_doc Other.result] theorem result : True := by trivial\n"
    project = lake_project(tmp_path, source)
    runtime = FakeRuntime(hovers=("theorem result : True",))

    capture_baseline(runtime_node(), str(project), runtime=runtime)

    assert runtime.calls == [
        (
            "lsp.hover",
            {
                "project_dir": str(project),
                "file_path": "Main.lean",
                "line": 0,
                "character": source.index("result", source.index("theorem")) + 3,
            },
        )
    ]


def test_verify_uses_lean_axiom_audit_for_preexisting_assumptions(tmp_path: Path) -> None:
    project = lake_project(
        tmp_path,
        "axiom existingCheat : False\n\ntheorem result : False := by sorry\n",
    )
    runtime = RejectingAxiomRuntime()
    runtime.hovers = iter(("theorem result : False", "theorem result : False"))
    baseline = capture_baseline(runtime_node(), str(project), runtime=runtime)
    runtime.calls.clear()

    (project / "Main.lean").write_text(
        "axiom existingCheat : False\n\ntheorem result : False := by\n  exact existingCheat\n"
    )
    result = verify_proof(
        runtime_node(),
        str(project),
        baseline=baseline,
        runtime=runtime,
    )

    assert not result.ok
    assert "kernel trust audit" in result.reason
    assert "(env.find? target).isNone" in runtime.audit_source
    assert "Lean.collectAxioms target" in runtime.audit_source
    assert 'Name.str (Name.anonymous) "result"' in runtime.audit_source
    assert "``propext, ``Classical.choice, ``Quot.sound" in runtime.audit_source
    assert not any((project / ".lake" / "autoform-verify").glob("AutoformVerify_*.lean"))


def test_restore_baseline_does_not_follow_symlink_swaps(tmp_path: Path) -> None:
    project = lake_project(tmp_path)
    runtime = FakeRuntime(hovers=("theorem result : True", "theorem result : True"))
    baseline = capture_baseline(runtime_node(), str(project), runtime=runtime)
    runtime.calls.clear()
    candidate = "theorem result : True := by\n  exact True.intro\n"
    target = project / "Main.lean"
    target.write_text(candidate)
    assert verify_proof(
        runtime_node(),
        str(project),
        baseline=baseline,
        runtime=runtime,
    ).ok

    outside = tmp_path.parent / f"{tmp_path.name}-outside.lean"
    outside.write_text(candidate)
    target.unlink()
    target.symlink_to(outside)
    restore_baseline(baseline)

    assert target.is_symlink()
    assert outside.read_text() == candidate


def test_restore_baseline_preserves_changes_after_verified_attempt(tmp_path: Path) -> None:
    project = lake_project(tmp_path)
    runtime = FakeRuntime(hovers=("theorem result : True", "theorem result : True"))
    baseline = capture_baseline(runtime_node(), str(project), runtime=runtime)
    runtime.calls.clear()
    candidate = "theorem result : True := by\n  exact True.intro\n"
    concurrent = "theorem result : True := by\n  exact id True.intro\n"
    (project / "Main.lean").write_text(candidate)

    result = verify_proof(
        runtime_node(),
        str(project),
        baseline=baseline,
        runtime=runtime,
    )
    assert result.ok

    (project / "Main.lean").write_text(concurrent)
    restore_baseline(baseline)

    assert (project / "Main.lean").read_text() == concurrent


def test_verify_rejects_new_axioms_and_character_literal_scanner_bypass(tmp_path: Path) -> None:
    project = lake_project(tmp_path, "theorem result : False := by sorry\n")
    node = runtime_node()
    baseline = capture_baseline(node, str(project), runtime=FakeRuntime())
    source = "def quote : Char := '\"'\naxiom cheat : False\ntheorem result : False := by exact cheat\n"
    (project / "Main.lean").write_text(source)
    result = verify_proof(node, str(project), baseline=baseline, runtime=FakeRuntime())
    assert not result.ok
    assert "introduced forbidden token 'axiom'" in result.reason


def test_verify_rejects_non_target_and_configuration_mutations(tmp_path: Path) -> None:
    project = lake_project(tmp_path)
    helper = project / "Helper.lean"
    helper.write_text("def helper : Nat := 1\n")
    node = runtime_node()
    baseline = capture_baseline(node, str(project), runtime=FakeRuntime())
    (project / "Main.lean").write_text("theorem result : True := by\n  exact True.intro\n")
    helper.write_text("axiom cheat : False\n")
    result = verify_proof(node, str(project), baseline=baseline, runtime=FakeRuntime())
    assert not result.ok
    assert "non-target Lean/config inputs" in result.reason

    restore_baseline(baseline)
    assert helper.read_text() == "def helper : Nat := 1\n"


def test_verify_fails_closed_on_unrecognized_diagnostics(tmp_path: Path) -> None:
    project = lake_project(tmp_path)
    for response in ("service unavailable", "Diagnostics: 1 error(s), 0 warning(s)"):
        result = verify_proof(runtime_node(), str(project), runtime=FakeRuntime(response))
        assert not result.ok
        assert "not a recognized clean result" in result.reason


@pytest.mark.parametrize(
    "source",
    [
        "theorem result : True := by sorry\n",
        "theorem result : True := by admit\n",
        "run_cmd IO.println \"untrusted elaboration\"\n theorem result : True := by trivial\n",
        "unsafe theorem result : True := by trivial\n",
    ],
)
def test_verify_rejects_forbidden_proof_escapes_before_runtime(tmp_path: Path, source: str) -> None:
    project = lake_project(tmp_path, source)
    runtime = FakeRuntime()
    result = verify_proof(runtime_node(), str(project), runtime=runtime)
    assert not result.ok
    assert "forbidden token" in result.reason
    assert runtime.calls == []


def test_verify_ignores_forbidden_words_in_nested_comments_and_strings(tmp_path: Path) -> None:
    source = '''/- outer sorry /- run_cmd IO.println "bad" -/ still comment -/\n\ntheorem result : True := by\n  have note := "admit #eval unsafe theorem"\n  trivial\n'''
    project = lake_project(tmp_path, source)
    result = verify_proof(runtime_node(), str(project), runtime=FakeRuntime())
    assert result.ok


def test_verify_rejects_runtime_errors(tmp_path: Path) -> None:
    project = lake_project(tmp_path)
    runtime = FakeRuntime("Diagnostics: 1 error(s), 0 warning(s)\n1:1: error: type mismatch")
    result = verify_proof(runtime_node(), str(project), runtime=runtime)
    assert not result.ok
    assert "not a recognized clean result" in result.reason


class CancellingAdapter(ProverAdapter):
    name = "cancel-test"

    def __init__(self, cancel: threading.Event) -> None:
        self.cancel = cancel
        self.closed = False
        self.result_called = False

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        return Run(self.name, goal=spec, project_dir=project_dir)

    def events(self, run: Run):
        try:
            yield Event(EventKind.MESSAGE, "started")
            self.cancel.set()
            yield Event(EventKind.MESSAGE, "must not be consumed")
        finally:
            self.closed = True

    def steer(self, run: Run, message: str) -> None:
        raise AssertionError("cancelled runs must not steer")

    def result(self, run: Run) -> ProofResult:
        self.result_called = True
        return ProofResult("proved")


def test_driver_pre_cancel_prevents_backend_launch() -> None:
    cancel = threading.Event()
    cancel.set()
    adapter = CancellingAdapter(cancel)
    result = prove(
        adapter,
        runtime_node(),
        "prove True",
        "/unused",
        verifier=None,
        cancel_event=cancel,
    )
    assert result.meta["sub_status"] == "cancelled"
    assert adapter.closed is False
    assert adapter.result_called is False


def test_driver_cancellation_closes_event_stream_and_normalizes_result(tmp_path: Path) -> None:
    cancel = threading.Event()
    adapter = CancellingAdapter(cancel)
    result = prove(
        adapter,
        runtime_node(),
        "prove True",
        str(tmp_path),
        verifier=None,
        cancel_event=cancel,
    )
    assert result.status == "failed"
    assert result.reason == "prover run cancelled"
    assert result.meta["sub_status"] == "cancelled"
    assert adapter.closed is True
    assert adapter.result_called is False


class EditingAdapter(ProverAdapter):
    name = "edit-test"

    def __init__(
        self,
        project: Path,
        result_status: str,
        *,
        cancel: threading.Event | None = None,
    ) -> None:
        self.project = project
        self.result_status = result_status
        self.cancel = cancel
        self.result_called = False

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        return Run(self.name, goal=spec, project_dir=project_dir)

    def events(self, run: Run):
        self.project.joinpath("Main.lean").write_text(
            "theorem result : True := by\n  exact True.intro\n"
        )
        yield Event(EventKind.EDIT, "edited Main.lean", path="Main.lean")
        if self.cancel is not None:
            self.cancel.set()
            yield Event(EventKind.MESSAGE, "cancelled after edit")

    def steer(self, run: Run, message: str) -> None:
        raise AssertionError("editing adapter must not steer")

    def result(self, run: Run) -> ProofResult:
        self.result_called = True
        return ProofResult(self.result_status, reason="blocked" if self.result_status == "failed" else "")


def _patch_lightweight_baseline(monkeypatch, project: Path) -> str:
    original = (project / "Main.lean").read_text()
    baseline = Baseline(
        root=project,
        files={
            "Main.lean": original.encode(),
            "lakefile.toml": (project / "lakefile.toml").read_bytes(),
        },
        targets=frozenset({"Main.lean"}),
    )
    monkeypatch.setattr(prover_driver, "capture_baseline", lambda node, project_dir: baseline)
    return original


def test_driver_cancellation_after_edit_restores_attempt_bytes(monkeypatch, tmp_path: Path) -> None:
    project = lake_project(tmp_path)
    original = _patch_lightweight_baseline(monkeypatch, project)
    cancel = threading.Event()
    adapter = EditingAdapter(project, "proved", cancel=cancel)

    result = prove(
        adapter,
        runtime_node(),
        "prove True",
        str(project),
        verifier=lambda *args, **kwargs: VerifyResult(True),
        cancel_event=cancel,
    )

    assert result.meta["sub_status"] == "cancelled"
    assert adapter.result_called is False
    assert (project / "Main.lean").read_text() == original


def test_driver_honest_failure_after_edit_restores_attempt_bytes(monkeypatch, tmp_path: Path) -> None:
    project = lake_project(tmp_path)
    original = _patch_lightweight_baseline(monkeypatch, project)
    adapter = EditingAdapter(project, "failed")

    result = prove(
        adapter,
        runtime_node(),
        "prove True",
        str(project),
        verifier=lambda *args, **kwargs: VerifyResult(True),
    )

    assert result.status == "failed"
    assert result.reason == "blocked"
    assert (project / "Main.lean").read_text() == original


def test_driver_verified_success_keeps_attempt_bytes(monkeypatch, tmp_path: Path) -> None:
    project = lake_project(tmp_path)
    original = _patch_lightweight_baseline(monkeypatch, project)
    adapter = EditingAdapter(project, "proved")

    result = prove(
        adapter,
        runtime_node(),
        "prove True",
        str(project),
        verifier=lambda *args, **kwargs: VerifyResult(True, checks={"verified": True}),
    )

    assert result.status == "proved"
    assert result.meta["verify"] == {"verified": True}
    assert (project / "Main.lean").read_text() != original


class SteeringAdapter(ProverAdapter):
    name = "steer-test"

    def __init__(self) -> None:
        self.steers: list[str] = []

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        return Run(self.name, goal=spec)

    def events(self, run: Run):
        for index in range(6):
            yield Event(EventKind.ERROR, f"failure {index}")

    def steer(self, run: Run, message: str) -> None:
        self.steers.append(message)

    def result(self, run: Run) -> ProofResult:
        return ProofResult("failed", reason="blocked")


class AlwaysSteer:
    calls = 0
    usage: dict[str, float] = {}

    def off_course(self, goal, window):
        return True

    def correction(self, goal, window):
        return "try a different lemma"


def test_driver_refuses_dependency_blocked_or_not_ready_nodes(tmp_path: Path) -> None:
    adapter = SteeringAdapter()
    for node in (runtime_node(can_prove=False), runtime_node(not_ready=True)):
        with pytest.raises(ValueError, match="not ready to prove"):
            prove(adapter, node, "prove True", str(tmp_path), verifier=None)


def test_driver_enforces_steer_cap(tmp_path: Path) -> None:
    adapter = SteeringAdapter()
    result = prove(
        adapter,
        runtime_node(),
        "prove True",
        str(tmp_path),
        verifier=None,
        steerer=AlwaysSteer(),
        judge_policy="always",
        max_steers=2,
    )
    assert result.status == "failed"
    assert adapter.steers == ["try a different lemma", "try a different lemma"]
    assert result.meta["steering"]["steers"] == 2


@pytest.mark.parametrize(
    ("adapter", "label"),
    [
        (ClaudeAdapter(mcp_config="", runner=lambda *args: (_ for _ in ()).throw(OSError("missing"))), "Claude"),
        (CodexAdapter(runner=lambda *args: (_ for _ in ()).throw(OSError("missing"))), "Codex"),
        (MuseAdapter(runner=lambda *args: (_ for _ in ()).throw(OSError("missing"))), "Muse"),
    ],
)
def test_backend_launch_failures_are_normalized(adapter, label) -> None:
    run = adapter.start("node", "spec", "/project")
    list(adapter.events(run))
    result = adapter.result(run)
    assert result.status == "failed"
    assert result.meta["sub_status"] == "backend_error"
    assert f"could not launch {label} worker" in result.reason


def test_claude_clean_run_has_initialized_terminal_error() -> None:
    adapter = ClaudeAdapter(
        mcp_config="",
        runner=lambda *args: iter(['{"type":"result","result":"completed"}']),
    )
    run = adapter.start("node", "spec", "/project")

    list(adapter.events(run))
    result = adapter.result(run)

    assert result.status == "proved"
    assert result.reason == ""


def test_backend_sandbox_policy_cannot_be_disabled_by_environment(monkeypatch) -> None:
    monkeypatch.setenv("AUTOFORM_UNSAFE_FULL_ACCESS", "1")
    claude = ClaudeAdapter(mcp_config="")
    codex = CodexAdapter()
    assert claude._autonomy_args == CLAUDE_ARGS
    assert codex._autonomy_args == CODEX_ARGS
    assert all("dangerously" not in arg for arg in claude._autonomy_args + codex._autonomy_args)


@pytest.mark.parametrize("adapter", [ClaudeAdapter(mcp_config=""), CodexAdapter(), MuseAdapter()])
def test_backend_deadlines_are_positive_and_bounded(adapter) -> None:
    run = adapter.start("node", "spec", "/project")
    assert run.handle.deadline is not None


@pytest.mark.parametrize("adapter_type", [ClaudeAdapter, CodexAdapter, MuseAdapter])
@pytest.mark.parametrize("timeout", [0, float("nan"), float("inf")])
def test_backend_rejects_nonpositive_or_nonfinite_deadline(adapter_type, timeout) -> None:
    kwargs = {"mcp_config": ""} if adapter_type is ClaudeAdapter else {}
    with pytest.raises(ValueError, match="must be positive"):
        adapter_type(max_wait_seconds=timeout, **kwargs)


class FakeProcess:
    pid = 123

    def __init__(self) -> None:
        self.running = True
        self.waits: list[int] = []
        self.signals: list[int] = []

    def poll(self):
        return None if self.running else 0

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if not self.running:
            return 0
        if len(self.waits) == 1:
            raise TimeoutError
        self.running = False
        return 0


def test_process_tree_cleanup_escalates_to_kill(monkeypatch) -> None:
    process = FakeProcess()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(_cli_common.os, "getpgid", lambda pid: 321)
    monkeypatch.setattr(_cli_common.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    _cli_common._kill_process_tree(process)
    assert signals == [(321, signal.SIGTERM), (321, signal.SIGKILL)]
    assert process.waits == [5, 5]


def test_json_line_parser_ignores_non_object_values() -> None:
    assert list(_cli_common._iter_json_lines(iter(["[]", "1", '{\"type\": \"result\"}']))) == [
        {"type": "result"}
    ]


def test_process_runner_rejects_nonzero_exit(monkeypatch, tmp_path: Path) -> None:
    process = FakeProcess()
    process.running = False
    process.stdout = iter(())
    process.wait = lambda timeout=None: 7
    monkeypatch.setattr(_cli_common.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(_cli_common.ProverProcessError, match="status 7"):
        list(_cli_common._subprocess_line_runner(["worker"], {}, str(tmp_path)))


def test_silent_subprocess_runner_observes_cancellation(monkeypatch, tmp_path: Path) -> None:
    cancel = threading.Event()
    process = FakeProcess()

    class SilentStdout:
        def __iter__(self):
            cancel.wait(timeout=2)
            return iter(())

        def close(self):
            pass

    process.stdout = SilentStdout()
    killed: list[FakeProcess] = []

    def record_kill(proc: FakeProcess) -> None:
        killed.append(proc)
        proc.running = False

    monkeypatch.setattr(_cli_common.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(_cli_common, "_kill_process_tree", record_kill)
    cancel.set()

    with pytest.raises(_cli_common.ProverCancelled, match="was cancelled"):
        list(
            _cli_common._subprocess_line_runner(
                ["worker"],
                {},
                str(tmp_path),
                cancel_event=cancel,
            )
        )
    assert killed == [process]
