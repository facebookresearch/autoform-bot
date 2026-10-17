---
name: setup
description: >-
  Set up, inspect, or repair repository infrastructure for an Autoform Lean
  project, including the Lean/Mathlib shell, an in-repository
  Obsidian-compatible blueprint vault, ignore rules, MkDocs, GitHub Pages, and
  verification CI, with optional Zulip community synchronization. Use for new
  repositories, environment repair, publication setup, infrastructure checks,
  or an explicitly requested Zulip project sync; do not choose mathematical
  scope or build the roadmap and theorem DAG.
---

# Set up an Autoform repository

Setup prepares the Lean toolchain, an empty blueprint vault, ignore rules,
MkDocs, CI, and optionally publication. It does not scope sources, choose
theorems, write roadmap nodes, or prove results; Roadmap owns that work.

Inspect before writing and preserve existing Lean, Markdown, workflow, and
ignore files. Resolve `AUTOFORM_PLUGIN_ROOT` from the loaded plugin and inspect
an existing project through the supported read-only entrypoint:

```bash
AUTOFORM_PLUGIN_ROOT="<loaded-plugin-root>"
PROJECT="<existing-project>"
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform project inspect "$PROJECT" --json
```

Infer safe local defaults from the request and repository. If a material choice
is missing, ask once for the run type (new, repair, or inspect), UpperCamelCase
package name, target directory, and whether publication is wanted. Without
explicit publication approval, make no remote changes. Setup prepares the shell
and stops before
mathematical planning.

Read the repo-shaped [Cabannes thesis project](assets/cabannes-thesis-project/README.md)
as a concrete setup example. Reuse its structure selectively: rename the Lean
package, keep the recommended release from `autoform project versions` unless
the user needs another Lean version, update branch and immutable workflow pins,
and merge rather than overwrite. Its populated
thesis notes illustrate later skills; Setup does not reproduce that mathematics.

For a new repository, require a target directory that does not already exist,
then create the complete local project atomically:

```bash
AUTOFORM_PLUGIN_ROOT="<loaded-plugin-root>"
TARGET="<absent-target>"
PACKAGE="<UpperCamelCaseName>"
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform project new "$TARGET" --package "$PACKAGE"
```

Without version flags `project new` uses the recommended release from
`autoform project versions`: a tested Lean/Mathlib pair whose resolved
`lake-manifest.json` is bundled. Pass `--release <RELEASE_ID>` for another
listed pair. If the user needs a different Lean version, pass
`--lean-toolchain <vX.Y.Z>`; `--mathlib-rev <REV>` overrides the default Mathlib
tag of the same name, and the toolchain must match the `lean-toolchain` of that
Mathlib revision. A pair equal to a listed release, by Mathlib tag or commit,
gets that release's bundled lock. Any other pair is created without
`lake-manifest.json` and with a warning: run `lake update` in it, which needs
network access and downloads the Mathlib build cache, then commit the manifest
it writes. Autoform needs Lean v4.27.0 or newer, and `project new` also warns
below that.
Every component of the target parent must be a real directory, not a symlink;
on macOS use `/private/tmp`, not the `/tmp` alias. The parent must not be group-
or world-writable unless it is a sticky directory owned by the user or root. If
`project new` reports `project-parent-unsafe`, choose another parent or, with the
user's agreement, remove that write access with `chmod g-w,o-w`.

Autoform's library catalog,
`$AUTOFORM_PLUGIN_ROOT/autoform_cli/project/libraries.json`, lists shared Lean
libraries that `project new --library <NAME>` adds, already required and locked.
Offer a library only when Setup creates a new project with `project new`, and
only when its entry has `offered: true`; never when inspecting, repairing, or
running `init` on an existing repository. Ask in the single round of questions,
and state the cost in the question: the project moves from the recommended
release to the library's `release`, its first build needs network access and
downloads the library and its dependencies (about 270 MB for ATLAS), and the
library modules the project imports are built locally. A yes may come as the
answer or in the setup request itself, such as "set up … with ATLAS"; a request
that names a catalog library uses it even when the entry is not offered, as long
as it names no other repository or revision for it. A request that does is a
library outside the catalog, below. Without a yes, add no library, including in
a headless run.

On a yes, run the `project new` command above with `--library <NAME>` and no
version flags; `<NAME>` is the entry's `name` exactly, such as `atlas`.
`--library` selects the library's release and writes its `[[require]]` and a
complete `lake-manifest.json`, so the project needs no `lake update`; it refuses
`--release`, `--lean-toolchain`, and `--mathlib-rev`. Validate as below; the
REPL require and `lake update repl` follow as for any project. Then run
`autoform library list` on the project, as the
[CLI reference](../../autoform_cli/README.md#searching-a-library-the-project-depends-on)
shows. When it lists the library as usable, report the entry's `git` and pinned
commit; otherwise report its reason and do not claim the library works. If
fetching fails, for example without network access or because the commit is
unreachable, report the error, say that the project builds once the fetch
succeeds, and give the retry command: resume the validation sequence from the
step that failed, starting with `lake update repl` when the REPL require was
added, then `lake exe cache get` and `lake build`. Never run a bare
`lake update` in such a project.

When the user says the project will depend on a Lean library outside the
catalog, create the project on that library's Lean and Mathlib: Lake builds a
dependency with the project's own Lean and Mathlib, not the versions the
library pins, so a project on another pair builds the library against the wrong
Mathlib. Read the library's `lean-toolchain` and the Mathlib `rev` its
`lake-manifest.json` locks, at the revision the user plans to require; a
read-only fetch of those two files is allowed. Pass both, as `--lean-toolchain`
and `--mathlib-rev`; `project new` then uses a listed release when the pair is
one. If either file cannot be read or the revision is unknown, ask the user
instead of guessing. Say which pair you chose and why in the setup report.
Adding the `require` stays the user's decision; do not add it or fetch the
library into the project.

`project new` writes the requested `lean-toolchain` and Mathlib revision (by
default the recommended, locked catalog pair), the Lean shell, and the complete
Autoform vault, site, ignore rules, and pinnable CI without running Lake, Lean,
or network operations; no later `init` is needed. It never overwrites an
existing target and fails closed on platforms without the required POSIX
filesystem operations, including Windows.
It pins generated workflows exactly as `init` does, described below, and omits
them when there is no commit to pin;
invoke `init` later through the same plugin-root launcher, passing the target
and `--autoform-ref <40-char-sha>`. Do not invent workflow sources or revisions,
and do not copy the populated example as a project generator.

For an incomplete existing repository, preserve its authored configuration and
run `autoform init` through the same `uv run --project "$AUTOFORM_PLUGIN_ROOT"`
prefix only for the Autoform vault/site repair overlay.

Neither `project new` nor `init` adds the Lean REPL that the REPL tool runs.
Declare `leanprover-community/repl` at an immutable revision: the commit of the
upstream tag that names the project's Lean version or, failing that, its
`vX.Y.0` release, as the bundled example pins the commit of tag `v4.32.0` for
v4.32.2. `git ls-remote --tags https://github.com/leanprover-community/repl.git`
lists the tags; if neither exists, ask the user instead of guessing.
Name the require `repl`, as that example does: the runtime runs
`lake exe @repl/repl`, which finds the package only under its require name.
Then run `lake update repl` so an existing manifest locks it without moving
Mathlib, commit the manifest, and verify `lake build @repl/repl`.

`autoform init` is the whole vault: `blueprint/` with its landing page,
`roadmap/README.md`, `coverage/`, and `sources/`, plus `mkdocs.yml`, the theme
override, both workflows, and ignore rules. Do not hand-build any of it and do
not copy the bundled example: the layout is fixed, and a chapter written as a
sibling file instead of `<chapter>/README.md` still validates while publishing
a book with no chapters. `init` preserves existing files, appending only missing
Autoform rules through a retained bounded regular root `.gitignore` file, so it
is also the repair path; it reports what it left alone. See the
[CLI reference](../../autoform_cli/README.md#commands) for its flags.

`init` pins the generated workflows to the Autoform commit that ran it, using a
safe remote only when a cached remote-tracking ref contains that commit. It
prefers Autoform's canonical repository, then `origin`, then `upstream`, then a
unique remaining source from the Autoform checkout it runs from or the
marketplace checkout an installed plugin was copied from. It infers that pin
only when tracked files are clean and the retained bounded, regular, link-free
required template snapshot and scaffold renderer match the pinned commit in
path, bytes, and executable-bit classification; an installed copy must also
match its marketplace checkout. All identity and tree reads ignore local Git
replacement objects. On any mismatch or when no remote has that local
containment evidence, `init` writes no CI rather than guess a source or ref:
guessing produced projects whose first push failed with nothing in the workflow
to explain why. When it reports that, find the commit the plugin was installed
from and pass
`--autoform-ref <40-char-sha>`, or say plainly that CI was not configured.
Never invent a ref. It must be a full 40-character commit sha: `init` refuses a
branch, a tag, or an abbreviated sha, because CI would silently reinstall a
different Autoform later and break a project that was passing.

The two workflows it writes are `autoform-verify.yml`, which validates the
Markdown DAG, builds Lean, rejects unfinished or unsafe proofs, and audits
theorem axioms on pull requests, and `blueprint-pages.yml`, which validates the
DAG and its `lean:` declarations, renders the blueprint, builds MkDocs, and
deploys GitHub Pages. Pass `--autoform-ref` to pin them at an immutable commit.
It also writes `.github/CODEOWNERS.autoform.example`. GitHub does not read it,
so it cannot mask active owners. Do not guess owners or activate it silently.
With names the user gives, merge its rules into the first CODEOWNERS file
GitHub reads, then require code-owner review, dismiss stale approvals, and
audit bypasses. Report that new rules cannot protect their own activation
unless equivalent coverage already exists on the pull request's base branch.

After it runs, fill in what only a human or a source can supply: the project
description in `blueprint/README.md`, the coverage contract, and a verified
`repo_url`. That URL is the *formalization project's own* repository, never
AutoformBot's: Material renders it as the repository link in the site header,
and pointing it at the plugin sends every reader to the wrong project. Pass
`--repository-url` to `autoform init`, or leave the key out until the remote
exists rather than guessing it. When a deployed site exists, feature its verified canonical URL in
the root `README.md`, never an inferred or pending one.

Adding workflow files is a local repository edit. Creating a remote, pushing,
or enabling Pages are separate outward-facing actions; perform them only when
the user requests them. Pin third-party Actions and the Autoform CLI source to
immutable commits.

Validate the prepared repository before reporting it ready. Build Lean first,
then run the publication sequence:

```bash
lake update          # only when the project has no lake-manifest.json
lake update repl     # after adding the REPL require when a manifest exists
lake exe cache get   # skip only when the project has no Mathlib dependency
lake build
lake build @repl/repl
```

Then validate, visualize, render, and strict-build the site, keeping
`--require-declarations` so a named Lean declaration that does not exist fails
here rather than in CI. The exact invocations, including how to resolve
`<AUTOFORM_PLUGIN_ROOT>`, are in the [CLI reference](../../autoform_cli/README.md#commands);
do not restate them here.

`render` writes a derived tree; the vault stays the source of truth. Ignore
`site-src/`, `site/`, `.autoform-publication-*/`, and `blueprint/dependencies.md`.

Publication is opt-in because files under `blueprint/` become public site
content, together with derived progress, graph pages, and a path-free
publication manifest. Show that boundary, confirm the exact repository and
visibility, default to private, and warn that private Pages may require a paid
GitHub plan. Rendering rejects symlinks and operational or sensitive files.
When approved, prepare the commit, remote, Pages source, and push; otherwise
leave the workflow inert.
If credentials, hosting, or repository settings block publication, report the
minimal owner action required.

Zulip synchronization is a separate opt-in outward-facing action. When the user
asks to discover community context or announce and coordinate the project, read
and follow [the shared Zulip workflow](references/zulip.md). Do not infer consent
to post from repository setup, roadmap work, or permission to search.

Report the Lean toolchain, vault path, CI and Pages files, validation results,
the publication decision, and any one-time GitHub setting the user must still
apply. State explicitly that no sources were scoped, roadmap nodes created, or
proofs started, then hand the repository to Roadmap.
