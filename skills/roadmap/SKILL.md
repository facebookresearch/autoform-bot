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

There is no `autoform` on `PATH`. Run it from the Lean project root as
`uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform`, where the root is the
absolute plugin directory holding `skills/` and `pyproject.toml`; give a
subagent that prefix.

## Own the whole pass

Honor a request explicitly limited to inspection, audit, status, or
visualization. Otherwise, a direct user invocation of Roadmap requests one
complete planning pass over the source boundary named by the user or already
adopted by the vault.

That direct invocation explicitly requests persistent execution. When the
runtime exposes a model-callable Goal lifecycle, use it before other work:
continue a compatible active Goal or create one for a complete, validated,
reconciled, and committed roadmap of that source boundary, with no token budget
unless the user supplied one. Never replace an unrelated active Goal, ask the
user to invoke another command, shell out to a host's interactive goal command,
or substitute todos or repository files for native continuation. Without a
compatible Goal, complete the same pass in the current run.

Treat source discovery, coarse coverage, chapter boundaries, fine
decomposition, and validation as internal checkpoints. Do not pause for
approval unless the user requested staged review. Make reversible choices and
record assumptions. Ask one concise question only after inspection cannot
identify or access the source, or when incompatible scope choices remain and
choosing one would discard accepted work.

## Inventory existing project mathematics

When the adopted boundary is a repository-wide library, global wiki, or other
existing codebase, project-owned Lean is a primary mathematical source rather
than implementation prior art to omit. Before planning future work, inventory
every in-scope tracked module and public mathematical declaration, plus
declaration-like wishlist or conjecture entries and adopted planning sources;
state how private helpers, tests, generated code, aggregate import roots, and
imported dependencies are treated; reconcile each in-scope declaration to a
roadmap article, an exact grouped ledger, or an evidenced coverage disposition;
and record each module and declaration name, with any separate literary
provenance, under `blueprint/sources/`.

Inventory and exposition are independent axes: a catalog ledger can close the
inventory axis but does not make an area `DECOMPOSED`. A future-work slice
cannot stand in for a repository-wide inventory; keep existing, wishlist,
conjectural, and planned mathematics in one book and DAG. `DECOMPOSED` means
represented by declaration articles, not unfinished, so it may describe
mathematics already implemented in Lean. A checked local declaration is valid
grounding; do not invent an external citation or historical-source claim.

## Ground and decompose

Inspect the repository and existing vault before writing. Preserve accepted
material and unrelated changes; send missing infrastructure to Setup and Lean
execution to Formalize.

Work from exact source passages. When the boundary includes existing Lean, first
extract module and declaration docstrings and nearby source comments alongside
the Lean signatures; they locate and describe code but never replace the adopted
source. Record stable source locations, assumptions, and uncovered prerequisites
under `blueprint/sources/`, labelling project-authored specifications as such.
External research is read-only, and contacting people requires permission. Do
not invent a statement, motivation, proof narrative, dependency, citation, or
historical attribution the sources do not support. Read [Setup's Zulip
workflow](../setup/references/zulip.md) only for requested Zulip work.

### Search before adding

Before adding a formalizable article, search the pinned Mathlib checkout, and
run `autoform search` over the blueprint and every usable shared library for its
result and for each intermediate result its proof proves or cites, as the
[search steps](../formalize/references/library-search.md#search-steps) say.

- If a library already proves a result, the result keeps its article and reuses
  that proof: its `## Proof depends on` lists only the articles that proof uses,
  usually none, and its proof sketch gives the source's own route. A proof that
  needs a library result with no article yet gets an article for it, proved from
  the library and linked under `## Proof depends on`.
- Name a reused library declaration in the implementation note, not the article,
  and leave `lean:` unset until Formalize declares the result in the project:
  `lean:` names only a declaration in the project's own Lean files.
- A refusal (exit 2) is not an empty result: resolve what it reports without
  adding the article, and search again. A dependency link to a page not yet
  written causes one, so search for a whole batch before writing the first.
- When another formalizable article already states the result, link to it from
  each article that needs it, under `## Depends on` or `## Proof depends on`,
  and from the coverage row, instead of adding a node.
- Search reads only the checkout it is given, so the owner of global consistency
  repeats it for what parallel agents added.

### Coverage and milestones

Enumerate the entire adopted boundary in `blueprint/coverage/README.md`, with
the dispositions the
[CLI reference](../../autoform_cli/README.md#audit-and-coverage) defines.
`DEFERRED` needs an explicit user decision or concrete external blocker; treat
any other `DEFERRED` row as queued work, and never defer merely to shorten the
run. `coverage.complete` only checks declared rows, so compare the table with
the source structure yourself.

Write milestone pages under `blueprint/roadmap/` by mathematical significance,
then one fine article per coherent unit with one unique main result. For an
existing repository, coherent grouping is allowed only when the exact ledger
still reconciles every public declaration. Do not turn existing formalization
into fictional future work. Put genuine statement prerequisites under
`## Depends on` and proof-only prerequisites under `## Proof depends on`. Never
delete or reverse a genuine article edge to make a chapter-level or scope-level
projection acyclic. After creating pages, run `autoform migrate article-ids
blueprint --write` to give new articles a durable `article_id`.

### Articles and implementation notes

Articles hold only frontmatter and informal mathematics. Put Lean-side findings
such as Mathlib gaps, prior art, and candidate declaration names in
`blueprint/.implementation-notes/<article_id>.md`, never a transcript, a secret,
or a machine-local path, and never link a note from an article; a shared library
declaration that does not apply is recorded on the source's page instead. The
[note rules](../../autoform_cli/README.md#articles-and-containment) say when a
note is stale, which `autoform audit` reports and you then delete, and how to
repair an orphan. Move an article by moving its file; a page written afresh
takes the old page's `article_id` line in the same edit. A commit that deletes
or merges an article moves what is still useful from its note into a surviving
article's note and deletes the note.

### Claims and revisions

Claims are optional; use them when [Formalize](../formalize/SKILL.md) would.
When the project uses them, acquire the `claim_target` that `autoform work
context` reports for a formalizable leaf before revising it, passing your own
`--worker-id`; renew it while editing and release it once the committed revision
is on the branch Formalize works from. A refused acquire means another agent
owns the article: leave it and report it. Claims write refs to the board's
remote, which is outward-facing, so make sure the request covers them.

When a revision changes a statement, or Human Review requests a Lean revision,
retract the article as step 6 of the [revision
contract](../../autoform_cli/reference/claims-and-revisions.md#revision-contract)
says. An article without `lean:` just loses `statement` and `proof`, a
Human-Review Lean revision is recorded in the implementation note, and retracted
dependents are claimed in one acquire when the project uses claims. Formalize
may land `lean:` on the branch it works from after your checkout, with or
without claims. Before the revision lands on that branch, and so before
releasing any claim, bring it up to date with that branch by rebase or merge,
retract any revised article that gained `lean:` there, repeat the Finish step's
final validation, and commit the retraction as a new commit. When the revision
lands later through a pull request, report the revised articles that had no
`lean:` on that branch at your last update, and list them in the pull request's
description if you open it, so whoever merges it retracts any that have gained
`lean:` by then.

### Statuses and catalogs

`open_statements` in `roadmap/README.md` is a project policy; absent means
strict, and [open
statements](../../autoform_cli/reference/open-statements.md#open-statements)
explains it. Change it only on the user's request and only once CI meets its
requirements, and turn it back off only once no open statement remains.

Assert `statement: formalized`, `proof: formalized`, or `mathlib: true` only
after exact verification. For declaration leaves, record exact compiled names in
`lean`. Set `proof: formalized` for a theorem or lemma only after checking that
its proof is complete and contains no placeholder or proof-wanted mechanism. An
axiom or wanted placeholder does not justify a formalized proof; a definition of
a conjecture proposition formalizes its statement representation, not the
conjecture. Set `mathlib: true` only for an exact verified upstream result. Hand
deeper trust and axiom review to Agent Review.

Use `catalog: module` only as a source inventory for one existing Lean module.
It may accompany exposition, but names, signatures, status tables, and imports
do not replace reader-facing mathematical prose. A catalog leaf omits
`declaration` and is never dispatchable proof work. Its assertion keys describe
the whole tracked module: set `statement: formalized` only when all inventoried
public declarations exist, and `proof: formalized` only when every included
implementation or proof is complete under the repository's trust policy. The
[catalog rules](../../autoform_cli/README.md#articles-and-containment) say which
names go in `lean` and which only in the declaration ledger; a keyword-declared
public name left out of `lean` or unresolved means the catalog must not carry
formalized assertions. A catalog holds back what depends on it, so a dependent
that needs one result links the declaration article instead.

When the project's CI pins an older `AUTOFORM_REF`, it rejects the keys and
markers that ref predates, such as `catalog:`, `area:`, `INVENTORIED` and
`statement: retracted`, and still rejects a rolled-up dependency cycle that
newer refs allow. Move the pin, or report it, before relying on one; until it
moves, omit the edge that closes such a cycle, retract by removing `statement`
and `proof` but keeping `lean:`, and report the old pin and each change it held
back.

### Large sources

For a large source, divide independent sections among available agents while
retaining one owner for global coverage, status semantics, and dependency
consistency. Reconcile every affected source and milestone page, the coverage
contract, `blueprint/README.md`, and the repository `README.md`.

## Finish

After the final edit, run `autoform check` with `--lean-root .` and `autoform
audit`, refresh the dependency summary with `autoform-visualize`, and resolve
every finding introduced by this work or inside the adopted boundary. A `check`
warning that a shared library declaration's docstring, or its module's, cites
one of the blueprint's sources usually means a library search was skipped:
reuse or record the declaration as the [CLI
reference](../../autoform_cli/README.md#check) says, with a reason that
holds for every article reaching the source, revisited when the boundary
changes; review a module when its warning first appears. Report unrelated
pre-existing findings instead of widening scope. A `catalog-unbounded-limit` or
`catalog-unscoped-limit` warning describes existing Lean code and never fails
the audit: report it, and change that code only when the user asks, through
Formalize, rebuilding before anything is recorded. Fix the other `warning:`
lines inside the adopted boundary as findings. Commit the vault, with the
refreshed summary where the repository tracks it, only after this final
validation; pushing is outward-facing and requires a user request.

Finish only when the adopted boundary has no `MAPPED` rows, every `DECOMPOSED`
area links source-grounded declaration articles, every `INVENTORIED` area links
exact catalogs, affected pages and the graph agree, and the latest commit
contains every change from the pass; a repository inventory also needs every
in-scope declaration reconciled. Do not stop after discovery, a coarse
proposal, one chapter, or unchanged validation. Report the material delta,
evidence, remaining blockers, and the next execution frontier, and for a
repository inventory the existing/formalized versus wishlist/conjectural/planned
counts and the reconciliation gaps. Before reporting, follow
[Debrief Orchestrator](../debrief-orchestrator/SKILL.md) with `--skill roadmap`.
Mark an active Goal complete only after these conditions hold.
