"""Every WebKit test file runs in CI's browser job, and `make test-browser` runs the same files.

The browser tests skip outside that job, so a file it does not list never runs anywhere; CONTRIBUTING.md says
`make test-browser` runs them as CI does.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_LISTED = re.compile(r"tests/ui/\w+_browser\.py")


def _ci_browser_job() -> set[str]:
    workflow = (_ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    job = workflow.split("\n  browser:\n", 1)[1].split("\n  mathjax:\n", 1)[0]
    return set(_LISTED.findall(job))


def _make_test_browser() -> set[str]:
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("\ntest-browser:\n", 1)[1].split("\n\n", 1)[0]
    return set(_LISTED.findall(target))


def test_ci_and_make_run_every_browser_test_file() -> None:
    on_disk = {path.relative_to(_ROOT).as_posix() for path in (_ROOT / "tests/ui").glob("*_browser.py")}
    assert on_disk
    assert _ci_browser_job() == on_disk
    assert _make_test_browser() == on_disk
