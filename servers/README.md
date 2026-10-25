# Lean servers

Autoform's four Lean tools require an absolute `project_dir`, so the plugin
directory is never mistaken for the user's Lean project. Mathlib and community
search are not server tools; use host-native tools for them.

## Shared runtime

Plugin hosts start the two stdio MCP adapters automatically. The first Lean
tool call starts one detached runtime per AutoformBot installation, Unix user,
and compute node, which owns one REPL slot pool and LSP session per active Lean
project. It outlives the session that started it, starts again on the next tool
call after a crash, and is replaced gracefully by an in-place upgrade. REPL
children and LSP processes start lazily, and a tool call stays pending while
Lean starts, so no `/repl-start`, `/lsp-start`, or model-side sleep is needed.

```bash
uv run autoform-lean-runtime start
uv run autoform-lean-runtime status
uv run autoform-lean-runtime stop
```

`stop` is graceful: it returns once admitted tool calls and Lean children have
shut down, so a later `start` replaces the runtime cleanly.

## REPL tools

`run_lean_code` runs code in a fresh REPL child for each call, so no
process-owned environment or proof-state handle survives the response. Before
that child starts, Lean's own header parser, run by the selected Lake
toolchain, reads the submitted source. Every import it reports must have one of
the allowlisted roots `Mathlib`, `Aesop`, `Batteries`, or `LeanSearchClient` (a
plain `Init` import is ignored); quoted or path-like module names are refused,
and a rejected or unrecognized parser response fails closed. Code with other
imports, such as the project's own modules, is checked as a saved file with
`lean_diagnostic_messages` instead. The allowlist is a policy check, not a
sandbox: each call runs with the runtime user's privileges, and `Init` alone
provides file and process IO, for example `#eval IO.Process.run`.

A REPL call whose header does not start with `module` or `prelude` imports
Mathlib, so it takes at least as long as `import Mathlib` on the node. The REPL
`timeout` is one total budget for waiting for an idle slot, the header check,
child startup, imports, and the submitted code; admitting the project counts
against `AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` instead. `get_repl_status` reports a
project as `cold`, `warming`, `warm`, or `retiring`; `warm` means its slot pool
is cached, not that a REPL subprocess remains resident.

## LSP tools

`lean_diagnostic_messages` runs `lake --no-build lean` once per call and waits
until Lean exits. When `LEAN_LSP_CMD` ends in `serve`, that command runs with
`--no-build lean` in place of `serve`; otherwise the `lake` on `PATH` runs.
Unbuilt or stale imports are reported, not built, and Lake names only the first
modules to build. At most `AUTOFORM_MAX_LEAN_PROJECTS` checks run at once.
`lean_hover` uses the project's Lean server, which stays warm between calls and
builds stale imports inside the call.

`LEAN_LSP_TIMEOUT` bounds each `lean_diagnostic_messages` call, including
waiting for a free Lean slot. For `lean_hover` its clock starts once the
project's Lean server is running and covers waiting behind other hovers on the
same project; waiting for a project slot and starting the server count against
`AUTOFORM_RUNTIME_RESPONSE_TIMEOUT`, so a cold hover can take about 870 seconds
by default. A call that runs out fails rather than returning partial results.
A hover left less than a second (or half of `LEAN_LSP_TIMEOUT`, if that is
less) after waiting behind other hovers is refused before it starts.

## Settings

Unless noted, every setting below is node-wide and read only when the runtime
starts, from the environment of the process that starts it (an MCP adapter
serving a tool call, or `uv run autoform-lean-runtime start`). After changing
one, run `uv run autoform-lean-runtime stop` and make sure the next starter has
the new value; `uv run autoform-lean-runtime status` shows the resolved values
under `config`.

- `AUTOFORM_MAX_LEAN_PROJECTS` (default 4): projects with loaded Lean state.
- `AUTOFORM_REPL_TOTAL_WORKERS` (default one per 32 GB of memory, at least 1)
  and `AUTOFORM_REPL_WORKERS_PER_PROJECT` (default 1): concurrent REPL calls;
  a worker is one call slot.
- `AUTOFORM_LEAN_IDLE_SECONDS` (default 1800): when idle project state closes;
  the runtime itself stays up.
- `LEAN_LSP_TIMEOUT` (default 60 seconds), at most
  `AUTOFORM_MAX_LSP_REQUEST_SECONDS` (default 600). Raising that maximum to 745
  seconds or more also needs `AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` raised.
- `AUTOFORM_REPL_REQUEST_TIMEOUT`, the default REPL `timeout` (default 240
  seconds or the cap if lower), at most `AUTOFORM_MAX_REPL_REQUEST_SECONDS`
  (default 240).
- `AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` (default 15 minutes): project admission,
  the tool operation, and a safety margin. Each client also reads it from its
  own environment as its wait for a response. Cleanup that fails within it
  passes to a background reaper, which may run past it; that project's
  replacement Lean processes wait until the reaper finishes.
- `AUTOFORM_RUNTIME_DIR`: where the private socket and the rotating runtime log
  live, by default below `$XDG_RUNTIME_DIR/autoform`, falling back to a
  uid-specific directory in `/tmp`. Every client reads it from its own
  environment to find the socket, so every adapter and `autoform-lean-runtime`
  command must have the same value.
- `LEAN_REPL_CMD` (default `lake exe @repl/repl`): the disposable child
  command, for every project the runtime serves. The default finds the REPL
  only through a require named `repl`; rename another require to `repl`, or set
  `LEAN_REPL_CMD="lake exe @<name>/repl"` for a require named `<name>`. A
  project with no REPL require, as `autoform project new` and `init` leave it,
  fails every REPL call until it adds one as the
  [setup skill](../skills/setup/SKILL.md) describes.
- `LEAN_REPL_HEADER_CMD`: the header check. Unset, it reuses the `lake`
  executable that `LEAN_REPL_CMD` starts with. A container or other wrapper
  must set it to a command that runs `servers/repl/parse_header.lean` with the
  matching toolchain's `lean --run`, passing the source on standard input.

The runtime refuses to start, which leaves the LSP tools unavailable too, when
`LEAN_LSP_TIMEOUT` exceeds its maximum, when an explicit
`AUTOFORM_REPL_REQUEST_TIMEOUT` exceeds its cap, when either maximum does not
fit `AUTOFORM_RUNTIME_RESPONSE_TIMEOUT`, when
`AUTOFORM_REPL_WORKERS_PER_PROJECT` exceeds `AUTOFORM_REPL_TOTAL_WORKERS`, or
when `LEAN_REPL_CMD` does not start with a `lake` executable and
`LEAN_REPL_HEADER_CMD` is unset.
