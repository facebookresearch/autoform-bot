"""Changelog fragments: each pull request adds a file to changelog.d/, and a release moves them into CHANGELOG.md."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "changelog.py"
_spec = importlib.util.spec_from_file_location("changelog_script", _SCRIPT)
assert _spec is not None and _spec.loader is not None
changelog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog)

_PREAMBLE = "# Changelog\n\nNotable changes.\n"
_RELEASED = "## 0.7.0 (2026-11-11)\n\n### Added\n\n- **Old.** Shipped (#1).\n"


def _repo(tmp_path: Path, fragments: dict[str, str], changelog_text: str = _PREAMBLE + "\n" + _RELEASED) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.8.0"\n', encoding="utf-8")
    (tmp_path / "changelog.d").mkdir()
    for name, text in fragments.items():
        (tmp_path / "changelog.d" / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_the_repository_fragments_pass_the_check(repo_root: Path) -> None:
    run = subprocess.run([sys.executable, str(_SCRIPT), "check"], cwd=repo_root, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize(
    ("name", "text", "problem"),
    [
        ("fixes-a-crash.md", "- **Crash.** Gone (#2).\n", "name it SECTION-SLUG.md"),
        ("fixed_crash.md", "- **Crash.** Gone (#2).\n", "name it SECTION-SLUG.md"),
        ("fixed-crash.md", "\n\n", "empty"),
        ("fixed-crash.md", "### Fixed\n\n- **Crash.** Gone (#2).\n", "fixed-crash.md:1: start each entry"),
        ("fixed-crash.md", "- Crash gone (#2).\n", "fixed-crash.md:1: start each entry"),
        ("fixed-crash.md", "- **Crash.** Gone\nfor good (#2).\n", "fixed-crash.md:2: start each entry"),
        ("fixed-crash.md", "- **Crash.** Gone (#2).\n- **Hang.** Gone.\n", "cite the pull request"),
    ],
)
def test_the_check_names_each_broken_fragment(tmp_path: Path, name: str, text: str, problem: str) -> None:
    found = changelog.problems(_repo(tmp_path, {name: text}))
    assert len(found) == 1 and problem in found[0], found


def test_the_check_refuses_an_unreleased_section_in_the_changelog(tmp_path: Path) -> None:
    root = _repo(tmp_path, {}, _PREAMBLE + "\n## Unreleased\n\n### Added\n\n- **New.** (#3)\n\n" + _RELEASED)
    assert [problem.split(":")[0] for problem in changelog.problems(root)] == ["CHANGELOG.md"]


def test_a_release_moves_the_fragments_under_the_new_version_in_section_order(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "fixed-b.md": "- **B.** Fixed (#5).\n",
            "added-z.md": "- **Z.** Added (#4).\n\n  A second paragraph.\n",
            "fixed-a.md": "- **A.** Fixed (#6).\n",
            "upgrading-pin.md": "- **Pin.** Move it (#7).\n",
        },
    )

    assert changelog.release(root, "2026-11-20") == 4

    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == (
        _PREAMBLE + "\n## 0.8.0 (2026-11-20)\n\n"
        "### Upgrading from 0.7.0\n\n- **Pin.** Move it (#7).\n\n"
        "### Added\n\n- **Z.** Added (#4).\n\n  A second paragraph.\n\n"
        "### Fixed\n\n- **A.** Fixed (#6).\n- **B.** Fixed (#5).\n\n" + _RELEASED
    )
    assert not list((root / "changelog.d").iterdir())
    with pytest.raises(SystemExit, match="already has 0.8.0"):
        changelog.release(root, "2026-11-21")
