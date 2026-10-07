---
name: formalize
description: >-
  Formalize ready leaves from an existing Autoform Markdown roadmap in Lean,
  using native agents, fail-closed claims, and verified Markdown progress.
  Use for proof execution; send missing scope or DAG structure to Roadmap.
---

# Formalize the Markdown roadmap

Treat `blueprint/roadmap/**/*.md` as the sole durable work graph. Resolve the
absolute installed plugin root and run commands through the invocation contract
in the [CLI reference](../../autoform_cli/README.md#commands), from the Lean
project. `<PROJECT>` is the absolute path of the checkout being edited; for a
subagent, that is its own worktree. Start by running `autoform work list
<PROJECT> --lean-root <PROJECT> --json` and inspect a selected leaf with
`autoform work context <NODE> <PROJECT> --lean-root <PROJECT> --json`. The
returned phase is derived from typed dependencies and verified assertions; never
author ready, running, retrying, failed, or blocked scheduler states. If `work
list` refuses because unfinished leaves lack `article_id`, add the IDs planned
by `autoform migrate article-ids <PROJECT>/blueprint --json` to those articles'
frontmatter, validate, and commit that change before claiming anything.

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
project, run `lake exe cache get` there under the `lake-build` claim before its
first build or Lean tool call.

Give every concurrent agent and subagent its own worker ID, such as its host and
worktree name, and pass it as `--worker-id` on every claim command. Never reuse
the lead's, because a board accepts a second acquire from the same owner, and do
not rely on exporting `AUTOFORM_WORKER_ID` once: a later tool call can start
from a profile that sets a shared value. Run claim commands from the project
worktree so every worker uses its `origin` board, or pass the same `--repo`
everywhere when it has none. Claims write refs to that board's remote, which is
outward-facing: make sure the request covers it before the first claim.

Before editing, acquire the exact `claim_target` returned by `work context`. A
failed acquire or renewal means ownership is unproven: stop writing. Leases
expire after 25 minutes, so run `autoform claim renew` about every five minutes
and immediately before integrating; renew, not acquire, is the check that a
claim is still held. Take the `lake-build` resource claim only around a Lake
build: if it is refused, wait and retry rather than abandoning the leaf, renew
it during a long build, and release it as soon as the build ends.

After acquiring the claim, bring the worktree up to date with the shared branch
and reload `work context`. Before editing, require the same `phase`, `blockers`,
`dependencies`, `article_revision`, `open_statements`, `assumes`, and `revision`
as the first read and, for a subagent, the same `phase`, `article_revision`,
`open_statements`, `assumes`, and `revision` it was dispatched with. Unrelated
parallel articles may legitimately change the graph-wide source revision.

Read the complete article, its `blueprint/.implementation-notes/<article_id>.md`
when present, cited sources, dependency articles, and existing Lean target.
Preserve the exact mathematical statement. Work only on the selected phase: do
not modify another article or its Lean declarations, or weaken a public
statement. The one exception is revising a declaration other articles' Lean
uses: start from `autoform work impact` and make only the edits the
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
weaker theorem, unused hypotheses, or an unrelated declaration, except as the
open-statement policy below allows.

`roadmap/README.md` sets the project's policy. Under the default strict policy,
project CI rejects `sorry`: the statement phase writes the declaration and, for
a theorem, the complete proof, which is why `work list` offers the phase only
once the proof prerequisites are proved. Under `open_statements: allowed`, the
statement phase of a theorem writes the faithful statement with a proof body of
exactly `sorry` and records `statement: formalized`, or writes the full proof
and, on acceptance, records both assertions. That `sorry` is the declaration's
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

After the focused build passes, require an independent Agent Review of every
changed statement or proof for source faithfulness, dependency correctness, and
proof integrity. The reviewer does not edit the candidate. Record progress only
when every required rubric passes; report insufficient evidence or any failing
score instead.

On acceptance, update only the claimed article with the exact compiled
declaration and truthful assertions: `statement: formalized`, plus `proof:
formalized` once the proof is complete. The article keeps only frontmatter and
mathematics. Write notes only in the claimed article's
`blueprint/.implementation-notes/<article_id>.md`: Lean names, Mathlib gaps,
prior art, friction, partial progress, and, after a useful failed route,
distilled reusable evidence (the remaining goal, checked lemmas, and next
route), never a transcript or retry counter, and never a secret or machine-local
path. The durable ID keeps the note with an article when its roadmap path moves,
and one file per claimed article avoids cross-worker merge hotspots. Move
still-useful notes already in the article there and drop the rest. Delete the
file once the article is proved; an empty file fails `autoform check`. Never
link the hidden note from an article, because publication omits it. A missing
prerequisite, an incorrect decomposition, a change another article needs, or a
proof recorded without its statement returns to Roadmap instead of silently
changing the DAG.

Run `autoform check <PROJECT>/blueprint --lean-root <PROJECT>` and `autoform
audit <PROJECT>/blueprint --lean-root <PROJECT>`. Resolve every finding this
work introduced on the claimed article, except `lean-target-deprecated` for a
superseded declaration that an expand, migrate, contract revision keeps in the
revised article's `lean:` until it is deleted; report unrelated pre-existing
findings instead of fixing them.

Commit the verified result in its worktree, renew the claim, and rebase onto or
merge the current shared branch. On the result, run the default `lake build`,
confirm that dependency readiness is unchanged, and confirm that the claimed
article differs from its starting `article_revision` only by this worker's
edits; if integration changed the candidate, repeat the review, check, and
audit. For a revision, also re-run `autoform work impact` on the rebuilt result;
if the route's claim set grew, acquire the whole larger set in one command under
the no-hold-and-wait rule and repair the new targets before landing. Keep the
article claim until every checkout on the claim board can see
the verified commit: on the shared branch and, for an `origin` board, pushed,
since other clones read their frontier from the remote. Without authority to
update or push that branch, or when integration fails, keep the claim and report
the branch, commit, claim, and worker ID for handoff instead of making the leaf
look free. The integrator renews, integrates, and releases a handed-off claim
with `--worker-id` set to the reported ID; if the lease has lapsed, it acquires
the claim under its own ID and repeats the reload gate before integrating.
Release the claim once the result is visible that way, or when abandoning the
leaf without a candidate.

Re-read the work frontier from the updated shared branch and repeat while
independent ready leaves and authorized capacity remain. Stop when the frontier
is empty or every remaining attempt has a concrete mathematical or ownership
blocker. Report changed articles and Lean files, integrated commits, claims
released or handed off, checks and reviews run, and exact remaining goals.
