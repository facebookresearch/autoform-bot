# AutoformBot agent guidance

User instructions override this file. [CONTRIBUTING.md](CONTRIBUTING.md)
covers contribution, style, and validation; the
[development skill](skills/develop-plugin/SKILL.md) covers product-development
workflow and cross-file maintenance.

## Product judgment

- Name the user outcome or maintenance burden before fixing, restacking, or
  merging a proposal; a request to resolve review blockers does not justify it.
- Optimize for repository quality and a simple product, not PR count, green
  checks, or docs and tests that only validate their own planning state.
- Closing or replacing a proposal that removes duplication is a success. Keep
  reusable insight in the owning skill or an issue; remove an artifact only when
  authorized.

## Repository invariants

- Treat current `main` as the product baseline unless an explicitly documented
  stack says otherwise. Unmerged replacements do not supersede it.
- Develop the plugin here and exercise installed behavior in an independent Lean
  consumer. The bundled Cabannes thesis is never a source of product exceptions.
- Markdown under `blueprint/roadmap/` is the only roadmap and dependency graph.
  Implementation handoffs live only in per-article files under
  `blueprint/.implementation-notes/`; rendered sites, dashboards, and runtime
  projections are derived views ([CLI reference](autoform_cli/README.md)).
- In consumer formalization work, Lean imports are allowed;
  [Formalize](skills/formalize/SKILL.md#import-edits) owns that policy. Never
  replace it with a blanket import ban.
- Public CLI and JSON shapes are contracts. Lean server work requires an
  explicit project root, bounded time and output, verified descendant cleanup,
  and no retry after dispatch with an unknown outcome. A project root is not an
  OS sandbox.
- Claims are optional and coordinate through Git `refs/autoform-claims/*`; the
  dashboard's publication overlay is loopback-only, ephemeral, and kept out of
  the vault and public site. Pushing claims or publishing requires
  user-supplied authority.
- The bundled example mirrors only the artifacts listed under
  [generated mirrors](skills/develop-plugin/references/repository-contracts.md#generated-mirrors);
  ignore rules are not among them.
- Note each user-visible change under Unreleased in `CHANGELOG.md`, and keep
  release versions synchronized across `pyproject.toml`, `uv.lock`, the Claude
  and Muse manifests, and the semantic base of Codex's cachebuster version.

## Inform, don't break

Autoform's commands inform the orchestrator; they must not break a codebase
(#325). A new check that a repository could fail warns and keeps the exit
status, so a repository that passed before still passes and the orchestrator
decides. A command exits nonzero only when it cannot give a correct answer:
unreadable input, a stale or missing build it would otherwise misreport, a
contract it cannot honor, or evidence it would have to forge. Never add a
refusal to the graph loader, where it stops every command. The
[CLI reference](autoform_cli/README.md) gives each command's exit statuses and
warnings.
