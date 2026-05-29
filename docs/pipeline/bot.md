# Autoform Bot Pipeline

## Overview

The autoform bot is a multi-agent pipeline that translates a mathematical textbook (LaTeX or Markdown) into verified Lean 4 proofs backed by Mathlib. An orchestrator agent reads the book, creates a task DAG (directed acyclic graph) of formalization work, and dispatches tasks to a pool of worker agents that operate in isolated git worktrees. Each worker writes Lean code, commits it, and submits it to a merge queue; completed merges trigger automated evaluation against the book's targets.

The pipeline supports both single-node and multi-node (SLURM) execution. All nodes share the same code path: every node runs a `WorkerNode`, and rank 0 additionally runs the coordinator.

## Pipeline Architecture

A run proceeds through these stages:

1. **Workspace setup** (rank 0). `ensure_run_workspace` creates the run directory with `code/` (a Lean 4 git repo from template), `book/` (copied LaTeX source), `skills/` (seeded reference material), and `traces/`/`archive/` directories. An initial `lake build` compiles the project and freezes `.lake/packages` read-only.

2. **Worker readiness**. Rank 0 starts a ZMQ task server and spawns its local worker as a child process. Remote workers (rank 1+) connect via ZMQ. Each worker blocks on a "ready" signal until workspace setup completes.

3. **Coordinator loop** (rank 0). Two concurrent coroutines run inside `LeanCoordinatorNode.run_pipeline`:
   - **Orchestrator loop** (`_orchestrator_loop`): waits for task reports on an `asyncio.Queue`, feeds them to the orchestrator agent, which reads reports, inspects the DAG, and adds/updates/deletes tasks.
   - **DAG runner** (`DAGRunner`): continuously picks ready tasks from the `ItemTracker` DAG and dispatches them to workers via `DistributedExecutor`.

4. **Task execution** (all nodes). Each `LeanWorkerNode` owns an `AgentPool` of worker+reviewer pairs. When a task arrives, `LeanConcurrentAgents.run_task` races multiple workers, builds the result with `lake build`, runs a review (correctness reviewer + quality inspector in parallel), and submits the winning diff to the merge queue.

5. **Merge queue** (rank 0). A bors-style `MergeQueue` batches completed worktree diffs, cherry-picks them onto main, and runs `lake build` on the staging branch. On success, it triggers merge evaluation.

6. **Merge evaluation** (rank 0). `run_merge_eval` diffs the merged code, asks a merge matcher agent to identify affected book targets, grades them with rubric-based LLM judges (faithfulness, proof integrity, code quality), updates the goal tracker, and auto-creates fix tasks for failures.

7. **Trace analysis** (rank 0). After each failed task, a persistent `TraceAnalyzerManager` agent inspects the worker's trace, writes a report and task-specific skills (hints for the next attempt), and signals the orchestrator to re-plan.

8. **Shutdown**. The coordinator finalizes all traces, archives reports, garbage-collects worktrees, and joins the local worker process.

## DAG Workflow

Tasks flow through these statuses: **pending** -> **in_progress** -> **completed** | **failed** | **deleted**.

### Creation

The orchestrator agent creates tasks by calling `add_item(title, description, depends_on, item_id)`. Each task represents at most one mathematical statement (definition, theorem, lemma, proposition, corollary) or one specific fix. Tasks carry a `flavor` field: `"task"` for orchestrator-created work, `"meval"` for merge-eval triage auto-created fixes, `"decomposition"` for analyzer-proposed subtasks.

### Dependency resolution

`ItemTracker` maintains a DAG with `depends_on` and `dependents` edges. A task is "ready" when all its dependencies are completed or deleted. `DAGRunner` polls for ready tasks and dispatches them to workers. The orchestrator can also call `dispatch_task(id)` or `dispatch_ready()` to push tasks immediately.

### Execution

`DistributedExecutor` picks a node with available capacity (using `pick_strategy`, default `BIGGEST_FIRST`) and sends the task via ZMQ. The worker node checks out agents from its pool, runs `ConcurrentAgents.run_task` (which can race multiple agents in parallel when `max_agents_per_task > 1`), and returns a result.

### Review

After a worker produces code that compiles (`lake build` passes), the review phase runs two agents concurrently:
- **Correctness reviewer**: checks the diff against the book source and the task prompt. Must approve statement faithfulness and proof soundness.
- **Quality inspector**: checks Mathlib conventions, naming, and code quality.

Both must approve. If either rejects, the worker receives the combined feedback and tries again (up to `max_review_cycles` iterations).

### Merge

Approved code is submitted to the `MergeQueue` via `MergeQueueClient` (ZMQ). The queue batches submissions, cherry-picks them onto a staging branch, runs `lake build`, and fast-forward merges on success. On failure, individual submissions are retried.

### Post-merge

After a successful merge batch:
1. **Merge eval** identifies affected book targets, assesses them with LLM judges, updates the goal tracker (pending/completed/failed), and auto-creates `meval-fix-*` tasks for failures.
2. **Trace analysis** runs for each failed task: a persistent analyzer agent inspects the worker trace, writes a report to `reports/task_reports/{task_id}.json`, and writes task-specific skills to `skills/tasks/{task_id}/guide.md`.
3. Reports land in the orchestrator's queue, triggering the next planning round.

### Re-planning

The orchestrator reads new reports, inspects the DAG, and decides what to do:
- **Completed tasks**: no action. Dependencies are unblocked.
- **Failed tasks**: update the description with better hints, split into subtasks, or delete and replace.
- **Merge eval failures**: review auto-created fix tasks, enrich them with context, and dispatch.

The ConstrainedTracker prevents agents from changing task status directly -- only `DAGRunner` owns lifecycle transitions.

## Multi-Node Setup

The pipeline uses SLURM-aware distributed execution via ZMQ. Configuration is auto-detected from environment variables (`SLURM_PROCID`, `SLURM_NTASKS`, `MASTER_ADDR`, `MASTER_PORT`).

### Roles

- **Rank 0** runs the `LeanCoordinatorNode` (orchestrator loop, DAG runner, merge queue, merge eval) and spawns a local worker as a child process.
- **Rank 1+** each run a `LeanWorkerNode` that connects to the coordinator via ZMQ.

### Communication

- **Task dispatch**: `ZmqTaskServer` (rank 0) sends task payloads to `ZmqTaskClient` (workers). Workers return results on the same channel.
- **Merge queue**: `MergeQueueServer` (rank 0) listens on `port + 100`. Workers submit diffs via `MergeQueueClient`.
- **Readiness signaling**: Before workspace setup completes, workers send `{"type": "waiting"}` and block until they receive `{"type": "ready"}`.

### Worktree isolation

Each worker creates git worktrees under `worktrees/{run_id}/`. Worktree creation is serialized across nodes via a file lock (`code/.worktree_lock`) to prevent concurrent `git worktree add` from corrupting `.git/worktrees/` on shared NFS. Each worktree symlinks `.lake/packages` from the main repo to share pre-built dependencies.

### Node pick strategy

`DistributedExecutor` selects which node receives a task using `pick_strategy`:
- `BIGGEST_FIRST` (default): picks the node with the most available agents.
- Other strategies are defined in `core.coordination.multinode.NodePickStrategy`.

## Agent Roles

### Orchestrator

**Config**: `agents/orchestrator/config.yaml` -- Opus 4.6, 1M context, 100k max turns, compact threshold 0.85.

The orchestrator is a long-lived, persistent agent that plans the task DAG. It runs once per round (triggered by new task reports), reads the book, inspects completed code via git, and creates/updates/deletes tasks. Its conversation history persists across rounds and survives restarts (via trace resume from `TraceStore`). It has access to the task tracker, goal tracker, reports loader, filesystem (read-only on book/code/skills), git (read-only on code), Lean analysis tools, a reading agent, a TODO list, and an escalation tool.

### Worker

**Config**: `agents/worker/config.yaml` -- Opus 4.6, 1M context, 250 max turns, 300s tool timeout.

Workers are ephemeral agents that execute a single task. Each worker operates in an isolated git worktree with access to Lean LSP, Lean REPL, filesystem (read-write in worktree), Mathlib search, git, a reading agent, and an escalation tool. Workers read task-specific skills before starting, write Lean code, run diagnostics, and commit their changes. They do not have access to bash (sandbox protection).

### Reviewer

**Config**: `agents/reviewer/config.yaml` -- Opus 4.6, 1M context, 40 max turns.

Reviewers are paired 1:1 with workers and share the same worktree. After a worker's code compiles, the reviewer checks the diff against the book source, verifies statement faithfulness, and approves or rejects with actionable feedback. Has access to LSP, filesystem (read-only on worktree), and git.

### Quality Inspector

**Config**: `agents/quality_inspector/config.yaml` -- Opus 4.6, 1M context, 40 max turns.

A transient agent spawned during review alongside the reviewer. It checks Mathlib conventions (naming, style, structure) on the diff. Has access to LSP, filesystem, and git on the worktree. Both the reviewer and inspector must approve for the review to pass.

### Trace Analyzer

**Config**: `agents/trace_analyzer/config.yaml` -- Opus 4.6, 1M context, 100k max turns.

A persistent per-task agent that runs after each failed attempt. It inspects the worker's trace via the `trace_inspector` tool, reads escalations, and writes: (1) a JSON report to `reports/task_reports/{task_id}.json` with findings and suggestions, and (2) a skill guide to `skills/tasks/{task_id}/guide.md` with lessons learned. Its conversation history accumulates across attempts, so it can reason about how a task evolves over time.

### Reader

**Config**: `agents/reader/config.yaml` -- Haiku 4.5, 15 max turns, 60s tool timeout.

A lightweight, short-lived agent used by the `read_and_summarize` tool. Spawned on demand to read a file and return a targeted summary, allowing callers (orchestrator, workers) to inspect large files without consuming their own context window. Has filesystem access only.

### Merge Matcher

**Config**: `agents/merge_matcher/config.yaml` -- Opus 4.6, 1M context, 1000 max turns, filesystem read-only (code + book).

Spawned during merge evaluation to identify which book targets are affected by a given git diff. Returns a JSON list of affected target indices. Has filesystem access to the code and book directories.

### Merge Eval Triage

**Config**: `agents/merge_eval_triage/config.yaml` -- Opus 4.6, 200k context, 100 max turns.

Spawned once per failed goal during merge evaluation. Reads the book, reads the Lean code, reads the eval feedback, and creates granular fix tasks (one per sorry, one per axiom issue, one per faithfulness problem) in the DAG with `meval` flavor. Has filesystem (read-only) and task tracker access.

## Tool Servers

The pipeline provides several custom MCP tool servers beyond the standard filesystem/git/LSP/REPL:

### Task Tracker (`task-tracker`)

Wraps `ItemTracker` with a `ConstrainedTracker` guard. Provides `list_items`, `get_item`, `add_item`, `update_item`, `delete_item`, `dispatch_task`, and `dispatch_ready`. Mutations are restricted to pending/failed items of the agent's configured flavor. Status transitions are owned by `DAGRunner`, not the agent.

### Goal Tracker (`goal-tracker`)

Read-only access to the goal tracker (book targets). Provides `list_goals(status, query)` (compact view: status + score only) and `get_goal(goal_id)` (full details including feedback, lean declaration, and file path). Available to the orchestrator only.

### Reports (`reports`)

Provides `load_reports()` which returns all JSON reports from `reports/task_reports/` as a compact array. Used by the orchestrator at the start of each round to learn what happened in the previous round.

### Lean Analysis (`lean-analysis`)

Provides `find_sorries_in_codebase()` which greps all `.lean` files in the code directory for `sorry` occurrences. Used by the orchestrator to audit proof completeness.

### Reading Agent (`reading-agent`)

Provides `read_and_summarize(path, instructions)` which spawns a short-lived Haiku reader agent to read a file and return a focused summary. Prevents large files from consuming the caller's context window. Available to orchestrator, workers, and reviewers.

### Escalation (`escalate`)

Provides `escalate(severity, message)` for workers to flag issues to the human operator. Severity levels: `critical` (pipeline blocked), `warning` (can continue but compromised), `decomposition` (propose task splitting). Logged to `escalations.jsonl`. The trace analyzer can read escalations for a task via the `escalation-reader` server.

### TODO (`todo`)

A persistent personal TODO list for the orchestrator. Provides `todo_add`, `todo_list`, `todo_update`, `todo_set_status`, and `todo_delete`. Capped at 30 items. Backed by `ItemTracker` persisted to `orchestrator_todos.json`.

## Skills System

Skills are reference documents injected into the worker's workspace at `skills/`. They provide accumulated knowledge that helps workers avoid known pitfalls.

### Seeding

On workspace initialization, `_seed_skills` copies the curated skill directories from `autoform/bot/skills/` into the run's `skills/` directory. Only directories that don't already exist are copied (preserving any modifications from a previous run). The source tree contains:

- `skills/lean/` -- Lean 4 syntax, tactic patterns, type coercions, proof patterns, norms/bounds, derivatives, integrals, build performance tips.
- `skills/mathlib/SKILL.md` -- Mathlib conventions and common pitfalls distilled from PR reviews. Workers are instructed to read this first.
- `skills/workflow/` -- Process guidance: axiom policy, build timeouts, commit conventions, sorry handling, proof strategies, review patterns, tool usage.

### Task-specific skills

After each failed attempt, the trace analyzer writes a guide to `skills/tasks/{task_id}/guide.md`. This file contains lessons specific to that task: what approaches were tried, what errors occurred, which Mathlib APIs to use, and what to try next. Workers are instructed to check for this file before starting any task.

### Lifecycle

Skill folders are reconciled with the DAG after each orchestrator round (`sync_skill_folders`):
- Pending/in-progress/failed tasks: ensure `skills/tasks/{task_id}/` exists.
- Completed/deleted tasks: archive the skill folder to `archive/skills/` and remove it.

## Configuration Reference

The pipeline is configured via a YAML file passed with `--config` or found at `{run_path}/config.yaml`. The file is parsed once into the frozen `PipelineConfig` dataclass.

### Top-level sections

```yaml
workspace:
  path: /path/to/runs        # Root directory for named runs (used with --name)
  mathlib_path: submodules/mathlib  # Path to Mathlib checkout (relative to repo root)
  lib_name: Formalization     # Name of the [[lean_lib]] in the generated lakefile

workers:
  agents_per_node: 2          # Number of worker+reviewer pairs per node
  min_agents_per_task: 1      # Minimum agents to race on a single task
  max_agents_per_task: 1      # Maximum agents to race on a single task
  max_concurrent_llm_calls: 2 # Concurrency limit for LLM API calls
  num_repls_per_node: null    # Number of Lean REPL instances per node (null = match agents)
  pick_strategy: biggest_first  # Node selection: biggest_first
  max_review_cycles: 0        # Max review-fix iterations per attempt (0 = no review)

llm:
  model: "Opus 4.6"           # Model name resolved via core.inference.client.lookup_model
                              # "Aristotle" routes to Harmonic's agent (see note below)

book:
  path: books/my_textbook     # Path to book data under autoform/data/
  files: [ch1.md, ch2.md]     # Optional: only copy these files (null = all)
  targets: targets.yaml       # Path to formalization targets file (relative to book data dir)

logging:
  level: INFO                 # Log level (DEBUG, INFO, WARNING, ERROR)
```

### CLI flags

```
python -m autoform.bot.main run \
  --config=path/to/config.yaml \  # Config file (default: autoform/bot/config.yaml)
  --name=my-run \                 # Run name (creates {workspace.path}/{name}/)
  --run_path=/abs/path \          # Explicit run directory (overrides --name)
  --agents_per_node=4 \           # Override workers.agents_per_node
  --nuke \                        # Delete existing workspace before starting
  --fresh \                       # Prune completed tasks and reset traces for a clean restart
  --port=8080 \                   # Fixed port for the registry API
  --test_tasks=5                  # Pre-populate DAG with N simple test tasks
```

### Agent config.yaml fields

Each agent definition in `agents/{role}/config.yaml` supports:

| Field | Description |
|---|---|
| `model` | LLM model name (e.g., `Opus 4.6`, `Haiku 4.5`) |
| `max_turns` | Maximum tool-use turns before the agent is stopped |
| `context_window` | Context window size in tokens |
| `compact_threshold` | Fraction of context window that triggers compaction (0.0-1.0) |
| `tool_timeout_s` | Timeout for individual tool calls in seconds |
| `tools.servers` | List of tool server keys to attach (e.g., `[lsp, lean_repl, filesystem, git]`) |
| `tools.allowlist` | Optional list of specific tool names to expose (restricts the server's full set) |
| `inference.cache.system` | Enable cache breakpoints on system prompt |
| `inference.cache.messages` | Enable cache breakpoints on message history |
| `inference.cache.tools` | Enable cache breakpoints on tool definitions |

## Workspace Structure

A run directory has the following layout after initialization:

```
{run_path}/
  config.yaml              # Snapshotted pipeline config
  dag.json                  # Task DAG state (ItemTracker persistence)
  goals.json                # Goal tracker state (book targets)
  orchestrator_todos.json   # Orchestrator's personal TODO list
  urls.json                 # Service discovery (registry, control plane URLs)
  escalations.jsonl         # Worker escalation log

  code/                     # Lean 4 git repository
    lakefile.toml            # Project config with [[lean_lib]] name
    {LibName}/               # Lean source files (e.g., Formalization/)
    {LibName}.lean           # Root import module (auto-generated)
    .lake/                   # Lake build artifacts
      packages/              # Symlinked, frozen read-only after initial build
    .git/
      worktrees/             # Git worktree metadata

  book/                     # Source files to formalize (read-only)

  skills/
    lean/                   # Lean/Mathlib API reference
    mathlib/                # Mathlib conventions (SKILL.md)
    workflow/               # Process and workflow guidance
    tasks/                  # Per-task skill guides (written by trace analyzer)
      {task_id}/
        guide.md

  traces/                   # Live agent traces (JSON)
    orchestrator.json
    tasks/
      {task_id}/
        {agent_id}.json     # Worker traces
        {reviewer_id}.json  # Reviewer traces
        analyzer.json       # Trace analyzer
    readers/                # Reading agent traces
    merge_batches/          # Merge queue step traces

  reports/
    task_reports/           # Per-task analysis reports (consumed each round)
    merge_reports/          # Per-merge evaluation reports
    eval_reports/           # Periodic full-codebase evaluation reports

  archive/                  # Historical data (append-only)
    traces/                 # Archived traces (full message history)
    reports/
      task_reports/         # Round-stamped report archives
    skills/                 # Archived task skill folders
    dag_{timestamp}.json    # DAG snapshots (on --fresh)
    usage_snapshot_{ts}.json  # Cost/token snapshots

  worktrees/                # Git worktrees for agents
    {run_id}/
      {run_id}-rank{N}-worker-{i}/  # One worktree per agent

  logs/
    pipeline_rank{N}.log    # Per-node log files

  tool-results/             # Ephemeral tool output (deleted on --fresh)
```

The `archive/` directory preserves full trace history (including pre-compaction messages), reports from every round, and skill guides from completed tasks. The `ArchiveTraceStore` appends new messages to archive traces rather than replacing them on compaction, so the visualizer can reconstruct the complete conversation history.

## Formalization Project Structure

The `code/` directory inside each run is a self-contained Lean 4 + Mathlib project. It follows this structure:

```
code/
  lakefile.toml          # Lake project configuration
  lean-toolchain         # Lean compiler version (must match Mathlib)
  <LibName>/             # source directory — all .lean files
    Module1.lean
    SubDir/
      Module2.lean
  <LibName>.lean         # root import file — imports all modules
```

`<LibName>` is set by `lib_name` in the pipeline config (e.g., `Algebraic_Combinatorics`). It determines the source directory name, root import file, and `[[lean_lib]]` entry in `lakefile.toml`.

### targets.yaml

The formalization targets — mathematical statements to formalize. Produced by the statement extraction pipeline or written manually:

```yaml
- name: "Parseval's identity"
  description: "For any f: {-1,1}^n → ℝ, ∑ |f̂(S)|² = 𝔼[f²]"
  kind: theorem
  location: "Section 1.3, Theorem 1.7"
  lean_declaration: BooleanFourier.parseval_identity
  lean_file: BooleanFourier/Plancherel.lean
```

| Field | Required | Description |
|---|---|---|
| `name` | yes | Short title |
| `description` | no | Mathematical content |
| `kind` | no | `definition`, `theorem`, `lemma`, `proposition` |
| `location` | no | Where it appears in the book |
| `lean_declaration` | no | Fully qualified Lean name (filled by eval) |
| `lean_file` | no | Relative path to the `.lean` file (filled by eval) |
