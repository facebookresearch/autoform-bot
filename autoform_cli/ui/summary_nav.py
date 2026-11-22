"""The site's navigation: SUMMARY.md with the book's chapters, the published source notes in it, and the
structure page."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from .._fs import is_within as _is_within
from .._fs import relative as _relative
from ..graph import SOURCES_DIR
from ..markdown import name_text, relative_link, title_text
from .page_links import _bracketed
from .titles import _file_title, _first_h1, _header_title


def _render_structure_page(
    *,
    page: Path,
    entry: Path | None = None,
) -> str:
    """Keep the old structure route as a bounded doorway into the book.

    The explorer owns hierarchy now. Republishing every source path as one raw
    HTML tree made this compatibility page larger than the rest of a small
    site, duplicated the explorer, and became unusable on repository-scale books.
    """
    target = entry or page.parent / "dependencies.md"
    explorer = relative_link(target, page, ".md")
    text = target.read_text(encoding="utf-8") if target.is_file() else ""
    # A header title is visible text already, while an H1 is Markdown, as in
    # the site's nav.
    named = _header_title(text)
    title = name_text(named) if named else title_text(_first_h1(text) or "")
    return "\n".join(
        [
            "---",
            "title: This page moved",
            "search:",
            "  exclude: true",
            "hide:",
            "  - toc",
            "---",
            "",
            "# This page moved",
            "",
            f"The book starts at [{title or 'its contents'}]({explorer}).",
            "",
        ]
    )


def _render_summary_nav(
    book_pages: list[Path],
    *,
    destination: Path,
    overview: Path,
    source_pages: Sequence[Path] = (),
) -> str:
    """Write one mathematical site nav with the book's real chapters.

    mkdocs.yml cannot know a project's chapters, and hand-listing them would be
    the second navigation manifest the roadmap skill forbids. mkdocs-literate-nav
    reads this file instead, so the tabs come from the same page order the book
    itself uses.

    A chapter's README nests under the nearest README above its folder, any
    other page under the nearest README in its own folder or above, and the
    tree is written depth first in book order. The book order walks the
    containment tree (:func:`_reading_walk`), so a page always comes after
    its chapter. Nesting by folder rather than by depth alone keeps each
    level one step below its parent, which literate-nav requires. The source
    notes, *source_pages*, follow the book in a section of their own.
    """
    # SUMMARY.md is a navigation manifest, not a searchable content page. A
    # repository-wide book can make this one generated page larger than every
    # article, so exclude it before Material constructs its transient index.
    lines = [
        "---",
        "search:",
        "  exclude: true",
        "---",
        "",
        f"- [Home]({overview.relative_to(destination).as_posix()})",
        "- Mathematics",
    ]
    pages = [page for page in book_pages if page != overview]
    folder_pages = {page.parent: page for page in pages if page.name.casefold() == "readme.md"}
    children: dict[Path | None, list[Path]] = {}
    for page in pages:
        folders = page.parent.parents if folder_pages.get(page.parent) == page else [page.parent, *page.parent.parents]
        parent = next((folder_pages[folder] for folder in folders if folder in folder_pages), None)
        children.setdefault(parent, []).append(page)
    stack = [(page, 1) for page in reversed(children.get(None, []))]
    while stack:
        page, depth = stack.pop()
        text = page.read_text(encoding="utf-8")
        named = _header_title(text)
        title = name_text(named) if named else title_text(_first_h1(text) or page.parent.name)
        lines.append(f"{'    ' * depth}- [{title}]({_relative(page, destination).as_posix()})")
        stack.extend((child, depth + 1) for child in reversed(children.get(page, [])))
    # What counts as done belongs beside the book it qualifies. The landing
    # page used to be the only route to it, which made the page impossible to
    # simplify without stranding it.
    # Last inside the book: navigation.indexes takes a section's first README
    # page as the page its heading opens, which made Mathematics open the
    # coverage page, and a section of its own showed an empty pane on a narrow
    # screen.
    depth = 2 if children.get(None) else 1
    coverage = destination / "coverage/README.md"
    if coverage.is_file():
        # Named by its title, as a chapter is; "Coverage" when only its file names it.
        named = _header_title(coverage.read_text(encoding="utf-8"))
        title = name_text(named) if named and named != _file_title(coverage) else "Coverage"
        lines.append(f"{'    ' * depth}- [{title}](coverage/README.md)")
    lines += _source_nav([page for page in source_pages if page.is_file()], destination=destination, depth=depth)
    return "\n".join(lines) + "\n"


def _source_nav(notes: list[Path], *, destination: Path, depth: int) -> list[str]:
    """The nav entries for the published source *notes*, at *depth*: the
    folder's README heading the rest, else a section of their own, named as
    the coverage entry is. A subfolder's README heads its folder's notes, as a
    chapter's README heads its pages."""

    if not notes:
        return []
    folder = destination / SOURCES_DIR
    heads = {page.parent: page for page in notes if page.name.casefold() == "readme.md"}
    top = heads.get(folder)
    children: dict[Path | None, list[Path]] = {}
    for page in sorted(notes):
        if page == top:
            continue
        start = page.parent.parent if heads.get(page.parent) == page else page.parent
        above = [start, *(parent for parent in start.parents if _is_within(parent, folder))]
        parent = next((heads[path] for path in above if path in heads and heads[path] != page), None)
        children.setdefault(None if parent == top else parent, []).append(page)

    def title(page: Path) -> str:
        text = page.read_text(encoding="utf-8")
        named = _header_title(text)
        if page.name.casefold() == "readme.md" and named in (None, _file_title(page)):
            # Named by its folder when only its file names it, as Coverage is "Coverage".
            return name_text(page.parent.name)
        return name_text(named) if named else title_text(_first_h1(text) or page.stem)

    def href(page: Path) -> str:
        return _bracketed(_relative(page, destination).as_posix())

    named = _header_title(top.read_text(encoding="utf-8")) if top else None
    label = name_text(named) if top and named and named != _file_title(top) else "Sources"
    lines = [f"{'    ' * depth}- [{label}]({href(top)})" if top else f"{'    ' * depth}- {label}"]
    stack = [(page, depth + 1) for page in reversed(children.get(None, []))]
    while stack:
        page, level = stack.pop()
        entry = f"[{title(page)}]({href(page)})"
        # MkDocs takes a page named README or index for its section's own page,
        # and navigation.indexes makes a heading with no page open the first
        # such child, so one directly under it gets a heading of its own.
        if top is None and level == depth + 1 and page.stem in {"README", "index"} and not children.get(page):
            lines += [f"{'    ' * level}- {_bare_title(title(page))}", f"{'    ' * (level + 1)}- {entry}"]
            continue
        lines.append(f"{'    ' * level}- {entry}")
        stack.extend((child, level + 1) for child in reversed(children.get(page, [])))
    return lines


def _bare_title(title: str) -> str:
    """*title*, written as :func:`~autoform_cli.markdown.name_text` writes it,
    as the title of a nav section with no page. literate-nav reads the line as
    a Markdown list item, so a marker it starts with that would open a list or
    a heading, as in ``1. Background``, is written as a reference too."""

    return re.sub(r"^(\d*)([.+#-])", lambda m: m[0] if m[0] == "." else f"{m[1]}&#{ord(m[2])};", title)
