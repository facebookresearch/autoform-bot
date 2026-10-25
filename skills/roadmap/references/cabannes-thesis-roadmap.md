# Worked roadmap: Cabannes thesis

The companion [blueprint vault](../../setup/assets/cabannes-thesis-project/blueprint/README.md)
models the start of a formalization project for Vivien Cabannes's thesis,
*From Weakly Supervised Learning to Active Labeling*
([arXiv:2209.11629](https://arxiv.org/abs/2209.11629)). Confirm the actual
source revision adopted by the project before relying on its paths or labels.

The example is small:

- the roadmap maps all six mathematical chapters at coarse granularity;
- two decomposed chapters make the book navigation and cross-chapter DAG
  concrete: “Infimum Loss” contains the source targets, while “Full
  Supervision” contains a supporting Lean definition;
- the coverage page distinguishes mapped, decomposed, and out-of-scope material;
- the [source page](../../setup/assets/cabannes-thesis-project/blueprint/sources/thesis.md)
  records stable labels from `infimum/core.tex`; and
- seven DAG nodes decompose one representative “Infimum Loss” slice and its
  supporting full-supervision construction.

This is a structural example, not a completion fixture: only one of its six
chapter clusters has pull-request-sized nodes, and its `MAPPED` rows make the
vault an invalid finish state for a complete Roadmap pass over the thesis. Use
it to inspect article shape, source links, coverage, and dependencies, not as a
stopping point to copy. Its nodes happen to map one-to-one to main public
artifacts; a node may also carry several supporting declarations under one
unique main result.

The five source targets and the dependencies their articles list:

| Node | Depends on | Proof depends on |
| --- | --- | --- |
| Eligibility | None | None |
| Non-ambiguity | Eligibility | None |
| Infimum loss | Eligibility | None |
| Non-ambiguity determinism | Non-ambiguity | None |
| Supervision recovery | Infimum loss; Full supervision | Non-ambiguity determinism; Full supervision is non-ambiguous |

Two supporting nodes come from existing Lean declarations: full supervision, in
a separate formalization chapter, and the proof that it is non-ambiguous, filed
with the Infimum Loss theorems. They feed the supervision-recovery target
without posing as thesis statements.

Existing and planned mathematics coexist in one book here: five of the seven
nodes are already formalized, both supporting declarations and three of the
five source targets, while Infimum loss is ready to state and Supervision
recovery remains planned.
