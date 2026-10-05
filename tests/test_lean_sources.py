from __future__ import annotations

import errno
import json
import os
import re
import stat
import subprocess
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


def test_targeted_index_prefilters_files_but_preserves_file_context_and_digest(
    tmp_path: Path,
) -> None:
    _index(tmp_path)
    other = tmp_path / "Project/Other.lean"
    other.write_text("def unrelated : Nat := 0\n", encoding="utf-8")
    full = index_project(tmp_path)

    targeted = index_project(tmp_path, names=("Outer.alpha",))

    assert targeted.find("Outer.alpha") is not None
    assert targeted.find("Outer.beta") is not None
    assert targeted.find("unrelated") is None
    assert targeted.source_digest == full.source_digest


def test_targeted_index_treats_inline_comments_as_lean_whitespace(
    tmp_path: Path,
) -> None:
    _index(tmp_path, "theorem /- separator -/ target : True := by trivial\n")

    targeted = index_project(tmp_path, names=("target",))

    assert targeted.find("target") is not None


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


def test_changes_inside_an_excluded_build_directory_do_not_invalidate_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (
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


def test_closed_source_binding_rejects_later_capture(
    tmp_path: Path,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")
    sources = open_project_sources(tmp_path)

    sources.close()

    with pytest.raises(TreeSnapshotError, match="closed"):
        sources.capture()


def test_missing_project_keeps_the_empty_index_contract(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    index = index_project(missing)

    assert index.root == missing.resolve()
    assert index.declarations == {}


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_direct_snapshot_names_a_stable_invalid_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_kind: str,
) -> None:
    root = tmp_path / "root"
    if root_kind == "file":
        root.write_text("not a directory\n")
    attempts = 0
    original_bind = lean_module.bind_project_sources

    def counted_bind(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(lean_module, "bind_project_sources", counted_bind)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)
    reason = (
        "directory root does not exist"
        if root_kind == "missing"
        else "directory root is not a directory"
    )

    with pytest.raises(lean_module.LeanSourceError, match=reason):
        snapshot_project_sources(root)

    assert attempts == lean_module._SNAPSHOT_ATTEMPTS


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


def test_source_capture_rejects_mid_capture_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (
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


def test_snapshot_retries_a_capture_that_races_one_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Project" / "Basic.lean"
    _index(tmp_path, "def before : Nat := 0\n")
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    changed = False

    def change_source_once(event: str, relative: str) -> None:
        nonlocal changed
        original_checkpoint(event, relative)
        if event == "before-final-verification" and not changed:
            changed = True
            source.write_text("def after : Nat := 1000\n", encoding="utf-8")

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", change_source_once)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)

    snapshot = snapshot_project_sources(tmp_path)

    assert changed
    assert snapshot.index.find("after") is not None
    assert snapshot.index.find("before") is None


def test_snapshot_reports_sources_that_keep_changing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Project" / "Basic.lean"
    _index(tmp_path, "def churn : Nat := 0\n")
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    edits = 0

    def change_source(event: str, relative: str) -> None:
        nonlocal edits
        original_checkpoint(event, relative)
        if event == "before-final-verification":
            edits += 1
            # Each edit also changes the size, so coarse timestamps cannot hide it.
            source.write_text(f"def churn : Nat := {'1' * (edits + 1)}\n", encoding="utf-8")

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", change_source)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)

    with pytest.raises(lean_module.LeanSourceError, match="Lean sources kept changing while they were indexed"):
        snapshot_project_sources(tmp_path)

    assert edits == lean_module._SNAPSHOT_ATTEMPTS


def test_snapshot_retries_a_root_replaced_while_it_is_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED
    ):
        pytest.skip("directory descriptor capture is unavailable")
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

    original_bind = lean_module.bind_project_sources
    attempts = 0

    def counted_bind(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(lean_module, "bind_project_sources", counted_bind)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)
    with monkeypatch.context() as context:
        context.setattr(directory_binding_module.os, "stat", stat_once_replaced)
        snapshot = snapshot_project_sources(project)

    assert swapped
    assert attempts == 2
    assert snapshot.index.find("canonical") is not None
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
    if not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED
    ):
        pytest.skip("directory descriptor capture is unavailable")
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
    original_bind = lean_module.bind_project_sources
    attempts = 0

    def counted_bind(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(lean_module, "bind_project_sources", counted_bind)
    reason = (
        f"directory tree could not be read: {os.strerror(errno.EIO)}"
        if failure == "io-error"
        else "directory tree is nested too deeply to capture"
    )

    with pytest.raises(lean_module.LeanSourceError, match=rf"^{re.escape(reason)}$"):
        snapshot_project_sources(tmp_path)

    assert attempts == 1


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
    original_bind = lean_module.bind_project_sources
    attempts = 0

    def counted_bind(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(lean_module, "bind_project_sources", counted_bind)

    with pytest.raises(lean_module.LeanSourceError, match="directory tree contains an unsupported entry name"):
        snapshot_project_sources(tmp_path)

    assert attempts == 1


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

    original_names = tree_snapshot_module._capture_path_names
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

    monkeypatch.setattr(directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False)
    monkeypatch.setattr(tree_snapshot_module, "_capture_path_names", redirect_then_list)
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


def test_source_binding_normalizes_an_invalid_exclusion_path(
    tmp_path: Path,
) -> None:
    _index(tmp_path, "def canonical : Nat := 0\n")

    with pytest.raises(OSError, match="Lean exclusion path cannot be inspected safely"):
        snapshot_project_sources(tmp_path, exclude_roots=("bad\0name",))


def test_directory_binding_resolves_dot_dot_after_a_symlinked_ancestor_physically(
    tmp_path: Path,
) -> None:
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
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
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
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
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
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
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
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
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
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
    if not directory_binding_module.DIRECTORY_BINDING_SUPPORTED:
        pytest.skip("directory descriptors are unavailable")
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
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    swapped = False

    def swap_and_restore(event: str, relative: str) -> None:
        nonlocal swapped
        original_checkpoint(event, relative)
        if event == "after-directory-list" and relative == "" and not swapped:
            root.rename(displaced)
            replacement.rename(root)
            swapped = True
        elif event == "before-final-verification" and relative == "" and swapped:
            root.rename(replacement)
            displaced.rename(root)

    monkeypatch.setattr(
        tree_snapshot_module,
        "_tree_snapshot_checkpoint",
        swap_and_restore,
    )
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
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    edits = 0

    def churn_link(event: str, relative: str) -> None:
        nonlocal edits
        original_checkpoint(event, relative)
        if event == "before-final-verification" and relative == "":
            edits += 1
            link.unlink()
            link.symlink_to(f"target-{edits}")

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", churn_link)

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


def test_snapshot_materialization_root_swap_never_redirects_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("", "nested"),
        files=(("result.txt", b"captured\n"),),
        symlinks=(),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )
    destination = tmp_path / "destination"
    displaced = tmp_path / "displaced"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    swapped = False

    def swap_root(event: str, relative: str) -> None:
        nonlocal swapped
        original_checkpoint(event, relative)
        if event == "after-materialization-directory-open" and relative == "":
            destination.rename(displaced)
            destination.symlink_to(outside, target_is_directory=True)
            swapped = True

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", swap_root)
    try:
        with pytest.raises(TreeSnapshotError, match="materialization"):
            snapshot.materialize_regular_files(destination)
        assert swapped
        assert not (outside / "result.txt").exists()
    finally:
        if destination.is_symlink():
            destination.unlink()
        if displaced.exists():
            for child in displaced.iterdir():
                child.unlink()
            displaced.rmdir()


def test_snapshot_materialization_nested_swap_never_redirects_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("", "nested"),
        files=(("nested/result.txt", b"captured\n"),),
        symlinks=(),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )
    destination = tmp_path / "destination"
    outside = tmp_path / "outside"
    outside.mkdir()
    displaced = destination / "nested-displaced"
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint

    def swap_nested(event: str, relative: str) -> None:
        original_checkpoint(event, relative)
        if event == "after-materialization-directory-open" and relative == "nested":
            (destination / "nested").rename(displaced)
            (destination / "nested").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", swap_nested)
    try:
        with pytest.raises(TreeSnapshotError, match="materialization"):
            snapshot.materialize_regular_files(destination)
        assert not (outside / "result.txt").exists()
    finally:
        linked = destination / "nested"
        if linked.is_symlink():
            linked.unlink()
        if displaced.exists():
            displaced.rmdir()
        if destination.exists():
            destination.rmdir()


def test_snapshot_materialization_retries_short_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("",),
        files=(("result.txt", b"captured bytes\n"),),
        symlinks=(),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )
    destination = tmp_path / "destination"
    original_write = tree_snapshot_module.os.write

    def short_write(descriptor: int, data) -> int:
        return original_write(descriptor, bytes(data[:1]))

    monkeypatch.setattr(tree_snapshot_module.os, "write", short_write)

    snapshot.materialize_regular_files(destination)

    assert (destination / "result.txt").read_bytes() == b"captured bytes\n"


def test_snapshot_materialization_closes_descriptors_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("", "nested"),
        files=(("nested/result.txt", b"captured\n"),),
        symlinks=(),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )
    destination = tmp_path / "destination"
    original_open = tree_snapshot_module.os.open
    original_write = tree_snapshot_module.os.write
    opened: list[int] = []
    writes = 0

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail_second_write(descriptor: int, data) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return original_write(descriptor, bytes(data[:1]))
        raise OSError(errno.EIO, "injected materialization failure")

    monkeypatch.setattr(tree_snapshot_module.os, "open", tracked_open)
    monkeypatch.setattr(tree_snapshot_module.os, "write", fail_second_write)

    with pytest.raises(TreeSnapshotError, match="materialized safely"):
        snapshot.materialize_regular_files(destination)

    assert opened
    assert writes == 2
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not destination.exists()


@pytest.mark.parametrize(
    "attack",
    ["replace", "overwrite", "inject", "chmod", "hardlink"],
)
def test_snapshot_materialization_rejects_final_tree_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("", "nested"),
        files=(("first.txt", b"first\n"), ("second.txt", b"second\n")),
        symlinks=(),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )
    destination = tmp_path / "destination"
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    attacked = False

    def tamper_before_second_file(event: str, relative: str) -> None:
        nonlocal attacked
        original_checkpoint(event, relative)
        if (
            event != "before-materialization-file-open"
            or relative != "second.txt"
            or attacked
        ):
            return
        attacked = True
        first = destination / "first.txt"
        if attack == "replace":
            first.unlink()
            first.write_bytes(b"evil!\n")
        elif attack == "overwrite":
            first.write_bytes(b"evil!\n")
        elif attack == "inject":
            (destination / "injected.lean").write_text("def injected : Nat := 0\n")
        elif attack == "chmod":
            destination.chmod(0o777)
            (destination / "nested").chmod(0o777)
            first.chmod(0o666)
        else:
            os.link(first, tmp_path / "outside-link")

    monkeypatch.setattr(
        tree_snapshot_module,
        "_tree_snapshot_checkpoint",
        tamper_before_second_file,
    )
    try:
        with pytest.raises(TreeSnapshotError):
            snapshot.materialize_regular_files(destination)
        assert attacked
    finally:
        outside_link = tmp_path / "outside-link"
        if outside_link.exists():
            outside_link.unlink()
        if destination.exists():
            for child in destination.iterdir():
                child.rmdir() if child.is_dir() else child.unlink()
            destination.rmdir()


@pytest.mark.parametrize("attack", ["inject", "overwrite", "symlink"])
def test_fast_snapshot_materialization_rejects_path_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("",),
        files=(("result.txt", b"captured\n"),),
        symlinks=(),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )
    destination = tmp_path / "destination"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint

    def tamper(event: str, relative: str) -> None:
        original_checkpoint(event, relative)
        if event != "before-materialization-final-verification":
            return
        if attack == "inject":
            (destination / "injected.lean").write_text("def injected : Nat := 0\n")
        elif attack == "overwrite":
            (destination / "result.txt").write_bytes(b"evil!!!!\n")
        else:
            (destination / "result.txt").unlink()
            (destination / "result.txt").symlink_to(outside)

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", tamper)

    with pytest.raises(TreeSnapshotError):
        snapshot.materialize_regular_files(destination, verify_bytes=False)


def test_fast_snapshot_materialization_skips_byte_recapture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = TreeSnapshot(
        root_identity=(1, 1),
        directories=("", "nested"),
        files=(("nested/result.txt", b"captured\n"),),
        symlinks=(),
        special=(),
        placeholders=(),
        omitted=(),
        identities=(),
    )

    def fail_recapture(*_args, **_kwargs):
        raise AssertionError("fast materialization reread captured bytes")

    monkeypatch.setattr(
        tree_snapshot_module,
        "capture_directory_descriptor",
        fail_recapture,
    )

    destination = tmp_path / "destination"
    snapshot.materialize_regular_files(destination, verify_bytes=False)

    assert (destination / "nested/result.txt").read_bytes() == b"captured\n"


def test_outer_managed_marker_ignores_unrelated_files_below_it(
    tmp_path: Path,
) -> None:
    outer_name = "manifest.json"
    outer_data = (
        b'{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    nested_name = "other.json"
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
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    changed = False

    def change_directory_mode(event: str, relative: str) -> None:
        nonlocal changed
        original_checkpoint(event, relative)
        if event == "before-final-verification" and relative == "" and not changed:
            source_directory.chmod(0o700)
            changed = True

    monkeypatch.setattr(
        tree_snapshot_module,
        "_tree_snapshot_checkpoint",
        change_directory_mode,
    )
    during_change = snapshot_project_sources(tmp_path)
    monkeypatch.setattr(
        tree_snapshot_module,
        "_tree_snapshot_checkpoint",
        original_checkpoint,
    )
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

    def refuse_descendant(parent_descriptor, name, expected, *, max_bytes=None):
        if name == "Packet.lean":
            raise AssertionError("managed descendants must not be opened")
        return original_read(
            parent_descriptor,
            name,
            expected,
            max_bytes=max_bytes,
        )

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
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    edits = 0

    def churn_output(event: str, relative: str) -> None:
        nonlocal edits
        original_checkpoint(event, relative)
        if event == "before-final-verification" and relative == "":
            edits += 1
            packet.write_text(f"def generated : Nat := {edits}\n")

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", churn_output)

    snapshot = snapshot_project_sources(tmp_path)

    assert edits == 1
    assert snapshot.index.find("authored") is not None
    assert snapshot.index.find("generated") is None


def test_managed_marker_verification_never_reenumerates_opaque_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _index(tmp_path, "def authored : Nat := 0\n", "Authored.lean")
    generated = tmp_path / "review-packets"
    generated.mkdir()
    (generated / "Packet.lean").write_text("def generated : Nat := 0\n")
    (generated / "manifest.json").write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    generated_identity = (generated.stat().st_dev, generated.stat().st_ino)
    original_scandir = tree_snapshot_module.os.scandir
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    opaque_scans = 0

    def one_opaque_scan(path):
        nonlocal opaque_scans
        if isinstance(path, int):
            metadata = os.fstat(path)
            if (metadata.st_dev, metadata.st_ino) == generated_identity:
                opaque_scans += 1
                if opaque_scans > 1:
                    raise AssertionError("opaque siblings were re-enumerated")
        return original_scandir(path)

    def add_irrelevant_churn(event: str, relative: str) -> None:
        original_checkpoint(event, relative)
        if event == "before-final-verification" and relative == "":
            for index in range(100):
                (generated / f"junk-{index}").write_text("junk\n")

    monkeypatch.setattr(tree_snapshot_module.os, "scandir", one_opaque_scan)
    monkeypatch.setattr(
        tree_snapshot_module,
        "_tree_snapshot_checkpoint",
        add_irrelevant_churn,
    )

    snapshot = snapshot_project_sources(
        tmp_path,
        limits=TreeCaptureLimits(max_entries=4),
    )

    assert opaque_scans == 1
    assert snapshot.index.find("authored") is not None
    assert snapshot.index.find("generated") is None


def test_managed_output_marker_change_retries_the_whole_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _index(tmp_path, "def authored : Nat := 0\n", "Authored.lean")
    generated = tmp_path / "review-packets"
    generated.mkdir()
    (generated / "Packet.lean").write_text("def generated : Nat := 0\n")
    marker = generated / "manifest.json"
    marker.write_text(
        '{"kind":"packets","packets":[],"schema":"autoform-skeleton-packets/v2"}\n'
    )
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    changed = False
    attempts = 0
    original_bind = lean_module.bind_project_sources

    def change_marker_once(event: str, relative: str) -> None:
        nonlocal changed
        original_checkpoint(event, relative)
        if event == "before-final-verification" and relative == "" and not changed:
            changed = True
            marker.write_text(
                '{"kind": "packets", "packets": [], '
                '"schema": "autoform-skeleton-packets/v2"}\n'
            )

    def counted_bind(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", change_marker_once)
    monkeypatch.setattr(lean_module, "bind_project_sources", counted_bind)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)

    snapshot = snapshot_project_sources(tmp_path)

    assert changed
    assert attempts == 2
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
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint
    edits = 0

    def churn_root_marker(event: str, relative: str) -> None:
        nonlocal edits
        original_checkpoint(event, relative)
        if event == "before-final-verification" and relative == "":
            edits += 1
            marker.write_text(f"{{\"irrelevant\": {edits}}}\n")

    monkeypatch.setattr(
        tree_snapshot_module,
        "_tree_snapshot_checkpoint",
        churn_root_marker,
    )

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

    assert observed_lengths == [65]
    assert index.find("oversizedPacket") is not None


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


def _init_git_repository(root: Path, *, object_format: str = "sha1") -> str:
    initialized = subprocess.run(
        ["git", "init", f"--object-format={object_format}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if initialized.returncode != 0:
        pytest.skip(f"Git does not support {object_format} repositories")
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Autoform Tests",
            "-c",
            "user.email=autoform@example.invalid",
            "commit",
            "-m",
            "fixture",
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


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_auto_linker_only_links_bytes_present_in_the_commit(
    tmp_path: Path,
    object_format: str,
) -> None:
    root = tmp_path / object_format
    root.mkdir()
    _index(root, "def committed : Nat := 0\n", "Committed.lean")
    _index(root, "def dirty : Nat := 0\n", "Dirty.lean")
    commit = _init_git_repository(root, object_format=object_format)
    (root / "Dirty.lean").write_text("def dirty : Nat := 1\n")
    (root / "Untracked.lean").write_text("def untracked : Nat := 0\n")

    linker = build_linker(root)

    assert linker.ref == commit
    assert linker.url("committed") == (
        f"https://github.com/owner/repo/blob/{commit}/Committed.lean#L1"
    )
    assert linker.location("dirty") is not None
    assert linker.location("untracked") is not None
    assert linker.url("dirty") is None
    assert linker.url("untracked") is None


def test_auto_linker_retries_ref_and_source_as_one_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "A.lean"
    source.write_text("def first : Nat := 0\n")
    first_commit = _init_git_repository(root)
    original_snapshot = lean_module.snapshot_project_sources
    captures = 0
    second_commit = ""

    def capture_then_commit(*args, **kwargs):
        nonlocal captures, second_commit
        snapshot = original_snapshot(*args, **kwargs)
        captures += 1
        if captures == 1:
            source.write_text("def second : Nat := 0\n")
            subprocess.run(["git", "add", "A.lean"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Autoform Tests",
                    "-c",
                    "user.email=autoform@example.invalid",
                    "commit",
                    "-m",
                    "second",
                ],
                cwd=root,
                capture_output=True,
                check=True,
            )
            second_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        return snapshot

    monkeypatch.setattr(lean_module, "snapshot_project_sources", capture_then_commit)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)

    linker = build_linker(root)

    assert captures == 2
    assert first_commit != second_commit
    assert linker.ref == second_commit
    assert linker.location("first") is None
    assert linker.url("second") == (
        f"https://github.com/owner/repo/blob/{second_commit}/A.lean#L1"
    )


def test_build_linker_preserves_alias_spelled_exclusions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _index(root, "def keptByLinker : Nat := 0\n", "Keep.lean")
    excluded = root / "Generated"
    excluded.mkdir()
    (excluded / "Leak.lean").write_text("def leakedByLinker : Nat := 0\n")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    linker = build_linker(
        alias,
        exclude_roots=(alias / "generated",),
        repository_url="https://github.com/owner/repo",
        ref="attested",
    )

    assert linker.location("keptByLinker") is not None
    assert linker.location("leakedByLinker") is None


def test_auto_linker_supports_a_symlinked_repository_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _index(root, "def linkedThroughAlias : Nat := 0\n", "A.lean")
    commit = _init_git_repository(root)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    linker = build_linker(alias)

    assert linker.ref == commit
    assert linker.url("linkedThroughAlias") == (
        f"https://github.com/owner/repo/blob/{commit}/A.lean#L1"
    )


def test_auto_linker_ignores_git_replace_objects(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "A.lean"
    source.write_text("def originalTree : Nat := 0\n")
    original = _init_git_repository(root)
    source.write_text("def replacementTree : Nat := 0\n")
    subprocess.run(["git", "add", "A.lean"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Autoform Tests",
            "-c",
            "user.email=autoform@example.invalid",
            "commit",
            "-m",
            "replacement",
        ],
        cwd=root,
        capture_output=True,
        check=True,
    )
    replacement = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "--detach", original], cwd=root, check=True)
    source.write_text("def replacementTree : Nat := 0\n")
    subprocess.run(["git", "replace", original, replacement], cwd=root, check=True)

    linker = build_linker(root)

    assert linker.ref == original
    assert linker.location("replacementTree") is not None
    assert linker.url("replacementTree") is None


def test_explicit_ref_linker_keeps_remote_bound_to_resolved_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    first.mkdir()
    _index(first, "def fromFirstRepository : Nat := 0\n", "A.lean")
    commit = _init_git_repository(first)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/owner/A.git"],
        cwd=first,
        check=True,
    )
    second = tmp_path / "second"
    second.mkdir()
    _index(second, "def fromSecondRepository : Nat := 0\n", "B.lean")
    _init_git_repository(second)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/owner/B.git"],
        cwd=second,
        check=True,
    )
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(first, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    original_index = lean_module.index_project
    swapped = False

    def index_then_swap(*args, **kwargs):
        nonlocal swapped
        index = original_index(*args, **kwargs)
        alias.unlink()
        alias.symlink_to(second, target_is_directory=True)
        swapped = True
        return index

    monkeypatch.setattr(lean_module, "index_project", index_then_swap)

    linker = build_linker(alias, ref=commit)

    assert swapped
    assert linker.repository_url == "https://github.com/owner/A"
    assert linker.location("fromFirstRepository") is not None
    assert linker.url("fromFirstRepository") == (
        f"https://github.com/owner/A/blob/{commit}/A.lean#L1"
    )


def test_auto_linker_ignores_inherited_repo_and_ci_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    first.mkdir()
    (first / "A.lean").write_text("def stableRepository : Nat := 0\n")
    first_commit = _init_git_repository(first)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/owner/A.git"],
        cwd=first,
        check=True,
    )
    second = tmp_path / "second"
    second.mkdir()
    (second / "A.lean").write_text("def stableRepository : Nat := 0\n")
    (second / "OnlyB.txt").write_text("different tree\n")
    second_commit = _init_git_repository(second)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://github.com/owner/B.git"],
        cwd=second,
        check=True,
    )
    monkeypatch.setenv("GIT_DIR", str(second / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(second))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/owner/evil.git")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(second))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/ci-wrong")
    monkeypatch.setenv("GITHUB_SHA", second_commit)

    linker = build_linker(first)

    assert first_commit != second_commit
    assert linker.ref == first_commit
    assert linker.repository_url == "https://github.com/owner/A"
    assert linker.url("stableRepository") == (
        f"https://github.com/owner/A/blob/{first_commit}/A.lean#L1"
    )
