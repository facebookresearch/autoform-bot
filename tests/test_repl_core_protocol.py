"""Focused contracts salvaged from the retired standalone REPL implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from servers import _process as process_group
from servers.repl import core as repl_core


def test_start_owns_a_posix_process_group(monkeypatch):
    captured = {}

    class Process:
        pid = 999_999_999

        def poll(self):
            return None

    process = Process()

    def popen(*args, **kwargs):
        captured.update(kwargs)
        return process

    monkeypatch.setattr(repl_core.subprocess, "Popen", popen)
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )

    repl.start()

    assert captured["start_new_session"] is True
    assert repl._process_group_id == process.pid
    repl.process = None
    repl._process_group_id = None


def test_start_rejects_unsupported_platform_before_spawning(monkeypatch):
    monkeypatch.setattr(repl_core.os, "name", "nt")
    monkeypatch.setattr(
        repl_core.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("unsupported transport must not spawn"),
    )
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )

    with pytest.raises(RuntimeError, match="requires a POSIX platform"):
        repl.start()


def test_start_retires_process_if_group_publication_is_interrupted(monkeypatch):
    class Process:
        pid_reads = 0

        @property
        def pid(self):
            self.pid_reads += 1
            if self.pid_reads == 1:
                raise KeyboardInterrupt
            return 4321

    process = Process()
    retired = []
    monkeypatch.setattr(repl_core.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        repl_core,
        "_kill_subprocesses",
        lambda candidate, process_group_id, deadline=None: retired.append(
            (candidate, process_group_id)
        ),
    )
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )

    with pytest.raises(KeyboardInterrupt):
        repl.start()

    assert retired == [(process, 4321)]
    assert repl.process is None
    assert repl._process_group_id is None


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_close_kills_descendant_after_repl_wrapper_already_exited():
    wrapper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess, sys; "
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(30)']); "
                "print(child.pid, flush=True)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert wrapper.stdout is not None
    child_pid = int(wrapper.stdout.readline())
    import psutil

    deadline = time.monotonic() + 2
    while psutil.Process(wrapper.pid).status() != psutil.STATUS_ZOMBIE:
        if time.monotonic() >= deadline:
            pytest.fail("REPL wrapper did not become a zombie")
        time.sleep(0.01)
    assert wrapper.returncode is None

    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )
    repl.process = wrapper
    repl._process_group_id = wrapper.pid
    try:
        repl.close()
        assert wrapper.returncode == 0
        try:
            assert psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
    finally:
        if wrapper.returncode is None:
            repl.close()
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_close_retains_process_handle_until_cleanup_succeeds(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )
    process = object()
    repl.process = process
    repl._process_group_id = 1234
    cleanup_calls = []

    def cleanup(candidate, process_group_id):
        cleanup_calls.append((candidate, process_group_id))
        if len(cleanup_calls) == 1:
            raise RuntimeError("cleanup failed")

    monkeypatch.setattr(repl_core, "_kill_subprocesses", cleanup)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        repl.close()

    assert repl.process is process
    assert repl._process_group_id == 1234

    repl.close()

    assert cleanup_calls == [(process, 1234), (process, 1234)]
    assert repl.process is None
    assert repl._process_group_id is None


def test_process_group_cleanup_escalates_and_reports_timeout(monkeypatch):
    signals = []
    live_results = iter((True, True))
    group_results = iter((False, False))
    process = object()

    monkeypatch.setattr(repl_core.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(
        repl_core.os,
        "killpg",
        lambda process_group_id, sent_signal: signals.append(
            (process_group_id, sent_signal)
        ),
    )
    monkeypatch.setattr(
        process_group,
        "_process_group_has_live_members",
        lambda process_group_id: next(live_results),
    )
    monkeypatch.setattr(
        process_group,
        "_wait_for_process",
        lambda candidate, deadline: pytest.fail(
            "the group leader must not be reaped while members remain live"
        ),
    )
    monkeypatch.setattr(
        process_group,
        "_wait_for_live_process_group_exit",
        lambda process_group_id, deadline: next(group_results),
    )

    with pytest.raises(RuntimeError, match="timed out terminating"):
        repl_core._kill_subprocesses(process, 1234)

    assert signals == [(1234, signal.SIGTERM), (1234, signal.SIGKILL)]


def test_process_group_cleanup_does_not_signal_an_all_zombie_group(monkeypatch):
    signals = []
    live_results = iter((True, False))

    monkeypatch.setattr(repl_core.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(
        repl_core.os,
        "killpg",
        lambda process_group_id, sent_signal: signals.append(
            (process_group_id, sent_signal)
        ),
    )
    monkeypatch.setattr(
        process_group,
        "_process_group_has_live_members",
        lambda process_group_id: next(live_results),
    )
    monkeypatch.setattr(
        process_group,
        "_wait_for_live_process_group_exit",
        lambda process_group_id, deadline: True,
    )
    monkeypatch.setattr(
        process_group,
        "_wait_for_process",
        lambda candidate, deadline: True,
    )

    repl_core._kill_subprocesses(object(), 1234)

    assert signals == [(1234, signal.SIGTERM)]


def test_process_group_cleanup_refuses_a_pre_reaped_leader_with_live_members(
    monkeypatch,
):
    process = SimpleNamespace(returncode=0)
    monkeypatch.setattr(
        process_group,
        "_process_group_has_live_members",
        lambda process_group_id: True,
    )
    monkeypatch.setattr(
        repl_core.os,
        "killpg",
        lambda *args: pytest.fail("a reused process group was signalled"),
    )

    with pytest.raises(RuntimeError, match="leader was reaped"):
        repl_core._kill_subprocesses(process, 1234)


def test_format_repl_response_prioritizes_errors_and_keeps_sorries():
    formatted = repl_core.format_repl_response(
        {
            "messages": [
                {"severity": "warning", "data": "unused"},
                {
                    "severity": "error",
                    "data": "unknown identifier",
                    "pos": {"line": 3, "column": 5},
                },
                "malformed message",
            ],
            "sorries": [{"goal": "Nat = Nat", "pos": {"line": 7}}],
        }
    )

    assert formatted == (
        "Compilation Errors (1)\n"
        "  - 3:5: error: unknown identifier\n"
        "\nSorries (1)\n"
        "  - Line 7: Nat = Nat"
    )
    assert "unused" not in formatted


def test_format_repl_response_truncates_diagnostics():
    formatted = repl_core.format_repl_response(
        {
            "messages": [
                {"severity": "warning", "data": f"warning {index}"}
                for index in range(repl_core.DEFAULT_MAX_DIAGNOSTICS + 2)
            ]
        }
    )

    assert "Warnings (12)" in formatted
    assert "warning 9" in formatted
    assert "warning 10" not in formatted
    assert "... and 2 more" in formatted


def test_format_repl_response_reports_explicit_repl_error():
    assert (
        repl_core.format_repl_response({"repl_error": "worker unavailable"})
        == "REPL error: worker unavailable"
    )


def test_format_repl_response_preserves_unknown_outcome_warning():
    assert repl_core.format_repl_response(
        {"repl_error": "response timed out", "outcome_unknown": True}
    ) == (
        "REPL error (execution outcome unknown; request not retried): "
        "response timed out"
    )


def test_additive_response_fields_are_tolerated_but_not_exported():
    response = {
        "env": 7,
        "infotree": {"future": True},
        "tactics": [{"proofState": 8}],
        "messages": [
            {
                "severity": "warning",
                "data": "warning",
                "pos": {"line": 1, "column": 2, "future": True},
                "future": True,
            }
        ],
        "sorries": [
            {
                "goal": "False",
                "proofState": 9,
                "future": True,
            }
        ],
    }

    repl_core._validate_command_response(response)

    assert repl_core._without_process_handles(response) == {
        "messages": [
            {
                "severity": "warning",
                "data": "warning",
                "pos": {"line": 1, "column": 2},
            }
        ],
        "sorries": [{"goal": "False"}],
    }


class _PipeProcess:
    def __init__(self, stack: ExitStack, stdout_chunks: list[bytes], stderr: bytes = b""):
        stdin_read, stdin_write = os.pipe()
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        self.stdin = stack.enter_context(os.fdopen(stdin_write, "wb", buffering=0))
        self.stdout = stack.enter_context(os.fdopen(stdout_read, "rb", buffering=0))
        self.stderr = stack.enter_context(os.fdopen(stderr_read, "rb", buffering=0))
        self._stdin_read = stack.enter_context(os.fdopen(stdin_read, "rb", buffering=0))
        self._stdout_write = stack.enter_context(os.fdopen(stdout_write, "wb", buffering=0))
        self._stderr_write = stack.enter_context(os.fdopen(stderr_write, "wb", buffering=0))
        self.stdout_chunks = list(stdout_chunks)
        self.stderr_bytes = stderr

    def poll(self):
        return None


def _repl_with_process(process: _PipeProcess | None, *, chunk_size: int = 4096, max_buffer_bytes: int = 1024):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            chunk_size=chunk_size,
            max_buffer_bytes=max_buffer_bytes,
            validate_imports=False,
            warmup_imports=frozenset(),
        )
    )
    repl.process = process
    return repl


def _disposable_repl_with_process(monkeypatch, process: _PipeProcess, **config):
    """Return a clean repl whose disposable call starts on ``process``."""
    # Start clean, as a pool slot does: the close() that opens a call would close the stand-in's pipes.
    repl = _repl_with_process(None, **config)
    monkeypatch.setattr(repl, "start", lambda: setattr(repl, "process", process))
    return repl


def _patch_pipe_reads(monkeypatch, process: _PipeProcess):
    real_read = os.read

    def take_chunk(chunks: list[bytes], size: int) -> bytes:
        if not chunks:
            return b""
        result = chunks[0][:size]
        chunks[0] = chunks[0][size:]
        if not chunks[0]:
            chunks.pop(0)
        return result

    def fake_read(fd: int, size: int) -> bytes:
        if fd == process.stdout.fileno():
            return take_chunk(process.stdout_chunks, size)
        if fd == process.stderr.fileno():
            if not process.stderr_bytes:
                raise BlockingIOError
            result = process.stderr_bytes[:size]
            process.stderr_bytes = process.stderr_bytes[size:]
            return result
        return real_read(fd, size)

    def fake_select(readable, writable, exceptional, timeout=None):
        if writable:
            return [], writable, []
        ready = []
        if process.stderr_bytes:
            ready.append(process.stderr.fileno())
        if process.stdout_chunks:
            ready.append(process.stdout.fileno())
        return ready, [], []

    monkeypatch.setattr(repl_core.os, "read", fake_read)
    monkeypatch.setattr(repl_core.select, "select", fake_select)


def test_disposable_call_uses_one_frame_and_removes_process_handles(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            validate_imports=True,
            warmup_imports=frozenset({"Mathlib"}),
        )
    )
    starts = []
    calls = []
    closes = []

    def close(**kwargs):
        closes.append(kwargs)
        repl.process = None
        repl._process_group_id = None

    def start():
        starts.append(True)
        repl.process = object()
        repl._process_group_id = 7

    monkeypatch.setattr(repl, "close", close)
    monkeypatch.setattr(repl, "start", start)
    monkeypatch.setattr(
        repl,
        "_check_header",
        lambda code, deadline: repl_core._LeanHeaderAnalysis((), True),
    )

    def run(code, timeout):
        calls.append(code)
        return {
            "env": 4,
            "messages": [
                {
                    "severity": "error",
                    "data": "unknown module prefix 'Mathlib'",
                    "pos": {"line": 1, "column": 0},
                    "endPos": {"line": 1, "column": 14},
                },
                {
                    "severity": "info",
                    "data": "message",
                    "pos": {"line": 2, "column": 1},
                    "endPos": {"line": 2, "column": 3},
                },
            ],
            "sorries": [
                {
                    "goal": "False",
                    "proofState": 8,
                    "pos": {"line": 3, "column": 1},
                    "endPos": {"line": 3, "column": 2},
                }
            ],
        }

    monkeypatch.setattr(repl, "_run", run)

    assert repl.run_disposable("#check Nat", timeout=3) == {
        "messages": [
            {
                "severity": "error",
                "data": "unknown module prefix 'Mathlib'",
                "pos": {"line": 1, "column": 0},
                "endPos": {"line": 1, "column": 14},
            },
            {
                "severity": "info",
                "data": "message",
                "pos": {"line": 1, "column": 1},
                "endPos": {"line": 1, "column": 3},
            },
        ],
        "sorries": [
            {
                "goal": "False",
                "pos": {"line": 2, "column": 1},
                "endPos": {"line": 2, "column": 2},
            }
        ],
    }
    assert len(starts) == 1
    assert calls == ["import Mathlib\n#check Nat"]
    assert len(closes) == 2


def test_disposable_call_uses_one_decreasing_phase_budget(monkeypatch):
    clock = {"now": 0.0}
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(warmup_imports=frozenset({"Mathlib"}))
    )
    budgets = []

    monkeypatch.setattr(repl_core.time, "monotonic", lambda: clock["now"])

    def check_header(code, deadline):
        assert deadline == 10
        clock["now"] = 2
        return repl_core._LeanHeaderAnalysis((), True)

    def start():
        budgets.append("startup")
        clock["now"] = 5

    def run(code, timeout):
        budgets.append(("command", timeout, code))
        return {"env": 1, "messages": []}

    monkeypatch.setattr(repl, "_check_header", check_header)
    monkeypatch.setattr(repl, "start", start)
    monkeypatch.setattr(repl, "_run", run)

    assert repl.run_disposable("#check Nat", timeout=10) == {"messages": []}
    assert budgets == [
        "startup",
        ("command", 5, "import Mathlib\n#check Nat"),
    ]


@pytest.mark.parametrize(
    ("expired_phase", "expected"),
    [
        ("header", {"repl_error": "Lean REPL header validation timed out after 3 seconds"}),
        ("startup", {"repl_error": "Lean REPL disposable child startup timed out after 3 seconds"}),
        ("command", {"repl_error": "Lean REPL combined command execution timed out after 3 seconds"}),
        (
            "dispatched",
            {
                "repl_error": "Lean REPL combined command execution timed out after 3 seconds",
                "outcome_unknown": True,
            },
        ),
    ],
)
def test_disposable_timeout_names_the_expired_phase(monkeypatch, expired_phase, expected):
    clock = {"now": 0.0}
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(warmup_imports=frozenset())
    )

    monkeypatch.setattr(repl_core.time, "monotonic", lambda: clock["now"])

    def check_header(code, deadline):
        if expired_phase == "header":
            raise TimeoutError("timed out checking the Lean header")
        if expired_phase == "startup":
            clock["now"] = 2
        return repl_core._LeanHeaderAnalysis((), True)

    def start():
        if expired_phase == "startup":
            pytest.fail("expired budget started a REPL child")
        if expired_phase == "command":
            clock["now"] = 2

    def run(**kwargs):
        if expired_phase != "dispatched":
            pytest.fail("expired budget dispatched the command")
        raise repl_core.ReplOutcomeUnknown("transport failed") from TimeoutError()

    monkeypatch.setattr(repl, "_check_header", check_header)
    monkeypatch.setattr(repl, "start", start)
    monkeypatch.setattr(repl, "_run", run)

    assert repl.run_disposable("#check Nat", timeout=1, budget=3) == expected


def test_disposable_backlog_adjusts_prefixed_positions(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            validate_imports=True,
            warmup_imports=frozenset({"Mathlib"}),
        )
    )

    monkeypatch.setattr(repl, "start", lambda: None)
    monkeypatch.setattr(
        repl,
        "_check_header",
        lambda code, deadline: repl_core._LeanHeaderAnalysis((), True),
    )
    response = {
        "env": 4,
        "messages": [
            {
                "severity": "error",
                "data": "unknown module prefix 'Mathlib'",
                "pos": {"line": 1, "column": 0},
            },
            {
                "severity": "error",
                "data": "bad",
                "pos": {"line": 2, "column": 1},
            },
        ],
    }
    monkeypatch.setattr(
        repl,
        "_run",
        lambda **kwargs: (_ for _ in ()).throw(
            repl_core.ReplStderrBacklog("stderr backlog", response)
        ),
    )

    assert repl.run_disposable("#check missing", timeout=3) == {
        "messages": [
            {
                "severity": "error",
                "data": "unknown module prefix 'Mathlib'",
                "pos": {"line": 1, "column": 0},
            },
            {
                "severity": "error",
                "data": "bad",
                "pos": {"line": 1, "column": 1},
            },
        ]
    }


@pytest.mark.parametrize(
    ("code", "analysis", "expected_frame"),
    [
        (
            "#check autoformWarmupMarker",
            repl_core._LeanHeaderAnalysis((), True),
            "import Mathlib\n#check autoformWarmupMarker",
        ),
        (
            "import Mathlib\n#check Nat",
            repl_core._LeanHeaderAnalysis(("Mathlib",), True),
            "import Mathlib\n#check Nat",
        ),
        (
            "module\npublic import REPL.Frontend\n",
            repl_core._LeanHeaderAnalysis(("REPL.Frontend",), False),
            "module\npublic import REPL.Frontend\n",
        ),
        (
            "prelude\nimport REPL.Frontend\n#check Nat",
            repl_core._LeanHeaderAnalysis(("REPL.Frontend",), False),
            "prelude\nimport REPL.Frontend\n#check Nat",
        ),
    ],
)
def test_unvalidated_disposable_composes_only_proven_ordinary_headers(
    monkeypatch, code, analysis, expected_frame
):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            validate_imports=False,
            warmup_imports=frozenset({"Mathlib"}),
        )
    )

    monkeypatch.setattr(repl, "start", lambda: None)
    monkeypatch.setattr(repl, "_check_header", lambda code, deadline: analysis)
    frames = []

    def run(**kwargs):
        frames.append(kwargs["code"])
        return {"env": 4, "messages": []}

    monkeypatch.setattr(repl, "_run", run)

    assert repl.run_disposable(code, timeout=3) == {"messages": []}
    assert frames == [expected_frame]


def test_disposable_call_does_not_return_a_result_before_verified_cleanup(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(validate_imports=False, warmup_imports=frozenset())
    )
    close_calls = 0

    def close(**kwargs):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 2:
            raise RuntimeError("cleanup failed")
        repl.process = None
        repl._process_group_id = None

    monkeypatch.setattr(repl, "close", close)
    monkeypatch.setattr(repl, "start", lambda: None)
    monkeypatch.setattr(
        repl,
        "_run",
        lambda **kwargs: {"env": 1, "messages": []},
    )

    with pytest.raises(repl_core.ReplCleanupError, match="retirement is required") as error:
        repl.run_disposable("#check Nat", timeout=1)

    assert error.value.result == {"messages": []}


def _repl_log(caplog) -> list[tuple[int, str]]:
    """Return the level and message of each repl_core record; caplog's root handler sees every logger."""
    return [(level, message) for name, level, message in caplog.record_tuples if name == repl_core.logger.name]


_MALFORMED_RESPONSE = {
    "repl_error": "Lean REPL returned a malformed response for the requested command.",
    "outcome_unknown": True,
}
_MALFORMED_LOGGED = f"Lean REPL call in . failed: {_MALFORMED_RESPONSE['repl_error']}"


@pytest.mark.parametrize(
    ("outcome", "expected", "logged"),
    [
        # The REPL's error variant means that Lean itself failed, here while finding its sysroot.
        (
            {"message": "process 'lean' exited with code 1"},
            {"repl_error": "process 'lean' exited with code 1"},
            ["Lean REPL call in . failed: process 'lean' exited with code 1"],
        ),
        ([], _MALFORMED_RESPONSE, [_MALFORMED_LOGGED]),
        (
            repl_core.ReplOutcomeUnknown("x"),
            {"repl_error": "x", "outcome_unknown": True},
            ["Lean REPL call in . failed: x"],
        ),
        (repl_core.ReplProcessExited("x"), {"repl_error": "x"}, ["Lean REPL call in . failed: x"]),
        (
            repl_core.ReplStderrBacklog("x", {"message": "bad"}),
            {"repl_error": "bad"},
            ["Lean REPL call in .: x", "Lean REPL call in . failed: bad"],
        ),
        (repl_core.ReplStderrBacklog("x", []), _MALFORMED_RESPONSE, ["Lean REPL call in .: x", _MALFORMED_LOGGED]),
        (
            repl_core.ReplStderrBacklog(
                "x",
                {"env": 7, "sorries": [{"goal": "False", "proofState": 9}]},
            ),
            {"sorries": [{"goal": "False"}]},
            ["Lean REPL call in .: x"],
        ),
    ],
)
def test_disposable_call_maps_each_response_outcome_without_a_retry(
    monkeypatch, caplog, outcome, expected, logged
):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(validate_imports=False, warmup_imports=frozenset())
    )
    frames = []

    def run(**kwargs):
        frames.append(kwargs["code"])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(repl, "start", lambda: None)
    monkeypatch.setattr(repl, "_run", run)

    with caplog.at_level(logging.WARNING, logger=repl_core.logger.name):
        assert repl.run_disposable("#eval 1", timeout=1) == expected

    assert frames == ["#eval 1"]
    assert _repl_log(caplog) == [(logging.WARNING, line) for line in logged]


@pytest.mark.parametrize(
    "raw_response",
    [
        {},
        {"messages": []},
        {"messages": {}},
        {
            "env": 1,
            "messages": [
                {
                    "severity": [],
                    "data": "boom",
                    "pos": {"line": 1, "column": 1},
                }
            ],
        },
        {
            "env": 1,
            "messages": [
                {
                    "severity": {},
                    "data": "boom",
                    "pos": {"line": 1, "column": 1},
                }
            ],
        },
        {"env": True},
        {"sorries": [{"goal": "False"}]},
    ],
)
def test_disposable_call_reports_every_malformed_response_as_unknown(
    monkeypatch, raw_response
):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(validate_imports=False, warmup_imports=frozenset())
    )
    frames = []
    monkeypatch.setattr(repl, "start", lambda: None)
    monkeypatch.setattr(
        repl,
        "_run",
        lambda **kwargs: frames.append(kwargs["code"]) or raw_response,
    )

    result = repl.run_disposable("#eval 1", timeout=1)

    assert result["outcome_unknown"] is True
    assert frames == ["#eval 1"]


def _fake_header_command(stdout: str, returncode: int = 0, stderr: str = "") -> list[str]:
    script = (
        "import sys; sys.stdin.read(); "
        f"sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r}); "
        f"sys.exit({returncode})"
    )
    return [sys.executable, "-c", script]


def _header_json(
    *modules: str,
    errors: tuple[str, ...] = (),
    is_module: bool = False,
    prelude: bool = False,
) -> str:
    def item(module: str, is_meta: bool = False) -> dict:
        return {"module": module, "importAll": False, "isExported": True, "isMeta": is_meta}

    entry: dict = {"errors": list(errors)}
    if not errors:
        implicit = [] if prelude else [item("Init"), item("Init", is_meta=True)]
        imports = implicit + [item(module) for module in modules]
        entry["result"] = {"imports": imports, "isModule": is_module, "isPrelude": prelude}
    return json.dumps({"imports": [entry]})


def _header_modules(command: list[str], *, timeout: float = 10, **config) -> list[str]:
    repl = repl_core.LeanRepl(repl_core.LeanReplConfig(header_command=command, **config))
    try:
        return list(repl._check_header("import Mathlib", time.monotonic() + timeout).modules)
    finally:
        repl.close()


def test_header_check_accepts_the_header_parser_schema():
    command = _fake_header_command(_header_json("Init", "Mathlib.Tactic"))

    assert _header_modules(command) == ["Mathlib.Tactic"]


def test_default_header_parser_selects_lean_from_the_lake_toolchain():
    command = repl_core.LeanReplConfig().header_command

    assert command[:3] == ["lake", "env", sys.executable]
    assert "LEAN_SYSROOT" in command[-2]
    assert command[-1] == repl_core.LEAN_HEADER_PARSER
    assert os.path.isfile(command[-1])


def test_header_parser_launcher_ignores_a_path_shadow(tmp_path):
    trusted_bin = tmp_path / "toolchain" / "bin"
    shadow_bin = tmp_path / "shadow"
    trusted_bin.mkdir(parents=True)
    shadow_bin.mkdir()

    def write_parser(path, module):
        path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"assert sys.argv[1:] == ['--run', {repl_core.LEAN_HEADER_PARSER!r}]\n"
            "sys.stdin.read()\n"
            f"sys.stdout.write({_header_json(module)!r})\n"
        )
        path.chmod(0o755)

    write_parser(trusted_bin / "lean", "Mathlib")
    write_parser(shadow_bin / "lean", "Unsafe")
    env = {
        "LEAN_SYSROOT": str(trusted_bin.parent),
        "PATH": f"{shadow_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    }

    modules = _header_modules(
        [sys.executable, "-c", repl_core.LEAN_HEADER_LAUNCHER, repl_core.LEAN_HEADER_PARSER],
        env=env,
    )

    assert modules == ["Mathlib"]


# Plain ``lean --deps-json`` output, in its older and current schemas. Its fast
# parser can miss imports that the REPL loads.
_LEGACY_INIT_ONLY = json.dumps(
    {"imports": [{"errors": [], "imports": [{"module": "Init", "importAll": False}]}]}
)
_FAST_PARSER_ORDINARY = json.dumps(
    {
        "imports": [
            {
                "errors": [],
                "result": {
                    "imports": [
                        {"importAll": False, "isExported": True, "isMeta": False, "module": "Init"},
                        {"importAll": False, "isExported": True, "isMeta": True, "module": "Init"},
                    ],
                    "isModule": False,
                },
            }
        ]
    }
)


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (_fake_header_command(_header_json(errors=("bad header",))), "bad header"),
        (
            _fake_header_command(_header_json("Mathlib.«/abs/path/Secret»")),
            "plain, nonempty module components",
        ),
        (
            _fake_header_command(_header_json("Mathlib.«..».Secret")),
            re.escape("import Mathlib.«..».Secret must use plain, nonempty module components"),
        ),
        # parse_header.lean prints this name escaped. Unescaped, as Name.toString
        # prints it, its root would read as the allowlisted Mathlib.
        (
            _fake_header_command(_header_json("«Mathlib.Unsafe».«x✝»")),
            "plain, nonempty module components",
        ),
    ],
)
def test_header_check_rejects_parse_errors_and_ambiguous_module_components(command, message):
    with pytest.raises(ValueError, match=message):
        _header_modules(command)


# The parser reports parse errors as JSON, so these are failures of the check.
@pytest.mark.parametrize(
    ("command", "message"),
    [
        (_fake_header_command("", returncode=1, stderr="unknown package\n"), "unknown package"),
        (_fake_header_command("not json"), "unrecognized output"),
        (
            _fake_header_command(
                '{"imports":[{"errors":[],"result":{"isModule":false,"isPrelude":false,'
                '"imports":[{"module":"Unsafe","isMeta":false}],"imports":[]}}]}'
            ),
            "unrecognized output",
        ),
        (
            _fake_header_command(
                '{"imports":[{"errors":[],"result":{"isModule":false,"isPrelude":false,'
                '"imports":[{"module":"Mathlib","isMeta":false,"importAll":NaN}]}}]}'
            ),
            "unrecognized output",
        ),
        (_fake_header_command(_LEGACY_INIT_ONLY), "unrecognized output"),
        (_fake_header_command(_FAST_PARSER_ORDINARY), "unrecognized output"),
    ],
)
def test_header_check_fails_closed_when_the_parser_fails(command, message):
    with pytest.raises(RuntimeError, match=f"Lean header check failed: {message}"):
        _header_modules(command)


_NO_IMPORTS = {"imports": [], "isModule": False, "isPrelude": False}


# Each payload breaks one rule of parse_header.lean's schema.
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"imports": [{"errors": [], "result": _NO_IMPORTS}], "errors": []},
        {"imports": {}},
        {"imports": []},
        {"imports": [{"errors": [], "result": _NO_IMPORTS}] * 2},
        {"imports": [[]]},
        {"imports": [{"result": _NO_IMPORTS}]},
        {"imports": [{"errors": "bad header", "result": _NO_IMPORTS}]},
        {"imports": [{"errors": [""]}]},
        {"imports": [{"errors": [1]}]},
        {"imports": [{"errors": []}]},
        {"imports": [{"errors": [], "imports": []}]},
        {"imports": [{"errors": [], "result": _NO_IMPORTS, "imports": []}]},
        {"imports": [{"errors": [], "result": []}]},
        {"imports": [{"errors": [], "result": {"isModule": False, "isPrelude": False}}]},
        {"imports": [{"errors": [], "result": {**_NO_IMPORTS, "imports": {}}}]},
        {"imports": [{"errors": [], "result": {"imports": [], "isPrelude": False}}]},
        {"imports": [{"errors": [], "result": {**_NO_IMPORTS, "isModule": 0}}]},
        {"imports": [{"errors": [], "result": {"imports": [], "isModule": False}}]},
        {"imports": [{"errors": [], "result": {**_NO_IMPORTS, "isPrelude": None}}]},
        {"imports": [{"errors": [], "result": {**_NO_IMPORTS, "imports": ["Mathlib"]}}]},
        {"imports": [{"errors": [], "result": {**_NO_IMPORTS, "imports": [{"isMeta": False}]}}]},
        {"imports": [{"errors": [], "result": {**_NO_IMPORTS, "imports": [{"module": ""}]}}]},
        {"imports": [{"errors": [], "result": {**_NO_IMPORTS, "imports": [{"module": 1}]}}]},
        {"imports": [{"errors": [], "result": {**_NO_IMPORTS, "imports": [{"module": "Mathlib", "isMeta": 1}]}}]},
    ],
)
def test_header_decoder_fails_closed_on_unrecognized_output(payload):
    with pytest.raises(RuntimeError, match="Lean header check failed: unrecognized output"):
        repl_core._decode_header_analysis(json.dumps(payload).encode())


def test_header_decoder_fails_closed_on_invalid_utf8():
    with pytest.raises(RuntimeError, match="Lean header check failed: unrecognized output"):
        repl_core._decode_header_analysis(b"\xff")


@pytest.mark.parametrize("module", ["Mathlib/X", "Mathlib\\X", "Mathlib..X"])
def test_header_decoder_refuses_path_like_or_empty_module_components(module):
    with pytest.raises(ValueError, match="plain, nonempty module components"):
        repl_core._decode_header_analysis(_header_json(module).encode())


@pytest.mark.parametrize(
    ("header_output", "expected_error"),
    [
        (_header_json("Mathlib", "Unsafe.Mod"), "Disallowed imports: Unsafe"),
        (_header_json(errors=("bad header",)), "Rejected Lean header: bad header"),
    ],
)
def test_disposable_call_rejects_what_lean_reports_before_starting(
    monkeypatch, caplog, header_output, expected_error
):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            allowed_imports=frozenset({"Mathlib"}),
            warmup_imports=frozenset(),
            header_command=_fake_header_command(header_output),
        )
    )
    monkeypatch.setattr(
        repl,
        "start",
        lambda: pytest.fail("invalid input must not start Lean"),
    )

    with caplog.at_level(logging.WARNING, logger=repl_core.logger.name):
        response = repl.run_disposable("/- hidden -/ import Unsafe\n#check Nat")

    assert expected_error in response["repl_error"]
    assert repl.is_clean()
    # The caller's input is at fault, not the runtime, so nothing is logged.
    assert _repl_log(caplog) == []


_LEAN_TYPE_MISMATCH = (
    "parse_header.lean:14:47: error: Application type mismatch: The argument\n"
    "  input\n"
    "has type\n"
    "  String : Type\n"
    "but is expected to have type\n"
    "  Parser.InputContext : Type\n"
)


@pytest.mark.parametrize(
    ("stdout", "stderr", "detail"),
    [
        # The launcher outside lake env, where LEAN_SYSROOT is unset.
        (
            "",
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "KeyError: 'LEAN_SYSROOT'\n",
            "KeyError: 'LEAN_SYSROOT'",
        ),
        # Lean prints elaboration errors on stdout.
        (
            "parse_header.lean:14:3: error: unknown constant 'Lean.Parser.parseHeader'\n\n",
            "",
            "parse_header.lean:14:3: error: unknown constant 'Lean.Parser.parseHeader'",
        ),
        # Lean follows the error line with indented terms, and Lake warns on
        # stderr while the manifest is out of date.
        (
            _LEAN_TYPE_MISMATCH,
            "",
            "parse_header.lean:14:47: error: Application type mismatch: The argument",
        ),
        (
            _LEAN_TYPE_MISMATCH,
            "warning: manifest out of date: git revision of dependency 'repl' changed; "
            "use `lake update repl` to update it\n",
            "parse_header.lean:14:47: error: Application type mismatch: The argument",
        ),
        # Without an error line on stdout, stderr's last non-empty line wins...
        ("stdout first\nstdout last\n", "stderr first\nstderr last\n\n", "stderr last"),
        # ...and stdout's last non-empty line is used only when stderr has none.
        ("stdout first\nstdout last\n\n", "", "stdout last"),
        ("", "", "exit status 1"),
        ("", "x" * 1000, "x" * repl_core._HEADER_FAILURE_DETAIL_CHARS + "..."),
    ],
)
def test_failed_header_command_reports_its_error_line(monkeypatch, stdout, stderr, detail):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            header_command=_fake_header_command(stdout, returncode=1, stderr=stderr),
        )
    )
    monkeypatch.setattr(
        repl,
        "start",
        lambda: pytest.fail("a failed header check must not start Lean"),
    )

    assert repl.run_disposable("#check Nat") == {"repl_error": f"Lean header check failed: {detail}"}
    assert repl.is_clean()


# lake env exits without reading its input when the lakefile or toolchain is
# broken, so writing a snippet larger than any pipe buffer meets a closed pipe.
def test_header_command_that_exits_without_reading_reports_its_error_line(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            header_command=[
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('error: broken lakefile\\n'); sys.exit(1)",
            ],
        )
    )
    monkeypatch.setattr(
        repl,
        "start",
        lambda: pytest.fail("a failed header check must not start Lean"),
    )
    code = "-- " + "x" * (1 << 20) + "\n#check Nat"

    assert repl.run_disposable(code, timeout=30) == {
        "repl_error": "Lean header check failed: error: broken lakefile"
    }
    assert repl.is_clean()


@pytest.mark.parametrize(
    "code",
    [
        "/- note -/ import Unsafe\n#check Nat",
        "/- /---/ -/\nimport Unsafe\n-- -/\n#check Nat\n",
        "/- a --/\n/- x /--/ -/\nimport Unsafe\n-- -/\n#check Nat\n",
    ],
)
def test_disposable_call_checks_submitted_header_before_warmup_prefix(monkeypatch, code):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            allowed_imports=frozenset({"Mathlib"}),
            warmup_imports=frozenset({"Mathlib"}),
        )
    )
    checked = []

    def check_header(code, deadline):
        checked.append(code)
        raise ValueError("stop")

    monkeypatch.setattr(repl, "_check_header", check_header)

    repl.run_disposable(code)

    assert checked == [code]


def test_disposable_call_refuses_non_posix_before_spawning_header_parser(monkeypatch):
    repl = repl_core.LeanRepl(repl_core.LeanReplConfig())
    monkeypatch.setattr(repl_core.os, "name", "nt")
    monkeypatch.setattr(
        repl_core.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("non-POSIX call spawned a parser"),
    )

    response = repl.run_disposable("#check Nat")

    assert "requires a POSIX platform" in response["repl_error"]
    assert repl.is_clean()


def _failing_lake_exe(stderr: str) -> list[str]:
    # Reading the whole frame first makes the failure arrive after the frame is
    # sent, as it does with Lake, which takes longer to start than a short
    # frame takes to write.
    script = (
        "import sys\n"
        "while sys.stdin.readline() not in ('\\n', ''):\n"
        "    pass\n"
        f"sys.stderr.write({stderr!r})\n"
        "sys.exit(1)\n"
    )
    return [sys.executable, "-c", script]


@pytest.mark.parametrize(
    ("lake_error", "hinted"),
    [
        ("error: unknown package `repl`", True),
        ("error: unknown executable @repl/repl", False),
    ],
)
def test_failed_repl_start_names_the_remedies_only_for_an_unknown_package(lake_error, hinted):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            validate_imports=False,
            warmup_imports=frozenset(),
            repl_command=_failing_lake_exe(lake_error + "\n"),
        )
    )

    error = repl.run_disposable("#check Nat", timeout=30)["repl_error"]

    assert lake_error in error
    assert error.startswith("Lake could not find the REPL package: ") is hinted
    assert ("add `leanprover-community/repl` as a require named `repl`" in error) is hinted
    assert ("rename the require to `repl`" in error) is hinted
    assert ('restart the runtime with LEAN_REPL_CMD="lake exe @<name>/repl"' in error) is hinted
    assert ("applies to every project the runtime serves" in error) is hinted
    assert repl.is_clean()


# Lake exits without reading the frame when it cannot find the REPL package, so
# a frame larger than the pipe buffer is not fully sent when the child's pipes
# close, and either stream can be seen closing first.
@pytest.mark.parametrize("closed_stream", ["stdout", "stderr"])
def test_failed_repl_start_before_the_frame_is_sent_names_the_remedies(monkeypatch, closed_stream):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [])
        process._stderr_write.write(b"error: unknown package `repl`\n")
        if closed_stream == "stdout":
            process._stdout_write.close()
        else:
            process._stderr_write.close()
        repl = _disposable_repl_with_process(monkeypatch, process)

        response = repl.run_disposable("#check Nat", timeout=1)

    assert response["repl_error"].startswith("Lake could not find the REPL package: ")
    assert "outcome_unknown" not in response
    assert repl.is_clean()


# The header parser on Lean v4.27.0 reports one plain Init import both for an
# ordinary header and for ``prelude`` followed by ``import Init``.
_V4_27_INIT_ONLY = (
    '{"imports":[{"errors":[],"result":{"imports":[{"importAll":false,"isExported":true,'
    '"isMeta":false,"module":"Init"}],"isModule":false,"isPrelude":%s}}]}'
)


@pytest.mark.parametrize(
    ("header_output", "code", "frame"),
    [
        (_header_json(), "#check Nat", "import Mathlib\n#check Nat"),
        (
            _header_json("Mathlib.TraversalAnchor"),
            "import Mathlib.TraversalAnchor\n#check autoformWarmupMarker",
            "import Mathlib\nimport Mathlib.TraversalAnchor\n#check autoformWarmupMarker",
        ),
        (
            _header_json("REPL.Frontend", is_module=True),
            "module\npublic import REPL.Frontend\n",
            "module\npublic import REPL.Frontend\n",
        ),
        (
            _header_json("REPL.Frontend", prelude=True),
            "prelude\nimport REPL.Frontend\n#check Nat",
            "prelude\nimport REPL.Frontend\n#check Nat",
        ),
        (_V4_27_INIT_ONLY % "false", "#check Nat", "import Mathlib\n#check Nat"),
        (
            _V4_27_INIT_ONLY % "true",
            "prelude\nimport Init\n#check Nat",
            "prelude\nimport Init\n#check Nat",
        ),
    ],
)
def test_disposable_call_prefixes_warmup_only_onto_proven_ordinary_headers(monkeypatch, header_output, code, frame):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            allowed_imports=frozenset({"Mathlib", "REPL"}),
            warmup_imports=frozenset({"Mathlib"}),
            header_command=_fake_header_command(header_output),
        )
    )
    frames = []
    monkeypatch.setattr(repl, "start", lambda: None)

    def run_frame(code, timeout):
        frames.append(code)
        return {"env": 1, "messages": [], "sorries": []}

    monkeypatch.setattr(repl, "_run", run_frame)

    assert repl.run_disposable(code) == {"messages": [], "sorries": []}
    assert frames == [frame]


def test_disposable_call_rejects_disallowed_warmup_import(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            allowed_imports=frozenset({"Mathlib"}),
            warmup_imports=frozenset({"Unsafe.Prelude"}),
            header_command=_fake_header_command(_header_json()),
        )
    )
    monkeypatch.setattr(
        repl,
        "start",
        lambda: pytest.fail("disallowed warmup must not start Lean"),
    )

    response = repl.run_disposable("#check Nat")

    assert "Disallowed imports: Unsafe" in response["repl_error"]


def test_header_check_kills_a_command_that_outlives_the_deadline():
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        _header_modules([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)

    assert time.monotonic() - started < 5


def test_header_check_never_spawns_after_its_shared_deadline(monkeypatch):
    repl = repl_core.LeanRepl(repl_core.LeanReplConfig())
    monkeypatch.setattr(repl_core.time, "monotonic", lambda: 2.0)
    monkeypatch.setattr(
        repl_core.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("expired header check spawned Lean"),
    )

    with pytest.raises(TimeoutError, match="timed out checking the Lean header"):
        repl._check_header("#check Nat", deadline=1.0)


def test_header_check_rejects_output_over_the_combined_limit():
    with pytest.raises(RuntimeError, match="Lean header check failed: header command output exceeded 1024 bytes"):
        _header_modules(_fake_header_command("x" * 600, stderr="y" * 600), max_buffer_bytes=1024)


def test_header_check_reaps_descendants_after_success(tmp_path):
    process_record = tmp_path / "processes.json"
    script = (
        "import json, os, subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'], stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL); "
        f"open({str(process_record)!r}, 'w').write(json.dumps("
        "{'group': os.getpgrp()})); "
        "sys.stdin.read(); "
        f"sys.stdout.write({_header_json('Mathlib')!r})"
    )

    assert _header_modules([sys.executable, "-c", script]) == ["Mathlib"]
    process_ids = json.loads(process_record.read_text())
    assert not process_group._process_group_has_live_members(process_ids["group"])


# Answers one frame, then waits for the next as the REPL does.
_ONE_FRAME_REPL = (
    "import sys\n"
    "while sys.stdin.readline() not in ('\\n', ''):\n"
    "    pass\n"
    "sys.stdout.write('{\"env\": 0}\\n\\n')\n"
    "sys.stdout.flush()\n"
    "sys.stdin.read()\n"
)


def test_disposable_call_closes_the_pipes_of_both_process_generations(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            header_command=_fake_header_command(_header_json()),
            repl_command=[sys.executable, "-c", _ONE_FRAME_REPL],
        )
    )
    processes = []
    real_spawn = repl._spawn

    def spawn(command):
        processes.append(real_spawn(command))
        return processes[-1]

    monkeypatch.setattr(repl, "_spawn", spawn)

    assert repl.run_disposable("#check Nat", timeout=30) == {}

    # The header parser's pipes, then the REPL child's.
    assert [
        [stream.closed for stream in (process.stdin, process.stdout, process.stderr)]
        for process in processes
    ] == [[True, True, True], [True, True, True]]
    assert repl.is_clean()


def _repl_failing_first_header_cleanup(monkeypatch, error: BaseException):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(), header_command=_fake_header_command(_header_json("Mathlib"))
        )
    )
    real_kill = repl_core._kill_subprocesses
    cleanup_calls = []

    def fail_once(process, process_group_id, deadline=None):
        cleanup_calls.append(process_group_id)
        if len(cleanup_calls) == 1:
            raise error
        return real_kill(process, process_group_id, deadline)

    monkeypatch.setattr(repl_core, "_kill_subprocesses", fail_once)
    return repl, cleanup_calls


def test_disposable_call_retries_header_cleanup_before_reuse(monkeypatch):
    repl, cleanup_calls = _repl_failing_first_header_cleanup(monkeypatch, RuntimeError("injected cleanup failure"))
    monkeypatch.setattr(repl, "start", lambda: pytest.fail("unclean header must not start Lean"))

    response = repl.run_disposable("import Mathlib\n#check Nat")

    assert "injected cleanup failure" in response["repl_error"]
    assert len(cleanup_calls) == 2
    assert repl.is_clean()


def test_disposable_call_preserves_header_cleanup_cancellation(monkeypatch):
    repl, cleanup_calls = _repl_failing_first_header_cleanup(
        monkeypatch, asyncio.CancelledError("cancel header cleanup")
    )

    with pytest.raises(asyncio.CancelledError, match="cancel header cleanup"):
        repl.run_disposable("import Mathlib\n#check Nat")

    assert len(cleanup_calls) == 2
    assert repl.is_clean()


def test_disposable_call_preserves_header_request_cancellation(monkeypatch):
    repl, cleanup_calls = _repl_failing_first_header_cleanup(monkeypatch, RuntimeError("header cleanup failed"))

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt("cancel header request")

    monkeypatch.setattr(repl_core, "_communicate_bounded", cancel)

    with pytest.raises(KeyboardInterrupt, match="cancel header request") as raised:
        repl.run_disposable("import Mathlib\n#check Nat")

    if hasattr(raised.value, "add_note"):
        assert raised.value.__notes__ == ["Lean REPL process cleanup also failed: header cleanup failed"]
    # The slot keeps the parser until a later close() verifies that it exited.
    assert not repl.is_clean()
    repl.close()
    assert len(cleanup_calls) == 2
    assert repl.is_clean()


def test_disposable_timeout_before_the_frame_is_sent_states_the_budget(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [])
        repl = _disposable_repl_with_process(monkeypatch, process)
        # Nothing becomes ready, so the write times out before the frame is sent.
        monkeypatch.setattr(repl_core.select, "select", lambda r, w, x, timeout=None: ([], [], []))

        response = repl.run_disposable("#check Nat", timeout=1, budget=3)

    assert response == {"repl_error": "Lean REPL combined command execution timed out after 3 seconds"}
    assert repl.is_clean()


def test_disposable_timeout_after_full_write_is_not_retried(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(warmup_imports=frozenset(), validate_imports=False)
    )
    calls = []
    monkeypatch.setattr(repl, "start", lambda: setattr(repl, "process", object()))

    def fail_after_send(code, timeout, mark_sent):
        calls.append(code)
        mark_sent()
        raise TimeoutError("response timed out")

    monkeypatch.setattr(repl, "_run_io", fail_after_send)
    response = repl.run_disposable("#eval 1", timeout=1)

    assert response == {
        "repl_error": "Lean REPL combined command execution timed out after 1 seconds",
        "outcome_unknown": True,
    }
    assert calls == ["#eval 1"]
    assert repl.process is None


def test_disposable_cleanup_failure_after_full_write_keeps_the_unknown_outcome(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(warmup_imports=frozenset(), validate_imports=False)
    )
    close_calls = 0

    def close(**kwargs):
        nonlocal close_calls
        close_calls += 1
        # The slot is clean when the call begins; every later cleanup fails.
        if close_calls > 1:
            raise RuntimeError("cleanup failed")

    def fail_after_send(code, timeout, mark_sent):
        mark_sent()
        raise TimeoutError("response timed out")

    monkeypatch.setattr(repl, "close", close)
    monkeypatch.setattr(repl, "start", lambda: setattr(repl, "process", object()))
    monkeypatch.setattr(repl, "_run_io", fail_after_send)

    with pytest.raises(repl_core.ReplCleanupError, match="retirement is required: cleanup failed") as error:
        repl.run_disposable("#eval 1", timeout=1)

    assert error.value.result == {
        "repl_error": (
            "Lean REPL combined command execution timed out after 1 seconds; "
            "process cleanup also failed: cleanup failed"
        ),
        "outcome_unknown": True,
    }
    assert close_calls == 3
    assert repl.process is not None


def test_disposable_timeout_after_full_write_keeps_and_logs_a_cleanup_failure(monkeypatch, caplog):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(warmup_imports=frozenset(), validate_imports=False)
    )
    real_close = repl.close
    close_calls = 0

    def close(**kwargs):
        nonlocal close_calls
        close_calls += 1
        # Only the cleanup right after the timeout fails; the slot's own
        # cleanup then retires the process.
        if close_calls == 2:
            raise RuntimeError("cleanup failed")
        real_close(**kwargs)

    def fail_after_send(code, timeout, mark_sent):
        mark_sent()
        raise TimeoutError("response timed out")

    monkeypatch.setattr(repl, "close", close)
    monkeypatch.setattr(repl, "start", lambda: setattr(repl, "process", object()))
    monkeypatch.setattr(repl, "_run_io", fail_after_send)

    with caplog.at_level(logging.WARNING, logger=repl_core.logger.name):
        response = repl.run_disposable("#eval 1", timeout=1)

    message = (
        "Lean REPL combined command execution timed out after 1 seconds; "
        "process cleanup also failed: cleanup failed"
    )
    assert response == {"repl_error": message, "outcome_unknown": True}
    assert _repl_log(caplog) == [(logging.WARNING, f"Lean REPL call in . failed: {message}")]
    assert close_calls == 3
    assert repl.is_clean()


@pytest.mark.parametrize("request_sent", [False, True])
def test_run_closes_and_reraises_cancellation(monkeypatch, request_sent):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(validate_imports=False, warmup_imports=frozenset())
    )
    repl.process = object()
    retired = []

    def cancel(code, timeout, mark_sent):
        if request_sent:
            mark_sent()
        raise asyncio.CancelledError

    monkeypatch.setattr(repl, "_run_io", cancel)
    monkeypatch.setattr(repl, "close", lambda **kwargs: retired.append(True))

    with pytest.raises(asyncio.CancelledError):
        repl._run("#check Nat", timeout=1)

    assert retired == [True]


def test_run_preserves_cancellation_when_cleanup_fails(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(validate_imports=False, warmup_imports=frozenset())
    )
    repl.process = object()

    monkeypatch.setattr(
        repl,
        "_run_io",
        lambda *args, **kwargs: (_ for _ in ()).throw(asyncio.CancelledError()),
    )
    monkeypatch.setattr(
        repl,
        "close",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        repl._run("#check Nat", timeout=1)

    if hasattr(raised.value, "add_note"):
        assert raised.value.__notes__ == [
            "Lean REPL process cleanup also failed: cleanup failed"
        ]


def test_wire_protocol_accepts_response_split_across_reads(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages":', b" []}\n", b"\n"])
        repl = _repl_with_process(process, chunk_size=8)
        _patch_pipe_reads(monkeypatch, process)

        assert repl._run("#check Nat", timeout=1) == {"messages": []}

        request = process._stdin_read.read(4096)
        assert json.loads(request.decode().strip()) == {"cmd": "#check Nat"}


def test_wire_protocol_rechecks_deadline_before_dispatch_delimiter(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [])
        repl = _repl_with_process(process)
        now = 0.0
        write_waits = 0
        sent = []

        def fake_select(readable, writable, exceptional, timeout=None):
            nonlocal now, write_waits
            if writable:
                write_waits += 1
                if write_waits == 2:
                    now = 2.0
                return [], writable, []
            return [], [], []

        monkeypatch.setattr(repl_core.select, "select", fake_select)
        monkeypatch.setattr(repl_core.time, "monotonic", lambda: now)

        with pytest.raises(TimeoutError, match="while writing"):
            repl._run_io(
                "#check Nat",
                timeout=1,
                mark_sent=lambda: sent.append(True),
            )

        request = os.read(process._stdin_read.fileno(), 4096)
        assert request.endswith(b"\n") and not request.endswith(b"\n\n")
        assert sent == []


def test_final_delimiter_write_failure_has_unknown_outcome(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [])
        repl = _repl_with_process(process)
        real_write = repl_core.os.write
        retired = []

        def fail_final_delimiter(fd: int, data) -> int:
            if fd == process.stdin.fileno() and bytes(data) == b"\n":
                raise OSError("ambiguous delimiter write")
            return real_write(fd, data)

        monkeypatch.setattr(repl_core.os, "write", fail_final_delimiter)
        monkeypatch.setattr(repl_core.select, "select", lambda r, w, x, timeout=None: ([], w, []))
        monkeypatch.setattr(repl, "close", lambda **kwargs: retired.append(True))

        with pytest.raises(repl_core.ReplOutcomeUnknown, match="fully sent"):
            repl._run("#check Nat", timeout=1)

        request = os.read(process._stdin_read.fileno(), 4096)
        assert request.endswith(b"\n") and not request.endswith(b"\n\n")
        assert retired == [True]


def test_wire_protocol_retires_process_on_unsolicited_second_frame(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"env":1}\n\n{"env":2}\n\n'])
        repl = _repl_with_process(process)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplOutcomeUnknown, match="unsolicited bytes"):
            repl._run("#check Nat", timeout=1)

        assert repl.process is None


def test_wire_protocol_exchanges_one_frame_over_real_pipes():
    with ExitStack() as stack:
        process = _PipeProcess(stack, [])
        repl = _repl_with_process(process)
        request = bytearray()

        def serve_request() -> None:
            while b"\n\n" not in request:
                request.extend(os.read(process._stdin_read.fileno(), 4096))
            os.write(process._stdout_write.fileno(), b'{"env":1}\n\n')

        worker = threading.Thread(target=serve_request, daemon=True)
        worker.start()
        assert repl._run("#check Nat", timeout=10) == {"env": 1}
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert bytes(request) == b'{"cmd": "#check Nat"}\n\n'


def test_wire_protocol_rechecks_stdout_before_dispatching_a_complete_request_body(
    monkeypatch,
):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [])
        repl = _repl_with_process(process)
        real_write = os.write
        first_write = True

        def write_body_then_emit_stdout(fd: int, data: bytes) -> int:
            nonlocal first_write
            if fd != process.stdin.fileno() or not first_write:
                return real_write(fd, data)
            first_write = False
            written = real_write(fd, data)
            real_write(process._stdout_write.fileno(), b'{"env":999}\n\n')
            return written

        monkeypatch.setattr(repl_core.os, "write", write_body_then_emit_stdout)

        with pytest.raises(repl_core.ReplProcessExited, match="unsolicited stdout"):
            repl._run("#check Nat", timeout=1)

        os.set_blocking(process._stdin_read.fileno(), False)
        written = os.read(process._stdin_read.fileno(), 4096)
        assert written.endswith(b"\n") and not written.endswith(b"\n\n")
        assert repl.process is None


def test_wire_protocol_preserves_utf8_split_across_reads(monkeypatch):
    response = json.dumps({"messages": [{"data": "Nat → Nat"}]}, ensure_ascii=False).encode()
    arrow = "→".encode()
    split = response.index(arrow) + 1
    with ExitStack() as stack:
        process = _PipeProcess(
            stack,
            [response[:split], response[split:] + b"\n\n"],
        )
        repl = _repl_with_process(process, chunk_size=7)
        _patch_pipe_reads(monkeypatch, process)

        result = repl._run("#check Nat", timeout=1)

    assert result["messages"][0]["data"] == "Nat → Nat"


def test_wire_protocol_reports_a_bounded_stderr_tail_on_premature_eof(monkeypatch):
    stderr = b"discarded-prefix" + (b"x" * 5000) + b"lean crashed"
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b""], stderr=stderr)
        repl = _repl_with_process(process, max_buffer_bytes=len(stderr))
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplOutcomeUnknown) as error:
            repl._run("#check Nat", timeout=1)

    assert str(error.value).endswith(stderr[-repl_core._STDERR_TAIL_BYTES :].decode())
    assert "discarded-prefix" not in str(error.value)


def test_wire_protocol_services_stdout_while_stderr_remains_readable(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"x")
        repl = _repl_with_process(process, chunk_size=8)
        _patch_pipe_reads(monkeypatch, process)

        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            if fd == process.stderr.fileno():
                return b"x" * size
            return real_read(fd, size)

        monkeypatch.setattr(repl_core.os, "read", fake_read)

        # Endlessly readable stderr never starves stdout, so the response is
        # captured. stderr cannot be drained to a boundary though, so the
        # process is retired and the response travels on the backlog error.
        with pytest.raises(repl_core.ReplStderrBacklog) as error:
            repl._run("#check Nat", timeout=1)

    assert error.value.response == {"messages": []}


def test_wire_protocol_times_out_while_stderr_remains_readable(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [], stderr=b"x")
        repl = _repl_with_process(process)
        _patch_pipe_reads(monkeypatch, process)

        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            if fd == process.stderr.fileno():
                return b"x"
            return real_read(fd, size)

        now = 0.0

        def fake_monotonic() -> float:
            nonlocal now
            now += 0.25
            return now

        monkeypatch.setattr(repl_core.os, "read", fake_read)
        monkeypatch.setattr(repl_core.time, "monotonic", fake_monotonic)

        with pytest.raises(repl_core.ReplOutcomeUnknown, match="timed out"):
            repl._run("#check Nat", timeout=1)


def test_wire_protocol_retires_a_process_generation_that_exceeds_its_stderr_quota(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b"{}\n\n"], stderr=b"0123456789")
        repl = _repl_with_process(process, chunk_size=10, max_buffer_bytes=8)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplStderrBacklog, match="process-generation stderr") as error:
            repl._run("#check Nat", timeout=1)

    assert error.value.response == {}
    assert repl.process is None
    # A bounded tail survives even though the process-wide quota was exceeded.
    assert "Tail: " in str(error.value)
    assert "23456789" in str(error.value)


def test_disposable_stderr_overflow_without_a_response_is_not_retried(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [], stderr=b"x" * 32)
        repl = _disposable_repl_with_process(monkeypatch, process, max_buffer_bytes=16)
        _patch_pipe_reads(monkeypatch, process)

        response = repl.run_disposable("#check Nat", timeout=1)

        assert "execution outcome is unknown and was not retried" in response["repl_error"]
        assert response["outcome_unknown"] is True
        assert repl.process is None


def test_wire_protocol_drains_stderr_while_waiting_to_write(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b"{}\n\n"], stderr=b"x" * 8)
        repl = _repl_with_process(process, chunk_size=4, max_buffer_bytes=16)
        _patch_pipe_reads(monkeypatch, process)
        normal_select = repl_core.select.select

        def block_stdin_until_stderr_is_drained(
            readable,
            writable,
            exceptional,
            timeout=None,
        ):
            if writable and process.stderr_bytes:
                assert process.stderr.fileno() in readable
                return [process.stderr.fileno()], [], []
            return normal_select(readable, writable, exceptional, timeout)

        monkeypatch.setattr(
            repl_core.select,
            "select",
            block_stdin_until_stderr_is_drained,
        )

        assert repl._run("#check Nat", timeout=1) == {}
        assert process.stderr_bytes == b""
        assert repl._stderr_bytes == 8


def test_wire_protocol_retires_a_process_with_closed_stderr_before_writing():
    with ExitStack() as stack:
        process = _PipeProcess(stack, [])
        process._stderr_write.close()
        repl = _repl_with_process(process)

        with pytest.raises(repl_core.ReplProcessExited, match="before the request frame"):
            repl._run("#check Nat", timeout=1)

        assert repl.process is None


def test_stdout_eof_before_the_frame_is_sent_reports_the_stderr_tail():
    # A child that exits before reading the frame, as Lake does when it cannot
    # find the REPL package, says why only on stderr.
    with ExitStack() as stack:
        process = _PipeProcess(stack, [])
        process._stderr_write.write(b"error: unknown package `repl`\n")
        process._stdout_write.close()
        repl = _repl_with_process(process)

        with pytest.raises(repl_core.ReplProcessExited, match="closed stdout before") as error:
            repl._run("#check Nat", timeout=1)

        assert repl.process is None
    assert "error: unknown package `repl`" in str(error.value)


def test_stdout_eof_keeps_only_a_fixed_stderr_tail(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(
            stack,
            [b""],
            stderr=b"a" * 100 + b"b" * repl_core._STDERR_TAIL_BYTES,
        )
        repl = _repl_with_process(
            process,
            chunk_size=512,
            max_buffer_bytes=1024,
        )
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplProcessExited) as error:
            repl._run_io("#check Nat", timeout=1, mark_sent=lambda: None)

        assert len(repl._stderr_tail) == repl_core._STDERR_TAIL_BYTES
        assert bytes(repl._stderr_tail) == b"b" * repl_core._STDERR_TAIL_BYTES
        assert "a" * 100 not in str(error.value)


def test_stdout_eof_takes_one_final_stderr_read_after_deadline(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b""])
        repl = _repl_with_process(process, chunk_size=8)
        now = 0.0
        stderr_reads = 0
        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            nonlocal now, stderr_reads
            if fd == process.stdout.fileno():
                now = 2.0
                return b""
            if fd == process.stderr.fileno():
                stderr_reads += 1
                return b"diagnost"[:size]
            return real_read(fd, size)

        def fake_select(readable, writable, exceptional, timeout=None):
            if writable:
                return [], writable, []
            return [process.stderr.fileno(), process.stdout.fileno()], [], []

        monkeypatch.setattr(repl_core.os, "read", fake_read)
        monkeypatch.setattr(repl_core.time, "monotonic", lambda: now)
        monkeypatch.setattr(repl_core.select, "select", fake_select)

        with pytest.raises(repl_core.ReplProcessExited, match="diagnostdiagnost"):
            repl._run_io("#check Nat", timeout=1, mark_sent=lambda: None)

        assert stderr_reads == 2


def test_wire_protocol_drains_queued_stderr_after_the_response_frame_completes(monkeypatch):
    # One stdout read completes the frame while stderr still holds several chunks.
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 40)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=1024)
        _patch_pipe_reads(monkeypatch, process)

        assert repl._run("#check Nat", timeout=5) == {"messages": []}

        # Unlike a fairness cycle, the post-response drain reads until stderr is empty.
        assert process.stderr_bytes == b""


def test_wire_protocol_reports_a_backlog_when_the_cap_ends_the_post_response_drain(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 40)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplStderrBacklog) as error:
            repl._run("#check Nat", timeout=5)

    # The response survives as the request's answer...
    assert error.value.response == {"messages": []}
    # ...and the drain stops once the generation exceeds its stderr cap.
    assert process.stderr_bytes == b"e" * 4


def test_wire_protocol_accepts_stderr_that_ends_exactly_at_the_cap(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 20)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)

        assert repl._run("#check Nat", timeout=5) == {"messages": []}

        assert repl.process is process
        assert process.stderr_bytes == b""


def test_wire_protocol_keeps_the_response_when_the_deadline_ends_the_stderr_drain(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"x")
        repl = _repl_with_process(process, max_buffer_bytes=1_000_000)
        _patch_pipe_reads(monkeypatch, process)

        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            if fd == process.stderr.fileno():
                return b"x" * size
            return real_read(fd, size)

        now = 0.0

        def fake_monotonic() -> float:
            nonlocal now
            now += 0.125
            return now

        monkeypatch.setattr(repl_core.os, "read", fake_read)
        monkeypatch.setattr(repl_core.time, "monotonic", fake_monotonic)

        # Endlessly readable stderr must not hang the post-response drain, and the
        # deadline must not discard a response that was already captured.
        with pytest.raises(repl_core.ReplStderrBacklog) as error:
            repl._run("#check Nat", timeout=1)

    assert error.value.response == {"messages": []}


def test_disposable_backlog_drops_the_environment_and_warns_that_stderr_became_unsafe(monkeypatch, caplog):
    with ExitStack() as stack:
        process = _PipeProcess(
            stack,
            [b'{"env":9,"messages":[]}\n\n'],
            stderr=b"e" * 64,
        )
        repl = _disposable_repl_with_process(monkeypatch, process, chunk_size=30, max_buffer_bytes=32)
        _patch_pipe_reads(monkeypatch, process)

        with caplog.at_level(logging.WARNING, logger=repl_core.logger.name):
            response = repl.run_disposable("#check Nat", timeout=5)

        assert response == {"messages": []}
        assert repl.process is None
    [(level, message)] = _repl_log(caplog)
    assert level == logging.WARNING
    assert message.startswith("Lean REPL call in .: ")
    assert "stderr became unsafe" in message


def test_run_never_leaves_a_reusable_process_when_a_backlog_stops_the_drain(monkeypatch):
    # The invariant holds at the source, so no caller of _run() can skip it.
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 40)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplStderrBacklog):
            repl._run("#check Nat", timeout=5)

        assert repl.process is None
        assert repl.is_clean()


def test_disposable_invalid_json_with_a_stderr_backlog_is_not_retried(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b"not-json\n\n"], stderr=b"e" * 40)
        repl = _disposable_repl_with_process(monkeypatch, process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)

        response = repl.run_disposable("#check Nat", timeout=5)

        assert response["outcome_unknown"] is True
        assert "response frame was malformed" in response["repl_error"]
        assert repl.process is None
        assert process.stderr_bytes == b"e" * 4


def test_wire_protocol_rejects_invalid_json(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b"not-json\n\n"])
        repl = _repl_with_process(process)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplOutcomeUnknown, match="fully sent"):
            repl._run("#check Nat", timeout=1)


@pytest.mark.parametrize(
    "response",
    [
        b'{"env":1,"env":2}\n\n',
        b'{"env":NaN}\n\n',
    ],
)
def test_disposable_call_rejects_noncanonical_response_json(monkeypatch, response):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [response])
        repl = _disposable_repl_with_process(monkeypatch, process)
        _patch_pipe_reads(monkeypatch, process)

        result = repl.run_disposable("#check Nat", timeout=1)

        assert result["outcome_unknown"] is True
        assert repl.process is None


def test_wire_protocol_rejects_oversized_response(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"data":"0123456789"}\n\n'])
        repl = _repl_with_process(process, max_buffer_bytes=8)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplOutcomeUnknown, match="response exceeded 8 bytes"):
            repl._run("#check Nat", timeout=1)


def test_close_after_an_expired_deadline_still_reserves_cleanup_time(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(warmup_imports=frozenset(), validate_imports=False)
    )
    repl.process = object()
    repl._process_group_id = 1234
    deadlines = []
    monkeypatch.setattr(
        repl_core,
        "_kill_subprocesses",
        lambda process, process_group_id, deadline=None: deadlines.append(deadline),
    )

    before = time.monotonic()
    repl.close(deadline=before - 5)

    assert deadlines[0] >= before + repl_core.DEFAULT_REPL_CLEANUP_SECONDS


def test_disposable_timeout_reaps_a_slow_to_exit_process_without_cleanup_failure(
    monkeypatch,
):
    real_kill = repl_core._kill_subprocesses
    refused = []

    def slow_reap(process, process_group_id, deadline=None):
        # A large Lean process needs real time to exit after SIGKILL.
        if deadline is not None and deadline - time.monotonic() < 0.5:
            refused.append(deadline)
            raise RuntimeError("timed out reaping the Lean REPL process")
        real_kill(process, process_group_id, deadline)

    monkeypatch.setattr(repl_core, "_kill_subprocesses", slow_reap)
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            # Never answers. The frame still fits in the pipe buffer, so the
            # call times out after it is fully sent.
            repl_command=[sys.executable, "-c", "import time; time.sleep(3600)"],
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )
    try:
        response = repl.run_disposable("#check Nat", timeout=0.5)

        assert response == {
            "repl_error": "Lean REPL combined command execution timed out after 0.5 seconds",
            "outcome_unknown": True,
        }
        assert refused == []
        assert repl.is_clean()
    finally:
        repl.close()
