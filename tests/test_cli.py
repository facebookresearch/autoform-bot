from __future__ import annotations

import json
from pathlib import Path

from autoform_cli.__main__ import main
from autoform_cli.runtime import load_runtime_graph


def _clean_blueprint(tmp_path: Path) -> Path:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    coverage = blueprint / "coverage"
    roadmap.mkdir(parents=True)
    coverage.mkdir(parents=True)
    (roadmap / "result.md").write_text(
        "---\ndeclaration: theorem\n---\n\n"
        "# Result\n\nA precise statement.\n\n## Depends on\n\nNo prerequisites.\n",
        encoding="utf-8",
    )
    (coverage / "README.md").write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Narrative | OUT | No formalization target |\n",
        encoding="utf-8",
    )
    return blueprint


def test_doctor_cli_reports_deterministic_human_and_json_output(tmp_path: Path, capsys) -> None:
    blueprint = _clean_blueprint(tmp_path)

    assert main(["doctor", str(blueprint)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "PASS: blueprint: resolved blueprint",
        "PASS: runtime: autoform-runtime/v4; markdown-articles; revision "
        + load_runtime_graph(blueprint).source_revision,
        "PASS: graph: 1 articles; 0 dependencies; 1 formalizable; 1 dispatchable; depth 0",
        "PASS: references: all parents, typed dependencies, and dispatchable leaves are consistent",
        "PASS: audit: roadmap audit passed",
        "PASS: lean targets: not checked; no Lean root supplied",
    ]

    assert main(["doctor", str(blueprint), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["clean"] is True
    assert [check["name"] for check in payload["checks"]] == [
        "blueprint",
        "runtime",
        "graph",
        "references",
        "audit",
        "lean targets",
    ]
    assert str(tmp_path) not in json.dumps(payload)


def test_doctor_cli_returns_failure_without_traceback(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing"

    assert main(["doctor", str(missing), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["clean"] is False
    assert payload["checks"][0] == {
        "detail": "project or blueprint directory does not exist",
        "name": "blueprint",
        "ok": False,
    }


def test_audit_cli_reports_clean_human_output(tmp_path: Path, capsys) -> None:
    blueprint = _clean_blueprint(tmp_path)

    assert main(["audit", str(blueprint)]) == 0
    assert capsys.readouterr().out == (
        "OK: roadmap audit passed\n"
        "    coverage: 0 mapped · 0 decomposed · 0 inventoried · 0 deferred · 1 out\n"
    )


def test_audit_cli_prints_coverage_summary_with_findings(tmp_path: Path, capsys) -> None:
    blueprint = _clean_blueprint(tmp_path)
    (blueprint / "coverage/README.md").write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main theorem | MAPPED | Source audit pending |\n",
        encoding="utf-8",
    )

    assert main(["audit", str(blueprint)]) == 1
    output = capsys.readouterr().out
    assert (
        "coverage: 1 mapped · 0 decomposed · 0 inventoried · 0 deferred · 0 out"
        in output
    )
    assert "declared-coverage-gap" in output


def test_audit_cli_reports_stable_json_and_failure(tmp_path: Path, capsys) -> None:
    blueprint = _clean_blueprint(tmp_path)
    (blueprint / "coverage" / "README.md").unlink()

    assert main(["audit", str(blueprint), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "clean": False,
        "coverage": None,
        "findings": [
            {
                "article_path": "coverage/README.md",
                "code": "missing-coverage-contract",
                "reason": "coverage contract is missing",
            }
        ],
        "warnings": [],
    }


def test_audit_cli_passes_with_only_warnings_and_prints_them(tmp_path: Path, capsys) -> None:
    blueprint = tmp_path / "blueprint"
    (blueprint / "roadmap").mkdir(parents=True)
    (blueprint / "sources").mkdir()
    (blueprint / "coverage").mkdir()
    (blueprint / "roadmap" / "inventory.md").write_text(
        "---\ncatalog: module\nlean: Inv.a Inv.b\n---\n\n"
        "# Inventory\n\nAn existing module.\n\n## Sources\n\n- [Ledger](../sources/ledger.md)\n",
        encoding="utf-8",
    )
    (blueprint / "sources" / "ledger.md").write_text("# Ledger\n", encoding="utf-8")
    (blueprint / "coverage" / "README.md").write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Inventory | INVENTORIED | [Inventory](../roadmap/inventory.md) |\n",
        encoding="utf-8",
    )
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Inv.lean").write_text(
        "set_option maxHeartbeats 0\ntheorem Inv.a : True := trivial\ntheorem Inv.b : True := trivial\n",
        encoding="utf-8",
    )
    command = ["audit", str(blueprint), "--lean-root", str(lean_root)]
    message = (
        "2 of 2 catalogued declarations have no maxHeartbeats limit (Inv.a at Inv.lean:1 and 1 more); "
        "set a finite limit"
    )

    assert main(command) == 0
    assert capsys.readouterr().out.splitlines() == [
        "OK: roadmap audit passed",
        "    coverage: 0 mapped · 0 decomposed · 1 inventoried · 0 deferred · 0 out",
        f"warning: roadmap/inventory.md: catalog-unbounded-limit: {message}",
    ]
    assert main([*command, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["clean"], payload["findings"]) == (True, [])
    assert payload["warnings"] == [
        {
            "article_path": "roadmap/inventory.md",
            "code": "catalog-unbounded-limit",
            "message": message,
            "declarations": [
                {"name": name, "option": "maxHeartbeats", "value": 0, "location": "Inv.lean:1", "scope": "file"}
                for name in ("Inv.a", "Inv.b")
            ],
        }
    ]

    # An error fails the audit, and the warning still prints, after the error.
    (blueprint / "roadmap" / "inventory.md").write_text(
        "---\ncatalog: module\nlean: Inv.a Inv.b Inv.missing\n---\n\n"
        "# Inventory\n\nAn existing module.\n\n## Sources\n\n- [Ledger](../sources/ledger.md)\n",
        encoding="utf-8",
    )
    assert main(command) == 1
    assert capsys.readouterr().out.splitlines() == [
        "    coverage: 0 mapped · 0 decomposed · 1 inventoried · 0 deferred · 0 out",
        "error: roadmap/inventory.md: lean-target-not-found: Lean declaration target was not found: Inv.missing",
        f"warning: roadmap/inventory.md: catalog-unbounded-limit: {message}",
    ]


def test_coverage_role_mismatch_warns_in_render_and_fails_audit(tmp_path: Path, capsys) -> None:
    """Role mismatches are audit findings while a blueprint migrates.

    Render publishes them as warnings, the way it publishes ``MAPPED`` rows;
    only an invalid contract stops it. A drafted leaf that declares nothing
    yet is enough to trip the role check under a ``DECOMPOSED`` row.
    """
    blueprint = tmp_path / "blueprint"
    (blueprint / "roadmap").mkdir(parents=True)
    (blueprint / "coverage").mkdir()
    (blueprint / "roadmap" / "lemma.md").write_text(
        "# Lemma\n\nA drafted lemma that declares nothing yet.\n",
        encoding="utf-8",
    )
    (blueprint / "coverage" / "README.md").write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Section 1 | DECOMPOSED | [Lemma](../roadmap/lemma.md) |\n",
        encoding="utf-8",
    )

    assert main(["check", str(blueprint)]) == 0
    capsys.readouterr()
    assert main(["render", str(blueprint), "--output", str(tmp_path / "site")]) == 0
    rendered = capsys.readouterr().out
    assert "warning: coverage contract line 5: coverage area 'Section 1' is DECOMPOSED" in rendered
    assert (tmp_path / "site" / "publication.json").is_file()
    assert main(["audit", str(blueprint)]) == 1
    assert "error: coverage/README.md: coverage-role-mismatch:" in capsys.readouterr().out
