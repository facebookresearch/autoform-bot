"""Actual book/map workspace journeys, with no graph-client suppression.

The public synthetic consumer has eighteen authored topics, two authored areas
and two explicit source tracks. Its complete statements are longer than graph
previews. Lean fixture declarations are simple zero identities; these tests
exercise publication and UI behavior, not kernel verification.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import pytest

from autoform_cli.mathjax import MATHJAX_BUNDLE
from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_integrated_explorer_browser import _PinnedRuntime, _math_source, _routes, _served, playwright


_LIBRARY = "Mathematical library"
_ANCESTOR = "Geometric configurations, foundational notation, and a deliberately long authored ancestor label"
_DEEP_TITLE = "Detailed identities, their complete explanations, and the surrounding elementary statements"
_TARGET = "division-00/part-01/results/main"
_OTHER = "division-09/main"
_ISOLATED = "division-00/part-01/results/isolated"
_READY = "division-00/part-01/results/ready"
_TARGET_TITLE = "Complete zero identity with full mathematical notation"
_OTHER_TITLE = "Counterpart zero identity"
_SOURCE_TITLES = ("Classical source essays", "Contemporary source records")
_TARGET_TEX = (r"0=0", r"\RR\ni0", r"\frac{0}{2}=0", r"\overline{0}=0", r"\sum_{i=1}^{n}0=0")
_LAST_SENTENCE = "This final mathematical sentence is retained in the complete reading page."


@dataclass(frozen=True)
class _WorkspaceSite:
    url: str
    records: dict[str, dict]

    def route(self, node_id: str) -> str:
        return urljoin(self.url + "/dependencies/index.json", self.records[node_id]["route"])


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture(scope="module")
def workspace_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_WorkspaceSite]:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("integrated-workspace") / "project"
    scaffold_project(project, title="Synthetic mathematical workspace")
    blueprint = project / "blueprint"
    _write(blueprint / "tex-macros.json", '{"RR":"\\\\mathbb{R}"}')
    _write(blueprint / "README.md", "# Synthetic mathematical workspace\n\n- [Mathematical library](roadmap/README.md)\n")
    topics = [f"division-{index:02d}" for index in range(17)] + ["source-notes"]
    links = "\n".join(f"- [Topic {index + 1}]({topic}/README.md)" for index, topic in enumerate(topics))
    _write(blueprint / "roadmap/README.md", (
        "# " + _LIBRARY + "\n\nAn authored collection of mathematical topics and complete reading pages.\n\n"
        + links + "\n- [Classical source essays](appendix-a/README.md)\n"
        "- [Contemporary source records](literature-b/README.md)\n"
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
        _write(blueprint / f"roadmap/{group}/main.md", (
            "---\ndeclaration: theorem\n" + claim + "---\n\n# " + result_title + "\n\n" + body
            + "\n\n## Depends on\n\n- [Base](base.md)\n"
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
    _write(project / "ReaderFixture.lean", (
        "namespace ReaderFixture\ndef base : Nat := 0\n"
        "theorem zero_identity : (0 : Nat) = 0 := rfl\n"
        "theorem counterpart_identity : (0 : Nat) = 0 := rfl\n"
        "theorem ready_identity : (0 : Nat) = 0 := rfl\nend ReaderFixture\n"
    ))
    render_site(blueprint, project / "site-src", lean_root=project)
    configuration = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    configuration["extra_javascript"] = [
        entry for entry in configuration["extra_javascript"]
        if getattr(entry, "path", str(entry)) != MATHJAX_BUNDLE
    ]
    build(configuration)
    index = json.loads((project / "site/dependencies/index.json").read_text(encoding="utf-8"))
    with _served(project / "site") as url:
        yield _WorkspaceSite(url, {record["id"]: record for record in index["nodes"]})


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


def _open(page: object, site: _WorkspaceSite, runtime: _PinnedRuntime, node_id: str = "roadmap") -> None:
    _routes(page, site.url, runtime, graph_client=True)
    page.goto(site.route(node_id))
    page.locator(".bp-dag-integrated .bp-dag-stage").wait_for()
    playwright.expect(page.locator(".bp-reading-body > .bp-reading-source")).to_have_count(1)
    playwright.expect(page.locator(".bp-dag-search")).to_have_attribute(
        "placeholder", f"Search {len(site.records):,} roadmap entries",
    )
    page.evaluate("() => window.__workspaceDocument = document")
    _frames(page)


def _selected(page: object, node_id: str) -> object:
    control = page.locator(f'.bp-dag-node[data-autoform-node-id="{node_id}"][data-selected="true"]')
    playwright.expect(control).to_be_visible()
    return control


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


def _pixels(page: object) -> dict:
    return page.locator(".bp-dag-canvas").evaluate("""canvas => {
      const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      let painted = 0; for (let n = 3; n < pixels.length; n += 4) if (pixels[n]) painted++;
      return {width: canvas.width, height: canvas.height, painted};
    }""")


def _surface(page: object) -> dict:
    return page.evaluate("""() => {
      const selected = document.querySelector('.bp-dag-node[data-selected=true]');
      const matrix = selected && new DOMMatrix(getComputedStyle(selected).transform);
      return {camera: matrix && [matrix.a, matrix.d, matrix.e, matrix.f],
        statuses: Array.from(document.querySelectorAll('.bp-dag-filter-popover input[value]:checked')).map(input => input.value).sort(),
        reading: document.querySelector('.bp-reading-body').scrollTop,
        outline: document.querySelector('.bp-dag-navigation').scrollTop};
    }""")


def _assert_surface(actual: dict, expected: dict) -> None:
    assert actual["statuses"] == expected["statuses"]
    assert actual["reading"] == pytest.approx(expected["reading"], abs=1)
    assert actual["outline"] == pytest.approx(expected["outline"], abs=1)
    assert actual["camera"] == pytest.approx(expected["camera"], abs=.5)


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
    assert _pixels(page)["painted"] > 0
    assert _math_source(page, card.locator("mjx-container").last) == _TARGET_TEX[-1]


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
    playwright.expect(page.locator(".bp-explorer-page > .bp-reading-source")).to_be_visible()
    assert page.evaluate("() => AutoformMath.refresh(document)") is True
    assert held
    for route in held:
        route.continue_()
    node = _selected(page, _TARGET)
    _full_math(page)
    _frames(page)
    box, stage = node.bounding_box(), page.locator(".bp-dag-stage").bounding_box()
    assert box and stage
    assert abs(box["x"] + box["width"] / 2 - stage["x"] - stage["width"] / 2) < stage["width"] * .15
    assert abs(box["y"] + box["height"] / 2 - stage["y"] - stage["height"] / 2) < stage["height"] * .15


def test_back_and_forward_restore_filter_camera_reading_and_outline_scroll(
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
    stage = page.locator(".bp-dag-stage").bounding_box()
    assert stage
    page.mouse.move(stage["x"] + stage["width"] / 2, stage["y"] + stage["height"] / 2)
    page.mouse.wheel(57, 39)
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


def test_focused_library_link_survives_1000_to_1280_resize_and_canvas_repaints(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 1000, "height": 900})
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    link = page.locator(".bp-dag-book")
    link.focus()
    link.evaluate("element => window.__focusedWorkspaceBook = element")
    before = _pixels(page)
    assert before["painted"] > 0
    page.set_viewport_size({"width": 1280, "height": 900})
    _frames(page)
    assert page.evaluate("() => document.querySelector('.bp-dag-book') === __focusedWorkspaceBook && document.activeElement === __focusedWorkspaceBook") is True
    after = _pixels(page)
    assert (after["width"], after["height"]) != (before["width"], before["height"])
    assert after["painted"] > 0, "the resized canvas lost its painted graph"


def test_closed_phone_reader_resized_to_desktop_is_visible_and_interactive(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 390, "height": 844})
    _open(page, site, workspace_runtime, _TARGET)
    handle = page.get_by_role("button", name="Toggle mathematical reading", exact=True)
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
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
    playwright.expect(page.locator('.bp-dag-node[data-selected="true"]')).to_have_count(0)
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
    if width <= 1100:
        page.get_by_role("button", name="Toggle topic outline", exact=True).click()
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
    regions = page.locator('.bp-dag-node[data-autoform-node-id^="region:"]')
    playwright.expect(regions).to_have_count(2)
    assert set(regions.locator(".bp-dag-node-title").all_text_contents()) == {"Geometry", "Analysis"}
    assert page.locator('.bp-dag-node[data-autoform-node-id="scope:appendix-a"]').count() == 0
    page.get_by_role("button", name="Sources", exact=True).click()
    playwright.expect(page.locator(".bp-dag-inventory-head h2")).to_have_text("Source tracks")
    titles = page.locator(".bp-dag-inventory-title").all_text_contents()
    assert sorted(titles) == sorted(_SOURCE_TITLES)
    assert site.records["source-notes"]["title"] not in titles
    page.get_by_role("button", name="Results", exact=True).click()
    playwright.expect(page.locator(".bp-dag-inventory-head h2")).to_have_text("Results")
    assert _TARGET_TITLE in page.locator(".bp-dag-inventory-title").all_text_contents()
    assert not set(_SOURCE_TITLES).intersection(page.locator(".bp-dag-inventory-title").all_text_contents())
    page.get_by_role("button", name="Regions", exact=True).click()
    playwright.expect(regions).to_have_count(2)
    page.locator('.bp-dag-node[data-autoform-node-id="region:Geometry"]').click()
    playwright.expect(page.locator('.bp-dag-node[data-autoform-node-id="scope:source-notes"]')).to_be_visible()
    assert page.url == site.route("roadmap")


def test_zero_edge_native_article_keeps_focal_node_surrounding_context_and_complete_math(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _ISOLATED)
    node = _selected(page, _ISOLATED)
    assert "no dependency edges" in node.get_attribute("aria-label")
    assert page.locator(".bp-dag-node").count() > 1
    card = _full_math(page, _ISOLATED, 2)
    assert "independent statement" in card.inner_text()
    assert page.url == site.route(_ISOLATED)
    assert page.locator(".bp-dag-empty").is_hidden()


def test_native_reader_and_library_links_remain_soft_navigation(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime, _TARGET)
    page.locator(".bp-reading-body").get_by_role("link", name="Read the counterpart identity", exact=True).click()
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    page.locator(".bp-dag-book").click()
    page.wait_for_url(site.route("roadmap"))
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(_LIBRARY)
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
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
    page.wait_for_url(site.route(_ISOLATED))
    _selected(page, _ISOLATED)
    body.get_by_role("link", name="Read the complete zero identity", exact=True).click()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    page.evaluate("() => history.back()")
    page.wait_for_url(site.route(_ISOLATED))
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


@pytest.mark.parametrize(("width", "height"), [(1600, 900), (1000, 900), (390, 844)])
def test_rich_workspace_region_and_full_reader_screenshots(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int, height: int,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": width, "height": height})
    _open(page, site, workspace_runtime)
    playwright.expect(page.locator('.bp-dag-node[data-autoform-node-id^="region:"]')).to_have_count(2)
    assert _pixels(page)["painted"] > 0
    if width <= 720:
        handle = page.get_by_role("button", name="Toggle mathematical reading", exact=True)
        if handle.get_attribute("aria-expanded") == "true":
            handle.click()
            _frames(page)
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
        report = {"viewport": [width, height], "route": page.url, "nodes": len(site.records), "areas": ["Geometry", "Analysis"],
                  "mathjax": page.evaluate("() => MathJax.version"), "complete_math": 5, "canvas": _pixels(page)}
        (directory / f"workspace-{width}x{height}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert page.evaluate("() => document === __workspaceDocument") is True
