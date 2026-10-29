"""Native selected-statement focus and Tab order over unchanged published reading.

The shared consumer's Lean declarations are elementary zero identities. These
checks establish browser/source behavior, not kernel verification.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import html5lib
import pytest

from tests.test_dag_viewer_browser import (
    _assert_hit,
    _reading_card,
    _ready_site,
    _site_record,
    _site_route,
    marked_math_site_url as marked_math_site_url,
)
from tests.test_integrated_explorer_browser import _routes
from tests.test_integrated_workspace_browser import (
    _BASE,
    _LAST_SENTENCE,
    _OTHER,
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


def _native_diagnostic(page: object, label: str, details: dict) -> None:
    folder = os.environ.get("AUTOFORM_UI_SCREENSHOT_DIR")
    if not folder:
        return
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / f"selected-focus-{label}.png"))
    evidence = {"url": page.url, "viewport": page.viewport_size, "history_length": page.evaluate("history.length"),
                "details": details}
    (directory / f"selected-focus-{label}.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


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


def test_full_phone_reader_keeps_covered_map_and_toolbar_out_of_native_tab_order(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 390, "height": 844})
    selected = site.route(_READY)
    expected = _published_source(page, selected)
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime, _BASE)
    _search(page, site.records[_READY]["title"], _READY)
    page.wait_for_url(selected)
    _full_math(page, _READY, 1)
    _complete_source(page, expected)
    viewer, handle = page.locator(".bp-dag-viewer"), page.locator(".bp-reading-pane > .bp-dag-sheet-handle")
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(handle).to_have_attribute("title", "Expand reading")
    assert "bp-dag-sheet-full" not in viewer.get_attribute("class").split()
    handle.click()
    _frames(page)
    playwright.expect(viewer).to_have_class(re.compile(r"\bbp-dag-sheet-full\b"))
    playwright.expect(handle).to_have_attribute("title", "Close reading")
    playwright.expect(page.locator(".bp-dag-main")).to_have_attribute("inert", "")
    playwright.expect(page.locator(".bp-dag-toolbar")).to_be_hidden()
    playwright.expect(page.locator(".bp-reading-body")).to_be_visible()
    assert page.locator(".bp-reading-pane").evaluate("""pane=>{
      const p=pane.getBoundingClientRect(),body=pane.closest('.bp-dag-body').getBoundingClientRect();
      return Math.abs(p.top-body.top)<=1&&Math.abs(p.height-body.height)<=1;
    }""")
    _complete_source(page, expected)
    history = page.evaluate("history.length")
    # Real keyboard traversal is the oracle. The DOM count only bounds the
    # walk; no focus is assigned to a computed sequence or hidden map proxy.
    bound = page.locator("a[href],button,input,select,textarea,summary,[tabindex]").count() + 2
    assert bound <= 2_000
    for direction in ("Tab", "Shift+Tab"):
        handle.focus()
        seen = []
        for _ in range(bound):
            page.keyboard.press(direction)
            _frames(page)
            focused = page.evaluate("""() => {
              const node=document.activeElement;
              if(node===document.body||node===document.documentElement)return {boundary:true};
              const box=node.getBoundingClientRect(),main=document.querySelector('.bp-dag-main');
              const toolbar=document.querySelector('.bp-dag-toolbar'),outline=document.querySelector('.bp-dag-navigation');
              const reading=document.querySelector('.bp-reading-pane'),handle=reading.querySelector('.bp-dag-sheet-handle');
              const style=getComputedStyle(node);
              const rectangle=box=>({left:box.left,right:box.right,top:box.top,bottom:box.bottom,width:box.width,height:box.height});
              const hit=document.elementFromPoint(box.x+box.width/2,box.y+box.height/2),parents=[];
              const fragments=Array.from(node.getClientRects()).filter(part=>part.width>0&&part.height>0);
              for(let parent=node.parentElement;parent&&parents.length<8;parent=parent.parentElement){
                const css=getComputedStyle(parent);parents.push({tag:parent.tagName,class:parent.className,
                  box:rectangle(parent.getBoundingClientRect()),scroll:[parent.scrollLeft,parent.scrollTop],
                  client:[parent.clientWidth,parent.clientHeight],overflow:[css.overflowX,css.overflowY],clipPath:css.clipPath});
              }
              return {boundary:false,atHandle:node===handle,inReader:reading.contains(node),
                covered:main.contains(node)||toolbar.contains(node)||outline.contains(node)||Boolean(node.closest('[inert]')),
                visible:box.width>0&&box.height>0&&box.left>=0&&box.right<=innerWidth&&box.top>=0&&box.bottom<=innerHeight&&
                  style.visibility!=='hidden'&&fragments.length>0&&fragments.every(part=>
                    part.left>=0&&part.right<=innerWidth&&part.top>=0&&part.bottom<=innerHeight&&
                    node.contains(document.elementFromPoint(part.x+part.width/2,part.y+part.height/2))),
                tag:node.tagName,id:node.id,class:node.className,href:node.getAttribute('href'),text:node.textContent.slice(0,240),
                box:rectangle(box),clientRects:Array.from(node.getClientRects()).slice(0,16).map(rectangle),
                hit:hit&&{tag:hit.tagName,id:hit.id,class:hit.className,text:hit.textContent.slice(0,120)},
                font:{family:style.fontFamily,size:style.fontSize,lineHeight:style.lineHeight},
                reading:rectangle(reading.getBoundingClientRect()),windowScroll:[scrollX,scrollY],parents};
            }""")
            if focused["boundary"]:
                break  # Native browser chrome is outside the document's Tab order.
            if focused["covered"] or not focused["visible"]:
                _native_diagnostic(page, f"full-reader-{direction}", focused)
            assert not focused["covered"] and focused["visible"], json.dumps(focused, indent=2)
            seen.append(focused)
            if focused["atHandle"]:
                break
        assert seen, f"{direction} did not reach any real native focus target"
        if direction == "Tab":
            assert any(target["inReader"] for target in seen)
    assert page.url == selected and page.evaluate("history.length") == history
    _complete_source(page, expected)
    page.get_by_role("button", name="Show the map", exact=True).click()
    _frames(page)
    assert "bp-dag-sheet-full" not in viewer.get_attribute("class").split()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    assert page.locator(".bp-dag-main").get_attribute("inert") is None
    playwright.expect(page.locator(".bp-dag-toolbar")).to_be_visible()
    _complete_source(page, expected)
    assert page.url == selected and page.evaluate("history.length") == history
    assert page.evaluate("document===window.__workspaceDocument") is True
    assert documents == [site.route(_BASE).split("#", 1)[0]]


@pytest.mark.parametrize("shortcut", ["/", "Control+k", "Meta+k"], ids=["slash", "control-k", "meta-k"])
def test_full_phone_reader_shortcut_reveals_focused_search_at_half_without_changing_the_article(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, shortcut: str,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 390, "height": 844})
    initial, selected = site.route(_BASE), site.route(_TARGET)
    initial_source, expected = _published_source(page, initial), _published_source(page, selected)
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime, _BASE)
    _search(page, site.records[_TARGET]["title"], _TARGET)
    page.wait_for_url(selected)
    _selected(page, _TARGET)
    playwright.expect(_full_math(page)).to_contain_text(_LAST_SENTENCE)
    source = _complete_source(page, expected)
    source.evaluate("node=>window.__shortcutReadingSource=node")
    viewer, handle = page.locator(".bp-dag-viewer"), page.locator(".bp-reading-pane > .bp-dag-sheet-handle")
    playwright.expect(handle).to_have_attribute("title", "Expand reading")
    handle.click()
    _frames(page)
    playwright.expect(viewer).to_have_class(re.compile(r"\bbp-dag-sheet-full\b"))
    playwright.expect(page.locator(".bp-dag-main")).to_have_attribute("inert", "")
    playwright.expect(page.locator(".bp-dag-toolbar")).to_be_hidden()
    link = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{_TARGET}"]').get_by_role(
        "link", name="Read the counterpart identity", exact=True,
    )
    playwright.expect(link).to_have_attribute("href", site.route(_OTHER))
    link.scroll_into_view_if_needed()
    link.focus()
    _frames(page)
    playwright.expect(link).to_be_focused()
    _assert_hit(page, link)
    link.evaluate("node=>window.__shortcutReadingLink=node")
    history = page.evaluate("history.length")
    search = page.locator(".bp-dag-search")
    value = search.input_value()
    page.keyboard.press(shortcut)
    _frames(page)
    playwright.expect(search).to_be_visible()
    playwright.expect(search).to_be_focused()
    _assert_hit(page, search)
    assert search.evaluate("node=>{const r=node.getBoundingClientRect();return r.width>0&&r.height>0&&r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;}")
    assert search.input_value() == value
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(handle).to_have_attribute("title", "Expand reading")
    assert "bp-dag-sheet-full" not in viewer.get_attribute("class").split()
    assert page.locator(".bp-dag-main").get_attribute("inert") is None
    playwright.expect(page.locator(".bp-dag-toolbar")).to_be_visible()
    assert page.locator(".bp-reading-pane").evaluate("""pane=>{
      const p=pane.getBoundingClientRect(),body=pane.closest('.bp-dag-body').getBoundingClientRect();
      return Math.abs(p.top-body.top-.45*body.height)<=1&&Math.abs(p.height-body.height)<=1;
    }""")
    assert page.url == selected and page.evaluate("history.length") == history
    _selected(page, _TARGET)
    playwright.expect(_full_math(page)).to_contain_text(_LAST_SENTENCE)
    _complete_source(page, expected)
    assert source.evaluate("node=>node===__shortcutReadingSource")
    assert link.evaluate("node=>node===__shortcutReadingLink&&node.isConnected")
    assert page.evaluate("document===window.__workspaceDocument") is True
    page.go_back()
    page.wait_for_url(initial)
    _frames(page)
    _selected(page, _BASE)
    _complete_source(page, initial_source)
    assert page.evaluate("document===window.__workspaceDocument") is True
    assert documents == [initial.split("#", 1)[0]]


def test_320px_link_key_is_whole_native_and_keeps_half_reader_feedback_and_camera(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 320, "height": 844})
    first, selected = site.route(_BASE), site.route(_TARGET)
    initial_source, expected = _published_source(page, first), _published_source(page, selected)
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    # A real failed optional request must retain the mathematical map and say
    # that concept coverage is unavailable, not manufacture an empty sidecar.
    _open(page, site, workspace_runtime, _BASE, wiki_route=lambda route: route.abort())
    playwright.expect(page.locator(".bp-map-key-body")).to_contain_text("Concept links unavailable")
    _complete_source(page, initial_source)
    camera = """nodes=>nodes.map(node=>[node.dataset.autoformNodeId,getComputedStyle(node).transform])
      .sort((a,b)=>a[0].localeCompare(b[0]))"""
    initial_camera = page.locator(".bp-dag-node").evaluate_all(camera)
    assert initial_camera
    _search(page, site.records[_TARGET]["title"], _TARGET)
    page.wait_for_url(selected)
    _frames(page)
    _assert_hit(page, _selected(page, _TARGET))
    playwright.expect(_full_math(page)).to_contain_text(_LAST_SENTENCE)
    _complete_source(page, expected)
    viewer, pane = page.locator(".bp-dag-viewer"), page.locator(".bp-reading-pane")
    handle = pane.locator(":scope > .bp-dag-sheet-handle")
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(handle).to_have_attribute("title", "Expand reading")
    assert "bp-dag-sheet-full" not in viewer.get_attribute("class").split()
    assert pane.evaluate("""pane=>{
      const p=pane.getBoundingClientRect(),body=pane.closest('.bp-dag-body').getBoundingClientRect();
      return Math.abs(p.top-body.top-.45*body.height)<=1&&Math.abs(p.height-body.height)<=1;
    }""")
    legend = page.locator(".bp-map-key-legend")
    summary, popup = legend.locator(":scope > summary"), legend.locator(".bp-map-key-body")
    playwright.expect(legend).to_have_count(1)
    playwright.expect(summary).to_have_text("Link key")
    playwright.expect(legend).not_to_have_attribute("open", "")
    playwright.expect(popup).to_be_hidden()
    playwright.expect(popup.locator("span")).to_have_text(["Statement", "Proof", "Concept links unavailable"])
    # The native summary and the complete pan note must be above, rather than
    # behind, the half reader. The map's actual framing still determines them.
    _assert_hit(page, summary)
    playwright.expect(page.locator(".bp-dag-density")).to_be_visible()
    feedback_geometry = page.locator(".bp-dag-density").evaluate("""note=>{
      const n=note.getBoundingClientRect(),key=document.querySelector('.bp-map-key-legend > summary').getBoundingClientRect();
      const pane=document.querySelector('.bp-reading-pane').getBoundingClientRect();
      const range=document.createRange();range.selectNodeContents(note);
      const rectangle=box=>({left:box.left,right:box.right,top:box.top,bottom:box.bottom,width:box.width,height:box.height});
      const whole=n.width>0&&n.height>0&&n.left>=0&&n.right<=innerWidth&&n.top>=0&&n.bottom<=key.top&&
        key.left>=0&&key.right<=innerWidth&&key.bottom<=pane.top&&
        Array.from(range.getClientRects()).every(box=>box.left>=n.left&&box.right<=n.right&&box.top>=n.top&&box.bottom<=n.bottom);
      const style=getComputedStyle(note),summary=document.querySelector('.bp-map-key-legend > summary'),keyStyle=getComputedStyle(summary);
      return {whole,note:rectangle(n),key:rectangle(key),pane:rectangle(pane),text:note.textContent,
        textRects:Array.from(range.getClientRects()).slice(0,16).map(rectangle),
        font:{family:style.fontFamily,size:style.fontSize,lineHeight:style.lineHeight},
        keyFont:{family:keyStyle.fontFamily,size:keyStyle.fontSize,lineHeight:keyStyle.lineHeight},
        noteBottom:note.style.bottom,keyBottom:document.querySelector('.bp-map-key').style.bottom,
        stage:rectangle(document.querySelector('.bp-dag-stage').getBoundingClientRect()),windowScroll:[scrollX,scrollY]};
    }""")
    if not feedback_geometry["whole"]:
        _native_diagnostic(page, "320-feedback", feedback_geometry)
    assert feedback_geometry["whole"], json.dumps(feedback_geometry, indent=2)
    feedback = page.locator(".bp-dag-density").inner_text()
    assert feedback.startswith("Drag to see more: ") and " cards in view." in feedback
    before = page.locator(".bp-dag-node").evaluate_all(camera)
    history = page.evaluate("history.length")
    scroll = page.locator(".bp-reading-body").evaluate("reader=>reader.scrollTop")
    summary.focus()
    summary.press("Enter")
    playwright.expect(legend).to_have_attribute("open", "")
    playwright.expect(summary).to_be_focused()
    playwright.expect(popup).to_be_visible()
    _assert_hit(page, popup)
    assert popup.evaluate("""body=>{
      const box=body.getBoundingClientRect();
      return box.width>0&&box.height>0&&box.left>=0&&box.right<=innerWidth&&box.top>=0&&box.bottom<=innerHeight&&
        Array.from(body.querySelectorAll('span')).every(node=>{
          const range=document.createRange();range.selectNodeContents(node);
          const rects=Array.from(range.getClientRects()).filter(rect=>rect.width&&rect.height);
          return rects.length&&rects.every(rect=>rect.left>=box.left&&rect.right<=box.right&&rect.top>=box.top&&rect.bottom<=box.bottom&&
            node.contains(document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2)));
        });
    }"""), "Every real typed/count/unavailable caption must be whole and natively reachable outside the summary"
    for caption in popup.locator("span").all():
        _assert_hit(page, caption)
    point = popup.get_by_text("Concept links unavailable", exact=True).bounding_box()
    assert point is not None
    x, y = point["x"] + point["width"] / 2, point["y"] + point["height"] / 2
    page.mouse.move(x, y)
    page.mouse.wheel(37, 43)
    _frames(page)
    assert page.locator(".bp-dag-node").evaluate_all(camera) == before
    page.mouse.down()
    page.mouse.move(x + 8, y + 4, steps=4)
    page.mouse.up()
    _frames(page)
    assert page.locator(".bp-dag-node").evaluate_all(camera) == before
    _assert_hit(page, _selected(page, _TARGET))
    assert page.url == selected and page.evaluate("history.length") == history
    assert page.locator(".bp-reading-body").evaluate("reader=>reader.scrollTop") == scroll
    assert page.locator(".bp-dag-density").inner_text() == feedback
    _complete_source(page, expected)
    summary.focus()
    summary.press("Enter")
    playwright.expect(legend).not_to_have_attribute("open", "")
    playwright.expect(popup).to_be_hidden()
    playwright.expect(summary).to_be_focused()
    page.go_back()
    page.wait_for_url(first)
    _frames(page)
    _complete_source(page, initial_source)
    assert page.locator(".bp-dag-node").evaluate_all(camera) == initial_camera
    assert page.evaluate("document===window.__workspaceDocument") is True
    assert documents == [first.split("#", 1)[0]]


@pytest.mark.parametrize("width,height", [(1600, 900), (1000, 900), (390, 844)])
def test_selected_statement_formal_reference_retains_genuine_title_math_code_and_native_activation(
    workspace_page: object, marked_math_site_url: str, workspace_runtime: _PinnedRuntime,
    width: int, height: int,
) -> None:
    page, base = workspace_page, marked_math_site_url
    page.set_viewport_size({"width": width, "height": height})
    _routes(page, base, workspace_runtime, graph_client=True)
    origin, destination = "topology/result", "topology/later-0"
    first, target = _site_route(page, base, origin), _site_route(page, base, destination)
    record = _site_record(page, base, destination)
    assert record["title"] == r"Later 0 $\ZZ/n$ and `$literal$`"
    expected = _published_source(page, first)
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    page.goto(first)
    _ready_site(page)
    page.evaluate("window.__formalReferenceDocument=document;window.__formalReferenceClicks=0")
    assert page.evaluate("AutoformMath.refresh(document.querySelector('.bp-reading-body'))") is True
    card = _reading_card(page, origin)
    relations = card.locator("details.bp-dependencies")
    playwright.expect(relations).to_have_attribute("open", "")
    playwright.expect(relations.locator("summary")).to_have_text("Uses 1 · Used by 55")
    playwright.expect(relations.locator("a.bp-ref")).to_have_count(56)
    reference = relations.locator(f'a.bp-ref[href="{target}"]')
    playwright.expect(reference).to_have_count(1)
    playwright.expect(reference.locator(".arithmatex")).to_have_count(1)
    playwright.expect(reference.locator(".arithmatex")).to_have_attribute("data-autoform-math-source", r"\(\ZZ/n\)")
    playwright.expect(reference.locator("mjx-container")).to_have_count(1)
    playwright.expect(reference.locator("mjx-c.mjx-c2124")).to_have_count(1)
    assert reference.locator("mjx-c").evaluate_all("""nodes=>nodes.length>=3&&nodes.every(node=>
      !['none','normal',''].includes(getComputedStyle(node,'::before').content))""")
    playwright.expect(reference.locator("code")).to_have_text("$literal$")
    assert reference.locator("code .arithmatex,code mjx-container,[tabindex],[role=application]").count() == 0
    assert reference.locator("mjx-merror,merror,script,iframe,img").count() == 0
    assert page.evaluate("MathJax.version") == "3.2.2"
    _complete_source(page, expected)
    reference.scroll_into_view_if_needed()
    _frames(page)
    _assert_hit(page, reference)
    # The router intentionally captures and stops reader-link clicks before
    # the anchor. Observe the genuine event before that reader boundary.
    reference.evaluate("""node=>{window.__formalReference=node;document.addEventListener('click',event=>{
      if(event.isTrusted&&event.target.closest&&event.target.closest('a.bp-ref')===node&&
        event.button===0&&!event.defaultPrevented&&!event.metaKey&&!event.ctrlKey&&!event.shiftKey&&!event.altKey)
        __formalReferenceClicks++;
    },true);}""")
    reference.focus()
    assert page.evaluate("AutoformMath.refresh(__formalReference)") is True
    playwright.expect(reference).to_be_focused()
    assert reference.evaluate("node=>node===__formalReference&&node.isConnected")
    playwright.expect(reference).to_have_attribute("href", target)
    reference.locator("mjx-container").click(button="right")
    playwright.expect(page.get_by_role("menu")).to_have_count(0)
    assert page.evaluate("__formalReferenceClicks") == 0
    # There is no MathJax menu to dismiss. Escape here is the explorer's
    # real clear-selection action, which would collapse the phone reader.
    reference.focus()
    page.keyboard.press("Enter")
    page.wait_for_url(target)
    _ready_site(page)
    selected = _reading_card(page, destination)
    playwright.expect(selected).to_have_class(re.compile(r"\bbp-reading-current\b"))
    playwright.expect(selected.locator(".bp-thmheading")).to_be_focused()
    assert "Its complete explanatory text remains available after relation navigation." in selected.inner_text()
    _complete_source(page, _published_source(page, target))
    assert page.evaluate("__formalReferenceClicks") == 1
    assert page.evaluate("document===__formalReferenceDocument") is True
    page.go_back()
    page.wait_for_url(first)
    _complete_source(page, expected)
    assert page.evaluate("document===__formalReferenceDocument") is True
    assert documents == [first.split("#", 1)[0]]
