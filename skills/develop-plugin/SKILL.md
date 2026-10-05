---
name: develop-plugin
description: >-
  Develop AutoformBot's CLI, servers, skills, manifests, tests, example, or
  installation for consumer-project defects.
---

# Develop Autoform from consumer nudges

Treat Autoform as an example-based plugin installed in an
independent formalization repository. Use the Cabannes thesis as an executable consumer example.

Inspect the worktree, state a consumer scenario, and observe installed behavior.
Name a refactor's invariant.

Treat user nudges during real work as product evidence. Distill reusable ones
into the owning skill as a trigger, decision rule, and action.
Ensure future agents need less steering.
Preserve the insight, not the transcript or consumer choice.
Add a focused test and acceptance assertion in `tests/test_skill_examples.py`.

Implement reusable plugin behavior. Keep Cabannes-specific facts in the example
and references; demonstrate outcomes without special-casing them.

Keep plugin and formalization roots distinct. Agents can infer routine details;
keep shared agent entrypoints concise and link command/schema details as on-demand references.

Source indexes, revisions, and links form one evidence boundary. Require
retained descriptors; repeated pathname reads are not a generation boundary.
Read bounded generated-output markers before descendants, keeping each marker
schema in its owning feature. Auto-detected links must match the blob at the
stable detected commit.

Run focused checks, then normally run:

```bash
make lint
make test
make check-example
```

Run `lake build` when example Lean results change. Validate edited skills and
the manifest with skill-creator and plugin-creator. Use cachebuster and reinstall
only to test installed discovery in a new thread. Report outcome and checks.
Treat rewritten private declaration safety as fail-closed evidence: correlate
the official user name to its lexical declaration by source coordinates.
