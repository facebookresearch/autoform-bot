# Mathlib and Lean style

Use the checked-out project's conventions when they are stricter. Verify local Mathlib source and
declaration types rather than relying on memory.

## Working method

- Search before proving: try `exact?`, `apply?`, and `rw?`, and use `rg` over the pinned Mathlib
  checkout before introducing new lemmas.
- Type-check incrementally with the REPL or LSP; finish with the project's normal Lean or Lake
  build command.
- Prefer a short proof using a verified existing theorem over recreating library mathematics.

## Conventions

- Use `snake_case` for theorems and lemmas, `UpperCamelCase` for types and classes, and
  `lowerCamelCase` for terms. Namespaces describe mathematics, never chapters, task IDs, or theorem
  numbers. Follow established suffixes such as `_iff`, `_of_`, `_inj`, `_mono`, `_eq`, and `_apply`.
- Use the weakest sufficient typeclasses (`Semiring` before `Ring`, `Preorder` before
  `LinearOrder`) and `Finite` before `Fintype` when enumeration is unnecessary. Remove unused
  hypotheses and prefer named implicit arguments over positional `@foo _ _ _` calls.
- Prefer `calc` for chains, `ext`/`funext` for extensional equality, API lemmas over broad `unfold`,
  and `by classical` inside proofs rather than unnecessarily classical statements.
- Use `simp only [...]` for nonterminal simplification; unrestricted `simp` is appropriate when it
  closes a well-understood goal. Handle `0`, empty sets, and other degenerate cases explicitly when
  they change the argument.
- Match tactics to the problem: `positivity`, `omega`, `norm_num`, `gcongr`, `ring`, `field_simp`,
  `linarith`/`nlinarith`, `push_cast`/`norm_cast`, `simp_rw`, and `split_ifs` are preferable to
  manual or opaque tactic walls when applicable.
- Keep top-level declarations at column zero, proof bodies consistently indented, imports narrow,
  and public declarations documented when their purpose is not obvious. Follow the repository's
  configured line length.

## Lean pitfalls

- Division and inversion by zero are total; for example `0 / 0 = 0` and `Real.log 0 = 0` in Lean.
- Natural subtraction truncates. Track coercions deliberately and use cast tactics rather than
  relying on elaboration accidents.
- Treat `erw`, aggressive unfolding, global `open`, broad automation, custom syntax, and new
  `[simp]` lemmas as review points because they can hide fragility or alter downstream behavior.
