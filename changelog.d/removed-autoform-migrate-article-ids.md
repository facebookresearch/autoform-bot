- **`autoform migrate article-ids`**, deprecated in 0.7.0, is gone, with its
  `--check` gate, its `--write` and its `--json` plan.
  `autoform links set BLUEPRINT ARTICLE` gives an article its `article_id`,
  and `autoform migrate links blueprint --write` gives one to each annotation
  it writes (#796).
