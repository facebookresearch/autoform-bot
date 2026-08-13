"""Muse/TBH CLI adapter for the unified Autoform prover.

Muse exposes a headless ``tbh exec --json`` surface with schema-versioned JSONL
events and policy-gated workspace tools. Unlike Claude and Codex, the stable CLI
does not expose a headless resume command, so one Muse invocation is one complete
proving attempt and this adapter declares :class:`SteeringCapability.NONE`.
The worker can still inspect, edit, and compile repeatedly inside that attempt;
Autoform's shared verification gate remains authoritative afterward.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._cli_common import (
    ProverCancelled,
    ProverProcessError,
    ProverTimeout,
    _build_spec_prompt,
    _failure_reason,
    _iter_json_lines,
    _looks_failed,
    _scrubbed_env,
    _subprocess_line_runner,
    build_worker_prompt,
)
from .base import Event, EventKind, ProofResult, ProverAdapter, Run, SteeringCapability

logger = logging.getLogger(__name__)


MUSE_SYSTEM_PROMPT = build_worker_prompt(
    tools_clause="with Muse's workspace tools and managed shell",
    build_phrase="the build will not run",
    blocker_phrase="naming the concrete blocker.",
)


def muse_runtime_env(runtime_dir: str | None = None) -> dict[str, str]:
    """Return a child environment whose Muse data cannot load user plugins.

    Muse stores its plugin registry below ``XDG_DATA_HOME``. A headless worker
    launched by the Autoform plugin must not load Autoform again, start a second
    copy of every MCP server, or inherit unrelated user plugins. Configuration
    and provider authentication remain inherited; only mutable runtime data is
    redirected to an Autoform-owned location.
    """
    env = _scrubbed_env()
    root = Path(
        runtime_dir
        or os.environ.get("AUTOFORM_MUSE_RUNTIME_DIR", "").strip()
        or Path.home() / ".local" / "share" / "autoform" / "muse-worker"
    ).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    env["XDG_DATA_HOME"] = str(root.resolve())
    return env


def _usage_from(obj: dict[str, Any]) -> dict[str, Any]:
    for candidate in (obj.get("usage"), (obj.get("payload") or {}).get("usage")):
        if isinstance(candidate, dict):
            return candidate
    return {}


def classify_muse_event(
    obj: dict[str, Any],
) -> tuple[Event | None, str | None, str | None, str | None, dict[str, Any]]:
    """Map one Muse record to event, final text, terminal error, session id, usage.

    Only ``run.terminal.*`` records are terminal. Plugin reminders and optional
    tool tasks may emit ``task.lifecycle.failed`` during an otherwise successful
    run, as the stable CLI's own echo provider demonstrates.
    """
    payload_type = str(obj.get("payload_type") or "")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    stream = obj.get("stream") if isinstance(obj.get("stream"), dict) else {}
    session_id = str(stream.get("id") or "") if stream.get("kind") == "session" else ""
    text = str(payload.get("text") or "")
    reason = str(payload.get("reason") or "")
    usage = _usage_from(obj)

    if payload_type == "run.output.delta" and text:
        return Event(EventKind.MESSAGE, text, raw=obj), None, None, session_id, usage
    if payload_type == "run.terminal.completed":
        return Event(EventKind.RESULT, text, raw=obj), text, None, session_id, usage
    if payload_type.startswith("run.terminal."):
        failure = reason or text or payload_type.rsplit(".", 1)[-1]
        return Event(EventKind.ERROR, failure, raw=obj), None, failure, session_id, usage

    if payload_type == "task.lifecycle.proposed":
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        task_kind = str(event.get("task_kind") or "")
        if task_kind:
            return Event(EventKind.TOOL, task_kind, raw=obj), None, None, session_id, usage
    return None, None, None, session_id, usage


def parse_muse_terminal_output(stdout: str) -> tuple[str, str, dict[str, int]]:
    """Extract final text, terminal error, and token usage from Muse JSONL."""
    final_text = ""
    terminal_error = ""
    deltas: list[str] = []
    totals = {"input_tokens": 0, "output_tokens": 0}
    for obj in _iter_json_lines(iter(stdout.splitlines())):
        event, final, error, _session_id, usage = classify_muse_event(obj)
        if event is not None and event.kind is EventKind.MESSAGE and event.content:
            deltas.append(event.content)
        if final is not None:
            final_text = final
        if error:
            terminal_error = error
        totals["input_tokens"] += int(
            usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        )
        totals["output_tokens"] += int(
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        )
    return final_text or "".join(deltas), terminal_error, totals


@dataclass
class _MuseRun:
    node: str
    spec: str
    project_dir: str
    model: str | None
    provider: str
    preset: str | None
    reasoning_effort: str | None
    max_model_steps: int | None
    runtime_dir: str | None
    extra_args: list[str] = field(default_factory=list)
    deadline: float | None = None
    started: bool = False
    final_text: str = ""
    terminal_error: str = ""
    session_id: str = ""
    timed_out: bool = False
    dropped_steers: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


DEFAULT_MAX_WAIT_SECONDS = 30 * 60.0


class MuseAdapter(ProverAdapter):
    """Drive one sandboxed headless Muse run as an Autoform prover."""

    name = "muse"
    steering = SteeringCapability.NONE

    def __init__(
        self,
        *,
        model: str | None = None,
        provider: str | None = None,
        preset: str | None = None,
        reasoning_effort: str | None = None,
        max_model_steps: int | None = None,
        system_prompt: str = MUSE_SYSTEM_PROMPT,
        muse_bin: str | None = None,
        runtime_dir: str | None = None,
        extra_args: list[str] | None = None,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        runner: Any | None = None,
    ) -> None:
        self._model = model or os.environ.get("AUTOFORM_MUSE_MODEL") or None
        self._provider = provider or os.environ.get("AUTOFORM_MUSE_PROVIDER") or "meta"
        self._preset = preset or os.environ.get("AUTOFORM_MUSE_PRESET") or None
        self._reasoning_effort = (
            reasoning_effort or os.environ.get("AUTOFORM_MUSE_REASONING_EFFORT") or None
        )
        configured_steps = os.environ.get("AUTOFORM_MUSE_MAX_MODEL_STEPS", "").strip()
        self._max_model_steps = max_model_steps
        if self._max_model_steps is None and configured_steps:
            self._max_model_steps = int(configured_steps)
        self._system_prompt = system_prompt
        self._muse_bin = muse_bin or os.environ.get("AUTOFORM_MUSE_BIN") or "tbh"
        self._runtime_dir = runtime_dir
        self._extra_args = list(extra_args or [])
        if not math.isfinite(max_wait_seconds) or max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be positive")
        self._max_wait_seconds = max_wait_seconds
        self._runner = runner or _subprocess_line_runner
        self._uses_builtin_runner = runner is None
        self._cancel_event: threading.Event | None = None

    def bind_cancel_event(self, cancel_event: threading.Event | None) -> None:
        self._cancel_event = cancel_event

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        state = _MuseRun(
            node=node,
            spec=spec,
            project_dir=str(project_dir),
            model=self._model,
            provider=self._provider,
            preset=self._preset,
            reasoning_effort=self._reasoning_effort,
            max_model_steps=self._max_model_steps,
            runtime_dir=self._runtime_dir,
            extra_args=self._extra_args,
            deadline=time.monotonic() + self._max_wait_seconds,
        )
        return Run(backend=self.name, goal=spec, project_dir=str(project_dir), handle=state)

    def events(self, run: Run) -> Iterator[Event]:
        state: _MuseRun = run.handle
        if state.started:
            return
        state.started = True
        prompt = f"{self._system_prompt}\n\n{_build_spec_prompt(state.node, state.spec)}"
        args = [
            self._muse_bin,
            "exec",
            "--json",
            "--provider",
            state.provider,
            "--workspace",
            state.project_dir,
            "--disable-approval",
            "--user-input-auto-resolve",
            "--disable-web-tools",
            "--no-foreign-personal-context",
            "--no-session-log",
            "--sandbox-network",
            "restricted",
        ]
        if state.preset:
            args += ["--preset", state.preset]
        if state.model:
            args += ["--model", state.model]
        if state.reasoning_effort:
            args += ["--reasoning-effort", state.reasoning_effort]
        if state.max_model_steps is not None:
            args += ["--max-model-steps", str(state.max_model_steps)]
        args += state.extra_args + [prompt]

        deltas: list[str] = []
        try:
            lines = (
                self._runner(
                    args,
                    muse_runtime_env(state.runtime_dir),
                    state.project_dir,
                    state.deadline,
                    self._cancel_event,
                )
                if self._uses_builtin_runner
                else self._runner(
                    args,
                    muse_runtime_env(state.runtime_dir),
                    state.project_dir,
                    state.deadline,
                )
            )
            for obj in _iter_json_lines(lines):
                event, final, error, session_id, usage = classify_muse_event(obj)
                if session_id:
                    state.session_id = session_id
                if event is not None and event.kind is EventKind.MESSAGE and event.content:
                    deltas.append(event.content)
                if final is not None:
                    state.final_text = final
                if error:
                    state.terminal_error = error
                state.input_tokens += int(
                    usage.get("input_tokens") or usage.get("prompt_tokens") or 0
                )
                state.output_tokens += int(
                    usage.get("output_tokens") or usage.get("completion_tokens") or 0
                )
                if event is not None:
                    yield event
        except ProverCancelled:
            state.terminal_error = "prover run cancelled"
            yield Event(EventKind.ERROR, state.terminal_error)
        except ProverProcessError as error:
            state.terminal_error = str(error)
            yield Event(EventKind.ERROR, state.terminal_error)
        except OSError as error:
            state.terminal_error = f"could not launch Muse worker: {error}"
            yield Event(EventKind.ERROR, state.terminal_error)
        except (TypeError, ValueError, AttributeError) as error:
            state.terminal_error = f"invalid Muse event stream: {error}"
            yield Event(EventKind.ERROR, state.terminal_error)
        except ProverTimeout:
            state.timed_out = True
            state.terminal_error = (
                f"timeout: run exceeded max_wait_seconds ({self._max_wait_seconds}s); "
                "worker killed"
            )
            yield Event(EventKind.ERROR, state.terminal_error)
        if not state.final_text and deltas:
            state.final_text = "".join(deltas)

    def steer(self, run: Run, message: str) -> None:
        state: _MuseRun = run.handle
        state.dropped_steers += 1
        logger.info("muse adapter: dropping steer; stable tbh has no headless resume")

    def result(self, run: Run) -> ProofResult:
        state: _MuseRun = run.handle
        text = (state.final_text or "").strip()
        usage = {
            "input_tokens": state.input_tokens,
            "output_tokens": state.output_tokens,
            "turns": 1 if state.started else 0,
        }
        meta: dict[str, Any] = {
            "session_id": state.session_id,
            "model": state.model or "muse-default",
            "provider": state.provider,
            "usage": usage,
        }
        if state.dropped_steers:
            meta["dropped_steers"] = state.dropped_steers
        if state.terminal_error:
            if state.timed_out:
                meta["sub_status"] = "timeout"
            elif state.terminal_error == "prover run cancelled":
                meta["sub_status"] = "cancelled"
            else:
                meta["sub_status"] = "backend_error"
            return ProofResult(
                status="failed",
                proof_text=text,
                reason=state.terminal_error,
                backend=self.name,
                landed_files=0,
                meta=meta,
            )
        proved = not _looks_failed(text)
        return ProofResult(
            status="proved" if proved else "failed",
            proof_text=text,
            reason="" if proved else _failure_reason(text),
            backend=self.name,
            landed_files=0,
            meta=meta,
        )
