- **`audit` warns about headings the site reads otherwise.** A line such as
  `#43655.`, which the site shows as a heading and Autoform reads as text, or an
  indented heading only Autoform reads, now gives a `site-heading` warning at
  its article, with `check`'s text and line, so a CI that runs only `audit` sees
  it too (#598). It never fails the audit. Unlike `check`, `audit` also warns on
  a page the site shows as typed.
