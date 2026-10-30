"""Tests for the Lean probe runner in ``autoform_cli.lean_probe``."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import psutil
import pytest

from autoform_cli.lean_probe import (
    PROBE_MARKER,
    PROBE_OUTPUT_ENV,
    _PROBE_POOL,
    _PROCESS_TERMINATION_GRACE,
    SkeletonError,
    _CommandTimedOut,
    _ProbeEnvironmentError,
    _build_probe_helper,
    _join_readers,
    lean_libraries,
    module_of,
    path_of,
    _probe_workers,
    _process_is_alive,
    project_control_snapshot,
    _read_probe_records,
    _remember_tagged_processes,
    render_probe,
    _render_probe_helper,
    _run_bounded_command,
    run_module_probes,
    run_probe,
    stable_paths,
    stale_build_said,
    _terminate_process_tree,
)
from tests.conftest import _SKELETON_FIXTURE, _lean_toolchain_available
from tests.test_skeleton import _assert_no_survivors, _build, _pid_is_live, _project, _requested, _signal_driver


# --------------------------------------------------------------------------- #
# The probe program
# --------------------------------------------------------------------------- #


def test_probe_spells_names_without_trusting_lean_to_parse_them() -> None:
    probe = render_probe(
        imports=("Skel.Main", "Skel.Defs", "Skel.Main"),
        roots=("Skel.observation_determined",),
        project_roots=("Skel",),
    )
    helper = _render_probe_helper()

    assert probe.startswith("import Skel.Defs\nimport Skel.Main\nimport «autoform-skeleton-helper»\n")
    assert _requested(probe) == [["Skel.observation_determined", ["Skel", "observation_determined"]]]
    assert f'"{PROBE_MARKER}' in helper
    assert "def probeOutputLimit : Nat := 67108864" in helper
    assert 'Name.str (Name.anonymous) "Init"' in helper
    assert "info.fromClass" in helper
    assert "privateToUserName c" in helper


def test_probe_elaborates_no_helper_under_the_project_imports() -> None:
    probe = render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))

    # Only this call elaborates with the project in scope, and it opens nothing.
    assert probe.splitlines() == [
        "import Skel.Main",
        "import «autoform-skeleton-helper»",
        "run_cmd AutoformSkeleton.main "
        + json.dumps(json.dumps([["Skel", ["Skel"]]]))
        + " "
        + json.dumps(json.dumps([["Skel.x", ["Skel", "x"]]])),
    ]
    assert "set_option" not in _render_probe_helper()


def test_probe_transports_quoted_and_numeric_name_components_structurally() -> None:
    probe = render_probe(
        imports=("Skel.Main",),
        roots=("Skel.«quoted.name with space».2", "Skel.«2»", "Skel.٢"),
        project_roots=("Skel.«quoted».2",),
    )

    assert _requested(probe, library_roots=True) == [["Skel.«quoted».2", ["Skel", "quoted", 2]]]
    assert _requested(probe) == [
        ["Skel.«quoted.name with space».2", ["Skel", "quoted.name with space", 2]],
        ["Skel.«2»", ["Skel", "2"]],
        ["Skel.٢", ["Skel", "٢"]],
    ]


def test_probe_refuses_to_render_nothing() -> None:
    with pytest.raises(SkeletonError):
        render_probe(imports=("Skel",), roots=(), project_roots=("Skel",))
    with pytest.raises(SkeletonError):
        render_probe(imports=(), roots=("Skel.x",), project_roots=("Skel",))


@pytest.fixture
def probe(tmp_path: Path, monkeypatch) -> str:
    (tmp_path / "lake-manifest.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("autoform_cli.lean_probe.shutil.which", lambda executable: "/bin/lake")
    return render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))


def test_probe_refuses_stale_artifacts_before_executing_lean(tmp_path: Path, monkeypatch, probe: str) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 3, stdout="target is out-of-date", stderr="")

    monkeypatch.setattr("autoform_cli.lean_probe._run_bounded_command", fake_run)

    with pytest.raises(SkeletonError, match=r"run `lake build Skel.Main`"):
        run_probe(probe, tmp_path)

    assert calls == [["/bin/lake", "--rehash", "--no-build", "build", "Skel.Main"]]


def test_probe_reports_a_failed_freshness_check_apart_from_stale_artifacts(
    tmp_path: Path, monkeypatch, probe: str
) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, stdout="error: permission denied (error code: 13)", stderr=""
        )

    monkeypatch.setattr("autoform_cli.lean_probe._run_bounded_command", fake_run)

    with pytest.raises(SkeletonError, match="must be writable") as refused:
        run_probe(probe, tmp_path)

    assert "stale" not in str(refused.value)
    assert "permission denied" in str(refused.value)


def test_a_failed_freshness_check_keeps_only_the_start_of_lake_output(
    tmp_path: Path, monkeypatch, probe: str
) -> None:
    lake = "\n".join(f"error: .lake/build/lib/lean/Skel/M{number}.hash: permission denied" for number in range(5000))

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=lake)

    monkeypatch.setattr("autoform_cli.lean_probe._run_bounded_command", fake_run)

    with pytest.raises(SkeletonError) as refused:
        run_probe(probe, tmp_path)

    [message] = refused.value.issues
    lines = message.splitlines()
    assert lines[1] == "error: .lake/build/lib/lean/Skel/M0.hash: permission denied"
    assert lines[-1] == "(4980 more lines from Lake)"
    assert len(lines) == 22


def test_a_failed_freshness_check_keeps_the_reason_lake_prints_on_stdout(
    tmp_path: Path, monkeypatch, probe: str
) -> None:
    # Lake prints each target's error on stdout and only its summary on stderr.
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, stdout="error: Skel/Main.olean.hash: permission denied\n", stderr="error: build failed\n"
        )

    monkeypatch.setattr("autoform_cli.lean_probe._run_bounded_command", fake_run)

    with pytest.raises(SkeletonError) as refused:
        run_probe(probe, tmp_path)

    [message] = refused.value.issues
    assert message.splitlines()[1:] == ["error: Skel/Main.olean.hash: permission denied", "error: build failed"]


def test_a_stale_build_keeps_the_unbuilt_module_lake_prints_on_stdout_beside_a_warning_on_stderr(
    tmp_path: Path, monkeypatch, probe: str
) -> None:
    # Lake prints its build monitor on stdout; a workspace warning still reaches stderr.
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            3,
            stdout="✖ [2/3] Building Skel.Main\nerror: target is out-of-date and needs to be rebuilt\n",
            stderr="warning: manifest out of date: packages directory changed\n",
        )

    monkeypatch.setattr("autoform_cli.lean_probe._run_bounded_command", fake_run)

    with pytest.raises(SkeletonError) as refused:
        run_probe(probe, tmp_path)

    assert stale_build_said(refused.value) == "✖ [2/3] Building Skel.Main"


def _bounded(tmp_path: Path, program: str, **options: object) -> object:
    options = {"timeout": 10, "context": "test command", **options}
    return _run_bounded_command([sys.executable, "-c", program], cwd=tmp_path, **options)


def _assert_dies(pid: int, what: str, why: str) -> None:
    deadline = time.monotonic() + 5
    while _pid_is_live(pid):
        if time.monotonic() >= deadline:
            pytest.fail(f"{what} process {pid} survived {why}")
        time.sleep(0.01)


def test_bounded_command_rejects_excess_output(tmp_path: Path) -> None:
    with pytest.raises(SkeletonError, match="1024-byte output limit"):
        _bounded(tmp_path, "import os; os.write(1, b'x' * 4096)", output_limit=1024)


def test_bounded_command_caps_stdout_and_stderr_together(tmp_path: Path) -> None:
    program = "import os; os.write(1, b'x' * 700); os.write(2, b'y' * 700)"
    with pytest.raises(SkeletonError, match="1024-byte output limit"):
        _bounded(tmp_path, program, output_limit=1024)


def test_bounded_command_rejects_invalid_utf8(tmp_path: Path) -> None:
    with pytest.raises(SkeletonError, match="invalid UTF-8"):
        _bounded(tmp_path, "import os; os.write(1, b'\\xff')")


def test_bounded_command_timeout_kills_descendants(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    program = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )

    with pytest.raises(SkeletonError, match="timed out"):
        _bounded(tmp_path, program, timeout=2)

    _assert_dies(int(child_pid.read_text(encoding="utf-8")), "descendant", "command timeout")


def test_bounded_command_rejects_a_successful_parent_with_a_live_descendant(
    tmp_path: Path,
) -> None:
    child_pid = tmp_path / "child.pid"
    program = (
        "import pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))"
    )

    with pytest.raises(SkeletonError, match="descendant processes"):
        _bounded(tmp_path, program)
    _assert_dies(int(child_pid.read_text(encoding="utf-8")), "descendant", "successful parent exit")


_OUTLIVED_BY_A_CHILD = (
    "import subprocess, sys; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
    "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
@pytest.mark.parametrize("waitid", [True, False], ids=["waitid", "zombie-status"])
@pytest.mark.parametrize(
    ("program", "refusal"),
    [("pass", None), (_OUTLIVED_BY_A_CHILD, "descendant processes"), ("import time; time.sleep(30)", "timed out")],
    ids=["exits", "outlived", "timed-out"],
)
def test_bounded_command_signals_its_group_only_while_the_leader_is_unreaped(
    tmp_path: Path, monkeypatch, program: str, refusal: str | None, waitid: bool
) -> None:
    # A reaped leader's process group id can pass to an unrelated group, which
    # a later killpg would then signal. Python before 3.13 has no os.waitid on
    # macOS, where psutil's zombie status stands in.
    leaders: dict[int, subprocess.Popen[bytes]] = {}
    popen, killpg = subprocess.Popen, os.killpg

    def remember(*args, **kwargs):
        process = popen(*args, **kwargs)
        leaders[process.pid] = process
        return process

    def unreaped(pid: int) -> bool:
        try:
            os.kill(pid, 0)  # a zombie still answers; a reaped process does not
        except ProcessLookupError:
            return False
        return True

    def checked_killpg(pid: int, signal_number: int) -> None:
        assert pid not in leaders or (leaders[pid].returncode is None and unreaped(pid)), (
            "signalled a reaped leader's group"
        )
        killpg(pid, signal_number)

    monkeypatch.setattr("autoform_cli.lean_probe.subprocess.Popen", remember)
    monkeypatch.setattr("autoform_cli.lean_probe.os.killpg", checked_killpg)
    if not waitid:
        monkeypatch.delattr(os, "waitid", raising=False)
    if refusal is None:
        assert _bounded(tmp_path, program).returncode == 0
    else:
        with pytest.raises(SkeletonError, match=refusal):
            _bounded(tmp_path, program, **({"timeout": 1} if refusal == "timed out" else {}))
    assert all(leader.returncode is not None for leader in leaders.values())


@pytest.mark.skipif(os.name != "posix", reason="SIGCHLD is POSIX-specific")
def test_bounded_command_refuses_to_run_while_sigchld_is_ignored(tmp_path: Path) -> None:
    # The kernel reaps each child as it exits and Popen reports status 0, so a
    # failed run, such as Lake's stale-artifact exit 3, would read as a success.
    previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        with pytest.raises(SkeletonError, match="cannot run while SIGCHLD is ignored"):
            _bounded(tmp_path, "raise SystemExit(3)")
    finally:
        signal.signal(signal.SIGCHLD, previous)


@pytest.mark.skipif(os.name != "posix", reason="SIGCHLD is POSIX-specific")
def test_bounded_command_refuses_a_command_reaped_outside_the_runner(tmp_path: Path) -> None:
    # signal.getsignal can't see a disposition set from C, but the kernel still
    # reaps the command as it exits, and Popen would report status 0.
    libc_signal = ctypes.CDLL(None).signal
    libc_signal.argtypes, libc_signal.restype = [ctypes.c_int, ctypes.c_void_p], ctypes.c_void_p
    libc_signal(signal.SIGCHLD, 1)  # SIG_IGN
    try:
        with pytest.raises(SkeletonError, match="reaped outside the runner"):
            _bounded(tmp_path, "raise SystemExit(3)")
    finally:
        signal.signal(signal.SIGCHLD, signal.getsignal(signal.SIGCHLD))  # back to what Python records


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
@pytest.mark.parametrize("waitid", [True, False], ids=["waitid", "zombie-status"])
def test_bounded_command_kills_a_child_that_only_its_group_reveals(tmp_path: Path, monkeypatch, waitid: bool) -> None:
    # A child that clears its environment escapes the token scan, and one the
    # descendant snapshot misses leaves only the process group to find it.
    child_pid = tmp_path / "child.pid"
    program = (
        "import pathlib, subprocess; "
        "child = subprocess.Popen(['/usr/bin/env', '-i', '/bin/sleep', '30'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))"
    )
    monkeypatch.setattr("autoform_cli.lean_probe._remember_descendants", lambda process, descendants: None)
    if not waitid:
        monkeypatch.delattr(os, "waitid", raising=False)
    with pytest.raises(SkeletonError, match="left descendant processes running"):
        _bounded(tmp_path, program)
    _assert_dies(int(child_pid.read_text(encoding="utf-8")), "environment-free", "a run that found it")


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
def test_bounded_command_defers_a_signal_that_arrives_while_the_leader_is_reaped(tmp_path: Path, monkeypatch) -> None:
    # A signal between waitpid's reap and Popen's recorded status must not reach
    # cleanup while the leader still looks unreaped, or cleanup would signal its
    # freed group id. The signal is handled once the reap is recorded.
    reaped: set[int] = set()
    waitpid, killpg = os.waitpid, os.killpg

    def signalled_waitpid(pid: int, options: int) -> tuple[int, int]:
        result = waitpid(pid, options)
        if result[0]:
            reaped.add(result[0])
            signal.raise_signal(signal.SIGTERM)
        return result

    def checked_killpg(pid: int, signal_number: int) -> None:
        assert pid not in reaped, "signalled a reaped leader's group"
        killpg(pid, signal_number)

    delivered: list[int] = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: delivered.append(signum))
    try:
        with monkeypatch.context() as patch:
            patch.setattr("autoform_cli.lean_probe.os.waitpid", signalled_waitpid)
            patch.setattr("autoform_cli.lean_probe.os.killpg", checked_killpg)
            with pytest.raises(SkeletonError, match="interrupted by SIGTERM"):
                _bounded(tmp_path, "pass")
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert reaped
    assert delivered == [signal.SIGTERM]


@pytest.mark.skipif(os.name != "posix", reason="detached-session assertion is POSIX-specific")
def test_bounded_command_finds_a_descendant_that_escapes_its_process_group(
    tmp_path: Path,
) -> None:
    child_pid = tmp_path / "detached-child.pid"
    child_program = (
        "import os, pathlib, sys, time; os.setsid(); time.sleep(0.1); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "ready_fd = int(sys.argv[1]); os.write(ready_fd, b'1'); "
        "os.close(ready_fd); time.sleep(30)"
    )
    parent_program = (
        "import os, subprocess, sys\n"
        "read_fd, write_fd = os.pipe()\n"
        "try:\n"
        f"    subprocess.Popen([sys.executable, '-c', {child_program!r}, str(write_fd)], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(write_fd,))\n"
        "finally:\n"
        "    os.close(write_fd)\n"
        "try:\n"
        "    ready = os.read(read_fd, 1)\n"
        "finally:\n"
        "    os.close(read_fd)\n"
        "if ready != b'1':\n"
        "    raise RuntimeError('detached child did not become ready')\n"
    )

    with pytest.raises(SkeletonError, match="descendant processes"):
        _bounded(tmp_path, parent_program)

    _assert_dies(int(child_pid.read_text(encoding="utf-8")), "detached descendant", "cleanup")


def test_tagged_process_scan_retries_a_transient_system_error(monkeypatch) -> None:
    class Candidate:
        pid = 123456

        def environ(self):
            return {"_AUTOFORM_PROCESS_TOKEN": "owned"}

        def create_time(self):
            return 7.0

    calls = 0

    def process_iter():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SystemError("transient Darwin process state")
        return [Candidate()]

    monkeypatch.setattr(psutil, "process_iter", process_iter)
    descendants = {}

    _remember_tagged_processes("owned", descendants, root_pid=654321)

    assert calls == 2
    assert list(descendants) == [(123456, 7.0)]


def test_tagged_process_scan_fails_closed_after_repeated_system_errors(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda: (_ for _ in ()).throw(SystemError("persistent failure")),
    )

    with pytest.raises(SkeletonError, match="cannot safely inspect"):
        _remember_tagged_processes("owned", {}, root_pid=654321)

    # Cleanup mode must still reach process-group termination rather than
    # allowing a flaky process-table read to mask the original failure.
    _remember_tagged_processes("owned", {}, root_pid=654321, strict=False)


@pytest.mark.parametrize("error", [OSError("scan failed"), psutil.Error("scan failed")])
def test_tagged_process_scan_fails_closed_when_enumeration_remains_unreadable(
    monkeypatch,
    error: BaseException,
) -> None:
    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda: (_ for _ in ()).throw(error),
    )

    with pytest.raises(SkeletonError, match="repeated process-table errors"):
        _remember_tagged_processes("owned", {}, root_pid=654321)

    _remember_tagged_processes("owned", {}, root_pid=654321, strict=False)


def test_tagged_process_scan_does_not_treat_mixed_failures_as_success(
    monkeypatch,
) -> None:
    errors = iter((SystemError("transient state"), OSError("table unreadable")))
    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda: (_ for _ in ()).throw(next(errors)),
    )

    with pytest.raises(SkeletonError, match="repeated process-table errors"):
        _remember_tagged_processes("owned", {}, root_pid=654321)


def test_tagged_process_scan_retries_a_candidate_system_error(monkeypatch) -> None:
    calls = 0

    class Candidate:
        pid = 123456

        def environ(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SystemError("transient process state")
            return {"_AUTOFORM_PROCESS_TOKEN": "owned"}

        def create_time(self):
            return 7.0

    monkeypatch.setattr(psutil, "process_iter", lambda: [Candidate()])
    descendants = {}

    _remember_tagged_processes("owned", descendants, root_pid=654321)

    assert calls == 2
    assert list(descendants) == [(123456, 7.0)]


def test_process_liveness_fails_closed_on_inspection_uncertainty() -> None:
    class Uncertain:
        def is_running(self):
            raise SystemError("transient process state")

    class Gone:
        def is_running(self):
            raise psutil.NoSuchProcess(123456)

    assert _process_is_alive(Uncertain()) is True
    assert _process_is_alive(Gone()) is False


def test_bounded_command_interruption_kills_the_process(tmp_path: Path, monkeypatch) -> None:
    process_pid = tmp_path / "process.pid"
    program = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(process_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    monotonic = time.monotonic
    calls = 0

    def interrupt_after_start() -> float:
        nonlocal calls
        calls += 1
        if calls != 2:
            return monotonic()
        deadline = monotonic() + 5
        while not process_pid.exists() and monotonic() < deadline:
            time.sleep(0.01)
        raise KeyboardInterrupt

    monkeypatch.setattr("autoform_cli.lean_probe.time.monotonic", interrupt_after_start)

    with pytest.raises(KeyboardInterrupt) as interrupted:
        _bounded(tmp_path, program)

    pid = int(process_pid.read_text(encoding="utf-8"))
    deadline = monotonic() + 5
    while psutil.pid_exists(pid) and monotonic() < deadline:
        time.sleep(0.01)
    assert not psutil.pid_exists(pid)
    assert interrupted.type is KeyboardInterrupt


@pytest.mark.skipif(os.name != "posix", reason="termination signals are POSIX-specific")
@pytest.mark.parametrize(
    "signals",
    [
        (signal.SIGTERM,),
        (getattr(signal, "SIGHUP", signal.SIGTERM),),
        (signal.SIGINT, signal.SIGINT),
    ],
    ids=["term", "hup", "double-int"],
)
def test_bounded_command_termination_signal_kills_the_process_group(
    tmp_path: Path, signals: tuple[int, ...]
) -> None:
    pids = tmp_path / "pids"
    code = "lean_probe._run_bounded_command(sys.argv[1:], cwd=Path.cwd(), timeout=60, context='test command')"
    _signal_driver(tmp_path, code, [sys.executable, "-c", _sleeping_probe(pids)], [pids], signals)


def _sleeping_probe(pids: Path) -> str:
    """A command that records its pid and its child's, then outlives any test."""

    return (
        "import os, pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(pids)!r}).write_text(f'{{os.getpid()}} {{child.pid}}'); "
        "time.sleep(60)"
    )


@pytest.mark.parametrize("workers", [2, 1], ids=["both-running", "one-queued"])
def test_probe_pool_interruption_kills_running_probes_and_starts_no_queued_one(
    tmp_path: Path, monkeypatch, workers: int
) -> None:
    pid_files = [tmp_path / "a.pids", tmp_path / "b.pids"]
    interrupted: list[float] = []

    def interrupt_once_the_workers_run(*args, **kwargs):
        deadline = time.monotonic() + 20
        running = pid_files[:workers]
        while not all(path.exists() and len(path.read_text(encoding="utf-8").split()) == 2 for path in running):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        interrupted.append(time.monotonic())
        raise KeyboardInterrupt

    def probe(module: str) -> object:
        return _bounded(tmp_path, _sleeping_probe(pid_files["AB".index(module)]), timeout=60)

    monkeypatch.setattr("autoform_cli.lean_probe._probe_workers", lambda jobs: workers)
    monkeypatch.setattr("autoform_cli.lean_probe.wait", interrupt_once_the_workers_run)

    with pytest.raises(KeyboardInterrupt):
        run_module_probes(["A", "B"], probe)

    # The probes sleep for a minute; the pool must stop them, not wait them out.
    assert time.monotonic() - interrupted[0] < 15
    assert pid_files[1].exists() == (workers == 2)
    _assert_no_survivors(pid_files)


@pytest.mark.skipif(os.name != "posix", reason="termination signals are POSIX-specific")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM], ids=["int", "term"])
def test_probe_pool_termination_signal_kills_every_workers_process_group(tmp_path: Path, signum: int) -> None:
    pid_files = [tmp_path / "a.pids", tmp_path / "b.pids"]
    code = (
        "lean_probe._probe_workers = lambda jobs: jobs; "
        "programs = dict(zip(('A', 'B'), sys.argv[1:])); "
        "lean_probe.run_module_probes(list(programs), lambda module: lean_probe._run_bounded_command("
        "[sys.executable, '-c', programs[module]], cwd=Path.cwd(), timeout=60, context='test probe'))"
    )
    _signal_driver(tmp_path, code, [_sleeping_probe(path) for path in pid_files], pid_files, (signum,))


@pytest.mark.skipif(os.name != "posix", reason="termination signals are POSIX-specific")
def test_a_signal_during_the_probe_pools_wait_is_handled_outside_it(tmp_path: Path, monkeypatch) -> None:
    # Raised inside wait(), the signal could leave a future's lock held and
    # hang the join; the pool must let wait() return, then cancel and join
    # every worker before the signal ends anything.
    pid_file = tmp_path / "a.pids"
    received: list[int] = []
    returned: list[bool] = []
    joined: list[bool] = []

    class Pool(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            super().shutdown(wait=wait, cancel_futures=cancel_futures)
            joined.append(wait)

    def signal_inside_wait(*args, **kwargs):
        if not returned:
            deadline = time.monotonic() + 20
            while not (pid_file.exists() and len(pid_file.read_text(encoding="utf-8").split()) == 2):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            signal.raise_signal(signal.SIGTERM)
        result = wait(*args, **kwargs)
        returned.append(True)
        return result

    def probe(module: str) -> object:
        return _bounded(tmp_path, _sleeping_probe(pid_file), timeout=60)

    monkeypatch.setattr("autoform_cli.lean_probe.wait", signal_inside_wait)
    monkeypatch.setattr("autoform_cli.lean_probe.ThreadPoolExecutor", Pool)
    before = set(threading.enumerate())
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: received.append(signum))
    try:
        with pytest.raises(SkeletonError, match="interrupted by SIGTERM"):
            run_module_probes(["A"], probe)
        workers = [thread for thread in threading.enumerate() if thread not in before and thread.is_alive()]
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert returned == [True]
    assert received == [signal.SIGTERM]
    assert joined == [True]
    assert workers == []
    _assert_no_survivors([pid_file])


def test_a_probe_cancelled_before_its_process_starts_never_starts_it(tmp_path: Path, monkeypatch) -> None:
    # The pool is cancelled after a worker takes the probe and before it
    # starts the process.
    def probe(module: str) -> object:
        _PROBE_POOL.cancelled.set()
        return _bounded(tmp_path, "pass")

    def popen(*args, **kwargs):
        raise AssertionError("a process started after the pool was cancelled")

    monkeypatch.setattr("autoform_cli.lean_probe.subprocess.Popen", popen)

    (result,) = run_module_probes(["A"], probe).values()

    assert isinstance(result, SkeletonError) and result.issues == ("test command was cancelled",)


def test_unreadable_probe_records_are_refused(tmp_path: Path) -> None:
    records = tmp_path / "records.out"
    records.write_bytes(PROBE_MARKER.encode() + b"\xff\n")
    with pytest.raises(SkeletonError, match="^the skeleton probe's records are not valid UTF-8$"):
        _read_probe_records(records, "skeleton probe")
    records.unlink()
    records.mkdir()
    with pytest.raises(SkeletonError, match="^the impact probe's records could not be read: "):
        _read_probe_records(records, "impact probe")


def test_probe_workers_follow_the_cpus_available_to_the_process(monkeypatch) -> None:
    monkeypatch.setattr(os, "process_cpu_count", lambda: 3, raising=False)
    assert _probe_workers(10) == 3
    monkeypatch.delattr(os, "process_cpu_count")
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
    assert _probe_workers(10) == 2
    monkeypatch.delattr(os, "sched_getaffinity")
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    assert _probe_workers(10) == 8
    assert _probe_workers(2) == 2


@pytest.mark.skipif(os.name != "posix", reason="termination signals are POSIX-specific")
def test_bounded_command_finishes_failure_teardown_when_signalled_during_it(
    tmp_path: Path, monkeypatch
) -> None:
    # A timeout starts teardown and SIGTERM arrives as it begins. Cleanup must still
    # reach the child that ignores SIGTERM, then report the timeout and re-deliver.
    parent_pid, child_pid = tmp_path / "parent.pid", tmp_path / "child.pid"

    def publish_pid(path: Path) -> str:
        return (
            f"pathlib.Path({str(path)!r} + '.tmp').write_text(str(os.getpid())); "
            f"os.replace({str(path)!r} + '.tmp', {str(path)!r}); "
        )

    child = (
        "import os, pathlib, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        + publish_pid(child_pid)
        + "time.sleep(60)"
    )
    program = (
        "import os, pathlib, subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        + publish_pid(parent_pid)
        + "time.sleep(60)"
    )
    tree: list[psutil.Process] = []
    monotonic = time.monotonic

    def time_out_once_both_run() -> float:
        if not tree and parent_pid.exists() and child_pid.exists():
            tree.extend(psutil.Process(int(path.read_text(encoding="utf-8"))) for path in (parent_pid, child_pid))
            return monotonic() + 3600
        return monotonic()

    def signal_then_terminate(*args: object, **kwargs: object) -> None:
        signal.raise_signal(signal.SIGTERM)
        _terminate_process_tree(*args, **kwargs)  # type: ignore[arg-type]

    delivered: list[int] = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: delivered.append(signum))
    survivors = tree
    try:
        with monkeypatch.context() as patch:
            patch.setattr("autoform_cli.lean_probe.time.monotonic", time_out_once_both_run)
            patch.setattr("autoform_cli.lean_probe._terminate_process_tree", signal_then_terminate)
            with pytest.raises(SkeletonError) as failure:
                _bounded(tmp_path, program, timeout=60)
        deadline = monotonic() + 5
        while survivors and monotonic() < deadline:
            survivors = [process for process in survivors if _process_is_alive(process)]
            time.sleep(0.01)
        assert len(tree) == 2
        assert not survivors
        assert "test command timed out" in str(failure.value)
        assert delivered == [signal.SIGTERM]
    finally:
        signal.signal(signal.SIGTERM, previous)
        for process in survivors:
            try:
                process.kill()
            except psutil.Error:
                pass


def test_bounded_command_cleanup_reserves_time_and_reuses_final_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    deadlines: list[float] = []

    def report_stuck_readers(readers, *, deadline: float) -> bool:
        deadlines.append(deadline)
        _join_readers(readers, deadline=deadline)
        return False

    monkeypatch.setattr("autoform_cli.lean_probe._join_readers", report_stuck_readers)

    with pytest.raises(SkeletonError, match="output pipes open"):
        _bounded(tmp_path, "pass")

    assert len(deadlines) == 2
    assert deadlines[0] < deadlines[1]
    # The final deadline is fixed before the first join, not renewed after it.
    assert deadlines[1] - deadlines[0] <= _PROCESS_TERMINATION_GRACE * 0.8


def test_probe_freshness_and_execution_have_separate_budgets(tmp_path: Path, monkeypatch, probe: str) -> None:
    # On a Mathlib project the freshness check alone can take minutes, which
    # must not come out of the probe's own budget.
    (tmp_path / "autoform-skeleton-helper.olean").write_bytes(b"")
    calls: list[float] = []

    def fake_run(command, **kwargs):
        calls.append(kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout="probe output", stderr="")

    times = iter((100.0, 101.0))
    monkeypatch.setattr("autoform_cli.lean_probe._run_bounded_command", fake_run)
    monkeypatch.setattr("autoform_cli.lean_probe.time.monotonic", lambda: next(times))

    assert run_probe(probe, tmp_path, timeout=10, freshness_timeout=20, helper=tmp_path) == "probe output"
    assert calls == [20, 9.0]


def test_a_probe_timeout_names_the_flag_that_raises_it(tmp_path: Path, monkeypatch, probe: str) -> None:
    (tmp_path / "autoform-skeleton-helper.olean").write_bytes(b"")
    bounded = _run_bounded_command

    def slow_probe(command, **kwargs):
        return bounded([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr("autoform_cli.lean_probe._check_artifacts_fresh", lambda *args, **kwargs: None)
    monkeypatch.setattr("autoform_cli.lean_probe._run_bounded_command", slow_probe)

    with pytest.raises(SkeletonError) as caught:
        run_probe(probe, tmp_path, timeout=1, helper=tmp_path)
    assert caught.value.issues == (
        "lake env lean timed out after 1 seconds; rerun with --timeout <seconds> for large projects",
    )


def test_probe_records_file_is_held_to_the_output_limit(tmp_path: Path, monkeypatch, probe: str) -> None:
    (tmp_path / "autoform-skeleton-helper.olean").write_bytes(b"")

    def flooding_probe(command, *, env, **kwargs):
        Path(env[PROBE_OUTPUT_ENV]).write_text("x" * 2048, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("autoform_cli.lean_probe._check_artifacts_fresh", lambda *args, **kwargs: None)
    monkeypatch.setattr("autoform_cli.lean_probe._run_bounded_command", flooding_probe)
    monkeypatch.setattr("autoform_cli.lean_probe.DEFAULT_PROBE_OUTPUT_LIMIT", 1024)

    with pytest.raises(SkeletonError, match="1024-byte output limit"):
        run_probe(probe, tmp_path, helper=tmp_path)


def test_probe_requires_an_existing_lake_manifest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("autoform_cli.lean_probe.shutil.which", lambda executable: "/bin/lake")
    probe = render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))

    with pytest.raises(SkeletonError, match="lake-manifest.json is missing"):
        run_probe(probe, tmp_path)


# --------------------------------------------------------------------------- #
# Project layout
# --------------------------------------------------------------------------- #


def test_libraries_and_modules_follow_the_lake_source_directory(tmp_path: Path) -> None:
    project = _project(tmp_path, src_dir="src")

    libraries = lean_libraries(project)

    assert [library.name for library in libraries] == ["Skel"]
    assert libraries[0].src_dir == (project / "src").resolve()
    assert libraries[0].roots == ("Skel",)
    assert module_of(project / "src" / "Skel" / "Defs.lean", libraries) == "Skel.Defs"
    assert module_of(project / "lakefile.toml", libraries) is None
    assert path_of("Skel.Defs", libraries, project) == "src/Skel/Defs.lean"
    assert path_of("Skel.Missing", libraries, project) is None


def test_a_file_no_library_selects_has_no_module(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text(
        'name = "Demo"\n\n[[lean_lib]]\nname = "Demo"\n\n[[lean_lib]]\nname = "Lib"\nglobs = ["Extra.+"]\n',
        encoding="utf-8",
    )

    libraries = lean_libraries(project)

    # Both libraries hold every file under the project, but Lake builds only
    # what their roots and globs make buildable: Lib's glob leaves out its own root.
    assert module_of(project / "Demo" / "Basic.lean", libraries) == "Demo.Basic"
    assert module_of(project / "Extra" / "Sub.lean", libraries) == "Extra.Sub"
    assert module_of(project / "Extra.lean", libraries) is None
    assert module_of(project / "Lib" / "Foo.lean", libraries) is None
    assert module_of(project / "v1" / "Old.lean", libraries) is None


@pytest.mark.parametrize(
    "lakefile",
    [pytest.param('name = "Solo"\n', id="absent"), pytest.param('name = "Solo"\nlean_lib = []\n', id="empty-array")],
)
def test_a_package_without_library_targets_is_its_own_library(tmp_path: Path, lakefile: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text(lakefile, encoding="utf-8")

    (library,) = lean_libraries(project)

    assert (library.name, library.roots) == ("Solo", ("Solo",))
    assert library.src_dir == project.resolve()


@pytest.mark.parametrize(
    ("lakefile", "src_dir"),
    [
        pytest.param(
            'name = "proj"\nsrcDir = "pkgsrc"\n\n[[lean_lib]]\nname = "Proj"\nsrcDir = "lib"\n',
            "pkgsrc/lib",
            id="library",
        ),
        pytest.param('name = "Proj"\nsrcDir = "pkgsrc"\n', "pkgsrc", id="package-only"),
    ],
)
def test_libraries_nest_under_the_package_source_directory(tmp_path: Path, lakefile: str, src_dir: str) -> None:
    project = tmp_path / "project"
    (project / src_dir).mkdir(parents=True)
    (project / "lakefile.toml").write_text(lakefile, encoding="utf-8")
    (project / src_dir / "Proj.lean").write_text("", encoding="utf-8")

    libraries = lean_libraries(project)

    # Lake joins a library's srcDir onto the package's srcDir, not onto the project root.
    assert libraries[0].src_dir == (project / src_dir).resolve()
    assert module_of(project / src_dir / "Proj.lean", libraries) == "Proj"
    assert path_of("Proj", libraries, project) == f"{src_dir}/Proj.lean"


@pytest.mark.parametrize(
    "toml_11",
    [
        pytest.param('leanOptions = { autoImplicit = false, }\n', id="inline-trailing-comma"),
        pytest.param('leanOptions = {\n  autoImplicit = false\n}\n', id="inline-multiline"),
        pytest.param('note = "\\e"\n', id="escape-e"),
        pytest.param('note = "\\x41"\n', id="escape-x"),
    ],
)
def test_lake_toml_1_1_extensions_are_rejected_on_every_python(tmp_path: Path, toml_11: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "Solo"\n' + toml_11, encoding="utf-8")

    # Lake 4.32.2 reads TOML 1.0 and rejects every spelling in this matrix.
    with pytest.raises(SkeletonError, match="cannot parse the Lake configuration"):
        lean_libraries(project)


@pytest.mark.parametrize(
    ("lean_lib", "match"),
    [
        pytest.param("lean_lib = true\n", r"lean_lib targets .*array of tables", id="boolean"),
        pytest.param("lean_lib = false\n", r"lean_lib targets .*array of tables", id="false"),
        pytest.param('lean_lib = "Solo"\n', r"lean_lib targets .*array of tables", id="string"),
        pytest.param(
            '[lean_lib]\nname = "Solo"\nroots = ["Other"]\n',
            r"lean_lib targets .*array of tables",
            id="single-brackets",
        ),
        pytest.param("lean_lib = [1]\n", r"lean_lib targets .*array of tables", id="array-of-int"),
        pytest.param('[[lean_lib]]\nroots = ["Other"]\n', r"lean_lib targets .*has no name", id="nameless"),
    ],
)
def test_a_lean_lib_that_is_not_an_array_is_refused_as_lake_refuses_it(
    tmp_path: Path, lean_lib: str, match: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "Solo"\n' + lean_lib, encoding="utf-8")

    # Lake's TOML loader fails with "expected array", "expected table" or
    # "missing required key: name" (Lake/Load/Toml.lean, decodeTargetDecls).
    with pytest.raises(SkeletonError, match=match):
        lean_libraries(project)


def test_lake_toml_parser_limits_fail_closed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text(
        f'name = "Solo"\nunknown = {"9" * 5_000}\n', encoding="utf-8"
    )

    with pytest.raises(SkeletonError, match="cannot safely parse.*exceeds the parser's limits"):
        lean_libraries(project)


def _project_with_both_lakefiles(tmp_path: Path) -> Path:
    """lakefile.toml builds ``Skel`` from ``src``; lakefile.lean builds a differently laid out library."""

    project = _project(tmp_path, src_dir="src")
    (project / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\n\npackage fromlean\n\n"
        'lean_lib FromLean where\n  srcDir := "lean-src"\n  roots := #[`LeanRoot]\n',
        encoding="utf-8",
    )
    return project


def test_lakefile_lean_wins_when_both_lakefiles_exist(tmp_path: Path, monkeypatch) -> None:
    project = _project_with_both_lakefiles(tmp_path)
    translated: list[Path] = []

    def translate(root: Path) -> bytes:
        translated.append(root)
        return b'name = "fromlean"\n\n[[lean_lib]]\nname = "FromLean"\nsrcDir = "lean-src"\nroots = ["LeanRoot"]\n'

    monkeypatch.setattr("autoform_cli.lean_probe._translate_lakefile", translate)

    (library,) = lean_libraries(project)

    # Lake builds from lakefile.lean when both exist, so its libraries are the project's.
    assert translated == [project.resolve()]
    assert (library.name, library.src_dir, library.roots) == ("FromLean", project.resolve() / "lean-src", ("LeanRoot",))


def test_a_project_with_both_lakefiles_is_refused_without_lake(tmp_path: Path, monkeypatch) -> None:
    project = _project_with_both_lakefiles(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    # Lake ignores lakefile.toml here, so reading it instead would describe the wrong build.
    with pytest.raises(SkeletonError, match="lake is not on PATH"):
        lean_libraries(project)


def test_a_failed_translation_reports_the_error_rather_than_lakes_info_line(tmp_path: Path, monkeypatch) -> None:
    project = _project_with_both_lakefiles(tmp_path)
    error = f"error: {project / 'lakefile.lean'}:7:12: Unknown identifier `nonsense`"
    stderr = "info: [root]: lakefile.lean and lakefile.toml are both present; using lakefile.lean\n" + error + "\n"
    monkeypatch.setattr(shutil, "which", lambda _name: "lake")
    monkeypatch.setattr(
        "autoform_cli.lean_probe._run_bounded_command",
        lambda command, **_options: subprocess.CompletedProcess(command, 1, "", stderr),
    )

    with pytest.raises(SkeletonError) as caught:
        lean_libraries(project)

    assert caught.value.issues == (f"lake translate-config failed: {error}",)


def _project_with_a_package_source_directory(tmp_path: Path) -> Path:
    """lakefile.lean puts the package's sources under ``pkgsrc`` and gives its library no srcDir of its own."""

    project = tmp_path / "project"
    project.mkdir()
    shutil.copy(_SKELETON_FIXTURE / "lean-toolchain", project / "lean-toolchain")
    (project / "lakefile.lean").write_text(
        'import Lake\nopen Lake DSL\n\npackage proj where\n  srcDir := "pkgsrc"\n\nlean_lib Proj\n', encoding="utf-8"
    )
    return project


def test_lakefile_lean_libraries_nest_under_the_package_source_directory(tmp_path: Path, monkeypatch) -> None:
    project = _project_with_a_package_source_directory(tmp_path)
    # lake translate-config writes the package's srcDir as a top-level key.
    monkeypatch.setattr(
        "autoform_cli.lean_probe._translate_lakefile",
        lambda _root: b'name = "proj"\nsrcDir = "pkgsrc"\n\n[[lean_lib]]\nname = "Proj"\n',
    )

    (library,) = lean_libraries(project)

    assert (library.name, library.src_dir) == ("Proj", project.resolve() / "pkgsrc")


def test_a_project_without_a_lakefile_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SkeletonError):
        lean_libraries(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="named pipes are POSIX-specific")
def test_lake_configuration_snapshot_rejects_a_named_pipe_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "lakefile.toml")

    with pytest.raises(SkeletonError, match="not a regular file"):
        lean_libraries(tmp_path)


def test_lake_configuration_snapshot_uses_content_not_file_identity(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    fstat = os.fstat
    calls = 0

    def unstable_file_identity(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        values = list(fstat(descriptor))
        values[1] += calls
        return os.stat_result(values)

    monkeypatch.setattr("autoform_cli.lean_probe.os.fstat", unstable_file_identity)

    (library,) = lean_libraries(project)

    assert library.name == "Skel"


def test_project_control_snapshot_retains_only_fingerprints(tmp_path: Path) -> None:
    project = _project(tmp_path)
    content = (project / "lakefile.toml").read_bytes()

    snapshot = dict(project_control_snapshot(project))

    assert snapshot["lakefile.toml"] == (len(content), hashlib.sha256(content).hexdigest())
    assert snapshot["lakefile.lean"] is None


def test_lake_configuration_snapshot_rejects_content_changed_between_reads(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    lakefile = project / "lakefile.toml"
    open_file = os.open
    reads = 0

    def change_before_second_read(path, flags, *args):
        nonlocal reads
        if Path(path) == lakefile:
            reads += 1
            if reads == 2:
                lakefile.write_text('name = "Changed"\n', encoding="utf-8")
        return open_file(path, flags, *args)

    monkeypatch.setattr("autoform_cli.lean_probe.os.open", change_before_second_read)

    with pytest.raises(SkeletonError, match="changed while it was read"):
        lean_libraries(project)


# --------------------------------------------------------------------------- #
# Probe failure reasons
# --------------------------------------------------------------------------- #


def test_a_stable_reason_leaves_paths_beside_the_project_as_lean_printed_them(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    text = f"{root}/Skel/A.lean:1:0 {root}-old/A.lean {root}ect/A.lean {root}.bak/A.lean {root}: {root}"
    assert stable_paths(text, root) == f"Skel/A.lean:1:0 {root}-old/A.lean {root}ect/A.lean {root}.bak/A.lean .: ."


def test_a_helper_build_that_times_out_states_its_budget_once(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "lake-manifest.json").write_text("{}\n", encoding="utf-8")

    def stall(command, *, timeout, context, **kwargs):
        raise _CommandTimedOut([f"{context} timed out after {timeout:g} seconds"])

    monkeypatch.setattr("autoform_cli.lean_probe.shutil.which", lambda executable: "/bin/lake")
    monkeypatch.setattr("autoform_cli.lean_probe._run_bounded_command", stall)

    with pytest.raises(_ProbeEnvironmentError) as caught:
        _build_probe_helper(tmp_path, tmp_path / "helper", timeout=5)
    assert caught.value.issues == (
        "building the skeleton probe helpers timed out after 5 seconds; "
        "rerun with --timeout <seconds> for large projects",
    )


# --------------------------------------------------------------------------- #
# The real probe
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_lakefile_lean_wins_through_lake_translate_config(tmp_path: Path) -> None:
    project = _project_with_both_lakefiles(tmp_path)

    (library,) = lean_libraries(project)

    assert (library.name, library.src_dir, library.roots) == ("FromLean", project.resolve() / "lean-src", ("LeanRoot",))


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_package_source_directory_nests_libraries_through_lake_translate_config(tmp_path: Path) -> None:
    project = _project_with_a_package_source_directory(tmp_path)

    (library,) = lean_libraries(project)

    assert (library.name, library.src_dir) == ("Proj", project.resolve() / "pkgsrc")


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_probe_stops_before_its_records_file_exceeds_the_limit(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    _build(project, "Skel.Main")
    limit = 1024
    sizes: list[int] = []
    bounded = _run_bounded_command

    def inspect_records(command, **kwargs):
        result = bounded(command, **kwargs)
        if kwargs.get("context") == "lake env lean":
            sizes.append(Path(kwargs["env"][PROBE_OUTPUT_ENV]).stat().st_size)
        return result

    monkeypatch.setattr("autoform_cli.lean_probe.DEFAULT_PROBE_OUTPUT_LIMIT", limit)
    monkeypatch.setattr("autoform_cli.lean_probe._run_bounded_command", inspect_records)
    probe = render_probe(
        imports=("Skel.Main",), roots=("Skel.observation_determined",), project_roots=("Skel",)
    )

    with pytest.raises(SkeletonError, match=f"{limit}-byte output limit"):
        run_probe(probe, project)
    assert sizes and sizes[0] <= limit
