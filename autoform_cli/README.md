# Blueprint format and CLI

The Autoform CLI validates, visualizes, and publishes the multilevel dependency
graph in `blueprint/roadmap/`. The Markdown book is the graph; no separate
graph file exists.

Agents run these commands; normal project work starts from Autoform's skills in
the user's agent window.

## Articles and containment

Every Markdown file below `blueprint/roadmap/` is an article node. A
`README.md` represents its directory and strictly contains the articles below
it; the nearest ancestor `README.md` is the single parent, at any depth. An
ordinary file's stable ID is its path without `.md`; a `README.md`'s is its
directory path, and the root article's is `roadmap`.

The H1 is the article's title. Container prose is the exposition; a standalone
list item linking to a formalizable leaf places the leaf there in the published
chapter. Unplaced leaves appear under “Other statements”,
unplaced module catalogs under “Module inventories”. Keep the root article a
short preface and table of contents, not an inventory of every milestone: large
inventories belong in coverage, and a detailed container introduces its
mathematics in prose and places statements under meaningful section headings.

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

An article's statement, which `autoform audit` and the published theorem box
read, is its body from the end of the frontmatter to the first `##` heading,
without the H1 line. The first H1 (the title), if written below a `##` section,
ends that section and resumes the statement; a later H1 stays in its section.
Headings deeper than `##`, and a `##` line inside a fenced code block or an HTML
comment, do not end the statement. Close each fence with its own bare line of
the same character, length and indent: on a chapter page an unclosed fence can
pair with a later article's fence and hide what lies between, so the audit
reports `unclosed-code-fence`, and for a fence left open at the margin `check`
warns and the site shows that page as typed (see
[Validation](reference/validation-and-audit.md#validation)). The graph and an
article's statement and sections read an unclosed fence as plain text, even
where the page pairs its backticks with a later run as inline code, and a later
bare line of its character at least as long closes it. Coverage files and the
unpublished-folder link check read it as open to the end of the file, and so
does render when it rewrites links, orders the book and places a chapter's
statements. An HTML comment that never closes, even one opened in inline code or
an indented code block, hides every later heading and dependency link: the graph
loses those edges, one opened in the statement puts the rest of the article in
the theorem box, and nothing reports it.

`## Depends on` lists what the article needs to be *stated*;
`## Proof depends on` lists what only its *proof* needs. Links under them are
graph edges, except in a code block (fenced or indented) or an HTML comment;
other links are navigation or citations. Dependencies resolve relative to the
article and must point at another roadmap article.

The optional `declaration` field marks a formalizable leaf and names its
intended Lean declaration command, any one, such as `def`, `theorem`,
`structure`, or `instance`; containers and exposition articles omit it. A
declaration that introduces data rather than a proposition has no separate
proof obligation, whatever its modifiers (such as `noncomputable`); an `axiom`
still has one.

`origin` records provenance for formalizable work: `cited` for a direct source
target, `bridged` for a result introduced between source targets, and
`background` for prerequisite mathematics.

Frontmatter is optional; a container that only supplies prose and placement
needs none.

The optional `area` field places a container in a mathematical region of its
parent's explorer map; on a leaf it only labels the article. Use mathematical
areas such as `Foundations` or `Geometry & Topology`, never repository or
workflow buckets such as `MathlibExt` or `catalog-01`. Autoform does not guess
areas from paths, imports, or titles.

An optional container-only `navigation` role separates mathematical topics
from repository organization:

- `topic` (the default): a mathematical subject or coherent group of results.
- `collection`: a library or other organizational collection; its parent's
  overview shows its children in its place.
- `transparent`: a generated pagination or range container, flattened the
  same way. Both are left out of the outline and ancestor breadcrumbs.
- `source`: a book or other source corpus, shown as itself.
- `module`: mirrors one Lean source module and holds its declarations; shown
  like a topic, sized as the module (see
  [Structure](reference/validation-and-audit.md#structure)).

Roles never change identity, containment, readiness, or edges. An overview
draws no edge to a collection it replaces with its contents, so depend on an
article inside the collection instead.

For a taxonomy kept apart from article files, `blueprint/atlas.json` may assign
the same areas without moving pages:

```json
{"schema":"autoform-atlas/v1","areas":{"Geometry & Topology":["topology","geometry"]}}
```

Every listed value is a roadmap node id. A node takes the first area that
lists it; an `area:` on the node wins. The file never stops a command: `check`
and `render` warn about each ignored entry, or the whole file when it is not
readable `autoform-atlas/v1` JSON, and `audit` reports each as an
`atlas-omission` [warning](reference/validation-and-audit.md#warnings). Render
leaves the file out of the site; a page's link to it keeps only its text.

A narrative leaf may summarize an existing Lean module by setting
`catalog: module` and omitting `declaration`; it is never dispatched as a proof
task. A catalog must be a leaf (`audit` reports `catalog-container`) and links
a declaration ledger under `blueprint/sources/` from `## Sources`: `check`
warns about a catalog that links none and `audit` reports
`catalog-missing-ledger`; a link to a missing ledger fails both, and a hidden
ledger under `sources/` draws a `check` warning and fails only `audit` (see
[Validation](reference/validation-and-audit.md#validation)).

A completed catalog records in `lean:` the full name of every public
declaration the module writes with a declaration keyword, as the Lean source
index records them. Names Lean generates, such as constructors, structure
fields, derived or unnamed instances, and `@[ext]`, `@[simps]`, or
`@[to_additive]` results, and declarations the index cannot see yet, behind
`nonrec` or `meta` or written `class inductive`, go in the ledger as
supporting entries, never in `lean:`; a `private` name never goes in `lean:`.
A completed catalog may assert `statement: formalized` and `proof: formalized`
once the complete module has been checked. Like any prerequisite, a catalog
linked under `## Depends on` or `## Proof depends on` holds its dependents back
until its whole-module assertion is recorded, so a dependent that needs one
result links the declaration article that expounds it.

`check --lean-root` and `audit --lean-root` resolve every name the catalog
lists and, for a name they do not find, say that a generated name belongs in
the ledger. They cannot prove the list omits no newly added public
declaration; completeness remains an authored assertion. `audit --lean-root`
reports the
[elaboration limits](reference/validation-and-audit.md#elaboration-limits) of a
catalog's declarations as warnings, and a catalog that records
`proof: formalized` gets `lean-target-wanted` for a wanted target it names; the
deprecation, declaration-kind, and node-size checks apply only to declaration
articles.

Versioned implementation notes live at
`blueprint/.implementation-notes/<article_id>.md`, one per article, and survive
article moves. Claims keep workers from editing the same leaf's note;
containers have no claims. Every command that loads the graph rejects
malformed, empty, symlinked, or orphaned notes, so assign the article ID before
creating its note. Any worker may delete a stale note or repair an orphan: move
anything still useful into the surviving article's note and delete the orphan,
or restore a lost `article_id` line if the article still exists. Delete a note
once its article is proved; for a container, once every formalizable
descendant and every `not_ready` leaf beneath it is proved and there is at
least one (a module catalog never counts). `autoform audit` reports a note left
after that point. Render omits the hidden directory from the site and the
publication hash.

## Assertions and derived status

An article asserts only facts a human or agent verified:

| Key | Meaning |
| --- | --- |
| `area: Geometry & Topology` | Authored mathematical region for atlas grouping. |
| `navigation: collection` | Presentation-only container role: `topic`, `collection`, `transparent`, `source`, or `module` (see above). |
| `catalog: module` | A non-dispatchable leaf cataloging one existing Lean module. Its `lean:` lists the module's public keyword-declared names, and it links its declaration ledger under `blueprint/sources/` from `## Sources`; cannot be combined with `declaration` or the `mathlib*` keys. |
| `statement: formalized` | The Lean statement exists and compiles. Requires `lean:`. |
| `statement: retracted` | A revision retracted the statement while `lean:` still names the old declaration, which stays in the build until Formalize restates the article and records `statement: formalized` in its place. Requires `lean:`; invalid with `proof: formalized` or `mathlib: true`. |
| `proof: formalized` | The Lean proof compiles. Requires `statement: formalized` and `lean:`. Under the open policy it may rest on open statements, and the article is then conditional; only the derived `fully_proved` means complete and `sorry`-free. |
| `mathlib: true` | The result is upstreamed into Mathlib. |
| `mathlib_declaration: Nat.exists_infinite_primes` | Mathlib name(s) the article restates; `audit` requires one on a `mathlib: true` article. Render lists each in a Mathlib row of the article's statement box, as text without a link, since nothing checks it against the pinned Mathlib. |
| `mathlib_file: Mathlib/Data/Nat/Prime/Infinite.lean` | The Mathlib file that declares the `mathlib_declaration` name(s), as a portable relative path; `search` reports it. |
| `not_ready: true` | Needs more blueprint work before it can be attempted. |
| `lean: Ns.decl` | Declaration name(s) that discharge the article. |
| `discussion: 42` | Issue number or URL where the article is being discussed. |
| `article_id: af_...` | Durable identity, `af_` plus 24 lowercase hex digits; `autoform work` requires it on unfinished formalizable leaves, `review record` on the article it records, and `review prepare`, `review check`, and `render --review` on every Lean-mapped article but a module catalog. |
| `review_approved: sha256:<64 hex>` | The hash of the complete review surface someone approved for this article. It shows that surface is unchanged, not who approved it. |
| `open_statements: allowed` | Project policy, valid only in `roadmap/README.md`: a theorem's statement may land with a `sorry` proof (see [Open statements](reference/open-statements.md#open-statements)). Absent or `forbidden` keeps the strict policy. |
| `published: false` | Folder setting, valid only in the `README.md` of a folder below `roadmap/`: `check` and `audit` validate the folder and everything in it, but render leaves it out of the site and of the publication hash. No link on a published roadmap article may point into it, whether a dependency, a source or prose; a link from any other page keeps only its text, with a warning. `audit` and render reject coverage evidence that points into it. A folder inside an unpublished one is unpublished too, whatever its own README says. |

Progress is *derived* from the DAG on every run, so it cannot go stale.
Readiness depends on the policy: under the default strict policy CI rejects
every `sorry`, so a theorem's statement lands only with its proof.

| Derived state | Holds when |
| --- | --- |
| `can_state` | Every statement prerequisite is stated and every proof prerequisite is proved. Under the open policy a theorem waits for no proof prerequisite, and a definition, whose body is its proof, waits for them to be stated. |
| `can_prove` | Stated, every statement prerequisite is stated, and every proof prerequisite is proved (strict policy) or stated (open policy). |
| `proved` | The article records `proof: formalized`, is a stated definition, or is in Mathlib. |
| `conditional` | Proved, but the proof rests on an open statement; open policy only. |
| `fully_proved` | Proved, and every prerequisite is fully proved, recursively. |
| `defined` | A definition is written but rests on unfinished work; one whose body rests on an open statement is `conditional` instead. |

A proved theorem that rests on unfinished work is green, not the dark green of
`fully_proved`. A `conditional` ("conditionally proved") article reaches an
open statement, a theorem stated or retracted but not proved (see
[Open statements](reference/open-statements.md#open-statements)), through its
dependencies; an open dependency counts together with whatever its statement
prerequisites reach, and a proved dependency passes on everything it reaches.
The site colours it violet and lists those open statements in an `Assumes`
row of its statement box.

## Commands

This section is the single source of truth for the command line; skills link
here. Every `autoform` invocation shown here or in a skill is parse-checked
against the real parser.

Commands are written as they appear on `PATH` with this package installed. In
a consumer project, where it is not, resolve `<AUTOFORM_PLUGIN_ROOT>` from the
loaded plugin and prefix each command, running from the project root:

```bash
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform check blueprint --lean-root .
```

`autoform --version` prints the installed version, followed by the plugin
checkout's commit when it runs from one, as in `autoform 0.6.0 (745a88599469)`;
a debrief records the same string.

### init

Create a new project's vault, site configuration, and CI. Existing files are
left alone, so the same command repairs a project. `--force` instead overwrites
every file `init` manages with its template, `blueprint/roadmap/README.md`,
`blueprint/coverage/README.md`, `README.md` and `mkdocs.yml` included, and
replaces a root `.gitignore` rather than adding to it:

```bash
autoform init . --title "Finite Flat Group Schemes" \
  --repository-url https://github.com/owner/repo
```

`init` appends the missing Autoform rules to an existing root `.gitignore`,
which must be a regular file with one link; when it cannot verify the append,
the error says the file may be partial: inspect it before retrying. Even with
`--force`, `init` refuses before writing anything when it finds an entry that
differs from a path it manages only in letter case or Unicode form (a
`Blueprint/` Lean library, `MkDocs.yml`), a managed directory it cannot list,
or a root `Readme.md` or `readme.md`. The vault is always `blueprint/`, so a
Lean library named `Blueprint` needs a different library name (its lakefile
`lean_lib`, root module and imports), not a lowercase rename.

Generated workflows pin `--autoform-ref` or, by default, the HEAD commit of the
Autoform checkout running the command (or of the marketplace checkout an
installed plugin was copied from), when a safe remote (Autoform's canonical
repository, then `origin`, then `upstream`, then a unique remaining one) has a
cached tracking ref containing it, tracked files are clean, and the templates
and any installed plugin copy match it; an explicit `--autoform-source` gets no
default ref. Without a ref the CI workflows are omitted.

### project

Create or inspect a Lean project and list Autoform's bundled known-good release
pairs:

```bash
autoform project versions
autoform project new ./FiniteFlat --package FiniteFlat
autoform project new ./FiniteFlat430 --package FiniteFlat --lean-toolchain v4.30.0
autoform project new ./Gauss --package Gauss --library atlas
autoform project inspect path/inside/project --json
```

`project new` builds a complete Lean shell (the release's resolved dependency
manifest, blueprint, and site) in a private sibling directory and publishes it
atomically: the target must not exist and is never overwritten, and a failed or
concurrent creation leaves no partial target. A failure or interrupt after
staging can leave a hidden `.autoform-new-*` directory in the parent, which the
error mentions; inspect it before removing it. The published tree has modes 0755
for directories and executables and 0644 for other files, whatever the umask.
Its `lakefile.toml` sets `enableArtifactCache = true` and `restoreAllArtifacts =
true`, so checkouts on the same toolchain share builds through Lake's artifact
cache while each module's `.olean` stays in `.lake/build`. Package names cannot
shadow Mathlib libraries such as `Archive` or `Counterexamples`. Workflows are
pinned as [`init`](#init) pins them. Apart from local Git reads, the command
runs no subprocess, Lake, Lean, or network operation. Its errors name a code:

- `project-parent-inaccessible` or `project-path-is-symlink`: the parent and
  its ancestors must be readable real directories, not links; on macOS, use
  `/private/tmp` rather than the `/tmp` symlink.
- `project-parent-unsafe`: the parent is group- or world-writable and not a
  sticky directory owned by you or root; run `chmod g-w,o-w` on it.
- `project-parent-busy`: another creation held the parent's lock for 30
  seconds; retry once it is done.
- `project-parent-changed`: the parent path stopped naming the locked
  directory. Nothing was published, but an `.autoform-new-*` stage may remain
  in the original directory; inspect it before removing it.
- `project-create-commit-uncertain`: publication started but its outcome could
  not be confirmed; locate the project before retrying.

Creation fails closed, including on Windows, where POSIX descriptor traversal,
advisory locking, directory sync, or atomic no-replace rename is unavailable.

`--lean-toolchain` also accepts `v4.30.0-rc1` and `leanprover/lean4:v4.30.0`;
`--mathlib-rev` requires it, and neither combines with `--release`. A pair equal
to a catalog entry gets that entry's bundled manifest. Any other pair is written
without `lake-manifest.json`, with a `project-release-unlisted` warning: run
`lake update` in the project (it needs network access, locks Mathlib and
downloads its build cache), then commit the manifest. Use the toolchain that
Mathlib revision declares in its own `lean-toolchain`, or Lake may rewrite the
project's toolchain or fail to build. A toolchain older than v4.27.0 gets a
`project-lean-below-minimum` warning. Warnings go to stderr, or with `--json`
to the `warnings` array, where such a pair reports `"release": null`; the exit
status stays 0. Successful JSON uses schema `autoform-project-creation/v1`.

`--library NAME` names an entry of `autoform_cli/project/libraries.json`, which
pins a shared Lean library to a full commit on a listed release. The project is
created on that release with the library's `[[require]]`, and its manifest
already locks the library's packages, so it needs no `lake update`; the first
Lake command clones them, which needs network access. An unknown name is
`project-library-unknown`, and a package name that is one of the library's
module roots or packages is `project-name-invalid`. The JSON result adds
`library: {name, git, rev}`.

`project inspect` is read-only: it inspects the nearest enclosing Lean project
without running Lake, Lean, Git, or the network. Automation should use
`--json`, accept only `autoform-project-inspection/v1`, and branch on `ok`,
`compatibility.status`, and `diagnostics[].code`, not message text. `ok` and
compatibility are independent, and `supported` certifies release identity, not
build validity. Exit 0 means `ok: true`, exit 1 an inspection or catalog error,
and exit 2 invalid CLI usage. The
[project-inspection reference](project/README.md) defines the statuses, JSON
fields, and diagnostic codes. `project versions` lists the bundled pairs, the
ones `project new` can lock offline.

### Publishing and the dashboard

Publishing a project runs four steps in order: validate, refresh the vault's
dependency summary, render the site source, then strict-build the site.

```bash
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform check blueprint --lean-root .
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform-visualize blueprint
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform render blueprint \
  --output site-src --lean-root . --require-declarations
uv run --with mkdocs --with mkdocs-material --with mkdocs-literate-nav \
  --with pymdown-extensions mkdocs build --strict
```

Drop `--require-declarations` when reviewing work in progress, where a
statement may name a Lean declaration that does not exist yet. On native
Windows, run `check` and `render` without `--lean-root`; the site then has no
code links ([publication contract](reference/runtime-and-publication.md#publication-contract)).
Pass a Lake project in a subdirectory, such as `v1/`, as `--lean-root v1`;
links name paths from the repository's top level. Edit the `--lean-root .`
that the generated `blueprint-pages.yml` passes to `check`, `audit` and
`render` to match.

`autoform dashboard . --site-dir site` then serves the built site on
`127.0.0.1` until you stop it, with a read-only live overlay of current author
claims at `/__autoform/live.json`, which only the loopback server exposes. The
overlay matches claims on an article's node ID or its `article_id`, reports the
`claim_target` each lease used, and is never written into the vault,
publication manifest, or public site. Claim badges update while it runs; re-run
render and the MkDocs build to refresh everything else.

The generated `blueprint-pages.yml` deploys only a commit that the project's own
`.github/workflows/autoform-verify.yml` (matched by file, not name) passed on a
push to `main`, with code links rendered at that commit by `--ref`. A failed
verification deploys nothing, so the last verified site stays up. Pull requests
and manual runs build the site but never deploy, and cannot cancel a waiting
deploy. To redeploy, re-run the Pages run that the newest successful verify run
on `main` started; re-running an older one deploys the older commit. Deleting or
renaming `autoform-verify.yml`, or disabling it in the repository's Actions
settings, stops every deploy, with no error. A project scaffolded before this
rule deploys every push; [Upgrading](../CHANGELOG.md#upgrading-from-050) says how
to switch.
No pin has to move: every `AUTOFORM_REF` since 0.5.0 has `render --ref`.

Pages gates on `check` and `render`, and runs `audit` after `check` and
`site check` after the MkDocs build only as reports, with `continue-on-error`:
`audit` logs every finding, or `OK: roadmap audit passed`. An invalid coverage
contract fails `render` before it writes anything, but `MAPPED` rows (each an
audit `declared-coverage-gap`; a new project lists the template's row) and
coverage-role issues, which `render` prints as warnings, still publish. A
project scaffolded before a report step can add it by hand, with
`continue-on-error: true` and the same `uvx --from` source:
`autoform audit blueprint --lean-root .` after `check`, and
`autoform site check site --source site-src` after the MkDocs build, which needs
an `AUTOFORM_REF` from 0.6.0 or later. To block merges on findings, run
`autoform audit` in CI without it.

### check

Validate structure, and optionally check that every `lean:` name exists in the
project's Lean sources:

```bash
autoform check blueprint --lean-root .
```

`check` validates the graph contract ([Validation](reference/validation-and-audit.md#validation)).
With `--lean-root` it resolves names only in the project's own Lean files, not
in Mathlib or other dependencies under `.lake`; when a name is not found, a note
at the end of the output says that a shared library's declaration belongs in
the article's implementation note.

With `--lean-root`, `check` also warns about each declaration of a usable,
checked-out shared library (see `autoform library list`) whose own docstring
cites an arXiv paper or DOI that an unproved article reaches through its
`## Sources` links, or through a blueprint Markdown page such a link names
(link text, and pages over 4 MiB, are not read). Identifiers match in their
usual forms: versions; `abs`, `pdf`, `html`, `e-print` and `src` URLs;
old-style ids such as `math.AG/0210149`; arXiv's DOIs; `https://doi.org/` and
`dx.doi.org` links, percent-encoded or not; and the labels `arXiv:`, `arXiv`,
`doi:` and `DOI`, also in backticks, emphasis or a table cell. Silence it by
naming the declaration in full in an implementation note, a page under
`blueprint/sources/` or one a `## Sources` link names, or in project Lean, in
full or by its last name component. Declarations citing a source only through
their module docstring share one warning, silenced by naming the module, or in
full a citing declaration; importing the module names it, importing only an
umbrella module above it does not. A module catalog, or a container whose scope
holds a target and is all proved, reaches nothing; a bare number cites nothing; and a
last-component match counts any identifier, comments and strings included, so a
generic one such as `symm` can hide the warning. Reuse such a declaration and
name it in the note of each article using it; when it does not apply, name it,
with why, on the source's page under `blueprint/sources/`.

`check` also warns when a library's index cannot be read or parsed, when a
library with a citing declaration fails verification against its checkout, and
when the project's Lake files cannot be used. None of these warnings changes the
exit status.

### audit and coverage

```bash
autoform audit blueprint --lean-root .
```

`audit` adds deterministic completeness, provenance, coverage, checked-fact,
and optional Lean-target checks ([Audit contract](reference/validation-and-audit.md#audit-contract)).
It is read-only and offline: it contacts no network service and writes no
findings into the blueprint. Pass `--json` for stable machine-readable output.
A nonzero exit status means at least one finding;
[warnings](reference/validation-and-audit.md#warnings) never change it.

The machine-checkable `coverage/README.md` contract contains one
`Area | Coverage | Evidence` table, one disposition per row:

- `MAPPED` is nonterminal.
- `INVENTORIED` is terminal for exact source accounting and links a
  `catalog: module` record, directly or through a containing roadmap scope:
  `coverage-role-mismatch` names a link that reaches none, and
  `unclassified-inventory` the first catalogs no such row reaches.
- `DECOMPOSED` links source-grounded declaration articles, directly or through
  a containing scope. Links that reach only catalogs are a
  `coverage-role-mismatch`; any other link reaching no declaration leaf, such as
  prose or `not_ready` leaves that declare nothing yet, is a
  `decomposed-without-declaration` warning.
- `DEFERRED` and `OUT` record an explicit later milestone or exclusion.

Audit JSON includes canonical rows, the exact coverage source hash, and counts,
which always have an integer `INVENTORIED` key; `publication.json` records only
aggregate counts. To record both axes of one source area, use distinct
axis-qualified `Area` labels, unrelated to an article's `area:` field:
`Repository inventory / MathlibExt` may be `INVENTORIED` while
`Mathematical exposition / MathlibExt` is still `MAPPED`. Duplicate exact area
labels are invalid. A broad inventory row may link a container and classify
every catalog below it.

The contract is read as published Markdown and fails closed; `coverage.py`
gives the edge cases:

- A table in an HTML comment or a code block is not the contract, nor is a
  table, row, or cell under its own or an ancestor's `hidden` attribute; CSS,
  such as a class or `display: none`, hides nothing. A table that does not
  publish, such as one a paragraph runs into, is rejected, and a published one
  the audit does not recognise, such as one without outer pipes, is named.
- A comment or code block between rows is reported, with the rows stranded
  below it, rather than truncating the contract. A row holding an unclosed
  `<style>`, or evidence with a raw `<table>`, is refused.
- Evidence holding only a code span, a comment, emphasis, an HTML tag, an
  entity, an empty link such as `[ ](notes.md)`, or hidden text is rejected,
  and so is evidence that is only `TODO`, `TBD`, `pending`, `placeholder`, or
  `unknown`, or opens with one as a marker (`TODO: choose a milestone`).
  "Pending Mathlib PR 1234" passes.
- `INVENTORIED` and `DECOMPOSED` evidence needs a complete inline link to an
  existing roadmap article, and each linked scope must contain the claimed role;
  audit reports a row that does not, and `render` warns. Every local link must
  resolve, fragments included.

#### What coverage completeness does and does not claim

`coverage.complete` in audit and `publication.json` means only that no declared
row is still `MAPPED`. `INVENTORIED` closes exact source accounting and
`DECOMPOSED` says the area has source-grounded declaration articles; neither
implies the other. It does **not** claim the rows cover the source
exhaustively, which no local check can judge, nor that linked articles are
formalized or proved.

Target metrics (the denominator, readiness count, and progress breakdowns in the
book and `autoform check`) count non-container articles carrying `declaration`,
never a `catalog: module` record. A scope's status bar, item count, and
dependency-map legend also count `not_ready` leaves that declare nothing yet, so
deferred work holds its scope open, and each catalog under a neutral status,
`inventory checked` once fully proved and `inventory pending` until then, which
never changes the scope's rolled-up status; a target that depends on a catalog
still waits for it.

### skeleton

Extract what a reader must trust for each formalized statement:

```bash
autoform skeleton blueprint --lean-root .
autoform skeleton blueprint --lean-root . --node chapter/main-result
autoform skeleton blueprint --lean-root . --output skeleton.json --packets review-packets --passages review-passages
```

The skeleton of a `lean:` declaration is its elaborated signature, then every
project declaration its *statement* rests on, transitively, quoted from the
sources in dependency order: a definition with its body, a theorem with its type
only. A theorem's proof is never emitted, though Lean's axiom analysis reads it,
so a proof edit can change the reported trust context. Each skeleton reports the
axioms the declaration rests on (a `sorry` shows as `sorryAx`) and, by name, the
non-core constants it assumes from Mathlib or another dependency, which are
trusted and not expanded.

For each module that declares a selected target, `skeleton` runs a probe with
`lake env lean` against the built project, so a packet reads as it does to a
file importing that module, without the declaration's local notation, local
instances, `open`, or `set_option`. Lake must first confirm, without
rebuilding, that every probed module is fresh (a missing `lake-manifest.json`
or stale artifacts fail the check); the check rewrites Lake's `.hash` files, so
`.lake` must be writable. The helper module `autoform-skeleton-helper` and its
`AutoformSkeleton` namespace are reserved; a module that takes either, on
`LEAN_PATH` or not, breaks the probes that import it.

Generated companions, such as constructors, projections, recursors and matchers,
fold onto the declaration the reader sees, so a structure appears once, as its
`structure` block. A `partial def` in a trusted closure is refused, and so is an
ordinary declaration with no source of its own, such as one a metaprogram adds
under an existing name; a theorem or axiom shows only its type, and an internal
helper such as `f._unary` is marked `-- source not shown`. A `lean:` name is
unresolved when it is absent from the sources or the built environment, Lean
declares it in another module than the one whose source holds it, the sources
define it more than once and the rules under
[Validation](reference/validation-and-audit.md#validation) choose none, it
reaches a refused declaration, or its probe fails on its own (the reason quotes
Lean's output). That affects only its article: the others still extract, the
command exits nonzero, and nothing is written into the vault. Limits apply per
probe, so a declaration that resolves alone can be unresolved beside its
module's other targets; one that resolves has the same hash under any selection.
A missing `lake`, a failed freshness check or helper build, a dependency library
rooted at `Init`, `Lean`, or `Std`, or a blueprint or source tree that changes
during extraction stops the whole command.

`--output` records the `autoform-skeleton/v5` report, with no timestamp or
absolute path. It names the target set, whether it covers every target or a
`--node` selection, and the exact blueprint, by a hash of every article and the
passage each cites; a report whose selection, articles and unresolved
declarations disagree cannot be built. Only a report that selects every article,
with the blueprint's `lean:` names as targets, is review evidence.

Run skeleton extraction only in a trusted checkout or an operating-system
sandbox: Lake evaluates `lakefile.lean`, and the probe imports project code
that may perform arbitrary IO and can forge probe output. The Lake freshness
check, the helper's build, and each probe have their own 600-second budget;
`--timeout SECONDS` sets the helper's and each probe's. At most eight probes
run at a time. These limits are resource controls, not a security or
authenticity boundary.

`--packets DIR` writes one comment-stripped packet per skeleton, named after
its article and declaration, with a manifest of articles and hashes; a blind
auditor gets the hash-named packets of `review prepare` instead. The
destination must be empty or hold Autoform's packet manifest. Each run replaces
the whole managed tree, aborts if the tree changes before commit, and preserves
an old tree that changes later at a reported recovery path. Packet and passage
manifests use their v2 schemas; v1 trees are recognized and replaced. An
unresolved selected declaration blocks all packet and passage output, though
`--output` still records the report. Destinations must be disjoint, not
symlinks, and on filesystems with hard links and atomic renames. Combined
outputs commit together and a failed commit restores the previous set, but a
reader may briefly see outputs of different generations.

A packet holds the signature, the same signature printed raw past project
notation, the canonical kernel material, the statement as written (cut before
its value), and the source of every project definition the statement rests on,
with comments and docstrings removed. Source that may hide a comment the probe
cannot find, such as a body using `local notation`, is withheld, as is an
equation-style statement that cannot be parsed outside its file; both are
marked `-- source not shown`, and the article stays resolved, so editing a
theorem's proof changes its packet only when that makes such a statement parse
or stop parsing outside its file. A `local` token, or a token the file had in
scope differently from the probe, can make the probe show code as a comment or a
comment as code. Source text is not notation-proof; only the kernel material is.

A local `## Sources` link with a `#L<start>-L<end>` fragment, such as
`../../../sources/lebl-ra/ch-real-nums.tex#L693-L714`, is a locator naming the
statement's source passage, unless it names an existing regular Markdown file
in the blueprint, `.md` in any letter case, which is a note. A first local
locator that names no readable, regular, UTF-8 lines inside the blueprint, or
names a file over 64 MiB, leaves its article's declarations unresolved.
`--passages DIR`, which requires `--packets`, writes each article's passage to
its own managed directory beside `article.lean`, the joint packet of the
article's declarations: a faithfulness judge gets the article packet and
passage, an auditor of one declaration only its packet.

#### Skeleton hashes

All are SHA-256. An article with several `lean:` names has one drift hash and
one review hash over them, null if any is unresolved. In the report,
`trusted`, `assumed`, and `boundary_modules` hold the whole closure, and
`statement_assumed` and `statement_boundary_modules` the part the statement
reaches, which is all the packet shows.

| Hash | Covers | Changes when |
| --- | --- | --- |
| drift hash | Elaborated root, trusted declarations, direct external assumptions and axioms, dependency edges, Lean version, `.olean` identities of the external boundary | Meaning, axioms used, the toolchain, or an external module changes; not spelling or comments |
| statement hash | The same without the axioms and what only they reach | Meaning changes, a proof inside a trusted definition included; not a theorem's own proof |
| evidence hash | The exact packet bytes | The packet text changes |
| article review hash | Joint packet, cited passage and locator, drift hash | Any of those changes |
| `review_approved` | Title, statement as the site shows it, passage and locator, joint and per-declaration packets, TeX macros the statement uses, each valid read-back's model and testimony | Any of those changes; not a proof edit, or a Lean or Mathlib bump, that leaves the packets unchanged |

None is a stable statement identifier, reviewer authentication, or an approval
key; all are advisory checksums when candidate code controls the checkout.

#### Read-back cards

A read-back is a blind agent's mathematical-English account of one packet,
recorded with `review record`. Cards live at
`blueprint/readbacks/<article_id>/<encoded-declaration>.md` and hold the exact
packet, its evidence hash as `packet:`, the statement hash as `statement:` (a
record, never compared), an unauthenticated `model:` label the coordinator
supplies, and the testimony. Loading rejects missing or unknown fields,
altered packets, identity mismatches, non-canonical cards, and malformed or
empty testimony.

Testimony is plain text with TeX formulas between dollar signs, written by the
rules in `skills/human-review/references/readback.md`. It is refused for
invisible or reordering characters, combining marks stacked over the lines
around or with nothing to sit on, showing no letter or digit, or exceeding
32 KiB or 500 lines; a write also refuses code points unassigned in its
Python's Unicode data. A formula never invalidates testimony: one
`testimony.formula_problems` cannot typeset is shown as typed, and render warns
without changing its exit status.

A card file holds at most 4 MiB, and its frontmatter is read from its first 32
lines. A larger, non-UTF-8, or unreadable file, or a directory or FIFO, at a
card path is reported as an invalid card, as is a card recording another
declaration than its path names or whose `.md` is in another case; linked cards
are skipped, and an unlistable directory under `readbacks/` stops loading. An
oversized card must be removed by hand. A write refuses a link on the way to the
card directory, refuses `--expected-card-hash` for a missing card, and takes the
directory's `flock`, giving up after ten seconds; it is refused before creating
anything where `flock` is missing, Windows included, though cards still load
there. A crash can leave a `.autoform-readback-*.tmp` file, which the loader
ignores; the committed card is the durable copy. Do not edit cards while a write
runs. A write gives `readbacks/` a `.gitattributes` of `* -text`, since a card
with CRLF line ends is invalid, and Git for Windows needs `core.longpaths` for
the paths of long declaration names.

### review

Review a formalized statement with three commands, then publish the review:

```bash
autoform review prepare blueprint --lean-root . --packets review-packets
autoform review record blueprint --packets review-packets \
  --article-id af_0123456789abcdef01234567 --declaration Ns.result \
  --testimony read-back.txt --model muse-spark-1.1
autoform review check blueprint --lean-root .
autoform render blueprint --lean-root . --review
```

`review prepare` extracts every Lean-mapped article from the built tree and
writes one blind packet per declaration under `blind/`, named by its hash, with
a manifest for the coordinator. Each Lean-mapped article must be a
declaration-sized leaf, or review commands refuse it with
`review-article-shape`; module catalogs are not reviewed. An `origin: cited`
article must link an in-vault, non-Markdown snapshot with an exact
`#L<start>-L<end>` range, no article may cite blank lines, and preparation
refuses a citation it cannot show.

`review record` runs no Lean. It refuses a packet whose bytes do not match its
hash, a packet or testimony that is not a regular file (a FIFO is refused
unread), an `article_id` the blueprint no longer has, and refused testimony,
then files the card; replacing a different card needs `--expected-card-hash`.
It warns about each formula a site would show as typed, with its exit status
unchanged. A card from a stale packet is invalid under `review check`, so
prepare and record again.

`review check` derives its evidence from its own extraction, never a committed
file, and fails until every Lean-mapped article has current cards and a
`review_approved` equal to the review hash it prints. An approval also binds the
macros in `blueprint/tex-macros.json` its statement uses, directly or through
another macro, so a macro edit stales only those approvals. The run compares the
articles, cited sources, and cards it read before Lean with what it sees after,
and a difference fails with `review-snapshot-changed`; rerun once the blueprint
is idle. These are comparisons, not a lock: an edit restored between them goes
unseen, and `tex-macros.json` is read once and compared with nothing.
`review prepare`, `review check`, and `render --review` take the same
`--timeout SECONDS` as `skeleton`.

`review prepare` and `review record` exit 2 on input they cannot derive, read,
or trust, on refused testimony, and on a card conflict. `review check` exits 1
on findings, and 2 on evidence it cannot derive or a blueprint that changed
during the run.

`render --review` makes the same check from its own extraction and puts a
*Review* disclosure under every formalized statement: the approval state, the
review hash, the cited passage, and for each declaration its card (current,
invalid with reasons, or missing) before the exact packet; the statement hash,
assumptions, and axioms are marked not bound. An edit during extraction fails
with `review-snapshot-changed`. A passage cut from a file the site leaves out,
such as a script, a file under `readbacks/`, or `atlas.json`, is refused with
`review-source-unpublished`. Articles in `published: false` folders are
reviewed but get no disclosure or warning, and module catalogs get none.
`--timeout` is accepted only with `--review`, and a refused review prints its
reasons on stdout and exits 1.

The gate is opt-in: commit `blueprint/.autoform-review` holding
`autoform-review-policy/v1` and a line end; `autoform init` never writes it.
With the marker, generated CI runs `review check` after the Lean build,
without making advisory roadmap or coverage findings merge blockers, and
deleting every card cannot turn it off. Without it, cards or `review_approved`
fail CI.

`review_approved` is a drift checksum, not an authorization: anyone, an agent
included, can paste the hash `review check` prints, and a current approval is
labelled self-approved wherever it is shown. Authorization needs a CODEOWNERS
rule for `/blueprint/` (in `CODEOWNERS.autoform.example`, activated as under
[Open statements](reference/open-statements.md#open-statements)) and a
default-branch ruleset that requires code-owner review, dismisses stale
approvals, and requires approval of the latest push.

### work

Inspect the current Markdown-derived formalization frontier:

```bash
autoform work list . --lean-root .
autoform work list . --lean-root . --json
autoform work context chapter/result . --lean-root . --json
```

`work list` returns the formalizable leaves whose next statement or proof phase
is unblocked, by the rule behind the derived `can_state` and `can_prove` states
and the site's Next up card (see [Open
statements](reference/open-statements.md#open-statements)). Under
`open_statements: allowed` a theorem's statement phase waits only for its
statement prerequisites, and a proof phase for every prerequisite, to be stated.

`work context` takes the path-derived node ID or an `article_id` and reports the
article, dependencies, source and Lean targets, blockers, article and graph
source revisions (the article revision hashes that article's bytes alone), and
the claim target, which prefers the `article_id`. A Lean target's `source_file`
is relative to `--lean-root`, and null without one or when the local scan, which
skips build output and nested checkouts (any subdirectory with a `.git` entry),
finds the declaration nowhere or several times with none chosen. Blockers are
unmet dependency IDs or `roadmap:not-a-formalizable-leaf`,
`roadmap:missing-article-id`, `roadmap:missing-article-revision`, or
`roadmap:not-ready`. `work list` fails if an unfinished formalizable leaf lacks
an `article_id` (add them with `autoform migrate article-ids --write`); `work
context` still selects such an article by path ID to report the blocker. An
article recording `statement: retracted` is a revision: JSON `revision` is true,
and the text adds a `revision:` (list) or `Revision:` (context) line saying to
start from `autoform work impact` (see the [revision
contract](reference/claims-and-revisions.md#revision-contract)). Both commands
are read-only and refuse an article whose bytes changed after the graph loaded;
rerun once the project is idle.

Under the open policy the text of `work list` starts with an `Open statements:
allowed` line and adds an `assumes:` line under each item resting on open
statements; `work context` prints `Open statements:` and `Assumes:` lines. JSON
uses `autoform-work/v2`: the frontier and every item carry `open_statements`,
and every item carries `assumes`, the open statements its proof rests on (empty
under the strict policy), and `revision`.

List the open statements and the articles that rest on them:

```bash
autoform work assumptions .
autoform work assumptions blueprint --json
```

`work assumptions` prints the policy, an `open:` line per open statement with
its declarations and the open statements it assumes, a `conditional:` line per
conditional article, and an `unproved:` line per other listed article that
assumes open statements. `--json` writes the `autoform-assumptions/v1` contract
CI audits the build against: every article whose `lean:` names a declaration,
with `open`, `assumes`, and `allowed_open_declarations` (the open declarations
its Lean may reach, plus its own when it is open). A `mathlib: true` article
(state `mathlib`) is never open and is allowed nothing, nor is any article under
the strict policy. Under the open policy, a module catalog that is not proved
keeps only the names no non-catalog article's `lean:` lists, and is left out
when none remain. CI checks that every listed name exists. It
reads Markdown only and needs no Lean build.

#### work impact

Ask what revising an article's Lean declarations would affect before editing
them:

```bash
autoform work impact chapter/result . --lean-root .
autoform work impact chapter/result . --lean-root . --declaration MyProject.helper --json
```

`work impact` runs a bounded Lean probe against a fresh build and reports
statement-impacted and proof-impacted articles, unnamed helpers, missing
Markdown dependency paths (`undeclared_dependencies`: impacted articles with no
Markdown path to the revised article, to one naming a revised declaration, or,
when none names it, to an owner), deprecated declarations and their users,
whether the revision is contained (the selected article's target is its only
claim target), and the complete `claim_targets` set. A helper shared by several
articles adds every owner's claim; an unowned helper, or a revised declaration
no article names, is claimed under every nearest owner's target or a stable
`lean/<slug>-<digest>` target. `unused_statement_dependencies`, the stated
articles whose Markdown statement rests on the revised declarations, directly or
through other statement dependencies, but that the probe does not find
statement-impacted, join `claim_targets` too. `mathlib: true` articles are
left out. The probe refuses a loaded root-package module missing from the
repository's source inventory, which skips nested Git checkouts, ignored
directories such as `build`, and symlinked directories, and a module an `M.*`
or `M.+` glob finds that it did not load.

The command refuses if the roadmap or the Lean sources change during the probe.
JSON uses `autoform-impact/v1`, bound to the Markdown `source_revision`, the
repository `lean_source_revision`, and a `build_revision` hash of the normalized
Lean records. A missing or stale build is reported as for `work import-impact`.
Changes to notation, attributes, instance priority, or unreported generated
declarations still require human review. Both impact probes import project
modules, so run them only in a trusted checkout or sandbox.

#### work import-impact

Report the coordination and review scope of an import edit without forbidding
the edit:

```bash
autoform work import-impact SELECTOR MODULE [TARGET] --lean-root PATH [--json] [--timeout SECONDS] [--module-scope SCOPE]
```

`import-impact` is advisory and edits no authored roadmap or Lean source, though
its Lake freshness check may refresh build hash sidecars. By default the probe
imports every module of the root libraries, so each must be built in this
checkout (a build in another clone or worktree does not count): a missing or
stale build exits 2 with the `lake build` command for those libraries, how many
modules were checked, and the first one Lake found unbuilt. `affected_modules`
is `MODULE` and every loaded module that imports it, transitively, complete over
the root libraries' Lake roots and globs. Modules that removing an import drops
from the build are not listed. Like `work impact`, the probe refuses a
root-package module the captured inventory lacks, and one whose loaded source
or artifact is not the captured root one, so a dependency cannot shadow it.

Under `--module-scope import-closure` the candidates are `MODULE` and every
root-package module whose source contains each component of a candidate's name,
repeated until none is added; Lean loads them and what they import. Only those
modules must be built, and stale-build advice names only them. An article owns a
declaration when one of its `lean:` names equals it or is a dotted prefix of it,
so `affected_articles` may list more articles and `article_local` is always
`false`; `affected_unowned_helpers` keep the parent-chain rule, and
`unresolved_articles` leaves out a name the source index places outside
`affected_modules`. A `.lean` file outside the captured sources, as in
`.lake/packages` or a path dependency, that may import a candidate makes the
command exit 2 and ask for the default scope; use the default scope too when a
component of a module name is a common word.

JSON uses `autoform-module-import-impact/v1`, with `module_graph_revision` and
`module_scope` (`loaded-root-library-graph` or `import-closure`). `article` is
the selected article (`id`, `article_id`, `claim_target`). `claim_targets` holds
only it and `module_claim_target`, `lean-module/<lowercase-slug>-<sha256[:16]>`,
whose key is `module_claim_key`. `affected_claim_targets` are downstream
warnings, not ownership requirements; `claim_keys` and `affected_claim_keys`
match the JSON of `autoform claim list --worker-id WORKER` given those targets.
`affected_articles` lists each article that names, or owns through the parent
chain, a declaration in an affected module, with those `modules`, and
`selected_affected` says whether the selected article is one.
`unresolved_articles` reports local declarations absent from the loaded
environment without blocking unrelated edits, and each
`affected_unowned_helpers` entry carries its own claim target and key.
`article_local` holds only when `selected_affected` does,
`affected_claim_targets` is empty, and no other article has unresolved
declarations.

Imports are allowed regardless of reported breadth. Follow the [Formalize import
workflow](../skills/formalize/SKILL.md#import-edits) for baseline evidence,
conflict checks, claim lifecycle, and post-build review.

### search

Ask whether the blueprint already holds a result before adding an article or a
Lean helper:

```bash
autoform search . "separating hyperplane" --lean-root .
autoform search . "non-ambiguous" --lean-root . --json --limit 20
autoform search blueprint "interlacing" --state proved --state fully_proved
autoform library list .
autoform library index . --lib MathlibExt
```

`search` takes the project or blueprint directory, then one quoted query; both
are required. A query that starts with `-` goes last, after `--`. It exits 0
with or without hits. See the [search
contract](reference/search.md#search-contract).

### migrate

Plan durable article identity metadata, and add it:

```bash
autoform migrate article-ids blueprint --json
autoform migrate article-ids blueprint --check
autoform migrate article-ids blueprint --write
```

An `article_id` is `af_` plus 24 lowercase hex digits. Without `--write` the
planner is read-only: it validates uniqueness and proposes deterministic IDs,
with exact source hashes, for articles that lack one. `--write` only adds each
ID as the first frontmatter line, or as a frontmatter block of its own. It
checks every article before writing the first, writes one only while its bytes
still have the planned hash, and refuses one that changed or became a symlink; a
rerun completes the plan. The runtime and `autoform work` see assigned IDs at
once.

### claim

Coordinate temporary cross-machine ownership without modifying the book:

```bash
export AUTOFORM_WORKER_ID="agent-name"
autoform claim acquire af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
autoform claim renew af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
autoform claim release af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
autoform claim list af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
```

Claims are optional: use them when other people or sessions may work on the same
project at once, or when the user asks; a single Formalize run needs none. A
claim excludes only workers that also claim. Claim an article by the
`claim_target` that `work context` reports: claims on its path ID and on its
`article_id` do not exclude each other. Each concurrent agent needs its own
worker ID, since a second acquire by the same owner succeeds; where shell state
does not persist, pass `--worker-id` on every command. Leases expire after 1500
seconds unless `--ttl` sets another length; renew well within that, and confirm
a claim with `renew`, not `acquire`, which also succeeds on an expired or
released lease. Acquire a whole set in one command (see the [claim
contract](reference/claims-and-revisions.md#claim-contract)).

Claims are fail-closed compare-and-swap leases under `refs/autoform-claims/` on
the Git `origin`; pass `--repo` for another board. A failed acquire or renew
means the caller cannot prove ownership and must stop before committing or
pushing protected work. Claims prove no mathematical correctness and do not
replace branch-level Git CAS.

### debrief

Ask the agent that led a Setup, Roadmap, or Formalize run what got in its way,
and draft an AutoformBot issue from the answer:

```bash
autoform debrief orchestrator form . --skill formalize
autoform debrief orchestrator record . --skill formalize < /tmp/answer.json
```

Orchestrator debriefs are on by default: `form` prints the form unless
`AUTOFORM_DEBRIEF` is `0`, `false`, `no`, or `off`, or the platform cannot store
the answer, and otherwise says the step is disabled. The form asks separately
for friction from the user's local setup (`local_setup_friction`) and with
AutoformBot itself (`autoform_friction`). `record` reads one JSON object
answering the form from `--answer` or standard input, refuses anything else, and
stores it under `runs/` with the installed AutoformBot version; a directory
without a roadmap, as during Setup, is accepted. When `autoform_friction` is not
empty, `record` writes a draft issue for `facebookresearch/autoform-bot`
under `issues/` and prints its path. The draft holds only that friction and the
environment, with the project path replaced by `$PROJECT` and home directories
by `~`, and ends, below a `---` footer, with a link to GitHub's new-issue form
pre-filled with the draft as recorded (only its title when the draft is too
long, as the footer and `record` say), so edit in the form. Nothing is sent.
With `--json`, `record` prints `{"run_id": ..., "path": ..., "issue": null |
{"path": ..., "url": ..., "url_has_body": true | false}}`.

Ask the agent that just finished a formalization attempt what infrastructure
would have made it cheaper, and keep its answer:

```bash
autoform debrief subagent form af_5b0e4d3c2a1f09e8d7c6b5a4 . --phase proof
autoform debrief subagent record af_5b0e4d3c2a1f09e8d7c6b5a4 . --phase proof \
  --worker-id agent-name < /tmp/answer.json
autoform debrief subagent render .
```

Subagent debriefs are opt-in: `form` prints the form only when
`AUTOFORM_SUBAGENT_DEBRIEF=1` on a platform that can store the answer, with the
outcome filled in from the runtime: the phase succeeded when the article's
statement (for `statement`) or proof (for `proof`) is now recorded as
formalized. `record` takes one JSON object as above, re-reads the outcome from
the runtime, stores a new record, and never edits the project; it requires
`--worker-id` or `AUTOFORM_WORKER_ID`. With `--json` it prints `{"attempt_id":
..., "outcome": "succeeded" | "not-succeeded", "path": ...}`, `path` being the
stored record. `render` rebuilds `views/` from the validated records, escaping
agent text and deleting reports whose record is gone.

Both kinds share one store, `autoform/debriefs/` in the repository's Git common
directory, which every worktree shares and Git does not track, or outside Git
`$XDG_STATE_HOME/autoform/debriefs/<project>-<hash>/`. Set
`AUTOFORM_DEBRIEF_DIR` to an absolute path outside the Git working tree to
choose another. A record is never overwritten; platforms that cannot retain
directory descriptors, such as Windows, refuse to read or write the store.

### visualize and render

Refresh the vault's dependency summary, `blueprint/dependencies.md`:

```bash
autoform-visualize blueprint
```

The summary is plain Markdown built from the explorer's project map (the
scaffolded vault ignores it): the statuses, one row per chapter with its items,
ready work and dependencies both ways, the articles while the roadmap has at
most 120, and how to open the explorer. Its front matter carries `kind: graph`
and `autoform_generated: true`. It replaces a missing page, one it wrote, or the
Mermaid page earlier versions wrote, and exits 2 refusing any other page.

Build the publishable site source, with links to Lean declarations at the
current commit:

```bash
autoform render blueprint --output site-src --lean-root . --require-declarations
```

`render` never writes into the vault. Once the roadmap has articles, the landing
page combines the mathematical reader and dependency map in one full-page client.
`structure.md`, `dependencies.md` and old dependency-page URLs enter canonical
article or chapter-card reading, with bounded maps, authored areas and global search.
Complete published statements, proofs and citations remain the reading content.
The index `dependencies/index.json`, project payload `dependencies.json`, and
other `dependencies/` payloads are internal to this viewer; their schemas may change.
A formal statement links to its canonical article or containing chapter card;
legacy `#node=<id>`, view and filter links remain aliases, not a second viewer.
A `lean:` declaration resolving to a repository permalink gets a source link;
a catalog's Lean row shows its first eight names and counts the rest.
Render percent-encodes each
link it writes from file and folder names, such as the book's previous and next
links; a Markdown link it moves keeps its decoded spelling and starts with `./`
when its first segment holds a colon, so no name can make a `javascript:` URL.

Render writes no Mermaid graph of its own. Authored Mermaid remains supported in
the vault and book, drawn in strict mode with plain-text labels and loaded only
on a page that has one: click callbacks and HTML in labels do not work, links to
URLs do. Point `mkdocs.yml` at `docs_dir: site-src` and enable `md_in_html` and
a `pymdownx.superfences` mermaid fence (see the [setup
template](templates/mkdocs.yml)); list the generated
`javascripts/blueprint-mermaid.js`, not the Mermaid library. The [publication
contract](reference/runtime-and-publication.md#publication-contract) gives the
schema and when render replaces an earlier site.

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
keep it outside `blueprint/`. An Obsidian `.canvas` file is left out silently,
as `.obsidian` is, and a Markdown page that is not UTF-8 with a warning. A link
to a file the site leaves out, a hidden path outside `sources/`, or a page in a
`published: false` folder keeps only its text; `check` and `render` warn about
each such link to a hidden path or an unpublished folder.

#### MathJax

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
copy of `tex-macros.json` that `render` reads once and publishes. Each read-back
card has its own TeX input with only base, ams, and noundefined. Only renderer-marked
formulas, such as `$...$` in Markdown, are typeset. Formulas in links, controls and
navigation get no extra MathJax tab stop, menu or accessibility explorer. Refreshes
retain earlier glyph styles; graph labels, metadata titles, `discussion:` values,
home-page leads and Material navigation/ToC stay typed. Explorer reading links and
breadcrumbs typeset publisher-derived title parts with project macros. A formula paints only within
its own line and block, and an inline formula wider than its paragraph is cut at
its edge, so write one that wide as a display, which scrolls.

### Upgrading from an earlier Autoform

What an existing repository meets when it moves to a new version, including what
CI at an older `AUTOFORM_REF` rejects, is in the
[changelog](../CHANGELOG.md#upgrading-from-050).

## Contracts

Each area's contract has its own page:

- [Validation and audit](reference/validation-and-audit.md): what `check` and `audit` refuse or warn about.
- [Search](reference/search.md): `autoform search`, and searching a library the project depends on.
- [Open statements](reference/open-statements.md): conditional results and the open-statement policy.
- [Claims and revisions](reference/claims-and-revisions.md): leases and revising a published statement.
- [Runtime and publication](reference/runtime-and-publication.md): `doctor`, the in-memory runtime projection and the published site.
