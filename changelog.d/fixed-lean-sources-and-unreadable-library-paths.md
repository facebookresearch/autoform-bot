- **Lean sources and unreadable library paths on Python 3.14.** From 3.14,
  pathlib finds no suffix in `..lean`, so that file, Lean's module `«.»`, was
  left out of the index Autoform builds from a project's Lean sources and of
  library verification, and the Lean MCP servers refused it. pathlib also reads
  a path it may not examine as absent, so `library list` and `search --library`
  called a checkout they could not examine not a Git checkout, or one with no
  readable Git HEAD, and `library list` said such a Lean root did not exist.
  Sources are now named by Lean's own rule, those paths are reported as
  unreadable as on 3.13, and CI tests 3.14 too (#800).
