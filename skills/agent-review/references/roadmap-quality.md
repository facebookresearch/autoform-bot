# Roadmap-quality rubric

Judge the mathematical plan, not the Lean proof quality. Review the declared
scope against the sources, then inspect the fine DAG and its coarse roadmap.

## Evidence

1. Read the coverage contract and the cited source passages; summaries are not
   substitutes for the source.
2. Run `autoform check` with `--lean-root` and inspect the project, chapter, and
   node-neighborhood graphs. Treat each warning that a shared library
   declaration's docstring cites one of the blueprint's sources as a library hit
   for item 5, and a warning about a module's docstring as a hit for each
   declaration it counts; search the module's name with `--library` to list
   them all.
3. Inspect every milestone and every node in the requested review scope, with
   its hidden `blueprint/.implementation-notes/<article_id>.md` when present.
4. Distinguish statement prerequisites from proof-only prerequisites.
5. Run `autoform search`, described in the
   [search contract](../../../autoform_cli/reference/search.md#search-contract), for each
   main result in scope and each intermediate result its proof proves or cites,
   by a few distinctive words and by its Lean name when it has one, over the
   blueprint and the project's usable shared libraries, as the
   [library search rules](../../formalize/references/library-search.md)
   describe. List the queries run and any library refused.
   - Each result hits its own article; a second article stating the same result
     is a duplicate. A hit whose `catalog` is `module` is an inventory listing
     the declaration, not a duplicate of the article that expounds it, and a
     library hit is not a duplicate of an article either.
   - Matching is literal, so finding none, even after a retry with fewer
     words, proves nothing: also compare the statements read for item 3.
   - A `complete` library hit for a result in scope or an intermediate result
     is a missed reuse unless the article that states or uses it is proved, its
     implementation note names the declaration, or the source's page records
     why it, or its module, does not apply.
   - Read each library declaration a source page names: one named without a
     reason, or with a reason that does not hold for an article reaching that
     source, is a missed reuse.

## Source fidelity and coverage — 40%

- Every planned main result has a stable source location and preserves its
  objects, hypotheses, quantifiers, and conclusion.
- The machine-checkable `Area | Coverage | Evidence` table uses `MAPPED`,
  `DECOMPOSED`, `INVENTORIED`, `DEFERRED`, and `OUT` to distinguish known
  material, roadmap nodes, inventoried existing modules, explicit later work,
  and material outside formalization scope. `DECOMPOSED` evidence reaches
  declaration articles, and `INVENTORIED` evidence reaches `catalog: module`
  leaves.
- Every row has concrete evidence, and no `MAPPED` row is described as complete.
- The fine nodes and coarse roadmap agree; discoveries made during decomposition
  are reflected in the milestones and coverage contract.

## Pull-request units and DAG — 40%

- Milestones group coherent mathematics by significance, not source section
  size.
- Each node is a plausible pull-request unit with one unique main result.
  Several definitions or statements may share a node when they support that
  result and should be reviewed and landed together.
- A `catalog: module` leaf records existing code and is never dispatched, so
  judge it by reconciliation instead: its `lean:` names resolve, it links its
  ledger under `blueprint/sources/`, and an `INVENTORIED` row reaches it. The
  pull-request-unit and unique-main-result rules do not apply to it.
- Every dependency is genuinely used. Missing intermediates, spurious edges,
  duplicates, missed library reuse, and cross-milestone prerequisites are
  identified.
- `## Depends on` contains statement prerequisites; `## Proof depends on`
  contains dependencies needed only by the proof.
- An article whose result a library already proves lists under
  `## Proof depends on` only the articles that proof uses, usually none. Links
  to the source's route there are spurious edges that block it.

## Usability and status discipline — 20%

- Titles, paths, source links, and proof sketches let a contributor understand
  the intended change without reconstructing the plan from unrelated pages.
- Each statement uses standard notation, gives context for each symbol, says
  what the result is for, and contains no proof steps.
- Frontmatter asserts only verified facts. Readiness and completion remain
  derived from the DAG.
- Uncertainty, Mathlib candidates, and unresolved source questions are explicit.
  Articles hold only frontmatter and informal mathematics, so Mathlib candidates
  and other Lean-side findings are in the article's implementation note, never
  in the article.

## Scores and verdict

Use the same anchors on each axis: 5 complete and precise; 4 sound with minor
issues; 3 useful but needing material cleanup; 2 materially incomplete or
misleading; 1 mostly unreliable; 0 absent or vacuous. Weight the three axes
40/40/20. Call the roadmap clean only when source fidelity and DAG quality are
at least 4 and usability is at least 3. Reject when source fidelity or DAG
quality is 2 or below. Missing source evidence caps source fidelity at 2; a node
other than a catalog with several peer main results and no unique completion
target caps DAG quality at 2.
