from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import pytest

from autoform_cli import dag_viewer, graph_pages
from autoform_cli.graph import Graph, Node
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode, full_view
from autoform_cli.status import derive


def _view(*edges: ViewEdge) -> GraphView:
    return GraphView(
        kind="full",
        title="Search connections",
        nodes=tuple(
            ViewNode(node_id, title, "node", (node_id,), (("planned", 1),))
            for node_id, title in (("z/base", "Base object"), ("a/top", "Main theorem"), ("m/use", "Application"))
        ),
        edges=edges,
    )


def _write(tmp_path: Path, view: GraphView, **kwargs) -> dict:
    output = tmp_path / "index.json"
    dag_viewer.write_search_index(
        output,
        view,
        context_links={node.id: f"chapters/context.html#node={node.id}" for node in view.nodes},
        **kwargs,
    )
    return json.loads(output.read_text(encoding="utf-8"))


def test_search_relations_index_node_order_and_preserve_typed_counts(tmp_path: Path) -> None:
    view = _view(
        ViewEdge("a/top", "m/use", proof_count=2),
        ViewEdge("z/base", "a/top", statement_count=3),
        ViewEdge("a/top", "m/use", statement_count=1),
        ViewEdge("z/base", "m/use"),
    )
    payload = _write(tmp_path, view)

    assert payload["schema"] == "autoform-dag-search/v1"
    assert [node["id"] for node in payload["nodes"]] == ["z/base", "a/top", "m/use"]
    assert payload["relations"] == [[0, 1, 3, 0], [1, 2, 1, 2]]
    # Keeping original node order is essential: the compact endpoints refer
    # to this array, not sorted ids or positions in a projection's layout.
    assert payload["nodes"][0] == {
        "id": "z/base",
        "kind": "node",
        "result": False,
        "status": "planned",
        "title": "Base object",
        "url": "chapters/context.html#node=z/base",
    }


def test_search_relation_output_is_deterministic_for_edge_order(tmp_path: Path) -> None:
    view = _view(
        ViewEdge("a/top", "m/use", proof_count=1),
        ViewEdge("z/base", "m/use", statement_count=1),
        ViewEdge("z/base", "a/top", statement_count=1),
    )
    _write(tmp_path, view)
    first = (tmp_path / "index.json").read_bytes()
    _write(tmp_path, replace(view, edges=tuple(reversed(view.edges))))

    assert (tmp_path / "index.json").read_bytes() == first


def test_search_relations_distinguish_an_edgeless_index_from_legacy_data(tmp_path: Path) -> None:
    payload = _write(tmp_path, _view())

    assert payload["relations"] == []
    assert set(payload) == {"nodes", "relations", "schema"}
    assert not any("data_url" in node or "article_url" in node for node in payload["nodes"])


@pytest.mark.parametrize(
    "edge",
    [ViewEdge("unknown", "a/top", statement_count=1), ViewEdge("z/base", "unknown", proof_count=1)],
)
def test_search_relations_fail_closed_for_unknown_endpoints(tmp_path: Path, edge: ViewEdge) -> None:
    output = tmp_path / "index.json"
    output.write_text("Existing index.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown node"):
        _write(tmp_path, _view(edge))

    assert output.read_text(encoding="utf-8") == "Existing index.\n"


def test_search_relations_fail_closed_for_duplicate_node_ids(tmp_path: Path) -> None:
    view = _view()
    view = replace(view, nodes=(*view.nodes, view.nodes[0]))

    with pytest.raises(ValueError, match="duplicate node ids"):
        _write(tmp_path, view)

    assert not (tmp_path / "index.json").exists()


@pytest.mark.parametrize("count", [-1, 1.5, True])
def test_search_relations_reject_invalid_counts(tmp_path: Path, count) -> None:
    with pytest.raises(ValueError, match="nonnegative integers"):
        _write(tmp_path, _view(ViewEdge("z/base", "a/top", statement_count=count)))

    assert not (tmp_path / "index.json").exists()


def test_search_context_metadata_is_additive_and_optional(tmp_path: Path) -> None:
    view = _view(ViewEdge("z/base", "a/top", proof_count=1))
    main = replace(
        view.nodes[1],
        declaration="theorem",
        lean="Project.mainTheorem",
        summary="A mathematical statement, not an import description.",
        area="Geometry",
        navigation="topic",
    )
    view = replace(view, nodes=(view.nodes[0], main, view.nodes[2]))
    payload = _write(
        tmp_path,
        view,
        data_links={main.id: "scopes/geometry.json"},
        article_links={main.id: "../roadmap/geometry.html#main-theorem"},
    )

    assert payload["nodes"][1] == {
        "id": "a/top",
        "kind": "node",
        "result": True,
        "status": "planned",
        "title": "Main theorem",
        "url": "chapters/context.html#node=a/top",
        "lean": "Project.mainTheorem",
        "summary": "A mathematical statement, not an import description.",
        "declaration": "theorem",
        "area": "Geometry",
        "navigation": "topic",
        "data_url": "scopes/geometry.json",
        "article_url": "../roadmap/geometry.html#main-theorem",
    }
    assert not any(key in payload["nodes"][0] for key in ("data_url", "article_url", "declaration", "area", "navigation"))
    assert payload["relations"] == [[0, 1, 0, 1]]


def test_search_scope_rollups_do_not_borrow_result_or_inventory_semantics(tmp_path: Path) -> None:
    counts = (("mathlib", 5), ("fully_proved", 3), ("planned", 2), ("inventory_checked", 20))
    scope = ViewNode(
        "geometry", "Geometry", "scope", tuple(f"geometry/n{index}" for index in range(30)),
        counts, status_key="planned", internal_dependency_count=11,
    )
    theorem = ViewNode(
        "geometry/result", "A theorem", "node", ("geometry/result",), (("fully_proved", 1),),
        declaration="theorem", status_key="fully_proved",
    )
    inventory = ViewNode(
        "geometry/catalog", "Module inventory", "node", ("geometry/catalog",), (("inventory_checked", 1),),
        catalog="module", status_key="fully_proved",
    )
    payload = _write(
        tmp_path,
        GraphView(
            kind="full", title="Scope endpoint", nodes=(scope, theorem, inventory),
            edges=(ViewEdge(scope.id, theorem.id, statement_count=1),),
        ),
    )
    scope_entry, result_entry, inventory_entry = payload["nodes"]

    assert scope_entry["kind"] == "scope" and scope_entry["result"] is False
    assert scope_entry["member_count"] == scope.item_count == 30
    assert scope_entry["status_counts"] == [[key, count] for key, count in counts]
    assert scope_entry["internal_dependency_count"] == 11
    assert scope_entry["status"] == "planned"
    assert result_entry["status"] == "fully_proved" and result_entry["declaration"] == "theorem"
    assert inventory_entry["catalog"] == "module" and inventory_entry["status"] == "inventory_checked"
    assert "declaration" not in inventory_entry
    for entry in (result_entry, inventory_entry):
        assert not any(key in entry for key in ("member_count", "status_counts", "internal_dependency_count"))


def test_full_graph_search_relations_are_authored_not_containment_or_imports(tmp_path: Path) -> None:
    roadmap = tmp_path / "blueprint/roadmap"
    graph = Graph(
        roadmap.parent,
        {
            "roadmap": Node("roadmap", "Mathematics", roadmap / "README.md", ()),
            "geometry": Node("geometry", "Geometry", roadmap / "geometry/README.md", (), parent="roadmap"),
            "geometry/base": Node(
                "geometry/base", "Base definition", roadmap / "geometry/base.md", (),
                declaration="def", parent="geometry", lean="Shared.Imported.base",
            ),
            "geometry/top": Node(
                "geometry/top", "Theorem", roadmap / "geometry/top.md", ("geometry/base",),
                statement_dependencies=("geometry/base",), proof_dependencies=("geometry/base",),
                declaration="theorem", parent="geometry", lean="Shared.Imported.top",
            ),
            "geometry/use": Node(
                "geometry/use", "Application", roadmap / "geometry/use.md", ("geometry/top",),
                proof_dependencies=("geometry/top",), declaration="theorem", parent="geometry",
            ),
        },
    )
    payload = _write(tmp_path, full_view(graph, derive(graph)))
    ids = [node["id"] for node in payload["nodes"]]
    relations = [
        (ids[source], ids[target], statement, proof)
        for source, target, statement, proof in payload["relations"]
    ]

    assert relations == [("geometry/base", "geometry/top", 1, 0), ("geometry/top", "geometry/use", 0, 1)]
    assert not any("roadmap" in edge[:2] or "geometry" in edge[:2] for edge in relations)
    # The same prerequisite under both dependency headings is one statement
    # edge in the authored full view, not a duplicated extra proof edge.
    assert sum(statement + proof for _, _, statement, proof in relations) == graph.edge_count


def test_publication_reuses_exact_book_links_and_encoded_scope_payloads(tmp_path: Path) -> None:
    """Direct theorem navigation must keep the renderer's anchors and route bases."""
    from autoform_cli.render import _anchored_links

    roadmap = tmp_path / "blueprint/roadmap"
    topic = "Geometry & métric"
    nested = f"{topic}/Metric % spaces"
    definition = f"{topic}/definition"
    theorem = f"{nested}/triangle lemma"
    graph = Graph(
        roadmap.parent,
        {
            "roadmap": Node("roadmap", "Mathematics", roadmap / "README.md", ()),
            topic: Node(topic, "Metric geometry", roadmap / topic / "README.md", (), parent="roadmap"),
            nested: Node(nested, "Triangles", roadmap / nested / "README.md", (), parent=topic),
            definition: Node(
                definition, "Metric", roadmap / f"{definition}.md", (), declaration="def", parent=topic,
            ),
            theorem: Node(
                theorem, "Triangle theorem", roadmap / f"{theorem}.md", (definition,),
                statement_dependencies=(definition,), declaration="theorem", parent=nested,
            ),
        },
    )
    destination = tmp_path / "out"
    targets = {
        definition: (destination / "roadmap" / topic / "README.md", "definition"),
        theorem: (destination / "roadmap" / nested / "README.md", "triangle%20lemma"),
    }
    requested: list[tuple[Path, tuple[str, ...]]] = []

    def node_links(page: Path, node_ids) -> dict[str, str]:
        selected = tuple(node_ids)
        requested.append((page, selected))
        return _anchored_links(targets, page, node_ids=selected)

    written = graph_pages.write_graph_pages(graph, derive(graph), destination, node_links=node_links)
    index = json.loads((destination / graph_pages.SEARCH_INDEX).read_text(encoding="utf-8"))
    indexed = {node["id"]: node for node in index["nodes"]}
    navigation = json.loads((destination / graph_pages.NAVIGATION_INDEX).read_text(encoding="utf-8"))
    outlined = {node["id"]: node for node in navigation["nodes"]}

    # Book targets are recorded from the original per-page callback calls,
    # not from an extra request for all articles on every explorer page.
    assert sorted(page for page, _ in requested) == sorted(written)
    for page, ids in requested:
        payload = json.loads(page.with_suffix(".json").read_text(encoding="utf-8"))
        assert set(ids) == {node["id"] for node in payload["nodes"] if node["kind"] == "node"}
    assert indexed[definition]["article_url"] == "../roadmap/Geometry%20%26%20m%C3%A9tric/index.html#definition"
    assert indexed[theorem]["article_url"] == (
        "../roadmap/Geometry%20%26%20m%C3%A9tric/Metric%20%25%20spaces/index.html#triangle%20lemma"
    )
    assert indexed[theorem]["data_url"] == "scopes/Geometry%20%26%20m%C3%A9tric/Metric%20%25%20spaces.json"
    assert outlined[nested]["data_url"] == indexed[theorem]["data_url"] == indexed[nested]["data_url"]
    assert outlined[nested]["article_url"] == (
        "../roadmap/Geometry%20%26%20m%C3%A9tric/Metric%20%25%20spaces/index.html"
    )
    assert outlined["roadmap"]["article_url"] == "../roadmap/index.html"
    catalog_url = "https://example.test/atlas-lean/dependencies/index.json"
    for node in indexed.values():
        target = urlsplit(urljoin(catalog_url, node["data_url"]))
        assert not target.fragment and not target.query
        path = unquote(target.path.removeprefix("/atlas-lean/"))
        assert (destination / path).is_file(), f"missing context payload: {node['id']} -> {node['data_url']}"


def test_publication_keeps_a_root_relative_book_target_unchanged(tmp_path: Path) -> None:
    roadmap = tmp_path / "blueprint/roadmap"
    graph = Graph(
        roadmap.parent,
        {"lemma": Node("lemma", "Lemma", roadmap / "lemma.md", (), declaration="lemma")},
    )
    destination = tmp_path / "out"
    href = "/atlas-lean/roadmap/index.html?print=1#lemma%20statement"

    graph_pages.write_graph_pages(
        graph,
        derive(graph),
        destination,
        node_links=lambda page, node_ids: {node_id: href for node_id in node_ids},
    )
    index = json.loads((destination / graph_pages.SEARCH_INDEX).read_text(encoding="utf-8"))

    assert index["nodes"][0]["article_url"] == href
    assert index["nodes"][0]["url"] == "chapters/roadmap.html#node=lemma"
