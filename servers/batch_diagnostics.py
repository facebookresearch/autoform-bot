"""Lean diagnostics from one batch elaboration of a file with ``lake lean``.

Each call runs ``lake --no-build lean <file> -- --json -DmaxErrors=0`` in its
own process group and reads Lean's messages from stdout, one JSON object per
line; Lean's exit marks completion. The group is stopped and verified gone
before the call returns, or handed back still owned if bounded cleanup fails.
``--no-build`` keeps builds out of the call: stale imports are reported, not
rebuilt.
"""

from __future__ import annotations

import json
import logging
import os
import selectors
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from servers._process import (
    _inherit_clean_env,
    _kill_subprocesses,
    _wait_for_live_process_group_exit,
)

logger = logging.getLogger(__name__)

MAX_STDOUT_BYTES = 16 * 1024 * 1024
# How long Lake may take to exit after closing its output, before the group is
# stopped. Both pipes close only when every process holding them has exited.
EXIT_GRACE_SECONDS = 2.0
# Lake replays stored build logs on stderr, megabytes for some projects; keep
# only the end, where a build failure is reported.
STDERR_TAIL_BYTES = 2000
# Lean's status after reporting errors, and Lake's when --no-build finds a
# target that would need building. Any other nonzero status is a failure.
LEAN_ERROR_EXIT_CODE = 1
NO_BUILD_EXIT_CODE = 3
CLEANUP_ATTEMPTS = 3
# Command-line Lean stops after 100 errors by default; the server does not.
LEAN_ARGS = ("--json", "-DmaxErrors=0")
_SEVERITIES = {"error": 1, "warning": 2, "information": 3}


class BatchDiagnosticsError(RuntimeError):
    """Lean's output could not be trusted as a complete diagnostic set."""


class BatchCleanupError(BatchDiagnosticsError):
    """The run's process group outlived bounded cleanup; ``process`` is still owned."""

    def __init__(self, process: subprocess.Popen) -> None:
        super().__init__(f"Lean's process group did not stop after {CLEANUP_ATTEMPTS} attempts")
        self.process = process


def batch_diagnostics(
    root: Path,
    path: Path,
    *,
    command: tuple[str, ...],
    timeout: float,
    budget: float | None = None,
) -> list[dict[str, Any]]:
    """Return diagnostics for ``path`` from one ``lake lean`` run.

    Each has the ``severity``, ``range.start`` and ``message`` that
    ``format_lsp_diagnostics`` reads. The result is clean only when Lean exits 0
    with no messages, every stdout line parses, and the file is unchanged.
    Anything else either returns Lean's messages or raises, so a failed run is
    never reported as a clean file. A timeout message states ``budget``, which
    defaults to ``timeout``.
    """
    budget = timeout if budget is None else budget
    source = path.read_bytes()
    returncode, stdout, stderr_tail = _run([*command, str(path), "--", *LEAN_ARGS], root, timeout)
    shown = f"`{' '.join(command)}`"
    tail = stderr_tail.decode("utf-8", errors="replace").strip()
    ending = f"\nLake's output ended with:\n{tail}" if tail else ""
    if stdout is None:
        raise TimeoutError(
            f"Lean did not finish checking {path.name} within {budget:g}s "
            f"({shown}; LEAN_LSP_TIMEOUT).{ending}"
        )
    if path.read_bytes() != source:
        raise BatchDiagnosticsError(f"{path.name} changed while Lean checked it; run again")

    lines = source.decode("utf-8", errors="replace").split("\n")
    diagnostics = [_diagnostic(message, lines) for message in _messages(stdout)]
    if returncode == 0:
        return diagnostics
    if returncode == LEAN_ERROR_EXIT_CODE and any(item["severity"] == 1 for item in diagnostics):
        return diagnostics
    if returncode == NO_BUILD_EXIT_CODE:
        raise BatchDiagnosticsError(
            f"Lean did not check {path.name}: some of its imports are not built or are out "
            f"of date. Build the modules that {path.name} imports, for example with `lake build "
            "<module>`, and run again: Lake lists only the first modules to build, not the ones "
            f"that import them.{ending}"
        )
    raise BatchDiagnosticsError(
        f"{shown} stopped with status {returncode} before Lean finished checking "
        f"{path.name}, so there are no complete diagnostics.{ending}"
    )


def _run(argv: list[str], root: Path, timeout: float) -> tuple[int, bytes | None, bytes]:
    """Run one command in a fresh process group and verify the group is gone.

    Returns ``None`` for stdout when the deadline passed first.
    """
    deadline = time.monotonic() + timeout
    # Lake leaves a temporary setup file behind on every run; keep it per call.
    with tempfile.TemporaryDirectory(prefix="autoform-lake-lean-") as scratch:
        process = subprocess.Popen(
            argv,
            cwd=root,
            env={**_inherit_clean_env(), "TMPDIR": scratch},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr_tail = _drain(process, deadline)
            if stdout is not None:
                _wait_for_live_process_group_exit(
                    process.pid, time.monotonic() + EXIT_GRACE_SECONDS
                )
        finally:
            try:
                _retire(process)
            finally:
                for stream in (process.stdout, process.stderr):
                    assert stream is not None
                    stream.close()
    return process.returncode, stdout, stderr_tail


def _retire(process: subprocess.Popen) -> None:
    """Stop the run's process group, then reap Lake, retrying a failed attempt.

    Lake leads the group and stays unreaped until the group is gone, so its
    group id cannot be reused while stragglers are being stopped. When every
    attempt fails, the error carries the process to whoever retires it next.
    """
    for attempt in range(CLEANUP_ATTEMPTS):
        try:
            _kill_subprocesses(process, process.pid)
            return
        except Exception as error:
            if attempt + 1 == CLEANUP_ATTEMPTS:
                raise BatchCleanupError(process) from error
            time.sleep(0.1 * 2**attempt)


def retire_until_gone(process: subprocess.Popen) -> None:
    """Retire a group that outlived bounded cleanup, retrying until it is gone."""
    while True:
        try:
            _retire(process)
            return
        except BatchCleanupError:
            logger.exception("a lake lean process group survived cleanup; retrying")
            time.sleep(1.0)


def _drain(process: subprocess.Popen, deadline: float) -> tuple[bytes | None, bytes]:
    """Read stdout and the end of stderr until both close or time runs out.

    Returns ``None`` for stdout when the deadline passes first.
    """
    assert process.stdout is not None and process.stderr is not None
    stdout = bytearray()
    stderr_tail = bytearray()
    with selectors.DefaultSelector() as selector:
        for stream, sink in ((process.stdout, stdout), (process.stderr, stderr_tail)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, sink)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, bytes(stderr_tail)
            for key, _ in selector.select(remaining):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                sink = key.data
                sink.extend(chunk)
                if sink is stderr_tail:
                    del sink[:-STDERR_TAIL_BYTES]
                elif len(sink) > MAX_STDOUT_BYTES:
                    raise BatchDiagnosticsError(
                        f"Lean wrote more than {MAX_STDOUT_BYTES} bytes of messages"
                    )
    return bytes(stdout), bytes(stderr_tail)


def _messages(stdout: bytes) -> list[dict[str, Any]]:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BatchDiagnosticsError("Lean wrote invalid UTF-8 messages") from error
    messages = []
    # Lean escapes newlines inside a message, but not U+2028 and its kin, so
    # split on newlines only.
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError as error:
            raise BatchDiagnosticsError(
                f"Lean wrote a line that is not a JSON message: {line[:200]!r}"
            ) from error
        if not isinstance(message, dict):
            raise BatchDiagnosticsError(f"Lean wrote a JSON value that is not a message: {line[:200]!r}")
        messages.append(message)
    return messages


def _diagnostic(message: dict[str, Any], lines: list[str]) -> dict[str, Any]:
    """Shape one ``lean --json`` message as ``format_lsp_diagnostics`` reads it."""
    severity = _SEVERITIES.get(message.get("severity"))
    if severity is None:
        raise BatchDiagnosticsError(f"Lean message has an unknown severity: {message!r:.200}")
    pos = message.get("pos")
    line, column = (pos.get("line"), pos.get("column")) if isinstance(pos, dict) else (None, None)
    if not isinstance(line, int) or not isinstance(column, int) or line < 1 or column < 0:
        raise BatchDiagnosticsError(f"Lean message has no position: {message!r:.200}")
    text = message.get("data")
    if not isinstance(text, str):
        raise BatchDiagnosticsError(f"Lean message has no text: {message!r:.200}")
    # Lean's 1-based line and code-point column become LSP's 0-based line and
    # UTF-16 column.
    source = lines[line - 1] if line <= len(lines) else ""
    character = len(source[:column].encode("utf-16-le")) // 2 + max(0, column - len(source))
    return {"severity": severity, "range": {"start": {"line": line - 1, "character": character}}, "message": text}
