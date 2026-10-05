"""Exec a Lean language server with Autoform's process boundary.

``leanclient`` owns LSP framing and request routing.  This launcher owns the
two process concerns that its public API does not expose: a scrubbed Lake
environment and a process-group identity that Autoform can verify at cleanup.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from servers import clean_lake_environment


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 3 or arguments[1] != "--":
        raise SystemExit("usage: python -m servers.lsp.launcher PID_FILE -- COMMAND [ARG ...]")

    identity_path = Path(arguments[0])
    command = arguments[2:]
    if os.getpgrp() != os.getpid():
        raise RuntimeError("Lean LSP launcher must be started in its own process group")
    identity_path.write_text(
        json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}),
        encoding="utf-8",
    )
    os.execvpe(command[0], command, clean_lake_environment())


if __name__ == "__main__":
    main()
