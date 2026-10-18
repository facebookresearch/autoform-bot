"""Reader journeys across a real, independently published Markdown blueprint.

These tests exercise the shared explorer, not a hand-authored graph payload or
an Atlas-specific adapter. The browser/runtime fixture is shared with the
existing WebKit suite, including its optional-extra and required-CI policy.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pytest

from autoform_cli import dag_viewer, mermaid
from autoform_cli.graph import load_graph
from autoform_cli.graph_pages import write_graph_pages
from autoform_cli.status import derive
from tests import test_dag_viewer_browser as browser_regressions

playwright = browser_regressions.playwright
webkit_browser = browser_regressions.webkit_browser

BASE_ID = "Foundations/Metric/axioms"
BASE_TITLE = "Metric axioms"
TARGET_ID = "Geometry/Distance/Finite/distance-bound"
TARGET_TITLE = "Finite distance theorem"
TARGET_LEAN = "Review.Geometry.distanceBound"
TARGET_STATEMENT = "Every finite set of distinct points has a strictly positive minimum pairwise distance."
USE_ID = "Applications/Optimization/separation"
USE_TITLE = "Separated configuration theorem"


@dataclass(frozen=True)
class _ReviewSite:
    url: str

    @property
    def overview(self) -> str:
        return f"{self.url}/dependencies.html"


def _write_review_site(directory: Path) -> Path:
    """Publish production graph pages, wrapping their hosts in minimal HTML."""
    blueprint = directory / "blueprint"
    roadmap = blueprint / "roadmap"
    scopes = {
        "roadmap": ("Mathematics review", None),
        "Foundations": ("Foundations", "Foundations"),
        "Foundations/Metric": ("Metric foundations", "Foundations"),
        "Geometry": ("Geometry", "Geometry"),
        "Geometry/Distance": ("Distance geometry", "Geometry"),
        "Geometry/Distance/Finite": ("Finite configurations", "Geometry"),
        "Applications": ("Applications", "Applications"),
        "Applications/Optimization": ("Optimization", "Applications"),
    }
    for node_id, (title, area) in scopes.items():
        path = roadmap / ("README.md" if node_id == "roadmap" else f"{node_id}/README.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = f"---\narea: {area}\n---\n\n" if area else ""
        path.write_text(f"{metadata}# {title}\n\nMathematics in {title.lower()}.\n", encoding="utf-8")

    articles = (
        (BASE_ID, BASE_TITLE, "def", "Review.metricAxioms", "A metric separates distinct points.", ()),
        (TARGET_ID, TARGET_TITLE, "theorem", TARGET_LEAN, TARGET_STATEMENT, (BASE_ID,)),
        (
            USE_ID, USE_TITLE, "theorem", "Review.Optimization.separation",
            "A finite configuration admits a positive separation radius.", (TARGET_ID,),
        ),
    )
    for node_id, title, declaration, lean, statement, dependencies in articles:
        path = roadmap / f"{node_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "---", f"declaration: {declaration}", f"lean: {lean}", "---", "",
            f"# {title}", "", "## Statement", "", statement, "",
        ]
        if dependencies:
            lines.extend(["## Depends on", ""])
            for dependency in dependencies:
                target = roadmap / f"{dependency}.md"
                lines.append(f"- [{dependency}]({mermaid.relative_link(target, path, '.md')})")
            lines.append("")
        if node_id == TARGET_ID:
            lines.extend([
                "## Proof depends on", "",
                f"- [{BASE_TITLE}]({mermaid.relative_link(roadmap / f'{BASE_ID}.md', path, '.md')})", "",
            ])
        path.write_text("\n".join(lines), encoding="utf-8")

    graph = load_graph(blueprint)
    site = directory / "site"

    def article_links(page: Path, ids: Iterable[str]) -> dict[str, str]:
        book = mermaid.relative_link(site / "book.html", page, ".html")
        return {node_id: f"{book}#{quote(node_id, safe='')}" for node_id in ids}

    pages = write_graph_pages(graph, derive(graph), site, node_links=article_links)
    for page in pages:
        generated = page.read_text(encoding="utf-8")
        start = generated.index('<link rel="stylesheet"')
        end = generated.index("</script>", start) + len("</script>")
        fragment = generated[start:end]
        page.with_suffix(".html").write_text(
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Continuous mathematics explorer review</title>'
            '<style>body{margin:0;font-family:sans-serif}</style></head><body>'
            f"<article><h1>Mathematics review</h1>{fragment}</article></body></html>",
            encoding="utf-8",
        )
    for relative, content in (
        (dag_viewer.SCRIPT_PATH, dag_viewer.viewer_script()),
        (dag_viewer.STYLE_PATH, dag_viewer.viewer_styles()),
    ):
        asset = site / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(content, encoding="utf-8")
    site.joinpath("book.html").write_text(
        "<!doctype html><title>Fixture mathematics</title>" + "".join(
            f'<section id="{html.escape(node_id, quote=True)}"><h1>{html.escape(title)}</h1>'
            f"<p>{html.escape(statement)}</p></section>"
            for node_id, title, _declaration, _lean, statement, _dependencies in articles
        ),
        encoding="utf-8",
    )
    return site


@pytest.fixture(scope="module")
def continuous_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_ReviewSite]:
    directory = _write_review_site(tmp_path_factory.mktemp("continuous-explorer"))
    with browser_regressions._served(directory) as url:
        yield _ReviewSite(url)


@pytest.fixture()
def continuous_page(webkit_browser: object) -> Iterator[object]:
    with browser_regressions._page(
        webkit_browser, viewport={"width": 1440, "height": 1000}, reduced_motion="reduce"
    ) as page:
        yield page


def _frames(page: object) -> None:
    page.evaluate("() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")


def _node(page: object, node_id: str) -> object:
    return page.locator(f'[data-autoform-node-id="{node_id}"]')


def _open(page: object, site: _ReviewSite) -> None:
    page.goto(site.overview)
    _node(page, "scope:Geometry").wait_for()
    _frames(page)


def _record_documents(page: object) -> list[str]:
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url) if request.is_navigation_request() else None)
    return requests


def _search_target(page: object) -> None:
    search = page.locator(".bp-dag-search")
    search.fill(TARGET_LEAN)
    result = page.locator(".bp-dag-result", has_text=TARGET_TITLE)
    playwright.expect(result).to_be_visible()
    result.click()
    _expect_focus(page, TARGET_ID)


def _expect_focus(page: object, node_id: str) -> None:
    page.wait_for_function(
        "id => { const p = new URLSearchParams(location.hash.slice(1)); "
        "return p.get('node') === id && p.get('focus') === '1'; }",
        arg=node_id,
    )
    playwright.expect(_node(page, node_id)).to_have_attribute("data-selected", "true")
    playwright.expect(_node(page, node_id)).to_be_visible()
    _frames(page)


def test_overview_connections_are_drawn_before_selecting_a_topic(
    continuous_page: object, continuous_site: _ReviewSite
) -> None:
    page = continuous_page
    _open(page, continuous_site)
    connections = page.locator(".bp-dag-toolbar").get_by_role("button", name=re.compile("connections", re.I))
    playwright.expect(connections).to_have_attribute("aria-pressed", "true")
    assert page.locator('.bp-dag-node[data-selected="true"]').count() == 0
    canvas = page.locator(".bp-dag-canvas")
    connected = canvas.evaluate("canvas => canvas.toDataURL()")
    connections.click()
    playwright.expect(connections).to_have_attribute("aria-pressed", "false")
    _frames(page)
    # This checks rendered output, not a flag that could leave the map edgeless.
    assert canvas.evaluate("canvas => canvas.toDataURL()") != connected


def test_remote_search_opens_the_theorem_and_its_neighborhood_in_one_shell(
    continuous_page: object, continuous_site: _ReviewSite
) -> None:
    page = continuous_page
    _open(page, continuous_site)
    search = page.locator(".bp-dag-search").element_handle()
    documents = _record_documents(page)
    _search_target(page)
    assert urlparse(page.url).path == "/dependencies/scopes/Geometry/Distance/Finite.html"
    assert search.evaluate("node => node === document.querySelector('.bp-dag-search')")
    for node_id in (BASE_ID, TARGET_ID, USE_ID):
        playwright.expect(_node(page, node_id)).to_be_visible()
        box = _node(page, node_id).bounding_box()
        stage = page.locator(".bp-dag-stage").bounding_box()
        inspector = page.locator(".bp-dag-inspector").bounding_box()
        assert box is not None and stage is not None and inspector is not None
        assert box["x"] >= stage["x"]
        assert box["x"] + box["width"] <= inspector["x"] - 8, "Reading panel covers a connected result"
    playwright.expect(page.locator(".bp-dag-inspector").get_by_text(TARGET_STATEMENT, exact=True)).to_be_visible()
    assert documents == []


def test_a_dependency_relation_pivots_directly_to_another_branch(
    continuous_page: object, continuous_site: _ReviewSite
) -> None:
    page = continuous_page
    _open(page, continuous_site)
    _search_target(page)
    documents = _record_documents(page)
    page.locator(".bp-dag-inspector").get_by_role("button", name=USE_TITLE, exact=True).click()
    _expect_focus(page, USE_ID)
    assert urlparse(page.url).path == "/dependencies/scopes/Applications/Optimization.html"
    playwright.expect(_node(page, TARGET_ID)).to_be_visible()
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text(USE_TITLE)
    assert documents == []


def test_outline_and_scope_links_change_context_without_rebuilding_search(
    continuous_page: object, continuous_site: _ReviewSite
) -> None:
    page = continuous_page
    _open(page, continuous_site)
    search = page.locator(".bp-dag-search").element_handle()
    documents = _record_documents(page)
    page.locator(".bp-dag-navigation").get_by_role("link", name="Geometry", exact=True).click()
    playwright.expect(page.locator(".bp-dag-scope-title")).to_have_text("Geometry")
    nested = _node(page, "scope:Geometry/Distance").locator("a.bp-dag-node-link")
    playwright.expect(nested).to_be_visible()
    nested.click()
    playwright.expect(page.locator(".bp-dag-scope-title")).to_have_text("Distance geometry")
    assert urlparse(page.url).path == "/dependencies/scopes/Geometry/Distance.html"
    assert search.evaluate("node => node === document.querySelector('.bp-dag-search')")
    # No search/index preload precedes either transition: changing the payload
    # must not make the old overview array masquerade as a loaded global index.
    _search_target(page)
    playwright.expect(_node(page, BASE_ID)).to_be_visible()
    playwright.expect(_node(page, USE_ID)).to_be_visible()
    assert search.evaluate("node => node === document.querySelector('.bp-dag-search')")
    assert documents == []


def test_back_restores_the_theorem_and_the_readers_camera(
    continuous_page: object, continuous_site: _ReviewSite
) -> None:
    page = continuous_page
    _open(page, continuous_site)
    _search_target(page)
    page.get_by_role("button", name="Zoom in", exact=True).click()
    _frames(page)
    before = _node(page, TARGET_ID).bounding_box()
    assert before is not None
    search = page.locator(".bp-dag-search").element_handle()
    documents = _record_documents(page)
    page.locator(".bp-dag-inspector").get_by_role("button", name=USE_TITLE, exact=True).click()
    _expect_focus(page, USE_ID)
    page.evaluate("history.back()")
    _expect_focus(page, TARGET_ID)
    after = _node(page, TARGET_ID).bounding_box()
    assert after is not None
    for key in ("x", "y", "width", "height"):
        assert abs(after[key] - before[key]) < 3, f"Back changed the theorem's {key}"
    assert search.evaluate("node => node === document.querySelector('.bp-dag-search')")
    assert documents == []


def test_a_copied_focused_permalink_restores_the_cross_branch_neighborhood(
    continuous_page: object, continuous_site: _ReviewSite
) -> None:
    page = continuous_page
    _open(page, continuous_site)
    _search_target(page)
    permalink = page.url
    assert parse_qs(urlparse(permalink).fragment)["focus"] == ["1"]
    page.goto("about:blank")
    page.goto(permalink)
    _expect_focus(page, TARGET_ID)
    for node_id in (BASE_ID, USE_ID):
        playwright.expect(_node(page, node_id)).to_be_visible()
    playwright.expect(page.locator(".bp-dag-inspector").get_by_text(TARGET_STATEMENT, exact=True)).to_be_visible()


def test_a_failed_context_fetch_keeps_the_current_map_and_search_usable(
    continuous_page: object, continuous_site: _ReviewSite
) -> None:
    page = continuous_page
    _open(page, continuous_site)
    search = page.locator(".bp-dag-search").element_handle()
    original_url = page.url
    documents = _record_documents(page)
    pattern = "**/dependencies/chapters/Geometry.json"
    page.route(pattern, lambda route: route.fulfill(status=503, content_type="application/json", body="{}"))
    with page.expect_response(pattern):
        page.locator(".bp-dag-navigation").get_by_role("link", name="Geometry", exact=True).click()
    _frames(page)
    assert page.url == original_url
    assert search.evaluate("node => node === document.querySelector('.bp-dag-search')")
    for node_id in ("scope:Geometry", "scope:Foundations", "scope:Applications"):
        playwright.expect(_node(page, node_id)).to_be_visible()
    page.unroute(pattern)
    _search_target(page)
    assert documents == []


@pytest.mark.parametrize("next_intent", ("another_context", "back"))
def test_a_delayed_context_does_not_override_a_newer_navigation_intent(
    continuous_page: object, continuous_site: _ReviewSite, next_intent: str
) -> None:
    page = continuous_page
    _open(page, continuous_site)
    search = page.locator(".bp-dag-search").element_handle()
    documents = _record_documents(page)
    outline = page.locator(".bp-dag-navigation")
    if next_intent == "back":
        outline.get_by_role("link", name="Applications", exact=True).click()
        playwright.expect(page.locator(".bp-dag-scope-title")).to_have_text("Applications")

    held: list[tuple[object, object]] = []
    pattern = "**/dependencies/chapters/Geometry.json"

    def delay_context(route: object) -> None:
        # Fetch the real generated payload now but withhold delivery. Releasing
        # it later does not depend on another server round trip finishing.
        held.append((route, route.fetch()))

    page.route(pattern, delay_context)
    with page.expect_request(pattern):
        outline.get_by_role("link", name="Geometry", exact=True).click()
    # Playwright may deliver the route after its request event. Let its event
    # loop run until the held request is available, without releasing it yet.
    for _ in range(100):
        if held:
            break
        page.evaluate("0")
    assert held, "the delayed context request never arrived"

    if next_intent == "another_context":
        outline.get_by_role("link", name="Foundations", exact=True).click()
        expected_title = "Foundations"
    else:
        page.evaluate("history.back()")
        expected_title = "Mathematics review"
    playwright.expect(page.locator(".bp-dag-scope-title")).to_have_text(expected_title)
    expected_url = page.url
    route, response = held.pop(0)
    try:
        route.fulfill(response=response)
    except playwright.Error as error:
        # Correct cancellation may make the older intercepted request vanish
        # before it can be released; that is also a valid latest-intent result.
        if not re.search("abort|cancel|intercept|already handled", str(error), re.I):
            raise
    _frames(page)
    assert page.url == expected_url
    playwright.expect(page.locator(".bp-dag-scope-title")).to_have_text(expected_title)
    assert search.evaluate("node => node === document.querySelector('.bp-dag-search')")
    assert documents == []
