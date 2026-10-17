# Lean servers

Autoform's four Lean tools require an absolute `project_dir`, so the plugin
directory is never mistaken for the user's Lean project. Stateless Mathlib or
community search stays outside the server surface and uses host-native tools
when useful.

## Shared runtime

Plugin hosts start the two stdio MCP processes automatically. They are
lightweight adapters: the first Lean tool call race-safely starts a detached
runtime for the current AutoformBot installation, Unix user, and compute node.
That runtime owns one REPL slot pool and LSP session per active Lean
project. Closing the session that started the runtime does not stop it; after a
crash, the next tool call starts it again. Runtime sockets include a code
fingerprint, so an in-place upgrade gracefully replaces the older build.

LSP sessions stay warm; each public REPL call gets a fresh child and no
process-owned environment or proof-state handle survives the response. Before
that child starts, the selected Lake toolchain runs Lean's own header parser,
which the REPL also uses, on the submitted source. Every import it reports must
have one of the allowlisted roots `Mathlib`, `Aesop`, `Batteries`, or
`LeanSearchClient` (a plain `Init` import is ignored), and a rejected or
unrecognized parser response fails closed.
Allowlisted imports require plain, nonempty canonical module components; names
that remain quoted or path-like after Lean parsing are refused instead of being
reduced to a misleading allowlisted root. The allowlist is a policy check, not a
sandbox: each call runs with the runtime user's privileges, and `Init` alone
provides file and process IO, for example `#eval IO.Process.run`. The header
check runs as one extra short Lean process per call. Execution names the
package-qualified `@repl/repl` target so a project target cannot shadow it.

REPL children and LSP processes remain lazy. A tool call stays pending while
Lean starts, so no `/repl-start`, `/lsp-start`, or model-side sleep is needed.
Every REPL call starts Lean afresh and, unless its header starts with `module`
or `prelude`, imports Mathlib, so it takes at least as long as `import Mathlib`
on the node. Calls to the same project run one at a time unless
`AUTOFORM_REPL_WORKERS_PER_PROJECT` is raised. The REPL `timeout` is one total
post-admission budget for an idle-slot wait, header validation, child startup,
generated imports, and submitted code; it and its cap default to 240 seconds.
Project-cache admission uses the daemon response budget separately. Idle project
state is closed after 30 minutes by default, while the small runtime remains
available. Its lifecycle is also explicit:

```bash
uv run autoform-lean-runtime start
uv run autoform-lean-runtime status
uv run autoform-lean-runtime stop
```

`stop` is graceful: it waits for admitted tool calls and Lean children to
finish shutting down before a subsequent `start` can replace the runtime.

`lean_diagnostic_messages` runs `lake --no-build lean` once per call and waits
until Lean exits. When `LEAN_LSP_CMD` ends in `serve`, that command runs with
`--no-build lean` in place of `serve`; otherwise the `lake` on `PATH` runs.
Unbuilt or stale imports are reported, not built, and Lake names only the first
modules to build. At most `AUTOFORM_MAX_LEAN_PROJECTS` checks run at once.
`lean_hover` uses the project's Lean server, which builds stale imports inside
the call.

`LEAN_LSP_TIMEOUT` (60 seconds by default) bounds each call, including time
spent waiting for a free Lean slot or behind other LSP calls on the same
project. A call that runs out fails rather than returning partial results, and
a hover that runs out also restarts the project's Lean server. The value must
not exceed `AUTOFORM_MAX_LSP_REQUEST_SECONDS` (600 seconds by default) or the
runtime refuses to start, and raising that maximum to 745 seconds or more also
needs `AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` raised. Like the limits below, it is
read when the runtime starts: run `uv run autoform-lean-runtime stop` after
changing it.

The private socket lives below `$XDG_RUNTIME_DIR/autoform`, falling back to a
uid-specific directory in `/tmp`; the rotating runtime log is beside it.
`AUTOFORM_RUNTIME_DIR` overrides that location. Node-wide limits are controlled
by `AUTOFORM_REPL_TOTAL_WORKERS`, `AUTOFORM_REPL_WORKERS_PER_PROJECT`,
`AUTOFORM_MAX_LEAN_PROJECTS`, and `AUTOFORM_LEAN_IDLE_SECONDS`. A REPL worker is
one concurrent call slot; an idle slot holds no Lean process. The first process
to start the runtime supplies those settings until it is stopped.

`LEAN_REPL_CMD` selects the disposable child command. Its default,
`lake exe @repl/repl`, finds the REPL only through a require named `repl`. For a
require with any other name, rename the require to `repl`, or restart the
runtime with `LEAN_REPL_CMD="lake exe @<name>/repl"` for a require named
`<name>`, which applies to every project the runtime serves. A project with no
REPL require, as `autoform project new` and `init` leave it, fails every REPL
call until it adds one as the [setup skill](../skills/setup/SKILL.md) describes.
When `LEAN_REPL_HEADER_CMD` is unset and `LEAN_REPL_CMD` starts with a `lake`
executable, absolute or not, header validation reuses that executable without
the command's other arguments. A container or other wrapper must also set
`LEAN_REPL_HEADER_CMD` to a command that runs `servers/repl/parse_header.lean`
with the matching toolchain's `lean --run`, passing the source on standard
input. Like the limits above, `LEAN_REPL_CMD` and `LEAN_REPL_HEADER_CMD` are
node-wide and read only when the runtime starts; after changing either, run
`uv run autoform-lean-runtime stop`. The next process to start the runtime, an
MCP adapter serving a tool call or `uv run autoform-lean-runtime start`, must
have the new values in its environment, and
`uv run autoform-lean-runtime status` shows the resolved commands under
`config`.

`AUTOFORM_MAX_REPL_REQUEST_SECONDS` caps the REPL `timeout`; when
`AUTOFORM_REPL_REQUEST_TIMEOUT` is unset, the default `timeout` is the lower of
240 seconds and that cap. If `LEAN_REPL_CMD` does not start with a `lake`
executable and `LEAN_REPL_HEADER_CMD` is unset, if an explicit
`AUTOFORM_REPL_REQUEST_TIMEOUT` exceeds that cap, or if that cap does not fit
the response budget below, the runtime refuses to start, so the LSP tools are
unavailable as well.

`AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` (default 15 minutes) bounds project
admission, the tool operation, and a safety margin. Failed cleanup transfers to
a retained background reaper and may continue past that response budget rather
than allowing an overlapping replacement. A `warm` REPL project means its REPL
slot pool is cached; it does not mean a REPL subprocess remains resident.
