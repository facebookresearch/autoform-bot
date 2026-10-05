import Skel.Defs

open scoped Skel

/-- Uses scoped notation from an opened namespace, and a cast, in its statement. -/
theorem Skel.heavy_of_notation [Skel.HasWeight Nat] (h : 0 < ⟪(1 : Nat)⟫)
    (hc : ((1 : Nat) : Int) ≤ 1) : Skel.heavy (1 : Nat) := h
