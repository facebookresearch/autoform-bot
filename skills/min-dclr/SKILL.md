---
name: min-dclr
description: >-
  Recompute and update a minimal source-to-Lean declaration checklist for the
  current repository or pull-request snapshot. Use when someone wants a
  refreshable list of source-facing statements and only their relevant new
  definitions; do not use for a full rubric-based formalization audit.
---

# Refresh a minimal declaration checklist

Treat every invocation as a fresh snapshot. Recompute the checklist from the
current source and code rather than trusting entries from an earlier run.

Resolve the mathematical source, review scope or pull-request base, and target
Markdown file. If no target is supplied and no uniquely marked checklist
exists, return the checklist in chat and ask where it should be persisted rather
than choosing a file silently.

Read [the snapshot workflow](references/snapshot-workflow.md). Replace the
entire managed checklist section so declarations that disappeared or left the
dependency closure are deleted and newly relevant declarations are added.
Use `autoform declaration-closure` as the sole authority for the Lean
dependency closure; do not ask the language model to infer that graph from
source text. If Lean cannot elaborate the requested modules, report that the
exact closure is unavailable rather than publishing a partial closure.
Whenever the source is available, the refreshed section must include the
original mathematical statement or definition as a verbatim quotation with its
locator, paired with the existing GitHub link to the Lean declaration that
implements it. Do not copy the Lean implementation into the Markdown. Preserve
all content outside that section.
