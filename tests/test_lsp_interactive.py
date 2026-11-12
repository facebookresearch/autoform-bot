"""Tests for the local interactive LSP tools: goals, buffer checks, and tactic attempts."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from servers.lean_runtime import LeanRuntimeConfig, LeanRuntimeServices
from servers.lsp import interactive
from servers.lsp import server as lsp
from tests.test_lsp_protocol import _real_lean_project, _requires_real_lean
from tests.test_shared_lean_runtime import FakeLsp, FakePool, make_lake_project, runtime_config


def _session(**kwargs) -> interactive.InteractiveLeanLspSession:
    return interactive.InteractiveLeanLspSession(lsp.LspConfig(), **kwargs)


def _publication(uri, diagnostics, version=None):
    params = {"uri": uri, "diagnostics": diagnostics}
    if version is not None:
        params["version"] = version
    return {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": params}


def _progress(uri, processing, version):
    return {
        "jsonrpc": "2.0",
        "method": "$/lean/fileProgress",
        "params": {"textDocument": {"uri": uri, "version": version}, "processing": processing},
    }


def _feed(session, monkeypatch, messages, clock, *, step=None):
    """Serve frames to the collector as the real reader would, recording notifications."""
    frames = iter(messages)

    def pump(timeout):
        clock["now"] += timeout if step is None else step
        message = next(frames, None)
        if message is not None and "method" in message:
            session._record_notification(message)
        return message

    monkeypatch.setattr(interactive.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(session, "_pump", pump)


def _quiet(session, monkeypatch):
    """Stub the stream: every notification succeeds and every version is clean."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        session, "_send_notification", lambda method, params, **kwargs: sent.append((method, params))
    )
    monkeypatch.setattr(session, "_collect_diagnostics", lambda uri, version, timeout: [])
    return sent


# --------------------------------------------------------------------------- #
# Diagnostics collection
# --------------------------------------------------------------------------- #


def test_diagnostics_wait_for_file_progress_completion(monkeypatch):
    session = _session()
    uri = "file:///tmp/Test.lean"
    _feed(
        session,
        monkeypatch,
        [
            _publication(uri, [{"message": "Replayed Foo.Bar"}], 2),  # Lake setup phase
            _publication(uri, [], 2),  # setup phase cleared
            _progress(uri, [{"kind": 1}], 2),  # elaboration starts
            _publication(uri, [{"message": "unsolved goals"}], 2),  # the real result
            _progress(uri, [], 2),  # elaboration done
        ],
        {"now": 0.0},
        step=0.1,
    )

    # The empty publication ending Lake's setup phase is not evidence that the
    # file is clean: only progress reporting the file finished is.
    assert session._collect_diagnostics(uri, 2, timeout=60) == [{"message": "unsolved goals"}]


def test_diagnostics_ignore_a_superseded_version(monkeypatch):
    session = _session()
    uri = "file:///tmp/Test.lean"
    _feed(
        session,
        monkeypatch,
        [
            # Still in flight for the previous edit when version 3 was sent.
            _publication(uri, [], 2),
            _progress(uri, [], 2),
            _progress(uri, [{"kind": 1}], 3),
            _publication(uri, [{"message": "unsolved goals"}], 3),
            _progress(uri, [], 3),
        ],
        {"now": 0.0},
        step=0.1,
    )

    assert session._collect_diagnostics(uri, 3, timeout=60) == [{"message": "unsolved goals"}]


def test_diagnostics_quiet_fallback_without_file_progress(monkeypatch):
    session = _session()
    uri = "file:///tmp/Test.lean"
    _feed(session, monkeypatch, [None, None, _publication(uri, [])], {"now": 0.0}, step=0.5)

    # An explicit empty publication followed by silence is a clean result from
    # a server that never reports progress for the URI.
    assert session._collect_diagnostics(uri, 2, timeout=60) == []


def test_deadline_without_publication_is_still_elaborating(monkeypatch):
    session = _session()
    clock = {"now": 0.0}
    _feed(session, monkeypatch, [], clock)

    with pytest.raises(interactive.LspStillElaborating, match="has not finished elaborating"):
        session._collect_diagnostics("file:///tmp/Test.lean", 2, timeout=5)

    assert clock["now"] == 5


def test_deadline_mid_elaboration_is_never_a_clean_result(monkeypatch):
    session = _session()
    uri = "file:///tmp/Test.lean"
    _feed(
        session,
        monkeypatch,
        [
            _progress(uri, [{"kind": 1}], 2),
            # Lake's setup phase ends with an empty publication; elaboration
            # then runs past the deadline without reporting completion.
            _publication(uri, [], 2),
        ],
        {"now": 0.0},
    )

    with pytest.raises(interactive.LspStillElaborating, match="not a clean file"):
        session._collect_diagnostics(uri, 2, timeout=60)


def test_malformed_diagnostics_payload_is_protocol_error():
    session = _session()

    with pytest.raises(lsp.LspProtocolError, match="diagnostics must be a list"):
        session._record_notification(_publication("file:///tmp/Test.lean", {"not": "a list"}))


def test_notifications_read_during_upstream_hover_are_recorded(monkeypatch):
    session = _session()
    uri = "file:///tmp/Test.lean"
    frames = iter([_publication(uri, [{"message": "boom"}], 7), {"jsonrpc": "2.0", "id": 1, "result": "ok"}])
    monkeypatch.setattr(lsp.LeanLspSession, "_read_message", lambda self, timeout=5, finish_by=None: next(frames, None))

    # Upstream's response reader skips notifications; ours records them first.
    assert session._read_response(1) == "ok"
    assert session._uri_state[uri].diagnostics[7] == [{"message": "boom"}]


def test_pump_reads_a_frame_that_straddles_its_poll_budget():
    read_fd, write_fd = os.pipe()
    session = _session()
    session.process = SimpleNamespace(stdout=SimpleNamespace(fileno=lambda: read_fd))
    body = json.dumps(_publication("file:///tmp/Test.lean", [], 2)).encode()
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    os.write(write_fd, frame[:10])

    def finish():
        time.sleep(0.3)
        os.write(write_fd, frame[10:])

    writer = threading.Thread(target=finish)
    writer.start()
    try:
        # Abandoning the frame at the poll deadline would desynchronise the stream.
        message = session._pump(timeout=0.05)
    finally:
        writer.join()
        os.close(read_fd)
        os.close(write_fd)
    assert message["method"] == "textDocument/publishDiagnostics"


# --------------------------------------------------------------------------- #
# Document synchronisation
# --------------------------------------------------------------------------- #


def test_second_call_edits_the_open_document_instead_of_reopening(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("example : True := by trivial\n")
    session = _session()
    sent = _quiet(session, monkeypatch)
    monkeypatch.setattr(session, "_request", lambda *args, **kwargs: {"goals": []})

    session.goal(str(source), 0, 3)
    source.write_text("example : True := by exact trivial\n")
    session.goal(str(source), 0, 3)

    assert [method for method, _ in sent] == ["textDocument/didOpen", "textDocument/didChange"]
    # Goals are always read against the file as it is on disk.
    assert sent[1][1]["contentChanges"] == [{"text": "example : True := by exact trivial\n"}]
    first, second = (params["textDocument"]["version"] for _, params in sent)
    # Upstream hover always opens at version 1, so ours never use it.
    assert 1 < first < second


def test_document_residency_is_bounded(tmp_path, monkeypatch):
    first = tmp_path / "First.lean"
    second = tmp_path / "Second.lean"
    first.write_text("#check Nat\n")
    second.write_text("#check Int\n")
    session = _session(max_open_documents=1)
    sent = _quiet(session, monkeypatch)

    session.check_buffer(str(first), "#check Nat\n", owner="a")
    session.check_buffer(str(second), "#check Int\n", owner="a")

    # Each check puts the disk text back before returning.
    assert [(method, params["textDocument"]["uri"]) for method, params in sent] == [
        ("textDocument/didOpen", first.resolve().as_uri()),
        ("textDocument/didChange", first.resolve().as_uri()),
        ("textDocument/didClose", first.resolve().as_uri()),
        ("textDocument/didOpen", second.resolve().as_uri()),
        ("textDocument/didChange", second.resolve().as_uri()),
    ]
    assert list(session._open) == [second.resolve().as_uri()]


def test_hover_releases_an_open_document_before_upstream_reopens_it(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = _session()
    sent = _quiet(session, monkeypatch)
    session.check_buffer(str(source), "#check Int\n", owner="a")
    monkeypatch.setattr(session, "_send_request", lambda method, params, timeout=30, finish_by=None: {"contents": "Nat : Type"})

    assert session.hover(str(source), 0, 7) == "Nat : Type"

    # Lean must never see a second didOpen for a document it already holds.
    assert [method for method, _ in sent] == [
        "textDocument/didOpen",
        "textDocument/didChange",
        "textDocument/didClose",
        "textDocument/didOpen",
        "textDocument/didClose",
    ]
    assert sent[3][1]["textDocument"]["version"] == 1
    assert session._open == {}


# --------------------------------------------------------------------------- #
# Goals
# --------------------------------------------------------------------------- #


def test_goal_distinguishes_no_tactic_block_from_no_goals(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("example : True := by trivial\n")
    session = _session()
    _quiet(session, monkeypatch)

    monkeypatch.setattr(session, "_request", lambda *args, **kwargs: None)
    assert session.goal(str(source), 0, 3)["state"] == "no_tactic_goal"

    monkeypatch.setattr(session, "_request", lambda *args, **kwargs: {"goals": []})
    assert session.goal(str(source), 0, 3)["state"] == "closed"

    monkeypatch.setattr(session, "_request", lambda *args, **kwargs: {"goals": ["⊢ True"], "rendered": "⊢ True"})
    result = session.goal(str(source), 0, 3)
    assert result["state"] == "open"
    assert interactive.format_goal(result) == "⊢ True"


def test_goal_retries_once_when_superseded(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("example : True := by trivial\n")
    session = _session()
    _quiet(session, monkeypatch)
    attempts = []

    def request(method, params, *, timeout):
        attempts.append(method)
        if len(attempts) == 1:
            raise interactive.LspRequestFailed(interactive.LSP_CONTENT_MODIFIED, "File changed.")
        return {"goals": ["⊢ True"]}

    monkeypatch.setattr(session, "_request", request)

    assert session.goal(str(source), 0, 3)["state"] == "open"
    assert attempts == ["$/lean/plainGoal", "$/lean/plainGoal"]
    assert session._retire_pending is False


def test_goal_drops_the_document_when_the_worker_exits(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("example : True := by trivial\n")
    session = _session()
    _quiet(session, monkeypatch)

    def request(method, params, *, timeout):
        raise interactive.LspRequestFailed(interactive.LSP_WORKER_EXITED, "worker exited")

    monkeypatch.setattr(session, "_request", request)

    assert session.goal(str(source), 0, 3)["state"] == "worker_exited"
    # The next call must reopen rather than edit a version the worker never saw.
    assert session._open == {}


def test_goal_rejects_a_position_outside_the_file_without_a_round_trip(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("example : True := by trivial\n")
    session = _session()
    sent = _quiet(session, monkeypatch)

    result = session.goal(str(source), 99, 0)

    assert result["state"] == "invalid_position"
    assert interactive.format_goal(result).startswith("Could not read a goal (invalid_position): line 99")
    assert sent == []


def test_request_error_responses_stay_request_scoped():
    with pytest.raises(interactive.LspRequestFailed, match="File changed") as caught:
        interactive._unwrap_response(1, {"id": 1, "error": {"code": -32801, "message": "File changed."}})
    assert caught.value.code == -32801
    with pytest.raises(lsp.LspProtocolError, match="malformed error"):
        interactive._unwrap_response(1, {"id": 1, "error": "not an object"})


# --------------------------------------------------------------------------- #
# Still elaborating
# --------------------------------------------------------------------------- #


def test_still_elaborating_keeps_the_session_and_its_document(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("example : True := by trivial\n")
    session = _session()
    sent = _quiet(session, monkeypatch)

    def elaborating(uri, version, timeout):
        raise interactive.LspStillElaborating("Lean has not finished elaborating")

    monkeypatch.setattr(session, "_collect_diagnostics", elaborating)

    with pytest.raises(interactive.LspStillElaborating):
        session.goal(str(source), 0, 3)

    # The document stays open and the session in service, so a repeat call
    # edits the same document and keeps waiting on the same elaboration.
    assert session._retire_pending is False
    assert source.resolve().as_uri() in session._open
    with pytest.raises(interactive.LspStillElaborating):
        session.goal(str(source), 0, 3)
    assert [method for method, _ in sent] == ["textDocument/didOpen", "textDocument/didChange"]


def test_a_protocol_failure_still_retires_the_session(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("example : True := by trivial\n")
    session = _session()
    _quiet(session, monkeypatch)

    def broken(uri, version, timeout):
        raise lsp.LspProtocolError("stream desync")

    monkeypatch.setattr(session, "_collect_diagnostics", broken)

    with pytest.raises(lsp.LspProtocolError):
        session.goal(str(source), 0, 3)
    assert session._retire_pending is True


# --------------------------------------------------------------------------- #
# Buffers and tactic attempts
# --------------------------------------------------------------------------- #


def test_check_buffer_never_writes_to_disk_and_reads_differently(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    on_disk = "example : True := by trivial\n"
    source.write_text(on_disk)
    session = _session()
    sent = _quiet(session, monkeypatch)

    candidate = "example : True := by grind\n"
    result = session.check_buffer(str(source), candidate, owner="worker-1")

    assert source.read_text() == on_disk
    assert sent[0][1]["textDocument"]["text"] == candidate
    assert result["buffer_sha256"] != result["disk_sha256"]
    rendered = interactive.format_buffer_check(result)
    assert "NOT written to disk" in rendered
    assert "compiles cleanly" not in rendered


def _hold_a_buffer(session, source, monkeypatch, owner):
    """Leave ``owner``'s buffer in place, as a check still elaborating at its deadline does."""

    def elaborating(uri, version, timeout):
        raise interactive.LspStillElaborating("Lean has not finished elaborating")

    with monkeypatch.context() as patch:
        patch.setattr(session, "_collect_diagnostics", elaborating)
        with pytest.raises(interactive.LspStillElaborating):
            session.check_buffer(str(source), "-- held\n", owner=owner)


def test_check_buffer_refuses_to_overwrite_another_callers_buffer(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("example : True := by trivial\n")
    session = _session()
    _quiet(session, monkeypatch)

    _hold_a_buffer(session, source, monkeypatch, "worker-1")

    with pytest.raises(interactive.LspBufferOwned, match="worker-1"):
        session.check_buffer(str(source), "-- second\n", owner="worker-2")
    with pytest.raises(interactive.LspBufferOwned, match="worker-1"):
        session.try_tactics(str(source), 0, ["rfl"], "worker-2", per_candidate_timeout=5, total_timeout=60)
    assert session._retire_pending is False


def test_check_buffer_puts_the_disk_text_back(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    on_disk = "example : True := by trivial\n"
    source.write_text(on_disk)
    session = _session()
    sent = _quiet(session, monkeypatch)

    session.check_buffer(str(source), "-- speculative\n", owner="worker-1")

    assert sent[-1][1]["contentChanges"] == [{"text": on_disk}]
    document = session._open[source.resolve().as_uri()]
    assert document.dirty is False and document.owner is None


def test_goal_and_hover_refuse_a_buffer_another_caller_holds(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("example : True := by trivial\n")
    session = _session()
    sent = _quiet(session, monkeypatch)
    _hold_a_buffer(session, source, monkeypatch, "worker-1")
    held = len(sent)

    with pytest.raises(interactive.LspBufferOwned, match="worker-1"):
        session.goal(str(source), 0, 3)
    with pytest.raises(interactive.LspBufferOwned, match="worker-1"):
        session.hover(str(source), 0, 3)
    assert len(sent) == held
    assert session._retire_pending is False


def test_try_tactics_reads_a_crlf_file_line_by_line(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_bytes(b"theorem t : True := by\r\n  sorry\r\n")
    session = _session()
    sent = _quiet(session, monkeypatch)
    monkeypatch.setattr(session, "_collect_diagnostics", lambda uri, version, timeout: [])

    result = session.try_tactics(str(source), 1, ["trivial"], "worker-1", per_candidate_timeout=5, total_timeout=60)

    assert result["replaced"] == "  sorry"
    # The original text first, to count its holes, then the candidate.
    assert sent[0][1]["textDocument"]["text"] == "theorem t : True := by\n  sorry\n"
    assert sent[1][1]["contentChanges"] == [{"text": "theorem t : True := by\n  trivial\n"}]


def test_try_tactics_classifies_each_candidate_and_restores_the_buffer(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    base = "theorem t : True := by\n  sorry\n"
    source.write_text(base)
    session = _session()
    sent = _quiet(session, monkeypatch)
    answers = {
        "sorry": [{"severity": 2, "message": "declaration uses `sorry`"}],
        "grind": [],
        "aesop": [{"severity": 1, "message": "unsolved goals"}],
        "omega": [{"severity": 2, "message": "declaration uses `sorry`"}],
    }

    def text(params):
        return params["contentChanges"][0]["text"] if "contentChanges" in params else params["textDocument"]["text"]

    def collect(uri, version, timeout):
        last = text(sent[-1][1])
        return next(diagnostics for tactic, diagnostics in answers.items() if f"  {tactic}\n" in last)

    monkeypatch.setattr(session, "_collect_diagnostics", collect)

    result = session.try_tactics(
        str(source), 1, ["grind", "aesop", "omega"], owner="worker-1", per_candidate_timeout=5, total_timeout=60
    )

    outcomes = {item["tactic"]: item["outcome"] for item in result["attempts"]}
    assert outcomes == {"grind": "closed", "aesop": "error", "omega": "incomplete"}
    # The replaced line keeps its indentation.
    assert text(sent[1][1]) == "theorem t : True := by\n  grind\n"
    assert text(sent[-1][1]) == base
    assert source.read_text() == base
    rendered = interactive.format_tactic_attempts(result)
    assert rendered.splitlines()[0] == "Tactic attempts at line 1 — buffer only, NOT written to disk."
    assert "Closed with no errors and no sorry left in the edited declaration: grind" in rendered


@pytest.mark.parametrize(
    ("original", "outcomes"),
    [(2, {"omega": "closed", "sorry": "incomplete"}), (None, {"omega": "incomplete", "sorry": "incomplete"})],
    ids=["counted", "still-elaborating"],
)
def test_try_tactics_does_not_count_a_hole_in_another_declaration(tmp_path, monkeypatch, original, outcomes):
    # omega closes theorem a's only hole; theorem b's still warns. When the original text has not finished
    # elaborating, its holes are unknown, so any warning reads as a hole left in the edited declaration.
    source = tmp_path / "Test.lean"
    source.write_text("theorem a (n : Nat) : n + 0 = n := by\n  sorry\n\ntheorem b : True := by\n  sorry\n")
    session = _session()
    sent = _quiet(session, monkeypatch)
    hole = {"severity": 2, "message": "declaration uses `sorry`"}

    def collect(uri, version, timeout):
        last = sent[-1][1]
        if "textDocument" in last and "text" in last["textDocument"]:
            if original is None:
                raise interactive.LspStillElaborating("the original text ran long")
            return [hole, hole]
        return [hole] if "  omega\n" in last["contentChanges"][0]["text"] else [hole, hole]

    monkeypatch.setattr(session, "_collect_diagnostics", collect)

    result = session.try_tactics(
        str(source), 1, ["omega", "sorry"], owner="worker-1", per_candidate_timeout=5, total_timeout=60
    )

    assert {item["tactic"]: item["outcome"] for item in result["attempts"]} == outcomes
    rendered = interactive.format_tactic_attempts(result)
    if original is None:
        assert "None of these closed it." in rendered
    else:
        assert result["attempts"][0]["detail"] == ""
        assert "Closed with no errors and no sorry left in the edited declaration: omega" in rendered


def test_try_tactics_restores_the_buffer_when_a_candidate_raises(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    base = "theorem t : True := by\n  sorry\n"
    source.write_text(base)
    session = _session()
    sent = _quiet(session, monkeypatch)
    calls = []

    def explode(uri, version, timeout):
        calls.append(version)
        # The original text elaborates; the candidate after it does not.
        if len(calls) > 1:
            raise lsp.LspProtocolError("stream desync")
        return []

    monkeypatch.setattr(session, "_collect_diagnostics", explode)

    with pytest.raises(lsp.LspProtocolError):
        session.try_tactics(str(source), 1, ["grind"], owner="worker-1", per_candidate_timeout=5, total_timeout=60)

    assert sent[-1][1]["contentChanges"] == [{"text": base}]
    assert session._retire_pending is True


def test_try_tactics_records_a_slow_candidate_without_retiring_the_session(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("theorem t : True := by\n  sorry\n")
    session = _session()
    _quiet(session, monkeypatch)
    calls = []

    def collect(uri, version, timeout):
        calls.append(timeout)
        # The first call is the original text; the second, the first candidate.
        if len(calls) == 2:
            raise interactive.LspStillElaborating("candidate ran long")
        return []

    monkeypatch.setattr(session, "_collect_diagnostics", collect)

    result = session.try_tactics(
        str(source), 1, ["grind", "omega"], owner="worker-1", per_candidate_timeout=5, total_timeout=60
    )

    assert [item["outcome"] for item in result["attempts"]] == ["timeout", "closed"]
    assert result["attempts"][0]["detail"] == "no result within 5s"
    assert session._retire_pending is False


def test_try_tactics_reports_a_line_outside_the_file(tmp_path):
    source = tmp_path / "Test.lean"
    source.write_text("theorem t : True := by\n  sorry\n")

    result = _session().try_tactics(str(source), 5, ["rfl"], "a", per_candidate_timeout=5, total_timeout=60)

    assert interactive.format_tactic_attempts(result) == "Could not try tactics: line 5 is outside a 2-line file"


def test_try_tactics_total_timeout_bounds_the_wait_to_enter_the_session(tmp_path):
    source = tmp_path / "Test.lean"
    source.write_text("theorem t : True := by\n  sorry\n")
    session = _session()
    session._operation_lock.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(lsp.LspBusyError, match="0.2s"):
            session.try_tactics(str(source), 1, ["rfl"], "a", per_candidate_timeout=5, total_timeout=0.2)
        assert time.monotonic() - started < 5
    finally:
        session._operation_lock.release()


# --------------------------------------------------------------------------- #
# MCP adapter: toolset and closer battery (fixed)
# --------------------------------------------------------------------------- #


class _RecordingRuntime:
    def __init__(self):
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        return "delegated"


def _tools(monkeypatch, **env):
    monkeypatch.delenv(interactive.TOOLSET_ENV, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    runtime = _RecordingRuntime()
    server = lsp.create_lsp_server(runtime)
    return server, runtime, {tool.name: tool for tool in asyncio.run(server.list_tools())}


def test_baseline_toolset_exposes_only_diagnostics_and_hover(monkeypatch):
    _, _, tools = _tools(monkeypatch, AUTOFORM_LSP_TOOLSET="baseline")

    assert set(tools) == set(interactive.BASELINE_TOOLS) == {"lean_diagnostic_messages", "lean_hover"}


def test_an_unknown_toolset_fails_loudly(monkeypatch):
    with pytest.raises(ValueError, match="unknown AUTOFORM_LSP_TOOLSET"):
        _tools(monkeypatch, AUTOFORM_LSP_TOOLSET="baselien")


def test_interactive_tools_delegate_to_the_runtime(monkeypatch):
    server, runtime, _ = _tools(monkeypatch)
    where = {"project_dir": "/lean", "file_path": "Main.lean"}

    asyncio.run(server.call_tool("lean_goal", {**where, "line": 1, "character": 2}))
    asyncio.run(server.call_tool("lean_check_buffer", {**where, "content": "", "owner": "me"}))
    asyncio.run(server.call_tool("lean_try_tactics", {**where, "line": 1, "tactics": ["rfl"], "owner": "me"}))
    asyncio.run(server.call_tool("lean_try_close", {**where, "line": 1, "owner": "me"}))

    assert runtime.calls == [
        ("lsp.goal", {**where, "line": 1, "character": 2}),
        ("lsp.check_buffer", {**where, "content": "", "owner": "me"}),
        ("lsp.try_tactics", {**where, "line": 1, "tactics": ["rfl"], "owner": "me"}),
        (
            "lsp.try_tactics",
            {
                **where,
                "line": 1,
                "tactics": list(interactive.CLOSER_BATTERY),
                "owner": "me",
                "per_candidate_timeout": 8.0,
            },
        ),
    ]


def test_closer_battery_is_the_generic_closers(monkeypatch):
    server, runtime, tools = _tools(monkeypatch)
    assert "aesop, omega, positivity, linarith" in tools["lean_try_close"].description

    where = {"project_dir": "/lean", "file_path": "M.lean"}
    asyncio.run(server.call_tool("lean_try_close", {**where, "line": 0, "owner": "me"}))
    assert runtime.calls[0][1]["tactics"] == ["aesop", "omega", "positivity", "linarith"]


# --------------------------------------------------------------------------- #
# Runtime dispatch
# --------------------------------------------------------------------------- #


def _services(session_class, **config):
    return LeanRuntimeServices(
        runtime_config(**config),
        repl_factory=FakePool,
        lsp_factory=session_class,
        start_sweepers=False,
    )


def test_still_elaborating_keeps_the_warm_runtime_session(tmp_path):
    project = make_lake_project(tmp_path, "lsp-elaborating")
    (project / "Main.lean").write_text("theorem t : True := by\n  trivial\n")
    sessions = []

    class Session(FakeLsp):
        def __init__(self, root):
            super().__init__(root)
            self.calls = 0
            sessions.append(self)

        def goal(self, file_path, line, character):
            self.calls += 1
            if self.calls == 1:
                raise interactive.LspStillElaborating("Lean has not finished elaborating")
            return {"state": "closed", "goals": []}

    services = _services(Session)
    try:
        params = {"project_dir": str(project), "file_path": "Main.lean", "line": 1, "character": 2}
        with pytest.raises(interactive.LspStillElaborating):
            services.dispatch("lsp.goal", params)
        assert services.dispatch("lsp.goal", params) == "No goals remaining at this position."
        assert len(sessions) == 1 and sessions[0].closed is False
    finally:
        services.close()


def test_try_tactics_dispatch_derives_and_clamps_its_budgets(tmp_path):
    project = make_lake_project(tmp_path, "lsp-tactics")
    (project / "Main.lean").write_text("theorem t : True := by\n  sorry\n")
    calls = []

    class Session(FakeLsp):
        def try_tactics(self, file_path, line, tactics, owner, *, per_candidate_timeout, total_timeout):
            calls.append((Path(file_path).name, line, tactics, owner, per_candidate_timeout, total_timeout))
            return {"line": line, "replaced": "  sorry", "attempts": [{"tactic": "rfl", "outcome": "closed"}]}

    services = _services(Session)
    try:
        params = {"project_dir": str(project), "file_path": "Main.lean", "line": 1, "owner": "me"}
        rendered = services.dispatch("lsp.try_tactics", {**params, "tactics": ["rfl", "trivial"]})
        services.dispatch("lsp.try_tactics", {**params, "tactics": ["rfl"], "per_candidate_timeout": 8})
        services.dispatch("lsp.try_tactics", {**params, "tactics": ["rfl"], "total_timeout": 10_000})
        for bad in ([], ["rfl"] * 9, [""], ["x" * 2001], "rfl"):
            with pytest.raises(ValueError, match="tactics"):
                services.dispatch("lsp.try_tactics", {**params, "tactics": bad})
        with pytest.raises(ValueError, match="per_candidate_timeout"):
            services.dispatch("lsp.try_tactics", {**params, "tactics": ["rfl"], "per_candidate_timeout": 0})
    finally:
        services.close()

    assert "closed     rfl" in rendered
    assert calls == [
        ("Main.lean", 1, ["rfl", "trivial"], "me", 30.0, 120.0),
        ("Main.lean", 1, ["rfl"], "me", 8.0, 68.0),
        # Clamped to AUTOFORM_MAX_LSP_REQUEST_SECONDS.
        ("Main.lean", 1, ["rfl"], "me", 30.0, 600.0),
    ]


def test_check_buffer_dispatch_formats_a_buffer_result(tmp_path):
    project = make_lake_project(tmp_path, "lsp-buffer")
    (project / "Main.lean").write_text("#check Nat\n")

    class Session(FakeLsp):
        def check_buffer(self, file_path, text, owner):
            if owner == "worker-2":
                raise interactive.LspBufferOwned("held by worker-1")
            return {"diagnostics": [], "buffer_sha256": "b", "disk_sha256": "d"}

    services = _services(Session)
    try:
        params = {"project_dir": str(project), "file_path": "Main.lean", "content": ""}
        assert "NOT written to disk" in services.dispatch("lsp.check_buffer", {**params, "owner": "worker-1"})
        with pytest.raises(interactive.LspBufferOwned):
            services.dispatch("lsp.check_buffer", {**params, "owner": "worker-2"})
    finally:
        services.close()


def test_max_open_documents_is_node_configuration(monkeypatch):
    assert LeanRuntimeConfig.from_environment().lsp_max_open_documents == 1
    monkeypatch.setenv("AUTOFORM_LSP_MAX_OPEN_DOCS", "3")
    config = LeanRuntimeConfig.from_environment()
    assert config.lsp_max_open_documents == 3
    assert config.as_dict()["lsp_max_open_documents"] == 3
    monkeypatch.setenv("AUTOFORM_LSP_MAX_OPEN_DOCS", "0")
    with pytest.raises(ValueError, match="AUTOFORM_LSP_MAX_OPEN_DOCS"):
        LeanRuntimeConfig.from_environment()


# --------------------------------------------------------------------------- #
# Real Lean
# --------------------------------------------------------------------------- #


@_requires_real_lean
def test_real_lean_goal_and_tactic_attempts_through_the_runtime(tmp_path):
    project = _real_lean_project(tmp_path / "interactive", "theorem probe (n : Nat) : n + 0 = n := by\n  sorry\n")
    main = project / "Probe" / "Main.lean"
    services = LeanRuntimeServices(
        runtime_config(lsp_timeout=120.0), repl_factory=FakePool, start_sweepers=False
    )
    try:
        where = {"project_dir": str(project), "file_path": "Probe/Main.lean"}
        assert services.dispatch("lsp.goal", {**where, "line": 1, "character": 2}).endswith(
            "n : Nat\n⊢ n + 0 = n\n```"
        )
        assert services.dispatch("lsp.goal", {**where, "line": 0, "character": 0}) == (
            "No tactic goal at this position (not inside a tactic block)."
        )
        attempts = services.dispatch(
            "lsp.try_tactics",
            {**where, "line": 1, "tactics": ["omega", "exact missing", "skip"], "owner": "me"},
        )
        lines = attempts.splitlines()
        assert lines[3].split() == ["closed", "omega"]
        assert lines[4].startswith("  error      exact missing")
        assert lines[5].startswith("  error      skip  — unsolved goals")
        assert "Closed with no errors and no sorry left in the edited declaration: omega" in attempts
        buffer = services.dispatch(
            "lsp.check_buffer",
            {**where, "content": "theorem probe (n : Nat) : n + 0 = n := by\n  simp\n", "owner": "me"},
        )
        assert buffer.endswith("The buffer elaborates with no diagnostics.")
        assert main.read_text() == "theorem probe (n : Nat) : n + 0 = n := by\n  sorry\n"
        # Hover still works on a file the session was holding open.
        assert "Nat" in services.dispatch("lsp.hover", {**where, "line": 0, "character": 8})
        assert services.dispatch("lsp.goal", {**where, "line": 1, "character": 2}).endswith("⊢ n + 0 = n\n```")
        assert services.status(include_projects=True)["lsp_projects"]["resident"][0]["valid"] is True
    finally:
        services.close()


@_requires_real_lean
def test_real_lean_a_hole_in_another_declaration_does_not_count(tmp_path):
    project = _real_lean_project(
        tmp_path / "two", "theorem a (n : Nat) : n + 0 = n := by\n  sorry\n\ntheorem b : True := by\n  sorry\n"
    )
    services = LeanRuntimeServices(runtime_config(lsp_timeout=120.0), repl_factory=FakePool, start_sweepers=False)
    try:
        where = {"project_dir": str(project), "file_path": "Probe/Main.lean"}
        attempts = services.dispatch(
            "lsp.try_tactics", {**where, "line": 1, "tactics": ["omega", "sorry"], "owner": "me"}
        )
    finally:
        services.close()
    lines = attempts.splitlines()
    assert lines[3].split() == ["closed", "omega"]
    assert lines[4].split()[:2] == ["incomplete", "sorry"]
    assert "Closed with no errors and no sorry left in the edited declaration: omega" in attempts


@_requires_real_lean
def test_real_lean_still_elaborating_keeps_the_session_and_keeps_waiting(tmp_path):
    project = _real_lean_project(
        tmp_path / "elaborating", "#eval IO.sleep 300000\n\ntheorem probe : True := by\n  trivial\n"
    )
    main = project / "Probe" / "Main.lean"
    session = interactive.InteractiveLeanLspSession(lsp.LspConfig(cwd=str(project), timeout=5))
    session.start()
    try:
        for _ in range(2):
            with pytest.raises(interactive.LspStillElaborating, match="not a clean file"):
                session.goal(str(main), 3, 2)
            assert session.is_alive() is True
        assert list(session._open) == [main.resolve().as_uri()]
    finally:
        session.close()
