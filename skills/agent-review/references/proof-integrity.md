# Proof-integrity rubric

Judge whether the proof chain is genuine kernel-checked mathematical work. A plausible tactic block
is insufficient when its dependencies are hollow.

## Evidence

1. Compile the changed Lean files or target and quote the actual command result.
2. Run `#print axioms` for each reviewed public declaration and important new dependency. The normal
   Mathlib baseline is `propext`, `Classical.choice`, and `Quot.sound`; `sorryAx` is a proof gap.
3. Trace the declaration body and its project-local dependencies. Search project Lean files for
   `sorry`, `admit`, and raw `axiom`, then inspect every relevant hit rather than trusting grep alone.
4. Read the source to determine whether it proves the claimed result; difficulty or missing local
   infrastructure does not justify axiomatizing content that the source proves.
5. In a repository with an explicit audited axiom ledger, apply that repository's ledger and
   discharge policy in addition to this rubric.
6. If `roadmap/README.md` sets `open_statements: allowed`, list the open statements the reviewed
   article may assume (`autoform work assumptions`) and trace each `sorryAx` to its source, for
   example with the CI audit the [open statements reference](../../../autoform_cli/README.md#open-statements)
   reproduces, which logs each declaration as open, conditional, or sorry-free.

## Open statements

Under the open-statement policy, a `sorry` inside a declared open upstream statement, one the
reviewed article reaches through its Markdown dependencies and that `work assumptions` attributes
to it, is a recorded dependency of the reviewed declaration, not a gap in it. Score the reviewed
proof on its own work, name every assumed open statement in the review, and mark the verdict as due
for re-review when one is discharged, since the skeleton's axioms and hashes then move. A `sorry`
anywhere else, including a helper, a `where` clause, or the reviewed declaration's own statement, or
an open statement that is not a declared dependency, keeps the ceilings below. A conditional proof
is never axiom-clean, complete, or sorry-free, whatever its score.

## Structural red flags

- Returning an assumption or class field that already contains the conclusion.
- Orphan classes, circular theorem-smuggling fields, vacuous definitions, empty-domain instances,
  `Subsingleton.elim`, unjustified `False.elim`, or `exfalso` used to manufacture the main result.
- Raw axioms or sorries moved, renamed, split, or hidden behind helper declarations.
- `native_decide`, opaque computation, or unjustified `noncomputable` used to conceal the central
  mathematics rather than implement a legitimate decision procedure or classical construction.
- Supporting lemmas that appear substantive but are unused by the advertised theorem.

Using a strong existing Mathlib theorem instead of replaying the source proof is legitimate when
the instantiated theorem really implies the reviewed statement.

## Scores

| Score | Standard |
|---:|---|
| 5 | Compiles; only standard Lean axioms appear; the dependency chain is genuine and free of gaps or hollow constructions. |
| 4 | Genuine proof with a clean chain and only a narrowly justified, explicitly audited nonstandard assumption. |
| 3 | Genuine work with a small, explicit gap whose corresponding result the source itself does not prove. |
| 2 | Unjustified gaps or major structural concerns such as orphan classes, vacuity, or trivial instances. |
| 1 | Axiom/sorry covers content the source proves, or the proof is structurally hollow or circular. |
| 0 | The result is entirely fabricated by gaps, contradiction, vacuity, or circular assumptions. |

Hard ceilings: any axiom or sorry covering source-proved content scores at most 1; orphan classes,
vacuous definitions, or trivial instances score at most 2. Give a separate justified/unjustified
verdict for every nonstandard axiom, sorry, or structural issue found. Pass at 3; reject at 2 or
below, and never describe a result with a score-3 gap, or a conditional proof, as axiom-clean.
