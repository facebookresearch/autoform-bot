- **Library index: an axiom reached through a mutual block.** `autoform
  library index` judged a declaration's axioms with `collectAxioms`, which
  reads the lists each module stores. A module fills those lists from one cache
  in the order it visits its declarations, so in a mutual block a member that
  finishes while another is still in progress keeps a partial list. A type
  whose constructors reached an axiom only through the other type of its block,
  and a theorem over it, could then be indexed as `complete`: six of sixteen
  such blocks were, on Lean 4.35.0. So could an unsafe definition that reached
  `sorry` only through its mutual partner: one of four such pairs was. The
  probe now walks the declarations itself, as the CI audit has since #190, and
  a name declared in two modules is checked through every copy: a declaration
  using it is left out if either copy rests on an axiom, even where its own
  module imported only the proved copy. Since every copy is read at once,
  copies from different modules can close a cycle no module has, and a
  declaration behind one is left out too (#651).
