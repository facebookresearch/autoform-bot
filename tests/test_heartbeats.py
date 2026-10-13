from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from autoform_cli import heartbeats
from autoform_cli.__main__ import main
from autoform_cli.heartbeats import (
    PROBE_MARKER,
    format_heartbeats,
    measure_heartbeats,
    parse_probe_output,
    render_probe,
)
from tests.test_skeleton import _blueprint, _build, _lean_toolchain_available, _project

_SOURCE = """\
/-
Copyright header.
-/
import Skel.Main

set_option maxHeartbeats 800000

namespace Skel.Limits

/-- A docstring

with a blank line. -/
@[simp]
-- why the limit is raised
set_option maxHeartbeats 1600000 in
open Nat in
theorem scoped : True := trivial

theorem fileWide : True := trivial

set_option maxHeartbeats 0 in
theorem unbounded : True := trivial

set_option maxHeartbeats 200000 in
theorem atDefault : True := by
  trivial

theorem broken : True := by
  exact missing

end Skel.Limits
"""


def _counting(used: dict[str, int], *, errors: dict[str, str] | None = None):
    """A runner that answers like Lean: one count per wrapped target, errors at given lines."""

    calls: list[tuple[str, str]] = []

    def run(probe: str, lean_root: Path, module: str) -> str:
        calls.append((probe, module))
        lines = []
        for number, line in enumerate(probe.splitlines(), start=1):
            wrapped = re.match(r'#autoform_count_heartbeats "(.+)" in$', line)
            if wrapped:
                name = wrapped.group(1)
                lines.append(f"/tmp/Probe.lean:{number}:0: info: {PROBE_MARKER}{used.get(name, 1)} {name}")
            for needle, message in (errors or {}).items():
                if needle in line and not line.startswith("#autoform"):
                    lines.append(f"/tmp/Probe.lean:{number}:2: error(lean.unknownIdentifier): {message}")
        return "\n".join(lines) + "\n"

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _limits_project(tmp_path: Path) -> tuple[Path, Path]:
    project = _project(tmp_path)
    (project / "Skel" / "Limits.lean").write_text(_SOURCE, encoding="utf-8")
    names = ["scoped", "fileWide", "unbounded", "atDefault", "broken"]
    blueprint = _blueprint(tmp_path, lean={name.lower(): f"Skel.Limits.{name}" for name in names})
    return project, blueprint


def test_the_probe_wraps_each_target_with_its_docstring_and_wrappers() -> None:
    probe, origin = render_probe(_SOURCE, {"Skel.Limits.scoped": 17, "Skel.Limits.fileWide": 19})
    lines = probe.splitlines()

    header = lines.index("import Skel.Main")
    assert lines[header + 1] == "import Lean.Elab.Command"
    assert lines[header + 2] == "open Lean Elab Command in"
    wrapped = lines.index('#autoform_count_heartbeats "Skel.Limits.scoped" in')
    assert lines[wrapped + 1] == "/-- A docstring"
    assert lines[lines.index('#autoform_count_heartbeats "Skel.Limits.fileWide" in') + 1].startswith("theorem fileWide")
    # Every source line survives, in order, and is traced back to its number.
    assert [line for line, source in zip(lines, origin) if source is not None] == _SOURCE.splitlines()
    assert [source for source in origin if source is not None] == list(range(1, len(_SOURCE.splitlines()) + 1))


def test_a_file_without_imports_gets_one() -> None:
    probe, origin = render_probe("theorem t : True := trivial\n", {"t": 1})

    assert probe.splitlines()[0] == "import Lean.Elab.Command"
    assert probe.splitlines()[-2:] == ['#autoform_count_heartbeats "t" in', "theorem t : True := trivial"]
    assert origin[-1] == 1


def test_parse_places_counts_and_errors() -> None:
    origin = [None, 1, 2, 3, None, 4, 5]
    output = "\n".join(
        [
            f"/tmp/P.lean:5:0: info: {PROBE_MARKER}312 A.a",
            f"/tmp/P.lean:1:0: info: {PROBE_MARKER}9 Not.requested",
            "/tmp/P.lean:7:2: error: unknown identifier 'x'",
            "/tmp/P.lean:1:0: error(lean.unknownSyntax): bad counter",
            "/tmp/P.lean:3:0: warning: unused variable",
        ]
    )

    counts, errors, inserted = parse_probe_output(output, origin, {"A.a": (4, 5), "A.b": (1, 3)})

    assert counts == {"A.a": 312}
    assert errors == {"A.a": ["line 5: unknown identifier 'x'"]}
    assert inserted == ["bad counter"]


def test_targets_under_raised_limits_are_measured_and_advised(tmp_path: Path) -> None:
    project, blueprint = _limits_project(tmp_path)
    runner = _counting(
        {"Skel.Limits.scoped": 350000, "Skel.Limits.fileWide": 1200, "Skel.Limits.unbounded": 900000},
        errors={"exact missing": "unknown identifier 'missing'"},
    )

    report = measure_heartbeats(blueprint, lean_root=project, runner=runner)

    assert [call[1] for call in runner.calls] == ["Skel.Limits"]
    measured = {item.name: (item.used, item.needed) for item in report.measurements}
    assert measured == {
        "Skel.Limits.scoped": (350000, 400000),
        "Skel.Limits.fileWide": (1200, None),
        "Skel.Limits.unbounded": (900000, 1600000),
        "Skel.Limits.broken": (None, None),
    }
    # `atDefault` puts the default back on itself, so nothing raised reaches it.
    assert report.findings == (
        "basics/broken: Skel.Limits.broken: elaboration failed: line 29: unknown identifier 'missing'",
        "basics/filewide: Skel.Limits.fileWide: uses 1200 heartbeats, within the default 200000; "
        "`set_option maxHeartbeats 800000` at Skel/Limits.lean:6 is not needed",
        "basics/scoped: Skel.Limits.scoped: uses 350000 heartbeats; lower "
        "`set_option maxHeartbeats 1600000` at Skel/Limits.lean:15 to 400000",
        "basics/unbounded: Skel.Limits.unbounded: uses 900000 heartbeats; put `set_option maxHeartbeats 1600000 in` "
        "on it instead of `set_option maxHeartbeats 0` at Skel/Limits.lean:21",
    )
    assert not report.clean
    assert "Skel.Limits.fileWide: 1200 heartbeats under 800000 (file)" in format_heartbeats(report)


def test_named_articles_are_measured_even_at_the_default(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"main": "Skel.heavy_of_weight"})

    unselected = measure_heartbeats(blueprint, lean_root=project, runner=_counting({}))
    selected = measure_heartbeats(blueprint, lean_root=project, node_ids=("basics/main",), runner=_counting({}))

    assert unselected.measurements == ()
    assert [(item.name, item.used, item.findings) for item in selected.measurements] == [
        ("Skel.heavy_of_weight", 1, ())
    ]


def test_module_system_files_are_reported_unmeasured(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"public": "Skel.PublicPartial.usesPublic"})
    runner = _counting({})

    report = measure_heartbeats(blueprint, lean_root=project, node_ids=("basics/public",), runner=runner)

    assert runner.calls == []
    assert [item.message for item in report.unresolved] == [
        "basics/public: Skel.PublicPartial.usesPublic: files using the module system (`module`) are not measured yet"
    ]


def test_cli_writes_the_report_as_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    project, blueprint = _limits_project(tmp_path)
    monkeypatch.setattr(heartbeats, "_run_probe", lambda probe, root, module, timeout: _counting({})(probe, root, module))

    code = main(["heartbeats", str(blueprint), "--lean-root", str(project), "--node", "basics/filewide", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["schema"] == "autoform-heartbeats/v1"
    assert report["measurements"] == [
        {
            "errors": [],
            "limits": [{"line": 6, "scope": "file", "value": 800000}],
            "line": 19,
            "name": "Skel.Limits.fileWide",
            "needed": None,
            "node_id": "basics/filewide",
            "path": "Skel/Limits.lean",
            "used": 1,
        }
    ]


_HEAVY = """\
/- A file whose header starts with a comment. -/
import Skel.Main

set_option maxHeartbeats 800000

namespace Skel.Heavy

/-- Documented,

across a blank line. -/
@[simp]
-- the limit below is what the probe must keep
set_option maxHeartbeats 400000 in
theorem documented : (List.range 50).length = 50 := by
  simp

theorem fileWide : 2 + 2 = 4 := rfl

set_option maxHeartbeats 0 in
theorem unbounded : True := trivial

end Skel.Heavy
"""


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_heartbeats_in_a_built_project(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "Skel" / "Heavy.lean").write_text(_HEAVY, encoding="utf-8")
    with (project / "Skel.lean").open("a", encoding="utf-8") as handle:
        handle.write("import Skel.Heavy\n")
    _build(project)
    names = ["documented", "fileWide", "unbounded"]
    blueprint = _blueprint(tmp_path, lean={name.lower(): f"Skel.Heavy.{name}" for name in names})

    report = measure_heartbeats(blueprint, lean_root=project, timeout=300)

    assert report.unresolved == ()
    assert [item.name for item in report.measurements] == [f"Skel.Heavy.{name}" for name in names]
    for item in report.measurements:
        assert item.errors == ()
        assert item.used is not None and 0 <= item.used < 200000
        assert item.needed is None
    assert all("is not needed" in finding for finding in report.findings)
    assert len(report.findings) == 3
