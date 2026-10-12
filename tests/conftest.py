"""Pytest configuration for autoform tests."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def _session_cache_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("cache")


@pytest.fixture(autouse=True)
def _private_cache_home(_session_cache_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test out of the user's cache directory.

    The directory is shared by the whole session, so real-Lean tests after the
    first reuse its compiled probe helpers, as later extractions do.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(_session_cache_home))


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
