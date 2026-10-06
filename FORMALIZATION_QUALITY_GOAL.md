# Goal prompt: add fail-closed formalization quality to Autoform

This is the Phase 1 implementation prompt under the broader
[archive skill transport plan](ARCHIVE_SKILL_TRANSPORT_PLAN.md). Complete that
plan's Phase 0 authorization and manifest checks before copying archive material.
Deliver this work through P01, P02, and P03 from that plan rather than one
umbrella PR: skill contract first, deterministic CLI enforcement second, and
workflow/example integration third.

You are implementing AutoformBot's first repository-enforced quality acceptance
layer. Work in this repository and preserve its existing architecture: Markdown
is the authored source of truth, the quality command preserves authored files,
and Lean remains the authority for compilation and proof checking.

## Goal

Transfer the reusable, public parts of the `lean-formalization-quality` skill
into AutoformBot. The result must prevent a source-to-Lean translation from
being presented as reviewed merely because it compiles: acceptance also needs
current, positive source-faithfulness evidence.

The implementation must be model-agnostic and discoverable from Codex, Claude
Code, and Muse. Do not copy Meta-internal transport, service, model, filesystem,
or publication assumptions into this repository.

## Why this skill comes first

Autoform already has project structure, dependency-DAG validation,
environment-backed skeleton extraction, hash-bound read-back review,
Markdown-native Formalize, publication, and rubric-based human or agent review.
The remaining gap is enforcement: a current build/trust artifact and an
independent source-faithfulness verdict are not yet bound into one visible
Markdown contract and CI acceptance gate. Lean can verify a proposition while
the proposition still mistranslates the source; this layer requires both kinds
of evidence without pretending either one proves the other.

Do not transfer `lean-formalizer-profile`, `formalize-arxiv-paper`, or a prover
backend in this change. Model routing is environment-specific, and an end-to-end
generator should not be added before its outputs can be audited.

## Existing foundation and trust split

Do not rebuild capabilities already merged on `main`:

- P01 adds the gate and handoff contract over Agent Review's existing
  `faithfulness.md`, `readback-faithfulness.md`, and `proof-integrity.md`
  references. Link and reuse them; do not create a second faithfulness rubric,
  read-back rubric, or evidence schema.
- P02 owns visible quality-table parsing, default-deny applicability, CLI
  findings, and one narrow skeleton extension that lets external
  `mathlib_declaration:` roots produce the existing skeleton/packet format from
  their recorded `mathlib_file:`. Reuse the audit formalization-evidence
  predicate and existing rendered-Markdown visibility and contained-link
  helpers rather than creating parallel selectors or path rules. For local
  `lean:` targets delegate compiled validity to the existing environment-backed
  skeleton freshness/probe contract. Do not treat `index_project` or another
  lexical scan as Lean validity, and do not create a second skeleton format.
- P03 orders generated verification as `lake build`, skeleton-backed
  `autoform quality`, then the existing integrity audit. Artifact-dependent
  quality must not run before the build or in a Pages job without an equivalent
  build. Preserve the integrity audit's `open_statements` policy and generated
  assumptions contract rather than replacing it with an unconditional
  zero-sorry check. CI recomputes current skeleton/hash bindings and validates
  an already recorded independent verdict; it never manufactures a semantic
  verdict or runs a reviewer merely to make the gate pass.
- PRs #136 and #138 already define revision-impact claims, retracted
  statements, and the project-wide open-statement policy. Quality consumes
  those facts rather than inventing another revision or openness state.

The trust boundary is conjunctive. Skeleton extraction establishes the current
compiled declaration's elaborated meaning and trust context, but not source
fidelity. Direct faithfulness/read-back review judges whether that meaning
matches the source, but not whether current code builds. Quality records and
enforces both; missing, stale, unresolved, or blocked evidence cannot pass.

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

P01 also makes that contract discoverable from Codex and Claude Code and adds
its explicit command entry to `.muse-plugin/plugin.json`. Host packaging belongs
to P01 so the skill-only PR is independently usable and testable; update the
plugin-surface tests there.

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
quality references, including read-back faithfulness, where their contracts
already match. Avoid maintaining two different definitions of the same gate.

### 2. Keep quality evidence in the Markdown article

Do not add quality fields to frontmatter. Autoform rejects unsupported keys, and
frontmatter is reserved for concise checked facts.

A formalization-evidence-bearing roadmap leaf must contain this visible section:

```markdown
## Formalization quality

| Gate | Status | Evidence |
| --- | --- | --- |
| source-fidelity | passed | [Verdict](../../reviews/theorem-2/verdict.json) and [read-back d1](../../reviews/theorem-2/d1.md) agree with the current verified bundle. |
| clause-coverage | passed | Binders, assumptions, and both conclusions mapped in both directions. |
| definition-fidelity | passed | Uses `Set.OrdConnected`; no project-local wrapper. |
| boundary-probes | passed | Checked the empty set and equality endpoint. |
| lean-validity | passed | Current build and environment-backed skeleton resolution passed. |
| proof-integrity | not-applicable | Statement is formalized; proof is not claimed complete. |
| provenance | passed | author=human:alice; coordinator=human:alice; auditor=model:audit-run-7; judge=human:bob; deterministic=skeleton/v4. |
```

Allowed statuses are `passed`, `blocked`, and `not-applicable`.
`not-applicable` requires a visible reason. A missing row is not equivalent to
`not-applicable`.

Every quality subject must declare `origin` explicitly. Missing origin produces
`missing-quality-origin`, even when links, prose, or other evidence are present.
The mapping is total: `origin: cited` requires `source-fidelity: passed` with
the current hash-bound `agrees` bundle; `origin: bridged` and
`origin: background` require justified `source-fidelity: not-applicable`.
`source-fidelity: blocked` remains a valid failing status for every origin and
produces `blocked-quality-gate`. A cited N/A uses the matrix's
`invalid-not-applicable`; `invalid-source-fidelity-origin` is reserved for a
nonblocked `passed` status where bridged/background requires N/A. Unsupported
origin values fail graph validation before quality policy runs.
Emit exactly one source-fidelity status code, in this order: missing origin;
`blocked`; invalid N/A under the matrix; invalid nonblocked origin/status pair;
then bundle validation for cited `passed`.

Applicability is default-deny:

| Gate | When `not-applicable` is allowed |
| --- | --- |
| source-fidelity | Only with explicit `origin: background` or `origin: bridged` and a visible rationale explaining why there is no direct source translation. `origin: cited` requires `passed`; omitted origin is invalid. |
| clause-coverage | Never. A trivial clause map is still recorded as `passed`. |
| definition-fidelity | Never. Evidence may state that no custom representation is used, but the status remains `passed`. |
| boundary-probes | Never. Evidence may justify that no nontrivial boundary exists, but the status remains `passed`. |
| lean-validity | Never. |
| proof-integrity | Only when no completed proof is claimed. This includes a theorem explicitly open under `open_statements: allowed`, with a visible rationale; the existing integrity audit still governs allowed assumptions. `proof: formalized` and a proof-bearing `mathlib: true` target require `passed`. Bare `lean:` or `mathlib_declaration:` evidence without either completion assertion does not claim a completed proof. |
| provenance | Never. |

Any case not explicitly allowed by this matrix is
`invalid-not-applicable`. Missing `origin` never grants an exemption.

Proof applicability and declaration intent use freshly resolved, hash-bound
`DeclarationSkeleton.kind` values and proof facts, never the authored
`declaration` label. P02 must version the
skeleton schema and add `DeclarationSkeleton.type_is_prop`, computed from
Lean's `Meta.isProof` for each root and included in the verified artifact hash.
For `mathlib: true`, aggregate every compiled root. A root is proof-bearing when
its kind is `theorem` or `axiom`, or when `type_is_prop: true`; this includes
data-valued axioms and proof-valued `def`/`opaque` roots. Only roots with
`type_is_prop: false` and a definition-like kind may contribute to an
all-definition-like set that uses justified proof-integrity N/A when no other
proof completion is claimed. A mixed set is proof-bearing.

Supported compiled kinds are `theorem`, `axiom`, `def`, `instance`, `opaque`,
`inductive`, `class`, `structure`, `constructor`, `recursor`, and `quot`.
Definition-like means exactly that set minus `theorem` and `axiom`.
`unknown`, any future kind outside that set, a missing `type_is_prop`, an
unresolved root, or an incomplete skeleton produces
`unsupported-quality-declaration-kind` or `unresolved-quality-declaration` and
can never grant N/A. A `theorem` with `type_is_prop: false` is internally
inconsistent and produces `inconsistent-quality-declaration-kind`; an `axiom`
with false remains conservatively proof-bearing.

Compare every compiled root against one centralized declaration-intent
compatibility map. Trim and case-fold the authored value and ignore only the
supported Lean modifiers `private`, `protected`, `noncomputable`, `partial`,
`unsafe`, `scoped`, and `local`. Normalize `theorem`, `lemma`, `corollary`, and
`proposition` to compiled `theorem`; normalize `def`, `definition`, `abbrev`,
and `irreducible_def` to compiled `def`; map `axiom`, `class`, `inductive`,
`instance`, `opaque`, and `structure` directly. A missing authored intent skips
only this compatibility comparison and never changes compiled proof
applicability. Any present unknown intent or incompatible root produces
`quality-target-kind-mismatch`, so relabeling a theorem as a definition cannot
weaken the proof gate.

`source-fidelity: passed` is not satisfied by bare prose such as “compared with
the source.” For an `origin: cited` article, its Evidence cell must link to a
local `autoform-readback-faithfulness-verdict/v1` artifact whose decision is
`agrees` and whose skeleton, packet, read-back, passage, and article-review
hashes match a freshly recomputed verified bundle. The cell must also link one
raw read-back Markdown file for each declaration, in verdict order. P02 hashes
those exact bytes and joins them to the verdict, while the recomputed skeleton,
packet, and passage manifests supply the remaining hashes. It checks artifact
existence, canonical schema, order, verdict, and current hash binding without
re-judging the mathematics. A missing raw read-back, stale hash, or decision of
`review`, `unknown`, or `disagrees` blocks acceptance. Source passages continue
to use the existing `## Sources` line-locator and passage-manifest contract;
quality adds no parallel review record or source-location schema. A prose-only
direct Agent Review may guide a human but cannot satisfy the deterministic
`passed` gate.

For `origin: cited`, the current bundle must contain a nonempty extracted
passage and its canonical contained `#L<start>-L<end>` locator. A nonnull review
hash with `passage: null` is insufficient and produces
`missing-quality-passage`.

The cited verdict must bind identity as well as bytes. The subject must have a
nonempty durable `article_id`; otherwise emit `missing-quality-article-id`.
The verdict's `item` must equal that exact ID, or emit
`quality-verdict-item-mismatch`. Every declaration must have a nonempty raw
read-back, or emit `missing-quality-readback`. An overall `agrees` decision is
valid only when the worst decision implied by every
`discrepancies[].category` under the read-back rubric's ordering is `agrees`.
Require a nonempty discrepancies array, recompute its decision, and require it
to equal `decision`; reject unknown categories. Require `equivalence_to_settle`
to be null unless the recomputed
decision is `review`, and nonempty for `review`. Require `passage_card`,
`read_back_card`, and the human-readable `verdict` to be present and
structurally valid, but never infer status by scanning their prose. Any
contradiction produces `inconsistent-quality-verdict`. Content hashes alone
cannot authorize reusing a verdict for a different article.

`provenance: passed` evidence must visibly record
`author=<human|model>:<local-id>`,
`coordinator=<human|model>:<local-id>`,
`auditor=<human|model>:<local-id>`,
`judge=<human|model>:<local-id>`, and
`deterministic=<tool-or-schema-list>`. For cited read-back evidence, author,
blind auditor, and faithfulness judge must be distinct; coordinator, blind
auditor, and judge must also be pairwise distinct so the auditor cannot receive
the coordinator's source context. The author may be the trusted coordinator.
For a bridged or background article with no blind read-back,
`auditor=not-used:<reason>` is allowed, but the direct judge must still differ
from the author. The checker validates this grammar and separation; it does not
authenticate the local identities. Do not hide them in an undefined external
review log.

These hashes are drift checksums, not signatures. The verdict schema does not
carry or authenticate reviewer identity, and the deterministic checker cannot
defend against malicious candidate code forging its own local evidence;
provenance identity and independence remain review/governance judgments. It
does reliably reject missing, stale, malformed, or explicitly non-passing
evidence.

### 3. Add a deterministic quality checker

Add a CLI command named `autoform quality` rather than overloading structural
`autoform check` or making the currently broader `autoform audit` semantics
ambiguous.

The command must:

- accept a blueprint path and `--lean-root` for every
  formalization-evidence-bearing project;
- reuse the existing audit formalization-evidence predicate independently of
  `declaration`: any
  article with `statement: formalized`, `proof: formalized`, `mathlib: true`, a
  nonempty `lean:`, `mathlib_declaration:`, or `mathlib_file:` field;
- require the quality table and all seven canonical rows for every
  formalization-evidence-bearing leaf, including one whose `declaration` field
  is absent;
- treat `declaration` alone as planning, not formalization evidence;
- require at least one nonempty `lean:` or `mathlib_declaration:` environment
  target for any formalization evidence. Status flags and `mathlib_file:`
  trigger the gate but cannot by themselves satisfy Lean validity;
- require every external `mathlib_declaration:` target to carry a matching
  `mathlib_file:` from the pinned Mathlib checkout; declaration lookup without
  an import/source module cannot produce review evidence;
- emit `invalid-quality-subject` when a container carries any formalization
  evidence instead of filtering that invalid subject out;
- emit `retracted-quality-subject` for `statement: retracted`; retraction is a
  work/revision state, never accepted quality evidence;
- enforce the complete default-deny applicability matrix above, including
  `proof-integrity: passed` for every completed-proof claim;
- reject duplicate gates, unknown gates, unknown statuses, invisible/empty
  evidence, missing evidence, and `blocked` mandatory gates;
- resolve cited evidence links locally without allowing paths to escape the
  blueprint;
- accept `lean-validity: passed` only after a current build/freshness check and
  Lean-environment resolution of every target; a lexical source hit never
  suffices;
- reuse the existing skeleton freshness and environment probe for local
  `lean:` targets. Extend that same skeleton format and packet pipeline to
  external `mathlib_declaration:` roots, importing the recorded
  `mathlib_file:`, verifying the declaration-to-module match in the pinned
  environment, and emitting the same signature, semantic, packet, and hash
  fields. Do not substitute a lexical lookup or a parallel external-evidence
  format;
- return `unverified-lean-validity` when the Lean root, build, or fresh artifact
  evidence is absent, stale, or failed, and
  `unresolved-quality-declaration` when a name is absent from the compiled
  environment; and
- make no authored blueprint or Lean-source changes, no network calls, and no
  durable quality-output changes. The existing skeleton freshness command may
  refresh `.lake` hash metadata.

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
- `invalid-quality-evidence`
- `stale-quality-evidence`
- `blocked-quality-gate`
- `invalid-not-applicable`
- `invalid-quality-subject`
- `retracted-quality-subject`
- `missing-quality-origin`
- `invalid-source-fidelity-origin`
- `missing-quality-article-id`
- `missing-quality-target`
- `missing-quality-passage`
- `missing-quality-readback`
- `quality-verdict-item-mismatch`
- `inconsistent-quality-verdict`
- `unresolved-quality-link`
- `unresolved-quality-declaration`
- `quality-target-kind-mismatch`
- `unsupported-quality-declaration-kind`
- `inconsistent-quality-declaration-kind`
- `unverified-lean-validity`

### 4. Integrate without breaking empty projects

Update both Setup-generated verification and Pages publication so `lake build`,
then `autoform quality blueprint --lean-root .`, then the existing policy-aware
integrity audit succeed before render or deploy. Cover workflows emitted by
both legacy `autoform init` and primary `autoform project new`. A separate
failing verification workflow is not a deployment gate by itself. An empty or
declaration-only planning blueprint must pass. A
formalization-evidence-bearing leaf without quality evidence must fail, and
Pages must not publish it.

The bundled Cabannes example currently lacks exact line-located source passages
and recorded read-back verdicts for its cited completed leaves. P03 must add
authorized frozen source passages with canonical `#L<start>-L<end>` locators,
then record genuinely independent read-backs and `agrees` verdicts bound to the
current skeletons before adding passing quality tables. CI validates those
already-recorded artifacts; it does not generate verdicts. If that evidence
cannot be obtained honestly, P03 remains draft or blocked. Do not invent,
self-approve, or weaken evidence merely to make the fixture pass.

Update:

- the CLI reference;
- Setup, Roadmap, Formalize, Human Review, and Agent Review handoffs where
  relevant.

The new skill reviews or gates existing work. It does not generate statements,
write proofs, select a model, publish externally, or introduce autonomous
orchestration.

## Test plan

Write tests before or alongside each behavior. Every requirement below needs an
automated assertion.

### Skill and packaging tests

1. The Codex, Claude Code, and Muse plugin surfaces discover
   `formalization-quality`; Muse has an explicit command entry.
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
| Declaration-only planning leaf, no table | pass |
| `statement: formalized`, valid table | pass |
| `statement: formalized`, no declaration and no table | fail |
| `statement: formalized`, complete table but no environment target | fail |
| `mathlib: true`, no statement/proof and no table | fail |
| `mathlib: true`, complete table but no environment target | fail |
| Nonempty `lean:`, no other completion field and no table | fail |
| Nonempty `lean:` only, current target, proof integrity N/A | pass |
| Nonempty `mathlib_declaration:` only, no table | fail |
| `mathlib_declaration:` with a complete table but no `mathlib_file:` | fail |
| Resolved `mathlib_declaration:` plus matching `mathlib_file:`, proof integrity N/A | pass |
| Cited Mathlib target with current external skeleton/read-back bundle | pass |
| Mathlib declaration absent from or mismatched with `mathlib_file:` | fail |
| `mathlib_file:` resolves outside the pinned Mathlib checkout or to a local shadow | fail |
| External Mathlib artifact is stale relative to its recorded source/module | fail |
| Nonempty `mathlib_file:` only, no table | fail |
| Nonempty `mathlib_file:` with a complete table but no environment target | fail |
| Any formalization evidence on a container | fail |
| `statement: retracted`, with or without a complete table | fail |
| `proof: formalized`, proof integrity passed | pass |
| `proof: formalized`, proof integrity N/A | fail |
| Cited statement, source fidelity N/A | fail (`invalid-not-applicable`) |
| Bridged statement, source fidelity N/A with bridge rationale | pass |
| Bridged statement, source fidelity N/A without rationale | fail (`invalid-not-applicable`) |
| Bridged statement, source fidelity passed | fail (`invalid-source-fidelity-origin`) |
| Omitted origin, source fidelity N/A | fail (`missing-quality-origin`) |
| Omitted origin, source fidelity passed with otherwise current evidence | fail (`missing-quality-origin`) |
| Background lemma, justified source fidelity N/A | pass |
| Background lemma, source fidelity N/A without rationale | fail (`invalid-not-applicable`) |
| Background lemma, source fidelity passed | fail (`invalid-source-fidelity-origin`) |
| Any explicit origin, source fidelity blocked | fail (`blocked-quality-gate`) |
| Cited source fidelity passed with a current hash-bound `agrees` verdict | pass |
| Source fidelity passed with bare self-attestation prose | fail |
| Cited verdict `item` differs from the subject `article_id` | fail (`quality-verdict-item-mismatch`) |
| Cited subject has no durable `article_id` | fail (`missing-quality-article-id`) |
| Cited verdict links an empty raw read-back | fail (`missing-quality-readback`) |
| Overall `agrees` contradicts discrepancy-category decision ordering | fail (`inconsistent-quality-verdict`) |
| `equivalence_to_settle` is inconsistent with the recomputed decision | fail (`inconsistent-quality-verdict`) |
| `passage_card`, `read_back_card`, or human-readable verdict is missing or malformed | fail (`inconsistent-quality-verdict`) |
| Verdict omits, reorders, or mismatches a linked raw read-back | fail |
| Cited verdict has a review hash but no current passage/line locator | fail |
| Source fidelity verdict carries a stale article review hash | fail |
| Read-back decision is `review`, `unknown`, or `disagrees` | fail |
| Clause coverage N/A | fail |
| Definition fidelity N/A | fail |
| Boundary probes N/A | fail |
| Lean validity N/A | fail |
| Provenance N/A | fail |
| Cited provenance has all five fields and the required role separation | pass |
| Provenance has a missing role or reuses a required independent identity | fail |
| Coordinator and blind auditor have the same identity | fail |
| Bridged/background provenance has justified `auditor=not-used`, distinct judge | pass |
| Statement-only claim, proof integrity N/A | pass |
| Explicitly allowed open theorem, proof integrity N/A with rationale | pass |
| Open theorem when project policy forbids open statements | fail in integrity audit |
| Mathlib theorem claim, proof integrity N/A | fail (`invalid-not-applicable`) |
| Mathlib data definition, `type_is_prop: false`, proof integrity N/A | pass |
| Mathlib predicate definition, `type_is_prop: false`, proof integrity N/A | pass |
| Proof-valued Mathlib `def`, `type_is_prop: true`, proof integrity N/A | fail (`invalid-not-applicable`) |
| Proof-valued Mathlib `opaque`, `type_is_prop: true`, proof integrity N/A | fail (`invalid-not-applicable`) |
| Mathlib theorem mislabeled as a definition | fail (`quality-target-kind-mismatch`) |
| Mathlib definition mislabeled as a theorem | fail (`quality-target-kind-mismatch`) |
| Mathlib definition then theorem roots, no authored intent, proof integrity N/A | fail (`invalid-not-applicable`) |
| Mathlib theorem then definition roots, no authored intent, proof integrity N/A | fail (`invalid-not-applicable`) |
| Mathlib axiom plus definition roots, no authored intent, proof integrity N/A | fail (`invalid-not-applicable`) |
| All-definition Mathlib roots with `type_is_prop: false`, proof integrity N/A | pass |
| Valid Mathlib definition plus unresolved root, proof integrity N/A | fail (`unresolved-quality-declaration`) |
| Valid Mathlib definition plus unsupported root kind, proof integrity N/A | fail (`unsupported-quality-declaration-kind`) |
| Data-valued Mathlib axiom, `type_is_prop: false`, proof integrity N/A | fail (`invalid-not-applicable`) |
| Mathlib theorem kind with `type_is_prop: false` | fail (`inconsistent-quality-declaration-kind`) |
| Unknown authored declaration intent | fail (`quality-target-kind-mismatch`) |
| Any mandatory gate blocked | fail |
| Resolved local evidence link | pass |
| Escaping or missing evidence link | fail |
| Freshly built, environment-resolved `lean:` target | pass |
| Name found lexically but body fails Lean | fail |
| Old `.olean` after changing a valid body to a broken body | fail |
| Lean validity passed without `--lean-root` | fail |
| Name missing from the compiled environment | fail |

The broken-body fixture must contain a real invalid declaration, for example
`theorem Broken : True := by definitely_not_a_tactic`. Assert that the lexical
index sees `Broken` while quality still rejects it, both with no artifact and
with a stale `.olean` from the formerly valid body.

### CLI integration tests

1. Human-readable success output includes checked article and gate counts.
2. Human-readable failure output identifies the exact article and gate.
3. `--json` output is deterministic and matches the documented schema.
4. Success exits `0`; every blocking finding exits nonzero.
5. The command does not change authored blueprint or Lean files and writes no
   durable quality report. Hash tracked/authored files before and after; exclude
   `.lake` build metadata that the reused freshness check may update.
6. Paths outside the blueprint are never read as evidence targets.

### Scaffold and example tests

1. `autoform init` and `autoform project new` each produce a blueprint that
   passes `autoform check` and `autoform quality` before mathematics is added.
2. Both generators' verification and Pages workflows order `lake build`,
   skeleton-backed quality, and the policy-aware integrity audit before
   render/deploy.
3. Allowed-open and forbidden-open fixtures prove the generated integrity gate
   preserves the existing assumptions contract.
4. A seeded quality failure makes the Pages deploy step unreachable.
5. Every cited completed Cabannes leaf has a contained line-located passage and
   a current recorded `agrees` verdict; the fixture passes check, quality,
   render, and strict MkDocs build.
6. Copies with one evidence row removed, one stale review hash, or one
   non-`agrees` verdict fail quality checking for their designated codes.

### Regression suite

All existing tests must remain green. Add the new quality command to the normal
development checks and run:

```bash
make lint
make test
make check-example
uv run pytest -q tests/test_plugin_runtime.py tests/test_skill_examples.py
uv run autoform quality \
  skills/setup/assets/cabannes-thesis-project/blueprint \
  --lean-root skills/setup/assets/cabannes-thesis-project
```

The focused pytest command is the repository-owned plugin-surface validator.
Host-native skill or plugin validators are supplemental: when available, record
their resolved command and version rather than using a machine-local path or
placeholder.

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
- Cover all parser, policy, path-safety, compiled-evidence, and authored-file
  immutability cases above.

Exit criterion: every parser, policy, and CLI integration test passes, including
all negative fixtures.

### Phase 3: integrate workflows and review

- Update verification and Pages CI, documentation, skill handoffs, and the
  Cabannes fixture with authorized frozen passages and independently produced
  review artifacts.
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
- If a local plugin installation is updated, reinstall through a supported host
  flow and verify discovery in a fresh session; do not assume an untracked
  repository helper exists.

Exit criterion: all commands exit zero, the worktree contains only intended
changes, and every negative test fails for the intended reason rather than an
unrelated parser or setup error.

## Non-goals

- Do not add a model router or pin a model.
- Do not import Meta-internal services or credentials.
- Do not add proof execution to the quality layer or make optional specialists
  part of the default Formalize flow.
- Do not claim that a completed checklist mechanically proves source fidelity.
- Do not create a JSON database that competes with authored Markdown.
- Do not weaken existing Lean build, axiom, or kernel checks.
- Do not make planning-only blueprints fail quality validation.

## Definition of done

The work is complete only when:

1. the new skill is packaged and documented for Codex, Claude Code, and Muse;
2. the quality command enforces the visible Markdown contract;
3. every positive and negative case in this prompt has an automated test;
4. the generated CI uses the command without breaking empty projects;
5. the complete example passes and a corrupted example fails predictably;
6. all existing and new tests, lint, documentation builds, and plugin validators
   pass; and
7. no internal-only dependency or unsupported quality claim appears in the
   shipped plugin.
