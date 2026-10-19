"""Reader journeys across a real, independently published Markdown blueprint.

These tests exercise the shared explorer, not a hand-authored graph payload or
an Atlas-specific adapter. The browser/runtime fixture is shared with the
existing WebKit suite, including its optional-extra and required-CI policy.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

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
MATH_TITLE = "Theorem (Crystallographic Restriction)"
MATH_STATEMENT = (
    r"Let $L \neq \{0\}.$ Then $\overline{G} = C_n$ or $D_n$, "
    r"where $n = 1, 2, 3, 4,$ or $6.$"
)
MATH_TEX = (r"L \neq \{0\}.", r"\overline{G} = C_n", "D_n", "n = 1, 2, 3, 4,", "6.")
MATHJAX_URL = "https://cdn.jsdelivr.net/npm/mathjax-full@3.2.2/es5/tex-svg.js"
MATHJAX_BYTES = 2_108_580
MATHJAX_SHA256 = "d4295dc33744836935c1399feece5159577b34c5c8ffb9f1c6324cd82e03a882"


@dataclass(frozen=True)
class _ReviewSite:
    url: str

    @property
    def overview(self) -> str:
        return f"{self.url}/dependencies.html"


@dataclass(frozen=True)
class _MathReviewSite(_ReviewSite):
    title: str
    tex: tuple[str, ...]

    @property
    def mathjax(self) -> str:
        return f"{self.url}/mathjax/tex-svg.js"


def _write_review_site(
    directory: Path,
    *,
    target_statement: str = TARGET_STATEMENT,
    target_title: str = TARGET_TITLE,
    statements: Mapping[str, str] | None = None,
    scope_statements: Mapping[str, str] | None = None,
) -> Path:
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
        "Geometry/Distance/Finite/page-1": ("Pagination wrapper", "Geometry"),
        "Applications": ("Applications", "Applications"),
        "Applications/Optimization": ("Optimization", "Applications"),
    }
    for node_id, (title, area) in scopes.items():
        path = roadmap / ("README.md" if node_id == "roadmap" else f"{node_id}/README.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = f"---\narea: {area}\n---\n\n" if area else ""
        if node_id == "Geometry/Distance/Finite/page-1":
            metadata = "---\narea: Geometry\nnavigation: transparent\n---\n\n"
        description = (scope_statements or {}).get(node_id, f"Mathematics in {title.lower()}.")
        path.write_text(f"{metadata}# {title}\n\n{description}\n", encoding="utf-8")

    articles = (
        (BASE_ID, BASE_TITLE, "def", "Review.metricAxioms", "A metric separates distinct points.", ()),
        (TARGET_ID, target_title, "theorem", TARGET_LEAN, target_statement, (BASE_ID,)),
        (
            "Geometry/Distance/Finite/page-1/paginated-result", "Paginated distance theorem", "theorem",
            "Review.Geometry.paginatedDistance", "A paginated result retains its mathematical context.", (BASE_ID,),
        ),
        (
            USE_ID, USE_TITLE, "theorem", "Review.Optimization.separation",
            "A finite configuration admits a positive separation radius.", (TARGET_ID,),
        ),
    )
    articles = tuple(
        (node_id, title, declaration, lean, (statements or {}).get(node_id, statement), dependencies)
        for node_id, title, declaration, lean, statement, dependencies in articles
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
    preview = dag_viewer.render_container(
        "dependencies.json", script_href=dag_viewer.SCRIPT_PATH, style_href=dag_viewer.STYLE_PATH,
        layout="embedded", search_href="dependencies/index.json", navigation_href="dependencies/navigation.json",
    )
    site.joinpath("index.html").write_text(
        '<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">'
        '<style>body{margin:0;font-family:sans-serif}</style></head><body><article>'
        f'<h1>Mathematics book landing</h1>{preview}</article></body></html>', encoding="utf-8",
    )
    return site


@pytest.fixture(scope="module")
def continuous_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_ReviewSite]:
    directory = _write_review_site(tmp_path_factory.mktemp("continuous-explorer"))
    with browser_regressions._served(directory) as url:
        yield _ReviewSite(url)


@pytest.fixture(scope="module")
def mathjax_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One bounded, verified real runtime; CI may provide an offline mirror."""
    mirror = os.environ.get("AUTOFORM_MATHJAX_BUNDLE")
    try:
        if mirror:
            with Path(mirror).open("rb") as stream:
                content = stream.read(MATHJAX_BYTES + 1)
        else:
            with urlopen(MATHJAX_URL, timeout=20) as response:
                content = response.read(MATHJAX_BYTES + 1)
    except OSError as error:
        if browser_regressions._REQUIRED or mirror:
            pytest.fail(f"Pinned MathJax runtime unavailable: {error}")
        pytest.skip(f"Pinned MathJax unavailable; set AUTOFORM_MATHJAX_BUNDLE: {error}")
    if len(content) != MATHJAX_BYTES or hashlib.sha256(content).hexdigest() != MATHJAX_SHA256:
        pytest.fail("Pinned MathJax runtime has an unexpected size or SHA256")
    bundle = tmp_path_factory.mktemp("mathjax-runtime") / "tex-svg.js"
    bundle.write_bytes(content)
    return bundle


def _math_site_directory(
    directory: Path, bundle: Path, *, title: str, statement: str,
) -> Path:
    site = _write_review_site(
        directory,
        target_title=title,
        target_statement=statement,
        statements={USE_ID: "A separated lattice has point group $G$."},
        scope_statements={"Geometry": "A lattice $L$ organizes discrete symmetries."},
    )
    asset = site / "mathjax/tex-svg.js"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(bundle.read_bytes())
    return site


@pytest.fixture(scope="module")
def math_site(tmp_path_factory: pytest.TempPathFactory, mathjax_bundle: Path) -> Iterator[_MathReviewSite]:
    directory = _math_site_directory(
        tmp_path_factory.mktemp("mathematical-explorer"), mathjax_bundle,
        title=MATH_TITLE, statement=MATH_STATEMENT,
    )
    with browser_regressions._served(directory) as url:
        yield _MathReviewSite(url, MATH_TITLE, MATH_TEX)


@pytest.fixture(scope="module")
def literal_math_site(
    tmp_path_factory: pytest.TempPathFactory, mathjax_bundle: Path
) -> Iterator[_MathReviewSite]:
    title = "Literal prices and a mathematical assumption"
    directory = _math_site_directory(
        tmp_path_factory.mktemp("literal-mathematical-explorer"), mathjax_bundle,
        title=title, statement=r"Literal prices are \$5 and \$6; suppose $n \neq 0$.",
    )
    with browser_regressions._served(directory) as url:
        yield _MathReviewSite(url, title, (r"n \neq 0",))


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


def _search_target(page: object, title: str = TARGET_TITLE) -> None:
    search = page.locator(".bp-dag-search")
    search.fill(TARGET_LEAN)
    result = page.locator(".bp-dag-result", has_text=title)
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


def test_a_landing_preview_expands_to_the_full_explorer_and_back_restores_the_book(
    continuous_page: object, continuous_site: _ReviewSite
) -> None:
    page = continuous_page
    page.goto(f"{continuous_site.url}/index.html")
    _node(page, "scope:Geometry").wait_for()
    search = page.locator(".bp-dag-search").element_handle()
    documents = _record_documents(page)
    _search_target(page)
    viewer = page.locator(".bp-dag-viewer")
    playwright.expect(viewer).to_have_attribute("data-layout", "app")
    box = viewer.bounding_box()
    assert box is not None and abs(box["width"] - 1440) < 2 and abs(box["height"] - 1000) < 2
    assert search.evaluate("node => node === document.querySelector('.bp-dag-search')")
    page.evaluate("history.back()")
    playwright.expect(viewer).to_have_attribute("data-layout", "embedded")
    playwright.expect(page.get_by_role("heading", name="Mathematics book landing", exact=True)).to_be_visible()
    assert search.evaluate("node => node === document.querySelector('.bp-dag-search')")
    assert documents == []


def test_a_paginated_result_keeps_the_visible_mathematical_ancestry(
    continuous_page: object, continuous_site: _ReviewSite
) -> None:
    page = continuous_page
    _open(page, continuous_site)
    page.locator(".bp-dag-search").fill("Review.Geometry.paginatedDistance")
    page.locator(".bp-dag-result", has_text="Paginated distance theorem").click()
    _expect_focus(page, "Geometry/Distance/Finite/page-1/paginated-result")
    trail = page.locator(".bp-dag-breadcrumb")
    playwright.expect(trail).to_contain_text("Geometry")
    playwright.expect(trail).to_contain_text("Finite configurations")
    playwright.expect(trail).not_to_contain_text("Pagination wrapper")


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


def _load_math_engine(page: object, site: _MathReviewSite, *, hold_startup: bool = False) -> None:
    page.evaluate(
        r"""hold => {
          let release;
          const gate = new Promise(resolve => { release = resolve; });
          window.__releaseMathStartup = release;
          window.MathJax = {
            tex: {inlineMath: [['$', '$'], ['\\(', '\\)']], displayMath: [['$$', '$$'], ['\\[', '\\]']]},
            options: {enableMenu: false},
            svg: {fontCache: 'local'},
            startup: {typeset: false}
          };
          if (hold) MathJax.startup.ready = function () {
            MathJax.startup.defaultReady();
            MathJax.startup.promise = MathJax.startup.promise.then(() => gate);
          };
        }""",
        arg=hold_startup,
    )
    page.add_script_tag(url=site.mathjax)
    page.wait_for_function("() => window.MathJax && typeof MathJax.typesetPromise === 'function'")
    # Instrument public APIs, retaining their genuine MathJax implementations.
    # The reject switch is fault injection, never substitute rendered output.
    page.evaluate(
        """() => {
          const original = MathJax.typesetPromise.bind(MathJax);
          const clear = MathJax.typesetClear.bind(MathJax);
          const trace = window.__mathTrace = {calls: [], clears: 0, active: 0, maximum: 0, reject: false};
          MathJax.typesetPromise = function (elements) {
            const roots = elements ? Array.from(elements) : [];
            trace.calls.push({count: roots.length, scoped: roots.length > 0 && roots.every(node =>
              node instanceof Element && node !== document.body && node !== document.documentElement &&
              !!node.closest('.bp-dag-viewer'))});
            trace.active++; trace.maximum = Math.max(trace.maximum, trace.active);
            let result;
            try { result = trace.reject ? Promise.reject(new Error('Intentional renderer failure')) : original(elements); }
            catch (error) { result = Promise.reject(error); }
            return Promise.resolve(result).finally(() => { trace.active--; });
          };
          MathJax.typesetClear = function (elements) { trace.clears++; return clear(elements); };
        }"""
    )
    if not hold_startup:
        page.evaluate("() => MathJax.startup.promise")


def _math_stats(page: object) -> dict[str, object]:
    return page.evaluate(
        """() => {
          const items = Array.from(MathJax.startup.document.math);
          return {items: items.length, detached: items.filter(item =>
            item.typesetRoot && !item.typesetRoot.isConnected).length,
            calls: __mathTrace.calls.length, active: __mathTrace.active, maximum: __mathTrace.maximum,
            scoped: __mathTrace.calls.every(call => call.scoped), clears: __mathTrace.clears};
        }"""
    )


def _math_idle(page: object) -> None:
    _frames(page)
    page.wait_for_function(
        "() => __mathTrace.active === 0 && Array.from(MathJax.startup.document.math).every(item => "
        "!item.typesetRoot || item.typesetRoot.isConnected)"
    )
    _frames(page)


def _assert_rendered_math(page: object, tex: tuple[str, ...]) -> None:
    for selector in (".bp-dag-focus-statement", ".bp-dag-inspector .bp-dag-summary"):
        surface = page.locator(selector)
        output = surface.locator('mjx-container[jax="SVG"]')
        playwright.expect(output).to_have_count(len(tex), timeout=15000)
        playwright.expect(output.first).to_be_visible()
        assert output.locator("svg").count() == len(tex)
        assert surface.locator('[data-mml-node="merror"]').count() == 0
        actual = surface.evaluate(
            "node => Array.from(MathJax.startup.document.math).filter(item => "
            "item.typesetRoot && node.contains(item.typesetRoot)).map(item => item.math)"
        )
        assert actual == list(tex), "Rendering changed the canonical theorem formula"
    _math_idle(page)
    stats = _math_stats(page)
    assert stats["scoped"] and stats["maximum"] <= 1
    assert stats["detached"] == 0


def test_real_mathjax_renders_the_reported_statement_after_topic_and_focus_navigation(
    continuous_page: object, math_site: _MathReviewSite
) -> None:
    page = continuous_page
    _open(page, math_site)
    _load_math_engine(page, math_site)
    documents = _record_documents(page)
    # startup.typeset=false rules out an initial whole-document pass masking
    # missing hooks on either the topic transition or the later theorem jump.
    assert page.evaluate("MathJax.config.startup.typeset") is False
    page.locator(".bp-dag-navigation").get_by_role("link", name="Geometry", exact=True).click()
    playwright.expect(page.locator('.bp-dag-scope-description mjx-container[jax="SVG"]')).to_have_count(1)
    _search_target(page, math_site.title)
    _assert_rendered_math(page, math_site.tex)
    assert documents == []


def test_a_math_engine_loaded_after_focus_typesets_both_existing_reading_surfaces(
    continuous_page: object, math_site: _MathReviewSite
) -> None:
    page = continuous_page
    _open(page, math_site)
    _search_target(page, math_site.title)
    playwright.expect(page.locator(".bp-dag-focus-statement mjx-container")).to_have_count(0)
    playwright.expect(page.locator(".bp-dag-focus-statement")).to_contain_text(r"\(L \neq \{0\}.\)")
    _load_math_engine(page, math_site)
    _assert_rendered_math(page, math_site.tex)


def test_math_api_becoming_available_after_startup_needs_no_second_script_load(
    continuous_page: object, math_site: _MathReviewSite
) -> None:
    page = continuous_page
    _open(page, math_site)
    _load_math_engine(page, math_site)
    _math_idle(page)
    page.evaluate(
        """() => {
          const render = MathJax.typesetPromise;
          let release;
          const gate = new Promise(resolve => { release = resolve; });
          window.__releaseLateMathAPI = release;
          MathJax.typesetPromise = undefined;
          MathJax.startup.promise = gate.then(() => { MathJax.typesetPromise = render; });
        }"""
    )
    _search_target(page, math_site.title)
    playwright.expect(page.locator(".bp-dag-focus-statement mjx-container")).to_have_count(0)
    playwright.expect(page.locator(".bp-dag-focus-statement")).to_contain_text(r"\(L \neq \{0\}.\)")
    assert page.evaluate("typeof MathJax.typesetPromise") == "undefined"
    # Restore the captured real/spied API solely by resolving startup.promise.
    # No script tag, load event or explicit typesetting call wakes the viewer.
    page.evaluate("__releaseLateMathAPI()")
    _assert_rendered_math(page, math_site.tex)


def test_delayed_mathjax_startup_typesets_the_current_context_after_pivot_and_back(
    continuous_page: object, math_site: _MathReviewSite
) -> None:
    page = continuous_page
    _open(page, math_site)
    _load_math_engine(page, math_site, hold_startup=True)
    documents = _record_documents(page)
    _search_target(page, math_site.title)
    page.locator(".bp-dag-inspector").get_by_role("button", name=USE_TITLE, exact=True).click()
    _expect_focus(page, USE_ID)
    page.evaluate("history.back()")
    _expect_focus(page, TARGET_ID)
    playwright.expect(page.locator(".bp-dag-focus-statement mjx-container")).to_have_count(0)
    assert _math_stats(page)["calls"] == 0, "Typesetting ran before the startup promise resolved"
    page.evaluate("__releaseMathStartup()")
    _assert_rendered_math(page, math_site.tex)
    assert documents == []


def test_literal_markdown_dollars_stay_prose_beside_genuine_rendered_math(
    continuous_page: object, literal_math_site: _MathReviewSite
) -> None:
    page = continuous_page
    _open(page, literal_math_site)
    _load_math_engine(page, literal_math_site)
    _search_target(page, literal_math_site.title)
    _assert_rendered_math(page, literal_math_site.tex)
    for selector in (".bp-dag-focus-statement", ".bp-dag-inspector .bp-dag-summary"):
        playwright.expect(page.locator(selector)).to_contain_text("Literal prices are $5 and $6; suppose")


@pytest.mark.parametrize("engine", ("missing", "rejecting"))
def test_optional_math_renderer_failure_preserves_text_and_cross_topic_navigation(
    continuous_page: object, math_site: _MathReviewSite, engine: str
) -> None:
    page = continuous_page
    _open(page, math_site)
    if engine == "rejecting":
        _load_math_engine(page, math_site)
        page.evaluate("__mathTrace.reject = true")
    documents = _record_documents(page)
    search = page.locator(".bp-dag-search").element_handle()
    _search_target(page, math_site.title)
    playwright.expect(page.locator(".bp-dag-focus-statement mjx-container")).to_have_count(0)
    playwright.expect(page.locator(".bp-dag-focus-statement")).to_contain_text(r"\(\overline{G} = C_n\)")
    if engine == "rejecting":
        page.wait_for_function("() => __mathTrace.calls.length > 0 && __mathTrace.active === 0")
    page.locator(".bp-dag-inspector").get_by_role("button", name=USE_TITLE, exact=True).click()
    _expect_focus(page, USE_ID)
    assert search.evaluate("node => node === document.querySelector('.bp-dag-search')")
    assert documents == []


def test_repeated_math_pivots_clear_detached_items_and_resize_preserves_cached_header(
    continuous_page: object, math_site: _MathReviewSite
) -> None:
    page = continuous_page
    _open(page, math_site)
    _load_math_engine(page, math_site)
    _search_target(page, math_site.title)
    _assert_rendered_math(page, math_site.tex)
    baseline = _math_stats(page)
    for _ in range(4):
        previous_calls = _math_stats(page)["calls"]
        page.locator(".bp-dag-inspector").get_by_role("button", name=USE_TITLE, exact=True).click()
        _expect_focus(page, USE_ID)
        _assert_rendered_math(page, ("G",))
        page.evaluate("history.back()")
        _expect_focus(page, TARGET_ID)
        _assert_rendered_math(page, math_site.tex)
        stats = _math_stats(page)
        assert stats["items"] == baseline["items"]
        assert stats["calls"] - previous_calls <= 4, "One pivot/back caused unbounded retypesetting"
    header = page.locator(".bp-dag-focus-statement").element_handle()
    stable = _math_stats(page)
    page.set_viewport_size({"width": 1500, "height": 1000})
    _math_idle(page)
    assert header.evaluate("node => node === document.querySelector('.bp-dag-focus-statement')")
    page.get_by_role("button", name="Zoom in", exact=True).click()
    _math_idle(page)
    final = _math_stats(page)
    assert final["calls"] == stable["calls"], "Resize or camera movement retypeset unchanged prose"
    assert final["clears"] > 0
