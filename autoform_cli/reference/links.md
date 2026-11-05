# Annotations in blueprint/links/

`blueprint/links/` holds [literature-links](https://github.com/mathlib-initiative/literature-links)
1.0.2 annotations: JSON Lines files, with optional `_defaults.json` folder
defaults. An annotation whose subject is a roadmap article carries that
article's facts, and every command reads them there instead of in its
frontmatter. Render never publishes the folder. A relative reference starts from
its own file, and `links/_defaults.json` holding `{"object": {"type": "lean",
"uri": "../.."}}` makes every end without a `uri` a declaration of the project.
Then `links/varieties/nullstellensatz.jsonl` may hold:

```json
{"id": "af_f8d7b54996f51594f707d44f", "kind": "theorem", "status": "formalized", "subject": [{"type": "generic", "uri": "../../roadmap/varieties/nullstellensatz.md"}], "object": [{"location": {"declaration": "Hartshorne.nullstellensatz"}}], "extensions": {"org.autoform": {"origin": "background"}}}
```

| Annotation | Frontmatter it replaces |
| --- | --- |
| a `lean` end in `object` whose `uri` is the project, the blueprint's parent folder | `lean:` names each end's `location.declaration`; the one with `role` `states` comes first and takes the credit, or the only one if it has no role |
| `status`: `formalized`, `stated` or `retracted` | `statement: formalized` and, unless `kind` is a definition's or `axiom`, `proof: formalized`; `statement: formalized` alone; `statement: retracted`. Each needs the credited end; `not_formalized` asserts nothing |
| `kind` | `declaration:`, as written |
| `id`, when it starts with `af_` | `article_id:`; Autoform ignores any other `id` |
| `extensions["org.autoform"]`: `origin`, `not_ready`, `discussion`, `review_approved`, `area`, `catalog`, `navigation` | the key of the same name; `not_ready` is a JSON boolean |
| an annotation with `extensions["org.autoform"]["mathlib"]`, a JSON boolean, and `lean` ends naming Mathlib declarations | `mathlib:`, `mathlib_declaration:`, and `mathlib_file:` from the first end's `module` |

`"object": []` records an article nobody has linked yet. The settings
`open_statements`, `tracks_lean`, `published` and `coverage_list_limit` stay in
README frontmatter; any other frontmatter key beside an annotation is ignored.
An article with no annotation keeps its frontmatter facts, with a warning while
the folder exists.

## Warnings

Loading never refuses a vault over the folder (#325). Every fact it leaves out
draws a warning that says why, one for each reason, counting the places and
naming the first five. Loading checks only the fields it reads; `check` adds
literature-links' own validation, whose problems are warnings too. Render
refuses a symbolic link here, as anywhere in `blueprint/`. Beside the folder,
`migrate article-ids --write` writes no ID and exits 2 if it has one to write.
