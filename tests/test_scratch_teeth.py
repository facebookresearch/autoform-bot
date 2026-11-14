"""Scratch only: run scratch_teeth/teeth.py on the mutants, once, in the 3.13 test job."""

import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(
    sys.version_info[:2] != (3, 13) or not os.environ.get("AUTOFORM_REQUIRE_NODE_TESTS"),
    reason="the mutants run once, in the 3.13 test job",
)
def test_every_mutant_is_killed(capsys: pytest.CaptureFixture[str]) -> None:
    result = subprocess.run(
        [sys.executable, "scratch_teeth/teeth.py", "scratch_teeth/mutants.json"],
        capture_output=True,
        text=True,
        check=False,
    )
    with capsys.disabled():
        print(result.stdout)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
