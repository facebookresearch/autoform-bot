# Autoform validation and audit

## Validation

`autoform check` rejects cycles between authored articles, missing targets,
escaping paths, symbolic links under `roadmap/`, self-dependencies,
missing or multiple H1 titles, unsupported frontmatter keys, assertion values
it does not recognize, and assertions the Lean cannot back: `proof: formalized`
without `statement: formalized`, or either without a `lean:` name, even beside
`mathlib: true`. With `--lean-root`, `check` also fails on a `lean:` name that
is absent from the sources or defined more than once in them (see below). Only
the article graph must be acyclic, so project, chapter, and scope maps can
relate two containers in both directions.

Hidden paths, those with a part starting with `.`, are no part of the roadmap:
under `roadmap/` they hold no articles, need no chapter page, and may be
symbolic links, such as an editor's lock file. A dependency on a hidden page,
or one that steps through a hidden folder and back out, fails naming the hidden
path, and so does an `.implementation-notes` folder under `roadmap/`. A
`## Sources` link to a local path must name an existing file inside the
blueprint that opens as written, with no hidden part, even one its `..` steps
leave again; a directory, an invalid path or URL, or a symbolic link loop
fails, and `audit` requires the same. Under `sources/`, a hidden note passes
`check` with a warning instead and `audit` still reports it: only a repository
link reaches it, so it must be committed. A `## Sources` link to a view
generated at the blueprint root, such as `dependencies.md` or `structure.md`,
fails too, and `audit` and render reject coverage evidence that names one.

These pass `check` with a warning:

- A `## Sources` link with another scheme or a network location; work and
  search leave it out, and `audit` checks fragments and fails such links.
- A `mathlib_file` that is not a portable relative path; search reports it as
  null.
- A byte-order mark at the start of an article that otherwise loads: frontmatter
  on its first line is read as text, so its keys are ignored. One that hides the
  article's H1 title fails `check`, as a missing title.
- A `declaration` on a container article, which nothing counts or schedules;
  `audit` reports it as `formalizable-container`.

`check` also warns, naming the file and the line, about markup a published page
could use to run script or pass for the site's own markup; each warning says
how to fix it. It reads every page the site publishes, as `render` publishes
it, the landing page, `coverage/README.md`, and source notes included. None of
it fails `check` or stops `render`, which publishes such a page, or such a
statement in its box, as typed (every character escaped in one preformatted
block under a note; a flagged landing page shows only its title and opening
sentence, as text) until the warning is fixed. It warns about raw HTML (a tag,
a character reference, an unclosed comment, or anything the site's Markdown
would pass through as HTML; a complete `<!-- ... -->` comment outside code is
left out of the page, and a reference a browser shows as typed, such as the
`&D;` in `R&D;`, is fine); a link, an image, or a link definition, used or not,
to anything but the site's own pages or an `http:`, `https:`, or `mailto:`
address, in any case, unless shown in code; an attribute list (the braces of a
code fence may only name its language, as in ```` ```{.lean} ````, and options
such as `title` or `linenums`); a code fence Autoform or the site reads as left
open where an article, a statement, its notes, or a stretch of a chapter ends;
and a list nested hundreds of levels deep, beyond what the site's converter can
read.

`check` also warns about a formula that defines a command, such as
`\newcommand`, `\def`, or `\DeclareMathOperator`, or uses one the site turns
off, such as `\label`: it shows an error in red and the rest of the page is
published as written. Put the project's notation in `blueprint/tex-macros.json`.

Lean indexing reads the whole Lean root or nothing. It stops with an error when
an entry it must read cannot be read safely: a `.lean` file that cannot be
read, a folder that cannot be listed or searched, a `.lean` entry that is a
symbolic link or a special file (editor lock links named `.#*.lean` are
skipped), or an unreadable `manifest.json` or `publication.json` below the
root, in any folder the walk enters, even one of unrelated files; other files
are never read. To clear a stop, fix the permissions, or move the entry out of
the Lean root or into a folder every command skips: `.direnv`, `.git`, `.lake`,
`.obsidian`, `.trash`, `.venv`, `build`, a folder below the root that holds a
`.git` entry, a skeleton or review packet output folder, or a folder below the
root that holds a complete `publication.json`, such as a rendered site or the
`mkdocs` copy of one. There is no command-line option to exclude other
folders. These names match in any letter case, except `build`, which must be
lowercase; the `.lean` suffix must match exactly, so `A.LEAN` is not indexed.
`render` without `--lean-root` uses the blueprint's parent folder as the Lean
root. A command that indexes during a `render` skips the site being written,
but one that indexes while `mkdocs build` runs can read the site's copies
before its `publication.json`, so run it again once the build is done. When
Lean inputs keep changing, indexing retries and then fails naming the changed
paths relative to the Lean root, for example `Lean sources kept changing while
they were indexed; changed: Proj/A.lean`. Skipped folders and files other than
`.lean` sources may change during a capture.

Within each file, up to an `#exit` line, the index records the declarations
written with `theorem`, `lemma`, `def`, `abbrev`, `instance`, `structure`,
`class`, `inductive`, `opaque`, `axiom`, `irreducible_def`, Batteries' `alias`,
and Batteries' wanted commands `theorem_wanted`, `proof_wanted`, `def_wanted`,
and `instance_wanted`, which state a result whose proof or construction is
deferred. The command may follow `@[...]` attribute lists and the modifiers
`public`, `private`, `protected`, `noncomputable`, `partial`, `unsafe`,
`scoped`, and `local`; a declaration behind any other modifier, such as `meta`
or `nonrec`, is not indexed, and neither is an unnamed `instance` or
`instance_wanted`. `_root_.X` is recorded as `X`. `alias b := a` records `b`,
and `alias ⟨mp, mpr⟩ := a_iff` each of its names that is not `_`; the `:=` must
be on the alias's line, and the target on that line or the next nonblank one.
A `structure` or `class` also records the fields it declares, indented under
it, after `where` (or `:=`), and each parent it names (`extends toA : A`), as
`Structure.field`, the projection Lean defines; a field that only gives a
default, `x := v` or `(x := v)`, counts only when the structure extends
nothing, since it may set an inherited field. A field is private when it or
its structure is, and protected when it is. The index reads no constructor,
recursor, unnamed parent's projection or inherited field; for such a name
directly under a project type, a declaration article's `check --lean-root`
error gives the type's `path:line`. `audit` takes a field as a one-line `def`
or theorem, elaborated under its structure's limits.

A name declared once has that declaration, even a private one. A name declared
more than once has the one declaration that is neither `private` nor a wanted
command (Batteries elaborates each into a private placeholder). With two or
more public declarations (a file no library builds counts too), or only private
and wanted ones, the name has none: `check --lean-root` fails with
`declaration defined more than once`, `render` warns (`--require-declarations`
exits 1), `audit` reports `lean-target-ambiguous`, `skeleton` leaves it
unresolved, and work and search report no `source_file`; each diagnostic lists
the `path:line` of every declaration of the name. `work impact` locates a
compiled constant in its own module's file. The rule sees only indexed
declarations: a public one behind an unindexed prefix such as `open X in`, or
written `«main»` where the article writes `main`, leaves a private one as the
name's only declaration. In a file that starts with `module`, a declaration
outside a `public section` and without `public` is private to Lean but public
to the index, so such a name may be reported as defined more than once where
Lean accepts it, or selected where Lean binds it nowhere outside its file.

## Audit contract

`autoform audit` reports structured findings at blueprint-relative paths. It
checks that no article leaves a code fence open, that formalizable articles are
declaration-sized leaves with statement text and an explicit dependency
section, that asserted Mathlib facts are internally consistent, and that cited
work resolves to local source material without escaping the blueprint.
Coverage files are checked for broken links and explicitly declared gaps. With
`--lean-root`, local declaration names and kinds are checked against the Lean
source index, along with the elaboration limits each target runs under, and
`node-too-large` reports a node whose resolved `lean:` declarations span at
least 200 lines and at least four times this project's median, each measured
from its first line to the line before the next declaration; the finding's
reason gives every measurement. `review_approved` is left to `review check`.

An alias has its target's kind, size, and elaboration limits when the index can
be sure what the target is (`alias_target` in `lean.py`); otherwise it matches
any intent and is measured by its own span and limits. Each name of
`alias ⟨mp, mpr⟩`, and any alias of one, is a theorem whatever its target is.

A wanted target matches the intent its command mirrors: `theorem_wanted` and
`proof_wanted` a `theorem`, `lemma`, `proposition`, or `corollary`,
`def_wanted` a `def` or `definition`, and `instance_wanted` an `instance`. A
theorem's wanted target can back `statement: formalized`, but no wanted target
backs a proved article: `lean-target-wanted` reports one named, directly or
through an alias, by an article that records `proof: formalized`, or by a
definition that records `statement: formalized`, since a stated definition
counts as proved. A placeholder carries no `sorry`, so no `sorry` check catches
one. The generated verify workflow's kernel-trust audit passes a `lean:` name
that resolves only to a root-package placeholder, or a public alias of one,
only for an article the assumption contract records as `stated`, `can_prove`,
`can_state`, `planned` or `not_ready`; any other state fails, including
`mathlib` and an unknown one. Any other `private` declaration is reported as
not a declaration of the Lean build.

### Warnings

A warning never fails the audit: `clean` and the exit status depend on findings
alone. The text output prints a `warning: <path>: <code>: <message>` line for
each, after the error lines. `--json` lists them after `findings` as
`warnings`, sorted by `article_path` and `code`. Each also carries a `message`
and `declarations`, one object per affected declaration and setting with its
`name`, `option`, `value`, `location` (the `set_option` as `path:line` relative
to the Lean root), and `scope` (`file`, `section`, `declaration`, or `proof`),
sorted by name. The catalog limit warnings under [Elaboration
limits](#elaboration-limits) fill `declarations`. Two warnings leave it empty:
`atlas-omission`, at `atlas.json`, names what loading ignored in that file, and
`decomposed-without-declaration`, at `coverage/README.md`, names a `DECOMPOSED`
row's links that reach no declaration leaf.

### Structure

Containment is inferred from nested `README.md` articles.
`missing-chapter-article` reports a directory directly under `roadmap/` that
holds articles but names no chapter, so its pages would attach to the roadmap
root. Deeper directories, such as `definitions/` and `theorems/` buckets, are
not checked. `overfull-container` reports a mathematical article with more than
24 direct children. A roadmap root whose direct children are all chapter pages
may hold 64; one root-level leaf returns it to 24. A `navigation: module` page
may hold 120.

### Elaboration limits

With `--lean-root`, the audit reads the `maxHeartbeats`,
`synthInstance.maxHeartbeats`, and `maxRecDepth` settings each resolved `lean:`
target is elaborated under, including a catalog's. For a declaration article,
`lean-target-unbounded-limit` reports a target with no heartbeat limit: a
`set_option maxHeartbeats 0` or `set_option synthInstance.maxHeartbeats 0` in
force at the declaration or inside its proof. `lean-target-unscoped-limit`
reports a target whose raised limit comes from a file- or section-wide
`set_option`. A raised limit put on the declaration itself with
`set_option ... in` is not reported. The pass is lexical: it follows
`namespace`, `mutual`, `end`, and `section` nesting, including
`noncomputable section` and `@[expose] public section`, and treats a more
deeply indented `set_option` as part of the proof above it.

A catalog's declarations get the same two checks as [warnings](#warnings), at
most one per catalog and code: `catalog-unbounded-limit` and
`catalog-unscoped-limit`. An alias is read as the declaration it reaches. The
message counts the affected declarations against all those the catalog's
resolved names reach, names the options, locates the first `set_option`, and
says to change existing code only on request and that a finite limit, or for
an unscoped one `set_option X N in` on the declarations that need it, must pass
a rebuild. A name that does not resolve is a `lean-target-not-found` or
`lean-target-ambiguous` finding and is not counted.

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
sentence ("Pending Mathlib PR 1234 ...") passes, and so does a marker written
without punctuation; both are left to review. A real statement that opens
`Unknown: ...` or `Pending: ...` is reported and needs rewording, and so does
one written in symbols alone, such as `⊥ ≠ ⊤.`. A reference-style link is read
without its definition when that sits in a later section. Coverage evidence is
held to the same placeholder rule.
