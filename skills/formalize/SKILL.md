---
name: formalize
description: >-
  Formalize ready leaves from an existing Autoform Markdown roadmap in Lean,
  using native agents and verified Markdown progress.
  Use for proof execution; send missing scope or DAG structure to Roadmap.
---

# Formalize the Markdown roadmap

There is no `autoform` on `PATH` in a Lean project. Run every `autoform`
command, from the Lean project, as
`uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform` followed by its arguments,
where `<AUTOFORM_PLUGIN_ROOT>` is the absolute plugin root: the directory that
holds this skill's `skills/` folder, along with `pyproject.toml` and
`autoform_cli/`. Give a subagent that root and this prefix with its task.

Treat `blueprint/roadmap/**/*.md` as the sole durable work graph. The
[CLI reference](../../autoform_cli/README.md#commands) documents the commands.
`<PROJECT>` is the absolute path of the checkout being edited; for a
subagent, that is its own worktree. Start by running `autoform work list
<PROJECT> --lean-root <PROJECT> --json` and inspect a selected leaf with
`autoform work context <NODE> <PROJECT> --lean-root <PROJECT> --json`. The
returned phase is derived from typed dependencies and verified assertions; never
author ready, running, retrying, failed, or blocked scheduler states. If `work
list` refuses because unfinished leaves lack `article_id`, run `autoform
migrate article-ids <PROJECT>/blueprint --write`, validate, and commit that
change before starting work. If a command refuses because an
implementation note names no roadmap article, find out why. When an article
that still exists lost its `article_id` line, restore the line. When the
article was deleted or merged, often by a parallel branch that Git merged
without a conflict, move anything still useful into the surviving article's
note, delete the orphan, and commit. Any worker may make this repair.

For a direct formalization request, use a compatible native persistent Goal
when available and complete one useful frontier pass. Native subagents may work
independent leaves in separate Git worktrees, but no custom scheduler or
provider adapter is part of the protocol. The lead's branch is the shared branch
that results integrate into; a leaf the lead takes itself is also worked in a
worktree, so nothing reaches that branch before its default build passes.
Before dispatching, commit the roadmap state the frontier was read from and base
each worktree on that commit, not on the remote's default branch (Claude Code's
agent isolation does so only with `worktree.baseRef: head`). Give each subagent
the `claim_target`, `phase`, `article_revision`, `open_statements`, `assumes`,
and `revision` it is dispatched for. A new worktree has no `.lake`: in a Mathlib
project, run `lake exe cache get` once in the lead's checkout before creating
worktrees, rerunning it there if it fails, then in each worktree before its
first build or Lean tool call, so the worktrees unpack from a full cache instead
of downloading into it together. If the rerun fails too, ask the user before
creating worktrees, since each would then fetch or build Mathlib on its own.
Builds take no claim, so workers on one machine may build at the same time:
dispatch no more at once than its memory can build for.

Claims are optional. Within one run the lead keeps its workers apart, so the run
needs none: no two workers, the lead included, hold the same target at once. A
worker holds its article's `claim_target` and the `claim_targets` the lead
confirmed until its work lands or it abandons the leaf without a candidate, so a
handoff keeps them. A subagent that must edit beyond what it holds, for an
import edit, a revision, a revision's grown `claim_targets`, or an article its
import edit broke, reports the new targets to the lead before editing, and goes
ahead only once the lead confirms that no other worker holds them. A subagent
that cannot wait for the lead's answer commits its work, stops, and reports
instead; the lead answers when it dispatches the leaf again from that commit. A
worker does not hold its import edit's `affected_claim_targets`, but the lead
checks them when it confirms the edit's new targets and again before the edit
lands; one that another worker holds is an active conflict. If workers would
wait on each other, the lead has one of them give up what it holds.

Use claims only when other people or sessions may work on the same project at
the same time, or when the user asks for them. Before choosing none, run
`autoform claim list` once, with any `--worker-id`: an unexpired claim on the
board means another session uses them, and if the command fails, as it does in a
checkout without an `origin`, tell the user and go on without claims. A claim
excludes only workers that also claim, and `work list` does not read the board,
so on such a project every worker uses them. When the run uses claims, tell each
subagent so, and follow the [claim rules](references/claims.md) at each step.

### Import edits

Lean imports are allowed; do not pre-block a proof because it needs one. Before
an effective module header or import change, run `autoform work import-impact`
as documented in the CLI reference. Only when the build the default scope needs
is infeasible, pass `--module-scope import-closure`, which needs only the edited
module's importers and what they import built. It gives the exact affected
modules but only partial affected articles and never article locality, so treat
every article owning a declaration in an affected module as possibly affected,
and rerun the default scope once its build is feasible. When the run uses
claims, follow the [import claims](references/claims.md#import-edits) from then
on; otherwise send the lead the report's `claim_targets` and
`affected_claim_targets` and wait for its answer, as above. Capture baseline skeleton evidence for the report's currently
built, named affected articles. The report covers added imports, so when an edit
removes an import, also baseline the articles of modules only that import kept
in the build. The report's affected articles are not a lock. Do not acquire or
edit them merely because they are listed; treat reported unresolved declarations
as unknown validation scope, not proof they are unaffected. Disclosure alone is
not a conflict or failed validation. After rebuilding, repeat the skeleton
evidence and use Agent Review to judge semantic changes. If any article actually
needs changes, then repair each under the review gates of the [revision
contract](../../autoform_cli/README.md#revision-contract). Stop for an active
conflict (an affected target another worker holds, or another owner's live claim
on one) or failed validation, not for a blanket import rule.

Bring the worktree up to date with the shared branch (after [acquiring the
claim](references/claims.md#acquire-and-renew), when the run uses claims) and
reload `work context`. Before editing, require the same `phase`, `blockers`,
`dependencies`, `article_revision`, `open_statements`, `assumes`, and `revision`
as the first read and, for a subagent, the same `phase`, `article_revision`,
`open_statements`, `assumes`, and `revision` it was dispatched with. Unrelated
parallel articles may legitimately change the graph-wide source revision.

Read the complete article, its `blueprint/.implementation-notes/<article_id>.md`
when present, cited sources, dependency articles, and existing Lean target.
Preserve the exact mathematical statement. Work only on the selected phase: do
not modify another article or its Lean declarations, or weaken a public
statement. The exceptions are deleting stale implementation notes and repairing
orphaned ones, as this skill describes, repairing another article's Lean that an
[import edit](#import-edits) breaks, and revising a declaration other
articles' Lean uses: start from
`autoform work impact` and make only the edits the
[revision contract](../../autoform_cli/README.md#revision-contract) requires,
under the claims it requires. A work item flagged `revision`, whose article
records `statement: retracted`, is such a revision; restating it replaces
`statement: retracted` with `statement: formalized`. Never add a new use of a
deprecated declaration. Search the pinned Mathlib checkout before adding
helpers, and use the shared Lean LSP and REPL with `<PROJECT>` as the project
path. Finish with the focused Lake target. Declare the result in a module the
library root imports, or that the lakefile's globs cover, because the default
build is the only one CI compiles and audits. Check the recorded declaration
with `#print axioms`; do not accept `sorry`, new axioms, unsafe shortcuts, a
weaker theorem, unused hypotheses (see below), or an unrelated declaration,
except as the open-statement policy below allows.

Before stating an article's result and before adding a helper, also run
`autoform search` over the blueprint of `<PROJECT>` and its usable shared
libraries for it, as the
[search contract](../../autoform_cli/README.md#search-contract) describes; the
[library search rules](references/library-search.md) say how to use a library
hit. Read each hit's statement; the search before stating an article's result
finds that article itself, which is not a duplicate. Matching is literal, so no
article states the helper only when retries with fewer words, and under other
usual names, also show none. A refusal (exit 2) is not an empty result. A
refusal that names a library: rerun without that library and continue, and
report that library as not searched, so its answer is unknown. Any other
refusal: retry one that asks for it, otherwise add no helper and report it as
the leaf's blocker. Use the declaration of a blueprint hit that states the
helper only when the selected article's dependencies reach that hit's article,
and as the open-statement policy below allows. A hit they do not reach is a
missing prerequisite; never restate a hit as a helper. A library hit needed as
a helper is used directly; one that states the article's own result is handled
as the library search rules say.

`roadmap/README.md` sets the project's policy. Under the default strict policy,
project CI rejects `sorry`: the statement phase writes the declaration and, for
a theorem, the complete proof, which is why `work list` offers the phase only
once the proof prerequisites are proved. Under `open_statements: allowed`, the
statement phase of a theorem writes the faithful statement with a proof body of
exactly `sorry` and records `statement: formalized`, or writes the full proof
and, on acceptance, records both assertions. Write such a statement as a
`theorem` with a `sorry` proof, not with Batteries' `theorem_wanted` or
`proof_wanted`: a dependent's proof can cite the theorem, but not the private
placeholder a wanted command declares. That `sorry` is the declaration's
whole body: never part of its type, a helper, a definition, or a `where` clause,
and never one case of a recursive proof, which Lean can compile into
auxiliaries such as `_f` that CI rejects. A definition is never left open: its
body is its proof, so `work list` offers its statement phase only once its
proof prerequisites are stated. In both policies the proof phase completes the
proof of an already recorded statement without changing that statement.

Under the open policy a proof may use the open statements its Markdown
dependencies reach: each open dependency with whatever its statement
prerequisites reach, and everything a proved dependency reaches, but not an
open dependency's proof prerequisites. `autoform work assumptions --json` lists
the exact `allowed_open_declarations`. The article then shows as conditionally
proved, and `#print axioms` lists the `sorryAx` it inherits without saying from
where. Before landing, record `proof: formalized` in the worktree (until then
the audit treats the article as open), reproduce the CI audit as the [open
statements reference](../../autoform_cli/README.md#open-statements) shows, and
require a `conditional` or `sorry-free` line for each recorded declaration and
a passing summary. An open statement the Markdown does not declare as a
dependency fails CI: send the missing dependency to Roadmap or stop using it.
Never describe a conditional proof as complete, fully proved, or sorry-free.

Treat Lean's `unusedVariables` and `unusedSectionVars` warnings on the article's
theorems as findings. Once the proof is complete and its focused build passes,
also run Batteries' linter: `#lint only unusedArguments in <Module>` after
`import <Module>`, where `<Module>` declares the article's theorems, and act
only on its findings for those theorems. From its release for Lean v4.32.0 it
does not report an argument whose name starts with `_` or any unnamed argument
other than an instance, such as the antecedent of `p → q` or the hypothesis in
`∀ n > 0`; its releases for Lean v4.30.0 and v4.31.0 check no theorem at all.
A finding means the proof never uses that argument.
If the statement assumes something
the source derives, such as a supremum the source obtains from boundedness,
restate it to conclude what the source concludes. If a section `variable`
brings it in, add `omit [C] in` for an instance or `omit h in` for an
`include h`. If the source states the hypothesis and Lean does not need it,
such as `y n ≠ 0` where `x / 0 = 0`, keep it. If the theorem genuinely holds
without it, such as a limsup result that holds in the extended reals, keep the
source's statement for the article and prove the general one as a separate
lemma that it follows from. Name a hypothesis you keep with a leading `_` and
add one sentence to the article giving the reason in mathematical or source
terms, so a later reviewer sees the decision.

After the focused build passes, require an independent Agent Review of every
changed statement or proof for source faithfulness, dependency correctness, and
proof integrity. The reviewer does not edit the candidate. Record progress only
when every required rubric passes; report insufficient evidence or any failing
score instead.

On acceptance, update only the selected article with the exact compiled
declaration and truthful assertions: `statement: formalized`, plus `proof:
formalized` once the proof is complete. The article keeps only frontmatter and
mathematics. Write notes only in the selected article's
`blueprint/.implementation-notes/<article_id>.md`: Lean names, Mathlib gaps,
prior art, friction, partial progress, and, after a useful failed route,
distilled reusable evidence (the remaining goal, checked lemmas, and next
route), never a transcript or retry counter, and never a secret or machine-local
path. The durable ID keeps the note with an article when its roadmap path moves,
and one file per article avoids cross-worker merge hotspots. Move still-useful
notes already in the article there and drop the rest. Delete the file once the
article is proved; an empty file fails `autoform check`. Never link the hidden
note from an article, because publication omits it. A missing prerequisite, an
incorrect decomposition, a change another article needs beyond an import repair,
or a proof recorded without its statement returns to Roadmap instead of silently
changing the DAG. So does a `check` warning that a shared library declaration's
docstring, or its module's, cites the blueprint's sources, even when deleting a
stale note brings it back: report it, and neither keep the note nor edit a
source page for it.

Run `autoform check <PROJECT>/blueprint --lean-root <PROJECT>` and `autoform
audit <PROJECT>/blueprint --lean-root <PROJECT>`. Resolve every finding this
work introduced on the selected article, except `lean-target-deprecated` for a
superseded declaration that an expand, migrate, contract revision keeps in the
revised article's `lean:` until it is deleted. Delete every note the audit
reports as `stale-implementation-note`, including a container's note that this
proof completed or that an earlier commit left behind, even though it belongs
to another article; the audit fails until they are all gone. Report other
pre-existing findings instead of fixing them. A `catalog-unbounded-limit` or
`catalog-unscoped-limit` warning describes existing Lean code and never fails
the audit: report it, and change that code only when the user asks, rebuilding
before anything is recorded.

Commit the verified result in its worktree and rebase onto or merge the current
shared branch. On the result, run the default `lake build` and `autoform audit`,
delete every note the audit now reports as stale in a follow-up commit, confirm
that dependency readiness is unchanged, and confirm that the selected article
differs from its starting `article_revision` only by this worker's edits; if
integration changed the candidate, repeat the review, check, and audit. For an
import edit, also re-run `autoform work import-impact` on the result; if
integration brought in a change to an article it lists as affected, that changed
the candidate too, so capture a fresh baseline of that article on the shared
branch. In a run without claims, send the lead that report's
`affected_claim_targets` and land the edit only once the lead confirms that no
other worker holds them. For a revision, also re-run `autoform work impact` on
the rebuilt result and repair any new targets before landing. Without authority
to update or push the shared branch, without the lead's answer, or when
integration fails, report the branch and commit for handoff. When the run uses
claims, the [claim rules](references/claims.md#integration-and-handoff) say how
to renew, keep, hand off, and release the claim set.

When `AUTOFORM_SUBAGENT_DEBRIEF=1`, the agent that worked an attempt then
follows the [subagent debrief steps](references/debrief-subagent.md) before
reporting.

Re-read the work frontier from the updated shared branch and repeat while
independent ready leaves and authorized capacity remain. Stop when the frontier
is empty or every remaining attempt has a concrete mathematical or ownership
blocker. Then the lead follows
[Debrief Orchestrator](../debrief-orchestrator/SKILL.md) with `--skill
formalize`. Report changed articles and Lean files, integrated commits, branches
and commits handed off, any claims released or handed off, checks and reviews
run, and exact remaining goals.
