{imports}
-- Autoform statement probe. This file is a Python-format template: `{{`/`}}` are
-- literal braces and single-brace fields are filled by
-- autoform_cli.statement_probe. It is written to a temporary file and run with
-- `lake env lean` inside the built project; it never modifies the project.
import Lean.Util.CollectAxioms
import Lean.Elab.Command
import Lean.Data.Json

open Lean Elab Command Meta

namespace AutoformUnused

/-! Hypotheses a proof never uses. A proof is a term the kernel has checked. A
hypothesis the term never mentions can be deleted from the statement, and the
same term proves what remains. This reads the proof; it searches for nothing. -/

/-- Each propositional hypothesis of a proved theorem that could be deleted, with
whether its proof uses it. A hypothesis that the conclusion or a later binder
mentions cannot be deleted and is left out. For any other, a proof term that
never mentions it, abstracted over the remaining binders, proves the statement
without it. `none` when the proof rests on `sorry`, whose term uses nothing. -/
def hypotheses (info : TheoremVal) : MetaM (Option (Array Json)) := do
  if (← collectAxioms info.name).contains ``sorryAx then return none
  forallTelescope info.type fun xs conclusion => do
    -- The proof applied to the binders: its own lambdas are reduced, and a proof
    -- that is a bare constant stays an application that mentions every binder.
    let proof := info.value.beta xs
    let mut out := #[]
    for i in [0:xs.size] do
      let x := xs[i]!
      let id := x.fvarId!
      unless ← isProp (← inferType x) do continue
      let mentionedLater ← xs[i+1:].toArray.anyM fun y => return (← inferType y).containsFVar id
      if conclusion.containsFVar id || mentionedLater then continue
      let used := proof.containsFVar id
      let decl ← id.getDecl
      -- A hypothesis written as `p → …` or `[C]` has a hygienic name no reader wrote.
      let name := if decl.userName.hasMacroScopes then "_" else decl.userName.toString
      out := out.push <| Json.mkObj [("name", Json.str name),
        ("type", Json.str (toString (← ppExpr decl.type))), ("used", Json.bool used)]
    return some out

end AutoformUnused

namespace AutoformStatementProbe

/-- Write one record to the file the CLI names, where nothing else Lean prints
can split it. Standard output is only for running the probe by hand. -/
def emit (fields : List (String × Json)) : CommandElabM Unit := do
  let line := s!"{marker}{{(Json.mkObj fields).compress}}\n"
  match ← IO.getEnv "{output_env}" with
  | some path =>
    let out ← IO.FS.Handle.mk path .append
    out.putStr line
    out.flush
  | none => IO.print line

def kindOf : ConstantInfo → String
  | .thmInfo _ => "theorem"
  | .axiomInfo _ => "axiom"
  | .defnInfo _ => "def"
  | .opaqueInfo _ => "opaque"
  | .inductInfo _ => "inductive"
  | _ => "other"

def probe (request : String) (root : Name) : CommandElabM Unit := do
  match (← getEnv).find? root with
  | none => emit [("root", Json.str request), ("found", Json.bool false)]
  | some info =>
    let (proof, hypotheses) ← match info with
      | .thmInfo v => do
        match ← liftTermElabM (AutoformUnused.hypotheses v) with
        | some found => pure ("proved", found)
        | none => pure ("sorry", #[])
      | _ => pure ("none", #[])
    emit [("root", Json.str request), ("found", Json.bool true), ("kind", Json.str (kindOf info)),
      ("proof", Json.str proof), ("hypotheses", Json.arr hypotheses)]

end AutoformStatementProbe

-- The command's own heartbeat limit would count every declaration together, so
-- a large project would hit it; the CLI's timeout bounds the run instead.
set_option maxHeartbeats 0 in
run_cmd do
  for (request, root) in [{roots}] do
    AutoformStatementProbe.probe request root
