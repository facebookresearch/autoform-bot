- **`autoform search --state` and `--declaration`**, deprecated in 0.7.0, are
  gone: filter each `--json` hit's `state` or `declaration` instead, with
  `--limit` at least `total_matches`. `filters` stays in `autoform-search/v1`,
  with both lists always empty (#765).
