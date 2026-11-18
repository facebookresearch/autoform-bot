"""The article ID a new article is proposed, and the file reads and writes that record it."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from ._fs import atomic_write


def _read_regular(path: Path) -> tuple[bytes, int]:
    """The bytes and permission bits of a regular file, refusing anything else."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read(), stat.S_IMODE(metadata.st_mode)
    finally:
        os.close(descriptor)


def _replace_if_unchanged(path: Path, expected_sha256: str, content: bytes, mode: int) -> None:
    """Replace *path* from a same-directory temporary file, unless it changed since it was planned."""

    def unchanged() -> None:
        current, _ = _read_regular(path)
        if hashlib.sha256(current).hexdigest() != expected_sha256:
            raise OSError("the article changed while the plan was applied; run the command again")

    atomic_write(path, content, mode=mode, check=unchanged)


def _proposed_id(path_id: str, source_sha256: str) -> str:
    digest = hashlib.sha256(b"autoform-article-id/v1\0")
    encoded_path = path_id.encode("utf-8")
    encoded_source = source_sha256.encode("ascii")
    digest.update(len(encoded_path).to_bytes(8, "big"))
    digest.update(encoded_path)
    digest.update(encoded_source)
    return f"af_{digest.hexdigest()[:24]}"
