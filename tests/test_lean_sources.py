from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from autoform_cli import _directory_binding as directory_binding_module
from autoform_cli import _tree_snapshot as tree_snapshot_module
from autoform_cli import lean as lean_module
from autoform_cli._tree_snapshot import (
    BoundDirectoryTree,
    TreeCaptureLimits,
    TreeChangedError,
    TreeSelection,
    TreeSnapshotError,
    bind_directory_tree,
)
from autoform_cli.lean import (
    Alias,
    SourceLinker,
    declaration_names,
    detect_ref,
    detect_repository_url,
    index_project,
    open_project_sources,
    repository_top_level,
    snapshot_project_sources,
    strip_lean_comments,
    verify_repository_snapshot,
)
from autoform_cli.skeleton import _output_identity, _replace_outputs

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


def _require_directory_descriptors() -> None:
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")


def _require_descriptor_capture() -> None:
    if not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED
    ):
        pytest.skip("directory descriptor capture is unavailable")


def _use_portable_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False)


def _index(tmp_path: Path, text: str = _SOURCE, name: str = "Project/Basic.lean"):
    return _index_files(tmp_path, {name: text})


def _index_files(tmp_path: Path, files: dict[str, str]):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return index_project(tmp_path)


def _on_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    action: Callable[[], None],
    relative: str | None = None,
) -> None:
    """Run `action` at each tree snapshot checkpoint named `event`, optionally only at `relative`."""
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint

    def checkpoint(name: str, path: str) -> None:
        original_checkpoint(name, path)
        if name == event and (relative is None or path == relative):
            action()

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", checkpoint)


def _count_binds(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Record the arguments of every `open_project_sources` call, one per capture attempt."""
    calls: list[tuple] = []
    original_open = lean_module.open_project_sources

    def counted_open(*args, **kwargs):
        calls.append(args)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(lean_module, "open_project_sources", counted_open)
    return calls


def _capture_through(entry: str, root: Path):
    """Capture Lean sources through one of the two public retrying entry points."""
    if entry == "snapshot":
        return snapshot_project_sources(root)
    with lean_module.bind_project_source_snapshot(root) as (_bound, snapshot):
        return snapshot


def _changed_path(root: Path) -> str | None:
    """Capture once without retrying; return the path the TreeChangedError names."""
    sources = open_project_sources(root)
    try:
        with pytest.raises(TreeChangedError) as error:
            sources.capture()
    finally:
        sources.close()
    return error.value.relative


def _snapshot_in_one_attempt(monkeypatch: pytest.MonkeyPatch, root: Path):
    """Snapshot through the retrying entry point and require a single attempt."""
    attempts = _count_binds(monkeypatch)
    snapshot = snapshot_project_sources(root)
    assert len(attempts) == 1
    return snapshot


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


@pytest.mark.parametrize(
    ("keyword", "body"),
    [
        ("theorem", ": True := trivial"),
        ("lemma", ": True := trivial"),
        ("def", ": Nat := 7"),
        ("abbrev", ": Nat := 7"),
        ("opaque", ": Nat := 7"),
        ("axiom", ": True"),
        ("instance", ": Inhabited Nat := ⟨7⟩"),
        ("structure", "where\n  value : Nat"),
    ],
)
def test_declaration_identifier_can_follow_its_keyword_on_the_next_line(
    tmp_path: Path, keyword: str, body: str
) -> None:
    source = f"import Mathlib\nnamespace Outer\n{keyword}\n  result {body}\nend Outer\n"
    index = _index(tmp_path, source)

    assert set(index.declarations) == {"Outer.result"}
    declaration = index.find("Outer.result")
    assert declaration.line == 3
    assert declaration.keyword == keyword
    assert index.line_counts == {Path("Project/Basic.lean"): len(source.splitlines())}


def test_comments_between_keyword_and_quoted_name_preserve_namespace_and_modifiers(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "namespace Outer.Inner",
            "@[simp] protected",
            "theorem -- its identifier follows the comment",
            "/- namespace Ghost",
            "   /- theorem hidden : True := trivial -/",
            "   #exit",
            "end Ghost -/",
            "  «with -- and /- and .» : True := trivial",
            "end Outer.Inner",
            "theorem afterwards : True := trivial",
        ]
    )
    index = _index(tmp_path, source)

    assert set(index.declarations) == {"Outer.Inner.«with -- and /- and .»", "afterwards"}
    declaration = index.find("Outer.Inner.«with -- and /- and .»")
    assert declaration.line == 3
    assert declaration.protected


def test_multiline_literals_do_not_supply_declaration_names_or_namespaces(tmp_path: Path) -> None:
    source = '\n'.join(
        [
            'def message : String := r#"',
            'namespace Ghost',
            'theorem',
            '  imaginary : True := trivial',
            'end Ghost',
            '"#',
            'namespace Real',
            'theorem',
            '  actual : True := trivial',
            'end Real',
        ]
    )
    index = _index(tmp_path, source)

    assert set(index.declarations) == {"message", "Real.actual"}
    assert index.find("Real.actual").line == 8


def test_declaration_scan_view_preserves_coordinates_and_original_literal_bytes(tmp_path: Path) -> None:
    source = 'def «name -- /-» : String := r#"α\nnamespace Ghost\ntheorem\n  imaginary : True"#\n'
    stripped = lean_module._without_lean_comments(source)
    scan_view = lean_module._without_lean_comments(source, mask_literals=True)
    snapshot = snapshot_project_sources(_index(tmp_path, source).root)

    assert stripped == source
    assert len(scan_view) == len(stripped)
    assert [i for i, char in enumerate(scan_view) if char == "\n"] == [
        i for i, char in enumerate(stripped) if char == "\n"
    ]
    assert "«name -- /-»" in scan_view
    assert "namespace Ghost" not in scan_view
    assert snapshot.source_files == ((Path("Project/Basic.lean"), source.encode("utf-8")),)


@pytest.mark.parametrize(
    ("source", "masked"),
    [
        ('def s := "ab" ++ t', "def s :=      ++ t"),
        ("def c := 'x' ++ t", "def c :=     ++ t"),
        ('def s := "a\\"b" ++ t', "def s :=        ++ t"),
        ('def s := s!"a{x}b" ++ t', "def s := s!        ++ t"),
        ('def s := r#"a"# ++ t', "def s :=        ++ t"),
        ('def «a b» := "c"', "def «a b» :=    "),
    ],
)
def test_masking_blanks_each_literal_through_its_closer(source: str, masked: str) -> None:
    # The closer is appended before its frame pops; appended after, it would
    # stay visible while the rest of its literal is blanked.
    assert lean_module._without_lean_comments(source, mask_literals=True) == masked


def test_alias_identifier_and_target_can_each_follow_on_the_next_line(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "import Batteries.Tactic.Alias",
            "namespace Outer",
            "theorem base : True := trivial",
            "protected",
            "alias",
            "  renamed :=",
            "    base",
            "end Outer",
        ]
    )
    index = _index(tmp_path, source)

    declaration = index.find("Outer.renamed")
    assert declaration.line == 5
    assert declaration.protected
    assert declaration.alias == Alias(("Outer.base", "base"), atomic=True, iff=False)
    assert index.reached(declaration) == index.find("Outer.base")


def test_iff_alias_pair_can_follow_its_keyword_on_the_next_line(tmp_path: Path) -> None:
    source = (
        "import Batteries.Tactic.Alias\nnamespace Outer\ntheorem base : True ↔ True := Iff.rfl\n"
        "protected\nalias\n  ⟨forward, backward⟩ :=\n    base\nend Outer\n"
    )
    index = _index(tmp_path, source)

    assert set(index.declarations) == {"Outer.base", "Outer.forward", "Outer.backward"}
    for name in ("Outer.forward", "Outer.backward"):
        declaration = index.find(name)
        assert declaration.line == 5
        assert declaration.protected
        assert declaration.alias == Alias(("Outer.base", "base"), atomic=True, iff=True)


def test_unfinished_keyword_does_not_take_the_next_command_as_its_name(tmp_path: Path) -> None:
    index = _index(tmp_path, "namespace Outer\ntheorem\ndef actual : Nat := 7\nend Outer\n")

    assert set(index.declarations) == {"Outer.actual"}

    stopped = _index(tmp_path, "theorem\n#exit\ndef unread : Nat := 7\n")
    assert stopped.declarations == {}
    assert stopped.line_counts == {Path("Project/Basic.lean"): 1}


@pytest.mark.parametrize(
    "command",
    [
        "#check Nat",
        "#print Nat",
        "#unrecognized_command",
        "open Nat",
        "variable (x : Nat)",
        "set_option pp.universes true",
        "attribute [simp] Nat.add_zero",
        "export Nat (succ)",
        "universe u",
        "include x",
        "omit x",
        "syntax \"custom\" : command",
        "example : True := trivial",
        "initialize value : Nat ← pure 7",
        "run_cmd pure ()",
        "run_elab do pure ()",
        "run_meta pure ()",
        'local notation "X" => Nat',
        'scoped notation "X" => Nat',
        "public import Mathlib",
        "public meta import Mathlib",
        "@[deprecated] initialize value : Nat ← pure 7",
        "private unsafe initialize value : Nat ← pure 7",
        '@[simp] scoped postfix:70 "!" => Nat.succ',
        'local prefix:70 "!" => Nat.succ',
        'macro_rules | `(custom) => `(Nat)',
        'elab_rules : command | `(custom) => pure ()',
        "assert_not_exists absent",
        "assert_not_imported Mathlib",
        "deprecated_module",
        "deprecated_syntax syntaxName",
        "docs_to_verso Nat",
        "init_quot",
        "unlock_limits",
        'register_tactic_tag tag "description"',
        "tactic_extension tag",
        'recommended_spelling "and" for "∧" in [And]',
        "gen_injective_theorems% Nat",
        "with_weak_namespace Nat #check Nat",
        "register_error_explanation code explanation",
        "set_library_suggestions ()",
        "seal Nat.add",
        "unseal Nat.add",
        'binder_predicate x " > " y:term => `($x > $y)',
        "aux_def value : Nat := 7",
        "norm_cast_add_elim Nat.add_zero",
        "register_linter_set checks := linter.unusedVariables",
        "register_label_attr label",
        "register_sym_simp_attr rules",
        "ensure_eval_term_instance Nat",
        "declare_core_config_elab elabName Config",
        "simproc_decl simplify (Nat.succ _) := fun _ => pure .continue",
        "cbv_simproc_decl simplify (Nat.succ _) := fun _ => pure .continue",
        "unif_hint (x : Nat) where ⊢ x ≟ x",
        "register_sym_simp simpVariant where\n  maxSteps := 100",
        "grind_pattern lemmaName => Nat.succ",
    ],
)
def test_unfinished_keyword_does_not_take_a_nondeclaration_command_name(tmp_path: Path, command: str) -> None:
    source = f"namespace Outer\ntheorem\n{command}\ndef actual : Nat := 7\nend Outer\n"
    index = _index(tmp_path, source)

    assert set(index.declarations) == {"Outer.actual"}
    assert index.find("Outer.actual").line == 3 + len(command.splitlines())


@pytest.mark.parametrize(
    "name",
    [
        "open'", "open!", "public?", "variable₁", "variable'", "attribute'", "set_option'", "run_cmd'", "run_elab'",
        "local'", "scoped'", "public'", "initialize'", "requires", "ensures",
        "«open»", "«#check»", "«run_cmd»", "«local»", "«@[deprecated]»",
        "Qualified.open", "Qualified.local", "Qualified.run_elab", "«Qualified.open».value",
        "α₁", "ℕ_value", "𝒜_value",
    ],
)
def test_multiline_names_are_not_rejected_by_a_command_token_prefix(tmp_path: Path, name: str) -> None:
    source = f"namespace Outer\ndef\n  {name} : Nat := 7\ntheorem actual : True := trivial\nend Outer\n"
    index = _index(tmp_path, source)

    assert set(index.declarations) == {f"Outer.{name}", "Outer.actual"}
    assert index.find(f"Outer.{name}").line == 2


@pytest.mark.parametrize(
    "following",
    [
        "@decoration",
        "not_a_name/part : Nat := 7",
        "not_a_name+part : Nat := 7",
        "123 : Nat := 7",
        "custom => Nat",
        "custom do pure ()",
        "custom binders => Nat",
        "custom (x : Nat) => Nat",
        "custom (x : Nat] : Nat := 7",
    ],
)
def test_multiline_continuation_requires_identifier_and_signature_syntax(tmp_path: Path, following: str) -> None:
    source = f"namespace Outer\ntheorem\n{following}\ndef actual : Nat := 7\nend Outer\n"
    index = _index(tmp_path, source)

    assert set(index.declarations) == {"Outer.actual"}
    assert index.find("Outer.actual").line == 4


@pytest.mark.parametrize(
    ("keyword", "name", "signature"),
    [
        ("def", "identity", "x : Nat := x"),
        ("def", "identity", "{α : Type u} (x : α) : α := x"),
        ("def", "identity", "{α : Type u}\n    (x : α) : α := x"),
        ("def", "identity", "{α : Type u}\n    [Inhabited α] : α := default"),
        ("theorem", "self_eq", "(x : Nat\n    ) : x = x := rfl"),
        ("theorem", "self_eq", "(«x)» : Nat) : «x)» = «x)» := rfl"),
        ("def", "identity", ".{u} {α : Type u} (x : α) := x"),
        ("def", "zero", ":= 0"),
        ("def", "zero", "\n    : Nat := 0"),
        ("def", "isZero", "| 0 => true\n    | _ => false"),
        ("structure", "Child", "extends Parent where\n    value : Nat"),
    ],
)
def test_multiline_declaration_names_keep_legal_signature_continuations(
    tmp_path: Path, keyword: str, name: str, signature: str
) -> None:
    separator = "" if signature.startswith(".{") else " "
    source = (
        f"namespace Outer\n@[simp] protected\n{keyword}\n  {name}{separator}{signature}\n"
        "theorem actual : True := trivial\nend Outer\n"
    )
    index = _index(tmp_path, source)

    assert set(index.declarations) == {f"Outer.{name}", "Outer.actual"}
    declaration = index.find(f"Outer.{name}")
    assert declaration.line == 3
    assert declaration.keyword == keyword
    assert declaration.protected


def test_decorated_declaration_after_unfinished_keyword_keeps_its_own_line(tmp_path: Path) -> None:
    source = (
        "namespace Outer\ntheorem\n@[simp] protected def actual : Nat := 7\n"
        "@[simp] protected\ntheorem\n  next : True := trivial\nend Outer\n"
    )
    index = _index(tmp_path, source)

    assert set(index.declarations) == {"Outer.actual", "Outer.next"}
    assert index.find("Outer.actual").line == 3
    assert index.find("Outer.actual").keyword == "def"
    assert index.find("Outer.actual").protected
    assert index.find("Outer.next").line == 5
    assert index.find("Outer.next").keyword == "theorem"
    assert index.find("Outer.next").protected


def test_anonymous_multiline_instance_does_not_become_a_named_declaration(tmp_path: Path) -> None:
    source = "instance\n  : Inhabited Nat := ⟨7⟩\ntheorem\n  actual : True := trivial\n"
    index = _index(tmp_path, source)

    assert set(index.declarations) == {"actual"}
    assert index.find("actual").line == 3


def test_sections_do_not_add_to_the_namespace(tmp_path: Path) -> None:
    index = _index(tmp_path)

    assert index.find("Outer.beta") is not None
    assert index.find("Outer.Helpers.beta") is None


def test_the_scan_indexes_every_declaration_form(tmp_path: Path) -> None:
    source = [
        "universe u v w",
        "namespace Outer",
        # A name on the line after its keyword, with and without trailing space.
        "theorem",
        "  nextLine : True := trivial",
        "lemma ",
        "  spaced : True := trivial",
        "def polymorphic.{u} (α : Type u) := α",
        "theorem «dotted.name» : True := trivial",
        "theorem _root_.rootLevel.{v} : True := trivial",
        "protected def guarded.{w} : Nat := 0",
        "theorem base_iff : True ↔ True := Iff.rfl",
        "alias ⟨forward, _⟩ := base_iff",
        "alias later :=",
        "  base_iff",
        "alias",
        "  renamed := base_iff",
        "@[simp]",
        "theorem attributed : True := trivial",
        "/-- A docstring on the declaration's own line. -/ theorem documented : True := trivial",
        "theorem_wanted wish : True",
        "proof_wanted proofWish : True",
        "def_wanted defWish : Nat",
        "instance_wanted instWish : Inhabited Nat",
        "@[simp]",
        "private",
        "noncomputable def split : Nat := 0",
        "end Outer",
    ]
    index = _index(tmp_path, "\n".join(source) + "\n")

    assert sorted(index.declarations) == [
        *(f"Outer.{name}" for name in ("attributed", "base_iff", "defWish", "documented", "forward", "guarded")),
        *(f"Outer.{name}" for name in ("instWish", "later", "nextLine", "polymorphic", "proofWish", "renamed")),
        *(f"Outer.{name}" for name in ("spaced", "split", "wish")),
        "Outer.«dotted.name»",
        "rootLevel",
    ]


def test_every_scope_closes_at_its_own_end(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "namespace Outer",
            "noncomputable section",
            "theorem a : True := trivial",
            "end",
            "theorem b : True := trivial",
            "@[expose] public section",
            "theorem c : True := trivial",
            "end",
            "public meta section",
            "end",
            "mutual",
            "def even : Nat → Bool",
            "  | 0 => true",
            "  | n + 1 => odd n",
            "def odd : Nat → Bool",
            "  | 0 => false",
            "  | n + 1 => even n",
            "end",
            "theorem d : True := trivial",
            "end Outer",
        ]
    )
    index = _index(tmp_path, source)

    names = ["Outer.a", "Outer.b", "Outer.c", "Outer.even", "Outer.odd", "Outer.d"]
    assert sorted(index.declarations) == sorted(names)


def test_attributes_and_modifiers_do_not_hide_a_declaration(tmp_path: Path) -> None:
    assert _index(tmp_path).find("Outer.Inner.gamma") is not None


def test_public_sections_and_declarations_are_indexed_without_losing_namespace(
    tmp_path: Path,
) -> None:
    index = _index(
        tmp_path,
        "namespace Outer\npublic section\npublic theorem visible : True := trivial\n"
        "end\ntheorem after : True := trivial\nend Outer\n",
    )

    assert index.find("Outer.visible") is not None
    assert index.find("Outer.after") is not None


def test_universe_binders_are_not_part_of_the_indexed_declaration_name(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path, "universe u\ndef polymorphic.{u} (α : Type u) := α\n")

    assert index.find("polymorphic") is not None
    assert index.find("polymorphic.{u}") is None


def test_commented_out_code_is_not_indexed(tmp_path: Path) -> None:
    index = _index(tmp_path)

    assert index.find("Ghost.commented_out") is None
    # The comment's newlines are kept, so the lines below it keep their numbers.
    assert index.find("toplevel").line == 31


def test_nothing_after_exit_is_indexed(tmp_path: Path) -> None:
    index = _index(tmp_path, "def real : Nat := 1\n#exit\ndef dead : Nat := 2\n")

    assert index.find("real") is not None
    assert index.find("dead") is None
    # Audit sizes the last declaration up to here, not through the dead tail.
    assert index.line_counts == {Path("Project/Basic.lean"): 1}


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


def test_case_distinct_build_namespace_is_indexed(tmp_path: Path) -> None:
    index = _index(tmp_path, "theorem buildResult : True := trivial\n", "Build/Actual.lean")

    assert index.find("buildResult") is not None


def test_lowercase_build_directories_are_skipped_at_any_depth(tmp_path: Path) -> None:
    for relative, name in (
        ("build/Skipped.lean", "topSkipped"),
        ("Nested/build/Skipped.lean", "nestedSkipped"),
        ("Other/Build/Kept.lean", "kept"),
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_text(f"def {name} : Nat := 0\n", encoding="utf-8")

    index = index_project(tmp_path)

    assert index.find("topSkipped") is None
    assert index.find("nestedSkipped") is None
    assert index.find("kept") is not None


def test_hidden_lean_source_directories_are_indexed(tmp_path: Path) -> None:
    hidden = tmp_path / ".proofs"
    hidden.mkdir()
    (hidden / "Hidden.lean").write_text("def hiddenProof : Nat := 0\n", encoding="utf-8")

    assert index_project(tmp_path).find("hiddenProof") is not None


@pytest.mark.parametrize("parent", ["", "Nested/"])
@pytest.mark.parametrize(
    "directory",
    [".direnv", ".git", ".lake", ".obsidian", ".trash", ".venv", ".GIT", ".LAKE", ".ObSiDiAn"],
)
def test_known_tooling_directories_are_not_scanned(
    tmp_path: Path,
    directory: str,
    parent: str,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    ignored = tmp_path / parent / directory
    ignored.mkdir(parents=True)
    (ignored / "Leak.lean").write_text("def leaked : Nat := 0\n", encoding="utf-8")

    index = index_project(tmp_path)

    assert index.find("canonical") is not None
    assert index.find("leaked") is None


def test_changes_inside_an_excluded_build_directory_do_not_invalidate_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_descriptor_capture()
    _index(tmp_path, "def canonical : Nat := 0\n")
    build_state = tmp_path / ".lake" / "build-state"
    build_state.parent.mkdir()
    build_state.write_text("old\n", encoding="utf-8")
    changed = False

    def change_excluded_file() -> None:
        nonlocal changed
        if not changed:
            changed = True
            build_state.write_text("new\n", encoding="utf-8")

    _on_checkpoint(monkeypatch, "before-final-verification", change_excluded_file)
    sources = open_project_sources(tmp_path)
    try:
        snapshot = sources.capture()
    finally:
        sources.close()

    assert changed
    assert snapshot.index.find("canonical") is not None


def test_closed_source_binding_rejects_later_capture(
    tmp_path: Path,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    sources = open_project_sources(tmp_path)

    sources.close()

    with pytest.raises(TreeSnapshotError, match="closed"):
        sources.capture()


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_direct_snapshot_names_a_stable_invalid_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_kind: str,
) -> None:
    root = tmp_path / "root"
    if root_kind == "file":
        root.write_text("not a directory\n")
    attempts = _count_binds(monkeypatch)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)
    reason = (
        "directory root does not exist"
        if root_kind == "missing"
        else "directory root is not a directory"
    )

    with pytest.raises(lean_module.LeanSourceError, match=reason):
        snapshot_project_sources(root)

    assert len(attempts) == lean_module._SNAPSHOT_ATTEMPTS


def test_index_project_keeps_supporting_a_symlinked_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _index(project, "def retainedCompatibility : Nat := 0\n")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    index = index_project(alias)

    assert index.root == project.resolve()
    assert index.find("retainedCompatibility") is not None


def test_symlinked_root_preserves_an_absolute_descendant_exclusion(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _index(project, "def kept : Nat := 0\n", "Keep.lean")
    generated = project / "generated"
    generated.mkdir()
    (generated / "Leak.lean").write_text("def leaked : Nat := 0\n", encoding="utf-8")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    index = index_project(alias, exclude_roots=(alias / "generated",))

    assert index.find("kept") is not None
    assert index.find("leaked") is None


def test_source_binding_rejects_root_replacement(tmp_path: Path) -> None:
    _require_directory_descriptors()
    project = tmp_path / "project"
    _index(project, "def original : Nat := 0\n")
    sources = open_project_sources(project)
    displaced = tmp_path / "displaced"
    try:
        try:
            project.rename(displaced)
        except OSError:
            pytest.skip("open directory replacement is unavailable")
        project.mkdir()
        _index(project, "def replacement : Nat := 0\n")

        with pytest.raises(TreeSnapshotError, match="changed while it was in use"):
            sources.capture()
    finally:
        sources.close()


def test_source_capture_rejects_mid_capture_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_descriptor_capture()
    source = tmp_path / "Project" / "Basic.lean"
    _index(tmp_path, "def before : Nat := 0\n")
    changed = False

    def change_source() -> None:
        nonlocal changed
        if not changed:
            changed = True
            source.write_text("def after : Nat := 1000\n", encoding="utf-8")

    _on_checkpoint(monkeypatch, "before-final-verification", change_source)

    sources = open_project_sources(tmp_path)
    try:
        with pytest.raises(TreeChangedError, match="changed while it was captured") as error:
            sources.capture()
    finally:
        sources.close()

    assert changed
    assert error.value.relative == "Project/Basic.lean"


def test_snapshot_retries_a_capture_that_races_one_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Project" / "Basic.lean"
    _index(tmp_path, "def before : Nat := 0\n")
    changed = False

    def change_source_once() -> None:
        nonlocal changed
        if not changed:
            changed = True
            source.write_text("def after : Nat := 1000\n", encoding="utf-8")

    _on_checkpoint(monkeypatch, "before-final-verification", change_source_once)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)

    snapshot = snapshot_project_sources(tmp_path)

    assert changed
    assert snapshot.index.find("after") is not None
    assert snapshot.index.find("before") is None


@pytest.mark.parametrize(
    ("entry", "edited", "unattributed_first_open", "named"),
    [
        ("snapshot", "B", False, "Project/B.lean"),
        ("bind", "B", False, "Project/B.lean"),
        # Each changed path is named once, in the order the attempts saw it.
        ("snapshot", "AB", False, "Project/A.lean, Project/B.lean"),
        # An attempt whose change names no path adds nothing to the list.
        ("snapshot", "B", True, "Project/B.lean"),
    ],
    ids=["snapshot", "bind", "two-paths", "unattributed"],
)
def test_snapshot_reports_sources_that_keep_changing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    edited: str,
    unattributed_first_open: bool,
    named: str,
) -> None:
    _require_descriptor_capture()
    _index(tmp_path, "def a : Nat := 0\n", "Project/A.lean")
    _index(tmp_path, "def b : Nat := 0\n", "Project/B.lean")
    edits = 0
    opens = 0
    original_open = lean_module.open_project_sources

    def open_sources(*args, **kwargs):
        nonlocal opens
        opens += 1
        if unattributed_first_open and opens == 1:
            raise TreeChangedError("directory tree changed before it was captured")
        return original_open(*args, **kwargs)

    def change_source() -> None:
        nonlocal edits
        name = edited[min(edits, len(edited) - 1)]
        edits += 1
        # Each edit also changes the size, so coarse timestamps cannot hide it.
        (tmp_path / "Project" / f"{name}.lean").write_text(
            f"def churn : Nat := {'1' * (edits + 1)}\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(lean_module, "open_project_sources", open_sources)
    _on_checkpoint(monkeypatch, "before-final-verification", change_source)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)

    with pytest.raises(lean_module.LeanSourceError, match="Lean sources kept changing while they were indexed") as error:
        _capture_through(entry, tmp_path)

    assert opens == lean_module._SNAPSHOT_ATTEMPTS
    assert edits == lean_module._SNAPSHOT_ATTEMPTS - unattributed_first_open
    assert str(error.value) == f"Lean sources kept changing while they were indexed; changed: {named}"


def _at(monkeypatch: pytest.MonkeyPatch, when: str, action: Callable[[], object]) -> list[str]:
    """Run `action` once at the capture moment `when`; the returned list records that it ran.

    `when` is "verify" (the checkpoint before the final verification), "list F"
    (the checkpoint after the scan lists folder F; "list " is the root),
    "relist F" (just before folder F's end-of-scan relist, not verification's), or "open N after W"
    or "opendir N after W" (just before the first descriptor-relative open of N
    as a file or as a folder once moment W has passed).
    """
    fired: list[str] = []

    def once() -> None:
        if not fired:
            fired.append(when)
            action()

    kind, _, rest = when.partition(" ")
    if kind == "verify":
        _on_checkpoint(monkeypatch, "before-final-verification", once)
    elif kind == "list":
        _on_checkpoint(monkeypatch, "after-directory-list", once, relative=rest)
    elif kind == "relist":
        original_recheck = tree_snapshot_module._recheck_scanned_directory

        def recheck(descriptor, *, relative: str, **kwargs):
            # Verification relists through the same function; only the scan's
            # call, which passes no verify_kept, is the end-of-scan relist.
            if relative == rest and kwargs.get("verify_kept") is None:
                once()
            return original_recheck(descriptor, relative=relative, **kwargs)

        monkeypatch.setattr(tree_snapshot_module, "_recheck_scanned_directory", recheck)
    else:
        name, _, after = rest.partition(" after ")
        armed = _at(monkeypatch, after, lambda: None)
        original_open = os.open

        def open_after(path, flags, mode=0o777, *, dir_fd=None):
            directory = bool(flags & getattr(os, "O_DIRECTORY", 0))
            if armed and path == name and dir_fd is not None and directory == (kind == "opendir"):
                once()
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", open_after)
    return fired


def _write_site(site: Path, generation: int) -> None:
    (site / "assets").mkdir(parents=True, exist_ok=True)
    (site / "assets" / "a.css").write_text(f"/* {generation} */\n", encoding="utf-8")
    (site / "index.html").write_text(f"<p>{generation}</p>\n", encoding="utf-8")


_PACKETS_MANIFEST = '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'


def _packets_output(folder: Path, revision: int) -> None:
    folder.mkdir()
    (folder / "manifest.json").write_text(_PACKETS_MANIFEST, encoding="utf-8")
    (folder / "Packet.lean").write_text(f"def generated{revision} : Nat := 0\n", encoding="utf-8")


def _clone(worker: Path, revision: int) -> None:
    (worker / ".git").mkdir(parents=True)
    (worker / ".git/HEAD").write_text(f"ref: refs/heads/r{revision}\n", encoding="utf-8")
    (worker / "Project").mkdir()
    (worker / "Project/Worker.lean").write_text(f"def nestedWorker{revision} : Nat := 0\n", encoding="utf-8")


def _lean(path: Path, name: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"def {name} : Nat := 0\n", encoding="utf-8")


def _lock_link(path: Path, number: int = 1) -> Path:
    path.unlink(missing_ok=True)
    try:
        path.symlink_to(f"user@host.{number}:1700000000")
    except OSError:
        pytest.skip("symlinks are unavailable")
    return path


def _notes_folder(path: Path) -> None:
    path.mkdir()
    (path / "notes.txt").write_text("notes\n", encoding="utf-8")


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _replace(path: Path, make: Callable[[Path], object]) -> None:
    _remove(path)
    make(path)


def _clean_site(site: Path) -> None:
    # mkdocs keeps the output folder and replaces its children.
    for entry in site.iterdir():
        _remove(entry)
    _write_site(site, 1)


def _atomic_save(folder: Path) -> None:
    (folder / ".notes.md.1.swp").write_text("swap\n", encoding="utf-8")
    (folder / "notes.md.tmp").write_text("1\n", encoding="utf-8")
    os.replace(folder / "notes.md.tmp", folder / "notes.md")


def _republish_packets(root: Path) -> None:
    # Publish as skeleton output does: stage beside the output, move the old
    # output to a backup, rename the stage into place, drop the backup.
    generated = root / "review-packets"
    stage = root / ".review-packets.autoform-stage-1"
    _packets_output(stage, 1)
    _replace_outputs([(generated, stage, _output_identity(generated))])
    assert sorted(path.name for path in root.iterdir()) == ["Proj", "review-packets"]


def _git_folder(path: Path) -> None:
    (path / ".git").mkdir(parents=True)
    _lean(path / "Packet.lean", "generated0")


def _web_app(path: Path, revision: int) -> None:
    path.mkdir()
    (path / "manifest.json").write_text(f'{{"name": "web app {revision}"}}\n', encoding="utf-8")


def _expose_packets(generated: Path) -> None:
    generated.rename(generated.parent / ".held")
    _lean(generated / "Packet.lean", "exposed")


def _rehide_packets(generated: Path) -> None:
    shutil.rmtree(generated)
    (generated.parent / ".held").rename(generated)


# Each case: (setup, [(moment, change), ...], names the capture must not
# index).  The root holds Proj/A.lean ("kept") besides what setup adds, and
# setup and change receive the root.  Every change leaves Lean inputs alone, so
# the capture succeeds in one attempt.
_UNRELATED_CHANGES = [
    pytest.param(
        lambda r: _write_site(r / "Aout/site", 0),
        [("verify", lambda r: _replace(r / "Aout/site", lambda p: _write_site(p, 1)))],
        (),
        id="site-rebuilt",
    ),
    pytest.param(
        lambda r: _write_site(r / "zz/site", 0),
        [("verify", lambda r: _clean_site(r / "zz/site"))],
        (),
        id="site-cleaned",
    ),
    pytest.param(
        lambda r: _write_site(r / "Aout/site", 0),
        [("verify", lambda r: _remove(r / "Aout/site"))],
        (),
        id="site-removed",
    ),
    pytest.param(
        lambda r: _write_site(r / "site", 0),
        [("list ", lambda r: _remove(r / "site"))],
        (),
        id="site-removed-after-root-listing",
    ),
    pytest.param(
        lambda r: _write_site(r / "site", 0),
        [("list site", lambda r: _remove(r / "site"))],
        (),
        id="site-removed-after-its-listing",
    ),
    pytest.param(
        lambda r: _write_site(r / "site", 0),
        [("opendir site after verify", lambda r: _remove(r / "site"))],
        (),
        id="site-removed-before-verification-opens-it",
    ),
    pytest.param(
        lambda r: (r / "notes.md").write_text("0\n", encoding="utf-8"),
        [("verify", _atomic_save)],
        (),
        id="atomic-save-in-root",
    ),
    pytest.param(
        lambda r: (r / "Proj/notes.md").write_text("0\n", encoding="utf-8"),
        [("verify", lambda r: _atomic_save(r / "Proj"))],
        (),
        id="atomic-save-beside-lean-file",
    ),
    pytest.param(
        lambda r: _lock_link(r / "Proj/.#A.lean").unlink(),
        [("verify", lambda r: _lock_link(r / "Proj/.#A.lean"))],
        (),
        id="lock-link-appearing",
    ),
    pytest.param(
        lambda r: _notes_folder(r / "Proj/site"),
        [("relist Proj", lambda r: _replace(r / "Proj/site", lambda p: p.write_text("<html></html>\n")))],
        (),
        id="folder-turned-file-before-relist",
    ),
    pytest.param(
        lambda r: _lock_link(r / "Proj/.#A.lean"),
        [("relist Proj", lambda r: _lock_link(r / "Proj/.#A.lean", 2))],
        (),
        id="lock-link-rewritten-before-relist",
    ),
    pytest.param(
        lambda r: _lean(r / "Proj/B.lean", "b"),
        [("list Proj", lambda r: _remove(r / "Proj/B.lean"))],
        ("b",),
        id="lean-file-removed-before-its-lookup",
    ),
    pytest.param(
        lambda r: _lean(r / "Proj/B.lean", "b"),
        [("open B.lean after list Proj", lambda r: _remove(r / "Proj/B.lean"))],
        ("b",),
        id="lean-file-removed-before-its-read",
    ),
    pytest.param(
        lambda r: _web_app(r / "site", 0),
        [("verify", lambda r: _replace(r / "site", lambda p: _web_app(p, 1)))],
        (),
        id="unrecognized-manifest-rebuilt",
    ),
    pytest.param(
        lambda r: _clone(r / "vendor/worker", 0),
        [("verify", lambda r: _replace(r / "vendor/worker", lambda p: _clone(p, 1)))],
        ("nestedWorker0", "nestedWorker1"),
        id="nested-checkout-recloned",
    ),
    pytest.param(
        lambda r: _packets_output(r / "review-packets", 0),
        [("verify", _republish_packets)],
        ("generated0", "generated1"),
        id="skeleton-output-republished",
    ),
    pytest.param(
        lambda r: _packets_output(r / "hidden", 0),
        [("verify", lambda r: _remove(r / "hidden"))],
        ("generated0",),
        id="skeleton-output-removed",
    ),
    pytest.param(
        lambda r: _git_folder(r / "hidden"),
        [("verify", lambda r: _remove(r / "hidden"))],
        ("generated0",),
        id="nested-checkout-removed",
    ),
]


@pytest.mark.parametrize(("setup", "changes", "absent"), _UNRELATED_CHANGES)
def test_change_without_lean_inputs_is_captured_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: Callable[[Path], object],
    changes: list,
    absent: tuple[str, ...],
) -> None:
    _require_descriptor_capture()
    _index(tmp_path, "def kept : Nat := 0\n", "Proj/A.lean")
    setup(tmp_path)
    fired = [_at(monkeypatch, when, lambda change=change: change(tmp_path)) for when, change in changes]

    snapshot = _snapshot_in_one_attempt(monkeypatch, tmp_path)

    assert fired == [[when] for when, _change in changes]
    assert snapshot.index.find("kept") is not None
    for name in absent:
        assert snapshot.index.find(name) is None


def _away(r: Path, relative: str, *, copy: bool = False) -> None:
    (r / relative).rename(r.parent / "away")
    if copy:
        shutil.copytree(r.parent / "away", r / relative)


def _back(r: Path, relative: str) -> None:
    if (r / relative).exists():
        shutil.rmtree(r / relative)
    (r.parent / "away").rename(r / relative)


def _swap_for_copy(r: Path, relative: str) -> None:
    _away(r, relative, copy=True)
    shutil.rmtree(r.parent / "away")


def _sub_and_tail(r: Path) -> None:
    _lean(r / "Proj/Sub/S.lean", "s")
    (r / "Proj/Tail").mkdir()


def _lean_in_site(site: Path, rebuilt: bool = False) -> None:
    if rebuilt:
        _replace(site, lambda path: _write_site(path, 1))
    _lean(site / "New.lean")


def _moved_out(r: Path, name: str, make: Callable[[Path], object] | None = None) -> None:
    (r.parent / "outside").mkdir(exist_ok=True)
    (r / "Proj" / name).rename(r.parent / "outside" / name)
    if make is not None:
        make(r / "Proj" / name)


def _moved_in(r: Path, name: str) -> None:
    if (r / "Proj" / name).exists():
        (r / "Proj" / name).rename(r.parent / "outside" / "replacement")
    (r.parent / "outside" / name).rename(r / "Proj" / name)


# Each case: (setup, [(moment, change), ...], the path the failed capture
# names).  The root holds Proj/A.lean besides what setup adds; setup and change
# receive the root, whose parent holds entries moved out of the tree.
_LEAN_CHANGES = [
    pytest.param(
        lambda r: None,
        [("verify", lambda r: _swap_for_copy(r, "Proj"))],
        "Proj",
        id="lean-folder-replaced-by-copy",
    ),
    pytest.param(
        lambda r: _lean(r / "Proj/B.lean", "b"),
        [("verify", lambda r: _remove(r / "Proj/A.lean"))],
        "Proj/A.lean",
        id="lean-file-removed",
    ),
    pytest.param(
        lambda r: _write_site(r / "site", 0),
        [("verify", lambda r: _lean_in_site(r / "site"))],
        "site/New.lean",
        id="lean-file-in-unrelated-folder",
    ),
    pytest.param(
        lambda r: _write_site(r / "site", 0),
        [("verify", lambda r: _lean_in_site(r / "site", rebuilt=True))],
        "site/New.lean",
        id="lean-file-in-rebuilt-unrelated-folder",
    ),
    pytest.param(
        lambda r: _write_site(r / "site", 0),
        [("verify", lambda r: _lean(r / "fresh/New.lean"))],
        "fresh/New.lean",
        id="lean-file-in-new-folder",
    ),
    pytest.param(
        lambda r: _write_site(r / "site", 0),
        [("verify", lambda r: _lean(r / "site/deep/New.lean"))],
        "site/deep/New.lean",
        id="lean-file-in-new-nested-folder",
    ),
    # Only the relist at the end of the folder's scan sees X.lean, as on the
    # strict path: it appears after the listing and is gone by verification.
    pytest.param(
        lambda r: _lean(r / "Zed/B.lean", "b"),
        [("list Zed", lambda r: _lean(r / "Zed/X.lean")), ("verify", lambda r: _remove(r / "Zed/X.lean"))],
        "Zed/X.lean",
        id="lean-file-only-during-its-lean-folders-scan",
    ),
    # site holds no Lean file, so it is relisted although it is not recorded.
    pytest.param(
        lambda r: _write_site(r / "site", 0),
        [("list site", lambda r: _lean(r / "site/X.lean")), ("verify", lambda r: _remove(r / "site/X.lean"))],
        "site/X.lean",
        id="lean-file-only-during-its-unrelated-folders-scan",
    ),
    # An empty folder named X.lean becomes a regular file between the
    # verification walk's stat and its open, which fails with ENOTDIR.
    pytest.param(
        lambda r: (r / "X.lean").mkdir(),
        [("opendir X.lean after verify", lambda r: _replace(r / "X.lean", _lean))],
        "X.lean",
        id="lean-named-folder-turned-file-during-verification-walk",
    ),
    # The scan keeps nothing for the entry, which becomes a .lean file before
    # Proj's end-of-scan relist and is gone before verification.
    pytest.param(
        lambda r: _notes_folder(r / "Proj/X.lean"),
        [("relist Proj", lambda r: _replace(r / "Proj/X.lean", _lean)), ("verify", lambda r: _remove(r / "Proj/X.lean"))],
        "Proj/X.lean",
        id="unselected-folder-turned-lean-file-before-relist",
    ),
    pytest.param(
        lambda r: _lock_link(r / "Proj/.#A.lean"),
        [("relist Proj", lambda r: _replace(r / "Proj/.#A.lean", _lean)), ("verify", lambda r: _remove(r / "Proj/.#A.lean"))],
        "Proj/.#A.lean",
        id="lock-link-turned-lean-file-before-relist",
    ),
    # Moved out of the tree between the scan's lookup and its open, so the scan
    # drops B.lean as vanished or walks the notes folder that took D's place;
    # moved back before Proj's relist, it loses its Lean input before
    # verification.  Only the relist sees it, and only because the scan keeps
    # no identity for a dropped file and the opened identity of a walked folder.
    pytest.param(
        lambda r: _lean(r / "Proj/B.lean", "b"),
        [
            ("open B.lean after list Proj", lambda r: _moved_out(r, "B.lean")),
            ("relist Proj", lambda r: _moved_in(r, "B.lean")),
            ("verify", lambda r: _remove(r / "Proj/B.lean")),
        ],
        "Proj/B.lean",
        id="lean-file-moved-out-at-open-and-back-before-relist",
    ),
    pytest.param(
        lambda r: _lean(r / "Proj/D/New.lean", "n"),
        [
            ("opendir D after list Proj", lambda r: _moved_out(r, "D", _notes_folder)),
            ("relist Proj", lambda r: _moved_in(r, "D")),
            ("verify", lambda r: _remove(r / "Proj/D/New.lean")),
        ],
        "Proj/D/New.lean",
        id="lean-folder-moved-out-at-open-and-back-before-relist",
    ),
    # Proj/Sub is restored before verification, so only the scan's own checks
    # see it: gone at Proj's relist, or a different folder when Proj looks at
    # Sub again after descending into it.
    pytest.param(
        _sub_and_tail,
        [("list Proj/Tail", lambda r: _away(r, "Proj/Sub")), ("verify", lambda r: _back(r, "Proj/Sub"))],
        "Proj/Sub",
        id="lean-folder-moved-away-during-its-parents-scan",
    ),
    pytest.param(
        lambda r: _lean(r / "Proj/Sub/S.lean", "s"),
        [("list Proj/Sub", lambda r: _away(r, "Proj/Sub", copy=True)), ("verify", lambda r: _back(r, "Proj/Sub"))],
        "Proj/Sub",
        id="lean-folder-replaced-by-copy-during-its-scan",
    ),
    # A marker that appears in a folder of Lean files hides them; only the
    # folder's end-of-scan relist sees one that is gone again by verification.
    pytest.param(
        lambda r: _lean(r / "vendor/lib/Lib.lean", "vendored"),
        [("relist vendor/lib", lambda r: (r / "vendor/lib/.git").mkdir()), ("verify", lambda r: (r / "vendor/lib/.git").rmdir())],
        "vendor/lib",
        id="checkout-marker-only-at-its-folders-relist",
    ),
    pytest.param(
        lambda r: _packets_output(r / "review-packets", 0),
        [("verify", lambda r: _expose_packets(r / "review-packets"))],
        "review-packets/Packet.lean",
        id="skeleton-output-replaced-by-lean-folder",
    ),
    pytest.param(
        lambda r: _packets_output(r / "review-packets", 0),
        [("verify", lambda r: _remove(r / "review-packets/manifest.json"))],
        "review-packets/Packet.lean",
        id="skeleton-output-unmarked-in-place",
    ),
    pytest.param(
        lambda r: _packets_output(r / "review-packets", 0),
        [("relist ", lambda r: _expose_packets(r / "review-packets")), ("verify", lambda r: _rehide_packets(r / "review-packets"))],
        "review-packets/Packet.lean",
        id="skeleton-output-exposed-only-at-root-relist",
    ),
]


@pytest.mark.parametrize(("setup", "changes", "named"), _LEAN_CHANGES)
def test_lean_change_is_caught_and_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: Callable[[Path], object],
    changes: list,
    named: str,
) -> None:
    _require_descriptor_capture()
    root = tmp_path / "root"
    _index(root, "def kept : Nat := 0\n", "Proj/A.lean")
    setup(root)
    fired = [_at(monkeypatch, when, lambda change=change: change(root)) for when, change in changes]

    assert _changed_path(root) == named
    expected = [[when] for when, _change in changes]
    if len(changes) > 1:
        # The last change hides the first from the final verification, so an
        # earlier check must catch it and verification is never reached.
        expected[-1] = []
    assert fired == expected


@pytest.mark.parametrize("step", ["lookup", "examine"])
@pytest.mark.parametrize(
    ("name", "named"),
    [("B.lean", "Proj/B.lean"), ("Sub", "Proj/Sub/S.lean"), ("L.lean", "Proj/L.lean")],
    ids=["file", "folder", "link"],
)
def test_lean_entry_dropped_as_vanished_and_back_at_its_folders_relist_is_caught(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    named: str,
    step: str,
) -> None:
    # The scan finds the entry gone at its lookup, or when it examines it (the
    # open of a file or folder, the readlink of a link), and drops it.  The
    # entry is back right after that call, so Proj's end-of-scan relist lists
    # it, and it is gone again before verification.  Only that relist can see
    # it, by examining the name it dropped.
    _require_descriptor_capture()
    _index(tmp_path, "def a : Nat := 0\n", "Proj/A.lean")
    entry = tmp_path / "Proj" / name

    def restore() -> None:
        if name == "L.lean":
            entry.symlink_to("A.lean")
        else:
            _lean(entry / "S.lean" if name == "Sub" else entry, "b")

    restore()
    call = "stat" if step == "lookup" else "readlink" if name == "L.lean" else "open"
    original = getattr(os, call)
    armed = _at(monkeypatch, "list Proj", lambda: None)
    fired: list[str] = []

    def vanish_around_call(path, *args, **kwargs):
        if armed and not fired and path == name and kwargs.get("dir_fd") is not None:
            fired.append(step)
            _remove(entry)
            try:
                return original(path, *args, **kwargs)
            finally:
                restore()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, call, vanish_around_call)
    _at(monkeypatch, "verify", lambda: _remove(entry))

    assert _changed_path(tmp_path) == named
    assert fired == [step]


def test_lean_file_edited_while_it_is_read_is_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_descriptor_capture()
    _index(tmp_path, "def kept : Nat := 0\n", "Proj/A.lean")
    source = tmp_path / "Proj" / "A.lean"
    original_fdopen = os.fdopen
    edited = False

    def edit_once(descriptor, *args, **kwargs):
        nonlocal edited
        if not edited and os.fstat(descriptor).st_ino == source.stat().st_ino:
            edited = True
            with source.open("a", encoding="utf-8") as handle:
                handle.write("def edited : Nat := 1\n")
        return original_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(tree_snapshot_module.os, "fdopen", edit_once)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)

    assert _changed_path(tmp_path) == "Proj/A.lean"
    edited = False
    binds = _count_binds(monkeypatch)
    snapshot = snapshot_project_sources(tmp_path)

    assert edited
    assert len(binds) == 2
    # `_changed_path`'s capture and this snapshot's first attempt each appended one `def edited`.
    assert len(snapshot.index.occurrences("edited")) == 2




def test_marker_candidate_removed_before_it_is_read_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_descriptor_capture()
    _index(tmp_path, "def kept : Nat := 0\n", "Proj/A.lean")
    site = tmp_path / "site"
    site.mkdir()
    manifest = site / "manifest.json"
    manifest.write_text('{"name": "web app"}\n', encoding="utf-8")
    original_names = tree_snapshot_module._capture_directory_names
    removed = False

    def list_then_remove(descriptor, **kwargs):
        nonlocal removed
        names = original_names(descriptor, **kwargs)
        if not removed and "manifest.json" in names:
            # The site generator removes its output between the listing and
            # the marker lookup.
            removed = True
            manifest.unlink()
        return names

    monkeypatch.setattr(tree_snapshot_module, "_capture_directory_names", list_then_remove)

    snapshot = _snapshot_in_one_attempt(monkeypatch, tmp_path)

    assert removed
    assert snapshot.index.find("kept") is not None



def test_checkout_marker_appearing_beside_lean_files_is_caught(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_descriptor_capture()
    _index(tmp_path, "def main : Nat := 0\n", "Main.lean")
    _index(tmp_path, "def vendored : Nat := 0\n", "vendor/lib/Lib.lean")
    marker = tmp_path / "vendor" / "lib" / ".git"

    def clone() -> None:
        if not marker.exists():
            marker.mkdir()

    _on_checkpoint(monkeypatch, "before-final-verification", clone)

    assert _changed_path(tmp_path) == "vendor/lib"

    marker.rmdir()
    binds = _count_binds(monkeypatch)
    snapshot = snapshot_project_sources(tmp_path)

    assert len(binds) == 2
    assert snapshot.index.find("main") is not None
    assert snapshot.index.find("vendored") is None


def test_unrelated_folders_do_not_change_tree_revisions(tmp_path: Path) -> None:
    _require_descriptor_capture()
    plain = tmp_path / "plain"
    busy = tmp_path / "busy"
    for root in (plain, busy):
        _index(root, "def a : Nat := 0\n", "Proj/A.lean")
    _write_site(busy / "site", 0)
    (busy / "docs").mkdir()
    (busy / "docs" / "x.md").write_text("notes\n", encoding="utf-8")

    def capture(root: Path):
        sources = open_project_sources(root)
        try:
            return sources.tree.capture(), sources.capture()
        finally:
            sources.close()

    plain_tree, plain_sources = capture(plain)
    busy_tree, busy_sources = capture(busy)
    shutil.rmtree(busy / "site")
    _write_site(busy / "site", 1)
    rebuilt_tree, rebuilt_sources = capture(busy)

    assert busy_tree.revision == plain_tree.revision
    assert busy_sources.revision == plain_sources.revision
    assert rebuilt_tree.generation_revision == busy_tree.generation_revision
    assert rebuilt_sources.generation_revision == busy_sources.generation_revision


def test_verification_walk_charges_capture_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_descriptor_capture()
    _index(tmp_path, "def kept : Nat := 0\n", "Proj/A.lean")
    (tmp_path / "site").mkdir()
    # Proj, site and Proj/A.lean: three entries per pass.
    limits = TreeCaptureLimits(max_entries=5)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)
    assert snapshot_project_sources(tmp_path, limits=limits).index.find("kept") is not None

    def grow() -> None:
        for number in range(10):
            (tmp_path / "site" / f"page{number}.html").write_text("x\n", encoding="utf-8")

    _on_checkpoint(monkeypatch, "before-final-verification", grow)
    binds = _count_binds(monkeypatch)

    with pytest.raises(lean_module.LeanSourceError, match="max_entries"):
        snapshot_project_sources(tmp_path, limits=limits)

    assert len(binds) == 2


@pytest.mark.parametrize("excluded", [".lake", "build"])
def test_rechecks_do_not_walk_excluded_folders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    excluded: str,
) -> None:
    _require_descriptor_capture()
    _index(tmp_path, "def kept : Nat := 0\n", "Proj/A.lean")
    (tmp_path / excluded).mkdir()
    for number in range(10):
        (tmp_path / excluded / f"Out{number}.lean").write_text(f"def out{number} : Nat := 0\n", encoding="utf-8")
    binds = _count_binds(monkeypatch)

    # Proj, the excluded folder and Proj/A.lean: three entries per pass, so
    # walking the excluded folder's ten entries would exceed the limit.
    snapshot = snapshot_project_sources(tmp_path, limits=TreeCaptureLimits(max_entries=5))

    assert len(binds) == 1
    assert snapshot.index.find("kept") is not None
    assert snapshot.index.find("out0") is None


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        # Opening site is refused.
        (0, "permission denied: site"),
        # site opens and lists, but looking up its entries is refused, which
        # blames site because a lookup checks only the folder's search permission.
        (0o444, "permission denied: site"),
    ],
    ids=["open", "lookup"],
)
def test_folder_refused_to_a_selected_recheck_is_not_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    reason: str,
) -> None:
    _require_descriptor_capture()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    _index(tmp_path, "def kept : Nat := 0\n", "Proj/A.lean")
    site = tmp_path / "site"
    site.mkdir()

    def refuse() -> None:
        if os.access(site, os.W_OK):
            (site / "New.lean").write_text("def appeared : Nat := 0\n", encoding="utf-8")
            site.chmod(mode)

    _on_checkpoint(monkeypatch, "before-final-verification", refuse)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)
    binds = _count_binds(monkeypatch)
    try:
        # The verification cannot look into site, so the attempt is a change;
        # the retry's scan then reports the lasting refusal.
        with pytest.raises(lean_module.LeanSourceError, match=rf"^{re.escape(reason)}$"):
            snapshot_project_sources(tmp_path)
    finally:
        site.chmod(0o755)

    assert len(binds) == 2


def test_snapshot_retries_a_root_replaced_while_it_is_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_descriptor_capture()
    project = tmp_path / "project"
    _index(project, "def canonical : Nat := 0\n")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    original_stat = os.stat
    swapped = False

    def stat_once_replaced(path, *args, **kwargs):
        nonlocal swapped
        if not swapped and os.fspath(path) == os.fspath(project) and kwargs.get("follow_symlinks") is False:
            swapped = True
            return original_stat(replacement, *args, **kwargs)
        return original_stat(path, *args, **kwargs)

    attempts = _count_binds(monkeypatch)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)
    with monkeypatch.context() as context:
        context.setattr(directory_binding_module.os, "stat", stat_once_replaced)
        snapshot = snapshot_project_sources(project)

    assert swapped
    assert len(attempts) == 2
    assert snapshot.index.find("canonical") is not None


@pytest.mark.parametrize("git_entry", ["file", "directory"])
@pytest.mark.parametrize("relative", [".claude/worktrees/worker", "vendor/worker"])
def test_nested_checkouts_are_not_indexed_as_project_source(
    tmp_path: Path,
    git_entry: str,
    relative: str,
) -> None:
    # A Git worktree or submodule has a .git file; a nested clone has a directory.
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / relative
    (worktree / "Project").mkdir(parents=True)
    marker = worktree / ".git"
    if git_entry == "file":
        marker.write_text("gitdir: elsewhere\n", encoding="utf-8")
    else:
        marker.mkdir()
    (worktree / "Project/Basic.lean").write_text(
        "def toplevel : Nat := 3\ndef workerOnly : Nat := 0\n", encoding="utf-8"
    )
    for number in range(32):
        (worktree / f"Project/Nested{number}.lean").write_text(
            f"def nestedWorker{number} : Nat := {number}\n",
            encoding="utf-8",
        )

    _index(tmp_path)
    snapshot = snapshot_project_sources(
        tmp_path,
        limits=TreeCaptureLimits(max_entries=12),
    )

    assert snapshot.index.find("toplevel").path == Path("Project/Basic.lean")
    assert snapshot.index.find("workerOnly") is None
    assert snapshot.index.find("nestedWorker0") is None

    # Churn below the nested checkout is outside this project's evidence
    # generation, just as it was outside the old pathname scan.
    (worktree / "Project/Nested0.lean").write_text(
        "def nestedWorker0 : Nat := 999\n",
        encoding="utf-8",
    )
    if git_entry == "file":
        marker.write_text("gitdir: moved-elsewhere\n", encoding="utf-8")
    else:
        (marker / "config").write_text("[core]\n", encoding="utf-8")
    after = snapshot_project_sources(
        tmp_path,
        limits=TreeCaptureLimits(max_entries=12),
    )
    assert after.revision == snapshot.revision
    assert after.generation_revision == snapshot.generation_revision


@pytest.mark.parametrize("replacement_kind", ["file", "directory"])
def test_nested_checkout_marker_replacement_is_captured_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    # A nested checkout contributes no Lean input, so its marker is not kept:
    # swapping a clone's .git folder for a worktree's .git file (or back)
    # leaves it hidden and is not a change.
    _index(tmp_path)
    worker = tmp_path / "vendor/worker"
    (worker / "Project").mkdir(parents=True)
    marker = worker / ".git"
    if replacement_kind == "file":
        marker.mkdir()
    else:
        marker.write_text("gitdir: before\n", encoding="utf-8")
    (worker / "Project/Worker.lean").write_text(
        "def nestedWorker : Nat := 0\n",
        encoding="utf-8",
    )

    changed = False

    def replace_marker() -> None:
        nonlocal changed
        if changed:
            return
        changed = True
        if replacement_kind == "file":
            marker.rmdir()
            marker.write_text("gitdir: after\n", encoding="utf-8")
        else:
            marker.unlink()
            marker.mkdir()

    _on_checkpoint(monkeypatch, "before-final-verification", replace_marker)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)

    snapshot = _snapshot_in_one_attempt(monkeypatch, tmp_path)

    assert changed
    assert snapshot.index.find("toplevel") is not None
    assert snapshot.index.find("nestedWorker") is None


@pytest.mark.parametrize(
    "transient_errno",
    [
        errno.ENOENT,
        errno.ENOTDIR,
        errno.ELOOP,
        *([errno.ESTALE] if hasattr(errno, "ESTALE") else []),
    ],
)
def test_snapshot_retries_a_one_shot_initial_bind_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transient_errno: int,
) -> None:
    root = tmp_path / "project"
    _index(root, "def recoveredAfterRename : Nat := 0\n")
    original_open = directory_binding_module.os.open
    root_opens = 0

    def missing_once(path, flags, *args, **kwargs):
        nonlocal root_opens
        if kwargs.get("dir_fd") is None and Path(path) == root:
            root_opens += 1
            if root_opens == 1:
                raise OSError(transient_errno, os.strerror(transient_errno))
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(directory_binding_module.os, "open", missing_once)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)

    snapshot = snapshot_project_sources(root)

    assert root_opens == 2
    assert snapshot.index.find("recoveredAfterRename") is not None


@pytest.mark.parametrize("failure", ["io-error", "recursion"])
def test_lasting_capture_failure_is_reported_without_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _require_descriptor_capture()
    _index(tmp_path, "def canonical : Nat := 0\n")

    def fail_read(*_args, **_kwargs):
        if failure == "io-error":
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(
        tree_snapshot_module,
        "_read_file",
        fail_read,
    )
    attempts = _count_binds(monkeypatch)
    reason = (
        f"directory tree could not be read: {os.strerror(errno.EIO)}"
        if failure == "io-error"
        else "directory tree is nested too deeply to capture"
    )

    with pytest.raises(lean_module.LeanSourceError, match=rf"^{re.escape(reason)}$"):
        snapshot_project_sources(tmp_path)

    assert len(attempts) == 1


def test_selected_verification_recurses_in_three_frames_per_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Capture has no depth limit, so the frames verification spends per
    # folder level decide how deep a tree fits under the recursion limit.
    _require_descriptor_capture()
    folder = tmp_path / "d" / "d" / "d" / "d"
    folder.mkdir(parents=True)
    (folder / "Deep.lean").write_text("def deep : Nat := 0\n")
    frames_at_level: dict[int, int] = {}
    lookup = tree_snapshot_module._lookup

    def counting_lookup(descriptor: int, name: str, relative: str) -> os.stat_result:
        names = []
        frame = sys._getframe()
        while frame is not None:
            names.append(frame.f_code.co_name)
            frame = frame.f_back
        if "_verify_snapshot" in names:
            level = relative.count("/") + 1 if relative else 0
            frames_at_level.setdefault(level, len(names))
        return lookup(descriptor, name, relative)

    monkeypatch.setattr(tree_snapshot_module, "_lookup", counting_lookup)

    snapshot = snapshot_project_sources(tmp_path)

    assert snapshot.index.find("deep") is not None
    assert [frames_at_level[level + 1] - frames_at_level[level] for level in range(4)] == [3, 3, 3, 3]


def test_selected_verification_calls_back_through_a_plain_function(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A C-implemented callback such as functools.partial adds no Python frame,
    # so the frame count above misses it, but it spends CPython's separate C
    # recursion budget at every folder level of verification.
    _require_descriptor_capture()
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "Deep.lean").write_text("def deep : Nat := 0\n")
    callbacks: list[type] = []
    recheck = tree_snapshot_module._recheck_scanned_directory

    def recording_recheck(*args: object, **kwargs: object) -> object:
        if kwargs.get("verify_kept") is not None:
            callbacks.append(type(kwargs["verify_kept"]))
        return recheck(*args, **kwargs)

    monkeypatch.setattr(tree_snapshot_module, "_recheck_scanned_directory", recording_recheck)

    snapshot_project_sources(tmp_path)

    assert callbacks and set(callbacks) == {types.FunctionType}


@pytest.mark.parametrize(
    ("relative", "directory"),
    [("Project/Secret.lean", False), ("Project/Private", True)],
)
def test_unreadable_lean_source_is_named_by_its_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    directory: bool,
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses file permissions")
    _index(tmp_path, "def canonical : Nat := 0\n")
    secret = tmp_path / relative
    if directory:
        secret.mkdir()
        (secret / "Hidden.lean").write_text("def hidden : Nat := 0\n", encoding="utf-8")
    else:
        secret.write_text("def secret : Nat := 0\n", encoding="utf-8")
    secret.chmod(0)
    try:
        if os.access(secret, os.R_OK):
            pytest.skip("read permission is not enforced")
        with pytest.raises(lean_module.LeanSourceError, match=rf"^permission denied: {re.escape(relative)}$") as caught:
            snapshot_project_sources(tmp_path)
    finally:
        secret.chmod(0o755 if directory else 0o644)

    assert str(tmp_path) not in str(caught.value)


def test_unsearchable_directory_is_blamed_instead_of_its_children(tmp_path: Path) -> None:
    """A folder with read but no search permission lists its names, but every
    lookup of one fails; the folder is at fault, not its entries."""

    _require_descriptor_capture()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    _index(tmp_path, "def canonical : Nat := 0\n")
    unsearchable = tmp_path / "Project" / "Sub"
    unsearchable.mkdir()
    (unsearchable / "Hidden.lean").write_text("def hidden : Nat := 0\n", encoding="utf-8")
    unsearchable.chmod(0o644)
    try:
        with pytest.raises(
            lean_module.LeanSourceError,
            match=r"^permission denied: Project/Sub$",
        ):
            snapshot_project_sources(tmp_path)
    finally:
        unsearchable.chmod(0o755)


def test_search_permission_revoked_at_any_lookup_or_open_blames_the_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revoking search permission on a folder refuses every later lookup,
    open and readlink of a name in it. Whichever of them the revocation
    precedes, during capture or final verification, the folder is named."""

    _require_descriptor_capture()
    _index(tmp_path, "def canonical : Nat := 0\n")
    project = tmp_path / "Project"
    (project / "Sub").mkdir()
    (project / "Sub" / "Nested.lean").write_text("def nested : Nat := 0\n", encoding="utf-8")
    (project / "Link.lean").symlink_to("Basic.lean")
    (project / "Out").mkdir()
    (project / "Out" / "manifest.json").write_text(
        json.dumps({"kind": "packets", "packets": [], "schema": "autoform-skeleton-packets/v2"}),
        encoding="utf-8",
    )
    folders = {}
    for relative in (".", "Project", "Project/Sub", "Project/Out"):
        metadata = os.stat(tmp_path / relative)
        folders[metadata.st_dev, metadata.st_ino] = relative
    operations = 0
    revoke_at = 0
    revoked = None

    def by_name(original):
        def operation(path, *args, dir_fd=None, **kwargs):
            nonlocal operations, revoked
            if dir_fd is not None:
                metadata = os.fstat(dir_fd)
                folder = (metadata.st_dev, metadata.st_ino)
                if folder in folders:
                    operations += 1
                    if operations == revoke_at:
                        revoked = folder
                    if folder == revoked:
                        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
            return original(path, *args, dir_fd=dir_fd, **kwargs)

        return operation

    for name in ("stat", "open", "readlink"):
        monkeypatch.setattr(os, name, by_name(getattr(os, name)))
    # Lean indexing rejects the link only after the capture has read it.
    with pytest.raises(lean_module.LeanSourceError, match="symbolic links are not supported"):
        snapshot_project_sources(tmp_path)
    for revoke_at in range(1, operations + 1):
        operations = 0
        revoked = None
        with pytest.raises(lean_module.LeanSourceError) as caught:
            snapshot_project_sources(tmp_path)
        assert str(caught.value) == f"permission denied: {folders[revoked]}", revoke_at


def test_entry_vanishing_after_a_refused_open_is_retried_as_a_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telling who refused an open repeats the lookup; if the entry is gone
    by then, nothing was refused for good and the capture is retried."""

    _require_descriptor_capture()
    _index(tmp_path, "def canonical : Nat := 0\n")
    failures = [
        ("open", PermissionError(errno.EACCES, "refused")),
        ("stat", FileNotFoundError(errno.ENOENT, "vanished")),
    ]

    def failing(name):
        original = getattr(os, name)

        def operation(path, *args, dir_fd=None, **kwargs):
            if dir_fd is not None and path == "Basic.lean" and failures[:1] and failures[0][0] == name:
                raise failures.pop(0)[1]
            return original(path, *args, dir_fd=dir_fd, **kwargs)

        return operation

    for name in ("open", "stat"):
        monkeypatch.setattr(os, name, failing(name))
    snapshot_project_sources(tmp_path)
    assert failures == []


def test_unsupported_entry_name_is_reported_without_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("backslashes separate Windows path components")
    _index(tmp_path, "def canonical : Nat := 0\n")
    try:
        (tmp_path / "Project" / "Odd\\Name.lean").write_text("def odd : Nat := 0\n", encoding="utf-8")
    except OSError:
        pytest.skip("backslashes in file names are unavailable")
    attempts = _count_binds(monkeypatch)

    with pytest.raises(lean_module.LeanSourceError, match="directory tree contains an unsupported entry name"):
        snapshot_project_sources(tmp_path)

    assert len(attempts) == 1


def test_portable_binding_rejects_a_replacement_root_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    _index(project, "def original : Nat := 0\n")
    replacement = tmp_path / "replacement"
    _index(replacement, "def replacement : Nat := 0\n")
    displaced = tmp_path / "displaced"
    _use_portable_capture(monkeypatch)
    bound = BoundDirectoryTree(project)
    original_verify = bound.verify
    original_capture = tree_snapshot_module._capture_portable
    capture_count = 0
    replacement_started = False

    def verify_then_replace() -> None:
        nonlocal replacement_started
        original_verify()
        if not replacement_started:
            project.rename(displaced)
            replacement.rename(project)
            replacement_started = True

    def capture_then_restore(*args, **kwargs):
        nonlocal capture_count
        snapshot = original_capture(*args, **kwargs)
        capture_count += 1
        if capture_count == 2:
            project.rename(replacement)
            displaced.rename(project)
        return snapshot

    monkeypatch.setattr(bound, "verify", verify_then_replace)
    monkeypatch.setattr(tree_snapshot_module, "_capture_portable", capture_then_restore)
    try:
        with pytest.raises(TreeSnapshotError, match="cannot be inspected safely"):
            bound.capture()
    finally:
        bound.close()
        if displaced.exists():
            if project.exists():
                project.rename(replacement)
            displaced.rename(project)


def test_descriptor_and_portable_capture_have_identical_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_directory_descriptors()
    root = tmp_path / "tree"
    (root / "a" / "x").mkdir(parents=True)
    (root / "a-").mkdir()
    (root / "a" / "x" / "One.lean").write_text("def one : Nat := 1\n")
    (root / "a-" / "Two.lean").write_text("def two : Nat := 2\n")
    with bind_directory_tree(root) as bound:
        descriptor_snapshot = bound.capture()

    _use_portable_capture(monkeypatch)
    with bind_directory_tree(root) as bound:
        portable_snapshot = bound.capture()

    assert descriptor_snapshot == portable_snapshot
    assert descriptor_snapshot.revision == portable_snapshot.revision
    assert (
        descriptor_snapshot.generation_revision
        == portable_snapshot.generation_revision
    )


def test_portable_binding_rejects_a_symlinked_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = tmp_path / "real"
    project = real / "project"
    project.mkdir(parents=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    _use_portable_capture(monkeypatch)

    with pytest.raises(TreeSnapshotError, match="unsafe component"):
        BoundDirectoryTree(alias / "project")


def test_portable_capture_does_not_require_path_stat_no_follow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    _index(root, "def portable : Nat := 0\n")
    original_stat = Path.stat

    def stat_without_no_follow(self, *args, **kwargs):
        if kwargs.get("follow_symlinks") is False:
            raise NotImplementedError("no-follow stat is unavailable")
        return original_stat(self, *args, **kwargs)

    _use_portable_capture(monkeypatch)
    monkeypatch.setattr(Path, "stat", stat_without_no_follow)

    with bind_directory_tree(root) as bound:
        snapshot = bound.capture()

    assert snapshot.files == (("Project/Basic.lean", b"def portable : Nat := 0\n"),)


def test_portable_capture_rejects_an_unrestored_nested_directory_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "Local.lean").write_text("def local : Nat := 0\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Escaped.lean").write_text("def escaped : Nat := 0\n", encoding="utf-8")
    displaced = root / "nested-displaced"

    def restore() -> None:
        if nested.is_symlink():
            nested.unlink()
            displaced.rename(nested)

    def descend(path) -> bool:
        if path.as_posix() == "nested" and not nested.is_symlink():
            nested.rename(displaced)
            nested.symlink_to(outside, target_is_directory=True)
        return True

    original_signature = tree_snapshot_module._stat_signature

    def coarse_directory_signature(metadata):
        signature = original_signature(metadata)
        if stat.S_ISDIR(metadata.st_mode):
            return (*signature[:3], 0, 0, 0, 0)
        return signature

    _use_portable_capture(monkeypatch)
    _on_checkpoint(monkeypatch, "between-portable-captures", restore)
    monkeypatch.setattr(tree_snapshot_module, "_stat_signature", coarse_directory_signature)
    bound = BoundDirectoryTree(
        root,
        selection=TreeSelection(include=lambda _path, _mode: True, descend=descend),
    )
    try:
        with pytest.raises(TreeSnapshotError, match="changed while it was captured"):
            bound.capture()
    finally:
        restore()
        bound.close()


@pytest.mark.xfail(
    strict=True,
    raises=pytest.fail.Exception,
    reason=(
        "the portable fallback is best effort: a redirection restored before the "
        "directory is re-examined, with directory times reset, is not detected"
    ),
)
def test_portable_capture_rejects_a_restored_nested_directory_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        (tmp_path / "symlink-probe").symlink_to("target", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    root = tmp_path / "project"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "Local.lean").write_text("def local : Nat := 0\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Escaped.lean").write_bytes(b"def escaped : Nat := 0\n")
    displaced = root / "nested-displaced"
    redirected = 0
    restored = 0

    def restore() -> None:
        nonlocal restored
        if nested.is_symlink():
            nested.unlink()
            displaced.rename(nested)
            restored += 1

    original_names = tree_snapshot_module._capture_directory_names
    original_read = tree_snapshot_module._read_portable_file
    original_signature = tree_snapshot_module._stat_signature

    def redirect_then_list(directory, **kwargs):
        nonlocal redirected
        if os.fspath(directory) == os.fspath(nested) and not nested.is_symlink():
            nested.rename(displaced)
            nested.symlink_to(outside, target_is_directory=True)
            redirected += 1
        return original_names(directory, **kwargs)

    def read_then_restore(path, *args, **kwargs):
        data = original_read(path, *args, **kwargs)
        restore()
        return data

    def coarse_directory_signature(metadata):
        signature = original_signature(metadata)
        if stat.S_ISDIR(metadata.st_mode):
            return (*signature[:3], 0, 0, 0, 0)
        return signature

    _use_portable_capture(monkeypatch)
    monkeypatch.setattr(tree_snapshot_module, "_capture_directory_names", redirect_then_list)
    monkeypatch.setattr(tree_snapshot_module, "_read_portable_file", read_then_restore)
    monkeypatch.setattr(tree_snapshot_module, "_stat_signature", coarse_directory_signature)
    bound = BoundDirectoryTree(
        root,
        selection=TreeSelection(include=lambda _path, _mode: True, descend=lambda _path: True),
    )
    try:
        try:
            snapshot = bound.capture()
        except TreeSnapshotError as error:
            assert "changed while it was captured" in str(error)
            return
        staged = (redirected, restored)
    finally:
        restore()
        bound.close()

    # The xfail must come from an undetected redirection, not a broken setup.
    assert staged == (2, 2)
    assert ("nested/Escaped.lean", b"def escaped : Nat := 0\n") in snapshot.files
    pytest.fail("a restored nested directory redirection was not detected")


def test_portable_capture_rejects_a_file_swapped_to_fifo_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("named pipes are unavailable")
    root = tmp_path / "project"
    source = root / "Source.lean"
    source.parent.mkdir()
    source.write_text("def original : Nat := 0\n")
    displaced = root / "Source.previous"
    swapped = False

    def include(relative, _mode: int) -> bool:
        nonlocal swapped
        if relative.as_posix() == "Source.lean" and not swapped:
            source.rename(displaced)
            os.mkfifo(source)
            swapped = True
        return True

    def blocking_path_open(*_args, **_kwargs):
        raise AssertionError("portable capture must use nonblocking os.open")

    _use_portable_capture(monkeypatch)
    monkeypatch.setattr(Path, "open", blocking_path_open)
    bound = BoundDirectoryTree(
        root,
        selection=TreeSelection(include=include, descend=lambda _path: True),
    )
    try:
        with pytest.raises(TreeSnapshotError, match="changed while it was captured"):
            bound.capture()
    finally:
        bound.close()

    assert swapped


def test_portable_binding_normalizes_an_invalid_root_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_portable_capture(monkeypatch)

    with pytest.raises(TreeSnapshotError, match="cannot be inspected safely"):
        BoundDirectoryTree(tmp_path / "bad\0name")


def test_source_binding_normalizes_an_invalid_exclusion_path(
    tmp_path: Path,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")

    with pytest.raises(OSError, match="Lean exclusion path cannot be inspected safely"):
        snapshot_project_sources(tmp_path, exclude_roots=("bad\0name",))


def test_directory_binding_resolves_dot_dot_after_a_symlinked_ancestor_physically(
    tmp_path: Path,
) -> None:
    _require_directory_descriptors()
    holder = tmp_path / "holder"
    (holder / "target").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "inner").mkdir(parents=True)
    (elsewhere / "target").mkdir()
    try:
        (holder / "link").symlink_to(elsewhere / "inner", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    physical = (elsewhere / "target").stat()
    lexical = (holder / "target").stat()

    binding = directory_binding_module.open_directory(holder / "link" / ".." / "target")
    try:
        assert binding.identity == (physical.st_dev, physical.st_ino)
        assert binding.identity != (lexical.st_dev, lexical.st_ino)
    finally:
        binding.close()


def test_directory_binding_refuses_a_symlinked_root(tmp_path: Path) -> None:
    _require_directory_descriptors()
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(OSError, match="directory root must not be a symbolic link"):
        directory_binding_module.open_directory(alias)


def test_directory_binding_accepts_a_symlinked_ancestor(tmp_path: Path) -> None:
    _require_directory_descriptors()
    real = tmp_path / "real"
    project = real / "project"
    _index(project, "def throughAncestorLink : Nat := 0\n")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    expected = project.stat()

    binding = directory_binding_module.open_directory(alias / "project")
    try:
        assert binding.identity == (expected.st_dev, expected.st_ino)
        binding.verify()
    finally:
        binding.close()
    snapshot = snapshot_project_sources(alias / "project")

    assert snapshot.index.find("throughAncestorLink") is not None


def test_directory_binding_needs_only_search_permission_on_ancestors(
    tmp_path: Path,
) -> None:
    _require_directory_descriptors()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    ancestor = tmp_path / "search-only"
    project = ancestor / "project"
    _index(project, "def searchOnlyAncestor : Nat := 0\n")
    ancestor.chmod(0o311)
    try:
        if os.access(ancestor, os.R_OK):
            pytest.skip("directory read permission is not enforced")
        binding = directory_binding_module.open_directory(project)
        binding.close()
        snapshot = snapshot_project_sources(project)
        index = index_project(project)
    finally:
        ancestor.chmod(0o755)

    assert snapshot.index.find("searchOnlyAncestor") is not None
    assert index.find("searchOnlyAncestor") is not None


def test_directory_binding_rejects_an_invalid_path_without_opening_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_directory_descriptors()
    parent = tmp_path / "parent"
    parent.mkdir()
    original_open = os.open
    opened: list[int] = []

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    try:
        with monkeypatch.context() as context:
            context.setattr(directory_binding_module.os, "open", tracked_open)
            with pytest.raises(OSError, match="directory path is invalid"):
                directory_binding_module.open_directory(parent / "bad\0name")
    finally:
        for descriptor in opened:
            os.close(descriptor)

    assert opened == []


def test_directory_binding_closes_its_descriptor_when_the_root_changes_while_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_directory_descriptors()
    root = tmp_path / "root"
    root.mkdir()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    original_open = os.open
    original_close = os.close
    original_stat = os.stat
    opened: list[int] = []
    closed: list[int] = []

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    def replaced_stat(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(root):
            return original_stat(replacement, *args, **kwargs)
        return original_stat(path, *args, **kwargs)

    leaked: list[int] = []
    try:
        with monkeypatch.context() as context:
            context.setattr(directory_binding_module.os, "open", tracked_open)
            context.setattr(directory_binding_module.os, "close", tracked_close)
            context.setattr(directory_binding_module.os, "stat", replaced_stat)
            with pytest.raises(OSError, match="directory root changed while it was opened"):
                directory_binding_module.open_directory(root)
        leaked = [descriptor for descriptor in opened if descriptor not in closed]
    finally:
        for descriptor in leaked:
            original_close(descriptor)

    assert len(opened) == 1
    assert leaked == []


def test_explicit_roots_are_skipped(tmp_path: Path) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n", "Project/Basic.lean")
    excluded = tmp_path / "site"
    excluded.mkdir()
    (excluded / "Copied.lean").write_text("def copied : Nat := 0\n", encoding="utf-8")
    index = index_project(tmp_path, exclude_roots=(excluded,))

    assert index.find("canonical") is not None
    assert index.find("copied") is None


@pytest.mark.parametrize("alias_exclusion", [False, True])
def test_exclusion_survives_case_aliases(
    tmp_path: Path,
    alias_exclusion: bool,
) -> None:
    project = tmp_path / "ProjectCase"
    _index(project, "def kept : Nat := 0\n", "Project/Keep.lean")
    excluded = project / "Excluded"
    excluded.mkdir()
    (excluded / "Leaked.lean").write_text("def leaked : Nat := 0\n", encoding="utf-8")
    alias = tmp_path / "projectcase"
    if not alias.exists():
        pytest.skip("filesystem is case-sensitive")

    requested_exclusion = alias / "excluded" if alias_exclusion else excluded
    index = index_project(alias, exclude_roots=(requested_exclusion,))

    assert index.find("kept") is not None
    assert index.find("leaked") is None


def test_future_exclusion_is_recanonicalized_after_case_alias_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "ProjectCase"
    _index(project, "def kept : Nat := 0\n", "Project/Keep.lean")
    alias = tmp_path / "projectcase"
    if not alias.exists():
        pytest.skip("filesystem is case-sensitive")
    sources = open_project_sources(alias, exclude_roots=(alias / "excluded",))
    original_verify = sources.tree.verify
    created = False

    def verify_then_create_alias() -> None:
        nonlocal created
        original_verify()
        if not created:
            excluded = project / "Excluded"
            excluded.mkdir()
            (excluded / "Leak.lean").write_text("def leaked : Nat := 0\n")
            created = True

    monkeypatch.setattr(sources.tree, "verify", verify_then_create_alias)
    try:
        snapshot = sources.capture()
    finally:
        sources.close()

    assert created
    assert snapshot.index.find("kept") is not None
    assert snapshot.index.find("leaked") is None


def test_exclusion_plan_survives_root_rename_restore_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    _index(root, "def kept : Nat := 0\n", "Keep.lean")
    excluded = root / "generated"
    excluded.mkdir()
    (excluded / "Leak.lean").write_text("def leaked : Nat := 0\n")
    replacement = tmp_path / "replacement"
    _index(replacement, "def replacementOnly : Nat := 0\n", "Other.lean")
    displaced = tmp_path / "displaced"
    sources = open_project_sources(root, exclude_roots=(excluded,))
    swapped = False

    def swap() -> None:
        nonlocal swapped
        if not swapped:
            root.rename(displaced)
            replacement.rename(root)
            swapped = True

    def restore() -> None:
        if swapped:
            root.rename(replacement)
            displaced.rename(root)

    _on_checkpoint(monkeypatch, "after-directory-list", swap, relative="")
    _on_checkpoint(monkeypatch, "before-final-verification", restore, relative="")
    snapshot = None
    try:
        try:
            snapshot = sources.capture()
        except TreeChangedError:
            pass
    finally:
        sources.close()
        if displaced.exists():
            if root.exists():
                root.rename(replacement)
            displaced.rename(root)

    assert swapped
    if snapshot is not None:
        assert snapshot.index.find("kept") is not None
        assert snapshot.index.find("leaked") is None
        assert snapshot.index.find("replacementOnly") is None


def test_visible_symlinked_source_directory_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Hidden.lean").write_text("def hidden : Nat := 0\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    try:
        (project / "Src").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    index = index_project(project)

    assert index.find("hidden") is None


def test_non_lean_symlink_is_ignored(tmp_path: Path) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("not Lean\n", encoding="utf-8")
    try:
        (tmp_path / "note-link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    index = index_project(tmp_path)

    assert index.find("canonical") is not None


def test_lean_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "Outside.lean"
    outside.write_text("def escaped : Nat := 0\n", encoding="utf-8")
    try:
        (tmp_path / "Linked.lean").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(OSError, match=r"unsafe Lean source Linked\.lean: symbolic link"):
        index_project(tmp_path)


def test_editor_lock_lean_symlinks_are_skipped(tmp_path: Path) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    before = snapshot_project_sources(tmp_path)
    project = tmp_path / "Project"
    try:
        (project / ".#Basic.lean").symlink_to("editor@host.1234:1700000000")
        (project / ".#Live.lean").symlink_to(project / "Basic.lean")
    except OSError:
        pytest.skip("symlinks are unavailable")

    after = snapshot_project_sources(tmp_path)

    assert after.index.find("canonical").path == Path("Project/Basic.lean")
    assert after.index.line_counts == {Path("Project/Basic.lean"): 1}
    assert after.generation_revision == before.generation_revision


@pytest.mark.parametrize("name", [".#Lock.lean", "notes-link.txt"])
def test_ignored_symlink_target_churn_does_not_invalidate_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    _index(tmp_path, "def stableBesideLink : Nat := 0\n")
    link = tmp_path / name
    try:
        link.symlink_to("target-0")
    except OSError:
        pytest.skip("symlinks are unavailable")
    edits = 0

    def churn_link() -> None:
        nonlocal edits
        edits += 1
        link.unlink()
        link.symlink_to(f"target-{edits}")

    _on_checkpoint(monkeypatch, "before-final-verification", churn_link, relative="")

    snapshot = snapshot_project_sources(tmp_path)

    assert edits == 1
    assert snapshot.index.find("stableBesideLink") is not None


@pytest.mark.parametrize("target", ["Basic.lean", "Moved.lean"], ids=["live", "dangling"])
def test_lean_symlink_is_refused_whether_or_not_its_target_exists(tmp_path: Path, target: str) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    project = tmp_path / "Project"
    try:
        (project / "Old.lean").symlink_to(project / target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(
        lean_module.LeanSourceError,
        match=r"^unsafe Lean source Project/Old\.lean: symbolic links are not supported$",
    ) as caught:
        snapshot_project_sources(tmp_path)

    assert str(tmp_path) not in str(caught.value)


def test_case_variants_of_the_lean_suffix_are_ignored(tmp_path: Path) -> None:
    _require_descriptor_capture()
    _index(tmp_path, "def canonical : Nat := 0\n")
    project = tmp_path / "Project"
    clean = snapshot_project_sources(tmp_path)
    (project / "Y.LEAN").write_text("theorem variantY : True := trivial\n", encoding="utf-8")
    (project / "L.Lean").symlink_to("Basic.lean")
    os.mkfifo(project / "P.LEAN")
    unreadable = project / "U.Lean"
    unreadable.touch(mode=0)
    try:
        captured = snapshot_project_sources(tmp_path)
    finally:
        unreadable.chmod(0o644)

    assert set(captured.index.declarations) == {"canonical"}
    assert captured.index.line_counts == {Path("Project/Basic.lean"): 1}
    assert captured.revision == clean.revision
    assert captured.generation_revision == clean.generation_revision


def test_source_digest_preserves_surrogate_escaped_filename_bytes(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("surrogate-escaped POSIX filenames are unavailable")
    name = os.fsdecode(b"Bad_\xff.lean")
    source = tmp_path / name
    try:
        source.write_text("def unusualName : Nat := 0\n", encoding="utf-8")
    except OSError:
        pytest.skip("surrogate-escaped POSIX filenames are unavailable")

    declaration = index_project(tmp_path).find("unusualName")

    assert declaration is not None
    assert os.fsencode(declaration.path.name) == b"Bad_\xff.lean"


@pytest.mark.parametrize(
    "invalid_value",
    ["1" * 5000, "[" * 2000 + "0" + "]" * 2000],
    ids=["integer-limit", "nested-value"],
)
def test_malformed_manifest_value_does_not_escape_source_indexing(
    tmp_path: Path,
    invalid_value: str,
) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "manifest.json").write_text(
        '{"kind":"packets","schema":' + invalid_value + "}\n",
        encoding="utf-8",
    )
    (generated / "Visible.lean").write_text("def visible : Nat := 0\n", encoding="utf-8")

    index = index_project(tmp_path)

    assert index.find("visible") is not None


def test_outer_managed_marker_hides_everything_below_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _index(tmp_path, "def authored : Nat := 0\n", "Authored.lean")
    nested = tmp_path / "generated" / "nested"
    nested.mkdir(parents=True)
    (tmp_path / "generated" / "manifest.json").write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    (nested / "Copied.lean").write_text("def hiddenByOuterMarker : Nat := 0\n")
    (nested / "manifest.json").write_text("{}\n")
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    scanned: list[str] = []

    def record_scan(event: str, relative: str) -> None:
        original_checkpoint(event, relative)
        if event == "after-directory-list":
            scanned.append(relative)

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", record_scan)

    snapshot = snapshot_project_sources(tmp_path)

    assert snapshot.index.find("authored") is not None
    assert snapshot.index.find("hiddenByOuterMarker") is None
    # The scan lists every folder it walks; the root shows the hook fired.
    assert "" in scanned
    assert "generated/nested" not in scanned


def test_folded_duplicate_marker_leaves_its_folder_indexed(tmp_path: Path) -> None:
    generated = tmp_path / "review-packets"
    generated.mkdir()
    (generated / "manifest.json").write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    try:
        (generated / "MANIFEST.JSON").symlink_to("elsewhere")
    except FileExistsError:
        pytest.skip("filesystem folds letter case in names")
    except OSError:
        pytest.skip("symlinks are unavailable")
    (generated / "Visible.lean").write_text("def visibleBesideFoldedLink : Nat := 0\n")

    index = index_project(tmp_path)

    # Two folded matches are no marker, so the capture descends and reads the
    # Lean file; indexing must keep what the capture kept.
    assert index.find("visibleBesideFoldedLink") is not None


def test_empty_non_source_directory_does_not_change_lean_generation(tmp_path: Path) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n", "A.lean")
    before = snapshot_project_sources(tmp_path)

    (tmp_path / "notes").mkdir()
    after = snapshot_project_sources(tmp_path)

    assert after.revision == before.revision
    assert after.generation_revision == before.generation_revision


def test_directory_permission_churn_does_not_create_a_stale_generation_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_directory = tmp_path / "Project"
    _index(tmp_path, "def stableAcrossDirectoryMode : Nat := 0\n")
    source_directory.chmod(0o755)
    assert stat.S_IMODE(source_directory.stat().st_mode) == 0o755
    changed = False

    def change_directory_mode() -> None:
        nonlocal changed
        if not changed:
            source_directory.chmod(0o700)
            changed = True

    with monkeypatch.context() as context:
        _on_checkpoint(context, "before-final-verification", change_directory_mode, relative="")
        during_change = snapshot_project_sources(tmp_path)
    after_change = snapshot_project_sources(tmp_path)

    assert changed
    assert during_change.generation_revision == after_change.generation_revision


def test_replaced_source_changes_only_the_generation_revision(tmp_path: Path) -> None:
    source = tmp_path / "A.lean"
    source.write_text("def canonical : Nat := 0\n", encoding="utf-8")
    before = snapshot_project_sources(tmp_path)
    displaced = tmp_path / "A.lean.previous"

    source.rename(displaced)
    source.write_text("def canonical : Nat := 0\n", encoding="utf-8")
    after = snapshot_project_sources(tmp_path)

    assert after.revision == before.revision
    assert before.index.source_digest == before.revision
    assert after.index.source_digest == after.revision
    assert after.generation_revision != before.generation_revision
    assert after.index.line_counts == {Path("A.lean"): 1}


@pytest.mark.parametrize(
    ("kind", "schema"),
    [
        ("packets", "autoform-skeleton-packets/v1"),
        ("packets", "autoform-skeleton-packets/v2"),
        ("passages", "autoform-skeleton-passages/v1"),
        ("passages", "autoform-skeleton-passages/v2"),
        ("packets", "autoform-review-packets/v1"),
    ],
)
def test_managed_skeleton_output_is_not_indexed_as_project_source(
    tmp_path: Path, kind: str, schema: str
) -> None:
    packets = tmp_path / "000-review-packets"
    packet = packets / "node" / "target.lean"
    packet.parent.mkdir(parents=True)
    packet.write_text("def target : Nat := 2\n", encoding="utf-8")
    (packets / "manifest.json").write_text(
        json.dumps({"kind": kind, kind: [], "schema": schema}) + "\n",
        encoding="utf-8",
    )

    index = _index(tmp_path, "def target : Nat := 1\n", name="Actual.lean")

    assert index.find("target").path == Path("Actual.lean")


def test_managed_skeleton_output_does_not_change_source_revisions(
    tmp_path: Path,
) -> None:
    _index(tmp_path, "def target : Nat := 1\n", name="Actual.lean")
    before = snapshot_project_sources(tmp_path)
    packets = tmp_path / "review-packets"
    packet = packets / "node" / "target.lean"
    packet.parent.mkdir(parents=True)
    packet.write_text("def target : Nat := 2\n", encoding="utf-8")
    (packets / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "packets",
                "packets": [],
                "schema": "autoform-skeleton-packets/v2",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    after = snapshot_project_sources(tmp_path)

    assert after.revision == before.revision
    assert after.generation_revision == before.generation_revision


def test_managed_output_marker_prunes_before_any_descendant_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _index(tmp_path, "def authored : Nat := 0\n", "Authored.lean")
    generated = tmp_path / "review-packets"
    (generated / "nested").mkdir(parents=True)
    (generated / "nested" / "Packet.lean").write_text("def generated : Nat := 0\n")
    (generated / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "packets",
                "packets": [],
                "schema": "autoform-skeleton-packets/v2",
            }
        )
    )
    original_read = tree_snapshot_module._read_file

    def refuse_descendant(parent_descriptor, name, *args, **kwargs):
        if name == "Packet.lean":
            raise AssertionError("managed descendants must not be opened")
        return original_read(parent_descriptor, name, *args, **kwargs)

    monkeypatch.setattr(tree_snapshot_module, "_read_file", refuse_descendant)

    snapshot = snapshot_project_sources(tmp_path)

    assert snapshot.index.find("authored") is not None
    assert snapshot.index.find("generated") is None


def test_managed_output_descendant_churn_does_not_retry_source_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _index(tmp_path, "def authored : Nat := 0\n", "Authored.lean")
    generated = tmp_path / "review-packets"
    generated.mkdir()
    packet = generated / "Packet.lean"
    packet.write_text("def generated : Nat := 0\n")
    (generated / "manifest.json").write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    edits = 0

    def churn_output() -> None:
        nonlocal edits
        edits += 1
        packet.write_text(f"def generated : Nat := {edits}\n")

    _on_checkpoint(monkeypatch, "before-final-verification", churn_output, relative="")

    snapshot = snapshot_project_sources(tmp_path)

    assert edits == 1
    assert snapshot.index.find("authored") is not None
    assert snapshot.index.find("generated") is None


def test_managed_marker_folder_is_listed_once_per_look_and_never_entered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Skeleton output contributes no Lean input, so verification looks at it
    # as at any unrecorded folder: one listing to recognize its marker, and
    # no further.  The scan's listing is the only other one; the end-of-scan
    # relist skips the folder because its identity did not change.  Growth
    # inside it now counts against capture limits as a fresh scan would.
    _index(tmp_path, "def authored : Nat := 0\n", "Authored.lean")
    generated = tmp_path / "review-packets"
    generated.mkdir()
    (generated / "Packet.lean").write_text("def generated : Nat := 0\n")
    (generated / "manifest.json").write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    generated_identity = (generated.stat().st_dev, generated.stat().st_ino)
    original_scandir = tree_snapshot_module.os.scandir
    opaque_scans = 0

    def count_opaque_scans(path):
        nonlocal opaque_scans
        if isinstance(path, int):
            metadata = os.fstat(path)
            if (metadata.st_dev, metadata.st_ino) == generated_identity:
                opaque_scans += 1
        return original_scandir(path)
    original_read = tree_snapshot_module._read_file

    def refuse_descendant(parent_descriptor, name, *args, **kwargs):
        if name == "Packet.lean":
            raise AssertionError("managed descendants must not be opened")
        return original_read(parent_descriptor, name, *args, **kwargs)

    def add_irrelevant_churn() -> None:
        for index in range(100):
            (generated / f"junk-{index}").write_text("junk\n")

    monkeypatch.setattr(tree_snapshot_module.os, "scandir", count_opaque_scans)
    monkeypatch.setattr(tree_snapshot_module, "_read_file", refuse_descendant)
    _on_checkpoint(monkeypatch, "before-final-verification", add_irrelevant_churn, relative="")

    snapshot = _snapshot_in_one_attempt(monkeypatch, tmp_path)

    assert opaque_scans == 2
    assert snapshot.index.find("authored") is not None
    assert snapshot.index.find("generated") is None


def test_managed_output_marker_rewrite_is_captured_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Skeleton output contributes no Lean input, so its manifest is not kept:
    # a rewrite that is still recognized leaves the folder hidden and is not
    # a change.
    _index(tmp_path, "def authored : Nat := 0\n", "Authored.lean")
    generated = tmp_path / "review-packets"
    generated.mkdir()
    (generated / "Packet.lean").write_text("def generated : Nat := 0\n")
    marker = generated / "manifest.json"
    marker.write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    changed = False

    def change_marker_once() -> None:
        nonlocal changed
        if not changed:
            changed = True
            marker.write_text(
                '{"kind": "packets", "packets": [], '
                '"schema": "autoform-skeleton-packets/v2"}\n'
            )

    _on_checkpoint(monkeypatch, "before-final-verification", change_marker_once, relative="")
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)

    snapshot = _snapshot_in_one_attempt(monkeypatch, tmp_path)

    assert changed
    assert snapshot.index.find("authored") is not None
    assert snapshot.index.find("generated") is None


def test_managed_marker_at_requested_root_never_prunes_the_root(tmp_path: Path) -> None:
    (tmp_path / "Root.lean").write_text("def rootSource : Nat := 0\n")
    (tmp_path / "manifest.json").write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )

    index = index_project(tmp_path)

    assert index.find("rootSource") is not None


def test_root_managed_marker_churn_does_not_invalidate_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "Root.lean").write_text("def stableRootSource : Nat := 0\n")
    marker = tmp_path / "manifest.json"
    marker.write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    edits = 0

    def churn_root_marker() -> None:
        nonlocal edits
        edits += 1
        marker.write_text(f"{{\"irrelevant\": {edits}}}\n")

    _on_checkpoint(monkeypatch, "before-final-verification", churn_root_marker, relative="")

    snapshot = snapshot_project_sources(tmp_path)

    assert edits == 1
    assert snapshot.index.find("stableRootSource") is not None


@pytest.mark.parametrize(
    "limits",
    [
        TreeCaptureLimits(max_entries=2),
        TreeCaptureLimits(max_total_bytes=1),
    ],
)
def test_managed_marker_surface_obeys_capture_limits(
    tmp_path: Path,
    limits: TreeCaptureLimits,
) -> None:
    generated = tmp_path / "review-packets"
    generated.mkdir()
    (generated / "Packet.lean").write_text("def generated : Nat := 0\n")
    (generated / "manifest.json").write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )

    with pytest.raises(lean_module.LeanSourceError, match="directory tree exceeds"):
        snapshot_project_sources(tmp_path, limits=limits)


def test_ambiguous_managed_markers_do_not_prune_source(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "review-packets"
    generated.mkdir()
    lower = generated / "manifest.json"
    upper = generated / "MANIFEST.JSON"
    lower.write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    upper.write_text("{}\n")
    if lower.samefile(upper):
        pytest.skip("filesystem does not permit case-colliding marker names")
    (generated / "Visible.lean").write_text("def visibleBesideAmbiguity : Nat := 0\n")

    index = index_project(tmp_path)

    assert index.find("visibleBesideAmbiguity") is not None


def test_managed_marker_case_alias_still_prunes_generated_source(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "review-packets"
    generated.mkdir()
    (generated / "MANIFEST.JSON").write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    (generated / "Hidden.lean").write_text("def hiddenByAlias : Nat := 0\n")

    index = index_project(tmp_path)

    assert index.find("hiddenByAlias") is None


def test_nonregular_managed_marker_does_not_prune_source(tmp_path: Path) -> None:
    generated = tmp_path / "review-packets"
    generated.mkdir()
    outside = tmp_path / "outside-manifest.json"
    outside.write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    try:
        (generated / "manifest.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    (generated / "Visible.lean").write_text("def visibleBesideLink : Nat := 0\n")

    index = index_project(tmp_path)

    assert index.find("visibleBesideLink") is not None


def test_large_managed_skeleton_manifest_still_excludes_generated_sources(
    tmp_path: Path,
) -> None:
    _index(tmp_path, "def target : Nat := 1\n", name="Actual.lean")
    packets = tmp_path / "000-review-packets"
    packet = packets / "node" / "target.lean"
    packet.parent.mkdir(parents=True)
    packet.write_text("def target : Nat := 2\n", encoding="utf-8")
    payload = json.dumps(
        {
            "kind": "packets",
            "packets": ["x" * (1024 * 1024 + 1)],
            "schema": "autoform-skeleton-packets/v2",
        },
        sort_keys=True,
    )
    (packets / "manifest.json").write_text(payload, encoding="utf-8")

    index = index_project(tmp_path)

    assert index.find("target").path == Path("Actual.lean")


def test_oversized_managed_manifest_leaves_its_directory_indexed_at_its_read_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packets = tmp_path / "review-packets"
    packet = packets / "node" / "target.lean"
    packet.parent.mkdir(parents=True)
    packet.write_text("def oversizedPacket : Nat := 0\n", encoding="utf-8")
    (packets / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "packets",
                "packets": ["x" * 256],
                "schema": "autoform-skeleton-packets/v2",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    observed_lengths: list[int] = []
    original = lean_module._is_managed_output_manifest_bytes

    def record_length(data: bytes) -> bool:
        observed_lengths.append(len(data))
        return original(data)

    monkeypatch.setattr(lean_module, "_MANAGED_OUTPUT_MANIFEST_BYTE_LIMIT", 64)
    monkeypatch.setattr(lean_module, "_is_managed_output_manifest_bytes", record_length)

    index = index_project(tmp_path)

    # The engine refuses bytes past the bound before it asks the recognizer.
    assert observed_lengths == []
    assert index.find("oversizedPacket") is not None


def test_only_a_complete_publication_manifest_hides_its_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Render copies a blueprint's Lean files into its site and mkdocs copies the
    # site again, manifest included; both copies go stale when the source changes.
    complete = {"complete": True, "schema": "autoform-publication/v2"}
    manifests = {
        "site-src": complete,
        "site": complete,
        "legacy": {**complete, "schema": "autoform-publication/v1"},
        "unfinished": {**complete, "complete": False},
        "web": {"name": "web app"},
        "padded": {**complete, "padding": "x" * 64},
        "object-schema": {**complete, "schema": {}},
        "array-schema": {**complete, "schema": []},
    }
    for folder, manifest in manifests.items():
        (tmp_path / folder / "Copy").mkdir(parents=True)
        (tmp_path / folder / "publication.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / folder / "Copy" / "Proofs.lean").write_text("theorem stale : True := trivial\n")
    (tmp_path / "Proofs.lean").write_text("theorem renamed : True := trivial\n")
    monkeypatch.setattr(lean_module, "PUBLICATION_MANIFEST_MAX_BYTES", 64, raising=False)

    snapshot = snapshot_project_sources(tmp_path)

    assert sorted(path.as_posix() for path, _data in snapshot.source_files) == [
        "Proofs.lean",
        "array-schema/Copy/Proofs.lean",
        "object-schema/Copy/Proofs.lean",
        "padded/Copy/Proofs.lean",
        "unfinished/Copy/Proofs.lean",
        "web/Copy/Proofs.lean",
    ]


def test_unfinished_packet_stage_is_not_indexed_as_project_source(tmp_path: Path) -> None:
    # A stage an older writer left without its manifest sorts before the
    # project's sources and would shadow the real declaration.
    packet = tmp_path / ".review-packets.autoform-stage-0123456789abcdef" / "blind" / "0123.lean"
    packet.parent.mkdir(parents=True)
    packet.write_text("def target : Nat := 2\n", encoding="utf-8")

    index = _index(tmp_path, "def target : Nat := 1\n", name="Actual.lean")

    assert index.find("target").path == Path("Actual.lean")


def test_irreducible_definitions_are_indexed(tmp_path: Path) -> None:
    index = _index(tmp_path, "namespace A\nirreducible_def b : Nat := 1\nend A\n")

    assert index.find("A.b").keyword == "irreducible_def"


def test_batteries_wanted_commands_are_indexed_with_their_keyword(tmp_path: Path) -> None:
    source = [
        "namespace W",
        # The module system's `public`, as atlas-lean writes its wanted results.
        "public theorem_wanted sturm (p : Nat) (hp : 0 < p) :",
        "    p = p",
        "private theorem_wanted lower : True",
        "proof_wanted legacy : 1 = 1",
        "@[simp] def_wanted η : Nat",
        "def_wanted derived : Nat := ❰η❱ + 1",
        "instance_wanted instFoo : Inhabited Nat",
        "instance_wanted : Nonempty Nat",
        "public theorem stated : True := trivial",
        "end W",
    ]
    index = _index(tmp_path, "\n".join(source) + "\n")

    assert {name: declaration.keyword for name, declaration in index.declarations.items()} == {
        "W.sturm": "theorem_wanted",
        "W.lower": "theorem_wanted",
        "W.legacy": "proof_wanted",
        "W.η": "def_wanted",
        "W.derived": "def_wanted",
        "W.instFoo": "instance_wanted",
        "W.stated": "theorem",
    }


def test_aliases_are_indexed_with_where_their_target_can_be(tmp_path: Path) -> None:
    source = [
        "namespace A.B",
        "alias u := t",
        '@[deprecated (since := "2025-01-01")] protected alias «old u» := _root_.Nat.succ_le',
        "alias ⟨mp, _⟩ := t_iff",
        "alias ⟨_, mpr⟩ := t_iff",
        "alias u2:=A.u",
        "alias near :=",
        "  t",
        "alias far :=",
        "",
        "  t",
        "private alias _root_.top := «x.y»",
        # Without Batteries, `alias` is an ordinary identifier.
        "  alias ++ s",
        "  alias x",
        "end A.B",
    ]
    index = _index(tmp_path, "\n".join(source) + "\n")

    t = ("A.B.t", "A.t", "t")
    assert {name: (declaration.protected, declaration.alias) for name, declaration in index.declarations.items()} == {
        "A.B.u": (False, Alias(t, atomic=True, iff=False)),
        "A.B.«old u»": (True, Alias(("Nat.succ_le",), atomic=False, iff=False)),
        "A.B.mp": (False, Alias(("A.B.t_iff", "A.t_iff", "t_iff"), atomic=True, iff=True)),
        "A.B.mpr": (False, Alias(("A.B.t_iff", "A.t_iff", "t_iff"), atomic=True, iff=True)),
        "A.B.u2": (False, Alias(("A.B.A.u", "A.A.u", "A.u"), atomic=False, iff=False)),
        "A.B.near": (False, Alias(t, atomic=True, iff=False)),
        "A.B.far": (False, Alias(t, atomic=True, iff=False)),
        "top": (False, Alias(("A.B.«x.y»", "A.«x.y»", "«x.y»"), atomic=True, iff=False)),
    }
    assert {declaration.keyword for declaration in index.declarations.values()} == {"alias"}
    assert index.find("A.B.mpr").line == 5


def test_root_qualified_names_leave_their_namespace_out(tmp_path: Path) -> None:
    index = _index(tmp_path, "namespace A\ntheorem _root_.B.c : True := trivial\ntheorem d : True := trivial\nend A\n")

    assert set(index.declarations) == {"B.c", "A.d"}


def test_an_alias_is_followed_only_to_an_earlier_declaration_of_its_file(tmp_path: Path) -> None:
    source = [
        "namespace X",
        "private theorem hidden : True := trivial",
        "alias nearby := hidden",
        "theorem base : True := trivial",
        "alias chain := nearby",
        "protected def p : Nat := 0",
        "alias skips := p",
        "protected",
        "theorem q : True := trivial",
        "alias skipsAbove := q",
        "alias outside := Nat.succ_le_succ",
        "alias partway := outside",
        "end X",
        "theorem t : True := trivial",
        "namespace A",
        "alias t := t",
        "end A",
        "alias dotted := X.p",
    ]
    (tmp_path / "A.lean").write_text("\n".join(source) + "\n", encoding="utf-8")
    # `X.base` is in another file, on a later line than this alias.
    (tmp_path / "B.lean").write_text("def base : Nat := 0\nnamespace X\nalias remote := base\nend X\n", encoding="utf-8")
    index = index_project(tmp_path)

    assert index.reached(index.find("X.chain")) == index.find("X.hidden")
    assert index.reached(index.find("dotted")) == index.find("X.p")
    # The alias is the innermost candidate of its own target, and is not declared yet.
    assert index.alias_target(index.find("A.t")) == index.find("t")
    # Another file may not be imported, the module system may have made a
    # protected name private, which Lean does not skip, and a chain whose end
    # is outside the index leads nowhere sure.
    for name in ("X.remote", "X.skips", "X.skipsAbove", "X.partway"):
        assert index.reached(index.find(name)) is None, name


def test_an_alias_has_no_sure_target_before_its_file_declares_one(tmp_path: Path) -> None:
    # B.lean declares its `N.aux` only after the alias and A.lean declares one
    # too, so the alias has no sure target.
    (tmp_path / "A.lean").write_text(
        "namespace N\nprivate theorem aux : True := trivial\nend N\n",
        encoding="utf-8",
    )
    (tmp_path / "B.lean").write_text(
        "theorem aux : True := trivial\nnamespace N\nalias good := aux\nprivate theorem aux : True := trivial\nend N\n",
        encoding="utf-8",
    )

    index = index_project(tmp_path)
    assert index.reached(index.find("N.good")) is None


def _where(declarations) -> list[tuple[str, int]]:
    return [(declaration.path.as_posix(), declaration.line) for declaration in declarations]


_PUBLIC_MAIN = "namespace Demo\ntheorem main : True := trivial\nend Demo\n"
_PRIVATE_MAIN = "private theorem Demo.main : True := trivial\n"


@pytest.mark.parametrize(
    ("files", "occurrences", "selected"),
    [
        (
            {"A.lean": f"@[simp] {_PRIVATE_MAIN}", "B.lean": _PUBLIC_MAIN},
            [("A.lean", 1), ("B.lean", 2)],
            [("B.lean", 2)],
        ),
        (
            {"A.lean": "@[simp]\nprivate\ntheorem Demo.main : True := trivial\n", "B.lean": _PUBLIC_MAIN},
            [("A.lean", 3), ("B.lean", 2)],
            [("B.lean", 2)],
        ),
        (
            {"A.lean": _PUBLIC_MAIN, "B.lean": "theorem bar : True := trivial\nprivate alias Demo.main := bar\n"},
            [("A.lean", 2), ("B.lean", 2)],
            [("A.lean", 2)],
        ),
        ({"A.lean": _PUBLIC_MAIN, "B.lean": _PRIVATE_MAIN}, [("A.lean", 2), ("B.lean", 1)], [("A.lean", 2)]),
        # Batteries makes a wanted command a private placeholder, so the
        # theorem that fills it may stand beside it.
        (
            {"A.lean": "theorem_wanted Demo.main : True\n", "B.lean": _PUBLIC_MAIN},
            [("A.lean", 1), ("B.lean", 2)],
            [("B.lean", 2)],
        ),
        ({"A.lean": "\n" + _PRIVATE_MAIN}, [("A.lean", 2)], [("A.lean", 2)]),
        (
            {"A.lean": "theorem Demo.main : True := trivial\n", "B.lean": _PRIVATE_MAIN, "C.lean": _PUBLIC_MAIN},
            [("A.lean", 1), ("B.lean", 1), ("C.lean", 2)],
            [],
        ),
        (
            {"A.lean": _PRIVATE_MAIN, "B.lean": "\nprivate def Demo.main : Nat := 0\n"},
            [("A.lean", 1), ("B.lean", 2)],
            [],
        ),
        ({"A.lean": _PRIVATE_MAIN, "B.lean": "\nproof_wanted Demo.main : True\n"}, [("A.lean", 1), ("B.lean", 2)], []),
        # A universe binder on the keyword's line leaves the name as it is.
        ({"A.lean": "def Demo.main.{u} : Nat := 0\n", "B.lean": _PUBLIC_MAIN}, [("A.lean", 1), ("B.lean", 2)], []),
    ],
    ids=[
        "earlier-private", "private-above", "private-alias", "later-private", "wanted",
        "lone-private", "two-public", "two-private", "private-wanted", "universe-binder",
    ],
)
def test_a_name_selects_its_one_public_declaration_or_is_ambiguous(
    tmp_path: Path, files: dict[str, str], occurrences: list[tuple[str, int]], selected: list[tuple[str, int]]
) -> None:
    index = _index_files(tmp_path, files)

    assert _where(filter(None, [index.find("Demo.main")])) == selected
    assert _where(index.occurrences("Demo.main")) == occurrences
    # A name that selects nothing lists every declaration.
    ambiguity = "" if selected else ", ".join(f"{path}:{line}" for path, line in occurrences)
    assert index.ambiguity("Demo.main") == ambiguity


def test_one_short_name_in_distinct_namespaces_is_no_duplicate(tmp_path: Path) -> None:
    index = _index_files(
        tmp_path,
        {
            "A.lean": "namespace A\ndef x : Nat := 0\nend A\n",
            "B.lean": "theorem B.x : True := trivial\nnamespace C\nprivate theorem x : True := trivial\nend C\n",
        },
    )

    assert _where(index.find(name) for name in ("A.x", "B.x", "C.x")) == [("A.lean", 2), ("B.lean", 1), ("B.lean", 3)]
    assert index.duplicates == {}


def test_an_alias_of_a_private_declaration_names_its_own_file_one(tmp_path: Path) -> None:
    # Inside B.lean, `foo` is the private one above the alias: importing A.lean
    # there would clash with it.
    index = _index_files(
        tmp_path,
        {
            "A.lean": "theorem foo : True := trivial\n",
            "B.lean": "private def foo : Nat := 0\nalias bar := foo\n",
        },
    )

    assert _where([index.find("foo"), index.alias_target(index.find("bar"))]) == [("A.lean", 1), ("B.lean", 1)]


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


@pytest.mark.parametrize("everywhere", [False, True])
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("/- a /- b -/ c -/\ndef d : Nat := 6", "def d : Nat := 6"),
        ('def s := "a\\"--b" -- x', 'def s := "a\\"--b"'),
        ('def s := r#"a"--b"# -- x', 'def s := r#"a"--b"#'),
        ("def c : Char := '\"' -- x", "def c : Char := '\"'"),
        ('def s := s!"{ {a := 1}.a ++ "--" }" -- x', 'def s := s!"{ {a := 1}.a ++ "--" }"'),
        ('def s := s!"{«a--b»}" -- x', 'def s := s!"{«a--b»}"'),
        ('def s := s!"{a /- c -/}" -- x', 'def s := s!"{a  }"'),
        ("def «a--b» := 1 -- x", "def «a--b» := 1"),
        ('def s := s!"{x -- }\n}" -- x', 'def s := s!"{x\n}"'),
        ('def s := s!"{r#"a"--b"#}" -- x', 'def s := s!"{r#"a"--b"#}"'),
        ("def s := s!\"{'{'}\" -- x", "def s := s!\"{'{'}\""),
        # An `r` that ends a name, or a name literal's, opens no raw string.
        ('def x := throwError"a\\"--b" -- c', 'def x := throwError"a\\"--b"'),
        ('def x := `r"a\\"--b" -- c', 'def x := `r"a\\"--b"'),
        # An unterminated raw string runs to the end of the text.
        ('def s := r"a -- x', 'def s := r"a -- x'),
    ],
)
def test_comment_stripping_follows_every_lexer_state(
    source: str, expected: str, everywhere: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stripper copies the text between two event matches unread. Matching
    # every position makes it read each character, so both modes give the same
    # output only if no event pattern misses a position some branch reacts to.
    if everywhere:
        for name in ("_CODE_EVENT", "_INTERPOLATION_EVENT", "_LITERAL_EVENT"):
            monkeypatch.setattr(lean_module, name, re.compile(r"(?s)."))
    assert strip_lean_comments(source) == expected


def test_a_raw_string_ends_at_its_first_closer(tmp_path: Path) -> None:
    # Lean reads no escapes in a raw string, so `r"\"` holds one backslash and
    # the comments after it are comments, not string contents.
    hidden = _index(
        tmp_path / "hidden",
        'def sep : String := r"\\"\n/-\ntheorem hidden : True := trivial\n-/\ndef after : Nat := 1\n',
    )
    real = _index(
        tmp_path / "real",
        'def sep : String := r"\\"\ndef o : String := "/-"\ntheorem real : True := trivial\ndef c : String := "-/"\n',
    )

    assert set(hidden.declarations) == {"sep", "after"}
    assert set(real.declarations) == {"sep", "o", "real", "c"}


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


def test_permalink_encodes_ref_and_source_path_segments(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        "theorem target : True := by trivial\n",
        name="Project/Hash#File.lean",
    )
    linker = SourceLinker(
        index=index,
        repository_url="https://github.com/owner/repo",
        ref="feature/topic",
    )

    assert linker.url("target") == (
        "https://github.com/owner/repo/blob/feature%2Ftopic/"
        "Project/Hash%23File.lean#L1"
    )


def test_permalink_percent_encodes_source_path_components(tmp_path: Path) -> None:
    linker = SourceLinker(
        index=_index(
            tmp_path,
            "def encodedPath : Nat := 0\n",
            "Part#1/A file.lean",
        ),
        repository_url="https://github.com/owner/repo",
        ref="deadbeef",
    )

    assert linker.url("encodedPath") == (
        "https://github.com/owner/repo/blob/deadbeef/Part%231/A%20file.lean#L1"
    )


def test_permalink_names_the_lean_root_s_path_in_the_repository(tmp_path: Path) -> None:
    linker = SourceLinker(
        index=_index(tmp_path),
        repository_url="https://github.com/owner/repo",
        ref="deadbeef",
        # Spellings are keyed by repository path, as the attestation records them.
        spellings={"v 1/Project/Basic.lean": "v 1/Project/basic.lean"},
        root_prefix="v 1",
    )

    assert linker.url("Outer.alpha") == (
        "https://github.com/owner/repo/blob/deadbeef/v%201/Project/basic.lean#L6"
    )


def test_no_link_without_repository_coordinates(tmp_path: Path) -> None:
    linker = SourceLinker(index=_index(tmp_path))

    assert linker.url("Outer.alpha") is None
    # The location is still known, so the page can still say where the code is.
    assert linker.location("Outer.alpha") is not None


def _git_commit(root: Path, message: str, path: str = ".") -> str:
    subprocess.run(["git", "add", path], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Autoform Tests",
            "-c",
            "user.email=autoform@example.invalid",
            "commit",
            "-m",
            message,
        ],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_git_repository(
    root: Path,
    *,
    object_format: str = "sha1",
    origin: str = "https://github.com/owner/repo.git",
) -> str:
    initialized = subprocess.run(
        ["git", "init", f"--object-format={object_format}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if initialized.returncode != 0:
        pytest.skip(f"Git does not support {object_format} repositories")
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=root, check=True)
    return _git_commit(root, "fixture")


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_snapshot_attestation_accepts_only_bytes_present_in_the_commit(
    tmp_path: Path,
    object_format: str,
) -> None:
    root = tmp_path / object_format
    root.mkdir()
    committed = root / "Committed.lean"
    committed.write_text("def committed : Nat := 0\n")
    dirty = root / "Dirty.lean"
    dirty.write_text("def dirty : Nat := 0\n")
    commit = _init_git_repository(root, object_format=object_format)
    dirty.write_text("def dirty : Nat := 1\n")

    assert verify_repository_snapshot(root, commit, ((committed, committed.read_bytes()),)) == commit
    assert verify_repository_snapshot(
        root, commit, ((committed, committed.read_bytes()), (dirty, dirty.read_bytes()))
    ) is None


def test_tree_lookup_matches_the_spelling_git_committed_only_when_asked_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_disk = "Poincare\u0301\u212b.lean"  # NFD é, then U+212B ANGSTROM SIGN
    committed = "Poincar\u00e9\u212b.lean"  # what macOS Git commits and prints: é composed, the sign kept
    oid = "a" * 40

    def ls_tree(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, f"100644 blob {oid}\t{committed}\0".encode(), b"")

    monkeypatch.setattr(lean_module.subprocess, "run", ls_tree)
    spellings: dict[str, str] = {}

    assert lean_module._git_tree_oids(tmp_path, "c" * 40, (on_disk,), spellings) == {on_disk: oid}
    assert spellings == {on_disk: committed}
    assert lean_module._git_tree_oids(tmp_path, "c" * 40, (on_disk,)) == {committed: oid}


def test_permalink_names_the_spelling_git_committed(tmp_path: Path) -> None:
    linker = SourceLinker(
        index=_index(tmp_path, "def decomposed : Nat := 0\n", "Poincare\u0301\u212b.lean"),
        repository_url="https://github.com/owner/repo",
        ref="deadbeef",
        spellings={"Poincare\u0301\u212b.lean": "Poincar\u00e9\u212b.lean"},
    )

    assert linker.url("decomposed") == "https://github.com/owner/repo/blob/deadbeef/Poincar%C3%A9%E2%84%AB.lean#L1"


def test_snapshot_attestation_ignores_git_replace_objects(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "A.lean"
    source.write_text("def originalTree : Nat := 0\n")
    original = _init_git_repository(root)
    source.write_text("def replacementTree : Nat := 0\n")
    replacement = _git_commit(root, "replacement", "A.lean")
    subprocess.run(["git", "checkout", "--detach", original], cwd=root, check=True)
    source.write_text("def replacementTree : Nat := 0\n")
    subprocess.run(["git", "replace", original, replacement], cwd=root, check=True)

    assert verify_repository_snapshot(root, original, ((source, source.read_bytes()),)) is None
    assert verify_repository_snapshot(
        root, original, ((source, b"def originalTree : Nat := 0\n"),)
    ) == original


def test_git_runner_ignores_inherited_repo_and_ci_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    first.mkdir()
    (first / "A.lean").write_text("def stableRepository : Nat := 0\n")
    first_commit = _init_git_repository(first, origin="https://github.com/owner/A.git")
    second = tmp_path / "second"
    second.mkdir()
    (second / "A.lean").write_text("def stableRepository : Nat := 0\n")
    (second / "OnlyB.txt").write_text("different tree\n")
    second_commit = _init_git_repository(second, origin="https://github.com/owner/B.git")
    monkeypatch.setenv("GIT_DIR", str(second / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(second))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/owner/evil.git")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(second))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/ci-wrong")
    monkeypatch.setenv("GITHUB_SHA", second_commit)

    source = first / "A.lean"

    assert first_commit != second_commit
    assert detect_repository_url(first) == "https://github.com/owner/A"
    assert detect_ref(first) == first_commit
    assert verify_repository_snapshot(first, "HEAD", ((source, source.read_bytes()),)) == first_commit
    assert verify_repository_snapshot(first, second_commit, ((source, source.read_bytes()),)) is None


def test_snapshot_attestation_accepts_a_clean_checkout_with_eol_conversion(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitattributes").write_text("*.lean text eol=crlf\n")
    source = tmp_path / "A.lean"
    source.write_text("def converted : Nat := 0\n")
    commit = _init_git_repository(tmp_path)
    source.unlink()
    subprocess.run(["git", "checkout", "--", "A.lean"], cwd=tmp_path, check=True)
    assert source.read_bytes() == b"def converted : Nat := 0\r\n"

    assert verify_repository_snapshot(
        tmp_path,
        commit,
        ((source, source.read_bytes()),),
    ) == commit
    assert verify_repository_snapshot(
        tmp_path,
        commit,
        ((source, b"def changed : Nat := 0\r\n"),),
    ) is None


def test_snapshot_attestation_compares_a_subdirectory_s_files_at_their_repository_paths(
    tmp_path: Path,
) -> None:
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    source = lean_root / "A.lean"
    source.write_text("def tracked : Nat := 0\n")
    notes = tmp_path / "blueprint" / "notes.md"
    notes.parent.mkdir()
    notes.write_text("# Notes\n")
    commit = _init_git_repository(tmp_path)
    reasons: list[str] = []

    # A file beside the Lean root counts too: the blueprint may sit next to the Lake project.
    assert verify_repository_snapshot(
        lean_root, commit, ((source, source.read_bytes()), (notes, notes.read_bytes())), reasons=reasons
    ) == commit
    assert verify_repository_snapshot(
        lean_root, commit, ((source, b"def changed : Nat := 0\n"),), reasons=reasons
    ) is None
    assert reasons == [f"lean/A.lean differs from commit {commit[:12]}"]


def test_snapshot_attestation_names_why_it_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "A.lean").write_text("def tracked : Nat := 0\n")
    commit = _init_git_repository(tmp_path)
    reasons: list[str] = []
    untracked = tmp_path / "Scratch.lean"
    untracked.write_text("def scratch : Nat := 0\n")

    assert verify_repository_snapshot(
        tmp_path, commit, ((untracked, untracked.read_bytes()),), reasons=reasons
    ) is None
    assert reasons == [f"Scratch.lean is not a file in commit {commit[:12]}"]

    monkeypatch.setattr(lean_module, "repository_top_level", lambda root: None)
    reasons.clear()
    assert verify_repository_snapshot(
        tmp_path, commit, ((untracked, untracked.read_bytes()),), reasons=reasons
    ) is None
    assert reasons == [
        "the Lean root is outside its Git repository's working tree, or spelled other than as Git spells it"
    ]


def test_repository_top_level_is_spelled_as_the_root_is(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "v1" / "Sub").mkdir(parents=True)
    (repo / "v1" / "Sub" / "A.lean").write_text("def a : Nat := 0\n")
    _init_git_repository(repo)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(repo, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert repository_top_level(repo) == repo
    assert repository_top_level(repo / "v1" / "Sub") == repo
    # Git prints the real path; the top level keeps the spelling it was given.
    assert repository_top_level(alias / "v1" / "Sub") == alias
    assert repository_top_level(repo / ".git") is None


def test_repository_top_level_compares_git_s_path_with_the_root_s_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # macOS Git prints a decomposed name composed.
    monkeypatch.setattr(lean_module, "_git", lambda root, *arguments: "true\nTh\u00e9/")
    assert repository_top_level(tmp_path / "The\u0301") == tmp_path
    assert repository_top_level(tmp_path / "other") is None

    # U+212B ANGSTROM SIGN, which NFC replaces on both sides.
    monkeypatch.setattr(lean_module, "_git", lambda root, *arguments: "true\n\u212b/")
    assert repository_top_level(tmp_path / "\u212b") == tmp_path

    monkeypatch.setattr(lean_module, "_git", lambda root, *arguments: "true\na/b/v1/")
    assert repository_top_level(Path("/v1")) is None


def test_repository_top_level_is_unknown_when_git_prints_a_name_that_is_not_utf_8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def undecodable(root: Path, *arguments: str) -> str:
        raise UnicodeDecodeError("utf-8", b"caf\xe9/", 3, 4, "invalid continuation byte")

    monkeypatch.setattr(lean_module, "_git", undecodable)

    assert repository_top_level(tmp_path) is None


