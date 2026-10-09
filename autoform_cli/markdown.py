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

#: Words that name the absence of a decision.
_PLACEHOLDER_WORDS = frozenset({"pending", "placeholder", "todo", "tbd", "unknown"})
#: Punctuation that turns a leading placeholder into a marker, as in ``TODO:``.
#: A single hyphen needs space after it, so ``Unknown-variance`` stays a word.
_MARKER_PUNCTUATION = re.compile(r"^\s*(?:[:\u2014]|--|[-\u2013]\s)")
#: Elements that label or illustrate prose without being prose.
_NON_PROSE_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "pre"})


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
    try:
        return converter.convert(text)
    except Exception:
        # A conversion that fails midway leaves the converter unable to render
        # the next document, and ``reset`` does not repair it.
        _converter.cache_clear()
        raise


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


def visible_prose(value: str) -> str:
    """Return the prose a reader sees once ``value`` is published.

    Headings, code blocks and diagrams are left out: a heading names what
    follows and a block of code illustrates it, but neither says it. Inline
    code stays, because a sentence may name a declaration.
    """

    tree = render_tree(value)
    if tree is None:
        return ""
    for element in tree.iter():
        if _local_name(element) in _NON_PROSE_TAGS or "mermaid" in element.get("class", "").split():
            element.set("hidden", "")
    return _collapse("".join(_visible_parts(tree, hidden=False)))


def has_substance(visible: str) -> bool:
    """Whether anything a reader could act on survives emphasis and punctuation."""

    return bool(re.search(r"\w", re.sub(r"[*_~\\]", "", visible)))


def is_placeholder(visible: str) -> bool:
    """Whether the text only announces that a decision is still outstanding.

    Two shapes are rejected. Text whose every word is a placeholder, however
    decorated -- ``TBD``, ``**TODO.**`` -- and text that opens with one used as
    a marker, where punctuation separates it from the rest: ``TODO: choose a
    milestone``.

    A status word that merely begins a sentence is left alone, because it is
    usually carrying real information: "Pending Mathlib PR 1234" and "Unknown
    provenance, excluded by agreement" both name something a reader can check.
    Rejecting those pushed authors toward vaguer wording to satisfy the checker.

    The gap this leaves is a marker written without punctuation, as in "TODO
    choose a milestone". That reads as prose to any rule cheap enough to trust,
    so it is left to human review rather than guessed at.
    """

    stripped = re.sub(r"[*_~\\]", "", visible)
    words = re.findall(r"\w+", stripped.casefold())
    if not words:
        return False
    if all(word in _PLACEHOLDER_WORDS for word in words):
        return True
    if words[0] not in _PLACEHOLDER_WORDS:
        return False
    _, _, remainder = stripped.casefold().partition(words[0])
    return _MARKER_PUNCTUATION.match(remainder) is not None


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

    parts: list[str] = []
    # An explicit stack, so markup nested a thousand elements deep cannot
    # exhaust the interpreter's own.
    pending: list[tuple[object, bool]] = [(element, hidden)]
    while pending:
        item, inherited = pending.pop()
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item.tag, str):
            # A comment or processing instruction. Its text is markup, not
            # content, and a reader never sees it. Any tail text belongs to the
            # parent, which queued it below.
            continue
        concealed = _conceals(item, inherited)
        if not concealed and item.text:
            parts.append(item.text)
        for child in reversed(item):
            # Tail text sits in this element, not the child, so it is hidden
            # only when this element is.
            if not concealed and child.tail:
                pending.append((child.tail, False))
            pending.append((child, concealed))
    return parts


def frontmatter_end(lines: list[str]) -> int:
    """Return the index of the first line after any YAML frontmatter block."""

    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index + 1
    return len(lines)


@dataclass(frozen=True, slots=True)
class ArticleSection:
    """One H2 section of an article."""

    #: The heading's text with comments removed, as the graph loader reads it.
    title: str
    #: The heading line exactly as the author wrote it.
    heading: str
    #: The author's Markdown up to the next H2, without surrounding blank lines.
    body: str


@dataclass(frozen=True, slots=True)
class ArticleParts:
    """An article body split at its published H2 headings.

    ``statement`` is everything between the frontmatter and the first H2, with
    the H1 title line removed. It is the author's Markdown, untouched apart from
    surrounding blank lines, so a caller that publishes it publishes what was
    written and indentation keeps its meaning.
    """

    statement: str
    sections: tuple[ArticleSection, ...]


def article_parts(text: str) -> ArticleParts:
    """Split an article into its statement and its H2 sections.

    This is the one definition of where a statement ends, for the audit and
    for the published page alike. A heading counts only where :func:`content`
    leaves it visible, so ``## Proof`` inside a fenced block, an indented code
    block, or an HTML comment ends nothing, and a heading below level two
    belongs to whatever it sits in. A comment that never closes hides
    every heading after it; the audit then reports the sections it cannot see.
    """

    lines = text.splitlines()
    body = lines[frontmatter_end(lines) :]
    # An indented code line needs no masking: four leading spaces already
    # keep it from matching HEADING.
    masked = mask_fences_and_comments(body)

    title = next((index for index, line in enumerate(masked) if _heading_level(line) == 1), None)
    starts = [index for index, line in enumerate(masked) if _heading_level(line) == 2]
    breaks = sorted([*starts, *(() if title is None else (title,))])

    # The statement is whatever no section holds: the text before the first
    # heading and the text under the title. A title written below a section
    # ends that section, so the statement beneath it is not lost into it.
    statement = body[: breaks[0]] if breaks else body
    sections: list[ArticleSection] = []
    for start, stop in zip(breaks, [*breaks[1:], len(body)]):
        if start == title:
            statement = [*statement, *_open_comment(body[start]), *body[start + 1 : stop]]
        else:
            sections.append(
                ArticleSection(
                    title=HEADING.match(masked[start]).group(2).strip(),
                    heading=body[start],
                    body=_trim(body[start + 1 : stop]),
                )
            )
    return ArticleParts(statement=_trim(statement), sections=tuple(sections))


def _open_comment(title_line: str) -> list[str]:
    """Return the comment a title line leaves open, so the statement keeps it.

    The title line is dropped from the statement. A comment begun on it and
    closed further down would otherwise lose its opener, and the text it hides
    would read as prose.
    """

    if not strip_line_comments(title_line, False)[1]:
        return []
    return [title_line[title_line.rfind("<!--") :]]


def _trim(lines: list[str]) -> str:
    """Join ``lines`` without the blank ones at either end."""

    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def _heading_level(line: str) -> int:
    heading = HEADING.match(line)
    return len(heading.group(1)) if heading else 0


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


def mask_fences_and_comments(lines: list[str]) -> list[str]:
    """Return ``lines`` with fenced blocks and HTML comments blanked.

    This is how the graph, the audit and the page agree on which headings and
    links an article has. The site draws a fence without a closing line as
    plain text, with the headings and links after it in view, so such a fence
    hides nothing here.
    """

    return _mask_fences_and_comments(lines, set(), unclosed_fence_is_text=True)


def unclosed_fence_lines(lines: list[str]) -> tuple[int, ...]:
    """Return the one-based source lines of fences that never close."""

    unclosed: set[int] = set()
    _mask_fences_and_comments(
        lines,
        set(),
        unclosed_fence_is_text=True,
        unclosed_fence_lines=unclosed,
    )
    return tuple(line + 1 for line in sorted(unclosed))


def _mask_fences_and_comments(
    lines: list[str],
    hidden: set[int],
    *,
    unclosed_fence_is_text: bool = False,
    unclosed_fence_lines: set[int] | None = None,
) -> list[str]:
    masked: list[str] = []
    fence: tuple[str, int] | None = None
    in_comment = False
    # Where the open fence began and the comment state there, to go back to
    # when the fence turns out to have no closing line.
    opened: tuple[int, bool] = (0, False)
    # Per fence character, the shortest opener known to find no closing line.
    # A later one at least as long cannot close either.
    unclosed: dict[str, int] = {}
    index = 0
    while index < len(lines) or (fence is not None and unclosed_fence_is_text):
        if index == len(lines):
            # Read on from the fence's first line as text.
            unclosed[fence[0]] = fence[1]
            if unclosed_fence_lines is not None:
                unclosed_fence_lines.add(opened[0])
            index, in_comment = opened
            del masked[index:]
            hidden.difference_update(range(index, len(lines)))
            fence = None
            continue
        raw = lines[index]
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
            index += 1
            continue
        opened_in_comment = in_comment
        line, in_comment = strip_line_comments(raw, in_comment)
        match = FENCE.match(line)
        if match is not None and len(match.group(1)) < unclosed.get(match.group(1)[0], len(line) + 1):
            marker = match.group(1)
            fence = (marker[0], len(marker))
            opened = (index, opened_in_comment)
            hidden.add(index)
            masked.append("")
            index += 1
            continue
        # A line shows nothing either because the author left it empty or
        # because a comment covers it. Only the second belongs to a construct.
        if not line.strip() and (opened_in_comment or raw.strip()):
            hidden.add(index)
        masked.append(line)
        index += 1
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
    "ArticleParts",
    "ArticleSection",
    "article_parts",
    "Content",
    "content",
    "content_lines",
    "frontmatter_end",
    "has_substance",
    "is_placeholder",
    "link_targets",
    "local_target_issue",
    "markdown_anchors",
    "PublishedTable",
    "markdown_links",
    "mask_fences_and_comments",
    "published_tables",
    "render_html",
    "render_tree",
    "rendered_visible_text",
    "site_converter",
    "strip_line_comments",
    "unclosed_fence_lines",
    "visible_prose",
]
