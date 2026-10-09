from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autoform_cli import article_identity
from autoform_cli.__main__ import main
from autoform_cli.article_identity import plan_article_ids, write_article_ids
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


def _bytes(blueprint: Path) -> dict[str, bytes]:
    return {path.relative_to(blueprint).as_posix(): path.read_bytes() for path in blueprint.rglob("*.md")}


def test_write_adds_each_planned_id_as_the_first_frontmatter_line_and_nothing_else(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    assigned = "af_0123456789abcdef01234567"
    _article(blueprint / "roadmap/chapter/README.md", "Chapter", assigned)
    plain = blueprint / "roadmap/chapter/plain.md"
    plain.write_text("# Plain\n\nNo frontmatter.\n", encoding="utf-8")
    before = _bytes(blueprint)
    plan = plan_article_ids(blueprint)
    planned = {entry.article_path: entry.article_id for entry in plan.entries if not entry.assigned}

    written = write_article_ids(blueprint)

    assert {entry.article_path: entry.article_id for entry in written} == planned
    after = _bytes(blueprint)
    assert after["roadmap/chapter/README.md"] == before["roadmap/chapter/README.md"]
    for path in ("roadmap/README.md", "roadmap/chapter/result.md"):
        assert after[path] == before[path].replace(b"---\n", f"---\narticle_id: {planned[path]}\n".encode(), 1)
    assert after["roadmap/chapter/plain.md"] == (
        f"---\narticle_id: {planned['roadmap/chapter/plain.md']}\n---\n\n".encode() + before["roadmap/chapter/plain.md"]
    )
    assert plan_article_ids(blueprint).complete
    assert load_graph(blueprint).nodes["chapter/plain"].title == "Plain"

    assert write_article_ids(blueprint) == ()
    assert _bytes(blueprint) == after


def test_write_keeps_crlf_line_endings(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path)
    result = blueprint / "roadmap/chapter/result.md"
    result.write_bytes(b"---\r\n---\r\n\r\n# Result\r\n")

    written = {entry.article_path: entry.article_id for entry in write_article_ids(blueprint)}

    article_id = written["roadmap/chapter/result.md"]
    assert result.read_bytes() == f"---\r\narticle_id: {article_id}\r\n---\r\n\r\n# Result\r\n".encode()
    assert load_graph(blueprint).nodes["chapter/result"].article_id == article_id


def test_write_refuses_every_article_when_one_changed_after_planning(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    result = blueprint / "roadmap/chapter/result.md"
    real_plan = article_identity.plan_article_ids

    def plan_then_edit(path):
        plan = real_plan(path)
        if not result.read_text(encoding="utf-8").endswith("Edited.\n"):
            result.write_text(result.read_text(encoding="utf-8") + "Edited.\n", encoding="utf-8")
        return plan

    monkeypatch.setattr(article_identity, "plan_article_ids", plan_then_edit)
    before = _bytes(blueprint)

    with pytest.raises(article_identity.ArticleIdWriteError, match="chapter/result.md: changed while the plan"):
        write_article_ids(blueprint)

    # Only the concurrent edit landed: no article got an ID, the edited one included.
    before["roadmap/chapter/result.md"] += b"Edited.\n"
    assert _bytes(blueprint) == before


def test_write_keeps_an_edit_made_during_writing_and_completes_on_rerun(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    real_replace = article_identity._replace_if_unchanged
    calls: list[Path] = []

    def edit_the_second(path, expected, content, mode):
        calls.append(path)
        if len(calls) == 2:
            path.write_text(path.read_text(encoding="utf-8") + "Edited meanwhile.\n", encoding="utf-8")
        return real_replace(path, expected, content, mode)

    monkeypatch.setattr(article_identity, "_replace_if_unchanged", edit_the_second)

    with pytest.raises(article_identity.ArticleIdWriteError, match="not written") as stopped:
        write_article_ids(blueprint)

    assert len(stopped.value.written) == 1
    assert calls[1].read_text(encoding="utf-8").endswith("Edited meanwhile.\n")
    assert "article_id" not in calls[1].read_text(encoding="utf-8")
    monkeypatch.setattr(article_identity, "_replace_if_unchanged", real_replace)
    assert len(write_article_ids(blueprint)) == 2
    assert plan_article_ids(blueprint).complete
    assert calls[1].read_text(encoding="utf-8").endswith("Edited meanwhile.\n")


def test_write_refuses_an_article_replaced_by_a_symlink(tmp_path: Path, monkeypatch) -> None:
    blueprint = _blueprint(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("---\n---\n\n# Outside\n", encoding="utf-8")
    result = blueprint / "roadmap/chapter/result.md"
    real_plan = article_identity.plan_article_ids

    def plan_then_swap(path):
        plan = real_plan(path)
        if not result.is_symlink():
            result.unlink()
            result.symlink_to(outside)
        return plan

    monkeypatch.setattr(article_identity, "plan_article_ids", plan_then_swap)

    with pytest.raises(article_identity.ArticleIdWriteError, match="chapter/result.md: cannot read article"):
        write_article_ids(blueprint)

    assert outside.read_text(encoding="utf-8") == "---\n---\n\n# Outside\n"


def test_cli_write_reports_each_id_and_leaves_json_to_the_plan(tmp_path: Path, capsys) -> None:
    blueprint = _blueprint(tmp_path)

    assert main(["migrate", "article-ids", str(blueprint), "--write"]) == 0
    out = capsys.readouterr().out
    assert out.count("added article_id af_") == 3
    assert "OK: 3 articles have durable article_id metadata" in out

    assert main(["migrate", "article-ids", str(blueprint), "--write", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["complete"] is True
    assert captured.err == ""

    with pytest.raises(SystemExit) as refused:
        main(["migrate", "article-ids", str(blueprint), "--write", "--check"])
    assert refused.value.code == 2
