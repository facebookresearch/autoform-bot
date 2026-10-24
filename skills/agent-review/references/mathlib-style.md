# Mathlib and Lean style

Use the checked-out project's conventions when they are stricter. Verify local Mathlib source and
declaration types rather than relying on memory.

## Conventions

- Search the pinned Mathlib checkout before introducing new lemmas, and prefer a short proof using a
  verified existing theorem over recreating library mathematics.
- Namespaces describe mathematics, never chapters, task IDs, or theorem numbers.
- Use the weakest sufficient typeclasses (`Semiring` before `Ring`, `Preorder` before
  `LinearOrder`) and `Finite` before `Fintype` when enumeration is unnecessary. Remove unused
  hypotheses.
- Follow the repository's configured line length.

## Lean pitfalls

- Division and inversion by zero are total; for example `0 / 0 = 0` and `Real.log 0 = 0` in Lean.
- Natural subtraction truncates. Track coercions deliberately and use cast tactics rather than
  relying on elaboration accidents.
- Treat `erw`, aggressive unfolding, global `open`, broad automation, custom syntax, and new
  `[simp]` lemmas as review points because they can hide fragility or alter downstream behavior.
