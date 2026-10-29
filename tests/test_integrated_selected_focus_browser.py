"""Native selected-statement focus and Tab order over unchanged published reading.

The shared consumer's Lean declarations are elementary zero identities. These
checks establish browser/source behavior, not kernel verification.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import html5lib
import pytest

from tests.test_integrated_workspace_browser import (
    _BASE,
    _LAST_SENTENCE,
    _READY,
    _TARGET,
    _PinnedRuntime,
    _WorkspaceSite,
    _browse_contents,
    _focus_graph_node,
    _frames,
    _full_math,
    _open,
    _search,
    _selected,
    playwright,
    workspace_browser as workspace_browser,
    workspace_page as workspace_page,
    workspace_runtime as workspace_runtime,
    workspace_site as workspace_site,
)

pytestmark = pytest.mark.skipif(playwright is None, reason="Playwright is needed for native selected-focus checks")


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _published_source(page: object, route: str) -> dict:
    response = page.request.get(route.split("#", 1)[0], timeout=10_000)
    assert response.ok
    tree = html5lib.parse(response.text(), treebuilder="etree", namespaceHTMLElements=False)
    sources = [node for node in tree.iter() if "bp-reading-source" in node.get("class", "").split()]
    assert len(sources) == 1
    source = sources[0]
    return {
        "text": _normalized("".join(source.itertext())),
        "headings": [_normalized("".join(node.itertext())) for node in source.iter("h1")],
        "tex": ["".join(node.itertext()) for node in source.iter() if "arithmatex" in node.get("class", "").split()],
        "cards": [node.get("data-autoform-node-id") for node in source.iter()
                  if "bp-thmwrapper" in node.get("class", "").split()],
    }


def _complete_source(page: object, expected: dict) -> object:
    source = page.locator(".bp-reading-body > .bp-reading-source")
    playwright.expect(source).to_have_count(1)
    playwright.expect(source.locator("h1")).to_have_text(expected["headings"])
    assert source.locator(".bp-thmwrapper").evaluate_all("nodes=>nodes.map(node=>node.dataset.autoformNodeId)") == expected["cards"]
    playwright.expect(source.locator(".arithmatex")).to_have_count(len(expected["tex"]))
    playwright.expect(source.locator("mjx-container")).to_have_count(len(expected["tex"]))
    assert source.locator("mjx-merror,merror").count() == 0
    assert source.locator(".arithmatex").evaluate_all(
        "nodes=>nodes.map(node=>node.getAttribute('data-autoform-math-source'))",
    ) == expected["tex"]
    restored = source.evaluate("""(source,tex)=>{
      const clone=source.cloneNode(true);
      Array.from(clone.querySelectorAll('.arithmatex')).forEach((node,index)=>node.textContent=tex[index]);
      return clone.textContent;
    }""", expected["tex"])
    assert _normalized(restored) == expected["text"], "Selection must retain the complete published mathematical source"
    return source


@pytest.mark.parametrize("width,height", [(1600, 900), (1000, 900), (390, 844)])
@pytest.mark.parametrize("action", ["search", "card", "inventory"])
def test_selected_late_statement_focus_is_visible_and_next_tab_stays_at_the_statement(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
    width: int, height: int, action: str,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": width, "height": height})
    initial, selected = site.route(_BASE), site.route(_READY)
    assert urlsplit(selected).fragment
    initial_source = _published_source(page, initial)
    expected = _published_source(page, selected)
    # This later statement follows the complete long main statement on the
    # same published chapter; focusing its page H1 really would be offscreen.
    assert expected["cards"].index(_READY) > expected["cards"].index(_TARGET)
    assert _LAST_SENTENCE in expected["text"]
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime, _BASE)
    if action == "search":
        _search(page, site.records[_READY]["title"], _READY)
    elif action == "card":
        _focus_graph_node(page, _READY).click()
    else:
        _browse_contents(page, "Statements in this topic")
        page.locator(f'.bp-dag-inventory-row[data-inventory-node-id="{_READY}"]').click()
    page.wait_for_url(selected)
    _selected(page, _READY)
    _full_math(page, _READY, 1)
    _complete_source(page, expected)
    statement = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{_READY}"].bp-reading-current')
    heading = statement.locator(".bp-thmheading")
    playwright.expect(heading).to_have_count(1)
    playwright.expect(heading).to_be_focused()
    assert heading.get_attribute("tabindex") == "-1"
    _frames(page)
    assert heading.evaluate("""node=>{
      const box=node.getBoundingClientRect(),reader=node.closest('.bp-reading-body'),port=reader.getBoundingClientRect();
      const h1=reader.querySelector('h1').getBoundingClientRect();
      return reader.scrollTop>reader.clientHeight&&h1.bottom<port.top&&
        box.top>=port.top&&box.bottom<=port.bottom&&box.left>=port.left&&box.right<=port.right&&
        box.top>=0&&box.bottom<=innerHeight&&box.left>=0&&box.right<=innerWidth&&
        node.contains(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2));
    }"""), "Native focus must be on the visible selected heading, not the chapter title above the scrollport"
    scroll = page.locator(".bp-reading-body").evaluate("reader=>reader.scrollTop")
    history = page.evaluate("history.length")
    page.keyboard.press("Tab")
    _frames(page)
    assert statement.evaluate("""node=>{
      const active=document.activeElement;if(active===document.body||!node.contains(active))return false;
      const box=active.getBoundingClientRect(),port=node.closest('.bp-reading-body').getBoundingClientRect();
      return box.width>0&&box.height>0&&box.top>=port.top&&box.bottom<=port.bottom&&
        box.left>=port.left&&box.right<=port.right&&box.top>=0&&box.bottom<=innerHeight&&
        active.contains(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2));
    }"""), "The next real Tab stop must remain at the selected statement"
    assert page.locator(".bp-reading-body").evaluate("reader=>reader.scrollTop") == scroll
    assert page.url == selected and page.evaluate("history.length") == history
    _complete_source(page, expected)
    page.go_back()
    page.wait_for_url(initial)
    _complete_source(page, initial_source)
    assert page.evaluate("document===window.__workspaceDocument") is True
    assert documents == [initial.split("#", 1)[0]]
