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
        """Compile a Lean snippet in a fresh project-scoped REPL process.

        Args:
            project_dir: Absolute path to the Lake project root.
            code: Lean code to execute.
            timeout: Optional total post-admission timeout in seconds. It
                covers the idle-slot wait, Lean header validation, disposable
                child startup, generated imports, and submitted code.
        """
        return runtime.request(
            "repl.run",
            {"project_dir": project_dir, "code": code, "timeout": timeout},
        )

    @server.tool
    def get_repl_status(project_dir: str) -> str:
        """Return pool capacity, memory use, lifecycle state, and shutdown state.

        State is ``cold``, ``warming``, ``warm``, or ``retiring``. Memory use
        is available only for a warm resident; retiring reports zero and
        ``shutdown: true`` while verified cleanup remains in progress.

        Args:
            project_dir: Absolute path to the Lake project root.
        """
        return json.dumps(runtime.request("repl.status", {"project_dir": project_dir}))

    return server


def main() -> None:
    create_repl_server(LeanRuntimeClient()).run(transport="stdio")


if __name__ == "__main__":
    main()
