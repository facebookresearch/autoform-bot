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

## Assertions and derived status

An article asserts only facts a human or agent verified:

| Key | Meaning |
| --- | --- |
| `statement: formalized` | The Lean statement exists and compiles. |
| `statement: retracted` | A revision retracted the statement while `lean:` still names the old declaration, which stays in the build until Formalize restates the article and records `statement: formalized` in its place. Requires `lean:`; invalid with `proof: formalized` or `mathlib: true`. CI's `autoform check` at an older `AUTOFORM_REF` rejects the marker, so move the pin first; until then, retract by removing `statement`, `statement_hash`, and `proof` and keeping `lean:`. |
| `statement_hash: sha256:...` | The statement Agent Review passed: `sha256:` and 64 lowercase hex digits, as `autoform skeleton` prints it. Requires `statement: formalized`; invalid with `statement: retracted` or `mathlib: true`. CI fails when the statement drifts from it, as `autoform skeleton --check-statements` reports. |
| `proof: formalized` | The Lean proof compiles. Under the open policy it may rest on open statements, and the article is then conditional; only the derived `fully_proved` means complete and `sorry`-free. |
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
autoform skeleton blueprint --lean-root . --check-statements
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

Each article also gets a **statement hash**, printed as `statement_hash` in
`--json` and as a `== ID · statement_hash sha256:...` line in the text report.
It is the SHA-256 of canonical JSON over the schema `autoform-statement/v1`,
the article's statement text (the published statement: the body between the
title and the first heading, dependency sections dropped, with line endings
and trailing whitespace normalized), and the sorted, de-duplicated name, kind,
and elaborated meaning of every `lean:` declaration and every project
declaration its statement rests on. A theorem's meaning is its type, so a proof
leaves the hash alone; axioms, external assumptions, boundary modules, the
Lean version, and the cited passage are left out. A toolchain bump rotates it
only when it changes the elaborated terms. It is null when any of the article's
names is unresolved. The report records the statement text it hashes.

Formalize records the hash as `statement_hash` beside `statement: formalized`
once the statement passes Agent Review. `--check-statements` then compares
every recorded hash with the current one and exits 1 with one line per drifted
article:

```text
error: ID: statement_hash OLD is recorded but the statement now hashes to NEW; re-review the statement and record the new hash
```

An article that records a hash but has no `lean:`, or one of whose names is
unresolved, also fails. Articles without the key are not checked; when none
records one, the command prints `no article records statement_hash; nothing
to check` and exits 0 without running Lean, and otherwise a clean run prints
`N recorded statement hash(es) match`. It honours `--timeout` and does not
combine with `--node`, `--json`, `--output`, `--packets`, or `--passages`. The
generated `autoform-verify.yml` runs it after the kernel-trust audit, so a type
edit that still compiles, or an edit to the article's statement text, fails CI
instead of keeping the article proved. Like the drift hash, it is a drift
check, not reviewer authentication or an approval key. A project whose
`AUTOFORM_REF` predates the key stops at `autoform check` with `unsupported
frontmatter key 'statement_hash'`; move the pin and replace the workflow with
the version `autoform init` writes before recording hashes.

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
report the migration blocker. An item whose article records `statement:
retracted` is a revision: it carries `revision` true in JSON, and the text of
`work list` adds a `revision:` line and `work context` a `Revision:` line saying
to start from `autoform work impact` (see the [revision
contract](#revision-contract)). Both commands are read-only projections of
Markdown.

Under the open policy the text output of `work list` starts with an `Open
statements: allowed` line and adds an `assumes:` line under each item that rests
on open statements; `work context` prints `Open statements:` and `Assumes:`
lines. In JSON, the frontier and every item carry `open_statements`, and every
item carries `assumes`, the open statements its proof rests on (empty under the
strict policy). The runtime projection carries the same `open_statements` flag,
and each node's runtime status adds `assumes` and `waiting_on`, the
prerequisites that keep an unproved node from its next phase.

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
allowed. It reads Markdown only and needs no Lean build.

Ask what revising an article's Lean declarations would affect before editing
them:

```bash
autoform work impact chapter/result . --lean-root .
autoform work impact chapter/result . --lean-root . --declaration MyProject.helper --json
```

The revised set is the article's `lean:` declarations, or the `--declaration`
names, each of which must be a project-local constant; a private declaration
goes by the name its source gives it, and `work impact` refuses to run while an
article's `lean:` or a `--declaration` names several private declarations at
once. `work impact` runs a Lean probe against the built project through the
same freshness check, output bound, and process handling as `skeleton`, so it
needs a fresh `lake build` and a writable `.lake`, and it carries the same
trust caveat: run it only in a trusted checkout or a sandbox. `--timeout
SECONDS` sets the probe's budget, 600 seconds by default. The report lists:

- statement-impacted articles, whose declarations' meaning (a type, or a
  definition's body or an inductive's constructors) reaches the revised set,
  where a theorem whose type is exactly another constant's and whose proof is
  that constant, as `alias` writes, shares that constant's meaning, even when
  written by hand;
- proof-impacted articles, whose proofs or definition bodies use the revised
  set, directly or through Lean-generated companions such as the `_simp_1`
  lemma `simp` uses, without their meaning changing;
- helpers that no article names, each with an owner when one exists: the
  article naming its nearest ancestor by name, such as `Foo` for `Foo.aux`;
- impacted articles without a Markdown dependency path to the revised article;
- every deprecated project declaration with its replacement, its users, not
  counting companions Lean generates for it such as `X.eq_1`, and the other
  articles whose `lean:` names it, and in `deprecated_unused` those with no
  users and no such article;
- the claim targets: the revised article's first, then every impacted
  article's, every helper owner's, and, for a helper without an owner, a
  `lean/<slug>-<digest>` key derived from its name, so two revisions touching
  the same helper contend for the same claim; each helper reports its key as
  `claim_target`. A revised declaration that no article names is claimed the
  same way, under its owner's target or its own key.

A revision is `contained` when no other article uses it and every helper it
impacts or revises belongs to the revised article, so its only claim target is
that article's; it can then be made in place under
that article's claim. A declaration derived from a revised one without naming
it in its statement changes with it, but `work impact` does not report that
declaration's users: the additive form `to_additive` writes, which goes
unreported itself, or a direction `alias ⟨mp, mpr⟩ :=` takes of an `Iff`, which
shows only as proof-impacted. Check such derivations on the revised set by
hand. The probe compares only types and values, so it cannot see a change to an
instance's priority or scope, to an attribute such as `@[simp]` or `@[ext]`, or
to notation: such a revision is never contained, whatever `contained` says, and
its claim targets are incomplete, so treat every stated article whose Lean
imports the changed module, directly or not, as statement-impacted. `--json`
writes `autoform-impact/v1`. The [revision
contract](#revision-contract) says what to do with the answer.

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
autoform claim acquire af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
```

Claim an article by the `claim_target` that `work context` reports. The board
hashes whatever string it is given, so a claim on an article's path ID and one
on its `article_id` do not exclude each other. Each concurrent agent needs its
own worker ID, because a second acquire by the same owner succeeds. Where shell
state does not persist between commands, as in agent tool calls, pass it with
`--worker-id` on every command instead of exporting `AUTOFORM_WORKER_ID` once.
Leases expire after 1500 seconds unless `--ttl` sets another length. Renew well
within that, and confirm a claim is still held with `renew`, not `acquire`,
which also succeeds once a lease has expired or been released. Several targets
in one command change all-or-nothing; see the [claim contract](#claim-contract).

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
blueprints. It validates structure and leaves mathematical correctness to the
agent and the Lean kernel.

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

The audit API also accepts an already compiled graph. Formalize may use its
findings while working the Markdown frontier, but the audit itself never
enqueues work, stamps articles, or creates another graph artifact.

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
absent) and exits 1 with `error: ...` on malformed frontmatter. Under
`forbidden` the audit is unchanged: any `sorryAx` fails with `NAME depends on
unexpected axiom sorryAx` and `root-package declarations failed the
kernel-trust audit`. Under `allowed` the workflow writes the `autoform work
assumptions blueprint --json` contract and audits every root-package
declaration against it. The audit accepts a `sorry` in a declared open
statement's own proof and a proof that reaches only the open statements its
article's Markdown dependencies reach. It rejects a `sorry` in a statement, a
`sorry` anywhere else in the root package, a dependency outside the root package
that depends on `sorry`, an article declaration that reaches an open statement
its Markdown dependencies do not reach (so a fully proved article, which
assumes nothing, may reach none), a `lean:` name missing from the build,
and an open statement that its article records as proved. Each article
declaration gets at most one of these status lines, with `NAME` the
declaration and `ID` the article's node ID, and one with an error gets none
of the last three, which read as passing:

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

The step runs `autoform work assumptions` from `AUTOFORM_REF`, and the earlier
`autoform check` step validates the frontmatter with that same pin. Scaffolded
workflows pin `AUTOFORM_REF` to the Autoform checkout that scaffolded them, so a
project scaffolded before open statements existed must, before opting in, move
`AUTOFORM_REF` to a commit that has `work assumptions` and replace
`.github/workflows/autoform-verify.yml` and `.github/autoform_audit.py` with the
versions `autoform init` writes at that commit. Both halves fail closed: an
older pin stops at `autoform check` with `unsupported frontmatter key
'open_statements'`, and an older workflow runs the strict audit, which rejects
every `sorry`.

To reproduce the open-statement audit locally after a build, with
`ROOT_PACKAGE` the Lake package name the workflow reads from `lake
translate-config toml`:

```bash
lake build
lake pack /tmp/autoform-root.tgz
autoform work assumptions blueprint --json > /tmp/autoform-assumptions.json
python3 .github/autoform_audit.py --open-statements /tmp/autoform-assumptions.json \
  ROOT_PACKAGE /tmp/autoform-root.tgz /tmp/autoform-probe.lean
lake env lean /tmp/autoform-probe.lean
```

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
Parallel agents get one Git worktree each and serialize `lake build` behind a
`lake-build` claim, because
builds share the elan toolchain and the Mathlib cache even when the checkouts
are separate.

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

Claims are temporary operational state, never article frontmatter. Future
Deicyde workers may share this protocol, but their current continue-uncoordinated
failure behavior must be removed before they use the canonical claim API.

## Revision contract

Revising a declaration X of article R that other articles' Lean uses touches
work R's claim does not cover. This contract makes that work claimable and
keeps the default build passing. Roadmap records a requested revision in the
Markdown (step 6); Formalize carries out the Lean side (steps 1 to 5).

1. On a fresh build, run `autoform work impact R . --lean-root .`, with
   `--declaration` when only some of R's declarations, or a helper, change.
2. Choose the route:
   - **Contained** (`contained: true`): revise X in place under R's claim.
     `contained` ignores R's own declarations, so re-check R's other
     declarations that use X as well.
   - **Expand, migrate, contract**, the default whenever anything uses X: add
     X' with the revised statement, leave X unchanged and mark it
     `@[deprecated X' (since := "YYYY-MM-DD")]`, and point R's `lean:` at X'.
     Under the open policy, while X's proof is still `sorry`, R's `lean:`
     names X beside X', so the audit keeps accepting that `sorry` as an open
     statement, and R records `proof` only after step 4 deletes X.
     Statement-impacted articles replace `statement: formalized` with
     `statement: retracted`, lose `statement_hash` and `proof`, and keep
     `lean:`, so they return to the frontier as revisions; under the open
     policy a statement-impacted theorem stays an open statement meanwhile
     (see [open statements](#open-statements)). When X's proof is sorry-free,
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
     Lean changes: R, the statement-impacted articles, and, in that case, the
     proof-impacted ones.
   - **In place**, only when X and X' cannot coexist, for example an instance
     or a structure change: the claim set is every `claim_targets` entry.
     Repair every impacted declaration in one commit whose default build
     passes. A change `work impact` cannot see, to an instance's priority or
     scope, an attribute, or notation, also takes this route whatever
     `contained` says, and its `claim_targets` are incomplete: add every stated
     article whose Lean imports the changed module and treat it as
     statement-impacted. A statement-impacted article keeps `statement` only
     after an Agent Review of its source faithfulness under X's new meaning,
     and then records its new `statement_hash`; otherwise it records
     `statement: retracted`, loses `statement_hash` and `proof`, and keeps
     `lean:`. A repaired dependent proof keeps `proof: formalized` only after
     an Agent Review of the repair; otherwise it loses `proof`. A theorem's
     proof that cannot be repaired becomes exactly `sorry` under the open
     policy; otherwise delete the declaration and remove its article's
     `lean:`, `statement`, `statement_hash`, and `proof`, which works only
     when nothing else uses it. When neither applies, the
     revision is blocked: release the claims and report it. Record what
     happened under `## Execution notes` of each touched article.
3. Claim the route's claim set with one `autoform claim acquire`. When it is
   refused, release everything and report the held claim as the blocker. After
   acquiring, re-run `work impact`; if the set grew, release and start over
   with the larger set. After rebasing onto the current shared branch and
   rebuilding, re-run it once more; if the set grew, acquire the whole larger
   set in one command under the no-hold-and-wait rule of the [claim
   contract](#claim-contract) and repair the new targets before landing. Under
   the open policy, reproduce the CI audit as [open statements](#open-statements)
   shows before landing. Land one commit, then
   release every claim.
4. Contract: delete a deprecated X once it appears in `deprecated_unused` of
   `work impact R . --lean-root .`, meaning no declaration uses it and no
   other article's `lean:` names it, and drop it from R's `lean:` in the same
   commit. `contained` is not enough: it ignores R's own declarations, such as
   an X' built from X.
5. `autoform audit --lean-root` reports `lean-target-deprecated` for an article
   whose `lean:` names a declaration with `deprecated` in its own `@[...]`
   attribute list; point it at the replacement. The check is lexical, so it
   misses a later `attribute [deprecated] X`, which the deprecated list of
   `work impact` does see. Under the open policy the finding is expected for a
   superseded X that step 2 keeps in R's `lean:` until step 4, so `autoform
   audit --lean-root` and `autoform doctor --lean-root` fail in that window by
   design, while CI, which runs neither, passes.
6. When Roadmap revises an article, its statement text or only its Lean, it
   records the decision and retracts the article: it replaces `statement:
   formalized` with `statement: retracted`, removes `statement_hash` and
   `proof: formalized`, and keeps `lean:`, which `work impact` needs, so the
   article returns to the frontier as a revision; an article without `lean:`
   just loses `statement`, `statement_hash`, and `proof`. It retracts only
   that article and the dependents whose Markdown text the revision rewrites;
   the Lean-side impact decides every other dependent. Roadmap edits only
   Markdown: it releases its claims and
   leaves the Lean revision to Formalize.

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
never proof work units. The source revision hashes exact roadmap article paths
and bytes, excluding timestamps, absolute paths, Git state, and operational
state. Optional Lean locations come from a local lexical scan and do not by
themselves establish compilation or proof correctness.

Schema v2 exposes optional durable `article_id` metadata beside the graph's
path-derived `id`. Temporary claims and local dashboard hooks may fall back to
the path ID, but durable execution records and routes must require `article_id`
until the path-move migration is complete. Operational state remains private and
excluded from runtime snapshots and publication.

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
