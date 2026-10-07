{imports}
-- Autoform impact probe. This file is a Python-format template: `{{`/`}}` are
-- literal braces and single-brace fields are filled by autoform_cli.impact.
-- It is written to a temporary file and run with `lake env lean` inside the
-- built project; it never modifies the project.
import Lean.Linter.Deprecated
import Lean.Elab.Command
import Lean.Data.Json

open Lean Elab Command Meta

namespace AutoformImpact

def probeOutputLimit : Nat := {output_limit}

/-- Write one complete record without letting the scratch file grow past the
CLI's output limit. The Python reader checks the limit again after exit. -/
def emitRecord (out : IO.FS.Handle) (written : IO.Ref Nat) (marker : String)
    (record : Json) : IO Unit := do
  let line := s!"{{marker}}{{record.compress}}\n"
  let total := (← written.get) + line.utf8ByteSize
  if total > probeOutputLimit then
    throw <| IO.userError s!"lake env lean exceeded the {{probeOutputLimit}}-byte output limit"
  written.set total
  out.putStr line

def kindOf : ConstantInfo → String
  | .defnInfo _   => "def"
  | .thmInfo _    => "theorem"
  | .axiomInfo _  => "axiom"
  | .opaqueInfo _ => "opaque"
  | .inductInfo _ => "inductive"
  | .ctorInfo _   => "constructor"
  | .recInfo _    => "recursor"
  | .quotInfo _   => "quot"

/-- The longest proper prefix of the user-facing name that is itself a
constant. A private name's prefixes are tried under its own private prefix
first, so a `where` helper of a private declaration finds that declaration. -/
def parentOf (env : Environment) (c : Name) : Option Name := Id.run do
  let privatePrefix := privatePrefix? c
  let mut p := (privateToUserName c).getPrefix
  while !p.isAnonymous do
    if let some pre := privatePrefix then
      if env.contains (pre ++ p) then return some (pre ++ p)
    if env.contains p then return some p
    p := p.getPrefix
  return none

def nameJson (n : Name) : Json := Json.str (toString n)

def namesJson (names : Array Name) : Json :=
  Json.arr ((names.qsort Name.lt).map nameJson)

/-- What a revision of any project-local constant can reach through `c`: the
local constants its type and its value mention, and its deprecation state.
Theorem values are read too (`allowOpaque`), since proofs break when the
statements they use change; an inductive's constructors stand in for a value,
and so does the `_unsafe_rec` companion of a `partial def`, whose kernel value
is only an `Inhabited` witness while the companion holds the body Lean runs.
`internal` is judged on the user-facing name: a private declaration someone
wrote is not an internal detail, while the companions Lean generates for it
(`_proof_1`, `match_1`, `_simp_1`) still are. `user_name` is that name for a
private constant, which is how articles and `--declaration` name it.
`alias_of` is the local constant a theorem's value is exactly when the
theorem's type is also exactly that constant's, as Batteries' `alias` writes:
its statement then changes with the target's although its type never mentions
it. A theorem whose type differs from its target's, even up to definitional
unfolding, keeps its own statement. -/
def record (env : Environment) (isLocal : Name → Bool) (c : Name) (info : ConstantInfo)
    (module : Name) : Json :=
  let value := info.value? (allowOpaque := true)
  let typeConstants := info.type.getUsedConstants
  -- `Compiler.mkUnsafeRecName c`, spelled out to keep the probe's imports small.
  let implementation := Name.str c "_unsafe_rec"
  let valueConstants := match info with
    | .inductInfo v => v.ctors.toArray
    | .opaqueInfo _ =>
      let uses := (value.map (·.getUsedConstants)).getD #[]
      if env.contains implementation then uses.push implementation else uses
    | _             => (value.map (·.getUsedConstants)).getD #[]
  let valueMissing := match info with
    | .thmInfo _ | .defnInfo _ | .opaqueInfo _ => value.isNone
    | _                                        => false
  let aliasOf := match info, value.map (·.consumeMData) with
    | .thmInfo _, some (.const target levels) =>
      let sameType := (env.find? target).any
        (fun (targetInfo : ConstantInfo) => info.type == targetInfo.instantiateTypeLevelParams levels)
      if isLocal target && sameType then some target else none
    | _, _ => none
  let usesDeprecated := (typeConstants ++ valueConstants).foldl
    (fun acc d => if Linter.isDeprecated env d && !acc.contains d then acc.push d else acc) #[]
  Json.mkObj [
    ("name", nameJson c),
    ("kind", Json.str (kindOf info)),
    ("instance", Json.bool (isInstanceCore env c)),
    ("internal", Json.bool (privateToUserName c).isInternalDetail),
    ("user_name", if isPrivateName c then nameJson (privateToUserName c) else Json.null),
    ("module", nameJson module),
    ("parent", ((parentOf env c).map nameJson).getD Json.null),
    ("type_uses", namesJson (typeConstants.filter isLocal)),
    ("value_uses", namesJson (valueConstants.filter isLocal)),
    ("alias_of", (aliasOf.map nameJson).getD Json.null),
    ("deprecated", Json.bool (Linter.isDeprecated env c)),
    ("replacement", ((Linter.getDeprecatedNewName env c).map nameJson).getD Json.null),
    ("uses_deprecated", namesJson usesDeprecated),
    ("value_missing", Json.bool valueMissing)]

end AutoformImpact

set_option maxHeartbeats 0 in
run_cmd do
  let projectModules : Std.HashSet Name := Std.HashSet.ofList [{project_modules}]
  let env ← getEnv
  -- `env.header` is slow to reach from the interpreted probe, so module names
  -- are read once.
  let moduleNames := env.header.moduleNames
  let moduleOf (n : Name) : Option Name :=
    (env.getModuleIdxFor? n).map fun idx => moduleNames[idx.toNat]!
  let isLocalModule (m : Name) : Bool := projectModules.contains m
  let isLocal (n : Name) : Bool := (moduleOf n).any isLocalModule
  -- Records go to the file the CLI names: a command's stdout is buffered into
  -- one message, and any other write could split a record.
  let some path ← IO.getEnv "{output_env}" | throwError "{output_env} is not set"
  let out ← IO.FS.Handle.mk path .write
  let written ← IO.mkRef 0
  try
    -- Module ownership and direct imports come from the loaded environment,
    -- not source syntax.  Emit even modules with no declarations so the
    -- evidence is a closed graph over every loaded project module.
    if {emit_module_evidence} then
      let sourceSearchPath ← getSrcSearchPath
      for module in moduleNames do
        if isLocalModule module then
          let some moduleIdx := env.getModuleIdx? module | continue
          let some sourcePath ← sourceSearchPath.findWithExt "lean" module |
            throwError m!"project module {{module}} has no source path"
          let sourcePath ← IO.FS.realPath sourcePath
          let oleanPath ← IO.FS.realPath (← findOLean module)
          let directImports := env.header.moduleData[moduleIdx.toNat]!.imports.foldl
            (fun imports imported =>
              if isLocalModule imported.module && !imports.contains imported.module then
                imports.push imported.module
              else imports)
            #[]
          AutoformImpact.emitRecord out written "{module_marker}" <| Json.mkObj [
            ("module", AutoformImpact.nameJson module),
            ("source_path", Json.str sourcePath.toString),
            ("olean_path", Json.str oleanPath.toString),
            ("direct_local_imports", AutoformImpact.namesJson directImports)]
    for (c, info) in env.constants do
      let some module := moduleOf c | continue
      if isLocalModule module then
        AutoformImpact.emitRecord out written "{marker}"
          (AutoformImpact.record env isLocal c info module)
  finally
    out.flush
