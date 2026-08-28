import Mathlib

namespace CabannesThesis

variable {Y : Type}

/-- A label is eligible for a weak observation when the observation admits it. -/
def Eligible (S : Y → Prop) (y : Y) : Prop := S y

/-- A weak observation is non-ambiguous when it admits at most one label. -/
def NonAmbiguous (S : Y → Prop) : Prop :=
  ∀ y z : Y, Eligible S y → Eligible S z → y = z

/-- A non-ambiguous observation determines the label it admits. -/
theorem nonAmbiguous_determinism {S : Y → Prop} (h : NonAmbiguous S) {y z : Y}
    (hy : Eligible S y) (hz : Eligible S z) : y = z :=
  h y z hy hz

/-- Full supervision is the weak observation that admits exactly one label. -/
def supervision (y : Y) : Y → Prop := fun z => z = y

/-- Full supervision is non-ambiguous, so weak supervision recovers it. -/
theorem supervision_nonAmbiguous (y : Y) : NonAmbiguous (supervision y) := by
  intro a b ha hb
  exact ha.trans hb.symm

end CabannesThesis
