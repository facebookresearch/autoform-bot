# The published site

What `render` publishes, and how the site is served, deployed and checked. The
[CLI reference](../README.md#publishing-and-the-dashboard) gives the commands.

## Rendered pages

`dependencies.md` and the old pages under `dependencies/` redirect to the
matching book page, keeping the address's fragment when JavaScript runs;
`structure.md` links the book. Complete published statements, proofs and
citations remain the reading content. The index `dependencies/index.json`,
project payload `dependencies.json`, and other `dependencies/` payloads are
internal to this viewer; their schemas may change. `dependencies/map/` is a
second, standalone page: one zoomable map of the whole project, which loads
`dependencies/map/data/` one level at a time and shows each result's statement
from its article. A formal statement links to its canonical article or
containing chapter card; legacy `#node=<id>`, view and filter links redirect
there, not to a second viewer. A `lean:` declaration resolving to a repository
permalink gets a source link; a catalog's Lean row shows its first eight names
and counts the rest. Render percent-encodes each link it writes from file and
folder names, such as the book's dependency links; a Markdown link it moves
keeps its decoded spelling and starts with `./` when its first segment holds a
colon, so no name can make a `javascript:` URL.

Render writes no Mermaid graph of its own. Authored Mermaid remains supported in
the vault and book, drawn in strict mode with plain-text labels and loaded only
on a page that has one: click callbacks and HTML in labels do not work, links to
URLs do. Point `mkdocs.yml` at `docs_dir: site-src` and enable `md_in_html` and
a `pymdownx.superfences` mermaid fence (see the [setup
template](../templates/mkdocs.yml)); list the generated
`javascripts/blueprint-mermaid.js`, not the Mermaid library. The [publication
contract](runtime-and-publication.md#publication-contract) gives the
schema and when render replaces an earlier site.

## Files render writes

`render` writes `javascripts/mathjax.js`, `stylesheets/blueprint.css`,
`javascripts/blueprint-mermaid.js`, `javascripts/blueprint-live.js`,
`javascripts/blueprint-dag.js`, and `assets/autoform.svg` over the vault's
copies, and writes `SUMMARY.md`, `structure.md`, `publication.json`,
`dependencies.md`, `dependencies.json`, the `dependencies/` pages, and the
chapter pages it consolidates; a vault copy of one of these is not published. A
directory holding a file at any of these paths, or a file where one of their
folders goes, stops `render` with its path before anything is written, and `check` warns about
it. The stale root `dependencies.html`, `graph.html`, and `progress.md` are kept
out of the site. A `tex-macros.json` directory that holds files leaves the site
without project macros, with a warning.

Besides its Markdown pages, a vault publishes only images (`.png`, `.jpg`,
`.jpeg`, `.gif`, `.webp`, `.avif`, `.bmp`, `.ico`), `.pdf`, and plain-text
`.txt`, `.csv`, `.json`, `.bib`, `.tex`, and `.lean` files, whatever the case of
the suffix. The site leaves out any other file under `blueprint/`, such as an
`.html` page, an `.svg`, a script, or a stylesheet, and `check` and `render`
warn about it by path: write it as Markdown, an image, a PDF, or plain text, or
keep it outside `blueprint/`. An Obsidian `.canvas` file, a [pinned name
list](pinned-names.md) and [`links/`](links.md) are left out silently, as
`.obsidian` is, and a Markdown page that is not UTF-8 with a warning. A link to
a file the site leaves out, a hidden path outside `sources/`, or a page in a
`published: false` folder keeps only its text; `check` and `render` warn about
each such link to a hidden path or an unpublished folder.

## MathJax

`mkdocs.yml` lists `javascripts/mathjax.js`, the site's MathJax configuration,
before the MathJax bundle, as `autoform init` writes both. It expects MathJax
3.2.2, loads it itself when no bundle is listed, and leaves formulas as typed
under any other release. An assignment to `window.MathJax` in another script,
such as the snippet Material's documentation gives, is ignored with a console
error. The articles on a page share one TeX input with the packages base, ams,
noundefined, boldsymbol, mathtools, braket, mhchem, and amscd (`CD` diagrams),
and the project's macros from `blueprint/tex-macros.json`, written as MathJax's
`tex.macros` is: a name maps to a body, `[body, arguments]`, or `[body,
arguments, default]`.

That input turns off `\label`, `\mmlToken`, `\DeclareMathOperator`, the
paired-delimiter and tag-form commands of mathtools, and `\mathtoolsset`, and
leaves off the packages MathJax would load on demand: cancel, bbox, enclose,
color, html, unicode, action, newcommand, extpfeil, verb, and bussproofs. So
`\newcommand`, `\def`, `\require`, `\cancel`, `\bbox`, `\color`, `\href`,
`\unicode`, and any other command those packages do not define show in red and
do nothing, however a formula or macro reaches them. A macro may not take one of
their names; `check` and `render` warn about a macro they leave out, judging the
copy of `tex-macros.json` that `render` reads once and publishes. `\bmod`,
`\pmod`, `\mod`, `\varliminf`, `\varlimsup`, `\varinjlim` and `\varprojlim`,
which MathJax builds from `\mmlToken`, are written again without it, and a
project macro of the same name replaces one. Each read-back
card has its own TeX input with only base, ams, and noundefined. Only renderer-marked
formulas, such as `$...$` in Markdown, are typeset. Formulas in links, controls and
navigation get no extra MathJax tab stop, menu or accessibility explorer. Refreshes
retain earlier glyph styles; graph labels, metadata titles, `discussion:` values,
home-page leads and Material navigation/ToC stay typed. Explorer reading links and
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
push to `main`, with code links rendered at that commit by `--ref`. A failed
verification deploys nothing, so the last verified site stays up. Pull requests
and manual runs build the site but never deploy, and cannot cancel a waiting
deploy. To redeploy, re-run the Pages run that the newest successful verify run
on `main` started; re-running an older one deploys the older commit. Deleting or
renaming `autoform-verify.yml`, or disabling it in the repository's Actions
settings, stops every deploy, with no error. An older scaffold deploys every
push; [Upgrading](../../CHANGELOG.md#upgrading-from-050) says how to switch, and no
pin has to move: every `AUTOFORM_REF` since 0.5.0 has `render --ref`.

## Pages reports

Pages gates on `check` and `render`, and runs `audit` after `check` and
`site check` after the MkDocs build only as reports, with `continue-on-error`:
`audit` logs every finding, or `OK: roadmap audit passed`. An invalid coverage
contract fails `render` before it writes anything, but `MAPPED` rows (each an
audit `declared-coverage-gap`; a new project lists the template's row) and
coverage-role issues, which `render` prints as warnings, still publish. An older
scaffold can add either step by hand with `continue-on-error: true` and the same
`uvx --from` source: `autoform audit blueprint --lean-root .` after `check`, and
`autoform site check site --source site-src` after the MkDocs build, which needs
an `AUTOFORM_REF` from 0.6.0 or later. To block merges on findings, run
`autoform audit` in CI without `continue-on-error`.
