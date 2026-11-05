# Annotations in blueprint/links/

`blueprint/links/` holds [literature-links](https://github.com/mathlib-initiative/literature-links)
1.0.2 annotations in JSON Lines files, with optional `_defaults.json` folder
defaults; render never publishes it. An annotation about a roadmap article
carries its facts, which every command reads there instead of in its
frontmatter, where only the settings `open_statements`, `tracks_lean`,
`published` and `coverage_list_limit` stay. An article with no annotation keeps
its frontmatter facts, with a warning. With `links/_defaults.json` holding
`{"object": {"type": "lean", "uri": "../.."}}`, which makes an end without a
`uri` the project's, `links/varieties/nullstellensatz.jsonl` may hold:

```json
{"id": "af_f8d7b54996f51594f707d44f", "kind": "theorem", "status": "formalized", "subject": [{"type": "generic", "uri": "../../roadmap/varieties/nullstellensatz.md"}], "object": [{"location": {"declaration": "Hartshorne.nullstellensatz"}}], "extensions": {"org.autoform": {"origin": "background"}}}
```

| Annotation | Frontmatter |
| --- | --- |
| a `lean` end whose `uri` is the project, the blueprint's parent folder | `lean:`, each end's `location.declaration`, the credited one first: the end with `role` `states`, or the only end if it has no role |
| `status` `formalized`, `stated` or `retracted`, beside a credited end | `statement: formalized` with `proof: formalized` unless `kind` is a definition's or `axiom`; `statement: formalized`; `statement: retracted` |
| `kind`, and `id` when it starts with `af_` | `declaration:` as written, and `article_id:` |
| `extensions["org.autoform"]`: `origin`, `not_ready`, `mathlib`, `discussion`, `review_approved`, `area`, `catalog`, `navigation` | the same keys; `not_ready` and `mathlib` are JSON booleans |
| a `lean` end whose `uri` is `https://github.com/leanprover-community/mathlib4` | `mathlib_declaration:`, and `mathlib_file:` from the first end's `module` |

Loading never refuses a vault over the folder (#325): every fact it leaves out
draws a counted warning saying why. It checks only the fields it reads, and
`check` adds literature-links' validation. Beside the folder, `migrate
article-ids --write` writes no ID: until `autoform links set` exists, give a new
article's ID as its annotation's `id`.
