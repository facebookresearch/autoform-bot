namespace Vendor

class Choice where
  proposition : Prop

def selectedChoice : Choice := ⟨True⟩

instance : Choice := selectedChoice

def visible._helper : Nat := 1

def matchBody : Nat → Nat
  | 0 => 10
  | n + 1 => n

private def privateHelper : Nat := 1

def usesPrivate : Nat := privateHelper

end Vendor
