---
name: develop-plugin
description: Maintain AutoformBot's code, skills, tests, examples, and installation.
---

# Develop Autoform

Treat Autoform as an example-based plugin for an independent formalization
repository. State a consumer scenario and invariant. Treat user nudges as
product evidence. Fix the product first: the command's output, error message, or
default. Add a skill sentence only when behavior cannot carry the fix, in the
one skill that owns the step. Tests check behavior, links, and that documented
commands parse; never add a test that pins a skill's wording, except the short
list of safety rules in `tests/test_skill_examples.py`.

Keep Cabannes-specific facts in examples; keep plugin and formalization roots
distinct. Agents can infer routine details; keep shared agent entrypoints concise
and link on-demand references.

For claims/views/source evidence/mirrors/pins/releases/explorers, read
[repository contracts](references/repository-contracts.md).

Run:

```bash
make lint
make test
make check-example
```

Validate skills and manifests with skill-creator and plugin-creator. Test
cachebuster/reinstall discovery only in a new thread.
