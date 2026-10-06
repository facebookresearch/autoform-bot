from __future__ import annotations

import os
from pathlib import Path

import pytest

from autoform_cli.lean import index_project, snapshot_project_sources
from autoform_cli.render import PublicationError, render_site


@pytest.mark.skipif(os.name != "nt", reason="Windows capability boundary")
@pytest.mark.parametrize("capture", [snapshot_project_sources, index_project])
def test_windows_source_inspection_fails_closed_without_descriptor_traversal(
    tmp_path: Path,
    capture,
) -> None:
    (tmp_path / "A.lean").write_text(
        "theorem notReadUnsafely : True := trivial\n",
        encoding="utf-8",
    )

    with pytest.raises(OSError, match="safe directory traversal is unavailable"):
        capture(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows capability boundary")
def test_windows_render_fails_before_writing_or_changing_the_output(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    blueprint.mkdir()
    output = tmp_path / "site-src"
    output.mkdir()
    sentinel = output / "PRECIOUS"
    sentinel.write_text("keep\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="unavailable on this platform"):
        render_site(blueprint, output)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert list(output.iterdir()) == [sentinel]
    assert not list(tmp_path.glob(".autoform-publication-*"))
