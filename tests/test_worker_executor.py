from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from autoform_cli.runtime import (
    RuntimeAssertions,
    RuntimeGraph,
    RuntimeLeanTarget,
    RuntimeNode,
    RuntimeStatus,
)
from autoform_worker.executor import ProverExecutor, _attempt_result, _verify_statement, backend_factory
from autoform_worker.scheduler import AttemptOutcome, WorkItem, WorkPhase
from servers.prover import Event, EventKind, ProofResult, ProverAdapter, Run


def _node(
    *,
    stated: bool = False,
    proved: bool = False,
    source_file: str | None = None,
) -> RuntimeNode:
    return RuntimeNode(
        id="result",
        title="Result",
        article_path="blueprint/roadmap/result.md",
        parent=None,
        depth=0,
        declaration="theorem",
        formalizable=True,
        dispatchable=True,
        statement_dependencies=(),
        proof_dependencies=(),
        dependencies=(),
        assertions=RuntimeAssertions(stated, proved, False),
        status=RuntimeStatus(
            "proved" if proved else ("can_prove" if stated else "can_state"),
            not stated,
            stated and not proved,
            stated,
            proved,
            proved,
            False,
        ),
        origin=None,
        source_targets=(),
        lean_targets=(RuntimeLeanTarget("result", source_file),) if source_file else (),
        mathlib=False,
        mathlib_declarations=(),
        mathlib_file=None,
    )


def _runtime(node: RuntimeNode) -> RuntimeGraph:
    return RuntimeGraph(
        "autoform-runtime/v1",
        "markdown-articles",
        "revision",
        "blueprint",
        (node,),
        1,
        1,
        1,
        0,
        0,
    )


class FakeAdapter(ProverAdapter):
    name = "fake"

    def __init__(self, result: ProofResult, on_event=None) -> None:
        self.terminal = result
        self.on_event = on_event
        self.cancel = None
        self.started: list[tuple[str, str, str]] = []

    def bind_cancel_event(self, cancel_event) -> None:
        self.cancel = cancel_event

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        self.started.append((node, spec, project_dir))
        return Run(self.name, goal=spec, project_dir=project_dir)

    def events(self, run: Run):
        if self.on_event is not None:
            self.on_event()
        yield Event(EventKind.RESULT, self.terminal.status)

    def steer(self, run: Run, message: str) -> None:
        pass

    def result(self, run: Run) -> ProofResult:
        return self.terminal


@pytest.mark.parametrize("name", ["claude", "codex", "muse"])
def test_backend_factory_supports_only_safe_cli_backends(name: str) -> None:
    assert isinstance(backend_factory(name)(), ProverAdapter)


def test_backend_factory_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        backend_factory("other")


def test_proof_result_mapping_distinguishes_retry_cancel_and_failure() -> None:
    assert _attempt_result(ProofResult("proved")).outcome is AttemptOutcome.SUCCEEDED
    assert (
        _attempt_result(ProofResult("failed", reason="missing", meta={"sub_status": "backend_error"})).outcome
        is AttemptOutcome.RETRY
    )
    assert (
        _attempt_result(ProofResult("failed", reason="stopped", meta={"sub_status": "cancelled"})).outcome
        is AttemptOutcome.CANCELLED
    )
    assert _attempt_result(ProofResult("failed", reason="invalid proof")).outcome is AttemptOutcome.FAILED


def test_statement_backend_claim_requires_fresh_runtime_confirmation(tmp_path, monkeypatch) -> None:
    article = tmp_path / "blueprint" / "roadmap" / "result.md"
    article.parent.mkdir(parents=True)
    article.write_text("---\ndeclaration: theorem\n---\n# Result\n")
    adapter = FakeAdapter(ProofResult("proved"))
    executor = ProverExecutor(tmp_path, lambda: adapter)
    monkeypatch.setattr("autoform_worker.executor.load_runtime_graph", lambda *args, **kwargs: _runtime(_node()))

    result = executor(WorkItem(_node(), WorkPhase.STATEMENT, 1, "revision"), threading.Event())

    assert result.outcome is AttemptOutcome.RETRY
    assert "still reports it unstated" in result.detail
    assert "Do not commit, push" in adapter.started[0][1]


def test_statement_success_is_verified_by_fresh_runtime_projection(tmp_path, monkeypatch) -> None:
    article = tmp_path / "blueprint" / "roadmap" / "result.md"
    article.parent.mkdir(parents=True)
    article.write_text("---\ndeclaration: theorem\n---\n# Result\n")

    def author_statement() -> None:
        article.write_text(
            "---\ndeclaration: theorem\nlean: result\nstatement: formalized\n---\n# Result\n"
        )
        (tmp_path / "Main.lean").write_text("theorem result : True := by trivial\n")

    adapter = FakeAdapter(ProofResult("proved"), on_event=author_statement)
    executor = ProverExecutor(tmp_path, lambda: adapter)
    monkeypatch.setattr(
        "autoform_worker.executor.load_runtime_graph",
        lambda *args, **kwargs: _runtime(_node(stated=True, source_file="Main.lean")),
    )
    monkeypatch.setattr(
        "autoform_worker.executor._verify_statement",
        lambda *args, **kwargs: "",
    )

    result = executor(WorkItem(_node(), WorkPhase.STATEMENT, 1, "revision"), threading.Event())

    assert result.outcome is AttemptOutcome.SUCCEEDED
    assert "compiled Lean declaration" in result.detail


def test_statement_verifier_rejects_any_unresolved_target(tmp_path) -> None:
    node = _node(stated=True, source_file="Main.lean")
    node = replace(
        node,
        lean_targets=(
            RuntimeLeanTarget("result", "Main.lean"),
            RuntimeLeanTarget("missing", None),
        ),
    )

    assert "no resolvable local Lean declaration" in _verify_statement(node, tmp_path)


def test_statement_markdown_claim_requires_resolvable_compiled_lean(tmp_path, monkeypatch) -> None:
    article = tmp_path / "blueprint" / "roadmap" / "result.md"
    article.parent.mkdir(parents=True)
    article.write_text("statement_formalized: false\n")
    adapter = FakeAdapter(
        ProofResult("proved"),
        on_event=lambda: (tmp_path / "Main.lean").write_text("theorem result : True := by trivial\n"),
    )
    executor = ProverExecutor(tmp_path, lambda: adapter)
    refreshed_node = _node(stated=True, source_file="Main.lean")
    monkeypatch.setattr(
        "autoform_worker.executor.load_runtime_graph",
        lambda *args, **kwargs: _runtime(refreshed_node),
    )
    checked = []

    def reject_unresolved(node, project_dir):
        checked.append((node, project_dir))
        return "target declaration does not resolve in Main.lean: result"

    monkeypatch.setattr("autoform_worker.executor._verify_statement", reject_unresolved)

    result = executor(WorkItem(_node(), WorkPhase.STATEMENT, 1, "revision"), threading.Event())

    assert result.outcome is AttemptOutcome.RETRY
    assert "does not resolve" in result.detail
    assert checked == [(refreshed_node, tmp_path.resolve())]


@pytest.mark.parametrize(
    ("backend_result", "cancel_during_run", "expected_outcome"),
    [
        (ProofResult("failed", reason="invalid statement"), False, AttemptOutcome.FAILED),
        (ProofResult("proved"), True, AttemptOutcome.CANCELLED),
    ],
)
def test_unsuccessful_statement_restores_authoritative_inputs(
    tmp_path,
    backend_result,
    cancel_during_run,
    expected_outcome,
) -> None:
    article = tmp_path / "blueprint" / "roadmap" / "result.md"
    article.parent.mkdir(parents=True)
    article.write_text("---\ndeclaration: theorem\n---\n# Result\n")
    source = tmp_path / "Main.lean"
    source.write_text("-- original\n")
    cancel = threading.Event()

    def mutate_project() -> None:
        article.write_text(
            "---\ndeclaration: theorem\nlean: result\nstatement: formalized\n---\n# Result\n"
        )
        source.write_text("theorem result : True := by trivial\n")
        (tmp_path / "Created.lean").write_text("theorem extra : True := by trivial\n")
        if cancel_during_run:
            cancel.set()

    adapter = FakeAdapter(backend_result, on_event=mutate_project)
    result = ProverExecutor(tmp_path, lambda: adapter)(
        WorkItem(_node(), WorkPhase.STATEMENT, 1, "revision"), cancel
    )

    assert result.outcome is expected_outcome
    assert article.read_text() == "---\ndeclaration: theorem\n---\n# Result\n"
    assert source.read_text() == "-- original\n"
    assert not (tmp_path / "Created.lean").exists()


@pytest.mark.parametrize(
    ("refreshed_proved", "expected_outcome"),
    [(False, AttemptOutcome.RETRY), (True, AttemptOutcome.SUCCEEDED)],
)
def test_proof_success_requires_fresh_runtime_status_transition(
    tmp_path,
    monkeypatch,
    refreshed_proved,
    expected_outcome,
) -> None:
    original = _node(stated=True)
    monkeypatch.setattr(
        "autoform_worker.executor.prove",
        lambda *args, **kwargs: ProofResult("proved"),
    )
    monkeypatch.setattr(
        "autoform_worker.executor.load_runtime_graph",
        lambda *args, **kwargs: _runtime(_node(stated=True, proved=refreshed_proved)),
    )

    result = ProverExecutor(tmp_path, lambda: FakeAdapter(ProofResult("proved")))(
        WorkItem(original, WorkPhase.PROOF, 1, "revision"), threading.Event()
    )

    assert result.outcome is expected_outcome
    if refreshed_proved:
        assert "authoritative runtime transition" in result.detail
    else:
        assert "still reports it unproved" in result.detail


def test_statement_success_rejects_stale_already_stated_work_item(tmp_path, monkeypatch) -> None:
    article = tmp_path / "blueprint" / "roadmap" / "result.md"
    article.parent.mkdir(parents=True)
    article.write_text("statement_formalized: true\nlean: result\n")
    source = tmp_path / "Main.lean"
    source.write_text("theorem result : True := by trivial\n")
    node = _node(stated=True, source_file="Main.lean")
    monkeypatch.setattr("autoform_worker.executor.load_runtime_graph", lambda *args, **kwargs: _runtime(node))

    result = ProverExecutor(tmp_path, lambda: FakeAdapter(ProofResult("proved")))(
        WorkItem(node, WorkPhase.STATEMENT, 1, "revision"), threading.Event()
    )

    assert result.outcome is AttemptOutcome.RETRY
    assert "did not transition from false to true" in result.detail


@pytest.mark.parametrize(
    ("mutation", "expected_detail"),
    [
        ("config", "non-target Lean/config inputs"),
        ("non_target", "non-target Lean/config inputs"),
        ("new_file", "non-target Lean/config inputs"),
        ("unrelated_declaration", "declaration delta does not match claimed targets"),
        ("article", "selected roadmap article changed outside statement/lean frontmatter"),
    ],
)
def test_statement_success_rejects_unrelated_side_effects(tmp_path, monkeypatch, mutation, expected_detail) -> None:
    article = tmp_path / "blueprint" / "roadmap" / "result.md"
    article.parent.mkdir(parents=True)
    article.write_text("---\ndeclaration: theorem\n---\n# Result\n")
    source = tmp_path / "Main.lean"
    source.write_text("-- existing target module\n")
    other = tmp_path / "Other.lean"
    other.write_text("theorem existing : True := by trivial\n")
    config = tmp_path / "lean-toolchain"
    config.write_text("leanprover/lean4:v4.19.0\n")

    def mutate_project() -> None:
        article.write_text(
            "---\ndeclaration: theorem\nlean: result\nstatement: formalized\n---\n# Result\n"
        )
        source.write_text("-- existing target module\ntheorem result : True := by trivial\n")
        if mutation == "config":
            config.write_text("leanprover/lean4:nightly\n")
        elif mutation == "non_target":
            other.write_text("theorem existing : False := by trivial\n")
        elif mutation == "new_file":
            (tmp_path / "Unrelated.lean").write_text("-- unrelated new input\n")
        elif mutation == "unrelated_declaration":
            source.write_text(
                "-- existing target module\n"
                "theorem unrelated : True := by trivial\n"
                "theorem result : True := by trivial\n"
            )
        else:
            article.write_text(
                "---\ndeclaration: theorem\nlean: result\nstatement: formalized\n---\n"
                "# Rewritten result\n"
            )

    refreshed = _node(stated=True, source_file="Main.lean")
    monkeypatch.setattr("autoform_worker.executor.load_runtime_graph", lambda *args, **kwargs: _runtime(refreshed))
    monkeypatch.setattr("autoform_worker.executor._verify_statement", lambda *args, **kwargs: "")

    result = ProverExecutor(tmp_path, lambda: FakeAdapter(ProofResult("proved"), on_event=mutate_project))(
        WorkItem(_node(), WorkPhase.STATEMENT, 1, "revision"), threading.Event()
    )

    assert result.outcome is AttemptOutcome.RETRY
    assert expected_detail in result.detail
    assert article.read_text() == "---\ndeclaration: theorem\n---\n# Result\n"
    assert source.read_text() == "-- existing target module\n"
    assert other.read_text() == "theorem existing : True := by trivial\n"
    assert config.read_text() == "leanprover/lean4:v4.19.0\n"
    assert not (tmp_path / "Unrelated.lean").exists()


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("article_path", "blueprint/roadmap/other.md"),
        ("declaration", "lemma"),
        ("lean_targets", (RuntimeLeanTarget("other", "Main.lean"),)),
        ("statement_dependencies", ("dependency",)),
        ("proof_dependencies", ("dependency",)),
        ("dependencies", ("dependency",)),
    ],
)
def test_proof_success_rejects_changed_target_metadata(
    tmp_path,
    monkeypatch,
    changed_field,
    changed_value,
) -> None:
    original = _node(stated=True, source_file="Main.lean")
    refreshed = replace(_node(stated=True, proved=True, source_file="Main.lean"), **{changed_field: changed_value})
    monkeypatch.setattr("autoform_worker.executor.prove", lambda *args, **kwargs: ProofResult("proved"))
    monkeypatch.setattr(
        "autoform_worker.executor.load_runtime_graph",
        lambda *args, **kwargs: _runtime(refreshed),
    )

    result = ProverExecutor(tmp_path, lambda: FakeAdapter(ProofResult("proved")))(
        WorkItem(original, WorkPhase.PROOF, 1, "revision"), threading.Event()
    )

    assert result.outcome is AttemptOutcome.FAILED
    assert "changed target metadata" in result.detail
    assert changed_field in result.detail


def test_proof_success_rejects_stale_already_proved_work_item(tmp_path, monkeypatch) -> None:
    node = _node(stated=True, proved=True, source_file="Main.lean")
    monkeypatch.setattr("autoform_worker.executor.prove", lambda *args, **kwargs: ProofResult("proved"))

    result = ProverExecutor(tmp_path, lambda: FakeAdapter(ProofResult("proved")))(
        WorkItem(node, WorkPhase.PROOF, 1, "revision"), threading.Event()
    )

    assert result.outcome is AttemptOutcome.FAILED
    assert "already proved before execution" in result.detail


def test_proof_success_requires_authored_false_to_true_transition(tmp_path, monkeypatch) -> None:
    original = _node(stated=True, source_file="Main.lean")
    refreshed = replace(
        _node(stated=True, proved=True, source_file="Main.lean"),
        assertions=RuntimeAssertions(True, False, False),
    )
    monkeypatch.setattr("autoform_worker.executor.prove", lambda *args, **kwargs: ProofResult("proved"))
    monkeypatch.setattr("autoform_worker.executor.load_runtime_graph", lambda *args, **kwargs: _runtime(refreshed))

    result = ProverExecutor(tmp_path, lambda: FakeAdapter(ProofResult("proved")))(
        WorkItem(original, WorkPhase.PROOF, 1, "revision"), threading.Event()
    )

    assert result.outcome is AttemptOutcome.FAILED
    assert "proof_formalized did not transition from false to true" in result.detail


def test_statement_respects_prelaunch_cancellation(tmp_path) -> None:
    adapter = FakeAdapter(ProofResult("proved"))
    cancel = threading.Event()
    cancel.set()

    result = ProverExecutor(tmp_path, lambda: adapter)(
        WorkItem(_node(), WorkPhase.STATEMENT, 1, "revision"), cancel
    )

    assert result.outcome is AttemptOutcome.CANCELLED
    assert adapter.started == []
