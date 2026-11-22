/-
Statement facts for `autoform work`'s fidelity check, run as

    lake env lean --run statement_facts.lean ROOTS MODULE... -- DECLARATION...

`ROOTS` is a comma-separated list of the project's root module names: a constant
belongs to the project when its module is one of them or below one. The probe
imports the modules, private parts included, and prints one JSON object with,
for each declaration:

- `found`, `constant`, `module`, `kind`, `wanted` (a `theorem_wanted` placeholder,
  `def d xs : ProofWanted T := ⟨⟩`), `levelParams`;
- `statement`: `∀ xs, T` (a placeholder's `ProofWanted` removed) with binder names
  and metadata erased, printed raw: two statements are the same term exactly
  when these strings are equal;
- `axioms`: what `#print axioms` lists;
- `closure`: every project constant the statement reaches through types,
  definition values and constructor types (never a theorem's proof), with its
  kind, normalized type and value.
-/
import Lean
import Lean.Util.CollectAxioms

open Lean

def kindOf : ConstantInfo → String
  | .thmInfo _ => "theorem"
  | .defnInfo _ => "def"
  | .axiomInfo _ => "axiom"
  | .opaqueInfo _ => "opaque"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor"
  | .quotInfo _ => "quot"

/-- Erase binder names and metadata, which do not change what a term says. -/
partial def norm : Expr → Expr
  | .forallE _ t b bi => .forallE `_ (norm t) (norm b) bi
  | .lam _ t b bi => .lam `_ (norm t) (norm b) bi
  | .letE _ t v b nd => .letE `_ (norm t) (norm v) (norm b) nd
  | .mdata _ e => norm e
  | .app f a => .app (norm f) (norm a)
  | .proj s i e => .proj s i (norm e)
  | e => e

/-- `∀ xs, ProofWanted T` ↦ `∀ xs, T`. -/
partial def stripWanted : Expr → Option Expr
  | .forallE n t b bi => (stripWanted b).map (.forallE n t · bi)
  | .mdata _ e => stripWanted e
  | e => if e.isAppOfArity `ProofWanted 1 then some e.appArg! else none

def render (e : Expr) : String := toString (norm e)

def main (args : List String) : IO UInt32 := do
  let some rootsArg := args.head?
    | IO.eprintln "usage: lean --run statement_facts.lean ROOTS MODULE... -- DECLARATION..."; return 2
  let roots := (rootsArg.splitOn ",").filter (· ≠ "") |>.map String.toName
  let (mods, rest) := args.tail.span (· ≠ "--")
  let decls := (rest.drop 1).map String.toName
  if roots.isEmpty || mods.isEmpty || decls.isEmpty then
    IO.eprintln "usage: lean --run statement_facts.lean ROOTS MODULE... -- DECLARATION..."
    return 2
  initSearchPath (← findSysroot)
  let env ← importModules (mods.toArray.map fun m => { module := m.toName, importAll := true }) {}
    (trustLevel := 1024)
  let moduleNames := env.header.moduleNames
  let moduleOf (n : Name) : Option Name := (env.getModuleIdxFor? n).map (moduleNames[·]!)
  let inProject (n : Name) : Bool := (moduleOf n).any fun m => roots.any (·.isPrefixOf m)
  let wantedModules := mods.map String.toName
  -- A `theorem_wanted` placeholder is private: look among the named modules' constants too.
  let findDecl (d : Name) : Option (Name × ConstantInfo) := Id.run do
    if let some info := env.find? d then return some (d, info)
    for i in [0:moduleNames.size] do
      unless wantedModules.contains moduleNames[i]! do continue
      for c in env.header.moduleData[i]!.constNames do
        if privateToUserName? c == some d then
          if let some info := env.find? c then return some (c, info)
    return none
  let ctx : Core.Context := { fileName := "<statement_facts>", fileMap := default }
  let mut out : Array (String × Json) := #[]
  for d in decls do
    let some (c, info) := findDecl d
      | out := out.push (d.toString, Json.mkObj [("found", toJson false)]); continue
    let wanted := stripWanted info.type
    let stmt := wanted.getD info.type
    let (axioms, _) ← (collectAxioms c : CoreM (Array Name)).toIO ctx { env }
    let mut seen : NameSet := {}
    let mut todo : Array Name := stmt.getUsedConstants
    let mut closure : Array Json := #[]
    while h : todo.size > 0 do
      let n := todo.back
      todo := todo.pop
      if seen.contains n then continue
      seen := seen.insert n
      unless inProject n do continue
      let some ci := env.find? n | continue
      let value? : Option Expr := match ci with
        | .defnInfo v => some v.value
        | .opaqueInfo v => some v.value
        | _ => none
      let ctorTypes : List Expr := match ci with
        | .inductInfo v => v.ctors.filterMap fun k => (env.find? k).map (·.type)
        | _ => []
      closure := closure.push (Json.mkObj [
        ("name", toJson n.toString),
        ("module", toJson ((moduleOf n).map (·.toString))),
        ("kind", toJson (kindOf ci)),
        ("levelParams", toJson (ci.levelParams.map (·.toString))),
        ("type", toJson (render ci.type)),
        ("valueHash", toJson (value?.map fun v => toString (norm v).hash)),
        ("ctorTypes", toJson (ctorTypes.map render))])
      todo := todo ++ ci.type.getUsedConstants
      if let some v := value? then todo := todo ++ v.getUsedConstants
      for t in ctorTypes do todo := todo ++ t.getUsedConstants
    out := out.push (d.toString, Json.mkObj [
      ("found", toJson true),
      ("constant", toJson c.toString),
      ("module", toJson ((moduleOf c).map (·.toString))),
      ("kind", toJson (kindOf info)),
      ("wanted", toJson wanted.isSome),
      ("levelParams", toJson (info.levelParams.map (·.toString))),
      ("statement", toJson (render stmt)),
      ("axioms", toJson (axioms.map (·.toString))),
      ("closure", Json.arr closure)])
  IO.println (Json.mkObj out.toList).compress
  return 0
