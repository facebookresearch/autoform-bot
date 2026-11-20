- **CI audit: a `sorry` copy behind a proved one.** When two modules state one
  theorem the same way, Lean keeps one copy, the later one it loads, and the
  audit read only that copy. So a declaration whose module imported a copy
  proved with `sorry` passed both audits whenever a proved copy loaded later:
  the strict audit printed `kernel trust clean`, and the open audit listed the
  declaration as `sorry-free`. The audit now reads every copy of a name more
  than one module declares, and a declaration resting on any copy's `sorry` or
  axiom fails. Since every copy is read at once, copies from different modules
  can close a cycle no module has; a declaration behind one fails with an error
  naming each name on the cycle that several modules declare, and those modules
  (#831).
