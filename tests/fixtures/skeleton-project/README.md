# Skeleton fixture

A Mathlib-free Lean project for `tests/test_skeleton.py`. It is copied to a
temporary directory and built there, so the fixture itself is never modified.
`Skel/Defs.lean` holds definitions a statement can rest on and a helper lemma
that only a proof uses; `Skel/Main.lean` holds one proved theorem and one whose
proof is `sorry`, so the skeleton has to show `sorryAx` for it.
