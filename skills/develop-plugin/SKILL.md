---
name: develop-plugin
description: >-
  Develop AutoformBot's CLI, servers, skills, manifests, tests, example, or
  installation for consumer-project defects.
---

# Develop Autoform from consumer nudges

Treat Autoform as an example-based plugin in an independent formalization
repository. Use the Cabannes thesis as a consumer example.

Inspect the worktree, state a consumer scenario, observe installed behavior,
and name refactor invariants.

Treat user nudges as product evidence. Encode reusable triggers and decisions
so future agents need less steering. Preserve insight, not the transcript, and
add a focused assertion in `tests/test_skill_examples.py`.

For each Lean/Mathlib release, regenerate `production_module_roots` from Lake
package configs. Update the private creation bundle, catalog identity, and
complete `lake update` manifest together; run `lake build`.
A direct-Mathlib-only manifest is invalid.

Implement reusable plugin behavior. Keep Cabannes-specific facts in examples.

Keep plugin and formalization roots distinct. Agents can infer routine details;
keep skills to non-obvious constraints and fragile domain steps.

Run tests with `PYTHONDONTWRITEBYTECODE=1`. Never attest pytest
assertion-rewrite caches; remove them and rerun instead.

Normally run:

```bash
make lint
make test
make check-example
```

Run `lake build` when example Lean results change. Validate edited skills and
the manifest with skill-creator and plugin-creator. Reinstall only to test
installed discovery in a new thread.

Treat rewritten private declaration safety as fail-closed evidence: correlate
the official user name to its lexical declaration by source coordinates.
