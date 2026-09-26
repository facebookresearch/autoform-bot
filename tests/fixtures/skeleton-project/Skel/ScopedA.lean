import Skel.Semantics

namespace Skel.ScopedA

scoped notation "⟬marker⟭" => Skel.Semantics.notationMarker

open scoped Skel.ScopedA

theorem activatesScope : ⟬marker⟭ = Skel.Semantics.notationMarker := rfl

end Skel.ScopedA
