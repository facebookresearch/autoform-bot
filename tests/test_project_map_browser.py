"""The whole-project map page in WebKit, on a site render and MkDocs publish: the walkthrough a reader takes."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest

from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_dag_viewer_browser import _REQUIRED, _page, _served, playwright

_ARTICLE = "---\ndeclaration: {kind}\nstatement: formalized\nlean: {lean}\n---\n\n# {title}\n\n{body}\n\n## Depends on\n\n{uses}\n"


@pytest.fixture(scope="module")
def webkit_browser() -> Iterator[object]:
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.webkit.launch()
        except playwright.Error as error:
            if _REQUIRED or "Executable doesn't exist" not in str(error):
                raise
            pytest.skip("WebKit is not installed")
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def map_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Two chapters: Lagrange's theorem uses a definition beside it, and Abel summation uses Lagrange across chapters."""
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("map-site") / "project"
    scaffold_project(project, title="Map fixture")
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text("# Fixture library\n\n- [Algebra](algebra/README.md)\n- [Analysis](analysis/README.md)\n",
                                       encoding="utf-8")
    articles = {
        "algebra": [("index", "def", "Project.Subgroup.index", "Index of a subgroup", "The index of $H$ in $G$.", "No prerequisites."),
                    ("lagrange", "theorem", "Project.Subgroup.card_dvd", "Lagrange's theorem", "The order of $H$ divides the order of $G$.",
                     "- [Index of a subgroup](index.md)")],
        "analysis": [("abel", "theorem", "Project.abel_summation", r"Abel summation for $\mathbb{R}$", "Summation by parts.",
                      "- [Lagrange's theorem](../algebra/lagrange.md)")],
    }
    for chapter, rows in articles.items():
        folder = roadmap / chapter
        folder.mkdir()
        (folder / "README.md").write_text(f"# {chapter.title()}\n\n" + "".join(f"- [{row[3]}]({row[0]}.md)\n" for row in rows),
                                          encoding="utf-8")
        for name, kind, lean, title, body, uses in rows:
            (folder / f"{name}.md").write_text(_ARTICLE.format(kind=kind, lean=lean, title=title, body=body, uses=uses), encoding="utf-8")
    render_site(project / "blueprint", project / "site-src")
    build(load_config(config_file=str(project / "mkdocs.yml")))
    with _served(project / "site") as url:
        yield url


def _open_map(page: object, url: str) -> None:
    # The CDN's MathJax stays off the network; formulas keep their readable source.
    page.route(lambda requested: not requested.startswith(url), lambda route: route.abort())
    page.goto(f"{url}/dependencies/map/")
    page.locator(".pm-part").first.wait_for()


def _reader_title(page: object) -> str:
    return page.locator(".pm-reader h2").inner_text()


def test_the_map_walkthrough_overview_search_pivot_back_and_whole_project(webkit_browser: object, map_url: str) -> None:
    with _page(webkit_browser, viewport={"width": 1440, "height": 900}, reduced_motion="reduce") as page:
        _open_map(page, map_url)
        # The opening names every region and its size, and the reading pane describes the whole library.
        labels = page.locator(".pm-part").evaluate_all("nodes => nodes.map(node => node.getAttribute('aria-label'))")
        assert "Algebra, 2 results, 1 used elsewhere" in labels and "Analysis, 1 results" in labels
        assert _reader_title(page) == "Fixture library"
        # A Lean name reaches its theorem directly: statement, Lean name, and what it uses.
        search = page.get_by_role("searchbox", name="Search theorems by name or Lean name")
        search.fill("Subgroup.card_dvd")
        playwright.expect(page.locator(".pm-hit")).to_have_count(1)
        search.press("Enter")
        playwright.expect(page.locator(".pm-reader h2")).to_have_text("Lagrange's theorem")
        playwright.expect(page.locator(".pm-lean code")).to_have_text("Project.Subgroup.card_dvd")
        playwright.expect(page.locator(".pm-result.pm-selected")).to_have_count(1)
        before = page.locator(".pm-result.pm-selected").bounding_box()
        # Following a prerequisite pivots the selection; Back restores both the selection and the view.
        page.locator(".pm-reader .pm-list button", has_text="Index of a subgroup").click()
        playwright.expect(page.locator(".pm-reader h2")).to_have_text("Index of a subgroup")
        page.go_back()
        playwright.expect(page.locator(".pm-reader h2")).to_have_text("Lagrange's theorem")
        after = page.locator(".pm-result.pm-selected").bounding_box()
        assert before is not None and after is not None
        assert abs(after["x"] - before["x"]) < 2 and abs(after["y"] - before["y"]) < 2 and abs(after["width"] - before["width"]) < 2
        # The trail names where the theorem lives; the whole library is one action away.
        playwright.expect(page.locator(".pm-trail li")).to_have_text(["Fixture library", "Algebra", "Lagrange's theorem"])
        page.get_by_role("button", name="Whole project").click()
        playwright.expect(page.locator(".pm-reader h2")).to_have_text("Fixture library")
        playwright.expect(page.locator(".pm-trail li")).to_have_text(["Fixture library"])


def test_zooming_out_keeps_the_theorem_and_shows_its_place(webkit_browser: object, map_url: str) -> None:
    with _page(webkit_browser, viewport={"width": 1280, "height": 800}, reduced_motion="reduce") as page:
        _open_map(page, map_url)
        search = page.get_by_role("searchbox", name="Search theorems by name or Lean name")
        search.fill("abel")
        playwright.expect(page.locator(".pm-hit")).to_have_count(1)
        search.press("Enter")
        playwright.expect(page.locator(".pm-reader h2")).to_have_text("Abel summation for \\(\\mathbb{R}\\)")
        page.locator(".pm-stage svg").focus()
        for _ in range(6):
            page.keyboard.press("-")
        # Still selected, and the region that holds it is outlined among the dimmed rest.
        playwright.expect(page.locator(".pm-result.pm-selected")).to_have_count(1)
        playwright.expect(page.locator('.pm-part.pm-on-path[data-part="analysis"]')).to_have_count(1)
        assert page.locator(".pm-reader .pm-kicker").inner_text() == "Result"


def test_one_click_picks_a_region_in_place_and_a_second_opens_it(webkit_browser: object, map_url: str) -> None:
    with _page(webkit_browser, viewport={"width": 1440, "height": 900}, reduced_motion="reduce") as page:
        _open_map(page, map_url)
        # The region's header band, clear of its cards. One click picks it where it is: the pane and an arrow show
        # what uses it, and the camera stays.
        band = page.locator('.pm-part[data-part="algebra"] > rect')
        band.click(position={"x": 24, "y": 10})
        playwright.expect(page.locator(".pm-reader h2")).to_have_text("Algebra")
        playwright.expect(page.locator('.pm-part.pm-picked[data-part="algebra"]')).to_have_count(1)
        playwright.expect(page.locator(".pm-reader .pm-used-by .pm-link-list button")).to_contain_text(["Analysis"])
        playwright.expect(page.locator('.pm-part.pm-used-by[data-part="analysis"]')).to_have_count(1)
        playwright.expect(page.locator(".pm-link.pm-used-by")).to_have_count(1)
        assert not page.url.endswith("#part=algebra")
        page.keyboard.press("Escape")
        playwright.expect(page.locator(".pm-part.pm-picked, .pm-link")).to_have_count(0)
        # Picked again, a second click opens it.
        band.click(position={"x": 24, "y": 10})
        playwright.expect(page.locator('.pm-part.pm-picked[data-part="algebra"]')).to_have_count(1)
        band.click(position={"x": 24, "y": 10})
        playwright.expect(page).to_have_url(re.compile(r"/dependencies/map/#part=algebra$"))
        playwright.expect(page.locator(".pm-reader h2")).to_have_text("Algebra")
        # The region's results are on the same map now, with their titles, and Analysis stays in view below.
        playwright.expect(page.locator('.pm-card[data-result^="algebra/"]')).to_have_count(2)
        playwright.expect(page.locator(".pm-trail li")).to_have_text(["Fixture library", "Algebra"])
