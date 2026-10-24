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
theorems, write roadmap nodes, or prove results; Roadmap owns that planning.

Inspect before writing and preserve existing Lean, Markdown, workflow, and
ignore files. Resolve `AUTOFORM_PLUGIN_ROOT` from the loaded plugin and inspect
an existing project read-only:

```bash
AUTOFORM_PLUGIN_ROOT="<loaded-plugin-root>"
PROJECT="<existing-project>"
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform project inspect "$PROJECT" --json
```

Infer safe local defaults from the request and repository. If a material choice
is missing, ask once for the run type (new, repair, or inspect), UpperCamelCase
package name, target directory, and whether publication is wanted. Without
explicit publication approval, make no remote changes.

The [Cabannes thesis project](assets/cabannes-thesis-project/README.md) shows a
finished project. Read it, but do not copy it: `project new` and `init` write
the layout, and its thesis notes illustrate later skills.

For a new repository, create the project in an absent target directory:

```bash
AUTOFORM_PLUGIN_ROOT="<loaded-plugin-root>"
TARGET="<absent-target>"
PACKAGE="<UpperCamelCaseName>"
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform project new "$TARGET" --package "$PACKAGE"
```

The [CLI reference](../../autoform_cli/README.md#commands) gives `project new`'s
version flags, what it writes, and its error codes. Keep the recommended
release from `autoform project versions` unless the user needs another Lean
version (at least v4.27.0). On macOS use `/private/tmp`, not the `/tmp` alias.
On `project-parent-unsafe`, choose another parent or, with the user's
agreement, run `chmod g-w,o-w` on it.

Autoform's library catalog,
`$AUTOFORM_PLUGIN_ROOT/autoform_cli/project/libraries.json`, lists shared Lean
libraries that `project new --library <NAME>` adds, required and locked. Offer
only entries with `offered: true`, and only for a new project (never inspect,
repair, or `init`). Ask in the single round of questions and state
the cost: the project moves to the library's `release`, its first build
downloads the library and its dependencies (about 270 MB for ATLAS), and the
modules it imports build locally. A yes may come in the request itself; a
request naming a catalog library uses it even when it is not offered, unless it
names another repository or revision, which makes it a library outside the
catalog. Without a yes, add no library, including in a headless run.

On a yes, pass `--library <NAME>` with no version flags, validate as below,
and run `autoform library list` on the project, as the
[CLI reference](../../autoform_cli/reference/search.md#searching-a-library-the-project-depends-on)
shows: report the entry's `git` and pinned commit if it lists the library as
usable, otherwise its reason, without claiming the library works. If a fetch
fails, report the error and that the project builds once a retry from the failed
validation step succeeds. Never run a bare `lake update` in such a project.

For a library outside the catalog, pass `--lean-toolchain` and `--mathlib-rev`
set to what the library's `lean-toolchain` and `lake-manifest.json` lock at the
revision the user plans to require; a read-only fetch of those two files is
allowed. Ask when either is unknown, and report the pair and why. Do not add the
`require` or fetch the library into the project; the user decides that.

`project new` writes the complete Autoform vault, site, ignore rules, and CI, so
no later `init` is needed. The rules ignore the derived `site/`, `site-src/`,
`.autoform-publication-*/`, and `blueprint/dependencies.md`. For an incomplete
existing repository, run `autoform init` through the same
`uv run --project "$AUTOFORM_PLUGIN_ROOT"` prefix: it writes the whole vault,
keeps existing files, and reports what it left alone. Do not hand-build any of
the vault. Without `--autoform-ref`, both write CI only when they can verify the
Autoform commit that ran them; otherwise find the commit the plugin was
installed from and run `init` on the target with `--autoform-ref <40-char-sha>`,
or report that CI was not configured. Never invent a ref or workflow source;
`init` refuses a branch, a tag, or an abbreviated sha.

Neither `project new` nor `init` adds the Lean REPL that the REPL tool runs.
Require `leanprover-community/repl` under the name `repl` at the commit of the
upstream tag naming the project's Lean version or, failing that, its `vX.Y.0`
release, as the bundled example pins the commit of tag `v4.32.0` for v4.32.2.
If neither tag exists in
`git ls-remote --tags https://github.com/leanprover-community/repl.git`, ask
the user instead of guessing. Then run `lake update repl` so an existing
manifest locks it without moving Mathlib, and commit the manifest.

For an existing project whose lakefile lacks Lake's artifact cache, offer to add
`enableArtifactCache = true` and `restoreAllArtifacts = true`; a `lakefile.lean`
takes them in its `package` declaration. Never enable the cache without
`restoreAllArtifacts`.

Put the project's notation in `blueprint/tex-macros.json` as MathJax
`tex.macros` definitions, never in articles.

The [CLI reference](../../autoform_cli/README.md#commands) says what the two
generated workflows check and when Pages deploys. `init` also writes
`.github/CODEOWNERS.autoform.example`, which GitHub does not read; the
[template](../../autoform_cli/templates/github/CODEOWNERS.autoform.example)
says how to activate it. Do not guess owners; activate it only with names the
user gives, never silently. Report that new rules cannot protect their own
activation unless equivalent coverage already exists on the pull request's base
branch.

After it runs, fill in what only a human or a source can supply: the project
description in `blueprint/README.md`, the coverage contract, and a verified
`repo_url`, the formalization project's own repository, never AutoformBot's.
Pass `--repository-url` to `autoform init` only once the remote exists. When a
deployed site exists, feature its verified canonical URL in the root
`README.md`, never an inferred or pending one.

Adding workflow files is a local repository edit. Creating a remote, pushing,
or enabling Pages are separate outward-facing actions; perform them only when
the user requests them. Pin third-party Actions and the Autoform CLI source to
immutable commits.

Validate the prepared repository before reporting it ready. Build Lean first:

```bash
lake update          # only when the project has no lake-manifest.json
lake update repl     # after adding the REPL require when a manifest exists
lake exe cache get   # skip only when the project has no Mathlib dependency
lake build
lake build @repl/repl
```

Then run the publication sequence in the
[CLI reference](../../autoform_cli/README.md#commands), keeping
`--require-declarations`.

Publication is opt-in: files under `blueprint/` become public site content,
with derived progress, graph pages, and a path-free publication manifest. Show
that boundary, confirm the exact repository and
visibility, default to private, and warn that private Pages may require a paid
GitHub plan. When approved, prepare the commit, remote, Pages source, and push;
otherwise leave the workflow inert. If credentials, hosting, or repository
settings block publication, report the minimal owner action required.

Zulip synchronization is a separate opt-in outward-facing action. When the user
asks to discover community context or announce and coordinate the project,
follow [the shared Zulip workflow](references/zulip.md). Do not infer consent
to post from repository setup, roadmap work, or permission to search.

Before reporting, follow [Debrief Orchestrator](../debrief-orchestrator/SKILL.md)
with `--skill setup`. Report the Lean toolchain, vault path, CI and Pages files,
validation results, the publication decision, and any one-time GitHub setting
the user must still apply. State that no sources were scoped, roadmap nodes
created, or proofs started, then hand the repository to Roadmap.
