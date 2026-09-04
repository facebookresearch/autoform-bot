"""Lean REPL backend: one session managing a ``lake exe repl`` subprocess.

Provides LeanRepl with non-blocking I/O, private import-context caching,
memory monitoring, and automatic restart.
"""

from __future__ import annotations

import json
import os
import random
import select
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any

from servers import ProjectFingerprint, lean_project_fingerprint

from .imports import (
    ResolvedImports,
    StaleResolvedImportsError,
    clean_lake_environment,
    split_imports_and_body as _split_imports_and_body,
)

logger = getLogger(__name__)

DEFAULT_MAX_DIAGNOSTICS = 10
DEFAULT_SMOKE_TEST_TIMEOUT = 10
DEFAULT_REPL_STARTUP_TIMEOUT = 180.0
DEFAULT_MAX_CONTEXTS_PER_PROCESS = 256

ALLOWED_IMPORTS = frozenset({"Mathlib", "Aesop", "Batteries", "LeanSearchClient"})
WARMUP_IMPORTS = frozenset({"Mathlib"})
_VALID_DIAGNOSTIC_SEVERITIES = frozenset({"trace", "info", "warning", "error"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_process_memory_gb(process: subprocess.Popen | None) -> float:
    """Return memory usage of a process and its children in GB."""
    if process is None or process.poll() is not None:
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


def _kill_subprocesses(process: subprocess.Popen) -> None:
    """Kill a process and all its children."""
    try:
        import psutil

        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
        parent.wait(timeout=5)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass


def _inherit_clean_env() -> dict[str, str]:
    """Return the host environment without ambient Python or Lean paths."""
    return clean_lake_environment()


def _is_natural_number(value: Any) -> bool:
    """Return whether a JSON value is a Lean ``Nat`` rather than a boolean."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_diagnostic_position(value: Any) -> bool:
    """Return whether a JSON value has the REPL's source-position shape."""
    return (
        isinstance(value, dict)
        and _is_natural_number(value.get("line"))
        and _is_natural_number(value.get("column"))
    )


def _validate_command_response(
    response: Any,
    *,
    context: str,
    require_environment: bool = True,
) -> tuple[int | None, list[dict[str, Any]]]:
    """Validate the protocol fields needed before retaining a REPL environment."""
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

    env_id = response.get("env")
    if require_environment and not _is_natural_number(env_id):
        raise ReplProtocolError(
            f"Lean REPL did not return a valid environment for {context}."
        )
    if not require_environment and env_id is not None and not _is_natural_number(env_id):
        raise ReplProtocolError(
            f"Lean REPL returned an invalid environment for {context}."
        )
    return env_id, messages


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EnvironmentHandle:
    project_identity: Path
    worker_token: object
    process_generation: int
    import_context: tuple[str, ...]
    resolved_imports: ResolvedImports | None
    env_id: int


@dataclass
class LeanReplConfig:
    """Configuration for a Lean REPL instance."""

    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)

    request_timeout: float = 30.0
    startup_timeout: float = DEFAULT_REPL_STARTUP_TIMEOUT
    chunk_size: int = 4096

    instance_mem_limit_gb: int = 16
    mem_interval_check: float = 1.0
    max_retries: int = 1

    allowed_imports: frozenset[str] = ALLOWED_IMPORTS
    warmup_imports: frozenset[str] = WARMUP_IMPORTS

    repl_command: list[str] = field(default_factory=lambda: ["lake", "exe", "repl"])

    # stdout is capped per response. stderr has no protocol framing, so its
    # ceiling applies to the entire process generation and resets on restart.
    max_buffer_bytes: int = 10 * 1024 * 1024
    mem_restart_ratio: float = 0.9
    validate_imports: bool = True
    max_contexts_per_process: int = DEFAULT_MAX_CONTEXTS_PER_PROCESS

    def __post_init__(self) -> None:
        """Reject limits that cannot bound the configured startup contexts."""
        limit = self.max_contexts_per_process
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("max_contexts_per_process must be a positive integer")
        minimum = 4 if self.warmup_imports else 3
        if limit < minimum:
            raise ValueError(
                "max_contexts_per_process must allow startup, import, and request contexts"
            )


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
    """Copy a response without IDs owned by a retired REPL process."""
    cleaned = dict(response)
    cleaned.pop("env", None)
    cleaned.pop("proofState", None)
    for response_field in ("sorries", "tactics"):
        values = cleaned.get(response_field)
        if not isinstance(values, list):
            continue
        cleaned[response_field] = [
            {key: value for key, value in item.items() if key != "proofState"}
            if isinstance(item, dict)
            else item
            for item in values
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
    """Raised when a REPL response violates the pinned JSON protocol."""


class ReplCommandError(RuntimeError):
    """Raised for the pinned REPL's explicit error-response variant."""


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


class LeanRepl:
    """Lean REPL process manager.

    Manages a ``lake exe repl`` subprocess with non-blocking I/O,
    a preloaded import environment, and automatic restart on failure.
    """

    def __init__(self, config: LeanReplConfig) -> None:
        self.config = config
        self.cwd = config.cwd
        self._project_identity = Path(config.cwd).resolve()
        self.process: subprocess.Popen | None = None

        self.request_timeout = config.request_timeout
        self.max_retries = config.max_retries

        self._worker_token = object()
        self._process_generation = 0
        self._base_environment: _EnvironmentHandle | None = None
        self._import_environments: dict[tuple[str, ...], _EnvironmentHandle] = {}
        self._contexts_created = 0
        self._project_fingerprint: ProjectFingerprint | None = None
        self.chunk_size: int = config.chunk_size

        self.mem_limit_gb: int = config.instance_mem_limit_gb

        self._process_lock = threading.Lock()
        self._request_deadline: float | None = None
        # stderr has no command boundary. Account for it monotonically across one
        # process generation and retain only a bounded tail for diagnostics.
        self._stderr_bytes = 0
        self._stderr_tail = bytearray()

        self._allowed_import_roots: frozenset[str] | None = None
        if config.validate_imports and config.allowed_imports:
            self._allowed_import_roots = config.allowed_imports

    def start(self, startup_timeout: float | None = None) -> None:
        """Start and warm the Lean REPL within one startup deadline."""
        self._base_environment = None
        self._import_environments.clear()
        self._contexts_created = 0
        self._project_fingerprint = None
        timeout = self.config.startup_timeout if startup_timeout is None else min(
            self.config.startup_timeout,
            startup_timeout,
        )
        deadline = time.monotonic() + timeout
        if self._request_deadline is not None:
            deadline = min(deadline, self._request_deadline)

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError(f"REPL startup timed out after {timeout:g} seconds")
            return value

        env = _inherit_clean_env()
        env.update(self.config.env)
        startup_fingerprint = lean_project_fingerprint(self._project_identity)

        self.process = subprocess.Popen(
            self.config.repl_command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._stderr_bytes = 0
        self._stderr_tail.clear()

        try:
            base_env_id: int | None = None
            base_imports = tuple(sorted(self.config.warmup_imports))
            if base_imports:
                header = "\n".join(f"import {root}" for root in base_imports)
                logger.info("Loading imports at startup: %s", base_imports)
                resp = self._run_counted(
                    code=header,
                    env_id=None,
                    timeout=remaining(),
                )
                base_env_id, messages = _validate_command_response(
                    resp,
                    context="startup imports",
                )
                errors = [m for m in messages if m["severity"] == "error"]
                if errors:
                    error_details = "\n".join(m["data"] for m in errors)
                    raise RuntimeError(f"Import preloading failed:\n{error_details}")
                if self._contexts_created + 1 > self.config.max_contexts_per_process:
                    raise RuntimeError("REPL startup exceeded its context limit")

            # A retained Init-derived environment keeps every later source request
            # in command mode. Otherwise a header-scanner miss sent without an
            # environment could execute imports in the REPL's fresh-file mode.
            smoke = self._run_counted(
                code="#check Nat",
                env_id=base_env_id,
                timeout=min(DEFAULT_SMOKE_TEST_TIMEOUT, remaining()),
            )
            smoke_env_id, smoke_messages = _validate_command_response(
                smoke,
                context="the startup smoke test",
            )
            smoke_errors = [m for m in smoke_messages if m["severity"] == "error"]
            if smoke_errors:
                error_details = "; ".join(m["data"] for m in smoke_errors)
                raise RuntimeError(
                    "REPL smoke test failed, LEAN_PATH may be misconfigured. "
                    f"Errors: {error_details}"
                )
            if self._contexts_created > self.config.max_contexts_per_process:
                raise RuntimeError("REPL startup exceeded its context limit")
            if lean_project_fingerprint(self._project_identity) != startup_fingerprint:
                raise RuntimeError("Lean project changed during REPL startup")
            self._process_generation += 1
            self._base_environment = self._make_environment_handle(
                base_imports,
                smoke_env_id if base_env_id is None else base_env_id,
            )
            self._project_fingerprint = startup_fingerprint
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Close the Lean REPL process and invalidate its environments."""
        try:
            if self.process is not None and self.process.poll() is None:
                _kill_subprocesses(self.process)
        finally:
            self.process = None
            self._base_environment = None
            self._import_environments.clear()
            self._contexts_created = 0
            self._project_fingerprint = None
            self._stderr_bytes = 0
            self._stderr_tail.clear()

    def restart(self, timeout: float | None = None) -> None:
        """Restart the Lean REPL process within an optional total timeout."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        if self._request_deadline is not None:
            deadline = (
                self._request_deadline
                if deadline is None
                else min(deadline, self._request_deadline)
            )
        self.close()
        if deadline is None:
            self.start()
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"REPL restart timed out after {timeout:g} seconds")
        self.start(startup_timeout=remaining)

    def is_alive(self) -> bool:
        """Check if the REPL process is alive."""
        return self.process is not None and self.process.poll() is None

    def get_memory_usage(self) -> float:
        """Return memory usage in GB."""
        return _get_process_memory_gb(self.process)

    @property
    def _base_env_id(self) -> int | None:
        """Compatibility view of the private base environment handle."""
        if self._base_environment is None:
            return None
        return self._base_environment.env_id

    @_base_env_id.setter
    def _base_env_id(self, env_id: int | None) -> None:
        if env_id is None:
            self._base_environment = None
            return
        self._base_environment = self._make_environment_handle(
            tuple(sorted(self.config.warmup_imports)),
            env_id,
        )

    def run(
        self,
        code: str,
        env_id: int | None = None,
        timeout: float | None = None,
        imports: ResolvedImports | None = None,
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Send code to the REPL within one deadline across recovery attempts."""
        if deadline is not None and timeout is not None:
            raise TypeError("pass timeout or deadline, not both")
        absolute_deadline = deadline is not None
        timeout = self.request_timeout if timeout is None else timeout
        if deadline is None:
            deadline = time.monotonic() + timeout

        def deadline_error() -> TimeoutError:
            if absolute_deadline:
                return TimeoutError("REPL command deadline exceeded")
            return TimeoutError(f"REPL command timed out after {timeout:g} seconds")

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise deadline_error()
            return value

        run_from_env = env_id is not None
        if imports is not None and type(imports) is not ResolvedImports:
            raise TypeError("imports must be a ResolvedImports descriptor or None")
        descriptor = imports
        if descriptor is not None and descriptor.project_root != self._project_identity:
            return {
                "repl_error": "Resolved imports belong to a different Lean project root."
            }
        structured_imports = None if descriptor is None else descriptor.modules
        if run_from_env and descriptor is not None:
            return {
                "repl_error": (
                    "Structured imports cannot be combined with an explicit "
                    "environment identifier."
                )
            }
        max_retries = 0 if run_from_env or descriptor is not None else self.max_retries

        header_line_count = 0
        if not run_from_env:
            inline_imports, code, header_line_count = _split_imports_and_body(code)
            if descriptor is not None and inline_imports:
                return {
                    "repl_error": (
                        "Structured imports cannot be combined with import statements "
                        "at the start of code."
                    )
                }
            if descriptor is None:
                if self.config.validate_imports and self._allowed_import_roots is not None:
                    submitted_roots = {stmt.split(".")[0] for stmt in inline_imports}
                    disallowed = submitted_roots - self._allowed_import_roots
                    if disallowed:
                        return {
                            "repl_error": (
                                f"Disallowed imports: {', '.join(sorted(disallowed))}. "
                                f"Allowed roots: {', '.join(sorted(self._allowed_import_roots))}."
                            )
                        }
            else:
                header_line_count = 0

        last_exception: Exception | None = None
        with self._process_lock, self._deadline_scope(deadline):
            if descriptor is not None:
                self._assert_resolved_imports_current(
                    descriptor,
                    deadline,
                    require_worker=False,
                )

            if run_from_env and not self.is_alive():
                self.close()
                raise ReplProcessRestarted(
                    "REPL process restarted before the request; environment state was lost"
                )

            initial_generation = self._process_generation
            process_before_memory_check = self.process
            try:
                if not self.is_alive():
                    self.restart(timeout=remaining())
                if self._process_generation == initial_generation:
                    self._check_memory_and_maybe_restart(timeout=remaining())
                self._assert_project_current(deadline)
            except (TimeoutError, RuntimeError) as error:
                self.close()
                if run_from_env:
                    raise ReplProcessRestarted(str(error)) from error
                return {"repl_error": str(error)}

            if descriptor is not None:
                self._assert_resolved_imports_current(descriptor, deadline)

            if run_from_env and (
                self._process_generation != initial_generation
                or self.process is not process_before_memory_check
            ):
                raise ReplProcessRestarted(
                    "REPL process restarted before the request; environment state was lost"
                )

            request_restarted = (
                self._process_generation != initial_generation
                or self.process is not process_before_memory_check
            )

            for i in range(max_retries + 1):
                body_dispatched = False
                try:
                    if run_from_env:
                        cached = None
                    elif descriptor is not None:
                        cached = self._import_environments.get(structured_imports)
                    else:
                        cached = self._base_environment

                    # The same module tuple can name different compiled artifacts
                    # after another collaborator rebuilds the project. Lean may
                    # retain loaded modules for the life of the process, so a fresh
                    # descriptor cannot safely reuse, or merely replace, the old
                    # process-local environment. Recycle the whole worker first.
                    if (
                        descriptor is not None
                        and cached is not None
                        and cached.resolved_imports != descriptor
                    ):
                        if request_restarted:
                            return {
                                "repl_error": (
                                    "Lean import artifacts changed after a fresh "
                                    "worker start"
                                )
                            }
                        self.restart(timeout=remaining())
                        request_restarted = True
                        self._assert_project_current(deadline)
                        self._assert_resolved_imports_current(descriptor, deadline)
                        cached = None

                    reservation = 1 + int(descriptor is not None and cached is None)
                    if (
                        self._contexts_created + reservation
                        > self.config.max_contexts_per_process
                    ):
                        if run_from_env:
                            self.close()
                            raise ReplProcessRestarted(
                                "REPL process reached its context limit; "
                                "environment state was lost"
                            )
                        if request_restarted:
                            return {
                                "repl_error": (
                                    "REPL context limit is too small for this request "
                                    "after a fresh worker start"
                                )
                            }
                        self.restart(timeout=remaining())
                        request_restarted = True
                        self._assert_project_current(deadline)
                        if descriptor is not None:
                            self._assert_resolved_imports_current(descriptor, deadline)
                        cached = (
                            self._import_environments.get(structured_imports)
                            if descriptor is not None
                            else self._base_environment
                        )
                        reservation = 1 + int(
                            descriptor is not None and cached is None
                        )
                        if (
                            self._contexts_created + reservation
                            > self.config.max_contexts_per_process
                        ):
                            return {
                                "repl_error": (
                                    "REPL context limit is too small for this request "
                                    "after a fresh worker start"
                                )
                            }

                    if descriptor is not None and cached is None:
                        assert structured_imports is not None
                        self._assert_resolved_imports_current(descriptor, deadline)
                        header = "\n".join(
                            f"import {module}" for module in structured_imports
                        )
                        imported = self._run_counted(
                            code=header,
                            env_id=None,
                            timeout=remaining(),
                        )
                        request_env_id, messages = _validate_command_response(
                            imported,
                            context="the requested imports",
                        )
                        self._assert_resolved_imports_current(descriptor, deadline)
                        if any(message["severity"] == "error" for message in messages):
                            if (
                                self._contexts_created
                                > self.config.max_contexts_per_process
                            ):
                                self.close()
                                return _without_process_handles(imported)
                            return imported
                        if (
                            self._contexts_created + 1
                            > self.config.max_contexts_per_process
                        ):
                            self.close()
                            return {
                                "repl_error": (
                                    "REPL import setup exceeded its context limit "
                                    "before the requested code ran"
                                )
                            }
                        assert request_env_id is not None
                        cached = self._make_environment_handle(
                            structured_imports,
                            request_env_id,
                            resolved_imports=descriptor,
                        )
                        self._import_environments[structured_imports] = cached

                    if run_from_env:
                        request_env_id = env_id
                    elif cached is not None:
                        expected_context = (
                            structured_imports
                            if descriptor is not None
                            else tuple(sorted(self.config.warmup_imports))
                        )
                        assert expected_context is not None
                        request_env_id = self._environment_id(
                            cached,
                            expected_context,
                            expected_resolved_imports=(
                                descriptor if descriptor is not None else None
                            ),
                        )
                    else:
                        request_env_id = None
                    if descriptor is not None:
                        self._assert_resolved_imports_current(descriptor, deadline)
                    else:
                        self._assert_project_current(deadline)
                    body_dispatched = True
                    resp = self._run_counted(
                        code=code,
                        env_id=request_env_id,
                        timeout=remaining(),
                    )
                    _validate_command_response(
                        resp,
                        context="the requested command",
                    )
                    try:
                        if descriptor is not None:
                            self._assert_resolved_imports_current(descriptor, deadline)
                        else:
                            self._assert_project_current(deadline)
                    except (StaleResolvedImportsError, TimeoutError, RuntimeError) as error:
                        raise ReplOutcomeUnknown(
                            "Lean project freshness changed while the requested "
                            "command was executing; its outcome is unknown"
                        ) from error
                    over_context_limit = (
                        self._contexts_created
                        > self.config.max_contexts_per_process
                    )
                    _adjust_line_numbers(resp, header_line_count)
                    if over_context_limit:
                        self.close()
                        if run_from_env:
                            raise ReplProcessRestarted(
                                "REPL process exceeded its context limit after the "
                                "command; environment state was lost"
                            )
                        resp = _without_process_handles(resp)
                    return resp
                except ReplStderrBacklog as e:
                    # _run() already retired the process, so nothing can inherit the
                    # undrained stderr; close() here is an idempotent assertion of
                    # that. The response is valid, so a plain request still receives
                    # it. An env-scoped request cannot transparently outlive the
                    # process that held its environment, so it is told loudly.
                    logger.error("%s", e)
                    self.close()
                    if run_from_env:
                        raise ReplProcessRestarted(str(e)) from e
                    if not body_dispatched:
                        setup = (
                            "REPL import setup"
                            if descriptor is not None
                            else "REPL setup"
                        )
                        return {
                            "repl_error": (
                                f"{setup} failed before the requested code ran: "
                                f"{e}"
                            )
                        }
                    # The command's diagnostics remain valid, but any environment
                    # identifier belongs to the process _run() just retired.
                    try:
                        _validate_command_response(
                            e.response,
                            context="the requested command",
                        )
                    except ReplCommandError as error:
                        return {"repl_error": str(error)}
                    except ReplProtocolError as error:
                        return {
                            "repl_error": str(error),
                            "outcome_unknown": True,
                        }
                    response = _without_process_handles(e.response)
                    _adjust_line_numbers(response, header_line_count)
                    return response
                except ReplOutcomeUnknown as e:
                    # The request was fully written, so replay could execute it
                    # twice. Retire the process and report the unknown outcome
                    # without entering the ordinary retry path.
                    logger.error("%s", e)
                    self.close()
                    if run_from_env:
                        raise
                    if not body_dispatched:
                        setup = (
                            "REPL import setup"
                            if descriptor is not None
                            else "REPL setup"
                        )
                        return {
                            "repl_error": (
                                f"{setup} failed before the requested code ran: "
                                f"{e}"
                            )
                        }
                    return {"repl_error": str(e), "outcome_unknown": True}
                except ReplCommandError as e:
                    logger.error("Lean REPL rejected the command: %s", e)
                    return {"repl_error": str(e)}
                except ReplProtocolError as e:
                    logger.error("%s", e)
                    self.close()
                    response: dict[str, Any] = {"repl_error": str(e)}
                    if body_dispatched:
                        response["outcome_unknown"] = True
                    return response
                except ReplProcessExited as e:
                    last_exception = e
                    logger.error("REPL process exited: %s. Attempt %d/%d.", e, i + 1, max_retries + 1)
                    if run_from_env:
                        self.close()
                        raise ReplProcessRestarted(str(e)) from e
                except (TimeoutError, RuntimeError, json.JSONDecodeError) as e:
                    last_exception = e
                    logger.error("Error running command: %s. Attempt %d/%d.", e, i + 1, max_retries + 1)
                    if run_from_env:
                        self.close()
                        raise ReplProcessRestarted(str(e)) from e

                if i >= max_retries or request_restarted:
                    self.close()
                    break

                backoff = min(2**i, 30) + random.uniform(0, 1)
                try:
                    if backoff >= remaining():
                        raise deadline_error()
                    time.sleep(backoff)
                    self.restart(timeout=remaining())
                    request_restarted = True
                except (TimeoutError, RuntimeError) as error:
                    last_exception = error
                    self.close()
                    break
            logger.error("Exceeded maximum retries for Lean REPL command")
            return {"repl_error": str(last_exception)}

    def _assert_resolved_imports_current(
        self,
        descriptor: ResolvedImports,
        deadline: float,
        *,
        require_worker: bool = True,
    ) -> None:
        try:
            descriptor.assert_current(deadline)
            if (
                require_worker
                and self._project_fingerprint != descriptor.project_fingerprint
            ):
                raise StaleResolvedImportsError(
                    "resolved Lean imports are stale: worker project configuration differs"
                )
        except StaleResolvedImportsError:
            self.close()
            raise

    def _assert_project_current(self, deadline: float) -> None:
        """Reject a worker whose project changed after process startup."""
        if deadline - time.monotonic() <= 0:
            raise TimeoutError("REPL command deadline exceeded")
        try:
            current = lean_project_fingerprint(self._project_identity)
        except OSError as error:
            self.close()
            raise RuntimeError("Lean project changed after REPL startup") from error
        if self._project_fingerprint != current:
            self.close()
            raise RuntimeError("Lean project changed after REPL startup")

    @contextmanager
    def _deadline_scope(self, deadline: float):
        previous = self._request_deadline
        self._request_deadline = deadline
        try:
            yield
        finally:
            self._request_deadline = previous

    def _make_environment_handle(
        self,
        import_context: tuple[str, ...],
        env_id: int,
        *,
        resolved_imports: ResolvedImports | None = None,
    ) -> _EnvironmentHandle:
        """Bind a raw Lean environment ID to this worker process generation."""
        if not _is_natural_number(env_id):
            raise RuntimeError("Lean REPL returned an invalid environment ID")
        return _EnvironmentHandle(
            project_identity=self._project_identity,
            worker_token=self._worker_token,
            process_generation=self._process_generation,
            import_context=import_context,
            resolved_imports=resolved_imports,
            env_id=env_id,
        )

    def _environment_id(
        self,
        handle: _EnvironmentHandle,
        expected_import_context: tuple[str, ...],
        *,
        expected_resolved_imports: ResolvedImports | None = None,
    ) -> int:
        """Validate a private environment handle before using its raw ID."""
        if (
            handle.project_identity != self._project_identity
            or handle.worker_token is not self._worker_token
            or handle.process_generation != self._process_generation
            or handle.import_context != expected_import_context
            or handle.resolved_imports != expected_resolved_imports
            or not self.is_alive()
            or not _is_natural_number(handle.env_id)
        ):
            raise ReplProcessRestarted(
                "Lean environment belongs to another worker or process generation"
            )
        return handle.env_id

    def _run_counted(
        self,
        code: str,
        env_id: int | None,
        timeout: float,
    ) -> Any:
        """Run one command and account for every returned retained-state ID."""
        response = self._run(code=code, env_id=env_id, timeout=timeout)
        response_env_id = response.get("env") if isinstance(response, dict) else None
        if _is_natural_number(response_env_id):
            self._contexts_created += 1
        proof_states: set[int] = set()
        if isinstance(response, dict):
            top_level_proof_state = response.get("proofState")
            if _is_natural_number(top_level_proof_state):
                proof_states.add(top_level_proof_state)
            for response_field in ("sorries", "tactics"):
                values = response.get(response_field, [])
                if not isinstance(values, list):
                    continue
                for value in values:
                    proof_state = (
                        value.get("proofState") if isinstance(value, dict) else None
                    )
                    if _is_natural_number(proof_state):
                        proof_states.add(proof_state)
        self._contexts_created += len(proof_states)
        return response

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

        def mark_sent() -> None:
            nonlocal request_sent
            request_sent = True

        try:
            return self._run_io(code, env_id, timeout, mark_sent)
        except (ReplOutcomeUnknown, ReplStderrBacklog):
            raise
        except Exception as error:
            self.close()
            if request_sent:
                raise ReplOutcomeUnknown(
                    "Lean REPL transport failed after the request was fully sent; "
                    "its execution outcome is unknown and was not retried: "
                    f"{error}"
                ) from error
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
            or self.process.poll() is not None
            or self.process.stdin is None
            or self.process.stdout is None
            or self.process.stderr is None
        ):
            raise ReplProcessExited("REPL process is not running.")

        end_time = time.monotonic() + timeout
        if self._request_deadline is not None:
            end_time = min(end_time, self._request_deadline)
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
            stderr_tail = bytes(self._stderr_tail[-200:]).decode("utf-8", errors="replace")
            return stderr_bytes, stderr_tail

        def raise_unknown_stderr_outcome() -> None:
            stderr_bytes, stderr_tail = stderr_details()
            reason = stderr_poison_reason or "stderr could not be drained"
            self.close()
            raise ReplOutcomeUnknown(
                f"REPL process-generation stderr became unsafe after the request "
                f"was sent ({reason}; {stderr_bytes} bytes observed); "
                f"the execution outcome is unknown and was not retried. Tail: {stderr_tail!r}"
            )

        def retire_before_request() -> None:
            stderr_bytes, stderr_tail = stderr_details()
            reason = stderr_poison_reason or "stderr became unsafe"
            self.close()
            raise ReplProcessExited(
                f"REPL process-generation stderr became unsafe before the request "
                f"frame was fully sent ({reason}; {stderr_bytes} bytes observed); "
                f"the process was recycled. Tail: {stderr_tail!r}"
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
                tail_limit = max(0, max_buffer)
                if tail_limit:
                    if len(chunk) >= tail_limit:
                        self._stderr_tail[:] = chunk[-tail_limit:]
                    else:
                        overflow = len(self._stderr_tail) + len(chunk) - tail_limit
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
        payload = memoryview(command.encode("utf-8"))
        offset = 0
        while offset < len(payload):
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                if stderr_poison_reason is not None:
                    retire_before_request()
                raise TimeoutError(
                    f"REPL command timed out after {timeout} seconds while writing"
                )
            readable, writable, _ = select.select(
                [stderr_fd] if stderr_open else [],
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
            if stdin_fd not in writable:
                continue
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

        mark_sent()
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
                        drain_stderr()
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
                    response_bytes = bytes(response_buffer[:separator]).strip()
                    # The frame is complete, so this command's remaining queued
                    # stderr can be drained without starving stdout. Leaving it in
                    # the pipe would let a command exceed the stderr ceiling
                    # unnoticed, misattribute diagnostics to the next command, and
                    # eventually block the child on a full stderr pipe.
                    stderr_drained = drain_stderr(after_response=True)
                    break

        if not stderr_drained:
            stderr_bytes = self._stderr_bytes
            stderr_tail = bytes(self._stderr_tail[-200:]).decode("utf-8", errors="replace")
            stderr_reason = stderr_poison_reason or "stderr could not be drained"
            # stderr is accounted to the process generation, never to whichever
            # command happened to observe it. Once that generation exceeds its
            # quota or cannot be drained, retire it before another request.
            self.close()

        # Retire a desynchronized process before parsing. Malformed JSON must not
        # bypass the stream-safety invariant and leave stale stderr reusable.
        try:
            response = json.loads(response_bytes.decode("utf-8"))
        except json.JSONDecodeError as error:
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
