from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from autoform_cli import dag_viewer, graph_pages, status
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
    # Contents counts what it lists, and names no internals such as the map's size limit.
    assert 'element("h2", "", "Contents")' in script
    assert "items without edges included" not in script and "map unavailable" not in script


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
    assert "autoform:typeset" not in script and "FORMULA_CLASS_JSON" not in script
    assert "window.AutoformMath.refresh(refs.readingBody)" in script
    assert "function readingLabel(" in script
    assert "refreshReadingLabels(refs.navigationInner)" in script
    assert "refreshReadingLabels(refs.readingNeighbors)" in script
    assert 'part[0] === "math" ? "arithmatex" : ""' in script
    assert "refs.readingBody.replaceChildren(bundle.readingSource)" in script
    assert "navigation.bindReader(refs.readingBody)" in script
    # All enhancement enters through the canonical complete-reader router.
    assert '!window.AutoformNavigation || !host.closest(".bp-explorer-page")' in script
    assert '!navigation || !firstBundle || !firstBundle.readingSource' in script
    assert "window.AutoformNavigation.create" in script
    assert "function appendRelations(" not in script and "function drawRegions(" not in script
    assert 'ui$relationIds = pair[1]' in dag_viewer._SCRIPT
    assert 'setMode("list", true)' in script
    assert 'refs.announcement.setAttribute("aria-live", "polite")' in script
    assert "control.dataset.autoformNodeId = node.id" in script
    assert "new ResizeObserver(resize)" in script
    assert "state.pointers.size === 2" in script
    assert "event.type === \"pointerup\" && wasSingle && !state.moved" in script
    assert "touch-action: none; cursor: grab;" in container
    assert 'url.origin === window.location.origin' in script
    assert "return geometry().nodes.get(node.id)" in script
    assert 'spotlight.before.has(route.source) && spotlight.before.has(route.target)' in script
    assert 'spotlight.after.has(route.source) && spotlight.after.has(route.target)' in script
    # The single bounded scene paints the authoritative routes, not a second grid projection.
    assert "current.routes.forEach" in script and "if (!current) return" in script
    assert "if (!state.activeDirty) return" in script
    assert "if (query === state.searchCacheQuery) return state.searchCache" in script
    assert f'data.schema !== "{dag_viewer.SCHEMA}"' in script
    assert f'INDEX_SCHEMA = "{dag_viewer.SEARCH_SCHEMA}"' in script
    assert 'ctx.fillText("×"' not in script
    # Ranking matches exact names before prefixes, and statement text last.
    assert script.index("if (title === query)") < script.index("if (title.startsWith(query))")
    assert script.index("if (lean === query)") < script.index("if (lean.startsWith(query))")
    assert script.index("if (lean.indexOf(query)") < script.index("if (summary.indexOf(query)")
    assert "terms.every(function (term) { return all.indexOf(term) >= 0; })" in script
    # Search ranks exact, then prefix, then substring matches of title, id and Lean name, and says nothing of why.
    assert "if (title === query) return 1; if (id === query) return 2; if (lean === query) return 3;" in script
    assert "if (title.startsWith(query)) return 4; if (id.startsWith(query)) return 5;" in script
    assert "if (lean.indexOf(query) >= 0) return 10; if (summary.indexOf(query) >= 0) return 11;" in script
    assert "Exact Lean name" not in script and "bp-dag-result-reason" not in script


def test_explorer_counts_agree_with_their_nouns() -> None:
    """Integrated result/source counts and typed relation descriptions use the same plural formatter."""
    script = _viewer_source()

    assert 'return count.toLocaleString() + " " + (count === 1 ? one : many);' in script
    assert 'plural(counts.statement, "statement dependency", "statement dependencies")' in script
    assert 'plural(counts.proof, "proof dependency", "proof dependencies")' in script
    assert 'ui$mode === "sources" ? "source"' in dag_viewer._SCRIPT
    assert '.toLocaleString() + " dependencies"' not in script


def test_card_meta_counts_items_in_a_readable_colour() -> None:
    """Topic cards read "1 item · 0 internal links" at three-quarter opacity, jargon in a faded grey."""
    script = _viewer_source()
    assert 'var scopeMeta = node.kind === "scope" ?' in script
    assert 'plural(node.member_count || 0, "item", "items")' in script
    assert "internal link" not in script
    rule = re.search(r"\.bp-dag-node-meta \{([^}]*)\}", dag_viewer._STYLE)
    assert rule is not None and "opacity" not in rule.group(1) and "font-size: 12px" in rule.group(1)
    share = re.search(r"color: color-mix\(in srgb, var\(--dag-node-text\) (\d+)%, var\(--dag-node-fill\)\)", rule.group(1))
    assert share is not None
    weight = int(share.group(1)) / 100

    def mixed(text: str, fill: str) -> str:
        pairs = ((int(text[index:index + 2], 16), int(fill[index:index + 2], 16)) for index in (1, 3, 5))
        return "#" + "".join(f"{round(a * weight + b * (1 - weight)):02x}" for a, b in pairs)

    below = set()
    for state in (*status.STATES, *status.INVENTORY_STATES):
        for text, fill in ((state.text, state.fill), (state.dark_text, state.dark_fill)):
            if _contrast(text, fill) < 4.5:
                # The title on this card falls short too; no mix of its colours can do better.
                below.add(state.key)
            else:
                assert _contrast(mixed(text, fill), fill) >= 4.5, state.key
    assert below == {"fully_proved"}


def test_rail_rows_show_a_status_swatch_and_a_lone_group_no_heading() -> None:
    """The rail is the semantic scope outline; leaf status belongs to result cards and search."""
    script = _viewer_source()

    assert 'element("h2", "bp-outline-label", "Topics")' in script
    assert 'node.scope === node.id && ["collection", "transparent", "source"].indexOf(node.navigation) < 0' in script
    assert 'link.title = node.title; link.dataset.navigationNodeId = node.id;' in script
    assert "function renderNavigation() {" in script and "renderOutline()" in script
    assert "bp-dag-navigation-group" not in script
    assert '(node.member_count || 0).toLocaleString() : statusLabel(node.status)' not in script


def test_explorer_shows_statuses_and_areas_but_never_internal_ids() -> None:
    """Cards, the details, Contents and search all printed ids such as "algebra/base" beside the titles."""
    script = _viewer_source()
    style = dag_viewer.render_container("graph.json")

    assert 'element("span", "bp-dag-node-meta", (node._region || node._overviewGroup) ? plural(node._topics, "topic", "topics")' in script
    assert ': scopeMeta || statusLabel(node.status))' in script
    assert 'element("span", "bp-dag-inventory-meta", statusLabel(node.status) + (node.area ? " · " + node.area : ""))' in script
    assert 'element("span", "bp-dag-result-meta", match.node.area || statusLabel(match.node.status))' in script
    for gone in ("bp-dag-node-id", "bp-dag-inventory-id", "bp-dag-result-id"):
        assert gone not in script and gone not in style


def test_details_close_in_their_corner_and_space_the_status_from_the_link() -> None:
    """Already-collapsible readers have a native corner close control and one complete source."""
    script, style = _viewer_source(), dag_viewer._STYLE

    close = re.search(r"\.bp-dag-sheet-open \.bp-dag-sheet-handle \{([^}]*)\}", style)
    assert close is not None
    for declaration in ("position: absolute;", "top: 12px;", "right: 12px;", "width: 32px;", "height: 32px;"):
        assert declaration in close.group(1)
    assert ".bp-dag-sheet-open .bp-reading-body h1 { padding-right: 32px; }" in style
    assert "titleWrap" not in script
    assert 'refs.sheetHandle.title = opened ? "Close reading" : "Open reading";' in script
    assert 'refs.sheetHandle.setAttribute("aria-expanded", String(Boolean(open)))' in script
    assert 'element("span", "bp-dag-status-pill",' not in script
    assert 'element("article", "bp-reading-body md-typeset")' in script
    assert 'refs.readingBody.replaceChildren(bundle.readingSource)' in script


def test_details_drop_instructions_and_label_paths_compactly() -> None:
    """The drawer explained its own controls, invented a summary for topics without one and titled its pivots."""
    from tests.test_wiki_workspace_controls import _run

    script, style = _viewer_source(), dag_viewer._STYLE

    for gone in (
        "keyboard-accessible",
        "Click a topic",
        "This topic contains",
        "Explore it to see",
        "Card height shows scope",
        "Path spotlight",
        "All paths",
        '"Roadmap item"',
        '"Dependency explorer"',
        '"Mathematics knowledge map"',
    ):
        assert gone not in script, gone
    assert '", no dependency edges in this view"' in script
    assert '[["Prerequisites", links.before], ["Used by", links.after]]' in script
    _run(r"""
      configure([focus]);renderReaderContext();
      assert.equal(marked(dom$readingNeighbors,'bp-reading-links'),undefined,
        'An edge-free result must not acquire empty prerequisite/dependent columns');
      const base=record('B','chapter',{title:'Actual prerequisite'});
      configure([focus,base],[['B','F',2,3]]);renderReaderContext();
      const row=marked(dom$readingNeighbors,'bp-reading-links');assert(row);
      assert(row.textContent.includes('Prerequisites · 1'));assert(row.textContent.includes('Used by · 0'));
      assert.equal(links(row).length,1);assert.equal(links(row)[0].textContent,'Actual prerequisite');
    """)
    assert "function appendRelations(" not in script and "bp-dag-pivots" not in script
    assert ".bp-reading-links { display: grid; grid-template-columns: 1fr 1fr;" in style


def test_a_click_pans_its_node_clear_of_the_details_it_opens() -> None:
    """Every other way of choosing a node placed it beside the drawer; a click left it under the drawer."""
    script = _viewer_source()

    assert 'settings = {history: "push", focus: "reading"}' in script
    assert 'navigation.navigateNode(id, settings)' in script
    assert "if (state.selected && mapAllowed) centerIfHidden(state.selected)" in script
    center = script.split("function center(", 1)[1].split("\n    function ", 1)[0]
    assert "free = freeArea()" in center
    hidden = script.split("function centerIfHidden(node) {", 1)[1].split("\n    function ", 1)[0]
    assert "refs.stage.clientWidth - 12" in hidden and "refs.stage.clientHeight - 12" in hidden
    assert "function selectNode(" not in script


def test_explorer_pages_relations_and_clamps_zoom() -> None:
    script = _viewer_source()

    assert 'pair[1].slice(0, 5)' in script
    assert '"Show all " + pair[1].length' in script
    assert 'nodes.slice(0, state.listLimit)' in script
    assert 'Math.min(LIST_PAGE, nodes.length - visible.length)' in script
    assert "clamp(old * factor, MIN_SCALE, 4)" in script


def test_explorer_styles_keep_one_family_one_type_scale_and_one_micro_label() -> None:
    """The rail and drawer headings were set in Georgia, among twenty rem sizes and unloaded weights."""
    style = dag_viewer._STYLE
    assert "serif" not in style.replace("sans-serif", "")
    # Integrated reading and region cards add an 18px title; !important only protects it from Material.
    assert set(re.findall(r"font-size: (\d+)px(?: !important)?;", style)) <= {"11", "12", "13", "14", "17", "18", "22"}
    assert set(re.findall(r"font-weight: ([^;]+);", style)) <= {"400", "600", "700"}
    uppercase = [rule.split("{")[0].strip() for rule in style.splitlines() if "text-transform: uppercase" in rule]
    assert uppercase == [
        ".bp-dag-filter-popover legend, .bp-dag-viewer.bp-dag-viewer .bp-dag-navigation-group h2",
    ]


def test_explorer_names_its_scope_once_and_lists_only_its_ancestors_above_it() -> None:
    """A kind pill, a rail kicker, the h1, the trail's last crumb and a home link all named the scope or the book."""
    script, style = _viewer_source(), dag_viewer._STYLE
    for retired in ("bp-dag-view-kind", "bp-dag-navigation-kicker", "bp-dag-navigation-home"):
        assert retired not in script and retired not in style
    assert "Current scope" not in script and "Library atlas" not in script
    assert 'refs.breadcrumb.replaceChildren(trailList(trail))' in script
    assert 'refs.pageHeading.textContent = currentBundle && currentBundle.title || data.title;' in script
    assert 'element("h1", "bp-dag-navigation-title"' not in script
    assert 'link.setAttribute("aria-current", "page")' in script
    assert ":is(.bp-dag-breadcrumb, .bp-dag-navigation-path) li + li::before { content: \"/\";" in style


def test_explorer_chrome_is_sized_in_px_not_in_materials_growing_rem() -> None:
    """Material's root font is 20px, 22px from 1600px, so a rem rail was 407px wide at 1600 by 900."""
    style = dag_viewer._STYLE
    assert not re.findall(r"\d+(?:\.\d+)?rem\b", style)
    assert "--dag-navigation-width: 288px;" in style
    # Beside the 288px rail the 12px-inset 384px drawer leaves 405px from 1101px; below, it covers the rail.
    assert "width: min(384px, calc(100% - 24px));" in style
    assert "@media (max-width: 1100px) {\n  .bp-dag-viewer { --dag-navigation-width: 248px; }" in style
    assert "left: calc(408px - var(--dag-navigation-width));" in style


def test_explorer_head_is_one_row_where_the_rail_always_shows() -> None:
    """One persistent context/Library row precedes the responsive control row; no legacy book-link reparenting."""
    style = dag_viewer._STYLE
    script = _viewer_source()
    assert ".bp-dag-integrated .bp-dag-head { display: block; padding: 0; }" in style
    assert ".bp-dag-integrated .bp-dag-context { display: flex;" in style
    breadcrumb = next(rule for rule in style.splitlines() if rule.startswith(".bp-dag-integrated .bp-dag-breadcrumb {"))
    declarations = {
        name.strip(): value.strip()
        for part in breadcrumb.split("{", 1)[1].rstrip(" }").split(";") if part.strip()
        for name, _, value in (part.partition(":"),)
    }
    assert declarations["display"] == "block"
    assert declarations["visibility"] == "visible !important"
    assert declarations["max-width"] == "none", "The integrated context must not inherit the legacy 32% breadcrumb limit"
    assert ".bp-dag-integrated .bp-dag-toolbar { display: flex;" in style
    assert "context.insertBefore(bookLink, crumbs)" in script
    assert "placeBook" not in script and "head.insertBefore(bookLink" not in script
    narrow = style.split("@media (max-width: 1100px) {\n", 2)[2].split("\n}", 1)[0]
    assert ".bp-dag-integrated .bp-dag-toolbar { flex-wrap: wrap; }" in narrow
    assert ".bp-dag-integrated .bp-dag-search-wrap { flex-basis: 100%; }" in narrow
    # The existing vertical compensation remains atomic with both class changes.
    sheet = script.split("function setSheet(open, selected) {", 1)[1].split("\n    function ", 1)[0]
    assert 'var top = refs.body.offsetTop;' in sheet
    assert 'host.classList.toggle("bp-dag-sheet-open", Boolean(open));' in sheet
    assert 'host.classList.toggle("bp-dag-has-selection", Boolean(selected));' in sheet
    assert "state.transform.y += top - refs.body.offsetTop; schedule();" in sheet


def test_explorer_drops_the_map_guide_and_keeps_a_plain_note_for_real_crops() -> None:
    """A pill over the map said to select a topic, or restated the selection the drawer and the dimming show."""
    script, style = _viewer_source(), dag_viewer._STYLE
    assert "bp-dag-guide" not in script and "bp-dag-guide" not in style
    assert "dependency neighborhood" not in script
    # The density note stays, only for a readable crop or for labels hidden by zoom, as plain text.
    assert 'state.readableCrop ? "Pan or Browse to see all "' in script and '"Zoom in to reveal "' in script
    density = next(rule for rule in style.splitlines() if rule.startswith(".bp-dag-density {"))
    assert "left: 16px; bottom: 12px;" in density and "pointer-events: none;" in density
    # On phones it clears the collapsed sheet's 52px peek and 8px inset by 8px.
    phones = style.split("@media (max-width: 680px) {\n", 1)[1].split("\n}", 1)[0]
    assert ".bp-dag-density { bottom: 68px; }" in phones
    assert not any(word in density for word in ("border", "shadow", "background", "blur"))


def test_explorer_toolbar_counts_only_hidden_items_and_files_the_edge_toggle_under_status() -> None:
    """The toolbar always counted what it showed, and the toggle for items without edges sat in it."""
    script, style = _viewer_source(), dag_viewer._STYLE
    assert "bp-dag-stats" not in script and "bp-dag-stats" not in style and '" shown"' not in script
    assert 'refs.hiddenButton.textContent = "Show " + hidden + " hidden"; refs.hiddenButton.hidden = !hidden;' in script
    assert '"Include items without edges"' in script and "Show items without edges" not in script
    assert "refs.filterPopover.appendChild(fieldset); refs.filterPopover.appendChild(edges);" in script
    # Map | Contents and the zoom buttons are borderless ghost buttons inside one 1px group each.
    assert (
        ".bp-dag-zoom, .bp-dag-modes { display: inline-flex; flex: none; gap: 2px; padding: 2px; "
        "border: 1px solid var(--dag-border);"
    ) in style
    assert ".bp-dag-zoom .bp-dag-button, .bp-dag-modes .bp-dag-mode-button { min-height: 30px; padding: 4px 10px; border: 0;" in style


def test_explorer_search_draws_its_magnifier_and_shortcut_hint_in_css() -> None:
    """U+2315 is not in Plus Jakarta Sans and sat off the box's line, and "( / )" read as debug output."""
    script, style = _viewer_source(), dag_viewer._STYLE
    assert "⌕" not in style and "( / )" not in script
    assert 'refs.search.placeholder = "Search the roadmap";' in script
    # A currentColor square masked by an inline SVG draws the magnifier in the muted text colour.
    assert "background: currentColor; color: var(--dag-muted);" in style
    assert "-webkit-mask: var(--dag-magnifier) center / 14px no-repeat; mask: var(--dag-magnifier)" in style
    assert '.bp-dag-search-wrap::after { content: "/";' in style
    # Apart from the :has() rule, so a browser without :has() still hides the hint on focus.
    assert ".bp-dag-search-wrap:focus-within::after { display: none; }\n" in style
    assert "\n.bp-dag-search-wrap:has(.bp-dag-search:not(:placeholder-shown))::after { display: none; }" in style
    phones = style.split("@media (max-width: 680px) {", 1)[1]
    assert ".bp-dag-search-wrap::after { display: none; }" in phones


def test_explorer_elevation_is_two_shadows_and_a_selection_ring_that_keeps_the_card_still() -> None:
    """A selected card took a 3px border, shifting its text, and blurred panels sat on five shadows."""
    style = dag_viewer._STYLE
    assert "backdrop-filter" not in style
    assert set(re.findall(r"box-shadow: ([^;]+);", style)) <= {
        "var(--dag-e1)",
        "var(--dag-e2)",
        "none",
        "0 0 0 2px var(--dag-link), var(--dag-e1)",
        "0 0 0 2px color-mix(in srgb, var(--dag-link) 40%, transparent), var(--dag-e1)",
        "0 0 0 3px color-mix(in srgb, var(--dag-link) 18%, transparent)",
        "inset 3px 0 0 var(--dag-link)",
    }
    assert ".bp-dag-node[data-selected=true] { box-shadow: 0 0 0 2px var(--dag-link), var(--dag-e1); }" in style
    assert "border-width" not in style


def test_explorer_labels_use_the_site_family_and_remain_readable_at_initial_scale() -> None:
    """Canvas labels and integrated DOM cards stay readable; weights need no text labels."""
    script = _viewer_source()
    assert script.count('refs.ctx.font = "10px sans-serif";') == 1  # Parse probe only, never a painted label.
    assert 'family = getComputedStyle(host).fontFamily; refs.ctx.font = "11px " + family;' in script
    assert 'if (refs.ctx.font.indexOf("11px") < 0) family = "sans-serif";' in script
    # The integrated map uses DOM titles/statuses, not painted numeric weights.
    # Their actual published CSS must remain readable at the .9 initial floor.
    rules = re.findall(r"\.bp-dag-integrated(?:\.bp-dag-atlas)? \.bp-dag-node-(?:title|meta) \{([^}]+)\}", dag_viewer._STYLE)
    sizes = [float(size) for rule in rules for size in re.findall(r"font-size:\s*([\d.]+)px", rule)]
    assert len(sizes) >= 3 and min(sizes) * .9 >= 11
    painter = script.split("function drawRoute(", 1)[1].split("function updateOverlays(", 1)[0]
    assert "fillText" not in painter and "measureText" not in painter
    assert 'readableFloor = .9;' in script


def _contrast(first: str, second: str) -> float:
    def luminance(colour: str) -> float:
        channels = [int(colour.lstrip("#")[index : index + 2], 16) / 255 for index in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_dark_explorer_cards_read_against_the_page_toned_map() -> None:
    """Outlined cards were filled with the dark map's own #18191A, and planned's stroke had 2.1:1 against it."""
    style = dag_viewer._STYLE
    page, surface = "#18191A", "#242526"
    assert "--dag-map: #18191a;" in style and ".bp-dag-main {" in style and "background: var(--dag-map); }" in style
    assert all(state.dark_fill.upper() != page for state in (*status.STATES, *status.INVENTORY_STATES))
    planned = next(state for state in status.STATES if state.key == "planned")
    assert min(_contrast(planned.dark_stroke, page), _contrast(planned.dark_stroke, surface)) >= 3
    # White on the dark link blue, #2D88FF, is 3.5:1; the Open button takes Meta blue there.
    assert "[data-md-color-scheme=slate] .bp-dag-open { background: #0064e0; }" in style
    assert _contrast("#FFFFFF", "#0064E0") >= 4.5
    assert _contrast("#FF7B72", surface) >= 4.5 and "--dag-error: #ff7b72;" in style


def test_status_ring_greys_stay_apart_in_both_schemes() -> None:
    """Planned's dark stroke rose to #737578, 1.39:1 from inventory pending's #8A8D91, so their arcs ran together."""
    planned = next(state for state in status.STATES if state.key == "planned")
    greys = (planned, *status.INVENTORY_STATES)
    # A ring runs planned, checked and pending inventory and closes, so each pair of the three can meet.
    # 1.5:1 keeps neighbouring 3px arcs a visible step apart; the old dark pair was 1.39:1.
    for first, second in ((0, 1), (1, 2), (0, 2)):
        for stroke in ("stroke", "dark_stroke"):
            ratio = _contrast(getattr(greys[first], stroke), getattr(greys[second], stroke))
            assert ratio >= 1.5, (greys[first].key, greys[second].key, stroke, ratio)
    for state in greys:
        assert min(_contrast(state.dark_stroke, state.dark_fill), _contrast(state.dark_stroke, "#18191A")) >= 3, state.key


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
