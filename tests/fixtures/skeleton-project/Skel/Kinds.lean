namespace Skel.Kinds

structure Pair where
  a : Nat
  b : Nat

inductive Color where
  | red
  | green

class HasZ (α : Type) where
  z : α

opaque opaqueSeed : Nat

local notation "𝟙" => (1 : Nat)

theorem usesLocalNotation : 𝟙 = 1 := rfl

namespace Scoped

scoped notation "𝟚" => (2 : Nat)

theorem usesOwnScope : 𝟚 = 2 := rfl

end Scoped

open Scoped in theorem usesSameLineOpen : 𝟚 = 2 := rfl

def plain : Nat := 3

def wf (a b : Nat) : Nat := if h : a = 0 then b else wf (a - 1) (b + 1)
termination_by a
decreasing_by omega

theorem usesWf (h : wf 1 0 = 1) : wf 1 0 = 1 := h

def fact : Nat → Nat
  | 0 => 1
  | n + 1 => (n + 1) * fact n

theorem usesStructural (h : fact 2 = 2) : fact 2 = 2 := h

theorem usesAutoParam (h : 1 < 2 := by decide) : 1 < 2 := h

structure Cfg where
  x : Nat := 1

theorem usesDefault (h : Cfg.x._default = 1) : Cfg.x._default = 1 := h

private def nestedProof : Fin 3 := ⟨1, by decide⟩

theorem usesNestedProof (h : nestedProof.val = 1) : nestedProof.val = 1 := h

end Skel.Kinds
