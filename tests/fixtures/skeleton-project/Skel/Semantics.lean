import Skel.Vendor
import Skel.VendorMacroUse
import Skel.VendorModule
import Skel.VendorPrivA
import Skel.VendorPrivAxiom
import Skel.VendorWf

namespace Skel.Semantics

syntax "semanticMacro" : term

macro_rules
  | `(semanticMacro) => `(1)

def expandedMacro : Nat := semanticMacro

def opaqueSeed : Nat := 1

opaque opaqueWitness : Nat := opaqueSeed

theorem usesOpaque : opaqueWitness = opaqueWitness := rfl

opaque ordinaryWithNamedCompanion : Nat

unsafe def ordinaryWithNamedCompanion._unsafe_rec : Nat := 1

def selectedProposition : Prop := Vendor.Choice.proposition

def usesExternalDetail : Nat := Vendor.visible._helper

def usesExternalMatch : Nat → Nat := Vendor.matchBody

def usesExternalPrivate : Nat := Vendor.usesPrivate

def usesVendorMacro : Nat := Vendor.macroAlias

def usesVendorWf : Nat := Vendor.wfWalk 2 3

def usesVendorPrivateChain : Nat := Vendor.viaPrivateBridge

def usesVendorModule : Nat := Vendor.moduleValue

theorem usesVendorPrivateAxiom : True := Vendor.usesHiddenAxiom

structure FieldPair where
  first : Nat
  second : Nat

def usesFieldOrder : Nat := (FieldPair.mk 1 2).first

def «quoted.helper» : Nat := 1

theorem usesQuoted : «quoted.helper» = 1 := rfl

def notationMarker : Nat := 1

def interpolationSmoke : String := s!"value {"a--b"}"

def matchBody : Nat → Nat
  | 0 => 10
  | n + 1 => n

def visible._helper : Nat := 1

def visible : Nat := visible._helper

def firstOnLine : Nat := 1 theorem secondOnLine : firstOnLine = 1 := rfl

def safeValue : Nat := 1

unsafe def unsafeValue : Nat := 1

partial def partialValue (n : Nat) : Nat := partialValue n

private partial def privatePartialValue (n : Nat) : Nat :=
  if n == 0 then 1 else privatePartialValue (n - 1)

def usesPrivatePartial : Nat := privatePartialValue 0

private unsafe def privateUnsafeValue : Nat := 1

unsafe def usesPrivateUnsafe : Nat := privateUnsafeValue

universe u

def universeNamed (α : Type u) : Type u := α

end Skel.Semantics
