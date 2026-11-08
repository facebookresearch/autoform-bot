---
name: formalize
description: >-
  Formalize ready leaves from an existing Autoform Markdown roadmap in Lean,
  using native agents and verified Markdown progress.
  Use for proof execution; send missing scope or DAG structure to Roadmap.
---

# Formalize the Markdown roadmap

There is no `autoform` on `PATH`. Run it from the Lean project as
`uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform`, where the root is the
absolute plugin directory holding `skills/` and `pyproject.toml`; give a
subagent that prefix, with the root filled in.

Treat `blueprint/roadmap/**/*.md` as the sole durable work graph; the [CLI
reference](../../autoform_cli/README.md#work) documents the `work` commands.
`<PROJECT>` is the absolute path of the checkout being edited (for a subagent,
its own worktree). First run `autoform work list <PROJECT> --json`, whose items
hold every field a dispatch needs. Whoever works a leaf, the lead too, follows
[leaf.md](references/leaf.md). The phase is derived; never author ready,
running, retrying, failed, or blocked scheduler states. If a command refuses for
a missing `article_id`, make the repair its message names, validate, and commit
first.

For a direct formalization request, use a compatible native persistent Goal when
available and repeat frontier passes as below. When a run starts or resumes, and
at each later re-read of the frontier, the lead first reads and decides the
reviews on the project's pull requests as the [review
steps](references/pull-request-reviews.md) say. Native subagents may work
independent leaves in separate Git worktrees; no custom scheduler or provider
adapter is part of the protocol. The lead's branch is the shared branch results
integrate into; the lead also works its own leaves in worktrees, so nothing
reaches that branch before its default build passes. Before dispatching, commit
the roadmap state the frontier was read from and base each worktree on that
commit, not on the remote's default branch (Claude Code's agent isolation does
so only with `worktree.baseRef: head`). Give each subagent the lead's checkout
path, its leaf's `work list` item, and the path
`<AUTOFORM_PLUGIN_ROOT>/skills/formalize/references/leaf.md`. A new worktree has
no `.lake`: in a Mathlib project, run `lake exe cache get` in the lead's
checkout before creating worktrees, rerunning it once if it fails. If the rerun
fails too, ask the user before creating worktrees. Then the lead runs no Lake
command in its own checkout while workers may start. Builds take no claim:
dispatch no more workers at once than the machine's memory can build for. If the
lakefile lacks the artifact cache `project new` writes, Setup offers to add it.

Use claims only when the user asks for them or another session already does.
Delete a leaf's `work/<article_id>` branch once its work lands or the leaf is
abandoned without a candidate; until then whoever continues the leaf uses it,
and a gone worker's is the lead's. A worker holds its article's `claim_target`
and the `claim_targets` the lead confirmed until its work lands or it abandons
the leaf without a candidate. To edit beyond them (an import edit, a revision or
its grown `claim_targets`, or an article its import edit broke), a subagent
reports the new targets and waits until the lead confirms no other worker holds
them; one that cannot wait commits, stops, and reports, and the lead answers
when it dispatches the leaf again from that commit. The lead checks an import
edit's `affected_claim_targets` when confirming and before the edit lands; one
another worker holds is an active conflict. If workers would wait on each other,
the lead has one of them give up what it holds. Before choosing none, run
`autoform claim list` once, with any `--worker-id`: an unexpired claim means
another session uses them. If the command fails, as it does without an `origin`,
tell the user and go on without claims; after `error: timeout:`, `error:
transport:` or `error: contention:` the board could not be read, so run it once
more, then ask the user before going on without them. A claim excludes only
workers that also claim, and `work list` does not read the board, so on such a
project every worker claims: tell each subagent so, and follow the [claim
rules](references/claims.md) at each step.

### Import edits

Lean imports are allowed; never pre-block a proof for needing one. Before an
effective module header or import change, run `autoform work import-impact`,
rerunning it with `--module-scope import-closure` when the default scope cannot
build or load every root library; report a failure that remains. With claims,
follow the [import claims](references/claims.md#import-edits) from then on;
otherwise send the lead the report's `claim_targets` and
`affected_claim_targets` and wait for its answer. Unless the user has paused
skeleton evidence, capture baseline skeleton evidence for the report's currently
built, named affected articles and, when the edit removes an import, for the
articles of modules only that import kept in the build. The affected articles
are not a lock: do not acquire or edit them because they are listed, and treat
unresolved declarations as unknown validation scope. After rebuilding, repeat
any baseline skeleton evidence and use Agent Review to judge semantic changes;
repair each article that needs changes under the review gates of the [revision
contract](../../autoform_cli/reference/claims-and-revisions.md#revision-contract).
Stop for an active conflict or failed validation, not for a blanket import rule;
disclosure alone is neither. With or without claims, a `module_generated` root
follows [its rules](references/claims.md#generated-import-roots).

### Integrate a result

Commit the verified result in its worktree and rebase onto or merge the current
shared branch. On the result, run the default `lake build` and `autoform audit`,
delete every note the audit now reports as stale in a follow-up commit, and
confirm that dependency readiness is unchanged and the selected article differs
from its starting `article_revision` only by this worker's edits; if integration
changed the candidate, repeat leaf.md's review, check, and audit. With
`<PROJECT>/blueprint/.autoform-review`, then run `autoform review check` with
`--lean-root`, which needs that build, and report its findings for every article
they name. For an import edit, re-run `autoform work import-impact` on the
result; if integration brought in a change to an article it lists as affected,
the candidate changed too: capture a fresh baseline of that article on the
shared branch. In a run without claims, send the lead that report's
`affected_claim_targets` and land the edit only once the lead confirms no other
worker holds them. Without authority to update or push the shared branch,
without the lead's answer, or when integration fails, report the branch and
commit for handoff. When the run uses claims, follow the [claim
rules](references/claims.md#integration-and-recovery) for the claim set. Habits,
none required: land as a fast-forward from the commit the checks ran on,
rebasing and rechecking if the shared branch moved; then take the next leaf,
telling the lead: one the landing unlocked, else one in its container, else any.

Once a worker has reported, debrief included, and its result is integrated or
handed off, the lead removes its worktree with `git worktree remove` (never
`--force`), unless, after integrating its result, that worker takes another leaf
there; first make sure a branch points at any handed-off commit. Autoform never
clears Lake's shared artifact cache, so tell the user if disk runs low. After
the run, `autoform project worktrees` lists leftover worktrees; pass it
`--remove` only when the user asks and every session using this clone has
stopped its workers. Re-read the work frontier from the updated shared branch
and repeat while independent ready leaves or accepted review findings and
authorized capacity remain. Stop when the frontier is empty and no accepted
review finding awaits repair, or every remaining attempt has a concrete
mathematical or ownership blocker. With `AUTOFORM_SUBAGENT_DEBRIEF=1` the lead
then [raises copied code](references/auto-abstraction.md). Then the lead follows
[Debrief Orchestrator](../debrief-orchestrator/SKILL.md) with `--skill
formalize`. Report changed articles and Lean files, integrated commits, branches
and commits handed off, pull requests opened, any claims released, Human Review
handoffs, checks and reviews run, and exact remaining goals.
