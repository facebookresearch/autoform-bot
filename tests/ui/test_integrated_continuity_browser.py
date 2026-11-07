"""Real WebKit continuity and phone geometry over captured mathematical consumers.

Lean declarations in these fixtures are elementary zero identities. These are
publication and UI regressions, not claims of kernel verification.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import unquote, urlsplit

import pytest

from autoform_cli.scaffold import scaffold_project
from autoform_cli.ui.mathjax import MATHJAX_BUNDLE
from autoform_cli.ui.render import render_site
from tests.ui.test_integrated_workspace_browser import (
    _LAST_SENTENCE,
    _OTHER,
    _PinnedRuntime,
    _TARGET,
    _TARGET_TITLE,
    _WorkspaceSite,
    _frames,
    _full_math,
    _open,
    _peek_reader,
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
      const saved = {
        statuses: Array.from(document.querySelectorAll('.bp-dag-filter-popover input[value]:checked')).map(input=>input.value).sort(),
        reading: reader.scrollTop, outline: outline.scrollTop, query: document.querySelector('.bp-dag-search').value,
        outline_open: document.querySelector('.bp-dag-outline-toggle').getAttribute('aria-expanded')};
      history.back(); return saved;
    }""", cleared)


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
        assert link.get_attribute("aria-current") is None
    current = outline.locator(f'[data-navigation-node-id="{_LATE_RESULT}"]')
    playwright.expect(current).to_have_text(site.records[_LATE_RESULT]["title"])
    playwright.expect(current).to_have_attribute("href", site.route(_LATE_RESULT))
    playwright.expect(current).to_have_attribute("aria-current", "page")
    playwright.expect(outline.locator('[data-navigation-node-id][aria-current="page"]')).to_have_count(1)
    playwright.expect(outline.locator(f'[data-navigation-node-id="{_OMITTED_TOPIC}"]')).to_have_count(0)
    before = links.count()
    reveal = outline.get_by_role("button", name="Show more topics", exact=True)
    playwright.expect(reveal).to_be_visible()
    reveal.click()
    omitted = outline.locator(f'[data-navigation-node-id="{_OMITTED_TOPIC}"]')
    playwright.expect(omitted).to_have_text(site.records[_OMITTED_TOPIC]["title"])
    assert before < links.count() <= 170
    playwright.expect(current).to_have_attribute("aria-current", "page")
    playwright.expect(outline.locator('[data-navigation-node-id][aria-current="page"]')).to_have_count(1)
    _selected(page, _LATE_RESULT)
    assert _LATE_END in _full_math(page, _LATE_RESULT, 1).inner_text()
    assert page.url == site.route(_LATE_RESULT)
    assert documents == [site.route(_LATE_RESULT).split("#", 1)[0]]
    assert page.evaluate("() => document === __workspaceDocument") is True


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
    # Working the whole map writes no history entry of its own, so Back still leaves this one.
    page.locator(".bp-whole-map").get_by_role("button", name="Zoom in", exact=True).click()
    stage = page.locator(".bp-dag-stage").bounding_box()
    assert stage
    page.mouse.move(stage["x"] + stage["width"] * .88, stage["y"] + stage["height"] * .72)
    page.mouse.wheel(57, 39)
    page.locator(".bp-dag-search").fill(_TARGET_TITLE)
    page.get_by_role("button", name="Topics", exact=True).click()
    _frames(page)
    edited = _scroll_and_back(page)
    assert edited["reading"] > 0 and edited["outline"] > 0 and edited["outline_open"] == "true"
    assert edited["query"] == _TARGET_TITLE
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    assert _LAST_SENTENCE in _full_math(page).inner_text()
    page.evaluate("() => history.forward()")
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    _full_math(page, _OTHER, 1)
    _wait_continuity_state(page, edited)
    # Escape in the reader records a different view at exactly the same canonical
    # URL. A cache keyed only by URL would overwrite one entry with the other.
    # Focus without scrolling: the reader's position belongs to the entry being left.
    relations = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{_OTHER}"] details.bp-dependencies > summary')
    relations.evaluate("summary => summary.focus({preventScroll: true})")
    playwright.expect(relations).to_be_focused()
    page.keyboard.press("Escape")
    playwright.expect(page.locator(".bp-dag-has-selection")).to_have_count(0)
    assert page.url == site.route(_OTHER)
    page.locator(".bp-dag-search").fill("Cleared selection view")
    _frames(page)
    cleared = _scroll_and_back(page, cleared=True)
    _selected(page, _OTHER)
    playwright.expect(page.locator(".bp-dag-has-selection")).to_have_count(1)
    _wait_continuity_state(page, edited)
    page.evaluate("() => history.forward()")
    playwright.expect(page.locator(".bp-dag-has-selection")).to_have_count(0)
    _wait_continuity_state(page, cleared)
    assert "Its complete explanation remains available." in _full_math(page, _OTHER, 1).inner_text()
    assert page.url == site.route(_OTHER)
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
    assert page.evaluate("() => document === __workspaceDocument") is True


def _horizontal_bounds(page: object, locator: object) -> dict:
    measured = locator.evaluate("""element => {
      const box = element.getBoundingClientRect();
      return {left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height};
    }""")
    width = page.evaluate("() => innerWidth")
    assert measured["left"] >= -1 and measured["right"] <= width + 1
    assert measured["width"] > 0 and measured["height"] > 0
    return measured


def test_phone_reader_opens_at_half_covers_the_body_at_full_and_peeks_over_the_map(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 390, "height": 844})
    documents = _documents(page)
    _open(page, site, workspace_runtime)
    handle = page.get_by_role("button", name=re.compile(r", reading$"))
    _peek_reader(page, handle)
    _frames(page)
    playwright.expect(page.locator(".bp-whole-map .pm-part").first).to_be_visible()
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
    playwright.expect(page.locator(".bp-whole-map .pm-part").first).to_be_visible()
