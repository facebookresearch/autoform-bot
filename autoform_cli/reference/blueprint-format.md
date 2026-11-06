# Blueprint format

Every Markdown file below `blueprint/roadmap/` is an article node. A
`README.md` represents its directory and strictly contains the articles below
it; the nearest ancestor `README.md` is the single parent, at any depth. An
ordinary file's stable ID is its path without `.md`; a `README.md`'s is its
directory path, and the root article's is `roadmap`.

The H1 is the article's title. Container prose is the exposition; a standalone
list item linking a formalizable leaf places it there in the published chapter.
Unplaced leaves appear under “Other statements”, unplaced module catalogs under
“Module inventories”. Keep the root article a short preface and table of
contents; large inventories belong in coverage, and a detailed container
introduces its mathematics in prose and places statements under headings.

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
and `audit` reports `unclosed-code-fence`. A fence line inside an HTML comment
opens no fence, in render too. The graph and an article's statement and sections
read an unclosed fence as plain text, even where the page pairs its backticks
with a later run as inline code, and a later bare line of its character at least
as long closes it. Coverage files, the unpublished-folder link check, and render
when it rewrites links, orders the book and places a chapter's statements read
it as open to the end of the file. An HTML comment that never closes, even one
opened in inline code or an indented code block, hides every later heading and
dependency link: the graph loses those edges, one opened in the statement puts
the rest of the article in the theorem box; `check` warns about one outside
code, and the audit about a hidden `## Depends on`.

`## Depends on` lists what the article needs to be *stated*,
`## Proof depends on` what only its *proof* needs, and `## Informally uses` what
the source's proof cites but the Lean proof need not use. Links under them
resolve relative to the article and must point at another roadmap article,
except in a code block or an HTML comment; other links are navigation or
citations. Status never counts informal links; `check` warns about a bad one.

The optional `declaration` field marks a formalizable leaf and names its
intended Lean declaration command, such as `def`, `theorem`, `structure`, or
`instance`; containers and exposition articles omit it. A declaration that
introduces data rather than a proposition has no separate proof obligation,
whatever its modifiers (such as `noncomputable`); an `axiom` still has one.

`origin` records provenance for formalizable work: `cited` for a direct source
target, `bridged` for a result introduced between source targets, and
`background` for prerequisite mathematics.

Frontmatter is optional.

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
  like a topic, sized as the module.

Roles never change identity, containment, readiness, or edges. An overview
draws no edge to a collection it replaces with its contents, so depend on an
article inside the collection instead. On a declaration article, `check` warns
about any role but `topic`, which the article ignores.

`blueprint/atlas.json` may assign the same areas without editing articles:

```json
{"schema":"autoform-atlas/v1","areas":{"Geometry & Topology":["topology","geometry"]}}
```

Every listed value is a roadmap node id. A node takes the first area that lists
it; an `area:` on the node wins. The file never stops a command: `check` and
`render` warn about each ignored entry, or the whole file when it is not
readable `autoform-atlas/v1` JSON. Render leaves the file out of the site; a
page's link to it keeps only its text.

A `catalog: module` leaf summarizes an existing Lean module and is never
dispatched as a proof task. `audit` reports `catalog-container` for a catalog
that is not a leaf; for one that links no declaration ledger from `## Sources`,
`check` warns and `audit` reports `catalog-missing-ledger`; the ledger link
follows the [Sources rules](validation-and-audit.md#validation).

A completed catalog records in `lean:` the full name of every public
declaration the module writes with a declaration keyword, as the Lean source
index records them; a structure field the index records may go there instead
of the ledger. Names Lean generates, such as constructors,
derived or unnamed instances, and `@[ext]`, `@[simps]`, or `@[to_additive]`
results, and declarations the index cannot see yet, behind `nonrec` or `meta`,
go in the ledger as supporting entries, never in `lean:`; a `private` name
never goes in `lean:`.
A completed catalog may assert `statement: formalized` and `proof: formalized`
once the complete module has been checked.

`check --lean-root` and `audit --lean-root` resolve every name the catalog
lists and, for a name they do not find, say that a generated name belongs in
the ledger. They cannot prove the list omits no newly added public
declaration; completeness remains an authored assertion. `audit --lean-root`
applies the [elaboration-limit](validation-and-audit.md#elaboration-limits)
warnings and `lean-target-wanted` to a catalog; the deprecation,
declaration-kind, and node-size checks apply only to declaration articles.

Versioned implementation notes live at
`blueprint/.implementation-notes/<article_id>.md`, one per article, and survive
article moves. A leaf's claim covers its note only while the leaf is worked on;
containers have no claims. `check` warns about a malformed, empty, unreadable,
symlinked, or orphaned note, so assign the article ID before creating its note.
It also warns about a notes folder under `roadmap/` or capitalized differently,
and about `.implementation-notes` as a file or a link, which nothing reads
through. Any worker may delete a stale note or repair an orphan: move anything
still useful into the surviving article's note and delete the orphan, or restore
a lost `article_id` line if the article still exists. Delete a note once its
article is proved; for a container, once every formalizable descendant and every
`not_ready` leaf beneath it is proved and there is at least one (a module
catalog never counts). `autoform audit` reports a note left after that point.
Render omits the hidden directory from the site and the publication hash.

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
| `mathlib_declaration: Nat.exists_infinite_primes` | Mathlib name(s) the article restates; `audit` requires one on a `mathlib: true` article. Render lists each in a Mathlib row of the article's statement box, as text without a link; once a [pinned name list](pinned-names.md) in `blueprint/libraries/` holds a name, `check` warns about one no list holds. |
| `mathlib_file: Mathlib/Data/Nat/Prime/Infinite.lean` | The Mathlib file that declares the `mathlib_declaration` name(s), as a portable relative path; `search` reports it. |
| `not_ready: true` | Needs more blueprint work before it can be attempted. |
| `lean: Ns.decl` | Declaration name(s) that discharge the article. On a target recording `statement: formalized`, the first takes its progress credit, and `audit` warns `duplicate-lean-target` when another target takes credit for the same declaration (see [Warnings](validation-and-audit.md#warnings)). |
| `discussion: 42` | Issue number or URL where the article is being discussed. |
| `article_id: af_...` | Durable identity, `af_` plus 24 lowercase hex digits; `autoform work` requires it on unfinished formalizable leaves, `review record` on the article it records, and `review prepare`, `review check`, and `render --review` on every Lean-mapped article but a module catalog. |
| `review_approved: sha256:<64 hex>` | The hash of the complete review surface someone approved for this article. It shows that surface is unchanged, not who approved it. |
| `open_statements: allowed` | Project policy, valid only in `roadmap/README.md`: a theorem's statement may land with a `sorry` proof (see [Open statements](open-statements.md#open-statements)). Absent or `forbidden` keeps the strict policy. |
| `published: false` | Folder setting, valid only in the `README.md` of a folder below `roadmap/`: `check` and `audit` validate the folder and everything in it, but render leaves it out of the site and of the publication hash. No link on a published roadmap article may point into it, whether a dependency, a source or prose; a link from any other page keeps only its text, with a warning. `audit` and render reject coverage evidence that points into it. A folder inside an unpublished one is unpublished too, whatever its own README says. |
| `tracks_lean: false` | Project setting, read only in `roadmap/README.md`: the project writes no Lean, so the book's pages show no Lean progress (no landing figures or Next up, no status marks on statements or status dots on references to them, no chapter progress lines, no status filter or status words in the explorer, and no tints, progress bars or status words on the whole-project map, whose cards name their kind instead; neither search box mentions Lean names, though both still match them). Absent or `true` keeps it. Anywhere else under `roadmap/`, or with another value, `check` warns and the setting is ignored. |
| `coverage_list_limit: 50` | Project setting, read only in `roadmap/README.md`: how many statements the coverage page lists under a row whose evidence it keeps, before it counts the rest; a list that replaces the evidence is shown in full. Absent means 50. Anywhere else under `roadmap/`, or with a value that is not a whole number from 1, `check` warns and the setting is ignored. |

Progress is *derived* from the DAG on every run, so it cannot go stale.
Readiness depends on the [policy](open-statements.md#open-statements).

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
[open statement](open-statements.md#open-statements) through its
dependencies; an open dependency counts together with whatever its statement
prerequisites reach, and a proved dependency passes on everything it reaches.
The site colours it violet and lists those open statements in an `Assumes`
row of its statement box.

## Coverage

The machine-checkable `coverage/README.md` contract contains one
`Area | Coverage | Evidence` table, one disposition per row:

- `MAPPED` is nonterminal.
- `INVENTORIED` is terminal for exact source accounting and links a
  `catalog: module` record, directly or through a containing roadmap scope:
  `coverage-role-mismatch` names a link that reaches none, and
  `unclassified-inventory` the first catalogs no such row reaches.
- `DECOMPOSED` links source-grounded declaration articles, directly or through a
  containing scope. Links that reach only catalogs are a
  `coverage-role-mismatch`; any other link reaching no declaration leaf is a
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

The contract is read as published Markdown and fails closed:

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
  as is [placeholder](validation-and-audit.md#statement-text) evidence.
- `INVENTORIED` and `DECOMPOSED` evidence needs a complete inline link to an
  existing roadmap article. Every local link must resolve, fragments included.

`coverage.complete` in audit and `publication.json` means only that no declared
row is still `MAPPED`. It does **not** claim the rows cover the source
exhaustively, nor that linked articles are formalized or proved.

Target metrics (the denominator, readiness count, and progress breakdowns in the
book and `autoform check`) count non-container articles carrying `declaration`,
never a `catalog: module` record. A scope's status bar, item count, and
dependency-map legend also count `not_ready` leaves that declare nothing yet, so
deferred work holds its scope open, and each catalog under a neutral status,
`inventory checked` once fully proved and `inventory pending` until then, which
never changes the scope's rolled-up status; a target that depends on a catalog
still waits for it.
