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
including `node-too-large`, and an unusable Lean root or unreadable source fails
only that check. The bundled example intentionally exits nonzero while its
declared coverage still holds `MAPPED` rows. The doctor is read-only and local:
it does not invoke Git, GitHub, subprocesses, network services, claims,
renderers, or dashboards, and it writes no file.

## Runtime contract

`autoform_cli.runtime` projects the Markdown graph into the read-only,
in-memory schema `autoform-runtime/v4`; the Markdown stays the authority, and
Autoform never creates, synchronizes, or treats `graph.json` as an authority. A
source target that is neither an http(s) link nor a path inside the blueprint,
and a `mathlib_file` that is not a portable relative path, are left out of the
projection ([Validation](validation-and-audit.md#validation) says how `check`
reports them). Each v4 node carries `article_id`, a nullable `catalog`,
`assertions.statement_retracted`, and `status.assumes` and `status.waiting_on`,
and the graph its `open_statements` policy. Temporary claims and local
dashboard hooks may fall back to an article's path-derived `id`, but durable
execution records and routes must require `article_id` until the path-move
migration is complete. Operational state is excluded from runtime snapshots and
publication. Projection fails with `the article changed after the graph was
loaded` when an article's bytes no longer match the digest recorded at load.
The `runtime` module docstring describes the projection for Python consumers.

## Publication contract

`autoform render` publishes the book, derived progress, and one dependency
explorer as bounded project, chapter, and nested-scope projections. Rendering
never reads a `graph.json` or an operational queue; [wiki references](wiki-links.md) are also derived. Hidden files and
directories, including `.implementation-notes`, are omitted; the vault's
implementation notes are read in place only to validate them. A `.autoform`
directory and symlinks, credentials, logs, provider state, and agent/task state
outside hidden directories fail the render rather than leak. Source and output
directories must be disjoint. Render refuses a blueprint path that differs only
in case from one it writes (`SUMMARY.md`, `dependencies.json`,
`dependencies.md`, `structure.md`, `publication.json`, `assets/`,
`dependencies/`, `javascripts/`, `stylesheets/`, and the generated stylesheet,
scripts and logo inside them), and two blueprint paths that differ only in case
or Unicode form. A blueprint file at exactly `dependencies.json` or
`javascripts/blueprint-dag.js` is left out of the site with a warning.

The search index and graph breadcrumbs may carry `reading_title` (`inline-parts-v1`):
captured title text and publisher-derived text, code and marked TeX parts.
Only reading controls and breadcrumbs typeset these parts with the project's macros; map,
inventory and search labels retain the title as typed. Unsupported labels
stay literal. The original title, relations and status are unchanged.

Every render writes `publication.json`, schema `autoform-publication/v3`, with
blueprint and Lean-source hashes (the Lean-source hash is empty when render
reads no Lean sources), Git ref, article and dependency counts, the site's file
inventory, and available views. It holds no timestamp or absolute path, so
identical inputs produce identical output files. The dashboard accepts only a
v3 site and reports an older one as stale until render replaces it. Render
reads the blueprint and the Lean sources once each; an edit saved during a
render waits for the next one, and until then the dashboard reports the built
site as stale. Under `--review`, an article or cited passage edited between the
capture and the end of the extraction fails the render instead (see `render
--review` under statement review).

Repository links appear only when the Lean bytes match one locally available
Git commit, recorded by its full object ID; a clean checkout that Git converts
on checkout still matches. Otherwise the render stays local, keeps source
notes in the site, records no Git ref, and warns with the first mismatch.
Links into the blueprint (an article's Markdown source, a source note) also
need the blueprint's bytes in that commit; when they differ, render keeps the
code links, keeps source notes in the site, and warns with the first mismatch,
or, for a blueprint outside the repository or in a nested checkout, does the
same without a warning. When render reads no Lean file (native
Windows, or a project with no `.lean` file yet), it records the Git ref only if
the blueprint matches it.

Render replaces only an empty directory or a complete `autoform-publication/v2`
or v3 site whose files match its inventory (`.DS_Store` and `._*` are ignored),
and refuses anything else, including a legacy v1 output, which you remove once
by hand. A failed or refused render leaves the output as it was. The site is
built in a hidden `.autoform-publication-*` directory beside the output (the
repository root for `--output site-src`) and swapped in at once; renders into
one parent directory take turns when it can be locked. A build directory a
killed render leaves behind is covered by the setup ignore rules but may hold
the previous site, so inspect it before deleting it. On filesystems without an
atomic exchange the previous site is first renamed to
`.autoform-publication-backup-<name>`, which the next render moves back if the
swap died midway. On macOS `fsync` does not flush the drive's cache, so a power
loss can still lose the newest site. Native Windows reads the blueprint and the
output as two pathname captures that must agree, commits through the backup
swap without a lock, and reads the Lean sources only for an explicit
`--lean-root`, which then fails closed. Without one it publishes no code
links; when a published statement names a `lean:` declaration it warns, and
`--require-declarations` makes it exit 1.

Check the site MkDocs builds from render's output for what the build broke:

```bash
autoform site check site --source site-src
```

`mkdocs build --strict` never checks a link written as raw HTML, as render
writes card references, the structure page and the landing page's Next up card.
`site check` reports, as warnings, an internal link or asset whose target the
site does not contain, a fragment that matches no `id` on its target page, a
link that is not a valid URL, and, with `--source`, each statement card that
render wrote into a source page but the page built from it lacks. A link with a
scheme, a network location, or a path from the site root is not checked, and
neither is a fragment of `name=value` pairs, such as the explorer's `#node=`.
The exit status is 0 whatever it finds; it is 1 only when the site or the
source is not a directory.
