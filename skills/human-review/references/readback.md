# Writing a read-back

A **read-back** is a rendering, in mathematical English, of what a Lean
skeleton *literally asserts*. It is neither a summary nor an explanation, and
it never guesses at what the author meant. A human reviewer compares it with
the statement the author intended, looking for every difference.

## You are working blind, and that is the point

You have been given one prepared packet: the comment-stripped skeleton of one
declaration. It holds the elaborated and raw signatures, canonical kernel
material, and the safely attributable source of project definitions the
statement rests on, with all comments and docstrings removed. A definition
whose source cannot be attributed safely is marked `source not shown`; use its
signatures and canonical material, and do not infer the missing source.

You have not been given the article, the source text, or any description of
the intent, and you must not look for them: do not open the blueprint, the
Lean project, or the source directory, or search the web for the theorem's
name. A reader who knows what the code is supposed to say will read that
meaning into it, and the discrepancies the reviewer needs to see disappear.
If the packet is ambiguous, say what is ambiguous rather than resolving it.

## Principles

1. **Translate the code, not an intent.** State what the Lean says. If it says
   less than a textbook theorem would, your read-back says less.
2. **Account for every binder and hypothesis.** Every quantified variable,
   every explicit and implicit argument, every typeclass assumption appears in
   the read-back. Dropping a hypothesis is the worst failure.
3. **Unfold the project's own definitions.** The packet's definitions are
   there to be unfolded: write what `IsSup E b` means in words, do not name it
   as if it were standard. Library notions such as the real numbers may be
   named without unfolding; when a library notion has a convention a reader
   might not expect, such as division by zero being zero, say so.
4. **Surface degenerate and edge cases.** Say what the quantifiers silently
   include: zero, empty sets, one-element types, junk values of total
   functions, and hypotheses that could never hold. A vacuous theorem is the
   classic faithfulness trap; if the hypotheses can be contradictory, say so.
5. **Preserve logical precision.** Keep the exact strength of every
   connective: `≤` against `<`, existence against unique existence, an
   implication against an equivalence, the direction of every inequality.
6. **Write for a mathematician who does not read Lean.** Plain mathematical
   English with standard notation, in plain text with LaTeX math. Write $P_i$,
   not `P i`. Do not mention Lean syntax, tactics, or universe levels.
7. **No judgment.** Do not say whether the formalization is right, faithful,
   or well designed, and do not defend it. Report; the reviewer decides.

## Format

One self-contained paragraph per declaration, understandable without the
packet. Completeness beats elegance: this is fine print. Introduce every
variable and symbol before using it, put the main assertion in a displayed
formula, and put each clause on a line of its own when the statement has
several.

Write plain text. GitHub shows its text exactly as typed, line breaks
included, so Markdown such as `**bold**`, `- item`, or a link is shown, not
read. Write a formula in a line of text between dollar signs, as `$P_i$`, and a
displayed formula between double dollar signs, as `$$\sum_{i < n} a_i \le b$$`;
GitHub shows their TeX. Dollar signs are only for formulas: write
`costs \$5`, not `costs $5`. `\(x\)` and `\[x\]` are shown as typed, so write
`$x$` and `$$x$$`.

Everything you write must be visible as written. Do not use invisible or
reordering characters, such as zero-width spaces, bidirectional overrides, or
right-to-left letters; unassigned code points; combining marks with nothing to
sit on; or more than two accents above or below one letter. Include at least
one letter or digit. A read-back that breaks these rules is rejected, however
faithful it is.
In formulas use standard notation only: letters, symbols, relations,
operators, arrows, delimiters, `\left` and `\right`, `\frac`, `\binom`,
`\sqrt`, accents, the fonts `\mathbb`, `\mathcal`, `\mathfrak`, `\mathscr`,
`\mathrm`, `\mathbf`, and `\mathsf`, `\operatorname` and `\text`, and the
`cases`, `aligned`, `gathered`, and matrix environments. A formula with any
other command is shown as typed instead of typeset, including `\phantom`,
`\rlap`, `\kern`, `\color`, `\tag`, `\large`, `\not`, and macro definitions,
and so is one with spacing such as `\,`, `\;`, `\quad`, or `~`: MathJax spaces
a formula itself, so write `$\int f(x) dx$`, not `$\int f(x)\,dx$`. Write
`\neq` or `\notin` for a negated relation. Never put a bracket right after
`\begin{aligned}` or `\begin{gathered}`, or a `[` or `*` right after `\\`; in
`\text`, write braces and one space at a time; never write something with
nothing in it, such as `$$ $$`, `\frac{}{}`, `x^{}`, or `\left.\right.`; and
never leave two cells, or two rows, empty one after the other. In a formula
write `\%` for a percent sign, never `%`. Keep it to a few kilobytes: a
read-back over 32 KiB or 500 lines is refused.

## Return only testimony

Return the read-back and nothing else. Do not create a vault card, copy
metadata, infer a destination path, or inspect a manifest; the coordinator
records your response for the exact packet. Never revise old testimony after
seeing an article or intended statement.
