from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli import graph_views
from autoform_cli.graph import Graph, Node
from autoform_cli.graph_views import chapter_view, focus_view, focus_views, full_view, project_view, scope_view
from autoform_cli.status import derive


def _graph(tmp_path: Path) -> Graph:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    for group, title in (("a", "Foundations"), ("b", "Main chapter"), ("c", "Applications")):
        page = roadmap / group / "README.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"---\nkind: roadmap\n---\n\n# {title}\n", encoding="utf-8")

    def node(node_id: str, title: str, **kwargs) -> Node:
        return Node(
            id=node_id,
            title=title,
            path=roadmap / f"{node_id}.md",
            dependencies=kwargs.pop("dependencies", ()),
            **kwargs,
        )

    nodes = {
        "a/base": node(
            "a/base",
            "Base object",
            declaration="def",
            statement_formalized=True,
        ),
        "b/bridge": node(
            "b/bridge",
            "Bridge lemma",
            dependencies=("a/base",),
            statement_dependencies=("a/base",),
            declaration="lemma",
        ),
        "b/top": node(
            "b/top",
            "Main theorem",
            dependencies=("b/bridge", "a/base"),
            statement_dependencies=("b/bridge",),
            proof_dependencies=("a/base",),
            declaration="theorem",
        ),
        "c/use": node(
            "c/use",
            "Application",
            dependencies=("b/top",),
            statement_dependencies=("b/top",),
            declaration="theorem",
        ),
    }
    return Graph(blueprint_dir=blueprint, nodes=nodes)


def test_project_view_collapses_chapters_without_flattening_statuses(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    view = project_view(graph, derive(graph))

    assert view.kind == "project"
    assert [(node.id, node.title, node.members) for node in view.nodes] == [
        ("scope:a", "Foundations", ("a/base",)),
        ("scope:b", "Main chapter", ("b/bridge", "b/top")),
        ("scope:c", "Applications", ("c/use",)),
    ]
    main = view.nodes[1]
    assert main.status_counts == (("can_state", 1), ("planned", 1))
    assert [(edge.source, edge.target, edge.statement_count, edge.proof_count) for edge in view.edges] == [
        ("scope:a", "scope:b", 1, 1),
        ("scope:b", "scope:c", 1, 0),
    ]


def test_chapter_view_keeps_external_relations_as_boundaries(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    view = chapter_view(graph, derive(graph), "b")

    assert view.kind == "chapter"
    assert view.scope == "b"
    assert {node.id for node in view.nodes} == {
        "b/bridge",
        "b/top",
        "boundary:a",
        "boundary:c",
    }


@pytest.mark.parametrize(
    ("source", "target"),
    (("roadmap", "chapter/result"), ("chapter/result", "roadmap")),
)
def test_roadmap_root_dependencies_have_nodes_in_every_view(
    tmp_path: Path,
    source: str,
    target: str,
) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    nodes = {
        "roadmap": Node("roadmap", "Roadmap", roadmap / "README.md", (), parent=None),
        "chapter": Node(
            "chapter",
            "Chapter",
            roadmap / "chapter/README.md",
            (),
            parent="roadmap",
            depth=1,
        ),
        "chapter/result": Node(
            "chapter/result",
            "Result",
            roadmap / "chapter/result.md",
            (source,),
            statement_dependencies=(source,),
            parent="chapter",
            depth=2,
            declaration="theorem",
        ),
    }
    if target == "roadmap":
        nodes["roadmap"] = Node(
            "roadmap",
            "Roadmap",
            roadmap / "README.md",
            (source,),
            statement_dependencies=(source,),
            parent=None,
        )
        nodes["chapter/result"] = Node(
            "chapter/result",
            "Result",
            roadmap / "chapter/result.md",
            (),
            parent="chapter",
            depth=2,
            declaration="theorem",
        )
    graph = Graph(tmp_path / "blueprint", nodes)
    statuses = derive(graph)

    project = project_view(graph, statuses)
    assert {node.id for node in project.nodes} == {"scope:roadmap", "scope:chapter"}
    assert next(node for node in project.nodes if node.id == "scope:roadmap").members == ()
    assert {(edge.source, edge.target) for edge in project.edges} == {
        (
            "scope:roadmap" if source == "roadmap" else "scope:chapter",
            "scope:roadmap" if target == "roadmap" else "scope:chapter",
        )
    }
    assert {endpoint for edge in project.edges for endpoint in (edge.source, edge.target)} <= {
        node.id for node in project.nodes
    }

    chapter = chapter_view(graph, statuses, "chapter")
    boundary = next(node for node in chapter.nodes if node.id == "boundary:roadmap")
    assert boundary.members == ("roadmap",)
    assert {endpoint for edge in chapter.edges for endpoint in (edge.source, edge.target)} <= {
        node.id for node in chapter.nodes
    }

    complete = full_view(graph, statuses)
    assert set(complete.member_ids) == set(graph.nodes)
    assert {(edge.source, edge.target) for edge in complete.edges} == {(source, target)}


def test_scope_view_collapses_nested_articles_and_rolls_up_dependencies(tmp_path: Path) -> None:
    roadmap = tmp_path / "blueprint" / "roadmap"
    nodes = {
        "roadmap": Node("roadmap", "Book", roadmap / "README.md", (), parent=None),
        "chapter": Node("chapter", "Chapter", roadmap / "chapter/README.md", (), parent="roadmap", depth=1),
        "section": Node("section", "Section", roadmap / "chapter/section/README.md", (), parent="chapter", depth=2),
        "section/base": Node(
            "section/base",
            "Base",
            roadmap / "chapter/section/base.md",
            (),
            parent="section",
            depth=3,
            declaration="def",
            statement_formalized=True,
        ),
        "result": Node(
            "result",
            "Result",
            roadmap / "chapter/result.md",
            ("section/base",),
            statement_dependencies=("section/base",),
            parent="chapter",
            depth=2,
            declaration="theorem",
        ),
    }
    graph = Graph(tmp_path / "blueprint", nodes)
    view = scope_view(graph, derive(graph), "chapter")

    assert [(node.id, node.kind, node.members) for node in view.nodes] == [
        ("scope:section", "scope", ("section/base",)),
        ("result", "node", ("result",)),
    ]
    assert [(edge.source, edge.target) for edge in view.edges] == [("scope:section", "result")]


def test_focus_view_uses_graph_distance_independently_of_chapter_scope(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    statuses = derive(graph)

    focused = focus_view(graph, statuses, "b/top", radius=1)
    assert focused.focus == "b/top"
    assert focused.radius == 1
    assert set(focused.member_ids) == {"a/base", "b/bridge", "b/top", "c/use"}
    assert [node.id for node in focused.nodes if node.focus] == ["b/top"]

    node_only = focus_view(graph, statuses, "b/top", radius=0)
    assert node_only.member_ids == ("b/top",)
    assert node_only.edges == ()

    with pytest.raises(ValueError, match="non-negative"):
        focus_view(graph, statuses, "b/top", radius=-1)


def test_bulk_focus_views_share_graph_wide_indexes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _graph(tmp_path)
    statuses = derive(graph)
    original = graph_views.topological_order
    calls = 0

    def counted(candidate: Graph) -> list[str]:
        nonlocal calls
        calls += 1
        return original(candidate)

    monkeypatch.setattr(graph_views, "topological_order", counted)
    views = focus_views(graph, statuses)

    assert calls == 1
    assert set(views) == set(graph.nodes)
    assert views["b/top"] == focus_view(graph, statuses, "b/top")


def test_full_view_preserves_every_fine_node_and_edge(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    view = full_view(graph, derive(graph))

    assert view.kind == "full"
    assert set(view.member_ids) == set(graph.nodes)
    assert sum(edge.dependency_count for edge in view.edges) == graph.edge_count
