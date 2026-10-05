---
name: develop-plugin
description: >-
  Develop or maintain AutoformBot's CLI, servers, skills, manifests, tests,
  bundled example, or local installation. Use for plugin defects seen in
  consumer Lean projects; not for their mathematics.
---

# Develop Autoform from consumer nudges

Autoform is an example-based plugin for an independent formalization
repository. Use the bundled Cabannes thesis only as an executable consumer
example. State a consumer scenario, inspect the worktree, observe installed
behavior, and name a refactor's invariant.

Treat user nudges as product evidence. Distill each reusable insight into the
owning skill so future agents need less steering. Preserve the insight,
not the transcript. Add an assertion in `tests/test_skill_examples.py`. Keep
Cabannes-specific facts out of reusable plugin behavior.

For external protocols, prefer a maintained client library implementing
framing, server requests, encoding, and synchronization barriers. Keep
Autoform's adapter responsible for policy: clean startup, absolute deadlines,
resource ownership, limits, and error translation. Test
that boundary as a contract; duplicate wire protocol only when necessary.

Keep plugin and formalization roots distinct. Agents can infer routine details;
skills should preserve only non-obvious constraints.

Normally run:

```bash
make lint
make test
make check-example
```

Run `lake build` when Lean changes. Validate with skill-creator and
plugin-creator. Cachebust and reinstall only to test discovery in a new thread.
