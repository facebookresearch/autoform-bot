from __future__ import annotations

import functools
import threading
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from autoform_cli import dag_viewer
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode

playwright = pytest.importorskip(
    "playwright.sync_api",
    reason="the optional browser extra is required for WebKit regression tests",
)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="session")
def webkit_browser() -> Iterator[object]:
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.webkit.launch()
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("WebKit is not installed; run `playwright install webkit`")
            raise
        yield browser
        browser.close()


@pytest.fixture()
def explorer_url(tmp_path: Path) -> Iterator[str]:
    core_nodes = (
        ViewNode(
            id="chapter/foundation",
            title="Foundation",
            kind="node",
            members=("chapter/foundation",),
            status_counts=(("fully_proved", 1),),
            declaration="def",
            status_key="fully_proved",
        ),
        ViewNode(
            id="chapter/middle",
            title="Middle lemma",
            kind="node",
            members=("chapter/middle",),
            status_counts=(("can_prove", 1),),
            declaration="lemma",
            status_key="can_prove",
        ),
        ViewNode(
            id="chapter/target",
            title="Target theorem",
            kind="node",
            members=("chapter/target",),
            status_counts=(("planned", 1),),
            declaration="theorem",
            status_key="planned",
        ),
    )
    nodes = core_nodes + tuple(
        ViewNode(
            id=f"inventory/auxiliary-{index}",
            title=f"Auxiliary inventory {index}",
            kind="node",
            members=(f"inventory/auxiliary-{index}",),
            status_counts=(("planned", 1),),
            declaration="lemma",
            status_key="planned",
        )
        for index in range(60)
    )
    view = GraphView(
        kind="full",
        title="Standalone dependency explorer",
        nodes=nodes,
        edges=(
            ViewEdge(nodes[0].id, nodes[1].id, statement_count=1),
            ViewEdge(nodes[1].id, nodes[2].id, statement_count=1),
        ),
    )
    links = {node.id: f"articles.html#{node.id}" for node in nodes}
    dag_viewer.write_payload(tmp_path / "graph.json", view, links=links)
    remote = ViewNode(
        id="remote/deep-theorem",
        title="Remote theorem",
        kind="node",
        members=("remote/deep-theorem",),
        status_counts=(("planned", 1),),
        declaration="theorem",
        status_key="planned",
    )
    dag_viewer.write_search_index(
        tmp_path / "search.json",
        GraphView(kind="full", title="Search", nodes=(*nodes, remote), edges=()),
        context_links={
            **{node.id: f"articles.html#{node.id}" for node in nodes},
            remote.id: "other.html#node=remote%2Fdeep-theorem",
        },
    )
    (tmp_path / "viewer.js").write_text(dag_viewer.viewer_script(), encoding="utf-8")

    fallback_links = tuple((node.title, links[node.id]) for node in core_nodes)
    explorer = dag_viewer.render_container(
        "graph.json",
        script_href="viewer.js",
        fallback_links=fallback_links,
        fallback_total=len(nodes),
        layout="embedded",
        search_href="search.json",
    )
    failed_explorer = dag_viewer.render_container(
        "missing.json",
        script_href="viewer.js",
        fallback_links=fallback_links,
        fallback_total=len(nodes),
        layout="embedded",
        search_href="search.json",
    )
    app_explorer = dag_viewer.render_container(
        "graph.json",
        script_href="viewer.js",
        fallback_links=fallback_links,
        fallback_total=len(nodes),
        layout="app",
        search_href="search.json",
    )
    page_shell = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Autoform explorer regression fixture</title>
  <style>
    body { margin: 0; font-family: sans-serif; }
    .page-spacer { height: 48rem; }
    .fixture { max-width: 76rem; margin: 0 auto; padding: 1rem; }
  </style>
</head>
<body>
  <div class="page-spacer" data-page-spacer="before"></div>
  <main class="fixture">{explorer}</main>
  <div class="page-spacer" data-page-spacer="after"></div>
</body>
</html>
"""
    (tmp_path / "index.html").write_text(page_shell.replace("{explorer}", explorer), encoding="utf-8")
    (tmp_path / "failure.html").write_text(page_shell.replace("{explorer}", failed_explorer), encoding="utf-8")
    (tmp_path / "duplicate.html").write_text(
        page_shell.replace("{explorer}", explorer + '\n<script defer src="viewer.js"></script>'),
        encoding="utf-8",
    )
    (tmp_path / "other.html").write_text("<!doctype html><title>Remote scope</title>", encoding="utf-8")
    (tmp_path / "app.html").write_text(
        page_shell.replace('<div class="page-spacer" data-page-spacer="before"></div>', "")
        .replace('<div class="page-spacer" data-page-spacer="after"></div>', "")
        .replace('class="fixture"', 'class="fixture" style="max-width:none;padding:0"')
        .replace("{explorer}", app_explorer),
        encoding="utf-8",
    )
    dense_nodes = tuple(
        ViewNode(
            id=f"dense/chapter-{index}",
            title=f"Dense chapter {index}",
            kind="scope",
            members=(f"dense/chapter-{index}/item",),
            status_counts=(("planned", 1),),
            status_key="planned",
        )
        for index in range(33)
    )
    dense_views = {
        "chain": GraphView(
            kind="project",
            title="Connected chain",
            nodes=dense_nodes,
            edges=tuple(
                ViewEdge(dense_nodes[index].id, dense_nodes[index + 1].id, statement_count=1)
                for index in range(len(dense_nodes) - 1)
            ),
        ),
        "star": GraphView(
            kind="project",
            title="Connected star",
            nodes=dense_nodes,
            edges=tuple(
                ViewEdge(dense_nodes[0].id, dense_nodes[index].id, statement_count=1)
                for index in range(1, len(dense_nodes))
            ),
        ),
    }
    flat_nodes = tuple(
        ViewNode(
            id=f"flat/item-{index}",
            title=f"Flat item {index}",
            kind="node",
            members=(f"flat/item-{index}",),
            status_counts=(("planned", 1),),
            status_key="planned",
        )
        for index in range(1_000)
    )
    dense_views["flat"] = GraphView(kind="chapter", title="Flat scope", nodes=flat_nodes, edges=())
    mixed_nodes = tuple(
        ViewNode(
            id=node_id,
            title=node_id,
            kind="node",
            members=(node_id,),
            status_counts=(("planned", 1),),
            status_key="planned",
        )
        for node_id in ("mixed/a", "mixed/b", "mixed/isolate-1", "mixed/isolate-2")
    )
    dense_views["mixed"] = GraphView(
        kind="chapter",
        title="Mixed scope",
        nodes=mixed_nodes,
        edges=(ViewEdge("mixed/a", "mixed/b", statement_count=1),),
    )
    atlas_nodes = (
        ViewNode(
            id="atlas/topology",
            title="Topology",
            kind="scope",
            members=tuple(f"atlas/topology/item-{index}" for index in range(80)),
            status_counts=(("fully_proved", 60), ("planned", 20)),
            status_key="planned",
            summary="Topology studies continuity, deformation, and invariants preserved by continuous maps.",
            area="Geometry & Topology",
        ),
        ViewNode(
            id="atlas/algebra",
            title="Algebra",
            kind="scope",
            members=tuple(f"atlas/algebra/item-{index}" for index in range(40)),
            status_counts=(("fully_proved", 40),),
            status_key="fully_proved",
            summary="Algebra organizes structures and the maps that preserve them.",
            area="Algebra",
        ),
        ViewNode(
            id="atlas/analysis",
            title="Analysis",
            kind="scope",
            members=tuple(f"atlas/analysis/item-{index}" for index in range(20)),
            status_counts=(("can_prove", 20),),
            status_key="can_prove",
            summary="Analysis studies limits, approximation, measure, and functional spaces.",
            area="Analysis & Probability",
        ),
    )
    dense_views["atlas"] = GraphView(kind="project", title="Knowledge atlas", nodes=atlas_nodes, edges=())
    app_shell = (
        page_shell.replace('<div class="page-spacer" data-page-spacer="before"></div>', "")
        .replace('<div class="page-spacer" data-page-spacer="after"></div>', "")
        .replace('class="fixture"', 'class="fixture" style="max-width:none;padding:0"')
    )
    for name, dense_view in dense_views.items():
        links = (
            {node.id: f"other.html#node={node.id}" for node in dense_view.nodes}
            if name == "atlas"
            else {}
        )
        dag_viewer.write_payload(tmp_path / f"{name}.json", dense_view, links=links)
        host = dag_viewer.render_container(f"{name}.json", script_href="viewer.js", layout="app")
        (tmp_path / f"{name}.html").write_text(
            app_shell.replace("{explorer}", host),
            encoding="utf-8",
        )

    handler = functools.partial(_QuietHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _open_ready(page: object, explorer_url: str) -> None:
    page.goto(f"{explorer_url}/index.html")
    page.locator(".bp-dag-stage").wait_for()
    page.locator("[data-autoform-node-id]").first.wait_for()
    playwright.expect(page.locator("main")).to_have_count(1)


def test_mobile_node_interaction_and_selected_sheet_collapse(webkit_browser: object, explorer_url: str) -> None:
    context = webkit_browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    _open_ready(page, explorer_url)

    node = page.locator('[data-autoform-node-id="chapter/middle"]')
    node.tap()

    playwright.expect(node).to_have_attribute("data-selected", "true")
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text("Middle lemma")
    handle = page.locator(".bp-dag-sheet-handle")
    inspector_inner = page.locator(".bp-dag-inspector-inner")
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(handle).to_have_attribute("aria-controls", "bp-dag-inspector-content-1")
    assert inspector_inner.get_attribute("aria-hidden") is None
    assert inspector_inner.get_attribute("inert") is None
    assert "node=chapter%2Fmiddle" in page.url

    handle.tap()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    playwright.expect(inspector_inner).to_have_attribute("aria-hidden", "true")
    playwright.expect(inspector_inner).to_have_attribute("inert", "")
    assert not page.locator(".bp-dag-viewer").evaluate(
        "element => element.classList.contains('bp-dag-sheet-open')"
    )
    handle.focus()
    page.keyboard.press("Tab")
    assert not page.evaluate(
        "document.querySelector('.bp-dag-inspector-inner').contains(document.activeElement)"
    )
    context.close()


def test_app_layout_fills_the_browser_viewport(webkit_browser: object, explorer_url: str) -> None:
    page = webkit_browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"{explorer_url}/app.html")
    page.locator(".bp-dag-stage").wait_for()
    viewer = page.locator('.bp-dag-viewer[data-layout="app"]')
    box = viewer.bounding_box()
    assert box is not None
    assert box["x"] <= 1
    assert box["y"] <= 1
    assert box["width"] >= 1430
    assert box["height"] >= 890
    stage = page.locator(".bp-dag-stage").bounding_box()
    assert stage is not None
    assert stage["width"] >= 1000
    page.close()


@pytest.mark.parametrize("page_name", ("chain", "star"))
def test_connected_project_maps_open_at_readable_scale(
    webkit_browser: object, explorer_url: str, page_name: str
) -> None:
    page = webkit_browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"{explorer_url}/{page_name}.html")
    node = page.locator(".bp-dag-node").first
    node.wait_for()
    box = node.bounding_box()
    assert box is not None
    assert box["height"] >= 44
    playwright.expect(page.locator(".bp-dag-density")).to_contain_text("Readable window")
    page.close()


def test_atlas_bubbles_open_authored_mathematical_context(
    webkit_browser: object, explorer_url: str
) -> None:
    page = webkit_browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"{explorer_url}/atlas.html")
    topic = page.locator('[data-autoform-node-id="atlas/topology"]')
    topic.wait_for()
    assert page.locator(".bp-dag-viewer").evaluate(
        "element => element.classList.contains('bp-dag-atlas')"
    )
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text("Knowledge atlas")
    playwright.expect(page.locator(".bp-dag-inspector")).to_contain_text("Geometry & Topology")
    playwright.expect(page.locator(".bp-dag-inspector")).to_contain_text(
        "No cross-topic dependency links are authored"
    )
    assert "50%" in topic.evaluate("element => getComputedStyle(element).borderRadius")
    assert "conic-gradient" in topic.evaluate("element => getComputedStyle(element).backgroundImage")
    topic.click()
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text("Topology")
    playwright.expect(page.locator(".bp-dag-summary")).to_contain_text(
        "continuity, deformation, and invariants"
    )
    playwright.expect(page.get_by_role("link", name="Explore topic")).to_be_visible()
    page.close()


def test_oversized_flat_scope_defaults_to_browse(webkit_browser: object, explorer_url: str) -> None:
    page = webkit_browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"{explorer_url}/flat.html")
    inventory = page.locator(".bp-dag-inventory")
    inventory.wait_for()
    playwright.expect(inventory).to_be_visible()
    playwright.expect(page.locator(".bp-dag-mode-button").first).to_be_disabled()
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_contain_text(
        "map unavailable above 120 items"
    )
    playwright.expect(page.locator(".bp-dag-node")).to_have_count(0)
    page.close()


def test_mobile_browse_pagination_stays_above_the_collapsed_sheet(
    webkit_browser: object, explorer_url: str
) -> None:
    context = webkit_browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    page.goto(f"{explorer_url}/flat.html")
    more = page.locator(".bp-dag-inventory > .bp-dag-load-more")
    more.wait_for()
    more.scroll_into_view_if_needed()
    box = more.bounding_box()
    handle_box = page.locator(".bp-dag-sheet-handle").bounding_box()
    assert box is not None and handle_box is not None
    assert box["y"] + box["height"] <= handle_box["y"]
    more.tap()
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_contain_text("200 of 1000 listed")
    context.close()


def test_mobile_isolates_do_not_overlap_the_connected_dag(webkit_browser: object, explorer_url: str) -> None:
    context = webkit_browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    page.goto(f"{explorer_url}/mixed.html")
    connected = page.locator('[data-autoform-node-id="mixed/a"]')
    isolated = page.locator('[data-autoform-node-id="mixed/isolate-1"]')
    connected.wait_for()
    isolated.wait_for()
    a, b = connected.bounding_box(), isolated.bounding_box()
    assert a is not None and b is not None
    assert a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"]
    context.close()


def test_plain_wheel_scrolls_the_page_instead_of_being_trapped(webkit_browser: object, explorer_url: str) -> None:
    page = webkit_browser.new_page(viewport={"width": 1280, "height": 720})
    _open_ready(page, explorer_url)
    stage = page.locator(".bp-dag-stage")
    stage.scroll_into_view_if_needed()
    box = stage.bounding_box()
    assert box is not None
    before = page.evaluate("window.scrollY")

    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 420)

    page.wait_for_function("before => window.scrollY > before", arg=before)
    assert page.evaluate("window.scrollY") > before
    page.close()


def test_search_keyboard_navigation_and_focus(webkit_browser: object, explorer_url: str) -> None:
    page = webkit_browser.new_page(viewport={"width": 1280, "height": 900})
    _open_ready(page, explorer_url)
    search = page.locator(".bp-dag-search")
    search.fill("middle")
    playwright.expect(search).to_have_attribute("aria-expanded", "true")

    search.press("ArrowDown")
    result = page.locator(".bp-dag-result").first
    playwright.expect(result).to_be_focused()
    result.press("Escape")
    playwright.expect(search).to_be_focused()
    playwright.expect(search).to_have_attribute("aria-expanded", "false")

    search.fill("target")
    search.press("Enter")
    playwright.expect(page.locator('[data-autoform-node-id="chapter/target"]')).to_be_focused()
    assert "node=chapter%2Ftarget" in page.url
    page.close()


def test_global_search_jumps_to_the_smallest_context(webkit_browser: object, explorer_url: str) -> None:
    page = webkit_browser.new_page(viewport={"width": 1280, "height": 900})
    _open_ready(page, explorer_url)
    search = page.locator(".bp-dag-search")
    search.fill("Remote theorem")
    result = page.locator(".bp-dag-result", has_text="Remote theorem")
    result.wait_for()
    result.click()
    page.wait_for_url("**/other.html#node=remote%2Fdeep-theorem")
    page.close()


def test_legacy_node_hash_resolves_through_the_global_index(webkit_browser: object, explorer_url: str) -> None:
    page = webkit_browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{explorer_url}/app.html#node=remote%2Fdeep-theorem")
    page.wait_for_url("**/other.html#node=remote%2Fdeep-theorem")

    page.goto(f"{explorer_url}/app.html#node=unknown%2Fitem")
    page.locator(".bp-dag-stage").wait_for()
    page.wait_for_timeout(100)
    assert page.url.endswith("app.html#node=unknown%2Fitem")
    page.close()


def test_paginated_search_and_arrow_navigation_restore_focus(
    webkit_browser: object, explorer_url: str
) -> None:
    page = webkit_browser.new_page(viewport={"width": 1280, "height": 900})
    _open_ready(page, explorer_url)
    foundation = page.locator('[data-autoform-node-id="chapter/foundation"]')
    foundation.focus()
    foundation.press("ArrowRight")
    playwright.expect(page.locator('[data-autoform-node-id="chapter/middle"]')).to_be_focused()

    search = page.locator(".bp-dag-search")
    search.fill("auxiliary")
    more = page.locator(".bp-dag-search-results .bp-dag-load-more")
    playwright.expect(more).to_have_text("Show 48 more of 48")
    more.focus()
    more.press("Enter")
    playwright.expect(page.locator(".bp-dag-result").nth(12)).to_be_focused()
    page.close()


def test_direct_list_view_has_current_shown_and_hidden_counts(
    webkit_browser: object, explorer_url: str
) -> None:
    page = webkit_browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{explorer_url}/index.html#view=list&status=fully_proved")
    page.locator(".bp-dag-inventory").wait_for()
    playwright.expect(page.locator(".bp-dag-stats")).to_contain_text("1 of 63 shown")
    playwright.expect(page.locator(".bp-dag-hidden-button")).to_have_text("+62 hidden")
    page.close()


def test_reselect_does_not_duplicate_history_and_back_restores_selection(
    webkit_browser: object, explorer_url: str
) -> None:
    page = webkit_browser.new_page(viewport={"width": 1280, "height": 900})
    _open_ready(page, explorer_url)
    foundation = page.locator('[data-autoform-node-id="chapter/foundation"]')
    target = page.locator('[data-autoform-node-id="chapter/target"]')
    initial_length = page.evaluate("history.length")

    foundation.click()
    after_first_selection = page.evaluate("history.length")
    foundation.click()
    assert page.evaluate("history.length") == after_first_selection == initial_length + 1

    target.click()
    assert page.evaluate("history.length") == initial_length + 2
    page.evaluate("history.back()")
    page.wait_for_function("location.hash.includes('chapter%2Ffoundation')")
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text("Foundation")
    playwright.expect(foundation).to_have_attribute("data-selected", "true")
    page.close()


def test_hiding_the_selected_status_clears_selection(
    webkit_browser: object, explorer_url: str
) -> None:
    page = webkit_browser.new_page(viewport={"width": 1280, "height": 900})
    _open_ready(page, explorer_url)
    page.locator('[data-autoform-node-id="chapter/target"]').click()
    assert "node=chapter%2Ftarget" in page.url

    page.locator(".bp-dag-filter-summary").click()
    page.locator('.bp-dag-filter-popover input[value="planned"]').uncheck()

    assert "node=" not in page.url
    assert "bp-dag-has-selection" not in (page.locator(".bp-dag-viewer").get_attribute("class") or "")
    page.close()


def test_no_javascript_keeps_the_linked_fallback(webkit_browser: object, explorer_url: str) -> None:
    context = webkit_browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.goto(f"{explorer_url}/index.html")

    fallback = page.locator(".bp-dag-fallback")
    playwright.expect(fallback).to_be_visible()
    playwright.expect(fallback.locator("li")).to_have_count(3)
    playwright.expect(fallback.get_by_role("link", name="Download the complete graph data")).to_have_attribute(
        "href", "graph.json"
    )
    context.close()


def test_fetch_failure_restores_the_linked_fallback(webkit_browser: object, explorer_url: str) -> None:
    page = webkit_browser.new_page()
    page.goto(f"{explorer_url}/failure.html")

    playwright.expect(page.locator(".bp-dag-error")).to_contain_text("HTTP 404")
    fallback = page.locator(".bp-dag-fallback")
    playwright.expect(fallback).to_be_visible()
    playwright.expect(fallback.get_by_role("link", name="Middle lemma")).to_have_attribute(
        "href", "articles.html#chapter/middle"
    )
    page.close()


def test_duplicate_script_include_mounts_one_explorer(
    webkit_browser: object, explorer_url: str
) -> None:
    page = webkit_browser.new_page()
    page.goto(f"{explorer_url}/duplicate.html")
    page.locator(".bp-dag-stage").wait_for()
    playwright.expect(page.locator(".bp-dag-head")).to_have_count(1)
    page.close()
