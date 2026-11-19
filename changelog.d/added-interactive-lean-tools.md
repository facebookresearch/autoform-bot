- **Interactive Lean tools.** The LSP adapter gains `lean_goal` (the tactic
  goal at a position), `lean_check_buffer` (diagnostics for caller-supplied
  text, never written to disk), `lean_try_tactics` (up to eight candidates,
  each in place of one line) and `lean_try_close` (`aesop`, `omega`,
  `positivity`, `linarith`). They edit a document the project's Lean server
  keeps open, so a candidate costs an incremental re-elaboration rather than a
  whole-file check; none of them is verification. `AUTOFORM_LSP_TOOLSET=baseline`
  hides them, and `AUTOFORM_LSP_MAX_OPEN_DOCS` (default 1) bounds the documents
  each server keeps open ([Lean servers](servers/README.md#lsp-tools), #770).
