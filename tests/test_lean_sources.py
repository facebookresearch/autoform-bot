from __future__ import annotations

from pathlib import Path

from autoform_cli.lean import (
    Declaration,
    SourceLinker,
    declaration_names,
    index_project,
)

_SOURCE = """import Mathlib

namespace Outer

/-- A documented definition. -/
def alpha : Nat := 1

section Helpers

theorem beta : True := trivial

end Helpers

namespace Inner

@[simp]
protected noncomputable def gamma : Nat := 2

lemma delta : True := trivial

end Inner

end Outer

/-
namespace Ghost
theorem commented_out : True := trivial
end Ghost
-/

def toplevel : Nat := 3
"""


def _index(tmp_path: Path, text: str = _SOURCE, name: str = "Project/Basic.lean"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return index_project(tmp_path)


def test_qualifies_names_with_their_namespace(tmp_path: Path) -> None:
    index = _index(tmp_path)

    assert set(index.declarations) == {
        "Outer.alpha",
        "Outer.beta",
        "Outer.Inner.gamma",
        "Outer.Inner.delta",
        "toplevel",
    }


def test_preserves_duplicate_declaration_occurrences(tmp_path: Path) -> None:
    _index(tmp_path, "def duplicate : Nat := 1\n", "Blueprint/Draft.lean")
    index = _index(tmp_path, "def duplicate : Nat := 2\n", "Project/Production.lean")

    assert [declaration.path for declaration in index.occurrences["duplicate"]] == [
        Path("Blueprint/Draft.lean"),
        Path("Project/Production.lean"),
    ]


def test_records_the_declaring_line(tmp_path: Path) -> None:
    index = _index(tmp_path)

    assert index.find("Outer.alpha").line == 6
    assert index.find("Outer.alpha").path == Path("Project/Basic.lean")
    assert index.find("Outer.alpha").keyword == "def"


def test_sections_do_not_add_to_the_namespace(tmp_path: Path) -> None:
    index = _index(tmp_path)

    assert index.find("Outer.beta") is not None
    assert index.find("Outer.Helpers.beta") is None


def test_attributes_and_modifiers_do_not_hide_a_declaration(tmp_path: Path) -> None:
    assert _index(tmp_path).find("Outer.Inner.gamma") is not None


def test_public_modifier_does_not_hide_a_declaration(tmp_path: Path) -> None:
    assert _index(tmp_path, "public def visible : Nat := 1\n").find("visible") is not None


def test_commented_out_code_is_not_indexed(tmp_path: Path) -> None:
    index = _index(tmp_path)

    assert index.find("Ghost.commented_out") is None


def test_line_comments_are_ignored(tmp_path: Path) -> None:
    index = _index(tmp_path, "-- def notReal : Nat := 0\ndef real : Nat := 1\n")

    assert index.find("notReal") is None
    assert index.find("real") is not None


def test_build_output_is_skipped(tmp_path: Path) -> None:
    (tmp_path / ".lake/packages/mathlib").mkdir(parents=True)
    (tmp_path / ".lake/packages/mathlib/Vendored.lean").write_text(
        "def vendored : Nat := 0\n", encoding="utf-8"
    )
    index = _index(tmp_path)

    assert index.find("vendored") is None


def test_anonymous_instances_are_not_mistaken_for_names(tmp_path: Path) -> None:
    index = _index(tmp_path, "instance : Inhabited Nat := ⟨0⟩\n")

    assert index.declarations == {}


def test_declaration_names_splits_a_list() -> None:
    assert declaration_names("A.b, C.d  E.f") == ["A.b", "C.d", "E.f"]
    assert declaration_names("") == []


def test_permalink_pins_the_commit(tmp_path: Path) -> None:
    linker = SourceLinker(
        index=_index(tmp_path),
        repository_url="https://github.com/owner/repo",
        ref="deadbeef",
    )

    assert linker.url("Outer.alpha") == (
        "https://github.com/owner/repo/blob/deadbeef/Project/Basic.lean#L6"
    )
    assert linker.url("Outer.missing") is None

    duplicate = Declaration(
        "Outer.alpha", Path("Different/Alpha.lean"), 9, "def"
    )
    assert linker.declaration_url(duplicate) == (
        "https://github.com/owner/repo/blob/deadbeef/Different/Alpha.lean#L9"
    )


def test_no_link_without_repository_coordinates(tmp_path: Path) -> None:
    linker = SourceLinker(index=_index(tmp_path))

    assert linker.url("Outer.alpha") is None
    # The location is still known, so the page can still say where the code is.
    assert linker.location("Outer.alpha") is not None
