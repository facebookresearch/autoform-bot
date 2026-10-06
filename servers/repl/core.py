"""Lean REPL backend: one session managing a ``lake exe repl`` subprocess.

Provides LeanRepl with non-blocking I/O, a preloaded import environment,
memory monitoring, automatic restart, and multi-snippet chaining.
"""

from __future__ import annotations

import errno
import json
import os
import random
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any, Callable

logger = getLogger(__name__)

DEFAULT_MAX_DIAGNOSTICS = 10
DEFAULT_SMOKE_TEST_TIMEOUT = 10
DEFAULT_REPL_STARTUP_TIMEOUT = 180.0
DEFAULT_REPL_CLEANUP_SECONDS = 2.0
REPL_ABORT_TERM_SECONDS = 0.5
REPL_ABORT_KILL_SECONDS = 1.0

ALLOWED_IMPORTS = frozenset({"Mathlib", "Aesop", "Batteries", "LeanSearchClient"})
WARMUP_IMPORTS = frozenset({"Mathlib"})
_VALID_DIAGNOSTIC_SEVERITIES = frozenset({"trace", "info", "warning", "error"})
_STDERR_TAIL_BYTES = 200
_PUBLIC_DIAGNOSTIC_FIELDS = frozenset({"severity", "data", "pos", "endPos"})
_PUBLIC_SORRY_FIELDS = frozenset({"goal", "pos", "endPos"})
LEAN_HEADER_LAUNCHER = (
    "import os; "
    "lean = os.path.join(os.environ['LEAN_SYSROOT'], 'bin', 'lean'); "
    "os.execv(lean, [lean, '--deps-json', '/dev/stdin'])"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_process_memory_gb(process: subprocess.Popen | None) -> float:
    """Return memory usage of a process and its children in GB."""
    if process is None or getattr(process, "returncode", None) is not None:
        return 0.0
    try:
        import psutil

        parent = psutil.Process(process.pid)
        total = parent.memory_info().rss
        for child in parent.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total / (1024**3)
    except Exception:
        return 0.0


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if error.errno == errno.EPERM:
            return True
        raise RuntimeError("failed to inspect the Lean process group") from error
    return True


def _process_group_has_live_members(process_group_id: int) -> bool:
    """Treat an all-zombie process group as terminated after SIGKILL."""
    if not _process_group_exists(process_group_id):
        return False

    import psutil

    for candidate in psutil.process_iter(["pid", "status"]):
        try:
            if os.getpgid(candidate.info["pid"]) != process_group_id:
                continue
            if candidate.info["status"] != psutil.STATUS_ZOMBIE:
                return True
        except (ProcessLookupError, PermissionError, psutil.Error):
            continue
    # Darwin may report EPERM for killpg(pgid, 0) after the final member exits.
    # psutil is the authoritative membership check for this same-user group.
    return False


def _wait_for_live_process_group_exit(process_group_id: int, deadline: float) -> bool:
    while _process_group_has_live_members(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def _wait_for_process(process: subprocess.Popen, deadline: float) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return process.poll() is not None
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        return False
    except OSError as error:
        raise RuntimeError("failed to reap the Lean process") from error
    return True


def _kill_subprocesses(
    process: subprocess.Popen,
    process_group_id: int,
    deadline: float | None = None,
) -> None:
    """Terminate and verify one dedicated POSIX process group."""
    if getattr(process, "returncode", None) is not None:
        if _process_group_has_live_members(process_group_id):
            raise RuntimeError(
                "refusing to signal a process group after its leader was reaped"
            )
        return
    _terminate_process_group(process_group_id, deadline)
    _reap_process(process, deadline)


def _terminate_process_group(
    process_group_id: int,
    deadline: float | None = None,
) -> None:
    """Terminate a dedicated group without reaping its leader."""
    started = time.monotonic()
    term_deadline = started + REPL_ABORT_TERM_SECONDS
    kill_deadline = term_deadline + REPL_ABORT_KILL_SECONDS
    if deadline is not None:
        term_deadline = min(term_deadline, deadline)
        kill_deadline = min(kill_deadline, deadline)

    if _process_group_has_live_members(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise RuntimeError("failed to terminate the Lean process group") from error

    _wait_for_live_process_group_exit(process_group_id, term_deadline)
    if _process_group_has_live_members(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise RuntimeError("failed to kill the Lean process group") from error

    group_exited = _wait_for_live_process_group_exit(process_group_id, kill_deadline)
    if not group_exited:
        raise RuntimeError("timed out terminating the Lean process group")


def _reap_process(
    process: subprocess.Popen,
    deadline: float | None = None,
) -> None:
    """Reap one leader only after its process group is known retired."""
    if getattr(process, "returncode", None) is not None:
        return
    kill_deadline = time.monotonic() + REPL_ABORT_KILL_SECONDS
    if deadline is not None:
        kill_deadline = min(kill_deadline, deadline)
    parent_reaped = _wait_for_process(process, kill_deadline)
    if not parent_reaped:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as error:
            raise RuntimeError("failed to kill the Lean process") from error
        parent_reaped = _wait_for_process(process, kill_deadline)
    if not parent_reaped:
        raise RuntimeError("timed out reaping the Lean process")


def _inherit_clean_env() -> dict[str, str]:
    """Return a copy of the current environment without PYTHONPATH noise."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def _is_natural_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_diagnostic_position(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and {"line", "column"} <= set(value)
        and _is_natural_number(value.get("line"))
        and _is_natural_number(value.get("column"))
    )


def _decode_repl_json(content: bytes) -> Any:
    """Decode strict JSON, rejecting duplicate keys and nonstandard constants."""

    def reject_constant(value: str) -> None:
        raise ReplProtocolError(
            f"Lean REPL returned nonstandard JSON constant {value!r}."
        )

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReplProtocolError(
                    f"Lean REPL returned duplicate JSON key {key!r}."
                )
            result[key] = value
        return result

    try:
        text = content.decode("utf-8")
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except UnicodeDecodeError as error:
        raise ReplProtocolError("Lean REPL returned invalid UTF-8.") from error


def _validate_command_response(
    response: Any,
    *,
    context: str,
    require_environment: bool,
) -> tuple[int | None, list[dict[str, Any]]]:
    """Validate the pinned REPL response fields used by Autoform."""
    if not isinstance(response, dict):
        raise ReplProtocolError(
            f"Lean REPL returned a malformed response for {context}."
        )
    if "message" in response:
        if set(response) == {"message"} and isinstance(response["message"], str):
            raise ReplCommandError(response["message"])
        raise ReplProtocolError(
            f"Lean REPL returned a malformed error response for {context}."
        )
    messages = response.get("messages", [])
    if not isinstance(messages, list):
        raise ReplProtocolError(
            f"Lean REPL returned malformed diagnostics for {context}."
        )
    for message in messages:
        severity = message.get("severity") if isinstance(message, dict) else None
        if (
            not isinstance(message, dict)
            or not {"severity", "data", "pos"} <= set(message)
            or not isinstance(severity, str)
            or severity not in _VALID_DIAGNOSTIC_SEVERITIES
            or not isinstance(message.get("data"), str)
            or not _valid_diagnostic_position(message.get("pos"))
        ):
            raise ReplProtocolError(
                f"Lean REPL returned malformed diagnostics for {context}."
            )
        end_pos = message.get("endPos")
        if end_pos is not None and not _valid_diagnostic_position(end_pos):
            raise ReplProtocolError(
                f"Lean REPL returned malformed diagnostics for {context}."
            )
    sorries = response.get("sorries", [])
    if not isinstance(sorries, list):
        raise ReplProtocolError(
            f"Lean REPL returned malformed sorries for {context}."
        )
    for sorry in sorries:
        pos = sorry.get("pos") if isinstance(sorry, dict) else None
        end_pos = sorry.get("endPos") if isinstance(sorry, dict) else None
        proof_state = sorry.get("proofState") if isinstance(sorry, dict) else None
        if (
            not isinstance(sorry, dict)
            or not isinstance(sorry.get("goal"), str)
            or "proofState" not in sorry
            or (pos is not None and not _valid_diagnostic_position(pos))
            or (end_pos is not None and not _valid_diagnostic_position(end_pos))
            or (proof_state is not None and not _is_natural_number(proof_state))
        ):
            raise ReplProtocolError(
                f"Lean REPL returned malformed sorries for {context}."
            )
    environment = response.get("env")
    if require_environment and not _is_natural_number(environment):
        raise ReplProtocolError(
            f"Lean REPL did not return a valid environment for {context}."
        )
    if (
        not require_environment
        and environment is not None
        and not _is_natural_number(environment)
    ):
        raise ReplProtocolError(
            f"Lean REPL returned an invalid environment for {context}."
        )
    return environment, messages


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    input_bytes: bytes,
    *,
    deadline: float,
    max_output_bytes: int,
) -> tuple[bytes, bytes]:
    """Exchange bytes with a child without unbounded output or time."""
    stdin_fd = process.stdin.fileno()
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    for fd in (stdin_fd, stdout_fd, stderr_fd):
        os.set_blocking(fd, False)

    pending = memoryview(input_bytes)
    offset = 0
    stdin_open = True
    stdout_open = True
    stderr_open = True
    stdout = bytearray()
    stderr = bytearray()

    def close_stdin() -> None:
        nonlocal stdin_open
        if not stdin_open:
            return
        stdin_open = False
        try:
            process.stdin.close()
        except OSError:
            pass

    while stdin_open or stdout_open or stderr_open:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out checking the Lean header")
        readable_fds: list[int] = []
        if stdout_open:
            readable_fds.append(stdout_fd)
        if stderr_open:
            readable_fds.append(stderr_fd)
        writable_fds = [stdin_fd] if stdin_open else []
        try:
            readable, writable, _ = select.select(
                readable_fds,
                writable_fds,
                [],
                remaining,
            )
        except InterruptedError:
            continue
        if not readable and not writable:
            raise TimeoutError("timed out checking the Lean header")

        for fd, target in ((stdout_fd, stdout), (stderr_fd, stderr)):
            if fd not in readable:
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                if fd == stdout_fd:
                    stdout_open = False
                else:
                    stderr_open = False
                continue
            if len(stdout) + len(stderr) + len(chunk) > max_output_bytes:
                raise ValueError(
                    f"Lean header parser output exceeded {max_output_bytes} bytes"
                )
            target.extend(chunk)

        if stdin_fd in writable:
            try:
                written = os.write(stdin_fd, pending[offset : offset + 65536])
            except BlockingIOError:
                continue
            except BrokenPipeError:
                close_stdin()
            else:
                if written <= 0:
                    close_stdin()
                else:
                    offset += written
                    if offset == len(pending):
                        close_stdin()

    close_stdin()
    # Do not poll or wait here. Process-group cleanup must verify that the
    # whole group is retired before reaping its leader, or a reused numeric
    # PGID could be signalled after the parser leaves a descendant behind.
    return bytes(stdout), bytes(stderr)


@dataclass(frozen=True)
class _LeanHeaderAnalysis:
    """Lean-owned facts needed to validate and compose one submitted header."""

    modules: tuple[str, ...]
    accepts_leading_imports: bool


def _decode_header_analysis(stdout: bytes) -> _LeanHeaderAnalysis:
    """Decode the strict schemas emitted by Lean's fast import parser."""
    try:
        payload = _decode_repl_json(stdout)
        if not isinstance(payload, dict) or set(payload) != {"imports"}:
            raise ValueError
        entries = payload["imports"]
        if not isinstance(entries, list) or len(entries) != 1:
            raise ValueError
        entry = entries[0]
        if not isinstance(entry, dict):
            raise ValueError
        errors = entry.get("errors")
        if not isinstance(errors, list) or not all(
            isinstance(error, str) and error for error in errors
        ):
            raise ValueError
        if errors:
            raise ValueError(errors[0])

        has_result = "result" in entry
        has_imports = "imports" in entry
        if has_result == has_imports:
            raise ValueError
        is_module: bool | None = None
        if has_result:
            result = entry["result"]
            if not isinstance(result, dict):
                raise ValueError
            imports = result.get("imports")
            is_module = result.get("isModule")
            if is_module is not None and type(is_module) is not bool:
                raise ValueError
        else:
            imports = entry["imports"]
        if not isinstance(imports, list):
            raise ValueError

        modules: list[str] = []
        has_implicit_init = False
        has_composition_metadata = has_result and is_module is not None
        for item in imports:
            if not isinstance(item, dict):
                raise ValueError
            module = item.get("module")
            if not isinstance(module, str) or not module:
                raise ValueError
            if "/" in module or "\\" in module:
                raise ValueError(
                    "Lean header contains an import module with a path separator"
                )
            if has_result:
                is_meta = item.get("isMeta")
                if is_meta is not None and type(is_meta) is not bool:
                    raise ValueError
                if is_meta is None:
                    has_composition_metadata = False
                if module == "Init" and is_meta:
                    has_implicit_init = True
            if module != "Init":
                modules.append(module)
        # Only the current schema exposes enough parser state to distinguish
        # Lean's implicit Init imports from an explicit ``prelude``/``module``
        # header. The legacy schema remains valid for allowlist checks, but
        # fails closed on source rewriting and sends the validated frame exact.
        return _LeanHeaderAnalysis(
            modules=tuple(modules),
            accepts_leading_imports=(
                has_composition_metadata
                and has_implicit_init
                and is_module is False
            ),
        )
    except (ReplProtocolError, TypeError, KeyError, json.JSONDecodeError):
        raise ValueError("unrecognized output from lean --deps-json") from None
    except ValueError as error:
        if str(error):
            raise
        raise ValueError("unrecognized output from lean --deps-json") from None


def _reject_legacy_deps_json_comment_bypass(code: str) -> None:
    """Refuse close spellings misparsed by Lean 4.30--4.32 deps-json.

    Those releases skip one character too many when an even run of dashes
    precedes ``/`` inside a block comment, so an import the real parser sees
    can disappear from the fast parser's result.  The byte pattern is rare;
    failing closed also avoids pretending that quoted/comment context can be
    reconstructed safely in Python.
    """

    dashes = 0
    for character in code:
        if character == "-":
            dashes += 1
            continue
        if character == "/" and dashes > 0 and dashes % 2 == 0:
            raise ValueError(
                "Lean header contains a block-comment close spelling that "
                "lean --deps-json cannot validate safely"
            )
        dashes = 0


def _split_imports_and_body(code: str) -> tuple[list[str], str, int]:
    """Split Lean code into import statements and body.

    Returns (import_names, body, header_line_count).
    """
    lines = code.split("\n")
    imports: list[str] = []
    body_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import "):
            imports.append(stripped[7:].strip())
            body_start = i + 1
        elif stripped == "" or stripped.startswith("--"):
            if imports:
                body_start = i + 1
        else:
            break

    body = "\n".join(lines[body_start:])
    return imports, body, body_start


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class LeanReplConfig:
    """Configuration for a Lean REPL instance."""

    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)

    request_timeout: float = 240.0
    startup_timeout: float = DEFAULT_REPL_STARTUP_TIMEOUT
    chunk_size: int = 4096

    instance_mem_limit_gb: int = 16
    max_retries: int = 1

    allowed_imports: frozenset[str] = ALLOWED_IMPORTS
    warmup_imports: frozenset[str] = WARMUP_IMPORTS

    repl_command: list[str] = field(default_factory=lambda: ["lake", "exe", "@repl/repl"])
    header_deps_command: list[str] = field(
        default_factory=lambda: ["lake", "env", sys.executable, "-c", LEAN_HEADER_LAUNCHER]
    )

    # stdout is capped per response. stderr has no protocol framing, so its
    # ceiling applies to the entire process generation and resets on restart.
    max_buffer_bytes: int = 10 * 1024 * 1024
    mem_restart_ratio: float = 0.9
    validate_imports: bool = True


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------


def _adjust_line_numbers(resp: dict, offset: int) -> None:
    """Offset all pos.line values so they map back to original source."""
    if offset == 0:
        return
    for msg in resp.get("messages", []):
        pos = msg.get("pos")
        if pos and isinstance(pos, dict) and "line" in pos:
            pos["line"] = pos["line"] + offset
        end_pos = msg.get("endPos")
        if end_pos and isinstance(end_pos, dict) and "line" in end_pos:
            end_pos["line"] = end_pos["line"] + offset
    for sorry in resp.get("sorries", []):
        pos = sorry.get("pos")
        if pos and isinstance(pos, dict) and "line" in pos:
            pos["line"] = pos["line"] + offset
        end_pos = sorry.get("endPos")
        if end_pos and isinstance(end_pos, dict) and "line" in end_pos:
            end_pos["line"] = end_pos["line"] + offset


def _without_process_handles(response: dict[str, Any]) -> dict[str, Any]:
    """Project a retired process response onto Autoform's public fields."""

    def public_fields(item: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
        projected = {key: value for key, value in item.items() if key in allowed}
        for key in ("pos", "endPos"):
            position = projected.get(key)
            if isinstance(position, dict):
                projected[key] = {
                    field: position[field]
                    for field in ("line", "column")
                    if field in position
                }
        return projected

    cleaned: dict[str, Any] = {}
    messages = response.get("messages")
    if isinstance(messages, list):
        cleaned["messages"] = [
            public_fields(message, _PUBLIC_DIAGNOSTIC_FIELDS)
            for message in messages
            if isinstance(message, dict)
        ]
    sorries = response.get("sorries")
    if isinstance(sorries, list):
        cleaned["sorries"] = [
            public_fields(sorry, _PUBLIC_SORRY_FIELDS)
            for sorry in sorries
            if isinstance(sorry, dict)
        ]
    return cleaned


def format_message(msg: dict) -> str:
    """Format one REPL message: ``"3:5: error: unknown identifier"``."""
    severity = msg.get("severity", "info")
    data = msg.get("data", "")
    pos = msg.get("pos")

    if pos and isinstance(pos, dict):
        line = pos.get("line")
        column = pos.get("column")
        if line is not None:
            if column is not None:
                return f"{line}:{column}: {severity}: {data}"
            return f"{line}: {severity}: {data}"

    return f"{severity}: {data}"


def format_repl_response(response: dict[str, Any]) -> str:
    """Parse a raw REPL response and format it as readable diagnostics."""
    if response.get("repl_error") is not None:
        if response.get("outcome_unknown") is True:
            return (
                "REPL error (execution outcome unknown; request not retried): "
                f"{response['repl_error']}"
            )
        return f"REPL error: {response['repl_error']}"

    messages = response.get("messages", [])
    sorries_raw = response.get("sorries", [])

    errors: list[str] = []
    warnings: list[str] = []
    infos: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        sev = msg.get("severity", "")
        if sev == "error":
            errors.append(format_message(msg))
        elif sev == "warning":
            warnings.append(format_message(msg))
        elif sev == "info":
            infos.append(format_message(msg))

    sorries: list[dict[str, Any]] = []
    for s in sorries_raw:
        if not isinstance(s, dict):
            continue
        pos = s.get("pos", {})
        sorries.append(
            {
                "line": pos.get("line", 0) if isinstance(pos, dict) else 0,
                "goal": s.get("goal", ""),
            }
        )

    parts: list[str] = []

    if errors:
        parts.append(f"Compilation Errors ({len(errors)})")
        for e in errors:
            parts.append(f"  - {e}")
    elif warnings:
        parts.append("Compiles successfully")
        parts.append(f"\nWarnings ({len(warnings)})")
        for w in warnings[:DEFAULT_MAX_DIAGNOSTICS]:
            parts.append(f"  - {w}")
        if len(warnings) > DEFAULT_MAX_DIAGNOSTICS:
            parts.append(f"  ... and {len(warnings) - DEFAULT_MAX_DIAGNOSTICS} more")
    elif infos:
        parts.append("Compiles successfully")
        parts.append(f"\nOutput ({len(infos)})")
        for i in infos[:DEFAULT_MAX_DIAGNOSTICS]:
            parts.append(f"  - {i}")
        if len(infos) > DEFAULT_MAX_DIAGNOSTICS:
            parts.append(f"  ... and {len(infos) - DEFAULT_MAX_DIAGNOSTICS} more")
    else:
        parts.append("Compiles successfully")

    if sorries:
        parts.append(f"\nSorries ({len(sorries)})")
        for s in sorries:
            parts.append(f"  - Line {s['line']}: {s['goal']}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LeanRepl
# ---------------------------------------------------------------------------


class ReplProtocolError(RuntimeError):
    """Raised when the REPL violates its pinned response protocol."""


class ReplCommandError(RuntimeError):
    """Raised when the REPL returns its explicit command-error variant."""


class ReplProcessExited(RuntimeError):
    """Raised when the REPL process dies unexpectedly."""


class ReplProcessRestarted(RuntimeError):
    """Raised when the REPL restarts and env_id state is lost."""


class ReplOutcomeUnknown(ReplProcessRestarted):
    """Raised when stderr poisoning leaves a sent command's outcome unknown."""


class ReplStderrBacklog(RuntimeError):
    """Raised when a response was captured but process stderr is no longer safe.

    The response data is valid and travels on ``response`` so a caller need not
    recompute it, but any ``env`` belongs to the process being retired and must
    not escape. stderr is unframed process output rather than command output, so
    an over-budget or undrainable process must not serve another request.
    """

    def __init__(self, message: str, response: Any) -> None:
        super().__init__(message)
        self.response = response


class ReplCleanupError(RuntimeError):
    """A disposable result is valid but its wrapper still owns a process."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


class LeanRepl:
    """Lean REPL process manager.

    Manages a ``lake exe repl`` subprocess with non-blocking I/O,
    a preloaded import environment, and automatic restart on failure.
    """

    def __init__(self, config: LeanReplConfig) -> None:
        self.config = config
        self.cwd = config.cwd
        self.process: subprocess.Popen | None = None
        self._process_group_id: int | None = None
        # Set while a process handle is owned but a close() has not completed.
        self._retire_pending = False

        self.request_timeout = config.request_timeout
        self.max_retries = config.max_retries

        self._base_env_id: int | None = None
        self.chunk_size: int = config.chunk_size

        self.mem_limit_gb: int = config.instance_mem_limit_gb

        self._process_lock = threading.Lock()
        # stderr has no command boundary. Account for it monotonically across one
        # process generation and retain only a bounded tail for diagnostics.
        self._stderr_bytes = 0
        self._stderr_tail = bytearray()

        self._allowed_import_roots: frozenset[str] | None = None
        if config.validate_imports and config.allowed_imports:
            self._allowed_import_roots = config.allowed_imports

    def start(
        self,
        startup_timeout: float | None = None,
        *,
        warm: bool = True,
    ) -> None:
        """Start and warm the Lean REPL within one startup deadline."""
        if os.name != "posix":
            raise RuntimeError("Lean REPL transport requires a POSIX platform")
        timeout = self.config.startup_timeout if startup_timeout is None else min(
            self.config.startup_timeout,
            startup_timeout,
        )
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError(f"REPL startup timed out after {timeout:g} seconds")
            return value

        env = _inherit_clean_env()
        env.update(self.config.env)

        try:
            self.process = subprocess.Popen(
                self.config.repl_command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            self._process_group_id = self.process.pid
            self._retire_pending = False
            self._stderr_bytes = 0
            self._stderr_tail.clear()

            if warm and self.config.warmup_imports:
                header = "\n".join(
                    f"import {root}" for root in self.config.warmup_imports
                )
                logger.info(
                    "Loading imports at startup: %s",
                    self.config.warmup_imports,
                )
                resp = self._run(code=header, env_id=None, timeout=remaining())
                environment, messages = _validate_command_response(
                    resp,
                    context="startup imports",
                    require_environment=True,
                )
                errors = [message for message in messages if message["severity"] == "error"]
                if errors:
                    error_details = "\n".join(message["data"] for message in errors)
                    raise RuntimeError(f"Import preloading failed:\n{error_details}")

                self._base_env_id = environment

                smoke = self._run(
                    code="#check Nat",
                    env_id=self._base_env_id,
                    timeout=min(DEFAULT_SMOKE_TEST_TIMEOUT, remaining()),
                )
                _, smoke_messages = _validate_command_response(
                    smoke,
                    context="the startup smoke test",
                    require_environment=True,
                )
                smoke_errors = [
                    message
                    for message in smoke_messages
                    if message["severity"] == "error"
                ]
                if smoke_errors:
                    error_details = "; ".join(message["data"] for message in smoke_errors)
                    raise RuntimeError(
                        f"REPL smoke test failed — LEAN_PATH may be misconfigured. Errors: {error_details}"
                    )
        except BaseException:
            try:
                self.close(deadline=deadline)
            except BaseException:
                logger.exception(
                    "failed to retire Lean REPL process after startup failure"
                )
            raise

    def close(self, *, deadline: float | None = None) -> None:
        """Close the Lean REPL process.

        A caller's deadline bounds cleanup only while it leaves at least
        ``DEFAULT_REPL_CLEANUP_SECONDS``. Request paths reach close() after their
        own deadline has expired, and a large Lean process cannot be reaped in a
        zero budget.
        """
        if deadline is not None:
            deadline = max(deadline, time.monotonic() + DEFAULT_REPL_CLEANUP_SECONDS)
        process = self.process
        process_group_id = self._process_group_id
        try:
            if process is not None:
                self._retire_pending = True
                if process_group_id is None:
                    try:
                        process_group_id = process.pid
                    except AttributeError:
                        process_group_id = None
                    else:
                        self._process_group_id = process_group_id
                if process_group_id is not None:
                    if deadline is None:
                        _kill_subprocesses(process, process_group_id)
                    else:
                        _kill_subprocesses(process, process_group_id, deadline)
            self.process = None
            self._process_group_id = None
            self._retire_pending = False
        finally:
            self._base_env_id = None
            self._stderr_bytes = 0
            self._stderr_tail.clear()

    def restart(self, timeout: float | None = None) -> None:
        """Restart the Lean REPL process within an optional total timeout."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        self.close(deadline=deadline)
        if deadline is None:
            self.start()
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"REPL restart timed out after {timeout:g} seconds")
        self.start(startup_timeout=remaining)

    def is_alive(self) -> bool:
        """Conservatively check without reaping the process-group leader.

        A process whose close() failed is not alive even while its leader is
        unreaped, so the next request retries cleanup instead of dispatching.
        """
        return (
            self.process is not None
            and not self._retire_pending
            and getattr(self.process, "returncode", None) is None
        )

    def get_memory_usage(self) -> float:
        """Return memory usage in GB."""
        return _get_process_memory_gb(self.process)

    def is_clean(self) -> bool:
        """Return whether this wrapper owns no process generation."""
        return self.process is None and self._process_group_id is None

    def _check_header(self, code: str, deadline: float) -> _LeanHeaderAnalysis:
        """Analyze the submitted header with the selected toolchain's Lean parser.

        The parser is this wrapper's process generation until close() verifies
        that it exited, so a failed cleanup is retried before the slot is reused.
        """
        env = _inherit_clean_env()
        env.update(self.config.env)
        self.process = subprocess.Popen(
            self.config.header_deps_command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        process = self.process
        self._process_group_id = process.pid
        stdout, stderr = _communicate_bounded(
            process,
            code.encode(),
            deadline=deadline,
            max_output_bytes=self.config.max_buffer_bytes,
        )
        self.close(deadline=deadline)
        returncode = process.returncode
        if returncode != 0:
            detail = stderr.decode(errors="replace").strip().splitlines()
            raise ValueError(detail[0] if detail else f"exit status {returncode}")
        return _decode_header_analysis(stdout)

    def run_disposable(
        self,
        code: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run one public call as the only request frame on a fresh process."""
        timeout = self.request_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout

        def remaining(phase: str) -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError(
                    f"Lean REPL {phase} timed out after {timeout:g} seconds"
                )
            return value

        with self._process_lock:
            result: dict[str, Any] | None = None
            request_error: BaseException | None = None
            added_imports: tuple[str, ...] = ()
            try:
                # A previous failed cleanup must settle before another process
                # generation can be created from this slot.
                self.close(deadline=deadline)
                if os.name != "posix":
                    raise RuntimeError("Lean REPL transport requires a POSIX platform")
                imports, _, _ = _split_imports_and_body(code)
                accepts_leading_imports = True
                if (
                    self.config.validate_imports
                    and self._allowed_import_roots is not None
                ):
                    try:
                        _reject_legacy_deps_json_comment_bypass(code)
                        header = self._check_header(code, deadline)
                    except ValueError as error:
                        result = {"repl_error": f"Rejected Lean header: {error}"}
                    else:
                        imports = list(header.modules)
                        accepts_leading_imports = header.accepts_leading_imports
                submitted_roots = {statement.split(".")[0] for statement in imports}
                if (
                    result is None
                    and self.config.validate_imports
                    and self._allowed_import_roots is not None
                ):
                    warmup_roots = {root.split(".")[0] for root in self.config.warmup_imports}
                    disallowed = (submitted_roots | warmup_roots) - self._allowed_import_roots
                    if disallowed:
                        result = {
                            "repl_error": (
                                f"Disallowed imports: {', '.join(sorted(disallowed))}. "
                                "Allowed roots: "
                                f"{', '.join(sorted(self._allowed_import_roots))}."
                            )
                        }
                if result is None:
                    added_imports = tuple(
                        root
                        for root in sorted(self.config.warmup_imports)
                        if accepts_leading_imports and root not in submitted_roots
                    )
                    prefix = "\n".join(
                        f"import {root}" for root in added_imports
                    )
                    command = f"{prefix}\n{code}" if prefix else code
                    # Do not send startup import or smoke-test frames. The
                    # submitted command is the generation's only request.
                    self.start(
                        startup_timeout=remaining("disposable child startup"),
                        warm=False,
                    )
                    response = self._run(
                        code=command,
                        env_id=None,
                        timeout=remaining("combined command execution"),
                    )
                    _validate_command_response(
                        response,
                        context="the requested command",
                        require_environment=True,
                    )
                    _adjust_line_numbers(response, -len(added_imports))
                    result = _without_process_handles(response)
            except ReplStderrBacklog as error:
                try:
                    _validate_command_response(
                        error.response,
                        context="the requested command",
                        require_environment=True,
                    )
                except ReplCommandError as command_error:
                    result = {"repl_error": str(command_error)}
                except ReplProtocolError as protocol_error:
                    result = {
                        "repl_error": str(protocol_error),
                        "outcome_unknown": True,
                    }
                else:
                    response = _without_process_handles(error.response)
                    _adjust_line_numbers(response, -len(added_imports))
                    result = response
            except ReplCommandError as error:
                result = {"repl_error": str(error)}
            except (ReplProtocolError, ReplOutcomeUnknown) as error:
                result = {"repl_error": str(error), "outcome_unknown": True}
            except (ReplProcessExited, TimeoutError, RuntimeError, OSError) as error:
                result = {"repl_error": str(error)}
            except BaseException as error:
                request_error = error
            finally:
                try:
                    self.close(
                        deadline=time.monotonic() + DEFAULT_REPL_CLEANUP_SECONDS
                    )
                except BaseException as cleanup_error:
                    logger.exception(
                        "failed to retire disposable Lean REPL process"
                    )
                    if request_error is not None:
                        note = f"Lean REPL process cleanup also failed: {cleanup_error}"
                        add_note = getattr(request_error, "add_note", None)
                        if add_note is not None:
                            add_note(note)
                    elif not isinstance(cleanup_error, Exception):
                        raise
                    elif result is not None:
                        raise ReplCleanupError(
                            "Disposable Lean REPL cleanup failed after a result "
                            f"was produced; pool retirement is required: {cleanup_error}",
                            result,
                        ) from cleanup_error

            if request_error is not None:
                raise request_error.with_traceback(request_error.__traceback__)
            return result

    def run(self, code: str, env_id: int | None = None, timeout: float | None = None) -> dict[str, Any]:
        """Send code to the REPL within one deadline across recovery attempts."""
        timeout = self.request_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError(f"REPL command timed out after {timeout:g} seconds")
            return value

        run_from_env = env_id is not None
        max_retries = 0 if run_from_env else self.max_retries

        header_line_count = 0
        if not run_from_env:
            imports, code, header_line_count = _split_imports_and_body(code)

            if self.config.validate_imports and self._allowed_import_roots is not None:
                submitted_roots = {stmt.split(".")[0] for stmt in imports}
                disallowed = submitted_roots - self._allowed_import_roots
                if disallowed:
                    return {
                        "repl_error": (
                            f"Disallowed imports: {', '.join(sorted(disallowed))}. "
                            f"Allowed roots: {', '.join(sorted(self._allowed_import_roots))}."
                        )
                    }

        last_exception: Exception | None = None
        with self._process_lock:
            if run_from_env and not self.is_alive():
                self.close(deadline=deadline)
                raise ReplProcessRestarted(
                    "REPL process restarted before the request; environment state was lost"
                )

            process_before_memory_check = self.process
            try:
                if not self.is_alive():
                    self.restart(timeout=remaining())
                self._check_memory_and_maybe_restart(timeout=remaining())
            except (TimeoutError, RuntimeError) as error:
                self.close(deadline=deadline)
                if run_from_env:
                    raise ReplProcessRestarted(str(error)) from error
                return {"repl_error": str(error)}

            if run_from_env and self.process is not process_before_memory_check:
                raise ReplProcessRestarted(
                    "REPL process restarted before the request; environment state was lost"
                )

            for i in range(max_retries + 1):
                try:
                    dispatch_env_id = env_id if run_from_env else self._base_env_id
                    resp = self._run(
                        code=code,
                        env_id=dispatch_env_id,
                        timeout=remaining(),
                    )
                    _validate_command_response(
                        resp,
                        context="the requested command",
                        require_environment=True,
                    )
                    _adjust_line_numbers(resp, header_line_count)
                    return resp
                except ReplStderrBacklog as e:
                    # _run() already retired the process, so nothing can inherit the
                    # undrained stderr; close() here is an idempotent assertion of
                    # that. The response is valid, so a plain request still receives
                    # it. An env-scoped request cannot transparently outlive the
                    # process that held its environment, so it is told loudly.
                    logger.error("%s", e)
                    self.close(deadline=deadline)
                    if run_from_env:
                        raise ReplProcessRestarted(str(e)) from e
                    try:
                        _validate_command_response(
                            e.response,
                            context="the requested command",
                            require_environment=True,
                        )
                    except ReplCommandError as error:
                        return {"repl_error": str(error)}
                    except ReplProtocolError as error:
                        return {
                            "repl_error": str(error),
                            "outcome_unknown": True,
                        }
                    # The command's diagnostics remain valid, but any environment
                    # identifier belongs to the process _run() just retired.
                    response = _without_process_handles(e.response)
                    _adjust_line_numbers(response, header_line_count)
                    return response
                except ReplOutcomeUnknown as e:
                    # The request was fully written, so replay could execute it
                    # twice. Retire the process and report the unknown outcome
                    # without entering the ordinary retry path.
                    logger.error("%s", e)
                    message = str(e)
                    try:
                        self.close(deadline=deadline)
                    except Exception as cleanup_error:
                        message += (
                            f"; process cleanup also failed: {cleanup_error}"
                        )
                    if run_from_env:
                        raise ReplOutcomeUnknown(message) from e
                    return {"repl_error": message, "outcome_unknown": True}
                except ReplCommandError as e:
                    logger.error("Lean REPL rejected the command: %s", e)
                    return {"repl_error": str(e)}
                except ReplProtocolError as e:
                    logger.error("%s", e)
                    message = str(e)
                    try:
                        self.close(deadline=deadline)
                    except Exception as cleanup_error:
                        message += (
                            f"; process cleanup also failed: {cleanup_error}"
                        )
                    if run_from_env:
                        raise ReplProcessRestarted(message) from e
                    return {"repl_error": message, "outcome_unknown": True}
                except ReplProcessExited as e:
                    last_exception = e
                    logger.error("REPL process exited: %s. Attempt %d/%d.", e, i + 1, max_retries + 1)
                    if run_from_env:
                        self.close(deadline=deadline)
                        raise ReplProcessRestarted(str(e)) from e
                except (TimeoutError, RuntimeError, json.JSONDecodeError) as e:
                    last_exception = e
                    logger.error("Error running command: %s. Attempt %d/%d.", e, i + 1, max_retries + 1)
                    if run_from_env:
                        self.close(deadline=deadline)
                        raise ReplProcessRestarted(str(e)) from e

                if i >= max_retries:
                    self.close(deadline=deadline)
                    break

                backoff = min(2**i, 30) + random.uniform(0, 1)
                try:
                    if backoff >= remaining():
                        raise TimeoutError(
                            f"REPL command timed out after {timeout:g} seconds"
                        )
                    time.sleep(backoff)
                    self.restart(timeout=remaining())
                except (TimeoutError, RuntimeError) as error:
                    last_exception = error
                    self.close(deadline=deadline)
                    break
            logger.error("Exceeded maximum retries for Lean REPL command")
            return {"repl_error": str(last_exception)}

    def _check_memory_and_maybe_restart(self, timeout: float | None = None) -> None:
        """Proactively restart if memory usage is near the limit."""
        if self.mem_limit_gb <= 0 or self.config.mem_restart_ratio <= 0:
            return
        try:
            usage_gb = self.get_memory_usage()
            threshold_gb = self.mem_limit_gb * self.config.mem_restart_ratio
            if usage_gb >= threshold_gb:
                logger.info("REPL memory %.2fGB >= threshold %.2fGB, restarting...", usage_gb, threshold_gb)
                self.restart(timeout=timeout)
        except (TimeoutError, RuntimeError):
            raise
        except Exception:
            logger.warning("Memory check failed, continuing", exc_info=True)

    def _run(self, code: str, env_id: int | None, timeout: float) -> dict[str, Any]:
        """Run one frame and distinguish safe pre-send failures from unknown outcomes."""
        request_sent = False
        cleanup_deadline = time.monotonic() + timeout

        def mark_sent() -> None:
            nonlocal request_sent
            request_sent = True

        try:
            return self._run_io(code, env_id, timeout, mark_sent)
        except ReplOutcomeUnknown as error:
            message = str(error)
            try:
                self.close(deadline=cleanup_deadline)
            except Exception as cleanup_error:
                message += f"; process cleanup also failed: {cleanup_error}"
                raise ReplOutcomeUnknown(message) from error
            raise
        except ReplStderrBacklog:
            raise
        except Exception as error:
            if request_sent:
                message = (
                    "Lean REPL transport failed after the request was fully sent; "
                    "its execution outcome is unknown and was not retried: "
                    f"{error}"
                )
                try:
                    self.close(deadline=cleanup_deadline)
                except Exception as cleanup_error:
                    message += f"; process cleanup also failed: {cleanup_error}"
                raise ReplOutcomeUnknown(message) from error
            self.close(deadline=cleanup_deadline)
            raise
        except BaseException as error:
            try:
                self.close(deadline=cleanup_deadline)
            except BaseException as cleanup_error:
                note = f"Lean REPL process cleanup also failed: {cleanup_error}"
                add_note = getattr(error, "add_note", None)
                if add_note is not None:
                    add_note(note)
                else:  # pragma: no cover - Python 3.10 compatibility
                    logger.error("%s", note)
            raise

    def _run_io(
        self,
        code: str,
        env_id: int | None,
        timeout: float,
        mark_sent: Callable[[], None],
    ) -> dict[str, Any]:
        """Send code to the REPL via stdin JSON-RPC, read response via non-blocking I/O."""
        cmd_obj: dict[str, Any] = {"cmd": code}
        if env_id is not None:
            cmd_obj["env"] = env_id
        command = json.dumps(cmd_obj) + "\n\n"

        if (
            self.process is None
            or getattr(self.process, "returncode", None) is not None
            or self.process.stdin is None
            or self.process.stdout is None
            or self.process.stderr is None
        ):
            raise ReplProcessExited("REPL process is not running.")

        end_time = time.monotonic() + timeout
        stdin_fd = self.process.stdin.fileno()
        stdout_fd = self.process.stdout.fileno()
        stderr_fd = self.process.stderr.fileno()
        os.set_blocking(stdin_fd, False)
        os.set_blocking(stdout_fd, False)
        os.set_blocking(stderr_fd, False)
        response_buffer = bytearray()
        max_buffer = self.config.max_buffer_bytes
        stderr_drained = True
        stderr_open = True
        stderr_poison_reason: str | None = None

        def stderr_details() -> tuple[int, str]:
            stderr_bytes = self._stderr_bytes
            stderr_tail = bytes(self._stderr_tail).decode("utf-8", errors="replace")
            return stderr_bytes, stderr_tail

        def raise_unknown_stderr_outcome() -> None:
            stderr_bytes, stderr_tail = stderr_details()
            reason = stderr_poison_reason or "stderr could not be drained"
            self.close(deadline=end_time)
            raise ReplOutcomeUnknown(
                f"REPL process-generation stderr became unsafe after the request "
                f"was sent ({reason}; {stderr_bytes} bytes observed); "
                f"the execution outcome is unknown and was not retried. Tail: {stderr_tail!r}"
            )

        def retire_before_request() -> None:
            stderr_bytes, stderr_tail = stderr_details()
            reason = stderr_poison_reason or "stderr became unsafe"
            self.close(deadline=end_time)
            raise ReplProcessExited(
                f"REPL process-generation stderr became unsafe before the request "
                f"frame was fully sent ({reason}; {stderr_bytes} bytes observed); "
                f"the process was recycled. Tail: {stderr_tail!r}"
            )

        def reject_unsolicited_stdout() -> None:
            try:
                chunk = os.read(stdout_fd, self.chunk_size)
            except BlockingIOError:
                return
            except OSError as error:
                self.close(deadline=end_time)
                raise ReplProcessExited(
                    "Lean REPL stdout failed before the request frame was fully sent"
                ) from error
            self.close(deadline=end_time)
            if chunk:
                raise ReplProcessExited(
                    "Lean REPL emitted unsolicited stdout before the request frame "
                    "was fully sent; the process was recycled"
                )
            raise ReplProcessExited(
                "Lean REPL closed stdout before the request frame was fully sent"
            )

        def drain_stderr(*, max_reads: int | None = None, after_response: bool = False) -> bool:
            """Drain process stderr fairly while retaining a bounded tail.

            ``max_reads`` bounds a single fairness cycle so a process that writes
            diagnostics continuously cannot starve stdout.

            ``after_response`` marks the drain that runs once the response frame is
            complete. It is unbounded in reads because no stdout read is left to
            starve, but the deadline and process-generation stderr ceiling stop it
            without destroying a response already captured.

            Returns whether stderr is currently empty and the process generation
            remains within budget. EAGAIN is never treated as a command boundary;
            the byte count and tail persist until the process is replaced.
            """
            nonlocal stderr_open, stderr_poison_reason

            if not stderr_open:
                return False

            reads = 0
            while max_reads is None or reads < max_reads:
                if after_response:
                    if stderr_poison_reason is not None:
                        return False
                    if time.monotonic() >= end_time:
                        readable, _, _ = select.select([stderr_fd], [], [], 0)
                        if not readable:
                            return True
                        stderr_poison_reason = "stderr remained readable at the command deadline"
                        return False
                if max_reads is None and not after_response and time.monotonic() >= end_time:
                    if stderr_poison_reason is not None:
                        raise_unknown_stderr_outcome()
                    raise TimeoutError(f"REPL command timed out after {timeout} seconds while reading stderr")
                try:
                    chunk = os.read(stderr_fd, self.chunk_size)
                except BlockingIOError:
                    return stderr_poison_reason is None
                except OSError as error:
                    stderr_open = False
                    stderr_poison_reason = f"stderr read failed: {error}"
                    return False
                if not chunk:
                    stderr_open = False
                    stderr_poison_reason = "stderr closed unexpectedly"
                    return False
                self._stderr_bytes += len(chunk)
                if len(chunk) >= _STDERR_TAIL_BYTES:
                    self._stderr_tail[:] = chunk[-_STDERR_TAIL_BYTES:]
                else:
                    overflow = len(self._stderr_tail) + len(chunk) - _STDERR_TAIL_BYTES
                    if overflow > 0:
                        del self._stderr_tail[:overflow]
                    self._stderr_tail.extend(chunk)
                reads += 1
                logger.debug(
                    "Lean REPL stderr: %s",
                    chunk.decode("utf-8", errors="replace").rstrip(),
                )
                if self._stderr_bytes > max_buffer and stderr_poison_reason is None:
                    stderr_poison_reason = (
                        f"stderr exceeded the {max_buffer}-byte process-generation ceiling"
                    )
                if after_response and stderr_poison_reason is not None:
                    return False
            return stderr_poison_reason is None

        # stdout and stderr are independent pipes. A child blocked on a full
        # stderr pipe may be unable to read its stdin, so service stderr fairly
        # while writing instead of waiting on stdin alone. Any stderr observed
        # here remains process-scoped; it is never assigned to this command.
        encoded_command = command.encode("utf-8")
        # Hold back the final blank-line byte. The REPL cannot dispatch this
        # command until that delimiter arrives, which gives us a stdout check
        # after the complete request body has been written. Once the delimiter
        # is sent, output belongs to this request under the REPL's sequential
        # one-response-per-frame protocol.
        payloads = (memoryview(encoded_command[:-1]), memoryview(encoded_command[-1:]))
        payload_index = 0
        payload = payloads[payload_index]
        offset = 0
        while payload_index < len(payloads):
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                if stderr_poison_reason is not None:
                    retire_before_request()
                raise TimeoutError(
                    f"REPL command timed out after {timeout} seconds while writing"
                )
            readable_fds = [stdout_fd]
            if stderr_open:
                readable_fds.append(stderr_fd)
            readable, writable, _ = select.select(
                readable_fds,
                [stdin_fd],
                [],
                remaining,
            )
            if not readable and not writable:
                if stderr_poison_reason is not None:
                    retire_before_request()
                raise TimeoutError(
                    f"REPL command timed out after {timeout} seconds while writing"
                )
            if stderr_fd in readable:
                drain_stderr(max_reads=1)
                if stderr_poison_reason is not None:
                    retire_before_request()
            if stdout_fd in readable:
                reject_unsolicited_stdout()
            if stdin_fd not in writable:
                continue
            dispatching = payload_index == len(payloads) - 1
            if dispatching:
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    if stderr_poison_reason is not None:
                        retire_before_request()
                    raise TimeoutError(
                        f"REPL command timed out after {timeout} seconds while writing"
                    )
                # Once writing the final delimiter is attempted, the REPL may
                # dispatch the request even if the write reports an error.
                mark_sent()
            try:
                written = os.write(stdin_fd, payload[offset:])
            except BlockingIOError:
                continue
            except OSError as error:
                raise ReplProcessExited(
                    f"REPL process closed stdin while writing: {error}"
                ) from error
            if written <= 0:
                raise ReplProcessExited("REPL process closed stdin while writing")
            offset += written
            if offset == len(payload):
                payload_index += 1
                if payload_index < len(payloads):
                    payload = payloads[payload_index]
                    offset = 0
        while True:
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                if stderr_poison_reason is not None:
                    raise_unknown_stderr_outcome()
                raise TimeoutError(f"REPL command timed out after {timeout} seconds")

            readable_fds = [stdout_fd]
            if stderr_open:
                readable_fds.append(stderr_fd)
            ready, _, _ = select.select(readable_fds, [], [], remaining)
            if not ready:
                if stderr_poison_reason is not None:
                    raise_unknown_stderr_outcome()
                raise TimeoutError(f"REPL command timed out after {timeout} seconds")

            # Drain diagnostics before handling stdout EOF so a crashing Lean
            # process cannot lose stderr that became readable at the same time.
            if stderr_fd in ready:
                drain_stderr(max_reads=1)

            if stdout_fd in ready:
                try:
                    chunk = os.read(stdout_fd, self.chunk_size)
                except BlockingIOError:
                    continue
                if not chunk:
                    if stderr_open:
                        # Capture one final diagnostic chunk without waiting past
                        # the command deadline or spinning on a noisy process.
                        drain_stderr(max_reads=1)
                    if stderr_poison_reason is not None:
                        raise_unknown_stderr_outcome()
                    stderr_text = self._stderr_tail.decode("utf-8", errors="replace")
                    raise ReplProcessExited(f"REPL process exited. stderr: {stderr_text}")
                response_buffer.extend(chunk)

                if len(response_buffer) > max_buffer:
                    if stderr_poison_reason is not None:
                        raise_unknown_stderr_outcome()
                    tail = bytes(response_buffer[-200:]).decode(
                        "utf-8",
                        errors="replace",
                    )
                    raise RuntimeError(
                        f"REPL response exceeded {max_buffer} bytes. Tail: {tail!r}"
                    )

                separator = response_buffer.find(b"\n\n")
                if separator >= 0:
                    trailing = response_buffer[separator + 2 :]
                    if trailing:
                        raise ReplProtocolError(
                            "Lean REPL emitted unsolicited bytes after its response frame"
                        )
                    response_bytes = bytes(response_buffer[:separator]).strip()
                    # The frame is complete, so this command's remaining queued
                    # stderr can be drained without starving stdout. Leaving it in
                    # the pipe would let a command exceed the stderr ceiling
                    # unnoticed, misattribute diagnostics to the next command, and
                    # eventually block the child on a full stderr pipe.
                    stderr_drained = drain_stderr(after_response=True)
                    break

        if not stderr_drained:
            stderr_bytes, stderr_tail = stderr_details()
            stderr_reason = stderr_poison_reason or "stderr could not be drained"
            # stderr is accounted to the process generation, never to whichever
            # command happened to observe it. Once that generation exceeds its
            # quota or cannot be drained, retire it before another request.
            self.close(deadline=end_time)

        # Retire a desynchronized process before parsing. Malformed JSON must not
        # bypass the stream-safety invariant and leave stale stderr reusable.
        try:
            response = _decode_repl_json(response_bytes)
        except (json.JSONDecodeError, ReplProtocolError) as error:
            if not stderr_drained:
                raise ReplOutcomeUnknown(
                    f"REPL process-generation stderr became unsafe after the request "
                    f"was sent ({stderr_reason}; {stderr_bytes} bytes observed), and "
                    "the response frame was malformed; the execution outcome is "
                    f"unknown and was not retried. Tail: {stderr_tail!r}"
                ) from error
            raise
        if not stderr_drained:
            raise ReplStderrBacklog(
                f"REPL process-generation stderr became unsafe ({stderr_reason}; "
                f"{stderr_bytes} bytes observed); "
                f"the process was recycled. Tail: {stderr_tail!r}",
                response,
            )
        return response
