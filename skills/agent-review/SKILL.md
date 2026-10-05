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
return insufficient evidence rather than guessing. In a project that allows
open statements, a proof resting on declared open statements is conditional:
name the statements it assumes and never call it axiom-clean.

Regenerate skeleton evidence from the exact candidate after its Lean build.
Do that only in a trusted checkout or an operating-system sandbox: the command
evaluates Lake configuration and project Lean metaprograms, and its resource
bounds are not a security boundary.
Treat a stale-build refusal as insufficient evidence; never approve a current
source excerpt paired with an older compiled declaration. Record the skeleton
hash as a drift checksum for the elaborated declaration and trust context, and
the evidence hash for the exact packet that was read. For a
source-faithfulness verdict, record the article review hash that binds the joint
packet to the cited passage, its locator, and the skeleton hash. These hashes
are provenance evidence, not reviewer authentication or an approval key.
Candidate code runs during extraction and can forge process output, so treat
its report as advisory when the checkout is not trusted.

Report findings first, ordered by severity and tied to files or nodes. Then give
the rubric scores, weighted verdict, commands run, unresolved questions, and a
short remediation list. Do not edit the reviewed work unless the user separately
asks for fixes.

Use the short [Cabannes thesis review case](references/thesis-review-case.md)
when a concrete Lean example helps distinguish faithfulness from integrity.
