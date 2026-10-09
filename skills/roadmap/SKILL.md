---
name: roadmap
description: >-
  Build, continue, inspect, or visualize a source-grounded mathematical roadmap
  and theorem DAG in an existing Autoform Markdown blueprint. Use for source
  research, scope, coverage, milestones, and roadmap articles; do not create
  repository infrastructure or prove Lean declarations.
---

# Build an Autoform roadmap

Maintain an ordered mathematical book whose formalizable leaves form a
dependency DAG of coherent, pull-request-sized units. Markdown under
`blueprint/` is the source of truth. Consult the
[format and command reference](../../autoform_cli/README.md) only when exact
syntax or CLI usage is needed, and the
[worked example](references/cabannes-thesis-roadmap.md) only when a concrete
source-to-DAG pattern would help.

## Own the whole pass

Honor a request explicitly limited to inspection, audit, status, or
visualization. Otherwise, a direct user invocation of Roadmap requests one
complete planning pass over the source boundary named by the user or already
adopted by the vault.

That direct invocation explicitly requests persistent execution. When the
runtime exposes a model-callable Goal lifecycle, use it before other work:
continue a compatible active Goal or create one for a complete, validated,
reconciled, and committed roadmap of that source boundary. Do not set a token
budget unless the user supplied one, and never replace an unrelated active
Goal. Do not ask the user to invoke another command, shell out to a host's
interactive goal command, or substitute todos or repository files for native
continuation. If no compatible Goal can be used, complete the same pass in the
current run and leave any unrelated Goal unchanged.

Treat source discovery, coarse coverage, chapter boundaries, fine
decomposition, and validation as internal checkpoints. Do not pause for
approval unless the user requested staged review. Make reversible choices and
record assumptions. Ask one concise question only after inspection cannot
identify or access the source, or when incompatible scope choices remain and
choosing one would discard accepted work.

## Ground and decompose

Inspect the repository and existing vault before writing. Preserve accepted
material and unrelated changes; send missing infrastructure to Setup. Formalize
owns Lean execution after this skill produces a validated ready frontier.

Work from exact source passages. Record stable source locations, assumptions,
and uncovered prerequisites under `blueprint/sources/`; label project-authored
specifications honestly. Search the pinned Mathlib checkout before planning a
replacement. External research is read-only, and contacting people requires
permission. Read [Setup's Zulip workflow](../setup/references/zulip.md) only for
requested Zulip work.

Before adding a formalizable article, run `autoform search` over the blueprint
for its result, by a few distinctive words of the statement and by a Lean name
when one is known; the
[search contract](../../autoform_cli/README.md#search-contract) says what is
matched. Read each hit's statement, and narrow a query whose listing the limit
cut short. Matching is literal, so the result is new only when retries with
fewer words, and under other usual names for its objects, also show no article
stating it. A refusal (exit 2) is not an empty result: resolve what it reports
without adding the article, and search again. A dependency link to a page not
yet written causes one, so search for a batch of articles before writing the
first.
When another formalizable article already states the result, link to it from
each article that needs it, under `## Depends on` or `## Proof depends on`, and
from the coverage row, instead of adding a node. A hit that is more general, a
special case, or only similar does not replace the result: add the article,
depending on the hit where it is used. Search reads only the checkout it is
given, so the owner of global consistency repeats it for what parallel agents
added.

Enumerate the entire adopted boundary in `blueprint/coverage/README.md`.
`MAPPED` is unfinished, `DECOMPOSED` links to roadmap nodes, `DEFERRED` records
an explicit user decision or concrete external blocker, and `OUT` explains an
exclusion. Treat every other `DEFERRED` row as queued work; never defer merely
to shorten the run. Because `coverage.complete` only checks declared rows,
compare the table with the source structure yourself.

Write milestone pages under `blueprint/roadmap/` by mathematical significance,
then one fine article per coherent unit with one unique main result. Ground each
statement and proof sketch in the source. Put genuine statement prerequisites
under `## Depends on` and proof-only prerequisites under `## Proof depends on`.
Assign durable `article_id` metadata to new articles; run
`autoform migrate article-ids blueprint --write` after creating the pages to
add deterministic IDs.
Assert formalization or `mathlib: true` only after exact verification. Before
revising a formalizable leaf, acquire the `claim_target` that `autoform work
context` reports for it, passing your own `--worker-id`; renew it while editing
and release it once the committed revision is on the branch Formalize works
from. A refused acquire means another agent owns the article: leave it and
report it. Claims write refs to the board's remote, which is outward-facing, so
make sure the request covers them. When a revision changes a statement whose
article has `lean:`, retract it: replace `statement: formalized` with
`statement: retracted`, remove `proof: formalized`, and keep `lean:`, which
Formalize needs to run `autoform work impact`; an article without `lean:` just
loses `statement` and `proof`. When the project's CI pins an `AUTOFORM_REF`
older than the marker, its `autoform check` rejects `statement: retracted`:
remove `statement` and `proof` and keep `lean:` until the pin moves, and report
the old pin. Under the open policy a retracted theorem stays
an open statement, so whatever rests on it stays conditionally proved. Retract
only that article and the dependents whose Markdown text the revision rewrites,
claiming them all in one acquire; the Lean-side impact decides every other
dependent. For a Lean revision requested in Human Review, record the decision in
the article and retract it the same way, so it returns to the frontier as a
statement phase flagged as a revision, and leave the Lean change to Formalize,
which follows the [revision
contract](../../autoform_cli/README.md#revision-contract); this skill edits
only Markdown. For a large source, divide independent sections among available
agents while retaining one owner for global coverage and dependency
consistency.

`open_statements` in `roadmap/README.md` is a project policy; absent means
strict. `allowed` lets a theorem's statement land with a `sorry` proof, so
dependents can be stated and proved before it is; the cost is conditionally
proved results that stay incomplete until those proofs land. Change it only on
the user's request and only once CI meets the
[open statements](../../autoform_cli/README.md#open-statements) requirements.
Turn it back off only once no open statement remains: the strict audit rejects
every `sorry`, and strict status shows a proof resting on one as proved.

Reconcile every affected source and milestone page, the coverage contract,
`blueprint/README.md`, and the repository `README.md`.

## Finish

After the final edit, use the CLI reference to run `autoform check` and
`autoform audit`, refresh the Mermaid graph, and resolve every finding introduced
by this work or inside the adopted boundary. Report unrelated pre-existing
findings instead of silently widening scope. Commit the vault and refreshed
graph only after this final validation; pushing is outward-facing and requires
a user request.

Finish only when the adopted boundary has no `MAPPED` rows, every `DECOMPOSED`
area links to a source-grounded fine DAG, affected pages and the graph agree,
and the latest commit contains every change from the pass. Do not stop after
discovery, a coarse proposal, one chapter, or unchanged validation. Report the
material delta, evidence, remaining explicit blockers, and next execution
frontier. Mark an active Goal complete only after these conditions hold.
