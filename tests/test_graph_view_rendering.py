from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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
            status_counts=((("inventory_checked", 1),) if index == 0 else (("planned", 1),)),
            declaration=None if index == 0 else "theorem",
            catalog="module" if index == 0 else None,
            status_key="planned",
            summary="A reader-facing mathematical summary." if index == 0 else None,
            lean="Project.node" if index == 0 else None,
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
    assert payload["schema"] == "autoform-dag-view/v2"
    assert payload["node_count"] == 170
    assert payload["edge_count"] == 169
    assert payload["counts"] == {
        "connected": 170,
        "dependencies": 169,
        "isolated": 0,
        "nodes": 170,
    }
    assert payload["view"] == {
        "breadcrumbs": [],
        "focus": None,
        "kind": "full",
        "presentation": "dag",
        "radius": None,
        "scope": None,
        "summary": None,
    }
    assert payload["present_statuses"] == ["planned", "inventory_checked"]
    assert payload["nodes"][0]["title"] == "Unsafe <script>"
    assert payload["nodes"][0]["catalog"] == "module"
    assert payload["nodes"][0]["status"] == "inventory_checked"
    assert payload["nodes"][0]["member_count"] == 1
    assert payload["nodes"][0]["member_count"] == 1
    assert "members" not in payload["nodes"][0]
    assert payload["nodes"][0]["status_counts"] == {"inventory_checked": 1}
    assert payload["nodes"][0]["summary"] == "A reader-facing mathematical summary."
    assert payload["nodes"][0]["lean"] == "Project.node"
    assert payload["nodes"][0]["isolated"] is False
    assert payload["palette"]["inventory_checked"]["label"] == "inventory checked"
    assert payload["palette"]["inventory_checked"]["neutral"] is True
    assert payload["palette"]["inventory_checked"]["light"] == payload["palette"]["planned"]["light"]
    assert max(node["row"] for node in payload["nodes"]) < dag_viewer.MAX_LAYOUT_ROWS
    assert max(node["column"] for node in payload["nodes"]) > 0
    positions = {(node["layout"]["x"], node["layout"]["y"]) for node in payload["nodes"]}
    assert len(positions) == len(nodes)
    assert all(
        payload["nodes"][index]["layout"]["rank"] < payload["nodes"][index + 1]["layout"]["rank"]
        for index in range(len(nodes) - 1)
    )

    script = dag_viewer.viewer_script()
    assert "innerHTML" not in script
    assert "textContent" in script
    assert "requestAnimationFrame" in script
    assert 'params.set("node", state.selected.id)' in script
    assert 'window.addEventListener("hashchange"' in script
    assert 'window.addEventListener("popstate"' in script
    assert 'view.kind !== "full"' in script
    assert '"Inventory checked is neutral:' in script


def test_isolated_inventory_does_not_spread_the_connected_dag() -> None:
    node_ids = ["root", "result", *(f"isolated-{index}" for index in range(1_000))]
    nodes = tuple(
        ViewNode(
            id=node_id,
            title=node_id,
            kind="node",
            members=(node_id,),
            status_counts=(("planned", 1),),
            declaration="theorem",
            status_key="planned",
        )
        for node_id in node_ids
    )
    view = GraphView(
        kind="full",
        title="Mixed graph",
        nodes=nodes,
        edges=(ViewEdge("root", "result", statement_count=1),),
    )

    positions = dag_viewer._positions(view)

    assert positions["root"]["column"] == 0
    assert positions["result"]["column"] == 1
    assert positions["isolated-0"]["column"] >= 3
    assert max(positions[node_id]["column"] for node_id in ("root", "result")) == 1


def test_high_level_topics_use_a_weighted_knowledge_atlas(tmp_path: Path) -> None:
    nodes = tuple(
        ViewNode(
            id=f"chapter-{index}",
            title=f"Chapter {index}",
            kind="scope",
            members=tuple(f"chapter-{index}/item-{item}" for item in range(index + 1)),
            status_counts=(("planned", 1),),
            status_key="planned",
            area="Foundations" if index < 10 else "Applications",
        )
        for index in range(33)
    )
    view = GraphView(kind="project", title="Project", nodes=nodes, edges=())
    positions = dag_viewer._positions(view)
    output = tmp_path / "atlas.json"
    dag_viewer.write_payload(output, view, links={})
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["view"]["presentation"] == "atlas"
    assert {(region["label"], region["count"]) for region in payload["regions"]} == {
        ("Applications", 23),
        ("Foundations", 10),
    }
    assert len({(position["x"], position["y"]) for position in positions.values()}) == len(nodes)
    assert all(position["width"] == position["height"] for position in positions.values())
    assert max(position["width"] for position in positions.values()) > min(
        position["width"] for position in positions.values()
    )


def test_explorer_marks_isolates_but_keeps_them_in_the_complete_inventory(tmp_path: Path) -> None:
    nodes = tuple(
        ViewNode(
            id=node_id,
            title=node_id.upper(),
            kind="node",
            members=(node_id,),
            status_counts=(("planned", 1),),
            status_key="planned",
        )
        for node_id in ("a", "b", "isolated")
    )
    view = GraphView(
        kind="full",
        title="Full graph",
        nodes=nodes,
        edges=(ViewEdge("a", "b", statement_count=1),),
    )

    output = tmp_path / "graph.json"
    dag_viewer.write_payload(output, view, links={})
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["counts"]["isolated"] == 1
    assert {node["id"] for node in payload["nodes"] if node["isolated"]} == {"isolated"}
    script = dag_viewer.viewer_script()
    assert "inventoryNodes" in script
    assert '" · items without edges included"' in script
    assert "MAX_DOM_NODES = 180" in script
    assert "MAX_MAP_NODES = 120" in script
    assert "MIN_READABLE_SCALE = .8" in script
    assert "MIN_ATLAS_SCALE = .55" in script
    assert "fitted < readableFloor" in script
    assert 'params.get("view") === "list" || !state.mapAllowed' in script
    assert '" of " + data.nodes.length + " shown"' in script
    assert '"+" + hidden + " hidden"' in script


def test_explorer_is_responsive_accessible_and_history_addressable(tmp_path: Path) -> None:
    container = dag_viewer.render_container(
        'unsafe" data-x="oops',
        script_href='scripts/viewer" onload="oops.js',
        fallback_links=(
            ("Unsafe <node>", 'roadmap/a" onclick="oops'),
            ("Unsafe protocol", "javascript:alert(1)"),
        ),
        fallback_total=5,
    )
    script = dag_viewer.viewer_script()

    assert 'data-graph-src="unsafe%22%20data-x%3D%22oops"' in container
    assert 'src="scripts/viewer%22%20onload%3D%22oops.js"' in container
    assert "Dependency explorer fallback." in container
    assert "Unsafe &lt;node&gt;" in container
    assert 'href="roadmap/a&quot; onclick=&quot;oops"' in container
    assert "javascript:alert" not in container
    assert "Showing 1 of 5 linked nodes" in container
    assert 'data-layout="app"' in container
    assert "<style data-autoform-dag-style>" in container
    assert "@media (max-width: 680px)" in container
    assert "@media (prefers-reduced-motion: reduce)" in container
    assert "100svh" in container
    assert "100dvh" in container
    assert 'refs.canvas.setAttribute("aria-hidden", "true")' in script
    assert 'refs.stage.setAttribute("role", "region")' in script
    assert 'toolbar.setAttribute("role", "toolbar")' in script
    assert 'element("div", "bp-dag-main")' in script
    assert 'element("main", "bp-dag-main")' not in script
    assert 'refs.announcement.setAttribute("aria-live", "polite")' in script
    assert 'refs.sheetHandle.setAttribute("aria-controls", refs.inspectorInner.id)' in script
    assert 'refs.inspectorInner.setAttribute("inert", "")' in script
    assert 'refs.inspectorInner.setAttribute("aria-hidden", "true")' in script
    assert 'window.addEventListener("resize", syncSheetAccessibility)' in script
    assert "control.dataset.autoformNodeId = node.id" in script
    assert 'key === "ArrowLeft"' in script
    assert "new ResizeObserver(resize)" in script
    assert "state.pointers.size === 2" in script
    assert "event.type === \"pointerup\" && wasSingle && !state.moved" in script
    assert "touch-action: pan-y" in container
    assert '.bp-dag-viewer[data-layout=app] .bp-dag-stage { touch-action: none; }' in container
    assert 'url.origin === window.location.origin' in script
    assert 'if (target === current) return' in script
    assert 'state.selected && !state.selectedStatuses.has(state.selected.status)' in script
    assert 'w: node.width, h: node.height' in script
    assert 'spotlight.before.has(edge.source) && spotlight.before.has(edge.target)' in script
    assert 'spotlight.after.has(edge.source) && spotlight.after.has(edge.target)' in script
    assert "if (!state.activeDirty) return" in script
    assert "if (query === state.searchCacheQuery) return state.searchCache" in script
    assert 'data.schema !== "autoform-dag-view/v2"' in script
    assert 'ctx.fillText("×" + count' in script
    assert "Title prefix" in script and "Id prefix" in script and "Word prefix" in script

    node = shutil.which("node")
    if node:
        runtime = tmp_path / "viewer.js"
        runtime.write_text(script, encoding="utf-8")
        subprocess.run([node, "--check", str(runtime)], check=True, capture_output=True, text=True)


def test_explorer_pages_large_searches_and_relations_and_fits_deep_graphs() -> None:
    script = dag_viewer.viewer_script()

    assert "MIN_SCALE = .002, MIN_READABLE_SCALE = .8, MIN_ATLAS_SCALE = .55" in script
    assert '"Show all "' not in script
    assert "Math.min(matches.length, state.searchLimit + SEARCH_PAGE)" in script
    assert "Math.min(visible + RELATION_PAGE, ids.length)" in script
    assert "ids.slice(visible, next)" in script
    assert "clamp(old * factor, MIN_SCALE, 4)" in script
    assert "Math.max(fitted, readableFloor), MIN_SCALE, 1.35" in script


def test_explorer_assets_and_ordinary_payload_stay_within_budgets(tmp_path: Path) -> None:
    nodes = tuple(
        ViewNode(
            id=f"chapter/node-{index}",
            title=f"Node {index}",
            kind="node",
            members=(f"chapter/node-{index}",),
            status_counts=(("planned", 1),),
            status_key="planned",
        )
        for index in range(500)
    )
    view = GraphView(
        kind="full",
        title="Budget fixture",
        nodes=nodes,
        edges=tuple(ViewEdge(nodes[index].id, nodes[index + 1].id, statement_count=1) for index in range(499)),
    )
    output = tmp_path / "budget.json"
    dag_viewer.write_payload(output, view, links={})
    search = tmp_path / "search.json"
    dag_viewer.write_search_index(
        search,
        view,
        context_links={node.id: f"chapters/chapter.html#node={node.id}" for node in nodes},
    )

    assert len(dag_viewer.viewer_script().encode()) < 56_000
    assert output.stat().st_size < 300_000
    assert search.stat().st_size < 100_000
    search_payload = json.loads(search.read_text(encoding="utf-8"))
    assert search_payload["schema"] == "autoform-dag-search/v1"
    assert "layout" not in search_payload["nodes"][0]


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
    assert graph_pages._viewer_script_link(tmp_path / "dependencies.md", tmp_path) == "javascripts/blueprint-dag.js"
    assert graph_pages._viewer_script_link(tmp_path / "dependencies/full.md", tmp_path) == "../javascripts/blueprint-dag.js"
    assert (
        graph_pages._viewer_script_link(tmp_path / "dependencies/chapters/large.md", tmp_path)
        == "../../javascripts/blueprint-dag.js"
    )


def test_payload_layout_follows_dependencies_and_keeps_collapsed_cycles_stable() -> None:
    nodes = (
        ViewNode("result", "Result", "node", ("result",), (("planned", 1),)),
        ViewNode("base", "Base", "node", ("base",), (("planned", 1),)),
    )
    acyclic = GraphView(
        kind="full",
        title="Acyclic",
        nodes=nodes,
        edges=(ViewEdge("base", "result", statement_count=1),),
    )
    cyclic = GraphView(
        kind="project",
        title="Collapsed cycle",
        nodes=nodes,
        edges=(
            ViewEdge("base", "result", statement_count=1),
            ViewEdge("result", "base", proof_count=1),
        ),
    )

    acyclic_positions = dag_viewer._positions(acyclic)
    assert acyclic_positions["base"]["rank"] < acyclic_positions["result"]["rank"]
    assert dag_viewer._positions(cyclic) == dag_viewer._positions(cyclic)
    assert len({(entry["x"], entry["y"]) for entry in dag_viewer._positions(cyclic).values()}) == 2


def test_payload_rejects_duplicate_nodes_and_unknown_edges() -> None:
    node = ViewNode("a", "A", "node", ("a",), (("planned", 1),))
    duplicate = GraphView(kind="full", title="Duplicate", nodes=(node, node), edges=())
    unknown = GraphView(
        kind="full",
        title="Unknown",
        nodes=(node,),
        edges=(ViewEdge("missing", "a", statement_count=1),),
    )

    with pytest.raises(ValueError, match="duplicate node ids"):
        dag_viewer._positions(duplicate)
    with pytest.raises(ValueError, match="unknown node"):
        dag_viewer._positions(unknown)


def test_interactive_graph_urls_encode_authored_path_delimiters() -> None:
    container = dag_viewer.render_container(
        "hash#chapter.json",
        script_href="../javascripts/blueprint-dag.js?cache#asset",
    )

    assert 'data-graph-src="hash%23chapter.json"' in container
    assert 'src="../javascripts/blueprint-dag.js%3Fcache%23asset"' in container
