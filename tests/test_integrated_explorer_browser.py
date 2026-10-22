"""Complete reading-source swaps through the router and the real safe MathJax.

The fixture is an actual rendered/MkDocs consumer. Suppressing only the graph
client in the source-service cases lets each readiness order be controlled;
the production router, complete published HTML and generated math config run
unchanged. CDN assets are mirrored from the pinned release, never a stock
typesetPromise spy or a different TeX configuration.
"""

from __future__ import annotations

import functools
import hashlib
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

import pytest

from autoform_cli.mathjax import MATHJAX_BASE, MATHJAX_BUNDLE
from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project

_REQUIRED = os.environ.get("AUTOFORM_REQUIRE_BROWSER_TESTS") == "1"
if _REQUIRED:
    import playwright.sync_api as playwright
else:
    playwright = pytest.importorskip("playwright.sync_api", reason="the optional browser extra is required")

_BUNDLE_SHA256 = "300480069078b5892d2363a2b65e2dfbbf30fe5c80f83edbfecf4610fd093862"
_ALGEBRA = "roadmap/algebra/index.html"
_TOPOLOGY = "roadmap/topology/index.html"
_EFFECTS = "roadmap/effects/index.html"
_ALGEBRA_TITLE = "Complete unit-distance theorem"
_TOPOLOGY_TITLE = "Complete continuity theorem"
_ALGEBRA_TEX = (
    r"\RR^2", r"\|P-Q\|=1", r"\sum_{i=1}^{n}m_i^2\geq n", r"\frac{1}{2}\binom{n}{2}",
    r"\overline{AB}=\sqrt{2}",
)
_TOPOLOGY_TEX = (r"\stolen+\RR", r"\lim_{n\to\infty}x_n=x")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _served(directory: Path) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class _PinnedRuntime:
    """A small cached local mirror of exactly the generated config's es5 URLs.

    CI can provision the whole release in AUTOFORM_MATHJAX_ES5. Otherwise only
    requested public assets are fetched, with a per-file time/size bound and a
    verified engine bundle. No article or repository data is sent to the CDN.
    """

    def __init__(self) -> None:
        path = os.environ.get("AUTOFORM_MATHJAX_ES5")
        self.root = Path(path) if path else None
        self.files: dict[str, bytes] = {}

    def payload(self, url: str) -> bytes:
        assert url.startswith(MATHJAX_BASE + "/"), url
        relative = unquote(urlsplit(url).path[len(urlsplit(MATHJAX_BASE).path) + 1:])
        assert relative and all(part not in {"", ".", ".."} for part in relative.split("/")), relative
        assert relative.endswith((".js", ".woff", ".woff2")), relative
        if relative not in self.files:
            path = self.root / relative if self.root else None
            if path is not None:
                data = path.read_bytes()
            else:
                with urlopen(MATHJAX_BASE + "/" + relative, timeout=30) as response:
                    data = response.read(3_000_001)
            assert len(data) <= 3_000_000, f"MathJax asset exceeded its size bound: {relative}"
            if relative == "tex-mml-chtml.js":
                assert hashlib.sha256(data).hexdigest() == _BUNDLE_SHA256
            self.files[relative] = data
        return self.files[relative]

    def serve(self, route: object) -> None:
        url = route.request.url
        content_type = "application/javascript" if urlsplit(url).path.endswith(".js") else "font/woff"
        route.fulfill(body=self.payload(url), content_type=content_type)


@pytest.fixture(scope="module")
def pinned_runtime() -> _PinnedRuntime:
    return _PinnedRuntime()


@pytest.fixture(scope="module")
def webkit_browser() -> Iterator[object]:
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.webkit.launch()
        except playwright.Error as error:
            if _REQUIRED or "Executable doesn't exist" not in str(error):
                raise
            pytest.skip("WebKit is not installed")
        yield browser
        browser.close()


@pytest.fixture()
def reader_page(webkit_browser: object) -> Iterator[object]:
    context = webkit_browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
    page = context.new_page()
    page.set_default_timeout(30_000)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        yield page
    finally:
        context.close()
    assert errors == []


@pytest.fixture(scope="module")
def reader_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("integrated-reader") / "project"
    scaffold_project(project, title="Complete mathematical reader")
    blueprint = project / "blueprint"
    (blueprint / "tex-macros.json").write_text('{"RR":"\\\\mathbb{R}"}', encoding="utf-8")
    roadmap = blueprint / "roadmap"
    (roadmap / "README.md").write_text(
        "# Reading fixture\n\n- [Algebra](algebra/README.md)\n- [Topology](topology/README.md)\n"
        "- [Forbidden effects](effects/README.md)\n",
        encoding="utf-8",
    )
    for topic, title in (("algebra", _ALGEBRA_TITLE), ("topology", _TOPOLOGY_TITLE),
                         ("effects", "Forbidden cross-formula effects")):
        folder = roadmap / topic
        folder.mkdir()
        (folder / "README.md").write_text(
            f"# {title}\n\n- [Base](base.md)\n- [Complete result](result.md)\n",
            encoding="utf-8",
        )
        (folder / "base.md").write_text(
            "---\ndeclaration: def\n---\n\n# Base\n\nThe base value is zero.\n\n"
            "## Depends on\n\nNo prerequisites.\n",
            encoding="utf-8",
        )
    # This is complete authored reading prose, deliberately beyond a 420-char
    # graph preview. The final displayed formula must not become a summary.
    lead = "The full reading page retains every hypothesis, formula and proof paragraph. " * 9
    algebra_math = "\n\n".join(f"Formula {n + 1}: ${tex}$." for n, tex in enumerate(_ALGEBRA_TEX[:-1]))
    (roadmap / "algebra/result.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# " + _ALGEBRA_TITLE + "\n\n" + lead + "\n\n"
        + algebra_math + "\n\n$$\n" + _ALGEBRA_TEX[-1] + "\n$$\n\n"
        "Literal prices are \\$5 and \\$10; code `$not_math$` stays code.\n\n"
        "[Read the continuity theorem](../topology/result.md).\n\n"
        "## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    (roadmap / "topology/result.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# " + _TOPOLOGY_TITLE + "\n\n"
        + "\n\n".join(f"${tex}$." for tex in _TOPOLOGY_TEX) + "\n\n"
        "[Read the unit-distance theorem](../algebra/result.md).\n\n"
        "## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    (roadmap / "effects/result.md").write_text(
        "---\ndeclaration: lemma\n---\n\n# Forbidden cross-formula effects\n\n"
        r"$\DeclareMathOperator{\stolen}{EVIL}$." + "\n\n" + r"$\stolen+\RR$." + "\n\n"
        "## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    render_site(blueprint, project / "site-src")
    configuration = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    # Exercise the generated script's asynchronous loader, not the old redundant
    # parser-blocking compatibility tag. The generated math config is unedited.
    configuration["extra_javascript"] = [
        entry for entry in configuration["extra_javascript"]
        if getattr(entry, "path", str(entry)) != MATHJAX_BUNDLE
    ]
    build(configuration)
    with _served(project / "site") as url:
        yield url


def _routes(page: object, site: str, runtime: _PinnedRuntime, *, graph_client: bool = False) -> None:
    def serve(route: object) -> None:
        url = route.request.url
        if url.startswith(MATHJAX_BASE + "/"):
            runtime.serve(route)
        elif url.startswith(site + "/"):
            if not graph_client and urlsplit(url).path.endswith("/javascripts/blueprint-dag.js"):
                route.abort()
            else:
                route.continue_()
        else:
            route.abort()
    page.route("**/*", serve)


def _bridge(page: object) -> None:
    """Mount actual router clones into one reader; do not reconstruct content."""
    navigation = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-navigation.js"
    page.add_script_tag(content=navigation.read_text(encoding="utf-8"))
    page.evaluate("""() => {
      const host = document.querySelector('.bp-dag-viewer'), source = document.querySelector('.bp-reading-source');
      const reader = document.createElement('section'); reader.id = 'test-complete-reader'; reader.className = 'bp-reading-source md-typeset';
      document.body.appendChild(reader);
      window.__readerDocument = document; window.__readerErrors = []; window.__readerCommits = 0;
      window.__readerNavigation = AutoformNavigation.create({host, onError: error => __readerErrors.push(String(error)),
        onCommit(bundle) {
          reader.replaceChildren(...Array.from(bundle.readingSource.childNodes));
          reader.setAttribute('data-page-title', bundle.title);
          source.remove();
          window.__readerBundle = bundle; window.__readerCommits++;
          window.__readerMath = AutoformMath.refresh(reader);
        }});
      __readerNavigation.bindReader(reader);
      window.__readerStart = __readerNavigation.start();
    }""")


def _wait_reader(page: object, title: str, tex: tuple[str, ...]) -> object:
    reader = page.locator("#test-complete-reader")
    playwright.expect(reader.locator("h1")).to_have_text(title)
    assert page.evaluate("() => window.__readerMath") is True
    formulas = reader.locator(".arithmatex")
    playwright.expect(formulas).to_have_count(len(tex))
    playwright.expect(reader.locator("mjx-container")).to_have_count(len(tex))
    assert reader.locator("mjx-merror, merror").count() == 0
    retained = formulas.evaluate_all("nodes => nodes.map(node => node.getAttribute('data-autoform-math-source').trim())")
    assert retained == [r"\(" + source + r"\)" for source in tex[:-1]] + [
        ("\\[\n" + tex[-1] + "\n\\]") if title == _ALGEBRA_TITLE else (r"\(" + tex[-1] + r"\)")
    ]
    assert page.locator("[data-autoform-math-stage]").count() == 0
    assert page.evaluate("() => document === __readerDocument && __readerErrors.length === 0") is True
    return reader


def _math_source(page: object, container: object, *, mathml: bool = False) -> str:
    container.click(button="right")
    page.get_by_role("menuitem", name="Show Math As", exact=True).hover()
    page.get_by_role("menuitem", name="MathML Code" if mathml else "Original Form", exact=True).click()
    source = page.locator(".CtxtMenu_Info pre")
    source.wait_for(state="visible")
    text = source.inner_text()
    page.locator(".CtxtMenu_InfoClose").click()
    return text


def _navigate(page: object, url: str) -> None:
    assert page.evaluate("url => __readerNavigation.navigate(url).then(Boolean)", url) is True


def test_complete_math_survives_repeated_soft_navigation_and_back(
    reader_page: object, reader_site: str, pinned_runtime: _PinnedRuntime,
) -> None:
    page = reader_page
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _routes(page, reader_site, pinned_runtime)
    page.goto(f"{reader_site}/{_ALGEBRA}")
    _bridge(page)
    assert page.evaluate("() => __readerStart.then(Boolean)") is True
    _wait_reader(page, _ALGEBRA_TITLE, _ALGEBRA_TEX)
    for _ in range(3):
        _navigate(page, f"{reader_site}/{_TOPOLOGY}")
        _wait_reader(page, _TOPOLOGY_TITLE, _TOPOLOGY_TEX)
        _navigate(page, f"{reader_site}/{_ALGEBRA}")
        _wait_reader(page, _ALGEBRA_TITLE, _ALGEBRA_TEX)
    before = page.evaluate("() => __readerCommits")
    page.evaluate("() => history.back()")
    page.wait_for_function("before => __readerCommits > before", arg=before)
    _wait_reader(page, _TOPOLOGY_TITLE, _TOPOLOGY_TEX)
    assert documents == [f"{reader_site}/{_ALGEBRA}"]


def test_delayed_engine_renders_latest_complete_reader_after_many_swaps(
    reader_page: object, reader_site: str, pinned_runtime: _PinnedRuntime,
) -> None:
    page = reader_page
    _routes(page, reader_site, pinned_runtime)
    held: list[object] = []
    page.route(MATHJAX_BUNDLE, lambda route: held.append(route))
    page.goto(f"{reader_site}/{_ALGEBRA}", wait_until="domcontentloaded")
    page.wait_for_function("() => typeof AutoformMath !== 'undefined'")
    _bridge(page)
    assert page.evaluate("() => __readerStart.then(Boolean)") is True
    for _ in range(5):
        _navigate(page, f"{reader_site}/{_TOPOLOGY}")
        _navigate(page, f"{reader_site}/{_ALGEBRA}")
    assert page.locator("#test-complete-reader mjx-container").count() == 0
    assert page.locator("#test-complete-reader .arithmatex").count() == 5
    assert held, "the actual pinned engine was not held"
    for route in held:
        pinned_runtime.serve(route)
    _wait_reader(page, _ALGEBRA_TITLE, _ALGEBRA_TEX)


def test_early_ssr_typesetting_then_delayed_graph_clone_keeps_real_menu_readback(
    reader_page: object, reader_site: str, pinned_runtime: _PinnedRuntime,
) -> None:
    page = reader_page
    _routes(page, reader_site, pinned_runtime)
    page.goto(f"{reader_site}/{_ALGEBRA}")
    assert page.evaluate("() => AutoformMath.refresh(document)") is True
    playwright.expect(page.locator(".bp-reading-source mjx-container")).to_have_count(5)
    page.evaluate("() => window.__originalSSRGlyph = document.querySelector('.bp-reading-source mjx-container')")
    data = page.locator(".bp-dag-viewer").get_attribute("data-graph-src")
    assert data
    from urllib.parse import urljoin
    held: list[object] = []
    def hold_graph(route: object) -> None:
        held.append(route)
        page.evaluate("() => window.__initialReaderGraphHeld = true")
    page.route(urljoin(page.url, data), hold_graph)
    _bridge(page)
    page.wait_for_function("() => document.querySelector('#test-complete-reader').children.length === 0")
    # Keep the original output visible until the graph commits its complete source.
    assert page.evaluate("() => __originalSSRGlyph.isConnected") is True
    page.wait_for_function("() => window.MathJax.version === '3.2.2'")
    page.wait_for_function("() => window.__initialReaderGraphHeld === true")
    assert held, "the initial graph request was not held"
    for route in held:
        route.continue_()
    assert page.evaluate("() => __readerStart.then(Boolean)") is True
    reader = _wait_reader(page, _ALGEBRA_TITLE, _ALGEBRA_TEX)
    assert page.evaluate("() => __originalSSRGlyph.isConnected") is False
    assert _math_source(page, reader.locator("mjx-container").last) == _ALGEBRA_TEX[-1]
    assert "mathvariant=\"double-struck\"" in _math_source(page, reader.locator("mjx-container").first, mathml=True)


def test_literal_dollars_stay_literal_and_cross_formula_macros_cannot_leak(
    reader_page: object, reader_site: str, pinned_runtime: _PinnedRuntime,
) -> None:
    page = reader_page
    _routes(page, reader_site, pinned_runtime)
    page.goto(f"{reader_site}/{_ALGEBRA}")
    _bridge(page)
    assert page.evaluate("() => __readerStart.then(Boolean)") is True
    reader = _wait_reader(page, _ALGEBRA_TITLE, _ALGEBRA_TEX)
    assert "$5 and $10" in reader.inner_text()
    assert reader.locator("code").filter(has_text="$not_math$").count() == 1
    _navigate(page, f"{reader_site}/{_EFFECTS}")
    assert page.evaluate("() => __readerMath") is True
    _navigate(page, f"{reader_site}/{_TOPOLOGY}")
    reader = _wait_reader(page, _TOPOLOGY_TITLE, _TOPOLOGY_TEX)
    compiled = _math_source(page, reader.locator("mjx-container").first, mathml=True)
    assert r"\stolen" in compiled and "EVIL" not in compiled
    assert 'mathvariant="double-struck"' in compiled
    assert page.evaluate("() => MathJax.config.tex.inlineMath.length === 0 && MathJax.config.tex.displayMath.length === 0")


def test_safe_pass_failure_keeps_visible_glyphs_until_real_engine_recovery(
    reader_page: object, reader_site: str, pinned_runtime: _PinnedRuntime,
) -> None:
    page = reader_page
    _routes(page, reader_site, pinned_runtime)
    page.goto(f"{reader_site}/{_ALGEBRA}")
    _bridge(page)
    assert page.evaluate("() => __readerStart.then(Boolean)") is True
    _wait_reader(page, _ALGEBRA_TITLE, _ALGEBRA_TEX)
    assert page.evaluate("""async () => {
      const reader = document.querySelector('#test-complete-reader'), original = reader.querySelector('.arithmatex');
      const copied = original.cloneNode(true); original.replaceWith(copied);
      const glyph = copied.querySelector('mjx-container'), mathjax = MathJax._.mathjax.mathjax;
      const retries = mathjax.handleRetriesFor;
      mathjax.handleRetriesFor = () => Promise.reject(new Error('controlled render failure'));
      try {
        const result = await AutoformMath.refresh(reader);
        return result === false && glyph.isConnected && copied.querySelector('mjx-container') === glyph;
      } finally { mathjax.handleRetriesFor = retries; }
    }""") is True
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('#test-complete-reader'))") is True
    reader = _wait_reader(page, _ALGEBRA_TITLE, _ALGEBRA_TEX)
    assert _math_source(page, reader.locator("mjx-container").first) == _ALGEBRA_TEX[0]


def test_loaded_wrong_version_leaves_complete_math_as_typed(
    reader_page: object, reader_site: str, pinned_runtime: _PinnedRuntime,
) -> None:
    page = reader_page
    page.add_init_script("""(() => {
      let current;
      Object.defineProperty(window, 'MathJax', {configurable: true, get: () => current, set: value => {
        current = value; let version = value.version;
        Object.defineProperty(value, 'version', {configurable: true,
          get: () => version === undefined ? undefined : '0.0.0', set: value => { version = value; }});
      }});
    })();""")
    _routes(page, reader_site, pinned_runtime)
    page.goto(f"{reader_site}/{_ALGEBRA}")
    page.wait_for_function("() => window.MathJax && MathJax.version === '0.0.0'")
    _bridge(page)
    assert page.evaluate("() => __readerStart.then(Boolean)") is True
    assert page.evaluate("() => __readerMath") is False
    reader = page.locator("#test-complete-reader")
    playwright.expect(reader.locator("h1")).to_have_text(_ALGEBRA_TITLE)
    assert reader.locator("mjx-container").count() == 0
    assert reader.locator(".arithmatex").count() == 5
    assert _ALGEBRA_TEX[-1] in reader.inner_text()


def test_unavailable_engine_keeps_reading_and_navigation_usable(
    reader_page: object, reader_site: str, pinned_runtime: _PinnedRuntime,
) -> None:
    page = reader_page
    _routes(page, reader_site, pinned_runtime)
    page.route(MATHJAX_BUNDLE, lambda route: route.abort())
    page.goto(f"{reader_site}/{_ALGEBRA}")
    _bridge(page)
    assert page.evaluate("() => __readerStart.then(Boolean)") is True
    assert page.evaluate("() => __readerMath") is False
    _navigate(page, f"{reader_site}/{_TOPOLOGY}")
    reader = page.locator("#test-complete-reader")
    playwright.expect(reader.locator("h1")).to_have_text(_TOPOLOGY_TITLE)
    assert reader.locator(".arithmatex").count() == 2
    assert reader.locator("mjx-container").count() == 0
    assert r"\stolen+\RR" in reader.inner_text()


def test_refresh_event_rebuilds_copied_math_without_reading_unmarked_text(
    reader_page: object, reader_site: str, pinned_runtime: _PinnedRuntime,
) -> None:
    page = reader_page
    _routes(page, reader_site, pinned_runtime)
    page.goto(f"{reader_site}/{_ALGEBRA}")
    _bridge(page)
    assert page.evaluate("() => __readerStart.then(Boolean)") is True
    _wait_reader(page, _ALGEBRA_TITLE, _ALGEBRA_TEX)
    page.evaluate("""() => {
      const reader = document.querySelector('#test-complete-reader'), formula = reader.querySelector('.arithmatex');
      formula.replaceWith(formula.cloneNode(true));
      const literal = document.createElement('p'); literal.id = 'unmarked-prices'; literal.textContent = '$5 and $10';
      reader.appendChild(literal);
      reader.dispatchEvent(new CustomEvent('autoform:math-refresh', {bubbles: true}));
    }""")
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('#test-complete-reader'))") is True
    reader = _wait_reader(page, _ALGEBRA_TITLE, _ALGEBRA_TEX)
    assert page.locator("#unmarked-prices").inner_text() == "$5 and $10"
    assert page.locator("#unmarked-prices mjx-container").count() == 0
    assert _math_source(page, reader.locator("mjx-container").first) == _ALGEBRA_TEX[0]


def test_fresh_source_ignores_authored_retained_tex_and_nonce(
    reader_page: object, reader_site: str, pinned_runtime: _PinnedRuntime,
) -> None:
    page = reader_page
    _routes(page, reader_site, pinned_runtime)
    held: list[object] = []
    page.route(MATHJAX_BUNDLE, lambda route: held.append(route))
    page.goto(f"{reader_site}/{_ALGEBRA}", wait_until="domcontentloaded")
    page.locator(".bp-reading-source .arithmatex").first.evaluate("""formula => {
      formula.setAttribute('data-autoform-math-source', '\\\\(FORGED_SOURCE\\\\)');
      formula.setAttribute('data-autoform-math-stamp', 'authored-nonce');
    }""")
    _bridge(page)
    assert page.evaluate("() => __readerStart.then(Boolean)") is True
    assert held
    for route in held:
        pinned_runtime.serve(route)
    reader = _wait_reader(page, _ALGEBRA_TITLE, _ALGEBRA_TEX)
    assert reader.locator(".arithmatex").first.get_attribute("data-autoform-math-stamp") != "authored-nonce"
    assert _math_source(page, reader.locator("mjx-container").first) == _ALGEBRA_TEX[0]


def test_glyph_clone_with_forged_source_stamp_is_not_reinterpreted(
    reader_page: object, reader_site: str, pinned_runtime: _PinnedRuntime,
) -> None:
    page = reader_page
    _routes(page, reader_site, pinned_runtime)
    page.goto(f"{reader_site}/{_ALGEBRA}")
    _bridge(page)
    assert page.evaluate("() => __readerStart.then(Boolean)") is True
    _wait_reader(page, _ALGEBRA_TITLE, _ALGEBRA_TEX)
    assert page.evaluate("""async () => {
      const reader = document.querySelector('#test-complete-reader'), owned = reader.querySelector('.arithmatex');
      const clone = owned.cloneNode(true), glyph = clone.querySelector('mjx-container');
      clone.setAttribute('data-autoform-math-source', '\\\\(FORGED_SOURCE\\\\)');
      clone.setAttribute('data-autoform-math-stamp', 'forged-nonce');
      owned.replaceWith(clone);
      return await AutoformMath.refresh(reader) === false && glyph.isConnected && clone.querySelector('mjx-container') === glyph;
    }""") is True
    playwright.expect(page.locator("#test-complete-reader mjx-container")).to_have_count(5)
    assert page.locator("[data-autoform-math-stage]").count() == 0
