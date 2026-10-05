from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from autoform_cli.lean import SourceLinker, declaration_names, index_project, strip_lean_comments


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


@pytest.mark.parametrize("git_entry", ["file", "directory"])
def test_nested_checkouts_are_not_indexed_as_project_source(
    tmp_path: Path, git_entry: str
) -> None:
    # A Git worktree or submodule has a .git file; a nested clone has a directory.
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / ".claude/worktrees/worker"
    (worktree / "Project").mkdir(parents=True)
    if git_entry == "file":
        (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    else:
        (worktree / ".git").mkdir()
    (worktree / "Project/Basic.lean").write_text(
        "def toplevel : Nat := 3\ndef workerOnly : Nat := 0\n", encoding="utf-8"
    )

    index = _index(tmp_path)

    assert index.find("toplevel").path == Path("Project/Basic.lean")
    assert index.find("workerOnly") is None


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs a non-root POSIX user, for whom a mode-0 directory is unreadable",
)
def test_unreadable_directories_do_not_abort_the_scan(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        index = _index(tmp_path)
    finally:
        locked.chmod(0o755)

    assert index.find("Outer.alpha") is not None


@pytest.mark.parametrize(
    "schema", ["autoform-skeleton-packets/v1", "autoform-skeleton-packets/v2"]
)
def test_managed_packet_output_is_not_indexed_as_project_source(
    tmp_path: Path, schema: str
) -> None:
    packets = tmp_path / "000-review-packets"
    packet = packets / "node" / "target.lean"
    packet.parent.mkdir(parents=True)
    packet.write_text("def target : Nat := 2\n", encoding="utf-8")
    (packets / "manifest.json").write_text(
        json.dumps({"kind": "packets", "packets": [], "schema": schema}) + "\n",
        encoding="utf-8",
    )

    index = _index(tmp_path, "def target : Nat := 1\n", name="Actual.lean")

    assert index.find("target").path == Path("Actual.lean")


def test_irreducible_definitions_are_indexed(tmp_path: Path) -> None:
    index = _index(tmp_path, "namespace A\nirreducible_def b : Nat := 1\nend A\n")

    assert index.find("A.b").keyword == "irreducible_def"


def test_anonymous_instances_are_not_mistaken_for_names(tmp_path: Path) -> None:
    index = _index(tmp_path, "instance : Inhabited Nat := ⟨0⟩\n")

    assert index.declarations == {}


def test_declaration_names_splits_a_list() -> None:
    assert declaration_names("A.b, C.d  E.f") == ["A.b", "C.d", "E.f"]
    assert declaration_names("") == []


def test_quoted_names_keep_spaces_and_dots_inside_one_component(tmp_path: Path) -> None:
    index = _index(tmp_path, "namespace A\ntheorem «b c.d» : True := trivial\nend A\n")

    assert declaration_names("A.«b c.d», X.y") == ["A.«b c.d»", "X.y"]
    assert index.find("A.«b c.d»") is not None


def test_comment_stripping_preserves_comment_markers_inside_strings() -> None:
    source = (
        'def a := "a--b /- c -/" -- remove me\n'
        'def b := r#"d--e /- f -/"# /- remove me -/\n'
        'def c := s!"value {"a--b"}" -- remove me\n'
        'def d := s!"brace {\'{\'} and {"a/-b"}" -- remove me\n'
        "def «e--f» : Char := '-'"
    )

    assert strip_lean_comments(source) == (
        'def a := "a--b /- c -/"\n'
        'def b := r#"d--e /- f -/"#\n'
        'def c := s!"value {"a--b"}"\n'
        'def d := s!"brace {\'{\'} and {"a/-b"}"\n'
        "def «e--f» : Char := '-'"
    )


def test_comment_stripping_reads_slash_dash_dash_slash_as_a_docstring() -> None:
    # Lean reads `/--` as a docstring opener, so `/--/ ... -/` is one comment.
    assert strip_lean_comments("/--/ KEEPOUT -/\ndef d : Nat := 6") == "def d : Nat := 6"
    assert strip_lean_comments("/-!/ KEEPOUT -/\ndef d : Nat := 6") == "def d : Nat := 6"


def test_double_brace_in_interpolation_opens_a_structure_instance(tmp_path: Path) -> None:
    # Lean has no `{{` escape: both braces open code, so these markers sit in a nested string.
    line = 'def s : String := s!"{{ fst := "--", snd := 1 : String × Nat }.fst}"'
    index = _index(tmp_path, line.replace("--", "/-") + "\ndef t : Nat := 1\n")

    assert strip_lean_comments(line + " -- remove me") == line
    assert index.find("t") is not None


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


def test_no_link_without_repository_coordinates(tmp_path: Path) -> None:
    linker = SourceLinker(index=_index(tmp_path))

    assert linker.url("Outer.alpha") is None
    # The location is still known, so the page can still say where the code is.
    assert linker.location("Outer.alpha") is not None


def test_a_name_defined_in_two_files_keeps_the_first_and_records_both(tmp_path: Path) -> None:
    _index(tmp_path, "namespace Demo\ntheorem main : True := trivial\nend Demo\n", "Demo/Main.lean")
    index = _index(tmp_path, "\ntheorem Demo.main : True := trivial\n", "Drafts/Main.lean")

    assert index.find("Demo.main").path == Path("Demo/Main.lean")
    assert [(item.path, item.line) for item in index.duplicates["Demo.main"]] == [
        (Path("Demo/Main.lean"), 2),
        (Path("Drafts/Main.lean"), 2),
    ]


def test_the_same_short_name_in_two_namespaces_is_not_a_duplicate(tmp_path: Path) -> None:
    index = _index(tmp_path, "namespace A\ndef x : Nat := 0\nend A\nnamespace B\ndef x : Nat := 1\nend B\n")

    assert set(index.declarations) == {"A.x", "B.x"}
    assert index.duplicates == {}


def test_private_names_collide_only_when_no_public_one_wins(tmp_path: Path) -> None:
    """Lean resolves a name to its public declaration; private ones elsewhere are distinct."""

    _index(tmp_path, "@[simp] private theorem helper : True := trivial\n", "A.lean")
    index = _index(tmp_path, "private theorem helper : True := trivial\n", "B.lean")
    assert index.find("helper").private
    assert [item.path for item in index.duplicates["helper"]] == [Path("A.lean"), Path("B.lean")]

    index = _index(tmp_path, "theorem helper : True := trivial\n", "B.lean")
    assert index.duplicates == {}
