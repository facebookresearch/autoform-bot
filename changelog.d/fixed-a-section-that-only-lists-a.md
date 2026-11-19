- **A section that only lists a page's child topics is hidden from the first
  paint.** The book hides such a section, a child index, since Contents lists
  those topics, but it found them only once its search index arrived, after the
  first paint had shown them, so the reading below one moved up when the book
  started. Render now finds them, and the frame of the first paint and the book
  both hide them, at any depth, as the book looked; Material's page still shows
  them, and so does the book when its search index fails to load. Two rules
  change with it: every direct child topic counts, where the book counted only
  those Contents showed, and only a relative link counts. Contents now always
  shows the current topic's own topics, however many entries come before them,
  so a hidden index is never the only way to them (#636, #834).
