from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autoform_cli.__main__ import main
from autoform_cli.ready import READY_SCHEMA, list_ready_work


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    blueprint = project / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "README.md").write_text(
        "---\narticle_id: af_000000000000000000000000\n---\n\n# Roadmap\n",
        encoding="utf-8",
    )
    (roadmap / "result.md").write_text(
        "---\n"
        "article_id: af_111111111111111111111111\n"
        "declaration: theorem\n"
        "source_units: [result]\n"
        "---\n\n"
        "# Result\n\nA precise result.\n\n"
        "## Depends on\n\nNo prerequisites.\n",
        encoding="utf-8",
    )
    artifact = b"The source result.\n"
    sources = blueprint / "sources"
    sources.mkdir()
    (sources / "book.md").write_bytes(artifact)
    coverage = blueprint / "coverage"
    coverage.mkdir()
    (coverage / "README.md").write_text(
        "---\n"
        "schema: autoform-coverage/v2\n"
        "artifact: sources/book.md\n"
        f"artifact_sha256: {_digest(artifact)}\n"
        "---\n\n"
        "# Coverage\n\n"
        "| Unit | Area | Lines | Locator | Unit SHA-256 | Coverage | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| result | Main result | 1-1 | Theorem 1 | {_digest(artifact)} | "
        "DECOMPOSED | [Result](../roadmap/result.md) |\n",
        encoding="utf-8",
    )
    return project


def test_ready_work_advances_from_statement_to_proof(tmp_path: Path) -> None:
    project = _project(tmp_path)

    statement = list_ready_work(project)
    assert [(item.node_id, item.phase) for item in statement.items] == [
        ("result", "statement")
    ]
    assert statement.blocked == 0
    assert statement.complete == 0

    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "declaration: theorem\n",
            "declaration: theorem\nstatement: formalized\nlean: Example.result\n",
        ),
        encoding="utf-8",
    )
    (project / "Example.lean").write_text(
        "namespace Example\n\ntheorem result : True := by\n  trivial\n\nend Example\n",
        encoding="utf-8",
    )

    proof = list_ready_work(project, lean_root=project)
    assert [(item.node_id, item.phase) for item in proof.items] == [
        ("result", "proof")
    ]
    assert proof.source_contract_sha256 == statement.source_contract_sha256
    assert proof.source_revision != statement.source_revision


def test_ready_cli_emits_stable_path_independent_json(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)

    assert main(["ready", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema"] == READY_SCHEMA
    assert payload["items"] == [
        {
            "article_id": "af_111111111111111111111111",
            "article_path": "blueprint/roadmap/result.md",
            "node_id": "result",
            "phase": "statement",
            "title": "Result",
        }
    ]
    assert payload["blocked_items"] == []
    assert str(tmp_path) not in json.dumps(payload)


def test_ready_cli_requires_exhaustive_coverage(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Result | DECOMPOSED | [Result](../roadmap/result.md) |\n",
        encoding="utf-8",
    )

    assert main(["ready", str(project), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == READY_SCHEMA
    assert payload["items"] == []
    assert {error["code"] for error in payload["errors"]} == {"coverage-v2-required"}


def test_ready_reports_authored_and_dependency_blockers(tmp_path: Path) -> None:
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    result = blueprint / "roadmap/result.md"
    result.write_text(
        result.read_text(encoding="utf-8").replace(
            "declaration: theorem\n",
            "declaration: theorem\nnot_ready: true\n",
        ),
        encoding="utf-8",
    )
    (blueprint / "roadmap/dependent.md").write_text(
        "---\n"
        "article_id: af_222222222222222222222222\n"
        "declaration: theorem\n"
        "source_units: [result]\n"
        "---\n\n"
        "# Dependent\n\nA dependent result.\n\n"
        "## Depends on\n\n- [Result](result.md)\n",
        encoding="utf-8",
    )
    coverage = blueprint / "coverage/README.md"
    coverage.write_text(
        coverage.read_text(encoding="utf-8").replace(
            "[Result](../roadmap/result.md)",
            "[Result](../roadmap/result.md), [Dependent](../roadmap/dependent.md)",
        ),
        encoding="utf-8",
    )

    ready = list_ready_work(project)

    assert ready.items == ()
    assert ready.blocked == 2
    blocked = {item.node_id: item for item in ready.blocked_items}
    assert blocked["result"].phase == "statement"
    assert blocked["result"].reasons == ("authored-not-ready",)
    assert blocked["result"].blocked_by == ()
    assert blocked["dependent"].phase == "statement"
    assert blocked["dependent"].reasons == ("statement-dependency-not-stated",)
    assert blocked["dependent"].blocked_by == ("result",)


def test_ready_rejects_missing_lean_target_for_completed_work(
    tmp_path: Path,
    capsys,
) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "declaration: theorem\n",
            "declaration: theorem\n"
            "statement: formalized\n"
            "proof: formalized\n"
            "lean: Example.missing\n",
        ),
        encoding="utf-8",
    )

    assert main(["ready", str(project), "--lean-root", str(project), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocked_items"] == []
    assert {error["code"] for error in payload["errors"]} == {
        "lean-target-not-found"
    }
