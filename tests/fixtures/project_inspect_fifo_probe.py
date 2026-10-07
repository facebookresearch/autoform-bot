"""Deadline-isolated FIFO probes for project inspection regression tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import autoform_cli.project.inspect as project_inspect
from autoform_cli.project import inspect_project


def main() -> int:
    mode, raw_root = sys.argv[1:]
    root = Path(raw_root)
    switched = False
    beneath = False
    if mode == "swap":
        lakefile = root / "lakefile.toml"
        original_open = project_inspect.os.open

        def racing_open(path, flags, *args, **kwargs):
            nonlocal switched, beneath
            if (Path(path) == lakefile or str(path) == "lakefile.toml") and not switched:
                switched = True
                beneath = kwargs.get("dir_fd") is not None
                lakefile.unlink()
                os.mkfifo(lakefile)
            return original_open(path, flags, *args, **kwargs)

        project_inspect.os.open = racing_open
        # A wrapped os.open is not in os.supports_dir_fd; keep the descriptor-relative opens.
        project_inspect.os.supports_dir_fd.add(racing_open)
    elif mode != "inspect":
        raise ValueError(f"unknown FIFO probe mode: {mode}")

    result = inspect_project(root)
    print(json.dumps({"result": result.as_dict(), "switched": switched, "dir_fd": beneath}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
