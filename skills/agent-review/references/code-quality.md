# Code-quality rubric

Judge Mathlib idiom, maintainability, and public API shape only. Do not use this axis to rescore
statement faithfulness or proof soundness; style may flag a review but never rescue or reject it.

Read [Mathlib style](mathlib-style.md) before scoring and inspect surrounding project and Mathlib
code for established names and APIs.

## Review points

- Each source statement has one self-contained public declaration; helpers may split proof work,
  but callers should not have to reconstruct a multi-part result from unrelated declarations.
- Names, namespaces, binders, imports, attributes, notation, and declaration placement follow local
  Mathlib practice.
- Hypotheses and typeclasses are minimal; implicit arguments are named where positional `@` use
  would be obscure; unused assumptions and redundant coercions are removed.
- Proofs use existing APIs and appropriate tactics, expose meaningful intermediate steps, handle
  degenerate cases clearly, and avoid broad unfolding or brittle automation.
- The code is readable enough for a Mathlib contributor to maintain without reverse engineering.

## Scores

| Score | Standard |
|---:|---|
| 5 | Fully idiomatic, well-factored, API-aware Mathlib-quality code. |
| 4 | Clean and readable with only minor naming, generality, layout, or tactic issues. |
| 3 | Functional but noticeably non-idiomatic or brittle; worthwhile cleanup is needed. |
| 2 | Multiple convention violations, poor abstraction choices, or a difficult-to-follow proof. |
| 1 | Pervasively opaque or unidiomatic; also applies when no single public declaration represents a multi-part source statement. |
| 0 | Structurally incomprehensible or dominated by severe Lean/Mathlib anti-patterns. |

Pass at 3. Ignore sorries and axioms on this axis except where they impair readability; proof
integrity owns their substantive verdict.
