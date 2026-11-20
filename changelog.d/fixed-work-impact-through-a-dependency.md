- **Work impact through a dependency.** `work impact` left out a root
  declaration impacted only through a dependency's declarations: a theorem
  stating `Dep.X.x = 1`, where the dependency defines `Dep.X.x` as the revised
  `Demo.I.i`, was in neither `statement_impacted` nor `claim_targets`, and a
  proof that uses the dependency's alias of a revised theorem, or the `simp`
  lemma the dependency made from it, was in neither `proof_impacted` nor
  `claim_targets`. That included a `by simp` proof in a root module that
  applies the same `attribute [simp]` itself, when Lean attributed the lemma
  both make to the dependency's module. The probe now also reads the
  declarations of each dependency module that imports a root module, directly
  or not, and the impact runs through them as through root declarations; none
  of them is reported as impacted, as a helper or as a claim target. A root
  helper declared under one of them, in a root declaration's namespace, is now
  owned through it by the article naming that root declaration, so its claim
  target, in `work impact` and in `work import-impact`, is that article's
  rather than a key of its own. Each record the probe gives now says whether
  it is a dependency's, so `build_revision` changes for every project (#826).
