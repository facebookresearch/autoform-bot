from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli import dag_viewer, graph_pages
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode
from autoform_cli.mermaid import relative_link, render_view_diagram


def test_project_view_renders_status_distribution_and_aggregated_edges() -> None:
    view = GraphView(
        kind="project",
        title="Project map",
        nodes=(
            ViewNode(
                id="scope:a",
                title="Foundations",
                kind="scope",
                members=("a/one", "a/two", "a/three"),
                status_counts=(("fully_proved", 2), ("can_prove", 1)),
            ),
            ViewNode(
                id="scope:b",
                title="Applications",
                kind="scope",
                members=("b/result",),
                status_counts=(("planned", 1),),
            ),
        ),
        edges=(ViewEdge("scope:a", "scope:b", statement_count=3, proof_count=1),),
    )

    diagram = render_view_diagram(
        view,
        links={"scope:a": "chapters/a.html", "scope:b": "chapters/b.html"},
    )

    assert 'n0["Foundations<br/><small>3 items · 2 fully proved · 1 ready to prove</small>"]:::scope' in diagram
    assert "n0 -->|3| n1" in diagram
    assert "n0 -.-> n1" in diagram
    assert 'click n0 "chapters/a.html"' in diagram
    assert "classDef scope fill:#EBF2FE" in diagram


def test_chapter_boundary_and_focused_theorem_have_distinct_presentation() -> None:
    view = GraphView(
        kind="focus",
        title="Local context",
        focus="chapter/result",
        radius=1,
        nodes=(
            ViewNode(
                id="boundary:foundations",
                title="Foundations",
                kind="boundary",
                members=("foundations/base",),
                status_counts=(("fully_proved", 1),),
            ),
            ViewNode(
                id="chapter/result",
                title="Main result",
                kind="node",
                members=("chapter/result",),
                status_counts=(("can_state", 1),),
                declaration="theorem",
                status_key="can_state",
                focus=True,
            ),
        ),
        edges=(ViewEdge("boundary:foundations", "chapter/result", proof_count=1),),
    )

    diagram = render_view_diagram(view, links={"chapter/result": "../book.html#result"})

    assert "External chapter: Foundations" in diagram
    assert ":::boundary" in diagram
    assert 'n1("Main result"):::can_state' in diagram
    assert "n0 -.-> n1" in diagram
    assert "class n1 focus" in diagram
    assert 'click n1 "../book.html#result"' in diagram


def test_full_view_payload_is_deterministic_layout_ready_and_data_only(tmp_path: Path) -> None:
    nodes = tuple(
        ViewNode(
            id=f"chapter/node-{index}",
            title="Unsafe <script>" if index == 0 else f"Node {index}",
            kind="node",
            members=(f"chapter/node-{index}",),
            status_counts=(("planned", 1),),
            declaration=None if index == 0 else "theorem",
            catalog="module" if index == 0 else None,
            status_key="planned",
        )
        for index in range(170)
    )
    view = GraphView(
        kind="full",
        title="Full graph",
        nodes=nodes,
        edges=tuple(ViewEdge(nodes[index].id, nodes[index + 1].id, statement_count=1) for index in range(169)),
    )
    outputs = [tmp_path / "one.json", tmp_path / "two.json"]
    for output in outputs:
        dag_viewer.write_payload(output, view, links={node.id: f"../book.html#{node.id}" for node in nodes})

    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    payload = json.loads(outputs[0].read_text(encoding="utf-8"))
    assert payload["schema"] == "autoform-dag-view/v1"
    assert payload["node_count"] == 170
    assert payload["edge_count"] == 169
    assert payload["nodes"][0]["title"] == "Unsafe <script>"
    assert payload["nodes"][0]["catalog"] == "module"
    assert max(node["row"] for node in payload["nodes"]) < dag_viewer.MAX_LAYOUT_ROWS
    assert max(node["column"] for node in payload["nodes"]) > 0

    script = dag_viewer.viewer_script()
    assert "innerHTML" not in script
    assert "textContent" in script
    assert "requestAnimationFrame" in script
    assert 'data.schema !== "autoform-dag-view/v1"' in script
    assert 'node.kind === "boundary"' in script
    assert 'params.delete("node")' in script
    assert 'count > 1' in script


def test_payload_layout_uses_dependencies_not_node_tuple_order(tmp_path: Path) -> None:
    inside = ViewNode("inside", "Inside", "node", ("inside",), (("planned", 1),))
    boundary = ViewNode(
        "boundary:outside",
        "Outside",
        "boundary",
        ("outside",),
        (("fully_proved", 1),),
    )
    view = GraphView(
        kind="chapter",
        title="Chapter",
        nodes=(inside, boundary),
        edges=(ViewEdge(boundary.id, inside.id, statement_count=3),),
    )
    output = tmp_path / "view.json"

    dag_viewer.write_payload(output, view, links={})

    payload = json.loads(output.read_text(encoding="utf-8"))
    columns = {node["id"]: node["column"] for node in payload["nodes"]}
    assert columns[boundary.id] < columns[inside.id]


def test_payload_rejects_a_cyclic_view(tmp_path: Path) -> None:
    nodes = (
        ViewNode("a", "A", "node", ("a",), (("planned", 1),)),
        ViewNode("b", "B", "node", ("b",), (("planned", 1),)),
    )
    view = GraphView(
        kind="full",
        title="Cycle",
        nodes=nodes,
        edges=(ViewEdge("a", "b", statement_count=1), ViewEdge("b", "a", statement_count=1)),
    )

    with pytest.raises(ValueError, match="contains a cycle"):
        dag_viewer.write_payload(tmp_path / "cycle.json", view, links={})


def test_large_views_fail_over_before_mermaid_hard_limits() -> None:
    node = ViewNode(
        id="n",
        title="Node",
        kind="node",
        members=("n",),
        status_counts=(("planned", 1),),
        status_key="planned",
    )
    edge_heavy = GraphView(
        kind="chapter",
        title="Large",
        nodes=(node,),
        edges=tuple(ViewEdge("n", "n", statement_count=1) for _ in range(dag_viewer.MAX_MERMAID_EDGE_LINES + 1)),
    )
    assert dag_viewer.requires_interactive(edge_heavy, "small")

    text_heavy = GraphView(kind="chapter", title="Large", nodes=(node,), edges=())
    assert dag_viewer.requires_interactive(
        text_heavy,
        "x" * (dag_viewer.MAX_MERMAID_CHARACTERS + 1),
    )
    assert not dag_viewer.requires_interactive(text_heavy, "small")


def test_viewer_script_links_are_relative_to_the_rendered_site_root(tmp_path: Path) -> None:
    assert (
        graph_pages._viewer_script_link(tmp_path / "dependencies.md", tmp_path)
        == "javascripts/blueprint-dag.js"
    )
    assert (
        graph_pages._viewer_script_link(tmp_path / "dependencies/full.md", tmp_path)
        == "../javascripts/blueprint-dag.js"
    )
    assert (
        graph_pages._viewer_script_link(
            tmp_path / "dependencies/chapters/dependencies.md",
            tmp_path,
        )
        == "../../javascripts/blueprint-dag.js"
    )


def test_interactive_graph_urls_encode_authored_path_delimiters(tmp_path: Path) -> None:
    site = tmp_path / "site"
    page = site / "dependencies/chapters/hash#chapter.md"
    node = ViewNode(
        "node",
        "x" * (dag_viewer.MAX_MERMAID_CHARACTERS + 1),
        "node",
        ("node",),
        (("planned", 1),),
    )
    view = GraphView(kind="chapter", title="Large", nodes=(node,), edges=())

    graph_pages._write_page(
        page,
        view=view,
        statuses={},
        links={},
        heading="Large",
        lead="Large graph.",
        site_root=site,
    )

    contents = page.read_text(encoding="utf-8")
    assert 'data-graph-src="hash%23chapter.json"' in contents
    assert page.with_suffix(".json").is_file()
    assert relative_link(page, site / "index.md", ".html") == "dependencies/chapters/hash%23chapter.html"
