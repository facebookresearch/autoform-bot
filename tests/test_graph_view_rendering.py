from __future__ import annotations

import json
from pathlib import Path

from autoform_cli import dag_viewer, graph_pages
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode
from autoform_cli.mermaid import render_view_diagram


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
            status_counts=(
                (("inventory_checked", 1),)
                if index == 0
                else (("planned", 1),)
            ),
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
    assert payload["nodes"][0]["status"] == "inventory_checked"
    assert payload["palette"]["inventory_checked"]["label"] == "inventory checked"
    assert (
        payload["palette"]["inventory_checked"]["light"]
        == payload["palette"]["planned"]["light"]
    )
    assert max(node["row"] for node in payload["nodes"]) < dag_viewer.MAX_LAYOUT_ROWS
    assert max(node["column"] for node in payload["nodes"]) > 0

    script = dag_viewer.viewer_script()
    assert "innerHTML" not in script
    assert "textContent" in script
    assert "requestAnimationFrame" in script
    assert "#node=" in script
    assert 'node.catalog === "module" ? "module inventory"' in script
    assert 'node.catalog + " catalog"' not in script


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
    assert graph_pages._viewer_script_link(tmp_path / "dependencies.md") == "javascripts/blueprint-dag.js"
    assert (
        graph_pages._viewer_script_link(tmp_path / "dependencies/full.md")
        == "../javascripts/blueprint-dag.js"
    )
    assert (
        graph_pages._viewer_script_link(tmp_path / "dependencies/chapters/large.md")
        == "../../javascripts/blueprint-dag.js"
    )
