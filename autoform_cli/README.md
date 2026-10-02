# Blueprint format and CLI

The Autoform CLI validates, visualizes, and publishes the multilevel dependency
graph embedded in `blueprint/roadmap/`. The Markdown book is the graph: no
separate authored or generated graph file exists.

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
“Additional formalization targets” section rather than disappearing.

Keep the root roadmap article short: it is the book's preface and table of
contents, not a dump of every planned milestone. Large planning inventories
belong in coverage or progress views; detailed containers should introduce their
mathematics in prose and place statements under meaningful section headings.

Frontmatter records checked facts:

```markdown
---
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

`## Depends on` lists what the article needs in order to be *stated*;
`## Proof depends on` lists what only its *proof* needs. Both are graph edges.
Links anywhere else are ordinary navigation or citations. Dependencies resolve
relative to the current article and must point at another roadmap article.

The optional `declaration` field marks a formalizable leaf and describes its
intended Lean artifact, for example `def`, `theorem`, `lemma`, `structure`, or
`instance`. Container and exposition articles omit it. Autoform records this
hint but does not constrain the set of Lean declaration commands. Declarations
that introduce data rather than a proposition carry no separate proof
obligation.

`origin` records provenance for formalizable work: `cited` for a direct source
target, `bridged` for a result introduced between source targets, and
`background` for prerequisite mathematics.

Frontmatter is optional. A container article that only supplies prose and
placement needs none at all; only checked facts are recorded.

## Assertions and derived status

An article asserts only facts a human or agent verified:

| Key | Meaning |
| --- | --- |
| `statement: formalized` | The Lean statement exists and compiles. |
| `proof: formalized` | The Lean proof is complete. |
| `mathlib: true` | The result is upstreamed into Mathlib. |
| `not_ready: true` | Needs more blueprint work before it can be attempted. |
| `lean: Ns.decl` | Declaration name(s) that discharge the article. |
| `discussion: 42` | Issue number or URL where the article is being discussed. |
| `review_approved: sha256:<64 hex>` | The hash of the complete review surface someone approved for this article. It shows that surface is unchanged, not who approved it. |

Everything a reader thinks of as progress is *derived* from the DAG on every
run, so it cannot go stale:

| Derived state | Holds when |
| --- | --- |
| `can_state` | Every statement prerequisite is stated. |
| `can_prove` | Stated, and every proof prerequisite is proved. |
| `proved` | The proof compiles. |
| `fully_proved` | Proved, and every prerequisite is fully proved, recursively. |
| `defined` | A definition is written but rests on unfinished work. |

`proved` and `fully_proved` differ on purpose: a theorem whose own proof
compiles but which rests on an unproved lemma is green, not dark green. The
palette and state names follow
[leanblueprint](https://pypi.org/project/leanblueprint/), so the published
graph reads the same way as the Lean community's LaTeX blueprints.

## Commands

This section is the single source of truth for the command line. Skills
describe what to achieve and link here; they do not restate flags, so a change
to the CLI lands in one place.

The commands below are written as they appear on `PATH`. Inside a consumer
project the plugin is not installed, so resolve `<AUTOFORM_PLUGIN_ROOT>` from
the loaded plugin and prefix each one, running from the project root:

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

Publishing a project runs four steps in order: validate, write the Mermaid
graph into the vault, render the site source, then strict-build the site.

```bash
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform check blueprint --lean-root .
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform-visualize blueprint
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform render blueprint \
  --output site-src --lean-root . --require-declarations
uv run --with mkdocs --with mkdocs-material --with mkdocs-literate-nav \
  --with pymdown-extensions --with "<AUTOFORM_PLUGIN_ROOT>" mkdocs build --strict
```

The build needs autoform too: `mkdocs.yml` reads formulas with its Markdown
extension, `autoform_cli.markdown:FormulaExtension`.

Drop `--require-declarations` when reviewing work in progress, where a
statement may name a Lean declaration that does not exist yet.

Validate structure, and optionally check that every `lean:` name really exists
in the project's Lean sources:

```bash
autoform check blueprint --lean-root .
autoform audit blueprint --lean-root .
```

`check` validates the graph contract. `audit` adds deterministic completeness,
provenance, coverage, checked-fact, and optional Lean-target checks. It is local
and read-only: it neither contacts network services nor writes findings back
into the blueprint. Pass `--json` for stable machine-readable output; a nonzero
exit status means the audit found at least one issue. The machine-checkable
`coverage/README.md` contract contains one `Area | Coverage | Evidence` table
with `MAPPED`, `DECOMPOSED`, `DEFERRED`, or `OUT` dispositions. `MAPPED` is
nonterminal; the other three explicitly disposition an area. Audit JSON includes
canonical rows, counts, and the exact coverage source hash, while
`publication.json` records aggregate counts without duplicating the authored
rows.

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
"Pending Mathlib PR 1234" names something a reader can check. `DECOMPOSED`
evidence must contain at least one complete inline link to an existing roadmap
article, and *every* link it offers must resolve, fragments included, under the
same rules the audit applies. A link missing its closing parenthesis does not
render and does not count.

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
project.

It does **not** claim that the declared rows cover the source exhaustively, and
it says nothing about whether the linked roadmap articles are formalized or
proved. A project that declares one narrow area and disposes of it reports
`complete` while most of its source remains undeclared. Exhaustiveness is an
authoring judgement that no local check can make.

Publication and audit are deliberately different gates. The generated
`blueprint-pages.yml` runs `check` and `render`; it does not run `audit`. An
invalid coverage contract fails `render` before any output is written, but a
valid contract with `MAPPED` rows publishes normally even though `audit` reports
each one as a `declared-coverage-gap`. That is intended: a roadmap is published
while it is still being decomposed, and the published `coverage.complete: false`
is how a reader sees that. Run `autoform audit` in CI when you want mapped rows
to block a merge.

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
command that runs Lean: for each module that declares a selected `lean:`
target it writes a small probe that imports that module and the probe's
helper module and makes one fully qualified call, with no `open` or
`set_option`, and runs it with `lake env lean` against the built project. The
helpers are compiled once per extraction, with the project's toolchain and no
project module in scope, into a temporary module named
`autoform-skeleton-helper`; a project, dependency, or `LEAN_PATH` module of
that name stops the extraction. Every constant the helper declares is in the
`AutoformSkeleton` namespace, which is reserved as well: when a module
declares a constant there that the helper also declares, its probe cannot
import the helper, and its targets are unresolved with a reason that names the
constant. A declaration's packet reads as it does to a
file that imports its module, whichever other articles are extracted with it.
That environment has the module's global notation, instances, and
attributes, including any the module declares after the declaration; it lacks
the module's `local notation` and local instances, and the `open` and
`set_option` commands in effect at the declaration. The probe adds exactly two
printing options, `pp.funBinderTypes` and `pp.coercions.types`, so a reader
sees what each binder ranges over and where each cast lands; signatures
otherwise print as `#check` prints them in a file whose only import is the
module, with an identifier that spells one of that file's tokens escaped as
`«»`. The helper module's own imports (`Lean.Elab.Command`,
`Lean.Util.CollectAxioms`, `Lean.Util.Path`, and `Lean.Data.Json`) are in
that environment too, with whatever global notation and instances they
declare, but their tokens do not change how a signature is escaped. Before any probe, Lake must confirm once, without rebuilding, that
every probed module matches its exact source inputs; a missing
`lake-manifest.json`, stale artifacts, or a source tree that changes during
extraction makes the command fail. Only the probed modules need to be built
and fresh. That check rehashes every input and rewrites Lake's `.hash` files,
so the project's `.lake` directory must be writable. A lexical closure would
miss what
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
Each name is probed in the module whose source the lexical index finds it in,
reading each file only up to `#exit`; a name Lean declares in another module
is unresolved too. A name that several indexed files declare, even when all
but one of them declare it `private`, is unresolved without a probe, because
the index cannot tell which one Lean binds, and every reason given for it
names each of those files in sorted order.
A probe that fails on its own (a nonzero exit, a timeout, or malformed or
ambiguous records) likewise leaves only its module's targets unresolved, with
the reason, and an error the probe meets while reading one declaration leaves
only that declaration unresolved. A reason quotes at most 2,000 characters of
Lean's output, shows the probe's temporary directory as `<scratch>` and
project paths relative to the project, and so reads the same on every run.
The output and material limits apply to a whole probe, so a module whose
selected declarations together pass one fails as a whole: a declaration that
resolves when selected alone can be unresolved beside its module's other
targets. A declaration that resolves has the same hash under any selection.
A missing `lake`, a failed freshness check or helper build, a dependency that
hides a toolchain library, or a blueprint or source tree that changes during
extraction still stops the whole command.
`--output` records the `autoform-skeleton/v5` report, which contains no
timestamp or absolute path, for a later render or review to consume. The
report identifies the exact blueprint, by a hash of every article's bytes and
of each cited source file a passage is cut from, its complete target set, and whether
the extraction covered all targets or an explicit `--node` selection; a
report whose selection, articles, and unresolved declarations disagree can be
neither built nor loaded. Review evidence is prepared only from a report that
selects every article and whose targets are the blueprint's `lean:` names, and
a record validates each article against a report scoped to that article. It
quotes each trusted definition's source and records theorem
dependencies by elaborated signature, so it stands on its own without ever
copying a theorem proof. Declarations in one project share most of what they
trust, so the report states each trusted declaration, each external
constant's semantic material, and each boundary module's identity once, in the
top-level `trusted`, `semantics`, and `boundary_modules` tables, and each
declaration names the entries it uses; the probe's own output is shared the
same way. A name means what the root's module environment declares under it:
a trusted declaration is printed there and can read differently under two root
modules, and two root modules with different imports can see two different
external constants, or two axioms, under one name. So `trusted` and
`semantics` are keyed by root module and then by name, and a declaration may
name only the entries under its own module. A module identity is the digest
of that module's compiled files, which one workspace builds once for every
root, so `boundary_modules` is keyed by module name alone. Two probes that
disagree about one boundary module's files stop the command with an error.
The probe also states each elaborated subterm of 256 bytes or more
once, since proof terms repeat large subterms heavily; the report keeps each
material's full text.

Run skeleton extraction only in a trusted checkout or an operating-system
sandbox. Lake evaluates `lakefile.lean`, and the generated probe imports project
code whose initializers, macros, and metaprograms may perform arbitrary IO and
can forge probe output. The timeout and output cap bound the direct batch
command; on POSIX, Autoform also terminates its process group, for every
probe, on every failure and on interruption. The Lake freshness check, the
helper build, and each probe have their own 600-second budget;
`--timeout SECONDS` sets the helper build's and each probe's, which a large
project may need. The budgets are per probe, not one deadline for the
extraction: probes run in rounds of parallel probes, so a whole extraction
can run for the freshness check, the helper build, and one probe budget per
round, that is, the number of probed modules divided by the parallel probe
count, rounded up. A shared deadline would make which module times out
depend on how the pool scheduled its neighbors, so a module's result would
no longer match extracting it alone. Each probe pays a Lean start and loads its
module's imports; on a Mathlib project that measured about 10 CPU-seconds and
140 MB of private memory per probe, with the `.olean` files mapped and shared.
Probes run in parallel, one per CPU available to the process, at most eight.
The worker that ran a probe parses its records as soon as it ends and drops
the raw output, so at most eight probes' raw output, 64 MiB each at most, is
held at once. The parsed records of every probed module stay in memory until
the report is built; one probe's semantic material expands to at most 512 Mi
characters, so the parsed material totals at most that much per probed
module. These limits are resource controls, not a security or authenticity
boundary.

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

Reports and packet manifests also carry an evidence hash over the exact
proof-free text shown to a reviewer. Semantic hashes survive presentation-only
edits; evidence hashes identify the bytes that were actually read. An
article's review hash binds its joint packet to the cited passage, that
passage's locator, and the article's drift hash. Human
approval does not record any of these hashes alone. It records one `review_approved`
hash over the complete review surface: the article's title and statement, cited source
passage and locator, exact joint packet, each declaration's drift hash, and
every validated read-back card.
Changing any reviewed input invalidates the approval. The hash shows that the
surface is unchanged since it was written down, not who wrote it; the review
commands below label such an approval self-approved until a verifier finds
who approved it. These hashes do not authenticate the evidence when candidate
code controls the checkout.

A read-back is an independent agent's mathematical-English account of one
declaration packet. The coordinator gives that agent only an opaque packet and
the read-back instructions, then records the returned testimony through the
CLI. Cards live at
`blueprint/readbacks/<article_id>/<encoded-declaration>.md`; the durable
`article_id` keeps testimony attached when an article moves, while the encoded
filename avoids platform-specific Lean-name collisions. Each versioned card
contains the exact packet, both hashes, a model label, and nonempty testimony.
The loader rejects missing or unknown fields, altered packets, identity
mismatches, and malformed or empty testimony. Testimony must show a reader
everything it says: invisible and reordering characters (zero-width spaces,
bidirectional overrides, Unicode's default-ignorable characters, spaces other
than the ASCII space, right-to-left letters and digits), code points the
Unicode data of the Python running the check leaves unassigned, named with
that data's version, more than four combining marks on one character or
more than two above or below it, a
combining mark with no character before it, TeX comments, and raw HTML are
rejected. Testimony
is rendered once, by a Markdown renderer that reads no HTML, and the site shows
that rendering byte for byte, so what was validated is what a reviewer sees.
Code is shown as typed. Outside code, formulas included, HTML tags, comments,
declarations, autolinks, and character references are refused by name, since a
Markdown viewer of the vault would read them, so in a formula put a space after
`<`. Links, link definitions, images, headings, footnotes, GitHub alerts,
Mermaid blocks, code fences naming anything but `lean`, `lean4`, or `text`,
and table rows with more or fewer cells than the header are refused;
attribute lists are shown as typed. GitHub shows the vault's cards, so
testimony is read a second time as GitHub reads it, by cmark-gfm with
formulas found where GitHub's Markdown API was seen to find them. It is
refused where that reading shows anything otherwise than the site does,
naming the line where the two part and a rewrite both read alike, and where
it holds a formula GitHub was not seen to read that way. How to write
formulas and Markdown that both read alike is set out in one place, the
read-back guide `skills/human-review/references/readback.md`: in short,
`$...$` in a line of text, `` $`...`$ `` where GitHub would read that
otherwise, a ```` ```math ```` fence for displayed math, and never `\(` or
`\[`. MathJax reads no text of a card outside the formulas the renderer
marked, so other TeX in text shows as typed, and `\$` shows a dollar sign.
TeX may use only an allowlist of the notation statements need, kept in
`readback.py` against the site's pinned MathJax 3.2.2: letters, symbols,
relations, operators, arrows, delimiters, fractions, roots, accents, fonts,
styles, `\text`, spaces from `\!` to `\qquad` and `~`, and the `cases`,
`array`, matrix, `aligned`, `gathered`, `split`, `align`, `gather`, and
`equation` environments, with at most one `align`, `gather`, or `equation` in
a formula. Any other command or environment, such as
`\phantom`, `\rlap`, `\kern`, `\color`, `\tag`, or a macro definition, is
refused by name. Each formula is read the way MathJax lays it out. TeX it
would not set is refused: unbalanced braces, `\left` without `\right`, a
missing argument, `x^a^b`, `\not` before anything but a relation, a function
name such as `\sin` alone as a script (write `x^{\sin}`), `\pmb` inside
`\pmb`, a character MathJax's fonts lack, a combining mark, array columns
other than `l`, `c`, and `r` with one `|` or `:` between two of them, and a
bracket after `aligned`, `gathered`, or `array` other than `[t]`, `[b]`, or
`[c]`. So are
arguments that show nothing (`\mathrm{}`, `\hat{\displaystyle}`), space
between two symbols that adds up to less than one negative thin space,
negative space at the start or end of a formula, which slides it over what
sits beside it, row spacing after `\\`, and a `*` right after `\\`, which
MathJax reads as part of the row break and does not show. A formula may
hold at most 2,048 characters, 8 em of space, 9 `&` in a row, 16 `\\` in an
environment and 32 in all, 16 empty
cells, and no empty row, nested at most 16 deep with scripts 8 deep; a
testimony may hold at most 64 em of space, the space in the rows of an
environment counted once per column.
Testimony must also show at least one letter or digit. Before any card is parsed its
testimony must fit limits well above what real read-backs use: 32 KiB, 500
lines, 1,024 math delimiters, 512 backticks in runs of at most 16, 64 opening
brackets, 64 columns of nesting, 256 underscores that start a word, 1,024
asterisks, 2,048 backslashes, and 2,048 table cells. The Markdown parser is
superlinear in each of these, so a byte limit alone would not bound it, and
every card in a pull request is read before its validity is known. The
rendering of a testimony must also fit in 256 KiB of HTML before that HTML is
parsed. Cards are read through no-follow descriptors, so a card or directory
swapped for a link is skipped. A card file
holds at most 4 MiB, and no file is read more than one byte past that, the byte
that shows it is larger. A file at a card path that is larger, is not UTF-8, or
cannot be read, and a directory or FIFO there, is reported as an invalid card,
not skipped, even when the declaration's name is too long for the filename to
spell. A card is attributed to the declaration its path names, so one that
records another declaration, or whose `.md` is in another case, is reported as
that declaration's invalid card. A directory under `readbacks/` that cannot be
listed stops loading with an error naming it, rather than reading as though it
held no card. Writes name
a card by the hash of its bytes, so a card that is not UTF-8 is replaced like
any other; one over the limit must be removed by hand.

A write walks to the card's directory without following links and takes that
directory's lock, so Autoform's writes to one directory happen one at a time.
If the directory was moved or replaced before the lock was taken, a fresh walk
no longer reaches it, so the write lets it go and walks again, three times at
most before it refuses; nothing is staged until the directory it holds is the
one the card's path reaches. Holding the lock, the write reads the current card
through that directory. An optional expected-card hash makes updates
compare-and-swap: different content replaces a card only when it names that
card's hash, and a card that names a hash conflicts with a missing card. The
conflict check a batch runs first applies the same rule. Filing content
identical to the current card writes nothing, so it succeeds even in a
read-only directory. Otherwise the write stages the card in a new temporary
file, flushes it to disk, renames it over the card's name in one step, and
flushes the directory. On macOS, where a plain `fsync` can leave data in the
drive's cache, each flush is `F_FULLFSYNC`, falling back to `fsync` on a file
system that does not support it; any other error from it fails the flush. Each
directory a first card's write makes on the way is flushed into its parent as
it is made. At every moment the card's name holds
either the old complete card (nothing, for a first card) or the new one. A
failure or interrupt before the rename removes the temporary file and leaves
the card as it was. If removing it also fails, the file is left, and the error
names it, or after an interrupt a warning does. A second interrupt that lands
while it is removed can leave it too, as can a crash, SIGTERM, or SIGKILL. It
is a `.autoform-readback-*.tmp` file beside the card, which the loader never
reads as a card and the blueprint `.gitignore`
that `autoform init` writes ignores. If only a flush of a directory fails, the
card is still published and the write warns. The contract covers Autoform's
writers only: while a write runs, any other change in `readbacks/<article>/` is
out of contract. An editor's save that lands during a write can be replaced without a
conflict, so edit cards while no write is running.

The lock belongs to the open file description, which a process forked during
a write shares, so a write unlocks before it closes; a write that cannot get
the lock within ten seconds gives up. If a process dies holding the lock, the
lock is released once no process shares that description: at once, unless such
a child is still running. An interrupt that lands just as a write opens a file
or directory can leak that descriptor, never a locked one, until the process
exits; a temporary file created that way is still removed. Publishing needs
descriptor-relative `open`, `mkdir`, `rename`, and `unlink`, `O_DIRECTORY`,
`O_NOFOLLOW`, `fchmod`, and `flock`, which Linux and macOS provide. Elsewhere,
including Windows, a write and the batch conflict check are refused before
anything is created or read; cards can still be loaded. `model:` remains
a label supplied by the coordinator, not authenticated provenance.

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
intent into it. Lean's parser, not a separate lexer, locates those comments,
and a source is shown only when the probe can prove it reads the source as
Lean read it when compiling the file, `local` syntax aside. Where a comment
starts depends on the token table in force at the declaration: with `++"` a
token, `x ++" -- y "` holds a string, and without it a comment. Lean does not
record that table, so the probe reconstructs it from the declaration's own
module, once per module. Every global token of a module Lean loads to
compile the file is in it, and a token of any other module is not; under
the module system Lean loads the file's imports and, through each loaded
module, only what that module imports `public`ly, unless a chain of
`import all` reaches it. A scoped token of a loaded module, and a token the
module itself declares, may or may not be; the probe counts such a token as
absent only when it can place the parser that declares the token after the
declaration's source ends. The probe parses the source
under every table those uncertain tokens allow, counting only the tokens that
occur in the text, since no other token changes how it lexes, and removes
comments only when every table under which the source parses agrees on the
text and on where each comment lies. Otherwise, or when the source contains
more than eight uncertain tokens, a source that may hold a comment is
withheld. The grammar it parses with is the root module's, with the
declaration's namespaces and the `open`s written above it activated; a source
that does not parse there, such as a body using `local notation`, is withheld
if it may hold a comment. A withheld source leaves the declaration's
signatures and kernel material in the packet and does not make the article
unresolved. What the probe cannot see is `local` syntax, which Lean records
nowhere: a `local` token, or a `local` or later-declared parser that reads raw
characters after an existing token, can make the packet show code as a
comment or a comment as code.

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
that names no text, because it points outside the blueprint, names a missing
file or non-UTF-8 text, or names no lines of it, leaves the article's
declarations unresolved. `--passages DIR` writes those passages beside
the packets, one per article, in a separate, disjoint managed directory. It
requires `--packets`. Each article directory also holds `article.lean`, the
joint packet of every declaration the article
names, because a source theorem is often formalized by several declarations
together and each alone is honestly incomplete. A faithfulness judge is given
the article packet and its passage; a read-back auditor is given one
declaration's packet alone, since a read-back is testimony about one
declaration.

Prepare and check the complete review protocol through first-class commands:

```bash
autoform review prepare blueprint --lean-root . \
  --output review.json --packets review-packets
autoform review record blueprint --lean-root . --bundle review.json \
  --article-id af_0123456789abcdef01234567 --declaration Ns.result \
  --packet review-packets/blind/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.lean \
  --testimony read-back.md --model muse-spark-1.1
autoform review record blueprint --lean-root . --bundle review.json \
  --manifest records.json --model muse-spark-1.1
autoform review check blueprint --lean-root . --bundle review.json
autoform audit blueprint --lean-root . --review-bundle review.json
autoform render blueprint --lean-root . --review-bundle review.json
autoform review check blueprint --lean-root .
autoform render blueprint --lean-root . --review
autoform review check blueprint --lean-root . --authenticate github
autoform render blueprint --lean-root . --review --authenticate github
autoform review authenticate blueprint --github
autoform review authenticate blueprint --github --pr 42 --since origin/main --trusted-ref origin/main
```

`review prepare` extracts Lean evidence from the current built tree and writes
a strict, versioned bundle plus opaque packets. The bundle contains the exact
article titles and statements, cited passages, declaration mapping, and packet bytes, but
not the later testimony. Every Lean-mapped review article must be a
declaration-sized leaf so its evidence appears in the rendered site. An article
marked `origin: cited` must link
to an in-vault, non-Markdown source snapshot with an exact
`#L<start>-L<end>` range; preparation refuses a citation it cannot put before
the reviewer. `review record` rechecks the selected current article
and the exact packet bytes before filing a card. Validation is per article, so
an unrelated article that changed since `review prepare` does not block the
record. Any blueprint change during the extraction itself does, even in an
article the record does not touch, and another article's empty or duplicated
`lean:` list stops the extraction. Either way nothing is filed; running the
record again once the blueprint is idle completes it. A record probes only the
modules of the articles it files, so only those modules need to be built and
fresh, and its packets match the ones `review prepare` wrote from a full
extraction.

`--manifest` files a batch against one extraction, where one record per card
would pay a Lake freshness check and a Lean start each. The manifest reuses
the packet manifest's field names, so a coordinator derives it from the one
`review prepare` writes by adding the testimony for each packet:

```json
{
  "schema": "autoform-review-records/v1",
  "records": [
    {"article_id": "af_0123456789abcdef01234567", "declaration": "Ns.result",
     "packet": "review-packets/blind/0123….lean", "testimony": "read-back.md"}
  ]
}
```

Relative paths resolve against the manifest's directory, a record may carry its
own `expected_card_hash`, and a declaration may appear once. Every packet and
testimony is read and checked against the bundle before Lean starts. So is
every card the batch would write over: a re-review lands on the path of the
card it supersedes, which it may replace only by naming that card's hash, and
the batch lists every card that needs one, with the hash, before extracting. The
extraction selects the batch's articles, and each article is
validated against its own part of it, as a single record would be. The
blueprint is then reloaded, and nothing is filed if a selected article changed
while Lean ran or the reloaded blueprint is not the one the extraction saw.
Every card is built and checked before the first is written,
so one bad record stops the batch. Publishing then goes card by card, each
under its own compare-and-swap; a concurrent writer or a failed write (a full
disk, or an I/O or permission error) can stop it midway. The command says how
many cards it filed, and because filing identical content is a no-op, running
the same batch again once the cause is cleared completes it.

`review check`, `audit`, and `render` re-extract the
current Lean evidence and reject unresolved, partial, foreign, or stale
bundles. `review check` also requires complete current cards and matching human
approvals, and judges them as one state of the blueprint: it loads the
articles, the sources they cite, and the cards before Lean runs, the
extraction must have seen those same articles and sources, and the cards must
be unchanged when it ends. The load reads each article and cited source once,
and every statement and passage compared afterward is cut from those bytes,
never read from the files again, so the verdict judges only that state, its
extraction, and those cards. An edit that breaks any of these comparisons fails the
check with `review-snapshot-changed` rather than mixing old and new evidence;
run it again once the blueprint is idle. These are comparisons, not a lock: an
article or card that changes and is restored between two of them goes unseen,
and the restored state is what gets judged; an edit after the last comparison
does not affect a verdict already reached. `audit --review-bundle`, `render --review`, and
`render --review-bundle` make the same check, then judge or show the cards it
validated rather than reading them again. They load the articles again to audit
or render them, and an edit to any article or cited source since the check
fails them with `review-snapshot-changed` too: every skeleton report records a
hash of the blueprint it was extracted from, covering each article's bytes and
the bytes of each source file a passage is cut from, and review evidence is
never built from a report and a blueprint that hash differently. What they then
audit or render is the bytes that second load read, as for a plain `audit` or
`render`, whatever the files hold afterward. The rendered review disclosure uses the same packet bytes that were
hashed, never a reconstructed or comment-bearing approximation. Read-back
cards are absorbed into their article and are not published as standalone
pages. Without `--bundle`, `review check` derives the bundle from its own
extraction, and `render --review` does the same: each extracts the tree once
instead of once to prepare and again to check. Either may instead read a
report `autoform skeleton --output` wrote, with `--skeleton-report FILE` in
place of `--lean-root` for `review check` and beside it for `render`; the
report must cover every article and carry the hash of the blueprint being
checked, so a report from another checkout or another state of the articles
is refused. Each review command that
extracts Lean, plus review-enabled `audit` and `render`, accepts the same
`--timeout SECONDS` probe override as `skeleton`.

Newly scaffolded projects commit the versioned `.autoform-review` policy marker,
so generated CI enforces this gate from the first formalized statement. Older
projects opt in by adding that marker; cards or `review_approved` assertions
without it fail instead of silently disabling review. CI never trusts a
committed bundle: after the Lean build it runs the review-only check against
one derived in the same run. Pages extracts in the job that builds Lean and
checks and renders that report in a separate job (see below), without turning
advisory roadmap or coverage findings into merge blockers.

`review_approved` records unchanged evidence, not who approved it:
`review check` prints the expected hash, so anyone, an agent included, can
paste it. An approval therefore has two separate properties. It is current when
`review_approved` equals the current review hash, which `review check`
enforces. It is authenticated when a verifier finds evidence that an allowed
person approved that exact hash. A current approval without that evidence is
labelled self-approved wherever it is shown. Without `--authenticate` nothing
uses the network and every approval is self-approved; authentication is
evidence a verifier checks, never a flag or setting.

The GitHub verifier needs full Git history, `GITHUB_TOKEN`,
`GITHUB_REPOSITORY`, and optionally `GITHUB_API_URL`, `GITHUB_SERVER_URL` (the
https host review links may point to), and `AUTOFORM_VERIFY_WORKFLOW` (default
`.github/workflows/autoform-verify.yml`). The token needs `contents`,
`pull-requests`, `actions`, and `issues` read access; the pull request gate
below needs only `contents` and `pull-requests`. Rulesets and collaborator
permissions are read with the metadata access every token has.

An approval is only as good as the files around it: whoever can change
CODEOWNERS, a workflow, the Lean sources, or the site's theme without review
can make any approval say anything. So, once per run, nothing is authenticated
unless code owner review guards all of them at R, the trusted ref
(`--trusted-ref`, default `HEAD`; Pages uses the head of the default branch):

- **Current head.** Without `--pr`, R is the head of the default branch on
  GitHub now, as `GET /repos/{owner}/{repo}/git/ref/heads/{branch}` reports
  it. The rulesets and permissions below are read as they are now, so a build
  of an older commit, such as a re-run of an old Pages run, would pair them
  with that commit's CODEOWNERS and bring back approvals a newer CODEOWNERS
  withdrew. Only the current head authenticates. `render --authenticate
  github`, which builds the site, stops instead of labelling anything when R
  is a commit the branch has moved past, or when the default branch or its
  head cannot be read, since labelling every approval self-approved would let
  either downgrade the site. `review check` and `review authenticate`, which
  publish nothing, label every approval self-approved instead, naming R and
  the head, or the failed lookup. Whatever the review settings, the generated
  Pages workflow's `deploy` job makes the same check before it deploys, so a
  build the branch has moved past never replaces the site. That workflow
  builds every push to the default branch, whatever its name: a trigger cannot
  name the default branch, so a push to any branch starts a run, and for any
  other branch every job is skipped before a runner starts. Only a build of
  the default branch outside a pull request authenticates approvals and
  deploys. Neither a pull request's runs nor a newer run of the branch, such
  as a re-run of an old one, can cancel a pending one (`queue: max`), so the
  build of the newer head publishes the site. A head that starts no run of its
  own, such as one pushed with `[skip ci]` or by a workflow's `GITHUB_TOKEN`,
  is built by the workflow's hourly scheduled run. The gate below trusts its
  base commit instead.
- **Ruleset.** The active rulesets on the default branch, as
  `GET /repos/{owner}/{repo}/rules/branches/{branch}` reports them, have pull
  request rules that turn on *Require review from Code Owners*
  (`require_code_owner_review`), *Dismiss stale pull request approvals when
  new commits are pushed* (`dismiss_stale_reviews_on_push`), and *Require
  approval of the most recent reviewable push* (`require_last_push_approval`).
  Without the second, an owner's approval of a typo fix still counts after
  the author pushes a CODEOWNERS line naming themselves; without the third, a
  code owner can push to someone else's pull request and approve their own
  push. GitHub enforces the strictest rule of every ruleset that applies, so
  each setting may come from a different ruleset, but only from one that
  `GET /repos/{owner}/{repo}/rulesets/{id}` says the verifier's token cannot
  bypass (`current_user_can_bypass` is `never`): a workflow whose token can
  bypass the ruleset can push to the default branch unreviewed. The reason
  names each missing setting. Classic branch protection does not count,
  because a workflow token cannot read it: a project protected only that way
  reads self-approved everywhere until it adds a ruleset. Listing the other
  bypass actors needs admin access, so the people and apps a ruleset lets
  bypass it, repository admins included where it does, are trusted: what they
  merge is taken as reviewed.
- **Coverage.** CODEOWNERS at R gives every path that could exist an owner
  GitHub enforces, which is read from the rules, not from the files R tracks:
  a pull request adding a file nobody owns, such as a new workflow, needs no
  code owner review. The last matching rule decides and `*` matches every
  path, so every path is decided by the last `*` rule or a later one, and
  each of those must name an enforced owner; rules before the last `*`
  decide nothing. Only a literal `*` is read as matching every path: a file
  whose catch-all is `**` or `/**` is refused as having no `*` rule. Start
  CODEOWNERS with a line such as `* @owner` and narrow it after, for instance
  with `blueprint/ @alice`. Articles and read-back
  cards get no exception: any pattern may also match a directory, so none
  can be shown to match only Markdown, and the site copies every other file
  under the blueprint. An individual `@user` counts when GitHub gives them
  admin, maintain, or write permission. A team (`@org/team`) counts only when
  `org` is the repository's owner: GitHub enforces a team only when it has
  write access, which a workflow token cannot read, and reports a team
  without it as an `Unknown owner` error, which the next check refuses.
  Email owners do not count, and neither does a rule the parser cannot
  decide (see below).
- **GitHub's reading.** `GET /repos/{owner}/{repo}/codeowners/errors?ref=R`
  lists no error. GitHub skips a line it cannot parse and ignores an owner it
  cannot use, so any error (`Invalid pattern`, `Invalid owner`, `Unknown
  owner`, or another kind) refuses, naming the lines. The verifier's own
  parser only ever narrows what GitHub accepts.

When any of these but the current-head check fails, every approval is
self-approved with one reason that names the missing settings, GitHub's
errors, or the rules without an enforced owner, ten at most, and
`review check --authenticate github` prints it for each. The check is of R as
it is now; the verifier does not audit how R's CODEOWNERS or ruleset came to
be.

Then let p be an article's path and H its `review_approved` hash. The approval
is authenticated only when all of these hold:

1. **Recording commit.** In `git rev-list --first-parent R -- p`, take the
   unbroken run of commits ending at R in which p records H. Its oldest
   commit, whose first parent does not record H, is a candidate M, and so is
   each newer commit whose own diff adds a `review_approved: H` line, as
   re-approving does. Candidates are tried newest first; the first that
   satisfies steps 2 to 6 authenticates, and otherwise the reason lists what
   refused each. Moving the article's file starts a new run.
2. **Pull request.** GitHub associates M with exactly one pull request P,
   merged into the repository's default branch. A direct push, a pull request
   merged into another branch, and several candidates are all refused. Pull
   requests into other repositories that GitHub also lists for M, such as the
   upstream pull request of a project that is a fork, are left out.
3. **Content only.** P changes only articles and read-back cards, judged by
   the name and, for a rename, the previous name of every file in its
   complete file list. GitHub lists at most 3000 files, so a list that long
   is refused as possibly cut short; a shorter one is complete, so P's
   `changed_files` count, which the pull requests GitHub lists for a commit
   leave out, is not compared. Otherwise the reason says to record approvals
   in a pull request that changes only articles and read-back cards. This is
   what stops P from changing CODEOWNERS one commit before M, moving its
   article, or editing the verify workflow. The gate below applies it too.
4. **Visible diff.** P's file list shows a patch for p that adds a frontmatter
   line recording H, and p records H at P's head commit. A missing or
   truncated patch is refused.
5. **Reviewer.** Some reviewer's latest review of P (comments and pending
   reviews aside; ordered by submission time, then ID) is APPROVED on P's head
   commit. The reviewer is not P's author and is neither author nor committer
   of any of P's commits as GitHub links them to accounts (logins compared
   without case). A commit GitHub links to no account, or a pull request
   with 250 or more commits, refuses the approval, since then nobody can be
   shown not to have written it. The review shows the reviewer as OWNER,
   MEMBER, or COLLABORATOR, `GET /repos/{owner}/{repo}/collaborators/{login}/permission`
   gives them admin, maintain, or write, and they are an individual `@user`
   code owner of p both at P's base B and at R. B is the first of M's
   first-parent ancestors that GitHub does not associate with P
   (`GET /repos/{owner}/{repo}/commits/{sha}/pulls`): M's first parent,
   unless P was rebased onto the default branch, when P's own earlier
   commits, which could set CODEOWNERS, come between. P's `base.sha` is not
   used, because GitHub sets it to the base branch as of P's last update,
   which need not be the commit P landed on.
6. **CI on P.** The verify workflow has a successful `pull_request` run on P's
   head commit, and that run belongs to P. GitHub lists a run's pull requests
   only while they are open, so after the merge the run is tied to P through
   its branch: P comes from a branch of this repository that headed no other
   pull request (`GET /pulls?state=all&head=owner:branch` lists P alone), the
   run came from that branch of this repository, any pull request into this
   repository GitHub still lists on the run is P into the default branch, and
   P never changed its base branch (no `base_ref_changed` event). GitHub also
   lists pull requests into other repositories from the branch, which anyone
   can open in a fork; those are left out, because a run here belongs to a
   pull request into this repository. That run's `review check`
   fails unless H was current at P's head commit, the commit the reviewer
   approved.

Pull requests from forks, and into another repository, are refused before
anything about them is read, in the gate too: a fork's run lists no pull
requests and comes from another repository, so step 6 cannot tie it to P.
Record approvals from a branch of the project's repository, and use a fresh
branch name for each approval pull request. Nothing then depends on a head
commit only a fork holds: P's head stays readable in this repository through
`refs/pull/N/head` after its branch is deleted, and if GitHub ever cannot
read it, the approval reads self-approved.

Anything the verifier cannot decide, including a failed request (a later page
of a list included, and a 404 for any list but the branch's rules), a spent
request budget, or an undecidable CODEOWNERS rule, leaves that one approval
self-approved, and `review check --authenticate github`, `render
--authenticate github`, and the rendered label say why. When GitHub gave no
usable answer (a server error, a rate limit, which is a 429 or a 403 that
GitHub's rate-limit headers or its message mark as one, a timeout, a network
failure, or malformed JSON),
a list changed between its pages, or a budget below the ceiling ran out, any
of which a later run may get past, `render` also lists the approval with the
reason under `unchecked_approvals` in the site's `publication.json`; the Pages
workflow deploys that site, then fails the run, so a site that understates its
approvals never passes for a green build, and its hourly scheduled run builds
the head again. Any other HTTP error, such as a 401, any other 403, a 410,
or a 422, and an answer over 8 MiB come back the same on every
run, so they refuse the approval: it reads self-approved with the error as the
reason, is not listed as unchecked, and the run stays green. A token that
lacks a permission the verifier needs therefore labels approvals
self-approved, saying why in the log, rather than failing the run. Anyone who
can review a pull request, which in a public repository is any GitHub account,
can force a self-approved label this way: about 70 reviews with the longest
bodies GitHub allows, in characters that take two bytes or more in its answer,
make the pull request's list of reviews larger than 8 MiB, and every approval
that pull request recorded then reads self-approved, on every run. It never
makes a label read approved, it is visible in the label's reason, and it
reaches only approvals recorded in a pull request that account can review;
recording the approval again in a new pull request restores it, unless that
one is flooded too. A run first reads `GET /rate_limit`, which costs nothing,
and may make what the hour has left less 50, which it keeps for the rest of
its run (the deploy job) and a gate run beside it, but never more than the
hour's limit less 200, which it keeps for the gate and later pushes. GitHub
allows a workflow's `GITHUB_TOKEN` 1000 requests an hour in one repository,
shared by every run there (15,000 on GitHub Enterprise Cloud), so the ceiling
is 800 (14,800), and a run has all of it while at most 150 of the hour's
requests are spent when it begins, which leaves room for gate runs and other
workflows earlier in the hour. Two full builds within an hour, or a build
after many gate runs, start with less, and the approvals a spent budget below
the ceiling leaves are unchecked, with a reason that names the budget and what
the hour had left. When GitHub does not answer `GET /rate_limit`, a run takes
the budget of a `GITHUB_TOKEN` with the hour to itself, 800, and what it
leaves is unchecked too. The ceiling is the most any run gets, so the
approvals that a run with all of it still leaves are a verdict, not a failure:
they read self-approved with a reason naming the ceiling, are not listed as
unchecked, and the run stays green, as does every later run that begins with
at most 150 of the hour's requests spent while the approvals cost more than
the ceiling. Approvals are checked in the order of their article ids, so the
same ones stay past it. A project reaches the ceiling at about 90 approvals
each recorded in its own pull request, fewer when lists need more
than one page or other commits land between approval pull requests; approvals
recorded in one pull request share most of their requests. The precondition
costs one request for the repository, one for the default branch's head
outside the gate, one per page of rules, one per ruleset with a pull request
rule, one for GitHub's CODEOWNERS errors, and one permission lookup per
individual owner it checks. Each approval then costs, per pull request: the
commit's pull requests, those of each commit the walk to B passes (M's first
parent, and each earlier commit of a rebased P), P's files, p at P's head, P's
reviews, the approving reviewer's permission, P's commits, the pull requests
of P's branch, P's events, and the runs on P's head, one request each plus one
per extra page. Approvals recorded by the same pull request share them, and a
permission is looked up once per login. In the gate, steps 1, 2, and 6 cost
nothing, and P itself costs one request (`GET
/repos/{owner}/{repo}/pulls/{number}`). Only a misconfigured environment
(missing or malformed GitHub variables, or a blueprint outside the Git
checkout), a checkout without full history, an unknown trusted ref, or, in
`render`, a superseded build or a failed lookup of the default branch or its
head, including one the budget leaves no request for, stops the whole run.
Apart from missing or malformed GitHub variables, which it reports before
building anything, `review check` still prints its findings when
authentication stops, with every approval self-approved and the error as the
reason, and exits 2.

Code owners come from the first of `.github/CODEOWNERS`, `CODEOWNERS`, and
`docs/CODEOWNERS` that exists at a commit. P cannot name its own reviewer:
step 3 refuses a P that touches CODEOWNERS, and the reviewer must be an owner
both at P's base and at R, so neither can a P whose commits change CODEOWNERS
and change it back, which step 3 does not see. Because of that, a new owner
can approve only pull requests merged after the addition, and removing an
owner voids their earlier approvals. A first location that is not UTF-8, or
of 3 MB or more, which GitHub does not load, is refused instead of falling
through to the next. Lines end at a line feed (a trailing carriage return is
dropped) and tokens are separated by spaces and tabs. Only an individual
`@user` can approve an article: a team covers a file for the precondition,
but a workflow token cannot check who is in it, so name individual reviewers
for articles. A negated, bracketed, escaped, or malformed pattern, or an
owner in another form, leaves undecided the articles that line might decide
and, at or after the last `*` rule, fails the coverage check. A line holding
any other control or separator character, such as U+2028 or a non-breaking
space, might be split differently by GitHub, so it leaves undecided every
path that no later line matches.

Approve the final head. Ask a code owner to review only once `review check`
is green on the pull request's last commit: an approval of an earlier commit
is refused, and a push after the approval needs a new one. An approval whose
hash was merged without such a review (a direct push, an unreviewed,
self-reviewed, or mixed pull request, a moved article) reads self-approved
until a later pull request re-approves it: one that changes only that
article, rewrites its `review_approved` line (moving the line within the
frontmatter is enough), and is reviewed as above. Step 1 tries that newer
commit first.

To withdraw an approval, dismiss the reviewer's review on P: their latest
verdict is then no longer an approval, and the next Pages build reads
self-approved. Removing the reviewer from CODEOWNERS or revoking their write
access withdraws every approval they gave. A CODEOWNERS change is a push, so
its own build relabels the site. A dismissal, or a change of access, team, or
ruleset, starts no build: the Pages workflow's scheduled run rebuilds the site
a day after its last build, so the change shows within about a day and an hour
when that rebuild succeeds. It shows later when the rebuild fails, since each
failure delays the next try by an hour, then two, four, and so on up to a day;
when the repository's other runs keep more than 100 of each hour's API
requests spent, in which case the schedule never builds; and when GitHub delays
scheduled runs, or disables them in a public repository after 60 days without
activity. To show it at once, run the Pages workflow by hand
(`workflow_dispatch`).

Residual limits. Code owner review is checked at R as it is now, not as it
was when each pull request merged. A team GitHub enforces is trusted as a
whole: any member's review satisfies it. Ruleset bypass actors other than the
verifier's own token are trusted; anyone who can bypass the ruleset can merge
a pull request that re-records a hash without review. A `pull_request` run
tests the head merged into the base as it was then, not the merge that
landed. A review's `author_association` can understate a writer's access,
for example for a private organization member, which reads self-approved.
Pages decides when it builds: a review dismissed after the merge, or a verify
run that finishes after it, shows at the next Pages build, which the schedule
starts a day after the last unless one of the delays above holds it back.
Older commits are read with the current frontmatter parser, so a schema change
refuses rather than guesses. Signed SSH or GPG approvals (issue #49) are
planned as a second verifier behind the same interface.

`review authenticate` needs no Lean. It lists every recorded approval with its
status and does not judge whether approvals are current. With `--since REF` it
exits 1 when an approval added or changed relative to REF is not
authenticated; unchanged approvals are not looked up, and removing one needs
nothing. `--pr N` makes it the pre-merge gate: only reviews of pull request N
count, code owners come from `--trusted-ref` alone, and steps 1, 2, and 6 are
skipped because nothing is merged yet; the precondition except the current
head, the refusal of forks, and steps 3 to 5 apply. Without `--pr`,
`--since` applies the full rule, as Pages would, which suits the default
branch after a merge, not a pull request; run inside a `pull_request` event
without `--pr`, it prints a hint naming `--pr`.
Generated CI runs the gate in `autoform-review-gate.yml` on each pull request
of an opted-in project, and again when a review is submitted or dismissed,
with `--pr` and the base commit as both `--since` and `--trusted-ref`. The
base is the first parent of the merge commit GitHub builds for the pull
request, which is what the gate checks out (`git rev-parse HEAD^1`), not the
event's `base.sha`, which can lag behind it and would blame the pull request
for approvals others merged since. That
workflow is the pull request's own copy, so a pull request that edits it can
disable it; it is early feedback. The authoritative label is the one Pages
computes on the default branch with `--authenticate github`, from that
branch's workflow and CODEOWNERS.

The generated Pages workflow splits that computation from the Lean build,
because building runs the project's own code (`lakefile.lean` and every
dependency's build code), which must not run beside the token that reads
reviews or the artifact that becomes the site. Its `lean` job checks out the
project, builds it, and uploads only the skeleton report
`autoform skeleton --output` writes. Its `build` job starts fresh with full
history, installs Autoform from the pinned ref, downloads the report, and
runs `review check` and `render --review` with `--skeleton-report`; it never
runs Lake. A skeleton report records the hash of the blueprint it was
extracted from, and both commands refuse one that does not match their own
checkout or that covers selected articles only, so the `lean` job decides
which statements are current, never who approved them. Authentication reads
only the `build` job's checkout and the GitHub API. MkDocs still runs
`mkdocs.yml` and `theme/` in that job, which is why the precondition requires
them to have a code owner, and both jobs install uv with its cache disabled,
so nothing the `lean` job writes is restored into the `build` job. Its
`deploy` job, which needs only `contents: read` besides the Pages
permissions, first fails unless the `build` job ran in its own attempt
(`github.run_attempt`), then asks GitHub for the head of the default branch
and fails unless it is the commit the run built, with or without statement
review; a failed lookup fails the job too. The Pages artifact is named for
the attempt that uploaded it (`github-pages-N`), and the job deploys only the
one named for its own attempt. So a re-run of the `deploy` job alone, or
"Re-run failed jobs" after a green `build` job, both of which keep the
earlier attempt's render, never replaces the site, even while the commit is
still the head: that render can show an approval withdrawn since. A full
re-run, or a re-run of the `build` job (GitHub re-runs a job's dependents
with it), renders afresh and deploys only if its commit is still the head,
so no re-run of an older commit replaces the site. After deploying, the job
fails the run when `publication.json` lists any `unchecked_approvals`.

Some events start no Pages run: a dismissed review, a change of access, team,
or rulesets, a verify run that finishes after the merge, a push with `[skip
ci]` or by a workflow's `GITHUB_TOKEN`, and a run that failed or left
approvals unchecked, which nothing runs again. So the workflow also runs at 23
minutes past every hour (`schedule`), off the top of the hour, when GitHub
delays scheduled runs most. Its first job, `decide`, lets every event but the
schedule build. A scheduled run builds nothing unless the commit it was
scheduled for is still the head of the default branch, and then builds it only
when the site has no complete build of it, or when that build is a day old. A
complete build is a deployment of the head whose newest status is a success,
with no failed run of the head after it, so a run that deployed and then
failed on unchecked approvals does not count. After failed runs of the head it
waits an hour, doubling with each failure up to a day. Only the runs that
failed after the head's newest successful deployment count, so failures the
site has since recovered from never lengthen the wait; a run that deployed and
then failed leaves a failed deployment, so `decide` looks back through the
head's six newest deployments for a successful one, and when none of them is,
every failed run of the head counts. It first asks `GET
/rate_limit`, which costs nothing, and builds only when at most 100 of the
hour's requests are spent, so that the verification has all it may make, the
ceiling above, even after decide's own requests (at most nine) and 41 more by
other runs during the build; in a repository whose other runs keep more than 100 of
every hour's requests spent, the schedule never builds, and only
pushes and manual runs rebuild the site. A scheduled run that builds nothing
makes at most nine requests besides that one (the head, its newest
deployments, the status of each up to the newest that succeeded, at most six,
and the workflow's runs on the head), usually four: at most 216 a day, under
1% of the 24,000 the hourly limit allows. The daily rebuild is one
full verification, at most the ceiling a day, and is what bounds how long a
withdrawn approval stays on the site; rebuilding every hour could spend most
of every hour's allowance, leaving pushes and the gate short. The `decide` job
needs `actions: read` and `deployments: read` besides `contents: read`. When
GitHub does not answer one of its requests, or answers with something it
cannot read, it builds nothing and ends its run green with a warning, since a
red run would count as a failed run of the head; the next hour asks again. A
warning on every scheduled run means the schedule is not building at all. A
head whose build fails every time, such as one whose Lean does not compile, is
retried once a day after its first few failures. In a public repository GitHub
disables a schedule after 60 days without activity; re-enable the workflow
from the Actions tab. In a private repository GitHub bills Actions by the
minute, rounding each job up to a whole minute, so the schedule costs minutes
even when it builds nothing: each scheduled run's `decide` job is at least a
minute, about 24 a day and 720 a month, and every push and pull request run
pays a minute for its own `decide` as well. The daily rebuild adds its Lean,
build, and deploy jobs, longer when review checks run `lake build`. Every
pending run of a ref is kept (`queue: max`), so several quick pushes to one
pull request build one after another, not only the newest. That is a large
share of the minutes a plan includes, 2,000 a month on GitHub Free. A private
repository whose plan has no GitHub Pages fails `configure-pages` on every
build; delete the `schedule` trigger from
`.github/workflows/blueprint-pages.yml` there. Public repositories pay no
minutes on GitHub-hosted runners.

When `--output`, `--packets`, and `--passages` are combined, all three outputs
are staged before publication and a failed commit restores the previous set.
This is failure atomicity, not simultaneous visibility across paths: each
rename is atomic, but a reader opening several outputs during publication can
briefly observe different generations.

Plan durable article identity metadata without changing the blueprint:

```bash
autoform migrate article-ids blueprint --json
autoform migrate article-ids blueprint --check
```

`article_id` accepts opaque values in the form `af_` plus 24 lowercase hex
digits. The planner validates uniqueness, proposes deterministic IDs for
missing articles, includes exact source hashes, and is strictly read-only.
Applying plans, moving runtime consumers and claims to durable IDs, and
preserving publication routes are intentionally deferred to follow-up changes.

Coordinate temporary cross-machine ownership without modifying the book:

```bash
export AUTOFORM_WORKER_ID="agent-name"
autoform claim acquire "chapter/main-result"
autoform claim renew "chapter/main-result"
autoform claim release "chapter/main-result"
```

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
statement boxes with collapsed dependency details, multi-scale dependency
maps, and direct links to Lean declarations at the current commit:

```bash
autoform render blueprint --output site-src --lean-root . --require-declarations
```

`render` never writes into the vault. It leads the landing page with the project
map over a summary of what is formalized and what is unblocked, places a compact
progress summary after each chapter's opening prose, writes `structure.md` so a
vault's layout can be checked against the book it produces, and shows a source
icon when a `lean:` declaration resolves to a repository permalink. Its
`dependencies.md` entry point rolls dependencies through the article hierarchy,
with links to declaration maps, one-hop local contexts, and the complete DAG.
Every graph article returns to the book, and every formal statement links to
its local context. Each graph is written as raw HTML of class `bp-graph`,
which no article may hold, and `javascripts/blueprint-mermaid.js` draws those
elements and no other Mermaid. A title is shown in a graph as typed, and a
link, made from file and folder names, is percent-encoded, so a name can
neither end Mermaid's string nor make the link a `javascript:` URL. Point
`mkdocs.yml` at `docs_dir: site-src` and enable `md_in_html` plus a
`pymdownx.superfences` mermaid fence; see the [repository
example](../skills/setup/assets/cabannes-thesis-project/mkdocs.yml).

`render` also writes `javascripts/mathjax.js`, the site's MathJax
configuration, on every build; list it in `mkdocs.yml` and nothing else for
MathJax. It loads MathJax 3.2.2, the release the read-back checks were written
for, and refuses to typeset with any other. Another script in `mkdocs.yml`
cannot replace that configuration: an assignment to `window.MathJax`, such as
the snippet Material's documentation gives, is ignored with an error in the
browser console, and a script that changes it instead, such as one that sets
`window.MathJax.startup.pageReady` or `window.MathJax.options` (or, in a
project that lists the bundle too, `window.MathJax.config`), stops MathJax
with an error there, and the formulas stay as typed. MathJax itself starts on
a configuration made from the site's settings as it starts, which no other
script has seen, so a change the check cannot see, such as a proxy put in
place of part of the configuration, is not read either. The articles on a page
share one TeX input, with the packages base, ams, noundefined, boldsymbol,
cancel, and
mathtools, and the project's macros from `blueprint/tex-macros.json`, written
as MathJax's `tex.macros` is: a name maps to a body, `[body, arguments]`, or
`[body, arguments, default]`. A body or default may not end in a single
backslash, since MathJax would join it to the text after it into one command,
as `\` and `label` make `\label`. `check` and `render` judge and build the
script from the copy of `tex-macros.json` that `render` reads once and
publishes beside it, so an edit made during a render reaches neither. Each read-back card is typeset with a TeX input
of its own that knows only base, ams, and noundefined, so nothing an article,
another card, or the project's macros define reaches it. These inputs read
only the formulas the site's Markdown marks, such as `$...$` or `$$...$$` in an
article, or `` $`...`$ `` and a ```` ```math ```` fence, the forms GitHub
also shows as formulas. These are the formulas `check` judges, each under the
same TeX refusals. Text the site prints as typed,
such as a statement's title in its heading, a `discussion:` value, the lead on
the home page, the navigation, and a table of contents, is shown as typed,
TeX and all. A setting changed in
any formula's MathJax menu, such as the renderer or the explorer a screen
reader uses, applies to every formula on the page and on the pages shown after
it, while each card keeps its own TeX input. Every formula paints
only within its own band, the height of its line, and across only within
the paragraph, heading, list, or table cell that holds it, with either
renderer the menu offers and with any theme. So no article formula can cover
a card, a status mark, a label, another line, or the navigation beside the
text. Inside that block a formula paints as TeX sets it, so the text `\rlap`,
`\llap`, and the mathtools laps put beside a formula is kept, while an inline
formula wider than its paragraph is cut at the paragraph's edge; write a
formula that wide as a display, which scrolls on its own when it is too wide
for the page. Either renderer draws a formula at the size TeX set; neither
shrinks a wide one to fit. A card too wide for the page scrolls inside its
card, and a shade at its edge shows there is more that way. A card's approval
label wraps on a narrow screen instead of running off it.

A project scaffolded before `render` wrote that file keeps a
`blueprint/javascripts/mathjax.js` and lists the MathJax bundle in `mkdocs.yml`
after it. It needs no edits: `render` replaces a copy `autoform init` wrote
with its own, and the script finds MathJax already loaded and checks its
version instead of loading it. A copy edited since is refused, since the edits
would be lost; move its macros to `tex-macros.json` and delete it.
`render` writes `stylesheets/blueprint.css`, `javascripts/blueprint-mermaid.js`,
and `assets/autoform.svg` over the vault's copies the same way, and writes
`SUMMARY.md`, `structure.md`, `publication.json`, the `dependencies.md` and
`dependencies/` graph pages, and the chapter pages it consolidates. A
directory, symlink, or special file at any of these paths, at
`tex-macros.json`, or at the stale `dependencies.html`, `graph.html`, or
`progress.md` it keeps out of the site, or a file where one of their folders
goes, is refused by `check` and `render` with the path it is at, before
anything is written.

Besides its Markdown pages, a vault publishes only files a browser shows or
offers to save and runs nothing in: images (`.png`, `.jpg`, `.jpeg`, `.gif`,
`.webp`, `.avif`, `.bmp`, `.ico`), `.pdf`, and plain-text `.txt`, `.csv`,
`.json`, `.bib`, `.tex`, and `.lean` files, whatever the case of the suffix.
Render copies them into the site as they are. Any other file under
`blueprint/`, such as an `.html` page, an `.svg`, a script, or a stylesheet,
would be the site's own markup once a reader followed a link to it, so
`check` and `render` refuse it by its path: write it as Markdown, save it as
an image, a PDF, or plain text, or keep it outside `blueprint/`. A copy of one
of the files render writes, listed above, is not published; render's own is.

The site's Markdown reads `` $`...`$ `` and a ```` ```math ```` fence with
autoform's own extension, which `mkdocs.yml` lists under
`markdown_extensions` as `autoform_cli.markdown:FormulaExtension`, with a
`math` fence formatted by `autoform_cli.markdown.formula_fence`; the Pages
workflow installs autoform for the build. A project scaffolded before then
shows both as code until its `mkdocs.yml` and
`.github/workflows/blueprint-pages.yml` get those lines from `autoform init`'s
templates; `check` judges their TeX either way.

## Validation

`autoform check` rejects cycles, missing targets, escaping paths,
self-dependencies, cycles introduced at any rolled-up containment level,
missing or multiple H1 titles, unsupported frontmatter keys, and assertion
values it does not recognize. With `--lean-root` it also fails on a `lean:` name
absent from the sources, as `leanblueprint checkdecls` does for LaTeX
blueprints. It also refuses raw HTML in an article, as `review record` does in
a read-back: a tag, a character reference, an unclosed comment, or anything the
site's Markdown would pass through as HTML. A complete `<!-- ... -->` comment
is allowed and is left out of the rendered page. Code is shown as typed, so
markup written in code is allowed, and so is a reference a browser shows as
typed, such as the `&D;` in `R&D;`. Since every article on a page is typeset
with one TeX input, it refuses as well a TeX command in a formula, the only
text MathJax reads, that changes formulas other than its own: a definition such as
`\newcommand`, `\def`, `\let`, or `\DeclareMathOperator`, `\require`, a tag
form, or a `\label`. It refuses `\mmlToken` too, which colors a symbol as the
formula says, like the site's status marks, and a strike, `\cancel`,
`\bcancel`, `\xcancel`, or `\cancelto`, not followed by its argument: an
option in brackets after one colors the strike, pads it, or thickens it, and
a `]`, `}`, or the end of a macro's argument after one lets what follows
the macro be that option. Write `\cancel{x}`. These are every command of the
page's packages that colors what it draws when given a color, as a test
that typesets each of them with MathJax checks. Put the project's notation
in `blueprint/tex-macros.json`, which `check` validates too, by the same
rules: a macro may not use one of these commands, leave a strike without
its argument right after it, or put a `[` or another argument right after
an argument (`#1[`, `#1 #2`), where an article's argument ending in a strike
would take an option; write `{#1}` instead. A command you only
mention, outside a formula or in code, is shown as typed. Each of these
refusals names the line the HTML or the command is on.
A link, an image, or a link definition, used or not, may lead only to the
site's own pages or to an `http:`, `https:`, or `mailto:` address; one to a
`javascript:`, `data:`, or any other scheme, in any case, would run or show
what it holds on the site's origin, and is refused with the line it is on.
An address shown in code is allowed.
An attribute list may only give a heading an id, as in `## Title {#title}`;
any class, style, or other attribute, and an id on anything but a heading, is
refused with the line it is on, since it could make an article's text look
like a card, a status mark, or an approval label. The id starts with a letter,
uses letters, digits, `-`, and `_`, is at most 64 characters, does not start
with `bp-`, `autoform`, `mjx-`, `mermaid`, `md-`, or `__`, and is not an id
the site gives one of its own elements on that page, such as a statement's
anchor. The braces of a code fence
may only name its language, as in ```` ```{.lean} ````, and options such as
`title` or `linenums`; a class, an id, or another attribute there lands on the
code block and is refused the same way. A Mermaid diagram, such as a
```` ```mermaid ```` fence, is refused with its line too: the site draws only
the dependency graphs `render` writes, with the loose security their links
need, which would also run a click's `call` as script. Show a diagram's source
in a ```` ```text ```` fence instead. `check` reads each article as the site publishes it: every line break
Python reads (a form feed or U+2028 among them) as a new line, without the
metadata MkDocs takes off the top of a page, and a statement in its box with
its indentation kept, so an indented line stays code. A chapter's page is its
narrative with the statements `render` puts in place of their links, so each
stretch of the narrative between them is read on its own, as the page has it.
The site reads code fences over a whole page, so a fence at the margin left
open where an article, a statement, its notes, or a stretch ends would run on
through what `render` puts after it, status marks included, to the next fence
on the page; it is refused with the line it opens on, and the fix is to close
it.
Every other Markdown page the site publishes is held to the same rules, since
its script or classes would run and show on the same site as the cards: the
landing page `blueprint/README.md` (the text you wrote, before `render` adds
its dashboard), `coverage/README.md`, and any other Markdown page in the vault.
`check` reads each page, articles included, after `render` points its relative
links at the pages and anchors they land on, and refuses what it refuses in an
article, naming the file and the line. `render` writes a moved link's target
percent-encoded, so a target such as `%3Cscript%3E.md` stays a link and adds
no markup.
`render` refuses the same articles, so the site never publishes markup that an
article's reviewer read as text and the owners of its theme never saw. It validates structure and
leaves mathematical correctness to the agent and the Lean kernel.

The Markdown files are the source of truth. Graphs and sites are derived views
that may be regenerated at any time.

## Audit contract

`autoform audit` reports structured findings at blueprint-relative paths. It
checks that formalizable articles are declaration-sized leaves with statement
text and an explicit dependency section, that asserted proof and Mathlib facts
are internally consistent, and that cited work resolves to local source
material without escaping the blueprint. Coverage files are checked for broken
links and explicitly declared gaps. With `--lean-root`, local declaration names
and declaration kinds are checked against the Lean source index.

### Structure

Containment is inferred from nested `README.md` articles, so a chapter
directory without one is invisible to the hierarchy: its pages attach to the
roadmap root and the book loses a level. `missing-chapter-article` reports a
directory directly under `roadmap/` that holds articles but names no chapter.
Deeper directories (the `definitions/` and `theorems/` buckets the bundled
example uses) are a filing convention inside a chapter and are not checked.
`overfull-container` reports an article with more than 24 direct children,
which is a table of contents rather than a chapter. Both defects leave a valid
graph, which is why they need their own checks rather than falling out of
`autoform check`.

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

The audit API also accepts an already compiled graph. Future orchestration may
turn its findings into private work items, but the audit itself never enqueues
work, stamps articles, or creates another graph artifact.

## Claim contract

Claims use canonical `autoform-claim/v1` JSON in orphan commit messages and
exact observed object IDs as update preconditions. Absent and verifiably expired
leases may be acquired; live peer leases are refused. Malformed or unreadable
refs are unverifiable and may not be acquired, renewed, released, or removed by
cleanup. A heartbeat verifies ownership on entry and permanently records any
later refusal or transport uncertainty as lost ownership.

A claim key is a slug and digest of any string, not a validated node id, so a
shared resource is locked the same way a node is. Parallel agents get one Git
worktree each and serialize `lake build` behind a `lake-build` claim, because
builds share the elan toolchain and the Mathlib cache even when the checkouts
are separate.

Claims are temporary operational state, never article frontmatter. Future
Deicyde workers may share this protocol, but their current continue-uncoordinated
failure behavior must be removed before they use the canonical claim API.

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
review. The bundled example intentionally exits nonzero while its declared
coverage still holds `MAPPED` rows.

This command is strictly read-only and local. It does not invoke Git, GitHub,
subprocesses, network services, claims, queues, reviews, recovery state,
providers, workers, renderers, or dashboards, and it creates no cache, scratch
repository, service, state directory, or `graph.json`. It is a project/runtime
doctor, separate from any future Deicyde fleet or machine-capability preflight.

## Runtime contract

`autoform_cli.runtime` projects the canonical Markdown graph into the versioned,
deeply immutable in-memory schema `autoform-runtime/v1`. Its declared authority
is `markdown-articles`: the adapter copies hierarchy, typed statement and proof
dependencies, authored assertions, derived progress, provenance, and optional
local Lean source locations, but it provides no persistence or write API.
`RuntimeGraph.as_dict()` and `to_json()` are deterministic compatibility
snapshots for consumers, not an authored or generated graph file. Autoform never
creates, synchronizes, or treats `graph.json` as an authority.

Every article remains in the runtime view so consumers can preserve the book's
arbitrary containment hierarchy. A node is dispatchable only when it is both a
formalizable article and a leaf; narrative containers and prose-only leaves are
never proof work units. The source revision hashes exact roadmap article paths
and bytes, excluding timestamps, absolute paths, Git state, and operational
state. Optional Lean locations come from a local lexical scan and do not by
themselves establish compilation or proof correctness.

Schema v1 retains the graph's path-derived article ID. That is suitable for
an ephemeral runtime projection and temporary claims, but it is not yet an
approved durable identity. Queues, reviews, recovery records, PR markers,
dashboard routes, providers, and logs must not persist against this ID until a
path-move identity and migration policy is defined. Those records remain private
and excluded from runtime snapshots and publication.

## Publication contract

`autoform render` publishes the book, derived progress, and dependency maps at
project, chapter, nested-scope, local, and full-graph scales. It never reads a
`graph.json` or an operational queue. Hidden files are omitted, while symlinks,
credentials, logs, provider state, and agent/task state inside the blueprint
cause the render to fail rather than silently leak them. Source and output
directories must be disjoint.

A render reads each published file once. Loading the blueprint captures every
article and every source file a passage cites; the render then captures the
other files it publishes and refuses a roadmap page that appeared after the
load. Pages, copied files, page order, and the source-content hash are all
computed from those bytes, so a file edited mid-render cannot be published
beside a hash or page order that describes different bytes. A published file
must be a regular file of at most 64 MiB.

Every render writes `publication.json` with the source-content hash, Git ref,
article and dependency counts, and available views, and, with
`--authenticate`, `unchecked_approvals`: each approval the verifier could not
finish checking, with the reason. It contains no timestamp or absolute path,
so identical inputs produce identical output files.
