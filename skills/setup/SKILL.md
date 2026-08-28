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
ignore files. Use `scripts/workspace_inspector.py` when auditing an existing
Lean workspace. Infer safe local defaults from the request and repository. If a
material choice is missing, ask once for the run type (new, repair, or inspect),
UpperCamelCase package name, target directory, and whether publication is
wanted. Without explicit publication approval, make no remote changes. Setup
prepares the shell and stops before
mathematical planning.

Read the repo-shaped [Cabannes thesis project](assets/cabannes-thesis-project/README.md)
as a concrete setup example. Reuse its structure selectively: rename the Lean
package, check the current matching stable Lean/Mathlib release, update branch
and immutable workflow pins, and merge rather than overwrite. Its populated
thesis notes illustrate later skills; Setup does not reproduce that mathematics.

For a new repository, require a target directory that does not already exist and
bootstrap the Lean/Mathlib shell with the plugin's internal helper:

```bash
bash "<AUTOFORM_PLUGIN_ROOT>/scripts/make_project.sh" \
  <ProjectName> [target-dir]
```

For a new or incomplete repository:

- create or repair a buildable Lean project with matching `lean-toolchain` and
  Mathlib revisions; and
- write the blueprint vault, site configuration, and CI with `autoform init`.

`autoform init` is the whole vault: `blueprint/` with its landing page,
`roadmap/README.md`, `coverage/`, and `sources/`, plus `mkdocs.yml`, the theme
override, both workflows, and ignore rules. Do not hand-build any of it and do
not copy the bundled example: the layout is fixed, and a chapter written as a
sibling file instead of `<chapter>/README.md` still validates while publishing
a book with no chapters. `init` never overwrites an existing file, so it is
also the repair path; it reports what it left alone. See the
[CLI reference](../../autoform_cli/README.md#commands) for its flags.

`init` pins the generated workflows to the Autoform commit that ran it, but it
can only do that when Autoform is running from a Git checkout. Installed as a
plugin it is a plain directory copy, so there is nothing to read and `init`
writes no CI rather than guess a ref: guessing produced projects whose first
push failed with nothing in the workflow to explain why. When it reports that,
find the commit the plugin was installed from and pass
`--autoform-ref <40-char-sha>`, or say plainly that CI was not configured.
Never invent a ref. It must be a full 40-character commit sha: `init` refuses a
branch, a tag, or an abbreviated sha, because CI would silently reinstall a
different Autoform later and break a project that was passing.

The two workflows it writes are `autoform-verify.yml`, which validates the
Markdown DAG, builds Lean, rejects unfinished or unsafe proofs, and audits
theorem axioms on pull requests, and `blueprint-pages.yml`, which validates the
DAG and its `lean:` declarations, renders the blueprint, builds MkDocs, and
deploys GitHub Pages. Pass `--autoform-ref` to pin them at an immutable commit.

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
lake exe cache get   # skip only when the project has no Mathlib dependency
lake build
```

Then validate, visualize, render, and strict-build the site, keeping
`--require-declarations` so a named Lean declaration that does not exist fails
here rather than in CI. The exact invocations, including how to resolve
`<AUTOFORM_PLUGIN_ROOT>`, are in the [CLI reference](../../autoform_cli/README.md#commands);
do not restate them here.

`render` writes a derived tree; the vault stays the source of truth. Ignore
`site-src/`, `site/`, and `blueprint/dependencies.md`.

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
