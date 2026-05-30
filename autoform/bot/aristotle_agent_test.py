# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for AristotleAgent — the Mode-B Aristotle worker.

Uses a fake ``AristotleInference`` (no network/key) that simulates the backend
extracting a result project into ``download_dir``, plus a real temporary git
repo + worktree so the land/commit/merge path is exercised for real.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from core.task import Task
from core.coordination.concurrent_agents import ConcurrentAgents
from core import worktree
from .aristotle_agent import AristotleAgent, create_aristotle_agents, create_aristotle_pool


# ---------------------------------------------------------------------------
# Fake backend: simulates submit → returns a project with one new .lean file
# ---------------------------------------------------------------------------


def _make_fake_inference(generated: dict[str, str] | None, *, download_ok: bool = True):
    """Return a fake AristotleInference class.

    ``download_result`` writes ``generated`` (relpath → contents) into a
    ``proj_aristotle`` root and returns it (or ``None`` when ``download_ok``
    is False, simulating an exhausted-retry download failure).
    """

    class FakeInference:
        last_status = "COMPLETE"

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._u = ""

        def set_system_prompt(self, p):
            self._sys = p

        def add_user_message(self, c):
            self._u = c

        def get_messages(self):
            return [{"role": "user", "content": self._u}, {"role": "assistant", "content": "done"}]

        async def complete(self):
            return types.SimpleNamespace(text="Formalized the target.")

        async def download_result(self, dest_dir, *, retries=3, retry_delay=5.0):
            if not download_ok:
                return None
            root = Path(dest_dir) / "proj_aristotle"
            root.mkdir(parents=True, exist_ok=True)
            for rel, content in (generated or {}).items():
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content)
            return root

    return FakeInference


def _git(args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True,
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], root)
    (root / "README.md").write_text("base\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "init"], root)


# ---------------------------------------------------------------------------
# call(): lands files + commits, crediting Aristotle
# ---------------------------------------------------------------------------


class TestCall:
    @pytest.mark.asyncio
    async def test_call_lands_and_commits(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        wt = worktree.create_worktree(repo, "wt0", worktrees_dir=tmp_path / "worktrees")

        monkeypatch.setattr(
            "autoform.bot.aristotle_agent.AristotleInference",
            _make_fake_inference({"MyLib/Generated.lean": "theorem t : 1 = 1 := rfl\n"}),
        )
        agent = AristotleAgent(id="a0", worktree_path=wt)
        text = await agent.call("formalize theorem t")

        assert "Formalized" in text
        assert (wt / "MyLib" / "Generated.lean").exists()
        assert agent.total_turns == 1
        assert agent.messages  # populated from inference
        # A commit landed, authored by Aristotle.
        author = _git(["log", "-1", "--format=%an <%ae>"], wt).stdout.strip()
        assert "Aristotle (Harmonic)" in author
        # Co-author trailer present.
        body = _git(["log", "-1", "--format=%b"], wt).stdout
        assert "Co-authored-by: Aristotle (Harmonic)" in body

    @pytest.mark.asyncio
    async def test_call_no_files_no_commit(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        wt = worktree.create_worktree(repo, "wt1", worktrees_dir=tmp_path / "worktrees")
        before = _git(["rev-parse", "HEAD"], wt).stdout.strip()

        # Download succeeds but the returned project has no changed files.
        monkeypatch.setattr(
            "autoform.bot.aristotle_agent.AristotleInference",
            _make_fake_inference({}),
        )
        agent = AristotleAgent(id="a1", worktree_path=wt)
        await agent.call("do nothing")
        after = _git(["rev-parse", "HEAD"], wt).stdout.strip()
        assert before == after  # no commit created

    @pytest.mark.asyncio
    async def test_call_raises_on_download_failure(self, tmp_path, monkeypatch):
        """The bug that made a real run report a spurious failure: a terminal
        task whose result couldn't be downloaded must raise loudly, not land
        nothing silently."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        wt = worktree.create_worktree(repo, "wt_dl", worktrees_dir=tmp_path / "worktrees")

        monkeypatch.setattr(
            "autoform.bot.aristotle_agent.AristotleInference",
            _make_fake_inference({"X.lean": "x"}, download_ok=False),
        )
        agent = AristotleAgent(id="a2", worktree_path=wt)
        with pytest.raises(RuntimeError, match="could not be downloaded"):
            await agent.call("prove")

    def test_create_aristotle_agents(self, tmp_path):
        agents = create_aristotle_agents(worktrees=[tmp_path / "a", tmp_path / "b"])
        assert [a.id for a in agents] == ["aristotle-0", "aristotle-1"]
        assert all(isinstance(a, AristotleAgent) for a in agents)

    @pytest.mark.asyncio
    async def test_create_aristotle_pool_builds_worktrees(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        pool = create_aristotle_pool(repo, num_agents=2, agent_id_prefix="rank0", run_id="run-test")

        assert pool.size == 2
        agents = pool.checkout(2)
        assert all(isinstance(a, AristotleAgent) for a in agents)
        assert all(Path(a.worktree_path).is_dir() for a in agents)  # worktrees created
        assert pool.get_reviewer(agents[0].id) is None  # no reviewers in Mode B
        # Pool lifecycle no-ops don't raise.
        await pool.initialize()
        await pool.shutdown()


# ---------------------------------------------------------------------------
# run_task integration: AristotleAgent drives the real race/build/merge loop
# ---------------------------------------------------------------------------


class TestRunTaskIntegration:
    @pytest.mark.asyncio
    async def test_aristotle_agent_merges_to_main(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        wt = worktree.create_worktree(repo, "wt2", worktrees_dir=tmp_path / "worktrees")

        monkeypatch.setattr(
            "autoform.bot.aristotle_agent.AristotleInference",
            _make_fake_inference({"Generated.lean": "theorem t : 2 = 2 := rfl\n"}),
        )
        agent = AristotleAgent(id="ar0", worktree_path=wt)

        # Base ConcurrentAgents: build() is a no-op pass (no Mathlib needed),
        # no reviewer → straight to merge. This is the real worker-tier loop.
        ca = ConcurrentAgents(repo_root=repo)
        task = Task(id="T1", title="t", description="formalize theorem t")
        result = await ca.run_task(task, [agent], get_reviewer=None)

        assert result.success
        assert result.winner_id == "ar0"
        # The file Aristotle wrote is now on main.
        on_main = _git(["show", "main:Generated.lean"], repo).stdout
        assert "2 = 2" in on_main
