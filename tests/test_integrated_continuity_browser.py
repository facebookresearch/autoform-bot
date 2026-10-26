"""Real WebKit continuity and phone geometry over captured mathematical consumers.

Lean declarations in these fixtures are elementary zero identities. These are
publication and UI regressions, not claims of kernel verification.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from autoform_cli.mathjax import MATHJAX_BUNDLE
from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_integrated_workspace_browser import (
    _AREAS,
    _LAST_SENTENCE,
    _LIBRARY,
    _OTHER,
    _OTHER_TITLE,
    _PinnedRuntime,
    _TARGET,
    _TARGET_TITLE,
    _WorkspaceSite,
    _assert_surface,
    _frames,
    _focus_graph_region,
    _full_math,
    _open,
    _peek_reader,
    _pixels,
    _search,
    _selected,
    _served,
    _surface,
    _write,
    playwright,
    workspace_browser as workspace_browser,
    workspace_page as workspace_page,
    workspace_runtime as workspace_runtime,
    workspace_site as workspace_site,
)

_LATE_PARENT = "topic-127"
_LATE_SCOPE = _LATE_PARENT + "/chapter"
_LATE_RESULT = _LATE_SCOPE + "/late"
_OMITTED_TOPIC = "topic-126"
_LATE_END = "The complete late mathematical explanation ends with this retained sentence."
_FIVE_AREAS = ("Algebra", "Analysis", "Geometry", "Number Theory", "Foundations")
_FIVE_TOPICS = tuple(f"topic-{number:02d}" for number in range(1, 16))


@pytest.fixture(scope="module")
def five_area_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_WorkspaceSite]:
    """Independent authored areas; planned targets, no Lean proof claims."""
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("five-authored-areas") / "project"
    scaffold_project(project, title="Five authored mathematical areas")
    blueprint = project / "blueprint"
    _write(blueprint / "README.md", "# Five authored mathematical areas\n\n- [Mathematical areas](roadmap/README.md)\n")
    _write(blueprint / "roadmap/README.md", (
        "# Five authored mathematical areas\n\nThese areas and modules are authored, not inferred from directory names.\n\n"
        + "\n".join(f"- [Authored topic {number}](./{topic}/README.md)"
                     for number, topic in enumerate(_FIVE_TOPICS, 1)) + "\n"
    ))
    for number, topic in enumerate(_FIVE_TOPICS):
        area_index, slot = divmod(number, 3)
        _write(blueprint / f"roadmap/{topic}/README.md", (
            f"---\nnavigation: module\narea: {_FIVE_AREAS[area_index]}\n---\n\n# Authored topic {number + 1}\n\n"
            "The area is explicit mathematical metadata.\n\n- [Planned zero identity](identity.md)\n"
        ))
        statement_link = "No prerequisites."
        proof_links = ""
        if area_index and slot == 0:
            predecessor = _FIVE_TOPICS[(area_index - 1) * 3]
            statement_link = f"- [Previous area's stated identity](../{predecessor}/identity.md)"
        if area_index and slot == 1:
            predecessor = _FIVE_TOPICS[(area_index - 1) * 3 + 2]
            proof_links = f"\n\n## Proof depends on\n\n- [Previous area's proof input](../{predecessor}/identity.md)\n"
        _write(blueprint / f"roadmap/{topic}/identity.md", (
            "---\ndeclaration: theorem\nnot_ready: true\n---\n\n"
            f"# Planned identity {number + 1}\n\nThe proposed target is $0=0$. "
            "This fixture records an unverified roadmap target, not a completed Lean proof.\n\n"
            "## Depends on\n\n" + statement_link + "\n" + proof_links
        ))
    render_site(blueprint, project / "site-src", lean_root=project)
    configuration = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    configuration["extra_javascript"] = [
        entry for entry in configuration["extra_javascript"]
        if getattr(entry, "path", str(entry)) != MATHJAX_BUNDLE
    ]
    build(configuration)
    index_path = project / "site/dependencies/index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    records = {record["id"]: record for record in index["nodes"]}
    root_path = index_path.parent / unquote(urlsplit(records["roadmap"]["data_url"]).path)
    root_graph = json.loads(root_path.read_text(encoding="utf-8"))
    with _served(project / "site") as url:
        yield _WorkspaceSite(url, records, root_graph)


@pytest.fixture(scope="module")
def continuity_outline_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_WorkspaceSite]:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("continuity-outline") / "project"
    scaffold_project(project, title="Large authored outline")
    blueprint = project / "blueprint"
    topics = [f"topic-{number:03d}" for number in range(128)]
    _write(blueprint / "README.md", "# Large authored outline\n\n- [Mathematics](roadmap/README.md)\n")
    _write(blueprint / "roadmap/README.md", (
        "# Complete indexed mathematics\n\nThese authored topics keep their real mathematical ancestry.\n\n"
        + "\n".join(f"- [Topic {number}](./{topic}/README.md)" for number, topic in enumerate(topics)) + "\n"
    ))
    for number, topic in enumerate(topics):
        if topic == _LATE_PARENT:
            _write(blueprint / f"roadmap/{topic}/README.md", (
                "# Late topic with exact ancestry\n\n- [Complete late statements](chapter/README.md)\n"
            ))
        else:
            _write(blueprint / f"roadmap/{topic}/README.md", f"# Authored topic {number}\n\n- [Reading note](note.md)\n")
            _write(blueprint / f"roadmap/{topic}/note.md", f"# Reading note {number}\n\nA narrative is not a declaration.\n")
    _write(blueprint / f"roadmap/{_LATE_SCOPE}/README.md", (
        "# Complete late statements\n\nThis is the real containing scope.\n\n- [Late zero identity](late.md)\n"
    ))
    _write(blueprint / f"roadmap/{_LATE_RESULT}.md", (
        "---\ndeclaration: theorem\nstatement: formalized\nproof: formalized\nlean: LateFixture.zero_identity\n---\n\n"
        "# Late zero identity\n\nThe equality $0=0$ holds.\n\n"
        + "\n\n".join(f"Explanation {number}. The complete mathematical reading paragraph remains part of the published statement."
                       for number in range(12))
        + "\n\n" + _LATE_END + "\n\n## Depends on\n\nNo prerequisites.\n"
    ))
    _write(project / "LateFixture.lean", "namespace LateFixture\ntheorem zero_identity : (0 : Nat) = 0 := rfl\nend LateFixture\n")
    _write(blueprint / "coverage/README.md", (
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
        f"| Late statement | DECOMPOSED | [Identity](../roadmap/{_LATE_RESULT}.md) |\n"
    ))
    render_site(blueprint, project / "site-src", lean_root=project)
    configuration = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    configuration["extra_javascript"] = [
        entry for entry in configuration["extra_javascript"]
        if getattr(entry, "path", str(entry)) != MATHJAX_BUNDLE
    ]
    build(configuration)
    index_path = project / "site/dependencies/index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    records = {record["id"]: record for record in index["nodes"]}
    root_path = index_path.parent / unquote(urlsplit(records["roadmap"]["data_url"]).path)
    root_graph = json.loads(root_path.read_text(encoding="utf-8"))
    with _served(project / "site") as url:
        yield _WorkspaceSite(url, records, root_graph)


def _documents(page: object) -> list[str]:
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url) if request.resource_type == "document" else None)
    return requests


def _continuity_state(page: object) -> dict:
    return {
        **_surface(page),
        "query": page.locator(".bp-dag-search").input_value(),
        "outline_open": page.get_by_role("button", name="Topics", exact=True).get_attribute("aria-expanded"),
    }


def _wait_continuity_state(page: object, expected: dict) -> None:
    page.wait_for_function("""expected => {
      const reader = document.querySelector('.bp-reading-body'), outline = document.querySelector('.bp-dag-navigation');
      return document.querySelector('.bp-dag-search').value === expected.query &&
        document.querySelector('.bp-dag-outline-toggle').getAttribute('aria-expanded') === expected.outline_open &&
        Math.abs(reader.scrollTop - expected.reading) <= 1 && Math.abs(outline.scrollTop - expected.outline) <= 1;
    }""", arg=expected)
    actual = _continuity_state(page)
    if expected["camera"] is not None:
        _assert_surface(actual, expected)
    else:
        assert actual["camera"] is None
        assert actual["statuses"] == expected["statuses"]
    assert actual["query"] == expected["query"] and actual["outline_open"] == expected["outline_open"]


def _scroll_and_back(page: object, *, cleared: bool = False) -> dict:
    # Depart in the same task as the last real scroll edits. Waiting for a
    # checkpoint first would hide the outgoing-entry/history race.
    return page.evaluate("""cleared => {
      const reader = document.querySelector('.bp-reading-body'), outline = document.querySelector('.bp-dag-navigation');
      const readerMax = reader.scrollHeight-reader.clientHeight, outlineMax = outline.scrollHeight-outline.clientHeight;
      if (readerMax <= 0 || outlineMax <= 0) throw new Error('The genuine mathematical consumer must be scrollable');
      reader.scrollTop = cleared ? reader.scrollTop/3 : Math.min(140, readerMax);
      outline.scrollTop = cleared ? 0 : Math.min(280, outlineMax);
      const selected = document.querySelector('.bp-dag-node[data-selected=true]');
      const matrix = selected && new DOMMatrix(getComputedStyle(selected).transform);
      const saved = {camera: matrix && [matrix.a,matrix.d,matrix.e,matrix.f],
        statuses: Array.from(document.querySelectorAll('.bp-dag-filter-popover input[value]:checked')).map(input=>input.value).sort(),
        reading: reader.scrollTop, outline: outline.scrollTop, query: document.querySelector('.bp-dag-search').value,
        outline_open: document.querySelector('.bp-dag-outline-toggle').getAttribute('aria-expanded')};
      history.back(); return saved;
    }""", cleared)


def test_keyboard_region_expansion_focuses_a_new_readable_map_control(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    documents = _documents(page)
    _open(page, site, workspace_runtime)
    region = _focus_graph_region(page, "Geometry")
    assert _pixels(page)["painted"] > 0
    region.focus()
    playwright.expect(region).to_be_focused()
    page.keyboard.press("Enter")
    playwright.expect(region).to_have_count(0)
    playwright.expect(page.locator('.bp-dag-node[data-autoform-node-id="scope:division-00"]')).to_be_visible()
    _frames(page)
    focused = page.locator(".bp-dag-node:focus")
    playwright.expect(focused).to_have_count(1)
    assert focused.get_attribute("data-autoform-node-id").startswith("scope:")
    assert page.locator(".bp-dag-stage").evaluate("stage => stage.contains(document.activeElement)") is True
    playwright.expect(page.locator('.bp-dag-node[tabindex="0"]')).to_have_count(1)
    playwright.expect(focused).to_have_attribute("tabindex", "0")
    assert page.url == site.route("roadmap")
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(_LIBRARY)
    assert _pixels(page)["painted"] > 0
    assert documents == [site.route("roadmap")]
    assert page.evaluate("() => document === __workspaceDocument") is True


@pytest.mark.parametrize(("width", "height"), [(390, 844), (1000, 900), (1600, 900)])
def test_region_graph_first_keeps_native_reading_reopen_and_back_pane_camera(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int, height: int,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": width, "height": height})
    documents = _documents(page)
    _open(page, site, workspace_runtime)
    # The open desktop reader over an area closes with ×, so the handle is found by its class, not its name.
    handle = page.locator(".bp-reading-pane > .bp-dag-sheet-handle")
    inner = page.locator(".bp-reading-pane > .bp-dag-inspector-inner")
    initial_width = page.locator(".bp-dag-stage").bounding_box()["width"]
    _focus_graph_region(page, "Geometry").press("Enter")
    _frames(page)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    playwright.expect(inner).to_have_attribute("inert", "")
    playwright.expect(inner).to_have_attribute("aria-hidden", "true")
    closed_width = page.locator(".bp-dag-stage").bounding_box()["width"]
    if width > 720:
        assert closed_width > initial_width + 250
        playwright.expect(inner).to_be_hidden()
    else:
        assert closed_width == pytest.approx(width, abs=1)
    handle.click()
    _frames(page)
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(inner).not_to_have_attribute("inert", "")
    assert inner.get_attribute("inert") is None and inner.get_attribute("aria-hidden") is None
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(_LIBRARY)
    playwright.expect(page.locator(".bp-reading-body")).to_be_visible()
    if width > 720:
        playwright.expect(handle).to_be_visible()
        playwright.expect(handle).to_have_text("×")
        playwright.expect(page.locator(".bp-reading-body")).to_be_focused()
        url = page.url
        handle.click()
        playwright.expect(handle).to_have_attribute("aria-expanded", "false")
        playwright.expect(inner).to_have_attribute("inert", "")
        assert page.url == url and page.evaluate("() => document === __workspaceDocument") is True
        handle.click()
        playwright.expect(handle).to_have_attribute("aria-expanded", "true")
        playwright.expect(page.locator(".bp-reading-body")).to_be_focused()
    parent = page.locator(".bp-dag-breadcrumb").get_by_role("link", name=_LIBRARY, exact=True)
    playwright.expect(parent).to_have_attribute("href", site.route("roadmap"))
    parent.click()
    assert page.url == site.route("roadmap")
    _focus_graph_region(page, "Geometry").press("Enter")
    _frames(page)
    focused = page.locator(".bp-dag-node:focus")
    scope_id = focused.get_attribute("data-autoform-node-id")
    assert scope_id.startswith("scope:")
    camera = focused.evaluate("""node=>{const m=new DOMMatrix(getComputedStyle(node).transform);return [m.a,m.d,m.e,m.f];}""")
    focused.press("Enter")
    page.wait_for_url(site.route(scope_id.removeprefix("scope:")))
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(inner).not_to_have_attribute("inert", "")
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(site.records[scope_id.removeprefix("scope:")]["title"])
    playwright.expect(page.locator(".bp-dag-viewer")).not_to_have_attribute("aria-busy", "true")
    _search(page, _TARGET_TITLE, _TARGET)
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    assert _LAST_SENTENCE in _full_math(page).inner_text()
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    page.go_back()
    page.wait_for_url(site.route(scope_id.removeprefix("scope:")))
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    page.go_back()
    page.wait_for_url(site.route("roadmap"))
    _frames(page)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    playwright.expect(inner).to_have_attribute("inert", "")
    playwright.expect(handle).to_be_visible()
    restored = page.locator(".bp-dag-node:focus")
    playwright.expect(restored).to_have_attribute("data-autoform-node-id", scope_id)
    assert restored.evaluate("""node=>{const m=new DOMMatrix(getComputedStyle(node).transform);return [m.a,m.d,m.e,m.f];}""") == pytest.approx(camera, abs=2)
    assert page.locator(".bp-dag-stage").bounding_box()["width"] == pytest.approx(closed_width, abs=1)
    assert documents == [site.route("roadmap")]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_arrow_roving_keeps_selection_and_full_reader_until_enter_activates_the_peer(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    documents = _documents(page)
    _open(page, site, workspace_runtime, _TARGET)
    first = _selected(page, _TARGET)
    assert _LAST_SENTENCE in _full_math(page).inner_text()
    page.evaluate("() => window.__continuityReader = document.querySelector('.bp-reading-body > .bp-reading-source')")
    first.focus()
    page.keyboard.press("ArrowRight")
    peer = page.locator(f'.bp-dag-node[data-autoform-node-id="{_OTHER}"]')
    playwright.expect(peer).to_be_focused()
    _frames(page)
    playwright.expect(page.locator('.bp-dag-node[tabindex="0"]')).to_have_count(1)
    playwright.expect(peer).to_have_attribute("tabindex", "0")
    playwright.expect(peer).to_have_attribute("data-selected", "false")
    _selected(page, _TARGET)
    assert page.url == site.route(_TARGET)
    assert page.evaluate("() => document.querySelector('.bp-reading-body > .bp-reading-source') === __continuityReader") is True
    assert _LAST_SENTENCE in _full_math(page).inner_text()
    assert _pixels(page)["painted"] > 0
    page.keyboard.press("Enter")
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    counterpart = _full_math(page, _OTHER, 1)
    assert "Its complete explanation remains available." in counterpart.inner_text()
    assert len(counterpart.inner_text()) > 420
    assert page.title() == _OTHER_TITLE
    assert page.evaluate("() => document.querySelector('.bp-reading-body > .bp-reading-source') !== __continuityReader") is True
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
    assert page.evaluate("() => document === __workspaceDocument") is True
    assert _pixels(page)["painted"] > 0


def test_late_authored_scope_and_ancestry_survive_the_outline_bound_and_explicit_reveal(
    workspace_page: object, continuity_outline_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, continuity_outline_site
    documents = _documents(page)
    scopes = [record for record in site.records.values() if record.get("scope") == record["id"]]
    assert len(scopes) > 120
    _open(page, site, workspace_runtime, _LATE_RESULT)
    _selected(page, _LATE_RESULT)
    card = _full_math(page, _LATE_RESULT, 1)
    assert _LATE_END in card.inner_text() and len(card.inner_text()) > 420
    outline = page.locator(".bp-dag-navigation")
    links = outline.locator("[data-navigation-node-id]")
    assert links.count() <= 120
    for node_id in (_LATE_PARENT, _LATE_SCOPE):
        link = outline.locator(f'[data-navigation-node-id="{node_id}"]')
        playwright.expect(link).to_have_text(site.records[node_id]["title"])
        assert link.get_attribute("href") == site.route(node_id)
    playwright.expect(outline.locator(f'[data-navigation-node-id="{_LATE_SCOPE}"]')).to_have_attribute("aria-current", "page")
    playwright.expect(outline.locator(f'[data-navigation-node-id="{_OMITTED_TOPIC}"]')).to_have_count(0)
    before = links.count()
    reveal = outline.get_by_role("button", name="Show more topics", exact=True)
    playwright.expect(reveal).to_be_visible()
    reveal.click()
    omitted = outline.locator(f'[data-navigation-node-id="{_OMITTED_TOPIC}"]')
    playwright.expect(omitted).to_have_text(site.records[_OMITTED_TOPIC]["title"])
    assert before < links.count() <= 170
    playwright.expect(outline.locator(f'[data-navigation-node-id="{_LATE_SCOPE}"]')).to_have_attribute("aria-current", "page")
    _selected(page, _LATE_RESULT)
    assert _LATE_END in _full_math(page, _LATE_RESULT, 1).inner_text()
    assert page.url == site.route(_LATE_RESULT)
    assert documents == [site.route(_LATE_RESULT).split("#", 1)[0]]
    assert page.evaluate("() => document === __workspaceDocument") is True
    assert _pixels(page)["painted"] > 0


def test_edited_outgoing_entry_and_same_url_entries_restore_their_own_reading_and_map_state(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 1000, "height": 600})
    documents = _documents(page)
    _open(page, site, workspace_runtime, _TARGET)
    _full_math(page)
    page.locator(".bp-reading-body").get_by_role("link", name="Read the counterpart identity", exact=True).click()
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    _full_math(page, _OTHER, 1)
    before = _surface(page)
    page.get_by_role("button", name="Zoom in", exact=True).click()
    stage = page.locator(".bp-dag-stage").bounding_box()
    assert stage
    page.mouse.move(stage["x"] + stage["width"] * .88, stage["y"] + stage["height"] * .72)
    page.mouse.wheel(57, 39)
    page.locator(".bp-dag-search").fill(_TARGET_TITLE)
    page.get_by_role("button", name="Topics", exact=True).click()
    _frames(page)
    edited = _scroll_and_back(page)
    assert edited["reading"] > 0 and edited["outline"] > 0 and edited["outline_open"] == "true"
    assert edited["query"] == _TARGET_TITLE and edited["camera"] != before["camera"]
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    assert _LAST_SENTENCE in _full_math(page).inner_text()
    page.evaluate("() => history.forward()")
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    _full_math(page, _OTHER, 1)
    _wait_continuity_state(page, edited)
    # Escape records a different view at exactly the same canonical URL. A
    # cache keyed only by URL would overwrite one entry with the other.
    _selected(page, _OTHER).focus()
    page.keyboard.press("Escape")
    playwright.expect(page.locator('.bp-dag-node[data-selected="true"]')).to_have_count(0)
    assert page.url == site.route(_OTHER)
    page.locator(".bp-dag-search").fill("Cleared selection view")
    _frames(page)
    cleared = _scroll_and_back(page, cleared=True)
    _selected(page, _OTHER)
    _wait_continuity_state(page, edited)
    page.evaluate("() => history.forward()")
    playwright.expect(page.locator('.bp-dag-node[data-selected="true"]')).to_have_count(0)
    _wait_continuity_state(page, cleared)
    assert "Its complete explanation remains available." in _full_math(page, _OTHER, 1).inner_text()
    assert page.url == site.route(_OTHER)
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
    assert page.evaluate("() => document === __workspaceDocument") is True
    assert _pixels(page)["painted"] > 0


def _horizontal_bounds(page: object, locator: object) -> dict:
    measured = locator.evaluate("""element => {
      const box = element.getBoundingClientRect();
      return {left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height};
    }""")
    width = page.evaluate("() => innerWidth")
    assert measured["left"] >= -1 and measured["right"] <= width + 1
    assert measured["width"] > 0 and measured["height"] > 0
    return measured


def test_phone_reader_opens_at_half_covers_the_body_at_full_and_peeks_with_readable_dependency_regions(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 390, "height": 844})
    documents = _documents(page)
    _open(page, site, workspace_runtime)
    handle = page.get_by_role("button", name=re.compile(r", reading$"))
    _peek_reader(page, handle)
    _frames(page)
    page.get_by_role("button", name="Fit the map", exact=True).click()
    _frames(page)
    regions = page.locator('.bp-dag-node[data-autoform-node-id^="region:"]')
    assert regions.count() > 0
    geometry = regions.evaluate_all("""nodes => nodes.map(node => {
      const box = node.getBoundingClientRect(), label = node.querySelector('.bp-dag-node-title');
      const matrix = new DOMMatrix(getComputedStyle(node).transform);
      return {left: box.left, right: box.right, top: box.top, bottom: box.bottom,
        title: label.textContent, font: parseFloat(getComputedStyle(label).fontSize) * Math.hypot(matrix.a, matrix.b)};
    })""")
    assert {node["title"] for node in geometry} <= set(_AREAS)
    stage = page.locator(".bp-dag-stage").bounding_box()
    assert stage
    for node in geometry:
        assert node["font"] >= 15
    for index, first in enumerate(geometry):
        for second in geometry[index + 1:]:
            assert (first["right"] + 8 <= second["left"] or second["right"] + 8 <= first["left"] or
                    first["bottom"] + 8 <= second["top"] or second["bottom"] + 8 <= first["top"])
    assert _pixels(page)["painted"] > 0
    _search(page, _TARGET_TITLE, _TARGET)
    page.wait_for_url(site.route(_TARGET))
    assert _LAST_SENTENCE in _full_math(page).inner_text()
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")

    def measure() -> list[dict]:
        return [_horizontal_bounds(page, page.locator(selector)) for selector in (
            ".bp-dag-body", ".bp-reading-pane", ".bp-reading-pane .bp-dag-inspector-inner", ".bp-reading-body")]

    def reader_is_hit() -> bool:
        return page.locator(".bp-reading-body").evaluate("""reader => {
      const box = reader.getBoundingClientRect(), hit = document.elementFromPoint(box.left+box.width/2, box.top+box.height/2);
      return reader.contains(hit);
    }""")

    # A result opens at half: the map keeps the top 45% and the reader ends at the body's bottom.
    body, pane, inner, reader = measure()
    _horizontal_bounds(page, page.locator(".bp-dag-head"))
    assert pane["top"] == pytest.approx(body["top"] + body["height"] * .45, abs=2)
    assert pane["height"] == pytest.approx(body["height"], abs=1)
    assert inner["height"] == pytest.approx(body["height"] * .55 - 56, abs=2)
    assert reader["top"] >= pane["top"] + 56 - 1 and reader["bottom"] <= body["bottom"] + 1
    assert reader_is_hit() is True
    assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1") is True
    # A tap covers the whole body, which takes the hidden toolbar's row.
    handle.click()
    _frames(page)
    playwright.expect(page.locator(".bp-dag-toolbar")).to_be_hidden()
    body, pane, inner, reader = measure()
    assert pane["top"] == pytest.approx(body["top"], abs=1)
    assert pane["height"] == pytest.approx(body["height"], abs=1)
    assert inner["height"] == pytest.approx(body["height"] - 56, abs=2)
    assert reader["top"] >= body["top"] + 56 - 1 and reader["bottom"] <= body["bottom"] + 1
    assert reader_is_hit() is True
    page.get_by_role("button", name="Show the map", exact=True).click()
    _frames(page)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    body = _horizontal_bounds(page, page.locator(".bp-dag-body"))
    closed = _horizontal_bounds(page, page.locator(".bp-reading-pane"))
    peek = max(0, min(closed["bottom"], body["bottom"]) - max(closed["top"], body["top"]))
    assert peek == pytest.approx(56, abs=1)
    assert handle.bounding_box()["height"] == pytest.approx(56, abs=1)
    assert handle.evaluate("""element => {
      const box = element.getBoundingClientRect();
      return element.contains(document.elementFromPoint(box.left+box.width/2, box.top+box.height/2));
    }""") is True
    handle.click()
    assert _LAST_SENTENCE in _full_math(page).inner_text()
    assert documents == [site.route("roadmap")]
    assert page.evaluate("() => document === __workspaceDocument") is True
    assert _pixels(page)["painted"] > 0


@pytest.mark.parametrize(("width", "height"), [(390, 844), (1000, 900)])
def test_five_authored_dependency_areas_remain_readable_reachable_and_keep_native_activation(
    workspace_page: object, five_area_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int, height: int,
) -> None:
    page, site = workspace_page, five_area_site
    page.set_viewport_size({"width": width, "height": height})
    captured = site.root_graph["nodes"]
    assert len(captured) == 15
    assert {node["area"] for node in captured} == set(_FIVE_AREAS)
    expected = {area: sum(node["member_count"] for node in captured if node["area"] == area) for area in _FIVE_AREAS}
    assert expected == dict.fromkeys(_FIVE_AREAS, 3)
    targets = [record for record in site.records.values() if record.get("result") is True]
    assert len(targets) == 15 and all(record["status"] == "not_ready" for record in targets)
    assert all(node["status_counts"] == [["not_ready", 1]] for node in captured)
    assert sum(edge["statement"] for edge in site.root_graph["edges"]) == 4
    assert sum(edge["proof"] for edge in site.root_graph["edges"]) == 4
    authored_areas = {node["id"]: node["area"] for node in captured}
    assert all(authored_areas[edge["source"]] != authored_areas[edge["target"]] for edge in site.root_graph["edges"])
    documents = _documents(page)
    _open(page, site, workspace_runtime)
    if width <= 720:
        _peek_reader(page, page.get_by_role("button", name=re.compile(r", reading$")))
    _frames(page)
    page.get_by_role("button", name="Fit the map", exact=True).click()
    page.evaluate("() => document.fonts.ready")
    _frames(page)
    chosen = page.locator(f'.bp-dag-node[data-autoform-node-id="region:{_FIVE_AREAS[0]}"]')
    playwright.expect(chosen).to_be_visible()
    chosen.focus()
    geometry = []
    stage = page.locator(".bp-dag-stage").bounding_box()
    context = page.locator(".bp-map-context").bounding_box()
    key = page.locator(".bp-map-key").bounding_box()
    assert stage and context and key
    for index, area in enumerate(_FIVE_AREAS):
        active = page.locator('.bp-dag-node:focus')
        playwright.expect(active).to_have_attribute("data-autoform-node-id", f"region:{area}")
        geometry.append(active.evaluate("""node => {
      const box = node.getBoundingClientRect(), title = node.querySelector('.bp-dag-node-title');
      const meta = node.querySelector('.bp-dag-node-meta'), sample = node.querySelector('.bp-dag-node-summary');
      const matrix = new DOMMatrix(getComputedStyle(node).transform), scale = Math.hypot(matrix.a, matrix.b);
      return {left:box.left,right:box.right,top:box.top,bottom:box.bottom,width:box.width,
        title:title.textContent,meta:meta.textContent,aria:node.getAttribute('aria-label'),
        shown:[...meta.children].filter(span => getComputedStyle(span).display !== 'none').map(span => span.textContent).join(''),
        summary:sample ? sample.textContent : null,
        titlePixels:parseFloat(getComputedStyle(title).fontSize)*scale,
        metaPixels:parseFloat(getComputedStyle(meta).fontSize)*scale,
        titleFits:title.scrollWidth<=title.clientWidth+1,
        sampleVisible:!!sample && getComputedStyle(sample).display!=='none' && sample.clientHeight>0,
        hit:node.contains(document.elementFromPoint(box.left+box.width/2,box.top+box.height/2))};
    }"""))
        if index + 1 < len(_FIVE_AREAS):
            active.press("ArrowDown" if stage["width"] <= 620 else "ArrowRight")
            _frames(page)
    assert {node["title"] for node in geometry} == set(_FIVE_AREAS)
    assert 280 <= stage["width"] <= width + 1
    for node in geometry:
        assert node["width"] >= 200 and node["titlePixels"] >= 15 and node["metaPixels"] >= 12
        assert node["meta"] == "3 topics · 0 of 3 fully proved" and ", 0 of 3 fully proved" in node["aria"]
        assert node["titleFits"] and node["hit"]
        if stage["width"] <= 620:
            assert not node["sampleVisible"] and node["shown"] == node["meta"]
        else:
            # The summary leads with the topic count, so the meta shows only the fully proved count, whole.
            assert node["sampleVisible"] and node["summary"].startswith("3 topics: ") and node["shown"] == "0 of 3 fully proved"
        assert node["left"] >= stage["x"] - 1 and node["right"] <= stage["x"] + stage["width"] + 1
        assert (node["left"] + node["right"]) / 2 == pytest.approx(stage["x"] + stage["width"] / 2, abs=2)
        assert node["top"] >= context["y"] + context["height"] + 8
        assert node["bottom"] <= key["y"] - 8
    assert _pixels(page)["painted"] > 0
    output = os.environ.get("AUTOFORM_UI_SCREENSHOT_DIR")
    if output:
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"five-areas-{width}x{height}.png"))
        report = {"viewport": [width, height], "candidate_sha": os.environ.get("AUTOFORM_CI_SOURCE_SHA"),
                  "geometry_views": "individual native keyboard focus jumps",
                  "counts": expected, "regions": geometry, "stage": stage, "context": context, "key": key,
                  "captured_edges": site.root_graph["edges"], "canvas": _pixels(page), "kernel_checked": False}
        (directory / f"five-areas-{width}x{height}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for _ in range(len(_FIVE_AREAS) - 1):
        page.locator('.bp-dag-node:focus').press("ArrowUp" if stage["width"] <= 620 else "ArrowLeft")
        _frames(page)
    playwright.expect(chosen).to_be_visible()
    chosen.click()
    playwright.expect(chosen).to_have_count(0)
    playwright.expect(page.locator(f'.bp-dag-node[data-autoform-node-id="scope:{_FIVE_TOPICS[0]}"]')).to_be_visible()
    assert page.url == site.route("roadmap")
    assert page.evaluate("() => document === __workspaceDocument") is True
    assert documents == [site.route("roadmap")]
    assert _pixels(page)["painted"] > 0
