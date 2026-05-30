# Aristotle worker (Mode-B integration)

Harmonic's **Aristotle** is an autonomous formal-reasoning agent: give it a Lean
project + a goal and it runs its *own* internal tools (proof search, Lean
builds, file edits) and returns finished files. That doesn't fit AutoformBot's
default worker, which is a turn-based, tool-calling LLM agent driven move by
move. This note describes how Aristotle is integrated **as a worker** instead.

## Two modes (and why this is Mode B)

- **Mode A — Aristotle as a served model.** `core/inference/sdk/aristotle.py`
  registers `Aristotle` in the model registry, so `model: "Aristotle"` resolves
  and `create_inference` builds the backend. This is enough for the simple
  `InferenceProtocol` consumers (statement extraction, the LLM graders) but
  **not** for the worker loop: that loop hands the agent MCP tools and expects
  `tool_calls` back plus incremental worktree edits — Aristotle returns prose +
  a finished tarball, so a worker "powered by Aristotle" would edit nothing in
  its worktree. *Selectable ≠ operational.*

- **Mode B — Aristotle as a worker (this).** In `ConcurrentAgents.run_task`, the
  **only** model-specific step is `agent.call(task.description)` — everything
  after it (`rebase` → `build` → `review` → `_attempt_merge`) operates on the
  agent's git worktree and is model-agnostic. So we swap just that step.

## The seam: `AristotleAgent`

`autoform/bot/aristotle_agent.py` defines `AristotleAgent`, which **duck-types
the `core.agent.Agent` surface** that `run_task` uses (`id`, `worktree_path`,
async `call`, `reset`, `set_trace`, `total_turns`, `messages`). Its `call()`:

1. bundles the **whole worktree** as Aristotle's project context
   (`AristotleInference(project_dir=worktree)`),
2. submits the task description, polls to a terminal status (optionally
   observing/steering via the `on_event`/`steer` hooks),
3. **lands** the returned files over the worktree and **commits** them
   (authored by Aristotle, co-author trailer).

Then `run_task` proceeds exactly as for an LLM worker: rebase, build, review,
merge. Nothing else in the pipeline changes — the merge queue, worktree
isolation, racing, and the supervisor's eval harness all act on files/compiled
output and are author-agnostic.

```
            run_task (UNCHANGED)
   agent.call ─► rebase ─► build ─► review ─► merge ─► main
       │
   AristotleAgent.call:  bundle worktree → Aristotle → land files → commit
```

## What's implemented

- `AristotleAgent` + `create_aristotle_agents` (`autoform/bot/aristotle_agent.py`)
- `solver` config field (`llm.solver: agent | aristotle`) in `autoform/bot/config.py`
- Unit + integration tests (`autoform/bot/aristotle_agent_test.py`) — including
  driving a real `ConcurrentAgents.run_task` to a merge with a fake backend.
- A runnable worker-tier demo (`examples/aristotle_worker_demo.py`) that takes a
  *real* Aristotle submission all the way to `main`.

## What remains to fully activate `solver: aristotle`

The demo constructs the agent directly. To flip the whole pipeline over, the
node still needs to build Aristotle agents instead of LLM agents:

- In `autoform/bot/worker_node.py` / `pool.py`, branch on `config.solver`: when
  `aristotle`, create a pool of `AristotleAgent`s over the per-agent worktrees
  (reusing worktree creation; skipping the REPL/LSP/MCP setup Aristotle doesn't
  use). Reviewers can remain LLM agents, or the `steer` hook can play the
  reviewer role in-flight (Hermes-style).
- Thread `solver` from `main.py` into `LeanWorkerNode`.

These are mechanical wiring changes; the design above is the substantive part.

## Caveats

- **Granularity & cost.** Aristotle jobs run minutes–hours; racing several per
  target is expensive. Use `min_agents_per_task: 1` and a separate concurrency
  budget — don't reuse the LLM-call resource pool sizing.
- **Build is the real gate.** With `LeanConcurrentAgents`, `build()` runs
  `lake build`; a returned project with `sorry`/errors fails the build (and the
  supervisor's harness rejects unjustified `sorry`/axioms), so Aristotle's
  output is held to the same bar as any worker's.
- **Orchestrator/supervisor unchanged.** The DAG planner and eval harness are
  upstream/downstream of the worker and don't change. (One real subtlety for
  copyrighted sources: the orchestrator and reviewers also read source text, so
  a fully copyright-confined run would need those to be Aristotle-only too.)
