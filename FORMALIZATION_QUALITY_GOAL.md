# Goal prompt: add fail-closed formalization quality to Autoform

This is the Phase 1 implementation prompt under the broader
[archive skill transport plan](ARCHIVE_SKILL_TRANSPORT_PLAN.md). Complete that
plan's Phase 0 authorization and manifest checks before copying archive material.
Deliver this work through P01, P02, and P03 from that plan rather than one
umbrella PR: skill contract first, deterministic CLI enforcement second, and
workflow/example integration third.

You are implementing the first quality-focused extension to AutoformBot. Work in
this repository and preserve its existing architecture: Markdown is the authored
source of truth, the Python CLI produces read-only derived views, and Lean remains
the authority for compilation and proof checking.

## Goal

Transfer the reusable, public parts of the `lean-formalization-quality` skill
into AutoformBot. The result must prevent a compiling but materially incorrect
source-to-Lean translation from being presented as reviewed work.

The implementation must be model-agnostic and usable from both Codex and Claude
Code. Do not copy Meta-internal transport, service, model, filesystem, or
publication assumptions into this repository.

## Why this skill comes first

Autoform already has strong project structure, dependency-DAG validation, Lean
declaration resolution, publication, human review, and rubric-based agent review.
Its largest uncovered risk is semantic: Lean can verify a proposition while the
proposition still mistranslates the source. A formalization-quality gate addresses
that risk before autonomous proving is expanded.

Do not transfer `lean-formalizer-profile`, `formalize-arxiv-paper`, or a prover
backend in this change. Model routing is environment-specific, and an end-to-end
generator should not be added before its outputs can be audited.

## Source provenance

The source archive used to design this change was:

- `math_lean_skills_agent_config_2026-08-31.zip`
- SHA-256: `9d38fe39237afdf673073fd6ebeb15f01514f033689edd56ba3b3251d611d7d3`
- source skill: `skills/lean-formalization-quality/`

Distill the policy; do not copy internal paths, model IDs, credentials,
benchmarks, or service instructions.

## Required design

### 1. Add a public skill

Add `skills/formalization-quality/SKILL.md` and a concise reference file under
`skills/formalization-quality/references/`.

The skill must require evidence for:

1. source fidelity;
2. forward and reverse clause coverage;
3. custom-definition and representation fidelity;
4. boundary and non-vacuity probes;
5. Lean compilation and declaration resolution;
6. proof integrity when a completed proof is claimed; and
7. authorship/provenance sufficient to distinguish human, model, and
   deterministic generation.

The skill must explicitly say that compilation proves the formal type, not that
the type matches the source. It must use conjunction over mandatory gates: one
blocked or missing required gate blocks acceptance. Reviewers may diagnose or
reject work but must not silently rewrite it and preserve the old authorship
claim.

Reuse or link the existing Agent Review faithfulness, proof-integrity, and code
quality references where their contracts already match. Avoid maintaining two
different definitions of the same gate.

### 2. Keep quality evidence in the Markdown article

Do not add quality fields to frontmatter. Autoform rejects unsupported keys, and
frontmatter is reserved for concise checked facts.

A formalizable roadmap leaf may instead contain this visible section:

```markdown
## Formalization quality

| Gate | Status | Evidence |
| --- | --- | --- |
| source-fidelity | passed | Compared with [source passage](../../sources/paper.md#theorem-2). |
| clause-coverage | passed | Binders, assumptions, and both conclusions mapped in both directions. |
| definition-fidelity | passed | Uses `Set.OrdConnected`; no project-local wrapper. |
| boundary-probes | passed | Checked the empty set and equality endpoint. |
| lean-validity | passed | `lake build MyProject.Theorem` and declaration resolution passed. |
| proof-integrity | not-applicable | Statement is formalized; proof is not claimed complete. |
| provenance | passed | Human-authored translation reviewed independently. |
```

Allowed statuses are `passed`, `blocked`, and `not-applicable`.
`not-applicable` requires a visible reason. A missing row is not equivalent to
`not-applicable`.

### 3. Add a deterministic quality checker

Add a CLI command named `autoform quality` rather than overloading structural
`autoform check` or making the currently broader `autoform audit` semantics
ambiguous.

The command must:

- accept a blueprint path and optional `--lean-root`;
- inspect formalizable roadmap leaves only;
- require the quality table once a leaf asserts `statement: formalized` or
  `proof: formalized`;
- require all seven canonical rows for a formalized statement;
- require `proof-integrity: passed` when `proof: formalized` is asserted;
- allow `proof-integrity: not-applicable` only when no completed proof is
  asserted;
- reject duplicate gates, unknown gates, unknown statuses, invisible/empty
  evidence, missing evidence, and `blocked` mandatory gates;
- reject `source-fidelity: not-applicable` for `origin: cited`;
- resolve cited evidence links locally without allowing paths to escape the
  blueprint;
- use `--lean-root` to confirm `lean-validity: passed` only when every asserted
  `lean:` declaration resolves; and
- make no filesystem or network changes.

Human evidence remains human judgment. The checker validates that the declared
contract is complete, visible, internally consistent, and locally resolvable; it
must not claim to prove semantic fidelity automatically.

Provide stable JSON output with article path, gate, status, evidence, and finding
codes. Use these finding codes unless implementation constraints justify a
documented change:

- `missing-quality-section`
- `missing-quality-gate`
- `duplicate-quality-gate`
- `unknown-quality-gate`
- `invalid-quality-status`
- `missing-quality-evidence`
- `blocked-quality-gate`
- `invalid-not-applicable`
- `unresolved-quality-link`
- `unresolved-quality-declaration`

### 4. Integrate without breaking empty projects

Update the Setup-generated verification workflow to run `autoform quality
blueprint --lean-root .`. An empty or planning-only blueprint must pass. A
formalized leaf without quality evidence must fail.

Update the bundled Cabannes example with honest evidence for its formalized
leaves. Do not invent source or build evidence merely to make the fixture pass.

Update:

- the CLI reference;
- Setup, Roadmap, Human Review, and Agent Review handoffs where relevant;
- both plugin manifests/descriptions if needed; and
- plugin-surface tests that enumerate shipped skills.

The new skill reviews or gates existing work. It does not generate statements,
write proofs, select a model, publish externally, or introduce autonomous
orchestration.

## Test plan

Write tests before or alongside each behavior. Every requirement below needs an
automated assertion.

### Skill and packaging tests

1. The Codex and Claude plugin surfaces discover `formalization-quality`.
2. Its `SKILL.md`, required reference, and OpenAI metadata exist.
3. The skill contains no internal-only tokens, including `internalfb`,
   `manifold://`, `metacode`, `MAST`, `RIFT`, `Pixelcloud`, `PingMe`,
   `LLAMA_API_KEY`, or hard-coded `/users/` paths.
4. Existing skill examples and plugin manifests still validate.

### Parser unit tests

Create focused tests for:

1. one valid seven-row table;
2. a missing section;
3. each missing canonical row;
4. a duplicate row;
5. an unknown row;
6. each invalid status;
7. blank, comment-only, hidden, code-only, and empty-link evidence;
8. `not-applicable` with and without a rationale;
9. a table inside a fence or HTML comment;
10. malformed Markdown that renders differently from its source text; and
11. duplicate matching tables where only one belongs to the required section.

Use rendered Markdown/HTML semantics where visibility matters. Do not accept a
regex-only implementation that treats hidden evidence as visible.

### Policy tests

Test these complete article cases:

| Case | Expected result |
| --- | --- |
| Planning-only formalizable leaf, no table | pass |
| `statement: formalized`, valid table | pass |
| `statement: formalized`, no table | fail |
| `proof: formalized`, proof integrity passed | pass |
| `proof: formalized`, proof integrity N/A | fail |
| Cited statement, source fidelity N/A | fail |
| Background lemma, justified source fidelity N/A | pass |
| Any mandatory gate blocked | fail |
| Resolved local evidence link | pass |
| Escaping or missing evidence link | fail |
| Resolved `lean:` name with `--lean-root` | pass |
| Missing `lean:` name with Lean-validity passed | fail |

### CLI integration tests

1. Human-readable success output includes checked article and gate counts.
2. Human-readable failure output identifies the exact article and gate.
3. `--json` output is deterministic and matches the documented schema.
4. Success exits `0`; every blocking finding exits nonzero.
5. The command is read-only: hash the fixture tree before and after and require
   equality.
6. Paths outside the blueprint are never read as evidence targets.

### Scaffold and example tests

1. `autoform init` still produces a blueprint that passes `autoform check` and
   `autoform quality` before any mathematics is added.
2. The generated verification workflow invokes the quality command.
3. The Cabannes fixture passes check, quality, render, and strict MkDocs build.
4. A copy of the fixture with one evidence row removed fails quality checking.

### Regression suite

All existing tests must remain green. Add the new quality command to the normal
development checks and run:

```bash
make lint
make test
make check-example
uv run autoform quality \
  skills/setup/assets/cabannes-thesis-project/blueprint \
  --lean-root skills/setup/assets/cabannes-thesis-project
```

Also run the plugin validators required by the repository and the
plugin-development instructions. If the local plugin is reinstalled for a live
smoke test, use the repository's cachebuster/update flow rather than hand-editing
a marketplace.

## Phased implementation plan

### Phase 1: freeze the contract

- Write the public skill and reference.
- Document the Markdown table schema and CLI JSON schema.
- Add packaging and policy-contract tests.

Exit criterion: the skill is discoverable, model-agnostic, contains no internal
dependencies, and its contract examples are asserted by tests.

### Phase 2: implement parsing and policy

- Add a small quality-evidence parser separate from graph construction.
- Add typed result objects and deterministic finding codes.
- Add `autoform quality` and its JSON output.
- Cover all parser, policy, path-safety, and read-only cases above.

Exit criterion: every parser, policy, and CLI integration test passes, including
all negative fixtures.

### Phase 3: integrate workflows and review

- Update generated CI, documentation, plugin manifests, skill handoffs, and the
  Cabannes fixture.
- Ensure Human Review shows evidence while Agent Review judges its mathematical
  substance.
- Preserve the distinction between machine-validated evidence structure and
  human/model semantic judgment.

Exit criterion: freshly scaffolded projects and the complete bundled example
pass the documented workflow, while deliberately corrupted copies fail for the
expected finding code.

### Phase 4: release validation

- Run lint, the full test suite, example checks, plugin validation, and a local
  CLI smoke test.
- Inspect the Git diff for accidental generated files or internal references.
- If a local plugin installation is updated, bump the cachebuster and reinstall
  through the supported CLI flow, then verify discovery in a fresh session.

Exit criterion: all commands exit zero, the worktree contains only intended
changes, and every negative test fails for the intended reason rather than an
unrelated parser or setup error.

## Non-goals

- Do not add a model router or pin a model.
- Do not import Meta-internal services or credentials.
- Do not add autonomous proof execution to `main`.
- Do not claim that a completed checklist mechanically proves source fidelity.
- Do not create a JSON database that competes with authored Markdown.
- Do not weaken existing Lean build, axiom, or kernel checks.
- Do not make planning-only blueprints fail quality validation.

## Definition of done

The work is complete only when:

1. the new skill is packaged and documented for both hosts;
2. the quality command enforces the visible Markdown contract;
3. every positive and negative case in this prompt has an automated test;
4. the generated CI uses the command without breaking empty projects;
5. the complete example passes and a corrupted example fails predictably;
6. all existing and new tests, lint, documentation builds, and plugin validators
   pass; and
7. no internal-only dependency or unsupported quality claim appears in the
   shipped plugin.
