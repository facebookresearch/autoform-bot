"""Adapter and lifecycle tests for Autoform's leanclient LSP boundary."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest
from leanclient.aio import AsyncLeanLSPClient, LeanTransportError

from servers.lsp import server as lsp


class _FakeAsyncClient:
    instances: list[_FakeAsyncClient] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.alive = False
        self.calls: list[tuple] = []
        self.diagnostic_items = [
            {
                "severity": 1,
                "message": "type mismatch",
                "range": {"start": {"line": 2, "character": 4}},
            }
        ]
        self.hover_result: dict | None = {
            "contents": {"kind": "markdown", "value": "`Nat`"}
        }
        self.diagnostics_error: BaseException | None = None
        self.block_diagnostics = False
        type(self).instances.append(self)

    async def start(self) -> None:
        self.calls.append(("start",))
        self.alive = True

    async def close(self) -> None:
        self.calls.append(("close",))
        self.alive = False

    async def open(self, path: str, *, wait: bool) -> None:
        self.calls.append(("open", path, wait))

    async def diagnostics(self, path: str, *, fresh: bool, timeout: float):
        self.calls.append(("diagnostics", path, fresh, timeout))
        if self.diagnostics_error is not None:
            raise self.diagnostics_error
        if self.block_diagnostics:
            await asyncio.sleep(60)
        return SimpleNamespace(items=self.diagnostic_items)

    async def barrier(self, path: str, *, timeout: float) -> None:
        self.calls.append(("barrier", path, timeout))

    async def hover(self, path: str, line: int, character: int, *, fresh: bool):
        self.calls.append(("hover", path, line, character, fresh))
        return self.hover_result

    async def close_file(self, path: str) -> None:
        self.calls.append(("close_file", path))


@pytest.fixture
def fake_session(tmp_path: Path, monkeypatch):
    _FakeAsyncClient.instances.clear()
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "AdapterTest"\n', encoding="utf-8")
    source = project / "Main.lean"
    source.write_text("#check Nat\n", encoding="utf-8")
    session = lsp.LeanLspSession(
        lsp.LspConfig(cwd=str(project), timeout=1),
        client_factory=_FakeAsyncClient,
    )
    monkeypatch.setattr(session, "_read_process_identity", lambda: 999_999_999)
    monkeypatch.setattr(
        session,
        "_process_group_exists",
        lambda process_group_id: bool(session._client and session._client.alive),
    )
    session.start()
    try:
        yield session, source, _FakeAsyncClient.instances[-1]
    finally:
        session.close()


def test_leanclient_dependency_and_public_api_are_pinned() -> None:
    assert version("leanclient") == "0.13.2"
    constructor = inspect.signature(AsyncLeanLSPClient)
    for parameter in (
        "project_path",
        "max_workers",
        "request_timeout",
        "check_version",
        "server_command",
        "report_delay_ms",
    ):
        assert parameter in constructor.parameters
    for method in ("start", "close", "open", "diagnostics", "barrier", "hover", "close_file"):
        assert inspect.iscoroutinefunction(getattr(AsyncLeanLSPClient, method))


def test_start_configures_pinned_client_and_supervised_command(fake_session) -> None:
    session, _, client = fake_session

    assert client.kwargs["project_path"] == str(Path(session.config.cwd).resolve())
    assert client.kwargs["max_workers"] == 1
    assert client.kwargs["request_timeout"] == 1
    assert client.kwargs["check_version"] is True
    assert client.kwargs["report_delay_ms"] is None
    command = client.kwargs["server_command"]
    assert command[:3] == [sys.executable, "-m", "servers.lsp.launcher"]
    assert command[-3:] == ["--", "lake", "serve"]
    assert session.is_alive()


def test_start_failure_closes_client_and_event_loop(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()

    class FailingClient(_FakeAsyncClient):
        async def start(self) -> None:
            self.calls.append(("start",))
            raise LeanTransportError("initialize failed")

    session = lsp.LeanLspSession(
        lsp.LspConfig(cwd=str(project), timeout=1),
        client_factory=FailingClient,
    )
    monkeypatch.setattr(
        session,
        "_read_process_identity",
        lambda: (_ for _ in ()).throw(lsp.LspCleanupError("no child identity")),
    )

    with pytest.raises(lsp.LspProtocolError, match="initialize failed"):
        session.start()

    assert session._client is None
    assert session._loop is None
    assert session._loop_thread is None


def test_launcher_scrubs_lake_environment_and_records_own_group(tmp_path: Path) -> None:
    identity = tmp_path / "identity.json"
    observed = tmp_path / "environment.json"
    probe = (
        "import json, os, sys; "
        "open(sys.argv[1], 'w').write(json.dumps({"
        "name: os.environ.get(name) for name in "
        "('ELAN_TOOLCHAIN', 'LEAN_PATH', 'LAKE_CONFIG', 'PYTHONPATH')}))"
    )
    environment = os.environ.copy()
    for name in ("ELAN_TOOLCHAIN", "LEAN_PATH", "LAKE_CONFIG", "PYTHONPATH"):
        environment[name] = "poisoned"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "servers.lsp.launcher",
            str(identity),
            "--",
            sys.executable,
            "-c",
            probe,
            str(observed),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        start_new_session=True,
    )

    assert result.returncode == 0, result.stderr
    process_identity = json.loads(identity.read_text(encoding="utf-8"))
    assert process_identity["pid"] == process_identity["pgid"]
    assert json.loads(observed.read_text(encoding="utf-8")) == {
        "ELAN_TOOLCHAIN": None,
        "LEAN_PATH": None,
        "LAKE_CONFIG": None,
        "PYTHONPATH": None,
    }


def test_diagnostics_use_lean_barrier_and_close_document(fake_session) -> None:
    session, source, client = fake_session

    assert session.get_diagnostics(str(source)) == client.diagnostic_items

    assert [call[0] for call in client.calls] == [
        "start",
        "open",
        "diagnostics",
        "close_file",
    ]
    assert client.calls[1] == ("open", "Main.lean", False)
    assert client.calls[2][1:3] == ("Main.lean", True)
    assert 0 < client.calls[2][3] <= session.config.timeout


def test_hover_waits_for_barrier_and_preserves_text_format(fake_session) -> None:
    session, source, client = fake_session

    assert session.hover(str(source), 0, 7) == "`Nat`"

    assert [call[0] for call in client.calls] == [
        "start",
        "open",
        "barrier",
        "hover",
        "close_file",
    ]
    assert client.calls[2][1] == "Main.lean"
    assert client.calls[3] == ("hover", "Main.lean", 0, 7, False)


def test_document_operations_reject_paths_outside_project(fake_session, tmp_path: Path) -> None:
    session, _, client = fake_session
    outside = tmp_path / "Outside.lean"
    outside.write_text("#check Int\n", encoding="utf-8")

    with pytest.raises(ValueError, match="inside project root"):
        session.get_diagnostics(str(outside))

    assert [call[0] for call in client.calls] == ["start"]
    assert session.is_alive()


def test_lsp_queue_wait_is_bounded_by_session_timeout(fake_session) -> None:
    session, source, _ = fake_session
    session.config.timeout = 0.01
    session._operation_lock.acquire()
    try:
        with pytest.raises(lsp.LspBusyError, match="waiting for the Lean LSP session"):
            session.hover(str(source), 0, 0)
    finally:
        session._operation_lock.release()


def test_leanclient_failure_poisons_session_before_next_operation(fake_session) -> None:
    session, source, client = fake_session
    client.diagnostics_error = LeanTransportError("broken stream")

    with pytest.raises(lsp.LspProtocolError, match="broken stream"):
        session.get_diagnostics(str(source))

    assert not session.is_alive()
    with pytest.raises(lsp.LspProtocolError, match="no longer usable"):
        session.get_diagnostics(str(source))


def test_operation_timeout_is_absolute_and_aborts_session(fake_session) -> None:
    session, source, client = fake_session
    session.config.timeout = 0.02
    client.block_diagnostics = True

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="running Lean LSP operation"):
        session.get_diagnostics(str(source))

    assert time.monotonic() - started < 1
    assert not session.is_alive()


def test_close_escalates_until_the_supervised_group_is_gone(
    fake_session, monkeypatch
) -> None:
    session, _, client = fake_session
    group_alive = {"value": True}
    signals: list[int] = []

    async def incomplete_close() -> None:
        client.calls.append(("close",))

    def signal_group(process_group_id: int, signal_number: int) -> None:
        signals.append(signal_number)
        if signal_number == signal.SIGKILL:
            group_alive["value"] = False

    monkeypatch.setattr(client, "close", incomplete_close)
    monkeypatch.setattr(session, "_process_group_exists", lambda process_group_id: group_alive["value"])
    monkeypatch.setattr(session, "_signal_process_group", signal_group)

    session.close()

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert session._process_group_id is None


def test_close_raises_and_retains_identity_when_group_survives(
    fake_session, monkeypatch
) -> None:
    session, _, client = fake_session

    async def incomplete_close() -> None:
        client.calls.append(("close",))

    monkeypatch.setattr(client, "close", incomplete_close)
    monkeypatch.setattr(session, "_process_group_exists", lambda process_group_id: True)
    monkeypatch.setattr(session, "_signal_process_group", lambda *args: None)
    monkeypatch.setattr(session, "_wait_for_group_exit", lambda *args: False)

    with pytest.raises(lsp.LspCleanupError, match="survived SIGKILL"):
        session.close()

    assert session._process_group_id == 999_999_999
    monkeypatch.setattr(session, "_process_group_exists", lambda process_group_id: False)


def test_document_operations_are_serialized(fake_session) -> None:
    session, source, client = fake_session
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_count = 0

    async def diagnostics(path: str, *, fresh: bool, timeout: float):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_entered.set()
            while not release_first.is_set():
                await asyncio.sleep(0.005)
        else:
            second_entered.set()
        return SimpleNamespace(items=[])

    client.diagnostics = diagnostics
    threads = [
        threading.Thread(target=session.get_diagnostics, args=(str(source),))
        for _ in range(2)
    ]
    threads[0].start()
    assert first_entered.wait(timeout=1)
    threads[1].start()
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    for thread in threads:
        thread.join(timeout=2)

    assert second_entered.is_set()
    assert all(not thread.is_alive() for thread in threads)


@pytest.mark.real_lean
@pytest.mark.skipif(shutil.which("lake") is None, reason="Lake is not installed")
def test_real_leanclient_adapter_waits_for_diagnostics_and_serves_hover(
    tmp_path: Path,
) -> None:
    project = tmp_path / "leanclient-project"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
    (project / "lakefile.toml").write_text(
        'name = "LeanclientFixture"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    source = project / "Main.lean"
    source.write_text(
        "def twice (n : Nat) : Nat := n + n\n"
        "example : False := by\n"
        "  trivial\n",
        encoding="utf-8",
    )
    session = lsp.LeanLspSession(lsp.LspConfig(cwd=str(project), timeout=60))

    session.start()
    process_group_id = session._process_group_id
    try:
        diagnostics = session.get_diagnostics(str(source))
        assert any(item.get("severity") == 1 for item in diagnostics)
        assert any("False" in str(item.get("message")) for item in diagnostics)
        hover = session.hover(str(source), 0, 5)
        assert hover is not None
        assert "twice" in hover
    finally:
        session.close()

    assert process_group_id is not None
    assert not session._process_group_exists(process_group_id)
