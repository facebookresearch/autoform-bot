# Unused hypotheses in a Formalize run

[Formalize](../SKILL.md) runs Batteries' linter as
`#lint only unusedArguments in <Module>` after `import <Module>`, where
`<Module>` declares the article's theorems; act only on its findings for those
theorems. From its release for Lean v4.32.0 it skips an argument named with a
leading `_` and an unnamed one other than an instance, such as the antecedent of
`p → q`; its releases for Lean v4.30.0 and v4.31.0 check no theorem at all, so a
clean run there is not evidence.

A finding, from Lean's warnings or the linter, means the proof never uses that
argument:

- If the statement assumes something the source derives, such as a supremum
  the source obtains from boundedness, restate it to conclude what the source
  concludes.
- If a section `variable` brings it in, add `omit [C] in` for an instance or
  `omit h in` for an `include h`.
- If the source states the hypothesis and Lean does not need it, such as
  `y n ≠ 0` where `x / 0 = 0`, keep it.
- If the theorem genuinely holds without it, such as a limsup result that holds
  in the extended reals, keep the source's statement for the article and prove
  the general one as a separate lemma that it follows from.

Name a hypothesis you keep with a leading `_` and add one sentence to the
article giving the reason in mathematical or source terms.
