# Infimum Loss milestone

This chapter isolates the conditions under which weak observations still
determine useful supervised information. It begins with eligibility and
non-ambiguity, then follows their consequences through the infimum-loss and
supervision-recovery results.

## Definitions

A weak observation is a set-like predicate describing which labels remain
possible. The first two definitions say when a label is admitted and when that
admission rule determines at most one label.

- [Eligibility](definitions/eligibility.md)
- [Non-ambiguity](definitions/non-ambiguity.md)

## Results

The infimum loss turns an observed set into an ordinary loss by choosing its
best compatible label. Non-ambiguity then supplies the uniqueness needed to
recover the supervised solution.

- [Infimum loss](theorems/infimum-loss.md)
- [Non-ambiguity determinism](theorems/non-ambiguity-determinism.md)
- [Full supervision is non-ambiguous](theorems/supervision-non-ambiguous.md)
- [Supervision recovery](theorems/supervision-recovery.md)
