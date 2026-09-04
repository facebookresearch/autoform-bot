---
name: orchestrate
description: >-
  Work through ready nodes in an existing Autoform blueprint by using the
  Autoform CLI for work discovery, claims, and validation. Do not use this
  skill to create the roadmap or install repository infrastructure.
---

# Orchestrate an Autoform formalization

Use the current host agent as the worker. The `autoform` CLI is the sole
orchestration and control interface; do not create a second scheduler, queue,
or provider-specific execution loop.

Resolve the absolute installed plugin root, then follow the invocation contract
in the [CLI reference](../../autoform_cli/README.md#commands) from the Lean
project. Pass the same project selector and Lean root throughout one attempt.

## Select and claim work

Before the first selection, run the structural check and audit validation pair
shown below with the Lean root. Do not dispatch work from a project that fails
either command.

Run `autoform ready <project-or-blueprint> --lean-root <lean-root> --json`.
For a registered multi-project workspace, also pass `--project <id>`. This
command validates the exhaustive source-unit contract and returns only
dispatchable leaf phases whose authored prerequisites are satisfied, plus
structured blocked items and their unmet dependency IDs. If it fails, repair
the roadmap, coverage, or completion evidence with the appropriate skill. Do
not work around it by selecting a Markdown file manually.

Choose one returned item and acquire its durable `article_id` before editing:

```bash
autoform claim acquire <article-id> --blueprint <project-or-blueprint>
```

Use a stable `AUTOFORM_WORKER_ID` for display. Let the CLI derive its
worktree-scoped session identity unless the caller deliberately supplies an
override. In a workspace, pass the same `--project <id>` to the claim command.
If acquisition fails or ownership becomes uncertain, do not edit or commit that
item. Try another item from a fresh `autoform ready` result. Renew the claim
during a long attempt. If the attempt is abandoned without a candidate, release
it. For a verified candidate or handoff, follow the integration rules below.

Separate contributors use separate Git worktrees. Before a full build, acquire
the shared resource with `autoform claim acquire --resource lake-build`; release
it immediately after the build.

## Complete one item

Read the complete roadmap article, every cited source passage, and both kinds
of dependency before editing. Preserve the exact hypotheses, quantifiers,
objects, and conclusion. Search the pinned local Mathlib checkout and existing
project code before introducing helpers. Use the shared Lean LSP and REPL with
the absolute Lean project directory.

Work only on the selected statement or proof phase. Do not modify another
roadmap node or weaken a public statement. A completed result contains no
`sorry`, `admit`, new `axiom`, `unsafe`, `partial`, `native_decide`, unused
hypothesis, or other trust shortcut.

Run a focused Lake build. If the exact declaration compiles, require an
independent Agent Review of every changed statement or proof for source
faithfulness, dependency correctness, and proof integrity. The reviewer does
not edit the candidate. Do not record formalized progress when that review
rejects it.

After review acceptance, update only the selected article with its exact
declaration name and truthful `statement: formalized` or `proof: formalized`
assertion. Then validate that final authored state:

```bash
autoform check <project-or-blueprint> --lean-root <lean-root>
autoform audit <project-or-blueprint> --lean-root <lean-root>
```

For a workspace, pass `--project <id>` to both commands. If final validation
fails, fix the item or remove the new progress assertions before handoff.
Readiness and completion remain derived state and must not be authored.

Commit the verified item in its worktree, then confirm claim ownership again
immediately before integration. Rebase or merge the current shared base and
if that changes the candidate, repeat the focused build, independent Agent
Review, metadata validation, `autoform check`, and `autoform audit`. Keep the
article claim until the verified commit has reached the authorized shared
branch. If the run is not authorized to update that branch, or integration
fails, keep the candidate isolated and report its branch, commit, and claim
state for an explicit handoff rather than making the item appear free.

After integration, release the article claim and rerun `autoform ready` from the
updated shared base. Continue until it returns no ready items or the user stops
the run. Before treating zero ready items as terminal, run the structural check
and audit pair again and inspect every `blocked_items` entry. Report completed
items, integrated commits, commands and Lean checks, released claims, and
explicit blockers.
