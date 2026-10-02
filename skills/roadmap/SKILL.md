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

When the adopted boundary is a repository, library, global wiki, or other
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

A future-work slice never stands in for a repository-wide inventory. Keep
existing, wishlist, conjectural, and planned mathematics in the same book and
DAG. `DECOMPOSED` means represented by roadmap articles, not unfinished, so it
may describe mathematics whose Lean implementation is already complete. A
checked local declaration is valid grounding for repository documentation; do
not invent an external citation or historical-source claim.

## Ground and decompose

Inspect the repository and existing vault before writing. Preserve accepted
material and unrelated changes; send missing infrastructure to Setup.

Work from exact source passages. Record stable source locations, assumptions,
and uncovered prerequisites under `blueprint/sources/`; label project-authored
specifications honestly. Search the pinned Mathlib checkout before planning a
replacement. External research is read-only, and contacting people requires
permission. Read [Setup's Zulip workflow](../setup/references/zulip.md) only for
requested Zulip work.

Enumerate the entire adopted boundary in `blueprint/coverage/README.md`.
`MAPPED` is unfinished, `DECOMPOSED` links to roadmap nodes, `DEFERRED` records
an explicit user decision or concrete external blocker, and `OUT` explains an
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

Assert `statement: formalized`, `proof: formalized`, or `mathlib: true` only
after exact verification. Definitions, structures, classes, instances, and
inductives have no separate proof obligation. A theorem or lemma receives
`proof: formalized` only when its proof is complete and contains no placeholder
or proof-wanted mechanism. An axiom or wanted declaration is not a proof; a
definition of a conjecture proposition formalizes its representation, not the
conjecture. Hand deeper trust and axiom review to Agent Review.

For a large source, divide independent sections among available agents while
retaining one owner for global coverage, status semantics, and dependency
consistency. Reconcile every affected source and milestone page, the coverage
contract, `blueprint/README.md`, and the repository `README.md`.

## Finish

After the final edit, use the CLI reference to run `autoform check` and
`autoform audit`, refresh the bounded vault graph, and resolve every finding
introduced by this work or inside the adopted boundary. Report unrelated
pre-existing findings instead of silently widening scope. Commit the vault and
refreshed graph only after this final validation; pushing is outward-facing and
requires a user request.

Finish only when the adopted boundary has no `MAPPED` rows, every `DECOMPOSED`
area links to a source-grounded fine DAG or exact catalog ledger, affected pages
and the graph agree, and the latest commit contains every change from the pass.
Do not stop after discovery, a coarse proposal, one chapter, or unchanged
validation. Report the material delta, evidence, existing/formalized versus
wishlist/conjectural/planned counts, declaration-reconciliation gaps, remaining
explicit blockers, and the next execution frontier. Mark an active Goal
complete only after these conditions hold.
