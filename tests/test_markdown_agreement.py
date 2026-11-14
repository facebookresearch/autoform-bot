"""The links and code spans markdown.py reads, against the site's own reading.

markdown.py reads a page's links and code spans by rules of its own, written to
agree with the site's Python-Markdown, so that check and the graph read a
page's links without rendering it. These tests write pages from a small
grammar, with a fixed seed, and read each both ways. A difference outside the
known ones, :data:`KNOWN` for links, fails, with the page that shows it.
"""

from __future__ import annotations

import html
import random
import re
from collections import Counter

import pytest
from markdown.inlinepatterns import BACKTICK_RE

from autoform_cli.markdown import (
    PAGE_HEADER,
    _PAGE_CODE_OR_COMMENT,
    content_lines,
    link_definitions,
    link_targets,
    markdown_links,
    mask_code_and_comments,
    published_page,
    render_html,
)

#: The seed, and how many pages and code-span texts it writes.
SEED = 1
PAGES = 1000
SPAN_TEXTS = 2000

#: Where :func:`markdown_links` and the site read a link differently, and why.
KNOWN = {
    "line read": (
        "check reads each line of a paragraph alone and keeps each link it finds, even one the site shows in a "
        "code span or formula it pairs across lines, as a definition's target, or as the text of a full reference "
        "whose label, or its definition's target, is on the next line (#325)"
    ),
    "display math": "markdown_links reads a display formula, a block from $$ or \\[ to $$ or \\], as text",
    "unclosed fence": (
        "content() keeps a fence that never closes open to the end of the page, where the site reads it as text "
        "(#201), so the links on its lines, and in a paragraph or definition they belong to, read differently"
    ),
    "rejected fence": (
        "content() opens a fence on a line pymdownx.superfences opens none on, as the text after its backticks "
        "is no language or options, so the site reads its lines as text"
    ),
    "nested link": (
        "Python-Markdown reads a link inside a reference link's text, or inside or after another element in a "
        "link's text, and a browser shows both links; markdown_links reads the outer one"
    ),
}

# The grammar writes paragraphs, quotes and list items, loose or tight, of
# brackets, links, and code spans or inline math; display formulas; and
# comments on lines of their own. Link k's text starts a{k}z and it targets
# l{k}.md, so a difference names its link. Every other reference definition has
# its target on the line after its label. Fence lines come only from backtick
# tokens that join, and \[ and \] can make a whole paragraph a display formula.
# Left out, as read differently in ways no class names case by case: code spans
# and inline math in one paragraph, which the site can cross or escape into
# each other; a comment in a code span, which content() still opens where the
# span crosses a line break; emphasis, inside which Python-Markdown reads the
# text after an element again, where a reference it skipped can link; a
# backtick after an escaped one, which only Python-Markdown's rescan pairs (see
# test_python_markdown_s_rescan_explains_only_its_own_difference); and a
# definition in a quote or list item, which link_definitions does not read. Nor
# does it write raw HTML, headings, tables, images, indented code, tilde
# fences, tabs, comments that share or span lines, list markers other than -
# and 1., a quote that deepens within a block, or link titles and <targets>.
TEXT = ["x", "x", " ", " ", "[", "]", "\\[", "\\]"]
CODE = ["`c`", "`", "`", "``", "\\`", "\\\\", "``a`b``"]
MATH = ["$x$", "$", "\\(", "\\)", "\\(y\\)", "$a b$"]
PREFIXES = ["", "", "", "> ", "- ", "1. ", "> - ", "- > "]
#: Where a link's text or label wraps onto its block's next line.
_WRAP = "\0"


class _Page:
    """One page the grammar writes, its reference definitions at the end."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.links = 0
        self.definitions: list[str] = []

    def link(self) -> str:
        self.links += 1
        k, form = self.links, self.rng.random()
        if form < 0.5:
            return f"[a{k}z](l{k}.md)"
        if form < 0.6:
            return f"[a{k}z{_WRAP}w](l{k}.md)"
        label = f"r{k} q" if form < 0.75 else f"a{k}z"
        # A label matches its definition whatever their case and wherever it wraps.
        self.definitions.append(f"[{label.upper() if self.rng.random() < 0.3 else label}]: l{k}.md")
        if form < 0.75:
            return f"[a{k}z][r{k}{self.rng.choice([' ', _WRAP])}q]"
        return f"[a{k}z][]" if form < 0.85 else f"[a{k}z]"

    def line(self, tokens: list[str]) -> str:
        pieces: list[str] = []
        for _ in range(self.rng.randint(1, 7)):
            piece = self.link() if self.rng.random() < 0.3 else self.rng.choice(tokens)
            while pieces and pieces[-1] == "\\`" and piece.startswith("`"):
                piece = self.rng.choice(tokens)
            pieces.append(piece)
        # Four spaces at a container's margin would open indented code.
        return "".join(pieces).lstrip(" ") or "x"

    def block(self) -> str:
        form = self.rng.random()
        if form < 0.05:
            return "$$\n" + self.line(TEXT).replace(_WRAP, "\n") + "\n$$"
        if form < 0.08:
            return "\\[\n" + self.line(TEXT).replace(_WRAP, "\n") + "\n\\]"
        if form < 0.12:
            return "<!-- " + self.line(TEXT).replace(_WRAP, " ") + " -->"
        tokens = TEXT + (CODE if self.rng.random() < 0.5 else MATH)
        prefix = self.rng.choice(PREFIXES)
        indent = "  " if prefix.endswith(("- ", ". ")) else prefix if prefix.startswith(">") else ""
        block = prefix + self.line(tokens)
        for _ in range(self.rng.randint(0, 2)):
            # A list's next line continues its item or starts the next one.
            block += "\n" + (prefix if indent == "  " and self.rng.random() < 0.5 else indent) + self.line(tokens)
        return block.replace(_WRAP, "\n" + indent)

    def text(self) -> str:
        blocks = [self.block() for _ in range(self.rng.randint(1, 5))]
        page = blocks[0]
        for block in blocks[1:]:
            page += self.rng.choice(["\n\n", "\n\n", "\n \n"]) + block
        if self.definitions:
            page += "\n\n" + "\n".join(
                definition.replace("]: ", "]:\n  ", 1) if index % 2 else definition
                for index, definition in enumerate(self.definitions)
            )
        return page + "\n"


#: The start of an anchor Python-Markdown writes, or the end of one.
_ANCHOR = re.compile(r'<a href="([^"]*)"|</a>')
#: A reference definition the grammar writes on one line.
_DEFINITION = re.compile(r"\[(?:r\d+ q|a\d+z)\]: l\d+\.md$", re.IGNORECASE)
#: A line that opens a fenced block: three or more backticks with none after
#: them, or three or more tildes.
_FENCE = re.compile(r" {0,3}(?:`{3,}[^`]*|~{3,}.*)$")


def _site(page: str) -> tuple[str, Counter[str], Counter[str]]:
    """The HTML the site makes of *page*, as MkDocs converts it without the
    metadata render writes on top, the targets of its links, and those of the
    links in another link's text, which a browser shows apart."""

    rendered = render_html(published_page(page).removeprefix(PAGE_HEADER))
    links: Counter[str] = Counter()
    nested: Counter[str] = Counter()
    depth = 0
    for found in _ANCHOR.finditer(rendered):
        if found[1] is None:
            depth -= 1
            continue
        links[found[1]] += 1
        nested[found[1]] += depth > 0
        depth += 1
    return rendered, links, nested


def _known(
    page: str, target: str, links: list[tuple[int, str, bool]], site: tuple[str, Counter[str], Counter[str]]
) -> str | None:
    """The difference in :data:`KNOWN` that explains how *links*, what
    :func:`markdown_links` reads in *page*, and *site*, as :func:`_site` reads
    it, differ on *target*, or None."""

    rendered, shown, nested = site
    read = sum(1 for _, found, _ in links if found == target)
    k = target.removeprefix("l").removesuffix(".md")
    marker = f"a{k}z"
    lines = page.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.search(rf"\[{marker}(?!\w)", line) and not line.lower().startswith(f"[{marker}]:")
        ),
        None,
    )
    if start is None:
        return None
    kept = content_lines(page)
    if read > shown[target]:
        # A display formula or code block holds only text, so one holds the marker if it opened before the
        # marker and has not closed.
        before = rendered[: max(rendered.find(marker), 0)]
        if before.rfind('<div class="arithmatex">') > before.rfind("</div>"):
            return "display math"
        if before.rfind("<pre") > before.rfind("</pre>"):
            return None
        # Read as a paragraph's text, since alone a line can open a block, as \[ ... \] does, with only the
        # definitions on one line, as a line read alone takes them.
        alone = _site("x " + lines[start] + "\n\n" + "\n".join(line for line in lines if _DEFINITION.match(line)))[1]
        # link_targets reads a line as check always has, and check reports what it finds there unwrapped, as
        # errors where a wrapped link only warns (#325).
        on_line = link_targets(kept[start], link_definitions(kept, alone=True)).count(target)
        if min(alone[target], on_line) >= read and not any(wrapped for _, found, wrapped in links if found == target):
            return "line read"
    elif shown[target] - read <= nested[target]:
        return "nested link"
    # content() blanks an unclosed fence from its first line to the end, where mask_code_and_comments() reads it as
    # text, so the first line they differ on opens it. A line of the link's paragraph or definition must be past it.
    first = last = start
    while first > 0 and lines[first - 1].strip():
        first -= 1
    while last + 1 < len(lines) and lines[last + 1].strip():
        last += 1
    definition = [index for index, line in enumerate(lines) if line.lower().startswith((f"[r{k} q]:", f"[{marker}]:"))]
    differ = [index for index, line in enumerate(mask_code_and_comments(lines)) if kept[index] != line]
    if differ and _FENCE.match(lines[differ[0]]) and set(differ) & {*range(first, last + 1), *definition}:
        return "unclosed fence"
    # Or content() blanks the link's line, or its definition's, in a closed fence whose first line, alone, the
    # site makes no code block of.
    for index in [start, *definition]:
        opener = next((above for above in range(index, -1, -1) if _FENCE.match(lines[above])), None)
        if not kept[index] and opener is not None:
            line = lines[opener].strip()
            if "<pre" not in render_html(line + "\n\n" + line[:3]):
                return "rejected fence"
    return None


def test_markdown_links_reads_the_links_the_site_shows() -> None:
    rng = random.Random(SEED)
    unexplained: list[str] = []
    for _ in range(PAGES):
        page = _Page(rng).text()
        links = markdown_links(page)
        site = _site(page)
        read = Counter(target for _, target, _ in links)
        for target in sorted(read.keys() | site[1].keys()):
            if read[target] != site[1][target] and _known(page, target, links, site) not in KNOWN:
                unexplained.append(f"{target}: read {read[target]}, site {site[1][target]}, on {page!r}")

    assert not unexplained, "\n".join(unexplained[:10])


#: A page for each known difference, and the target it shows it on.
WITNESSES = [
    ("A `code span\n[a1z](l1.md) this` link\n", "l1.md", "line read"),
    ("A $x\n[a1z](l1.md) z$ link\n", "l1.md", "line read"),
    ("> [d]:\n> [a1z](l1.md)\n", "l1.md", "line read"),
    # The site reads the two lines as one full reference, to l2.md.
    ("[a1z]\n[a2z]\n\n[a1z]: l1.md\n[a2z]: l2.md\n", "l1.md", "line read"),
    # The site reads one full reference, to l2.md, whose definition's target is on the next line.
    ("[a1z][a2z]\n\n[a1z]: l1.md\n[a2z]:\n  l2.md\n", "l1.md", "line read"),
    ("$$\n[a1z](l1.md)\n$$\n", "l1.md", "display math"),
    ("\\[\n[a1z](l1.md)\n\\]\n", "l1.md", "display math"),
    ("```\n[a1z](l1.md)\n", "l1.md", "unclosed fence"),
    ("[a1z][r1 q]\n\n```\n[r1 q]: l1.md\n", "l1.md", "unclosed fence"),
    ("```[a1z](l1.md)\n\n```\n", "l1.md", "rejected fence"),
    ("[x [a1z](l1.md) y][r2 q]\n\n[r2 q]: l2.md\n", "l1.md", "nested link"),
    ("[`c` [a1z](l1.md)](l2.md)\n", "l1.md", "nested link"),
]


#: Differences no class may explain, made by hand as a reader that regressed
#: would make them: the page, the target, and what markdown_links would read.
UNEXPLAINED = [
    # A link lost after a line of two backticks, which opens no fence.
    ("``x\n\n[a1z](l1.md)\n", "l1.md", []),
    # A link read in a code span the site pairs on its line.
    ("`[a1z](l1.md)`\n", "l1.md", [(1, "l1.md", False)]),
    # A link read in a closed fence.
    ("```\n[a1z](l1.md)\n```\n", "l1.md", [(2, "l1.md", False)]),
    # A line-read link marked wrapped, which check would only warn about.
    ("A `code span\n[a1z](l1.md) this` link\n", "l1.md", [(2, "l1.md", True)]),
]


@pytest.mark.parametrize(("page", "target", "links"), UNEXPLAINED)
def test_no_class_explains_another_difference(page: str, target: str, links: list[tuple[int, str, bool]]) -> None:
    assert _known(page, target, links, _site(page)) is None


def test_each_known_difference_has_a_witness() -> None:
    assert {kind for _, _, kind in WITNESSES} == KNOWN.keys()


@pytest.mark.parametrize(("page", "target", "kind"), WITNESSES)
def test_each_known_difference_still_shows(page: str, target: str, kind: str) -> None:
    # A difference markdown.py stops making fails here, so its class goes.
    links = markdown_links(page)
    site = _site(page)

    assert sum(1 for _, found, _ in links if found == target) != site[1][target]
    assert _known(page, target, links, site) == kind


#: Pieces of the texts the code-span comparison writes: letters, spaces, an
#: asterisk, backtick and backslash runs, a spaced span, and line breaks, blank
#: lines among them.
SPAN_PIECES = [
    "a", "b", " ", " ", "*", "`", "`", "``", "```", "````", "`` ` ``", "\\", "\\\\", "\\\\\\", "\n", "\n\n", "\n \n"
]

#: Python-Markdown's backtick pattern, compiled as its inline processors compile it.
_BACKTICK = re.compile(BACKTICK_RE, re.DOTALL | re.UNICODE)


def _span_text(rng: random.Random) -> str:
    text = "".join(rng.choice(SPAN_PIECES) for _ in range(rng.randint(1, 12)))
    # Indented code and a fenced block show their text in <code> too.
    text = re.sub(r"(?m)^ {4,}", "   ", text)
    return re.sub(r"(?m)^( {0,3})(?=```+[^`\n]*$)", r"\1a", text)


def _page_spans(text: str) -> list[str]:
    return [run[0][len(run[1]) : -len(run[1])].strip() for run in _PAGE_CODE_OR_COMMENT.finditer(text) if run[1]]


def _site_spans(text: str) -> list[str]:
    return [html.unescape(code) for code in re.findall(r"<code>(.*?)</code>", render_html(text), re.DOTALL)]


def _backtick_spans(text: str, *, again: bool) -> list[str]:
    """The code spans Python-Markdown's backtick pattern pairs in each
    paragraph of *text*, setting each match aside as it finds it and looking on
    from there or, *again*, from the paragraph's start, as Python-Markdown does."""

    spans: list[str] = []
    for paragraph in re.split(r"\n[ \t]*\n", text):
        found_spans: dict[str, str] = {}
        at = count = 0
        while (found := _BACKTICK.search(paragraph, at)) is not None:
            # A character of its own stands for each match, so the spans come out in the order they stand.
            mark = chr(0xE000 + count)
            count += 1
            if found[3] is not None:
                found_spans[mark] = found[3].strip()
            paragraph = paragraph[: found.start()] + mark + paragraph[found.end() :]
            at = 0 if again else found.start() + 1
        spans += [found_spans[character] for character in paragraph if character in found_spans]
    return spans


def _rescanned(text: str, page: list[str], site: list[str]) -> bool:
    """Whether Python-Markdown's rescan explains how *page* and *site*, the
    spans the page's pattern and the site pair in *text*, differ: after each
    span it sets aside, Python-Markdown looks again from the paragraph's start,
    where the page's pattern reads on."""

    return page == _backtick_spans(text, again=False) and site == _backtick_spans(text, again=True)


def test_the_page_s_pattern_pairs_code_spans_as_python_markdown_does() -> None:
    """The page copies Python-Markdown's backtick rule, so an upgrade that
    pairs spans differently fails here rather than on a published page."""

    rng = random.Random(SEED)
    unexplained: list[str] = []
    for _ in range(SPAN_TEXTS):
        text = _span_text(rng)
        page, site = _page_spans(text), _site_spans(text)
        if page != site and not _rescanned(text, page, site):
            unexplained.append(f"{text!r}: page {page}, site {site}")

    assert not unexplained, "\n".join(unexplained[:10])


@pytest.mark.parametrize(
    ("text", "page", "site", "rescanned"),
    [
        # The one difference the comparison above allows: once the span of a is
        # set aside, the escaped backtick that stood next to its run closes one.
        ("`\\```a``", ["a"], ["\\", "a"], True),
        # Python-Markdown makes a list here, which no reading of backticks explains.
        ("* a`\n* b`", ["* b"], [], False),
    ],
)
def test_python_markdown_s_rescan_explains_only_its_own_difference(
    text: str, page: list[str], site: list[str], rescanned: bool
) -> None:
    assert (_page_spans(text), _site_spans(text)) == (page, site)
    assert _rescanned(text, page, site) is rescanned
