"""Every reader of Lean names in the package uses lean.py's one grammar.

The readers below are built on it and read each spelling one way, quoted
components and malformed spellings included. lean_probe and the generated
audit helper keep readers of their own, since neither may import the package:
lean_probe reads every name the grammar takes into the same components, and the
helper reads every spelling as lean_probe does. Lake's names, in
project/_lake_metadata.py, use the grammar's character classes under Lake's own
rules: a quoted component may hold «, and a component may be a number.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.elaboration_limits import _components
from autoform_cli.lean import (
    _ASCII_IDENTIFIER,
    _CONTINUATION_IDENTIFIER,
    _name_parts,
    _unquoted_parts,
)
from autoform_cli.lean_probe import SkeletonError, lean_name_components, lean_name_parts
from autoform_cli.library.citations import _drop_needless_quotes
from autoform_cli.library.index import _module_name
from autoform_cli.search import unquoted
from tests.audit_fixture import _load_helper

_NAMES = [
    line
    for line in (Path(__file__).parent / "fixtures/mathlib-names.txt").read_text(encoding="utf-8").splitlines()
    if line and not line.startswith("#")
]
#: An antiquotation the index reads in a Mathlib macro, not a name Mathlib declares.
_ANTIQUOTATION = "$typeName.instCommMonoid"
#: Quoted components Lean accepts that no Mathlib name has: empty, and holding a
#: space or a dot.
_QUOTED_SPELLINGS = ["Foo.«».bar", "«a b».c", "A.«b.c»"]
#: Names lean_probe reads and the grammar refuses (numeric components among
#: them), names both refuse, and more empty quoted components.
_PROBE_SPELLINGS = ["«»", "a.«»", "Foo.1", "_private.M.0.x", "«a«b»", "$x.y", "a-b", "Nat..add", "a."]

_READERS = {
    "lean._name_parts": _name_parts,
    "search.unquoted": unquoted,
    "audit's duplicate-target key": _unquoted_parts,
    "scopes a namespace opens": lambda name: _components(f" {name}"),
    "citations' needless quotes": _drop_needless_quotes,
    "module names": _module_name,
}


def test_the_grammar_takes_every_mathlib_name_and_quoted_components_without_a_guillemet() -> None:
    assert len(_NAMES) > 400
    assert [name for name in _NAMES if not _CONTINUATION_IDENTIFIER.fullmatch(name)] == [_ANTIQUOTATION]
    assert _CONTINUATION_IDENTIFIER.fullmatch("Foo.«».«a.b c».x₁'")
    assert not _CONTINUATION_IDENTIFIER.fullmatch("Foo.«a«b»")
    assert _ASCII_IDENTIFIER.fullmatch("Foo_1'!?") and not _ASCII_IDENTIFIER.fullmatch("Ω")


@pytest.mark.parametrize(
    ("reader", "spelling", "reading"),
    [
        ("lean._name_parts", "Foo.«a.b c».x", ["Foo", "«a.b c»", "x"]),
        ("lean._name_parts", "«a«b»", ["a", "«b»"]),
        ("search.unquoted", "_root_.Foo.«a.b c»", "Foo.a.b c"),
        ("search.unquoted", "«a«b»", "a.b"),
        ("search.unquoted", "a«b»c", "a.b.c"),
        ("search.unquoted", "«_root_».x", "x"),
        ("search.unquoted", "Nat.", "Nat"),
        ("search.unquoted", ".add", "add"),
        ("search.unquoted", "Nat..add", "Nat.add"),
        ("audit's duplicate-target key", "_root_.Foo.«a.b c»", ("Foo", "a.b c")),
        ("audit's duplicate-target key", "«a«b»", ("a", "b")),
        ("audit's duplicate-target key", "a«b»c", ("a", "b", "c")),
        ("scopes a namespace opens", "Foo.«».bar", 3),
        ("scopes a namespace opens", "_root_.Foo.«a.b c»", 3),
        ("scopes a namespace opens", "«a«b».c", 1),
        ("scopes a namespace opens", ".", 1),
        ("scopes a namespace opens", "4.2.", 1),
        # Not a scope Mathlib opens: none of the 72,902 scope lines in its sources is an antiquotation.
        ("scopes a namespace opens", _ANTIQUOTATION, 1),
        ("scopes a namespace opens", f"_root_.{_ANTIQUOTATION}", 1),
        ("citations' needless quotes", "Nat.«add».«a b»", "Nat.add.«a b»"),
        ("citations' needless quotes", "A.«b.c»", "A.«b.c»"),
        ("citations' needless quotes", "Foo.«».bar", "Foo.«».bar"),
        ("module names", "Mathlib.Algebra.Group", True),
        ("module names", "Mathlib.Nat'", True),
        ("module names", "Mathlib.«a b»", False),
        ("module names", "Real.ℝ", False),
        ("module names", "Foo.1", False),
    ],
)
def test_each_reader_reads_a_spelling_one_way(reader: str, spelling: str, reading: object) -> None:
    assert _READERS[reader](spelling) == reading


def test_dropping_needless_quotes_keeps_every_name() -> None:
    for name in [*_NAMES, *_QUOTED_SPELLINGS]:
        assert _unquoted_parts(_drop_needless_quotes(name)) == _unquoted_parts(name), name


def _probe_parts(name: str) -> tuple[tuple[str, bool], ...] | None:
    try:
        return lean_name_parts(name)
    except SkeletonError:
        return None


def test_lean_probe_reads_every_name_the_grammar_takes_into_the_same_components() -> None:
    for name in [*_NAMES, *_QUOTED_SPELLINGS, *_PROBE_SPELLINGS]:
        if _CONTINUATION_IDENTIFIER.fullmatch(name):
            parts = tuple((part[1:-1], True) if part[:1] == "«" else (part, False) for part in _name_parts(name))
            assert _probe_parts(name) == parts, name


def test_the_audit_helper_reads_declaration_names_as_lean_probe_does(repo_root: Path) -> None:
    helper = _load_helper(repo_root)
    for name in [*_NAMES, *_QUOTED_SPELLINGS, *_PROBE_SPELLINGS]:
        try:
            parts = helper._declaration_name_parts(name, name)
        except helper.AuditInputError:
            parts = None
        assert parts == _probe_parts(name), name
        if parts is not None:
            assert helper._json_declaration_name(name) == list(lean_name_components(name)), name
