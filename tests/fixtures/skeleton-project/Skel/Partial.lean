import Skel.PartialMutual

namespace Skel.Partial

partial def outer (n : Nat) : Nat := go n
where go (k : Nat) : Nat := if k = 0 then 1 else go (k - 1)

def viaWhere (n : Nat) : Nat := outer.go n

theorem usesWhere (h : viaWhere 0 = 1) : viaWhere 0 = 1 := h

partial
def ownLine (n : Nat) : Nat := if n = 0 then 1 else ownLine (n - 1)

theorem usesOwnLine (h : ownLine 0 = 1) : ownLine 0 = 1 := h

theorem usesAfterMutual (h : Inner.afterMutual 0 = 1) : Inner.afterMutual 0 = 1 := h

theorem usesMutual (h : Inner.ev 2 = true) : Inner.ev 2 = true := h

set_option hygiene false in
macro "mkspin" : command =>
  `(partial def fromMacro (n : Nat) : Nat := if n = 0 then 1 else fromMacro (n - 1))
mkspin

theorem usesMacro (h : fromMacro 0 = 1) : fromMacro 0 = 1 := h

end Skel.Partial
