# Worked orchestration: the Infimum Loss slice

In the Cabannes thesis example, `eligibility` and `non-ambiguity` have no
formalization prerequisites and may be assigned in parallel in separate
worktrees. `infimum-loss` waits for `eligibility`, while
`non-ambiguity-determinism` waits for `non-ambiguity`. The Full Supervision
support chapter can proceed alongside those branches. `supervision-recovery`
waits for both source branches and for its supporting definition and lemma.

For one ready article:

1. Confirm that the runtime projection marks it as a dispatchable leaf and that
   its typed prerequisites are satisfied.
2. Acquire its node claim before editing and keep the lease renewed during the
   attempt.
3. Open the cited thesis label and recover the exact assumptions and conclusion.
4. Search the target Lean project and pinned Mathlib checkout before choosing an
   API, then develop the declaration with the shared Lean tools.
5. Acquire the shared build claim, run the focused Lake target, and release the
   build claim when it finishes.
6. Ask independent agents to compare the complete Lean statement with the cited
   source and to inspect the proof for trust shortcuts.
7. Only then record the exact compiled declaration and truthful formalization
   assertions in the article. Release the node claim on success or failure.
8. Recheck the Markdown DAG. A newly unblocked leaf is the next work item;
   source order alone is not a scheduling rule.
