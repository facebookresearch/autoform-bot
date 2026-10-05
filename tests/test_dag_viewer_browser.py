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

DEEP_NODE_COUNT = 12_769


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
        for index in range(120)
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
    (tmp_path / "viewer.js").write_text(dag_viewer.viewer_script(), encoding="utf-8")

    fallback_links = tuple((node.title, links[node.id]) for node in core_nodes)
    explorer = dag_viewer.render_container(
        "graph.json",
        script_href="viewer.js",
        fallback_links=fallback_links,
        fallback_total=len(nodes),
    )
    failed_explorer = dag_viewer.render_container(
        "missing.json",
        script_href="viewer.js",
        fallback_links=fallback_links,
        fallback_total=len(nodes),
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
    duplicate_explorer = explorer + '\n<script defer src="viewer.js"></script>'
    (tmp_path / "duplicate.html").write_text(
        page_shell.replace("{explorer}", duplicate_explorer),
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


@pytest.fixture()
def deep_explorer_url(tmp_path: Path) -> Iterator[str]:
    nodes = tuple(
        ViewNode(
            id=f"deep/node-{index}",
            title=f"Deep node {index}",
            kind="node",
            members=(f"deep/node-{index}",),
            status_counts=(("planned", 1),),
            status_key="planned",
        )
        for index in range(DEEP_NODE_COUNT)
    )
    view = GraphView(
        kind="full",
        title="Deep dependency explorer",
        nodes=nodes,
        edges=tuple(
            ViewEdge(nodes[index].id, nodes[index + 1].id, statement_count=1)
            for index in range(DEEP_NODE_COUNT - 1)
        ),
    )
    dag_viewer.write_payload(tmp_path / "graph.json", view, links={})
    (tmp_path / "viewer.js").write_text(dag_viewer.viewer_script(), encoding="utf-8")
    explorer = dag_viewer.render_container("graph.json", script_href="viewer.js")
    (tmp_path / "index.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        f'<body><main>{explorer}</main></body></html>',
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

    page.set_viewport_size({"width": 1000, "height": 844})
    page.wait_for_function(
        "element => !element.hasAttribute('inert') && !element.hasAttribute('aria-hidden')",
        arg=inspector_inner.element_handle(),
    )
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


def test_fit_and_zoom_out_keep_a_deep_graph_inside_the_viewport(
    webkit_browser: object, deep_explorer_url: str
) -> None:
    page = webkit_browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{deep_explorer_url}/index.html")
    page.locator(".bp-dag-stage").wait_for()
    density = page.locator(".bp-dag-density")
    message = f"Zoom in to reveal {DEEP_NODE_COUNT} node labels"

    playwright.expect(density).to_have_text(message, timeout=30_000)
    page.get_by_role("button", name="Zoom out").click()
    playwright.expect(density).to_have_text(message, timeout=30_000)
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
    playwright.expect(more).to_have_text("Show 50 more of 108")
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
    playwright.expect(page.locator(".bp-dag-stats")).to_contain_text("1 of 123 shown")
    playwright.expect(page.locator(".bp-dag-hidden-button")).to_have_text("+122 hidden")
    page.close()


def test_mobile_list_pagination_stays_above_the_collapsed_sheet(
    webkit_browser: object, explorer_url: str
) -> None:
    context = webkit_browser.new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
    )
    page = context.new_page()
    _open_ready(page, explorer_url)
    page.get_by_role("button", name="List", exact=True).tap()

    more = page.locator(".bp-dag-inventory > .bp-dag-load-more")
    playwright.expect(more).to_be_visible()
    box = more.bounding_box()
    assert box is not None
    assert page.evaluate(
        "point => Boolean(document.elementFromPoint(point.x, point.y).closest('.bp-dag-load-more'))",
        {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2},
    )
    more.tap()
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text(
        "123 of 123 listed · isolates included"
    )
    context.close()


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


def test_hiding_the_selected_status_clears_selection_and_durable_location(
    webkit_browser: object, explorer_url: str
) -> None:
    page = webkit_browser.new_page(viewport={"width": 1280, "height": 900})
    _open_ready(page, explorer_url)
    initial_length = page.evaluate("history.length")
    target = page.locator('[data-autoform-node-id="chapter/target"]')
    target.click()
    assert page.evaluate("history.length") == initial_length + 1
    assert "node=chapter%2Ftarget" in page.url

    page.locator(".bp-dag-filter-summary").click()
    planned = page.locator('.bp-dag-filter-popover input[value="planned"]')
    planned.uncheck()

    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text(
        "Standalone dependency explorer"
    )
    assert "node=" not in page.url
    assert page.evaluate("history.length") == initial_length + 1
    playwright.expect(planned).not_to_be_checked()

    page.reload()
    page.locator(".bp-dag-stage").wait_for()
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text(
        "Standalone dependency explorer"
    )
    assert "node=" not in page.url
    playwright.expect(page.locator('.bp-dag-filter-popover input[value="planned"]')).not_to_be_checked()

    page.go_back()
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text(
        "Standalone dependency explorer"
    )
    playwright.expect(page.locator('.bp-dag-filter-popover input[value="planned"]')).to_be_checked()
    assert "node=" not in page.url
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


def test_duplicate_consumer_script_include_mounts_one_explorer(
    webkit_browser: object, explorer_url: str
) -> None:
    page = webkit_browser.new_page()
    page.goto(f"{explorer_url}/duplicate.html")
    page.locator(".bp-dag-stage").wait_for()

    playwright.expect(page.locator(".bp-dag-head")).to_have_count(1)
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_attribute(
        "data-dag-ready", "true"
    )
    page.close()
