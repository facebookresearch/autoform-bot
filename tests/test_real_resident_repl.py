"""Opt-in integration tests of the resident REPL against the pinned upstream Lean REPL."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from servers.repl import resident as rr
from servers.repl.core import default_header_command
from tests.test_real_repl import REPL_FIXTURE

pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOFORM_RUN_REAL_REPL_TESTS") != "1",
    reason="set AUTOFORM_RUN_REAL_REPL_TESTS=1 to run pinned REPL integration",
)

_FILE = (
    "import Mathlib\n"
    "\n"
    "theorem other : True := by\n"
    "  sorry\n"
    "\n"
    "theorem probe (n : Nat) : n + 0 = n := by\n"
    "  sorry\n"
)


@pytest.fixture
def resident():
    repl = rr.ResidentRepl(REPL_FIXTURE, ["lake", "exe", "repl"], header_command=default_header_command("lake"))
    yield repl
    repl.close()


def _try(repl, path: Path, line: int, tactics: list[str]) -> dict:
    return repl.try_tactics(str(path), line, tactics, per_candidate_timeout=60, total_timeout=300)


def test_candidates_are_judged_by_the_declaration_they_edit(resident, tmp_path):
    path = tmp_path / "Probe.lean"
    path.write_text(_FILE, encoding="utf-8")

    result = _try(resident, path, 6, ["simp", "exact 1", "sorry"])

    # `other` still uses sorry, which does not count against a candidate in `probe`.
    assert [attempt["outcome"] for attempt in result["attempts"]] == ["closed", "error", "incomplete"]
    assert result["backend"] == "repl"
    assert "note" not in result
    # The second call reuses the warm process.
    assert _try(resident, path, 6, ["rfl"])["attempts"][0]["outcome"] == "closed"
    assert resident.restarts == 1


def test_a_header_lean_reads_differently_falls_back(resident, tmp_path):
    path = tmp_path / "Probe.lean"
    # The split takes the whole first line for a comment; Lean reads the import after it.
    path.write_text("/- note -/ import Mathlib\n" + _FILE.removeprefix("import Mathlib\n"), encoding="utf-8")

    with pytest.raises(rr.ResidentFallback, match="reads this header's imports differently"):
        _try(resident, path, 6, ["simp"])


def test_a_stale_import_is_used_as_built_and_named(resident, tmp_path):
    path = tmp_path / "Probe.lean"
    path.write_text(_FILE, encoding="utf-8")
    source = REPL_FIXTURE / "Mathlib.lean"
    olean = REPL_FIXTURE / ".lake" / "build" / "lib" / "lean" / "Mathlib.olean"
    before = source.stat()
    later = olean.stat().st_mtime_ns + 10**9
    os.utime(source, ns=(later, later))
    try:
        result = _try(resident, path, 6, ["simp"])
    finally:
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert result["attempts"][0]["outcome"] == "closed"
    assert "used that build: Mathlib" in result["note"]


def test_a_module_header_falls_back(resident, tmp_path):
    path = tmp_path / "Probe.lean"
    path.write_text("module\n\n" + _FILE.replace("import Mathlib", "public import Mathlib"), encoding="utf-8")

    with pytest.raises(rr.ResidentFallback, match="`module` header"):
        _try(resident, path, 8, ["simp"])
