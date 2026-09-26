namespace Vendor

class Choice where
  proposition : Prop

def selectedChoice : Choice := ⟨True⟩

instance : Choice := selectedChoice

end Vendor
