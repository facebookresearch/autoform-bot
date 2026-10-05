# Project inspection reference

This is the detailed contract for `autoform project inspect` and
`autoform project versions`. See the [CLI reference](../README.md#commands) for
the command synopsis and flags.

## Scope and resolver rules

`project inspect` reads the nearest enclosing project's decision files without
running Lake, Lean, Git, or the network. `supported` means the inspected Lean
toolchain, immutable Mathlib Git lock, and release loading layout identify a
bundled release. `unlisted` means those inputs are known but identify no
bundled release. `indeterminate` means Autoform cannot establish that
catalog-comparable identity, for example because a decision file is invalid,
unreadable, or required but missing, Mathlib is path-based, or a required
configuration is not evaluated. When Lake can load the root manifest, a
`.lake/package-overrides.json` entry for Mathlib replaces the manifest's, and
a `lakefile.toml` that requests a different Mathlib than the lock gets a
`lake-manifest-stale` warning. Lake resolves every requirement, direct or
transitive, from the root manifest, but the report certifies Mathlib only when
`lakefile.toml` requires it directly. An override selects Mathlib's source when
Mathlib is active; it does not make Mathlib active on its own. Without a direct
Mathlib requirement, the selected entry is unused for this report, with a
`mathlib-manifest-unused` warning and an `indeterminate` answer. This includes
an `inherited` entry: it records prior resolver state and can remain stale after
a dependency stops requiring Mathlib. Autoform does not inspect dependency
lakefiles to prove otherwise. A root package named `mathlib` is likewise reused
instead of the manifest entry.
A require that neither the manifest nor the overrides record, or any require
when they record no packages, is a `lake-manifest-incomplete` error, because
Lake then refuses to build until `lake update`; a require of the root
package's own name is satisfied by the root and is not looked up.
`lakefile.lean` takes
precedence, as in Lake, but is never evaluated, so its projects stay
`indeterminate`. The decision files are size-bounded and read twice as one
snapshot; inspection retries or fails if their bytes, identities, presence, or
case aliases change. As in elan, only the trimmed first line of
`lean-toolchain` counts.
Catalog matching treats elan's stable Lean release aliases, URI scheme and host
case, and Mathlib's explicit `./` root subdirectory as equivalent while
preserving the authored values in the report.

This predicts an ordinary Lake invocation. CLI `--packages` / `--file`
overrides, `LAKE_PKG_URL_MAP`, and edits inside an already-materialized checkout
are outside the offline report.

The sections below define the report's exit codes, JSON fields, and diagnostic
codes.

`supported` certifies only the inspected Lean/Mathlib release identity; it is
not a full Lake configuration or build-validity check. `ok` means inspection
produced no error diagnostic; it does not mean compatibility was decidable.
A missing or legacy manifest can therefore be a warning with `ok: true` and an
`indeterminate` compatibility result, while an invalid, unreadable, or
oversized decision file that Lake and the inspection need to consult is an
error with `ok: false`. Target and package options outside the report are left
to Lake.

## Exit codes, JSON, and diagnostics

`autoform project inspect` exits 0 when the report has no error diagnostic
(`ok: true`) and 1 when it has at least one (`ok: false`). It also exits 1
when the bundled release catalog cannot be loaded; with `--json` it then
prints `{"error": {"code": "project-catalog-invalid", "message": ...},
"ok": false}` instead of a report. A command-line usage error exits 2.

`--json` prints one object with sorted keys and these fields:

| Field | Value |
|---|---|
| `schema` | `"autoform-project-inspection/v1"` |
| `ok` | `true` when no diagnostic has severity `error` |
| `project_root` | path from the target to the nearest enclosing project, such as `"."` or `"../.."`; `null` when no project was found |
| `lean_toolchain` | the trimmed first line of `lean-toolchain`, or `null` when it is missing or invalid |
| `lake` | `null` when the Lake configuration is missing, unreadable or invalid; otherwise `config` (`"lakefile.toml"` or `"lakefile.lean"`), the package `name` and `version` (both `null` for `lakefile.lean`, which is not evaluated), and `targets`, a list of `{kind, name}` for each `lean_lib` and `lean_exe` |
| `mathlib` | the Mathlib entry the manifest or overrides select; `null` when there is none, when either file cannot be decoded, or when Autoform cannot establish that it is active (`lakefile.lean`, or a `mathlib-manifest-unused` warning). It is still reported when another error makes the status `indeterminate`; see below |
| `compatibility` | `status` (`supported`, `unlisted` or `indeterminate`, defined under [Commands](../README.md#commands)), `release` (the matching catalog id, or `null`), and `recommended_release` (the catalog's recommended id) |
| `autoform_paths` | which of `blueprint`, `mkdocs.yml`, `.github/workflows/autoform-verify.yml` and `.github/workflows/blueprint-pages.yml` exist under the project root, with that exact spelling |
| `diagnostics` | a list of `{severity, code, message, path}`, sorted by severity (`error` first), then code, then path; `path` is the file concerned, relative to the project root, or `null` |

The `mathlib` object has `source` (`"lake-manifest.json"` or
`".lake/package-overrides.json"`, whichever Lake takes the entry from),
`type` (`"git"` or `"path"`), `inherited`, and the entry's `url` (with any
embedded credentials replaced by `***`), `input_rev`, `rev`, `dir` and
`sub_dir`, each `null` when the entry does not have it, and `config_file`
and `manifest_file`, which default to `"lakefile"` and
`"lake-manifest.json"` when the entry has no value or `null`, as Lake reads
them.

Errors mean Lake would fail on the project, a file Autoform needs cannot be
read, or Autoform's bounded parser cannot safely decode the input; the last
case can include a Lake-valid file. Any error makes the status
`indeterminate`. Warnings mean Lake would work but Autoform cannot decide, or
wants to point something out.

| Code | Severity | Meaning |
|---|---|---|
| `target-unreadable` | error | The target path cannot be resolved. |
| `project-not-found` | error | No directory at or above the target has a `lakefile.lean`, `lakefile.toml` or `lean-toolchain`. |
| `project-changed-during-inspection` | error | The decision files kept changing across every retry, so no single snapshot was read. |
| `unreadable-file` | error | A decision file is not a regular, readable UTF-8 file of at most 1 MiB. |
| `missing-lake-config` | error | There is neither `lakefile.toml` nor `lakefile.lean`. |
| `invalid-lakefile-toml` | error | `lakefile.toml` is not TOML that Autoform can decode safely, including a Lake-valid file beyond the bounded parser's resource limits, or Lake would refuse a field that the report reads (a bad name or version, a malformed require, or a target entry or name Lake rejects). |
| `missing-lean-toolchain` | error | There is no `lean-toolchain`. |
| `invalid-lean-toolchain` | error | The trimmed first line of `lean-toolchain` is empty or contains whitespace or a non-printable character. |
| `invalid-lake-manifest` | error | `lake-manifest.json` or `.lake/package-overrides.json` is not JSON Lake decodes, or has an unknown version. |
| `lake-manifest-incomplete` | error | A requirement of `lakefile.toml` is recorded by neither the manifest nor the overrides, or there are requirements and they record no packages; Lake asks for `lake update`. |
| `missing-lake-manifest` | warning | There is no `lake-manifest.json`, so the locked Mathlib is unknown; Lake would run an update. |
| `unsupported-lake-manifest` | warning | A manifest or overrides file uses a legacy layout (before version 0.7) that Lake still reads but Autoform does not decode. |
| `lakefile-lean-not-evaluated` | warning | `lakefile.lean` takes precedence and is never evaluated, so its package and Mathlib are unknown. |
| `mathlib-overridden` | warning | `.lake/package-overrides.json` selects the reported Mathlib entry whenever Mathlib is active. |
| `lake-manifest-stale` | warning | `lakefile.toml` asks for a different Mathlib than the manifest locks; Lake builds the locked one. |
| `mathlib-manifest-unused` | warning | Autoform cannot establish that the selected Mathlib is active: `lakefile.toml` does not directly require it, an inherited entry may be stale because dependency lakefiles are not inspected, or the root package is itself named `mathlib` and Lake reuses it. |
| `release-indeterminate` | warning | With no error, the toolchain or the Mathlib Lake builds is unknown or path-based. |
| `release-unlisted` | warning | The toolchain and Mathlib are known but are not a bundled catalog pair, or Mathlib is not loaded the way releases load it (another `subDir`, `configFile` or `manifestFile`). |
