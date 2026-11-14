"""Desktop chapters open with the reader showing the chapter text.

Map-first is only for pages whose reader would just list their children
(tests/ui/test_integrated_workspace_browser.py covers that case). A hub chapter
whose three statements depend on three other chapters, a 14-statement chapter
with one unlinked statement, and a chapter of 121 linked statements, more than
the old explorer could draw, all have text of their own. At 1440x900 each must
open on the whole map with a reader at least 340 px wide and the chapter text
visible, without the overview's collapsed layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.scaffold import scaffold_project
from autoform_cli.ui.render import render_site
from tests.facts_fixture import write_annotated
from tests.ui.test_integrated_explorer_browser import _routes, _served, pinned_runtime, webkit_browser  # noqa: F401


def _article(path: Path, title: str, depends: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_annotated(
        path,
        f"---\ndeclaration: theorem\n---\n\n# {title}\n\nThe statement {title} holds: $0=0$.\n\n## Depends on\n\n"
        + ("\n".join(f"- [{label}]({target})" for label, target in depends) or "No prerequisites.") + "\n",
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
    # Eleven chains of eleven linked statements, one over the old explorer's limit of 120.
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


@pytest.mark.parametrize("chapter", ["hub", "shelf", "big"])
def test_desktop_chapter_opens_with_the_reader(
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
            page.locator(".bp-dag-integrated .bp-whole-map .pm-app").wait_for()
            page.wait_for_load_state("networkidle")
            _frames(page)
            viewer = page.locator(".bp-dag-viewer")
            assert viewer.evaluate("element => element.classList.contains('bp-dag-overview-only')") is False
            reader = page.locator(".bp-reading-pane").bounding_box()
            assert reader is not None and reader["width"] >= 340, reader
            assert page.locator(".bp-reading-body").get_by_text("chapter text is readable here").is_visible()
        finally:
            context.close()
    assert errors == []
