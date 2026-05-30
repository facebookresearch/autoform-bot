#!/usr/bin/env python
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""End-to-end demo of the Mode-B Aristotle worker (the worker tier of the pipeline).

Drives a *real* Aristotle submission through the unmodified
``ConcurrentAgents.run_task`` loop:

    create throwaway repo + worktree
      -> AristotleAgent.call(): bundle worktree to Aristotle, land + commit files
      -> rebase -> build -> review(none) -> merge to main

`build()` here is the base no-op (so the demo needs no Mathlib checkout); the
production pipeline uses ``LeanConcurrentAgents.build`` = `lake build`. This
demo proves the *integration mechanism*: Aristotle's output flows through the
real worker-tier machinery onto `main`.

Run:  ARISTOTLE_API_KEY=arstl_... python -m examples.aristotle_worker_demo
"""

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import aristotlelib

from autoform.bot.aristotle_agent import AristotleAgent
from core import worktree
from core.coordination.concurrent_agents import ConcurrentAgents
from core.task import Task

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("demo")


def _git(args, cwd):
    return subprocess.run(["git", "-c", "user.name=demo", "-c", "user.email=demo@demo", *args],
                          cwd=cwd, capture_output=True, text=True)


def _seed_repo(root: Path) -> None:
    """A minimal Lean v4.28 project committed on main."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.28.0\n")
    (root / "lakefile.toml").write_text(
        'name = "Demo"\ndefaultTargets = ["Demo"]\n\n'
        '[[require]]\nname = "mathlib"\n'
        'git = "https://github.com/leanprover-community/mathlib4.git"\nrev = "v4.28.0"\n\n'
        '[[lean_lib]]\nname = "Demo"\n'
    )
    (root / "Demo").mkdir(exist_ok=True)
    (root / "Demo" / "Target.lean").write_text(
        "import Mathlib\n\n-- TODO(aristotle): prove `1 + 1 = 2`\n"
    )
    _git(["init", "-b", "main"], root)
    _git(["add", "-A"], root)
    _git(["commit", "-m", "seed Demo project"], root)


async def main() -> int:
    key = os.environ.get("ARISTOTLE_API_KEY")
    if not key:
        print("Set ARISTOTLE_API_KEY", file=sys.stderr)
        return 2
    aristotlelib.set_api_key(key)

    tmp = Path(tempfile.mkdtemp(prefix="aristotle-worker-demo-"))
    repo = tmp / "repo"
    _seed_repo(repo)
    wt = worktree.create_worktree(repo, "worker0", worktrees_dir=tmp / "worktrees")
    log.info("repo=%s worktree=%s", repo, wt)

    async def on_event(ev):
        et = getattr(getattr(ev, "event_type", None), "name", "?")
        log.info("  [aristotle] %s", et)

    agent = AristotleAgent(id="aristotle-worker-0", worktree_path=wt, on_event=on_event,
                           poll_interval=15, max_wait_seconds=1800)

    task = Task(
        id="demo-1",
        title="prove 1+1=2",
        description="In `Demo/Target.lean`, replace the TODO with a complete Lean 4 proof "
                    "that `1 + 1 = 2` (using Mathlib). The project must build.",
    )

    ca = ConcurrentAgents(repo_root=repo)  # base: build() is a no-op (no Mathlib needed)
    log.info("running worker-tier loop (this submits to Aristotle)...")
    result = await ca.run_task(task, [agent], get_reviewer=None)

    log.info("\n=== result: success=%s winner=%s ===", result.success, result.winner_id)
    if result.success:
        log.info("Demo/Target.lean on main:\n%s", _git(["show", "main:Demo/Target.lean"], repo).stdout)
        log.info("commit: %s", _git(["log", "-1", "--format=%h %an: %s"], repo).stdout.strip())
    else:
        log.info("error: %s", result.error)
    log.info("(artifacts under %s)", tmp)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
