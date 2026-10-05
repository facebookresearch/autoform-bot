from __future__ import annotations

import os
import stat
import sys
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from autoform_cli import _directory_binding as directory_binding_module
from autoform_cli import _tree_snapshot as tree_snapshot_module
from autoform_cli._tree_snapshot import (
    BoundDirectoryTree,
    OpaqueDirectoryMarker,
    TreeCaptureLimitError,
    TreeCaptureLimits,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
    bind_directory_tree,
)


def _capture(
    root: Path,
    selection: TreeSelection,
    *,
    portable: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> TreeSnapshot:
    if not portable and not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED
    ):
        pytest.skip("directory descriptor capture is unavailable")
    if portable:
        monkeypatch.setattr(
            directory_binding_module,
            "DIRECTORY_BINDING_SUPPORTED",
            False,
        )
    with bind_directory_tree(root, selection=selection) as bound:
        return bound.capture()


def _all_entries(limits: TreeCaptureLimits) -> TreeSelection:
    return TreeSelection(
        include=lambda _path, _mode: True,
        descend=lambda _path: True,
        limits=limits,
    )


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize("marker_kind", ["file", "directory"])
def test_presence_marker_makes_a_nested_directory_opaque(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
    marker_kind: str,
) -> None:
    root = tmp_path / "tree"
    worker = root / "worker"
    payload = worker / "payload"
    payload.mkdir(parents=True)
    (root / "kept").write_bytes(b"kept\n")
    (payload / "hidden").write_text("before\n", encoding="utf-8")
    marker = worker / ".git"
    if marker_kind == "file":
        marker.write_text("gitdir: before\n", encoding="utf-8")
    else:
        marker.mkdir()

    selection = TreeSelection(
        include=lambda _path, _mode: True,
        descend=lambda _path: True,
        record_omitted=False,
        opaque_markers=(
            OpaqueDirectoryMarker(
                ".git",
                0,
                lambda _data: False,
                presence_only=True,
            ),
        ),
    )
    before = _capture(root, selection, portable=portable, monkeypatch=monkeypatch)

    assert before.opaque_directories == ("worker",)
    assert before.files == (("kept", b"kept\n"),)
    assert all(not relative.startswith("worker/") for relative, _identity in before.identities)

    (payload / "hidden").write_text("after\n", encoding="utf-8")
    if marker_kind == "file":
        marker.write_text("gitdir: after\n", encoding="utf-8")
    else:
        (marker / "config").write_text("[core]\n", encoding="utf-8")
    after = _capture(root, selection, portable=portable, monkeypatch=monkeypatch)

    assert after.revision == before.revision
    assert after.generation_revision == before.generation_revision


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_entries", -1),
        ("max_depth", True),
        ("max_file_bytes", 1.5),
        ("max_total_bytes", "1"),
    ],
)
def test_capture_limits_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        TreeCaptureLimits(**{field: value})  # type: ignore[arg-type]


def test_capture_limits_are_frozen() -> None:
    limits = TreeCaptureLimits(max_entries=1)

    with pytest.raises(FrozenInstanceError):
        limits.max_entries = 2  # type: ignore[misc]


def test_capture_limit_error_exposes_a_stable_discriminator() -> None:
    error = TreeCaptureLimitError("max_entries", 3)

    assert error.limit == "max_entries"
    assert error.maximum == 3
    assert str(error) == "directory tree exceeds max_entries=3"


@pytest.mark.parametrize("portable", [False, True])
def test_capture_and_close_are_serialized(
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
    root = tmp_path / "tree"
    root.mkdir()
    (root / "payload").write_bytes(b"payload")
    entered = threading.Event()
    release = threading.Event()
    close_started = threading.Event()
    closed = threading.Event()
    captured: list[TreeSnapshot] = []
    failures: list[BaseException] = []
    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint

    def checkpoint(event: str, relative: str) -> None:
        original_checkpoint(event, relative)
        if event == "after-directory-list" and relative == "":
            entered.set()
            if not release.wait(5):
                raise AssertionError("capture test did not release its checkpoint")

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", checkpoint)
    bound = BoundDirectoryTree(root)

    def capture() -> None:
        try:
            captured.append(bound.capture())
        except BaseException as error:
            failures.append(error)

    def close() -> None:
        close_started.set()
        bound.close()
        closed.set()

    capture_thread = threading.Thread(target=capture)
    close_thread = threading.Thread(target=close)
    capture_thread.start()
    assert entered.wait(5)
    close_thread.start()
    try:
        assert close_started.wait(5)
        assert not closed.wait(0.1)
    finally:
        release.set()
        capture_thread.join(5)
        close_thread.join(5)

    assert failures == []
    assert len(captured) == 1
    assert not capture_thread.is_alive()
    assert not close_thread.is_alive()
    assert closed.is_set()


def test_portable_file_capture_accepts_windows_path_and_handle_stat_views(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    payload = root / "payload.cmd"
    payload.write_bytes(b"payload")
    original_fstat = tree_snapshot_module.os.fstat
    path_signature = tree_snapshot_module._stat_signature(os.lstat(payload))
    handle_signatures: list[tuple[int, ...]] = []
    inode_delta = 0

    class WindowsHandleStat:
        def __init__(self, metadata: os.stat_result) -> None:
            self.st_dev = metadata.st_dev
            self.st_ino = metadata.st_ino + inode_delta
            self.st_mode = metadata.st_mode ^ stat.S_IXUSR
            self.st_nlink = metadata.st_nlink
            self.st_size = metadata.st_size
            self.st_mtime_ns = metadata.st_mtime_ns
            self.st_ctime_ns = metadata.st_ctime_ns + 1

    def windows_handle_fstat(descriptor: int) -> WindowsHandleStat:
        metadata = WindowsHandleStat(original_fstat(descriptor))
        handle_signatures.append(tree_snapshot_module._stat_signature(metadata))
        return metadata

    monkeypatch.setattr(tree_snapshot_module.os, "fstat", windows_handle_fstat)
    monkeypatch.setattr(tree_snapshot_module, "_WINDOWS_STAT_VIEWS", True)

    snapshot = _capture(
        root,
        _all_entries(TreeCaptureLimits()),
        portable=True,
        monkeypatch=monkeypatch,
    )

    assert snapshot.files == (("payload.cmd", b"payload"),)
    assert handle_signatures
    assert handle_signatures[0][2] != path_signature[2]
    assert handle_signatures[0][6] != path_signature[6]
    assert (
        tree_snapshot_module._cross_interface_signature(handle_signatures[0])
        == tree_snapshot_module._cross_interface_signature(path_signature)
    )

    inode_delta = 1
    with pytest.raises(TreeSnapshotError, match="changed while it was captured"):
        _capture(
            root,
            _all_entries(TreeCaptureLimits()),
            portable=True,
            monkeypatch=monkeypatch,
        )


@pytest.mark.parametrize("portable", [False, True])
def test_invalid_selection_byte_limit_cannot_bypass_total_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "payload").write_bytes(b"payload")
    selection = TreeSelection(
        include=lambda _path, _mode: True,
        descend=lambda _path: True,
        byte_limit=lambda _path: -2,
        limits=TreeCaptureLimits(max_total_bytes=1),
    )

    with pytest.raises(TreeSnapshotError, match="selection byte limit"):
        _capture(
            root,
            selection,
            portable=portable,
            monkeypatch=monkeypatch,
        )


@pytest.mark.parametrize("portable", [False, True])
def test_entry_limit_counts_ignored_and_placeholder_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "ignored.bin").write_bytes(b"ignored")
    (root / "placeholder.bin").write_bytes(b"placeholder")

    def selection(max_entries: int) -> TreeSelection:
        return TreeSelection(
            include=lambda _path, _mode: False,
            descend=lambda _path: True,
            placeholder=lambda path, _mode: path.name == "placeholder.bin",
            limits=TreeCaptureLimits(
                max_entries=max_entries,
                max_file_bytes=0,
                max_total_bytes=0,
            ),
        )

    snapshot = _capture(
        root,
        selection(2),
        portable=portable,
        monkeypatch=monkeypatch,
    )

    assert snapshot.files == ()
    assert snapshot.placeholders == ("placeholder.bin",)
    assert snapshot.omitted == (("ignored.bin", "file"),)

    with pytest.raises(TreeCaptureLimitError, match="max_entries=1") as captured:
        _capture(
            root,
            selection(1),
            portable=portable,
            monkeypatch=monkeypatch,
        )
    assert captured.value.limit == "max_entries"
    assert captured.value.maximum == 1


@pytest.mark.parametrize("portable", [False, True])
def test_depth_limit_accepts_the_boundary_and_rejects_the_next_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    leaf = root / "one" / "two" / "leaf"
    leaf.parent.mkdir(parents=True)
    leaf.write_bytes(b"")

    snapshot = _capture(
        root,
        _all_entries(TreeCaptureLimits(max_depth=3)),
        portable=portable,
        monkeypatch=monkeypatch,
    )

    assert snapshot.files == (("one/two/leaf", b""),)

    with pytest.raises(TreeCaptureLimitError, match="max_depth=2") as captured:
        _capture(
            root,
            _all_entries(TreeCaptureLimits(max_depth=2)),
            portable=portable,
            monkeypatch=monkeypatch,
        )
    assert captured.value.limit == "max_depth"
    assert captured.value.maximum == 2


@pytest.mark.parametrize("portable", [False, True])
def test_file_byte_limit_accepts_the_boundary_and_rejects_one_more_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    payload = b"x" * (64 * 1024)
    (root / "payload").write_bytes(payload)

    snapshot = _capture(
        root,
        _all_entries(TreeCaptureLimits(max_file_bytes=len(payload))),
        portable=portable,
        monkeypatch=monkeypatch,
    )

    assert snapshot.files == (("payload", payload),)

    with pytest.raises(
        TreeCaptureLimitError,
        match=f"max_file_bytes={len(payload) - 1}",
    ) as captured:
        _capture(
            root,
            _all_entries(TreeCaptureLimits(max_file_bytes=len(payload) - 1)),
            portable=portable,
            monkeypatch=monkeypatch,
        )
    assert captured.value.limit == "max_file_bytes"
    assert captured.value.maximum == len(payload) - 1


@pytest.mark.parametrize("portable", [False, True])
def test_maximum_python_file_limit_does_not_overflow_the_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "payload").write_bytes(b"payload")

    snapshot = _capture(
        root,
        _all_entries(TreeCaptureLimits(max_file_bytes=sys.maxsize)),
        portable=portable,
        monkeypatch=monkeypatch,
    )

    assert snapshot.files == (("payload", b"payload"),)


@pytest.mark.parametrize("portable", [False, True])
def test_total_byte_limit_accepts_the_boundary_and_rejects_one_more_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a").write_bytes(b"12")
    (root / "b").write_bytes(b"345")

    snapshot = _capture(
        root,
        _all_entries(TreeCaptureLimits(max_total_bytes=5)),
        portable=portable,
        monkeypatch=monkeypatch,
    )

    assert snapshot.files == (("a", b"12"), ("b", b"345"))
    reader_name = "_read_portable_file" if portable else "_read_file"
    original_reader = getattr(tree_snapshot_module, reader_name)
    read_paths: list[str] = []

    def record_read(*args, **kwargs):
        path = args[0] if portable else args[1]
        read_paths.append(Path(path).name)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(tree_snapshot_module, reader_name, record_read)

    with pytest.raises(TreeCaptureLimitError, match="max_total_bytes=4") as captured:
        _capture(
            root,
            _all_entries(TreeCaptureLimits(max_total_bytes=4)),
            portable=portable,
            monkeypatch=monkeypatch,
        )

    assert read_paths == ["a"]
    assert captured.value.limit == "max_total_bytes"
    assert captured.value.maximum == 4


@pytest.mark.parametrize("portable", [False, True])
def test_selection_and_capture_byte_limits_compose_on_retained_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "payload").write_bytes(b"0123456789")

    def selection(max_file_bytes: int) -> TreeSelection:
        return TreeSelection(
            include=lambda _path, _mode: True,
            descend=lambda _path: True,
            byte_limit=lambda _path: 2,
            limits=TreeCaptureLimits(max_file_bytes=max_file_bytes),
        )

    snapshot = _capture(
        root,
        selection(3),
        portable=portable,
        monkeypatch=monkeypatch,
    )

    assert snapshot.files == (("payload", b"012"),)

    with pytest.raises(TreeCaptureLimitError) as captured:
        _capture(
            root,
            selection(2),
            portable=portable,
            monkeypatch=monkeypatch,
        )
    assert captured.value.limit == "max_file_bytes"
    assert captured.value.maximum == 2


def test_file_read_cap_does_not_trust_an_underreported_size() -> None:
    budget = tree_snapshot_module._CaptureBudget(TreeCaptureLimits())

    assert budget.file_read_limit(0, None) is None
    assert budget.file_read_limit(0, 10) == 10


@pytest.mark.parametrize("portable", [False, True])
def test_oversized_file_is_rejected_before_its_reader_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "payload").write_bytes(b"1234")
    reader_name = "_read_portable_file" if portable else "_read_file"

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("an oversized file must not be opened for reading")

    monkeypatch.setattr(tree_snapshot_module, reader_name, unexpected_read)

    with pytest.raises(TreeSnapshotError, match="max_file_bytes=3"):
        _capture(
            root,
            _all_entries(TreeCaptureLimits(max_file_bytes=3)),
            portable=portable,
            monkeypatch=monkeypatch,
        )


@pytest.mark.parametrize(
    "limits",
    [
        TreeCaptureLimits(max_entries=1),
        TreeCaptureLimits(max_depth=1),
    ],
)
def test_descriptor_fallback_fails_before_unbounded_listdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limits: TreeCaptureLimits,
) -> None:
    if not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED
    ):
        pytest.skip("directory descriptor capture is unavailable")
    root = tmp_path / "tree"
    root.mkdir()
    (root / "payload").write_bytes(b"payload")
    monkeypatch.setattr(tree_snapshot_module, "_DESCRIPTOR_SCANDIR_SUPPORTED", False)

    def unexpected_listdir(*_args, **_kwargs):
        raise AssertionError("finite structural limits must not call os.listdir")

    monkeypatch.setattr(tree_snapshot_module.os, "listdir", unexpected_listdir)

    with pytest.raises(TreeSnapshotError, match="bounded directory enumeration"):
        _capture(
            root,
            _all_entries(limits),
            portable=False,
            monkeypatch=monkeypatch,
        )


def test_expected_child_precheck_fails_before_unbounded_listdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED
    ):
        pytest.skip("directory descriptor capture is unavailable")
    root = tmp_path / "tree"
    child = root / "child"
    child.mkdir(parents=True)
    identity = (child.stat().st_dev, child.stat().st_ino)
    monkeypatch.setattr(tree_snapshot_module, "_DESCRIPTOR_SCANDIR_SUPPORTED", False)

    def unexpected_listdir(*_args, **_kwargs):
        raise AssertionError("bounded expected-child checks must not call os.listdir")

    monkeypatch.setattr(tree_snapshot_module.os, "listdir", unexpected_listdir)

    with pytest.raises(TreeSnapshotError, match="bounded directory enumeration"):
        BoundDirectoryTree(
            root,
            expected_children={"child": identity},
            selection=_all_entries(TreeCaptureLimits(max_entries=1)),
        )


def test_expected_child_postcheck_uses_capture_override_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED
    ):
        pytest.skip("directory descriptor capture is unavailable")
    root = tmp_path / "tree"
    child = root / "child"
    child.mkdir(parents=True)
    identity = (child.stat().st_dev, child.stat().st_ino)
    bound = BoundDirectoryTree(root, expected_children={"child": identity})
    monkeypatch.setattr(tree_snapshot_module, "_DESCRIPTOR_SCANDIR_SUPPORTED", False)

    def unexpected_listdir(*_args, **_kwargs):
        raise AssertionError("bounded expected-child checks must not call os.listdir")

    monkeypatch.setattr(tree_snapshot_module.os, "listdir", unexpected_listdir)
    try:
        with pytest.raises(TreeSnapshotError, match="bounded directory enumeration"):
            bound.capture(
                selection=_all_entries(TreeCaptureLimits(max_entries=1)),
            )
    finally:
        bound.close()


def test_expected_child_postcheck_fails_before_unbounded_listdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED
    ):
        pytest.skip("directory descriptor capture is unavailable")
    root = tmp_path / "tree"
    child = root / "child"
    child.mkdir(parents=True)
    identity = (child.stat().st_dev, child.stat().st_ino)
    selection = _all_entries(TreeCaptureLimits(max_entries=1))
    bound = BoundDirectoryTree(
        root,
        expected_children={"child": identity},
        selection=selection,
    )
    original_capture = tree_snapshot_module.capture_directory_descriptor

    def unexpected_listdir(*_args, **_kwargs):
        raise AssertionError("bounded expected-child checks must not call os.listdir")

    def capture_then_disable_streaming(*args, **kwargs):
        snapshot = original_capture(*args, **kwargs)
        monkeypatch.setattr(
            tree_snapshot_module,
            "_DESCRIPTOR_SCANDIR_SUPPORTED",
            False,
        )
        monkeypatch.setattr(tree_snapshot_module.os, "listdir", unexpected_listdir)
        return snapshot

    monkeypatch.setattr(
        tree_snapshot_module,
        "capture_directory_descriptor",
        capture_then_disable_streaming,
    )
    try:
        with pytest.raises(TreeSnapshotError, match="bounded directory enumeration"):
            bound.capture()
    finally:
        bound.close()


def test_descriptor_and_portable_limits_capture_the_same_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (
        directory_binding_module.DIRECTORY_BINDING_SUPPORTED
        and tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED
    ):
        pytest.skip("directory descriptor capture is unavailable")
    root = tmp_path / "tree"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "included").write_bytes(b"data")
    (root / "ignored").write_bytes(b"ignored")
    (root / "placeholder").write_bytes(b"placeholder")
    limits = TreeCaptureLimits(
        max_entries=4,
        max_depth=2,
        max_file_bytes=4,
        max_total_bytes=4,
    )
    selection = TreeSelection(
        include=lambda path, _mode: path.name == "included",
        descend=lambda _path: True,
        placeholder=lambda path, _mode: path.name == "placeholder",
        limits=limits,
    )

    descriptor_snapshot = _capture(
        root,
        selection,
        portable=False,
        monkeypatch=monkeypatch,
    )
    with monkeypatch.context() as portable_context:
        portable_snapshot = _capture(
            root,
            selection,
            portable=True,
            monkeypatch=portable_context,
        )

    assert descriptor_snapshot == portable_snapshot


def test_portable_capture_sanitizes_unsupported_reparse_points(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    payload = root / "opaque-entry"
    payload.write_bytes(b"payload")
    identity = (payload.stat().st_dev, payload.stat().st_ino)
    original_reparse = tree_snapshot_module._is_reparse_point

    def is_reparse(metadata) -> bool:
        return (metadata.st_dev, metadata.st_ino) == identity or original_reparse(
            metadata
        )

    def unsupported_readlink(_path) -> str:
        raise ValueError("private unsupported reparse tag")

    monkeypatch.setattr(
        directory_binding_module,
        "DIRECTORY_BINDING_SUPPORTED",
        False,
    )
    monkeypatch.setattr(tree_snapshot_module, "_is_reparse_point", is_reparse)
    monkeypatch.setattr(tree_snapshot_module.os, "readlink", unsupported_readlink)

    with pytest.raises(
        TreeSnapshotError,
        match="directory tree contains an unsupported reparse point",
    ) as caught:
        with bind_directory_tree(root) as bound:
            bound.capture()

    assert "private" not in str(caught.value)
