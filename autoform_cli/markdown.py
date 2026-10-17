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
import os
import re
import stat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

import html5lib
import markdown as pymarkdown
from markdown.blockprocessors import ReferenceProcessor
from markdown.inlinepatterns import ReferenceInlineProcessor
from pymdownx.arithmatex import RE_BRACKET_INLINE, RE_SMART_DOLLAR_INLINE
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
FENCE = re.compile(r"^ {0,3}(`{3,}(?=[^`]*$)|~{3,})")
INLINE_CODE = re.compile(r"(`+).*?\1")

#: A closing fence carries nothing but its marker. ``pymdownx.superfences`` keeps
#: ````` trailing`` inside the code block, so treating it as a closer would expose
#: content that is still fenced when the page renders.
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")

#: The destination of an inline link, just after its label. The closing
#: parenthesis is required: a target such as ``[Node](../roadmap/node.md``
#: renders as literal text, so accepting it would let unrendered evidence
#: satisfy a coverage disposition.
_INLINE_TARGET = re.compile(r"\(\s*(<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")

#: The second label of a full or collapsed reference link, ``[text][label]`` or
#: ``[label][]``, as Python-Markdown reads it after the first.
_REFERENCE_LABEL = ReferenceInlineProcessor.RE_LINK

#: A reference definition, ``[label]: target "title"``, as the site's Markdown
#: reads one.
_LINK_DEFINITION = ReferenceProcessor.RE

#: Inline math, which the site reads right after code spans and before any
#: link, so a bracket inside it neither opens nor closes one.
_INLINE_MATH = re.compile(f"(?:{RE_SMART_DOLLAR_INLINE}|{RE_BRACKET_INLINE})")

#: A backslash escape that decides whether a bracket opens a link or an image.
_BRACKET_ESCAPE = re.compile(r"\\[\\\[\]!]")

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


def link_targets(value: str, references: dict[str, str] | None = None) -> tuple[str, ...]:
    """Return the target of every link Python-Markdown reads in ``value``.

    Inline code is masked, not deleted, as the renderer reads it before links:
    a link shown as an example inside backticks is no link, while a label that
    is only code, ``[`Foo.bar`](foo.md)``, is still a label. Inline math is
    masked the same way, since the site reads it before links too. Brackets
    nest in a label and an escaped bracket is text. A reference link,
    ``[text][label]``, ``[label][]`` or ``[label]``, takes its target from
    ``references``, as :func:`link_definitions` reads them, and is no link
    without a definition; a definition shows no link itself. Angle-bracket
    targets are unwrapped, and images are not links.
    """

    if _LINK_DEFINITION.match(value):
        return ()
    references = references or {}
    # Code spans, then inline math, are read before any link. Brackets are
    # matched with escapes masked; a target keeps its escapes.
    code_masked = _INLINE_MATH.sub(_blank, INLINE_CODE.sub(_blank, value))
    masked = _BRACKET_ESCAPE.sub(_blank, code_masked)
    closing = _closing_brackets(masked)
    targets: list[str] = []
    # Python-Markdown reads full and collapsed references, inline links and
    # shortcut references in that order, each in its own pass. A reference pass
    # skips what it reads without a definition: both labels of a full or
    # collapsed reference, the whole bracket pair of a shortcut one, so `[[1]]`
    # links nothing even with `[1]` defined. Neither pass starts at `![`.
    passed = spent = -1
    start = masked.find("[")
    while start >= 0:
        close = closing.get(start)
        image = masked[start - 1 : start] == "!"
        link = None
        if close is not None:
            label = masked[start + 1 : close]
            reference = _REFERENCE_LABEL.match(masked, close + 1) if start > passed else None
            full = _WHITESPACE.sub(" ", (reference.group(1) or label).lower()) if reference else None
            short = _WHITESPACE.sub(" ", label.lower())
            inline = _INLINE_TARGET.match(code_masked, close + 1)
            if reference and full not in references and not image:
                passed = reference.end() - 1
            if full in references:
                link = references[full], reference.end()
            elif inline is not None:
                link = _unwrap_target(inline.group(1)), inline.end()
            elif start > spent and short in references:
                link = references[short], close + 1
            elif not image:
                spent = max(spent, close)
        if link is None:
            start = masked.find("[", start + 1)
            continue
        if not image:
            targets.append(link[0])
        start = masked.find("[", link[1])
    return tuple(targets)


def link_definitions(lines: list[str]) -> dict[str, str]:
    """Return the target of each reference definition in ``lines``, by label.

    ``lines`` are masked as :func:`mask_code_and_comments` or
    :func:`content_lines` leave them, so a definition in code or a comment does
    not count. A definition counts on a line of its own, and its label is
    matched without regard to case. A later definition of a label replaces an
    earlier one, as the site's Markdown reads them.
    """

    references: dict[str, str] = {}
    for line in lines:
        definition = _LINK_DEFINITION.match(line)
        if definition is not None:
            references[definition.group(1).strip().lower()] = definition.group(2).lstrip("<").rstrip(">")
    return references


def _closing_brackets(text: str) -> dict[int, int]:
    """Map the index of each ``[`` in ``text`` to that of the ``]`` closing it."""

    closing: dict[int, int] = {}
    opened: list[int] = []
    for index, character in enumerate(text):
        if character == "[":
            opened.append(index)
        elif character == "]" and opened:
            closing[opened.pop()] = index
    return closing


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group(0))


def markdown_links(text: str) -> list[tuple[int, str]]:
    """Return ``(line number, target)`` for every visible link in ``text``.

    A reference link is reported on its own line, with the target of its
    definition anywhere in ``text``.
    """

    lines = content_lines(text)
    references = link_definitions(lines)
    links: list[tuple[int, str]] = []
    for line_number, line in enumerate(lines, start=1):
        links.extend((line_number, target) for target in link_targets(line, references))
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

    ``statement`` is everything no H2 section holds: the text between the
    frontmatter and the first H2, and the text under an H1 title written below
    an H2, with the title line removed. It is the author's Markdown, untouched
    apart from surrounding blank lines, so a caller that publishes it publishes
    what was written and indentation keeps its meaning.
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
    breaks = [index for index, line in enumerate(masked) if index == title or _heading_level(line) == 2]

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


# More link spellings than any one path in a blueprint needs. On a
# case-insensitive file system, each level of links can double the spellings
# of the same link, and resolving them all would take exponential time.
_MAX_LINKS = 4096


def resolved_path(path: Path) -> Path | None:
    """``path`` with its symbolic links resolved, or None if that fails or a link cannot be read.

    On POSIX, `_realpath_posix` resolves it as Python 3.13's `os.path.realpath`
    does, on every version: a symlink loop stays as written and the rest of the
    path resolves past it. Before 3.13, `realpath` recurses once per link and
    stops at a loop, applying the rest of the path's `..` steps as written, so
    a link after the loop could lead out of the blueprint unresolved, and
    `Path.resolve` raises when the path `realpath` returns still runs through a
    loop. From 3.13 it runs too, because `realpath` keeps a link it cannot read
    as written, so a target outside the blueprint would look inside. So a link
    gets the same verdict on every supported version; callers still check where
    it points and whether it is a file. Windows `realpath` applies `..` as
    written on every version, as Windows itself does.
    """

    try:
        if os.name == "nt":
            return Path(os.path.realpath(path))
        resolved = _realpath_posix(os.fspath(path))
    except (OSError, ValueError):
        # A NUL in the path, which callers check for first, `os.getcwd` failing
        # for a relative path, or a path Windows refuses to resolve, such as one
        # over its length limit.
        return None
    return None if resolved is None else Path(resolved)


def _realpath_posix(path: str) -> str | None:
    """Python 3.13's POSIX `os.path.realpath`, or None at a link it cannot read.

    Each spelling of a link is read once. A link met again before its target is
    resolved is a loop and stays as written; a later `..` leaves it again. A
    part that cannot be looked up, missing or under a loop, also stays as
    written, and the parts after it still resolve. Unlike 3.13, a link that
    cannot be read leaves the path unresolved: macOS follows a link whose mode
    forbids reading it, so keeping that link as written would hide where it
    leads. A path that needs more than `_MAX_LINKS` spellings of links is not
    resolved either.
    """

    pending: list[str | None] = path.split("/")[::-1]
    resolved = "/" if path.startswith("/") else os.getcwd()
    # Each link seen, mapped to its resolved target, or to None while that is pending.
    links: dict[str, str | None] = {}
    while pending:
        name = pending.pop()
        if name is None:
            # The link popped next has its target resolved.
            links[pending.pop()] = resolved
            continue
        if name in ("", "."):
            continue
        if name == "..":
            resolved = resolved[: resolved.rindex("/")] or "/"
            continue
        joined = os.path.join(resolved, name)
        try:
            is_link = stat.S_ISLNK(os.lstat(joined).st_mode)
        except OSError:
            is_link = False
        if not is_link:
            resolved = joined
            continue
        if joined in links:
            resolved = links[joined] or joined
            continue
        if len(links) >= _MAX_LINKS:
            return None
        try:
            target = os.readlink(joined)
        except OSError:
            return None
        if target.startswith("/"):
            resolved = "/"
        links[joined] = None
        pending += [joined, None, *target.split("/")[::-1]]
    return resolved


def local_target_issue(
    source_path: Path,
    target: str,
    boundary: Path,
    *,
    label: str,
) -> tuple[str, str] | None:
    """Return ``(code, reason)`` when a local link does not resolve, or names a hidden path.

    ``source_path`` is the file containing the link, ``boundary`` the directory
    the link may not escape. External schemes are the reader's problem and are
    reported as fine. A fragment on a Markdown target must name a real heading.
    """

    try:
        split = urlsplit(target)
    except ValueError:
        return f"malformed-{label}-url", f"{label} link is not a valid URL: {target!r}"
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
        candidate = source_path
    else:
        relative = Path(raw_path)
        if relative.is_absolute():
            return f"{label}-escapes-blueprint", f"{label} link escapes the blueprint: {target!r}"
        candidate = source_path.parent / relative
    candidate = resolved_path(candidate)
    if candidate is None:
        return f"{label}-not-found", f"{label} link does not resolve to a file: {target!r}"

    boundary = boundary.resolve()
    if not _is_within(candidate, boundary):
        return f"{label}-escapes-blueprint", f"{label} link escapes the blueprint: {target!r}"
    # Render resolves links in a copy of the vault that has no hidden paths and no
    # symbolic links, so a link on a page it publishes breaks when the path it spells
    # is hidden. `.x/../paper.md` still passes.
    spelled = Path(os.path.normpath(source_path.parent.resolve() / raw_path))
    if _is_hidden_path(spelled, boundary) and not _is_hidden_path(source_path.resolve(), boundary):
        return f"hidden-{label}-target", f"{label} link names a hidden path, which render omits: {target!r}"
    # Before Python 3.14, `Path.is_file` raises under an unreadable directory.
    if not os.path.isfile(candidate):
        return f"{label}-not-found", f"{label} link does not resolve to a file: {target!r}"
    if split.fragment and candidate.suffix.casefold() == ".md":
        fragment = unquote(split.fragment)
        if fragment not in markdown_anchors(candidate):
            return f"{label}-anchor-not-found", f"{label} link fragment does not resolve: {target!r}"
    return None


def mask_fences_and_comments(lines: list[str]) -> list[str]:
    """Return ``lines`` with fenced blocks and HTML comments blanked.

    This is how the graph and the audit agree on which headings and links an
    article has. A fence without a closing line is read as plain text, so it
    hides nothing here. A one-article page draws it the same way, but a chapter
    page can pair it with a later article's fence, so the audit reports it as
    ``unclosed-code-fence``.
    """

    return _mask_fences_and_comments(lines, set(), unclosed_fence_is_text=True)


def first_unclosed_fence(lines: list[str]) -> int | None:
    """Return the index of the first fence line that never closes, if any."""

    unclosed: set[int] = set()
    _mask_fences_and_comments(
        lines,
        set(),
        unclosed_fence_is_text=True,
        unclosed_openers=unclosed,
    )
    return min(unclosed, default=None)


def mask_code_and_comments(lines: list[str]) -> list[str]:
    """Return :func:`mask_fences_and_comments` with indented code blocks blanked too."""

    return _mask_indented_code(mask_fences_and_comments(lines), set())


def _mask_fences_and_comments(
    lines: list[str],
    hidden: set[int],
    *,
    unclosed_fence_is_text: bool = False,
    unclosed_openers: set[int] | None = None,
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
            if unclosed_openers is not None:
                unclosed_openers.add(opened[0])
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


def _is_hidden_path(path: Path, boundary: Path) -> bool:
    """Whether a part of *path* below *boundary* starts with ".", so render omits it (its `_is_hidden`)."""
    return _is_within(path, boundary) and any(part.startswith(".") for part in path.relative_to(boundary).parts)


__all__ = [
    "EXTERNAL_SCHEMES",
    "FENCE",
    "FENCE_CLOSE",
    "HEADING",
    "INLINE_CODE",
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
    "link_definitions",
    "link_targets",
    "local_target_issue",
    "markdown_anchors",
    "PublishedTable",
    "markdown_links",
    "mask_code_and_comments",
    "mask_fences_and_comments",
    "published_tables",
    "render_html",
    "render_tree",
    "rendered_visible_text",
    "resolved_path",
    "site_converter",
    "strip_line_comments",
    "first_unclosed_fence",
    "visible_prose",
]
