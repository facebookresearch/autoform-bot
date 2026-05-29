# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for AristotleInference — job submission, polling, and steering.

These use an injected fake ``aristotlelib`` (the ``lib`` constructor arg) so
no network access or API key is required.
"""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.inference import InferenceConfig, ToolResult, ToolSchema
from ..sdk.aristotle import (
    AristotleInference,
    _CONTINUABLE_STATUSES,
    _IN_FLIGHT_STATUSES,
    _map_finish_reason,
    _status_value,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEvent:
    def __init__(self, event_id: str, event_type_name: str) -> None:
        self.event_id = event_id
        self.event_type = types.SimpleNamespace(name=event_type_name)


class FakeTask:
    """A task that walks through ``statuses`` on successive ``refresh`` calls.

    Optionally surfaces ``events`` (newest-first) so the event-watcher /
    steering path can be exercised.
    """

    def __init__(
        self,
        statuses: list[str],
        output_summary: str = "Proved the theorem.",
        events: list[FakeEvent] | None = None,
        agent_task_id: str = "task-1",
    ) -> None:
        self._statuses = statuses
        self._i = 0
        self.status = statuses[0]
        self.output_summary = output_summary
        self.agent_task_id = agent_task_id
        self._events = events or []

    async def refresh(self) -> None:
        self._i = min(self._i + 1, len(self._statuses) - 1)
        self.status = self._statuses[self._i]

    async def get_events(self, limit: int = 50, newest_first: bool = True) -> tuple[list[FakeEvent], None]:
        evs = list(reversed(self._events)) if newest_first else list(self._events)
        return evs[:limit], None


class FakeProject:
    def __init__(self, task: FakeTask, ask_returns: FakeTask | None = None) -> None:
        self._task = task
        self._ask_returns = ask_returns
        self.project_id = "proj-1"
        self.ask_prompts: list[str] = []

    async def get_tasks(self, limit: int = 1, newest_first: bool = True) -> tuple[list[FakeTask], None]:
        return [self._task], None

    async def ask(self, prompt: str) -> FakeTask:
        self.ask_prompts.append(prompt)
        return self._ask_returns if self._ask_returns is not None else self._task


def _make_lib(project: FakeProject) -> Any:
    """Build a fake ``aristotlelib`` module exposing the surface we call."""
    create = AsyncMock(return_value=project)
    create_from_directory = AsyncMock(return_value=project)
    lib = types.SimpleNamespace(
        Project=types.SimpleNamespace(create=create, create_from_directory=create_from_directory),
        set_api_key=lambda key: key,
    )
    return lib


def _make_inference(project: FakeProject, **kwargs: Any) -> AristotleInference:
    return AristotleInference(model_name="aristotle", poll_interval=0, lib=_make_lib(project), **kwargs)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------


class TestStatusHelpers:
    def test_status_value_from_enum_like(self):
        assert _status_value(types.SimpleNamespace(value="QUEUED")) == "QUEUED"

    def test_status_value_from_string(self):
        assert _status_value("COMPLETE") == "COMPLETE"

    def test_in_flight_membership(self):
        assert "IN_PROGRESS" in _IN_FLIGHT_STATUSES
        assert "COMPLETE" not in _IN_FLIGHT_STATUSES

    def test_continuable_membership(self):
        assert "OUT_OF_BUDGET" in _CONTINUABLE_STATUSES
        assert "COMPLETE_WITH_ERRORS" in _CONTINUABLE_STATUSES
        assert "FAILED" not in _CONTINUABLE_STATUSES

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("COMPLETE", "stop"),
            ("COMPLETE_WITH_ERRORS", "stop"),
            ("OUT_OF_BUDGET", "length"),
            ("FAILED", "error"),
            ("CANCELED", "cancelled"),
        ],
    )
    def test_map_finish_reason(self, status: str, expected: str):
        assert _map_finish_reason(status) == expected


# ---------------------------------------------------------------------------
# Conversation management
# ---------------------------------------------------------------------------


class TestConversationManagement:
    def test_system_prompt_roundtrip(self):
        inf = _make_inference(FakeProject(FakeTask(["COMPLETE"])))
        inf.set_system_prompt("You are a Lean expert.")
        assert inf.get_system_prompt() == "You are a Lean expert."

    def test_get_messages_includes_system(self):
        inf = _make_inference(FakeProject(FakeTask(["COMPLETE"])))
        inf.set_system_prompt("sys")
        inf.add_user_message("hi")
        msgs = inf.get_messages()
        assert msgs[0] == {"role": "system", "content": "sys"}
        assert msgs[1] == {"role": "user", "content": "hi"}

    def test_tool_results_folded_into_user_message(self):
        inf = _make_inference(FakeProject(FakeTask(["COMPLETE"])))
        inf.add_tool_results([ToolResult(tool_call_id="x", content="ok"), ToolResult(tool_call_id="y", content="boom", is_error=True)])
        contents = [m["content"] for m in inf.get_messages()]
        assert "[tool result] ok" in contents
        assert "[tool error] boom" in contents

    def test_cleanup_interrupted_strips_empty_assistant(self):
        inf = _make_inference(FakeProject(FakeTask(["COMPLETE"])))
        inf.add_user_message("hi")
        inf._messages.append({"role": "assistant", "content": "  "})
        inf.cleanup_interrupted()
        assert inf._messages[-1] == {"role": "user", "content": "hi"}

    @pytest.mark.asyncio
    async def test_reset_drops_project(self):
        project = FakeProject(FakeTask(["COMPLETE"]))
        inf = _make_inference(project)
        inf.add_user_message("prove x")
        await inf.complete()
        assert inf._project is not None
        inf.reset()
        assert inf._project is None
        assert inf._messages == []


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


class TestComplete:
    @pytest.mark.asyncio
    async def test_first_turn_creates_project_and_returns_summary(self):
        project = FakeProject(FakeTask(["QUEUED", "IN_PROGRESS", "COMPLETE"], output_summary="QED"))
        inf = _make_inference(project)
        inf.add_user_message("prove 1+1=2")
        result = await inf.complete()

        assert result.text == "QED"
        assert result.finish_reason == "stop"
        assert result.tool_calls == []
        assert result.usage.total_tokens == 0
        assert result.call_id == "task-1"
        inf._lib.Project.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_first_turn_with_project_dir_bundles_directory(self, tmp_path):
        project = FakeProject(FakeTask(["COMPLETE"]))
        inf = _make_inference(project, project_dir=tmp_path)
        inf.add_user_message("fill the sorries")
        await inf.complete()
        inf._lib.Project.create_from_directory.assert_awaited_once()
        inf._lib.Project.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_second_turn_continues_via_ask(self):
        project = FakeProject(FakeTask(["COMPLETE"]))
        inf = _make_inference(project)
        inf.add_user_message("prove lemma A")
        await inf.complete()
        inf.add_user_message("now prove lemma B")
        await inf.complete()
        assert project.ask_prompts == ["now prove lemma B"]

    @pytest.mark.asyncio
    async def test_system_prompt_prepended_on_first_submission(self):
        project = FakeProject(FakeTask(["COMPLETE"]))
        inf = _make_inference(project)
        inf.set_system_prompt("Use Mathlib conventions.")
        inf.add_user_message("prove it")
        await inf.complete()
        _, kwargs = inf._lib.Project.create.call_args
        assert "Use Mathlib conventions." in kwargs["prompt"]
        assert "prove it" in kwargs["prompt"]

    @pytest.mark.asyncio
    async def test_tools_are_ignored(self):
        project = FakeProject(FakeTask(["COMPLETE"]))
        inf = _make_inference(project)
        inf.add_user_message("prove it")
        result = await inf.complete(
            tools=[ToolSchema(name="read", description="d", parameters={})],
            inference_config=InferenceConfig(),
        )
        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_out_of_budget_maps_to_length(self):
        project = FakeProject(FakeTask(["OUT_OF_BUDGET"], output_summary="partial"))
        inf = _make_inference(project)
        inf.add_user_message("prove it")
        result = await inf.complete()
        assert result.finish_reason == "length"
        assert inf._last_status == "OUT_OF_BUDGET"

    @pytest.mark.asyncio
    async def test_empty_summary_falls_back_to_placeholder(self):
        project = FakeProject(FakeTask(["COMPLETE"], output_summary=""))
        inf = _make_inference(project)
        inf.add_user_message("prove it")
        result = await inf.complete()
        assert "no summary" in result.text


# ---------------------------------------------------------------------------
# In-flight observation + steering
# ---------------------------------------------------------------------------


class TestSteering:
    @pytest.mark.asyncio
    async def test_on_event_observer_receives_events(self):
        events = [FakeEvent("e1", "THINKING"), FakeEvent("e2", "EDITING_FILE")]
        project = FakeProject(FakeTask(["IN_PROGRESS", "COMPLETE"], events=events))
        seen: list[str] = []

        async def observer(ev):
            seen.append(ev.event_type.name)

        inf = _make_inference(project, on_event=observer)
        inf.add_user_message("prove it")
        await inf.complete()
        assert seen == ["THINKING", "EDITING_FILE"]
        assert project.ask_prompts == []  # observation only, no steering

    @pytest.mark.asyncio
    async def test_steer_injects_ask_while_in_flight(self):
        events = [FakeEvent("e1", "EDITING_FILE")]
        running = FakeTask(["IN_PROGRESS", "IN_PROGRESS", "IN_PROGRESS"], events=events)
        steered = FakeTask(["COMPLETE"], output_summary="fixed", agent_task_id="task-2")
        project = FakeProject(running, ask_returns=steered)

        async def steer(new_events, task):
            if any(e.event_type.name == "EDITING_FILE" for e in new_events):
                return "you are off-course; use `norm_num`"
            return None

        inf = _make_inference(project, steer=steer)
        inf.add_user_message("prove it")
        result = await inf.complete()

        assert project.ask_prompts == ["you are off-course; use `norm_num`"]
        assert inf._steer_count == 1
        # Polling followed the live session onto the steered task.
        assert result.text == "fixed"
        assert result.call_id == "task-2"

    @pytest.mark.asyncio
    async def test_steer_returning_none_does_not_ask(self):
        events = [FakeEvent("e1", "THINKING")]
        project = FakeProject(FakeTask(["IN_PROGRESS", "COMPLETE"], events=events))

        async def steer(new_events, task):
            return None

        inf = _make_inference(project, steer=steer)
        inf.add_user_message("prove it")
        await inf.complete()
        assert project.ask_prompts == []
        assert inf._steer_count == 0

    @pytest.mark.asyncio
    async def test_no_duplicate_events_across_polls(self):
        events = [FakeEvent("e1", "THINKING")]
        # Stays in flight several polls; the same event must be delivered once.
        project = FakeProject(FakeTask(["IN_PROGRESS", "IN_PROGRESS", "IN_PROGRESS", "COMPLETE"], events=events))
        count = {"n": 0}

        async def observer(ev):
            count["n"] += 1

        inf = _make_inference(project, on_event=observer)
        inf.add_user_message("prove it")
        await inf.complete()
        assert count["n"] == 1
