"""Protocol and lifecycle regression tests for the Lean server's LSP backend."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
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
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


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
    monkeypatch.setattr(
        lsp,
        "_terminate_process_group",
        lambda process_group_id: setattr(process, "killed", True),
    )
    monkeypatch.setattr(lsp, "_reap_process", lambda owned: owned.wait())

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


def test_startup_transfers_failed_cleanup_without_losing_initialization_error(
    monkeypatch,
):
    process = _FakeProcess()
    attempts = 0
    monkeypatch.setattr(lsp.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(lsp.time, "sleep", lambda delay: None)

    def terminate(process_group_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("stubborn process group")
        process.killed = True

    monkeypatch.setattr(lsp, "_terminate_process_group", terminate)
    monkeypatch.setattr(lsp, "_reap_process", lambda owned: owned.wait())
    session = lsp.LeanLspSession(lsp.LspConfig())
    monkeypatch.setattr(
        session,
        "_send_request",
        lambda method, params: (_ for _ in ()).throw(
            lsp.LspProtocolError("initialize rejected")
        ),
    )

    with pytest.raises(lsp.LeanLspStartupError) as raised:
        session.start()

    assert isinstance(raised.value.cause, lsp.LspProtocolError)
    assert str(raised.value.cause) == "initialize rejected"
    assert raised.value.session is session
    assert attempts == 1
    assert session.process is process
    assert session.is_alive() is False

    session.abort()

    assert attempts == 2
    assert session.process is None


def test_start_retires_process_if_group_publication_is_interrupted(monkeypatch):
    process = _FakeProcess()
    monkeypatch.setattr(lsp.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        lsp,
        "_terminate_process_group",
        lambda process_group_id: setattr(process, "killed", True),
    )
    monkeypatch.setattr(lsp, "_reap_process", lambda owned: owned.wait())

    class InterruptedSession(lsp.LeanLspSession):
        interrupted = False

        def __setattr__(self, name, value):
            if name == "_process_group_id" and value == process.pid and not self.interrupted:
                self.interrupted = True
                raise RuntimeError("group publication interrupted")
            super().__setattr__(name, value)

    session = InterruptedSession(lsp.LspConfig())

    with pytest.raises(RuntimeError, match="group publication interrupted"):
        session.start()

    assert process.killed is True
    assert process.waited is True
    assert session.process is None


def test_cleanup_retry_never_resignals_a_retired_group(monkeypatch):
    process = _FakeProcess()
    process.stdin = _FakeStream(failures=1)
    session = _owned_session(process)
    terminations = []
    reaps = []

    monkeypatch.setattr(
        lsp,
        "_terminate_process_group",
        lambda process_group_id: terminations.append(process_group_id),
    )

    def reap(owned):
        reaps.append(owned)
        owned.wait()

    monkeypatch.setattr(lsp, "_reap_process", reap)
    monkeypatch.setattr(lsp.time, "sleep", lambda delay: None)

    session.abort()

    assert terminations == [process.pid]
    assert len(reaps) == 2
    assert process.stdin.close_calls == 2
    assert session.process is None


def test_reap_retry_never_resignals_a_retired_group(monkeypatch):
    process = _FakeProcess()
    session = _owned_session(process)
    terminations = []
    reap_calls = 0

    monkeypatch.setattr(
        lsp,
        "_terminate_process_group",
        lambda process_group_id: terminations.append(process_group_id),
    )

    def reap(owned):
        nonlocal reap_calls
        reap_calls += 1
        if reap_calls == 1:
            raise RuntimeError("injected reap failure")
        owned.wait()

    monkeypatch.setattr(lsp, "_reap_process", reap)
    monkeypatch.setattr(lsp.time, "sleep", lambda delay: None)

    session.abort()

    assert terminations == [process.pid]
    assert reap_calls == 2
    assert session.process is None


def test_is_alive_does_not_reap_the_group_leader(monkeypatch):
    process = _FakeProcess()
    session = _owned_session(process)
    monkeypatch.setattr(
        process,
        "poll",
        lambda: pytest.fail("is_alive reaped the process-group leader"),
    )

    assert session.is_alive() is True
    assert process.returncode is None


@pytest.mark.parametrize("operation", ["diagnostics", "hover"])
def test_retirement_stops_a_queued_operation_before_dispatch(
    tmp_path, monkeypatch, operation
):
    session = lsp.LeanLspSession(lsp.LspConfig())
    source = tmp_path / "Queued.lean"
    source.write_text("#check Nat\n")
    calls = []
    errors = []
    started = threading.Event()
    session._operation_lock.acquire()
    monkeypatch.setattr(
        session,
        "_get_diagnostics",
        lambda *args, **kwargs: calls.append("diagnostics") or [],
    )
    monkeypatch.setattr(
        session,
        "_hover",
        lambda *args, **kwargs: calls.append("hover") or None,
    )

    def run():
        started.set()
        try:
            if operation == "diagnostics":
                session.get_diagnostics(str(source))
            else:
                session.hover(str(source), 0, 0)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(timeout=1)
    time.sleep(0.05)
    assert thread.is_alive()
    session.retire()
    session._operation_lock.release()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert calls == []
    assert len(errors) == 1
    assert isinstance(errors[0], lsp.LspProtocolError)
    assert "retiring" in str(errors[0])


@pytest.mark.parametrize("operation", ["diagnostics", "hover"])
def test_did_close_failure_keeps_result_but_retires_session(
    tmp_path, monkeypatch, operation
):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = _owned_session(_FakeProcess())

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

    if operation == "diagnostics":
        assert session.get_diagnostics(str(source)) == []
    else:
        assert session.hover(str(source), 0, 0) == "Nat : Type"

    assert session.is_alive() is False
    with pytest.raises(lsp.LspProtocolError, match="retiring"):
        if operation == "diagnostics":
            session.get_diagnostics(str(source))
        else:
            session.hover(str(source), 0, 0)


@pytest.mark.parametrize("operation", ["diagnostics", "hover"])
def test_post_admission_budget_exhaustion_keeps_session_healthy(
    tmp_path, monkeypatch, operation
):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = _owned_session(_FakeProcess())
    session.config.timeout = 0
    monkeypatch.setattr(lsp.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(
        session,
        "_get_diagnostics",
        lambda *args, **kwargs: pytest.fail("expired admission ran diagnostics"),
    )
    monkeypatch.setattr(
        session,
        "_hover",
        lambda *args, **kwargs: pytest.fail("expired admission ran hover"),
    )

    with pytest.raises(lsp.LspBusyError, match="waiting for the Lean LSP session"):
        if operation == "diagnostics":
            session.get_diagnostics(str(source))
        else:
            session.hover(str(source), 0, 0)

    assert session.is_alive() is True
    assert session._retire_pending is False


def test_document_close_budget_exhaustion_retires_session(tmp_path, monkeypatch):
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
    assert session._retire_pending is True


def test_hover_input_decode_failure_keeps_healthy_session(tmp_path):
    source = tmp_path / "Invalid.lean"
    source.write_bytes(b"\xff")
    session = _owned_session(_FakeProcess())

    with pytest.raises(UnicodeDecodeError):
        session.hover(str(source), 0, 0)

    assert session.is_alive() is True
    assert session._retire_pending is False


def test_abort_refuses_to_signal_a_pre_reaped_leader_with_live_group(monkeypatch):
    process = _FakeProcess()
    process.returncode = 0
    session = _owned_session(process)

    class StopRetry(BaseException):
        pass

    monkeypatch.setattr(
        lsp,
        "_process_group_has_live_members",
        lambda process_group_id: True,
    )
    monkeypatch.setattr(
        lsp,
        "_terminate_process_group",
        lambda process_group_id: pytest.fail("a reused process group was signalled"),
    )
    monkeypatch.setattr(
        lsp.time,
        "sleep",
        lambda delay: (_ for _ in ()).throw(StopRetry()),
    )

    with pytest.raises(StopRetry):
        session.abort()

    assert session.process is process
    assert session._process_group_id == process.pid
    assert session._group_retired is False


def test_concurrent_abort_and_close_serialize_owned_cleanup(monkeypatch):
    process = _FakeProcess()
    session = _owned_session(process)
    entered = threading.Event()
    release = threading.Event()
    active = 0
    maximum_active = 0
    errors: list[BaseException] = []
    state_lock = threading.Lock()

    def terminate(process_group_id):
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        entered.set()
        assert release.wait(timeout=2)
        with state_lock:
            active -= 1

    monkeypatch.setattr(lsp, "_terminate_process_group", terminate)
    monkeypatch.setattr(lsp, "_reap_process", lambda owned: owned.wait())

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
def test_close_retires_a_descendant_without_pre_reaping_its_wrapper():
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
    session = _owned_session(process)
    try:
        import psutil

        deadline = time.monotonic() + 5
        while psutil.Process(process.pid).status() != psutil.STATUS_ZOMBIE:
            if time.monotonic() >= deadline:
                pytest.fail("LSP wrapper did not become a zombie")
            time.sleep(0.01)
        assert process.returncode is None
        assert session.is_alive() is False

        session.close()

        assert session.process is None
        assert process.returncode == 0
        try:
            assert psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
    finally:
        if process.returncode is None:
            session.abort()


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


def test_lsp_json_integer_limit_is_reported_as_a_protocol_error():
    body = b'{"jsonrpc":"2.0","id":' + (b"9" * 5000) + b"}"
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = SimpleNamespace(stdout=reader)
    try:
        os.write(write_fd, frame)
        with pytest.raises(lsp.LspProtocolError, match="invalid JSON body"):
            session._read_message(timeout=1)
    finally:
        os.close(write_fd)
        reader.close()


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
