"""Shared internals for the CLI-agent prover backends (Claude, Codex).

Both ``claude -p`` and ``codex exec`` are headless coding-agent CLIs driven the same
way: launch with the worker discipline + the node spec, stream JSONL events, steer by
resuming the session, and judge the run by its final ``FAILED — <reason>`` line. The
genuinely-identical pieces live here — one definition — so "what counts as an honest
FAILED", the spec prompt, the env scrub, the JSONL parse, and the shared
worker-discipline text never drift between backends. The parts that genuinely differ
(each CLI's args, event schema, and final-text rule) stay in the adapters.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)


class ProverTimeout(Exception):
    """The CLI worker exceeded its wall-clock deadline (the child was killed)."""


class ProverCancelled(Exception):
    """The caller cancelled the CLI worker and its process group was killed."""


class ProverProcessError(Exception):
    """The CLI worker exited unsuccessfully or could not be launched."""


def _scrubbed_env() -> dict[str, str]:
    """A copy of the environment with ``ANTHROPIC_API_KEY`` /
    ``ANTHROPIC_AUTH_TOKEN`` removed.

    For the Claude backend this routes billing to the Max subscription (never the
    API); for Codex (its own auth) it is project hygiene. Same operation either way.
    """
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _build_spec_prompt(node: str, spec: str) -> str:
    """The first-turn user prompt: the node target + its spec."""
    return (
        f"# Formalization target: {node}\n\n"
        f"{spec}\n\n"
        "Prove this node now. Write the proof into the project and report the result "
        "(or an honest `FAILED — <reason>` if you cannot)."
    )


def build_worker_prompt(
    *,
    tools_clause: str,
    build_phrase: str,
    blocker_phrase: str,
    extra_hyp_clause: str = "",
    billing_paragraph: str = "",
    repl_word: str = "",
) -> str:
    """Assemble the worker-discipline system prompt from the shared skeleton + the
    backend-specific bits, so the Claude and Codex prompts can't drift while each
    keeps its exact text. A backend supplies only how it compiles (``tools_clause``),
    its extra faithfulness clause, an optional billing paragraph, and small wording
    deltas (``repl_word`` / ``build_phrase`` / ``blocker_phrase``).
    """
    return (
        "You are a Lean 4 / Mathlib formalization worker — a prover backend. Given a target "
        "node and its spec, search Mathlib, write a GENUINE Lean 4 proof, and compile-to-iterate "
        f"{tools_clause} until it compiles cleanly with no gaps.\n\n"
        "Hard rule — no cheating: `sorry`, `admit`, raw `axiom`, and `native_decide` are NEVER an "
        "acceptable finished proof; do not hide a gap behind an `opaque`/`macro`/structure field or "
        "a vacuous `False.elim`. The statement must be proved faithfully — no weakening, no smuggled "
        f"hypotheses{extra_hyp_clause}. Grep the whole project for `sorry`/`admit`/`axiom` "
        "before calling anything done.\n\n"
        f"{billing_paragraph}"
        "Output: on success, write the proof into the node's file and report the final Lean content "
        f"plus a one-line {repl_word}compilation status. If you could NOT discharge the target (does not "
        f"compile, a `sorry` remains, {build_phrase}, or you ran out of road), do NOT deliver a "
        f"success-shaped result — end with a line `FAILED — <one-line reason>` {blocker_phrase} "
        "Reporting FAILED honestly is correct; delivering a sorry'd file as done is the one thing you "
        "must never do."
    )


def _iter_json_lines(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Parse a stream of JSONL lines into objects, skipping blanks and unparseable
    lines — the boilerplate both CLI event loops share before each backend classifies
    the object its own way."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """terminate() then kill() the child **and its process group** (the child is
    started in its own group so grandchildren — ``lake`` builds, git — die too)."""
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        # Every managed worker is launched with start_new_session=True, so its
        # pid is also its process-group id even after the leader exits.
        pgid = proc.pid

    def _signal_group(sig: int) -> None:
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except Exception:
                pass
        try:
            proc.send_signal(sig)
        except Exception:
            pass

    _signal_group(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except Exception:
        _signal_group(signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        except Exception:  # pragma: no cover - unkillable child
            logger.warning("could not reap CLI worker pid %s", proc.pid)


def _subprocess_line_runner(
    args: list[str],
    env: dict[str, str],
    cwd: str,
    deadline: float | None = None,
    cancel_event: threading.Event | None = None,
) -> Iterator[str]:
    """Real launcher: run a CLI and yield its stdout lines (JSONL).

    Lives behind the injectable ``runner`` seam so the adapters are unit-testable
    without spawning a live ``claude``/``codex`` process.

    ``deadline`` is an absolute ``time.monotonic()`` instant: when it passes, the
    child (and its whole process group — it is started with
    ``start_new_session=True``) is terminated then killed and
    :class:`ProverTimeout` is raised. The same kill path runs when the generator
    is closed early (``GeneratorExit``), so an abandoned run never leaks a
    fully-autonomous child process. Lines are pumped through a queue by a reader
    thread so the deadline and cancellation are enforced even while the child is
    silent.
    """
    proc = subprocess.Popen(
        args,
        cwd=cwd or None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        start_new_session=True,  # own process group → the kill path reaps grandchildren
    )
    assert proc.stdout is not None
    lines: queue.Queue[Any] = queue.Queue()
    _EOF = object()

    def _pump() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.put(line)
        except Exception:  # pragma: no cover - pipe torn down mid-read
            pass
        finally:
            lines.put(_EOF)

    threading.Thread(target=_pump, daemon=True).start()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ProverCancelled(f"CLI worker was cancelled: {args[0]}")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise ProverTimeout(f"CLI worker exceeded its deadline: {args[0]}")
            try:
                poll_interval = 0.1 if cancel_event is not None else 1.0
                item = lines.get(
                    timeout=poll_interval if remaining is None else min(remaining, poll_interval)
                )
            except queue.Empty:
                continue  # re-check the deadline, keep waiting for output
            if item is _EOF:
                break
            yield item
        returncode = proc.wait(timeout=5)
        if returncode != 0:
            raise ProverProcessError(
                f"CLI worker exited with status {returncode}: {args[0]}"
            )
    finally:
        # Runs on normal exhaustion, on ProverTimeout, AND on generator close
        # (GeneratorExit) — the child never outlives its consumer.
        _kill_process_tree(proc)
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            logger.warning("could not finish reaping CLI worker pid %s", proc.pid)


# A status-like FAILED line: the contract's `FAILED — <reason>` at line start
# (allowing markdown emphasis/heading lead-ins) or a `status: FAILED` field.
# UPPERCASE only for the bare form — the token is a status marker, not prose.
_FAILED_LINE_RE = re.compile(r"^[\s>*_#`-]*FAILED\b")
_STATUS_FAILED_RE = re.compile(r"^[\s>*_`-]*status\s*[:=]\s*FAILED\b", re.IGNORECASE)


def _looks_failed(text: str) -> bool:
    """Heuristic: did the worker report an honest FAILED rather than a proof?

    The worker contract ends a failure with a ``FAILED — <reason>`` line; an empty
    result is also treated as a failure (no proof produced). The match is
    deliberately STRICT — ``FAILED`` counts only as a status-like token (at line
    start, or a ``status: FAILED`` field), never anywhere in prose ("the previous
    attempt FAILED, so I …" is not a failure report). A false *proved* here is
    caught by the verify gate downstream, but a false *failed* has NO backstop —
    it silently discards a genuine proof — which is why this must not loosen.
    """
    if not text.strip():
        return True
    return any(
        _FAILED_LINE_RE.match(line) or _STATUS_FAILED_RE.match(line)
        for line in text.splitlines()
    )


def _failure_reason(text: str) -> str:
    """Extract the one-line reason from a ``FAILED — <reason>`` report."""
    if not text.strip():
        return "worker produced no output"
    for line in text.splitlines():
        m = _FAILED_LINE_RE.match(line) or _STATUS_FAILED_RE.match(line)
        if m:
            # Strip the "FAILED —/-/:" lead-in (and any markdown emphasis).
            rest = line[m.end():].lstrip(" *_`—-:").strip()
            return rest or "worker reported FAILED"
    return "worker reported FAILED"
