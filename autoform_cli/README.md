# Blueprint format and CLI

The Autoform CLI validates, visualizes, and publishes the multilevel dependency
graph in `blueprint/roadmap/`. The Markdown book is the graph.

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
pair with a later article's fence and hide what lies between, so `check` warns
([Validation](reference/validation-and-audit.md#validation)) and `audit`
reports `unclosed-code-fence`. A fence line inside an HTML comment opens no
fence, in render too. The graph and an article's statement and sections read an
unclosed fence as plain text, even where the page pairs its backticks with a
later run as inline code, and a later bare line of its character at least as
long closes it. Coverage files and the unpublished-folder link check read it as
open to the end of the file, and so does render when it rewrites links, orders
the book and places a chapter's statements. An HTML comment that never closes,
even one opened in inline code or an indented code block, hides every later
heading and dependency link: the graph loses those edges, one opened in the
statement puts the rest of the article in the theorem box, and nothing reports
it.

`## Depends on` lists what the article needs to be *stated*,
`## Proof depends on` what only its *proof* needs, and `## Informally uses` what
the source's proof cites but the Lean proof need not use. Links under them
resolve relative to the article and must point at another roadmap article,
except in a code block or an HTML comment; other links are navigation or
citations. Status never counts informal links; `check` warns about a bad one.

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

The optional `area` field groups the chapters, and deeper containers naming a
chapter's area, into regions of the project map, and Topics heads each area's
chapters with its name; on a leaf it only labels the article. Use mathematical
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
article inside the collection instead. On a declaration article, `check` warns
about any role but `topic`, which the article ignores.

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
`catalog-missing-ledger`; the ledger link follows the [Sources
rules](reference/validation-and-audit.md#validation).

A completed catalog records in `lean:` the full name of every public
declaration the module writes with a declaration keyword, as the Lean source
index records them; a structure field the index records may go there instead
of the ledger. Names Lean generates, such as constructors,
derived or unnamed instances, and `@[ext]`, `@[simps]`, or `@[to_additive]`
results, and declarations the index cannot see yet, behind `nonrec` or `meta`
or written `class inductive`, go in the ledger as supporting entries, never in
`lean:`; a `private` name never goes in `lean:`.
A completed catalog may assert `statement: formalized` and `proof: formalized`
once the complete module has been checked.

`check --lean-root` and `audit --lean-root` resolve every name the catalog
lists and, for a name they do not find, say that a generated name belongs in
the ledger. They cannot prove the list omits no newly added public
declaration; completeness remains an authored assertion. `audit --lean-root`
applies the [elaboration-limit](reference/validation-and-audit.md#elaboration-limits)
warnings and `lean-target-wanted` to a catalog; the deprecation,
declaration-kind, and node-size checks apply only to declaration articles.

Versioned implementation notes live at
`blueprint/.implementation-notes/<article_id>.md`, one per article, and survive
article moves. Claims keep workers from editing the same leaf's note;
containers have no claims. `check` warns about a malformed, empty, unreadable,
symlinked, or orphaned note, so assign the article ID before creating its note.
It also warns about a notes folder under `roadmap/` or capitalized differently,
and about `.implementation-notes` as a file or a link, which nothing reads
through. `audit` reports each as `invalid-implementation-note`. Any worker may
delete a stale note or repair an orphan: move anything still useful into the
surviving article's note and delete the orphan, or restore a lost `article_id`
line if the article still exists. Delete a note once its article is proved; for
a container, once every formalizable descendant and every `not_ready` leaf
beneath it is proved and there is at least one (a module catalog never counts).
`autoform audit` reports a note left after that point. Render omits the hidden
directory from the site and the publication hash.

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
| `mathlib_declaration: Nat.exists_infinite_primes` | Mathlib name(s) the article restates; `audit` requires one on a `mathlib: true` article. Render lists each in a Mathlib row of the article's statement box, as text without a link; once a [pinned name list](reference/pinned-names.md) in `blueprint/libraries/` holds a name, `check` warns about one no list holds. |
| `mathlib_file: Mathlib/Data/Nat/Prime/Infinite.lean` | The Mathlib file that declares the `mathlib_declaration` name(s), as a portable relative path; `search` reports it. |
| `not_ready: true` | Needs more blueprint work before it can be attempted. |
| `lean: Ns.decl` | Declaration name(s) that discharge the article. On a target recording `statement: formalized`, the first takes its progress credit, and `audit` warns `duplicate-lean-target` when another target takes credit for the same declaration (see [Warnings](reference/validation-and-audit.md#warnings)). |
| `discussion: 42` | Issue number or URL where the article is being discussed. |
| `article_id: af_...` | Durable identity, `af_` plus 24 lowercase hex digits; `autoform work` requires it on unfinished formalizable leaves, `review record` on the article it records, and `review prepare`, `review check`, and `render --review` on every Lean-mapped article but a module catalog. |
| `review_approved: sha256:<64 hex>` | The hash of the complete review surface someone approved for this article. It shows that surface is unchanged, not who approved it. |
| `open_statements: allowed` | Project policy, valid only in `roadmap/README.md`: a theorem's statement may land with a `sorry` proof (see [Open statements](reference/open-statements.md#open-statements)). Absent or `forbidden` keeps the strict policy. |
| `published: false` | Folder setting, valid only in the `README.md` of a folder below `roadmap/`: `check` and `audit` validate the folder and everything in it, but render leaves it out of the site and of the publication hash. No link on a published roadmap article may point into it, whether a dependency, a source or prose; a link from any other page keeps only its text, with a warning. `audit` and render reject coverage evidence that points into it. A folder inside an unpublished one is unpublished too, whatever its own README says. |
| `tracks_lean: false` | Project setting, read only in `roadmap/README.md`: the project writes no Lean, so the book's pages show no Lean progress (no landing figures or Next up, no status marks on statements or status dots on their dependency references, no chapter progress lines). Absent or `true` keeps it. Anywhere else under `roadmap/`, or with another value, `check` warns and the setting is ignored. |

Progress is *derived* from the DAG on every run, so it cannot go stale.
Readiness depends on the [policy](reference/open-statements.md#open-statements).

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
[open statement](reference/open-statements.md#open-statements) through its
dependencies; an open dependency counts together with whatever its statement
prerequisites reach, and a proved dependency passes on everything it reaches.
The site colours it violet and lists those open statements in an `Assumes`
row of its statement box.

## Commands

This section is the single source of truth for the command line; skills link
here, and every invocation in either is parse-checked against the real parser.

Commands are written as they appear on `PATH` with this package installed; in a
consumer project, where it is not, run each from the project root with the
prefix `uv run --project "<AUTOFORM_PLUGIN_ROOT>"`, the root resolved from the
loaded plugin, as [Publishing](#publishing-and-the-dashboard) shows.

`autoform --version` prints the installed version, followed by the plugin
checkout's commit when it runs from one, as in `autoform 0.6.0 (745a88599469)`.

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

[Project setup](reference/project-setup.md#init) gives the `.gitignore` rules,
the names `init` refuses, and the Autoform pin without which CI is omitted.

### project

Create or inspect a Lean project, report its worktrees, and list Autoform's
bundled known-good release pairs, the ones `project new` can lock offline:

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
concurrent creation leaves no partial target. Its errors name a code, which
[Project setup](reference/project-setup.md#project-new) lists.

`project inspect` is read-only and offline. Automation should use `--json`,
accept only `autoform-project-inspection/v1`, and branch on `ok`,
`compatibility.status`, and `diagnostics[].code`, not message text. The
[project-inspection reference](project/README.md) defines the statuses, exit
codes, JSON fields, and diagnostic codes.

`autoform project worktrees --json` lists each linked worktree with the codes
that protect it, and `--remove MANIFEST` removes the unprotected ones after
checking each again ([Project setup](reference/project-setup.md#project-worktrees)).
Exit 1: no checkout, bad manifest, or a refusal.

### Publishing and the dashboard

Publishing a project runs four steps in order:

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

`autoform dashboard . --site-dir site` then serves the built site on `127.0.0.1`
until you stop it, with a read-only live overlay of current author claims. The
generated `blueprint-pages.yml` deploys only a commit that `autoform-verify.yml`
passed on a push to `main` (see [the published site](reference/site.md)).

### check

Validate structure and, with `--lean-root`, that every `lean:` name exists:

```bash
autoform check blueprint --lean-root .
```

`check` validates the graph contract ([Validation](reference/validation-and-audit.md#validation)).
With `--lean-root` it resolves names only in the project's own Lean files, not
in Mathlib or other dependencies under `.lake`. A name not found, unless the
error gives its type's `path:line` (see Validation), adds a note at the end of
the output: a shared library's declaration belongs in the article's
implementation note.

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
when the project's Lake files cannot be used. None changes the exit status.

`--no-site-warnings` skips the warnings about the pages the site would publish
([Validation](reference/validation-and-audit.md#validation)), except those about
notes under `blueprint/sources/`, for a run that renders next: `render` prints
the rest, and fails where `check` would warn that it stops. The generated
`blueprint-pages.yml` passes it when its `AUTOFORM_REF`'s `check --help` lists
it; `autoform-verify.yml` does not.

### audit and coverage

```bash
autoform audit blueprint --lean-root .
```

`audit` adds deterministic completeness, provenance, coverage, checked-fact,
and optional Lean-target checks ([Audit contract](reference/validation-and-audit.md#audit-contract)).
It is read-only and offline. Pass `--json` for stable machine-readable output.
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
  as is [placeholder](reference/validation-and-audit.md#statement-text) evidence.
- `INVENTORIED` and `DECOMPOSED` evidence needs a complete inline link to an
  existing roadmap article. Every local link must resolve, fragments included.

#### What coverage completeness does and does not claim

`coverage.complete` in audit and `publication.json` means only that no declared
row is still `MAPPED`. It does **not** claim the rows cover the source
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
project declaration its *statement* rests on, quoted from the sources: a
definition with its body, a theorem with its type only. It reports the axioms
the declaration rests on (a `sorry` shows as `sorryAx`). `--output` records the
`autoform-skeleton/v5` report, `--packets DIR` writes one packet per skeleton,
and `--passages DIR`, which requires `--packets`, each article's cited passage.
An unresolved `lean:` name affects only its article: the others still extract,
the command exits nonzero, and nothing is written into the vault.

Run skeleton extraction only in a trusted checkout or an operating-system
sandbox: Lake evaluates `lakefile.lean`, and the probe imports project code
that may perform arbitrary IO and can forge probe output. `--timeout SECONDS`
sets the helper's and each probe's budget, 600 seconds by default.

[Skeletons and statement review](reference/skeleton-and-review.md) gives what a
skeleton holds, the resolution rules, the report, packets and passages, what
each hash binds, and what a read-back card holds and when it is refused.

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

`review prepare` writes one blind packet per declaration of every Lean-mapped
article, named by its hash, with a manifest for the coordinator. `review record`
runs no Lean and files a blind auditor's read-back as a card. `review check`
fails until every Lean-mapped article has current cards and a `review_approved`
equal to the review hash it prints. `render --review` puts a *Review* disclosure
under every formalized statement. [Statement
review](reference/skeleton-and-review.md#statement-review) gives each command's
rules and refusals. `review prepare`, `review check`, and `render --review` take
the same `--timeout SECONDS` as `skeleton`.

`review prepare` and `review record` exit 2 on input they cannot derive, read,
or trust, on refused testimony, and on a card conflict. `review check` exits 1
on findings, and 2 on evidence it cannot derive or a blueprint that changed
during the run. `render --review` exits 1 on a refused review, printing its
reasons on stdout; `--timeout` is accepted only with `--review`.

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

Inspect the current Markdown-derived formalization frontier, and list the open
statements and the articles that rest on them:

```bash
autoform work list . --lean-root .
autoform work list . --lean-root . --json
autoform work context chapter/result . --lean-root . --json
autoform work assumptions .
autoform work assumptions blueprint --json
```

`work list` returns the formalizable leaves whose next statement or proof phase
is unblocked, by the rule behind the derived `can_state` and `can_prove` states
and the site's Next up card (see [Open
statements](reference/open-statements.md#open-statements)).

`work context` takes the path-derived node ID or an `article_id`; `work list`
fails if an unfinished formalizable leaf lacks one (add them with `autoform
migrate article-ids --write`). Both are read-only and refuse an article whose
bytes changed after the graph loaded; rerun once the project is idle. [Work list
and context](reference/work.md#work-list-and-context) gives the report, the
`autoform-work/v2` JSON, blockers, revision lines and open-policy output.

`work assumptions --json` writes the `autoform-assumptions/v1` contract CI
audits the build against ([work assumptions](reference/work.md#work-assumptions)).

#### work impact

Before editing an article's Lean declarations, ask what revising them affects:

```bash
autoform work impact chapter/result . --lean-root .
autoform work impact chapter/result . --lean-root . --declaration MyProject.helper --json
```

`work impact` runs a bounded Lean probe against a fresh build ([work
impact](reference/work.md#work-impact)). Both impact commands exit 2 on any
refusal, a missing or stale build included, and import project modules, so run
them only in a trusted checkout or sandbox.

#### work import-impact

Report an import edit's coordination and review scope without forbidding it:

```bash
autoform work import-impact SELECTOR MODULE [TARGET] --lean-root PATH [--json] [--timeout SECONDS] [--module-scope SCOPE]
```

`import-impact` is advisory and edits no authored roadmap or Lean source. By
default the probe imports every module of the root libraries, so each must be
built in this checkout: a missing or stale build exits 2 with the `lake build`
command for them. [work import-impact](reference/work.md#work-import-impact)
gives the scopes, ownership rules, JSON and every field.

Imports are allowed regardless of reported breadth; follow the [Formalize import
workflow](../skills/formalize/SKILL.md#import-edits).

### search

Before adding an article or a Lean helper, check the blueprint for its result:

```bash
autoform search . "separating hyperplane" --lean-root .
autoform search . "non-ambiguous" --lean-root . --json --limit 20
autoform search blueprint "interlacing" --state proved --state fully_proved
autoform library list .
autoform library index . --lib MathlibExt
```

`search` takes the project or blueprint directory, then one quoted query; both
are required. A query that starts with `-` goes last, after `--`. It exits 0
with or without hits ([search contract](reference/search.md#search-contract)).

### migrate

Plan durable article identity metadata, and add it:

```bash
autoform migrate article-ids blueprint --json
autoform migrate article-ids blueprint --check
autoform migrate article-ids blueprint --write
```

Without `--write` the planner is read-only: it validates uniqueness and
proposes deterministic IDs, with exact source hashes, for articles that lack
one. `--write` only adds each ID as the first frontmatter line, or as a
frontmatter block of its own. It checks every article before writing the first,
writes one only while its bytes still have the planned hash, and refuses one
that changed or became a symlink; a rerun completes the plan. The runtime and
`autoform work` see assigned IDs at once.

### claim

Coordinate temporary cross-machine ownership without modifying the book:

```bash
export AUTOFORM_WORKER_ID="agent-name"
autoform claim acquire af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
autoform claim renew af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
autoform claim release af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
autoform claim list af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
```

Claims are optional: use them when the user asks, or when another session on the
project already does; a single Formalize run needs none. A claim excludes only
workers that also claim. Claim an article by the `claim_target` that `work
context` reports: claims on its path ID and on its `article_id` do not exclude
each other. Each concurrent agent needs its own worker ID, since a second
acquire by the same owner succeeds; where shell state does not persist, pass
`--worker-id` on every command. Leases expire after 1500 seconds unless `--ttl`
sets another length; renew well within that, and confirm a claim with `renew`,
not `acquire`, which also succeeds on an expired or released lease. Acquire a
whole set in one command (see the [claim
contract](reference/claims-and-revisions.md#claim-contract), which also covers
`acquire --note`).

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

Ask the agent that just finished a formalization attempt what infrastructure
would have made it cheaper, and keep its answer:

```bash
autoform debrief subagent form af_5b0e4d3c2a1f09e8d7c6b5a4 . --phase proof
autoform debrief subagent record af_5b0e4d3c2a1f09e8d7c6b5a4 . --phase proof \
  --worker-id agent-name < /tmp/answer.json
autoform debrief subagent render .
```

Orchestrator debriefs are on unless `AUTOFORM_DEBRIEF` turns them off; subagent
debriefs need `AUTOFORM_SUBAGENT_DEBRIEF=1`. Both share one store that Git does
not track, which `AUTOFORM_DEBRIEF_DIR` moves; [Debriefs](reference/debrief.md)
gives the form, what `record` stores and prints, and where the store lives.

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
article or chapter-card reading beside the whole-project map, with global search.
Complete published statements, proofs and citations remain the reading content.
The index `dependencies/index.json`, project payload `dependencies.json`, and
other `dependencies/` payloads are internal to this viewer; their schemas may change.
`dependencies/map/` is a second, standalone page:
one zoomable map of the whole project, which loads `dependencies/map/data/` one
level at a time and shows each result's statement from its article.
A formal statement links to its canonical article or containing chapter card;
legacy `#node=<id>`, view and filter links remain aliases, not a second viewer.
A `lean:` declaration resolving to a repository permalink gets a source link;
a catalog's Lean row shows its first eight names and counts the rest.
Render percent-encodes each
link it writes from file and folder names, such as the book's dependency
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
keep it outside `blueprint/`. An Obsidian `.canvas` file and a [pinned name
list](reference/pinned-names.md) are left out silently, as `.obsidian` is, and a
Markdown page that is not UTF-8 with a warning. A link to a file the site leaves
out, a hidden path outside `sources/`, or a page in a `published: false` folder
keeps only its text; `check` and `render` warn about each such link to a hidden
path or an unpublished folder.

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

## Contracts

Each area's contract has its own page:

- [Validation and audit](reference/validation-and-audit.md): what `check` and `audit` refuse or warn about.
- [Search](reference/search.md): `autoform search`, and searching a library the project depends on.
- [Pinned name lists](reference/pinned-names.md): a library's names at a pinned revision, and what `check` warns about them.
- [Skeletons and statement review](reference/skeleton-and-review.md): what `skeleton` extracts, its hashes, read-back cards, and the `review` rules.
- [Open statements](reference/open-statements.md): conditional results and the open-statement policy.
- [Claims and revisions](reference/claims-and-revisions.md): leases and revising a published statement.
- [Work](reference/work.md): the frontier, open statements, and the impact of a Lean or import edit.
- [Debriefs](reference/debrief.md): what the debrief commands ask, store and print.
- [Project setup](reference/project-setup.md): what `init` and `project` write, refuse and report.
- [The published site](reference/site.md): subdirectory projects, the dashboard overlay, and Pages deploys and reports.
- [Runtime and publication](reference/runtime-and-publication.md): `doctor`, the in-memory runtime projection and the published site.
- [Upgrading](../CHANGELOG.md#upgrading-from-050): what a repository meets on a new version, including what CI at an older `AUTOFORM_REF` rejects.
