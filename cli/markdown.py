"""Shared Markdown primitives for the deterministic blueprint checks.

The audit, the coverage contract, and the renderer must agree on what counts as
published Markdown. When each one carried its own regular expressions they
disagreed in ways that failed open: a table hidden inside an HTML comment was
treated as authoritative, and a link missing its closing parenthesis satisfied a
check even though it never renders as a link. This module is the single place
those rules live.

Two ideas run through everything here:

* Only *visible* Markdown carries meaning. Fenced code blocks, indented code
  blocks, and HTML comments are documentation about a contract, never the
  contract itself.
* Line numbers are part of the diagnostic. Masking never changes how many lines
  a document has, so a caller can always report the author's own line number.

Where a rule has to predict what a reader sees, it follows the configured
renderer rather than an approximation of it. Anchor generation in particular
reproduces Python-Markdown's ``toc`` slugging and unique-ID behaviour, because a
checker that guesses at anchors both rejects valid fragments and accepts
fragments that never appear on the published page. ``tests/test_markdown.py``
holds a differential test that compares this module against Python-Markdown
itself, so the two cannot drift apart unnoticed.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

import html5lib
import markdown as pymarkdown
from pymdownx.superfences import fence_div_format

#: The Markdown extensions the generated `mkdocs.yml` enables, and their
#: settings. Anchor prediction builds a real converter from these, so the site's
#: configuration and the checker's idea of it cannot be two different things.
#: `tests/test_markdown.py` asserts this matches the shipped template.
SITE_EXTENSIONS: tuple[str, ...] = (
    "attr_list",
    "toc",
    "md_in_html",
    "tables",
    "pymdownx.arithmatex",
    "pymdownx.superfences",
)
SITE_EXTENSION_CONFIGS: dict[str, dict[str, object]] = {
    "toc": {"toc_depth": "2-3"},
    "pymdownx.arithmatex": {"generic": True},
    "pymdownx.superfences": {
        "custom_fences": [
            {"name": "mermaid", "class": "mermaid", "format": fence_div_format},
        ]
    },
}

#: A published heading's ID, read back out of the rendered HTML.
_HEADING_ID = re.compile(r"<h[1-6][^>]*\bid=\"([^\"]*)\"", re.IGNORECASE)

#: Link schemes that are resolved by the reader's browser, not by this checker.
EXTERNAL_SCHEMES = frozenset({"http", "https"})

HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+).*?\1")
HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)

#: A closing fence carries nothing but its marker. ``pymdownx.superfences`` keeps
#: ````` trailing`` inside the code block, so treating it as a closer would expose
#: content that is still fenced when the page renders.
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")

#: A complete inline link. The closing parenthesis is required: a target such as
#: ``[Node](../roadmap/node.md`` renders as literal text, so accepting it would
#: let unrendered evidence satisfy a coverage disposition.
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")

#: An unordered or ordered list marker. Content indented under a list item is a
#: continuation of that item, not an indented code block.
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")

#: CommonMark's indentation threshold for an indented code block.
_CODE_INDENT = 4

#: Elements whose contents a browser never shows the reader. Text inside these
#: is not evidence of anything.
_NON_VISIBLE_TAGS = frozenset({"script", "style", "template", "noscript", "head", "title"})

#: Runs of whitespace, which HTML collapses when it draws them.
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Content:
    """A line-preserving view of the publishable Markdown in a document.

    ``lines`` holds one entry per source line with unpublished spans blanked.
    ``hidden`` holds the indexes of lines that belong to an unpublished
    construct: a fenced block, an indented code block, or an HTML comment,
    *including the blank lines inside them*.

    That last detail is what makes the view safe to scan. A caller that ends a
    construct at a blank line -- a table body, say -- needs to distinguish a
    blank the author typed from a blank that merely sits inside a comment. Treat
    them alike and a comment containing an empty line silently swallows every
    row beneath it.
    """

    lines: tuple[str, ...]
    hidden: frozenset[int]

    def is_hidden(self, index: int) -> bool:
        """Whether line ``index`` belongs to an unpublished construct."""

        return index in self.hidden

    def ends_block(self, index: int) -> bool:
        """Whether line ``index`` is a blank line the author actually typed.

        Only these end a table or a paragraph. A blank line inside a comment or
        a fence is part of that construct and carries on through it.
        """

        return not self.lines[index].strip() and index not in self.hidden


def strip_line_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Remove HTML comment spans from one line, carrying state across lines.

    Returns the visible remainder of ``line`` and whether a comment is still
    open when the line ends. Text on the same line as a comment's start or end
    is preserved, so ``| OUT | real <!-- aside --> |`` keeps its cell layout.
    """

    output: list[str] = []
    index = 0
    while index < len(line):
        if in_comment:
            end = line.find("-->", index)
            if end < 0:
                return "".join(output), True
            index = end + 3
            in_comment = False
            continue
        start = line.find("<!--", index)
        if start < 0:
            output.append(line[index:])
            break
        output.append(line[index:start])
        index = start + 4
        in_comment = True
    return "".join(output), in_comment


def content(text: str) -> Content:
    """Return the publishable view of ``text``, recording what is hidden.

    Fenced code blocks, indented code blocks, and HTML comments are blanked.
    The result always has exactly as many lines as ``text``, so index ``i``
    still describes line ``i + 1`` of the source document.
    """

    lines = text.splitlines()
    hidden: set[int] = set()
    masked = _mask_fences_and_comments(lines, hidden)
    masked = _mask_indented_code(masked, hidden)
    return Content(tuple(masked), frozenset(hidden))


def content_lines(text: str) -> list[str]:
    """Return ``text`` line by line with everything unpublished masked out."""

    return list(content(text).lines)


def link_targets(value: str) -> tuple[str, ...]:
    """Return the targets of every complete inline link in ``value``.

    Inline code is ignored, so a link shown as an example inside backticks does
    not count as a real reference. Angle-bracket targets are unwrapped.
    """

    return tuple(
        _unwrap_target(match.group(1)) for match in LINK.finditer(INLINE_CODE.sub("", value))
    )


def markdown_links(text: str) -> list[tuple[int, str]]:
    """Return ``(line number, target)`` for every visible link in ``text``."""

    links: list[tuple[int, str]] = []
    for line_number, line in enumerate(content_lines(text), start=1):
        links.extend((line_number, target) for target in link_targets(line))
    return links


@dataclass(frozen=True, slots=True)
class PublishedTable:
    """A table as the site publishes it, in the text a reader sees."""

    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@lru_cache(maxsize=1)
def _converter() -> pymarkdown.Markdown:
    """One converter, reused. Heading IDs are scoped per run, so this is safe."""

    return site_converter()


def render_html(text: str) -> str:
    """Render ``text`` exactly as the generated site would."""

    converter = _converter()
    converter.reset()
    return converter.convert(text)


def render_tree(text: str) -> object | None:
    """Render ``text`` and parse the result the way a browser would.

    HTML5 tree construction is the point, not tokenising. Browsers repair
    malformed markup rather than reject it, and the repair is what decides what a
    reader ends up seeing: ``<span hidden />`` opens an element that stays open,
    because self-closing syntax does not apply to non-void elements, while a
    second ``<p>`` implicitly closes the first. A tokeniser sees neither, so it
    gets both the hiding and the showing wrong.
    """

    try:
        rendered = render_html(text)
    except Exception:
        return None
    try:
        return html5lib.parse(rendered, treebuilder="etree", namespaceHTMLElements=False)
    except Exception:
        return None


def rendered_visible_text(value: str) -> str:
    """Return the text a reader sees once ``value`` is published.

    Deciding whether a fragment of Markdown *says* anything means looking at what
    it renders to, not at its source: a URL is not prose, a tag is not evidence,
    and text a browser hides is not either.
    """

    tree = render_tree(value)
    if tree is None:
        # Content the renderer cannot process shows the reader nothing we can
        # vouch for, so report no visible text rather than guess.
        return ""
    return _collapse("".join(_visible_parts(tree, hidden=False)))


def published_tables(text: str) -> list[PublishedTable]:
    """Return every table ``text`` publishes, in the text a reader sees.

    Whether a table renders at all depends on its surroundings, not only on its
    own two structural lines: a paragraph directly above the header makes the
    whole thing one lazy paragraph instead. Rows come back alongside the headers
    so a caller can tell *which* published table its source lines became, rather
    than trusting that a matching header somewhere on the page is the same table.

    Only tables a reader can actually see are returned. Hiding propagates down
    from ancestors, so a table inside ``<div hidden>`` counts no more than one
    carrying ``hidden`` itself, and a hidden row inside a visible table drops out
    while its siblings remain.
    """

    tree = render_tree(text)
    if tree is None:
        return []
    tables: list[PublishedTable] = []
    _collect_tables(tree, hidden=False, tables=tables)
    return tables


def _collect_tables(element: object, hidden: bool, tables: list[PublishedTable]) -> None:
    if not isinstance(element.tag, str):
        return
    concealed = _conceals(element, hidden)
    if _local_name(element) == "table":
        if not concealed:
            tables.append(_read_table(element))
        # Recurse regardless: a nested table inherits this one's visibility.
    for child in element:
        _collect_tables(child, concealed, tables)


def _read_table(element: object) -> PublishedTable:
    headers: tuple[str, ...] = ()
    rows: list[tuple[str, ...]] = []
    for row in _visible_rows(element, hidden=False):
        # A concealed cell is not an empty column, it is no column at all. Keeping
        # it as an empty string invents a phantom column, which both hides a
        # table whose visible headers match and invents mismatches in one whose
        # rows do.
        cells = [
            child
            for child in row
            if isinstance(child.tag, str) and not _conceals(child, hidden=False)
        ]
        heading_cells = tuple(_cell_text(cell) for cell in cells if _local_name(cell) == "th")
        body_cells = tuple(_cell_text(cell) for cell in cells if _local_name(cell) == "td")
        if heading_cells and not headers:
            headers = heading_cells
        elif body_cells:
            rows.append(body_cells)
    return PublishedTable(headers, tuple(rows))


def _visible_rows(element: object, hidden: bool) -> list[object]:
    """Return the rows of one table that a reader can see, skipping nested ones."""

    rows: list[object] = []
    for child in element:
        if not isinstance(child.tag, str):
            continue
        tag = _local_name(child)
        if tag == "table":
            # A nested table's rows belong to it, not to this one.
            continue
        concealed = _conceals(child, hidden)
        if tag == "tr":
            if not concealed:
                rows.append(child)
            continue
        rows.extend(_visible_rows(child, concealed))
    return rows


def _conceals(element: object, hidden: bool) -> bool:
    """Whether ``element`` and its contents are kept from the reader."""

    return hidden or _local_name(element) in _NON_VISIBLE_TAGS or "hidden" in element.attrib


def _local_name(element: object) -> str:
    return str(element.tag).rsplit("}", 1)[-1]


def _cell_text(cell: object) -> str:
    return _collapse("".join(_visible_parts(cell, hidden=False)))


def _collapse(text: str) -> str:
    """Collapse whitespace the way HTML does when it draws text."""

    return _WHITESPACE.sub(" ", text).strip()


def _visible_parts(element: object, hidden: bool) -> list[str]:
    """Walk a parsed tree, collecting only the text a browser would draw."""

    if not isinstance(element.tag, str):
        # A comment or processing instruction. Its text is markup, not content,
        # and a reader never sees it. Any tail text belongs to the parent, which
        # collects it below.
        return []
    concealed = _conceals(element, hidden)
    parts: list[str] = []
    if not concealed and element.text:
        parts.append(element.text)
    for child in element:
        parts.extend(_visible_parts(child, concealed))
        # Tail text sits in this element, not the child, so it is hidden only
        # when this element is.
        if not concealed and child.tail:
            parts.append(child.tail)
    return parts


def frontmatter_end(lines: list[str]) -> int:
    """Return the index of the first line after any YAML frontmatter block."""

    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index + 1
    return len(lines)


def site_converter() -> pymarkdown.Markdown:
    """Return a converter configured exactly as the generated site is."""

    return pymarkdown.Markdown(
        extensions=list(SITE_EXTENSIONS),
        extension_configs=SITE_EXTENSION_CONFIGS,
    )


def markdown_anchors(path: Path) -> set[str]:
    """Return the heading anchors MkDocs will publish for ``path``.

    The anchors come from running the configured renderer and reading the IDs
    back out of its HTML, rather than from predicting what it would do. Heading
    IDs depend on far more than the heading line: whether the heading sits in a
    blockquote or a list item, whether a raw HTML block swallows it, how
    ``attr_list`` treats an escaped brace, and what ``arithmatex`` leaves behind
    for the slugger. Every approximation of that got some of them wrong in both
    directions, rejecting fragments that resolve and accepting fragments absent
    from the page.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()
    lines = text.splitlines()
    # MkDocs strips YAML frontmatter before Markdown ever sees it, so those
    # lines cannot contribute headings. Caching by this exact content observes
    # edits immediately without relying on filesystem timestamp resolution.
    body = "\n".join(lines[frontmatter_end(lines) :])
    return set(_anchors_from_body(body))


@lru_cache(maxsize=128)
def _anchors_from_body(body: str) -> frozenset[str]:
    try:
        rendered = render_html(body)
    except Exception:
        # A document the renderer cannot process publishes no anchors we can
        # promise, so report none rather than guess at them.
        return frozenset()
    return frozenset(html.unescape(found) for found in _HEADING_ID.findall(rendered))


def local_target_issue(
    source_path: Path,
    target: str,
    boundary: Path,
    *,
    label: str,
) -> tuple[str, str] | None:
    """Return ``(code, reason)`` when a local link does not resolve.

    ``source_path`` is the file containing the link, ``boundary`` the directory
    the link may not escape. External schemes are the reader's problem and are
    reported as fine. A fragment on a Markdown target must name a real heading.
    """

    split = urlsplit(target)
    scheme = split.scheme.casefold()
    if scheme in EXTERNAL_SCHEMES:
        return None
    if scheme:
        return f"unsupported-{label}-link", f"{label} link uses unsupported scheme: {target!r}"
    if split.netloc:
        return f"unsupported-{label}-link", f"{label} link uses a network location: {target!r}"

    raw_path = unquote(split.path)
    if "\x00" in raw_path:
        return f"malformed-{label}-link", f"{label} link contains an invalid path: {target!r}"
    if not raw_path:
        candidate = source_path.resolve()
    else:
        relative = Path(raw_path)
        if relative.is_absolute():
            return f"{label}-escapes-blueprint", f"{label} link escapes the blueprint: {target!r}"
        candidate = (source_path.parent / relative).resolve()

    boundary = boundary.resolve()
    if not _is_within(candidate, boundary):
        return f"{label}-escapes-blueprint", f"{label} link escapes the blueprint: {target!r}"
    try:
        is_file = candidate.is_file()
    except (OSError, ValueError):
        return f"malformed-{label}-link", f"{label} link contains an invalid path: {target!r}"
    if not is_file:
        return f"{label}-not-found", f"{label} link does not resolve to a file: {target!r}"
    if split.fragment and candidate.suffix.casefold() == ".md":
        fragment = unquote(split.fragment)
        if fragment not in markdown_anchors(candidate):
            return f"{label}-anchor-not-found", f"{label} link fragment does not resolve: {target!r}"
    return None


def _mask_fences_and_comments(lines: list[str], hidden: set[int]) -> list[str]:
    masked: list[str] = []
    fence: tuple[str, int] | None = None
    in_comment = False
    for index, raw in enumerate(lines):
        if fence is not None:
            # A fence closes on the raw line: `<!--` inside a code block is
            # literal text, not the start of a comment. The closing delimiter
            # belongs to the block, so it is hidden along with the body.
            match = FENCE_CLOSE.match(raw)
            if match is not None:
                marker = match.group(1)
                if marker[0] == fence[0] and len(marker) >= fence[1]:
                    fence = None
            hidden.add(index)
            masked.append("")
            continue
        opened_in_comment = in_comment
        line, in_comment = strip_line_comments(raw, in_comment)
        match = FENCE.match(line)
        if match is not None:
            marker = match.group(1)
            fence = (marker[0], len(marker))
            hidden.add(index)
            masked.append("")
            continue
        # A line shows nothing either because the author left it empty or
        # because a comment covers it. Only the second belongs to a construct.
        if not line.strip() and (opened_in_comment or raw.strip()):
            hidden.add(index)
        masked.append(line)
    return masked


def _mask_indented_code(lines: list[str], hidden: set[int]) -> list[str]:
    masked = list(lines)
    in_list = False
    index = 0
    while index < len(masked):
        line = masked[index]
        if not line.strip():
            index += 1
            continue
        if _indent(line) < _CODE_INDENT:
            # This line sets the context every following indented line is read
            # against, and it stays set across the blank lines that separate a
            # list item from its continuation paragraphs.
            in_list = _LIST_ITEM.match(line) is not None
            index += 1
            continue
        # Indented content is a code block only outside a list, and only where
        # it does not continue the paragraph directly above it.
        if in_list or (index > 0 and masked[index - 1].strip()):
            index += 1
            continue
        end = index
        while end < len(masked) and (
            not masked[end].strip() or _indent(masked[end]) >= _CODE_INDENT
        ):
            end += 1
        # Blank lines trailing the block separate it from whatever follows, so
        # they end a table or paragraph as any other blank line does.
        body = end
        while body > index and not masked[body - 1].strip():
            body -= 1
        for inside in range(index, body):
            hidden.add(inside)
            masked[inside] = ""
        index = end
    return masked


def _indent(line: str) -> int:
    expanded = line.expandtabs(_CODE_INDENT)
    return len(expanded) - len(expanded.lstrip(" "))


def _unwrap_target(target: str) -> str:
    if target.startswith("<") and target.endswith(">"):
        return target[1:-1]
    return target


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


__all__ = [
    "EXTERNAL_SCHEMES",
    "FENCE",
    "FENCE_CLOSE",
    "HEADING",
    "HTML_COMMENT",
    "INLINE_CODE",
    "LINK",
    "SITE_EXTENSIONS",
    "SITE_EXTENSION_CONFIGS",
    "Content",
    "content",
    "content_lines",
    "frontmatter_end",
    "link_targets",
    "local_target_issue",
    "markdown_anchors",
    "PublishedTable",
    "markdown_links",
    "published_tables",
    "render_html",
    "render_tree",
    "rendered_visible_text",
    "site_converter",
    "strip_line_comments",
]
