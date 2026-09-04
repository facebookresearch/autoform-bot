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
Preserve all content outside that section.
