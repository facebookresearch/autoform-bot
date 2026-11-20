- **`autoform work list --lean-root` and `work context --lean-root`**,
  deprecated in 0.7.0, are gone: `search --lean-root` prints a Lean name's
  file and line. `lean_targets[].source_file` stays in `autoform-work/v2`,
  always null (#768).
