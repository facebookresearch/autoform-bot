# Worked roadmap: Cabannes thesis

The companion [blueprint vault](../../setup/assets/cabannes-thesis-project/blueprint/README.md)
models the start of a formalization project for Vivien Cabannes's thesis,
*From Weakly Supervised Learning to Active Labeling*
([arXiv:2209.11629](https://arxiv.org/abs/2209.11629)). Confirm the actual
source revision adopted by the project before relying on its paths or labels.

The example stays intentionally small:

- the roadmap maps all six mathematical chapters at coarse granularity;
- two decomposed chapters make the book navigation and cross-chapter DAG
  concrete: “Infimum Loss” contains the source targets, while “Full
  Supervision” contains two supporting Lean declarations;
- the coverage page distinguishes mapped, partial, and out-of-scope material;
- the source page records stable labels from `infimum/core.tex`; and
- seven DAG nodes decompose one representative “Infimum Loss” slice and its
  supporting full-supervision construction.

These nodes happen to map one-to-one to main public artifacts. That is not a
format restriction: a node may carry several supporting definitions or
statements when one unique main result makes the whole node a coherent pull
request and review unit.

This asymmetry is deliberate. The six chapter entries are candidate roadmap
clusters, while only one cluster has pull-request-sized nodes. The example is not a
completed whole-thesis plan: comparison, consistency, learning-rate results,
and the other chapters still need source inspection and decomposition.

The detailed slice uses labels verified in `infimum/core.tex` from the public
[arXiv e-print source](https://export.arxiv.org/e-print/2209.11629):

| Node | Source label | Prerequisites |
| --- | --- | --- |
| Eligibility | `il:def:eligibility` | — |
| Non-ambiguity | `il:def:non-ambiguity` | — |
| Infimum loss | `il:thm:infimum-loss` | Eligibility |
| Non-ambiguity determinism | `il:thm:ambiguity` | Non-ambiguity |
| Supervision recovery | `il:thm:non-ambiguity` | Infimum loss; Non-ambiguity determinism |

Two supporting nodes are kept in a separate formalization chapter: full
supervision and the proof that it is non-ambiguous. They are grounded in the
existing Lean declarations and feed the source-level supervision-recovery
target without being presented as additional thesis statements.

Use the pattern—whole-source map, explicit coverage contract, approved small
slice, then dependency links—not the thesis mathematics. Validate the example
with `autoform check` and inspect its generated graph before returning ready
nodes to the user or a separately installed execution workflow.
