- **Deprecated declarations still in use.** `work impact` called a deprecated
  root declaration safe to delete, in `deprecated_unused`, while a dependency's
  declaration still used it, or while an `attribute [simp]` line in another
  module, a root module's or a dependency's, still named it: the dependency's
  declaration was left out of `deprecated[].users`, and the `simp` lemma that
  line makes was taken to go with the deprecated theorem. Both are now among
  its users (#826).
