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
import re
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

_STANDARD_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
_DECL_RE = re.compile(r"^(?:noncomputable )?(?:theorem|lemma|def|instance)\s+([A-Za-z_][\w'.]*)", re.M)
_AXIOM_RE = re.compile(r"(?:^|[^A-Za-z])axiom\s")

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
            inf = AristotleInference(
                model_name=self._model_name,
                project_dir=self.worktree_path,
                poll_interval=self._poll_interval,
                max_wait_seconds=self._max_wait_seconds,
                on_event=self._on_event,
                steer=self._steer,
            )
            inf.set_system_prompt(self._system_prompt)
            inf.add_user_message(prompt)

            result = await inf.complete()
            self._messages = inf.get_messages()

            # Explicit, retrying download — landing must not be best-effort, or
            # run_task sees an empty worktree and reports a spurious failure.
            root = await inf.download_result(td)
            if root is None:
                raise RuntimeError(
                    f"Agent {self.id}: Aristotle task finished "
                    f"({inf.last_status}) but its result could not be downloaded"
                )
            if self._overlay_from(root):
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

    def _overlay_from(self, root: Path) -> bool:
        """Copy Aristotle's returned project files (under ``root``) over the
        worktree at the same relative paths; git then sees only genuine
        changes. Skips dirs, symlinks, and ``.git``/``.lake``. Returns True if
        at least one file was copied.
        """
        copied = 0
        for src in root.rglob("*"):
            if src.is_dir() or src.is_symlink():
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


REVIEWER_SYSTEM_PROMPT = (
    "You are a STRICT, ADVERSARIAL reviewer of Lean 4 / Mathlib formalizations produced "
    "by an autonomous prover. Your default verdict is REJECTED; APPROVE only if, after "
    "actively hunting for defects, you find none. You are given MECHANICAL GROUND TRUTH "
    "(a full-file scan and `#print axioms` results) — trust it over any prose or "
    "docstring. REJECT if ANY of the following holds:\n"
    "1. **Hidden gaps / axioms** — the scan shows `sorry`/`admit`/raw `axiom`/`native_decide`, "
    "OR `#print axioms` lists `sorryAx` or any axiom outside {propext, Classical.choice, "
    "Quot.sound}.\n"
    "2. **Vacuity / triviality** — a theorem restates `True`/something trivially provable; an "
    "`instance` is vacuous; a proof closes a goal via `False.elim` of a false hypothesis.\n"
    "3. **Forced generality** — a parameter advertised as general is secretly pinned (e.g. a "
    "smoothness `k` constrained to `⊤`, or `k = c`), making the 'general' result trivial.\n"
    "4. **Weakened statement** — extra hypotheses not warranted, or content hidden in "
    "structure/class fields (a Theorem/Proposition must be a proved theorem, not an assumed "
    "field). The claimed result must actually be established (e.g. an `IsManifold` instance "
    "must really prove `ContDiffOn` of the transitions, not merely assert a ChartedSpace).\n"
    "5. **Task not met** — the change does not actually accomplish the stated task.\n"
    "When uncertain, REJECT. Begin your reply with exactly `APPROVED` or `REJECTED`, then give "
    "specific reasons grounded in the mechanical evidence and the code."
)


def _line_has_nonstandard_axiom(line: str) -> bool:
    """True if a `#print axioms` line mentions `sorryAx` or any axiom outside the
    standard set."""
    if "sorryAx" in line:
        return True
    tail = line.split("axioms:", 1)[-1]
    names = re.findall(r"[A-Za-z_][\w.]*", tail)
    return any(nm not in _STANDARD_AXIOMS for nm in names)


def _axiom_audit(worktree: Path, file_to_decls: dict[Path, list[str]]) -> str:
    """`#print axioms` on each changed file's top-level declarations — the
    un-foolable check for hidden `sorryAx` / non-standard axioms.

    Appends `#print axioms` lines to a copy of each file and elaborates it
    (reliable: the copy is self-contained, so no cross-module import resolution
    is needed). Flags any non-standard axiom. Best-effort per file.
    """
    out_lines: list[str] = []
    any_bad = False
    for f, decls in file_to_decls.items():
        if not decls:
            continue
        tmp = f.parent / f"_AxiomAudit_{f.stem}.lean"
        try:
            tmp.write_text(f.read_text() + "\n\n" + "".join(f"#print axioms {d}\n" for d in decls))
            proc = subprocess.run(
                ["lake", "env", "lean", str(tmp.relative_to(worktree))],
                cwd=str(worktree), capture_output=True, text=True, timeout=900,
            )
        except Exception as err:  # pragma: no cover
            out_lines.append(f"- {f.name}: axiom audit could not run: {err}")
            continue
        finally:
            tmp.unlink(missing_ok=True)
        ax = [ln.strip() for ln in (proc.stdout + proc.stderr).splitlines() if "depends on axioms" in ln]
        for ln in ax:
            if _line_has_nonstandard_axiom(ln):
                any_bad = True
                out_lines.append("  ⚠️ " + ln)
            else:
                out_lines.append("  " + ln)
        if not ax:
            out_lines.append(f"- {f.name}: (no axiom output — elaboration may have failed)")
    header = "⚠️ NON-STANDARD AXIOM / sorryAx DETECTED" if any_bad else "OK: only standard axioms"
    return header + "\n" + ("\n".join(out_lines) or "(nothing to audit)")


def _mechanical_audit(worktree: Path) -> tuple[str, list[Path]]:
    """Full-file scan of the changed `.lean` files + `#print axioms`. Returns the
    report text and the list of changed Lean files."""
    names = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~1"], cwd=str(worktree),
        capture_output=True, text=True,
    ).stdout.split()
    lean_files = [worktree / n for n in names if n.endswith(".lean")]
    scan_lines, file_to_decls = [], {}
    for f in lean_files:
        txt = f.read_text() if f.exists() else ""
        rel = f.relative_to(worktree)
        scan_lines.append(
            f"- {rel}: sorry={txt.count('sorry')} admit={txt.count('admit')} "
            f"axiom={len(_AXIOM_RE.findall(txt))} native_decide={txt.count('native_decide')}"
        )
        file_to_decls[f] = _DECL_RE.findall(txt)
    report = (
        "## Mechanical full-file scan (ground truth)\n" + "\n".join(scan_lines)
        + "\n\n## #print axioms (ground truth)\n" + _axiom_audit(worktree, file_to_decls)
    )
    return report, lean_files


class ClaudeReviewer:
    """A Claude-backed reviewer that duck-types the reviewer surface
    ``ConcurrentAgents.review`` uses (``id``, async ``call``, ``reset``,
    ``set_trace``, ``total_turns``, ``messages``).

    Unlike the full tool-calling reviewer agent, it has no tools: it computes
    the worktree diff itself and asks Claude for an ``APPROVED``/``REJECTED``
    verdict. The verdict gates the merge exactly like the pipeline's reviewer.
    """

    def __init__(
        self, *, id: str, worktree_path: Path | str, model: str = "Opus 4.6",
        use_cli: bool = False, rigorous: bool = True,
    ) -> None:
        self.id = id
        self.worktree_path = Path(worktree_path)
        self._model = model
        self._use_cli = use_cli  # route via the `claude` CLI (Max OAuth) instead of the SDK
        self._rigorous = rigorous  # run mechanical ground-truth checks (scan + #print axioms)
        self.total_turns = 0
        self._messages: list[dict[str, Any]] = []

    async def call(self, user_message: str | None = None) -> str:
        import asyncio

        self.total_turns += 1
        diff = subprocess.run(
            ["git", "diff", "HEAD~1"], cwd=str(self.worktree_path),
            capture_output=True, text=True,
        ).stdout
        if self._rigorous:
            # Un-foolable ground truth: full-file scan + `#print axioms`, plus the
            # whole changed files (not just the diff) so nothing escapes review.
            audit, lean_files = await asyncio.to_thread(_mechanical_audit, self.worktree_path)
            full = []
            for f in lean_files:
                try:
                    full.append(f"### {f.name} (full)\n```lean\n{f.read_text()[:30000]}\n```")
                except OSError:
                    pass
            context = audit + "\n\n" + "\n\n".join(full) + f"\n\n## Diff\n```diff\n{diff[:20000]}\n```"
        else:
            context = f"## Changed code (git diff HEAD~1)\n```diff\n{diff[:40000]}\n```"
        prompt = (
            f"{user_message or ''}\n\n{context}\n\n"
            "Reply APPROVED or REJECTED (first word), then specific reasons grounded in the evidence."
        )
        self._messages = [{"role": "user", "content": prompt}]
        if self._use_cli:
            # Scrub ANTHROPIC_API_KEY so the CLI uses Max OAuth (not API billing).
            import os

            env = os.environ.copy()
            env.pop("ANTHROPIC_API_KEY", None)
            proc = await asyncio.to_thread(
                subprocess.run,
                ["claude", "-p", REVIEWER_SYSTEM_PROMPT + "\n\n" + prompt, "--output-format", "text"],
                capture_output=True, text=True, env=env,
            )
            answer = (proc.stdout or "").strip() or "REJECTED: reviewer produced no output"
        else:
            from core.inference.client import create_inference, lookup_model

            inf = create_inference(lookup_model(self._model))
            inf.set_system_prompt(REVIEWER_SYSTEM_PROMPT)
            inf.add_user_message(prompt)
            result = await inf.complete()
            answer = (result.text or "").strip()
        self._messages.append({"role": "assistant", "content": answer})
        return answer

    def reset(self) -> None:
        self.total_turns = 0
        self._messages = []

    def set_trace(self, trace: Any | None) -> None:
        return None

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self._messages

    async def __aenter__(self) -> ClaudeReviewer:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def close(self) -> None:
        return None


def _claude_cli(prompt: str, *, timeout: int = 180) -> str:
    """Invoke the `claude` CLI (Max OAuth — `ANTHROPIC_API_KEY` scrubbed). Returns
    stdout, or "" on failure."""
    import os

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, env=env, timeout=timeout,
        )
        return (proc.stdout or "").strip()
    except Exception as err:  # pragma: no cover
        logger.warning("claude CLI failed: %s", err)
        return ""


STEER_JUDGE_RUBRIC = (
    "You are the live-steering judge ('Hermes') for an autonomous Lean prover. You see a "
    "window of its recent events (thinking, file edits, errors). Decide whether it is going "
    "OFF-COURSE relative to the GOAL — e.g. abandoning the goal, axiomatizing/`sorry`-ing what "
    "it was asked to prove, weakening or pinning a parameter it was told to keep general, "
    "going in circles, or building the wrong thing. Only steer when genuinely warranted; a "
    "needless steer wastes a turn. If steering, give a SHORT, concrete corrective instruction."
)


def make_claude_steer(goal: str, *, min_gap_s: float = 120.0, max_steers: int = 3):
    """Build a `steer` callback (for AristotleInference) backed by a Claude judge.

    On each batch of new events it (rate-limited) asks Claude whether the prover
    is off-course w.r.t. ``goal``; if so, returns a corrective prompt that the
    backend injects via ``project.ask`` mid-run. This is the working replacement
    for the keyword heuristic (which never fired): it reads the actual reasoning
    in the event stream rather than grepping for tokens.
    """
    import json
    import time

    state = {"last": 0.0, "count": 0, "reasons": []}

    async def steer(new_events: list[Any], task: Any) -> str | None:
        import asyncio

        if state["count"] >= max_steers:
            return None
        relevant = [
            e for e in new_events
            if getattr(getattr(e, "event_type", None), "name", "") in ("EDITING_FILE", "THINKING", "MESSAGE", "ERROR")
        ]
        if not relevant:
            return None
        now = time.monotonic()
        if now - state["last"] < min_gap_s:
            return None
        window = "\n".join(
            f"[{getattr(e.event_type, 'name', '?')}] {(getattr(e, 'content', None) or '')[:300]}"
            for e in relevant[-8:]
        )
        prompt = (
            f"{STEER_JUDGE_RUBRIC}\n\n## GOAL\n{goal}\n\n## RECENT EVENTS\n{window}\n\n"
            f"## PRIOR STEER REASONS\n{state['reasons'] or '(none)'}\n\n"
            'Return ONE LINE of JSON: {"steer": <bool>, "reason": "<short>", "prompt": "<corrective instruction or empty>"}'
        )
        raw = await asyncio.to_thread(_claude_cli, prompt)
        if not raw or "{" not in raw:
            return None
        try:
            decision = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        except Exception:
            return None
        if decision.get("steer") and (decision.get("prompt") or "").strip():
            state["last"] = now
            state["count"] += 1
            state["reasons"].append((decision.get("reason") or "")[:120])
            logger.info("Claude steer #%d: %s", state["count"], state["reasons"][-1])
            return decision["prompt"].strip()
        return None

    return steer


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
