from __future__ import annotations

import json
from pathlib import Path

import pytest

import autoform_cli.__main__ as cli
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
        "PASS: runtime: autoform-runtime/v2; markdown-articles; revision "
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


def _with_body(tmp_path: Path, body: str) -> Path:
    blueprint = _clean_blueprint(tmp_path)
    article = blueprint / "roadmap" / "result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace("A precise statement.\n", f"A precise statement.\n\n{body}\n"),
        encoding="utf-8",
    )
    return blueprint


@pytest.mark.parametrize(
    ("body", "issue"),
    [
        ("<img/src=x onerror=alert(1)>", "raw HTML is not allowed: the site would publish"),
        ("Some <span style='display:none'>hidden</span> text.", "raw HTML is not allowed: <span>"),
        ("Fish &amp; chips.", "HTML character references are not allowed: &amp;"),
        ("<!-- a note that never ends", "HTML comments are not allowed"),
        ("<script>alert(1)</script>", "raw HTML is not allowed: <script>"),
    ],
)
def test_check_refuses_raw_html_in_an_article(tmp_path: Path, capsys, body: str, issue: str) -> None:
    blueprint = _with_body(tmp_path, body)

    assert main(["check", str(blueprint)]) == 1
    output = capsys.readouterr().out
    assert f"error: result: line 9: {issue}" in output
    assert "OK:" not in output


@pytest.mark.parametrize(
    "body",
    [
        "<!-- a note to the authors -->",
        "```lean\ntheorem lt : (1 : Nat) < 2 := by decide\n```",
        "For $a < b$ and $b > c$.",
    ],
)
def test_check_allows_comments_code_and_formulas(tmp_path: Path, capsys, body: str) -> None:
    """Deliberate guard: what an article may still say."""

    blueprint = _with_body(tmp_path, body)

    assert main(["check", str(blueprint)]) == 0, capsys.readouterr().out


def test_check_judges_the_articles_its_graph_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The verdict is about the articles the graph parsed; markup written after
    the load is judged by the next check, never paired with the earlier parse."""

    blueprint = _clean_blueprint(tmp_path)
    article = blueprint / "roadmap" / "result.md"
    load = cli.load_graph

    def load_then_edit(*args: object, **kwargs: object) -> object:
        graph = load(*args, **kwargs)
        article.write_text(
            article.read_text(encoding="utf-8").replace(
                "A precise statement.\n", "A precise statement.\n\n<script>alert(1)</script>\n"
            ),
            encoding="utf-8",
        )
        return graph

    monkeypatch.setattr(cli, "load_graph", load_then_edit)
    assert main(["check", str(blueprint)]) == 0, capsys.readouterr().out
    monkeypatch.undo()

    assert main(["check", str(blueprint)]) == 1
    assert "error: result: line 9: raw HTML is not allowed: <script>" in capsys.readouterr().out
