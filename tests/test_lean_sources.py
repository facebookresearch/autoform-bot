from __future__ import annotations

import os
from pathlib import Path

import pytest

from autoform_cli import _tree_snapshot as tree_snapshot_module
from autoform_cli import lean as lean_module
from autoform_cli import workspace as workspace_module
from autoform_cli._tree_snapshot import TreeSnapshot, TreeSnapshotError
from autoform_cli.lean import (
    SourceLinker,
    declaration_kind,
    declaration_keywords,
    declaration_names,
    index_project,
    mathlib_module_name,
    open_project_sources,
    snapshot_project_sources,
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


def test_hidden_lean_source_directories_are_indexed(tmp_path: Path) -> None:
    hidden = tmp_path / ".proofs"
    hidden.mkdir()
    (hidden / "Hidden.lean").write_text("def hiddenProof : Nat := 0\n", encoding="utf-8")

    assert index_project(tmp_path).find("hiddenProof") is not None


@pytest.mark.parametrize("directory", [".direnv", ".obsidian", ".trash", ".venv"])
def test_known_tooling_directories_are_not_scanned(
    tmp_path: Path,
    directory: str,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    ignored = tmp_path / directory
    ignored.mkdir()
    try:
        (ignored / "external").symlink_to(tmp_path.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    index = index_project(tmp_path)

    assert index.find("canonical") is not None


def test_changes_inside_an_excluded_build_directory_do_not_invalidate_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    build_state = tmp_path / ".lake" / "build-state"
    build_state.parent.mkdir()
    build_state.write_text("old\n", encoding="utf-8")
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    changed = False

    def change_excluded_file(event: str, relative: str) -> None:
        nonlocal changed
        original_checkpoint(event, relative)
        if event == "before-final-verification" and not changed:
            changed = True
            build_state.write_text("new\n", encoding="utf-8")

    monkeypatch.setattr(
        tree_snapshot_module,
        "_tree_snapshot_checkpoint",
        change_excluded_file,
    )
    sources = open_project_sources(tmp_path)
    try:
        snapshot = sources.capture()
    finally:
        sources.close()

    assert changed
    assert snapshot.index.find("canonical") is not None


@pytest.mark.parametrize("portable", [False, True])
def test_closed_source_binding_rejects_later_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    if portable:
        monkeypatch.setattr(workspace_module, "_DIRECTORY_BINDING_SUPPORTED", False)
    sources = open_project_sources(tmp_path)

    sources.close()

    with pytest.raises(TreeSnapshotError, match="closed"):
        sources.capture()


def test_explicit_and_publication_staging_roots_are_skipped(tmp_path: Path) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n", "Project/Basic.lean")
    excluded = tmp_path / "site"
    excluded.mkdir()
    (excluded / "Copied.lean").write_text("def copied : Nat := 0\n", encoding="utf-8")
    staging = tmp_path / ".autoform-publication-site-random/source"
    staging.mkdir(parents=True)
    (staging / "Staged.lean").write_text("def staged : Nat := 0\n", encoding="utf-8")

    index = index_project(tmp_path, exclude_roots=(excluded,))

    assert index.find("canonical") is not None
    assert index.find("copied") is None
    assert index.find("staged") is None


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


def test_portable_exclusion_survives_case_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "ProjectCase"
    _index(project, "def kept : Nat := 0\n", "Project/Keep.lean")
    excluded = project / "Excluded"
    excluded.mkdir()
    (excluded / "Leaked.lean").write_text("def leaked : Nat := 0\n", encoding="utf-8")
    alias = tmp_path / "projectcase"
    if not alias.exists():
        pytest.skip("filesystem is case-sensitive")
    monkeypatch.setattr(workspace_module, "_DIRECTORY_BINDING_SUPPORTED", False)

    index = index_project(alias, exclude_roots=(alias / "excluded",))

    assert index.find("kept") is not None
    assert index.find("leaked") is None


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


def test_portable_source_scanner_rejects_a_directory_reparse_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Src"
    source.mkdir()
    (source / "Hidden.lean").write_text("def hidden : Nat := 0\n", encoding="utf-8")
    source_identity = (source.stat().st_dev, source.stat().st_ino)
    original = tree_snapshot_module._is_reparse_point

    def mark_source(metadata) -> bool:
        return (metadata.st_dev, metadata.st_ino) == source_identity or original(metadata)

    monkeypatch.setattr(workspace_module, "_DIRECTORY_BINDING_SUPPORTED", False)
    monkeypatch.setattr(tree_snapshot_module, "_is_reparse_point", mark_source)

    with pytest.raises(OSError, match="directory tree changed while it was captured"):
        index_project(tmp_path)


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


def test_generated_publication_roots_are_never_indexed(tmp_path: Path) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n", "blueprint/Proofs.lean")
    generated = tmp_path / "aaa-output"
    generated.mkdir()
    (generated / "publication.json").write_text(
        '{"schema":"autoform-publication/v2"}\n', encoding="utf-8"
    )
    (generated / "Copied.lean").write_text(
        "def generatedOnly : Nat := 0\n", encoding="utf-8"
    )

    index = index_project(tmp_path)

    assert index.find("canonical") is not None
    assert index.find("generatedOnly") is None


def test_oversized_publication_manifest_read_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "publication.json").write_bytes(
        b"x" * (lean_module._PUBLICATION_MANIFEST_BYTE_LIMIT + 4096)
    )
    observed_lengths: list[int] = []
    original = lean_module._is_publication_manifest_bytes

    def record_length(data: bytes) -> bool:
        observed_lengths.append(len(data))
        return original(data)

    monkeypatch.setattr(lean_module, "_is_publication_manifest_bytes", record_length)

    index_project(tmp_path)

    assert observed_lengths == [lean_module._PUBLICATION_MANIFEST_BYTE_LIMIT + 1]


def test_bounded_publication_manifest_capture_fills_short_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "publication.json").write_text(
        '{"schema":"autoform-publication/v2"}\n', encoding="utf-8"
    )
    (generated / "Copied.lean").write_text(
        "def copiedAfterShortRead : Nat := 0\n", encoding="utf-8"
    )
    original_fdopen = tree_snapshot_module.os.fdopen

    class ShortReadStream:
        def __init__(self, stream) -> None:
            self._stream = stream

        def read(self, size: int = -1) -> bytes:
            return self._stream.read(min(size, 3) if size >= 0 else size)

        def close(self) -> None:
            self._stream.close()

    def short_fdopen(*args, **kwargs):
        return ShortReadStream(original_fdopen(*args, **kwargs))

    monkeypatch.setattr(tree_snapshot_module.os, "fdopen", short_fdopen)

    index = index_project(tmp_path)

    assert index.find("canonical") is not None
    assert index.find("copiedAfterShortRead") is None


def test_publication_marker_survives_case_alias(tmp_path: Path) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    generated = tmp_path / "generated"
    generated.mkdir()
    marker = generated / "publication.json"
    marker.write_text('{"schema":"autoform-publication/v2"}\n', encoding="utf-8")
    physical_marker = generated / "PUBLICATION.JSON"
    marker.rename(physical_marker)
    if not marker.exists():
        pytest.skip("filesystem is case-sensitive")
    (generated / "Copied.lean").write_text(
        "def copiedThroughMarkerAlias : Nat := 0\n", encoding="utf-8"
    )

    index = index_project(tmp_path)

    assert index.find("canonical") is not None
    assert index.find("copiedThroughMarkerAlias") is None


def test_portably_ambiguous_publication_markers_fail_closed(tmp_path: Path) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    generated = tmp_path / "generated"
    generated.mkdir()
    lower = generated / "publication.json"
    upper = generated / "PUBLICATION.JSON"
    lower.write_text('{"schema":"autoform-publication/v2"}\n', encoding="utf-8")
    upper.write_text("{}\n", encoding="utf-8")
    if lower.samefile(upper):
        pytest.skip("filesystem does not permit case-colliding marker names")

    with pytest.raises(OSError, match="ambiguous publication manifests"):
        index_project(tmp_path)


def test_publication_marker_file_and_symlink_alias_fail_closed(tmp_path: Path) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("", "generated"),
        files=(
            ("generated/Copied.lean", b"def hiddenByAmbiguity : Nat := 0\n"),
            (
                "generated/publication.json",
                b'{"schema":"autoform-publication/v2"}\n',
            ),
        ),
        symlinks=(("generated/PUBLICATION.JSON", "elsewhere"),),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )

    with pytest.raises(OSError, match="ambiguous publication manifests"):
        lean_module._indexed_source_snapshot(tmp_path, snapshot, ())


def test_outer_publication_marker_ignores_ambiguous_descendant_markers(
    tmp_path: Path,
) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("", "generated", "generated/nested"),
        files=(
            ("generated/nested/Copied.lean", b"def hiddenByOuterMarker : Nat := 0\n"),
            (
                "generated/nested/publication.json",
                b'{"schema":"autoform-publication/v2"}\n',
            ),
            (
                "generated/publication.json",
                b'{"schema":"autoform-publication/v2"}\n',
            ),
        ),
        symlinks=(("generated/nested/PUBLICATION.JSON", "elsewhere"),),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )

    index = lean_module._indexed_source_snapshot(tmp_path, snapshot, ())

    assert index.index.find("hiddenByOuterMarker") is None


def test_generated_publication_descendants_do_not_change_lean_generation(
    tmp_path: Path,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n", "A.lean")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "publication.json").write_text(
        '{"schema":"autoform-publication/v2"}\n', encoding="utf-8"
    )
    copied = generated / "Copied.lean"
    copied.write_text("def copied : Nat := 0\n", encoding="utf-8")
    before = snapshot_project_sources(tmp_path)

    copied.write_text("def copied : Nat := 1\n", encoding="utf-8")
    after = snapshot_project_sources(tmp_path)

    assert after.revision == before.revision
    assert after.generation_revision == before.generation_revision


def test_empty_non_source_directory_does_not_change_lean_generation(tmp_path: Path) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n", "A.lean")
    before = snapshot_project_sources(tmp_path)

    (tmp_path / "notes").mkdir()
    after = snapshot_project_sources(tmp_path)

    assert after.revision == before.revision
    assert after.generation_revision == before.generation_revision


def test_outer_publication_marker_deterministically_excludes_nested_markers(
    tmp_path: Path,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n", "A.lean")
    outer = tmp_path / "generated"
    nested = outer / "nested"
    nested.mkdir(parents=True)
    for directory in (outer, nested):
        (directory / "publication.json").write_text(
            '{"schema":"autoform-publication/v2"}\n', encoding="utf-8"
        )
    copied = nested / "Copied.lean"
    copied.write_text("def copied : Nat := 0\n", encoding="utf-8")
    before = snapshot_project_sources(tmp_path)

    (nested / "publication.json").write_text("{}\n", encoding="utf-8")
    copied.write_text("def copied : Nat := 1\n", encoding="utf-8")
    after = snapshot_project_sources(tmp_path)

    assert after.revision == before.revision
    assert after.generation_revision == before.generation_revision


def test_anonymous_instances_are_not_mistaken_for_names(tmp_path: Path) -> None:
    index = _index(tmp_path, "instance : Inhabited Nat := ⟨0⟩\n")

    assert index.declarations == {}


def test_declaration_names_splits_a_list() -> None:
    assert declaration_names("A.b, C.d  E.f") == ["A.b", "C.d", "E.f"]
    assert declaration_names("") == []


def test_declaration_intent_aliases_have_one_shared_normalization() -> None:
    assert declaration_kind("lemma") == "theorem"
    assert declaration_kind("Corollary") == "theorem"
    assert declaration_kind("definition") == "def"
    assert declaration_kind("unknown") is None
    assert declaration_keywords("proposition") == frozenset({"lemma", "theorem"})


def test_mathlib_file_maps_only_canonical_source_paths() -> None:
    assert mathlib_module_name("Mathlib.lean") == "Mathlib"
    assert mathlib_module_name("Mathlib/Data/Nat/Prime/Basic.lean") == (
        "Mathlib.Data.Nat.Prime.Basic"
    )
    for invalid in (
        "",
        "Mathlib/Data/Nat/Prime/Basic",
        "Mathlib//Data/Nat.lean",
        "./Mathlib/Data/Nat.lean",
        "Mathlib/../Outside.lean",
        r"Mathlib\Data\Nat.lean",
        "/Mathlib/Data/Nat.lean",
        "Batteries/Data/Nat.lean",
        "Mathlib/not-valid!.lean",
    ):
        assert mathlib_module_name(invalid) is None


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
