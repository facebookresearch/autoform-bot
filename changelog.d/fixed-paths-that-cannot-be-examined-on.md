- **Paths that cannot be examined, on Python 3.14.** From 3.14, pathlib reads a
  path it may not examine, such as one below a folder that cannot be searched,
  as absent. So `work import-impact --module-scope import-closure` left out
  such a package directory, library folder or workspace override, and a file
  there that may import the module no longer made it refuse the narrowed
  scope. `search`, `work list` and the other commands that take a project or
  blueprint said it did not exist; `check` and the other commands that load the
  roadmap (`audit`, `skeleton`, `review`, `links`, `move`, `migrate`,
  `autoform-visualize`) said so of its roadmap, and `render` of the blueprint.
  Each is reported as unreadable again. The commands that load the roadmap, and
  `render`, now say they cannot read the roadmap or capture the blueprint; 3.13
  stopped all of them but `review prepare` and `review record` with a traceback
  there (#813).
