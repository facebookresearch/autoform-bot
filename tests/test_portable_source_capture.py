"""Lean sources and code links through the one tree capture, on every system CI runs, Windows included."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli._tree_snapshot import ALL_ENTRIES, bind_directory_tree
from autoform_cli.lean import LeanSourceError, index_project, snapshot_project_sources
from tests.ui.test_render import _project


@pytest.mark.parametrize("capture", [snapshot_project_sources, index_project])
def test_lean_sources_are_read_on_every_system(tmp_path: Path, capture) -> None:
    (tmp_path / "A.lean").write_text("theorem readEverywhere : True := trivial\n", encoding="utf-8")

    result = capture(tmp_path)

    index = result.index if capture is snapshot_project_sources else result
    assert set(index.declarations) == {"readEverywhere"}


@pytest.mark.parametrize("explicit_root", [False, True])
def test_render_links_declarations_on_every_system(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    explicit_root: bool,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "site-src"
    arguments = ["render", str(project / "blueprint"), "--output", str(output), "--require-declarations"]
    if explicit_root:
        arguments += ["--lean-root", str(project)]

    status = main(arguments)

    stdout = capsys.readouterr().out
    assert status == 0
    assert "warning:" not in stdout
    assert "2 code links" in stdout
    assert "Project/Basic.lean" in (output / "roadmap" / "README.md").read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")
def test_a_junction_is_recorded_as_a_link_and_not_followed(tmp_path: Path) -> None:
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Outside.lean").write_text("theorem outside : True := trivial\n", encoding="utf-8")
    root = tmp_path / "tree"
    root.mkdir()
    _winapi.CreateJunction(str(outside), str(root / "junction"))

    with bind_directory_tree(root, selection=ALL_ENTRIES) as bound:
        snapshot = bound.capture()

    assert [path for path, _target in snapshot.symlinks] == ["junction"]
    assert snapshot.files == ()
    # A POSIX link to a folder is skipped by the Lean selection; a junction is refused. Neither is followed.
    with pytest.raises(LeanSourceError, match="unsafe Lean source junction: symbolic links are not supported"):
        index_project(root)
