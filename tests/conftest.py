"""Pytest configuration for autoform tests."""

from __future__ import annotations

import errno
import functools
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

try:
    import _posixsubprocess
except ImportError:  # Windows starts processes through _winapi.CreateProcess, which an audit hook sees.
    _posixsubprocess = None

# Helper modules that assert, rewritten as pytest rewrites the test files they came from.
pytest.register_assert_rewrite("tests.audit_fixture", "tests.dom_fixture", "tests.work_fixture")

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


#: Audit events that change the file system, start a process or reach the network. `open` counts by its mode.
_SIDE_EFFECT_EVENTS = frozenset({
    "os.chflags", "os.chmod", "os.chown", "os.link", "os.mkdir", "os.remove", "os.removexattr", "os.rename",
    "os.rmdir", "os.setxattr", "os.symlink", "os.truncate", "os.utime", "shutil.chown", "shutil.copyfile",
    "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.make_archive", "shutil.move", "shutil.rmtree",
    "shutil.unpack_archive", "sqlite3.connect", "tempfile.mkdtemp", "tempfile.mkstemp",
    "_posixsubprocess.fork_exec", "_winapi.CreateProcess", "os.exec", "os.fork", "os.forkpty", "os.posix_spawn",
    "os.spawn", "os.startfile", "os.system", "subprocess.Popen", "socket.__new__", "socket.getaddrinfo",
})
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
#: What the code under a `no_side_effects` block did, or None outside one. An audit hook cannot be removed, so one
#: hook, installed on first use, reads this.
_side_effects: list[str] | None = None
_side_effect_hook = False


def _audit_side_effect(event: str, args: tuple[object, ...]) -> None:
    effects = _side_effects
    if effects is None:
        return
    if event == "open":
        mode, flags = args[1], args[2]
        if isinstance(mode, str):
            writing = bool(set(mode) & set("wax+"))
        else:
            writing = isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        if not writing:
            return
    elif event not in _SIDE_EFFECT_EVENTS:
        return
    effects.append(f"{event} {args!r:.300}")
    # Stopped too. Code that catches the error still fails the test, through the list.
    raise PermissionError(f"the code under test must not {event}")


class _NoSideEffects(AbstractContextManager[None]):
    def __enter__(self) -> None:
        global _side_effect_hook, _side_effects
        if _side_effects is not None:
            # An inner block would end the outer one's watch and restore its own patches over the outer's.
            raise RuntimeError("no_side_effects blocks do not nest")
        if not _side_effect_hook:
            sys.addaudithook(_audit_side_effect)
            _side_effect_hook = True
        # A module first imported under the block would otherwise write its bytecode.
        self._bytecode, sys.dont_write_bytecode = sys.dont_write_bytecode, True
        # Before 3.14, multiprocessing starts a process through fork_exec with no audit event.
        self._fork_exec = getattr(_posixsubprocess, "fork_exec", None)
        if self._fork_exec is not None:
            _posixsubprocess.fork_exec = self._refuse_fork_exec
        _side_effects = []

    def _refuse_fork_exec(self, *args: object) -> object:
        _audit_side_effect("_posixsubprocess.fork_exec", args)
        # Reached only once the block has ended, as another thread may still call it.
        return self._fork_exec(*args)

    def __exit__(self, *_exc: object) -> None:
        global _side_effects
        if self._fork_exec is not None:
            _posixsubprocess.fork_exec = self._fork_exec
        sys.dont_write_bytecode = self._bytecode
        effects, _side_effects = _side_effects, None
        assert not effects, "the code under test wrote a file, started a process or reached the network:\n" + "\n".join(
            effects or ()
        )


@pytest.fixture
def no_side_effects() -> AbstractContextManager[None]:
    """A block whose code may read files but not write one anywhere, start a process or open a socket.

    Each such step is stopped and fails the test, even when the code catches the error it raises, so a call that
    promises to write nothing and start nothing is held to it. Set up files before the block, not in it.
    """
    return _NoSideEffects()
