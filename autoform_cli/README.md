# Blueprint format and CLI

The Autoform CLI validates, visualizes, and publishes the multilevel dependency
graph embedded in `blueprint/roadmap/`. The Markdown book is the graph: no
separate authored or generated graph file exists.

This is an agent-facing interface. Normal project work starts from Autoform's
skills in the user's preferred agent window; those agents invoke these commands
as needed.

## Articles and containment

Every Markdown file below `blueprint/roadmap/` is an article node. A
`README.md` represents its directory and strictly contains the articles below
it; the nearest ancestor `README.md` is the single parent. This supports any
number of levels, from book to chapter to section to declaration. Ordinary
files use their path without `.md` as a stable ID; `README.md` uses its
directory path, with the root article named `roadmap`.

The H1 is the article's human title. Container
prose supplies the mathematical exposition, and a standalone list item linking
to a formalizable leaf places that definition or result at the exact position in
the published chapter. Leaves without a placement slot appear under an explicit
“Additional formalization targets” section rather than disappearing; a module
catalog without one appears under “Module inventories” instead.

Keep the root roadmap article short: it is the book's preface and table of
contents, not a dump of every planned milestone. Large planning inventories
belong in coverage or progress views; detailed containers should introduce their
mathematics in prose and place statements under meaningful section headings.

Frontmatter records checked facts:

```markdown
---
article_id: af_5b0e4d3c2a1f09e8d7c6b5a4
declaration: theorem
origin: cited
statement: formalized
proof: formalized
lean: MyProject.separatingHyperplane
---

# Separating hyperplane theorem

State the intended result and proof sketch here.

## Depends on

- [Convex set](convex.md)

## Proof depends on

- [Supporting hyperplane](supporting-hyperplane.md)

## Sources

- [Chapter 2](../../sources/convexity.md#separation)
```

An article's statement is its body from the end of the frontmatter to the first
`##` heading, without the H1 line. The article's first H1 (its title), if
written below a `##` section, ends that section and resumes the statement; a
later H1 stays in its section. No heading deeper than `##` ends it, and neither
does a `##` line inside a fenced code block or an HTML comment. `autoform audit`
and the published theorem box both read that span. A fence closes only on a
bare fence line. A fence that never closes is read as plain text when its
article is inspected alone, so it hides no heading or dependency link there,
but on a chapter page it can pair with a later article's closing fence and hide
everything between them. The audit reports a fence with no closing line as
`unclosed-code-fence`. A later bare fence line of the same character and at
least the opener's length closes it here, though the page asks for the opener's
exact length and indent, so close each fence with its own line. The page can
also pair an unclosed backtick fence line with a later backtick run in the same
paragraph and show the text between them as inline code; a link there is not a
link on the page but is still read as a dependency.

An HTML comment that never closes, even one opened by a `<!--` in inline code
or an indented code block, hides every heading and dependency link after it:
the graph loses those edges, and the part of the article the comment opens in
keeps the rest, so one opened in the statement puts the later sections,
dependency list included, in the theorem box. Nothing reports the comment
itself.

`## Depends on` lists what the article needs in order to be *stated*;
`## Proof depends on` lists what only its *proof* needs. Both are graph edges,
except a link in a code block, fenced or indented, or in an HTML comment. Links
anywhere else are ordinary navigation or citations. Dependencies resolve
relative to the current article and must point at another roadmap article.

The optional `declaration` field marks a formalizable leaf and describes its
intended Lean artifact, for example `def`, `theorem`, `lemma`, `structure`, or
`instance`. Container and exposition articles omit it. Autoform records this
hint but does not constrain the set of Lean declaration commands. Declarations
that introduce data rather than a proposition carry no separate proof
obligation; leading modifiers such as `noncomputable` do not change that, and an
`axiom` does not get this shortcut.

`origin` records provenance for formalizable work: `cited` for a direct source
target, `bridged` for a result introduced between source targets, and
`background` for prerequisite mathematics.

Frontmatter is optional. A container article that only supplies prose and
placement needs none at all; only checked facts are recorded.

The optional `area` field places a container in an authored mathematical
region of its parent's explorer map; on a leaf it only labels the article. Use
mathematical areas such as `Foundations` or `Geometry & Topology`, never
repository or workflow buckets such as `MathlibExt` or `catalog-01`. Autoform
does not guess areas from paths, imports, or titles.

Mathematical topics and repository organization are different hierarchies.
An optional container-only `navigation` role makes that distinction explicit:

- `topic` (the default): a mathematical subject or coherent group of results.
- `collection`: a library or organizational collection. Its mathematical
  children are exposed in the parent's overview; the collection remains in
  **Sources** with its own route.
- `transparent`: a generated pagination/range container. It is flattened in
  topic navigation and omitted from the outline and ancestor breadcrumbs.
- `source`: a book or other source corpus, available through **Sources** rather
  than presented as a peer mathematical concept.

These roles never change article identity, containment, proof readiness, or
dependency edges. A relation mentioning a collection itself stays attached to
that collection; it is not attributed to a child without evidence. Source-book
results and direct leaves remain in **All results**, including items with no
edges. The global outline is a shared derived index, not another authored graph.
Older pins reject the new frontmatter: move `AUTOFORM_REF` before adding it.

An overview with more than twelve topics and multiple authored `area` labels
opens at region scale. Open a region, then a topic in one click; a separate
**Connections** control exposes its genuine prerequisites and dependents.
Browser Back restores these scales. Regions roll up the actual topic edges;
no geometric relation is inferred from a directory or a shared title.

For a taxonomy maintained separately from article files, `blueprint/atlas.json`
may assign the same areas without moving pages:

```json
{"schema":"autoform-atlas/v1","areas":{"Geometry & Topology":["topology","geometry"]}}
```

Every listed value is a roadmap node id. A node takes the first area that
lists it, and an `area:` authored on the node wins over the file. The file only
assigns areas, so it never stops a command: `check` and `render`
warn about each entry they ignore, or about the whole file when it is not
readable `autoform-atlas/v1` JSON, and `audit` reports each as an
`atlas-omission` [warning](#warnings). An older `AUTOFORM_REF` ignores the file.

A repository-wide inventory may use a narrative leaf to summarize an existing
Lean module containing several declarations. Such a leaf sets
`catalog: module` and omits `declaration`, so it is never dispatched as one
proof task. A catalog must be a leaf: `audit` reports one with contained
articles as `catalog-container`. Every catalog links a declaration ledger under
`blueprint/sources/` from its `## Sources` section: `check` warns about a
catalog that links none and `audit` reports it as `catalog-missing-ledger`,
while a link to a missing or hidden ledger fails both, as such a source link
does for any article. A completed catalog also
records in `lean:` the full name of every public declaration the module writes
with a declaration keyword, which is what the Lean source index records (see
[Validation](#validation)). Names Lean generates, such as constructors,
structure fields, derived or unnamed instances, and `@[ext]`, `@[simps]`, or
`@[to_additive]` results, and declarations the index cannot see yet, behind
`nonrec` or `meta` or written `class inductive`, go in the ledger as
supporting entries, never in `lean:`; a `private` name never goes in `lean:`.
A completed catalog may assert `statement: formalized` and `proof: formalized`
when the complete module has been checked. It contributes to the neutral
inventory metric and graph status while remaining a readable page in the vault
and a section of its parent chapter on the site, but its own entry stays
outside the mathematical `Scoped roadmap` completion percentage. Like any
prerequisite, a catalog linked under `## Depends on` or `## Proof depends on`
holds its dependents back until its whole-module assertion is recorded, so a
dependent that needs one result links the declaration article that expounds it.

`check --lean-root` and `audit --lean-root` resolve every name the catalog
lists, and say for a catalog's unresolved name that a generated name belongs in
the ledger. They cannot prove that the list omitted no newly added public
declaration; completeness remains an authored assertion pending
repository-inventory reconciliation. `audit --lean-root` also reads the
[elaboration limits](#elaboration-limits) of the declarations a catalog
resolves and reports them as warnings, which never fail the audit. A catalog
that records `proof: formalized` also gets `lean-target-wanted` for a wanted
target it names; the deprecation, declaration-kind, and node-size checks apply
only to declaration articles.

Versioned implementation notes live outside the mathematical roadmap at
`blueprint/.implementation-notes/<article_id>.md`, one file per article. The
durable ID survives article moves. Claims keep independent workers from editing
the same leaf's note; containers have no claims, and any worker may delete a
note that `autoform audit` reports as stale or repair an orphaned one. Every
command that loads the graph, including `autoform check`, rejects malformed,
empty, symlinked, or orphaned notes; assign the article ID before creating its
note. Parallel branches can orphan a note without a Git conflict, when one
deletes or merges an article while another writes its note; a worker repairs
that by moving anything still useful into the surviving article's note and
deleting the orphan. When the article still exists but lost its `article_id`
line, restore the line instead.
Delete the file once its article is proved; for a container, delete it once
every formalizable descendant, and every leaf marked `not_ready` beneath it,
is proved and there is at least one; a module catalog never counts. The
directory is hidden, so current and older renderers omit it from the site and
publication hash without requiring an `AUTOFORM_REF` migration; `autoform
audit` reports a note left behind after that point.

## Assertions and derived status

An article asserts only facts a human or agent verified:

| Key | Meaning |
| --- | --- |
| `area: Geometry & Topology` | Authored mathematical region for atlas grouping. CI's `autoform check` at an older `AUTOFORM_REF` rejects the key, so move the pin first, or assign the area in `blueprint/atlas.json`, which older refs ignore. |
| `navigation: collection` | Presentation-only container role: `topic`, `collection`, `transparent`, or `source`. Collections and pagination are transparent in mathematical navigation; books/libraries remain source facets. Older pins reject this key. |
| `catalog: module` | A non-dispatchable leaf cataloging one existing Lean module. Its `lean:` lists the module's public keyword-declared names, and it links its declaration ledger under `blueprint/sources/` from `## Sources`; cannot be combined with `declaration` or the `mathlib*` keys. CI's `autoform check` at an older `AUTOFORM_REF` rejects the key, so move the pin first. |
| `statement: formalized` | The Lean statement exists and compiles. Requires `lean:`. |
| `statement: retracted` | A revision retracted the statement while `lean:` still names the old declaration, which stays in the build until Formalize restates the article and records `statement: formalized` in its place. Requires `lean:`; invalid with `proof: formalized` or `mathlib: true`. CI's `autoform check` at an older `AUTOFORM_REF` rejects the marker, so move the pin first; until then, retract by removing `statement` and `proof` and keeping `lean:`. |
| `proof: formalized` | The Lean proof compiles. Requires `statement: formalized` and `lean:`. Under the open policy it may rest on open statements, and the article is then conditional; only the derived `fully_proved` means complete and `sorry`-free. |
| `mathlib: true` | The result is upstreamed into Mathlib. |
| `not_ready: true` | Needs more blueprint work before it can be attempted. |
| `lean: Ns.decl` | Declaration name(s) that discharge the article. |
| `discussion: 42` | Issue number or URL where the article is being discussed. |
| `article_id: af_...` | Durable identity, `af_` plus 24 lowercase hex digits; `autoform work` requires it on unfinished formalizable leaves. |
| `open_statements: allowed` | Project policy, valid only in `roadmap/README.md`: a theorem's statement may land with a `sorry` proof (see [Open statements](#open-statements)). Absent or `forbidden` keeps the strict policy. |

Everything a reader thinks of as progress is *derived* from the DAG on every
run, so it cannot go stale. Readiness depends on the project's policy: under
the default strict policy CI rejects every `sorry`, so a theorem's statement
lands only with its proof; under `open_statements: allowed` it may land with a
`sorry` proof.

| Derived state | Holds when |
| --- | --- |
| `can_state` | Every statement prerequisite is stated and every proof prerequisite is proved. Under the open policy a theorem waits for no proof prerequisite, and a definition, whose body is its proof, waits for them to be stated. |
| `can_prove` | Stated, every statement prerequisite is stated, and every proof prerequisite is proved (strict policy) or stated (open policy). |
| `proved` | The article records `proof: formalized`, is a stated definition, or is in Mathlib. |
| `conditional` | Proved, but the proof rests on an open statement; open policy only. |
| `fully_proved` | Proved, and every prerequisite is fully proved, recursively. |
| `defined` | A definition is written but rests on unfinished work; one whose body rests on an open statement is `conditional` instead. |

`proved` and `fully_proved` differ on purpose: a theorem whose own proof
compiles but which rests on unfinished work is green, not dark green.
`conditional`, labelled "conditionally proved", is the open policy's case of
that: the article is proved, but it reaches an open statement, a theorem that
is stated or retracted but not proved
(see [Open statements](#open-statements)), through its dependencies. An open
dependency counts together with whatever its statement prerequisites reach, and
a proved dependency passes on
everything it reaches. The site colours it violet, never green, and lists those
open statements in an `Assumes` row on the article page. It is never
`fully_proved`, which keeps its meaning in both policies; a conditional result
is complete only once every open statement it assumes is proved. The
palette and state names follow
[leanblueprint](https://pypi.org/project/leanblueprint/), so the published
graph reads the same way as the Lean community's LaTeX blueprints.

## Commands

This section is the single source of truth for the command line. Skills
describe what to achieve and link here; they do not restate flags, so a change
to the CLI lands in one place.

The commands below are written as they appear on `PATH` once this Python
package is installed. Inside a consumer project the package is not installed,
so resolve `<AUTOFORM_PLUGIN_ROOT>` from the loaded plugin and prefix each one,
running from the project root:

```bash
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform check blueprint --lean-root .
```

Create a new project's vault, site configuration, and CI. The layout is fixed,
so it is written rather than described; existing files are left alone, which
makes the same command the repair path:

```bash
autoform init . --title "Finite Flat Group Schemes" \
  --repository-url https://github.com/owner/repo
```

Pass `--autoform-ref <sha>` to pin the generated workflows at an immutable
commit, `--force` to overwrite, and `--json` for machine-readable output.
An existing root `.gitignore` must be a bounded single-link regular file;
missing Autoform rules are appended through one retained descriptor. If an
append cannot be fully reverified, the error says it may be partial and the
file must be inspected before retrying. Before writing anything, even with
`--force`, `init` refuses an existing entry that differs from a path it manages
only in letter case or Unicode form, such as a `Blueprint/` Lean library or
`MkDocs.yml`: Git and Linux CI see only the exact spelling, while a
case-insensitive filesystem resolves the managed path to the variant. `init`
also refuses when it cannot list a directory on a path it manages. A root
`Readme.md` or `readme.md` is refused, not skipped like an existing `README.md`.
The vault is always `blueprint/`, so a Lean library named `Blueprint` needs a
different library name (its lakefile `lean_lib`, root module and imports), not
a lowercase rename.

Create or inspect a Lean project and list Autoform's bundled known-good release pairs:

```bash
autoform project versions
autoform project new ./FiniteFlat --package FiniteFlat
autoform project new ./FiniteFlat430 --package FiniteFlat --lean-toolchain v4.30.0
autoform project new ./Gauss --package Gauss --library atlas
autoform project inspect .
autoform project inspect path/inside/project --json
autoform project versions --json
```

`project new` requires an absent target and uses the catalog's recommended
release unless `--release` names another listed one. It builds a
complete Lean shell with the release's Lake-generated resolved dependency
manifest, blueprint, and site in a private sibling directory, validates the
staged project, then publishes the directory with an atomic no-replace rename.
It never overwrites an existing path. Failed and concurrent creations leave no
partial target, and exactly one concurrent creator can win. A failure or
interrupt after staging can leave a hidden `.autoform-new-*` directory in the
parent, and the error says so; inspect it before removing it. The published
tree has fixed modes, 0755 for directories and 0644 for files (0755 for
executables), whatever the umask.
A private creation bundle adds generated production-module roots, so package
names cannot shadow Mathlib libraries such as `Archive` or `Counterexamples`.
Its release identity is cross-checked with the public catalog and its complete
Lake manifest before any filesystem state is created.
Generated workflows are pinned as `init` pins them: `--autoform-ref` must be a
full commit SHA, an explicit `--autoform-source` carries its own ref or none,
and without flags the workflows pin the HEAD commit of the Autoform checkout
running the command, or of the marketplace checkout an installed plugin was
copied from, read with local Git. Source selection prefers Autoform's canonical
repository, then `origin`, then `upstream`, then a unique remaining safe remote,
but only when one of its cached tracking refs contains HEAD. An inferred pin is
used only when tracked files are clean and the scaffold renderer and the
bounded, link-free template files it lists match that commit in paths, bytes,
and executable-bit classification; other files in the templates folder are
ignored, and an installed copy must match the checkout too.
Local Git replacement objects are ignored. Apart from those reads, the command
runs no subprocesses, Lake, Lean, or network operations. Without a ref the local
project is complete but the workflows are omitted.
It fails closed where POSIX descriptor traversal, advisory locking, directory
sync, or atomic no-replace rename is unavailable, including on Windows.
The parent and each of its ancestors must be readable real directories, because
each caller-supplied path component is opened without following links
(`project-parent-inaccessible` or `project-path-is-symlink` otherwise). On
macOS, use the canonical `/private/tmp` path rather than the `/tmp` symlink. The
parent must not be group- or world-writable unless it is a sticky directory
owned by you or root; otherwise creation fails with
`project-parent-unsafe`, which `chmod g-w,o-w` on the parent fixes. Creations in
one parent are serialized with an advisory lock, and a lock held elsewhere for
30 seconds fails with `project-parent-busy`.
Immediately before publication and after syncing it, Autoform reopens the
requested parent without following links and rechecks its device, inode, and
owner. A pre-publication mismatch preserves the stage; a later mismatch reports
where publication was observed and tells the caller not to retry blindly.

`--lean-toolchain` takes a Lean release tag (`v4.30.0`, `v4.30.0-rc1`, or
`leanprover/lean4:v4.30.0`) and `--mathlib-rev` a Mathlib tag, branch, or
commit, defaulting to the same tag; `--mathlib-rev` requires `--lean-toolchain`,
and neither combines with `--release`. A pair equal to a catalog entry gets that
entry's bundled manifest. Any other pair is written without
`lake-manifest.json` and reported with a `project-release-unlisted` warning:
run `lake update` in the project, which needs network access, resolves and
locks Mathlib, and downloads the Mathlib build cache, then commit the manifest.
Use the toolchain that Mathlib revision declares in its own `lean-toolchain`;
otherwise Lake may rewrite the project's toolchain, warn, or fail to build.
Until the manifest exists, `project inspect` reports `indeterminate` with
`missing-lake-manifest`; afterwards, `unlisted`, or `supported` when Lake locks a
catalog commit. Autoform needs Lean v4.27.0 or
newer (the skeleton probe uses that release's `String` API and the generated
audit reads ILean `decls`); older toolchains get a `project-lean-below-minimum`
warning. With `--json`, such a pair reports `"release": null` and its warnings
go to the `warnings` array; otherwise warnings go to stderr. Either way the
exit status stays 0. Successful JSON uses schema
`autoform-project-creation/v1`.

`--library NAME` names an entry of the bundled library catalog,
`autoform_cli/project/libraries.json`. Each entry pins a shared Lean library
to a full commit on a listed release. The project is created on that release,
its `lakefile.toml` gains the library's `[[require]]` after the existing
blocks, and its `lake-manifest.json` is the release's bundled manifest plus the
library's resolved packages, so it needs no `lake update`. The first Lake
command clones the packages the manifest locks, which needs network access.
`--library` combines with no version option. An unknown name is
`project-library-unknown`, and a package name that is one of the library's
module roots or packages is `project-name-invalid`. The JSON result adds
`library: {name, git, rev}`.

`project inspect` is local and read-only. It inspects the nearest enclosing Lean
project without running Lake, Lean, Git, or the network. For automation, use
`--json`; accept only `autoform-project-inspection/v1`, then branch on `ok`,
`compatibility.status`, and `diagnostics[].code`, not message text.

Treat `ok` and compatibility independently:

- `ok` reports whether inspection found errors.
- `compatibility.status` is `supported` when the active Lean/Mathlib release
  identity matches the bundled catalog, `unlisted` when comparable but absent,
  and `indeterminate` when Autoform cannot establish that identity.

Exit 0 means `ok: true`, exit 1 an inspection or catalog error, and exit 2
invalid CLI usage. `supported` certifies release identity, not build validity.
See the [project-inspection reference](project/README.md) for resolver rules,
scope limits, JSON fields, nullability, and diagnostic codes.

`project versions` lists the bundled catalog of known-good Lean and Mathlib
pairs. These are the pairs `project new` can lock offline; it does not resolve
others, which it writes unlocked for `lake update`.

Publishing a project runs four steps in order: validate, write the Mermaid
graph into the vault, render the site source, then strict-build the site.

```bash
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform check blueprint --lean-root .
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform-visualize blueprint
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform render blueprint \
  --output site-src --lean-root . --require-declarations
uv run --with mkdocs --with mkdocs-material --with mkdocs-literate-nav \
  --with pymdown-extensions mkdocs build --strict
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform dashboard . --site-dir site
```

Drop `--require-declarations` when reviewing work in progress, where a
statement may name a Lean declaration that does not exist yet. On native
Windows, run `check` and `render` without `--lean-root`: the site then has no
code links (see the publication contract below).

`dashboard` serves the exact built MkDocs site on `127.0.0.1`, choosing an
available port unless `--port` is supplied, and adds a read-only live overlay
from current author claims. Static content remains the
same artifact deployed to GitHub Pages; only the loopback server exposes
`/__autoform/live.json`. The overlay is ephemeral, matches claims taken on
either an article's path-derived node ID or its `article_id`, reports in that
JSON the `claim_target` each lease used, and is never written into the vault,
publication manifest, or public site. Re-run render and the MkDocs build to
refresh durable content; claim badges update while the local server is running.

Validate structure, and optionally check that every `lean:` name really exists
in the project's Lean sources:

```bash
autoform check blueprint --lean-root .
autoform audit blueprint --lean-root .
```

`check` validates the graph contract. `audit` adds deterministic completeness,
provenance, coverage, checked-fact, and optional Lean-target checks. It is local
and read-only: it neither contacts network services nor writes findings back
into the blueprint. Pass `--json` for stable machine-readable output. A nonzero
exit status means the audit found at least one finding; [warnings](#warnings)
never change it. The machine-checkable
`coverage/README.md` contract contains one `Area | Coverage | Evidence` table
with `MAPPED`, `INVENTORIED`, `DECOMPOSED`, `DEFERRED`, or `OUT` dispositions.
`MAPPED` is nonterminal. `INVENTORIED` is the terminal disposition for exact
source accounting and must link to a `catalog: module` record, directly or
through a containing roadmap scope: `coverage-role-mismatch` names a link that
reaches none, and `unclassified-inventory` the first catalogs no such row
reaches. CI's `render` at an older `AUTOFORM_REF` rejects `INVENTORIED` as an
unknown disposition, so move the pin first. `DECOMPOSED` is reserved for
source-grounded declaration articles and links to them, directly or through a
containing roadmap scope. A row whose links reach nothing but catalogs is a
`coverage-role-mismatch`. Any other link that reaches no declaration leaf, such
as one to prose or to leaves marked `not_ready` that declare nothing yet, is a
`decomposed-without-declaration` [warning](#warnings), since earlier versions
accepted it. `DEFERRED` and `OUT` record an explicit later milestone or
exclusion. Audit JSON includes canonical rows, counts, and the
exact coverage source hash, while `publication.json` records aggregate counts
without duplicating the authored rows. The `counts` object always includes an
integer `INVENTORIED` key, zero when no row uses it.

One row carries one disposition, so represent both axes with distinct,
axis-qualified `Area` labels; these name source areas and are unrelated to an
article's `area:` field. For example, `Repository inventory / MathlibExt`
may be `INVENTORIED` while `Mathematical exposition / MathlibExt` remains
`MAPPED` and later becomes `DECOMPOSED`; duplicate exact area labels are
invalid. A broad inventory row may link a container and classify every catalog
below it while finer exposition rows evolve independently.

The contract is read as published Markdown and fails closed. A table inside an
HTML comment, a fenced block, or a four-space-indented block is documentation
rather than contract, and is not discovered at all. A closing fence must carry
nothing but its marker, so a table below ```` ``` trailing ```` stays inside the
code block for the checker exactly as it does for the reader.

Because hidden content ends a table for every renderer, a comment or code block
written *between* rows is reported rather than silently truncating the contract.
This holds for multi-line constructs too: a blank line inside a comment or fence
belongs to that construct rather than ending the table. Any row-shaped line
stranded below such a break is named, including malformed rows and rows written
without their outer pipes, since Python-Markdown accepts `A | OUT | reason` as a
row just as readily as the canonical form.

Whether the table publishes at all is settled by rendering the document and
looking for it, not by inspecting its two structural lines. That distinction
matters because a table's fate depends on its surroundings: a comment can break
the delimiter row while leaving its column count intact, and a paragraph running
straight into the header makes the whole thing one lazy paragraph. Both publish
nothing and both are rejected. A comment *inside* a header cell does still render,
so that contract stands. If the page publishes a contract table the audit does not
recognise -- one written without outer pipes, say -- it says so rather than
claiming there is no table.

Finding a matching header on the page is not enough to conclude it came from the
lines just read, and neither is finding matching rows. A canonical-looking table
that renders as a paragraph, sitting above an unrelated raw-HTML table with
identical rows, satisfies any comparison of values while publishing nothing
itself. So provenance is established rather than inferred: the source rows are
rendered again carrying a marker, and the published table has to be the one that
marker turns up in. The marker is grown until neither the source nor any
published cell contains it. Checking the source alone is not enough, because
rendering synthesises text the source never held literally: `&#97;utoform...`
and `autoform<span></span>...` both normalise to the same cell a reader sees. The
comparison is against published cell text, so that is what the marker has to be
absent from.

Substituting rows is only sound if it changes nothing else, which is not
something to assume: an unclosed `<style>` inside a row swallows the rest of the
document, so removing that row *exposes* tables the page never published, and one
of those can then supply the marker. So the trace has to leave the page's
topology intact -- same tables in the same order, same headers, identical rows
everywhere except the one position being traced. A row that fails this is
refused, and named as such, because the trace says nothing about a document the
substitution changed. One honest consequence: evidence that itself contains a raw
`<table>` disappears along with the row and is refused for the same reason. That
is fail-closed on a cell no contract needs, which is the right side to err on.

Only what a reader can see counts. Visibility propagates from ancestors, so a
table inside `<div hidden>` is no more published than one carrying `hidden`
itself; a hidden row drops out while its siblings remain; and a hidden cell is
treated as no column rather than an empty one, since keeping it invents a column
that both disguises a table whose visible headers match and manufactures
mismatches in one whose rows do. The boundary here is deliberate: hiding is read
from HTML, not from CSS. An element hidden by a stylesheet class or an inline
`display: none` still counts as published, because following that faithfully
would mean resolving the site's stylesheets, and a check that resolves them
badly is worse than one whose limit is written down.

Evidence must say something to a reader, judged on rendered text rather than
Markdown source. A cell holding only a code span, only a comment, only emphasis,
only an HTML tag, only an entity, or only an empty link such as `[ ](notes.md)`
is rejected: each carries word characters in the source and shows the reader
nothing. Text a browser hides is treated the same way. That check runs on an
HTML5 tree rather than on a pattern or a token stream, because what a reader ends
up seeing is decided by the repair a browser performs on malformed markup:
`<span hidden>reason` and `<span hidden />Reason` both stay hidden, since an
unclosed non-void element stays open, while `<p hidden>aside<p>Real reason` shows
its second paragraph, since that one implicitly closes the first. `title="hidden"`
hides nothing at all. So is evidence that is nothing but `TODO`, `TBD`, `pending`,
`placeholder`, or `unknown`, or that opens with one of those as a marker such as
`TODO: choose a milestone`. A status word that merely begins a sentence is fine:
"Pending Mathlib PR 1234" names something a reader can check. `INVENTORIED` and
`DECOMPOSED` evidence must contain at least one complete inline link to an
existing roadmap article. Every linked roadmap scope must contain the role the
row claims: a module inventory for `INVENTORIED`, or a declaration-bearing leaf
for `DECOMPOSED`; audit reports a row that does not, and `render` warns. Every
local link it offers must resolve, fragments included, under the same rules the
audit applies. A link missing its closing parenthesis does not render and does
not count.

Fragment checking uses the renderer rather than predicting it. Anchors come from
running Python-Markdown with the extensions the generated `mkdocs.yml` enables
and reading the heading IDs back out of its HTML. Heading IDs turn out to depend
on much more than the heading line -- whether it sits in a blockquote or a list
item, whether a raw HTML block swallows it, how `attr_list` treats an escaped
brace, what `arithmatex` leaves behind for the slugger -- and every attempt to
predict that got some cases wrong in both directions. The extension list lives in
one place in Python, and a test binds it to the shipped `mkdocs.yml` so enabling
a heading-affecting extension cannot silently invalidate the audit.

### What coverage completeness does and does not claim

`coverage.complete` in audit and `publication.json` means exactly one thing:
every row the author declared has reached a terminal disposition, so no row is
still `MAPPED`. It is a statement about the contract, not a measurement of the
project. Terminal dispositions close different questions: `INVENTORIED` closes
exact source accounting, while `DECOMPOSED` says the area has source-grounded
declaration articles. One does not imply the other.

It does **not** claim that the declared rows cover the source exhaustively, and
it says nothing about whether the linked roadmap articles are formalized or
proved. A project that declares one narrow area and disposes of it reports
`complete` while most of its source remains undeclared. Exhaustiveness is an
authoring judgement that no local check can make.

Module inventory counts are reported separately from formalization-target
completion. The target denominator, readiness count, declaration-kind totals,
and the progress-state breakdowns in the book and in `autoform check` use
non-container articles carrying `declaration`; a `catalog: module` record
counts toward none of them. A scope's status ring and item count, and the
legend beside each dependency map, also count a leaf marked `not_ready` that
declares nothing yet, so deferred work holds its scope open. All three also
count every catalog, under a neutral status: `inventory checked` once the
catalog is fully proved, which records inspection rather than a completed
result, and `inventory pending` until then, whatever its derived state. Neither
status changes a scope's rolled-up status or completion, though a target that
depends on a catalog waits for it like any prerequisite.

Publication and audit are deliberately different gates. The generated
`blueprint-pages.yml` runs `check` and `render`; it does not run `audit`. An
invalid coverage contract fails `render` before any output is written, but a
valid contract with `MAPPED` rows publishes normally even though `audit` reports
each one as a `declared-coverage-gap`. That is intended: a roadmap is published
while it is still being decomposed, and the published `coverage.complete: false`
is how a reader sees that. Coverage-role issues take the same path: `render`
prints each one as a warning and publishes, and `audit` reports it. Run
`autoform audit` in CI when you want mapped rows or role findings to block a
merge.

Extract what a reader must trust for each formalized statement:

```bash
autoform skeleton blueprint --lean-root .
autoform skeleton blueprint --lean-root . --node chapter/main-result
autoform skeleton blueprint --lean-root . --output skeleton.json --packets review-packets --passages review-passages
```

A theorem means what its statement means. The skeleton of a `lean:`
declaration is the reading list a person needs to agree that the Lean says what
the article claims: the elaborated signature, then every project declaration
the *statement* rests on, transitively, quoted from the sources in dependency
order. A definition contributes its body as well as its type, because the body
is part of its meaning; a theorem met along the way contributes only its type
to the packet. Proof bodies are never emitted. Lean's axiom analysis does
inspect proof metadata, so changing a proof can change the reported trust
context. The proof beneath a skeleton may be orders of magnitude longer, and it
is the kernel's to check, not the reader's. Each skeleton also reports the
axioms the declaration finally rests on, so a `sorry` shows up as `sorryAx`
beside the statement, and the
non-core constants it assumes from Mathlib or another dependency, listed by
name so a reader can see that a statement uses the library's notion of a limit
rather than a homemade one.

The closure is computed from elaborated terms, which is why this is the one
command that runs Lean: it writes a small probe and runs it with
`lake env lean` against the built project. Before the probe, Lake must confirm
without rebuilding that every imported module matches its exact source inputs;
a missing `lake-manifest.json`, stale artifacts, or a source tree that changes
during extraction makes the command fail. That check rehashes every input and
rewrites Lake's `.hash` files, so the project's `.lake` directory must be
writable. A lexical closure would miss what
`open`, notation, implicit instances, and auto-bound variables bring in, and
every miss silently shrinks the surface a reader is told to trust. Constructors,
projections, recursors, `noConfusion` helpers, and matchers are folded onto the
declaration the reader sees in the source, so a structure appears once, as its
`structure` block. Only companions that Lean's environment records as generated
are folded; an internal-looking name alone is not proof of provenance. Names
outside the project are the trusted base and are not expanded. Rangeless
internal-detail declarations, including helpers such as `f._unary`, `f._f`,
and `S.x._default`, are bound by their elaborated material and marked
`-- source not shown` rather than being passed off as their parent's source.
An ordinary declaration without its own source, such as one a metaprogram adds
under an existing name, is refused. A `partial def`
anywhere in a declaration's trusted closure is refused, as recorded by the Lean
environment rather than by the source text: its kernel face is an opaque
constant, so the body a reader would see is not what Lean checks. The command
exits nonzero when a `lean:` name is absent from the sources or from the built
environment, or reaches a refused declaration; that name is unresolved for its article only, other articles still extract, and it writes nothing into the vault.
A name that several indexed files declare, even when all but one of them
declare it `private`, is unresolved without a probe, because the index cannot
tell which one Lean binds; the reason names each of those files in sorted
order.
`--output` records the `autoform-skeleton/v4` report, which contains no
timestamp or absolute path, for a later render or review to consume. The
report identifies the exact blueprint, its complete target set, and whether
the extraction covered all targets or an explicit `--node` selection. It
quotes each trusted definition's source and records theorem
dependencies by elaborated signature, so it stands on its own without ever
copying a theorem proof. Declarations in one project share most of what they
trust, so the report states each trusted declaration, each external
constant's semantic material, and each boundary module's identity once, in the
top-level `trusted`, `semantics`, and `boundary_modules` tables, and each
declaration names the entries it uses; the probe's own output is shared the
same way. The probe also states each elaborated subterm of 256 bytes or more
once, since proof terms repeat large subterms heavily; the report keeps each
material's full text.

Run skeleton extraction only in a trusted checkout or an operating-system
sandbox. Lake evaluates `lakefile.lean`, and the generated probe imports project
code whose initializers, macros, and metaprograms may perform arbitrary IO and
can forge probe output. The timeout and output cap bound the direct batch
command; on POSIX, Autoform also terminates its process group. The Lake
freshness check and the probe each have their own 600-second budget;
`--timeout SECONDS` sets the probe's, which a large project may need. They are resource
controls, not a security or authenticity boundary.

Every skeleton carries a full SHA-256 **drift hash**. It is derived from
canonical elaborated expressions for the root, every trusted declaration, each
direct external assumption, and each axiom, together with the dependency edges,
Lean version, and compiled `.olean` identities (every part a module-system
build writes) for the transitive external boundary. Local source spelling and
comments do not enter the hash, while macro expansion, synthesized instance
bodies, types, and definition bodies do. Proof axioms, toolchain changes, and
unrelated edits in an external module or its imports may also rotate it.
Only modules whose `.olean` lies in the toolchain's own `lib/lean` count as
core and stay outside the boundary; a dependency module named `Lake.Foo` is
external like any other. A dependency library rooted at `Init`, `Lean`, or
`Std` hides the toolchain's copy from the probe, so extraction stops with an
error.
Compiler-generated matcher and recursor bodies stay in the hash even though
they are folded out of the human reading list.
An article with several `lean:` names has one hash over all of them, printed as
the article skeleton. If any of those names is unresolved, the report records
the article's hash and review hash as null rather than hashing the rest. It
compares reports across builds; it is not a stable statement identifier,
reviewer authentication, or an approval key.

Reports and packet manifests also carry an evidence hash over the exact packet
shown to a reviewer. It identifies those bytes but does not authenticate who
reviewed them. An article review hash additionally binds the joint packet to the
cited passage, its locator, and the drift hash, so a review recorded against it
does not survive a change of meaning that leaves the packet text unchanged. All
three are advisory provenance checksums when candidate code controls the
checkout.

`--packets DIR` writes one comment-stripped packet per skeleton, with a
manifest mapping packets to articles and hashes. The destination must be empty
or carry Autoform's packet manifest; each run replaces the complete managed
tree, so removed declarations cannot leave stale packets behind. A concurrent
change detected before commit aborts publication instead of being overwritten.
If the isolated old tree changes later, Autoform preserves it at a reported
recovery path instead of deleting it.
Packet and passage manifests use their v2 schemas; v1 output trees are still
recognized and replaced during an upgrade.

An unresolved selected declaration makes the report incomplete and prevents
all packet and passage publication; `--output` alone can still record that
diagnostic report. Output destinations must be disjoint and non-symlinked, and
their parent filesystems must support the temporary files, hard links, and
atomic renames used for guarded replacement.

A packet holds only what a blind auditor may see: the signature, the same
signature printed in Lean's raw expression form, the canonical kernel material,
the statement as written, and the source of every project definition it rests
on, with every comment and docstring the probe can identify removed, so that a
reader who is asked what the Lean literally asserts cannot read the author's
intent into it. Lean's parser, not a separate lexer, locates those comments.
The probe parses each source in its own environment, which has the notation of
every module the run imports but not the file's `local` notation.
A source it cannot parse there (a body that uses `local notation`) is withheld
if it may hold a comment. Source containing a known non-builtin token with
`--` or `/-` is also withheld unless that token was globally active through an
import, since the probe cannot reconstruct when a same-module token was declared
or where a scoped token was active. A withheld source leaves the declaration's
signatures and kernel material in the packet and does not make the article
unresolved. A `local` token containing `--` is invisible to the probe: the
packet can then show code as a comment or a comment as code.

Every item's signature is also printed raw, bypassing project notation,
unexpanders, and custom delaborators, so an `infixl " + " => HMul.hMul` cannot
make a product read as a sum in any signature. Source text is not notation-proof:
a definition's body, including a definition root's own source, is shown as
written, where such notation still applies. For those, and for structures and
generated companions, only the canonical kernel material, shown for every item,
states the meaning without notation.

Each theorem's packet also carries the statement *as written*, cut before a
`:=` value or a structure-style `where` value by Lean's parser with its
enclosing namespaces and the file's opened namespaces in scope. A proof Lean
cannot parse, for example one using `local notation`, does not stop those cuts.
The statement sits beside the elaborated signature: the printed form shows
binders that `variable` and `include` inject and the type every cast lands in;
the written form shows what the pretty-printer elides. Equation-style forms
that cannot be parsed outside their file are marked `-- source not shown`.

A statement's source passage can travel with it. A `## Sources` link to a
non-Markdown file inside the blueprint with a `#L<start>-L<end>` fragment, for
example `../../../sources/lebl-ra/ch-real-nums.tex#L693-L714`, names the exact
text the statement came from. External URLs are skipped. A first local locator
that names no text, because it points outside the blueprint, names an invalid
path, a symlink loop, a missing file or non-UTF-8 text, or names no lines of
it, leaves the article's declarations unresolved. `--passages DIR` writes those
passages beside the packets, one per article, in a separate, disjoint managed
directory. It requires `--packets`. Each article directory also holds
`article.lean`, the joint packet of every declaration the article
names, because a source theorem is often formalized by several declarations
together and each alone is honestly incomplete. A judge of faithfulness is
given the article packet and its passage; an auditor asked what one
declaration asserts is given that declaration's packet alone.

When `--output`, `--packets`, and `--passages` are combined, all three outputs
are staged before publication and a failed commit restores the previous set.
This is failure atomicity, not simultaneous visibility across paths: each
rename is atomic, but a reader opening several outputs during publication can
briefly observe different generations.

Inspect the current Markdown-derived formalization frontier without creating a
queue or scheduler state:

```bash
autoform work list . --lean-root .
autoform work list . --lean-root . --json
autoform work context chapter/result . --lean-root . --json
```

`work list` returns only formalizable leaves whose next statement or proof phase
is unblocked, by the same policy-dependent rule as the derived `can_state` and
`can_prove` states, the runtime projection, and the site's Next up card. Under
the strict policy project CI rejects `sorry`, so a theorem's statement lands
with its proof, and an article's statement phase also waits until its `## Proof
depends on` prerequisites are proved. Under `open_statements: allowed` a
theorem's statement phase waits only for the statement prerequisites to be
stated, a definition's also for its proof prerequisites, which its body uses,
and the proof phase for every prerequisite to be stated. `work
context` accepts the path-derived node ID (see Articles and containment) or an
assigned `article_id` and reports the exact article, dependencies, source
targets (except those the Runtime contract leaves out), Lean targets, blockers,
article and graph source revisions, and claim
target. The article revision hashes that article's bytes alone. A Lean target's
`source_file` is relative to `--lean-root`, and null without one or when the
local scan does not find the declaration. The scan skips build output and nested
checkouts, meaning any subdirectory with a `.git` entry, such as a worker's
worktree or a submodule. Blockers are unmet dependency IDs or one of
`roadmap:not-a-formalizable-leaf`, `roadmap:missing-article-id`,
`roadmap:missing-article-revision`, and `roadmap:not-ready`. The claim target
prefers durable `article_id` metadata. `work list` fails explicitly if an
unfinished formalizable leaf lacks one; add the missing IDs with `autoform
migrate article-ids --write`. `work context` may still
select that article by its path ID to
report the migration blocker. An item whose article records `statement:
retracted` is a revision: it carries `revision` true in JSON, and the text of
`work list` adds a `revision:` line and `work context` a `Revision:` line saying
to start from `autoform work impact` (see the [revision
contract](#revision-contract)). Both commands are read-only projections of
Markdown.

Under the open policy the text output of `work list` starts with an `Open
statements: allowed` line and adds an `assumes:` line under each item that rests
on open statements; `work context` prints `Open statements:` and `Assumes:`
lines. In JSON, `work list` and `work context` use `autoform-work/v2`: the
frontier and every item carry `open_statements`, and every item carries
`assumes`, the open statements its proof rests on (empty under the strict
policy), plus `revision` for an explicit retraction. The runtime projection
carries the same `open_statements` flag, and each node's runtime status adds
`assumes` and `waiting_on`, the prerequisites that keep an unproved node from
its next phase.

List the open statements and the articles that rest on them:

```bash
autoform work assumptions .
autoform work assumptions blueprint --json
```

`work assumptions` prints the policy, one `open:` line per open statement with
its declarations and the open statements it assumes, if any, one
`conditional:` line per conditional article, and one `unproved:` line per other
listed article that assumes open statements, such as a retracted definition
whose body reaches one.
`--json` writes the `autoform-assumptions/v1` contract that CI audits the build
against: every article whose `lean:` names a declaration, stated or not, with
`open`, `assumes`, and `allowed_open_declarations`, the declarations of the
open statements its Lean may reach, plus its own when it is open. A `mathlib:
true` article is listed with state `mathlib`, `open` false, and nothing assumed
or allowed, so CI checks that its names exist and reach no open statement. Under
the strict policy every such article is listed with `open` false and nothing
allowed, and CI checks that its names exist. It reads Markdown only and needs
no Lean build.

Ask what revising an article's Lean declarations would affect before editing
them:

```bash
autoform work impact chapter/result . --lean-root .
autoform work impact chapter/result . --lean-root . --declaration MyProject.helper --json
```

`work impact` runs a bounded Lean probe against a fresh build and reports
statement-impacted and proof-impacted articles, unnamed helpers, missing
Markdown dependency paths, deprecated declarations and their users, whether
the change is contained, and the complete `claim_targets` set. Helpers shared
by several articles contribute every owner's claim; an unowned helper gets a
stable `lean/<slug>-<digest>` target. A revised declaration that no article
names is claimed the same way, under every nearest owner's target or its own
stable key. `unused_statement_dependencies` lists the stated articles whose
Markdown statement rests on the revised declarations, directly or through other
statement dependencies, that are not statement-impacted: the probe follows
names, so a dependent whose Lean inlines a revised definition's body shows no
use although its statement changes meaning. They join `claim_targets` too. A
`mathlib: true` article is left out: its statement is a Mathlib declaration,
which cannot use the revised one, and it cannot record `statement: retracted`.
A revision is contained exactly when the selected article's target is its only
claim target. A Markdown path reaches the revised declarations when it ends at
the revised article, at an article that names one of them, or, when none names
it, at an owner; missing dependency paths are counted the same way. Project
locality is an exact inventory of regular repository source modules, never a
namespace-prefix guess. The inventory skips nested Git checkouts, ignored
directories such as `build`, and symlinked directories, so the probe refuses a
loaded root-package module whose source the inventory lacks, and a module an
`M.*` or `M.+` glob finds that it did not load, which Lake builds even when
nothing imports it, instead of silently leaving out what such a module uses.

The command snapshots and rereads the roadmap around the probe. It retains one
bound Lean source generation, derives module inventory and locations from that
generation's captured bytes, then recaptures through the same binding and
refuses any content or source-identity change. JSON uses `autoform-impact/v1`
and binds its answer to the Markdown `source_revision`, the repository
`lean_source_revision`, and a `build_revision` hash of the normalized Lean
records. The probe imports project modules, so run it only in a trusted checkout
or sandbox. It compares elaborated types and values; changes to notation,
attributes, instance priority, or unreported generated declarations still
require human review.

Report the coordination and review scope of an import edit without forbidding
the edit:

```bash
autoform work import-impact SELECTOR MODULE [TARGET] --lean-root PATH [--json] [--timeout SECONDS]
```

`import-impact` is advisory and does not edit authored roadmap or Lean source;
its Lake freshness check may refresh build hash sidecars. `SELECTOR` identifies
the working article and `MODULE` the module whose effective header would change.
Lean supplies the loaded module graph; Autoform binds every node to its captured
root source and configured root-package artifact so dependencies cannot shadow
it. The probe imports project modules, so run it only in a trusted checkout or
sandbox. Like `work impact`, it refuses a root-package module the captured
inventory lacks, so `affected_modules` is complete over the root libraries'
loaded Lake roots/globs, not dormant source files outside that build boundary.
It holds `MODULE` and every loaded module that imports it, directly or
transitively, which covers what an added import can affect. Removing an import
can also drop modules from the build: roots and single-module globs bring in
only what they import, transitively, so a module only the removed import
reached is no longer built. The report does not list such modules.

JSON uses `autoform-module-import-impact/v1` and binds the graph with
`module_graph_revision`; `module_scope` is `loaded-root-library-graph`.
The import edit's `claim_targets` contain only the selected article and a stable
`module_claim_target` resource, `lean-module/<lowercase-slug>-<sha256[:16]>`.
`affected_claim_targets` are downstream warnings, not pre-emptive ownership
requirements; `claim_keys` and `affected_claim_keys` match the JSON from
`autoform claim list --worker-id WORKER`. `unresolved_articles` reports local
declarations absent from the loaded environment without globally blocking
unrelated edits, while every `affected_unowned_helpers` entry includes its own
claim target and key.

`article` is the selected article (`id`, `article_id`, `claim_target`), and
`module_claim_key` is the claim key of `module_claim_target`.
`affected_articles` lists each article that names, or owns through the parent
chain, a declaration in an affected module, with those `modules`;
`selected_affected` says whether the selected article is one of them.
`article_local` holds only when it is, `affected_claim_targets` is empty, and
no other article has unresolved declarations, like `contained` in
`work impact`.

Imports are allowed regardless of reported breadth. Follow the
[Formalize import workflow](../skills/formalize/SKILL.md#import-edits) for
baseline evidence, conflict checks, claim lifecycle, and post-build review.

Ask whether the blueprint already holds a result before adding an article or
a Lean helper:

```bash
autoform search . "separating hyperplane" --lean-root .
autoform search . "non-ambiguous" --lean-root . --json --limit 20
autoform search blueprint "interlacing" --state proved --state fully_proved
autoform library list .
autoform library index . --lib MathlibExt
```

`search` takes the project or blueprint directory, then one quoted query; both
are required. A query that starts with `-` goes last, after `--`. It exits 0
with or without hits. See the [search contract](#search-contract).

Plan durable article identity metadata, and add it:

```bash
autoform migrate article-ids blueprint --json
autoform migrate article-ids blueprint --check
autoform migrate article-ids blueprint --write
```

`article_id` accepts opaque values in the form `af_` plus 24 lowercase hex
digits. The planner validates uniqueness, proposes deterministic IDs for
missing articles, and includes exact source hashes; without `--write` it is
strictly read-only. `--write` adds each planned ID as the first frontmatter
line of the article that lacks one, or as a frontmatter block of its own, and
changes nothing else. It writes an article only while its bytes still have
the planned hash, checks every article before writing the first, and refuses
an article that changed or became a symlink after planning; after such a
refusal, running it again completes the plan. The runtime and
`autoform work` expose assigned IDs immediately; preserving publication routes
across path moves remains a follow-up change.

Coordinate temporary cross-machine ownership without modifying the book:

```bash
export AUTOFORM_WORKER_ID="agent-name"
autoform claim acquire af_5b0e4d3c2a1f09e8d7c6b5a4
autoform claim renew af_5b0e4d3c2a1f09e8d7c6b5a4
autoform claim release af_5b0e4d3c2a1f09e8d7c6b5a4
autoform claim acquire af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
```

Claims are optional: use them when other people or sessions may work on the same
project at the same time, or when the user asks. A single Formalize run needs
none, because its lead keeps its workers apart. A claim excludes only workers
that also claim, so on a shared project every worker uses them. Claim an article
by the `claim_target` that `work context` reports. The board hashes whatever
string it is given, so a claim on an article's path ID and one on its
`article_id` do not exclude each other. Each concurrent agent needs its own
worker ID, because a second acquire by the same owner succeeds. Where shell
state does not persist between commands, as in agent tool calls, pass it with
`--worker-id` on every command instead of exporting `AUTOFORM_WORKER_ID` once.
Leases expire after 1500 seconds unless `--ttl` sets another length. Renew well
within that, and confirm a claim is still held with `renew`, not `acquire`,
which also succeeds once a lease has expired or been released. Several targets
in one command change all-or-nothing; see the [claim contract](#claim-contract).

Pass several targets to `acquire`, `renew`, or `release` when one change needs
shared ownership. The board reads the set once and sends one atomic push with a
lease for every ref, so either every target changes or none does. A remote that
cannot push atomically is refused, and duplicate or malformed targets fail
before any push. To avoid deadlock, acquire the complete set in one command;
never hold a partial set while waiting for another target.

Claims are fail-closed compare-and-swap leases under
`refs/autoform-claims/` on the Git `origin`; pass `--repo` for another claim
board. A failed acquire or renew means the caller cannot prove ownership and
must stop before committing or pushing protected work. Claims do not prove
mathematical correctness and do not replace branch-level Git CAS.

Write the Mermaid dependency graph into the vault, where Obsidian renders it:

```bash
autoform-visualize blueprint
```

Build the publishable site source — a book overview, aggregate progress,
statement boxes with collapsed dependency details, a multi-scale dependency
explorer, and direct links to Lean declarations at the current commit:

```bash
autoform render blueprint --output site-src --lean-root . --require-declarations
```

`render` never writes into the vault. It leads the landing page with the project
explorer, once the roadmap has articles to draw, over a summary of what is
formalized and what is unblocked, places a compact
progress summary after each chapter's opening prose, writes `structure.md` so a
vault's layout can be checked against the book it produces, and shows a source
icon when a `lean:` declaration resolves to a repository permalink; a catalog
instead links its first eight names in its Lean row and counts the rest. Its
`dependencies.md` opens a full-viewport application and rolls dependencies
through the article hierarchy, with bounded project, chapter, and nested-scope
projections. Global search loads a layout-free index and opens a result's direct
prerequisite → result → dependent neighborhood in the same mounted explorer,
without walking its ancestor scopes. Topic, outline, and related-result links
also navigate in place; Back/Forward restore context and camera. That index,
`dependencies/index.json`, is internal to the bundled
viewer, and its schema may change. The compatibility `dependencies/full.md`
route renders only the project shell and never fits the whole repository into
one canvas. Every generated site projection uses the same deterministic
JSON-derived Canvas and semantic-DOM explorer with search, filters, pan/zoom,
and hash-routed node neighborhoods, so none inherits Mermaid's text, edge, or
SVG-size ceilings. Authored Mermaid remains supported in the vault and book.
A persistent hierarchy rail keeps the current ancestry and immediate contents
visible. The project overview groups readable topic cards by authored
mathematical area and shows authored dependency arrows immediately. Hover and
selection emphasize those relationships; they do not gate their visibility.
Its detail drawer overlays the map instead of shrinking it.
The viewer's JavaScript and stylesheet are shared assets served once per site.
Dragging a topic card pans the map; clicking it opens its mathematical context.
Breadcrumbs return through the explorer hierarchy to the book (to the home page
for a roadmap with neither a root article nor root leaves), and every formal
statement links to the smallest explorer scope containing that item. Point
`mkdocs.yml` at `docs_dir: site-src` and enable `md_in_html` plus a
`pymdownx.superfences` mermaid fence; see the [repository
example](../skills/setup/assets/cabannes-thesis-project/mkdocs.yml).

This shared-explorer layout replaces the `dependencies/nodes/*.html` focus
pages of earlier sites: a statement's dependency link is now
`<scope>.html#node=<id>`, and an old focus-page link no longer resolves. Only a
legacy `dependencies/full.html#node=<id>` link resolves, through the global
index, to that result's bounded connected neighborhood. New focused links use
`<scope>.html#node=<id>&focus=1`; both forms remain reloadable static routes.
The layout-free search index carries optional `data_url` and `article_url`
links plus `relations` rows `[source_index, target_index, statement_count,
proof_count]` referencing its node table. These derive only from the authored
graph, not containment or imports. A focused map initially draws at most eight
neighbors on each side; all direct relations remain accessible in its complete
connection list. The [publication
contract](#publication-contract) gives the schema and when render replaces an
earlier site.

### Upgrading from an earlier Autoform

What an existing repository meets when it moves to this version:

1. CI at an older `AUTOFORM_REF` rejects `catalog:` and `area:` as unsupported
   frontmatter keys and `INVENTORIED` as an unknown coverage disposition, so
   move the pin before using them. Older refs ignore `blueprint/atlas.json`.
2. A `DECOMPOSED` link that reaches only prose or leaves marked `not_ready`
   that declare nothing yet is now a `decomposed-without-declaration` audit
   warning, which `render` also prints; neither fails.
3. Coverage JSON is `autoform-coverage/v2`: `INVENTORIED` is a new terminal
   disposition, and `counts` always holds an `INVENTORIED` key. `audit`'s
   coverage line always shows an inventoried count.
4. `publication.json` is `autoform-publication/v3`. The first render replaces a
   v2 site and refuses a v1 one, and the dashboard reports an older site as
   stale until then; the [publication contract](#publication-contract) gives
   the rules.
5. The runtime is `autoform-runtime/v4`, which gives each node a nullable
   `catalog`.
6. `audit --json` carries a `warnings` list beside `findings`, and the text
   output adds `warning:` lines; neither changes the exit status.
7. `check`'s status line counts only leaves that carry `declaration`, so
   containers, prose leaves and `not_ready` leaves that declare nothing yet
   drop out of it, and with `--lean-root` it counts the distinct `lean:` names
   it resolved rather than the articles naming them. A `blueprint/atlas.json`
   that is not `autoform-atlas/v1`, which earlier versions ignored, now draws a
   warning from `check`, `render` and `audit`; it fails nothing.
8. Old `dependencies/nodes/*.html` links no longer resolve: only
   `dependencies/full.html#node=<id>` redirects, and a statement's dependency
   link is now `<scope>.html#node=<id>`.
9. Setup leaves an existing `mkdocs.yml` alone. A large site can add Material's
   `navigation.prune` feature by hand, as the scaffolded configuration does.

## Validation

`autoform check` rejects cycles between authored articles or introduced at any
rolled-up containment level, missing targets, escaping paths, symbolic links
anywhere under `roadmap/`, and self-dependencies,
missing or multiple H1 titles, unsupported frontmatter keys, assertion values
it does not recognize, and assertions the Lean cannot back: `proof: formalized`
without `statement: formalized`, or either without a `lean:` name, even beside
`mathlib: true`. A `## Sources` link to a local path must name a file
inside the blueprint, as `audit` also requires: a missing file, a directory, an
invalid path, or a target outside the blueprint fails. So does a target whose
path as written, once its `..` steps are applied, has a part below the
blueprint starting with `.`, unless the article citing it is hidden too,
because render omits hidden files and directories. Under `sources/`, such a
target passes `check` with a warning instead, and `audit` still reports it:
only a repository link reaches the note, and render writes one when it links
the other source notes there, so the note must be committed. `audit` alone checks
fragments and fails links with other schemes or a network location. Such a link
passes `check` with a warning, because work and search leave it out; so does a
`mathlib_file` that is not a portable relative path, which search reports as
null. A byte-order mark at the start of an article hides whatever opens its
first line: frontmatter there is read as text, so its keys are ignored, and
`check` warns about the mark when the article otherwise loads; an H1 title
there is missed, and `check` fails the article as missing one.
A `declaration` on a container article passes `check` with a warning, because
nothing counts or schedules a container's own declaration; `audit` reports it
as `formalizable-container`.
With `--lean-root`, `check`
also fails on a `lean:` name
absent from the sources, as `leanblueprint checkdecls` does for LaTeX
blueprints. It validates structure and leaves mathematical correctness to the
agent and the Lean kernel.

Contracting that acyclic article graph into chapters or nested scopes can put
an edge in both directions between two containers. Generated views retain and
draw both relations: an authored dependency is not deleted merely to make a
coarser projection acyclic.

Source-aware `--lean-root` inspection requires directory-descriptor traversal.
Platforms without that capability, including Windows, fail closed instead of
treating repeated pathname reads as one filesystem generation. Declaration
locations and source revisions come from the same retained capture. Recognized
skeleton packet/passages directories are identified by their bounded managed
manifest before descendants are read; publication-output policy remains with
the publication feature rather than this source layer.

Automatically detected Git permalinks are emitted only for captured files whose
bytes equal the blob at the stable detected commit. Dirty, untracked, missing,
or concurrently checked-out files keep local declaration locations but receive
no URL. An explicitly supplied ref remains a caller attestation.

Lean indexing reads the whole Lean root or nothing. It stops with an error,
rather than indexing what it could, when an entry it must read cannot be read
safely: a `.lean` file that cannot be read, a folder that cannot be listed or
searched, a `.lean` entry that is a symbolic link or a special file (editor
lock links named `.#*.lean` are skipped), or an unreadable `manifest.json`
below the root, which is read to recognize skeleton output folders, or an
unreadable `publication.json` below the root, which is read to recognize
rendered sites. These stops apply in every folder the walk
enters, so an unreadable folder of unrelated files inside the Lean root stops
indexing too; other files are never read. `render` without `--lean-root` uses
the blueprint's parent folder as the Lean root. To clear a stop, fix the
permissions, or move the entry out of the Lean root or into a folder the walk
skips: `.direnv`, `.git`, `.lake`, `.obsidian`, `.trash`, `.venv`,
`build`, a folder below the root that holds a `.git` entry, a
recognized skeleton output folder, or a folder below the root that holds a
complete `publication.json`, such as a rendered site or the `mkdocs` copy of
one. Every command skips these folders, so `render` and the other commands
index the same files. `render` marks the site it is writing before it copies
any file into it, so a command that indexes during a render skips it too.
`mkdocs build` empties `site/` and then copies files in name order, so `site/`
can hold copies before its `publication.json`; a command that indexes during
the build can read them, so run it again once the build is done. There is no
command-line option to exclude other folders. The skipped folder names and the
`manifest.json`, `publication.json` and `.git` names match in any letter case,
except `build`, which must be lowercase. The `.lean` suffix must match
exactly, so `A.LEAN` is not indexed.

A capture rechecks only what the index uses: each captured `.lean` file must
keep its metadata, and each folder that holds `.lean` entries must keep its
device, inode, file type and the names of the `.lean` entries and folders it
holds for the index. Any other folder, such as a generated site, `mkdocs`
output, skeleton output or a nested Git checkout, may change, vanish or be
rebuilt during a capture, and files other than `.lean` sources may change
anywhere. Whatever is at such a path when it is rechecked must be a folder the
walk skips or hold no `.lean` entry, so a folder that loses its marker and
exposes `.lean` files is a change. A capture that sees Lean inputs change is
retried; when every attempt changes, the error names the changed paths it can
attribute, relative to the Lean root, for example `Lean sources kept changing
while they were indexed; changed: Proj/A.lean`. A change seen only as a failed
lookup, such as a file removed just before it is rechecked, carries no path.
A capture is not one atomic filesystem generation: a `.lean` file or marker
that appears at a path after the capture last looked there, or in a folder
after the final check listed it, is not seen, so coordinated moves across
folders can yield a set of sources that no single state had.

Within each file, up to an `#exit` line, the index records the declarations
written with `theorem`,
`lemma`, `def`, `abbrev`, `instance`, `structure`, `class`, `inductive`,
`opaque`, `axiom`, `irreducible_def`, Batteries' wanted commands
(`Batteries.Util.ProofWanted`) `theorem_wanted`, `proof_wanted`, `def_wanted`,
and `instance_wanted`, and Batteries' `alias`. The command may follow `@[...]`
attribute lists and the modifiers `public`, `private`, `protected`,
`noncomputable`, `partial`, `unsafe`, `scoped`, and `local`; a declaration
behind any other modifier, such as `meta` or `nonrec`, is not indexed, and
neither is an unnamed `instance` or `instance_wanted`. A name written
`_root_.X` is recorded as `X`, whatever namespace it is declared in. A wanted
command states a result whose proof or construction is deferred. `alias b :=
a` records `b`, and `alias ⟨mp, mpr⟩ := a_iff` records each of its two names
that is not `_`; the `:=` must be on the alias's line, and the target on that
line or the next nonblank one.

The Markdown files are the source of truth. Graphs and sites are derived views
that may be regenerated at any time.

## Audit contract

`autoform audit` reports structured findings at blueprint-relative paths. It
checks that no article leaves a code fence open, that formalizable articles are
declaration-sized leaves with statement text and an explicit dependency
section, that asserted Mathlib facts are
internally consistent, and that cited work resolves to local source
material without escaping the blueprint. Coverage files are checked for broken
links and explicitly declared gaps. With `--lean-root`, local declaration names
and declaration kinds are checked against the Lean source index, along with the
elaboration limits each target runs under.

An alias has its target's kind, size, and elaboration limits when the index
can be sure what the target is. The audit tries the names Lean tries, in the
alias's namespace, then in each enclosing one, then at the root, passing over
a declaration below the alias in its file, which Lean has not seen yet. The
first other name must be a declaration above the alias in the same file. A
name the index does not hold may be declared by another library such as
Mathlib, one in another file may not be imported, and a `protected` one for a
one-component target is skipped by Lean unless it is also private, written or
under the module system; such an alias matches any intent and is measured, like
any other declaration, by its own span and limits. Each name of
`alias ⟨mp, mpr⟩`, and any alias of one, is a theorem whatever its target is.
Batteries makes an alias of anything but a theorem a `def`, but the audit checks
what an article's object is, and an alias names the same object as its target.

A wanted target matches the intent its command mirrors: `theorem_wanted` and
`proof_wanted` a `theorem`, `lemma`, `proposition`, or `corollary`,
`def_wanted` a `def` or `definition`, and `instance_wanted` an `instance`. A
theorem's wanted target can back `statement: formalized`, but no wanted target
backs a proved article: `lean-target-wanted` reports one named, directly or
through an alias, by an article that records `proof: formalized`, or by a
definition that records `statement: formalized`, since a stated definition
counts as proved. A wanted command
without a body elaborates to a placeholder that carries no `sorry`, so no
`sorry` check catches one. The kernel-trust audit of the generated verify
workflow does not resolve wanted targets yet: Batteries declares every
placeholder `private`, so that audit reports a `lean:` name naming one as not a
declaration of the Lean build, as it does for any `private` declaration.

### Warnings

A finding is a problem the audit fails on. A warning never fails the audit:
`clean` and the exit status depend on findings alone, so a repository that
passes keeps passing when a warning appears. The text output prints a
`warning: <path>: <code>: <message>` line for each, after the error lines.
`--json` lists them after `findings` as `warnings`, sorted by `article_path`
and `code`. Each also carries a `message` and `declarations`, one object per
affected declaration and setting with its `name`, `option`, `value`,
`location` (the `set_option` as `path:line` relative to the Lean root), and
`scope` (`file`, `section`, `declaration`, or `proof`), sorted by name. The
catalog limit warnings under [Elaboration limits](#elaboration-limits) fill
`declarations`. Two warnings leave it empty: `atlas-omission`, at `atlas.json`,
names what loading ignored in that file, and `decomposed-without-declaration`,
at `coverage/README.md`, names a `DECOMPOSED` row's links that reach no
declaration leaf.

### Structure

Containment is inferred from nested `README.md` articles, so a chapter
directory without one is invisible to the hierarchy: its pages attach to the
roadmap root and the book loses a level. `missing-chapter-article` reports a
directory directly under `roadmap/` that holds articles but names no chapter.
Deeper directories (the `definitions/` and `theorems/` buckets the bundled
example uses) are a filing convention inside a chapter and are not checked.
`overfull-container` reports a mathematical article with more than 24 direct
children, which is a table of contents rather than a chapter. A roadmap root
whose direct children are all chapter pages is a repository-wide subject index
and may hold 64; one root-level leaf returns the root to the 24-article limit.
Both defects leave a valid graph, which is why they need their own checks
rather than falling out of `autoform check`.

### Node size

`node-too-large` is retrospective and needs `--lean-root`: it measures the
source span of a node's resolved `lean:` declarations, from each declaration's
first line to the line before the next one. A node is reported only once it
clears both 200 lines and four times this project's own median, so a project
whose units are uniformly long is measured against itself rather than gated on
an imported norm, and a project with too few finished nodes to have a
meaningful median cannot clear the multiple at all. Every measurement appears
in the finding's reason, so `--json` over a finished project is also the
calibration corpus for the threshold.

Nothing authored in an article predicts this. On the 43 finished nodes of
[`phulin/finite-flat`](https://github.com/phulin/finite-flat), prose length
correlates with realized Lean length at r = -0.03 and prerequisite count at
r = 0.25; its largest node is 1344 lines of Lean behind 66 words of prose and a
single declaration name. Pre-formalization size estimates were considered and
rejected on that evidence.

### Elaboration limits

With `--lean-root`, the audit reads the `maxHeartbeats`,
`synthInstance.maxHeartbeats`, and `maxRecDepth` settings each resolved `lean:`
target is elaborated under, including a catalog's. For a declaration article,
`lean-target-unbounded-limit` reports a target with no heartbeat limit: a
`set_option maxHeartbeats 0` or
`set_option synthInstance.maxHeartbeats 0` in force at the declaration or inside
its proof, which lets elaboration run indefinitely instead of failing.
`lean-target-unscoped-limit` reports a target whose raised limit comes from a
file- or section-wide `set_option`, which also covers every later declaration
whether or not it needs one; Mathlib's `linter.style.setOption` rejects
unscoped heartbeat limits for the same reason. A raised limit put on the
declaration itself with `set_option ... in` is accepted Mathlib practice and is
not reported. The pass is lexical: it follows `namespace`, `mutual`, `end`, and
`section` nesting, including `noncomputable section` and the module system's
`@[expose] public section`, and treats an indented `set_option` as part of the
proof above it.

A catalog inventories code that already exists, so its declarations get the
same two checks as [warnings](#warnings), at most one per catalog and code:
`catalog-unbounded-limit` and `catalog-unscoped-limit`. As for a declaration
article, an alias is read as the declaration it reaches. The message counts the
affected declarations against all those the catalog's resolved names reach (an
alias listed beside its target counts once), names the options involved, and
locates the `set_option` of the first by name, for
example `2 of 40 catalogued declarations have no maxHeartbeats limit
(Inv.alpha at Inv.lean:1 and 1 more); existing code: change it only on request,
and a finite limit must pass a rebuild`. A name that does not resolve is a
`lean-target-not-found` finding and is not counted.

ATLAS v1, built with Autoform, shows what accumulates without this check: 901
raised `maxHeartbeats` settings, 497 of them file- or section-wide and 9 of them
`0` ([#260](https://github.com/facebookresearch/autoform-bot/issues/260)).

The audit API also accepts an already compiled graph. Formalize may use its
findings while working the Markdown frontier, but the audit itself never
enqueues work, stamps articles, or creates another graph artifact.

### Statement text

A formalizable article must state its result in prose a reader sees. At most
one of three findings reports a statement that does not, checked in this order:

- `missing-statement-text`: the statement has no prose outside code blocks, HTML
  comments, and lines that hold only a heading written with `#` marks, such as
  `### Setting`.
- `empty-statement-text`: the published prose has no letter or digit, as with a
  hidden element, a horizontal rule, an image, a link with no text, or a heading
  that is underlined, quoted, or in a list item.
- `placeholder-statement-text`: every word is one of `pending`, `placeholder`,
  `todo`, `tbd`, `unknown`, or the statement opens with one of them followed by
  a colon or a dash, as in `TODO: state it`. A single hyphen or en dash counts
  only with a space after it, so `Unknown-variance` is a word.

The prose is read as the site publishes it, without headings, code blocks, and
diagrams; inline code and mathematics count. A status word that begins a
sentence passes ("Pending Mathlib PR 1234 ..."), and so does a marker written
without punctuation; both are left to review. A real statement that opens
`Unknown: ...` or `Pending: ...` is reported and needs rewording, and so does
one written in symbols alone, such as `⊥ ≠ ⊤.`, which has no letter or digit.
A reference-style link is read
without its definition when that sits in a later section. Coverage evidence is
held to the same placeholder rule.

## Search contract

`autoform search` is a read-only projection of Markdown: it writes nothing,
builds no index, and starts no process. The query splits on whitespace into
terms. Words that carry no meaning (`a`, `an`, `and`, `are`, `as`, `at`, `be`,
`by`, `can`, `do`, `does`, `for`, `from`, `has`, `have`, `if`, `in`, `is`,
`it`, `its`, `let`, `of`, `on`, `or`, `such`, `that`, `the`, `then`, `there`,
`these`, `this`, `to`, `we`, `when`, `where`, `which`, `whose`, `with`) are
dropped unless the query has no other word; `not`, `every`, and `exists` are
kept. A term sheds surrounding sentence punctuation, quotation marks,
backticks, `$`, emphasis marks that wrap it on both sides, and parentheses
around it, in any combination, but keeps a parenthesis that closes one inside
it, as in `f(x)`, and keeps square brackets. A word of letters only also sheds a
final `ing`, `ed`, `es`, or `s`, with the `i` of `-ies` and a doubled last
letter, when four letters remain and it does not end in `ss`, `us`, or `is`,
and a final `y` after a consonant when five remain: `recovered`, `topologies`,
and `topology` are searched as `recover`, `topolog`, and `topolog`. Only the
query is shortened, so a form that does not begin with the shortened word
finds nothing: `indices` does not find `index`, `mapping` `map`, `sets` `set`,
`bodies` `body`, nor `define` `defining`. A name with a dot, underscore, or
digit is searched exactly. `terms` in the JSON
shows what was searched. An article is a hit
when every term is a substring of one of its fields; terms may sit in
different fields. Both sides are folded first: case,
width, accents and other combining marks on letters, typographic dashes and
quotes, and invisible characters do not matter. Fields, best
first: `title`; `lean` (the last component of each `lean:` and
`mathlib_declaration` name, and a term that is the whole name or its last
components, as `Convex.separation` is of `Project.Convex.separation`, with or
without `_root_.` and `«»`; a prime, `?`, or `!` ending a name is not compared,
so `Project.foo` is also a `lean` match for `Project.foo'`); `node_id` (the
last component of the path ID);
`statement_text`; `ancestors` (titles of containing articles below the roadmap
root); `qualified_names` (the path ID and Lean names in full, which is where a
namespace alone matches). A title is
matched without its Markdown markup. The statement is
matched as the site publishes it, without headings, code blocks, diagrams,
comments, and elements carrying `hidden`; inline code and mathematics count,
and published mathematics is delimited `\(...\)`, so search for a formula's
content. Other frontmatter and the sections after the statement are not
searched. There is no phrase search and no synonym or prefix handling: `map`
matches `roadmap`, `unambiguous` does not match `non-ambiguous`, and one
absent term means no hit, so query with a few distinctive words or one exact
Lean name, and retry with fewer words before treating a result as new.
A symbol such as `ℝ` folds to its plain letter, and a one-letter term matches
nearly every article. A superscript or subscript digit folds to the digit while
`^` and `_` stay, so `S^1` and `S¹`, or `H_0` and `H₀`, do not find each other;
search for each spelling.

Hits sort by the best field any term matched. Among hits with the same best
field, those that contain every word as typed, in any field, come before those
reached only through a shortened word; then by the weakest field any term
needed, then more dependents first, then node ID.

`--state` takes exact derived state keys, which `autoform search --help` lists
(`proved` does not include `fully_proved`), and `--declaration` frontmatter
`declaration` kinds, each repeatable; they apply before `total_matches` is
counted and `--limit` (default 20) after. `--declaration def` also keeps
`noncomputable def`, but not `definition`. A kind that no article in the
blueprint declares is refused with exit code 2 and the kinds in use, rather
than answered with no hits.

`--json` writes `autoform-search/v1`: `schema`, `source_revision`,
`open_statements`, `query`, `terms`, `filters` (`declaration` and `state`,
each a list), `limit`, `total_matches`, and
`hits`, with no timestamp and no path outside the project. A hit carries
`node_id`, `article_id`, `article_path`, `article_revision`, `title`,
`declaration`, `catalog` (`module` for a module inventory), `state`,
`statement_text` (the authored Markdown),
`lean_targets` (all of them), `mathlib`, `mathlib_declarations`,
`mathlib_file`, `source_targets` (as the article writes them, once each, minus
any that is neither an http(s) link nor a path inside the blueprint), `used_by`,
`used_by_count`, `shared_title`, and `matched_fields`; `article_id`,
`declaration`, `catalog`, and `mathlib_file` may be null. A Lean target has
`declaration`, `source_file` (relative to `--lean-root`), and `line`; the last
two are null without `--lean-root` or when the lexical scan does not find the
name.
`used_by` holds the first ten dependents by node ID, through statement or
proof; `used_by_count` counts all. `shared_title` marks a title another article
repeats and is advisory. `matched_fields` lists, best first and once each, the
fields in which some term found its best match; it does not say which term.
No hit is `"hits":[]` with exit 0. Human output shortens the statement, omits
sources and dependent names, and is not a contract.

Search rereads each article it matches against and refuses, with exit 2, bytes
other than those the graph was built from, as it refuses a symlinked roadmap
entry. Each call reads every such article and renders its statement, so its
cost grows with the blueprint.

### Searching a library the project depends on

`--library NAME` (repeatable) also searches the index a locked Lake package
publishes, and needs `--lean-root`, which names the Lake project.
For example,
`autoform search . "separating hyperplane" --lean-root . --library atlas`.
`autoform library list ROOT` reports each package in `lake-manifest.json`
with its type, locked revision, and whether search would accept it, and for a
package it would not, why. Text shows one row per package: name, type, the
first 12 characters of the revision, and `usable` or `not usable: REASON`.
`--json` emits `autoform-library-list/v1`, `schema` and `packages`, each with
`name`, `type`, `revision`, `index` (whether the checkout holds an index
file), `usable`, and `reason` (`null` when usable).
Neither command starts a process or writes a file.
A search without `--library` whose project locks a package with an index file
adds one `note:` line on stderr naming the package and the flags that search
it; stdout, the JSON and the exit status are unchanged. The note only checks
that the file exists, so `library list` still decides whether it is usable.

The index is the file `autoform-library-index.jsonl` at the package root, or
the file given as `--library NAME=PATH`. Its format,
`autoform-library-index/v0`, is provisional and may change without notice
until it is promoted to `v1`.

Before an index is used, search checks by reading files that the checkout is
at the locked revision, that the index's toolchain and direct dependencies are
the ones the checkout pins, that the Lean files under the indexed directories
are exactly the indexed modules with the recorded digests, and that the index
as a whole is one generation. A package that is not locked, not checked out, a
path dependency, replaced by `.lake/package-overrides.json`, without an index,
or failing a check is refused, as is any library of a project whose `.lake` or
packages directory is a symbolic link, with exit 2 and a message that begins
`library NAME:`. One refused library refuses the whole call, blueprint hits
included, since a partial answer would read as "this result is new". A
package replaced with `lake --packages=` is not detected. Neither is an edit
to the index file that leaves each module's `name`, `source_file`, and `sha256`
and the header's toolchain, dependencies, and source directories alone: a
declaration record, a module's `module_doc` or `module_system`, or the
header's `generator`, `probe`, or `package`.

A declaration matches when every term occurs in its last name component
(`lean`), docstring, module header (`module_doc`), full name or module
(`qualified_names`), the constants in its type (`mentions`), or its keyword
(`kind`), ranked in that order. Hits sort by best field, then the words as
typed before a stem, then worst field, then `complete` before `wanted`, then
name. `--state` and `--declaration` apply to articles only; `--limit` applies
to each list.

With `--library` the JSON schema is `autoform-search/v2`: every
`autoform-search/v1` key keeps its meaning, and `libraries` lists, in the
order given, `name`, `revision`, `index_schema`, `generator`, `probe`,
`differences`, `total_matches`, and `hits`. A hit carries the declaration's
`name`, `kind`, `status`, `module`, `source_file`, `line`, `signature`,
`statement`, `docstring`, `mentions`, `auto_named`, `module_system`,
`matched_fields`, `module_doc` (the module header when it matched, otherwise
`null`), and `import`, the line that makes a `complete` declaration available
(`null` for a `wanted` one, which carries no proof). `module_system: false`
marks a file a Lean module file cannot import. `differences` lists the
library's toolchain and each direct dependency whose locked revision is not
the project's, as objects with `what` (`lean_toolchain` or `dependency`),
`name` (`null` for the toolchain), `library`, and `project` (`null` when the
project locks no such dependency or either side is a path dependency); it does
not say the library fails to build. Without
`--library` the output stays `autoform-search/v1`, byte for byte.

#### Publishing an index

`autoform library index ROOT` writes `autoform-library-index.jsonl` at the
package root of a built library. `--lib NAME` (repeatable) limits it to those
`lean_lib` targets; the default is every `lean_lib` of the package. The format
is `autoform-library-index/v0`. Only the source directories of those targets
are read. A target's `globs` select its modules as Lake does: `M` is one
module, `M.*` the module and its submodules, `M.+` its submodules only.

It is the only library command that runs Lean and Lake. It imports the
library's modules, so run it on a trusted checkout or in a sandbox. It does
not build: a missing or stale build exits 2 with the `lake build` command
for the indexed targets and the first module Lake found unbuilt, and the
freshness check rewrites Lake's `.hash` files, so `.lake` must be writable.
`--timeout SECONDS`, a positive number, bounds the probe. The index records
the Autoform version that wrote it, so the command refuses to run from an
Autoform that is not installed. The index is written whole or not at all; a
failed write says why and whether the previous index is unchanged.

A declaration is indexed when all of these hold:

1. It is not a constructor, recursor, projection, auxiliary recursor,
   `noConfusion` or matcher, and its name is not an internal detail.
2. It has a declaration range.
3. Its range starts with a declaration keyword, after any docstring,
   attributes and modifiers. This leaves out what a command or attribute
   generates beside a declaration, and `where` and `let rec` helpers.
4. The source text at its selection range is its name, a dot-separated suffix
   of it, or the keyword `instance`. A leading `_root_.` is not part of the
   written name, so `theorem _root_.Nat.foo` is indexed as `Nat.foo`, and the
   quotes of a component such as `«odd name»` are not compared.

The `line` of a record is the line of the declaration's first modifier or,
with none, of its keyword: `theorem`, not the docstring or an attribute above
it. Its `statement` starts there and ends at the first of these, outside
brackets, comments, strings and character literals:

- `:=`, unless it belongs to a `let` or `have` written in the type;
- the word `where`;
- a `|` that starts a line, or is followed by a `=>` before the next `:=`,
  unless it belongs to an absolute value, which is a `|` and a closing `|`
  with no space inside (`|x|`) or, at the start of a line, a `|` that follows
  a `:`, a comma or an infix sign and has a closing `|` before any `=>`;
  a `|` that is part of an operator such as `<|` is not a bar;
- for an `inductive` type, the first `|`.

The statement may be cut short, and never holds a part of the body. A wanted
placeholder has no alternatives and no `where`: its statement runs to the end
of the command, or to its `:=` when a `def_wanted` or `instance_wanted` is
written with a body.

If a range the probe reports is not in the source file, the source is not the
one the library was built from, and the command exits 2 and names the
declaration and its module.

A declaration that rests on `sorry` or on an axiom other than `propext`,
`Quot.sound` and `Classical.choice` is left out, and the count is written to
standard error. A result declared with `theorem_wanted`, `proof_wanted`,
`def_wanted` or `instance_wanted` is indexed with status `wanted`.

`--check` writes nothing. It exits 0 when the index at the root is current, 1
when it is stale, with the reason on standard error, and 2 when it cannot
tell. A package with no index is stale. A package root that does not exist,
an index that exists but cannot be read, and a missing or stale build are
cases where it cannot tell. Changed, new and missing source files are found
without running Lean.
Run it in the library's CI after the build, with the same `--lib` flags, and
pin the Autoform revision: the bytes of an index depend on it.

The generator is tested on Lean 4.34.1 only and is not supported on Windows: it
exits 2, before it runs Lean, when the package's `lean-toolchain` is not
`leanprover/lean4:v4.34.1` or a spelling elan resolves to it, such as `v4.34.1`.
The index's `package` is the name in the package's `lake-manifest.json`, else in its
`lakefile.toml`, and the command exits 2 when neither gives one; the folder it
is checked out in never matters.
Module names must be ASCII identifiers. A Lean file under an indexed
directory that no indexed library builds is refused. So are two modules that
declare the same public name in two ways: Lean refuses to import both.

Lean does import two modules that state one theorem the same way, and keeps
one copy. Such a name is indexed once, under the first of its modules in name
order, with the line, statement, docstring and status of that module's
declaration. Which copy Lean keeps depends on the order it loads the modules
in; the index does not. Each such name is a warning on standard error that
names the module it is indexed under and the others, and the summary line
counts them. `--check` does not mention them. A name that is not indexed, such
as an equation lemma two modules both generate, is no warning. A name whose
first module leaves it on `sorry` or declares it as an axiom is left out and
counted with the others, with a warning that names the module whose declaration
was read and the other modules, since one of them may hold a whole proof. If the docstring of such a name comes from `@[inherit_doc]`,
the record has none.

A wanted name is no clash either: it is private to its module, so two modules
may use the same one, for example two anonymous `instance_wanted` of one
class, and each is indexed. A declaration record is identified by its name and
its module together.

## Open statements

By default a project runs the strict policy: CI rejects every `sorry`, so a
theorem's statement lands only together with its proof, and a statement waits
until the proof's prerequisites are proved. `open_statements: allowed` in
`roadmap/README.md` lets a theorem's statement land with a `sorry` proof. Such
an article, a theorem whose statement is formalized and whose proof is not, is
an open statement; CI audits it only through the declarations its `lean:`
names. A proof that uses open statements is conditional:
it compiles and records `proof: formalized`, but is reported as conditionally
proved, never as fully proved. Status, `work`, and the site derive that from
the Markdown dependencies and CI from what the Lean uses, so CI can print
`sorry-free` for an article the site shows as conditional, when its Markdown
depends on an open statement its Lean does not use. CI is never the looser of
the two, since a Lean reach the Markdown does not declare fails. The policy lets
dependents be stated and proved against a faithful statement before its proof
exists; the price is conditional
results that stay incomplete until every open statement they rest on is proved.

A retracted theorem, one recording `statement: retracted` as a revision leaves
it, stays an open statement: the Lean its `lean:` names,
`sorry` or not, still compiles into whatever uses it. The audit keeps accepting
its `sorry`, and what rests on it stays conditional, until Formalize restates
and proves it, or the marker and `lean:` are removed. A theorem that was never
stated is not open even when it has `lean:`: a draft name does not become an
assumption, and CI rejects its `sorry`. A definition is never open: its body
is its proof, CI rejects a `sorry` in it, and its statement phase waits until
its proof prerequisites are stated. A retracted definition is not open either,
but what rests on it still assumes the open statements its body reaches. Turn
the policy back off only once no open statement remains, since the strict audit
rejects every `sorry` and strict
status shows a proof resting on one as proved, not conditional.

Write an open statement's proof as exactly `sorry`. The audit accepts a `sorry`
only inside the proof of a theorem that an open article's `lean:` names: never
in its type, a helper, a definition, or a `where` clause, and never inherited
from a declaration outside the root package. Lean-generated auxiliaries count as
helpers: a `where` clause becomes `T.aux`, well-founded recursion over two or
more arguments moves the `decreasing_by` proof into `T._unary`, and structural
recursion through a `mutual` block compiles the bodies into `T._f`, so all three
fail. Recursion can move a `sorry` case into such an auxiliary, so write the
whole proof as `sorry`, never one case of it. `lake build --wfail` and
`warningAsError` turn Lean's "declaration uses `sorry`" warning into an error,
so they cannot be combined with open statements; the generated workflow runs
plain `lake build`.

The generated `autoform-verify.yml` reads the policy with `python3
.github/autoform_audit.py --policy blueprint`, which prints `allowed` or
`forbidden` from `roadmap/README.md` (`forbidden` when the file or key is
absent) and exits 1 with `error: ...` on malformed frontmatter. Under either
policy the workflow then writes the `autoform work assumptions blueprint --json`
contract. Under `forbidden` it runs the strict audit with `--targets`: any
`sorryAx` fails with `NAME depends on unexpected axiom sorryAx` and
`root-package declarations failed the kernel-trust audit`. So does a `lean:`
name the build did not compile, with `NAME [ID] is not a declaration of the
Lean build; fix the article's lean: name or build the module that declares
it`: a theorem in a file no target imports or inside a string
passes the lexical `autoform check --lean-root` but not this. The strict audit
refuses a contract that allows open statements, records an article as open, or
lets one assume an open statement. Under `allowed` the workflow audits every
root-package declaration against the contract. The audit accepts a `sorry` in a
declared open statement's own proof and a proof that reaches only the open
statements its article's Markdown dependencies reach. It rejects a `sorry` in a
statement, a `sorry` anywhere else in the root package, a dependency outside the
root package that depends on `sorry`, an article declaration that reaches an
open statement its Markdown dependencies do not reach (so a fully proved
article, which assumes nothing, may reach none), a `lean:` name missing from the
build, and an open statement that its article records as proved. Each article
declaration gets at most one of these status lines, with `NAME` the declaration
and `ID` the article's node ID. A declaration with an error, or one that
reaches a failed declaration, gets none of them, and no declaration gets one
when the kernel replay fails:

```text
open statement (proof is sorry): NAME [ID]
open statement (proof depends on sorry elsewhere): NAME [ID]
open statement (proof is sorry-free; restate it if retracted, then record proof: formalized): NAME [ID]
conditional: NAME [ID] rests on open statement(s) A, B
sorry-free: NAME [ID]
```

A passing audit ends with `kernel trust clean except declared open statements
(N root-package declaration(s) audited; K open statement(s), C conditional
declaration(s))`; a failing one logs each error, naming the declaration and
what to change, and ends with `root-package declarations failed the
open-statement audit`.

Both audits check two more things. A `lean:` name outside the root package, as
a Mathlib article's is, must be neither unsafe nor partial (a `partial def`
counts as partial) and use no axiom beyond `propext`,
`Classical.choice`, and `Quot.sound`; otherwise it fails with `NAME [ID] is
outside the root package and is unsafe or partial` or `NAME [ID] is outside the
root package and depends on unexpected axiom AX`. The open-statement audit
reports `sorryAx` there as `NAME [ID] is outside the root package and depends
on sorry`. And both replay every root-package declaration through the kernel
on top of a fresh import of the modules outside the root package, because the
build's `.olean` files can hold anything and a root `run_cmd` can add a
declaration with kernel checking off. The replay runs only once every other
check has passed: replaying a declaration that reaches `Lean.reduceBool` or
`Lean.reduceNat` runs compiled project code, and the axiom check rejects every
such declaration, since both depend on `Lean.trustCompiler`. Until then the
audit logs `kernel replay of the root package skipped: it runs once every other
check passes`. A declaration the kernel rejects fails with `kernel replay of the
root package failed: ...`, which names the first one; the workflow's scan for
the kernel-check bypass option is only lexical.

Both audits refuse an unsafe or partial root-package declaration with `unsafe
or partial declaration: NAME`, and report a `partial def` under its own name.
For every computable recursive definition, structural, well-founded, or
`partial`, Lean adds a partial copy `NAME._unsafe_rec` for the compiler to run.
The audits skip these copies: the kernel refuses a safe declaration that refers
to one, and the kernel replay skips them too, so it refuses such a declaration
added with kernel checking off. A `lean:` name that is one still fails, with
`NAME [ID] is Lean's compiled copy of a recursive definition; name the
definition itself`.

The probe runs no project code. Its header imports only the toolchain's Lean,
and the workflow runs it with plain `lean`, not `lake env lean`, passing the
project's search path in `AUTOFORM_AUDIT_LEAN_PATH`. The probe refuses to run
when that variable is unset or when `LEAN_PATH` is set. It loads the
root-package modules as data, with the toolchain's library first on the search
path and no environment extensions, so no `initialize` block runs and no project
instance, macro, or elaborator applies to the probe's own code. It finds each
declaration's axioms by walking types and values itself, not from the axiom
tables the `.olean` files store, and counts a name that no module declares as an
axiom, which fails with `NAME depends on X, which no module of the build
declares`, or for a `lean:` name outside the root package with `NAME [ID] is
outside the root package and depends on X, which no module of the build
declares`. The walk takes a mutual inductive block as one node, so each type and
constructor of the block gets the axioms of the whole block. It imports the
toolchain's `Init` along with the build, so a core name that the build declares
itself, such as `propext`, cannot pass the axiom allowlist, which compares
names: a differing declaration fails to load with `environment already contains
'propext'`, and an identical copy of a theorem or of a Prop-valued axiom is
merged with the toolchain's own constant. Lean does let a
theorem stand in for another module's axiom of the same name and type, which the
module system relies on, so a theorem restating an axiom such as
`Lean.trustCompiler`, whose type is `True`, fails with `M declares a theorem N
in place of the axiom K declares; rename one of them so the audit can run`. A
root module whose first component names an entry of the toolchain's library,
such as `Lean.Hack`, would be looked up in the toolchain's library, where it
fails to load or a toolchain module of the same name stands in for it, so the
audit refuses it with `root module M shares its first component R with the
toolchain's library; rename the module so the audit can run`. Lean loads a
module from the first search-path entry that holds its first component, and Lake
lists dependency libraries before the root package's, so a dependency library
that also provides a root module's first component could stand in for the root
package's files. The audit refuses that with `root module M shares its first
component R with another library on the search path (DIRS); rename the module so
the audit can run`, where DIRS lists the library directories that hold R. The
workflow runs the probe with `LEAN_ABORT_ON_PANIC=1`, so a panic stops it
instead of letting it continue with a default value. It also requires the
probe's success line as its last line, so a probe that stops early fails the
step even when it exits 0.

The audit does not cover build-time IO. `lake build` runs root and dependency
code, and the Lake configuration, on the same runner before the audit, so that
code can rewrite the audit script, the toolchain, the workflow's inputs, or the
blueprint before the audit reads them. The audit also does not defend against a
malicious dependency at build time, edits to the workflow, or crafted `.olean`
bytes. Closing those needs a separate audit job that runs no project code; the
generated workflow does not have one. The project's `lean-toolchain` chooses
the Lean that builds the project and runs the probe: the workflow installs elan
with `--default-toolchain none`, and elan installs any toolchain the file names,
including a fork on GitHub, so the audit trusts that toolchain too.

`autoform init` writes `.github/CODEOWNERS.autoform.example`. GitHub ignores
that filename, so the example cannot mask or replace a repository's active
owners. Its two suggested rules cover Autoform's control paths: the roadmap's
`open_statements` policy and `.github/` CI. To activate them, find the first
existing CODEOWNERS file in GitHub's order (`.github/CODEOWNERS`, `CODEOWNERS`,
then `docs/CODEOWNERS`), or choose `.github/CODEOWNERS` when none exists; copy
the rules there, replace `@OWNER`, and uncomment them. Consider rules for the
Lean build controls too: the Lake configuration, `lean-toolchain`, and
`lake-manifest.json` decide what CI compiles.

GitHub reads CODEOWNERS from a pull request's base branch, so new rules cannot
protect their own activation unless equivalent coverage already exists there.
Review that change explicitly. Then require pull requests and code-owner
review, dismiss stale approvals after new commits, and audit every
branch-protection or ruleset bypass. These rules put changes in front of an
owner; they do not stop project code from changing files on the CI runner.

The step runs `autoform work assumptions` from `AUTOFORM_REF`, and the earlier
`autoform check` step validates the frontmatter with that same pin. Scaffolded
workflows pin `AUTOFORM_REF` to the Autoform checkout that scaffolded them, so a
project scaffolded before open statements existed must, before opting in, move
`AUTOFORM_REF` to a commit that has `work assumptions` and replace
`.github/workflows/autoform-verify.yml` and `.github/autoform_audit.py` with the
versions `autoform init` writes at that commit. An opted-in project with an
older pin fails closed: `autoform check` stops with `unsupported frontmatter key
'open_statements'`, and the audit step fails without `work assumptions`. Under
the strict policy that missing command, which argparse reports as `invalid
choice: 'assumptions'`, is the one failure the step tolerates: it logs a warning
that `AUTOFORM_REF` predates `autoform work assumptions` and runs the strict
audit without the `lean:` name check, as earlier workflows did. Any other
failure there, such as a failed fetch, fails the step. An older workflow runs
the strict audit, which rejects every `sorry`. A newer
`.github/autoform_audit.py` keeps the older workflow's three-argument form,
which runs the strict audit without the `lean:` name check. Its probe refuses
to run under an older workflow's `lake env lean`, so replace the two files
together.

To reproduce the CI audit locally after a build, with `ROOT_PACKAGE` the Lake
package name the workflow reads from `lake translate-config toml`, and
`--targets` in place of `--open-statements` under the strict policy:

```bash
lake build
lake pack /tmp/autoform-root.tgz
autoform work assumptions blueprint --json > /tmp/autoform-assumptions.json
python3 .github/autoform_audit.py --open-statements /tmp/autoform-assumptions.json \
  ROOT_PACKAGE /tmp/autoform-root.tgz /tmp/autoform-probe.lean
AUTOFORM_AUDIT_LEAN_PATH="$(lake env printenv LEAN_PATH)" LEAN_ABORT_ON_PANIC=1 \
  lean /tmp/autoform-probe.lean
```

The audit passes only when `lean` exits 0 and the last line it prints is the
`kernel trust clean ...` line; the workflow checks both.

## Claim contract

Claims use canonical `autoform-claim/v1` JSON in orphan commit messages and
exact observed object IDs as update preconditions. Absent and verifiably expired
leases may be acquired; live peer leases are refused. Malformed or unreadable
refs are unverifiable and may not be acquired, renewed, released, or removed by
cleanup. A heartbeat verifies ownership on entry and permanently records any
later refusal or transport uncertainty as lost ownership.

A claim key is a slug and digest of any string, not a validated node id, so a
shared resource is locked the same way a node is, as is a Lean helper no
article owns under the `lean/<slug>-<digest>` key `work impact` reports.
Parallel agents get one Git worktree each, and builds take no claim. Worktrees
on one machine share its elan toolchain and Mathlib cache, so Formalize fills
the cache once before creating them.

`acquire`, `renew`, and `release` accept several targets. The board reads
every ref once, applies the single-key ownership checks to each, and sends one
atomic push with a lease per ref, so either every claim changes or none does;
a board that cannot push atomically is refused. Success prints the usual line
per target. A refusal exits 1 with one line such as `error: could not acquire
af_y, af_z; no claim was acquired: held by another worker: af_y`, naming the
blocking targets when known, and leaves no claim behind; a duplicate target
exits 2. Workers that need several
claims follow a no-hold-and-wait rule: acquire the whole set in one command and,
when it is refused, release everything already held and retry with the whole
set, never holding some claims while waiting for others. The CLI does not
enforce the rule; it is what keeps two multi-article revisions from
deadlocking.

A Git process that stops while updating a claim ref leaves a lock file in the
board, and Git then refuses the updates it guards:
`refs/autoform-claims/<key>.lock` blocks that claim, `packed-refs.lock` blocks
every release, and on a board in the reftable ref format
`reftable/tables.list.lock` blocks every claim update. On a board given as a
local path, or as a `file://` URL except on Windows, a command whose push such
a file still blocks two seconds later exits 1 naming the file and when it was
modified; a lock that goes away within those two seconds counts as a lost race.
On such a board, `claim cleanup` leaves a lease whose release a lock file
blocks in place and still exits 0, and `claim list` and `claim cleanup` warn on
stderr about each lock file not modified within two seconds of the current
time. Git records no owner for a lock file, so Autoform never deletes one: once
no Git process is using the board, delete the file and retry. Over any other
transport the board's files are not visible, so a lock file there shows up as a
lost race or as Git's own error, depending on the file, the operation and the
server's Git version, and Git's error also stops `claim cleanup`.

Claims are temporary operational state, never article frontmatter.

## Revision contract

Revising a declaration X of article R that other articles' Lean uses touches
work R's claim does not cover. This contract makes that work claimable and
keeps the default build passing. Roadmap records a requested revision in the
Markdown (step 6); Formalize carries out the Lean side (steps 1 to 5).

1. On a fresh build, run `autoform work impact R . --lean-root .`, with
   `--declaration` when only some of R's declarations, or a helper, change.
2. Choose the route:
   - **Contained** (`contained: true`): revise X in place, under R's claim when
     using claims. `contained` ignores R's own declarations, so re-check R's
     other declarations that use X as well.
   - **Expand, migrate, contract**, the default whenever anything uses X: add
     X' with the revised statement, leave X unchanged and mark it
     `@[deprecated X' (since := "YYYY-MM-DD")]`, and point R's `lean:` at X'.
     Under the open policy, while X's proof is still `sorry`, R's `lean:`
     names X beside X', so the audit keeps accepting that `sorry` as an open
     statement, and R records `proof` only after step 4 deletes X.
     Statement-impacted articles replace `statement: formalized` with
     `statement: retracted`, lose `proof`, and keep `lean:`, so they return to
     the frontier as revisions; under the open policy a statement-impacted
     theorem stays an open statement meanwhile (see [open
     statements](#open-statements)). When X's proof is sorry-free,
     proof-impacted articles keep everything, since their proofs still use the
     valid old X; migrating them to X' is later work. While it is still
     `sorry`, proof-impacted theorems lose `proof: formalized` but keep
     `statement` and `lean:`, so they return to the frontier as proof phases
     and migrate to X'. A stated definition counts as proved whatever its
     `proof:` says, so a proof-impacted definition is migrated to X' in the
     same commit or, when that is not possible, retracted like a
     statement-impacted article. Otherwise they would keep resting on the
     deprecated, `sorry`'d X, show "conditional, assumes R" although R's text
     now describes X', and keep X out of `deprecated_unused`, so R could never
     record its proof. The claim set is every article whose frontmatter or
     Lean changes: R, the statement-impacted articles, the unused statement
     dependencies, and, in that case, the proof-impacted ones.
   - **In place**, only when X and X' cannot coexist, for example an instance
     or a structure change: the claim set is every `claim_targets` entry.
     Repair every impacted declaration in one commit whose default build
     passes. A change `work impact` cannot see, to an instance's priority or
     scope, an attribute, or notation, also takes this route whatever
     `contained` says, and its `claim_targets` are incomplete: add every stated
     article whose Lean imports the changed module and treat it as
     statement-impacted. A statement-impacted article keeps `statement` only
     after an Agent Review of its source faithfulness under X's new meaning;
     otherwise it records `statement: retracted`, loses `proof`, and keeps
     `lean:`. A repaired dependent proof keeps `proof: formalized` only after
     an Agent Review of the repair; otherwise it loses `proof`. A theorem's
     proof that cannot be repaired becomes exactly `sorry` under the open
     policy; otherwise delete the declaration and remove its article's
     `lean:`, `statement`, and `proof`, which works only when nothing else
     uses it. When neither applies, the revision is blocked: release any claims
     and report it. Record what happened in the revision commit and report. Use
     an implementation note only until [its deletion
     rule](#articles-and-containment) applies.

   An unused statement dependency, which rules out the contained route, is
   re-reviewed under the revised meaning, X' on the expand route and X's new
   meaning in place: it keeps `statement` only after an Agent Review of its
   source faithfulness; otherwise it records `statement: retracted`, loses
   `proof`, and keeps `lean:`.
3. When using claims, claim the route's claim set with one
   `autoform claim acquire`. When it is refused, release everything and report
   the held claim as the blocker. After acquiring, re-run `work impact`; if the
   set grew, release and start over with the larger set. Without claims, a
   Formalize subagent has its lead confirm the set instead. With or without
   claims, after rebasing onto the current shared branch and rebuilding, re-run
   `work impact` once more; if the set grew, repair the new targets before
   landing, after acquiring the whole larger set in one command under the
   no-hold-and-wait rule of the [claim contract](#claim-contract) or, without
   claims, after the lead confirms them. Under the open policy, reproduce the CI
   audit as [open statements](#open-statements) shows before landing. Land one
   commit, then release any claims.
4. Contract: delete a deprecated X once it appears in `deprecated_unused` of
   `work impact R . --lean-root .`, meaning no declaration uses it and no
   other article's `lean:` names it, and drop it from R's `lean:` in the same
   commit. `contained` is not enough: it ignores R's own declarations, such as
   an X' built from X.
5. `autoform audit --lean-root` reports `lean-target-deprecated` for a
   declaration article whose `lean:` names a declaration with `deprecated` in
   its own `@[...]` attribute list; point it at the replacement. The check is
   lexical, so it misses a later `attribute [deprecated] X`, which the
   deprecated list of `work impact` does see. Under the open policy the finding
   is expected for a superseded X that step 2 keeps in R's `lean:` until step 4,
   so `autoform audit --lean-root` and `autoform doctor --lean-root` fail in
   that window by design, while CI, which runs neither, passes.
6. When Roadmap revises an article, its statement text or only its Lean, it
   records the decision and retracts the article: it replaces `statement:
   formalized` with `statement: retracted`, removes `proof: formalized`, and
   keeps `lean:`, which `work impact` needs, so the article returns to the
   frontier as a revision. It retracts only that article and the dependents
   whose Markdown text the revision rewrites; the Lean-side impact decides
   every other dependent. Roadmap edits only Markdown: it releases any claims
   and leaves the Lean revision to Formalize.

## Local runtime doctor

Use the runtime projection and roadmap audit together without contacting any
external service:

```bash
autoform doctor . --lean-root .
autoform doctor blueprint --json
```

The doctor reports six ordered checks: blueprint resolution, runtime schema,
graph counts, reference invariants, roadmap audit, and optional local Lean
targets. It exits zero only when every required check passes. Omitting
`--lean-root` records an explicit advisory pass; supplying it performs only a
lexical local-source check, not a Lean build, kernel check, or proof-honesty
review. Only the `lean targets` check reads Lean sources, once. It holds every
finding that needs the Lean root, including `node-too-large`, and an unusable
Lean root or unreadable source fails only that check. The bundled example
intentionally exits nonzero while its declared coverage still holds `MAPPED`
rows.

This command is strictly read-only and local. It does not invoke Git, GitHub,
subprocesses, network services, claims, queues, reviews, recovery state,
providers, workers, renderers, or dashboards, and it creates no cache, scratch
repository, service, state directory, or `graph.json`. It is a project/runtime
doctor, separate from any future worker fleet or machine-capability preflight.

## Runtime contract

`autoform_cli.runtime` projects the canonical Markdown graph into the versioned,
deeply immutable in-memory schema `autoform-runtime/v4`. Its declared authority
is `markdown-articles`: the adapter copies hierarchy, typed statement and proof
dependencies, authored assertions, derived progress, provenance, and optional
local Lean source locations, but it provides no persistence or write API. A
source target that is neither an http(s) link nor a path inside the blueprint,
and a `mathlib_file` that is not a portable relative path, are left out rather
than refused. `check` fails on a local path outside the blueprint and warns
about the rest.
`RuntimeGraph.as_dict()` and `to_json()` are deterministic compatibility
snapshots for consumers, not an authored or generated graph file. Autoform never
creates, synchronizes, or treats `graph.json` as an authority.

Every article remains in the runtime view so consumers can preserve the book's
arbitrary containment hierarchy. A node is dispatchable only when it is both a
formalizable article and a leaf; narrative containers and prose-only leaves are
never proof work units. Module inventories are likewise non-formalizable,
non-dispatchable source records. The source revision hashes exact roadmap
article paths and bytes, excluding timestamps, absolute paths, Git state, and
operational state. Optional Lean locations come from a local lexical scan and do
not by themselves establish compilation or proof correctness.

Schema v4 adds a nullable catalog discriminator for non-dispatchable module
inventories. It retains v3's optional durable `article_id` metadata beside the
graph's path-derived `id`, project `open_statements` policy,
`statement_retracted` assertions, and each status's `assumes` and `waiting_on`
fields. Temporary claims and local dashboard hooks may fall back to the path ID,
but durable execution records and routes must require `article_id` until the
path-move migration is complete. Operational state remains private and excluded
from runtime snapshots and publication.

Projection fails when an article's bytes no longer match the digest recorded
when the graph was loaded, so a source revision never labels node data parsed
from older bytes. A hand-built node without a digest makes no snapshot claim:
its `source_sha256` stays null and the revision covers whatever bytes its
article holds at projection time.

## Publication contract

`autoform render` publishes the book, derived progress, and one dependency
explorer as bounded project, chapter, and nested-scope projections. The legacy
full route is a project-scale compatibility shell backed by a layout-free global
search index, never an all-repository node cloud. Rendering never reads a
`graph.json` or an operational queue. Hidden files and directories, including
`.implementation-notes`, are omitted, while a `.autoform` directory and
symlinks, credentials, logs, provider state, and agent/task state outside hidden
directories cause the render to fail rather than silently leak them. Source and
output directories must be disjoint. A blueprint path that differs only in case
from one render writes (`SUMMARY.md`, `dependencies.json`, `dependencies.md`,
`structure.md`, `publication.json`, `assets/`, `dependencies/`, `javascripts/`,
`stylesheets/`, and the generated stylesheet, scripts and logo inside them) is
refused, as are two blueprint paths that differ only in case or Unicode form,
because they would collide on a case-insensitive filesystem. A blueprint file at
exactly `dependencies.json` or `javascripts/blueprint-dag.js` is left out of the
site with a warning, because the explorer writes its own file there.

Every render writes `publication.json` with blueprint and Lean-source hashes
(the Lean-source hash is empty when render reads no Lean sources, as on native
Windows), Git ref, article and dependency counts, the site's file inventory, and
available views. It contains no timestamp or absolute path, so identical inputs
produce identical output files. Render reads the blueprint and the Lean sources
once each and derives every page, link and hash from those bytes; an edit saved
during a render waits for the next one, and until then the dashboard reports the
built site as stale. The vault's hidden implementation notes are read in place
only to validate them. Repository links appear only when the Lean bytes match
one locally available Git commit, recorded by its full object ID; a clean
checkout that Git converts on checkout still matches. Otherwise the render stays
local, keeps source notes in the site, records no Git ref, and warns with the
first mismatch. Links into the blueprint (an article's Markdown source, a source
note) also need the blueprint's bytes in that commit. When they differ, render
keeps the code links, keeps source notes in the site instead of linking to them,
and warns with the first mismatch; a blueprint outside the repository, or in a
submodule or other checkout nested in it, gets the same result without a
warning. When render reads no Lean file (native Windows, or a project with no
`.lean` file yet), it records the Git ref only if the blueprint matches it.

The manifest's schema is `autoform-publication/v3`. It records what v2
records, but the site holds the shared explorer's JSON projections and script
in place of v2's Mermaid dependency maps, and the blueprint hash is taken under
a label of its own, so no v2 `source_revision` equals a v3 one. The dashboard
accepts only a v3 site and reports an older one as stale until render replaces
it.

The site is built and synced in a hidden `.autoform-publication-*` directory
beside the output, then swapped in with one atomic exchange (`renamex_np` on
macOS, `renameat2` on Linux); renders committing into the same parent directory
take turns when it can be locked. On filesystems without the exchange (NFS, SMB,
9p or WSL2 `/mnt/c`, exFAT, sshfs) the previous site is first renamed to
`.autoform-publication-backup-<name>`; if render dies between the two renames,
the next render moves it back before anything else. Render replaces only an
empty directory or a complete `autoform-publication/v2` or v3 site whose files
match its inventory (macOS metadata files, `.DS_Store` and `._*`, are ignored
in both), and refuses anything else, including a legacy v1 output, which you
remove once by hand. A failed or refused render leaves the output as it was. A
render killed mid-way can leave a `.autoform-publication-*` directory beside the
output (the repository root for `--output site-src`); render never indexes it
as Lean source and the setup ignore rules cover it, but it may hold the previous
site, so inspect it before deleting it. (On macOS `fsync` does not flush the
drive's cache, so a power loss can still lose the newest site.) Native Windows
has no directory-descriptor traversal. There render reads the blueprint and the
output as two pathname captures that must agree, commits through the backup
swap without a lock, and reads the Lean sources only for an explicit
`--lean-root`, which then fails closed. Without one it publishes no code
links; when a published statement names a `lean:` declaration it warns, and
`--require-declarations` makes it exit 1.
