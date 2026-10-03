"""Regression for the Intel macOS ``cryptography`` constraint.

``cryptography`` stopped publishing x86_64 macOS wheels at 49.0.0, so resolving the lock for an
Intel Mac without source builds failed and the MCP servers could not start there. The Linux CI
matrix cannot observe that, so these tests resolve the committed lock for each platform, and
check that pyproject.toml alone keeps every other platform on 49 or later.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(UV is None, reason="uv is not available")


def _uv(*args: str) -> subprocess.CompletedProcess[str]:
    assert UV is not None
    # Use the interpreter running the tests, so uv neither searches for nor downloads one.
    command = [UV, *args, "--python", sys.executable]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=300, check=False)


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


@pytest.mark.parametrize(
    ("sys_platform", "machine", "accepted", "refused"),
    [
        ("darwin", "x86_64", "48.0.1", "49.0.0"),
        ("darwin", "arm64", "50.0.0", "48.0.1"),
        ("linux", "x86_64", "50.0.0", "48.0.1"),
        ("linux", "aarch64", "50.0.0", "48.0.1"),
        ("win32", "AMD64", "50.0.0", "48.0.1"),
    ],
)
def test_constraints_split_cryptography_without_the_lock(
    sys_platform: str, machine: str, accepted: str, refused: str
) -> None:
    # uv carries a lock's versions forward as preferences, so the lock can show this split while
    # pyproject.toml does not state it; a fresh or upgraded lock then resolves 48.x everywhere.
    with (ROOT / "pyproject.toml").open("rb") as handle:
        constraints = map(Requirement, tomllib.load(handle)["tool"]["uv"]["constraint-dependencies"])
    environment = {"sys_platform": sys_platform, "platform_machine": machine}
    specifiers = [
        constraint.specifier
        for constraint in constraints
        if constraint.name == "cryptography" and (constraint.marker is None or constraint.marker.evaluate(environment))
    ]
    assert all(specifier.contains(accepted) for specifier in specifiers)
    assert not all(specifier.contains(refused) for specifier in specifiers)
