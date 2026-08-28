from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.article_identity import plan_article_ids
from autoform_cli.graph import GraphValidationError, load_graph


def _article(path: Path, title: str, article_id: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = f"article_id: {article_id}\n" if article_id else ""
    path.write_text(f"---\n{frontmatter}---\n\n# {title}\n", encoding="utf-8")


def _blueprint(tmp_path: Path) -> Path:
    blueprint = tmp_path / "blueprint"
    _article(blueprint / "roadmap/README.md", "Roadmap")
    _article(blueprint / "roadmap/chapter/README.md", "Chapter")
    _article(blueprint / "roadmap/chapter/result.md", "Result")
    return blueprint


def test_graph_loads_valid_article_id_and_preserves_path_key(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"
    _article(blueprint / "roadmap/chapter/result.md", "Result", article_id)

    graph = load_graph(blueprint)

    assert graph.nodes["chapter/result"].id == "chapter/result"
    assert graph.nodes["chapter/result"].article_id == article_id


def test_graph_rejects_malformed_and_duplicate_article_ids(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    _article(blueprint / "roadmap/chapter/result.md", "Result", "result")
    with pytest.raises(GraphValidationError, match="malformed article_id"):
        load_graph(blueprint)

    duplicate = "af_0123456789abcdef01234567"
    _article(blueprint / "roadmap/chapter/README.md", "Chapter", duplicate)
    _article(blueprint / "roadmap/chapter/result.md", "Result", duplicate)
    with pytest.raises(GraphValidationError, match="duplicate article_id"):
        load_graph(blueprint)


def test_plan_is_deterministic_read_only_and_reports_exact_hashes(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    before = {path: path.read_bytes() for path in blueprint.rglob("*.md")}

    first = plan_article_ids(blueprint)
    second = plan_article_ids(blueprint)

    assert first == second
    assert first.missing_count == 3
    assert not first.complete
    assert all(entry.article_id.startswith("af_") for entry in first.entries)
    assert all(len(entry.source_sha256) == 64 for entry in first.entries)
    for entry in first.entries:
        assert entry.source_sha256 == hashlib.sha256(
            (blueprint / entry.article_path).read_bytes()
        ).hexdigest()
    assert {path: path.read_bytes() for path in blueprint.rglob("*.md")} == before


def test_plan_rejects_collision_between_authored_and_proposed_ids(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    initial = plan_article_ids(blueprint)
    proposed = next(entry.article_id for entry in initial.entries if entry.path_id == "chapter/result")
    _article(blueprint / "roadmap/README.md", "Roadmap", proposed)

    with pytest.raises(GraphValidationError, match="also names article"):
        plan_article_ids(blueprint)


def test_cli_json_and_check_exit_status(tmp_path: Path, capsys) -> None:
    blueprint = _blueprint(tmp_path)

    assert main(["migrate", "article-ids", str(blueprint), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "autoform-article-id-plan/v1"
    assert payload["missing_count"] == 3
    assert main(["migrate", "article-ids", str(blueprint), "--check"]) == 1
    assert "3 article(s) need" in capsys.readouterr().out

    ids = {entry["article_path"]: entry["article_id"] for entry in payload["entries"]}
    for relative, article_id in ids.items():
        path = blueprint / relative
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("---\n", f"---\narticle_id: {article_id}\n", 1), encoding="utf-8")

    assert main(["migrate", "article-ids", str(blueprint), "--check"]) == 0
    assert "3 articles have durable" in capsys.readouterr().out
