# Lean Beam managed server contract

Autoform registers Lean Beam alongside its bundled `autoform-lsp` and
`autoform-repl` servers. This document defines the managed preview of the
Lean-FRO-maintained
[`leanprover/lean-beam`](https://github.com/leanprover/lean-beam) session API.
On first MCP start, Autoform fetches and checks out the exact locked commit,
runs Beam's own installer into Autoform-owned user state, verifies the live
`beam_version` identity, and `exec`s the upstream server. Autoform does not
proxy Lean operations or parse Beam's private broker protocol. In a Beam
session, do not interleave calls to the older Autoform servers for the same
workspace.

The supported development revision and protocol are recorded in
[`lean-beam.lock.json`](../lean-beam.lock.json). The managed install lives under
`$XDG_DATA_HOME/autoform/lean-beam/<commit>` (or
`~/.local/share/autoform/lean-beam/<commit>`); set `AUTOFORM_BEAM_HOME` to
override that root. The first start needs Git, Elan, network access to the
public Lean Beam repository, and enough time to build both tested toolchains.

Autoform's launcher delegates installation to the pinned checkout's own
`install-beam.sh`, but does not ask that installer to modify host configuration
or install Beam's companion skill. Run `autoform beam doctor --json` to start or
reuse the managed install, call the public `beam_version` tool, and compare the
live runtime with Autoform's packaged lock. A passing doctor report establishes
runtime identity, not the host controls listed in that report.

Autoform's Codex registration includes this denylist:

```toml
disabled_tools = ["lean_save", "lean_close_save"]
```

Use a host's equivalent technical denylist when it has one. If the selected
host cannot enforce the exclusion, do not call the save tools until the
upstream defects are fixed.

New Autoform scaffolds include `.beam/` in the root `.gitignore`. Add the rule
manually before first use in an existing project. Beam's workspace state is
derived local data and must not appear in commits.

Keep a finite caller-visible tool deadline. Codex documents
`[mcp_servers.lean-beam].tool_timeout_sec` as the per-server tool deadline and
lists a 60-second default. Configure an explicit finite value from
measured consumer evidence rather than relying on a client-version default.
That setting bounds the caller's wait; it does not by itself prove that the host
sends MCP cancellation or terminates the server, so expiry does not establish
either. See the
[official Codex MCP configuration reference](https://learn.chatgpt.com/docs/extend/mcp#other-configuration-options).

Autoform supplies the MCP registration for Codex, Claude, and Muse; Beam still
owns the actual server protocol and process after the launcher verifies it.

This pin is an exact commit from a draft pull request and is for integration
development, not release. The managed preview may coexist with the default
Autoform servers, but Autoform must not ship the cutover until Lean Beam
publishes a tagged release containing the opaque source-snapshot work from
[`leanprover/lean-beam#254`](https://github.com/leanprover/lean-beam/pull/254),
its release CI is green, and the save and external-build synchronization
defects in
[`#255`](https://github.com/leanprover/lean-beam/issues/255) and
[`#256`](https://github.com/leanprover/lean-beam/issues/256) are resolved in
that release. Setup also needs a public typed way to verify the selected
workspace toolchain and bundle, tracked in
[`#257`](https://github.com/leanprover/lean-beam/issues/257).
Release also requires accurate effect annotations. At this pin, `lean_run_at`
is advertised as read-only even though Lean tactics and metaprograms can perform
arbitrary IO; host approval policy must not rely on that annotation.
Until then, CI and preview setup additionally evaluate `Lean.versionString`
inside each workspace and compare it with `lake env lean --version`. That
checks which compiler served the request, but it is not a typed
workspace-identity API for agents.

## Explicit state model

Every workspace-bound call carries an absolute workspace descriptor:

```json
{"workspace":{"root":"/absolute/path/to/project"}}
```

Use the Beam API in this order:

1. Call `lean_sync` for a saved Lean file and retain its opaque `snapshot`.
2. Use `lean_run_at` for one isolated command or tactic block.
3. When continuation matters, use `lean_run_at_handle`, then prefer the
   non-linear `lean_run_with`. Advance to its `next_handle` only after semantic
   success, then release the parent. `lean_run_with_linear` consumes its parent
   before execution, so reserve it for deliberate consume-on-attempt flows.
4. Release unused handles with `lean_release`.
5. After a real source edit, save it and call `lean_update` or `lean_sync` for a
   fresh snapshot. Never substitute a fresh token into stale coordinates.
6. Use `lean_close` for one document and `lean_drop_workspace` after project
   configuration changes or when a workspace generation must be discarded.

Position fields use zero-based LSP line and character coordinates. Characters
are counted in UTF-16 code units, not Unicode scalar values or UTF-8 bytes.

There is no Autoform session id and no hidden continuation between calls. The
workspace root, saved source file and path, source snapshot, and proof handles
are the complete public state. Snapshots and handles are invalid after a source
edit, document close, backend or MCP restart, or workspace drop; synchronize
again instead of carrying either token across those boundaries.

The workspace descriptor routes a request; it is not a filesystem
authorization boundary. Beam requires an absolute, existing Lean/Lake project
root, but relative source paths may leave it through `..`, and absolute paths
may inspect dependency sources outside it. The integration test
records that behavior so a consumer cannot mistake the root for containment.
Source-path symlink behavior is not qualified by this preview.
Autoform has not yet adopted it as the release policy: the older project-root
admission requirement remains an open gate. Use the preview only where the MCP
owner process already runs inside an adequate operating-system or container
sandbox; the agent host's command sandbox does not imply that its MCP process is
confined.

`lean_run_at` accepts one top-level command or one tactic block. It is not a
replacement for the old arbitrary multi-command `run_lean_code` call, and it
cannot introduce imports. Put imports and multi-command changes in the saved
source file, then synchronize it. Autoform does not split Lean source with a
home-grown parser. Native multi-command speculation is tracked in
[`leanprover/lean-beam#100`](https://github.com/leanprover/lean-beam/issues/100).

Do not automatically retry a speculative command after cancellation, timeout,
or transport loss. Its result is unknown. Discard the affected workspace
generation or restart the MCP process, reread the file, synchronize it, and
resume from a fresh snapshot. Beam isolates document state, but Lean code and
metaprogramming can still perform arbitrary IO; this is not an operating-system
sandbox.

Beam cancellation is cooperative. Autoform does not supervise the Beam
process or add a timeout or retry layer. After sending cancellation, call
`lean_drop_workspace` under a finite host deadline. A returned drop is the
ordered fence proving that the earlier request settled and the workspace was
evicted; discard its snapshots and handles, then synchronize again. If the drop
does not return, it is waiting behind the stuck request and the MCP host must
terminate and restart the Beam process instead. Doing so invalidates every
handle owned by that process.

Until the upstream save issues are closed, Beam preview workflows use `lean_sync`
for the interactive diagnostics barrier and an external `lake build` for final
verification. Do not overlap that build with any Beam call. Workflows do not
call `lean_save` or `lean_close_save`; the direct Beam server still exposes
those tools, so the host must enforce the denylist above. Until
`leanprover/lean-beam#256` is fixed, any external
`lake build` performed while the MCP process is alive must be followed by
`lean_drop_workspace` before the next Beam operation; the next call recreates
the workspace from disk.

Integration CI covers cooperative MCP cancellation, post-cancellation eviction,
fresh synchronization, and clean EOF teardown. Before changing
`development_only` to false, each supported host must still prove that its
configured deadline sends cancellation and tears down the process when
cooperative cancellation does not settle. Server-level tests cannot establish
host behavior. Until that is proven for a host, treat a host-level tool timeout
as unknown execution state and restart the Beam MCP process before reuse.
