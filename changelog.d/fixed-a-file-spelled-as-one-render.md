- **A file spelled as one render writes, in another case, is left out.** A disk
  that ignores case wrote render's `dependencies/chapters/roadmap.json` over a
  vault's `Roadmap.json`, which `publication.json` still listed as published.
  Such a file is now left out with a warning on every disk, as a page at the URL
  of one render writes already is (#724).
