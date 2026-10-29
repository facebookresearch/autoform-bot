from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli import graph_pages
from autoform_cli.graph import Graph, Node, load_graph
from autoform_cli.graph_views import project_view
from autoform_cli.render import render_site
from autoform_cli.status import derive
from tests.test_integrated_publication import _decoded_relations


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

    project_page = (output / "dependencies.md").read_text(encoding="utf-8")
    chapter_page = (output / "dependencies/chapters/chapter.md").read_text(encoding="utf-8")
    full_page = (output / "dependencies/full.md").read_text(encoding="utf-8")
    assert 'data-page-href="roadmap/index.html"' in project_page
    assert '<a href="roadmap/index.html">Continue to the mathematical page</a>' in project_page
    project_payload = json.loads((output / "dependencies.json").read_text(encoding="utf-8"))
    assert project_payload["schema"] == "autoform-dag-view/v2"
    assert sum(node["member_count"] for node in project_payload["nodes"]) == 1
    source, target = ("scope:chapter", "scope:roadmap") if root_depends_on_chapter else ("scope:roadmap", "scope:chapter")
    assert project_payload["edges"] == [{"source": source, "target": target, "statement": 1, "proof": 0}]
    assert 'class="bp-dag-viewer"' in chapter_page
    chapter_payload = json.loads(
        (output / "dependencies/chapters/chapter.json").read_text(encoding="utf-8")
    )
    boundary = next(node for node in chapter_payload["nodes"] if node["kind"] == "boundary")
    # It stands for what the project's Top level card does.
    assert boundary["title"] == "Top level"
    assert boundary["url"] == "../../roadmap/index.html"
    assert 'data-page-href="../../roadmap/chapter/index.html"' in chapter_page
    assert 'class="bp-dag-viewer"' in full_page
    full_payload = json.loads((output / "dependencies/full.json").read_text(encoding="utf-8"))
    assert {node["title"] for node in full_payload["nodes"]} == {
        "Top level",
        "Chapter",
    }
    search_index = json.loads((output / "dependencies/index.json").read_text(encoding="utf-8"))
    assert {node["title"] for node in search_index["nodes"]} == {
        "Root result",
        "Chapter",
        "Chapter result",
    }
    leaf = "chapter/result"
    assert _decoded_relations(search_index) == [[leaf, "roadmap", 1, 0] if root_depends_on_chapter else ["roadmap", leaf, 1, 0]]
    records = {node["id"]: node for node in search_index["nodes"]}
    assert records[leaf]["route"] == "../roadmap/chapter/index.html#result"
    assert records[leaf]["result"] is True and records["roadmap"]["result"] is False
    canonical = (output / "roadmap/chapter/README.md").read_text(encoding="utf-8")
    assert 'class="bp-reading-source"' in canonical and 'data-autoform-node-id="chapter/result"' in canonical


def test_a_boundary_to_the_root_opens_the_page_that_draws_its_articles(tmp_path: Path) -> None:
    """The project page collapses the root's own articles into its Top level card.

    So a boundary to the root, from a chapter or a nested section, opens the
    canonical mathematical contents and carries the card's title. The card
    keeps the root's summary and area off, since those describe the project.
    """
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    section = roadmap / "chapter/section"
    section.mkdir(parents=True)
    (blueprint / "README.md").write_text("# Book\n\n- [Roadmap](roadmap/README.md)\n", encoding="utf-8")
    (roadmap / "README.md").write_text(
        "---\narea: Analysis\n---\n\n# Roadmap\n\nThe whole project.\n\n"
        "- [Base](base.md)\n- [Chapter](chapter/README.md)\n",
        encoding="utf-8",
    )
    (roadmap / "base.md").write_text("---\ndeclaration: def\n---\n\n# Base\n", encoding="utf-8")
    (roadmap / "chapter/README.md").write_text(
        "---\narea: Algebra\n---\n\n# Chapter\n\nChapter prose.\n\n"
        "- [Result](result.md)\n- [Section](section/README.md)\n",
        encoding="utf-8",
    )
    (section / "README.md").write_text("# Section\n\n- [Deep](deep.md)\n", encoding="utf-8")
    for page, base in ((roadmap / "chapter/result.md", "../base.md"), (section / "deep.md", "../../base.md")):
        page.write_text(
            f"---\ndeclaration: theorem\n---\n\n# {page.stem.title()}\n\n## Depends on\n\n- [Base]({base})\n",
            encoding="utf-8",
        )
    output = tmp_path / "site-src"

    _render(blueprint, output)

    for payload, url in (
        ("chapters/chapter.json", "../../roadmap/index.html"),
        ("scopes/chapter/section.json", "../../../roadmap/index.html"),
    ):
        nodes = json.loads((output / "dependencies" / payload).read_text(encoding="utf-8"))["nodes"]
        boundary = next(node for node in nodes if node["kind"] == "boundary")
        assert (boundary["id"], boundary["title"], boundary["url"]) == ("boundary:roadmap", "Top level", url)
    assert (output / "dependencies/chapters/roadmap.md").is_file()
    project = json.loads((output / "dependencies.json").read_text(encoding="utf-8"))
    cards = {node["id"]: (node["title"], node["summary"], node["area"]) for node in project["nodes"]}
    assert cards == {
        "scope:roadmap": ("Top level", None, None),
        "scope:chapter": ("Chapter", "Chapter prose.", "Algebra"),
    }
    canonical = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert "The whole project." in canonical and 'data-autoform-node-id="base"' in canonical
    assert 'class="bp-reading-source"' in canonical and 'class="bp-dag-viewer"' in canonical


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


def test_scope_payloads_count_targets_not_notes(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    chapter = roadmap / "chapter"
    pair = roadmap / "pair"
    notes = roadmap / "notes"
    for directory in (chapter, pair, notes):
        directory.mkdir(parents=True)
    (blueprint / "README.md").write_text("# Book\n\n- [Chapter](roadmap/chapter/README.md)\n", encoding="utf-8")
    (roadmap / "README.md").write_text(
        "# Roadmap\n\n- [Chapter](chapter/README.md)\n- [Pair](pair/README.md)\n- [Notes](notes/README.md)\n",
        encoding="utf-8",
    )
    (chapter / "README.md").write_text("# Chapter\n\n- [Result](result.md)\n- [Note](note.md)\n", encoding="utf-8")
    (chapter / "result.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# Result\n\nStatement.\n\n## Depends on\n\nNo prerequisites.\n",
        encoding="utf-8",
    )
    # The note declares nothing, so it is not a formalization target.
    (chapter / "note.md").write_text("# Note\n\nProse only.\n", encoding="utf-8")
    (pair / "README.md").write_text("# Pair\n\n- [First](first.md)\n- [Later](later.md)\n", encoding="utf-8")
    (pair / "first.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# First\n\nStatement.\n\n## Depends on\n\nNo prerequisites.\n",
        encoding="utf-8",
    )
    # A leaf marked not ready declares nothing yet, but it is still a target.
    (pair / "later.md").write_text("---\nnot_ready: true\n---\n\n# Later\n\nDeferred.\n", encoding="utf-8")
    (notes / "README.md").write_text("# Notes\n\n- [Aside](aside.md)\n", encoding="utf-8")
    (notes / "aside.md").write_text("# Aside\n\nProse only.\n", encoding="utf-8")

    output = tmp_path / "site-src"
    _render(blueprint, output)

    project_map = (output / "dependencies.md").read_text(encoding="utf-8")
    chapter_map = (output / "dependencies/chapters/chapter.md").read_text(encoding="utf-8")
    pair_map = (output / "dependencies/chapters/pair.md").read_text(encoding="utf-8")
    notes_map = (output / "dependencies/chapters/notes.md").read_text(encoding="utf-8")
    project = json.loads((output / "dependencies.json").read_text(encoding="utf-8"))
    counts = {node["id"]: (node["member_count"], node["status_counts"]) for node in project["nodes"]}
    assert counts == {
        "scope:chapter": (1, [["can_state", 1]]),
        "scope:pair": (2, [["can_state", 1], ["not_ready", 1]]),
        "scope:notes": (0, []),
    }
    assert sum(count for count, _states in counts.values()) == 3
    for alias, href in (
        (project_map, "roadmap/index.html"),
        (chapter_map, "../../roadmap/chapter/index.html"),
        (pair_map, "../../roadmap/pair/index.html"),
        (notes_map, "../../roadmap/notes/index.html"),
    ):
        assert f'data-page-href="{href}"' in alias
        assert f'<a href="{href}">Continue to the mathematical page</a>' in alias
    index = json.loads((output / "dependencies/index.json").read_text(encoding="utf-8"))
    records = {node["id"]: node for node in index["nodes"]}
    assert records["chapter/note"]["result"] is False
    assert records["notes/aside"]["result"] is False and records["pair/later"]["result"] is False
    assert records["chapter/result"]["result"] is True and records["pair/first"]["result"] is True


def test_dependency_map_legends_leave_out_containers(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    chapter = roadmap / "chapter"
    chapter.mkdir(parents=True)
    (blueprint / "README.md").write_text("# Book\n\n- [Chapter](roadmap/chapter/README.md)\n", encoding="utf-8")
    (roadmap / "README.md").write_text("# Roadmap\n\n- [Chapter](chapter/README.md)\n", encoding="utf-8")
    (chapter / "README.md").write_text("# Chapter\n\n- [Result](result.md)\n", encoding="utf-8")
    (chapter / "result.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# Result\n\nStatement.\n\n## Depends on\n\nNo prerequisites.\n",
        encoding="utf-8",
    )

    output = tmp_path / "site-src"
    _render(blueprint, output)

    # Alias shells hold no second legend. Scope counts still include only the
    # theorem, and the standalone API preserves its no-JS legend.
    for payload in ("dependencies.json", "dependencies/full.json"):
        data = json.loads((output / payload).read_text(encoding="utf-8"))
        assert sum(node["member_count"] for node in data["nodes"]) == 1
        assert [pair for node in data["nodes"] for pair in node["status_counts"]] == [["can_state", 1]]
    for page in ("dependencies.md", "dependencies/full.md"):
        rendered = (output / page).read_text(encoding="utf-8")
        assert 'class="bp-legend-count"' not in rendered
        assert "Continue to the mathematical page" in rendered
    standalone = tmp_path / "standalone"
    graph = load_graph(blueprint)
    graph_pages.write_graph_pages(graph, derive(graph), standalone, node_links=lambda _page, _ids: {})
    for page in ("dependencies.md", "dependencies/full.md"):
        rendered = (standalone / page).read_text(encoding="utf-8")
        assert rendered.count('class="bp-legend-count"') == 1
        assert '<span class="bp-legend-count">1</span>' in rendered
