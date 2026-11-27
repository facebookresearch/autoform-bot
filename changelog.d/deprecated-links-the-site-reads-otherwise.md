- **Links the site reads otherwise.** From 0.9.0 the graph, `check` and `audit`
  read an article's links as the site does, with its own Markdown converter, and
  Autoform's own link reader goes (#880). Until then the graph is unchanged, and
  `check` warns, and `audit` warns as `site-reading`, wherever that would change
  an outcome: a dependency, proof dependency or informal use that starts or
  stops counting or changes section, one that would then refuse the vault, and
  a `## Sources` or coverage link they would judge otherwise. Each link warns
  once, or each heading the site reads otherwise, such as `Notes` over `---`,
  an indented `## Depends on` or a later H1, once for the links below it. An
  autolink to an external source counts from 0.9.0 and warns about nothing. A
  link inside a `<textarea>` or `<xmp>`, which a browser shows as text, is no
  link to the site, so it warns; a raw-HTML `<a href>` counts nowhere.
