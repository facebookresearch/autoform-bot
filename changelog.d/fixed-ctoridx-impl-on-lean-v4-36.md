- **The Lean audit on Lean v4.36.** From v4.36, Lean gives every inductive type
  that eliminates into `Type` an unsafe `T.ctorIdx._impl` for the compiler, and
  both the kernel-trust and the open-statement audit refused it as an unsafe
  declaration, so a project on v4.36 with any such type failed CI's Lean audit
  before its kernel replay ran. The audit now skips that declaration, as it skips
  the `_unsafe_rec` copies of recursive definitions, when the type's safe
  `T.ctorIdx` names it as its `implemented_by`. An unsafe constant with the name
  alone is still refused, and so is a `lean:` target that names one (#880).
