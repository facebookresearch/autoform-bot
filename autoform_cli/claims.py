"""Host-neutral, fail-closed Git-ref leases for cooperative work claims.

Each claim is stored under ``refs/autoform-claims/`` and points to an orphan
commit whose message is the lease JSON. Mutations use an exact observed object
ID as a compare-and-swap precondition, so concurrent claimants cannot silently
overwrite one another. The user-facing contract is the claim contract in
``autoform_cli/reference/claims-and-revisions.md``.

``claim list`` copies the whole board with one fetch and reads every lease at
once, so its cost hardly grows with the number of claims. Git can read the refs
it advertises one by one, which is why a listing is not a consistent snapshot. A
peer's acquire during a listing changes nothing, except that a server speaking
Git's older stateless HTTP protocol may refuse the fetch, which is then retried,
for three fetches in all. A case-insensitive scratch cannot store two claim
keys, or two folders they name, that differ only in case under their names. Git
refuses such a fetch, except that some versions, such as Apple's 2.50, store one
key's name holding the other key's lease without a word. So on such a scratch a
listing whose fetch wrote any ref also names the board's claims, with one more
command that downloads the board's list of refs, and fetches the claims again by
name when two of them collide. A fetch Git refuses names them too, and fetches
them by name when the names explain the refusal: a key beside a folder that
differs from it only in case, or a name that is not a claim key, such as one
APFS refuses because it is not UTF-8. ``claim list TARGET...`` names the
targets' refs and fetches only those, so changes to other claims never make it
retry and no other claim's lease is copied, and a target released meanwhile has
the targets named again, never the whole board; over a network its cost still
grows with the board's list of refs.

A listing, by ``claim list``, runs Git in its own process group, without a
controlling terminal on Linux and macOS so ``ssh`` cannot prompt on one, and
stops a Git command that runs out of time or is interrupted together with
everything it started; only removing its own refs afterwards runs in the
caller's group. That holds on the main thread, where ``claim list`` lists. Off
it, a listing runs Git in the caller's process group and with its terminal: a
signal sent to that group, such as a terminal's interrupt, reaches Git, but one
sent only to the caller does not, ``ssh`` can prompt on that terminal, and a
command that runs out of time is stopped without what it started. ``acquire``
and ``release`` run Git in the caller's process group, so a terminal's
interrupt reaches Git, which then removes the ref lock it holds.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ._strict_json import reject_json_constant, strict_json_object

CLAIM_REF_PREFIX = "refs/autoform-claims/"
CLAIM_SCHEMA = "autoform-claim/v1"
# 3000-01-01T00:00:00Z, so a new claim stays held until released. Older clients read such a lease as live, and
# a lease they wrote still lapses at its own expires_at.
_HELD_UNTIL = 32503680000
CLAIM_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_SCP_REPOSITORY_RE = re.compile(r"^(?:[^/@:]+@)?(?:\[[^\]]+\]|[^/:]+):.+$")
_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_ROOTED_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SCRATCH_INIT_ATTEMPTS = 10
_SCRATCH_INIT_DELAY_S = 0.05
# `claim list` fetches into refs under this prefix that only that call reads.
_LIST_REF_PREFIX = "refs/autoform-list/"
# Each listing's namespace there names its host and process, so a later listing
# on the host can delete the refs of one a signal stopped.
_LISTING_NAMESPACE_RE = re.compile(r"([0-9a-f]{8})-([0-9]+)-[0-9a-f]{32}")
# What tells a Linux host's processes apart from another's with the same name.
_BOOT_ID = "/proc/sys/kernel/random/boot_id"
_PID_NAMESPACE = "/proc/self/ns/pid"
_LIST_FETCH_ATTEMPTS = 3
# A glob that matches no ref, so a fetch given it as `--negotiation-tip` sends no
# haves; an exact ref name that does not exist would fail the fetch instead.
_NO_NEGOTIATION_TIP = f"{_LIST_REF_PREFIX}none/*"
# How a stateless protocol v0 server refuses a want whose ref moved after it
# advertised it; this failure is worth fetching again.
_MOVED_WANT = "not our ref"
# How a fetch refuses a named ref the board no longer has, which is worth
# fetching again only when the listing named its refs first.
_MISSING_REF = "couldn't find remote ref"
# For a name the board lacks, as a claim released meanwhile, Git fetches
# refs/<name>, refs/tags/<name>, refs/heads/<name>, refs/remotes/<name> or
# refs/remotes/<name>/HEAD instead, such as a branch ending in the claim's
# path. These negative refspecs leave such a name unfetched, and the claim
# left out.
_ONLY_CLAIM_REFS = "^refs/refs/*\n^refs/tags/*\n^refs/heads/*\n^refs/remotes/*\n"
# How Git refuses to store fetched refs: two names, or a name and a folder, that
# differ only in case on a case-insensitive scratch, or a name the filesystem
# refuses, such as one that is not UTF-8 on APFS. The words change with Git's
# version, so the board's names say which it was.
_LOCAL_REFUSAL = (
    "only differ in casing",
    "reference already exists",
    "cannot lock ref",
    "some local refs could not be updated",
)
# The warning a fetch prints for a `--negotiation-tip` that matches no ref yet.
_NEGOTIATION_TIP_WARNING = "warning: ignoring --negotiation-tip="
# A peer's push holds a ref's lock file for milliseconds; one that outlasts
# this grace after a failed push is reported as blocking the claim.
_LOCK_GRACE_S = 2.0
_LOCK_POLL_S = 0.05
# One git command's time limit when its caller sets no deadline.
_GIT_CALL_TIMEOUT_S = 120.0
# A step still running after this long is named to a progress callback.
_PROGRESS_AFTER_S = 5.0
# The longest one wait for a git command may be: poll() takes milliseconds that
# must fit a C int, about 24.8 days.
_LONGEST_WAIT_S = 24 * 86400.0
# How long a stopped git command gets to clean up, such as removing a ref's
# lock file, before it is killed.
_STOP_GRACE_S = 1.0
# How long a killed git command gets to be reaped.
_KILL_GRACE_S = 5.0
# An isolated git command leads its own process group, on POSIX without a
# controlling terminal, so ssh cannot prompt on one and stopping it also ends
# git's transport.
_OWN_PROCESS_GROUP: dict[str, Any] = (
    {"start_new_session": True}
    if os.name == "posix"
    else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
)
# What `git rev-parse --local-env-vars` prints: variables that point Git at
# another repository than the one the working directory names.
_LOCAL_GIT_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_COMMON_DIR",
    }
)
# Those of them that name a repository, its objects or its work tree. A Git
# hook runs with GIT_DIR set to its own repository, so the board's Git drops
# them; configuration variables stay, so `git -c` settings still reach the board.
# A pre-receive hook also runs in the receiving repository's quarantine, in
# which Git refuses every ref update, the scratch's included.
_REPOSITORY_GIT_ENV = (_LOCAL_GIT_ENV - {"GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}) | {
    "GIT_QUARANTINE_PATH"
}

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "autoform",
    "GIT_AUTHOR_EMAIL": "autoform@localhost",
    "GIT_COMMITTER_NAME": "autoform",
    "GIT_COMMITTER_EMAIL": "autoform@localhost",
    # A missing credential fails at once instead of waiting for a prompt.
    "GIT_TERMINAL_PROMPT": "0",
}
# ClaimBoard._git, or a functools.partial of it that carries a deadline.
_Git = Callable[..., "subprocess.CompletedProcess[Any]"]
_CAS_REJECTIONS = (
    "stale info",
    "fetch first",
    "remote ref updated since checkout",
    "cannot lock ref",
    # Git 2.53, unlike 2.50, words a lost race to update or delete a ref with
    # only one of these.
    "incorrect old value provided",
    "reference does not exist",
)
# Git 2.53 words a lost race to create a ref with only this, and also any push
# a ref's lock file blocks, so where lock files are not visible it counts as a
# lost race only for a create.
_LOST_CREATE = "reference already exists"
# How Git reports a lock file, such as packed-refs.lock, that it could not take.
_LOCK_FILE_EXISTS_RE = re.compile(r"Unable to create '([^']*)\.lock': File exists")
# When the remote aborts an atomic push, every ref reports "atomic transaction
# failed"; only this error line names the ref whose expected value was gone.
_LOCK_FAILURE_RE = re.compile(r"cannot lock ref '([^']+)'")


class ClaimTransportError(RuntimeError):
    """A claim board operation could not be completed or verified."""


class MalformedLeaseError(ClaimTransportError):
    """A claim ref exists, but its lease cannot be verified safely."""


class ClaimTimeoutError(ClaimTransportError):
    """The caller's deadline passed before a step finished."""

    def __init__(self, step: str) -> None:
        super().__init__(f"ran out of time while {step}")
        self.step = step


class ClaimContentionError(ClaimTransportError):
    """The board refused every attempt to fetch claims because claim refs kept moving."""


@dataclass(frozen=True, slots=True)
class ClaimRefLock:
    """A Git lock file in a local claim board.

    ``ref`` is the claim ref it guards, ``packed-refs``, which blocks every
    deletion, or ``reftable``, which blocks every update of a reftable board.
    """

    path: Path
    ref: str
    modified: float

    def describe(self) -> str:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.modified))
        if self.ref == "reftable":
            blocked = "every claim update"
        elif self.ref == "packed-refs":
            blocked = "releasing claims"
        else:
            blocked = f"updates of {_readable(self.ref)}"
        return f"Git lock file {_readable(os.fsencode(self.path))} (modified {stamp}) blocks {blocked}"


class ClaimRefLockedError(ClaimTransportError):
    """A Git lock file on a local claim board blocks a claim update."""

    def __init__(self, message: str, locks: Sequence[ClaimRefLock]) -> None:
        super().__init__(message)
        self.locks = tuple(locks)


@dataclass(frozen=True, slots=True)
class ClaimBatchResult:
    """The outcome of an all-or-nothing multi-key claim operation.

    It is true exactly when the operation succeeded for every key. Otherwise no
    key changed, ``reason`` says why, and ``blocking`` names the keys
    responsible when known.
    """

    ok: bool
    blocking: tuple[str, ...] = ()
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not CLAIM_KEY_RE.fullmatch(key) or ".." in key:
        raise ValueError(f"invalid claim key {key!r}")
    parts = key.split("/")
    if any(part.startswith(".") or part.endswith(".") or part.lower().endswith(".lock") for part in parts):
        raise ValueError(f"invalid claim key {key!r}")
    return key


def _validate_keys(keys: Sequence[str]) -> tuple[str, ...]:
    if isinstance(keys, str):
        raise TypeError("claim keys must be a sequence of keys, not one string")
    batch = tuple(_validate_key(key) for key in keys)
    if not batch:
        raise ValueError("at least one claim key is required")
    seen: set[str] = set()
    for key in batch:
        if key in seen:
            raise ValueError(f"duplicate claim key {key!r}")
        seen.add(key)
    return batch


def _claims_by_key(refs: Mapping[str, str], namespace: str) -> dict[str, str]:
    """Return the object ID of every ref under ``namespace`` whose rest is a valid claim key, by key."""
    claims: dict[str, str] = {}
    for ref, oid in refs.items():
        # ls-remote patterns also match longer names ending in the same path.
        if not ref.startswith(namespace):
            continue
        key = ref[len(namespace) :]
        try:
            _validate_key(key)
        except ValueError:
            continue
        claims[key] = oid
    return claims


def _left_out(refs: Mapping[str, str], namespace: str, claims: Mapping[str, str]) -> int:
    """How many refs under ``namespace`` are not among ``claims``, since their names are not claim keys."""
    return sum(ref.startswith(namespace) for ref in refs) - len(claims)


def _lines(output: str) -> list[str]:
    """Git's output split at newlines only: a ref name can hold U+2028, at which ``splitlines`` splits too."""
    return output.split("\n")


def _this_host() -> str | None:
    """Name this host's processes with 8 hex digits of a SHA-256, as a listing's namespace does, or return None.

    Containers in one pod share a host name but not their processes, so on Linux
    the boot ID and the PID namespace count too, and if ``/proc`` cannot give
    one of them, the host has no name. Elsewhere the host name alone names it,
    so two machines with one name must not share a scratch.
    """
    identity = socket.gethostname()
    if sys.platform.startswith("linux"):
        try:
            identity += "\0" + Path(_BOOT_ID).read_text().strip()
            identity += "\0" + os.readlink(_PID_NAMESPACE)
        except OSError:
            return None
    return hashlib.sha256(os.fsencode(identity)).hexdigest()[:8]


def _process_is_gone(pid: int) -> bool:
    """Whether no process has ``pid``; POSIX only, since ``os.kill`` ends the process on Windows."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
        # Another user's process, or a number no process has.
        pass
    return False


def _collide_ignoring_case(keys: Sequence[str]) -> bool:
    """Whether two of ``keys``, or two folders they name, differ only in case.

    Either pair is one name on a case-insensitive filesystem, so a scratch
    there cannot store both keys' refs under their names.
    """
    paths = set()
    for key in keys:
        parts = key.split("/")
        paths.update("/".join(parts[:end]) for end in range(1, len(parts) + 1))
    return len({path.lower() for path in paths}) < len(paths)


def _fetch_failure(stderr: str) -> str:
    """Why a fetch failed, without the warning every listing's fetch prints."""
    lines = (line for line in stderr.splitlines() if not line.startswith(_NEGOTIATION_TIP_WARNING))
    return _readable("\n".join(lines).strip()[:300])


def _readable(output: str | bytes) -> str:
    """Git's ``output`` as text a message can hold, each byte that is not UTF-8 written as ``\\xff``."""
    if isinstance(output, str):
        output = output.encode("utf-8", "surrogateescape")
    return output.decode("utf-8", "backslashreplace")


def _rejected_keys(detail: str, keys_by_ref: Mapping[str, str], markers: Sequence[str]) -> tuple[str, ...]:
    """Return the keys a refused push reports as stale or contended, in push order."""
    refs = set(_LOCK_FAILURE_RE.findall(detail))
    for line in detail.splitlines():
        # Porcelain status lines read "<flag>\t<from>:<to>\t<summary>".
        flag, _, rest = line.partition("\t")
        refspec, _, summary = rest.partition("\t")
        if flag == "!" and any(marker in summary.lower() for marker in markers):
            refs.add(refspec.rpartition(":")[2])
    return tuple(key for ref, key in keys_by_ref.items() if ref in refs)


def _refusal(blocked: Mapping[str, str]) -> ClaimBatchResult:
    """Refuse a batch before pushing, naming each blocking key in batch order and every reason."""
    return ClaimBatchResult(False, tuple(blocked), " or ".join(dict.fromkeys(blocked.values())))


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _validate_timeout(timeout: float) -> float:
    if not _is_finite_number(timeout) or timeout <= 0:
        raise ValueError("claim list timeout must be a finite positive number")
    return timeout


def author_claim_key(node_id: str) -> str:
    """Return a readable, ref-safe, collision-resistant author claim key."""
    if not isinstance(node_id, str):
        raise TypeError("node_id must be a string")
    slug = re.sub(r"[^a-z0-9-]+", "-", node_id.lower()).strip("-")[:48] or "node"
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:16]
    return f"author/{slug}-{digest}"


def _repository_is_remote(value: str, *, windows: bool | None = None) -> bool:
    """Match Git's URL/SCP forms without misclassifying Windows paths."""

    on_windows = os.name == "nt" if windows is None else windows
    windows_path = (
        value.startswith("\\")
        or _WINDOWS_ROOTED_DRIVE_RE.match(value) is not None
        or (on_windows and _WINDOWS_DRIVE_PREFIX_RE.match(value) is not None)
    )
    return not windows_path and (
        "://" in value or _SCP_REPOSITORY_RE.fullmatch(value) is not None
    )


class _Terminated(BaseException):
    """SIGTERM, SIGHUP or SIGQUIT arrived while a claim git command was running."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@contextlib.contextmanager
def _git_ends_with_its_caller():
    """Let SIGTERM, SIGHUP and SIGQUIT end a running isolated claim git command along with its caller.

    The command leads its own session, so a signal sent to the caller's process
    group no longer reaches it. In the main thread, such a signal is raised as
    an exception, which ``ClaimBoard._git`` answers by stopping the command's
    group; the signal is then delivered again to the handler it replaced. A
    caller whose handler returns gets a ``ClaimTransportError``.
    """
    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: dict[int, Any] = {}

    def terminate(signum: int, frame: object) -> None:
        raise _Terminated(signum)

    for name in ("SIGTERM", "SIGHUP", "SIGQUIT"):
        signum = getattr(signal, name)
        handler = signal.getsignal(signum)
        if handler in (signal.SIG_IGN, None):
            continue
        previous[signum] = handler
        signal.signal(signum, terminate)
    try:
        yield
    except _Terminated as exc:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        previous.clear()
        signal.raise_signal(exc.signum)
        name = signal.Signals(exc.signum).name
        raise ClaimTransportError(f"claim git command interrupted by {name}") from None
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _communicate(
    process: subprocess.Popen[Any],
    data: str | bytes | None,
    limit: float,
    step: str,
    progress: Callable[[str], None] | None,
) -> tuple[Any, Any]:
    """Wait up to ``limit`` seconds for ``process``, naming a slow ``step`` to ``progress`` once."""
    limit = min(limit, _LONGEST_WAIT_S)
    if progress is None or limit <= _PROGRESS_AFTER_S:
        return process.communicate(data, timeout=limit)
    if data is not None:
        # A communicate call after one that timed out takes no input, and before
        # Python 3.13 it sends none of what is left, so a thread sends it all.
        threading.Thread(target=_send, args=(process.stdin, data), daemon=True).start()
        process.stdin = None
    try:
        return process.communicate(timeout=_PROGRESS_AFTER_S)
    except subprocess.TimeoutExpired:
        progress(step)
    return process.communicate(timeout=limit - _PROGRESS_AFTER_S)


def _send(pipe: Any, data: str | bytes) -> None:
    """Write ``data`` to a command's input and close it, as communicate does; one that stops early ends it."""
    with contextlib.suppress(OSError, ValueError), pipe:
        pipe.write(data)


def _stop(process: subprocess.Popen[Any], *, group: bool) -> None:
    """Stop a git command and reap it: on POSIX, SIGTERM, a grace to clean up, then SIGKILL.

    With ``group``, the command leads its own process group and the signals
    reach everything it started. On Windows the command is killed at once, and
    with ``group`` taskkill ends what it started first.
    """
    if os.name != "posix":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if group:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                    timeout=_KILL_GRACE_S,
                )
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=_KILL_GRACE_S)
        return
    for signum, grace in ((signal.SIGTERM, _STOP_GRACE_S), (signal.SIGKILL, _KILL_GRACE_S)):
        with contextlib.suppress(OSError):
            if group:
                # git is not reaped yet, so its pid still names its process group.
                os.killpg(process.pid, signum)
            else:
                process.send_signal(signum)
        try:
            process.communicate(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


class ClaimBoard:
    """Lease operations against a Git repository via a local bare object store."""

    def __init__(self, repo_url: str | os.PathLike[str], worker_id: str, scratch: str | os.PathLike[str]):
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        raw_repo_url = os.fspath(repo_url)
        if not _repository_is_remote(raw_repo_url):
            try:
                raw_repo_url = str(Path(raw_repo_url).expanduser().resolve())
            except RuntimeError as exc:
                # Such as ~user naming no user, or a symbolic link loop.
                raise ValueError(f"cannot resolve the claim board path {raw_repo_url!r}: {exc}") from None
        self.repo_url = raw_repo_url
        self.worker_id = worker_id
        self.scratch = Path(scratch)

    def _git(
        self,
        args: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
        deadline: float | None = None,
        step: str = "running git",
        progress: Callable[[str], None] | None = None,
        isolated: bool = False,
    ) -> subprocess.CompletedProcess[Any]:
        """Run git in the scratch board; with ``input_bytes``, input and output are bytes.

        A command gets 120 seconds or, with ``deadline`` (a
        ``time.monotonic()`` value), until then, which raises
        ``ClaimTimeoutError`` naming ``step``; running out stops it.
        ``progress`` hears ``step`` once if the command is still running after
        five seconds.

        An ``isolated`` command leads its own process group without a
        controlling terminal, so stopping it ends everything it started.
        Otherwise it shares the caller's process group, as a push must: a
        terminal's interrupt then reaches git, which removes its ref locks.
        Off the main thread an isolated command shares the caller's process
        group too, since nothing there could stop it when the caller is
        signalled.
        """
        if deadline is None:
            limit = _GIT_CALL_TIMEOUT_S
        else:
            limit = deadline - time.monotonic()
            if limit <= 0:
                raise ClaimTimeoutError(step)
        data = input_text if input_bytes is None else input_bytes
        isolated = isolated and threading.current_thread() is threading.main_thread()
        guard = _git_ends_with_its_caller() if isolated else contextlib.nullcontext()
        with guard:
            return self._run_git(args, data, check, input_bytes is None, limit, step, progress, deadline, isolated)

    def _run_git(
        self,
        args: list[str],
        data: str | bytes | None,
        check: bool,
        text: bool,
        limit: float,
        step: str,
        progress: Callable[[str], None] | None,
        deadline: float | None,
        isolated: bool,
    ) -> subprocess.CompletedProcess[Any]:
        env = {name: value for name, value in os.environ.items() if name not in _REPOSITORY_GIT_ENV}
        try:
            process = subprocess.Popen(
                ["git", *args],
                cwd=self.scratch,
                stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # A ref name need not be UTF-8; such a name reads as an invalid claim key.
                encoding="utf-8" if text else None,
                errors="surrogateescape" if text else None,
                env={**env, **_GIT_ENV},
                **(_OWN_PROCESS_GROUP if isolated else {}),
            )
        except OSError as exc:
            raise ClaimTransportError(f"git claim-board operation failed: {exc}") from exc
        try:
            stdout, stderr = _communicate(process, data, limit, step, progress)
        except subprocess.TimeoutExpired as exc:
            _stop(process, group=isolated)
            if deadline is not None:
                raise ClaimTimeoutError(step) from exc
            raise ClaimTransportError(f"git claim-board operation failed: {exc}") from exc
        except BaseException:
            _stop(process, group=isolated)
            raise
        proc = subprocess.CompletedProcess(["git", *args], process.returncode, stdout, stderr)
        if check and proc.returncode != 0:
            detail = _readable(proc.stderr or proc.stdout).strip()[:300]
            command = _readable(" ".join(args[:2]))
            raise ClaimTransportError(f"git {command} failed against claim board: {detail}")
        return proc

    def _ensure_scratch(self, git: _Git | None = None) -> None:
        git = self._git if git is None else git
        last_error: BaseException | None = None
        for attempt in range(_SCRATCH_INIT_ATTEMPTS):
            try:
                if self.scratch.is_dir():
                    ready = git(
                        ["rev-parse", "--is-bare-repository"],
                        check=False,
                        step="preparing the scratch",
                    )
                    if ready.returncode == 0 and ready.stdout.strip() == "true":
                        return
                self.scratch.mkdir(parents=True, exist_ok=True)
            except ClaimTimeoutError:
                raise
            except (OSError, ClaimTransportError) as exc:
                last_error = exc
                break
            try:
                git(["init", "--bare", "--quiet", "."], step="preparing the scratch")
                return
            except ClaimTimeoutError:
                raise
            except ClaimTransportError as exc:
                last_error = exc
            if attempt + 1 < _SCRATCH_INIT_ATTEMPTS:
                time.sleep(_SCRATCH_INIT_DELAY_S)
        raise ClaimTransportError(
            "cannot prepare the local claim-board scratch directory"
        ) from last_error

    @staticmethod
    def _ref(key: str) -> str:
        return CLAIM_REF_PREFIX + _validate_key(key)

    def _remote_oid(self, key: str) -> str | None:
        ref = self._ref(key)
        proc = self._git(["ls-remote", self.repo_url, ref])
        for line in _lines(proc.stdout):
            oid, _, name = line.partition("\t")
            if name == ref:
                return oid
        return None

    def _remote_oids(self, keys: Sequence[str], git: _Git | None = None) -> dict[str, str]:
        """Read every present key's object ID with one ``ls-remote``."""
        git = self._git if git is None else git
        keys_by_ref = {self._ref(key): key for key in keys}
        proc = git(["ls-remote", self.repo_url, *keys_by_ref], step="naming the claim refs")
        oids: dict[str, str] = {}
        for line in _lines(proc.stdout):
            oid, separator, ref = line.partition("\t")
            # ls-remote patterns also match longer names ending in the same
            # path, such as refs/heads/<ref>; only the exact ref is the claim.
            if separator and ref in keys_by_ref:
                oids[keys_by_ref[ref]] = oid
        return oids

    def _read_lease(self, key: str, oid: str) -> dict[str, Any]:
        ref = self._ref(key)
        if self._git(["cat-file", "-e", f"{oid}^{{commit}}"], check=False).returncode != 0:
            # Offer the board no local refs: the scratch can keep every claim ref
            # an older `list` mirrored and the private refs of a killed `list`,
            # and negotiating from them saves nothing for one lease.
            self._git(
                [
                    "fetch",
                    "--quiet",
                    f"--negotiation-tip={_NO_NEGOTIATION_TIP}",
                    self.repo_url,
                    f"+{ref}:{ref}",
                ]
            )
        proc = self._git(["cat-file", "commit", oid], check=False)
        if proc.returncode != 0:
            raise MalformedLeaseError(f"claim {key!r} does not point to a readable commit")
        return self._parse_lease(key, proc.stdout)

    def _parse_lease(self, key: str, commit: str) -> dict[str, Any]:
        """Return the verified lease in the text of ``key``'s commit, or raise ``MalformedLeaseError``."""
        _, separator, message = commit.partition("\n\n")
        if not separator:
            raise MalformedLeaseError(f"claim {key!r} has no lease message")
        try:
            # A lease commit is UTF-8, as a listing reads it; git's output reads
            # any other byte as a surrogate.
            commit.encode()
            lease = json.loads(
                message,
                object_pairs_hook=strict_json_object,
                parse_constant=reject_json_constant,
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise MalformedLeaseError(f"claim {key!r} has invalid lease JSON") from exc
        if not isinstance(lease, dict) or not self._lease_is_valid(lease, key):
            raise MalformedLeaseError(f"claim {key!r} has an invalid lease schema")
        return lease

    @staticmethod
    def _lease_is_valid(lease: Mapping[str, Any], key: str | None = None) -> bool:
        acquired_at = lease.get("acquired_at")
        expires_at = lease.get("expires_at")
        valid = (
            lease.get("schema") == CLAIM_SCHEMA
            and isinstance(lease.get("owner"), str)
            and bool(lease.get("owner"))
            and isinstance(lease.get("resource"), str)
            and _is_finite_number(acquired_at)
            and _is_finite_number(expires_at)
            and acquired_at <= expires_at
        )
        return bool(valid and (key is None or lease.get("resource") == key))

    def _held_by_peer(self, key: str, old: str | None) -> bool:
        """Return whether ``old`` is another worker's verified live lease, which blocks acquiring."""
        if old is None:
            return False
        lease = self._read_lease(key, old)
        return bool(
            lease is not None
            and self._lease_is_valid(lease, key)
            and lease.get("owner") != self.worker_id
            and not self.expired(lease)
        )

    def _owned_lease(self, key: str, old: str | None) -> dict[str, Any] | None:
        """Return this worker's verified lease at ``old``, or ``None`` if absent or not owned."""
        if old is None:
            return None
        lease = self._read_lease(key, old)
        if lease is None or not self._lease_is_valid(lease, key) or lease.get("owner") != self.worker_id:
            return None
        return lease

    def _make_lease_commit(self, key: str, note: str = "", target: str = "") -> str:
        key = _validate_key(key)
        now = time.time()
        if not math.isfinite(now):
            raise ValueError("claim timestamp must be finite")
        if now >= _HELD_UNTIL:
            # Such a lease would be born expired, or malformed to every reader.
            raise ValueError("the clock reads 3000-01-01 or later, the expiry every new claim records")
        lease: dict[str, Any] = {
            "schema": CLAIM_SCHEMA,
            "owner": self.worker_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": now,
            "expires_at": _HELD_UNTIL,
            "resource": key,
        }
        if note:
            lease["note"] = note
        if target:
            # The string the key was hashed from, so `claim list` shows what `claim release` takes.
            lease["target"] = target
        tree = self._git(["mktree"], input_text="").stdout.strip()
        message = json.dumps(lease, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._git(["commit-tree", tree, "-m", message]).stdout.strip()

    def _ref_store(self) -> Path | None:
        """Return the Git directory holding the board's refs, or None when its files are not visible.

        A local path, or a ``file://`` URL except on Windows, is visible, found
        as Git's local transport finds it; any other transport is not.
        """
        location = self.repo_url
        if location.startswith("file://"):
            if os.name == "nt":
                # Git for Windows reads drive letters and hosts in a file URL
                # its own way; such a board is treated as not visible.
                return None
            # Git decodes the whole URL, then ignores the host and keeps "?" and "#". A byte that is not UTF-8
            # was read as a surrogate, which turns back into that byte.
            raw = location[len("file://") :].encode("utf-8", "surrogateescape")
            decoded = os.fsdecode(urllib.parse.unquote_to_bytes(raw))
            path_start = decoded.find("/")
            if path_start < 0:
                return None
            location = decoded[path_start:]
        elif _repository_is_remote(location):
            return None
        while len(location) > 1 and location.endswith("/"):
            location = location[:-1]
        env = {name: value for name, value in os.environ.items() if name not in _LOCAL_GIT_ENV}
        # Git's enter_repo tries these candidates in this order. It takes the
        # first that is a file or a Git directory, and fails if a file is bad.
        for candidate in (f"{location}/.git", location, f"{location}.git/.git", f"{location}.git"):
            if not os.path.isfile(candidate) and not os.path.isdir(candidate):
                continue
            try:
                proc = subprocess.run(
                    ["git", f"--git-dir={candidate}", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    capture_output=True,
                    timeout=120,
                    env=env,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if proc.returncode == 0:
                return Path(os.fsdecode(proc.stdout.removesuffix(b"\n")))
            if os.path.isfile(candidate):
                return None
        return None

    def ref_locks(self) -> list[ClaimRefLock]:
        """Return the Git lock files that can block the board's claims, once they outlast the grace.

        These are the lock files under the claim refs, ``packed-refs.lock`` and
        a reftable board's ``tables.list.lock``. Git leaves one behind when a
        process stops while updating a ref, and refuses the updates it guards
        until someone deletes it. A younger lock may belong to a push still in
        progress. Git records no owner that could prove a lock abandoned, so
        locks are reported, never deleted. Only a board given as a local path,
        or as a ``file://`` URL except on Windows, has visible files.
        """
        store = self._ref_store()
        if store is None:
            return []
        paths = {store / "packed-refs.lock": "packed-refs", store / "reftable" / "tables.list.lock": "reftable"}
        for directory, _, files in os.walk(store / CLAIM_REF_PREFIX):
            for name in files:
                if name.endswith(".lock"):
                    path = Path(directory) / name
                    paths[path] = path.relative_to(store).as_posix()[: -len(".lock")]
        now = time.time()
        # A modification time ahead of the clock is no sign of a push in progress.
        return [lock for lock in self._existing_locks(paths) if abs(now - lock.modified) >= _LOCK_GRACE_S]

    @staticmethod
    def _existing_locks(paths: Mapping[Path, str]) -> list[ClaimRefLock]:
        locks: list[ClaimRefLock] = []
        for path, ref in paths.items():
            try:
                locks.append(ClaimRefLock(path, ref, path.lstat().st_mtime))
            except OSError:
                continue
        return sorted(locks, key=lambda lock: str(lock.path))

    def _blocking_locks(self, store: Path, updates: Sequence[tuple[str, str | None, str]]) -> list[ClaimRefLock]:
        """Return the lock files that still block a failed push's refs after the grace.

        ``packed-refs.lock`` blocks only deletions; a reftable board's
        ``tables.list.lock`` blocks every update.
        """
        paths = {store / f"{self._ref(key)}.lock": self._ref(key) for key, _, _ in updates}
        if any(not new for _, _, new in updates):
            paths[store / "packed-refs.lock"] = "packed-refs"
        paths[store / "reftable" / "tables.list.lock"] = "reftable"
        deadline = time.monotonic() + _LOCK_GRACE_S
        while True:
            locks = self._existing_locks(paths)
            if not locks or time.monotonic() >= deadline:
                return locks
            time.sleep(_LOCK_POLL_S)

    def _cas_push(self, key: str, old: str | None, new: str) -> bool:
        return self._cas_push_refs([(key, old, new)]).ok

    def _cas_push_refs(
        self,
        updates: Sequence[tuple[str, str | None, str]],
        *,
        atomic: bool = False,
    ) -> ClaimBatchResult:
        """Push ``(key, old, new)`` updates, each leased on its observed ``old``; empty ``new`` deletes.

        With ``atomic`` the remote applies every update or none. A lost
        compare-and-swap is a failed result; anything else raises, including
        ``ClaimRefLockedError`` for a local board's lock file that outlasts the
        grace, which Git's message alone cannot tell apart from a lost race. A
        lock that clears within the grace is a lost race.
        """
        keys_by_ref = {self._ref(key): key for key, _, _ in updates}
        proc = self._git(
            [
                "push",
                "--quiet",
                "--porcelain",
                *(["--atomic"] if atomic else []),
                *(f"--force-with-lease={self._ref(key)}:{old or ''}" for key, old, _ in updates),
                self.repo_url,
                *(f"{new if new else ''}:{self._ref(key)}" for key, _, new in updates),
            ],
            check=False,
        )
        if proc.returncode == 0:
            return ClaimBatchResult(True)
        store = self._ref_store()
        locks = self._blocking_locks(store, updates) if store is not None else []
        if locks:
            raise ClaimRefLockedError(
                f"{'; '.join(lock.describe() for lock in locks)}. Git leaves a lock file behind when a process "
                "stops while updating a ref; if no Git process is using the claim board, "
                f"delete {'them' if len(locks) > 1 else 'it'} and retry",
                locks,
            )
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        if atomic and "does not support --atomic" in detail:
            # Falling back to separate pushes could leave a partial claim set.
            raise ClaimTransportError("claim board does not support atomic pushes; refusing a multi-key update")
        markers = _CAS_REJECTIONS
        # Read only in Git's own refusal lines, never in hook output. With no
        # lock left on a visible board, a lock Git met belonged to a push that
        # has since let go.
        if store is not None or all(old is None for _, old, _ in updates):
            markers += (_LOST_CREATE,)
        stale = _rejected_keys(detail, keys_by_ref, markers)
        if (
            stale
            or any(marker in detail.lower() for marker in _CAS_REJECTIONS)
            or (store is not None and self._names_a_needed_lock(detail, updates))
        ):
            return ClaimBatchResult(False, stale, "changed concurrently" if stale else "a claim changed concurrently")
        raise ClaimTransportError(f"claim CAS push failed: {_readable(detail[:300])}")

    def _names_a_needed_lock(self, detail: str, updates: Sequence[tuple[str, str | None, str]]) -> bool:
        """Whether Git could not take a pushed ref's lock, or ``packed-refs.lock`` for a deletion."""
        needed = [f"/{self._ref(key)}" for key, _, _ in updates]
        if any(not new for _, _, new in updates):
            needed.append("/packed-refs")
        return any(path.endswith(tuple(needed)) for path in _LOCK_FILE_EXISTS_RE.findall(detail))

    def read(self, key: str) -> dict[str, Any] | None:
        """Return the current parsed lease, including an expired lease, or ``None``."""
        self._ensure_scratch()
        oid = self._remote_oid(key)
        return self._read_lease(key, oid) if oid else None

    @classmethod
    def expired(cls, lease: Mapping[str, Any], now: float | None = None) -> bool:
        """Return whether a lease is malformed or no longer live."""
        expires_at = lease.get("expires_at")
        if not _is_finite_number(expires_at):
            return True
        comparison_time = time.time() if now is None else now
        if not _is_finite_number(comparison_time):
            raise ValueError("claim expiry comparison clock must be finite")
        return expires_at <= comparison_time

    def acquire(self, key: str, note: str = "", target: str = "") -> bool:
        """CAS-acquire a free, expired, or owned lease.

        A malformed lease raises ``MalformedLeaseError``.
        """
        key = _validate_key(key)
        self._ensure_scratch()
        old = self._remote_oid(key)
        if self._held_by_peer(key, old):
            return False
        new = self._make_lease_commit(key, note, target)
        return self._cas_push(key, old, new)

    def release(self, key: str) -> bool:
        """CAS-delete this worker's lease; refuse foreign or unverifiable ownership."""
        key = _validate_key(key)
        self._ensure_scratch()
        old = self._remote_oid(key)
        if old is None:
            return True
        if self._owned_lease(key, old) is None:
            return False
        return self._cas_push(key, old, "")

    # The *_many methods read every key with one ls-remote, apply the per-key
    # checks of the single-key method, and change all keys in one atomic push,
    # so a batch never leaves some of its claims taken and others not. Where
    # the single-key method raises MalformedLeaseError, a batch names that key
    # as blocking instead, so one refusal can explain every blocked key.

    def acquire_many(
        self,
        keys: Sequence[str],
        *,
        note: str = "",
        targets: Mapping[str, str] | None = None,
    ) -> ClaimBatchResult:
        """Acquire every key or none; a live lease of another worker on any key blocks all. *targets* maps a key
        to the string it was hashed from, which its lease records."""
        keys = _validate_keys(keys)
        self._ensure_scratch()
        olds = self._remote_oids(keys)
        blocked: dict[str, str] = {}
        for key in keys:
            try:
                if self._held_by_peer(key, olds.get(key)):
                    blocked[key] = "held by another worker"
            except MalformedLeaseError:
                blocked[key] = "malformed lease"
        if blocked:
            return _refusal(blocked)
        targets = targets or {}
        updates = [(key, olds.get(key), self._make_lease_commit(key, note, targets.get(key, ""))) for key in keys]
        return self._cas_push_refs(updates, atomic=True)

    def release_many(self, keys: Sequence[str]) -> ClaimBatchResult:
        """Delete every present key or none; absent keys count as released, as in :meth:`release`."""
        keys = _validate_keys(keys)
        self._ensure_scratch()
        olds = self._remote_oids(keys)
        present = [key for key in keys if key in olds]
        blocked: dict[str, str] = {}
        for key in present:
            try:
                if self._owned_lease(key, olds[key]) is None:
                    blocked[key] = "not held by this worker"
            except MalformedLeaseError:
                blocked[key] = "malformed lease"
        if blocked:
            return _refusal(blocked)
        if not present:
            return ClaimBatchResult(True)
        return self._cas_push_refs([(key, olds[key], "") for key in present], atomic=True)

    def list(
        self,
        keys: Sequence[str] | None = None,
        *,
        timeout: float | None = None,
        progress: Callable[[str], None] | None = None,
        warn: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Return all claim refs, or those of ``keys``, including malformed and expired entries.

        A fetch copies every claim ref and its commit into refs private to
        this call, so each listed lease is the one its ref named when the board
        answered, other processes sharing the scratch cannot change it, and the
        git commands it takes do not grow with the number of claims. A ref that
        changes meanwhile is listed as it was; every update checks ownership
        again. The listing is not a proven consistent snapshot: the board may
        advertise its refs one by one, so a peer's multi-claim push made
        meanwhile can show some of its claims changed and others not.

        With ``keys``, only their refs are fetched and read, so changes to other
        claims never make the listing retry; a key without a claim ref is left
        out.

        With ``timeout``, the listing's git commands share that many seconds,
        and running out raises ``ClaimTimeoutError`` naming the step it was on;
        deleting the private refs afterwards has its own time limit.
        ``progress`` hears the name of each step still running after five
        seconds. A claim ref whose name is not a claim key is left out, and
        ``warn`` hears how many a listing left out, if any.

        Except on Windows, a listing first deletes the private refs that a
        listing of this version on this host left when a signal stopped it,
        unless a lock file that a killed Git command left in the scratch blocks
        that. On Linux, this host also means this boot and PID namespace, and a
        host whose ``/proc`` cannot give them deletes none; elsewhere it means
        this host name, so two machines with one name must not share a scratch.
        An older version's refs stay, as do those of a listing ``/proc`` could
        not name.
        """
        targets = None if keys is None else _validate_keys(keys)
        deadline = None if timeout is None else time.monotonic() + _validate_timeout(timeout)
        git = functools.partial(self._git, deadline=deadline, progress=progress, isolated=True)
        self._ensure_scratch(git)
        self._prune_stopped_listings(git)
        written: dict[str, dict[str, str] | None] = {}
        try:
            listed, left_out = self._fetch_claims(written, git, targets)
            leases = self._read_listed(listed, git)
        finally:
            # Deleting the private refs gets its own time limit, even after the deadline.
            for namespace, refs in written.items():
                self._delete_refs(namespace, refs)
        if left_out and warn is not None:
            warn(f"left out {left_out} claim ref(s) whose name is not a valid claim key")
        return leases

    def _prune_stopped_listings(self, git: _Git) -> None:
        """Delete the private refs of this host's listings whose process is gone.

        A namespace from another host, from an older version, or whose process
        ID a new process took stays, and so does every namespace on Windows or
        on a Linux host that :func:`_this_host` cannot name. Git deletes a batch
        of refs whole or not at all, and refuses one that deletes a ref beside
        its own lock file, as a stopped update leaves, so such a ref stays out of
        the batch; looking for its lock file runs no Git. Git deletes no ref
        while ``packed-refs.lock`` is held, so the prune then runs no Git at all.
        A batch Git refuses anyway leaves every ref for a later listing.
        """
        if os.name != "posix":
            return
        host = _this_host()
        if host is None or os.path.lexists(self.scratch / "packed-refs.lock"):
            return
        found = git(
            ["for-each-ref", "--format=%(refname)", _LIST_REF_PREFIX],
            check=False,
            step="finding refs that stopped listings left",
        )
        stale = []
        for ref in _lines(found.stdout) if found.returncode == 0 else ():
            owner = _LISTING_NAMESPACE_RE.fullmatch(ref.removeprefix(_LIST_REF_PREFIX).partition("/")[0])
            if owner and owner[1] == host and _process_is_gone(int(owner[2])):
                stale.append(ref)
        stale = [ref for ref in stale if not os.path.lexists(self.scratch / f"{ref}.lock")]
        if stale:
            git(
                ["update-ref", "--stdin"],
                check=False,
                input_text="".join(f"delete {ref}\n" for ref in stale),
                step="deleting refs that stopped listings left",
            )

    def _fetch_claims(
        self,
        written: dict[str, dict[str, str] | None],
        git: _Git,
        targets: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, str], int]:
        """Fetch every claim ref, or those of ``targets``, into refs private to this call.

        Return each claim's object ID, by key, and how many claim refs were
        left out because their names are not claim keys.

        Each fetch's namespace goes into ``written`` before it runs, with the refs
        it wrote once they are read. A private ref is named after its claim key,
        which a case-insensitive scratch cannot do for two keys, or two folders
        they name, that differ only in case; the board's keys are then fetched
        again by name into numbered refs. Git refuses such a fetch, except that
        some versions, such as Apple's 2.50, store one key's name holding the
        other key's lease without a word, so a case-insensitive scratch names
        the board's keys after a fetch that wrote any ref, and fetches them by
        name when two collide, or when the board has a name that is not a key
        and the scratch kept fewer refs than the board has names or reads back a
        key the board does not have, as when ``k`` and U+212A KELVIN SIGN fold
        together, or a folder ``K`` takes in ``k/x``. A fetch Git refuses names
        them too, and they are fetched by name when they explain the refusal: a key
        beside a folder that differs from it only in case, or a name that is
        not a claim key, such as one APFS refuses as not UTF-8.
        With ``targets``, only the present targets are fetched by name, and a
        target released meanwhile is left out or has the targets named again,
        never the board.
        A board that refuses every attempt because claim refs moved raises
        ``ClaimContentionError``.
        """
        keys: list[str] | None = None
        left_out = 0
        if targets is None:

            def name() -> list[str]:
                nonlocal left_out
                named, left_out = self._remote_keys(git)
                return named
        else:

            def name() -> list[str]:
                return list(self._remote_oids(targets, git))

            keys = name()
        failures = 0
        while True:
            if keys is not None and not keys:
                return {}, left_out
            host = _this_host()
            # Without a host name, the form older versions used, which no listing prunes.
            owner = f"{host}-{os.getpid()}-" if host else ""
            namespace = f"{_LIST_REF_PREFIX}{owner}{uuid.uuid4().hex}/"
            written[namespace] = None
            # Negotiate only from this fetch's refs, which do not exist yet: the
            # scratch's other refs save nothing here, and under protocol v0 each
            # round spent offering them is a window in which a peer's acquire fails it.
            args = ["fetch", "--quiet", "--no-tags", f"--negotiation-tip={namespace}*"]
            if keys is None:
                fetch = git(
                    [*args, self.repo_url, f"+{CLAIM_REF_PREFIX}*:{namespace}*"],
                    check=False,
                    step="fetching the claim board",
                )
            else:
                refspecs = "".join(f"+{CLAIM_REF_PREFIX}{key}:{namespace}{index}\n" for index, key in enumerate(keys))
                refspecs += _ONLY_CLAIM_REFS
                fetch = git(
                    [*args, "--stdin", self.repo_url],
                    check=False,
                    input_text=refspecs,
                    step=f"fetching {len(keys)} claim refs",
                )
            if fetch.returncode == 0:
                refs = written[namespace] = self._refs_in(namespace, git)
                if keys is None:
                    if refs and self._scratch_ignores_case():
                        named = name()
                        # Keys are ASCII, so only a name that is not one folds silently:
                        # the scratch then keeps fewer refs, or reads back a key the board lacks.
                        folded = left_out and (
                            len(refs) < len(named) + left_out
                            or not _claims_by_key(refs, namespace).keys() <= set(named)
                        )
                        if _collide_ignoring_case(named) or folded:
                            keys = named
                            continue
                    claims = _claims_by_key(refs, namespace)
                    return claims, _left_out(refs, namespace, claims)
                numbered = ((key, refs.get(f"{namespace}{index}")) for index, key in enumerate(keys))
                return {key: oid for key, oid in numbered if oid is not None}, left_out
            if keys is None and any(marker in fetch.stderr for marker in _LOCAL_REFUSAL):
                named = name()
                if left_out or _collide_ignoring_case(named):
                    keys = named
                    continue
            failures += 1
            # A claim released after `ls-remote` named it is no longer there to fetch.
            retryable = _MOVED_WANT in fetch.stderr or (keys is not None and _MISSING_REF in fetch.stderr)
            if not retryable:
                raise ClaimTransportError(f"cannot fetch the claim board: {_fetch_failure(fetch.stderr)}")
            if failures == _LIST_FETCH_ATTEMPTS:
                raise ClaimContentionError(
                    f"the claim board refused {failures} fetches because claim refs kept moving: "
                    f"{_fetch_failure(fetch.stderr)}"
                )
            if keys is not None:
                keys = name()

    def _scratch_ignores_case(self) -> bool:
        """Whether the scratch's filesystem takes two names that differ only in case for one.

        ``head`` names the scratch's ``HEAD`` exactly when it does. ``lexists``
        does not follow ``HEAD``, which Git can write as a symbolic link.
        """
        return os.path.lexists(self.scratch / "head")

    def _remote_keys(self, git: _Git) -> tuple[list[str], int]:
        """Return every validly named claim key the board advertises, and how many claim refs it leaves out."""
        proc = git(["ls-remote", "--refs", self.repo_url, f"{CLAIM_REF_PREFIX}*"], step="naming the claim refs")
        refs = {line.partition("\t")[2]: "" for line in _lines(proc.stdout)}
        claims = _claims_by_key(refs, CLAIM_REF_PREFIX)
        return list(claims), _left_out(refs, CLAIM_REF_PREFIX, claims)

    def _refs_in(self, namespace: str, git: _Git) -> dict[str, str]:
        """Return the object ID of every ref under ``namespace``, by ref name."""
        proc = git(
            ["for-each-ref", "--format=%(objectname)%09%(refname)", namespace],
            step="reading the fetched refs",
        )
        refs: dict[str, str] = {}
        for line in _lines(proc.stdout):
            oid, separator, ref = line.partition("\t")
            if separator and ref.startswith(namespace):
                refs[ref] = oid
        return refs

    def _delete_refs(self, namespace: str, refs: Mapping[str, str] | None) -> None:
        """Delete a listing's private refs, reading them again if the listing never did.

        A ref left behind only keeps its objects in the scratch, so a failure
        here never replaces the listing's own result or error.
        """
        try:
            if refs is None:
                refs = self._refs_in(namespace, self._git)
            if refs:
                self._git(["update-ref", "--stdin"], input_text="".join(f"delete {ref}\n" for ref in refs))
        except ClaimTransportError:
            pass

    def _read_commits(self, oids: Sequence[str], git: _Git) -> dict[str, bytes | None]:
        """Read every object with one ``cat-file --batch``: its raw commit, or None if missing or not a commit.

        A tag is peeled to its commit, as ``cat-file commit`` in :meth:`_read_lease` does.
        """
        if not oids:
            return {}
        requests = "".join(f"{oid}^{{commit}}\n" for oid in oids).encode()
        output = git(["cat-file", "--batch"], input_bytes=requests, step=f"reading {len(oids)} leases").stdout
        commits: dict[str, bytes | None] = {}
        position = 0
        for oid in oids:
            end = output.index(b"\n", position)
            header = output[position:end].split(b" ")
            position = end + 1
            if len(header) != 3:
                commits[oid] = None
                continue
            size = int(header[2])
            commits[oid] = output[position : position + size] if header[1] == b"commit" else None
            position += size + 1
        return commits

    def _read_listed(self, listed: Mapping[str, str], git: _Git) -> list[dict[str, Any]]:
        commits = self._read_commits(list(dict.fromkeys(listed.values())), git)
        leases: list[dict[str, Any]] = []
        for key, oid in listed.items():
            try:
                commit = commits[oid]
                if commit is None:
                    raise MalformedLeaseError(f"claim {key!r} does not point to a readable commit")
                try:
                    text = commit.decode()
                except UnicodeDecodeError as exc:
                    raise MalformedLeaseError(f"claim {key!r} has invalid lease JSON") from exc
                lease = dict(self._parse_lease(key, text))
            except MalformedLeaseError as exc:
                lease = {
                    "resource": key,
                    "schema": "unreadable",
                    "_error": str(exc),
                    "_malformed": True,
                }
            else:
                lease["_malformed"] = False
            lease["_key"] = key
            lease["_oid"] = oid
            lease["_expired"] = not lease["_malformed"] and self.expired(lease)
            leases.append(lease)
        return sorted(leases, key=lambda lease: str(lease["_key"]))


__all__ = [
    "CLAIM_KEY_RE",
    "CLAIM_REF_PREFIX",
    "CLAIM_SCHEMA",
    "ClaimBatchResult",
    "ClaimBoard",
    "ClaimContentionError",
    "ClaimRefLock",
    "ClaimRefLockedError",
    "ClaimTimeoutError",
    "ClaimTransportError",
    "MalformedLeaseError",
    "author_claim_key",
]
