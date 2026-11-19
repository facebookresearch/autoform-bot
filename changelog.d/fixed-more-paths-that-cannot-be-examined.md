- **More paths that cannot be examined, on Python 3.14.** More commands took a
  path they may not examine for an absent one on 3.14. `links set` and `move`
  said such a blueprint directory did not exist, `move` said a chapter whose
  README.md it could not examine had none, and `site check` said such a site or
  source was not a directory; 3.13 stopped each with a traceback, and now each
  says it cannot read the path. `library index` said such a package root did
  not exist and such a module had no source file, `dashboard` that such a site
  did not exist or had no `publication.json`, and `debrief` and `abstraction`
  took a project whose roadmap they could not examine for one with no roadmap
  yet, and such a directory for a missing one. The Lean MCP servers called such
  a Lean file missing, and a project whose Lake file or manifest they could not
  examine no Lake project. The others report the path as unreadable again, as
  on 3.13 (#830).
