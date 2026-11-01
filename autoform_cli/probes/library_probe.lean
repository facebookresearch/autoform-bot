{imports}
-- Autoform library index probe. This file is a Python-format template: `{{`/`}}` are
-- literal braces and single-brace fields are filled by autoform_cli.library.generate
-- through lean_probe.render_probe_template, which adds `output_env` and
-- `output_limit`: the variable naming the records file, and its byte cap.
-- It is written to a temporary file and run with `lake env lean` inside the built
-- library; it never modifies the library. It is deliberately not a module file: only
-- a plain importer sees module and non-module files together, private placeholders,
-- docstrings, declaration ranges and module headers.
import Lean

open Lean Meta Elab

namespace AutoformLibrary

def probeOutputLimit : Nat := {output_limit}

def libraryModules : NameSet :=
  (({modules} : String).splitOn "\n").foldl (fun set module => set.insert module.toName) {{}}

def emitRecord (out : IO.FS.Handle) (written : IO.Ref Nat) (record : Json) : IO Unit := do
  let line := s!"{marker}{{record.compress}}\n"
  let total := (← written.get) + line.utf8ByteSize
  if total > probeOutputLimit then
    throw <| IO.userError s!"lake env lean exceeded the {{probeOutputLimit}}-byte output limit"
  written.set total
  out.putStr line

/-- The head constant of a type's conclusion, under its binders. -/
partial def conclusionHead : Expr → Option Name
  | .forallE _ _ b _ => conclusionHead b
  | .mdata _ e       => conclusionHead e
  | e                => e.getAppFn.constName?

/-- The conclusion's first argument with the binders put back: the type a
`ProofWanted T` placeholder stands for. -/
partial def unwrap : Expr → Expr
  | .forallE n t b bi => .forallE n t (unwrap b) bi
  | .mdata _ e        => unwrap e
  | e                 => e.getAppArgs.getD 0 e

/-- Print a type in a context this probe builds itself, so the library's own
`leanOptions`, open namespaces and current namespace do not change the bytes. -/
def printed (e : Expr) : MetaM String := do
  let options := ({{}} : Options)
    |>.setBool `pp.fullNames true
    |>.setBool `pp.universes false
    |>.setBool `pp.unicode.fun true
    |>.setBool `pp.proofs false
    |>.setBool `pp.mvars false
  let format ← withOptions (fun _ => options) (ppExpr e)
  return format.pretty (width := 1000000)

def positionJson (p : Position) : Json := Json.arr #[p.line, p.column]

def names (values : Array Name) : Json :=
  Json.arr ((values.map toString).qsort (· < ·) |>.map Json.str)

/-- What the module at `index` recorded for `name` in a per-declaration extension. Lean's own
lookups read the module it first loaded the name from; this reads the module asked for. -/
def recorded [Inhabited α] (ext : MapDeclarationExtension α) (env : Environment)
    (index : ModuleIdx) (name : Name) : Option α :=
  let find (level : OLeanLevel) :=
    (ext.getModuleEntries (level := level) env index).binSearch (name, default)
      (fun a b => Name.quickLt a.1 b.1) |>.map (·.2)
  find .exported <|> find .server

/-- The docstring the module at `index` gives `name`, as `findDocString?` renders one. A
docstring taken over with `@[inherit_doc]` is not read. -/
def docstringIn (env : Environment) (index : ModuleIdx) (name : Name) : IO (Option String) := do
  if let some text := recorded docStringExt env index name then
    return some (← rewriteManualLinks text)
  if let some verso := recorded versoDocStringExt env index name then
    return some (← Doc.runMarkdown env (Doc.MarkdownM.run' (Doc.ToMarkdown.toMarkdown verso)) {{}} .anonymous [])
  return none

/-- The axioms the copy `info` of a name rests on. `collectAxioms` answers for the copy of the
module Lean first loaded the name from; this reads the proof of the copy given. -/
def axiomsOf (info : ConstantInfo) : MetaM (Array Name) := do
  let mut found : NameSet := if info matches .axiomInfo _ then ({{}} : NameSet).insert info.name else {{}}
  for constant in info.getUsedConstantsAsSet do
    for used in ← collectAxioms constant do
      found := found.insert used
  return found.toArray

/-- Run `action`, which reads one fact about `name` in `module`, and say which constant it was
about when it fails, so the error names the declaration and no placeholder is indexed. -/
def about {{α : Type}} (what : String) (name module : Name) (action : MetaM α) : MetaM α :=
  try action
  catch e => throwError "cannot {{what}} {{name}} in {{module}}: {{e.toMessageData}}"

def run : MetaM Unit := do
  let some path ← IO.getEnv "{output_env}" | throwError "{output_env} is not set"
  let out ← IO.FS.Handle.mk path .write
  let written ← IO.mkRef 0
  let env ← getEnv
  let modules := env.header.moduleNames
  for module in modules do
    if libraryModules.contains module then
      let docs := (getModuleDoc? env module).getD #[]
      emitRecord out written (Json.mkObj [
        ("record", "module"),
        ("name", toString module),
        ("module_doc", if docs.isEmpty then Json.null
          else Json.str ("\n\n".intercalate (docs.toList.map (·.doc.trimAscii.toString))))])
  -- Lean imports two modules that state one theorem the same way and keeps one copy. Report
  -- every public name that more than one library module holds, with those modules in name
  -- order; the first of them is the one the name is indexed under. Equation lemmas realized
  -- in two modules are among them.
  let mut holders : Std.HashMap Name (Array Name) := {{}}
  for module in modules, data in env.header.moduleData do
    if libraryModules.contains module then
      for name in data.constNames do
        if !isPrivateName name then
          holders := holders.insert name ((holders.getD name #[]).push module)
  holders := holders.filterMap fun _ held =>
    if held.size > 1 then some (held.qsort (fun a b => a.toString < b.toString)) else none
  for (name, held) in holders.toArray.qsort (fun a b => a.1.toString < b.1.toString) do
    emitRecord out written (Json.mkObj [
      ("record", "shared"),
      ("name", toString name),
      ("modules", Json.arr (held.map fun module => Json.str (toString module)))])
  let found := env.constants.fold (init := #[]) fun acc name info =>
    match env.getModuleIdxFor? name with
    | some index =>
      let module := modules[index.toNat]!
      if libraryModules.contains module then acc.push (name, info, module) else acc
    | none => acc
  -- Fold order follows the hash map; sort so the records, and a failure, are reproducible.
  for (name, info, kept) in found.qsort (fun a b => a.1.toString < b.1.toString) do
    let user := privateToUserName name
    -- Which copy of a shared name Lean keeps follows from the order it loads the modules in,
    -- so the record is of the first holder in name order instead: its module, its ranges, its
    -- docstring, type and the axioms of its proof. Lean compares the types of two copies up to
    -- the names and annotations of their binders, so they may be written differently.
    let own : Option (Name × ModuleIdx × ConstantInfo) := do
      let module ← (← holders.get? name)[0]?
      let index ← env.getModuleIdx? module
      let data ← env.header.moduleData[index.toNat]?
      let copy ← data.constants[(← data.constNames.idxOf? name)]?
      return (module, index, copy)
    if own.isNone && holders.contains name then
      throwError "no copy of the shared name {{name}} in the first module that holds it"
    let module := match own with | some (module, _, _) => module | none => kept
    let ranges? ← match own with
      | some (_, index, _) => pure (recorded declRangeExt env index name)
      | none => findDeclarationRanges? name
    let some ranges := ranges? | continue
    let copy := match own with | some (_, _, copy) => copy | none => info
    let wrapper := (conclusionHead copy.type).bind fun head =>
      if head == `ProofWanted || head == `DefWanted || head == `DerivedWanted then some head else none
    let generated := info matches .ctorInfo _ | .recInfo _ | .quotInfo _
      || env.isProjectionFn name || isAuxRecursor env name || isNoConfusion env name
      || Meta.isMatcherCore env name || user.isInternalDetail
    if generated || (isPrivateName name && wrapper.isNone) then continue
    let type := if wrapper.isSome then unwrap copy.type else copy.type
    let docstring ← about "read the docstring of" user module do
      match own with
      | some (_, index, _) => docstringIn env index name
      | none => findDocString? env name
    let axioms ← about "collect the axioms of" user module do
      if own.isSome then axiomsOf copy else collectAxioms name
    let signature ← about "print the type of" user module (printed type)
    emitRecord out written (Json.mkObj [
      ("record", "constant"),
      ("name", toString user),
      ("module", toString module),
      ("private", isPrivateName name),
      ("wrapper", match wrapper with | some head => Json.str (toString head) | none => Json.null),
      ("range", Json.arr #[positionJson ranges.range.pos, positionJson ranges.range.endPos]),
      ("selection", Json.arr #[positionJson ranges.selectionRange.pos, positionJson ranges.selectionRange.endPos]),
      ("signature", Json.str signature),
      -- Lean keeps the white space before a docstring's closing `-/`.
      ("docstring", match docstring with | some text => Json.str text.trimAscii.toString | none => Json.null),
      -- A wanted result that refers to another one names its private placeholder.
      ("mentions", names (type.getUsedConstants.map privateToUserName)),
      ("axioms", names axioms)])
  out.flush

end AutoformLibrary

set_option maxHeartbeats 0 in
#eval AutoformLibrary.run
