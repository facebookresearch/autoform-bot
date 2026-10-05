"""Shared path validation for Autoform's LSP and REPL servers.

Lean tools always name an absolute Lake project and never infer one from the
server process's working directory.
"""

from __future__ import annotations

from pathlib import Path

LAKE_PROJECT_MARKERS = ("lakefile.lean", "lakefile.toml", "lake-manifest.json")


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
    if not any((path / marker).is_file() for marker in LAKE_PROJECT_MARKERS):
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
    if path.suffix != ".lean":
        raise ValueError(f"file_path must name a .lean file: {path}")
    if not path.is_file():
        raise ValueError(f"file_path does not exist: {path}")
    return root, path
