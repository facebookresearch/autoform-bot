# Lean servers

Autoform's four Lean tools require an absolute `project_dir`, so the plugin
directory is never mistaken for the user's Lean project. Stateless Mathlib or
community search stays outside the server surface and uses host-native tools
when useful.

## Shared runtime

Plugin hosts start the two stdio MCP processes automatically. They are
lightweight adapters: the first Lean tool call race-safely starts a detached
runtime for the current AutoformBot installation, Unix user, and compute node.
That runtime owns one REPL admission pool and LSP session per active Lean
project. LSP sessions stay warm; each public REPL call gets a fresh child and
no process-owned environment or proof-state handle survives the response.
Before that child starts, the selected Lake toolchain's `lean --deps-json`
parser validates the submitted header and its complete import set; a rejected
or unrecognized parser response fails closed. Execution names the
package-qualified `@repl/repl` target so a project target cannot shadow it.
Closing the session that started it does not stop it; after a crash, the next
tool call starts it again. Runtime sockets include a code fingerprint, so an
in-place upgrade gracefully replaces the older build.

REPL children and LSP processes remain lazy. A cold tool call stays pending
while Lean warms up, so no `/repl-start`, `/lsp-start`, or model-side sleep is
needed. The REPL `timeout` is one total post-admission budget for an idle-slot
wait, header validation, child startup, generated imports, and submitted code;
the default and maximum are 240 seconds. Project-cache admission uses the
daemon response budget separately. Idle project state is closed after 30
minutes by default, while the small runtime remains available. Its lifecycle
is also explicit:

```bash
uv run autoform-lean-runtime start
uv run autoform-lean-runtime status
uv run autoform-lean-runtime stop
```

`stop` is graceful: it waits for admitted tool calls and Lean children to
finish shutting down before a subsequent `start` can replace the runtime.

The private socket lives below `$XDG_RUNTIME_DIR/autoform`, falling back to a
uid-specific directory in `/tmp`; the rotating runtime log is beside it.
`AUTOFORM_RUNTIME_DIR` overrides that location. Node-wide limits are controlled
by `AUTOFORM_REPL_TOTAL_WORKERS`, `AUTOFORM_REPL_WORKERS_PER_PROJECT`,
`AUTOFORM_MAX_LEAN_PROJECTS`, and `AUTOFORM_LEAN_IDLE_SECONDS`. The first
process to start the runtime supplies those settings until it is stopped.
`LEAN_REPL_CMD` selects the disposable child command. An absolute `lake`
executable is reused for header validation; a container or other wrapper must
also set `LEAN_REPL_HEADER_CMD` to the matching toolchain-owned parser command.
`AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` bounds project admission, retained cleanup,
the tool operation, and a safety margin. A `warm` REPL project means its cold
wrapper pool is cached; it does not mean a REPL subprocess remains resident.
