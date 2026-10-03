from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from autoform_cli import _directory_binding as directory_binding_module
from autoform_cli import _tree_snapshot as tree_snapshot_module
from autoform_cli import lean as lean_module
from autoform_cli._tree_snapshot import (
    BoundDirectoryTree,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
    bind_directory_tree,
    capture_directory_descriptor,
)
from autoform_cli.lean import (
    SourceLinker,
    build_linker,
    declaration_names,
    index_project,
    open_project_sources,
    snapshot_project_sources,
    strip_lean_comments,
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


@pytest.mark.parametrize("directory", [".GIT", ".LAKE", ".ObSiDiAn"])
def test_case_aliases_of_tooling_directories_are_not_scanned(
    tmp_path: Path,
    directory: str,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    ignored = tmp_path / directory
    ignored.mkdir()
    (ignored / "Leak.lean").write_text("def leaked : Nat := 0\n")

    index = index_project(tmp_path)

    assert index.find("canonical") is not None
    assert index.find("leaked") is None


def test_case_alias_of_publication_staging_prefix_is_not_scanned(
    tmp_path: Path,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    ignored = tmp_path / ".AUTOFORM-PUBLICATION-probe"
    ignored.mkdir()
    (ignored / "Leak.lean").write_text("def leaked : Nat := 0\n")

    index = index_project(tmp_path)

    assert index.find("canonical") is not None
    assert index.find("leaked") is None


@pytest.mark.parametrize("portable", [False, True])
def test_changes_inside_an_excluded_build_directory_do_not_invalidate_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    if portable:
        monkeypatch.setattr(
            directory_binding_module,
            "DIRECTORY_BINDING_SUPPORTED",
            False,
        )
    elif not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED
    ):
        pytest.skip("directory descriptor capture is unavailable")
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
        monkeypatch.setattr(
            directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False
        )
    sources = open_project_sources(tmp_path)

    sources.close()

    with pytest.raises(TreeSnapshotError, match="closed"):
        sources.capture()


def test_missing_project_keeps_the_empty_index_contract(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    index = index_project(missing)

    assert index.root == missing.resolve()
    assert index.declarations == {}


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
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
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


@pytest.mark.parametrize("portable", [False, True])
def test_source_capture_rejects_mid_capture_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    if portable:
        monkeypatch.setattr(
            directory_binding_module,
            "DIRECTORY_BINDING_SUPPORTED",
            False,
        )
    elif not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED
    ):
        pytest.skip("directory descriptor capture is unavailable")
    source = tmp_path / "Project" / "Basic.lean"
    _index(tmp_path, "def before : Nat := 0\n")
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    changed = False

    def change_source(event: str, relative: str) -> None:
        nonlocal changed
        original_checkpoint(event, relative)
        if event == "before-final-verification" and not changed:
            changed = True
            source.write_text("def after : Nat := 1000\n", encoding="utf-8")

    monkeypatch.setattr(
        tree_snapshot_module,
        "_tree_snapshot_checkpoint",
        change_source,
    )

    sources = open_project_sources(tmp_path)
    try:
        with pytest.raises(TreeSnapshotError, match="changed while it was captured"):
            sources.capture()
    finally:
        sources.close()

    assert changed


def test_portable_binding_rejects_a_replacement_root_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    _index(project, "def original : Nat := 0\n")
    replacement = tmp_path / "replacement"
    _index(replacement, "def replacement : Nat := 0\n")
    displaced = tmp_path / "displaced"
    monkeypatch.setattr(
        directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False
    )
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
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
    root = tmp_path / "tree"
    (root / "a" / "x").mkdir(parents=True)
    (root / "a-").mkdir()
    (root / "a" / "x" / "One.lean").write_text("def one : Nat := 1\n")
    (root / "a-" / "Two.lean").write_text("def two : Nat := 2\n")
    with bind_directory_tree(root) as bound:
        descriptor_snapshot = bound.capture()

    monkeypatch.setattr(
        directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False
    )
    with bind_directory_tree(root) as bound:
        portable_snapshot = bound.capture()

    assert descriptor_snapshot == portable_snapshot
    assert descriptor_snapshot.revision == portable_snapshot.revision
    assert (
        descriptor_snapshot.generation_revision
        == portable_snapshot.generation_revision
    )


def test_expected_child_identity_is_checked_during_descriptor_capture(
    tmp_path: Path,
) -> None:
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
    root = tmp_path / "tree"
    child = root / "child"
    child.mkdir(parents=True)
    (child / "Original.lean").write_text("def original : Nat := 0\n")
    expected = (child.stat().st_dev, child.stat().st_ino)
    displaced = tmp_path / "displaced"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "Replacement.lean").write_text("def replacement : Nat := 0\n")
    binding = directory_binding_module.open_directory(root)
    try:
        child.rename(displaced)
        replacement.rename(child)
        with pytest.raises(TreeSnapshotError, match="changed while it was captured"):
            capture_directory_descriptor(
                binding.descriptor,
                expected_children={"child": expected},
            )
    finally:
        binding.close()


def test_expected_child_rejects_a_case_colliding_entry(tmp_path: Path) -> None:
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
    root = tmp_path / "tree"
    child = root / "child"
    child.mkdir(parents=True)
    collision = root / "CHILD"
    try:
        collision.mkdir()
    except FileExistsError:
        pytest.skip("filesystem does not permit case-colliding directory names")
    if collision.samefile(child):
        pytest.skip("filesystem does not permit case-colliding directory names")
    expected = (child.stat().st_dev, child.stat().st_ino)
    binding = directory_binding_module.open_directory(root)
    try:
        with pytest.raises(TreeSnapshotError, match="changed while it was captured"):
            capture_directory_descriptor(
                binding.descriptor,
                expected_children={"child": expected},
            )
    finally:
        binding.close()


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
    monkeypatch.setattr(
        directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False
    )

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

    monkeypatch.setattr(
        directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False
    )
    monkeypatch.setattr(Path, "stat", stat_without_no_follow)

    snapshot = snapshot_project_sources(root)

    assert snapshot.index.find("portable") is not None


def test_portable_capture_rejects_repeatable_nested_directory_redirection(
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

    def checkpoint(event: str, _relative: str) -> None:
        if event == "between-portable-captures":
            restore()

    original_signature = tree_snapshot_module._stat_signature

    def coarse_directory_signature(metadata):
        signature = original_signature(metadata)
        if stat.S_ISDIR(metadata.st_mode):
            return (*signature[:3], 0, 0, 0, 0)
        return signature

    monkeypatch.setattr(directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False)
    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", checkpoint)
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

    monkeypatch.setattr(
        directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False
    )
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
    monkeypatch.setattr(
        directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False
    )

    with pytest.raises(TreeSnapshotError, match="cannot be inspected safely"):
        BoundDirectoryTree(tmp_path / "bad\0name")


@pytest.mark.parametrize("portable", [False, True])
def test_source_binding_normalizes_an_invalid_exclusion_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    if portable:
        monkeypatch.setattr(
            directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False
        )

    with pytest.raises(OSError, match="Lean exclusion path cannot be inspected safely"):
        snapshot_project_sources(tmp_path, exclude_roots=("bad\0name",))


def test_directory_binding_does_not_normalize_away_a_symlink(
    tmp_path: Path,
) -> None:
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
    holder = tmp_path / "holder"
    holder.mkdir()
    (holder / "target").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (holder / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(OSError, match="must not contain a symbolic link"):
        directory_binding_module.open_directory(holder / "link" / ".." / "target")


def test_directory_binding_closes_descriptors_after_invalid_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
    parent = tmp_path / "parent"
    parent.mkdir()
    original_open = os.open
    original_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    leaked: list[int] = []
    try:
        with monkeypatch.context() as context:
            context.setattr(directory_binding_module.os, "open", tracked_open)
            context.setattr(directory_binding_module.os, "close", tracked_close)
            for _attempt in range(20):
                with pytest.raises(OSError, match="must not contain a symbolic link"):
                    directory_binding_module.open_directory(parent / "bad\0name")
        leaked = [descriptor for descriptor in opened if descriptor not in closed]
    finally:
        for descriptor in leaked:
            original_close(descriptor)

    assert opened
    assert leaked == []


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
    monkeypatch.setattr(
        directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False
    )

    index = index_project(alias, exclude_roots=(alias / "excluded",))

    assert index.find("kept") is not None
    assert index.find("leaked") is None


@pytest.mark.parametrize("portable", [False, True])
def test_future_exclusion_is_recanonicalized_after_case_alias_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    project = tmp_path / "ProjectCase"
    _index(project, "def kept : Nat := 0\n", "Project/Keep.lean")
    alias = tmp_path / "projectcase"
    if not alias.exists():
        pytest.skip("filesystem is case-sensitive")
    if portable:
        monkeypatch.setattr(
            directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False
        )
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


def test_exclusion_closes_child_descriptor_when_identity_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and lean_module._DESCRIPTOR_LISTING_SUPPORTED
    ):
        pytest.skip("directory descriptor traversal is unavailable")
    root = tmp_path / "project"
    (root / "excluded").mkdir(parents=True)
    root_metadata = root.stat(follow_symlinks=False)
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    original_open = os.open
    original_fstat = os.fstat
    original_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def tracked_open(path, flags, *, dir_fd=None):
        descriptor = original_open(path, flags, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    def fail_child_fstat(descriptor):
        if len(opened) == 2 and descriptor == opened[1]:
            raise OSError("injected child identity failure")
        return original_fstat(descriptor)

    def tracked_close(descriptor):
        closed.append(descriptor)
        return original_close(descriptor)

    leaked: list[int] = []
    try:
        with monkeypatch.context() as context:
            context.setattr(lean_module.os, "open", tracked_open)
            context.setattr(lean_module.os, "fstat", fail_child_fstat)
            context.setattr(lean_module.os, "close", tracked_close)
            with pytest.raises(OSError, match="injected child identity failure"):
                lean_module._canonical_exclusion_tail(
                    root,
                    ("excluded", "future"),
                    root_identity,
                )
        leaked = [descriptor for descriptor in opened if descriptor not in closed]
    finally:
        for descriptor in leaked:
            original_close(descriptor)

    assert len(opened) == 2
    assert leaked == []


def test_exclusion_attempts_every_close_after_one_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and lean_module._DESCRIPTOR_LISTING_SUPPORTED
    ):
        pytest.skip("directory descriptor traversal is unavailable")
    root = tmp_path / "project"
    (root / "excluded").mkdir(parents=True)
    root_metadata = root.stat(follow_symlinks=False)
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    original_close = os.close
    close_attempts: list[int] = []

    def fail_first_close(descriptor: int) -> None:
        close_attempts.append(descriptor)
        original_close(descriptor)
        if len(close_attempts) == 1:
            raise OSError("injected close failure")

    with monkeypatch.context() as context:
        context.setattr(lean_module.os, "close", fail_first_close)
        result = lean_module._canonical_exclusion_tail(
            root,
            ("excluded", "future"),
            root_identity,
        )

    assert result.as_posix() == "excluded/future"
    assert len(close_attempts) == 2


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

    monkeypatch.setattr(
        directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False
    )
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

    with pytest.raises(OSError, match="publication manifest exceeds"):
        index_project(tmp_path)

    assert observed_lengths == [lean_module._PUBLICATION_MANIFEST_BYTE_LIMIT + 1]


@pytest.mark.parametrize("manifest_name", ["publication.json", "manifest.json"])
@pytest.mark.parametrize(
    "invalid_value",
    ["1" * 5000, "[" * 2000 + "0" + "]" * 2000],
    ids=["integer-limit", "nested-value"],
)
def test_malformed_manifest_value_does_not_escape_source_indexing(
    tmp_path: Path,
    manifest_name: str,
    invalid_value: str,
) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / manifest_name).write_text(
        '{"kind":"packets","schema":' + invalid_value + "}\n",
        encoding="utf-8",
    )
    (generated / "Visible.lean").write_text("def visible : Nat := 0\n", encoding="utf-8")

    index = index_project(tmp_path)

    assert index.find("visible") is not None


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


@pytest.mark.parametrize(
    "relative",
    [
        "../escaped.txt",
        "C:escaped.txt",
        "C:/escaped.txt",
        "NUL",
        "NUL.txt",
        "NUL .txt",
        "trailing.",
        "trailing ",
        "invalid?.txt",
        "control\x01.txt",
    ],
)
def test_snapshot_materialization_rejects_non_relative_paths(
    tmp_path: Path,
    relative: str,
) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("",),
        files=((relative, b"escaped\n"),),
        symlinks=(),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )
    destination = tmp_path / "destination"

    with pytest.raises(TreeSnapshotError, match="unsafe materialization path"):
        snapshot.materialize_regular_files(destination)

    assert not destination.exists()
    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.parametrize(
    ("files", "directories"),
    [
        ((("A.txt", b"first"), ("a.txt", b"second")), ("",)),
        ((("parent", b"file"), ("parent/child", b"child")), ("",)),
        ((("a/child", b"child"),), ("", "A")),
        ((), ("", "a/b")),
    ],
)
def test_snapshot_materialization_rejects_aliases_and_type_conflicts(
    tmp_path: Path,
    files: tuple[tuple[str, bytes], ...],
    directories: tuple[str, ...],
) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=directories,
        files=files,
        symlinks=(),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )
    destination = tmp_path / "destination"

    with pytest.raises(TreeSnapshotError, match="materialization path"):
        snapshot.materialize_regular_files(destination)

    assert not destination.exists()


def test_snapshot_materialization_orders_valid_directory_records(tmp_path: Path) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("", "a/b", "a"),
        files=(("a/b/result.txt", b"result\n"),),
        symlinks=(),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )
    destination = tmp_path / "destination"

    snapshot.materialize_regular_files(destination)

    assert (destination / "a/b/result.txt").read_bytes() == b"result\n"


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


@pytest.mark.parametrize("outer_kind", ["publication", "managed"])
def test_outer_output_marker_ignores_other_manifest_type_below_it(
    tmp_path: Path,
    outer_kind: str,
) -> None:
    if outer_kind == "publication":
        outer_name = "publication.json"
        outer_data = b'{"schema":"autoform-publication/v2"}\n'
        nested_name = "manifest.json"
    else:
        outer_name = "manifest.json"
        outer_data = (
            b'{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
        )
        nested_name = "publication.json"
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("", "generated", "generated/nested"),
        files=(
            ("generated/nested/Copied.lean", b"def hiddenByOuterMarker : Nat := 0\n"),
            (f"generated/nested/{nested_name}", b"{}\n"),
            (f"generated/{outer_name}", outer_data),
        ),
        symlinks=((f"generated/nested/{nested_name.upper()}", "elsewhere"),),
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


@pytest.mark.parametrize(
    ("kind", "schema"),
    [
        ("packets", "autoform-skeleton-packets/v1"),
        ("packets", "autoform-skeleton-packets/v2"),
        ("passages", "autoform-skeleton-passages/v1"),
        ("passages", "autoform-skeleton-passages/v2"),
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
        json.dumps({"kind": kind, "packets": [], "schema": schema}) + "\n",
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
            "packets": ["x" * (lean_module._PUBLICATION_MANIFEST_BYTE_LIMIT + 1)],
            "schema": "autoform-skeleton-packets/v2",
        },
        sort_keys=True,
    )
    (packets / "manifest.json").write_text(payload, encoding="utf-8")

    index = index_project(tmp_path)

    assert index.find("target").path == Path("Actual.lean")


def test_oversized_managed_manifest_fails_closed_at_its_read_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packets = tmp_path / "review-packets"
    packets.mkdir()
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
    monkeypatch.setattr(lean_module, "_MANAGED_OUTPUT_MANIFEST_BYTE_LIMIT", 64)

    with pytest.raises(OSError, match="managed output manifest exceeds"):
        index_project(tmp_path)


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


def test_build_linker_can_use_a_captured_index_without_live_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("git detection must not run")

    monkeypatch.setattr(lean_module, "_git", unexpected_git)

    linker = build_linker(tmp_path, source_index=index, detect_missing=False)

    assert linker.index is index
    assert linker.repository_url is None
    assert linker.ref is None


def test_build_linker_never_pairs_a_captured_index_with_a_live_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)

    def unexpected_ref(_root):
        raise AssertionError("a captured source index requires an explicit ref")

    monkeypatch.setattr(lean_module, "detect_ref", unexpected_ref)

    linker = build_linker(
        tmp_path,
        repository_url="https://github.com/owner/repo",
        source_index=index,
    )

    assert linker.ref is None
    assert linker.url("Outer.alpha") is None


def test_build_linker_rejects_a_captured_index_from_another_root(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    index = _index(first)
    second.mkdir()

    with pytest.raises(ValueError, match="different Lean root"):
        build_linker(second, source_index=index, detect_missing=False)
