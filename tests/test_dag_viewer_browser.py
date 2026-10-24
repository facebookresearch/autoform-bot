from __future__ import annotations

import functools
import json
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import html5lib
import pytest

from autoform_cli import dag_viewer
from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_integrated_publication import _decoded_relations

# CI sets this, so a missing browser fails the job instead of skipping every case.
_REQUIRED = os.environ.get("AUTOFORM_REQUIRE_BROWSER_TESTS") == "1"
if _REQUIRED:
    import playwright.sync_api as playwright
else:
    playwright = pytest.importorskip(
        "playwright.sync_api",
        reason="the optional browser extra is required for WebKit regression tests",
    )


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _served(directory: Path) -> Iterator[str]:
    handler = functools.partial(_QuietHandler, directory=str(directory))
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


# Module scope: Playwright's event loop stays running while the browser is open,
# and asyncio.run() in later test modules refuses to start under it.
@pytest.fixture(scope="module")
def webkit_browser() -> Iterator[object]:
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.webkit.launch()
        except playwright.Error as error:
            if _REQUIRED or "Executable doesn't exist" not in str(error):
                raise
            pytest.skip("WebKit is not installed; run `playwright install webkit`")
        yield browser
        browser.close()


@contextmanager
def _page(browser: object, **options: object) -> Iterator[object]:
    """Open a page in a context of its own, close it, and fail on any uncaught page error."""
    context = browser.new_context(**options)
    errors: list[str] = []
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        yield page
    finally:
        context.close()
    assert errors == []


@pytest.fixture()
def desktop_page(webkit_browser: object) -> Iterator[object]:
    with _page(webkit_browser, viewport={"width": 1280, "height": 900}) as page:
        yield page


@pytest.fixture()
def mobile_page(webkit_browser: object) -> Iterator[object]:
    # Reduced motion keeps the sheet from sliding while a test measures it.
    with _page(
        webkit_browser,
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
        reduced_motion="reduce",
    ) as page:
        yield page


@dataclass(frozen=True)
class _ExplorerConsumer:
    """Canonical routes and scope payloads captured from the actual publisher."""

    url: str
    records: dict[str, dict]
    graphs: dict[str, dict]
    headings: dict[str, str]
    page_titles: dict[str, str]

    def route(self, node_id: str) -> str:
        return urljoin(self.url + "/dependencies/index.json", self.records[node_id]["route"])

    def alias(self, node_id: str) -> str:
        return urljoin(self.url + "/dependencies/index.json", self.records[node_id]["url"])

    def heading(self, node_id: str) -> str:
        return self.headings[urlsplit(self.route(node_id)).path]

    def title(self, node_id: str) -> str:
        record = self.records[node_id]
        return record["title"] if record.get("result") else self.page_titles[urlsplit(self.route(node_id)).path]


def _write_consumer(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _consumer_article(roadmap: Path, node_id: str, title: str, *, depends: str = "",
                      claim: str = "not_ready: true\n", declaration: str = "theorem", text: str = "") -> None:
    _write_consumer(roadmap / (node_id + ".md"), (
        "---\ndeclaration: " + declaration + "\n" + claim + "---\n\n# " + title + "\n\n"
        + (text or "This authored synthetic statement retains the identity $0=0$ in its complete reading page.")
        + "\n\n## Depends on\n\n" + (depends or "No prerequisites.") + "\n"
    ))


@pytest.fixture(scope="module")
def explorer_consumer(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_ExplorerConsumer]:
    """Render real authored stress cases; no manual graph, status, layout or reading reconstruction.

    Lean source below is inspected by the publisher, not built by a proof kernel.
    The chapter has 63 leaves, global search has 60 matching auxiliary results,
    the large scope has 1,000 not-ready statements, and chain/star dependencies
    are authored between the statements of 33 actual child topics.
    """
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("explorer-consumer") / "project"
    scaffold_project(project, title="Explorer consumer")
    roadmap = project / "blueprint/roadmap"
    _write_consumer(project / "blueprint/README.md", (
        "# Explorer consumer\n\nComplete authored reading is available without JavaScript.\n\n"
        "- [Mathematical library](roadmap/README.md)\n"
    ))
    scopes = ("chapter", "remote", "chain", "star", "flat", "mixed", "atlas", "topics")
    _write_consumer(roadmap / "README.md", "# Mathematical library\n\n" + "".join(
        f"- [{scope.title()}]({scope}/README.md)\n" for scope in scopes
    ))
    _write_consumer(roadmap / "chapter/README.md", (
        "---\nnavigation: module\n---\n\n# Dependency chapter\n\n"
        "The complete chapter introduces the foundation, middle lemma and target theorem.\n\n"
        "- [Foundation](foundation.md)\n- [Middle lemma](middle.md)\n- [Target theorem](target.md)\n"
        + "".join(f"- [Auxiliary result {n}](auxiliary-{n:02d}.md)\n" for n in range(60))
    ))
    _consumer_article(roadmap, "chapter/foundation", "Foundation", declaration="def",
                      claim="statement: formalized\nlean: ExplorerFixture.foundation\n")
    _consumer_article(roadmap, "chapter/middle", "Middle lemma", declaration="lemma",
                      claim="statement: formalized\nlean: ExplorerFixture.middle\n",
                      depends="- [Foundation](foundation.md)")
    _consumer_article(roadmap, "chapter/target", "Target theorem", depends="- [Middle lemma](middle.md)", text=(
        "The target theorem gathers the foundation and the middle lemma into one statement: $0=0$.\n\n"
        + "\n\n".join(f"Explanation {n}. " + "This complete mathematical paragraph remains in the reading page. " * 4
                     for n in range(6))
        + "\n\nFinal sentence of the complete target statement."
    ))
    for n in range(60):
        _consumer_article(roadmap, f"chapter/auxiliary-{n:02d}", f"Auxiliary result {n}", declaration="lemma")
    _write_consumer(roadmap / "remote/README.md", (
        "---\nnavigation: module\n---\n\n# Remote scope\n\n- [Remote theorem](deep-theorem.md)\n"
    ))
    _consumer_article(roadmap, "remote/deep-theorem", "Remote theorem",
                      claim="statement: formalized\nlean: Remote.Namespace.deepTheorem\n",
                      text="A spectral gap statement for the remote synthetic fixture retains the identity $0=0$.")

    for shape in ("chain", "star"):
        _write_consumer(roadmap / f"{shape}/README.md", (
            f"---\nnavigation: module\n---\n\n# Connected {shape}\n\n"
            + "".join(f"- [Dense chapter {n}](chapter-{n:02d}/README.md)\n" for n in range(33))
        ))
        for n in range(33):
            _write_consumer(roadmap / f"{shape}/chapter-{n:02d}/README.md", (
                f"---\nnavigation: module\n---\n\n# Dense chapter {n}\n\n"
                "This authored topic retains its complete statement.\n\n- [Identity](result.md)\n"
            ))
            previous = n - 1 if shape == "chain" else 0
            depends = f"- [Earlier identity](../chapter-{previous:02d}/result.md)" if n else ""
            _consumer_article(roadmap, f"{shape}/chapter-{n:02d}/result", f"{shape.title()} identity {n}", depends=depends)

    _write_consumer(roadmap / "flat/README.md", (
        "---\nnavigation: module\n---\n\n# Flat scope\n\n"
        "Every item below is an authored not-ready statement, not an invented payload entry.\n\n"
        + "".join(f"- [Flat item {n}](item-{n:04d}.md)\n" for n in range(1_000))
    ))
    for n in range(1_000):
        _consumer_article(roadmap, f"flat/item-{n:04d}", f"Flat item {n}")

    mixed = ("a", "b", "isolate-1", "isolate-2")
    _write_consumer(roadmap / "mixed/README.md", (
        "---\nnavigation: module\n---\n\n# Mixed scope\n\n"
        + "".join(f"- [Mixed {name}]({name}.md)\n" for name in mixed)
    ))
    for name in mixed:
        _consumer_article(roadmap, f"mixed/{name}", f"Mixed {name}", depends="- [Mixed a](a.md)" if name == "b" else "")

    areas = ("Algebra", "Analysis & Probability", "Discrete Mathematics & Computation",
             "Foundations & Logic", "Geometry & Topology")
    atlas_topics = [("topology", "Topology", areas[-1], 80), ("algebra", "Algebra", areas[0], 40),
                    ("analysis", "Analysis", areas[1], 20)]
    atlas_topics += [(f"auxiliary-{n:02d}", f"Auxiliary topic {n}", areas[n % len(areas)], 1) for n in range(26)]
    _write_consumer(roadmap / "atlas/README.md", (
        "---\nnavigation: module\n---\n\n# Knowledge atlas\n\n"
        "The regions below come from these authored areas, not from file paths.\n\n"
        + "".join(f"- [{title}]({name}/README.md)\n" for name, title, _area, _count in atlas_topics)
        + "- [Library inventory](library/README.md)\n"
    ))
    for name, title, area, count in atlas_topics:
        _write_consumer(roadmap / f"atlas/{name}/README.md", (
            f"---\nnavigation: module\narea: {area}\n---\n\n# {title}\n\n"
            + ("Topology studies continuity, deformation, and invariants preserved by continuous maps.\n\n" if name == "topology" else "")
            + "".join(f"- [Identity {n}](item-{n:02d}.md)\n" for n in range(count))
        ))
        for n in range(count):
            if name == "algebra" or name == "topology" and n < 60:
                claim = "statement: formalized\nproof: formalized\nlean: ExplorerFixture.zero_identity\n"
            elif name == "analysis" or name == "topology":
                claim = "statement: formalized\nlean: ExplorerFixture.zero_identity\n"
            else:
                claim = "not_ready: true\n"
            depends = ""
            if name == "topology" and n == 0:
                depends = "- [Algebra identity](../algebra/item-00.md)\n- [Analysis identity](../analysis/item-00.md)"
            _consumer_article(roadmap, f"atlas/{name}/item-{n:02d}", f"{title} identity {n}", claim=claim, depends=depends)
    _write_consumer(roadmap / "atlas/library/README.md", (
        "---\nnavigation: source\narea: Algebra\n---\n\n# Library inventory\n\n"
        "- [Inspected module](module.md)\n"
    ))
    _write_consumer(roadmap / "atlas/library/module.md", (
        "---\ncatalog: module\n---\n\n# Inspected module\n\n"
        "This module inventory is not a mathematical proof.\n"
    ))

    _write_consumer(roadmap / "topics/README.md", (
        "---\nnavigation: module\n---\n\n# Topics and results\n\n"
        "- [Compactness](compactness/README.md)\n- [Heine-Borel theorem](heine-borel.md)\n"
        "- [Tychonoff theorem](tychonoff.md)\n- [Topology library](library.md)\n"
    ))
    _write_consumer(roadmap / "topics/compactness/README.md", (
        "---\nnavigation: module\n---\n\n# Compactness\n\n- [Identity](identity.md)\n"
    ))
    _consumer_article(roadmap, "topics/compactness/identity", "Compactness identity")
    for name, title in (("heine-borel", "Heine-Borel theorem"), ("tychonoff", "Tychonoff theorem")):
        _consumer_article(roadmap, f"topics/{name}", title)
    names = ["Topology.«open, closed»", *(f"Topology.lemma{n}" for n in range(2, 11))]
    _write_consumer(roadmap / "topics/library.md", (
        "---\ncatalog: module\nlean: " + ", ".join(names) + "\n---\n\n# Topology library\n\n"
        "All ten names are retained in the full authored inventory:\n\n"
        + "\n".join(f"- \x60{name}\x60" for name in names) + "\n"
    ))
    _write_consumer(project / "ExplorerFixture.lean", (
        "namespace ExplorerFixture\ndef foundation : Nat := 0\n"
        "theorem middle : (0 : Nat) = 0 := rfl\n"
        "theorem zero_identity : (0 : Nat) = 0 := rfl\nend ExplorerFixture\n"
        "namespace Remote.Namespace\ntheorem deepTheorem : (0 : Nat) = 0 := by sorry\nend Remote.Namespace\n"
        "namespace Topology\ndef «open, closed» : Nat := 0\n"
        + "".join(f"theorem lemma{n} : (0 : Nat) = 0 := rfl\n" for n in range(2, 11)) + "end Topology\n"
    ))
    render_site(project / "blueprint", project / "site-src", lean_root=project)
    build(load_config(config_file=str(project / "mkdocs.yml"), strict=True))
    index_path = project / "site/dependencies/index.json"
    records = {node["id"]: node for node in json.loads(index_path.read_text(encoding="utf-8"))["nodes"]}
    graphs = {
        scope: json.loads((index_path.parent / unquote(urlsplit(records[scope]["data_url"]).path)).read_text(encoding="utf-8"))
        for scope in scopes
    }
    headings, page_titles = {}, {}
    for record in records.values():
        path = urlsplit(urljoin("https://fixture.invalid/dependencies/index.json", record["route"])).path
        if path in headings:
            continue
        document = html5lib.parse((project / "site" / unquote(path.lstrip("/"))).read_text(encoding="utf-8"),
                                 namespaceHTMLElements=False)
        source = next(node for node in document.iter("div") if "bp-reading-source" in node.get("class", "").split())
        heading = next(source.iter("h1"), None)
        assert source.get("data-page-title") or heading is not None, "Published reading needs its own heading or title metadata"
        page_titles[path] = source.get("data-page-title") or "".join(heading.itertext()).strip()
        headings[path] = "".join(heading.itertext()).strip() if heading is not None else page_titles[path]
    assert len(graphs["chapter"]["nodes"]) == 63
    assert len(graphs["flat"]["nodes"]) == 1_000
    assert [records[f"chapter/{name}"]["status"] for name in ("foundation", "middle", "target")] == [
        "fully_proved", "can_prove", "not_ready",
    ]
    assert sum(node["title"].startswith("Auxiliary result ") for node in records.values()) == 60
    assert all(records[node_id]["result"] for node_id in ("chapter/target", "remote/deep-theorem", "flat/item-0150"))

    # A deliberately unsupported bare host is static only. Its links point to
    # the same real publisher output; it must never launch a second renderer.
    fallback = dag_viewer.render_container(
        urljoin("/dependencies/index.json", records["roadmap"]["data_url"]), script_href="javascripts/blueprint-dag.js",
        style_href="stylesheets/blueprint-dag.css",
        fallback_links=tuple((records[f"chapter/{name}"]["title"], urljoin("/dependencies/index.json", records[f"chapter/{name}"]["route"]))
                             for name in ("foundation", "middle", "target")),
    )
    _write_consumer(project / "site/bare.html", "<!doctype html><title>Linked fallback</title><main>" + fallback + "</main>")
    with _served(project / "site") as url:
        yield _ExplorerConsumer(url, records, graphs, headings, page_titles)


@pytest.fixture(scope="module")
def empty_consumer_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("empty-explorer-consumer") / "project"
    scaffold_project(project, title="Fresh roadmap")
    _write_consumer(project / "blueprint/roadmap/README.md", "# Fresh roadmap\n\nNo mathematical articles are authored yet.\n")
    render_site(project / "blueprint", project / "site-src")
    build(load_config(config_file=str(project / "mkdocs.yml"), strict=True))
    with _served(project / "site") as url:
        yield url


@pytest.fixture(scope="module")
def site_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Serve a blueprint as render and MkDocs publish it, under Material's styles and scripts."""
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("site") / "project"
    scaffold_project(project, title="Explorer fixture")
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text(
        "# Fixture roadmap\n\n- [Algebra](algebra/README.md)\n- [Topology](topology/README.md)\n",
        encoding="utf-8",
    )
    for chapter in ("Algebra", "Topology"):
        folder = roadmap / chapter.lower()
        folder.mkdir()
        (folder / "README.md").write_text(
            f"# {chapter}\n\n- [Base](base.md)\n- [Result](result.md)\n",
            encoding="utf-8",
        )
        (folder / "base.md").write_text(
            "---\ndeclaration: def\n---\n\n# Base\n\nThe base object.\n\n## Depends on\n\nNo prerequisites.\n",
            encoding="utf-8",
        )
        (folder / "result.md").write_text(
            "---\ndeclaration: theorem\n---\n\n# Result\n\nThe main result.\n\n## Depends on\n\n- [Base](base.md)\n",
            encoding="utf-8",
        )
    render_site(project / "blueprint", project / "site-src")
    build(load_config(config_file=str(project / "mkdocs.yml")))
    with _served(project / "site") as url:
        yield url


def _offline(page: object, site_url: str) -> object:
    """Keep a built site's CDN scripts and web fonts off the network."""
    page.route(lambda url: not url.startswith(site_url), lambda route: route.abort())
    return page


def _open_site(page: object, site_url: str, path: str) -> object:
    """Open a real published consumer, including legacy aliases, after its full reader mounts."""
    _offline(page, site_url)
    page.goto(f"{site_url}/{path}")
    _ready_site(page)
    return page


def _ready_site(page: object) -> None:
    page.locator(".bp-dag-integrated .bp-dag-node").first.wait_for()
    playwright.expect(page.locator(".bp-reading-body > .bp-reading-source")).to_have_count(1)
    playwright.expect(page.locator(".bp-explorer-page > .bp-reading-source")).to_have_count(0)


def _site_record(page: object, site_url: str, node_id: str) -> dict[str, object]:
    response = page.request.get(f"{site_url}/dependencies/index.json", timeout=10_000)
    assert response.ok
    return next(node for node in response.json()["nodes"] if node["id"] == node_id)


def _site_route(page: object, site_url: str, node_id: str) -> str:
    record = _site_record(page, site_url, node_id)
    assert isinstance(record["route"], str)
    route = urljoin(f"{site_url}/dependencies/index.json", record["route"])
    assert route.startswith(site_url + "/roadmap/"), "The reader permalink must be a canonical book page"
    return route


def _reading_card(page: object, node_id: str) -> object:
    card = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{node_id}"]')
    playwright.expect(card).to_have_count(1)
    return card


def _assert_hit(page: object, control: object) -> None:
    playwright.expect(control).to_be_visible()
    assert control.evaluate(
        """element => {
          const box = element.getBoundingClientRect();
          const x = Math.max(0, box.left) + Math.min(box.width, innerWidth - Math.max(0, box.left)) / 2;
          const y = Math.max(0, box.top) + Math.min(box.height, innerHeight - Math.max(0, box.top)) / 2;
          const hit = document.elementFromPoint(x, y);
          return hit === element || element.contains(hit);
        }"""
    )


def _open_dependency(card: object, title: str) -> object:
    """Use the publisher's native disclosure and its numbered reference label."""
    disclosure = card.locator("details.bp-dependencies")
    playwright.expect(disclosure).to_have_count(1)
    if disclosure.get_attribute("open") is None:
        disclosure.locator(":scope > summary").click()
    link = disclosure.get_by_role("link", name=re.compile(r"\(" + re.escape(title) + r"\)$"))
    playwright.expect(link).to_be_visible()
    return link


def _close_phone_reading(page: object) -> None:
    """Reveal the map/list through the actual full-sheet Reading handle."""
    handle = page.locator(".bp-dag-sheet-handle")
    if handle.get_attribute("aria-expanded") == "true":
        _assert_hit(page, handle)
        handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    playwright.expect(page.locator(".bp-dag-inspector-inner")).to_have_attribute("inert", "")
    _paint_frame(page)


@pytest.fixture(scope="module")
def marked_math_site_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A genuine marked-math consumer with 55 authored dependents, not an inconsistent payload-only patch."""
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("marked-math-site") / "project"
    scaffold_project(project, title="Marked mathematics fixture")
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text(
        "# Mathematical library\n\n- [Algebra](algebra/README.md)\n- [Topology](topology/README.md)\n",
        encoding="utf-8",
    )
    expressions = (r"L\neq\{0\}", r"\overline{G}=C_n", r"D_n", r"n=1,2,3,4", r"6")
    for name in ("algebra", "topology"):
        folder = roadmap / name
        folder.mkdir()
        links = "- [Base](base.md)\n- [Result](result.md)\n"
        if name == "topology":
            links += "".join(f"- [Later {n}](later-{n}.md)\n" for n in range(55))
        (folder / "README.md").write_text(
            f"---\nnavigation: module\n---\n\n# {name.title()}\n\n{links}",
            encoding="utf-8",
        )
        (folder / "base.md").write_text(
            "---\ndeclaration: def\n---\n\n# Base\n\nThe base value is $0$.\n\n## Depends on\n\nNo prerequisites.\n",
            encoding="utf-8",
        )
        notations = ", ".join("$" + expression + "$" for expression in expressions)
        (folder / "result.md").write_text(
            "---\ndeclaration: theorem\n---\n\n# Result\n\n"
            + "The synthetic reading fixture retains these five mathematical notations: " + notations + ".\n\n"
            + "This complete explanatory paragraph stays in the mathematical reader. " * 10
            + "\n\nFinal sentence of the complete statement.\n\n## Depends on\n\n- [Base](base.md)\n",
            encoding="utf-8",
        )
        if name == "topology":
            for n in range(55):
                (folder / f"later-{n}.md").write_text(
                    f"---\ndeclaration: theorem\n---\n\n# Later {n}\n\nThe identity $0=0$ is stated explicitly.\n\n"
                    "Its complete explanatory text remains available after relation navigation.\n\n"
                    "## Depends on\n\n- [Result](result.md)\n",
                    encoding="utf-8",
                )
    render_site(project / "blueprint", project / "site-src")
    build(load_config(config_file=str(project / "mkdocs.yml")))
    index = json.loads((project / "site/dependencies/index.json").read_text(encoding="utf-8"))
    assert sum(edge[0] == "topology/result" for edge in _decoded_relations(index)) == 55
    with _served(project / "site") as url:
        yield url


@pytest.fixture(scope="module")
def marked_math_runtime() -> object:
    from tests.test_integrated_explorer_browser import _PinnedRuntime

    return _PinnedRuntime()


def _consumer_ready(page: object, consumer: _ExplorerConsumer, node_id: str = "chapter") -> None:
    page.locator(".bp-dag-integrated .bp-dag-stage").wait_for(state="attached")
    playwright.expect(page.locator(".bp-reading-body > .bp-reading-source")).to_have_count(1)
    playwright.expect(page.locator(".bp-explorer-page > .bp-reading-source")).to_have_count(0)
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(consumer.heading(node_id))
    playwright.expect(page).to_have_title(consumer.title(node_id))
    page.wait_for_function("document.querySelector('.bp-dag-integrated')?.getAttribute('aria-busy') !== 'true'")


def _open_consumer(page: object, consumer: _ExplorerConsumer, node_id: str = "chapter", *, suffix: str = "") -> None:
    _offline(page, consumer.url)
    page.goto(consumer.route(node_id) + suffix)
    _consumer_ready(page, consumer, node_id)


def _consumer_selected(page: object, consumer: _ExplorerConsumer, node_id: str) -> object:
    _consumer_ready(page, consumer, node_id)
    control = page.locator(f'.bp-dag-node[data-autoform-node-id="{node_id}"]')
    playwright.expect(control).to_have_attribute("data-selected", "true")
    anchor = unquote(urlsplit(consumer.route(node_id)).fragment)
    if anchor:
        playwright.expect(page.locator(f'.bp-reading-body [id="{anchor}"].bp-reading-current')).to_have_count(1)
    return control


def _focus_consumer_node(page: object, target_id: str) -> object:
    """Reach a culled source node with ordinary map keys, not a hidden/forced click."""
    handle = page.locator(".bp-dag-sheet-handle")
    if page.viewport_size["width"] <= 720 and handle.get_attribute("aria-expanded") == "true":
        handle.click()
        _paint_frame(page)
    pending: list[tuple[str, ...]] = [()]
    visited: set[str] = set()
    while pending and len(visited) <= 120:
        path = pending.pop(0)
        page.get_by_role("button", name="Fit shown nodes", exact=True).click()
        _paint_frame(page)
        page.locator('.bp-dag-node[tabindex="0"]').first.focus()
        for key in path:
            page.keyboard.press(key)
            _paint_frame(page)
        active = page.locator(".bp-dag-node:focus")
        node_id = active.get_attribute("data-autoform-node-id")
        if node_id == target_id:
            _assert_hit(page, active)
            return active
        if node_id in visited:
            continue
        visited.add(node_id)
        pending.extend((*path, key) for key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"))
    pytest.fail(f"Authored node is not keyboard-reachable: {target_id}")


def _load_search_index(page: object, entries: int) -> object:
    """Focus the search box and wait until the whole-roadmap index of *entries* records has loaded."""
    search = page.locator(".bp-dag-search")
    search.focus()
    playwright.expect(search).to_have_attribute("placeholder", f"Search {entries:,} roadmap entries")
    return search


def test_mobile_node_interaction_and_selected_sheet_collapse(mobile_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = mobile_page
    _open_consumer(page, explorer_consumer, "chapter/foundation")
    _focus_consumer_node(page, "chapter/middle").tap()
    node = _consumer_selected(page, explorer_consumer, "chapter/middle")
    playwright.expect(node.locator(".bp-dag-node-meta")).to_have_text("ready to prove")
    reader = page.locator(".bp-reading-body")
    playwright.expect(reader.locator("h1")).to_have_text(explorer_consumer.heading("chapter/middle"))
    playwright.expect(_reading_card(page, "chapter/middle")).to_contain_text("Middle lemma")
    handle, inner = page.locator(".bp-dag-sheet-handle"), page.locator(".bp-dag-inspector-inner")
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(handle).to_have_attribute("aria-controls", inner.get_attribute("id"))
    assert inner.get_attribute("aria-hidden") is None and inner.get_attribute("inert") is None
    assert page.url == explorer_consumer.route("chapter/middle")
    _assert_hit(page, handle)
    handle.tap()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    playwright.expect(inner).to_have_attribute("aria-hidden", "true")
    playwright.expect(inner).to_have_attribute("inert", "")
    playwright.expect(node).to_have_attribute("data-selected", "true")
    handle.focus()
    page.keyboard.press("Tab")
    assert not inner.evaluate("element => element.contains(document.activeElement)")
    # Collapse never replaces the theorem by the former short summary inspector.
    playwright.expect(reader.locator("h1")).to_have_text(explorer_consumer.heading("chapter/middle"))


def test_app_layout_fills_the_browser_viewport(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_consumer(page, explorer_consumer)
    viewer = page.locator(".bp-dag-viewer").bounding_box()
    assert viewer is not None and viewer["x"] <= 1 and viewer["y"] <= 1
    assert viewer["width"] >= 1430 and viewer["height"] >= 890
    rail, stage, reader = (page.locator(selector).bounding_box() for selector in (
        ".bp-dag-navigation", ".bp-dag-stage", ".bp-reading-pane",
    ))
    assert rail is not None and stage is not None and reader is not None
    assert rail["x"] + rail["width"] <= stage["x"] + 1
    assert stage["x"] + stage["width"] <= reader["x"] + 1
    assert stage["width"] >= 550 and reader["width"] >= 340


@pytest.mark.parametrize("page_name", ("chain", "star"))
def test_connected_project_maps_open_at_readable_scale(
    desktop_page: object, explorer_consumer: _ExplorerConsumer, page_name: str,
) -> None:
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_consumer(page, explorer_consumer, page_name)
    graph = explorer_consumer.graphs[page_name]
    assert len(graph["nodes"]) == 33 and len(graph["edges"]) == 32
    node = page.locator('.bp-dag-node[tabindex="0"]')
    _assert_hit(page, node)
    box = node.bounding_box()
    assert box is not None and box["height"] >= 44
    density = page.locator(".bp-dag-density")
    playwright.expect(density).to_have_text("Pan or Results to see all 33 items")
    assert density.evaluate(
        "element => ['borderTopStyle','boxShadow','pointerEvents'].map(name => getComputedStyle(element)[name])"
    ) == ["none", "none", "none"]
    assert _canvas_pixels(page)["painted"] > 0
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": 390, "height": 844})
    _close_phone_reading(page)
    _paint_frame(page)
    playwright.expect(density).to_have_text("Pan or Results to see all 33 items")
    note, sheet = density.bounding_box(), page.locator(".bp-dag-sheet-handle").bounding_box()
    assert note is not None and sheet is not None and note["y"] + note["height"] <= sheet["y"] - 8
    _assert_hit(page, page.locator('.bp-dag-node[tabindex="0"]'))


def test_atlas_hierarchy_keeps_topics_readable_and_details_out_of_the_way(
    desktop_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_consumer(page, explorer_consumer, "atlas")
    playwright.expect(page.locator(".bp-dag-atlas")).to_have_count(1)
    playwright.expect(page.locator(".bp-dag-guide")).to_have_count(0)
    region = _focus_consumer_node(page, "region:Geometry & Topology")
    assert "Geometry & Topology" in region.get_attribute("aria-label")
    region.click()
    handle = page.locator(".bp-dag-sheet-handle")
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    topic = _focus_consumer_node(page, "scope:atlas/topology")
    playwright.expect(topic.locator(".bp-dag-node-meta")).to_have_text("80 items")
    assert "50%" not in topic.evaluate("element => getComputedStyle(element).borderRadius")
    ring = topic.evaluate("element => getComputedStyle(element).backgroundImage")
    assert "conic-gradient" in ring
    # Validate the actual palette and scheme, not one browser's RGB serialization.
    original = next(node for node in explorer_consumer.graphs["atlas"]["nodes"] if node["id"] == "scope:atlas/topology")
    scheme = "dark" if page.locator("body").get_attribute("data-md-color-scheme") == "slate" else "light"
    palette = explorer_consumer.graphs["atlas"]["palette"]
    entries = [(key, count) for key, count in original["status_counts"] if count > 0]
    total = sum(count for _key, count in entries)
    actual = topic.evaluate("node => node.style.getPropertyValue('--dag-status-ring')")
    assert "fully_proved" in dict(entries) and "can_prove" in dict(entries)
    assert [key for key, _count in entries].index("fully_proved") < [key for key, _count in entries].index("can_prove")
    assert total == 80
    # CSS preserves the source colors but may round fractional degree text;
    # assert every segment's color/order and its authoritative proportion.
    segments = re.findall(r"(#[0-9a-fA-F]+)\s+([\d.]+)deg\s+([\d.]+)deg", actual)
    assert len(segments) == len(entries), {"ring": ring, "inline": actual, "entries": entries}
    cursor = 0.0
    for (key, count), (color, start, end) in zip(entries, segments):
        assert color.lower() == palette[key][scheme]["stroke"].lower()
        assert abs(float(start) - cursor) < .001
        cursor += count / total * 360
        assert abs(float(end) - cursor) < .001
    topic.click()
    _consumer_ready(page, explorer_consumer, "atlas/topology")
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(page.locator(".bp-reading-body")).to_contain_text("continuity, deformation, and invariants")
    assert len(explorer_consumer.graphs["atlas"]["nodes"]) == 30
    # Source inventories keep their honest authored role, never a proof-status summary.
    page.get_by_role("button", name="Sources", exact=True).click()
    row = page.locator(".bp-dag-inventory-row", has_text="Library inventory")
    row.click()
    _consumer_ready(page, explorer_consumer, "atlas/library")
    # The publisher inlines this inventory into its source chapter. Activate
    # its existing native permalink, not a fictitious standalone index link.
    inventory = _reading_card(page, "atlas/library/module")
    permalink = inventory.locator("a.bp-permalink")
    playwright.expect(permalink).to_have_count(1)
    assert urljoin(page.url, permalink.get_attribute("href")) == explorer_consumer.route("atlas/library/module")
    permalink.scroll_into_view_if_needed()
    permalink.hover()
    _assert_hit(page, permalink)
    permalink.click()
    _consumer_ready(page, explorer_consumer, "atlas/library/module")
    playwright.expect(page.locator(".bp-reading-body")).to_contain_text("not a mathematical proof")
    assert page.locator(".bp-dag-distribution,.bp-dag-status-pill").count() == 0


def test_a_view_of_topics_and_results_draws_readable_result_cards(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer, "topics")
    result = _focus_consumer_node(page, "topics/heine-borel")
    title = result.locator(".bp-dag-node-title")
    assert title.evaluate("element => element.scrollWidth <= element.clientWidth")
    assert not page.locator(".bp-dag-viewer").evaluate("element => element.classList.contains('bp-dag-atlas')")
    scope = _focus_consumer_node(page, "scope:topics/compactness")
    playwright.expect(scope.locator(".bp-dag-node-meta")).to_have_text("1 item")
    library = _focus_consumer_node(page, "topics/library")
    library.click()
    _consumer_ready(page, explorer_consumer, "topics/library")
    # The full reading inventory supersedes the removed eight-name summary cap.
    for name in ["Topology.«open, closed»", *(f"Topology.lemma{n}" for n in range(2, 11))]:
        playwright.expect(page.locator(".bp-reading-body")).to_contain_text(name)


def test_a_roadmap_without_articles_says_so(desktop_page: object, empty_consumer_url: str) -> None:
    page = desktop_page
    _offline(page, empty_consumer_url)
    page.goto(empty_consumer_url + "/roadmap/index.html")
    page.locator(".bp-dag-integrated .bp-dag-stage").wait_for()
    empty = page.locator(".bp-dag-empty")
    playwright.expect(empty).to_be_visible()
    playwright.expect(empty).to_have_text("This roadmap has no articles yet.")
    playwright.expect(page.locator(".bp-reading-body")).to_contain_text("No mathematical articles are authored yet.")


def _click_empty_canvas(page: object) -> None:
    empty = page.evaluate("""() => {
      const stage=document.querySelector('.bp-dag-stage').getBoundingClientRect();
      for(let y=stage.top+8;y<stage.bottom;y+=16)
        for(let x=stage.left+8;x<stage.right;x+=16)
          if(document.elementFromPoint(x,y)?.classList.contains('bp-dag-canvas')) return [x,y];
      return null;
    }""")
    assert empty is not None
    page.mouse.click(*empty)
    _paint_frame(page)


def test_a_click_on_empty_map_space_keeps_the_readers_zoom(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_consumer(page, explorer_consumer, "mixed")
    page.get_by_role("button", name="Zoom in").click()
    _paint_frame(page)
    transforms = "() => Array.from(document.querySelectorAll('.bp-dag-node'), node => [node.dataset.autoformNodeId,node.style.transform])"
    before = page.evaluate(transforms)
    _click_empty_canvas(page)
    assert page.evaluate(transforms) == before
    _focus_consumer_node(page, "mixed/b").click()
    node = _consumer_selected(page, explorer_consumer, "mixed/b")
    page.get_by_role("button", name="Zoom in").click()
    _paint_frame(page)
    before = node.evaluate("node => node.style.transform")
    _click_empty_canvas(page)
    playwright.expect(node).to_have_attribute("data-selected", "false")
    assert node.evaluate("node => node.style.transform") == before
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(explorer_consumer.heading("mixed/b"))


def test_oversized_flat_scope_defaults_to_contents(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_consumer(page, explorer_consumer, "flat")
    playwright.expect(page.locator(".bp-dag-inventory")).to_be_visible()
    playwright.expect(page.get_by_role("button", name="Map · too many items")).to_be_disabled()
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("100 of 1,000")
    playwright.expect(page.locator(".bp-dag-inventory-row")).to_have_count(100)
    playwright.expect(page.locator(".bp-dag-node")).to_have_count(0)
    playwright.expect(page.locator(".bp-reading-body")).to_contain_text("Every item below is an authored not-ready statement")


def test_choosing_from_contents_keeps_the_readers_place_and_focus(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    """Back restores an actual paginated Results row, not the former summary-only inspector."""
    page = desktop_page
    _open_consumer(page, explorer_consumer, "flat")
    page.locator(".bp-dag-inventory > .bp-dag-load-more").click()
    row = page.locator('.bp-dag-inventory-row[data-inventory-node-id="flat/item-0150"]')
    scroll = page.locator(".bp-dag-inventory-scroll")
    row.scroll_into_view_if_needed()
    row.focus()
    offset = scroll.evaluate("element => {element.scrollTop += 40;return element.scrollTop;}")
    assert offset > 0
    page.evaluate("window.__consumerDocument=document")
    row.press("Enter")
    _consumer_selected(page, explorer_consumer, "flat/item-0150")
    playwright.expect(page.locator(".bp-reading-body h1")).to_be_focused()
    playwright.expect(_reading_card(page, "flat/item-0150")).to_contain_text("complete reading page")
    page.go_back()
    _consumer_ready(page, explorer_consumer, "flat")
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("200 of 1,000")
    playwright.expect(row).to_be_focused()
    assert abs(scroll.evaluate("element => element.scrollTop") - offset) < 1
    playwright.expect(row).to_be_in_viewport()
    assert page.evaluate("document===window.__consumerDocument") is True
    # The complete article comes back without discarding the restored list entry.
    page.go_forward()
    _consumer_selected(page, explorer_consumer, "flat/item-0150")
    page.go_back()
    _consumer_ready(page, explorer_consumer, "flat")
    playwright.expect(row).to_be_focused()
    assert abs(scroll.evaluate("element => element.scrollTop") - offset) < 1


def test_phone_selection_opens_full_reading_and_returns_to_a_reachable_selected_map(
    mobile_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    page = mobile_page
    _open_consumer(page, explorer_consumer)
    page.evaluate("window.__consumerDocument=document")
    search = _load_search_index(page, len(explorer_consumer.records))
    search.fill("Target theorem")
    search.press("Enter")
    node = _consumer_selected(page, explorer_consumer, "chapter/target")
    playwright.expect(page.locator(".bp-reading-body h1")).to_be_focused()
    sheet, body = page.locator(".bp-reading-pane").bounding_box(), page.locator(".bp-dag-body").bounding_box()
    assert sheet is not None and body is not None
    assert abs(sheet["y"] - body["y"]) < 1 and abs(sheet["height"] - body["height"]) < 1
    playwright.expect(_reading_card(page, "chapter/target")).to_contain_text("Final sentence of the complete target statement.")
    _close_phone_reading(page)
    box, peek, stage = node.bounding_box(), page.locator(".bp-dag-sheet-handle").bounding_box(), page.locator(".bp-dag-stage").bounding_box()
    assert box is not None and peek is not None and stage is not None
    assert stage["x"] <= box["x"] and box["x"] + box["width"] <= stage["x"] + stage["width"]
    assert stage["y"] <= box["y"] and box["y"] + box["height"] <= peek["y"]
    _assert_hit(page, node)
    playwright.expect(node).to_have_attribute("data-selected", "true")
    assert page.evaluate("document===window.__consumerDocument") is True


def test_mobile_contents_pagination_stays_above_the_collapsed_sheet(mobile_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = mobile_page
    _open_consumer(page, explorer_consumer, "flat")
    page.get_by_role("button", name="Results", exact=True).click()
    _close_phone_reading(page)
    more = page.locator(".bp-dag-inventory > .bp-dag-load-more")
    more.scroll_into_view_if_needed()
    _assert_hit(page, more)
    box, handle = more.bounding_box(), page.locator(".bp-dag-sheet-handle").bounding_box()
    assert box is not None and handle is not None and box["y"] + box["height"] <= handle["y"]
    more.tap()
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("200 of 1,000")


def test_mobile_isolates_do_not_overlap_the_connected_dag(mobile_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = mobile_page
    _open_consumer(page, explorer_consumer, "mixed")
    _focus_consumer_node(page, "mixed/a")
    # Compare the real shared layout's controls, never a separate mobile grid.
    boxes = page.locator(".bp-dag-node").evaluate_all(
        "nodes => nodes.map(node => ({id:node.dataset.autoformNodeId,box:node.getBoundingClientRect().toJSON()}))"
    )
    assert boxes
    for n, left in enumerate(boxes):
        for right in boxes[n + 1:]:
            a, b = left["box"], right["box"]
            assert a["right"] <= b["left"] or b["right"] <= a["left"] or a["bottom"] <= b["top"] or b["bottom"] <= a["top"]
    _assert_hit(page, _focus_consumer_node(page, "mixed/isolate-1"))
    page.locator(".bp-dag-filter-summary").tap()
    playwright.expect(page.get_by_role("checkbox", name="Include items without edges")).to_be_visible()


def test_a_plain_wheel_pans_the_full_page_explorer(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer, "chapter/middle")
    node = _consumer_selected(page, explorer_consumer, "chapter/middle")
    _paint_frame(page)
    before, stage = node.bounding_box(), page.locator(".bp-dag-stage").bounding_box()
    assert before is not None and stage is not None
    page.mouse.move(stage["x"] + stage["width"] / 2, stage["y"] + stage["height"] / 2)
    page.mouse.wheel(0, -120)
    _paint_frame(page)
    after = node.bounding_box()
    assert after is not None and after["y"] > before["y"]
    assert abs(after["height"] - before["height"]) < .5
    scale = r"node => Number(/scale\(([^)]+)\)/.exec(node.style.transform)[1])"
    unzoomed = node.evaluate(scale)
    page.keyboard.down("Control")
    page.mouse.wheel(0, -10)
    page.keyboard.up("Control")
    _paint_frame(page)
    assert 1 < node.evaluate(scale) / unzoomed < 1.1


def test_search_keyboard_navigation_and_focus(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    search = _load_search_index(page, len(explorer_consumer.records))
    results, announcement = page.locator(".bp-dag-search-results"), page.locator("p.bp-dag-sr-only")
    assert search.get_attribute("aria-expanded") is None
    search.fill("no such node")
    playwright.expect(announcement).to_have_text("No results")
    search.fill("middle")
    expected = sum("middle" in " ".join(str(record.get(field, "")) for field in ("title", "id", "lean", "summary")).lower()
                   for record in explorer_consumer.records.values())
    playwright.expect(announcement).to_have_text(f"{expected} results")
    playwright.expect(results).to_be_visible()
    search.press("ArrowDown")
    result = page.locator(".bp-dag-result").first
    playwright.expect(result).to_be_focused()
    result.press("Escape")
    playwright.expect(search).to_be_focused()
    playwright.expect(results).to_be_hidden()
    search.press("ArrowDown")
    playwright.expect(result).to_be_focused()
    page.get_by_role("button", name="Results", exact=True).focus()
    playwright.expect(results).to_be_hidden()
    search.fill("Target theorem")
    page.evaluate("window.__consumerDocument=document")
    search.press("Enter")
    _consumer_selected(page, explorer_consumer, "chapter/target")
    playwright.expect(page.locator(".bp-reading-body h1")).to_be_focused()
    assert page.url == explorer_consumer.route("chapter/target")
    assert page.evaluate("document===window.__consumerDocument") is True


def test_a_search_result_in_this_view_selects_the_drawn_node(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    search = _load_search_index(page, len(explorer_consumer.records))
    page.evaluate("window.__consumerDocument=document")
    for query, node_id in (("Target theorem", "chapter/target"), ("Middle lemma", "chapter/middle")):
        search.fill(query)
        if node_id.endswith("target"):
            search.press("Enter")
        else:
            page.locator(f'.bp-dag-result[data-result-id="{node_id}"]').click()
        node = _consumer_selected(page, explorer_consumer, node_id)
        playwright.expect(page.locator(".bp-reading-body h1")).to_be_focused()
        _assert_hit(page, node)
        assert page.locator(".bp-dag-node").count() > 1
        assert page.url == explorer_consumer.route(node_id)
        assert page.evaluate("document===window.__consumerDocument") is True


def test_a_result_neither_drawn_here_nor_safely_linked_selects_nothing(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    def add_unlinked_record(route: object) -> None:
        index = route.fetch().json()
        index["nodes"].append({"id": "elsewhere/unlinked", "kind": "node", "status": "planned",
                               "title": "Unlinked result", "url": None, "route": None})
        route.fulfill(json=index)
    page.route("**/dependencies/index.json", add_unlinked_record)
    _open_consumer(page, explorer_consumer)
    search = _load_search_index(page, len(explorer_consumer.records) + 1)
    search.fill("Unlinked result")
    url = page.url
    source = page.locator(".bp-reading-body").inner_text()
    page.locator(".bp-dag-result", has_text="Unlinked result").click()
    playwright.expect(page.locator(".bp-dag-error")).to_contain_text("Could not")
    playwright.expect(page.locator(".bp-dag-has-selection")).to_have_count(0)
    assert page.url == url and page.locator(".bp-reading-body").inner_text() == source


def test_a_contents_query_lists_only_this_scope(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    search = _load_search_index(page, len(explorer_consumer.records))
    page.get_by_role("button", name="Results", exact=True).click()
    search.fill("theorem")
    playwright.expect(page.locator(".bp-dag-inventory-title")).to_have_text(["Target theorem"])
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("1 result · search the whole roadmap")
    results = page.locator(".bp-dag-search-results")
    playwright.expect(results).to_be_hidden()
    search.press("ArrowDown")
    playwright.expect(page.locator(".bp-dag-result").first).to_be_focused()
    page.keyboard.press("Escape")
    playwright.expect(results).to_be_hidden()
    page.get_by_role("button", name="search the whole roadmap").click()
    playwright.expect(results.locator('.bp-dag-result[data-result-id="remote/deep-theorem"]')).to_be_visible()


def test_global_search_jumps_to_the_smallest_context(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    page.evaluate("window.__consumerDocument=document")
    search = _load_search_index(page, len(explorer_consumer.records))
    search.fill("Remote.Namespace.deepTheorem")
    result = page.locator('.bp-dag-result[data-result-id="remote/deep-theorem"]')
    playwright.expect(result.locator(".bp-dag-result-title")).to_have_text("Remote theorem")
    expected = explorer_consumer.graphs["chapter"]["palette"][explorer_consumer.records["remote/deep-theorem"]["status"]]["label"]
    playwright.expect(result.locator(".bp-dag-result-meta")).to_have_text(expected)
    playwright.expect(result).not_to_contain_text("remote/deep-theorem")
    search.fill("spectral gap")
    playwright.expect(result).to_be_visible()
    result.click()
    _consumer_selected(page, explorer_consumer, "remote/deep-theorem")
    assert page.url == explorer_consumer.route("remote/deep-theorem")
    assert page.locator(".bp-dag-node").count() <= 8
    assert page.evaluate("document===window.__consumerDocument") is True


def test_legacy_node_hash_resolves_through_the_global_index(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _offline(page, explorer_consumer.url)
    page.goto(explorer_consumer.url + "/dependencies/chapters/chapter.html#node=remote%2Fdeep-theorem")
    _consumer_selected(page, explorer_consumer, "remote/deep-theorem")
    assert page.url == explorer_consumer.route("remote/deep-theorem")
    navigations: list[str] = []
    page.on("request", lambda request: navigations.append(request.url) if request.is_navigation_request() else None)
    page.goto(explorer_consumer.url + "/dependencies/chapters/chapter.html#node=unknown%2Fitem")
    _consumer_ready(page, explorer_consumer)
    playwright.expect(page.locator(".bp-dag-search")).to_have_attribute("placeholder", f"Search {len(explorer_consumer.records):,} roadmap entries")
    assert page.url.endswith("#node=unknown%2Fitem")
    assert len(navigations) == 1
    assert page.locator('.bp-dag-node[data-selected="true"]').count() == 0


def test_paginated_search_and_arrow_navigation_restore_focus(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer, "chapter/middle")
    foundation = _focus_consumer_node(page, "chapter/foundation")
    length, url = page.evaluate("history.length"), page.url
    stacked = page.locator(".bp-dag-stage").evaluate("stage => stage.clientWidth <= 620")
    foundation.press("ArrowDown" if stacked else "ArrowRight")
    playwright.expect(page.locator('.bp-dag-node[data-autoform-node-id="chapter/middle"]')).to_be_focused()
    assert page.evaluate("history.length") == length and page.url == url
    search = _load_search_index(page, len(explorer_consumer.records))
    search.fill("Auxiliary result")
    more = page.locator(".bp-dag-search-results .bp-dag-load-more")
    playwright.expect(more).to_have_text("Show 48 more of 48")
    more.focus()
    more.press("Enter")
    playwright.expect(page.locator(".bp-dag-result").nth(12)).to_be_focused()
    playwright.expect(page.locator(".bp-dag-result")).to_have_count(60)
    page.locator(".bp-dag-result").nth(12).press("ArrowUp")
    playwright.expect(page.locator(".bp-dag-result").nth(11)).to_be_focused()


def _release(page: object, held: list[object]) -> None:
    for _ in range(100):
        if held:
            held.pop(0).continue_()
            return
        page.evaluate("0")
    raise AssertionError("the held request never arrived")


def test_a_late_search_index_keeps_focus_and_leaves_a_closed_panel_closed(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    """A failed cold index may retry; only Navigation owns that fetch and the reader stays mounted."""
    page = desktop_page
    held: list[object] = []
    attempts = 0
    def late_index(route: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            route.fulfill(status=503, body="Deliberately unavailable initial index")
        else:
            held.append(route)
    page.route("**/dependencies/index.json", late_index)
    _open_consumer(page, explorer_consumer)
    search, results = page.locator(".bp-dag-search"), page.locator(".bp-dag-search-results")
    search.fill("Auxiliary result")
    more = results.locator(".bp-dag-load-more")
    more.focus()
    more.press("Enter")
    thirteenth = page.locator(".bp-dag-result").nth(12)
    playwright.expect(thirteenth).to_be_focused()
    selected = thirteenth.get_attribute("data-result-id")
    _release(page, held)
    playwright.expect(search).to_have_attribute("placeholder", f"Search {len(explorer_consumer.records):,} roadmap entries")
    playwright.expect(page.locator(f'.bp-dag-result[data-result-id="{selected}"]')).to_be_focused()
    page.get_by_role("button", name="Fit shown nodes", exact=True).focus()
    playwright.expect(results).to_be_hidden()

    attempts = 0
    page.reload()
    _consumer_ready(page, explorer_consumer)
    search.fill("Auxiliary result")
    search.press("Escape")
    playwright.expect(results).to_be_hidden()
    _release(page, held)
    playwright.expect(search).to_have_attribute("placeholder", f"Search {len(explorer_consumer.records):,} roadmap entries")
    playwright.expect(results).to_be_hidden()
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text("Dependency chapter")


def test_direct_list_view_has_current_shown_and_hidden_counts(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _offline(page, explorer_consumer.url)
    page.goto(explorer_consumer.url + "/dependencies/chapters/chapter.html#view=list&status=fully_proved")
    _consumer_ready(page, explorer_consumer)
    playwright.expect(page.locator(".bp-dag-inventory")).to_be_visible()
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("1 result")
    playwright.expect(page.locator(".bp-dag-hidden-button")).to_have_text("Show 62 hidden")
    playwright.expect(page.locator(".bp-dag-inventory-row")).to_have_count(1)


def test_hidden_items_without_edges_stay_hidden_after_a_reload(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    page.locator(".bp-dag-filter-summary").click()
    isolates = page.get_by_role("checkbox", name="Include items without edges")
    isolates.uncheck()
    before = page.url
    assert "#isolates=" not in before  # The canonical router stores opaque state, not a second hash writer.
    page.reload()
    _consumer_ready(page, explorer_consumer)
    playwright.expect(page.locator(".bp-dag-hidden-button")).to_have_text("Show 60 hidden")
    page.locator(".bp-dag-filter-summary").click()
    playwright.expect(isolates).not_to_be_checked()
    assert page.url == before


def test_reselect_does_not_duplicate_history_and_back_restores_selection(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    page.evaluate("window.__consumerDocument=document")
    initial = page.evaluate("history.length")
    _focus_consumer_node(page, "chapter/foundation").click()
    foundation = _consumer_selected(page, explorer_consumer, "chapter/foundation")
    assert page.evaluate("history.length") == initial + 1
    _assert_hit(page, foundation)
    foundation.click()
    _consumer_selected(page, explorer_consumer, "chapter/foundation")
    assert page.evaluate("history.length") == initial + 1
    _focus_consumer_node(page, "chapter/middle").click()
    _consumer_selected(page, explorer_consumer, "chapter/middle")
    assert page.evaluate("history.length") == initial + 2
    page.go_back()
    foundation = _consumer_selected(page, explorer_consumer, "chapter/foundation")
    foundation.focus()
    length = page.evaluate("history.length")
    stacked = page.locator(".bp-dag-stage").evaluate("stage => stage.clientWidth <= 620")
    foundation.press("ArrowDown" if stacked else "ArrowRight")
    playwright.expect(page.locator('.bp-dag-node[data-autoform-node-id="chapter/middle"]')).to_be_focused()
    assert page.evaluate("history.length") == length
    assert page.url == explorer_consumer.route("chapter/foundation")
    page.go_back()
    _consumer_ready(page, explorer_consumer)
    assert page.url == explorer_consumer.route("chapter")
    assert page.evaluate("document===window.__consumerDocument") is True


def test_hiding_the_selected_status_clears_selection(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer, "chapter/target")
    summary, popover = page.locator(".bp-dag-filter-summary"), page.locator(".bp-dag-filter-popover")
    summary.click()
    statuses = {node["status"] for node in explorer_consumer.records.values()}
    playwright.expect(popover.locator(".bp-dag-swatch")).to_have_count(len(statuses))
    popover.locator("input").first.press("Escape")
    playwright.expect(popover).to_be_hidden()
    playwright.expect(summary).to_be_focused()
    summary.click()
    page.mouse.click(2, 2)
    playwright.expect(popover).to_be_hidden()
    summary.click()
    target_status = explorer_consumer.records["chapter/target"]["status"]
    page.locator(f'.bp-dag-filter-popover input[value="{target_status}"]').uncheck()
    playwright.expect(page.locator(".bp-dag-has-selection")).to_have_count(0)
    assert page.url == explorer_consumer.route("chapter/target")
    playwright.expect(page.locator(".bp-reading-body")).to_contain_text("Final sentence of the complete target statement.")


def test_no_javascript_keeps_the_linked_fallback(webkit_browser: object, explorer_consumer: _ExplorerConsumer) -> None:
    with _page(webkit_browser, java_script_enabled=False) as page:
        _offline(page, explorer_consumer.url)
        page.goto(explorer_consumer.route("chapter/target"))
        source = page.locator(".bp-explorer-page > .bp-reading-source")
        playwright.expect(source).to_contain_text("Final sentence of the complete target statement.")
        playwright.expect(source.locator("h1")).to_have_text(explorer_consumer.heading("chapter/target"))
        card = source.locator('.bp-thmwrapper[data-autoform-node-id="chapter/target"]')
        playwright.expect(card.locator(".arithmatex")).to_have_count(1)
        fallback = page.locator(".bp-dag-fallback")
        playwright.expect(fallback).to_be_visible()
        download = fallback.get_by_role("link", name="Download the complete graph data")
        playwright.expect(download).to_be_visible()
        assert page.request.get(download.get_attribute("href") if download.get_attribute("href").startswith("http")
                                else urljoin(page.url, download.get_attribute("href"))).json()["schema"] == "autoform-dag-view/v2"
        reference = _open_dependency(card, "Middle lemma")
        assert urljoin(page.url, reference.get_attribute("href")) == explorer_consumer.route("chapter/middle")
        reference.click()
        assert page.url == explorer_consumer.route("chapter/middle")
        playwright.expect(page.locator(".bp-explorer-page > .bp-reading-source h1")).to_have_text(explorer_consumer.heading("chapter/middle"))


def test_fetch_failure_restores_the_linked_fallback(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    data_url = urljoin(explorer_consumer.url + "/dependencies/index.json", explorer_consumer.records["chapter"]["data_url"])
    page.route("**" + urlsplit(data_url).path, lambda route: route.fulfill(status=404, body="Deliberately missing graph"))
    _offline(page, explorer_consumer.url)
    page.goto(explorer_consumer.route("chapter"))
    playwright.expect(page.locator(".bp-dag-error")).to_contain_text("HTTP 404")
    source = page.locator(".bp-explorer-page > .bp-reading-source")
    playwright.expect(source.locator("h1")).to_have_text("Dependency chapter")
    reference = _open_dependency(source.locator('.bp-thmwrapper[data-autoform-node-id="chapter/target"]'), "Middle lemma")
    assert urljoin(page.url, reference.get_attribute("href")) == explorer_consumer.route("chapter/middle")
    playwright.expect(page.locator(".bp-dag-fallback")).to_be_visible()
    playwright.expect(page.locator(".bp-dag-fallback").get_by_role("link", name="Download the complete graph data")).to_be_visible()
    assert not page.locator("body").evaluate("body => body.classList.contains('bp-dag-app-page')")


def test_duplicate_script_include_mounts_one_explorer(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    def duplicate(route: object) -> None:
        body = route.fetch().text()
        body = body.replace("</body>", '<script defer src="/javascripts/blueprint-dag.js"></script></body>')
        route.fulfill(content_type="text/html", body=body)
    page.route("**" + urlsplit(explorer_consumer.route("chapter")).path, duplicate)
    _open_consumer(page, explorer_consumer)
    playwright.expect(page.locator(".bp-dag-head")).to_have_count(1)
    playwright.expect(page.locator(".bp-dag-stage")).to_have_count(1)
    playwright.expect(page.locator(".bp-reading-body > .bp-reading-source")).to_have_count(1)


def test_a_bare_graph_host_stays_a_linked_static_fallback(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _offline(page, explorer_consumer.url)
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(explorer_consumer.url + "/bare.html")
    fallback = page.locator(".bp-dag-fallback")
    playwright.expect(fallback).to_be_visible()
    playwright.expect(fallback.locator("li")).to_have_count(3)
    playwright.expect(page.locator(".bp-dag-stage")).to_have_count(0)
    download = fallback.get_by_role("link", name="Download the complete graph data")
    graph_url = urljoin(page.url, download.get_attribute("href"))
    assert graph_url not in requests
    assert page.request.get(graph_url).json()["schema"] == "autoform-dag-view/v2"
    fallback.get_by_role("link", name="Middle lemma", exact=True).click()
    _consumer_selected(page, explorer_consumer, "chapter/middle")


@pytest.mark.parametrize("viewport", ({"width": 390, "height": 844}, {"width": 844, "height": 390}))
def test_a_published_canvas_covers_exactly_its_stage(
    desktop_page: object, site_url: str, viewport: dict[str, int]
) -> None:
    """Site CSS once gave the canvas a 30rem floor, stretching drawings away from short stages' nodes."""
    desktop_page.set_viewport_size(viewport)
    page = _open_site(desktop_page, site_url, "dependencies.html")
    canvas = page.locator(".bp-dag-canvas").bounding_box()
    stage = page.locator(".bp-dag-stage").bounding_box()
    assert canvas is not None and stage is not None
    assert abs(canvas["height"] - stage["height"]) < 1
    assert abs(canvas["width"] - stage["width"]) < 1


def test_the_published_landing_page_is_one_full_page_explorer_opened_by_its_hero(
    desktop_page: object, site_url: str
) -> None:
    """The landing mathematics belongs to the same full-page reading/map shell, not a second hero surface."""
    page = _open_site(desktop_page, site_url, "index.html")
    box = page.locator(".bp-dag-viewer").bounding_box()
    assert box is not None and (box["width"], box["height"]) == (1280, 900)
    playwright.expect(page.locator(".md-header")).to_be_hidden()
    playwright.expect(page.get_by_role("heading", level=1)).to_have_count(1)
    playwright.expect(page.get_by_role("heading", level=1)).to_have_text("Explorer fixture")
    playwright.expect(page.locator(".bp-reading-body .bp-hero-title")).to_have_count(1)
    library = page.locator(".bp-dag-book")
    playwright.expect(library).to_have_attribute("href", _site_route(page, site_url, "roadmap"))
    _assert_hit(page, library)
    algebra = page.locator('.bp-dag-navigation-link[data-navigation-node-id="algebra"]')
    playwright.expect(algebra).to_have_attribute("href", _site_route(page, site_url, "algebra"))
    _assert_hit(page, algebra)
    # A static publication is not the dashboard's live-overlay service.
    page.route("**/__autoform/live.json", lambda route: route.fulfill(status=404, body=""))
    page.reload()
    _ready_site(page)
    playwright.expect(page.locator("#bp-live-activity")).to_have_count(0)


@pytest.mark.parametrize("width", (390, 1280))
def test_dashboard_live_activity_follows_the_integrated_landing_and_soft_navigation(
    desktop_page: object, site_url: str, width: int,
) -> None:
    """A genuine local response enhances the complete reader, not a retired second hero.

    Static 404 absence is tested above; this journey verifies the populated
    overlay survives reader replacement and Back without another document.
    """
    from autoform_cli.dashboard import LIVE_SCHEMA

    page = desktop_page
    page.set_viewport_size({"width": width, "height": 900})
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    page.route("**/__autoform/live.json", lambda route: route.fulfill(json={
        "schema": LIVE_SCHEMA, "source_revision": "fixture-local-overlay", "claims": [{
            "node_id": "algebra/result", "title": "Result", "owner": "reader-worker", "note": "Local proof attempt",
        }],
    }))
    _open_site(page, site_url, "index.html")
    page.evaluate("() => window.__liveActivityDocument = document")

    def assert_landing_panel() -> None:
        playwright.expect(page.locator("#bp-live-activity")).to_have_count(1)
        activity = page.locator(".bp-reading-body .bp-landing-intro > #bp-live-activity")
        playwright.expect(activity).to_have_count(1)
        playwright.expect(activity).to_contain_text("Live local work")
        playwright.expect(activity).to_contain_text("Result — reader-worker")
        playwright.expect(page.locator(".bp-reading-body .bp-hero-title")).to_have_count(1)
        playwright.expect(page.locator(".bp-dag-intro")).to_have_count(0)
        if width == 390:
            handle = page.get_by_role("button", name="Toggle mathematical reading", exact=True)
            if handle.get_attribute("aria-expanded") == "false":
                handle.click()
        playwright.expect(activity).to_be_visible()

    assert_landing_panel()
    algebra_route = _site_route(page, site_url, "algebra")
    for _ in range(2):
        if width == 390:
            outline = page.get_by_role("button", name="Toggle topic outline", exact=True)
            if outline.get_attribute("aria-expanded") == "false":
                outline.click()
        page.locator('.bp-dag-navigation-link[data-navigation-node-id="algebra"]').click()
        page.wait_for_url(algebra_route)
        if width == 390 and outline.get_attribute("aria-expanded") == "true":
            outline.click()
        playwright.expect(page.locator("#bp-live-activity")).to_have_count(0)
        playwright.expect(page.locator('.bp-reading-body [data-autoform-node-id="algebra/result"] .bp-live-badge')).to_have_count(1)
        page.evaluate("() => history.back()")
        page.wait_for_url(f"{site_url}/index.html")
        assert_landing_panel()
    assert documents == [f"{site_url}/index.html"]
    assert page.evaluate("() => document === __liveActivityDocument") is True


def test_on_a_phone_the_landing_hero_opens_in_the_details_sheet(mobile_page: object, site_url: str) -> None:
    """A collapsed phone reader remains named; opening it reveals the same authored landing content."""
    page = _open_site(mobile_page, site_url, "index.html")
    playwright.expect(page.locator(".bp-dag-navigation")).to_be_hidden()
    handle = page.get_by_role("button", name="Toggle mathematical reading", exact=True)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    headings = page.get_by_role("heading", level=1)
    playwright.expect(headings).to_have_count(1)
    playwright.expect(headings).to_have_text("Explorer fixture")
    inner = page.locator(".bp-reading-pane .bp-dag-inspector-inner")
    playwright.expect(inner).to_have_attribute("inert", "")
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    assert inner.get_attribute("inert") is None and inner.get_attribute("aria-hidden") is None
    playwright.expect(page.locator(".bp-reading-body .bp-hero-title")).to_be_visible()
    playwright.expect(headings).to_have_count(1)
    playwright.expect(headings).to_have_text("Explorer fixture")
    handle.click()
    playwright.expect(headings).to_have_count(1)
    playwright.expect(headings).to_have_text("Explorer fixture")


def test_on_a_phone_the_status_filter_and_search_results_open_in_view(mobile_page: object, site_url: str) -> None:
    """Toolbar popovers must remain on-screen and clickable over the phone's full reading pane."""
    page = mobile_page
    def lengthen_the_index(route: object) -> None:
        index = route.fetch().json()
        index["nodes"] += [
            {"id": f"more/result-{n}", "kind": "node", "status": "planned", "result": True, "title": f"Result {n}", "url": None}
            for n in range(20)
        ]
        route.fulfill(json=index)
    # The shared router loads its catalog before its first graph; install the fixture before opening.
    page.route("**/dependencies/index.json", lengthen_the_index)
    _open_site(page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    playwright.expect(page.get_by_role("button", name="Toggle mathematical reading", exact=True)).to_have_attribute(
        "aria-expanded", "true"
    )
    page.locator(".bp-dag-filter-summary").tap()
    popover = page.locator(".bp-dag-filter-popover")
    playwright.expect(popover).to_be_visible()
    box, viewport = popover.bounding_box(), page.viewport_size
    assert box is not None and viewport is not None
    assert 0 <= box["x"] and box["x"] + box["width"] <= viewport["width"]
    assert 0 <= box["y"] and box["y"] + box["height"] <= viewport["height"]
    _assert_hit(page, popover.locator("input").first)
    search = _load_search_index(page, 27)
    search.fill("Result")
    assert _paints_over_the_details(page, ".bp-dag-result", ".bp-dag-search-results") is True


@pytest.mark.parametrize("width", (800, 1100, 1101, 1440))
def test_a_node_chosen_on_a_wider_screen_lands_beside_the_open_details(
    desktop_page: object, site_url: str, width: int
) -> None:
    """The graph, explicit activation and full reader agree; keyboard roving alone does not replace mathematics."""
    page = desktop_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    _open_site(page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    def assert_beside_reading(node: object) -> None:
        playwright.expect(node).to_have_attribute("data-selected", "true")
        _paint_frame(page)
        box, reader, stage = (
            item.bounding_box() for item in (node, page.locator(".bp-reading-pane"), page.locator(".bp-dag-stage"))
        )
        assert box is not None and reader is not None and stage is not None
        assert box["x"] + box["width"] <= reader["x"] or reader["x"] + reader["width"] <= box["x"]
        assert stage["x"] <= box["x"] and box["x"] + box["width"] <= stage["x"] + stage["width"]
        _assert_hit(page, node)
    result = page.locator('[data-autoform-node-id="algebra/result"].bp-dag-node')
    assert_beside_reading(result)
    _reading_card(page, "algebra/result")
    result.focus()
    stacked = page.locator(".bp-dag-stage").evaluate("stage => stage.clientWidth <= 620")
    result.press("ArrowUp" if stacked else "ArrowLeft")
    base = page.locator('[data-autoform-node-id="algebra/base"].bp-dag-node')
    playwright.expect(base).to_be_focused()
    playwright.expect(result).to_have_attribute("data-selected", "true")
    assert page.url == _site_route(page, site_url, "algebra/result")
    base.press("Enter")
    page.wait_for_url(_site_route(page, site_url, "algebra/base"))
    assert_beside_reading(base)
    _reading_card(page, "algebra/base")


def test_the_rail_under_the_open_details_leaves_the_tab_order(desktop_page: object, site_url: str) -> None:
    """An explicitly closed narrow outline is absent from Tab; an open outline and wide rail remain usable."""
    page = desktop_page
    page.set_viewport_size({"width": 1000, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    _open_site(page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    outline = page.locator(".bp-dag-navigation")
    playwright.expect(outline).to_be_hidden()
    page.get_by_role("button", name="Fit shown nodes").focus()
    for _ in range(8):
        page.keyboard.press("Tab")
        assert not outline.evaluate("rail => rail.contains(document.activeElement)")
    toggle = page.get_by_role("button", name="Toggle topic outline", exact=True)
    toggle.click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "true")
    link = outline.locator('.bp-dag-navigation-link[data-navigation-node-id="algebra"]')
    _assert_hit(page, link)
    link.focus()
    playwright.expect(link).to_be_focused()
    toggle.click()
    playwright.expect(outline).to_be_hidden()
    page.set_viewport_size({"width": 1440, "height": 900})
    playwright.expect(outline).to_be_visible()
    link.focus()
    playwright.expect(link).to_be_focused()
    assert page.locator(".bp-reading-pane .bp-dag-inspector-inner").get_attribute("inert") is None


@pytest.mark.parametrize(
    ("path", "title"),
    (
        ("dependencies/chapters/algebra.html#node=algebra%2Fresult", "Algebra"),
        ("index.html#node=scope%3Aalgebra", "Algebra"),
    ),
)
def test_the_rail_under_the_open_details_keeps_the_page_heading(
    desktop_page: object, site_url: str, path: str, title: str
) -> None:
    """The reading page owns one authored H1 even when the topic outline is closed."""
    page = desktop_page
    page.set_viewport_size({"width": 1000, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    _open_site(page, site_url, path)
    playwright.expect(page.locator(".bp-reading-pane")).to_be_visible()
    playwright.expect(page.locator(".bp-dag-navigation")).to_be_hidden()
    headings = page.get_by_role("heading", level=1)
    playwright.expect(headings).to_have_count(1)
    playwright.expect(headings).to_have_text(title)
    playwright.expect(headings).to_have_css("opacity", "1")
    assert page.locator(".bp-reading-pane .bp-dag-inspector-inner").get_attribute("inert") is None


def test_contents_start_right_of_the_open_details(desktop_page: object, site_url: str) -> None:
    """Results occupy their own pane without clipping the selected marker or lying beneath the reader."""
    page = desktop_page
    page.set_viewport_size({"width": 1000, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    _offline(page, site_url)
    page.goto(f"{site_url}/dependencies/chapters/algebra.html#node=algebra%2Fresult&view=list")
    inventory = page.locator(".bp-dag-inventory")
    playwright.expect(inventory).to_be_visible()
    row = inventory.locator('.bp-dag-inventory-row[aria-current="true"]')
    playwright.expect(row).to_contain_text("Result")
    playwright.expect(inventory.locator(".bp-dag-inventory-title")).to_have_text(["Base", "Result"])
    playwright.expect(inventory.locator(".bp-dag-inventory-summary")).to_have_text("2 results")
    reader = page.locator(".bp-reading-pane").bounding_box()
    assert reader is not None
    for item in (inventory.locator(".bp-dag-inventory-head h2"), row, row.locator(".bp-dag-inventory-title")):
        box = item.bounding_box()
        assert box is not None and box["x"] + box["width"] <= reader["x"] + 1
    _assert_hit(page, row)
    page.get_by_role("button", name="Map", exact=True).click()
    playwright.expect(inventory).to_be_hidden()
    _reading_card(page, "algebra/result")
    playwright.expect(page.locator(".bp-reading-pane")).to_be_visible()


def _paints_over_the_details(page: object, items: str, container: str) -> bool | None:
    """Whether the first of *items* shown in *container* over the open details is what a click there would hit.

    None means that no such item lies over the details.
    """
    return page.evaluate(
        """([items, container]) => {
          const drawer = document.querySelector(".bp-dag-inspector").getBoundingClientRect();
          const shown = document.querySelector(container).getBoundingClientRect();
          for (const item of document.querySelectorAll(items)) {
            const box = item.getBoundingClientRect();
            // The reading pane is now on the right on desktop. Probe an actual shared visible area,
            // not only the row's centre, which can be left of it while its right half overlaps.
            const left = Math.max(box.left, drawer.left, shown.left, 0);
            const right = Math.min(box.right, drawer.right, shown.right, innerWidth);
            const top = Math.max(box.top, drawer.top, shown.top, 0);
            const bottom = Math.min(box.bottom, drawer.bottom, shown.bottom, innerHeight);
            if (right - left > 2 && bottom - top > 2)
              return item.contains(document.elementFromPoint((left + right) / 2, (top + bottom) / 2));
          }
          return null;
        }""",
        [items, container],
    )


def test_the_toolbars_popovers_paint_over_the_open_details(desktop_page: object, site_url: str) -> None:
    """Real overlapping popup areas must win hit-testing over both phone and wider reading panes."""
    page = desktop_page
    page.emulate_media(reduced_motion="reduce")
    palette: dict[str, object] = {}
    def every_status(route: object) -> None:
        payload = route.fetch().json()
        palette.update(payload["palette"])
        payload["nodes"] += [
            {"id": f"more/{key}", "kind": "node", "status": key, "status_counts": [[key, 1]], "title": f"More {key}"}
            for key in payload["palette"]
        ]
        route.fulfill(json=payload)
    def lengthen_the_index(route: object) -> None:
        index = route.fetch().json()
        # The published palette is authoritative; read it before the router's first request.
        index["nodes"] += [
            {"id": f"more/{key}", "kind": "node", "status": key, "result": True, "title": f"More {key}", "url": None}
            for key in palette
        ]
        index["nodes"] += [
            {"id": f"more/result-{n}", "kind": "node", "status": "planned", "result": True, "title": f"Result {n}", "url": None}
            for n in range(20)
        ]
        route.fulfill(json=index)
    payload = page.request.get(f"{site_url}/dependencies/chapters/algebra.json", timeout=10_000)
    assert payload.ok
    palette.update(payload.json()["palette"])
    page.route("**/algebra.json", every_status)
    page.route("**/dependencies/index.json", lengthen_the_index)
    page.set_viewport_size({"width": 390, "height": 844})
    _open_site(page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    _reading_card(page, "algebra/result")
    page.locator(".bp-dag-filter-summary").click()
    playwright.expect(page.locator(".bp-dag-filter-popover")).to_be_visible()
    assert _paints_over_the_details(page, ".bp-dag-check", ".bp-dag-filter-popover") is True
    page.locator(".bp-dag-filter-summary").click()
    search = _load_search_index(page, 27 + len(palette))
    for width in (1000, 800):
        page.set_viewport_size({"width": width, "height": 900})
        search.fill("")
        search.fill("Result")
        playwright.expect(page.locator(".bp-dag-search-results")).to_be_visible()
        assert _paints_over_the_details(page, ".bp-dag-result", ".bp-dag-search-results") is True


def test_a_short_phones_explorer_keeps_its_sheet_handle_on_screen(mobile_page: object, site_url: str) -> None:
    """A 28rem floor, 560px under Material's root font, pushed the sheet's handle below a short window."""
    page = mobile_page
    page.set_viewport_size({"width": 390, "height": 480})
    _open_site(page, site_url, "dependencies/chapters/algebra.html")
    viewer = page.locator(".bp-dag-viewer").bounding_box()
    handle = page.locator(".bp-dag-sheet-handle").bounding_box()
    assert viewer is not None and handle is not None
    assert viewer["height"] <= 480
    assert 0 <= handle["y"] and handle["y"] + handle["height"] <= 480


def test_a_phones_chapter_explorer_gives_its_breadcrumb_a_line_of_its_own(mobile_page: object, site_url: str) -> None:
    """The textbook link and kind pill shared the crumbs' line, cutting the chapter's name and Book's target short."""
    page = _open_site(mobile_page, site_url, "dependencies/chapters/algebra.html")
    current = page.locator('.bp-dag-breadcrumb [aria-current="page"]')
    playwright.expect(current).to_have_text("Algebra")
    assert current.evaluate("element => element.scrollWidth <= element.clientWidth")
    book = page.locator(".bp-dag-breadcrumb li").first.bounding_box()
    assert book is not None and book["width"] >= 24


def test_the_explorer_draws_its_breadcrumb_once(desktop_page: object, site_url: str) -> None:
    """One persistent canonical trail replaces the former duplicate header and rail trails."""
    page = _open_site(desktop_page, site_url, "dependencies/chapters/algebra.html")
    crumbs = page.locator(".bp-dag-breadcrumb")
    playwright.expect(crumbs).to_have_count(1)
    playwright.expect(crumbs).to_be_visible()
    playwright.expect(crumbs.locator('[aria-current="page"]')).to_have_text("Algebra")
    playwright.expect(page.locator(".bp-dag-navigation-path")).to_have_count(0)
    library = page.locator(".bp-dag-book")
    _assert_hit(page, library)
    for width in (1000, 390, 1280):
        page.set_viewport_size({"width": width, "height": 900 if width != 390 else 844})
        playwright.expect(crumbs).to_be_visible()
        playwright.expect(crumbs.locator('[aria-current="page"]')).to_have_text("Algebra")
        playwright.expect(page.get_by_role("heading", level=1)).to_have_count(1)
    _open_site(page, site_url, "index.html")
    playwright.expect(crumbs).to_have_count(1)
    playwright.expect(crumbs).to_be_visible()


def test_the_published_atlas_hides_the_isolate_toggle(desktop_page: object, site_url: str) -> None:
    page = _open_site(desktop_page, site_url, "dependencies.html")
    # This atlas has only isolated topics; hiding them all is not a meaningful view filter.
    page.locator(".bp-dag-filter-summary").click()
    playwright.expect(page.locator(".bp-dag-filter-popover")).to_be_visible()
    playwright.expect(page.get_by_role("group", name="Edges", exact=True)).to_be_hidden()


def test_published_explorer_controls_keep_their_own_styles_under_material(
    desktop_page: object, site_url: str
) -> None:
    """Controls and reading typography retain their own roles beneath Material's generic typeset rules."""
    page = _open_site(desktop_page, site_url, "dependencies/chapters/algebra.html")
    summary = page.locator(".bp-dag-filter-summary")
    assert summary.evaluate(
        "element => {const s=getComputedStyle(element);return [element.tagName,s.display,s.borderLeftWidth,s.borderRightWidth];}"
    ) == ["BUTTON", "flex", "1px", "1px"]
    page.locator(".bp-dag-search").fill("Result")
    results = page.locator(".bp-dag-results")
    results.wait_for()
    assert results.evaluate("element => getComputedStyle(element).listStyleType") == "none"
    assert results.locator("li").first.evaluate("element => getComputedStyle(element).marginLeft") == "0px"
    rail = page.locator(".bp-dag-navigation-list")
    assert rail.evaluate("element => getComputedStyle(element).listStyleType") == "none"
    assert rail.evaluate("element => getComputedStyle(element).display") == "grid"
    assert rail.locator("li").first.evaluate("element => getComputedStyle(element).marginLeft") == "0px"
    heading = page.locator(".bp-reading-body h1")
    playwright.expect(heading).to_have_css("font-weight", "700")
    assert heading.evaluate("element => parseFloat(getComputedStyle(element).fontSize)") >= 22
    page.set_viewport_size({"width": 390, "height": 844})
    crumbs = page.locator(".bp-dag-breadcrumb li")
    assert crumbs.count() >= 2
    assert len({round(crumbs.nth(index).bounding_box()["y"]) for index in range(crumbs.count())}) == 1
    assert page.locator(".bp-dag-head").evaluate("head => head.scrollWidth <= head.clientWidth + 1")


def _assert_site_chrome_is_reachable(page: object) -> None:
    """The header, heading and navigation are visible, and each is the element a click would hit."""
    for chrome in (
        page.locator(".md-header"),
        page.get_by_role("heading", level=1),
        page.get_by_role("navigation", name="Blueprint chapters", exact=True).get_by_role(
            "link", name="Next Topology", exact=True
        ),
    ):
        playwright.expect(chrome).to_be_visible()
        chrome.click(trial=True, timeout=5_000)


def test_a_published_app_page_without_script_keeps_the_site_chrome(webkit_browser: object, site_url: str) -> None:
    """Canonical complete mathematics and native book links remain usable without enhancement."""
    with _page(webkit_browser, java_script_enabled=False) as page:
        _offline(page, site_url)
        page.goto(_site_route(page, site_url, "algebra"))
        loading = page.locator(".bp-dag-loading")
        playwright.expect(loading).to_be_attached()
        playwright.expect(loading).to_be_hidden()
        # WebKit's no-script trial click can follow the native Next link; audit this page before it.
        playwright.expect(page.locator('.bp-reading-source .bp-thmwrapper[data-autoform-node-id="algebra/result"]')).to_contain_text(
            "The main result."
        )
        _assert_site_chrome_is_reachable(page)
        playwright.expect(page.locator(".bp-dag-integrated")).to_have_count(0)
        # A legacy entrance is still a usable native notice, not a blank graph without JavaScript.
        page.goto(f"{site_url}/dependencies/chapters/algebra.html")
        notice = page.locator(".bp-reading-source")
        playwright.expect(notice).to_be_visible()
        link = notice.get_by_role("link").first
        href = link.get_attribute("href")
        assert isinstance(href, str)
        assert urljoin(page.url, href) == _site_route(page, site_url, "algebra")
        link.click(trial=True)


def test_a_published_app_page_restores_the_site_chrome_when_its_data_fails(
    desktop_page: object, site_url: str
) -> None:
    page = _offline(desktop_page, site_url)
    canonical = _site_route(page, site_url, "algebra")
    page.route("**/algebra.json", lambda route: route.fulfill(status=404, body=""))
    page.goto(canonical)
    playwright.expect(page.locator(".bp-dag-error")).to_contain_text("HTTP 404")
    _assert_site_chrome_is_reachable(page)
    playwright.expect(page.locator('.bp-reading-source .bp-thmwrapper[data-autoform-node-id="algebra/result"]')).to_contain_text(
        "The main result."
    )
    assert "bp-dag-app-page" not in (page.locator("body").get_attribute("class") or "")


def test_a_published_chapter_explorer_links_its_textbook_chapter_and_keeps_its_heading(
    desktop_page: object, site_url: str
) -> None:
    """The canonical mathematical page is already the reader; Library is navigation, not a second self-link."""
    page = _open_site(desktop_page, site_url, "dependencies/chapters/algebra.html")
    assert page.url == _site_route(page, site_url, "algebra")
    library = page.locator(".bp-dag-book")
    playwright.expect(library).to_have_attribute("href", _site_route(page, site_url, "roadmap"))
    playwright.expect(library).to_have_text("Library")
    _assert_hit(page, library)
    playwright.expect(page.get_by_role("heading", level=1)).to_have_count(1)
    playwright.expect(page.get_by_role("heading", level=1)).to_have_text("Algebra")
    _reading_card(page, "algebra/base")
    playwright.expect(_reading_card(page, "algebra/result")).to_contain_text("The main result.")


def test_slash_on_the_published_landing_page_opens_the_explorer_search(desktop_page: object, site_url: str) -> None:
    """The landing page is the full-page explorer, so "/" opens its search, as on every explorer page."""
    page = _open_site(desktop_page, site_url, "index.html")
    page.keyboard.press("/")
    playwright.expect(page.locator(".bp-dag-search")).to_be_focused()


def test_slash_inside_the_explorer_opens_its_search_but_an_input_keeps_its_keys(
    desktop_page: object, explorer_consumer: _ExplorerConsumer
) -> None:
    """Focus inside the explorer lets "/" open its search, but no shortcut takes a key from an input."""
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    search, wrap = page.locator(".bp-dag-search"), page.locator(".bp-dag-search-wrap")
    playwright.expect(search).to_have_attribute("placeholder", f"Search {len(explorer_consumer.records):,} roadmap entries")
    # The box draws a masked magnifier and a "/" key hint, which focus hides.
    assert "svg" in wrap.evaluate("element => getComputedStyle(element, '::before').webkitMaskImage")
    hint = "element => [getComputedStyle(element, '::after').content, getComputedStyle(element, '::after').display]"
    assert wrap.evaluate(hint) == ['"/"', "block"]
    page.locator('.bp-dag-node[tabindex="0"]').focus()
    page.keyboard.press("/")
    playwright.expect(search).to_be_focused()
    assert wrap.evaluate(hint)[1] == "none"
    page.locator(".bp-dag-filter-summary").click()
    isolates = page.get_by_role("checkbox", name="Include items without edges")
    isolates.focus()
    page.keyboard.press("Control+K")
    playwright.expect(isolates).to_be_focused()


def test_the_explorer_typesets_the_math_it_draws_in_its_rail_and_details(
    desktop_page: object, marked_math_site_url: str, marked_math_runtime: object
) -> None:
    """Actual marked reader math survives navigation and more-than-50 authored relations with real readback."""
    from tests.test_integrated_explorer_browser import _math_source, _routes

    page, site_url = desktop_page, marked_math_site_url
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _routes(page, site_url, marked_math_runtime, graph_client=True)
    first = _site_route(page, site_url, "algebra/result")
    page.goto(first)
    _ready_site(page)
    page.evaluate("() => window.__mathReaderDocument = document")
    def full_math(node_id: str, count: int) -> object:
        assert page.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-reading-body'))") is True
        card = _reading_card(page, node_id)
        playwright.expect(card.locator(".arithmatex")).to_have_count(count)
        playwright.expect(card.locator("mjx-container")).to_have_count(count)
        playwright.expect(card.locator("mjx-merror, merror")).to_have_count(0)
        assert page.locator("[data-autoform-math-stage]").count() == 0
        return card
    card = full_math("algebra/result", 5)
    assert _math_source(page, card.locator("mjx-container").first) == r"L\neq\{0\}"
    assert "Final sentence of the complete statement." in card.inner_text()
    search = page.locator(".bp-dag-search")
    search.fill("topology/result")
    remote = page.locator('.bp-dag-result[data-result-id="topology/result"]')
    playwright.expect(remote).to_be_visible()
    remote.click()
    page.wait_for_url(_site_route(page, site_url, "topology/result"))
    full_math("topology/result", 5)
    related = page.locator(".bp-reading-neighbors").get_by_role("button", name="Show all 55", exact=True)
    playwright.expect(related).to_be_visible()
    related.click()
    inventory = page.locator(".bp-dag-inventory")
    playwright.expect(inventory).to_be_visible()
    playwright.expect(inventory.locator(".bp-dag-inventory-row")).to_have_count(55)
    last = inventory.locator(".bp-dag-inventory-row", has=page.get_by_text("Later 54", exact=True))
    last.scroll_into_view_if_needed()
    _assert_hit(page, last)
    last.click()
    page.wait_for_url(_site_route(page, site_url, "topology/later-54"))
    last_card = full_math("topology/later-54", 1)
    assert _math_source(page, last_card.locator("mjx-container").first) == "0=0"
    assert page.evaluate("() => document === __mathReaderDocument")
    # The chapter-card fragment selects the initial statement in the client;
    # HTTP document requests never include URL fragments.
    assert documents == [urlsplit(first)._replace(fragment="").geturl()]


def _paint_frame(page: object) -> None:
    page.evaluate("() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")



def _canvas_pixels(page: object) -> dict[str, object]:
    return page.locator(".bp-dag-canvas").evaluate(
        """canvas => {
          const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
          let painted = 0;
          for (let index = 3; index < pixels.length; index += 4) if (pixels[index]) painted++;
          return {width: canvas.width, height: canvas.height, painted};
        }"""
    )



def _assert_sheet_state(page: object, *, selected: str | None, opened: bool) -> None:
    _paint_frame(page)
    state = page.locator(".bp-dag-viewer").evaluate(
        "host => ({selected: host.classList.contains('bp-dag-has-selection'), "
        "opened: host.classList.contains('bp-dag-sheet-open')})"
    )
    assert state == {"selected": selected is not None, "opened": opened}
    playwright.expect(page.locator(".bp-dag-sheet-handle")).to_have_attribute("aria-expanded", str(opened).lower())
    inner = page.locator(".bp-dag-inspector-inner")
    assert inner.get_attribute("inert") == (None if opened else "")
    assert inner.get_attribute("aria-hidden") == (None if opened else "true")
    marked = page.locator('.bp-dag-node[data-selected="true"]')
    playwright.expect(marked).to_have_count(0 if selected is None else 1)
    if selected is not None:
        playwright.expect(marked).to_have_attribute("data-autoform-node-id", selected)



@pytest.fixture(scope="module")
def count_label_site_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real published route count from twelve authored, unverified dependency links."""
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("canvas-count-consumer") / "project"
    scaffold_project(project, title="Canvas dependency count consumer")
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text(
        "# Canvas dependency count consumer\n\n- [Algebra](algebra/README.md)\n- [Topology](topology/README.md)\n",
        encoding="utf-8",
    )
    algebra, topology = roadmap / "algebra", roadmap / "topology"
    algebra.mkdir()
    topology.mkdir()
    (algebra / "README.md").write_text("# Algebra\n\n- [Base statement](base.md)\n", encoding="utf-8")
    (algebra / "base.md").write_text(
        "---\ndeclaration: theorem\nnot_ready: true\n---\n\n# Base statement\n\n"
        "This proposed zero identity is an unverified target.\n\n## Depends on\n\nNo prerequisites.\n",
        encoding="utf-8",
    )
    (topology / "README.md").write_text(
        "# Topology\n\n" + "\n".join(f"- [Planned statement {number}](result-{number}.md)" for number in range(12)) + "\n",
        encoding="utf-8",
    )
    for number in range(12):
        (topology / f"result-{number}.md").write_text(
            f"---\ndeclaration: theorem\nnot_ready: true\n---\n\n# Planned statement {number}\n\n"
            "This fixture records an unverified target, not a Lean proof.\n\n"
            "## Depends on\n\n- [Base statement](../algebra/base.md)\n",
            encoding="utf-8",
        )
    render_site(project / "blueprint", project / "site-src")
    payload = json.loads((project / "site-src/dependencies.json").read_text(encoding="utf-8"))
    assert len(payload["edges"]) == 1 and payload["edges"][0]["statement"] == 12 and payload["edges"][0]["proof"] == 0
    build(load_config(config_file=str(project / "mkdocs.yml"), strict=True))
    with _served(project / "site") as url:
        yield url



def _observe_canvas_typography(page: object, *, reject_family: bool) -> None:
    page.add_init_script(
        """
        const rejectFamily = __REJECT_FAMILY__;
        window.labelPaints = [];
        window.fontProbes = [];
        const prototype = CanvasRenderingContext2D.prototype;
        const paint = prototype.fillText;
        const font = Object.getOwnPropertyDescriptor(prototype, 'font');
        Object.defineProperty(prototype, 'font', {
          configurable: font.configurable, enumerable: font.enumerable, get: font.get,
          set(value) {
            const graph=this.canvas.classList.contains('bp-dag-canvas');
            const rejected=graph && rejectFamily && String(value).includes('Plus Jakarta Sans');
            if (!rejected) font.set.call(this, value);
            if (graph) window.fontProbes.push({requested:String(value),actual:font.get.call(this),rejected});
          }
        });
        if (rejectFamily) {
          const context = HTMLCanvasElement.prototype.getContext;
          HTMLCanvasElement.prototype.getContext = function (...args) {
            const result = context.apply(this, args);
            if (args[0] === '2d' && result && this.classList.contains('bp-dag-canvas'))
              font.set.call(result, '11px serif');
            return result;
          };
        }
        prototype.fillText = function (text, ...args) {
          if (this.canvas.classList.contains('bp-dag-canvas'))
            window.labelPaints.push({text: String(text), font: this.font, width: this.measureText(text).width});
          return paint.call(this, text, ...args);
        };
        """.replace("__REJECT_FAMILY__", str(reject_family).lower())
    )


def _live_card_typography(page: object) -> dict:
    """Measure actual published DOM text, not a retired numeric Canvas label."""
    title = page.locator(".bp-dag-node-title").first
    playwright.expect(title).to_be_visible()
    return title.evaluate("""node => {
      const style=getComputedStyle(node),control=node.closest('.bp-dag-node');
      const scale=new DOMMatrix(getComputedStyle(control).transform).a;
      const range=document.createRange();range.selectNodeContents(node);
      const text=node.textContent, pixels=parseFloat(style.fontSize),family=style.fontFamily;
      const ctx=document.createElement('canvas').getContext('2d');
      ctx.font=style.fontWeight+' '+pixels+'px '+family;
      return {text,family,pixels,scale,width:range.getBoundingClientRect().width/scale,
        expected:ctx.measureText(text).width};
    }""")



def test_widening_with_the_book_focused_resizes_and_repaints_the_live_canvas(
    desktop_page: object, site_url: str
) -> None:
    page = desktop_page
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": 1000, "height": 800})
    _open_site(page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    _paint_frame(page)
    book = page.locator(".bp-dag-book").element_handle()
    book.focus()
    before = _canvas_pixels(page)
    assert before["painted"] > 0
    page.set_viewport_size({"width": 1280, "height": 900})
    playwright.expect(page.locator(".bp-dag-context > .bp-dag-book")).to_have_count(1)
    _paint_frame(page)
    assert book.evaluate("node => node === document.activeElement && node === document.querySelector('.bp-dag-book')")
    after = _canvas_pixels(page)
    assert after["width"] != before["width"] and after["height"] != before["height"]
    assert after["painted"] > 0, "Resizing cleared the canvas without painting the graph again"
    stage = page.locator(".bp-dag-stage").bounding_box()
    ratio = page.evaluate("Math.min(2, devicePixelRatio || 1)")
    assert stage is not None
    assert abs(after["width"] - stage["width"] * ratio) <= 1
    assert abs(after["height"] - stage["height"] * ratio) <= 1



@pytest.mark.parametrize("reject_family", (False, True), ids=("multiword-site-font", "rejected-site-font"))
def test_dom_cards_keep_the_site_font_when_the_canvas_probe_accepts_or_rejects_it(
    desktop_page: object, count_label_site_url: str, reject_family: bool
) -> None:
    page, site_url = desktop_page, count_label_site_url
    _observe_canvas_typography(page, reject_family=reject_family)
    canonical = _site_route(page, site_url, "roadmap")
    if not reject_family:
        def serif_host_before_startup(route: object) -> None:
            body = route.fetch().text()
            marker = 'class="bp-dag-viewer"'
            assert body.count(marker) == 1
            body = body.replace(
                marker,
                marker + ' style="font-family: &quot;Times New Roman&quot;, serif !important;"',
                1,
            )
            route.fulfill(content_type="text/html", body=body)

        # The family must be present on the host before the runtime's probe;
        # Material can override an inherited root custom property later.
        page.route(canonical, serif_host_before_startup)
    _open_site(page, site_url, urlsplit(canonical).path.lstrip("/"))
    _paint_frame(page)
    typography = _live_card_typography(page)
    family = page.locator(".bp-dag-viewer").evaluate("host => getComputedStyle(host).fontFamily")
    assert ("Plus Jakarta Sans" if reject_family else "Times New Roman") in family
    assert ("Plus Jakarta Sans" if reject_family else "Times New Roman") in typography["family"]
    assert typography["pixels"] * typography["scale"] >= 11
    assert abs(typography["width"] - typography["expected"]) < .75
    probes = page.evaluate("window.fontProbes")
    candidates = [probe for probe in probes if probe["requested"].startswith("11px ") and family in probe["requested"]]
    assert candidates
    if reject_family:
        assert all(probe["rejected"] and probe["actual"] == "10px sans-serif" for probe in candidates)
    else:
        assert all(not probe["rejected"] and "Times New Roman" in probe["actual"] for probe in candidates)
    assert page.evaluate("window.labelPaints.some(paint => paint.text === '×12')") is False
    assert _canvas_pixels(page)["painted"] > 0



def test_a_deep_long_rail_trail_keeps_its_full_accessible_names(
    desktop_page: object, site_url: str
) -> None:
    """Logical ancestors come from authored catalog titles; elision must retain their full accessible names."""
    page = desktop_page
    labels = (
        "Collected mathematical foundations and supporting results",
        "Topological vector spaces and infinite-dimensional functional analysis",
        "The complete mathematical statement and prerequisite structure of the current result",
    )
    canonical = _site_route(page, site_url, "algebra")
    def long_titles(route: object) -> None:
        index = route.fetch().json()
        titles = dict(zip(("roadmap", "algebra", "algebra/result"), labels))
        for node in index["nodes"]:
            if node["id"] in titles:
                node["title"] = titles[node["id"]]
        route.fulfill(json=index)
    def long_authored_heading(route: object) -> None:
        body = route.fetch().text()
        body, replaced = re.subn(r'(<h1\b[^>]*>)Algebra(?=<)', lambda match: match[1] + labels[1], body, count=1)
        assert replaced == 1
        body = body.replace('data-page-title="Algebra"', 'data-page-title="' + labels[1] + '"')
        route.fulfill(content_type="text/html", body=body)
    page.route("**/dependencies/index.json", long_titles)
    page.route("**" + urlsplit(canonical).path, long_authored_heading)
    _open_site(page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    crumbs = page.locator(".bp-dag-breadcrumb li")
    playwright.expect(crumbs).to_have_count(3)
    for index, label in enumerate(labels):
        item = crumbs.nth(index)
        playwright.expect(item).to_have_text(label)
        assert item.evaluate(
            "item => item.scrollWidth <= item.clientWidth + 1 || item.title === item.textContent || item.querySelector('a')?.title === item.textContent"
        ), "An elided mathematical ancestor has no full tooltip"
    link = page.locator('.bp-dag-navigation-link[data-navigation-node-id="algebra"]')
    playwright.expect(link).to_have_text(labels[1])
    playwright.expect(link).to_have_attribute("title", labels[1])
    rail_box, box = page.locator(".bp-dag-navigation").bounding_box(), link.bounding_box()
    assert rail_box is not None and box is not None
    assert box["x"] >= rail_box["x"] and box["x"] + box["width"] <= rail_box["x"] + rail_box["width"] + 1
    heading = page.locator(".bp-reading-body h1")
    playwright.expect(heading).to_have_text(labels[1])
    assert heading.evaluate("node => node.scrollWidth <= node.clientWidth + 1")



@pytest.mark.parametrize("width", (390, 1000, 1280))
def test_sheet_selection_close_clear_filter_and_history_states_stay_consistent(
    desktop_page: object, explorer_consumer: _ExplorerConsumer, width: int,
) -> None:
    """Keep atomic close/clear/filter behavior after genuine route commits."""
    page = desktop_page
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": width, "height": 900})
    _open_consumer(page, explorer_consumer, "chapter/middle")
    page.evaluate("window.__consumerDocument=document")
    middle = _consumer_selected(page, explorer_consumer, "chapter/middle")
    _assert_sheet_state(page, selected="chapter/middle", opened=True)
    handle = page.locator(".bp-dag-sheet-handle")
    if width == 390:
        handle.click()
        _assert_sheet_state(page, selected="chapter/middle", opened=False)
        handle.click()
        _assert_sheet_state(page, selected="chapter/middle", opened=True)
    # Route navigation may legitimately change the scene. Clearing the already
    # committed selection must not introduce the reviewed 1000px vertical jump.
    middle.focus()
    original = middle.bounding_box()
    assert original is not None
    middle.press("Escape")
    _assert_sheet_state(page, selected=None, opened=width > 720)
    if width == 1000:
        assert abs(middle.bounding_box()["y"] - original["y"]) < 2, "Clearing selection moved the committed map vertically"
    assert page.url == explorer_consumer.route("chapter/middle")
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(explorer_consumer.heading("chapter/middle"))

    if width == 390:
        handle.click()
    # Ordinary reading links, rather than forced offscreen node clicks, retain
    # each entry's open reader state for the Back/Forward assertions.
    page.locator(".bp-reading-neighbors").get_by_role("link", name=re.compile(r"^Foundation")).click()
    _consumer_selected(page, explorer_consumer, "chapter/foundation")
    page.locator(".bp-reading-neighbors").get_by_role("link", name=re.compile(r"^Middle lemma")).click()
    middle = _consumer_selected(page, explorer_consumer, "chapter/middle")
    _assert_sheet_state(page, selected="chapter/middle", opened=True)
    survivor = page.locator('.bp-dag-node:not([data-autoform-node-id="chapter/middle"])').first
    before_filter = survivor.bounding_box()
    assert before_filter is not None
    page.locator(".bp-dag-filter-summary").click()
    checkbox = page.locator('.bp-dag-filter-popover input[value="can_prove"]')
    if width == 1000:
        popup = page.locator(".bp-dag-filter-popover")
        metrics = popup.evaluate("""popup => {
          const rect=element=>element.getBoundingClientRect().toJSON();
          const checkbox=popup.querySelector('input[value="can_prove"]');
          const style=getComputedStyle(popup), box=rect(checkbox);
          return {viewport:{width:innerWidth,height:innerHeight},popup:rect(popup),checkbox:box,
            anchor:rect(popup.closest('.bp-dag-filters')),position:style.position,left:style.left,right:style.right,
            hit:document.elementFromPoint(box.x+box.width/2,box.y+box.height/2)===checkbox};
        }""")
        print("AUTOFORM_STATUS_POPUP_GEOMETRY " + json.dumps(metrics, sort_keys=True))
        screenshots = os.environ.get("AUTOFORM_UI_SCREENSHOT_DIR")
        if screenshots:
            directory = Path(screenshots)
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / "status-popup-1000.png"))
            (directory / "status-popup-1000.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
        bounds, viewport = metrics["popup"], metrics["viewport"]
        assert 0 <= bounds["x"] and bounds["x"] + bounds["width"] <= viewport["width"], metrics
        assert 0 <= bounds["y"] and bounds["y"] + bounds["height"] <= viewport["height"], metrics
        assert metrics["hit"] is True, metrics
        _assert_hit(page, checkbox)
    checkbox.uncheck()
    _assert_sheet_state(page, selected=None, opened=width > 720)
    if width == 1000:
        after_filter = survivor.bounding_box()
        assert after_filter is not None and abs(after_filter["y"] - before_filter["y"]) < 2, "Hiding the selection moved a surviving node vertically"
        assert page.locator('.bp-dag-node[data-autoform-node-id="chapter/middle"]').count() == 0
    page.go_back()
    _consumer_selected(page, explorer_consumer, "chapter/foundation")
    _assert_sheet_state(page, selected="chapter/foundation", opened=True)
    page.go_forward()
    _consumer_ready(page, explorer_consumer, "chapter/middle")
    _assert_sheet_state(page, selected=None, opened=width > 720)
    playwright.expect(page.locator('.bp-dag-filter-popover input[value="can_prove"]')).not_to_be_checked()
    assert page.evaluate("document===window.__consumerDocument") is True


def test_edges_group_and_status_summary_follow_checkbox_and_history_state(
    desktop_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    summary = page.locator(".bp-dag-filter-summary")
    popover = page.locator(".bp-dag-filter-popover")
    summary.click()
    edges = page.locator(".bp-dag-filter-popover fieldset.bp-dag-isolated")
    playwright.expect(edges).to_be_visible()
    isolates = edges.get_by_role("checkbox", name="Include items without edges", exact=True)
    isolates.uncheck()
    playwright.expect(popover).to_be_visible()  # A filter checkpoint keeps its menu open.
    playwright.expect(summary).to_have_text("Status · edges only")
    search = _load_search_index(page, len(explorer_consumer.records))
    search.fill("Middle lemma")
    search.press("Enter")
    _consumer_selected(page, explorer_consumer, "chapter/middle")
    playwright.expect(popover).to_be_hidden()
    playwright.expect(summary).to_have_attribute("aria-expanded", "false")
    playwright.expect(page.locator(".bp-reading-body h1")).to_be_focused()
    # The focal neighborhood has no isolates. That context hides the actual
    # Edges fieldset; it does not pretend that the earlier chapter filter still applies.
    playwright.expect(edges).to_be_hidden()
    assert edges.evaluate("element => element.hidden") is True
    playwright.expect(summary).to_have_text("Status")
    page.go_back()
    _consumer_ready(page, explorer_consumer)
    playwright.expect(popover).to_be_hidden()
    playwright.expect(summary).to_have_attribute("aria-expanded", "false")
    summary.click()
    playwright.expect(isolates).not_to_be_checked()
    playwright.expect(summary).to_have_text("Status · edges only")
    page.go_forward()
    _consumer_selected(page, explorer_consumer, "chapter/middle")
    playwright.expect(popover).to_be_hidden()
    playwright.expect(summary).to_have_text("Status")
    page.go_back()
    _consumer_ready(page, explorer_consumer)
    playwright.expect(popover).to_be_hidden()
    playwright.expect(summary).to_have_attribute("aria-expanded", "false")
    summary.click()
    enabled = len({node["status"] for node in explorer_consumer.records.values()})
    target_status = explorer_consumer.records["chapter/target"]["status"]
    page.locator(f'.bp-dag-filter-popover input[value="{target_status}"]').uncheck()
    playwright.expect(summary).to_have_text(f"Status · {enabled - 1} · edges only")
    isolates.check()
    playwright.expect(summary).to_have_text(f"Status · {enabled - 1}")


def test_complete_reader_has_native_actions_without_the_legacy_summary_chrome(
    desktop_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    """The old Close-details/status/Open-article row is replaced by full native reading."""
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_consumer(page, explorer_consumer, "chapter/middle")
    page.evaluate("window.__consumerDocument=document")
    reader = page.locator(".bp-reading-body")
    playwright.expect(reader.locator("h1")).to_have_text(explorer_consumer.heading("chapter/middle"))
    card = _reading_card(page, "chapter/middle")
    playwright.expect(card.locator(".arithmatex")).to_have_count(1)
    playwright.expect(page.locator(".bp-dag-close,.bp-dag-open,.bp-dag-status-pill")).to_have_count(0)
    stage, pane = page.locator(".bp-dag-stage").bounding_box(), page.locator(".bp-reading-pane").bounding_box()
    assert stage is not None and pane is not None and stage["x"] + stage["width"] <= pane["x"] + 1
    prerequisite = _open_dependency(card, "Foundation")
    assert urljoin(page.url, prerequisite.get_attribute("href")) == explorer_consumer.route("chapter/foundation")
    prerequisite.scroll_into_view_if_needed()
    _assert_hit(page, prerequisite)
    prerequisite.click()
    _consumer_selected(page, explorer_consumer, "chapter/foundation")
    page.go_back()
    _consumer_selected(page, explorer_consumer, "chapter/middle")
    assert page.evaluate("document===window.__consumerDocument") is True


def test_details_read_as_mathematics_not_as_instructions(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer, "chapter/target")
    reader = page.locator(".bp-reading-body")
    playwright.expect(reader.locator("h1")).to_have_text(explorer_consumer.heading("chapter/target"))
    card = _reading_card(page, "chapter/target")
    playwright.expect(card).to_contain_text("Final sentence of the complete target statement.")
    assert len(card.inner_text()) > 420
    assert len(explorer_consumer.records["chapter/target"]["summary"]) <= 420
    playwright.expect(card.locator(".arithmatex")).to_have_count(1)
    assert card.locator(".arithmatex").inner_text() == r"\(0=0\)"
    playwright.expect(reader).not_to_contain_text("Path spotlight")
    playwright.expect(reader).not_to_contain_text("keyboard-accessible")
    playwright.expect(page.locator(".bp-dag-summary,.bp-dag-inspector-header")).to_have_count(0)
    # Dependency links still come from the real index, and the complete
    # mathematical statement remains the primary document rather than a preview.
    playwright.expect(page.locator(".bp-reading-neighbors").get_by_role("link", name=re.compile(r"^Middle lemma"))).to_be_visible()
    # The removed inspector's path pivots do not justify dropping their
    # compatibility behavior: the same controller interprets old directions.
    for direction, expected in (
        ("prerequisites", ["chapter/foundation", "chapter/middle"]),
        ("dependents", ["chapter/middle", "chapter/target"]),
        ("both", ["chapter/foundation", "chapter/middle", "chapter/target"]),
    ):
        page.goto(explorer_consumer.url + "/dependencies/chapters/chapter.html#node=chapter%2Fmiddle&direction=" + direction)
        _consumer_selected(page, explorer_consumer, "chapter/middle")
        _paint_frame(page)
        assert sorted(page.locator(".bp-dag-node").evaluate_all("nodes => nodes.map(node => node.dataset.autoformNodeId)")) == expected
        playwright.expect(_reading_card(page, "chapter/target")).to_contain_text("Final sentence of the complete target statement.")


@pytest.mark.parametrize(("name", "counts"), (("mixed", "4 results"), ("flat", "100 of 1,000")))
def test_the_rail_counts_in_words_that_agree_with_their_numbers(
    desktop_page: object, explorer_consumer: _ExplorerConsumer, name: str, counts: str,
) -> None:
    """Count wording now belongs to the canonical Results view, not a duplicate rail overview."""
    page = desktop_page
    _open_consumer(page, explorer_consumer, name)
    page.get_by_role("button", name="Results", exact=True).click()
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text(counts)
    graph = explorer_consumer.graphs[name]
    assert len(graph["nodes"]) == (4 if name == "mixed" else 1_000)
    assert len(graph["edges"]) == (1 if name == "mixed" else 0)
    if name == "mixed":
        page.locator(".bp-dag-search").fill("mixed/a")
        playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("1 result · search the whole roadmap")
    else:
        page.locator(".bp-dag-inventory > .bp-dag-load-more").click()
        playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("200 of 1,000")


def test_semantic_outline_and_status_controls_do_not_repeat_leaf_summary_rows(
    desktop_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    """Scopes stay in the outline; leaves and their status remain in the map/full reader."""
    page = desktop_page
    _open_consumer(page, explorer_consumer, "chapter/middle")
    navigation = page.locator(".bp-dag-navigation")
    scope = navigation.locator('[data-navigation-node-id="chapter"]')
    playwright.expect(scope).to_have_text("Dependency chapter")
    playwright.expect(scope).to_have_attribute("href", explorer_consumer.route("chapter"))
    playwright.expect(scope).to_have_attribute("title", "Dependency chapter")
    playwright.expect(navigation.locator('[data-navigation-node-id="chapter/middle"]')).to_have_count(0)
    playwright.expect(page.locator('.bp-dag-node[data-autoform-node-id="chapter/middle"] .bp-dag-node-meta')).to_have_text("ready to prove")
    page.locator(".bp-dag-filter-summary").click()
    filters = page.locator(".bp-dag-filter-popover")
    statuses = {node["status"] for node in explorer_consumer.records.values()}
    playwright.expect(filters.locator(".bp-dag-swatch")).to_have_count(len(statuses))
    for status in statuses:
        expected = sum(node["status"] == status for node in explorer_consumer.records.values())
        row = filters.locator("label").filter(has=page.locator(f'input[value="{status}"]'))
        playwright.expect(row.locator(".bp-dag-check-count")).to_have_text(str(expected))


def test_the_toolbar_counts_only_what_it_hides_and_shows_it_on_request(
    desktop_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    page = desktop_page
    _offline(page, explorer_consumer.url)
    page.goto(explorer_consumer.url + "/dependencies/chapters/chapter.html#view=list&status=fully_proved")
    _consumer_ready(page, explorer_consumer)
    reveal, summary = page.locator(".bp-dag-hidden-button"), page.locator(".bp-dag-inventory-summary")
    playwright.expect(reveal).to_have_text("Show 62 hidden")
    playwright.expect(summary).to_have_text("1 result")
    reveal.click()
    playwright.expect(reveal).to_be_hidden()
    playwright.expect(summary).to_have_text("63 results")
    playwright.expect(page.locator(".bp-dag-inventory-row")).to_have_count(63)
    page.goto(explorer_consumer.url + "/dependencies/chapters/chapter.html#view=list&status=-")
    _consumer_ready(page, explorer_consumer)
    playwright.expect(page.locator(".bp-dag-inventory-row")).to_have_count(0)
    playwright.expect(reveal).to_have_text("Show 63 hidden")
    playwright.expect(_reading_card(page, "chapter/target")).to_contain_text("Final sentence of the complete target statement.")


def test_a_click_that_opens_details_over_its_card_pans_it_clear(desktop_page: object, site_url: str) -> None:
    """Explicit graph activation keeps its focal theorem visible beside the persistent full reading pane."""
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    _open_site(page, site_url, "dependencies/chapters/algebra.html")
    page.evaluate("() => window.__clickedGraphDocument = document")
    before_head = page.locator(".bp-dag-head").bounding_box()
    for node_id in ("algebra/result", "algebra/base"):
        card = page.locator(f'.bp-dag-node[data-autoform-node-id="{node_id}"]')
        card.click()
        page.wait_for_url(_site_route(page, site_url, node_id))
        selected = page.locator(f'.bp-dag-node[data-autoform-node-id="{node_id}"][data-selected="true"]')
        playwright.expect(selected).to_be_visible()
        _paint_frame(page)
        box, reader, stage = (
            item.bounding_box() for item in (selected, page.locator(".bp-reading-pane"), page.locator(".bp-dag-stage"))
        )
        assert box is not None and reader is not None and stage is not None
        assert stage["x"] <= box["x"] and box["x"] + box["width"] <= stage["x"] + stage["width"]
        assert stage["y"] <= box["y"] and box["y"] + box["height"] <= stage["y"] + stage["height"]
        assert box["x"] + box["width"] <= reader["x"] + 1
        _assert_hit(page, selected)
        _reading_card(page, node_id)
        assert page.evaluate("() => document === __clickedGraphDocument")
    after_head = page.locator(".bp-dag-head").bounding_box()
    assert before_head is not None and after_head is not None
    assert abs(after_head["height"] - before_head["height"]) < 2



@pytest.mark.parametrize("width", (800, 1000))
def test_a_head_without_a_textbook_link_has_no_blank_row(desktop_page: object, site_url: str, width: int) -> None:
    """The reusable Library/context row is real navigation, never a reserved blank textbook-link row."""
    page = desktop_page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    for path in ("dependencies.html", "dependencies/full.html"):
        _open_site(page, site_url, path)
        _assert_hit(page, page.locator(".bp-dag-book"))
        context, toolbar, head = (
            page.locator(selector).bounding_box() for selector in (".bp-dag-context", ".bp-dag-toolbar", ".bp-dag-head")
        )
        assert context is not None and toolbar is not None and head is not None
        assert context["height"] >= 20
        assert 0 <= toolbar["y"] - (context["y"] + context["height"]) <= 12
        assert toolbar["y"] + toolbar["height"] <= head["y"] + head["height"] + 1
        page.locator(".bp-dag-search").fill("algebra/result")
        page.locator('.bp-dag-result[data-result-id="algebra/result"]').click()
        page.wait_for_url(_site_route(page, site_url, "algebra/result"))
        _reading_card(page, "algebra/result")
        after = page.locator(".bp-dag-head").bounding_box()
        assert after is not None and abs(after["height"] - head["height"]) < 2



def test_opening_details_keeps_the_clicked_card_under_the_pointer(desktop_page: object, site_url: str) -> None:
    """Phone reading-plane toggles do not silently move the current graph camera or replace its node."""
    page = desktop_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(reduced_motion="reduce")
    _open_site(page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    handle = page.get_by_role("button", name="Toggle mathematical reading", exact=True)
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    node = page.locator('.bp-dag-node[data-autoform-node-id="algebra/result"]')
    _paint_frame(page)
    original = node.bounding_box()
    identity = node.element_handle()
    url, history_length = page.url, page.evaluate("history.length")
    assert original is not None
    for _ in range(2):
        handle.click()
        playwright.expect(handle).to_have_attribute("aria-expanded", "true")
        _paint_frame(page)
        opened = node.bounding_box()
        assert opened is not None and abs(opened["y"] - original["y"]) < 2
        handle.click()
        playwright.expect(handle).to_have_attribute("aria-expanded", "false")
        _paint_frame(page)
        closed = node.bounding_box()
        assert closed is not None and abs(closed["y"] - original["y"]) < 2
        assert identity.evaluate("node => node === document.querySelector('.bp-dag-node[data-autoform-node-id=\"algebra/result\"]')")
        assert page.url == url and page.evaluate("history.length") == history_length



def test_live_dom_labels_use_the_site_family_without_numeric_edge_labels(desktop_page: object, count_label_site_url: str) -> None:
    """Verify actual card glyphs and weighted-edge rendering in a published consumer."""
    page, site_url = desktop_page, count_label_site_url
    _observe_canvas_typography(page, reject_family=False)
    canonical = _site_route(page, site_url, "roadmap")
    _open_site(page, site_url, urlsplit(canonical).path.lstrip("/"))
    _paint_frame(page)
    typography = _live_card_typography(page)
    family = page.locator(".bp-dag-viewer").evaluate("host => getComputedStyle(host).fontFamily")
    assert "Plus Jakarta Sans" in family and "Plus Jakarta Sans" in typography["family"]
    assert typography["pixels"] * typography["scale"] >= 11
    assert abs(typography["width"] - typography["expected"]) < .75
    assert page.evaluate("window.labelPaints.some(paint => paint.text === '×12')") is False
    assert _canvas_pixels(page)["painted"] > 0
