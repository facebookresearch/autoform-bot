"""Opt-in contract test for Autoform's pinned Lean Beam MCP boundary."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryFile

import pytest

from cli.beam import inspect_beam_runtime


pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOFORM_RUN_REAL_LEAN_BEAM_TESTS") != "1",
    reason="set AUTOFORM_RUN_REAL_LEAN_BEAM_TESTS=1 to exercise the installed Beam runtime",
)

MODERN_MCP_VERSION = "2026-07-28"
REQUEST_TIMEOUT_SECONDS = 180


class McpClient:
    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        self._next_id = 0
        self._stderr = TemporaryFile(mode="w+t", encoding="utf-8")
        self._stdout_buffer = b""
        self._responses: dict[int | str, dict] = {}
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            bufsize=0,
            start_new_session=True,
        )

    def _send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(
            (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        )
        self.process.stdin.flush()

    def _read_message(self, deadline: float) -> dict:
        assert self.process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            while True:
                line, separator, remainder = self._stdout_buffer.partition(b"\n")
                if separator:
                    self._stdout_buffer = remainder
                    return json.loads(line)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise AssertionError("timed out waiting for a complete MCP response")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise AssertionError(
                        f"Lean Beam exited before a complete MCP response: {self.stderr()}"
                    )
                self._stdout_buffer += chunk
        finally:
            selector.close()

    def send_request(
        self,
        method: str,
        params: dict | None = None,
        *,
        request_id: int | str | None = None,
    ) -> int | str:
        if request_id is None:
            self._next_id += 1
            request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        return request_id

    def read_response(self, request_id: int | str) -> dict:
        if response := self._responses.pop(request_id, None):
            return response
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while True:
            response = self._read_message(deadline)
            if response.get("method") is not None:
                continue
            if response.get("id") == request_id:
                return response
            self._responses[response.get("id")] = response

    def request(self, method: str, params: dict | None = None) -> dict:
        return self.read_response(self.send_request(method, params))

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def modern_request(self, method: str, params: dict | None = None) -> dict:
        return self.read_response(self.send_modern_request(method, params))

    def send_modern_request(
        self,
        method: str,
        params: dict | None = None,
        *,
        request_id: int | str | None = None,
    ) -> int | str:
        params = dict(params or {})
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": MODERN_MCP_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {
                "name": "autoform-integration-test",
                "version": "0",
            },
        }
        return self.send_request(method, params, request_id=request_id)

    def response_ready(self, request_id: int | str) -> bool:
        return request_id in self._responses

    def forget_request(self, request_id: int | str) -> None:
        self._responses.pop(request_id, None)

    def discover(self) -> None:
        response = self.modern_request("server/discover")
        result = response["result"]
        assert result["resultType"] == "complete"
        assert result["supportedVersions"] == [MODERN_MCP_VERSION]

    def call_tool(self, name: str, arguments: dict | None = None) -> tuple[dict, dict]:
        response = self.modern_request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
        )
        result = response["result"]
        assert result["resultType"] == "complete"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "lean-beam-mcp"
        structured = result["structuredContent"]
        assert isinstance(structured, dict)
        return result, structured

    def stderr(self) -> str:
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()

    def close(self) -> None:
        close_error: str | None = None
        try:
            if self.process.poll() is None:
                assert self.process.stdin is not None
                self.process.stdin.close()
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    close_error = "Lean Beam did not shut down after MCP EOF"
            returncode = self.process.poll()
            stderr = self.stderr()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.killpg(self.process.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                close_error = close_error or "Lean Beam left a process in its MCP process group"
        finally:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if self.process.poll() is None:
                self.process.wait(timeout=10)
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()
            self._stderr.close()
        if close_error is not None:
            raise AssertionError(close_error)
        if returncode != 0:
            raise AssertionError(f"Lean Beam exited with {returncode}: {stderr}")


def workspace(root: Path) -> dict[str, str]:
    return {"root": str(root.resolve())}


def assert_success(result: dict, structured: dict) -> None:
    assert result.get("isError") is not True, result
    assert structured.get("success") is True, structured


def wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def test_pinned_beam_explicit_session_contract(repo_root: Path, tmp_path: Path) -> None:
    beam_command = os.environ["AUTOFORM_LEAN_BEAM_MCP"]
    toolchain = os.environ.get("AUTOFORM_REAL_LEAN_TOOLCHAIN", "leanprover/lean4:v4.33.0")
    external_project = tmp_path / "beam-dep"
    external_project.mkdir()
    (external_project / "lean-toolchain").write_text(f"{toolchain}\n", encoding="utf-8")
    (external_project / "lakefile.toml").write_text(
        'name = "beamDep"\ndefaultTargets = ["BeamDep"]\n\n[[lean_lib]]\nname = "BeamDep"\n',
        encoding="utf-8",
    )
    external_source = external_project / "BeamDep.lean"
    external_source.write_text("def externalValue : Nat := 7\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text(f"{toolchain}\n", encoding="utf-8")
    (project / "lakefile.toml").write_text(
        'name = "BeamSmoke"\n'
        'defaultTargets = ["BeamSmoke"]\n\n'
        '[[require]]\nname = "beamDep"\npath = "../beam-dep"\n\n'
        '[[lean_lib]]\nname = "BeamSmoke"\n',
        encoding="utf-8",
    )
    source = project / "BeamSmoke.lean"
    source.write_text(
        "import BeamDep\n"
        "import BeamSmoke.A\n\n"
        "def answer : Nat := dependencyValue + externalValue\n\n"
        "set_option linter.unusedVariables true in\n"
        "theorem warnOnly (n : Nat) : True := by\n"
        "  trivial\n\n"
        '#check ("😀", Nat)\n',
        encoding="utf-8",
    )
    smoke_lines = source.read_text(encoding="utf-8").splitlines()
    runtime_line = smoke_lines.index("def answer : Nat := dependencyValue + externalValue") - 1
    declaration_line = smoke_lines.index("set_option linter.unusedVariables true in") - 1
    hover_line = smoke_lines.index('#check ("😀", Nat)')
    (project / "BeamSmoke").mkdir()
    dependency_source = project / "BeamSmoke" / "A.lean"
    dependency_source.write_text("def dependencyValue : Nat := 42\n", encoding="utf-8")
    cancellation_source = project / "BeamCancellation.lean"
    cancellation_source.write_text(
        "import Lean\n\n"
        "open Lean Elab Tactic\n\n"
        "private partial def waitForAutoformCancelGate (path : System.FilePath) : TacticM Unit := do\n"
        "  if ← path.pathExists then\n"
        "    pure ()\n"
        "  else\n"
        "    IO.sleep 20\n"
        "    if let some tk := (← readThe Core.Context).cancelTk? then\n"
        "      if ← tk.isSet then\n"
        "        throwInterruptException\n"
        "    waitForAutoformCancelGate path\n\n"
        'elab "autoform_cancel_gate" : tactic => do\n'
        '  let some startedText ← IO.getEnv "AUTOFORM_BEAM_CANCEL_STARTED"\n'
        '    | throwError "missing AUTOFORM_BEAM_CANCEL_STARTED"\n'
        '  let some releaseText ← IO.getEnv "AUTOFORM_BEAM_CANCEL_RELEASE"\n'
        '    | throwError "missing AUTOFORM_BEAM_CANCEL_RELEASE"\n'
        '  IO.FS.writeFile (System.FilePath.mk startedText) "started\\n"\n'
        "  waitForAutoformCancelGate (System.FilePath.mk releaseText)\n"
        "  evalTactic (← `(tactic| exact trivial))\n\n"
        "example : True := by\n"
        "  trivial -- autoform cancellation target\n",
        encoding="utf-8",
    )
    initial_build = subprocess.run(
        ["lake", "build"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    assert initial_build.returncode == 0, initial_build.stdout + initial_build.stderr

    assert Path(beam_command).is_absolute()
    assert os.access(beam_command, os.X_OK)
    cancel_started = tmp_path / "cancel-started"
    cancel_release = tmp_path / "cancel-release"
    beam_env = dict(os.environ)
    beam_env["AUTOFORM_LEAN_BEAM_MCP"] = beam_command
    beam_env["AUTOFORM_BEAM_CANCEL_STARTED"] = str(cancel_started)
    beam_env["AUTOFORM_BEAM_CANCEL_RELEASE"] = str(cancel_release)

    admission = inspect_beam_runtime(beam_command, timeout=REQUEST_TIMEOUT_SECONDS)
    assert admission.ok, admission.issues
    assert admission.observed is not None
    assert admission.observed["source_commit"] == admission.expected["commit"]
    client = McpClient(
        [sys.executable, "-m", "cli.beam_launcher"],
        cwd=repo_root,
        env=beam_env,
    )
    try:
        lock = json.loads((repo_root / "lean-beam.lock.json").read_text(encoding="utf-8"))
        client.discover()
        assert lock["mcp_protocol"] == MODERN_MCP_VERSION
        version_result, identity = client.call_tool("beam_version")
        assert version_result.get("isError") is not True
        assert identity["name"] == "lean-beam-mcp"
        assert identity["version"] == lock["version"]
        assert identity["mcp_protocol"] == lock["mcp_protocol"]
        assert identity["source_commit"] == lock["commit"]
        assert identity["runtime_current"] is True
        assert "runtime_error" not in identity
        assert identity.get("source_dirty") is not True
        assert identity["runtime_active"] is False

        missing_root = tmp_path / "missing-project"
        file_root = tmp_path / "not-a-directory"
        file_root.write_text("not a project directory\n", encoding="utf-8")
        empty_root = tmp_path / "not-a-project"
        empty_root.mkdir()
        invalid_roots = (
            ("relative/project", "workspace root must be an absolute path"),
            (str(missing_root), "workspace root does not resolve:"),
            (str(file_root), "workspace root is not a directory:"),
            (str(empty_root), "workspace root is not a Lean/Lake project:"),
        )
        for root, expected_message in invalid_roots:
            result, rejected = client.call_tool(
                "lean_sync",
                {
                    "workspace": {"root": root},
                    "path": "BeamSmoke.lean",
                },
            )
            assert result["isError"] is True
            assert rejected["code"] == "invalidInput"
            assert expected_message in rejected["message"]

        _, inactive_stats = client.call_tool("beam_stats")
        assert inactive_stats["workspaces"] == {}

        descriptor = workspace(project)
        absent_drop_result, absent_drop = client.call_tool(
            "lean_drop_workspace",
            {"workspace": descriptor},
        )
        assert absent_drop_result.get("isError") is not True
        assert absent_drop["dropped"] is False
        assert absent_drop["invalidated_handles"] is False
        assert absent_drop["reason"] == "notFound"
        _, still_inactive_stats = client.call_tool("beam_stats")
        assert still_inactive_stats["workspaces"] == {}

        alias = tmp_path / "project-alias"
        alias.symlink_to(project, target_is_directory=True)
        alias_result, alias_sync = client.call_tool(
            "lean_sync",
            {
                "workspace": {"root": str(alias.absolute())},
                "path": "BeamSmoke.lean",
                "diagnostic_scope": "all",
                "diagnostics_in_result": True,
            },
        )
        assert alias_result.get("isError") is not True
        assert alias_sync["workspace"] == descriptor
        _, alias_stats = client.call_tool("beam_stats")
        assert set(alias_stats["workspaces"]) == {f"local:{project.resolve()}"}

        external_result, external_sync = client.call_tool(
            "lean_sync",
            {
                "workspace": descriptor,
                "path": str(external_source.resolve()),
            },
        )
        assert external_result.get("isError") is not True
        assert external_sync["workspace"] == descriptor
        assert external_sync["path"] == external_source.resolve().as_uri()
        relative_external_result, relative_external_sync = client.call_tool(
            "lean_sync",
            {
                "workspace": descriptor,
                "path": "../beam-dep/BeamDep.lean",
            },
        )
        assert relative_external_result.get("isError") is not True
        assert relative_external_sync["workspace"] == descriptor
        assert relative_external_sync["path"] == external_source.resolve().as_uri()

        cancellation_result, cancellation_sync = client.call_tool(
            "lean_sync",
            {
                "workspace": descriptor,
                "path": "BeamCancellation.lean",
            },
        )
        assert cancellation_result.get("isError") is not True
        cancellation_snapshot = cancellation_sync["snapshot"]
        cancellation_line = cancellation_source.read_text(encoding="utf-8").splitlines().index(
            "  trivial -- autoform cancellation target"
        )
        cancelled_id = client.send_modern_request(
            "tools/call",
            {
                "name": "lean_run_at",
                "arguments": {
                    "workspace": descriptor,
                    "path": "BeamCancellation.lean",
                    "snapshot": cancellation_snapshot,
                    "line": cancellation_line,
                    "character": 2,
                    "text": "autoform_cancel_gate",
                },
            },
            request_id="autoform-cancelled-run-at",
        )
        wait_for_file(cancel_started)
        client.notify(
            "notifications/cancelled",
            {
                "requestId": cancelled_id,
                "reason": "integration deadline expired",
            },
        )
        drop_result, dropped = client.call_tool(
            "lean_drop_workspace",
            {"workspace": descriptor},
        )
        assert drop_result.get("isError") is not True
        assert dropped["workspace"] == descriptor
        assert dropped["dropped"] is True
        assert dropped["invalidated_handles"] is True
        assert not client.response_ready(cancelled_id)
        client.forget_request(cancelled_id)

        recovery_result, recovered = client.call_tool(
            "lean_sync",
            {
                "workspace": descriptor,
                "path": "BeamCancellation.lean",
            },
        )
        assert recovery_result.get("isError") is not True
        assert recovered["snapshot"] != cancellation_snapshot
        post_cancel_result, post_cancel = client.call_tool(
            "lean_run_at",
            {
                "workspace": descriptor,
                "path": "BeamCancellation.lean",
                "snapshot": recovered["snapshot"],
                "line": cancellation_line,
                "character": 2,
                "text": "exact trivial",
            },
        )
        assert_success(post_cancel_result, post_cancel)

        sync_result, synced = client.call_tool(
            "lean_sync",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "diagnostic_scope": "all",
                "diagnostics_in_result": True,
            },
        )
        assert sync_result.get("isError") is not True
        snapshot = synced["snapshot"]
        assert isinstance(snapshot, str) and snapshot
        assert synced["workspace"] == descriptor
        assert synced["readiness"]["save_ready"] is True
        counts = synced["diagnostics"]["counts"]
        assert counts["total"] == sum(
            counts[name]
            for name in ("error", "warning", "information", "hint", "unknown")
        )
        assert counts["warning"] >= 1
        diagnostics = synced["diagnostics"]["items"]
        assert any(item["severity"] == "warning" for item in diagnostics)
        assert all(item["path"] == "BeamSmoke.lean" for item in diagnostics)
        assert all(item["snapshot"] == snapshot for item in diagnostics)
        assert synced["document_progress"]["done"] is True

        runtime_result, runtime_probe = client.call_tool(
            "lean_run_at",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": runtime_line,
                "character": 0,
                "text": "#eval Lean.versionString",
            },
        )
        assert_success(runtime_result, runtime_probe)
        expected_lean_version = toolchain.rsplit(":v", 1)[1]
        assert any(
            expected_lean_version in message["text"]
            for message in runtime_probe["messages"]
        )

        hover_result, hover = client.call_tool(
            "lean_hover",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": hover_line,
                "character": 14,
            },
        )
        assert hover_result.get("isError") is not True
        assert "Nat" in hover["contents"]["value"]

        mint_result, minted = client.call_tool(
            "lean_run_at_handle",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": declaration_line,
                "character": 0,
                "text": "def transient : Nat := answer + 1",
            },
        )
        assert_success(mint_result, minted)
        first_handle = minted["next_handle"]
        assert isinstance(first_handle, dict)

        other_project = tmp_path / "other-project"
        other_project.mkdir()
        (other_project / "lean-toolchain").write_text(f"{toolchain}\n", encoding="utf-8")
        (other_project / "lakefile.toml").write_text(
            'name = "OtherSmoke"\ndefaultTargets = ["OtherSmoke"]\n\n[[lean_lib]]\nname = "OtherSmoke"\n',
            encoding="utf-8",
        )
        (other_project / "OtherSmoke.lean").write_text("def other : Nat := 7\n", encoding="utf-8")
        cross_workspace_result, cross_workspace = client.call_tool(
            "lean_run_with",
            {
                "workspace": workspace(other_project),
                "path": "OtherSmoke.lean",
                "handle": first_handle,
                "text": "#check transient",
            },
        )
        assert cross_workspace_result["isError"] is True
        assert cross_workspace["code"] == "invalidParams"
        assert "does not match handle workspace" in cross_workspace["message"]

        failed_result, failed_continuation = client.call_tool(
            "lean_run_with",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": first_handle,
                "text": 'def broken : Nat := "not a Nat"',
            },
        )
        assert failed_result.get("isError") is not True
        assert failed_continuation["success"] is False
        assert failed_continuation["next_handle"] is None

        isolated_result, isolated = client.call_tool(
            "lean_run_at",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": declaration_line,
                "character": 0,
                "text": "#check transient",
            },
        )
        assert isolated_result.get("isError") is not True
        assert isolated["success"] is False
        assert any(
            "unknown identifier" in message["text"].lower()
            for message in isolated["messages"]
        )

        continuation_result, continued = client.call_tool(
            "lean_run_with",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": first_handle,
                "text": "def continued : Nat := transient + 1",
            },
        )
        assert_success(continuation_result, continued)
        next_handle = continued["next_handle"]
        assert isinstance(next_handle, dict)
        linear_result, linear = client.call_tool(
            "lean_run_with_linear",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": next_handle,
                "text": "def linear : Nat := continued + 1",
            },
        )
        assert_success(linear_result, linear)
        linear_handle = linear["next_handle"]
        assert isinstance(linear_handle, dict)
        consumed_result, consumed = client.call_tool(
            "lean_run_with",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": next_handle,
                "text": "#check continued",
            },
        )
        assert consumed_result["isError"] is True
        assert consumed["code"] == "invalidParams"
        for handle in (linear_handle, first_handle):
            release_result, released = client.call_tool(
                "lean_release",
                {
                    "workspace": descriptor,
                    "path": "BeamSmoke.lean",
                    "handle": handle,
                },
            )
            assert release_result.get("isError") is not True
            assert released.get("result") is None

        edit_handle_result, edit_handle_payload = client.call_tool(
            "lean_run_at_handle",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": declaration_line,
                "character": 0,
                "text": "def staleAfterEdit : Nat := answer",
            },
        )
        assert_success(edit_handle_result, edit_handle_payload)
        edit_handle = edit_handle_payload["next_handle"]

        source.write_text(
            source.read_text(encoding="utf-8") + "\n-- changed after snapshot\n",
            encoding="utf-8",
        )
        _, updated = client.call_tool(
            "lean_update",
            {"workspace": descriptor, "path": "BeamSmoke.lean"},
        )
        assert updated["snapshot"] != snapshot
        stale_result, stale = client.call_tool(
            "lean_run_at",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": runtime_line,
                "character": 0,
                "text": "#check answer",
            },
        )
        assert stale_result["isError"] is True
        assert stale["code"] == "contentModified"
        assert stale["data"]["reason"] == "snapshotMismatch"
        assert stale["data"]["expectedSnapshot"] == snapshot
        assert stale["data"]["currentSnapshot"] == updated["snapshot"]
        stale_edit_handle_result, stale_edit_handle = client.call_tool(
            "lean_run_with",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": edit_handle,
                "text": "#check staleAfterEdit",
            },
        )
        assert stale_edit_handle_result["isError"] is True
        assert stale_edit_handle["code"] == "contentModified"

        _, invalidated = client.call_tool(
            "lean_run_at_handle",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": updated["snapshot"],
                "line": declaration_line,
                "character": 0,
                "text": "def invalidatedAfterDrop : Nat := answer",
            },
        )
        invalidated_handle = invalidated["next_handle"]
        assert isinstance(invalidated_handle, dict)

        dependency_source.write_text(
            dependency_source.read_text(encoding="utf-8")
            + "\ndef availableAfterExternalBuild : Nat := dependencyValue\n",
            encoding="utf-8",
        )
        dependency_sync_result, dependency_sync = client.call_tool(
            "lean_sync",
            {"workspace": descriptor, "path": "BeamSmoke/A.lean"},
        )
        assert dependency_sync_result.get("isError") is not True
        assert dependency_sync["readiness"]["save_ready"] is True

        build = subprocess.run(
            ["lake", "build"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        assert build.returncode == 0, build.stdout + build.stderr

        drop_result, dropped = client.call_tool(
            "lean_drop_workspace",
            {"workspace": descriptor},
        )
        assert drop_result.get("isError") is not True
        assert dropped["dropped"] is True
        assert dropped["invalidated_handles"] is True
        stale_handle_result, stale_handle = client.call_tool(
            "lean_run_with",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": invalidated_handle,
                "text": "#check invalidatedAfterDrop",
            },
        )
        assert stale_handle_result["isError"] is True
        assert stale_handle["code"] == "contentModified"
        _, resynced = client.call_tool(
            "lean_sync",
            {"workspace": descriptor, "path": "BeamSmoke.lean"},
        )
        assert resynced["readiness"]["save_ready"] is True
        assert resynced["snapshot"] != updated["snapshot"]
        stale_generation_result, stale_generation = client.call_tool(
            "lean_run_at",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": updated["snapshot"],
                "line": runtime_line,
                "character": 0,
                "text": "#check answer",
            },
        )
        assert stale_generation_result["isError"] is True
        assert stale_generation["code"] == "contentModified"
        assert stale_generation["data"]["reason"] == "snapshotMismatch"
        assert stale_generation["data"]["expectedSnapshot"] == updated["snapshot"]
        assert stale_generation["data"]["currentSnapshot"] == resynced["snapshot"]
        dependency_probe_result, dependency_probe = client.call_tool(
            "lean_run_at",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": resynced["snapshot"],
                "line": runtime_line,
                "character": 0,
                "text": "#check availableAfterExternalBuild",
            },
        )
        assert_success(dependency_probe_result, dependency_probe)

        eof_handle_result, eof_handle = client.call_tool(
            "lean_run_at_handle",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": resynced["snapshot"],
                "line": declaration_line,
                "character": 0,
                "text": "def liveAtEof : Nat := answer",
            },
        )
        assert_success(eof_handle_result, eof_handle)
        assert isinstance(eof_handle["next_handle"], dict)
    finally:
        cancel_release.write_text("release\n", encoding="utf-8")
        client.close()
