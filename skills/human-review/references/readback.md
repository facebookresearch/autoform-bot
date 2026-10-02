# Writing a read-back

A **read-back** is a rendering, in mathematical English, of what a Lean
skeleton *literally asserts*. It is neither a summary nor an explanation, and
it never guesses at what the author meant. A human reviewer will set it beside
the statement the author intended to formalize; every difference between the
two is exactly what they are looking for. This practice follows the blind
read-back audits used by [Prove2me](https://prove2.me).

## You are working blind, and that is the point

You have been given one prepared packet: the comment-stripped skeleton of one
declaration. It holds the elaborated and raw signatures, canonical kernel
material, and the safely attributable source of project definitions the
statement rests on, with all comments and docstrings removed. A definition
whose source cannot be attributed safely is marked `source not shown`; use its
signatures and canonical material, and do not infer the missing source.

You have not been given the article, the source text, or any description of
the intent, and you must not look for them. Do not open the blueprint, the
Lean project, or the source directory. Do not search for the theorem's name on
the web. A reader who knows what the code is supposed to say will read that
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
   English with standard notation, in Markdown with LaTeX math. Write $P_i$,
   not `P i`. Do not mention Lean syntax, tactics, or universe levels.
7. **No judgment.** Do not say whether the formalization is right, faithful,
   or well designed, and do not defend it. Report; the reviewer decides.

## Format

One self-contained paragraph per declaration, understandable without the
packet. Completeness beats elegance: this is fine print. Introduce every
variable and symbol before using it, put the main assertion in display math,
and use a short list when the statement has several clauses.

Use inert Markdown only. Raw HTML (tags, comments, and character references
such as `&amp;`), headings, footnotes, Markdown links, images, autolinks, link
definitions, visibility-changing attributes, and Mermaid blocks are rejected
when the coordinator records the testimony. They add no mathematical content
and would let generated prose hide text, run code, or fetch remote resources
in the published review site. HTML is refused even inside formulas, so put a
space after `<` when a letter follows it: `$a < b > c$`, not `$a<b>c$`. Code
shows `<` as typed. Label a fenced code block `lean`, `lean4`, or `text`, or
leave it bare.

The testimony must also read the same on GitHub, which shows the vault's
cards with a different Markdown reader; where the two readings differ it is
refused, with the line and a way to fix it. Put a blank line before every
list, table, and code block, and a line of text between a bulleted and a
numbered list. Indent a nested list four spaces, and number every list from 1.
Give every table row as many cells as the header and every cell of the
delimiter row a `-`; write `\|` for a pipe in a table cell, and keep code
that holds a pipe out of tables. Close a code block with the fence that opens
it, at the same indent. Do not end a line with a backslash, start a list item
with `[x]` or `[ ]`, write `~` in text, or open a block quote with `[!NOTE]`
or the like, and write a backslash before punctuation only where Markdown
would read it otherwise, as in `\*`: GitHub hides the backslash before any
punctuation, and the site only before some.

Write a formula in a line of text between dollar signs, as `$P_i$`, and a
displayed formula in a code block whose opening fence is ```` ```math ````
alone. GitHub reads `$...$` as a formula only where the first `$` starts a
line or follows a space or `(`, neither `$` has a space just inside it, no
letter, digit, or `_` follows the last, the formula stays on one line, and it
is not in emphasis; it reads Markdown escapes such as `\{`, `\_`, and `\\`
in it first, and a pair of `*` as emphasis. Where any of that matters, or a
`<`, `>`, or `&` follows the formula right away, write it as `` $`...`$ ``,
whose TeX GitHub takes as written, with no letter, digit, `_`, or `\` just
before it: `` $`\{x\}`$ ``, not `$\{x\}$`; `` $`n`$th ``, not `$n$th`;
`` $`2*3*4`$ ``, not `$2*3*4$`. Never use `\(...\)` or `\[...\]`: write
`$x$`, not `\(x\)`. Keep dollar signs out of formulas and `$$` out of lines of
text. In text write `\$` for a dollar sign, and put dollar signs meant as
typed in code.

Everything you write must be visible as written. Do not use invisible
characters such as zero-width spaces, or stack more than two accents above or
below one letter. In formulas use standard notation only: letters, symbols,
relations, operators, arrows, delimiters, `\frac`, `\sqrt`, accents, the fonts
`\mathbb`, `\mathcal`, `\mathfrak`, `\mathscr`, `\mathrm`, `\mathbf`, and
`\mathsf`, `\operatorname` and `\text` with visible content, and the
`equation`, `cases`, `aligned`, `gathered`, `array`, and matrix environments.
Any other command is refused by name, including `\phantom`, `\rlap`, `\kern`,
`\color`, `\tag`, `\large`, and macro definitions. Space with `\,`, `\;`,
`\quad`, or `~`: one formula may space 8 em in all, as much as 8 `\quad`, and
a read-back 64 em. Never write two `\!` in a row. In a formula write `\%` for
a percent sign, never `%`. A read-back that breaks this is rejected, however faithful
it is. Keep it to a few kilobytes: a read-back over 32 KiB, or with more than
64 `[`, is refused unread; in a formula `\lbrack` and `\rbrack` do not count.

## Return only testimony

Return only the Markdown testimony described above. Do not create a vault
card, copy metadata, infer a destination path, or inspect a manifest. The
coordinator records your response with the exact packet through Autoform's
`review record` command. That command rejects a response if the prepared
bundle, current Lean declaration, or packet bytes no longer agree.

A read-back testifies about one exact packet. If either the packet's meaning
or its bytes change, a coordinator must request a new blind read-back. Never
revise old testimony after seeing an article or intended statement.
