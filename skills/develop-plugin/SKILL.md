---
name: develop-plugin
description: >-
  Maintain AutoformBot for consumer-project defects.
---

# Develop Autoform

Treat Autoform as an example-based plugin for an independent formalization. Use
the Cabannes thesis only as its executable example.

Inspect the worktree; state a consumer scenario, installed behavior,
and invariant. Treat user nudges as product evidence. Preserve the insight,
not the transcript, as an owning-skill rule, focused test, and assertion;
future agents need less steering.

Implement reusable behavior. Keep Cabannes-specific facts in examples. Keep
plugin and formalization roots distinct. Agents can infer routine details.
Keep shared agent entrypoints concise and link command/schema details as
on-demand references.

Source indexes, revisions, and links form one evidence boundary. Require
retained descriptors; repeated pathname reads are not a generation boundary.
Read bounded generated-output markers before descendants, keeping each marker
schema in its owning feature. Auto-detected links must match the blob at the
stable detected commit.

Treat rewritten private declaration safety as fail-closed evidence: correlate
the official user name to its lexical declaration by source coordinates.

Run focused checks, then normally run:

```bash
make lint
make test
make check-example
```

Run `lake build` when example Lean results change. Validate edited skills and
the manifest with skill-creator and plugin-creator. Use cachebuster and
reinstall only to test installed discovery in a new thread.
Report outcome and checks.
