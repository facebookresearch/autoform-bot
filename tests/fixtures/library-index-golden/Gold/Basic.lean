/-- Zero is a right identity for `+` on ℕ. -/
theorem Gold.add_zero' (n : Nat) : n + 0 = n := rfl

/-- Zero is a left identity for `+` on ℕ. -/
theorem Gold.zero_add' (n : Nat) : 0 + n = n := Nat.zero_add n
