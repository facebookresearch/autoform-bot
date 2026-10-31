# Autoform repository example

This compact repository models a realistic Lean formalization project. Setup
uses its infrastructure; the other skills use its populated Cabannes thesis
slice as a handoff example.

**Blueprint:** [Browse the formalization blueprint](blueprint/README.md).

Developed with [AutoformBot](https://github.com/facebookresearch/autoform-bot).

The project pins stable Lean and Mathlib `v4.32.2` releases plus the upstream
REPL commit tagged `v4.32.0`. `autoform render` writes the blueprint as MkDocs
source for a leanblueprint-styled mathematical book with an aggregate progress
view, direct Lean source icons, and dependency maps of the project and each
chapter.

Its theorem DAG deliberately shows a partial state: the Full Supervision
chapter is proved; in the Infimum Loss chapter the infimum loss theorem is
ready to state, and the stronger supervision-recovery target stays planned
behind it and Full Supervision.

To start a project, do not copy this one: Setup's `autoform project new` and
`autoform init` write the layout with current pins.
