from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli import __main__ as cli
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
        "PASS: runtime: autoform-runtime/v1; markdown-articles; revision "
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
        "    coverage: 0 mapped · 0 decomposed · 0 deferred · 1 out\n"
    )


def test_check_cli_rejects_replacement_before_printing_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blueprint = _clean_blueprint(tmp_path / "selected")
    replacement = _clean_blueprint(tmp_path / "replacement")
    held = tmp_path / "held-blueprint"
    original_derive = cli.status.derive

    def replace_after_graph(graph):
        result = original_derive(graph)
        blueprint.rename(held)
        replacement.rename(blueprint)
        return result

    monkeypatch.setattr(cli.status, "derive", replace_after_graph)
    try:
        assert main(["check", str(blueprint)]) == 1
        output = capsys.readouterr().out
        assert "blueprint directory changed during use" in output
        assert "OK:" not in output
    finally:
        blueprint.rename(replacement)
        held.rename(blueprint)


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
    assert "coverage: 1 mapped · 0 decomposed · 0 deferred · 0 out" in output
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
    }


@pytest.mark.parametrize("command", ("check", "audit"))
def test_validation_commands_report_unsafe_lean_tree_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    blueprint = _clean_blueprint(tmp_path)
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    outside = tmp_path / "Outside.lean"
    outside.write_text("def outside : Nat := 0\n", encoding="utf-8")
    try:
        (lean_root / "Linked.lean").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert main([command, str(blueprint), "--lean-root", str(lean_root)]) == 1
    output = capsys.readouterr().out
    assert "unsafe Lean source Linked.lean: symbolic links are not supported" in output
