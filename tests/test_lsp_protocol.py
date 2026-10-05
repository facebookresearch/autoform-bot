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


class _FakeProcess:
    def __init__(self, *, pid: int = 43210) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.killed = False
        self.waited = False
        self.stdin = _FakeStream()
        self.stdout = _FakeStream()
        self.stderr = None

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


class _FakeStream:
    def __init__(
        self,
        *,
        failures: int = 0,
        errors: list[BaseException] | None = None,
        events: list[str] | None = None,
        name: str = "",
    ) -> None:
        self.failures = failures
        self.errors = list(errors or [])
        self.events = events
        self.name = name
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.events is not None:
            self.events.append(f"close:{self.name}")
        if self.failures:
            self.failures -= 1
            raise OSError(f"cannot close {self.name}")
        if self.errors:
            raise self.errors.pop(0)


class _Cancellation(BaseException):
    pass


def _finish_fake_process(process: _FakeProcess) -> None:
    process.killed = True
    process.returncode = -9
    process.waited = True


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
        "_kill_subprocesses",
        lambda owned, process_group_id: _finish_fake_process(owned),
    )

    session = lsp.LeanLspSession(lsp.LspConfig())

    def fail_initialize(method, params):
        raise lsp.LspProtocolError("initialize rejected")

    monkeypatch.setattr(session, "_send_request", fail_initialize)

    with pytest.raises(lsp.LspProtocolError, match="initialize rejected"):
        session.start()

    assert process.killed is True
    assert process.waited is True
    assert popen_kwargs["start_new_session"] is True
    assert session.process is None
    assert session._process_group_id is None
    assert session.is_clean()


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
        _finish_fake_process(owned)

    monkeypatch.setattr(lsp, "_kill_subprocesses", retire)
    session = lsp.LeanLspSession(lsp.LspConfig())
    monkeypatch.setattr(
        session,
        "_send_request",
        lambda method, params: (_ for _ in ()).throw(
            lsp.LspProtocolError("initialize rejected")
        ),
    )

    with pytest.raises(lsp.LspProtocolError, match="initialize rejected"):
        session.start()

    assert attempts == 2
    assert session.is_clean()


def test_startup_publication_cancellation_still_retires_the_uncached_child(monkeypatch):
    process = _FakeProcess()
    session = lsp.LeanLspSession(lsp.LspConfig())
    monkeypatch.setattr(lsp.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        session,
        "_publish_process_group",
        lambda owned: (_ for _ in ()).throw(
            _Cancellation("cancel process-group publication")
        ),
    )
    retired = []

    def retire(owned, process_group_id):
        retired.append((owned, process_group_id))
        _finish_fake_process(owned)

    monkeypatch.setattr(lsp, "_kill_subprocesses", retire)

    with pytest.raises(_Cancellation, match="cancel process-group publication"):
        session.start()

    assert retired == [(process, process.pid)]
    assert session.is_clean()


def test_startup_lock_exit_cancellation_retires_the_initialized_child(monkeypatch):
    process = _FakeProcess()
    session = lsp.LeanLspSession(lsp.LspConfig())
    inner_lock = threading.Lock()

    class InterruptingLock:
        def __init__(self):
            self.interrupt = True

        def __enter__(self):
            inner_lock.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            inner_lock.release()
            if self.interrupt:
                self.interrupt = False
                raise _Cancellation("cancel lifecycle-lock exit")
            return False

    session._lifecycle_lock = InterruptingLock()
    monkeypatch.setattr(lsp.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(session, "_send_request", lambda method, params: {})
    monkeypatch.setattr(session, "_send_notification", lambda method, params: None)
    retired = []

    def retire(owned, process_group_id):
        retired.append((owned, process_group_id))
        _finish_fake_process(owned)

    monkeypatch.setattr(lsp, "_kill_subprocesses", retire)

    with pytest.raises(_Cancellation, match="cancel lifecycle-lock exit"):
        session.start()

    assert retired == [(process, process.pid)]
    assert session.is_clean()


def test_close_preserves_cancellation_until_process_and_pipes_are_clean(monkeypatch):
    process = _FakeProcess()
    stdin = process.stdin
    stdout = process.stdout
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = process
    session._process_group_id = process.pid
    session._group_retired = False
    events = []

    def cancel_shutdown(method, params):
        events.append("cancel")
        raise _Cancellation("cancel shutdown")

    def retire(owned, process_group_id):
        events.append("retire")
        _finish_fake_process(owned)

    monkeypatch.setattr(session, "_send_request", cancel_shutdown)
    monkeypatch.setattr(lsp, "_kill_subprocesses", retire)

    with pytest.raises(_Cancellation, match="cancel shutdown"):
        session.close()

    assert events == ["cancel", "retire"]
    assert stdin.close_calls == 1
    assert stdout.close_calls == 1
    assert session.is_clean()


def test_cleanup_retry_retains_ownership_and_never_resignals_after_group_exit(monkeypatch):
    events: list[str] = []
    process = _FakeProcess()
    process.stdin = _FakeStream(failures=1, events=events, name="stdin")
    process.stdout = _FakeStream(events=events, name="stdout")
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = process
    session._process_group_id = process.pid
    session._group_retired = False
    session._retire_pending = True
    retire_calls = 0

    def retire(owned, process_group_id):
        nonlocal retire_calls
        retire_calls += 1
        events.append("retire")
        _finish_fake_process(owned)

    monkeypatch.setattr(lsp, "_kill_subprocesses", retire)
    monkeypatch.setattr(lsp.time, "sleep", lambda delay: events.append("retry"))

    session.abort()

    assert events == ["retire", "close:stdin", "close:stdout", "retry", "close:stdin"]
    assert retire_calls == 1
    assert process.stdin is None
    assert process.stdout is None
    assert session.is_clean()


def test_cancellation_between_group_retirement_stores_never_resignals(monkeypatch):
    class Session(lsp.LeanLspSession):
        def __setattr__(self, name, value):
            if (
                name == "_group_retired"
                and value is True
                and getattr(self, "interrupt_retirement_store", False)
            ):
                object.__setattr__(self, name, value)
                object.__setattr__(self, "interrupt_retirement_store", False)
                raise _Cancellation("cancel retirement publication")
            super().__setattr__(name, value)

    process = _FakeProcess()
    session = Session(lsp.LspConfig())
    session.process = process
    session._process_group_id = process.pid
    session._group_retired = False
    session._retire_pending = True
    session.interrupt_retirement_store = True
    retire_calls = 0

    def retire(owned, process_group_id):
        nonlocal retire_calls
        retire_calls += 1
        _finish_fake_process(owned)

    monkeypatch.setattr(lsp, "_kill_subprocesses", retire)

    with pytest.raises(_Cancellation, match="cancel retirement publication"):
        session.abort()

    assert retire_calls == 1
    assert session.is_clean()


def test_pipe_cleanup_preserves_cancellation_over_an_ordinary_close_error(monkeypatch):
    events: list[str] = []
    cancellation = _Cancellation("cancel pipe cleanup")
    process = _FakeProcess()
    process.stdin = _FakeStream(failures=1, events=events, name="stdin")
    process.stdout = _FakeStream(
        errors=[cancellation],
        events=events,
        name="stdout",
    )
    stdin = process.stdin
    stdout = process.stdout
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = process
    session._process_group_id = process.pid
    session._group_retired = False
    session._retire_pending = True
    retire_calls = 0

    def retire(owned, process_group_id):
        nonlocal retire_calls
        retire_calls += 1
        events.append("retire")
        _finish_fake_process(owned)

    monkeypatch.setattr(lsp, "_kill_subprocesses", retire)

    with pytest.raises(_Cancellation, match="cancel pipe cleanup"):
        session.abort()

    assert retire_calls == 1
    assert session.is_clean()
    assert process.stdin is None
    assert process.stdout is None
    assert stdin.close_calls == 2
    assert stdout.close_calls == 2


def test_cleanup_retry_preserves_sleep_cancellation_until_cleanup_succeeds(monkeypatch):
    process = _FakeProcess()
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = process
    session._process_group_id = process.pid
    session._group_retired = False
    session._retire_pending = True
    attempts = 0
    sleeps = 0

    def retire(owned, process_group_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("retry cleanup")
        _finish_fake_process(owned)

    def interrupt_sleep(delay):
        nonlocal sleeps
        sleeps += 1
        raise _Cancellation("cancel retry sleep")

    monkeypatch.setattr(lsp, "_kill_subprocesses", retire)
    monkeypatch.setattr(lsp.time, "sleep", interrupt_sleep)

    with pytest.raises(_Cancellation, match="cancel retry sleep"):
        session.abort()

    assert attempts == 2
    assert sleeps == 1
    assert session.is_clean()


@pytest.mark.parametrize("operation", ["abort", "close"])
def test_retirement_transition_cannot_be_cancelled_before_cleanup(
    monkeypatch, operation
):
    process = _FakeProcess()
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = process
    session._process_group_id = process.pid
    session._group_retired = False
    session._retire_pending = True
    original_retire = session._retire_until_clean
    calls = 0

    def interrupt_before_cleanup(context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _Cancellation("cancel before cleanup")
        return original_retire(context)

    monkeypatch.setattr(session, "_retire_until_clean", interrupt_before_cleanup)
    monkeypatch.setattr(
        lsp,
        "_kill_subprocesses",
        lambda owned, process_group_id: _finish_fake_process(owned),
    )

    with pytest.raises(_Cancellation, match="cancel before cleanup"):
        getattr(session, operation)()

    assert calls == 2
    assert session.is_clean()


def test_failed_operation_poisons_a_queued_waiter_before_releasing_admission(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    first_entered = threading.Event()
    release_first = threading.Event()
    second_touched_transport = threading.Event()
    errors: list[BaseException] = []
    calls = 0
    calls_lock = threading.Lock()

    def diagnostics(file_path, *, timeout=None):
        nonlocal calls
        with calls_lock:
            calls += 1
            number = calls
        if number == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
            raise lsp.LspProtocolError("broken shared stream")
        second_touched_transport.set()
        return []

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

    assert not first.is_alive()
    assert not second.is_alive()
    assert not second_touched_transport.is_set()
    assert len(errors) == 2
    messages = {str(error) for error in errors}
    assert any("broken shared stream" in message for message in messages)
    assert any("session is retiring" in message for message in messages)


def test_verified_cleanup_does_not_make_a_poisoned_session_reusable(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    session._mark_retire_pending()
    session.abort()
    assert session.is_clean()
    assert not session.is_alive()
    monkeypatch.setattr(
        session,
        "_get_diagnostics",
        lambda *args, **kwargs: pytest.fail("a cleaned poisoned stream was reused"),
    )

    with pytest.raises(lsp.LspProtocolError, match="session is retiring"):
        session.get_diagnostics("ignored.lean")


@pytest.mark.parametrize("operation", ["diagnostics", "hover"])
def test_did_close_failure_poisons_a_queued_operation(
    tmp_path, monkeypatch, operation
):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig())
    close_entered = threading.Event()
    release_close = threading.Event()
    notifications = []
    errors: list[BaseException] = []

    def notify(method, params, **kwargs):
        notifications.append(method)
        if method == "textDocument/didClose":
            close_entered.set()
            assert release_close.wait(timeout=2)
            raise lsp.LspProtocolError("didClose write failed")

    monkeypatch.setattr(session, "_send_notification", notify)
    monkeypatch.setattr(session, "_collect_diagnostics", lambda uri, timeout: [])
    monkeypatch.setattr(
        session,
        "_send_request",
        lambda method, params, timeout=30: {
            "contents": {"kind": "plaintext", "value": "Nat : Type"}
        },
    )

    def run() -> None:
        try:
            if operation == "diagnostics":
                session.get_diagnostics(str(source))
            else:
                session.hover(str(source), 0, 0)
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert close_entered.wait(timeout=1)
    second.start()
    release_close.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(errors) == 2
    assert any("didClose write failed" in str(error) for error in errors)
    assert any("session is retiring" in str(error) for error in errors)
    assert notifications == ["textDocument/didOpen", "textDocument/didClose"]


def test_document_close_budget_exhaustion_poisons_the_session(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig(timeout=60))
    clock = iter([0.0, 0.0, 0.0, 0.0, 60.0])
    notifications = []
    monkeypatch.setattr(lsp.time, "monotonic", lambda: next(clock, 60.0))
    monkeypatch.setattr(
        session,
        "_send_notification",
        lambda method, params, **kwargs: notifications.append(method),
    )
    monkeypatch.setattr(session, "_collect_diagnostics", lambda uri, timeout: [])

    with pytest.raises(TimeoutError, match="before closing the document"):
        session.get_diagnostics(str(source))

    assert notifications == ["textDocument/didOpen"]
    assert session._poisoned is True
    assert session._retire_pending is True


@pytest.mark.parametrize("operation", ["diagnostics", "hover"])
def test_did_close_cancellation_outranks_an_ordinary_operation_error(
    tmp_path, monkeypatch, operation
):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig())
    operation_error = lsp.LspProtocolError("operation failed")
    close_cancellation = _Cancellation("cancel didClose")

    def notify(method, params, **kwargs):
        if method == "textDocument/didClose":
            raise close_cancellation

    monkeypatch.setattr(session, "_send_notification", notify)
    monkeypatch.setattr(
        session,
        "_collect_diagnostics",
        lambda uri, timeout: (_ for _ in ()).throw(operation_error),
    )
    monkeypatch.setattr(
        session,
        "_send_request",
        lambda method, params, timeout=30: (_ for _ in ()).throw(operation_error),
    )

    with pytest.raises(_Cancellation, match="cancel didClose") as raised:
        if operation == "diagnostics":
            session.get_diagnostics(str(source))
        else:
            session.hover(str(source), 0, 0)

    assert raised.value is close_cancellation
    assert raised.value.__cause__ is operation_error
    assert session._poisoned is True


@pytest.mark.parametrize("operation", ["diagnostics", "hover"])
def test_operation_cancellation_outranks_an_ordinary_did_close_error(
    tmp_path, monkeypatch, operation
):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig())
    operation_cancellation = _Cancellation("cancel operation")
    close_error = lsp.LspProtocolError("didClose failed")

    def notify(method, params, **kwargs):
        if method == "textDocument/didClose":
            raise close_error

    monkeypatch.setattr(session, "_send_notification", notify)
    monkeypatch.setattr(
        session,
        "_collect_diagnostics",
        lambda uri, timeout: (_ for _ in ()).throw(operation_cancellation),
    )
    monkeypatch.setattr(
        session,
        "_send_request",
        lambda method, params, timeout=30: (_ for _ in ()).throw(
            operation_cancellation
        ),
    )

    with pytest.raises(_Cancellation, match="cancel operation") as raised:
        if operation == "diagnostics":
            session.get_diagnostics(str(source))
        else:
            session.hover(str(source), 0, 0)

    assert raised.value is operation_cancellation
    assert raised.value.__cause__ is close_error
    assert session._poisoned is True


def test_concurrent_abort_and_close_serialize_owned_cleanup(monkeypatch):
    process = _FakeProcess()
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = process
    session._process_group_id = process.pid
    session._group_retired = False
    session._retire_pending = True
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
        _finish_fake_process(owned)
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
    assert closer.is_alive()
    release.set()
    aborter.join(timeout=2)
    closer.join(timeout=2)

    assert errors == []
    assert maximum_active == 1
    assert session.is_clean()


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
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline())
    assert process.wait(timeout=5) == 0
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = process
    session._process_group_id = process.pid
    session._group_retired = False
    try:
        session.close()
        assert session.is_clean()
        try:
            import psutil

            child = psutil.Process(child_pid)
            assert child.status() == psutil.STATUS_ZOMBIE
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
