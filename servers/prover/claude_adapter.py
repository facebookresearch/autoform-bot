"""Claude-Max adapter — drives a headless ``claude -p`` worker as a prover backend.

This is the Claude Max backend: a full Claude Code session running headless
(``claude -p``), so the prover can edit the project and compile-to-iterate with
allowlisted ``lake``/``lean`` commands (plus MCP diagnostics when available),
just as the in-session ``autoform-worker`` does. It runs on the **Claude Max
subscription** — every ``claude`` invocation has ``ANTHROPIC_API_KEY`` scrubbed
from its environment, so it is billed to the subscription, never the API.

The four adapter methods:

* ``start``  — assemble the system prompt (the ``autoform-worker`` discipline +
  the node's spec) and launch the first ``claude -p`` turn with
  ``--output-format stream-json`` (streamed events) + ``--print``.
* ``events`` — parse the stream-json lines into normalized
  :class:`~servers.prover.base.Event`\\ s. Captures the ``session_id`` from the
  stream so a later steer can ``--resume`` the SAME session.
* ``steer``  — inject the correction as a **follow-up turn** on the captured
  session (``claude --resume <session_id> -p <correction>``). See the module
  note below for why this (rather than stdin streaming) is the mechanism.
* ``result`` — the final assistant text (the Lean proof, or an honest ``FAILED``)
  parsed into a :class:`~servers.prover.base.ProofResult`.

THE STEER MECHANISM (the one real design choice — documented for the summary):
``claude -p`` is a *batch* invocation: it reads one prompt, streams its work, and
exits. There is no live stdin channel to interrupt a turn mid-flight. So a steer
is delivered as the **next turn of the same conversation**: we capture the
``session_id`` emitted on the stream and, when the shared steerer asks to steer,
queue the correction; the driver's event loop, on reaching the end of the current
turn's stream, sees a queued steer and launches a follow-up turn with
``claude --resume <session_id> -p "<correction>"`` (full conversation context
preserved). This is the simplest mechanism that actually works with the public
CLI: turn-granular steering rather than token-granular interruption. It keeps the
adapter's surface identical to Aristotle's (whose ``project.ask`` is likewise a
new task on the live session), so the SHARED driver loop is unchanged. ``events``
transparently chains the resumed turn's stream after the current one, so to the
driver it is one continuous event iterator.

Shared CLI-agent internals (the honest-FAILED parse, the spec prompt, the env
scrub, the JSONL parse, the worker-discipline skeleton) live in ``_cli_common`` —
one definition across the Claude and Codex backends.
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

# Default model for the headless worker (overridable via ctor / env).
DEFAULT_MODEL = "opus"
DEFAULT_MAX_WAIT_SECONDS = 30 * 60.0

#: Safe non-interactive default. ``dontAsk`` hard-denies tools that are neither
#: built-in read-only operations nor explicitly allowed. Bash is scoped to Lean
#: checks, read-only search/git inspection, and target-directory creation.
#: Compound-command parsing applies every subcommand's rule independently.
DEFAULT_AUTONOMY_ARGS = [
    "--permission-mode",
    "dontAsk",
    "--allowedTools",
    (
        "Read,Grep,Glob,Edit,Write,"
        "Bash(lake build *),Bash(lake env lean *),Bash(lean *),"
        "Bash(rg *),Bash(git status *),Bash(git diff *),Bash(mkdir *)"
    ),
]
SESSION_ISOLATION_ARGS = [
    # Keep subscription/keychain authentication (unlike --bare) while excluding
    # repository-controlled settings, hooks, and skill expansion.
    "--setting-sources",
    "user",
    "--settings",
    '{"disableAllHooks":true}',
    "--disable-slash-commands",
]


def _default_autonomy_args() -> list[str]:
    """Return the fixed, least-privilege non-interactive policy.

    Environment variables must never widen a prover worker's filesystem or
    command permissions.
    """
    return list(DEFAULT_AUTONOMY_ARGS)


def _default_mcp_config() -> str | None:
    """Auto-discover the MCP config for the headless worker.

    The worker can use the stateful ``lean-lsp-mcp`` tools, so the child
    receives a ``--mcp-config``.
    Direct ``lake``/``lean`` verification remains authoritative. Resolution order:

    1. ``AUTOFORM_MCP_CONFIG`` env var (explicit override), else
    2. the plugin's own ``.mcp.json`` at the repo root relative to this package,
       if present, else
    3. ``None`` (no flag — the worker falls back to plain ``lake`` builds).
    """
    env = os.environ.get("AUTOFORM_MCP_CONFIG", "").strip()
    if env:
        return env
    candidate = Path(__file__).resolve().parents[2] / ".mcp.json"
    if candidate.exists():
        return str(candidate)
    return None

# The prover discipline the headless worker is held to — the SAME no-cheating /
# honest-FAILED contract the in-session ``autoform-worker`` agent carries
# (agents/autoform-worker.md), assembled from the shared skeleton in ``_cli_common``
# so it cannot drift from the Codex backend's copy.
WORKER_SYSTEM_PROMPT = build_worker_prompt(
    tools_clause=(
        "with direct `lake env lean` / `lake build` commands "
        "(and MCP diagnostics when available)"
    ),
    extra_hyp_clause=", no pinned-general parameter",
    billing_paragraph=(
        "Billing: the parent process has already removed `ANTHROPIC_API_KEY` and "
        "`ANTHROPIC_AUTH_TOKEN` from your environment. Do not inspect or manipulate "
        "authentication; invoke the allowlisted Lean commands directly.\n\n"
    ),
    repl_word="REPL ",
    build_phrase="build will not run",
    blocker_phrase="and the concrete blocker.",
)


def _classify_stream_event(obj: dict[str, Any]) -> Event | None:
    """Map one parsed stream-json object onto a normalized :class:`Event`.

    The ``claude -p --output-format stream-json`` stream emits objects with a
    ``type`` field (``system`` / ``assistant`` / ``user`` / ``result``). We pull
    out a short text payload and a normalized kind; objects with no useful
    payload return ``None`` (skipped).
    """
    etype = obj.get("type")

    if etype == "assistant":
        message = obj.get("message", {})
        for block in message.get("content", []) or []:
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                return Event(EventKind.MESSAGE, block["text"], raw=obj)
            if btype == "thinking" and block.get("thinking", "").strip():
                return Event(EventKind.THINKING, block["thinking"], raw=obj)
            if btype == "tool_use":
                name = block.get("name", "tool")
                tin = block.get("input", {})
                # Edits to .lean files are the load-bearing "edit" signal.
                target = str(tin.get("file_path") or tin.get("path") or "")
                if name in ("Edit", "Write", "MultiEdit"):
                    # Normalize the WRITTEN text into Event.payload so the
                    # structured triggers (sorry-count, forbidden-token) stay
                    # backend-agnostic. Write carries `content`, Edit
                    # `new_string`, MultiEdit a list of edits.
                    payload = str(tin.get("new_string") or tin.get("content") or "")
                    if not payload and isinstance(tin.get("edits"), list):
                        payload = "\n".join(
                            str(e.get("new_string", ""))
                            for e in tin["edits"] if isinstance(e, dict)
                        )
                    return Event(EventKind.EDIT, f"{name} {target}".strip(), raw=obj,
                                 path=target, payload=payload)
                return Event(EventKind.TOOL, f"{name} {target}".strip(), raw=obj, path=target)
        return None

    if etype == "user":
        # Tool results (build output, REPL diagnostics) come back as user turns.
        message = obj.get("message", {})
        for block in message.get("content", []) or []:
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                text = str(content)
                kind = EventKind.ERROR if block.get("is_error") else EventKind.TOOL
                return Event(kind, text, raw=obj)
        return None

    if etype == "result":
        return Event(EventKind.RESULT, str(obj.get("result", "")), raw=obj)

    return None


@dataclass
class _ClaudeRun:
    """Native run state for the Claude backend (held inside ``Run.handle``)."""

    node: str
    spec: str
    project_dir: str
    model: str
    session_id: str = ""
    pending_steer: str | None = None
    final_text: str = ""
    started: bool = False
    extra_args: list[str] = field(default_factory=list)
    deadline: float | None = None       # absolute time.monotonic() wall-clock cap
    timed_out: bool = False
    terminal_error: str = ""
    dropped_steers: int = 0             # steers skipped for lack of a session id
    # Token accounting, accumulated across EVERY turn (initial + steers + folds)
    # from each turn's terminal ``result`` stream object. Feeds the usage ledger
    # behind formalization.yaml — capture here or the numbers are lost.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0          # cache reads dominate agentic sessions —
    cache_creation_tokens: int = 0      # recorded so totals reconcile with cost
    cost_usd: float = 0.0               # claude's reported figure (notional on Max)
    turns: int = 0


class ClaudeAdapter(ProverAdapter):
    """Drive a headless ``claude -p`` worker as a swappable prover backend.

    Args:
        model: Model id passed to ``claude --model`` (default ``"opus"``).
        system_prompt: The worker discipline (defaults to
            :data:`WORKER_SYSTEM_PROMPT`).
        autonomy_args: Permission flags for the headless worker (defaults to
            :data:`DEFAULT_AUTONOMY_ARGS`, i.e. locked-down ``dontAsk`` plus an
            explicit tool allowlist). ``[]`` disables.
        mcp_config: Path passed to ``--mcp-config`` so the worker gets the
            stateful ``lean-lsp-mcp`` tools.
            ``None`` (default) auto-discovers via :func:`_default_mcp_config`
            (``AUTOFORM_MCP_CONFIG`` env, else the plugin's own ``.mcp.json``);
            ``""`` disables the flag entirely.
        extra_args: Extra ``claude`` CLI args the caller wants threaded through.
        max_wait_seconds: Wall-clock ceiling for the WHOLE run (all turns). On
            expiry the child process group is killed, a terminal error event is
            yielded, and the run reports ``failed`` with meta sub-status
            ``"timeout"``. ``None`` disables the cap.
        runner: Injectable launcher ``(args, env, cwd, deadline=None) ->
            Iterator[str]`` yielding stream-json lines. Defaults to a real
            ``subprocess`` launcher; tests inject a fake so no live ``claude``
            process is spawned.
    """

    name = "claude"
    #: Turn-granular: a correction lands only as the next ``--resume`` turn, so
    #: the driver skips the per-event judge by default and steers this backend
    #: via the verify-gate fold. See :class:`~servers.prover.base.SteeringCapability`.
    steering = SteeringCapability.BETWEEN_TURNS

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        system_prompt: str = WORKER_SYSTEM_PROMPT,
        autonomy_args: list[str] | None = None,
        mcp_config: str | None = None,
        extra_args: list[str] | None = None,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        runner: Any | None = None,
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt
        self._autonomy_args = list(
            autonomy_args if autonomy_args is not None else _default_autonomy_args()
        )
        self._mcp_config = _default_mcp_config() if mcp_config is None else (mcp_config or None)
        self._extra_args = list(extra_args or [])
        if not math.isfinite(max_wait_seconds) or max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be positive")
        self._max_wait_seconds = max_wait_seconds
        self._runner = runner or _subprocess_line_runner
        self._uses_builtin_runner = runner is None
        self._cancel_event: threading.Event | None = None

    # ------------------------------------------------------------------
    # Adapter surface
    # ------------------------------------------------------------------

    def bind_cancel_event(self, cancel_event: threading.Event | None) -> None:
        self._cancel_event = cancel_event

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        state = _ClaudeRun(
            node=node,
            spec=spec,
            project_dir=str(project_dir),
            model=self._model,
            extra_args=self._extra_args,
            deadline=time.monotonic() + self._max_wait_seconds,
        )
        return Run(backend=self.name, goal=spec, project_dir=str(project_dir), handle=state)

    def events(self, run: Run) -> Iterator[Event]:
        """Stream events from the first turn, then chain any steered follow-up turns.

        Each turn is one ``claude -p`` invocation. We capture ``session_id`` from
        the stream so a steer (queued by the driver via :meth:`steer`) can
        ``--resume`` the same conversation as the *next* turn — chained
        transparently so the driver sees one continuous iterator.
        """
        state: _ClaudeRun = run.handle

        try:
            # First turn: system prompt + spec. Guarded so the generator is
            # RE-ENTRANT: after the initial call exhausted the stream, the
            # driver's verify-gate fold queues a steer and calls events() again —
            # that re-entry must run ONLY the corrective resume turn below,
            # never replay the first turn.
            if not state.started:
                state.started = True
                first_prompt = _build_spec_prompt(state.node, state.spec)
                yield from self._run_turn(state, first_prompt, resume=False)

            # Drain any steers the driver queued during the turn (turn-granular
            # steering — see the module docstring on the mechanism).
            while state.pending_steer:
                correction = state.pending_steer
                state.pending_steer = None
                if not state.session_id:
                    # No session id captured → resuming is impossible. A bare
                    # `claude -p "<correction>"` would be a fresh CONTEXT-FREE
                    # session whose output would overwrite final_text and decide
                    # the verdict — skip the steer instead (mirrors the codex
                    # adapter's guard); annotated in the result meta.
                    state.dropped_steers += 1
                    logger.info("claude adapter: no session id; dropping steer (no resume context)")
                    break
                yield from self._run_turn(state, correction, resume=True)
        except ProverCancelled:
            state.terminal_error = "prover run cancelled"
            yield Event(EventKind.ERROR, state.terminal_error)
        except ProverProcessError as error:
            state.terminal_error = str(error)
            yield Event(EventKind.ERROR, state.terminal_error)
        except OSError as error:
            state.terminal_error = f"could not launch Claude worker: {error}"
            yield Event(EventKind.ERROR, state.terminal_error)
        except (TypeError, ValueError, AttributeError) as error:
            state.terminal_error = f"invalid Claude event stream: {error}"
            yield Event(EventKind.ERROR, state.terminal_error)
        except ProverTimeout:
            state.timed_out = True
            logger.warning("claude adapter: %s hit max_wait_seconds; worker killed", state.node)
            yield Event(EventKind.ERROR,
                        f"timeout: run exceeded max_wait_seconds ({self._max_wait_seconds}s); worker killed")

    def steer(self, run: Run, message: str) -> None:
        """Queue ``message`` as the next follow-up turn (delivered between turns).

        Best-effort and non-raising: the actual ``--resume`` launch happens in
        :meth:`events` when the current turn's stream ends.
        """
        state: _ClaudeRun = run.handle
        # Coalesce: keep the latest correction if several arrive before the turn ends.
        state.pending_steer = message
        logger.info("claude adapter: queued steer for next turn: %s", message[:120])

    def result(self, run: Run) -> ProofResult:
        state: _ClaudeRun = run.handle
        text = (state.final_text or "").strip()
        usage = {"input_tokens": state.input_tokens, "output_tokens": state.output_tokens,
                 "cache_read_tokens": state.cache_read_tokens,
                 "cache_creation_tokens": state.cache_creation_tokens,
                 "cost_usd": round(state.cost_usd, 6), "turns": state.turns}
        if state.terminal_error:
            sub_status = "cancelled" if state.terminal_error == "prover run cancelled" else "backend_error"
            return ProofResult(
                status="failed",
                proof_text=text,
                reason=state.terminal_error,
                backend=self.name,
                landed_files=0,
                meta={"session_id": state.session_id, "model": state.model,
                      "sub_status": sub_status, "usage": usage},
            )
        if state.timed_out:
            return ProofResult(
                status="failed",
                proof_text=text,
                reason=f"timeout: run exceeded max_wait_seconds ({self._max_wait_seconds}s); worker killed",
                backend=self.name,
                landed_files=0,
                meta={"session_id": state.session_id, "model": state.model,
                      "sub_status": "timeout", "usage": usage},
            )
        proved = not _looks_failed(text)
        meta = {"session_id": state.session_id, "model": state.model, "usage": usage}
        if state.dropped_steers:
            meta["dropped_steers"] = state.dropped_steers
        return ProofResult(
            status="proved" if proved else "failed",
            proof_text=text,
            reason="" if proved else _failure_reason(text),
            backend=self.name,
            landed_files=0,  # files are written in-place by the worker's own tools
            meta=meta,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_turn(self, state: _ClaudeRun, prompt: str, *, resume: bool) -> Iterator[Event]:
        args = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", state.model]
        if resume and state.session_id:
            args += ["--resume", state.session_id]
        elif not resume:
            args += ["--append-system-prompt", self._system_prompt]
        args += SESSION_ISOLATION_ARGS + self._autonomy_args
        if self._mcp_config:
            args += ["--strict-mcp-config", "--mcp-config", self._mcp_config]
        args += state.extra_args

        env = _scrubbed_env()
        plugin_root = str(Path(__file__).resolve().parents[2])
        # The shared headless MCP config uses Claude's documented variable.
        # Set it explicitly because a Claude worker may be launched by Codex or
        # a standalone dispatcher rather than from a Claude plugin session.
        env.setdefault("CLAUDE_PLUGIN_ROOT", plugin_root)
        env.setdefault("AUTOFORM_PLUGIN_ROOT", plugin_root)
        env["LEAN_PROJECT_DIR"] = state.project_dir
        env.setdefault("MCP_CONNECTION_NONBLOCKING", "true")
        for obj in _iter_json_lines(
            (
                self._runner(
                    args,
                    env,
                    state.project_dir,
                    state.deadline,
                    self._cancel_event,
                )
                if self._uses_builtin_runner
                else self._runner(args, env, state.project_dir, state.deadline)
            )
        ):
            # Capture the session id (emitted on the ``system: init`` line and the
            # ``result`` line) so a steer can resume this exact conversation.
            sid = obj.get("session_id")
            if sid:
                state.session_id = sid
            if obj.get("type") == "result":
                # The terminal object of each turn carries the turn's token usage
                # and claude's cost figure — accumulate them per run.
                # VERIFY-LIVE: this SUMS across resumed turns on the reasoning
                # that each `claude -p` invocation reports its own turn. If any
                # CLI version reports session-cumulative usage/cost on
                # --resume, this overstates; confirm with two live turns.
                usage = obj.get("usage") or {}
                state.input_tokens += int(usage.get("input_tokens") or 0)
                state.output_tokens += int(usage.get("output_tokens") or 0)
                state.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
                state.cache_creation_tokens += int(
                    usage.get("cache_creation_input_tokens") or 0)
                try:
                    state.cost_usd += float(obj.get("total_cost_usd") or 0.0)
                except (TypeError, ValueError):
                    pass
                state.turns += 1
            event = _classify_stream_event(obj)
            if event is None:
                continue
            if event.kind is EventKind.RESULT and event.content:
                state.final_text = event.content
            yield event
