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
    assert all(node["layout"] == {"height": 56, "width": 196} for node in payload["nodes"])
    assert [node["id"] for node in payload["nodes"]] == [node.id for node in nodes]
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
    assert payload["edges"] == [
        {"source": source.id, "target": target.id, "statement": 1, "proof": 0}
        for source, target in zip(nodes, nodes[1:])
    ]
    assert not any(node["isolated"] for node in payload["nodes"])

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
    # An atlas that large keeps its overview's count of topics in each region.
    topics = tuple(
        ViewNode(node.id, node.title, "scope", node.members, node.status_counts, area="Algebra") for node in nodes
    )
    dag_viewer.write_payload(output, GraphView(kind="project", title="Large", nodes=topics, edges=()), links={})
    atlas = json.loads(output.read_text(encoding="utf-8"))
    assert not any("layout" in node for node in atlas["nodes"])
    assert atlas["regions"] == [{"count": dag_viewer.MAX_MAP_NODES + 1, "label": "Algebra"}]


@pytest.mark.parametrize("wrapper_count", [2, dag_viewer.MAX_MAP_NODES + 1])
def test_overview_dimensions_follow_drawn_nodes_without_changing_source_topology(
    tmp_path: Path, wrapper_count: int,
) -> None:
    wrappers = tuple(
        ViewNode(
            f"scope:wrapper-{index}", f"Wrapper {index}", "scope", ("a", "b"), (("planned", 2),),
            navigation="collection" if index % 2 else "transparent",
        )
        for index in range(wrapper_count)
    )
    results = tuple(ViewNode(name, name, "node", (name,), (("planned", 1),)) for name in ("a", "b"))
    edges = (ViewEdge(wrappers[0].id, "a", statement_count=1), ViewEdge("a", "b", 1, 2))
    view = GraphView("project", "Overview", (*wrappers, *results), edges, overview=True)
    output = tmp_path / "overview.json"
    dag_viewer.write_payload(output, view, links={})
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert [node["id"] for node in payload["nodes"]] == [node.id for node in view.nodes]
    assert all(node["map"] is False and "layout" not in node and node["isolated"] for node in payload["nodes"][:-2])
    assert all(
        node["map"] is True and node["layout"] == {"height": 56, "width": 196} and not node["isolated"]
        for node in payload["nodes"][-2:]
    )
    assert payload["edges"] == [
        {"source": wrappers[0].id, "target": "a", "statement": 1, "proof": 0},
        {"source": "a", "target": "b", "statement": 1, "proof": 2},
    ]
    assert payload["edge_count"] == 3
    assert [node["status_counts"] for node in payload["nodes"]] == [
        *([["planned", 2]] for _ in wrappers), [["planned", 1]], [["planned", 1]],
    ]
    assert payload["view"]["presentation"] == "dag" and payload["regions"] == []


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


def test_isolated_inventory_keeps_connected_data_and_dimensions_unchanged(tmp_path: Path) -> None:
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
    assert [node["id"] for node in payload["nodes"]] == node_ids
    assert all(node["layout"] == {"height": 56, "width": 196} for node in payload["nodes"])
    assert [node["id"] for node in payload["nodes"] if node["isolated"]] == node_ids[2:]
    assert all(node["status_counts"] == [["planned", 1]] for node in payload["nodes"])
    assert payload["edges"] == [{"source": "root", "target": "result", "statement": 1, "proof": 0}]
    connected = tmp_path / "connected.json"
    dag_viewer.write_payload(
        connected, GraphView("full", "Mixed graph", nodes[:2], view.edges), links={},
    )
    original = json.loads(connected.read_text(encoding="utf-8"))
    assert payload["nodes"][:2] == original["nodes"]
    assert payload["edges"] == original["edges"] and payload["edge_count"] == original["edge_count"] == 1


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
    assert payload["regions"] == [
        {"label": "Applications", "count": 23},
        {"label": "Foundations", "count": 10},
    ]
    assert [node["id"] for node in payload["nodes"]] == [node.id for node in nodes]
    assert all(set(layout) == {"height", "width"} for layout in layouts)
    assert all(layout["width"] > layout["height"] for layout in layouts)
    assert {layout["width"] for layout in layouts} == {232}
    assert layouts[0]["height"] == 88 and layouts[-1]["height"] == 104
    assert [layout["height"] for layout in layouts] == sorted(layout["height"] for layout in layouts)
    assert len({layout["height"] for layout in layouts}) > 1


@pytest.mark.parametrize(
    ("items", "height"), [(0, 88), (1, 88), (2, 90), (3, 92), (15, 100), (31, 104), (255, 116), (4096, 116)],
)
def test_atlas_dimensions_retain_item_count_variation_and_height_bounds(
    tmp_path: Path, items: int, height: int,
) -> None:
    node = ViewNode("scope:topic", "Topic", "scope", (), (("planned", items),), area="Authored area")
    output = tmp_path / "dimensions.json"
    dag_viewer.write_payload(output, GraphView("project", "Topics", (node,), ()), links={})
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["nodes"][0]["layout"] == {"height": height, "width": 232}
    assert payload["nodes"][0]["member_count"] == items
    assert payload["nodes"][0]["status_counts"] == [["planned", items]]
    assert payload["view"]["presentation"] == "atlas"
    assert payload["regions"] == [{"count": 1, "label": "Authored area"}]


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
    assert regions == [{"count": 1, "label": area} for area in sorted(areas)]


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
    assert all(node["layout"] == {"height": 56, "width": 196} for node in payload["nodes"])


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
    assert "The dependency explorer needs JavaScript and its graph data." in container
    assert "Unsafe &lt;node&gt;" in container
    assert 'href="roadmap/a&quot; onclick=&quot;oops"' in container
    assert "javascript:alert" not in container
    assert "Showing 1 of 5 linked nodes" in container
    assert "<style data-autoform-dag-style>" in container
    assert "@media (max-width: 680px)" in container
    assert "@media (prefers-reduced-motion: reduce)" in container
    assert "100svh" in container
    assert "100dvh" in container
    assert 'refs.stage.setAttribute("role", "region")' in script
    assert 'toolbar.setAttribute("role", "group")' in script
    assert 'refs.navigation = element("aside", "bp-dag-navigation")' in script
    assert 'refs.navigation.setAttribute("aria-label", "Topics")' in script
    assert 'renderNavigation()' in script
    # Only the publisher's controlled formula service refreshes mathematics;
    # navigation clones the complete reading source rather than graph summaries.
    assert "typesetPromise" not in script
    assert "autoform:typeset" not in script and "FORMULA_CLASS_JSON" not in script
    assert "window.AutoformMath.refresh(refs.readingBody)" in script
    assert "function readingLabel(" in script
    assert "refreshReadingLabels(refs.navigationInner)" in script
    assert "refreshReadingLabels(refs.readingNeighbors)" in script
    assert "refreshReadingLabels(refs.breadcrumb)" in script
    assert 'readingLabel("li", "", entry.label, entry.reading_title)' in script
    assert 'readingLabel("a", !className && index === 0 ? "bp-dag-book" : "", entry.label, entry.reading_title)' in script
    assert 'part[0] === "math" ? "arithmatex" : ""' in script
    assert "refs.readingBody.replaceChildren(bundle.readingSource)" in script
    assert "navigation.bindReader(refs.readingBody)" in script
    # All enhancement enters through the canonical complete-reader router.
    assert '!window.AutoformNavigation || !host.closest(".bp-explorer-page")' in script
    assert '!navigation || !firstBundle || !firstBundle.readingSource' in script
    assert "window.AutoformNavigation.create" in script
    assert "function appendRelations(" not in script and "function drawRegions(" not in script
    assert 'setMode("list", true)' in script
    assert 'refs.announcement.setAttribute("aria-live", "polite")' in script
    assert "new ResizeObserver(queueResize)" in script
    assert 'url.origin === window.location.origin' in script
    assert "if (query === state.searchCacheQuery) return state.searchCache" in script
    assert f'data.schema !== "{dag_viewer.SCHEMA}"' in script
    assert f'INDEX_SCHEMA = "{dag_viewer.SEARCH_SCHEMA}"' in script
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
    # The handle names the page, typesetting its TeX, with a status mark only for a statement.
    assert 'refs.sheetHandle.replaceChildren(close ? "×" : record && record.result && record.status ? swatch(record.status) : "", handleTitle);' in script
    assert 'readingLabel("span", "bp-dag-sheet-title", title,' in script
    # Its accessible name starts with the title it shows; an open desktop reader over the overview closes with ×.
    assert 'refs.sheetHandle.setAttribute("aria-label", close ? "Close reading" : title + ", reading");' in script
    assert "Toggle mathematical reading" not in script
    assert "close = opened && !sheetLayout() && state.overviewOnly" in script
    assert "var page = currentBundle || data" in script
    assert "record = page.readingSource && records.get(page.readingSource.dataset.autoformNodeId);" in script


def test_explorer_counts_agree_with_their_nouns() -> None:
    """Integrated result/source counts use the plural formatter."""
    script = _viewer_source()

    assert 'return count.toLocaleString() + " " + (count === 1 ? one : many);' in script
    assert 'ui$mode === "sources" ? "source"' in dag_viewer._SCRIPT
    assert '.toLocaleString() + " dependencies"' not in script


def test_rail_rows_show_a_status_swatch_and_a_lone_group_no_heading() -> None:
    """The rail is the semantic scope outline; leaf status belongs to result cards and search."""
    script = _viewer_source()

    assert 'element("h2", "bp-outline-label", "Topics")' in script
    from tests.test_qa_outline_navigation import _PROJECTION_PEERS, _run

    _run(r"""
      const root=scope('roadmap',null),branch=scope('branch','roadmap'),child=scope('child','branch'),
        zero=page('chapter-zero','roadmap'),source=scope('source','roadmap',{navigation:'source'});
      const nodes=[root,child,zero,source,branch],before=JSON.stringify(nodes);
      const tree=outlineProjection(nodes,'branch','branch',120);
      assert.deepEqual(tree.entries.map(entry=>entry.node.id),['chapter-zero','branch','child']);
      assert.equal(tree.entries.find(entry=>entry.node===child).depth,1);
      assert.strictEqual(tree.root,root);assert.equal(JSON.stringify(nodes),before);
    """, ["outlineProjection"], _PROJECTION_PEERS)
    assert 'tooltip(link, node.title); link.dataset.navigationNodeId = node.id;' in script
    assert "function renderNavigation() {" in script and "renderOutline()" in script
    assert "bp-dag-navigation-group" not in script
    assert '(node.member_count || 0).toLocaleString() : statusLabel(node.status)' not in script


def test_explorer_shows_statuses_and_areas_but_never_internal_ids() -> None:
    """Cards, the details, Contents and search all printed ids such as "algebra/base" beside the titles."""
    script = _viewer_source()
    style = dag_viewer.render_container("graph.json")

    # A row keeps its status and says where it lives, below the list's own page or the root, or else its area.
    assert 'element("span", "bp-dag-inventory-meta", statusLabel(node.status) + (where ? " · " + where : ""))' in script
    assert 'var where = navigation && placeOf(node, currentBundle && currentBundle.scope) || node.area;' in script
    assert 'element("span", "bp-dag-result-meta", statusLabel(match.node.status))' in script
    assert 'element("span", "bp-dag-result-place", topic ? "Topic" + (place ? " in " + place : "") : place)' in script
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
    # The title says what a tap does next: on a phone at half that is the full reader, not a close.
    assert ('refs.sheetHandle.title = !opened ? "Open reading" : navigation && sheetLayout() && !host.classList.contains("bp-dag-sheet-full") ?'
            ' "Expand reading" : "Close reading";') in script
    assert 'refs.sheetHandle.setAttribute("aria-expanded", String(Boolean(open)))' in script
    assert 'element("span", "bp-dag-status-pill",' not in script
    assert 'element("article", "bp-reading-body md-typeset")' in script
    assert 'refs.readingBody.replaceChildren(bundle.readingSource)' in script


def test_details_drop_instructions_and_label_paths_compactly() -> None:
    """The drawer explained its own controls, invented a summary for topics without one and titled its pivots."""
    from tests.test_wiki_workspace_controls import _run

    script = _viewer_source()

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
    # A result's relations show once, in the statement's own disclosure: no strip under the reader repeats them,
    # which keeps only the topic's other pages and its concept links.
    assert "bp-reading-links" not in script and '[["Prerequisites", links.before], ["Used by", links.after]]' not in script
    _run(r"""
      const base=record('B','chapter',{title:'Actual prerequisite'});
      configure([focus,base],[['B','F',2,3]]);renderReaderContext();
      assert.equal(marked(dom$readingNeighbors,'bp-reading-links'),undefined,
        'The statement lists its own relations, so the reader must not repeat them');
      assert.deepEqual(dom$readingNeighbors.children.map(child=>child.className),['bp-reading-related-topics']);
      assert(links(dom$readingNeighbors).every(link=>link.getAttribute('aria-label')===null),'Topic links cannot claim dependency weights');
    """)
    assert "function appendRelations(" not in script and "bp-dag-pivots" not in script
    assert '"Prerequisite"' not in script and '"Dependent"' not in script


def test_explorer_pages_relations() -> None:
    script = _viewer_source()

    # The reader's relation strip, with its first five links and "Show all", is gone; the statement's disclosure lists every relation.
    assert 'pair[1].slice(0, 5)' not in script and '"Show all " + pair[1].length' not in script
    assert 'nodes.slice(0, state.listLimit)' in script
    assert 'Math.min(LIST_PAGE, nodes.length - visible.length)' in script


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
    """The book's rem sizes set Sources and totals larger than the 17px statement in the pane."""
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
        ".bp-next-kicker, .bp-hero-kicker)"
    )
    totals = ".bp-reading-body :is(.bp-progress-total, .bp-next-title)"
    assert (size(notes), size(labels), size(totals)) == ("14", "12", "16")
    # The workspace sets theorem statements upright; book pages keep amsthm italics.
    assert ".bp-dag-integrated .theorem-style-plain > .bp-thmcontent { font-style: normal; }" in style


def test_explorer_names_its_scope_once_and_lists_only_its_ancestors_above_it() -> None:
    """A kind pill, a rail kicker, the h1, the trail's last crumb and a home link all named the scope or the book."""
    script, style = _viewer_source(), dag_viewer._STYLE
    for retired in ("bp-dag-view-kind", "bp-dag-navigation-kicker", "bp-dag-navigation-home"):
        assert retired not in script and retired not in style
    assert "Current scope" not in script and "Library atlas" not in script
    breadcrumb = script.split("function renderBreadcrumb() {", 1)[1].split("\n    function ", 1)[0]
    assert 'var entries = routeTrail(currentBundle);' in breadcrumb
    assert 'refs.breadcrumb.replaceChildren(trailList(' in breadcrumb
    assert 'refreshReadingLabels(refs.breadcrumb)' in breadcrumb
    assert 'reading_title: pending.reading_title' in script and 'reading_title: record.reading_title' in script
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
    """One persistent context row precedes the controls; Home is its first native breadcrumb, not a second widget."""
    style = dag_viewer._STYLE
    script = _viewer_source()
    assert ".bp-dag-integrated .bp-dag-head { display: block; padding: 0; }" in style
    assert ".bp-dag-integrated .bp-dag-context { display: flex;" in style
    assert ".bp-dag-integrated .bp-dag-breadcrumb { display: block; flex: 1 1 auto; max-width: none; visibility: visible !important; }" in style
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
    assert 'readingLabel("a", !className && index === 0 ? "bp-dag-book" : "", entry.label, entry.reading_title)' in script
    assert "bookLink" not in script and "homeLink" not in script
    assert "placeBook" not in script and "head.insertBefore(bookLink" not in script
    narrow = style.split("@media (max-width: 1100px) {\n", 2)[2].split("\n}", 1)[0]
    assert ".bp-dag-integrated .bp-dag-toolbar { flex-wrap: wrap; }" in narrow
    # The filter starts the second row on a tablet, so its menu anchors to its button's left.
    assert ".bp-dag-integrated .bp-dag-search-wrap { flex-basis: calc(100% - 96px); }" in narrow
    assert ".bp-dag-integrated .bp-dag-filter-popover { left: 0; right: auto;" in narrow
    sheet = script.split("function setSheet(open, selected) {", 1)[1].split("\n    function ", 1)[0]
    assert 'host.classList.toggle("bp-dag-sheet-open", Boolean(open));' in sheet
    assert 'host.classList.toggle("bp-dag-has-selection", Boolean(selected));' in sheet


def test_explorer_head_switches_materials_palette_from_the_trail_row() -> None:
    """The explorer hides Material's header, and with it the theme's sun or moon."""
    script, style = _viewer_source(), dag_viewer._STYLE
    # Only a site whose mkdocs.yml lists palette entries renders the form, so an older dark-only site gets no button.
    assert 'var schemes = document.querySelector("[data-md-component=palette]");' in script
    assert '"Switch light or dark theme"' in script and "flip.title = themeLabel; context.appendChild(flip);" in script
    # It clicks Material's own toggle, the one unhidden label, which steps to the next entry of a two- or three-entry palette.
    # A palette list without toggle keys draws the form but no label, so it gets no switch, and a missing label is never clicked.
    assert 'if (schemes && schemes.querySelector("label")) {' in script
    assert 'var next = schemes.querySelector("label:not([hidden])"); if (next) next.click();' in script
    assert ":checked)" not in script
    rule = next(line for line in style.splitlines() if line.startswith(".bp-dag-viewer .bp-dag-theme {"))
    assert "width: 32px; min-height: 32px;" in rule and "border: 0;" in rule
    # A drawn icon, not a glyph each fallback font sizes its own way; touch reaches 44px through ::after, not a taller row.
    assert ".bp-dag-viewer .bp-dag-theme svg { display: block; width: 18px; height: 18px;" in style
    assert '.bp-dag-viewer .bp-dag-theme::after { content: ""; position: absolute; inset: -6px; }' in style and "position: relative;" in rule
    coarse = style.split("@media (pointer: coarse) {\n", 1)[1].split("\n}", 1)[0]
    assert ".bp-dag-theme" not in coarse and '"◐"' not in script


def test_explorer_toolbar_counts_only_hidden_items() -> None:
    """The toolbar always counted what it showed."""
    script, style = _viewer_source(), dag_viewer._STYLE
    assert "bp-dag-stats" not in script and "bp-dag-stats" not in style and '" shown"' not in script
    assert 'refs.hiddenButton.textContent = "Show " + hidden + " filtered out"; refs.hiddenButton.hidden = !hidden;' in script
    # The four-way view switch is gone.
    assert "bp-dag-modes" not in style and "bp-dag-mode-button" not in style and "bp-dag-mode-button" not in script
    # A touch screen hides no control, and every toolbar control reaches 44px.
    coarse = style.split("@media (pointer: coarse) {\n", 1)[1].split("\n}", 1)[0]
    assert "display: none" not in coarse and "visibility: hidden" not in coarse and "pointer-events: none" not in coarse
    assert (".bp-dag-toolbar :is(.bp-dag-search, .bp-dag-button, .bp-dag-filter-summary, .bp-dag-hidden-button) "
            "{ min-height: 44px; }") in coarse
    assert ".bp-dag-search, .bp-dag-button, .bp-dag-filter-summary, .bp-dag-hidden-button { min-height: 44px; }" in style


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
        'enabled + " of " + presentStatuses.length) + " statuses";'
    ) in script
    assert 'button("bp-dag-filter-summary", "Filter: all statuses")' in script
    # #442's Back to map and Browse replace the Map / Results / Sources / Areas switch, in its own words.
    assert 'button("bp-dag-button", "Back to map")' in script and 'button("bp-dag-button", "Browse", "Browse statements or source texts")' in script
    assert '"Regions"' not in script and '"Areas"' not in script
    # A disabled button never lights up on hover.
    style = dag_viewer._STYLE
    assert ".bp-dag-button:not(:disabled):hover, .bp-dag-filter-summary:hover" in style
    assert "No nodes match" not in script
    # The router loads the index before the shell mounts, so only a retry after a failed load is
    # ever in flight: ensureGlobalSearch marks it, and either outcome or the mounted index clears it.
    assert script.count('refs.search.setAttribute("aria-busy", "true")') == 1
    assert 'state.searchLoading = true; refs.search.setAttribute("aria-busy", "true");' in script
    assert 'state.searchLoading = false; refs.search.removeAttribute("aria-busy");' in script
    assert 'sharedIndex = index; records = new Map(); refs.search.removeAttribute("aria-busy");' in script


def test_workspace_header_presses_one_view_and_gives_the_trail_its_own_row() -> None:
    """Two views read as pressed at once, a deep trail sat in a third of an empty row, and tablets had three rows."""
    script, style = _viewer_source(), dag_viewer._STYLE
    # Back to map shows only away from the map, and Browse marks the list on screen, so at most one control reads as pressed.
    assert 'refs.graphMode.hidden = state.mode === "graph";' in script
    assert 'pressed(refs.listMode, state.mode === "list");' in script
    assert 'if (refs.sourcesMode) pressed(refs.sourcesMode, state.mode === "sources");' in script
    assert script.count("pressed(refs.") == 2 and 'var areas = state.mode === "graph"' not in script
    # The closed Browse toggle carries the pressed look while a list or the sources replace the map.
    assert (".bp-dag-browse-popover .bp-dag-button[aria-pressed=true], .bp-dag-pivot[aria-pressed=true], "
            ".bp-dag-viewer:has(.bp-dag-stage[hidden]) .bp-dag-browse > .bp-dag-button { border-color: var(--dag-link);") in style
    # A phone hangs the Browse menu from the toolbar, since Browse can wrap to the start of a row.
    phone = style.split("@media (max-width: 720px) {\n  .bp-dag-integrated .bp-dag-body", 1)[1].split("\n}", 1)[0]
    assert ".bp-dag-integrated .bp-dag-toolbar { position: relative; }" in phone
    assert ".bp-dag-integrated :is(.bp-dag-browse, .bp-dag-filters) { position: static; }" in phone
    # The filter's 300px legend hangs from the toolbar too: from Filter's place on the second row it ran past a 320px screen.
    assert ".bp-dag-integrated .bp-dag-browse-popover, .bp-dag-viewer.bp-dag-integrated .bp-dag-filter-popover { left: 12px; right: 12px; width: auto; max-width: none; }" in phone
    assert ".bp-dag-integrated .bp-dag-breadcrumb { display: block; flex: 1 1 auto; max-width: none;" in style
    assert ".bp-dag-viewer.bp-dag-integrated .bp-dag-breadcrumb li { min-width: 4em; }" in style
    assert ".bp-dag-viewer.bp-dag-integrated .bp-dag-breadcrumb li:first-child { flex: none; min-width: 0; }" in style
    home = re.search(r"^\.bp-dag-integrated \.bp-dag-book \{([^}]+)\}", style, re.M)
    assert home is not None and dict(re.findall(r"([\w-]+):\s*([^;]+);", home.group(1)))["margin"] == "0"
    assert ".bp-dag-viewer.bp-dag-integrated .bp-dag-breadcrumb li:has(.bp-dag-sr-only) { flex: none; min-width: 0; }" in style
    tablet = style.split("@media (max-width: 1100px) {\n  .bp-dag-integrated", 1)[1].split("\n}", 1)[0]
    # Two rows, search and Topics on the first; search no longer takes a row of its own.
    assert ".bp-dag-integrated .bp-dag-search-wrap { flex-basis: calc(100% - 96px); }" in tablet
    # The second row never wraps, on a tablet or a phone: the filter grows from 100px into its free space and truncates its summary instead.
    assert ("@media (max-width: 1100px) {\n"
            "  .bp-dag-integrated .bp-dag-filters { flex: 1 1 100px; min-width: 0; max-width: max-content; }\n"
            "  .bp-dag-integrated .bp-dag-filter-summary { display: block; max-width: 100%; overflow: hidden; line-height: 22px; text-overflow: ellipsis; }") in style
    # Topics sat a few px above the control beside it.
    assert ".bp-dag-integrated .bp-dag-outline-toggle { display: inline-flex; align-items: center; }" in tablet
    phones = style.split("@media (max-width: 720px) {\n  .bp-dag-integrated", 1)[1].split("\n}", 1)[0]
    assert ".bp-dag-integrated .bp-dag-book { margin-right:" not in phones
    assert (".bp-dag-integrated :is(.bp-dag-filters, .bp-dag-browse, .bp-dag-hidden-button) "
            "{ flex: 1 1 44px; min-width: 44px; max-width: max-content; }") in phones
    assert (".bp-dag-integrated :is(.bp-dag-filter-summary, .bp-dag-browse > .bp-dag-button, .bp-dag-hidden-button) "
            "{ min-width: 44px; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }") in phones
    assert ".bp-dag-integrated .bp-dag-breadcrumb li:not(:first-child, :nth-last-child(-n+2)) { display: none; }" not in phones
    # The real renderer folds before layout rather than hiding ancestor nodes
    # a second time; its summary retains their complete accessible labels.
    assert 'refs.breadcrumb.replaceChildren(trailList(entries, null, !sheetLayout()));' in script
    assert 'entries.slice(1, -2).map(function (entry) { return entry.label; }).join(" / ")' in script
    # A phone keeps search and Topics on the first row too; Topics on the second pushed the toolbar to three rows.
    assert "search-wrap { flex-basis: 100%; }" not in phones


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


def test_search_placeholder_uses_opaque_readable_book_tokens_in_both_schemes() -> None:
    style = dag_viewer._STYLE
    placeholder = re.search(r"^\.bp-dag-search::placeholder \{([^}]+)\}", style, re.M)
    assert placeholder is not None
    declarations = dict(re.findall(r"([\w-]+):\s*([^;]+);", placeholder.group(1)))
    assert declarations["color"] == "var(--dag-muted)" and declarations["opacity"] == "1"
    field = re.search(r"^\.bp-dag-search \{([^}]+)\}", style, re.M)
    assert field is not None and "background: var(--dag-panel);" in field.group(1)
    assert "--dag-muted: var(--bp-muted, #65676b);" in style
    panel = re.search(r"--dag-panel: color-mix\(in srgb, var\(--dag-bg\) (\d+)%, (#[\da-f]+) \d+%\)", style)
    assert panel is not None
    book = render._stylesheet()
    for selector in (":root", "[data-md-color-scheme=slate]"):
        rule = re.search(re.escape(selector) + r" \{([^}]+)\}", book)
        assert rule is not None
        tokens = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", rule.group(1)))
        background = _mix(tokens["--bp-surface"], panel.group(2), int(panel.group(1)) / 100)
        assert _contrast(tokens["--bp-muted"], background) >= 4.5, selector


def test_a_map_that_cannot_load_leaves_a_readable_notice_in_the_middle_of_the_stage() -> None:
    """Without the canvas, a map script that failed to load left the stage empty."""
    notice = re.search(r"^\.bp-dag-viewer\.bp-dag-viewer \.bp-dag-map-unavailable \{([^}]+)\}", dag_viewer._STYLE, re.M)
    assert notice is not None
    declarations = dict(re.findall(r"([\w-]+):\s*([^;]+);", notice.group(1)))
    assert (declarations["position"], declarations["top"], declarations["left"]) == ("absolute", "50%", "50%")
    assert declarations["transform"] == "translate(-50%, -50%)" and declarations["max-width"] == "calc(100% - 32px)"
    # Muted text on the page's surface, which the book's tokens keep above 4.5:1 in both schemes.
    assert (declarations["color"], declarations["background"]) == ("var(--dag-muted)", "var(--dag-bg)")
    book = render._stylesheet()
    for selector in (":root", "[data-md-color-scheme=slate]"):
        rule = re.search(re.escape(selector) + r" \{([^}]+)\}", book)
        assert rule is not None
        tokens = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", rule.group(1)))
        assert _contrast(tokens["--bp-muted"], tokens["--bp-surface"]) >= 4.5, selector


def test_focus_and_the_current_statement_do_not_borrow_status_blue() -> None:
    """Focus rings were about 2:1 and the rail showed focus as hover."""
    style = dag_viewer._STYLE
    # One solid ring for every focus-visible state, drawn inside rows.
    assert "color-mix(in srgb, var(--dag-link) 45%" not in style and "52%" not in style
    assert [rule.split(" {")[0] for rule in style.splitlines() if rule.startswith(".bp-dag-viewer") and ":focus-visible {" in rule] == [
        ".bp-dag-viewer :is(button, a, [tabindex]):focus-visible",
        ".bp-dag-viewer :is(.bp-dag-result, .bp-dag-inventory-row, .bp-dag-navigation-link, .bp-reading-body, .bp-dag-sheet-handle):focus-visible",
    ]
    assert ":focus-visible { outline: 2px solid var(--dag-link); outline-offset: 2px; }" in style
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

    # --bp-link on the explorer's surfaces: page, map, panel, and the 10% and 9% current-row tints, at the 4.5:1 small text needs.
    # The dark link was #2D88FF, 4.44:1 on the panel and 4.18:1 on the outline's current row; it is #4A9AFF.
    for link, page, field in (("#0064E0", "#FFFFFF", "#F0F2F5"), ("#4A9AFF", "#242526", "#18191A")):
        surfaces = (page, field, mixed(page, "#64748B", 0.94), mixed(link, page, 0.10), mixed(link, page, 0.09))
        assert min(_contrast(link, surface) for surface in surfaces) >= 4.5, link
        # The old ring, link at 45% over the page, measured about 2:1 against it.
        assert _contrast(mixed(link, page, 0.45), page) < 3


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


def test_status_strokes_outline_cards_on_both_maps() -> None:
    """A tinted card is barely darker than the map, so its stroke alone has to draw the card's edge."""
    assert "--dag-map: var(--bp-sunken, #f0f2f5);" in dag_viewer._STYLE
    light_maps, dark_maps = ("#F0F2F5", "#FFFFFF"), ("#18191A", "#242526")
    # Planned and the inventory greys are quiet on purpose; the ring tests keep those three apart.
    for state in status.STATES:
        if state.key != "planned":
            assert min(_contrast(state.stroke, surface) for surface in (state.fill, *light_maps)) >= 3, state.key
            assert min(_contrast(state.dark_stroke, surface) for surface in (state.dark_fill, *dark_maps)) >= 3, state.key


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


def test_the_reader_opens_a_statements_own_relations_and_nothing_repeats_them() -> None:
    """A pill repeated a card's fully proved share, and a strip under the reader repeated the statement's relations."""
    script = _viewer_source()
    # The details pill that repeated a card's fully proved share left with the standalone inspector,
    # and the integrated reader shows the scope's own article instead.
    assert 'element("span", "bp-dag-status-pill",' not in script
    assert '" items in this scope"' not in script and '"Mathematical regions"' not in script
    assert '" → Result → "' not in script
    assert 'var relations = target.querySelector(".bp-dependencies"); if (relations) relations.open = true;' in script
    # What stays under the reader is #442's topic pages and concept links, never a second relations strip.
    for gone in ("bp-reading-links", '"bp-reading-related"'):
        assert gone not in script, gone
    for gone in (".bp-reading-links", ".bp-reading-related {", ".bp-reading-related a", ".bp-reading-related strong"):
        assert gone not in dag_viewer._STYLE, gone


def test_small_status_marks_fill_finished_states_with_the_stroke() -> None:
    """The light tints made rail swatches and dependency dots of finished work vanish."""
    script = _viewer_source()
    assert 'function solid(key, color) { return ((palette[key] || palette.planned || {}).light || {}).fill === "#FFFFFF" ? color.fill : color.stroke; }' in script
    assert '"-fill", solid(key, color) || "transparent");' in script


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

    # The reader, hierarchy rail and bounded layout share one prebuilt
    # response; consumers still need no application build or runtime CDN.
    assert len(dag_viewer.viewer_script().encode()) < 145_000
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


def test_payload_preserves_dependencies_and_collapsed_cycles_without_server_coordinates(tmp_path: Path) -> None:
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

    for view, expected in (
        (acyclic, [{"source": "base", "target": "result", "statement": 1, "proof": 0}]),
        (cyclic, [
            {"source": "base", "target": "result", "statement": 1, "proof": 0},
            {"source": "result", "target": "base", "statement": 0, "proof": 1},
        ]),
    ):
        outputs = [tmp_path / f"{view.kind}-{index}.json" for index in range(2)]
        for output in outputs:
            dag_viewer.write_payload(output, view, links={})
        assert outputs[0].read_bytes() == outputs[1].read_bytes()
        payload = json.loads(outputs[0].read_text(encoding="utf-8"))
        assert [node["id"] for node in payload["nodes"]] == ["result", "base"]
        assert all(node["layout"] == {"height": 56, "width": 196} for node in payload["nodes"])
        assert all(node["status_counts"] == [["planned", 1]] and not node["isolated"] for node in payload["nodes"])
        assert payload["edges"] == expected
        assert payload["edge_count"] == len(expected)
        assert payload["view"]["presentation"] == "dag" and payload["regions"] == []


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
