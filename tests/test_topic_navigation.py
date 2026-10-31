"""Presentation roles and scope projections must reveal mathematics without rewriting its evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli import dag_viewer
from autoform_cli.graph import Graph, GraphValidationError, Node, load_graph
from autoform_cli.graph_pages import _dependency_heading, write_graph_pages
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode, full_view, project_view, scope_view, scope_views
from autoform_cli.status import derive


def _geometry_graph(tmp_path: Path) -> Graph:
    nodes: dict[str, Node] = {}

    def add(node_id: str, title: str, parent: str | None = None, **facts: object) -> None:
        dependencies = facts.pop("statement_dependencies", ())
        nodes[node_id] = Node(
            node_id, title, tmp_path / f"{node_id}.md", dependencies,
            parent=parent, statement_dependencies=dependencies, **facts,
        )

    add("roadmap", "All mathematics")
    add("Geometry", "Geometry", "roadmap")
    # Deliberately not named MathlibExt, WantedExt or part-01. Only authored
    # roles may make a container transparent, never a filesystem heuristic.
    add("Geometry/reusable", "Reusable library", "Geometry", navigation="collection")
    add("Geometry/reusable/metric", "Distance geometry", "Geometry/reusable", area="Distances")
    add("Geometry/reusable/metric/distance", "Distance multiplicity", "Geometry/reusable/metric", declaration="def")
    add("Geometry/reusable/comparison", "Comparison geometry", "Geometry/reusable", area="Comparison")
    add("Geometry/reusable/comparison/inequality", "Four-point comparison", "Geometry/reusable/comparison", declaration="def")
    add("Geometry/deferred", "Deferred library", "Geometry", navigation="collection")
    add("Geometry/deferred/range", "A pagination range", "Geometry/deferred", navigation="transparent")
    add("Geometry/deferred/range/triangles", "Comparison triangles", "Geometry/deferred/range", area="Comparison")
    add("Geometry/deferred/range/triangles/result", "Triangle bound", "Geometry/deferred/range/triangles",
        declaration="theorem", statement_dependencies=("Geometry/reusable/comparison/inequality",))
    add("Geometry/book", "A geometry textbook", "Geometry", navigation="source")
    add("Geometry/book/curvature", "Curvature theorem", "Geometry/book", declaration="theorem",
        statement_dependencies=("Geometry/reusable/metric/distance",))
    add("Geometry/result", "A direct result", "Geometry", declaration="theorem",
        statement_dependencies=("Geometry/reusable",))
    add("Analysis", "Analysis", "roadmap")
    add("Analysis/base", "Analytic prerequisite", "Analysis", declaration="def",
        statement_dependencies=("Geometry/reusable/comparison/inequality",))
    return Graph(tmp_path, nodes)


def test_topics_replace_library_and_pagination_peers_without_dropping_sources_or_edges(tmp_path: Path) -> None:
    graph = _geometry_graph(tmp_path)
    before = graph.nodes.copy()
    statuses = derive(graph)
    view = scope_view(graph, statuses, "Geometry")
    assert view == scope_views(graph, statuses)["Geometry"]
    assert view.overview
    assert {node.title for node in view.nodes if node.kind == "scope" and not node.navigation} == {
        "Distance geometry", "Comparison geometry", "Comparison triangles",
    }
    assert "Geometry/book/curvature" in view.member_ids
    assert "Geometry/deferred/range/triangles/result" in view.member_ids
    assert "scope:Geometry/deferred/range" not in {node.id for node in view.nodes}
    assert {(edge.source, edge.target, edge.statement_count) for edge in view.edges} == {
        ("scope:Geometry/reusable/comparison", "scope:Geometry/deferred/range/triangles", 1),
        ("scope:Geometry/reusable/metric", "scope:Geometry/book", 1),
        ("scope:Geometry/reusable", "Geometry/result", 1),
        ("scope:Geometry/reusable/comparison", "boundary:Analysis", 1),
    }
    assert graph.nodes == before

    output = tmp_path / "geometry.json"
    dag_viewer.write_payload(output, view, links={node.id: f"pages/{index}.html" for index, node in enumerate(view.nodes)})
    payload = json.loads(output.read_text())
    # The overview draws what replaces the collections, not the collections.
    assert payload["view"]["presentation"] == "dag"
    assert {node["title"] for node in payload["nodes"] if not node["map"]} == {"Reusable library", "Deferred library"}
    assert {node["title"] for node in payload["nodes"] if node["map"]} >= {
        "Distance geometry", "Comparison geometry", "Comparison triangles", "A geometry textbook", "A direct result",
    }
    assert all("layout" not in node for node in payload["nodes"] if not node["map"])
    assert all("layout" in node for node in payload["nodes"] if node["map"])
    assert len(payload["edges"]) == 4  # relations of the collections themselves stay in the payload
    # The rail counts and the isolate grid follow the map, not the payload.
    assert payload["edge_count"] == 3
    assert [node["isolated"] for node in payload["nodes"] if node["id"] == "Geometry/result"] == [True]


def test_an_overview_legend_counts_a_collected_catalog_once(tmp_path: Path) -> None:
    nodes: dict[str, Node] = {}

    def add(node_id: str, parent: str | None = None, **facts: object) -> None:
        nodes[node_id] = Node(node_id, node_id, tmp_path / f"{node_id}.md", (), parent=parent, **facts)

    add("roadmap")
    add("G", "roadmap")
    add("G/lib", "G", navigation="collection")
    add("G/lib/m", "G/lib")
    add("G/lib/m/catalog", "G/lib/m", catalog="module")
    add("G/lib/m/theorem", "G/lib/m", declaration="theorem")
    add("G/r", "G", declaration="theorem")
    graph = Graph(tmp_path, nodes)
    site = tmp_path / "site"
    write_graph_pages(graph, derive(graph), site,
        node_links=lambda _page, node_ids: {node_id: "book.html#" + node_id for node_id in node_ids})
    page = (site / "dependencies/chapters/G.md").read_text()
    assert 'inventory pending</span><span class="bp-legend-count">1<' in page


def _scope(node_id: str, **facts: object) -> ViewNode:
    return ViewNode(id=node_id, title=node_id, kind="scope", members=(), status_counts=(), **facts)


@pytest.mark.parametrize(("view", "expected"), (
    (GraphView(
        kind="chapter", title="Areas dependency map", overview=True,
        nodes=(_scope("scope:a"), _scope("scope:b"), _scope("scope:lib", navigation="collection", area="Library")),
        edges=(ViewEdge("scope:a", "scope:b", 1, 0),),
    ), "dag"),
    (GraphView(
        kind="chapter", title="Wrapper dependency map", overview=True,
        nodes=(_scope("scope:a"), _scope("scope:lib", navigation="collection")),
        edges=(ViewEdge("scope:lib", "scope:a", 1, 0),),
    ), "atlas"),
), ids=("collection-area-beside-topics", "only-edge-on-the-collection"))
def test_an_overview_heading_names_what_its_map_draws(tmp_path: Path, view: GraphView, expected: str) -> None:
    output = tmp_path / "view.json"
    dag_viewer.write_payload(output, view, links={})
    presentation = json.loads(output.read_text())["view"]["presentation"]
    # Drawn, the first is a->b with no area and the second a lone topic.
    assert presentation == expected
    assert _dependency_heading(view).endswith(" dependencies") == (presentation == "dag")


def test_breadcrumbs_skip_collections_and_pagination(tmp_path: Path) -> None:
    graph = _geometry_graph(tmp_path)
    site = tmp_path / "site"
    written = write_graph_pages(graph, derive(graph), site,
        node_links=lambda _page, node_ids: {node_id: "book.html#" + node_id for node_id in node_ids})
    assert all(page.suffix == ".md" for page in written)
    page = site / "dependencies/scopes/Geometry/deferred/range/triangles.json"
    payload = json.loads(page.read_text())
    assert [crumb["label"] for crumb in payload["view"]["breadcrumbs"]] == [
        "Book", "All mathematics", "Geometry", "Comparison triangles",
    ]


@pytest.mark.parametrize("role", ("topic", "collection", "source", "transparent", "module"))
def test_navigation_roles_load_as_presentation_metadata(tmp_path: Path, role: str) -> None:
    roadmap = tmp_path / "roadmap"
    roadmap.mkdir()
    (roadmap / "README.md").write_text(f"---\nnavigation: {role}\n---\n# Geometry\n")
    assert load_graph(tmp_path).nodes["roadmap"].navigation == role


@pytest.mark.parametrize("metadata", (
    "navigation: guess-from-directory",
    "navigation: collection\ndeclaration: theorem",
    "navigation: module\ndeclaration: theorem",
))
def test_invalid_roles_cannot_hide_a_mathematical_declaration(tmp_path: Path, metadata: str) -> None:
    roadmap = tmp_path / "roadmap"
    roadmap.mkdir()
    (roadmap / "README.md").write_text(f"---\n{metadata}\n---\n# Geometry\n")
    with pytest.raises(GraphValidationError, match="navigation"):
        load_graph(tmp_path)


def _overview_payload(tmp_path: Path, nodes: dict[str, Node], scope: str) -> dict:
    graph = Graph(tmp_path, nodes)
    view = scope_view(graph, derive(graph), scope)
    assert view.overview
    output = tmp_path / "view.json"
    dag_viewer.write_payload(output, view, links={})
    return json.loads(output.read_text())


def test_an_overview_of_pagination_parts_of_leaves_maps_the_leaves(tmp_path: Path) -> None:
    nodes: dict[str, Node] = {}

    def add(node_id: str, parent: str | None = None, **facts: object) -> None:
        nodes[node_id] = Node(node_id, node_id, tmp_path / f"{node_id}.md", (), parent=parent, **facts)

    add("roadmap")
    add("roadmap/module", "roadmap")
    for part in ("part-01", "part-02"):
        add(f"roadmap/module/{part}", "roadmap/module", navigation="transparent")
        for index in range(2):
            add(f"roadmap/module/{part}/result-{index}", f"roadmap/module/{part}", declaration="theorem")

    payload = _overview_payload(tmp_path, nodes, "roadmap/module")

    assert sorted(node["id"] for node in payload["nodes"] if node["map"]) == [
        f"roadmap/module/{part}/result-{index}" for part in ("part-01", "part-02") for index in range(2)
    ]
    assert all("layout" in node for node in payload["nodes"] if node["map"])


def test_an_overview_whose_only_child_is_a_source_book_maps_the_book(tmp_path: Path) -> None:
    nodes: dict[str, Node] = {}

    def add(node_id: str, parent: str | None = None, **facts: object) -> None:
        nodes[node_id] = Node(node_id, node_id, tmp_path / f"{node_id}.md", (), parent=parent, **facts)

    add("roadmap")
    add("roadmap/chapter", "roadmap")
    add("roadmap/chapter/book", "roadmap/chapter", navigation="source")
    add("roadmap/chapter/book/result", "roadmap/chapter/book", declaration="theorem")

    payload = _overview_payload(tmp_path, nodes, "roadmap/chapter")

    assert [node["id"] for node in payload["nodes"] if node["map"]] == ["scope:roadmap/chapter/book"]


@pytest.mark.parametrize("role", ("collection", "transparent"))
def test_deep_role_flattening_preserves_topic_boundaries_source_order_and_edges(
    tmp_path: Path, role: str
) -> None:
    nodes: dict[str, Node] = {}

    def add(node_id: str, parent: str | None, **facts: object) -> None:
        dependencies = facts.pop("statement_dependencies", ())
        nodes[node_id] = Node(
            node_id, node_id, tmp_path / f"{node_id}.md", dependencies,
            parent=parent, statement_dependencies=dependencies, **facts,
        )

    add("Math", None)
    add("before", "Math", declaration="def")
    parent = "Math"
    for index in range(1_200):
        node_id = f"wrapper{index:04d}"
        add(node_id, parent, navigation=role)
        parent = node_id
    deepest_wrapper = parent
    for index in range(1_200):
        node_id = f"topic{index:04d}"
        add(node_id, parent)
        parent = node_id
    add("result", parent, declaration="theorem")
    add("source", deepest_wrapper, navigation="source")
    add("source-result", "source", declaration="theorem")
    add("direct-result", deepest_wrapper, declaration="lemma")
    add("after", "Math", declaration="theorem", statement_dependencies=("result", "wrapper0000"))
    graph = Graph(tmp_path, nodes)
    original = nodes.copy()

    view = scope_view(graph, derive(graph), "Math", include_external=False)

    assert [node.id for node in view.nodes] == [
        "before", "scope:topic0000", "scope:source", "direct-result", "after", "scope:wrapper0000",
    ]
    assert view.overview
    assert next(node for node in view.nodes if node.id == "scope:topic0000").members == ("result",)
    assert next(node for node in view.nodes if node.id == "scope:source").navigation == "source"
    assert {(edge.source, edge.target, edge.statement_count, edge.proof_count) for edge in view.edges} == {
        ("scope:topic0000", "after", 1, 0),
        ("scope:wrapper0000", "after", 1, 0),
    }
    assert graph.nodes == original


def _graph(tmp_path: Path) -> Graph:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"

    def node(node_id: str, title: str, parent: str | None, **kwargs) -> Node:
        return Node(id=node_id, title=title, path=roadmap / f"{node_id}.md", dependencies=(), parent=parent, **kwargs)

    proved = {"declaration": "theorem", "statement_formalized": True, "proof_formalized": True}
    nodes = {
        "roadmap": node("roadmap", "Roadmap", None),
        "done": node("done", "Finished chapter", "roadmap"),
        "done/sec": node("done/sec", "Finished section", "done"),
        "done/sec/a": node("done/sec/a", "Proved result", "done/sec", **proved),
        # A note declares nothing and is not marked not ready, so it is not a target.
        "done/note": node("done/note", "Note", "done"),
        "open": node("open", "Open chapter", "roadmap"),
        "open/b": node("open/b", "Proved result", "open", **proved),
        "open/c": node("open/c", "Stated result", "open", declaration="theorem", statement_formalized=True),
    }
    return Graph(blueprint_dir=blueprint, nodes=nodes)


def test_scope_is_complete_only_when_every_target_beneath_it_is_fully_proved(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    statuses = derive(graph)

    project = {node.id: node for node in project_view(graph, statuses).nodes}
    assert project["scope:done"].complete
    assert project["scope:done"].item_count == 1
    assert project["scope:done"].members == ("done/sec/a",)
    assert not project["scope:open"].complete
    assert project["scope:open"].item_count == 2

    nested = {node.id: node for node in scope_view(graph, statuses, "done").nodes}
    assert nested["scope:done/sec"].complete


def test_node_level_views_draw_containers_as_scopes(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    statuses = derive(graph)

    nodes = {node.id: node for node in full_view(graph, statuses).nodes}
    assert nodes["done"].kind == "scope"
    assert nodes["done"].complete
    assert nodes["done"].members == ("done",)
    assert nodes["done"].status_counts == (("fully_proved", 1),)
    assert nodes["open"].kind == "scope" and not nodes["open"].complete
    assert nodes["done/sec/a"].kind == "node"
    assert nodes["done/note"].kind == "node"


def test_open_work_holds_a_scope_open(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"

    def node(node_id: str, parent: str | None, *dependencies: str, **kwargs) -> Node:
        path = blueprint / "roadmap" / f"{node_id}.md"
        return Node(
            id=node_id,
            title=node_id,
            path=path,
            dependencies=dependencies,
            statement_dependencies=dependencies,
            parent=parent,
            **kwargs,
        )

    proved = {"declaration": "theorem", "statement_formalized": True, "proof_formalized": True}
    nodes = {
        "roadmap": node("roadmap", None),
        "partial": node("partial", "roadmap"),
        "partial/a": node("partial/a", "partial", **proved),
        "partial/later": node("partial/later", "partial", not_ready=True),
        "notes": node("notes", "roadmap"),
        "notes/n": node("notes/n", "notes"),
        "base": node("base", "roadmap"),
        "base/d": node("base/d", "base", declaration="def"),
        "lib": node("lib", "roadmap"),
        "lib/m": node("lib/m", "lib", "base/d", declaration="theorem", mathlib=True),
    }
    graph = Graph(blueprint_dir=blueprint, nodes=nodes)
    project = {node.id: node for node in project_view(graph, derive(graph)).nodes}
    # A section still marked not ready, a chapter of notes, and a Mathlib
    # result whose prerequisite is open each leave their scope unfinished.
    assert not project["scope:partial"].complete
    assert project["scope:partial"].status_counts == (("fully_proved", 1), ("not_ready", 1))
    assert not project["scope:notes"].complete
    assert not project["scope:lib"].complete
