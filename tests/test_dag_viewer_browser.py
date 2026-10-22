from __future__ import annotations

import functools
import json
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pytest

from autoform_cli import dag_viewer
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode
from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project

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


@pytest.fixture(scope="module")
def explorer_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    tmp_path = tmp_path_factory.mktemp("explorer")
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
            # Long enough to fill a phone's sheet to its height cap.
            summary=" ".join(
                ["The target theorem gathers the foundation and the middle lemma into one statement."] * 6
            ),
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
        kind="chapter",
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
        lean="Remote.Namespace.deepTheorem",
        summary="A spectral gap statement for the remote fixture.",
    )
    dag_viewer.write_search_index(
        tmp_path / "search.json",
        GraphView(kind="full", title="Search", nodes=(*nodes, remote), edges=()),
        context_links={
            # Index records link explorer contexts, never the articles the view links.
            **{node.id: f"index.html#node={node.id}" for node in nodes},
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
        search_href="search.json",
    )
    failed_explorer = dag_viewer.render_container(
        "missing.json",
        script_href="viewer.js",
        fallback_links=fallback_links,
        fallback_total=len(nodes),
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
  </style>
</head>
<body>
  <main>{explorer}</main>
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
            status_counts=(("fully_proved", 60), ("can_prove", 20)),
            status_key="can_prove",
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
        # Only module inventories: a topic with no target rolls up to planned.
        ViewNode(
            id="atlas/library",
            title="Library",
            kind="scope",
            members=(),
            status_counts=(("inventory_checked", 5),),
            status_key="planned",
            area="Algebra",
        ),
    )
    atlas_areas = (
        "Algebra & Number Theory",
        "Analysis & Probability",
        "Discrete Mathematics & Computation",
        "Foundations & Logic",
        "Geometry & Topology",
    )
    atlas_nodes += tuple(
        ViewNode(
            id=f"atlas/auxiliary-{index}",
            title=f"Auxiliary topic {index}",
            kind="scope",
            members=tuple(f"atlas/auxiliary-{index}/item-{item}" for item in range(10 + index * 7)),
            status_counts=(("planned", 10 + index * 7),),
            status_key="planned",
            area=atlas_areas[index % len(atlas_areas)],
        )
        for index in range(26)
    )
    dense_views["atlas"] = GraphView(
        kind="project",
        title="Knowledge atlas",
        nodes=atlas_nodes,
        edges=(
            ViewEdge("atlas/algebra", "atlas/topology", statement_count=3),
            ViewEdge("atlas/analysis", "atlas/topology", statement_count=2),
        ),
    )
    dense_views["topics"] = GraphView(
        kind="chapter",
        title="Topics and results",
        nodes=(
            ViewNode("topics/compactness", "Compactness", "scope", ("topics/compactness/a",), (("planned", 1),)),
            ViewNode("topics/heine-borel", "Heine-Borel theorem", "node", ("topics/heine-borel",), (("planned", 1),)),
            ViewNode("topics/tychonoff", "Tychonoff theorem", "node", ("topics/tychonoff",), (("planned", 1),)),
            # Ten names, the first holding ", " inside guillemets, of which the explorer shows eight.
            ViewNode(
                "topics/library",
                "Topology library",
                "node",
                ("topics/library",),
                (("inventory_checked", 1),),
                catalog="module",
                lean=", ".join(["Topology.«open, closed»", *(f"Topology.lemma{index}" for index in range(2, 11))]),
            ),
        ),
        edges=(),
    )
    dense_views["empty"] = GraphView(kind="project", title="Fresh roadmap", nodes=(), edges=())
    for name, dense_view in dense_views.items():
        links = (
            {node.id: f"other.html#node={node.id}" for node in dense_view.nodes}
            if name == "atlas"
            else {}
        )
        dag_viewer.write_payload(tmp_path / f"{name}.json", dense_view, links=links)
        host = dag_viewer.render_container(f"{name}.json", script_href="viewer.js")
        (tmp_path / f"{name}.html").write_text(
            page_shell.replace("{explorer}", host),
            encoding="utf-8",
        )

    with _served(tmp_path) as url:
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
    assert sum(edge[0] == "topology/result" for edge in index["relations"]) == 55
    with _served(project / "site") as url:
        yield url


@pytest.fixture(scope="module")
def marked_math_runtime() -> object:
    from tests.test_integrated_explorer_browser import _PinnedRuntime

    return _PinnedRuntime()


def _open_ready(page: object, explorer_url: str) -> None:
    page.goto(f"{explorer_url}/index.html")
    page.locator(".bp-dag-stage").wait_for()
    page.locator("[data-autoform-node-id]").first.wait_for()
    playwright.expect(page.locator("main")).to_have_count(1)


def _load_search_index(page: object) -> object:
    """Focus the search box and wait until the whole-roadmap index has loaded.

    Focus starts the load and marks the box aria-busy in the same handler, so
    the busy state clearing means the index is in place.
    """
    search = page.locator(".bp-dag-search")
    search.focus()
    playwright.expect(search).not_to_have_attribute("aria-busy", "true")
    return search


def test_mobile_node_interaction_and_selected_sheet_collapse(mobile_page: object, explorer_url: str) -> None:
    page = mobile_page
    _open_ready(page, explorer_url)

    node = page.locator('[data-autoform-node-id="chapter/middle"]')
    node.tap()

    playwright.expect(node).to_have_attribute("data-selected", "true")
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text("Middle lemma")
    # The card and the details name the kind and status, never the internal id.
    playwright.expect(node.locator(".bp-dag-node-meta")).to_have_text("Lemma · ready to prove")
    playwright.expect(page.locator(".bp-dag-inspector")).not_to_contain_text("chapter/middle")
    handle = page.locator(".bp-dag-sheet-handle")
    inspector_inner = page.locator(".bp-dag-inspector-inner")
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(handle).to_have_attribute("aria-controls", "bp-dag-inspector-content-1")
    assert inspector_inner.get_attribute("aria-hidden") is None
    assert inspector_inner.get_attribute("inert") is None
    assert "node=chapter%2Fmiddle" in page.url
    # The handle names the selection, so the sheet's heading is hidden text, and the close button
    # sits in the handle's top-right corner, clear of its title.
    page.evaluate("() => new Promise(done => setTimeout(done, 300))")
    heading, close, handle_box = (
        page.locator(selector).bounding_box() for selector in (".bp-dag-inspector h2", ".bp-dag-close", ".bp-dag-sheet-handle")
    )
    assert heading is not None and close is not None and handle_box is not None
    assert heading["width"] <= 1 and heading["height"] <= 1
    assert (close["width"], close["height"]) == (32, 32)
    assert handle_box["y"] <= close["y"] and close["y"] + close["height"] <= handle_box["y"] + handle_box["height"]
    assert abs(handle_box["x"] + handle_box["width"] - 10 - (close["x"] + close["width"])) < 2
    assert handle.evaluate("element => getComputedStyle(element).paddingRight") == "48px"

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


def test_app_layout_fills_the_browser_viewport(desktop_page: object, explorer_url: str) -> None:
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{explorer_url}/index.html")
    page.locator(".bp-dag-stage").wait_for()
    viewer = page.locator(".bp-dag-viewer")
    box = viewer.bounding_box()
    assert box is not None
    assert box["x"] <= 1
    assert box["y"] <= 1
    assert box["width"] >= 1430
    assert box["height"] >= 890
    stage = page.locator(".bp-dag-stage").bounding_box()
    assert stage is not None
    assert stage["width"] >= 1000


@pytest.mark.parametrize("page_name", ("chain", "star"))
def test_connected_project_maps_open_at_readable_scale(
    desktop_page: object, explorer_url: str, page_name: str
) -> None:
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{explorer_url}/{page_name}.html")
    node = page.locator(".bp-dag-node").first
    node.wait_for()
    box = node.bounding_box()
    assert box is not None
    assert box["height"] >= 44
    density = page.locator(".bp-dag-density")
    playwright.expect(density).to_have_text("Pan or Contents to see all 33 articles")
    # The note is plain text at the map's bottom-left, not a bubble over the map.
    style = density.evaluate(
        "element => ['borderTopStyle', 'boxShadow', 'pointerEvents'].map(name => getComputedStyle(element)[name])"
    )
    assert style == ["none", "none", "none"]
    note, stage = density.bounding_box(), page.locator(".bp-dag-stage").bounding_box()
    assert note is not None and stage is not None
    assert abs(note["x"] - stage["x"] - 16) < 1
    assert abs(stage["y"] + stage["height"] - 12 - (note["y"] + note["height"])) < 1
    # On a phone it sat flush on the collapsed sheet's peek; it keeps 8px above it.
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": 390, "height": 844})
    playwright.expect(density).to_have_text("Pan or Contents to see all 33 articles")
    note, sheet = density.bounding_box(), page.locator(".bp-dag-inspector").bounding_box()
    assert note is not None and sheet is not None
    assert abs(sheet["y"] - 8 - (note["y"] + note["height"])) < 1


def test_atlas_hierarchy_keeps_topics_readable_and_details_out_of_the_way(
    desktop_page: object, explorer_url: str
) -> None:
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{explorer_url}/atlas.html")
    topic = page.locator('[data-autoform-node-id="atlas/topology"]')
    topic.wait_for()
    assert page.locator(".bp-dag-viewer").evaluate(
        "element => element.classList.contains('bp-dag-atlas')"
    )
    navigation = page.locator(".bp-dag-navigation")
    playwright.expect(navigation).to_be_visible()
    playwright.expect(navigation).to_contain_text("Knowledge atlas")
    # No guide pill over the map: the drawer and the dimming show a selection.
    playwright.expect(page.locator(".bp-dag-guide")).to_have_count(0)
    playwright.expect(navigation).to_contain_text("Geometry & Topology")
    playwright.expect(page.locator(".bp-dag-inspector")).to_be_hidden()
    # The closed drawer still holds the overview. Topics count once, by their least-complete item,
    # unlike the bars' items, and each status row carries the swatch and meaning the hidden page legend held.
    playwright.expect(page.locator(".bp-dag-inspector h3", has_text="status")).to_have_text(
        "Topics by least-complete status"
    )
    playwright.expect(
        page.locator('.bp-dag-inspector .bp-dag-swatch[title="Proved, and every prerequisite is fully proved too."]')
    ).to_have_count(1)
    stage_width = page.locator(".bp-dag-stage").bounding_box()["width"]
    topic_box = topic.bounding_box()
    assert topic_box is not None and topic_box["height"] >= 62 and topic_box["width"] >= 150
    assert "50%" not in topic.evaluate("element => getComputedStyle(element).borderRadius")
    # A neutral 1px frame; the statuses are a bar inside the card, not a ring around it.
    assert topic.evaluate("element => getComputedStyle(element).borderTopWidth") == "1px"
    ring = topic.evaluate(
        "element => getComputedStyle(element.querySelector('.bp-dag-node-core'), '::after').backgroundImage"
    )
    assert "linear-gradient" in ring
    # The bar follows the legend's state order: finished green before ready-to-prove blue.
    assert ring.index("rgb(34, 119, 58)") < ring.index("rgb(0, 100, 224)")
    # The region is canvas text, which assistive technology never reads.
    assert "Geometry & Topology" in (topic.get_attribute("aria-label") or "")
    topic.click()
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text("Topology")
    # A topic's pill counts its finished items, not its least-complete item's "ready to prove".
    playwright.expect(page.locator(".bp-dag-status-pill")).to_have_text("60 of 80 fully proved")
    # A topic has no declaration, so no kicker names its kind over the title.
    playwright.expect(page.locator(".bp-dag-inspector .bp-dag-kicker")).to_have_count(0)
    # The breakdown counts in the card's noun, not "80 items in this scope".
    playwright.expect(page.locator(".bp-dag-inspector h3", has_text="in this topic")).to_have_text(
        "80 articles in this topic"
    )
    playwright.expect(page.locator(".bp-dag-summary")).to_contain_text(
        "continuity, deformation, and invariants"
    )
    playwright.expect(page.get_by_role("link", name="Explore topic")).to_be_visible()
    playwright.expect(page.locator('[data-autoform-node-id="atlas/algebra"]')).to_have_attribute(
        "data-relation", "prerequisite"
    )
    selected_stage_width = page.locator(".bp-dag-stage").bounding_box()["width"]
    assert abs(selected_stage_width - stage_width) < 2
    playwright.expect(navigation.locator('[data-navigation-node-id="atlas/topology"]')).to_have_attribute(
        "aria-current", "true"
    )
    # A topic made only of module inventories says so instead of its rolled-up planned status.
    # The open drawer overlays the map, so this card is chosen without the pointer.
    page.locator('[data-autoform-node-id="atlas/library"]').dispatch_event("click")
    playwright.expect(page.locator(".bp-dag-status-pill")).to_have_text("inventory only")
    # Contents names each topic's region, which the map paints only on the canvas.
    page.get_by_role("button", name="Contents").click()
    topology = page.locator(".bp-dag-inventory-row").filter(has=page.get_by_text("Topology", exact=True))
    playwright.expect(topology.locator(".bp-dag-inventory-meta")).to_contain_text("Geometry & Topology")
    playwright.expect(topology).not_to_contain_text("atlas/topology")


def test_a_view_of_topics_and_results_draws_readable_result_cards(desktop_page: object, explorer_url: str) -> None:
    """An edgeless view with a topic drew its results as 76px atlas squares showing a few characters."""
    page = desktop_page
    page.goto(f"{explorer_url}/topics.html")
    title = page.locator('[data-autoform-node-id="topics/heine-borel"] .bp-dag-node-title')
    title.wait_for()
    assert not page.locator(".bp-dag-viewer").evaluate("element => element.classList.contains('bp-dag-atlas')")
    assert title.evaluate("element => element.scrollWidth <= element.clientWidth")
    meta = page.locator('[data-autoform-node-id="topics/compactness"] .bp-dag-node-meta')
    # A topic card names its fully proved share, which its status bar cannot.
    playwright.expect(meta).to_have_text("0 of 1 fully proved")
    # A drawn DAG counts its nodes by status; only an atlas counts topics by their least-complete item.
    playwright.expect(page.locator(".bp-dag-inspector h3")).to_have_text("Nodes by status")
    page.locator('[data-autoform-node-id="topics/library"]').click()
    playwright.expect(page.locator(".bp-dag-lean")).to_contain_text("Topology.lemma8 and 2 more")


def test_a_roadmap_without_articles_says_so(desktop_page: object, explorer_url: str) -> None:
    """An empty project view said that no node matched controls the reader never touched."""
    page = desktop_page
    page.goto(f"{explorer_url}/empty.html")
    empty = page.locator(".bp-dag-empty")
    playwright.expect(empty).to_be_visible()
    playwright.expect(empty).to_have_text("This roadmap has no articles yet.")


def test_a_click_on_empty_map_space_keeps_the_readers_zoom(desktop_page: object, explorer_url: str) -> None:
    """Every click on empty space re-fitted the camera, with or without a selection to clear."""
    page = desktop_page
    # Wide enough beside the hierarchy rail that the map fits at a readable scale and zooms about its centre.
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{explorer_url}/mixed.html")
    page.locator("[data-autoform-node-id]").first.wait_for()
    page.get_by_role("button", name="Zoom in").click()
    frames = "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
    transforms = "() => Array.from(document.querySelectorAll('.bp-dag-node'), node => node.style.transform)"

    def click_empty_space() -> None:
        empty = page.evaluate(
            """() => {
              const stage = document.querySelector(".bp-dag-stage").getBoundingClientRect();
              for (let y = stage.top + 8; y < stage.bottom; y += 16)
                for (let x = stage.left + 8; x < stage.right; x += 16)
                  if (document.elementFromPoint(x, y)?.classList.contains("bp-dag-canvas")) return [x, y];
              return null;
            }"""
        )
        assert empty is not None
        page.mouse.click(*empty)
        page.evaluate(frames)
        page.evaluate(frames)

    page.evaluate(frames)
    before = page.evaluate(transforms)
    click_empty_space()
    assert page.evaluate(transforms) == before

    # Clearing a selection keeps the zoom too; only clearing a path filter re-fits the map.
    node = page.locator('[data-autoform-node-id="mixed/b"]')
    node.click()
    playwright.expect(node).to_have_attribute("data-selected", "true")
    page.get_by_role("button", name="Zoom in").click()
    page.evaluate(frames)
    before = node.evaluate("node => node.style.transform")
    click_empty_space()
    playwright.expect(node).to_have_attribute("data-selected", "false")
    assert node.evaluate("node => node.style.transform") == before


def test_oversized_flat_scope_defaults_to_contents(desktop_page: object, explorer_url: str) -> None:
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{explorer_url}/flat.html")
    inventory = page.locator(".bp-dag-inventory")
    inventory.wait_for()
    playwright.expect(inventory).to_be_visible()
    playwright.expect(page.locator(".bp-dag-mode-button").first).to_be_disabled()
    # The first page of a list too long to draw.
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("100 of 1,000")
    playwright.expect(page.locator(".bp-dag-node")).to_have_count(0)


def test_choosing_from_contents_keeps_the_readers_place_and_focus(desktop_page: object, explorer_url: str) -> None:
    """Each choice from the list scrolled it to its top, and closing the details dropped focus to the page."""
    page = desktop_page
    page.goto(f"{explorer_url}/flat.html")
    page.locator(".bp-dag-inventory > .bp-dag-load-more").click()
    scroll = page.locator(".bp-dag-inventory-scroll")
    row = page.locator(".bp-dag-inventory-row").nth(150)
    row.scroll_into_view_if_needed()
    # Off centre, since focus scrolls a row it reveals to the centre and must not pass for a kept place.
    offset = scroll.evaluate("element => { element.scrollTop += 40; return element.scrollTop; }")
    assert offset > 0

    row.click()
    playwright.expect(row).to_have_attribute("aria-current", "true")
    assert scroll.evaluate("element => element.scrollTop") == offset
    close = page.get_by_role("button", name="Close details")
    close.press("Enter")
    playwright.expect(row).to_be_focused()
    playwright.expect(row).to_have_attribute("aria-current", "true")
    assert scroll.evaluate("element => element.scrollTop") == offset

    # Escape in the details clears the selection and returns focus to its row too.
    row.click()
    close.press("Escape")
    playwright.expect(page.locator(".bp-dag-has-selection")).to_have_count(0)
    playwright.expect(row).to_be_focused()
    assert scroll.evaluate("element => element.scrollTop") == offset

    # Back restores the selection and brings its row into view.
    scroll.evaluate("element => { element.scrollTop = 0; }")
    page.evaluate("history.back()")
    playwright.expect(row).to_have_attribute("aria-current", "true")
    playwright.expect(row).to_be_in_viewport()


def test_a_node_chosen_on_a_phone_lands_above_the_open_sheet(mobile_page: object, explorer_url: str) -> None:
    """Centring in the whole stage put the chosen node under the sheet its selection opens."""
    page = mobile_page
    _open_ready(page, explorer_url)
    search = page.locator(".bp-dag-search")
    search.fill("target")
    search.press("Enter")
    node = page.locator('[data-autoform-node-id="chapter/target"]')
    playwright.expect(node).to_be_focused()
    box, sheet = node.bounding_box(), page.locator(".bp-dag-inspector").bounding_box()
    assert box is not None and sheet is not None
    assert box["y"] + box["height"] <= sheet["y"]


def test_mobile_contents_pagination_stays_above_the_collapsed_sheet(
    mobile_page: object, explorer_url: str
) -> None:
    page = mobile_page
    page.goto(f"{explorer_url}/flat.html")
    more = page.locator(".bp-dag-inventory > .bp-dag-load-more")
    more.wait_for()
    more.scroll_into_view_if_needed()
    box = more.bounding_box()
    handle_box = page.locator(".bp-dag-sheet-handle").bounding_box()
    assert box is not None and handle_box is not None
    assert box["y"] + box["height"] <= handle_box["y"]
    more.tap()
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("200 of 1,000")


def test_mobile_isolates_do_not_overlap_the_connected_dag(mobile_page: object, explorer_url: str) -> None:
    page = mobile_page
    page.goto(f"{explorer_url}/mixed.html")
    connected = page.locator('[data-autoform-node-id="mixed/a"]')
    isolated = page.locator('[data-autoform-node-id="mixed/isolate-1"]')
    connected.wait_for()
    isolated.wait_for()
    a, b = connected.bounding_box(), isolated.bounding_box()
    assert a is not None and b is not None
    assert a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"]
    page.locator(".bp-dag-filter-summary").tap()
    playwright.expect(page.get_by_role("checkbox", name="Show unlinked")).to_be_visible()


def test_a_plain_wheel_pans_the_full_page_explorer(desktop_page: object, explorer_url: str) -> None:
    """The full-page explorer leaves nothing else to scroll, and a plain wheel or trackpad scroll did nothing there."""
    page = desktop_page
    page.goto(f"{explorer_url}/index.html")
    node = page.locator('[data-autoform-node-id="chapter/middle"]')
    node.wait_for()
    frames = "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
    page.evaluate(frames)
    before = node.bounding_box()
    stage = page.locator(".bp-dag-stage").bounding_box()
    assert before is not None and stage is not None

    page.mouse.move(stage["x"] + stage["width"] / 2, stage["y"] + stage["height"] / 2)
    page.mouse.wheel(0, -120)
    page.evaluate(frames)
    after = node.bounding_box()
    assert after is not None
    # Scrolling up moves the map down, at the same zoom.
    assert after["y"] > before["y"]
    assert abs(after["height"] - before["height"]) < 0.5

    # Control with the wheel zooms by the size of the step, where it once took a fixed 13% step.
    scale = r"node => Number(/scale\(([^)]+)\)/.exec(node.style.transform)[1])"
    unzoomed = node.evaluate(scale)
    page.keyboard.down("Control")
    page.mouse.wheel(0, -10)
    page.keyboard.up("Control")
    page.evaluate(frames)
    assert 1.0 < node.evaluate(scale) / unzoomed < 1.1


def test_a_drag_that_starts_on_a_card_pans_and_a_tap_still_opens_it(desktop_page: object, explorer_url: str) -> None:
    """Presses on cards were ignored by the map, so on a phone, where cards cover most of it, a drag did nothing."""
    page = desktop_page
    _open_ready(page, explorer_url)
    node = page.locator('[data-autoform-node-id="chapter/middle"]')
    frames = "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
    page.evaluate(frames)
    hit = """node => {
      const box = node.getBoundingClientRect();
      return node.contains(document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2));
    }"""
    assert node.evaluate(hit) is True
    before = node.bounding_box()
    assert before is not None
    x, y = before["x"] + before["width"] / 2, before["y"] + before["height"] / 2

    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 60, y + 40, steps=6)
    page.mouse.up()
    page.evaluate(frames)
    after = node.bounding_box()
    assert after is not None
    # The first step past 8px carries the whole way travelled, so the card ends under the pointer.
    assert abs(after["x"] - before["x"] - 60) < 1 and abs(after["y"] - before["y"] - 40) < 1
    playwright.expect(node).to_have_attribute("data-selected", "false")
    assert "node=" not in page.url

    # A press that wobbles less than 8px is still a tap.
    assert node.evaluate(hit) is True
    page.mouse.move(x + 60, y + 40)
    page.mouse.down()
    page.mouse.move(x + 64, y + 43, steps=2)
    page.mouse.up()
    playwright.expect(node).to_have_attribute("data-selected", "true")
    assert "node=chapter%2Fmiddle" in page.url


def test_search_keyboard_navigation_and_focus(desktop_page: object, explorer_url: str) -> None:
    page = desktop_page
    _open_ready(page, explorer_url)
    search = _load_search_index(page)
    results = page.locator(".bp-dag-search-results")
    announcement = page.locator(".bp-dag-sr-only")
    # A search box takes no aria-expanded; the result count is announced instead.
    assert search.get_attribute("aria-expanded") is None
    search.fill("no such node")
    playwright.expect(announcement).to_have_text("No results")
    search.fill("middle")
    playwright.expect(results).to_be_visible()
    # The target's statement mentions the middle lemma, so it matches too.
    playwright.expect(announcement).to_have_text("2 results")

    search.press("ArrowDown")
    result = page.locator(".bp-dag-result").first
    playwright.expect(result).to_be_focused()
    result.press("Escape")
    playwright.expect(search).to_be_focused()
    playwright.expect(results).to_be_hidden()
    # Focus moving from the results to a control outside them closes them too.
    search.press("ArrowDown")
    playwright.expect(result).to_be_focused()
    page.get_by_role("button", name="Contents").focus()
    playwright.expect(results).to_be_hidden()

    search.fill("target")
    search.press("Enter")
    playwright.expect(page.locator('[data-autoform-node-id="chapter/target"]')).to_be_focused()
    assert "node=chapter%2Ftarget" in page.url


def test_a_search_result_in_this_view_selects_the_drawn_node(desktop_page: object, explorer_url: str) -> None:
    """Index records carry no layout, so choosing one blanked the map and linked back to the explorer."""
    page = desktop_page
    _open_ready(page, explorer_url)
    search = _load_search_index(page)
    for query, node_id in (("target", "chapter/target"), ("middle", "chapter/middle")):
        search.fill(query)
        if query == "target":
            search.press("Enter")
        else:
            page.locator(".bp-dag-result", has_text="Middle lemma").click()
        node = page.locator(f'[data-autoform-node-id="{node_id}"]')
        playwright.expect(node).to_have_attribute("data-selected", "true")
        # Focus may scroll the page, so both boxes are read once it has moved.
        playwright.expect(node).to_be_focused()
        box, stage = node.bounding_box(), page.locator(".bp-dag-stage").bounding_box()
        assert box is not None and stage is not None
        assert stage["x"] <= box["x"] and box["x"] + box["width"] <= stage["x"] + stage["width"]
        assert stage["y"] <= box["y"] and box["y"] + box["height"] <= stage["y"] + stage["height"]
        assert page.locator(".bp-dag-node").count() > 1
        playwright.expect(page.get_by_role("link", name="Open article")).to_have_attribute(
            "href", f"{explorer_url}/articles.html#{node_id}"
        )


def test_a_result_neither_drawn_here_nor_safely_linked_selects_nothing(
    desktop_page: object, explorer_url: str
) -> None:
    """Such a record went to chooseNode, which selected a node the map does not draw."""
    page = desktop_page

    def add_unlinked_record(route: object) -> None:
        index = route.fetch().json()
        index["nodes"].append(
            {"id": "elsewhere/unlinked", "kind": "node", "status": "planned", "title": "Unlinked result", "url": None}
        )
        route.fulfill(json=index)

    page.route("**/search.json", add_unlinked_record)
    _open_ready(page, explorer_url)
    search = _load_search_index(page)
    search.fill("Unlinked")
    url = page.url
    page.locator(".bp-dag-result", has_text="Unlinked result").click()
    page.evaluate("() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")
    playwright.expect(page.locator(".bp-dag-has-selection")).to_have_count(0)
    assert page.url == url


def test_a_contents_query_lists_only_this_scope(desktop_page: object, explorer_url: str) -> None:
    """Whole-roadmap matches belong to the search dropdown, not under "Contents".

    The dropdown opened on every keystroke and covered the list being filtered,
    so in Contents it opens only when asked: by Down from the search box, or the
    summary's button, which a touch screen can reach.
    """
    page = desktop_page
    _open_ready(page, explorer_url)
    search = _load_search_index(page)
    page.get_by_role("button", name="Contents").click()
    search.fill("theorem")
    playwright.expect(page.locator(".bp-dag-inventory-title")).to_have_text(["Target theorem"])
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("1 of 63 · search the whole roadmap")
    results = page.locator(".bp-dag-search-results")
    playwright.expect(results).to_be_hidden()
    search.press("ArrowDown")
    playwright.expect(page.locator(".bp-dag-result").first).to_be_focused()
    page.keyboard.press("Escape")
    playwright.expect(results).to_be_hidden()
    page.get_by_role("button", name="search the whole roadmap").click()
    playwright.expect(results.locator(".bp-dag-result", has_text="Remote theorem")).to_be_visible()


def test_global_search_jumps_to_the_smallest_context(desktop_page: object, explorer_url: str) -> None:
    page = desktop_page
    _open_ready(page, explorer_url)
    search = page.locator(".bp-dag-search")
    # A Lean name finds its entry, which shows its status rather than its id or why it matched.
    search.fill("Remote.Namespace.deepTheorem")
    first = page.locator(".bp-dag-result").first
    playwright.expect(first.locator(".bp-dag-result-title")).to_have_text("Remote theorem")
    playwright.expect(first.locator(".bp-dag-result-meta")).to_have_text("planned")
    playwright.expect(first).not_to_contain_text("remote/deep-theorem")
    search.fill("spectral gap")
    playwright.expect(page.locator(".bp-dag-result", has_text="Remote theorem")).to_be_visible()
    search.fill("Remote theorem")
    result = page.locator(".bp-dag-result", has_text="Remote theorem")
    result.wait_for()
    result.click()
    page.wait_for_url("**/other.html#node=remote%2Fdeep-theorem")


def test_legacy_node_hash_resolves_through_the_global_index(desktop_page: object, explorer_url: str) -> None:
    page = desktop_page
    page.goto(f"{explorer_url}/index.html#node=remote%2Fdeep-theorem")
    page.wait_for_url("**/other.html#node=remote%2Fdeep-theorem")

    navigations: list[str] = []
    page.on("request", lambda request: navigations.append(request.url) if request.is_navigation_request() else None)
    with page.expect_response("**/search.json"):
        page.goto(f"{explorer_url}/index.html#node=unknown%2Fitem")
    # The busy state clears in the same promise chain, just before the redirect decision.
    playwright.expect(page.locator(".bp-dag-search")).not_to_have_attribute("aria-busy", "true")
    # One more task lets a redirect that decision started reach the network.
    page.evaluate("() => new Promise(done => setTimeout(done))")
    assert page.url.endswith("index.html#node=unknown%2Fitem")
    assert len(navigations) == 1


def test_paginated_search_and_arrow_navigation_restore_focus(
    desktop_page: object, explorer_url: str
) -> None:
    page = desktop_page
    _open_ready(page, explorer_url)
    foundation = page.locator('[data-autoform-node-id="chapter/foundation"]')
    foundation.focus()
    foundation.press("ArrowRight")
    playwright.expect(page.locator('[data-autoform-node-id="chapter/middle"]')).to_be_focused()

    search = _load_search_index(page)
    search.fill("auxiliary")
    more = page.locator(".bp-dag-search-results .bp-dag-load-more")
    playwright.expect(more).to_have_text("Show 48 more of 48")
    more.focus()
    more.press("Enter")
    playwright.expect(page.locator(".bp-dag-result").nth(12)).to_be_focused()


def _release(page: object, held: list[object]) -> None:
    """Let the oldest held request through once Playwright has delivered it."""
    for _ in range(100):
        if held:
            held.pop(0).continue_()
            return
        page.evaluate("0")
    raise AssertionError("the held request never arrived")


def test_a_late_search_index_keeps_focus_and_leaves_a_closed_panel_closed(
    desktop_page: object, explorer_url: str
) -> None:
    """The index callback rebuilt the dropdown, dropping focus to the page and reopening a closed panel."""
    page = desktop_page
    held: list[object] = []
    page.route("**/search.json", lambda route: held.append(route))
    _open_ready(page, explorer_url)
    search = page.locator(".bp-dag-search")
    results = page.locator(".bp-dag-search-results")
    search.fill("auxiliary")
    more = results.locator(".bp-dag-load-more")
    more.focus()
    more.press("Enter")
    thirteenth = page.locator(".bp-dag-result").nth(12)
    playwright.expect(thirteenth).to_be_focused()
    _release(page, held)
    playwright.expect(search).not_to_have_attribute("aria-busy", "true")
    playwright.expect(thirteenth).to_be_focused()
    page.mouse.click(2, 2)
    playwright.expect(results).to_be_hidden()

    page.reload()
    page.locator("[data-autoform-node-id]").first.wait_for()
    search.fill("auxiliary")
    search.press("Escape")
    playwright.expect(results).to_be_hidden()
    _release(page, held)
    playwright.expect(search).not_to_have_attribute("aria-busy", "true")
    playwright.expect(results).to_be_hidden()


def test_direct_list_view_has_current_shown_and_hidden_counts(
    desktop_page: object, explorer_url: str
) -> None:
    page = desktop_page
    page.goto(f"{explorer_url}/index.html#view=list&status=fully_proved")
    page.locator(".bp-dag-inventory").wait_for()
    # The reviewed #411 count controls replace the redundant always-present stats string.
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("1 of 63")
    playwright.expect(page.locator(".bp-dag-hidden-button")).to_have_text("Show 62 filtered out")
    playwright.expect(page.locator(".bp-dag-inventory-row")).to_have_count(1)


def test_hidden_items_without_edges_stay_hidden_after_a_reload(desktop_page: object, explorer_url: str) -> None:
    """The location records only that they are hidden, so a reload has to read that back."""
    page = desktop_page
    _open_ready(page, explorer_url)
    # The toggle is the filter popover's second group.
    page.locator(".bp-dag-filter-summary").click()
    isolates = page.locator(".bp-dag-filter-popover fieldset + fieldset").get_by_role(
        "checkbox", name="Show unlinked"
    )
    isolates.uncheck()
    page.wait_for_function("location.hash === '#isolates=0'")

    page.reload()
    page.locator("[data-autoform-node-id]").first.wait_for()
    playwright.expect(page.locator(".bp-dag-hidden-button")).to_have_text("Show 60 filtered out")
    page.locator(".bp-dag-filter-summary").click()
    playwright.expect(isolates).not_to_be_checked()


def test_reselect_does_not_duplicate_history_and_back_restores_selection(
    desktop_page: object, explorer_url: str
) -> None:
    page = desktop_page
    _open_ready(page, explorer_url)
    foundation = page.locator('[data-autoform-node-id="chapter/foundation"]')
    target = page.locator('[data-autoform-node-id="chapter/target"]')
    initial_length = page.evaluate("history.length")

    foundation.click()
    after_first_selection = page.evaluate("history.length")
    foundation.click()
    assert page.evaluate("history.length") == after_first_selection == initial_length + 1

    page.get_by_role("button", name="Close details").click()
    target.click()
    # Focus follows the click two frames later; waiting for it keeps it from
    # landing during the arrow keys below.
    playwright.expect(target).to_be_focused()
    assert page.evaluate("history.length") == initial_length + 2
    page.evaluate("history.back()")
    page.wait_for_function("location.hash.includes('chapter%2Ffoundation')")
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text("Foundation")
    playwright.expect(foundation).to_have_attribute("data-selected", "true")

    # Arrow keys browse without a history entry per step, so one Back undoes them all.
    middle = page.locator('[data-autoform-node-id="chapter/middle"]')
    foundation.focus()
    foundation.press("ArrowRight")
    playwright.expect(middle).to_be_focused()
    middle.press("ArrowRight")
    playwright.expect(target).to_be_focused()
    page.evaluate("history.back()")
    page.wait_for_function("!location.hash.includes('node=')")

    # From an unselected map the first arrow key adds the entry, so Back returns to the map, not the page before.
    page.goto("about:blank")
    _open_ready(page, explorer_url)
    foundation.focus()
    foundation.press("ArrowRight")
    playwright.expect(middle).to_be_focused()
    middle.press("ArrowRight")
    playwright.expect(target).to_be_focused()
    page.evaluate("history.back()")
    page.wait_for_function("!location.hash.includes('node=')")
    assert page.url == f"{explorer_url}/index.html"


def test_hiding_the_selected_status_clears_selection(
    desktop_page: object, explorer_url: str
) -> None:
    page = desktop_page
    _open_ready(page, explorer_url)
    page.locator('[data-autoform-node-id="chapter/target"]').click()
    assert "node=chapter%2Ftarget" in page.url

    summary = page.locator(".bp-dag-filter-summary")
    popover = page.locator(".bp-dag-filter-popover")
    summary.click()
    # Each status row carries its colour; Escape and a press outside both close the popover.
    playwright.expect(popover.locator(".bp-dag-swatch")).to_have_count(3)
    popover.locator("input").first.press("Escape")
    playwright.expect(popover).to_be_hidden()
    playwright.expect(summary).to_be_focused()
    summary.click()
    page.mouse.click(2, 2)
    playwright.expect(popover).to_be_hidden()

    summary.click()
    page.locator('.bp-dag-filter-popover input[value="planned"]').uncheck()

    assert "node=" not in page.url
    assert "bp-dag-has-selection" not in (page.locator(".bp-dag-viewer").get_attribute("class") or "")


def test_no_javascript_keeps_the_linked_fallback(webkit_browser: object, explorer_url: str) -> None:
    with _page(webkit_browser, java_script_enabled=False) as page:
        page.goto(f"{explorer_url}/index.html")

        fallback = page.locator(".bp-dag-fallback")
        playwright.expect(fallback).to_be_visible()
        playwright.expect(fallback.locator("li")).to_have_count(3)
        playwright.expect(fallback.get_by_role("link", name="Download the complete graph data")).to_have_attribute(
            "href", "graph.json"
        )


def test_fetch_failure_restores_the_linked_fallback(desktop_page: object, explorer_url: str) -> None:
    page = desktop_page
    page.goto(f"{explorer_url}/failure.html")

    playwright.expect(page.locator(".bp-dag-error")).to_contain_text("HTTP 404")
    fallback = page.locator(".bp-dag-fallback")
    playwright.expect(fallback).to_be_visible()
    playwright.expect(fallback.get_by_role("link", name="Middle lemma")).to_have_attribute(
        "href", "articles.html#chapter/middle"
    )


def test_duplicate_script_include_mounts_one_explorer(
    desktop_page: object, explorer_url: str
) -> None:
    page = desktop_page
    page.goto(f"{explorer_url}/duplicate.html")
    page.locator(".bp-dag-stage").wait_for()
    playwright.expect(page.locator(".bp-dag-head")).to_have_count(1)


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
    # A second tap covers the map; a third returns to the peek.
    handle.click()
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_class(re.compile(r"\bbp-dag-sheet-full\b"))
    playwright.expect(headings).to_have_count(1)
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
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
    search = _load_search_index(page)
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
    search = _load_search_index(page)
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
    playwright.expect(page.get_by_role("group", name="Links", exact=True)).to_be_hidden()


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
    desktop_page: object, explorer_url: str
) -> None:
    """Focus inside the explorer lets "/" open its search, but no shortcut takes a key from an input."""
    page = desktop_page
    _open_ready(page, explorer_url)
    search, wrap = page.locator(".bp-dag-search"), page.locator(".bp-dag-search-wrap")
    playwright.expect(search).to_have_attribute("placeholder", "Search titles and Lean names")
    # The box draws a masked magnifier and a "/" key hint, which focus hides.
    assert "svg" in wrap.evaluate("element => getComputedStyle(element, '::before').webkitMaskImage")
    hint = "element => [getComputedStyle(element, '::after').content, getComputedStyle(element, '::after').display]"
    assert wrap.evaluate(hint) == ['"/"', "block"]
    page.locator("[data-autoform-node-id]").first.focus()
    page.keyboard.press("/")
    playwright.expect(search).to_be_focused()
    assert wrap.evaluate(hint)[1] == "none"
    page.locator(".bp-dag-filter-summary").click()
    isolates = page.get_by_role("checkbox", name="Show unlinked")
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
    remote_card = full_math("topology/result", 5)
    # The map names the selected result, and its own relations open in place of a separate strip.
    playwright.expect(page.locator(".bp-map-context")).to_have_text("Result · uses 1 · used by 55")
    playwright.expect(page.locator(".bp-reading-neighbors")).to_have_count(0)
    relations = remote_card.locator("details.bp-dependencies")
    playwright.expect(relations).to_have_attribute("open", "")
    playwright.expect(relations.locator("summary")).to_have_text("Uses 1 · Used by 55")
    playwright.expect(relations.locator("a.bp-ref")).to_have_count(56)
    last = relations.get_by_role("link", name=re.compile(r"\(Later 54\)$"))
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



def _record_label_paints(page: object, *, reject_family: bool) -> None:
    page.add_init_script(
        """
        const rejectFamily = __REJECT_FAMILY__;
        window.labelPaints = [];
        const prototype = CanvasRenderingContext2D.prototype;
        const paint = prototype.fillText;
        const font = Object.getOwnPropertyDescriptor(prototype, 'font');
        if (rejectFamily) {
          Object.defineProperty(prototype, 'font', {
            configurable: font.configurable, enumerable: font.enumerable, get: font.get,
            set(value) {
              if (String(value).includes('Plus Jakarta Sans')) return;
              font.set.call(this, value);
            }
          });
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
def test_canvas_label_paints_use_the_site_family_or_a_valid_fallback(
    desktop_page: object, count_label_site_url: str, reject_family: bool
) -> None:
    page, site_url = desktop_page, count_label_site_url
    _record_label_paints(page, reject_family=reject_family)
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
    page.wait_for_function("window.labelPaints.some(paint => paint.text === '×12')")
    painted = page.evaluate("window.labelPaints.filter(paint => paint.text === '×12').at(-1)")
    family = page.locator(".bp-dag-viewer").evaluate("host => getComputedStyle(host).fontFamily")
    assert ("Plus Jakarta Sans" if reject_family else "Times New Roman") in family
    pixels = float(re.search(r"(\d+(?:\.\d+)?)px", painted["font"]).group(1))
    assert pixels >= 11 and painted["font"].startswith("600 ")
    assert ("sans-serif" if reject_family else "Times New Roman") in painted["font"]
    assert _canvas_pixels(page)["painted"] > 0
    measured = page.evaluate(
        """({text, family, pixels}) => {
          const ctx = document.createElement('canvas').getContext('2d');
          ctx.font = '600 ' + pixels + 'px ' + family;
          const expected = ctx.measureText(text).width;
          ctx.font = '600 ' + pixels + 'px sans-serif';
          return {expected, generic: ctx.measureText(text).width};
        }""",
        {"text": painted["text"], "family": "sans-serif" if reject_family else family, "pixels": pixels},
    )
    assert abs(painted["width"] - measured["expected"]) < .1
    if not reject_family:
        assert abs(painted["width"] - measured["generic"]) > .5, "The live labels ignored the site's serif family"



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
    desktop_page: object, explorer_url: str, width: int
) -> None:
    page = desktop_page
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": width, "height": 900})
    if width == 1000:
        def trail_without_book(route: object) -> None:
            payload = route.fetch().json()
            payload["view"]["book"] = None
            payload["view"]["breadcrumbs"] = [
                {"label": "Mathematics", "url": "articles.html"},
                {"label": "Standalone dependency explorer", "url": None},
            ]
            route.fulfill(json=payload)

        page.route("**/graph.json", trail_without_book)
    _open_ready(page, explorer_url)
    _assert_sheet_state(page, selected=None, opened=False)
    if width == 390:
        page.locator(".bp-dag-sheet-handle").click()
        _assert_sheet_state(page, selected=None, opened=True)
        page.locator(".bp-dag-sheet-handle").click()
        _assert_sheet_state(page, selected=None, opened=False)

    foundation = page.locator('[data-autoform-node-id="chapter/foundation"]')
    middle = page.locator('[data-autoform-node-id="chapter/middle"]')
    original = foundation.bounding_box()
    assert original is not None
    foundation.click()
    _assert_sheet_state(page, selected="chapter/foundation", opened=True)
    if width == 1000:
        assert abs(foundation.bounding_box()["y"] - original["y"]) < 2, "Opening the trail row moved the clicked card"
    page.get_by_role("button", name="Close details", exact=True).click()
    _assert_sheet_state(page, selected="chapter/foundation", opened=False)
    if width == 1000:
        assert abs(foundation.bounding_box()["y"] - original["y"]) < 2, "Closing the trail row moved the selection"
    foundation.click()
    _assert_sheet_state(page, selected="chapter/foundation", opened=True)
    foundation.press("Escape")
    _assert_sheet_state(page, selected=None, opened=False)
    if width == 1000:
        assert abs(foundation.bounding_box()["y"] - original["y"]) < 2, "Clearing selection changed the map's vertical position"
    assert "node=" not in page.url

    foundation.click()
    middle.click()
    _assert_sheet_state(page, selected="chapter/middle", opened=True)
    page.locator(".bp-dag-filter-summary").click()
    page.locator('.bp-dag-filter-popover input[value="can_prove"]').uncheck()
    _assert_sheet_state(page, selected=None, opened=False)
    page.evaluate("history.back()")
    page.wait_for_function("location.hash.includes('node=chapter%2Ffoundation')")
    _assert_sheet_state(page, selected="chapter/foundation", opened=True)
    page.evaluate("history.forward()")
    page.wait_for_function("!new URLSearchParams(location.hash.slice(1)).has('node')")
    _assert_sheet_state(page, selected=None, opened=False)
    playwright.expect(page.locator('.bp-dag-filter-popover input[value="can_prove"]')).not_to_be_checked()



def test_edges_group_and_status_summary_follow_checkbox_and_history_state(
    desktop_page: object, explorer_url: str
) -> None:
    page = desktop_page
    _open_ready(page, explorer_url)
    summary = page.locator(".bp-dag-filter-summary")
    middle = page.locator('[data-autoform-node-id="chapter/middle"]')
    foundation = page.locator('[data-autoform-node-id="chapter/foundation"]')
    middle.click()
    summary.click()
    edges = page.get_by_role("group", name="Links", exact=True)
    playwright.expect(edges).to_be_visible()
    isolates = edges.get_by_role("checkbox", name="Show unlinked", exact=True)
    playwright.expect(summary).to_have_text("Filter: all statuses")
    isolates.uncheck()
    playwright.expect(summary).to_have_text("Filter: all statuses · linked only")
    foundation.click()
    summary.click()
    isolates.check()
    playwright.expect(summary).to_have_text("Filter: all statuses")
    page.evaluate("history.back()")
    page.wait_for_function("location.hash.includes('isolates=0')")
    playwright.expect(isolates).not_to_be_checked()
    playwright.expect(summary).to_have_text("Filter: all statuses · linked only")
    page.evaluate("history.forward()")
    page.wait_for_function("!location.hash.includes('isolates=0')")
    playwright.expect(isolates).to_be_checked()
    playwright.expect(summary).to_have_text("Filter: all statuses")
    page.locator('.bp-dag-filter-popover input[value="planned"]').uncheck()
    playwright.expect(summary).to_have_text("Filter: 2 of 3 statuses")
    isolates.uncheck()
    playwright.expect(summary).to_have_text("Filter: 2 of 3 statuses · linked only")



def test_details_close_from_their_corner_and_space_the_status_from_the_link(
    desktop_page: object, explorer_url: str
) -> None:
    """The close button floated beside the kicker, and the status text touched the Open article button."""
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{explorer_url}/index.html#node=chapter%2Fmiddle")
    playwright.expect(page.locator(".bp-dag-inspector h2")).to_have_text("Middle lemma")
    drawer, close, heading, pill, link = (
        page.locator(selector).bounding_box()
        for selector in (".bp-dag-inspector", ".bp-dag-close", ".bp-dag-inspector h2", ".bp-dag-status-pill", ".bp-dag-open")
    )
    assert drawer is not None and close is not None and heading is not None and pill is not None and link is not None
    assert (close["width"], close["height"]) == (32, 32)
    # 10px inside the drawer's 1px border, at its top-right corner, with the title stopping short of it.
    assert abs(drawer["x"] + drawer["width"] - 11 - (close["x"] + close["width"])) < 1.5
    assert abs(close["y"] - drawer["y"] - 11) < 1.5
    assert heading["x"] + heading["width"] <= close["x"]
    # The status and the button share a line, 16px apart.
    assert abs(pill["y"] + pill["height"] / 2 - (link["y"] + link["height"] / 2)) < 1.5
    assert abs(link["x"] - (pill["x"] + pill["width"]) - 16) < 1.5



def test_details_read_as_mathematics_not_as_instructions(desktop_page: object, explorer_url: str) -> None:
    """The drawer explained the map's controls, and its path pivots sat under a "Path spotlight" heading."""
    page = desktop_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{explorer_url}/index.html#node=chapter%2Fmiddle")
    inspector = page.locator(".bp-dag-inspector")
    playwright.expect(inspector.locator("h2")).to_have_text("Middle lemma")
    playwright.expect(inspector.locator(".bp-dag-kicker")).to_have_text("lemma")
    playwright.expect(inspector).not_to_contain_text("Path spotlight")
    # The relation lists come first; the pivots below them only steer the map's highlight.
    playwright.expect(inspector.locator(".bp-dag-section h3")).to_have_text(["Uses (1)", "Used by (1)", "Highlight on map"])
    playwright.expect(inspector.get_by_role("group", name="Highlight on map")).to_have_count(1)
    pivots = inspector.locator(".bp-dag-pivot")
    playwright.expect(pivots).to_have_text(["All", "Uses", "Used by"])
    playwright.expect(pivots.first).to_have_attribute("aria-pressed", "true")
    lists = inspector.locator(".bp-dag-relation-list").last.bounding_box()
    assert lists is not None and pivots.first.bounding_box()["y"] > lists["y"] + lists["height"]
    # One segmented control: the three pivots share a row inside one border.
    tops = {round(pivots.nth(index).bounding_box()["y"]) for index in range(3)}
    assert len(tops) == 1
    page.get_by_role("button", name="Close details").click()
    page.keyboard.press("Escape")
    playwright.expect(page.locator(".bp-dag-has-selection")).to_have_count(0)
    # The overview opens with the view's title, with no kicker over it and no closing instructions.
    playwright.expect(inspector.locator(".bp-dag-kicker")).to_have_count(0)
    playwright.expect(inspector).not_to_contain_text("keyboard-accessible")



@pytest.mark.parametrize(("name", "counts"), (("mixed", "4 items · 1 dependency"), ("flat", "1,000 items · 0 dependencies")))
def test_the_rail_counts_in_words_that_agree_with_their_numbers(
    desktop_page: object, explorer_url: str, name: str, counts: str
) -> None:
    """The rail read "1 dependencies" and printed a count without its plural."""
    page = desktop_page
    page.goto(f"{explorer_url}/{name}.html")
    playwright.expect(page.locator(".bp-dag-navigation-counts")).to_have_text(counts)



def test_rail_rows_end_in_a_status_swatch_and_a_lone_group_goes_unnamed(
    desktop_page: object, explorer_url: str
) -> None:
    """Each leaf row ended in a status phrase of its own length, and a lone "Inside this scope" headed the rail."""
    page = desktop_page
    _open_ready(page, explorer_url)
    navigation = page.locator(".bp-dag-navigation")
    row = navigation.locator('[data-navigation-node-id="chapter/middle"]')
    playwright.expect(row).to_have_text("Middle lemma")
    swatch = row.get_by_role("img", name="ready to prove")
    playwright.expect(swatch).to_have_attribute("title", "ready to prove")
    playwright.expect(navigation.locator(".bp-dag-navigation-group h2")).to_have_count(0)
    # Topics keep their item counts, and several areas keep their headings.
    page.goto(f"{explorer_url}/atlas.html")
    topology = navigation.locator('[data-navigation-node-id="atlas/topology"]')
    playwright.expect(topology.locator(".bp-dag-navigation-meta")).to_have_text("80")
    playwright.expect(navigation.locator(".bp-dag-navigation-group h2").first).to_be_visible()



def test_the_toolbar_counts_only_what_it_hides_and_shows_it_on_request(
    desktop_page: object, explorer_url: str
) -> None:
    """The toolbar always said "N of M shown", hiding nothing or not."""
    page = desktop_page
    page.goto(f"{explorer_url}/index.html#view=list&status=fully_proved")
    page.locator(".bp-dag-inventory").wait_for()
    reveal, summary = page.locator(".bp-dag-hidden-button"), page.locator(".bp-dag-inventory-summary")
    playwright.expect(reveal).to_have_text("Show 62 filtered out")
    playwright.expect(summary).to_have_text("1 of 63")
    reveal.click()
    playwright.expect(reveal).to_be_hidden()
    playwright.expect(summary).to_have_text("63 items")



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
    # The page opens at half; taps go on to full and back to the peek.
    handle.click()
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    node = page.locator('.bp-dag-node[data-autoform-node-id="algebra/result"]')
    _paint_frame(page)
    original = node.bounding_box()
    identity = node.element_handle()
    url, history_length = page.url, page.evaluate("history.length")
    assert original is not None
    for _ in range(2):
        # Full hides the toolbar, and the map shifts back by the head it frees.
        for _level in ("half", "full"):
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



def test_a_phone_reader_opens_at_half_snaps_where_it_is_dragged_and_maps_back_from_full(
    mobile_page: object, site_url: str,
) -> None:
    """The phone reader covered the map on arrival, said only "Reading", and ignored a drag on its grabber."""
    page = _open_site(mobile_page, site_url, "dependencies/chapters/algebra.html#node=algebra%2Fresult")
    viewer, pane, body = page.locator(".bp-dag-viewer"), page.locator(".bp-reading-pane"), page.locator(".bp-dag-body")
    handle = page.get_by_role("button", name="Toggle mathematical reading", exact=True)
    full = re.compile(r"\bbp-dag-sheet-full\b")
    saved = "() => history.state.autoformNavigation.snapshot.readingOpen"
    # Half leaves the top 45% to the map, with the selected node above the sheet.
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(viewer).not_to_have_class(full)
    playwright.expect(handle).to_have_text("Result")
    playwright.expect(handle.locator(".bp-dag-swatch")).to_have_count(1)
    _paint_frame(page)
    sheet, area = pane.bounding_box(), body.bounding_box()
    node = page.locator('.bp-dag-node[data-autoform-node-id="algebra/result"]').bounding_box()
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


def test_the_canvas_paints_its_labels_in_the_sites_family(desktop_page: object, count_label_site_url: str) -> None:
    """Verify a real routed count paint, not a scratch context or retired region-frame label."""
    page, site_url = desktop_page, count_label_site_url
    _record_label_paints(page, reject_family=False)
    canonical = _site_route(page, site_url, "roadmap")
    _open_site(page, site_url, urlsplit(canonical).path.lstrip("/"))
    page.wait_for_function("window.labelPaints.some(paint => paint.text === '×12')")
    painted = page.evaluate("window.labelPaints.filter(paint => paint.text === '×12').at(-1)")
    family = page.locator(".bp-dag-viewer").evaluate("host => getComputedStyle(host).fontFamily")
    assert "Plus Jakarta Sans" in family and "Plus Jakarta Sans" in painted["font"]
    pixels = float(re.search(r"(\d+(?:\.\d+)?)px", painted["font"]).group(1))
    assert pixels >= 11 and painted["font"].startswith("600 ")
    assert _canvas_pixels(page)["painted"] > 0
    expected = page.evaluate(
        """({text, family, pixels}) => {
          const ctx=document.createElement('canvas').getContext('2d');
          ctx.font='600 '+pixels+'px '+family; return ctx.measureText(text).width;
        }""",
        {"text": painted["text"], "family": family, "pixels": pixels},
    )
    assert abs(painted["width"] - expected) < .1
