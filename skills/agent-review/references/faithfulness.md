# Faithfulness rubric

Judge whether the Lean statement captures the source statement at full strength. Do not score proof
quality here.

## Evidence

1. Read the original source passage itself; comments and docstrings are not substitutes.
2. Read the complete Lean signature, supporting definitions, relevant instances, and nearby public
   declarations that may jointly formalize a multi-part statement.
3. Compare quantifiers, hypotheses, domains, locality, conclusions, and edge cases side by side.
4. Check that source-specific objects have substantive definitions. Reusing a standard Mathlib
   concept or typeclass is correct when it genuinely matches the source concept.

If the source is unavailable, report insufficient evidence instead of inventing a score. The human
trust surface includes every new or changed definition, public statement, axiom, instance, notation,
coercion, and meaning-changing attribute; proof-only helpers need only be classified correctly.

## Common failures

- Dropped conclusions, reordered or restricted quantifiers, extra nonredundant hypotheses, or a
  different underlying type, function space, topology, measure, or finiteness condition.
- Replacing an explicit source quantity by an abstract proxy without proving their connection.
- Encoding the desired conclusion in a project-defined class field or hypothesis, especially when
  the class has no real instances.
- Hollow definitions that ignore parameters, theorem-like `def`s returning `Prop`, vacuous domains,
  `True` substitutions, or simplified models never connected to the source objects.
- Treating a comment that rationalizes a deviation as evidence that the deviation is acceptable.

## Scores

| Score | Standard |
|---:|---|
| 5 | Same mathematical objects, quantifiers, hypotheses, locality, and conclusions; every part is represented and supporting definitions are genuine. |
| 4 | Very close; only redundant or implementation-level assumptions such as decidability differ. |
| 3 | Mathematically equivalent with genuinely superficial differences such as naming, coercions, or a slightly stronger harmless typeclass. |
| 2 | Some structure matches, but a domain, hypothesis, conclusion, or modeling choice materially changes the theorem. |
| 1 | Major strengthening, weakening, or quantifier mismatch; the advertised result is largely not what Lean states. |
| 0 | Unrelated or vacuous statement, hollow model, or trivial replacement of the source result. |

Hard ceilings: a wrong domain/type, missing conclusion, nonredundant hypothesis change, or content
hidden in an orphan class scores at most 2. Score 3 is reserved for differences that do not alter
the mathematics. Pass at 4; reject at 2 or below.
