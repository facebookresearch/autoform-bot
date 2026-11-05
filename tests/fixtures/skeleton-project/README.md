# Skeleton fixture

A Mathlib-free Lean project for `tests/test_skeleton.py` and
`tests/test_lean_probe.py`, which copy it to a temporary directory and build it
there, so the fixture itself is never modified. Its `lean-toolchain` is the one
the two real-Lean CI jobs install (the REPL job pins its own) and the one
`tests/conftest.py`'s real-Lean gate checks for; other real-Lean tests copy or
read it.
