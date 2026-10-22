# Autoform repository example

This compact repository models a realistic Lean formalization project. Setup
uses its infrastructure; the other skills use its populated Cabannes thesis
slice as a handoff example.

**Blueprint:** [Browse the formalization blueprint](blueprint/README.md).

Developed with [AutoformBot](https://github.com/facebookresearch/autoform-bot).

- `lean-toolchain`, `lakefile.toml`, and `lake-manifest.json` pin matching
  stable Lean and Mathlib `v4.32.2` releases plus the upstream REPL commit
  tagged `v4.32.0`; the Lean sources are in `src/`.
- `blueprint/` is an Obsidian-compatible Markdown vault with roadmap, coverage,
  sources, and a seven-node theorem DAG spanning two formalization chapters.
- `mkdocs.yml` builds the `autoform render` output as a leanblueprint-styled
  mathematical book: an aggregate progress view, numbered statement boxes,
  direct Lean source icons, collapsed dependency details, and dependency maps
  of the project and each chapter. The render also records a deterministic,
  path-free `publication.json` manifest.
- `autoform-verify.yml` validates the DAG and Lean project on pull requests.
- `blueprint-pages.yml` renders and deploys the blueprint with GitHub Pages.
- `.github/CODEOWNERS.autoform.example` suggests owner rules for CI and the
  roadmap policy without changing the repository's active CODEOWNERS.

The DAG deliberately shows a partial state: the Full Supervision support
chapter is proved, Infimum Loss is ready to state, and the stronger
supervision-recovery target remains planned behind both chapters.

When adapting this repository, rename the Lean package and module, replace the
mathematics through Roadmap, merge existing ignore/workflow files, and keep
the recommended release from `autoform project versions` unless another Lean
version is needed. Refresh the default branch,
repository URL, and immutable Autoform/Action pins rather than copying them
blindly; for another Lean version, also move the REPL pin as the setup skill
describes and regenerate `lake-manifest.json` with `lake update`.
