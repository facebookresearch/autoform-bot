import Skel.Defs

namespace Skel

variable {Y : Type}

def supervision (y : Y) : Y → Prop := fun z => z = y

theorem supervision_nonAmbiguous (y : Y) : NonAmbiguous (supervision y) := by
  intro a b ha hb
  have := eligible_of (supervision y) a ha
  exact ha.trans hb.symm

/-- Uses a structure in its statement, and sorry in its proof. -/
theorem observation_determined (o : Observation Y) (h : NonAmbiguous o.admits) :
    ∃ y, o.admits y ∧ ∀ z, o.admits z → z = y := by
  sorry

theorem heavy_of_weight [HasWeight Y] (y : Y) (h : 0 < HasWeight.weight y) : heavy y := h

end Skel
