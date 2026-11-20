"""What render reports, and how it puts the site it built in place of the output directory.

Committing a site: it is built and synced in a hidden
``.autoform-publication-*`` directory beside the output, then swapped in with
one atomic exchange (``renamex_np`` on macOS, ``renameat2`` on Linux); renders
committing into the same parent directory take turns when it can be locked. On
filesystems without the exchange (NFS, SMB, 9p or WSL2 ``/mnt/c``, exFAT,
sshfs) the previous site is first renamed to
``.autoform-publication-backup-<name>``; if render dies between the two
renames, the next render moves it back before anything else. macOS metadata
files are ignored when an existing site is matched against its inventory.
Render never indexes a leftover staging directory as Lean source. On macOS
``fsync`` does not flush the drive's cache, so a power loss can still lose the
newest site. Render reads the output by path in two walks
that must agree. Native Windows commits through the backup swap without a lock.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import secrets
import shutil
import stat
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .._tree_snapshot import TreeSnapshotError, bind_directory_tree
from ..graph import PublicationError
from ..lean import _REPLACEABLE_SCHEMAS, PUBLICATION_MANIFEST


#: Hidden siblings of the output: a site being built, a replaced one, or a backup.
_STAGE_PREFIX = ".autoform-publication-"
#: How a filesystem says it lacks an operation (an atomic exchange, a directory sync).
_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP})
#: Rename refusals that strerror alone would leave puzzling.
_REFUSED_COMMITS = {
    errno.EBUSY: "the output directory is a mount point or in use",
    errno.EXDEV: "the output directory is on another filesystem than its parent "
    "(a mount point or an overlay lower layer)",
}
#: How long a render waits for another render committing into the same parent.
_PUBLICATION_LOCK_WAIT_SECONDS = 30.0


@dataclass(slots=True)
class RenderReport:
    """What a render produced, and what it could not resolve."""

    output_dir: Path
    pages: int = 0
    nodes: int = 0
    linked: int = 0
    unresolved: list[str] = field(default_factory=list)
    """A message for each published declaration not found, or defined more than once, in the sources."""
    warnings: list[str] = field(default_factory=list)


#: An output render may replace: its identity and its files' digests (none for an empty one).
_State = tuple[tuple[int, int], dict[str, str]]


def _is_os_metadata(name: str) -> bool:
    base = PurePosixPath(name).name
    return base == ".DS_Store" or base.startswith("._")


def _destination_state(path: Path) -> _State | None:
    """The state of an empty directory or an exact Autoform site at *path*.

    ``None`` means nothing is there. Anything render does not own is refused: a
    symlink, a file, a legacy or foreign directory, or a site whose files no
    longer match its manifest. macOS metadata files (Finder's ``.DS_Store`` and
    the ``._*`` files it writes on non-APFS volumes) are disposable and leave
    with the directory that holds them.
    """
    try:
        metadata = os.lstat(path)
        empty = stat.S_ISDIR(metadata.st_mode) and all(
            _is_os_metadata(name) and stat.S_ISREG(os.lstat(path / name).st_mode) for name in os.listdir(path)
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PublicationError([f"could not inspect {path}: {error.strerror or error}"]) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise PublicationError(["refusing symlink output directory"])
    if not stat.S_ISDIR(metadata.st_mode):
        raise PublicationError(["output path exists and is not a directory"])
    identity = (metadata.st_dev, metadata.st_ino)
    if empty:
        return identity, {}
    manifest = None
    try:  # read before anything below it
        if stat.S_ISREG(os.lstat(path / PUBLICATION_MANIFEST).st_mode):
            with open(path / PUBLICATION_MANIFEST, "rb") as handle:
                manifest = json.loads(handle.read())
    except (OSError, ValueError, RecursionError):
        pass
    if isinstance(manifest, dict) and manifest.get("schema") == "autoform-publication/v1":
        raise PublicationError(
            [
                f"refusing to replace the legacy autoform-publication/v1 site at {path}: it has no file "
                "inventory to check before deleting it; delete it once by hand and render again"
            ]
        )
    schema = manifest.get("schema") if isinstance(manifest, dict) else None
    if not (isinstance(schema, str) and schema in _REPLACEABLE_SCHEMAS):
        raise PublicationError(
            [
                "refusing to overwrite a non-Autoform output directory or legacy "
                "publication; choose an empty directory or remove it explicitly"
            ]
        )
    if manifest.get("complete") is not True:
        raise PublicationError(["refusing to overwrite an incomplete Autoform publication"])
    expected = manifest.get("files")
    if not isinstance(expected, dict):
        raise PublicationError(["publication manifest has no valid file inventory"])
    expected = {name: digest for name, digest in expected.items() if not _is_os_metadata(name)}
    try:
        with bind_directory_tree(path, expected_identity=identity) as tree:
            snapshot = tree.capture(digests=True)
    except TreeSnapshotError as error:
        raise PublicationError([f"could not inspect the output directory: {error}"]) from error
    files = {
        name: digest.hex()
        for name, digest in snapshot.files
        if name != PUBLICATION_MANIFEST and not _is_os_metadata(name)
    }
    names = {*files, *dict(snapshot.symlinks), *dict(snapshot.special)}
    if names != expected.keys():
        untracked = ", ".join(sorted(names ^ expected.keys()))
        raise PublicationError(
            [f"refusing to overwrite an output directory with untracked or missing files: {untracked}"]
        )
    if sorted(name for name in snapshot.directories if name) != manifest.get("directories"):
        raise PublicationError(
            ["refusing to overwrite an output directory with untracked or missing directories"]
        )
    if files != expected:
        raise PublicationError(["refusing to overwrite a modified Autoform publication"])
    return identity, files


def _publish(
    destination: Path, expected: _State | None, render: Callable[[Path], RenderReport]
) -> RenderReport:
    """Render into a hidden sibling of *destination*, then swap it into place.

    The previous site is deleted only once it is proved to be the exact site
    inspected before rendering; any other outcome keeps it.
    """
    stage = destination.with_name(f"{_STAGE_PREFIX}{secrets.token_hex(8)}")
    backup = _backup(destination)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(stage)  # the umask decides published modes
    except OSError as error:
        raise PublicationError(
            [
                f"could not create a publication stage in {destination.parent}: {error.strerror or error}; "
                "render needs write access to the output directory's parent"
            ]
        ) from error
    staged = _identity(stage)
    try:
        try:
            report = render(stage)
            for path in (*stage.rglob("*"), stage):
                _fsync(path)
        except OSError as error:
            raise PublicationError([f"could not write the site: {error}; output was not changed"]) from error
        with _locked_parent(destination.parent):
            if _destination_state(destination) != expected:
                unchanged = "output was not changed" if expected is None else "previous site was preserved"
                raise PublicationError([f"output directory changed during publication; {unchanged}"])
            try:
                _commit(stage, destination, backup, replace=expected is not None)
                with suppress(PermissionError):  # nor can such a parent be opened to sync
                    _fsync(destination.parent)
            except OSError as error:
                if _identity(stage) == staged and not os.path.lexists(backup):  # nothing moved
                    reason = _REFUSED_COMMITS.get(error.errno, error.strerror or str(error))
                    message = f"could not publish {destination}: {reason}; output was not changed"
                    raise PublicationError([message]) from error
                kept = next((path for path in (backup, stage) if os.path.lexists(path)), None)
                message = f"publishing {destination} may be incomplete: {error.strerror or error}"
                kept_note = f", and {kept} may hold the previous site" if kept else ""
                raise PublicationError([f"{message}; nothing was deleted{kept_note}"]) from error
        if expected is not None:  # the replaced site now sits at the stage's name
            report.warnings.extend(_retire(stage, expected))
        report.output_dir = destination
        return report
    finally:
        if _identity(stage) == staged:  # still the unpublished stage
            with suppress(OSError):
                _remove_tree(stage)


@contextmanager
def _locked_parent(parent: Path) -> Iterator[None]:
    """Hold the output parent's lock, waiting while another render commits beside this one.

    A filesystem without advisory locks (NFS without lockd) or a host without
    them (Windows) proceeds unlocked; the commit still re-checks the output it
    replaces.
    """
    if os.name == "nt":
        yield
        return
    import fcntl

    try:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except PermissionError:  # a parent the user may write but not read cannot hold the lock
        descriptor = None
    except OSError as error:
        message = f"could not open the output parent {parent}: {error.strerror or error}"
        raise PublicationError([message]) from error
    if descriptor is None:
        yield
        return
    try:
        deadline = time.monotonic() + _PUBLICATION_LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise PublicationError(
                        [
                            f"another process kept the output parent {parent} locked for "
                            f"{_PUBLICATION_LOCK_WAIT_SECONDS:g} seconds; output was not changed; retry"
                        ]
                    ) from error
                time.sleep(0.05)
            except OSError:
                break
        yield
    finally:
        os.close(descriptor)  # releases the lock


def _commit(stage: Path, destination: Path, backup: Path, *, replace: bool) -> None:
    """Move *stage* to *destination*; a replaced site ends up at *stage*'s name.

    One atomic exchange where the filesystem has it. Elsewhere (NFS, SMB, 9p,
    exFAT, ...) the old site moves to *backup* first, and the next render moves
    it back if this one dies before the second rename.
    """
    if not replace:
        os.rename(stage, destination)  # atomic; refuses a non-empty directory and a file
        return
    try:
        _exchange(stage, destination)
        return
    except OSError as error:
        if error.errno not in _UNSUPPORTED:
            raise
    os.rename(destination, backup)
    try:
        os.rename(stage, destination)
    except BaseException:
        with suppress(OSError):  # after a Ctrl-C that landed past the install, the new site stays
            os.rename(backup, destination)
        raise
    os.rename(backup, stage)  # no backup outlives the lock


def _exchange(first: Path, second: Path) -> None:
    """Swap two names in one step: RENAME_SWAP on macOS, RENAME_EXCHANGE on Linux."""
    if os.name == "nt":
        raise OSError(errno.ENOSYS, "atomic exchange is unavailable", str(second))
    library = ctypes.CDLL(None, use_errno=True)
    first_bytes, second_bytes = os.fsencode(first), os.fsencode(second)
    try:
        if sys.platform == "darwin":
            result = library.renamex_np(first_bytes, second_bytes, 0x00000002)
        else:
            result = library.renameat2(-100, first_bytes, -100, second_bytes, 2)
    except AttributeError:  # a C library without renameat2 (musl, glibc before 2.28)
        raise OSError(errno.ENOSYS, "atomic exchange is unavailable", str(second)) from None
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(second))


def _backup(destination: Path) -> Path:
    return destination.with_name(f"{_STAGE_PREFIX}backup-{destination.name}")


def _recover_backup(destination: Path) -> list[str]:
    """Finish a two-rename replacement that a killed render left half done.

    Beside a missing or empty output the backup is the site, so it goes back;
    beside an exact site it is the replaced one, so it goes. Anything else is
    left for the user. Returns warnings for the report.
    """
    backup = _backup(destination)
    if not os.path.lexists(backup):
        return []
    with _locked_parent(destination.parent):
        try:
            state = _destination_state(destination)
            if state is None or not state[1]:
                os.rename(backup, destination)  # also replaces an empty directory
                return []
            replaced = _destination_state(backup)
            if replaced is None:
                return []
            retired = destination.with_name(f"{_STAGE_PREFIX}{secrets.token_hex(8)}")
            os.rename(backup, retired)
        except (OSError, PublicationError) as error:
            raise PublicationError(
                [f"could not recover the previous site at {backup}: {error}; restore or remove it by hand"]
            ) from error
    return _retire(retired, replaced)


def _retire(site: Path, expected: _State) -> list[str]:
    """Delete a replaced site that still equals *expected*; otherwise say where it is."""
    try:
        unchanged = _destination_state(site) == expected
    except (OSError, PublicationError):
        unchanged = False
    if not unchanged:
        return [f"the previous site was kept at {site}: it changed during the commit"]
    try:
        _remove_tree(site)
    except OSError as error:
        reason = error.strerror or error
        return [f"the previous site at {site} was only partly deleted: {reason}; remove the rest by hand"]
    return []


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    return metadata.st_dev, metadata.st_ino


def _fsync(path: Path) -> None:
    """Flush one file or directory; some network filesystems cannot sync a directory.

    Windows cannot open a directory to sync it, and flushes only a writable handle.
    """
    if os.name == "nt":
        if path.is_file():
            with open(path, "r+b") as handle:
                os.fsync(handle.fileno())
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in _UNSUPPORTED or not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise
    finally:
        os.close(descriptor)


def _remove_tree(path: Path) -> None:
    """Delete a site or stage, its ``publication.json`` last.

    Until then a leftover of an interrupted deletion still carries the marker
    that keeps Lean walks out of it. The root itself goes with rmdir, so a file
    saved into it meanwhile stops the deletion instead of being lost. An entry
    already gone is skipped: on a volume without native extended attributes,
    macOS deletes ``._X`` along with ``X``.
    """
    with os.scandir(path) as entries:
        children = [entry for entry in entries if entry.name != PUBLICATION_MANIFEST]
    for child in children:
        if not child.is_dir(follow_symlinks=False):
            with suppress(FileNotFoundError):
                os.unlink(child.path)
        elif sys.version_info >= (3, 12):
            shutil.rmtree(child.path, onexc=_skip_missing)
        else:
            shutil.rmtree(child.path, onerror=_skip_missing)
    with suppress(FileNotFoundError):  # a stage that failed early has none
        os.unlink(path / PUBLICATION_MANIFEST)
    os.rmdir(path)


def _skip_missing(_function: object, _path: str, error: BaseException | tuple) -> None:
    """An rmtree error handler (``onexc``, or ``onerror``'s exc_info) that skips vanished entries."""
    exception = error[1] if isinstance(error, tuple) else error
    if not isinstance(exception, FileNotFoundError):
        raise exception
