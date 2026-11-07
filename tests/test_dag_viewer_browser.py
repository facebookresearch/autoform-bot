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

from autoform_cli import dag_viewer, site_index
from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_integrated_publication import _decoded_relations

# CI sets this, so a missing browser fails the job instead of skipping every case.
_REQUIRED = os.environ.get("AUTOFORM_REQUIRE_BROWSER_TESTS") == "1"
_TITLE_MATH_SCOPE = r"Groups $\ZZ/n$ and code `$literal$`"
_TITLE_MATH_FOUNDATION = r"Groups $\mathbb{Z}/n$ and code `$literal$`"
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


# WebKit reports this when a resize callback changes layout again within one frame. It is a
# notice, not an uncaught exception, and it failed unrelated tests at random; a real layout
# loop still fails the geometry assertions.
_BENIGN_PAGE_ERRORS = ("ResizeObserver loop completed with undelivered notifications.",)


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
    assert [error for error in errors if error not in _BENIGN_PAGE_ERRORS] == []


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
    """Canonical routes, scope payloads and typed relations captured from the actual publisher."""

    url: str
    records: dict[str, dict]
    graphs: dict[str, dict]
    headings: dict[str, str]
    page_titles: dict[str, str]
    relations: list[list[object]]

    def route(self, node_id: str) -> str:
        return urljoin(self.url + "/dependencies/index.json", self.records[node_id]["route"])

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
    index = json.loads(index_path.read_text(encoding="utf-8"))
    records = {node["id"]: node for node in index["nodes"]}
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
        yield _ExplorerConsumer(url, records, graphs, headings, page_titles, _decoded_relations(index))


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
def site_url(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Serve a blueprint as render and MkDocs publish it, under Material's styles and scripts.

    A test can pass a project setting for the roadmap's README, such as ``tracks_lean: false``.
    """
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("site") / "project"
    scaffold_project(project, title="Explorer fixture")
    roadmap = project / "blueprint/roadmap"
    setting = getattr(request, "param", None)
    (roadmap / "README.md").write_text(
        (f"---\n{setting}\n---\n\n" if setting else "")
        + "# Fixture roadmap\n\n- [Algebra](algebra/README.md)\n- [Topology](topology/README.md)\n",
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
    page.locator(".bp-dag-integrated .bp-whole-map .pm-app").wait_for()
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


def _reference_counts(relations: list[list[object]], owner_id: str, target_id: str) -> tuple[int, int]:
    """Original weights in these one-direction fixtures, not a reciprocal-link resolver."""
    pair = {owner_id, target_id}
    assert len(pair) == 2
    edges = [relation for relation in relations if set(relation[:2]) == pair]
    assert len(edges) == 1, "This fixture reference must name one unambiguous original directed pair"
    counts = (edges[0][2], edges[0][3])
    assert all(type(count) is int and count >= 0 for count in counts) and sum(counts) > 0
    return counts


def _named_reference(disclosure: object, target: object, href: str, counts: tuple[int, int]) -> object:
    """Match a reference by the name its statement shows, and its exact typed descriptions."""
    playwright.expect(target).to_have_count(1)
    title = target.locator(".bp-thmheading > .bp-thmtitle").text_content()
    assert isinstance(title, str)
    label = title.strip()
    link = disclosure.locator("a.bp-ref").filter(has_text=re.compile(r"^" + re.escape(label) + r"$"))
    playwright.expect(link).to_have_count(1)
    playwright.expect(link).to_have_text(label)
    published_href = link.get_attribute("href")
    assert published_href is not None and urljoin(disclosure.page.url, published_href) == href
    weights = ", ".join(
        f"{count} {kind} {'dependency' if count == 1 else 'dependencies'}"
        for count, kind in zip(counts, ("statement", "proof")) if count
    )
    assert link.get_attribute("aria-label") is None
    playwright.expect(link).to_have_attribute("title", weights)
    playwright.expect(link).to_have_accessible_description(weights)
    playwright.expect(link).to_have_accessible_name(label)
    playwright.expect(link).to_be_visible()
    return link


def _open_dependency(card: object, consumer: _ExplorerConsumer, target_id: str) -> object:
    """Use the existing disclosure, canonical endpoint and original typed pair."""
    disclosure = card.locator("details.bp-dependencies")
    playwright.expect(disclosure).to_have_count(1)
    if disclosure.get_attribute("open") is None:
        disclosure.locator(":scope > summary").click()
    owner_id = card.get_attribute("data-autoform-node-id")
    assert owner_id in consumer.records and target_id in consumer.records
    target = card.page.locator(
        ".bp-reading-source .bp-thmwrapper[data-autoform-node-id=" + json.dumps(target_id) + "]"
    )
    playwright.expect(target.locator(".bp-thmheading > .bp-thmtitle")).to_have_text(
        consumer.records[target_id]["title"]
    )
    return _named_reference(
        disclosure, target, consumer.route(target_id), _reference_counts(consumer.relations, owner_id, target_id)
    )


def _close_phone_reading(page: object) -> None:
    """Reveal the map/list through the actual Reading handle, whose taps cycle half, full and the peek."""
    handle = page.locator(".bp-dag-sheet-handle")
    for _ in range(2):
        if handle.get_attribute("aria-expanded") != "true":
            break
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
    (project / "blueprint/tex-macros.json").write_text(
        json.dumps({"ZZ": r"\mathbb{Z}"}), encoding="utf-8",
    )
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
        if name == "algebra":
            links += "- [Groups](title-foundation.md)\n- [Title-math target](title-target.md)\n- [Catalogue](catalog.md)\n"
        if name == "topology":
            links += "".join(f"- [Later {n}](later-{n}.md)\n" for n in range(55))
        heading = _TITLE_MATH_SCOPE if name == "algebra" else name.title()
        (folder / "README.md").write_text(
            f"---\nnavigation: module\n---\n\n# {heading}\n\n{links}",
            encoding="utf-8",
        )
        (folder / "base.md").write_text(
            "---\ndeclaration: def\n---\n\n# Base\n\nThe base value is $0$. "
            "Navigate to [Result $y^2$](result.md).\n\n## Depends on\n\nNo prerequisites.\n",
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
        if name == "algebra":
            (folder / "title-foundation.md").write_text(
                "---\ndeclaration: def\n---\n\n# " + _TITLE_MATH_FOUNDATION
                + "\n\nThis authored group notation retains the identity $0=0$.\n\n"
                "## Depends on\n\nNo prerequisites.\n", encoding="utf-8",
            )
            (folder / "title-target.md").write_text(
                "---\ndeclaration: theorem\n---\n\n# Title-math target\n\n"
                "The actual [group notation](title-foundation.md) remains available in the reader.\n\n"
                "## Depends on\n\n- [Groups](title-foundation.md)\n", encoding="utf-8",
            )
            _write_consumer(folder / "catalog.md", (
                "---\ncatalog: module\n---\n\n# Catalogue of groups\n\n"
                "An authored inventory is not a mathematical proof.\n\n"
                "## Sources\n\n- [Catalogue source](../../sources/catalog.md)\n"
            ))
            _write_consumer(project / "blueprint/sources/catalog.md", "# Catalogue source\n\nAuthored source commentary.\n")
        if name == "topology":
            for n in range(55):
                title = f"Later {n}" + (r" $\ZZ/n$ and `$literal$`" if n < 8 else "")
                (folder / f"later-{n}.md").write_text(
                    f"---\ndeclaration: theorem\n---\n\n# {title}\n\nThe identity $0=0$ is stated explicitly.\n\n"
                    "Its complete explanatory text remains available after relation navigation.\n\n"
                    "## Depends on\n\n- [Result](result.md)\n",
                    encoding="utf-8",
                )
    render_site(project / "blueprint", project / "site-src")
    build(load_config(config_file=str(project / "mkdocs.yml")))
    index = json.loads((project / "site/dependencies/index.json").read_text(encoding="utf-8"))
    assert sum(edge[0] == "topology/result" for edge in _decoded_relations(index)) == 55
    records = {node["id"]: node for node in index["nodes"]}
    for node_id, title in (("algebra", _TITLE_MATH_SCOPE), ("algebra/title-foundation", _TITLE_MATH_FOUNDATION)):
        assert records[node_id]["title"] == title
        assert records[node_id]["reading_title"] == site_index._reading_label(title)
    with _served(project / "site") as url:
        yield url


@pytest.fixture(scope="module")
def marked_math_runtime() -> object:
    from tests.test_integrated_explorer_browser import _PinnedRuntime

    return _PinnedRuntime()


def _assert_native_handle_math(page: object) -> None:
    handle = page.locator(".bp-reading-pane > .bp-dag-sheet-handle")
    assert page.evaluate("AutoformMath.refresh(document.querySelector('.bp-dag-sheet-handle'))") is True
    title = handle.locator(".bp-dag-sheet-title")
    playwright.expect(title.locator(".arithmatex")).to_have_count(1)
    playwright.expect(title.locator(".arithmatex")).to_have_attribute("data-autoform-math-source", r"\(\ZZ/n\)")
    playwright.expect(title.locator("mjx-container")).to_have_count(1)
    playwright.expect(title.locator("mjx-c.mjx-c2124")).to_have_count(1)
    assert title.locator("mjx-c").evaluate_all("""nodes=>nodes.length>=3&&nodes.every(node=>
      !['none','normal',''].includes(getComputedStyle(node,'::before').content))""")
    playwright.expect(title.locator("code")).to_have_text("$literal$")
    assert r"\(" not in title.inner_text() and r"\ZZ" not in title.inner_text()
    assert title.locator("code .arithmatex,code mjx-container,[tabindex],[role=application],mjx-merror,merror").count() == 0
    playwright.expect(handle.locator(".bp-dag-swatch")).to_have_count(0)
    assert page.evaluate("MathJax.version") == "3.2.2"
    _assert_hit(page, handle)
    handle.focus()
    assert page.evaluate("AutoformMath.refresh(document.querySelector('.bp-dag-sheet-handle'))") is True
    playwright.expect(handle).to_be_focused()


@pytest.mark.parametrize("entrance", ["chapter-heading", "catalogue"])
def test_phone_peek_typesets_the_pages_own_chapter_title_without_borrowing_a_catalogue_status(
    mobile_page: object, marked_math_site_url: str, marked_math_runtime: object, entrance: str,
) -> None:
    from tests.test_integrated_explorer_browser import _routes
    from tests.test_integrated_selected_focus_browser import _complete_source, _published_source

    page, base = mobile_page, marked_math_site_url
    scope = _site_route(page, base, "algebra")
    response = page.request.get(scope)
    assert response.ok
    tree = html5lib.parse(response.text(), namespaceHTMLElements=False)
    source = next(node for node in tree.iter() if "bp-reading-source" in node.get("class", "").split())
    page_title = source.get("data-page-title")
    assert page_title
    if entrance == "catalogue":
        record = _site_record(page, base, "algebra/catalog")
        assert record["result"] is False and record["scope"] == "algebra"
        assert record["title"] != _TITLE_MATH_SCOPE
        canonical = _site_route(page, base, "algebra/catalog")
    else:
        anchor = next(source.iter("h1")).get("id")
        assert anchor and anchor != urlsplit(_site_route(page, base, "algebra/result")).fragment
        canonical = urlsplit(scope)._replace(fragment=anchor).geturl()
    expected, home_source = _published_source(page, canonical), _published_source(page, base + "/index.html")
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _routes(page, base, marked_math_runtime, graph_client=True)
    page.goto(canonical)
    _ready_site(page)
    page.evaluate("window.__chapterHandleDocument=document")
    _complete_source(page, expected)
    # Render writes the page's own title as text, since nothing typesets it, and the tab reads it as it is.
    assert page_title == "Groups ZZ/n and code $literal$" and page.title() == page_title
    _close_phone_reading(page)
    _paint_frame(page)
    _assert_native_handle_math(page)
    assert page.url == canonical
    home = page.locator(".bp-dag-breadcrumb .bp-dag-book")
    playwright.expect(home).to_have_attribute("href", base + "/index.html")
    _assert_hit(page, home)
    home.click()
    page.wait_for_url(base + "/index.html")
    _complete_source(page, home_source)
    page.go_back()
    page.wait_for_url(canonical)
    # History commits its URL before the captured reader is reconciled.
    # Wait for this source's actual owner and full card count, not just H1 count.
    restored = page.locator('.bp-reading-body > .bp-reading-source[data-autoform-node-id="algebra"]')
    playwright.expect(restored).to_have_count(1)
    playwright.expect(restored.locator(".bp-thmwrapper")).to_have_count(len(expected["cards"]))
    _complete_source(page, expected)
    _paint_frame(page)
    _assert_native_handle_math(page)
    assert page.evaluate("document===__chapterHandleDocument") is True
    assert documents == [scope.split("#", 1)[0]]


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
    """The book has selected ``node_id``, and the whole map has followed it there and selected its card."""
    _consumer_ready(page, consumer, node_id)
    host = page.locator(".bp-dag-viewer")
    playwright.expect(host).to_have_attribute("data-map-start", node_id)
    playwright.expect(host).to_have_class(re.compile(r"\bbp-dag-has-selection\b"))
    # Attached rather than visible: on a phone the half reader may cover the card.
    control = page.locator(f'.bp-whole-map .pm-result.pm-selected[data-result="{node_id}"]')
    playwright.expect(control).to_have_count(1)
    anchor = unquote(urlsplit(consumer.route(node_id)).fragment)
    if anchor:
        # A statement's anchor is on its card's heading, and the card is the current reading.
        current = f'.bp-reading-body .bp-reading-current [id="{anchor}"], .bp-reading-body [id="{anchor}"].bp-reading-current'
        playwright.expect(page.locator(current)).to_have_count(1)
    return control


def _map_card(page: object, node_id: str) -> object:
    """The whole map's card for ``node_id``, once the map has followed the book there and selected it."""
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_attribute("data-map-start", node_id)
    card = page.locator(f'.bp-whole-map .pm-result.pm-selected[data-result="{node_id}"]')
    playwright.expect(card).to_be_visible()
    # Once the map's flight there has settled and the card takes clicks.
    card.click(trial=True)
    return card


def _load_search_index(page: object) -> object:
    """Focus the search box and wait until no whole-roadmap index load is in flight.

    The router loads the index before the explorer mounts. After a failed
    load, focus retries and marks the box aria-busy in the same handler, so
    the busy state clearing means that retry has finished.
    """
    search = page.locator(".bp-dag-search")
    search.focus()
    playwright.expect(search).not_to_have_attribute("aria-busy", "true")
    return search


def _browse_contents(page: object, action: str) -> None:
    """Use a real source-choice menu, or the direct statements action when no sources exist."""
    toggle = page.locator(".bp-dag-browse > .bp-dag-button")
    popover = page.locator(".bp-dag-browse-popover")
    playwright.expect(popover).to_be_hidden()
    if toggle.get_attribute("aria-controls") is None:
        assert action in {"All statements", "Statements in this section"}
        playwright.expect(toggle).to_have_attribute("aria-label", "Browse statements")
        toggle.click()
        playwright.expect(popover).to_be_hidden()
        playwright.expect(page.locator(".bp-dag-inventory")).to_be_visible()
        return
    toggle.click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "true")
    playwright.expect(popover).to_be_visible()
    popover.get_by_role("button", name=action, exact=True).click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    playwright.expect(popover).to_be_hidden()


def test_mobile_node_interaction_and_selected_sheet_collapse(mobile_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = mobile_page
    _open_consumer(page, explorer_consumer)
    # With the reader at its peek, the chapter's part is in reach on the map, and a tap on a card opens that result.
    _close_phone_reading(page)
    playwright.expect(page.locator('.bp-whole-map .pm-part.pm-focus[data-part="chapter"]')).to_have_count(1)
    page.locator('.bp-whole-map .pm-result[data-result="chapter/middle"]').tap()
    _consumer_selected(page, explorer_consumer, "chapter/middle")
    # The map's card names the result and its status, never the internal id. The half reader covers it on a phone.
    card = page.locator('.bp-whole-map .pm-result.pm-selected[data-result="chapter/middle"]')
    playwright.expect(card).to_have_attribute("aria-label", "Middle lemma, ready to prove")
    reader = page.locator(".bp-reading-body")
    playwright.expect(reader.locator("h1")).to_have_text(explorer_consumer.heading("chapter/middle"))
    playwright.expect(_reading_card(page, "chapter/middle")).to_contain_text("Middle lemma")
    handle, inner = page.locator(".bp-dag-sheet-handle"), page.locator(".bp-dag-inspector-inner")
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(handle).to_have_attribute("aria-controls", inner.get_attribute("id"))
    assert inner.get_attribute("aria-hidden") is None and inner.get_attribute("inert") is None
    assert page.url == explorer_consumer.route("chapter/middle")
    _assert_hit(page, handle)
    # The reader opens at half; a tap covers the map and the next returns to the peek.
    handle.tap()
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_class(re.compile(r"\bbp-dag-sheet-full\b"))
    handle.tap()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    playwright.expect(inner).to_have_attribute("aria-hidden", "true")
    playwright.expect(inner).to_have_attribute("inert", "")
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_attribute("data-map-start", "chapter/middle")
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


def test_an_atlas_source_inventory_opens_its_module_through_its_native_permalink(
    desktop_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    # Source inventories keep their honest authored role, never a proof-status summary.
    _open_consumer(page, explorer_consumer, "atlas")
    _browse_contents(page, "Source texts")
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


def test_a_catalog_reads_its_full_inventory(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer, "topics/library")
    # The full reading inventory supersedes the removed eight-name summary cap.
    for name in ["Topology.«open, closed»", *(f"Topology.lemma{n}" for n in range(2, 11))]:
        playwright.expect(page.locator(".bp-reading-body")).to_contain_text(name)


def test_a_roadmap_without_articles_says_so(desktop_page: object, empty_consumer_url: str) -> None:
    page = desktop_page
    _offline(page, empty_consumer_url)
    page.goto(empty_consumer_url + "/roadmap/index.html")
    page.locator(".bp-dag-integrated .bp-dag-stage").wait_for()
    empty = page.locator(".bp-whole-map .pm-hint")
    playwright.expect(empty).to_be_visible()
    playwright.expect(empty).to_have_text("Nothing is on the map yet: the roadmap has no results.")
    # The notice reads across the stage's middle, not down a column one word wide.
    box = empty.bounding_box()
    assert box is not None and box["width"] > 160, box
    playwright.expect(page.locator(".bp-reading-body")).to_contain_text("No mathematical articles are authored yet.")


def test_an_oversized_flat_scope_opens_on_the_whole_map_beside_its_reader(
    desktop_page: object, explorer_consumer: _ExplorerConsumer
) -> None:
    """A topic of 1,000 statements, too many for the explorer's own drawing, opens on the whole map, not a list."""
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_consumer(page, explorer_consumer, "flat")
    playwright.expect(page.locator(".bp-dag-stage")).to_be_visible()
    playwright.expect(page.locator('.bp-whole-map .pm-part.pm-focus[data-part="flat"]')).to_have_count(1)
    playwright.expect(page.get_by_role("button", name="Back to map", exact=True)).to_be_hidden()
    # The oversized chapter opens with its reader, not a map-first overview.
    playwright.expect(page.locator(".bp-dag-viewer")).not_to_have_class(re.compile(r"\bbp-dag-overview-only\b"))
    reader = page.locator(".bp-reading-pane").bounding_box()
    assert reader is not None and reader["width"] >= 340, reader
    _browse_contents(page, "Statements in this section")
    playwright.expect(page.get_by_role("button", name="Back to map", exact=True)).to_be_enabled()
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("100 of 1,000")
    playwright.expect(page.locator(".bp-dag-inventory-row")).to_have_count(100)
    playwright.expect(page.locator(".bp-reading-body")).to_contain_text("Every item below is an authored not-ready statement")


def test_choosing_from_contents_keeps_the_readers_place_and_focus(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    """Back restores an actual paginated Results row, not the former summary-only inspector."""
    page = desktop_page
    _open_consumer(page, explorer_consumer, "flat")
    _browse_contents(page, "Statements in this section")
    page.locator(".bp-dag-inventory > .bp-dag-load-more").click()
    # Paging moves focus to the first new row rather than leaving it on the button below them.
    playwright.expect(page.locator(".bp-dag-inventory-row").nth(100)).to_be_focused()
    row = page.locator('.bp-dag-inventory-row[data-inventory-node-id="flat/item-0150"]')
    scroll = page.locator(".bp-dag-inventory-scroll")
    row.scroll_into_view_if_needed()
    row.focus()
    offset = scroll.evaluate("element => {element.scrollTop += 40;return element.scrollTop;}")
    assert offset > 0
    page.evaluate("window.__consumerDocument=document")
    row.press("Enter")
    _consumer_selected(page, explorer_consumer, "flat/item-0150")
    # Focus lands on the chosen statement, which the reader scrolled to, not on the page title above it.
    playwright.expect(page.locator(".bp-reading-body .bp-reading-current .bp-thmheading")).to_be_focused()
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


def test_phone_selection_opens_the_reader_at_half_and_returns_to_a_reachable_selected_map(
    mobile_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    page = mobile_page
    _open_consumer(page, explorer_consumer)
    page.evaluate("window.__consumerDocument=document")
    search = _load_search_index(page)
    search.fill("Target theorem")
    search.press("Enter")
    _consumer_selected(page, explorer_consumer, "chapter/target")
    playwright.expect(page.locator(".bp-reading-body .bp-reading-current .bp-thmheading")).to_be_focused()
    node = _map_card(page, "chapter/target")
    sheet, body = page.locator(".bp-reading-pane").bounding_box(), page.locator(".bp-dag-body").bounding_box()
    assert sheet is not None and body is not None
    # The three-position sheet opens a selection at half, leaving the top 45%
    # to the map with the selected card above the sheet.
    assert sheet["y"] == pytest.approx(body["y"] + body["height"] * .45, abs=2) and abs(sheet["height"] - body["height"]) < 1
    selected = node.bounding_box()
    assert selected is not None and selected["y"] + selected["height"] <= sheet["y"]
    playwright.expect(_reading_card(page, "chapter/target")).to_contain_text("Final sentence of the complete target statement.")
    _close_phone_reading(page)
    box, peek, stage = node.bounding_box(), page.locator(".bp-dag-sheet-handle").bounding_box(), page.locator(".bp-dag-stage").bounding_box()
    assert box is not None and peek is not None and stage is not None
    assert stage["x"] <= box["x"] and box["x"] + box["width"] <= stage["x"] + stage["width"]
    assert stage["y"] <= box["y"] and box["y"] + box["height"] <= peek["y"]
    _assert_hit(page, node)
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_attribute("data-map-start", "chapter/target")
    assert page.evaluate("document===window.__consumerDocument") is True



def test_a_phone_that_follows_a_link_to_a_statement_keeps_its_trail_and_search_on_screen(
    mobile_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    """The half reader hangs below the viewer, and a statement's anchor scrolled the whole explorer up off its trail and search."""
    page = mobile_page
    _open_consumer(page, explorer_consumer)
    page.goto(explorer_consumer.route("chapter/target"))
    _consumer_selected(page, explorer_consumer, "chapter/target")
    page.wait_for_timeout(300)
    assert page.evaluate("document.querySelector('.bp-dag-viewer').scrollTop") == 0
    for selector in (".bp-dag-breadcrumb", ".bp-dag-search"):
        box = page.locator(selector).bounding_box()
        assert box is not None and box["y"] >= 0, selector
    # A fresh load of the same link takes the browser's own anchor scroll.
    page.reload()
    _consumer_selected(page, explorer_consumer, "chapter/target")
    page.wait_for_timeout(300)
    assert page.evaluate("document.querySelector('.bp-dag-viewer').scrollTop") == 0
    box = page.locator(".bp-dag-search").bounding_box()
    assert box is not None and box["y"] >= 0

def test_mobile_contents_pagination_stays_above_the_collapsed_sheet(mobile_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = mobile_page
    _open_consumer(page, explorer_consumer, "flat")
    _browse_contents(page, "Statements in this section")
    _close_phone_reading(page)
    more = page.locator(".bp-dag-inventory > .bp-dag-load-more")
    more.scroll_into_view_if_needed()
    _assert_hit(page, more)
    box, handle = more.bounding_box(), page.locator(".bp-dag-sheet-handle").bounding_box()
    assert box is not None and handle is not None and box["y"] + box["height"] <= handle["y"]
    more.tap()
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("200 of 1,000")


def test_a_books_own_area_map_presses_nothing_and_a_list_presses_only_its_browse_choice(
    desktop_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    """Two views once read as pressed on a book's area map. With Back to map and Browse, the map offers no way back to
    itself and presses nothing, and a list presses only its own Browse choice."""
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_consumer(page, explorer_consumer, "atlas")
    pressed = page.get_by_role("group", name="Dependency explorer controls", exact=True).locator('[aria-pressed="true"]')
    back = page.get_by_role("button", name="Back to map", exact=True)
    playwright.expect(pressed).to_have_count(0)
    playwright.expect(back).to_be_hidden()
    _browse_contents(page, "Statements in this section")
    playwright.expect(pressed).to_have_count(1)
    playwright.expect(pressed).to_have_text("Statements in this section")
    back.click()
    playwright.expect(back).to_be_hidden()
    playwright.expect(pressed).to_have_count(0)


@pytest.mark.parametrize("node_id", ["chapter/middle", "chapter"])
def test_back_to_map_from_a_statement_list_focuses_the_whole_map(
    desktop_page: object, explorer_consumer: _ExplorerConsumer, node_id: str,
) -> None:
    """Back to map hands focus to the whole map: to the card it has selected, or else to the map itself."""
    page = desktop_page
    page.emulate_media(reduced_motion="reduce")
    _open_consumer(page, explorer_consumer, node_id)
    target = page.locator(f'.bp-whole-map .pm-result.pm-selected[data-result="{node_id}"]')
    if node_id == "chapter":
        # A topic's page opens the topic on the map and selects no card, so the map itself takes focus.
        playwright.expect(page.locator('.bp-whole-map .pm-part.pm-focus[data-part="chapter"]')).to_have_count(1)
        target = page.locator(".bp-whole-map .pm-stage > svg")
    playwright.expect(target).to_have_count(1)
    # A part file arriving while the list hides the map redraws it without its card, so let the map load first.
    page.wait_for_load_state("networkidle")
    _browse_contents(page, "Statements in this section")
    back = page.get_by_role("button", name="Back to map", exact=True)
    back.click()
    playwright.expect(back).to_be_hidden()
    playwright.expect(target).to_be_focused()


def test_a_map_that_cannot_load_leaves_one_notice_and_back_to_map_focuses_the_filter(
    desktop_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    """Without the whole map the stage says so once, the lists still work, and Back to map leaves focus on the filter."""
    page = desktop_page
    page.route("**/javascripts/project-map.js", lambda route: route.abort())
    _open_consumer(page, explorer_consumer, "chapter/middle")
    notice = page.locator(".bp-dag-stage > .bp-dag-map-unavailable")
    playwright.expect(notice).to_have_count(1)
    playwright.expect(notice).to_have_text("The map could not load. Contents, search and the statement lists still work.")
    playwright.expect(notice).to_have_attribute("role", "status")
    playwright.expect(notice).to_be_visible()
    playwright.expect(page.locator(".bp-whole-map")).to_have_count(0)
    _browse_contents(page, "Statements in this section")
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("63 statements")
    back = page.get_by_role("button", name="Back to map", exact=True)
    back.click()
    playwright.expect(back).to_be_hidden()
    playwright.expect(page.locator(".bp-dag-filter-summary")).to_be_focused()
    playwright.expect(notice).to_have_count(1)
    playwright.expect(notice).to_be_visible()


def test_a_map_that_stops_after_making_its_host_still_leaves_the_notice(
    webkit_browser: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    """The map makes its host in the stage first, so a script that throws after that never marks it set up."""
    stub = (
        "(function () { var host = document.createElement('div'); host.className = 'bp-whole-map';"
        " document.querySelector('[data-whole-map] .bp-dag-stage').appendChild(host);"
        " throw new Error('the map stopped partway'); })();"
    )
    # Its own page: the shared one fails on any uncaught page error, and this script throws one on purpose.
    context = webkit_browser.new_context(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/javascripts/project-map.js", lambda route: route.fulfill(content_type="text/javascript", body=stub))
    try:
        _open_consumer(page, explorer_consumer, "chapter/middle")
        notice = page.locator(".bp-dag-stage > .bp-dag-map-unavailable")
        playwright.expect(notice).to_have_count(1)
        playwright.expect(notice).to_be_visible()
        playwright.expect(page.locator(".bp-whole-map")).to_have_count(1)
    finally:
        context.close()
    assert len(errors) == 1 and "the map stopped partway" in errors[0], errors


def test_back_restores_a_phone_reader_at_half_rather_than_carrying_full_over(
    mobile_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    """History keeps only whether the reader was open, so Back restored it with the page's current position: full leaked onto
    an entry that was left at half."""
    page = mobile_page
    _open_consumer(page, explorer_consumer, "chapter/middle")
    host, handle = page.locator(".bp-dag-viewer"), page.locator(".bp-dag-sheet-handle")
    playwright.expect(host).to_have_class(re.compile(r"\bbp-dag-sheet-open\b"))
    _open_dependency(_reading_card(page, "chapter/middle"), explorer_consumer, "chapter/foundation").click()
    _consumer_ready(page, explorer_consumer, "chapter/foundation")
    playwright.expect(handle).to_have_attribute("title", "Expand reading")
    handle.click()
    playwright.expect(host).to_have_class(re.compile(r"\bbp-dag-sheet-full\b"))
    page.go_back()
    _consumer_ready(page, explorer_consumer, "chapter/middle")
    playwright.expect(host).to_have_class(re.compile(r"\bbp-dag-sheet-open\b"))
    playwright.expect(host).not_to_have_class(re.compile(r"\bbp-dag-sheet-full\b"))
    playwright.expect(handle).to_have_attribute("title", "Expand reading")


def test_search_keyboard_navigation_and_focus(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    search = _load_search_index(page)
    results, announcement = page.locator(".bp-dag-search-results"), page.locator("p.bp-dag-sr-only[aria-live]")
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
    page.locator(".bp-dag-browse > .bp-dag-button").focus()
    playwright.expect(results).to_be_hidden()
    search.fill("Target theorem")
    page.evaluate("window.__consumerDocument=document")
    search.press("Enter")
    _consumer_selected(page, explorer_consumer, "chapter/target")
    playwright.expect(page.locator(".bp-reading-body .bp-reading-current .bp-thmheading")).to_be_focused()
    assert page.url == explorer_consumer.route("chapter/target")
    assert page.evaluate("document===window.__consumerDocument") is True


def test_a_search_result_in_this_view_selects_the_drawn_node(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    search = _load_search_index(page)
    page.evaluate("window.__consumerDocument=document")
    for query, node_id in (("Target theorem", "chapter/target"), ("Middle lemma", "chapter/middle")):
        search.fill(query)
        if node_id.endswith("target"):
            search.press("Enter")
        else:
            page.locator(f'.bp-dag-result[data-result-id="{node_id}"]').click()
        _consumer_selected(page, explorer_consumer, node_id)
        playwright.expect(page.locator(".bp-reading-body .bp-reading-current .bp-thmheading")).to_be_focused()
        _assert_hit(page, _map_card(page, node_id))
        assert page.locator(".bp-whole-map .pm-result").count() > 1
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
    search = _load_search_index(page)
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
    search = _load_search_index(page)
    _browse_contents(page, "Statements in this section")
    search.fill("theorem")
    playwright.expect(page.locator(".bp-dag-inventory-title")).to_have_text(["Target theorem"])
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("1 statement · search the whole roadmap")
    results = page.locator(".bp-dag-search-results")
    playwright.expect(results).to_be_hidden()
    search.press("ArrowDown")
    playwright.expect(page.locator(".bp-dag-result").first).to_be_focused()
    page.keyboard.press("Escape")
    playwright.expect(results).to_be_hidden()
    page.get_by_role("button", name="search the whole roadmap").click()
    playwright.expect(results.locator('.bp-dag-result[data-result-id="remote/deep-theorem"]')).to_be_visible()


def test_wide_search_results_keep_a_rows_status_near_its_title(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    """At 1440px the results panel spanned about 1,080px, putting a row's title and status about 1,000px apart."""
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_consumer(page, explorer_consumer)
    search = _load_search_index(page)
    wrap = page.locator(".bp-dag-search-wrap").bounding_box()
    search.fill("theorem")
    results = page.locator(".bp-dag-search-results")
    playwright.expect(results).to_be_visible()
    box = results.bounding_box()
    assert wrap is not None and box is not None and wrap["width"] > 760
    assert box["width"] <= 720 + 1 and abs(box["x"] - wrap["x"]) < 1


def test_global_search_jumps_to_the_smallest_context(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    page.evaluate("window.__consumerDocument=document")
    search = _load_search_index(page)
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
    # The map goes to the result inside its own topic.
    playwright.expect(page.locator('.bp-whole-map .pm-part.pm-focus[data-part="remote"]')).to_have_count(1)
    assert page.evaluate("document===window.__consumerDocument") is True


def test_legacy_node_hash_resolves_through_the_global_index(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _offline(page, explorer_consumer.url)
    page.goto(explorer_consumer.url + "/dependencies/chapters/chapter.html#node=remote%2Fdeep-theorem")
    _consumer_selected(page, explorer_consumer, "remote/deep-theorem")
    assert page.url == explorer_consumer.route("remote/deep-theorem")
    # The map has arrived too, so no part file of its is loading when the page goes.
    _map_card(page, "remote/deep-theorem")
    navigations: list[str] = []
    page.on("request", lambda request: navigations.append(request.url) if request.is_navigation_request() else None)
    page.goto(explorer_consumer.url + "/dependencies/chapters/chapter.html#node=unknown%2Fitem")
    _consumer_ready(page, explorer_consumer)
    # The router loaded the whole-roadmap index before this commit, so no retry is in flight.
    playwright.expect(page.locator(".bp-dag-search")).not_to_have_attribute("aria-busy", "true")
    assert page.url.endswith("#node=unknown%2Fitem")
    # The old page, then the book page its script replaces it with.
    assert len(navigations) == 2 and "/dependencies/chapters/chapter.html" in navigations[0]
    assert "/roadmap/" in navigations[1]
    # Nothing is selected, in the book or on the map, once the map has drawn.
    playwright.expect(page.locator(".bp-whole-map .pm-part").first).to_be_visible()
    playwright.expect(page.locator(".bp-dag-has-selection")).to_have_count(0)
    playwright.expect(page.locator(".bp-whole-map .pm-result.pm-selected")).to_have_count(0)


def test_paginated_search_and_arrow_navigation_restore_focus(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer, "chapter/middle")
    search = _load_search_index(page)
    search.fill("Auxiliary result")
    more = page.locator(".bp-dag-search-results .bp-dag-load-more")
    # The count after "of" is every match, not the remainder.
    playwright.expect(more).to_have_text("Show 48 more of 60")
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
    playwright.expect(search).to_have_attribute("aria-busy", "true")
    _release(page, held)
    playwright.expect(search).not_to_have_attribute("aria-busy", "true")
    playwright.expect(page.locator(f'.bp-dag-result[data-result-id="{selected}"]')).to_be_focused()
    page.locator(".bp-dag-browse > .bp-dag-button").focus()
    playwright.expect(results).to_be_hidden()

    attempts = 0
    page.reload()
    _consumer_ready(page, explorer_consumer)
    search.fill("Auxiliary result")
    search.press("Escape")
    playwright.expect(results).to_be_hidden()
    playwright.expect(search).to_have_attribute("aria-busy", "true")
    _release(page, held)
    playwright.expect(search).not_to_have_attribute("aria-busy", "true")
    playwright.expect(results).to_be_hidden()
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text("Dependency chapter")


def test_direct_list_view_has_current_shown_and_hidden_counts(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _offline(page, explorer_consumer.url)
    page.goto(explorer_consumer.url + "/dependencies/chapters/chapter.html#view=list&status=fully_proved")
    _consumer_ready(page, explorer_consumer)
    playwright.expect(page.locator(".bp-dag-inventory")).to_be_visible()
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("1 statement")
    # The list holds the chapter's 63 statements: the filter shows the one fully proved and offers back the other 62.
    playwright.expect(page.locator(".bp-dag-hidden-button")).to_have_text("Show 62 filtered out")
    playwright.expect(page.locator(".bp-dag-inventory-row")).to_have_count(1)


def test_reselect_does_not_duplicate_history_and_back_restores_selection(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    page.evaluate("window.__consumerDocument=document")
    initial = page.evaluate("history.length")
    _load_search_index(page).fill("Foundation")
    page.locator('.bp-dag-result[data-result-id="chapter/foundation"]').click()
    _consumer_selected(page, explorer_consumer, "chapter/foundation")
    assert page.evaluate("history.length") == initial + 1
    # Picking the selected card again on the map is not another entry.
    _map_card(page, "chapter/foundation").click()
    _paint_frame(page)
    _consumer_selected(page, explorer_consumer, "chapter/foundation")
    assert page.evaluate("history.length") == initial + 1
    _open_dependency(_reading_card(page, "chapter/foundation"), explorer_consumer, "chapter/middle").click()
    _consumer_selected(page, explorer_consumer, "chapter/middle")
    assert page.evaluate("history.length") == initial + 2
    page.go_back()
    _consumer_selected(page, explorer_consumer, "chapter/foundation")
    _map_card(page, "chapter/foundation")
    assert page.url == explorer_consumer.route("chapter/foundation")
    page.go_back()
    _consumer_ready(page, explorer_consumer)
    assert page.url == explorer_consumer.route("chapter")
    assert page.evaluate("document===window.__consumerDocument") is True


def test_hiding_the_selected_status_keeps_the_selection_and_dims_its_card(
    desktop_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
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
    # The map dims the statuses the filter leaves out, so the selection stays and its card dims; since the map hides
    # nothing, the change is announced as the filter.
    host, filtered = page.locator(".bp-dag-viewer"), f"Filter: {len(statuses) - 1} of {len(statuses)} statuses"
    playwright.expect(summary).to_have_text(filtered)
    playwright.expect(host).to_have_class(re.compile(r"\bbp-dag-has-selection\b"))
    playwright.expect(host).to_have_attribute("data-map-statuses", re.compile(rf"^(?!.*\b{target_status}\b)"))
    card = page.locator('.bp-whole-map .pm-result.pm-selected[data-result="chapter/target"]')
    playwright.expect(card).to_have_class(re.compile(r"\bpm-dimmed\b"))
    playwright.expect(page.locator("p.bp-dag-sr-only[aria-live]")).to_have_text(filtered)
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
        data = page.request.get(urljoin(page.url, download.get_attribute("href"))).json()
        assert data["schema"] == site_index.SEARCH_SCHEMA and len(data["nodes"]) == len(explorer_consumer.records)
        reference = _open_dependency(card, explorer_consumer, "chapter/middle")
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
    reference = _open_dependency(
        source.locator('.bp-thmwrapper[data-autoform-node-id="chapter/target"]'), explorer_consumer, "chapter/middle"
    )
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
    """Site CSS once gave the canvas a 30rem floor; the map drawn in the stage fills it exactly, from a legacy alias too."""
    desktop_page.set_viewport_size(viewport)
    page = _open_site(desktop_page, site_url, "dependencies.html")
    playwright.expect(page.locator(".bp-whole-map .pm-part").first).to_be_visible()
    canvas = page.locator(".bp-whole-map .pm-stage > svg").bounding_box()
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
    current = page.locator('.bp-dag-breadcrumb li[aria-current="page"]')
    playwright.expect(current).to_have_text("Home")
    playwright.expect(current.locator("a")).to_have_count(0)
    playwright.expect(page.locator(".bp-dag-book")).to_have_count(0)
    assert page.url == site_url + "/index.html"
    _assert_hit(page, current)
    algebra = page.locator('.bp-dag-navigation-link[data-navigation-node-id="algebra"]')
    playwright.expect(algebra).to_have_attribute("href", _site_route(page, site_url, "algebra"))
    _assert_hit(page, algebra)
    # A static publication is not the dashboard's live-overlay service.
    page.route("**/__autoform/live.json", lambda route: route.fulfill(status=404, body=""))
    page.reload()
    _ready_site(page)
    playwright.expect(page.locator("#bp-live-activity")).to_have_count(0)


# None shares the module's unparametrized site rather than building a second copy of it.
@pytest.mark.parametrize(("site_url", "tracked"), [(None, True), ("tracks_lean: false", False)], indirect=["site_url"])
def test_a_project_that_tracks_no_lean_offers_no_lean_names_or_statuses(
    desktop_page: object, site_url: str, tracked: bool
) -> None:
    """A project that tracks no Lean has none to search for and no status to filter by or show; Browse rows keep their place."""
    page = _open_site(desktop_page, site_url, "index.html")
    offer = "Search titles and Lean names" if tracked else "Search titles"
    nothing = "No matching title, statement, " + ("article id, or Lean name." if tracked else "or article id.")
    search = page.locator(".bp-dag-search")
    playwright.expect(search).to_have_attribute("placeholder", offer)
    playwright.expect(search).to_have_attribute("aria-label", offer)
    search.fill("zzz")
    playwright.expect(page.locator(".bp-dag-search-results")).to_contain_text(nothing)
    search.fill("Base")
    results = page.locator(".bp-dag-result")
    playwright.expect(results.first).to_be_visible()
    playwright.expect(page.locator(".bp-dag-result-meta")).to_have_count(results.count() if tracked else 0)
    search.fill("")
    if tracked:
        playwright.expect(page.locator(".bp-dag-filter-summary")).to_be_visible()
    else:
        playwright.expect(page.locator(".bp-dag-filter-summary")).to_be_hidden()
    _browse_contents(page, "All statements")
    meta = page.locator('.bp-dag-inventory-row[data-inventory-node-id="algebra/base"] .bp-dag-inventory-meta')
    playwright.expect(meta).to_be_visible()
    playwright.expect(meta).to_have_text(re.compile(r".+ · Algebra") if tracked else "Algebra")
    # An old link's status filter narrows the list only where the reader can see the filter and undo it.
    page.goto("about:blank")
    page.goto(f"{site_url}/index.html#view=list&status=fully_proved")
    playwright.expect(page.locator(".bp-dag-inventory")).to_be_visible()
    if tracked:
        playwright.expect(page.locator(".bp-dag-hidden-button")).to_be_visible()
    else:
        playwright.expect(page.locator(".bp-dag-hidden-button")).to_be_hidden()


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
            handle = page.get_by_role("button", name=re.compile(r", reading$"))
            if handle.get_attribute("aria-expanded") == "false":
                handle.click()
        playwright.expect(activity).to_be_visible()

    assert_landing_panel()
    algebra_route = _site_route(page, site_url, "algebra")
    for _ in range(2):
        if width == 390:
            outline = page.get_by_role("button", name="Contents", exact=True)
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
    handle = page.get_by_role("button", name=re.compile(r", reading$"))
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    # The handle shows the page's title, so its name starts with it, and voice control can say what it sees.
    assert handle.get_attribute("aria-label") == handle.inner_text().strip() + ", reading"
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
    # A second tap covers the map; a third returns to the peek.
    handle.click()
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_class(re.compile(r"\bbp-dag-sheet-full\b"))
    playwright.expect(headings).to_have_count(1)
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    playwright.expect(headings).to_have_count(1)
    playwright.expect(headings).to_have_text("Explorer fixture")


def test_on_a_phone_the_status_filter_and_search_results_open_in_view(mobile_page: object, site_url: str) -> None:
    """Toolbar popovers must remain on-screen and clickable over the phone's reading pane.

    They open over the reader at half, its default; at full the toolbar steps aside for reading.
    """
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
    playwright.expect(page.get_by_role("button", name=re.compile(r", reading$"))).to_have_attribute(
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
    # At 320px the legend's 300px width ran past the screen from Filter's place; it hangs from the toolbar instead.
    page.set_viewport_size({"width": 320, "height": 640})
    playwright.expect(popover).to_be_visible()
    narrow = popover.bounding_box()
    assert narrow is not None and 0 <= narrow["x"] and narrow["x"] + narrow["width"] <= 320
    _assert_hit(page, popover.locator(".bp-dag-check-count").first)
    page.set_viewport_size({"width": viewport["width"], "height": viewport["height"]})
    # Contents follows search on screen as in the tab order, on the same row: it led the tab order from the row below search.
    assert page.locator(".bp-dag-toolbar > :is(.bp-dag-search-wrap, .bp-dag-outline-toggle)").evaluate_all(
        "items => items.map(item => item.className)") == ["bp-dag-search-wrap", "bp-dag-button bp-dag-outline-toggle"]
    topics, field = page.get_by_role("button", name="Contents", exact=True).bounding_box(), page.locator(".bp-dag-search").bounding_box()
    assert topics is not None and field is not None and topics["x"] > field["x"] + field["width"] - 1
    assert abs(topics["y"] + topics["height"] / 2 - field["y"] - field["height"] / 2) < 2
    search = _load_search_index(page)
    search.fill("Result")
    results = page.locator(".bp-dag-search-results")
    playwright.expect(results).to_be_visible()
    box = results.bounding_box()
    assert box is not None
    assert 0 <= box["x"] and box["x"] + box["width"] <= viewport["width"]
    assert box["y"] + box["height"] <= viewport["height"]
    assert box["width"] >= viewport["width"] - 24 - 1  # min(576px, 100vw - 24px)
    _assert_hit(page, page.locator(".bp-dag-result").first)
    # Where the results reach the half-open reader, they paint over it.
    assert page.evaluate(
        """() => {
          const shown = document.querySelector('.bp-dag-search-results');
          const r = shown.getBoundingClientRect();
          const p = document.querySelector('.bp-reading-pane').getBoundingClientRect();
          const top = Math.max(r.top, p.top), bottom = Math.min(r.bottom, p.bottom);
          if (bottom - top <= 2) return "no overlap";
          const x = (Math.max(r.left, p.left) + Math.min(r.right, p.right)) / 2;
          return shown.contains(document.elementFromPoint(x, (top + bottom) / 2));
        }"""
    ) is True


def test_the_rail_under_the_open_details_leaves_the_tab_order(desktop_page: object, site_url: str) -> None:
    """An explicitly closed narrow outline is absent from Tab; an open outline and wide rail remain usable."""
    page = desktop_page
    page.set_viewport_size({"width": 1000, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    _open_site(page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    outline = page.locator(".bp-dag-navigation")
    playwright.expect(outline).to_be_hidden()
    page.locator(".bp-dag-browse > .bp-dag-button").focus()
    for _ in range(8):
        page.keyboard.press("Tab")
        assert not outline.evaluate("rail => rail.contains(document.activeElement)")
    toggle = page.get_by_role("button", name="Contents", exact=True)
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
    playwright.expect(inventory.locator(".bp-dag-inventory-summary")).to_have_text("2 statements")
    reader = page.locator(".bp-reading-pane").bounding_box()
    assert reader is not None
    for item in (inventory.locator(".bp-dag-inventory-head h2"), row, row.locator(".bp-dag-inventory-title")):
        box = item.bounding_box()
        assert box is not None and box["x"] + box["width"] <= reader["x"] + 1
    _assert_hit(page, row)
    page.get_by_role("button", name="Back to map", exact=True).click()
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
            {"id": f"more/{key}", "kind": "node", "status": key, "title": f"More {key}"}
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
    search = _load_search_index(page)
    for width in (1000, 800):
        page.set_viewport_size({"width": width, "height": 900})
        search.fill("")
        search.fill("Result")
        playwright.expect(page.locator(".bp-dag-search-results")).to_be_visible()
        assert _paints_over_the_details(page, ".bp-dag-result", ".bp-dag-search-results") is True
        # Results read at least 576px wide whatever the box beside them, and stay on screen.
        box = page.locator(".bp-dag-search-results").bounding_box()
        assert box is not None and box["width"] >= 576 - 1 and box["x"] + box["width"] <= width + 1


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
    playwright.expect(library).to_have_text("Home")
    playwright.expect(library).to_have_attribute("href", site_url + "/index.html")
    _assert_hit(page, library)
    for width in (1000, 390, 1280):
        page.set_viewport_size({"width": width, "height": 900 if width != 390 else 844})
        playwright.expect(crumbs).to_be_visible()
        playwright.expect(crumbs.locator('[aria-current="page"]')).to_have_text("Algebra")
        playwright.expect(page.get_by_role("heading", level=1)).to_have_count(1)
    _open_site(page, site_url, "index.html")
    playwright.expect(crumbs).to_have_count(1)
    playwright.expect(crumbs).to_be_visible()
    playwright.expect(crumbs.locator('li[aria-current="page"]')).to_have_text("Home")
    playwright.expect(page.locator(".bp-dag-book")).to_have_count(0)


_STATEMENT_PAGE = "dependencies/chapters/algebra.html#node=algebra%2Fresult"


def _widths(page: object) -> dict[str, float]:
    """Each pane's width, 0 for a hidden one, and the body's."""
    return page.evaluate("""() => Object.fromEntries([["body", ".bp-dag-body"], ["topics", ".bp-dag-navigation"],
      ["map", ".bp-dag-main"], ["reading", ".bp-reading-pane"]].map(([name, selector]) =>
      [name, document.querySelector(selector).getBoundingClientRect().width]))""")


def _drag(page: object, splitter: object, x: float) -> None:
    box = splitter.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(x, box["y"] + box["height"] / 2, steps=8)
    page.mouse.up()
    _paint_frame(page)


def _inside(page: object, inner: str, outer: str) -> bool:
    """Whether every element matching INNER lies wholly inside the first element matching OUTER."""
    return page.evaluate("""([inner, outer]) => {
      const b = document.querySelector(outer).getBoundingClientRect();
      return [...document.querySelectorAll(inner)].every(element => {
        const a = element.getBoundingClientRect();
        return a.left >= b.left - 1 && a.right <= b.right + 1 && a.top >= b.top - 1 && a.bottom <= b.bottom + 1;
      });
    }""", [inner, outer])


def test_the_reading_pane_drags_to_80_percent_and_keeps_that_width_on_the_next_page(desktop_page: object, site_url: str) -> None:
    """With Contents hidden the reader drags to 80% of the body; the map keeps 160px whatever changes later."""
    page = _open_site(desktop_page, site_url, _STATEMENT_PAGE)
    topics, reading = page.get_by_role("separator", name="Resize Contents"), page.get_by_role("separator", name="Resize the reading pane")
    before = _widths(page)
    # With Contents shown, the map keeps 160px, and its zoom bar wraps to fit it.
    _drag(page, reading, 10)
    narrow = _widths(page)
    assert narrow["reading"] == pytest.approx(before["body"] - before["topics"] - 160, abs=2)
    assert abs(int(reading.get_attribute("aria-valuenow")) - 100 * narrow["reading"] / narrow["body"]) <= 1
    assert abs(int(reading.get_attribute("aria-valuemin")) - 100 * 300 / narrow["body"]) <= 1
    assert abs(int(reading.get_attribute("aria-valuemax")) - 100 * narrow["reading"] / narrow["body"]) <= 1
    assert _inside(page, ".bp-whole-map .pm-tools button", ".bp-dag-main")
    # Each splitter sits in tab order beside its pane, and Contents' tab before the map.
    assert page.evaluate("""() => document.querySelector('[aria-label="Resize Contents"]').previousElementSibling.matches('.bp-dag-navigation') &&
      document.querySelector('[aria-label="Resize the reading pane"]').nextElementSibling.matches('.bp-reading-pane') &&
      document.querySelector('.bp-dag-main').firstElementChild.matches('.bp-dag-pane-tab-topics')""")
    topics.focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator(".bp-dag-navigation")).to_be_hidden()
    playwright.expect(page.get_by_role("button", name="Show Contents")).to_be_focused()
    body = page.locator(".bp-dag-body").bounding_box()
    _drag(page, reading, body["x"] + 0.2 * body["width"])
    wide = _widths(page)
    assert wide["reading"] == pytest.approx(0.8 * wide["body"], abs=2)
    assert wide["map"] == pytest.approx(0.2 * wide["body"], abs=2)
    # The choice holds on the next page.
    _open_site(page, site_url, "dependencies/chapters/topology.html#node=topology%2Fresult")
    again = _widths(page)
    assert again["topics"] == 0 and again["reading"] == pytest.approx(wide["reading"], abs=2)
    # Contents coming back, or growing to its widest, takes from the reader and leaves the map its 160px.
    page.get_by_role("button", name="Show Contents").click()
    playwright.expect(page.locator(".bp-dag-navigation")).to_be_visible()
    playwright.expect(topics).to_be_focused()
    assert _widths(page)["map"] == pytest.approx(160, abs=2)
    page.keyboard.press("End")
    _paint_frame(page)
    widest = _widths(page)
    assert widest["topics"] == pytest.approx(480, abs=2) and widest["map"] == pytest.approx(160, abs=2)
    _assert_hit(page, topics)
    # A double-click puts the reader back at its usual width, and Delete does the same for Contents, as does a Mac's
    # delete key.
    reading.dblclick()
    topics.focus()
    page.keyboard.press("Delete")
    _paint_frame(page)
    usual = _widths(page)
    assert usual["topics"] == pytest.approx(216, abs=2)
    assert usual["reading"] == pytest.approx(max(360, 0.39 * usual["body"]), abs=2)
    page.keyboard.press("End")
    page.keyboard.press("Backspace")
    _paint_frame(page)
    assert _widths(page)["topics"] == pytest.approx(216, abs=2)
    # Home gives the reader its smallest width.
    reading.focus()
    page.keyboard.press("Home")
    _paint_frame(page)
    assert _widths(page)["reading"] == pytest.approx(300, abs=2)
    # Contents' splitter, focused when the window narrows to the drawer's layout, hands focus to the drawer's toggle.
    topics.focus()
    page.set_viewport_size({"width": 1000, "height": 800})
    playwright.expect(page.get_by_role("button", name="Contents", exact=True)).to_be_focused()


def test_a_pane_hides_from_its_splitter_or_the_map_only_button_and_its_tab_brings_it_back(
    desktop_page: object, site_url: str,
) -> None:
    page = _open_site(desktop_page, site_url, _STATEMENT_PAGE)
    reading = page.get_by_role("separator", name="Resize the reading pane")
    full = _widths(page)
    # Keys move the splitter: Left widens the reader by 16px, Shift+Left by 64.
    reading.focus()
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("Shift+ArrowLeft")
    _paint_frame(page)
    assert _widths(page)["reading"] == pytest.approx(full["reading"] + 80, abs=2)
    # Dragged nearly shut, the reader hides when the drag ends, and the map takes its place.
    body = page.locator(".bp-dag-body").bounding_box()
    _drag(page, reading, body["x"] + body["width"] - 40)
    playwright.expect(page.locator(".bp-reading-pane")).to_be_hidden()
    show = page.get_by_role("button", name="Show the reading pane")
    playwright.expect(show).to_be_focused()
    assert _widths(page)["map"] == pytest.approx(full["body"] - full["topics"], abs=2)
    # The tab brings it back at the width it had before the drag.
    show.click()
    playwright.expect(page.locator(".bp-reading-pane")).to_be_visible()
    assert _widths(page)["reading"] == pytest.approx(full["reading"] + 80, abs=2)
    # A drag ends when the browser takes back its pointer.
    box = reading.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    viewer = page.locator(".bp-dag-viewer")
    playwright.expect(viewer).to_have_class(re.compile(r"\bbp-dag-resizing\b"))
    reading.dispatch_event("lostpointercapture")
    playwright.expect(viewer).not_to_have_class(re.compile(r"\bbp-dag-resizing\b"))
    page.mouse.up()
    # Map only hides both panes, and pressed again brings both back.
    map_only = page.get_by_role("button", name="Map only")
    map_only.click()
    playwright.expect(map_only).to_have_attribute("aria-pressed", "true")
    assert _widths(page)["map"] == pytest.approx(full["body"], abs=2)
    playwright.expect(page.get_by_role("separator")).to_have_count(0)
    map_only.click()
    playwright.expect(map_only).to_have_attribute("aria-pressed", "false")
    playwright.expect(page.locator(".bp-dag-navigation")).to_be_visible()
    playwright.expect(page.locator(".bp-reading-pane")).to_be_visible()


def test_a_reader_hidden_over_the_list_leaves_its_tab_focused_and_a_pick_then_focuses_the_map(
    desktop_page: object, site_url: str,
) -> None:
    # Hidden over the statement list, the reader leaves its tab focused beside the rows, not over them.
    page = _open_site(desktop_page, site_url, _STATEMENT_PAGE)
    _browse_contents(page, "All statements")
    page.get_by_role("separator", name="Resize the reading pane").focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator(".bp-reading-pane")).to_be_hidden()
    playwright.expect(page.get_by_role("button", name="Show the reading pane")).to_be_focused()
    assert page.evaluate("""() => { const tab = document.querySelector('.bp-dag-pane-tab-reading').getBoundingClientRect();
      return [...document.querySelectorAll('.bp-dag-inventory-row')].every(row => row.getBoundingClientRect().right <= tab.left + 1); }""")
    # With only the reader hidden, a statement picked from the list focuses the map rather than the hidden reader.
    page.locator('.bp-dag-inventory-row[data-inventory-node-id="algebra/base"]').focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_attribute("data-map-start", "algebra/base")
    page.wait_for_function("() => document.querySelector('.bp-whole-map').contains(document.activeElement)")


def test_between_721_and_1100px_only_the_reader_resizes_and_map_only_closes_the_topics_drawer(
    desktop_page: object, site_url: str,
) -> None:
    page = desktop_page
    page.set_viewport_size({"width": 1000, "height": 800})
    # A stored preference of another shape is ignored, and replaced on the next change. The site is served from the root.
    page.add_init_script("localStorage.setItem('autoform:book-panes:v1:/', '5')")
    _open_site(page, site_url, _STATEMENT_PAGE)
    playwright.expect(page.get_by_role("separator", name="Resize Contents")).to_be_hidden()
    reading = page.get_by_role("separator", name="Resize the reading pane")
    full = _widths(page)
    # The open drawer floats over the map, so it doesn't lower the reader's limit.
    toggle = page.get_by_role("button", name="Contents", exact=True)
    toggle.click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "true")
    _drag(page, reading, 10)
    assert _widths(page)["reading"] == pytest.approx(full["body"] - 160, abs=2)
    map_only = page.get_by_role("button", name="Map only")
    map_only.click()
    playwright.expect(map_only).to_have_attribute("aria-pressed", "true")
    playwright.expect(map_only).to_have_attribute("title", "Show the reading pane")
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    playwright.expect(page.locator(".bp-dag-navigation")).to_be_hidden()
    assert _widths(page)["map"] == pytest.approx(full["body"], abs=2)
    # Map only here leaves the wide layout's Contents as it was.
    page.set_viewport_size({"width": 1280, "height": 800})
    playwright.expect(page.locator(".bp-dag-navigation")).to_be_visible()


def test_a_width_no_drag_could_give_is_dropped_and_a_reader_saved_too_wide_leaves_the_map_160px(
    desktop_page: object, site_url: str,
) -> None:
    """A reader saved on a wider screen leaves the map its 160px in each layout with a reader, Contents shown or not.

    The choice is this site's: another site on the host hid its Contents, which stay shown here.
    """
    page = desktop_page
    page.add_init_script("""localStorage.setItem('autoform:book-panes:v1:/', JSON.stringify({topics: -50, reading: 0.94}));
      localStorage.setItem('autoform:book-panes:v1:/other-book/', JSON.stringify({topicsHidden: true}))""")
    _open_site(page, site_url, _STATEMENT_PAGE)
    shown = _widths(page)
    assert shown["topics"] == pytest.approx(216, abs=2) and shown["map"] == pytest.approx(160, abs=2)
    page.get_by_role("separator", name="Resize Contents").focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator(".bp-dag-navigation")).to_be_hidden()
    assert _widths(page)["map"] == pytest.approx(160, abs=2)
    page.set_viewport_size({"width": 1000, "height": 800})
    playwright.expect(page.get_by_role("separator", name="Resize Contents")).to_be_hidden()
    assert _widths(page)["map"] == pytest.approx(160, abs=2)


def test_without_the_map_a_statement_picked_with_the_reader_hidden_focuses_the_filter(
    desktop_page: object, site_url: str,
) -> None:
    page = _offline(desktop_page, site_url)
    page.route("**/javascripts/project-map.js", lambda route: route.abort())
    page.goto(f"{site_url}/{_STATEMENT_PAGE}")
    playwright.expect(page.locator(".bp-dag-stage > .bp-dag-map-unavailable")).to_have_count(1)
    playwright.expect(page.locator(".bp-reading-body > .bp-reading-source")).to_have_count(1)
    page.get_by_role("separator", name="Resize the reading pane").focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator(".bp-reading-pane")).to_be_hidden()
    _browse_contents(page, "All statements")
    page.locator('.bp-dag-inventory-row[data-inventory-node-id="algebra/base"]').focus()
    page.keyboard.press("Enter")
    playwright.expect(page.locator(".bp-dag-filter-summary")).to_be_focused()


def test_a_touch_screen_gets_24px_splitters_on_the_panes_edges(webkit_browser: object, site_url: str) -> None:
    with _page(webkit_browser, viewport={"width": 1280, "height": 800}, has_touch=True) as page:
        _open_site(page, site_url, _STATEMENT_PAGE)
        for name, edge in (("Resize Contents", "right"), ("Resize the reading pane", "left")):
            splitter = page.get_by_role("separator", name=name)
            box = splitter.bounding_box()
            pane = page.locator(".bp-dag-navigation" if edge == "right" else ".bp-reading-pane").bounding_box()
            assert box["width"] == pytest.approx(24, abs=0.5), name
            assert box["x"] + box["width"] / 2 == pytest.approx(pane["x"] + (pane["width"] if edge == "right" else 0), abs=1), name
        # The edge moves as far as the pointer does, though pressed 8px off the splitter's middle.
        topics = page.get_by_role("separator", name="Resize Contents").bounding_box()
        start = _widths(page)
        page.mouse.move(topics["x"] + topics["width"] / 2 + 8, topics["y"] + topics["height"] / 2)
        page.mouse.down()
        page.mouse.move(topics["x"] + topics["width"] / 2 + 48, topics["y"] + topics["height"] / 2, steps=8)
        page.mouse.up()
        _paint_frame(page)
        assert _widths(page)["topics"] == pytest.approx(start["topics"] + 40, abs=2)
        # It still drags: the reader to its widest leaves the map 160px.
        before = _widths(page)
        _drag(page, page.get_by_role("separator", name="Resize the reading pane"), 10)
        assert _widths(page)["reading"] == pytest.approx(before["body"] - before["topics"] - 160, abs=2)


@pytest.mark.parametrize("site_url", [None, "tracks_lean: false"], indirect=True)
def test_a_page_opened_with_the_reader_hidden_leaves_focus_where_it_was(desktop_page: object, site_url: str) -> None:
    """Opening a page focuses nothing in its place: neither the filter nor search takes the focus the map would."""
    page = desktop_page
    page.add_init_script("localStorage.setItem('autoform:book-panes:v1:/', JSON.stringify({readingHidden: true}))")
    _open_site(page, site_url, _STATEMENT_PAGE)
    playwright.expect(page.locator(".bp-reading-pane")).to_be_hidden()
    page.wait_for_function("() => document.querySelector('.bp-whole-map svg[tabindex=\"0\"]')")
    _paint_frame(page)
    assert page.evaluate("() => !document.activeElement.matches('.bp-dag-filter-summary, .bp-dag-search')")


def test_topics_drags_wider_and_dragged_shut_comes_back_at_that_width(desktop_page: object, site_url: str) -> None:
    page = _open_site(desktop_page, site_url, _STATEMENT_PAGE)
    topics = page.get_by_role("separator", name="Resize Contents")
    start = _widths(page)
    body = page.locator(".bp-dag-body").bounding_box()
    _drag(page, topics, body["x"] + start["topics"] + 40)
    wider = _widths(page)
    assert wider["topics"] == pytest.approx(start["topics"] + 40, abs=3)
    _drag(page, topics, body["x"] + 40)
    playwright.expect(page.locator(".bp-dag-navigation")).to_be_hidden()
    show = page.get_by_role("button", name="Show Contents")
    playwright.expect(show).to_be_focused()
    show.click()
    _paint_frame(page)
    assert _widths(page)["topics"] == pytest.approx(wider["topics"], abs=2)


def test_a_stored_width_that_is_not_a_number_is_dropped(desktop_page: object, site_url: str) -> None:
    page = desktop_page
    page.add_init_script("localStorage.setItem('autoform:book-panes:v1:/', JSON.stringify({topics: 'wide', reading: 'x'}))")
    _open_site(page, site_url, _STATEMENT_PAGE)
    widths = _widths(page)
    assert widths["topics"] == pytest.approx(216, abs=2)
    assert widths["reading"] == pytest.approx(max(360, 0.39 * widths["body"]), abs=2)


def test_forced_colours_keep_the_splitter_line_and_map_onlys_pressed_look(desktop_page: object, site_url: str) -> None:
    page = _open_site(desktop_page, site_url, _STATEMENT_PAGE)
    page.emulate_media(forced_colors="active")
    highlight = page.evaluate("""() => { const probe = document.createElement('div'); probe.style.background = 'Highlight';
      document.body.append(probe); const colour = getComputedStyle(probe).backgroundColor; probe.remove(); return colour; }""")
    page.get_by_role("separator", name="Resize the reading pane").hover()
    page.wait_for_function("""colour => getComputedStyle(document.querySelector('[aria-label="Resize the reading pane"]'), '::after')
      .backgroundColor === colour""", arg=highlight, timeout=5_000)
    map_only = page.get_by_role("button", name="Map only")
    map_only.click()
    playwright.expect(map_only).to_have_attribute("aria-pressed", "true")
    # A 2px border marks it pressed, and its reach stays 44px.
    assert map_only.evaluate("button => [getComputedStyle(button).borderTopWidth, getComputedStyle(button, '::after').top]") == [
        "2px", "-8px"]


def test_the_map_repacks_after_a_pane_drag_not_during_it(desktop_page: object, site_url: str) -> None:
    """A drag that turns the stage from portrait to landscape repacks the map when it ends, not mid-drag."""
    page = _open_site(desktop_page, site_url, _STATEMENT_PAGE)
    page.get_by_role("separator", name="Resize Contents").focus()
    page.keyboard.press("Enter")
    _paint_frame(page)
    stage = page.locator(".bp-dag-stage")
    portrait = stage.bounding_box()
    assert portrait is not None and portrait["width"] < portrait["height"]
    map_host = page.locator(".bp-whole-map")
    playwright.expect(map_host).to_have_attribute("data-pm-packed", "portrait")
    splitter = page.get_by_role("separator", name="Resize the reading pane").bounding_box()
    body = page.locator(".bp-dag-body").bounding_box()
    assert splitter is not None and body is not None
    page.mouse.move(splitter["x"] + splitter["width"] / 2, splitter["y"] + splitter["height"] / 2)
    page.mouse.down()
    page.mouse.move(body["x"] + body["width"] - 300, splitter["y"] + splitter["height"] / 2, steps=8)
    _paint_frame(page)
    _paint_frame(page)
    landscape = stage.bounding_box()
    assert landscape is not None and landscape["width"] > landscape["height"]
    assert map_host.get_attribute("data-pm-packed") == "portrait"
    page.mouse.up()
    playwright.expect(map_host).to_have_attribute("data-pm-packed", "landscape")


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
    _paint_frame(page)
    crumbs = page.locator(".bp-dag-breadcrumb li")
    assert crumbs.count() >= 2
    # Resizing intentionally refolds this trail. Measure one native DOM
    # generation, not separate nth locators across that replacement.
    boxes = crumbs.evaluate_all("nodes=>nodes.map(node=>{const r=node.getBoundingClientRect();return {y:r.y,width:r.width,height:r.height};})")
    assert len(boxes) >= 2 and all(box["width"] > 0 and box["height"] > 0 for box in boxes)
    assert len({round(box["y"]) for box in boxes}) == 1
    assert page.locator(".bp-dag-head").evaluate("head => head.scrollWidth <= head.clientWidth + 1")


def test_a_reading_pane_table_keeps_its_words_whole(desktop_page: object, site_url: str) -> None:
    """The pane breaks a long word anywhere, so a table squeezed into it broke its headers mid-word ("Dependen t")."""
    page = _open_site(desktop_page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    headers, path = page.locator(".bp-reading-body > .bp-reading-source").evaluate("""source => {
      const table = html => { const node = source.appendChild(document.createElement('table')); node.innerHTML = html; return node; };
      const words = ['Dependent', 'Unwitnessed', 'Disposition', 'Statement', 'Reference', 'Coverage', 'Evidence'];
      const wide = table('<tr>' + words.map(word => '<th>' + word + '</th>').join('') + '</tr>');
      const named = table('<tr><td><code>Mathlib.AlgebraicGeometry.EllipticCurve.Projective.Point.toAffineLiftZeroEquivOfIsUnit</code></td></tr>');
      const lines = node => {
        const range = document.createRange(); range.selectNodeContents(node);
        return new Set(Array.from(range.getClientRects(), rect => Math.round(rect.top))).size;
      };
      return [Array.from(wide.querySelectorAll('th'), lines), lines(named.querySelector('code'))];
    }""")
    # Seven headers don't fit the pane: each keeps its line, and the table scrolls instead.
    assert headers == [1] * 7
    # Material breaks code anywhere, so a long name in a cell doesn't widen its table past the pane.
    assert path > 1


def _assert_site_chrome_is_reachable(page: object) -> None:
    """The header and heading are visible, and each is the element a click would hit."""
    for chrome in (
        page.locator(".md-header"),
        page.get_by_role("heading", level=1),
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
        playwright.expect(page.locator('.bp-reading-source .bp-thmwrapper[data-autoform-node-id="algebra/result"]')).to_contain_text(
            "The main result."
        )
        _assert_site_chrome_is_reachable(page)
        playwright.expect(page.locator(".bp-dag-integrated")).to_have_count(0)
        # Without JavaScript, a legacy entrance refreshes to its canonical page.
        page.goto(f"{site_url}/dependencies/chapters/algebra.html")
        page.wait_for_url(_site_route(page, site_url, "algebra"))
        playwright.expect(page.locator('.bp-reading-source .bp-thmwrapper[data-autoform-node-id="algebra/result"]')).to_contain_text(
            "The main result."
        )


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
    """The canonical mathematical page is already the reader; Home is navigation, not a second self-link."""
    page = _open_site(desktop_page, site_url, "dependencies/chapters/algebra.html")
    assert page.url == _site_route(page, site_url, "algebra")
    library = page.locator(".bp-dag-book")
    playwright.expect(library).to_have_attribute("href", site_url + "/index.html")
    playwright.expect(library).to_have_text("Home")
    _assert_hit(page, library)
    playwright.expect(page.get_by_role("heading", level=1)).to_have_count(1)
    playwright.expect(page.get_by_role("heading", level=1)).to_have_text("Algebra")
    _reading_card(page, "algebra/base")
    playwright.expect(_reading_card(page, "algebra/result")).to_contain_text("The main result.")


def test_global_command_k_opens_landing_search_while_unfocused_slash_stays_native(desktop_page: object, site_url: str) -> None:
    """Only the modified shortcut is global; slash requires explorer focus."""
    page = _open_site(desktop_page, site_url, "index.html")
    page.evaluate("() => {document.body.tabIndex=-1;document.body.focus();}")
    page.keyboard.press("/")
    assert page.evaluate("() => document.activeElement===document.body")
    page.keyboard.press("Control+K")
    playwright.expect(page.locator(".bp-dag-search")).to_be_focused()


def test_slash_inside_the_explorer_opens_its_search_but_an_input_keeps_its_keys(
    desktop_page: object, explorer_consumer: _ExplorerConsumer
) -> None:
    """Focus inside the explorer lets "/" open its search, but no shortcut takes a key from an input."""
    page = desktop_page
    _open_consumer(page, explorer_consumer)
    search, wrap = page.locator(".bp-dag-search"), page.locator(".bp-dag-search-wrap")
    playwright.expect(search).to_have_attribute("placeholder", "Search titles and Lean names")
    playwright.expect(search).not_to_have_attribute("aria-busy", "true")
    # The box draws a masked magnifier and a "/" key hint, which focus hides.
    assert "svg" in wrap.evaluate("element => getComputedStyle(element, '::before').webkitMaskImage")
    hint = "element => [getComputedStyle(element, '::after').content, getComputedStyle(element, '::after').display]"
    assert wrap.evaluate(hint) == ['"/"', "block"]
    map_view = page.locator(".bp-whole-map .pm-stage > svg")
    map_view.focus()
    playwright.expect(map_view).to_be_focused()
    page.keyboard.press("/")
    playwright.expect(search).to_be_focused()
    assert wrap.evaluate(hint)[1] == "none"
    page.locator(".bp-dag-filter-summary").click()
    status = page.locator(".bp-dag-filter-popover fieldset input[type=checkbox]").first
    status.focus()
    page.keyboard.press("Control+K")
    playwright.expect(status).to_be_focused()


def test_complete_reader_math_survives_navigation_and_paginated_authored_relations(
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
        # Generated formal-reference titles now have their own safe math.
        # These exact counts still describe the complete authored statement;
        # the reference-title native test independently checks its controls.
        playwright.expect(card.locator(".arithmatex:not(.bp-dependencies .arithmatex)")).to_have_count(count)
        playwright.expect(card.locator("mjx-container:not(.bp-dependencies mjx-container)")).to_have_count(count)
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
    remote_card = full_math("topology/result", 5)
    # The map selects the result's card, named for it, and its own relations open in place of a separate strip; on a
    # desktop the counts are the reader's, said once.
    playwright.expect(_map_card(page, "topology/result")).to_have_attribute("aria-label", re.compile(r"^Result, "))
    playwright.expect(page.locator(".bp-reading-links")).to_have_count(0)
    relations = remote_card.locator("details.bp-dependencies")
    playwright.expect(relations).to_have_attribute("open", "")
    playwright.expect(relations.locator("summary")).to_have_text("Uses 1 · Used by 55")
    playwright.expect(relations.locator("a.bp-ref")).to_have_count(56)
    last_id = "topology/later-54"
    index_response = page.request.get(f"{site_url}/dependencies/index.json", timeout=10_000)
    assert index_response.ok
    last_card = _reading_card(page, last_id)
    playwright.expect(last_card.locator(".bp-thmheading > .bp-thmtitle")).to_have_text("Later 54")
    last = _named_reference(relations, last_card, _site_route(page, site_url, last_id),
                            _reference_counts(_decoded_relations(index_response.json()), "topology/result", last_id))
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


def test_authored_explorer_title_math_keeps_native_outline_see_also_and_breadcrumb_controls(
    desktop_page: object, marked_math_site_url: str, marked_math_runtime: object
) -> None:
    """Real published title parts and pinned CHTML keep code, focus, hrefs and native Enter intact."""
    from tests.test_integrated_explorer_browser import _routes

    page, site_url = desktop_page, marked_math_site_url
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _routes(page, site_url, marked_math_runtime, graph_client=True)
    first = _site_route(page, site_url, "algebra/title-target")
    scope_route = _site_route(page, site_url, "algebra")
    foundation_route = _site_route(page, site_url, "algebra/title-foundation")
    page.goto(first)
    _ready_site(page)
    # Wait for the actual optional layer's final reader refresh, not a timed
    # sleep that could race a replacement of otherwise-correct controls.
    playwright.expect(page.locator(".bp-reading-wiki-links h5").first).to_have_text("See also")
    outline = page.locator('.bp-dag-navigation-link[data-navigation-node-id="algebra"]')
    # The article links to the foundation, so the related title is its entry under "See also" at the article's end.
    related = page.locator('.bp-reading-wiki-list a[href="' + foundation_route + '"]')
    around = page.locator(".bp-reading-around")
    ancestor = page.locator(".bp-dag-breadcrumb a").filter(has=page.locator(".arithmatex"))
    playwright.expect(outline).to_have_attribute("href", scope_route)
    playwright.expect(related).to_have_attribute("href", foundation_route)
    playwright.expect(ancestor).to_have_attribute("href", scope_route)
    page.evaluate("""foundation => {
      window.__titleMathDocument=document;window.__titleMathClicks=0;
      window.__titleMathControls=[document.querySelector('[data-navigation-node-id="algebra"]'),
        Array.from(document.querySelectorAll('.bp-reading-wiki-list a')).find(node=>node.href===foundation),
        document.querySelector('.bp-dag-breadcrumb a:has(.arithmatex)')];
      // The reader takes a click on a link in the article before the link hears it, so count where the click begins.
      document.addEventListener('click',event=>{if(event.target.closest&&event.target.closest('a')===__titleMathControls[1])__titleMathClicks++;},true);
    }""", foundation_route)
    outline.focus()
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-dag-integrated'))") is True
    assert page.evaluate("() => document.activeElement===__titleMathControls[0] && __titleMathControls.every(node=>node.isConnected)")
    assert outline.evaluate("node=>node===__titleMathControls[0]")
    assert related.evaluate("node=>node===__titleMathControls[1]")
    assert ancestor.evaluate("node=>node===__titleMathControls[2]")
    for control, tex in ((outline, r"\ZZ/n"), (around, r"\ZZ/n"), (related, r"\mathbb{Z}/n"), (ancestor, r"\ZZ/n")):
        playwright.expect(control.locator(".arithmatex")).to_have_count(1)
        playwright.expect(control.locator(".arithmatex")).to_have_attribute("data-autoform-math-source", r"\(" + tex + r"\)")
        playwright.expect(control.locator("mjx-container")).to_have_count(1)
        # The pinned engine must expand the project macro to the actual
        # double-struck Z glyph, not merely emit any nonempty formula shell.
        playwright.expect(control.locator("mjx-c.mjx-c2124")).to_have_count(1)
        assert control.locator("mjx-c").evaluate_all(
            "glyphs=>glyphs.length>=3 && glyphs.every(glyph=>"
            "!['none','normal',''].includes(getComputedStyle(glyph,'::before').content))"
        )
        playwright.expect(control.locator("code")).to_have_text("$literal$")
        assert control.locator("code .arithmatex, code mjx-container, [tabindex], [role=application]").count() == 0
        assert control.locator("mjx-merror, merror, script, iframe, img").count() == 0
        assert control.evaluate("node=>!node.classList.contains('arithmatex')")
    _assert_hit(page, outline)
    outline.locator("mjx-container").click(button="right")
    playwright.expect(page.get_by_role("menu")).to_have_count(0)
    page.keyboard.press("Escape")
    related.focus()
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-reading-wiki-links'))") is True
    playwright.expect(related).to_be_focused()
    playwright.expect(related).to_have_attribute("href", foundation_route)
    assert related.evaluate("node=>node===__titleMathControls[1]")
    _assert_hit(page, related)
    page.keyboard.press("Enter")
    page.wait_for_url(foundation_route)
    _reading_card(page, "algebra/title-foundation")
    assert page.evaluate("() => __titleMathClicks===1 && document===__titleMathDocument")
    # The actual authored math-bearing leaf is now the nonlinked current
    # breadcrumb, and its authored module remains a canonical ancestor link.
    playwright.expect(page.locator(".bp-reading-wiki-links h5")).to_have_text(["Linked from"])
    ancestor = page.locator(".bp-dag-breadcrumb a").filter(has=page.locator(".arithmatex"))
    current = page.locator('.bp-dag-breadcrumb li[aria-current="page"]')
    ancestor.evaluate("node=>window.__titleMathAncestor=node")
    current.evaluate("node=>window.__titleMathCurrentCrumb=node")
    _paint_frame(page)
    # The breadcrumb's math refresh must not move the stage the map draws in.
    stage_before = page.locator(".bp-dag-stage").bounding_box()
    assert stage_before is not None
    # The map sets its own formula titles, and the breadcrumb's refresh must not set them again. A title keeps its
    # node while it is off screen, so each formula is checked in its title, not in the map.
    playwright.expect(page.locator(".bp-whole-map .pm-tex mjx-container").first).to_be_attached()
    page.evaluate("() => { window.__titleMathMapGlyphs = Array.from(document.querySelectorAll('.bp-whole-map .pm-tex mjx-container')); }")
    ancestor.focus()
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-dag-breadcrumb'))") is True
    playwright.expect(ancestor).to_be_focused()
    playwright.expect(ancestor).to_have_attribute("href", scope_route)
    assert ancestor.evaluate("node=>node===__titleMathAncestor")
    assert current.evaluate("node=>node===__titleMathCurrentCrumb && !node.hasAttribute('tabindex')")
    playwright.expect(current.locator("a, button, summary")).to_have_count(0)
    for crumb, tex in ((ancestor, r"\ZZ/n"), (current, r"\mathbb{Z}/n")):
        playwright.expect(crumb.locator(".arithmatex")).to_have_attribute("data-autoform-math-source", r"\(" + tex + r"\)")
        playwright.expect(crumb.locator("mjx-container")).to_have_count(1)
        playwright.expect(crumb.locator("mjx-c.mjx-c2124")).to_have_count(1)
        playwright.expect(crumb.locator("code")).to_have_text("$literal$")
        assert crumb.locator("code .arithmatex, code mjx-container, [tabindex], [role=application]").count() == 0
        assert crumb.locator("mjx-merror, merror, script, iframe, img").count() == 0
        assert crumb.evaluate("node=>!node.classList.contains('arithmatex')")
    assert page.locator(".bp-dag-stage").bounding_box() == stage_before
    assert page.evaluate("() => __titleMathMapGlyphs.every(glyph => glyph.closest('.pm-tex') !== null)")
    ancestor.locator("mjx-container").click(button="right")
    playwright.expect(page.get_by_role("menu")).to_have_count(0)
    page.keyboard.press("Escape")
    # BrowserContext uses the same genuine pinned router for a native
    # Control-click popup; no navigation callback or payload is substituted.
    # WebKit reports modified native tabs at context level without a Page popup.
    _routes(page.context, site_url, marked_math_runtime, graph_client=True)
    with page.context.expect_page() as opened:
        ancestor.click(modifiers=["Control"])
    popup = opened.value
    try:
        popup.wait_for_url(scope_route)
        assert page.url == foundation_route
        assert page.evaluate("() => document===__titleMathDocument")
    finally:
        popup.close()
    ancestor.focus()
    _assert_hit(page, ancestor)
    page.keyboard.press("Enter")
    page.wait_for_url(scope_route)
    page.go_back()
    page.wait_for_url(foundation_route)
    playwright.expect(current.locator("mjx-container")).to_have_count(1)
    playwright.expect(page.locator(".bp-reading-body > .bp-reading-source")).to_have_count(1)
    assert page.evaluate("() => document===__titleMathDocument")
    page.go_back()
    page.wait_for_url(first)
    # Back commits the route and then restores the view saved with it, which draws the outline again. Focus the outline
    # once the navigation is done, or that redraw takes the focused link away.
    playwright.expect(page.locator(".bp-dag-integrated")).not_to_have_attribute("aria-busy", "true")
    playwright.expect(related).to_have_attribute("href", foundation_route)
    outline.focus()
    outline.evaluate("node=>window.__titleMathReturnOutline=node")
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-dag-navigation-inner'))") is True
    assert outline.evaluate("node=>node===__titleMathReturnOutline && document.activeElement===node")
    page.keyboard.press("Enter")
    page.wait_for_url(scope_route)
    assert page.evaluate("() => document===__titleMathDocument")
    assert documents == [urlsplit(first)._replace(fragment="").geturl()]


def test_explorer_title_math_has_no_raw_tex_tooltips_or_clipped_focus_rings(
    desktop_page: object, marked_math_site_url: str, marked_math_runtime: object
) -> None:
    """A tooltip shows a title's TeX as typed, and the site's clip on a list holding a formula cut its links' rings."""
    from tests.test_integrated_explorer_browser import _routes

    page, site_url = desktop_page, marked_math_site_url
    _routes(page, site_url, marked_math_runtime, graph_client=True)
    foundation_route = _site_route(page, site_url, "algebra/title-foundation")
    page.goto(_site_route(page, site_url, "algebra/title-target"))
    _ready_site(page)
    related = page.locator('.bp-reading-wiki-list a[href="' + foundation_route + '"]')
    playwright.expect(related.locator("mjx-container")).to_have_count(1)
    outline = page.locator('.bp-dag-navigation-link[data-navigation-node-id="algebra"]')
    ancestor = page.locator(".bp-dag-breadcrumb a").filter(has=page.locator(".arithmatex"))
    for control in (outline, ancestor):
        assert control.get_attribute("title") is None
    assert ancestor.evaluate("node => node.parentElement.hasAttribute('title')") is False
    # A title without a formula keeps its tooltip.
    plain = page.locator(".bp-dag-navigation-link:not(:has(.arithmatex))").first
    assert plain.evaluate("node => node.title !== '' && node.title === node.textContent")
    # The link's ring is drawn 2px outside it, at the list's edge.
    assert related.evaluate("node => getComputedStyle(node.closest('ul')).overflowX") == "visible"


def test_tab_titles_place_lines_and_accessible_names_read_title_math_as_text(
    desktop_page: object, marked_math_site_url: str, marked_math_runtime: object
) -> None:
    """A tab title, a place line or an accessible name cannot typeset: a formula reads as text and code keeps its dollars."""
    from tests.test_integrated_explorer_browser import _routes

    page, site_url = desktop_page, marked_math_site_url
    _routes(page, site_url, marked_math_runtime, graph_client=True)
    page.goto(_site_route(page, site_url, "algebra"))
    _ready_site(page)
    handle, heading = page.locator(".bp-dag-sheet-handle"), page.locator(".bp-dag-viewer > h1.bp-dag-sr-only")
    # The topic's tab and heading read the page's own title; a project macro such as \ZZ reads as its name.
    playwright.expect(page).to_have_title("Groups ZZ/n and code $literal$")
    playwright.expect(heading).to_have_text("Groups ZZ/n and code $literal$")
    playwright.expect(handle).to_have_attribute("aria-label", "Groups ZZ/n and code $literal$, reading")
    page.locator(".bp-dag-search").fill("algebra/title-foundation")
    hit = page.locator('.bp-dag-result[data-result-id="algebra/title-foundation"]')
    playwright.expect(hit.locator(".bp-dag-result-place")).to_have_text("Groups ZZ/n and code $literal$")
    hit.click()
    playwright.expect(page).to_have_title("Groups ℤ/n and code $literal$")
    playwright.expect(heading).to_have_text("Groups ℤ/n and code $literal$")
    playwright.expect(handle).to_have_attribute("aria-label", "Groups ℤ/n and code $literal$, reading")


def test_failed_reader_reconciliation_opens_current_permalink_in_fresh_native_tab(
    desktop_page: object, marked_math_site_url: str, marked_math_runtime: object
) -> None:
    """One real renderer boundary rejects SPA B; a native noopener tab loads B's complete source."""
    from tests.test_integrated_explorer_browser import _routes
    from tests.test_integrated_selected_focus_browser import _complete_source, _published_source

    page, site_url = desktop_page, marked_math_site_url
    documents: list[str] = []
    page.context.on("request", lambda request: documents.append(request.url)
                    if request.resource_type == "document" else None)
    _routes(page.context, site_url, marked_math_runtime, graph_client=True)
    first = _site_route(page, site_url, "algebra/result")
    failed = _site_route(page, site_url, "topology/result")
    page.goto(first)
    _ready_site(page)
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-reading-body'))") is True
    playwright.expect(_reading_card(page, "algebra/result").locator("mjx-container")).to_have_count(5)
    page.evaluate("""() => {
      window.__failureDocument=document;window.__failureReading=document.querySelector('.bp-reading-body > .bp-reading-source');
      window.__readingBoundaryFailure=0;
      const actual=Element.prototype.replaceChildren;
      Element.prototype.replaceChildren=function(...nodes){
        if(this.classList.contains('bp-reading-body')&&!__readingBoundaryFailure){
          __readingBoundaryFailure++;Element.prototype.replaceChildren=actual;
          throw new Error('Injected one-shot reading reconciliation failure');
        }
        return actual.apply(this,nodes);
      };
    }""")
    page.locator(".bp-dag-search").fill("topology/result")
    target = page.locator('.bp-dag-result[data-result-id="topology/result"]')
    playwright.expect(target).to_be_visible()
    _assert_hit(page, target)
    target.click()
    page.wait_for_url(failed)
    fallback = page.get_by_role("link", name="Open complete article in a new tab", exact=True)
    playwright.expect(fallback).to_be_visible()
    playwright.expect(fallback).to_have_attribute("href", failed)
    playwright.expect(fallback).to_have_attribute("target", "_blank")
    assert "noopener" in (fallback.get_attribute("rel") or "").split()
    assert page.evaluate("() => __readingBoundaryFailure===1 && document===__failureDocument && "
                         "document.querySelector('.bp-reading-body > .bp-reading-source')===__failureReading")
    assert documents == [urlsplit(first)._replace(fragment="").geturl()]
    _assert_hit(page, fallback)
    with page.context.expect_page() as opened:
        fallback.click()
    recovered = opened.value
    try:
        recovered.wait_for_url(failed)
        _ready_site(recovered)
        assert recovered.evaluate("() => window.opener===null && !window.__failureDocument && !window.__readingBoundaryFailure")
        assert recovered.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-reading-body'))") is True
        card = _reading_card(recovered, "topology/result")
        playwright.expect(card.locator(".arithmatex:not(.bp-dependencies .arithmatex)")).to_have_count(5)
        playwright.expect(card.locator("mjx-container:not(.bp-dependencies mjx-container)")).to_have_count(5)
        # All eight authored math-bearing reference titles are additional
        # controls, not substitutes for any of the five statement formulas.
        titles = card.locator(".bp-dependencies .arithmatex")
        playwright.expect(titles).to_have_count(8)
        assert titles.evaluate_all("nodes=>nodes.map(node=>node.getAttribute('data-autoform-math-source'))") == [r"\(\ZZ/n\)"] * 8
        playwright.expect(titles.locator("mjx-container")).to_have_count(8)
        playwright.expect(titles.locator("mjx-c.mjx-c2124")).to_have_count(8)
        assert titles.locator("mjx-c").evaluate_all("""nodes=>nodes.length>=24&&nodes.every(node=>
          !['none','normal',''].includes(getComputedStyle(node,'::before').content))""")
        playwright.expect(card.locator(".bp-dependencies code")).to_have_text(["$literal$"] * 8)
        assert card.locator(".bp-dependencies [tabindex], .bp-dependencies [role=application]").count() == 0
        playwright.expect(card.locator(".arithmatex")).to_have_count(13)
        playwright.expect(card.locator("mjx-container")).to_have_count(13)
        playwright.expect(card.locator("mjx-merror, merror")).to_have_count(0)
        playwright.expect(card).to_contain_text("Final sentence of the complete statement.")
        _complete_source(recovered, _published_source(recovered, failed))
        assert recovered.evaluate("() => MathJax.version==='3.2.2'")
        assert page.url == failed
        assert page.evaluate("() => document===__failureDocument && "
                             "document.querySelector('.bp-reading-body > .bp-reading-source')===__failureReading")
        assert documents == [urlsplit(first)._replace(fragment="").geturl(), urlsplit(failed)._replace(fragment="").geturl()]
    finally:
        recovered.close()


@pytest.mark.parametrize("explorer", [False, True])
def test_marked_reader_link_math_does_not_steal_native_navigation_keys(
    desktop_page: object, marked_math_site_url: str, marked_math_runtime: object, explorer: bool
) -> None:
    """Published Markdown creates the control; the pinned menu's saved accessibility setting is genuine."""
    from tests.test_integrated_explorer_browser import _math_source, _routes

    page, site_url = desktop_page, marked_math_site_url
    if explorer:
        page.add_init_script("localStorage.setItem('MathJax-Menu-Settings', JSON.stringify({explorer: true}))")
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _routes(page, site_url, marked_math_runtime, graph_client=True)
    first = _site_route(page, site_url, "algebra/base")
    page.goto(first)
    _ready_site(page)
    page.evaluate("() => window.__controlMathDocument = document")
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-reading-body'))") is True
    card = _reading_card(page, "algebra/base")
    playwright.expect(card.locator("mjx-container")).to_have_count(2)
    outside = card.locator("mjx-container").first
    assert _math_source(page, outside) == "0"
    if explorer:
        playwright.expect(outside).to_have_attribute("role", "application")
    else:
        playwright.expect(outside).to_have_attribute("tabindex", "0")
    link = card.locator("a:has(mjx-container)")
    playwright.expect(link).to_have_count(1)
    control_math = link.locator("mjx-container")
    playwright.expect(control_math).to_have_count(1)
    playwright.expect(link.locator(".arithmatex")).to_have_attribute("data-autoform-math-source", r"\(y^2\)")
    playwright.expect(control_math.locator("mjx-msup")).to_have_count(1)
    assert control_math.locator("mjx-c").evaluate_all(
        "glyphs => glyphs.length >= 2 && glyphs.every(glyph => "
        "!['none', 'normal', ''].includes(getComputedStyle(glyph, '::before').content))"
    )
    assert link.locator("[tabindex], [role=application]").count() == 0
    playwright.expect(card.locator("mjx-merror, merror")).to_have_count(0)
    _assert_hit(page, link)
    # This formula deliberately has no source-menu control; the ordinary
    # formula above still passed genuine menu readback through _math_source.
    control_math.click(button="right")
    playwright.expect(page.get_by_role("menu")).to_have_count(0)
    page.keyboard.press("Escape")
    link.focus()
    page.keyboard.press("Enter")
    page.wait_for_url(_site_route(page, site_url, "algebra/result"))
    playwright.expect(_reading_card(page, "algebra/result").locator("mjx-container")).to_have_count(5)
    assert page.evaluate("() => document === __controlMathDocument")
    assert documents == [urlsplit(first)._replace(fragment="").geturl()]


def _paint_frame(page: object) -> None:
    page.evaluate("() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")



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
    # The map follows the book's selection mark, so it lets go of its card when the book does.
    card = page.locator(".bp-whole-map .pm-result.pm-selected")
    if selected is not None:
        playwright.expect(card).to_have_attribute("data-result", selected)
    else:
        playwright.expect(card).to_have_count(0)



def test_widening_with_the_book_focused_keeps_its_focus_and_the_map_fills_the_wider_stage(
    desktop_page: object, site_url: str
) -> None:
    page = desktop_page
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": 1000, "height": 800})
    _open_site(page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    # Once the map has its style and draws.
    playwright.expect(page.locator(".bp-whole-map .pm-part").first).to_be_visible()
    _paint_frame(page)
    book = page.locator(".bp-dag-breadcrumb .bp-dag-book")
    playwright.expect(book).to_have_text("Home")
    playwright.expect(book).to_have_attribute("href", site_url + "/index.html")
    book.focus()
    map_view = page.locator(".bp-whole-map .pm-stage > svg")
    before = map_view.bounding_box()
    assert before is not None
    page.set_viewport_size({"width": 1280, "height": 900})
    playwright.expect(page.locator(".bp-dag-book")).to_have_count(1)
    _paint_frame(page)
    playwright.expect(book).to_have_attribute("href", site_url + "/index.html")
    playwright.expect(book).to_be_focused()
    # Width changes may refold the trail, restoring its matching native link.
    # Painting again at that width must not replace it a second time.
    book.evaluate("node => window.__resizedHomeCrumb = node")
    _paint_frame(page)
    assert book.evaluate("node => node === __resizedHomeCrumb && node === document.activeElement")
    # The whole map follows its stage to the new size.
    after, stage = map_view.bounding_box(), page.locator(".bp-dag-stage").bounding_box()
    assert after is not None and stage is not None
    assert after["width"] != before["width"] and after["height"] != before["height"]
    assert abs(after["width"] - stage["width"]) < 1 and abs(after["height"] - stage["height"]) < 1



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
    playwright.expect(crumbs).to_have_count(4)
    for index, label in enumerate(("Home", *labels)):
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
    _consumer_selected(page, explorer_consumer, "chapter/middle")
    _assert_sheet_state(page, selected="chapter/middle", opened=True)
    handle = page.locator(".bp-dag-sheet-handle")
    if width == 390:
        # Half, then full, then the peek; one more tap opens half again.
        handle.click()
        _assert_sheet_state(page, selected="chapter/middle", opened=True)
        handle.click()
        _assert_sheet_state(page, selected="chapter/middle", opened=False)
        handle.click()
        _assert_sheet_state(page, selected="chapter/middle", opened=True)
    # Route navigation may legitimately change the scene. Clearing the already
    # committed selection must not introduce the reviewed 1000px vertical jump.
    stage = page.locator(".bp-dag-stage")
    original = stage.bounding_box()
    assert original is not None
    # Escape in the reader clears the selection.
    _reading_card(page, "chapter/middle").locator("details.bp-dependencies > summary").press("Escape")
    _assert_sheet_state(page, selected=None, opened=width > 720)
    if width == 1000:
        assert abs(stage.bounding_box()["y"] - original["y"]) < 2, "Clearing selection moved the map vertically"
    assert page.url == explorer_consumer.route("chapter/middle")
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(explorer_consumer.heading("chapter/middle"))

    if width == 390:
        handle.click()
    # Ordinary reading links in each statement's own dependency disclosure,
    # rather than forced offscreen node clicks, retain each entry's open
    # reader state for the Back/Forward assertions.
    _open_dependency(_reading_card(page, "chapter/middle"), explorer_consumer, "chapter/foundation").click()
    _consumer_selected(page, explorer_consumer, "chapter/foundation")
    _open_dependency(_reading_card(page, "chapter/foundation"), explorer_consumer, "chapter/middle").click()
    _consumer_selected(page, explorer_consumer, "chapter/middle")
    _assert_sheet_state(page, selected="chapter/middle", opened=True)
    before_filter = stage.bounding_box()
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
    # Filtering out the selection's status keeps the selection and the reader as they were.
    _assert_sheet_state(page, selected="chapter/middle", opened=True)
    if width == 1000:
        after_filter = stage.bounding_box()
        assert after_filter is not None and abs(after_filter["y"] - before_filter["y"]) < 2, "Filtering moved the map vertically"
    # The map is told which statuses to show, and dims the rest, the selected card included.
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_attribute("data-map-statuses", re.compile(r"^(?!.*\bcan_prove\b)"))
    playwright.expect(page.locator(".bp-whole-map .pm-result.pm-selected")).to_have_class(re.compile(r"\bpm-dimmed\b"))
    page.go_back()
    _consumer_selected(page, explorer_consumer, "chapter/foundation")
    _assert_sheet_state(page, selected="chapter/foundation", opened=True)
    page.go_forward()
    _consumer_selected(page, explorer_consumer, "chapter/middle")
    _assert_sheet_state(page, selected="chapter/middle", opened=True)
    playwright.expect(page.locator('.bp-dag-filter-popover input[value="can_prove"]')).not_to_be_checked()
    assert page.evaluate("document===window.__consumerDocument") is True


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
    prerequisite = _open_dependency(card, explorer_consumer, "chapter/foundation")
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
    # They show once, in the selected statement's own disclosure, which opens with it; no second strip repeats them.
    playwright.expect(page.locator(".bp-reading-links")).to_have_count(0)
    playwright.expect(card.locator("details.bp-dependencies")).to_have_attribute("open", "")
    playwright.expect(_open_dependency(card, explorer_consumer, "chapter/middle")).to_be_visible()
    # Old permalinks that name the removed inspector's path direction still select their statement; the direction no
    # longer chooses what the map draws.
    for direction in ("prerequisites", "dependents", "both"):
        page.goto(explorer_consumer.url + "/dependencies/chapters/chapter.html#node=chapter%2Fmiddle&direction=" + direction)
        _consumer_selected(page, explorer_consumer, "chapter/middle")
        playwright.expect(_reading_card(page, "chapter/target")).to_contain_text("Final sentence of the complete target statement.")


@pytest.mark.parametrize(("name", "counts"), (("mixed", "4 statements"), ("flat", "100 of 1,000")))
def test_the_rail_counts_in_words_that_agree_with_their_numbers(
    desktop_page: object, explorer_consumer: _ExplorerConsumer, name: str, counts: str,
) -> None:
    """Count wording now belongs to the canonical Results view, not a duplicate rail overview."""
    page = desktop_page
    _open_consumer(page, explorer_consumer, name)
    _browse_contents(page, "Statements in this section")
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text(counts)
    graph = explorer_consumer.graphs[name]
    assert len(graph["nodes"]) == (4 if name == "mixed" else 1_000)
    members = {node["id"] for node in graph["nodes"]}
    assert sum(set(relation[:2]) <= members for relation in explorer_consumer.relations) == (1 if name == "mixed" else 0)
    if name == "mixed":
        page.locator(".bp-dag-search").fill("mixed/a")
        playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("1 statement · search the whole roadmap")
    else:
        page.locator(".bp-dag-inventory > .bp-dag-load-more").click()
        playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("200 of 1,000")


def test_semantic_outline_and_status_controls_do_not_repeat_leaf_summary_rows(
    desktop_page: object, explorer_consumer: _ExplorerConsumer,
) -> None:
    """The current leaf has a canonical outline link; other leaves are not duplicated summary rows."""
    page = desktop_page
    _open_consumer(page, explorer_consumer, "chapter/middle")
    navigation = page.locator(".bp-dag-navigation")
    scope = navigation.locator('[data-navigation-node-id="chapter"]')
    playwright.expect(scope).to_have_text("Dependency chapter")
    playwright.expect(scope).to_have_attribute("href", explorer_consumer.route("chapter"))
    playwright.expect(scope).to_have_attribute("title", "Dependency chapter")
    current = navigation.locator('[data-navigation-node-id="chapter/middle"]')
    playwright.expect(current).to_have_count(1)
    playwright.expect(current).to_have_attribute("href", explorer_consumer.route("chapter/middle"))
    playwright.expect(current).to_have_attribute("aria-current", "page")
    playwright.expect(navigation.locator('[data-navigation-node-id="chapter/foundation"], [data-navigation-node-id="chapter/target"]')).to_have_count(0)
    # The map's card names the statement and its status.
    playwright.expect(_map_card(page, "chapter/middle")).to_have_attribute("aria-label", "Middle lemma, ready to prove")
    page.locator(".bp-dag-filter-summary").click()
    filters = page.locator(".bp-dag-filter-popover")
    statuses = {node["status"] for node in explorer_consumer.records.values()}
    playwright.expect(filters.locator(".bp-dag-swatch")).to_have_count(len(statuses))
    for status in statuses:
        expected = sum(node["status"] == status for node in explorer_consumer.records.values())
        row = filters.locator("label").filter(has=page.locator(f'input[value="{status}"]'))
        playwright.expect(row.locator(".bp-dag-check-count")).to_have_text(str(expected))
        # The filter doubles as the legend, so each status says what it means.
        meaning = explorer_consumer.graphs["chapter"]["palette"][status]["meaning"]
        playwright.expect(row.locator(".bp-dag-check-meaning")).to_have_text(meaning)


@pytest.mark.parametrize("width", (721, 800, 1000))
def test_a_filter_never_adds_a_tablet_toolbar_row(desktop_page: object, explorer_consumer: _ExplorerConsumer, width: int) -> None:
    """A filter's longer summary never wraps onto a row of its own and pushes the map down."""
    page = desktop_page
    page.set_viewport_size({"width": width, "height": 900})
    _open_consumer(page, explorer_consumer)
    toolbar, stage = page.locator(".bp-dag-toolbar"), page.locator(".bp-dag-stage")
    before = toolbar.bounding_box(), stage.bounding_box()
    page.locator(".bp-dag-filter-summary").click()
    page.locator('.bp-dag-filter-popover input[value="can_prove"]').uncheck()
    playwright.expect(page.locator(".bp-dag-filter-summary")).to_have_text(re.compile(r"^Filter: \d+ of \d+ statuses$"))
    assert (toolbar.bounding_box()["height"], stage.bounding_box()["y"]) == (before[0]["height"], before[1]["y"])
    # A list adds Back to map beside Browse on the second row, and the toolbar keeps its two rows.
    _browse_contents(page, "Statements in this section")
    back = page.get_by_role("button", name="Back to map", exact=True)
    playwright.expect(back).to_be_visible()
    assert toolbar.bounding_box()["height"] == before[0]["height"]
    row = [box["y"] + box["height"] / 2 for box in (
        page.locator(".bp-dag-filter-summary").bounding_box(), page.locator(".bp-dag-browse").bounding_box(), back.bounding_box())]
    assert max(row) - min(row) < 2, row



def test_a_phone_toolbar_keeps_its_controls_on_one_row_under_search(desktop_page: object, explorer_consumer: _ExplorerConsumer) -> None:
    """At 390px "Filter: all statuses" pushed zoom onto a third row, and the map lost that row's height."""
    page = desktop_page
    page.set_viewport_size({"width": 390, "height": 844})
    _open_consumer(page, explorer_consumer)
    # A child hidden only by visibility still takes its place in the row, so it counts.
    rows = """() => new Set([...document.querySelector('.bp-dag-toolbar').children]
      .filter(child => child.getClientRects().length).map(child => Math.round(child.getBoundingClientRect().top))).size"""
    assert page.evaluate(rows) == 2
    playwright.expect(page.locator(".bp-dag-filter-summary")).to_have_text("Filter: all statuses")
    # A list adds Back to map and has no map to zoom, and its controls still fit one row.
    _browse_contents(page, "Statements in this section")
    playwright.expect(page.get_by_role("button", name="Back to map", exact=True)).to_be_visible()
    assert page.evaluate(rows) == 2



@pytest.mark.parametrize("width", (800, 1000))
def test_a_head_without_a_textbook_link_has_no_blank_row(desktop_page: object, site_url: str, width: int) -> None:
    """The reusable Home/context row is real navigation, never a reserved blank textbook-link row."""
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


@pytest.mark.parametrize("width", (390, 1000, 1440))
def test_the_explorer_head_follows_and_switches_the_readers_colour_scheme(
    desktop_page: object, site_url: str, width: int
) -> None:
    """The site opens in the reader's setting, and the head stands in for Material's hidden sun or moon."""
    page = desktop_page
    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(color_scheme="dark", reduced_motion="reduce")
    _open_site(page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    body = page.locator("body")
    playwright.expect(body).to_have_attribute("data-md-color-scheme", "slate")
    flip = page.get_by_role("button", name="Switch light or dark theme", exact=True)
    # It ends the trail row on every width, so it neither adds a row nor joins the wrapping toolbar.
    playwright.expect(page.locator(".bp-dag-context > .bp-dag-theme")).to_have_count(1)
    _assert_hit(page, flip)
    assert flip.get_attribute("title") == "Switch light or dark theme"
    box = flip.bounding_box()
    assert box is not None and (box["width"], box["height"]) == (32, 32)
    flip.click()
    playwright.expect(body).to_have_attribute("data-md-color-scheme", "default")
    # Material keeps the choice over the media query, on this page and the next.
    page.reload()
    _ready_site(page)
    playwright.expect(body).to_have_attribute("data-md-color-scheme", "default")
    page.get_by_role("button", name="Switch light or dark theme", exact=True).click()
    playwright.expect(body).to_have_attribute("data-md-color-scheme", "slate")


@pytest.mark.parametrize("attempt", (1, 2, 3))
def test_dragging_the_reader_without_handle_focus_keeps_the_selected_card_under_the_pointer(
    desktop_page: object, site_url: str, attempt: int,
) -> None:
    """A pointer drag must hold the card even when the press gives no keyboard focus."""
    page = desktop_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(reduced_motion="reduce")
    _open_site(page, site_url, _site_route(page, site_url, "algebra/result").removeprefix(site_url + "/"))
    handle = page.get_by_role("button", name=re.compile(r", reading$"))
    handle.click()
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    # QA's 40833240355/40833244794 probe: suppress only the press's default
    # focus, as on iOS Safari, while the actual pointer handlers still run.
    page.evaluate("""() => {
      document.addEventListener('mousedown', event => event.preventDefault(), true);
      window.__readerDragFocusLog = [];
      document.addEventListener('focusin', event => {
        window.__readerDragFocusLog.push(event.target.className || event.target.tagName);
      }, true);
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    }""")
    playwright.expect(handle).not_to_be_focused()
    node = _map_card(page, "algebra/result")
    _paint_frame(page)
    initial, grip = node.bounding_box(), handle.bounding_box()
    identity = node.element_handle()
    assert initial is not None and grip is not None and identity is not None
    url, history_length = page.url, page.evaluate("history.length")
    reading = page.locator(".bp-reading-body > .bp-reading-source").text_content()
    x, y = grip["x"] + grip["width"] / 2, grip["y"] + grip["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    try:
        page.mouse.move(x, y - 190, steps=8)
        page.mouse.move(x, y - 380, steps=8)
    finally:
        page.mouse.up()
    _paint_frame(page)
    page.wait_for_timeout(300)
    _paint_frame(page)
    opened = node.bounding_box()
    state = page.evaluate("""() => ({
      open: document.querySelector('.bp-dag-viewer').classList.contains('bp-dag-sheet-open'),
      full: document.querySelector('.bp-dag-viewer').classList.contains('bp-dag-sheet-full'),
      handleFocused: document.activeElement === document.querySelector('.bp-dag-sheet-handle'),
      focusLog: window.__readerDragFocusLog
    })""")
    assert state["open"] and not state["full"], (attempt, state)
    assert not state["handleFocused"] and not any("sheet-handle" in str(entry) for entry in state["focusLog"]), state
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(handle).not_to_be_focused()
    assert opened is not None
    assert abs(opened["x"] - initial["x"]) < 2 and abs(opened["y"] - initial["y"]) < 2, (attempt, initial, opened, state)
    assert identity.evaluate("node => node === document.querySelector('.bp-whole-map .pm-result[data-result=\"algebra/result\"]')")
    assert page.url == url and page.evaluate("history.length") == history_length
    assert page.locator(".bp-reading-body > .bp-reading-source").text_content() == reading




def test_a_phone_reader_gives_the_article_its_whole_height_and_ends_full_clear_of_the_map_pill(mobile_page: object, site_url: str) -> None:
    """Disclosures pinned under the reader took up to 136px of a 327px reader at half; at full the Map pill covered their last row."""
    page = _open_site(mobile_page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    handle = page.get_by_role("button", name=re.compile(r", reading$"))
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    body = page.locator(".bp-reading-body")
    # Nothing is pinned under the article: it runs to the reader's foot.
    assert body.evaluate("node => Math.abs(node.getBoundingClientRect().bottom - node.parentElement.getBoundingClientRect().bottom)") < 1
    box, area = handle.bounding_box(), page.locator(".bp-dag-body").bounding_box()
    assert box is not None and area is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, area["y"] + 20, steps=6)
    page.mouse.up()
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_class(re.compile(r"\bbp-dag-sheet-full\b"))
    # A long article, scrolled to its end, where its links to other articles are.
    body.evaluate("""node => {
      node.querySelector('.bp-reading-source').append(...Array.from({length: 60}, () => document.createElement('p')).map(p => (p.textContent = 'A line.', p)));
      node.scrollTop = node.scrollHeight;
    }""")
    _paint_frame(page)
    assert body.evaluate("node => node.scrollTop > 0")
    last = body.evaluate("node => node.lastElementChild.getBoundingClientRect().bottom")
    pill = page.get_by_role("button", name="Show the map", exact=True).bounding_box()
    assert pill is not None and last <= pill["y"], (last, pill)

def test_a_phone_reader_opens_at_half_snaps_where_it_is_dragged_and_maps_back_from_full(
    mobile_page: object, site_url: str,
) -> None:
    """The phone reader covered the map on arrival, said only "Reading", and ignored a drag on its grabber."""
    page = _open_site(mobile_page, site_url, _site_route(mobile_page, site_url, "algebra/result").removeprefix(site_url + "/"))
    viewer, pane, body = page.locator(".bp-dag-viewer"), page.locator(".bp-reading-pane"), page.locator(".bp-dag-body")
    handle = page.get_by_role("button", name=re.compile(r", reading$"))
    full = re.compile(r"\bbp-dag-sheet-full\b")
    saved = "() => history.state.autoformNavigation.snapshot.readingOpen"
    # Half leaves the top 45% to the map, with the selected card above the sheet.
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(viewer).not_to_have_class(full)
    playwright.expect(handle).to_have_text("Result")
    playwright.expect(handle.locator(".bp-dag-swatch")).to_have_count(1)
    _paint_frame(page)
    sheet, area = pane.bounding_box(), body.bounding_box()
    node = _map_card(page, "algebra/result").bounding_box()
    assert sheet is not None and area is not None and node is not None
    assert sheet["y"] == pytest.approx(area["y"] + area["height"] * .45, abs=2)
    assert area["y"] <= node["y"] and node["y"] + node["height"] <= sheet["y"]
    # The reader's last line can be scrolled into view at half.
    reader = page.locator(".bp-reading-body").bounding_box()
    assert reader is not None and reader["y"] + reader["height"] <= area["y"] + area["height"] + 1
    history_length = page.evaluate("history.length")

    def drag_to(y: float) -> None:
        box = handle.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2, y, steps=6)
        page.mouse.up()
        _paint_frame(page)

    drag_to(area["y"] + 20)
    playwright.expect(viewer).to_have_class(full)
    playwright.expect(page.locator(".bp-dag-toolbar")).to_be_hidden()
    assert page.evaluate(saved) is True
    pill = page.get_by_role("button", name="Show the map", exact=True)
    _assert_hit(page, pill)
    pill.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    playwright.expect(handle).to_be_focused()
    playwright.expect(pill).to_be_hidden()
    playwright.expect(page.locator(".bp-dag-toolbar")).to_be_visible()
    assert page.evaluate(saved) is False
    peek = pane.bounding_box()
    assert peek is not None and peek["y"] == pytest.approx(area["y"] + area["height"] - 56, abs=2)
    # A drag that stops near the middle snaps to half, and its click is not taken as a tap.
    drag_to(area["y"] + area["height"] * .5)
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(viewer).not_to_have_class(full)
    drag_to(area["y"] + area["height"] - 10)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    assert page.evaluate("history.length") == history_length


_LEAN_FIXTURE = """namespace Fixture

/-- The base object. -/
def base (n : ℕ) : ℕ :=
  match n with
  | 0 => 1
  | k + 1 => 2 * base k

theorem result (n : ℕ) (h : 0 < n) :
    base n ≥ n + 1 := by
  sorry

end Fixture
"""


@pytest.fixture(scope="module")
def lean_site_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A published site whose two statements name declarations in a Lean file render read."""
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("lean-site") / "project"
    scaffold_project(project, title="Lean fixture")
    (project / "Fixture").mkdir()
    (project / "Fixture/Basic.lean").write_text(_LEAN_FIXTURE, encoding="utf-8")
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text("# Lean fixture\n\n- [Algebra](algebra/README.md)\n", encoding="utf-8")
    folder = roadmap / "algebra"
    folder.mkdir()
    (folder / "README.md").write_text("# Algebra\n\n- [Base](base.md)\n- [Result](result.md)\n", encoding="utf-8")
    (folder / "base.md").write_text(
        "---\ndeclaration: def\nlean: Fixture.base\n---\n\n# Base\n\nThe base object.\n\n## Depends on\n\nNo prerequisites.\n",
        encoding="utf-8",
    )
    (folder / "result.md").write_text(
        "---\ndeclaration: theorem\nlean: Fixture.result\n---\n\n# Result\n\nThe main result.\n\n## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    render_site(project / "blueprint", project / "site-src", lean_root=project)
    build(load_config(config_file=str(project / "mkdocs.yml")))
    with _served(project / "site") as url:
        yield url


def test_a_name_in_a_statements_lean_shows_its_declaration_and_leads_to_its_statement(
    webkit_browser: object, lean_site_url: str,
) -> None:
    """Hovering or focusing a name in a statement's Lean shows its declaration, a definition with its body, and
    following it opens the formalizing statement."""
    with _page(webkit_browser, viewport={"width": 1280, "height": 800}) as page:
        _offline(page, lean_site_url)
        page.goto(f"{lean_site_url}/dependencies/chapters/algebra.html#node=algebra%2Fresult")
        _ready_site(page)
        viewer, card = page.locator(".bp-dag-viewer"), page.locator(".bp-lean-card")
        block = page.locator('.bp-reading-body [data-autoform-node-id="algebra/result"] .bp-lean-source')
        name = block.locator('.bp-lean-ref[data-lean-name="Fixture.base"]')
        # Only the project's name links.
        assert block.locator(".bp-lean-ref").all_inner_texts() == ["base"]
        name.hover()
        playwright.expect(card).to_be_visible()
        playwright.expect(card).to_contain_text("def base (n : ℕ) : ℕ :=")
        playwright.expect(card).to_contain_text("| k + 1 => 2 * base k")
        playwright.expect(card).to_contain_text("Fixture/Basic.lean:4")
        assert name.get_attribute("aria-describedby") == card.get_attribute("id")
        page.keyboard.press("Escape")
        playwright.expect(card).to_be_hidden()
        # The pointer can rest on the card, and leaving both closes it; a hovered name's card closes as the reader scrolls.
        page.mouse.move(2, 2)
        name.hover()
        playwright.expect(card).to_be_visible()
        card.hover()
        page.wait_for_timeout(300)
        playwright.expect(card).to_be_visible()
        page.mouse.move(2, 2)
        playwright.expect(card).to_be_hidden()
        name.hover()
        playwright.expect(card).to_be_visible()
        page.locator(".bp-reading-body").dispatch_event("scroll")
        playwright.expect(card).to_be_hidden()
        page.mouse.move(2, 2)
        # The keyboard reaches the name and shows the same card, which stays as the reader scrolls and while the reader
        # selects in it, and Escape closes it, not the reader.
        name.focus()
        playwright.expect(card).to_be_visible()
        playwright.expect(card).to_contain_text("Fixture.base")
        page.locator(".bp-reading-body").dispatch_event("scroll")
        card.locator("pre").click()
        playwright.expect(card).to_be_visible()
        page.keyboard.press("Escape")
        playwright.expect(card).to_be_hidden()
        playwright.expect(viewer).to_have_class(re.compile(r"\bbp-dag-has-selection\b"))
        playwright.expect(viewer).to_have_attribute("data-map-start", "algebra/result")
        # Following the name opens the statement that formalizes it.
        name.click()
        playwright.expect(viewer).to_have_attribute("data-map-start", "algebra/base")
        playwright.expect(card).to_be_hidden()

