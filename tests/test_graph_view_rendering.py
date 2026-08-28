from __future__ import annotations

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
