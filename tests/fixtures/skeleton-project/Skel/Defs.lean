namespace Skel

variable {Y : Type}

/-- A weak observation admits a label. -/
def Eligible (S : Y → Prop) (y : Y) : Prop := S y

/-- At most one label is admitted. -/
def NonAmbiguous (S : Y → Prop) : Prop :=
  ∀ y z : Y, Eligible S y → Eligible S z → y = z

/-- A helper only used inside a proof; must NOT appear in a skeleton. -/
theorem eligible_of (S : Y → Prop) (y : Y) (h : S y) : Eligible S y := h

/-- A structure, to check inductive handling. -/
structure Observation (Y : Type) where
  admits : Y → Prop
  nonempty : ∃ y, admits y

/-- A class, to check that a local class projection folds onto the class. -/
class HasWeight (Y : Type) where
  weight : Y → Nat

/-- Uses the class projection in its body. -/
def heavy [HasWeight Y] (y : Y) : Prop := 0 < HasWeight.weight y

/-- Scoped notation: only parses where `Skel` is open. -/
scoped notation "⟪" y "⟫" => HasWeight.weight y

end Skel
