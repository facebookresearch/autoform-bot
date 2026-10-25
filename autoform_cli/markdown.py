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

Fences and comments are read differently for an article alone and on a page.
A fence closes only on a bare fence line; for the graph and the audit, a later
bare line of the same character and at least the opener's length closes it,
and a fence that never closes is read as plain text, so it hides no heading or
dependency link. On a chapter page, where ``pymdownx.superfences`` asks for the
opener's exact length and indent, an unclosed fence can pair with a later
article's closing fence and hide everything between them, which is why
``check`` warns about one and the site shows that page as typed. The page can
also pair an unclosed backtick fence line with a later backtick run in the same
paragraph and show the text between as inline code; a link there is no link on
the page but is still read as a dependency. An HTML comment that never closes
hides every later heading and dependency link, and the part of the article it
opens in (statement or section) keeps the rest.
"""

from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from itertools import groupby
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import html5lib
import markdown as pymarkdown
from markdown.blockprocessors import ReferenceProcessor
from markdown.inlinepatterns import ReferenceInlineProcessor
from pymdownx.arithmatex import RE_BRACKET_INLINE, RE_SMART_DOLLAR_INLINE
from pymdownx.superfences import fence_div_format

#: The class pymdownx.arithmatex gives each formula it finds in the site's
#: Markdown. The page's MathJax reads these elements and no other text, so a
#: title or the navigation shows its TeX as typed, but for the titles and
#: summaries the dependency explorer draws in its rail and details, which it
#: marks with this class.
FORMULA_CLASS = "arithmatex"

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

#: The file names MkDocs converts to pages, as ``mkdocs.utils.markdown_extensions``
#: lists them. A file so named in the vault is a page on the site, whoever wrote it.
PAGE_SUFFIXES = (".markdown", ".mdown", ".mkdn", ".mkd", ".md")

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


def render_tree(text: str, *, allow_html: bool = True) -> object | None:
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
        # Raw HTML is stashed by the converter; generated inline math and
        # escaped code are not. A reading-only label can reject authored tags
        # without mistaking a TeX inequality or a code literal for markup.
        if not allow_html and _converter().htmlStash.rawHtmlBlocks:
            return None
    except Exception:
        return None
    try:
        return html5lib.parse(rendered, treebuilder="etree", namespaceHTMLElements=False)
    except Exception:
        return None


#: What could be markup where render writes a title itself, as references.
_TITLE_REFERENCES = str.maketrans({character: f"&#{ord(character)};" for character in "&<>\"'`*_[]$\\"})


@lru_cache(maxsize=4096)
def title_text(title: str) -> str:
    """``title``, Markdown an author wrote, as the plain text a reader sees,
    with every character that could be markup written as a numeric reference.

    Render writes titles into headings, navigation entries, and page metadata,
    and the theme prints the last two as they are. A reference reads as its
    character in all three, so a title only ever reads as text."""

    return name_text(rendered_visible_text(title))


def name_text(name: str) -> str:
    """``name``, such as a file's, with :func:`title_text`'s references and
    nothing read as Markdown first, so a name that holds a tag shows it."""

    return name.translate(_TITLE_REFERENCES)


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


def page_title(text: str) -> str | None:
    """The title MkDocs takes from the page ``text``, as the text a reader
    sees: its first block's, when that block is an H1, else None, and MkDocs
    titles the page by its file's name."""

    tree = render_tree(text)
    body = tree.find("body") if tree is not None else None
    first = body[0] if body is not None and len(body) else None
    if first is None or first.tag != "h1":
        return None
    return _collapse("".join(_visible_parts(first, hidden=False))) or None


def yaml_string(value: str) -> str:
    """``value`` as a YAML double-quoted string MkDocs reads back as written:
    JSON's escapes, with a character above U+FFFF as YAML's ``\\U`` escape,
    since YAML refuses the surrogate pair JSON writes for it."""

    return re.sub(
        r"\\u(d[89ab][0-9a-f]{2})\\u(d[c-f][0-9a-f]{2})",
        lambda pair: "\\U%08X" % (0x10000 + ((int(pair[1], 16) - 0xD800) << 10) + (int(pair[2], 16) - 0xDC00)),
        json.dumps(value),
    )


def header_title(title: str) -> str:
    """The ``title:`` line of a page's header for ``title``, the text a reader
    sees: the theme prints a page's metadata as it is, so ``&``, ``<``, ``>``
    and ``"`` are written as references, in a YAML string."""

    return "title: " + yaml_string(html.escape(title, quote=False).replace('"', "&quot;"))


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


#: The sections that list an article's dependencies. The site shows them in
#: its own form in the article's box, so they are not published as written.
DEPENDENCY_SECTIONS = frozenset({"depends on", "proof depends on"})

#: The boxes a leaf article's statement and the sections after it are
#: published in, on the page of the article that contains it.
STATEMENT_BOX = "bp-thmcontent"
NOTES_BOX = "bp-thmnotes"

#: What render writes at the top of every page an author wrote, in place of
#: the page's frontmatter. MkDocs takes it for the page's metadata, so no key
#: an author wrote reaches the theme, which prints metadata unescaped, and
#: MkDocs reads no leading line of the page as metadata either.
PAGE_HEADER = "---\n{}\n---\n"

#: A code span, which shows a comment as typed, or an HTML comment with its
#: end. A span opens at a run of backticks no backslash escapes and may cross
#: lines, but not a blank one, as in CommonMark.
_CODE_OR_COMMENT = re.compile(r"(?<![\\`])(`+)(?!`)(?:(?!\n[ \t]*\n).)*?(?<!`)\1(?!`)|<!--.*?-->", re.DOTALL)


def published_lines(text: str) -> str:
    """``text`` with every line break :meth:`str.splitlines` reads written as
    ``\\n``, and ending in one.

    The site edits articles a line at a time and writes them back this way,
    so a form feed or U+2028, which Python-Markdown reads as text, is a line
    break by the time the page is converted. Checking this text, rather than
    the bytes in the vault, checks the lines the page has."""

    return "".join(f"{line}\n" for line in text.splitlines())


def published_page(text: str) -> str:
    """The page render publishes for a page an author wrote with ``text``:
    :data:`PAGE_HEADER`, then the page with its frontmatter, as
    :func:`frontmatter_end` reads it, left as blank lines, so each line is
    three below its line in the file, and without the HTML comments outside
    code, as :func:`published_lines` writes it.

    A comment in a code span or a fenced or indented code block is shown as
    typed, so it stays, as does a comment that never closes, which check
    warns about.
    """

    # A byte-order mark would sit after render's header, where MkDocs no longer strips it.
    lines = text.removeprefix("\ufeff").splitlines()
    start = frontmatter_end(lines)
    if start == len(lines) and (start < 2 or lines[-1].strip() != "---"):
        start = 0  # a first line of --- that nothing closes is no frontmatter
    body = lines[start:]
    code: set[int] = set()
    continued: set[int] = set()
    masked = _mask_fences_and_comments(body, set(), unclosed_fence_is_text=True, fenced=code, continued=continued)
    # A line that opens a comment and holds nothing else is blank once masked,
    # but indented as code it may open or close a code block, which shows the
    # comment as typed. A line inside a comment opened earlier stays hidden.
    for index, line in enumerate(body):
        if index not in code and index not in continued and not masked[index].strip():
            if line.lstrip().startswith("<!--") and _indent(line) >= _CODE_INDENT:
                masked[index] = line
    _mask_indented_code(masked, code)
    kept: list[str] = []
    for in_code, run in groupby(enumerate(body), key=lambda numbered: numbered[0] in code):
        block = "\n".join(line for _, line in run)
        kept.append(block if in_code else _CODE_OR_COMMENT.sub(lambda found: found.group() if found[1] else "", block))
    return PAGE_HEADER + "\n" * start + published_lines("\n".join(kept))


def published_markdown(text: str) -> list[tuple[str, str]]:
    """Each piece of the article ``text`` the site can publish, with the
    Markdown it is converted in: the page MkDocs reads from its file, and the
    statement and notes a leaf's boxes show on its chapter page. ``text`` is
    :func:`published_page` text; the renderer publishes these same pieces,
    so check reads an article as the site does."""

    # MkDocs reads the header off, and the blank lines after it.
    page = text.removeprefix(PAGE_HEADER).lstrip("\n")
    pieces = [(page, page)]
    parts = statement_and_notes(text)
    alone = converted_alone(*parts)
    for part, box in zip(parts, (STATEMENT_BOX, NOTES_BOX), strict=True):
        if part:
            pieces.append((part, part if alone else "\n".join(boxed(part, box))))
    return pieces


def converted_alone(*parts: str) -> bool:
    """Whether render converts a statement and its notes on their own before
    boxing them: when either holds a "<", which the box's HTML reading could
    take for a tag."""

    return any("<" in part for part in parts)


def boxed(part: str, box: str) -> list[str]:
    """The lines that publish ``part`` as Markdown in a ``<div>`` of class
    ``box`` on a page."""

    return [f'<div class="{box}" markdown="1">', "", part, "", "</div>"]


def statement_and_notes(text: str) -> tuple[str, str]:
    """A leaf article's statement and the sections after it, as the site
    publishes them in the article's box.

    The statement is :func:`article_parts`'s, the span the audit reads. The
    dependency sections are dropped, since the box shows the DAG in its
    metadata line, and the headings in both parts are demoted below the
    chapter page's. Otherwise each part is the author's Markdown, less the
    blank lines around it and each section's body, so an indented first line
    is code in the box as in the vault."""

    parts = article_parts(text)
    notes = "\n\n".join(
        f"{section.heading}\n\n{section.body}".rstrip()
        for section in parts.sections
        if section.title.casefold() not in DEPENDENCY_SECTIONS
    )
    return _demote_headings(parts.statement), _demote_headings(notes)


def outside_fences(text: str, transform) -> str:
    """Apply *transform* to every line that is not inside a code fence."""
    fence: tuple[str, int] | None = None
    out: list[str] = []
    for line in text.splitlines():
        match = FENCE.match(line) if fence is None else FENCE_CLOSE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            out.append(line)
            continue
        out.append(line if fence is not None else transform(line))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _demote_headings(text: str) -> str:
    def demote(line: str) -> str:
        heading = HEADING.match(line)
        if heading is None:
            return line
        level = min(len(heading.group(1)) + 4, 6)
        return f"{'#' * level} {heading.group(2)}"

    return outside_fences(text, demote)


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


#: Views earlier renders wrote at the blueprint root, which render no longer writes.
RETIRED_VIEWS = frozenset({"dependencies.html", "graph.html", "progress.md"})
#: Views generated at the blueprint root. Render omits the vault's copies: it
#: writes the current views itself and drops stale copies of the retired ones.
GENERATED_VIEWS = RETIRED_VIEWS | {
    "dependencies.md",
    # The vault keeps its own Obsidian-readable copy; the site builds one.
    "structure.md",
}


def resolved_path(path: Path) -> Path | None:
    """``path`` with its symbolic links resolved, or None when the system cannot open it.

    The kernel follows the links first, so a missing part, a symlink loop or a
    chain past its link limit gives None on every Python, and that limit bounds
    the work `os.path.realpath` then does. Strict `realpath` also gives None at
    a link it cannot read, which macOS follows anyway. Windows applies `..` as
    text before it looks anything up. Callers still check where the result
    points and whether it is a file.
    """

    try:
        os.stat(path)
        return Path(os.path.realpath(path, strict=True))
    except (OSError, ValueError):  # ValueError: a NUL in the path
        return None


def directory_path(path: Path) -> Path | None:
    """``path`` as `resolved_path` gives it, or, for a directory it gives None
    for, the path the system gives the opened directory; None when neither works.

    For a blueprint root. `resolved_path` gives None at a link the system
    follows but will not let anyone read, which macOS allows for another
    user's symlink; above a blueprint such a link changes nothing inside it,
    so the system is asked for the path of the directory it opened. Links
    under that path then resolve as usual, and one inside the blueprint that
    cannot be read still gives None.
    """

    resolved = resolved_path(path)
    if resolved is not None or os.name == "nt":
        return resolved
    import fcntl

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except (OSError, ValueError):
        return None
    try:
        if hasattr(fcntl, "F_GETPATH"):  # macOS
            opened = os.fsdecode(fcntl.fcntl(descriptor, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
        else:  # Linux; elsewhere, without /proc, this fails and gives None
            opened = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return Path(opened)


def blueprint_root(path: str | Path) -> Path:
    """The directory ``path`` names, as `directory_path` gives it, or as written, made absolute."""

    given = Path(path).expanduser()
    return directory_path(given) or given.absolute()


def local_target_issue(
    source_path: Path,
    target: str,
    boundary: Path,
    *,
    label: str,
    unpublished: tuple[Path, ...] = (),
) -> tuple[str, str] | None:
    """Return ``(code, reason)`` when a local link does not resolve, or names a path render omits.

    ``source_path`` is the file containing the link, ``boundary`` the directory
    the link may not escape, and ``unpublished`` the folders render leaves out.
    External schemes are the reader's problem and are reported as fine. A
    fragment on a Markdown target must name a real heading.
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
    boundary = boundary.resolve()
    # The path the link spells, after its `..` steps. One that leaves the
    # blueprint escapes it, and nothing outside is looked at.
    spelled = Path(os.path.normpath(source_path.parent.resolve() / raw_path))
    if Path(raw_path).is_absolute() or not _is_within(spelled, boundary):
        return f"{label}-escapes-blueprint", f"{label} link escapes the blueprint: {target!r}"
    if spelled.parent == boundary and spelled.name in GENERATED_VIEWS:
        return f"generated-{label}-target", f"{label} link names a generated view, which render omits: {target!r}"
    if any(_is_within(spelled, folder) for folder in unpublished):
        reason = f"{label} link names an unpublished folder, which render omits: {target!r}"
        return f"unpublished-{label}-target", reason
    candidate = resolved_path(source_path.parent / raw_path if raw_path else source_path)
    if candidate is None:
        return f"{label}-not-found", f"{label} link does not resolve to a file: {target!r}"
    if not _is_within(candidate, boundary):
        return f"{label}-escapes-blueprint", f"{label} link escapes the blueprint: {target!r}"
    # Render resolves links in a copy of the vault that has no hidden paths and no
    # symbolic links, so a link on a page it publishes breaks when the path it spells
    # is hidden. A link naming a hidden part that its `..` steps leave again, as
    # `.x/../paper.md` does, is refused too: nothing needs one, and render opens
    # coverage evidence in its copy, which has no `.x`. Only a repository link
    # reaches a hidden note under `sources/`, and render writes one when it links
    # the other source notes there; that link resolves only once the note is
    # committed, so it is reported too, and `check` only warns about it.
    named = any(part.startswith(".") and part != ".." for part in Path(raw_path).parts)
    if named or _is_hidden_path(spelled, boundary):
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
    fenced: set[int] | None = None,
    continued: set[int] | None = None,
) -> list[str]:
    fenced = set() if fenced is None else fenced
    # The lines that start inside a comment opened on an earlier line.
    continued = set() if continued is None else continued
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
            fenced.difference_update(range(index, len(lines)))
            continued.difference_update(range(index, len(lines)))
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
            fenced.add(index)
            masked.append("")
            index += 1
            continue
        opened_in_comment = in_comment
        if opened_in_comment:
            continued.add(index)
        line, in_comment = strip_line_comments(raw, in_comment)
        match = FENCE.match(line)
        if match is not None and len(match.group(1)) < unclosed.get(match.group(1)[0], len(line) + 1):
            marker = match.group(1)
            fence = (marker[0], len(marker))
            opened = (index, opened_in_comment)
            hidden.add(index)
            fenced.add(index)
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


def relative_link(target: Path, output: Path, link_extension: str) -> str:
    """Relative link from the page at *output* to *target*, with its suffix swapped."""
    relative = Path(os.path.relpath(target.resolve(), output.resolve().parent))
    if not relative.name:
        # The page's own directory is ``.``, which has no name to take a suffix.
        return quote(relative.as_posix(), safe="/")
    return quote(relative.with_suffix(link_extension).as_posix(), safe="/")

__all__ = [
    "EXTERNAL_SCHEMES",
    "FENCE",
    "FENCE_CLOSE",
    "FORMULA_CLASS",
    "GENERATED_VIEWS",
    "HEADING",
    "INLINE_CODE",
    "NOTES_BOX",
    "PAGE_SUFFIXES",
    "RETIRED_VIEWS",
    "SITE_EXTENSIONS",
    "SITE_EXTENSION_CONFIGS",
    "STATEMENT_BOX",
    "ArticleParts",
    "ArticleSection",
    "article_parts",
    "Content",
    "boxed",
    "converted_alone",
    "content",
    "blueprint_root",
    "content_lines",
    "directory_path",
    "frontmatter_end",
    "has_substance",
    "is_placeholder",
    "link_definitions",
    "link_targets",
    "local_target_issue",
    "markdown_anchors",
    "PublishedTable",
    "markdown_links",
    "outside_fences",
    "PAGE_HEADER",
    "published_markdown",
    "published_page",
    "mask_code_and_comments",
    "mask_fences_and_comments",
    "published_tables",
    "relative_link",
    "render_html",
    "render_tree",
    "rendered_visible_text",
    "resolved_path",
    "site_converter",
    "statement_and_notes",
    "title_text",
    "name_text",
    "page_title",
    "header_title",
    "yaml_string",
    "strip_line_comments",
    "first_unclosed_fence",
    "visible_prose",
]
