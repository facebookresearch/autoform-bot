from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

import pytest

from autoform_cli import article_identity
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


def test_proposed_ids_are_deterministic_and_name_both_path_and_source() -> None:
    source = hashlib.sha256(b"# Result\n").hexdigest()
    other = hashlib.sha256(b"# Other\n").hexdigest()
    proposed = article_identity._proposed_id("chapter/result", source)

    assert proposed == article_identity._proposed_id("chapter/result", source)
    assert re.fullmatch(r"af_[0-9a-f]{24}", proposed)
    # A path as long as the first, so only the path itself tells them apart.
    assert proposed != article_identity._proposed_id("chapter/others", source)
    assert proposed != article_identity._proposed_id("chapter/result", other)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reading_refuses_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    article = tmp_path / "article.md"
    article.symlink_to(outside)

    with pytest.raises(OSError):
        article_identity._read_regular(article)


@pytest.mark.parametrize("fchmod", [True, False], ids=["fchmod", "no-fchmod"])
def test_a_replacement_keeps_the_file_mode(tmp_path: Path, monkeypatch, fchmod: bool) -> None:
    if not fchmod:  # as on Windows before Python 3.13
        monkeypatch.delattr(os, "fchmod", raising=False)
    article = tmp_path / "article.md"
    article.write_text("# Result\n", encoding="utf-8")
    article.chmod(0o640)
    _, mode = article_identity._read_regular(article)

    article_identity._replace_if_unchanged(article, _sha256(article), b"# Result, edited\n", mode)

    assert article.read_bytes() == b"# Result, edited\n"
    assert stat.S_IMODE(article.stat().st_mode) == 0o640


def test_a_replacement_refuses_a_file_changed_since_it_was_read(tmp_path: Path) -> None:
    article = tmp_path / "article.md"
    article.write_text("# Result\n", encoding="utf-8")
    expected = _sha256(article)
    article.write_text("# Result\nEdited meanwhile.\n", encoding="utf-8")

    with pytest.raises(OSError, match="changed while the plan was applied"):
        article_identity._replace_if_unchanged(article, expected, b"# Replaced\n", 0o644)

    assert article.read_text(encoding="utf-8") == "# Result\nEdited meanwhile.\n"
    assert [path.name for path in tmp_path.iterdir()] == ["article.md"]


def test_a_replacement_keeps_an_edit_made_while_the_new_file_is_flushed(tmp_path: Path, monkeypatch) -> None:
    article = tmp_path / "article.md"
    article.write_text("# Result\n", encoding="utf-8")
    expected = _sha256(article)
    fsync = os.fsync

    def edit_then_fsync(descriptor: int) -> None:
        article.write_text("# Result\nEdited meanwhile.\n", encoding="utf-8")
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", edit_then_fsync)

    with pytest.raises(OSError, match="changed while the plan was applied"):
        article_identity._replace_if_unchanged(article, expected, b"# Replaced\n", 0o644)

    assert article.read_text(encoding="utf-8") == "# Result\nEdited meanwhile.\n"
    assert [path.name for path in tmp_path.iterdir()] == ["article.md"]
