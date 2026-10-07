# AutoformBot agent guidance

User instructions override this file. Use [CONTRIBUTING.md](CONTRIBUTING.md)
for contribution, style, and validation practices. Use the
[development skill](skills/develop-plugin/SKILL.md) for product-development
workflow and cross-file maintenance.

## Product judgment

- Before fixing, restacking, or merging an existing proposal, identify the
  concrete user outcome or maintenance burden it improves. A request to resolve
  review blockers does not prove that the proposal belongs in the repository.
- Optimize for repository quality and a good, simple product—not merged line
  count, PR count, or green checks. Documentation and tests that merely validate
  their own planning state are not a substitute for useful behavior.
- Treat declining, closing, deleting, or replacing a proposal as a successful
  engineering outcome when it removes duplication, obsolete machinery, or
  needless repository state. Preserve genuinely reusable insight in the owning
  skill or a focused issue. Remove the inferior artifact only when authorized;
  otherwise recommend its closure or removal.

## Repository invariants

- Treat current `main` as the product baseline unless an explicitly documented
  stack says otherwise. Unmerged replacements do not supersede it.
- Develop the plugin here and exercise installed behavior in an independent Lean
  consumer checkout. The bundled Cabannes thesis is a compatibility example,
  never a source of product-specific exceptions.
- Markdown under `blueprint/` is the authored roadmap and dependency graph.
  Rendered sites, Mermaid graphs, dashboards, and runtime projections are
  derived views, not another durable scheduler or graph.
- Public CLI and JSON shapes are contracts. Lean server work requires an
  explicit project root, bounded time and output, verified descendant cleanup,
  and no retry after dispatch with an unknown outcome. A project root is not an
  OS sandbox.
- Claims coordinate across machines through Git `refs/autoform-claims/*`. Only
  the dashboard's publication overlay is loopback-only, ephemeral, and excluded
  from the vault and public site. Pushing claims or publishing requires
  user-supplied authority.
- The bundled example mirrors generated workflows (modulo its Autoform pin), the
  audit helper, and site configuration (modulo project fields); keep their
  focused equivalence tests aligned. Do not assume other templates, including
  ignore rules, are equivalent.
- Keep release versions synchronized across `pyproject.toml`, `uv.lock`, Claude
  and Muse manifests, and the semantic base of Codex's cachebuster version.
