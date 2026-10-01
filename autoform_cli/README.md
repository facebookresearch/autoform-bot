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
  --with pymdown-extensions mkdocs build --strict
```

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
that name stops the extraction. A declaration's packet reads as it does to a
file that imports its module, whichever other articles are extracted with it.
That environment has the module's global notation, instances, and
attributes, including any the module declares after the declaration; it lacks
the module's `local notation` and local instances, and the `open` and
`set_option` commands in effect at the declaration. The probe adds exactly two
printing options, `pp.funBinderTypes` and `pp.coercions.types`, so a reader
sees what each binder ranges over and where each cast lands; signatures
otherwise print as `#check` prints them in a file whose only import is the
module. The helper module's own imports (`Lean.Elab.Command`,
`Lean.Util.CollectAxioms`, `Lean.Util.Path`, and `Lean.Data.Json`) are in
that environment too, with whatever global notation and instances they
declare. Before any probe, Lake must confirm once, without rebuilding, that
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
report identifies the exact blueprint, its complete target set, and whether
the extraction covered all targets or an explicit `--node` selection. It
quotes each trusted definition's source and records theorem
dependencies by elaborated signature, so it stands on its own without ever
copying a theorem proof. Declarations in one project share most of what they
trust, so the report states each trusted declaration, each external
constant's semantic material, and each boundary module's identity once, in the
top-level `trusted`, `semantics`, and `boundary_modules` tables, and each
declaration names the entries it uses; the probe's own output is shared the
same way. A trusted declaration is printed in its root's module environment
and can read differently under two root modules, so `trusted` is keyed by root
module and then by name, and a declaration may name only the entries under its
own module. The probe also states each elaborated subterm of 256 bytes or more
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
project may need. Each probe pays a Lean start and loads its
module's imports; on a Mathlib project that measured about 10 CPU-seconds and
140 MB of private memory per probe, with the `.olean` files mapped and shared.
Probes run in parallel on half the CPU cores, at most eight. They are resource
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
bidirectional overrides), TeX comments, and raw HTML are rejected. HTML tags,
comments, declarations, and character references are refused by name before
parsing, wherever they are written, so in a formula put a space after `<`. TeX
may use only an allowlist of the notation statements need, kept in
`readback.py` against the site's pinned MathJax 3.2.2: letters, symbols,
relations, operators, arrows, delimiters, fractions, roots, accents, fonts,
`\text`, thin to double-quad spaces, and the `cases`, `aligned`, and matrix
environments. Any other command or environment, such as `\phantom`, `\rlap`,
`\kern`, `\color`, `\tag`, or a macro definition, is refused by name, as are
listed commands given arguments that show nothing (`\mathrm{}`), two negative
spaces or more than four spaces in a row, and row spacing after `\\`. Testimony
must also show at least one letter or digit. Before any card is parsed its
testimony must fit limits well above what real read-backs use: 32 KiB, 500
lines, 1,024 math delimiters, 512 backticks in runs of at most 16, 64 opening
brackets, 64 columns of nesting, 256 tag openers (`<` before a letter, `/`,
`!`, or `?`), 256 underscores that start a word, and 2,048 backslashes. The
Markdown parser is superlinear in each of these, so a byte limit alone would
not bound it, and every card in a pull
request is read before its validity is known. Cards are read through no-follow
descriptors, so a card or directory swapped for a link is skipped. A write
walks to the card's directory without following links and takes that
directory's lock. If the directory was moved or replaced before the lock was
taken, a fresh walk no longer reaches it, so the write lets it go and walks
again, three times at most before it refuses; nothing is staged until the
directory it holds is the one the card's path reaches. The write then stages
the card in a unique temporary file and checks the card it would replace. Just
before the exchange it checks that the temporary file still holds, unchanged,
the card it wrote. It exchanges the staged file with the old card in one atomic
step, so the card's name holds a complete card at every moment, and checks that
the card it swapped out is the one it compared. If an editor saved over the
card in between, the write withdraws its card and reports a conflict: it
exchanges the two files back, and if the editor saved again over the new card
meanwhile, it keeps exchanging until the card's name holds the newest save; a
card deleted meanwhile stays deleted. The error names each temporary file left
holding a card, and why it was left. A first card is renamed into place without
replacing a name. An optional expected-card hash makes updates
compare-and-swap, and the conflict check a batch runs first applies the same
rule: a card that names a hash conflicts with a missing card in both. Filing
identical content succeeds without writing. Success is reported only if, with
the lock still held, a no-follow walk from the blueprint finds at the card's
path the file the write left there, compared by device, inode, and content
hash. The lock orders Autoform's own writers. Other writers are safe when they
work by path: an editor that renames a saved file over the card, deletes it, or
opens it with truncation after the write swapped the new card in either makes
the write fail with a conflict or acts after it, and its save is never lost. A
program that opened the card before the write swapped it, with or without
truncation, and writes to it afterwards is not safe: those bytes go to the
replaced file, which is deleted. A reader can briefly see a card that is then
withdrawn: the new card, when an editor's save collides with a write or an
interrupt or transient error stops a write after its exchange. The lock belongs
to the open file description, which a process forked during a write shares, so
a write unlocks before it closes; a write that cannot get the lock within ten
seconds gives up. If a process dies holding the lock, the lock is released once
no process shares that description: at once, unless such a child is still
running. A crash, or a second interrupt while a write cleans up after the
first, can leave a `.autoform-readback-*.tmp` file beside the card, holding
either the unpublished card or a card taken from the card's name; the loader
never reads it as a card. An interrupt while a write withdraws its card starts
the withdrawal over; if an editor also saved during it, the older save can be
left at the card's name and the newer in the temporary file the warning names.
An interrupt that lands just as a write opens a file or directory can leak that
descriptor, never a locked one, until the process exits; a temporary file
created that way is still removed. Publishing needs Linux with `renameat2`
(glibc 2.28 or later, on a filesystem with atomic exchange such as ext4, XFS,
Btrfs, or tmpfs) or macOS with `renameatx_np` (APFS). Elsewhere, including
Windows, a write and the batch conflict check are refused before anything is
created or read; cards can still be loaded. A filesystem that rejects the
atomic rename when it is called (`ENOSYS`, `EINVAL`, or `ENOTSUP`) is found
only then: the write is refused with the card untouched and no temporary file
left, but the card's directory may already have been created. `model:` remains
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
intent into it. Lean's parser, not a separate lexer, locates those comments.
The probe parses each source in the environment of its root's module, which
has that module's global notation and the notation of everything it imports,
but not the file's `local` notation.
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
under its own compare-and-swap; only a concurrent writer can stop it midway,
the command says how many cards it filed, and because filing identical content
is a no-op, running the same batch again completes it.

`review check`, `audit`, and `render` re-extract the
current Lean evidence and reject unresolved, partial, foreign, or stale
bundles. `review check` also requires complete current cards and matching human
approvals, and judges them as one state of the blueprint: it loads the
articles and cards before Lean runs, the extraction must have seen those same
articles, and the cards must be unchanged when it ends. Article text read
after that load, such as a statement compared with its prepared evidence, must
be the bytes the load parsed, and the verdict judges only that state, its
extraction, and those cards. An edit that breaks any of these comparisons fails the
check with `review-snapshot-changed` rather than mixing old and new evidence;
run it again once the blueprint is idle. These are comparisons, not a lock: an
article or card that changes and is restored between two of them goes unseen,
and the restored state is what gets judged; an edit after the last comparison
does not affect a verdict already reached. `audit --review-bundle`, `render --review`, and
`render --review-bundle` make the same check, then judge or show the cards it
validated rather than reading them again. They load the articles again to audit
or render them, and an edit to any article since the check fails them with
`review-snapshot-changed` too: every skeleton report records a hash of the
blueprint it was extracted from, and review evidence is never built from a
report and articles that hash differently. The article text they then audit or
render must be the bytes that second load parsed, as it must for a plain
`audit` or `render`; otherwise the audit reports `article-changed` and the
render stops. The rendered review disclosure uses the same packet bytes that were
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

- **Ruleset.** An active ruleset on the default branch has a pull request rule
  with *Require review from Code Owners*, as
  `GET /repos/{owner}/{repo}/rules/branches/{branch}` reports it. Classic
  branch protection does not count, because a workflow token cannot read it: a
  project protected only that way reads self-approved everywhere until it adds
  a ruleset. The ruleset's bypass actors, and repository admins where it lets
  them bypass, are trusted: what they merge is taken as reviewed.
- **Coverage.** CODEOWNERS at R gives every tracked file except articles and
  read-back cards an owner GitHub enforces. Articles are the Markdown files
  under `<blueprint>/roadmap/` and cards those under `<blueprint>/readbacks/`.
  Everything else needs an owner: CODEOWNERS itself, `.github/`, `lakefile.*`,
  `lake-manifest.json`, `lean-toolchain`, the Lean sources, `mkdocs.yml`,
  `theme/`, and every other Markdown file, `blueprint/README.md` included. A
  team owner (`@org/team`) counts: GitHub enforces a team only when it has
  write access, and a workflow token cannot read team permissions, so teams
  are trusted. An individual `@user` counts when GitHub gives them admin,
  maintain, or write permission. Email owners do not count, and neither does a
  rule the parser cannot decide (see below).

When either fails, every approval is self-approved with one reason that names
the uncovered files, ten at most, and `review check --authenticate github`
prints it for each. The check is of R as it is now; the verifier does not
audit how R's CODEOWNERS or ruleset came to be.

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
   merged into another branch, and several candidates are all refused.
3. **Content only.** P changes only articles and read-back cards, judged by
   the name and, for a rename, the previous name of every file in its
   complete file list; a list GitHub truncates is refused. Otherwise the
   reason says to record approvals in a pull request that changes only
   articles and read-back cards. This is what stops P from changing
   CODEOWNERS one commit before M, moving its article, or editing the verify
   workflow. The gate below applies it too.
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
   code owner of p both at M's first parent and at R.
6. **CI on P.** The verify workflow has a successful `pull_request` run on P's
   head commit, and that run belongs to P. GitHub lists a run's pull requests
   only while they are open, so after the merge the run is tied to P through
   its branch: P comes from a branch of this repository that headed no other
   pull request (`GET /pulls?state=all&head=owner:branch` lists P alone), the
   run came from that branch of this repository, any pull request GitHub
   still lists on the run is P into the default branch, and P never changed
   its base branch (no `base_ref_changed` event). That run's `review check`
   fails unless H was current at P's head commit, the commit the reviewer
   approved.

Pull requests from forks are refused at step 6: a fork's run lists no pull
requests and comes from another repository, so a workflow token cannot tie it
to P. Record approvals from a branch of the project's repository, and use a
fresh branch name for each approval pull request.

Anything the verifier cannot decide, including a failed request (a later page
of a list included), a spent budget of 500 requests, or an undecidable
CODEOWNERS rule, leaves that one approval self-approved, and
`review check --authenticate github` and the rendered label say why. The
precondition costs one request for the repository, one per page of rules, and
one permission lookup per individual owner it checks. Each approval then costs,
per pull request: the commit's pull requests, P's files, p at P's head, P's
reviews, the approving reviewer's permission, P's commits, the pull requests
of P's branch, P's events, and the runs on P's head, one request each plus one
per extra page. Approvals recorded by the same pull request share them, and a
permission is looked up once per login. Only missing credentials, a shallow
checkout, or an unknown trusted ref stops the whole run.

Code owners come from the first of `.github/CODEOWNERS`, `CODEOWNERS`, and
`docs/CODEOWNERS` that exists at a commit. P cannot name its own reviewer:
step 3 refuses a P that touches CODEOWNERS, and the reviewer must be an owner
both at M's first parent and at R. Because of that, a new owner can approve
only pull requests merged after the addition, and removing an owner voids
their earlier approvals. A first location that is not UTF-8, or of 3 MB or
more, which GitHub does not load, is refused instead of falling through to
the next. Lines end at a line feed (a trailing carriage return is dropped)
and tokens are separated by spaces and tabs. Only an individual `@user` can
approve an article: a team covers a file for the precondition, but a
workflow token cannot check who is in it, so name individual reviewers for
articles. A negated, bracketed, escaped, or malformed pattern, or an owner in
another form, leaves undecided the articles that line might decide, and
leaves its files uncovered. A line holding any other control or separator
character, such as U+2028 or a non-breaking space, might be split differently
by GitHub, so it leaves undecided every path that no later line matches.

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
access withdraws every approval they gave.

Residual limits. Code owner review is checked at R as it is now, not as it
was when each pull request merged. Teams, ruleset bypass actors, and admins
are trusted; anyone who can bypass the ruleset can merge a pull request that
re-records a hash without review. A `pull_request` run tests the head merged
into the base as it was then, not the merge that landed. A review's
`author_association` can understate a writer's access, for example for a
private organization member, which reads self-approved. Pages decides when it
builds: a review dismissed after the merge, or a verify run that finishes
after it, shows at the next Pages build. Older commits are read with the
current frontmatter parser, so a schema change refuses rather than guesses.
Signed SSH or GPG approvals (issue #49) are planned as a second verifier
behind the same interface.

`review authenticate` needs no Lean. It lists every recorded approval with its
status and does not judge whether approvals are current. With `--since REF` it
exits 1 when an approval added or changed relative to REF is not
authenticated; unchanged approvals are not looked up, and removing one needs
nothing. `--pr N` makes it the pre-merge gate: only reviews of pull request N
count, code owners come from `--trusted-ref` alone, and steps 1, 2, and 6 are
skipped because nothing is merged yet; the precondition and steps 3 to 5
apply. Without `--pr`, `--since` applies the full rule, as Pages would, which
suits the default branch after a merge, not a pull request; run inside a
`pull_request` event without `--pr`, it prints a hint naming `--pr`.
Generated CI runs the gate in `autoform-review-gate.yml` on each pull request
of an opted-in project, and again when a review is submitted or dismissed,
with `--pr` and the base commit as both `--since` and `--trusted-ref`. That
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
so nothing the `lean` job writes is restored into the `build` job.

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
its local context. Point `mkdocs.yml` at `docs_dir: site-src` and enable
`md_in_html` plus a `pymdownx.superfences` mermaid fence; see the [repository
example](../skills/setup/assets/cabannes-thesis-project/mkdocs.yml).

## Validation

`autoform check` rejects cycles, missing targets, escaping paths,
self-dependencies, cycles introduced at any rolled-up containment level,
missing or multiple H1 titles, unsupported frontmatter keys, and assertion
values it does not recognize. With `--lean-root` it also fails on a `lean:` name
absent from the sources, as `leanblueprint checkdecls` does for LaTeX
blueprints. It also refuses raw HTML in an article, as `review record` does in
a read-back: a tag, a character reference, an unclosed comment, or anything the
site's Markdown would pass through as HTML. A complete `<!-- ... -->` comment
is allowed and is left out of the rendered page. `render` refuses the same
articles, so the site never publishes markup that an article's reviewer read
as text and the owners of its theme never saw. It validates structure and
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

Every render writes `publication.json` with the source-content hash, Git ref,
article and dependency counts, and available views. It contains no timestamp or
absolute path, so identical inputs produce identical output files.
