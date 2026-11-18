"""A path the system refuses to examine, as one below a folder that cannot be searched."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest


def unexaminable(monkeypatch: pytest.MonkeyPatch, path: Path, *, follow_symlinks: bool | None = None) -> None:
    """Make ``os.stat`` and ``os.lstat`` refuse ``path`` with PermissionError, for any user.

    With ``follow_symlinks``, only the calls that do, or do not, follow a link
    there are refused; ``os.lstat`` does not. Before Python 3.14, pathlib's
    checks raise this error; from 3.14 they return False.
    """

    denied = os.fspath(path)
    real_stat, real_lstat = os.stat, os.lstat

    def refuse(target: object, follows: bool) -> None:
        named = isinstance(target, (str, os.PathLike)) and os.fspath(target) == denied
        if named and follow_symlinks in (None, follows):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), denied)

    def stat(target, *args, **kwargs):
        refuse(target, kwargs.get("follow_symlinks", True))
        return real_stat(target, *args, **kwargs)

    def lstat(target, *args, **kwargs):
        refuse(target, False)
        return real_lstat(target, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "lstat", lstat)
