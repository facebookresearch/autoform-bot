import Skel.Vendor

namespace Skel.Semantics

syntax "semanticMacro" : term

macro_rules
  | `(semanticMacro) => `(1)

def expandedMacro : Nat := semanticMacro

def opaqueSeed : Nat := 1

opaque opaqueWitness : Nat := opaqueSeed

theorem usesOpaque : opaqueWitness = opaqueWitness := rfl

def selectedProposition : Prop := Vendor.Choice.proposition

def «quoted.helper» : Nat := 1

theorem usesQuoted : «quoted.helper» = 1 := rfl

def notationMarker : Nat := 1

def interpolationSmoke : String := s!"value {"a--b"}"

end Skel.Semantics
