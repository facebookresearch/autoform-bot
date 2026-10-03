namespace Skel.Unparsed

namespace Sc
scoped notation "⟪two⟫" => (2 : Nat)
end Sc

def Other.zero : Nat := 0

open Other
  Sc

/-- Uses notation from a namespace opened on a continuation line. -/
def two : Nat := ⟪two⟫

theorem usesContinuedOpen : two = 2 := rfl

local infixr:80 " ⊞ " => Nat.add

theorem localInProof : 1 + 1 = 2 := by
  show 1 ⊞ 1 = 2
  rfl

/-- Local notation in a definition's body. -/
def localDef (a : Nat) : Nat := a ⊞ 1

theorem usesLocalDef : localDef 1 = 2 := rfl

-- `-- _b +` is a comment here: this file does not import `Skel.CommentToken`.
def blindToken (a _b : Nat) : Nat := a +-- _b +
  0

theorem usesBlindToken : blindToken 2 3 = 2 := rfl

end Skel.Unparsed
