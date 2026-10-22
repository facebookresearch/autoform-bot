from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from autoform_cli import dag_viewer, graph_pages, render, status
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
    layout = {node["id"]: node["layout"] for node in payload["nodes"]}

    assert (layout["root"]["column"], layout["root"]["row"]) == (0, 0)
    assert (layout["result"]["column"], layout["result"]["row"]) == (1, 0)
    # The isolates fill rows under the chain, from its left edge, rather than columns beside it.
    assert (layout["isolated-0"]["column"], layout["isolated-0"]["row"]) == (0, 2)
    assert all(layout[f"isolated-{index}"]["row"] >= 2 for index in range(100))
    assert max(layout[f"isolated-{index}"]["column"] for index in range(100)) >= 1


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
    assert '"Show unlinked"' in script
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
    assert 'spotlight.before.has(source) && spotlight.before.has(target) ? 1' in script
    assert 'spotlight.after.has(source) && spotlight.after.has(target) ? 2' in script
    # The area overview and the atlas draw no links at rest; a selected, hovered
    # or focused card shows only its own. Elsewhere edge work still uses only
    # the incident edges of the currently visible nodes.
    assert "subject = state.selected || state.hovered" in script
    assert "if (atlas && subject)" in script
    assert "(incident.get(subject.id) || []).forEach" in script
    assert "} else if (!quiet) {" in script
    assert "visible.forEach(function (node) { (incident.get(node.id) || []).forEach" in script
    assert "quiet && !edgeIndexes.has(route.edgeIndex)) return;" in script
    # The compact overview, where a tap opens an area and nothing hovers, keeps its links at rest.
    assert "var quiet = navigation ? !compactRegions() && data.nodes.every(" in script
    assert "if (!state.activeDirty) return" in script
    assert "if (query === state.searchCacheQuery) return state.searchCache" in script
    assert f'data.schema !== "{dag_viewer.SCHEMA}"' in script
    assert f'index.schema !== "{dag_viewer.SEARCH_SCHEMA}"' in script
    # Counts are words on a pill, drawn only on a link the selection lights.
    assert 'var text = count + " links"' in script and '"×" + ' not in script
    assert "if (count > 1 && role > 0 && Math.hypot(bx - ax, by - ay) > 90) pill(" in script
    assert "if (label && role > 0) pill(ctx, route.count," in script
    assert 'ctx.setLineDash([6, 4])' in script and "setLineDash([5, 5])" not in script
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


def test_resting_a_mouse_on_a_card_previews_its_neighbours_without_selecting_it() -> None:
    """Nothing on the map reacted before a click, so seeing what a card links to meant opening it."""
    script = _viewer_source()
    start = script.index('["pointerenter", "focus", "pointerleave", "blur"].forEach')
    hover = script[start:script.index('control.addEventListener("keydown"', start)]
    # A mouse waits 120 ms; keyboard focus and leaving act on the next tick, and a touch, which has no hover, sets nothing.
    # Focus the explorer moves itself after a click is not keyboard focus, so it previews nothing.
    assert "clearTimeout(hoverTimer);" in hover
    assert ('if (index - 1 ? index || event.pointerType !== "touch" : control.matches(":focus-visible")) hoverTimer = setTimeout('
            'function () { state.hovered = index < 2 && node; schedule(); }, index ? 0 : 120);') in hover
    # A hovered card that leaves the map (zoom, filter, a new projection, a late timer) gets no pointerleave, so the redraw drops it.
    assert "if (state.hovered && !overlays.has(state.hovered.id)) { state.hovered = null; schedule(); }" in script
    # The preview only draws: no selection, URL or history change.
    assert not re.search(r"select|history|location|remember|activate", hover)
    # Any map previews the direct neighbours; a selection keeps its own spotlight.
    assert "var spotlight = (atlas || !state.selected) && subject ? atlasSpotlight(subject) : pathSpotlight();" in script
    assert "return state.spotlight || (state.spotlight = spotlightOf(collect(state.selected.id, prerequisites), collect(state.selected.id, dependents)));" in script
    # The cards fade briefly, and not at all for a reader who asks for reduced motion.
    style = dag_viewer._STYLE
    assert "will-change: transform; transition: opacity .15s ease, filter .15s ease; }" in style
    reduced = style[style.index("@media (prefers-reduced-motion: reduce) {"):]
    assert ".bp-dag-node { will-change: auto; transition: none; }" in reduced[:reduced.index("\n}")]


def test_a_press_on_a_card_pans_once_it_travels_and_otherwise_stays_a_tap() -> None:
    """A press on a card was dropped, so a drag or pinch over the cards, most of a phone's map, did nothing.

    Capturing the pointer on the press would send a tap's click to the stage, so the card would never open.
    """
    script = _viewer_source()
    down = script[script.index("function pointerDown(event)"):script.index("function pinchState()")]
    assert ".bp-dag-node" not in down and "setPointerCapture" not in down.split("function grab()")[0]
    assert "if (state.pointers.size === 2) { state.pinch = pinchState(); grab(); }" in down
    assert "state.moved = true;" in down and "refs.stage.setPointerCapture(id)" in down
    move = script[script.index("function pointerMove(event)"):script.index("function pointerUp(event)")]
    assert "if (!state.moved) { if (Math.hypot(dx, dy) <= 8) return; grab(); }" in move
    # A press released off the stage sends no pointerup here; the next move with no button down forgets it instead of panning.
    assert move.index("if (!event.buttons) state.pointers.delete(event.pointerId);") < move.index("var previous")
    assert move.index("grab();") < move.index("state.pointers.set(")
    assert '!state.moved && state.selected && !event.target.closest(".bp-dag-node")) clearSelection(true)' in script
    assert "event.stopPropagation(); if (!state.moved || !event.detail) activateNode(node, false);" in script
    # A tap leaves the history to the card it opens; only a pan or pinch checkpoints the camera.
    assert "if (state.moved) remember();" in script[script.index("function pointerUp(event)"):]


def test_the_phone_reader_peeks_opens_to_half_or_covers_the_map() -> None:
    """The phone reader was a 48px "Reading" bar or covered the whole map, and only a click on it worked.

    Half and full both count as open, so history keeps its readingOpen boolean.
    """
    script, style = _viewer_source(), dag_viewer._STYLE
    phones = style.split("@media (max-width: 720px) {\n  .bp-dag-integrated", 1)[1].split("\n}", 1)[0]
    assert "transform: translateY(calc(100% - 56px));" in phones
    assert ".bp-dag-viewer.bp-dag-integrated.bp-dag-sheet-open .bp-reading-pane { transform: translateY(45%); }" in phones
    assert ".bp-dag-viewer.bp-dag-integrated.bp-dag-sheet-full .bp-reading-pane { transform: none; }" in phones
    # At half the reader stops at the screen's bottom, so its end can be scrolled into view.
    assert ".bp-dag-sheet-open:not(.bp-dag-sheet-full) .bp-dag-inspector-inner { height: calc(55% - 56px); }" in phones
    assert ".bp-dag-integrated.bp-dag-sheet-full .bp-dag-toolbar { display: none; }" in phones
    assert ".bp-dag-integrated.bp-dag-sheet-full .bp-reading-map { display: block; position: absolute;" in phones
    assert "touch-action: none;" in next(rule for rule in phones.splitlines() if ".bp-dag-sheet-handle {" in rule)
    # Wider screens never show the pill, even when a resized phone left the reader at full.
    assert ".bp-reading-map { display: none; }\n@media (min-width: 721px) {" in style
    sheet = script.split("function setSheet(open, selected) {", 1)[1].split("\n    function ", 1)[0]
    assert 'host.classList.toggle("bp-dag-sheet-full", open > 1 || open === true && host.classList.contains("bp-dag-sheet-full"));' in sheet
    assert "readingOpen: host.classList.contains(\"bp-dag-sheet-open\")" in script
    # A tap cycles peek, half and full; a drag snaps to the nearest, and its click is not a second tap.
    assert '(open + host.classList.contains("bp-dag-sheet-full") + 1) % 3 : !open' in script
    assert "if (dragged && event.detail) return;" in script
    assert "setSheet(top < height * .225 ? 2 : top < height * .725 - 28 ? 1 : 0, state.selected);" in script
    assert 'var mapButton = button("bp-reading-map", "Map", "Show the map");' in script
    # The handle names the page with its status, and a chosen node is kept above the half sheet.
    assert "refs.sheetHandle.replaceChildren(record && record.status ? swatch(record.status) : \"\", title);" in script
    # Its accessible name starts with the title it shows.
    assert 'refs.sheetHandle.setAttribute("aria-label", title + ", reading");' in script and "Toggle mathematical reading" not in script
    assert "navigation ? drawer.clientHeight * .45 : drawer.offsetTop" in script
    assert 'navigation && sheetLayout() && host.classList.contains("bp-dag-sheet-open") ? freeArea().band : refs.stage.clientHeight;' in script
    assert "box.y + box.h > (free.top || 0) + free.band - 12) center(node, .82);" in script


def test_explorer_counts_agree_with_their_nouns() -> None:
    """The rail read "1 items · 1 dependencies"."""
    script = _viewer_source()

    assert 'return count.toLocaleString() + " " + (count === 1 ? one : many);' in script
    assert 'plural(data.edge_count || data.edges.length, "dependency", "dependencies")' in script
    assert '.toLocaleString() + " dependencies"' not in script


def test_card_meta_counts_items_in_a_readable_colour() -> None:
    """Topic cards read "1 item · 0 internal links" at three-quarter opacity, jargon in a faded grey."""
    script = _viewer_source()
    assert 'scopeMeta = node.kind === "scope" ?' in script
    # A topic counts the articles inside it, the noun the book uses for them.
    assert 'plural(node.member_count || 0, "article", "articles")' in script
    assert "internal link" not in script
    rule = re.search(r"\.bp-dag-node-meta \{([^}]*)\}", dag_viewer._STYLE)
    assert rule is not None and "opacity" not in rule.group(1)
    # 12px at rest, and counter-scaled so it renders at 11px or more at the .8 and .68 zoom floors.
    assert "font-size: clamp(12px, calc(11px / var(--dag-k, 1)), 17px);" in rule.group(1)
    assert 'refs.nodeLayer.style.setProperty("--dag-k", state.transform.scale);' in script
    share = re.search(r"color: color-mix\(in srgb, var\(--dag-node-text\) (\d+)%, var\(--dag-node-fill\)\)", rule.group(1))
    assert share is not None
    weight = int(share.group(1)) / 100

    def mixed(text: str, fill: str) -> str:
        pairs = ((int(text[index:index + 2], 16), int(fill[index:index + 2], 16)) for index in (1, 3, 5))
        return "#" + "".join(f"{round(a * weight + b * (1 - weight)):02x}" for a, b in pairs)

    # White on light fully proved green was 3.3:1; finished fills are tints now, so every label reads.
    for state in (*status.STATES, *status.INVENTORY_STATES):
        for text, fill in ((state.text, state.fill), (state.dark_text, state.dark_fill)):
            assert _contrast(mixed(text, fill), fill) >= 4.5, state.key


def test_rail_rows_show_a_status_swatch_and_a_lone_group_no_heading() -> None:
    """Leaf rows ended in status phrases of every length, and a rail of one group still named it."""
    script = _viewer_source()

    assert 'if (groups.size > 1) section.appendChild(element("h2", "", label));' in script
    assert 'mark.title = statusLabel(node.status); mark.setAttribute("role", "img"); mark.setAttribute("aria-label", mark.title);' in script
    assert '(node.member_count || 0).toLocaleString() : statusLabel(node.status)' not in script


def test_explorer_shows_statuses_and_areas_but_never_internal_ids() -> None:
    """Cards, the details, Contents and search all printed ids such as "algebra/base" beside the titles."""
    script = _viewer_source()
    style = dag_viewer.render_container("graph.json")

    assert 'var lead = node._region ? plural(node._topics, "topic", "topics") : scopeMeta ? "" : kind, tail = scopeMeta || statusLabel(node.status);' in script
    assert 'var meta = node._region || !lead ? tail : lead + " · " + tail;' in script
    assert 'line.appendChild(element("span", "", (lead ? " · " : "") + tail));' in script
    assert 'element("span", "bp-dag-inventory-meta", statusLabel(node.status) + (node.area ? " · " + node.area : ""))' in script
    assert 'element("span", "bp-dag-result-meta", match.node.area || statusLabel(match.node.status))' in script
    for gone in ("bp-dag-node-id", "bp-dag-inventory-id", "bp-dag-result-id"):
        assert gone not in script and gone not in style


def test_details_close_in_their_corner_and_space_the_status_from_the_link() -> None:
    """The close button floated beside the kicker, the status touched Open article, and the sheet named the title twice."""
    script, style = _viewer_source(), dag_viewer._STYLE

    close = re.search(r"\.bp-dag-close \{([^}]*)\}", style)
    assert close is not None
    for declaration in ("position: absolute;", "top: 10px;", "right: 10px;", "width: 32px;", "height: 32px;"):
        assert declaration in close.group(1)
    # The header text stops short of the 32px button 10px from the edge.
    assert ".bp-dag-inspector-header { padding-right: 32px; }" in style
    assert "titleWrap" not in script
    actions = re.search(r"\.bp-dag-actions \{([^}]*)\}", style)
    assert actions is not None and "flex-wrap: wrap;" in actions.group(1) and "gap: 8px 16px;" in actions.group(1)
    assert 'element("span", "bp-dag-status-pill",' in script
    assert "actions.appendChild(mark);" in script and "actions.appendChild(open);" in script
    phone = style.split("@media (max-width: 680px) {\n", 1)[1].split("\n}", 1)[0]
    # The handle repeats the selection's title, so the sheet hides its heading but keeps it for assistive technology.
    assert ".bp-dag-viewer.bp-dag-viewer .bp-dag-inspector-header h2 { position: absolute; width: 1px;" in phone
    assert "padding: 10px 48px 8px 12px;" in phone
    assert ".bp-dag-viewer:not(.bp-dag-sheet-open) .bp-dag-close { display: none; }" in phone


def test_details_drop_instructions_and_label_paths_compactly() -> None:
    """The drawer explained its own controls, invented a summary for topics without one and titled its pivots."""
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
    # Human review requires a level without authored edges to say so.
    assert '"No dependencies are authored between these topics."' in script
    assert 'if (node.declaration) header.appendChild(element("p", "bp-dag-kicker",' in script
    # The lists come first in one vocabulary; the pivots below them only steer the map, so a side without relations is left out.
    assert 'appendRelations("Uses", before); appendRelations("Used by", after);' in script
    assert script.index('appendRelations("Used by", after);') < script.index('caption = "Highlight on map"')
    assert '[["All", "both", 1], ["Uses", "prerequisites", before.length], ["Used by", "dependents", after.length]]' in script
    assert "if (!entry[2]) return;" in script and "pivot.disabled" not in script
    # Atlas cards name a neighbour by the list it sits in; rows carry the map's status dot; the kicker reads in sentence case.
    assert 'relation === "prerequisite" ? "Uses" : relation === "dependent" ? "Used by" : ""' in script
    assert '"Prerequisite"' not in script and '"Dependent"' not in script
    assert "choose.prepend(swatch(related.status));" in script
    assert ".bp-dag-relation-button .bp-dag-swatch { display: inline-block;" in style
    assert ".bp-dag-kicker::first-letter { text-transform: uppercase; }" in style
    assert ".bp-dag-inspector-header h2 { margin: 0; color: var(--dag-fg);" in style
    for gone in ("Direct prerequisites", "Direct dependents", '"Dependents "', '"Paths"', ".bp-dag-pivot:disabled", ".bp-dag-pivot:not([aria-pressed=true], :disabled)"):
        assert gone not in script + style, gone
    pivots = re.search(r"\.bp-dag-pivots \{([^}]*)\}", style)
    assert pivots is not None and "display: flex;" in pivots.group(1) and "border: 1px solid" in pivots.group(1)


def test_a_click_pans_its_node_clear_of_the_details_it_opens() -> None:
    """Every other way of choosing a node placed it beside the drawer; a click left it under the drawer."""
    script = _viewer_source()

    assert "if (moveCamera) center(node, .82); else reveal(node);" in script
    # Centring and revealing share the drawer's geometry, on either side and as the phone sheet.
    reveal = script.split("function reveal(node) {", 1)[1].split("\n    function ", 1)[0]
    assert "free = freeArea()" in reveal and "free = freeArea()" in script.split("function center(", 1)[1]
    # Only a node that the details cover moves.
    assert "box.x + box.w > free.right" in reveal and "box.x < free.left" in reveal
    assert "box.y + box.h > (free.top || 0) + free.band" in reveal


def test_explorer_pages_relations_and_clamps_zoom() -> None:
    script = _viewer_source()

    assert "Math.min(visible + RELATION_PAGE, ids.length)" in script
    assert "ids.slice(visible, next)" in script
    assert "clamp(old * factor, MIN_SCALE, 4)" in script


def test_explorer_styles_keep_one_family_one_type_scale_and_one_micro_label() -> None:
    """The rail and drawer headings were set in Georgia, among twenty rem sizes and unloaded weights."""
    style = dag_viewer._STYLE
    assert "serif" not in style.replace("sans-serif", "")
    # Integrated reading and region cards add an 18px title; !important only protects it from Material.
    assert set(re.findall(r"font-size: (\d+)px(?: !important)?;", style)) <= {"11", "12", "13", "14", "16", "17", "18", "22"}
    assert set(re.findall(r"font-weight: ([^;]+);", style)) <= {"400", "600", "700"}
    # Rail group headings and filter legends are sentence case, like every other label; the drawer kicker capitalises only its first letter.
    assert "text-transform: uppercase" not in style.replace(".bp-dag-kicker::first-letter { text-transform: uppercase; }", "")
    group = ".bp-dag-filter-popover legend, .bp-dag-viewer.bp-dag-viewer .bp-dag-navigation-group h2 {"
    assert "font-size: 12px; font-weight: 600; letter-spacing: normal;" in style.split(group, 1)[1].split("}", 1)[0]


def test_reading_pane_sizes_put_the_statement_first() -> None:
    """The book's rem sizes set Sources, totals and Previous/Next larger than the 17px statement in the pane."""
    style = dag_viewer._STYLE
    body = re.search(r"^\.bp-reading-body \{([^}]*)\}", style, re.M)
    assert body is not None and "font-size: 16px; line-height: 1.6;" in body.group(1)

    def size(selector: str) -> str:
        rule = re.search(rf"^{re.escape(selector)} \{{ font-size: (\d+)px; \}}", style, re.M)
        assert rule is not None, selector
        return rule.group(1)

    notes = ".bp-reading-body :is(.bp-thmnotes, .bp-meta, .bp-progress-states), .bp-reading-body details.bp-dependencies"
    labels = (
        ".bp-reading-body :is(.bp-thmnotes h6, .bp-key, .bp-mark, .bp-progress-kicker, "
        ".bp-book-nav-direction, .bp-next-kicker, .bp-hero-kicker)"
    )
    totals = ".bp-reading-body :is(.bp-progress-total, .bp-book-nav-title, .bp-next-title)"
    assert (size(notes), size(labels), size(totals)) == ("14", "12", "16")
    # The workspace sets theorem statements upright; book pages keep amsthm italics.
    assert ".bp-dag-integrated .theorem-style-plain > .bp-thmcontent { font-style: normal; }" in style


def test_explorer_names_its_scope_once_and_lists_only_its_ancestors_above_it() -> None:
    """A kind pill, a rail kicker, the h1, the trail's last crumb and a home link all named the scope or the book."""
    script, style = _viewer_source(), dag_viewer._STYLE
    for retired in ("bp-dag-view-kind", "bp-dag-navigation-kicker", "bp-dag-navigation-home"):
        assert retired not in script and retired not in style
    assert "Current scope" not in script and "Library atlas" not in script
    # The rail's one-line trail drops the current crumb, which the h1 right under it names.
    rail = 'trailList(trail.slice(0, -1), "bp-dag-navigation-path")'
    assert rail in script
    assert script.index(rail) < script.index('element("h1", "bp-dag-navigation-title"')
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
    """Over the rail the hidden breadcrumb left a head row holding only the textbook link."""
    style = dag_viewer._STYLE
    one_row = style.split("@media (min-width: 1101px) {\n", 1)[1].split("\n}", 1)[0]
    assert ".bp-dag-context { display: contents; }" in one_row
    assert ".bp-dag-viewer:has(.bp-dag-navigation-path) .bp-dag-breadcrumb { display: none; }" in one_row
    # The textbook link ends the row in the DOM too, so Tab follows the row instead of starting at its end.
    assert "order:" not in one_row
    # The landing page's trail stays beside the search, capped so that it cannot push the controls over the link.
    assert ".bp-dag-breadcrumb { flex: 0 1 auto; max-width: 32%; }" in one_row
    script = _viewer_source()
    assert script.index("head.appendChild(toolbar);") < script.index("placeBook();")
    assert "context.appendChild(bookLink)" not in script
    # At 1,100px and below a grid draws the link at the end of the first row, under phones on a line of its own.
    # The runtime moves it before the toolbar there, so that Tab still follows what is drawn.
    assert 'narrow = matchMedia("(max-width: 1100px)")' in script
    assert "head.insertBefore(bookLink, narrow.matches ? toolbar : null);" in script
    assert 'narrow.addEventListener("change", placeBook);' in script
    narrow = style.split("@media (max-width: 1100px) {\n", 1)[1].split("\n}", 1)[0]
    assert ".bp-dag-book { grid-area: 1 / 2;" in narrow and ".bp-dag-toolbar { grid-column: 1 / -1; }" in narrow
    phones = style.split("@media (max-width: 680px) {\n", 1)[1].split("\n}", 1)[0]
    assert ".bp-dag-book { grid-area: 2 / 1;" in phones
    # Margins, not padding, space the link, so its focus ring sits on the text.
    assert ".bp-dag-book { grid-area: 1 / 2; align-self: center; margin-right: 16px; }" in narrow
    assert ".bp-dag-book { grid-area: 2 / 1; justify-self: start; margin: 4px 8px 0; }" in phones
    assert not [rule for rule in style.splitlines() if ".bp-dag-book {" in rule and "padding" in rule]
    # Up to 1,100px open details cover the rail, so the head keeps a row where the trail then appears.
    assert (
        "@media (min-width: 681px) and (max-width: 1100px) { "
        ".bp-dag-viewer:has(.bp-dag-navigation-path) .bp-dag-breadcrumb { visibility: hidden; } }"
    ) in style
    assert ".bp-dag-viewer.bp-dag-has-selection.bp-dag-sheet-open .bp-dag-breadcrumb { visibility: visible; }" in style
    # A head without a textbook link has nothing for that row while the rail keeps the trail, so it collapses.
    assert (
        ".bp-dag-viewer:has(.bp-dag-navigation-path):not(.bp-dag-has-selection.bp-dag-sheet-open) "
        ".bp-dag-head:not(:has(.bp-dag-book)) .bp-dag-context { display: none; }"
    ) in style.split("@media (min-width: 681px) and (max-width: 1100px) {\n", 1)[1].split("\n}", 1)[0]
    # That row grows the head while details open, so the map shifts back by it instead of jumping under the pointer.
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
    # The density note stays, only while a shown card's real box leaves the stage or labels are hidden by zoom, as plain text.
    note = script.split("var cropped = state.active.some(function (node) {", 1)[1].split("\n    function ", 1)[0]
    assert "var box = screen(node); return box.x < 0 || box.y < 0 || box.x + box.w > refs.stage.clientWidth" in note
    # A card under a phone's open reader is off screen too; the fit uses the same bound.
    assert "var bottom = mapHeight();\n      var cropped" in script and "box.y + box.h > bottom;" in note
    assert "var shown = mapHeight();" in script
    assert 'cropped ? "Showing part of the map. Drag to see more, or open "' in note and '" for all "' in note
    assert '"Zoom in to reveal "' in note and "readableCrop" not in script and "Pan or " not in script
    density = next(rule for rule in style.splitlines() if rule.startswith(".bp-dag-density {"))
    assert "left: 16px; bottom: 12px;" in density and "pointer-events: none;" in density
    # On phones it clears the collapsed sheet's 52px peek and 8px inset by 8px.
    phones = style.split("@media (max-width: 680px) {\n", 1)[1].split("\n}", 1)[0]
    assert ".bp-dag-density { bottom: 68px; }" in phones
    assert not any(word in density for word in ("border", "shadow", "background", "blur"))
    # In the workspace it sits a line above the map key, and under the map header while the phone reader covers the bottom.
    assert ".bp-dag-integrated .bp-dag-density { bottom: 36px; }" in style
    assert ".bp-dag-integrated.bp-dag-sheet-open:not(.bp-dag-compact-regions) .bp-dag-density { top: 44px; bottom: auto; }" in style


def test_explorer_head_switches_materials_palette_from_the_trail_row() -> None:
    """The explorer hides Material's header, and with it the theme's sun or moon."""
    script, style = _viewer_source(), dag_viewer._STYLE
    # Only a site whose mkdocs.yml lists palette entries renders the form, so an older dark-only site gets no button.
    assert 'var schemes = document.querySelector("[data-md-component=palette]");' in script
    assert '"Switch light or dark theme"' in script and "flip.title = themeLabel; context.appendChild(flip);" in script
    # It clicks Material's own toggle, the one unhidden label, which steps to the next entry of a two- or three-entry palette.
    assert 'schemes.querySelector("label:not([hidden])").click();' in script
    assert ":checked)" not in script
    rule = next(line for line in style.splitlines() if line.startswith(".bp-dag-viewer .bp-dag-theme {"))
    assert "width: 32px; min-height: 32px;" in rule and "border: 0;" in rule


def test_explorer_toolbar_counts_only_hidden_items_and_files_the_edge_toggle_under_status() -> None:
    """The toolbar always counted what it showed, and the toggle for items without edges sat in it."""
    script, style = _viewer_source(), dag_viewer._STYLE
    assert "bp-dag-stats" not in script and "bp-dag-stats" not in style and '" shown"' not in script
    assert 'refs.hiddenButton.textContent = "Show " + hidden + " filtered out"; refs.hiddenButton.hidden = !hidden;' in script
    assert '"Show unlinked"' in script and "without edges" not in script
    assert "refs.filterPopover.append(fieldset, edges);" in script
    # Map | Contents and the zoom buttons are borderless ghost buttons inside one 1px group each.
    assert (
        ".bp-dag-zoom, .bp-dag-modes { display: inline-flex; flex: none; gap: 2px; padding: 2px; "
        "border: 1px solid var(--dag-border);"
    ) in style
    assert ".bp-dag-zoom .bp-dag-button, .bp-dag-modes .bp-dag-mode-button { min-height: 30px; padding: 4px 10px; border: 0;" in style
    # Zoom leaves with the map, and a finger, which pinches, keeps only a 44px Fit.
    assert ".bp-dag-viewer:has(.bp-dag-stage[hidden]) .bp-dag-zoom { display: none; }" in style
    coarse = style.split("@media (pointer: coarse) {\n", 1)[1].split("\n}", 1)[0]
    assert ".bp-dag-zoom .bp-dag-button:not(:last-child) { display: none; }" in coarse
    assert ".bp-dag-zoom .bp-dag-button { min-width: 44px; min-height: 44px; }" in coarse


def test_explorer_toolbar_copy_says_what_it_searches_and_filters() -> None:
    """The toolbar read "Search 7 roadmap entries", "Status · 2 · edges only" and "Regions".

    The 7 mixed topics, articles and the root, and search matches titles, ids,
    Lean names and summaries, so the placeholder says what it matches and no
    count. With the count gone, aria-busy is what says the index is loading.
    """
    script = _viewer_source()
    assert "roadmap entries" not in script and "Search the roadmap" not in script
    assert script.count("refs.search.placeholder =") == 1
    assert (
        'refs.filterSummary.textContent = "Filter: " + (enabled === presentStatuses.length ? "all" : '
        'enabled + " of " + presentStatuses.length) +\n        " statuses" + (state.showIsolated ? "" : " · linked only");'
    ) in script
    assert 'button("bp-dag-filter-summary", "Filter: all statuses")' in script
    assert 'element("legend", "", "Links")' in script and '"Edges"' not in script
    assert 'button("bp-dag-mode-button", "Areas")' in script and '"Regions"' not in script
    assert '"Nothing matches these filters."' in script and "No nodes match" not in script
    assert 'mapAllowed ? "Map" : "Map (too large)"' in script
    assert script.count('refs.search.setAttribute("aria-busy", "true")') == 2
    assert script.count('refs.search.removeAttribute("aria-busy")') == 3
    assert 'if (navigation && host.getAttribute("data-catalog-src")) refs.search.setAttribute("aria-busy", "true");' in script


def test_workspace_header_presses_one_view_and_gives_the_trail_its_own_row() -> None:
    """Map and Areas both read as pressed at the root, a deep trail sat in a third of an empty row, and tablets had three rows."""
    script, style = _viewer_source(), dag_viewer._STYLE
    # Areas stays: inside an area it is the way back to the overview, whose root crumb is then not a link.
    assert 'var areas = state.mode === "graph" && !state.area && data.nodes.some(function (node) { return node._region; });' in script
    assert 'pressed(refs.graphMode, state.mode === "graph" && !areas);' in script
    assert 'if (refs.regionsMode) pressed(refs.regionsMode, areas);' in script
    assert 'refs.graphMode.setAttribute("aria-pressed", String(state.mode === "graph"));' not in script
    assert ".bp-dag-integrated .bp-dag-breadcrumb { display: block; flex: 1 1 auto; max-width: none;" in style
    assert ".bp-dag-viewer.bp-dag-integrated .bp-dag-breadcrumb li { min-width: 4em; }" in style
    assert ".bp-dag-viewer.bp-dag-integrated .bp-dag-breadcrumb li:has(.bp-dag-sr-only) { flex: none; min-width: 0; }" in style
    tablet = style.split("@media (max-width: 1100px) {\n  .bp-dag-integrated", 1)[1].split("\n}", 1)[0]
    assert ".bp-dag-integrated .bp-dag-search-wrap { flex-basis: 240px; }" in tablet and "100%" not in tablet
    # Topics was a few px above Status in the same row.
    assert ".bp-dag-integrated .bp-dag-outline-toggle { display: inline-flex; align-items: center; }" in tablet
    phones = style.split("@media (max-width: 720px) {\n  .bp-dag-integrated", 1)[1].split("\n}", 1)[0]
    assert ".bp-dag-integrated .bp-dag-breadcrumb li:not(:first-child, :last-child) { display: none; }" in phones
    assert ".bp-dag-integrated .bp-dag-search-wrap { flex-basis: 100%; }" in phones


def test_explorer_search_draws_its_magnifier_and_shortcut_hint_in_css() -> None:
    """U+2315 is not in Plus Jakarta Sans and sat off the box's line, and "( / )" read as debug output."""
    script, style = _viewer_source(), dag_viewer._STYLE
    assert "⌕" not in style and "( / )" not in script
    assert 'refs.search.placeholder = "Search titles and Lean names";' in script
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
        "0 0 0 2px var(--dag-map), 0 0 0 4px var(--dag-fg), var(--dag-e1)",
        "0 0 0 2px color-mix(in srgb, var(--dag-link) 40%, transparent), var(--dag-e1)",
        "0 0 0 3px color-mix(in srgb, var(--dag-link) 18%, transparent)",
        "inset 3px 0 0 var(--dag-link)",
        "inset 3px 0 0 var(--dag-fg)",
    }
    selected = ".bp-dag-node[data-selected=true] { box-shadow: 0 0 0 2px var(--dag-map), 0 0 0 4px var(--dag-fg), var(--dag-e1); }"
    assert selected in style
    assert "border-width" not in style


def test_selection_focus_and_the_current_statement_do_not_borrow_status_blue() -> None:
    """Selection shared ready-to-prove's blue, focus rings were about 2:1 and the rail showed focus as hover."""
    style = dag_viewer._STYLE
    # The selected card's ring is the text colour past a gap; blue stays a status colour.
    assert "data-selected=true] { box-shadow: 0 0 0 2px var(--dag-link)" not in style
    assert ".bp-dag-node[data-subdued=true] { opacity: .62; filter: saturate(.25); }" in style
    # One solid ring for every focus-visible state, drawn inside rows and outside a card's selection ring.
    assert "color-mix(in srgb, var(--dag-link) 45%" not in style and "52%" not in style
    assert [rule.split(" {")[0] for rule in style.splitlines() if rule.startswith(".bp-dag-viewer") and ":focus-visible {" in rule] == [
        ".bp-dag-viewer :is(button, a, [tabindex]):focus-visible",
        ".bp-dag-viewer :is(.bp-dag-result, .bp-dag-inventory-row, .bp-dag-navigation-link, .bp-reading-body):focus-visible",
        ".bp-dag-viewer .bp-dag-node:focus-visible",
    ]
    assert ":focus-visible { outline: 2px solid var(--dag-link); outline-offset: 2px; }" in style
    assert ".bp-dag-node:focus-visible { outline-offset: 4px; }" in style
    assert [rule for rule in style.splitlines() if "outline:" in rule and "focus-visible" in rule and "solid var(--dag-link)" not in rule] == []
    # Hovering a rail link lights its row; focus is the ring alone.
    assert ".bp-dag-navigation-link:hover { border-color: var(--dag-border); background: var(--dag-panel); }" in style
    assert ".bp-dag-navigation-link:focus-visible" not in style.replace(".bp-dag-navigation-link, .bp-reading-body):focus-visible", "")
    current = re.search(r"^\.bp-reading-current \{([^}]*)\}", style, re.M)
    assert current is not None and "outline" not in current.group(1)
    # Its bar is ink, not a second can_prove blue beside the statement's own status rule.
    assert "box-shadow: inset 3px 0 0 var(--dag-fg);" in current.group(1) and "padding: 10px 12px 10px 15px;" in current.group(1)

    def mixed(colour: str, base: str, share: float) -> str:
        pairs = [(int(colour[i : i + 2], 16), int(base[i : i + 2], 16)) for i in (1, 3, 5)]
        return "#" + "".join(f"{round(c * share + b * (1 - share)):02X}" for c, b in pairs)

    # --bp-link on the explorer's surfaces: page, map, panel, and the 10% and 9% current-row tints.
    for link, page, field in (("#0064E0", "#FFFFFF", "#F0F2F5"), ("#2D88FF", "#242526", "#18191A")):
        surfaces = (page, field, mixed(page, "#64748B", 0.94), mixed(link, page, 0.10), mixed(link, page, 0.09))
        assert min(_contrast(link, surface) for surface in surfaces) >= 3.9, link
        # The old ring, link at 45% over the page, measured about 2:1 against it.
        assert _contrast(mixed(link, page, 0.45), page) < 3


def test_explorer_canvas_paints_its_labels_in_the_site_family_at_11px_or_more() -> None:
    """Painted labels inherit the site family and are readable when the dependency map opens."""
    script = _viewer_source()
    assert script.count('refs.ctx.font = "10px sans-serif";') == 1  # Parse probe only, never a painted label.
    assert 'family = getComputedStyle(host).fontFamily; refs.ctx.font = "11px " + family;' in script
    assert 'ctx.fillStyle = dark ? "#B0B3B8" : "#65676B"; ctx.font = "600 13px " + family;' in script
    # The free-standing map paints its count pills at a fixed 13px.
    assert 'pill(ctx, count, (ax + bx) / 2, (ay + by) / 2 + offset * 3, 1);' in script
    # The integrated router measures and paints the same 13px family; its .9 initial floor gives 11.7px.
    assert 'refs.ctx.font = "600 13px " + family;' in script
    assert 'ctx.font = "600 " + 13 * scale + "px " + family;' in script
    assert 'readableFloor = navigation ? .9 :' in script


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


def test_card_titles_take_the_page_ink_on_every_status_fill() -> None:
    """Planned titles were grey and finished ones white on solid green, so the status recoloured the title."""
    style = dag_viewer._STYLE
    title = re.search(r"\.bp-dag-node-title \{([^}]*)\}", style)
    assert title is not None and "color: var(--dag-fg);" in title.group(1)
    assert "--dag-fg: var(--bp-fg, #1c1e21);" in style
    # The book's light and slate ink, then the standalone viewer's fallback.
    light, dark = re.findall(r"--bp-fg: (#[0-9A-Fa-f]{6});", render._stylesheet())
    for state in (*status.STATES, *status.INVENTORY_STATES):
        assert min(_contrast(light, state.fill), _contrast("#1C1E21", state.fill)) >= 4.5, state.key
        assert _contrast(dark, state.dark_fill) >= 4.5, state.key
    # Dark ink on mathlib's old solid #1C5C33 was 2.1:1; finished fills are tints that still deepen with completion.
    greens = [next(state for state in status.STATES if state.key == key) for key in ("mathlib", "fully_proved", "proved")]
    assert all(_contrast(state.fill, "#FFFFFF") < 1.5 for state in greens)
    assert _contrast(greens[0].fill, "#FFFFFF") > _contrast(greens[1].fill, "#FFFFFF") > _contrast(greens[2].fill, "#FFFFFF")


def test_status_strokes_outline_cards_on_both_maps() -> None:
    """A tinted card is barely darker than the map, so its stroke alone has to draw the card's edge."""
    assert "--dag-map: var(--bp-sunken, #f0f2f5);" in dag_viewer._STYLE
    light_maps, dark_maps = ("#F0F2F5", "#FFFFFF"), ("#18191A", "#242526")
    # Planned and the inventory greys are quiet on purpose; the ring tests keep those three apart.
    for state in status.STATES:
        if state.key != "planned":
            assert min(_contrast(state.stroke, surface) for surface in (state.fill, *light_maps)) >= 3, state.key
            assert min(_contrast(state.dark_stroke, surface) for surface in (state.dark_fill, *dark_maps)) >= 3, state.key


def test_card_kind_words_match_the_book_captions() -> None:
    """A card read "Base / ready to state" while the reading pane beside it said "Definition 1 (Base)"."""
    script = _viewer_source()
    kinds = re.search(r"var KINDS = new Map\(Object\.entries\(\{(.*?)\}\)\);", script, re.S)
    assert kinds is not None
    assert dict(re.findall(r'(\w+): "([^"]+)"', kinds.group(1))) == render.DECLARATION_LABELS
    assert 'var kind = KINDS.get((node.declaration || "").toLowerCase())' in script
    # An ATLAS title such as "Theorem (Crystallographic Restriction)" names its kind already.
    assert 'if (kind && (node.title || "").indexOf(kind) === 0) kind = "";' in script


def test_card_meta_keeps_its_status_whole() -> None:
    """A stated theorem's card read "Theorem · statement formali…": the ellipsis took the status, not the kind."""
    style = dag_viewer._STYLE
    assert re.search(r"^\.bp-dag-node-meta \{ display: flex;", style, re.M)
    assert ".bp-dag-node-meta > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: pre; }" in style
    assert ".bp-dag-node-meta > span + span { flex: none; }" in style


def test_status_ring_greys_stay_apart_in_both_schemes() -> None:
    """Planned's dark stroke rose to #737578, 1.39:1 from inventory pending's #8A8D91, so their arcs ran together."""
    planned = next(state for state in status.STATES if state.key == "planned")
    greys = (planned, *status.INVENTORY_STATES)
    # A status bar runs planned, checked and pending inventory, so with one of them absent the other two meet.
    # 1.5:1 keeps neighbouring segments a visible step apart; the old dark pair was 1.39:1.
    for first, second in ((0, 1), (1, 2), (0, 2)):
        for stroke in ("stroke", "dark_stroke"):
            ratio = _contrast(getattr(greys[first], stroke), getattr(greys[second], stroke))
            assert ratio >= 1.5, (greys[first].key, greys[second].key, stroke, ratio)
    for state in greys:
        assert min(_contrast(state.dark_stroke, state.dark_fill), _contrast(state.dark_stroke, "#18191A")) >= 3, state.key


def test_topic_and_region_cards_frame_neutrally_and_show_progress_as_a_bar() -> None:
    """A 3px conic ring in status colours framed every topic and region card, so a mostly stated region read as done.

    Statement formalized shares proved's green, so the bar alone cannot say how much is done: the
    card's meta line names the fully proved share, as the pill does.

    Regions behind the cards cycled seven pastels that meant nothing, and a topic's pill named its
    least-complete item ("ready to prove") above a breakdown that was mostly fully proved.
    """
    script, style = _viewer_source(), dag_viewer._STYLE
    card = re.search(r"^\.bp-dag-viewer\.bp-dag-atlas \.bp-dag-node\[data-kind=scope\] \{([^}]*)\}", style, re.M)
    assert card is not None
    for declaration in ("padding: 0;", "border: 1px solid var(--dag-border);", "background: var(--dag-node-fill);"):
        assert declaration in card.group(1)
    assert "dag-status-ring" not in card.group(1)
    bar = re.search(r"^\.bp-dag-node-core::after \{([^}]*)\}", style, re.M)
    assert bar is not None and "background: var(--dag-status-ring, var(--dag-border));" in bar.group(1)
    # The bar sits in the bottom padding of every card size, so it adds no height and meets no text.
    bottom = int(re.search(r"bottom: (\d+)px;", bar.group(1)).group(1))
    height = int(re.search(r"height: (\d+)px;", bar.group(1)).group(1))
    paddings = re.findall(r"\.bp-dag-node-core \{[^}]*?padding: (\d+)px", style)
    assert paddings and bottom + height <= min(map(int, paddings)), paddings
    assert "conic-gradient" not in script
    assert '"linear-gradient(90deg," + stops.join(",") + ")"' in script
    assert 'stops.push((color.stroke || "#9ca3af") + " " + cursor + "% " + end + "%");' in script
    # Regions merge their topics' counts in the order they meet them; the bar keeps the legend's.
    assert ".sort(function (a, b) { return legendRank(a[0]) - legendRank(b[0]); });" in script
    # The palette's keys arrive sorted, so the client spells out the legend's order; it must match status.STATES.
    legend = re.search(r'var LEGEND = "([a-z_ ]+)"\.split\(" "\);', script)
    assert legend is not None and legend.group(1).split() == [state.key for state in status.STATES]

    # Every region is the book's panel with a rule and a muted label; no hue competes with status.
    assert "regionColors" not in script
    assert 'ctx.fillStyle = dark ? "#282A2C" : "#F6F7F8";' in script
    assert 'ctx.strokeStyle = dark ? "#3E4042" : "#CED0D4"; ctx.lineWidth = 1;' in script

    def mixed(colour: str, base: str, share: float) -> str:
        pairs = [(int(colour[i : i + 2], 16), int(base[i : i + 2], 16)) for i in (1, 3, 5)]
        return "#" + "".join(f"{round(c * share + b * (1 - share)):02X}" for c, b in pairs)

    # The panel is --dag-panel over each scheme's --bp-surface; the rule and label are --bp-rule and --bp-muted.
    for surface, panel, rule, label, field in (
        ("#FFFFFF", "#F6F7F8", "#CED0D4", "#65676B", "#F0F2F5"),
        ("#242526", "#282A2C", "#3E4042", "#B0B3B8", "#18191A"),
    ):
        assert mixed(surface, "#64748B", 0.94) == panel
        assert f"--bp-rule: {rule};" in render._stylesheet() and f"--bp-muted: {label};" in render._stylesheet()
        assert _contrast(label, panel) >= 4.5 and panel != field

    # A topic's pill counts its finished items; a scope of module inventories still says so.
    # A topic with no targets said "planned", its rolled-up status, though nothing in it is planned.
    assert ('progress ? num(done) + " of " + num(total) + " fully proved" : '
            'node.kind === "scope" && !node._unknownCount ? "no targets" : statusLabel(node.status)') in script
    assert "/^(fully_proved|mathlib)$/.test(entry[0])" in script
    assert 'inventoryOnly ? "inventory only" :' in script
    # Card and pill share one count, which leaves neutral module inventories out of the total like the hero.
    assert "if ((palette[entry[0]] || {}).neutral) return; total += entry[1];" in script
    assert 'num(count[0]) + " of " + num(count[1]) + " fully proved"' in script
    # The pill's dot is finished green only once every target is; as a swatch it carries both schemes' colours.
    assert 'mark.prepend(swatch(progress && done === total ? "fully_proved" : node.status));' in script and "--dag-pill-" not in script
    # The breakdown and the overview use the card's nouns.
    assert '" items in this scope"' not in script and '"Mathematical regions"' not in script
    # The map names the selected result, and the reader shows its relations once, in the statement's own disclosure.
    assert 'links ? " · uses " + links.before.length + " · used by " + links.after.length : ""' in script
    # The title is its own span, so a long one gives way before the counts.
    assert 'refs.mapContext.replaceChildren(element("span", "", links ? record.title :' in script
    assert ".bp-map-context > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; }" in dag_viewer._STYLE
    assert '" → Result → "' not in script
    assert 'var relations = target.querySelector(".bp-dependencies"); if (relations) relations.open = true;' in script
    for gone in ("bp-reading-neighbors", "bp-reading-links", "bp-reading-related"):
        assert gone not in script and gone not in dag_viewer._STYLE, gone


def test_small_status_marks_fill_finished_states_with_the_stroke() -> None:
    """The light tints made tiny map plates, rail swatches and dependency dots of finished work vanish."""
    script = _viewer_source()
    assert 'function solid(key, color) { return ((palette[key] || palette.planned || {}).light || {}).fill === "#FFFFFF" ? color.fill : color.stroke; }' in script
    # At .82 over the map neighbouring finished plates fell under 1.5:1 and defined's under 3:1, so a highlighted one is opaque.
    assert 'if (tiny) { if (highlighted) ctx.globalAlpha = 1; ctx.fillStyle = solid(node.status, color); ctx.fillRect(' in script
    assert '"-fill", solid(key, color) || "transparent");' in script
    # Against the map the zoomed-out plate of every finished state keeps 3:1, as it did before the tints.
    for state in status.STATES:
        if state.fill != "#FFFFFF":
            assert _contrast(state.stroke, "#F0F2F5") >= 3, state.key
            assert _contrast(state.dark_stroke, "#18191A") >= 3, state.key


def _book_marks(dark: bool) -> dict[str, str]:
    """The fill of each state's dependency dot and swatch, as the book's stylesheet emits it."""
    marks = re.findall(
        r"(?m)^(\[data-md-color-scheme=slate\] )?[^{\n]*\.bp-swatch-(\w+) \{ background: (#[0-9A-Fa-f]{6});",
        render._stylesheet(),
    )
    return {key: colour for slate, key, colour in marks if bool(slate) == dark}


def _mix(colour: str, base: str, share: float) -> str:
    pairs = ((int(colour[index:index + 2], 16), int(base[index:index + 2], 16)) for index in (1, 3, 5))
    return "#" + "".join(f"{round(a * share + b * (1 - share)):02X}" for a, b in pairs)


def test_each_finished_status_has_its_own_small_mark_in_both_schemes() -> None:
    """Fully proved, proved and defined shared stroke #22773A, so their dots and swatches were one colour.

    In the book's dependency rows and the explorer's rail the mark is the only status signal. Light marks
    darken towards mathlib and dark ones brighten towards it, so more finished always reads stronger.
    """
    finished = ("mathlib", "fully_proved", "proved", "defined")
    assert [state.key for state in status.STATES[:4]] == list(finished)
    style = dag_viewer._STYLE
    assert "--dag-panel: color-mix(in srgb, var(--dag-bg) 94%, #64748b 6%);" in style
    assert "background: color-mix(in srgb, var(--dag-link) 9%, var(--dag-bg));" in style
    # The map and the page, then a hovered and a selected rail row, which sit on the panel and on a link-blue tint.
    surfaces = {
        False: ("#F0F2F5", "#FFFFFF", _mix("#64748B", "#FFFFFF", .06), _mix("#0064E0", "#FFFFFF", .09)),
        True: ("#18191A", "#242526", _mix("#64748B", "#242526", .06), _mix("#2D88FF", "#242526", .09)),
    }
    for dark in (False, True):
        book = _book_marks(dark)
        marks = [book[key] for key in finished]
        for (first, second), (one, other) in zip(zip(finished, finished[1:]), zip(marks, marks[1:])):
            ratio = _contrast(one, other)
            assert ratio >= 1.5, (first, second, dark, ratio)
            assert (_contrast(one, "#000000") > _contrast(other, "#000000")) is dark, (first, second, dark)
        for key, mark in zip(finished, marks):
            assert min(_contrast(mark, surface) for surface in surfaces[dark]) >= 3, (key, dark, mark)


def test_filled_marks_that_are_not_green_part_from_every_green_by_hue() -> None:
    """The four greens span the lightness that keeps 3:1 on the map, so filled amber and grey marks match one.

    Light defined against not ready is 1.06:1 and against inventory pending 1.02:1; dark fully proved against
    not ready is 1.10:1. That is accepted (status.py says so), but hue must keep them apart.
    """

    def lab(colour: str) -> tuple[float, float, float]:
        channels = [int(colour[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        xyz = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b,
               (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
        x, y, z = (t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in xyz)
        return 116 * y - 16, 500 * (x - y), 200 * (y - z)

    finished = ("mathlib", "fully_proved", "proved", "defined")
    filled = [state.key for state in (*status.STATES, *status.INVENTORY_STATES)
              if state.fill != "#FFFFFF" and state.key not in finished]
    assert {"not_ready", "inventory_pending"} <= set(filled)
    for dark in (False, True):
        book = _book_marks(dark)
        for key in filled:
            for green in finished:
                distance = sum((a - b) ** 2 for a, b in zip(lab(book[key]), lab(book[green]))) ** .5
                assert distance >= 40, (key, green, dark, round(distance))


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
