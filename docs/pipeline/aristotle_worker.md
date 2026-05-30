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

- `AristotleAgent` + `create_aristotle_agents` + `create_aristotle_pool`
  (`autoform/bot/aristotle_agent.py`).
- `solver` config field (`llm.solver: agent | aristotle`) in `autoform/bot/config.py`.
- **Full pipeline wiring:** `LeanWorkerNode.initialize()` branches on `solver` —
  when `aristotle`, it builds an Aristotle worker pool (`create_aristotle_pool`:
  same worktrees + `.lake` symlink, no REPL/LSP, no reviewers) instead of the
  LLM pool. `main.py` threads `solver` from the config. The `LeanConcurrentAgents`
  build gate (`lake build`) and merge queue are reused unchanged.
- Unit + integration tests (`autoform/bot/aristotle_agent_test.py`): land/commit,
  pool construction (real worktrees + `AgentPool` lifecycle), and driving a real
  `ConcurrentAgents.run_task` to a merge with a fake backend.
- A runnable worker-tier demo (`examples/aristotle_worker_demo.py`) that takes a
  *real* Aristotle submission all the way to `main`.

## Running it

```yaml
# config.yaml
llm:
  solver: aristotle      # workers are Aristotle; model only matters if reviewers are enabled
workers:
  agents_per_node: 1     # Aristotle jobs are slow/expensive — don't race many
  min_agents_per_task: 1
  max_agents_per_task: 1
```

`ARISTOTLE_API_KEY` must be in the environment (the SDK reads it directly).
Everything else — orchestrator DAG planning, the supervisor's eval harness,
the merge queue — is unchanged.

## Not yet done (deliberately)

- **Reviewers.** Mode-B workers currently have no paired reviewer; the build
  gate + supervisor eval still apply. A natural next step is to let the `steer`
  hook play an in-flight reviewer (Hermes-style) rather than a post-hoc one.
- **Separate concurrency budget.** Aristotle jobs are long; the LLM-call
  resource-pool sizing isn't the right knob for them.

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
