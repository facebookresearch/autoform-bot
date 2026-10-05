# Lean servers

Autoform's four Lean tools require an absolute `project_dir`, so the plugin
directory is never mistaken for the user's Lean project. Stateless Mathlib or
community search stays outside the server surface and uses host-native tools
when useful.

## Shared runtime

Plugin hosts start the two stdio MCP processes automatically. They are
lightweight adapters: the first Lean tool call race-safely starts a detached
runtime for the current AutoformBot installation and Unix user on the compute
node. That runtime owns one resident REPL pool and LSP session per active Lean
project, so sessions using that exact installation reuse the same warmed
processes. Separate checkouts or installed copies intentionally use separate
runtimes and resource budgets.

Closing the session that started it does not stop it; after a crash, the next
tool call starts it again. Runtime sockets include a code-and-dependency
fingerprint, so an in-place upgrade gracefully replaces the older build.
Bootstrap and lifetime locks omit the build and wire-protocol versions,
preventing an upgraded client from starting beside a draining older daemon.
Upgrade discovery uses only the
stable `daemon.ping` and `daemon.shutdown` control messages with the protocol
encoded in each socket name; a newer or unidentifiable generation fails closed.

REPL and LSP processes remain lazy. A cold tool call stays pending while Lean
warms up, so no `/repl-start`, `/lsp-start`, or model-side sleep is needed. Idle
project processes are closed after 30 minutes by default, while the small
runtime remains available. Its lifecycle is also explicit:

```bash
uv run autoform-lean-runtime start
uv run autoform-lean-runtime status
uv run autoform-lean-runtime stop
```

`stop` is graceful: it waits for admitted tool calls and Lean children to
finish shutting down before a subsequent `start` can replace the runtime.

The LSP session delegates Lean's evolving JSON-RPC protocol to the pinned
`leanclient` backend. In particular, diagnostics and hover wait on Lean's
`textDocument/waitForDiagnostics` barrier instead of guessing completion from
a quiet stdout interval. Autoform still owns admission deadlines, project-path
validation, a scrubbed Lake environment, and verified process-group cleanup.
This backend requires Lean 4.24 or newer; Autoform's bundled project pins a
supported stable release.

The private socket lives below `$XDG_RUNTIME_DIR/autoform`, falling back to a
uid-specific directory in `/tmp`; the rotating runtime log is beside it.
`AUTOFORM_RUNTIME_DIR` overrides that location. Per-installation limits are
controlled by `AUTOFORM_REPL_TOTAL_WORKERS`, `AUTOFORM_REPL_WORKERS_PER_PROJECT`,
`AUTOFORM_MAX_LEAN_PROJECTS`, and `AUTOFORM_LEAN_IDLE_SECONDS`. The first
process to start the runtime supplies those settings until it is stopped.
`AUTOFORM_REPL_REQUEST_TIMEOUT` sets the default end-to-end REPL call budget
(180 seconds), bounded by `AUTOFORM_MAX_REPL_REQUEST_SECONDS`. The client-side
`AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` must remain above the per-installation
request limits.
