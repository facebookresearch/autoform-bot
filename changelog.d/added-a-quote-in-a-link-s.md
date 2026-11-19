- **A quote in a link's destination.** `check` and render warn about a link
  whose destination holds a quote the site reads otherwise than the graph does.
  The site takes a quote for the start of a title, so it may cut the address at
  the quote and show the text after the link, later links included, as that
  title, or read on past the `)` and take that text into the address; a `(`
  after a quote takes the rest of the text into the address, or cuts the
  address's last character. Each warning names the link, what the site shows,
  the links it hides and the fix: the address in `<…>` or its quote
  percent-encoded, or, for a `(` in a title, the title ended with its quote
  right before the `)`. The graph reads links as before, and no exit status
  changes. Render puts a local link it rewrites in `<…>` when the address holds
  a quote, as it does for a space or a parenthesis, so `%27` holds there too
  (#828).
