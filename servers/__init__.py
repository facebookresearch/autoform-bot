"""Shared path validation for Autoform's LSP and REPL servers.

Lean tools always name an absolute Lake project and never infer one from the
server process's working directory.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

LAKE_PROJECT_MARKERS = ("lakefile.lean", "lakefile.toml", "lake-manifest.json")
#: The errors pathlib read as "no such path" before Python 3.14, as in autoform_cli/_fs.py.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})
_ABSENT_WINERRORS = frozenset({21, 123, 1921})


def _is_file(path: Path) -> bool:
    """Whether ``path`` is a regular file, as autoform_cli._fs.path_is answers, which the servers cannot import: a
    missing path is not one, and a path that cannot be examined raises, as pathlib answered before Python 3.14."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError as error:
        if getattr(error, "winerror", None) in _ABSENT_WINERRORS or error.errno in _ABSENT_ERRNOS:
            return False
        raise
    except ValueError:  # a NUL in the path, or one the system cannot encode
        return False


def resolve_lean_project_dir(project_dir: str) -> Path:
    """Return a validated, absolute Lake project directory."""
    if not isinstance(project_dir, str) or not project_dir.strip():
        raise ValueError("project_dir is required and must be an absolute Lake project path")

    path = Path(project_dir).expanduser()
    if not path.is_absolute():
        raise ValueError(f"project_dir must be absolute, got {project_dir!r}")

    try:
        path = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"project_dir does not exist: {project_dir}") from exc

    if not path.is_dir():
        raise ValueError(f"project_dir is not a directory: {path}")
    if not any(_is_file(path / marker) for marker in LAKE_PROJECT_MARKERS):
        markers = ", ".join(LAKE_PROJECT_MARKERS)
        raise ValueError(f"project_dir is not a Lake project: {path} (expected one of: {markers})")
    return path


def resolve_lean_file(project_dir: str, file_path: str) -> tuple[Path, Path]:
    """Resolve an existing in-project Lean file without using cwd."""
    root = resolve_lean_project_dir(project_dir)
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path is required")
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"file_path must stay inside project_dir: {path}") from exc
    # Lean's rule: the extension follows the last dot unless that dot starts the
    # name. PurePath.suffix agreed until Python 3.14, which finds none in `..lean`.
    if not (len(path.name) > len(".lean") and path.name.endswith(".lean")):
        raise ValueError(f"file_path must name a .lean file: {path}")
    if not _is_file(path):
        raise ValueError(f"file_path does not exist: {path}")
    return root, path
