"""Phone Back after closing the reader and opening an unrelated result.

At 390 px the reader closes from its handle. Opening a result whose map shares
no card with the first one, then pressing Back, must return to the first
result's map with its reader still closed and its history entry intact, not
the error line "Could not open this view". The site is the bundled example
project, rendered and built by the real publisher.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.parse import quote, urljoin

import pytest

from autoform_cli.render import render_site
from tests.test_integrated_explorer_browser import _routes, _served, pinned_runtime, webkit_browser  # noqa: F401

_REPO = Path(__file__).resolve().parents[1]
_PHONE = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True, "device_scale_factor": 2}
_VIEWER = "document.querySelector('.bp-dag-viewer.bp-dag-integrated')"
_OPEN = f"() => {_VIEWER}?.classList.contains('bp-dag-sheet-open') === true"
_CLOSED = f"() => {_VIEWER}?.classList.contains('bp-dag-sheet-open') === false"
_STATE = f"""() => {{
  const host = {_VIEWER}, banner = host && host.querySelector('.bp-dag-error');
  const entry = history.state && history.state.autoformNavigation;
  return {{
    readingOpen: Boolean(host && host.classList.contains('bp-dag-sheet-open')),
    error: banner && !banner.hidden ? banner.textContent : '',
    snapshot: Boolean(entry && entry.snapshot),
  }};
}}"""


@pytest.fixture(scope="module")
def example_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("phone-back") / "example"
    shutil.copytree(_REPO / "skills/setup/assets/cabannes-thesis-project", project)
    render_site(project / "blueprint", project / "site-src")
    build(load_config(config_file=str(project / "mkdocs.yml"), strict=False))
    return project / "site"


def _disjoint_results(site: Path) -> tuple[dict, dict]:
    """Two results whose selected-result maps (closed relation neighbourhoods) share no card; A's has at most 24."""
    index = json.loads((site / "dependencies" / "index.json").read_text(encoding="utf-8"))
    nodes = index["nodes"]
    ids = [node["id"] for node in nodes]
    near = {node_id: {node_id} for node_id in ids}
    for row in index.get("relations") or []:
        source = row[0] if isinstance(row[0], str) else ids[row[0]]
        target = row[1] if isinstance(row[1], str) else ids[row[1]]
        near[source].add(target)
        near[target].add(source)
    titles = [node["title"] for node in nodes]
    # Plain, unique titles keep the search step exact.
    results = [node for node in nodes if node.get("result") is True and node.get("route")
               and "$" not in node["title"] and titles.count(node["title"]) == 1]
    best = None
    for a in results:
        if len(near[a["id"]]) > 24:
            continue
        for b in results:
            if a is b or near[a["id"]] & near[b["id"]]:
                continue
            score = (a.get("scope") != b.get("scope"), min(len(near[a["id"]]), 6), min(len(near[b["id"]]), 6))
            if best is None or score > best[0]:
                best = (score, a, b)
    assert best is not None, "the example needs two results with disjoint maps"
    return best[1], best[2]


@pytest.mark.parametrize("entry", ["search", "hash"])
def test_phone_back_after_closing_the_reader_and_opening_an_unrelated_result(
    webkit_browser: object, pinned_runtime: object, example_site: Path, entry: str,  # noqa: F811
) -> None:
    a, b = _disjoint_results(example_site)
    errors: list[str] = []
    with _served(example_site) as url:
        context = webkit_browser.new_context(reduced_motion="reduce", **_PHONE)
        try:
            page = context.new_page()
            page.set_default_timeout(30_000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            _routes(page, url, pinned_runtime, graph_client=True)
            page.goto(urljoin(url + "/dependencies/index.json", a["route"]))
            page.wait_for_function("(title) => document.title === title", arg=a["title"])
            page.wait_for_function(_OPEN)
            page.wait_for_load_state("networkidle")
            # A tap cycles half, full and the peek, so tap until the reader closes.
            handle = page.locator(".bp-dag-sheet-handle")
            for _ in range(2):
                if page.evaluate(_CLOSED):
                    break
                handle.click()
            page.wait_for_function(_CLOSED)
            if entry == "search":
                search = page.locator("input.bp-dag-search")
                search.click()
                search.fill(b["title"])
                page.locator(f'.bp-dag-result[data-result-id="{b["id"]}"]').click()
            else:
                page.evaluate("(hash) => { location.hash = hash; }", "node=" + quote(b["id"], safe=""))
            page.wait_for_function("(title) => document.title === title", arg=b["title"])
            page.wait_for_function(_OPEN)
            page.wait_for_load_state("networkidle")
            page.go_back()
            page.wait_for_function("(title) => document.title === title", arg=a["title"])
            page.wait_for_load_state("networkidle")
            page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 50))))")
            back = page.evaluate(_STATE)
        finally:
            context.close()
    assert errors == []
    assert "Could not open this view" not in back["error"], back
    assert back["readingOpen"] is False, back
    assert back["snapshot"] is True, back
