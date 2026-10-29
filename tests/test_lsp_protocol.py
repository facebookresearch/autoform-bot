"""Protocol and lifecycle regression tests for the Lean server's LSP backend."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from servers._process import processes_with_token
from servers.lsp import server as lsp
from tests.lean_fixture import _real_lean_project, _requires_real_lean


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
        # No kernel allocates this PID, so real process-group probes find nothing.
        self.pid = 999_999_999
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


def _owned_session(process: _FakeProcess, token: str | None = None) -> lsp.LeanLspSession:
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = process
    session._process_group_id = process.pid
    session._token = token
    return session


def _session_env(token: str) -> dict[str, str]:
    """An environment carrying token, as start() gives the Lean server."""
    return {**os.environ, lsp._SESSION_TOKEN_ENV: token}


def _wait_until_gone(process: psutil.Process, timeout: float) -> bool:
    """Whether process exits or becomes a zombie in time; a reused PID counts as gone."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _kill_quietly(process: psutil.Process) -> None:
    # is_running() compares creation times, so a recycled PID is never signalled.
    try:
        if process.is_running():
            process.kill()
    except psutil.Error:
        pass


def test_json_rpc_error_response_raises_protocol_error(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    monkeypatch.setattr(
        session,
        "_read_message",
        lambda timeout, finish_by=None: {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "initialization failed"},
        },
    )

    with pytest.raises(lsp.LspProtocolError, match=r"\[-32603\]: initialization failed"):
        session._read_response(1)


def test_reading_a_response_without_a_process_fails_at_once():
    # _read_message used to return None at once, so _read_response spun until its deadline.
    session = lsp.LeanLspSession(lsp.LspConfig())

    with pytest.raises(RuntimeError, match="LSP process not running"):
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
    # Both pipes close once to let the watchdog exit, then again in cleanup.
    assert process.stdin.close_calls == process.stdout.close_calls == 2
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


def test_start_gives_each_server_its_own_token(monkeypatch):
    environments = []

    def popen(*args, env, **kwargs):
        environments.append(env)
        return _FakeProcess()

    monkeypatch.setattr(lsp.subprocess, "Popen", popen)
    sessions = [lsp.LeanLspSession(lsp.LspConfig()) for _ in range(2)]
    for session in sessions:
        monkeypatch.setattr(session, "_send_request", lambda method, params: {})
        monkeypatch.setattr(session, "_send_notification", lambda method, params: None)
        session.start()

    # A shared token would let one session's close stop the other's Lean.
    tokens = [environment[lsp._SESSION_TOKEN_ENV] for environment in environments]
    assert tokens == [session._token for session in sessions]
    assert tokens[0] != tokens[1]
    # Only the server gets it, not the runtime's REPL or diagnostics children.
    assert lsp._SESSION_TOKEN_ENV not in os.environ


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
    # The first failure hits the best-effort close that lets the watchdog
    # exit; the second fails the stream cleanup after the group is retired.
    process.stdin = _FakeStream(failures=2)
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
    assert process.stdin.close_calls == 3
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


class _FakeTokenCarrier:
    """A process a token scan reports, which records each signal and exits on it."""

    def __init__(self, signals: list[str]) -> None:
        self.signals = signals

    def send_signal(self, signal_number: int) -> None:
        self.signals.append(signal.Signals(signal_number).name)

    def is_running(self) -> bool:
        return False


def test_abort_retries_the_token_sweep_before_reaping_the_leader(monkeypatch):
    process = _FakeProcess()
    session = _owned_session(process, "session-token")
    events: list[str] = []
    # Each scan of the first attempt finds a process carrying the token, so
    # one is left after SIGKILL; the retry's first scan finds none.
    scans = iter([[_FakeTokenCarrier(events)]] * 3)

    def scan(name, token):
        assert name == lsp._SESSION_TOKEN_ENV
        assert token == "session-token"
        events.append("scan")
        return next(scans, [])

    def reap(owned):
        events.append("reap")
        owned.wait()

    monkeypatch.setattr(lsp, "processes_with_token", scan)
    monkeypatch.setattr(lsp, "_terminate_process_group", lambda process_group_id: events.append("group"))
    monkeypatch.setattr(lsp, "_reap_process", reap)
    monkeypatch.setattr(lsp.time, "sleep", lambda delay: None)

    session.abort()

    # The scan after SIGKILL failed the first attempt. The retry did not signal
    # the retired group again, and the leader was reaped only after a sweep
    # found nothing left.
    assert events == ["group", "scan", "SIGTERM", "scan", "SIGKILL", "scan", "scan", "reap"]
    assert session.process is None


def test_token_sweep_signals_only_what_its_latest_scan_found(monkeypatch):
    # The first scan's process no longer carries the token at the second, as
    # when its PID has passed to another process, so it gets no SIGKILL.
    session = _owned_session(_FakeProcess(), "session-token")
    signals: list[str] = []
    scans = iter([[_FakeTokenCarrier(signals)], []])
    monkeypatch.setattr(lsp, "processes_with_token", lambda name, token: next(scans, []))
    monkeypatch.setattr(lsp, "_terminate_process_group", lambda process_group_id: None)
    monkeypatch.setattr(lsp, "_reap_process", lambda owned: owned.wait())

    session.abort()

    assert signals == ["SIGTERM"]
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


def test_retirement_stops_a_queued_operation_before_dispatch(tmp_path, monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    source = tmp_path / "Queued.lean"
    source.write_text("#check Nat\n")
    calls = []
    errors = []
    started = threading.Event()
    session._operation_lock.acquire()
    monkeypatch.setattr(
        session,
        "_hover",
        lambda *args, **kwargs: calls.append("hover") or None,
    )

    def run():
        started.set()
        try:
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


def test_did_close_failure_keeps_result_but_retires_session(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = _owned_session(_FakeProcess())

    def notify(method, params, **kwargs):
        if method == "textDocument/didClose":
            raise TimeoutError("didClose write timed out")

    monkeypatch.setattr(session, "_send_notification", notify)
    monkeypatch.setattr(
        session,
        "_send_request",
        lambda method, params, timeout=30, finish_by=None: {"contents": "Nat : Type"},
    )

    assert session.hover(str(source), 0, 0) == "Nat : Type"

    assert session.is_alive() is False
    with pytest.raises(lsp.LspProtocolError, match="retiring"):
        session.hover(str(source), 0, 0)


def test_post_admission_budget_exhaustion_keeps_session_healthy(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = _owned_session(_FakeProcess())
    session.config.timeout = 0
    monkeypatch.setattr(lsp.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(
        session,
        "_hover",
        lambda *args, **kwargs: pytest.fail("expired admission ran hover"),
    )

    with pytest.raises(lsp.LspBusyError, match="waiting for the Lean LSP session"):
        session.hover(str(source), 0, 0)

    assert session.is_alive() is True
    assert session._retire_pending is False


def test_queued_hover_left_a_sliver_is_refused_before_it_writes(tmp_path, monkeypatch):
    # A write cut off midway desynchronizes Lean, so a hover that waited behind
    # others nearly its whole budget is refused, and the server stays.
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = _owned_session(_FakeProcess())
    clock = {"now": 0.0}

    class Queue:
        def acquire(self, timeout):
            clock["now"] = 59.5
            return True

        def release(self):
            pass

    monkeypatch.setattr(lsp.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(session, "_operation_lock", Queue())
    monkeypatch.setattr(session, "_hover", lambda *args, **kwargs: pytest.fail("a sliver of budget ran hover"))

    with pytest.raises(lsp.LspBusyError, match=r"60s \(LEAN_LSP_TIMEOUT\)"):
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

    def answer_at_deadline(method, params, timeout=30, finish_by=None):
        clock["now"] = 60.0
        return {"contents": "Nat : Type"}

    monkeypatch.setattr(session, "_send_request", answer_at_deadline)

    assert session.hover(str(source), 0, 0) == "Nat : Type"
    assert notifications == ["textDocument/didOpen"]
    assert session._retire_pending is True


def test_hover_input_decode_failure_keeps_healthy_session(tmp_path):
    source = tmp_path / "Invalid.lean"
    source.write_bytes(b"\xff")
    session = _owned_session(_FakeProcess())

    with pytest.raises(UnicodeDecodeError):
        session.hover(str(source), 0, 0)

    assert session.is_alive() is True


def test_abort_refuses_to_signal_a_pre_reaped_leader_with_live_group(monkeypatch):
    process = _FakeProcess()
    process.returncode = 0
    session = _owned_session(process)

    class StopRetry(BaseException):
        pass

    monkeypatch.setattr(
        lsp,
        "process_group_has_live_members",
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


@pytest.mark.skipif(os.name != "posix", reason="native LSP backend requires POSIX groups")
def test_token_scan_finds_only_the_processes_carrying_the_token():
    token = secrets.token_hex(16)
    untagged = {key: value for key, value in os.environ.items() if key != lsp._SESSION_TOKEN_ENV}
    processes = {
        name: subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=environment)
        for name, environment in {
            "this session": _session_env(token),
            "another session": _session_env(secrets.token_hex(16)),
            "no session": untagged,
        }.items()
    }
    try:
        # Popen returns once the child has exec'd, so its environment is set.
        assert [process.pid for process in processes_with_token(lsp._SESSION_TOKEN_ENV, token)] == [
            processes["this session"].pid
        ]
    finally:
        for process in processes.values():
            process.kill()
            process.wait()


def test_token_scan_leaves_out_processes_it_cannot_read(monkeypatch):
    class Candidate:
        def __init__(self, environment):
            self.environment = environment

        def environ(self):
            if isinstance(self.environment, BaseException):
                raise self.environment
            return self.environment

    tagged = {lsp._SESSION_TOKEN_ENV: "session-token"}
    candidates = {
        1: Candidate(tagged),
        # Such as a setuid program the user runs: it may carry the token,
        # but nothing shows that it does.
        2: Candidate(psutil.AccessDenied(2)),
        3: Candidate(psutil.ZombieProcess(3)),
        4: Candidate(psutil.NoSuchProcess(4)),
        os.getpid(): Candidate(tagged),
    }
    monkeypatch.setattr(psutil, "pids", lambda: list(candidates))
    monkeypatch.setattr(psutil, "Process", lambda pid: candidates[pid])

    assert processes_with_token(lsp._SESSION_TOKEN_ENV, "session-token") == [candidates[1]]


def test_token_scan_fails_when_the_process_table_cannot_be_read(monkeypatch):
    def unreadable():
        raise PermissionError("cannot list processes")

    monkeypatch.setattr(psutil, "pids", unreadable)

    # The attempt fails, so close and abort retry instead of reaping the leader.
    with pytest.raises(RuntimeError, match="cannot read the process table"):
        processes_with_token(lsp._SESSION_TOKEN_ENV, "session-token")


@pytest.mark.parametrize(
    "error",
    [OSError(errno.EIO, "cannot read the environment"), SystemError("inconsistent process state")],
    ids=["os-error", "system-error"],
)
def test_token_scan_fails_on_any_other_environment_error(monkeypatch, error):
    class Candidate:
        def environ(self):
            raise error

    monkeypatch.setattr(psutil, "pids", lambda: [1])
    monkeypatch.setattr(psutil, "Process", lambda pid: Candidate())

    # Only exited and unreadable processes are passed over; anything else
    # fails the attempt, so close and abort retry.
    with pytest.raises(type(error)):
        processes_with_token(lsp._SESSION_TOKEN_ENV, "session-token")


@pytest.mark.parametrize("token", [None, ""], ids=["none", "empty"])
def test_token_scan_refuses_a_missing_token(token):
    # A missing token would match every process without the variable.
    with pytest.raises(ValueError, match="nonempty"):
        processes_with_token(lsp._SESSION_TOKEN_ENV, token)


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
    try:
        assert entered.wait(timeout=1)
        closer.start()
        closer.join(timeout=0.1)
        assert closer.is_alive()
    finally:
        # Join while the patched helpers are still installed.
        release.set()
        aborter.join(timeout=5)
        if closer.ident is not None:
            closer.join(timeout=5)

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
    child = psutil.Process(int(process.stdout.readline()))
    session = _owned_session(process)
    try:
        assert _wait_until_gone(psutil.Process(process.pid), timeout=5), "LSP wrapper did not exit"
        assert session.is_alive() is False
        # is_alive() must not reap the leader, or its PGID could be reused.
        assert process.returncode is None

        session.close()

        assert session.process is None
        assert process.returncode == 0
        assert _wait_until_gone(child, timeout=0)
    finally:
        if process.returncode is None:
            session.abort()
        _kill_quietly(child)


def test_close_sends_exit_without_params(monkeypatch):
    process = _FakeProcess()
    session = _owned_session(process)
    written = []
    monkeypatch.setattr(session, "_send_request", lambda method, params, **kwargs: None)
    monkeypatch.setattr(session, "_write_message", lambda msg, **kwargs: written.append(msg))
    monkeypatch.setattr(lsp, "_wait_for_live_process_group_exit", lambda process_group_id, deadline: True)
    monkeypatch.setattr(lsp, "_terminate_process_group", lambda process_group_id: None)
    monkeypatch.setattr(lsp, "_reap_process", lambda owned: owned.wait())

    session.close()

    # Lean ignores an exit that carries params, so close() waited out its
    # whole grace period before signalling the group.
    assert written == [{"jsonrpc": "2.0", "method": "exit"}]
    assert session.process is None


# Like Lean's watchdog: each file worker runs in its own session. On stdin EOF
# it first writes more than a pipe holds, which blocks while stdout is open but
# unread and fails harmlessly once stdout is closed, then stops every worker
# group and exits. SIGTERM kills it before that cleanup can run.
_FAKE_WATCHDOG = (
    "import os, signal, subprocess, sys\n"
    "worker = subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True\n"
    ")\n"
    "print('worker', worker.pid, flush=True)\n"
    "sys.stdin.read()\n"
    "try:\n"
    "    os.write(1, b'x' * (4 << 20))\n"
    "except BrokenPipeError:\n"
    "    pass\n"
    "os.killpg(worker.pid, signal.SIGKILL)\n"
    "worker.wait()\n"
)


def _start_crashing_wrapper(
    watchdog: str, *, env: dict[str, str] | None = None, reports: int = 2
) -> tuple[subprocess.Popen, dict[str, psutil.Process]]:
    """Start watchdog under a wrapper that exits at once; return the wrapper and the reported processes by name."""
    wrapper = (
        "import subprocess, sys\n"
        f"server = subprocess.Popen([sys.executable, '-c', {watchdog!r}])\n"
        "print('server', server.pid, flush=True)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", wrapper],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    assert process.stdout is not None
    spawned = {}
    for _ in range(reports):
        name, pid = process.stdout.readline().split()
        spawned[name] = psutil.Process(int(pid))
    return process, spawned


@pytest.mark.skipif(os.name != "posix", reason="native LSP backend requires POSIX groups")
def test_abort_lets_the_watchdog_stop_workers_outside_the_group():
    process = subprocess.Popen(
        [sys.executable, "-c", _FAKE_WATCHDOG],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    worker = psutil.Process(int(process.stdout.readline().split()[1]))
    # Without a token there is no sweep, so only the watchdog can stop the worker.
    session = _owned_session(process)
    try:
        session.retire()
        session.close()

        assert session.process is None
        assert _wait_until_gone(worker, timeout=5)
    finally:
        _kill_quietly(worker)
        if session.process is not None:
            session.abort()


@pytest.mark.skipif(os.name != "posix", reason="native LSP backend requires POSIX groups")
def test_abort_after_the_wrapper_crashed_still_reaches_the_watchdog():
    # Like lake serve dying under a live lean --server: the wrapper exits and
    # leaves the watchdog in the group, still reading the same stdin.
    process, spawned = _start_crashing_wrapper(_FAKE_WATCHDOG)
    # Without a token there is no sweep, so only the watchdog can stop the worker.
    session = _owned_session(process)
    try:
        assert _wait_until_gone(psutil.Process(process.pid), timeout=5), "LSP wrapper did not exit"
        assert session.is_alive() is False
        assert process.returncode is None

        session.close()

        assert session.process is None
        assert _wait_until_gone(spawned["worker"], timeout=5)
        assert _wait_until_gone(spawned["server"], timeout=5)
    finally:
        for child in spawned.values():
            _kill_quietly(child)
        if session.process is not None:
            session.abort()


_FAKE_WORKER = (
    "import subprocess, sys\n"
    "build = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(build.pid, flush=True)\n"
    "sys.stdin.read()\n"
)
# Like Lean's watchdog when a file worker exits on its exit notification before
# shutdown gets to kill it: the worker's build keeps running in the worker's
# group, and the watchdog exits without signalling that group.
_SKIPPING_WATCHDOG = (
    "import subprocess, sys\n"
    "worker = subprocess.Popen(\n"
    f"    [sys.executable, '-c', {_FAKE_WORKER!r}],\n"
    "    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True,\n"
    ")\n"
    "print('build', worker.stdout.readline().strip(), flush=True)\n"
    "sys.stdin.read()\n"
    "worker.stdin.close()\n"
    "worker.wait()\n"
)


@pytest.mark.skipif(os.name != "posix", reason="native LSP backend requires POSIX groups")
@pytest.mark.parametrize("retired", [True, False], ids=["abort", "graceful"])
def test_close_stops_a_build_the_watchdog_left_running(monkeypatch, retired):
    token = secrets.token_hex(16)
    process = subprocess.Popen(
        [sys.executable, "-c", _SKIPPING_WATCHDOG],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=_session_env(token),
        start_new_session=True,
    )
    assert process.stdout is not None
    build = psutil.Process(int(process.stdout.readline().split()[1]))
    session = _owned_session(process, token)
    if retired:
        session.retire()
    else:
        def shutdown(method, params, **kwargs):
            # Stands in for Lean's shutdown request, which stops the workers
            # as an EOF does; the watchdog is gone before retirement starts.
            process.stdin.close()
            _wait_until_gone(psutil.Process(process.pid), timeout=5)

        monkeypatch.setattr(session, "_send_request", shutdown)
        monkeypatch.setattr(session, "_send_notification", lambda method, params, **kwargs: None)
    try:
        session.close()

        assert session.process is None
        assert _wait_until_gone(build, timeout=0)
    finally:
        _kill_quietly(build)
        if session.process is not None:
            session.abort()


@pytest.mark.skipif(os.name != "posix", reason="native LSP backend requires POSIX groups")
def test_close_stops_a_build_left_running_after_the_wrapper_crashed():
    # Like lake serve dying under a live lean --server that then skips a
    # worker: the watchdog and the build no longer descend from the leader.
    token = secrets.token_hex(16)
    process, spawned = _start_crashing_wrapper(_SKIPPING_WATCHDOG, env=_session_env(token))
    session = _owned_session(process, token)
    try:
        assert _wait_until_gone(psutil.Process(process.pid), timeout=5), "LSP wrapper did not exit"

        session.close()

        assert session.process is None
        assert _wait_until_gone(spawned["build"], timeout=0)
    finally:
        for child in spawned.values():
            _kill_quietly(child)
        if session.process is not None:
            session.abort()


# Like lean --server with a file open: the file worker runs in its own session
# and starts a build, and neither reads from the server. The build ignores
# SIGTERM before it reports, so only the sweep's SIGKILL stops it.
_STUBBORN_BUILD = (
    "import os, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('build', os.getpid(), flush=True)\n"
    "time.sleep(60)\n"
)
_ORPHANED_WORKER = (
    "import subprocess, sys, time\n"
    f"subprocess.Popen([sys.executable, '-c', {_STUBBORN_BUILD!r}])\n"
    "time.sleep(60)\n"
)
_SERVER_WITH_A_WORKER = (
    "import subprocess, sys\n"
    f"worker = subprocess.Popen([sys.executable, '-c', {_ORPHANED_WORKER!r}], start_new_session=True)\n"
    "print('worker', worker.pid, flush=True)\n"
    "worker.wait()\n"
)


@pytest.mark.skipif(os.name != "posix", reason="native LSP backend requires POSIX groups")
def test_close_stops_workers_orphaned_before_close():
    # Like lean --server dying before close starts, which leaked every time in
    # #251: its worker and the worker's build run on, reparented, outside every
    # group the session started, and no process left descends from the leader.
    token = secrets.token_hex(16)
    process, spawned = _start_crashing_wrapper(_SERVER_WITH_A_WORKER, env=_session_env(token), reports=3)
    session = _owned_session(process, token)
    try:
        spawned["server"].kill()
        assert _wait_until_gone(spawned["server"], timeout=5)
        assert _wait_until_gone(psutil.Process(process.pid), timeout=5), "LSP wrapper did not exit"
        assert not _wait_until_gone(spawned["worker"], timeout=0)
        assert not _wait_until_gone(spawned["build"], timeout=0)

        session.close()

        assert session.process is None
        assert _wait_until_gone(spawned["worker"], timeout=0)
        assert _wait_until_gone(spawned["build"], timeout=0)
    finally:
        for child in spawned.values():
            _kill_quietly(child)
        if session.process is not None:
            session.abort()


def _command_mentions(process: psutil.Process, text: str) -> bool:
    try:
        return any(text in part for part in process.cmdline())
    except psutil.Error:
        return False


def _open_until(
    session,
    project: Path,
    leader: psutil.Process,
    marker: str,
    descendants: set[psutil.Process],
    module: str = "Main",
) -> None:
    """Open Probe/<module>.lean and record descendants until one's command mentions marker."""
    source = project / "Probe" / f"{module}.lean"
    session._send_notification(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": source.as_uri(),
                "languageId": "lean4",
                "version": 1,
                "text": source.read_text(encoding="utf-8"),
            }
        },
    )
    deadline = time.monotonic() + 120
    while not any(_command_mentions(child, marker) for child in descendants):
        if time.monotonic() >= deadline:
            pytest.fail(f"Lean never started a process mentioning {marker}")
        descendants.update(leader.children(recursive=True))
        time.sleep(0.1)
    descendants.update(leader.children(recursive=True))


@_requires_real_lean
def test_real_lean_abort_stops_file_workers(tmp_path):
    # Building the import blocks, so the file worker's `lake setup-file` and
    # its `lean ... -o` child are still running when cleanup starts.
    project = _real_lean_project(tmp_path / "abort", "import Probe.Slow\n", Slow="#eval IO.sleep 300000\n")
    session = lsp.LeanLspSession(lsp.LspConfig(cwd=str(project), timeout=120))
    session.start()
    leader = psutil.Process(session.process.pid)
    descendants: set[psutil.Process] = set()
    try:
        # A hover that runs out of time leaves the document open like this.
        _open_until(session, project, leader, "Slow.lean", descendants)

        session.retire()
        session.close()

        assert session.process is None
        assert [child for child in descendants if not _wait_until_gone(child, timeout=10)] == []
    finally:
        for child in descendants:
            _kill_quietly(child)
        if session.process is not None:
            session.abort()


@_requires_real_lean
def test_real_lean_abort_stops_the_workers_of_every_open_file(tmp_path):
    # Lean's watchdog kills workers in URI order and skips any that has
    # already exited, so Second.lean's worker, which sorts after Main.lean's,
    # can exit on its exit notification before its turn and leave its build
    # running. Whether it does is a race, so this test can pass without the
    # token sweep; the _SKIPPING_WATCHDOG tests cannot. Hover keeps at most one
    # document open, so both files are opened directly.
    project = _real_lean_project(
        tmp_path / "abort-two",
        "theorem probe : 1 + 1 = 2 := rfl\n",
        Second="import Probe.Slow\n",
        Slow="#eval IO.sleep 300000\n",
    )
    session = lsp.LeanLspSession(lsp.LspConfig(cwd=str(project), timeout=120))
    session.start()
    leader = psutil.Process(session.process.pid)
    descendants: set[psutil.Process] = set()
    try:
        _open_until(session, project, leader, "--worker", descendants)
        _open_until(session, project, leader, "Slow.lean", descendants, module="Second")

        session.retire()
        session.close()

        assert session.process is None
        assert [child for child in descendants if not _wait_until_gone(child, timeout=10)] == []
    finally:
        for child in descendants:
            _kill_quietly(child)
        if session.process is not None:
            session.abort()


@_requires_real_lean
def test_real_lean_close_stops_workers_orphaned_before_close(tmp_path):
    # Killing lean --server orphans its file worker, the worker's `lake
    # setup-file` and the `lean ... -o` building Slow.lean, which keep
    # running; #251 saw them outlive close every time.
    project = _real_lean_project(tmp_path / "orphaned", "import Probe.Slow\n", Slow="#eval IO.sleep 300000\n")
    session = lsp.LeanLspSession(lsp.LspConfig(cwd=str(project), timeout=120))
    session.start()
    leader = psutil.Process(session.process.pid)
    descendants: set[psutil.Process] = set()
    try:
        _open_until(session, project, leader, "Slow.lean", descendants)
        server = next(child for child in descendants if _command_mentions(child, "--server"))
        server.kill()
        assert _wait_until_gone(server, timeout=10)
        builds = [child for child in descendants if _command_mentions(child, "Slow.lean")]
        assert any(not _wait_until_gone(build, timeout=0) for build in builds), "the build died with the server"

        session.retire()
        session.close()

        assert session.process is None
        assert [child for child in descendants if not _wait_until_gone(child, timeout=10)] == []
    finally:
        for child in descendants:
            _kill_quietly(child)
        if session.process is not None:
            session.abort()


@_requires_real_lean
def test_real_lean_graceful_close_is_prompt_and_complete(tmp_path):
    project = _real_lean_project(tmp_path / "graceful", "theorem probe : 1 + 1 = 2 := rfl\n")
    session = lsp.LeanLspSession(lsp.LspConfig(cwd=str(project), timeout=120))
    session.start()
    leader = psutil.Process(session.process.pid)
    descendants: set[psutil.Process] = set()
    try:
        # Opened directly, so a file worker is still running when close() starts.
        _open_until(session, project, leader, "--worker", descendants)

        started = time.monotonic()
        session.close()

        # An exit with params left Lean running until the 5s grace expired.
        assert time.monotonic() - started < 4
        assert session.process is None
        assert [child for child in descendants if not _wait_until_gone(child, timeout=10)] == []
    finally:
        for child in descendants:
            _kill_quietly(child)
        if session.process is not None:
            session.abort()


@_requires_real_lean
def test_real_lean_hover_reopens_a_file_in_one_session(tmp_path):
    project = _real_lean_project(tmp_path / "hover", "def probe : Nat := 3\n")
    main = project / "Probe" / "Main.lean"
    session = lsp.LeanLspSession(lsp.LspConfig(cwd=str(project)))
    session.start()
    try:
        assert "Nat" in session.hover(str(main), 0, 4)
        # The second hover reopens the file the first one closed, with new text.
        main.write_text("def probe : Int := 3\n", encoding="utf-8")
        assert "Int" in session.hover(str(main), 0, 4)
        assert session.is_alive() is True
    finally:
        session.close()


@_requires_real_lean
def test_real_lean_hover_that_runs_out_keeps_the_server(tmp_path):
    # #272: Lean elaborates nothing below the sleeping #eval, so it cannot answer a
    # hover on line 1. Closing the file makes it answer, so the server keeps
    # running and serves the next hover.
    project = _real_lean_project(
        tmp_path / "slow", "def probe : Nat := 3\n", Slow="#eval IO.sleep 120000\ndef late : Nat := 1\n"
    )
    session = lsp.LeanLspSession(lsp.LspConfig(cwd=str(project), timeout=20))
    session.start()
    try:
        with pytest.raises(lsp.LspHoverTimeout, match="LEAN_LSP_TIMEOUT"):
            session.hover(str(project / "Probe" / "Slow.lean"), 1, 4)
        assert session.is_alive() is True
        assert "Nat" in session.hover(str(project / "Probe" / "Main.lean"), 0, 4)
    finally:
        session.close()


@_requires_real_lean
def test_real_lean_hover_that_runs_out_while_building_leaves_nothing_running(tmp_path):
    # A hover on a file whose import is unbuilt waits while Lake builds it, and the
    # build sleeps. Closing the file makes the worker exit, and Lean stops the
    # worker's process group, its build included, so the server stays with
    # nothing of the file left running.
    project = _real_lean_project(
        tmp_path / "building",
        "import Probe.Slow\ndef probe : Nat := late\n",
        Slow="#eval IO.sleep 120000\ndef late : Nat := 1\n",
    )
    session = lsp.LeanLspSession(lsp.LspConfig(cwd=str(project), timeout=20))
    session.start()
    try:
        with pytest.raises(lsp.LspHoverTimeout, match="LEAN_LSP_TIMEOUT"):
            session.hover(str(project / "Probe" / "Main.lean"), 1, 4)
        assert session.is_alive() is True
        assert session._left_running(time.monotonic() + 5) is False
    finally:
        session.close()


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

    def send_request(method, params, timeout=30, finish_by=None):
        assert method == "textDocument/hover"
        assert params["textDocument"]["uri"] == source.resolve().as_uri()
        return {"contents": {"kind": "plaintext", "value": "Nat : Type"}}

    monkeypatch.setattr(session, "_send_request", send_request)

    assert session.hover(str(source), 0, 7) == "Nat : Type"
    assert [method for method, _ in notifications] == [
        "textDocument/didOpen",
        "textDocument/didClose",
    ]
    # Lean's watchdog exits on a didOpen it cannot parse, so pin every field.
    assert notifications[0][1] == {
        "textDocument": {
            "uri": source.resolve().as_uri(),
            "languageId": "lean4",
            "version": 1,
            "text": source_text,
        }
    }


def test_hover_skips_a_server_request_that_reuses_its_id(tmp_path: Path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = _owned_session(_FakeProcess())
    monkeypatch.setattr(session, "_write_message", lambda msg, timeout=30: None)
    messages = iter(
        [
            # Lean's workers number their own requests from 0, so ids collide.
            {"jsonrpc": "2.0", "id": 1, "method": "workspace/semanticTokens/refresh"},
            {"jsonrpc": "2.0", "id": 1, "result": {"contents": "Nat : Type"}},
        ]
    )
    monkeypatch.setattr(session, "_read_message", lambda timeout=5, finish_by=None: next(messages))

    assert session.hover(str(source), 0, 7) == "Nat : Type"
    assert session.is_alive() is True


def test_hover_closes_the_document_after_a_failed_request(tmp_path: Path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig())
    notifications: list[str] = []

    monkeypatch.setattr(
        session,
        "_send_notification",
        lambda method, params, **kwargs: notifications.append(method),
    )

    def timeout(method, params, timeout=30, finish_by=None):
        raise TimeoutError("no hover reply")

    monkeypatch.setattr(session, "_send_request", timeout)

    with pytest.raises(TimeoutError, match="no hover reply"):
        session.hover(str(source), 0, 7)

    assert notifications == [
        "textDocument/didOpen",
        "textDocument/didClose",
    ]


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

    def request(method, params, timeout=30, finish_by=None):
        nonlocal calls
        with calls_lock:
            calls += 1
            number = calls
        if number == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        return None

    monkeypatch.setattr(session, "_send_request", request)
    threads = [
        threading.Thread(target=session.hover, args=(str(first), 0, 0)),
        threading.Thread(target=session.hover, args=(str(second), 0, 0)),
    ]
    threads[0].start()
    assert first_entered.wait(timeout=1)
    threads[1].start()

    # The second call cannot begin reading the shared stdout stream until the
    # first document's complete didOpen/hover/didClose lifecycle finishes.
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


def test_lsp_json_nesting_limit_is_reported_as_a_protocol_error(tmp_path: Path):
    # 200,000 levels overrun the JSON decoder's recursion limit (about 10,000
    # on Python 3.13, 1,000 on 3.10) in a body far under MAX_LSP_MESSAGE_BYTES.
    # A pipe cannot buffer the whole frame, so the session reads it from a file.
    body = b"[" * 200_000 + b"]" * 200_000
    frame = tmp_path / "frame"
    frame.write_bytes(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    session = lsp.LeanLspSession(lsp.LspConfig())
    with frame.open("rb", buffering=0) as reader:
        session.process = SimpleNamespace(stdout=reader)
        with pytest.raises(lsp.LspProtocolError, match="invalid JSON body"):
            session._read_message(timeout=1)


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


def _frame(msg):
    body = json.dumps(msg).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


_PROGRESS = {"jsonrpc": "2.0", "method": "$/lean/fileProgress", "params": {}}
_EMPTY_DIAGNOSTICS = {
    "jsonrpc": "2.0",
    "method": "textDocument/publishDiagnostics",
    "params": {"uri": "file:///Test.lean", "diagnostics": []},
}


@contextlib.contextmanager
def _paced_session(tmp_path, monkeypatch, on_write):
    """A session with a ten-second budget reading Lean's frames from a pipe on a fake clock.

    on_write(msg, now, arrivals, sent) schedules what Lean sends after each write,
    as (time, bytes) pairs in time order.
    """
    import select

    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    session = lsp.LeanLspSession(lsp.LspConfig(timeout=10))
    session.process = SimpleNamespace(stdout=reader)
    clock = {"now": 0.0}
    arrivals: list[tuple[float, bytes]] = []
    sent: list[str | None] = []
    real_select = select.select

    def paced_select(rlist, wlist, xlist, timeout):
        # Return at once while the pipe holds data, else at the next arrival
        # within the timeout, else after the whole timeout.
        ready, _, _ = real_select(rlist, wlist, xlist, 0)
        if not ready and arrivals and arrivals[0][0] <= clock["now"] + timeout:
            at, data = arrivals.pop(0)
            clock["now"] = max(clock["now"], at)
            os.write(write_fd, data)
            ready, _, _ = real_select(rlist, wlist, xlist, 0)
        if not ready:
            clock["now"] += timeout
        return ready, [], []

    def write(msg, timeout=30):
        sent.append(msg.get("method"))
        on_write(msg, clock["now"], arrivals, sent)

    monkeypatch.setattr(lsp.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(select, "select", paced_select)
    monkeypatch.setattr(session, "_write_message", write)
    try:
        yield SimpleNamespace(session=session, source=str(source), clock=clock, sent=sent)
    finally:
        os.close(write_fd)
        reader.close()


def test_hover_finishes_a_frame_whose_header_straddles_a_second(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    session = lsp.LeanLspSession(lsp.LspConfig(timeout=10))
    session.process = SimpleNamespace(stdout=reader)
    writers = []

    def write(msg, timeout=30):
        if msg.get("method") != "textDocument/hover":
            return
        # The hover is this fresh session's first request.
        reply = _frame({"jsonrpc": "2.0", "id": 1, "result": {"contents": "Nat : Type"}})
        os.write(write_fd, _frame(_PROGRESS) + reply[:7])

        def finish():
            # A pause inside the header must not cut the frame before the deadline.
            time.sleep(1.5)
            os.write(write_fd, reply[7:])

        writer = threading.Thread(target=finish, daemon=True)
        writers.append(writer)
        writer.start()

    monkeypatch.setattr(session, "_write_message", write)
    try:
        assert session.hover(str(source), 0, 7) == "Nat : Type"
        assert session.is_alive() is True
    finally:
        for writer in writers:
            writer.join(timeout=5)
        os.close(write_fd)
        reader.close()


def test_hover_fails_closed_when_lean_exits_mid_wait(tmp_path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    session = lsp.LeanLspSession(lsp.LspConfig(timeout=10))
    session.process = SimpleNamespace(stdout=reader)

    def write(msg, timeout=30):
        if msg.get("method") == "textDocument/hover":
            # A progress notification, then Lean dies before it answers.
            os.write(write_fd, _frame(_PROGRESS))
            os.close(write_fd)

    monkeypatch.setattr(session, "_write_message", write)
    try:
        with pytest.raises(lsp.LspProtocolError, match="closed stdout"):
            session.hover(str(source), 0, 7)
        assert session.is_alive() is False
    finally:
        reader.close()


_SENT = ["textDocument/didOpen", "textDocument/hover"]
_CUT_BODY = b"Content-Length: 10\r\n\r\n{}"


_CLOSED = [*_SENT, "textDocument/didClose"]


@pytest.mark.parametrize(
    ("stall", "on_close", "message", "gave_up"),
    [
        ([(4.9, b"C")], [], "reading an LSP header", 8.75),
        ([(4.9, _CUT_BODY)], [], "reading an LSP body", 8.75),
        ([], [], "closing the file brought no answer", 10),
        ([], [(8.0, _CUT_BODY)], "closing the file brought no answer", 10),
    ],
    ids=["header", "body", "silence", "answer-cut-off"],
)
def test_hover_fails_at_its_deadline_when_the_reply_never_completes(
    tmp_path, monkeypatch, stall, on_close, message, gave_up
):
    def lean(msg, now, arrivals, sent):
        if msg.get("method") == "textDocument/hover":
            # A progress notification three seconds in, then a frame that starts
            # mid-read and stalls, or nothing more, while Lean stays alive.
            arrivals.extend([(3.0, _frame(_PROGRESS)), *stall])
        elif msg.get("method") == "textDocument/didClose":
            arrivals.extend(on_close)

    with _paced_session(tmp_path, monkeypatch, lean) as lsp_session:
        # The request stops waiting at 7.5, a quarter of the budget early, to leave
        # time to close the file; a frame already started may run on to 8.75, half
        # that time. Running out fails rather than returning no hover, and retires
        # the session.
        with pytest.raises(TimeoutError, match=message) as raised:
            lsp_session.session.hover(lsp_session.source, 0, 7)
        assert not isinstance(raised.value, (lsp.LspBusyError, lsp.LspHoverTimeout))
        assert lsp_session.clock["now"] == pytest.approx(gave_up)
        assert lsp_session.session.is_alive() is False
        assert lsp_session.sent == _CLOSED


def test_hover_finishes_a_reply_that_starts_before_it_stops_waiting(tmp_path, monkeypatch):
    # A frame cut off at 7.5 would leave the stream out of step, so one already
    # started may finish, by 8.75.
    reply = _frame({"jsonrpc": "2.0", "id": 1, "result": {"contents": "Nat : Type"}})

    def lean(msg, now, arrivals, sent):
        if msg.get("method") == "textDocument/hover":
            arrivals.extend([(7.4, reply[:5]), (8.0, reply[5:])])

    with _paced_session(tmp_path, monkeypatch, lean) as lsp_session:
        assert lsp_session.session.hover(lsp_session.source, 0, 7) == "Nat : Type"
        assert lsp_session.clock["now"] == pytest.approx(8)
        assert lsp_session.sent == _CLOSED
        assert lsp_session.session.is_alive() is True


def _answers_the_close(answer: dict):
    """Lean as a hover that runs out finds it: silent until the close, which it answers."""

    def lean(msg, now, arrivals, sent):
        if msg.get("id") == 2:
            arrivals.append((now + 0.1, _frame({"jsonrpc": "2.0", "id": 2, "result": {"contents": "Int : Type"}})))
        elif msg.get("method") == "textDocument/didClose" and len(sent) == 3:
            # Progress Lean sent before it saw the close comes first, and an empty
            # publishDiagnostics follows the answer.
            answered = _frame({"jsonrpc": "2.0", "id": 1, **answer})
            arrivals.extend(
                [(now + 0.2, _frame(_PROGRESS)), (now + 0.5, answered), (now + 0.6, _frame(_EMPTY_DIAGNOSTICS))]
            )

    return lean


_CONTENT_MODIFIED = {"error": {"code": -32801, "message": "The file worker for file:///Test.lean has been terminated."}}


@pytest.mark.parametrize(
    ("answer", "hovered"),
    [(_CONTENT_MODIFIED, None), ({"result": {"contents": "Nat : Type"}}, "Nat : Type")],
    ids=["content-modified", "late-hover"],
)
def test_hover_keeps_the_server_when_lean_answers_the_closed_file(tmp_path, monkeypatch, answer, hovered):
    # #272: closing a file makes Lean answer the hover still pending on it, with
    # contentModified unless the hover's own answer wins the race. Either shows
    # Lean still serves the project, so the server need not restart.
    with _paced_session(tmp_path, monkeypatch, _answers_the_close(answer)) as lsp_session:
        session = lsp_session.session
        if hovered is None:
            with pytest.raises(lsp.LspHoverTimeout, match=r"10s \(LEAN_LSP_TIMEOUT\)"):
                session.hover(lsp_session.source, 0, 7)
        else:
            assert session.hover(lsp_session.source, 0, 7) == hovered
        assert lsp_session.clock["now"] == pytest.approx(8)
        assert session.is_alive() is True
        assert session.hover(lsp_session.source, 0, 7) == "Int : Type"
        assert lsp_session.sent == _CLOSED * 2


def test_hover_restarts_the_server_when_the_closed_file_left_a_build_running(tmp_path, monkeypatch):
    # A worker that does not exit when its file closes keeps its build running.
    # Retiring the session stops both, so the server restarts.
    with _paced_session(tmp_path, monkeypatch, _answers_the_close(_CONTENT_MODIFIED)) as lsp_session:
        monkeypatch.setattr(lsp_session.session, "_left_running", lambda deadline: True)
        with pytest.raises(TimeoutError, match="still ran after the close") as raised:
            lsp_session.session.hover(lsp_session.source, 0, 7)
        assert not isinstance(raised.value, lsp.LspHoverTimeout)
        assert lsp_session.session.is_alive() is False


def test_hover_restarts_the_server_when_lean_answers_the_closed_file_malformed(tmp_path, monkeypatch):
    with _paced_session(tmp_path, monkeypatch, _answers_the_close({})) as lsp_session:
        with pytest.raises(lsp.LspProtocolError, match="neither result nor error") as raised:
            lsp_session.session.hover(lsp_session.source, 0, 7)
        assert not isinstance(raised.value, lsp.LspErrorReply)
        assert lsp_session.session.is_alive() is False


def test_hover_restarts_the_server_when_its_file_cannot_be_closed(tmp_path, monkeypatch):
    def lean(msg, now, arrivals, sent):
        if msg.get("method") == "textDocument/didClose":
            raise OSError("broken pipe")

    with _paced_session(tmp_path, monkeypatch, lean) as lsp_session:
        with pytest.raises(TimeoutError, match="closing the file failed") as raised:
            lsp_session.session.hover(lsp_session.source, 0, 7)
        assert not isinstance(raised.value, lsp.LspHoverTimeout)
        assert lsp_session.clock["now"] == pytest.approx(7.5)
        assert lsp_session.session.is_alive() is False


@pytest.mark.parametrize("left_running", [False, True], ids=["nothing-left", "build-left"])
def test_hover_keeps_the_server_after_an_error_reply_unless_a_build_is_left(tmp_path, monkeypatch, left_running):
    # An error reply arrives as a whole frame, so the stream is in step, but a
    # build a crashed worker started still has to be stopped, which retiring does.
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = _owned_session(_FakeProcess())
    methods: list[str] = []
    replies = iter(
        [
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32902, "message": "worker crashed"}},
            {"jsonrpc": "2.0", "id": 2, "result": {"contents": "Nat : Type"}},
        ]
    )
    monkeypatch.setattr(session, "_write_message", lambda msg, timeout=30: methods.append(msg.get("method")))
    monkeypatch.setattr(session, "_read_message", lambda timeout, finish_by=None: next(replies))
    monkeypatch.setattr(session, "_left_running", lambda deadline: left_running)

    with pytest.raises(lsp.LspErrorReply, match=r"\[-32902\]: worker crashed"):
        session.hover(str(source), 0, 0)
    assert session.is_alive() is not left_running
    if not left_running:
        assert session.hover(str(source), 0, 0) == "Nat : Type"
        assert methods == _CLOSED * 2


def test_a_token_process_outside_the_server_group_is_left_running():
    # Lean starts each file worker, and so its builds, in a session of its own.
    token = secrets.token_hex(8)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], env=_session_env(token), start_new_session=True
    )
    try:
        session = _owned_session(_FakeProcess(), token=token)
        assert session._left_running(time.monotonic() + 0.2) is True
        child.kill()
        child.wait()
        assert session._left_running(time.monotonic() + 0.2) is False
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


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
