"""site_links: the links the site's converter reads on a page, placed back on the page's text.

The four disagreements and the cases found with them in design note "One Markdown reader" are site-truth tests
here: each asserts the site's answer, which markdown_links does not give. The grammar of
test_markdown_agreement.py writes pages on which the marks must change no link and place every one.
"""

from __future__ import annotations

import html
import random
import re
from collections import Counter

import html5lib
import pytest
from markdown.blockparser import BlockParser
from markdown.util import AMP_SUBSTITUTE

from autoform_cli.markdown import (
    _RAW_TEXT_ELEMENT,
    PAGE_HEADER,
    SiteLink,
    SiteReadingError,
    _site_tree,
    markdown_links,
    published_page,
    render_html,
    site_links,
)
from tests.test_markdown import LINK_CORPUS, LINK_DESTINATIONS
from tests.test_markdown_agreement import SEED, _Page


def _read(text: str) -> list[tuple[str, str | None, int | None, str | None]]:
    """``(href, kind, line, placed text)`` for each link site_links reads in *text*."""

    return [
        (link.href, link.kind, link.line, text[slice(*link.span)] if link.span else None) for link in site_links(text)
    ]


def _site_anchors(text: str) -> Counter[tuple[str, str | None]]:
    """The address and title of each link the site's converter reads on *text*'s page, read without marks; on a page
    with a raw-text element, such as ``<textarea>``, each a browser builds from the page."""

    page = published_page(text).removeprefix(PAGE_HEADER)
    if _RAW_TEXT_ELEMENT.search(page):
        built = html5lib.parse(render_html(page), treebuilder="etree", namespaceHTMLElements=False)
        return Counter((a.get("href"), a.get("title")) for a in built.iter("a") if a.get("href") is not None)
    root = _site_tree(page)
    return Counter(
        (html.unescape(anchor.get("href").replace(AMP_SUBSTITUTE, "&")), anchor.get("title"))
        for anchor in root.iter("a")
        if anchor.get("href") is not None
    )


def test_an_inline_link_is_placed_on_its_destination_with_its_title_and_section() -> None:
    text = '# Title\n\n## Depends on\n\nSee [a](a.md) and [b](sub/b.md "Bee").\n'

    assert site_links(text) == [
        SiteLink("a.md", None, "inline", "Depends on", 5, (32, 36)),
        SiteLink("sub/b.md", "Bee", "inline", "Depends on", 5, (46, 54)),
    ]


def test_a_reference_link_is_placed_on_its_definition_s_destination() -> None:
    text = "## Uses\n\n[full][r], [r][] and [R].\n\n[r]: target.md\n"

    links = site_links(text)

    assert [(link.href, link.kind, link.section, link.line) for link in links] == [
        ("target.md", "reference", "Uses", 5)
    ] * 3
    assert {text[slice(*link.span)] for link in links} == {"target.md"}


def test_autolinks_are_links_placed_at_their_end() -> None:
    # D1: the site links them. A mail address is linked as mailto:, which the page does not write, so it has no span.
    text = "## Sources\n\n<https://example.org/a> and <someone@example.org>\n"

    assert _read(text) == [
        ("https://example.org/a", "autolink", 3, "https://example.org/a"),
        ("mailto:someone@example.org", "autolink", 3, None),
    ]
    assert markdown_links(text) == []


def test_a_linked_image_is_one_link_to_its_outer_destination() -> None:
    assert _read("[![Badge](badge.png)](page.md)\n") == [("page.md", "inline", 1, "page.md")]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("[a](<a b.md>)\n", [("a b.md", "inline", 1, "a b.md")], id="angle-brackets"),
        pytest.param("[a](a(b)c.md)\n", [("a(b)c.md", "inline", 1, "a(b)c.md")], id="balanced-parentheses"),
        pytest.param("[a]()\n", [("", "inline", 1, "")], id="empty"),
        # The address is not the text the page holds, so it has no span; the line still comes from its mark.
        pytest.param("[a](a\\_b.md)\n", [("a_b.md", "inline", 1, None)], id="escape"),
        pytest.param(
            "[a][r]\n\n[r]:\n  next-line.md\n", [("next-line.md", "reference", 4, "next-line.md")], id="next-line"
        ),
    ],
)
def test_destination_forms(text: str, expected: list[tuple[str, str | None, int | None, str | None]]) -> None:
    assert _read(text) == expected


def test_lines_are_the_page_s_own_past_frontmatter_comments_and_crlf() -> None:
    text = "---\ntitle: x\n---\n<!-- a comment\n[hidden](no.md)\nover lines -->\n# T\r\n\r\n[a](a.md)\r\n[b][r]\r\n\r\n[r]:\r\n  r.md\r\n"

    assert _read(text) == [("a.md", "inline", 9, "a.md"), ("r.md", "reference", 13, "r.md")]


@pytest.mark.parametrize(("source", "expected"), LINK_CORPUS)
def test_site_links_reads_the_site_s_targets_in_the_link_corpus(source: str, expected: list[str]) -> None:
    assert [link.href for link in site_links(source)] == expected


# The open disagreements, each with the site's answer.


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("> ```\n> [a](in-fence.md)\n> ```\n\n[b](after.md)\n", id="quote"),
        pytest.param("- ```\n  [a](in-fence.md)\n  ```\n\n[b](after.md)\n", id="list-item"),
    ],
)
def test_a_fence_opened_in_a_quote_or_list_item_shows_code(text: str) -> None:
    assert [link.href for link in site_links(text)] == ["after.md"]
    assert "in-fence.md" in [target for _, target, _ in markdown_links(text)]


@pytest.mark.parametrize(
    ("text", "line"),
    [
        pytest.param("> [x][r]\n>\n> [r]:\n> next-q.md\n", 4, id="quote-next-line"),
        pytest.param("- [x][r]\n- [r]: next-q.md\n", 2, id="list-item"),
        pytest.param("- see [x][r]\n\n- [r]:\n  next-q.md\n", 4, id="list-item-next-line"),
    ],
)
def test_a_definition_in_a_quote_or_list_item_is_read(text: str, line: int) -> None:
    assert _read(text) == [("next-q.md", "reference", line, "next-q.md")]
    assert markdown_links(text) == []


def test_links_after_an_unclosed_fence_are_read() -> None:
    text = "```\n[a](after-unclosed.md)\n"

    assert _read(text) == [("after-unclosed.md", "inline", 2, "after-unclosed.md")]
    assert markdown_links(text) == []


def test_a_quote_in_a_destination_ends_the_address_as_the_site_reads_it() -> None:
    # The #828 class: the site links up to the quote and takes the next link into a title.
    text = "[a](https://x.org/#b), [c](https://x.org/#b'), [d](https://x.org/#b'').\n"

    assert [(link.href, link.title, link.span) for link in site_links(text)] == [
        ("https://x.org/#b", None, (4, 20)),
        ("https://x.org/#b", "), [d](https://x.org/#b'", (27, 43)),
    ]


@pytest.mark.parametrize(
    ("text", "section"),
    [
        pytest.param("## A\n\n#Depends on\n\n[a](a.md)\n", None, id="unspaced-h1"),
        pytest.param("## A\n\n   ## Depends on\n\n[a](a.md)\n", "A", id="indented-paragraph"),
        pytest.param("## A\n\n    ## Depends on\n\n[a](a.md)\n", "A", id="indented-code"),
        pytest.param("## Depends on {#deps}\n\n[a](a.md)\n", "Depends on", id="attribute-list"),
        pytest.param("## [Part](part.md)\n", "Part", id="link-in-heading"),
        pytest.param(
            "## Equilibrium (`R < Vmax`, `a & b`)\n\n[a](a.md)\n", "Equilibrium (R < Vmax, a & b)", id="code-span"
        ),
        pytest.param("## a &lt b\n\n[a](a.md)\n", "a &lt b", id="no-semicolon-lt"),
        pytest.param("## R&copy 2\n\n[a](a.md)\n", "R&copy 2", id="no-semicolon-copy"),
    ],
)
def test_sections_follow_the_site_s_headings(text: str, section: str | None) -> None:
    # D3: sections are the site's H2 headings, as it shows their text; an H1, here one with no space, ends one.
    assert [link.section for link in site_links(text)] == [section]


def test_a_title_below_a_section_ends_that_section() -> None:
    # The page shows the H1 over what follows it, as the loader reads a title.
    text = "Intro.\n\n## Depends on\n\n- [Base](base.md)\n\n# Top\n\nSee [Other](other.md).\n"

    assert [(link.href, link.section) for link in site_links(text)] == [("base.md", "Depends on"), ("other.md", None)]


#: A definition in a quote with nothing after its colon, and an empty quoted line below it: the site reads text, and
#: links no use of its label. The second is the shape of a Stacks page on trough.
EMPTY_QUOTED_DEFINITIONS = [
    pytest.param("> See [x].\n>\n> [x]:\n>\n> text\n", id="minimal"),
    pytest.param(
        "## Tag 01XN, a lemma\n\n"
        "> **Stacks Project, Tag 01XN.**  Let `f` be quasi-compact, with the\n"
        "> notation of the cartesian square of [Stacks, Tag 02KF].\n\n"
        "> **The left-to-right map of [Stacks, Tag 0GN5]** for the cartesian square of schemes of\n"
        "> [Stacks, Tag 02KF]:\n"
        ">\n"
        "> `G ⊗ Lg^*Rf_*F ⟶ Rf'_*(L(f')^*G ⊗ L(g')^*F)`.\n"
        ">\n",
        id="stacks",
    ),
]


@pytest.mark.parametrize("text", EMPTY_QUOTED_DEFINITIONS)
def test_an_empty_quoted_definition_links_nothing(text: str) -> None:
    assert _site_anchors(text) == Counter()
    assert site_links(text) == []


#: A `>` below a definition's colon that is not the definition's own quote prefix: indented past it, the site reads
#: it as the destination, and links each use of the label to ''.
MARKER_DESTINATIONS = [
    pytest.param("See [x].\n\n[x]:\n    >\n", id="indented"),
    pytest.param("See [x].\n\n[x]:\n\t>\n", id="tab"),
    pytest.param("> See [x].\n>\n> [x]:\n>     >\n", id="indented-in-a-quote"),
]


@pytest.mark.parametrize("text", MARKER_DESTINATIONS)
def test_a_marker_the_site_reads_as_a_destination_is_placed(text: str) -> None:
    assert _site_anchors(text) == Counter({("", None): 1})
    assert [(link.href, link.kind) for link in site_links(text)] == [("", "reference")]


@pytest.mark.xfail(strict=True, reason="known limit: only a line that is the definition's own quote prefix is skipped")
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("- See [x].\n\n    [x]:\n    >\n\n    text\n", id="quote-in-a-list-item"),
        pytest.param("> See [x].\n>\n> [x]:\n>    >\n", id="inner-quote-marker"),
    ],
)
def test_an_empty_quote_deeper_than_the_definition_still_takes_a_mark(text: str) -> None:
    _assert_inert_and_placed(text, verbatim=False)


# The marks, on pages the agreement grammar writes, with autolinks and headings added.


#: Tabs in destinations and titles, and before and after a link on its line: the site expands each to the next
#: stop of four, so a mark that is not a whole number of stops long moves every tab after it.
TAB_CASES = [
    "[a](<x\ty>)\n",
    '[a](x.md "t\tu")\n',
    "ab\t[a](x.md)\tc\td\n",
    'x\t[a](<p\tq.md> "r\ts")\t[b](b.md "u\tv")\t[c](<w\tz.md>)\n',
    '[a][r]\tand\t[b][s]\n\n[r]:\tr.md\t"t\tu"\n[s]:\n\ts.md\n',
    "x\t<https://example.org/a>\ty\t<a@example.org>\tz\n",
    '> q\t[a](<x\ty> "t\tu")\tz\n\n- i\t[b](b.md "v\tw")\tz\n',
]


def _grammar_pages() -> list[str]:
    rng = random.Random(SEED)
    pages = []
    for index in range(300):
        text = _Page(rng).text()
        if index % 3 == 0:
            text = f"## Depends on\n\n{text}\n<https://example.org/{index}> and <a{index}@example.org>\n"
        if index % 3 == 1:
            # Tabs where the grammar writes spaces past a line's start, and a link with tabs in it.
            text = re.sub(r"(?<=\S) ", lambda space: "\t" if rng.random() < 0.5 else " ", text)
            text += f'\nx\t[t{index}](<d\t{index}.md> "t\ti")\ty\n'
        pages.append(text)
    return pages


def _assert_inert_and_placed(text: str, *, verbatim: bool) -> None:
    links = site_links(text)
    assert Counter((link.href, link.title) for link in links) == _site_anchors(text), text
    for link in links:
        assert link.kind is not None and link.line is not None, (text, link)
        if link.span is not None:
            assert text[slice(*link.span)] == link.href, (text, link)
        elif verbatim and not link.href.startswith("mailto:") and " " not in link.href:
            raise AssertionError((text, link))
    # Each inline link and autolink has a place of its own; reference links share their definition's.
    own = [link.span for link in links if link.kind != "reference" and link.span]
    assert len(own) == len(set(own)), text


def test_marks_change_no_link_and_place_every_one() -> None:
    for text in _grammar_pages():
        _assert_inert_and_placed(text, verbatim=True)


@pytest.mark.parametrize(
    "source",
    [source for source, _ in LINK_CORPUS + LINK_DESTINATIONS]
    + TAB_CASES
    + [case.values[0] for case in EMPTY_QUOTED_DEFINITIONS + MARKER_DESTINATIONS],
)
def test_marks_change_no_link_and_place_every_one_in_the_corpora(source: str) -> None:
    _assert_inert_and_placed(source, verbatim=False)


def test_a_tab_after_a_mark_keeps_its_stop() -> None:
    # cove-5f's case on #845: the site expands the tab to two spaces, to the stop after 'x'.
    assert [link.href for link in site_links("[a](<x\ty>)\n")] == ["x  y"]
    assert [(link.href, link.title) for link in site_links('[a](x.md "t\tu")\n')] == [("x.md", "t u")]


def test_marks_never_collide_with_the_page_s_own_private_use_characters() -> None:
    text = "\ue000\ue001 [a](a.md) \ue0000\ue001\n"

    assert _read(text) == [("a.md", "inline", 1, "a.md")]


# Fail closed.


def test_a_page_too_deep_for_the_converter_raises_and_spoils_no_other() -> None:
    with pytest.raises(SiteReadingError, match="converter fails on this page"):
        site_links("- " * 500 + "[a](a.md)\n")

    assert _read("[a](a.md)\n") == [("a.md", "inline", 1, "a.md")]


def test_a_converter_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(self: BlockParser, lines: list[str]) -> None:
        raise ValueError("the converter failed")

    monkeypatch.setattr(BlockParser, "parseDocument", fail)

    with pytest.raises(SiteReadingError, match="the converter failed"):
        site_links("[a](a.md)\n")
