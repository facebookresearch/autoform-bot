# Archive skill transport plan

## Decision

Do not copy the 51-skill archive wholesale into AutoformBot. The archive and
Autoform overlap, but they operate at different layers:

- Autoform owns durable project structure, dependency state, review contracts,
  publication, and—in the optional `execution` overlay—safe orchestration.
- The archive mixes reusable Lean workflows, corpus-production pipelines,
  general mathematical tools, Meta-internal operations, and personal-project
  procedures.

Use four integration boundaries:

1. **Autoform core (`main`)**: portable capabilities that validate claims
   Autoform records or publishes.
2. **Autoform execution overlay (`execution`)**: proof creation, refactoring,
   and other code-mutating specialist work.
3. **Optional companion plugin (`autoform-corpus`)**: source acquisition and
   large statement-bank/corpus production. This should be independently
   installable rather than expanding the default plugin.
4. **External composition**: general tools and domain workflows that Autoform
   may call when present but should not vendor.

Meta-internal operations and project-specific skills stay outside all public
Autoform packages.

The first implementation remains the portable formalization-quality gate in
[FORMALIZATION_QUALITY_GOAL.md](FORMALIZATION_QUALITY_GOAL.md). Nothing in this
plan authorizes autonomous execution on `main`.

## Audit basis

This plan covers every `SKILL.md` in:

- archive: `math_lean_skills_agent_config_2026-08-31.zip`
- verified SHA-256:
  `9d38fe39237afdf673073fd6ebeb15f01514f033689edd56ba3b3251d611d7d3`
- inventory: 51 skills, 74 script files, 21 reference files, and 51 OpenAI
  metadata files

The machine authority is
[`skills/archive-transport-manifest.json`](skills/archive-transport-manifest.json),
and the access/licensing decision is recorded in
[`ARCHIVE_SKILL_SOURCE_PROVENANCE.md`](ARCHIVE_SKILL_SOURCE_PROVENANCE.md).

Before copying any text or code into this MIT-licensed repository, confirm that
the archive material is authorized and license-compatible. Until then, use it
as design input and reimplement the portable contract. Never transfer internal
credentials, endpoints, paths, employee identifiers, unpublished benchmark
details, or service instructions.

## Admission tests

A skill may enter Autoform only when all applicable answers are yes:

1. Does it directly create, validate, review, or improve an Autoform artifact?
2. Is the behavior useful across unrelated Lean projects?
3. Can it be made provider-, model-, host-, and filesystem-agnostic?
4. Does it have an observable completion condition and negative tests?
5. Does it have one clear owner instead of duplicating an existing skill,
   agent, CLI command, or CI gate?
6. Can it preserve `main`'s non-autonomous contract?
7. Can its output be represented without creating a second authored state
   store beside the Markdown blueprint?

Failure on questions 1–4 or 6 excludes it from core. Overlap under question 5
means merge the useful delta into the existing owner rather than ship another
skill with nearly identical triggers.

## Delivery contract: executing this plan produces PRs

This document is an implementation plan, not authorization to land all changes
in one branch. Executing it must produce a sequence of independently reviewable
pull requests. Do not commit directly to `main` or `execution`, and do not make
one umbrella implementation PR.

Use [ARCHIVE_SKILL_TRANSPORT_GOAL.md](ARCHIVE_SKILL_TRANSPORT_GOAL.md) as the
master execution prompt. The plan defines what to build; the goal prompt defines
how to branch, validate, publish, and report the PR series.

Each PR must:

- implement one coherent user-visible capability or one prerequisite contract;
- include its own tests and documentation;
- pass on the branch named as its base, without relying on uncommitted work;
- identify its dependency PRs explicitly;
- avoid unrelated formatting, generated files, and opportunistic cleanup;
- include positive and purpose-built negative tests for every new gate;
- report exact commands and results in the PR description; and
- remain revertible without removing unrelated capabilities.

Prefer independent PRs when changes do not depend on one another. Use a stacked
PR only when the child cannot be meaningfully tested against the target branch;
set the child PR's base to the parent branch, then retarget it after the parent
merges. Never hide a stack by opening every child against `main` with duplicated
parent commits.

No archive skill receives a PR merely because it appeared in the inventory.
`CORE-MERGE` and `EXEC-MERGE` work belongs in the PR for its existing owner;
`COMPOSE`, `EXCLUDE-*`, and most `EXTRACT-CONCEPT` decisions need no runtime PR.

### Planned PR series

| PR | Base | Review unit | Depends on | Required evidence before opening |
|---|---|---|---|---|
| P00 | `main` | Add the transport policy, machine-readable 51-skill manifest, and licensing/authorization record. | None | Manifest reconciles 51/51 skills; policy tests and plugin validation pass. |
| P01 | `main` | Add the portable `formalization-quality` skill and references; merge the nonduplicative source-audit rules into Agent Review. No CLI behavior yet. | P00 | Skill validators, discovery tests, internal-token scan, and rubric fixture tests pass. |
| P02 | P01 branch, then `main` | Add the Markdown quality-evidence parser and `autoform quality` CLI with deterministic JSON and finding codes. | P01 | Parser, policy, path-containment, read-only, CLI exit-code, and JSON tests pass. |
| P03 | P02 branch, then `main` | Integrate quality checking into generated CI, documentation, and the Cabannes fixture. | P02 | Fresh empty scaffold passes; valid example passes; deliberately corrupted example fails for the intended finding code; strict site build passes. |
| P04 | `main` | Add one portable `mathlib-search` skill, fold in optional Loogle behavior, and update Roadmap/Agent Review handoffs. | P00 | Local-search, Loogle-present, Loogle-absent fallback, exact/partial/missing, and no-invented-name tests pass. |
| P05 | `main` | Merge portable cache, toolchain, build-evidence, and REPL smoke-test deltas into Setup/server documentation. Do not add duplicate setup/build skills. | P00 | Prepared, missing-cache, mismatched-toolchain, and REPL smoke fixtures pass. |
| P06 | `main` | Add deterministic statement-equivalence verification and its host-driven skill. | P01, P04 | Definitional and nontrivial bridges pass; malformed, forbidden, timeout, missing-prelude, and no-bridge cases return `not verified`; no network is used in tests. |
| E01 | `execution` after syncing accepted core prerequisites | Merge portable `lean-proof` discipline into `autoform-worker` and `orchestrate`; no new overlapping skill. | P04 and relevant merged core changes | Worker contract, claim enforcement, focused build, no-shortcut, and honest-failure tests pass. |
| E02 | E01 branch, then `execution` | Add shared declaration-interface freeze and post-edit verification used by all cleanup specialists, plus `simplify-proofs`. | E01 | Statement/API mutation and new-placeholder fixtures fail; readability-preserving fixture builds and passes axioms. |
| E03 | E02 branch, then `execution` | Add opt-in `mathlibify-proofs`. | E02, P04 | Interface freeze, narrow-import/style checks, build, axiom scan, and unapproved-heavy-import negative fixture pass. |
| E04 | E02 branch, then `execution` | Add explicitly invoked `lean-proof-golf`; keep it out of default orchestration. | E02 | Default invocation test proves it never runs implicitly; metric improvement, interface freeze, build, and regression tests pass. |
| E05 | `execution` after syncing accepted core prerequisites | Add `lean-comparator` for repositories that already provide a frozen task interface. | P05 | Valid solution, altered signature, forbidden axiom, missing binary, timeout, and `infra_error` classification tests pass. |
| E06 | E03 branch, then `execution` | Add `mathlib-extension` for nodes explicitly identified as reusable library gaps. | E03 | Small `MathlibExt` fixture, import visibility, narrow imports, examples, duplicate search, and hidden-declaration negative fixture pass. |
| C00 | new optional companion plugin/repository | Scaffold `autoform-corpus`, define its source-record/import schema, and add offline fixtures. | P00, P01 | Plugin/package validation, schema round trip, stable-ID import, and no-internal-token tests pass. |
| C01 | C00 branch | Add source fetching and visual PDF correction as one source-acquisition layer. | C00 | Frozen HTML/TeX/PDF fixtures, hashes, page binding, corrections, redirects, and unavailable-source cases pass offline. |
| C02 | C01 branch | Add conjecture and textbook exercise extraction with one shared atomic source-record contract. | C01 | Complete inventory reconciliation, duplicate numbering, damaged OCR, atomic splitting, and terminal-disposition tests pass. |
| C03 | C02 branch | Add generic theorem-bank formalization/import into Autoform articles. | C02, P02 | Stable IDs, helper closure, idempotent regeneration, buildable imports, quality evidence, and failed-stage blocking tests pass. |
| C04 | C03 branch | Add textbook-exercise formalization on the same shared pipeline. | C03 | Exact/schema accounting, correction use, chapter coverage, standalone task builds, and rejected-placeholder tests pass. |
| C05 | C04 branch | Add the arXiv/paper orchestrator over the already-tested source, extraction, formalization, and review stages. | C01–C04 | One offline paper runs end to end twice with byte-identical accepted artifacts; seeded source, semantic, compile, and join faults fail at their designated stages. |
| D01 | `main` after stable core/companion interfaces | Document optional composition with external writing, reference-checking, and computational skills; no runtime dependency. | P03–P06; C00 schema if available | Link checks pass and removing every optional tool leaves core tests green. |

P-series PRs target public `main`; E-series PRs target the optional `execution`
overlay; C-series PRs belong to the separately approved companion plugin; D01
is documentation-only. A PR should be omitted if its prerequisite design is
rejected. Do not create placeholder PRs for later waves.

### PR granularity rules

- Keep parser/CLI mechanics separate from workflow enforcement so reviewers can
  validate the contract before CI begins rejecting repositories.
- Keep skill instructions separate from substantial runtime code unless the
  skill would otherwise be undiscoverable or untestable.
- Keep each mutating execution mode separate because Simplify, Mathlibify,
  Golf, Comparator, and Mathlib Extension have different intent and risk.
- Combine source fetch with visual correction because they jointly establish
  immutable source evidence; combine conjecture and exercise extraction only
  at their shared record/schema layer, not their downstream formalizers.
- Split a listed PR further when it cannot be reviewed coherently or its tests
  require unrelated fixtures. Do not merge listed PRs merely to reduce PR count.
- Avoid arbitrary line-count limits: mathematical and validation coherence is
  the boundary. As a warning threshold, explain any hand-written diff above
  roughly 800 changed lines excluding fixtures and generated lockfiles.

### PR publication procedure

For each implementation unit:

1. update from the intended base and verify it is clean;
2. create a purpose-named branch (`autoform/<pr-id>-<slug>`);
3. implement only that row's scope;
4. run its focused tests and the cross-cutting release gates;
5. inspect the complete diff and commit only intended files;
6. push the branch and open a draft PR with scope, non-goals, dependencies,
   commands, results, and rollback notes;
7. mark ready only when all required evidence passes; and
8. incorporate review in that PR rather than mixing fixes into another unit.

Opening PRs is an external action. The execution agent must confirm that the
user's request still authorizes publication and that the authenticated remote
is the intended repository before the first push. The present planning task
does not itself create branches, commits, pushes, or PRs.

## Disposition vocabulary

- **CORE-ADAPT**: ship a portable public skill or deterministic checker on
  `main`.
- **CORE-MERGE**: merge unique rules into an existing core skill/tool; do not
  ship a duplicate skill.
- **EXEC-ADAPT**: ship only on `execution`, opt-in when it mutates Lean code.
- **EXEC-MERGE**: merge unique rules into an existing execution worker or
  orchestrator; do not add a duplicate skill.
- **CORPUS-ADAPT**: redesign for an optional `autoform-corpus` companion.
- **COMPOSE**: document as a compatible external skill/tool; do not vendor.
- **EXTRACT-CONCEPT**: retain a portable idea, but not the archived skill.
- **EXCLUDE-INTERNAL**: tied to private infrastructure or operations.
- **EXCLUDE-PROJECT**: tied to one mathematical project or source collection.

## Complete 51-skill review

| # | Archive skill | Decision | Destination and transport method |
|---:|---|---|---|
| 1 | `analyze-lean-eval` | EXCLUDE-INTERNAL | MAST, Scuba, MetaGen, and internal evaluation operations are not Autoform project behavior. Keep in the internal agent configuration. A future generic trace viewer would need a new public event schema and its own proposal. |
| 2 | `audit-lean-to-text` | CORE-MERGE | Merge its obligation map, source-route attribution, boundary probes, and findings-first reporting into `agent-review` and `formalization-quality`. Do not duplicate the existing faithfulness and proof-integrity rubrics. Test that every claimed text obligation maps to a declaration or an explicit unsupported finding. |
| 3 | `build-gate` | CORE-MERGE | Autoform Setup, generated CI, and the execution honesty gate already build, scan gaps, and audit axioms. Import only the cache-safety rule, explicit build evidence, and challenge/solution routing. Test cached and cache-missing paths plus zero-sorry and allowed-intentional-sorry fixtures. |
| 4 | `competition-solution-writing` | COMPOSE | Contest exposition, PDF production, and Drive upload are separate products. Document optional composition after Autoform verification; do not add upload or prose-writing scope to the plugin. |
| 5 | `copyedit-math-story` | COMPOSE | General prose polishing is independent of the blueprint lifecycle. It may edit published prose only after mathematical review, but remains an external writing skill. |
| 6 | `extract-lean-conjectures` | CORPUS-ADAPT | Put conjecture discovery, atomic terminal dispositions, source/literature provenance, and statement-only Lean output in `autoform-corpus`. Replace internal models and ProtoHub with provider-neutral interfaces and Autoform Markdown nodes. Test exhaustive record accounting and one terminal disposition per stable ID. |
| 7 | `extract-textbook-exercises` | CORPUS-ADAPT | Keep OCR/book inventory and exercise extraction outside core. Emit source records that can be imported into Autoform articles. Test damaged OCR, duplicate numbering, chapter boundaries, and exact inventory reconciliation. |
| 8 | `fact-check-references` | COMPOSE | Scholarly metadata verification is useful but not Lean- or Autoform-specific. The corpus pack may require or recommend it without vendoring it. |
| 9 | `fetch-paper` | CORPUS-ADAPT | Provide a small source-acquisition entry point in `autoform-corpus`, with immutable URL/hash metadata and no host-specific proxy requirement. Test arXiv HTML, TeX, PDF, redirects, unavailable sources, and byte hashes. |
| 10 | `fm-loop` | EXCLUDE-INTERNAL | Personal chat, Phabricator, MAST, commit, sync, and PingMe monitoring must not enter a public formalization plugin. |
| 11 | `formalize-arxiv-paper` | CORPUS-ADAPT | Preserve its staged source→candidate→compile→independent-review→release graph, but make models pluggable and express accepted statements as Autoform nodes. Do not ship until core quality gates exist. Test stage isolation, stopped prerequisites, repair lineage, and fail-closed joins. |
| 12 | `formalize-lean-textbook-exercises` | CORPUS-ADAPT | Preserve proof-exercise triage, exact/schema distinctions, chapter banks, correction layers, and coverage accounting. Replace private model/profile assumptions and generated bespoke dashboards with Autoform import/render adapters. Test every source exercise receives an accepted or explicit terminal disposition. |
| 13 | `formalize-lean-theorems` | CORPUS-ADAPT | Use as the generic theorem-bank engine in `autoform-corpus`. Map each atomic source result to one durable article ID; represent shared definitions as dependencies. Test idempotent regeneration, helper closure, chapter coverage, and buildable aggregate imports. |
| 14 | `gap-group-theory` | COMPOSE | GAP is a general computational backend. Autoform may record its scripts/certificates as evidence, but should not vendor GAP tutorials or installation logic. |
| 15 | `informalize` | COMPOSE | This is only an alias for Lean-to-English translation. Do not ship aliases that broaden trigger collisions; compose with an external translation skill. |
| 16 | `latex-setup` | COMPOSE | Toolchain installation and PDF troubleshooting are environment utilities. Autoform's MkDocs publication does not require TeX. |
| 17 | `lean-comparator` | EXEC-ADAPT | Add an opt-in frozen-task verification specialist on `execution`. It must use the repository's pinned Comparator and distinguish `pass`, `rejected`, and `infra_error`. Never substitute logical equivalence for an exact frozen interface. Test altered signatures, forbidden axioms, missing binaries, and valid solutions. |
| 18 | `lean-devserver-setup` | CORE-MERGE | Merge only portable cache, toolchain-version, and REPL smoke-test rules into Setup and `servers/README.md`. Exclude Meta devserver, OD, proxy, and internal model instructions. Test a prepared fixture, missing cache, mismatched toolchain, and REPL startup. |
| 19 | `lean-do-loop` | EXCLUDE-INTERNAL | MAST queue babysitting, RL training, cron, and PingMe are operational research automation, not repository formalization. |
| 20 | `lean-formalization-quality` | CORE-ADAPT | First transfer. Add a model-neutral skill plus a deterministic visible-evidence checker, following `FORMALIZATION_QUALITY_GOAL.md`. This closes the central gap between Lean validity and source fidelity. |
| 21 | `lean-formalizer-profile` | EXTRACT-CONCEPT | Do not transfer the Muse/MetaCode profile. Later define a provider-neutral provenance interface for optional generators: exact provider/model ID, prompt/response hashes, route, and no silent fallback. Core quality must work when authoring is human and no model exists. |
| 22 | `lean-hardness-atlas` | EXCLUDE-INTERNAL | FateX/FateH/MAST and internal artifact publication are evaluation infrastructure. A generic benchmark product would be a separate project. |
| 23 | `lean-paper-writing` | COMPOSE | Turning Lean into papers is a downstream exposition workflow. Autoform can export theorem maps that an external paper-writing skill consumes; it should not own manuscript generation. |
| 24 | `lean-proof` | EXEC-MERGE | Merge its compile-gated increments, goal inspection, Mathlib-first search, and anti-thrashing rules into `autoform-worker` and `orchestrate` on `execution`. Do not ship a second proof skill with overlapping triggers. Test worker prompt invariants and the focused-build honesty gate. |
| 25 | `lean-proof-golf` | EXEC-ADAPT | Add only as explicitly requested post-proof optimization. It must never run automatically. Freeze declarations, record a baseline metric, build after changes, and re-run axioms/quality checks. Test that default orchestration never invokes it and that interface changes are rejected. |
| 26 | `lean-status` | EXCLUDE-INTERNAL | This reports private RL experiment status and MAST queues, not Lean project status. Autoform's existing status views remain authoritative for projects. |
| 27 | `lean-stmt-equiv` | CORE-ADAPT | Port in a later quality wave. Split deterministic verification from bridge generation: Autoform validates supplied `A ↔ B` bridge code with Lean; the active host/provider may propose a bridge. Report `verified` or `not verified`, never infer non-equivalence from search failure. Test definitional equality, real bridges, malformed statements, forbidden bridge code, timeouts, and custom preludes. |
| 28 | `lean-to-english-proof` | COMPOSE | English proof translation is useful downstream but outside the roadmap/control plane. Autoform should expose source/declaration maps for an external translator. |
| 29 | `leanstral` | EXCLUDE-INTERNAL | The archived skill is coupled to internal RIFT endpoints. If a public Leanstral API becomes supported, implement it as a prover adapter behind the execution interface, not as core policy. |
| 30 | `loogle` | CORE-MERGE | Fold portable query syntax and optional local CLI detection into `mathlib-search`. Exclude Meta proxy/devserver installation. Loogle absence must trigger a documented fallback, not a hidden install. |
| 31 | `math-conjecture-research` | COMPOSE | Open-ended mathematical research, computation, papers, and publication exceed Autoform's formalization lifecycle. It may produce sources consumed by Roadmap. |
| 32 | `math-paper-writing` | COMPOSE | General manuscript composition is not an Autoform responsibility. Keep as an independent skill. |
| 33 | `math-tools-devserver-setup` | COMPOSE | Multi-tool installation for GAP, PARI, Sage, Z3, nauty, and Meta hosts is environment management. Autoform should detect optional tools but not install this stack. |
| 34 | `mathlib-extension` | EXEC-ADAPT | Add an opt-in specialist for nodes explicitly classified as reusable library gaps. Reuse `mathlib-search`, quality gates, and Mathlibify; require narrow imports, examples, root import visibility, and duplication review. Test a small `MathlibExt` fixture and an intentionally hidden/non-importable declaration. |
| 35 | `mathlib-search` | CORE-ADAPT | Add one portable search skill shared by Roadmap and Execution. Search pinned local sources and optional Loogle, verify candidates with `#check`, and never report names from memory. Test Loogle present/absent, source fallback, ambiguous matches, and exact/partial/missing classification. |
| 36 | `mathlibify-proofs` | EXEC-ADAPT | Add as an opt-in contribution-quality pass after proof and fidelity acceptance. Preserve theorem interfaces, inspect nearby Mathlib, reduce imports, normalize API/style, rebuild, lint, and re-audit axioms. Test the interface-freeze gate and reject new placeholders or heavier unapproved imports. |
| 37 | `metacode-lean` | EXCLUDE-INTERNAL | EdenFS and MetaCode operational guidance belongs in the internal host configuration, not a public plugin. |
| 38 | `muse-formalization-dataset` | EXTRACT-CONCEPT | Do not transfer the Muse-specific pipeline. Reuse its portable ideas—generator/verifier separation, leakage labels, exact artifact hashes, and rejected-attempt accounting—in `autoform-corpus`. |
| 39 | `muse-spark` | EXCLUDE-INTERNAL | Internal provider transport, authentication, and model availability stay outside Autoform. Optional generators use a provider-neutral adapter contract. |
| 40 | `nauty` | COMPOSE | A general graph-computation tool. Autoform dependency graphs do not require graph-isomorphism generation. Record nauty certificates as external evidence when a project uses them. |
| 41 | `order-sums-research` | EXCLUDE-PROJECT | This is a project-specific mathematical research loop. Keep it in that project and let it consume Autoform rather than becoming Autoform. |
| 42 | `package-aai-harbor-tasks` | EXCLUDE-INTERNAL | AAI/ADO Harbor packaging and submission policy are product-specific and internal. They may consume exported corpora through a separate private adapter. |
| 43 | `pari-gp` | COMPOSE | General computational number theory tooling remains external. Autoform can cite retained scripts/results as evidence. |
| 44 | `prepare-aai-ado-tasks` | EXCLUDE-INTERNAL | Internal dataset eligibility, Lean-version policy, and Harbor/legacy packaging do not belong in the public plugin. |
| 45 | `ramanujan` | EXCLUDE-PROJECT | Berndt/Ramanujan notation and source rules belong in the Ramanujan project or a source-specific corpus extension. General lessons should become quality tests, not core special cases. |
| 46 | `sagemath` | COMPOSE | Sage is a general computational environment. Detect and use it through project-specific workflows; do not vendor its tutorial/setup skill. |
| 47 | `simplify-math-writing` | COMPOSE | General exposition cleanup remains external and must follow mathematical verification. |
| 48 | `simplify-proofs` | EXEC-ADAPT | Add a conservative, explicitly requested readability pass distinct from Mathlibify and Golf. Preserve all declarations and abstraction boundaries, build after each coherent edit, and review the diff. Test interface freezing and forbidden-placeholder rejection. |
| 49 | `verify-lean-translation` | CORE-MERGE | This is a focused alias/entry point for `audit-lean-to-text`. Merge its translation table and per-sentence verdicts into Agent Review; do not ship a second overlapping skill. |
| 50 | `visual-pdf-math-extraction` | CORPUS-ADAPT | Add to `autoform-corpus` as the source-repair layer. Preserve rendered-page provenance and correction hashes; make image/OCR tools optional. Test damaged formulas, page binding, correction overlays, and unchanged raw evidence. |
| 51 | `z3` | COMPOSE | General SMT tooling remains external. Autoform may accept checked certificates or Lean-lifted results, but SAT output alone is not a Lean proof. |

## Decision totals

| Disposition group | Count | Skills |
|---|---:|---|
| Core adaptations and merges | 8 | `audit-lean-to-text`, `build-gate`, `lean-devserver-setup`, `lean-formalization-quality`, `lean-stmt-equiv`, `loogle`, `mathlib-search`, `verify-lean-translation` |
| Execution adaptations and merges | 6 | `lean-comparator`, `lean-proof`, `lean-proof-golf`, `mathlib-extension`, `mathlibify-proofs`, `simplify-proofs` |
| Corpus companion | 7 | `extract-lean-conjectures`, `extract-textbook-exercises`, `fetch-paper`, `formalize-arxiv-paper`, `formalize-lean-textbook-exercises`, `formalize-lean-theorems`, `visual-pdf-math-extraction` |
| External composition | 16 | General writing, research, LaTeX, and computational-tool skills |
| Internal/private or concept-only | 12 | Meta operations, model transports/profiles, evaluation, and AAI packaging |
| Project-specific | 2 | `order-sums-research`, `ramanujan` |

Every archive skill appears exactly once in the review table. The totals count
`CORE-MERGE` and `EXEC` merges by the layer receiving their unique content;
they do not imply that 14 new standalone skills should be created.

## Resulting public skill surface

### `main`

Keep the current five skills and add only:

- `formalization-quality` in the first wave;
- `mathlib-search` after its portable backend contract is ready; and
- optionally `statement-equivalence` after deterministic bridge verification
  exists.

Strengthen `agent-review`, `setup`, `roadmap`, and documentation by merging the
archive deltas identified above. Do not add duplicate `build-gate`,
`audit-lean-to-text`, `loogle`, or `verify-lean-translation` entry points.

### `execution`

Extend the existing `orchestrate` skill and specialist agents. Add standalone
entry points only where user intent is meaningfully distinct:

- `mathlibify-proofs`;
- `simplify-proofs`;
- `lean-proof-golf`;
- `lean-comparator`; and
- `mathlib-extension`.

These are opt-in. The normal proof worker must not silently refactor APIs,
golf proofs, or prepare upstream contributions.

### `autoform-corpus`

Create this companion only after the core quality contract is stable. Suggested
surface:

- `fetch-source`;
- `repair-source-math`;
- `extract-conjectures`;
- `extract-textbook-exercises`;
- `formalize-theorems`;
- `formalize-textbook-exercises`; and
- `formalize-paper` as their orchestrator.

It should emit/import ordinary Autoform Markdown rather than maintaining a
second project-state system. Large immutable raw responses and source bundles
may remain external artifacts referenced by hashes.

## Testable implementation plan

### Phase 0: freeze policy and provenance

1. Confirm authorization/licensing for any text or scripts considered for
   verbatim reuse.
2. Add a machine-readable transport manifest containing the archive SHA-256,
   all 51 names, disposition, target layer, replacement owner, and whether code
   or only concepts may be reused.
3. Add a test requiring exactly 51 unique records and the decision totals above.
4. Add a test rejecting a transported skill not present in the manifest.

Pass condition: the manifest reconciles exactly with the archived inventory and
no skill is unclassified or multiply owned.

### Phase 1: semantic quality on `main`

Implement [FORMALIZATION_QUALITY_GOAL.md](FORMALIZATION_QUALITY_GOAL.md), then
merge the nonduplicative audit rules from `audit-lean-to-text` and
`verify-lean-translation` into Agent Review.

Required tests:

- visible seven-gate quality evidence passes;
- every missing, blocked, malformed, hidden, or internally inconsistent gate
  fails with a stable finding code;
- a compiling but deliberately weakened statement fails source review;
- planning-only nodes continue to pass;
- the checker is read-only and rejects escaping evidence links;
- generated CI, the example, both plugin manifests, and skill discovery pass;
- a denylist scan finds no internal service/path/model identifiers.

Pass condition: all existing tests plus the positive and negative quality
fixtures pass, and a corrupted Cabannes fixture fails for the intended semantic
contract reason.

### Phase 2: portable search and equivalence

1. Add `mathlib-search`, merging portable Loogle behavior into one owner.
2. Teach Roadmap and Execution to call the same exact/partial/missing search
   contract.
3. Add statement equivalence with deterministic Lean verification separated
   from optional bridge generation.
4. Merge cache/toolchain/REPL deltas into Setup rather than adding setup skills.

Required tests:

- search finds a known declaration in the pinned fixture;
- unavailable Loogle falls back locally without installing software;
- invented and ambiguous names are never classified exact;
- definitionally identical and nontrivially bridged types verify;
- invalid, forbidden, timeout, or no-bridge cases return `not verified`, never
  `not equivalent`;
- custom preludes reject `axiom`, `sorry`, `admit`, `opaque`, and `unsafe`;
- no network is required by deterministic tests.

Pass condition: search and equivalence JSON are deterministic, path-safe, and
verified by Lean in the pinned example project.

### Phase 3: execution-only proof lifecycle

Work on `execution`, preserving the branch's claim, worktree, steering, and
honesty-gate architecture.

1. Merge `lean-proof` practices into the existing worker.
2. Add conservative `simplify-proofs`.
3. Add `mathlibify-proofs` for contribution preparation.
4. Add explicit `lean-proof-golf`; keep it out of default prompts and automatic
   orchestration.
5. Add `lean-comparator` and `mathlib-extension` specialists.

Required tests:

- each mutating specialist refuses work without a valid node claim;
- baseline and final declaration types are byte- or elaboration-equivalent;
- new `sorry`, `admit`, axioms, unsafe features, and forbidden evaluators fail;
- target and broader builds plus axiom audits run after mutation;
- Mathlibify checks imports/style and cannot run as an automatic proof step;
- Golf requires explicit invocation and a declared metric improvement;
- Comparator preserves exact frozen interfaces and distinguishes infrastructure
  failure from rejection;
- concurrent workers cannot edit the same node or file.

Pass condition: deliberate interface, axiom, ownership, and build regressions
are rejected by deterministic gates independent of the model's final message.

### Phase 4: optional corpus companion

Create a separate plugin/package only after Phases 1 and 2 are stable.

1. Define a source-record and import contract mapping stable source IDs to
   Autoform articles.
2. Port source fetching and visual correction first.
3. Port conjecture and textbook extraction.
4. Port theorem, exercise, and paper formalization orchestration last.
5. Use a provider-neutral generation interface; a human-only run must remain
   supported.

Required tests:

- source bytes, spans, corrections, prompts/responses when present, and final
  declarations are hash-bound;
- every atomic source record has exactly one terminal disposition;
- stage failure blocks downstream acceptance;
- generation and independent review identities are distinct when policy
  requires independence;
- repeated generation is idempotent and joins by stable ID, never list index;
- corpus output imports into Autoform and passes quality/check/render;
- fixtures run offline with fake providers and frozen source material;
- internal-token and credential scans pass.

Pass condition: one small paper and one small textbook fixture reproduce
byte-identical accepted artifacts on two clean runs, while seeded source,
semantic, compilation, and join faults each fail at their intended gate.

### Phase 5: composition documentation

Document external integration points for paper writing, translation, reference
checking, GAP, PARI/GP, SageMath, nauty, Z3, and environment setup. Do not make
them required dependencies.

Required tests:

- links name capabilities rather than assuming a particular local skill path;
- Autoform installs and all examples run without any external integration;
- optional evidence records clearly distinguish computation from Lean proof.

Pass condition: removing every optional external tool leaves core validation,
rendering, and tests green.

## Cross-cutting release gates

Run these for every wave:

```bash
make lint
make test
make check-example
python3 <PLUGIN_CREATOR_ROOT>/scripts/validate_plugin.py .
```

Also run the skill validator on each added or changed skill. When testing an
updated local installation, use the plugin cachebuster/reinstall helper; never
hand-edit marketplace state.

Each wave must additionally prove:

- no unexpected working-tree changes or generated artifacts;
- no secret, credential, private endpoint, employee-specific path, or
  Meta-internal command entered the public package;
- every new CLI surface has deterministic JSON, documented exit codes, path
  containment tests, and read-only tests where applicable;
- positive fixtures pass and purpose-built negative fixtures fail for the
  expected reason;
- `main` remains non-autonomous; and
- existing consumers not opting into a new evidence contract retain a
  documented migration path.

## Stop conditions

Stop a proposed transfer when:

- authorization or licensing is unclear;
- the skill requires a private service to be meaningful;
- its behavior duplicates an existing Autoform owner;
- acceptance depends only on an LLM self-report;
- no deterministic failure fixture can be written;
- it creates a second mutable project-state store; or
- it would silently broaden `main` into autonomous proof execution.

## Final completion criterion

This transport program is complete when the manifest still accounts for all 51
archive skills, every accepted capability lives in exactly one layer, all
adapted behavior is provider-neutral and test-gated, optional packs can be
removed without breaking core, and excluded internal/project-specific skills
have not leaked into Autoform's public runtime or documentation.
