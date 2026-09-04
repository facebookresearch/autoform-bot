---
name: setup
description: >-
  Set up, inspect, or repair repository infrastructure for an Autoform Lean
  project, including the Lean/Mathlib shell, an in-repository
  Obsidian-compatible blueprint vault, ignore rules, MkDocs, GitHub Pages, and
  verification CI, with guidance for separately authorized Zulip coordination.
  Use for new repositories, environment repair, publication setup,
  infrastructure checks, or an explicitly requested Zulip project sync; do not choose mathematical
  scope or build the roadmap and theorem DAG.
---

# Set up an Autoform repository

Setup prepares the Lean toolchain, one or more empty blueprint vaults, ignore
rules, MkDocs, CI, and optionally publication. It does not scope sources, choose
theorems, write roadmap nodes, or prove results; Roadmap owns that work.

Inspect existing Lean, Lake, Markdown, workflow, and ignore files before
writing, and preserve them. Start with the offline, read-only
`autoform project inspect <TARGET>` command. If `.autoform.toml` exists, also
run `autoform workspace inspect <TARGET>` and treat its project registry as
authoritative; never infer managed vaults by scanning sibling directories.
When a blueprint exists, also use `autoform doctor` for its runtime contract,
passing `--project` when a workspace root contains several projects. Infer safe
local defaults from the request and repository. If a
material choice is missing, ask once for the run type (new, repair, or inspect),
UpperCamelCase package name, target directory, and whether publication is
wanted. Without explicit publication approval, make no remote changes. Setup
prepares the shell and stops before
mathematical planning.

Resolve the loaded `<AUTOFORM_PLUGIN_ROOT>` once and invoke every Autoform
command through that root as the CLI reference specifies. Do not rely on an
unrelated package or command already present on the consumer's `PATH`.

Read the repo-shaped [Cabannes thesis project](assets/cabannes-thesis-project/README.md)
as a concrete setup example. Reuse its structure selectively: rename the Lean
package, check the current matching stable Lean/Mathlib release, update branch
and immutable workflow pins, and merge rather than overwrite. Its populated
thesis notes illustrate later skills; Setup does not reproduce that mathematics.

For a new repository, use either an absent target directory or the literal
target `.` from an empty current directory. Before writing it, run
`autoform project provenance --json` through the loaded `<AUTOFORM_PLUGIN_ROOT>`. This
online read verifies the exact plugin-root Git checkout or its Codex installer
record against the recorded remote commit; a plain wheel cannot infer provenance.
Stop before creating the target if this check fails. Read `autoform
project versions --json`, select its single
recommended release for this setup
scenario, and pass both verified provenance values into the offline creation
command:

```bash
autoform project new <TARGET> --package <UpperCamelCaseName> \
  --release <RECOMMENDED_RELEASE_ID> \
  --autoform-source <VERIFIED_HTTPS_GIT_SOURCE> \
  --autoform-ref <VERIFIED_40_CHAR_SHA>
```

Use `.` for `<TARGET>` only when creating in the empty current directory; the
command form is `autoform project new .` with the same explicit package,
release, source, and ref flags shown above.

The catalog is a bundled known-good allowlist, not an automatic selection
mechanism. `project new` writes matching `lean-toolchain` and Mathlib revisions,
the Lean shell, and the Autoform vault without running Git, Lake, Lean, or
network operations; it never overwrites an existing entry. The `.` form
preserves the current directory inode and mode and fails closed when an
interrupted transaction cannot prove ownership. Do not invent version pairs or
copy the populated example as a project generator.

For an existing repository, prefer a manifest-managed workspace when the
repository contains several formalization efforts, already uses `blueprint` or
`Blueprint` for unrelated material, or needs repository-defined placement.
Create `.autoform.toml` with `autoform workspace init`, supplying an explicit
repository-relative blueprint collection. Then use `autoform blueprint new`
for each approved project. The command creates one child vault and appends one
entry to the root registry; it does not write `autoform.toml` inside the vault.
Use `autoform blueprint register` to adopt an existing vault without changing
any file inside it.
Location names and paths belong to the consumer repository. Autoform understands
the generic `blueprints` capability and must not special-case package or
directory names from its examples.
Workspace mutation requires the CLI's file-locking and no-follow safety support.
If the command reports that the platform is unsupported, stop and report the
blocker rather than hand-writing around the safety gate.

Inspect a workspace before and after creation. `autoform workspace check`
validates exactly the registered projects and ignores unregistered siblings.
Single-vault commands accept the workspace root plus `--project`, infer the
sole project at the workspace root, or infer the containing project when
invoked from inside its registered vault. They never infer a project from an
unrelated directory. Keep `autoform init` only
for backwards-compatible repositories that intentionally want one lowercase
`blueprint/` at the repository root.

If inspection reports a structurally valid stable patch pair as `unlisted`, do
not silently upgrade the consumer or patch its installed Autoform cache. Report
that repair is blocked. When the user elects to extend Autoform, validate the
exact pair and add it through a reviewed plugin change; an older stable patch
may be supported without replacing the catalog's recommended release.

For an incomplete legacy single-vault repository, inspect first, preview the
conservative repair, then apply it only when the plan contains solely the
intended missing Autoform files:

```bash
autoform project inspect <TARGET>
autoform project repair <TARGET> --dry-run --json
autoform project repair <TARGET>
```

`project repair` requires the explicit project root and a clean supported
Lean/Mathlib configuration. It preserves every existing managed file
byte-for-byte, adds only unambiguous missing Autoform overlay files, and stops
with zero writes when preflight finds a conflict. Supply exact title,
repository URL, or immutable workflow provenance only when the CLI reports
that a missing parameterized file needs it; an explicitly empty repository URL
is different from an omitted value. Never infer these values. Reuse the same
inputs after an interrupted multi-file repair, and inspect any reported stale
temporary or retained published file before removing it. Never substitute
`init --force` for repair.

`project repair` deliberately refuses a manifest-managed workspace: its legacy
overlay would otherwise create an unrelated lowercase `blueprint/`. Repair
workspace registration with the workspace commands and inspect shared CI or
publication files separately until a workspace-aware publication repair
contract exists.

Legacy `autoform init` is the whole vault: `blueprint/` with its landing page,
`roadmap/README.md`, `coverage/`, and `sources/`, plus `mkdocs.yml`, the theme
override, ignore rules, and both workflows when an immutable Autoform pin is
available. Do not hand-build any of it and do not copy the bundled example: the
layout is fixed, and `autoform check` rejects a chapter directory whose chapter
was written as a sibling file instead of `<chapter>/README.md`. Use `project
repair` for an incomplete existing project; `init` is not a repair command. See the
[CLI reference](../../autoform_cli/README.md#commands) for its flags.

`init` pins generated workflows to an explicitly supplied verified source and
commit, or discovers the same pair from a verified exact-root checkout or Codex
installer record. If neither source passes verification, `init` writes no CI
rather than guess. Pass `--autoform-source <credential-free-https-git-url>` and
`--autoform-ref <40-char-sha>` together; one without the other is an error.
Never invent either value. `init` refuses a branch, tag, abbreviated SHA,
credential-bearing URL, or mismatched pair.

The two workflows it writes are `autoform-verify.yml`, which validates the
Markdown DAG, builds Lean, binds local declaration claims to root-package
artifacts and Mathlib claims to the manifest-pinned commit of a clean canonical
upstream Mathlib checkout plus its package trace, rejects unfinished or unsafe
proofs, and audits theorem axioms on pull requests, and `blueprint-pages.yml`,
which runs the same artifact gate before it renders the blueprint, builds
MkDocs, and deploys GitHub Pages. Pass the verified source and commit pair to
pin them.

After creating a vault, fill in what only a human or a source can supply: the
project description in its `README.md`, its coverage contract, and a verified
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

Then validate, visualize, render, and strict-build the selected vault, keeping
`--require-declarations` so a named Lean declaration that does not exist fails
here rather than in CI. The exact invocations, including how to resolve
`<AUTOFORM_PLUGIN_ROOT>`, are in the [CLI reference](../../autoform_cli/README.md#commands);
do not restate them here.

`render` writes a derived tree; the vault stays the source of truth. Ignore
`site-src/`, `site/`, and the selected vault's generated `dependencies.md`.

Publication is opt-in because files under the selected vault become public site
content, together with derived progress, graph pages, and a path-free
publication manifest. Show that boundary, confirm the exact repository and
visibility, default to private, and warn that private Pages may require a paid
GitHub plan. Rendering rejects symlinks and operational or sensitive files.
When approved, prepare the commit, remote, Pages source, and push; otherwise
leave the workflow inert.
If credentials, hosting, or repository settings block publication, report the
minimal owner action required.

Zulip synchronization is a separate opt-in outward-facing action and requires
host-provided authenticated tooling; Autoform does not ship a Zulip client. When
the user asks to discover community context or announce and coordinate the
project, read and follow [the shared Zulip workflow](references/zulip.md). Do not infer consent
to post from repository setup, roadmap work, or permission to search.

Report the Lean toolchain, workspace manifest when present, exact vault path,
CI and Pages files, validation results,
the publication decision, and any one-time GitHub setting the user must still
apply. State explicitly that no sources were scoped, roadmap nodes created, or
proofs started, then hand the repository to Roadmap.
