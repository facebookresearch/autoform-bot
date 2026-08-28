---
kind: source
status: adopted
---

# Thesis source map

Vivien Cabannes, *From Weakly Supervised Learning to Active Labeling*, PhD
thesis, 2022. Stable public record: [arXiv:2209.11629](https://arxiv.org/abs/2209.11629).

The initial DAG is anchored in the “Infimum Loss” chapter. These labels occur
in `infimum/core.tex` in the public e-print source; verify them again against
the source revision adopted by the project:

| Blueprint target | Source label |
| --- | --- |
| Eligibility | `il:def:eligibility` |
| Non-ambiguity | `il:def:non-ambiguity` |
| Non-ambiguity determinism | `il:thm:ambiguity` |
| Infimum loss | `il:thm:infimum-loss` |
| Supervision recovery | `il:thm:non-ambiguity` |

Labels locate the authoritative statements; the node summaries are planning
notes and must not replace source inspection.

## Supporting formalization

The [Full Supervision chapter](../roadmap/full-supervision/README.md) separates
the Lean definition `CabannesThesis.supervision` and its elementary
non-ambiguity lemma from the stronger source-level supervision-recovery target.
These are implementation support nodes, not additional claims about the thesis
source.
