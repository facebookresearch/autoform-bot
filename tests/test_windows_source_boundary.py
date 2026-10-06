from __future__ import annotations

import os
from pathlib import Path

import pytest

from autoform_cli.lean import index_project, snapshot_project_sources


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
