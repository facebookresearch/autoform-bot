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
  Supervision” contains two supporting Lean declarations;
- the coverage page distinguishes mapped, partial, and out-of-scope material;
- the source page records stable labels from `infimum/core.tex`; and
- seven DAG nodes decompose one representative “Infimum Loss” slice and its
  supporting full-supervision construction.

This is a structural example, not a completion fixture: only one of its six
chapter clusters has pull-request-sized nodes, and its `MAPPED` rows make the
vault an invalid finish state for a complete Roadmap pass over the thesis. Use
it to inspect article shape, source links, coverage, and dependencies, not as a
stopping point to copy. Its nodes happen to map one-to-one to main public
artifacts; a node may also carry several supporting declarations under one
unique main result.

The detailed slice uses labels verified in `infimum/core.tex` from the public
[arXiv e-print source](https://export.arxiv.org/e-print/2209.11629):

| Node | Source label | Prerequisites |
| --- | --- | --- |
| Eligibility | `il:def:eligibility` | None |
| Non-ambiguity | `il:def:non-ambiguity` | None |
| Infimum loss | `il:thm:infimum-loss` | Eligibility |
| Non-ambiguity determinism | `il:thm:ambiguity` | Non-ambiguity |
| Supervision recovery | `il:thm:non-ambiguity` | Infimum loss; Non-ambiguity determinism |

Two supporting nodes sit in a separate formalization chapter: full supervision
and the proof that it is non-ambiguous. Grounded in existing Lean declarations,
they feed the supervision-recovery target without posing as thesis statements.

Existing and planned mathematics coexist in one book here: five of the seven
nodes are already formalized, both supporting declarations and three of the
five source targets, while Infimum loss is ready to state and Supervision
recovery remains planned.
