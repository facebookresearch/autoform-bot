"""Report a repository's linked worktrees, and remove the ones a reviewed report lists.

``project worktrees`` reads every linked worktree of the repository holding its
target, except the main worktree and the target's own, and says whether each is
removable or what protects it. It changes nothing: Git runs with
``--no-optional-locks``, and the claim board is read through a temporary
scratch. ``--remove`` takes a saved report and removes only the worktrees it
marks removable, each through ``git worktree remove`` without ``--force``, after
checking again that it still has the same commit and branch and that nothing
protects it. Nothing else in Autoform removes a worktree. The user-facing
contract is the ``project worktrees`` section of ``autoform_cli/README.md``.

What protects a worktree is evidence, never inference. Git's own command
refuses only a dirty or locked worktree: it removes one holding a Git lock
file, one containing another worktree, and a detached one whose commit nothing
else reaches, and it never looks at processes or claims, so each is checked
here. A process is identified by its PID and start time, so a reused PID is a
new process, and a process running as the caller that cannot be inspected
protects every worktree, as does a claim board that cannot be read. A lease
names an article, not a worktree, so any live lease written from this host
protects them all. A lock file protects whatever its age.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from ._fs import is_within
from ._repository import _git_environment
from ._strict_json import reject_json_constant, strict_json_object
from .claims import ClaimBoard, ClaimTransportError, _repository_is_remote

WORKTREES_SCHEMA = "autoform-worktrees/v1"
REMOVAL_SCHEMA = "autoform-worktree-removal/v1"

# One Git command's time limit; a status over a large untracked tree can be slow.
_GIT_TIMEOUT_S = 120.0
# The claim board listing's deadline, as `claim list` defaults to.
_CLAIM_TIMEOUT_S = 120.0
# Paths a dirty worktree's evidence names before it says how many more there are.
_DIRTY_PATHS_SHOWN = 5
# Files in a worktree's Git directory that mean an operation stopped partway.
_OPERATION_MARKERS = (
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "sequencer",
)


class WorktreeError(RuntimeError):
    """The worktrees could not be read or the manifest cannot be honored, so there is no correct answer."""


@dataclass(frozen=True, slots=True)
class _Worktree:
    """One record of ``git worktree list --porcelain``."""

    path: Path
    head: str | None
    branch: str | None
    locked: str | None
    prunable: bool

    @property
    def real(self) -> Path:
        return Path(os.path.realpath(self.path))


@dataclass(frozen=True, slots=True)
class _Process:
    pid: int
    started: float
    name: str
    places: tuple[tuple[str, Path], ...]

    def describe(self) -> str:
        return f"pid {self.pid} ({self.name}) started {_timestamp(self.started)}"


@dataclass(frozen=True, slots=True)
class _Repository:
    top: Path
    common_dir: Path
    integration: str | None
    worktrees: tuple[_Worktree, ...]

    @property
    def candidates(self) -> tuple[_Worktree, ...]:
        """Every linked worktree except the one the target is in; the main worktree is listed first."""
        top = os.path.realpath(self.top)
        return tuple(worktree for worktree in self.worktrees[1:] if str(worktree.real) != top)


@dataclass(slots=True)
class WorktreeEntry:
    path: Path
    head: str | None
    branch: str | None
    protected_by: dict[str, str] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)
    logical_bytes: int | None = 0
    freeable_bytes: int | None = 0

    @property
    def removable(self) -> bool:
        return not self.protected_by

    @property
    def recover(self) -> str | None:
        path = shlex.quote(str(self.path))
        if self.branch is not None:
            return f"git worktree add {path} {shlex.quote(self.branch)}"
        if self.head is not None:
            return f"git worktree add --detach {path} {self.head}"
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "freeable_bytes": self.freeable_bytes,
            "head": self.head,
            "ignored": self.ignored,
            "logical_bytes": self.logical_bytes,
            "path": str(self.path),
            "protected_by": dict(sorted(self.protected_by.items())),
            "recover": self.recover,
            "removable": self.removable,
        }


@dataclass(frozen=True, slots=True)
class WorktreeReport:
    repository: Path
    integration: str | None
    entries: tuple[WorktreeEntry, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "integration": self.integration,
            "repository": str(self.repository),
            "schema": WORKTREES_SCHEMA,
            "worktrees": [entry.as_dict() for entry in self.entries],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class RemovedWorktree:
    path: Path
    head: str | None
    branch: str | None
    freed_bytes: int
    recover: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "freed_bytes": self.freed_bytes,
            "head": self.head,
            "path": str(self.path),
            "recover": self.recover,
        }


@dataclass(frozen=True, slots=True)
class RefusedWorktree:
    path: Path
    protected_by: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "protected_by": dict(sorted(self.protected_by.items()))}


@dataclass(frozen=True, slots=True)
class RemovalReport:
    repository: Path
    removed: tuple[RemovedWorktree, ...]
    refused: tuple[RefusedWorktree, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "refused": [entry.as_dict() for entry in self.refused],
            "removed": [entry.as_dict() for entry in self.removed],
            "repository": str(self.repository),
            "schema": REMOVAL_SCHEMA,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def report_worktrees(target: str | os.PathLike[str]) -> WorktreeReport:
    """Report every linked worktree but the target's own, removable or with what protects it."""
    repository = _read_repository(Path(target))
    shared = _claim_protections(repository.top)
    processes, uninspected = _scan_processes()
    _add(shared, "liveness-unknown", _uninspected_evidence(uninspected))
    entries = tuple(
        _evaluate(repository, worktree, processes, shared, sizes=True) for worktree in repository.candidates
    )
    return WorktreeReport(repository.common_dir, repository.integration, entries)


def remove_worktrees(target: str | os.PathLike[str], manifest: str | os.PathLike[str]) -> RemovalReport:
    """Remove each worktree the saved report ``manifest`` marks removable that nothing protects now.

    Each is checked again immediately before its removal, against a fresh
    worktree list and process scan; the claim board is read once, first. One
    whose path, commit or branch differs from the manifest is ``changed``.
    """
    entries = _load_manifest(Path(manifest))
    repository = _read_repository(Path(target))
    if str(repository.common_dir) != entries[0]:
        raise WorktreeError(
            f"the manifest describes the repository {entries[0]}, not {repository.common_dir}"
        )
    shared = _claim_protections(repository.top)
    removed: list[RemovedWorktree] = []
    refused: list[RefusedWorktree] = []
    for wanted in entries[1]:
        path = Path(wanted["path"])
        repository = _read_repository(Path(target))
        current = next(
            (worktree for worktree in repository.candidates if str(worktree.real) == os.path.realpath(path)),
            None,
        )
        if current is None:
            refused.append(RefusedWorktree(path, {"changed": "no longer a linked worktree of this repository"}))
            continue
        if (current.head, current.branch) != (wanted["head"], wanted["branch"]):
            refused.append(
                RefusedWorktree(
                    path,
                    {
                        "changed": f"now {_describe_checkout(current.branch, current.head)}, "
                        f"not {_describe_checkout(wanted['branch'], wanted['head'])}"
                    },
                )
            )
            continue
        processes, uninspected = _scan_processes()
        protections = dict(shared)
        _add(protections, "liveness-unknown", _uninspected_evidence(uninspected))
        entry = _evaluate(repository, current, processes, protections, sizes=False)
        if not entry.removable:
            refused.append(RefusedWorktree(path, entry.protected_by))
            continue
        free_before = _free_bytes(current.path.parent)
        result = _git(repository.top, "worktree", "remove", "--", str(current.path))
        if result.returncode != 0:
            refused.append(RefusedWorktree(path, {"git-refused": _text(result.stderr).strip()}))
            continue
        free_after = _free_bytes(current.path.parent)
        freed = free_after - free_before if free_before is not None and free_after is not None else 0
        removed.append(RemovedWorktree(current.path, current.head, current.branch, freed, entry.recover))
    return RemovalReport(repository.common_dir, tuple(removed), tuple(refused))


def format_report(report: WorktreeReport) -> list[str]:
    lines: list[str] = []
    removable = [entry for entry in report.entries if entry.removable]
    for entry in report.entries:
        checkout = _describe_checkout(entry.branch, entry.head)
        if entry.removable:
            line = f"removable {entry.path} ({checkout}): {_describe_sizes(entry)}"
            if entry.ignored:
                line += f"; deletes ignored {', '.join(entry.ignored)}"
        else:
            reasons = "; ".join(f"{code}: {evidence}" for code, evidence in sorted(entry.protected_by.items()))
            line = f"protected {entry.path} ({checkout}): {reasons}"
        lines.append(line)
    freeable = sum(entry.freeable_bytes or 0 for entry in removable)
    logical = sum(entry.logical_bytes or 0 for entry in removable)
    lines.append(
        f"{len(report.entries)} linked worktree(s): {len(removable)} removable, freeing up to "
        f"{_human_bytes(freeable)} of {_human_bytes(logical)}; {len(report.entries) - len(removable)} protected"
    )
    return lines


def format_removal(report: RemovalReport) -> list[str]:
    lines = [
        f"removed {entry.path} ({_describe_checkout(entry.branch, entry.head)}): free space "
        f"{_signed_bytes(entry.freed_bytes)}; recover with: {entry.recover}"
        for entry in report.removed
    ]
    for entry in report.refused:
        reasons = "; ".join(f"{code}: {evidence}" for code, evidence in sorted(entry.protected_by.items()))
        lines.append(f"refused {entry.path}: {reasons}")
    freed = sum(entry.freed_bytes for entry in report.removed)
    lines.append(f"removed {len(report.removed)}, free space {_signed_bytes(freed)}; refused {len(report.refused)}")
    return lines


def _read_repository(target: Path) -> _Repository:
    start = target if target.is_dir() else target.parent
    if not start.is_dir():
        raise WorktreeError(f"{target} is not a directory in a Git checkout")
    top = _git_line(start, "rev-parse", "--show-toplevel")
    common = _git_line(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if top is None or common is None:
        raise WorktreeError(f"{target} is not in a Git checkout's working tree")
    head = _git(start, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    integration = _text(head.stdout).strip() or None if head.returncode == 0 else None
    listing = _git(start, "worktree", "list", "--porcelain", "-z")
    if listing.returncode != 0:
        raise WorktreeError(f"git worktree list failed: {_text(listing.stderr).strip()}")
    return _Repository(
        Path(top),
        Path(os.path.realpath(common)),
        integration,
        tuple(_parse_worktrees(listing.stdout)),
    )


def _parse_worktrees(output: bytes) -> list[_Worktree]:
    worktrees: list[_Worktree] = []
    record: dict[str, str] = {}
    for item in output.split(b"\0"):
        if not item:
            if "worktree" in record:
                head = record.get("HEAD")
                branch = record.get("branch")
                worktrees.append(
                    _Worktree(
                        path=Path(record["worktree"]),
                        head=head if head and head.strip("0") else None,
                        branch=branch.removeprefix("refs/heads/") if branch else None,
                        locked=record.get("locked"),
                        prunable="prunable" in record,
                    )
                )
            record = {}
            continue
        name, _, value = os.fsdecode(item).partition(" ")
        record[name] = value
    return worktrees


def _evaluate(
    repository: _Repository,
    worktree: _Worktree,
    processes: list[_Process],
    shared: dict[str, str],
    *,
    sizes: bool,
) -> WorktreeEntry:
    entry = WorktreeEntry(worktree.path, worktree.head, worktree.branch)
    protections = entry.protected_by
    if worktree.locked is not None:
        protections["locked"] = worktree.locked or "locked without a reason"
    if worktree.prunable or not worktree.path.is_dir():
        protections["missing"] = "its directory is gone; git worktree prune drops the record"
    else:
        _check_status(worktree, entry)
        _add(protections, "git-operation", _git_operation(repository, worktree))
        nested = [
            other.real
            for other in repository.worktrees
            if other.real != worktree.real and is_within(other.real, worktree.real)
        ]
        if nested:
            protections["contains-worktree"] = ", ".join(str(path) for path in nested)
        _add(protections, "in-use", _users(worktree.real, processes))
        if sizes:
            measured = _sizes(worktree.real, set(nested))
            entry.logical_bytes, entry.freeable_bytes = measured if measured else (None, None)
    _add(protections, "unintegrated", _unintegrated(repository, worktree))
    for code, evidence in shared.items():
        protections[code] = evidence
    return entry


def _check_status(worktree: _Worktree, entry: WorktreeEntry) -> None:
    result = _git(worktree.path, "status", "--porcelain", "-z", "--ignored")
    if result.returncode != 0:
        entry.protected_by["dirty"] = f"git status failed: {_text(result.stderr).strip()}"
        return
    changed: list[str] = []
    untracked: list[str] = []
    fields = iter(result.stdout.split(b"\0"))
    for item in fields:
        if len(item) < 4:
            continue
        status, path = item[:2].decode("ascii", "replace"), os.fsdecode(item[3:])
        if status == "!!":
            entry.ignored.append(path)
        elif status == "??":
            untracked.append(path)
        else:
            changed.append(path)
            if "R" in status or "C" in status:
                next(fields, None)  # a rename or copy names its source next
    if changed or untracked:
        shown = (changed + untracked)[:_DIRTY_PATHS_SHOWN]
        more = len(changed) + len(untracked) - len(shown)
        entry.protected_by["dirty"] = (
            f"{len(changed)} changed, {len(untracked)} untracked: {', '.join(shown)}"
            + (f" and {more} more" if more else "")
        )


def _git_operation(repository: _Repository, worktree: _Worktree) -> str | None:
    git_dir = _git_line(worktree.path, "rev-parse", "--path-format=absolute", "--git-dir")
    if git_dir is None:
        return "its Git directory could not be found"
    found: list[str] = []
    for directory, _, files in os.walk(git_dir):
        found.extend(
            os.path.relpath(os.path.join(directory, name), git_dir) for name in files if name.endswith(".lock")
        )
    found.extend(name for name in _OPERATION_MARKERS if os.path.lexists(os.path.join(git_dir, name)))
    if worktree.branch is not None:
        branch_lock = repository.common_dir / "refs" / "heads" / f"{worktree.branch}.lock"
        if branch_lock.exists():
            found.append(str(branch_lock))
    return ", ".join(sorted(found)) or None


def _unintegrated(repository: _Repository, worktree: _Worktree) -> str | None:
    if worktree.head is None:
        return "its branch has no commit"
    if repository.integration is None:
        return f"{repository.top} has no commit to compare with"
    ancestor = _git(repository.top, "merge-base", "--is-ancestor", worktree.head, repository.integration)
    if ancestor.returncode == 0:
        return None
    if ancestor.returncode == 1:
        cherry = _git(repository.top, "cherry", repository.integration, worktree.head)
        if cherry.returncode == 0:
            missing = sum(1 for line in _text(cherry.stdout).splitlines() if line.startswith("+"))
            if not missing:
                return None  # every commit has a patch-equivalent one in the integration HEAD
            return f"{missing} commit(s) the integration HEAD {repository.integration[:12]} lacks"
        ancestor = cherry
    return f"Git could not compare it with the integration HEAD: {_text(ancestor.stderr).strip()}"


def _users(worktree: Path, processes: list[_Process]) -> str | None:
    users = [
        f"{process.describe()}: {', '.join(kind for kind, place in process.places if is_within(place, worktree))}"
        for process in processes
        if any(is_within(place, worktree) for _, place in process.places)
    ]
    return "; ".join(users) or None


def _scan_processes() -> tuple[list[_Process], list[str]]:
    """Every process running as this user's effective UID, and those that could not be inspected.

    Another user's process is out of reach, so it is skipped rather than
    counted as uncertain; on a system without UIDs every process is inspected.
    """
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    try:
        candidates = list(psutil.process_iter(["pid", "name", "create_time", "uids"]))
    except (psutil.Error, OSError) as error:
        return [], [f"the process table could not be read ({error})"]
    processes: list[_Process] = []
    uninspected: list[str] = []
    for candidate in candidates:
        info = candidate.info
        uids = info.get("uids")
        if euid is not None and uids is not None and uids.effective != euid:
            continue
        name = info.get("name") or "?"
        started = info.get("create_time")
        if (euid is not None and uids is None) or started is None:
            uninspected.append(f"pid {candidate.pid} ({name})")
            continue
        try:
            cwd = candidate.cwd()
            exe = candidate.exe()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.Error, OSError):
            uninspected.append(f"pid {candidate.pid} ({name})")
            continue
        places = tuple(
            (kind, Path(os.path.realpath(place)))
            for kind, place in (("working directory", cwd), ("executable", exe))
            if place
        )
        processes.append(_Process(candidate.pid, started, name, places))
    return processes, uninspected


def _uninspected_evidence(uninspected: list[str]) -> str | None:
    if not uninspected:
        return None
    shown = ", ".join(uninspected[:_DIRTY_PATHS_SHOWN])
    more = len(uninspected) - _DIRTY_PATHS_SHOWN
    return f"could not inspect {shown}" + (f" and {more} more" if more > 0 else "")


def _claim_protections(top: Path) -> dict[str, str]:
    """What the ``origin`` claim board says about every worktree: nothing without an ``origin``."""
    configured = _git(top, "config", "--get", "remote.origin.url")
    url = _text(configured.stdout).strip()
    if configured.returncode == 1 and not url:
        return {}
    if configured.returncode != 0 or not url:
        return {"liveness-unknown": f"the origin remote could not be read: {_text(configured.stderr).strip()}"}
    if not _repository_is_remote(url):
        url = str((top / Path(url).expanduser()).resolve())
    try:
        with tempfile.TemporaryDirectory(prefix="autoform-worktrees-") as temporary:
            board = ClaimBoard(url, "autoform-worktrees", Path(temporary) / "claims.git")
            leases = board.list(timeout=_CLAIM_TIMEOUT_S)
    except (ClaimTransportError, ValueError, OSError) as error:
        return {"liveness-unknown": f"the claim board could not be read: {error}"}
    host = socket.gethostname()
    live = [lease for lease in leases if not lease["_malformed"] and not lease["_expired"]]
    held = [
        f"{lease['_key']} held by {lease['owner']} until {_timestamp(lease['expires_at'])}"
        + (f" (note: {lease['note']})" if isinstance(lease.get("note"), str) and lease["note"] else "")
        for lease in live
        if lease.get("host") == host
    ]
    # A lease without a host could have been written here.
    unplaced = [str(lease["_key"]) for lease in live if not isinstance(lease.get("host"), str)]
    unplaced += [str(lease["_key"]) for lease in leases if lease["_malformed"]]
    protections: dict[str, str] = {}
    if held:
        protections["claimed"] = "; ".join(held)
    if unplaced:
        protections["liveness-unknown"] = f"claim(s) naming no host or unreadable: {', '.join(unplaced)}"
    return protections


def _sizes(root: Path, skip: set[Path]) -> tuple[int, int] | None:
    """Logical bytes, each inode once, and the allocated bytes only this tree links; None when unreadable."""
    inodes: dict[tuple[int, int], list[int]] = {}
    directories = 0
    stack = [root]
    try:
        while stack:
            with os.scandir(stack.pop()) as entries:
                for item in entries:
                    status = item.stat(follow_symlinks=False)
                    if item.is_dir(follow_symlinks=False):
                        if Path(item.path) not in skip:
                            directories += _allocated(status)
                            stack.append(Path(item.path))
                        continue
                    seen = inodes.setdefault((status.st_dev, status.st_ino), [0, status.st_nlink, 0, 0])
                    seen[0] += 1
                    seen[2], seen[3] = status.st_size, _allocated(status)
    except OSError:
        return None
    logical = sum(size for _, _, size, _ in inodes.values())
    freeable = directories + sum(allocated for links, total, _, allocated in inodes.values() if links >= total)
    return logical, freeable


def _allocated(status: os.stat_result) -> int:
    blocks = getattr(status, "st_blocks", None)
    return status.st_size if blocks is None else blocks * 512


def _free_bytes(path: Path) -> int | None:
    for candidate in (path, *path.parents):
        try:
            return shutil.disk_usage(candidate).free
        except OSError:
            continue
    return None


def _load_manifest(path: Path) -> tuple[str, list[dict[str, Any]]]:
    try:
        manifest = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=strict_json_object,
            parse_constant=reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise WorktreeError(f"cannot read the manifest {path}: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != WORKTREES_SCHEMA:
        raise WorktreeError(f"the manifest {path} is not an {WORKTREES_SCHEMA} report")
    repository, worktrees = manifest.get("repository"), manifest.get("worktrees")
    if not isinstance(repository, str) or not isinstance(worktrees, list):
        raise WorktreeError(f"the manifest {path} lacks its repository or worktrees")
    wanted: list[dict[str, Any]] = []
    for entry in worktrees:
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("removable"), bool)
            and all(entry.get(key) is None or isinstance(entry.get(key), str) for key in ("head", "branch"))
        ):
            raise WorktreeError(f"the manifest {path} has a malformed worktree entry")
        if entry["removable"]:
            wanted.append({"path": entry["path"], "head": entry.get("head"), "branch": entry.get("branch")})
    return repository, wanted


def _git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", *arguments],
            cwd=cwd,
            capture_output=True,
            env=_git_environment(),
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorktreeError(f"git {arguments[0]} could not run in {cwd}: {error}") from error


def _git_line(cwd: Path, *arguments: str) -> str | None:
    result = _git(cwd, *arguments)
    output = _text(result.stdout).strip()
    return output if result.returncode == 0 and output else None


def _text(data: bytes) -> str:
    return os.fsdecode(data)


def _add(protections: dict[str, str], code: str, evidence: str | None) -> None:
    if evidence:
        protections[code] = f"{protections[code]}; {evidence}" if code in protections else evidence


def _describe_checkout(branch: str | None, head: str | None) -> str:
    commit = head[:12] if head else "no commit"
    return f"{branch} at {commit}" if branch else f"detached at {commit}"


def _describe_sizes(entry: WorktreeEntry) -> str:
    if entry.freeable_bytes is None or entry.logical_bytes is None:
        return "sizes unreadable"
    return f"frees up to {_human_bytes(entry.freeable_bytes)} of {_human_bytes(entry.logical_bytes)}"


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"


def _signed_bytes(count: int) -> str:
    return f"{'+' if count >= 0 else '-'}{_human_bytes(abs(count))}"


def _timestamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "REMOVAL_SCHEMA",
    "WORKTREES_SCHEMA",
    "RemovalReport",
    "WorktreeError",
    "WorktreeReport",
    "format_removal",
    "format_report",
    "remove_worktrees",
    "report_worktrees",
]
