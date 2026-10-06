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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
import psutil

from autoform_cli.__main__ import main
from autoform_cli.graph import load_graph
from autoform_cli.lean import (
    PACKET_SCHEMA,
    PASSAGE_SCHEMA,
    Declaration,
    LeanSourceError,
    SourceIndex,
    index_project,
)
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
    _CommandTimedOut,
    _PROBE_POOL,
    _PROCESS_TERMINATION_GRACE,
    _ProbeEnvironmentError,
    _SignalGuard,
    _declaration,
    _install_output,
    _join_readers,
    _remove_output,
    _run_module_probes,
    _rename_no_replace,
    _run_bounded_command,
    _replace_outputs,
    _stage_output,
    _terminate_process_tree,
    _hash_module_files,
    _local_safety_issue,
    _process_is_alive,
    _remember_tagged_processes,
    _project_control_snapshot,
    _without_comments,
    _probe_modules,
    _probe_record_issue,
    _probe_workers,
    _read_probe_records,
    _render_probe_helper,
    extract_graph_skeletons,
    extract_skeletons,
    format_report,
    lean_libraries,
    load_skeleton_report,
    module_of,
    parse_probe_output,
    path_of,
    render_probe,
    run_probe,
    source_excerpt,
    write_packets,
    write_skeleton_report,
)
from tests.test_review_cli import _undecodable_json

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


def _trusted(name: str, kind: str, lines: list[int] | None, signature: str, **fields: object) -> dict[str, object]:
    """A trusted-declaration record from ``Skel.Defs``, as the probe prints it."""

    source = fields.pop("source", None)
    return {
        "name": name,
        "source_name": name,
        "kind": kind,
        "module": "Skel.Defs",
        "range": lines,
        "signature": signature,
        "raw_signature": signature.replace("→", "->"),
        "semantic_schema": SEMANTIC_SCHEMA,
        "semantic": _semantic({"type": {"sort": {"zero": None}}, **fields.pop("material", {})}),
        "depends": fields.pop("depends", []),
        "source": source,
        "source_comments": None if source is None else _leading_doc(source),
        **fields,
    }


def _fake_probe_output(*, include_ghost: bool = False) -> str:
    """What the probe says about the fixture, as captured from a real run."""

    eligible = _trusted(
        "Skel.Eligible", "def", [5, 6], "Skel.Eligible {Y : Type} (S : Y → Prop) (y : Y) : Prop",
        material={"value": {"bvar": 0}},
        source="/-- A weak observation admits a label. -/\ndef Eligible (S : Y → Prop) (y : Y) : Prop := S y",
    )
    non_ambiguous = _trusted(
        "Skel.NonAmbiguous", "def", [8, 10], "Skel.NonAmbiguous {Y : Type} (S : Y → Prop) : Prop",
        material={"value": {"bvar": 1}},
        depends=["Skel.Eligible"],
        source=(
            "/-- At most one label is admitted. -/\n"
            "def NonAmbiguous (S : Y → Prop) : Prop :=\n"
            "  ∀ y z : Y, Eligible S y → Eligible S z → y = z"
        ),
    )
    observation = _trusted(
        "Skel.Observation", "structure", [15, 18], "Skel.Observation (Y : Type) : Type",
        material={"constructors": []},
        source=(
            "/-- A structure, to check inductive handling. -/\n"
            "structure Observation (Y : Type) where\n"
            "  admits : Y → Prop\n"
            "  nonempty : ∃ y, admits y"
        ),
    )
    statement = (
        "/-- Uses a structure in its statement, and sorry in its proof. -/\n"
        "theorem observation_determined (o : Observation Y) (h : NonAmbiguous o.admits) :\n"
        "    ∃ y, o.admits y ∧ ∀ z, o.admits z → z = y"
    )
    records = [
        "some unrelated line from Lean",
        _probe_lines(
            {
                "root": "Skel.observation_determined",
                "found": True,
                "kind": "theorem",
                "module": "Skel.Main",
                "range": [14, 17],
                "signature": "Skel.observation_determined {Y : Type} (o : Skel.Observation Y) :\n  ∃ y, o.admits y",
                "raw_signature": (
                    "Skel.observation_determined {Y : Type} (o : Skel.Observation Y) :\n  Exists fun y => o.admits y"
                ),
                "semantic_schema": SEMANTIC_SCHEMA,
                "semantic": _semantic({"type": {"sort": {"zero": None}}}),
                "lean_version": "4.32.2",
                "source": None,
                "source_comments": None,
                "statement_source": statement,
                "statement_comments": _leading_doc(statement),
                "depends": ["Skel.NonAmbiguous", "Skel.Observation"],
                # Deliberately out of dependency order: the report must sort them.
                "trusted": [non_ambiguous, observation, eligible],
                "assumed": ["Mathlib.Fake"],
                "assumed_semantics": [["Mathlib.Fake", _semantic({"type": {"sort": {"zero": None}}})]],
                "boundary_modules": [["Mathlib.Fake", "olean", "Skel/Defs.lean"]],
                "axioms": ["sorryAx"],
                "axiom_semantics": [["sorryAx", _semantic({"type": {"sort": {"zero": None}}})]],
            }
        ),
    ]
    if include_ghost:
        records.append(_record("Skel.ghost", found=False))
    return "\n".join(records)


def _fake_found_record() -> dict[str, object]:
    return parse_probe_output(_fake_probe_output())["Skel.observation_determined"]


def _fake_default_probe(monkeypatch, probe) -> list[tuple[str, ...]]:
    """Stand in for Lake and Lean behind the default runner.

    ``probe(program, lean_root)`` answers each per-module probe. Returns the
    module lists the hoisted Lake freshness check was asked about.
    """

    checks: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "autoform_cli.skeleton._check_build_freshness", lambda root, modules, **kwargs: checks.append(modules)
    )
    monkeypatch.setattr(
        "autoform_cli.skeleton._build_probe_helper", lambda root, directory, **kwargs: directory
    )
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", lambda program, root, **kwargs: probe(program, root))
    return checks


def _fake_report(tmp_path: Path, output: str | None = None) -> SkeletonReport:
    """Extract the fixture's one-article blueprint against the fake (or given) probe output."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    output = _fake_probe_output() if output is None else output
    if os.name == "nt":
        declaration = Declaration(
            "Skel.observation_determined",
            Path("Skel/Main.lean"),
            15,
            "theorem",
        )
        index = SourceIndex(
            root=project,
            declarations={declaration.name: declaration},
            source_digest="windows-publication-fixture",
            line_counts={Path("Skel/Main.lean"): 21},
        )
        return extract_graph_skeletons(
            load_graph(blueprint),
            lean_root=project,
            libraries=lean_libraries(project),
            index=index,
            runner=lambda _probe, _root: output,
        )
    return extract_skeletons(blueprint, lean_root=project, runner=lambda probe, root: output)


def _assert_load_rejects(path: Path, payload: dict[str, object], match: str | None = None) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SkeletonError, match=match):
        load_skeleton_report(path)


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
    assert 'Lean.Name.str (Lean.Name.str (Lean.Name.anonymous) "Skel") "observation_determined"' in probe
    assert f'"{PROBE_MARKER}' in helper
    assert "def probeOutputLimit : Nat := 67108864" in helper
    assert 'Lean.Name.str (Lean.Name.anonymous) "Init"' in helper
    assert "info.fromClass" in helper
    assert "privateToUserName c" in helper


def test_probe_elaborates_no_helper_under_the_project_imports() -> None:
    probe = render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))

    # Only this call elaborates with the project in scope, and it opens nothing.
    assert probe.splitlines() == [
        "import Skel.Main",
        "import «autoform-skeleton-helper»",
        'run_cmd AutoformSkeleton.main [Lean.Name.str (Lean.Name.anonymous) "Skel"] '
        '[("Skel.x", Lean.Name.str (Lean.Name.str (Lean.Name.anonymous) "Skel") "x")]',
    ]
    assert "set_option" not in _render_probe_helper()


def test_probe_transports_quoted_and_numeric_name_components_structurally() -> None:
    probe = render_probe(
        imports=("Skel.Main",),
        roots=("Skel.«quoted.name with space».2",),
        project_roots=("Skel",),
    )

    assert '"Skel.«quoted.name with space».2"' in probe
    assert 'Lean.Name.str (Lean.Name.str (Lean.Name.anonymous) "Skel") "quoted.name with space"' in probe
    assert "Lean.Name.num (Lean.Name.str" in probe


def test_probe_refuses_to_render_nothing() -> None:
    with pytest.raises(SkeletonError):
        render_probe(imports=("Skel",), roots=(), project_roots=("Skel",))
    with pytest.raises(SkeletonError):
        render_probe(imports=(), roots=("Skel.x",), project_roots=("Skel",))


@pytest.fixture
def probe(tmp_path: Path, monkeypatch) -> str:
    (tmp_path / "lake-manifest.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("autoform_cli.skeleton.shutil.which", lambda executable: "/bin/lake")
    return render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))


def test_probe_refuses_stale_artifacts_before_executing_lean(tmp_path: Path, monkeypatch, probe: str) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 3, stdout="target is out-of-date", stderr="")

    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", fake_run)

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

    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", fake_run)

    with pytest.raises(SkeletonError, match="must be writable") as refused:
        run_probe(probe, tmp_path)

    assert "stale" not in str(refused.value)
    assert "permission denied" in str(refused.value)


def test_probe_semantic_schema_matches_the_python_reader() -> None:
    helper = _render_probe_helper()

    assert re.findall(r'^def semanticSchema := "([^"]*)"$', helper, re.MULTILINE) == [SEMANTIC_SCHEMA]


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


@pytest.mark.skipif(os.name != "posix", reason="detached-session assertion is POSIX-specific")
def test_bounded_command_finds_a_descendant_that_escapes_its_process_group(
    tmp_path: Path,
) -> None:
    child_pid = tmp_path / "detached-child.pid"
    # The child leaves the process group before it records its pid, and the
    # parent exits only once that record exists, so the child has escaped by
    # the time the command ends however slowly it starts.
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
        if calls == 1:
            return monotonic()
        deadline = monotonic() + 5
        while not process_pid.exists() and monotonic() < deadline:
            time.sleep(0.01)
        raise KeyboardInterrupt

    monkeypatch.setattr("autoform_cli.skeleton.time.monotonic", interrupt_after_start)

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


def _sleeping_probe(pids: Path) -> str:
    """A command that records its pid and its child's, then outlives any test."""

    return (
        "import os, pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(pids)!r}).write_text(f'{{os.getpid()}} {{child.pid}}'); "
        "time.sleep(60)"
    )


def _assert_no_survivors(pid_files: list[Path]) -> None:
    deadline = time.monotonic() + 5
    survivors = [int(pid) for path in pid_files if path.exists() for pid in path.read_text(encoding="utf-8").split()]
    while survivors and time.monotonic() < deadline:
        survivors = [pid for pid in survivors if _pid_is_live(pid)]
        time.sleep(0.01)
    assert not survivors


@pytest.mark.parametrize("workers", [2, 1], ids=["both-running", "one-queued"])
def test_probe_pool_interruption_kills_running_probes_and_starts_no_queued_one(
    tmp_path: Path, monkeypatch, workers: int
) -> None:
    pid_files = [tmp_path / "a.pids", tmp_path / "b.pids"]

    def interrupt_once_the_workers_run(*args, **kwargs):
        deadline = time.monotonic() + 20
        running = pid_files[:workers]
        while not all(path.exists() and len(path.read_text(encoding="utf-8").split()) == 2 for path in running):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        raise KeyboardInterrupt

    def run(program: str, root: Path) -> str:
        return _run_bounded_command([sys.executable, "-c", program], cwd=root, timeout=60, context="test probe").stdout

    monkeypatch.setattr("autoform_cli.skeleton._probe_workers", lambda jobs: workers)
    monkeypatch.setattr("autoform_cli.skeleton.wait", interrupt_once_the_workers_run)

    with pytest.raises(KeyboardInterrupt):
        _run_module_probes([("A", _sleeping_probe(pid_files[0])), ("B", _sleeping_probe(pid_files[1]))], run, tmp_path)

    assert pid_files[1].exists() == (workers == 2)
    _assert_no_survivors(pid_files)


@pytest.mark.skipif(os.name != "posix", reason="termination signals are POSIX-specific")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM], ids=["int", "term"])
def test_probe_pool_termination_signal_kills_every_workers_process_group(tmp_path: Path, signum: int) -> None:
    pid_files = [tmp_path / "a.pids", tmp_path / "b.pids"]
    driver = (
        "import signal, sys; from pathlib import Path; "
        "signal.signal(signal.SIGHUP, signal.SIG_DFL); "
        "signal.signal(signal.SIGINT, signal.default_int_handler); "
        "import autoform_cli.skeleton as skeleton; "
        "skeleton._probe_workers = lambda jobs: jobs; "
        "run = lambda program, root: skeleton._run_bounded_command("
        "[sys.executable, '-c', program], cwd=root, timeout=60, context='test probe').stdout; "
        "skeleton._run_module_probes(list(zip(('A', 'B'), sys.argv[1:])), run, Path.cwd())"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    cli = subprocess.Popen(
        [sys.executable, "-c", driver, *(_sleeping_probe(path) for path in pid_files)], cwd=tmp_path, env=env
    )
    try:
        deadline = time.monotonic() + 20
        while not all(path.exists() and len(path.read_text(encoding="utf-8").split()) == 2 for path in pid_files):
            assert time.monotonic() < deadline and cli.poll() is None
            time.sleep(0.05)
        os.kill(cli.pid, signum)
        assert cli.wait(timeout=15) == -signum
    finally:
        if cli.poll() is None:
            cli.kill()
    _assert_no_survivors(pid_files)


@pytest.mark.skipif(os.name != "posix", reason="termination signals are POSIX-specific")
def test_a_signal_as_the_probe_pool_starts_its_cleanup_does_not_skip_it(tmp_path: Path, monkeypatch) -> None:
    # A worker fails with an error that is not a probe failure, and a SIGTERM
    # lands as the pool first disarms its guard; the pool must still cancel
    # and join every worker before the signal ends anything.
    pid_file = tmp_path / "a.pids"
    disarm = _SignalGuard.disarm
    received: list[int] = []
    joined: list[bool] = []

    class Pool(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            super().shutdown(wait=wait, cancel_futures=cancel_futures)
            joined.append(wait)

    def signal_on_first_disarm(self: _SignalGuard) -> None:
        if self.armed and threading.current_thread() is threading.main_thread():
            self._handle(signal.SIGTERM, None)
        disarm(self)

    def run(program: str, root: Path) -> str:
        if program == "broken":
            deadline = time.monotonic() + 20
            while not (pid_file.exists() and len(pid_file.read_text(encoding="utf-8").split()) == 2):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            raise RuntimeError("worker broke")
        return _run_bounded_command([sys.executable, "-c", program], cwd=root, timeout=60, context="test probe").stdout

    monkeypatch.setattr("autoform_cli.skeleton._probe_workers", lambda jobs: jobs)
    monkeypatch.setattr("autoform_cli.skeleton._SignalGuard.disarm", signal_on_first_disarm)
    monkeypatch.setattr("autoform_cli.skeleton.ThreadPoolExecutor", Pool)
    before = set(threading.enumerate())
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: received.append(signum))
    try:
        with pytest.raises(SkeletonError, match="interrupted by SIGTERM"):
            _run_module_probes([("A", _sleeping_probe(pid_file)), ("B", "broken")], run, tmp_path)
        workers = [thread for thread in threading.enumerate() if thread not in before and thread.is_alive()]
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert received == [signal.SIGTERM]
    assert joined == [True]
    assert workers == []
    _assert_no_survivors([pid_file])


_STALLING_LAKE = """\
#!{python}
import os, pathlib, subprocess, sys, time
args = sys.argv[1:]
phase = os.environ["FAKE_LAKE_PHASE"]


def stall():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pathlib.Path(os.environ["FAKE_LAKE_PIDS"]).write_text(f"{{os.getpid()}} {{child.pid}}")
    time.sleep(60)


if args[:1] == ["--rehash"]:
    sys.exit(0)
if "-o" in args:
    if phase == "helper":
        stall()
    pathlib.Path(args[args.index("-o") + 1]).write_bytes(b"")
    sys.exit(0)
stall()
"""


@pytest.mark.skipif(os.name != "posix", reason="termination signals are POSIX-specific")
@pytest.mark.parametrize("signum", [signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)], ids=["term", "hup"])
@pytest.mark.parametrize("phase", ["translate", "helper", "probe"])
def test_a_termination_signal_during_extraction_removes_its_scratch(tmp_path: Path, phase: str, signum: int) -> None:
    # Lake stalls while translating lakefile.lean, while building the probe
    # helper, or while a probe runs; the signal must still remove every
    # temporary directory the extraction made before it ends the process.
    project = _project(tmp_path)
    (project / "lake-manifest.json").write_text("{}\n", encoding="utf-8")
    if phase == "translate":
        (project / "lakefile.toml").unlink()
        (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "lake").write_text(_STALLING_LAKE.format(python=sys.executable), encoding="utf-8")
    (bin_dir / "lake").chmod(0o755)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    pid_file = tmp_path / "lake.pids"
    driver = (
        "import signal, sys; from pathlib import Path; "
        "signal.signal(signal.SIGHUP, signal.SIG_DFL); "
        "from autoform_cli.skeleton import extract_skeletons; "
        "extract_skeletons(Path(sys.argv[1]), lean_root=Path(sys.argv[2]))"
    )
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        PATH=f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        TMPDIR=str(scratch),
        FAKE_LAKE_PHASE=phase,
        FAKE_LAKE_PIDS=str(pid_file),
    )
    cli = subprocess.Popen([sys.executable, "-c", driver, str(blueprint), str(project)], cwd=tmp_path, env=env)
    try:
        deadline = time.monotonic() + 30
        while not pid_file.exists() or len(pid_file.read_text(encoding="utf-8").split()) < 2:
            assert time.monotonic() < deadline and cli.poll() is None
            time.sleep(0.05)
        assert list(scratch.glob("autoform-skeleton-*"))
        os.kill(cli.pid, signum)
        assert cli.wait(timeout=15) == -signum
    finally:
        if cli.poll() is None:
            cli.kill()
    _assert_no_survivors([pid_file])
    assert list(scratch.glob("autoform-skeleton-*")) == []


def test_a_probe_cancelled_before_its_process_starts_never_starts_it(tmp_path: Path, monkeypatch) -> None:
    # The pool is cancelled after a worker takes the probe and before it
    # starts the process.
    def run(program: str, root: Path) -> str:
        _PROBE_POOL.cancelled.set()
        return _run_bounded_command([sys.executable, "-c", program], cwd=root, timeout=60, context="test probe").stdout

    def popen(*args, **kwargs):
        raise AssertionError("a process started after the pool was cancelled")

    monkeypatch.setattr("autoform_cli.skeleton.subprocess.Popen", popen)

    (result,) = _run_module_probes([("A", "pass")], run, tmp_path).values()

    assert isinstance(result, SkeletonError) and result.issues == ("test probe was cancelled",)


def test_unreadable_probe_records_fail_only_that_probe(tmp_path: Path) -> None:
    records = tmp_path / "records.out"
    records.write_bytes(PROBE_MARKER.encode() + b"\xff\n")
    with pytest.raises(SkeletonError, match="records are not valid UTF-8"):
        _read_probe_records(records)
    records.unlink()
    records.mkdir()
    with pytest.raises(SkeletonError, match="records could not be read"):
        _read_probe_records(records)


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


def _pid_is_live(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


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
            patch.setattr("autoform_cli.skeleton.time.monotonic", time_out_once_both_run)
            patch.setattr("autoform_cli.skeleton._terminate_process_tree", signal_then_terminate)
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

    monkeypatch.setattr("autoform_cli.skeleton._join_readers", report_stuck_readers)

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
    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", fake_run)
    monkeypatch.setattr("autoform_cli.skeleton.time.monotonic", lambda: next(times))

    assert run_probe(probe, tmp_path, timeout=10, freshness_timeout=20, helper=tmp_path) == "probe output"
    assert calls == [20, 9.0]


def test_a_probe_timeout_names_the_flag_that_raises_it(tmp_path: Path, monkeypatch, probe: str) -> None:
    (tmp_path / "autoform-skeleton-helper.olean").write_bytes(b"")
    bounded = _run_bounded_command

    def slow_probe(command, **kwargs):
        return bounded([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr("autoform_cli.skeleton._check_artifacts_fresh", lambda *args, **kwargs: None)
    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", slow_probe)

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

    monkeypatch.setattr("autoform_cli.skeleton._check_artifacts_fresh", lambda *args, **kwargs: None)
    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", flooding_probe)
    monkeypatch.setattr("autoform_cli.skeleton.DEFAULT_PROBE_OUTPUT_LIMIT", 1024)

    with pytest.raises(SkeletonError, match="1024-byte output limit"):
        run_probe(probe, tmp_path, helper=tmp_path)


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


def test_parse_probe_output_keeps_an_error_the_probe_caught_on_one_root() -> None:
    error = _record("Skel.ghost", error="unknown constant")
    records = parse_probe_output("\n".join([_fake_probe_output(), error]))

    assert records["Skel.ghost"] == {"root": "Skel.ghost", "error": "unknown constant"}
    assert records["Skel.observation_determined"]["found"] is True
    for bad in (_record("Skel.ghost", error=3), _record("Skel.ghost", error="x", found=False)):
        with pytest.raises(SkeletonError, match="invalid error record for Skel.ghost"):
            parse_probe_output(bad)


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
        parse_probe_output(probe({"5": [3, "x"]}, [5]))
    with pytest.raises(SkeletonError, match=f"invalid semantic material for {root['root']}"):
        parse_probe_output(probe({}, [0]))
    with pytest.raises(SkeletonError, match=f"invalid semantic material for {root['root']}"):
        parse_probe_output(probe({}, text))  # type: ignore[arg-type]
    monkeypatch.setattr("autoform_cli.skeleton._PROBE_MATERIAL_LIMIT", len(text) - 1)
    with pytest.raises(SkeletonError, match="exceeds"):
        parse_probe_output(probe({"0": [text[:10]], "1": [0, text[10:30]]}, shared_text))


def _unknown_safety(record: dict[str, object]) -> None:
    semantic = json.loads(str(record["semantic"]))
    semantic["root"]["safety"] = "unknown"
    record["semantic"] = json.dumps(semantic)


def _ambiguous_companion(record: dict[str, object]) -> None:
    semantic = json.loads(str(record["semantic"]))
    semantic["generated"] = [{"name": "ambiguous.display.name", "material": semantic["root"]}]
    record["semantic"] = json.dumps(semantic)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda record: record.update(semantic_schema="unknown"), "unsupported semantic schema"),
        (lambda record: record.update(semantic="not JSON"), "invalid elaborated semantic material"),
        (
            lambda record: record.update(semantic=_undecodable_json("a-number-too-long")),
            "invalid elaborated semantic material",
        ),
        (_unknown_safety, "invalid elaborated semantic material"),
        (_ambiguous_companion, "invalid elaborated semantic material"),
        (lambda record: record["trusted"][0].update(depends=[False]), "invalid depends"),
        (lambda record: record.update(source="theorem t : True := by trivial"), "proof-bearing source"),
        (lambda record: record.update(statement_source=7), "invalid statement_source"),
    ],
)
def test_parse_probe_output_rejects_incomplete_semantic_records(mutate, match: str) -> None:
    record = _fake_found_record()
    mutate(record)
    with pytest.raises(SkeletonError, match=match):
        parse_probe_output(_probe_lines(record))


def test_parse_probe_output_flags_a_missing_source_and_withholds_an_unparsed_statement() -> None:
    record = _fake_found_record()
    trusted = record["trusted"]
    assert isinstance(trusted, list) and isinstance(trusted[0], dict)
    trusted[0]["source"] = trusted[0]["source_comments"] = None
    (parsed,) = parse_probe_output(_probe_lines(record)).values()
    assert "omitted required source" in str(_probe_record_issue(parsed))

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
    record = _fake_found_record()
    # Lean parsed neither the statement nor a trusted source, and that source
    # has a docstring Lean could not locate: both are withheld.
    record["statement_source"] = record["statement_comments"] = None
    record["trusted"][2]["source_comments"] = None

    report = _fake_report(tmp_path, _probe_lines(record))

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
    data["trusted"]["Skel.Main"][data["nodes"][0]["declarations"][0]["trusted"][2]]["source_withheld"] = True
    _assert_load_rejects(path, data, "invalid withheld source flag")
    data = report.as_dict()
    data["trusted"]["Skel.Main"][eligible.name]["start_line"] = None
    data["trusted"]["Skel.Main"][eligible.name]["end_line"] = None
    _assert_load_rejects(path, data, "required source is missing")


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


def test_lake_toml_parser_limits_fail_closed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text(
        f'name = "Solo"\nunknown = {"9" * 5_000}\n', encoding="utf-8"
    )

    with pytest.raises(SkeletonError, match="cannot safely parse.*exceeds the parser's limits"):
        lean_libraries(project)


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


def test_project_control_snapshot_retains_only_fingerprints(tmp_path: Path) -> None:
    project = _project(tmp_path)
    content = (project / "lakefile.toml").read_bytes()

    snapshot = dict(_project_control_snapshot(project))

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
    report = _fake_report(tmp_path)
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

    # An external boundary can change meaning without changing the packet text;
    # a review recorded against the review hash must not carry over.
    node, changed = report.nodes[0], replace(report.nodes[0], declarations=(module_change,))
    assert changed.evidence_hash == node.evidence_hash
    assert changed.hash != node.hash
    assert changed.review_hash != node.review_hash


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
    record = _fake_found_record()
    trusted = record["trusted"]
    assert isinstance(trusted, list)
    signature = "Skel.eligible_of {Y : Type} (S : Y → Prop) (y : Y) (h : S y) : Skel.Eligible S y"
    trusted.append(_trusted("Skel.eligible_of", "theorem", [12, 13], signature, depends=["Skel.Eligible"]))
    report = _fake_report(tmp_path, _probe_lines(record))
    theorem = next(item for item in report.nodes[0].declarations[0].trusted if item.name == "Skel.eligible_of")

    assert theorem.source is None
    assert ":= h" not in report.nodes[0].declarations[0].blind_text()
    assert ":= h" not in format_report(report, lean_root=tmp_path / "project")


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
        "basics/ghost: Skel.ghost: not in the built environment after importing "
        "Skel.Main; check the `lean:` target and declaring source",
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
    _assert_load_rejects(path, payload, "not a canonical")


def test_extraction_never_runs_lean_when_nothing_resolves(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"phantom": "Skel.doesNotExist"})

    def runner(probe: str, lean_root: Path) -> str:
        raise AssertionError("the probe must not run")

    report = extract_skeletons(blueprint, lean_root=project, runner=runner)

    assert tuple(issue.message for issue in report.unresolved) == (
        "basics/phantom: Skel.doesNotExist: declaration not found in the Lean sources",
    )


@pytest.mark.parametrize(
    ("changed", "edit", "match"),
    [
        ("project/Skel/Defs.lean", "\n-- concurrent edit\n", "changed during skeleton extraction"),
        ("blueprint/roadmap/basics/determined.md", "\nChanged.\n", "blueprint changed"),
        ("project/lakefile.toml", "\n# changed\n", "configuration changed"),
    ],
    ids=["sources", "blueprint", "lake-configuration"],
)
def test_default_extraction_rejects_input_changed_during_probe(
    tmp_path: Path, monkeypatch, changed: str, edit: str, match: str
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    path = tmp_path / changed

    def changing_probe(probe: str, lean_root: Path) -> str:
        path.write_text(path.read_text(encoding="utf-8") + edit, encoding="utf-8")
        # A coarse filesystem clock can stamp this write at or before the snapshot it follows.
        later = time.time_ns() + 10**9
        os.utime(path, ns=(later, later))
        return _fake_probe_output()

    _fake_default_probe(monkeypatch, changing_probe)

    with pytest.raises(SkeletonError, match=match):
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


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        (None, "Lean sources could not be indexed"),
        (
            "permission denied: Project/Secret.lean",
            "Lean sources could not be indexed: permission denied: Project/Secret.lean",
        ),
    ],
)
@pytest.mark.parametrize("failure_call", (1, 2))
def test_extraction_translates_source_index_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_call: int, reason: str | None, message: str
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    real_index_project = index_project
    calls = 0

    def fail_index(root: Path):
        nonlocal calls
        calls += 1
        if calls == failure_call:
            if reason is not None:
                raise LeanSourceError(reason)
            raise OSError(f"private host detail: {root}")
        return real_index_project(root)

    monkeypatch.setattr("autoform_cli.skeleton.index_project", fail_index)

    with pytest.raises(SkeletonError) as error:
        extract_skeletons(
            blueprint,
            lean_root=project,
            runner=lambda probe, root: _fake_probe_output(),
        )

    assert error.value.issues == (message,)
    assert str(tmp_path) not in str(error.value)
    assert calls == failure_call


def test_extraction_rejects_configuration_changed_while_reading_libraries(tmp_path: Path, monkeypatch) -> None:
    # An edit reverted before the final check would otherwise select libraries silently.
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    lakefile = project / "lakefile.toml"
    original = lakefile.read_bytes()
    ran: list[str] = []

    def racing_libraries(root: Path) -> tuple[object, ...]:
        lakefile.write_bytes(original + b'\n[[lean_lib]]\nname = "Transient"\n')
        return lean_libraries(root)

    def reverting_runner(probe: str, lean_root: Path) -> str:
        ran.append(probe)
        lakefile.write_bytes(original)
        return _fake_probe_output()

    monkeypatch.setattr("autoform_cli.skeleton.lean_libraries", racing_libraries)

    with pytest.raises(SkeletonError, match="configuration changed"):
        extract_skeletons(blueprint, lean_root=project, runner=reverting_runner)
    assert ran == []


def test_extraction_rejects_a_passage_changed_during_probe(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    source = blueprint / "sources" / "book.tex"
    source.parent.mkdir()
    source.write_text("before\n", encoding="utf-8")
    article = blueprint / "roadmap" / "basics" / "determined.md"
    _replace_source(article, "## Depends on", "## Sources\n\n- [book](../../sources/book.tex#L1-L1)\n\n## Depends on")

    def changing_probe(probe: str, lean_root: Path) -> str:
        source.write_text("after\n", encoding="utf-8")
        return _fake_probe_output()

    _fake_default_probe(monkeypatch, changing_probe)

    with pytest.raises(SkeletonError, match="blueprint changed while skeletons were being extracted"):
        extract_skeletons(blueprint, lean_root=project)


def test_blind_packet_shows_the_statement_without_notation(tmp_path: Path) -> None:
    report = _fake_report(tmp_path)
    declaration = report.nodes[0].declarations[0]

    assert "-- raw signature:\n" + declaration.raw_signature in declaration.blind_text()
    assert "Exists fun y => o.admits y" in declaration.blind_text()
    # Notation that hides a different operator changes the packet a reviewer sees.
    misread = replace(declaration, raw_signature=declaration.raw_signature + " ")
    assert misread.evidence_hash != declaration.evidence_hash


def test_packets_drop_the_comments_lean_reports_and_keep_the_rest(tmp_path: Path) -> None:
    record = _fake_found_record()
    trusted = record["trusted"][2]
    # `/--/` opens a docstring whose body is `/ KEEPOUT `; a lexer that closes
    # it at the next `-/` would show the docstring as code.
    trusted["source"] = "/--/ KEEPOUT -/\ndef docOpened : Nat := 6"
    trusted["source_comments"] = [[0, len("/--/ KEEPOUT -/")]]
    record["trusted"] = [trusted, record["trusted"][1], record["trusted"][0]]
    report = _fake_report(tmp_path, _probe_lines(record))

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

    # A malformed record fails its module's probe, which leaves that module's roots unresolved.
    (issue,) = _fake_report(tmp_path, _probe_lines(record)).unresolved
    assert issue.reason.startswith("probe of module Skel.Main failed: ")
    assert "invalid statement_comments" in issue.reason


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
        ("../../sources/chapter#L1-L1", "names something other than a regular file"),
        ("../../sources/%00book.tex#L1-L1", "contains an invalid path"),
    ],
)
def test_extraction_reports_a_locator_that_names_no_passage(tmp_path: Path, link: str, why: str) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    sources = blueprint / "sources"
    sources.mkdir()
    (sources / "chapter").mkdir()
    (sources / "book.tex").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (sources / "empty.tex").write_text("", encoding="utf-8")
    (sources / "binary.tex").write_bytes(b"\xff\xfe\n")
    (tmp_path / "outside.tex").write_text("outside\n", encoding="utf-8")
    article = blueprint / "roadmap" / "basics" / "determined.md"
    _replace_source(article, "## Depends on", f"## Sources\n\n- [book]({link})\n\n## Depends on")

    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())

    assert not report.clean and report.nodes[0].declarations == ()
    [unresolved] = report.unresolved
    assert unresolved.declaration == "Skel.observation_determined"
    assert unresolved.reason.startswith("source locator ") and why in unresolved.reason

    _replace_source(article, link, "../../sources/book.tex#L2-L3")
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    assert report.clean
    assert report.nodes[0].passage == "two\nthree"


def test_markdown_locator_is_a_note_whatever_the_case_of_its_suffix(tmp_path: Path) -> None:
    # Audit reads `NOTES.MD` as Markdown, so its fragment names a heading, not lines.
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    sources = blueprint / "sources"
    sources.mkdir()
    (sources / "NOTES.MD").write_text("# Notes\n", encoding="utf-8")
    (sources / "book.tex").write_text("one\ntwo\nthree\n", encoding="utf-8")
    article = blueprint / "roadmap" / "basics" / "determined.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "## Depends on",
            "## Sources\n\n- [notes](../../sources/NOTES.MD#L1-L1)\n"
            "- [book](../../sources/book.tex#L2-L3)\n\n## Depends on",
        ),
        encoding="utf-8",
    )

    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())

    assert report.clean
    assert report.nodes[0].passage == "two\nthree"
    assert report.nodes[0].passage_locator == "sources/book.tex#L2-L3"


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


#: The fixture module that declares each root the per-module fake probe answers for.
_FAKE_ROOT_MODULES = {
    "Skel.observation_determined": "Skel.Main",
    "Skel.supervision_nonAmbiguous": "Skel.Main",
    "Skel.heavy_of_notation": "Skel.Uses",
    "Skel.ScopedA.activatesScope": "Skel.ScopedA",
}


def _fake_module_probe(probe: str, lean_root: Path) -> str:
    """Answer one per-module probe with a found record for each fixture root it names."""

    modules = _probe_modules(probe)
    lines: list[str] = []
    for root, module in _FAKE_ROOT_MODULES.items():
        if module in modules and f'"{root}"' in probe:
            lines += _probe_lines({**_fake_found_record(), "root": root, "module": module}).splitlines()
    return "\n".join(dict.fromkeys(lines))


def test_each_probe_imports_only_its_roots_module(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(
        tmp_path,
        lean={
            "determined": "Skel.observation_determined",
            "notation": "Skel.heavy_of_notation",
            "scoped": "Skel.ScopedA.activatesScope",
        },
    )
    # An article whose passage cannot be read has no root to probe, so its module is not imported.
    article = blueprint / "roadmap" / "basics" / "scoped.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "## Depends on", "## Sources\n\n- [book](sources/missing.txt#L1-L1)\n\n## Depends on"
        ),
        encoding="utf-8",
    )
    probes: list[str] = []
    lock = threading.Lock()

    def runner(probe: str, lean_root: Path) -> str:
        with lock:
            probes.append(probe)
        return _fake_module_probe(probe, lean_root)

    full = extract_skeletons(blueprint, lean_root=project, runner=runner)
    full_probes = {_probe_modules(probe): probe for probe in probes}
    probes.clear()
    scoped = extract_skeletons(blueprint, lean_root=project, runner=runner, node_ids=("basics/determined",))

    assert sorted(full_probes) == [("Skel.Main",), ("Skel.Uses",)]
    assert '"Skel.heavy_of_notation"' in full_probes["Skel.Uses",]
    assert '"Skel.heavy_of_notation"' not in full_probes["Skel.Main",]
    # A subset selection renders the very same program for the module it keeps.
    assert probes == [full_probes["Skel.Main",]]
    assert scoped.node("basics/determined") == full.node("basics/determined")


def _two_module_blueprint(tmp_path: Path) -> Path:
    return _blueprint(
        tmp_path, lean={"determined": "Skel.observation_determined", "notation": "Skel.heavy_of_notation"}
    )


def _fake_lake(monkeypatch, project: Path, answer) -> list[list[str]]:
    """Run the default runner against a fake Lake; ``answer(modules, command, env)`` plays each probe.

    Each probe is recorded as its command followed by the modules it imports.
    """

    (project / "lake-manifest.json").write_text("{}\n", encoding="utf-8")
    calls: list[list[str]] = []
    lock = threading.Lock()

    def fake_run(command, *, env=None, **kwargs):
        if command[1] == "--rehash" or "-o" in command:
            with lock:
                calls.append(command)
            if "-o" in command:
                Path(command[command.index("-o") + 1]).write_bytes(b"")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        modules = _probe_modules(Path(command[-1]).read_text(encoding="utf-8"))
        with lock:
            calls.append([*command, *modules])
        return answer(modules, command, env)

    monkeypatch.setattr("autoform_cli.skeleton.shutil.which", lambda executable: "/bin/lake")
    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", fake_run)
    return calls


def _answer_records(modules: tuple[str, ...], command: list[str], env: dict[str, str]):
    probe = Path(command[-1]).read_text(encoding="utf-8")
    Path(env[PROBE_OUTPUT_ENV]).write_text(_fake_module_probe(probe, Path.cwd()), encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_a_custom_runner_cannot_be_given_a_timeout_it_would_ignore(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})

    with pytest.raises(ValueError, match="a custom runner bounds its own"):
        extract_skeletons(blueprint, lean_root=project, runner=lambda probe, root: "", timeout=5)


def test_lake_freshness_is_checked_once_before_one_probe_per_module(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    calls = _fake_lake(monkeypatch, project, _answer_records)

    report = extract_skeletons(_two_module_blueprint(tmp_path), lean_root=project)

    assert report.clean
    assert calls[0] == ["/bin/lake", "--rehash", "--no-build", "build", "Skel.Main", "Skel.Uses"]
    # The helpers are built once, after the freshness check and before any probe.
    assert calls[1][1:4] == ["env", "lean", f"--root={Path(calls[1][-1]).parent}"]
    assert calls[1][-1].endswith("/autoform-skeleton-helper.lean")
    assert all(command[1:3] == ["env", "lean"] and "-o" not in command for command in calls[2:])
    assert sorted(command[4:] for command in calls[2:]) == [["Skel.Main"], ["Skel.Uses"]]


@pytest.mark.parametrize("failure", ["exit", "timeout", "malformed"])
def test_a_failed_module_probe_leaves_only_its_roots_unresolved(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    project = _project(tmp_path)

    def answer(modules, command, env):
        if "Skel.Uses" not in modules:
            return _answer_records(modules, command, env)
        if failure == "timeout":
            raise _CommandTimedOut(["lake env lean timed out after 1 seconds"])
        if failure == "malformed":
            Path(env[PROBE_OUTPUT_ENV]).write_text(
                "\n".join([_record("Skel.heavy_of_notation", found=False)] * 2), encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="error: Skel.Uses broke")

    _fake_lake(monkeypatch, project, answer)

    report = extract_skeletons(_two_module_blueprint(tmp_path), lean_root=project)

    determined, notation = report.nodes
    assert determined.complete and [d.name for d in determined.declarations] == ["Skel.observation_determined"]
    assert not notation.complete and notation.declarations == ()
    (issue,) = report.unresolved
    assert (issue.node_id, issue.declaration) == ("basics/notation", "Skel.heavy_of_notation")
    assert issue.reason.startswith("probe of module Skel.Uses failed: ")
    assert {
        "exit": "error: Skel.Uses broke",
        "timeout": "rerun with --timeout <seconds>",
        "malformed": "duplicate records for Skel.heavy_of_notation",
    }[failure] in issue.reason


def test_a_root_lean_declares_in_another_module_is_unresolved(tmp_path: Path) -> None:
    # Its packet was printed where its source was found, not where Lean declares it.
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    output = _probe_lines({**_fake_found_record(), "module": "Skel.Defs"})

    report = extract_skeletons(blueprint, lean_root=project, runner=lambda probe, root: output)

    (issue,) = report.unresolved
    assert issue.reason == "Lean declares it in module Skel.Defs, not in Skel.Main where its source was found"


@pytest.mark.parametrize("passage", ["found", "broken"])
def test_a_root_several_files_declare_fails_closed_naming_every_file(tmp_path: Path, passage: str) -> None:
    # Lean accepts a private declaration beside a public one of the same name
    # in another module, and two unrelated modules may each declare it; the
    # lexical index cannot tell which one a probe would bind.
    project = _project(tmp_path)
    for module, prefix, value in (("Cdup", "", "2 = 2"), ("Adup", "private ", "True"), ("Bdup", "", "1 = 1")):
        (project / "Skel" / f"{module}.lean").write_text(
            f"namespace Skel\n{prefix}theorem dup : {value} := sorry\nend Skel\n", encoding="utf-8"
        )
    blueprint = _blueprint(tmp_path, lean={"dup": "Skel.dup"})
    if passage == "broken":
        article = blueprint / "roadmap" / "basics" / "dup.md"
        article.write_text(
            article.read_text(encoding="utf-8").replace(
                "## Depends on", "## Sources\n\n- [book](../../sources/missing.tex#L1-L1)\n\n## Depends on"
            ),
            encoding="utf-8",
        )
    probes: list[str] = []

    def runner(probe: str, root: Path) -> str:
        probes.append(probe)
        return _probe_lines({**_fake_found_record(), "root": "Skel.dup", "module": "Skel.Adup"})

    report = extract_skeletons(blueprint, lean_root=project, runner=runner)

    assert probes == [] and report.nodes[0].declarations == ()
    (issue,) = report.unresolved
    assert issue.reason.endswith("several source files declare it: Skel/Adup.lean, Skel/Bdup.lean, Skel/Cdup.lean")
    assert issue.reason.startswith("source locator ") == (passage == "broken")


def test_an_error_on_one_root_leaves_the_rest_of_its_module_resolved(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(
        tmp_path, lean={"determined": "Skel.observation_determined", "supervision": "Skel.supervision_nonAmbiguous"}
    )
    error = f"unknown declaration in {project / 'Skel' / 'Main.lean'}"
    output = "\n".join([_probe_lines(_fake_found_record()), _record("Skel.supervision_nonAmbiguous", error=error)])

    report = extract_skeletons(blueprint, lean_root=project, runner=lambda probe, root: output)

    nodes = {node.node_id: node for node in report.nodes}
    assert nodes["basics/determined"].complete
    assert not nodes["basics/supervision"].complete
    (issue,) = report.unresolved
    assert (issue.node_id, issue.declaration) == ("basics/supervision", "Skel.supervision_nonAmbiguous")
    assert issue.reason == "the probe failed on this declaration: unknown declaration in Skel/Main.lean"


def test_a_failed_probes_reason_reads_the_same_on_every_run(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)

    def answer(modules, command, env):
        if "Skel.Uses" not in modules:
            return _answer_records(modules, command, env)
        # Lean names the probe's temporary file, the project's own paths, and
        # the helper, compiled into the extraction's scratch directory.
        stderr = (
            f"{command[-1]}:3:0: error: unknown module prefix\n"
            f"{project / 'Skel' / 'Uses.lean'}:1:0: note: imported here\n"
            f"{env['LEAN_PATH'].split(os.pathsep)[-1]}\n" + "trace line\n" * 500
        )
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    _fake_lake(monkeypatch, project, answer)
    blueprint = _two_module_blueprint(tmp_path)
    written = []
    for name in ("first.json", "second.json"):
        report = extract_skeletons(blueprint, lean_root=project)
        written.append(write_skeleton_report(report, tmp_path / name).read_bytes())

    assert written[0] == written[1]
    (issue,) = report.unresolved
    assert "<scratch>/AutoformSkeletonProbe.lean:3:0: error" in issue.reason
    assert "\nSkel/Uses.lean:1:0: note" in issue.reason and str(tmp_path) not in issue.reason
    assert "\n<scratch>\n" in issue.reason and "autoform-skeleton-" not in issue.reason
    assert len(issue.reason) <= 2000 and issue.reason.endswith(" more characters]")


def test_a_failure_every_probe_would_share_stops_the_extraction(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)

    def answer(modules, command, env):
        if "Skel.Uses" not in modules:
            return _answer_records(modules, command, env)
        olean = project / ".lake" / "packages" / "dep" / "Std" / "Vendor.olean"
        stderr = f"error: object file '{olean}' of module Std.Vendor does not exist"
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    _fake_lake(monkeypatch, project, answer)

    with pytest.raises(SkeletonError, match="hides the toolchain's own `Std`"):
        extract_skeletons(_two_module_blueprint(tmp_path), lean_root=project)

    # Without its compiled helpers no probe can run.
    probe = render_probe(imports=("Skel.Main",), roots=("Skel.x",), project_roots=("Skel",))
    (tmp_path / "helper").mkdir()
    with pytest.raises(_ProbeEnvironmentError, match="helpers are missing"):
        run_probe(probe, project, helper=tmp_path / "helper", check_freshness=False)


def test_environmental_failures_still_abort_a_per_module_extraction(tmp_path: Path, monkeypatch) -> None:
    # Labelled guard: stale artifacts concern every module, so nothing is probed.
    project = _project(tmp_path)
    calls = _fake_lake(monkeypatch, project, _answer_records)

    def stale(command, **kwargs):
        calls.append(command)
        if command[1] == "--rehash":
            return subprocess.CompletedProcess(command, 3, stdout="target is out-of-date", stderr="")
        raise AssertionError("a probe ran after a failed freshness check")

    monkeypatch.setattr("autoform_cli.skeleton._run_bounded_command", stale)

    with pytest.raises(SkeletonError, match=r"build artifacts are stale; run `lake build Skel.Main Skel.Uses`"):
        extract_skeletons(_two_module_blueprint(tmp_path), lean_root=project)
    assert calls == [["/bin/lake", "--rehash", "--no-build", "build", "Skel.Main", "Skel.Uses"]]


def test_report_is_identical_across_checkout_roots(tmp_path: Path) -> None:
    first, second = (_fake_report(tmp_path / name) for name in ("first", "second"))

    assert first.to_json() == second.to_json()


def test_report_round_trips_through_json_deterministically(tmp_path: Path) -> None:
    report = _fake_report(tmp_path)

    first = report.to_json()
    assert first == report.to_json()
    assert report.as_dict() == json.loads(first)
    assert json.loads(first)["schema"] == SKELETON_SCHEMA
    assert str(tmp_path) not in first

    path = tmp_path / "skeleton.json"
    path.write_text(first, encoding="utf-8")
    assert load_skeleton_report(path) == report

    _assert_load_rejects(path, {"schema": "something-else"})
    for schema in ("autoform-skeleton/v1", "autoform-skeleton/v2", "autoform-skeleton/v3", "autoform-skeleton/v4"):
        legacy = report.as_dict()
        legacy["schema"] = schema
        _assert_load_rejects(path, legacy)


def test_public_skeleton_compatibility_helpers(tmp_path: Path) -> None:
    report = _fake_report(tmp_path)
    node = report.nodes[0]
    declaration = node.declarations[0]

    assert report.declarations(node.node_id) == node.declarations
    assert report.declarations("missing") == ()
    assert not declaration.defines
    assert replace(declaration, kind="def").defines
    assert not replace(declaration, kind="axiom").defines

    project = tmp_path / "project"
    excerpt = source_excerpt(declaration, project)
    assert excerpt is not None and excerpt.endswith("  sorry")
    assert source_excerpt(replace(declaration, path="../outside.lean"), project) is None
    assert source_excerpt(replace(declaration, end_line=10_000), project) is None


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
    assert list(data["trusted"]) == ["Skel.Main"]
    assert sorted(data["trusted"]["Skel.Main"]) == ["Skel.Eligible", "Skel.NonAmbiguous", "Skel.Observation"]
    assert list(data["semantics"]) == ["Skel.Main"]
    assert list(data["semantics"]["Skel.Main"]) == ["Mathlib.Fake", "sorryAx"]
    assert list(data["boundary_modules"]) == ["Mathlib.Fake"]
    for node in data["nodes"]:
        (declaration,) = node["declarations"]
        assert declaration["trusted"] == ["Skel.Eligible", "Skel.NonAmbiguous", "Skel.Observation"]
        assert declaration["boundary_modules"] == ["Mathlib.Fake"]
    source = data["trusted"]["Skel.Main"]["Skel.Eligible"]["source"]
    assert first.count(json.dumps(source, ensure_ascii=False)) == 1
    path = tmp_path / "skeleton.json"
    path.write_text(first, encoding="utf-8")
    assert load_skeleton_report(path) == report

    for table, name in (("trusted", "Skel.Eligible"), ("semantics", "sorryAx"), ("boundary_modules", "Mathlib.Fake")):
        payload = json.loads(first)
        entries = payload[table]["Skel.Main"] if table in {"trusted", "semantics"} else payload[table]
        entries["Skel.Unused"] = entries[name]
        if table == "trusted":
            entries["Skel.Unused"] = dict(entries[name], name="Skel.Unused")
        _assert_load_rejects(path, payload, "not a canonical")
        del entries["Skel.Unused"], entries[name]
        _assert_load_rejects(path, payload, "mismatched")

    payload = json.loads(first)
    payload["trusted"]["Skel.Main"]["Skel.Eligible"]["name"] = "Skel.NonAmbiguous"
    _assert_load_rejects(path, payload, "mismatched shared trusted declaration")


def _two_module_report(tmp_path: Path) -> str:
    project = _project(tmp_path)
    report = extract_skeletons(_two_module_blueprint(tmp_path), lean_root=project, runner=_fake_module_probe)
    assert report.clean
    return report.to_json()


def _divergent_module_probe(probe: str, lean_root: Path) -> str:
    """Answer like ``_fake_module_probe``, but ``Skel.Uses`` sees other constants under the same names."""

    modules = _probe_modules(probe)
    lines: list[str] = []
    for root, module in _FAKE_ROOT_MODULES.items():
        if module in modules and f'"{root}"' in probe:
            record = {**_fake_found_record(), "root": root, "module": module}
            if module == "Skel.Uses":
                other = _semantic({"type": {"bvar": 0}})
                record["assumed_semantics"] = [["Mathlib.Fake", other]]
                record["axiom_semantics"] = [["sorryAx", other]]
            lines += _probe_lines(record).splitlines()
    return "\n".join(dict.fromkeys(lines))


def test_report_keys_shared_material_by_root_module(tmp_path: Path) -> None:
    project = _project(tmp_path)
    report = extract_skeletons(_two_module_blueprint(tmp_path), lean_root=project, runner=_divergent_module_probe)
    assert report.clean
    first = report.to_json()
    data = json.loads(first)
    path = tmp_path / "skeleton.json"
    path.write_text(first, encoding="utf-8")
    assert load_skeleton_report(path) == report

    # Each root module states the trusted declarations it printed, and keeps its
    # own semantics when its imports declare other constants under one name.
    assert list(data["trusted"]) == list(data["semantics"]) == ["Skel.Main", "Skel.Uses"]
    assert data["trusted"]["Skel.Main"] == data["trusted"]["Skel.Uses"]
    for name in ("Mathlib.Fake", "sorryAx"):
        assert data["semantics"]["Skel.Main"][name] != data["semantics"]["Skel.Uses"][name]

    # A declaration may name only the material printed for its own root module.
    for table, mismatch, copy in (
        ("trusted", "trusted declarations", "Skel.Other"),
        ("semantics", "assumption semantics", "Skel.Uses"),
    ):
        payload = json.loads(first)
        del payload[table]["Skel.Uses"]
        _assert_load_rejects(path, payload, f"mismatched {mismatch} for Skel.heavy_of_notation")
        payload = json.loads(first)
        payload[table][copy] = payload[table]["Skel.Main"]
        _assert_load_rejects(path, payload, "not a canonical")
        payload = json.loads(first)
        payload[table]["Skel.Other"] = {}
        _assert_load_rejects(path, payload, f"malformed shared {table} table for root module Skel.Other")

    payload = json.loads(first)
    payload["trusted"] = payload["trusted"]["Skel.Main"]
    _assert_load_rejects(path, payload)


def test_cli_reports_conflicting_shared_material_as_an_error(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = _project(tmp_path)
    blueprint = _two_module_blueprint(tmp_path)

    def probe(program: str, lean_root: Path) -> str:
        # The two probes disagree about the compiled files of one boundary module.
        output = _fake_module_probe(program, lean_root)
        if _probe_modules(program) == ("Skel.Uses",):
            output = output.replace("Skel/Defs.lean", "Skel/Main.lean")
        return output

    _fake_default_probe(monkeypatch, probe)
    output = tmp_path / "skeleton.json"

    assert main(["skeleton", str(blueprint), "--lean-root", str(project), "--output", str(output)]) == 2

    assert "error: conflicting module identity for Mathlib.Fake in one skeleton report" in capsys.readouterr().err
    assert not output.exists()


def test_report_loader_rejects_a_v4_report_with_a_clear_message(tmp_path: Path) -> None:
    payload = json.loads(_two_module_report(tmp_path))
    payload["schema"] = "autoform-skeleton/v4"
    payload["trusted"] = payload["trusted"]["Skel.Main"]
    path = tmp_path / "skeleton.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SkeletonError) as refused:
        load_skeleton_report(path)

    assert "is an autoform-skeleton/v4 report; this version reads only autoform-skeleton/v5 reports" in str(
        refused.value
    )


@pytest.mark.parametrize("damage", ["nested", "a-number-too-long"])
def test_a_report_or_packet_manifest_that_cannot_be_decoded_is_refused(tmp_path: Path, damage: str) -> None:
    text = _undecodable_json(damage)
    path = tmp_path / "skeleton.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SkeletonError, match=f"^cannot read skeleton report {re.escape(str(path))}: "):
        load_skeleton_report(path)

    packets = tmp_path / "packets"
    packets.mkdir()
    (packets / PACKET_MANIFEST).write_text(text, encoding="utf-8")
    with pytest.raises(SkeletonError, match="refusing to overwrite non-Autoform packet output"):
        write_packets(_fake_report(tmp_path), packets)
    assert (packets / PACKET_MANIFEST).read_text(encoding="utf-8") == text


def test_report_loader_refuses_a_lone_surrogate_as_unreadable(tmp_path: Path) -> None:
    text = _fake_report(tmp_path).to_json()
    assert '"lean_version":"' in text
    path = tmp_path / "skeleton.json"
    path.write_text(text.replace('"lean_version":"', '"lean_version":"\\ud800', 1), encoding="utf-8")

    with pytest.raises(SkeletonError) as refused:
        load_skeleton_report(path)

    reason = "it escapes a lone surrogate, '\\ud800', which UTF-8 cannot encode"
    assert refused.value.issues == (f"cannot read skeleton report {path}: {reason}",)


def test_report_loader_rejects_scope_tampering(tmp_path: Path) -> None:
    report = _fake_report(tmp_path)
    path = tmp_path / "skeleton.json"

    payload = report.as_dict()
    payload["target_count"] = 0
    _assert_load_rejects(path, payload, "not a canonical")

    payload = report.as_dict()
    payload["selection"]["mode"] = []
    _assert_load_rejects(path, payload, "invalid skeleton selection mode")

    payload = report.as_dict()
    payload["nodes"][0] = replace(report.nodes[0], declarations=(), complete=False).as_dict()
    payload["trusted"] = payload["semantics"] = payload["boundary_modules"] = {}
    payload["unresolved"] = [
        {"declaration": "made.up", "node_id": "basics/determined", "reason": "missing"}
    ]
    _assert_load_rejects(path, payload, "mismatched unresolved declarations")

    payload = report.as_dict()
    payload["targets"][0]["declarations"].append("Skel.observation_determined")
    _assert_load_rejects(path, payload, "duplicate target declarations")


_INCOHERENT_REPORTS = {
    "has an invalid selection mode": lambda report: replace(report, selection="partial"),
    "contains an invalid target set": lambda report: replace(report, targets=report.targets[::-1]),
    "contains an invalid skeleton article selection": lambda report: replace(
        report, selected_nodes=report.selected_nodes[::-1], nodes=report.nodes[::-1]
    ),
    "contains an incomplete all-article selection": lambda report: replace(
        report, selected_nodes=report.selected_nodes[:1], nodes=report.nodes[:1]
    ),
    "contains an empty filtered article selection": lambda report: replace(
        report, selection="filtered", selected_nodes=(), nodes=()
    ),
    "contains duplicate skeleton article ids": lambda report: replace(
        report, nodes=(report.nodes[0], report.nodes[0])
    ),
    "does not contain exactly its selected articles": lambda report: replace(
        report, targets=(), selected_nodes=()
    ),
    "contains an untargeted declaration for basics/determined": lambda report: replace(
        report, targets=(("basics/determined", ("Skel.other",)), *report.targets[1:])
    ),
    "contains mismatched unresolved declarations": lambda report: replace(
        report,
        unresolved=(UnresolvedTarget("basics/determined", "Skel.observation_determined", "missing"),),
    ),
}


@pytest.mark.parametrize("issue", sorted(_INCOHERENT_REPORTS))
def test_an_incoherent_report_cannot_be_constructed(tmp_path: Path, issue: str) -> None:
    project = _project(tmp_path)
    report = extract_skeletons(_two_module_blueprint(tmp_path), lean_root=project, runner=_fake_module_probe)
    assert report.clean and [node.node_id for node in report.nodes] == ["basics/determined", "basics/notation"]

    # dataclasses.replace runs the same checks as the loader, so no copy escapes them.
    with pytest.raises(SkeletonError, match=f"skeleton report {re.escape(issue)}"):
        _INCOHERENT_REPORTS[issue](report)


def test_report_loader_rejects_mismatched_hashes_and_trust_identities(tmp_path: Path) -> None:
    report = _fake_report(tmp_path)
    path = tmp_path / "skeleton.json"

    payload = report.as_dict()
    payload["nodes"][0]["declarations"][0]["hash"] = "sha256:" + "0" * 64
    _assert_load_rejects(path, payload, "not a canonical")

    payload = report.as_dict()
    payload["nodes"][0]["declarations"][0]["statement"] = "theorem t : True := by trivial"
    payload["nodes"][0]["declarations"][0]["statement_comments"] = []
    _assert_load_rejects(path, payload, "not a canonical")

    payload = report.as_dict()
    payload["nodes"][0]["passage"] = "different source theorem"
    payload["nodes"][0]["passage_locator"] = "sources/book.tex#L1-L1"
    _assert_load_rejects(path, payload, "not a canonical")

    payload = report.as_dict()
    declaration = payload["nodes"][0]["declarations"][0]
    declaration["skeleton_lines"] = float(declaration["skeleton_lines"])
    _assert_load_rejects(path, payload, "not a canonical")

    payload = report.as_dict()
    payload["nodes"][0]["declarations"][0]["assumed"] = ["Mathlib.Other"]
    _assert_load_rejects(path, payload, "mismatched assumption semantics")

    payload = report.as_dict()
    trusted = payload["trusted"]["Skel.Main"][payload["nodes"][0]["declarations"][0]["trusted"][0]]
    trusted["kind"] = "theorem"
    trusted["semantic"] = _semantic({"type": {"sort": {"zero": None}}})
    _assert_load_rejects(path, payload, "proof-bearing source is forbidden")


def test_text_report_quotes_the_sources_a_reader_must_trust(tmp_path: Path) -> None:
    report = _fake_report(tmp_path)

    text = format_report(report, lean_root=tmp_path / "project")

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


def _cli(tmp_path: Path, monkeypatch, *arguments: object) -> int:
    """Run `autoform skeleton` on the fixture's one-article blueprint against the fake probe."""

    _project(tmp_path)
    _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    _fake_default_probe(monkeypatch, lambda probe, root: _fake_probe_output())
    command = ["skeleton", tmp_path / "blueprint", "--lean-root", tmp_path / "project", *arguments]
    return main([str(argument) for argument in command])


def test_cli_writes_the_artifact_and_fails_on_unresolved_names(tmp_path: Path, capsys, monkeypatch) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(
        tmp_path,
        lean={"determined": "Skel.observation_determined", "phantom": "Skel.doesNotExist"},
    )
    _fake_default_probe(monkeypatch, lambda probe, root: _fake_probe_output())
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
    calls: list[dict[str, object]] = []

    def fake_run_probe(probe: str, root: Path, **kwargs: object) -> str:
        calls.append(kwargs)
        return _fake_probe_output()

    def fake_build_helper(root: Path, directory: Path, **kwargs: object) -> Path:
        calls.append(kwargs)
        return directory

    monkeypatch.setattr("autoform_cli.skeleton._check_build_freshness", lambda root, modules, **kwargs: None)
    monkeypatch.setattr("autoform_cli.skeleton._build_probe_helper", fake_build_helper)
    monkeypatch.setattr("autoform_cli.skeleton.run_probe", fake_run_probe)
    command = ["skeleton", str(blueprint), "--lean-root", str(project), "--json"]
    assert main([*command, "--timeout", "1800"]) == 0
    # The timeout bounds the helper build and the probe; Lake's freshness check
    # already ran for all of them.
    helper = calls[1].pop("helper")
    assert calls == [{"timeout": 1800.0}, {"timeout": 1800.0, "check_freshness": False}]
    assert isinstance(helper, Path) and helper.name.startswith("autoform-skeleton-")
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
    output = tmp_path / "skeleton.json"
    output.mkdir()

    assert _cli(tmp_path, monkeypatch, "--output", output) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "report output exists and is not a regular file" in captured.err


def test_cli_keeps_json_stdout_machine_readable_with_packets(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    packets = tmp_path / "packets"

    assert _cli(tmp_path, monkeypatch, "--packets", packets, "--json") == 0

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
    packets = tmp_path / "packets"
    packets.mkdir()
    (packets / "keep.txt").write_text("mine\n", encoding="utf-8")

    assert _cli(tmp_path, monkeypatch, "--packets", packets) == 2

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
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    output = tmp_path / "skeleton.json"
    output.write_text("old report\n", encoding="utf-8")

    result = _cli(tmp_path, monkeypatch, "--packets", packets, "--passages", passages, "--output", output)

    assert result == 0
    assert (packets / PACKET_MANIFEST).is_file()
    assert (passages / PACKET_MANIFEST).is_file()
    assert load_skeleton_report(output).clean
    assert list(tmp_path.glob(".*.autoform-*")) == []
    assert capsys.readouterr().err == ""


def test_cli_does_not_publish_packets_when_report_staging_fails(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    output = tmp_path / "skeleton.json"

    def fail_report_stage(report: SkeletonReport, destination: Path, stages: list[Path]):
        raise OSError("simulated report staging failure")

    monkeypatch.setattr("autoform_cli.skeleton._stage_report_output", fail_report_stage)

    result = _cli(tmp_path, monkeypatch, "--packets", packets, "--passages", passages, "--output", output)

    assert result == 2
    assert not packets.exists()
    assert not passages.exists()
    assert not output.exists()
    assert list(tmp_path.glob(".*.autoform-stage-*")) == []
    assert "simulated report staging failure" in capsys.readouterr().err


def test_cli_rolls_back_packet_trees_when_report_commit_fails(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    output = tmp_path / "skeleton.json"

    def fail_report_install(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        if "autoform-stage" in source_path.name and Path(destination) == output:
            raise OSError("simulated report commit failure")
        _install_output(source_path, Path(destination))

    monkeypatch.setattr("autoform_cli.skeleton._install_output", fail_report_install)

    result = _cli(tmp_path, monkeypatch, "--packets", packets, "--passages", passages, "--output", output)

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
    _fake_default_probe(monkeypatch, lambda probe, root: _fake_probe_output())
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

    def interrupt_after_report_install(source: str | Path, destination: str | Path) -> None:
        _install_output(Path(source), Path(destination))
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
    report = _fake_report(tmp_path)
    output = tmp_path / "skeleton.json"
    output.write_text("old report\n", encoding="utf-8")

    def replace_before_install(stage: Path, destination: Path) -> None:
        if destination == output:
            output.write_text("concurrent report\n", encoding="utf-8")
        _install_output(stage, destination)

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
    report = _fake_report(tmp_path)
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
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    report_target = tmp_path / "report-target.json"
    report_target.write_text("keep me\n", encoding="utf-8")
    output = tmp_path / "skeleton.json"
    output.symlink_to(report_target)

    result = _cli(tmp_path, monkeypatch, "--packets", packets, "--passages", passages, "--output", output)

    assert result == 2
    assert not packets.exists()
    assert not passages.exists()
    assert output.is_symlink()
    assert report_target.read_text(encoding="utf-8") == "keep me\n"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refusing symlink report output" in captured.err


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
    _build(project)
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

    _replace_source(project / "Skel" / "Main.lean", "∃ y, o.admits y ∧ ∀ z, o.admits z → z = y := by", "True := by")
    with pytest.raises(SkeletonError, match="build artifacts are stale"):
        extract_skeletons(blueprint, lean_root=project)


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_probe_records_bypass_the_command_capture_and_other_output(tmp_path: Path, monkeypatch) -> None:
    # Lean holds a command's `IO.println` output until the command ends, then
    # prints it at a cost quadratic in its size. A probe that leaves before its
    # command ends shows whether its records went past that capture, and a large
    # message printed meanwhile must not split one of them.
    project = _project(tmp_path)
    _build(project, "Skel.Main")
    probe = render_probe(
        imports=("Skel.Main",), roots=("Skel.observation_determined",), project_roots=("Skel",)
    )
    helper = _render_probe_helper()
    # The probe leaves once every root's records are written.
    loop_end = "  finally\n    out.flush\n"
    assert helper.count(loop_end) == 1
    leave = "    (← IO.getStdout).flush\n    let _ : Unit ← IO.Process.exit 0\n"
    noise = "#eval IO.println (String.mk (List.replicate 3000000 'x'))\n\n"
    command = "run_cmd AutoformSkeleton.main"
    assert command in probe
    helper = helper.replace(loop_end, f"{leave}{loop_end}")
    probe = probe.replace(command, f"{noise}{command}")
    monkeypatch.setattr("autoform_cli.skeleton._render_probe_helper", lambda: helper)

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
    _build(project, f"Skel.{module}")
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


_PRINTING_MODULES = {
    # Root-level names the probe's own helpers use.
    "PktCollide": (
        "inductive Expr where\n  | lit : Nat → Expr\n"
        "def Name : Type := Nat\n"
        "def Syntax : Type := Nat\n"
        "def Json : Type := Nat\n"
        "def Environment : Type := Nat\n"
        "def Piece : Type := Nat\n"
        "namespace Skel.PktCollide\n"
        "def size : Expr → Nat := fun _ => 0\n"
        "theorem size_lit : size (Expr.lit 1) = 0 := rfl\n"
        "def zero : Name := (0 : Nat)\n"
        "theorem zero_eq : zero = zero := rfl\n"
        "end Skel.PktCollide\n"
    ),
    # Root-level names inside namespaces the helpers open.
    "PktPrint": (
        "inductive Term where\n  | var : Nat → Term\n"
        "namespace Meta\ndef weight (n : Nat) : Nat := n + 1\nend Meta\n"
        "namespace Skel.PktPrint\n"
        "def ident (t : Term) : Term := t\n"
        "theorem ident_var : ident (Term.var 1) = Term.var 1 := rfl\n"
        "theorem weight_zero : Meta.weight 0 = 1 := rfl\n"
        "end Skel.PktPrint\n"
    ),
}


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_signatures_print_as_in_a_file_that_imports_the_module(tmp_path: Path) -> None:
    project = _project(tmp_path)
    for module, source in _PRINTING_MODULES.items():
        (project / "Skel" / f"{module}.lean").write_text(source, encoding="utf-8")
    _build(project, *(f"Skel.{module}" for module in _PRINTING_MODULES))
    blueprint = _blueprint(
        tmp_path,
        lean={
            "sizes": "Skel.PktCollide.size_lit",
            "zeros": "Skel.PktCollide.zero_eq",
            "idents": "Skel.PktPrint.ident_var",
            "weights": "Skel.PktPrint.weight_zero",
        },
    )

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean, report.unresolved
    printed: dict[str, dict[str, str]] = {}
    for node in report.nodes:
        for declaration in node.declarations:
            shown = printed.setdefault(declaration.module, {})
            shown[declaration.name] = declaration.signature
            shown.update((item.name, item.signature) for item in declaration.trusted)
    assert set(printed) == {"Skel.PktCollide", "Skel.PktPrint"}
    assert {"Expr", "Name", "Skel.PktCollide.size"} <= set(printed["Skel.PktCollide"])
    assert {"Term", "Meta.weight", "Skel.PktPrint.ident"} <= set(printed["Skel.PktPrint"])
    for module, signatures in printed.items():
        check = tmp_path / f"check-{module}.lean"
        check.write_text(
            f"import {module}\n"
            "set_option pp.funBinderTypes true\n"
            "set_option pp.coercions.types true\n"
            + "".join(f"#check {name}\n" for name in signatures),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["lake", "env", "lean", str(check)], cwd=project, capture_output=True, text=True, timeout=600, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.splitlines() == list(signatures.values())


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_project_module_named_like_the_probe_helpers_stops_extraction(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    _build(project, "Skel.Main")
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "autoform-skeleton-helper.olean").write_bytes(b"")
    monkeypatch.setenv("LEAN_PATH", str(shadow))
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})

    with pytest.raises(SkeletonError, match="already provides a module named «autoform-skeleton-helper»"):
        extract_skeletons(blueprint, lean_root=project)


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_declaration_the_probe_cannot_print_leaves_its_module_resolved(tmp_path: Path) -> None:
    project = _built_module(
        tmp_path,
        "PktRefuse",
        "import Lean\n"
        "open Lean PrettyPrinter Delaborator\n"
        "namespace Skel.PktRefuse\n"
        "def refused (n : Nat) : Nat := n\n"
        '@[app_delab refused] def delabRefused : Delab := throwError "this delaborator refuses"\n'
        "theorem bad : refused 1 = 1 := rfl\n"
        "theorem good : 1 = 1 := rfl\n"
        "end Skel.PktRefuse\n",
    )
    blueprint = _blueprint(tmp_path, lean={"bad": "Skel.PktRefuse.bad", "good": "Skel.PktRefuse.good"})

    report = extract_skeletons(blueprint, lean_root=project)

    nodes = {node.node_id: node for node in report.nodes}
    assert nodes["basics/good"].complete
    (issue,) = report.unresolved
    assert (issue.node_id, issue.declaration) == ("basics/bad", "Skel.PktRefuse.bad")
    assert issue.reason == "the probe failed on this declaration: this delaborator refuses"


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_dead_copy_after_exit_does_not_move_a_declaration(tmp_path: Path) -> None:
    # PktDead sorts first and, after `#exit`, repeats PktLive's theorem, which
    # Lean never reads.
    project = _project(tmp_path)
    (project / "Skel" / "PktLive.lean").write_text(
        "namespace Skel.PktLive\ntheorem rX : 5 = 5 := rfl\nend Skel.PktLive\n", encoding="utf-8"
    )
    (project / "Skel" / "PktDead.lean").write_text(
        "import Skel.PktLive\n"
        "namespace Skel.PktDead\ntheorem real : True := trivial\nend Skel.PktDead\n"
        "#exit\n"
        "namespace Skel.PktLive\ntheorem rX : 5 = 5 := rfl\nend Skel.PktLive\n",
        encoding="utf-8",
    )
    build = subprocess.run(
        ["lake", "build", "Skel.PktDead"], cwd=project, capture_output=True, text=True, timeout=600, check=False
    )
    assert build.returncode == 0, build.stdout + build.stderr
    blueprint = _blueprint(tmp_path, lean={"live": "Skel.PktLive.rX"})

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    (declaration,) = report.nodes[0].declarations
    assert (declaration.module, declaration.path) == ("Skel.PktLive", "Skel/PktLive.lean")


def _comment_range(source: str, opener: str, closer: str | None = None) -> tuple[int, int]:
    """The UTF-8 byte range from ``opener`` up to the end of the next ``closer``,
    or up to the end of its line when there is no closer."""

    data = source.encode()
    start = data.index(opener.encode())
    if closer is None:
        return start, data.index(b"\n", start)
    return start, data.index(closer.encode(), start) + len(closer.encode())


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_source_shows_only_comments_every_possible_token_table_agrees_on(tmp_path: Path) -> None:
    # Each token below changes how the text before it lexes: `++"` and `++r`
    # move a string's quotes, `+-` joins a comment opener's first `-`, and `+/`
    # and `+/-` swallow a block comment's `/`. In PktLexAfter they are declared
    # after the sources that contain them, so Lean read those as comments, and
    # the probe, which proves the tokens absent there, shows the source without
    # them. In PktLexBefore they may be active, so Lean may have read the same
    # text as code, and the probe cannot tell; it shows none of it.
    project = _project(tmp_path)
    (project / "Skel" / "PktLexAfter.lean").write_text(
        "namespace Skel.PktLexAfter\n"
        'def leakQ : String := "a" ++" b " -- KEEPOUT_Q "\n'
        '  ++ "c"\n'
        'def leakR : String := "a" ++r"\\" -- KEEPOUT_R "\n'
        '  ++ "z"\n'
        "def joined (a b : Nat) : Nat := a +-- KEEPOUT_J\n"
        "  b\n"
        "def opened (a b : Nat) : Nat := a +/- KEEPOUT_O -/ b\n"
        "theorem lexRoot (h : leakQ = leakR ∧ joined = opened) : True := trivial\n"
        'infixl:65 " ++\\" " => fun (a _b : String) => a\n'
        'infixl:65 " ++r " => fun (a _b : String) => a\n'
        'infixl:65 " +- " => Nat.sub\n'
        'infixl:65 " +/ " => Nat.sub\n'
        "end Skel.PktLexAfter\n",
        encoding="utf-8",
    )
    (project / "Skel" / "PktLexBefore.lean").write_text(
        "namespace Skel.PktLexBefore\n"
        'infixl:65 " ++\\" " => fun (a _b : String) => a\n'
        'infixl:65 " ++r " => fun (a _b : String) => a\n'
        'infixl:65 " +- " => HSub.hSub\n'
        'infixl:65 " +/- " => HAdd.hAdd\n'
        'infixl:65 " -/ " => HSub.hSub\n'
        'def leakQ (b : String → String) : String := "a" ++" b " -- CODE_Q "\n'
        '  ++ "c"\n'
        'def leakR : String := "a" ++r"\\" -- CODE_R "\n'
        '  ++ "z"\n'
        "def joined (a : Int) (code : Int → Int) : Int := a +-- code\n"
        "  a\n"
        "def opened (b : Nat) : Nat := 1 +/- b -/ 2\n"
        "theorem lexRoot (h : leakQ = leakQ ∧ leakR = leakR ∧ joined = joined ∧ opened = opened) : True := trivial\n"
        "end Skel.PktLexBefore\n",
        encoding="utf-8",
    )
    _build(project, "Skel.PktLexAfter", "Skel.PktLexBefore")
    blueprint = _blueprint(
        tmp_path, lean={"after": "Skel.PktLexAfter.lexRoot", "before": "Skel.PktLexBefore.lexRoot"}
    )

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    after = report.node("basics/after")
    assert after is not None
    trusted = {item.name.rsplit(".", 1)[1]: item for item in after.declarations[0].trusted}
    for name, opener, closer in [("leakQ", "--", None), ("leakR", "--", None), ("joined", "--", None),
                                 ("opened", "/-", "-/")]:
        item = trusted[name]
        assert item.source is not None and not item.source_withheld, name
        assert item.source_comments == (_comment_range(item.source, opener, closer),), name
    assert "KEEPOUT" not in after.blind_text()
    before = report.node("basics/before")
    assert before is not None
    for item in before.declarations[0].trusted:
        assert item.source is None and item.source_withheld, item.name


def _assert_withheld(report: SkeletonReport, node_id: str, *names: str) -> None:
    """Assert that ``node_id`` of the clean ``report`` trusts exactly ``names``, all withheld, and shows no secret."""

    node = report.node(node_id)
    assert report.clean and node is not None
    trusted = node.declarations[0].trusted
    assert sorted(item.name for item in trusted) == sorted(names)
    for item in trusted:
        assert item.source is None and item.source_withheld, item.name
    assert "SECRET" not in node.blind_text()


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_token_registered_without_a_placeable_declaration_withholds_the_source(tmp_path: Path) -> None:
    # Both modules make `!"` a token before their sources, so Lean read the
    # `-- SECRET_…` text there as a comment, which a parse without the token
    # reads as code in a string. AttrUse registers a parser declared in
    # another module; RawUse writes the token and a parser entry naming that
    # module's parser itself. Neither declaration is in the module, so its
    # position bounds nothing there, and the token may be active.
    project = _project(tmp_path)
    parser = (
        "import Lean\n"
        "open Lean Parser\n"
        "namespace Skel\n"
        'syntax (name := bqKind) term:65 " ¡¡ " term:66 : term\n'
        + "\n" * 40
        + "{attr}def bangQuoteP : TrailingParser := "
        'trailingNode `Skel.bqKind 65 0 (symbol " !\\" " >> termParser 66)\n'
        "end Skel\n"
    )
    (project / "Skel" / "AttrDef.lean").write_text(
        parser.format(attr="@[run_parser_attribute_hooks] "), encoding="utf-8"
    )
    (project / "Skel" / "AttrUse.lean").write_text(
        "import Skel.AttrDef\n"
        "attribute [term_parser] Skel.bangQuoteP\n"
        "macro_rules | `($a ¡¡ $_b) => pure a\n"
        'def Skel.attrLeak : String := "a" !" -- SECRET_ATTR "\n'
        '  "b"\n'
        "theorem Skel.attrRoot (h : Skel.attrLeak = Skel.attrLeak) : True := trivial\n",
        encoding="utf-8",
    )
    (project / "Skel" / "RawDef.lean").write_text(parser.format(attr=""), encoding="utf-8")
    (project / "Skel" / "RawUse.lean").write_text(
        "import Skel.RawDef\n"
        "open Lean Elab Command\n"
        'run_cmd do Lean.Parser.parserExtension.add (.token "!\\""); '
        "Lean.Parser.parserExtension.add (.parser `term ``Skel.bangQuoteP false "
        '(Lean.Parser.trailingNode `Skel.bqKind 65 0 (Lean.Parser.symbol " !\\" " >> '
        "Lean.Parser.termParser 66)) 0)\n"
        "macro_rules | `($a ¡¡ $_b) => pure a\n"
        'def Skel.rawLeak : String := "a" !" -- SECRET_RAW "\n'
        '  "b"\n'
        "theorem Skel.rawRoot (h : Skel.rawLeak = Skel.rawLeak) : True := trivial\n",
        encoding="utf-8",
    )
    _build(project, "Skel.AttrUse", "Skel.RawUse")
    blueprint = _blueprint(tmp_path, lean={"attr": "Skel.attrRoot", "raw": "Skel.rawRoot"})

    report = extract_skeletons(blueprint, lean_root=project)

    for node_id, name in [("basics/attr", "Skel.attrLeak"), ("basics/raw", "Skel.rawLeak")]:
        _assert_withheld(report, node_id, name)

@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_metaprogram_token_before_a_later_parser_entry_withholds_the_source(tmp_path: Path) -> None:
    # A metaprogram makes `!"` a token before `tieLeak`, so Lean read the
    # `-- SECRET_TIE "` text there as a comment, and the `example` holds only
    # because it did. The parser entry written later names `lateP`, declared
    # after `tieLeak`, and writes no token of its own. Its position bounds that
    # entry alone, not the earlier token, which may be active at `tieLeak`.
    project = _project(tmp_path)
    (project / "Skel" / "TieLeak.lean").write_text(
        "import Lean\n"
        "open Lean Parser\n"
        "namespace Skel\n"
        'syntax (name := nrBang) term:65 &" !\\" " term:66 : term\n'
        "@[macro Skel.nrBang] def nrBangMacro : Macro := fun stx => pure stx[0]\n"
        "end Skel\n"
        'run_cmd Lean.Parser.parserExtension.add (.token "!\\"")\n'
        'def Skel.tieLeak : String := "a" !" -- SECRET_TIE "\n'
        '  "b"\n'
        'example : Skel.tieLeak = "a" := rfl\n'
        'def Skel.lateP : Parser := symbol " !\\" " >> termParser 66\n'
        "run_cmd Lean.Parser.parserExtension.add (.parser `term ``Skel.lateP false "
        '(Lean.Parser.symbol " !\\" " >> Lean.Parser.termParser 66) 0)\n'
        "theorem Skel.tieRoot (h : Skel.tieLeak = Skel.tieLeak) : True := trivial\n",
        encoding="utf-8",
    )
    _build(project, "Skel.TieLeak")
    blueprint = _blueprint(tmp_path, lean={"tie": "Skel.tieRoot"})

    report = extract_skeletons(blueprint, lean_root=project)

    _assert_withheld(report, "basics/tie", "Skel.tieLeak")


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_scoped_token_an_open_may_activate_withholds_the_source(tmp_path: Path) -> None:
    # `namespace Leak` activates `!"` for `scopeLeak`, whose `_root_` name
    # lives outside `Leak`, and an `open` with `Leak` on its next line
    # activates it for `tabLeak`. Read as code, the `-- SECRET_…` text is a
    # string. A parse without the scoped parser fails on its token there, so
    # the probe must not take that failure as evidence the token was absent.
    project = _project(tmp_path)
    (project / "Skel" / "ScopeTok.lean").write_text(
        "namespace Leak\n"
        'scoped infixl:65 " !\\" " => fun (a _b : String) => a\n'
        "end Leak\n",
        encoding="utf-8",
    )
    (project / "Skel" / "ScopeUse.lean").write_text(
        "import Skel.ScopeTok\n"
        "namespace Leak\n"
        'def _root_.Skel.scopeLeak : String := "a" !" -- SECRET_ROOTNS "\n'
        '  "b"\n'
        "end Leak\n"
        "open\n"
        "  Leak\n"
        'def Skel.tabLeak : String := "a" !" -- SECRET_TAB "\n'
        '  "b"\n'
        "theorem Skel.scopeRoot (h : Skel.scopeLeak = Skel.tabLeak) : True := trivial\n",
        encoding="utf-8",
    )
    _build(project, "Skel.ScopeUse")
    blueprint = _blueprint(tmp_path, lean={"scope": "Skel.scopeRoot"})

    report = extract_skeletons(blueprint, lean_root=project)

    _assert_withheld(report, "basics/scope", "Skel.scopeLeak", "Skel.tabLeak")



@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_quotation_naming_a_parser_withholds_the_source(tmp_path: Path) -> None:
    # Inside `namespace Skel.Dq`, Lean resolves the `bar` of `` `(bar| …) `` to
    # `Skel.Dq.bar` and reads the quotation with its `!"` token, so the
    # `-- SECRET_DYNQ "` text was a comment, as the `run_cmd` checks. Resolved
    # outside the namespace, `bar` is the root parser, under which that text
    # is a string. A `term` quotation names a category, which resolves the
    # same everywhere, and keeps its comment ranges.
    project = _project(tmp_path)
    (project / "Skel" / "DynQ.lean").write_text(
        "import Lean\n"
        "open Lean\n"
        "namespace Skel.Dq\n"
        'syntax bar := "!\\"" str\n'
        "end Skel.Dq\n"
        'syntax bar := "!" str str\n'
        "namespace Skel.Dq\n"
        'def leak : MacroM Syntax := `(bar| !" -- SECRET_DYNQ "\n'
        '  "x")\n'
        "def cat : MacroM Syntax := `(term| 1 + -- note cat\n"
        "  2)\n"
        "run_cmd do\n"
        "  let s ← Lean.Elab.liftMacroM leak\n"
        '  unless s.isOfKind `Skel.Dq.bar && s.getNumArgs == 2 do throwError "read as code"\n'
        "end Skel.Dq\n"
        "theorem Skel.dynRoot (h : Skel.Dq.leak = Skel.Dq.cat) : True := trivial\n",
        encoding="utf-8",
    )
    _build(project, "Skel.DynQ")
    blueprint = _blueprint(tmp_path, lean={"dyn": "Skel.dynRoot"})

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    node = report.node("basics/dyn")
    assert node is not None
    trusted = {item.name: item for item in node.declarations[0].trusted}
    assert set(trusted) == {"Skel.Dq.leak", "Skel.Dq.cat"}
    leak = trusted["Skel.Dq.leak"]
    assert leak.source is None and leak.source_withheld
    cat = trusted["Skel.Dq.cat"]
    assert cat.source is not None and not cat.source_withheld
    assert cat.source_comments == (_comment_range(cat.source, "--", None),)
    assert "SECRET" not in node.blind_text()

@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_an_open_inside_a_source_withholds_only_what_follows_it(tmp_path: Path) -> None:
    # An `open … in` changes how Lean reads the text after it, which the
    # probe's parse cannot follow. In `innerLeak` it activates `!"`, so Lean
    # read the `-- SECRET_INNER "` text as a comment; a parse with `Leak`
    # active from the start fails on `#[[1]]`, so only the inner open does.
    # A statement before the open is still cut: `oin` keeps its statement,
    # while `instmt`, with the open inside its statement, does not.
    project = _project(tmp_path)
    (project / "Skel" / "OinTok.lean").write_text(
        "namespace Leak\n"
        'scoped infixl:65 " +++ " => Nat.add\n'
        'scoped infixl:65 " !\\" " => fun (a _b : String) => a\n'
        'scoped notation "#[[" => (0 : Nat)\n'
        "end Leak\n",
        encoding="utf-8",
    )
    (project / "Skel" / "OinUse.lean").write_text(
        "import Skel.OinTok\n"
        'def Skel.innerLeak : String := if #[[1]].size = 1 then (open Leak in "a" !" -- SECRET_INNER "\n'
        '  "b") else ""\n'
        'example : Skel.innerLeak = "a" := rfl\n'
        "open Leak\n"
        "theorem Skel.oin : 1 +++ 2 = 3 := by open Leak in rfl\n"
        "theorem Skel.instmt : (open Leak in 1 +++ 2) = 3 := rfl\n"
        "theorem Skel.oinRoot (h : Skel.innerLeak = Skel.innerLeak) : True := trivial\n",
        encoding="utf-8",
    )
    _build(project, "Skel.OinUse")
    blueprint = _blueprint(tmp_path, lean={"oin": "Skel.oin", "instmt": "Skel.instmt", "inner": "Skel.oinRoot"})

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    oin = report.node("basics/oin")
    assert oin is not None
    assert oin.declarations[0].statement == "theorem Skel.oin : 1 +++ 2 = 3"
    instmt = report.node("basics/instmt")
    assert instmt is not None
    assert instmt.declarations[0].statement is None
    _assert_withheld(report, "basics/inner", "Skel.innerLeak")


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_choice_node_holding_the_value_leaves_the_statement_unknown(tmp_path: Path) -> None:
    # `thmB` and the builtin `theorem` both parse all of `Skel.amb`, so the
    # parse is a choice node, whose alternatives come in the order Lean added
    # their parsers, the last first: Lean elaborated `thmB` (`Skel.viaB`
    # exists), whose value starts after `let y`, not at the first `:=`. The
    # probe does not rebuild that order for scoped and own entries, so a
    # choice node holding a value leaves the statement unknown.
    project = _project(tmp_path)
    (project / "Skel" / "ChUse.lean").write_text(
        "import Lean\n"
        "open Lean Parser Command\n"
        'syntax (name := thmB) "theorem " ident " : " term " := " "let " ident declVal : command\n'
        "open Elab Command in\n"
        "@[command_elab thmB] def elabB : CommandElab := fun stx => do\n"
        "  elabCommand (← `(theorem $(⟨stx[1]⟩):ident : $(⟨stx[3]⟩) := by trivial))\n"
        "  elabCommand (← `(def $(mkIdent `Skel.viaB) : Nat := 0))\n"
        "theorem Skel.amb : True := let y := 0\n"
        "  trivial\n"
        "example : Skel.viaB = 0 := rfl\n",
        encoding="utf-8",
    )
    _build(project, "Skel.ChUse")
    blueprint = _blueprint(tmp_path, lean={"amb": "Skel.amb"})

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    amb = report.node("basics/amb")
    assert amb is not None
    assert amb.declarations[0].statement is None


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_the_probe_rebuilds_each_modules_grammar_once(tmp_path: Path) -> None:
    # Two roots in CacheA trust declarations of CacheA and CacheB. Each
    # module's grammar is rebuilt once per probe, however many sources and
    # roots need it, and the probe says so once per module.
    project = _project(tmp_path)
    (project / "Skel" / "CacheB.lean").write_text(
        "namespace Skel.CacheB\n"
        "def b1 (n : Nat) : Nat := -- note b1\n"
        "  n\n"
        "end Skel.CacheB\n",
        encoding="utf-8",
    )
    (project / "Skel" / "CacheA.lean").write_text(
        "import Skel.CacheB\n"
        "namespace Skel.CacheA\n"
        'infixl:65 " +++ " => Nat.add\n'
        "def a1 (n : Nat) : Nat := -- note a1\n"
        "  n +++ 1\n"
        "def a2 (n : Nat) : Nat := -- note a2\n"
        "  a1 n +++ Skel.CacheB.b1 n\n"
        "theorem rootA (h : a2 0 = 1) : True := trivial\n"
        "theorem rootB (h : a1 0 = Skel.CacheB.b1 1) : True := trivial\n"
        "end Skel.CacheA\n",
        encoding="utf-8",
    )
    _build(project, "Skel.CacheA")
    roots = ("Skel.CacheA.rootA", "Skel.CacheA.rootB")
    probe = render_probe(imports=("Skel.CacheA",), roots=roots, project_roots=("Skel",))

    output = run_probe(probe, project)

    records = parse_probe_output(output, expected_roots=roots)
    built = [
        json.loads(line[len(PROBE_MARKER) :])["name"]
        for line in output.splitlines()
        if line.startswith(PROBE_MARKER) and '"table":"grammar"' in line
    ]
    assert sorted(built) == ["Skel.CacheA", "Skel.CacheB"]
    trusted = {item["name"]: item for root in roots for item in records[root]["trusted"]}
    assert set(trusted) == {"Skel.CacheA.a1", "Skel.CacheA.a2", "Skel.CacheB.b1"}
    for item in trusted.values():
        assert item["source"] is not None and item["source_comments"], item["name"]

@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_long_proof_does_not_make_the_statement_check_quadratic(tmp_path: Path) -> None:
    # The statement is parsed from the source and from each prefix ending
    # before a `:=`. Checking each of ManyTok's 500 scoped tokens against
    # every such prefix of a 600-`:=` proof outlasts the timeout; checking it
    # against the source and the seams with `:= sorry` does not.
    project = _project(tmp_path)
    (project / "Skel" / "ManyTok.lean").write_text(
        "import Lean\n"
        "namespace Many\n"
        "run_cmd for i in [0:500] do\n"
        '  Lean.Parser.parserExtension.add (.token s!"@@{i}@@") (kind := .scoped)\n'
        "end Many\n",
        encoding="utf-8",
    )
    (project / "Skel" / "LongProof.lean").write_text(
        "import Skel.ManyTok\n"
        "theorem Skel.longRoot (n : Nat) : n = n := by\n"
        + "  have h : n + 1 = n + 1 := rfl\n" * 600
        + "  rfl\n",
        encoding="utf-8",
    )
    _build(project, "Skel.LongProof")
    roots = ("Skel.longRoot",)
    probe = render_probe(imports=("Skel.LongProof",), roots=roots, project_roots=("Skel",))

    output = run_probe(probe, project, timeout=600)

    records = parse_probe_output(output, expected_roots=roots)
    assert records["Skel.longRoot"]["statement_source"] == "theorem Skel.longRoot (n : Nat) : n = n"


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_tokens_only_the_probes_own_imports_declare_do_not_withhold_a_source(tmp_path: Path) -> None:
    # `throwError` and `trace[` are tokens of the `Lean` modules the probe
    # helper imports, not of HelperTokens, which imports nothing. Lean read
    # them there as identifiers, and so must the probe.
    project = _project(tmp_path)
    (project / "Skel" / "HelperTokens.lean").write_text(
        "namespace Skel.HelperTokens\n"
        "def throwError (n : Nat) : Nat := -- note throwError\n"
        "  n\n"
        "def trace : Array Nat := #[1, 2]\n"
        "def usesBoth : Nat := -- note usesBoth\n"
        "  throwError trace[0]!\n"
        "theorem helperRoot (h : usesBoth = throwError trace[1]!) : True := -- note proof\n"
        "  trivial\n"
        "end Skel.HelperTokens\n",
        encoding="utf-8",
    )
    _build(project, "Skel.HelperTokens")
    blueprint = _blueprint(tmp_path, lean={"helper": "Skel.HelperTokens.helperRoot"})

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    node = report.node("basics/helper")
    assert node is not None
    declaration = node.declarations[0]
    assert declaration.statement == "theorem helperRoot (h : usesBoth = throwError trace[1]!) : True"
    trusted = {item.name.rsplit(".", 1)[1]: item for item in declaration.trusted}
    for name in ["throwError", "usesBoth"]:
        item = trusted[name]
        assert item.source is not None and not item.source_withheld, name
        assert item.source_comments == (_comment_range(item.source, "--", None),), name
    assert "note" not in node.blind_text()


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_sources_whose_tokens_are_provable_are_shown_without_comments(tmp_path: Path) -> None:
    # `+--` is a token wherever `Skel.CommentToken` is imported. PktPrecDep does
    # not import it, so there its text is a comment even under a root that
    # does; PktPrec does, so there it is code. Notation the file declares
    # above its use, and scoped notation in its namespace, lex the same with
    # or without their tokens as far as comments go.
    project = _project(tmp_path)
    (project / "Skel" / "PktPrecDep.lean").write_text(
        "namespace Skel.PktPrecDep\n"
        "/-- KEEPOUT_DOC: reads like `a +-- b`, notation this file does not import. -/\n"
        "def depDoc (a : Nat) : Nat := a +-- KEEPOUT_DEP\n"
        "  0\n"
        "end Skel.PktPrecDep\n",
        encoding="utf-8",
    )
    (project / "Skel" / "PktPrec.lean").write_text(
        "import Skel.CommentToken\n"
        "import Skel.PktPrecDep\n"
        "namespace Skel.PktPrec\n"
        'infixl:65 " ⊕⊕ " => Nat.add\n'
        "/-- KEEPOUT_SAME -/\n"
        "def sameModule (a b : Nat) : Nat := a ⊕⊕ b -- KEEPOUT_SAME_LINE\n"
        "namespace A\n"
        'scoped infixl:70 " ⊗⊗ " => Nat.mul\n'
        "def scopedUse (a b : Nat) : Nat := a ⊗⊗ b /- KEEPOUT_SCOPED -/\n"
        "end A\n"
        "def imported (a b : Nat) : Nat := a +-- b\n"
        "/-- KEEPOUT_FOO -/\n"
        "theorem A.foo (h : Skel.PktPrecDep.depDoc 1 = 1) : A.scopedUse 1 2 = 2 := rfl\n"
        "theorem precRoot (h : imported 2 3 = 6) : sameModule 1 2 = 3 := -- KEEPOUT_PROOF\n"
        "  rfl\n"
        "end Skel.PktPrec\n",
        encoding="utf-8",
    )
    _build(project, "Skel.PktPrec")
    blueprint = _blueprint(tmp_path, lean={"foo": "Skel.PktPrec.A.foo", "prec": "Skel.PktPrec.precRoot"})

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    declarations = {d.name: d for node in report.nodes for d in node.declarations}
    foo = declarations["Skel.PktPrec.A.foo"]
    prec = declarations["Skel.PktPrec.precRoot"]
    assert foo.statement == "/-- KEEPOUT_FOO -/\ntheorem A.foo (h : Skel.PktPrecDep.depDoc 1 = 1) : A.scopedUse 1 2 = 2"
    assert prec.statement == "theorem precRoot (h : imported 2 3 = 6) : sameModule 1 2 = 3"
    trusted = {item.name: item for d in (foo, prec) for item in d.trusted}
    for name in ["Skel.PktPrecDep.depDoc", "Skel.PktPrec.sameModule", "Skel.PktPrec.A.scopedUse",
                 "Skel.PktPrec.imported"]:
        assert trusted[name].source is not None and not trusted[name].source_withheld, name
    assert trusted["Skel.PktPrec.imported"].source_comments == ()
    assert "def imported (a b : Nat) : Nat := a +-- b" in prec.blind_text()
    assert "KEEPOUT" not in foo.blind_text() + prec.blind_text()


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_module_lexes_with_the_tokens_of_the_imports_lean_loads_for_it(tmp_path: Path) -> None:
    # Under the module system Lean does not load what an import imports
    # privately, so `+--` is not a token in ModUse, and the build proves it:
    # read as code, `KEEPOUT_M` would be an unknown identifier. The probe,
    # which imports everything, must not take the token as present there.
    project = _project(tmp_path)
    (project / "Skel" / "ModTok.lean").write_text(
        'module\n\nnamespace Skel.ModTok\ninfixl:65 " +-- " => Nat.sub\nend Skel.ModTok\n', encoding="utf-8"
    )
    (project / "Skel" / "ModMid.lean").write_text(
        "module\n\nimport Skel.ModTok\n\npublic section\nnamespace Skel.ModMid\ndef mid : Nat := 1\nend Skel.ModMid\n",
        encoding="utf-8",
    )
    (project / "Skel" / "ModUse.lean").write_text(
        "module\n\npublic import Skel.ModMid\n\npublic section\nnamespace Skel.ModUse\n"
        "def hidden (a b : Nat) : Nat := a +-- KEEPOUT_M\n  b\n"
        "theorem modRoot (h : hidden 1 2 = 1) : True := trivial\n"
        "end Skel.ModUse\n",
        encoding="utf-8",
    )
    _build(project, "Skel.ModUse")

    report = extract_skeletons(_blueprint(tmp_path, lean={"mod": "Skel.ModUse.modRoot"}), lean_root=project)

    assert report.clean
    (root,) = [d for node in report.nodes for d in node.declarations]
    (hidden,) = root.trusted
    assert hidden.source is not None and not hidden.source_withheld
    assert hidden.source_comments == (_comment_range(hidden.source, "--"),)
    assert "KEEPOUT" not in root.blind_text()


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_signatures_escape_only_the_tokens_of_their_module(tmp_path: Path) -> None:
    # `throwError` is a keyword once `Lean.Exception` is imported, as it is in
    # the probe's helper, but not in a file whose only import is Skel.EscUse,
    # where `#check` prints these binders bare.
    project = _project(tmp_path)
    (project / "Skel" / "EscUse.lean").write_text(
        "namespace Skel.EscUse\n"
        "def plain (throwError : Nat) : Nat := throwError\n"
        "theorem useRoot (throwError : Nat) : plain throwError = throwError := rfl\n"
        "end Skel.EscUse\n",
        encoding="utf-8",
    )
    _build(project, "Skel.EscUse")

    report = extract_skeletons(_blueprint(tmp_path, lean={"use": "Skel.EscUse.useRoot"}), lean_root=project)

    assert report.clean
    (use,) = [d for node in report.nodes for d in node.declarations]
    (plain,) = use.trusted
    for text in (use.signature, use.raw_signature, plain.signature):
        assert "throwError" in text and "«" not in text, text


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_signatures_print_at_the_format_width_check_uses(tmp_path: Path) -> None:
    # 115 characters: `#check` keeps it on one line at the default
    # `format.width` of 120.
    project = _built_module(
        tmp_path,
        "PktWide",
        "namespace Skel.PktWide\n"
        "theorem wide (alpha beta gamma delta : Nat) (h : alpha + beta = gamma + delta) :\n"
        "    gamma + delta = gamma + delta := rfl\n"
        "end Skel.PktWide\n",
    )
    blueprint = _blueprint(tmp_path, lean={"wide": "Skel.PktWide.wide"})

    report = extract_skeletons(blueprint, lean_root=project)

    (declaration,) = report.nodes[0].declarations
    assert declaration.signature == (
        "Skel.PktWide.wide (alpha beta gamma delta : Nat) (h : alpha + beta = gamma + delta) : gamma + delta = gamma + delta"
    )
    assert max(len(line) for line in declaration.raw_signature.splitlines()) > 100


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_project_constant_in_the_helper_namespace_is_named_as_the_cause(tmp_path: Path) -> None:
    project = _built_module(
        tmp_path,
        "PktColl",
        "namespace AutoformSkeleton\n"
        "def main : Nat := 1\n"
        "end AutoformSkeleton\n"
        "namespace Skel.PktColl\n"
        "theorem coll_root : True := trivial\n"
        "end Skel.PktColl\n",
    )
    blueprint = _blueprint(tmp_path, lean={"coll": "Skel.PktColl.coll_root"})

    report = extract_skeletons(blueprint, lean_root=project)

    (issue,) = report.unresolved
    assert issue.reason.startswith(
        "probe of module Skel.PktColl failed: module Skel.PktColl declares AutoformSkeleton.main, "
        "but the AutoformSkeleton namespace is reserved by the skeleton probe's helper"
    )
    assert "lake build" not in issue.reason


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_node_selection_does_not_change_a_declarations_evidence(tmp_path: Path) -> None:
    # Only the other article's module declares the notation. Each declaration
    # is printed where its own module is imported, so neither selecting nor
    # adding that article changes the packet `review record` re-extracts.
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
    alone = extract_skeletons(
        _blueprint(tmp_path / "alone", lean={"two": "Skel.PktWrap.wrap_two"}), lean_root=project
    )

    assert full.clean and scoped.clean and alone.clean
    (declaration,) = full.declarations("basics/two")
    assert declaration.signature == "Skel.PktWrap.wrap_two : Skel.PktWrap.wrap 2 = 2"
    assert scoped.node("basics/two") == full.node("basics/two")
    assert alone.node("basics/two").review_hash == full.node("basics/two").review_hash
    (three,) = full.declarations("basics/three")
    assert three.signature == "Skel.PktWrap.wrap_three : ⟪3⟫ = 3"


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_an_unrelated_modules_attribute_does_not_change_a_trusted_declaration(tmp_path: Path) -> None:
    # `attribute [instance]` in another article's module would make `natInh` an
    # instance in a probe that imported both modules.
    project = _project(tmp_path)
    (project / "Skel" / "InstDef.lean").write_text(
        "namespace Skel.InstDef\n"
        "def natInh : Inhabited Nat := ⟨0⟩\n"
        "theorem natInh_default : natInh.default = 0 := rfl\n"
        "end Skel.InstDef\n",
        encoding="utf-8",
    )
    (project / "Skel" / "InstAttr.lean").write_text(
        "import Skel.InstDef\n"
        "attribute [instance] Skel.InstDef.natInh\n"
        "namespace Skel.InstAttr\n"
        "theorem other : True := trivial\n"
        "end Skel.InstAttr\n",
        encoding="utf-8",
    )
    _build(project, "Skel.InstDef", "Skel.InstAttr")

    alone = extract_skeletons(
        _blueprint(tmp_path / "alone", lean={"inst": "Skel.InstDef.natInh_default"}), lean_root=project
    )
    beside = extract_skeletons(
        _blueprint(tmp_path / "beside", lean={"inst": "Skel.InstDef.natInh_default", "other": "Skel.InstAttr.other"}),
        lean_root=project,
    )

    assert alone.clean and beside.clean
    (before,) = alone.declarations("basics/inst")
    (after,) = beside.declarations("basics/inst")
    (trusted,) = after.trusted
    assert trusted.name == "Skel.InstDef.natInh" and trusted.kind == "def"
    assert after.trusted == before.trusted
    assert after.hash == before.hash
    assert beside.node("basics/inst").review_hash == alone.node("basics/inst").review_hash


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_a_trusted_declaration_reads_as_each_root_module_prints_it(tmp_path: Path) -> None:
    project = _project(tmp_path)
    modules = {
        "PktWrap": "namespace Skel.PktWrap\ndef wrap (n : Nat) : Nat := n\nend Skel.PktWrap\n",
        "PktWrapNotation": 'import Skel.PktWrap\nnamespace Skel.PktWrap\nnotation "⟪" x "⟫" => wrap x\nend Skel.PktWrap\n',
        "PktWrapUser": "import Skel.PktWrap\nnamespace Skel.PktWrap\n"
        "def needsTwo (h : wrap 2 = 2) : Nat := 0\n"
        "theorem uses_needsTwo : needsTwo rfl = 0 := rfl\nend Skel.PktWrap\n",
        "PktWrapUser2": "import Skel.PktWrapUser\nimport Skel.PktWrapNotation\nnamespace Skel.PktWrap\n"
        "theorem uses_needsTwo' : needsTwo rfl = 0 := rfl\nend Skel.PktWrap\n",
    }
    for module, source in modules.items():
        (project / "Skel" / f"{module}.lean").write_text(source, encoding="utf-8")
    _build(project, "Skel.PktWrapUser", "Skel.PktWrapUser2")
    blueprint = _blueprint(
        tmp_path, lean={"u1": "Skel.PktWrap.uses_needsTwo", "u2": "Skel.PktWrap.uses_needsTwo'"}
    )

    report = extract_skeletons(blueprint, lean_root=project)

    assert report.clean
    (u1,) = report.declarations("basics/u1")
    (u2,) = report.declarations("basics/u2")
    (plain,) = (item for item in u1.trusted if item.name == "Skel.PktWrap.needsTwo")
    (notated,) = (item for item in u2.trusted if item.name == "Skel.PktWrap.needsTwo")
    assert "Skel.PktWrap.wrap 2 = 2" in plain.signature and "⟪2⟫ = 2" in notated.signature
    assert plain.semantic == notated.semantic and plain.raw_signature == notated.raw_signature
    data = report.as_dict()
    assert {module: sorted(entries) for module, entries in data["trusted"].items()} == {
        "Skel.PktWrapUser": ["Skel.PktWrap.needsTwo", "Skel.PktWrap.wrap"],
        "Skel.PktWrapUser2": ["Skel.PktWrap.needsTwo", "Skel.PktWrap.wrap"],
    }
    path = tmp_path / "skeleton.json"
    write_skeleton_report(report, path)
    loaded = load_skeleton_report(path)
    assert loaded == report
    (reloaded,) = loaded.declarations("basics/u2")
    assert notated in reloaded.trusted


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_two_root_modules_may_see_different_constants_under_one_name(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    lakefile = project / "lakefile.toml"
    lakefile.write_text(
        lakefile.read_text(encoding="utf-8") + '\n[[require]]\nname = "dep"\npath = "dep"\n', encoding="utf-8"
    )
    dep = project / "dep"
    dep.mkdir()
    shutil.copy(_FIXTURE / "lean-toolchain", dep / "lean-toolchain")
    (dep / "lakefile.toml").write_text(
        'name = "dep"\n\n[[lean_lib]]\nname = "Dep"\nroots = ["DepA", "DepB"]\n', encoding="utf-8"
    )
    (dep / "DepA.lean").write_text("namespace Ext\ndef collision : Nat := 1\nend Ext\n", encoding="utf-8")
    (dep / "DepB.lean").write_text("namespace Ext\ndef collision : Int := 2\nend Ext\n", encoding="utf-8")
    # Neither root module imports the other, so each may declare its own Shared.ax.
    for module, dependency, value in (("UA", "DepA", 1), ("UB", "DepB", 2)):
        (project / "Skel" / f"{module}.lean").write_text(
            f"import {dependency}\nnamespace Shared\naxiom ax : {value} = {value}\nend Shared\n"
            f"namespace Skel.{module}\n"
            f"theorem {module.lower()} : Ext.collision = Ext.collision := (fun _ => rfl) Shared.ax\n"
            f"end Skel.{module}\n",
            encoding="utf-8",
        )
    _build(project, "Skel.UA", "Skel.UB")
    blueprint = _blueprint(tmp_path, lean={"ua": "Skel.UA.ua", "ub": "Skel.UB.ub"})
    output = tmp_path / "skeleton.json"

    assert main(["skeleton", str(blueprint), "--lean-root", str(project), "--output", str(output)]) == 0

    assert "error:" not in capsys.readouterr().err
    data = json.loads(output.read_text(encoding="utf-8"))
    for name in ("Ext.collision", "Shared.ax"):
        assert data["semantics"]["Skel.UA"][name] != data["semantics"]["Skel.UB"][name]
    loaded = load_skeleton_report(output)
    (ua,) = loaded.declarations("basics/ua")
    (ub,) = loaded.declarations("basics/ub")
    assert ua.assumed == ub.assumed == ("Ext.collision",)
    assert dict(ua.assumed_semantics)["Ext.collision"] != dict(ub.assumed_semantics)["Ext.collision"]
    assert dict(ua.axiom_semantics)["Shared.ax"] != dict(ub.axiom_semantics)["Shared.ax"]


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
    _build(project, "Skel.Semantics")

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

    _replace_source(project / "Skel" / "Semantics.lean", "if n == 0 then 1 else", "if n == 0 then 2 else")
    _build(project, "Skel.Semantics")

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
    assert build.returncode == 0, build.stdout + build.stderr


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

    # `+--` comes from a module the root's own module does not import, so an
    # article that uses it leaves this packet unchanged.
    roots["token"] = "Skel.usesCommentToken"
    (project / "Skel" / "BlindTokenUse.lean").write_text(
        "import Skel.Unparsed\nimport Skel.CommentToken\n\n"
        "theorem Skel.usesBlindTokenWithToken : Skel.Unparsed.blindToken 2 3 = 2 := rfl\n",
        encoding="utf-8",
    )
    _build(project, "Skel.BlindTokenUse")
    roots["tokenblind"] = "Skel.usesBlindTokenWithToken"
    with_token = extract(tmp_path / "with-token")
    assert with_token["Skel.Unparsed.usesBlindToken"] == blind
    # Under a root whose module does import `+--`, blindToken's own file still
    # does not, so its source reads as that file's lexer read it.
    reached = with_token["Skel.usesBlindTokenWithToken"]
    assert trusted(reached) == trusted(blind)
    assert "+--" not in reached.blind_text()
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
    _build(project, "Skel.Semantics")

    roots = (
        "Skel.Semantics.usesExternalDetail",
        "Skel.Semantics.usesExternalMatch",
        "Skel.Semantics.usesExternalPrivate",
    )

    before = _probe_records(project, roots)
    detail = before["Skel.Semantics.usesExternalDetail"]
    assert detail["assumed"] == ["Vendor.visible._helper"]
    assert [item[0] for item in detail["boundary_modules"]] == ["Skel.Vendor"]
    detail_hash = _declaration_of(project, detail).hash

    matched = before["Skel.Semantics.usesExternalMatch"]
    assert matched["assumed"] == ["Vendor.matchBody"]
    match_semantic = json.loads(dict(matched["assumed_semantics"])["Vendor.matchBody"])
    assert len(match_semantic["generated"]) == 1

    private = before["Skel.Semantics.usesExternalPrivate"]
    assert private["assumed"] == ["Vendor.usesPrivate"]
    assert all("privateHelper" not in name for name in private["assumed"])

    _replace_source(
        project / "Skel" / "Vendor.lean", "def visible._helper : Nat := 1", "def visible._helper : Nat := 2"
    )
    _build(project, "Skel.Semantics")

    changed_detail = _probe_records(project, roots)["Skel.Semantics.usesExternalDetail"]
    assert changed_detail["semantic"] == detail["semantic"]
    assert changed_detail["assumed_semantics"] != detail["assumed_semantics"]
    assert _declaration_of(project, changed_detail).hash != detail_hash


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_shared_probe_tables_keep_every_hash(tmp_path: Path) -> None:
    # The probe states each trusted declaration, semantic material, and module
    # once per run. Drift hashes must survive sharing; the full golden also
    # records intentional changes to the packet evidence presented to a reader.
    project = _project(tmp_path)
    _build(project)
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
            declaration = _declaration_of(project, records[root])
            declarations.append(declaration)
            hashes[f"{project_root}:{root}"] = [declaration.hash, declaration.evidence_hash]
        node = NodeSkeleton(node_id=project_root, article_path="a.md", declarations=tuple(declarations))
        hashes[project_root] = [node.hash, node.evidence_hash, node.review_hash]
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    assert digest == "9dad08e83318314c6f864446bd56d216063d49ec303e2eda20c5a5867ffcd20a", f"{digest}\n{json.dumps(hashes, indent=1)}"


def _probe_records(
    project: Path, roots: tuple[str, ...], module: str = "Skel.Semantics", project_root: str | None = None
) -> dict[str, dict[str, object]]:
    probe = render_probe(imports=(module,), roots=roots, project_roots=(project_root or module,))
    return parse_probe_output(run_probe(probe, project))


def _declaration_of(project: Path, record: dict[str, object]):
    return _declaration(
        record,
        libraries=lean_libraries(project),
        lean_root=project,
        index=index_project(project),
        module_hashes={},
        snapshot_started_ns=None,
    )


def _probe_declaration(project: Path, root: str, *, module: str = "Skel.Semantics"):
    record = _probe_records(project, (root,), module)[root]
    return record, _declaration_of(project, record)


def _replace_source(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
@pytest.mark.parametrize(
    ("root", "module", "old", "new", "boundary"),
    [
        # A macro leaves no constant behind, so the module that defines it is not
        # bound; only the compiled module that expanded it witnesses the change.
        ("usesVendorMacro", "VendorMacro", "((1 : Nat))", "((2 : Nat))", ["Skel.VendorMacroUse"]),
        # `wfWalk` reaches `wfHelper` only through its generated `_unary` body.
        ("usesVendorWf", "VendorWfHelper", "n + 1", "n + 2", ["Skel.VendorWf", "Skel.VendorWfHelper"]),
        ("usesVendorPrivateChain", "VendorPrivB", ":= 1", ":= 2", ["Skel.VendorPrivA", "Skel.VendorPrivB"]),
        # Swapping fields keeps the constructor type and the projection name; only
        # the projection body says which field `first` selects.
        ("usesFieldOrder", "Semantics", "  first : Nat\n  second : Nat\n", "  second : Nat\n  first : Nat\n", []),
    ],
    ids=["vendor-macro", "external-wf-helper", "external-private-chain", "structure-field-order"],
)
def test_hidden_change_rotates_the_declaration_hash(
    tmp_path: Path, root: str, module: str, old: str, new: str, boundary: list[str]
) -> None:
    project = _project(tmp_path)
    _build(project, "Skel.Semantics")
    record, before = _probe_declaration(project, f"Skel.Semantics.{root}")
    assert [item[0] for item in record["boundary_modules"]] == boundary

    _replace_source(project / "Skel" / f"{module}.lean", old, new)
    _build(project, "Skel.Semantics")
    changed_record, changed = _probe_declaration(project, f"Skel.Semantics.{root}")

    assert changed_record["assumed_semantics"] == record["assumed_semantics"]
    assert changed.hash != before.hash


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_external_private_axiom_binds_its_module(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build(project, "Skel.Semantics")
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


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_dependency_module_under_a_core_name_is_bound(tmp_path: Path) -> None:
    project = _project_with_core_named_dependency(tmp_path, "Lake.Vendor")
    _build(project, "Skel.UsesDep")
    root = "Skel.UsesDep.root"
    record, before = _probe_declaration(project, root, module="Skel.UsesDep")

    # Only the toolchain's own modules are core; a package may reuse the name.
    assert record["assumed"] == ["Lake.Vendor.magic"]
    assert [item[:2] for item in record["boundary_modules"]] == [["Lake.Vendor", "olean"]]

    _replace_source(project / "dep" / "Lake" / "Vendor.lean", ":= 1", ":= 2")
    _build(project, "Skel.UsesDep")
    _, changed = _probe_declaration(project, root, module="Skel.UsesDep")

    assert changed.hash != before.hash


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_dependency_shadowing_a_toolchain_library_fails_closed(tmp_path: Path) -> None:
    project = _project_with_core_named_dependency(tmp_path, "Std.Vendor")
    _build(project, "Skel.UsesDep")

    with pytest.raises(SkeletonError, match="cannot load toolchain module Std\\..*hides the toolchain's own `Std`"):
        _probe_declaration(project, "Skel.UsesDep.root", module="Skel.UsesDep")


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_boundary_module_identity_is_checkout_path_independent(tmp_path: Path) -> None:
    roots = ("Skel.Semantics.usesVendorMacro", "Skel.Semantics.usesVendorModule")
    (tmp_path / "first").mkdir()
    (tmp_path / "second" / "nested").mkdir(parents=True)
    identities = []
    for project in (_project(tmp_path / "first"), _project(tmp_path / "second" / "nested")):
        _build(project, "Skel.Semantics")
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
    _build(project)

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

    # A dotted Lake root must include this module, but not Skel.Vendor.
    before = _probe_records(project, roots)
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
    selection = "Skel.Semantics.selectedProposition"
    local_selected = _probe_records(project, (selection,), project_root="Skel")[selection]
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
    _replace_source(source, "| `(semanticMacro) => `(1)", "| `(semanticMacro) => `(2)")
    _replace_source(source, "  | 0 => 10\n  | n + 1 => n", "  | 1 => 10\n  | n => n")
    _replace_source(source, "def visible._helper : Nat := 1", "def visible._helper : Nat := 2")
    _replace_source(
        source,
        "universe u\n\ndef universeNamed (α : Type u) : Type u := α",
        "universe v\n\ndef universeNamed (α : Type v) : Type v := α",
    )
    _build(project, "Skel.Semantics")
    changed = _probe_records(project, roots)
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

    _replace_source(project / "Skel" / "Vendor.lean", "⟨True⟩", "⟨False⟩")
    _build(project, "Skel.Semantics")
    changed_instance = _probe_records(project, roots)["Skel.Semantics.selectedProposition"]
    assert changed_instance["semantic"] == selected["semantic"]
    assert changed_instance["assumed"] == selected["assumed"]
    assert changed_instance["assumed_semantics"] == selected["assumed_semantics"]
    assert _hash_module_files(
        changed_instance["boundary_modules"], lean_root=project, cache={}
    ) != before_modules
    changed_local = _probe_records(project, (selection,), project_root="Skel")[selection]
    changed_local_semantics = {
        item["name"]: item["semantic"] for item in changed_local["trusted"]
    }
    assert changed_local_semantics["Vendor.selectedChoice"] != local_semantics[
        "Vendor.selectedChoice"
    ]


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_axiom_types_are_part_of_the_trust_boundary(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build(project)

    result = "Skel.AxiomUse.result"
    external = _probe_records(project, (result,), "Skel.AxiomUse")[result]
    assert external["assumed"] == ["AxiomVendor.P"]
    assert [item[0] for item in external["boundary_modules"]] == ["Skel.AxiomVendor"]
    external_modules = _hash_module_files(external["boundary_modules"], lean_root=project, cache={})

    local = _probe_records(project, (result,), "Skel.AxiomUse", "Skel")[result]
    assert [item["name"] for item in local["trusted"]] == ["AxiomVendor.P"]
    proposition = local["trusted"][0]

    _replace_source(project / "Skel" / "AxiomVendor.lean", "def P : Prop := True", "def P : Prop := False")
    _build(project, "Skel.AxiomUse")

    changed_external = _probe_records(project, (result,), "Skel.AxiomUse")[result]
    assert _hash_module_files(
        changed_external["boundary_modules"], lean_root=project, cache={}
    ) != external_modules
    changed_local = _probe_records(project, (result,), "Skel.AxiomUse", "Skel")[result]
    changed_proposition = changed_local["trusted"][0]
    assert changed_proposition["name"] == proposition["name"]
    assert changed_proposition["semantic"] != proposition["semantic"]


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_statement_parsing_does_not_leak_scoped_notation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _build(project)
    probe = render_probe(
        imports=("Skel.ScopedA",),
        roots=("Skel.ScopedA.activatesScope",),
        project_roots=("Skel",),
    )
    probe += """
open Lean Elab Command
run_cmd do
  let grammars ← IO.mkRef ({} : AutoformSkeleton.GrammarCache)
  -- The probe above already wrote this module's grammar record; a second
  -- would be a duplicate, so this call's records are discarded.
  let semantic ← IO.mkRef ({ output := ← IO.FS.Handle.mk "/dev/null" .write } : AutoformSkeleton.SemanticCache)
  let _ ← AutoformSkeleton.statementSource grammars semantic `Skel.ScopedA.activatesScope
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
    _replace_source(
        article,
        "## Depends on",
        "## Sources\n\n- [notes](../../sources/notes.md)\n"
        "- [external](https://example.com/paper.tex#L1-L2)\n"
        "- [Theorem 2](../../sources/book.tex#L5-L7)\n\n## Depends on",
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
    (tmp_path / "r.json").write_text(report.to_json(), encoding="utf-8")
    assert load_skeleton_report(tmp_path / "r.json") == report


def test_packet_publication_refuses_an_incomplete_report(tmp_path: Path) -> None:
    report = _fake_report(tmp_path)
    incomplete = replace(
        report,
        nodes=(replace(report.nodes[0], declarations=(), complete=False),),
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
    _fake_default_probe(monkeypatch, lambda probe, root: _fake_probe_output())
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


@pytest.mark.parametrize(
    ("packet_schema", "passage_schema"),
    [
        (PACKET_SCHEMA, PASSAGE_SCHEMA),
        ("autoform-skeleton-packets/v1", "autoform-skeleton-passages/v1"),
    ],
)
def test_packet_publication_replaces_stale_managed_output(
    tmp_path: Path, packet_schema: str, passage_schema: str
) -> None:
    report = _fake_report(tmp_path)
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"

    write_packets(report, packets, passages=passages)
    stale_packet = packets / "stale.lean"
    stale_passage = passages / "stale.txt"
    stale_packet.write_text("stale\n", encoding="utf-8")
    stale_passage.write_text("stale\n", encoding="utf-8")
    for root, schema in ((packets, packet_schema), (passages, passage_schema)):
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
    report = _fake_report(tmp_path)
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
    report = _fake_report(tmp_path)
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


def _published(tmp_path: Path, *, passages: bool = True) -> tuple[SkeletonReport, Path, Path]:
    """Publish the fake report once, then mark each published tree as the old output."""

    report = _fake_report(tmp_path)
    packets, passage_dir = tmp_path / "packets", tmp_path / "passages"
    write_packets(report, packets, passages=passage_dir if passages else None)
    (packets / "old-marker").write_text("old packets\n", encoding="utf-8")
    if passages:
        (passage_dir / "old-marker").write_text("old passages\n", encoding="utf-8")
    return report, packets, passage_dir


def test_packet_publication_rolls_back_both_trees_on_commit_failure(
    tmp_path: Path, monkeypatch
) -> None:
    report, packets, passages = _published(tmp_path)

    def fail_passage_install(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        if "autoform-stage" in source_path.name and Path(destination) == passages:
            raise OSError("simulated passage commit failure")
        _install_output(source_path, Path(destination))

    monkeypatch.setattr("autoform_cli.skeleton._install_output", fail_passage_install)

    with pytest.raises(SkeletonError, match="could not publish skeleton output"):
        write_packets(report, packets, passages=passages)

    assert (packets / "old-marker").read_text(encoding="utf-8") == "old packets\n"
    assert (passages / "old-marker").read_text(encoding="utf-8") == "old passages\n"


def test_packet_publication_rolls_back_both_trees_when_interrupted(
    tmp_path: Path, monkeypatch
) -> None:
    report, packets, passages = _published(tmp_path)

    def interrupt_passage_install(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        _install_output(source_path, Path(destination))
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
    report, packets, _ = _published(tmp_path, passages=False)
    marker = packets / "old-marker"
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
    report, packets, _ = _published(tmp_path, passages=False)
    changed_backup: Path | None = None

    def change_backup_after_install(source: str | Path, destination: str | Path) -> None:
        nonlocal changed_backup
        _install_output(Path(source), Path(destination))
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
    report, packets, passages = _published(tmp_path)
    replace = os.replace

    def replace_then_fail(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if "autoform-stage" in source_path.name and destination_path == passages:
            displaced = tmp_path / "displaced-packets"
            replace(packets, displaced)
            packets.mkdir()
            (packets / "valuable").write_text("concurrent publisher\n", encoding="utf-8")
            raise OSError("simulated passage commit failure")
        _install_output(source_path, destination_path)

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
    report, packets, _ = _published(tmp_path, passages=False)
    concurrent_inode: int | None = None

    def create_before_install(stage: Path, destination: Path) -> None:
        nonlocal concurrent_inode
        if destination == packets:
            destination.mkdir()
            concurrent_inode = destination.stat().st_ino
        _install_output(stage, destination)

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
    report, packets, _ = _published(tmp_path, passages=False)
    marker = packets / "old-marker"

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
    report, packets, _ = _published(tmp_path, passages=False)
    marker = packets / "old-marker"

    def interrupt_after_preflight_install(stage: Path, destination: Path) -> None:
        _install_output(stage, destination)
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
    report, packets, passages = _published(tmp_path)
    remove_output = _remove_output

    def fail_passage_install(stage: Path, destination: Path) -> None:
        if "autoform-stage" in stage.name and destination == passages:
            raise OSError("simulated passage install failure")
        _install_output(stage, destination)

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
    report, packets, passages = _published(tmp_path)
    rename_no_replace = _rename_no_replace
    concurrent_inode: int | None = None

    def fail_passage_install(stage: Path, destination: Path) -> None:
        if "autoform-stage" in stage.name and destination == passages:
            raise OSError("simulated passage install failure")
        _install_output(stage, destination)

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


def test_packet_rollback_restores_backups_when_a_destination_becomes_a_symlink(
    tmp_path: Path, monkeypatch
) -> None:
    report, packets, passages = _published(tmp_path)

    def swap_then_fail(stage: Path, destination: Path) -> None:
        if "autoform-stage" in stage.name and destination == passages:
            os.replace(packets, tmp_path / "elsewhere")
            packets.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
            raise OSError("simulated passage install failure")
        _install_output(stage, destination)

    monkeypatch.setattr("autoform_cli.skeleton._install_output", swap_then_fail)

    with pytest.raises(SkeletonError, match="simulated passage install failure") as info:
        write_packets(report, packets, passages=passages)

    assert (passages / "old-marker").read_text(encoding="utf-8") == "old passages\n"
    assert list(tmp_path.glob(".passages.autoform-backup-*")) == []
    backups = list(tmp_path.glob(".packets.autoform-backup-*"))
    assert len(backups) == 1
    assert any(f"previous output remains at {backups[0]}" in issue for issue in info.value.issues)


def test_packet_publication_uses_normal_modes_and_preserves_existing_mode(tmp_path: Path) -> None:
    report = _fake_report(tmp_path)
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
    report = _fake_report(tmp_path)
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    stage_output = _stage_output
    calls = 0

    def fail_second_stage(destination: Path, stages: list[Path]) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated passage staging failure")
        return stage_output(destination, stages)

    monkeypatch.setattr("autoform_cli.skeleton._stage_output", fail_second_stage)

    with pytest.raises(SkeletonError, match="could not prepare skeleton output"):
        write_packets(report, packets, passages=passages)

    assert list(tmp_path.glob(".packets.autoform-stage-*")) == []
    assert not packets.exists() and not passages.exists()


@pytest.mark.parametrize("step", ["create", "mode"])
def test_packet_publication_removes_stages_interrupted_while_they_are_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"determined": "Skel.observation_determined"})
    report = extract_skeletons(blueprint, lean_root=project, runner=lambda p, r: _fake_probe_output())
    packets = tmp_path / "packets"
    passages = tmp_path / "passages"
    write_packets(report, packets, passages=passages)
    mkdir = Path.mkdir
    chmod = Path.chmod

    def interrupted_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        mkdir(self, *args, **kwargs)
        if self.name.startswith(".packets.autoform-stage-"):
            raise KeyboardInterrupt

    def interrupted_chmod(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.startswith(".passages.autoform-stage-"):
            raise KeyboardInterrupt
        chmod(self, *args, **kwargs)

    if step == "create":
        monkeypatch.setattr(Path, "mkdir", interrupted_mkdir)
    else:
        monkeypatch.setattr(Path, "chmod", interrupted_chmod)

    with pytest.raises(KeyboardInterrupt):
        write_packets(report, packets, passages=passages)

    assert list(tmp_path.glob(".*.autoform-stage-*")) == []
    assert (packets / PACKET_MANIFEST).is_file() and (passages / PACKET_MANIFEST).is_file()


def test_packet_publication_detects_a_concurrent_file_edit(tmp_path: Path, monkeypatch) -> None:
    report = _fake_report(tmp_path)
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
    report = _fake_report(tmp_path)
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
