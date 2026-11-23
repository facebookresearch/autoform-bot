"""A retained directory gives its descriptor back once and is unusable afterwards."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autoform_cli._directory_binding import DIRECTORY_BINDING_SUPPORTED, open_directory

pytestmark = pytest.mark.skipif(not DIRECTORY_BINDING_SUPPORTED, reason="needs O_DIRECTORY, O_NOFOLLOW and dir_fd")


def test_a_closed_binding_refuses_use_and_never_closes_its_number_again(tmp_path: Path) -> None:
    retained = open_directory(tmp_path)
    number = retained.descriptor
    retained.close()

    with pytest.raises(OSError, match="closed"):
        retained.descriptor
    # A file opened since may hold the freed number; closing the binding again, as __del__ does, leaves it open.
    reused = os.open(tmp_path, os.O_RDONLY)
    if reused != number:
        os.dup2(reused, number)
        os.close(reused)
    try:
        retained.close()
        os.fstat(number)
    finally:
        try:
            os.close(number)
        except OSError:
            pass
