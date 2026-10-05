---
name: develop-plugin
description: >-
  Maintain AutoformBot for consumer-project defects.
---

# Develop Autoform

Treat Autoform as an example-based plugin for an independent formalization. Use
the Cabannes thesis only as its executable example.

Inspect the worktree. State a consumer scenario and observe installed behavior.
Name a refactor's invariant.

Treat user nudges as product evidence. Encode reusable triggers, decisions, and
actions in the owning skill so future agents need less steering. Preserve the
insight, not the transcript. Add a focused `tests/test_skill_examples.py` assertion.

Keep behavior reusable and Cabannes-specific facts in its example or references.

Keep plugin and formalization roots distinct. Agents can infer routine details;
keep shared agent entrypoints concise and link details as on-demand references.

Indexes, revisions, and links share an evidence boundary. Retain descriptors;
repeated pathname reads are not a generation boundary. Read bounded output
markers before descendants and keep each marker schema in its owning feature.
Auto-detected links must match the blob at the stable detected commit.

Treat rewritten private declaration safety as fail-closed evidence: correlate
the official user name to its lexical declaration by source coordinates.

Run focused checks, then normally run:

```bash
make lint
make test
make check-example
```

Run `lake build` for changed example Lean results. Validate skills and manifests
with skill-creator and plugin-creator. Use cachebuster/reinstall only to test
discovery in a new thread. Report checks.
