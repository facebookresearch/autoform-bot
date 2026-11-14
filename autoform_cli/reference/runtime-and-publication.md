# Autoform runtime and publication

## Local runtime doctor

Run the runtime projection and roadmap audit together:

```bash
autoform doctor . --lean-root .
autoform doctor blueprint --json
```

The doctor reports six ordered checks: blueprint resolution, runtime schema,
graph counts, reference invariants, roadmap audit, and optional local Lean
targets. It exits zero only when every required check passes. Omitting
`--lean-root` records an explicit advisory pass; supplying it performs only a
lexical local-source check, not a Lean build, kernel check, or proof-honesty
review. The `lean targets` check holds every finding that needs the Lean root,
and an unusable Lean root or unreadable source fails only that check. The
bundled example intentionally exits nonzero while its declared coverage still
holds `MAPPED` rows. The doctor is read-only and local: it runs no Git, GitHub,
subprocess, network service, claim, renderer or dashboard, and writes no file.

## Runtime contract

`autoform_cli.runtime` projects the Markdown graph into the read-only, in-memory
schema `autoform-runtime/v6`; the Markdown stays the authority, and Autoform
never creates, synchronizes, or treats `graph.json` as an authority. A source
target that is neither an http(s) link nor a path inside the blueprint, and a
`mathlib_file` that is not a portable relative path, are left out of the
projection ([Validation](validation-and-audit.md#validation) says how `check`
reports them). Each v6 node carries `article_id`, `article_revision`, a nullable
`catalog`, `informal_dependencies`, `assertions.statement_retracted`, and
`status.assumes` and `status.waiting_on`, and the graph its `open_statements`
policy. Temporary claims may fall back to an article's path-derived `id`, but
durable execution records and routes must require `article_id` until the
path-move migration is complete. Operational state is excluded from runtime
snapshots and publication. Projection fails with
`the article changed after the graph was loaded` when an article's bytes no
longer match the digest recorded at load.

## Publication contract

`autoform render` publishes the book, derived progress, and the bounded project,
chapter, and nested-scope projections the book reads. Rendering never reads a
`graph.json` or an operational queue; [wiki references](../../docs/wiki-links.md) are also
derived. Hidden files and directories are omitted. A `.autoform` directory and
symlinks, credentials, logs, provider state, and agent/task state outside
left-out folders fail the render rather than leak. Source and output directories
must be disjoint. Render refuses a blueprint path that differs only in case from
a file it writes at a fixed path ([the site page](site.md#files-render-writes)
lists them), and two blueprint paths that differ only in case or Unicode form. A
blueprint file at exactly `dependencies.json`, `javascripts/blueprint-dag.js` or
`stylesheets/blueprint-dag.css`, a page MkDocs would publish where render writes
its own, or a file spelled as one render writes but in another case, is left out
of the site, with a warning unless it is the root `dependencies.md` or `structure.md`.

The search index and graph breadcrumbs may carry `reading_title` (`inline-parts-v1`):
captured title text and publisher-derived text, code and marked TeX parts. Reading,
breadcrumbs and search typeset supported parts with project macros; inventory math uses
bounded viewer-owned jobs. Unsupported or failed/stalled math stays literal.
A title with escapes and no math reads as its heading; relations and status are unchanged.

Every render writes `publication.json`, schema `autoform-publication/v3`, with
blueprint and Lean-source hashes (the latter empty when render reads no Lean
file), Git ref, article and dependency counts, the site's file hashes, and
available views (`project`, `chapter` and `full` name the old dependency pages).
`autoform` gives the rendering Autoform's `version`, `commit` and `source`, best
effort. `inventory` maps each blueprint path, a hidden, unpublished or top-level
`templates/` folder (in `links/` too) as one key, to a site URL (`published`), a
repository URL (`linked`) or a reason (`omitted`), over the verified commit's
files (`basis` `commit`) or those render read (`capture`). It holds no timestamp
or absolute path, so identical inputs and Autoform produce identical files.
Render reads the blueprint and the Lean sources once each; an edit saved during
a render waits for the next one. Under `--review`, an article or cited passage
edited during extraction fails the render instead
([review](../README.md#review)).

Repository links appear only when the Lean bytes match one locally available Git
commit, recorded by its full object ID; a clean checkout Git converts still
matches. Otherwise the render stays local, records no Git ref, and warns with
the first mismatch. Links into the blueprint (an article's Markdown source;
under `sources/`, a hidden note and any file but a note or an image) also need
the blueprint's bytes in that commit; when they differ, render keeps only the
code links and warns with the first mismatch, or, for a blueprint outside the
repository or in a nested checkout, does the same without a warning. When render
reads no Lean file, it records the Git ref only if the blueprint matches it.

Render replaces only an empty directory or a complete `autoform-publication/v2`
or v3 site whose files match its inventory (`.DS_Store` and `._*` are ignored),
and refuses anything else. A failed or refused render leaves the output as it
was. The site is built in a hidden `.autoform-publication-*` directory beside
the output (the repository root for `--output site-src`) and swapped in at once;
renders into one parent directory take turns when it can be locked. A build
directory a killed render leaves behind is covered by the setup ignore rules but
may hold the previous site, so inspect it before deleting it. On filesystems
without an atomic exchange the previous site is first renamed to
`.autoform-publication-backup-<name>`, which the next render moves back if the
swap died midway. On macOS `fsync` does not flush the drive's cache, so a power
loss can still lose the newest site. Native Windows commits through the backup
swap without a lock.

Check the site MkDocs builds from render's output for what the build broke:

```bash
autoform site check site --source site-src
```

`mkdocs build --strict` never checks a link written as raw HTML, as render
writes many. `site check` reports, as warnings, an internal link or asset whose
target the site lacks, a fragment that matches no `id` on its target page, a
link that is not a valid URL, with `--source` each statement card a source page
holds that its built page lacks, and with `--mathjax`, an unpacked MathJax
3.2.2, each article formula MathJax rejects and each command it does not know,
typeset in Node.js as on the site. Links with a scheme, a network location, a
path from the site root or a `name=value` fragment are not checked. It exits 1
only when the site or the source is not a directory.
