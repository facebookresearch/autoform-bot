module

namespace Skel.PublicPartial
public section
public partial def spin (n : Nat) : Nat := if n = 0 then 1 else spin (n - 1)
def viaSpin (n : Nat) : Nat := spin n
theorem usesPublic (h : viaSpin 0 = 1) : viaSpin 0 = 1 := h
end
end Skel.PublicPartial
