"""Auto-install and launch Autoform's pinned Lean Beam MCP server."""

from __future__ import annotations

import os
import sys

from .beam import BeamInstallError, ensure_managed_beam, inspect_beam_runtime


def main() -> int:
    try:
        configured = os.environ.get("AUTOFORM_LEAN_BEAM_MCP")
        executable = ensure_managed_beam() if configured is None else configured
        inspection = inspect_beam_runtime(executable)
    except (BeamInstallError, OSError, ValueError) as error:
        print(f"autoform Lean Beam bootstrap failed: {error}", file=sys.stderr)
        return 1
    if not inspection.ok:
        for issue in inspection.issues:
            print(f"autoform Lean Beam bootstrap failed [{issue.code}]: {issue.message}", file=sys.stderr)
        return 1
    assert inspection.command is not None
    executable = inspection.command
    os.execv(executable, [executable])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
