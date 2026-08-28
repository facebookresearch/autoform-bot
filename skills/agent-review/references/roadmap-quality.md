# Roadmap-quality rubric

Judge the mathematical plan, not the Lean proof quality. Review the declared
scope against the sources, then inspect the fine DAG and its coarse roadmap.

## Evidence

1. Read the coverage contract and the cited source passages; summaries are not
   substitutes for the source.
2. Run `autoform check` and inspect the project, chapter, and node-neighborhood
   graphs.
3. Inspect every milestone and every node in the requested review scope.
4. Distinguish statement prerequisites from proof-only prerequisites.

## Source fidelity and coverage — 40%

- Every planned main result has a stable source location and preserves its
  objects, hypotheses, quantifiers, and conclusion.
- The machine-checkable `Area | Coverage | Evidence` table uses `MAPPED`,
  `DECOMPOSED`, `DEFERRED`, and `OUT` to distinguish known material, roadmap
  nodes, explicit later work, and material outside formalization scope.
- Every row has concrete evidence, and no `MAPPED` row is described as complete.
- The fine nodes and coarse roadmap agree; discoveries made during decomposition
  are reflected in the milestones and coverage contract.

## Pull-request units and DAG — 40%

- Milestones group coherent mathematics by significance, not source section
  size.
- Each node is a plausible pull-request unit with one unique main result.
  Several definitions or statements may share a node when they support that
  result and should be reviewed and landed together.
- Every dependency is genuinely used. Missing intermediates, spurious edges,
  duplicates, and cross-milestone prerequisites are identified.
- `## Depends on` contains statement prerequisites; `## Proof depends on`
  contains dependencies needed only by the proof.

## Usability and status discipline — 20%

- Titles, paths, source links, and proof sketches let a contributor understand
  the intended change without reconstructing the plan from unrelated pages.
- Frontmatter asserts only verified facts. Readiness and completion remain
  derived from the DAG.
- Uncertainty, Mathlib candidates, and unresolved source questions are explicit.

## Scores and verdict

Use the same anchors on each axis: 5 complete and precise; 4 sound with minor
issues; 3 useful but needing material cleanup; 2 materially incomplete or
misleading; 1 mostly unreliable; 0 absent or vacuous. Weight the three axes
40/40/20. Call the roadmap clean only when source fidelity and DAG quality are
at least 4 and usability is at least 3. Reject when source fidelity or DAG
quality is 2 or below. Missing source evidence caps source fidelity at 2; a node
with several peer main results and no unique completion target caps DAG quality
at 2.
