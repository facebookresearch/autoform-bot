---
name: agent-review
description: >-
  Judge an Autoform mathematical roadmap or Lean formalization with explicit,
  evidence-based rubrics. Use for an independent agent audit of source coverage,
  DAG quality, statement faithfulness, proof integrity, axioms, sorries, or
  Mathlib contribution quality; do not use merely to prepare a visualization for
  a human reviewer.
---

# Judge Autoform work as an agent

Select the rubric from the artifact under review.

- For a roadmap or blueprint, read [roadmap quality](references/roadmap-quality.md),
  inspect its declared sources and coverage boundary, and validate the Markdown
  DAG.
- For Lean code, read [faithfulness](references/faithfulness.md),
  [proof integrity](references/proof-integrity.md), [code quality](references/code-quality.md),
  and [Mathlib style](references/mathlib-style.md). Compile the relevant target,
  inspect the proof chain, and compare the complete public statement with the
  original source.

Keep objective evidence separate from judgment. Never claim compilation,
declaration resolution, axiom cleanliness, source coverage, or dependency
correctness without showing how it was checked. If required sources are absent,
return insufficient evidence rather than guessing.

Report findings first, ordered by severity and tied to files or nodes. Then give
the rubric scores, weighted verdict, commands run, unresolved questions, and a
short remediation list. Do not edit the reviewed work unless the user separately
asks for fixes.

Use the short [Cabannes thesis review case](references/thesis-review-case.md)
when a concrete Lean example helps distinguish faithfulness from integrity.
