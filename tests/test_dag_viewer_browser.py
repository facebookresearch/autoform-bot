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


def test_mobile_node_interaction_and_selected_sheet_collapse(webkit_browser: object, explorer_url: str) -> None:
    context = webkit_browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    _open_ready(page, explorer_url)

    node = page.locator('[data-autoform-node-id="chapter/middle"]')
    node.tap()

    playwright.expect(node).to_have_attribute("data-selected", "true")
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text("Middle lemma")
    playwright.expect(page.locator(".bp-dag-sheet-handle")).to_have_attribute("aria-expanded", "true")
    assert "node=chapter%2Fmiddle" in page.url

    handle = page.locator(".bp-dag-sheet-handle")
    handle.tap()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    assert not page.locator(".bp-dag-viewer").evaluate(
        "element => element.classList.contains('bp-dag-sheet-open')"
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

    page.wait_for_function("before => window.scrollY > before", before)
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
