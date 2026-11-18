# Work one leaf

`<PROJECT>` is the absolute path of your worktree. The lead gives you `<LEAD>`
(its checkout of the shared branch results integrate into) and your `work list`
item, whose `article_path` is `<ARTICLE_PATH>`, and says if the run uses claims:
then follow the [claim rules](claims.md) at each step. Run `autoform` from the
Lean project as `uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform`, the prefix
the lead gives you; give a subagent that prefix. Never author ready, running,
retrying, failed, or blocked scheduler states. Record facts only with `autoform
links set <PROJECT>/blueprint <PROJECT>/<ARTICLE_PATH> KEY=VALUE` or `--unset
KEY`, and report an article it says to edit by hand; if a refusal names leaves
missing an `article_id`, run `links set` on each, validate, and commit first.

1. Within one run, start with `git switch -c work/<article_id>`; a refusal means
   the leaf is taken, unless you continue it: then use that branch. Delete the
   branch once its work lands or the leaf is abandoned without a candidate.
2. Before any build or Lean tool call, copy, never link, the lead's packages:
   `mkdir -p .lake && cp -Rc <LEAD>/.lake/packages .lake/` (`cp -R
   --reflink=auto` on Linux), then, in a Mathlib project, `lake exe cache get`.
3. Inspect your leaf with `autoform work context <NODE> <PROJECT> --json` and,
   before your first edit, save the output of `autoform audit
   <PROJECT>/blueprint --lean-root <PROJECT> --json` as your baseline audit.
4. With claims, [acquire the claim](claims.md#acquire). Update the worktree from
   the shared branch and reload `work context`. Before editing, require the same
   `phase`, `blockers`, `dependencies`, `article_revision`, `open_statements`,
   `assumes`, and `revision` as the first read and, for a subagent, as its item.
   Unrelated parallel articles may change the graph-wide source revision.
5. Read the complete article, cited sources, dependency articles, existing Lean
   target, and `blueprint/.implementation-notes/<article_id>.md` when present.
   Read and write no note while `check` warns `.implementation-notes` is a link.

Before stating your article's result and before adding a helper, run `autoform
search` over the blueprint of `<PROJECT>` and its usable shared libraries for
it, following the [search steps](library-search.md#search-steps), which also
cover library hits and warnings that name a library. A search for your article's
own result finds that article, not a duplicate. Fix a refusal your arguments
caused, retry one that asks for it, and report any other as the leaf's blocker,
adding no helper. Use a blueprint hit stating a helper only if your dependencies
reach its article and the open-statement policy allows; a hit they do not reach
is a missing prerequisite. Never restate a hit as a helper.

Work only on your item's phase. Preserve the exact mathematical statement. Edit
no other article or its Lean, and weaken no public statement, except to delete
stale or repair orphaned notes, to repair another article's Lean that an import
edit breaks, to [raise copied code](auto-abstraction.md) at the end of a run, or
as a revision requires (below). Search the pinned Mathlib checkout before adding
a helper, add no new use of a deprecated declaration, and use the shared Lean
LSP and REPL with project path `<PROJECT>`. `roadmap/README.md` sets the [open
statements](../../../autoform_cli/reference/open-statements.md#open-statements)
policy. Under the default strict policy, the statement phase writes the
declaration and, for a theorem, the complete proof. Under `open_statements:
allowed`, a theorem's statement phase either writes the faithful statement as a
`theorem` whose whole body is `sorry`, adding no other `sorry`, and records
`statement: formalized`, or writes the full proof and, on acceptance, records
both assertions. In both policies the proof phase leaves the recorded statement
unchanged and completes its proof. Under the open policy a proof may use the
open statements its Markdown dependencies reach; `autoform work assumptions
--json` lists the exact `allowed_open_declarations`.

Lean imports are allowed; never pre-block a proof for needing one. Before an
effective module header or import change, follow [Import
edits](../SKILL.md#import-edits). Before revising a declaration other articles'
Lean uses (an item flagged `revision` is one; restating it records `statement:
formalized`), start from `autoform work impact` and edit only as the [revision
contract](../../../autoform_cli/reference/claims-and-revisions.md#revision-contract)
requires, under the claims it requires. Without claims, to edit beyond your
article (an import edit, a revision or its grown `claim_targets`, or an article
your import edit broke), report the new targets and wait until the lead confirms
no other worker holds them; if you cannot wait, commit, stop, and report.

Finish with the focused Lake target, declaring the result in a module the
library root imports or the lakefile's globs cover. Check the recorded
declaration with `#print axioms`: reject `sorry` beyond the policy, new axioms,
unsafe shortcuts, a weaker theorem, unused hypotheses, and an unrelated
declaration. Treat `unusedVariables` and `unusedSectionVars` warnings on your
article's theorems as findings; once the proof is complete and its focused build
passes, also run Batteries' `unusedArguments` linter and resolve each finding,
both per the [unused-hypothesis rules](unused-hypotheses.md). After the focused
build passes, require an independent Agent Review of every changed statement or
proof under its [faithfulness](../../agent-review/references/faithfulness.md)
and [proof integrity](../../agent-review/references/proof-integrity.md) rubrics;
the reviewer does not edit the candidate. Record progress only when both pass,
and report insufficient evidence or any failing score instead.

On acceptance, record only your article's exact compiled declaration as `lean`
and truthful assertions: `statement: formalized`, plus `proof: formalized` once
the proof is complete. The article keeps only frontmatter and informal
mathematics, plus, in a vault about a library, how that library states and
proves each result, in English and TeX; move its still-useful notes to its
implementation note, the only place to write notes: Lean names, Mathlib gaps,
prior art, friction, partial progress, and, after a useful failed route, the
remaining goal, checked lemmas, and next route, never a transcript, retry
counter, secret, or machine-local path. Never link the note from an article.

Run `autoform check <PROJECT>/blueprint --lean-root <PROJECT>` and `autoform
audit` with the same arguments. Resolve each finding you introduced on your
article, matching `article_path` and `code`, not `reason`, against the baseline;
keep a `lean-target-deprecated` the revision contract expects. Delete every
`stale-implementation-note`, any article's. Report other pre-existing findings
unfixed; report a `catalog-unbounded-limit` or `catalog-unscoped-limit` warning,
and change that code only when the user asks, rebuilding before anything is
recorded. Under the open policy, before landing a proof, record `proof:
formalized`, reproduce the CI audit as the open statements reference shows, and
require a `conditional` or `sorry-free` line for each recorded declaration and a
passing summary; never describe a conditional proof as complete, fully proved,
or sorry-free. Never write read-backs or `review_approved`; with
`blueprint/.autoform-review`, hand Human Review each newly formalized statement
and each article whose read-backs or approval the result made stale.

Then integrate the verified result as the first paragraph of [Integrate a
result](../SKILL.md#integrate-a-result) says, landing only as that step allows.
With `AUTOFORM_SUBAGENT_DEBRIEF=1`, [debrief](debrief-subagent.md) before
reporting. Report the branch, commit, changed files, checks, reviews, findings,
Human Review handoffs, AutoformBot friction, and exact remaining goals. Report
for Roadmap, instead of silently changing the DAG, a missing prerequisite, an
incorrect decomposition, a change another article needs beyond an import repair,
a proof recorded without its statement, an open statement the Markdown does not
declare as a dependency (or stop using it), or a `check` warning that a library
docstring cites the blueprint's sources, even when deleting a stale note brings
it back; neither keep the note nor edit a source page for it.
