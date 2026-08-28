"""Focused contracts salvaged from the retired standalone REPL implementation."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack

import pytest

from servers.repl import core as repl_core


def test_split_imports_preserves_body_offset_after_comments_and_blank_lines():
    code = """-- preface
import Mathlib.Data.Nat.Basic

-- body comment
#check Nat
"""

    imports, body, offset = repl_core._split_imports_and_body(code)

    assert imports == ["Mathlib.Data.Nat.Basic"]
    assert body == "#check Nat\n"
    assert offset == 4


def test_split_imports_stops_at_the_first_body_statement():
    imports, body, offset = repl_core._split_imports_and_body(
        "import Mathlib\n#check Nat\nimport Aesop\n"
    )

    assert imports == ["Mathlib"]
    assert body == "#check Nat\nimport Aesop\n"
    assert offset == 1


def test_run_rejects_disallowed_import_roots_before_touching_the_process():
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            allowed_imports=frozenset({"Mathlib"}),
            warmup_imports=frozenset(),
        )
    )

    response = repl.run("import Unsafe.Module\n#check Nat")

    assert response == {
        "repl_error": "Disallowed imports: Unsafe. Allowed roots: Mathlib."
    }
    assert repl.process is None


def test_run_offsets_diagnostics_after_stripping_import_header(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    monkeypatch.setattr(
        repl,
        "_run",
        lambda code, env_id, timeout: {
            "messages": [
                {
                    "severity": "error",
                    "data": "boom",
                    "pos": {"line": 1, "column": 2},
                    "endPos": {"line": 1, "column": 3},
                }
            ],
            "sorries": [
                {
                    "goal": "False",
                    "pos": {"line": 2, "column": 1},
                    "endPos": {"line": 2, "column": 2},
                }
            ],
        },
    )

    response = repl.run("import Mathlib\n\n#check Missing", timeout=1)

    assert response["messages"][0]["pos"]["line"] == 3
    assert response["messages"][0]["endPos"]["line"] == 3
    assert response["sorries"][0]["pos"]["line"] == 4
    assert response["sorries"][0]["endPos"]["line"] == 4


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


def _repl_with_process(process: _PipeProcess, *, chunk_size: int = 4096, max_buffer_bytes: int = 1024):
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


def test_wire_protocol_accepts_response_split_across_reads(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages":', b" []}\n", b"\n"])
        repl = _repl_with_process(process, chunk_size=8)
        _patch_pipe_reads(monkeypatch, process)

        assert repl._run("#check Nat", env_id=3, timeout=1) == {"messages": []}

        request = process._stdin_read.read(4096)
        assert json.loads(request.decode().strip()) == {"cmd": "#check Nat", "env": 3}


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

        result = repl._run("#check Nat", env_id=None, timeout=1)

    assert result["messages"][0]["data"] == "Nat → Nat"


def test_wire_protocol_reports_complete_stderr_on_premature_eof(monkeypatch):
    stderr = (b"x" * 5000) + b"lean crashed"
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b""], stderr=stderr)
        repl = _repl_with_process(process)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplProcessExited) as error:
            repl._run("#check Nat", env_id=None, timeout=1)

    assert str(error.value).endswith(stderr.decode())


def test_wire_protocol_rejects_invalid_json(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b"not-json\n\n"])
        repl = _repl_with_process(process)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(json.JSONDecodeError):
            repl._run("#check Nat", env_id=None, timeout=1)


def test_wire_protocol_rejects_oversized_response(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"data":"0123456789"}\n\n'])
        repl = _repl_with_process(process, max_buffer_bytes=8)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(RuntimeError, match="response exceeded 8 bytes"):
            repl._run("#check Nat", env_id=None, timeout=1)
