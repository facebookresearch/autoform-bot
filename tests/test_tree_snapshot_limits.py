from __future__ import annotations

import gc
import hashlib
import io
import os
import stat
import sys
import weakref
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from autoform_cli import _tree_snapshot as tree_snapshot_module
from autoform_cli._tree_snapshot import (
    OpaqueDirectoryMarker,
    TreeCaptureLimitError,
    TreeCaptureLimits,
    TreeChangedError,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
    bind_directory_tree,
)
from tests.test_lean_sources import _require_descriptor_capture, _use_portable_capture


def _capture(
    root: Path,
    selection: TreeSelection,
    *,
    portable: bool,
    monkeypatch: pytest.MonkeyPatch,
    digests: bool = False,
) -> TreeSnapshot:
    if portable:
        _use_portable_capture(monkeypatch)
    else:
        _require_descriptor_capture()
    with bind_directory_tree(root, selection=selection) as bound:
        return bound.capture(digests=digests)


def _all_entries(limits: TreeCaptureLimits) -> TreeSelection:
    return TreeSelection(
        include=lambda _path, _mode: True,
        descend=lambda _path: True,
        limits=limits,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_entries", -1),
        ("max_entries", True),
        ("max_total_bytes", 1.5),
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
def test_entry_limit_counts_ignored_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "ignored.bin").write_bytes(b"ignored")
    (root / "other.bin").write_bytes(b"other")

    def selection(max_entries: int) -> TreeSelection:
        return TreeSelection(
            include=lambda _path, _mode: False,
            descend=lambda _path: True,
            limits=TreeCaptureLimits(
                max_entries=max_entries,
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
    assert snapshot.omitted == (("ignored.bin", "file"), ("other.bin", "file"))

    with pytest.raises(TreeCaptureLimitError, match=r"^directory tree exceeds max_entries=1$"):
        _capture(
            root,
            selection(1),
            portable=portable,
            monkeypatch=monkeypatch,
        )


@pytest.mark.parametrize("portable", [False, True])
def test_maximum_python_byte_limit_does_not_overflow_the_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "payload").write_bytes(b"payload")

    snapshot = _capture(
        root,
        _all_entries(TreeCaptureLimits(max_total_bytes=sys.maxsize)),
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

    with pytest.raises(TreeCaptureLimitError, match=r"^directory tree exceeds max_total_bytes=4$"):
        _capture(
            root,
            _all_entries(TreeCaptureLimits(max_total_bytes=4)),
            portable=portable,
            monkeypatch=monkeypatch,
        )

    assert read_paths == ["a"]


def test_file_read_cap_does_not_trust_an_underreported_size() -> None:
    budget = tree_snapshot_module._CaptureBudget(TreeCaptureLimits())

    assert budget.file_read_limit(0, None) is None
    assert budget.file_read_limit(0, 10) == 10


def test_descriptor_and_portable_limits_capture_the_same_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_descriptor_capture()
    root = tmp_path / "tree"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "included").write_bytes(b"data")
    (root / "ignored").write_bytes(b"ignored")
    limits = TreeCaptureLimits(
        max_entries=3,
        max_total_bytes=4,
    )
    selection = TreeSelection(
        include=lambda path, _mode: path.name == "included",
        descend=lambda _path: True,
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

    _use_portable_capture(monkeypatch)
    monkeypatch.setattr(tree_snapshot_module, "_is_reparse_point", is_reparse)
    monkeypatch.setattr(tree_snapshot_module.os, "readlink", unsupported_readlink)

    with pytest.raises(
        TreeSnapshotError,
        match="directory tree contains an unsupported reparse point",
    ) as caught:
        with bind_directory_tree(root) as bound:
            bound.capture()

    assert "private" not in str(caught.value)


@pytest.mark.parametrize("portable", [False, True])
def test_capture_releases_its_state_without_cyclic_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "payload").write_bytes(b"payload")
    selection = _all_entries(TreeCaptureLimits())
    # Capture state left in a reference cycle would keep the selection, and so
    # its include callable, alive until the cyclic collector runs.
    released = weakref.ref(selection.include)

    enabled = gc.isenabled()
    gc.disable()
    try:
        snapshot = _capture(root, selection, portable=portable, monkeypatch=monkeypatch)
        del selection
        assert released() is None
    finally:
        if enabled:
            gc.enable()

    assert snapshot.files == (("payload", b"payload"),)


def _selected_sources(
    *,
    markers: tuple[OpaqueDirectoryMarker, ...] = (),
    limits: TreeCaptureLimits = TreeCaptureLimits(),
) -> TreeSelection:
    return TreeSelection(
        include=lambda path, _mode: path.suffix == ".src",
        descend=lambda _path: True,
        record_omitted=False,
        limits=limits,
        opaque_markers=markers,
        unselected_may_change=True,
    )


def test_selected_capture_releases_its_state_without_cyclic_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "kept.src").write_bytes(b"kept")
    (root / "a" / "other.txt").write_bytes(b"other")
    selection = _selected_sources()
    # Selected-mode verification relists each folder through a callback; state
    # left in a reference cycle would keep the selection alive until the cyclic
    # collector runs.
    released = weakref.ref(selection.include)

    enabled = gc.isenabled()
    gc.disable()
    try:
        snapshot = _capture(root, selection, portable=False, monkeypatch=monkeypatch)
        del selection
        assert released() is None
    finally:
        if enabled:
            gc.enable()

    assert snapshot.files == (("a/b/kept.src", b"kept"),)


def _at_final_verification(monkeypatch: pytest.MonkeyPatch, action) -> None:
    original = tree_snapshot_module._tree_snapshot_checkpoint

    def checkpoint(name: str, path: str) -> None:
        original(name, path)
        if name == "before-final-verification":
            action()

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", checkpoint)


def test_selected_capture_requires_no_omitted_records() -> None:
    with pytest.raises(ValueError, match="record_omitted=False"):
        TreeSelection(
            include=lambda _path, _mode: True,
            descend=lambda _path: True,
            unselected_may_change=True,
        )


def test_opaque_markers_require_selected_capture() -> None:
    with pytest.raises(ValueError, match="unselected_may_change=True"):
        TreeSelection(
            include=lambda _path, _mode: True,
            descend=lambda _path: True,
            opaque_markers=(OpaqueDirectoryMarker(".git", 0, lambda _data: False, presence_only=True),),
        )


def test_selected_capture_refuses_portable_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "a.src").write_bytes(b"a\n")

    with pytest.raises(TreeSnapshotError, match="directory-descriptor traversal"):
        _capture(tmp_path, _selected_sources(), portable=True, monkeypatch=monkeypatch)


@pytest.mark.parametrize(("folder", "expected"), [("", "."), ("sub", "sub")])
def test_strict_selection_still_rejects_unselected_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    folder: str,
    expected: str,
) -> None:
    _require_descriptor_capture()
    parent = tmp_path / folder if folder else tmp_path
    parent.mkdir(exist_ok=True)
    (parent / "notes.md").write_bytes(b"0\n")

    def save() -> None:
        (parent / "notes.md.tmp").write_bytes(b"1\n")

    _at_final_verification(monkeypatch, save)

    with pytest.raises(TreeChangedError) as error:
        _capture(tmp_path, _all_entries(TreeCaptureLimits()), portable=False, monkeypatch=monkeypatch)

    assert error.value.relative == expected


def test_selected_capture_ignores_unselected_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_descriptor_capture()
    (tmp_path / "kept.src").write_bytes(b"kept\n")
    (tmp_path / "out" / "deep").mkdir(parents=True)
    (tmp_path / "out" / "deep" / "page.html").write_bytes(b"0\n")

    def rebuild() -> None:
        (tmp_path / "out" / "deep" / "page.html").unlink()
        (tmp_path / "out" / "deep").rmdir()
        (tmp_path / "out" / "fresh").mkdir()
        (tmp_path / "notes.md.tmp").write_bytes(b"1\n")

    _at_final_verification(monkeypatch, rebuild)

    snapshot = _capture(tmp_path, _selected_sources(), portable=False, monkeypatch=monkeypatch)

    assert snapshot.directories == ("",)
    assert snapshot.files == (("kept.src", b"kept\n"),)
    assert snapshot.omitted == ()


def test_selected_capture_relist_uses_the_selection_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_descriptor_capture()
    (tmp_path / "kept.src").write_bytes(b"kept\n")
    (tmp_path / "notes.md").write_bytes(b"0\n")
    selection = _selected_sources(limits=TreeCaptureLimits(max_entries=2))

    snapshot = _capture(tmp_path, selection, portable=False, monkeypatch=monkeypatch)
    assert snapshot.files == (("kept.src", b"kept\n"),)

    # The scan lists two entries.  A temporary file that exists only while
    # the folder is relisted pushes that relist over max_entries; the final
    # verification sees two entries again.
    original = tree_snapshot_module._tree_snapshot_checkpoint
    temporary = tmp_path / "notes.md.tmp"

    def checkpoint(name: str, path: str) -> None:
        original(name, path)
        if name == "after-directory-list" and path == "":
            temporary.write_bytes(b"1\n")
        elif name == "before-final-verification":
            temporary.unlink(missing_ok=True)

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", checkpoint)

    with pytest.raises(TreeChangedError) as error:
        _capture(tmp_path, selection, portable=False, monkeypatch=monkeypatch)

    assert error.value.relative == "."


@pytest.mark.parametrize(
    ("holds_selected_entry", "recognized", "named"),
    [
        # An unrecognized marker name hides nothing: a selected entry that
        # appears beside it is a change.
        (False, False, "X/late.src"),
        # A folder that holds nothing selected may become hidden by a marker
        # the walk recognizes from its bytes, as the scan does; it then
        # contributes nothing, whatever it holds.
        (False, True, None),
        # A marker that now hides recorded selected entries is a change.
        (True, True, "X"),
    ],
)
def test_verification_recognizes_markers_like_the_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holds_selected_entry: bool,
    recognized: bool,
    named: str | None,
) -> None:
    _require_descriptor_capture()
    (tmp_path / "kept.src").write_bytes(b"kept\n")
    folder = tmp_path / "X"
    folder.mkdir()
    (folder / "m.json").write_bytes(b"no")
    if holds_selected_entry:
        (folder / "inner.src").write_bytes(b"inner\n")
    selection = _selected_sources(
        markers=(OpaqueDirectoryMarker("m.json", 16, lambda data: data == b"ok"),),
    )

    snapshot = _capture(tmp_path, selection, portable=False, monkeypatch=monkeypatch)
    assert ("X" in snapshot.directories) is holds_selected_entry

    def change() -> None:
        if recognized:
            (folder / "m.json").write_bytes(b"ok")
        (folder / "late.src").write_bytes(b"late\n")

    _at_final_verification(monkeypatch, change)
    if named is not None:
        with pytest.raises(TreeChangedError) as error:
            _capture(tmp_path, selection, portable=False, monkeypatch=monkeypatch)
        assert error.value.relative == named
        return
    snapshot = _capture(tmp_path, selection, portable=False, monkeypatch=monkeypatch)
    assert snapshot.directories == ("",)
    assert snapshot.files == (("kept.src", b"kept\n"),)


@pytest.mark.parametrize("portable", [False, True])
def test_a_digest_capture_keeps_each_files_sha256_instead_of_its_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: bool,
) -> None:
    root = tmp_path / "tree"
    (root / "nested").mkdir(parents=True)
    (root / "empty").write_bytes(b"")
    (root / "nested" / "small").write_bytes(b"345")
    # Several reads long, so the digest has to cover every chunk.
    (root / "nested" / "large").write_bytes(os.urandom(tree_snapshot_module._READ_CHUNK_BYTES * 5 // 2))
    selection = _all_entries(TreeCaptureLimits())

    whole = _capture(root, selection, portable=portable, monkeypatch=monkeypatch)
    hashed = _capture(root, selection, portable=portable, monkeypatch=monkeypatch, digests=True)

    digests = tuple((name, hashlib.sha256(data).digest()) for name, data in whole.files)
    assert hashed == replace(whole, files=digests)


@pytest.mark.parametrize("portable", [False, True])
def test_a_digest_capture_counts_file_bytes_against_the_total_limit(
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
        digests=True,
    )

    assert snapshot.files == (("a", hashlib.sha256(b"12").digest()), ("b", hashlib.sha256(b"345").digest()))
    with pytest.raises(TreeCaptureLimitError, match=r"^directory tree exceeds max_total_bytes=4$"):
        _capture(
            root,
            _all_entries(TreeCaptureLimits(max_total_bytes=4)),
            portable=portable,
            monkeypatch=monkeypatch,
            digests=True,
        )


def test_a_digest_read_hashes_exactly_the_bytes_a_plain_read_returns() -> None:
    chunk = tree_snapshot_module._READ_CHUNK_BYTES
    data = os.urandom(chunk * 5 // 2)

    for max_bytes in (None, 0, chunk - 1, chunk, 2 * chunk + 7, len(data) - 1, len(data)):
        expected = data if max_bytes is None else data[: max_bytes + 1]
        plain = tree_snapshot_module._read_stream(io.BytesIO(data), max_bytes, digest=False)
        hashed = tree_snapshot_module._read_stream(io.BytesIO(data), max_bytes, digest=True)

        assert plain == (expected, len(expected))
        assert hashed == (hashlib.sha256(expected).digest(), len(expected))
