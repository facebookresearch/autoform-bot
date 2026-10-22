"""Lean REPL MCP server."""

from __future__ import annotations

import json

from fastmcp.server import FastMCP

from servers.lean_client import LeanRuntimeClient


def create_repl_server(runtime: LeanRuntimeClient) -> FastMCP:
    """Create the public REPL MCP adapter for the shared Lean runtime."""
    server = FastMCP(name="autoform-repl")

    @server.tool
    def run_lean_code(project_dir: str, code: str, timeout: float | None = None) -> str:
        """Compile a Lean snippet in a fresh Lean REPL process for the project.

        ``import Mathlib`` is added in front unless the snippet starts with
        ``module`` or ``prelude`` or already has ``import Mathlib``. Imports
        must come from Mathlib, Aesop, Batteries, or LeanSearchClient. To
        check code that needs other modules, such as the project's own, save
        it in the project and use ``lean_diagnostic_messages``.

        Each call starts Lean and loads its imports from scratch, including an
        added ``import Mathlib``, so even a one-line snippet takes at least as
        long as that import.

        Args:
            project_dir: Absolute path to the Lake project root.
            code: Lean code to execute.
            timeout: Optional seconds for the call once the runtime admits its
                project: waiting for a free slot, the header check, starting
                Lean, the imports, and the code. Waiting for admission comes
                first and is not counted. The default and maximum are 240
                seconds unless the operator changes them.
        """
        return runtime.request(
            "repl.run",
            {"project_dir": project_dir, "code": code, "timeout": timeout},
        )

    @server.tool
    def get_repl_status(project_dir: str) -> str:
        """Return pool capacity, memory use, lifecycle state, and shutdown state.

        State is ``cold``, ``warming``, ``warm``, or ``retiring``; ``warm``
        means the project's slots are cached, not that a REPL process stays
        alive. Memory use counts only processes of calls in progress, so it is
        zero between calls; retiring reports zero and ``shutdown: true`` while
        verified cleanup remains in progress.

        Args:
            project_dir: Absolute path to the Lake project root.
        """
        return json.dumps(runtime.request("repl.status", {"project_dir": project_dir}))

    return server


def main() -> None:
    create_repl_server(LeanRuntimeClient()).run(transport="stdio")


if __name__ == "__main__":
    main()
