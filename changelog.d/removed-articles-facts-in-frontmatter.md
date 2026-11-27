- **Articles' facts in frontmatter are not read.** Deprecated in 0.7.0: an
  article's facts come only from its annotation in `blueprint/links/`, and its
  frontmatter gives only the settings `open_statements`, `tracks_lean`,
  `published` and `coverage_list_limit`. A fact line's value is not read or
  checked; one warning per load counts the articles whose frontmatter still
  holds one and names the first five and the command that moves them, and
  `render` refuses to publish while any does. `check`'s `warning[deprecated]:`
  line goes, and with it the loader's warning that such articles kept their
  facts until 0.8.0 (#843).
