"""Desktop chapters open with the reader showing the chapter text.

Map-first is only for real overviews: authored-area overviews and scopes
coarsened because they exceed the map's node limit
(tests/test_scoped_overview_browser.py covers the authored-area case). A hub
chapter whose three statements depend on three other chapters gets linked
context cards, and a 14-statement chapter with one unlinked statement gets a
native shelf; neither is an overview. At 1440x900 both must open with a
reader at least 340 px wide and the chapter text visible, without the
overview's collapsed layout or its 0.9 zoom-out floor. Arriving from a map
card focuses the reader's heading. A chapter of 121 linked statements is
coarsened, so it keeps the collapsed reader and the floor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_integrated_explorer_browser import _routes, _served, pinned_runtime, webkit_browser  # noqa: F401


def _article(path: Path, title: str, depends: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ndeclaration: theorem\n---\n\n# {title}\n\nThe statement {title} holds: $0=0$.\n\n## Depends on\n\n"
        + ("\n".join(f"- [{label}]({target})" for label, target in depends) or "No prerequisites.") + "\n",
        encoding="utf-8",
    )


def _chapter(roadmap: Path, slug: str, title: str, items: list[tuple[str, str, list[tuple[str, str]]]]) -> None:
    for name, label, depends in items:
        _article(roadmap / slug / f"{name}.md", label, depends)
    (roadmap / slug / "README.md").write_text(
        f"# {title}\n\nThe complete {title.lower()} chapter text is readable here.\n\n"
        + "\n".join(f"- [{label}]({name}.md)" for name, label, _ in items) + "\n",
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def chapters_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("reader-first") / "project"
    scaffold_project(project, title="Reader first")
    roadmap = project / "blueprint" / "roadmap"
    for slug in ("alpha", "beta", "gamma"):
        _chapter(roadmap, slug, slug.title(), [("x", f"{slug.title()} base", []), ("y", f"{slug.title()} step", [("base", "x.md")])])
    _chapter(roadmap, "hub", "Hub", [
        ("h1", "Hub one", [("Alpha base", "../alpha/x.md")]),
        ("h2", "Hub two", [("Beta base", "../beta/x.md"), ("Hub one", "h1.md")]),
        ("h3", "Hub three", [("Gamma base", "../gamma/x.md"), ("Hub two", "h2.md")]),
    ])
    # A chain of 13 statements and one with no local link.
    shelf = [(f"s{k:02d}", f"Shelf {k}", [(f"Shelf {k - 1}", f"s{k - 1:02d}.md")] if 0 < k < 13 else []) for k in range(14)]
    _chapter(roadmap, "shelf", "Shelf", shelf)
    # Eleven chains of eleven linked statements, one over the map's limit of 120.
    big = [(f"b{k:03d}", f"Big {k}", [(f"Big {k - 1}", f"b{k - 1:03d}.md")] if k % 11 else []) for k in range(121)]
    _chapter(roadmap, "big", "Big", big)
    (roadmap / "README.md").write_text(
        "# Reader first\n\n" + "\n".join(f"- [{slug.title()}]({slug}/README.md)" for slug in ("alpha", "beta", "gamma", "hub", "shelf", "big")) + "\n",
        encoding="utf-8",
    )
    render_site(project / "blueprint", project / "site-src")
    build(load_config(config_file=str(project / "mkdocs.yml"), strict=False))
    return project / "site"


def _frames(page: object) -> None:
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 50))))")


def _scale(page: object) -> float:
    transform = page.locator(".bp-dag-node").first.evaluate("element => element.style.transform")
    return float(re.search(r"scale\(([\d.]+)\)", transform).group(1))


@pytest.mark.parametrize("chapter", ["hub", "shelf"])
def test_desktop_chapter_opens_with_the_reader_and_zooms_out_freely(
    webkit_browser: object, pinned_runtime: object, chapters_site: Path, chapter: str,  # noqa: F811
) -> None:
    errors: list[str] = []
    with _served(chapters_site) as url:
        context = webkit_browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        try:
            page = context.new_page()
            page.set_default_timeout(30_000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            _routes(page, url, pinned_runtime, graph_client=True)
            page.goto(f"{url}/roadmap/{chapter}/index.html")
            page.wait_for_selector(".bp-dag-node", state="visible")
            page.wait_for_load_state("networkidle")
            _frames(page)
            viewer = page.locator(".bp-dag-viewer")
            assert viewer.evaluate("element => element.classList.contains('bp-dag-scope-overview')") is False
            reader = page.locator(".bp-reading-pane").bounding_box()
            assert reader is not None and reader["width"] >= 340, reader
            assert page.locator(".bp-reading-body").get_by_text("chapter text is readable here").is_visible()
            zoom_out = page.get_by_role("button", name="Zoom out", exact=True)
            for _ in range(4):
                zoom_out.click()
                _frames(page)
            scale = _scale(page)
            # The fixture really has the context card or the shelf that once collapsed the reader.
            if chapter == "hub":
                assert page.locator(".bp-dag-node", has_text="Needed here").count() == 1
            else:
                assert page.locator(".bp-scope-shelves").is_visible()
        finally:
            context.close()
    assert errors == []
    assert scale < 0.9 - 1e-6, scale


@pytest.mark.parametrize("chapter", ["hub", "shelf"])
def test_a_map_card_arrival_at_a_reader_first_chapter_focuses_the_reading_heading(
    webkit_browser: object, pinned_runtime: object, chapters_site: Path, chapter: str,  # noqa: F811
) -> None:
    errors: list[str] = []
    with _served(chapters_site) as url:
        context = webkit_browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        try:
            page = context.new_page()
            page.set_default_timeout(30_000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            _routes(page, url, pinned_runtime, graph_client=True)
            page.goto(f"{url}/roadmap/index.html")
            page.wait_for_selector(".bp-dag-node", state="visible")
            page.wait_for_load_state("networkidle")
            page.locator(".bp-dag-node", has_text=chapter.title()).first.click()
            page.wait_for_function("(title) => document.querySelector('.bp-reading-body h1')?.textContent.trim().startsWith(title)", arg=chapter.title())
            # Focus lands after the reader's math pass, so wait up to 10 s for it rather than read it once.
            where = page.evaluate("""async () => {
              const heading = () => document.querySelector('.bp-reading-body h1');
              for (let n = 0; n < 100 && document.activeElement !== heading(); n++) await new Promise(r => setTimeout(r, 100));
              return document.activeElement === heading() ? null : document.activeElement.outerHTML.slice(0, 120);
            }""")
        finally:
            context.close()
    assert errors == []
    assert where is None, where


def test_a_chapter_over_the_map_limit_opens_map_first_with_the_zoom_floor(
    webkit_browser: object, pinned_runtime: object, chapters_site: Path,  # noqa: F811
) -> None:
    errors: list[str] = []
    with _served(chapters_site) as url:
        context = webkit_browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        try:
            page = context.new_page()
            page.set_default_timeout(30_000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            _routes(page, url, pinned_runtime, graph_client=True)
            page.goto(f"{url}/roadmap/big/index.html")
            page.wait_for_selector(".bp-dag-node", state="visible")
            page.wait_for_load_state("networkidle")
            _frames(page)
            viewer = page.locator(".bp-dag-viewer")
            assert viewer.evaluate("element => element.classList.contains('bp-dag-scope-overview')") is True
            reader = page.locator(".bp-reading-pane").bounding_box()
            assert reader is not None and reader["width"] <= 64, reader
            zoom_out = page.get_by_role("button", name="Zoom out", exact=True)
            for _ in range(4):
                zoom_out.click()
                _frames(page)
            scale = _scale(page)
        finally:
            context.close()
    assert errors == []
    assert scale >= 0.9 - 1e-6, scale
