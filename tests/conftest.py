"""Pytest configuration for autoform tests."""

from __future__ import annotations

import errno
import functools
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

# Helper modules that assert, rewritten as pytest rewrites the test files they came from.
pytest.register_assert_rewrite("tests.audit_fixture", "tests.work_fixture")

_SKELETON_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "skeleton-project"


def _check_lean_toolchain() -> bool:
    """Whether a project pinned like the skeleton fixture builds here without a download."""

    def unavailable(reason: str) -> bool:
        if os.environ.get("AUTOFORM_REQUIRE_REAL_LEAN_TESTS") == "1":
            raise RuntimeError(f"real Lean tests are required but unavailable: {reason}")
        return False

    if shutil.which("lake") is None:
        return unavailable("lake is not on PATH")
    elan = shutil.which("elan")
    if elan is None:
        return True
    pinned = (_SKELETON_FIXTURE / "lean-toolchain").read_text(encoding="utf-8").strip()
    try:
        listed = subprocess.run([elan, "toolchain", "list"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return unavailable("elan toolchain discovery failed")
    available = any(line.split()[:1] == [pinned] for line in listed.stdout.splitlines())
    return available or unavailable(f"{pinned} is not installed")


# For the skipif marks: `elan toolchain list` runs once per session, not once per mark.
# A raised RuntimeError is not cached, so every mark still fails closed.
_lean_toolchain_available = functools.cache(_check_lean_toolchain)


@pytest.fixture
def repo_root() -> Path:
    """Return the repository root directory."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def runtime_dir() -> Iterator[Path]:
    """A directory short enough to hold a Unix-domain socket.

    `sockaddr_un` caps a socket path near 108 bytes. On macOS `tmp_path` lands
    under `/private/var/folders/<hash>/T/pytest-of-<user>/pytest-<n>/<test-name>/`,
    which spends most of that budget before the runtime appends its own
    `lean-v<protocol>-<install>.sock`, so shared-runtime tests failed on the
    path rather than on the behaviour they assert. The limit is real and the
    production code handles it by keeping its own paths short, so the tests
    move instead of the check.

    Mode 0700 because the runtime refuses a directory any other user can read.
    """
    directory = Path(tempfile.mkdtemp(prefix="af-", dir="/tmp"))
    directory.chmod(0o700)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def unreadable_link(monkeypatch: pytest.MonkeyPatch) -> Callable[[Path, Path | str], None]:
    """Make a symbolic link that the system follows but will not let anyone read.

    macOS refuses `readlink` on a link whose own mode is 000 and still follows
    it. Where a link's mode does not bind, as on Linux or for root,
    `os.readlink` is made to refuse the link instead.
    """
    refused: set[str] = set()
    readlink = os.readlink

    def refusing(path: str | os.PathLike[str], **kwargs: object) -> str:
        if os.fspath(path) in refused:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), os.fspath(path))
        return readlink(path, **kwargs)

    monkeypatch.setattr(os, "readlink", refusing)

    def make(link: Path, target: Path | str) -> None:
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks are unavailable")
        if hasattr(os, "lchmod"):
            os.lchmod(link, 0)
        try:
            readlink(link)
        except OSError:
            return
        refused.update({os.fspath(link), os.path.join(os.path.realpath(link.parent), link.name)})

    return make
