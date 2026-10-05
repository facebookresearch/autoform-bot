---
name: develop-plugin
description: >-
  Develop AutoformBot's CLI, servers, skills, manifests, tests, example, or
  installation for consumer-project defects.
---

# Develop Autoform from consumer nudges

Treat Autoform as an example-based plugin installed in an independent formalization
repository.

Inspect the worktree, state a consumer scenario, observe installed behavior,
and name the invariant.

Treat user nudges as product evidence. Put reusable triggers, decisions, and
actions in the owning skill so future agents need less steering. Preserve the
insight, not the transcript.
Add a focused test and acceptance assertion in `tests/test_skill_examples.py`.

Implement reusable behavior. Keep Cabannes-specific facts in the example and
references; demonstrate outcomes without special-casing them.

Keep plugin and formalization roots distinct. Agents can infer routine details;
keep shared agent entrypoints concise and link command/schema details as on-demand references.

Treat client-side scale as a publication contract. Material search stays
title-only—one record per page, without section or body records—unless measured
browser memory proves a wider index safe.

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
