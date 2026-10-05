"""Protocol and lifecycle regression tests for the Lean server's LSP backend."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from servers.lsp import server as lsp


class _FakeStream:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.failures:
            self.failures -= 1
            raise OSError("cannot close pipe")


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 43210
        self.returncode: int | None = None
        self.killed = False
        self.waited = False
        self.stdin = _FakeStream()
        self.stdout = _FakeStream()

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


def _retire_fake_group(process: _FakeProcess, process_group_id: int) -> None:
    assert process_group_id == process.pid
    process.kill()
    process.wait()


def _owned_session(process: _FakeProcess) -> lsp.LeanLspSession:
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = process
    session._process_group_id = process.pid
    return session


def test_json_rpc_error_response_raises_protocol_error(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    monkeypatch.setattr(
        session,
        "_read_message",
        lambda timeout: {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "initialization failed"},
        },
    )

    with pytest.raises(lsp.LspProtocolError, match="initialization failed"):
        session._read_response(1)


def test_start_aborts_process_when_initialize_fails(monkeypatch):
    process = _FakeProcess()
    popen_kwargs = {}

    def popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(lsp.subprocess, "Popen", popen)
    monkeypatch.setattr(lsp, "_kill_subprocesses", _retire_fake_group)

    session = lsp.LeanLspSession(lsp.LspConfig())

    def fail_initialize(method, params):
        raise lsp.LspProtocolError("initialize rejected")

    monkeypatch.setattr(session, "_send_request", fail_initialize)

    with pytest.raises(lsp.LspProtocolError, match="initialize rejected"):
        session.start()

    assert popen_kwargs["start_new_session"] is True
    assert process.killed is True
    assert process.waited is True
    assert process.stdin.close_calls == process.stdout.close_calls == 1
    assert session.process is None
    assert session._process_group_id is None


def test_start_rejects_an_unsupported_platform_before_spawning(monkeypatch):
    monkeypatch.setattr(lsp.os, "name", "nt")
    monkeypatch.setattr(
        lsp.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("unsupported LSP start spawned a process"),
    )

    with pytest.raises(RuntimeError, match="POSIX"):
        lsp.LeanLspSession(lsp.LspConfig()).start()


def test_startup_retries_cleanup_without_losing_the_initialization_error(monkeypatch):
    process = _FakeProcess()
    attempts = 0
    monkeypatch.setattr(lsp.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(lsp.time, "sleep", lambda delay: None)

    def retire(owned, process_group_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("stubborn process group")
        _retire_fake_group(owned, process_group_id)

    monkeypatch.setattr(lsp, "_kill_subprocesses", retire)
    session = lsp.LeanLspSession(lsp.LspConfig())

    def fail_initialize(method, params):
        raise lsp.LspProtocolError("initialize rejected")

    monkeypatch.setattr(session, "_send_request", fail_initialize)

    with pytest.raises(lsp.LspProtocolError, match="initialize rejected"):
        session.start()

    assert attempts == 2
    assert session.process is None


def test_cleanup_retry_never_resignals_a_retired_group(monkeypatch):
    process = _FakeProcess()
    process.stdin = _FakeStream(failures=1)
    session = _owned_session(process)
    signals = []

    def retire(owned, process_group_id):
        signals.append(process_group_id)
        _retire_fake_group(owned, process_group_id)

    monkeypatch.setattr(lsp, "_kill_subprocesses", retire)
    monkeypatch.setattr(lsp.time, "sleep", lambda delay: None)

    session.abort()

    # The pipe failure is retried after the leader was reaped, when its
    # process-group id may already belong to an unrelated group.
    assert signals == [process.pid]
    assert process.stdin.close_calls == 2
    assert session.process is None


def test_failed_operation_poisons_a_queued_waiter_before_releasing_admission(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    first_entered = threading.Event()
    release_first = threading.Event()
    calls = []
    errors: list[BaseException] = []

    def diagnostics(file_path, *, timeout=None):
        calls.append(file_path)
        first_entered.set()
        assert release_first.wait(timeout=2)
        raise lsp.LspProtocolError("broken shared stream")

    monkeypatch.setattr(session, "_get_diagnostics", diagnostics)

    def run() -> None:
        try:
            session.get_diagnostics("ignored.lean")
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert len(calls) == 1
    assert sorted(str(error) for error in errors) == [
        "Lean LSP session is retiring after a failed operation",
        "broken shared stream",
    ]


@pytest.mark.parametrize("operation", ["diagnostics", "hover"])
def test_did_close_failure_keeps_the_result_but_poisons_the_session(tmp_path, monkeypatch, operation):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = _FakeProcess()

    def notify(method, params, **kwargs):
        if method == "textDocument/didClose":
            raise TimeoutError("didClose write timed out")

    monkeypatch.setattr(session, "_send_notification", notify)
    monkeypatch.setattr(session, "_collect_diagnostics", lambda uri, timeout: [])
    monkeypatch.setattr(
        session,
        "_send_request",
        lambda method, params, timeout=30: {"contents": "Nat : Type"},
    )

    def call():
        if operation == "diagnostics":
            return session.get_diagnostics(str(source))
        return session.hover(str(source), 0, 0)

    assert call() == ([] if operation == "diagnostics" else "Nat : Type")
    assert not session.is_alive()
    with pytest.raises(lsp.LspProtocolError, match="retiring"):
        call()


def test_document_close_budget_exhaustion_poisons_the_session(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig(timeout=60))
    clock = {"now": 0.0}
    notifications = []
    monkeypatch.setattr(lsp.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        session,
        "_send_notification",
        lambda method, params, **kwargs: notifications.append(method),
    )

    def finish_at_deadline(uri, timeout):
        clock["now"] = 60.0
        return []

    monkeypatch.setattr(session, "_collect_diagnostics", finish_at_deadline)

    assert session.get_diagnostics(str(source)) == []
    assert notifications == ["textDocument/didOpen"]
    assert session._poisoned is True


def test_concurrent_abort_and_close_serialize_owned_cleanup(monkeypatch):
    process = _FakeProcess()
    session = _owned_session(process)
    entered = threading.Event()
    release = threading.Event()
    active = 0
    maximum_active = 0
    errors: list[BaseException] = []
    state_lock = threading.Lock()

    def retire(owned, process_group_id):
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        entered.set()
        assert release.wait(timeout=2)
        _retire_fake_group(owned, process_group_id)
        with state_lock:
            active -= 1

    monkeypatch.setattr(lsp, "_kill_subprocesses", retire)

    def call(operation) -> None:
        try:
            operation()
        except BaseException as error:
            errors.append(error)

    aborter = threading.Thread(target=call, args=(session.abort,))
    closer = threading.Thread(target=call, args=(session.close,))
    aborter.start()
    assert entered.wait(timeout=1)
    closer.start()
    closer.join(timeout=0.1)
    assert closer.is_alive()
    release.set()
    aborter.join(timeout=2)
    closer.join(timeout=2)

    assert errors == []
    assert maximum_active == 1
    assert session.process is None


@pytest.mark.skipif(os.name != "posix", reason="native LSP backend requires POSIX groups")
def test_close_retires_a_descendant_after_the_lsp_wrapper_exits():
    script = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline())
    assert process.wait(timeout=5) == 0
    session = _owned_session(process)
    try:
        session.close()
        assert session.process is None
        try:
            import psutil

            assert psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_diagnostics_wait_through_initial_quiet_period(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    uri = "file:///tmp/Test.lean"
    messages = iter(
        [
            None,
            None,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            },
            None,
        ]
    )
    monkeypatch.setattr(session, "_read_message", lambda timeout: next(messages))

    # The first two quiet reads are not evidence that the file is clean. The
    # explicit empty publication is, and should be returned as a valid result.
    assert session._collect_diagnostics(uri, timeout=60) == []


def test_diagnostics_timeout_without_publication(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    clock = {"now": 0.0}

    monkeypatch.setattr(lsp.time, "monotonic", lambda: clock["now"])

    def quiet_read(timeout):
        clock["now"] += timeout
        return None

    monkeypatch.setattr(session, "_read_message", quiet_read)

    with pytest.raises(TimeoutError, match="waiting for diagnostics"):
        session._collect_diagnostics("file:///tmp/Test.lean", timeout=5)

    assert clock["now"] == 5


def test_malformed_diagnostics_payload_is_protocol_error(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    uri = "file:///tmp/Test.lean"
    messages = iter(
        [
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": {"not": "a list"}},
            }
        ]
    )
    monkeypatch.setattr(session, "_read_message", lambda timeout: next(messages))

    with pytest.raises(lsp.LspProtocolError, match="diagnostics must be a list"):
        session._collect_diagnostics(uri, timeout=60)


def test_get_diagnostics_closes_document_after_timeout(tmp_path: Path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("example : True := by trivial\n")
    session = lsp.LeanLspSession(lsp.LspConfig())
    notifications: list[str] = []

    monkeypatch.setattr(
        session,
        "_send_notification",
        lambda method, params, **kwargs: notifications.append(method),
    )

    def timeout(uri, timeout):
        raise TimeoutError("no diagnostics publication")

    monkeypatch.setattr(session, "_collect_diagnostics", timeout)

    with pytest.raises(TimeoutError, match="no diagnostics publication"):
        session.get_diagnostics(str(source))

    assert notifications == [
        "textDocument/didOpen",
        "textDocument/didClose",
    ]


def test_hover_opens_and_closes_document(tmp_path: Path, monkeypatch):
    source = tmp_path / "Test.lean"
    source_text = "#check Nat\n"
    source.write_text(source_text)
    session = lsp.LeanLspSession(lsp.LspConfig())
    notifications: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        session,
        "_send_notification",
        lambda method, params, **kwargs: notifications.append((method, params)),
    )

    def send_request(method, params, timeout=30):
        assert method == "textDocument/hover"
        assert params["textDocument"]["uri"] == source.resolve().as_uri()
        return {"contents": {"kind": "plaintext", "value": "Nat : Type"}}

    monkeypatch.setattr(session, "_send_request", send_request)

    assert session.hover(str(source), 0, 7) == "Nat : Type"
    assert [method for method, _ in notifications] == [
        "textDocument/didOpen",
        "textDocument/didClose",
    ]
    assert notifications[0][1]["textDocument"]["text"] == source_text


def test_document_operations_are_serialized_per_session(tmp_path: Path, monkeypatch):
    first = tmp_path / "First.lean"
    second = tmp_path / "Second.lean"
    first.write_text("#check Nat\n")
    second.write_text("#check Int\n")
    session = lsp.LeanLspSession(lsp.LspConfig())
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    monkeypatch.setattr(
        session,
        "_send_notification",
        lambda method, params, **kwargs: None,
    )

    def collect(uri, timeout):
        nonlocal calls
        with calls_lock:
            calls += 1
            number = calls
        if number == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        return []

    monkeypatch.setattr(session, "_collect_diagnostics", collect)
    threads = [
        threading.Thread(target=session.get_diagnostics, args=(str(first),)),
        threading.Thread(target=session.get_diagnostics, args=(str(second),)),
    ]
    threads[0].start()
    assert first_entered.wait(timeout=1)
    threads[1].start()

    # The second call cannot begin reading the shared stdout stream until the
    # first document's complete didOpen/diagnostics/didClose lifecycle finishes.
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    for thread in threads:
        thread.join(timeout=2)

    assert second_entered.is_set()
    assert all(not thread.is_alive() for thread in threads)


def test_lsp_queue_wait_is_bounded_by_session_timeout(tmp_path: Path):
    source = tmp_path / "Queued.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig(timeout=0.01))
    session._operation_lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="waiting for the Lean LSP session"):
            session.hover(str(source), 0, 0)
    finally:
        session._operation_lock.release()


@pytest.mark.parametrize(
    "partial",
    [
        b"Content-Length: 10\r\n",
        b"Content-Length: 10\r\n\r\n{}",
    ],
)
def test_partial_lsp_frame_cannot_block_past_deadline(partial):
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = SimpleNamespace(stdout=reader)
    try:
        os.write(write_fd, partial)
        with pytest.raises(TimeoutError, match="reading an LSP"):
            session._read_message(timeout=0.01)
    finally:
        os.close(write_fd)
        reader.close()


def test_lsp_write_cannot_block_on_a_full_pipe():
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    while True:
        try:
            os.write(write_fd, b"x" * 4096)
        except BlockingIOError:
            break

    writer = os.fdopen(write_fd, "wb", buffering=0)
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = SimpleNamespace(stdin=writer)
    try:
        with pytest.raises(TimeoutError, match="writing an LSP message"):
            session._write_message({"jsonrpc": "2.0"}, timeout=0.01)
    finally:
        writer.close()
        os.close(read_fd)
