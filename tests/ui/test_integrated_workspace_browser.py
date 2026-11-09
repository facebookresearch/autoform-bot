"""Actual book/map workspace journeys, with no graph-client suppression.

The public synthetic consumer has eighteen authored topics, two authored areas
and two explicit source tracks. Its complete statements are longer than graph
previews. Lean fixture declarations are simple zero identities; these tests
exercise publication and UI behavior, not kernel verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import html5lib
import pytest

from autoform_cli.scaffold import scaffold_project
from autoform_cli.ui.mathjax import MATHJAX_BUNDLE, MATHJAX_VERSION
from autoform_cli.ui.render import render_site
from tests.ui.test_integrated_explorer_browser import (
    _PinnedRuntime,
    _math_menu_evidence,
    _math_source,
    _routes,
    _served,
    playwright,
)
from tests.ui.test_integrated_publication import _decoded_relations


_LIBRARY = "Mathematical library"
_ANCESTOR = "Geometric configurations, foundational notation, and a deliberately long authored ancestor label"
_DEEP_TITLE = "Detailed identities, their complete explanations, and the surrounding elementary statements"
_TARGET = "division-00/part-01/results/main"
_OTHER = "division-09/main"
_ISOLATED = "division-00/part-01/results/isolated"
_READY = "division-00/part-01/results/ready"
_BASE = "division-00/part-01/results/base"
_AREAS = ("Geometry", "Analysis", "Unstarted mathematics")
_TARGET_TITLE = "Complete zero identity with full mathematical notation"
_OTHER_TITLE = "Counterpart zero identity"
_SOURCE_TITLES = ("Classical source essays", "Contemporary source records")
_TARGET_TEX = (r"0=0", r"\RR\ni0", r"\frac{0}{2}=0", r"\overline{0}=0", r"\sum_{i=1}^{n}0=0")
_LAST_SENTENCE = "This final mathematical sentence is retained in the complete reading page."


@dataclass(frozen=True)
class _WorkspaceSite:
    url: str
    records: dict[str, dict]
    root_graph: dict
    wiki_layer: dict | None = None
    wiki_pairs: frozenset[tuple[str, str]] = frozenset()
    index: dict | None = None

    def route(self, node_id: str) -> str:
        return urljoin(self.url + "/dependencies/index.json", self.records[node_id]["route"])


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _area_edges(index: dict) -> list[dict]:
    nodes = index["nodes"]
    by_id = {node["id"]: node for node in nodes}

    def area(node: dict) -> object:
        # The nearest area on the record or above it; the root's describes the whole project.
        while (parent := node.get("parent")) is not None:
            if node.get("area"):
                return node["area"]
            node = nodes[parent] if type(parent) is int else by_id[parent]
        return None

    areas = {node["id"]: area(node) for node in nodes}
    return [
        {"source": areas[source], "target": areas[target], "statement": statement, "proof": proof}
        for source, target, statement, proof in _decoded_relations(index)
        if areas[source] and areas[target] and areas[source] != areas[target]
    ]


@pytest.fixture(scope="module")
def workspace_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_WorkspaceSite]:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("integrated-workspace") / "project"
    scaffold_project(project, title="Synthetic mathematical workspace")
    blueprint = project / "blueprint"
    _write(blueprint / "tex-macros.json", '{"RR":"\\\\mathbb{R}"}')
    _write(blueprint / "README.md", "# Synthetic mathematical workspace\n\n- [Mathematical library](roadmap/README.md)\n")
    topics = [f"division-{index:02d}" for index in range(15)] + ["scope:authored", "boundary:authored", "source-notes"]
    links = "\n".join(f"- [Topic {index + 1}](./{topic}/README.md)" for index, topic in enumerate(topics))
    _write(blueprint / "roadmap/README.md", (
        "# " + _LIBRARY + "\n\nAn authored collection of mathematical topics and complete reading pages.\n\n"
        + links + "\n- [Classical source essays](appendix-a/README.md)\n"
        "- [Contemporary source records](literature-b/README.md)\n"
        "- [A topic without declarations](unstarted/README.md)\n"
    ))
    for index, topic in enumerate(topics):
        title = _ANCESTOR if index == 0 else (
            f"Topic {index + 1}: detailed mathematical foundations, examples, identities, and their complete explanations"
        )
        if topic == "source-notes":
            title = "Quantitative geometry, despite the source-looking directory name"
        area = "Geometry" if index < 9 or topic == "source-notes" else "Analysis"
        child = "part-01/README.md" if index == 0 else "main.md"
        _write(blueprint / f"roadmap/{topic}/README.md", (
            f"---\nnavigation: module\narea: {area}\n---\n\n# {title}\n\n"
            "This topic's name and area come from its authored content, not its directory.\n\n"
            f"- [Complete statements]({child})\n" + ("" if index == 0 else "- [Base](base.md)\n")
        ))
        group = f"{topic}/part-01/results" if index == 0 else topic
        if index == 0:
            _write(blueprint / "roadmap/division-00/part-01/README.md", (
                "---\nnavigation: transparent\n---\n\n# A transparent organizational layer\n\n"
                "- [Detailed identities](results/README.md)\n"
            ))
            _write(blueprint / f"roadmap/{group}/README.md", (
                "---\nnavigation: module\n---\n\n# " + _DEEP_TITLE + "\n\n"
                "Every statement below retains its full mathematical source.\n\n"
                "- [Base](base.md)\n- [Complete identity](main.md)\n"
                "- [Ready identity](ready.md)\n- [Independent identity](isolated.md)\n"
            ))
        _write(blueprint / f"roadmap/{group}/base.md", (
            "---\ndeclaration: def\nstatement: formalized\nlean: ReaderFixture.base\n---\n\n"
            "# Base value\n\nThe base value is zero.\n\n## Depends on\n\nNo prerequisites.\n"
        ))
        claim = "statement: formalized\nproof: formalized\nlean: ReaderFixture.zero_identity\n" if index == 0 else (
            "statement: formalized\nproof: formalized\nlean: ReaderFixture.counterpart_identity\n" if index == 9 else ""
        )
        result_title = _TARGET_TITLE if index == 0 else _OTHER_TITLE if index == 9 else f"Identity in topic {index + 1}"
        if index == 0:
            paragraphs = "\n\n".join(
                f"Explanation {n + 1}. The zero identity is stated explicitly, with every notation and reading paragraph retained. "
                "These complete mathematical paragraphs are not a graph preview."
                for n in range(12)
            )
            body = f"The proposition ${_TARGET_TEX[0]}$ holds.\n\n" + paragraphs + "\n\n"
            body += "\n\n".join(f"Notation: ${tex}$." for tex in _TARGET_TEX[1:-1])
            body += "\n\n$$\n" + _TARGET_TEX[-1] + "\n$$\n\n" + _LAST_SENTENCE
            body += "\n\n[Read the counterpart identity](../../../division-09/main.md).\n"
            body += "\n\n[Read the independent identity](isolated.md).\n"
        else:
            body = "The zero identity $0=0$ holds.\n\n" + "Its complete explanation remains available. " * 12
            if index == 9:
                body += "\n\nThe proof uses the complete zero identity from the geometric topic."
        proof_dependencies = (
            "\n\n## Proof depends on\n\n- [Complete zero identity](../division-00/part-01/results/main.md)\n"
            if index == 9 else ""
        )
        _write(blueprint / f"roadmap/{group}/main.md", (
            "---\ndeclaration: theorem\n" + claim + "---\n\n# " + result_title + "\n\n" + body
            + "\n\n## Depends on\n\n- [Base](base.md)\n"
            + proof_dependencies
        ))
        if index == 0:
            _write(blueprint / f"roadmap/{group}/ready.md", (
                "---\ndeclaration: theorem\nstatement: formalized\nlean: ReaderFixture.ready_identity\n---\n\n"
                "# Ready zero identity\n\nThe equality $0=0$ is stated.\n\n## Depends on\n\n- [Base](base.md)\n"
            ))
            _write(blueprint / f"roadmap/{group}/isolated.md", (
                "---\ndeclaration: theorem\nnot_ready: true\n---\n\n# Independent zero identity\n\n"
                "This independent statement retains $0=0$ and $\\frac{0}{2}=0$ in its full reading page.\n\n"
                "[Read the complete zero identity](main.md).\n\n"
                "## Depends on\n\nNo prerequisites.\n"
            ))
    for name, title in zip(("appendix-a", "literature-b"), _SOURCE_TITLES):
        _write(blueprint / f"roadmap/{name}/README.md", (
            f"---\nnavigation: source\n---\n\n# {title}\n\n"
            "This is an authored source track, not a guessed topic.\n\n- [Reading note](note.md)\n"
        ))
        _write(blueprint / f"roadmap/{name}/note.md", "# Reading note\n\nComplete source commentary is retained.\n")
    _write(blueprint / "roadmap/unstarted/README.md", (
        "---\nnavigation: module\narea: Unstarted mathematics\n---\n\n"
        "# A reading topic without declarations\n\nThis authored topic currently has zero mathematical targets.\n\n"
        "- [Reading note](note.md)\n"
    ))
    _write(blueprint / "roadmap/unstarted/note.md", "# Reading note\n\nA narrative is not a declaration to formalize.\n")
    _write(project / "ReaderFixture.lean", (
        "namespace ReaderFixture\ndef base : Nat := 0\n"
        "theorem zero_identity : (0 : Nat) = 0 := rfl\n"
        "theorem counterpart_identity : (0 : Nat) = 0 := zero_identity\n"
        "theorem ready_identity : (0 : Nat) = 0 := rfl\nend ReaderFixture\n"
    ))
    render_site(blueprint, project / "site-src", lean_root=project)
    configuration = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    build(configuration)
    index_path = project / "site/dependencies/index.json"
    index_bytes = index_path.read_bytes()
    index = json.loads(index_bytes)
    records = {record["id"]: record for record in index["nodes"]}
    wiki_layer = json.loads((index_path.parent / "wiki-links.json").read_bytes())
    assert wiki_layer["schema"] == "autoform-wiki-links/v1"
    assert wiki_layer["index_sha256"] == hashlib.sha256(index_bytes).hexdigest()
    assert wiki_layer["availability"] == "complete" and wiki_layer["unavailable_sources"] == []
    identifiers = [record["id"] for record in index["nodes"]]
    wiki_pairs = frozenset((identifiers[source], identifiers[target]) for source, target in wiki_layer["references"])
    assert wiki_pairs == {(_TARGET, _OTHER), (_TARGET, _ISOLATED), (_ISOLATED, _TARGET)}
    root_path = index_path.parent / unquote(urlsplit(records["roadmap"]["data_url"]).path)
    root_graph = json.loads(root_path.read_text(encoding="utf-8"))
    exported = os.environ.get("AUTOFORM_UI_SITE_EXPORT_DIR")
    if exported:
        destination = Path(exported)
        shutil.copytree(project / "site", destination, dirs_exist_ok=True)
        runtime = (destination / "javascripts/blueprint-dag.js").read_bytes()
        fingerprint = re.match(rb"/\* autoform-explorer-source-sha256:([0-9a-f]{64}) \*/", runtime)
        assert fingerprint is not None, "the exported site did not carry its exact compiled source fingerprint"
        provenance = {
            "schema": "autoform-ci-site-provenance/v1",
            "consumer": "rich synthetic mathematical workspace",
            "candidate_sha": os.environ.get("AUTOFORM_CI_SOURCE_SHA"),
            "runtime_bytes": len(runtime),
            "runtime_sha256": hashlib.sha256(runtime).hexdigest(),
            "runtime_source_sha256": fingerprint.group(1).decode("ascii"),
            "root_nodes": len(root_graph["nodes"]),
            "relations": len(index["relations"]),
            "area_edges": _area_edges(index),
            "kernel_checked": False,
        }
        (destination / "ci-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    with _served(project / "site") as url:
        yield _WorkspaceSite(url, records, root_graph, wiki_layer, wiki_pairs, index)


@pytest.fixture(scope="module")
def navigation_overview_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_WorkspaceSite]:
    """An authored five-area overview with navigation, but no mathematical prose to hide."""
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("navigation-only-overview") / "project"
    scaffold_project(project, title="Authored mathematical map")
    blueprint = project / "blueprint"
    areas = ("Algebra", "Analysis", "Discrete mathematics", "Geometry", "Logic")
    topics = [(area, f"topic-{number}-{part}") for number, area in enumerate(areas) for part in range(3)]
    _write(blueprint / "README.md", "# Authored mathematical map\n\n- [Mathematical map](roadmap/README.md)\n")
    _write(blueprint / "roadmap/README.md", "# Mathematical map\n\n## Topics\n\n" + "\n".join(
        f"- [{area} topic {topic[-1]}]({topic}/README.md)" for area, topic in topics
    ) + "\n")
    for number, area in enumerate(areas):
        for part in range(3):
            topic = f"topic-{number}-{part}"
            _write(blueprint / f"roadmap/{topic}/README.md", (
                f"---\nnavigation: module\narea: {area}\n---\n\n# {area} topic {part}\n\n"
                "A complete mathematical statement follows.\n\n- [Proposed identity](result.md)\n"
            ))
            previous = f"topic-{(number - 1) % len(areas)}-0"
            # Different theorem leaves induce a cyclic area aggregation; the
            # authored theorem graph itself must remain an acyclic blueprint.
            prerequisites = f"- [Previous identity](../{previous}/result.md)" if part else "No prerequisites."
            _write(blueprint / f"roadmap/{topic}/result.md", (
                "---\ndeclaration: theorem\nnot_ready: true\n---\n\n# Proposed identity\n\n"
                "The equality $0=0$ is an unverified fixture target, not a claim of a Lean proof.\n\n"
                f"## Depends on\n\n{prerequisites}\n"
            ))
    render_site(blueprint, project / "site-src")
    configuration = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    build(configuration)
    index = json.loads((project / "site/dependencies/index.json").read_text(encoding="utf-8"))
    records = {record["id"]: record for record in index["nodes"]}
    graph = json.loads((project / "site/dependencies.json").read_text(encoding="utf-8"))
    assert len(graph["nodes"]) == 15 and len(_area_edges(index)) == 10
    with _served(project / "site") as url:
        yield _WorkspaceSite(url, records, graph)


_ESCAPED_RESULT = "escapes/main"
_ESCAPED_SHOWN = "Result foo_bar *star* #1 a\\b"


@pytest.fixture(scope="module")
def escaped_titles_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_WorkspaceSite]:
    """A topic and a result whose titles hold Markdown escapes (#727)."""
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("escaped-titles") / "project"
    scaffold_project(project, title="Escaped titles")
    blueprint = project / "blueprint"
    _write(blueprint / "README.md", "# Escaped titles\n\n- [Library](roadmap/README.md)\n")
    _write(blueprint / "roadmap/README.md", "# Library\n\n- [Topic](escapes/README.md)\n")
    _write(blueprint / "roadmap/escapes/README.md", (
        "---\nnavigation: module\n---\n\n# Topic foo\\_bar\n\nThe topic's one result.\n\n- [Result](main.md)\n"
    ))
    _write(blueprint / "roadmap/escapes/main.md", (
        "---\ndeclaration: theorem\nnot_ready: true\n---\n\n# Result foo\\_bar \\*star\\* \\#1 a\\\\b\n\n"
        "The equality $0=0$ holds.\n\n## Depends on\n\nNo prerequisites.\n"
    ))
    render_site(blueprint, project / "site-src")
    configuration = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    configuration["extra_javascript"] = [
        entry for entry in configuration["extra_javascript"]
        if getattr(entry, "path", str(entry)) != MATHJAX_BUNDLE
    ]
    build(configuration)
    index = json.loads((project / "site/dependencies/index.json").read_text(encoding="utf-8"))
    records = {record["id"]: record for record in index["nodes"]}
    with _served(project / "site") as url:
        yield _WorkspaceSite(url, records, {})


@pytest.fixture(scope="module")
def workspace_browser() -> Iterator[object]:
    required = os.environ.get("AUTOFORM_REQUIRE_BROWSER_TESTS") == "1"
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.webkit.launch()
        except playwright.Error as error:
            if required or "Executable doesn't exist" not in str(error):
                raise
            pytest.skip("WebKit is not installed")
        yield browser
        browser.close()


@pytest.fixture()
def workspace_page(workspace_browser: object) -> Iterator[object]:
    context = workspace_browser.new_context(viewport={"width": 1600, "height": 900}, reduced_motion="reduce")
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
def workspace_runtime() -> _PinnedRuntime:
    return _PinnedRuntime()


def _frames(page: object) -> None:
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def _browse_contents(page: object, action: str) -> None:
    """Choose the real statement or source inventory through Browse."""
    toggle = page.get_by_role("button", name=re.compile(r"^Browse statements(?: or source texts)?$"))
    popover = page.locator(".bp-dag-browse-popover")
    playwright.expect(popover).to_be_hidden()
    if toggle.get_attribute("aria-controls") is None:
        assert action in {"All statements", "Statements in this section"}, "A source-free scope must not offer a source choice"
        playwright.expect(toggle).to_have_attribute("aria-label", "Browse statements")
        assert toggle.get_attribute("aria-expanded") is None
        playwright.expect(popover.get_by_role("button", name="Source texts", exact=True)).to_have_count(0)
        toggle.click()
        playwright.expect(page.locator(".bp-dag-inventory")).to_be_visible()
        playwright.expect(popover).to_be_hidden()
        return
    toggle.click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "true")
    playwright.expect(popover).to_be_visible()
    popover.get_by_role("button", name=action, exact=True).click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    playwright.expect(popover).to_be_hidden()


def _peek_reader(page: object, handle: object) -> None:
    """Tap the reader's handle down to its peek: a phone cycles half, full and peek, so two taps at most, then it must be closed."""
    for _ in range(2):
        if handle.get_attribute("aria-expanded") != "true":
            break
        handle.click()
        _frames(page)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")


def _open(
    page: object, site: _WorkspaceSite, runtime: _PinnedRuntime, node_id: str = "roadmap",
    *, wiki_route: Callable[[object], None] | None = None,
) -> None:
    _routes(page, site.url, runtime, graph_client=True)
    if wiki_route is not None:
        # Register after the normal catch-all, retaining the real server's
        # renderer-produced asset for a delayed-response or transport-fault test.
        page.route(site.url + "/dependencies/wiki-links.json", wiki_route)
    page.goto(site.route(node_id))
    page.locator(".bp-dag-integrated .bp-dag-stage").wait_for()
    playwright.expect(page.locator(".bp-reading-body > .bp-reading-source")).to_have_count(1)
    # The placeholder says what search matches; aria-busy clearing says its index has loaded.
    search = page.locator(".bp-dag-search")
    playwright.expect(search).to_have_attribute("placeholder", "Search titles and Lean names")
    playwright.expect(search).not_to_have_attribute("aria-busy", "true")
    page.evaluate("() => window.__workspaceDocument = document")
    _frames(page)


def _selected(page: object, node_id: str) -> object:
    """The book has routed to ``node_id`` and the whole-project map has followed: it selects a result's card, or opens a
    part."""
    playwright.expect(page.locator(".bp-dag-viewer[data-graph-src]")).to_have_attribute("data-map-start", node_id)
    place = page.locator(f'.bp-whole-map .pm-result.pm-selected[data-result="{node_id}"], '
                         f'.bp-whole-map .pm-part.pm-focus[data-part="{node_id}"]')
    playwright.expect(place).to_have_count(1)
    return place


def _map_card(page: object, node_id: str) -> object:
    """``node_id``'s card on the whole map once the map has arrived, zoomed out with the map's own key until it is drawn."""
    playwright.expect(page.locator(".bp-whole-map .pm-result.pm-selected")).to_have_count(1)
    card = page.locator(f'.bp-whole-map .pm-result[data-result="{node_id}"]')
    for _ in range(4):
        if card.count():
            break
        page.locator(".bp-whole-map .pm-stage > svg").press("-")
        _frames(page)
    playwright.expect(card).to_have_count(1)
    return card


def _full_math(page: object, node_id: str = _TARGET, count: int = 5) -> object:
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-reading-body'))") is True
    card = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{node_id}"]')
    playwright.expect(card).to_have_count(1)
    playwright.expect(card.locator(".arithmatex")).to_have_count(count)
    playwright.expect(card.locator("mjx-container")).to_have_count(count)
    assert card.locator("mjx-merror, merror").count() == 0
    return card


def _search(page: object, query: str, node_id: str) -> None:
    search = page.locator(".bp-dag-search")
    search.fill(query)
    playwright.expect(page.locator(f'.bp-dag-result[data-result-id="{node_id}"]')).to_be_visible()
    search.press("Enter")


def _surface(page: object) -> dict:
    return page.evaluate("""() => ({
      statuses: Array.from(document.querySelectorAll('.bp-dag-filter-popover input[value]:checked')).map(input => input.value).sort(),
      reading: document.querySelector('.bp-reading-body').scrollTop,
      outline: document.querySelector('.bp-dag-navigation').scrollTop})""")


def _assert_surface(actual: dict, expected: dict) -> None:
    assert actual["statuses"] == expected["statuses"]
    assert actual["reading"] == pytest.approx(expected["reading"], abs=1)
    assert actual["outline"] == pytest.approx(expected["outline"], abs=1)


def test_wide_workspace_has_three_nonoverlapping_panes_complete_math_and_one_h1(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    card = _full_math(page)
    assert len(card.inner_text()) > 420
    assert _LAST_SENTENCE in card.inner_text()
    playwright.expect(page.get_by_role("heading", level=1)).to_have_count(1)
    playwright.expect(page.locator(".bp-explorer-page > .bp-reading-source")).to_have_count(0)
    nav = page.locator(".bp-dag-navigation").bounding_box()
    graph = page.locator(".bp-dag-stage").bounding_box()
    reading = page.locator(".bp-reading-pane").bounding_box()
    assert nav and graph and reading
    assert nav["x"] + nav["width"] <= graph["x"] + 1
    assert graph["x"] + graph["width"] <= reading["x"] + 1
    assert page.locator(".bp-dag-stage").evaluate("""stage => {
      const rect = stage.getBoundingClientRect(), hit = document.elementFromPoint(rect.x + rect.width/2, rect.y + rect.height/2);
      return stage.contains(hit);
    }""") is True
    viewer = page.locator(".bp-dag-viewer").bounding_box()
    assert viewer and viewer["x"] == pytest.approx(0, abs=1) and viewer["y"] == pytest.approx(0, abs=1)
    assert viewer["width"] == pytest.approx(1600, abs=1) and viewer["height"] == pytest.approx(900, abs=1)
    assert _math_source(page, card.locator("mjx-container").last) == _TARGET_TEX[-1]


@pytest.mark.parametrize("width", [1600, 1000, 390])
def test_empty_authored_overview_opens_on_the_map_and_restores_its_reading_and_camera(
    workspace_page: object, navigation_overview_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int,
) -> None:
    page, site = workspace_page, navigation_overview_site
    page.set_viewport_size({"width": width, "height": 900 if width > 720 else 844})
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime)
    page.locator(".bp-dag-overview-only").wait_for()
    # The same retained control is visible on the collapsible overview, but a
    # wide theorem reader intentionally hides it. Inspect that state by DOM,
    # while keeping native click/visibility assertions on the overview itself.
    handle = page.locator(".bp-reading-pane > .bp-dag-sheet-handle")
    inner = page.locator(".bp-reading-pane .bp-dag-inspector-inner")
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    playwright.expect(inner).to_have_attribute("inert", "")
    playwright.expect(inner).to_have_attribute("aria-hidden", "true")
    assert page.locator(".bp-dag-stage").evaluate("""stage => {
      const box=stage.getBoundingClientRect(); return stage.contains(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2));
    }""") is True
    regions = page.locator('.bp-whole-map .pm-part[data-part^="region:"]')
    if width == 1600:
        playwright.expect(regions).to_have_count(5)
        assert regions.evaluate_all("""nodes => nodes.every(node => {
          const box=node.getBoundingClientRect(),stage=node.closest('.bp-dag-stage').getBoundingClientRect();
          return box.left>=stage.left+12&&box.right<=stage.right-12&&box.top>=stage.top+12&&box.bottom<=stage.bottom-12;
        })""") is True, "The initial five-area desktop overview should not need zooming or panning"
    source = page.locator(".bp-reading-body > .bp-reading-source")
    root_text = source.text_content()
    assert all(f"{area} topic" in root_text for area in ("Algebra", "Analysis", "Discrete mathematics", "Geometry", "Logic"))
    playwright.expect(regions.first).to_be_visible()
    frame = f'.bp-whole-map .pm-part[data-part="{regions.first.get_attribute("data-part")}"] > rect'
    camera = page.locator(frame).evaluate("rect => ['x', 'y', 'width', 'height'].map(name => Number(rect.getAttribute(name)))")
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    assert inner.get_attribute("inert") is None and inner.get_attribute("aria-hidden") is None
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text("Mathematical map")
    playwright.expect(handle).to_be_visible()
    if width <= 720:
        # A phone reader opens at half; the next tap covers the map and the one after returns to the peek, as the title says.
        playwright.expect(handle).to_have_attribute("title", "Expand reading")
        handle.click()
        playwright.expect(page.locator(".bp-dag-viewer")).to_have_class(re.compile(r"\bbp-dag-sheet-full\b"))
    playwright.expect(handle).to_have_attribute("title", "Close reading")
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    _frames(page)
    target = "topic-0-0/result"
    _search(page, target, target)
    page.wait_for_url(site.route(target))
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    _full_math(page, target, 1)
    page.evaluate("() => history.back()")
    page.wait_for_url(site.route("roadmap"))
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    page.wait_for_function("""([selector, expected]) => {
      const rect=document.querySelector(selector);
      return Boolean(rect)&&['x','y','width','height'].every((name,index)=>
        Math.abs(Number(rect.getAttribute(name))-expected[index])<=.5);
    }""", arg=[frame, camera])
    assert source.text_content() == root_text
    assert page.evaluate("() => document === __workspaceDocument") is True
    assert len(documents) == 1


_PANE_WIDTHS = """() => Object.fromEntries([["body", ".bp-dag-body"], ["topics", ".bp-dag-navigation"],
  ["map", ".bp-dag-main"], ["reading", ".bp-reading-pane"]].map(([name, selector]) =>
  [name, document.querySelector(selector).getBoundingClientRect().width]))"""


@pytest.mark.parametrize("width", [1101, 1600])
def test_map_only_gives_an_overview_the_whole_body_and_its_tab_brings_back_the_handle(
    workspace_page: object, navigation_overview_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int,
) -> None:
    """Map only on a collapsed overview left an empty 48px column; the reader comes back as its handle, which takes focus."""
    page, site = workspace_page, navigation_overview_site
    page.set_viewport_size({"width": width, "height": 900})
    _open(page, site, workspace_runtime)
    page.locator(".bp-dag-overview-only").wait_for()
    _frames(page)
    assert page.evaluate(_PANE_WIDTHS)["reading"] == pytest.approx(48, abs=2)
    map_only = page.get_by_role("button", name="Map only")
    map_only.click()
    playwright.expect(map_only).to_have_attribute("aria-pressed", "true")
    _frames(page)
    shut = page.evaluate(_PANE_WIDTHS)
    assert shut["map"] == pytest.approx(shut["body"], abs=2)
    page.get_by_role("button", name="Show the reading pane").click()
    playwright.expect(page.locator(".bp-reading-pane > .bp-dag-sheet-handle")).to_be_focused()
    page.get_by_role("button", name="Show Contents").click()
    _frames(page)
    back = page.evaluate(_PANE_WIDTHS)
    assert back["reading"] == pytest.approx(48, abs=2) and back["topics"] > 100


def test_a_map_shut_beside_a_statement_shows_on_an_overview_and_shuts_again_beside_the_next(
    workspace_page: object, navigation_overview_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    """An overview's collapsed reader is its 48px handle, which needs the map beside it; opened, it shuts the map."""
    page, site = workspace_page, navigation_overview_site
    _open(page, site, workspace_runtime, "topic-0-1/result")
    page.get_by_role("separator", name="Resize the reading pane").focus()
    page.keyboard.press("End")
    _frames(page)
    assert page.evaluate(_PANE_WIDTHS)["map"] == 0
    page.goto(site.route("roadmap"))
    page.locator(".bp-dag-overview-only").wait_for()
    page.locator(".bp-whole-map .pm-app").wait_for()
    _frames(page)
    overview = page.evaluate(_PANE_WIDTHS)
    assert overview["reading"] == pytest.approx(48, abs=2) and overview["map"] > 160
    tab = page.get_by_role("button", name="Show the map")
    playwright.expect(tab).to_be_hidden()
    handle = page.locator(".bp-reading-pane > .bp-dag-sheet-handle")
    handle.click()
    _frames(page)
    assert page.evaluate(_PANE_WIDTHS)["map"] == 0
    playwright.expect(tab).to_be_visible()
    handle.click()
    _frames(page)
    assert page.evaluate(_PANE_WIDTHS)["map"] > 160
    playwright.expect(tab).to_be_hidden()
    # A pick on the map opens the statement with focus left on the map; the map shuts, and its tab takes the focus.
    page.locator('.bp-whole-map svg[tabindex="0"]').focus()
    page.evaluate("""() => document.querySelector('.bp-whole-map').dispatchEvent(new CustomEvent('autoform-map-select',
      {bubbles: true, detail: {id: 'topic-1-0/result', history: 'push', focus: false}}))""")
    playwright.expect(page.locator(".bp-dag-viewer")).not_to_have_class(re.compile(r"\bbp-dag-overview-only\b"))
    playwright.expect(tab).to_be_focused()
    assert page.evaluate(_PANE_WIDTHS)["map"] == 0


def test_the_maps_general_hint_hides_on_a_narrow_map(
    workspace_page: object, navigation_overview_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    """The whole project's hint shows on a wide map and hides at the 160px the book keeps for the map."""
    page, site = workspace_page, navigation_overview_site
    _open(page, site, workspace_runtime)
    page.locator(".bp-dag-overview-only").wait_for()
    page.locator(".bp-reading-pane > .bp-dag-sheet-handle").click()
    _frames(page)
    hint = page.locator(".bp-whole-map .pm-hint")
    playwright.expect(hint).to_be_visible()
    # End shuts the map beside an overview's opened reader, and Right opens it again at its narrowest.
    page.get_by_role("separator", name="Resize the reading pane").focus()
    page.keyboard.press("End")
    _frames(page)
    assert page.evaluate(_PANE_WIDTHS)["map"] == 0
    page.keyboard.press("ArrowRight")
    _frames(page)
    assert page.evaluate(_PANE_WIDTHS)["map"] == pytest.approx(160, abs=2)
    playwright.expect(hint).to_be_hidden()


@pytest.mark.parametrize("width", [1280, 1600, 390])
def test_far_right_genuine_math_menu_and_hover_submenu_stay_inside_the_workspace(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": width, "height": 900 if width > 720 else 844})
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    formula = _full_math(page).locator("mjx-container").last
    playwright.expect(formula).to_have_attribute("display", "true")
    assert page.evaluate("() => MathJax.version") == MATHJAX_VERSION
    formula.scroll_into_view_if_needed()
    _frames(page)
    box = formula.bounding_box()
    assert box and box["width"] > 4

    def scrolls() -> dict:
        return page.evaluate("""() => {
          const reader = document.querySelector('.bp-reading-body');
          return {window:[scrollX,scrollY],body:[document.body.scrollLeft,document.body.scrollTop],
            document:[document.documentElement.scrollLeft,document.documentElement.scrollTop],
            reader:[reader.scrollLeft,reader.scrollTop]};
        }""")

    before = scrolls()
    # Native contextmenu on the existing complete formula block, at its
    # rightmost interior point. No moved DOM, synthetic event or forced click.
    formula.click(button="right", position={"x": box["width"] - 2, "y": box["height"] / 2})
    try:
        _math_menu_evidence(page, f"workspace-{width}-root")
        playwright.expect(page.locator(".CtxtMenu_MenuFrame")).to_have_count(1)
        menus = page.get_by_role("menu")
        root = menus.first.bounding_box()
        assert root and root["x"] >= 0 and root["y"] >= 0
        # Phone display math keeps its natural centered width. Prove this
        # actual right-click would overflow using the real menu's width and
        # the pinned library's 5px anchor offset, not a fixed page inset.
        assert box["x"] + box["width"] - 2 - 5 + root["width"] > width
        assert root["x"] + root["width"] <= width
        assert root["y"] + root["height"] <= page.viewport_size["height"]
        show = page.get_by_role("menuitem", name=re.compile(r"^Show Math As(?:\s*\u25ba)?$"))
        # The right-click can leave the pointer over another item, such as Math Settings, whose wide submenu opens at once and,
        # with no room to its right at 390px, covers this item's middle. Its far end stays clear.
        item = show.bounding_box()
        assert item is not None
        show.hover(position={"x": item["width"] - 6, "y": item["height"] / 2})
        command = page.get_by_role("menuitem", name="TeX Commands", exact=True)
        playwright.expect(command).to_be_visible()
        assert menus.count() >= 2
        _math_menu_evidence(page, f"workspace-{width}-submenu")
        for index in range(menus.count()):
            bounds = menus.nth(index).bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["y"] >= 0
            assert bounds["x"] + bounds["width"] <= width
            assert bounds["y"] + bounds["height"] <= page.viewport_size["height"]
        assert show.evaluate("""item => {
          const box = item.getBoundingClientRect();
          return item.contains(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2));
        }""") is True
        command.click()
        source = page.locator(".CtxtMenu_Info pre")
        playwright.expect(source).to_be_visible()
        assert source.inner_text() == _TARGET_TEX[-1]
        _math_menu_evidence(page, f"workspace-{width}-source")
        page.get_by_role("button", name="Close Dialog Box", exact=True).click()
    except Exception:
        _math_menu_evidence(page, f"workspace-{width}-failed")
        raise
    _frames(page)
    assert scrolls() == before
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
    assert page.url == site.route(_TARGET)
    assert page.evaluate("() => document === __workspaceDocument") is True


@pytest.mark.parametrize("query", [_TARGET_TITLE, "ReaderFixture.zero_identity"])
def test_global_title_and_lean_search_select_and_read_in_one_action_without_document_navigation(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, query: str,
) -> None:
    page, site = workspace_page, workspace_site
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime)
    _search(page, query, _TARGET)
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    playwright.expect(page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{_TARGET}"]')).to_contain_text(_TARGET_TITLE)
    _full_math(page)
    assert documents == [site.route("roadmap")]
    assert page.evaluate("() => document === __workspaceDocument") is True
    assert page.locator(".bp-dag-viewer").count() == 1


def test_native_anchor_boot_after_delayed_index_selects_and_centers_the_real_theorem(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _routes(page, site.url, workspace_runtime, graph_client=True)
    held: list[object] = []
    page.route(site.url + "/dependencies/index.json", lambda route: held.append(route))
    page.goto(site.route(_TARGET), wait_until="domcontentloaded")
    # Until the index arrives, the reading waits in the book's frame, laid out where the book will keep it.
    playwright.expect(page.locator(".bp-book-frame .bp-reading-body > .bp-reading-source")).to_be_visible()
    assert page.evaluate("() => AutoformMath.refresh(document)") is True
    assert held
    for route in held:
        route.continue_()
    _selected(page, _TARGET)
    _full_math(page)
    card = page.locator(f'.bp-whole-map .pm-result.pm-selected[data-result="{_TARGET}"]')
    playwright.expect(card).to_have_count(1)
    _frames(page)
    box, stage = card.bounding_box(), page.locator(".bp-dag-stage").bounding_box()
    assert box and stage
    # The whole map centres an arriving result across the stage, a third of the way down, under its part's pinned title.
    assert abs(box["x"] + box["width"] / 2 - stage["x"] - stage["width"] / 2) < stage["width"] * .15
    assert abs(box["y"] + box["height"] / 2 - stage["y"] - stage["height"] * .3) < stage["height"] * .15


def test_back_and_forward_restore_filter_reading_and_outline_scroll(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    page.locator(".bp-dag-filter-summary").click()
    status = site.records[_READY]["status"]
    assert status != site.records[_TARGET]["status"]
    page.locator(f'.bp-dag-filter-popover input[value="{status}"]').uncheck()
    page.keyboard.press("Escape")
    page.locator(".bp-reading-body").evaluate("reader => reader.scrollTop = 260")
    page.locator(".bp-dag-navigation").evaluate("outline => outline.scrollTop = 320")
    page.locator(".bp-dag-search").fill(_OTHER_TITLE)
    playwright.expect(page.locator(f'.bp-dag-result[data-result-id="{_OTHER}"]')).to_be_visible()
    _frames(page)
    first = _surface(page)
    assert first["reading"] > 0 and first["outline"] > 0
    page.locator(f'.bp-dag-result[data-result-id="{_OTHER}"]').click()
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    _frames(page)
    second = _surface(page)
    page.evaluate("() => history.back()")
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    _frames(page)
    _assert_surface(_surface(page), first)
    page.evaluate("() => history.forward()")
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    _frames(page)
    _assert_surface(_surface(page), second)
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_focused_library_link_survives_1000_to_1280_resize_and_the_map_fills_the_wider_stage(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 1000, "height": 900})
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    link = page.locator(".bp-dag-breadcrumb .bp-dag-book")
    playwright.expect(link).to_have_text("Home")
    playwright.expect(link).to_have_attribute("href", site.url + "/index.html")
    link.focus()
    source = page.locator(".bp-reading-body > .bp-reading-source").text_content()
    map_view = page.locator(".bp-whole-map .pm-stage > svg")
    before = map_view.bounding_box()
    assert before
    page.set_viewport_size({"width": 1280, "height": 900})
    _frames(page)
    playwright.expect(page.locator(".bp-dag-book")).to_have_count(1)
    playwright.expect(link).to_have_attribute("href", site.url + "/index.html")
    playwright.expect(link).to_be_focused()
    link.evaluate("element => window.__focusedWorkspaceBook = element")
    _frames(page)
    assert link.evaluate("element => element === __focusedWorkspaceBook && document.activeElement === element") is True
    # The whole map follows its stage to the new width.
    after, stage = map_view.bounding_box(), page.locator(".bp-dag-stage").bounding_box()
    assert after and stage and after["width"] != before["width"]
    assert abs(after["width"] - stage["width"]) < 1 and abs(after["height"] - stage["height"]) < 1
    assert page.locator(".bp-reading-body > .bp-reading-source").text_content() == source
    assert page.url == site.route(_TARGET) and page.evaluate("document === __workspaceDocument") is True


def test_closed_phone_reader_resized_to_desktop_is_visible_and_interactive(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 390, "height": 844})
    _open(page, site, workspace_runtime, _TARGET)
    handle = page.get_by_role("button", name=re.compile(r", reading$"))
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    # Half, then full, then the peek.
    handle.click()
    handle.click()
    inner = page.locator(".bp-reading-pane .bp-dag-inspector-inner")
    playwright.expect(inner).to_have_attribute("inert", "")
    playwright.expect(inner).to_have_attribute("aria-hidden", "true")
    page.set_viewport_size({"width": 1600, "height": 900})
    _frames(page)
    playwright.expect(page.locator(".bp-reading-pane")).to_be_visible()
    assert inner.get_attribute("inert") is None and inner.get_attribute("aria-hidden") is None
    assert page.locator(".bp-reading-body").evaluate("""reader => {
      const box = reader.getBoundingClientRect(); return reader.contains(document.elementFromPoint(box.x+box.width/2, box.y+box.height/2));
    }""") is True
    _full_math(page)


def test_hiding_selected_status_does_not_hide_or_inert_the_desktop_reader(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    page.locator(".bp-dag-filter-summary").click()
    status = site.records[_TARGET]["status"]
    page.locator(f'.bp-dag-filter-popover input[value="{status}"]').uncheck()
    # The map dims the statuses the filter leaves out: the selection stays, and its card dims.
    playwright.expect(page.locator(".bp-dag-has-selection")).to_have_count(1)
    playwright.expect(_selected(page, _TARGET)).to_have_class(re.compile(r"\bpm-dimmed\b"))
    inner = page.locator(".bp-reading-pane .bp-dag-inspector-inner")
    assert inner.get_attribute("inert") is None and inner.get_attribute("aria-hidden") is None
    playwright.expect(page.locator(".bp-reading-pane")).to_be_visible()
    _full_math(page)


@pytest.mark.parametrize("width", [1600, 1000, 390])
def test_long_ancestor_labels_and_full_reading_heading_stay_within_their_panes(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": width, "height": 900 if width > 720 else 844})
    _open(page, site, workspace_runtime, _TARGET)
    heading = page.locator(".bp-reading-body h1")
    playwright.expect(heading).to_have_text(_DEEP_TITLE)
    assert heading.evaluate("heading => heading.scrollWidth <= heading.clientWidth + 1") is True
    assert page.locator(".bp-dag-head").evaluate("head => head.scrollWidth <= head.clientWidth + 1") is True
    # A phone folds the middle ancestors into a named elision; the parent,
    # current article and canonical Home remain native, visible context.
    crumbs = page.locator(".bp-dag-breadcrumb li")
    playwright.expect(crumbs.nth(0)).to_have_text("Home")
    playwright.expect(crumbs.nth(0).locator("a")).to_have_attribute("href", site.url + "/index.html")
    if width <= 720:
        playwright.expect(crumbs).to_have_count(4)
        hidden_names = _LIBRARY + " / " + _ANCESTOR
        elision = crumbs.nth(1)
        playwright.expect(elision).to_be_visible()
        playwright.expect(elision).to_have_attribute("title", hidden_names)
        assert elision.evaluate("item=>item.firstChild.nodeType===Node.TEXT_NODE&&item.firstChild.textContent==='…'")
        playwright.expect(elision.locator(".bp-dag-sr-only")).to_have_count(1)
        playwright.expect(elision.locator(".bp-dag-sr-only")).to_have_text(hidden_names)
        parent, current = crumbs.nth(2), crumbs.nth(3)
    else:
        playwright.expect(crumbs).to_have_count(5)
        playwright.expect(crumbs.nth(1)).to_have_text(_LIBRARY)
        playwright.expect(crumbs.nth(2)).to_have_attribute("title", _ANCESTOR)
        playwright.expect(crumbs.nth(2)).to_have_text(_ANCESTOR)
        playwright.expect(page.locator(".bp-dag-breadcrumb .bp-dag-sr-only")).to_have_count(0)
        playwright.expect(crumbs.nth(2)).to_be_visible()
        parent, current = crumbs.nth(3), crumbs.nth(4)
        # The trail has the row to itself, rather than a third of it.
        trail_box, head_box = page.locator(".bp-dag-breadcrumb").bounding_box(), page.locator(".bp-dag-head").bounding_box()
        assert trail_box is not None and head_box is not None and trail_box["width"] >= head_box["width"] / 2
    playwright.expect(parent).to_have_text(_DEEP_TITLE)
    playwright.expect(parent).to_be_visible()
    playwright.expect(parent.locator("a")).to_have_attribute("href", site.route(site.records[_TARGET]["scope"]))
    playwright.expect(current).to_have_text(_TARGET_TITLE)
    playwright.expect(current).to_have_attribute("aria-current", "page")
    playwright.expect(current.locator("a")).to_have_count(0)
    if width == 1000:
        # A tablet gives search and Contents the first row and filter, Browse and zoom the second, rather than three rows.
        boxes = page.locator(".bp-dag-toolbar").evaluate(
            "bar => [...bar.children].filter(child => child.getClientRects().length)"
            ".map(child => { const box = child.getBoundingClientRect(); return [box.top, box.bottom]; })"
        )
        first, second = boxes[:2], boxes[2:]
        assert max(top for top, _ in first) < min(bottom for _, bottom in first), boxes
        assert max(top for top, _ in second) < min(bottom for _, bottom in second), boxes
        assert min(top for top, _ in second) >= max(bottom for _, bottom in first), boxes
    if width <= 1100:
        page.get_by_role("button", name="Contents", exact=True).click()
    outline = page.locator(".bp-dag-navigation")
    playwright.expect(outline).to_be_visible()
    ancestor = outline.locator('[data-navigation-node-id="division-00"]')
    playwright.expect(ancestor).to_have_text(_ANCESTOR)
    assert ancestor.get_attribute("title") == _ANCESTOR
    assert ancestor.evaluate("link => link.scrollWidth <= link.clientWidth + 1") is True
    assert "A transparent organizational layer" not in outline.inner_text()


def test_authored_sources_results_and_areas_have_distinct_real_views(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime)
    # The map offers no way back to itself, and Browse marks only the list on screen, so at most one control reads as pressed.
    pressed = page.get_by_role("group", name="Dependency explorer controls", exact=True).locator('[aria-pressed="true"]')
    playwright.expect(pressed).to_have_count(0)
    playwright.expect(page.get_by_role("button", name="Back to map", exact=True)).to_be_hidden()
    # The whole map's regions are the authored areas, not the source tracks; an area of one topic is that topic.
    regions = page.locator('.bp-whole-map .pm-part[data-part^="region:"]')
    playwright.expect(regions).to_have_count(2)
    titles = regions.evaluate_all("nodes => nodes.map(node => node.querySelector('title').textContent)")
    assert sorted(titles) == ["Analysis", "Geometry"]
    playwright.expect(page.locator('.bp-whole-map .pm-part[data-part="unstarted"]')).to_have_count(1)
    _browse_contents(page, "Source texts")
    playwright.expect(page.locator(".bp-dag-inventory-head h2")).to_have_text("Source texts")
    playwright.expect(pressed).to_have_count(1)
    playwright.expect(pressed).to_have_text("Source texts")
    titles = page.locator(".bp-dag-inventory-title").all_text_contents()
    assert sorted(titles) == sorted(_SOURCE_TITLES)
    assert site.records["source-notes"]["title"] not in titles
    _browse_contents(page, "All statements")
    playwright.expect(page.locator(".bp-dag-inventory-head h2")).to_have_text("Statements")
    playwright.expect(pressed).to_have_count(1)
    playwright.expect(pressed).to_have_text("All statements")
    assert _TARGET_TITLE in page.locator(".bp-dag-inventory-title").all_text_contents()
    assert not set(_SOURCE_TITLES).intersection(page.locator(".bp-dag-inventory-title").all_text_contents())
    assert "Reading note" not in page.locator(".bp-dag-inventory-title").all_text_contents()
    page.get_by_role("button", name="Back to map", exact=True).click()
    playwright.expect(pressed).to_have_count(0)
    # One click on Geometry's band picks it where it is and a second opens it; its topics are named from their content.
    band = page.locator('.bp-whole-map .pm-part[data-part="region:Geometry"] > rect')
    band.click(position={"x": 24, "y": 10})
    playwright.expect(page.locator('.bp-whole-map .pm-part.pm-picked[data-part="region:Geometry"]')).to_have_count(1)
    band.click(position={"x": 24, "y": 10})
    notes = page.locator('.bp-whole-map .pm-part[data-part="source-notes"]')
    playwright.expect(notes.locator("title")).to_have_text(site.records["source-notes"]["title"])
    playwright.expect(pressed).to_have_count(0)
    assert page.url == site.route("roadmap")


def test_zero_edge_native_article_has_no_topic_footer_and_its_peers_are_in_the_article(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _ISOLATED)
    _selected(page, _ISOLATED)
    # The reader lists no "More in" pages under the statement: the topic's article holds its other statements.
    card = _full_math(page, _ISOLATED, 2)
    assert "independent statement" in card.inner_text()
    playwright.expect(page.locator(".bp-reading-pane")).not_to_contain_text("More in ")
    assert page.url == site.route(_ISOLATED)
    peer = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{_TARGET}"]').get_by_role(
        "link", name=re.compile(r"^Link to "))
    playwright.expect(peer).to_have_attribute("href", site.route(_TARGET))
    peer.click()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    _full_math(page)
    assert page.evaluate("() => document === __workspaceDocument") is True
    page.go_back()
    page.wait_for_url(site.route(_ISOLATED))
    _selected(page, _ISOLATED)
    _full_math(page, _ISOLATED, 2)
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_real_result_flag_drives_document_title_leaf_breadcrumb_and_result_count(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    assert site.records[_TARGET]["result"] is True
    _open(page, site, workspace_runtime)
    _search(page, _TARGET_TITLE, _TARGET)
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    assert page.title() == site.records[_TARGET]["title"]
    playwright.expect(page.locator(".bp-dag-breadcrumb li").last).to_have_text(_TARGET_TITLE)
    _browse_contents(page, "Statements in this section")
    enabled = set(page.locator(".bp-dag-filter-popover input[value]:checked").evaluate_all("inputs => inputs.map(input => input.value)"))
    expected = sum(record.get("result") is True and record.get("scope") == site.records[_TARGET]["scope"]
                   and record.get("status") in enabled
                   for record in site.records.values())
    assert expected > 0
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text(f"{expected} statements")


def test_an_escaped_title_reads_as_its_heading_in_contents_search_the_breadcrumb_and_the_tab(
    workspace_page: object, escaped_titles_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    """Issue #727: titles with Markdown escapes read as typed in Contents, search, the breadcrumb and, on a result's
    route, the tab, where their headings read `Topic foo_bar` and `Result foo_bar *star* #1 a\\b`. A topic's tab
    already took its heading's text."""
    page, site = workspace_page, escaped_titles_site
    assert site.records[_ESCAPED_RESULT]["title"] == _ESCAPED_SHOWN
    _open(page, site, workspace_runtime, "escapes")
    assert page.title() == "Topic foo_bar"
    playwright.expect(page.locator('.bp-dag-navigation-link[data-navigation-node-id="escapes"]')).to_have_text("Topic foo_bar")
    playwright.expect(page.locator(".bp-dag-breadcrumb li").last).to_have_text("Topic foo_bar")
    search = page.locator(".bp-dag-search")
    search.fill(_ESCAPED_SHOWN)
    hit = page.locator(f'.bp-dag-result[data-result-id="{_ESCAPED_RESULT}"] .bp-dag-result-title')
    playwright.expect(hit).to_have_text(_ESCAPED_SHOWN)
    search.press("Enter")
    page.wait_for_url(site.route(_ESCAPED_RESULT))
    playwright.expect(page.locator(".bp-dag-breadcrumb li").last).to_have_text(_ESCAPED_SHOWN)
    playwright.expect(page).to_have_title(_ESCAPED_SHOWN)


@pytest.mark.parametrize("node_id", ["scope:authored/main", "boundary:authored/main"])
def test_authored_prefix_like_ids_activate_the_exact_published_result(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, node_id: str,
) -> None:
    page, site = workspace_page, workspace_site
    assert site.records[node_id]["result"] is True
    _open(page, site, workspace_runtime)
    _search(page, site.records[node_id]["title"], node_id)
    page.wait_for_url(site.route(node_id))
    _selected(page, node_id)
    playwright.expect(page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{node_id}"]')).to_have_count(1)
    assert page.title() == site.records[node_id]["title"]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_zero_count_authored_region_is_visible_and_does_not_invent_one_item(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    authored = [node for node in site.root_graph["nodes"] if node.get("area") == "Unstarted mathematics"]
    assert authored
    _open(page, site, workspace_runtime)
    # An area of one topic is that topic on the whole map.
    region = page.locator('.bp-whole-map .pm-part[data-part="unstarted"]')
    playwright.expect(region).to_have_attribute("aria-label", re.compile(r", 0 results$"))
    _browse_contents(page, "All statements")
    page.get_by_role("button", name="Back to map", exact=True).click()
    playwright.expect(region).to_be_visible()


def test_isolated_topic_link_opens_connected_result_and_its_real_edge(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _ISOLATED)
    _selected(page, _ISOLATED)
    _full_math(page, _ISOLATED, 2)
    owner = site.records[_ISOLATED]["scope"]
    parent = page.locator(".bp-dag-breadcrumb").get_by_role("link", name=site.records[owner]["title"], exact=True)
    playwright.expect(parent).to_have_attribute("href", site.route(owner))
    assert parent.evaluate("""link => {
      const own = link.getBoundingClientRect(), crumb = link.closest('li').getBoundingClientRect();
      const hit = document.elementFromPoint(own.x + own.width / 2, own.y + own.height / 2);
      return own.width > 0 && own.left >= crumb.left - 1 && own.right <= crumb.right + 1 &&
        (hit === link || link.contains(hit));
    }"""), "A truncated breadcrumb must retain its own unobscured pointer target"
    parent.click()
    page.wait_for_url(site.route(owner))
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_attribute("data-dag-ready", "true")
    playwright.expect(page.locator(".bp-dag-viewer")).not_to_have_attribute("aria-busy", "true")
    # The whole map opens the topic and draws every result in it, the unlinked one included.
    _selected(page, owner)
    for identifier in (_ISOLATED, _BASE, _TARGET):
        playwright.expect(page.locator(f'.bp-whole-map .pm-result[data-result="{identifier}"]')).to_have_count(1)
    page.go_back()
    page.wait_for_url(site.route(_ISOLATED))
    _selected(page, _ISOLATED)
    _full_math(page, _ISOLATED, 2)
    peer = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{_TARGET}"]').get_by_role(
        "link", name=re.compile(r"^Link to "))
    playwright.expect(peer).to_have_attribute("href", site.route(_TARGET))
    peer.click()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    # The whole map draws the statement link between the target and its base, either way round.
    route = page.locator(", ".join(f'.bp-whole-map path[data-key="croute:{a}>{b}"]'
                                   for a, b in ((_BASE, _TARGET), (_TARGET, _BASE))))
    playwright.expect(route).to_have_count(1)
    playwright.expect(route).not_to_have_class(re.compile(r"\bpm-proof\b"))
    page.go_back()
    page.wait_for_url(site.route(_ISOLATED))
    _selected(page, _ISOLATED)
    _full_math(page, _ISOLATED, 2)


def test_a_drag_from_a_map_card_pans_and_keeps_the_reading_and_a_tap_opens_it(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    card = _map_card(page, _BASE)
    url = page.url
    before = card.bounding_box()
    stage = page.locator(".bp-dag-stage").bounding_box()
    assert before and stage
    x, y = before["x"] + before["width"] / 2, before["y"] + before["height"] / 2
    # Drag towards the middle of the map, so the card stays in view for the tap.
    dx = 40 if x < stage["x"] + stage["width"] / 2 else -40
    dy = 30 if y < stage["y"] + stage["height"] / 2 else -30
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y + dy, steps=5)
    page.mouse.up()
    _frames(page)
    after = card.bounding_box()
    assert after
    assert abs(after["x"] - before["x"] - dx) < 1 and abs(after["y"] - before["y"] - dy) < 1
    _selected(page, _TARGET)
    assert page.url == url

    # The whole map takes a move under 4px as a tap.
    page.mouse.move(x + dx, y + dy)
    page.mouse.down()
    page.mouse.move(x + dx + 2, y + dy + 2, steps=2)
    page.mouse.up()
    page.wait_for_url(site.route(_BASE))
    _selected(page, _BASE)


def test_native_reader_and_library_links_remain_soft_navigation(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    home_url = site.url + "/index.html"
    published = page.request.get(home_url, timeout=10_000)
    assert published.ok and published.url == home_url
    tree = html5lib.parse(published.text(), treebuilder="etree", namespaceHTMLElements=False)
    reading = next(element for element in tree.iter() if "bp-reading-source" in element.get("class", "").split())
    # The reading starts with the renderer's boot script (dag_viewer.BOOT_SCRIPT), which removes itself as it runs.
    assert reading[0].tag == "script"
    reading.remove(reading[0])
    heading = next(element for element in reading.iter() if element.tag == "h1")
    home_heading = " ".join("".join(heading.itertext()).split())
    home_text = " ".join("".join(reading.itertext()).split())
    assert home_heading == reading.get("data-page-title") and home_heading != site.records["roadmap"]["title"]
    book_link = next(element for element in reading.iter() if "bp-intro-book" in element.get("class", "").split())
    assert urljoin(home_url, book_link.get("href")) == site.route("roadmap")
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime, _TARGET)
    page.locator(".bp-reading-body").get_by_role("link", name="Read the counterpart identity", exact=True).click()
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    home = page.locator(".bp-dag-book")
    playwright.expect(home).to_have_count(1)
    playwright.expect(home).to_have_text("Home")
    playwright.expect(home).to_have_attribute("href", home_url)
    home.click()
    page.wait_for_url(home_url)
    source = page.locator(".bp-reading-body > .bp-reading-source")
    playwright.expect(source).to_have_count(1)
    playwright.expect(source.locator("h1")).to_have_text(home_heading)
    playwright.expect(source).to_have_text(home_text)
    playwright.expect(source.locator(".bp-intro-book")).to_have_attribute("href", site.route("roadmap"))
    playwright.expect(page.locator('.bp-dag-breadcrumb li[aria-current="page"]')).to_have_text("Home")
    playwright.expect(page.locator(".bp-dag-book")).to_have_count(0)
    page.go_back()
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    playwright.expect(_full_math(page, _OTHER, 1)).to_contain_text(site.records[_OTHER]["title"])
    page.go_back()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    playwright.expect(_full_math(page)).to_contain_text(_LAST_SENTENCE)
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_verified_concept_links_end_the_article_as_see_also_and_linked_from_and_navigate_natively(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    assert site.wiki_layer and site.wiki_layer["availability"] == "complete"
    assert site.wiki_pairs == {(_TARGET, _OTHER), (_TARGET, _ISOLATED), (_ISOLATED, _TARGET)}
    wiki_url = site.url + "/dependencies/wiki-links.json"
    held: list[object] = []
    documents: list[str] = []
    requests: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    page.on("request", lambda request: requests.append(request.url) if request.url == wiki_url else None)
    with page.expect_request(wiki_url) as requested:
        _open(page, site, workspace_runtime, _TARGET, wiki_route=lambda route: held.append(route))
    assert requested.value.url == wiki_url and len(held) == 1
    _selected(page, _TARGET)
    card = _full_math(page)
    playwright.expect(card).to_contain_text(_LAST_SENTENCE)
    links = page.locator(".bp-reading-body .bp-reading-wiki-links")
    # Until the real response is released, nothing is drawn: no empty lists and no counts.
    playwright.expect(links).to_have_count(0)
    with page.expect_response(wiki_url) as delivered:
        held[0].continue_()
    assert delivered.value.status == 200 and delivered.value.json() == site.wiki_layer
    playwright.expect(links).to_have_count(1)
    # They end the article, open, and scroll with it: a heading and a list for each direction. The article is the
    # statement's card, on a page that holds other cards too, so they end the card, headed a level below its h4.
    playwright.expect(links.locator("h5")).to_have_text(["See also", "Linked from"])
    assert links.evaluate("section => section === section.parentElement.lastElementChild")
    assert links.evaluate("section => section.parentElement.matches('.bp-thmwrapper') && section.parentElement.dataset.autoformNodeId") == _TARGET
    outgoing, incoming = links.locator("ul").nth(0), links.locator("ul").nth(1)
    playwright.expect(outgoing.get_by_role("link")).to_have_count(2)
    assert {link.get_attribute("href") for link in outgoing.get_by_role("link").all()} == {
        site.route(_OTHER), site.route(_ISOLATED),
    }
    playwright.expect(incoming.get_by_role("link")).to_have_count(1)
    playwright.expect(incoming.get_by_role("link", name=site.records[_ISOLATED]["title"], exact=True)).to_have_attribute(
        "href", site.route(_ISOLATED),
    )
    playwright.expect(links.locator(".bp-reading-wiki-notice")).to_have_count(0)
    outgoing.get_by_role("link", name=site.records[_OTHER]["title"], exact=True).click()
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    _full_math(page, _OTHER, 1)
    # That page links nowhere, so it lists only what links to it.
    playwright.expect(links.locator("h5")).to_have_text(["Linked from"])
    playwright.expect(links.get_by_role("link", name=_TARGET_TITLE, exact=True)).to_have_attribute("href", site.route(_TARGET))
    page.go_back()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    playwright.expect(_full_math(page)).to_contain_text(_LAST_SENTENCE)
    # This real definition has formal dependents, but no authored concept links, so nothing ends its article.
    assert not any(_BASE in pair for pair in site.wiki_pairs)
    base = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{_BASE}"]')
    # The permalink shows "#" and is named for the statement it links to.
    permalink = base.get_by_role("link", name=re.compile(r"^Link to "))
    playwright.expect(permalink).to_have_attribute("href", site.route(_BASE))
    permalink.click()
    page.wait_for_url(site.route(_BASE))
    _selected(page, _BASE)
    playwright.expect(_full_math(page, _BASE, 0)).to_contain_text("The base value is zero.")
    playwright.expect(links).to_have_count(0)
    playwright.expect(page.locator(".bp-reading-wiki-notice")).to_have_count(0)
    page.go_back()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    _full_math(page)
    assert requests == [wiki_url]
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_failed_optional_concept_transport_shows_no_links_and_keeps_math_and_navigation(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    assert site.wiki_layer and site.wiki_pairs
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    # Fail only the optional HTTP request; do not substitute graph JSON or a
    # fabricated sidecar, change the actual reader, or bypass digest validation.
    _open(page, site, workspace_runtime, _TARGET, wiki_route=lambda route: route.abort())
    _selected(page, _TARGET)
    card = _full_math(page)
    playwright.expect(card).to_contain_text(_LAST_SENTENCE)
    playwright.expect(page.locator(".bp-reading-wiki-links")).to_have_count(0)
    playwright.expect(page.locator(".bp-dag-error")).to_be_hidden()
    card.get_by_role("link", name="Read the counterpart identity", exact=True).click()
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    _full_math(page, _OTHER, 1)
    page.go_back()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    playwright.expect(_full_math(page)).to_contain_text(_LAST_SENTENCE)
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_graph_first_browse_is_contextual_keyboard_closable_and_returns_to_the_same_scope_map(
    workspace_page: object, navigation_overview_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, navigation_overview_site
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime)
    # The redesign's handle names the page it opens, so its accessible name ends ", reading".
    handle = page.get_by_role("button", name=re.compile(r", reading$"))
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    stage = page.locator(".bp-dag-stage")
    playwright.expect(stage).to_be_visible()
    toggle = page.get_by_role("button", name="Browse statements", exact=True)
    popover = page.locator(".bp-dag-browse-popover")
    assert not any(record.get("navigation") == "source" for record in site.records.values())
    assert toggle.get_attribute("aria-controls") is None and toggle.get_attribute("aria-expanded") is None
    playwright.expect(popover).to_be_hidden()
    playwright.expect(popover.get_by_role("button", name="Source texts", exact=True)).to_have_count(0)
    toggle.focus()
    toggle.press("Enter")
    playwright.expect(page.locator(".bp-dag-inventory")).to_be_visible()
    playwright.expect(popover).to_be_hidden()
    assert page.url == site.route("roadmap")
    root_results = {record["id"] for record in site.records.values() if record.get("result") is True}
    rows = page.locator(".bp-dag-inventory-row")
    assert set(rows.evaluate_all("nodes => nodes.map(node => node.dataset.inventoryNodeId)")) == root_results
    page.get_by_role("button", name="Back to map", exact=True).click()
    playwright.expect(stage).to_be_visible()
    scope = next(record for record in site.records.values()
                 if record.get("scope") == record["id"] and record.get("area") == "Geometry")
    _search(page, scope["title"], scope["id"])
    page.wait_for_url(site.route(scope["id"]))
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(scope["title"])
    playwright.expect(toggle).to_have_attribute("aria-label", "Browse statements")
    assert toggle.get_attribute("aria-controls") is None and toggle.get_attribute("aria-expanded") is None
    toggle.click()
    playwright.expect(popover).to_be_hidden()
    playwright.expect(stage).to_be_hidden()
    expected = {record["id"] for record in site.records.values()
                if record.get("result") is True and record.get("scope") == scope["id"]}
    assert len(expected) == 1
    rows = page.locator(".bp-dag-inventory-row")
    playwright.expect(rows).to_have_count(1)
    assert set(rows.evaluate_all("nodes => nodes.map(node => node.dataset.inventoryNodeId)")) == expected
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("1 statement")
    assert page.url == site.route(scope["id"])
    page.get_by_role("button", name="Back to map", exact=True).click()
    playwright.expect(stage).to_be_visible()
    playwright.expect(page.locator(".bp-dag-inventory")).to_be_hidden()
    _selected(page, scope["id"])
    playwright.expect(page.locator(f'.bp-whole-map .pm-result[data-result="{next(iter(expected))}"]')).to_have_count(1)
    playwright.expect(page.locator(".bp-dag-breadcrumb li").last).to_have_text(scope["title"])
    assert page.url == site.route(scope["id"]), "Returning to the map must not widen this topic to the library"
    assert documents == [site.route("roadmap")]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_actual_source_browse_menu_keeps_native_keyboard_close_and_root_navigation(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime)
    assert {record["title"] for record in site.records.values() if record.get("navigation") == "source"} == set(_SOURCE_TITLES)
    toggle = page.get_by_role("button", name="Browse statements or source texts", exact=True)
    popover = page.locator(".bp-dag-browse-popover")
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.focus()
    toggle.press("Enter")
    playwright.expect(toggle).to_have_attribute("aria-expanded", "true")
    playwright.expect(popover).to_be_visible()
    root_action = popover.get_by_role("button", name="All statements", exact=True)
    playwright.expect(root_action).to_be_visible()
    playwright.expect(popover.get_by_role("button", name="Source texts", exact=True)).to_be_visible()
    playwright.expect(popover.get_by_role("button", name="Statements in this section", exact=True)).to_have_count(0)
    root_action.focus()
    root_action.press("Escape")
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    playwright.expect(popover).to_be_hidden()
    playwright.expect(toggle).to_be_focused()
    toggle.click()
    playwright.expect(popover).to_be_visible()
    page.locator(".bp-dag-breadcrumb li").last.click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    playwright.expect(popover).to_be_hidden()
    assert page.url == site.route("roadmap")
    assert documents == [site.route("roadmap")]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_delayed_math_readiness_cannot_restore_an_older_native_section_after_back(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _routes(page, site.url, workspace_runtime, graph_client=True)
    held: list[object] = []
    page.route(MATHJAX_BUNDLE, lambda route: held.append(route))
    page.goto(site.route(_TARGET), wait_until="domcontentloaded")
    _selected(page, _TARGET)
    page.evaluate("() => window.__workspaceDocument = document")
    body = page.locator(".bp-reading-body")
    body.get_by_role("link", name="Read the independent identity", exact=True).click()
    page.wait_for_url(site.route(_ISOLATED), wait_until="domcontentloaded")
    _selected(page, _ISOLATED)
    body.get_by_role("link", name="Read the complete zero identity", exact=True).click()
    page.wait_for_url(site.route(_TARGET), wait_until="domcontentloaded")
    _selected(page, _TARGET)
    page.evaluate("() => history.back()")
    page.wait_for_url(site.route(_ISOLATED), wait_until="domcontentloaded")
    _selected(page, _ISOLATED)
    assert held
    for route in held:
        workspace_runtime.serve(route)
    card = _full_math(page, _ISOLATED, 2)
    _frames(page)
    assert page.url == site.route(_ISOLATED)
    current = body.locator(".bp-reading-current")
    playwright.expect(current).to_have_count(1)
    playwright.expect(current).to_have_attribute("data-autoform-node-id", _ISOLATED)
    assert body.evaluate("reader => reader.contains(document.activeElement)") is True
    assert _math_source(page, card.locator("mjx-container").last) == r"\frac{0}{2}=0"
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_a_scroll_in_the_reader_holds_while_the_page_with_a_fragment_is_still_loading(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _routes(page, site.url, workspace_runtime, graph_client=True)
    held: list[object] = []
    page.route(MATHJAX_BUNDLE, lambda route: held.append(route))
    page.goto(site.route(_TARGET), wait_until="domcontentloaded")
    _selected(page, _TARGET)
    assert page.evaluate("() => document.readyState") != "complete"
    # Until the page loads, WebKit keeps the element its fragment names in view. The reader holds a copy of it instead.
    fragment = urlsplit(site.route(_TARGET)).fragment
    reader = page.locator(".bp-reading-body")
    assert reader.evaluate("(reader, id) => reader.contains(document.getElementById(id))", fragment) is True
    end = reader.evaluate("reader => { reader.scrollTop = reader.scrollHeight; return reader.scrollTop; }")
    assert end > 0
    page.evaluate("""() => {
      const probe = document.body.appendChild(document.createElement('div'));
      probe.style.height = '1px'; void document.body.offsetHeight; probe.remove(); void document.body.offsetHeight;
    }""")
    _frames(page)
    assert reader.evaluate("reader => reader.scrollTop") == end
    assert held
    for route in held:
        workspace_runtime.serve(route)


@pytest.mark.parametrize(("width", "height"), [(1600, 900), (1000, 900), (390, 844)])
def test_rich_workspace_region_and_full_reader_screenshots(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int, height: int,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": width, "height": height})
    _open(page, site, workspace_runtime)
    # The authored areas on the whole map; the area of one topic is that topic.
    for part in ("region:Geometry", "region:Analysis", "unstarted"):
        playwright.expect(page.locator(f'.bp-whole-map .pm-part[data-part="{part}"]')).to_be_visible()
    if width <= 720:
        _peek_reader(page, page.get_by_role("button", name=re.compile(r", reading$")))
    page.evaluate("() => document.fonts.ready")
    output = os.environ.get("AUTOFORM_UI_SCREENSHOT_DIR")
    directory = Path(output) if output else None
    if directory:
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"regions-{width}x{height}.png"))
    _search(page, _TARGET_TITLE, _TARGET)
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    _full_math(page)
    _frames(page)
    playwright.expect(page.get_by_role("heading", level=1)).to_have_count(1)
    if directory:
        page.screenshot(path=str(directory / f"reader-{width}x{height}.png"))
        report = {"viewport": [width, height], "route": page.url, "nodes": len(site.records), "areas": list(_AREAS),
                  "root_nodes": len(site.root_graph["nodes"]), "relations": len(site.index["relations"]),
                  "area_edges": _area_edges(site.index),
                  "mathjax": page.evaluate("() => MathJax.version"), "complete_math": 5,
                  "map_parts": page.locator(".bp-whole-map .pm-part").count()}
        (directory / f"workspace-{width}x{height}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert page.evaluate("() => document === __workspaceDocument") is True


_DECLUTTER_END = "The complete mathematical exposition and its native section anchor are retained."


@pytest.fixture(scope="module")
def reader_declutter_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_WorkspaceSite]:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("reader-declutter") / "project"
    scaffold_project(project, title="Independent mathematical reader")
    blueprint = project / "blueprint"
    _write(blueprint / "README.md", "# Independent mathematical reader\n\n- [Mathematics](roadmap/README.md)\n")
    exposition = "\n\n".join(
        f"Paragraph {number + 1}. This is complete mathematical exposition, not a graph summary or a navigation index."
        for number in range(8)
    )
    _write(blueprint / "roadmap/README.md", (
        "# Independent mathematical reader\n\nThe scalar equality $0=0$ remains readable.\n\n"
        "## Topic catalogue\n\n- [First mathematical topic](alpha/README.md)\n"
        "- [Second mathematical topic](beta/README.md)\n\n"
        "## Full exposition\n\n" + exposition + "\n\nThe equality $\\frac{0}{2}=0$ is retained.\n\n"
        "## Sources\n\n- [Second mathematical topic](beta/README.md)\n\n"
        "## Bibliography\n\n- [First mathematical topic](alpha/README.md)\n\n"
        "## Commentary links\n\n- [First mathematical topic](alpha/README.md)\n"
        "- This mathematical commentary is not a navigation-only link.\n\n"
        "[Read retained exposition](#retained-section).\n\n"
        "## Retained section\n\n" + _DECLUTTER_END + "\n"
    ))
    _write(blueprint / "roadmap/alpha/README.md", (
        "---\nnavigation: module\n---\n\n# First mathematical topic\n\n"
        "The complete reading page includes its authored theorem.\n\n"
        "## Reading contents\n\nRead the [Complete identity](identity.md).\n"
    ))
    _write(blueprint / "roadmap/alpha/identity.md", (
        "---\ndeclaration: theorem\nnot_ready: true\n---\n\n# Complete identity\n\n"
        "The complete proposed statement is $0=0$. This is a planned fixture target, not a verified Lean proof.\n\n"
        + exposition + "\n\n## Depends on\n\nNo prerequisites.\n"
    ))
    _write(blueprint / "roadmap/beta/README.md", (
        "---\nnavigation: module\n---\n\n# Second mathematical topic\n\n"
        "A complete reading note is retained. Read the [topic note](note.md).\n"
    ))
    _write(blueprint / "roadmap/beta/note.md", (
        "# Topic note\n\nA narrative reading note, not a formalization target or a claimed Lean proof.\n"
    ))
    render_site(blueprint, project / "site-src", lean_root=project)
    configuration = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    build(configuration)
    index_path = project / "site/dependencies/index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    records = {record["id"]: record for record in index["nodes"]}
    for node_id in ("alpha", "beta"):
        assert records[node_id]["kind"] == "scope" and records[node_id]["scope"] == node_id
        assert type(records[node_id]["parent"]) is int
        assert index["nodes"][records[node_id]["parent"]]["id"] == "roadmap"
    published_root = index_path.parent / unquote(urlsplit(records["roadmap"]["route"]).path)
    tree = html5lib.parse(published_root.read_text(encoding="utf-8"), treebuilder="etree", namespaceHTMLElements=False)
    reading = next(element for element in tree.iter() if "bp-reading-source" in element.get("class", "").split())
    assert not any("bp-as-typed" in element.get("class", "").split() for element in reading.iter())
    assert {"topic-catalogue", "sources", "bibliography", "commentary-links", "retained-section"} <= {
        element.get("id") for element in reading.iter() if element.tag == "h2"
    }
    assert sum("arithmatex" in element.get("class", "").split() for element in reading.iter()) == 2
    root_path = index_path.parent / unquote(urlsplit(records["roadmap"]["data_url"]).path)
    root_graph = json.loads(root_path.read_text(encoding="utf-8"))
    with _served(project / "site") as url:
        yield _WorkspaceSite(url, records, root_graph)


def test_enhanced_reader_hides_only_duplicate_scope_index_and_keeps_full_ssr_sources_and_math(
    workspace_page: object, workspace_browser: object, reader_declutter_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, reader_declutter_site
    _open(page, site, workspace_runtime)
    reader = page.locator(".bp-reading-body > .bp-reading-source")
    playwright.expect(reader.locator("#topic-catalogue")).to_have_count(1)  # Check existence before hidden accepts absence.
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_have_count(1)
    playwright.expect(reader.locator("#topic-catalogue")).to_be_hidden()
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_be_hidden()
    for node_id in ("alpha", "beta"):
        playwright.expect(page.locator(f'.bp-dag-navigation [data-navigation-node-id="{node_id}"]')).to_be_visible()
    playwright.expect(reader.locator("#sources")).to_be_visible()
    playwright.expect(reader.locator("#sources + ul")).to_be_visible()
    playwright.expect(reader.locator("#bibliography")).to_be_visible()
    playwright.expect(reader.locator("#bibliography + ul")).to_be_visible()
    playwright.expect(reader.locator("#commentary-links + ul")).to_be_visible()
    assert _DECLUTTER_END in reader.inner_text() and len(reader.inner_text()) > 420
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-reading-body'))") is True
    playwright.expect(reader.locator(".arithmatex")).to_have_count(2)
    playwright.expect(reader.locator("mjx-container")).to_have_count(2)
    assert reader.locator("mjx-merror,merror").count() == 0
    playwright.expect(reader.locator("h1")).to_have_count(1)
    context = workspace_browser.new_context(viewport={"width": 1600, "height": 900}, java_script_enabled=False)
    try:
        native = context.new_page()
        _routes(native, site.url, workspace_runtime, graph_client=True)
        native.goto(site.route("roadmap"))
        source = native.locator(".bp-reading-source")
        playwright.expect(source.locator("#topic-catalogue")).to_be_visible()
        playwright.expect(source.locator("#topic-catalogue + ul a")).to_have_count(2)
        playwright.expect(source.locator("#topic-catalogue + ul")).to_be_visible()
        assert _DECLUTTER_END in source.inner_text()
        playwright.expect(source.locator(".arithmatex")).to_have_count(2)
        playwright.expect(source.locator("mjx-container")).to_have_count(0)
    finally:
        context.close()


def test_decluttered_reader_keeps_native_math_anchors_leaf_navigation_and_back_projection(
    workspace_page: object, reader_declutter_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, reader_declutter_site
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime)
    reader = page.locator(".bp-reading-body")
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_have_count(1)
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_be_hidden()
    reader.get_by_role("link", name="Read retained exposition", exact=True).click()
    page.wait_for_url(site.route("roadmap") + "#retained-section")
    playwright.expect(reader.locator("#retained-section")).to_be_visible()
    assert _DECLUTTER_END in reader.inner_text()
    page.locator('.bp-dag-navigation [data-navigation-node-id="alpha"]').click()
    page.wait_for_url(site.route("alpha"))
    playwright.expect(reader.get_by_role("link", name="Complete identity", exact=True)).to_be_visible()
    reader.get_by_role("link", name="Complete identity", exact=True).click()
    page.wait_for_url(site.route("alpha/identity"))
    _selected(page, "alpha/identity")
    card = _full_math(page, "alpha/identity", 1)
    assert "complete proposed statement" in card.inner_text() and len(card.inner_text()) > 420
    # The card's anchor is on its heading, which site search makes a record of.
    assert card.locator(".bp-thmheading").get_attribute("id")
    page.evaluate("() => history.back()")
    page.wait_for_url(site.route("alpha"))
    playwright.expect(reader.locator("h1")).to_have_text(site.records["alpha"]["title"])
    page.evaluate("() => history.back()")
    page.wait_for_url(site.route("roadmap") + "#retained-section")
    # popstate changes the URL before the bounded asynchronous reader commit.
    # Missing catalogue nodes in the outgoing chapter must not satisfy hidden.
    playwright.expect(reader.locator("#retained-section")).to_be_visible()
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_have_count(1)
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_be_hidden()
    assert _DECLUTTER_END in reader.inner_text()
    assert documents == [site.route("roadmap")]
    assert page.evaluate("() => document === __workspaceDocument") is True
