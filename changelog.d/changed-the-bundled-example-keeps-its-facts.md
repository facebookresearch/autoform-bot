- **The bundled example keeps its facts in `blueprint/links/`.** Its articles'
  facts left their frontmatter through `migrate links --write`, and its CI's
  `AUTOFORM_REF` moved to 0.7.0, the first release that reads them there. Its
  `mkdocs.yml` no longer loads the Mermaid script, which that release's site
  script loads itself (#816).
