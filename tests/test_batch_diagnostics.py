"""Batch diagnostics: one ``lake lean`` run per call, verified dead afterwards."""

from __future__ import annotations

import json
import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import psutil
import pytest

from servers import batch_diagnostics as batch
from servers.lean_runtime import DEFAULT_LSP_TIMEOUT, LeanRuntimeServices
from servers.lsp import server as lsp
from tests.lean_fixture import _real_lean_project, _requires_real_lean
from tests.runtime_fixture import make_lake_project, runtime_config

_CLEAN = "No diagnostics — file compiles cleanly."


def _fake_lake(tmp_path: Path, body: str) -> tuple[str, ...]:
    """Return a command that runs ``body`` with the target file as ``target``."""
    script = tmp_path / "fake_lake.py"
    script.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "target = Path(sys.argv[1])\n"
        "assert sys.argv[2:] == ['--', '--json', '-DmaxErrors=0'], sys.argv\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    return (sys.executable, str(script))


def _lean_file(tmp_path: Path, text: str = "theorem t : True := trivial\n") -> Path:
    path = tmp_path / "Probe.lean"
    path.write_text(text, encoding="utf-8")
    return path


def _message(severity: str, line: int, column: int, data: str) -> str:
    return json.dumps({"severity": severity, "pos": {"line": line, "column": column}, "data": data})


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_a_silent_successful_run_is_clean(tmp_path):
    command = _fake_lake(tmp_path, "sys.exit(0)\n")
    diagnostics = batch.batch_diagnostics(tmp_path, _lean_file(tmp_path), command=command, timeout=30)

    assert diagnostics == []
    assert lsp.format_lsp_diagnostics(diagnostics) == _CLEAN


def test_messages_become_language_server_diagnostics(tmp_path):
    path = _lean_file(tmp_path, "theorem t : True := trivial\ndef «𝓝x» : Nat := \"y\"\n")
    lines = [
        _message("error", 2, 18, "Type mismatch"),
        _message("warning", 1, 8, "declaration uses `sorry`"),
        _message("information", 1, 0, "2"),
    ]
    command = _fake_lake(tmp_path, f"print({chr(10).join(lines)!r})\nsys.exit(1)\n")

    diagnostics = batch.batch_diagnostics(tmp_path, path, command=command, timeout=30)

    # Lean counts lines from 1 and columns in code points; the language server
    # counts lines from 0 and columns in UTF-16 units, so 𝓝 counts twice.
    assert diagnostics == [
        {"severity": 1, "range": {"start": {"line": 1, "character": 19}}, "message": "Type mismatch"},
        {"severity": 2, "range": {"start": {"line": 0, "character": 8}}, "message": "declaration uses `sorry`"},
        {"severity": 3, "range": {"start": {"line": 0, "character": 0}}, "message": "2"},
    ]


def test_unicode_line_separators_stay_inside_their_message(tmp_path):
    text = "one" + chr(0x2028) + "two" + chr(0x85) + "three"
    # Lean's JSON printer leaves these characters unescaped.
    line = json.dumps({"severity": "error", "pos": {"line": 1, "column": 0}, "data": text}, ensure_ascii=False)
    command = _fake_lake(tmp_path, f"sys.stdout.buffer.write({(line + chr(10)).encode()!r})\nsys.exit(1)\n")

    diagnostics = batch.batch_diagnostics(tmp_path, _lean_file(tmp_path), command=command, timeout=30)

    assert [item["message"] for item in diagnostics] == [text]


def test_stale_imports_are_reported_instead_of_built(tmp_path):
    command = _fake_lake(
        tmp_path,
        """
        sys.stderr.write("error: target is out-of-date and needs to be rebuilt\\n")
        sys.stderr.write("Some required targets logged failures:\\n- Skel.Defs\\n")
        sys.exit(3)
        """,
    )

    with pytest.raises(batch.BatchDiagnosticsError, match="imports are not built") as raised:
        batch.batch_diagnostics(tmp_path, _lean_file(tmp_path), command=command, timeout=30)

    assert "Build the modules that Probe.lean imports" in str(raised.value)
    assert str(raised.value).endswith("- Skel.Defs")


def test_a_lake_failure_without_a_lean_error_is_never_clean(tmp_path):
    command = _fake_lake(
        tmp_path,
        """
        print(json.dumps({"severity": "warning", "pos": {"line": 1, "column": 0}, "data": "w"}))
        sys.stderr.write("boom\\n")
        sys.exit(1)
        """,
    )

    with pytest.raises(batch.BatchDiagnosticsError, match="stopped with status 1 before Lean finished") as raised:
        batch.batch_diagnostics(tmp_path, _lean_file(tmp_path), command=command, timeout=30)

    assert raised.value.args[0].endswith("boom")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("print('not json')", "not a JSON message"),
        ("print('[1, 2]')", "not a message"),
        (f"print({_message('fatal', 1, 0, 'x')!r})", "unknown severity"),
        (f"print({json.dumps({'severity': 'error', 'data': 'no position'})!r})", "no position"),
        ("target.write_text('changed', encoding='utf-8')", "changed while Lean checked it"),
        (f"print({_message('error', 1, 0, 'first error')!r})\nsys.exit(137)", "status 137 before Lean finished"),
    ],
)
def test_output_that_cannot_be_trusted_fails_closed(tmp_path, body, match):
    command = _fake_lake(tmp_path, body + "\n")

    with pytest.raises(batch.BatchDiagnosticsError, match=match):
        batch.batch_diagnostics(tmp_path, _lean_file(tmp_path), command=command, timeout=30)


def test_a_timeout_stops_the_whole_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    command = _fake_lake(
        tmp_path,
        f"""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        Path({str(pid_file)!r}).write_text(str(child.pid))
        sys.stderr.write("still elaborating\\n")
        sys.stderr.flush()
        time.sleep(600)
        """,
    )

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="LEAN_LSP_TIMEOUT") as raised:
        batch.batch_diagnostics(tmp_path, _lean_file(tmp_path), command=command, timeout=2)

    assert time.monotonic() - started < 30
    assert "Lean did not finish checking Probe.lean within 2s" in str(raised.value)
    assert str(raised.value).endswith("still elaborating")
    assert _gone(int(pid_file.read_text()))


def test_processes_left_behind_after_a_normal_exit_are_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "EXIT_GRACE_SECONDS", 0.2)
    pid_file = tmp_path / "child.pid"
    command = _fake_lake(
        tmp_path,
        f"""
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        Path({str(pid_file)!r}).write_text(str(child.pid))
        sys.exit(0)
        """,
    )

    assert batch.batch_diagnostics(tmp_path, _lean_file(tmp_path), command=command, timeout=30) == []
    assert _gone(int(pid_file.read_text()))


def test_a_failed_cleanup_attempt_is_retried(tmp_path, monkeypatch):
    calls = []
    real = batch._kill_subprocesses

    def flaky(process, process_group_id):
        calls.append(process_group_id)
        if len(calls) == 1:
            raise RuntimeError("timed out terminating the Lean process group")
        real(process, process_group_id)

    monkeypatch.setattr(batch, "_kill_subprocesses", flaky)
    command = _fake_lake(tmp_path, "sys.exit(0)\n")

    assert batch.batch_diagnostics(tmp_path, _lean_file(tmp_path), command=command, timeout=30) == []
    assert len(calls) == 2


def test_lakes_temporary_files_go_with_the_call(tmp_path):
    seen = tmp_path / "scratch"
    command = _fake_lake(
        tmp_path,
        f"""
        scratch = Path(os.environ["TMPDIR"])
        (scratch / "tmp.setup").write_text("{{}}")
        Path({str(seen)!r}).write_text(str(scratch))
        sys.exit(0)
        """,
    )

    assert batch.batch_diagnostics(tmp_path, _lean_file(tmp_path), command=command, timeout=30) == []
    assert not Path(seen.read_text()).exists()


def test_output_over_the_limit_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "MAX_STDOUT_BYTES", 1000)
    command = _fake_lake(tmp_path, "sys.stdout.write('x' * 5000)\nsys.stdout.flush()\ntime.sleep(600)\n")

    with pytest.raises(batch.BatchDiagnosticsError, match="more than 1000 bytes"):
        batch.batch_diagnostics(tmp_path, _lean_file(tmp_path), command=command, timeout=30)


def test_only_the_end_of_lakes_output_is_kept(tmp_path):
    command = _fake_lake(
        tmp_path,
        """
        sys.stderr.write("replayed build log\\n" * 200000)
        sys.stderr.write("LAST LINE\\n")
        sys.exit(2)
        """,
    )

    with pytest.raises(batch.BatchDiagnosticsError) as raised:
        batch.batch_diagnostics(tmp_path, _lean_file(tmp_path), command=command, timeout=60)

    tail = str(raised.value).split("Lake's output ended with:\n", 1)[1]
    assert tail.endswith("LAST LINE")
    assert len(tail) <= batch.STDERR_TAIL_BYTES


def _project(tmp_path: Path) -> Path:
    project = make_lake_project(tmp_path, "probe")
    _lean_file(project)
    return project


def _services(**config: object) -> LeanRuntimeServices:
    def no_sessions(_root):
        raise AssertionError("batch diagnostics must not start a language server")

    return LeanRuntimeServices(runtime_config(**config), lsp_factory=no_sessions, start_sweepers=False)


def _check(services: LeanRuntimeServices, project: Path) -> object:
    try:
        return services.dispatch("lsp.diagnostics", {"project_dir": str(project), "file_path": "Probe.lean"})
    except Exception as error:
        return error


def _run_checks(services: LeanRuntimeServices, project: Path, count: int) -> list[object]:
    results: list[object] = []
    threads = [threading.Thread(target=lambda: results.append(_check(services, project))) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


def test_at_most_max_projects_checks_run_at_once(tmp_path):
    project = _project(tmp_path)
    marker = tmp_path / "running"
    command = _fake_lake(
        tmp_path,
        f"""
        marker = Path({str(marker)!r})
        fd = os.open(marker, os.O_CREAT | os.O_EXCL)
        time.sleep(0.5)
        os.close(fd)
        marker.unlink()
        """,
    )
    services = _services(diagnostics_command=command, max_projects=1)
    try:
        results = _run_checks(services, project, 3)
    finally:
        services.close()

    assert results == [_CLEAN] * 3


def test_time_waiting_for_a_slot_counts_against_lean_lsp_timeout(tmp_path, monkeypatch):
    project = _project(tmp_path)
    timeouts: list[float] = []
    budgets: list[float] = []

    def check(_root, _path, *, command, timeout, budget):
        timeouts.append(timeout)
        budgets.append(budget)
        time.sleep(1.0)
        return []

    monkeypatch.setattr("servers.lean_runtime.batch_diagnostics", check)
    services = _services(max_projects=1, lsp_timeout=3.0)
    try:
        results = _run_checks(services, project, 2)
    finally:
        services.close()

    assert results == [_CLEAN] * 2
    # The second call waited about 1 s for the slot, so it got that much less,
    # while its timeout message still states the whole LEAN_LSP_TIMEOUT.
    assert max(timeouts) > 2.5
    assert min(timeouts) < 2.5
    assert budgets == [3.0, 3.0]


def test_a_slot_that_never_frees_fails_within_lean_lsp_timeout(tmp_path, monkeypatch):
    project = _project(tmp_path)

    def check(_root, _path, *, command, timeout, budget):
        time.sleep(1.5)
        return []

    monkeypatch.setattr("servers.lean_runtime.batch_diagnostics", check)
    services = _services(max_projects=1, lsp_timeout=1.0)
    try:
        results = _run_checks(services, project, 2)
    finally:
        services.close()

    assert _CLEAN in results
    [failure] = [result for result in results if isinstance(result, TimeoutError)]
    assert str(failure) == "timed out after 1s (LEAN_LSP_TIMEOUT) waiting for a free Lean slot"


def test_a_group_that_outlives_cleanup_keeps_its_slot_until_it_is_gone(tmp_path, monkeypatch):
    real = batch._kill_subprocesses
    stopped = threading.Event()

    def stubborn(process, process_group_id):
        if not stopped.is_set():
            raise RuntimeError("timed out terminating the Lean process group")
        real(process, process_group_id)

    monkeypatch.setattr(batch, "_kill_subprocesses", stubborn)
    project = _project(tmp_path)
    services = _services(diagnostics_command=_fake_lake(tmp_path, "sys.exit(0)\n"), max_projects=1, lsp_timeout=1.0)
    try:
        assert isinstance(_check(services, project), batch.BatchCleanupError)
        # The group may still be alive, so the next call cannot take its slot.
        assert "waiting for a free Lean slot" in str(_check(services, project))

        stopped.set()
        [reaper] = services._batch_reapers
        reaper.join(timeout=10)
        assert _check(services, project) == _CLEAN
    finally:
        stopped.set()
        services.close()


# --------------------------------------------------------------------------- #
# Real Lean
# --------------------------------------------------------------------------- #

_PROBE = """\
theorem good : 1 + 1 = 2 := rfl

-- 𝓝 sits before the error column on the last line.
def bad : Nat := "not a nat"

#eval (2 : Nat)

theorem warns (h : True) : True := by
  sorry

def «𝓝x» : Nat := "astral before the error column"
"""

_PROBE_DIAGNOSTICS = """\
Diagnostics: 2 error(s), 1 warning(s)
4:17: error: Type mismatch
  "not a nat"
has type
  String
but is expected to have type
  Nat
6:0: info: 2
8:8: warning: declaration uses `sorry`
11:19: error: Type mismatch
  "astral before the error column"
has type
  String
but is expected to have type
  Nat"""

_LAKE_LEAN = ("lake", "--no-build", "lean")


@_requires_real_lean
def test_real_lean_batch_diagnostics_render_the_probe(tmp_path):
    project = _real_lean_project(tmp_path / "probe", _PROBE, Clean="theorem ok : True := trivial\n")

    diagnostics = batch.batch_diagnostics(project, project / "Probe" / "Main.lean", command=_LAKE_LEAN, timeout=300)
    clean = batch.batch_diagnostics(project, project / "Probe" / "Clean.lean", command=_LAKE_LEAN, timeout=300)

    assert lsp.format_lsp_diagnostics(diagnostics) == _PROBE_DIAGNOSTICS
    assert clean == []


@_requires_real_lean
def test_real_lean_reports_unbuilt_imports_without_building_them(tmp_path):
    project = _real_lean_project(
        tmp_path / "stale", "import Probe.Mid\n", Mid="import Probe.Dep\n", Dep="def dep : Nat := 1\n"
    )

    with pytest.raises(batch.BatchDiagnosticsError, match="imports are not built") as raised:
        batch.batch_diagnostics(project, project / "Probe" / "Main.lean", command=_LAKE_LEAN, timeout=300)

    # Lake lists only the first module to build, not the ones that import it.
    assert str(raised.value).endswith("- Probe.Dep")
    assert "- Probe.Mid" not in str(raised.value)
    assert not list((project / ".lake").rglob("*.olean"))


@_requires_real_lean
def test_real_lean_batch_diagnostics_through_a_mathlib_import():
    root = os.environ.get("AUTOFORM_REAL_LEAN_MATHLIB_PROJECT")
    if not root:
        if os.environ.get("AUTOFORM_REQUIRE_REAL_LEAN_TESTS") == "1":
            pytest.fail("AUTOFORM_REAL_LEAN_MATHLIB_PROJECT must name a built Mathlib project")
        pytest.skip("needs AUTOFORM_REAL_LEAN_MATHLIB_PROJECT, a built Mathlib project")
    project = Path(root).resolve()
    probe = project / "src" / "GeneratedProject" / "BatchDiagnosticsProbe.lean"
    created = not probe.parent.exists()
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("import Mathlib\nexample : (2 : Nat) + 2 = 5 := rfl\n", encoding="utf-8")
    try:
        # The shipped LEAN_LSP_TIMEOUT default, so real Lean is checked against it.
        diagnostics = batch.batch_diagnostics(project, probe, command=_LAKE_LEAN, timeout=DEFAULT_LSP_TIMEOUT)
    finally:
        probe.unlink(missing_ok=True)
        if created:
            probe.parent.rmdir()

    assert [item["range"]["start"]["line"] for item in diagnostics if item["severity"] == 1] == [1]
