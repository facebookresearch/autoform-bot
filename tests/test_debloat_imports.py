from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from servers.prover import debloat_imports
from servers.prover.debloat_imports import DebloatSkipped, debloat, header_imports

HEADER = """/- Copyright -/
-- a comment
import Mathlib.A
import Mathlib.B  -- trailing comment

import Project.C

/-! # Doc -/
theorem t : True := trivial
"""


def test_header_imports_skips_comments_and_stops_at_code() -> None:
    assert header_imports(HEADER) == [(2, "Mathlib.A"), (3, "Mathlib.B"), (5, "Project.C")]
    assert header_imports("/- multi\nline -/\nimport X\n") == [(2, "X")]


@pytest.mark.parametrize("line", ["module", "public import Mathlib.A", "meta import Mathlib.A"])
def test_header_imports_refuses_module_system_files(line: str) -> None:
    with pytest.raises(DebloatSkipped, match="module system"):
        header_imports(f"{line}\nimport Mathlib.B\n")


class FakeLake:
    def __init__(self, *, probe: str, compiles: list[tuple[int, str]], fresh: bool = True) -> None:
        self.probe = probe
        self.compiles = list(compiles)
        self.fresh = fresh
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], project: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:3] == ["lake", "build", "--no-build"]:
            return subprocess.CompletedProcess(args, 0 if self.fresh else 1, "", "")
        if args[-1].startswith("/") and args[-1].endswith(".lean"):
            assert "findRedundantImports" in Path(args[-1]).read_text()
            return subprocess.CompletedProcess(args, 0, self.probe, "")
        code, out = self.compiles.pop(0)
        return subprocess.CompletedProcess(args, code, out, "")


def _file(tmp_path: Path) -> Path:
    (tmp_path / "lakefile.toml").write_text("")
    path = tmp_path / "File.lean"
    path.write_text(HEADER)
    return path


REDUNDANT_A = "DEBLOAT-REDUNDANT Mathlib.A\nDEBLOAT-CLOSURE 10 10 true\n"


def test_debloat_removes_only_redundant_lines(tmp_path: Path, monkeypatch) -> None:
    path = _file(tmp_path)
    lake = FakeLake(probe=REDUNDANT_A, compiles=[(0, ""), (0, "")])
    monkeypatch.setattr(debloat_imports, "run", lake)

    assert debloat(path, tmp_path) is True

    assert path.read_text() == HEADER.replace("import Mathlib.A\n", "")
    assert sum(c[:3] == ["lake", "env", "lean"] and c[-1] == "File.lean" for c in lake.calls) == 2


def test_debloat_assume_clean_compiles_once(tmp_path: Path, monkeypatch) -> None:
    path = _file(tmp_path)
    lake = FakeLake(probe=REDUNDANT_A, compiles=[(0, "")])
    monkeypatch.setattr(debloat_imports, "run", lake)
    assert debloat(path, tmp_path, assume_clean=True) is True
    assert lake.compiles == []


def test_debloat_reverts_on_new_errors_but_tolerates_existing_ones(tmp_path: Path, monkeypatch) -> None:
    path = _file(tmp_path)
    old = "File.lean:9:0: error: unsolved goals"
    new = "File.lean:8:0: error: unknown identifier 'x'"
    monkeypatch.setattr(debloat_imports, "run", FakeLake(probe=REDUNDANT_A, compiles=[(1, old), (1, new)]))
    assert debloat(path, tmp_path) is False
    assert path.read_text() == HEADER

    shifted = "File.lean:8:0: error: unsolved goals"
    monkeypatch.setattr(debloat_imports, "run", FakeLake(probe=REDUNDANT_A, compiles=[(1, old), (1, shifted)]))
    assert debloat(path, tmp_path) is True


def test_debloat_refuses_when_closure_changes_or_build_is_stale(tmp_path: Path, monkeypatch) -> None:
    path = _file(tmp_path)
    bad = "DEBLOAT-REDUNDANT Mathlib.A\nDEBLOAT-CLOSURE 10 9 true\n"
    monkeypatch.setattr(debloat_imports, "run", FakeLake(probe=bad, compiles=[]))
    with pytest.raises(DebloatSkipped, match="closure check failed"):
        debloat(path, tmp_path)

    monkeypatch.setattr(debloat_imports, "run", FakeLake(probe=REDUNDANT_A, compiles=[], fresh=False))
    with pytest.raises(DebloatSkipped, match="out of date"):
        debloat(path, tmp_path)
    assert path.read_text() == HEADER


def test_debloat_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    path = _file(tmp_path)
    monkeypatch.setattr(debloat_imports, "run", FakeLake(probe=REDUNDANT_A, compiles=[]))
    assert debloat(path, tmp_path, dry_run=True) is True
    assert path.read_text() == HEADER
