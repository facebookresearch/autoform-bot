# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""AristotleAgent — a worker that solves a target with Harmonic's Aristotle.

This is the "Mode B" integration of Aristotle into the AutoformBot worker tier.
Rather than driving a turn-based tool-calling agent loop, an ``AristotleAgent``
hands its *whole worktree* (a Lean project) plus the task description to
Aristotle, which runs its own internal tools and returns finished files; the
agent lands those files in the worktree and commits them.

Crucially it **duck-types the surface of** ``core.agent.Agent`` that
``ConcurrentAgents.run_task`` depends on (``id``, ``worktree_path``, async
``call``, ``reset``, ``set_trace``, ``total_turns``, ``messages``). So the
existing race/rebase/build/review/merge machinery runs **unchanged** — only the
"produce code in the worktree" step is swapped. The build (``lake build``),
reviewer (an LLM agent), and merge queue all operate on the worktree via git
and are model-agnostic, so they don't care that Aristotle wrote the code.

In-flight steering (the ``steer`` callback on ``AristotleInference``) flows
through here, so a Hermes-style reviewer can redirect a running Aristotle task.

Limitations are inherited from the backend (see ``core/inference/sdk/aristotle.py``):
Aristotle is job-based and slow (minutes–hours), so racing many Aristotle
workers is expensive; prefer ``min_agents_per_task: 1``.
"""

from __future__ import annotations

import fcntl
import logging
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import worktree
from core.coordination.pool import AgentPool
from core.inference.sdk.aristotle import AristotleInference, EventObserver, SteerCallback

logger = logging.getLogger(__name__)

# Credit Aristotle as the author of the Lean it writes (committer stays the
# pipeline identity so git history is well-formed).
_ARISTOTLE_AUTHOR = "Aristotle (Harmonic) <aristotle-harmonic@harmonic.fun>"
_COMMITTER_NAME = "autoform-bot"
_COMMITTER_EMAIL = "autoform-bot@users.noreply.github.com"

# Files/dirs never copied back from Aristotle's returned project.
_SKIP_TOP = {".git", ".lake"}

DEFAULT_WORKER_SYSTEM_PROMPT = (
    "You are an expert Lean 4 / Mathlib formalizer. The provided project is a "
    "Lean library; formalize the requested target into it, writing idiomatic, "
    "compiling Lean 4. Do not use `sorry`, `admit`, or new axioms. Make the "
    "project build with `lake build`."
)


class AristotleAgent:
    """A drop-in worker (duck-typed ``Agent``) backed by Aristotle.

    Args:
        id: Agent id (used by the coordinator / traces).
        worktree_path: The agent's git worktree — bundled to Aristotle as the
            project context, and where returned files are landed + committed.
        model_name: Aristotle model identifier (tracing only).
        system_prompt: System prompt sent with each submission.
        poll_interval / max_wait_seconds: Forwarded to ``AristotleInference``.
        on_event / steer: Optional observation / in-flight steering callbacks.
    """

    def __init__(
        self,
        *,
        id: str,
        worktree_path: Path | str,
        model_name: str = "aristotle",
        system_prompt: str = DEFAULT_WORKER_SYSTEM_PROMPT,
        poll_interval: int = 20,
        max_wait_seconds: float | None = 5400,
        on_event: EventObserver | None = None,
        steer: SteerCallback | None = None,
    ) -> None:
        self.id = id
        self.worktree_path = Path(worktree_path)
        self._model_name = model_name
        self._system_prompt = system_prompt
        self._poll_interval = poll_interval
        self._max_wait_seconds = max_wait_seconds
        self._on_event = on_event
        self._steer = steer

        # Agent-surface state expected by run_task.
        self.total_turns = 0
        self._messages: list[dict[str, Any]] = []
        self._trace: Any | None = None

    # ------------------------------------------------------------------
    # Agent surface used by ConcurrentAgents.run_task
    # ------------------------------------------------------------------

    async def call(self, user_message: str | None = None) -> str:
        """Submit the worktree + task to Aristotle; land + commit the result.

        Returns Aristotle's natural-language ``output_summary`` (non-empty), so
        run_task treats the turn as productive. Returns ``""`` only if nothing
        usable came back, which run_task surfaces as a failed attempt.
        """
        self.total_turns += 1
        prompt = user_message or ""

        with tempfile.TemporaryDirectory() as td:
            download_dir = Path(td)
            inf = AristotleInference(
                model_name=self._model_name,
                project_dir=self.worktree_path,
                download_dir=download_dir,
                poll_interval=self._poll_interval,
                max_wait_seconds=self._max_wait_seconds,
                on_event=self._on_event,
                steer=self._steer,
            )
            inf.set_system_prompt(self._system_prompt)
            inf.add_user_message(prompt)

            result = await inf.complete()
            self._messages = inf.get_messages()

            landed = self._overlay_result(download_dir)
            if landed:
                self._commit(prompt or "formalize target")
            else:
                logger.warning("Agent %s: Aristotle returned no project files to land", self.id)

        return (result.text or "").strip()

    def reset(self) -> None:
        self.total_turns = 0
        self._messages = []

    def set_trace(self, trace: Any | None) -> None:
        self._trace = trace

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self._messages

    # ------------------------------------------------------------------
    # Pool lifecycle (AgentPool.initialize/__aenter__ + shutdown/close).
    # Aristotle needs no warm-up (no REPL/LSP/subprocesses), so these no-op.
    # ------------------------------------------------------------------

    async def __aenter__(self) -> AristotleAgent:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------------
    # Landing Aristotle's output into the worktree
    # ------------------------------------------------------------------

    def _overlay_result(self, download_dir: Path) -> bool:
        """Copy Aristotle's returned project files over the worktree.

        The backend extracts the result tarball as ``download_dir/<root>/…``
        (a single top-level project directory). We overlay every file from
        there onto the worktree at the same relative path; git then sees only
        genuine changes. Returns True if at least one file was copied.
        """
        roots = [p for p in download_dir.iterdir() if p.is_dir()]
        if not roots:
            return False
        # Prefer the conventional ``*_aristotle`` root; else the sole directory.
        root = next((p for p in roots if p.name.endswith("_aristotle")), roots[0])

        copied = 0
        for src in root.rglob("*"):
            if src.is_dir():
                continue
            rel = src.relative_to(root)
            if rel.parts and rel.parts[0] in _SKIP_TOP:
                continue
            dest = self.worktree_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied += 1
        return copied > 0

    def _commit(self, summary: str) -> None:
        """Stage everything and commit, crediting Aristotle as author.

        No-ops cleanly if Aristotle's output was identical to the worktree.
        """
        wt = str(self.worktree_path)
        subprocess.run(["git", "add", "-A"], cwd=wt, check=False)
        # Nothing staged → nothing to commit (run_task then treats it as no progress).
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=wt).returncode == 0:
            return
        msg = f"Aristotle: {summary[:72]}"
        trailer = "\n\nCo-authored-by: " + _ARISTOTLE_AUTHOR
        subprocess.run(
            [
                "git",
                "-c", f"user.name={_COMMITTER_NAME}",
                "-c", f"user.email={_COMMITTER_EMAIL}",
                "commit",
                "--author", _ARISTOTLE_AUTHOR,
                "-m", msg + trailer,
            ],
            cwd=wt,
            check=False,
        )


def create_aristotle_agents(
    *,
    worktrees: list[Path],
    id_prefix: str = "aristotle",
    system_prompt: str = DEFAULT_WORKER_SYSTEM_PROMPT,
    poll_interval: int = 20,
    max_wait_seconds: float | None = 5400,
    on_event: EventObserver | None = None,
    steer: SteerCallback | None = None,
) -> list[AristotleAgent]:
    """Build one ``AristotleAgent`` per worktree (the Mode-B analog of the
    LLM-agent pool's worker construction)."""
    return [
        AristotleAgent(
            id=f"{id_prefix}-{i}",
            worktree_path=wt,
            system_prompt=system_prompt,
            poll_interval=poll_interval,
            max_wait_seconds=max_wait_seconds,
            on_event=on_event,
            steer=steer,
        )
        for i, wt in enumerate(worktrees)
    ]


def create_aristotle_pool(
    repo_root: Path,
    num_agents: int,
    *,
    agent_id_prefix: str = "aristotle",
    run_id: str | None = None,
    system_prompt: str = DEFAULT_WORKER_SYSTEM_PROMPT,
    poll_interval: int = 20,
    max_wait_seconds: float | None = 5400,
    on_event: EventObserver | None = None,
    steer: SteerCallback | None = None,
) -> AgentPool:
    """Create an ``AgentPool`` of Aristotle workers — the Mode-B analog of
    ``create_lean_pool``.

    Mirrors the LLM pool's worktree setup (NFS-safe lock + ``git worktree``
    + a ``.lake/packages`` symlink so ``lake build`` works in the worktree),
    but builds ``AristotleAgent``s and no reviewers (the build gate and the
    supervisor's eval harness still hold Aristotle's output to the same bar;
    a reviewer can later be added, or the ``steer`` hook can play that role
    in-flight).
    """
    if run_id is None:
        run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_worktrees_dir = repo_root.parent / "worktrees" / run_id
    run_worktrees_dir.mkdir(parents=True, exist_ok=True)

    wt_paths: list[Path] = []
    lock_path = repo_root / ".worktree_lock"
    with open(lock_path, "w") as lock_file:
        logger.info("[%s] Waiting for worktree lock...", agent_id_prefix)
        fcntl.lockf(lock_file, fcntl.LOCK_EX)
        subprocess.run(["git", "-C", str(repo_root), "worktree", "prune"], capture_output=True)
        for i in range(num_agents):
            wt_name = f"{run_id}-{agent_id_prefix}-worker-{i}"
            wt_paths.append(worktree.create_worktree(repo_root, wt_name, worktrees_dir=run_worktrees_dir))
        logger.info("[%s] Created %d Aristotle worktrees", agent_id_prefix, num_agents)

    # Share pre-resolved Mathlib deps so `lake build` in the worktree is cheap.
    for wt in wt_paths:
        lake_src = repo_root / ".lake" / "packages"
        lake_dst = Path(wt) / ".lake" / "packages"
        if lake_src.exists() and not lake_dst.exists():
            lake_dst.parent.mkdir(parents=True, exist_ok=True)
            lake_dst.symlink_to(lake_src.resolve())

    agents = create_aristotle_agents(
        worktrees=[Path(w) for w in wt_paths],
        id_prefix=f"{agent_id_prefix}-worker",
        system_prompt=system_prompt,
        poll_interval=poll_interval,
        max_wait_seconds=max_wait_seconds,
        on_event=on_event,
        steer=steer,
    )
    return AgentPool(agents=agents, reviewers={})
