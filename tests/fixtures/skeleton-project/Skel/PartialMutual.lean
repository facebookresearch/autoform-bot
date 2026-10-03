namespace Skel.Partial.Inner

mutual
def ev : Nat → Bool
  | 0 => true
  | n + 1 => od n
def od : Nat → Bool
  | 0 => false
  | n + 1 => ev n
end
partial def afterMutual (n : Nat) : Nat := if n = 0 then 1 else afterMutual (n - 1)

end Skel.Partial.Inner
