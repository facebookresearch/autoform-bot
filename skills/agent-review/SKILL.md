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

There is no `autoform` on `PATH`. Run it from the project under review as
`uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform`, where the root is the
absolute plugin directory holding `skills/` and `pyproject.toml`; give a
subagent that prefix.

Select the rubric from the artifact under review.

- For a roadmap or blueprint, read [roadmap quality](references/roadmap-quality.md),
  inspect its declared sources and coverage boundary, and validate the Markdown
  DAG.
- For Lean code, read [faithfulness](references/faithfulness.md),
  [proof integrity](references/proof-integrity.md), [code quality](references/code-quality.md),
  and [Mathlib style](references/mathlib-style.md). Compile the relevant target,
  inspect the proof chain, and compare the complete public statement with the
  original source.
- For a read-back, an auditor's English account of what Lean declarations
  assert, read [read-back faithfulness](references/readback-faithfulness.md).
  A trusted coordinator supplies its hash-bound review item. Judge only the
  supplied testimony against the cited source passage, without opening Lean,
  and return exactly the reference's JSON verdict.

Keep objective evidence separate from judgment. Never claim compilation,
declaration resolution, axiom cleanliness, source coverage, or dependency
correctness without showing how it was checked. If required sources are absent,
return insufficient evidence rather than guessing. A proof resting on declared
open statements is conditional: name them and never call it axiom-clean.

Except in the isolated read-back-judge role, regenerate skeleton evidence from
the exact candidate after its Lean build. Do that only in a trusted checkout or
an operating-system sandbox: the command evaluates Lake configuration and
project Lean metaprograms, and its resource bounds are not a security boundary.
Candidate code runs during extraction and can forge process output, so treat
its report as advisory when the checkout is not trusted. Treat a stale-build
refusal as insufficient evidence; never approve a current source excerpt paired
with an older compiled declaration. Record the skeleton hash, the evidence hash
of the packet that was read, and, for a source-faithfulness verdict, the
article review hash; the
[CLI reference](../../autoform_cli/README.md#skeleton-hashes) says what each
binds. These hashes are provenance evidence, not reviewer authentication or an
approval key. If the user has paused skeleton evidence during proof development,
skip it and Formalize's import-edit baselines, say so, and record no skeleton
hash; still build the target, run `#print axioms` on each recorded declaration
and `autoform check` and `audit` with `--lean-root`, and compare the compiled
statement with its source.

Report findings first, ordered by severity and tied to files or nodes. Then give
the rubric scores, weighted verdict, commands run, unresolved questions, and a
short remediation list. The read-back rubric's exact JSON output replaces this
general report layout. Do not edit the reviewed work unless the user separately
asks for fixes.

Score faithfulness and integrity separately: a sorry-free proof that drops a
source hypothesis still fails faithfulness; report it, do not average it away.
