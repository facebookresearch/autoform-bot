"""The whole-project map page in WebKit, on a site render and MkDocs publish: the walkthrough a reader takes."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest

from autoform_cli.scaffold import scaffold_project
from autoform_cli.ui.render import render_site
from tests.ui.test_dag_viewer_browser import _REQUIRED, _page, _paint_frame, _served, playwright

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
                    ("lagrange", "theorem", "Project.Subgroup.card_dvd", "Lagrange's theorem",
                     "The order of $H$ divides the order of $G$.\n\nThe quotient is the index.\n\nSo the order of an element divides it.",
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
    # An article that states nothing, as a library's reading notes are.
    with (roadmap / "algebra/README.md").open("a", encoding="utf-8") as readme:
        readme.write("- [Reading notes](notes.md)\n")
    (roadmap / "algebra/notes.md").write_text("# Reading notes\n\nRead the index before Lagrange's theorem.\n", encoding="utf-8")
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
        # The pane shows the statement from the book's own page, not the index's summary.
        playwright.expect(page.locator(".pm-reader .pm-math")).to_contain_text("divides the order of")
        assert page.locator(".pm-reader .pm-math .bp-thmheading, .pm-reader .pm-math script").count() == 0
        playwright.expect(page.locator(".pm-result.pm-selected")).to_have_count(1)
        before = page.locator(".pm-result.pm-selected").bounding_box()
        # Following a prerequisite pivots the selection; Back restores both the selection and the view.
        page.locator(".pm-reader .pm-list button", has_text="Index of a subgroup").click()
        playwright.expect(page.locator(".pm-reader h2")).to_have_text("Index of a subgroup")
        page.go_back()
        playwright.expect(page.locator(".pm-reader h2")).to_have_text("Lagrange's theorem")
        # The reading pane changes at once, but the map draws on the next animation frame.
        _paint_frame(page)
        after = page.locator(".pm-result.pm-selected").bounding_box()
        assert before is not None and after is not None
        assert abs(after["x"] - before["x"]) < 2 and abs(after["y"] - before["y"]) < 2 and abs(after["width"] - before["width"]) < 2
        # The trail names where the theorem lives; the whole library is one action away.
        playwright.expect(page.locator(".pm-trail li")).to_have_text(["Fixture library", "Algebra", "Lagrange's theorem"])
        page.get_by_role("button", name="Whole project").click()
        playwright.expect(page.locator(".pm-reader h2")).to_have_text("Fixture library")
        playwright.expect(page.locator(".pm-trail li")).to_have_text(["Fixture library"])


def test_back_returns_the_reading_pane_to_where_it_was_scrolled(webkit_browser: object, map_url: str) -> None:
    # A short window, so the pane scrolls. The theorem's statement from its book page is longer than its summary.
    with _page(webkit_browser, viewport={"width": 1440, "height": 400}, reduced_motion="reduce") as page:
        _open_map(page, map_url)
        search = page.get_by_role("searchbox", name="Search theorems by name or Lean name")
        search.fill("Subgroup.card_dvd")
        playwright.expect(page.locator(".pm-hit")).to_have_count(1)
        search.press("Enter")
        playwright.expect(page.locator(".pm-reader .pm-math")).to_contain_text("divides it")
        bottom = page.locator(".pm-reader").evaluate("node => { node.scrollTop = node.scrollHeight; return node.scrollTop; }")
        assert bottom > 0
        # Clicked where it is, as a reader would, without scrolling it into view first.
        page.locator(".pm-reader .pm-list button", has_text="Index of a subgroup").evaluate("button => button.click()")
        playwright.expect(page.locator(".pm-reader h2")).to_have_text("Index of a subgroup")
        page.go_back()
        playwright.expect(page.locator(".pm-reader .pm-math")).to_contain_text("divides it")
        page.wait_for_function("want => document.querySelector('.pm-reader').scrollTop === want", arg=bottom, timeout=5000)


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
        assert page.locator(".pm-reader .pm-kicker").inner_text() == "Theorem"


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
        # One link out to Analysis, with dots flowing along it.
        playwright.expect(page.locator(".pm-link:not(.pm-flowing)")).to_have_count(1)
        playwright.expect(page.locator(".pm-link.pm-flowing")).to_have_count(1)
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


# Each read of the map's data, with the credentials it asked for: the book's router asks for "same-origin", and the map's
# own request(), which only the standalone page uses, for none.
_MAP_DATA_READS = """(() => {
    const native = window.fetch;
    window.mapDataReads = [];
    window.fetch = function (resource, settings) {
        const url = new URL(String(resource && resource.url || resource), location.href);
        if (url.pathname.includes("/dependencies/map/data/")) window.mapDataReads.push([url.pathname, settings && settings.credentials || null]);
        return native.apply(this, arguments);
    };
})();"""


def _open_book(page: object, url: str, path: str) -> None:
    page.route(lambda requested: not requested.startswith(url), lambda route: route.abort())
    page.add_init_script(_MAP_DATA_READS)
    page.goto(f"{url}/{path}")
    page.locator(".bp-whole-map .pm-part").first.wait_for(state="attached")


def _assert_the_map_read_its_data_once_through_the_router(page: object) -> None:
    reads = page.evaluate("() => window.mapDataReads")
    paths = [path for path, _credentials in reads]
    assert paths and len(paths) == len(set(paths)), reads
    assert {credentials for _path, credentials in reads} == {"same-origin"}, reads


def _reading(page: object) -> object:
    return page.locator(".bp-reading-body .bp-reading-current")


def test_the_book_shows_the_map_beside_the_page_and_opens_what_is_picked_on_it(webkit_browser: object, map_url: str) -> None:
    with _page(webkit_browser, viewport={"width": 1440, "height": 900}, reduced_motion="reduce") as page:
        _open_book(page, map_url, "roadmap/algebra/index.html")
        book = page.locator(".bp-dag-viewer")
        # A chapter's page sends the map to its part. The book keeps its own outline, search and reader, so the map
        # shows none of its own.
        playwright.expect(book).to_have_attribute("data-map-start", "algebra")
        playwright.expect(page.locator('.bp-whole-map .pm-part.pm-focus[data-part="algebra"]')).to_have_count(1)
        # Inside a part, the map's general hint would sit on the cards, so the book leaves it to the whole project.
        playwright.expect(page.locator(".bp-whole-map .pm-hint")).to_be_hidden()
        assert page.locator(".bp-whole-map :is(.pm-search, .pm-side, .pm-reader)").evaluate_all(
            "nodes => nodes.length === 3 && nodes.every(node => getComputedStyle(node).display === 'none')")
        # A result picked on the map opens in the reader, and Back returns to the one before.
        page.locator('.bp-whole-map .pm-result[data-result="algebra/lagrange"]').click()
        playwright.expect(_reading(page)).to_have_attribute("data-autoform-node-id", "algebra/lagrange")
        playwright.expect(book).to_have_attribute("data-map-start", "algebra/lagrange")
        page.locator('.bp-whole-map .pm-result[data-result="algebra/index"]').click()
        playwright.expect(_reading(page)).to_have_attribute("data-autoform-node-id", "algebra/index")
        page.go_back()
        playwright.expect(_reading(page)).to_have_attribute("data-autoform-node-id", "algebra/lagrange")
        playwright.expect(page.locator('.bp-whole-map .pm-result.pm-selected[data-result="algebra/lagrange"]')).to_have_count(1)
        # A click between the cards of the part in view keeps the reader where it is and adds no history entry.
        entries = page.evaluate("""() => {
            window.mapPicks = [];
            document.addEventListener('autoform-map-select', event => window.mapPicks.push(event.detail.id), true);
            return history.length;
        }""")
        spot = page.evaluate("""() => {
            const stage = document.querySelector('.bp-whole-map .pm-stage').getBoundingClientRect();
            for (let y = stage.top + 20; y < stage.bottom - 20; y += 16) for (let x = stage.left + 20; x < stage.right - 20; x += 16) {
                const hit = document.elementFromPoint(x, y);
                if (hit && hit.closest('.pm-part[data-part="algebra"]') && !hit.closest('[data-result], .pm-top, .pm-hint')) return [x, y];
            }
            return null;
        }""")
        assert spot is not None
        page.mouse.click(*spot)
        playwright.expect(_reading(page)).to_have_attribute("data-autoform-node-id", "algebra/lagrange")
        playwright.expect(book).to_have_attribute("data-map-start", "algebra/lagrange")
        assert page.evaluate("() => history.length") == entries and page.evaluate("() => window.mapPicks") == []
        # The book owns the address: the map never writes its own history entries inside it.
        assert page.evaluate("() => !(history.state && history.state.pm)")
        # The book's status filter dims the map's results whose status it leaves out.
        page.locator(".bp-dag-filter-summary").click()
        boxes = page.locator(".bp-dag-filter-popover fieldset input[type=checkbox]")
        for index in range(boxes.count()):
            boxes.nth(index).uncheck()
        cards = page.locator(".bp-whole-map .pm-result").count()
        assert cards > 0
        playwright.expect(page.locator(".bp-whole-map .pm-result.pm-dimmed")).to_have_count(cards)
        for index in range(boxes.count()):
            boxes.nth(index).check()
        playwright.expect(page.locator(".bp-whole-map .pm-result.pm-dimmed")).to_have_count(0)
        page.keyboard.press("Escape")
        # The map takes the site's scheme from its toggle, over the system's.
        page.evaluate("() => document.body.setAttribute('data-md-color-scheme', 'slate')")
        assert page.locator(".bp-whole-map .pm-app").evaluate("node => getComputedStyle(node).getPropertyValue('--pm-bg').trim()") == "#16181b"
        _assert_the_map_read_its_data_once_through_the_router(page)


def test_a_page_that_states_nothing_opens_the_map_on_its_part(webkit_browser: object, map_url: str) -> None:
    with _page(webkit_browser, viewport={"width": 1440, "height": 900}, reduced_motion="reduce") as page:
        _open_book(page, map_url, "roadmap/algebra/notes.html")
        playwright.expect(page.locator(".bp-dag-viewer")).to_have_attribute("data-map-start", "algebra/notes")
        # The map has no card for it, so it opens the chapter the notes are in, not the whole library.
        playwright.expect(page.locator('.bp-whole-map .pm-part.pm-focus[data-part="algebra"]')).to_have_count(1)
        # Finding the notes' chapter read the map's search file, through the router like the rest.
        reads = page.evaluate("() => window.mapDataReads")
        assert any(path.endswith("/dependencies/map/data/search.json") for path, _credentials in reads), reads
        _assert_the_map_read_its_data_once_through_the_router(page)


def test_a_part_picked_on_the_map_opens_its_chapter_in_the_reader(webkit_browser: object, map_url: str) -> None:
    with _page(webkit_browser, viewport={"width": 1440, "height": 900}, reduced_motion="reduce") as page:
        _open_book(page, map_url, "index.html")
        book, heading = page.locator(".bp-dag-viewer"), page.locator(".bp-reading-body h1")
        playwright.expect(page.locator(".bp-whole-map .pm-hint")).to_be_visible()
        # Zoomed out, the view is still the whole project and the hint stays, though a zoom lets go of the view's part.
        before = page.locator('.bp-whole-map .pm-part[data-part="algebra"] > rect').get_attribute("width")
        page.locator(".bp-whole-map .pm-stage > svg").focus()
        page.keyboard.press("-")
        page.wait_for_function("""w => document.querySelector('.bp-whole-map .pm-part[data-part="algebra"] > rect').getAttribute('width') !== w""", arg=before)
        playwright.expect(page.locator(".bp-whole-map .pm-hint")).to_be_visible()
        page.keyboard.press("+")
        # One click on Algebra's band picks it where it is, and the reader turns to the chapter's page.
        page.locator('.bp-whole-map .pm-part[data-part="algebra"] > rect').click(position={"x": 24, "y": 10})
        playwright.expect(heading).to_have_text(re.compile(r"^Algebra"))
        playwright.expect(book).to_have_attribute("data-map-start", "algebra")
        playwright.expect(page.locator('.bp-whole-map .pm-part.pm-picked[data-part="algebra"]')).to_have_count(1)
        # Focus doesn't jump to the reader once the page has landed, so a second click or Enter opens the part on the map.
        page.evaluate("() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")
        assert page.evaluate("() => !document.activeElement.closest('.bp-reading-body')")
        page.go_back()
        playwright.expect(heading).not_to_have_text(re.compile(r"^Algebra"))
        playwright.expect(book).to_have_attribute("data-map-start", "roadmap")


def test_the_map_lets_go_of_a_result_when_the_book_does(webkit_browser: object, map_url: str) -> None:
    with _page(webkit_browser, viewport={"width": 1440, "height": 900}, reduced_motion="reduce") as page:
        _open_book(page, map_url, "roadmap/algebra/index.html")
        book, selected = page.locator(".bp-dag-viewer"), page.locator(".bp-whole-map .pm-result.pm-selected")
        page.locator('.bp-whole-map .pm-result[data-result="algebra/lagrange"]').click()
        playwright.expect(selected).to_have_attribute("data-result", "algebra/lagrange")
        # Escape in the reader lets go of the result in the book, and the map lets go of its card.
        page.locator(".bp-reading-body").evaluate("body => { body.tabIndex = -1; body.focus(); }")
        page.keyboard.press("Escape")
        playwright.expect(book).not_to_have_class(re.compile(r"\bbp-dag-has-selection\b"))
        playwright.expect(selected).to_have_count(0)
        # Back to the selection selects the card again.
        page.go_back()
        playwright.expect(book).to_have_class(re.compile(r"\bbp-dag-has-selection\b"))
        playwright.expect(selected).to_have_attribute("data-result", "algebra/lagrange")


def test_the_statement_being_read_lights_up_on_the_map(webkit_browser: object, map_url: str) -> None:
    # A short window, so the chapter's statements scroll past the top of the reader.
    with _page(webkit_browser, viewport={"width": 1440, "height": 420}, reduced_motion="reduce") as page:
        _open_book(page, map_url, "roadmap/algebra/index.html")
        # The first statement scrolls just past the top, so the next one is the one being read.
        page.locator(".bp-reading-body").evaluate("""body => {
            const first = body.querySelector('.bp-thmwrapper[data-autoform-node-id="algebra/index"]');
            body.scrollTop += first.getBoundingClientRect().bottom - body.getBoundingClientRect().top;
        }""")
        playwright.expect(page.locator('.bp-whole-map .pm-result.pm-reading[data-result="algebra/lagrange"]')).to_have_count(1)


def _serve_mathjax(page: object, url: str) -> None:
    """Serve the site from ``url`` and the pinned MathJax in place of the CDN; block everything else."""
    from autoform_cli.ui.mathjax import MATHJAX_BASE
    from tests.ui.test_integrated_explorer_browser import _PinnedRuntime

    runtime = _PinnedRuntime()

    def serve(route: object) -> None:
        target = route.request.url
        if target.startswith(MATHJAX_BASE + "/"):
            runtime.serve(route)
        elif target.startswith(url + "/"):
            route.continue_()
        else:
            route.abort()

    page.route("**/*", serve)


def test_the_map_typesets_a_title_with_a_formula_over_its_card(webkit_browser: object, map_url: str) -> None:
    with _page(webkit_browser, viewport={"width": 1440, "height": 900}, reduced_motion="reduce") as page:
        _serve_mathjax(page, map_url)
        page.goto(f"{map_url}/roadmap/analysis/index.html")
        # Abel summation's card title is set by the site's MathJax, on its card, with no TeX and no SVG copy left.
        title = page.locator(".bp-whole-map .pm-tex.pm-card-title")
        playwright.expect(title.locator("mjx-container")).to_have_count(1)
        playwright.expect(title).to_contain_text("Abel summation for")
        assert "\\(" not in title.inner_text() and "mathbb" not in title.inner_text()
        playwright.expect(page.locator(".bp-whole-map svg text", has_text="Abel summation")).to_have_count(0)
        # The layer is inert, though MathJax makes each formula a tab stop: the card's own label already names it.
        assert title.locator("mjx-container").evaluate("node => { node.focus(); return document.activeElement !== node; }")
        # The title stays on its card when the camera moves.
        for move in (None, 40):
            if move:
                page.locator(".bp-whole-map .pm-stage > svg").hover()
                page.mouse.wheel(0, move)
                page.wait_for_timeout(300)
            card = page.locator('.bp-whole-map .pm-result[data-result="analysis/abel"] > rect').bounding_box()
            box = title.bounding_box()
            assert card["x"] < box["x"] and box["x"] + box["width"] < card["x"] + card["width"], (card, box)
            assert card["y"] < box["y"] and box["y"] + box["height"] < card["y"] + card["height"], (card, box)


def test_the_map_typesets_formulas_in_its_lists(webkit_browser: object, map_url: str) -> None:
    """A list entry is HTML, so the site's MathJax sets its formula, as on the cards; only SVG text reads it as text."""
    with _page(webkit_browser, viewport={"width": 1440, "height": 900}, reduced_motion="reduce") as page:
        _serve_mathjax(page, map_url)
        page.goto(f"{map_url}/dependencies/map/")
        page.locator(".pm-part").first.wait_for()
        search = page.get_by_role("searchbox")
        search.fill("abel")
        hit = page.locator(".pm-hit-title")
        playwright.expect(hit.locator("mjx-container")).to_have_count(1)
        playwright.expect(hit).to_contain_text("Abel summation for")
        assert "\\(" not in hit.inner_text() and "mathbb" not in hit.inner_text()
        # So is an entry in the list of what uses Lagrange's theorem.
        search.fill("Subgroup.card_dvd")
        playwright.expect(page.locator(".pm-hit")).to_have_count(1)
        search.press("Enter")
        used_by = page.locator(".pm-reader .pm-list button", has_text="Abel summation for")
        playwright.expect(used_by.locator("mjx-container")).to_have_count(1)
