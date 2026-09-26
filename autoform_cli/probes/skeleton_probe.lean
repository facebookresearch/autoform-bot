{imports}
-- Autoform skeleton probe. This file is a Python-format template: `{{`/`}}` are
-- literal braces and single-brace fields are filled by autoform_cli.skeleton.
-- It is written to a temporary file and run with `lake env lean` inside the
-- built project; it never modifies the project.
import Lean.Util.CollectAxioms
import Lean.Util.Path
import Lean.Elab.Command
import Lean.Data.Json

open Lean Elab Command Meta Term

-- A reader must see what is quantified over: `∃ n : ℕ, …`, not `∃ n, …`.
set_option pp.funBinderTypes true
-- and where a cast lands: `(↑n : ℚ)`, not `↑n`, since `1 / ↑n` means something
-- else in `ℕ`.
set_option pp.coercions.types true

namespace AutoformSkeleton

/-- The probe-to-Python contract for elaborated declaration material. Bump this
when the canonical expression encoding below changes. -/
def semanticSchema := "autoform-lean-expr/v1"

/-- Preserve the structure of a Lean name. `Name.toString` is deliberately not
used: quoted components may themselves contain dots. -/
partial def nameJson : Name → Json
  | .anonymous => Json.null
  | .str p s   => Json.mkObj [("str", Json.arr #[nameJson p, Json.str s])]
  | .num p n   => Json.mkObj [("num", Json.arr #[nameJson p, n])]

partial def levelJson : Level → Json
  | .zero     => Json.mkObj [("zero", Json.null)]
  | .succ u   => Json.mkObj [("succ", levelJson u)]
  | .max u v  => Json.mkObj [("max", Json.arr #[levelJson u, levelJson v])]
  | .imax u v => Json.mkObj [("imax", Json.arr #[levelJson u, levelJson v])]
  | .param n  => Json.mkObj [("param", nameJson n)]
  | .mvar id  => Json.mkObj [("mvar", nameJson id.name)]

def binderInfoJson : BinderInfo → Json
  | .default        => "default"
  | .implicit       => "implicit"
  | .strictImplicit => "strictImplicit"
  | .instImplicit   => "instImplicit"

def literalJson : Literal → Json
  | .natVal n => Json.mkObj [("nat", n)]
  | .strVal s => Json.mkObj [("string", s)]

/-- Canonical kernel expression material. Binder display names and metadata do
not affect meaning, so they are omitted. Applications and implicit arguments
remain explicit, which exposes macro expansions and synthesized instances. -/
partial def exprJson : Expr → Json
  | .bvar i          => Json.mkObj [("bvar", i)]
  | .fvar id         => Json.mkObj [("fvar", nameJson id.name)]
  | .mvar id         => Json.mkObj [("mvar", nameJson id.name)]
  | .sort u          => Json.mkObj [("sort", levelJson u)]
  | .const n us      => Json.mkObj [
      ("const", nameJson n), ("levels", Json.arr (us.toArray.map levelJson))]
  | .app f a         => Json.mkObj [("app", Json.arr #[exprJson f, exprJson a])]
  | .lam _ t b bi    => Json.mkObj [("lam", Json.arr #[binderInfoJson bi, exprJson t, exprJson b])]
  | .forallE _ t b bi => Json.mkObj [
      ("forall", Json.arr #[binderInfoJson bi, exprJson t, exprJson b])]
  | .letE _ t v b nd => Json.mkObj [
      ("let", Json.arr #[Json.bool nd, exprJson t, exprJson v, exprJson b])]
  | .lit l           => Json.mkObj [("literal", literalJson l)]
  | .mdata _ e       => exprJson e
  | .proj n i e      => Json.mkObj [
      ("projection", Json.arr #[nameJson n, i, exprJson e])]

/-- Elaboration result whose exact bytes bind a review to kernel-visible
meaning. The theorem proof is excluded; definition and opaque bodies are not. -/
def semanticMaterial (env : Environment) (c : Name) : String :=
  let payload :=
    match env.find? c with
    | some (.defnInfo v) => Json.mkObj [("type", exprJson v.type), ("value", exprJson v.value)]
    | some (.opaqueInfo v) => Json.mkObj [("type", exprJson v.type), ("value", exprJson v.value)]
    | some (.inductInfo v) => Json.mkObj [
        ("type", exprJson v.type),
        ("constructors", Json.arr <| v.ctors.toArray.map fun ctor =>
          Json.mkObj [
            ("name", nameJson ctor),
            ("type", match env.find? ctor with
              | some info => exprJson info.type
              | none => Json.null)])]
    | some info => Json.mkObj [("type", exprJson info.type)]
    | none => Json.null
  payload.compress

/-- Reuse canonical expression serialization across roots in one probe. The
environment is immutable for the generated `run_cmd`, so `Name` is a complete
cache key. -/
def cachedSemanticMaterial
    (cache : IO.Ref (Std.HashMap Name String))
    (env : Environment) (c : Name) : CommandElabM String := do
  if let some material := (← cache.get)[c]? then
    return material
  let material := semanticMaterial env c
  cache.modify (·.insert c material)
  return material

/-- Constants that fix the *meaning* of `c`: its type always, and its value only
when `c` is a definition. A theorem's proof is never part of its meaning. -/
def meaningConstants (env : Environment) (c : Name) : Array Name :=
  match env.find? c with
  | some (.defnInfo v)   => v.type.getUsedConstants ++ v.value.getUsedConstants
  | some (.opaqueInfo v) => v.type.getUsedConstants ++ v.value.getUsedConstants
  | some (.inductInfo v) => v.type.getUsedConstants ++ v.ctors.toArray
  | some info            => info.type.getUsedConstants
  | none                 => #[]

/-- Fold generated companions -- constructors, projections, recursors, matchers,
equation lemmas -- onto the declaration a reader sees in the source. -/
partial def canonical (env : Environment) (c : Name) : Name :=
  match env.find? c with
  | some (.ctorInfo v) => v.induct
  | some (.recInfo _)  => canonical env c.getPrefix
  | _ =>
    if let some info := env.getProjectionFnInfo? c then canonical env info.ctorName
    else if isAuxRecursor env c || isNoConfusion env c || Meta.isMatcherCore env c || c.isInternalDetail then
      if c.getPrefix != Name.anonymous && env.contains c.getPrefix then canonical env c.getPrefix else c
    else c

/-- Direct meaning-dependencies of a folded declaration. Generated companions
are traversed but folded back onto the source declaration. Results are shared
across roots because both the environment and project roots are fixed for one
generated probe. -/
def expandedMeaning
    (cache : IO.Ref (Std.HashMap Name (Array Name)))
    (env : Environment) (projectRoots : List Name) (c : Name) : CommandElabM (Array Name) := do
  if let some dependencies := (← cache.get)[c]? then
    return dependencies
  let isLocal (n : Name) : Bool :=
    match env.getModuleIdxFor? n with
    | some idx =>
      let mod := env.header.moduleNames[idx.toNat]!
      projectRoots.any (fun projectRoot => projectRoot.isPrefixOf mod)
    | none   => false
  let isClassProjection (e : Name) : Bool :=
    match env.getProjectionFnInfo? e with
    | some info => info.fromClass
    | none      => false
  let dependencies := Id.run do
    let mut out : Array Name := #[]
    let mut work : Array Name := #[c]
    let mut seen : Array Name := #[]
    while h : work.size > 0 do
      let d := work[work.size - 1]
      work := work.pop
      if seen.contains d then continue
      seen := seen.push d
      for e in meaningConstants env d do
        let f := canonical env e
        if f == c then
          if !seen.contains e then work := work.push e
        else if !isLocal f && isClassProjection e then
          continue
        else if !out.contains f then
          out := out.push f
    return out
  cache.modify (·.insert c dependencies)
  return dependencies

def kindOf (env : Environment) (c : Name) : String :=
  match env.find? c with
  | some (.defnInfo _)   => if isInstanceCore env c then "instance" else "def"
  | some (.thmInfo _)    => "theorem"
  | some (.axiomInfo _)  => "axiom"
  | some (.opaqueInfo _) => "opaque"
  | some (.inductInfo _) => if isClass env c then "class" else if isStructure env c then "structure" else "inductive"
  | some (.ctorInfo _)   => "constructor"
  | some (.recInfo _)    => "recursor"
  | some (.quotInfo _)   => "quot"
  | none                 => "unknown"

def moduleOf (env : Environment) (c : Name) : Option Name :=
  (env.getModuleIdxFor? c).map fun idx => env.header.moduleNames[idx.toNat]!

def signatureOf (c : Name) : CommandElabM String := do
  let sig ← liftTermElabM (PrettyPrinter.ppSignature c)
  return sig.fmt.pretty 100

/-- First node of syntax kind `k` inside `stx`, depth-first. -/
partial def findKind? (stx : Syntax) (k : SyntaxNodeKind) : Option Syntax :=
  if stx.getKind == k then some stx else stx.getArgs.findSome? (findKind? · k)

/-- The namespaces the file opens above `line`, from its `open …` commands. Their
scoped notation (`#s`, `n !`, `∑ x ∈ s, f x`) must be active for the statement to
parse; Lean records what a declaration means, not how its file was set up. -/
def openedNamespaces (lines : List String) (line : Nat) : List Name :=
  (lines.take (line - 1)).flatMap fun l =>
    let l := l.trimAsciiStart.toString
    if l.startsWith "open " then
      ((l.drop 5).toString.splitOn " ")
        |>.filter (fun t => t ≠ "" && t ≠ "scoped" && t ≠ "in")
        |>.takeWhile (fun t => t ≠ "hiding" && t ≠ "renaming" && !t.startsWith "(")
        |>.map String.toName
    else []

/-- Capture a declaration from the same source snapshot the probe inspects.
The surrounding source-tree guard rejects concurrent edits. -/
def declarationSource (c : Name) : CommandElabM (Option String) := do
  let env ← getEnv
  let some r ← findDeclarationRanges? c | return none
  let some idx := env.getModuleIdxFor? c | return none
  let mod := env.header.moduleNames[idx.toNat]!
  let sp ← getSrcSearchPath
  let some path ← sp.findWithExt "lean" mod | return none
  let text ← IO.FS.readFile path
  let lines := text.splitOn "\n"
  return some <| "\n".intercalate
    (lines.drop (r.range.pos.line - 1) |>.take (r.range.endPos.line - r.range.pos.line + 1))

/-- The declaration's source up to its value: the statement as written, without
the proof. Parsed with Lean's own parser rather than cut by pattern matching. -/
def statementSource (root : Name) : CommandElabM (Option String) := do
  let env ← getEnv
  let some r ← findDeclarationRanges? root | return none
  let some idx := env.getModuleIdxFor? root | return none
  let mod := env.header.moduleNames[idx.toNat]!
  let sp ← getSrcSearchPath
  let some path ← sp.findWithExt "lean" mod | return none
  let text ← IO.FS.readFile path
  let lines := text.splitOn "\n"
  let snippet := "\n".intercalate (lines.drop (r.range.pos.line - 1) |>.take (r.range.endPos.line - r.range.pos.line + 1))
  -- `activateScoped` mutates the environment. Isolate those parser-only changes
  -- so one requested declaration cannot change how the next one is parsed.
  withEnv env do
    for ns in openedNamespaces lines r.range.pos.line do
      if env.isNamespace ns then activateScoped ns
    let parserEnv ← getEnv
    match Parser.runParserCategory parserEnv `command snippet with
    | .error _ => return none
    | .ok stx =>
      let decl := (findKind? stx ``Parser.Command.declaration).getD stx
      let val := (findKind? decl ``Parser.Command.declValSimple).orElse fun _ =>
        (findKind? decl ``Parser.Command.declValEqns).orElse fun _ =>
          findKind? decl ``Parser.Command.whereStructInst
      let some v := val | return none
      let some pos := v.getPos? | return none
      -- `pos` is a byte position: cut by bytes, not by characters, or every `∀`
      -- before the value pushes the cut past it.
      let bytes := snippet.toUTF8.extract 0 pos.byteIdx
      return some ((String.fromUTF8! bytes).trimAsciiEnd.toString)

def rangeJson (c : Name) : CommandElabM Json := do
  match ← findDeclarationRanges? c with
  | some r => return Json.arr #[r.range.pos.line, r.range.endPos.line]
  | none   => return Json.null

def emit (request : String) (fields : List (String × Json)) : CommandElabM Unit :=
  IO.println s!"{marker}{{(Json.mkObj (("root", Json.str request) :: fields)).compress}}"

def skeleton
    (projectRoots : List Name)
    (expandCache : IO.Ref (Std.HashMap Name (Array Name)))
    (semanticCache : IO.Ref (Std.HashMap Name String))
    (request : String) (root : Name) : CommandElabM Unit := do
  let env ← getEnv
  unless env.contains root do
    emit request [("found", Json.bool false)]
    return
  let isLocal (n : Name) : Bool :=
    match moduleOf env n with
    | some m => projectRoots.any (fun projectRoot => projectRoot.isPrefixOf m)
    | none   => false
  let isCore (n : Name) : Bool :=
    match moduleOf env n with
    | some m => [{core_roots}].contains m.getRoot
    | none   => true
  let expand (c : Name) : CommandElabM (Array Name) :=
    expandedMeaning expandCache env projectRoots c
  let mut trusted : Array Name := #[]
  let mut edges : Array (Name × Array Name) := #[]
  let mut assumed : Array Name := #[]
  let mut work : Array Name := #[root]
  while h : work.size > 0 do
    let c := work[work.size - 1]
    work := work.pop
    let mut localDeps : Array Name := #[]
    for d in ← expand c do
      if isLocal d then
        if !localDeps.contains d then localDeps := localDeps.push d
        if !trusted.contains d && d != root then
          trusted := trusted.push d
          work := work.push d
      else if !isCore d && !d.isInternalDetail && !assumed.contains d then
        assumed := assumed.push d
    edges := edges.push (c, localDeps.qsort Name.lt)
  let axioms ← collectAxioms root
  -- Axioms are part of the trust boundary even when reached only through a
  -- theorem proof. Their types can name project definitions or external
  -- notions whose meaning must be bound just like statement dependencies.
  for axiomName in axioms do
    for d in ← expand axiomName do
      if isLocal d then
        if !trusted.contains d && d != root then
          trusted := trusted.push d
          work := work.push d
      else if !isCore d && !d.isInternalDetail && !assumed.contains d then
        assumed := assumed.push d
  while h : work.size > 0 do
    let c := work[work.size - 1]
    work := work.pop
    let mut localDeps : Array Name := #[]
    for d in ← expand c do
      if isLocal d then
        if !localDeps.contains d then localDeps := localDeps.push d
        if !trusted.contains d && d != root then
          trusted := trusted.push d
          work := work.push d
      else if !isCore d && !d.isInternalDetail && !assumed.contains d then
        assumed := assumed.push d
    edges := edges.push (c, localDeps.qsort Name.lt)
  let sortedAssumed := assumed.qsort Name.lt
  let sortedAxioms := axioms.qsort Name.lt
  let mut boundaryClosure := sortedAssumed
  for axiomName in sortedAxioms do
    if !isCore axiomName && !axiomName.isInternalDetail && !boundaryClosure.contains axiomName then
      boundaryClosure := boundaryClosure.push axiomName
  let mut boundaryWork := boundaryClosure
  while h : boundaryWork.size > 0 do
    let c := boundaryWork[boundaryWork.size - 1]
    boundaryWork := boundaryWork.pop
    for d in ← expand c do
      if !isLocal d && !isCore d && !d.isInternalDetail && !boundaryClosure.contains d then
        boundaryClosure := boundaryClosure.push d
        boundaryWork := boundaryWork.push d
  let sortedBoundaryClosure := boundaryClosure.qsort Name.lt
  let mut boundaryModules : Array Name := #[]
  for c in sortedBoundaryClosure do
    if let some mod := moduleOf env c then
      if !boundaryModules.contains mod then boundaryModules := boundaryModules.push mod
  let mut boundaryModuleFiles : Array Json := #[]
  for mod in boundaryModules.qsort Name.lt do
    let path ← findOLean mod
    boundaryModuleFiles := boundaryModuleFiles.push <| Json.arr #[
      Json.str (toString mod), Json.str "olean", Json.str path.toString]
  let mut items : Array Json := #[]
  for c in trusted.qsort Name.lt do
    let deps := (edges.find? (·.1 == c)).map (·.2) |>.getD #[]
    let kind := kindOf env c
    let source ← if kind == "theorem" || kind == "axiom" then
      pure Json.null
    else
      match ← declarationSource c with | some s => pure (Json.str s) | none => pure Json.null
    items := items.push <| Json.mkObj [
      ("name", Json.str (toString c)),
      ("kind", Json.str kind),
      ("module", Json.str (toString ((moduleOf env c).getD Name.anonymous))),
      ("range", ← rangeJson c),
      ("signature", Json.str (← signatureOf c)),
      ("semantic_schema", Json.str semanticSchema),
      ("semantic", Json.str (← cachedSemanticMaterial semanticCache env c)),
      ("depends", Json.arr (deps.map fun d => Json.str (toString d))),
      ("source", source)]
  let rootDeps := (edges.find? (·.1 == root)).map (·.2) |>.getD #[]
  let statement := match ← statementSource root with | some s => Json.str s | none => Json.null
  let rootKind := kindOf env root
  let source ← if rootKind == "theorem" || rootKind == "axiom" then
    pure Json.null
  else
    match ← declarationSource root with | some s => pure (Json.str s) | none => pure Json.null
  let mut assumedSemantics : Array Json := #[]
  for d in sortedAssumed do
    assumedSemantics := assumedSemantics.push <| Json.arr #[
      Json.str (toString d), Json.str (← cachedSemanticMaterial semanticCache env d)]
  let mut axiomSemantics : Array Json := #[]
  for d in sortedAxioms do
    axiomSemantics := axiomSemantics.push <| Json.arr #[
      Json.str (toString d), Json.str (← cachedSemanticMaterial semanticCache env d)]
  emit request <| [
    ("found", Json.bool true),
    ("statement_source", statement),
    ("source", source),
    ("kind", Json.str rootKind),
    ("lean_version", Json.str Lean.versionString),
    ("module", Json.str (toString ((moduleOf env root).getD Name.anonymous))),
    ("range", ← rangeJson root),
    ("signature", Json.str (← signatureOf root)),
    ("semantic_schema", Json.str semanticSchema),
    ("semantic", Json.str (← cachedSemanticMaterial semanticCache env root)),
    ("depends", Json.arr (rootDeps.map fun d => Json.str (toString d))),
    ("trusted", Json.arr items),
    ("assumed", Json.arr (sortedAssumed.map fun d => Json.str (toString d))),
    ("assumed_semantics", Json.arr assumedSemantics),
    ("boundary_modules", Json.arr boundaryModuleFiles),
    ("axioms", Json.arr (sortedAxioms.map fun d => Json.str (toString d))),
    ("axiom_semantics", Json.arr axiomSemantics)]

end AutoformSkeleton

set_option maxHeartbeats 0 in
run_cmd do
  let projectRoots : List Name := [{project_roots}]
  let expandCache : IO.Ref (Std.HashMap Name (Array Name)) ← IO.mkRef {{}}
  let semanticCache : IO.Ref (Std.HashMap Name String) ← IO.mkRef {{}}
  for (request, root) in [{roots}] do
    AutoformSkeleton.skeleton projectRoots expandCache semanticCache request root
