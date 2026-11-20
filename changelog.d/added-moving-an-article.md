- **Moving an article.** `autoform move BLUEPRINT OLD NEW` renames an article,
  or a folder article with its whole folder, all or none, and keeps its
  `article_id` (#601). It rewrites every link the graph reads that names a moved
  file and rebases the moved pages' own links. It moves the article's annotation
  files, and a folder's mirror in `blueprint/links/`, rewriting only the
  references literature-links reads (`$schema`, and each end's `uri` and
  `location.profile`) and keeping every other byte and each file's mode. The
  implementation note and read-back cards named by its node id move with it,
  and one already at the new name refuses the move. The graph is loaded again
  and compared under the new names, and any difference undoes the move. It
  refuses another worker's live claim, by path ID or
  `article_id`, on an article whose page, implementation note, annotation
  file or inherited folder default it changes, and a claim board it cannot
  read, which `--dry-run` only warns about; with no `origin` and no `--repo`,
  it says `claims: not checked`.
  `--dry-run` prints the plan, and `--json` gives it as `autoform-move-plan/v1`.
  It warns about what it leaves as written: a link no reader counts (an image,
  raw HTML, code, math, a comment, or a definition the graph does not read), one
  that names what moves only through other capitals, a read-back card, a review
  approval whose title, statement or cited passage it changes, `atlas.json`,
  `structure.md` and the old published address.
