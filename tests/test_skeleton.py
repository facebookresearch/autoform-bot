from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
import psutil

from autoform_cli.__main__ import main
from autoform_cli.lean import PACKET_SCHEMA, PASSAGE_SCHEMA, index_project
from autoform_cli.skeleton import (
    DeclarationSkeleton,
    NodeSkeleton,
    PACKET_MANIFEST,
    PROBE_MARKER,
    PROBE_OUTPUT_ENV,
    SEMANTIC_SCHEMA,
    SKELETON_SCHEMA,
    SkeletonReport,
    TrustedDeclaration,
    SkeletonError,
    UnresolvedTarget,
    _declaration,
    _install_output,
    _join_readers,
    _remove_output,
    _rename_no_replace,
    _run_bounded_command,
    _replace_outputs,
    _stage_output,
    _hash_module_files,
    _local_safety_issue,
    _without_comments,
    _probe_modules,
    _probe_record_issue,
    extract_skeletons,
    format_report,
    lean_libraries,
    load_skeleton_report,
    module_of,
    parse_probe_output,
    path_of,
    render_probe,
    run_probe,
    write_packets,
    write_skeleton_report,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "skeleton-project"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _blueprint(root: Path, *, lean: dict[str, str]) -> Path:
    """Write a vault whose articles name the given ``lean:`` declarations."""

    blueprint = root / "blueprint"
    chapter = blueprint / "roadmap" / "basics"
    chapter.mkdir(parents=True)
    (blueprint / "roadmap" / "README.md").write_text("# Roadmap\n", encoding="utf-8")
    (chapter / "README.md").write_text("# Basics\n", encoding="utf-8")
    for stem, names in lean.items():
        (chapter / f"{stem}.md").write_text(
            f"---\ndeclaration: theorem\nlean: {names}\n---\n\n# {stem}\n\nA statement.\n\n"
            "## Depends on\n\nNone.\n",
            encoding="utf-8",
        )
    coverage = blueprint / "coverage"
    coverage.mkdir()
    (coverage / "README.md").write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n| All | OUT | scratch |\n",
        encoding="utf-8",
    )
    return blueprint


def _project(root: Path, *, src_dir: str = ".") -> Path:
    """Write an unbuilt copy of the fixture project, optionally under ``src_dir``."""

    project = root / "project"
    project.mkdir()
    shutil.copy(_FIXTURE / "lean-toolchain", project / "lean-toolchain")
    lakefile = (_FIXTURE / "lakefile.toml").read_text(encoding="utf-8")
    if src_dir != ".":
        lakefile += f'srcDir = "{src_dir}"\n'
    (project / "lakefile.toml").write_text(lakefile, encoding="utf-8")
    source_root = project / src_dir
    shutil.copytree(_FIXTURE / "Skel", source_root / "Skel")
    shutil.copy(_FIXTURE / "Skel.lean", source_root / "Skel.lean")
    return project


def _record(root: str, **fields: object) -> str:
    return PROBE_MARKER + json.dumps({"root": root, **fields})


def _probe_lines(record: dict[str, object]) -> str:
    """Encode a resolved probe record as the probe prints it: shared entries, then the root."""

    if record.get("found") is not True:
        return PROBE_MARKER + json.dumps(record)
    entries: dict[tuple[str, str], object] = {}
    wire = dict(record, semantic=[record["semantic"]])
    for item in wire["trusted"]:
        entries["trusted", item["name"]] = dict(item, semantic=[item["semantic"]])
    wire["trusted"] = [item["name"] for item in wire["trusted"]]
    for name, semantic in [*wire.pop("assumed_semantics"), *wire.pop("axiom_semantics")]:
        entries["semantic", name] = [semantic]
    for module, kind, path in wire["boundary_modules"]:
        entries.setdefault(("module", module), []).append([kind, path])
    wire["boundary_modules"] = list(dict.fromkeys(module for module, _, _ in wire["boundary_modules"]))
    lines = [
        PROBE_MARKER + json.dumps({"table": table, "name": name, "value": value})
        for (table, name), value in entries.items()
    ]
    return "\n".join([*lines, PROBE_MARKER + json.dumps(wire)])


def _semantic(payload: dict[str, object]) -> str:
    return json.dumps(
        {"generated": [], "root": {"safety": "safe", **payload}},
        separators=(",", ":"),
    )


def _leading_doc(text: str) -> list[list[int]]:
    """The comment ranges Lean reports for text that opens with one docstring."""

    return [[0, len(text[: text.index("-/") + 2].encode("utf-8"))]]


def _fake_probe_output(*, include_ghost: bool = False) -> str:
    """What the probe says about the fixture, as captured from a real run."""

    eligible = {
        "name": "Skel.Eligible",
        "source_name": "Skel.Eligible",
        "kind": "def",
        "module": "Skel.Defs",
        "range": [5, 6],
        "signature": "Skel.Eligible {Y : Type} (S : Y → Prop) (y : Y) : Prop",
        "raw_signature": "Skel.Eligible {Y : Type} (S : Y -> Prop) (y : Y) : Prop",
        "semantic_schema": SEMANTIC_SCHEMA,
        "semantic": _semantic({"type": {"sort": {"zero": None}}, "value": {"bvar": 0}}),
        "depends": [],
        "source": "/-- A weak observation admits a label. -/\ndef Eligible (S : Y → Prop) (y : Y) : Prop := S y",
    }
    eligible["source_comments"] = _leading_doc(eligible["source"])
    non_ambiguous = {
        "name": "Skel.NonAmbiguous",
        "source_name": "Skel.NonAmbiguous",
        "kind": "def",
        "module": "Skel.Defs",
        "range": [8, 10],
        "signature": "Skel.NonAmbiguous {Y : Type} (S : Y → Prop) : Prop",
        "raw_signature": "Skel.NonAmbiguous {Y : Type} (S : Y -> Prop) : Prop",
        "semantic_schema": SEMANTIC_SCHEMA,
        "semantic": _semantic({"type": {"sort": {"zero": None}}, "value": {"bvar": 1}}),
        "depends": ["Skel.Eligible"],
        "source": (
            "/-- At most one label is admitted. -/\n"
            "def NonAmbiguous (S : Y → Prop) : Prop :=\n"
            "  ∀ y z : Y, Eligible S y → Eligible S z → y = z"
        ),
    }
    non_ambiguous["source_comments"] = _leading_doc(non_ambiguous["source"])
    observation = {
        "name": "Skel.Observation",
        "source_name": "Skel.Observation",
        "kind": "structure",
        "module": "Skel.Defs",
        "range": [15, 18],
        "signature": "Skel.Observation (Y : Type) : Type",
        "raw_signature": "Skel.Observation (Y : Type) : Type",
        "semantic_schema": SEMANTIC_SCHEMA,
        "semantic": _semantic({"type": {"sort": {"zero": None}}, "constructors": []}),
        "depends": [],
        "source": (
            "/-- A structure, to check inductive handling. -/\n"
            "structure Observation (Y : Type) where\n"
            "  admits : Y → Prop\n"
            "  nonempty : ∃ y, admits y"
        ),
    }
    observation["source_comments"] = _leading_doc(observation["source"])
    statement = (
        "/-- Uses a structure in its statement, and sorry in its proof. -/\n"
        "theorem observation_determined (o : Observation Y) (h : NonAmbiguous o.admits) :\n"
        "    ∃ y, o.admits y ∧ ∀ z, o.admits z → z = y"
    )
    records = [
        "some unrelated line from Lean",
        _found_record(
            "Skel.observation_determined",
            found=True,
            kind="theorem",
            module="Skel.Main",
            range=[14, 17],
            signature="Skel.observation_determined {Y : Type} (o : Skel.Observation Y) :\n  ∃ y, o.admits y",
            raw_signature=(
                "Skel.observation_determined {Y : Type} (o : Skel.Observation Y) :\n  Exists fun y => o.admits y"
            ),
            semantic_schema=SEMANTIC_SCHEMA,
            semantic=_semantic({"type": {"sort": {"zero": None}}}),
            lean_version="4.32.2",
            source=None,
            source_comments=None,
            statement_source=statement,
            statement_comments=_leading_doc(statement),
            depends=["Skel.NonAmbiguous", "Skel.Observation"],
            # Deliberately out of dependency order: the report must sort them.
            trusted=[non_ambiguous, observation, eligible],
            assumed=["Mathlib.Fake"],
            assumed_semantics=[["Mathlib.Fake", _semantic({"type": {"sort": {"zero": None}}})]],
            boundary_modules=[["Mathlib.Fake", "olean", "Skel/Defs.lean"]],
            axioms=["sorryAx"],
            axiom_semantics=[["sorryAx", _semantic({"type": {"sort": {"zero": None}}})]],
        ),
    ]
    if include_ghost:
        records.append(_record("Skel.ghost", found=False))
    return "\n".join(records)


def _found_record(root: str, **fields: object) -> str:
    return _probe_lines({"root": root, **fields})


def _fake_found_record() -> dict[str, object]:
    return parse_probe_output(_fake_probe_output())["Skel.observation_determined"]


# --------------------------------------------------------------------------- #
# The probe program
# --------------------------------------------------------------------------- #


def test_probe_spells_names_without_trusting_lean_to_parse_them() -> None:
    probe = render_probe(
        imports=("Skel.Main", "Skel.Defs", "Skel.Main"),
        roots=("Skel.observation_determined",),
        project_roots=("Skel",),
    )

    assert probe.startswith("import Skel.Defs\nimport Skel.Main\n")
    assert 'Name.str (Name.str (Name.anonymous) "Skel") "observation_determined"' in probe
    assert f'"{PROBE_MARKER}' in probe
    assert "def probeOutputLimit : Nat := 67108864" in probe
    assert 'Name.str (Name.anonymous) "Init"' in probe
    assert "info.fromClass" in probe
    assert "privateToUserName c" in probe


def test_probe_transports_quoted_and_numeric_name_components_structurally() -> None:
    probe = render_probe(
        imports=("Skel.Main",),
        roots=("Skel.«quoted.name with space».2",),
        project_roots=("Skel",),
    )

    assert '"Skel.«quoted.name with space».2"' in probe
    assert 'Name.str (Name.str (Name.anonymous) "Skel") "quoted.name with space"' in probe
    assert "Name.num (Name.str" in probe


def test_probe_refuses_to_render_nothing() -> None:
    with pytest.raises(SkeletonError):
        render_probe(imports=("Skel",), roots=(), project_roots=("Skel",))
    with pytest.raises(SkeletonError):
        render_probe(imports=(), roots=("Skel.x",), project_roots=("Skel",))


def test_probe_refuses_stale_artifacts_before_executing_lean(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    (tmp_path / "lake-manifest.json").write_text("{}\n", encoding="utf-8")

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 3, stdout="target is out-of-date", stderr="")

    monkeypatch.setattr("autoform_cli.skeleton.shutil.which", lambda executable: "/bin/lake")
    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", fake_run)
    probe = render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))

    with pytest.raises(SkeletonError, match="build artifacts are stale"):
        run_probe(probe, tmp_path)

    assert calls == [["/bin/lake", "--rehash", "--no-build", "build", "Skel.Main"]]


def test_probe_reports_a_failed_freshness_check_apart_from_stale_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "lake-manifest.json").write_text("{}\n", encoding="utf-8")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, stdout="error: permission denied (error code: 13)", stderr=""
        )

    monkeypatch.setattr("autoform_cli.skeleton.shutil.which", lambda executable: "/bin/lake")
    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", fake_run)
    probe = render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))

    with pytest.raises(SkeletonError, match="must be writable") as refused:
        run_probe(probe, tmp_path)

    assert "stale" not in str(refused.value)
    assert "permission denied" in str(refused.value)


def test_probe_semantic_schema_matches_the_python_reader() -> None:
    probe = render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))

    assert re.findall(r'^def semanticSchema := "([^"]*)"$', probe, re.MULTILINE) == [SEMANTIC_SCHEMA]


def test_bounded_command_rejects_excess_output(tmp_path: Path) -> None:
    with pytest.raises(SkeletonError, match="1024-byte output limit"):
        _run_bounded_command(
            [sys.executable, "-c", "import os; os.write(1, b'x' * 4096)"],
            cwd=tmp_path,
            timeout=10,
            context="test command",
            output_limit=1024,
        )


def test_bounded_command_caps_stdout_and_stderr_together(tmp_path: Path) -> None:
    program = "import os; os.write(1, b'x' * 700); os.write(2, b'y' * 700)"
    with pytest.raises(SkeletonError, match="1024-byte output limit"):
        _run_bounded_command(
            [sys.executable, "-c", program],
            cwd=tmp_path,
            timeout=10,
            context="test command",
            output_limit=1024,
        )


def test_bounded_command_rejects_invalid_utf8(tmp_path: Path) -> None:
    with pytest.raises(SkeletonError, match="invalid UTF-8"):
        _run_bounded_command(
            [sys.executable, "-c", "import os; os.write(1, b'\\xff')"],
            cwd=tmp_path,
            timeout=10,
            context="test command",
        )


def test_bounded_command_timeout_kills_descendants(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    program = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )

    with pytest.raises(SkeletonError, match="timed out"):
        _run_bounded_command(
            [sys.executable, "-c", program],
            cwd=tmp_path,
            timeout=2,
            context="test command",
        )

    pid = int(child_pid.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            child = psutil.Process(pid)
            if not child.is_running() or child.status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"descendant process {pid} survived command timeout")


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
        _run_bounded_command(
            [sys.executable, "-c", program],
            cwd=tmp_path,
            timeout=10,
            context="test command",
        )
    pid = int(child_pid.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            child = psutil.Process(pid)
            if not child.is_running() or child.status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"descendant process {pid} survived successful parent exit")


@pytest.mark.skipif(os.name != "posix", reason="detached-session assertion is POSIX-specific")
def test_bounded_command_finds_a_descendant_that_escapes_its_process_group(
    tmp_path: Path,
) -> None:
    child_pid = tmp_path / "detached-child.pid"
    child_program = (
        "import os, pathlib, time; os.setsid(); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    parent_program = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_program!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    )

    with pytest.raises(SkeletonError, match="descendant processes"):
        _run_bounded_command(
            [sys.executable, "-c", parent_program],
            cwd=tmp_path,
            timeout=10,
            context="test command",
        )

    pid = int(child_pid.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            child = psutil.Process(pid)
            if not child.is_running() or child.status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"detached descendant process {pid} survived cleanup")


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
        if calls == 1:
            return monotonic()
        deadline = monotonic() + 5
        while not process_pid.exists() and monotonic() < deadline:
            time.sleep(0.01)
        raise KeyboardInterrupt

    monkeypatch.setattr("autoform_cli.skeleton.time.monotonic", interrupt_after_start)

    with pytest.raises(KeyboardInterrupt) as interrupted:
        _run_bounded_command(
            [sys.executable, "-c", program],
            cwd=tmp_path,
            timeout=10,
            context="test command",
        )

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
    program = (
        "import os, pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(pids)!r}).write_text(f'{{os.getpid()}} {{child.pid}}'); "
        "time.sleep(60)"
    )
    # Start from a terminal's dispositions even when pytest runs under nohup or in the background.
    driver = (
        "import signal, sys; from pathlib import Path; "
        "signal.signal(signal.SIGHUP, signal.SIG_DFL); "
        "signal.signal(signal.SIGINT, signal.default_int_handler); "
        "from autoform_cli.skeleton import _run_bounded_command; "
        "_run_bounded_command(sys.argv[1:], cwd=Path.cwd(), timeout=60, context='test command')"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    cli = subprocess.Popen(
        [sys.executable, "-c", driver, sys.executable, "-c", program], cwd=tmp_path, env=env
    )
    try:
        deadline = time.monotonic() + 20
        while not pids.exists() or len(pids.read_text(encoding="utf-8").split()) < 2:
            assert time.monotonic() < deadline and cli.poll() is None
            time.sleep(0.05)
        for signum in signals:
            os.kill(cli.pid, signum)
        assert cli.wait(timeout=15) == -signals[0]
    finally:
        if cli.poll() is None:
            cli.kill()
    deadline = time.monotonic() + 5
    survivors = [int(pid) for pid in pids.read_text(encoding="utf-8").split()]
    while survivors and time.monotonic() < deadline:
        survivors = [pid for pid in survivors if _pid_is_live(pid)]
        time.sleep(0.01)
    assert not survivors


def _pid_is_live(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_bounded_command_cleanup_reserves_time_and_reuses_final_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    deadlines: list[float] = []

    def report_stuck_readers(readers, *, deadline: float) -> bool:
        deadlines.append(deadline)
        _join_readers(readers, deadline=deadline)
        return False

    monkeypatch.setattr("autoform_cli.skeleton._join_readers", report_stuck_readers)

    with pytest.raises(SkeletonError, match="output pipes open"):
        _run_bounded_command(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            timeout=10,
            context="test command",
        )

    assert len(deadlines) == 3
    assert deadlines[0] < deadlines[1]
    assert deadlines[1] == deadlines[2]


def test_probe_freshness_and_execution_have_separate_budgets(tmp_path: Path, monkeypatch) -> None:
    # On a Mathlib project the freshness check alone can take minutes, which
    # must not come out of the probe's own budget.
    (tmp_path / "lake-manifest.json").write_text("{}\n", encoding="utf-8")
    calls: list[float] = []

    def fake_run(command, **kwargs):
        calls.append(kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout="probe output", stderr="")

    times = iter((100.0, 101.0))
    monkeypatch.setattr("autoform_cli.skeleton.shutil.which", lambda executable: "/bin/lake")
    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", fake_run)
    monkeypatch.setattr("autoform_cli.skeleton.time.monotonic", lambda: next(times))
    probe = render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))

    assert run_probe(probe, tmp_path, timeout=10, freshness_timeout=20) == "probe output"
    assert calls == [20, 9.0]


def test_a_probe_timeout_names_the_flag_that_raises_it(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "lake-manifest.json").write_text("{}\n", encoding="utf-8")
    bounded = _run_bounded_command

    def slow_probe(command, **kwargs):
        return bounded([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr("autoform_cli.skeleton.shutil.which", lambda executable: "/bin/lake")
    monkeypatch.setattr("autoform_cli.skeleton._check_artifacts_fresh", lambda *args, **kwargs: None)
    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", slow_probe)
    probe = render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))

    with pytest.raises(SkeletonError) as caught:
        run_probe(probe, tmp_path, timeout=1)
    assert caught.value.issues == (
        "lake env lean timed out after 1 seconds; rerun with --timeout <seconds> for large projects",
    )


def test_probe_records_file_is_held_to_the_output_limit(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "lake-manifest.json").write_text("{}\n", encoding="utf-8")

    def flooding_probe(command, *, env, **kwargs):
        Path(env[PROBE_OUTPUT_ENV]).write_text("x" * 2048, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("autoform_cli.skeleton.shutil.which", lambda executable: "/bin/lake")
    monkeypatch.setattr("autoform_cli.skeleton._check_artifacts_fresh", lambda *args, **kwargs: None)
    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", flooding_probe)
    monkeypatch.setattr("autoform_cli.skeleton.DEFAULT_PROBE_OUTPUT_LIMIT", 1024)
    probe = render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))

    with pytest.raises(SkeletonError, match="1024-byte output limit"):
        run_probe(probe, tmp_path)


def test_probe_requires_an_existing_lake_manifest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("autoform_cli.skeleton.shutil.which", lambda executable: "/bin/lake")
    probe = render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))

    with pytest.raises(SkeletonError, match="lake-manifest.json is missing"):
        run_probe(probe, tmp_path)


def test_parse_probe_output_keys_records_by_root_and_ignores_noise() -> None:
    records = parse_probe_output(_fake_probe_output(include_ghost=True))

    assert set(records) == {"Skel.observation_determined", "Skel.ghost"}
    assert records["Skel.ghost"] == {"root": "Skel.ghost", "found": False}


def test_parse_probe_output_rejects_malformed_records() -> None:
    with pytest.raises(SkeletonError):
        parse_probe_output(PROBE_MARKER + "{not json")
    with pytest.raises(SkeletonError):
        parse_probe_output(PROBE_MARKER + '{"found": true}')


def test_parse_probe_output_rejects_wrong_types_duplicates_and_unrequested_roots() -> None:
    with pytest.raises(SkeletonError, match="non-boolean found"):
        parse_probe_output(_record("Skel.ghost", found="false"))

    output = _fake_probe_output()
    line = output.splitlines()[-1]
    with pytest.raises(SkeletonError, match="duplicate records"):
        parse_probe_output("\n".join([output, line]))
    with pytest.raises(SkeletonError, match="unrequested root"):
        parse_probe_output(output, expected_roots=("Skel.somewhere_else",))


def test_parse_probe_output_resolves_shared_entries_strictly() -> None:
    output = _fake_probe_output()
    lines = output.splitlines()
    table = next(line for line in lines if '"table": "trusted"' in line)
    root = json.loads(lines[-1][len(PROBE_MARKER) :])
    with pytest.raises(SkeletonError, match="duplicate trusted entries"):
        parse_probe_output("\n".join([table, output]))
    with pytest.raises(SkeletonError, match="no trusted entry for Skel.Eligible"):
        parse_probe_output("\n".join(line for line in lines if '"name": "Skel.Eligible"' not in line))
    with pytest.raises(SkeletonError, match="no module entry for Mathlib.Fake"):
        parse_probe_output("\n".join(line for line in lines if '"table": "module"' not in line))
    renamed = table.replace('"name": "Skel.NonAmbiguous"', '"name": "Skel.Renamed"', 1)
    with pytest.raises(SkeletonError, match="malformed shared table entry"):
        parse_probe_output(renamed)
    with pytest.raises(SkeletonError, match="invalid fields"):
        parse_probe_output(_record(**root, assumed_semantics=[]))


def test_parse_probe_output_expands_fragments_strictly(monkeypatch) -> None:
    output = _fake_probe_output()
    *shared, last = output.splitlines()
    root = json.loads(last[len(PROBE_MARKER) :])
    (text,) = root["semantic"]

    def probe(fragments: dict[str, object], semantic: list[object]) -> str:
        lines = [
            PROBE_MARKER + json.dumps({"table": "fragment", "name": name, "value": value})
            for name, value in fragments.items()
        ]
        return "\n".join([*shared, *lines, PROBE_MARKER + json.dumps(dict(root, semantic=semantic))])

    # Fragment 1 repeats fragment 0 where the text does not.
    fragments = {"0": [text[:10]], "1": [0, text[10:20], 0]}
    shared_text = [1, text[30:]]
    reference = parse_probe_output(output)[root["root"]]
    parsed = parse_probe_output(probe({"0": [text[:10]], "1": [0, text[10:30]]}, shared_text))
    assert parsed[root["root"]] == reference
    with pytest.raises(SkeletonError, match="invalid elaborated semantic material"):
        parse_probe_output(probe(fragments, shared_text))
    with pytest.raises(SkeletonError, match="malformed fragment 0"):
        parse_probe_output(probe({"0": [1, "x"], "1": ["y"]}, [0]))
    with pytest.raises(SkeletonError, match="malformed fragment 01"):
        parse_probe_output(probe({"01": [text]}, [1]))
    with pytest.raises(SkeletonError, match="malformed fragment 0"):
        parse_probe_output(probe({"0": [True]}, [0]))
    with pytest.raises(SkeletonError, match=f"invalid semantic material for {root['root']}"):
        parse_probe_output(probe({}, [0]))
    with pytest.raises(SkeletonError, match=f"invalid semantic material for {root['root']}"):
        parse_probe_output(probe({}, text))  # type: ignore[arg-type]
    monkeypatch.setattr("autoform_cli.skeleton._PROBE_MATERIAL_LIMIT", len(text) - 1)
    with pytest.raises(SkeletonError, match="exceeds"):
        parse_probe_output(probe({"0": [text[:10]], "1": [0, text[10:30]]}, shared_text))


def test_parse_probe_output_rejects_incomplete_semantic_records() -> None:
    record = _fake_found_record()
    record["semantic_schema"] = "unknown"
    with pytest.raises(SkeletonError, match="unsupported semantic schema"):
        parse_probe_output(_probe_lines(record))

    record = _fake_found_record()
    record["semantic"] = "not JSON"
    with pytest.raises(SkeletonError, match="invalid elaborated semantic material"):
        parse_probe_output(_probe_lines(record))

    record = _fake_found_record()
    semantic = json.loads(str(record["semantic"]))
    semantic["root"]["safety"] = "unknown"
    record["semantic"] = json.dumps(semantic)
    with pytest.raises(SkeletonError, match="invalid elaborated semantic material"):
        parse_probe_output(_probe_lines(record))

    record = _fake_found_record()
    semantic = json.loads(str(record["semantic"]))
    semantic["generated"] = [
        {"name": "ambiguous.display.name", "material": semantic["root"]}
    ]
    record["semantic"] = json.dumps(semantic)
    with pytest.raises(SkeletonError, match="invalid elaborated semantic material"):
        parse_probe_output(_probe_lines(record))

    record = _fake_found_record()
    trusted = record["trusted"]
    assert isinstance(trusted, list) and isinstance(trusted[0], dict)
    trusted[0]["depends"] = [False]
    with pytest.raises(SkeletonError, match="invalid depends"):
        parse_probe_output(_probe_lines(record))

    record = _fake_found_record()
    trusted = record["trusted"]
    assert isinstance(trusted, list) and isinstance(trusted[0], dict)
    trusted[0]["source"] = trusted[0]["source_comments"] = None
    (parsed,) = parse_probe_output(_probe_lines(record)).values()
    assert "omitted required source" in str(_probe_record_issue(parsed))

    record = _fake_found_record()
    record["source"] = "theorem t : True := by trivial"
    with pytest.raises(SkeletonError, match="proof-bearing source"):
        parse_probe_output(_probe_lines(record))

    record = _fake_found_record()
    record["statement_source"] = 7
    with pytest.raises(SkeletonError, match="invalid statement_source"):
        parse_probe_output(_probe_lines(record))

    # A statement Lean cannot parse is withheld from the packet, not refused.
    record = _fake_found_record()
    record["statement_source"] = record["statement_comments"] = None
    (parsed,) = parse_probe_output(_probe_lines(record)).values()
    assert _probe_record_issue(parsed) is None


def test_generated_companions_without_a_source_range_need_no_source() -> None:
    record = _fake_found_record()
    trusted = record["trusted"]
    assert isinstance(trusted, list) and isinstance(trusted[0], dict)
    base = trusted[0]
    assert base["name"] == "Skel.NonAmbiguous" and base["range"] is not None

    def rangeless(name: str) -> dict[str, object]:
        return dict(base, name=name, source_name=name, range=None, source=None, source_comments=None)

    # Lean's internal-detail spellings only.
    companions = ["Skel.NonAmbiguous._unary", "Skel.Other.eq_1"]
    record["trusted"] = [*trusted, *(rangeless(name) for name in companions)]
    (parsed,) = parse_probe_output(_probe_lines(record)).values()
    assert _probe_record_issue(parsed) is None

    # An ordinary name needs its own source, even under a declaration with a range.
    for name in ("Skel.Unplaced.helper", "Skel.NonAmbiguous.generated"):
        record["trusted"] = [*trusted, rangeless(name)]
        (parsed,) = parse_probe_output(_probe_lines(record)).values()
        assert _probe_record_issue(parsed) == (
            f"the skeleton probe omitted required source for Skel.observation_determined trusted declaration {name}"
        )


def test_unparsable_source_is_withheld_not_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    record = _fake_found_record()
    # Lean parsed neither the statement nor a trusted source, and that source
    # has a docstring Lean could not locate: both are withheld.
    record["statement_source"] = record["statement_comments"] = None
    record["trusted"][2]["source_comments"] = None

    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _probe_lines(record))

    assert report.clean and not report.unresolved
    (declaration,) = report.nodes[0].declarations
    assert declaration.statement is None and declaration.source_withheld
    eligible = next(item for item in declaration.trusted if item.name == "Skel.Eligible")
    assert eligible.source is None and eligible.source_withheld
    packet = report.nodes[0].blind_text()
    assert "as written" not in packet and "Eligible (S" not in packet
    assert packet.count("-- source not shown: no reliable standalone source is available") == 2
    assert f"-- raw signature: {eligible.raw_signature}" in packet
    path = write_skeleton_report(report, tmp_path / "report.json")
    assert load_skeleton_report(path) == report
    # The flag cannot excuse source a report actually carries.
    data = json.loads(path.read_text(encoding="utf-8"))
    data["trusted"][data["nodes"][0]["declarations"][0]["trusted"][2]]["source_withheld"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SkeletonError, match="invalid withheld source flag"):
        load_skeleton_report(path)
    data = report.as_dict()
    data["trusted"][eligible.name]["start_line"] = None
    data["trusted"][eligible.name]["end_line"] = None
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SkeletonError, match="required source is missing"):
        load_skeleton_report(path)


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


def test_a_package_without_library_targets_is_its_own_library(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "Solo"\n', encoding="utf-8")

    (library,) = lean_libraries(project)

    assert (library.name, library.roots) == ("Solo", ("Solo",))
    assert library.src_dir == project.resolve()


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
    lakefile = project / "lakefile.toml"
    open_file = os.open
    saved = False

    # An editor saves identical bytes between the two reads: a new file, the same content.
    def save_identical_copy_after_first_open(path, flags, *args):
        nonlocal saved
        descriptor = open_file(path, flags, *args)
        if Path(path) == lakefile and not saved:
            saved = True
            (tmp_path / "lakefile.copy").write_bytes(lakefile.read_bytes())
            os.replace(tmp_path / "lakefile.copy", lakefile)
        return descriptor

    monkeypatch.setattr("autoform_cli.skeleton.os.open", save_identical_copy_after_first_open)

    (library,) = lean_libraries(project)

    assert saved and library.name == "Skel"


@pytest.mark.parametrize(
    ("swap", "expected_error"),
    [("symlink", "not a regular file"), ("file", "changed while it was read")],
)
def test_lake_configuration_snapshot_rejects_an_input_swapped_after_inspection(
    tmp_path: Path, monkeypatch, swap: str, expected_error: str
) -> None:
    project = _project(tmp_path)
    lakefile = project.resolve() / "lakefile.toml"
    inspected = tmp_path / "inspected.toml"
    inspected.write_bytes(lakefile.read_bytes())
    if swap == "symlink":
        lakefile.unlink()
        lakefile.symlink_to(inspected)
    lstat = Path.lstat

    # The inspection sees a regular file; the open then meets what replaced it.
    def inspect_before_swap(self: Path) -> os.stat_result:
        return lstat(inspected) if self == lakefile else lstat(self)

    monkeypatch.setattr(Path, "lstat", inspect_before_swap)

    with pytest.raises(SkeletonError, match=expected_error):
        lean_libraries(project)


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

    monkeypatch.setattr("autoform_cli.skeleton.os.open", change_before_second_read)

    with pytest.raises(SkeletonError, match="changed while it was read"):
        lean_libraries(project)


# --------------------------------------------------------------------------- #
# Extraction against a fake probe
# --------------------------------------------------------------------------- #


def test_extraction_orders_the_skeleton_and_locates_every_source(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    probes: list[str] = []

    def runner(probe: str, lean_root: Path) -> str:
        probes.append(probe)
        assert lean_root == project.resolve()
        return _fake_probe_output()

    report = extract_skeletons(blueprint, lean_root=project, runner=runner)

    assert report.clean
    assert len(probes) == 1 and "import Skel.Main" in probes[0]
    (node,) = report.nodes
    assert node.node_id == "basics/determined"
    assert node.article_path == "roadmap/basics/determined.md"
    (declaration,) = node.declarations
    assert declaration.path == "Skel/Main.lean"
    assert (declaration.start_line, declaration.end_line) == (14, 17)
    assert declaration.axioms == ("sorryAx",)
    assert declaration.assumed == ("Mathlib.Fake",)
    # Each trusted declaration is read after what it rests on; ties by name.
    assert [item.name for item in declaration.trusted] == [
        "Skel.Eligible",
        "Skel.NonAmbiguous",
        "Skel.Observation",
    ]
    assert declaration.trusted[1].path == "Skel/Defs.lean"
    # Two signature lines plus spans of 2, 3, and 4 source lines.
    assert declaration.skeleton_lines == 2 + 2 + 3 + 4
    assert declaration.declaration_lines == 4


@pytest.mark.parametrize("safety", ["safe", "unsafe"])
def test_local_safety_trusts_the_environment_without_source_lookup(safety: str) -> None:
    # A generated or rewritten name has no source index entry; that is no reason to refuse it.
    semantic = json.dumps({"generated": [], "root": {"safety": safety, "type": {}, "value": {}}})

    assert _local_safety_issue("_private.Project.0.Project.value._proof_1", semantic) is None


def test_local_safety_rejects_partial_material_including_generated_companions() -> None:
    issue = _local_safety_issue("spin", _semantic({"type": {}, "value": {}}).replace('"safe"', '"partial"'))
    assert issue == "partial declaration spin cannot be included in a trusted skeleton"

    companion = {"name": {"str": [None, "go"]}, "material": {"safety": "partial", "type": {}}}
    semantic = json.dumps({"generated": [companion], "root": {"safety": "safe", "type": {}, "value": {}}})
    assert _local_safety_issue("outer", semantic) == (
        "partial declaration outer cannot be included in a trusted skeleton"
    )


def test_skeleton_hash_uses_elaborated_semantics_not_source_formatting(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda probe, root: _fake_probe_output())
    declaration = report.nodes[0].declarations[0]

    presentation_only = replace(declaration, signature="differently formatted", statement="different spelling")
    semantic_change = replace(declaration, semantic=declaration.semantic + " changed")
    assumed_change = replace(
        declaration,
        assumed_semantics=((declaration.assumed[0], "changed external definition"),),
    )
    axiom_change = replace(
        declaration,
        axiom_semantics=((declaration.axioms[0], "changed axiom type"),),
    )
    module, file_kind, _ = declaration.boundary_modules[0]
    module_change = replace(
        declaration,
        boundary_modules=((module, file_kind, "sha256:" + "0" * 64),),
    )
    hidden_assumption = replace(declaration, assumed=())

    assert presentation_only.hash == declaration.hash
    assert presentation_only.evidence_hash != declaration.evidence_hash
    assert semantic_change.hash != declaration.hash
    assert assumed_change.hash != declaration.hash
    assert axiom_change.hash != declaration.hash
    assert module_change.hash != declaration.hash
    assert hidden_assumption.hash != declaration.hash


def test_assumed_module_identity_is_path_independent_and_rejects_a_concurrent_edit(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "first" / "External.olean"
    other = tmp_path / "second" / "External.olean"
    artifact.parent.mkdir()
    other.parent.mkdir()
    artifact.write_bytes(b"old")
    other.write_bytes(b"old")
    first = _hash_module_files(
        [["External", "olean", str(artifact)]], lean_root=tmp_path, cache={}
    )
    second = _hash_module_files(
        [["External", "olean", str(other)]], lean_root=tmp_path, cache={}
    )
    assert first == second

    other.write_bytes(b"different artifact")
    changed = _hash_module_files(
        [["External", "olean", str(other)]], lean_root=tmp_path, cache={}
    )
    assert changed != first

    started = time.time_ns()
    artifact.write_bytes(b"new")
    # A coarse filesystem clock can stamp a write in the same tick at or before `started`.
    os.utime(artifact, ns=(started + 10**9, started + 10**9))

    with pytest.raises(SkeletonError, match="changed during skeleton extraction"):
        _hash_module_files(
            [["External", "olean", str(artifact)]],
            lean_root=tmp_path,
            cache={},
            snapshot_started_ns=started,
        )


def test_assumed_module_identity_requires_compiled_parts_with_an_olean(tmp_path: Path) -> None:
    for entries in (
        [["External", "lean", "External.lean"]],
        [["External", "olean.private", "External.olean.private"]],
        [["External", "olean", "External.olean"], ["External", "olean", "External.olean"]],
    ):
        with pytest.raises(SkeletonError, match="assumed module"):
            _hash_module_files(entries, lean_root=tmp_path, cache={})

    parts = ("olean", "olean.server", "olean.private")
    for kind in parts:
        (tmp_path / f"External.{kind}").write_bytes(b"compiled")
    entries = [["External", kind, f"External.{kind}"] for kind in parts]
    identities = _hash_module_files(entries, lean_root=tmp_path, cache={})
    assert [kind for _, kind, _ in identities] == list(parts)
    # Equal bytes in different parts still hash apart.
    assert len({digest for _, _, digest in identities}) == 3


def test_trusted_theorem_source_never_exposes_its_proof(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    record = _fake_found_record()
    trusted = record["trusted"]
    assert isinstance(trusted, list)
    trusted.append(
        {
            "name": "Skel.eligible_of",
            "source_name": "Skel.eligible_of",
            "kind": "theorem",
            "module": "Skel.Defs",
            "range": [12, 13],
            "signature": "Skel.eligible_of {Y : Type} (S : Y → Prop) (y : Y) (h : S y) : Skel.Eligible S y",
            "raw_signature": "Skel.eligible_of {Y : Type} (S : Y -> Prop) (y : Y) (h : S y) : Skel.Eligible S y",
            "semantic_schema": SEMANTIC_SCHEMA,
            "semantic": _semantic({"type": {"sort": {"zero": None}}}),
            "depends": ["Skel.Eligible"],
            "source": None,
            "source_comments": None,
        }
    )
    output = _probe_lines(record)

    report = extract_skeletons(blueprint, lean_root=project, runner=lambda probe, root: output)
    theorem = next(item for item in report.nodes[0].declarations[0].trusted if item.name == "Skel.eligible_of")

    assert theorem.source is None
    assert ":= h" not in report.nodes[0].declarations[0].blind_text()
    assert ":= h" not in format_report(report, lean_root=project)


def test_extraction_reports_names_the_sources_and_the_environment_lack(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(
        tmp_path,
        lean={
            "determined": "Skel.observation_determined",
            "phantom": "Skel.doesNotExist",
            "ghost": "Skel.ghost",
        },
    )
    # `Skel.ghost` is lexically present so the probe is asked about it, but the
    # fake environment does not contain it, as after an unbuilt edit.
    main_file = project / "Skel" / "Main.lean"
    main_file.write_text(
        main_file.read_text(encoding="utf-8") + "\nnamespace Skel\ntheorem ghost : True := trivial\nend Skel\n",
        encoding="utf-8",
    )

    report = extract_skeletons(
        blueprint, lean_root=project, runner=lambda probe, root: _fake_probe_output(include_ghost=True)
    )

    assert not report.clean
    assert tuple(issue.message for issue in report.unresolved) == (
        "basics/ghost: Skel.ghost: not in the built environment; run `lake build`",
        "basics/phantom: Skel.doesNotExist: declaration not found in the Lean sources",
    )
    assert [node.node_id for node in report.nodes] == ["basics/determined", "basics/ghost", "basics/phantom"]
    assert report.nodes[1].declarations == ()


def test_article_with_an_unresolved_declaration_has_no_article_hash(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(
        tmp_path, lean={"mixed": "Skel.observation_determined Skel.doesNotExist"}
    )
    output = _probe_lines(_fake_found_record())

    report = extract_skeletons(blueprint, lean_root=project, runner=lambda probe, root: output)

    (node,) = report.nodes
    assert [item.name for item in node.declarations] == ["Skel.observation_determined"]
    # A hash over the resolved subset would not change when the missing
    # declaration's meaning does, so the article gets none.
    assert node.hash is None and node.review_hash is None
    assert node.as_dict()["hash"] is None and node.as_dict()["review_hash"] is None
    assert "None" not in format_report(report)
    path = write_skeleton_report(report, tmp_path / "report.json")
    assert load_skeleton_report(path) == report
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["nodes"][0]["hash"] = node.declarations[0].hash
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SkeletonError, match="invalid article hash"):
        load_skeleton_report(path)


def test_extraction_never_runs_lean_when_nothing_resolves(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"phantom": "Skel.doesNotExist"})

    def runner(probe: str, lean_root: Path) -> str:
        raise AssertionError("the probe must not run")

    report = extract_skeletons(blueprint, lean_root=project, runner=runner)

    assert tuple(issue.message for issue in report.unresolved) == (
        "basics/phantom: Skel.doesNotExist: declaration not found in the Lean sources",
    )


def test_default_extraction_rejects_sources_changed_during_probe(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})

    def changing_probe(probe: str, lean_root: Path) -> str:
        source = lean_root / "Skel" / "Defs.lean"
        source.write_text(source.read_text(encoding="utf-8") + "\n-- concurrent edit\n", encoding="utf-8")
        # A coarse filesystem clock can stamp this write at or before the snapshot it follows.
        later = time.time_ns() + 10**9
        os.utime(source, ns=(later, later))
        return _fake_probe_output()

    monkeypatch.setattr("autoform_cli.skeleton.run_probe", changing_probe)

    with pytest.raises(SkeletonError, match="changed during skeleton extraction"):
        extract_skeletons(blueprint, lean_root=project)


def test_custom_runner_rejects_sources_changed_during_probe(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})

    def changing_runner(probe: str, lean_root: Path) -> str:
        source = lean_root / "Skel" / "Main.lean"
        source.write_text(
            source.read_text(encoding="utf-8") + "\n-- concurrent edit\n",
            encoding="utf-8",
        )
        return _fake_probe_output()

    with pytest.raises(SkeletonError, match="Lean sources changed"):
        extract_skeletons(blueprint, lean_root=project, runner=changing_runner)


def test_extraction_rejects_a_blueprint_changed_during_probe(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    article = blueprint / "roadmap" / "basics" / "determined.md"

    def changing_probe(probe: str, lean_root: Path) -> str:
        article.write_text(article.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")
        return _fake_probe_output()

    monkeypatch.setattr("autoform_cli.skeleton.run_probe", changing_probe)

    with pytest.raises(SkeletonError, match="blueprint changed"):
        extract_skeletons(blueprint, lean_root=project)


def test_extraction_rejects_a_passage_changed_during_probe(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    source = blueprint / "sources" / "book.tex"
    source.parent.mkdir()
    source.write_text("before\n", encoding="utf-8")
    article = blueprint / "roadmap" / "basics" / "determined.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "## Depends on",
            "## Sources\n\n- [book](../../sources/book.tex#L1-L1)\n\n## Depends on",
        ),
        encoding="utf-8",
    )

    def changing_probe(probe: str, lean_root: Path) -> str:
        source.write_text("after\n", encoding="utf-8")
        return _fake_probe_output()

    monkeypatch.setattr("autoform_cli.skeleton.run_probe", changing_probe)

    with pytest.raises(SkeletonError, match="source passage changed"):
        extract_skeletons(blueprint, lean_root=project)


def test_blind_packet_shows_the_statement_without_notation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    declaration = report.nodes[0].declarations[0]

    assert "-- raw signature:\n" + declaration.raw_signature in declaration.blind_text()
    assert "Exists fun y => o.admits y" in declaration.blind_text()
    # Notation that hides a different operator changes the packet a reviewer sees.
    misread = replace(declaration, raw_signature=declaration.raw_signature + " ")
    assert misread.evidence_hash != declaration.evidence_hash


def test_review_hash_binds_the_meaning_hash(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    node = report.nodes[0]
    declaration = node.declarations[0]

    # An external boundary can change meaning without changing the packet text;
    # a review recorded against the review hash must not carry over.
    module, kind, _ = declaration.boundary_modules[0]
    changed_declaration = replace(
        declaration,
        boundary_modules=((module, kind, "sha256:" + "0" * 64),),
    )
    changed = replace(node, declarations=(changed_declaration,))
    assert changed.evidence_hash == node.evidence_hash
    assert changed.hash != node.hash
    assert changed.review_hash != node.review_hash


def test_packets_drop_the_comments_lean_reports_and_keep_the_rest(tmp_path: Path) -> None:
    record = _fake_found_record()
    trusted = record["trusted"][2]
    # `/--/` opens a docstring whose body is `/ KEEPOUT `; a lexer that closes
    # it at the next `-/` would show the docstring as code.
    trusted["source"] = "/--/ KEEPOUT -/\ndef docOpened : Nat := 6"
    trusted["source_comments"] = [[0, len("/--/ KEEPOUT -/")]]
    record["trusted"] = [trusted, record["trusted"][1], record["trusted"][0]]
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _probe_lines(record))

    blind = report.nodes[0].blind_text()
    assert "KEEPOUT" not in blind and "def docOpened : Nat := 6" in blind
    assert "Uses a structure" not in blind and "theorem observation_determined" in blind


def test_removing_comments_does_not_join_tokens_or_lines() -> None:
    inline = "Nat.succ/- explanation -/0"
    multiline = "foo/- first\nsecond -/bar"

    assert _without_comments(inline, ((8, 25),)) == "Nat.succ" + " " * 17 + "0"
    assert _without_comments(multiline, ((3, 21),)) == "foo\n" + " " * 9 + "bar"
    # Deleting the comment would turn `h x = hx` into the tautology `hx = hx`.
    glued = "def g (h : Nat → Nat) (x hx : Nat) : Prop := h/- -/x = hx"
    start = len(glued.encode("utf-8")) - len("/- -/x = hx")
    assert _without_comments(glued, ((start, start + 5),)).endswith(":= h     x = hx")


def test_packets_fail_closed_when_lean_cannot_locate_comments(tmp_path: Path) -> None:
    record = _fake_found_record()
    # `=--` is a project token here, not a line comment. With ranges from Lean
    # the code is kept; without them the source is withheld, not guessed.
    record["trusted"][2]["source"] = "def claim : Prop := 2 + 2 =--\n  5"
    record["trusted"][2]["source_comments"] = []
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _probe_lines(record))
    assert "def claim : Prop := 2 + 2 =--\n  5" in report.nodes[0].blind_text()

    record["trusted"][2]["source_comments"] = None
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _probe_lines(record))
    assert "=--" not in report.nodes[0].blind_text()
    (claim,) = [item for item in report.nodes[0].declarations[0].trusted if item.name == "Skel.Eligible"]
    assert claim.source is None and claim.source_withheld


@pytest.mark.parametrize("ranges", [[[2, 5]], [[0, 999]], [[0, 5], [3, 8]], [[5, 3]], "none"])
def test_probe_comment_ranges_must_cover_comments(tmp_path: Path, ranges: object) -> None:
    record = _fake_found_record()
    record["statement_comments"] = ranges
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})

    with pytest.raises(SkeletonError, match="invalid statement_comments"):
        extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _probe_lines(record))


@pytest.mark.parametrize(
    ("link", "why"),
    [
        ("../../sources/book.tex#L4-L4", "names no lines of its file"),
        ("../../sources/book.tex#L5-L6", "names no lines of its file"),
        ("../../sources/book.tex#L2-L1", "names no lines of its file"),
        ("../../sources/book.tex#L0-L1", "names no lines of its file"),
        ("../../sources/empty.tex#L1-L1", "names no lines of its file"),
        ("../../sources/missing.tex#L1-L1", "names a missing file"),
        ("../../sources/binary.tex#L1-L1", "not readable UTF-8 text"),
        ("../../../outside.tex#L1-L1", "points outside the blueprint"),
    ],
)
def test_extraction_reports_a_locator_that_names_no_passage(tmp_path: Path, link: str, why: str) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    sources = blueprint / "sources"
    sources.mkdir()
    (sources / "book.tex").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (sources / "empty.tex").write_text("", encoding="utf-8")
    (sources / "binary.tex").write_bytes(b"\xff\xfe\n")
    (tmp_path / "outside.tex").write_text("outside\n", encoding="utf-8")
    article = blueprint / "roadmap" / "basics" / "determined.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "## Depends on", f"## Sources\n\n- [book]({link})\n\n## Depends on"
        ),
        encoding="utf-8",
    )

    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())

    assert not report.clean and report.nodes[0].declarations == ()
    [unresolved] = report.unresolved
    assert unresolved.declaration == "Skel.observation_determined"
    assert unresolved.reason.startswith("source locator ") and why in unresolved.reason

    article.write_text(
        article.read_text(encoding="utf-8").replace(link, "../../sources/book.tex#L2-L3"), encoding="utf-8"
    )
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    assert report.clean
    assert report.nodes[0].passage == "two\nthree"


def test_extraction_rejects_lake_configuration_changed_during_probe(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})

    def changing_probe(probe: str, lean_root: Path) -> str:
        lakefile = lean_root / "lakefile.toml"
        lakefile.write_text(lakefile.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        return _fake_probe_output()

    monkeypatch.setattr("autoform_cli.skeleton.run_probe", changing_probe)

    with pytest.raises(SkeletonError, match="configuration changed"):
        extract_skeletons(blueprint, lean_root=project)


def test_node_selection_rejects_unknown_articles(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})

    with pytest.raises(SkeletonError) as caught:
        extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: "", node_ids=("basics/nope",))

    assert caught.value.issues == ("unknown article: basics/nope",)


@pytest.mark.parametrize("targets", [", ,", "Skel.observation_determined, Skel.observation_determined"])
def test_extraction_rejects_empty_or_duplicate_declaration_targets(
    tmp_path: Path, targets: str
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": targets})

    def runner(probe: str, lean_root: Path) -> str:
        raise AssertionError("invalid targets must be rejected before running Lean")

    with pytest.raises(SkeletonError, match="lean target list|duplicate Lean declaration"):
        extract_skeletons(blueprint, lean_root=project, runner=runner)


def test_node_selection_rejects_an_article_without_declarations(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    article = blueprint / "roadmap" / "basics" / "notes.md"
    article.write_text("# Notes\n\nNo formal declaration.\n", encoding="utf-8")

    with pytest.raises(SkeletonError, match="article has no Lean declaration targets"):
        extract_skeletons(
            blueprint,
            lean_root=project,
            runner=lambda p, r: "",
            node_ids=("basics/notes",),
        )


def test_report_distinguishes_full_and_filtered_blueprint_scope(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(
        tmp_path,
        lean={
            "first": "Skel.observation_determined",
            "second": "Skel.observation_determined",
        },
    )

    def runner(probe: str, root: Path) -> str:
        return _fake_probe_output()

    full = extract_skeletons(blueprint, lean_root=project, runner=runner)
    filtered = extract_skeletons(
        blueprint,
        lean_root=project,
        runner=runner,
        node_ids=("basics/first",),
    )

    assert full.blueprint_hash == filtered.blueprint_hash
    assert full.targets == filtered.targets
    assert full.selection == "all"
    assert full.selected_nodes == ("basics/first", "basics/second")
    assert filtered.selection == "filtered"
    assert filtered.selected_nodes == ("basics/first",)
    assert full.to_json() != filtered.to_json()


def test_node_selection_does_not_change_what_the_probe_imports(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(
        tmp_path,
        lean={
            "determined": "Skel.observation_determined",
            "notation": "Skel.heavy_of_notation",
            "scoped": "Skel.ScopedA.activatesScope",
        },
    )
    # An article whose passage cannot be read still names a module.
    article = blueprint / "roadmap" / "basics" / "scoped.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "## Depends on", "## Sources\n\n- [book](sources/missing.txt#L1-L1)\n\n## Depends on"
        ),
        encoding="utf-8",
    )
    probes: list[str] = []

    def runner(probe: str, lean_root: Path) -> str:
        probes.append(probe)
        record = _fake_found_record()
        # As in Lean, a signature prints with the notation its imports bring in.
        if "import Skel.Uses" in probe:
            record["signature"] = "Skel.observation_determined : printed with notation from Skel.Uses"
        return _probe_lines(record)

    full = extract_skeletons(blueprint, lean_root=project, runner=runner)
    scoped = extract_skeletons(blueprint, lean_root=project, runner=runner, node_ids=("basics/determined",))

    assert _probe_modules(probes[0]) == _probe_modules(probes[1]) == ("Skel.Main", "Skel.ScopedA", "Skel.Uses")
    assert "Skel.heavy_of_notation" in probes[0] and "Skel.heavy_of_notation" not in probes[1]
    assert scoped.node("basics/determined") == full.node("basics/determined")


def test_report_is_identical_across_checkout_roots(tmp_path: Path) -> None:
    reports = []
    for name in ("first", "second"):
        checkout = tmp_path / name
        checkout.mkdir()
        project = _project(checkout)
        blueprint = _blueprint(
            checkout, lean={"determined": "Skel.observation_determined"}
        )
        reports.append(
            extract_skeletons(
                blueprint,
                lean_root=project,
                runner=lambda probe, root: _fake_probe_output(),
            )
        )

    assert reports[0].to_json() == reports[1].to_json()


def test_report_round_trips_through_json_deterministically(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())

    first = report.to_json()
    assert first == report.to_json()
    assert json.loads(first)["schema"] == SKELETON_SCHEMA
    assert str(tmp_path) not in first

    path = tmp_path / "skeleton.json"
    path.write_text(first, encoding="utf-8")
    assert load_skeleton_report(path) == report

    path.write_text('{"schema": "something-else"}', encoding="utf-8")
    with pytest.raises(SkeletonError):
        load_skeleton_report(path)

    for schema in ("autoform-skeleton/v1", "autoform-skeleton/v2", "autoform-skeleton/v3"):
        legacy = report.as_dict()
        legacy["schema"] = schema
        path.write_text(json.dumps(legacy), encoding="utf-8")
        with pytest.raises(SkeletonError):
            load_skeleton_report(path)


def test_report_states_shared_material_once(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(
        tmp_path,
        lean={"determined": "Skel.observation_determined", "supervision": "Skel.supervision_nonAmbiguous"},
    )
    second = _probe_lines({**_fake_found_record(), "root": "Skel.supervision_nonAmbiguous"}).splitlines()[-1]
    output = _fake_probe_output() + "\n" + second
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: output)
    assert report.clean

    first = report.to_json()
    data = json.loads(first)
    assert sorted(data["trusted"]) == ["Skel.Eligible", "Skel.NonAmbiguous", "Skel.Observation"]
    assert list(data["semantics"]) == ["Mathlib.Fake", "sorryAx"]
    assert list(data["boundary_modules"]) == ["Mathlib.Fake"]
    for node in data["nodes"]:
        (declaration,) = node["declarations"]
        assert declaration["trusted"] == ["Skel.Eligible", "Skel.NonAmbiguous", "Skel.Observation"]
        assert declaration["boundary_modules"] == ["Mathlib.Fake"]
    source = data["trusted"]["Skel.Eligible"]["source"]
    assert first.count(json.dumps(source, ensure_ascii=False)) == 1
    path = tmp_path / "skeleton.json"
    path.write_text(first, encoding="utf-8")
    assert load_skeleton_report(path) == report

    for table, name in (("trusted", "Skel.Eligible"), ("semantics", "sorryAx"), ("boundary_modules", "Mathlib.Fake")):
        payload = json.loads(first)
        payload[table]["Skel.Unused"] = payload[table][name]
        if table == "trusted":
            payload[table]["Skel.Unused"] = dict(payload[table][name], name="Skel.Unused")
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SkeletonError, match="unreferenced shared entries"):
            load_skeleton_report(path)
        del payload[table]["Skel.Unused"], payload[table][name]
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SkeletonError, match="mismatched"):
            load_skeleton_report(path)

    payload = json.loads(first)
    payload["trusted"]["Skel.Eligible"]["name"] = "Skel.NonAmbiguous"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SkeletonError, match="mismatched shared trusted declaration"):
        load_skeleton_report(path)


def test_report_loader_rejects_scope_tampering(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    path = tmp_path / "skeleton.json"

    payload = report.as_dict()
    payload["target_count"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SkeletonError, match="target count"):
        load_skeleton_report(path)

    payload = report.as_dict()
    payload["nodes"][0] = replace(report.nodes[0], declarations=(), complete=False).as_dict()
    payload["trusted"] = payload["semantics"] = payload["boundary_modules"] = {}
    payload["unresolved"] = [
        {"declaration": "made.up", "node_id": "basics/determined", "reason": "missing"}
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SkeletonError, match="mismatched unresolved declarations"):
        load_skeleton_report(path)

    payload = report.as_dict()
    payload["targets"][0]["declarations"].append("Skel.observation_determined")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SkeletonError, match="duplicate target declarations"):
        load_skeleton_report(path)


def test_report_loader_rejects_mismatched_hashes_and_trust_identities(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    path = tmp_path / "skeleton.json"

    payload = report.as_dict()
    payload["nodes"][0]["declarations"][0]["hash"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SkeletonError, match="invalid declaration hash"):
        load_skeleton_report(path)

    payload = report.as_dict()
    payload["nodes"][0]["declarations"][0]["statement"] = "theorem t : True := by trivial"
    payload["nodes"][0]["declarations"][0]["statement_comments"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SkeletonError, match="invalid declaration evidence hash"):
        load_skeleton_report(path)

    payload = report.as_dict()
    payload["nodes"][0]["passage"] = "different source theorem"
    payload["nodes"][0]["passage_locator"] = "sources/book.tex#L1-L1"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SkeletonError, match="invalid article review hash"):
        load_skeleton_report(path)

    payload = report.as_dict()
    payload["nodes"][0]["declarations"][0]["assumed"] = ["Mathlib.Other"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SkeletonError, match="mismatched assumption semantics"):
        load_skeleton_report(path)

    payload = report.as_dict()
    trusted = payload["trusted"][payload["nodes"][0]["declarations"][0]["trusted"][0]]
    trusted["kind"] = "theorem"
    trusted["semantic"] = _semantic({"type": {"sort": {"zero": None}}})
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SkeletonError, match="proof-bearing source is forbidden"):
        load_skeleton_report(path)


def test_text_report_quotes_the_sources_a_reader_must_trust(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())

    text = format_report(report, lean_root=project)

    assert text.startswith("== basics/determined · theorem Skel.observation_determined\n")
    assert "   trusts 3 local declarations, 11 lines to read; the declaration itself spans 4 lines · skeleton " in text
    assert "   assumes: Mathlib.Fake\n" in text
    assert "   axioms: sorryAx\n" in text
    assert "   -- def Skel.Eligible  (Skel/Defs.lean:5-6)\n" in text
    assert "   def Eligible (S : Y → Prop) (y : Y) : Prop := S y\n" in text
    assert "   structure Observation (Y : Type) where\n" in text
    # The elaborated signature restores what `variable` binders leave implicit.
    assert "   -- Skel.NonAmbiguous {Y : Type} (S : Y → Prop) : Prop\n" in text
    # The quoted source travels inside the report, so no Lean tree is needed to print it.
    assert "   def Eligible (S : Y → Prop) (y : Y) : Prop := S y\n" in format_report(report)


def test_cli_writes_the_artifact_and_fails_on_unresolved_names(tmp_path: Path, capsys, monkeypatch) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(
        tmp_path,
        lean={"determined": "Skel.observation_determined", "phantom": "Skel.doesNotExist"},
    )
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", lambda probe, root: _fake_probe_output())
    output = tmp_path / "out" / "skeleton.json"

    assert main(["skeleton", str(blueprint), "--lean-root", str(project), "--output", str(output)]) == 1

    out = capsys.readouterr().out
    assert out.splitlines() == [
        f"{output}: 1 skeleton(s) for 2 article(s)",
        "error: basics/phantom: Skel.doesNotExist: declaration not found in the Lean sources",
    ]
    assert load_skeleton_report(output).nodes[0].node_id == "basics/determined"

    assert main(["skeleton", str(blueprint), "--lean-root", str(project), "--node", "basics/determined", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["unresolved"] == []


def test_cli_sets_the_probe_timeout(tmp_path: Path, capsys, monkeypatch) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    timeouts: list[float] = []

    def fake_run_probe(probe: str, root: Path, *, timeout: float) -> str:
        timeouts.append(timeout)
        return _fake_probe_output()

    monkeypatch.setattr("autoform_cli.__main__.run_probe", fake_run_probe)
    command = ["skeleton", str(blueprint), "--lean-root", str(project), "--json"]
    assert main([*command, "--timeout", "1800"]) == 0
    assert timeouts == [1800.0]
    for bad in ("0", "-5", "inf", "nan", "soon"):
        with pytest.raises(SystemExit):
            main([*command, "--timeout", bad])
    assert "expected a positive number of seconds" in capsys.readouterr().err


def test_cli_reports_extraction_failures_on_stderr(tmp_path: Path, capsys) -> None:
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})

    assert main(["skeleton", str(blueprint), "--lean-root", str(tmp_path / "nowhere")]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: no lakefile.toml or lakefile.lean in ")


def test_cli_reports_an_invalid_report_output_without_a_traceback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", lambda probe, root: _fake_probe_output())
    output = tmp_path / "skeleton.json"
    output.mkdir()

    assert main(
        ["skeleton", str(blueprint), "--lean-root", str(project), "--output", str(output)]
    ) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "report output exists and is not a regular file" in captured.err


def test_cli_keeps_json_stdout_machine_readable_with_packets(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", lambda probe, root: _fake_probe_output())
    packets = tmp_path / "packets"

    assert main(
        [
            "skeleton",
            str(blueprint),
            "--lean-root",
            str(project),
            "--packets",
            str(packets),
            "--json",
        ]
    ) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["schema"] == SKELETON_SCHEMA
    assert "blind packet(s) written" in captured.err


def test_cli_rejects_passages_without_packets(tmp_path: Path, capsys) -> None:
    assert main(
        [
            "skeleton",
            str(tmp_path / "missing-blueprint"),
            "--lean-root",
            str(tmp_path / "missing-project"),
            "--passages",
            str(tmp_path / "passages"),
        ]
    ) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: --passages requires --packets\n"


def test_cli_reports_unsafe_packet_output_without_a_traceback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", lambda probe, root: _fake_probe_output())
    packets = tmp_path / "packets"
    packets.mkdir()
    (packets / "keep.txt").write_text("mine\n", encoding="utf-8")

    assert main(
        [
            "skeleton",
            str(blueprint),
            "--lean-root",
            str(project),
            "--packets",
            str(packets),
        ]
    ) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refusing to overwrite non-Autoform packet output" in captured.err


def test_cli_rejects_a_report_path_inside_the_packet_tree(tmp_path: Path, capsys) -> None:
    packets = tmp_path / "packets"
    assert main(
        [
            "skeleton",
            str(tmp_path / "blueprint"),
            "--lean-root",
            str(tmp_path / "project"),
            "--packets",
            str(packets),
            "--output",
            str(packets / "manifest.json"),
        ]
    ) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: --output must be disjoint from packet and passage directories\n"


def test_cli_publishes_report_and_packet_trees_together(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", lambda probe, root: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    output = tmp_path / "skeleton.json"
    output.write_text("old report\n", encoding="utf-8")

    result = main(
        [
            "skeleton",
            str(blueprint),
            "--lean-root",
            str(project),
            "--packets",
            str(packets),
            "--passages",
            str(passages),
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert (packets / PACKET_MANIFEST).is_file()
    assert (passages / PACKET_MANIFEST).is_file()
    assert load_skeleton_report(output).clean
    assert list(tmp_path.glob(".*.autoform-*")) == []
    assert capsys.readouterr().err == ""


def test_cli_does_not_publish_packets_when_report_staging_fails(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", lambda probe, root: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    output = tmp_path / "skeleton.json"

    def fail_report_stage(report: SkeletonReport, destination: Path):
        raise OSError("simulated report staging failure")

    monkeypatch.setattr("autoform_cli.skeleton._stage_report_output", fail_report_stage)

    result = main(
        [
            "skeleton",
            str(blueprint),
            "--lean-root",
            str(project),
            "--packets",
            str(packets),
            "--passages",
            str(passages),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert not packets.exists()
    assert not passages.exists()
    assert not output.exists()
    assert list(tmp_path.glob(".*.autoform-stage-*")) == []
    assert "simulated report staging failure" in capsys.readouterr().err


def test_cli_rolls_back_packet_trees_when_report_commit_fails(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", lambda probe, root: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    output = tmp_path / "skeleton.json"
    install_output = _install_output

    def fail_report_install(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        if "autoform-stage" in source_path.name and Path(destination) == output:
            raise OSError("simulated report commit failure")
        install_output(source_path, Path(destination))

    monkeypatch.setattr("autoform_cli.skeleton._install_output", fail_report_install)

    result = main(
        [
            "skeleton",
            str(blueprint),
            "--lean-root",
            str(project),
            "--packets",
            str(packets),
            "--passages",
            str(passages),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert not packets.exists()
    assert not passages.exists()
    assert not output.exists()
    assert list(tmp_path.glob(".*.autoform-*")) == []
    assert "simulated report commit failure" in capsys.readouterr().err


def test_cli_rolls_back_packet_trees_and_report_when_interrupted_after_report_install(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", lambda probe, root: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    output = tmp_path / "skeleton.json"
    output.write_text("old report\n", encoding="utf-8")
    report = extract_skeletons(
        blueprint,
        lean_root=project,
        runner=lambda probe, root: _fake_probe_output(),
    )
    write_packets(report, packets, passages=passages)
    (packets / "old-marker").write_text("old packets\n", encoding="utf-8")
    (passages / "old-marker").write_text("old passages\n", encoding="utf-8")
    install_output = _install_output

    def interrupt_after_report_install(source: str | Path, destination: str | Path) -> None:
        install_output(Path(source), Path(destination))
        if "autoform-stage" in Path(source).name and Path(destination) == output:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        "autoform_cli.skeleton._install_output", interrupt_after_report_install
    )

    with pytest.raises(KeyboardInterrupt):
        main(
            [
                "skeleton",
                str(blueprint),
                "--lean-root",
                str(project),
                "--packets",
                str(packets),
                "--passages",
                str(passages),
                "--output",
                str(output),
            ]
        )

    assert (packets / "old-marker").read_text(encoding="utf-8") == "old packets\n"
    assert (passages / "old-marker").read_text(encoding="utf-8") == "old passages\n"
    assert output.read_text(encoding="utf-8") == "old report\n"
    assert list(tmp_path.glob(".*.autoform-*")) == []


def test_report_publication_preserves_a_concurrent_replacement_before_install(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(
        blueprint,
        lean_root=project,
        runner=lambda probe, root: _fake_probe_output(),
    )
    output = tmp_path / "skeleton.json"
    output.write_text("old report\n", encoding="utf-8")
    install_output = _install_output

    def replace_before_install(stage: Path, destination: Path) -> None:
        if destination == output:
            output.write_text("concurrent report\n", encoding="utf-8")
        install_output(stage, destination)

    monkeypatch.setattr("autoform_cli.skeleton._install_output", replace_before_install)

    with pytest.raises(SkeletonError, match="published output changed during rollback"):
        write_skeleton_report(report, output)

    assert output.read_text(encoding="utf-8") == "concurrent report\n"
    backups = list(tmp_path.glob(".skeleton.json.autoform-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old report\n"


def test_report_publication_rolls_back_when_interrupted_after_exclusive_link(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(
        blueprint,
        lean_root=project,
        runner=lambda probe, root: _fake_probe_output(),
    )
    output = tmp_path / "skeleton.json"
    output.write_text("old report\n", encoding="utf-8")
    link = os.link

    def interrupt_after_link(source: str | Path, destination: str | Path) -> None:
        link(source, destination)
        if "autoform-stage" in Path(source).name and Path(destination) == output:
            raise KeyboardInterrupt

    monkeypatch.setattr("autoform_cli.skeleton.os.link", interrupt_after_link)

    with pytest.raises(KeyboardInterrupt):
        write_skeleton_report(report, output)

    assert output.read_text(encoding="utf-8") == "old report\n"
    assert list(tmp_path.glob(".*.autoform-*")) == []


def test_cli_refuses_a_symlink_report_without_publishing_packets(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", lambda probe, root: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    report_target = tmp_path / "report-target.json"
    report_target.write_text("keep me\n", encoding="utf-8")
    output = tmp_path / "skeleton.json"
    output.symlink_to(report_target)

    result = main(
        [
            "skeleton",
            str(blueprint),
            "--lean-root",
            str(project),
            "--packets",
            str(packets),
            "--passages",
            str(passages),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert not packets.exists()
    assert not passages.exists()
    assert output.is_symlink()
    assert report_target.read_text(encoding="utf-8") == "keep me\n"
    assert "refusing symlink report output" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The real probe
# --------------------------------------------------------------------------- #


def _lean_toolchain_available() -> bool:
    """Whether the fixture can be built here without downloading a toolchain."""

    def unavailable(reason: str) -> bool:
        if os.environ.get("AUTOFORM_REQUIRE_REAL_LEAN_TESTS") == "1":
            raise RuntimeError(f"real Lean tests are required but unavailable: {reason}")
        return False

    if shutil.which("lake") is None:
        return unavailable("lake is not on PATH")
    elan = shutil.which("elan")
    if elan is None:
        return True
    pinned = (_FIXTURE / "lean-toolchain").read_text(encoding="utf-8").strip()
    try:
        listed = subprocess.run([elan, "toolchain", "list"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return unavailable("elan toolchain discovery failed")
    available = any(line.split()[:1] == [pinned] for line in listed.stdout.splitlines())
    return available or unavailable(f"{pinned} is not installed")


def test_required_real_lean_tests_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOFORM_REQUIRE_REAL_LEAN_TESTS", "1")
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="lake is not on PATH"):
        _lean_toolchain_available()


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_the_probe_reads_a_built_project(tmp_path: Path) -> None:
    project = _project(tmp_path)
    build = subprocess.run(["lake", "build"], cwd=project, capture_output=True, text=True, timeout=600, check=False)
    assert build.returncode == 0, build.stderr
    blueprint = _blueprint(
        tmp_path,
        lean={
            "determined": "Skel.observation_determined",
            "heavy": "Skel.heavy_of_weight",
            "notation": "Skel.heavy_of_notation",
            "supervision": "Skel.supervision_nonAmbiguous, Skel.supervision",
        },
    )

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    determined = report.nodes[0].declarations[0]
    # Scoped notation from a namespace the file opens still parses, and a cast
    # is printed with the type it lands in.
    notation = next(d for n in report.nodes for d in n.declarations if d.name == "Skel.heavy_of_notation")
    assert notation.statement is not None
    assert notation.statement.rstrip().endswith("Skel.heavy (1 : Nat)")
    assert "(↑1 : Int)" in notation.signature or "(↑(1 : Nat) : Int)" in notation.signature
    # The statement as written is cut before the proof by Lean's parser.
    assert determined.statement is not None
    assert determined.statement.startswith("/-- Uses a structure in its statement")
    assert determined.statement.rstrip().endswith("∀ z, o.admits z → z = y")
    assert ":=" not in determined.statement and "sorry" not in determined.statement.split("-/")[-1]
    blind = determined.blind_text()
    assert "-- as written:" in blind and ":= by" not in blind and "sorry in its proof" not in blind
    # The statement rests on two definitions and a structure. The helper lemma
    # the proof uses, and the structure's generated companions, never appear.
    assert [(item.name, item.kind) for item in determined.trusted] == [
        ("Skel.Eligible", "def"),
        ("Skel.NonAmbiguous", "def"),
        ("Skel.Observation", "structure"),
    ]
    assert determined.axioms == ("sorryAx",)
    assert determined.assumed == ()
    assert determined.signature.startswith("Skel.observation_determined {Y : Type} (o : Skel.Observation Y)")
    assert determined.trusted[2].start_line == 15 and determined.trusted[2].end_line == 18

    heavy = report.nodes[1].declarations[0]
    # A local class reached through its projection is trusted once, as the class.
    assert [(item.name, item.kind) for item in heavy.trusted] == [("Skel.HasWeight", "class"), ("Skel.heavy", "def")]

    supervision, definition = next(n for n in report.nodes if n.node_id.endswith("/supervision")).declarations
    assert [item.name for item in supervision.trusted] == ["Skel.Eligible", "Skel.NonAmbiguous", "Skel.supervision"]
    assert supervision.axioms == ()
    assert definition.kind == "def" and definition.trusted == ()

    partial_blueprint = _blueprint(
        tmp_path / "partial",
        lean={"partial": "Skel.Semantics.partialValue"},
    )
    _assert_partial_refused(
        extract_skeletons(partial_blueprint, lean_root=project), "Skel.Semantics.partialValue"
    )

    ordinary_blueprint = _blueprint(
        tmp_path / "ordinary",
        lean={"ordinary": "Skel.Semantics.ordinaryWithNamedCompanion"},
    )
    assert extract_skeletons(ordinary_blueprint, lean_root=project).clean

    source = project / "Skel" / "Main.lean"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "∃ y, o.admits y ∧ ∀ z, o.admits z → z = y := by",
            "True := by",
        ),
        encoding="utf-8",
    )
    with pytest.raises(SkeletonError, match="build artifacts are stale"):
        extract_skeletons(blueprint, lean_root=project)


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_probe_records_bypass_the_command_capture_and_other_output(tmp_path: Path) -> None:
    # Lean holds a command's `IO.println` output until the command ends, then
    # prints it at a cost quadratic in its size. A probe that leaves before its
    # command ends shows whether its records went past that capture, and a large
    # message printed meanwhile must not split one of them.
    project = _project(tmp_path)
    build = subprocess.run(
        ["lake", "build", "Skel.Main"], cwd=project, capture_output=True, text=True, timeout=600, check=False
    )
    assert build.returncode == 0, build.stdout + build.stderr
    probe = render_probe(
        imports=("Skel.Main",), roots=("Skel.observation_determined",), project_roots=("Skel",)
    )
    loop = next(line for line in probe.splitlines() if "AutoformSkeleton.skeleton projectRoots" in line)
    leave = "    (← IO.getStdout).flush\n    let _ : Unit ← IO.Process.exit 0"
    noise = "#eval IO.println (String.mk (List.replicate 3000000 'x'))\n\n"
    command = "set_option maxHeartbeats 0 in\nrun_cmd"
    assert command in probe
    probe = probe.replace(loop, f"{loop}\n{leave}").replace(command, f"{noise}{command}")

    records = parse_probe_output(run_probe(probe, project), expected_roots=("Skel.observation_determined",))

    assert records["Skel.observation_determined"]["found"] is True


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

    monkeypatch.setattr("autoform_cli.skeleton.DEFAULT_PROBE_OUTPUT_LIMIT", limit)
    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", inspect_records)
    probe = render_probe(
        imports=("Skel.Main",), roots=("Skel.observation_determined",), project_roots=("Skel",)
    )

    with pytest.raises(SkeletonError, match=f"{limit}-byte output limit"):
        run_probe(probe, project)
    assert sizes and sizes[0] <= limit


def _built_module(tmp_path: Path, module: str, source: str) -> Path:
    project = _project(tmp_path)
    (project / "Skel" / f"{module}.lean").write_text(source, encoding="utf-8")
    build = subprocess.run(
        ["lake", "build", f"Skel.{module}"], cwd=project, capture_output=True, text=True, timeout=600, check=False
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return project


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_project_notation_cannot_disguise_the_statement(tmp_path: Path) -> None:
    project = _built_module(
        tmp_path,
        "PktNotation",
        "namespace Skel.PktNotation\n"
        'infixl:65 (priority := high) " + " => HMul.hMul\n'
        "theorem addComm' (a b : Nat) : a + b = b + a := Nat.mul_comm a b\n"
        "def disguisedProduct (a b : Nat) : Nat := a + b\n"
        "theorem usesDisguised (a b : Nat) : disguisedProduct a b = a * b := rfl\n"
        "end Skel.PktNotation\n",
    )
    blueprint = _blueprint(
        tmp_path,
        lean={
            "comm": "Skel.PktNotation.addComm'",
            "trusted": "Skel.PktNotation.usesDisguised",
        },
    )

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    declaration = next(
        declaration
        for node in report.nodes
        for declaration in node.declarations
        if declaration.name == "Skel.PktNotation.addComm'"
    )
    # Every notated form reads as addition; the packet must show multiplication.
    assert "a + b = b + a" in declaration.signature
    assert "HMul.hMul." in declaration.raw_signature
    assert "-- raw signature:\n" + declaration.raw_signature in declaration.blind_text()

    uses_disguised = next(
        declaration
        for node in report.nodes
        for declaration in node.declarations
        if declaration.name == "Skel.PktNotation.usesDisguised"
    )
    (trusted,) = uses_disguised.trusted
    assert "a + b" in (trusted.source or "")
    assert '"HMul"' in trusted.semantic and '"hMul"' in trusted.semantic
    assert f"-- canonical kernel material: {trusted.semantic}" in uses_disguised.blind_text()


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_node_selection_does_not_change_a_declarations_evidence(tmp_path: Path) -> None:
    # Only the other article's module declares the notation, but a full
    # extraction imports it and Lean prints the signature with it. The packet
    # `review record` extracts for one article must be the one `prepare` wrote.
    project = _project(tmp_path)
    (project / "Skel" / "PktWrap.lean").write_text(
        "namespace Skel.PktWrap\n"
        "def wrap (n : Nat) : Nat := n\n"
        "theorem wrap_two : wrap 2 = 2 := rfl\n"
        "end Skel.PktWrap\n",
        encoding="utf-8",
    )
    (project / "Skel" / "PktWrapNotation.lean").write_text(
        "import Skel.PktWrap\n"
        "namespace Skel.PktWrap\n"
        'notation "⟪" x "⟫" => wrap x\n'
        "theorem wrap_three : wrap 3 = 3 := rfl\n"
        "end Skel.PktWrap\n",
        encoding="utf-8",
    )
    _build(project, "Skel.PktWrap", "Skel.PktWrapNotation")
    blueprint = _blueprint(
        tmp_path, lean={"two": "Skel.PktWrap.wrap_two", "three": "Skel.PktWrap.wrap_three"}
    )

    full = extract_skeletons(blueprint, lean_root=project)
    scoped = extract_skeletons(blueprint, lean_root=project, node_ids=("basics/two",))

    assert full.clean and scoped.clean
    (declaration,) = full.declarations("basics/two")
    assert declaration.signature == "Skel.PktWrap.wrap_two : ⟪2⟫ = 2"
    assert scoped.node("basics/two") == full.node("basics/two")


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_project_delaborator_cannot_disguise_the_statement(tmp_path: Path) -> None:
    project = _built_module(
        tmp_path,
        "PktDelab",
        "import Lean\n"
        "open Lean PrettyPrinter Delaborator\n"
        "namespace Skel.PktDelab\n"
        "def hidden (a b : Nat) : Prop := a = b\n"
        "@[app_delab hidden] def delabHidden : Delab := do `(True)\n"
        "theorem target (a : Nat) : hidden a a := rfl\n"
        "end Skel.PktDelab\n",
    )
    blueprint = _blueprint(tmp_path, lean={"target": "Skel.PktDelab.target"})

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    declaration = report.nodes[0].declarations[0]
    assert "True" in declaration.signature
    assert "Skel.PktDelab.hidden" in declaration.raw_signature


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_generated_declaration_does_not_borrow_its_parents_source(tmp_path: Path) -> None:
    # `mk_secret` adds definitions without source ranges under the names of
    # `base` and `thm`. Ordinary names are refused. An internal-looking name
    # cannot be proved generated, so it is retained without invented source.
    project = _built_module(
        tmp_path,
        "Rev",
        "import Lean\n"
        "open Lean Elab Command\n"
        "def Skel.Rev.mkSecret (n : Name) (v : Nat) : CoreM Unit := do\n"
        "  let val : DefinitionVal :=\n"
        "    { name := n, levelParams := [], type := mkConst ``Nat,\n"
        "      value := mkNatLit v, hints := .abbrev, safety := .safe }\n"
        "  addDecl (.defnDecl val)\n"
        'elab "mk_secret" : command => liftTermElabM do\n'
        "  Skel.Rev.mkSecret `Skel.Rev.base.secret 7\n"
        "  Skel.Rev.mkSecret `Skel.Rev.thm.secret 8\n"
        "  Skel.Rev.mkSecret `Skel.Rev.base._f 9\n"
        "namespace Skel.Rev\n"
        "def base : Nat := 1\n"
        "mk_secret\n"
        "theorem root : base.secret = 7 := rfl\n"
        "theorem thm : thm.secret = 8 := rfl\n"
        "theorem internal : base._f = 9 := rfl\n"
        "end Skel.Rev\n",
    )
    blueprint = _blueprint(
        tmp_path,
        lean={"root": "Skel.Rev.root", "thm": "Skel.Rev.thm", "internal": "Skel.Rev.internal"},
    )

    report = extract_skeletons(blueprint, lean_root=project)

    assert not report.clean
    assert tuple(issue.message for issue in report.unresolved) == (
        "basics/root: Skel.Rev.root: the skeleton probe omitted required source for "
        "Skel.Rev.root trusted declaration Skel.Rev.base.secret",
        "basics/thm: Skel.Rev.thm: the skeleton probe omitted required source for "
        "Skel.Rev.thm trusted declaration Skel.Rev.thm.secret",
    )
    internal = report.node("basics/internal")
    assert internal is not None and internal.complete
    (declaration,) = internal.declarations
    (helper,) = declaration.trusted
    assert helper.name == "Skel.Rev.base._f"
    assert helper.source is None and helper.source_withheld
    assert "def base : Nat := 1" not in declaration.blind_text()
    assert "-- source not shown" in declaration.blind_text()


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_lean_decides_which_source_text_is_comment(tmp_path: Path) -> None:
    project = _built_module(
        tmp_path,
        "PktStrip",
        "namespace Skel.PktStrip\n"
        'notation:50 a " =-- " b => a ≠ b\n'
        "/--/ KEEPOUT_DOC -/\n"
        "def docOpened : Nat := 6\n"
        "def claim : Prop := 2 + 2 =--\n"
        "  5\n"
        "def joined : Nat := Nat.succ/- KEEPOUT_JOIN -/0\n"
        "def multiline : Nat := Nat.succ/- KEEPOUT_MULTI\n"
        "  STILL_HIDDEN -/0\n"
        "/-- KEEPOUT_ROOT -/\n"
        "theorem stripRoot : docOpened = 6 ∧ claim ∧ joined = 1 ∧ multiline = 1 := -- KEEPOUT_PROOF\n"
        "  ⟨rfl, by unfold claim; decide, rfl, rfl⟩\n"
        "end Skel.PktStrip\n",
    )
    blueprint = _blueprint(tmp_path, lean={"strip": "Skel.PktStrip.stripRoot"})

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    blind = report.nodes[0].blind_text()
    assert "KEEPOUT" not in blind
    assert "STILL_HIDDEN" not in blind
    assert "def docOpened : Nat := 6" in blind
    # The probe cannot reconstruct whether a same-module token was declared
    # before this source, so it does not guess whether `=--` is code.
    assert "def claim : Prop := 2 + 2 =--\n  5" not in blind
    assert "-- source not shown" in blind
    assert re.search(r"def joined : Nat := Nat\.succ +0", blind)
    assert re.search(r"def multiline : Nat := Nat\.succ\n +0", blind)


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_ambiguous_scoped_token_is_withheld_and_where_statement_is_recovered(tmp_path: Path) -> None:
    project = _built_module(
        tmp_path,
        "ParseEdges",
        "namespace Skel.ParseEdges\n"
        "namespace Scope\n"
        'scoped infixl:65 " +-- " => Nat.add\n'
        "end Scope\n"
        "def SECRET (n : Nat) : Nat := n\n"
        "/-\n"
        "open Scope\n"
        "-/\n"
        "def hidden : Nat := 1 +-- SECRET\n"
        "  2\n"
        "def lateHidden : Nat := 1 +--- SECRET\n"
        "  2\n"
        'infixl:65 " +--- " => Nat.add\n'
        "theorem root : hidden = 3 := rfl\n"
        "theorem lateRoot : lateHidden = 3 := rfl\n"
        "structure ProofPair : Prop where\n"
        "  left : True\n"
        "  right : True\n"
        'local notation "⊹" => True.intro\n'
        'local notation "⊙" => (rfl : (1 : Nat) = 1)\n'
        "theorem letStatement : (let n := 1; n = 1) := ⊙\n"
        "theorem whereProof (α : Type) : ProofPair where\n"
        "  left := by exact ⊹\n"
        "  right := by exact ⊹\n"
        "end Skel.ParseEdges\n",
    )
    blueprint = _blueprint(
        tmp_path,
        lean={
            "trap": "Skel.ParseEdges.root",
            "late": "Skel.ParseEdges.lateRoot",
            "let": "Skel.ParseEdges.letStatement",
            "where": "Skel.ParseEdges.whereProof",
        },
    )

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    trap = report.node("basics/trap")
    assert trap is not None
    (hidden,) = trap.declarations[0].trusted
    assert hidden.name == "Skel.ParseEdges.hidden"
    assert hidden.source is None and hidden.source_withheld
    assert "SECRET" not in trap.blind_text()
    late = report.node("basics/late")
    assert late is not None
    (late_hidden,) = late.declarations[0].trusted
    assert late_hidden.name == "Skel.ParseEdges.lateHidden"
    assert late_hidden.source is None and late_hidden.source_withheld
    assert "SECRET" not in late.blind_text()
    let_statement = report.node("basics/let")
    assert let_statement is not None
    assert let_statement.declarations[0].statement == (
        "theorem letStatement : (let n := 1; n = 1)"
    )
    where = report.node("basics/where")
    assert where is not None
    assert where.declarations[0].statement == "theorem whereProof (α : Type) : ProofPair"
    assert not where.declarations[0].source_withheld


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_private_dependency_safety_uses_the_lean_environment(tmp_path: Path) -> None:
    project = _project(tmp_path)
    build = subprocess.run(
        ["lake", "build", "Skel.Semantics"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert build.returncode == 0, build.stderr

    unsafe_blueprint = _blueprint(
        tmp_path / "private-unsafe",
        lean={"unsafe": "Skel.Semantics.usesPrivateUnsafe"},
    )
    unsafe_report = extract_skeletons(unsafe_blueprint, lean_root=project)
    (unsafe_root,) = unsafe_report.nodes[0].declarations
    (unsafe_dependency,) = unsafe_root.trusted
    assert unsafe_dependency.name != "Skel.Semantics.privateUnsafeValue"
    assert json.loads(unsafe_dependency.semantic)["root"]["safety"] == "unsafe"

    partial_blueprint = _blueprint(
        tmp_path / "private-partial",
        lean={"partial": "Skel.Semantics.usesPrivatePartial"},
    )
    _assert_partial_refused(
        extract_skeletons(partial_blueprint, lean_root=project), "Skel.Semantics.privatePartialValue"
    )

    source = project / "Skel" / "Semantics.lean"
    before = source.read_text(encoding="utf-8")
    after = before.replace(
        "if n == 0 then 1 else privatePartialValue (n - 1)",
        "if n == 0 then 2 else privatePartialValue (n - 1)",
    )
    assert after != before
    source.write_text(after, encoding="utf-8")
    rebuild = subprocess.run(
        ["lake", "build", "Skel.Semantics"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert rebuild.returncode == 0, rebuild.stderr

    _assert_partial_refused(
        extract_skeletons(partial_blueprint, lean_root=project), "Skel.Semantics.privatePartialValue"
    )


def _assert_partial_refused(report, dependency: str) -> None:
    """A partial root or dependency leaves its article with no trusted skeleton."""

    assert not report.clean
    assert all(not node.declarations and not node.complete for node in report.nodes)
    reason = f"partial declaration {dependency} cannot be included in a trusted skeleton"
    assert [issue.reason for issue in report.unresolved] == [reason]


def _build(project: Path, *targets: str) -> None:
    build = subprocess.run(
        ["lake", "build", *targets], cwd=project, capture_output=True, text=True, timeout=600, check=False
    )
    assert build.returncode == 0, build.stderr


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_type_and_value_less_roots_show_their_whole_declaration(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build(project, "Skel.Kinds")
    roots = {
        "pair": "Skel.Kinds.Pair",
        "color": "Skel.Kinds.Color",
        "zero": "Skel.Kinds.HasZ",
        "seed": "Skel.Kinds.opaqueSeed",
        "scoped": "Skel.Kinds.Scoped.usesOwnScope",
    }

    report = extract_skeletons(_blueprint(tmp_path, lean=roots), lean_root=project)

    assert report.clean
    assert {d.name: d.statement for node in report.nodes for d in node.declarations} == {
        "Skel.Kinds.Pair": "structure Pair where\n  a : Nat\n  b : Nat",
        "Skel.Kinds.Color": "inductive Color where\n  | red\n  | green",
        "Skel.Kinds.HasZ": "class HasZ (α : Type) where\n  z : α",
        "Skel.Kinds.opaqueSeed": "opaque opaqueSeed : Nat",
        # Notation scoped to the namespace the theorem sits in is active there.
        "Skel.Kinds.Scoped.usesOwnScope": "theorem usesOwnScope : 𝟚 = 2",
    }


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_local_notation_statement_gets_a_packet_without_its_text(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build(project, "Skel.Kinds")
    roots = {"local": "Skel.Kinds.usesLocalNotation", "plain": "Skel.Kinds.plain"}

    report = extract_skeletons(_blueprint(tmp_path, lean=roots), lean_root=project)

    assert report.clean
    by_name = {d.name: d for node in report.nodes for d in node.declarations}
    local = by_name["Skel.Kinds.usesLocalNotation"]
    # `𝟙` does not parse outside the file, so no text is shown; the signatures
    # and kernel material still say what the theorem states.
    assert local.statement is None and local.source is None
    packet = local.blind_text()
    assert "𝟙" not in packet and "-- source not shown" in packet
    assert "OfNat.ofNat.{0} Nat 1" in local.raw_signature

    # The source index cannot see a declaration behind `open … in`; ask the probe.
    probe = render_probe(imports=("Skel.Kinds",), roots=("Skel.Kinds.usesSameLineOpen",), project_roots=("Skel",))
    (record,) = parse_probe_output(run_probe(probe, project)).values()
    assert record["statement_source"] == "theorem usesSameLineOpen : 𝟚 = 2"


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_source_lean_cannot_read_outside_its_file_is_withheld(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build(project, "Skel.Unparsed", "Skel.CommentTokenUse")
    roots = {
        "continued": "Skel.Unparsed.usesContinuedOpen",
        "proof": "Skel.Unparsed.localInProof",
        "local": "Skel.Unparsed.usesLocalDef",
        "blind": "Skel.Unparsed.usesBlindToken",
    }

    def extract(root: Path) -> dict[str, DeclarationSkeleton]:
        report = extract_skeletons(_blueprint(root, lean=roots), lean_root=project)
        assert report.clean
        return {d.name: d for node in report.nodes for d in node.declarations}

    def trusted(declaration: DeclarationSkeleton) -> TrustedDeclaration:
        (item,) = declaration.trusted
        return item

    alone = extract(tmp_path / "alone")
    # `open Other` continues with `Sc` on the next line, so `⟪two⟫` parses and
    # Lean finds the docstring.
    two = trusted(alone["Skel.Unparsed.usesContinuedOpen"])
    assert two.source is not None and "Uses notation" not in alone["Skel.Unparsed.usesContinuedOpen"].blind_text()
    # Only the proof uses local notation: the statement is still shown.
    assert alone["Skel.Unparsed.localInProof"].statement == "theorem localInProof : 1 + 1 = 2"
    # A body with local notation does not parse, and its docstring cannot be
    # told from code: the source is withheld, the node stays clean.
    local = trusted(alone["Skel.Unparsed.usesLocalDef"])
    assert local.source is None and local.source_withheld
    assert "⊞" not in alone["Skel.Unparsed.usesLocalDef"].blind_text()
    # `-- _b +` is a comment in a file without the `+--` token.
    blind = alone["Skel.Unparsed.usesBlindToken"]
    assert "+--" not in blind.blind_text() and not trusted(blind).source_withheld

    # With `+--` in the probe's environment, the probe cannot tell whether that
    # text is a comment in its file, so it shows no source containing it.
    roots["token"] = "Skel.usesCommentToken"
    with_token = extract(tmp_path / "with-token")
    blind = with_token["Skel.Unparsed.usesBlindToken"]
    assert trusted(blind).source_withheld and "+--" not in blind.blind_text()
    # Its own module imports the token, so there it is code.
    assert with_token["Skel.usesCommentToken"].statement == "theorem Skel.usesCommentToken : 2 +-- 3 = 6"
    assert with_token["Skel.Unparsed.usesLocalDef"].hash == alone["Skel.Unparsed.usesLocalDef"].hash


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_rangeless_companions_extract_without_claiming_parent_source(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build(project, "Skel.Kinds", "Skel.Partial")
    roots = {
        "wf": "Skel.Kinds.usesWf",
        "structural": "Skel.Kinds.usesStructural",
        "auto": "Skel.Kinds.usesAutoParam",
        "default": "Skel.Kinds.usesDefault",
        "private": "Skel.Kinds.usesNestedProof",
        "mutual": "Skel.Partial.usesMutual",
    }

    report = extract_skeletons(_blueprint(tmp_path, lean=roots), lean_root=project)

    assert report.clean
    trusted = {item.name: item for node in report.nodes for d in node.declarations for item in d.trusted}
    companions = (
        "Skel.Kinds.wf._unary",
        "Skel.Kinds.fact._f",
        "Skel.Partial.Inner.ev._f",
        "Skel.Kinds.Cfg.x._default",
        "Skel.Kinds.usesAutoParam._auto_1",
    )
    proof = next(name for name in trusted if name.endswith("nestedProof._proof_1"))
    for name in (*companions, proof):
        assert trusted[name].start_line is None
        assert trusted[name].source is None and trusted[name].source_withheld
    # Ordinary, well-founded and structural recursion have `_unsafe_rec`
    # companions too; only a `partial def` is partial.
    for name in ("Skel.Kinds.wf", "Skel.Kinds.wf._unary", "Skel.Kinds.fact", "Skel.Partial.Inner.ev"):
        assert json.loads(trusted[name].semantic)["root"]["safety"] == "safe"


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_partial_dependencies_are_rejected_however_they_are_written(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build(project, "Skel.Partial", "Skel.PublicPartial")
    cases = {
        "Skel.Partial.usesWhere": "Skel.Partial.outer.go",
        "Skel.Partial.usesOwnLine": "Skel.Partial.ownLine",
        "Skel.Partial.usesAfterMutual": "Skel.Partial.Inner.afterMutual",
        "Skel.Partial.usesMacro": "Skel.Partial.fromMacro",
        "Skel.PublicPartial.usesPublic": "Skel.PublicPartial.spin",
    }

    for root, dependency in cases.items():
        blueprint = _blueprint(tmp_path / root, lean={"partial": root})
        _assert_partial_refused(extract_skeletons(blueprint, lean_root=project), dependency)


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_partial_dependency_refuses_only_its_own_article(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    _build(project, "Skel.Partial")
    blueprint = _blueprint(
        tmp_path,
        lean={"ordinary": "Skel.Partial.usesMutual", "partial": "Skel.Partial.usesOwnLine"},
    )

    report = extract_skeletons(blueprint, lean_root=project)

    ordinary, refused = report.nodes
    assert ordinary.node_id == "basics/ordinary" and ordinary.complete
    assert [item.name for item in ordinary.declarations] == ["Skel.Partial.usesMutual"]
    assert refused.node_id == "basics/partial" and refused.declarations == () and refused.hash is None
    assert not report.clean
    assert [issue.message for issue in report.unresolved] == [
        "basics/partial: Skel.Partial.usesOwnLine: partial declaration Skel.Partial.ownLine "
        "cannot be included in a trusted skeleton"
    ]

    packets = tmp_path / "packets"
    arguments = ["skeleton", str(blueprint), "--lean-root", str(project), "--json"]
    assert main([*arguments, "--packets", str(packets)]) == 1
    captured = capsys.readouterr()
    assert "refusing to publish review packets" in captured.err
    assert json.loads(captured.out)["unresolved"] == [issue.as_dict() for issue in report.unresolved]
    assert not packets.exists()


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_external_internal_detail_rotates_the_declaration_hash(tmp_path: Path) -> None:
    project = _project(tmp_path)
    build = subprocess.run(
        ["lake", "build", "Skel.Semantics"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert build.returncode == 0, build.stderr

    roots = (
        "Skel.Semantics.usesExternalDetail",
        "Skel.Semantics.usesExternalMatch",
        "Skel.Semantics.usesExternalPrivate",
    )

    def records() -> dict[str, dict[str, object]]:
        probe = render_probe(
            imports=("Skel.Semantics",),
            roots=roots,
            project_roots=("Skel.Semantics",),
        )
        return parse_probe_output(run_probe(probe, project))

    def declaration(record: dict[str, object]):
        return _declaration(
            record,
            libraries=lean_libraries(project),
            lean_root=project,
            index=index_project(project),
            module_hashes={},
            snapshot_started_ns=None,
        )

    before = records()
    detail = before["Skel.Semantics.usesExternalDetail"]
    assert detail["assumed"] == ["Vendor.visible._helper"]
    assert [item[0] for item in detail["boundary_modules"]] == ["Skel.Vendor"]
    detail_hash = declaration(detail).hash

    matched = before["Skel.Semantics.usesExternalMatch"]
    assert matched["assumed"] == ["Vendor.matchBody"]
    match_semantic = json.loads(dict(matched["assumed_semantics"])["Vendor.matchBody"])
    assert len(match_semantic["generated"]) == 1

    private = before["Skel.Semantics.usesExternalPrivate"]
    assert private["assumed"] == ["Vendor.usesPrivate"]
    assert all("privateHelper" not in name for name in private["assumed"])

    source = project / "Skel" / "Vendor.lean"
    text = source.read_text(encoding="utf-8")
    changed_text = text.replace("def visible._helper : Nat := 1", "def visible._helper : Nat := 2")
    assert changed_text != text
    source.write_text(changed_text, encoding="utf-8")
    rebuild = subprocess.run(
        ["lake", "build", "Skel.Semantics"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert rebuild.returncode == 0, rebuild.stderr

    changed_detail = records()["Skel.Semantics.usesExternalDetail"]
    assert changed_detail["semantic"] == detail["semantic"]
    assert changed_detail["assumed_semantics"] != detail["assumed_semantics"]
    assert declaration(changed_detail).hash != detail_hash


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_shared_probe_tables_keep_every_hash(tmp_path: Path) -> None:
    # The probe states each trusted declaration, semantic material, and module
    # once per run. Drift hashes must survive sharing; the full golden also
    # records intentional changes to the packet evidence presented to a reader.
    project = _project(tmp_path)
    build = subprocess.run(["lake", "build"], cwd=project, capture_output=True, text=True, timeout=600, check=False)
    assert build.returncode == 0, build.stderr
    runs = {
        "Skel": (
            "Skel.observation_determined",
            "Skel.supervision_nonAmbiguous",
            "Skel.supervision",
            "Skel.heavy_of_weight",
            "Skel.heavy_of_notation",
            "Skel.Kinds.usesWf",
            "Skel.Kinds.usesStructural",
            "Skel.Kinds.usesDefault",
            "Skel.Semantics.usesFieldOrder",
            "Skel.AxiomUse.result",
        ),
        "Skel.Semantics": (
            "Skel.Semantics.selectedProposition",
            "Skel.Semantics.usesExternalDetail",
            "Skel.Semantics.usesExternalMatch",
            "Skel.Semantics.usesExternalPrivate",
            "Skel.Semantics.usesVendorMacro",
            "Skel.Semantics.usesVendorWf",
            "Skel.Semantics.usesVendorModule",
            "Skel.Semantics.usesVendorPrivateAxiom",
        ),
    }
    hashes: dict[str, list[object]] = {}
    for project_root, roots in runs.items():
        probe = render_probe(imports=("Skel",), roots=roots, project_roots=(project_root,))
        output = run_probe(probe, project)
        records = parse_probe_output(output, expected_roots=roots)
        # Two roots here trust `Skel.NonAmbiguous`; its record is stated once.
        assert output.count('"source_name":"Skel.NonAmbiguous"') == (project_root == "Skel")
        assert '"table":"fragment"' in output
        declarations = []
        for root in roots:
            assert _probe_record_issue(records[root]) is None, root
            declaration = _declaration(
                records[root],
                libraries=lean_libraries(project),
                lean_root=project,
                index=index_project(project),
                module_hashes={},
                snapshot_started_ns=None,
            )
            declarations.append(declaration)
            hashes[f"{project_root}:{root}"] = [declaration.hash, declaration.evidence_hash]
        node = NodeSkeleton(node_id=project_root, article_path="a.md", declarations=tuple(declarations))
        hashes[project_root] = [node.hash, node.evidence_hash, node.review_hash]
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    assert digest == "3db5dbb9571689ba28c8877c4f25661a64db9768ada86293e0116cc5a17b6238", f"{digest}\n{json.dumps(hashes, indent=1)}"


def _build_semantics(project: Path) -> None:
    build = subprocess.run(
        ["lake", "build", "Skel.Semantics"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert build.returncode == 0, build.stderr


def _probe_declaration(project: Path, root: str, *, module: str = "Skel.Semantics"):
    probe = render_probe(imports=(module,), roots=(root,), project_roots=(module,))
    record = parse_probe_output(run_probe(probe, project))[root]
    declaration = _declaration(
        record,
        libraries=lean_libraries(project),
        lean_root=project,
        index=index_project(project),
        module_hashes={},
        snapshot_started_ns=None,
    )
    return record, declaration


def _replace_source(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_vendor_macro_outside_the_boundary_rotates_the_declaration_hash(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build_semantics(project)
    root = "Skel.Semantics.usesVendorMacro"
    record, before = _probe_declaration(project, root)
    # A macro leaves no constant behind, so the module that defines it is not
    # bound; only the compiled module that expanded it witnesses the change.
    assert [item[0] for item in record["boundary_modules"]] == ["Skel.VendorMacroUse"]

    _replace_source(project / "Skel" / "VendorMacro.lean", "((1 : Nat))", "((2 : Nat))")
    _build_semantics(project)
    changed_record, changed = _probe_declaration(project, root)

    assert changed_record["assumed_semantics"] == record["assumed_semantics"]
    assert changed.hash != before.hash


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_external_wf_helper_in_another_module_rotates_the_declaration_hash(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build_semantics(project)
    root = "Skel.Semantics.usesVendorWf"
    record, before = _probe_declaration(project, root)
    # `wfWalk` reaches `wfHelper` only through its generated `_unary` body.
    assert [item[0] for item in record["boundary_modules"]] == ["Skel.VendorWf", "Skel.VendorWfHelper"]

    _replace_source(project / "Skel" / "VendorWfHelper.lean", "n + 1", "n + 2")
    _build_semantics(project)
    changed_record, changed = _probe_declaration(project, root)

    assert changed_record["assumed_semantics"] == record["assumed_semantics"]
    assert changed.hash != before.hash


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_external_private_helper_dependency_in_another_module_rotates_the_hash(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build_semantics(project)
    root = "Skel.Semantics.usesVendorPrivateChain"
    record, before = _probe_declaration(project, root)
    assert [item[0] for item in record["boundary_modules"]] == ["Skel.VendorPrivA", "Skel.VendorPrivB"]

    _replace_source(project / "Skel" / "VendorPrivB.lean", "privateTarget : Nat := 1", "privateTarget : Nat := 2")
    _build_semantics(project)
    changed_record, changed = _probe_declaration(project, root)

    assert changed_record["assumed_semantics"] == record["assumed_semantics"]
    assert changed.hash != before.hash


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_external_private_axiom_binds_its_module(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build_semantics(project)
    record, _ = _probe_declaration(project, "Skel.Semantics.usesVendorPrivateAxiom")

    assert record["axioms"] == ["_private.Skel.VendorPrivAxiom.0.Vendor.hiddenAxiom"]
    assert [item[0] for item in record["boundary_modules"]] == ["Skel.VendorPrivAxiom"]


def _project_with_core_named_dependency(tmp_path: Path, module: str) -> Path:
    """Write a fixture copy that requires a package whose library is named ``module``."""

    project = _project(tmp_path)
    dependency = project / "dep"
    (dependency / module.split(".")[0]).mkdir(parents=True)
    shutil.copy(_FIXTURE / "lean-toolchain", dependency / "lean-toolchain")
    (dependency / "lakefile.toml").write_text(
        f'name = "dep"\n\n[[lean_lib]]\nname = "Dep"\nroots = ["{module}"]\n', encoding="utf-8"
    )
    (dependency / (module.replace(".", "/") + ".lean")).write_text(
        f"namespace {module}\ndef magic : Nat := 1\nend {module}\n", encoding="utf-8"
    )
    with (project / "lakefile.toml").open("a", encoding="utf-8") as lakefile:
        lakefile.write('\n[[require]]\nname = "dep"\npath = "dep"\n')
    (project / "Skel" / "UsesDep.lean").write_text(
        f"import {module}\nnamespace Skel.UsesDep\ndef val : Nat := {module}.magic\n"
        "theorem root (h : val = 1) : val = 1 := h\nend Skel.UsesDep\n",
        encoding="utf-8",
    )
    return project


def _build_uses_dep(project: Path) -> None:
    build = subprocess.run(
        ["lake", "build", "Skel.UsesDep"], cwd=project, capture_output=True, text=True, timeout=600, check=False
    )
    assert build.returncode == 0, build.stderr


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_dependency_module_under_a_core_name_is_bound(tmp_path: Path) -> None:
    project = _project_with_core_named_dependency(tmp_path, "Lake.Vendor")
    _build_uses_dep(project)
    root = "Skel.UsesDep.root"
    record, before = _probe_declaration(project, root, module="Skel.UsesDep")

    # Only the toolchain's own modules are core; a package may reuse the name.
    assert record["assumed"] == ["Lake.Vendor.magic"]
    assert [item[:2] for item in record["boundary_modules"]] == [["Lake.Vendor", "olean"]]

    _replace_source(project / "dep" / "Lake" / "Vendor.lean", ":= 1", ":= 2")
    _build_uses_dep(project)
    _, changed = _probe_declaration(project, root, module="Skel.UsesDep")

    assert changed.hash != before.hash


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_dependency_shadowing_a_toolchain_library_fails_closed(tmp_path: Path) -> None:
    project = _project_with_core_named_dependency(tmp_path, "Std.Vendor")
    _build_uses_dep(project)

    with pytest.raises(SkeletonError, match="cannot load toolchain module Std\\..*hides the toolchain's own `Std`"):
        _probe_declaration(project, "Skel.UsesDep.root", module="Skel.UsesDep")


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_structure_field_order_rotates_the_declaration_hash(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build_semantics(project)
    root = "Skel.Semantics.usesFieldOrder"
    _, before = _probe_declaration(project, root)

    # Swapping fields keeps the constructor type and the projection name; only
    # the projection body says which field `first` selects.
    _replace_source(
        project / "Skel" / "Semantics.lean",
        "  first : Nat\n  second : Nat\n",
        "  second : Nat\n  first : Nat\n",
    )
    _build_semantics(project)
    _, changed = _probe_declaration(project, root)

    assert changed.hash != before.hash


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_boundary_module_identity_is_checkout_path_independent(tmp_path: Path) -> None:
    roots = ("Skel.Semantics.usesVendorMacro", "Skel.Semantics.usesVendorModule")
    (tmp_path / "first").mkdir()
    (tmp_path / "second" / "nested").mkdir(parents=True)
    identities = []
    for project in (_project(tmp_path / "first"), _project(tmp_path / "second" / "nested")):
        _build_semantics(project)
        probed = {root: _probe_declaration(project, root) for root in roots}
        identities.append({root: (item.hash, item.boundary_modules) for root, (_, item) in probed.items()})
    assert identities[0] == identities[1]

    # A module-system build splits its artifact; every part is bound.
    record, _ = probed["Skel.Semantics.usesVendorModule"]
    assert [item[:2] for item in record["boundary_modules"]] == [
        ["Skel.VendorModule", "olean"],
        ["Skel.VendorModule", "olean.server"],
        ["Skel.VendorModule", "olean.private"],
    ]

    started = time.time_ns()
    private_part = Path(record["boundary_modules"][2][2])
    os.utime(private_part, ns=(started + 10**9, started + 10**9))
    with pytest.raises(SkeletonError, match="changed during skeleton extraction"):
        _hash_module_files(
            record["boundary_modules"],
            lean_root=project,
            cache={},
            snapshot_started_ns=started,
        )


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_probe_semantics_cover_elaboration_and_the_full_trust_boundary(tmp_path: Path) -> None:
    project = _project(tmp_path)
    build = subprocess.run(["lake", "build"], cwd=project, capture_output=True, text=True, timeout=600, check=False)
    assert build.returncode == 0, build.stderr

    roots = (
        "Skel.Semantics.expandedMacro",
        "Skel.Semantics.firstOnLine",
        "Skel.Semantics.matchBody",
        "Skel.Semantics.safeValue",
        "Skel.Semantics.secondOnLine",
        "Skel.Semantics.usesOpaque",
        "Skel.Semantics.selectedProposition",
        "Skel.Semantics.unsafeValue",
        "Skel.Semantics.universeNamed",
        "Skel.Semantics.usesQuoted",
        "Skel.Semantics.visible",
    )

    def records() -> dict[str, dict[str, object]]:
        probe = render_probe(
            imports=("Skel.Semantics",),
            roots=roots,
            # A dotted Lake root must include this module, but not Skel.Vendor.
            project_roots=("Skel.Semantics",),
        )
        return parse_probe_output(run_probe(probe, project))

    before = records()
    macro = before["Skel.Semantics.expandedMacro"]
    assert macro["semantic_schema"] == SEMANTIC_SCHEMA
    assert set(json.loads(str(macro["semantic"]))["root"]) == {
        "safety",
        "type",
        "value",
    }

    opaque = before["Skel.Semantics.usesOpaque"]
    assert [item["name"] for item in opaque["trusted"]] == [
        "Skel.Semantics.opaqueSeed",
        "Skel.Semantics.opaqueWitness",
    ]
    opaque_item = next(item for item in opaque["trusted"] if item["name"].endswith("opaqueWitness"))
    assert set(json.loads(opaque_item["semantic"])["root"]) == {
        "safety",
        "type",
        "value",
    }

    selected = before["Skel.Semantics.selectedProposition"]
    assert selected["trusted"] == []
    assert selected["assumed"] == ["Vendor.instChoice"]
    assert [item[0] for item in selected["assumed_semantics"]] == [
        "Vendor.instChoice",
    ]
    assert [item[0] for item in selected["boundary_modules"]] == ["Skel.Vendor"]
    before_modules = _hash_module_files(selected["boundary_modules"], lean_root=project, cache={})
    local_probe = render_probe(
        imports=("Skel.Semantics",),
        roots=("Skel.Semantics.selectedProposition",),
        project_roots=("Skel",),
    )
    local_selected = parse_probe_output(run_probe(local_probe, project))[
        "Skel.Semantics.selectedProposition"
    ]
    local_semantics = {item["name"]: item["semantic"] for item in local_selected["trusted"]}
    assert "Vendor.instChoice" in local_semantics
    assert "Vendor.selectedChoice" in local_semantics
    quoted = before["Skel.Semantics.usesQuoted"]
    assert [item["name"] for item in quoted["trusted"]] == ["Skel.Semantics.«quoted.helper»"]

    matched = before["Skel.Semantics.matchBody"]
    assert matched["trusted"] == []
    generated = json.loads(str(matched["semantic"]))["generated"]
    assert len(generated) == 1
    assert isinstance(generated[0]["name"], dict)

    visible = before["Skel.Semantics.visible"]
    assert [item["name"] for item in visible["trusted"]] == [
        "Skel.Semantics.visible._helper"
    ]
    helper_before = visible["trusted"][0]["semantic"]

    first_on_line = before["Skel.Semantics.firstOnLine"]
    assert first_on_line["source"] == "def firstOnLine : Nat := 1"
    assert "secondOnLine" not in first_on_line["source"]
    second_on_line = before["Skel.Semantics.secondOnLine"]
    assert second_on_line["statement_source"] == "theorem secondOnLine : firstOnLine = 1"
    assert "firstOnLine : Nat := 1" not in second_on_line["statement_source"]
    assert "rfl" not in second_on_line["statement_source"]

    assert json.loads(str(before["Skel.Semantics.safeValue"]["semantic"]))["root"][
        "safety"
    ] == "safe"
    assert json.loads(str(before["Skel.Semantics.unsafeValue"]["semantic"]))[
        "root"
    ]["safety"] == "unsafe"
    universe_before = before["Skel.Semantics.universeNamed"]["semantic"]

    source = project / "Skel" / "Semantics.lean"
    text = source.read_text(encoding="utf-8")
    source.write_text(
        text.replace("| `(semanticMacro) => `(1)", "| `(semanticMacro) => `(2)")
        .replace(
            "  | 0 => 10\n  | n + 1 => n",
            "  | 1 => 10\n  | n => n",
        )
        .replace("def visible._helper : Nat := 1", "def visible._helper : Nat := 2")
        .replace(
            "universe u\n\ndef universeNamed (α : Type u) : Type u := α",
            "universe v\n\ndef universeNamed (α : Type v) : Type v := α",
        ),
        encoding="utf-8",
    )
    rebuild = subprocess.run(
        ["lake", "build", "Skel.Semantics"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert rebuild.returncode == 0, rebuild.stderr
    changed = records()
    after = changed["Skel.Semantics.expandedMacro"]
    assert after["signature"] == macro["signature"]
    assert after["statement_source"] == macro["statement_source"]
    assert after["semantic"] != macro["semantic"]
    changed_match = changed["Skel.Semantics.matchBody"]
    assert changed_match["signature"] == matched["signature"]
    assert changed_match["semantic"] != matched["semantic"]
    assert changed_match["trusted"] == []
    changed_visible = changed["Skel.Semantics.visible"]
    assert changed_visible["semantic"] == visible["semantic"]
    assert changed_visible["trusted"][0]["semantic"] != helper_before
    assert changed["Skel.Semantics.universeNamed"]["semantic"] == universe_before

    vendor = project / "Skel" / "Vendor.lean"
    vendor.write_text(
        vendor.read_text(encoding="utf-8").replace("⟨True⟩", "⟨False⟩"),
        encoding="utf-8",
    )
    rebuild = subprocess.run(
        ["lake", "build", "Skel.Semantics"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert rebuild.returncode == 0, rebuild.stderr
    changed_instance = records()["Skel.Semantics.selectedProposition"]
    assert changed_instance["semantic"] == selected["semantic"]
    assert changed_instance["assumed"] == selected["assumed"]
    assert changed_instance["assumed_semantics"] == selected["assumed_semantics"]
    assert _hash_module_files(
        changed_instance["boundary_modules"], lean_root=project, cache={}
    ) != before_modules
    changed_local = parse_probe_output(run_probe(local_probe, project))[
        "Skel.Semantics.selectedProposition"
    ]
    changed_local_semantics = {
        item["name"]: item["semantic"] for item in changed_local["trusted"]
    }
    assert changed_local_semantics["Vendor.selectedChoice"] != local_semantics[
        "Vendor.selectedChoice"
    ]


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_axiom_types_are_part_of_the_trust_boundary(tmp_path: Path) -> None:
    project = _project(tmp_path)
    build = subprocess.run(["lake", "build"], cwd=project, capture_output=True, text=True, timeout=600, check=False)
    assert build.returncode == 0, build.stderr

    external_probe = render_probe(
        imports=("Skel.AxiomUse",),
        roots=("Skel.AxiomUse.result",),
        project_roots=("Skel.AxiomUse",),
    )
    external = parse_probe_output(run_probe(external_probe, project))["Skel.AxiomUse.result"]
    assert external["assumed"] == ["AxiomVendor.P"]
    assert [item[0] for item in external["boundary_modules"]] == ["Skel.AxiomVendor"]
    external_modules = _hash_module_files(external["boundary_modules"], lean_root=project, cache={})

    local_probe = render_probe(
        imports=("Skel.AxiomUse",),
        roots=("Skel.AxiomUse.result",),
        project_roots=("Skel",),
    )
    local = parse_probe_output(run_probe(local_probe, project))["Skel.AxiomUse.result"]
    assert [item["name"] for item in local["trusted"]] == ["AxiomVendor.P"]
    proposition = local["trusted"][0]

    source = project / "Skel" / "AxiomVendor.lean"
    source.write_text(
        source.read_text(encoding="utf-8").replace("def P : Prop := True", "def P : Prop := False"),
        encoding="utf-8",
    )
    rebuild = subprocess.run(
        ["lake", "build", "Skel.AxiomUse"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert rebuild.returncode == 0, rebuild.stderr

    changed_external = parse_probe_output(run_probe(external_probe, project))["Skel.AxiomUse.result"]
    assert _hash_module_files(
        changed_external["boundary_modules"], lean_root=project, cache={}
    ) != external_modules
    changed_local = parse_probe_output(run_probe(local_probe, project))["Skel.AxiomUse.result"]
    changed_proposition = changed_local["trusted"][0]
    assert changed_proposition["name"] == proposition["name"]
    assert changed_proposition["semantic"] != proposition["semantic"]


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_statement_parsing_does_not_leak_scoped_notation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    build = subprocess.run(["lake", "build"], cwd=project, capture_output=True, text=True, timeout=600, check=False)
    assert build.returncode == 0, build.stderr
    probe = render_probe(
        imports=("Skel.ScopedA",),
        roots=("Skel.ScopedA.activatesScope",),
        project_roots=("Skel",),
    )
    probe += """
open Lean Elab Command
run_cmd do
  let _ ← AutoformSkeleton.statementSource `Skel.ScopedA.activatesScope
  let env ← getEnv
  match Parser.runParserCategory env `command
      "example : ⟬marker⟭ = Skel.Semantics.notationMarker := rfl" with
  | .error _ => pure ()
  | .ok _ => throwError "scoped notation escaped statementSource"
"""

    record = parse_probe_output(run_probe(probe, project))["Skel.ScopedA.activatesScope"]
    assert "⟬marker⟭" in record["statement_source"]


# --------------------------------------------------------------------------- #
# Source passages
# --------------------------------------------------------------------------- #


def test_a_line_locator_on_a_source_file_yields_the_passage(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    source = blueprint / "sources" / "book.tex"
    source.parent.mkdir()
    # A form feed inside an earlier line, as `pdftotext` writes between pages,
    # must not count as a line break: locators are what `sed` counts.
    source.write_text("\n".join(f"line {n}" + ("\x0c" if n == 2 else "") for n in range(1, 21)) + "\n", encoding="utf-8")
    article = blueprint / "roadmap" / "basics" / "determined.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "## Depends on",
            "## Sources\n\n- [notes](../../sources/notes.md)\n"
            "- [external](https://example.com/paper.tex#L1-L2)\n"
            "- [Theorem 2](../../sources/book.tex#L5-L7)\n\n## Depends on",
        ),
        encoding="utf-8",
    )
    (blueprint / "sources" / "notes.md").write_text("# Notes\n", encoding="utf-8")

    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())

    (node,) = report.nodes
    assert node.passage == "line 5\nline 6\nline 7"
    assert node.passage_locator == "sources/book.tex#L5-L7"
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    write_packets(report, packets, passages=passages)
    assert (passages / "basics" / "determined" / "passage.txt").read_text(encoding="utf-8") == "line 5\nline 6\nline 7\n"
    manifest = json.loads((packets / PACKET_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["packets"][0]["passage"] == "basics/determined/passage.txt"
    assert manifest["packets"][0]["article_packet"] == "basics/determined/article.lean"
    assert manifest["packets"][0]["packet_hash"] == report.nodes[0].declarations[0].evidence_hash
    assert manifest["packets"][0]["article_packet_hash"] == report.nodes[0].evidence_hash
    assert manifest["packets"][0]["review_hash"] == report.nodes[0].review_hash
    passage_entry = json.loads((passages / PACKET_MANIFEST).read_text(encoding="utf-8"))[
        "passages"
    ][0]
    passage_bytes = (passages / passage_entry["passage"]).read_bytes()
    assert passage_entry["hash"] == "sha256:" + hashlib.sha256(passage_bytes).hexdigest()
    # The passage never enters the blind packet.
    packet_path = packets / manifest["packets"][0]["packet"]
    assert "line 5" not in packet_path.read_text(encoding="utf-8")
    assert load_skeleton_report_roundtrip(report, tmp_path)


def test_packet_publication_refuses_an_incomplete_report(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(
        blueprint,
        lean_root=project,
        runner=lambda probe, root: _fake_probe_output(),
    )
    incomplete = replace(
        report,
        unresolved=(
            UnresolvedTarget("basics/determined", "Skel.observation_determined", "missing"),
        ),
    )
    packets = tmp_path / "packets"

    with pytest.raises(SkeletonError, match="incomplete skeleton report"):
        write_packets(incomplete, packets)

    assert not packets.exists()


def test_cli_does_not_publish_packets_for_unresolved_declarations(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(
        tmp_path,
        lean={"determined": "Skel.observation_determined", "missing": "Skel.absent"},
    )
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", lambda probe, root: _fake_probe_output())
    packets = tmp_path / "packets"
    report_path = tmp_path / "skeleton.json"

    result = main(
        [
            "skeleton",
            str(blueprint),
            "--lean-root",
            str(project),
            "--output",
            str(report_path),
            "--packets",
            str(packets),
        ]
    )

    assert result == 1
    assert not packets.exists()
    assert load_skeleton_report(report_path).unresolved
    assert "refusing to publish review packets" in capsys.readouterr().err


def test_packet_publication_replaces_stale_managed_output(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"

    write_packets(report, packets, passages=passages)
    stale_packet = packets / "stale.lean"
    stale_passage = passages / "stale.txt"
    stale_packet.write_text("stale\n", encoding="utf-8")
    stale_passage.write_text("stale\n", encoding="utf-8")
    for root, schema in (
        (packets, "autoform-skeleton-packets/v1"),
        (passages, "autoform-skeleton-passages/v1"),
    ):
        manifest_path = root / PACKET_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema"] = schema
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    write_packets(report, packets, passages=passages)

    assert not stale_packet.exists()
    assert not stale_passage.exists()
    assert json.loads((packets / PACKET_MANIFEST).read_text(encoding="utf-8"))["schema"] == PACKET_SCHEMA
    passage_manifest = json.loads((passages / PACKET_MANIFEST).read_text(encoding="utf-8"))
    assert passage_manifest["kind"] == "passages"
    assert passage_manifest["schema"] == PASSAGE_SCHEMA


def test_packet_publication_refuses_unmanaged_or_symlink_output(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    unmanaged = tmp_path / "unmanaged"
    unmanaged.mkdir()
    (unmanaged / "keep.txt").write_text("mine\n", encoding="utf-8")

    with pytest.raises(SkeletonError, match="non-Autoform packet output"):
        write_packets(report, unmanaged)

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SkeletonError, match="symlink packet output"):
        write_packets(report, linked)

    fake_managed = tmp_path / "fake-managed"
    fake_managed.mkdir()
    (fake_managed / PACKET_MANIFEST).write_text(report.to_json(), encoding="utf-8")
    valuable = fake_managed / "valuable.txt"
    valuable.write_text("keep me\n", encoding="utf-8")
    with pytest.raises(SkeletonError, match="non-Autoform packet output"):
        write_packets(report, fake_managed)
    assert valuable.read_text(encoding="utf-8") == "keep me\n"


def test_packet_filenames_cannot_collide_by_case_or_with_article_packet(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    node = report.nodes[0]
    declaration = node.declarations[0]
    report = replace(
        report,
        targets=((node.node_id, ("Foo.x", "foo.x", "article")),),
        nodes=(
            replace(
                node,
                declarations=(
                    replace(declaration, name="Foo.x"),
                    replace(declaration, name="foo.x"),
                    replace(declaration, name="article"),
                ),
            ),
        ),
    )

    packets = tmp_path / "packets"
    write_packets(report, packets)
    manifest = json.loads((packets / PACKET_MANIFEST).read_text(encoding="utf-8"))
    relative_paths = [entry["packet"] for entry in manifest["packets"]]

    assert len({path.casefold() for path in relative_paths}) == 3
    assert all((packets / path).is_file() for path in relative_paths)
    assert (packets / "basics" / "determined" / "article.lean").is_file()


def test_packet_publication_rolls_back_both_trees_on_commit_failure(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    write_packets(report, packets, passages=passages)
    (packets / "old-marker").write_text("old packets\n", encoding="utf-8")
    (passages / "old-marker").write_text("old passages\n", encoding="utf-8")

    install_output = _install_output

    def fail_passage_install(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        if "autoform-stage" in source_path.name and Path(destination) == passages:
            raise OSError("simulated passage commit failure")
        install_output(source_path, Path(destination))

    monkeypatch.setattr("autoform_cli.skeleton._install_output", fail_passage_install)

    with pytest.raises(SkeletonError, match="could not publish skeleton output"):
        write_packets(report, packets, passages=passages)

    assert (packets / "old-marker").read_text(encoding="utf-8") == "old packets\n"
    assert (passages / "old-marker").read_text(encoding="utf-8") == "old passages\n"


def test_packet_publication_rolls_back_both_trees_when_interrupted(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(
        blueprint,
        lean_root=project,
        runner=lambda probe, root: _fake_probe_output(),
    )
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    write_packets(report, packets, passages=passages)
    (packets / "old-marker").write_text("old packets\n", encoding="utf-8")
    (passages / "old-marker").write_text("old passages\n", encoding="utf-8")
    install_output = _install_output

    def interrupt_passage_install(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        install_output(source_path, Path(destination))
        if "autoform-stage" in source_path.name and Path(destination) == passages:
            raise KeyboardInterrupt

    monkeypatch.setattr("autoform_cli.skeleton._install_output", interrupt_passage_install)

    with pytest.raises(KeyboardInterrupt):
        write_packets(report, packets, passages=passages)

    assert (packets / "old-marker").read_text(encoding="utf-8") == "old packets\n"
    assert (passages / "old-marker").read_text(encoding="utf-8") == "old passages\n"


def test_packet_publication_rolls_back_when_interrupted_after_backup_rename(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(
        blueprint,
        lean_root=project,
        runner=lambda probe, root: _fake_probe_output(),
    )
    packets = tmp_path / "packets"
    write_packets(report, packets)
    marker = packets / "old-marker"
    marker.write_text("old packets\n", encoding="utf-8")
    replace_path = os.replace

    def interrupt_after_backup(source: str | Path, destination: str | Path) -> None:
        replace_path(source, destination)
        if Path(source) == packets and "autoform-backup" in Path(destination).name:
            raise KeyboardInterrupt

    monkeypatch.setattr("autoform_cli.skeleton.os.replace", interrupt_after_backup)

    with pytest.raises(KeyboardInterrupt):
        write_packets(report, packets)

    assert marker.read_text(encoding="utf-8") == "old packets\n"
    assert list(tmp_path.glob(".packets.autoform-backup-*")) == []


def test_packet_publication_preserves_a_changed_backup_during_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(
        blueprint,
        lean_root=project,
        runner=lambda probe, root: _fake_probe_output(),
    )
    packets = tmp_path / "packets"
    write_packets(report, packets)
    (packets / "old-marker").write_text("old packets\n", encoding="utf-8")
    install_output = _install_output
    changed_backup: Path | None = None

    def change_backup_after_install(source: str | Path, destination: str | Path) -> None:
        nonlocal changed_backup
        install_output(Path(source), Path(destination))
        if "autoform-stage" in Path(source).name and Path(destination) == packets:
            (changed_backup,) = tmp_path.glob(".packets.autoform-backup-*")
            (changed_backup / "concurrent-marker").write_text("keep me\n", encoding="utf-8")

    monkeypatch.setattr("autoform_cli.skeleton._install_output", change_backup_after_install)

    with pytest.warns(RuntimeWarning, match="backup changed.*preserved"):
        write_packets(report, packets)

    assert changed_backup is not None
    assert (changed_backup / "concurrent-marker").read_text(encoding="utf-8") == "keep me\n"
    assert not (packets / "old-marker").exists()


def test_packet_publication_does_not_delete_a_concurrent_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    write_packets(report, packets, passages=passages)
    (packets / "old-marker").write_text("old packets\n", encoding="utf-8")
    replace = os.replace
    install_output = _install_output

    def replace_then_fail(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if "autoform-stage" in source_path.name and destination_path == passages:
            displaced = tmp_path / "displaced-packets"
            replace(packets, displaced)
            packets.mkdir()
            (packets / "valuable").write_text("concurrent publisher\n", encoding="utf-8")
            raise OSError("simulated passage commit failure")
        install_output(source_path, destination_path)

    monkeypatch.setattr("autoform_cli.skeleton._install_output", replace_then_fail)

    with pytest.raises(SkeletonError, match="preserved"):
        write_packets(report, packets, passages=passages)

    assert (packets / "valuable").read_text(encoding="utf-8") == "concurrent publisher\n"
    backups = list(tmp_path.glob(".packets.autoform-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "old-marker").read_text(encoding="utf-8") == "old packets\n"


def test_packet_publication_does_not_replace_a_concurrent_empty_directory(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    write_packets(report, packets)
    (packets / "old-marker").write_text("old packets\n", encoding="utf-8")
    install_output = _install_output
    concurrent_inode: int | None = None

    def create_before_install(stage: Path, destination: Path) -> None:
        nonlocal concurrent_inode
        if destination == packets:
            destination.mkdir()
            concurrent_inode = destination.stat().st_ino
        install_output(stage, destination)

    monkeypatch.setattr("autoform_cli.skeleton._install_output", create_before_install)

    with pytest.raises(SkeletonError, match="published output changed during rollback"):
        write_packets(report, packets)

    assert concurrent_inode is not None
    assert packets.stat().st_ino == concurrent_inode
    assert list(packets.iterdir()) == []
    backups = list(tmp_path.glob(".packets.autoform-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "old-marker").read_text(encoding="utf-8") == "old packets\n"


def test_packet_publication_preflights_no_replace_before_moving_old_tree(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    write_packets(report, packets)
    marker = packets / "old-marker"
    marker.write_text("old packets\n", encoding="utf-8")

    def unavailable(source: Path, destination: Path) -> None:
        raise SkeletonError(["atomic no-replace rename is unavailable"])

    monkeypatch.setattr("autoform_cli.skeleton._rename_no_replace", unavailable)

    with pytest.raises(SkeletonError, match="no-replace rename is unavailable"):
        write_packets(report, packets)

    assert marker.read_text(encoding="utf-8") == "old packets\n"
    assert list(tmp_path.glob(".packets.autoform-backup-*")) == []
    assert list(tmp_path.glob(".packets.autoform-preflight-*")) == []


def test_packet_publication_cleans_up_an_interrupted_preflight(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    write_packets(report, packets)
    marker = packets / "old-marker"
    marker.write_text("old packets\n", encoding="utf-8")
    install_output = _install_output

    def interrupt_after_preflight_install(stage: Path, destination: Path) -> None:
        install_output(stage, destination)
        if "autoform-preflight" in destination.parent.name:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        "autoform_cli.skeleton._install_output", interrupt_after_preflight_install
    )

    with pytest.raises(KeyboardInterrupt):
        write_packets(report, packets)

    assert marker.read_text(encoding="utf-8") == "old packets\n"
    assert list(tmp_path.glob(".packets.autoform-backup-*")) == []
    assert list(tmp_path.glob(".packets.autoform-preflight-*")) == []


def test_packet_publication_restores_old_trees_when_quarantine_cleanup_fails(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    write_packets(report, packets, passages=passages)
    (packets / "old-marker").write_text("old packets\n", encoding="utf-8")
    (passages / "old-marker").write_text("old passages\n", encoding="utf-8")
    install_output = _install_output
    remove_output = _remove_output

    def fail_passage_install(stage: Path, destination: Path) -> None:
        if "autoform-stage" in stage.name and destination == passages:
            raise OSError("simulated passage install failure")
        install_output(stage, destination)

    def fail_quarantine_cleanup(path: Path) -> None:
        if "autoform-rollback" in path.name:
            raise OSError("simulated quarantine cleanup failure")
        remove_output(path)

    monkeypatch.setattr("autoform_cli.skeleton._install_output", fail_passage_install)
    monkeypatch.setattr("autoform_cli.skeleton._remove_output", fail_quarantine_cleanup)

    with pytest.raises(SkeletonError, match="preserved for recovery"):
        write_packets(report, packets, passages=passages)

    assert (packets / "old-marker").read_text(encoding="utf-8") == "old packets\n"
    assert (passages / "old-marker").read_text(encoding="utf-8") == "old passages\n"
    quarantines = list(tmp_path.glob(".packets.autoform-rollback-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / PACKET_MANIFEST).is_file()


def test_packet_publication_does_not_overwrite_during_backup_restore(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    write_packets(report, packets, passages=passages)
    (packets / "old-marker").write_text("old packets\n", encoding="utf-8")
    (passages / "old-marker").write_text("old passages\n", encoding="utf-8")
    install_output = _install_output
    rename_no_replace = _rename_no_replace
    concurrent_inode: int | None = None

    def fail_passage_install(stage: Path, destination: Path) -> None:
        if "autoform-stage" in stage.name and destination == passages:
            raise OSError("simulated passage install failure")
        install_output(stage, destination)

    def create_before_restore(source: Path, destination: Path) -> None:
        nonlocal concurrent_inode
        if destination == packets and "autoform-backup" in source.name:
            destination.mkdir()
            concurrent_inode = destination.stat().st_ino
        rename_no_replace(source, destination)

    monkeypatch.setattr("autoform_cli.skeleton._install_output", fail_passage_install)
    monkeypatch.setattr("autoform_cli.skeleton._rename_no_replace", create_before_restore)

    with pytest.raises(SkeletonError, match="could not restore skeleton output"):
        write_packets(report, packets, passages=passages)

    assert concurrent_inode is not None
    assert packets.stat().st_ino == concurrent_inode
    assert list(packets.iterdir()) == []
    assert (passages / "old-marker").read_text(encoding="utf-8") == "old passages\n"
    backups = list(tmp_path.glob(".packets.autoform-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "old-marker").read_text(encoding="utf-8") == "old packets\n"


def test_packet_publication_uses_normal_modes_and_preserves_existing_mode(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    control = tmp_path / "control"
    control.mkdir()

    write_packets(report, packets)

    assert stat.S_IMODE(packets.stat().st_mode) == stat.S_IMODE(control.stat().st_mode)
    packets.chmod(0o750)
    write_packets(report, packets)
    assert stat.S_IMODE(packets.stat().st_mode) == 0o750


def test_packet_publication_cleans_up_when_second_stage_fails(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    stage_output = _stage_output
    calls = 0

    def fail_second_stage(destination: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated passage staging failure")
        return stage_output(destination)

    monkeypatch.setattr("autoform_cli.skeleton._stage_output", fail_second_stage)

    with pytest.raises(SkeletonError, match="could not prepare skeleton output"):
        write_packets(report, packets, passages=passages)

    assert list(tmp_path.glob(".packets.autoform-stage-*")) == []
    assert not packets.exists() and not passages.exists()


def test_packet_publication_detects_a_concurrent_file_edit(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    write_packets(report, packets)
    marker = packets / "review.txt"
    marker.write_text("before\n", encoding="utf-8")

    def edit_then_replace(outputs) -> None:
        marker.write_text("concurrent edit\n", encoding="utf-8")
        _replace_outputs(outputs)

    monkeypatch.setattr("autoform_cli.skeleton._replace_outputs", edit_then_replace)

    with pytest.raises(SkeletonError, match="changed during publication"):
        write_packets(report, packets)
    assert marker.read_text(encoding="utf-8") == "concurrent edit\n"


def test_packet_publication_checks_the_isolated_old_tree_before_install(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    write_packets(report, packets)
    marker = packets / "review.txt"
    marker.write_text("before\n", encoding="utf-8")
    replace = os.replace

    def edit_before_backup(source: str | Path, destination: str | Path) -> None:
        if Path(source) == packets and "autoform-backup" in Path(destination).name:
            marker.write_text("concurrent edit\n", encoding="utf-8")
        replace(source, destination)

    monkeypatch.setattr("autoform_cli.skeleton.os.replace", edit_before_backup)

    with pytest.raises(SkeletonError, match="changed during publication"):
        write_packets(report, packets)
    assert marker.read_text(encoding="utf-8") == "concurrent edit\n"


def load_skeleton_report_roundtrip(report, tmp_path: Path) -> bool:
    path = tmp_path / "r.json"
    path.write_text(report.to_json(), encoding="utf-8")
    return load_skeleton_report(path) == report
