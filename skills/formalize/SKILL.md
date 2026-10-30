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
subagent that prefix.

Treat `blueprint/roadmap/**/*.md` as the sole durable work graph; the [CLI
reference](../../autoform_cli/README.md#commands) documents the commands.
`<PROJECT>` is the absolute path of the checkout being edited (for a subagent,
its own worktree). First run `autoform work list <PROJECT> --lean-root <PROJECT>
--json`, then inspect a selected leaf with `autoform work context <NODE>
<PROJECT> --lean-root <PROJECT> --json`. The phase is derived; never author
ready, running, retrying, failed, or blocked scheduler states. If `work list`
refuses because unfinished leaves lack `article_id`, run `autoform migrate
article-ids <PROJECT>/blueprint --write`, validate, and commit before starting
work. If a command refuses because an implementation note names no roadmap
article, any worker may repair the note as the [format
reference](../../autoform_cli/README.md#articles-and-containment) says, and
commit.

For a direct formalization request, use a compatible native persistent Goal when
available and complete one useful frontier pass. When a run starts or resumes,
first read the review threads on the pull requests this project's runs opened,
as their reports or the user name them, and decide each finding: fix it under
the rules below, note why it is resolved or moot, or ask the user; reply on a
pull request only if the request covers posting. Native subagents may work
independent leaves in separate Git worktrees; no custom scheduler or provider
adapter is part of the protocol. A subagent may keep its worktree for several
related leaves, each once it is ready, integrating one before taking the next;
the lead dispatches each with fields from the frontier after that integration.
The lead's branch is the shared branch results integrate into; the lead also
works its own leaves in worktrees, so nothing reaches that branch before its
default build passes. Before dispatching, commit the roadmap state the frontier
was read from and base each worktree on that commit, not on the remote's default
branch (Claude Code's agent isolation does so only with `worktree.baseRef:
head`). Give each subagent the `claim_target`, `phase`, `article_revision`,
`open_statements`, `assumes`, and `revision` it is dispatched for. A new
worktree has no `.lake`: in a Mathlib project, run `lake exe cache get` in the
lead's checkout before creating worktrees, rerunning it once if it fails, then
in each worktree before its first build or Lean tool call. If the rerun fails
too, ask the user before creating worktrees, since each would then fetch or
build Mathlib itself. Builds take no claim: dispatch no more workers at once
than the machine's memory can build for. If the lakefile lacks the artifact
cache `project new` writes, Setup offers to add it.

Claims are optional. Within one run no one needs them: the lead keeps workers,
itself included, from holding the same target at once. A worker holds its
article's `claim_target` and the `claim_targets` the lead confirmed until its
work lands or it abandons the leaf without a candidate; a handoff keeps them. To
edit beyond them (an import edit, a revision or its grown `claim_targets`, or an
article its import edit broke), a subagent reports the new targets and waits
until the lead confirms no other worker holds them; one that cannot wait
commits, stops, and reports, and the lead answers when it dispatches the leaf
again from that commit. The lead checks an import edit's
`affected_claim_targets` when confirming and before the edit lands; one another
worker holds is an active conflict. If workers would wait on each other, the
lead has one of them give up what it holds.

Use claims only when other people or sessions may work on the project
concurrently, or the user asks for them. Before choosing none, run `autoform
claim list` once, with any `--worker-id`: an unexpired claim means another
session uses them. If the command fails, as it does without an `origin`, tell
the user and go on without claims; after `error: timeout:`, `error: transport:`
or `error: contention:` the board exists but could not be read, so run it once
more, then ask the user before going on without them. A claim excludes only
workers that also claim, and `work list` does not read the board, so on such a
project every worker claims: tell each subagent so, and follow the [claim
rules](references/claims.md) at each step.

### Import edits

Lean imports are allowed; do not pre-block a proof because it needs one. Before
an effective module header or import change, run `autoform work import-impact`,
rerunning it with `--module-scope import-closure` when the default scope cannot
build or load every root library. When the run uses claims, follow the [import
claims](references/claims.md#import-edits) from then on; otherwise send the lead
the report's `claim_targets` and `affected_claim_targets` and wait for its
answer. Capture baseline skeleton evidence for the report's currently
built, named affected articles and, when the edit removes an import, for the
articles of modules only that import kept in the build. The affected articles
are not a lock: do not acquire or edit them merely because they are listed, and
treat reported unresolved declarations as unknown validation scope, not proof
they are unaffected. After rebuilding, repeat the skeleton evidence and use
Agent Review to judge semantic changes; repair each article that actually needs
changes under the review gates of the [revision
contract](../../autoform_cli/reference/claims-and-revisions.md#revision-contract).
Stop for an active conflict (an affected target another worker holds, or another
owner's live claim on one) or failed validation, not for a blanket import rule;
disclosure alone is neither.

Bring the worktree up to date with the shared branch (after [acquiring the
claim](references/claims.md#acquire-and-renew), when the run uses claims) and
reload `work context`. Before editing, require the same `phase`, `blockers`,
`dependencies`, `article_revision`, `open_statements`, `assumes`, and `revision`
as the first read and, for a subagent, the same `phase`, `article_revision`,
`open_statements`, `assumes`, and `revision` it was dispatched with. Unrelated
parallel articles may change the graph-wide source revision.

Read the complete article, its `blueprint/.implementation-notes/<article_id>.md`
when present, cited sources, dependency articles, and existing Lean target.
Preserve the exact mathematical statement. Work only on the selected phase: do
not modify another article or its Lean declarations, or weaken a public
statement. The exceptions are deleting stale and repairing orphaned
implementation notes, repairing another article's Lean that an [import
edit](#import-edits) breaks, and revising a declaration other articles' Lean
uses: start from `autoform work impact` and make only the edits the [revision
contract](../../autoform_cli/reference/claims-and-revisions.md#revision-contract)
requires, under the claims it requires. A work item flagged `revision`, whose
article records `statement: retracted`, is such a revision; restating it
replaces `statement: retracted` with `statement: formalized`. Never add a new
use of a deprecated declaration. Search the pinned Mathlib checkout before
adding helpers, and use the shared Lean LSP and REPL with `<PROJECT>` as the
project path. Finish with the focused Lake target, declaring the result in a
module the library root imports or the lakefile's globs cover: CI compiles and
audits only the default build. Check the recorded declaration with `#print
axioms`; do not accept `sorry`, new axioms, unsafe shortcuts, a weaker theorem,
unused hypotheses (see below), or an unrelated declaration, except as the
open-statement policy below allows.

Before stating an article's result and before adding a helper, run `autoform
search` over the blueprint of `<PROJECT>` and its usable shared libraries for
it, following the [search steps](references/library-search.md#search-steps),
which also cover library hits and refusals that name a library.

- Searching for an article's own result finds the article itself; it is not a
  duplicate.
- Retry a refusal that asks for it. Any other refusal that names no library
  blocks the leaf: add no helper and report it as the blocker.
- Use the declaration of a blueprint hit that states the helper only when the
  selected article's dependencies reach that hit's article, and as the
  open-statement policy below allows. A hit they do not reach is a missing
  prerequisite; never restate a hit as a helper.

`roadmap/README.md` sets the project's policy, as [open
statements](../../autoform_cli/reference/open-statements.md#open-statements)
explains. Under the default strict policy, project CI rejects `sorry`: the
statement phase writes the declaration and, for a theorem, the complete proof.
Under `open_statements: allowed`, the statement phase of a theorem either writes
the faithful statement as a `theorem` whose whole body is `sorry` (not
Batteries' `theorem_wanted` or `proof_wanted`) and records `statement:
formalized`, or writes the full proof and, on acceptance, records both
assertions. That `sorry` is never part of the type, a helper, a definition, or a
`where` clause, nor one case of a recursive proof. A definition is never left
open. In both policies the proof phase completes the proof of an already
recorded statement without changing that statement.

Under the open policy a proof may use the open statements its Markdown
dependencies reach; `autoform work assumptions --json` lists the exact
`allowed_open_declarations`, and the article then shows as conditionally proved;
`#print axioms` lists the inherited `sorryAx` without saying from where.
Before landing, record `proof: formalized` in the worktree (until then the audit
treats the article as open), reproduce the CI audit as the open statements
reference shows, and require a `conditional` or `sorry-free` line for each
recorded declaration and a passing summary. An open statement the Markdown does
not declare as a dependency fails CI: send the missing dependency to Roadmap or
stop using it. Never describe a conditional proof as complete, fully proved, or
sorry-free.

Treat Lean's `unusedVariables` and `unusedSectionVars` warnings on the article's
theorems as findings. Once the proof is complete and its focused build passes,
also run Batteries' `#lint only unusedArguments in <Module>` after
`import <Module>`, where `<Module>` declares them: without `in` it lints only the
current file, and its releases for Lean v4.30.0 and v4.31.0 check no theorem, so
a clean run there is not evidence. Resolve each finding as the [unused-hypothesis
rules](references/unused-hypotheses.md) say.

After the focused build passes, require an independent Agent Review of every
changed statement or proof for source faithfulness, dependency correctness, and
proof integrity. The reviewer does not edit the candidate. Record progress only
when every required rubric passes; report insufficient evidence or any failing
score instead.

On acceptance, update only the selected article with the exact compiled
declaration and truthful assertions: `statement: formalized`, plus `proof:
formalized` once the proof is complete. The article keeps only frontmatter and
mathematics; move its still-useful notes to its
`blueprint/.implementation-notes/<article_id>.md`, the only place to write
notes: Lean names, Mathlib gaps, prior art, friction, partial progress, and,
after a useful failed route, the remaining goal, checked lemmas, and next route,
never a transcript, retry counter, secret, or machine-local path. Delete the
file once the article is proved; an empty file fails `autoform check`. Never
link the note from an article. A missing prerequisite, an incorrect
decomposition, a change another article needs beyond an import repair, or a
proof recorded without its statement returns to Roadmap instead of silently
changing the DAG. So does a `check` warning that a shared library declaration's
docstring, or its module's, cites the blueprint's sources, even when deleting a
stale note brings it back: report it, and neither keep the note nor edit a
source page for it.

Run `autoform check <PROJECT>/blueprint --lean-root <PROJECT>` and `autoform
audit <PROJECT>/blueprint --lean-root <PROJECT>`. Resolve every finding this
work introduced on the selected article, except `lean-target-deprecated` for a
superseded declaration that an expand, migrate, contract revision keeps in the
revised article's `lean:` until it is deleted. Delete every note the audit
reports as `stale-implementation-note`, including another article's, such as a
container's this proof completed or one an earlier commit left behind; the audit
fails until they are all gone. Report other pre-existing findings instead of
fixing them. A `catalog-unbounded-limit` or `catalog-unscoped-limit` warning
describes existing Lean code and never fails the audit: report it, and change
that code only when the user asks, rebuilding before anything is recorded.

If `<PROJECT>/blueprint/.autoform-review` exists, project CI also runs `autoform
review check`, which fails on a formalized statement until Human Review has
filed its read-backs and approval. Never write read-backs or `review_approved`
yourself. Hand each newly formalized statement, and each article whose
read-backs or approval the result made stale (an import edit's repairs
included), to Human Review; until that review lands, the gate stays red wherever
the result is integrated.

Commit the verified result in its worktree and rebase onto or merge the current
shared branch. On the result, run the default `lake build` and `autoform audit`,
delete every note the audit now reports as stale in a follow-up commit, and
confirm that dependency readiness is unchanged and that the selected article
differs from its starting `article_revision` only by this worker's edits; if
integration changed the candidate, repeat the review, check, and audit. With the
review marker, then run `autoform review check` with `--lean-root`, which needs
that build, and report its findings for every article they name. For an import
edit, re-run `autoform work import-impact` on the result; if integration brought
in a change to an article it lists as affected, the candidate changed too:
capture a fresh baseline of that article on the shared branch. In a run without
claims, send the lead that report's `affected_claim_targets` and land the edit
only once the lead confirms that no other worker holds them. For a revision,
also re-run `autoform work impact` on the rebuilt result and repair any new
targets before landing. Without authority to update or push the shared branch,
without the lead's answer, or when integration fails, report the branch and
commit for handoff. When the run uses claims, the [claim
rules](references/claims.md#integration-and-handoff) say how to renew, keep,
hand off, and release the claim set.

When `AUTOFORM_SUBAGENT_DEBRIEF=1`, the agent that worked an attempt follows the
[subagent debrief steps](references/debrief-subagent.md) before reporting.

Once a worker has reported, debrief included, and its result is integrated or
handed off (branch any handed-off commit first), the lead removes its worktree
with `git worktree remove`, never `--force`, unless it takes another leaf there.
Autoform never clears Lake's shared artifact cache: tell the user if disk runs
low. Use `autoform project worktrees` and its `--remove` only after the run,
when the user asks and no session's workers on this clone run. Re-read
the work frontier from the updated shared branch and repeat while independent
ready leaves and authorized capacity remain. Stop when the frontier is empty or
every remaining attempt has a concrete mathematical or ownership blocker. Then
the lead follows [Debrief Orchestrator](../debrief-orchestrator/SKILL.md) with
`--skill formalize`. Report changed articles and Lean files, integrated commits,
branches and commits handed off, pull requests opened, any claims released or
handed off, checks and reviews run, and exact remaining goals.
