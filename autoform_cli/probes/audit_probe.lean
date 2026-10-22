import Lean.Elab.Command
import Lean.Data.Json
import Lean.Replay

open Lean Elab Command

-- The kernel-trust probe that `.github/autoform_audit.py` writes. Its source is
-- autoform-bot's autoform_cli/probes/audit_probe.lean: the helper carries a
-- verbatim copy and appends one `run_cmd` line, which passes the build's modules
-- and the contract's table to `autoformAuditStrict` or `autoformAuditOpen`.
--
-- The header is the toolchain's Lean alone. The build is imported at run time,
-- into an environment of its own, so none of its declarations, instances,
-- macros, elaborators or initializers apply to this file.

/-- A name spelled as its components: strings, and numbers for numeric ones. -/
def autoformAuditNameOf (json : Json) : Except String Name := do
  let mut name := Name.anonymous
  for part in (← json.getArr?) do
    match part with
    | .str text => name := Name.str name text
    | _ => name := Name.num name (← part.getNat?)
  return name

/-- The root package's modules, each spelled as its components. -/
def autoformAuditReadModules (text : String) : Except String (List Name) := do
  (← (← _root_.Lean.Json.parse text).getArr?).toList.mapM autoformAuditNameOf

/-- Per `lean:` target: its name, its article, the article's state, and whether
a wanted placeholder may back that state. -/
def autoformAuditReadTargets (text : String) : Except String (Array (Name × String × String × Bool)) := do
  (← (← _root_.Lean.Json.parse text).getArr?).mapM fun entry => do
    let declName ← autoformAuditNameOf (← entry.getArrVal? 0)
    let article ← (← entry.getArrVal? 1).getStr?
    let state ← (← entry.getArrVal? 2).getStr?
    let mayBeWanted ← (← entry.getArrVal? 3).getBool?
    return (declName, article, state, mayBeWanted)

/-- Per article declaration: its name, its article, whether the article is an
open statement, the open statements the declaration may rest on, the article's
state, and whether a wanted placeholder may back that state. -/
def autoformOpenAuditReadArticles (text : String) :
    Except String (Array (Name × String × Bool × Array Name × String × Bool)) := do
  (← (← _root_.Lean.Json.parse text).getArr?).mapM fun entry => do
    let declName ← autoformAuditNameOf (← entry.getArrVal? 0)
    let article ← (← entry.getArrVal? 1).getStr?
    let isOpen ← (← entry.getArrVal? 2).getBool?
    let allowedOpen ← (← (← entry.getArrVal? 3).getArr?).mapM autoformAuditNameOf
    let state ← (← entry.getArrVal? 4).getStr?
    let mayBeWanted ← (← entry.getArrVal? 5).getBool?
    return (declName, article, isOpen, allowedOpen, state, mayBeWanted)

/-- The wrapper at the end of a Batteries wanted placeholder's type. Batteries
elaborates `theorem_wanted` and `proof_wanted` to a private `def` of type
`ProofWanted T`, `def_wanted` and `instance_wanted` to one of type `DefWanted T`,
and a `def_wanted` with a body to one of type `DerivedWanted T`. -/
partial def autoformAuditWantedHead : Expr → Option Name
  | .forallE _ _ body _ => autoformAuditWantedHead body
  | .mdata _ e => autoformAuditWantedHead e
  | e => match e.getAppFn.constName? with
    | some head => if head == `ProofWanted || head == `DefWanted || head == `DerivedWanted then some head else none
    | none => none

/-- The root package's wanted placeholders by the name an article writes, which
`env.find?` misses because Batteries declares each one `private`. -/
def autoformAuditWantedIndex (env : Environment) (isRoot : Name → Bool) : Std.HashMap Name (Array Name) := Id.run do
  let mut index : Std.HashMap Name (Array Name) := {}
  for (declName, info) in env.constants do
    if isRoot declName && isPrivateName declName && (autoformAuditWantedHead info.type).isSome then
      let user := privateToUserName declName
      index := index.insert user ((index.getD user #[]).push declName)
  return index

/-- What a target names: the declaration of that name, else the one root-package
wanted placeholder of that name. The error is how many placeholders share the
name: none, or several, which select none, as the lexical index does. -/
def autoformAuditResolve (env : Environment) (wanted : Std.HashMap Name (Array Name)) (declName : Name) :
    Except Nat (Name × ConstantInfo) :=
  match env.find? declName with
  | some info => .ok (declName, info)
  | none => match (wanted.getD declName #[]).toList with
    | [placeholder] => match env.find? placeholder with
      | some info => .ok (placeholder, info)
      | none => .error 0
    | found => .error found.length

/-- The types and constructors of one mutual inductive block refer to each
other. Nothing else in a safe environment does, so the walk treats a block as
one node and needs no cycle handling. -/
def autoformAuditBlockOf (env : Environment) (declName : Name) : Name :=
  let induct := match env.find? declName with
    | some (.ctorInfo info) => info.induct
    | _ => declName
  match env.find? induct with
  | some (.inductInfo info) => info.all.head?.getD induct
  | _ => declName

/-- The constants a node refers to, as `collectAxioms` follows them, except that
an open statement's proof is not entered: a dependent rests on the statement,
whatever its proof uses. -/
def autoformAuditEdges (env : Environment) (openSet : Std.HashSet Name) (node : Name) : Array Name :=
  match env.find? node with
  | some (.inductInfo info) =>
    info.all.foldl (init := (#[] : Array Name)) fun used induct =>
      match env.find? induct with
      | some (.inductInfo block) =>
        block.ctors.foldl (init := used ++ block.type.getUsedConstants) fun used ctor =>
          used ++ ((env.find? ctor).map (·.type.getUsedConstants)).getD #[]
      | _ => used
  | some (.thmInfo info) =>
    if openSet.contains node then info.type.getUsedConstants
    else info.type.getUsedConstants ++ info.value.getUsedConstants
  | some (.defnInfo info) => info.type.getUsedConstants ++ info.value.getUsedConstants
  | some (.opaqueInfo info) => info.type.getUsedConstants ++ info.value.getUsedConstants
  | some info => info.type.getUsedConstants
  | none => #[]

/-- The axioms a node depends on, sorted as `collectAxioms` sorts them. The
walk reads types and values itself instead of the axiom tables the oleans
store, keeps an explicit stack because dependency chains are deep and this
runs in the interpreter, and shares its cache across calls. A name that no
module declares counts as an axiom, so every check fails closed on it. -/
def autoformAuditAxioms (env : Environment) (declName : Name) :
    StateM (Std.HashMap Name (Array Name)) (Array Name) := do
  let start := autoformAuditBlockOf env declName
  if let some known := (← get).get? start then
    return known
  modify (·.insert start #[])
  let mut stack : Array (Name × Array Name × Nat) := #[(start, autoformAuditEdges env {} start, 0)]
  while !stack.isEmpty do
    let (node, edges, next) := stack.back!
    if h : next < edges.size then
      stack := stack.set! (stack.size - 1) (node, edges, next + 1)
      let target := autoformAuditBlockOf env edges[next]
      unless (← get).contains target do
        modify (·.insert target #[])
        stack := stack.push (target, autoformAuditEdges env {} target, 0)
    else
      let mut axioms : Array Name := match env.find? node with
        | some (.axiomInfo _) | none => #[node]
        | _ => #[]
      for used in edges do
        for found in ((← get).get? (autoformAuditBlockOf env used)).getD #[] do
          unless axioms.contains found do
            axioms := axioms.push found
      modify (·.insert node (axioms.qsort Name.lt))
      stack := stack.pop
  return ((← get).get? start).getD #[]

/-- For every computable recursive definition, `partial def` included, Lean adds
a partial copy `X._unsafe_rec` for the compiler to run. No safe declaration can
refer to one, and the kernel replay skips them, so the root scan skips them too.
A `lean:` target that names one is still refused. -/
def autoformAuditCompiledCopy (declName : Name) (info : ConstantInfo) : Bool :=
  info.isPartial && (Compiler.isUnsafeRecName? declName).isSome

/-- Whether a declaration is unsafe or partial, as the skeleton probe reads
safety: a `partial def` is a safe `opaque` whose compiled copy is partial, so
the audit refuses it under its own name. -/
def autoformAuditUnsafeOrPartial (env : Environment) (declName : Name) (info : ConstantInfo) : Bool :=
  info.isUnsafe || info.isPartial ||
    ((info matches .opaqueInfo _) && (env.find? (Compiler.mkUnsafeRecName declName)).any (·.isPartial))

/-- The open statements a node rests on. A constant outside the root package
contributes none: the audit requires those to be free of `sorry`. -/
partial def autoformOpenAuditOpenHits (env : Environment) (isRoot : Name → Bool) (openSet : Std.HashSet Name)
    (node : Name) : StateM (Std.HashMap Name (Array Name)) (Array Name) := do
  if let some known := (← get).get? node then
    return known
  -- In progress. Only recursion through unsafe or partial constants comes back here, and every root that
  -- reaches it fails the safety check or the kernel replay.
  modify (·.insert node #[])
  let mut hits : Array Name := if openSet.contains node then #[node] else #[]
  for used in autoformAuditEdges env openSet node do
    if used == ``sorryAx || !isRoot used then
      continue
    let target := autoformAuditBlockOf env used
    if target == node then
      continue
    for hit in (← autoformOpenAuditOpenHits env isRoot openSet target) do
      unless hits.contains hit do
        hits := hits.push hit
  hits := hits.qsort Name.lt
  modify (·.insert node hits)
  return hits

/-- Whether a node rests on a root declaration that failed the audit, through the
same edges as `autoformOpenAuditOpenHits`: a declaration whose `where` clause or
other auxiliary failed gets no status line. -/
partial def autoformOpenAuditReachesFailed (env : Environment) (isRoot : Name → Bool)
    (openSet : Std.HashSet Name) (failed : Std.HashSet Name) (node : Name) :
    StateM (Std.HashMap Name Bool) Bool := do
  if let some known := (← get).get? node then
    return known
  modify (·.insert node false)
  let mut broken := failed.contains node
  for used in autoformAuditEdges env openSet node do
    if broken then
      break
    if used == ``sorryAx || !isRoot used then
      continue
    let target := autoformAuditBlockOf env used
    if target == node then
      continue
    broken := failed.contains used || (← autoformOpenAuditReachesFailed env isRoot openSet failed target)
  modify (·.insert node broken)
  return broken

def autoformOpenAuditNameList (names : Array Name) : MessageData :=
  MessageData.joinSep (names.toList.map MessageData.ofName) ", "

/-- Whether a declaration of the build belongs to one of the root package's modules.
`moduleNames` is `env.header.moduleNames`, which builds a new array on every call,
so the caller computes it once rather than once for each constant. -/
def autoformAuditIsRoot (env : Environment) (moduleNames : Array Name) (targetModules : List Name)
    (declName : Name) : Bool :=
  match env.getModuleIdxFor? declName with
  | some moduleIdx => targetModules.contains moduleNames[moduleIdx.toNat]!
  | none => false

/-- The build: the root package's modules and what they import, loaded from the
search path in `AUTOFORM_AUDIT_LEAN_PATH` once nothing on it can stand in for the
toolchain's library or for a root module, and once no theorem of the build
stands in for an axiom. -/
def autoformAuditImportBuild (targetModules : List Name) : CommandElabM Environment := do
  -- `lake env` puts the project's libraries ahead of the toolchain's, so a
  -- root module under `Lean` or `Init` could have replaced this header.
  if (← IO.getEnv "LEAN_PATH").isSome then
    throwError "LEAN_PATH is set, so this probe's imports may come from the project; run it with plain lean, not lake env: AUTOFORM_AUDIT_LEAN_PATH=\"$(lake env printenv LEAN_PATH)\" LEAN_ABORT_ON_PANIC=1 lean PROBE"
  let some projectPath ← IO.getEnv "AUTOFORM_AUDIT_LEAN_PATH"
    | throwError "AUTOFORM_AUDIT_LEAN_PATH is not set; run the probe as AUTOFORM_AUDIT_LEAN_PATH=\"$(lake env printenv LEAN_PATH)\" LEAN_ABORT_ON_PANIC=1 lean PROBE"
  -- The toolchain's library comes first, so `propext` and every other core
  -- name mean the toolchain's constants. A root module under one of its
  -- entries would be looked up there instead: it would fail to load, or the
  -- toolchain's module of the same name would load and go unaudited.
  let libDir ← getLibDir (← getBuildDir)
  let projectEntries := System.SearchPath.parse projectPath
  let mut seenRoots : Array String := #[]
  for moduleName in targetModules do
    let root := moduleName.getRoot.toString (escape := false)
    if (← (libDir / root).isDir) || (← (libDir / (root ++ ".olean")).pathExists) then
      throwError "root module {moduleName} shares its first component {root} with the toolchain's library; rename the module so the audit can run"
    if seenRoots.contains root then
      continue
    seenRoots := seenRoots.push root
    -- Lean loads a module from the first entry that holds its first component,
    -- and Lake lists dependency libraries first, so a dependency that ships a
    -- file under the same component would stand in for the root package's.
    let mut holders : Array System.FilePath := #[]
    for entry in projectEntries do
      if (← (entry / root).isDir) || (← (entry / (root ++ ".olean")).pathExists) then
        let real ← IO.FS.realPath entry
        unless holders.contains real do
          holders := holders.push real
    if holders.size > 1 then
      let listed := ", ".intercalate (holders.toList.map (·.toString))
      throwError "root module {moduleName} shares its first component {root} with another library on the search path ({listed}); rename the module so the audit can run"
  searchPathRef.set (libDir :: projectEntries)
  -- Without extensions no `initialize` block of the build runs. `Init` comes
  -- along even when the build never imports it, so a build that declares a
  -- core name of its own, such as `propext`, clashes with the toolchain's on
  -- import, is merged with it when it is an identical theorem or Prop-valued
  -- axiom, or is refused below, instead of passing the allowlist by name.
  let env ← importModules (#[({ module := `Init } : Import)] ++ targetModules.toArray.map ({ module := · }))
    {} (loadExts := false)
  -- Lean keeps one of two same-named theorems, for the equation lemmas several
  -- modules generate, and lets a theorem stand in for an axiom of the same
  -- type, for the theorems the module system exports as axioms. A theorem
  -- restating an axiom such as `Lean.trustCompiler`, whose type is `True`,
  -- would take it out of the audit.
  let moduleNames := env.header.moduleNames
  let mut replaced : Std.HashMap Name Name := {}
  let mut moduleIdx := 0
  for data in env.header.moduleData do
    for declName in data.constNames, info in data.constants do
      if info matches .axiomInfo _ then
        if let some (.thmInfo _) := env.find? declName then
          replaced := replaced.insert declName moduleNames[moduleIdx]!
    moduleIdx := moduleIdx + 1
  unless replaced.isEmpty do
    moduleIdx := 0
    for data in env.header.moduleData do
      for declName in data.constNames, info in data.constants do
        if let some axiomModule := replaced.get? declName then
          if info matches .thmInfo _ then
            throwError "{moduleNames[moduleIdx]!} declares a theorem {declName} in place of the axiom {axiomModule} declares; rename one of them so the audit can run"
      moduleIdx := moduleIdx + 1
    throwError "a theorem of the build stands in for an axiom; rename it so the audit can run"
  return env

/-- The axioms a declaration of the build depends on, through `cache`, which
every call of one audit shares. -/
def autoformAuditAxiomsOf (env : Environment) (cache : IO.Ref (Std.HashMap Name (Array Name))) (declName : Name) :
    IO (Array Name) := do
  let (found, updated) := Id.run ((autoformAuditAxioms env declName).run (← cache.get))
  cache.set updated
  return found

-- `Lean.Environment.replay` is deprecated from v4.34 in favor of the kernel
-- environment's; it keeps one spelling for every supported toolchain.
set_option linter.deprecated false in
/-- Send every root constant through the kernel again, on top of a fresh import
of the other modules, and throw `failure` if the kernel rejects one. The axiom
walk trusts whatever the oleans hold, and a root `run_cmd` can add a declaration
with kernel checking off. Call this last: replaying a constant that reaches
`Lean.reduceBool` or `Lean.reduceNat` runs compiled project code, and the axiom
check refuses every such constant, since both depend on `Lean.trustCompiler`. -/
def autoformAuditKernelReplay (env : Environment) (targetModules : List Name) (isRoot : Name → Bool)
    (failure : MessageData) : CommandElabM Unit := do
  let baseImports := env.header.moduleNames.filterMap fun moduleName =>
    if targetModules.contains moduleName then none else some ({ module := moduleName } : Import)
  let mut rootConstants : Std.HashMap Name ConstantInfo := {}
  for (declName, info) in env.constants do
    if isRoot declName then
      rootConstants := rootConstants.insert declName info
  let replayed ← (do
      let base ← importModules baseImports {} (loadExts := false)
      discard <| Lean.Environment.replay rootConstants base : IO Unit).toBaseIO
  if let .error error := replayed then
    logError m!"kernel replay of the root package failed: {error}"
    throwError failure

/-- The strict audit of the modules in `moduleJson`. Every root-package
declaration must be safe and depend on no axiom but `propext`,
`Classical.choice` and `Quot.sound`. Each target in `tableJson`, a `lean:` name
of the strict policy's assumption contract, must be a declaration of the build:
the root scan covers those in the root package, and any other gets the same
safety and axiom checks. A target that resolves to a wanted placeholder, or to a
public alias of one, passes as wanted only when its article's state is one a
placeholder may back. Last, the root package must pass the kernel replay. -/
def autoformAuditStrict (moduleJson tableJson : String) : CommandElabM Unit := do
  let targetModules ← match autoformAuditReadModules moduleJson with
    | .ok modules => pure modules
    | .error message => throwError "cannot read the module list: {message}"
  let allowed : List Name := [``propext, ``Classical.choice, ``Quot.sound]
  let targets ← match autoformAuditReadTargets tableJson with
    | .ok targets => pure targets
    | .error message => throwError "cannot read the target table: {message}"
  let env ← autoformAuditImportBuild targetModules
  let axiomCache ← IO.mkRef ({} : Std.HashMap Name (Array Name))
  let axiomsOf := autoformAuditAxiomsOf env axiomCache
  let moduleNames := env.header.moduleNames
  let isRoot := autoformAuditIsRoot env moduleNames targetModules
  let mut errors : Array MessageData := #[]
  let mut checked : Nat := 0
  let mut badSafety : Array Name := #[]
  let mut badAxioms : Array (Name × Name) := #[]
  for (declName, info) in env.constants do
    if isRoot declName && !autoformAuditCompiledCopy declName info then
      checked := checked + 1
      if autoformAuditUnsafeOrPartial env declName info then
        badSafety := badSafety.push declName
      for usedAxiom in (← axiomsOf declName) do
        unless allowed.contains usedAxiom do
          badAxioms := badAxioms.push (declName, usedAxiom)
  let wanted := autoformAuditWantedIndex env isRoot
  let mut wantedLines : Array MessageData := #[]
  for (declName, article, state, mayBeWanted) in targets do
    match autoformAuditResolve env wanted declName with
    | .error 0 =>
      errors := errors.push m!"{declName} [{article}] is not a declaration of the Lean build; fix the article's lean: name or build the module that declares it"
    | .error count =>
      errors := errors.push m!"{declName} [{article}] names a wanted placeholder in each of {count} modules; give them different names"
    | .ok (resolved, info) =>
      -- A wanted placeholder, or a public alias of one, defers its proof or construction.
      if let some head := autoformAuditWantedHead info.type then
        if !mayBeWanted then
          errors := errors.push m!"{declName} [{article}] is a wanted placeholder ({head}), which defers its proof or construction, so it cannot back an article recorded as {state}"
        else unless badSafety.contains resolved || badAxioms.any (·.1 == resolved) do
          wantedLines := wantedLines.push m!"wanted: {declName} [{article}]"
        continue
      -- No article states a compiled copy, and the root scan skips them, so a target that names one,
      -- in or outside the root package, is refused here and checked no further.
      if autoformAuditCompiledCopy declName info then
        errors := errors.push m!"{declName} [{article}] is Lean's compiled copy of a recursive definition; name the definition itself"
        continue
      unless isRoot declName do
        if autoformAuditUnsafeOrPartial env declName info then
          errors := errors.push m!"{declName} [{article}] is outside the root package and is unsafe or partial"
        for usedAxiom in (← axiomsOf declName) do
          if (env.find? usedAxiom).isNone then
            errors := errors.push m!"{declName} [{article}] is outside the root package and depends on {usedAxiom}, which no module of the build declares"
          else unless allowed.contains usedAxiom do
            errors := errors.push m!"{declName} [{article}] is outside the root package and depends on unexpected axiom {usedAxiom}"
  for line in wantedLines do
    logInfo line
  for declName in badSafety do
    logError m!"unsafe or partial declaration: {declName}"
  for (declName, usedAxiom) in badAxioms do
    if (env.find? usedAxiom).isNone then
      logError m!"{declName} depends on {usedAxiom}, which no module of the build declares"
    else
      logError m!"{declName} depends on unexpected axiom {usedAxiom}"
  for error in errors do
    logError error
  if checked == 0 then
    throwError "kernel-trust audit found no root-package declarations"
  unless badSafety.isEmpty && badAxioms.isEmpty && errors.isEmpty do
    logInfo m!"kernel replay of the root package skipped: it runs once every other check passes"
    throwError "root-package declarations failed the kernel-trust audit"
  autoformAuditKernelReplay env targetModules isRoot "root-package declarations failed the kernel-trust audit"
  logInfo m!"kernel trust clean ({checked} root-package declaration(s) audited)"

/-- The open-statement audit of the modules in `moduleJson`. The strict audit's
rules hold with one exception: a theorem that an open article in `articleJson`
names may keep `sorry` in its proof. Whatever reaches such a statement is
reported as conditional on it, and must belong to an article whose Markdown
dependencies reach it. -/
def autoformAuditOpen (moduleJson articleJson : String) : CommandElabM Unit := do
  let targetModules ← match autoformAuditReadModules moduleJson with
    | .ok modules => pure modules
    | .error message => throwError "cannot read the module list: {message}"
  let allowed : List Name := [``propext, ``Classical.choice, ``Quot.sound]
  let articles ← match autoformOpenAuditReadArticles articleJson with
    | .ok articles => pure articles
    | .error message => throwError "cannot read the article table: {message}"
  let env ← autoformAuditImportBuild targetModules
  let axiomCache ← IO.mkRef ({} : Std.HashMap Name (Array Name))
  let axiomsOf := autoformAuditAxiomsOf env axiomCache
  let moduleNames := env.header.moduleNames
  let isRoot := autoformAuditIsRoot env moduleNames targetModules
  let mut errors : Array MessageData := #[]
  let mut openSet : Std.HashSet Name := {}
  for (declName, _, isOpen, _) in articles do
    if isOpen && isRoot declName then
      if let some (.thmInfo _) := env.find? declName then
        openSet := openSet.insert declName
  let mut roots : Array Name := #[]
  for (declName, info) in env.constants do
    if isRoot declName && !autoformAuditCompiledCopy declName info then
      roots := roots.push declName
  roots := roots.qsort Name.lt
  let mut hitCache : Std.HashMap Name (Array Name) := {}
  -- Declarations with an error, and those resting on one, get no info line that
  -- reads as a clean result.
  let mut failed : Std.HashSet Name := {}
  let mut brokenCache : Std.HashMap Name Bool := {}
  for declName in roots do
    let some info := env.find? declName | continue
    let reported := errors.size
    if autoformAuditUnsafeOrPartial env declName info then
      errors := errors.push m!"unsafe or partial declaration: {declName}"
    let usedAxioms ← axiomsOf declName
    for usedAxiom in usedAxioms do
      if (env.find? usedAxiom).isNone then
        errors := errors.push m!"{declName} depends on {usedAxiom}, which no module of the build declares"
      else unless usedAxiom == ``sorryAx || allowed.contains usedAxiom do
        errors := errors.push m!"{declName} depends on unexpected axiom {usedAxiom}"
    let typeConstants := info.type.getUsedConstants
    -- Values are read by kind: `ConstantInfo.value?` hides theorem proofs by
    -- default, and a hidden proof would pass this check vacuously.
    let valueConstants := match info with
      | .thmInfo val => val.value.getUsedConstants
      | .defnInfo val => val.value.getUsedConstants
      | .opaqueInfo val => val.value.getUsedConstants
      | _ => #[]
    if typeConstants.contains ``sorryAx then
      errors := errors.push m!"{declName} has sorry in its statement; state the claim in full and keep sorry only in the proof"
    else if valueConstants.contains ``sorryAx && !openSet.contains declName then
      errors := errors.push m!"{declName} contains sorry but is not an open statement: only a theorem that an open article's lean: names may keep a sorry, written directly in its own proof, not in a helper, where clause or definition; Lean compiles a recursive proof into auxiliaries such as _f and _unary, so a recursive open statement's proof must be exactly sorry"
    let mut external : Array Name := #[]
    for used in typeConstants ++ valueConstants do
      if used == ``sorryAx || isRoot used || external.contains used then
        continue
      external := external.push used
      if (← axiomsOf used).contains ``sorryAx then
        errors := errors.push m!"{declName} uses {used}, which is outside the root package and depends on sorry"
    let (hits, cache) := Id.run ((autoformOpenAuditOpenHits env isRoot openSet
      (autoformAuditBlockOf env declName)).run hitCache)
    hitCache := cache
    if errors.size == reported && usedAxioms.contains ``sorryAx && hits.isEmpty then
      errors := errors.push m!"{declName} depends on sorry outside every declared open statement"
    if errors.size != reported then
      failed := failed.insert declName
  let mut openCount : Nat := 0
  let mut conditionalCount : Nat := 0
  let mut statusLines : Array MessageData := #[]
  let wanted := autoformAuditWantedIndex env isRoot
  for (declName, article, isOpen, allowedOpen, state, mayBeWanted) in articles do
    let reported := errors.size
    match autoformAuditResolve env wanted declName with
    | .error 0 =>
      errors := errors.push m!"{declName} [{article}] is not a declaration of the Lean build; fix the article's lean: name or build the module that declares it"
    | .error count =>
      errors := errors.push m!"{declName} [{article}] names a wanted placeholder in each of {count} modules; give them different names"
    | .ok (resolved, info) =>
      -- As in the strict probe, a wanted placeholder or a public alias of one gets the wanted rule.
      if let some head := autoformAuditWantedHead info.type then
        if !mayBeWanted then
          errors := errors.push m!"{declName} [{article}] is a wanted placeholder ({head}), which defers its proof or construction, so it cannot back an article recorded as {state}"
        else
          let (reachesFailed, cache) := Id.run ((autoformOpenAuditReachesFailed env isRoot openSet failed
            (autoformAuditBlockOf env resolved)).run brokenCache)
          brokenCache := cache
          unless failed.contains resolved || reachesFailed do
            statusLines := statusLines.push m!"wanted: {declName} [{article}]"
        continue
      -- As in the strict probe, a target that names a compiled copy is refused and checked no further.
      if autoformAuditCompiledCopy declName info then
        errors := errors.push m!"{declName} [{article}] is Lean's compiled copy of a recursive definition; name the definition itself"
        continue
      if !isRoot declName then
        if autoformAuditUnsafeOrPartial env declName info then
          errors := errors.push m!"{declName} [{article}] is outside the root package and is unsafe or partial"
        for usedAxiom in (← axiomsOf declName) do
          if (env.find? usedAxiom).isNone then
            errors := errors.push m!"{declName} [{article}] is outside the root package and depends on {usedAxiom}, which no module of the build declares"
          else if usedAxiom == ``sorryAx then
            errors := errors.push m!"{declName} [{article}] is outside the root package and depends on sorry"
          else unless allowed.contains usedAxiom do
            errors := errors.push m!"{declName} [{article}] is outside the root package and depends on unexpected axiom {usedAxiom}"
        if errors.size == reported then
          statusLines := statusLines.push m!"sorry-free: {declName} [{article}]"
      else
        let block := autoformAuditBlockOf env declName
        let (hits, cache) := Id.run ((autoformOpenAuditOpenHits env isRoot openSet block).run hitCache)
        hitCache := cache
        let (reachesFailed, cache) := Id.run
          ((autoformOpenAuditReachesFailed env isRoot openSet failed block).run brokenCache)
        brokenCache := cache
        let broken := failed.contains declName || reachesFailed
        let undeclared := hits.filter (fun hit => !allowedOpen.contains hit)
        unless undeclared.isEmpty do
          errors := errors.push m!"{declName} [{article}] rests on open statement(s) {autoformOpenAuditNameList undeclared}, which its article's Markdown dependencies do not reach; add the dependency to the article or stop using them"
        if openSet.contains declName then
          unless isOpen do
            errors := errors.push m!"{declName} [{article}] is an open statement, but its article records it as proved"
          openCount := openCount + 1
          let ownSorry := match info with
            | .thmInfo val => val.value.getUsedConstants.contains ``sorryAx
            | _ => false
          if errors.size == reported && !broken then
            if ownSorry then
              statusLines := statusLines.push m!"open statement (proof is sorry): {declName} [{article}]"
            else if (← axiomsOf declName).contains ``sorryAx then
              statusLines := statusLines.push m!"open statement (proof depends on sorry elsewhere): {declName} [{article}]"
            else
              statusLines := statusLines.push m!"open statement (proof is sorry-free; restate it if retracted, then record proof: formalized): {declName} [{article}]"
        else if !hits.isEmpty then
          conditionalCount := conditionalCount + 1
          if errors.size == reported && !broken then
            statusLines := statusLines.push m!"conditional: {declName} [{article}] rests on open statement(s) {autoformOpenAuditNameList hits}"
        else unless (← axiomsOf declName).contains ``sorryAx do
          if errors.size == reported && !broken then
            statusLines := statusLines.push m!"sorry-free: {declName} [{article}]"
  if roots.isEmpty || !errors.isEmpty then
    for line in statusLines do
      logInfo line
    for error in errors do
      logError error
    if roots.isEmpty then
      throwError "open-statement audit found no root-package declarations"
    logInfo m!"kernel replay of the root package skipped: it runs once every other check passes"
    throwError "root-package declarations failed the open-statement audit"
  -- The replay names only the first declaration it rejects, so after a failure
  -- no article gets a line that reads as a clean result.
  autoformAuditKernelReplay env targetModules isRoot "root-package declarations failed the open-statement audit"
  for line in statusLines do
    logInfo line
  logInfo m!"kernel trust clean except declared open statements ({roots.size} root-package declaration(s) audited; {openCount} open statement(s), {conditionalCount} conditional declaration(s))"
