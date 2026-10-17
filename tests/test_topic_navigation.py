"""Presentation roles must reveal mathematics without rewriting its evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli import dag_viewer
from autoform_cli.graph import Graph, GraphValidationError, Node, load_graph
from autoform_cli.graph_pages import NAVIGATION_INDEX, write_graph_pages
from autoform_cli.graph_views import scope_view, scope_views
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
    assert view.overview and view.total_count == 5
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
    assert payload["view"]["presentation"] == "atlas"
    assert {node["title"] for node in payload["nodes"] if node["map"]} == {
        "Distance geometry", "Comparison geometry", "Comparison triangles",
    }
    assert all("layout" not in node for node in payload["nodes"] if not node["map"])
    assert len(payload["edges"]) == 4  # provenance and external context remain inspectable


def test_shared_outline_skips_collections_but_keeps_source_book_routes_and_canonical_result_contexts(tmp_path: Path) -> None:
    graph = _geometry_graph(tmp_path)
    site = tmp_path / "site"
    written = write_graph_pages(graph, derive(graph), site,
        node_links=lambda _page, node_ids: {node_id: "book.html#" + node_id for node_id in node_ids})
    assert all(page.suffix == ".md" for page in written)
    assert (site / NAVIGATION_INDEX).is_file()
    index = json.loads((site / NAVIGATION_INDEX).read_text())
    entries = {node["id"]: node for node in index["nodes"]}
    assert entries["Geometry/reusable/metric"]["parent"] == "Geometry"
    assert entries["Geometry/deferred/range/triangles"]["parent"] == "Geometry"
    assert entries["Geometry/book"]["source"]
    assert entries["Geometry/reusable"]["navigation"] == "collection"
    assert not entries["Geometry/reusable/metric"]["source"]
    assert "Geometry/deferred/range" not in entries
    assert entries["Geometry/reusable/metric"]["url"].endswith("/Geometry/reusable/metric.html")
    page = site / "dependencies/scopes/Geometry/deferred/range/triangles.json"
    payload = json.loads(page.read_text())
    assert [crumb["label"] for crumb in payload["view"]["breadcrumbs"]] == [
        "Book", "All mathematics", "Geometry", "Comparison triangles",
    ]
    search = json.loads((site / "dependencies/index.json").read_text())
    assert all(node["result"] for node in search["nodes"] if node["kind"] == "node")


@pytest.mark.parametrize("role", ("topic", "collection", "source", "transparent"))
def test_navigation_roles_load_as_presentation_metadata(tmp_path: Path, role: str) -> None:
    roadmap = tmp_path / "roadmap"
    roadmap.mkdir()
    (roadmap / "README.md").write_text(f"---\nnavigation: {role}\n---\n# Geometry\n")
    assert load_graph(tmp_path).nodes["roadmap"].navigation == role


@pytest.mark.parametrize("metadata", ("navigation: guess-from-directory", "navigation: collection\ndeclaration: theorem"))
def test_invalid_roles_cannot_hide_a_mathematical_declaration(tmp_path: Path, metadata: str) -> None:
    roadmap = tmp_path / "roadmap"
    roadmap.mkdir()
    (roadmap / "README.md").write_text(f"---\n{metadata}\n---\n# Geometry\n")
    with pytest.raises(GraphValidationError, match="navigation"):
        load_graph(tmp_path)
