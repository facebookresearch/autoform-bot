"""tex-plain.js and its port in render, the reducers behind every label that cannot typeset, against one case table."""

from __future__ import annotations

import json
import re
import string
from pathlib import Path

from autoform_cli import tex_plain
from tests.dom_fixture import run_dom

_ROOT = Path(__file__).resolve().parents[1]
_ASSET = _ROOT / "autoform_cli/assets/tex-plain.js"
# One table for both readers, so a page and the browser read each case the same.
_CASES = json.loads((_ROOT / "tests/fixtures/tex-plain-cases.json").read_text(encoding="utf-8"))


def test_every_case_reads_as_its_table_says() -> None:
    out = run_dom(
        "load(args.asset); return args.cases.map(c => window.AutoformTexPlain.math(c.tex));",
        args={"asset": str(_ASSET), "cases": _CASES},
    )
    assert json.loads(out) == [case["text"] for case in _CASES]


def test_the_port_reads_every_case_as_the_reducer_does() -> None:
    assert [tex_plain.math(case["tex"]) for case in _CASES] == [case["text"] for case in _CASES]


def test_the_port_and_the_reducer_read_every_table_entry_alike() -> None:
    # The case table reaches a few entries of each table, and each reader keeps its own copy. So each name either
    # reader knows, and every word in the reducer's source, goes in as a command: before a group, before another
    # command (where an operator name takes a space) and after \not. So do each sign after \not, and each character
    # escaped, as a script and under \mathbb.
    names = set(re.findall(r"[A-Za-z]+", _ASSET.read_text(encoding="utf-8")))
    names |= set(tex_plain._SYMBOLS) | set(tex_plain._ACCENTS) | tex_plain._OPERATORS | tex_plain._FONTS
    names |= tex_plain._SIZES | tex_plain._STYLES
    signs = set(tex_plain._NEGATED) | set(tex_plain._NEGATED.values())
    characters = set(string.printable) | set(tex_plain._SUP) | set(tex_plain._SUB)
    inputs = sorted(
        {f"\\{name}{{x}}y \\{name}\\alpha \\not\\{name}" for name in names} | {f"a \\not{sign} b" for sign in signs}
        | {f"a\\{c}b x^{{{c}}} x_{{{c}}} \\mathbb{{{c}}}" for c in characters}
    )
    out = run_dom(
        "load(args.asset); return args.inputs.map(t => window.AutoformTexPlain.math(t));",
        args={"asset": str(_ASSET), "inputs": inputs},
    )
    reducer = json.loads(out)
    assert [(tex, read, tex_plain.math(tex)) for tex, read in zip(inputs, reducer) if read != tex_plain.math(tex)] == []


def test_braces_nested_past_pythons_stack_read_as_typed() -> None:
    # The port's own limit, which its docstring names: tex-plain.js still reads this, and no title nests so deep.
    deep = "{" * 3000 + "x" + "}" * 3000
    assert tex_plain.math(deep) == deep
