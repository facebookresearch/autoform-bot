from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from autoform_cli import dag_viewer, graph_pages
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode


def _viewer_source() -> str:
    """Inspect readable implementation seams, not a compiler's private names.

    The emitted runtime and its exact source fingerprint have separate asset
    tests. Normalizing explicit private-property prefixes and renderer-private
    ui$/dom$ locals keeps these static assertions independent of compiler naming.
    Executed production bodies are never normalized.
    """
    source = dag_viewer._SCRIPT.replace("refs.$", "refs.").replace("state.$", "state.")
    source = re.sub(r"\bui\$([A-Za-z_]\w*)", r"state.\1", source)
    return re.sub(r"\bdom\$([A-Za-z_]\w*)", r"refs.\1", source)


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
        for index in range(dag_viewer.MAX_MAP_NODES)
    )
    view = GraphView(
        kind="full",
        title="Full graph",
        nodes=nodes,
        edges=tuple(ViewEdge(source.id, target.id, statement_count=1) for source, target in zip(nodes, nodes[1:])),
    )
    outputs = [tmp_path / "one.json", tmp_path / "two.json"]
    for output in outputs:
        dag_viewer.write_payload(output, view, links={node.id: f"../book.html#{node.id}" for node in nodes})

    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    payload = json.loads(outputs[0].read_text(encoding="utf-8"))
    assert payload["schema"] == "autoform-dag-view/v2"
    # The explorer reads each field once; nothing is duplicated for other readers.
    assert set(payload) == {
        "edge_count",
        "edges",
        "nodes",
        "palette",
        "present_statuses",
        "regions",
        "schema",
        "title",
        "view",
    }
    assert set(payload["nodes"][0]["layout"]) == {"column", "height", "row", "width", "x", "y"}
    assert len(payload["nodes"]) == dag_viewer.MAX_MAP_NODES
    assert payload["edge_count"] == dag_viewer.MAX_MAP_NODES - 1
    assert payload["view"] == {
        "book": None,
        "breadcrumbs": [],
        "kind": "full",
        "presentation": "dag",
        "summary": None,
    }
    assert payload["present_statuses"] == ["planned", "inventory_checked"]
    assert payload["nodes"][0]["title"] == "Unsafe <script>"
    assert payload["nodes"][0]["status"] == "inventory_checked"
    assert payload["nodes"][0]["member_count"] == 1
    assert "members" not in payload["nodes"][0]
    assert payload["nodes"][0]["status_counts"] == [["inventory_checked", 1]]
    assert payload["nodes"][0]["summary"] == "A reader-facing mathematical summary."
    assert payload["nodes"][0]["lean"] == "Project.node"
    assert payload["nodes"][0]["isolated"] is False
    assert payload["palette"]["inventory_checked"]["label"] == "inventory checked"
    assert payload["palette"]["inventory_checked"]["neutral"] is True
    assert max(node["layout"]["column"] for node in payload["nodes"]) > 0
    positions = {(node["layout"]["x"], node["layout"]["y"]) for node in payload["nodes"]}
    assert len(positions) == len(nodes)
    assert all(
        payload["nodes"][index]["layout"]["x"] < payload["nodes"][index + 1]["layout"]["x"]
        for index in range(len(nodes) - 1)
    )

    script = dag_viewer.viewer_script()
    assert "innerHTML" not in script
    assert "textContent" in script
    assert "requestAnimationFrame" in script
    for inventory in ("inventory_checked", "inventory_pending"):
        assert "it records inspection, not a mathematical proof." in payload["palette"][inventory]["meaning"]
    assert payload["palette"]["inventory_pending"]["neutral"] is True
    assert "mark.title = entry.meaning || statusLabel(key)" in _viewer_source()


def test_a_view_too_large_for_the_map_gets_no_layout(tmp_path: Path) -> None:
    """The explorer only browses a view above its map limit, yet each node carried a layout it never drew."""
    nodes = tuple(
        ViewNode(f"n{index}", f"N{index}", "node", (f"n{index}",), (("planned", 1),))
        for index in range(dag_viewer.MAX_MAP_NODES + 1)
    )
    output = tmp_path / "large.json"
    dag_viewer.write_payload(output, GraphView(kind="chapter", title="Large", nodes=nodes, edges=()), links={})

    assert not any("layout" in node for node in json.loads(output.read_text(encoding="utf-8"))["nodes"])
    # The runtime draws a map up to the same limit.
    assert (
        f"var NODE_W = {dag_viewer._NODE_WIDTH}, NODE_H = {dag_viewer._NODE_HEIGHT}, "
        f"MAX_MAP_NODES = {dag_viewer.MAX_MAP_NODES}," in _viewer_source()
    )
    # An atlas that large keeps its overview's count of topics in each region.
    topics = tuple(
        ViewNode(node.id, node.title, "scope", node.members, node.status_counts, area="Algebra") for node in nodes
    )
    dag_viewer.write_payload(output, GraphView(kind="project", title="Large", nodes=topics, edges=()), links={})
    regions = json.loads(output.read_text(encoding="utf-8"))["regions"]
    assert regions == [{"count": dag_viewer.MAX_MAP_NODES + 1, "label": "Algebra"}]


def test_each_inventory_has_a_neutral_colour_of_its_own(tmp_path: Path) -> None:
    """Checked and pending catalogs are drawn apart from each other and from every state.

    Both are neutral, but a ring holding planned targets and both kinds of
    inventory needs three greys, and borrowing a state's colour would read as
    target work.
    """
    statuses = ("planned", "inventory_pending", "inventory_checked")
    view = GraphView(
        kind="chapter",
        title="Library",
        nodes=tuple(
            ViewNode(
                id=f"library/{status}",
                title=status,
                kind="node",
                members=(f"library/{status}",),
                status_counts=((status, 1),),
                catalog=None if status == "planned" else "module",
                status_key="planned",
            )
            for status in statuses
        ),
        edges=(),
    )
    output = tmp_path / "library.json"
    dag_viewer.write_payload(output, view, links={})
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert [node["status"] for node in payload["nodes"]] == list(statuses)
    assert payload["present_statuses"] == ["planned", "inventory_checked", "inventory_pending"]
    pending = payload["palette"]["inventory_pending"]
    assert pending["label"] == "inventory pending"
    assert pending["neutral"] is True
    for inventory in ("inventory_checked", "inventory_pending"):
        for scheme in ("light", "dark"):
            assert all(
                entry[scheme]["stroke"] != payload["palette"][inventory][scheme]["stroke"]
                for key, entry in payload["palette"].items()
                if key != inventory
            )


def test_status_counts_keep_the_legend_order(tmp_path: Path) -> None:
    """Sorted object keys split a ring's finished arcs with grey ones and listed rows out of legend order."""
    counts = (
        ("mathlib", 10),
        ("fully_proved", 50),
        ("proved", 5),
        ("can_prove", 4),
        ("planned", 35),
        ("inventory_checked", 3),
    )
    view = GraphView(kind="project", title="Project", nodes=(ViewNode("scope:a", "A", "scope", (), counts),), edges=())
    output = tmp_path / "project.json"
    dag_viewer.write_payload(output, view, links={})

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["nodes"][0]["status_counts"] == [list(entry) for entry in counts]


def test_a_catalog_carries_a_few_lean_names_and_counts_the_rest(tmp_path: Path) -> None:
    """A catalog lists its keyword-declared public names, which made its payload and inspector line unbounded."""
    names = [f"Mathlib.Algebra.Module.lemma_{index:03d}" for index in range(150)]
    nodes = (
        ViewNode(
            "catalog",
            "Catalog",
            "node",
            ("catalog",),
            (("inventory_checked", 1),),
            catalog="module",
            lean=", ".join(names),
        ),
        ViewNode(
            "leaf", "Leaf", "node", ("leaf",), (("planned", 1),), declaration="theorem", lean=" ".join(names[:20])
        ),
    )
    output = tmp_path / "inventory.json"
    dag_viewer.write_payload(output, GraphView(kind="chapter", title="Inventory", nodes=nodes, edges=()), links={})

    catalog, leaf = json.loads(output.read_text(encoding="utf-8"))["nodes"]
    assert catalog["lean"] == ", ".join(names[:8])
    assert catalog["lean_more"] == 142
    assert len(catalog["lean"].encode()) < 400
    # A declaration leaf names only its own declarations, so it keeps them all.
    assert leaf["lean"] == " ".join(names[:20])
    assert "lean_more" not in leaf


def test_isolated_inventory_does_not_spread_the_connected_dag(tmp_path: Path) -> None:
    node_ids = ["root", "result", *(f"isolated-{index}" for index in range(100))]
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

    output = tmp_path / "mixed.json"
    dag_viewer.write_payload(output, view, links={})
    payload = json.loads(output.read_text(encoding="utf-8"))
    columns = {node["id"]: node["layout"]["column"] for node in payload["nodes"]}

    assert columns["root"] == 0
    assert columns["result"] == 1
    assert columns["isolated-0"] >= 3


def test_high_level_topics_use_a_readable_grouped_knowledge_atlas(tmp_path: Path) -> None:
    nodes = tuple(
        ViewNode(
            id=f"chapter-{index}",
            title=f"Chapter {index}",
            kind="scope",
            members=tuple(f"chapter-{index}/item-{item}" for item in range(index + 1)),
            status_counts=(("planned", index + 1),),
            status_key="planned",
            area="Foundations" if index < 10 else "Applications",
        )
        for index in range(33)
    )
    view = GraphView(kind="project", title="Project", nodes=nodes, edges=())
    output = tmp_path / "atlas.json"
    dag_viewer.write_payload(output, view, links={})
    payload = json.loads(output.read_text(encoding="utf-8"))
    layouts = [node["layout"] for node in payload["nodes"]]

    assert payload["view"]["presentation"] == "atlas"
    assert {(region["label"], region["count"]) for region in payload["regions"]} == {
        ("Applications", 23),
        ("Foundations", 10),
    }
    assert len({(layout["x"], layout["y"]) for layout in layouts}) == len(nodes)
    # The same fields a DAG's layout has; an atlas card is placed by x and y alone.
    assert all(set(layout) == {"column", "height", "row", "width", "x", "y"} for layout in layouts)
    assert all(layout["width"] > layout["height"] for layout in layouts)
    assert min(layout["width"] for layout in layouts) >= 220
    assert min(layout["height"] for layout in layouts) >= 88


def test_atlas_regions_that_differ_only_in_case_keep_one_order(tmp_path: Path) -> None:
    """Identical inputs publish identical bytes, whatever the hash seed of the render."""
    areas = ("algebra", "Algebra", "ALGEBRA", "aLGEBRA", "AlgebrA", "alGEBRA")
    view = GraphView(
        kind="project",
        title="Project",
        nodes=tuple(ViewNode(f"scope:{area}", area, "scope", (), (("planned", 1),), area=area) for area in areas),
        edges=(),
    )
    output = tmp_path / "atlas.json"
    dag_viewer.write_payload(output, view, links={})

    regions = json.loads(output.read_text(encoding="utf-8"))["regions"]
    assert [region["label"] for region in regions] == sorted(areas)


def test_a_view_of_topics_and_leaves_is_a_dag_not_an_atlas(tmp_path: Path) -> None:
    """Catalogs carry no edges, so a chapter holding them beside its sections is edgeless but not all topics."""
    nodes = (
        ViewNode("scope:section", "Section", "scope", ("section/a",), (("planned", 1),), area="Topology"),
        ViewNode("heine-borel", "Heine-Borel theorem", "node", ("heine-borel",), (("planned", 1),)),
        ViewNode("catalog", "Existing module", "node", ("catalog",), (("inventory_checked", 1),), catalog="module"),
    )
    output = tmp_path / "chapter.json"
    dag_viewer.write_payload(output, GraphView(kind="chapter", title="Chapter", nodes=nodes, edges=()), links={})
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["view"]["presentation"] == "dag"
    assert payload["regions"] == []
    assert {node["layout"]["width"] for node in payload["nodes"]} == {196}


def test_an_explorer_page_is_named_for_how_the_explorer_draws_it() -> None:
    """The heading followed the edges, so an edgeless chapter of results was a "knowledge map" drawn as cards."""
    results = GraphView("chapter", "Results dependency map", (ViewNode("a", "A", "node", ("a",), (("planned", 1),)),), ())
    topics = tuple(ViewNode(f"scope:{name}", name, "scope", (), (("planned", 1),), area="Algebra") for name in "xy")
    atlas = GraphView("chapter", "Topics dependency map", topics, (ViewEdge("scope:x", "scope:y", statement_count=1),))

    assert graph_pages._dependency_heading(results) == "Results dependencies"
    assert graph_pages._dependency_heading(atlas) == "Topics knowledge map"
    assert graph_pages._dependency_heading(results, "Top level") == "Top level dependencies"


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

    assert {node["id"] for node in payload["nodes"] if node["isolated"]} == {"isolated"}
    script = _viewer_source()
    assert "inventoryNodes" in script
    assert '"Include items without edges"' in script
    assert "state.showIsolated || !node.isolated || node === state.selected" in script


def test_explorer_host_is_escaped_responsive_and_accessible() -> None:
    container = dag_viewer.render_container(
        'unsafe" data-x="oops',
        script_href='scripts/viewer" onload="oops.js',
        fallback_links=(
            ("Unsafe <node>", 'roadmap/a" onclick="oops'),
            ("Unsafe protocol", "javascript:alert(1)"),
        ),
        fallback_total=5,
    )
    script = _viewer_source()

    assert 'data-graph-src="unsafe%22%20data-x%3D%22oops"' in container
    assert 'src="scripts/viewer%22%20onload%3D%22oops.js"' in container
    assert "Dependency explorer fallback." in container
    assert "Unsafe &lt;node&gt;" in container
    assert 'href="roadmap/a&quot; onclick=&quot;oops"' in container
    assert "javascript:alert" not in container
    assert "Showing 1 of 5 linked nodes" in container
    assert "<style data-autoform-dag-style>" in container
    assert "@media (max-width: 680px)" in container
    assert "@media (prefers-reduced-motion: reduce)" in container
    assert "100svh" in container
    assert "100dvh" in container
    assert 'refs.canvas.setAttribute("aria-hidden", "true")' in script
    assert 'refs.stage.setAttribute("role", "region")' in script
    assert 'toolbar.setAttribute("role", "group")' in script
    assert 'refs.navigation = element("aside", "bp-dag-navigation")' in script
    assert 'refs.navigation.setAttribute("aria-label", "Wiki hierarchy")' in script
    assert 'renderNavigation()' in script
    # Only the publisher's controlled formula service refreshes mathematics;
    # navigation clones the complete reading source rather than graph summaries.
    assert "typesetPromise" not in script
    assert "window.AutoformMath.refresh(container)" in script
    assert "window.AutoformMath.refresh(refs.readingBody)" in script
    assert "refs.readingBody.replaceChildren(bundle.readingSource)" in script
    assert "typeset(refs.navigationInner); refs.navigationInner.textContent" in script
    # Relation rows that Show more appends to the details are typeset in their list.
    assert 'more.addEventListener("click", function () { appendNextBatch(); typeset(list); });' in script
    assert 'refs.announcement.setAttribute("aria-live", "polite")' in script
    assert "control.dataset.autoformNodeId = node.id" in script
    assert "new ResizeObserver(resize)" in script
    assert "state.pointers.size === 2" in script
    assert "event.type === \"pointerup\" && wasSingle && !state.moved" in script
    assert "touch-action: none; cursor: grab;" in container
    assert 'url.origin === window.location.origin' in script
    assert 'w: node.width, h: node.height' in script
    assert 'spotlight.before.has(edge.source) && spotlight.before.has(edge.target)' in script
    assert 'spotlight.after.has(edge.source) && spotlight.after.has(edge.target)' in script
    # Global overview relationships remain visible; edge work still uses only
    # incident edges of the selected node or currently visible nodes.
    assert "if (atlas && state.selected)" in script
    assert "(incident.get(state.selected.id) || []).forEach" in script
    assert "visible.forEach(function (node) { (incident.get(node.id) || []).forEach" in script
    assert "if (!state.activeDirty) return" in script
    assert "if (query === state.searchCacheQuery) return state.searchCache" in script
    assert f'data.schema !== "{dag_viewer.SCHEMA}"' in script
    assert f'index.schema !== "{dag_viewer.SEARCH_SCHEMA}"' in script
    assert 'ctx.fillText("×" + count' in script
    # Ranking matches exact names before prefixes, and statement text last.
    assert script.index("if (title === query)") < script.index("if (title.startsWith(query))")
    assert script.index("if (lean === query)") < script.index("if (lean.startsWith(query))")
    assert script.index("if (lean.indexOf(query)") < script.index("if (summary.indexOf(query)")
    assert "terms.every(function (term) { return all.indexOf(term) >= 0; })" in script


def test_explorer_pages_relations_and_clamps_zoom() -> None:
    script = _viewer_source()

    assert "Math.min(visible + RELATION_PAGE, ids.length)" in script
    assert "ids.slice(visible, next)" in script
    assert "clamp(old * factor, MIN_SCALE, 4)" in script


def test_explorer_assets_and_ordinary_payload_stay_within_budgets(tmp_path: Path) -> None:
    nodes = tuple(
        ViewNode(
            id=f"chapter/node-{index}",
            title=f"Node {index}",
            kind="node",
            members=(f"chapter/node-{index}",),
            status_counts=(("planned", 1),),
            status_key="planned",
            lean="Project.NodeZero" if index == 0 else None,
            summary="A spectral gap statement." if index == 0 else None,
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

    # The reader, hierarchy rail and pinned layout engine share one prebuilt
    # response; consumers still need no application build or runtime CDN.
    assert len(dag_viewer.viewer_script().encode()) < 132_000
    assert output.stat().st_size < 300_000
    assert search.stat().st_size < 100_000
    search_payload = json.loads(search.read_text(encoding="utf-8"))
    assert search_payload["schema"] == "autoform-dag-search/v1"
    assert "layout" not in search_payload["nodes"][0]
    assert search_payload["nodes"][0]["lean"] == "Project.NodeZero"
    assert search_payload["nodes"][0]["summary"] == "A spectral gap statement."
    assert "lean" not in search_payload["nodes"][1]
    assert "summary" not in search_payload["nodes"][1]


def test_viewer_script_links_are_relative_to_the_rendered_site_root(tmp_path: Path) -> None:
    assert graph_pages._viewer_script_link(tmp_path / "dependencies.md", tmp_path) == "javascripts/blueprint-dag.js"
    assert graph_pages._viewer_script_link(tmp_path / "dependencies/full.md", tmp_path) == "../javascripts/blueprint-dag.js"
    assert (
        graph_pages._viewer_script_link(tmp_path / "dependencies/chapters/large.md", tmp_path)
        == "../../javascripts/blueprint-dag.js"
    )


def test_payload_layout_follows_dependencies_and_keeps_collapsed_cycles_stable(tmp_path: Path) -> None:
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

    def layout(view: GraphView) -> dict[str, tuple[int, int]]:
        output = tmp_path / f"{view.kind}.json"
        dag_viewer.write_payload(output, view, links={})
        payload = json.loads(output.read_text(encoding="utf-8"))
        return {node["id"]: (node["layout"]["x"], node["layout"]["y"]) for node in payload["nodes"]}

    assert layout(acyclic) == {"base": (0, 0), "result": (312, 0)}
    # A collapsed cycle has no topological order, so it keeps source order.
    assert layout(cyclic) == {"result": (0, 0), "base": (312, 0)}


def test_payload_rejects_duplicate_nodes_and_unknown_edges(tmp_path: Path) -> None:
    node = ViewNode("a", "A", "node", ("a",), (("planned", 1),))
    duplicate = GraphView(kind="full", title="Duplicate", nodes=(node, node), edges=())
    unknown = GraphView(
        kind="full",
        title="Unknown",
        nodes=(node,),
        edges=(ViewEdge("missing", "a", statement_count=1),),
    )

    with pytest.raises(ValueError, match="duplicate node ids"):
        dag_viewer.write_payload(tmp_path / "duplicate.json", duplicate, links={})
    with pytest.raises(ValueError, match="unknown node"):
        dag_viewer.write_payload(tmp_path / "unknown.json", unknown, links={})


def test_an_explorer_page_encodes_a_literal_percent_in_its_payload_name(tmp_path: Path) -> None:
    """render_container keeps % for hrefs that arrive encoded, so the page encodes the payload's name."""
    page = tmp_path / "100%25 results.md"
    node = ViewNode("a", "A", "node", ("a",), (("planned", 1),))

    graph_pages._write_page(
        page,
        view=GraphView(kind="chapter", title="Results", nodes=(node,), edges=()),
        statuses={},
        links={},
        heading="Results",
        lead="One result.",
        site_root=tmp_path,
        search_href="index.json",
        breadcrumbs=(),
    )

    assert (tmp_path / "100%25 results.json").is_file()
    assert 'data-graph-src="100%2525%20results.json"' in page.read_text(encoding="utf-8")


def test_interactive_graph_urls_encode_authored_path_delimiters() -> None:
    container = dag_viewer.render_container(
        "hash#chapter.json",
        script_href="../javascripts/blueprint-dag.js?cache#asset",
    )

    assert 'data-graph-src="hash%23chapter.json"' in container
    assert 'src="../javascripts/blueprint-dag.js%3Fcache%23asset"' in container
