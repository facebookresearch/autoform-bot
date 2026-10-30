"""Failure-atomic replacement of generated files and directories.

``skeleton``, ``review prepare`` and ``library index`` publish through here.
A writer stages its output beside the destination as
``.<name>.autoform-stage-<16 hex>``, a name ``lean`` skips when it reads
project sources, and hands ``replace_outputs`` the identity the destination
had before staging. A packet tree is marked by ``PACKET_MANIFEST``, which
``lean`` also reads; ``validate_managed_output`` refuses to replace a
non-empty directory whose manifest does not name a known schema. Detected
failures raise ``SkeletonError``; ``stage_output``, ``remove_output`` and
the install preflight of ``replace_outputs`` let ``OSError`` through.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
import warnings
from pathlib import Path

from ._tree_snapshot import _normalized_name
from .lean import MANAGED_OUTPUT_SCHEMAS
from .lean_probe import SkeletonError

PACKET_MANIFEST = "manifest.json"


def output_identity(path: Path) -> tuple[int, int, str] | None:
    """``(st_dev, st_ino, digest)`` of a file or tree, or None when absent.

    The digest covers every name and file's bytes; a symlink or special file
    at or below ``path`` is refused.
    """

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkeletonError([f"cannot inspect output {path}: {exc}"]) from exc
    digest = hashlib.sha256()
    try:
        for child in [path, *sorted(path.rglob("*"))] if stat.S_ISDIR(metadata.st_mode) else [path]:
            if child.is_symlink():
                raise SkeletonError([f"refusing symlink in output: {child}"])
            digest.update(child.relative_to(path).as_posix().encode("utf-8") + b"\0")
            if child.is_dir():
                digest.update(b"directory\0")
            elif child.is_file():
                digest.update(b"file\0" + child.read_bytes() + b"\0")
            else:
                raise SkeletonError([f"refusing special file in output: {child}"])
    except OSError as exc:
        raise SkeletonError([f"cannot inspect output {path}: {exc}"]) from exc
    return metadata.st_dev, metadata.st_ino, digest.hexdigest()


def validate_managed_output(path: Path, *, kind: str, schema: str | None = None) -> tuple[int, int, str] | None:
    """``output_identity(path)``, refusing a non-directory, or a non-empty
    directory whose manifest does not name ``kind`` with a known schema
    (``schema`` pins one).
    """

    identity = output_identity(path)
    if identity is None:
        return identity
    if not path.is_dir():
        raise SkeletonError([f"output exists and is not a directory: {path}"])
    try:
        if not any(path.iterdir()):
            return identity
    except OSError as exc:
        raise SkeletonError([f"cannot inspect output {path}: {exc}"]) from exc
    manifest = path / PACKET_MANIFEST
    try:
        readable = manifest.is_file() and not manifest.is_symlink()
        payload = json.loads(manifest.read_text(encoding="utf-8")) if readable else None
    except (OSError, RecursionError, ValueError):
        payload = None
    if not (
        isinstance(payload, dict)
        and payload.get("kind") == kind
        and isinstance(payload.get("schema"), str)
        and (kind, payload.get("schema")) in (MANAGED_OUTPUT_SCHEMAS if schema is None else ((kind, schema),))
        and isinstance(payload.get(kind), list)
    ):
        raise SkeletonError([f"refusing to overwrite non-Autoform output: {path}"])
    return identity


def stage_output(destination: Path, content: str | None = None, *, stages: list[Path] | None = None) -> Path:
    """Create a same-directory stage, a directory or a file of ``content``, with the old mode.

    With ``stages``, the stage is registered there before it is created, so
    whoever owns ``stages`` removes it however this call or a later step ends.
    Without, a stage this call cannot finish is removed before the error leaves.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = not destination.is_symlink() and (
            destination.is_dir() if content is None else destination.is_file()
        )
        mode = stat.S_IMODE(destination.stat().st_mode) if existing else None
    except OSError as exc:
        raise SkeletonError([f"cannot inspect output {destination}: {exc}"]) from exc
    stage = destination.with_name(f".{destination.name}.autoform-stage-{secrets.token_hex(8)}")
    if stages is not None:
        stages.append(stage)
    try:
        stream = stage.mkdir() if content is None else stage.open("x", encoding="utf-8")
    except FileExistsError:
        # Never created here, so the owner of ``stages`` must not remove a foreign path.
        if stages is not None:
            stages.remove(stage)
        raise
    try:
        if stream is not None:
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        if mode is not None:
            stage.chmod(mode)
    except BaseException:
        if stages is None:
            with contextlib.suppress(OSError):
                remove_output(stage)
        raise
    return stage


def remove_output(path: Path) -> None:
    """Remove one transaction-owned file or directory without following links.

    A directory loses its manifest last: Lean source capture skips a folder by
    that marker, matched by folded name, so no packet may outlive it.
    """

    if path.is_dir() and not path.is_symlink():
        with os.scandir(path) as entries:
            children = [entry for entry in entries if _normalized_name(entry.name) != PACKET_MANIFEST]
        for child in children:
            if child.is_dir(follow_symlinks=False):
                shutil.rmtree(child.path)
            else:
                os.unlink(child.path)
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename ``source`` only when ``destination`` is absent."""

    if os.name == "nt":  # pragma: no cover - Windows-specific path
        os.rename(source, destination)
        return

    library = ctypes.CDLL(None, use_errno=True)
    source_bytes, destination_bytes = os.fsencode(source), os.fsencode(destination)
    try:
        if sys.platform.startswith("linux"):
            result = library.renameat2(-100, source_bytes, -100, destination_bytes, 1)
        elif sys.platform == "darwin":
            result = library.renamex_np(source_bytes, destination_bytes, 0x00000004)
        else:
            raise AttributeError(sys.platform)
    except AttributeError as exc:
        raise SkeletonError([f"atomic no-replace rename is unavailable on {sys.platform}"]) from exc
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _install_output(stage: Path, destination: Path) -> None:
    """Install a stage without overwriting a concurrent output."""

    metadata = stage.lstat()
    if stat.S_ISREG(metadata.st_mode):
        os.link(stage, destination)
        stage.unlink()
    else:
        _rename_no_replace(stage, destination)


def _preflight_output_installs(
    outputs: list[tuple[Path, Path, tuple[int, int, str] | None]],
) -> None:
    """Prove each destination filesystem supports its artifact install."""

    checked: set[tuple[int, int]] = set()
    for destination, stage, _ in outputs:
        try:
            key = (stat.S_IFMT(stage.lstat().st_mode), destination.parent.stat().st_dev)
        except OSError as exc:
            raise SkeletonError([f"cannot inspect output stage {stage}: {exc}"]) from exc
        if key[0] not in {stat.S_IFREG, stat.S_IFDIR}:
            raise SkeletonError([f"output stage is not a file or directory: {stage}"])
        if key in checked:
            continue
        checked.add(key)
        probe_root = Path(tempfile.mkdtemp(prefix=f".{destination.name}.autoform-preflight-", dir=destination.parent))
        source = probe_root / "source"
        try:
            source.touch(exist_ok=False) if key[0] == stat.S_IFREG else source.mkdir()
            _install_output(source, probe_root / "target")
        except BaseException:
            try:
                remove_output(probe_root)
            except OSError as cleanup_exc:
                warnings.warn(
                    f"could not remove output preflight artifacts at {probe_root}: {cleanup_exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            raise
        try:
            remove_output(probe_root)
        except OSError as cleanup_exc:
            raise SkeletonError(
                [f"could not remove output preflight artifacts at {probe_root}: {cleanup_exc}"]
            ) from cleanup_exc


def replace_outputs(
    outputs: list[tuple[Path, Path, tuple[int, int, str] | None]],
) -> None:
    """Commit staged outputs with rollback, but not cross-path linearizability.

    Each rename is atomic. Readers can still observe the interval between the
    renames; avoiding that requires one shared generation pointer rather than
    another rollback branch.
    """

    _preflight_output_installs(outputs)
    for destination, _, identity in outputs:
        if output_identity(destination) != identity:
            raise SkeletonError([f"output changed during publication: {destination}"])
    backups: dict[Path, tuple[Path, tuple[int, int, str]]] = {}
    installs: dict[Path, tuple[Path, tuple[int, int, str]]] = {}
    try:
        for destination, _, identity in outputs:
            if identity is None:
                continue
            backup = destination.with_name(
                f".{destination.name}.autoform-backup-{secrets.token_hex(8)}"
            )
            backups[destination] = (backup, identity)
            os.replace(destination, backup)
            if output_identity(backup) != identity:
                raise SkeletonError([f"output changed during publication: {destination}"])
        for destination, stage, _ in outputs:
            stage_identity = output_identity(stage)
            if stage_identity is None:
                raise SkeletonError([f"output stage disappeared: {stage}"])
            installs[destination] = (stage, stage_identity)
            _install_output(stage, destination)
    except BaseException as exc:
        rollback_issues: list[str] = []
        for destination, (_, expected) in reversed(installs.items()):
            try:
                destination_identity = output_identity(destination)
                if destination_identity is None:
                    continue
                if destination_identity != expected:
                    rollback_issues.append(
                        f"published output changed during rollback and was preserved at {destination}"
                    )
                    continue
                quarantine = destination.with_name(
                    f".{destination.name}.autoform-rollback-{secrets.token_hex(8)}"
                )
                os.replace(destination, quarantine)
                try:
                    quarantine_identity = output_identity(quarantine)
                    if quarantine_identity != expected:
                        rollback_issues.append(
                            f"changed rolled-back output was preserved for recovery at {quarantine}"
                        )
                        continue
                    remove_output(quarantine)
                except (OSError, SkeletonError) as cleanup_exc:
                    rollback_issues.append(
                        f"could not remove rolled-back output preserved for recovery at {quarantine}: {cleanup_exc}"
                    )
            except (OSError, SkeletonError) as rollback_exc:
                rollback_issues.append(
                    f"could not roll back output {destination}: {rollback_exc}"
                )
        for destination, (backup, expected) in backups.items():
            try:
                backup_identity = output_identity(backup)
                destination_identity = output_identity(destination)
                if backup_identity is not None and destination_identity is None:
                    _install_output(backup, destination)
                elif backup_identity is not None:
                    state = "previous output" if backup_identity == expected else "changed backup"
                    rollback_issues.append(f"{state} was preserved for recovery at {backup}")
                elif destination_identity != expected:
                    rollback_issues.append(
                        f"could not find previous output for {destination} during rollback"
                    )
            except (OSError, SkeletonError) as rollback_exc:
                rollback_issues.append(
                    f"could not restore output {destination}; previous output remains at {backup}: {rollback_exc}"
                )
        if not isinstance(exc, (OSError, SkeletonError)):
            raise
        issues = list(exc.issues) if isinstance(exc, SkeletonError) else [f"could not publish output: {exc}"]
        raise SkeletonError([*issues, *rollback_issues]) from exc
    for backup, expected in backups.values():
        try:
            backup_identity = output_identity(backup)
            if backup_identity == expected:
                remove_output(backup)
                continue
            message = (
                f"output backup disappeared before cleanup: {backup}"
                if backup_identity is None
                else f"output backup changed before cleanup and was preserved at {backup}"
            )
        except (OSError, SkeletonError) as cleanup_exc:
            message = f"could not remove previous output preserved at {backup}: {cleanup_exc}"
        warnings.warn(message, RuntimeWarning, stacklevel=2)
