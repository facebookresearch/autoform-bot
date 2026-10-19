"""Deep presentation roles must not change mathematical scope or edge meaning."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.graph import Graph, Node
from autoform_cli.graph_views import scope_view
from autoform_cli.status import derive


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
    assert view.overview and view.total_count == 5
    assert next(node for node in view.nodes if node.id == "scope:topic0000").members == ("result",)
    assert next(node for node in view.nodes if node.id == "scope:source").navigation == "source"
    assert {(edge.source, edge.target, edge.statement_count, edge.proof_count) for edge in view.edges} == {
        ("scope:topic0000", "after", 1, 0),
        ("scope:wrapper0000", "after", 1, 0),
    }
    assert graph.nodes == original
