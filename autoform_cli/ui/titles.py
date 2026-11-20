"""Titles as the site shows them: the title a page is published under, from its header or first heading,
and a title as HTML, as plain text, or without the kind its caption already shows."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from ..markdown import HEADING as _HEADING
from ..markdown import (
    PAGE_HEADER,
    header_title,
    mask_fences_and_comments,
    page_title,
    published_page,
    rendered_visible_text,
)
from . import site_index, tex_plain


def _titled(published: str, authored: bytes | None, page: Path) -> str:
    """``published``, a page an author wrote, under a header that names its
    title, so MkDocs takes no title from the page itself: the theme prints a
    title as it is, and in ``<title>`` reads a reference back as the
    character it names. The title is the text of the page's H1 in
    ``authored``, as render publishes it when nothing is flagged, so a page
    shown as typed keeps it: the first block's, when that block is an H1, as
    MkDocs would take it, else the first H1 below, else the file's name."""

    text = published_page(authored.decode("utf-8")).removeprefix(PAGE_HEADER) if authored else ""
    later = _first_h1(text) or ""
    # The theme prints the title where nothing typesets it, so a formula reads as text.
    heading = page_title(text, formula=tex_plain.visible) or rendered_visible_text(later, formula=tex_plain.visible)
    return f"---\n{header_title(heading or _file_title(page))}\n---\n" + published.removeprefix(PAGE_HEADER)


def _file_title(source: Path) -> str:
    """The title MkDocs gives a page with no H1, from its file's name."""

    title = ("index" if source.stem == "README" else source.stem).replace("-", " ").replace("_", " ")
    return title.capitalize() if title.lower() == title else title


def _header_title(text: str) -> str | None:
    """The title :func:`_titled` wrote into the header of the page ``text``,
    as the text a reader sees."""

    lines = text.split("\n", 3)
    if len(lines) < 3 or lines[0] != "---" or not lines[1].startswith("title: ") or lines[2] != "---":
        return None
    # JSON writes a character above U+FFFF as a surrogate pair, where the
    # header has YAML's \U escape; a backslash the title holds is doubled.
    value = re.sub(
        r"(?<!\\)((?:\\\\)*)\\U([0-9A-F]{8})",
        lambda escape: escape[1] + json.dumps(chr(int(escape[2], 16)))[1:-1],
        lines[1].removeprefix("title: "),
    )
    return html.unescape(json.loads(value))


def _label_html(text: str) -> str:
    """A title as the book's readingLabel shows it: typeset when the index's parts hold a formula, else as the index
    ships it, with a Markdown escape read as the page reads it."""
    return html.escape(site_index._shown_title(text)) if site_index._reading_label(text) is None else _title_html(text)


#: A heading element's tag.
_HEADING_TAG = re.compile(r"h[1-6]")


def _page_heading(text: str) -> str | None:
    """The title the page ``text`` is published under, as text: its header's,
    which a page shown as typed keeps, or else its first H1."""

    return _header_title(text) or _first_h1(text)


def _first_h1(text: str) -> str | None:
    for line in mask_fences_and_comments(text.splitlines()):
        heading = _HEADING.match(line)
        if heading is not None and len(heading.group(1)) == 1:
            return heading.group(2).strip()
    return None


def _title_html(text: str, *, fallback: str | None = None) -> str:
    """A title as HTML whose TeX and code the site typesets, not as the source's dollar signs.

    Only converter-validated text, code and math enter the page; authored HTML,
    destinations and attributes stay literal. A title the converter leaves alone
    shows ``fallback`` as text when given: a page header's visible title, whose
    emphasis and links read as the page's own heading does.
    """
    reading = site_index._reading_label(text, require_math=False)
    if reading is None:
        return html.escape(site_index._shown_title(text) if fallback is None else fallback)
    return "".join(
        html.escape(value) if kind == "text" else
        f"<code>{html.escape(value)}</code>" if kind == "code" else
        f'<span class="arithmatex">{html.escape(value)}</span>'
        for kind, value in reading["parts"]
    )


def _plain_title(text: str) -> str:
    """A title as the text an accessible name holds, read as the explorer's plainTitle reads it: with the parts the
    search index records for a title with a formula, code without its backticks and each formula as :mod:`tex_plain`
    reads it; otherwise the title as the index ships it, a Markdown escape read as its heading reads it, with only
    formulas in ``\\( \\)`` read, so a literal ``$`` stays."""
    reading = site_index._reading_label(text)
    if reading is None:
        return tex_plain.visible(site_index._shown_title(text))
    return "".join(tex_plain.math(value[2:-2]) if kind == "math" else value for kind, value in reading["parts"])


def _display_title(title: str, caption: str) -> str:
    """Drop a kind the caption already shows: 'Theorem (X)' under 'Theorem' reads X.

    Display only; the graph keeps the authored title. A title that is just the
    kind word, or empty, gives "", so the caption alone names the statement, and
    the outer parentheses are only removed when they enclose the whole rest of
    the title.
    """
    if title.casefold() == caption.casefold():
        return ""
    prefix = f"{caption} ("
    if not (title.casefold().startswith(prefix.casefold()) and title.endswith(")")):
        return title
    inner, depth = title[len(prefix) : -1], 0
    for character in inner:
        depth += (character == "(") - (character == ")")
        if depth < 0:
            return title
    return inner if depth == 0 else title
