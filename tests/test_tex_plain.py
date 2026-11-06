"""tex-plain.js, the reducer behind every label that cannot typeset, against the shared case table."""

from __future__ import annotations

import json
from pathlib import Path

from tests.dom_fixture import run_dom

_ROOT = Path(__file__).resolve().parents[1]
_CASES = json.loads((_ROOT / "tests/fixtures/tex-plain-cases.json").read_text(encoding="utf-8"))


def test_every_case_reads_as_its_table_says() -> None:
    out = run_dom(
        "load(args.asset); return args.cases.map(c => window.AutoformTexPlain.math(c.tex));",
        args={"asset": str(_ROOT / "autoform_cli/assets/tex-plain.js"), "cases": _CASES},
    )
    assert json.loads(out) == [case["text"] for case in _CASES]
