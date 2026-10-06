# Archive skill transport plan

## Decision

Do not copy the 51-skill archive wholesale into AutoformBot. The archive and
Autoform overlap, but they operate at different layers:

- Autoform owns durable project structure, dependency state, review contracts,
  publication, and the Markdown-native `formalize` workflow on `main`.
- The archive mixes reusable Lean workflows, corpus-production pipelines,
  general mathematical tools, Meta-internal operations, and personal-project
  procedures.

Use four integration boundaries:

1. **Autoform core (`main`)**: portable capabilities that validate claims
   Autoform records or publishes.
2. **Formalize specialist layer (`main`)**: proof creation, refactoring, and
   other code-mutating specialist work, invoked through the existing
   Markdown-native `formalize` skill rather than a second execution branch or
   orchestration state store.
3. **Optional companion plugin (`autoform-corpus`)**: source acquisition and
   large statement-bank/corpus production. This should be independently
   installable rather than expanding the default plugin.
4. **External composition**: general tools and domain workflows that Autoform
   may call when present but should not vendor.

Meta-internal operations and project-specific skills stay outside all public
Autoform packages.

Manifest `target_layer` values describe logical ownership, not Git branches.
In particular, the `formalize` layer ships from the canonical `main` branch;
delivery units record their real target branch separately.

The first implementation remains the portable formalization-quality gate in
[FORMALIZATION_QUALITY_GOAL.md](FORMALIZATION_QUALITY_GOAL.md). This policy does
not itself authorize any implementation PR or autonomous run.

## Audit basis

This plan declares a decision for each of the 51 skill names externally
attested as the complete `SKILL.md` set in:

- archive: `math_lean_skills_agent_config_2026-08-31.zip`
- externally attested SHA-256:
  `9d38fe39237afdf673073fd6ebeb15f01514f033689edd56ba3b3251d611d7d3`
- externally attested aggregate: 51 skills, 74 script files, 21 reference
  files, and 51 OpenAI metadata files

The machine authority for **transport decisions**, not source completeness, is
[`skills/archive-transport-manifest.json`](skills/archive-transport-manifest.json),
and the access/licensing decision is recorded in
[`ARCHIVE_SKILL_SOURCE_PROVENANCE.md`](ARCHIVE_SKILL_SOURCE_PROVENANCE.md).
The archive/member evidence is not regenerable from this repository; see that
provenance file before repeating any completeness or license-scan claim.

Before copying any text or code into this MIT-licensed repository, confirm that
the archive material is authorized and license-compatible. Until then, use it
as design input and reimplement the portable contract. Never transfer internal
credentials, endpoints, paths, employee identifiers, unpublished benchmark
details, or service instructions.

## Repository baseline and prior work

This plan was recalibrated against canonical `main` commit
`7fa6d1d6dcda161d5575a588bb723271ac58467c`. Seven merged capabilities materially
change the original archive review:

- PR #12 already provides environment-backed skeleton extraction, and PR #53
  provides the read-back faithfulness rubric. P01 extends the merged review
  contract; P02 must reuse the skeleton freshness check and elaborated
  environment rather than implement a lexical declaration lookup or a parallel
  trust report.
- PR #92 already provides the Markdown-native `formalize` skill with native
  agents, worktree isolation, fail-closed claims, focused builds, and
  independent Agent Review. E01 is therefore satisfied by current `main`.
  Later E-series units are optional specialists behind Formalize, not work on
  the deprecated `execution` branch.
- PRs #136 and #138 add revision-impact claim sets and an explicit project
  policy for open statements. Quality must consume those contracts: a retracted
  statement never passes acceptance, while an explicitly allowed open theorem
  may use justified `proof-integrity: not-applicable` until its proof is
  completed. Later specialists reuse `autoform work impact` rather than
  inventing a second revision graph.
- PR #158 makes formalized statement/proof states require a nonempty parseable
  `lean:` declaration name and rejects a formalized proof without its statement.
  Quality fixtures must enter through that graph contract rather than duplicate
  or bypass it.
- PR #96 makes catalog-backed `autoform project new` the primary atomic project
  creation path, with release/toolchain selection and cache setup. P05 may merge
  only demonstrably missing build-evidence or server-smoke rules into Setup; it
  must not recreate project creation, the release catalog, or cache bootstrap.

Baseline commit hashes are planning provenance, not forever-current dependency
pins. Recalibrate the manifest and this plan when later repository changes
invalidate a recorded capability or delivery dependency.

## Admission tests

A skill may enter Autoform only when all applicable answers are yes:

1. Does it directly create, validate, review, or improve an Autoform artifact?
2. Is the behavior useful across unrelated Lean projects?
3. Can it be made provider-, model-, host-, and filesystem-agnostic?
4. Does it have an observable completion condition and negative tests?
5. Does it have one clear owner instead of duplicating an existing skill,
   agent, CLI command, or CI gate?
6. Can it preserve `main`'s request-driven Formalize contract without adding a
   background scheduler or default specialist mutation?
7. Can its output be represented without creating a second authored state
   store beside the Markdown blueprint?

Failure on questions 1–4 or 6 excludes it from core. Overlap under question 5
means merge the useful delta into the existing owner rather than ship another
skill with nearly identical triggers.

## Delivery contract: executing this plan produces PRs

This document is an implementation plan, not authorization to land all changes
in one branch. Executing it must produce a sequence of independently reviewable
pull requests. Do not commit directly to `main`, and do not make one umbrella
implementation PR.

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
`CORE-MERGE` and `FORMALIZE-MERGE` work belongs in the PR for its existing owner;
`COMPOSE`, `EXCLUDE-*`, and most `EXTRACT-CONCEPT` decisions need no runtime PR.

### Planned PR series

The owner, target repository, target branch, stack parent, dependencies, and
approval gates below are duplicated in the manifest intentionally and tested
for exact parity. P-, E-, and D-series units bind the canonical
`facebookresearch/autoform-bot` repository. C-series repositories remain `—`
until `companion-repository-approved` binds an actual owner/repository; their
requested branch is `main`, which execution must verify after that binding and
before creating a branch. A stack parent is always a delivery-unit ID. `—`
means no repository, stack parent, or list item.

| Unit | Owner | Target repository | Target branch | Stack parent | Review unit | Depends on | Approval gates | Required evidence before opening |
|---|---|---|---|---|---|---|---|---|
| P00 | `policy.transport` | `facebookresearch/autoform-bot` | `main` | — | Add the transport policy, machine-readable 51-decision manifest, and authorization record. | — | — | Manifest and plan are internally consistent, attestation limits are explicit, and strict policy tests pass. |
| P01 | `core.formalization-quality` | `facebookresearch/autoform-bot` | `main` | — | Add the portable `formalization-quality` skill and link the existing Agent Review rubrics without redefining them. | `P00` | — | All three host surfaces, internal-token scan, and rubric fixtures pass. |
| P02 | `core.formalization-quality` | `facebookresearch/autoform-bot` | `main` | `P01` | Add the quality parser/CLI and extend the existing skeleton pipeline to recorded external Mathlib roots. | `P01` | — | Parser, policy, path containment, local/external compiled evidence, stale/wrong/shadowed-module negatives, immutability, exit-code, and JSON tests pass. |
| P03 | `core.formalization-quality` | `facebookresearch/autoform-bot` | `main` | `P02` | Gate policy-aware verification and Pages from both project generators, document the workflow, and add authorized Cabannes passages plus independent verdicts. | `P02` | — | `init` and `project new` fixtures pass; open-statement policy is preserved; every cited completed example node has current evidence; quality failures cannot deploy. |
| P04 | `core.mathlib-search` | `facebookresearch/autoform-bot` | `main` | — | Add one portable `mathlib-search` skill and fold in optional Loogle behavior. | `P00` | — | Local and optional Loogle search, fallback, and no-invented-name tests pass. |
| P05 | `core.setup` | `facebookresearch/autoform-bot` | `main` | — | Merge only missing build-evidence and LSP/REPL smoke deltas into current Setup; do not recreate project creation or cache bootstrap. | `P00` | — | Existing catalog/project-new tests stay green; explicit build-record, strict and allowed-open, and server-smoke fixtures cover only the accepted delta. |
| P06 | `core.statement-equivalence` | `facebookresearch/autoform-bot` | `main` | — | Add deterministic statement-equivalence verification and its host-driven skill. | `P01`, `P04` | — | Positive bridges and all fail-closed cases pass without network access. |
| P07 | `core.agent-review` | `facebookresearch/autoform-bot` | `main` | — | Merge the nonduplicative archive audit rules into the existing Agent Review owner. | `P01` | — | Source-obligation, boundary, unsupported-claim, and no-duplicate-rubric fixtures pass. |
| E01 | `core.formalize` | `facebookresearch/autoform-bot` | `main` | — | Record that `lean-proof` discipline is already owned by merged Markdown `formalize`; no PR is required. | — | — | Current Formalize contract and focused verification cover the reusable behavior. |
| E02 | `core.formalize` | `facebookresearch/autoform-bot` | `main` | — | Add a shared declaration-interface freeze, output-file overlap preflight, and explicitly requested `simplify-proofs`. | `P03`, `P04` | `specialist-scope-approved` | API mutation and new/unlisted placeholders fail; allowed inherited open assumptions survive; overlapping or ambiguous file targets serialize; the fixture builds. |
| E03 | `core.formalize` | `facebookresearch/autoform-bot` | `main` | `E02` | Add explicitly requested `mathlibify-proofs` through Formalize. | `E02`, `P04` | `specialist-scope-approved` | Interface, import, build, axiom, style, and negative fixtures pass. |
| E04 | `core.formalize` | `facebookresearch/autoform-bot` | `main` | `E02` | Add explicitly requested `lean-proof-golf`; never invoke it by default. | `E02` | `specialist-scope-approved` | Non-default invocation, metric, interface, build, and regression tests pass. |
| E05 | `core.formalize` | `facebookresearch/autoform-bot` | `main` | — | Add `lean-comparator` for repositories with a frozen task interface. | `P05` | `specialist-scope-approved` | Valid, altered-signature, forbidden-axiom, missing-binary, timeout, and infrastructure cases pass. |
| E06 | `core.formalize` | `facebookresearch/autoform-bot` | `main` | `E03` | Add `mathlib-extension` for nodes identified as reusable library gaps. | `E03` | `specialist-scope-approved` | Import, example, duplicate-search, and hidden-declaration fixtures pass. |
| C00 | `corpus.framework` | — | `main` | — | Scaffold the approved companion and define its source-record/import schema. | `P03`, `P05`, `P06` | `companion-repository-approved` | Plugin validation, schema round trip, stable-ID import, and token scan pass. |
| C01 | `corpus.fetch-source` | — | `main` | `C00` | Add source fetching and visual PDF correction as one acquisition layer. | `C00` | `companion-repository-approved` | Frozen source fixtures, hashes, page binding, corrections, redirects, and unavailable cases pass. |
| C02 | `corpus.extract` | — | `main` | `C01` | Add conjecture and textbook extraction with one atomic source-record contract. | `C01` | `companion-repository-approved` | Inventory, numbering, OCR, splitting, and terminal-disposition tests pass. |
| C03 | `corpus.formalize` | — | `main` | `C02` | Add generic theorem-bank formalization/import into Autoform articles. | `C02` | `companion-repository-approved` | Stable IDs, closure, idempotence, builds, quality evidence, and blocking tests pass. |
| C04 | `corpus.formalize` | — | `main` | `C03` | Add textbook-exercise formalization on the shared pipeline. | `C03` | `companion-repository-approved` | Exact/schema accounting, corrections, coverage, standalone builds, and rejection tests pass. |
| C05 | `corpus.formalize` | — | `main` | `C04` | Add the paper pipeline over the tested source, extraction, formalization, and review stages. | `C04` | `companion-repository-approved` | A frozen paper is byte-stable across two runs and seeded faults fail at their designated stages. |
| D01 | `docs.composition` | `facebookresearch/autoform-bot` | `main` | — | Document optional external writing, reference, and computation tools. | `P03`, `P05`, `P06` | — | Link checks pass and removing every optional tool leaves core tests green. |
| D02 | `docs.composition` | `facebookresearch/autoform-bot` | `main` | — | Document optional composition with the approved corpus companion. | `C00` | `companion-repository-approved` | Documentation names only the accepted source-record interface and link checks pass. |

P-, E-, and D-series work lands on public `main`; the E prefix is retained only
to preserve the audit identifiers from the original review. E-series behavior
extends `formalize` and does not revive the deprecated `execution` branch,
`autoform-worker`, or `orchestrate`. `autoform-corpus` remains the C-series
logical target layer, not a claim that a repository or branch already exists.
C-series work belongs to the companion repository only after approval binds
and verifies it. A unit is omitted when its prerequisite design
or approval gate is rejected; never create placeholder PRs for later waves.

### PR granularity rules

- Keep parser/CLI mechanics separate from workflow enforcement so reviewers can
  validate the contract before CI begins rejecting repositories.
- Keep skill instructions separate from substantial runtime code unless the
  skill would otherwise be undiscoverable or untestable.
- Keep each mutating specialist separate because Simplify, Mathlibify,
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

Opening PRs is an external action. The implementing agent must confirm that the
user's request still authorizes publication and that the authenticated remote
is the intended repository before the first push. The present planning task
does not itself create branches, commits, pushes, or PRs.

## Disposition vocabulary

- **CORE-ADAPT**: ship a portable public skill or deterministic checker on
  `main`.
- **CORE-MERGE**: merge unique rules into an existing core skill/tool; do not
  ship a duplicate skill.
- **FORMALIZE-ADAPT**: ship an explicitly invoked Formalize specialist on
  `main`; it does not name a separate branch.
- **FORMALIZE-MERGE**: merge unique rules into the existing `formalize` owner; do
  not add a duplicate proof skill.
- **CORPUS-ADAPT**: redesign for an optional `autoform-corpus` companion.
- **COMPOSE**: document as a compatible external skill/tool; do not vendor.
- **EXTRACT-CONCEPT**: retain a portable idea, but not the archived skill.
- **EXCLUDE-INTERNAL**: tied to private infrastructure or operations.
- **EXCLUDE-PROJECT**: tied to one mathematical project or source collection.

## Complete 51-skill review

| # | Archive skill | Decision | Destination and transport method |
|---:|---|---|---|
| 1 | `analyze-lean-eval` | EXCLUDE-INTERNAL | MAST, Scuba, MetaGen, and internal evaluation operations are not Autoform project behavior. Keep in the internal agent configuration. A future generic trace viewer would need a new public event schema and its own proposal. |
| 2 | `audit-lean-to-text` | CORE-MERGE | Merge its obligation map, source-route attribution, boundary probes, and findings-first reporting into the existing `agent-review` owner. Formalization Quality links and consumes that canonical rubric; it does not own a second copy. Test that every claimed text obligation maps to a declaration or an explicit unsupported finding. |
| 3 | `build-gate` | CORE-MERGE | Autoform Setup, generated CI, and the Formalize honesty gate already build, scan gaps, and audit axioms. P05 merges only any still-missing explicit build-evidence rule into Setup; #96 already owns atomic project creation, toolchain selection, and cache bootstrap, while current Formalize owns honest route reporting. Test zero-sorry and allowed-intentional-sorry fixtures without duplicating project creation. |
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
| 17 | `lean-comparator` | FORMALIZE-ADAPT | Add an opt-in frozen-task verification specialist through Formalize. It must use the repository's pinned Comparator and distinguish `pass`, `rejected`, and `infra_error`. Never substitute logical equivalence for an exact frozen interface. Test altered signatures, forbidden axioms, missing binaries, and valid solutions. |
| 18 | `lean-devserver-setup` | CORE-MERGE | #96 already owns portable project/toolchain/cache setup. P05 may merge only a missing LSP/REPL smoke rule into Setup or server documentation; exclude Meta devserver, OD, proxy, and internal model instructions. Omit the delta if current tests already cover it. |
| 19 | `lean-do-loop` | EXCLUDE-INTERNAL | MAST queue babysitting, RL training, cron, and PingMe are operational research automation, not repository formalization. |
| 20 | `lean-formalization-quality` | CORE-ADAPT | First transfer. Add a model-neutral skill plus a deterministic visible-evidence checker, following `FORMALIZATION_QUALITY_GOAL.md`. This closes the central gap between Lean validity and source fidelity. |
| 21 | `lean-formalizer-profile` | EXTRACT-CONCEPT | Do not transfer the Muse/MetaCode profile. Later define a provider-neutral provenance interface for optional generators: exact provider/model ID, prompt/response hashes, route, and no silent fallback. Core quality must work when authoring is human and no model exists. |
| 22 | `lean-hardness-atlas` | EXCLUDE-INTERNAL | FateX/FateH/MAST and internal artifact publication are evaluation infrastructure. A generic benchmark product would be a separate project. |
| 23 | `lean-paper-writing` | COMPOSE | Turning Lean into papers is a downstream exposition workflow. Autoform can export theorem maps that an external paper-writing skill consumes; it should not own manuscript generation. |
| 24 | `lean-proof` | FORMALIZE-MERGE | The merged `formalize` skill already owns compile-gated increments, goal inspection, Mathlib-first search, worktree isolation, and honest failure. Preserve any remaining nonduplicative discipline there; do not ship a second proof skill. Test the Formalize contract and focused-build honesty gate. |
| 25 | `lean-proof-golf` | FORMALIZE-ADAPT | Add only as explicitly requested post-proof optimization. It must never run automatically. Freeze declarations, record a baseline metric, build after changes, and re-run axioms/quality checks. Test that the normal Formalize flow never invokes it and that interface changes are rejected. |
| 26 | `lean-status` | EXCLUDE-INTERNAL | This reports private RL experiment status and MAST queues, not Lean project status. Autoform's existing status views remain authoritative for projects. |
| 27 | `lean-stmt-equiv` | CORE-ADAPT | Port in a later quality wave. Split deterministic verification from bridge generation: Autoform validates supplied `A ↔ B` bridge code with Lean; the active host/provider may propose a bridge. Report `verified` or `not verified`, never infer non-equivalence from search failure. Test definitional equality, real bridges, malformed statements, forbidden bridge code, timeouts, and custom preludes. |
| 28 | `lean-to-english-proof` | COMPOSE | English proof translation is useful downstream but outside the roadmap/control plane. Autoform should expose source/declaration maps for an external translator. |
| 29 | `leanstral` | EXCLUDE-INTERNAL | The archived skill is coupled to internal RIFT endpoints. If a public Leanstral API becomes supported, implement it behind an explicitly approved provider interface, not as core policy. |
| 30 | `loogle` | CORE-MERGE | Fold portable query syntax and optional local CLI detection into `mathlib-search`. Exclude Meta proxy/devserver installation. Loogle absence must trigger a documented fallback, not a hidden install. |
| 31 | `math-conjecture-research` | COMPOSE | Open-ended mathematical research, computation, papers, and publication exceed Autoform's formalization lifecycle. It may produce sources consumed by Roadmap. |
| 32 | `math-paper-writing` | COMPOSE | General manuscript composition is not an Autoform responsibility. Keep as an independent skill. |
| 33 | `math-tools-devserver-setup` | COMPOSE | Multi-tool installation for GAP, PARI, Sage, Z3, nauty, and Meta hosts is environment management. Autoform should detect optional tools but not install this stack. |
| 34 | `mathlib-extension` | FORMALIZE-ADAPT | Add an opt-in specialist for nodes explicitly classified as reusable library gaps. Reuse `mathlib-search`, quality gates, and Mathlibify; require narrow imports, examples, root import visibility, and duplication review. Test a small `MathlibExt` fixture and an intentionally hidden/non-importable declaration. |
| 35 | `mathlib-search` | CORE-ADAPT | Add one portable search skill shared by Roadmap and Formalize. Search pinned local sources and optional Loogle, verify candidates with `#check`, and never report names from memory. Test Loogle present/absent, source fallback, ambiguous matches, and exact/partial/missing classification. |
| 36 | `mathlibify-proofs` | FORMALIZE-ADAPT | Add as an opt-in contribution-quality pass after proof and fidelity acceptance. Preserve theorem interfaces, inspect nearby Mathlib, reduce imports, normalize API/style, rebuild, lint, and re-audit axioms. Test the interface-freeze gate and reject new placeholders or heavier unapproved imports. |
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
| 48 | `simplify-proofs` | FORMALIZE-ADAPT | Add a conservative, explicitly requested readability pass distinct from Mathlibify and Golf. Preserve all declarations and abstraction boundaries, build after each coherent edit, and review the diff. Test interface freezing and forbidden-placeholder rejection. |
| 49 | `verify-lean-translation` | CORE-MERGE | This is a focused alias/entry point for `audit-lean-to-text`. Merge its translation table and per-sentence verdicts into Agent Review; do not ship a second overlapping skill. |
| 50 | `visual-pdf-math-extraction` | CORPUS-ADAPT | Add to `autoform-corpus` as the source-repair layer. Preserve rendered-page provenance and correction hashes; make image/OCR tools optional. Test damaged formulas, page binding, correction overlays, and unchanged raw evidence. |
| 51 | `z3` | COMPOSE | General SMT tooling remains external. Autoform may accept checked certificates or Lean-lifted results, but SAT output alone is not a Lean proof. |

## Decision totals

| Disposition group | Count | Skills |
|---|---:|---|
| Core adaptations and merges | 8 | `audit-lean-to-text`, `build-gate`, `lean-devserver-setup`, `lean-formalization-quality`, `lean-stmt-equiv`, `loogle`, `mathlib-search`, `verify-lean-translation` |
| Formalize specialist adaptations and merges | 6 | `lean-comparator`, `lean-proof`, `lean-proof-golf`, `mathlib-extension`, `mathlibify-proofs`, `simplify-proofs` |
| Corpus companion | 7 | `extract-lean-conjectures`, `extract-textbook-exercises`, `fetch-paper`, `formalize-arxiv-paper`, `formalize-lean-textbook-exercises`, `formalize-lean-theorems`, `visual-pdf-math-extraction` |
| External composition | 16 | General writing, research, LaTeX, and computational-tool skills |
| Internal/private or concept-only | 12 | Meta operations, model transports/profiles, evaluation, and AAI packaging |
| Project-specific | 2 | `order-sums-research`, `ramanujan` |

Every archive skill appears exactly once in the review table. The totals count
`CORE-MERGE` and `FORMALIZE-MERGE` by the layer receiving their unique content;
they do not imply that 14 new standalone skills should be created.

## Resulting public skill surface

### `main`

Keep the current six skills, including the merged Markdown-native `formalize`
skill, and add only:

- `formalization-quality` in the first wave;
- `mathlib-search` after its portable backend contract is ready; and
- optionally `statement-equivalence` after deterministic bridge verification
  exists.

Strengthen `agent-review`, `setup`, `roadmap`, and documentation by merging the
archive deltas identified above. Do not add duplicate `build-gate`,
`audit-lean-to-text`, `loogle`, or `verify-lean-translation` entry points.

### Formalize specialist layer

Extend the existing `formalize` skill on `main`. Reuse the skeleton/read-back
trust and review contracts from PRs #12 and #53, and the native-agent,
worktree, and Markdown work/claim contract from PR #92. Add standalone entry
points only where user intent is meaningfully distinct:

- `mathlibify-proofs`;
- `simplify-proofs`;
- `lean-proof-golf`;
- `lean-comparator`; and
- `mathlib-extension`.

These are opt-in. The normal Formalize flow must not silently refactor APIs,
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
2. Add a machine-readable transport manifest containing the attested archive
   SHA-256, 51 declared names, disposition, target layer, replacement owner,
   and whether code or only concepts may be reused.
3. Add a test requiring exactly 51 unique declared records, strict JSON, and
   the decision totals above. This proves internal policy consistency, not ZIP
   completeness.
4. Test that every decision and delivery unit has a registered owner, every
   dependency names an earlier unit, the graph is acyclic, and the plan and
   manifest carry identical owner/repository/branch/stack/dependency/approval
   fields. Only approval-gated C-series units may leave the repository unbound.
5. Add a negative test rejecting a repository skill directory not present in
   the manifest's current-skill inventory.

Pass condition: the manifest and numbered policy table agree exactly, no
declared skill is unclassified or multiply owned, and the source-completeness
claim remains explicitly external until a generated member inventory exists.

### Phase 1: semantic quality on `main`

Implement P01–P03 through
[FORMALIZATION_QUALITY_GOAL.md](FORMALIZATION_QUALITY_GOAL.md). P07 separately
merges the nonduplicative audit rules from `audit-lean-to-text` and
`verify-lean-translation` into their existing Agent Review owner.

Required tests:

- every completion-bearing field triggers the visible seven-gate contract even
  when `declaration` or statement/proof flags are absent;
- a completion claim on a container fails, while declaration-only planning
  continues to pass;
- every quality subject has explicit origin; omitted origin fails even with
  otherwise-current evidence;
- the seven-gate default-deny N/A matrix is exercised gate by gate, including
  cited/bridged/background origin and compiled Mathlib kind cases;
- compiled declaration kinds, not authored labels, govern Mathlib proof
  applicability; mixed theorem/definition roots, spoofed intent, and unresolved
  kinds fail closed;
- P02 versions and hashes Lean's compiled `type_is_prop` fact; proof-valued
  `def`/`opaque` roots fail proof N/A while data and predicate definitions pass;
- every missing, blocked, malformed, hidden, or internally inconsistent gate
  fails with a stable finding code;
- source-fidelity pass for cited work requires a current hash-bound read-back
  artifact; prose-only direct review, stale hashes, and every decision other
  than `agrees` fail;
- a compiling but deliberately weakened statement fails source review;
- a lexically visible declaration with a broken body and a stale old `.olean`
  both fail compiled Lean validity;
- cited external Mathlib declarations produce the same skeleton/read-back
  evidence through their recorded module; missing or mismatched modules fail;
- external modules outside the pinned Mathlib checkout, local shadows, and
  stale Mathlib artifacts fail closed;
- the checker leaves authored files unchanged and rejects escaping evidence
  links;
- verification and Pages both gate on build, quality, and integrity before
  render/deploy;
- every cited completed example node has an authorized line-located passage and
  an independently produced current verdict;
- the Codex, Claude Code, and Muse surfaces and skill discovery pass;
- a denylist scan finds no internal service/path/model identifiers.

Pass condition: all existing tests plus the positive and negative quality
fixtures pass, and a corrupted Cabannes fixture fails for the intended semantic
contract reason.

### Phase 2: portable search and equivalence

1. Add `mathlib-search`, merging portable Loogle behavior into one owner.
2. Teach Roadmap and Formalize to call the same exact/partial/missing search
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

### Phase 3: opt-in Formalize specialists

Work on `main` through the existing `formalize` skill. Preserve Markdown as
the only durable work graph, use the existing claim/worktree contract, and do
not restore a separate execution branch, worker daemon, or scheduler state.
The reusable `lean-proof` practices are already represented by Formalize, so
E01 is a completed baseline record rather than a new PR.

1. Keep `lean-proof` practices in the existing Formalize contract.
2. Add conservative `simplify-proofs`.
3. Add `mathlibify-proofs` for contribution preparation.
4. Add explicit `lean-proof-golf`; keep it out of default prompts and the
   normal Formalize flow.
5. Add `lean-comparator` and `mathlib-extension` specialists.

Required tests:

- each mutating specialist refuses work without a valid node claim;
- baseline and final declaration types are byte- or elaboration-equivalent;
- new `sorry`, `admit`, axioms, unsafe features, and forbidden evaluators fail;
- E02–E04 preserve the dispatched `open_statements` and `assumes` contract:
  permitted inherited `sorryAx` remains distinguished from every new or
  unlisted gap;
- target and broader builds plus axiom audits run after mutation;
- Mathlibify checks imports/style and cannot run as an automatic proof step;
- Golf requires explicit invocation and a declared metric improvement;
- Comparator preserves exact frozen interfaces and distinguishes infrastructure
  failure from rejection;
- article claims continue to prevent duplicate node work; before concurrent
  dispatch, resolve each specialist's Lean targets to normalized repository
  files and serialize overlapping or ambiguous file sets. Do not pretend
  arbitrary claim strings are canonical file aliases. For revisions, start
  with the merged `autoform work impact` claim set and add no competing impact
  graph.

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
uv run pytest -q tests/test_plugin_runtime.py tests/test_skill_examples.py
```

When a host-provided skill or plugin validator is installed, run it by its
resolved command and record that command and version; an unresolved placeholder
is never a release gate. When testing an updated local installation, use a
supported host reinstall flow; never hand-edit marketplace state or assume an
untracked repository helper exists.

Known supplemental host validators are:

```bash
claude plugin validate . --strict
muse plugins validate . --json
```

Their absence does not replace or weaken the repository-owned pytest gate.

Each wave must additionally prove:

- no unexpected working-tree changes or generated artifacts;
- no secret, credential, private endpoint, employee-specific path, or
  Meta-internal command entered the public package;
- every new CLI surface has deterministic JSON, documented exit codes, path
  containment tests, and authored-file immutability tests where applicable;
- positive fixtures pass and purpose-built negative fixtures fail for the
  expected reason;
- specialist mutation remains explicit and claim-gated; and
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
- it would silently make a specialist part of default Formalize execution.

## Final completion criterion

This transport program is complete when the manifest still accounts for all 51
externally attested skill names, every accepted capability lives in exactly one layer, all
adapted behavior is provider-neutral and test-gated, optional packs can be
removed without breaking core, and excluded internal/project-specific skills
have not leaked into Autoform's public runtime or documentation.
