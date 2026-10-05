from __future__ import annotations

import pytest

from autoform_cli._lean_names import LeanNameError, parse_lean_name, render_lean_name_term


def test_parse_lean_name_preserves_surface_components() -> None:
    parts = parse_lean_name("Math.αβ₁.lookup!?.«quoted.name with space».0002.«».«003»")

    assert [(part.text, part.quoted, part.numeric) for part in parts] == [
        ("Math", False, False),
        ("αβ₁", False, False),
        ("lookup!?", False, False),
        ("quoted.name with space", True, False),
        ("0002", False, True),
        ("", True, False),
        ("003", True, False),
    ]


def test_render_lean_name_term_uses_structural_components() -> None:
    assert render_lean_name_term("Math.αβ₁.lookup!?") == (
        'Name.str (Name.str (Name.str (Name.anonymous) "Math") "αβ₁") "lookup!?"'
    )
    assert render_lean_name_term("Skel.«quoted.name with space».0002") == (
        'Name.num (Name.str (Name.str (Name.anonymous) "Skel") "quoted.name with space") 2'
    )
    assert render_lean_name_term("Skel.0000.«0002»") == (
        'Name.str (Name.num (Name.str (Name.anonymous) "Skel") 0) "0002"'
    )
    assert render_lean_name_term("Skel.«»") == 'Name.str (Name.str (Name.anonymous) "Skel") ""'


def test_render_lean_name_term_does_not_convert_large_numeric_components() -> None:
    digits = "9" * 5000

    assert render_lean_name_term(f"Skel.{digits}") == f'Name.num (Name.str (Name.anonymous) "Skel") {digits}'


@pytest.mark.parametrize(
    "name",
    (
        "",
        ".Root",
        "Root.",
        "Root..leaf",
        "Root.«unterminated",
        "Root.not quoted",
        "Root.λ",
        "Root.Π",
        "Root.Σ",
        "Root.中文",
        "Root.foo-",
        "Root.12abc",
        "Root.!suffix",
        "Root.₁suffix",
    ),
)
def test_parse_lean_name_rejects_invalid_surface_spellings(name: str) -> None:
    with pytest.raises(LeanNameError, match="invalid Lean name"):
        parse_lean_name(name)
