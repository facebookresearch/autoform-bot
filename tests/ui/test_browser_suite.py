"""Every WebKit test file runs in CI's browser job, and `make test-browser` runs the same files.

The browser tests skip outside that job, so a file it does not list never runs anywhere; CONTRIBUTING.md says
`make test-browser` runs them as CI does. Each list is read as its shell reads it, so a file commented out of the
command does not count as run.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _pytest_files(command: str) -> set[str]:
    """The browser test files a shell command hands pytest, its comments dropped."""
    words = shlex.split(command, comments=True)
    assert "pytest" in words, command
    return {word for word in words[words.index("pytest") + 1 :] if word.endswith("_browser.py")}


def _ci_browser_job() -> set[str]:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    runs = [step["run"] for step in workflow["jobs"]["browser"]["steps"] if "pytest" in step.get("run", "")]
    assert len(runs) == 1, runs
    return _pytest_files(runs[0])


def _make_test_browser() -> set[str]:
    # Make hands the shell each recipe line, a backslash at its end joining the next one to it.
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8").replace("\\\n", " ")
    recipe = makefile.split("\ntest-browser:", 1)[1].split("\n\n", 1)[0].splitlines()[1:]
    runs = [line for line in recipe if line.startswith("\t") and "pytest" in shlex.split(line, comments=True)]
    assert len(runs) == 1, recipe
    return _pytest_files(runs[0])


def test_ci_and_make_run_every_browser_test_file() -> None:
    on_disk = {path.relative_to(_ROOT).as_posix() for path in (_ROOT / "tests/ui").glob("*_browser.py")}
    assert on_disk
    assert _ci_browser_job() == on_disk
    assert _make_test_browser() == on_disk
