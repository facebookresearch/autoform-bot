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
“Additional formalization targets” section rather than disappearing.

Keep the root roadmap article short: it is the book's preface and table of
contents, not a dump of every planned milestone. Large planning inventories
belong in coverage or progress views; detailed containers should introduce their
mathematics in prose and place statements under meaningful section headings.

Frontmatter records checked facts:

```markdown
---
article_id: af_5b0e4d3c2a1f09e8d7c6b5a4
area: Analysis & Probability
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
obligation; leading modifiers such as `noncomputable` do not change that, and an
`axiom` does not get this shortcut.

`origin` records provenance for formalizable work: `cited` for a direct source
target, `bridged` for a result introduced between source targets, and
`background` for prerequisite mathematics.

Frontmatter is optional. A container article that only supplies prose and
placement needs none at all; only checked facts are recorded.

The optional `area` field assigns a container to an authored mathematical
region in the knowledge atlas. Use mathematical areas such as `Foundations` or
`Geometry & Topology`, never repository or workflow buckets such as
`MathlibExt` or `catalog-01`. Autoform does not guess areas from paths, imports,
or titles.

A repository-wide inventory may use a narrative leaf to summarize an existing
Lean module containing several declarations. Such a leaf sets
`catalog: module` and omits `declaration`, so it is never dispatched as one
proof task. A completed catalog records every exact compiled public name in
`lean:` and links a declaration ledger under `blueprint/sources/` from its exact
`## Sources` section; it may assert
`statement: formalized` and `proof: formalized` when the complete module has
been checked. It contributes to the neutral inventory metric and graph status
while remaining a readable catalog page, but stays outside the mathematical
`Scoped roadmap` completion percentage.

`check --lean-root` and `audit --lean-root` resolve every compiled name the
catalog lists and audit validates the local ledger link. They cannot prove that
the list omitted no newly added public declaration; completeness remains an
authored assertion pending repository-inventory reconciliation.

## Assertions and derived status

An article asserts only facts a human or agent verified:

| Key | Meaning |
| --- | --- |
| `area: Geometry & Topology` | Authored mathematical region for atlas grouping. |
| `catalog: module` | A non-dispatchable leaf cataloging one existing Lean module. |
| `statement: formalized` | The Lean statement exists and compiles. |
| `proof: formalized` | The Lean proof is complete. |
| `mathlib: true` | The result is upstreamed into Mathlib. |
| `not_ready: true` | Needs more blueprint work before it can be attempted. |
| `lean: Ns.decl` | Declaration name(s) that discharge the article. |
| `discussion: 42` | Issue number or URL where the article is being discussed. |
| `article_id: af_...` | Durable identity, `af_` plus 24 lowercase hex digits; `autoform work` requires it on unfinished formalizable leaves. |

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

Inspect a Lean project and list Autoform's bundled known-good release pairs:

```bash
autoform project inspect .
autoform project inspect path/inside/project --json
autoform project versions --json
```

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
pairs. It is an allowlist, not a resolver.

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
statement may name a Lean declaration that does not exist yet.

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
into the blueprint. Pass `--json` for stable machine-readable output; a nonzero
exit status means the audit found at least one issue. The machine-checkable
`coverage/README.md` contract contains one `Area | Coverage | Evidence` table
with `MAPPED`, `INVENTORIED`, `DECOMPOSED`, `DEFERRED`, or `OUT` dispositions.
`MAPPED` is nonterminal. `INVENTORIED` is the terminal disposition for exact
source accounting and must link to a `catalog: module` record, directly or
through a containing roadmap scope. `DECOMPOSED` is reserved for
source-grounded declaration articles and must link to one, directly or through
a containing roadmap scope. `DEFERRED` and `OUT` record an explicit later
milestone or exclusion. Audit JSON includes canonical rows, counts, and the
exact coverage source hash, while `publication.json` records aggregate counts
without duplicating the authored rows.

One row carries one disposition, so represent both axes with distinct,
axis-qualified `Area` labels. For example, `Repository inventory / MathlibExt`
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
for `DECOMPOSED`. Every local link it offers must resolve, fragments included,
under the same rules the audit applies. A link missing its closing parenthesis
does not render and does not count.

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
and progress-state breakdown use non-container articles carrying
`declaration`; a `catalog: module` record contributes only to the separate
module-inventory count. The coverage `counts` object always includes an integer
`INVENTORIED` key, including zero when no row uses it. Status assertions on a
catalog remain accepted. The presentation calls a fully checked catalog
`inventory checked`, not a completed definition or result. The rendered site
and `autoform check` show the module-inventory count separately from those
target metrics.

For an existing blueprint, migrate a catalog-only `DECOMPOSED` row to
`INVENTORIED`. If the same source also has declaration articles, keep that
inventory row and add distinct, axis-qualified `DECOMPOSED` rows for the
mathematical scopes; do not overwrite the inventory claim. This is a
backward-compatible extension of `autoform-coverage/v1` and
`autoform-publication/v1`: existing Markdown and frontmatter still parse, and no
manifest schema version changes. Until migrated, a catalog-only `DECOMPOSED` row
remains syntactically accepted but audit reports `coverage-role-mismatch`; a
catalog not reached by any `INVENTORIED` evidence also reports
`unclassified-inventory`. Target-completion percentages may change because
catalog records are no longer included in their denominator.

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
environment, or reaches a refused declaration; that name is unresolved for its article only, other articles still extract, and it writes nothing into the vault;
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
that names no text, because it points outside the blueprint, names a missing
file or non-UTF-8 text, or names no lines of it, leaves the article's
declarations unresolved. `--passages DIR` writes those passages beside
the packets, one per article, in a separate, disjoint managed directory. It
requires `--packets`. Each article directory also holds `article.lean`, the
joint packet of every declaration the article
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
is unblocked. Project CI rejects `sorry`, so a theorem's statement lands with
its proof, and an article's statement phase also waits until its `## Proof
depends on` prerequisites are proved. The derived `can_state` state and the
site's Next up card do not apply this gate; dispatch from `work list`. `work
context` accepts the path-derived node ID (see Articles and containment) or an
assigned `article_id` and reports the exact article, dependencies, source
targets, Lean targets, blockers, article and graph source revisions, and claim
target. The article revision hashes that article's bytes alone. A Lean target's
`source_file` is relative to `--lean-root`, and null without one or when the
local scan does not find the declaration. The scan skips build output and nested
checkouts, meaning any subdirectory with a `.git` entry, such as a worker's
worktree or a submodule. Blockers are unmet dependency IDs or one of
`roadmap:not-a-formalizable-leaf`, `roadmap:proof-without-statement`,
`roadmap:missing-article-id`, `roadmap:missing-article-revision`, and
`roadmap:not-ready`. The claim target prefers durable `article_id` metadata.
`work list` fails explicitly if an unfinished formalizable leaf lacks one; plan
the missing IDs with `autoform migrate article-ids` and add them to the
frontmatter. `work context` may still select that article by its path ID to
report the migration blocker. Both commands are read-only projections of
Markdown.

Plan durable article identity metadata without changing the blueprint:

```bash
autoform migrate article-ids blueprint --json
autoform migrate article-ids blueprint --check
```

`article_id` accepts opaque values in the form `af_` plus 24 lowercase hex
digits. The planner validates uniqueness, proposes deterministic IDs for
missing articles, includes exact source hashes, and is strictly read-only.
Runtime v2 and `autoform work` expose assigned IDs immediately; applying plans
and preserving publication routes across path moves remain follow-up changes.

Coordinate temporary cross-machine ownership without modifying the book:

```bash
export AUTOFORM_WORKER_ID="agent-name"
autoform claim acquire af_5b0e4d3c2a1f09e8d7c6b5a4
autoform claim renew af_5b0e4d3c2a1f09e8d7c6b5a4
autoform claim release af_5b0e4d3c2a1f09e8d7c6b5a4
```

Claim an article by the `claim_target` that `work context` reports. The board
hashes whatever string it is given, so a claim on an article's path ID and one
on its `article_id` do not exclude each other. Each concurrent agent needs its
own worker ID, because a second acquire by the same owner succeeds. Where shell
state does not persist between commands, as in agent tool calls, pass it with
`--worker-id` on every command instead of exporting `AUTOFORM_WORKER_ID` once.
Leases expire after 1500 seconds unless `--ttl` sets another length. Renew well
within that, and confirm a claim is still held with `renew`, not `acquire`,
which also succeeds once a lease has expired or been released.

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
explorer over a summary of what is formalized and what is unblocked, places a compact
progress summary after each chapter's opening prose, writes `structure.md` so a
vault's layout can be checked against the book it produces, and shows a source
icon when a `lean:` declaration resolves to a repository permalink. Its
`dependencies.md` opens a full-viewport application and rolls dependencies
through the article hierarchy, with bounded project, chapter, and nested-scope
projections. Global search loads a layout-free index and jumps to the smallest
useful scope; the compatibility `dependencies/full.md` route renders only the
project shell and never fits the whole repository into one canvas. Every generated
site projection uses the same deterministic JSON-derived Canvas and semantic-DOM
explorer with search, filters, pan/zoom, and hash-routed node neighborhoods, so
none inherits Mermaid's text, edge, or SVG-size ceilings. Authored Mermaid remains
supported in the vault and book, including the bounded graph produced by
`autoform-visualize`. Breadcrumbs return through the explorer hierarchy to the
book, and every formal statement links to the smallest explorer scope containing
that item. Point `mkdocs.yml` at `docs_dir: site-src` and enable `md_in_html` plus a
`pymdownx.superfences` mermaid fence; see the [repository
example](../skills/setup/assets/cabannes-thesis-project/mkdocs.yml).

This shared-explorer layout is recorded as `autoform-publication/v2`; it
replaces the v1 `dependencies/nodes/*.html` focus pages. Legacy
`dependencies/full.html#node=<id>` links use the global index to redirect to the
smallest bounded scope containing that node. Existing v1 output directories
remain recognized so a normal clean render upgrades them in place.

## Validation

`autoform check` rejects cycles, missing targets, escaping paths,
self-dependencies, cycles introduced at any rolled-up containment level,
missing or multiple H1 titles, unsupported frontmatter keys, and assertion
values it does not recognize. With `--lean-root` it also fails on a `lean:` name
absent from the sources, as `leanblueprint checkdecls` does for LaTeX
blueprints. It validates structure and leaves mathematical correctness to the
agent and the Lean kernel.

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
`overfull-container` reports a mathematical article with more than 24 direct
children, or a repository-wide root subject index with more than 64,
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

The audit API also accepts an already compiled graph. Formalize may use its
findings while working the Markdown frontier, but the audit itself never
enqueues work, stamps articles, or creates another graph artifact.

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
deeply immutable in-memory schema `autoform-runtime/v2`. Its declared authority
is `markdown-articles`: the adapter copies hierarchy, typed statement and proof
dependencies, authored assertions, derived progress, provenance, and optional
local Lean source locations, but it provides no persistence or write API.
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

Schema v2 adds non-dispatchable module catalogs and exposes optional durable
`article_id` metadata beside the graph's path-derived `id`. The path ID is
suitable for an ephemeral runtime projection, temporary claims, and local
dashboard hooks. Durable queues, reviews, recovery records, PR markers, routes,
providers, and logs must require `article_id` until path-move migration is
complete. Operational state remains private and excluded from runtime snapshots
and publication.

## Publication contract

`autoform render` publishes the book, derived progress, and one dependency
explorer as bounded project, chapter, and nested-scope projections. The legacy
full route is a project-scale compatibility shell backed by a layout-free global
search index, never an all-repository node cloud. Rendering never reads a
`graph.json` or an operational queue. Hidden files are omitted, while symlinks,
credentials, logs, provider state, and agent/task state inside the blueprint
cause the render to fail rather than silently leak them. Source and output
directories must be disjoint.

Every render writes `publication.json` with the source-content hash, Git ref,
article and dependency counts, available views, and whether capture used a
retained directory descriptor or the documented portable best-effort path. It
contains no timestamp or absolute path, so identical inputs on the same
filesystem-capability class produce identical output files.
Rendering reads only one retained source snapshot. Later edits cannot mix into
the output; they instead make the dashboard report the built site as stale
until it is rendered again.
