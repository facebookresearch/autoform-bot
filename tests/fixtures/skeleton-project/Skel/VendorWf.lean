import Skel.VendorWfHelper

namespace Vendor

def wfWalk (a b : Nat) : Nat :=
  if h : a = 0 then wfHelper b else wfWalk (a - 1) (b + 1)
termination_by a
decreasing_by omega

end Vendor
