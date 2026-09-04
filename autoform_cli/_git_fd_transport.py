"""Run one side of Git's local smart transport from a pinned directory FD."""

from __future__ import annotations

import os
import sys


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in {"upload", "receive"}:
        raise SystemExit("usage: _git_fd_transport.py {upload|receive} DIRECTORY_FD")
    mode = sys.argv[1]
    try:
        directory_fd = int(sys.argv[2])
        os.fchdir(directory_fd)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"could not enter pinned Git repository: {exc}") from exc
    os.execvp("git", ["git", f"{mode}-pack", "."])


if __name__ == "__main__":
    main()
