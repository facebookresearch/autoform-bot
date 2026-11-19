- **Import impact through a dependency.** `work import-impact` left out of
  `affected_modules` a root module that imports `MODULE` only through a
  dependency's modules. When `Demo` imports the dependency's `Dep.X`, which
  imports `Demo.I`, which imports `Demo.A`, an import edit to `Demo.A` changes
  `Demo` too, yet only `Demo.A` and `Demo.I`, and their articles, were listed.
  The probe now records each root module's imports through modules outside the
  root package too, so `Demo` and its articles are listed, and
  `module_graph_revision` changes where such a path exists. The default scope,
  which `--module-scope import-closure` asks for when a dependency's file may
  import a candidate, now covers this case (#754).
