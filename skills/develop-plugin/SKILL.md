---
name: develop-plugin
description: Maintain AutoformBot plugin infrastructure.
---

# Develop Autoform

Treat Autoform as an example-based plugin for an independent formalization
repository.

Inspect the worktree: state a consumer scenario, behavior, and invariant. Treat
user nudges as product evidence; preserve insight, not the transcript, in a
focused assertion so future agents need less steering.

For each Lean/Mathlib release, regenerate `production_module_roots` from Lake
package configs. Update the private creation bundle, catalog identity, and
complete `lake update` manifest together; run `lake build`.
A direct-Mathlib-only manifest is invalid.

Keep behavior reusable and Cabannes-specific facts in examples. Keep plugin
and formalization roots distinct. Agents can infer routine details. Keep shared
agent entrypoints concise; link details as on-demand references.

Source indexes, revisions, and links are one evidence boundary: retain
descriptors because repeated pathname reads are not a generation boundary.
Read bounded outputs before descendants, with each marker schema in its owning
feature. Auto-detected links must match the blob at the stable detected commit.
Let toolchain-matched Lean parse names and decide semantic facts; Python handles
bounded transport and presentation.

Normally run:

```bash
make lint
make test
make check-example
```

Validate skills and manifests with skill-creator and plugin-creator. Use
cachebuster/reinstall only for discovery in a new thread.
Treat rewritten private declaration safety as fail-closed evidence: correlate
the official user name to its lexical declaration by source coordinates.
