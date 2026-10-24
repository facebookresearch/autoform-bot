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
- Markdown under `blueprint/roadmap/` is the authored roadmap and dependency
  graph. Versioned implementation handoffs live only in hidden, per-article
  files under `blueprint/.implementation-notes/`; they are not mathematics or
  graph state. Rendered sites, the vault's dependency summary, dashboards, and
  runtime projections are derived views, not another durable scheduler or graph.
- In consumer formalization work, Lean imports are allowed;
  [Formalize](skills/formalize/SKILL.md#import-edits) owns that policy. Never
  replace it with a blanket import ban.
- Public CLI and JSON shapes are contracts. Lean server work requires an
  explicit project root, bounded time and output, verified descendant cleanup,
  and no retry after dispatch with an unknown outcome. A project root is not an
  OS sandbox.
- Claims are optional; when used, they coordinate across machines through Git
  `refs/autoform-claims/*`. Only the dashboard's publication overlay is
  loopback-only, ephemeral, and excluded from the vault and public site. Pushing
  claims or publishing requires user-supplied authority.
- The bundled example mirrors generated workflows (modulo its Autoform pin), the
  audit helper, and site configuration (modulo project fields); keep their
  focused equivalence tests aligned. Do not assume other templates, including
  ignore rules, are equivalent.
- Keep release versions synchronized across `pyproject.toml`, `uv.lock`, Claude
  and Muse manifests, and the semantic base of Codex's cachebuster version.

## Inform, don't break

Autoform's commands inform the orchestrator; they must not break a codebase
(#325). A new check that a repository could fail reports a warning and keeps the
exit status, so a repository that passed before still passes, and the
orchestrator decides what to do. A command exits nonzero only when it cannot
give a correct answer: input it cannot read, a stale or missing build it would
otherwise misreport, a contract it cannot honor, or evidence it would have to
forge. Never add a refusal to the graph loader, where it stops every command.
The [CLI reference](autoform_cli/README.md) gives each command's exact rules.

| Command | Exits nonzero for | Warns, exit unchanged, for |
| --- | --- | --- |
| `check` | invalid graph or `## Sources` structure; a `lean:` name missing or ambiguous under `--lean-root` | what work and search omit, byte-order marks, container declarations, library declarations citing the blueprint's sources |
| `audit` | any finding; reporting findings is its job | none |
| `render` | an input it refuses to publish (a symlink, a special or sensitive file), an output that overlaps the source, unresolved names under `--require-declarations`, a review it cannot derive or trust under `--review` | unresolved names otherwise, omitted repository links, a card formula shown as typed |
| `skeleton` | a stale or missing build, an unresolved or refused declaration | none; its report is trust evidence |
| `review` | `prepare`, `record`: evidence or input it cannot derive, read, or trust, testimony the read-back rules refuse, a card conflict (2); `check`: any finding, reporting them is its job (1), and evidence it cannot derive or a blueprint changed during the run (2) | `record`: each formula a site would show as typed |
| `work`, `search` | invalid usage (2); bytes changed since the graph loaded; a refused library; under `--module-scope import-closure`, a file outside the captured sources that may import the closure | none; they leave out values they cannot represent, which `check` warns about |
| `library index` | a missing or stale build (2); a stale index under `--check` (1) | none |
| `project`, `doctor` | an inspection or catalog error, a failed diagnosis | a release pairing it cannot resolve |
| `init` | an input or target it cannot use safely: a case or Unicode variant of a path it manages (even with `--force`), a link or unusable root `.gitignore` in the way, a directory it cannot list, an invalid option or pinned Autoform tree | the existing entries it left alone; CI it skips when there is no Autoform ref to pin |
