# The published site

What `render` publishes, and how the site is served, deployed and checked. The
[CLI reference](../README.md#publishing-and-the-dashboard) gives the commands.

## Rendered pages

`dependencies.md` and old pages under `dependencies/` redirect to the matching
book page, keeping the address's fragment when JavaScript runs; `structure.md`
links the book. `dependencies.json` and the payloads under `dependencies/`, such
as the index `dependencies/index.json`, are internal; their schemas may change.
`dependencies/map/` is a standalone page: one zoomable map of the project, which
loads `dependencies/map/data/` level by level and shows each result's statement
from its article and, on a click, its local graph. A formal statement links to
its canonical article or containing chapter card; legacy `#node=<id>`, view and
filter links redirect there, not to a second viewer. A `lean:` declaration
resolving to a repository permalink gets a source link; a catalog's Lean row
shows its first eight names and counts the rest. Render percent-encodes each
link it writes from file and folder names, such as the book's dependency links;
a Markdown link it moves keeps its decoded spelling and starts with `./` when
its first segment holds a colon, so no name can make a `javascript:` URL.

Authored Mermaid is supported in the vault and book, drawn in strict mode with
plain-text labels and loaded only on a page that has one: click callbacks and
HTML in labels do not work, links to URLs do. Point `mkdocs.yml` at
`docs_dir: site-src` and enable `md_in_html` and a `pymdownx.superfences`
mermaid fence (see the [setup template](../templates/mkdocs.yml)); list the
generated `javascripts/blueprint-mermaid.js`, not the Mermaid library. The
[publication contract](runtime-and-publication.md#publication-contract) gives
the schema and when render replaces an earlier site.

## Files render writes

`render` writes `javascripts/mathjax.js`, `stylesheets/blueprint.css`,
`javascripts/blueprint-mermaid.js`, `javascripts/blueprint-live.js`,
`javascripts/blueprint-dag.js`, `stylesheets/blueprint-dag.css`,
`javascripts/project-map.js`, `stylesheets/project-map.css`,
`assets/autoform.svg`, `SUMMARY.md`, `structure.md`, `publication.json`,
`dependencies.md`, `dependencies.json`, the `dependencies/` pages, and the
chapter pages it consolidates, over the vault's copies. A directory holding a
file at any of these paths, or a file where one of their folders goes, stops
`render` with its path before anything is written, and `check` warns about it.
The stale root `dependencies.html`, `graph.html`, and `progress.md` are kept out
of the site. A `tex-macros.json` directory that holds files leaves the site
without project macros, with a warning.

Besides its Markdown pages, a vault publishes only images (`.png`, `.jpg`,
`.jpeg`, `.gif`, `.webp`, `.avif`, `.bmp`, `.ico`), `.pdf`, and `.txt`, `.csv`,
`.json`, `.bib`, `.tex`, and `.lean` files, whatever the case of the suffix;
`check` and `render` warn by path about any other file, such as an `.html` page
or an `.svg`. An Obsidian `.canvas` file, a top-level `templates/` folder (as in
MkDocs), a [pinned name list](pinned-names.md) and [`links/`](links.md) are left
out silently, as `.obsidian` is, and a non-UTF-8 page with a warning. At a
verified commit, `sources/` publishes only notes and images and links any other
file the commit holds there. A link to a file the site leaves out or a hidden
path outside `sources/` keeps only its text; `check` and `render` warn about
each one into a hidden path, a `published: false` folder or `templates/`.

## MathJax

`mkdocs.yml` lists `javascripts/mathjax.js`, the site's MathJax configuration,
which loads MathJax 3.2.2's pinned bundle once the page is parsed and leaves
formulas as typed under any other release; a site that lists the bundle too runs
that copy, unpinned, until it drops the line. Another script's `window.MathJax`,
such as Material's documented snippet, is ignored with a console error. The
articles on a page share one TeX input with the packages base, ams, noundefined,
boldsymbol, mathtools, braket, mhchem, and amscd (`CD` diagrams), and project
macros from `blueprint/tex-macros.json`, written as MathJax's `tex.macros` is: a
name maps to a body, `[body, arguments]`, or `[body, arguments, default]`.

That input turns off `\label`, `\mmlToken`, `\DeclareMathOperator`, the
paired-delimiter and tag-form commands of mathtools, and `\mathtoolsset`, and
leaves off the packages MathJax would load on demand: cancel, bbox, enclose,
color, html, unicode, action, newcommand, extpfeil, verb, and bussproofs. So
`\def`, `\require`, `\href`, and any other command those packages do not define
show in red and do nothing, however a formula or macro reaches them. A macro may
not take one of their names; `check` and `render` warn about a macro they leave
out, judging the copy of `tex-macros.json` that `render` reads once and
publishes. `\bmod`, `\pmod`, `\mod`, `\varliminf`, `\varlimsup`, `\varinjlim`
and `\varprojlim`, which MathJax builds from `\mmlToken`, are written again
without it, and a project macro of the same name replaces one. Each read-back
card has its own TeX input with only base, ams, and noundefined. Only renderer-marked
formulas, such as `$...$` in Markdown, are typeset. Formulas in links, controls and
navigation get no extra MathJax tab stop, menu or accessibility explorer. Refreshes
retain earlier glyph styles; metadata titles and Material navigation read formulas as
text, while `discussion:` values and Material ToC stay as typed. Explorer reading links and
breadcrumbs typeset publisher-derived title parts with project macros. A formula paints only within
its own line and block, and an inline formula wider than its paragraph is cut at
its edge, so write one that wide as a display, which scrolls.

## Dashboard

Only the loopback server exposes the dashboard's read-only live overlay of
current author claims, at `/__autoform/live.json`. The overlay matches claims on
an article's node ID or its `article_id`, reports the `claim_target` each lease
used, and is never written into the vault, publication manifest, or public site.
Claim badges update live; re-render and rebuild the site to refresh the rest.

## Pages deploys

Pass a Lake project in a subdirectory, such as `v1/`, as `--lean-root v1`, also
in place of the `--lean-root .` the generated `blueprint-pages.yml` passes to
`check`, `audit` and `render`; links name paths from the repository's top level.

The generated `blueprint-pages.yml` deploys only a commit that the project's own
`.github/workflows/autoform-verify.yml` (matched by file, not name) passed on a
push to `main`, with code links rendered at that commit by `--ref` and, when
that run uploaded its top-level Lake build's `.ilean` files (artifact
`autoform-ileans`) and `AUTOFORM_REF`'s render takes `--ileans`, the names in
its Lean linked by them. Pull requests and manual runs build the site but never
deploy or cancel a waiting deploy. To redeploy, re-run the Pages run started by
the newest successful verify run on `main`; an older one deploys its commit.
Deleting, renaming or disabling `autoform-verify.yml` silently stops deploys.

## Pages reports

Pages gates on `check` and `render`, and runs `audit` after `check` and
`site check` after the MkDocs build only as reports, with `continue-on-error`:
`audit` logs every finding, or `OK: roadmap audit passed`. An invalid coverage
contract fails `render` before it writes anything, but `MAPPED` rows (each an
audit `declared-coverage-gap`; a new project lists the template's row) and
coverage-role issues, which `render` prints as warnings, still publish. To block
merges on findings, run `autoform audit` in CI without `continue-on-error`.
