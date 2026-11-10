# Project setup

What `autoform init` and `autoform project` write, refuse and report. The [CLI
reference](../README.md#init) gives the synopses.

## init

`init` appends the missing Autoform rules to an existing root `.gitignore`,
which must be a regular file with one link. Even with `--force`, `init` refuses
before writing anything when it finds an entry that differs from a path it
manages only in letter case or Unicode form (a `Blueprint/` Lean library,
`MkDocs.yml`), a managed directory it cannot list, or a root `Readme.md` or
`readme.md`. The vault is always `blueprint/`, so a Lean library named
`Blueprint` needs a different library name (its lakefile `lean_lib`, root module
and imports), not a lowercase rename.

`init --no-verify-workflow` replaces `.github/workflows/autoform-verify.yml`
with a stub, kept by `init --force`, that passes and checks nothing, so Pages
publishes unverified commits. A project whose `lean:` names all lie in its root
package can run the [audit after a build](open-statements.md) in its own CI.

### Workflow pins

Generated workflows pin `--autoform-ref` or, by default, the HEAD commit of the
Autoform checkout running the command (or of the marketplace checkout an
installed plugin was copied from), when a safe remote (Autoform's canonical
repository, then `origin`, then `upstream`, then a unique remaining one) has a
cached tracking ref containing it, tracked files are clean, and the templates
and any installed plugin copy match it; an explicit `--autoform-source` gets no
default ref. Without a ref the CI workflows are omitted, except that stub.

## project new

A failure or interrupt after staging can leave a hidden `.autoform-new-*`
directory in the parent, which the error mentions; inspect it before removing
it. Whatever the umask, the published tree has modes 0755 for directories and
executables, 0644 otherwise. Its `lakefile.toml` sets
`enableArtifactCache = true` and `restoreAllArtifacts = true`. Its vault keeps
facts in [annotations](links.md), unlike `init`'s. Package names cannot shadow
Mathlib libraries such as `Archive` or `Counterexamples`. Workflows are pinned
as [`init`](#workflow-pins) pins them. Apart from local Git reads, the command
runs no subprocess, Lake, Lean, or network operation. Its errors name a code:

- `project-parent-inaccessible` or `project-path-is-symlink`: the parent and
  its ancestors must be readable real directories, not links; on macOS, use
  `/private/tmp` rather than the `/tmp` symlink.
- `project-parent-unsafe`: the parent is group- or world-writable and not a
  sticky directory owned by you or root; run `chmod g-w,o-w` on it.
- `project-parent-busy`: another creation held the parent's lock for 30 seconds.
- `project-parent-changed`: the parent path stopped naming the locked
  directory; nothing was published, but a stage may remain in the original one.
- `project-create-commit-uncertain`: publication started but its outcome could
  not be confirmed; locate the project before retrying.

Without the POSIX calls it needs, as on Windows, creation fails closed.

### Toolchains

`--lean-toolchain` also accepts `v4.30.0-rc1` and `leanprover/lean4:v4.30.0`;
`--mathlib-rev` requires it, and neither combines with `--release`. A catalog
pair gets its bundled manifest. Any other pair gets no `lake-manifest.json` and
a `project-release-unlisted` warning: run `lake update` there (it needs network
access, locks Mathlib and downloads its build cache), then commit the manifest.
Use that Mathlib revision's `lean-toolchain`, or Lake may rewrite yours or fail
to build. A toolchain older than v4.27.0 gets a `project-lean-below-minimum`
warning. Any `--autoform-ref` gets `project-autoform-ref-unchecked`, as CI reads
`blueprint/links/` only from Autoform 3ea366ae on. Warnings go to stderr, or
with `--json` to `warnings`, where such a pair reports `"release": null`; the
exit status stays 0. Successful JSON uses schema `autoform-project-creation/v1`.

### Libraries

`--library NAME` names an entry of `autoform_cli/project/libraries.json`, which
pins a shared Lean library to a full commit on a listed release. The project is
created on that release with the library's `[[require]]`, and its manifest
already locks the library's packages, so it needs no `lake update`; the first
Lake command clones them, which needs network access. An unknown name is
`project-library-unknown`, and a package name that is one of the library's
module roots or packages is `project-name-invalid`. The JSON result adds
`library: {name, git, rev}`.

## project worktrees

`autoform project worktrees --json` runs Git and reads the claim board (`origin`
or `--repo`; else `board` is null) to list, as `autoform-project-worktrees/v1`,
each linked worktree but the main one and the target's with its `protected_by`
codes; none means removable, and an unknown code protects: `locked`, `in-use`,
`git-operation`, `contains-worktree`, `dirty` (hidden or unreadable status too),
`unintegrated` (the target's HEAD lacks a commit by ancestry and non-merge patch
ID, or only its own reflog or refs reach one), `recent` (index, HEAD reflog or
`.lake` itself changed in 25 minutes), `claimed` (a live lease naming this
hostname, which a Mac without a fixed HostName changes), `liveness-unknown` (an
uninspectable process of yours begun after it, an unreadable board or lease). A
report's removable entries get sizes; `freeable_bytes` (an upper bound) omits
hard-linked blocks. `--remove MANIFEST` rechecks each such entry (`changed` if
it moved or went, or the target's HEAD moved), runs `git worktree remove` (else
`git-refused`), and prints them with `removed` and measured `freed_bytes`.
