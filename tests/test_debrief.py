from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from servers.prover import Event, EventKind, ProofResult, ProverAdapter, Run, debrief
from servers.prover import driver as prover_driver
from servers.prover.claude_adapter import (
    DEBRIEF_EMPTY_MCP_CONFIG,
    DEBRIEF_READ_ONLY_TOOLS,
    ClaudeAdapter,
)
from servers.prover.driver import prove
from servers.prover.verify import VerifyResult
from tests.test_prover_execution import runtime_node


@pytest.fixture(autouse=True)
def _clean_debrief_env(monkeypatch):
    for name in (
        debrief.DEBRIEF_ENV,
        debrief.DEBRIEF_DIR_ENV,
        debrief.DEBRIEF_QUESTION_FILE_ENV,
        debrief.DEBRIEF_BUDGET_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(prover_driver, "capture_baseline", lambda *args, **kwargs: None)


def _records(project: Path) -> list[dict]:
    path = project / ".autoform" / "debriefs" / debrief.RECORDS_FILENAME
    return [json.loads(line) for line in path.read_text().splitlines()]


class DebriefAdapter(ProverAdapter):
    name = "debrief-test"

    def __init__(
        self,
        *,
        status: str = "proved",
        reason: str = "",
        sub_status: str | None = None,
        session_id: str = "session-1",
        response: str | Exception | None = '{"nothing_to_report": true}',
        cancel: threading.Event | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.status = status
        self.reason = reason
        self.sub_status = sub_status
        self.session_id = session_id
        self.response = response
        self.cancel = cancel

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        self.calls.append("start")
        return Run(self.name, goal=spec, project_dir=project_dir)

    def events(self, run: Run):
        yield Event(EventKind.MESSAGE, "worked")
        if self.cancel is not None:
            self.cancel.set()
            yield Event(EventKind.MESSAGE, "cancelled")

    def steer(self, run: Run, message: str) -> None:
        raise AssertionError("must not steer")

    def result(self, run: Run) -> ProofResult:
        meta = {"session_id": self.session_id}
        if self.sub_status is not None:
            meta["sub_status"] = self.sub_status
        return ProofResult(self.status, reason=self.reason, meta=meta)

    def debrief(self, run: Run, question: str, *, budget_seconds: float) -> str | None:
        self.calls.append(f"debrief:{question}")
        if isinstance(self.response, Exception):
            run.meta["debrief_usage"] = {"turns": 0}
            raise self.response
        return self.response


def test_parse_accepts_raw_and_fenced_json_objects() -> None:
    expected = {"friction": [], "nothing_to_report": True}
    encoded = json.dumps(expected)
    assert debrief.parse_debrief_text(encoded) == expected
    assert debrief.parse_debrief_text(f"```json\n{encoded}\n```") == expected
    assert debrief.parse_debrief_text(f"```JSON\n{encoded}\n```") == expected


def test_parse_retains_invalid_or_non_object_answers() -> None:
    invalid = debrief.parse_debrief_text("not JSON")
    assert invalid["raw"] == "not JSON" and invalid["parse_error"]
    assert "not a JSON object" in debrief.parse_debrief_text("[]")["parse_error"]


def test_default_question_is_project_neutral_and_fills_placeholders() -> None:
    question = debrief.build_question("chapter/result", "proved")
    assert "`chapter/result`" in question
    assert "`proved`" in question
    assert "{node_id}" not in question and "{outcome}" not in question
    assert "```" not in question


def test_question_file_overrides_default(monkeypatch, tmp_path: Path) -> None:
    form = tmp_path / "form.md"
    form.write_text('node={node_id} outcome={outcome} {"keep": "braces"}')
    monkeypatch.setenv(debrief.DEBRIEF_QUESTION_FILE_ENV, str(form))
    assert debrief.build_question("a/b", "FAILED: x") == 'node=a/b outcome=FAILED: x {"keep": "braces"}'


def test_debrief_dir_defaults_into_project_and_resolves_relative_overrides(
    monkeypatch, tmp_path: Path
) -> None:
    assert debrief.debrief_dir(tmp_path) == tmp_path / ".autoform" / "debriefs"
    monkeypatch.setenv(debrief.DEBRIEF_DIR_ENV, "feedback")
    assert debrief.debrief_dir(tmp_path) == tmp_path / "feedback"
    monkeypatch.setenv(debrief.DEBRIEF_DIR_ENV, str(tmp_path / "elsewhere"))
    assert debrief.debrief_dir("/unused") == tmp_path / "elsewhere"


def test_disabled_by_default(tmp_path: Path) -> None:
    adapter = DebriefAdapter()
    prove(adapter, runtime_node(), "prove True", str(tmp_path), verifier=None)
    assert not any(call.startswith("debrief:") for call in adapter.calls)
    assert not (tmp_path / ".autoform").exists()


@pytest.mark.parametrize(
    ("status", "reason", "gate", "expected"),
    [
        ("proved", "", VerifyResult(True), "proved"),
        ("proved", "", VerifyResult(False, reason="declaration changed"),
         "gate rejected: declaration changed"),
        ("failed", "missing lemma", None, "FAILED: missing lemma"),
    ],
)
def test_runs_once_after_verdict_and_writes_record_and_report(
    monkeypatch, tmp_path: Path, status, reason, gate, expected
) -> None:
    monkeypatch.setenv(debrief.DEBRIEF_ENV, "1")
    adapter = DebriefAdapter(status=status, reason=reason)
    result = prove(
        adapter,
        runtime_node(),
        "prove True",
        str(tmp_path),
        verifier=(lambda *args, **kwargs: gate) if gate is not None else None,
        max_gate_folds=0,
    )

    debriefs = [call for call in adapter.calls if call.startswith("debrief:")]
    assert len(debriefs) == 1 and f"`{expected}`" in debriefs[0]
    assert result.status == ("failed" if gate is not None and not gate.ok else status)

    [record] = _records(tmp_path)
    assert record["node_id"] == "chapter/result"
    assert record["outcome"] == expected
    assert record["backend"] == "debrief-test"
    assert record["debrief"] == {"nothing_to_report": True}
    [report] = (tmp_path / ".autoform" / "debriefs" / "reports").iterdir()
    assert report.name.startswith("result--")
    assert "# Proof debrief: chapter/result" in report.read_text()


def test_skips_cancelled_sessionless_and_unsupported_backends(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(debrief.DEBRIEF_ENV, "1")
    cancel = threading.Event()
    cancelled = DebriefAdapter(cancel=cancel)
    prove(cancelled, runtime_node(), "p", str(tmp_path), verifier=None, cancel_event=cancel)
    sessionless = DebriefAdapter(session_id="")
    prove(sessionless, runtime_node(), "p", str(tmp_path), verifier=None)
    unsupported = DebriefAdapter(response=None)
    prove(unsupported, runtime_node(), "p", str(tmp_path), verifier=None)

    assert not any(c.startswith("debrief:") for c in cancelled.calls + sessionless.calls)
    assert any(c.startswith("debrief:") for c in unsupported.calls)
    assert not (tmp_path / ".autoform").exists()


def test_records_unparseable_answers_and_errors_without_changing_verdict(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(debrief.DEBRIEF_ENV, "1")
    prove(DebriefAdapter(response="not JSON"), runtime_node(), "p", str(tmp_path), verifier=None)
    result = prove(
        DebriefAdapter(response=RuntimeError("resume failed")),
        runtime_node(),
        "p",
        str(tmp_path),
        verifier=None,
    )

    first, second = _records(tmp_path)
    assert first["debrief"]["raw"] == "not JSON" and first["debrief"]["parse_error"]
    assert second["debrief"] == {"error": "RuntimeError: resume failed", "usage": {"turns": 0}}
    assert result.status == "proved"


def test_missing_question_file_is_recorded_not_raised(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(debrief.DEBRIEF_ENV, "1")
    monkeypatch.setenv(debrief.DEBRIEF_QUESTION_FILE_ENV, str(tmp_path / "missing.md"))
    result = prove(DebriefAdapter(), runtime_node(), "p", str(tmp_path), verifier=None)
    assert result.status == "proved"
    assert _records(tmp_path)[0]["debrief"]["error"].startswith("FileNotFoundError")


def test_render_reports_rebuilds_index_and_shows_custom_fields(tmp_path: Path) -> None:
    debrief.write_record(
        tmp_path,
        {
            "node_id": "chapter/result",
            "outcome": "proved",
            "run_id": "abc",
            "recorded_at": "20261001T000000Z",
            "debrief": {
                "friction": [{"what": "unfold", "file": "A.lean", "line": 3, "lines": 6}],
                "infrastructure_proposals": [
                    {"kind": "helper-lemma", "name": "foo", "what": "bar", "evidence": "friction 1"}
                ],
                "tactics_tried": [{"tactic": "my_tac", "outcome": "closed"}],
            },
        },
    )
    [report] = debrief.render_reports(tmp_path)
    text = report.read_text()
    assert "- `foo` (helper-lemma): bar" in text
    assert "### `tactics_tried`" in text and "my_tac" in text
    assert f"(reports/{report.name})" in (tmp_path / "README.md").read_text()


def _claude_runner(calls: list[tuple[list[str], float | None]]):
    def runner(args, env, cwd, deadline):
        calls.append((list(args), deadline))
        if len(calls) == 1:
            text, usage, cost = "completed", 10, 0.25
        else:
            text, usage, cost = '{"nothing_to_report": true}', 4, 0.125
        return iter(
            [
                json.dumps({"type": "system", "subtype": "init", "session_id": "sid-1"}),
                json.dumps(
                    {
                        "type": "result",
                        "session_id": "sid-1",
                        "result": text,
                        "usage": {"input_tokens": usage, "output_tokens": usage},
                        "total_cost_usd": cost,
                    }
                ),
            ]
        )

    return runner


def test_claude_debrief_is_read_only_and_keeps_verdict_and_usage_separate(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(debrief.DEBRIEF_ENV, "1")
    calls: list[tuple[list[str], float | None]] = []
    adapter = ClaudeAdapter(
        mcp_config="fake-mcp.json",
        runner=_claude_runner(calls),
        max_wait_seconds=20,
        extra_args=["--allowedTools", "Edit,Write,Bash(*)"],
    )

    result = prove(adapter, runtime_node(), "prove True", str(tmp_path), verifier=None)

    assert result.status == "proved"
    assert result.proof_text == "completed"
    assert result.meta["usage"]["worker"]["input_tokens"] == 10
    assert result.meta["usage"]["worker"]["turns"] == 1
    assert len(calls) == 2
    (first, first_deadline), (second, second_deadline) = calls
    assert "--resume" not in first
    assert second[second.index("--resume") + 1] == "sid-1"
    allowed = [second[i + 1] for i, arg in enumerate(second) if arg == "--allowedTools"]
    available = [second[i + 1] for i, arg in enumerate(second) if arg == "--tools"]
    assert allowed == available == [DEBRIEF_READ_ONLY_TOOLS]
    assert second[second.index("--mcp-config") + 1] == DEBRIEF_EMPTY_MCP_CONFIG
    assert "fake-mcp.json" not in second
    assert second_deadline > first_deadline

    [record] = _records(tmp_path)
    assert record["debrief"]["nothing_to_report"] is True
    assert record["debrief"]["usage"]["input_tokens"] == 4
    assert record["debrief"]["usage"]["cost_usd"] == 0.125
    assert record["debrief"]["usage"]["turns"] == 1


def test_claude_debrief_resumes_a_timed_out_session(tmp_path: Path) -> None:
    calls: list[tuple[list[str], float | None]] = []
    adapter = ClaudeAdapter(mcp_config="", runner=_claude_runner(calls), max_wait_seconds=20)
    run = adapter.start("chapter/result", "prove True", str(tmp_path))
    run.handle.session_id = "sid-timeout"
    run.handle.timed_out = True
    run.handle.started = True
    calls.append(([], None))  # the runner answers the debrief on its second call

    assert adapter.debrief(run, "form", budget_seconds=5) == '{"nothing_to_report": true}'
    assert calls[1][0][calls[1][0].index("--resume") + 1] == "sid-timeout"
    assert run.handle.timed_out is True
    assert run.handle.final_text == ""


def test_statement_gate_ignores_debrief_output(tmp_path: Path) -> None:
    from autoform_worker.executor import _capture_statement_baseline

    (tmp_path / "Main.lean").write_text("theorem t : True := trivial\n")
    debrief.write_record(debrief.debrief_dir(tmp_path), {"node_id": "a/b", "debrief": {}})
    assert set(_capture_statement_baseline(tmp_path).files) == {"Main.lean"}
