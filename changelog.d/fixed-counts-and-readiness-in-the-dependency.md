- **Counts and readiness in the dependency summary.** The summary
  `autoform-visualize` writes called the chapters' items the roadmap's
  articles, and its status table counted an article marked not ready with no
  declaration, so neither matched `autoform check`. It now gives check's
  article count, with the formalization targets and module inventories apart
  from the rest, and a status table that counts what check counts; where a
  chapter also counts such deferred work, it says by how many. Each listed
  article names what it waits for, up to five prerequisites and then a count.
  The legends no longer call a `mathlib: true` article upstreamed, since it
  restates an existing Mathlib declaration, and "not ready" no longer reads as
  missing blueprint work (#762, #778).
