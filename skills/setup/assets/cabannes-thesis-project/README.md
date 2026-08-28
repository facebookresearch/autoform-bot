# Autoform repository example

This compact repository models a realistic Lean formalization project. Setup
uses its infrastructure; the other skills use its populated Cabannes thesis
slice as a handoff example.

**Blueprint:** [Browse the formalization blueprint](blueprint/README.md).

Developed with [AutoformBot](https://github.com/facebookresearch/autoform-bot).

- `lean-toolchain`, `lakefile.toml`, and `CabannesThesis/` pin matching stable
  Lean and Mathlib `v4.32.2` releases.
- `blueprint/` is an Obsidian-compatible Markdown vault with roadmap, coverage,
  sources, and a seven-node theorem DAG spanning two formalization chapters.
- `mkdocs.yml` builds the `autoform render` output as a leanblueprint-styled
  mathematical book: an aggregate progress view, numbered statement boxes,
  direct Lean source icons, collapsed dependency details, and project, chapter,
  local-context, and full-DAG Mermaid maps. The render also records a
  deterministic, path-free `publication.json` manifest.
- `autoform-verify.yml` validates the DAG and Lean project on pull requests.
- `blueprint-pages.yml` renders and deploys the blueprint with GitHub Pages.

The DAG deliberately shows a partial state: the Full Supervision support
chapter is proved, Infimum Loss is ready to state, and the stronger
supervision-recovery target remains planned behind both chapters.

When adapting this repository, rename the Lean package and module, replace the
mathematics through Roadmap, merge existing ignore/workflow files, and check
for a newer matching stable Lean/Mathlib release. Refresh the default branch,
repository URL, and immutable Autoform/Action pins rather than copying them
blindly.
