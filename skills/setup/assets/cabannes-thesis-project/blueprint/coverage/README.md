---
kind: coverage
status: in-progress
---

# Thesis coverage

Coverage is tracked at chapter level before every chapter has a theorem DAG.
The labels below are validated Autoform dispositions: `MAPPED`, `DECOMPOSED`,
`DEFERRED`, or `OUT`.

| Area | Coverage | Evidence |
| --- | --- | --- |
| Fast Rates for Structured Prediction | `MAPPED` | Listed in the roadmap; source audit pending |
| Exponential Convergence Rates for SVM | `MAPPED` | Listed in the roadmap; source audit pending |
| Infimum Loss | `DECOMPOSED` | [Five target nodes](../roadmap/infimum-loss/README.md) plus a [two-node supporting chapter](../roadmap/full-supervision/README.md) |
| Disambiguation Framework | `MAPPED` | Listed in the roadmap; source audit pending |
| Laplacian Regularization | `MAPPED` | Listed in the roadmap; source audit pending |
| Streaming Stochastic Gradients | `MAPPED` | Listed in the roadmap; source audit pending |
| Experiments and narrative material | `OUT` | Not a formalization target unless needed by a selected theorem |

## Completion rule

The current milestone is complete when the seven decomposed nodes compile under
their recorded Lean declarations and pass mathematical and code review. That
does not mean the thesis-wide roadmap is complete.
