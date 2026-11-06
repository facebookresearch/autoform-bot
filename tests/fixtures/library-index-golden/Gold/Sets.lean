module
/-! # Sets

Finite sets of naturals, à la carte. -/
public def Gold.evens (n : Nat) : List Nat :=
  (List.range n).filter (· % 2 = 0)

public instance : Inhabited (List Nat) := ⟨[]⟩
