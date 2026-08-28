from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.graph import Graph, Node
from autoform_cli.graph_views import project_view
from autoform_cli.render import render_site
from autoform_cli.status import derive


def _render(blueprint: Path, output: Path) -> None:
    coverage = blueprint / "coverage/README.md"
    coverage.parent.mkdir(exist_ok=True)
    coverage.write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
        "| Project scope | MAPPED | Source audit pending |\n",
        encoding="utf-8",
    )
    render_site(blueprint, output)


def test_project_view_maps_top_level_container_dependencies_to_its_chapter(tmp_path: Path) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    nodes = {
        "roadmap": Node("roadmap", "Book", roadmap / "README.md", (), parent=None),
        "a": Node("a", "A", roadmap / "a/README.md", (), parent="roadmap", depth=1),
        "b": Node("b", "B", roadmap / "b/README.md", (), parent="roadmap", depth=1),
        "a/result": Node(
            "a/result",
            "Result",
            roadmap / "a/result.md",
            ("b",),
            statement_dependencies=("b",),
            parent="a",
            depth=2,
            declaration="theorem",
        ),
        "b/base": Node(
            "b/base",
            "Base",
            roadmap / "b/base.md",
            (),
            parent="b",
            depth=2,
            declaration="def",
        ),
    }
    graph = Graph(tmp_path / "blueprint", nodes)

    view = project_view(graph, derive(graph))

    assert [(edge.source, edge.target) for edge in view.edges] == [("scope:b", "scope:a")]


@pytest.mark.parametrize("root_depends_on_chapter", (False, True))
def test_render_publishes_dependencies_between_the_roadmap_root_and_a_chapter(
    tmp_path: Path,
    root_depends_on_chapter: bool,
) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    chapter = roadmap / "chapter"
    chapter.mkdir(parents=True)
    (blueprint / "README.md").write_text(
        "# Book\n\n- [Roadmap](roadmap/README.md)\n- [Chapter](roadmap/chapter/README.md)\n",
        encoding="utf-8",
    )
    root_dependency = "\n## Depends on\n\n- [Chapter result](chapter/result.md)\n" if root_depends_on_chapter else ""
    (roadmap / "README.md").write_text(
        "# Root result\n" + root_dependency,
        encoding="utf-8",
    )
    chapter_dependency = "" if root_depends_on_chapter else "\n## Depends on\n\n- [Root result](../README.md)\n"
    (chapter / "README.md").write_text(
        "# Chapter\n\n- [Chapter result](result.md)\n",
        encoding="utf-8",
    )
    (chapter / "result.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# Chapter result\n" + chapter_dependency,
        encoding="utf-8",
    )

    output = tmp_path / "site-src"
    _render(blueprint, output)

    project_map = (output / "dependencies.md").read_text(encoding="utf-8")
    chapter_map = (output / "dependencies/chapters/chapter.md").read_text(encoding="utf-8")
    full_map = (output / "dependencies/full.md").read_text(encoding="utf-8")
    assert "1 item across 1 chapter" in project_map
    assert '"../../dependencies.html"' in chapter_map
    assert "External chapter: Root result" in chapter_map
    assert "Root result" in full_map
    assert "Chapter result" in full_map


def test_nested_container_keeps_its_own_narrative_and_statements(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    section = roadmap / "chapter" / "section"
    section.mkdir(parents=True)
    (blueprint / "README.md").write_text("# Book\n\n- [Chapter](roadmap/chapter/README.md)\n", encoding="utf-8")
    (roadmap / "README.md").write_text("# Roadmap\n\n- [Chapter](chapter/README.md)\n", encoding="utf-8")
    (roadmap / "chapter" / "README.md").write_text(
        "# Chapter\n\nChapter prose.\n\n- [Section](section/README.md)\n", encoding="utf-8"
    )
    (section / "README.md").write_text(
        "# Section\n\nSection prose.\n\n## Result\n\n- [Local result](result.md)\n",
        encoding="utf-8",
    )
    (section / "result.md").write_text(
        "---\ndeclaration: theorem\n---\n\n"
        "# Local result\n\nA local statement.\n\n## Depends on\n\nNo prerequisites.\n",
        encoding="utf-8",
    )

    output = tmp_path / "site-src"
    _render(blueprint, output)

    chapter = (output / "roadmap/chapter/README.md").read_text(encoding="utf-8")
    rendered_section = (output / "roadmap/chapter/section/README.md").read_text(encoding="utf-8")
    assert "Chapter prose." in chapter
    assert 'id="section-result"' not in chapter
    assert "Section prose." in rendered_section
    assert 'id="result"' in rendered_section
    assert rendered_section.index("## Result") < rendered_section.index('id="result"')


def test_prose_only_leaf_article_remains_a_book_page(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    (blueprint / "README.md").write_text(
        "# Book\n\n- [Roadmap](roadmap/README.md)\n- [Epilogue](roadmap/epilogue.md)\n",
        encoding="utf-8",
    )
    (roadmap / "README.md").write_text("# Roadmap\n", encoding="utf-8")
    (roadmap / "epilogue.md").write_text("# Epilogue\n\nClosing prose.\n", encoding="utf-8")

    output = tmp_path / "site-src"
    _render(blueprint, output)

    epilogue = (output / "roadmap/epilogue.md").read_text(encoding="utf-8")
    assert "Closing prose." in epilogue
    assert "bp-book-nav-previous" in epilogue


def test_same_named_leaf_slots_are_scoped_to_their_container(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    for chapter in ("a", "b"):
        directory = roadmap / chapter
        directory.mkdir(parents=True)
        (directory / "README.md").write_text(
            f"# Chapter {chapter.upper()}\n\n## Result\n\n- [Result](result.md)\n",
            encoding="utf-8",
        )
        (directory / "result.md").write_text(
            "---\ndeclaration: theorem\n---\n\n"
            f"# Result {chapter.upper()}\n\nStatement {chapter.upper()}.\n\n"
            "## Depends on\n\nNo prerequisites.\n",
            encoding="utf-8",
        )
    (blueprint / "README.md").write_text(
        "# Book\n\n- [A](roadmap/a/README.md)\n- [B](roadmap/b/README.md)\n",
        encoding="utf-8",
    )
    (roadmap / "README.md").write_text("# Roadmap\n\n- [A](a/README.md)\n- [B](b/README.md)\n", encoding="utf-8")

    output = tmp_path / "site-src"
    _render(blueprint, output)

    for chapter in ("a", "b"):
        rendered = (output / f"roadmap/{chapter}/README.md").read_text(encoding="utf-8")
        assert f"Statement {chapter.upper()}." in rendered
        assert "Additional formalization targets" not in rendered


def test_render_preserves_fragment_on_container_article_link(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    chapter = roadmap / "chapter"
    chapter.mkdir(parents=True)
    (blueprint / "README.md").write_text("# Book\n\n[Details](roadmap/chapter/README.md#details)\n", encoding="utf-8")
    (roadmap / "README.md").write_text("# Roadmap\n", encoding="utf-8")
    (chapter / "README.md").write_text(
        "# Chapter\n\n[Jump](README.md#details)\n\n## Details\n\nText.\n", encoding="utf-8"
    )
    (chapter / "result.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# Result\n\nStatement.\n\n## Depends on\n\nNo prerequisites.\n",
        encoding="utf-8",
    )

    output = tmp_path / "site-src"
    _render(blueprint, output)

    rendered = (output / "README.md").read_text(encoding="utf-8")
    rendered_chapter = (output / "roadmap/chapter/README.md").read_text(encoding="utf-8")
    assert "roadmap/chapter/README.md#details" in rendered
    assert "[Jump](#details)" in rendered_chapter
    assert "##details" not in rendered_chapter
