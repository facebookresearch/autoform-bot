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


def test_a_title_is_text_in_a_label_and_a_tooltip() -> None:
    """Mermaid draws a label as HTML, so the markup a title can carry in its
    code is written as entities, which it shows as typed."""

    view = GraphView(
        kind="focus",
        title="Local context",
        focus="chapter/result",
        radius=1,
        nodes=(
            ViewNode(
                id="chapter/result",
                title='Top `<b style="position:fixed">x</b>` & co',
                kind="node",
                members=("chapter/result",),
                status_counts=(("can_state", 1),),
                declaration="theorem",
                status_key="can_state",
            ),
        ),
        edges=(),
    )

    diagram = render_view_diagram(view, links={"chapter/result": "../book.html#result"})
    title = "Top #96;#lt;b style=#quot;position:fixed#quot;#gt;x#lt;/b#gt;#96; #amp; co"

    assert f'n0("{title}"):::can_state' in diagram
    assert f'click n0 "../book.html#result" "{title} — ' in diagram
    assert "<b" not in diagram and "&" not in diagram


def test_a_link_is_one_url_that_names_no_scheme() -> None:
    """A link is made from file and folder names, which may hold a quote, a
    semicolon or a colon; written as a URL, it stays one Mermaid string and
    a relative path, so a name cannot add a click's call or a script link."""

    def node(id: str) -> ViewNode:
        return ViewNode(id=id, title="T", kind="scope", members=(id,), status_counts=(("can_state", 1),))

    view = GraphView(kind="project", title="Project map", nodes=(node("scope:a"), node("scope:b")), edges=())
    links = {
        "scope:a": "javascript:alert(7).html",
        "scope:b": '../roadmap/index.html#t";click n0 call alert(7);click n0 "op',
    }

    clicks = [line for line in render_view_diagram(view, links=links).splitlines() if "click" in line]

    assert clicks == [
        '  click n0 "javascript%3Aalert%287%29.html" "T — 1 item · 1 ready to state"',
        '  click n1 "../roadmap/index.html#t%22%3Bclick%20n0%20call%20alert%287%29%3Bclick%20n0%20%22op"'
        ' "T — 1 item · 1 ready to state"',
    ]
