- **A statement's address first paints its own trail.** A page opened at a
  statement's address, such as `topic/index.html#theorem`, first painted its
  topic page's trail, and the book's first commit then linked the topic and
  added the statement's title, so the trail moved as the book took over. Render
  now writes, for each statement on a page, the trail the book draws for it,
  and the page's first script puts that trail in on such an address. Each page
  also marks what the first commit reads from the search index: whether the
  page's place is a topic, and which statement cards are results (#636, #806).
