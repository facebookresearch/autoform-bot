from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli import statement_probe
from autoform_cli.__main__ import main
from autoform_cli.statement_probe import (
    PROBE_MARKER,
    ProbeError,
    format_probe_report,
    parse_probe_output,
    probe_statements,
    render_probe,
)
from tests.test_skeleton import _blueprint, _build, _lean_toolchain_available, _project


def _record(root: str, **fields: object) -> str:
    return PROBE_MARKER + json.dumps({"root": root, **fields})


def _theorem(root: str, *hypotheses: tuple[str, str, bool], proof: str = "proved") -> str:
    listed = [{"name": name, "type": type_, "used": used} for name, type_, used in hypotheses]
    return _record(root, found=True, kind="theorem", proof=proof, hypotheses=listed)


def _definition(root: str) -> str:
    return _record(root, found=True, kind="def", proof="none", hypotheses=[])


def test_the_probe_lifts_lean_s_command_heartbeat_limit() -> None:
    # All declarations run in one command; under Lean's default limit a real
    # project ran out of heartbeats. The CLI's timeout bounds the run instead.
    probe = render_probe(imports=("Skel",), roots=("Skel.heavy",))

    assert "set_option maxHeartbeats 0 in\nrun_cmd do" in probe


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([_record("A.p", found=False), _record("A.p", found=False)], "twice"),
        ([], "no answer for A.p"),
        ([_theorem("A.p", ("h", "P", False), proof="sorry")], "without reading its proof"),
        ([_record("A.p", found=True, kind="def", proof="proved", hypotheses=[])], "exactly when it is not a theorem"),
    ],
    ids=["two-answers", "no-answer", "hypotheses-of-an-unread-proof", "proof-of-a-def"],
)
def test_parse_refuses_answers_that_do_not_fit_the_question(lines: list[str], message: str) -> None:
    with pytest.raises(ProbeError, match=message):
        parse_probe_output("\n".join(lines), expected_roots=("A.p",))


def test_an_unused_hypothesis_is_a_finding_that_does_not_fail(tmp_path: Path) -> None:
    project = _project(tmp_path)
    lean = {"eligible": "Skel.Eligible", "heavy": "Skel.heavy_of_weight", "pending": "Skel.observation_determined"}
    blueprint = _blueprint(tmp_path, lean=lean)
    output = "\n".join(
        [
            _definition("Skel.Eligible"),
            _theorem("Skel.heavy_of_weight", ("h", "0 < y", False), ("_", "Fact p", True)),
            _theorem("Skel.observation_determined", proof="sorry"),
        ]
    )

    report = probe_statements(blueprint, lean_root=project, runner=lambda probe, root: output)

    # The source may state a hypothesis its theorem does not need: a reviewer
    # decides, so the run passes.
    assert report.clean
    assert report.findings == ("basics/heavy: Skel.heavy_of_weight: hypothesis-unused: the proof never uses h : 0 < y",)
    text = format_probe_report(report)
    assert "  Skel.heavy_of_weight (theorem): 1 of 2 hypotheses unused\n" in text
    assert "  Skel.observation_determined (theorem): the proof rests on sorry, so it was not read\n" in text
    assert "  Skel.Eligible (def)\n" in text
    assert "\nerror:" not in text


def test_unresolved_declarations_are_reported_and_fail(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"absent": "Skel.absent", "heavy": "Skel.heavy_of_weight, Skel.Eligible"})

    output = "\n".join([_record("Skel.heavy_of_weight", found=False), _definition("Skel.Eligible")])

    report = probe_statements(blueprint, lean_root=project, runner=lambda probe, root: output)

    assert [item.message for item in report.unresolved] == [
        "basics/absent: Skel.absent: declaration not found in the Lean sources",
        "basics/heavy: Skel.heavy_of_weight: declaration not found in the built environment",
    ]
    assert [item.name for item in report.declarations] == ["Skel.Eligible"]
    assert not report.clean


def test_nothing_placed_reports_without_running_lean(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"absent": "Skel.absent"})

    def runner(probe: str, root: Path) -> str:
        raise AssertionError("no declaration was placed, so Lean must not run")

    report = probe_statements(blueprint, lean_root=project, runner=runner)

    assert [item.declaration for item in report.unresolved] == ["Skel.absent"]


def test_the_probe_imports_only_the_modules_it_reads_and_narrows_to_the_selection(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"heavy": "Skel.heavy_of_weight", "unambiguous": "Skel.NonAmbiguous"})
    asked: list[str] = []

    def runner(probe: str, root: Path) -> str:
        asked.append(probe)
        return _theorem("Skel.heavy_of_weight")

    report = probe_statements(blueprint, lean_root=project, node_ids=("basics/heavy",), runner=runner)

    assert [item.node_id for item in report.declarations] == ["basics/heavy"]
    # Lake checks the freshness of everything imported, so nothing else is.
    assert [line for line in asked[0].splitlines() if line.startswith("import Skel")] == ["import Skel.Main"]
    assert "NonAmbiguous" not in asked[0]


def test_cli_exit_codes_json_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    blueprint = _blueprint(tmp_path, lean={"heavy": "Skel.heavy_of_weight"})
    timeouts: list[float] = []

    def fake_run_probe(probe: str, root: Path, *, timeout: float, label: str) -> str:
        timeouts.append(timeout)
        return _theorem("Skel.heavy_of_weight", ("h", "0 < y", False))

    monkeypatch.setattr(statement_probe, "run_probe", fake_run_probe)

    assert main(["probe", str(blueprint), "--lean-root", str(project), "--json", "--timeout", "30"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "autoform-probe/v1"
    assert payload["declarations"][0]["hypotheses"] == [{"name": "h", "type": "0 < y", "used": False}]
    assert main(["probe", str(blueprint), "--lean-root", str(project)]) == 0
    assert "finding: basics/heavy: Skel.heavy_of_weight: hypothesis-unused" in capsys.readouterr().out
    assert timeouts == [30.0, statement_probe.DEFAULT_PROBE_TIMEOUT]
    assert main(["probe", str(blueprint), "--lean-root", str(project), "--node", "nope"]) == 2


_UNUSED = """\
import Skel.Main

namespace Skel

variable {Y : Type}

class PosFact (n : Nat) : Prop where
  pos : 0 < n

-- The hypothesis is not used.
theorem needless (y : Y) (h : y = y) : y = y := rfl
-- `same` mentions `h`, so `h` cannot be deleted on its own; `same` can.
theorem chain (n : Nat) (h : 0 < n) (same : h = h) : n = n := rfl
-- The conclusion mentions `h`, so it cannot be deleted.
theorem stated (p : Prop) (h : p) : h = h := rfl
-- A proof that is another theorem may use anything: nothing is claimed unused.
theorem via : ∀ (y : Nat) (h : y = y), y = y := needless
-- An instance hypothesis, whose name no reader wrote.
theorem inst (n : Nat) [PosFact n] : 0 < n + 1 := Nat.succ_pos n
-- The term of a `sorry` uses nothing, so it says nothing.
theorem pending (n : Nat) (h : 0 < n) : 0 < n := sorry

end Skel
"""


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_unused_hypotheses_in_a_built_project(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "Skel" / "Unused.lean").write_text(_UNUSED, encoding="utf-8")
    with (project / "Skel.lean").open("a", encoding="utf-8") as handle:
        handle.write("import Skel.Unused\n")
    _build(project)
    names = "Eligible chain heavy_of_weight inst needless pending stated via".split()

    blueprint = _blueprint(tmp_path, lean={name.lower(): f"Skel.{name}" for name in names})

    report = probe_statements(blueprint, lean_root=project)

    assert report.unresolved == ()
    read = {
        item.name: (item.proof, [(h.name, h.type, h.used) for h in item.hypotheses]) for item in report.declarations
    }
    assert read == {
        "Skel.needless": ("proved", [("h", "y = y", False)]),
        "Skel.heavy_of_weight": ("proved", [("h", "0 < Skel.HasWeight.weight y", True)]),
        "Skel.chain": ("proved", [("same", "h = h", False)]),
        "Skel.stated": ("proved", []),
        "Skel.via": ("proved", [("h", "y = y", True)]),
        "Skel.inst": ("proved", [("_", "Skel.PosFact n", False)]),
        "Skel.pending": ("sorry", []),
        "Skel.Eligible": ("none", []),
    }
