__AUTOFORM_IMPORTS__
import Lean.Util.CollectAxioms
import Lean.Elab.Command
import Lean.Meta.Instances
import Lean.OriginalConstKind
import Lean.Structure
import Lean.Class

open Lean Elab Command

private def decodeName (context raw : String) : CommandElabM Name :=
  match Syntax.decodeNameLit ("`" ++ raw) with
  | some name => pure name
  | none => throwError m!"{context}: invalid Lean name: {raw}"

private def declaringModule? (env : Environment) (declName : Name) : Option Name := do
  let moduleIdx ← env.getModuleIdxFor? declName
  env.header.moduleNames[moduleIdx.toNat]?

private def matchesDeclarationKind
    (env : Environment) (declName : Name) (expected : String) : Bool :=
  match expected with
  | "theorem" => getOriginalConstKind? env declName == some .thm
  | "axiom" => getOriginalConstKind? env declName == some .axiom
  | "opaque" => getOriginalConstKind? env declName == some .opaque
  | "abbrev" =>
      match env.find? declName with
      | some (.defnInfo info) => info.hints == .abbrev
      | _ => false
  | "def" =>
      match env.find? declName with
      | some (.defnInfo info) => info.hints != .abbrev && !Meta.isInstanceCore env declName
      | _ => false
  | "instance" => Meta.isInstanceCore env declName
  | "class" => isClass env declName
  | "structure" => isStructure env declName && !isClass env declName
  | "inductive" =>
      getOriginalConstKind? env declName == some .induct && !isStructure env declName
  | _ => false

run_cmd do
  let rootModuleStrings : List String := [__AUTOFORM_ROOT_MODULES__]
  let rootModules ← rootModuleStrings.mapM (decodeName "root module")
  let expectedArtifacts : List (String × String) := [__AUTOFORM_EXPECTED_ARTIFACTS__]
  let localTargets : List (String × String × String) := [__AUTOFORM_LOCAL_TARGETS__]
  let allowed : List Name := [``propext, ``Classical.choice, ``Quot.sound]
  let env ← getEnv
  let mut badArtifacts := false
  for (moduleText, expectedPath) in expectedArtifacts do
    let moduleName ← decodeName "root artifact module" moduleText
    let actual ← IO.FS.realPath (← findOLean moduleName)
    let expected ← IO.FS.realPath (System.FilePath.mk expectedPath)
    unless actual == expected do
      badArtifacts := true
      logError m!"root module {moduleName} resolved to {actual}, expected {expected}"
  let mut badTargets := false
  for (article, declText, expectedKind) in localTargets do
    let declName ← decodeName article declText
    if env.find? declName |>.isNone then
      badTargets := true
      logError m!"{article}: local declaration does not exist: {declName}"
    else
      match declaringModule? env declName with
      | none =>
          badTargets := true
          logError m!"{article}: local declaration has no declaring module: {declName}"
      | some moduleName =>
          unless rootModules.contains moduleName do
            badTargets := true
            logError m!"{article}: local declaration {declName} belongs to non-root module {moduleName}"
      unless expectedKind.isEmpty do
        unless matchesDeclarationKind env declName expectedKind do
          badTargets := true
          logError m!"{article}: declaration {declName} does not have expected kind {expectedKind}"
  let mut checked : Nat := 0
  let mut badSafety : Array Name := #[]
  let mut badAxioms : Array (Name × Name) := #[]
  for (declName, info) in env.constants do
    if let some moduleIdx := env.getModuleIdxFor? declName then
      if let some moduleName := env.header.moduleNames[moduleIdx.toNat]? then
        if rootModules.contains moduleName then
          checked := checked + 1
          if info.isUnsafe || info.isPartial then
            badSafety := badSafety.push declName
          for usedAxiom in (← Lean.collectAxioms declName) do
            unless allowed.contains usedAxiom do
              badAxioms := badAxioms.push (declName, usedAxiom)
  for declName in badSafety do
    logError m!"unsafe or partial declaration: {declName}"
  for (declName, usedAxiom) in badAxioms do
    logError m!"{declName} depends on unexpected axiom {usedAxiom}"
  if checked == 0 then
    throwError "kernel-trust audit found no root-package declarations"
  unless !badArtifacts && !badTargets && badSafety.isEmpty && badAxioms.isEmpty do
    throwError "blueprint or root-package declarations failed the artifact audit"
  logInfo m!"artifact audit clean ({checked} root-package declaration(s) audited)"
