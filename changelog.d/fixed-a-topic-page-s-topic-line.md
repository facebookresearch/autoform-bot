- **A topic page's "← topic" line shows in its first paint where the book
  shows it, and only there.** The page's frame followed CSS's `:target`, which
  WebKit sets only once it has read the whole page, after the book's
  script has loaded. Until then the frame left the line out, even on a
  statement's address. Then it showed the line for any element inside a
  statement's card, such as a heading in the card's notes, and for an address
  the browser decodes to a card's anchor, such as one with a percent-encoded
  letter, but not where an earlier element on the page shared a card's anchor.
  The frame now shows the line from the start where the address names a
  statement as the book's routes compare addresses, so it no longer changes
  while the book starts (#636, #833).
