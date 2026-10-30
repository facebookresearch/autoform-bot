"""Stable phone framing controls, using actual CSS, callbacks and a native journey.

The browser fixture states unfinished zero identities. These tests establish
presentation/navigation behavior, not kernel certification.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from tests.test_integrated_dag_layout import _NODE, _ROOT, _run
from tests.test_integrated_workspace_browser import (
    _frames,
    _full_math,
    _open,
    _search,
    navigation_overview_site as navigation_overview_site,
    playwright,
    workspace_browser as workspace_browser,
    workspace_page as workspace_page,
    workspace_runtime as workspace_runtime,
)
from tests.test_qa_scene_framing import _ENVIRONMENT


_CSS = _ROOT / "autoform_cli/assets/blueprint-dag.css"


def _rule(css: str, selector: str) -> dict[str, str]:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match is not None, f"Missing actual CSS rule: {selector}"
    return {
        name.strip(): value.strip()
        for declaration in match[1].split(";") if declaration.strip()
        for name, value in [declaration.split(":", 1)]
    }


def test_phone_fit_target_stays_in_the_existing_group_footprint_and_leaves_desktop_text() -> None:
    css = _CSS.read_text(encoding="utf-8")
    block = re.search(r"@media\s*\(max-width:\s*720px\)\s*\{([\s\S]*?)\n\}", css)
    assert block is not None
    phone = block[1]
    group = _rule(phone, ".bp-dag-integrated .bp-dag-zoom")
    assert group["height"] == "44px"
    assert group["padding"] == group["gap"] == group["border"] == "0"
    assert group["box-shadow"] == "inset 0 0 0 1px var(--dag-border)"
    # Fit inherits the shared touch target before its later, equally specific icon rule.
    target = _rule(phone, ".bp-dag-integrated .bp-dag-zoom .bp-dag-button")
    target.update(_rule(phone, ".bp-dag-integrated .bp-dag-zoom .bp-dag-fit"))
    assert target["width"] == target["min-width"] == target["max-width"] == "44px"
    assert target["height"] == target["min-height"] == "44px"
    assert target["flex"] == "0 0 44px" and target["padding"] == target["font-size"] == "0"
    icon = _rule(phone, ".bp-dag-integrated .bp-dag-fit::before")
    assert icon["content"] == '""' and icon["display"] == "block"
    assert icon["width"] == icon["height"] == "16px"
    assert icon["border"] == "1.5px solid currentColor" and icon["border-radius"] == "50%"
    assert icon["background"].startswith("radial-gradient(") and "url(" not in icon["background"]
    # Neither the visual icon nor hidden text applies to a wide workspace.
    assert "bp-dag-fit" not in css[:block.start()] + css[block.end():]


@pytest.mark.skipif(_NODE is None, reason="Node is needed for actual framing callback checks")
def test_actual_framing_labels_and_tooltips_track_fit_and_readable_crop_without_data_changes() -> None:
    data = {
        "nodes": [{"id": key, "kind": "node", "width": 196, "height": 100} for key in ("A", "B")],
        "edges": [{"source": "A", "target": "B", "statement": 1, "proof": 0}],
    }
    _run(_ENVIRONMENT + r"""
      const before=JSON.stringify(data);fit();
      assert.equal(dom$fit.textContent,'Fit');assert.equal(dom$fit['aria-label'],'Fit the map');
      assert.equal(dom$fit.title,dom$fit['aria-label']);
      dom$stage.clientHeight=100;fit();assert(ui$readableCrop);
      assert.equal(dom$fit.textContent,'Recentre');assert.equal(dom$fit['aria-label'],'Recentre the map');
      assert.equal(dom$fit.title,dom$fit['aria-label']);
      dom$stage.clientHeight=600;fit();assert.equal(ui$readableCrop,false);
      assert.equal(dom$fit.textContent,'Fit');assert.equal(dom$fit.title,'Fit the map');
      assert.equal(JSON.stringify(data),before);
    """, {"data": data, "width": 390, "height": 600, "direction": "lr", "atlas": False})


def _box(locator: object) -> dict:
    box = locator.bounding_box()
    assert box is not None
    return box


def _same_box(actual: dict, expected: dict) -> None:
    for key in ("x", "y", "width", "height"):
        assert actual[key] == pytest.approx(expected[key], abs=.01)


def _capture(page: object, phase: str, details: dict) -> None:
    folder = os.environ.get("AUTOFORM_UI_SCREENSHOT_DIR")
    if not folder:
        return
    destination = Path(folder)
    destination.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(destination / f"phone-framing-{phase}.png"))
    (destination / f"phone-framing-{phase}.json").write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")


@pytest.mark.skipif(playwright is None, reason="Playwright is needed for native framing-control checks")
def test_phone_fit_recentre_round_trip_keeps_native_target_toolbar_stage_and_all_five_areas(
    workspace_page: object, navigation_overview_site: object, workspace_runtime: object,
) -> None:
    page, site = workspace_page, navigation_overview_site
    page.set_viewport_size({"width": 390, "height": 844})
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime)
    page.locator(".bp-dag-overview-only").wait_for()
    fit = page.locator(".bp-dag-fit")
    _capture(page, "initial", {
        "url": page.url, "fit_label": fit.get_attribute("aria-label"), "fit_title": fit.get_attribute("title"),
        "source": page.locator(".bp-reading-body > .bp-reading-source").text_content(), "published_graph": site.root_graph,
        "geometry": page.locator('.bp-dag-node[data-autoform-node-id^="region:"]').evaluate_all("""nodes=>{
          const rectangle=box=>({left:box.left,right:box.right,top:box.top,bottom:box.bottom,width:box.width,height:box.height});
          const stage=document.querySelector('.bp-dag-stage'),bounds=stage.getBoundingClientRect();
          return {stage:rectangle(bounds),client:[stage.clientWidth,stage.clientHeight],
            toolbar:rectangle(document.querySelector('.bp-dag-toolbar').getBoundingClientRect()),
            body:rectangle(document.querySelector('.bp-dag-body').getBoundingClientRect()),
            reading:rectangle(document.querySelector('.bp-reading-pane').getBoundingClientRect()),
            readingOpen:document.querySelector('.bp-dag-sheet-handle').getAttribute('aria-expanded'),
            cards:nodes.slice(0,120).map(node=>{const box=node.getBoundingClientRect(),matrix=new DOMMatrix(getComputedStyle(node).transform);
              const title=node.querySelector('.bp-dag-node-title'),meta=node.querySelector('.bp-dag-node-meta');
              return {id:node.dataset.autoformNodeId,box:rectangle(box),scale:matrix.a,
                camera:[matrix.a,matrix.d,matrix.e,matrix.f],title:title.textContent,meta:meta.textContent,
                titlePixels:parseFloat(getComputedStyle(title).fontSize)*matrix.a,metaPixels:parseFloat(getComputedStyle(meta).fontSize)*matrix.a,
                whole:box.left>=bounds.left+12&&box.right<=bounds.right-12&&box.top>=bounds.top+12&&box.bottom<=bounds.bottom-12,
                hit:node.contains(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2))};})};
        }"""),
    })
    playwright.expect(fit).to_have_attribute("aria-label", "Fit the map")
    playwright.expect(fit).to_have_attribute("title", "Fit the map")
    playwright.expect(fit).to_be_visible()
    initial = {
        "fit": _box(fit), "group": _box(page.locator(".bp-dag-zoom")),
        "toolbar": _box(page.locator(".bp-dag-toolbar")), "stage": _box(page.locator(".bp-dag-stage")),
    }
    assert initial["fit"]["width"] == initial["fit"]["height"] == pytest.approx(44, abs=.01)
    assert initial["group"]["height"] == pytest.approx(44, abs=.01)
    assert fit.evaluate("button => {const b=button.getBoundingClientRect(); return button.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2));}") is True
    icon = fit.evaluate("button => {const style=getComputedStyle(button,'::before');return {width:style.width,height:style.height,content:style.content,font:getComputedStyle(button).fontSize};}")
    assert icon == {"width": "16px", "height": "16px", "content": '""', "font": "0px"}
    controls = page.locator('.bp-dag-node[data-autoform-node-id^="region:"]')
    playwright.expect(controls).to_have_count(5)
    root_source = page.locator(".bp-reading-body > .bp-reading-source").text_content()
    camera = controls.first.evaluate("node => getComputedStyle(node).transform")
    _capture(page, "fit", initial)

    target = "topic-0-1/result"
    _search(page, target, target)
    page.wait_for_url(site.route(target))
    _full_math(page, target, 1)
    playwright.expect(fit).to_have_attribute("aria-label", "Recentre the map")
    playwright.expect(fit).to_have_attribute("title", "Recentre the map")
    _frames(page)
    for key, locator in (("fit", fit), ("group", page.locator(".bp-dag-zoom")),
                         ("toolbar", page.locator(".bp-dag-toolbar")), ("stage", page.locator(".bp-dag-stage"))):
        _same_box(_box(locator), initial[key])
    _capture(page, "recentre", {"target": target, "fit": _box(fit), "stage": _box(page.locator(".bp-dag-stage"))})

    page.go_back()
    page.wait_for_url(site.route("roadmap"))
    playwright.expect(fit).to_have_attribute("aria-label", "Fit the map")
    playwright.expect(page.locator('.bp-reading-pane > .bp-dag-sheet-handle')).to_have_attribute("aria-expanded", "false")
    _frames(page)
    for key, locator in (("fit", fit), ("group", page.locator(".bp-dag-zoom")),
                         ("toolbar", page.locator(".bp-dag-toolbar")), ("stage", page.locator(".bp-dag-stage"))):
        _same_box(_box(locator), initial[key])
    playwright.expect(controls).to_have_count(5)
    assert controls.evaluate_all("""nodes => nodes.every(node => {
      const b=node.getBoundingClientRect(),s=node.closest('.bp-dag-stage').getBoundingClientRect();
      const matrix=new DOMMatrix(getComputedStyle(node).transform);
      return matrix.a>=.9&&b.left>=s.left+12&&b.right<=s.right-12&&b.top>=s.top+12&&b.bottom<=s.bottom-12&&
        node.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2));
    })""") is True
    assert controls.first.evaluate("node => getComputedStyle(node).transform") == camera
    assert page.locator(".bp-reading-body > .bp-reading-source").text_content() == root_source
    assert page.evaluate("() => document === __workspaceDocument") is True and len(documents) == 1
    _capture(page, "back", {"camera": camera, "fit": _box(fit), "stage": _box(page.locator(".bp-dag-stage"))})

    # The same real button remains visibly textual outside the phone rule.
    page.set_viewport_size({"width": 1600, "height": 900})
    playwright.expect(fit).to_have_attribute("aria-label", "Fit the map")
    playwright.expect(fit).to_have_text("Fit")
    assert fit.evaluate("button => {const range=document.createRange();range.selectNodeContents(button);return parseFloat(getComputedStyle(button).fontSize)>=12&&range.getBoundingClientRect().width>0&&getComputedStyle(button,'::before').content==='none';}") is True
