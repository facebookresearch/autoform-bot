---
name: develop-plugin
description: Maintain AutoformBot's code, skills, tests, examples, and installation.
---

# Develop Autoform

Treat Autoform as an example-based plugin for an independent formalization.
State a consumer scenario and invariant. Treat user nudges as product evidence;
preserve insight, not the transcript, in a focused test so
future agents need less steering.

Keep Cabannes-specific facts in examples and plugin and formalization roots
distinct. Agents can infer routine details; keep shared agent entrypoints concise
and link on-demand references.

Keep imports permissive: consumer Lean uses Formalize's
`autoform work import-impact`; plugin changes preserve it.

For each Lean/Mathlib release, regenerate `production_module_roots` from Lake
package configs. Update the private creation bundle, catalog identity, and
complete `lake update` manifest together; run `lake build`.
A direct-Mathlib-only manifest is invalid.

Source indexes, revisions, and links form one evidence boundary: retain
descriptors because repeated pathname reads are not a generation boundary.
Read bounded outputs before descendants, keep each marker schema in its owning
feature, and require links to match the blob at the stable detected commit.

For claims/views/mirrors/pins, read
[repository contracts](references/repository-contracts.md).

Normally run:

```bash
make lint
make test
make check-example
```

Validate skills and manifests with skill-creator and plugin-creator. Test
cachebuster/reinstall discovery only in a new thread.

Treat rewritten private declaration safety as fail-closed evidence: correlate
the official user name to its lexical declaration by source coordinates.
