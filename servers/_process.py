"""Give a Lean child a clean environment, find the processes carrying a token the caller set, and stop and reap its dedicated POSIX process group.

autoform_cli.lean_probe loads this file by path, so it imports only the
standard library and psutil.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import time
from typing import Any

REPL_ABORT_TERM_SECONDS = 0.5
REPL_ABORT_KILL_SECONDS = 1.0


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if error.errno == errno.EPERM:
            return True
        raise RuntimeError("failed to inspect the Lean process group") from error
    return True


def process_group_has_live_members(process_group_id: int) -> bool:
    """Treat an all-zombie process group as terminated after SIGKILL."""
    if not _process_group_exists(process_group_id):
        return False

    import psutil

    for candidate in psutil.process_iter(["pid", "status"]):
        try:
            if os.getpgid(candidate.info["pid"]) != process_group_id:
                continue
            if candidate.info["status"] != psutil.STATUS_ZOMBIE:
                return True
        except (ProcessLookupError, PermissionError, psutil.Error):
            continue
    # Darwin may report EPERM for killpg(pgid, 0) after the final member exits.
    # psutil is the authoritative membership check for this same-user group.
    return False


def _wait_for_live_process_group_exit(process_group_id: int, deadline: float) -> bool:
    while process_group_has_live_members(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def _wait_for_process(process: subprocess.Popen, deadline: float) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return process.poll() is not None
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        return False
    except OSError as error:
        raise RuntimeError("failed to reap the Lean process") from error
    return True


def _kill_subprocesses(
    process: subprocess.Popen,
    process_group_id: int,
    deadline: float | None = None,
) -> None:
    """Terminate and verify one dedicated POSIX process group."""
    if getattr(process, "returncode", None) is not None:
        if process_group_has_live_members(process_group_id):
            raise RuntimeError(
                "refusing to signal a process group after its leader was reaped"
            )
        return
    _terminate_process_group(process_group_id, deadline)
    _reap_process(process, deadline)


def _terminate_process_group(
    process_group_id: int,
    deadline: float | None = None,
) -> None:
    """Terminate a dedicated group without reaping its leader."""
    started = time.monotonic()
    term_deadline = started + REPL_ABORT_TERM_SECONDS
    kill_deadline = term_deadline + REPL_ABORT_KILL_SECONDS
    if deadline is not None:
        term_deadline = min(term_deadline, deadline)
        kill_deadline = min(kill_deadline, deadline)

    if process_group_has_live_members(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise RuntimeError("failed to terminate the Lean process group") from error

    _wait_for_live_process_group_exit(process_group_id, term_deadline)
    if process_group_has_live_members(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise RuntimeError("failed to kill the Lean process group") from error

    group_exited = _wait_for_live_process_group_exit(process_group_id, kill_deadline)
    if not group_exited:
        raise RuntimeError("timed out terminating the Lean process group")


def _reap_process(
    process: subprocess.Popen,
    deadline: float | None = None,
) -> None:
    """Reap one leader only after its process group is known retired."""
    if getattr(process, "returncode", None) is not None:
        return
    kill_deadline = time.monotonic() + REPL_ABORT_KILL_SECONDS
    if deadline is not None:
        kill_deadline = min(kill_deadline, deadline)
    parent_reaped = _wait_for_process(process, kill_deadline)
    if not parent_reaped:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as error:
            raise RuntimeError("failed to kill the Lean process") from error
        parent_reaped = _wait_for_process(process, kill_deadline)
    if not parent_reaped:
        raise RuntimeError("timed out reaping the Lean process")


def inherit_clean_env() -> dict[str, str]:
    """Return a copy of the current environment without PYTHONPATH noise."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def processes_with_token(name: str, token: str) -> list[Any]:
    """Return the processes whose environment sets ``name`` to ``token``.

    A process whose environment cannot be read is left out, so it is never
    signalled: nothing shows that it carries the token. Lean and Lake run as
    the user, who can read their environments; setuid programs the user runs,
    such as ``login``, are unreadable at all times, so counting them would
    fail every close. A process table that cannot be read fails the scan.
    """
    if not token:
        # A missing token would match every process without the variable.
        raise ValueError("a process token must be a nonempty string")
    import psutil

    try:
        pids = psutil.pids()
    except (OSError, psutil.Error) as error:
        raise RuntimeError(f"cannot read the process table to find the processes carrying {name}") from error
    found = []
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            # The handle records the creation time before the environment is
            # read, so it can only name the process that was read or an
            # earlier one, which psutil then refuses to signal.
            candidate = psutil.Process(pid)
            environment = candidate.environ()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # Exited or unreadable; a zombie reads as exited or empty.
            continue
        if environment.get(name) == token:
            found.append(candidate)
    return found
