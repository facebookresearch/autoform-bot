- **The dashboard's live claim overlay**, deprecated in 0.7.0, is gone:
  `autoform dashboard` serves the built site on loopback, with its Host and
  Origin checks, and nothing else. `--repo`, `--scratch`, the
  `/__autoform/live.json` endpoint and its `autoform-live/v1` payload go, so it
  no longer needs a Git `origin` remote, and render no longer writes
  `javascripts/blueprint-live.js`. A `--site-dir` without render's
  `publication.json` is refused, as one without the script was.
  `autoform claim list` prints the claims (#782).
