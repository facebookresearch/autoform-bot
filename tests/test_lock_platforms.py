"""Regression for the Intel macOS ``cryptography`` constraint.

``cryptography`` stopped publishing x86_64 macOS wheels at 49.0.0, so resolving the lock for an
Intel Mac without source builds failed and the MCP servers could not start there. The Linux CI
matrix cannot observe that, so these tests resolve the committed lock for each platform.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(UV is None, reason="uv is not available")


def _uv(*args: str) -> subprocess.CompletedProcess[str]:
    assert UV is not None
    return subprocess.run([UV, *args], cwd=ROOT, capture_output=True, text=True, timeout=300, check=False)


def _locked_cryptography(platform: str) -> tuple[int, ...]:
    result = _uv("tree", "--locked", "--python-platform", platform)
    assert result.returncode == 0, result.stderr
    versions = set(re.findall(r"cryptography v(\d+(?:\.\d+)*)", result.stdout))
    assert len(versions) == 1, f"expected one cryptography version for {platform}, got {versions}"
    return tuple(int(part) for part in versions.pop().split("."))


def test_intel_macos_resolves_without_source_builds() -> None:
    result = _uv(
        "sync",
        "--locked",
        "--dry-run",
        "--no-build",
        "--no-cache",
        "--offline",
        "--no-install-project",
        "--python-platform",
        "x86_64-apple-darwin",
    )
    assert result.returncode == 0, result.stderr


def test_intel_macos_keeps_cryptography_with_wheels() -> None:
    assert _locked_cryptography("x86_64-apple-darwin") < (49,)


@pytest.mark.parametrize("platform", ["aarch64-apple-darwin", "x86_64-manylinux_2_28"])
def test_constraint_does_not_apply_elsewhere(platform: str) -> None:
    assert _locked_cryptography(platform) >= (49,)
