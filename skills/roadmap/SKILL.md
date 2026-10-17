---
name: roadmap
description: >-
  Build, continue, inspect, or visualize a source-grounded mathematical roadmap
  and theorem DAG in an existing Autoform Markdown blueprint. Use for source
  research, repository-wide inventories of existing and planned mathematics,
  scope, coverage, milestones, and roadmap articles; do not create repository
  infrastructure or prove Lean declarations.
---

# Build an Autoform roadmap

Maintain an ordered mathematical book whose formalizable leaves form a
dependency DAG of coherent, pull-request-sized units. Markdown under
`blueprint/` is the source of truth. Consult the
[format and command reference](../../autoform_cli/README.md) only when exact
syntax or CLI usage is needed, and the
[worked example](references/cabannes-thesis-roadmap.md) only when a concrete
source-to-DAG pattern would help.

There is no `autoform` on `PATH` in a Lean project. Run every `autoform`
command, from the Lean project root, as
`uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform` followed by its arguments,
where `<AUTOFORM_PLUGIN_ROOT>` is the absolute plugin root: the directory that
holds this skill's `skills/` folder, along with `pyproject.toml` and
`autoform_cli/`. Give a subagent that root and this prefix with its task.

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

## Inventory existing project mathematics

When the adopted boundary is a repository-wide library, global wiki, or other
existing codebase, project-owned Lean is a primary mathematical source rather
than implementation prior art to omit. Before planning future work:

- inventory every in-scope tracked module and public mathematical declaration,
  plus declaration-like wishlist or conjecture entries and adopted planning
  sources;
- state explicitly how private helpers, tests, generated code, aggregate import
  roots, and imported dependencies are treated;
- reconcile each in-scope declaration to a roadmap article, an exact grouped
  ledger, or an evidenced coverage disposition; and
- record each module and declaration name, together with any separate literary
  provenance, under `blueprint/sources/`.

Treat exact inventory reconciliation and mathematical exposition as independent
completeness axes. A declaration ledger answers what exists and where; prose
explains the mathematics. A catalog ledger can close the inventory axis, but it
does not make an area `DECOMPOSED`.

A future-work slice cannot stand in for a repository-wide inventory. Keep
existing, wishlist, conjectural, and planned mathematics in the same book and
DAG. `DECOMPOSED` means represented by declaration articles, not unfinished,
so it may describe mathematics whose Lean implementation is already complete.
A checked local declaration is valid grounding for repository documentation;
do not invent an external citation or historical-source claim.

## Ground and decompose

Inspect the repository and existing vault before writing. Preserve accepted
material and unrelated changes; send missing infrastructure to Setup. Formalize
owns Lean execution after this skill produces a validated ready frontier.

Work from exact source passages. When the boundary includes existing Lean,
first extract module and declaration docstrings and nearby source comments
alongside the Lean signatures and implementations; they locate and describe
code but never replace the adopted source. Record stable source locations,
assumptions, and uncovered prerequisites under `blueprint/sources/`; label
project-authored specifications honestly. Search the pinned Mathlib checkout
and every usable shared library before planning a replacement. External
research is read-only, and contacting people requires permission. Do not invent
a statement, motivation, proof narrative, dependency, citation, or historical
attribution that the available sources do not support. Read
[Setup's Zulip workflow](../setup/references/zulip.md) only for requested
Zulip work.

Before adding a formalizable article, run `autoform search` over the blueprint
for its result, by a few distinctive words of the statement and by a Lean name
when one is known; the
[search contract](../../autoform_cli/README.md#search-contract) says what is
matched. Read each hit's statement, and narrow a query whose listing the limit
cut short. Matching is literal, so the result is new only when retries with
fewer words, and under other usual names for its objects, also show no article
stating it. Search the project's usable shared libraries in the same call: run
`autoform library list .` once, and pass each package it reports as usable to
every search as `--library NAME --lean-root .`, even while the blueprint is
empty, since a library may already prove the result. The
[library search rules](../formalize/references/library-search.md) give the
details.
If a library already proves a result, reuse that proof instead of proving the
result again: the result keeps its article, and its proof cites the library
declaration. If a proof needs a library result that has no article yet, add an
article for it, proved from the library, and link it under `## Proof depends
on`. A refusal (exit 2) is not an empty result: resolve what it reports without
adding the article, and search again, except that a refusal naming a library is
handled as the library search rules say. A dependency link to a page not yet
written causes one, so search for a batch of articles before writing the first.
When another formalizable article already states the result, link to it from
each article that needs it, under `## Depends on` or `## Proof depends on`, and
from the coverage row, instead of adding a node. A hit that is more general, a
special case, or only similar does not replace the result: add the article,
depending on the hit where it is used. Search reads only the checkout it is
given, so the owner of global consistency repeats it for what parallel agents
added.

Enumerate the entire adopted boundary in `blueprint/coverage/README.md`.
`MAPPED` is unfinished, `DECOMPOSED` links source-grounded declaration
articles, `INVENTORIED` links exact module catalogs, `DEFERRED` records an
explicit user decision or concrete external blocker, and `OUT` explains an
exclusion. Treat every other `DEFERRED` row as queued work; never defer merely
to shorten the run. Because `coverage.complete` only checks declared rows,
compare the table with the source structure yourself.

Write milestone pages under `blueprint/roadmap/` by mathematical significance,
then one fine article per coherent unit with one unique main result. For an
existing repository, coherent grouping is allowed only when the exact ledger
still reconciles every public declaration. Do not turn existing formalization
into fictional future work. Ground each statement and proof sketch in the
source. Put genuine statement prerequisites under `## Depends on` and
proof-only prerequisites under `## Proof depends on`.
Never delete or reverse a genuine article edge to make a chapter-level or
scope-level projection acyclic. The authored leaf DAG is the mathematical
record; a coarser view may have relations in both directions and must present
them honestly.
Assign durable `article_id` metadata to new articles; run
`autoform migrate article-ids blueprint --write` after creating the pages to
add deterministic IDs.
Articles hold only frontmatter and informal mathematics; put Lean-side findings
such as Mathlib gaps, prior art, and candidate declaration names in
`blueprint/.implementation-notes/<article_id>.md`, never a transcript, a secret,
or a machine-local path. The hidden per-article file
stays out of the mathematical wiki and remains valid when the article moves.
Never link it from an article. Delete it when the article is proved or marked
`mathlib: true`; delete a container's note when every formalizable descendant,
and every leaf marked `not_ready` beneath it, is proved and there is at least
one; a module catalog never counts. To move an article, move its file, which
keeps its `article_id` line and
so its note; a page written afresh must take that line in the same edit that
deletes the old page. When a commit deletes or merges an article,
move anything still useful from its note into a surviving article's note and
delete the note in that commit; an orphaned note blocks every command that loads
the graph, and any worker may repair one the same way, or by restoring the
`article_id` line of an article that still exists but lost it.

Before revising a formalizable leaf, acquire the `claim_target` that `autoform
work context` reports for it, passing your own `--worker-id`; renew it while
editing and release it once the committed revision is on the branch Formalize
works from. A refused acquire means another agent owns the article: leave it and
report it. Claims write refs to the board's remote, which is outward-facing, so
make sure the request covers them. When a revision changes a statement whose
article has `lean:` once rebased onto the branch Formalize works from, retract
it: replace `statement: formalized` with `statement: retracted`, remove
`proof: formalized`, and keep `lean:`, which
Formalize needs to run `autoform work impact`; an article without `lean:` just
loses `statement` and `proof`. When the project's CI pins an `AUTOFORM_REF`
older than the marker, its `autoform check` rejects `statement: retracted`:
remove `statement` and `proof` and keep `lean:` until the pin moves, and report
the old pin. Under the open policy a retracted theorem stays
an open statement, so whatever rests on it stays conditionally proved. Retract
only that article and the dependents whose Markdown text the revision rewrites,
claiming them all in one acquire; the Lean-side impact decides every other
dependent. For a Lean revision requested in Human Review, record the decision in
the article's `blueprint/.implementation-notes/<article_id>.md` and retract the
article the same way, so it returns to the frontier as a statement phase flagged
as a revision, and leave the Lean change to Formalize, which follows the
[revision contract](../../autoform_cli/README.md#revision-contract); this skill
edits only Markdown.

`open_statements` in `roadmap/README.md` is a project policy; absent means
strict. `allowed` lets a theorem's statement land with a `sorry` proof, so
dependents can be stated and proved before it is; the cost is conditionally
proved results that stay incomplete until those proofs land. Change it only on
the user's request and only once CI meets the
[open statements](../../autoform_cli/README.md#open-statements) requirements.
Turn it back off only once no open statement remains: the strict audit rejects
every `sorry`, and strict status shows a proof resting on one as proved.

Assert `statement: formalized`, `proof: formalized`, or `mathlib: true` only
after exact verification. For declaration leaves, record exact compiled names
in `lean`. Definitions, structures, classes, instances, and inductives have no
separate proof obligation. Set `proof: formalized` for a theorem or lemma only
after checking that its proof is complete and contains no placeholder or
proof-wanted mechanism. An axiom or wanted placeholder does not justify a
formalized proof; a definition of a conjecture proposition formalizes its
statement representation, not the conjecture. Set `mathlib: true` only for an
exact verified upstream result. Hand deeper trust and axiom review to Agent
Review.

Use `catalog: module` only as a source inventory for one existing Lean module.
It may accompany exposition, but names, signatures, status tables, and imports
do not replace reader-facing mathematical prose. A catalog leaf omits
`declaration` and is never dispatchable proof work. When the project's CI pins
an `AUTOFORM_REF` older than catalogs, its `autoform check` rejects `catalog:`
and `area:`, and its `render` rejects `INVENTORIED`: move the pin, or report
it, before adding catalogs, areas, or `INVENTORIED` rows.

For a `catalog: module` leaf, assertion keys describe the whole tracked module:
set `statement: formalized` only when all inventoried public declarations
exist, and `proof: formalized` only when every included implementation or proof
is complete under the repository's trust policy. Record the full name of every
public declaration the module writes with a declaration keyword, as the Lean
source index records them, in `lean` and in the declaration ledger linked from
the article's `## Sources` section. Names Lean generates
(constructors, structure fields, derived or unnamed instances, and `@[ext]`,
`@[simps]`, or `@[to_additive]` results) and declarations the index cannot see
yet (behind `nonrec` or `meta`, or written `class inductive`) go in the ledger
as supporting entries, never in `lean`; a `private` name never goes in `lean`.
A keyword-declared public name left out of `lean` or unresolved means the
catalog is not yet verified and must not carry formalized assertions. Like any
prerequisite, a catalog linked under `## Depends on` or `## Proof depends on`
holds its dependents back until its whole-module assertion is recorded, so a
dependent that needs one result links the declaration article that expounds it.

For a large source, divide independent sections among available agents while
retaining one owner for global coverage, status semantics, and dependency
consistency. Reconcile every affected source and milestone page, the coverage
contract, `blueprint/README.md`, and the repository `README.md`.

## Finish

After the final edit, use the CLI reference to run `autoform check` and
`autoform audit`, refresh the Mermaid graph, and resolve every finding
introduced by this work or inside the adopted boundary. Report unrelated
pre-existing findings instead of silently widening scope. A
`catalog-unbounded-limit` or `catalog-unscoped-limit` warning describes
existing Lean code and never fails the audit: report it, and change that code
only when the user asks, through Formalize, rebuilding before anything is
recorded. Fix the other `warning:` lines inside the adopted boundary as you
would a finding. Commit the vault and refreshed graph only after this final
validation; pushing is outward-facing and requires a user request.

Finish only when the adopted boundary has no `MAPPED` rows, every `DECOMPOSED`
area links source-grounded declaration articles, every `INVENTORIED` area links
exact catalogs, affected pages and the graph agree, and the latest commit
contains every change from the pass; a repository inventory also needs every
in-scope declaration reconciled. Do not stop after discovery, a coarse
proposal, one chapter, or unchanged validation. Report the material delta,
evidence, remaining blockers, and the next execution frontier, and for a
repository inventory the existing/formalized versus wishlist/conjectural/planned
counts and the reconciliation gaps. Mark an active Goal complete only after
these conditions hold.
