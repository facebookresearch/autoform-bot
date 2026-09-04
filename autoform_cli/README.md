# Blueprint format and CLI

The Autoform CLI validates, visualizes, and publishes the multilevel dependency
graph embedded in a blueprint vault's `roadmap/` directory. A root
`.autoform.toml` can register multiple vaults in one repository; the original
single-vault layout remains available at `blueprint/`. The Markdown book is the
graph: no separate authored or generated graph file exists.

## Articles and containment

Every Markdown file below a selected vault's `roadmap/` is an article node. A
`README.md` represents its directory and strictly contains the articles below
it; the nearest ancestor `README.md` is the single parent. This supports any
number of levels, from book to chapter to section to declaration. Ordinary
files use their path without `.md` as the current graph ID; `README.md` uses its
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
intent and generated CI checks it against the built declaration. The supported
intents are `abbrev`, `axiom`, `class`, `corollary`, `def`, `definition`,
`inductive`, `instance`, `lemma`, `opaque`, `proposition`, `structure`, and
`theorem`; theorem-like aliases share Lean's kernel-level theorem kind.
Declarations that introduce data rather than a proposition carry no separate
proof obligation.

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
| `mathlib: true` | The exact result exists in the pinned Mathlib dependency. Requires `mathlib_declaration` and `mathlib_file`. |
| `mathlib_declaration: Ns.decl` | Exact upstream declaration name(s). |
| `mathlib_file: Mathlib/Path/File.lean` | Exact Mathlib source file that declares the upstream name(s). |
| `not_ready: true` | Needs more blueprint work before it can be attempted. |
| `lean: Ns.decl` | Declaration name(s) that discharge the article. |
| `discussion: 42` | Issue number or URL where the article is being discussed. |

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

### Multi-project workspaces

Use a workspace manifest when a repository contains several blueprint efforts,
when the legacy `blueprint/` name conflicts with its established layout, or when
Autoform must coexist with unrelated documentation. Initialize the
repository-level registry without creating a vault, then add projects
independently:

```bash
autoform workspace init . --blueprint-root docs/blueprints
autoform blueprint new finite-flat \
  --path FiniteFlat --title "Finite Flat Group Schemes"
autoform blueprint new another-project \
  --path AnotherProject --title "Another Project"
autoform blueprint register imported-project \
  --path ExistingVault --title "Imported Project"
autoform workspace inspect .
autoform blueprint list .
autoform workspace check . --lean-root .
```

The generated `.autoform.toml` is the sole ownership registry:

```toml
schema = "autoform-workspace/v1"

[locations.blueprints]
path = "docs/blueprints"
provides = ["blueprints"]

[projects."finite-flat"]
title = "Finite Flat Group Schemes"
blueprint = { location = "blueprints", path = "FiniteFlat" }
```

Location and project identifiers are user-defined. Autoform recognizes the
generic `blueprints` capability; it never special-cases repository names such
as those shown in examples. A repository may declare additional named locations
and capabilities for other tooling or future Autoform features. Project
blueprint paths name immediate child directories of their selected collection,
and registered vault paths may not overlap, so every managed vault has a
distinct boundary even when a repository declares several locations.

No `autoform.toml` is written inside a vault. Autoform checks only entries under
`projects`; unregistered siblings are ignored. Paths are repository-relative,
portable to common case-sensitive and case-insensitive filesystems, confined
beneath the workspace root, distinct under Unicode-normalized case-insensitive
comparison, and may not traverse symbolic links. Windows-reserved names,
forbidden characters, control characters, trailing dots, and surrounding
whitespace are rejected before any filesystem mutation. Registration uses a
TOML-aware edit, preserving comments and supporting both standard and inline
`projects` tables while validating the complete result before publishing it
atomically.
Workspace mutation currently requires POSIX-style file locking, no-follow
opens, and directory-descriptor support; unsupported platforms fail before
writing. The manifest format itself remains portable across common
case-sensitive and case-insensitive filesystems.

From inside a registered vault, single-project commands select that project. A
workspace root also selects its sole registered project. Autoform never infers
a project from an unrelated directory; pass `--project` there and at a
workspace root containing multiple projects:

```bash
autoform check . --project finite-flat --lean-root .
autoform audit . --project finite-flat --lean-root .
autoform doctor . --project finite-flat --lean-root .
autoform-visualize . --project finite-flat
autoform render . --project finite-flat --output site-src/finite-flat
```

`workspace check` is the repository-wide verification command and visits every
registered project exactly once, applying the same path and symlink checks as
single-project commands. Explicit vault paths continue to work for
ad-hoc inspection, but do not add an unregistered directory to repository-wide
checks. Workspace initialization currently creates only the manifest and its
blueprint collection; publication remains an explicit per-vault setup decision.
`blueprint register` validates an existing vault and adds only the root registry
entry, which is the migration path for pre-existing blueprint directories.
Workspace JSON responses carry operation-specific versioned schemas so callers
can distinguish initialization, blueprint changes, listing, inspection,
checking, and errors.

### Legacy single-vault setup

Create a new project's vault, site configuration, and CI. The layout is fixed,
so it is written rather than described. This command is retained for dedicated
repositories already using the canonical lowercase `blueprint/` layout:

```bash
autoform init . --title "Finite Flat Group Schemes" \
  --repository-url https://github.com/owner/repo
```

Pass `--autoform-source <credential-free-https-git-url>` and
`--autoform-ref <sha>` together to pin the generated workflows at an immutable
commit. Passing only one is an error. Use `--force` to overwrite and `--json`
for machine-readable output.

Do not run legacy `init` in a manifest-managed workspace: it would create an
unregistered `blueprint/` vault. Use `workspace init` and `blueprint new`
instead. Likewise, `project repair` deliberately refuses manifest-managed
workspaces until shared publication infrastructure has a workspace-aware repair
contract.

Create or inspect a Lean project and list Autoform's bundled known-good release pairs:

```bash
autoform project versions
autoform project provenance --json
autoform project new ./FiniteFlat \
  --package FiniteFlat \
  --release lean-v4.32.2-mathlib-v4.32.2 \
  --autoform-source https://github.com/facebookresearch/autoform-bot.git \
  --autoform-ref <full-commit-sha>
autoform project new . \
  --package FiniteFlat \
  --release lean-v4.32.2-mathlib-v4.32.2 \
  --autoform-source https://github.com/facebookresearch/autoform-bot.git \
  --autoform-ref <full-commit-sha>
autoform project inspect .
autoform project inspect path/inside/project --json
autoform project repair . --dry-run --json
autoform project repair . --title "Finite Flat Group Schemes" \
  --repository-url https://github.com/owner/repo
autoform project versions --json
```

`project new` requires an absent target, or the literal target `.` when the
current directory is empty, plus an explicit release ID. An absent target uses
one atomic no-replace rename. The `.` form preserves the directory inode and
mode and uses a durable, recoverable transaction to publish each top-level
entry without replacement. It never overwrites an existing path. Exactly one
cooperative concurrent creator can win; ambiguous recovery state is preserved
for inspection rather than deleted.
The command does not run Git, Lake, Lean, subprocesses, or network operations.
It accepts an already verified Autoform source and full commit together, and
omits generated workflows when neither is supplied.

`project provenance` is the online step. It accepts only the exact plugin-root
checkout or the bounded Codex installer record, fetches the recorded commit,
and compares the installed plugin and importable packages with that commit.
It reports a credential-free HTTPS source and full SHA only after all checks
pass. A plain wheel cannot infer provenance. Run it before creating the
consumer target, then pass both returned values to `project new`.

`project repair` operates only on an explicitly named project root that already
has a clean, supported Lake/Lean configuration. It preserves every existing
managed path byte-for-byte and adds only absent Autoform overlay files whose
content is canonical and unambiguous. Missing parameterized files require their
exact inputs: `--title`, `--repository-url` (use an explicit empty value when
that is intended), and the `--autoform-source`/`--autoform-ref` pair for
workflows. A project whose workflows were deliberately omitted remains valid;
a partial workflow pair does not. Repair never writes when preflight finds a
symlink, unsafe or missing parent, stale repair temporary, malformed
configuration, unsupported release, or missing input. `--dry-run` performs the
same plan without mutation. Calls serialize on the project root, and
publication is atomic per file without replacing a concurrent writer. After an
interrupted multi-file repair, inspect any reported retained path, then retry
with the same inputs; there is no operation-wide transaction. If the project
changes after a file is published, repair retains that file and reports it for
manual recovery rather than risk unlinking a concurrent replacement. A failed
pre-publication attempt likewise retains and reports its exact temporary path
rather than deleting by pathname after a separate identity check. Like creation
and inspection, repair runs no Git, Lake, Lean, subprocess, or network operation.

`project inspect` is deterministic, local, and read-only. It discovers the
nearest project root; parses bounded `lakefile.toml`, `lean-toolchain`, the
optional `.autoform.toml`, and known Autoform paths; records configuration
hashes; and reports whether the
configured Lean/Mathlib pair exactly matches the bundled catalog. It does not
run Lake, Lean, Git, subprocesses, or network operations. A `lakefile.lean` is
reported as present but unevaluated because executing it would violate that
boundary. Symlinked decision-bearing configuration and malformed consumed
fields fail inspection. Reports contain only project-relative paths, never the
host's absolute project location.

`project versions` reads the catalog packaged with the installed wheel. The
catalog is an explicit known-good allowlist, not a resolver: the command never
contacts a registry, selects a version, or mutates a project. An unlisted but
structurally valid pair is advisory; absence from this catalog does not prove a
project is incompatible. It is a snapshot refreshed when Autoform is released;
its single recommended entry is the newest stable Lean and Mathlib pair
validated at that time.

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

Generated CI additionally rebuilds the root Lake package, then checks every
local `lean:` target belongs to one of those built modules. Each
`mathlib_declaration` must exist in the module named by `mathlib_file`. Lake
must resolve that module from the sole `mathlib` entry in `lake-manifest.json`.
That entry must pin a full commit from the canonical upstream Mathlib URL, and
the checked-out dependency must have the matching clean Git `HEAD` and origin.
The queried artifacts must remain inside that checkout's build directory, and
must carry a valid Lake build or cache trace; a full build trace must record the
`mathlib` package id. Local path packages, forks, mirrors, dirty or mismatched
checkouts, and other dependencies exporting a `Mathlib.*` module are rejected,
as is a root-package declaration impersonating a Mathlib result. Existence and
declaration kind are read from Lean's environment, not inferred from source
text.

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

For exhaustive source work, opt in with exact frontmatter:

```markdown
---
schema: autoform-coverage/v2
artifact: sources/book.txt
artifact_sha256: <64 lowercase hex characters>
---

| Unit | Area | Lines | Locator | Unit SHA-256 | Coverage | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| chapter-one | First chapter | 1-42 | Chapter 1 | <span hash> | DECOMPOSED | [Result](../roadmap/result.md) |
```

The artifact must be a nonempty, regular, non-symlink UTF-8 file with LF line
endings and a final LF. Ordered one-based spans must partition it exactly, and
each unit hash covers the raw LF-terminated bytes in that span. A decomposed
unit may link only to formalizable roadmap leaves. Those leaves reciprocate in
their frontmatter with `source_units: [chapter-one]`. The immutable
`load_execution_input` API binds this contract to the unchanged
`autoform-runtime/v1` projection; schema-less v1 remains valid for audit and
render but is refused for ready-work discovery with `coverage-v2-required`.

List the formalization phases that are ready for the current agent:

```bash
autoform ready blueprint --lean-root .
autoform ready . --project finite-flat --lean-root . --json
```

`ready` is read-only. It loads the same immutable execution input, requires
durable `article_id` frontmatter, and returns only dispatchable formalizable
leaves whose statement or proof prerequisites are satisfied. Its deterministic
JSON includes the roadmap and source-contract revisions, structured blocked
items with unmet dependency IDs, and counts for ready, blocked, and complete
work. With local formalized progress, pass `--lean-root`; `ready` rejects
missing or unresolved Lean targets rather than treating stale metadata as
complete. It does not acquire a claim or edit files; use the claim commands
below before beginning the returned item.

A v2 publication excludes the entire `blueprint/sources/` authority tree, so
renamed artifacts cannot survive an incremental render. With repository
coordinates its authored Markdown links become repository blob links; without
them, those links become plain text instead of dangling site links. Raw HTML
links into the excluded tree are rejected.

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

Plan durable article identity metadata without changing the blueprint:

```bash
autoform migrate article-ids blueprint --json
autoform migrate article-ids blueprint --check
```

`article_id` accepts opaque values in the form `af_` plus 24 lowercase hex
digits. The planner validates uniqueness, proposes deterministic IDs for
missing articles, includes exact source hashes, and is strictly read-only.
Apply the proposed IDs to article frontmatter before dispatching collaborative
work. Claims resolve current roadmap paths to these durable IDs, so renaming an
article does not change its lock.

Coordinate temporary cross-machine ownership without modifying the book:

```bash
export AUTOFORM_WORKER_ID="agent-name"
autoform claim acquire "chapter/main-result"
autoform claim renew "chapter/main-result"
autoform claim release "chapter/main-result"
autoform claim acquire --resource lake-build
autoform claim list
autoform claim cleanup
```

Claims are fail-closed compare-and-swap leases under
`refs/autoform-claims/` on the Git `origin`; pass `--repo` for another claim
board. Article targets are resolved against the current project's `blueprint/`
and keyed by their durable `article_id`; use `--blueprint` when invoking the
command elsewhere. Raw locks require `--resource`. The CLI derives a stable
session from the worktree, with `--session-id` or
`AUTOFORM_CLAIM_SESSION_ID` as an explicit override. A failed acquire or renew
means the caller cannot prove ownership and must stop before committing or
pushing protected work. Claims do not prove mathematical correctness and do
not replace branch-level Git CAS. `list` and `cleanup` need neither a worker nor
a worktree when `--repo` and, if needed, `--scratch` are supplied.

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

Publication is staged, synced, validated, and atomically exchanged with the
previous generated site. This fail-closed transaction requires macOS
`renameatx_np` or Linux `renameat2`; other platforms can still use the remaining
CLI commands but cannot run `autoform render`. A legacy
`autoform-publication/v1` output is never deleted automatically. Remove it
explicitly or choose an empty output directory once, then subsequent v2 renders
can replace only the exact checksummed generation they inspected.
The renderer hashes both the blueprint snapshot and the exact Lean-file
generation used for declaration links, then rechecks both under the publication
lock immediately before the atomic rename. That check is the publication
linearization point; later source edits belong to the next render. Generated
v1/v2 publication trees and private staging directories are never indexed as
Lean source.
An existing v2 publication from before Lean-source hashing is still replaced
only after its complete inventory is verified, then upgraded in place.

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

The audit API also accepts an already compiled graph. Future orchestration may
turn its findings into private work items, but the audit itself never enqueues
work, stamps articles, or creates another graph artifact.

## Claim contract

Claims use canonical `autoform-claim/v2` JSON in orphan commit messages. A
cryptographically random lease ID and a session-local receipt for the exact
pushed object fence every ownership operation; `worker_id` is display metadata,
not authority. Live peer leases cannot be stolen. Leases are limited to 3600
seconds and assume clocks differ by at most 300 seconds. Entries outside those
bounds fail closed, appear as `_recovery_required` in `claim list`, and are
recovered only by an explicit CAS-safe `claim cleanup`. A heartbeat captures one
lease ID, records any refusal or transport uncertainty as lost ownership, and
waits for an in-flight renewal before exiting.

Owners must stop at `expires_at`. Other observers cannot take over or clean up
the ref until `expires_at + 300` seconds, so the intervening skew window fails
closed instead of admitting two owners. A valid unrenewed lease is bounded by
its 3600-second TTL plus this 300-second reclaim grace; a timestamp already 300
seconds ahead of an observer can add at most one further 300-second offset.
Renewals clamp their timestamp and expiry to the prior values when a clock steps
backward.

Moving from v1 path keys to v2 durable IDs is a one-way rollout. Stop v1 clients
before the first v2 claim. Autoform refuses live or unreadable v1 author refs,
replaces expired v1 refs with permanent compatibility fences, and installs a
fence for the current article path or raw resource name before acquiring its v2
key. Old clients reject those fences, so they cannot acquire a path already
owned by v2. Historical renamed paths with no claim ref cannot be discovered;
retiring v1 clients is therefore part of the protocol, not an optional cleanup.
Use `claim cleanup --blueprint PROJECT` during rollout so expired v1 path refs
become fences while expired durable-ID refs remain reusable.

Article claims require a real graph node with materialized `article_id`
frontmatter. Separate raw-resource keys cover coordination outside the roadmap.
Parallel contributors get one Git worktree each and serialize shared build state with
`autoform claim acquire --resource lake-build`.

Leases are temporary operational state; compatibility fences are persistent
migration state. Neither belongs in article frontmatter. The Orchestrate skill
is a thin client of `ready`, `claim`, `check`, and `audit`; there is no second
worker scheduler or provider-specific execution service.

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
subprocesses, network services, claims, reviews, orchestration state,
providers, renderers, or dashboards, and it creates no cache, scratch
repository, service, state directory, or `graph.json`. It is a project/runtime
doctor, separate from ready-work discovery and machine-capability preflight.

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

Each node retains its path-derived `id` for links and carries optional authored
`article_id` frontmatter for identity across path moves. `autoform ready` and
article claims require that durable identity; ordinary check, audit, render,
and human review continue to support roadmaps while IDs are being migrated.

## Publication contract

`autoform render` publishes the book, derived progress, and dependency maps at
project, chapter, nested-scope, local, and full-graph scales. It never reads a
`graph.json` or an operational queue. Hidden files are omitted, while symlinks,
credentials, logs, provider state, and agent/task state inside the blueprint
cause the render to fail rather than silently leak them. Source and output
directories must be disjoint.

Every render writes `publication.json` with blueprint and Lean-source hashes,
Git ref, article and dependency counts, complete file inventory, and available
views. It contains no timestamp or absolute path, so identical inputs produce
identical output files. Autoform validates and syncs the staged tree before one
atomic filesystem commit, then verifies ownership and syncs both parent
directories. Once the commit begins, Autoform never tries to exchange a recovery
path back into the live destination. If it cannot verify the final state or
durability, it preserves the private workspace and reports its exact recovery
path instead of deleting a potentially unique generation. If the site was fully
verified before cleanup becomes unsafe, the render succeeds and reports the
retained workspace as a warning.
