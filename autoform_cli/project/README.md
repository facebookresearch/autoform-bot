# Project inspection reference

This is the contract for `autoform project inspect` and
`autoform project versions`; the [CLI reference](../README.md#commands) gives
their synopsis and flags.

## Scope and resolver rules

`project inspect` reads the nearest enclosing project's decision files without
running Lake, Lean, Git, or the network. `supported` means the inspected Lean
toolchain, immutable Mathlib Git lock, and release loading layout identify a
bundled release. `unlisted` means those inputs are known but identify no
bundled release. `indeterminate` means Autoform cannot establish that
catalog-comparable identity, for example because a decision file is invalid,
unreadable, or required but missing, Mathlib is path-based, or a required
configuration is not evaluated.

When Lake can load the root manifest, a `.lake/package-overrides.json` entry for
Mathlib replaces the manifest's. An override selects Mathlib's source when
Mathlib is active; it does not make Mathlib active on its own. Lake resolves
every requirement, direct or transitive, from the root manifest, but the report
certifies Mathlib only when `lakefile.toml` requires it directly; otherwise the
selected entry, including an `inherited` one, is unused for this report, with a
`mathlib-manifest-unused` warning and an `indeterminate` answer. A require of
the root package's own name is satisfied by the root, so it is never looked up
in the manifest or overrides. `lakefile.lean` takes precedence, as in Lake, but
is never evaluated, so its projects stay `indeterminate`. The decision files are
size-bounded and read as one snapshot; inspection retries or fails if their
bytes, identities, presence, or case aliases change. Catalog matching treats
elan's stable Lean release aliases, URI scheme and host case, and Mathlib's
explicit `./` root subdirectory as equivalent while preserving the authored
values in the report. A project that `project new` wrote without
`lake-manifest.json` is `indeterminate`, with `missing-lake-manifest`, until
`lake update` writes one; then it is `unlisted`, or `supported` when Lake locks
a catalog commit.

This predicts an ordinary Lake invocation. CLI `--packages` / `--file`
overrides, `LAKE_PKG_URL_MAP`, and edits inside an already-materialized checkout
are outside the offline report.

`supported` certifies only the inspected Lean/Mathlib release identity, not
the full Lake configuration or build validity; target and package options
outside the report are left to Lake. `ok` means inspection produced no error
diagnostic, not that compatibility was decidable: a missing or legacy manifest
can be a warning with `ok: true` and an `indeterminate` result, while an
invalid, unreadable, or oversized decision file that Lake and the inspection
need to consult is an error with `ok: false`.

## Exit codes, JSON, and diagnostics

`autoform project inspect` exits 0 when the report has no error diagnostic
(`ok: true`) and 1 when it has any (`ok: false`) or the bundled release catalog
cannot be loaded; with `--json` the latter prints
`{"error": {"code": "project-catalog-invalid", "message": ...}, "ok": false}`
instead of a report. A command-line usage error exits 2.

`--json` prints one object with sorted keys and these fields:

| Field | Value |
|---|---|
| `schema` | `"autoform-project-inspection/v1"` |
| `ok` | `true` when no diagnostic has severity `error` |
| `project_root` | path from the target to the nearest enclosing project, such as `"."` or `"../.."`; `null` when no project was found |
| `lean_toolchain` | the trimmed first line of `lean-toolchain`, or `null` when it is missing or invalid |
| `lake` | `null` when the Lake configuration is missing, unreadable or invalid; otherwise `config` (`"lakefile.toml"` or `"lakefile.lean"`), the package `name` and `version` (both `null` for `lakefile.lean`, which is not evaluated), and `targets`, a list of `{kind, name}` for each `lean_lib` and `lean_exe` |
| `mathlib` | the Mathlib entry the manifest or overrides select; `null` when there is none, when either file cannot be decoded, or when Autoform cannot establish that it is active (`lakefile.lean`, or a `mathlib-manifest-unused` warning). It is still reported when another error makes the status `indeterminate`; see below |
| `compatibility` | `status` (`supported`, `unlisted` or `indeterminate`, defined under [Scope and resolver rules](#scope-and-resolver-rules)), `release` (the matching catalog id, or `null`), and `recommended_release` (the catalog's recommended id) |
| `autoform_paths` | which of `blueprint`, `mkdocs.yml`, `.github/workflows/autoform-verify.yml` and `.github/workflows/blueprint-pages.yml` exist under the project root, with that exact spelling |
| `diagnostics` | a list of `{severity, code, message, path}`, sorted by severity (`error` first), then code, then path; `path` is the file concerned, relative to the project root, or `null` |

The `mathlib` object has `source` (`"lake-manifest.json"` or
`".lake/package-overrides.json"`, whichever Lake takes the entry from),
`type` (`"git"` or `"path"`), `inherited`, and the entry's `url` (with any
embedded credentials, and everything after the first `?` or `#`, replaced by
`***`; when a `?` or `#` comes before the `@` that ends the credentials,
everything after `scheme://` is replaced), `input_rev`, `rev`, `dir` and
`sub_dir`, each `null` when the entry does not have it, and `config_file`
and `manifest_file`, which default to `"lakefile"` and
`"lake-manifest.json"` when the entry has no value or `null`, as Lake reads
them.

`input_rev` is the requested revision, such as a tag or branch (the
manifest's `inputRev`), and `rev` is the commit Lake locked. The release
catalog that `project versions --json` prints calls the same two values
`mathlib_rev` and `mathlib_commit`; each v1 schema keeps its own names.

Errors mean Lake would fail on the project, a file Autoform needs cannot be
read, or Autoform's bounded parser cannot safely decode the input, which can
include a Lake-valid file. Any error makes the status `indeterminate`. Warnings
mean Lake would work but Autoform cannot decide, or wants to point something
out.

| Code | Severity | Meaning |
|---|---|---|
| `target-unreadable` | error | The target path cannot be resolved. |
| `project-not-found` | error | No directory at or above the target has a `lakefile.lean`, `lakefile.toml` or `lean-toolchain`. |
| `project-changed-during-inspection` | error | The decision files kept changing across every retry, so no single snapshot was read. |
| `unreadable-file` | error | A decision file is not a regular, readable UTF-8 file of at most 1 MiB. Symlinked decision files and a symlinked `.lake` are followed, as Lake follows them; a decision-file link that dangles, loops, exceeds the kernel's expanded-path buffer, or ends on anything other than a regular file is unreadable. A present non-directory `.lake` makes its override path unreadable, not absent. |
| `missing-lake-config` | error | There is neither `lakefile.toml` nor `lakefile.lean`. |
| `invalid-lakefile-toml` | error | `lakefile.toml` is not TOML that Autoform can decode safely, including a Lake-valid file beyond the bounded parser's resource limits, or Lake would refuse a field that the report reads (a bad name or version, a malformed require, or a target entry or name Lake rejects). |
| `missing-lean-toolchain` | error | There is no `lean-toolchain`. |
| `invalid-lean-toolchain` | error | The trimmed first line of `lean-toolchain` is empty or contains whitespace or a non-printable character. |
| `invalid-lake-manifest` | error | `lake-manifest.json` is not JSON Lake decodes, or has an unknown version. |
| `invalid-package-overrides` | error | `.lake/package-overrides.json` is not JSON Lake decodes, or has an unknown version. |
| `lake-manifest-incomplete` | error | A requirement of `lakefile.toml` is recorded by neither the manifest nor the overrides, or there are requirements and they record no packages; Lake asks for `lake update`. |
| `missing-lake-manifest` | warning | There is no `lake-manifest.json`, so the locked Mathlib is unknown; Lake would run an update. |
| `unsupported-lake-manifest` | warning | `lake-manifest.json` uses a legacy layout (before version 0.7) that Lake still reads but Autoform does not decode. |
| `unsupported-package-overrides` | warning | `.lake/package-overrides.json` uses a legacy layout (before version 0.7) that Lake still reads but Autoform does not decode. |
| `lakefile-lean-not-evaluated` | warning | `lakefile.lean` takes precedence and is never evaluated, so its package and Mathlib are unknown. |
| `mathlib-overridden` | warning | `.lake/package-overrides.json` selects the reported Mathlib entry whenever Mathlib is active. |
| `lake-manifest-stale` | warning | `lakefile.toml` asks for a different Mathlib than the manifest locks; Lake builds the locked one. |
| `mathlib-manifest-unused` | warning | Autoform cannot establish that the selected Mathlib is active: `lakefile.toml` does not directly require it, an inherited entry may be stale because dependency lakefiles are not inspected, or the root package is itself named `mathlib` and Lake reuses it. |
| `release-indeterminate` | warning | With no error, the toolchain or the Mathlib Lake builds is unknown or path-based. |
| `release-unlisted` | warning | The toolchain and Mathlib are known but are not a bundled catalog pair, or Mathlib is not loaded the way releases load it (another `subDir`, `configFile` or `manifestFile`). |
