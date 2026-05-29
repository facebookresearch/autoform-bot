# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Aristotle (Harmonic) inference backend.

Implements :class:`InferenceProtocol` on top of Harmonic's ``aristotlelib``
SDK. Aristotle is *not* a turn-based chat model: it is an autonomous formal
reasoning agent that takes a prompt (and optionally a whole Lean project),
runs its own internal tools (proof search, Lean builds, file edits), and
returns finished files plus a natural-language ``output_summary``.

We map that job-based API onto the chat-shaped protocol as follows:

* A single, persistent ``Project`` backs the whole conversation. It is
  created lazily on the first :meth:`complete` call (optionally bundling a
  seed Lean project from ``project_dir``).
* Each :meth:`complete` call submits the latest user turn — the first turn
  via ``Project.create`` / ``create_from_directory``, every later turn via
  ``project.ask`` — then polls the resulting ``AgentTask`` to a terminal
  status. This *is* multi-turn steering: follow-up ``ask`` calls reuse
  Aristotle's live server-side session (see the ``CONTINUABLE`` statuses).
* The task's ``output_summary`` becomes the assistant text; the changed Lean
  files can optionally be downloaded to ``download_dir``.

Honest limitations (documented for callers):

* **No per-turn tool calling.** Aristotle runs its own tools internally, so
  ``tools`` passed by the agent loop are ignored and ``TurnResult.tool_calls``
  is always empty. Drive Aristotle with plain instructions, not tool schemas.
* **No token usage / prompt caching.** Aristotle bills by compute, not
  tokens, so ``TokenUsage`` is reported as zeros and ``CacheConfig`` is a
  no-op.

In-flight steering IS supported. While a task is running you can issue
``project.ask(...)`` to redirect the live session (this is what Marathon's
"Hermes" watcher does). Pass a ``steer`` callback to observe the event stream
as it arrives and optionally return a steering prompt, which is injected via
``project.ask`` while the task is still in flight. An ``on_event`` callback is
also available for pure observation (progress, logging). Between-turn steering
via follow-up ``complete()`` calls works too (each reuses the live session).

The status-classification vocabulary (``IN_FLIGHT`` / ``CONTINUABLE`` /
terminal) mirrors the design used in Marathon (https://github.com/Deicyde/marathon),
a standalone Aristotle driver, distilled here to the minimum this adapter
needs and depending only on the public ``aristotlelib``.
"""

from __future__ import annotations

import logging
import tarfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..protocol import (
    InferenceConfig,
    InferenceProtocol,
    TokenUsage,
    ToolResult,
    ToolSchema,
    TurnResult,
)

logger = logging.getLogger(__name__)

# Callback contracts for live observation / steering. ``Any`` stands for
# aristotlelib's ``Event`` / ``AgentTask`` (kept untyped so the module imports
# without the optional ``aristotlelib`` dependency installed).
#
# - EventObserver: called once per new event as it arrives during a run.
# - SteerCallback: called with each batch of new events plus the running task;
#   return a non-empty prompt to redirect the live task via ``project.ask``,
#   or ``None``/empty to let it continue. This is the upstreamable
#   generalization of Marathon's Hermes watcher.
EventObserver = Callable[[Any], Awaitable[None]]
SteerCallback = Callable[[list[Any], Any], Awaitable[str | None]]


# ---------------------------------------------------------------------------
# Status classification (compared by ``.value`` string, enum-agnostic)
# ---------------------------------------------------------------------------

# Task is still running on Aristotle's side; keep polling.
_IN_FLIGHT_STATUSES: frozenset[str] = frozenset({"QUEUED", "IN_PROGRESS"})

# Terminal statuses where Aristotle's server-side session is preserved, so the
# next turn should *continue* it via ``project.ask`` rather than resubmit.
# The web UI labels these "Review Suggested" / "Out of Budget"; the SDK
# documents both as resumable ("Resume by telling Aristotle to continue").
_CONTINUABLE_STATUSES: frozenset[str] = frozenset({"COMPLETE_WITH_ERRORS", "OUT_OF_BUDGET"})


def _status_value(status: Any) -> str:
    """Return the ``.value`` of a ``TaskStatus`` (or the string itself)."""
    return str(getattr(status, "value", status))


def _map_finish_reason(status_value: str) -> str:
    """Map a terminal ``TaskStatus`` value to a normalized finish reason."""
    match status_value:
        case "COMPLETE" | "COMPLETE_WITH_ERRORS":
            return "stop"
        case "OUT_OF_BUDGET":
            return "length"
        case "FAILED":
            return "error"
        case "CANCELED":
            return "cancelled"
        case _:
            return status_value.lower() or "stop"


class AristotleInference(InferenceProtocol):
    """:class:`InferenceProtocol` backed by Harmonic's Aristotle agent.

    Args:
        model_name: Identifier recorded on results (Aristotle takes no model
            parameter; this is purely for tracing/pricing lookup).
        project_dir: Optional Lean project bundled as context on the first
            submission (via ``Project.create_from_directory``). When ``None``,
            the first turn is a bare prompt (``Project.create``).
        download_dir: Optional directory; when set, each completed turn's
            result tarball is downloaded and extracted here, and the
            extraction path is appended to the assistant text.
        poll_interval: Seconds between status polls while a task runs.
        max_wait_seconds: Optional ceiling on how long to wait for a single
            task; ``None`` waits indefinitely.
        on_event: Optional async callback invoked once per new event as the
            task runs (observation only — progress bars, logging).
        steer: Optional async callback for in-flight steering. Called with
            ``(new_events, task)`` on each poll that surfaces new events; if it
            returns a non-empty prompt and the task is still in flight, the
            prompt is injected via ``project.ask`` to redirect the live
            session, and polling switches to the resulting task.
        lib: The ``aristotlelib`` module (injected for testing). When ``None``
            it is imported lazily on first use.
    """

    def __init__(
        self,
        *,
        model_name: str,
        project_dir: Path | str | None = None,
        download_dir: Path | str | None = None,
        poll_interval: int = 10,
        max_wait_seconds: float | None = None,
        on_event: EventObserver | None = None,
        steer: SteerCallback | None = None,
        lib: Any | None = None,
    ) -> None:
        super().__init__()
        self._model_name = model_name
        self._project_dir = Path(project_dir) if project_dir is not None else None
        self._download_dir = Path(download_dir) if download_dir is not None else None
        self._poll_interval = poll_interval
        self._max_wait_seconds = max_wait_seconds
        self._on_event = on_event
        self._steer = steer

        self._lib = lib

        # Conversation state. ``_messages`` is a simple role/content log used
        # only for tracing; the authoritative state lives in the Aristotle
        # ``Project`` session held in ``_project``.
        self._system_prompt: str = ""
        self._messages: list[dict[str, Any]] = []
        self._project: Any | None = None
        self._last_status: str = ""
        self._steer_count: int = 0

    # ------------------------------------------------------------------
    # Lazy SDK access
    # ------------------------------------------------------------------

    def _aristotlelib(self) -> Any:
        if self._lib is None:
            import aristotlelib  # lazy: only required when this backend is used

            self._lib = aristotlelib
        return self._lib

    # ------------------------------------------------------------------
    # Protocol: conversation management
    # ------------------------------------------------------------------

    @property
    def model_name(self) -> str:
        return self._model_name

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt

    def get_system_prompt(self) -> str:
        return self._system_prompt

    def add_user_message(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # Aristotle has no per-turn tool-call protocol, so tool results should
        # not normally arrive. Fold any that do into a plain user message so no
        # information is silently dropped.
        for r in results:
            prefix = "[tool error] " if r.is_error else "[tool result] "
            self._messages.append({"role": "user", "content": prefix + r.content})

    def reset(self) -> None:
        # Drop the conversation *and* the Aristotle session so the next turn
        # starts a fresh project (the system prompt is preserved by contract).
        self._messages.clear()
        self._project = None
        self._last_status = ""

    def get_messages(self) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        if self._system_prompt:
            msgs.append({"role": "system", "content": self._system_prompt})
        msgs.extend(self._messages)
        return msgs

    def replace_history(self, summary: str) -> None:
        self._messages.clear()
        self._messages.append({"role": "user", "content": f"[Context summary of previous conversation]\n\n{summary}"})

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        self._messages = [m for m in messages if m.get("role") != "system"]

    def cleanup_interrupted(self) -> None:
        # Strip a trailing assistant turn that never produced text (e.g. an
        # interrupted poll), and any trailing tool-result messages.
        while self._messages:
            last = self._messages[-1]
            role = last.get("role", "")
            content = last.get("content", "")
            if role == "assistant" and (not content or (isinstance(content, str) and not content.strip())):
                self._messages.pop()
                continue
            if role == "user" and isinstance(content, str) and content.startswith(("[tool result] ", "[tool error] ")):
                self._messages.pop()
                continue
            break

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _latest_user_content(self) -> str:
        for msg in reversed(self._messages):
            if msg.get("role") == "user":
                return str(msg.get("content", ""))
        return ""

    def _initial_prompt(self, user_content: str) -> str:
        """Prompt for the first submission: system context + first user turn."""
        if self._system_prompt:
            return f"{self._system_prompt}\n\n{user_content}".strip()
        return user_content

    async def _submit(self, user_content: str) -> Any:
        """Submit ``user_content`` and return the resulting ``AgentTask``.

        First turn creates the project (bundling ``project_dir`` when given);
        later turns continue the live session via ``project.ask``.
        """
        lib = self._aristotlelib()
        if self._project is None:
            prompt = self._initial_prompt(user_content)
            if self._project_dir is not None:
                self._project = await lib.Project.create_from_directory(
                    prompt=prompt, project_dir=self._project_dir
                )
            else:
                self._project = await lib.Project.create(prompt=prompt)
            tasks, _ = await self._project.get_tasks(limit=1, newest_first=True)
            if not tasks:
                raise RuntimeError(
                    f"project {getattr(self._project, 'project_id', '?')} has no AgentTask after submission"
                )
            return tasks[0]

        if self._last_status and self._last_status not in _CONTINUABLE_STATUSES:
            logger.debug(
                "Continuing Aristotle project after non-continuable status %r; "
                "ask() starts a new task on the same project.",
                self._last_status,
            )
        return await self._project.ask(user_content)

    async def _fetch_new_events(self, task: Any, seen: set[str]) -> list[Any]:
        """Return events on ``task`` not already in ``seen``, oldest-first.

        Updates ``seen`` in place. Best-effort: a transient fetch error
        returns no new events rather than aborting the run.
        """
        try:
            events, _ = await task.get_events(limit=50, newest_first=True)
        except Exception as err:  # pragma: no cover - transient API hiccup
            logger.debug("event fetch failed (continuing): %s", err)
            return []
        fresh = [e for e in events if getattr(e, "event_id", None) not in seen]
        for e in fresh:
            seen.add(getattr(e, "event_id", None))
        fresh.reverse()
        return fresh

    async def _poll_to_terminal(self, task: Any) -> Any:
        """Poll ``task`` until it leaves the in-flight statuses.

        When ``on_event``/``steer`` are configured, the event stream is polled
        alongside status. A ``steer`` callback that returns a prompt while the
        task is still in flight injects it via ``project.ask`` (in-flight
        steering) and polling switches to the resulting task.
        """
        import asyncio

        watching = self._on_event is not None or self._steer is not None
        seen: set[str] = set()
        start = time.monotonic()
        while _status_value(task.status) in _IN_FLIGHT_STATUSES:
            if self._max_wait_seconds is not None and (time.monotonic() - start) > self._max_wait_seconds:
                logger.warning(
                    "Aristotle task %s exceeded max_wait_seconds=%s; returning last-known state.",
                    getattr(task, "agent_task_id", "?"),
                    self._max_wait_seconds,
                )
                break
            await asyncio.sleep(self._poll_interval)
            await task.refresh()

            if not watching:
                continue

            new_events = await self._fetch_new_events(task, seen)
            for event in new_events:
                if self._on_event is not None:
                    await self._on_event(event)

            if self._steer is not None and new_events:
                prompt = await self._steer(new_events, task)
                if prompt:
                    # Only redirect a task that is still running; ``ask`` on an
                    # idle project would start an unrelated task instead.
                    await task.refresh()
                    if _status_value(task.status) in _IN_FLIGHT_STATUSES and self._project is not None:
                        steered = await self._project.ask(prompt)
                        self._steer_count += 1
                        logger.info(
                            "Steered Aristotle in-flight (#%d): %s",
                            self._steer_count,
                            prompt[:120],
                        )
                        # Follow the live session onto the new task.
                        task = steered
        return task

    async def _maybe_download(self) -> str:
        """Download + extract the result tarball when ``download_dir`` is set.

        Returns a short note to append to the assistant text, or ``""``.
        """
        if self._download_dir is None or self._project is None:
            return ""
        try:
            self._download_dir.mkdir(parents=True, exist_ok=True)
            project_id = getattr(self._project, "project_id", "aristotle")
            tar_path = self._download_dir / f"{project_id}.tar.gz"
            await self._project.get_files(destination=tar_path)
            with tarfile.open(tar_path) as tar:
                tar.extractall(self._download_dir)  # noqa: S202 - trusted Aristotle output
            return f"\n\n[Aristotle files extracted to {self._download_dir}]"
        except Exception as err:  # pragma: no cover - best-effort side channel
            logger.warning("Failed to download Aristotle result files: %s", err)
            return f"\n\n[Aristotle file download failed: {err}]"

    # ------------------------------------------------------------------
    # Protocol: complete
    # ------------------------------------------------------------------

    async def complete(
        self,
        *,
        tools: list[ToolSchema] | None = None,
        inference_config: InferenceConfig | None = None,
    ) -> TurnResult:
        if tools:
            logger.debug(
                "AristotleInference ignores %d tool schema(s): Aristotle runs its own tools internally.",
                len(tools),
            )

        user_content = self._latest_user_content()
        task = await self._submit(user_content)
        task = await self._poll_to_terminal(task)

        status_value = _status_value(task.status)
        self._last_status = status_value

        text = (getattr(task, "output_summary", None) or "").strip()
        text += await self._maybe_download()
        if not text:
            text = f"[Aristotle task {status_value.lower()} with no summary]"

        self._messages.append({"role": "assistant", "content": text})

        return TurnResult(
            text=text,
            thinking="",
            tool_calls=[],
            usage=TokenUsage(),
            model=self._model_name,
            call_id=str(getattr(task, "agent_task_id", "")),
            finish_reason=_map_finish_reason(status_value),
        )
