"""Every reader of Lean names in the package uses lean.py's one grammar.

Until the readers shared it, seven modules held copies of the grammar that disagreed
at the edges. The copies are kept below as they were, and ``_DIFFERENCES``
lists every spelling where a reader now parts from its copy: on the other real
names in ``fixtures/mathlib-names.txt`` each reader gives what its copy gave.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from autoform_cli.elaboration_limits import _components
from autoform_cli.lean import (
    _ASCII_IDENTIFIER,
    _CONTINUATION_IDENTIFIER,
    _name_parts,
    _unquoted_parts,
)
from autoform_cli.library.citations import _drop_needless_quotes
from autoform_cli.library.index import _module_name
from autoform_cli.search import unquoted

_NAMES = [
    line
    for line in (Path(__file__).parent / "fixtures/mathlib-names.txt").read_text(encoding="utf-8").splitlines()
    if line and not line.startswith("#")
]


# The copies, as each reader held one before.
def _name_parts_copy(name: str) -> list[str]:
    return re.findall(r"«[^»]*»|[^.«»]+", name)


def _unquoted_copy(name: str) -> str:
    # search.unquoted's, which library/declarations.py also used for its spelled names.
    return name.removeprefix("_root_.").replace("«", "").replace("»", "")


def _duplicate_key_copy(name: str) -> tuple[str, ...]:
    parts = [p[1:-1] if p[:1] == "«" and p[-1:] == "»" else p for p in re.findall(r"(?:«[^«»]*»|[^.«»])+", name)]
    return tuple(parts[1:] if len(parts) > 1 and parts[0] == "_root_" else parts)


def _scope_components_copy(text: str) -> int:
    name = re.match(r"(?:«[^»]*»|[^\s«»])+", text.lstrip())
    return len(re.findall(r"«[^»]*»|[^.«»\s]+", name.group())) if name else 1


def _needless_quotes_copy(name: str) -> str:
    def plain(part: re.Match[str]) -> str:
        return part[1] if "." not in part[1] and _CONTINUATION_IDENTIFIER.fullmatch(part[1]) else part[0]

    return re.sub(r"«([^«»]*)»", plain, name) if "«" in name else name


def _module_name_copy(name: str) -> bool:
    # impact.py's and library/index.py's.
    return all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'!?]*", part) for part in name.split("."))


_READERS = {
    "lean._name_parts": (_name_parts, _name_parts_copy),
    "search.unquoted": (unquoted, _unquoted_copy),
    "audit's duplicate-target key": (_unquoted_parts, _duplicate_key_copy),
    "scopes a namespace opens": (lambda name: _components(f" {name}"), lambda name: _scope_components_copy(f" {name}")),
    "citations' needless quotes": (_drop_needless_quotes, _needless_quotes_copy),
    "module names": (_module_name, _module_name_copy),
}

#: (reader, spelling, what its copy gave, what it gives now).
_DIFFERENCES = [
    ("lean._name_parts", "«a«b»", ["«a«b»"], ["a", "«b»"]),
    ("search.unquoted", "«a«b»", "ab", "a.b"),
    ("search.unquoted", "a«b»c", "abc", "a.b.c"),
    ("search.unquoted", "«_root_».x", "_root_.x", "x"),
    ("search.unquoted", "Nat.", "Nat.", "Nat"),
    ("search.unquoted", ".add", ".add", "add"),
    ("search.unquoted", "Nat..add", "Nat..add", "Nat.add"),
    ("audit's duplicate-target key", "«a«b»", ("a«b»",), ("a", "b")),
    ("audit's duplicate-target key", "a«b»c", ("a«b»c",), ("a", "b", "c")),
    ("scopes a namespace opens", "«a«b».c", 2, 1),
    ("scopes a namespace opens", ".", 0, 1),
    ("scopes a namespace opens", "4.2.", 2, 1),
    # An antiquotation the index reads in a Mathlib macro, not a scope Mathlib
    # opens: none of the 72,902 scope lines in its sources counts differently.
    ("scopes a namespace opens", "$typeName.instCommMonoid", 2, 1),
    ("scopes a namespace opens", "_root_.$typeName.instCommMonoid", 3, 1),
]


def test_the_grammar_takes_empty_and_spaced_quoted_components_but_no_guillemet_inside_one() -> None:
    assert _CONTINUATION_IDENTIFIER.fullmatch("Foo.«».«a.b c».x₁'")
    assert not _CONTINUATION_IDENTIFIER.fullmatch("Foo.«a«b»")
    assert _unquoted_parts("_root_.Foo.«a.b c»") == ("Foo", "a.b c")
    assert _ASCII_IDENTIFIER.fullmatch("Foo_1'!?") and not _ASCII_IDENTIFIER.fullmatch("Ω")


@pytest.mark.parametrize("reader", _READERS)
def test_each_reader_reads_every_real_name_as_its_copy_did(reader: str) -> None:
    now, copy = _READERS[reader]
    names = [*_NAMES, *(f"_root_.{name}" for name in _NAMES)]

    listed = sorted(spelling for row, spelling, _before, _after in _DIFFERENCES if row == reader and spelling in names)

    assert len(_NAMES) > 400
    assert sorted(name for name in names if now(name) != copy(name)) == listed


@pytest.mark.parametrize(("reader", "spelling", "before", "after"), _DIFFERENCES)
def test_where_a_reader_parts_from_its_copy(reader: str, spelling: str, before: object, after: object) -> None:
    now, copy = _READERS[reader]

    assert (copy(spelling), now(spelling)) == (before, after)
