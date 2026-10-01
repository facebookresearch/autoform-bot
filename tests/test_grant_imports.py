from __future__ import annotations

import json
from pathlib import Path

import pytest

from servers.prover import debrief, grant_imports
from tests.test_runtime import _article


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(debrief.DEBRIEF_DIR_ENV, raising=False)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    _article(project, "README.md", title="Roadmap")
    _article(project, "chapter/README.md", title="Chapter")
    _article(
        project,
        "chapter/result.md",
        title="Result",
        declaration="theorem",
        statement="formalized",
        lean="Project.result",
    )
    (project / "Project.lean").write_text(
        "import Mathlib.Data.Nat.Basic\nimport Mathlib.Order.Basic\n\n"
        "theorem Project.result : True := trivial\n",
        encoding="utf-8",
    )
    mathlib = project / ".lake" / "packages" / "mathlib" / "Mathlib"
    for module in ("Data/Nat/Basic", "Order/Basic", "Algebra/Real", "NumberTheory/Real"):
        (mathlib / f"{module}.lean").parent.mkdir(parents=True, exist_ok=True)
        (mathlib / f"{module}.lean").write_text("", encoding="utf-8")
    (mathlib / "NumberTheory" / "Real.lean").write_text("theorem foo_bar : True := trivial\n")
    return project


def _debrief(project: Path, node: str, answer: dict) -> None:
    debrief.write_record(
        debrief.debrief_dir(project),
        {"node_id": node, "run_id": node.replace("/", "-"), "debrief": answer},
    )


def test_harvest_collects_import_requests_from_both_fields(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _debrief(project, "chapter/result", {
        "infrastructure_proposals": [
            {"kind": "import-export", "what": "import Mathlib.Algebra.Real for `foo_bar`."},
            {"kind": "helper-lemma", "what": "Mathlib.Ignored.Module"},
        ],
        "searched_for": [
            {"why_hard": "inaccessible-import", "wanted": "`baz`", "resolution": "Mathlib.Order.Basic"},
        ],
    })

    asks = grant_imports.harvest(debrief.debrief_dir(project) / debrief.RECORDS_FILENAME)

    assert [(a["node"], a["module"], a["wanted"]) for a in asks] == [
        ("chapter/result", "Mathlib.Algebra.Real", ["foo_bar"]),
        ("chapter/result", "Mathlib.Order.Basic", ["baz"]),
    ]


def test_node_targets_route_through_the_runtime_graph(tmp_path: Path) -> None:
    assert grant_imports.node_targets(_project(tmp_path)) == {"chapter/result": ["Project.lean"]}


def test_build_plan_classifies_and_suggests_real_modules(tmp_path: Path) -> None:
    project = _project(tmp_path)
    asks = [
        {"node": "chapter/result", "module": "Mathlib.Algebra.Real", "wanted": [], "evidence": ""},
        {"node": "chapter/result", "module": "Mathlib.Algebra.Real", "wanted": [], "evidence": ""},
        {"node": "chapter/result", "module": "Mathlib.Order.Basic", "wanted": [], "evidence": ""},
        {"node": "chapter/result", "module": "Mathlib.Made.Up", "wanted": ["Real.foo_bar"], "evidence": ""},
        {"node": "chapter/other", "module": "Mathlib.Algebra.Real", "wanted": [], "evidence": ""},
    ]

    plan = grant_imports.build_plan(project, asks, {"chapter/result": ["Project.lean"]})

    by_module = {g["module"]: g for g in plan["grants"]}
    assert by_module["Mathlib.Algebra.Real"]["status"] == "resolved"
    assert by_module["Mathlib.Algebra.Real"]["approved"] is True
    assert by_module["Mathlib.Algebra.Real"]["times_asked"] == 2
    assert by_module["Mathlib.Order.Basic"]["status"] == "already-imported"
    made_up = by_module["Mathlib.Made.Up"]
    assert made_up["status"] == "module-not-found" and made_up["approved"] is False
    assert made_up["suggested_modules"] == ["Mathlib.NumberTheory.Real"]
    assert [a["node"] for a in plan["unrouted"]] == ["chapter/other"]


def test_insert_import_keeps_a_sorted_block_sorted(tmp_path: Path) -> None:
    path = tmp_path / "A.lean"
    path.write_text("import Mathlib.A\nimport Mathlib.C\n\ntheorem t : True := trivial\n")
    grant_imports.insert_import(path, "Mathlib.B")
    assert path.read_text().startswith("import Mathlib.A\nimport Mathlib.B\nimport Mathlib.C\n")

    bare = tmp_path / "B.lean"
    bare.write_text("import Project.Other\n\ntheorem t : True := trivial\n")
    grant_imports.insert_import(bare, "Mathlib.B")
    assert bare.read_text().startswith("import Project.Other\nimport Mathlib.B\n")


def test_apply_keeps_compiling_imports_reverts_failures_and_rejects_bad_entries(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    (project / "Broken.lean").write_text("theorem b : True := trivial\n")
    original_broken = (project / "Broken.lean").read_text()
    plan = {"grants": [
        {"approved": True, "module": "Mathlib.Algebra.Real", "file": "Project.lean"},
        {"approved": True, "module": "Mathlib.Algebra.Real", "file": "Broken.lean"},
        {"approved": True, "module": "Mathlib.Made.Up", "file": "Project.lean"},
        {"approved": True, "module": "Project.Internal", "file": "Project.lean"},
        {"approved": True, "module": "Mathlib.Algebra.Real", "file": "../outside.lean"},
        {"approved": False, "module": "Mathlib.NumberTheory.Real", "file": "Project.lean"},
    ]}
    compiled: list[str] = []

    def compile(root: Path, rel: str) -> tuple[bool, str]:
        compiled.append(rel)
        return rel != "Broken.lean", "error: boom"

    status = grant_imports.apply_plan(project, plan, compile=compile, debloat=False)

    assert status == 1
    assert compiled == ["Broken.lean", "Project.lean"]
    imports = grant_imports.current_imports(project, "Project.lean")
    assert "Mathlib.Algebra.Real" in imports
    assert "Mathlib.Made.Up" not in imports and "Mathlib.NumberTheory.Real" not in imports
    assert (project / "Broken.lean").read_text() == original_broken


def test_apply_dry_run_writes_nothing(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = (project / "Project.lean").read_text()
    plan = {"grants": [{"approved": True, "module": "Mathlib.Algebra.Real", "file": "Project.lean"}]}

    def compile(root: Path, rel: str) -> tuple[bool, str]:
        raise AssertionError("dry run must not compile")

    assert grant_imports.apply_plan(project, plan, dry_run=True, compile=compile) == 0
    assert (project / "Project.lean").read_text() == before


def test_cli_propose_reads_the_debrief_ledger_and_writes_the_plan_beside_it(
    tmp_path: Path, capsys
) -> None:
    project = _project(tmp_path)
    _debrief(project, "chapter/result", {
        "infrastructure_proposals": [{"kind": "import-export", "what": "needs Mathlib.Algebra.Real"}],
    })

    assert grant_imports.main(["--project", str(project), "propose"]) == 0

    plan = json.loads((project / ".autoform" / "debriefs" / "import-plan.json").read_text())
    assert [(g["module"], g["file"], g["status"]) for g in plan["grants"]] == [
        ("Mathlib.Algebra.Real", "Project.lean", "resolved")
    ]
    assert "READY TO GRANT" in capsys.readouterr().out


def test_apply_debloats_only_files_that_kept_imports_and_tolerates_skips(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from servers.prover import debloat_imports

    project = _project(tmp_path)
    (project / "Broken.lean").write_text("theorem b : True := trivial\n")
    debloated: list[tuple[str, bool]] = []

    def fake_debloat(path: Path, root: Path, *, assume_clean: bool = False, **kwargs) -> bool:
        debloated.append((path.name, assume_clean))
        raise debloat_imports.DebloatSkipped("stale build")

    monkeypatch.setattr(debloat_imports, "debloat", fake_debloat)
    plan = {"grants": [
        {"approved": True, "module": "Mathlib.Algebra.Real", "file": "Project.lean"},
        {"approved": True, "module": "Mathlib.Algebra.Real", "file": "Broken.lean"},
    ]}

    status = grant_imports.apply_plan(
        project, plan, compile=lambda root, rel: (rel == "Project.lean", "error: boom")
    )

    assert status == 1  # from Broken.lean's compile failure, not the skipped debloat
    assert debloated == [("Project.lean", True)]
    assert "Mathlib.Algebra.Real" in grant_imports.current_imports(project, "Project.lean")
    assert "debloat skipped (granted imports kept): stale build" in capsys.readouterr().out
