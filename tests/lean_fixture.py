"""A Lean project that real-Lean server tests build, and the mark that skips them without Lean."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import _SKELETON_FIXTURE, _lean_toolchain_available


_requires_real_lean = pytest.mark.skipif(
    not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain"
)


def _real_lean_project(root: Path, main: str, **modules: str) -> Path:
    (root / "Probe").mkdir(parents=True)
    (root / "lean-toolchain").write_text(
        (_SKELETON_FIXTURE / "lean-toolchain").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "lakefile.toml").write_text('name = "probe"\n\n[[lean_lib]]\nname = "Probe"\n', encoding="utf-8")
    (root / "Probe" / "Main.lean").write_text(main, encoding="utf-8")
    for name, text in modules.items():
        (root / "Probe" / f"{name}.lean").write_text(text, encoding="utf-8")
    return root
