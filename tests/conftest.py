"""Pytest configuration for autoform tests."""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest


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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep every Nth collected test when ``AUTOFORM_TEST_SHARD`` is ``i/N``.

    CI splits the real-Lean skeleton suite across parallel jobs this way. Taking
    tests round-robin in collection order spreads its few slow tests, which
    sit next to each other, across the jobs.
    """
    shard = os.environ.get("AUTOFORM_TEST_SHARD")
    if not shard:
        return
    index, _, total = shard.partition("/")
    if not (index.isdigit() and total.isdigit() and int(index) < int(total)):
        raise pytest.UsageError(f"AUTOFORM_TEST_SHARD must be i/N with 0 <= i < N, not {shard!r}")
    kept = [item for position, item in enumerate(items) if position % int(total) == int(index)]
    config.hook.pytest_deselected(items=[item for item in items if item not in set(kept)])
    items[:] = kept
