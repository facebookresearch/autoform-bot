from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from autoform_cli.markdown import (
    SITE_EXTENSION_CONFIGS,
    SITE_EXTENSIONS,
    ArticleSection,
    _PAGE_CODE_OR_COMMENT,
    _comments_blanked,
    article_parts,
    content,
    content_lines,
    directory_path,
    has_substance,
    is_placeholder,
    local_target_issue,
    markdown_anchors,
    markdown_links,
    mask_code_and_comments,
    mask_fences_and_comments,
    outside_fences,
    published_page,
    PublishedTable,
    published_tables,
    render_html,
    rendered_visible_text,
    resolved_path,
    only_links,
    site_converter,
    strip_line_comments,
    visible_prose,
)
from autoform_cli.ui.publishable import publishable_article

#: Heading forms whose published anchors are easy to get subtly wrong, paired
#: with what the configured MkDocs renderer actually publishes for them. Several
#: of these depend on block context or on a specific extension setting rather
#: than on the heading line alone, which is why anchors are taken from the
#: renderer instead of predicted.
ANCHOR_CORPUS = [
    ("# Depends on", ["depends-on"]),
    ("# Café", ["cafe"]),
    ("# Naïve Bayes — dashes", ["naive-bayes-dashes"]),
    ("# [Linked result](other.md)", ["linked-result"]),
    ("# `Code` heading", ["code-heading"]),
    ("# *Emphasised* result", ["emphasised-result"]),
    ("# Heading with &amp; entity", ["heading-with-entity"]),
    ("# Title {.class}", ["title"]),
    (r"# Title \{.class\}", ["title-class"]),
    ("# Result {#custom-id}", ["custom-id"]),
    ("# Trailing hashes ###", ["trailing-hashes"]),
    ("# 1. Numbered", ["1-numbered"]),
    ("# Depends on\n\n## Depends on\n\n### Depends on", ["depends-on", "depends-on_1", "depends-on_2"]),
    (
        "# Roadmap article\n\n## Depends on\n\n## Depends on\n\n## Result {#custom-id}",
        ["roadmap-article", "depends-on", "depends-on_1", "custom-id"],
    ),
    # An explicit ID is emitted verbatim and never uniquified, but it is
    # reserved before any heading is slugged.
    ("# A {#dup}\n\n# B {#dup}", ["dup"]),
    ("# Depends on\n\n# B {#depends-on}", ["depends-on", "depends-on_1"]),
    ("# ***", ["_1"]),
    ("Setext one\n===", ["setext-one"]),
    ("Setext two\n---", ["setext-two"]),
    # MkDocs strips frontmatter before Markdown sees it, so a setext-looking
    # closing delimiter must not turn a YAML key into a heading.
    ("---\nkind: blueprint\n---\n\n# Real heading", ["real-heading"]),
    # `arithmatex` in generic mode leaves one copy of the maths for the slugger.
    ("# $x + y$", ["x-y"]),
    # Block context: these headings publish anchors even though they do not
    # start their line, and the raw HTML block suppresses one that does.
    ("> # Quoted heading", ["quoted-heading"]),
    ("- # Item heading", ["item-heading"]),
    ("<div>\n# Not a heading\n</div>", []),
    ("```\n# Fenced heading\n```", []),
    ("<!-- # Commented heading -->", []),
    ("    # Indented heading", []),
]


@pytest.mark.parametrize(("source", "expected"), ANCHOR_CORPUS)
def test_anchors_are_what_the_configured_renderer_publishes(
    tmp_path: Path, source: str, expected: list[str]
) -> None:
    article = tmp_path / "article.md"
    article.write_text(source + "\n", encoding="utf-8")

    assert markdown_anchors(article) == set(expected)


def test_the_extension_config_matches_the_scaffolded_mkdocs_yml() -> None:
    """The checker's idea of the site config must be the site's config.

    Anchor prediction is only as good as the extension list it runs, and that
    list lives in the template MkDocs actually builds with. Enabling a new
    heading-affecting extension there without telling this module would make the
    audit disagree with the published page, so bind the two together.
    """

    template = (
        Path(__file__).resolve().parents[1] / "autoform_cli/templates/mkdocs.yml"
    ).read_text(encoding="utf-8")
    block = template[template.index("markdown_extensions:") :]
    block = block[: block.index("\nextra_css:")]

    declared = set(re.findall(r"^  - ([\w.]+):?(?:\s+#.*)?$", block, re.MULTILINE))

    assert declared == set(SITE_EXTENSIONS)
    # Settings that change heading IDs have to agree too, not just the names.
    assert "toc_depth: 2-3" in block
    assert SITE_EXTENSION_CONFIGS["toc"] == {"toc_depth": "2-3"}
    assert "generic: true" in block
    assert SITE_EXTENSION_CONFIGS["pymdownx.arithmatex"] == {"generic": True}


#: Pins the Pages build may keep below uv.lock. mkdocs-literate-nav 0.6.2
#: requires only mkdocs>=1.4.1, so it resolves beside the pinned mkdocs; the
#: lock's 0.6.3 would also install properdocs.
PAGES_PINS_BELOW_THE_LOCK = {"mkdocs-literate-nav": "0.6.2"}


def test_the_pages_build_pins_the_renderer_and_the_locked_versions() -> None:
    """Checks and the published site parse pages with the same exact renderer versions,
    and the build's other pins are the set uv.lock resolved."""

    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    locked: dict[str, set[str]] = {}
    with (root / "uv.lock").open("rb") as handle:
        for package in tomllib.load(handle)["package"]:
            locked.setdefault(package["name"], set()).add(package["version"])
    pages = (root / "autoform_cli/templates/github/workflows/blueprint-pages.yml").read_text(encoding="utf-8")
    for package in ("markdown", "pymdown-extensions"):
        (pin,) = re.findall(rf'^    "{re.escape(package)}==([^"]+)",$', pyproject, re.MULTILINE)
        assert re.findall(rf"--with {re.escape(package)}==(\S+)", pages) == [pin], package
    pinned = dict(re.findall(r"--(?:from|with) ([\w.-]+)==(\S+)", pages))

    assert "mkdocs-material" in pinned
    drifted = {
        name: (version, sorted(locked[name]))
        for name, version in pinned.items()
        if name in locked and locked[name] != {version} and PAGES_PINS_BELOW_THE_LOCK.get(name) != version
    }
    assert drifted == {}, "Pages pins that differ from uv.lock"


def test_rendered_visible_text_is_what_a_reader_sees() -> None:
    def visible(value: str) -> str:
        return rendered_visible_text(value).strip()

    assert visible("[Node](../roadmap/node.md)") == "Node"
    assert visible("[ ](missing.md)") == ""
    assert visible("<span></span>") == ""
    assert visible("&amp; &nbsp;").startswith("&")
    assert visible("![alt](image.png)") == ""
    # Text a browser never shows is not text a reader sees.
    assert visible("<script>reason</script>") == ""
    assert visible("<span hidden>reason</span>") == ""
    # Browsers close an unclosed element and keep hiding its contents.
    assert visible("<span hidden>reason") == ""
    # `hidden` has to be the attribute, not any occurrence of the word.
    assert visible('<span title="hidden">real reason</span>') == "real reason"
    assert visible('<span aria-hidden="true">real reason</span>') == "real reason"
    # A nested copy of the tag must not close the suppression early.
    assert visible("<div hidden>a<div>b</div>c</div>") == ""
    # Browsers repair markup rather than reject it, and the repair decides what
    # stays hidden. Self-closing syntax does not apply to a non-void element, so
    # the span stays open; a second <p> implicitly closes the first, so it does not.
    assert visible("<span hidden />Reason") == ""
    assert visible("<p hidden>aside<p>Real reason") == "Real reason"
    # A comment is markup, not content a reader sees.
    assert visible("real <!-- aside --> reason") == "real reason"


def test_published_tables_reports_rows_as_a_reader_sees_them() -> None:
    tables = published_tables(
        "| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
        "| Main | OUT | [Node](node.md) |\n"
    )

    assert len(tables) == 1
    assert tables[0].headers == ("Area", "Coverage", "Evidence")
    # The link label, not its destination.
    assert tables[0].rows == (("Main", "OUT", "Node"),)
    # A paragraph running into a table publishes no table.
    assert published_tables("Intro\n| Area | Coverage |\n| --- | --- |\n| a | b |\n") == []


def test_published_tables_skips_what_a_reader_cannot_see() -> None:
    table = "<table><tr><th>A</th></tr><tr><td>b</td></tr></table>"

    # Hiding propagates from an ancestor, not just from the table itself.
    assert published_tables(f"<div hidden>\n{table}\n</div>\n") == []
    assert published_tables(table.replace("<table>", "<table hidden>")) == []
    # A hidden row drops out while its visible siblings remain.
    rows = published_tables(
        "<table><tr><th>A</th></tr><tr hidden><td>gone</td></tr><tr><td>kept</td></tr></table>"
    )
    assert [row for table_ in rows for row in table_.rows] == [("kept",)]
    # Keeping a concealed cell as an empty string invents a column no reader
    # sees, which both disguises a table whose visible headers match and
    # manufactures mismatches in one whose rows do.
    tables = published_tables(
        "<table><tr><th>Area</th><th>Coverage</th><th>Evidence</th><th hidden>Notes</th></tr>"
        "<tr><td>a</td><td>b</td><td>c</td><td hidden>aside</td></tr></table>"
    )
    assert tables == [
        PublishedTable(headers=("Area", "Coverage", "Evidence"), rows=(("a", "b", "c"),))
    ]


#: Text paired with the lines the checks read in it: everything the site does
#: not publish is blank, and every line keeps its number.
CONTENT_LINES = [
    # The tail keeps its own line number, which is what diagnostics report.
    pytest.param(
        "# Title\n\n```markdown\n| Area | Coverage | Evidence |\n```\n\n<!-- hidden\nstill hidden\n-->\n\n"
        "    indented code\n\nvisible tail\n",
        ["# Title", *[""] * 11, "visible tail"],
        id="code-blocks-and-comments",
    ),
    pytest.param("```\n<!--\n```\n\nvisible\n", ["", "", "", "", "visible"], id="comment-opener-inside-a-fence"),
    pytest.param("<!--\n```\n-->\n\nvisible\n", ["", "", "", "", "visible"], id="fence-inside-a-comment"),
    pytest.param("| OUT | real <!-- aside --> reason |\n", ["| OUT | real  reason |"], id="text-beside-a-comment"),
    pytest.param(
        "- item\n\n    continuation of the item\n",
        ["- item", "", "    continuation of the item"],
        id="indented-content-under-a-list-item",
    ),
    # List context has to survive the blank lines between paragraphs, or the
    # second one is mistaken for a code block and its links go unchecked.
    pytest.param(
        "- item\n\n    first continuation\n\n    second continuation\n\ntail\n",
        ["- item", "", "    first continuation", "", "    second continuation", "", "tail"],
        id="every-continuation-paragraph-in-a-list",
    ),
    pytest.param("    indented code\n\nvisible\n", ["", "", "visible"], id="indented-code-at-the-start"),
    # pymdownx.superfences keeps a closing fence with trailing text inside the
    # code block, so anything after it is still fenced and must stay masked: the
    # checks that read tables keep that fence, and a table in its paragraph is
    # not published.
    pytest.param("```\nfenced\n``` trailing\nstill fenced\n", ["", "", "", ""], id="closing-fence-with-trailing-text"),
    pytest.param("```\nfenced\n```\npublished\n", ["", "", "", "published"], id="bare-closing-fence"),
    # The statement of an article whose title line opens a comment, which still
    # hides what it covers.
    pytest.param("<!-- TODO:\nwrite the statement -->", ["", ""], id="comment-opened-on-the-title-line"),
]


@pytest.mark.parametrize(("text", "expected"), CONTENT_LINES)
def test_masking_blanks_what_the_site_hides_without_moving_lines(text: str, expected: list[str]) -> None:
    assert content_lines(text) == expected


def test_render_s_line_transforms_read_no_fence_inside_a_comment() -> None:
    """Issue #527: a fence line in a comment hid every later line from render's link rewriting."""
    text = "<!--\n```lean\n-->\nafter\n```\n<!--\n```\nlast\n"

    assert outside_fences(text, lambda line: f"+{line}") == "+<!--\n+```lean\n+-->\n+after\n```\n<!--\n```\n+last\n"


def test_render_s_line_transforms_read_a_comment_that_never_closes_as_text() -> None:
    """The site shows such a comment, and the code after it, as typed; so is one opened in a code span."""
    for opener in ("<!-- note", "Write `<!--` to open one."):
        text = f"{opener}\n\n```text\nsee [x](a.md)\n```\nafter\n"

        assert outside_fences(text, lambda line: f"+{line}") == f"+{opener}\n+\n```text\nsee [x](a.md)\n```\n+after\n"

    # The comment left open is the one reopened on the line that closes the first, so the fence line is no fence.
    reopened = "<!--\n```\n--> text <!-- open\nsee [x](a.md)\n"
    assert outside_fences(reopened, lambda line: f"+{line}") == "+<!--\n+```\n+--> text <!-- open\n+see [x](a.md)\n"


def test_render_s_line_transforms_read_a_comment_opener_in_code_as_text() -> None:
    """A `<!--` in a code span or an indented code block opens no comment on the site, though in indented code a later
    `-->` would close one for the graph's reader; the fenced block between them is code and stays as typed."""
    pairs = (("Write `<!--` to open a comment.", "Close it with `-->`."), ("    <!-- in code", "    --> too"))
    for opener, closer in pairs:
        text = f"{opener}\n\n```text\nsee [x](a.md)\n# not a heading\n```\n\n{closer}\n"

        assert outside_fences(text, lambda line: f"+{line}") == (
            f"+{opener}\n+\n```text\nsee [x](a.md)\n# not a heading\n```\n+\n+{closer}\n"
        )


@pytest.mark.parametrize(
    ("line", "open_before", "expected"),
    [
        ("Write `<!--` to open one.", False, ("Write `<!--` to open one.", False)),
        ("``a`<!--`` b", False, ("``a`<!--`` b", False)),
        ("a `x` b <!-- c", False, ("a `x` b ", True)),
        ("x <!-- y `z`", False, ("x ", True)),
        # An escaped backtick opens no span, as on the page.
        ("a \\` b <!-- c \\` d", False, ("a \\` b ", True)),
        ("--> then `<!--` stays text", True, (" then `<!--` stays text", False)),
        ("x `y` z `<!--` w", False, ("x `y` z `<!--` w", False)),
        ("Write `<!--`", False, ("Write `<!--`", False)),
        # An escape before the span opens it still.
        ("a\\* `<!--` b", False, ("a\\* `<!--` b", False)),
        # An escaped backslash leaves the backtick after it free to open a span, as on the page.
        ("a \\\\`x` <!-- c `z` -->", False, ("a \\\\`x` ", False)),
        ("a \\\\`x <!-- old --> y` <!-- c ` -->", False, ("a \\\\`x <!-- old --> y` ", False)),
        # So does part of a longer run.
        ("a `` b <!-- c `d`", False, ("a `` b <!-- c `d`", False)),
        # Only a span that starts before the `<!--` keeps it as text.
        ("a ` b <!-- c ``d``", False, ("a ` b ", True)),
    ],
)
def test_a_comment_opener_in_a_code_span_is_text(line: str, open_before: bool, expected: tuple[str, bool]) -> None:
    """Issue #657: the graph and the audit read a `<!--` in a code span as text, as the page does."""

    assert strip_line_comments(line, open_before) == expected


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        # A span that wraps onto the next line closes at that line's first backtick, so a backtick inside a comment
        # there pairs with nothing; read alone, the line would pair the two and show the comment as text.
        (["a `x", "y` <!-- QH ` QI -->", "QA"], ["a `x", "y` ", "QA"]),
        # The rest of the paragraph reads every opener, even after a line that looks balanced alone.
        (["a `x", "y` z ` v", "q` <!-- `[H](h.md)` -->"], ["a `x", "y` z ` v", "q` "]),
        # A blank line ends the paragraph, and the span with it, but not inside a comment the page may never open.
        (["a `x", "", "`<!--` b"], ["a `x", "", "`<!--` b"]),
        (["a `b <!-- c", "d` e", "", "f `g -->", "h` <!-- `[H](h.md)` -->"], ["a `b ", "", "", "", "h` "]),
        # The site can keep a fence inside the paragraph, so only a blank line before it starts afresh.
        (["a `x", "```", "code", "```", "y` <!-- c ` d -->"], ["a `x", "", "", "", "y` "]),
        (["a `x", "", "```", "code", "```", "`<!--` b"], ["a `x", "", "", "", "", "`<!--` b"]),
        # A fence that never closes is text again.
        (["a `x", "~~~", "y` <!-- c ` d -->", "e"], ["a `x", "~~~", "y` ", "e"]),
        # A span that closes on its own line leaves the next line's spans to read, as does an escaped backtick.
        (["a `x` b", "`<!--` c"], ["a `x` b", "`<!--` c"]),
        (["a \\` b", "`<!--` c"], ["a \\` b", "`<!--` c"]),
        # A comment is cut out of the line itself, so the text either side of it does not join.
        (["x \\<!-- c -->`y", "z` <!-- `[H](h.md)` -->"], ["x \\`y", "z` "]),
        (["x \\`<!-- c -->`y", "z` <!-- `[H](h.md)` -->"], ["x \\``y", "z` "]),
        (["a `<!-- c -->`` b ``` d", "e` <!-- `[H](h.md)` -->"], ["a ``` b ``` d", "e` "]),
        # A backtick inside a comment pairs with nothing outside it.
        (["a <!-- x", "y ` --> `c`", "`<!--` d"], ["a ", " `c`", "`<!--` d"]),
        (["`c` <!-- x `", "y --> `<!--` z"], ["`c` ", " `<!--` z"]),
    ],
)
def test_a_line_that_may_continue_a_span_reads_every_comment_opener(lines: list[str], expected: list[str]) -> None:
    """Issue #657: a `<!--` in a code span is text only where the line starts outside any span the paragraph opened
    above; elsewhere every `<!--` opens a comment, as before."""

    assert mask_fences_and_comments(lines) == expected


def test_a_line_read_as_continuing_a_span_opens_every_comment() -> None:
    assert strip_line_comments("y` <!-- QH ` QI -->", False, spans=False) == ("y` ", False)


def test_a_heading_in_a_comment_after_a_wrapped_span_splits_nothing() -> None:
    parts = article_parts(
        "# Result\n\nRun `lake\nbuild` <!-- `old` draft\n\n## Proof\n\nold proof -->\n\n"
        "Statement continues.\n\n## Depends on\n\n- [Base](base.md)\n"
    )

    assert [section.title for section in parts.sections] == ["Depends on"]


def test_a_title_that_may_continue_a_span_keeps_the_comment_it_opens_in_the_statement() -> None:
    """The title line is read as the masking reads it, so a comment it opens, there because a span from the line
    above may run into it, still hides what follows in the statement."""

    statement = article_parts("Intro `a\n# Title ` <!--` c\n[H](h.md) -->\n\nBody.\n").statement

    assert statement == "Intro `a\n<!--` c\n[H](h.md) -->\n\nBody."


@pytest.mark.parametrize(
    "text",
    [
        "Intro x \\<!-- c -->`y\n  z` <!-- `q`\n```\nsecret\n```\n-->\n",
        "Intro a `b <!-- c\n  d` e\n\nf `g -->\n  h` <!-- `q`\n```\nsecret\n```\n-->\n",
    ],
    ids=["joined", "blank-line-in-comment"],
)
def test_a_fence_inside_a_comment_after_a_wrapped_span_is_not_published(text: str) -> None:
    """The page finds its code blocks through the same masking, so a comment the masking missed would publish the
    fence inside it as code."""

    assert "secret" not in published_page(text)


#: A comment after a code span that wraps onto its line, over a heading. On the
#: page the span closes before the comment, which must come out whole.
_DRAFT = " <!-- `old` draft\n\n## Proof\n\nold -->\n\nAfter.\n"


@pytest.mark.parametrize(
    ("text", "published"),
    [
        # An even run of backslashes leaves the backtick after it free to open the span.
        pytest.param(f"Run \\\\`lake\nbuild`{_DRAFT}", "Run \\\\`lake\nbuild` \n\nAfter.\n", id="escaped-backslash"),
        # So does part of a longer run.
        pytest.param(f"Run ``lake\nbuild`{_DRAFT}", "Run ``lake\nbuild` \n\nAfter.\n", id="part-of-a-run"),
        # The site keeps a fenced block with no blank line before it in the paragraph.
        pytest.param(
            f"Run `lake\n```\ncode\n```\nbuild`{_DRAFT}", "Run `lake\n```\ncode\n```\nbuild` \n\nAfter.\n", id="fence"
        ),
        # Taking out the first comment joins the backticks either side of it, which the site then pairs.
        pytest.param(f"Run `lake <!-- c\n\nd --> build`{_DRAFT}", "Run `lake  build` \n\nAfter.\n", id="joined"),
        pytest.param(
            "Run `lake <!-- c\n\nd --> build` <!-- `old`\n```\nsecret\n```\n-->\n",
            "Run `lake  build` \n",
            id="joined-fence",
        ),
    ],
)
def test_the_page_pairs_code_spans_as_the_site_does(text: str, published: str) -> None:
    """Issue #657: a code span the page missed kept a comment the site then
    showed, from its `<!--` to its `-->`."""

    page = published_page(text).removeprefix("---\n{}\n---\n")

    assert page == published
    assert "Proof" not in site_converter().convert(page)


def test_render_s_line_transforms_read_no_fence_in_a_comment_a_join_brings_out() -> None:
    """Render's line transforms read the fences the page has, so none in the
    comment that taking out the first one leaves outside any span."""

    text = "Run `lake <!-- c\n\nd --> build` <!-- `old`\n```\nsecret\n```\n-->\n"

    assert outside_fences(text, lambda line: f"+{line}") == "".join(f"+{line}\n" for line in text.splitlines())


@pytest.mark.parametrize(
    "rest",
    ["\n\n## Depends on\n\n- [Other](other.md)\n\n-->\n", "\n```\ncode\n```\ne -->\n"],
    ids=["paragraphs", "fence"],
)
def test_a_backslash_taking_a_comment_out_brings_up_to_a_backtick_escapes_nothing(rest: str) -> None:
    """Review of #657: read as an escape, the joined backslash and backtick
    left the second `<!--` outside any span, and taking that out too lost the
    rest of the page without a word. The page reads the backslash as it did
    before, so the `<!--` stays and check names it, as on main."""

    text = f"x \\<!-- c -->`<!--` d{rest}"

    assert published_page(text).removeprefix("---\n{}\n---\n") == f"x \\`<!--` d{rest}"


@pytest.mark.parametrize(
    "text",
    [
        "Write `<!--`<!-- c -->` to open.\n\n## Proof\n\n- [Other](other.md)\n\n-->\n",
        "``a``<!-- c -->`b`\nc `` <!-- `\n\n## Proof\n\n- [Other](other.md)\n\n-->\n",
        "`a`<!-- c -->`` x ` y <!-- [Other](other.md) `` -->\n",
    ],
    ids=["closer", "next-line", "same-line"],
)
def test_runs_of_backticks_taking_a_comment_out_brings_together_pair_as_they_did_apart(text: str) -> None:
    """Review of #657: read as one run, two runs that taking a comment out
    joined paired otherwise, which left a later `<!--` outside any span, and
    taking that out too lost the rest of the page without a word, while the
    graph still read it. The page pairs them as the runs they were, so that
    `<!--` stays and check names it."""

    assert published_page(text).removeprefix("---\n{}\n---\n") == text.replace("<!-- c -->", "")


def test_a_comment_takes_no_fenced_block_the_masking_reads_as_code_with_it() -> None:
    """The masking reads the second `<!--` as text in a span, and the block
    below it as code. Taking out the first comment joins two runs of
    backticks, which then pair before that `<!--`, but a comment from there
    would take the code block with it, so it stays in."""

    text = "x `<!-- c -->`` q ``` <!-- y `` z\n```\ncode\n```\nw -->\n"

    assert published_page(text).removeprefix("---\n{}\n---\n") == "x ``` q ``` <!-- y `` z\n```\ncode\n```\nw -->\n"


#: Pages whose code spans turn on backslashes or on the length of a backtick
#: run, marked with what the site does with the text: QS shows it, and QH
#: hides it in a comment.
_SPAN_PAGES = [
    pytest.param("# QS1\n\nRun \\\\`QS2\nQS3` <!-- `old` QH1\n\n## QH2\n\nQH3 -->\n\nQS4\n", id="escaped-backslash"),
    pytest.param("Run \\\\`QS1\nQS2` <!-- `old` QH1 --> QS3\n", id="escaped-backslash-one-line"),
    pytest.param("Run \\`QS1\nQS2` <!-- `x` QS3\n\n## QS4\n\nQS5 -->\n\nQS6\n", id="escaped-backtick"),
    pytest.param("Run \\\\\\`QS1\nQS2` <!-- `x` QS3\n\n## QS4\n\nQS5 -->\n\nQS6\n", id="three-backslashes"),
    pytest.param("Run \\\\\\\\`QS1\nQS2` <!-- `x` QH1\n\n## QH2\n\nQH3 -->\n\nQS3\n", id="four-backslashes"),
    pytest.param("a \\\\`x <!-- QS1 ` QS2\n\n## QS3\n\nQS4 -->\n\nQS5\n", id="escaped-backslash-same-line"),
    pytest.param("# Result \\\\`a <!-- QS1 ` QS2\n\n## QS3\n\nQS4 -->\n\nQS5\n", id="escaped-backslash-title"),
    pytest.param("a \\\\`x <!-- QS1 --> y` <!-- QH1 ` QH2 -->\nQS2\n", id="escaped-backslash-span-holds-a-comment"),
    pytest.param("Price 5\\` QS1\nWrite `<!--` QS2 here.\n\nQS3 more.\n\n<!-- QH1 -->\n\nQS4\n", id="escaped-backtick-above"),
    pytest.param("a \\` b `<!--` QS1\n\nQS2\n\n<!-- QH1 -->\n\nQS3\n", id="escaped-backtick-before"),
    pytest.param("Run ``QS1\nQS2` <!-- `x` QH1\n\n## QH2\n\nQH3 -->\n\nQS3\n", id="part-of-a-run"),
    pytest.param("``QS1` <!-- QH1` d\n\n## QH2\n\nQH3 -->\n\nQS2\n", id="part-of-a-run-first"),
    pytest.param("``a` b` <!-- QS1` QS2 -->\n\nQS3\n", id="part-of-a-run-same-line"),
    pytest.param("The ``QS1'' QS2\nwrite `<!--` QH1.\n\nQH2\n\n<!-- QH3 -->\n\nQS3\n", id="tex-quotes"),
    # Alone, the first line pairs by part of its run; the paragraph pairs the whole run with the second line's.
    pytest.param(
        "QS1 a `` QS2 `\nQS3 `` <!-- `\n\n## QH1\n\n- [QH2](l.md) QH3\n\n-->\n\nQS4\n", id="part-of-a-run-on-the-line-above"
    ),
]

#: More marked pages: a span that crosses a fenced block, a comment taken out
#: or a line, layouts the page read right before, and line breaks where the
#: site starts a new block, which no span crosses. QC marks text shown in a
#: code block, and QU text after a comment that never closes, which the page
#: keeps and check names.
_MARKED_PAGES = _SPAN_PAGES + [
    pytest.param("Run `QS1\n```\nQC1\n```\nQS2` <!-- `old` QH1\n\n## QH2\n\nQH3 -->\n\nQS3\n", id="fence"),
    pytest.param("Run `QS1\n```\nQC1\n```\nQS2` <!-- `old` QH1 --> QS3\n", id="fence-one-line"),
    pytest.param("- a `QS1\n  ```\n  QC1\n  ```\n  QS2` <!-- `x` QH1\n\n## QH2\n\nQH3 -->\n\nQS3\n", id="fence-in-a-list"),
    pytest.param("Run `QS1\n\n```\nQC1\n```\n\nQS2` <!-- `x` QS3\n\n## QS4\n\nQS5 -->\n\nQS6\n", id="fence-apart"),
    pytest.param("Run `QS1 <!-- QH1\n\nQH2 --> QS2` <!-- `old` QH3\n\n## QH4\n\nQH5 -->\n\nQS3\n", id="joined"),
    pytest.param("Run `QS1 <!-- QH1\n\nQH2 --> QS2` <!-- `old` QH3 --> QS3\n", id="joined-one-line"),
    pytest.param("Run `QS1 <!-- QH1\n\nQH2 --> QS2` <!-- `x`\n```\nQH3\n```\n-->\nQS3\n", id="joined-fence"),
    pytest.param("A span `a <!-- QS1\n--> c` QS2 crosses a line.\n", id="wrapped-span"),
    pytest.param("Use `<!-- QS1\n` to open QS2.\n\nQS3 text.\n\n<!-- QH1 -->\n\nQS4\n", id="wrapped-span-then-comment"),
    pytest.param("- Use `<!-- QS1\n  ` here QS2.\n\nQS3 -->\n\nQS4\n", id="wrapped-span-in-a-list"),
    pytest.param("Intro a `QS1 <!-- QS2\n  QS3` QS4\n\nQS5 `QS6 -->\n  QS7` <!-- `QH1`\n```\nQH2\n```\n-->\n", id="pr-725"),
    pytest.param("a `<!-- QS1 -->`` b ``` d\ne` QS2 <!-- QH1 -->\nQS3\n", id="span-holds-a-comment"),
    pytest.param("Para.\n\n    <!-- QC1\n\nQS1 para\n\n    --> QC2\n", id="indented-code"),
    pytest.param("Write `<!--` to start a comment and `-->` to end it. QS1\n\nQS2\n\n<!-- QH1 -->\n\nQS3\n", id="doc-page"),
    pytest.param("| a | b |\n|---|---|\n| QS1 <!-- QH1 --> | `<!--` QS2 |\n| QS3 | z |\n", id="table"),
    pytest.param("a <!-- QH1 --> QS1 <!-- QU1 never closes\n\nQU2\n", id="never-closes"),
    pytest.param("Run `QS1\n \nQS2 <!-- QH1 --> QS3` QS4\n", id="whitespace-line"),
    # A span ends where the site starts a new block, so a later backtick closes none: review of #745.
    pytest.param("a ``QS1\n## QS2\nb <!-- QH1 --> `QS3` c\n", id="heading"),
    pytest.param("a ``QS1\n***\nb <!-- QH1 --> `QS2` c\n", id="thematic-break"),
    pytest.param("a ``QS1\n===\nb <!-- QH1 --> `QS2` c\n", id="setext-underline"),
    pytest.param("a ``QS1\n> b <!-- QH1 --> `QS2` c\n", id="quote"),
    pytest.param("- a ``QS1\n- b <!-- QH1 --> `QS2` c\n", id="list-item"),
    pytest.param("| a ``QS1 |\n| --- |\n| b <!-- QH1 --> `QS2` c |\n", id="table-row"),
    pytest.param("a ``QS1\n[d]: d.md\nb <!-- QH1 --> `QS2` c\n", id="definition"),
    pytest.param("a ``QS1\n<!-- QH1 -->\nb <!-- QH2 --> `QS2` c\n", id="line-start-comment"),
    pytest.param("a ``QS1\n<!-- QH1 --> b <!-- QH2 --> `QS2` c\n", id="after-a-line-start-comment"),
    pytest.param("We call this the ``QS1'' case.\n<!-- QH1: expand -->\nSee `QS2` for the statement.\n", id="tex-todo"),
    pytest.param(
        "- We call this the ``QS1'' case.\n- Write `<!--` to open one.\n\n## QS2\n\n- [QS3](base.md) <!-- QH1 -->\n",
        id="list-items-then-a-heading",
    ),
    pytest.param("Run \\\\`QS1\n## QS2 <!-- QH1 -->\nQS3` QS4\n", id="escaped-backslash-then-a-heading"),
    pytest.param(
        "p `QS1 <!-- QH1\n\nQH2 --> QS2` q `<!-- QH3 -->` <!-- QS3 ` d\n\n## QS4\n\n-->\n", id="runs-apart-after-a-join"
    ),
    # And nowhere else: a list marker in a paragraph, a `#` after a space, a comment a `<!--` before it took in, and
    # a table separator under indented code.
    pytest.param("a `QS1\n- QS2 <!-- QS3 --> QS4` c\n", id="list-marker-in-a-paragraph"),
    pytest.param("a `QS1\n # QS2 <!-- QS3 --> QS4` c\n", id="hash-after-a-space"),
    pytest.param("Write `<!--` and `QS1\n<!-- QS2 -->\nQS3` c\n", id="comment-opened-in-a-span"),
    pytest.param("    QC1 <!-- QC2\n\na `QS1\n<!-- QS2 -->\nQS3` c\n", id="comment-opened-in-indented-code"),
    pytest.param("    QC1 | QC2\n|---|\na `QS1\nb <!-- QS2 --> QS3` c\n", id="indented-code-then-a-separator"),
]


def _misread(text: str, shown: str, kinds: str) -> list[str]:
    """The markers of ``kinds`` in ``text`` that ``shown`` shows though QH, or leaves out though not."""

    return [marker for marker in re.findall(rf"Q[{kinds}]\d", text) if (marker in shown) == marker.startswith("QH")]


@pytest.mark.parametrize("text", _MARKED_PAGES)
def test_the_page_shows_exactly_what_is_outside_its_comments(text: str) -> None:
    """Issue #657: the site shows every marker outside a comment, and none in
    one; and check names nothing but a comment that never closes, so render
    shows the page rather than the page as typed."""

    assert _misread(text, rendered_visible_text(published_page(text).removeprefix("---\n{}\n---\n")), "SHCU") == []
    assert bool(publishable_article(text)[1]) == ("QU" in text)


@pytest.mark.parametrize("text", _SPAN_PAGES)
def test_the_graph_hides_the_comments_the_page_hides(text: str) -> None:
    """Issue #657: the graph pairs a line's code spans with the page's pattern, so backslashes and backtick runs
    decide the same comments for both."""

    assert _misread(text, "\n".join(mask_code_and_comments(text.splitlines())), "SH") == []


@pytest.mark.parametrize("text", _MARKED_PAGES)
def test_the_page_publishes_again_unchanged_and_keeps_its_lines_for_render(text: str) -> None:
    """The page leaves no comment the site would still hide, so publishing it again changes nothing; and render's
    line transforms, which blank the same comments, keep every line where it was."""

    page = published_page(text).removeprefix("---\n{}\n---\n")
    blanked = _comments_blanked(text.splitlines())

    assert published_page(page).removeprefix("---\n{}\n---\n") == page
    assert len(blanked) == len(text.splitlines())
    assert "".join(blanked) == page.replace("\n", "")


@pytest.mark.parametrize(
    ("text", "page"),
    [
        ("x\n\n    old\n\n```lean\nfoo\n```\n<!-- c -->\n", "x\n\n    old\n\n```lean\nfoo\n```\n"),
        ("---\ntitle: t\n---\n\n```lean\nfoo\n```\n<!-- c -->\nlast\n", "\n\n\n\n```lean\nfoo\n```\n\nlast\n"),
    ],
    ids=["after-indented-code", "after-frontmatter"],
)
def test_a_blank_line_that_starts_a_run_stays(text: str, page: str) -> None:
    """Review of #745: a run of lines between indented code blocks that held a
    `<!--` lost its first line when that line was blank, so a fence right
    after an indented block was published as a code span, and every line
    after the frontmatter moved up one."""

    assert published_page(text).removeprefix("---\n{}\n---\n") == page


@pytest.mark.parametrize("separator", ["\x1c", "\x1d", "\x1e", "\u2028"])
def test_a_line_break_in_a_page_stands_for_no_fenced_block(separator: str) -> None:
    """The page reads a fenced block as one character that a page's lines are
    split at, so none of an author's own text reads as one, and a comment over
    that break still comes out whole."""

    assert published_page(f"x <!-- a {separator} b --> y") == "---\n{}\n---\nx  y\n"


#: Backtick and backslash layouts whose pairing is easy to get wrong: escapes,
#: runs of different lengths, and a span over a line or a blank one.
_PAIRING = [
    "a \\\\`x` b", "a \\`x` b`", "``a`", "```a``", "a ``b` c`` d", "\\\\\\`a` b`", "`a\\`", "x `` ` `` y",
    "`a`b`", "``a``b``", "\\``x`", "a \\\\\\\\`b`", "`a\nb`", "`a\n\nb`", "`a\n \nb`", "a ` b ` c", "`` `a` ``",
    "a`b", "````a`````b````", "\\\\`\\\\`", "`\\\\`", "a\\*`b`", "a `b\\` c`", "x\\\\\\\\\\`y` z`", "``a` b` c`",
]


@pytest.mark.parametrize("text", _PAIRING)
def test_the_page_s_pattern_pairs_code_spans_as_python_markdown_does(text: str) -> None:
    """The page copies Python-Markdown's backtick rule, so an upgrade that
    pairs spans differently fails here rather than on a published page."""

    spans = [run[0][len(run[1]) : -len(run[1])].strip() for run in _PAGE_CODE_OR_COMMENT.finditer(text) if run[1]]
    code = re.findall(r"<code>(.*?)</code>", site_converter().convert(text), re.DOTALL)

    assert spans == [html.unescape(element) for element in code]


def test_a_long_run_of_backticks_that_pairs_with_nothing_is_tried_once() -> None:
    """Review of #657: each backtick of a run that pairs with nothing tried
    every shorter length again, so a run of 160 took seconds to read. A run
    is tried from its start only, which every length it could open with is."""

    code = "from autoform_cli.markdown import published_page; print(published_page('`' * 8000 + ' x <!-- c --> y')[-6:])"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True)

    assert done.stdout == " x  y\n\n"


def test_render_s_line_transforms_pair_no_code_span_across_a_page_s_frontmatter() -> None:
    """Review of #542: a backtick run in the frontmatter, which the site doesn't read as Markdown, paired with one in
    the body and kept the comment between, so the comment's fence line hid every line after it."""
    for text in (
        "---\ntitle: a ` b\n---\nThe `x` value.\n<!--\n```\n-->\nUse ` and see [x](a.md).\n",
        "---\n# use ``` here\n---\n<!--\n```\n-->\nsee [x](a.md)\n",
    ):
        assert outside_fences(text, lambda line: f"+{line}", page=True) == "".join(
            f"+{line}\n" for line in text.splitlines()
        )


def test_content_distinguishes_hidden_lines_from_blank_ones() -> None:
    view = content("visible\n\n<!-- hidden -->\n")

    assert view.lines == ("visible", "", "")
    assert view.hidden == frozenset({2})
    assert not view.is_hidden(1)
    # Line 1 is a blank the author typed and ends a block; line 2 only looks
    # blank because a comment covers it.
    assert view.ends_block(1)
    assert not view.ends_block(2)


@pytest.mark.parametrize(
    ("text", "hidden"),
    [
        pytest.param("visible\n<!-- note\n\nmore note -->\nafter\n", frozenset({1, 2, 3}), id="comment"),
        pytest.param("visible\n```\n\nexample\n```\nafter\n", frozenset({1, 2, 3, 4}), id="fence"),
    ],
)
def test_blank_lines_inside_a_comment_or_a_fence_belong_to_it(text: str, hidden: frozenset[int]) -> None:
    view = content(text)

    assert view.hidden == hidden
    assert not view.ends_block(2)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "# Title\n\n[One](a.md)\n\n```\n[Two](b.md)\n```\n\n[Three](c.md)\n",
            [(3, "a.md", False), (9, "c.md", False)],
        ),
        # A link whose text wraps is reported on the line it starts on.
        ("# Title\n\nSee [the\nbase](a.md) and [top](b.md).\n", [(3, "a.md", True), (4, "b.md", False)]),
        # So is one only reading the block whole finds: a line read alone hides it in code.
        ("See ` one\ntwo ` [x](a.md) ` three\n", [(2, "a.md", True)]),
        # A line read alone opened a code span at an escaped backtick, which the site does not.
        ("a \\` b [x](a.md) \\` c\n", [(1, "a.md", True)]),
        # A link a line read alone finds is still read, though the site shows it as code.
        ("A `code\n[x](m.md) end`\nand [y](m.md) z.\n", [(2, "m.md", False), (3, "m.md", False)]),
        # A span from the line above closes on this one, which, read alone, pairs the rest of
        # the run around the link: only reading the block whole finds it.
        ("See x `` y\n``a [x](a.md)` z\n", [(2, "a.md", True)]),
        # A full reference is read once, though a line read alone takes its label for a shortcut.
        ("See [the\nlemma][d] for it.\n\n[d]: x.md\n", [(2, "x.md", False)]),
        # A wrapped link stays wrapped beside a link to its target on the line it starts on.
        ("See [A](x.md) and [the\nidea](x.md).\n", [(1, "x.md", False), (1, "x.md", True)]),
        # An autolink starts no raw HTML, so a link can wrap beside it.
        ("Source:\n<https://arxiv.org/abs/1>, see [the\nlemma](a.md).\n", [(2, "a.md", True)]),
    ],
)
def test_markdown_links_report_visible_links_with_line_numbers(
    text: str, expected: list[tuple[int, str, bool]]
) -> None:
    assert markdown_links(text) == expected


#: Link forms paired with the targets the configured renderer links them to. A
#: link the site publishes and the checks miss is never validated, so the scan
#: must find each of these, and no link the site does not publish.
LINK_CORPUS = [
    ("[`Foo.bar`](a.md)", ["a.md"]),
    ("[](a.md)", ["a.md"]),
    ("`[Example](a.md)`", []),
    ("[[1] Smith](a.md)", ["a.md"]),
    (r"[\[1\] Smith](a.md)", ["a.md"]),
    (r"\[Not a link](a.md)", []),
    ("[![Badge](a.png)](b.md)", ["b.md"]),
    ("[Paper][paper]\n\n[paper]: a.md", ["a.md"]),
    ("[Paper][]\n\n[PAPER]: <a.md>", ["a.md"]),
    ("[Paper] and [paper] [Other]\n\n[paper]: a.md", ["a.md", "a.md"]),
    ("[Unknown][missing] and [unknown]", []),
    ("[[a]] and [see [a]]\n\n[a]: x.md", []),
    ("![x [a] y]\n\n[a]: x.md", ["x.md"]),
    ("[x [b](c.md) y]", ["c.md"]),
    ("[paper]: a.md", []),
    ("[Paper][paper]\n\n```\n[paper]: a.md\n```", []),
    ("[Paper][paper]\n\n    [paper]: a.md", []),
    ("![Figure][fig]\n\n[fig]: a.png", []),
    ("[One][x] and [Two](b.md)\n\n[x]: a.md\n[x]: c.md", ["c.md", "b.md"]),
    (r"\\[x](a.md)", ["a.md"]),
    (r"\![x](a.png)", ["a.png"]),
    ("[Two  spaces]\n\n[two spaces]: a.md", ["a.md"]),
    ("[Paper][two  spaces](b.md)\n\n[two spaces]: a.md", ["a.md"]),
    ("- [Torsion](t.md) on $E[2]$\n\n[2]: ../sources/jones.md", ["t.md"]),
    ("[Half-open $(0,1]$](h.md)", ["h.md"]),
    (r"Index \([a]\) here" + "\n\n[a]: x.md", []),
    ("[x][y][z](a.md)\n\n[z]: z.md", ["a.md"]),
    ("[x [a][b] y][undefined]\n\n[b]: b.md", []),
    ("[Paper][missing][b](x.md)\n\n[paper]: p.md\n[b]: b.md", ["p.md", "x.md"]),
    ("[Node](<a b.md>)", ["a b.md"]),
    ("[Node](a.md", []),
    ("![Figure](a.png)", []),
    # A paragraph or a list item is read whole, so a link can wrap across its lines.
    ("[Wrapped\nlabel](a.md)", ["a.md"]),
    ("- [Wrapped\n  label](a.md)\n- [Next](b.md)", ["a.md", "b.md"]),
    ("Text [a\n- b](a.md)", ["a.md"]),
    ("> [Quoted\nlazily](a.md)", ["a.md"]),
    ("> [Quoted\n> lines](a.md)", ["a.md"]),
    ("`[In\ncode](a.md)`", []),
    ("- [One\n- two](a.md)", []),
    ("Text [a\n# Heading\nb](a.md)", []),
    ("Title [a\n===\nb](a.md)", []),
    ("Text [a\n> b](a.md)", []),
    ("Text [a\n<div>\nb](a.md)\n</div>", []),
    ("| A | B |\n| --- | --- |\n| [x | y |\n| z](a.md) | w |", []),
    ("See [Exercise\n#3](a.md)", []),
    ("[a]:\n  x.md\nSee [y](y.md).", ["y.md"]),
    ("[a]:\n  x.md\nSee [y\nz](y.md).", ["y.md"]),
    ("[a \\` b](a.md)\nand `c` here", ["a.md"]),
    ("a \\` b [x](a.md) \\` c", ["a.md"]),
    ("> [a\n>\n> b](a.md)", []),
    ("> - [One\n> - two](a.md)", []),
    ("> a [x\n> > b](a.md)", []),
    ("A\nb [x\n===\ny](a.md)", ["a.md"]),
    ("> - a\n- [b\n> c](a.md)", ["a.md"]),
    ("- a\n\n> q\n- [b\n> c](a.md)", ["a.md"]),
    ("# H\n    [a\n    b](a.md)", []),
    ("> a [x\n> <div>b</div>](a.md)", ["a.md"]),
    (">\n1. [item\n> lemma](a.md)", ["a.md"]),
    ("> q\n# h\nb [x\n> c](a.md)", []),
    ("> > a [x\n> b](a.md)", ["a.md"]),
    ("- a\n## h\n    - [d\n  t](a.md)", []),
    (">\n- [a\n> - b](a.md)", []),
    ("> a\n\nb [x\n> c](a.md)", []),
    ("- a\n[d]: x.md\n    b [c\n    d](a.md)", ["a.md"]),
    ("[d]:\n>[x](a.md)", ["a.md"]),
    ("[d]:\n>[x\n>y](a.md)", ["a.md"]),
    ("- a\n> q\n- [b\n> c](a.md)", []),
    ("Text [a\n<?x y?>\nb](a.md)", []),
    ("Text [a\n<!DOCTYPE x>\nb](a.md)", []),
    ("Text [a\n<![CDATA[ x ]]>\nb](a.md)", []),
    ("Text [a\n<![x]>\nb](a.md)", ["a.md"]),
    ("Text [a\n-   -   -\nb](a.md)", ["a.md"]),
    ("Text [a\n- -\t-\nb](a.md)", []),
    ("Title [a\n=-=\nb](a.md)", ["a.md"]),
    ("Text [a\n|---|\nb](a.md)", ["a.md"]),
    ("> [a](\n> x.md)", ["x.md"]),
    ("> [Lemma\n> B][]\n\n[Lemma B]: b.md", ["b.md"]),
    ("See [the\n<em>lemma</em>](a.md).", ["a.md"]),
    ("For [all $a\n<b$ see](a.md).", ["a.md"]),
    ("> - [a\n- b](a.md)", []),
    ("1) [One\n2) two](a.md)", ["a.md"]),
    ("| A | B |\n| --- | --- |\n| x | y |\n\nSee [the\nbase](a.md).", ["a.md"]),
    ("- By [the lemma](a.md see\n  the notes (below) for more.", []),
    ('- By [the\n  lemma](a.md "Title (draft)")', ["a.md"]),
]


@pytest.mark.parametrize(("source", "expected"), LINK_CORPUS)
def test_links_are_the_ones_the_configured_renderer_publishes(source: str, expected: list[str]) -> None:
    published = re.findall(r'<a href="([^"]*)"', render_html(source))

    assert [html.unescape(href) for href in published] == expected
    assert [target for _, target, _ in markdown_links(source)] == expected


@pytest.mark.parametrize(
    ("source", "site", "read"),
    [
        ("A `code span\n[hides](a.md) this` link", [], ["a.md"]),
        ("A $x\n[y](a.md) z$ link", [], ["a.md"]),
        ("> [d]:\n> [x](a.md)", [], ["a.md"]),
        # The site reads the two lines as one full reference.
        ("[Paper]\n[paper]\n\n[paper]: a.md", ["a.md"], ["a.md", "a.md"]),
    ],
)
def test_a_link_a_line_read_alone_shows_is_still_read(source: str, site: list[str], read: list[str]) -> None:
    # The site shows less here than check read a line at a time before, and check still reads it all (#325).
    published = re.findall(r'<a href="([^"]*)"', render_html(source))

    assert [html.unescape(href) for href in published] == site
    assert [(target, wrapped) for _, target, wrapped in markdown_links(source)] == [(target, False) for target in read]


@pytest.mark.parametrize(
    "source",
    [
        "``a [L](x.md)`",
        "\\` a ` [L](x.md) `",
        "m1z<!\n  m2z\\\\<!-- c -->`` m3z<!<!-- c --><!--[a4z](l4.md)[a5z](l5.md)`",
    ],
    ids=["empty-pair", "escaped-backtick", "comment-in-the-span"],
)
def test_a_link_in_a_span_the_site_pairs_on_its_line_is_not_read(source: str) -> None:
    # Read alone, each line paired its backticks unlike the site: around nothing,
    # or from an escaped backtick. The site shows the link as code, and so does check.
    assert "<a href" not in render_html(source)
    assert markdown_links(source) == []


def test_anchor_rendering_is_cached_by_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import autoform_cli.markdown as markdown_module

    path = tmp_path / "article.md"
    path.write_text("# Alpha\n", encoding="utf-8")
    markdown_module._anchors_from_body.cache_clear()
    original = markdown_module.render_html
    calls = 0

    def counted_render(text: str) -> str:
        nonlocal calls
        calls += 1
        return original(text)

    monkeypatch.setattr(markdown_module, "render_html", counted_render)

    first = markdown_anchors(path)
    first.add("mutated")
    assert markdown_anchors(path) == {"alpha"}
    assert calls == 1

    # Equal-sized edits must invalidate naturally; path, size, and timestamps are
    # deliberately not part of the cache contract.
    path.write_text("# Bravo\n", encoding="utf-8")
    assert markdown_anchors(path) == {"bravo"}
    assert calls == 2


def test_local_targets_are_resolved_against_the_boundary(tmp_path: Path) -> None:
    article = tmp_path / "roadmap" / "node.md"
    article.parent.mkdir(parents=True)
    article.write_text("# Node\n\n## Results\n", encoding="utf-8")
    source = tmp_path / "coverage" / "README.md"
    source.parent.mkdir()
    source.write_text("# Coverage\n", encoding="utf-8")
    hidden = tmp_path / ".drafts" / "notes.md"
    hidden.parent.mkdir()
    hidden.write_text("# Notes\n", encoding="utf-8")

    def issue(target: str) -> str | None:
        problem = local_target_issue(source, target, tmp_path, label="coverage")
        return None if problem is None else problem[0]

    assert issue("../roadmap/node.md") is None
    assert issue("../roadmap/node.md#results") is None
    # Nothing needs to step through a hidden folder, and render's copy has none.
    assert issue("../.drafts/../roadmap/node.md") == "hidden-coverage-target"
    # Render omits the vault's copies of the root views, whether or not one exists.
    (tmp_path / "structure.md").write_text("# Structure\n", encoding="utf-8")
    assert issue("../structure.md") == issue("../dependencies.md") == "generated-coverage-target"
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "structure.md").write_text("# Structure notes\n", encoding="utf-8")
    assert issue("../sources/structure.md") is None
    assert issue("../.x/../roadmap/node.md") == "coverage-not-found"
    assert issue("https://example.invalid/page") is None
    assert issue("../roadmap/node.md#absent") == "coverage-anchor-not-found"
    assert issue("../roadmap/absent.md") == "coverage-not-found"
    assert issue("../../outside.md") == "coverage-escapes-blueprint"
    assert issue("../.drafts/notes.md") == "hidden-coverage-target"
    # A page inside a hidden folder, such as coverage reached through a link to one,
    # spells a hidden path even when it names a sibling.
    assert local_target_issue(hidden, "notes.md", tmp_path, label="coverage")[0] == "hidden-coverage-target"
    assert issue("mailto:someone@example.invalid") == "unsupported-coverage-link"
    assert issue("//example.invalid/page") == "unsupported-coverage-link"


def test_a_link_through_a_hidden_symlink_is_hidden(tmp_path: Path) -> None:
    """Issue #304: render resolves links in a copy of the vault without symlinks.

    `../.link/paper.md` reaches a published file through `.link`, a symlink to
    `sources`, but render would link `.link/paper.md`, a page it never writes. The
    hidden folder holding the vault does not count.
    """
    root = tmp_path / ".checkout"
    for name in ("sources/paper.md", "roadmap/result.md"):
        (root / name).parent.mkdir(parents=True)
        (root / name).write_text("# Page\n", encoding="utf-8")
    try:
        (root / ".link").symlink_to("sources", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    def issue(target: str) -> str | None:
        problem = local_target_issue(root / "roadmap" / "result.md", target, root, label="source")
        return None if problem is None else problem[0]

    assert issue("../sources/paper.md") is None
    assert issue("../.link/paper.md") == "hidden-source-target"


@pytest.mark.skipif(os.name == "nt", reason="Windows applies `..` as written, before any link")
def test_resolved_path_follows_links_as_the_system_opens_them(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "away").mkdir()
    (root / "away" / "file.tex").write_text("text\n", encoding="utf-8")
    (root / "here" / "sub").mkdir(parents=True)
    try:
        (root / "here" / "loop").symlink_to("loop")
        (root / "here" / "sub" / "back").symlink_to("../../away")
    except OSError:
        pytest.skip("symlinks are unavailable")

    def resolved(path: str) -> str | None:
        found = resolved_path(root / "here" / path)
        return None if found is None else found.relative_to(root).as_posix()

    # A later `..` steps out of the link it follows, not out of the name as written.
    assert resolved("sub/back/../away/file.tex") == "away/file.tex"
    # Nothing can open a path through a loop or a missing folder, or a missing file.
    assert [resolved(path) for path in ("loop", "loop/../sub", ".x/../sub", "sub/missing.tex")] == [None] * 4


def test_a_link_that_cannot_be_read_does_not_resolve(
    tmp_path: Path, unreadable_link: Callable[[Path, Path | str], None]
) -> None:
    # macOS follows a link whose mode forbids reading it, so keeping it as
    # written would hide where it leads.
    root = tmp_path.resolve()
    (root / "away").mkdir()
    (root / "away" / "paper.md").write_text("# Paper\n", encoding="utf-8")
    (root / "blueprint" / "roadmap").mkdir(parents=True)
    unreadable_link(root / "blueprint" / "locked", root / "away")

    assert resolved_path(root / "blueprint" / "locked" / "paper.md") is None
    issue = local_target_issue(root / "blueprint" / "roadmap" / "result.md", "../locked/paper.md", root / "blueprint", label="source")
    assert issue == ("source-not-found", "source link does not resolve to a file: '../locked/paper.md'")


@pytest.mark.skipif(os.name == "nt", reason="Windows reads links with os.path.realpath")
def test_a_directory_behind_a_link_that_cannot_be_read_has_the_path_the_system_opens(
    tmp_path: Path, unreadable_link: Callable[[Path, Path | str], None]
) -> None:
    # As for a blueprint reached through another user's symlink.
    root = tmp_path.resolve()
    (root / "real" / "blueprint").mkdir(parents=True)
    unreadable_link(root / "via", root / "real")

    assert resolved_path(root / "via" / "blueprint") is None
    assert directory_path(root / "via" / "blueprint") == root / "real" / "blueprint"
    assert directory_path(root / "via" / "missing") is None


#: Articles paired with the statement and the sections `article_parts` reads in them.
ARTICLE_PARTS = [
    pytest.param(
        "---\ndeclaration: theorem\n---\n\n# Title\n\nThe statement.\n\n"
        "## Depends on\n\n- [Base](base.md)\n\n## Sources\n\n- [Paper](paper.md)\n",
        "The statement.",
        (
            ArticleSection("Depends on", "## Depends on", "- [Base](base.md)"),
            ArticleSection("Sources", "## Sources", "- [Paper](paper.md)"),
        ),
        id="from-the-title-to-the-first-h2",
    ),
    pytest.param(
        "# Title\n\nFirst part.\n\n### Remark\n\nSecond part.\n\n## Sources\n",
        "First part.\n\n### Remark\n\nSecond part.",
        (ArticleSection("Sources", "## Sources", ""),),
        id="a-deeper-heading-ends-nothing",
    ),
    *(
        pytest.param(
            f"# Title\n\nBefore.\n\n{hidden}\n\nAfter.\n\n## Sources\n",
            f"Before.\n\n{hidden}\n\nAfter.",
            (ArticleSection("Sources", "## Sources", ""),),
            id=f"an-unpublished-heading-ends-nothing-{name}",
        )
        for name, hidden in (
            ("backticks", "```\n## Not a section\n```"),
            ("tildes", "~~~~\n## Not a section\n~~~~"),
            ("comment", "<!--\n## Not a section\n-->"),
            ("indented", "    ## Not a section"),
        )
    ),
    pytest.param("# Title\n\nOnly a statement.\n", "Only a statement.", (), id="without-sections"),
    *(
        pytest.param(text, "", (), id=f"no-body-{name}")
        for name, text in (
            ("empty", ""),
            ("title", "# Title\n"),
            ("frontmatter", "---\ndeclaration: theorem\n---\n"),
            ("frontmatter-never-closed", "---\nnever closed\n# Title\n\nBody.\n"),
        )
    ),
    pytest.param(
        "# Title\r\n\r\nBody.\r\n\r\n## Depends on <!-- edges --> ##\r\n\r\nNone.\r\n",
        "Body.",
        (ArticleSection("Depends on", "## Depends on <!-- edges --> ##", "None."),),
        id="a-section-title-as-published",
    ),
    # The graph loader reads only ATX headings, so a setext underline is prose
    # here too; one rule for where a section starts, whatever reads the article.
    pytest.param(
        "# Title\n\nBody.\n\nProof\n-----\n\nSteps.\n", "Body.\n\nProof\n-----\n\nSteps.", (), id="only-atx-headings"
    ),
    pytest.param(
        "---\nlean: A.b\n---\n\n# Title\n\nAbove.\n\n---\n\nBelow.\n",
        "Above.\n\n---\n\nBelow.",
        (),
        id="a-horizontal-rule-after-the-frontmatter",
    ),
    pytest.param(
        "# Title\n\n    theorem draft : True := trivial\n\n## Sources\n\n    cited\n",
        "    theorem draft : True := trivial",
        (ArticleSection("Sources", "## Sources", "    cited"),),
        id="the-indentation-that-makes-it-code",
    ),
    # The page draws a fence without a closing line as plain text and shows
    # the section after it, so the section is read as one.
    pytest.param(
        "# Title\n\nBefore.\n\n```\ncode\n\n## Depends on\n\n- [Base](base.md)\n",
        "Before.\n\n```\ncode",
        (ArticleSection("Depends on", "## Depends on", "- [Base](base.md)"),),
        id="a-fence-that-never-closes-hides-no-heading",
    ),
    pytest.param(
        "# Title\n\nBody.\n\n<!-- todo\n\n## Depends on\n\n- [Base](base.md)\n",
        "Body.\n\n<!-- todo\n\n## Depends on\n\n- [Base](base.md)",
        (),
        id="an-unclosed-comment-hides-every-heading-after-it",
    ),
    # CONTENT_LINES shows that the comment still hides the statement it covers.
    pytest.param(
        "# Title <!-- TODO:\nwrite the statement -->\n\n## Depends on\n\nNone.\n",
        "<!-- TODO:\nwrite the statement -->",
        (ArticleSection("Depends on", "## Depends on", "None."),),
        id="a-comment-opened-on-the-title-line",
    ),
]


@pytest.mark.parametrize(("text", "statement", "sections"), ARTICLE_PARTS)
def test_a_statement_runs_from_the_title_to_the_first_published_h2(
    text: str, statement: str, sections: tuple[ArticleSection, ...]
) -> None:
    parts = article_parts(text)

    assert (parts.statement, parts.sections) == (statement, sections)


def test_prose_before_the_title_belongs_to_the_statement() -> None:
    assert article_parts("Lead-in.\n\n# Title\n\nBody.\n").statement.split() == ["Lead-in.", "Body."]


def test_coverage_and_link_checks_read_a_fence_that_never_closes_to_the_end() -> None:
    # Headings and dependencies read it as text, so an article keeps its
    # sections and edges. Coverage files and the link checks keep it open, so
    # nothing after it counts: the site shows a page whose fence is left open
    # at the margin as typed (#201).
    text = "```lean\nexample\n\n[Base](base.md)\n\n| Area | Coverage | Evidence |\n"

    assert mask_fences_and_comments(text.splitlines()) == text.splitlines()
    assert markdown_links(text) == []
    assert not any(line.strip() for line in content(text).lines)


#: Lines paired with what `mask_fences_and_comments` leaves of them. A fence
#: that never closes is text only from its own first line.
MASKED = [
    # Read as an opener, the first line would pair with the later fence and hide the heading.
    pytest.param(
        ["```x``` is inline code here.", "## Shown", "```", "hidden", "```"],
        ["```x``` is inline code here.", "## Shown", "", "", ""],
        id="inline-code-starting-with-three-backticks-is-not-a-fence",
    ),
    # The closed fence stays hidden, and the comment inside the open one counts
    # again once that fence is read as text.
    pytest.param(
        ["```", "hidden <!--", "```", "shown", "~~~py", "<!-- a comment", "## Hidden by the comment"],
        ["", "", "", "shown", "~~~py", "", ""],
        id="a-comment-inside-a-fence-that-never-closes",
    ),
    pytest.param(
        ["```", "hidden <!--", "```", "shown", "~~~py", "## Shown"],
        ["", "", "", "shown", "~~~py", "## Shown"],
        id="a-fence-that-never-closes-is-text",
    ),
    pytest.param(
        ["~~~", "```", "## Hidden", "~~~", "## Shown"],
        ["", "", "", "", "## Shown"],
        id="a-fence-closes-only-on-its-own-character",
    ),
    pytest.param(
        ["````py", "## Shown", "```", "hidden", "```", "after"],
        ["````py", "## Shown", "", "", "", "after"],
        id="a-closed-fence-after-one-that-never-closes",
    ),
    pytest.param(
        ["<!-- a comment", "ends --> ```", "## Shown"],
        ["", " ```", "## Shown"],
        id="reread-with-the-comment-state-it-opened-in",
    ),
    # A known limit: the page needs a closing line of the opener's own length,
    # so it draws this fence as text and shows the heading.
    pytest.param(
        ["```lean", "x", "``` -- end", "## Depends on", "````", "code", "````"],
        ["", "", "", "", "", "code", "````"],
        id="a-later-longer-fence-line-closes-it",
    ),
]


@pytest.mark.parametrize(("lines", "expected"), MASKED)
def test_masking_reads_each_fence_as_the_page_draws_it(lines: list[str], expected: list[str]) -> None:
    assert mask_fences_and_comments(lines) == expected


def test_many_fences_that_never_close_do_not_each_reread_the_article(monkeypatch: pytest.MonkeyPatch) -> None:
    import autoform_cli.markdown as markdown_module

    lines = [line for index in range(20000) for line in (f"```{'`' * (index % 3)}info{index}", "## Heading")]
    reads: list[str] = []

    class Counted:
        """Each line read is matched against exactly one fence pattern."""

        def __init__(self, pattern: re.Pattern[str]) -> None:
            self.pattern = pattern

        def match(self, line: str) -> re.Match[str] | None:
            reads.append(line)
            assert len(reads) <= 3 * len(lines), "an unclosed fence rereads the article"
            return self.pattern.match(line)

    monkeypatch.setattr(markdown_module, "FENCE", Counted(markdown_module.FENCE))
    monkeypatch.setattr(markdown_module, "FENCE_CLOSE", Counted(markdown_module.FENCE_CLOSE))
    assert mask_fences_and_comments(lines) == lines
    assert len(reads) >= len(lines)


def test_a_title_below_a_section_ends_that_section() -> None:
    parts = article_parts("Intro.\n\n## Depends on\n\n- [Base](base.md)\n\n# Title\n\nStatement.\n\n## Sources\n")

    assert parts.statement.split() == ["Intro.", "Statement."]
    assert parts.sections == (
        ArticleSection("Depends on", "## Depends on", "- [Base](base.md)"),
        ArticleSection("Sources", "## Sources", ""),
    )


@pytest.mark.parametrize(
    "visible",
    [
        "TODO",
        "tbd.",
        "**TODO.**",
        "todo tbd",
        "TODO: state it",
        "TBD - pick one",
        "Pending \u2013 later",
        "TODO -",
        "TODO\u2014later",
        "TODO -- later",
        "TODO--later",
        "TODO \u2013 later",
        "TODO\u2013 later",
        "TBD\u2014write this",
        "Unknown: x",
        "TODO - x",
    ],
)
def test_is_placeholder_accepts_bare_and_marker_placeholders(visible: str) -> None:
    assert is_placeholder(visible)


@pytest.mark.parametrize(
    "visible",
    [
        "",
        "Pending Mathlib PR 1234",
        "TODO state it",
        "Let x be a todo list.",
        "TODO-lists form a monoid.",
        "Unknown-variance case: the sample mean is normal.",
        # A dash joining two words does not mark a placeholder.
        "Unknown\u2013known duality holds for every pair.",
        "Unknown-variance case.",
    ],
)
def test_is_placeholder_leaves_sentences_alone(visible: str) -> None:
    assert not is_placeholder(visible)


@pytest.mark.parametrize(("visible", "expected"), [("", False), ("...", False), ("**_~", False), ("x", True)])
def test_has_substance_needs_a_word_character(visible: str, expected: bool) -> None:
    assert has_substance(visible) is expected


@pytest.mark.parametrize(
    "source",
    [
        "### Setting\n\nLet x.",
        "Setting\n=======\n\nLet x.",
        "> ### Quoted\n> Let x.",
        "```lean\ntheorem t : True\n```\n\nLet x.",
        "```mermaid\ngraph TD\n```\n\nLet x.",
        "    indented code\n\nLet x.",
        "<!-- note -->Let x.",
    ],
)
def test_visible_prose_leaves_out_headings_code_blocks_and_diagrams(source: str) -> None:
    assert visible_prose(source) == "Let x."


def test_visible_prose_keeps_inline_code_and_drops_hidden_text() -> None:
    assert visible_prose("Use `Nat.add_zero`.") == "Use Nat.add_zero."
    assert visible_prose("<span hidden>secret</span> shown") == "shown"


@pytest.mark.parametrize(
    ("source", "links", "expected"),
    [
        ("[A](a.md), [B](b.md), and [C](c.md)", 3, True),
        ("[A](a.md); [B](b.md) And [C](c.md).", 3, True),
        ("[`K(X)`](a.md)", 1, True),
        ("[A](a.md) uses Exercise 6.2", 1, False),
        # "or" offers a choice, and a mark is the author's word on the links.
        ("[A](a.md) or [B](b.md)", 2, False),
        ("[A](a.md), [B](b.md), …", 2, False),
        ("[A](a.md)*", 1, False),
        ("[A](a.md) ✗ and [B](b.md) (?)", 2, False),
        ("[A](a.md) → [B](b.md)", 2, False),
        ("~~[A](a.md)~~", 1, False),
        ("[A](a.md) <https://example.test/paper>", 1, False),
        ("[A](a.md) ![scan](scan.png)", 1, False),
        ("[A](a.md)", 2, False),
        ("No links here", 0, False),
    ],
)
def test_only_links_wants_the_links_counted_and_nothing_else(source: str, links: int, expected: bool) -> None:
    assert only_links(source, links) is expected


def test_visible_text_survives_markup_nested_thousands_deep() -> None:
    source = "<span>" * 3000 + "x"

    assert rendered_visible_text(source) == "x"
    assert visible_prose(source) == "x"


def test_a_statement_the_renderer_gives_up_on_does_not_spoil_the_next_one() -> None:
    import autoform_cli.markdown as markdown_module

    # A list nested this deep exhausts the renderer. The failure must stay
    # with that text and not leave the shared converter half-finished.
    with pytest.raises(RecursionError):
        markdown_module.render_html("- " * 500 + "x")
    assert visible_prose("- " * 500 + "x") == ""

    assert visible_prose("[ ](missing.md)") == ""
    assert visible_prose("**Let** x.") == "Let x."
