- **Lean fences without a span per token.** A `lean` or `lean4` fence with
  nothing else after its marker, at a page's top level or in a statement's box,
  is published in the marking of the Lean under statements: Lean 4 keywords,
  comments and strings take Material's code colors in Material's code box, and
  nothing else gets a span. Pygments' Lean 3 lexer spanned every token, spaces
  included, and read Lean 4 keywords as names. `#check`, `#eval`, `#print`,
  `#reduce` and `#synth` count as keywords, in fences and under statements. A
  fence in a list or a quote, with options or an attribute list, beside a
  paragraph's line, or never closed is highlighted as before (#772).
