"""Run Lean probes in a built Lake project.

No other ``autoform_cli`` module starts Lake or Lean. This one imports nothing
from ``autoform_cli``, so any module can import it without a cycle.
``lean_libraries`` reads each ``lean_lib`` target's layout from the Lake
configuration, through ``read_snapshot_file``. ``run_probe`` runs one probe
with ``lake env lean`` after Lake's freshness check; ``impact`` and
``library.generate`` call it directly. For ``skeleton``, ``render_probe``
renders one probe per root module, ``default_runner`` checks freshness once and
builds the probe's helper module, and ``run_module_probes`` runs the probes in
a bounded pool.

Resources. Probes run one per CPU available to the process, at most eight; on
Python 3.13 and later ``PYTHON_CPU_COUNT`` sets that count. Each probe's
budget is its own rather than a share of one deadline, so its result does not
depend on which probes ran before it, and an extraction can run for one probe
budget per round of parallel probes. Every command has a timeout and an output
cap. On a failure or an interruption, Autoform terminates the command, its
process group on POSIX, and every descendant it found. A command that exits
while a descendant it can find still runs fails.

Failure reasons. When the Lake freshness check fails other than as stale, the
error keeps the first 20 lines of Lake's output and counts the rest. A reason
quotes at most 2,000 characters of Lean's output, shows the probe's temporary
directory as ``<scratch>`` and project paths relative to the project, and so
reads the same on every run. The impact probe's refusal of a root-package
module the source inventory lacks is the exception: it is quoted alone, as
Lean printed it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

import psutil
import tomli

#: Every line the probe wants read back starts with this marker, so Lean's own
#: informational output can never be mistaken for a result.
PROBE_MARKER = "AUTOFORM_SKELETON "
# Names the file the probe writes its records to; see run_probe.
PROBE_OUTPUT_ENV = "AUTOFORM_SKELETON_OUTPUT"
#: The module the probe's helpers are compiled into. A hyphen cannot appear in
#: an unquoted Lean module name, so no ordinary project module is called this.
_PROBE_HELPER_MODULE = "autoform-skeleton-helper"

#: Module roots whose declarations are never listed as assumptions: they are
#: the language itself, not mathematics a reader might want to double-check.
#: The probe counts a module as core only when its `.olean` also resolves under
#: the toolchain's own `lib/lean`; a dependency module named `Lake.Foo` is
#: external and bound like any other.
_CORE_MODULE_ROOTS = ("Init", "Lean", "Std", "Lake")

#: Lean resolves a module by its root directory on the search path, so a
#: dependency library rooted at `Std` hides the toolchain's `Std` from the probe.
_SHADOWED_CORE_MODULE = re.compile(
    r"object file '[^']*' of module ((?:" + "|".join(_CORE_MODULE_ROOTS) + r")(?:\.\S+)?) does not exist"
)
#: The impact probe's refusal of a loaded or glob-built module the source
#: inventory lacks; the project is built, so the refusal gets no `lake build` hint.
_UNCAPTURED_ROOT_MODULE = re.compile(r"the impact probe (?:loaded|found) root-package module .*")
#: A module Lake reports built whose .olean is missing, as when its artifact
#: cache is enabled without restoring artifacts into .lake/build.
_MISSING_OBJECT_FILE = re.compile(r"object file '[^']*' of module (\S+) does not exist")

#: Bounds each probe process; extraction runs one per root module.
DEFAULT_PROBE_TIMEOUT = 600.0

#: The Lake freshness check hashes every imported input, which on a Mathlib
#: project can take minutes on its own, so it does not share the probe's budget.
DEFAULT_FRESHNESS_TIMEOUT = 600.0
#: Probes run in parallel, one Lean process per root module. On a Mathlib
#: project each needs about 10 CPU-seconds and 140 MB of private memory (the
#: imported `.olean` files are mapped and shared), so extraction uses one
#: worker per CPU available to it, at most eight, and never more than modules.
_MAX_PROBE_WORKERS = 8
DEFAULT_PROBE_OUTPUT_LIMIT = 64 * 1024 * 1024
#: Characters of Lean output a failure reason quotes.
_FAILURE_DETAIL_LIMIT = 2000
_PROCESS_TERMINATION_GRACE = 2.0
#: Lake's exit status when ``--no-build`` finds a target that needs rebuilding.
_LAKE_NO_BUILD_EXIT = 3
_PROCESS_TOKEN_ENV = "_AUTOFORM_PROCESS_TOKEN"
SNAPSHOT_FILE_LIMIT = 64 * 1024 * 1024


class SkeletonError(RuntimeError):
    """A probe, or the extraction or output around it, failed; ``issues`` says why.

    This module raises it for Lake configuration, command, probe-run and
    snapshot failures; ``skeleton`` for malformed skeleton-probe records,
    blueprints, article selections, inputs that change mid-run and managed
    output; ``impact`` for malformed impact-probe records and its project
    inventory.
    """

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


class _CommandTimedOut(SkeletonError):
    """A bounded command ran out of time; its caller knows which budget to raise."""


class _ProbeEnvironmentError(SkeletonError):
    """Every probe would fail alike, so the pool stops instead of isolating it."""


# --------------------------------------------------------------------------- #
# Snapshot reads
# --------------------------------------------------------------------------- #


def _read_snapshot_pass(
    path: Path, *, keep_content: bool
) -> tuple[bytes, tuple[int, str]] | None:
    """Read one bounded regular-file pass."""

    try:
        path_metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkeletonError([f"cannot inspect skeleton input {path}: {exc}"]) from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise SkeletonError([f"skeleton input is not a regular file: {path}"])
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkeletonError([f"cannot open skeleton input {path}: {exc}"]) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SkeletonError([f"skeleton input is not a regular file: {path}"])
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        size = 0
        while True:
            block = os.read(descriptor, 64 * 1024)
            if not block:
                break
            size += len(block)
            if size > SNAPSHOT_FILE_LIMIT:
                raise SkeletonError(
                    [f"skeleton input exceeds the {SNAPSHOT_FILE_LIMIT}-byte limit: {path}"]
                )
            digest.update(block)
            if keep_content:
                chunks.append(block)
    except OSError as exc:
        raise SkeletonError([f"cannot read skeleton input {path}: {exc}"]) from exc
    finally:
        os.close(descriptor)
    return b"".join(chunks), (size, digest.hexdigest())


def read_snapshot_file(path: Path) -> tuple[bytes, tuple[int, str]] | None:
    """Return a regular file's bytes and their (size, SHA-256) fingerprint, or
    None when the path is missing.

    The file is read twice, and a change between the reads is refused, as is
    a file that is not regular, one over ``SNAPSHOT_FILE_LIMIT`` bytes, and any
    other error reaching it, such as a parent that is not a directory.
    """

    captured = _read_snapshot_pass(path, keep_content=True)
    verified = _read_snapshot_pass(path, keep_content=False)
    captured_fingerprint = None if captured is None else captured[1]
    verified_fingerprint = None if verified is None else verified[1]
    if captured_fingerprint != verified_fingerprint:
        raise SkeletonError([f"skeleton input changed while it was read: {path}"])
    return captured


#: The Lake inputs that select the compiled environment.
_PROJECT_CONTROL_FILES = (
    "lakefile.toml",
    "lakefile.lean",
    "lean-toolchain",
    "lake-manifest.json",
)


def _snapshot_regular_file(path: Path) -> tuple[int, str] | None:
    captured = read_snapshot_file(path)
    return None if captured is None else captured[1]


def project_control_snapshot(root: Path) -> tuple[tuple[str, tuple[int, str] | None], ...]:
    """Fingerprint the Lake inputs that select the compiled environment."""

    return tuple(
        (name, _snapshot_regular_file(root / name)) for name in _PROJECT_CONTROL_FILES
    )


# --------------------------------------------------------------------------- #
# Bounded commands
# --------------------------------------------------------------------------- #


def _remember_descendants(
    process: subprocess.Popen[bytes],
    descendants: dict[tuple[int, float], psutil.Process],
) -> None:
    """Retain handles for children that may outlive their immediate parent."""

    if process.returncode is not None:
        return  # a reaped leader's PID may belong to another process
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except (psutil.Error, OSError, SystemError):
        return
    for child in children:
        try:
            descendants[(child.pid, child.create_time())] = child
        except (psutil.Error, OSError, SystemError):
            continue


def _remember_tagged_processes(
    token: str,
    descendants: dict[tuple[int, float], psutil.Process],
    *,
    root_pid: int,
    strict: bool = True,
) -> None:
    """Find descendants that escaped the original parent and process group."""

    last_scan_error: BaseException | None = None
    for _attempt in range(2):
        retry = False
        try:
            candidates = tuple(psutil.process_iter())
        except (psutil.Error, OSError, SystemError) as error:
            last_scan_error = error
            continue
        for candidate in candidates:
            if candidate.pid in {os.getpid(), root_pid}:
                continue
            try:
                if candidate.environ().get(_PROCESS_TOKEN_ENV) == token:
                    descendants[(candidate.pid, candidate.create_time())] = candidate
            except (psutil.Error, OSError):
                continue
            except SystemError as error:
                last_scan_error = error
                retry = True
        if not retry:
            return
    if strict and last_scan_error is not None:
        raise SkeletonError(
            ["cannot safely inspect descendant processes after repeated process-table errors"]
        ) from last_scan_error


def _process_is_alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except (psutil.Error, OSError, SystemError):
        # Uncertainty is live for the success gate and cleanup target list.  A
        # disappeared/reused PID is handled by psutil's identity checks when
        # termination is attempted; treating inspection failure as dead could
        # let an escaped descendant survive a successful command.
        return True


def _process_group_is_alive(pid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_group_has_live_members(pid: int) -> bool:
    """Whether the group ``pid`` leads holds a process that is not a zombie.

    Its leader stays unreaped, and so a member, until the group is retired.
    """

    if not _process_group_is_alive(pid):
        return False
    try:
        for candidate in psutil.process_iter(["pid", "status"]):
            try:
                if os.getpgid(candidate.info["pid"]) == pid and candidate.info["status"] != psutil.STATUS_ZOMBIE:
                    return True
            except (OSError, psutil.Error):
                continue
    except (OSError, psutil.Error, SystemError):
        return True  # a table that cannot be read proves nothing gone
    return False


def _leader_has_exited(process: subprocess.Popen[bytes]) -> bool:
    """Whether the command has exited, leaving it unreaped on POSIX.

    ``Popen.poll`` would reap it, and a reaped leader's process group id can
    pass to an unrelated group before the next ``killpg``.
    """

    if os.name != "posix" or process.returncode is not None:
        return process.poll() is not None
    try:
        if hasattr(os, "waitid"):
            # Unlike a zombie status, this waits for every thread of the leader.
            return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        return psutil.Process(process.pid).status() == psutil.STATUS_ZOMBIE
    except psutil.ZombieProcess:
        return True
    except (ChildProcessError, psutil.NoSuchProcess):
        # Reaped elsewhere: poll records it, so nothing signals the group again.
        return process.poll() is not None
    except (OSError, psutil.Error, SystemError):
        return False


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.perf_counter())


def _join_readers(readers: list[threading.Thread], *, deadline: float) -> bool:
    for reader in readers:
        reader.join(timeout=_remaining(deadline))
    return not any(reader.is_alive() for reader in readers)


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    descendants: dict[tuple[int, float], psutil.Process],
    *,
    deadline: float,
    token: str,
) -> None:
    """Best-effort termination of a command and every descendant observed.

    The leader is reaped last, so the group is only signalled while its id
    cannot belong to another group.
    """

    started, available = time.perf_counter(), _remaining(deadline)
    for share, group_signal, method in ((0.4, "SIGTERM", "terminate"), (0.8, "SIGKILL", "kill")):
        _remember_descendants(process, descendants)
        _remember_tagged_processes(
            token,
            descendants,
            root_pid=process.pid,
            strict=False,
        )
        live = [child for child in descendants.values() if child.pid != process.pid and _process_is_alive(child)]
        if os.name == "posix" and process.returncode is None:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, getattr(signal, group_signal))
        # On POSIX killpg already reached the leader; Popen.terminate would poll, and so reap, it.
        leader = [process] if os.name != "posix" and process.poll() is None else []
        for target in leader + live:
            with contextlib.suppress(OSError, psutil.Error, SystemError):
                getattr(target, method)()
        phase_deadline = started + available * share
        while not _leader_has_exited(process) and _remaining(phase_deadline):
            time.sleep(min(0.01, _remaining(phase_deadline)))
        with contextlib.suppress(OSError, psutil.Error, SystemError):
            psutil.wait_procs(live, timeout=_remaining(phase_deadline))
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=_remaining(deadline))


class _CommandSignalled(BaseException):
    """A termination signal arrived while a bounded command was running."""


#: Per-thread state of a probe pool's workers; see run_module_probes.
_PROBE_POOL = threading.local()


_GUARDED_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP")
_ACTIVE_SIGNAL_GUARD: _SignalGuard | None = None


class _SignalGuard:
    """Route termination signals through the running command's cleanup.

    A signal that arrives while no started command is registered is deferred, so
    none can land between spawning a process group and remembering it; repeats
    during cleanup are swallowed.  On exit the previous handlers are restored and
    the first signal is re-delivered to them, so a default disposition still ends
    the process with the conventional status.
    """

    def __init__(self) -> None:
        self.previous: dict[int, object] = {}
        self.received: int | None = None
        self.armed = False

    def _handle(self, signum: int, frame: object) -> None:
        if self.received is None:
            self.received = signum
            if self.armed:
                self.armed = False
                raise _CommandSignalled(signum)

    def arm(self) -> None:
        """Let the next signal interrupt the command that was just registered."""

        if not self.previous:
            return
        if self.received is not None:
            raise _CommandSignalled(self.received)
        self.armed = True

    def disarm(self) -> None:
        self.armed = False

    def __enter__(self) -> _SignalGuard:
        global _ACTIVE_SIGNAL_GUARD
        for name in _GUARDED_SIGNALS:
            signum = getattr(signal, name)
            previous = signal.getsignal(signum)
            if previous in (signal.SIG_IGN, None):
                continue
            self.previous[signum] = previous
            signal.signal(signum, self._handle)
        _ACTIVE_SIGNAL_GUARD = self
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        global _ACTIVE_SIGNAL_GUARD
        _ACTIVE_SIGNAL_GUARD = None
        self.armed = False
        for signum, previous in self.previous.items():
            signal.signal(signum, previous)  # type: ignore[arg-type]
        if self.received is None:
            return False
        signal.raise_signal(self.received)
        if isinstance(exc, _CommandSignalled):
            name = signal.Signals(self.received).name
            raise SkeletonError([f"interrupted by {name}"]) from None
        return False


def _signal_guard() -> contextlib.AbstractContextManager[_SignalGuard]:
    """Guard signals in the main POSIX thread; reuse an enclosing guard."""

    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        return contextlib.nullcontext(_SignalGuard())
    if _ACTIVE_SIGNAL_GUARD is not None:
        return contextlib.nullcontext(_ACTIVE_SIGNAL_GUARD)
    return _SignalGuard()


def _run_bounded_command(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
    """Run one command with bounded output, time, and descendant lifetime."""

    with _signal_guard() as guard:
        try:
            return _run_registered_command(command, guard=guard, **options)  # type: ignore[arg-type]
        finally:
            guard.disarm()


def _run_registered_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    context: str,
    env: dict[str, str] | None = None,
    output_limit: int = DEFAULT_PROBE_OUTPUT_LIMIT,
    guard: _SignalGuard,
) -> subprocess.CompletedProcess[str]:
    if timeout <= 0:
        raise _CommandTimedOut([f"{context} timed out"])
    if output_limit < 1:
        raise ValueError("output_limit must be positive")
    if os.name == "posix" and signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        # The kernel would reap the command as it exits, and Popen would then report status 0.
        raise SkeletonError([f"{context} cannot run while SIGCHLD is ignored: its exit status would be lost"])
    process: subprocess.Popen[bytes] | None = None
    readers: list[threading.Thread] = []
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    capture_lock = threading.Lock()
    overflow = threading.Event()
    reader_errors: list[BaseException] = []
    captured = 0

    def drain(name: str, stream: object) -> None:
        nonlocal captured
        try:
            while True:
                block = stream.read(64 * 1024)  # type: ignore[attr-defined]
                if not block:
                    return
                with capture_lock:
                    remaining = max(0, output_limit - captured)
                    if remaining:
                        chunks[name].append(block[:remaining])
                    if len(block) > remaining:
                        overflow.set()
                    captured = min(output_limit + 1, captured + len(block))
        except (OSError, ValueError) as exc:
            reader_errors.append(exc)
        finally:
            with contextlib.suppress(OSError):
                stream.close()  # type: ignore[attr-defined]

    def check_output() -> None:
        if reader_errors:
            raise SkeletonError([f"{context} output could not be read: {reader_errors[0]}"])
        if overflow.is_set():
            raise SkeletonError([f"{context} exceeded the {output_limit}-byte output limit"])

    # Set when this command runs as one of a pool's probes and the pool is
    # being torn down; the command then ends like one that timed out.
    cancelled: threading.Event | None = getattr(_PROBE_POOL, "cancelled", None)
    descendants: dict[tuple[int, float], psutil.Process] = {}
    token = secrets.token_hex(16)
    process_env = os.environ.copy() if env is None else env.copy()
    process_env[_PROCESS_TOKEN_ENV] = token
    deadline = time.monotonic() + timeout
    cleanup_deadline: float | None = None
    try:
        if cancelled is not None and cancelled.is_set():
            raise SkeletonError([f"{context} was cancelled"])
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_env,
                close_fds=True,
                start_new_session=os.name == "posix",
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            raise SkeletonError([f"{context} failed: {exc}"]) from exc
        guard.arm()
        assert process.stdout is not None and process.stderr is not None
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            reader = threading.Thread(target=drain, args=(name, stream), daemon=True)
            reader.start()
            readers.append(reader)
        while not _leader_has_exited(process):
            _remember_descendants(process, descendants)
            check_output()
            if cancelled is not None and cancelled.is_set():
                raise SkeletonError([f"{context} was cancelled"])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _CommandTimedOut([f"{context} timed out after {timeout:g} seconds"])
            overflow.wait(min(0.05, remaining))
        _remember_descendants(process, descendants)
        if os.name == "posix" and process.returncode is not None:
            # Reaped elsewhere, as when C code ignores SIGCHLD where signal.getsignal
            # can't see it. Popen then reports status 0, and the group id is free.
            raise SkeletonError([f"{context} was reaped outside the runner: its exit status is lost"])
        group_alive = os.name == "posix" and _process_group_has_live_members(process.pid)
        _remember_tagged_processes(token, descendants, root_pid=process.pid)
        if group_alive or any(map(_process_is_alive, descendants.values())):
            raise SkeletonError([f"{context} left descendant processes running"])
        # The group is retired. No signal may land between the reap and the recorded status.
        guard.disarm()
        process.wait()
        guard.arm()
        cleanup_deadline = time.perf_counter() + _PROCESS_TERMINATION_GRACE
        if not _join_readers(readers, deadline=time.perf_counter() + _PROCESS_TERMINATION_GRACE * 0.2):
            raise SkeletonError([f"{context} left descendant processes holding its output pipes open"])
        check_output()
        try:
            stdout = b"".join(chunks["stdout"]).decode("utf-8")
            stderr = b"".join(chunks["stderr"]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkeletonError([f"{context} emitted invalid UTF-8 output"]) from exc
        return subprocess.CompletedProcess(command, process.returncode, stdout=stdout, stderr=stderr)
    except BaseException:
        guard.disarm()
        if cleanup_deadline is None:
            cleanup_deadline = time.perf_counter() + _PROCESS_TERMINATION_GRACE
        if process is not None:
            _terminate_process_tree(process, descendants, deadline=cleanup_deadline, token=token)
        _join_readers(readers, deadline=cleanup_deadline)
        raise
    finally:
        try:
            if process is not None:
                for reader, stream in zip(readers + [None, None], (process.stdout, process.stderr)):
                    if stream is not None and not (reader and reader.is_alive()):
                        stream.close()
        finally:
            # A re-raised exception retains this frame.  Drop Popen so its
            # Windows process handle does not keep an exited PID allocated.
            process = None


# --------------------------------------------------------------------------- #
# Lean project layout
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LeanLibrary:
    """One ``lean_lib`` target: where its modules live and what they are called."""

    name: str
    src_dir: Path
    roots: tuple[str, ...]
    globs: tuple[str, ...] = ()
    artifact_dir: Path | None = None


def lean_libraries(lean_root: str | Path) -> tuple[LeanLibrary, ...]:
    """Read the project's library targets from its Lake configuration.

    A TOML manifest is read directly. A Lean manifest is evaluated by Lake
    itself through ``lake translate-config``, the same way the generated verify
    workflow reads the root package name, so both manifest languages are
    handled without a second parser for Lean syntax. When both exist, Lake
    builds from ``lakefile.lean``, so that is the one read here too.

    As in Lake, a library's ``srcDir`` is relative to the package's ``srcDir``,
    which is also the source directory of a package without library targets.
    """

    root = Path(lean_root).expanduser().resolve()
    if read_snapshot_file(root / "lakefile.lean") is not None:
        text = _translate_lakefile(root)
    elif (toml_snapshot := read_snapshot_file(root / "lakefile.toml")) is not None:
        text = toml_snapshot[0]
    else:
        raise SkeletonError([f"no lakefile.toml or lakefile.lean in {root}"])
    try:
        config = tomli.loads(text.decode("utf-8"))
    except (UnicodeError, tomli.TOMLDecodeError) as exc:
        raise SkeletonError([f"cannot parse the Lake configuration of {root}: {exc}"]) from exc
    except (RecursionError, ValueError) as exc:
        raise SkeletonError(
            [f"cannot safely parse the Lake configuration of {root}: it exceeds the parser's limits"]
        ) from exc

    build_dir = config.get("buildDir", ".lake/build")
    lean_lib_dir = config.get("leanLibDir", "lib/lean")
    if not isinstance(build_dir, str) or not isinstance(lean_lib_dir, str):
        raise SkeletonError([f"cannot read the buildDir/leanLibDir of {root}"])
    artifact_dir = (root / build_dir / lean_lib_dir).resolve()
    package_src = root / str(config.get("srcDir", "."))

    libraries: list[LeanLibrary] = []
    for entry in config.get("lean_lib", []) or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        src_dir = package_src / str(entry.get("srcDir", "."))
        roots = entry.get("roots")
        if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
            roots = [name]
        # Lake accepts one glob or an array, and translate-config writes them
        # only when they differ from the default of one glob per root.
        globs = entry.get("globs")
        if isinstance(globs, str):
            globs = [globs]
        if not isinstance(globs, list) or not all(isinstance(item, str) for item in globs):
            globs = []
        libraries.append(
            LeanLibrary(
                name=name,
                src_dir=src_dir.resolve(),
                roots=tuple(roots),
                globs=tuple(globs),
                artifact_dir=artifact_dir,
            )
        )
    if not libraries:
        package = config.get("name")
        if isinstance(package, str) and package:
            libraries.append(
                LeanLibrary(name=package, src_dir=package_src.resolve(), roots=(package,), artifact_dir=artifact_dir)
            )
    if not libraries:
        raise SkeletonError([f"the Lake configuration of {root} declares no library"])
    return tuple(libraries)


def _translate_lakefile(root: Path) -> bytes:
    lake = shutil.which("lake")
    if lake is None:
        raise SkeletonError(["lake is not on PATH, so lakefile.lean cannot be evaluated"])
    with _signal_guard(), tempfile.TemporaryDirectory(prefix="autoform-skeleton-") as scratch:
        target = Path(scratch) / "lakefile.toml"
        result = _run_bounded_command(
            [lake, "translate-config", "toml", str(target)],
            cwd=root,
            timeout=120,
            context="lake translate-config",
        )
        if result.returncode != 0:
            output = (result.stderr or result.stdout).strip()
            # With both lakefiles, Lake opens with an info line saying which one it
            # uses; leave it out so the error itself fits in the limit.
            errors = "\n".join(line for line in output.splitlines() if not line.startswith("info:")).strip()
            raise SkeletonError([f"lake translate-config failed: {(errors or output)[:300]}"])
        translated = read_snapshot_file(target)
        if translated is None:
            raise SkeletonError(["lake translate-config failed: translated configuration is missing"])
        return translated[0]


def module_of(path: Path, libraries: tuple[LeanLibrary, ...]) -> str | None:
    """Return the Lean module name of a source file, or ``None`` if no library builds it.

    Lake builds only the modules a library's roots and globs make buildable, not
    every file under its srcDir, so a stray file there has no module.
    """

    resolved = path.resolve()
    if resolved.suffix != ".lean":
        return None
    held = [lib for lib in libraries if resolved != lib.src_dir and resolved.is_relative_to(lib.src_dir)]
    for library in sorted(held, key=lambda lib: len(lib.src_dir.parts), reverse=True):
        module = ".".join(resolved.relative_to(library.src_dir).with_suffix("").parts)
        if _builds(library, module):
            return module
    return None


def _builds(library: LeanLibrary, module: str) -> bool:
    """Whether Lake builds ``module`` for ``library``, as ``LeanLibConfig.isBuildableModule`` decides.

    Lake finds, imports and builds a module by that rule, not by ``isLocalModule``:
    a glob matches the module, or a root prefixes it and a glob matches that root.
    Without ``globs``, each root is its own glob.
    """

    globs = library.globs or library.roots

    def globbed(name: str) -> bool:
        for glob in globs:
            base, mode = (glob[:-2], glob[-1]) if glob.endswith((".*", ".+")) else (glob, "")
            if (name == base and mode != "+") or (mode and name.startswith(f"{base}.")):
                return True
        return False

    return globbed(module) or any(
        (module == root or module.startswith(f"{root}.")) and globbed(root) for root in library.roots
    )


def path_of(module: str, libraries: tuple[LeanLibrary, ...], lean_root: Path) -> str | None:
    """Return the repository-relative source path of ``module``, if it exists."""

    parts = module.split(".")
    for library in libraries:
        candidate = library.src_dir.joinpath(*parts).with_suffix(".lean")
        if candidate.is_file():
            try:
                return candidate.resolve().relative_to(lean_root.resolve()).as_posix()
            except ValueError:
                return candidate.name
    return None


def stable_paths(text: str, lean_root: Path, *scratch: Path) -> str:
    """``text`` with the paths that change from run to run made stable.

    Each ``scratch`` directory, which changes on every run, reads as
    `<scratch>`, and paths inside the project read relative to it; paths
    elsewhere, such as the toolchain's or a sibling directory's, stay as Lean
    printed them.
    """

    replacements = {str(path): "<scratch>" for directory in scratch for path in (directory, directory.resolve())}
    for root in (lean_root, lean_root.resolve()):
        replacements[str(root) + os.sep] = ""
        replacements[str(root)] = "."
    # A directory matches only where its name ends, so `/srv/proj` leaves `/srv/proj-old` alone.
    pattern = "|".join(
        re.escape(old) + ("" if old.endswith(os.sep) else r"(?![\w.-])")
        for old in sorted(replacements, key=len, reverse=True)
    )
    return re.sub(pattern, lambda match: replacements[match.group()], text)


def _stable_detail(text: str, lean_root: Path, *scratch: Path) -> str:
    """``stable_paths`` of a failure reason, cut to ``_FAILURE_DETAIL_LIMIT`` characters."""

    text = stable_paths(text, lean_root, *scratch)
    if len(text) <= _FAILURE_DETAIL_LIMIT:
        return text
    keep = _FAILURE_DETAIL_LIMIT - 64
    cut = text.rfind("\n", keep // 2, keep)
    cut = keep if cut < 0 else cut
    return f"{text[:cut]}\n[{len(text) - cut} more characters]"


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #

def _probe_template() -> str:
    """The probe's Lean helpers, kept under probes/ beside this module."""

    return (Path(__file__).parent / "probes" / "skeleton_probe.lean").read_text(encoding="utf-8")


def _render_probe_helper() -> str:
    """Render the helper module every probe imports; see ``_build_probe_helper``."""

    return _probe_template().format(
        core_roots=", ".join(_lean_name(name) for name in _CORE_MODULE_ROOTS),
        error_limit=_FAILURE_DETAIL_LIMIT,
        marker=PROBE_MARKER,
        output_env=PROBE_OUTPUT_ENV,
        output_limit=DEFAULT_PROBE_OUTPUT_LIMIT,
    )


def render_probe(
    *,
    imports: tuple[str, ...],
    roots: tuple[str, ...],
    project_roots: tuple[str, ...],
) -> str:
    """Render the Lean program that extracts the skeleton of every root.

    The program imports ``imports`` and the compiled helper module and makes one
    fully qualified call, with no ``open`` or ``set_option``: the helpers were
    elaborated without the project in scope, and signatures print as they do
    to a file with only those imports.
    """

    if not roots:
        raise SkeletonError(["refusing to render a probe with no declarations"])
    if not imports:
        raise SkeletonError(["refusing to render a probe with no imports"])
    lines = [f"import {module}" for module in sorted(set(imports))]
    lines.append(f"import «{_PROBE_HELPER_MODULE}»")
    project = lean_requests(tuple(sorted(set(project_roots))))
    lines.append(f"run_cmd AutoformSkeleton.main {project} {lean_requests(roots)}")
    return "\n".join(lines) + "\n"


def _lean_name(name: str) -> str:
    """Spell a Lean name as a term without trusting Lean to parse it."""

    result = "Name.anonymous"
    for component in lean_name_components(name):
        if isinstance(component, int):
            result = f"Name.num ({result}) {component}"
        else:
            result = f"Name.str ({result}) {json.dumps(component, ensure_ascii=False)}"
    return result


def lean_requests(roots: tuple[str, ...]) -> str:
    """Spell the requested roots as a Lean string of JSON the probe decodes.

    The probe would compile a list literal of names, and Lean's code generator
    gives up on one longer than about 1,500 entries.
    """

    payload = [[name, list(lean_name_components(name))] for name in roots]
    return json.dumps(json.dumps(payload, ensure_ascii=False), ensure_ascii=False)


def lean_name_components(name: str) -> tuple[str | int, ...]:
    """The components of a Lean name: unquoted ASCII digits are numeric."""

    return tuple(
        int(part) if not quoted and part.isascii() and part.isdigit() else part
        for part, quoted in lean_name_parts(name)
    )


_LEAN_NAME_PART = r"«([^»]+)»|([^.\s«»]+)"
_LEAN_NAME = re.compile(rf"(?:{_LEAN_NAME_PART})(?:\.(?:{_LEAN_NAME_PART}))*")


def lean_name_parts(name: str) -> tuple[tuple[str, bool], ...]:
    """Parse the dot-separated surface spelling of a Lean ``Name``.

    Guillemets quote one name component, so ``A.«b.c d»`` has two components,
    not three. Constructing the name structurally keeps all such spellings out
    of generated Lean syntax.
    """

    if not _LEAN_NAME.fullmatch(name):
        raise SkeletonError([f"invalid Lean declaration name: {name!r}"])
    return tuple((quoted, True) if quoted else (plain, False) for quoted, plain in re.findall(_LEAN_NAME_PART, name))


def _probe_modules(probe: str) -> tuple[str, ...]:
    """Read the generated probe's leading project imports, without its helper."""

    modules: list[str] = []
    for line in probe.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("import "):
            break
        module = stripped.removeprefix("import ").strip()
        if not module or any(character.isspace() for character in module):
            raise SkeletonError(["the generated skeleton probe contains an invalid import"])
        if module != f"«{_PROBE_HELPER_MODULE}»":
            modules.append(module)
    if not modules:
        raise SkeletonError(["the generated skeleton probe contains no project imports"])
    return tuple(dict.fromkeys(modules))


def _probe_purpose(label: str) -> str:
    """What `lake build` must come before, as the freshness messages say it."""

    return "extracting skeletons" if label == "skeleton probe" else f"running the {label}"


STALE_BUILD = "Lean build artifacts are stale; run `lake build "
_LAKE_SAID = 200
# A freshness check over thousands of modules can fail with a line for each.
_LAKE_LINES = 20


def _check_artifacts_fresh(
    lake: str, lean_root: Path, modules: tuple[str, ...], *, timeout: float, label: str = "skeleton probe"
) -> None:
    """Ask Lake to prove that imported artifacts match their exact inputs.

    ``--rehash`` distrusts every cached ``.hash`` sidecar and hashes the inputs
    afresh; Lake has no mode that does so without rewriting those sidecars, so
    the project's build directory must be writable.
    """

    result = _run_bounded_command(
        [lake, "--rehash", "--no-build", "build", *modules],
        cwd=lean_root,
        timeout=timeout,
        context="cannot verify Lean build freshness",
    )
    # Lake prints each target's line on stdout and only warnings and its summary on stderr.
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    if result.returncode == _LAKE_NO_BUILD_EXIT:
        raise SkeletonError([f"{STALE_BUILD}{shlex.join(modules)}` before {_probe_purpose(label)}\n{output}"])
    if result.returncode != 0:
        raise SkeletonError(
            [
                f"cannot verify Lean build freshness: `lake --rehash --no-build build` exited with status "
                f"{result.returncode}; the check rewrites `.hash` files under `.lake`, which must be writable"
                f"\n{_first_lines(output)}"
            ]
        )


def _first_lines(detail: str) -> str:
    """Lake's first lines, with a count of the rest."""

    lines = detail.splitlines()
    if len(lines) <= _LAKE_LINES:
        return detail
    return "\n".join([*lines[:_LAKE_LINES], f"({len(lines) - _LAKE_LINES} more lines from Lake)"])


def stale_build_said(error: SkeletonError) -> str | None:
    """Lake's line for the first unbuilt module if ``error`` is a stale build, ``""`` if Lake said nothing.

    ``_check_artifacts_fresh`` names every probed module and appends all of Lake's
    output, in which Lake first replays the warnings of what is built; ``✖``
    marks the module that is not. A caller that imports whole libraries names
    them instead, with only this line.
    """

    detail = "; ".join(error.issues)
    if not detail.startswith(STALE_BUILD):
        return None
    lines = [line.strip() for line in detail.split("\n")[1:] if line.strip()]
    said = next((line for line in lines if line.startswith("✖")), lines[0] if lines else "")
    return said if len(said) <= _LAKE_SAID else said[: _LAKE_SAID - 3].rstrip() + "..."


def _lake_executable(lean_root: Path, label: str = "skeleton probe") -> str:
    """Find Lake, and refuse a project Lake has never resolved."""

    lake = shutil.which("lake")
    if lake is None:
        raise SkeletonError([f"lake is not on PATH; {_probe_purpose(label)} requires a built Lean project"])
    if not (lean_root / "lake-manifest.json").is_file():
        raise SkeletonError(
            [f"lake-manifest.json is missing; run `lake build` before {_probe_purpose(label)}"]
        )
    return lake


def _check_build_freshness(
    lean_root: Path, modules: tuple[str, ...], *, timeout: float = DEFAULT_FRESHNESS_TIMEOUT
) -> None:
    """Run the Lake freshness check once over every module about to be probed."""

    _check_artifacts_fresh(_lake_executable(lean_root), lean_root, modules, timeout=timeout)


def _probe_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def _build_probe_helper(lean_root: Path, directory: Path, *, timeout: float) -> Path:
    """Compile the probe's helpers into ``directory`` and return it.

    The helpers are elaborated with the project's toolchain and no project
    module imported, so no project name, notation, or option reaches them; each
    probe finds the module through ``LEAN_PATH``. A failure here concerns every
    probe.
    """

    lake = _lake_executable(lean_root)
    olean = directory / f"{_PROBE_HELPER_MODULE}.olean"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / f"{_PROBE_HELPER_MODULE}.lean"
        source.write_text(_render_probe_helper(), encoding="utf-8")
        result = _run_bounded_command(
            [lake, "env", "lean", f"--root={directory}", "-o", str(olean), str(source)],
            cwd=lean_root,
            timeout=timeout,
            context="building the skeleton probe helpers",
            env=_probe_environment(),
        )
    except _CommandTimedOut as exc:
        raise _ProbeEnvironmentError([f"{exc.issues[0]}; rerun with --timeout <seconds> for large projects"]) from exc
    if result.returncode != 0:
        raw = (result.stderr or result.stdout).strip()
        detail = _stable_detail(raw, lean_root, directory)
        _raise_if_core_module_shadowed(raw, detail)
        raise _ProbeEnvironmentError([f"cannot build the skeleton probe helpers\n{detail}"])
    return directory


def _raise_if_core_module_shadowed(output: str, detail: str, label: str = "skeleton probe") -> None:
    """Name the dependency library at fault when Lean could not load a toolchain module.

    ``output`` is Lean's whole output, searched because ``detail``, the
    reason's shortened copy, could cut the refusal off.
    """

    shadowed = _SHADOWED_CORE_MODULE.search(output)
    if shadowed:
        root = shadowed.group(1).split(".", 1)[0]
        raise _ProbeEnvironmentError(
            [
                f"the {label} cannot load toolchain module {shadowed.group(1)}: a dependency "
                f"library probably provides modules under `{root}`, which hides the toolchain's own `{root}`; "
                f"rename that library's modules\n{detail}"
            ]
        )


def run_probe(
    probe: str,
    lean_root: Path,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    freshness_timeout: float = DEFAULT_FRESHNESS_TIMEOUT,
    check_freshness: bool = True,
    helper: Path | None = None,
    label: str = "skeleton probe",
) -> str:
    """Run ``probe`` with ``lake env lean`` inside the built project.

    ``freshness_timeout`` bounds the Lake freshness check that runs first and
    ``timeout`` the probe itself; neither spends the other's budget. Extraction
    checks freshness once for all its probes and passes ``check_freshness=False``.
    ``helper`` is the directory ``_build_probe_helper`` put the helpers in;
    without one, a probe that imports them gets its own within ``timeout``.
    ``label`` names the probe in failure messages.
    """

    lake = _lake_executable(lean_root, label)
    modules = _probe_modules(probe)
    if check_freshness:
        _check_artifacts_fresh(lake, lean_root, modules, timeout=freshness_timeout, label=label)
    deadline = time.monotonic() + timeout
    env = _probe_environment()
    with _signal_guard(), tempfile.TemporaryDirectory(prefix="autoform-skeleton-") as scratch:
        if f"import «{_PROBE_HELPER_MODULE}»" in probe.splitlines():
            if helper is None:
                helper = _build_probe_helper(
                    lean_root, Path(scratch) / "helper", timeout=max(0.0, deadline - time.monotonic())
                )
            if not (helper / f"{_PROBE_HELPER_MODULE}.olean").is_file():
                raise _ProbeEnvironmentError([f"the skeleton probe helpers are missing from {helper}"])
            # Lake puts the project's own directories first, so the helper cannot
            # hide a project module; one with the helper's name hides the helper,
            # and the probe fails.
            env["LEAN_PATH"] = os.pathsep.join(filter(None, (env.get("LEAN_PATH"), str(helper))))
        source = Path(scratch) / "AutoformSkeletonProbe.lean"
        source.write_text(probe, encoding="utf-8")
        # Records go to their own file: on stdout any other write could split one.
        records = Path(scratch) / "records.out"
        env[PROBE_OUTPUT_ENV] = str(records)
        try:
            result = _run_bounded_command(
                [lake, "env", "lean", str(source)],
                cwd=lean_root,
                timeout=max(0.0, deadline - time.monotonic()),
                context="lake env lean",
                env=env,
            )
        except _CommandTimedOut as exc:
            raise SkeletonError(
                [
                    f"lake env lean timed out after {timeout:g} seconds; "
                    "rerun with --timeout <seconds> for large projects"
                ]
            ) from exc
        output = _read_probe_records(records, label) if result.returncode == 0 else ""
    if result.returncode != 0:
        raw = (result.stderr or result.stdout).strip()
        detail = _stable_detail(raw, lean_root, *filter(None, (Path(scratch), helper)))
        _raise_if_core_module_shadowed(raw, detail, label)
        # Searched in the whole output: the shortened reason could cut the refusal off.
        uncaptured = _UNCAPTURED_ROOT_MODULE.search(raw)
        if uncaptured:
            raise SkeletonError([uncaptured.group()])
        missing = _MISSING_OBJECT_FILE.search(raw)
        if missing:
            raise SkeletonError(
                [
                    f"the {label} found no .olean for {missing.group(1)}; if Lake's artifact cache "
                    "is enabled, set `restoreAllArtifacts = true` in the lakefile, otherwise run `lake build`"
                    f"\n{detail}"
                ]
            )
        if result.returncode < 0:
            # Most likely the out-of-memory killer, which leaves no output to explain it.
            raise SkeletonError([f"the {label} was killed by signal {-result.returncode}\n{detail}".rstrip()])
        raise SkeletonError([f"the {label} failed; is the project built with `lake build`?\n{detail}"])
    return output or result.stdout


def default_runner(
    root: Path, *, timeout: float
) -> tuple[Callable[[tuple[str, ...]], contextlib.AbstractContextManager[object]], Callable[[str, Path], str]]:
    """Return ``(probing, runner)``, the probe runner extraction uses by default.

    ``probing(modules)`` holds the signal guard and one scratch directory while
    ``modules`` are probed: it runs the Lake freshness check once over them,
    within its own default timeout, and builds the probe's helpers there within
    ``timeout``. ``runner`` then runs each probe with those helpers and no
    second freshness check, within a ``timeout`` of its own.
    """

    helper: Path | None = None

    @contextlib.contextmanager
    def probing(modules: tuple[str, ...]) -> Iterator[None]:
        nonlocal helper
        # The guard encloses the scratch directory, so the commands inside
        # reuse it and a termination signal is re-delivered once the directory
        # is gone, before any boundary module is hashed.
        with _signal_guard(), tempfile.TemporaryDirectory(prefix="autoform-skeleton-") as scratch:
            _check_build_freshness(root, modules)
            helper = _build_probe_helper(root, Path(scratch), timeout=timeout)
            yield

    def runner(probe: str, lean_root: Path) -> str:
        return run_probe(probe, lean_root, timeout=timeout, check_freshness=False, helper=helper)

    return probing, runner


def _read_probe_records(records: Path, label: str) -> str:
    """Read the probe's records file under the same byte limit as its captured output."""

    try:
        with records.open("rb") as handle:
            data = handle.read(DEFAULT_PROBE_OUTPUT_LIMIT + 1)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise SkeletonError([f"the {label}'s records could not be read: {exc.strerror or exc}"]) from exc
    if len(data) > DEFAULT_PROBE_OUTPUT_LIMIT:
        raise SkeletonError([f"lake env lean exceeded the {DEFAULT_PROBE_OUTPUT_LIMIT}-byte output limit"])
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkeletonError([f"the {label}'s records are not valid UTF-8"]) from exc


def _probe_workers(jobs: int) -> int:
    """How many probe processes run at once; see ``_MAX_PROBE_WORKERS``."""

    if hasattr(os, "process_cpu_count"):
        cpus = os.process_cpu_count()
    elif hasattr(os, "sched_getaffinity"):
        cpus = len(os.sched_getaffinity(0))
    else:
        cpus = os.cpu_count()
    return max(1, min(jobs, _MAX_PROBE_WORKERS, cpus or 1))


def run_module_probes(modules: list[str], probe: Callable[[str], object]) -> dict[str, object]:
    """Run ``probe(module)`` for each module in a bounded pool and collect the results.

    A ``SkeletonError`` from one probe is that module's result. Anything else,
    an interrupt, a signal, or a ``_ProbeEnvironmentError``, cancels the pool:
    every running command terminates its process tree and every worker is
    joined before the error propagates. Each probe has the budget its command
    enforces; a budget shared across the pool would make which module fails
    depend on scheduling.
    """

    cancelled = threading.Event()

    def run(module: str) -> object:
        _PROBE_POOL.cancelled = cancelled
        try:
            return probe(module)
        finally:
            _PROBE_POOL.cancelled = None

    results: dict[str, object] = {}
    pool = ThreadPoolExecutor(max_workers=_probe_workers(len(modules)), thread_name_prefix="autoform-probe")
    with _signal_guard() as guard:
        try:
            futures = {pool.submit(run, module): module for module in modules}
            pending = set(futures)
            while pending:
                # Polled rather than armed: a signal raised inside wait() could
                # leave a future's lock held, and the join below would hang.
                if guard.received is not None:
                    raise _CommandSignalled(guard.received)
                done, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        results[futures[future]] = future.result()
                    except _ProbeEnvironmentError:
                        raise
                    except SkeletonError as exc:
                        results[futures[future]] = exc
        finally:
            cancelled.set()
            pool.shutdown(wait=True, cancel_futures=True)
    return results


__all__ = [
    "DEFAULT_FRESHNESS_TIMEOUT",
    "DEFAULT_PROBE_OUTPUT_LIMIT",
    "DEFAULT_PROBE_TIMEOUT",
    "PROBE_MARKER",
    "PROBE_OUTPUT_ENV",
    "SNAPSHOT_FILE_LIMIT",
    "STALE_BUILD",
    "LeanLibrary",
    "SkeletonError",
    "default_runner",
    "lean_libraries",
    "lean_name_components",
    "lean_name_parts",
    "lean_requests",
    "module_of",
    "path_of",
    "project_control_snapshot",
    "read_snapshot_file",
    "render_probe",
    "run_module_probes",
    "run_probe",
    "stable_paths",
    "stale_build_said",
]
