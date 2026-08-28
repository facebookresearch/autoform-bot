# Worked review: supervision recovery

The Cabannes thesis example anchors its
[supervision-recovery node](../../setup/assets/cabannes-thesis-project/blueprint/roadmap/infimum-loss/theorems/supervision-recovery.md)
at `il:thm:non-ambiguity` in the “Infimum Loss” chapter. Follow its
[source map](../../setup/assets/cabannes-thesis-project/blueprint/sources/thesis.md)
and use the cited theorem, not the node summary, as the mathematical authority.

- **Faithfulness:** compare every eligibility and non-ambiguity hypothesis,
  ambient object, quantifier, and conclusion with the source theorem.
- **Proof integrity:** compile the declaration, search its proof chain for
  placeholders, and inspect `#print axioms` output.
- **Code quality:** inspect theorem shape, reuse, naming, and maintainability
  separately from whether the result is mathematically correct.

For example, a proof that compiles without `sorry` but silently drops the
source's non-ambiguity hypothesis may have strong integrity and still fail
faithfulness. Report that distinction rather than averaging it away.
