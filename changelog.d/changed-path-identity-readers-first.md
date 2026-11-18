- **Path identity, readers first.** An article's node id, its path below
  `roadmap/`, starts to replace its `article_id` (#601). Autoform now reads the
  forms below that name an article by its node id, while every writer keeps the
  `article_id` form, so CI at an older `AUTOFORM_REF` keeps passing; a later
  change switches the writers, with the migration. The `article_id` forms are
  read until 0.9.0. `review check` also accepts an approval, spelled
  `v2:sha256:<64 hex>`, of the review hash without the article's `article_id`,
  where its cited passage stands (the passage stays bound) and the targets of
  its statement's plain inline links (their text and titles stay bound), which a
  move keeps; it still prints the hash with them, and an approval of that one
  still drifts when a move rewrites a link in the statement. A link is plain,
  and the site surely shows it as one, when it is written on one line with text
  holding no bracket, nor a backslash outside math and code, a target holding no
  space, bracket, angle bracket, parenthesis, quote, backslash, backtick or
  dollar, and at most a double-quoted title holding none of these but spaces and
  single quotes; stands neither inside another bracket nor right after a `]`;
  and the lines up to the blank ones around it hold no `|` or control character,
  even in code or math, no display math or fence, and no `<` outside code and
  math; none is indented four or more spaces once its quote and list markers are
  read, or ends in `]`, and each closes every code span, formula and bracket it
  opens, a code span on the whole run of backticks it opens at, with no formula
  across a code span and every link's destination plain. No target is cut in a
  statement holding a raw HTML block, a fence opened in a quote or a list, which
  a quote carries past blank lines, or the start tag of an element such as
  `<textarea>`, whose content a browser shows as text, anywhere, code included,
  nor in an article holding a line break only Python reads, such as U+2028,
  which the site reads as text. Any other link, a reference definition and an
  image keep their targets bound, so retargeting one still drifts an approval of
  the new hash. An Autoform before this one reads a `v2:` approval as drift.
  Read-back cards are read under `readbacks/<node id>/` too, in schema
  `autoform-readback/v2`, which records no `article_id`; a declaration with a
  card under each has no valid card, and the inventory keys an unpublished
  folder's cards there once. Implementation notes are read at
  `.implementation-notes/<node id>.md` too, in folders as in `roadmap/`: a
  folder article's note is `<folder>.md` and the root's `roadmap.md`. `check`
  warns about an article with a note under each name, `audit` reports either as
  stale, and a folder among the notes is read rather than warned about. A note a
  project already named by node id, which `check` warned about, now counts, so
  `audit` can report it as stale. A debrief record without an `article_id`
  loads.
