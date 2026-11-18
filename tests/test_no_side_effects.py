"""The `no_side_effects` fixture: what a guarded call may do, and each step it fails the test for."""

from __future__ import annotations

import importlib
import multiprocessing
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import pytest


def test_a_guarded_call_may_read_files_and_import_modules(
    tmp_path: Path, no_side_effects: AbstractContextManager[None]
) -> None:
    (tmp_path / "notes.txt").write_text("kept", encoding="utf-8")
    (tmp_path / "guarded_import_probe.py").write_text("VALUE = 1\n", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        with no_side_effects:
            assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "kept"
            # A first import writes no bytecode under the guard, so it is no write.
            assert importlib.import_module("guarded_import_probe").VALUE == 1
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("guarded_import_probe", None)
    assert not (tmp_path / "__pycache__").exists()


def test_a_guarded_call_may_open_a_descriptor(no_side_effects: AbstractContextManager[None]) -> None:
    # As multiprocessing opens its pipe on Windows before it starts the process, which the guard then stops.
    read_end, write_end = os.pipe()
    try:
        with no_side_effects:
            with open(write_end, "wb", closefd=False):
                pass
    finally:
        os.close(read_end)
        os.close(write_end)


def _swallowed_write(directory: Path) -> None:
    try:
        (directory / "swallowed.txt").write_text("x", encoding="utf-8")
    except OSError:
        pass


#: Each step a guarded call must not take, with the audit event the failure names. The guard this replaced
#: compared the files under tmp_path and patched four calls, so it missed a write outside tmp_path, a new
#: directory, a multiprocessing start and a posix_spawn.
_SIDE_EFFECTS: list[tuple[str, Callable[[Path, Path], object], str]] = [
    ("a write outside tmp_path", lambda _tmp, outside: (outside / "x.txt").write_text("x", encoding="utf-8"), "open"),
    ("a new directory", lambda tmp, _outside: os.mkdir(tmp / "new"), "os.mkdir"),
    ("a write the code swallows", lambda tmp, _outside: _swallowed_write(tmp), "open"),
    ("a rename", lambda tmp, _outside: os.rename(tmp / "kept.txt", tmp / "moved.txt"), "os.rename"),
    ("a removal", lambda tmp, _outside: os.remove(tmp / "kept.txt"), "os.remove"),
    ("a subprocess", lambda _tmp, _outside: subprocess.run([sys.executable, "-c", "pass"], check=False), "Popen"),
    ("a socket", lambda _tmp, _outside: socket.socket().close(), "socket.__new__"),
    (
        "a multiprocessing start",
        lambda _tmp, _outside: multiprocessing.get_context("spawn").Process(target=print).start(),
        "_posixsubprocess.fork_exec" if os.name == "posix" else "_winapi.CreateProcess",
    ),
]
if hasattr(os, "posix_spawn"):
    _SIDE_EFFECTS.append(
        (
            "a posix_spawn",
            lambda _tmp, _outside: os.posix_spawn(sys.executable, [sys.executable, "-c", "pass"], os.environ),
            "os.posix_spawn",
        )
    )


@pytest.mark.parametrize(
    ("name", "step", "event"), [pytest.param(name, step, event, id=name) for name, step, event in _SIDE_EFFECTS]
)
def test_a_guarded_call_fails_the_test_for_each_side_effect(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    no_side_effects: AbstractContextManager[None],
    name: str,
    step: Callable[[Path, Path], object],
    event: str,
) -> None:
    (tmp_path / "kept.txt").write_text("kept", encoding="utf-8")
    outside = tmp_path_factory.mktemp("outside")
    finished: list[str] = []

    with pytest.raises(AssertionError, match=event.replace(".", r"\.")):
        with no_side_effects:
            step(tmp_path, outside)
            finished.append(name)

    # Each step was stopped, not only reported: it raised before it could finish, so no process ran and no
    # socket opened, and it left nothing behind. Only the swallowed write finishes, having caught the error.
    assert finished == ([name] if name == "a write the code swallows" else [])
    assert sorted(path.name for path in tmp_path.iterdir()) == ["kept.txt"]
    assert list(outside.iterdir()) == []


def test_guarded_blocks_do_not_nest(tmp_path: Path, no_side_effects: AbstractContextManager[None]) -> None:
    with pytest.raises(AssertionError, match=r"os\.mkdir"):
        with no_side_effects:
            with pytest.raises(RuntimeError, match="do not nest"):
                with no_side_effects:
                    pass
            # The refused inner block left the outer one watching.
            os.mkdir(tmp_path / "after")

    assert not (tmp_path / "after").exists()
