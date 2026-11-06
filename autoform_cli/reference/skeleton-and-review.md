# Autoform skeletons and statement review

What `autoform skeleton` extracts and writes, its hashes, read-back cards, and
the `review` rules. The [CLI reference](../README.md#skeleton) gives the
synopses, exit statuses and safety rules.

## Skeleton contract

The skeleton of a `lean:` declaration is its elaborated signature, then every
project declaration its *statement* rests on, transitively, quoted from the
sources in dependency order: a definition with its body, a theorem with its type
only, though Lean's axiom analysis reads its proof, so a proof edit can change
the reported trust context. Each skeleton reports the axioms the declaration
rests on (a `sorry` shows as `sorryAx`) and, by name, the non-core constants it
assumes from Mathlib or another dependency, which are trusted and not expanded.

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
`structure` block. A field named in `lean:` keeps its own packet, whose
signature carries the field's type and whose source is only its name. A
`partial def` in a trusted closure is refused, and so is an
ordinary declaration with no source of its own, such as one a metaprogram adds
under an existing name; a theorem or axiom shows only its type, and an internal
helper such as `f._unary` is marked `-- source not shown`. A `lean:` name is
unresolved when it is absent from the sources or the built environment, Lean
declares it in another module than the one whose source holds it, the sources
define it more than once and the rules under
[Validation](validation-and-audit.md#validation) choose none, it
reaches a refused declaration, or its probe fails on its own (the reason quotes
Lean's output). That affects only its article: the others still extract, the
command exits nonzero, and nothing is written into the vault. Limits apply per
probe, so a declaration that resolves alone can be unresolved beside its
module's other targets; one that resolves has the same hash under any selection.
A missing `lake`, a failed freshness check or helper build, a dependency library
rooted at `Init`, `Lean`, or `Std`, or a blueprint or source tree that changes
during extraction stops the whole command.

### Report

`--output` records the `autoform-skeleton/v5` report, with no timestamp; only
failure reasons quote absolute paths. It names the target set (every target or a
`--node` selection) and the blueprint, by a hash of every article, every file in
[`links/`](links.md) and each cited passage; a report whose selection, articles
and unresolved declarations disagree cannot be built. Only a report on every
article, with the blueprint's `lean:` names as targets, is review evidence.

### Limits

The Lake freshness check, the helper's build, and each probe have 600 seconds
each; `--timeout SECONDS` sets the last two. At most eight probes run at once.
These limits are resource controls, not a security or authenticity boundary.

Each phase is named on stderr as it starts: `skeleton: probing <n> modules...`
when Lean probes start, `skeleton: assembling...` once none runs (the build must
still not change), and `skeleton: writing...` before `--output` or `--packets`.

### Packets and passages

`--packets DIR` writes one comment-stripped packet per skeleton, named after its
article and declaration, with a manifest of articles and hashes; a blind auditor
gets the hash-named packets of `review prepare` instead. The destination must be
empty or hold Autoform's packet manifest. Each run replaces the whole managed
tree, aborts if the tree changes before commit, and preserves an old tree that
changes later at a reported recovery path. Packet and passage manifests are v2;
v1 trees are recognized and replaced. An unresolved selected declaration blocks
all packet and passage output, though `--output` still records the report.
Destinations must be disjoint, not symlinks, and on filesystems with hard links
and atomic renames. Combined outputs commit together and a failed commit
restores the previous set, but a reader may briefly see mixed generations.

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

## Skeleton hashes

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

## Read-back cards

A read-back is a blind agent's mathematical-English account of one packet,
recorded with `review record`. Cards live at
`blueprint/readbacks/<article_id>/<encoded-declaration>.md` and hold the packet,
its evidence hash as `packet:`, the statement hash as `statement:` (a record,
never compared), an unauthenticated `model:` label the coordinator supplies, and
the testimony. Loading rejects missing or unknown fields, altered packets,
identity mismatches, non-canonical cards, and malformed or empty testimony.

Testimony is plain text with TeX formulas between dollar signs, following
`skills/human-review/references/readback.md`. It is refused for invisible or
reordering characters, combining marks stacked over the lines around or with
nothing to sit on, showing no letter or digit, or exceeding 32 KiB or 500 lines;
a write also refuses code points unassigned in its Python's Unicode data. A
formula never invalidates testimony: one `testimony.formula_problems` cannot
typeset is shown as typed, and render warns without changing its exit status.

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

## Statement review

`review prepare` extracts every Lean-mapped article from the built tree and
writes one blind packet per declaration under `blind/`, named by its hash, with
a manifest for the coordinator. Each such article must be a declaration-sized
leaf, or review commands refuse it with `review-article-shape`; module catalogs
are not reviewed. An `origin: cited` article must link an in-vault, non-Markdown
snapshot with an exact `#L<start>-L<end>` range, no article may cite blank
lines, and preparation refuses a citation it cannot show.

`review record` runs no Lean. It refuses a packet not matching its hash, a
non-regular packet or testimony file (a FIFO is refused unread), an `article_id`
the blueprint no longer has, and refused testimony, then files the card;
replacing a different card needs `--expected-card-hash`. It warns about each
formula a site would show as typed, with its exit status unchanged. A card from
a stale packet is invalid under `review check`, so prepare and record again.

`review check` derives its evidence from its own extraction, never a committed
file, and fails until every Lean-mapped article has current cards and a
`review_approved` equal to the review hash it prints. An approval also binds the
macros in `blueprint/tex-macros.json` its statement uses, directly or through
another macro, so a macro edit stales only those approvals. The run compares the
articles, cited sources, and cards it read before Lean with what it sees after,
and a difference fails with `review-snapshot-changed`; rerun once the blueprint
is idle. These are comparisons, not a lock: an edit restored between them goes
unseen, and `tex-macros.json` is read once and compared with nothing.

`render --review` makes the same check from its own extraction and puts a
*Review* disclosure under every formalized statement: the approval state, the
review hash, the cited passage, and for each declaration its card (current,
invalid with reasons, or missing) before the exact packet; the statement hash,
assumptions, and axioms are marked not bound. An edit during extraction fails
with `review-snapshot-changed`. A passage cut from a file the site leaves out,
such as a script, a file under `readbacks/`, or `atlas.json`, is refused with
`review-source-unpublished`. Articles in `published: false` folders are
reviewed but get no disclosure or warning, and module catalogs get none.
