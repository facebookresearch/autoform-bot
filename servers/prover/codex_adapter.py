"""Codex adapter — drives a headless OpenAI ``codex exec`` worker as a prover backend.

A third swappable backend alongside Claude-on-Max and Aristotle. It mirrors the
Claude adapter: launch a headless coding-agent CLI on the node's spec, normalize
its event stream onto the shared :class:`~servers.prover.base.Event` vocabulary,
steer turn-granularly by resuming the session, and parse the final report into a
:class:`~servers.prover.base.ProofResult` — held to the SAME no-cheating /
honest-``FAILED`` discipline. Only the CLI and its output schema differ, so the
shared driver + steerer are unchanged, and the honest-FAILED parse / spec prompt /
env scrub / JSONL parse / discipline skeleton are shared via ``_cli_common``.

**Billing / auth.** Codex runs on its OWN auth — the ``codex`` CLI's logged-in
account (a ChatGPT subscription, or an OpenAI API key), **not** the Claude Max
subscription. This backend therefore does not depend on ``ANTHROPIC_API_KEY`` (it
drops it as hygiene) and simply inherits the environment ``codex login`` set up.

**Interface assumptions** (``codex exec`` JSON mode). This targets
``codex exec --json`` emitting JSONL events and ``codex exec resume <id>`` for a
follow-up (steer) turn. Event-classification and the session-id capture are
deliberately DEFENSIVE — several codex schema shapes are tolerated (top-level
``type`` or nested ``item.type``) — and the proved/failed verdict rests on the
worker's final ``FAILED — <reason>`` line, **not** on any single schema field. So a
codex build whose JSON differs still yields a correct verdict from the final text;
steering merely degrades to a no-op if no session id is seen. Override the binary,
model, or flags via the ctor / ``AUTOFORM_CODEX_BIN`` if your codex differs.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
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

#: The codex binary (overridable so a pinned path / wrapper can be used).
DEFAULT_CODEX_BIN = os.environ.get("AUTOFORM_CODEX_BIN", "codex")
DEFAULT_MAX_WAIT_SECONDS = 30 * 60.0
#: Safe non-interactive default: edits and Lean commands are allowed only inside
#: the selected workspace. This policy is fixed; environment variables cannot
#: widen it or disable the sandbox.
DEFAULT_AUTONOMY_ARGS = ["--sandbox", "workspace-write"]


def _default_autonomy_args() -> list[str]:
    """Return the fixed workspace-write sandbox policy."""
    return list(DEFAULT_AUTONOMY_ARGS)


def _resume_autonomy_args(args: list[str]) -> list[str]:
    """Drop first-turn-only options from ``codex exec resume`` arguments.

    Current Codex resumes inherit the session sandbox and do not accept the
    first-turn ``--sandbox <mode>`` option.
    """
    result: list[str] = []
    index = 0
    while index < len(args):
        if args[index] == "--sandbox":
            index += 2
            continue
        result.append(args[index])
        index += 1
    return result


# The SAME no-cheating / honest-FAILED contract the Claude backend states, framed
# for codex (no separate system-prompt flag, so it is inlined into the first turn) —
# assembled from the shared skeleton in ``_cli_common`` so the two cannot drift.
CODEX_SYSTEM_PROMPT = build_worker_prompt(
    tools_clause="(run `lake env lean` / the project's REPL)",
    build_phrase="the build will not run",
    blocker_phrase="naming the concrete blocker.",
)


# codex ``exec --json`` item types → normalized EventKind (defensive sets; matching
# is also substring-based below so schema drift still classifies sensibly).
_MSG_ITEMS = {"agent_message", "assistant_message", "message"}
_THINK_ITEMS = {"reasoning", "agent_reasoning", "thinking"}
_EDIT_ITEMS = {"file_change", "patch", "apply_patch", "file_update"}
_TOOL_ITEMS = {"command_execution", "function_call", "mcp_tool_call", "local_shell_call", "exec_command"}


def _item_text(item: dict[str, Any]) -> str:
    """Best-effort text payload from a codex item across schema variants."""
    for k in ("text", "message", "content", "delta", "output", "aggregated_output", "command"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):
            parts = [c.get("text", "") for c in v if isinstance(c, dict)]
            if any(parts):
                return " ".join(p for p in parts if p)
    return ""


def _classify_codex_event(obj: dict[str, Any]) -> tuple[Event | None, str | None, str | None]:
    """Map one codex JSON line → ``(Event|None, agent_text|None, session_id|None)``.

    The 2nd element is the final-answer text to remember (only for agent messages);
    the 3rd is a session/thread id to capture for resume-steering. Tolerant of both
    a top-level ``type`` and a nested ``item.type``."""
    sid = (obj.get("session_id") or obj.get("thread_id")
           or obj.get("conversation_id") or obj.get("id_session"))
    item = obj.get("item") if isinstance(obj.get("item"), dict) else obj
    itype = str(item.get("type") or obj.get("type") or "").lower().split(".")[-1]
    text = _item_text(item)

    if "error" in itype or obj.get("is_error"):
        return Event(EventKind.ERROR, text, raw=obj), None, sid
    if itype in _MSG_ITEMS or itype.endswith("message"):
        return Event(EventKind.MESSAGE, text, raw=obj), (text or None), sid
    if itype in _THINK_ITEMS or "reason" in itype or "think" in itype:
        return Event(EventKind.THINKING, text, raw=obj), None, sid
    if itype in _EDIT_ITEMS or "patch" in itype or "file_change" in itype:
        # Path best-effort across codex schema variants; the patch/file text
        # itself doubles as the written payload for the structured triggers.
        path = str(item.get("path") or item.get("file") or item.get("file_path") or "")
        return Event(EventKind.EDIT, text, raw=obj, path=path, payload=text), None, sid
    if itype in _TOOL_ITEMS or "command" in itype or "tool" in itype or "exec" in itype:
        return Event(EventKind.TOOL, text, raw=obj), None, sid
    if itype in ("completed", "result") and text:
        return Event(EventKind.RESULT, text, raw=obj), None, sid
    return None, None, sid


@dataclass
class _CodexRun:
    """Native run state for the Codex backend (held inside ``Run.handle``)."""

    node: str
    spec: str
    project_dir: str
    model: str | None
    session_id: str = ""
    pending_steer: str | None = None
    final_text: str = ""
    started: bool = False
    extra_args: list[str] = field(default_factory=list)
    deadline: float | None = None       # absolute time.monotonic() wall-clock cap
    timed_out: bool = False
    terminal_error: str = ""
    dropped_steers: int = 0
    # Token accounting across every turn (codex ``turn.completed`` events carry
    # a usage dict; read defensively wherever one appears).
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    turns: int = 0


class CodexAdapter(ProverAdapter):
    """Drive a headless ``codex exec`` worker as a swappable prover backend.

    Args mirror :class:`~servers.prover.claude_adapter.ClaudeAdapter`. ``runner`` is
    injectable ``(args, env, cwd, deadline) -> Iterator[str]`` (tests pass a fake
    so no live ``codex`` runs). ``autonomy_args`` defaults to a workspace-write
    sandbox, and environment variables cannot disable it.
    """

    name = "codex"
    #: Turn-granular, exactly like the Claude CLI: corrections land as the next
    #: ``codex exec resume`` turn; the driver steers this backend via the fold.
    steering = SteeringCapability.BETWEEN_TURNS

    def __init__(
        self,
        *,
        model: str | None = None,
        system_prompt: str = CODEX_SYSTEM_PROMPT,
        codex_bin: str = DEFAULT_CODEX_BIN,
        autonomy_args: list[str] | None = None,
        extra_args: list[str] | None = None,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        runner: Any | None = None,
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt
        self._codex_bin = codex_bin
        self._autonomy_args = list(
            autonomy_args if autonomy_args is not None else _default_autonomy_args()
        )
        self._extra_args = list(extra_args or [])
        if not math.isfinite(max_wait_seconds) or max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be positive")
        self._max_wait_seconds = max_wait_seconds
        self._runner = runner or _subprocess_line_runner
        self._uses_builtin_runner = runner is None
        self._cancel_event: threading.Event | None = None

    # ------------------------------------------------------------------ surface

    def bind_cancel_event(self, cancel_event: threading.Event | None) -> None:
        self._cancel_event = cancel_event

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        state = _CodexRun(node=node, spec=spec, project_dir=str(project_dir),
                          model=self._model, extra_args=self._extra_args,
                          deadline=time.monotonic() + self._max_wait_seconds)
        return Run(backend=self.name, goal=spec, project_dir=str(project_dir), handle=state)

    def events(self, run: Run) -> Iterator[Event]:
        """First turn (discipline + spec), then any steered resume turns."""
        state: _CodexRun = run.handle
        try:
            # codex exec has no separate system-prompt flag, so the worker discipline is
            # prepended to the first user prompt. Guarded for RE-ENTRANCY: the
            # driver's verify-gate fold re-enters events() after the stream
            # exhausted, and that re-entry must run ONLY the corrective resume
            # turn, never replay the first turn.
            if not state.started:
                state.started = True
                first = f"{self._system_prompt}\n\n{_build_spec_prompt(state.node, state.spec)}"
                yield from self._run_turn(state, first, resume=False)

            while state.pending_steer:
                correction = state.pending_steer
                state.pending_steer = None
                if not state.session_id:
                    # No session captured → cannot resume with context; drop the steer
                    # rather than run a context-less turn (best-effort, never raises).
                    state.dropped_steers += 1
                    logger.info("codex adapter: no session id; dropping steer (no resume context)")
                    break
                yield from self._run_turn(state, correction, resume=True)
        except ProverCancelled:
            state.terminal_error = "prover run cancelled"
            yield Event(EventKind.ERROR, state.terminal_error)
        except ProverProcessError as error:
            state.terminal_error = str(error)
            yield Event(EventKind.ERROR, state.terminal_error)
        except OSError as error:
            state.terminal_error = f"could not launch Codex worker: {error}"
            yield Event(EventKind.ERROR, state.terminal_error)
        except (TypeError, ValueError, AttributeError) as error:
            state.terminal_error = f"invalid Codex event stream: {error}"
            yield Event(EventKind.ERROR, state.terminal_error)
        except ProverTimeout:
            state.timed_out = True
            logger.warning("codex adapter: %s hit max_wait_seconds; worker killed", state.node)
            yield Event(EventKind.ERROR,
                        f"timeout: run exceeded max_wait_seconds ({self._max_wait_seconds}s); worker killed")

    def steer(self, run: Run, message: str) -> None:
        """Queue ``message`` as the next resume turn (delivered between turns)."""
        state: _CodexRun = run.handle
        state.pending_steer = message
        logger.info("codex adapter: queued steer for next turn: %s", message[:120])

    def result(self, run: Run) -> ProofResult:
        state: _CodexRun = run.handle
        text = (state.final_text or "").strip()
        usage = {"input_tokens": state.input_tokens, "output_tokens": state.output_tokens,
                 "cached_tokens": state.cached_tokens, "turns": state.turns}
        if state.terminal_error:
            sub_status = "cancelled" if state.terminal_error == "prover run cancelled" else "backend_error"
            return ProofResult(
                status="failed",
                proof_text=text,
                reason=state.terminal_error,
                backend=self.name,
                landed_files=0,
                meta={"session_id": state.session_id, "model": state.model or "codex-default",
                      "sub_status": sub_status, "usage": usage},
            )
        if state.timed_out:
            return ProofResult(
                status="failed",
                proof_text=text,
                reason=f"timeout: run exceeded max_wait_seconds ({self._max_wait_seconds}s); worker killed",
                backend=self.name,
                landed_files=0,
                meta={"session_id": state.session_id, "model": state.model or "codex-default",
                      "sub_status": "timeout", "usage": usage},
            )
        proved = not _looks_failed(text)
        meta: dict[str, Any] = {"session_id": state.session_id,
                                "model": state.model or "codex-default",
                                "usage": usage}
        if state.dropped_steers:
            meta["dropped_steers"] = state.dropped_steers
        return ProofResult(
            status="proved" if proved else "failed",
            proof_text=text,
            reason="" if proved else _failure_reason(text),
            backend=self.name,
            landed_files=0,  # files are written in-place by codex's own tools
            meta=meta,
        )

    # ---------------------------------------------------------------- internals

    def _run_turn(self, state: _CodexRun, prompt: str, *, resume: bool) -> Iterator[Event]:
        args = [self._codex_bin, "exec"]
        if resume and state.session_id:
            args += ["resume", state.session_id]
        args += ["--json", "--skip-git-repo-check"]
        if state.model:
            args += ["-m", state.model]
        autonomy = (
            _resume_autonomy_args(self._autonomy_args)
            if resume
            else self._autonomy_args
        )
        args += autonomy + state.extra_args + [prompt]

        lines = (
            self._runner(
                args,
                _scrubbed_env(),
                state.project_dir,
                state.deadline,
                self._cancel_event,
            )
            if self._uses_builtin_runner
            else self._runner(args, _scrubbed_env(), state.project_dir, state.deadline)
        )
        for obj in _iter_json_lines(lines):
            usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
            if usage is None and isinstance(obj.get("item"), dict):
                iu = obj["item"].get("usage")
                usage = iu if isinstance(iu, dict) else None
            if usage:
                # VERIFY-LIVE: assumes per-event usage deltas; if a codex build
                # emits cumulative snapshots (or duplicates usage on nested and
                # top-level events for the same tokens), this overcounts —
                # check one live `codex exec --json` transcript.
                state.input_tokens += int(usage.get("input_tokens") or 0)
                state.output_tokens += int(usage.get("output_tokens") or 0)
                state.cached_tokens += int(usage.get("cached_input_tokens") or 0)
                state.turns += 1
            event, final, sid = _classify_codex_event(obj)
            if sid:
                state.session_id = sid
            if final:
                state.final_text = final
            if event is not None:
                if event.kind is EventKind.RESULT and event.content and not state.final_text:
                    state.final_text = event.content
                yield event
