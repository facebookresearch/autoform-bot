"""Report a repository's linked worktrees, and remove the ones a reviewed report lists.

``project worktrees`` reads every linked worktree of the repository holding its
target, except the main worktree and the target's own, and lists what protects
each; one nothing protects is removable. It changes nothing: Git runs with
``--no-optional-locks``, and the claim board is read through a temporary
scratch. ``--remove`` takes a saved report and removes only the worktrees it
lists as removable, each through ``git worktree remove`` without ``--force``,
after checking again that the integration HEAD, its commit and its branch are
unchanged and that nothing protects it. Nothing else in Autoform removes a
worktree. The user-facing contract is the ``project worktrees`` section of
``autoform_cli/README.md``.

What protects a worktree is evidence, never inference. Git's own command
refuses only a dirty or locked worktree: it removes one holding a Git lock
file, one containing another worktree, and a detached one whose reflog alone
reaches its commits, and it never looks at processes or claims, so each is
checked here. A process is identified by its PID and start time, so a reused
PID is a new process. An agent between commands has no process inside its
worktree, so a worktree whose index, HEAD reflog or ``.lake`` changed within a
lease's lifetime is protected too. A process running as the caller that cannot
be inspected protects every worktree created before it started, since it may
have started inside one. A lease names an article, not a worktree, so any live
lease written from this host protects them all, and a board that cannot be
read protects them all. A lock file protects whatever its age.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from ._fs import is_within
from ._repository import _git_environment
from ._strict_json import reject_json_constant, strict_json_object
from .claims import CLAIM_TTL_S, ClaimBoard, ClaimTransportError, _repository_is_remote

WORKTREES_SCHEMA = "autoform-project-worktrees/v1"

# One Git command's time limit; a status over a large untracked tree can be slow.
_GIT_TIMEOUT_S = 120.0
# The claim board listing's deadline, as `claim list` defaults to.
_CLAIM_TIMEOUT_S = 120.0
# A worktree touched within a lease's lifetime may belong to an agent between commands.
_RECENT_S = CLAIM_TTL_S
# Paths or processes one piece of evidence names before it says how many more there are.
_SHOWN = 5
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
    """One record of ``git worktree list --porcelain``, with its directory in the common Git dir."""

    path: Path
    head: str | None
    branch: str | None
    locked: str | None
    admin: Path | None

    @property
    def real(self) -> Path:
        return Path(os.path.realpath(self.path))

    @property
    def created(self) -> float | None:
        """The earliest time its Git directory records: creation, or earlier than it, never later."""
        if self.admin is None:
            return None
        times: list[float] = []
        for path, attribute in ((self.admin, "st_birthtime"), (self.admin / "commondir", "st_mtime")):
            try:
                value = getattr(os.stat(path), attribute, None)
            except OSError:
                continue
            if value:
                times.append(value)
        return min(times, default=None)


@dataclass(frozen=True, slots=True)
class _Process:
    pid: int
    started: float | None
    name: str
    places: tuple[tuple[str, Path], ...] = ()

    def describe(self) -> str:
        started = "at an unknown time" if self.started is None else _timestamp(self.started)
        return f"pid {self.pid} ({self.name}) started {started}"


@dataclass(frozen=True, slots=True)
class _Machine:
    """What one process scan and claim board read say about every worktree."""

    processes: tuple[_Process, ...]
    uninspected: tuple[_Process, ...]
    board: str | None
    claims: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _Repository:
    top: Path
    common_dir: Path
    integration: str | None
    worktrees: tuple[_Worktree, ...]

    def is_candidate(self, worktree: _Worktree) -> bool:
        """Every linked worktree is one except the one the target is in; the main worktree comes first."""
        return worktree in self.worktrees[1:] and str(worktree.real) != os.path.realpath(self.top)

    @property
    def candidates(self) -> tuple[_Worktree, ...]:
        return tuple(worktree for worktree in self.worktrees if self.is_candidate(worktree))


@dataclass(slots=True)
class WorktreeEntry:
    path: Path
    head: str | None
    branch: str | None
    protected_by: list[tuple[str, str]] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    logical_bytes: int | None = None
    freeable_bytes: int | None = None
    removed: bool = False
    freed_bytes: int | None = None

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

    def protect(self, code: str, detail: str | None) -> None:
        """Record ``code`` with its evidence; a second piece of evidence for one code joins the first."""
        if not detail:
            return
        for index, (known, earlier) in enumerate(self.protected_by):
            if known == code:
                self.protected_by[index] = (code, f"{earlier}; {detail}")
                return
        self.protected_by.append((code, detail))

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "freeable_bytes": self.freeable_bytes,
            "freed_bytes": self.freed_bytes,
            "head": self.head,
            "ignored": self.ignored,
            "logical_bytes": self.logical_bytes,
            "path": str(self.path),
            "protected_by": [{"code": code, "detail": detail} for code, detail in sorted(self.protected_by)],
            "recover": self.recover,
            "removed": self.removed,
        }


@dataclass(frozen=True, slots=True)
class WorktreeReport:
    repository: Path
    integration: str | None
    board: str | None
    entries: tuple[WorktreeEntry, ...]
    #: For a removal, the real paths of the worktrees the manifest asked it to remove.
    requested: frozenset[str] = frozenset()

    @property
    def refused(self) -> tuple[WorktreeEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if os.path.realpath(entry.path) in self.requested and not entry.removed
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "board": self.board,
            "integration": self.integration,
            "repository": str(self.repository),
            "schema": WORKTREES_SCHEMA,
            "worktrees": [entry.as_dict() for entry in self.entries],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def report_worktrees(target: str | os.PathLike[str] = ".", *, repo: str | None = None) -> WorktreeReport:
    """Report every linked worktree but the target's own, with what protects each.

    ``repo`` is the claim board, by default the ``origin`` remote; with neither,
    leases are not checked and the report's ``board`` is None.
    """
    repository = _read_repository(Path(target))
    machine = _read_machine(repository, repo)
    entries = tuple(_evaluate(repository, worktree, machine, sizes=True) for worktree in repository.candidates)
    return WorktreeReport(repository.common_dir, repository.integration, machine.board, entries)


def remove_worktrees(
    target: str | os.PathLike[str], manifest: str | os.PathLike[str], *, repo: str | None = None
) -> WorktreeReport:
    """Remove each worktree the saved report ``manifest`` lists as removable that nothing protects now.

    Each is checked again immediately before its removal, against a fresh
    worktree list and process scan; the claim board is read once, first. The
    result is the report as rechecked, without sizes, where a removed entry has
    ``removed`` and its measured ``freed_bytes``. An entry whose integration
    HEAD, path, commit or branch differs from the manifest is ``changed``.
    """
    header, wanted = _load_manifest(Path(manifest))
    repository = _read_repository(Path(target))
    if header["repository"] != str(repository.common_dir):
        raise WorktreeError(
            f"the manifest describes the repository {header['repository']}, not {repository.common_dir}"
        )
    machine = _read_machine(repository, repo)
    done: dict[str, WorktreeEntry] = {}
    for item in wanted:
        path = Path(item["path"])
        repository = _read_repository(Path(target))
        current = next((tree for tree in repository.worktrees if str(tree.real) == os.path.realpath(path)), None)
        entry = WorktreeEntry(path, item["head"], item["branch"])
        if current is None:
            entry.protect("changed", "no longer a linked worktree of this repository")
        elif current == repository.worktrees[0]:
            entry.protect("changed", "the main worktree")
        elif not repository.is_candidate(current):
            entry.protect("changed", "the worktree the target is in")
        elif header["integration"] != repository.integration:
            entry.protect(
                "changed",
                f"the integration HEAD is now {_short(repository.integration)}, not {_short(header['integration'])}",
            )
        elif (current.head, current.branch) != (item["head"], item["branch"]):
            now = _describe_checkout(current.branch, current.head)
            entry.protect("changed", f"now {now}, not {_describe_checkout(item['branch'], item['head'])}")
        else:
            entry = _evaluate(repository, current, _rescan(machine), sizes=False)
            if entry.removable:
                _remove(repository, current, entry)
        done[os.path.realpath(path)] = entry
    done_paths = set(done)
    repository = _read_repository(Path(target))
    rescanned = _rescan(machine)
    entries = [
        done.pop(str(worktree.real), None) or _evaluate(repository, worktree, rescanned, sizes=False)
        for worktree in repository.candidates
    ]
    entries.extend(done.values())  # removed, or no longer a candidate
    return WorktreeReport(
        repository.common_dir,
        repository.integration,
        machine.board,
        tuple(sorted(entries, key=lambda entry: str(entry.path))),
        frozenset(done_paths),
    )


def format_worktrees(report: WorktreeReport) -> list[str]:
    lines: list[str] = []
    removable = [entry for entry in report.entries if entry.removable]
    for entry in report.entries:
        checkout = _describe_checkout(entry.branch, entry.head)
        if entry.removable:
            line = f"removable {entry.path} ({checkout}): {_describe_sizes(entry)}"
            if entry.ignored:
                line += f"; deletes ignored {', '.join(entry.ignored)}"
        else:
            line = f"protected {entry.path} ({checkout}): {_reasons(entry)}"
        lines.append(line)
    freeable = sum(entry.freeable_bytes or 0 for entry in removable)
    logical = sum(entry.logical_bytes or 0 for entry in removable)
    lines.append(
        f"{len(report.entries)} linked worktree(s): {len(removable)} removable, freeing up to "
        f"{_human_bytes(freeable)} of {_human_bytes(logical)}; {len(report.entries) - len(removable)} protected"
    )
    if report.board is None:
        lines.append("claims: not checked, since there is no origin remote and no --repo")
    return lines


def format_removal(report: WorktreeReport) -> list[str]:
    lines: list[str] = []
    removed = [entry for entry in report.entries if entry.removed]
    for entry in removed:
        lines.append(
            f"removed {entry.path} ({_describe_checkout(entry.branch, entry.head)}): free space "
            f"{_signed_bytes(entry.freed_bytes or 0)}; recover with: {entry.recover}"
        )
    for entry in report.refused:
        lines.append(f"refused {entry.path} ({_describe_checkout(entry.branch, entry.head)}): {_reasons(entry)}")
    freed = sum(entry.freed_bytes or 0 for entry in removed)
    lines.append(f"removed {len(removed)}, free space {_signed_bytes(freed)}; refused {len(report.refused)}")
    if report.board is None:
        lines.append("claims: not checked, since there is no origin remote and no --repo")
    return lines


def _remove(repository: _Repository, worktree: _Worktree, entry: WorktreeEntry) -> None:
    free_before = _free_bytes(worktree.path.parent)
    result = _git(repository.top, "worktree", "remove", "--", str(worktree.path))
    if result.returncode != 0:
        entry.protect("git-refused", _text(result.stderr).strip() or f"exit status {result.returncode}")
        return
    free_after = _free_bytes(worktree.path.parent)
    entry.removed = True
    entry.freed_bytes = free_after - free_before if free_before is not None and free_after is not None else None


def _read_repository(target: Path) -> _Repository:
    start = target if target.is_dir() else target.parent
    if not start.is_dir():
        raise WorktreeError(f"{target} is not a directory in a Git checkout")
    top = _git_line(start, "rev-parse", "--show-toplevel")
    common = _git_line(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if top is None or common is None:
        raise WorktreeError(f"{target} is not in a Git checkout's working tree")
    head = _git(start, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    integration = (_text(head.stdout).strip() or None) if head.returncode == 0 else None
    listing = _git(start, "worktree", "list", "--porcelain", "-z")
    if listing.returncode != 0:
        raise WorktreeError(f"git worktree list failed: {_text(listing.stderr).strip()}")
    common_dir = Path(os.path.realpath(common))
    return _Repository(Path(top), common_dir, integration, tuple(_parse_worktrees(listing.stdout, common_dir)))


def _parse_worktrees(output: bytes, common_dir: Path) -> list[_Worktree]:
    admins = _admin_directories(common_dir)
    worktrees: list[_Worktree] = []
    record: dict[str, str] = {}
    for item in output.split(b"\0"):
        if not item:
            if "worktree" in record:
                path = Path(record["worktree"])
                head = record.get("HEAD")
                branch = record.get("branch")
                worktrees.append(
                    _Worktree(
                        path=path,
                        head=head if head and head.strip("0") else None,
                        branch=branch.removeprefix("refs/heads/") if branch else None,
                        locked=record.get("locked"),
                        admin=admins.get(os.path.realpath(path)),
                    )
                )
            record = {}
            continue
        name, _, value = os.fsdecode(item).partition(" ")
        record[name] = value
    return worktrees


def _admin_directories(common_dir: Path) -> dict[str, Path]:
    """Each linked worktree's real path, mapped to its directory under ``worktrees/``, from its ``gitdir``."""
    admins: dict[str, Path] = {}
    try:
        directories = list(os.scandir(common_dir / "worktrees"))
    except OSError:
        return admins
    for directory in directories:
        try:
            pointer = (Path(directory.path) / "gitdir").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        dot_git = Path(directory.path) / pointer  # Git may write it relative to this directory
        admins[os.path.realpath(dot_git.parent)] = Path(directory.path)
    return admins


def _read_machine(repository: _Repository, repo: str | None) -> _Machine:
    board, claims = _read_claims(repository.top, repo)
    processes, uninspected = _scan_processes()
    return _Machine(processes, uninspected, board, claims)


def _rescan(machine: _Machine) -> _Machine:
    processes, uninspected = _scan_processes()
    return _Machine(processes, uninspected, machine.board, machine.claims)


def _evaluate(repository: _Repository, worktree: _Worktree, machine: _Machine, *, sizes: bool) -> WorktreeEntry:
    entry = WorktreeEntry(worktree.path, worktree.head, worktree.branch)
    if worktree.locked is not None:
        entry.protect("locked", worktree.locked or "locked without a reason")
    present = worktree.path.is_dir()
    if present:
        _check_status(worktree, entry)
        nested = [
            str(other.real)
            for other in repository.worktrees
            if other.real != worktree.real and is_within(other.real, worktree.real)
        ]
        entry.protect("contains-worktree", ", ".join(nested))
        entry.protect("in-use", _users(worktree.real, machine.processes))
    entry.protect("git-operation", _git_operation(repository, worktree))
    entry.protect("unintegrated", _unintegrated(repository, worktree))
    entry.protect("recent", _recent(worktree))
    entry.protect("liveness-unknown", _uninspected(worktree, machine.uninspected))
    for code, detail in machine.claims:
        entry.protect(code, detail)
    if sizes and entry.removable:
        # Walking a Lake build tree is slow, so only what removal would free is measured.
        entry.logical_bytes, entry.freeable_bytes = (_sizes(worktree.real) or (None, None)) if present else (0, 0)
    return entry


def _check_status(worktree: _Worktree, entry: WorktreeEntry) -> None:
    result = _git(worktree.path, "status", "--porcelain", "-z", "--ignored")
    if result.returncode != 0:
        entry.protect("dirty", f"git status failed: {_text(result.stderr).strip() or result.returncode}")
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
        entry.protect("dirty", f"{len(changed)} changed, {len(untracked)} untracked: {_some(changed + untracked)}")


def _git_operation(repository: _Repository, worktree: _Worktree) -> str | None:
    if worktree.admin is None:
        return "its Git directory could not be found"
    found: list[str] = []
    for directory, _, files in os.walk(worktree.admin):
        found.extend(
            os.path.relpath(os.path.join(directory, name), worktree.admin) for name in files if name.endswith(".lock")
        )
    found.extend(name for name in _OPERATION_MARKERS if os.path.lexists(worktree.admin / name))
    if worktree.branch is not None:
        branch_lock = repository.common_dir / "refs" / "heads" / f"{worktree.branch}.lock"
        if branch_lock.exists():
            found.append(str(branch_lock))
    return ", ".join(sorted(found)) or None


def _unintegrated(repository: _Repository, worktree: _Worktree) -> str | None:
    """Whether removal could lose a commit: the integration HEAD lacks one, or only this reflog reaches one.

    A commit counts as integrated when the integration HEAD reaches it or, for
    a commit that is not a merge, holds one with the same patch ID.
    """
    if worktree.head is None:
        return "its branch has no commit"
    if repository.integration is None:
        return f"{repository.top} has no commit to compare with"
    if worktree.branch is None:
        orphaned = _reflog_only_commits(repository, worktree)
        if orphaned:
            return orphaned
    ancestor = _git(repository.top, "merge-base", "--is-ancestor", worktree.head, repository.integration)
    if ancestor.returncode == 0:
        return None
    if ancestor.returncode != 1:
        return f"Git could not compare it with the integration HEAD: {_text(ancestor.stderr).strip()}"
    span = f"{repository.integration}..{worktree.head}"
    merges = _git(repository.top, "rev-list", "--count", "--merges", span)
    cherry = _git(repository.top, "cherry", repository.integration, worktree.head)
    if merges.returncode != 0 or cherry.returncode != 0:
        failed = merges if merges.returncode != 0 else cherry
        return f"Git could not compare it with the integration HEAD: {_text(failed.stderr).strip()}"
    # `git cherry` leaves merges out, so they are counted apart.
    missing = sum(1 for line in _text(cherry.stdout).splitlines() if line.startswith("+"))
    missing += int(_text(merges.stdout).strip() or 0)
    if not missing:
        return None  # every commit is a non-merge with a patch-equivalent one in the integration HEAD
    return f"{missing} commit(s) the integration HEAD {_short(repository.integration)} lacks"


def _reflog_only_commits(repository: _Repository, worktree: _Worktree) -> str | None:
    """Commits a detached worktree's HEAD reflog names that no ref reaches; removal deletes that reflog."""
    if worktree.admin is None:
        return None
    try:
        lines = (worktree.admin / "logs" / "HEAD").read_bytes().splitlines()
    except OSError:
        return None
    named = {os.fsdecode(part) for line in lines for part in line.split(b" ")[:2]}
    named = {oid for oid in named if oid.strip("0")}
    if not named:
        return None
    tips = _git(repository.top, "for-each-ref", "--format=%(objectname)")
    if tips.returncode != 0:
        return f"its reflog could not be checked: {_text(tips.stderr).strip()}"
    stdin = "".join(f"{oid}\n" for oid in sorted(named)) + "".join(
        f"^{tip}\n" for tip in sorted(set(_text(tips.stdout).split()))
    )
    unreached = _git(repository.top, "rev-list", "--ignore-missing", "--stdin", stdin=stdin.encode("ascii"))
    if unreached.returncode != 0:
        return f"its reflog could not be checked: {_text(unreached.stderr).strip()}"
    count = len(_text(unreached.stdout).split())
    return f"its HEAD reflog alone reaches {count} commit(s)" if count else None


def _recent(worktree: _Worktree) -> str | None:
    now = time.time()
    places = [(".lake", worktree.path / ".lake")]
    if worktree.admin is not None:
        places += [("index", worktree.admin / "index"), ("HEAD reflog", worktree.admin / "logs" / "HEAD")]
    changed = []
    for name, path in places:
        try:
            age = now - os.stat(path).st_mtime
        except OSError:
            continue
        if age < _RECENT_S:
            changed.append(f"its {name} changed {max(int(age // 60), 0)} min ago")
    return "; ".join(changed) or None


def _users(worktree: Path, processes: tuple[_Process, ...]) -> str | None:
    users = []
    for process in processes:
        kinds = [kind for kind, place in process.places if is_within(place, worktree)]
        if kinds:
            users.append(f"{process.describe()}: {', '.join(kinds)}")
    return "; ".join(users) or None


def _uninspected(worktree: _Worktree, uninspected: tuple[_Process, ...]) -> str | None:
    """The uninspectable processes started after the worktree was created, which may have started in it."""
    created = worktree.created
    blocking = [
        process
        for process in uninspected
        if process.started is None or created is None or created < process.started
    ]
    if not blocking:
        return None
    return f"could not inspect {_some([process.describe() for process in blocking])}"


def _scan_processes() -> tuple[tuple[_Process, ...], tuple[_Process, ...]]:
    """Every process running as this user's effective UID, and those that could not be inspected.

    Another user's process is out of reach, so it is skipped rather than
    counted as uncertain; on a system without UIDs every process is inspected.
    A process table that cannot be read counts as one uninspectable process
    with no start time.
    """
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    try:
        candidates = list(psutil.process_iter(["pid", "name", "create_time", "uids"]))
    except (psutil.Error, OSError) as error:
        return (), (_Process(0, None, f"the process table: {error}"),)
    processes: list[_Process] = []
    uninspected: list[_Process] = []
    for candidate in candidates:
        info = candidate.info
        uids = info.get("uids")
        if euid is not None and uids is not None and uids.effective != euid:
            continue
        process = _Process(candidate.pid, info.get("create_time"), info.get("name") or "?")
        if (euid is not None and uids is None) or process.started is None:
            uninspected.append(process)
            continue
        try:
            cwd = candidate.cwd()
            exe = candidate.exe()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.Error, OSError):
            uninspected.append(process)
            continue
        places = tuple(
            (kind, Path(os.path.realpath(place)))
            for kind, place in (("working directory", cwd), ("executable", exe))
            if place
        )
        processes.append(_Process(process.pid, process.started, process.name, places))
    return tuple(processes), tuple(uninspected)


def _read_claims(top: Path, repo: str | None) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    """The board read, or None without one, and the protections its leases give every worktree."""
    if repo is None:
        configured = _git(top, "config", "--get", "remote.origin.url")
        url = _text(configured.stdout).strip()
        if configured.returncode == 1 and not url:
            return None, ()
        if configured.returncode != 0 or not url:
            return "origin", (("liveness-unknown", f"the origin remote could not be read: {configured.returncode}"),)
        if not _repository_is_remote(url):
            url = str((top / Path(url).expanduser()).resolve())
        repo = url
    try:
        with tempfile.TemporaryDirectory(prefix="autoform-worktrees-") as temporary:
            board = ClaimBoard(repo, "autoform-worktrees", Path(temporary) / "claims.git")
            leases = board.list(timeout=_CLAIM_TIMEOUT_S)
    except (ClaimTransportError, ValueError, OSError) as error:
        return repo, (("liveness-unknown", f"the claim board could not be read: {error}"),)
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
    protections: list[tuple[str, str]] = []
    if held:
        protections.append(("claimed", "; ".join(held)))
    if unplaced:
        protections.append(("liveness-unknown", f"claim(s) naming no host or unreadable: {', '.join(unplaced)}"))
    return board.repo_url, tuple(protections)


def _sizes(root: Path) -> tuple[int, int] | None:
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


def _load_manifest(path: Path) -> tuple[dict[str, str | None], list[dict[str, str | None]]]:
    """The manifest's repository and integration HEAD, and the entries it lists as removable."""
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
    repository, integration, worktrees = (manifest.get(key) for key in ("repository", "integration", "worktrees"))
    if not isinstance(repository, str) or not _optional_text(integration) or not isinstance(worktrees, list):
        raise WorktreeError(f"the manifest {path} lacks its repository, integration or worktrees")
    wanted: list[dict[str, str | None]] = []
    for entry in worktrees:
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("protected_by"), list)
            and _optional_text(entry.get("head"))
            and _optional_text(entry.get("branch"))
        ):
            raise WorktreeError(f"the manifest {path} has a malformed worktree entry")
        if not entry["protected_by"] and not entry.get("removed"):
            wanted.append({"path": entry["path"], "head": entry.get("head"), "branch": entry.get("branch")})
    return {"repository": repository, "integration": integration}, wanted


def _optional_text(value: object) -> bool:
    return value is None or isinstance(value, str)


def _git(cwd: Path, *arguments: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", *arguments],
            cwd=cwd,
            input=stdin,
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


def _some(items: list[str]) -> str:
    more = len(items) - _SHOWN
    return ", ".join(items[:_SHOWN]) + (f" and {more} more" if more > 0 else "")


def _reasons(entry: WorktreeEntry) -> str:
    return "; ".join(f"{code}: {detail}" for code, detail in sorted(entry.protected_by))


def _short(oid: str | None) -> str:
    return oid[:12] if oid else "no commit"


def _describe_checkout(branch: str | None, head: str | None) -> str:
    return f"{branch} at {_short(head)}" if branch else f"detached at {_short(head)}"


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
    "WORKTREES_SCHEMA",
    "WorktreeError",
    "WorktreeReport",
    "format_removal",
    "format_worktrees",
    "remove_worktrees",
    "report_worktrees",
]
