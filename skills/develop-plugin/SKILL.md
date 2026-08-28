---
name: develop-plugin
description: >-
  Develop or maintain AutoformBot's CLI, servers, skills, manifests, tests,
  bundled example, or local installation. Use for plugin defects seen in
  consumer Lean projects; not for their mathematics.
---

# Develop Autoform from consumer nudges

Treat Autoform as an example-based plugin whose product is installed behavior
in an independent formalization repository. Use the bundled Cabannes thesis
repository only as an executable consumer example.

Inspect the worktree, state a consumer scenario, and observe installed behavior.
For a refactor, name the invariant. Trace needed layers.

Treat user nudges during real work as product evidence. Distill reusable ones
into the owning skill as a trigger, decision rule, and action.
Ensure future agents need less steering.
Preserve the insight, not the transcript or consumer choice.
Add a focused test and acceptance assertion in `tests/test_skill_examples.py`.

Implement reusable plugin behavior. Keep Cabannes-specific facts in the example
and references; demonstrate outcomes without special-casing them.

Keep plugin and formalization roots distinct. Agents can infer routine details;
keep skills to non-obvious constraints and fragile domain steps.

Run focused checks, then normally run:

```bash
make lint
make test
make check-example
```

Run `lake build` when example Lean results change. Validate edited skills and
the manifest with skill-creator and plugin-creator. Use cachebuster and reinstall
only to test installed discovery in a new thread. Report outcome and checks.
