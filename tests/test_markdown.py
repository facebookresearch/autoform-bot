from __future__ import annotations

import re
from pathlib import Path

import pytest

from autoform_cli.markdown import (
    SITE_EXTENSION_CONFIGS,
    SITE_EXTENSIONS,
    content,
    content_lines,
    link_targets,
    local_target_issue,
    markdown_anchors,
    markdown_links,
    PublishedTable,
    published_tables,
    rendered_visible_text,
)

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
    # An explicit ID is emitted verbatim and never uniquified, but it is
    # reserved before any heading is slugged.
    ("# A {#dup}\n\n# B {#dup}", ["dup"]),
    ("# Depends on\n\n# B {#depends-on}", ["depends-on", "depends-on_1"]),
    ("# ***", ["_1"]),
    ("Setext one\n===", ["setext-one"]),
    ("Setext two\n---", ["setext-two"]),
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


def test_frontmatter_cannot_contribute_anchors(tmp_path: Path) -> None:
    # MkDocs strips frontmatter before Markdown sees it, so a setext-looking
    # closing delimiter must not turn a YAML key into a heading.
    article = tmp_path / "article.md"
    article.write_text("---\nkind: blueprint\n---\n\n# Real heading\n", encoding="utf-8")

    assert markdown_anchors(article) == {"real-heading"}


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


def test_a_paragraph_running_into_a_table_publishes_no_table() -> None:
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


def test_a_hidden_cell_is_not_an_empty_column() -> None:
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


def test_masking_blanks_code_blocks_and_comments_without_moving_lines() -> None:
    text = (
        "# Title\n"
        "\n"
        "```markdown\n"
        "| Area | Coverage | Evidence |\n"
        "```\n"
        "\n"
        "<!-- hidden\n"
        "still hidden\n"
        "-->\n"
        "\n"
        "    indented code\n"
        "\n"
        "visible tail\n"
    )

    lines = content_lines(text)

    assert len(lines) == len(text.splitlines())
    assert lines[0] == "# Title"
    assert [line for line in lines if line.strip()] == ["# Title", "visible tail"]
    # The tail keeps its own line number, which is what diagnostics report.
    assert lines.index("visible tail") == 12


def test_a_comment_opener_inside_a_fence_is_literal_text() -> None:
    text = "```\n<!--\n```\n\nvisible\n"

    assert [line for line in content_lines(text) if line.strip()] == ["visible"]


def test_a_fence_inside_a_comment_does_not_open_a_code_block() -> None:
    text = "<!--\n```\n-->\n\nvisible\n"

    assert [line for line in content_lines(text) if line.strip()] == ["visible"]


def test_text_beside_a_comment_on_one_line_survives() -> None:
    assert content_lines("| OUT | real <!-- aside --> reason |\n") == [
        "| OUT | real  reason |"
    ]


def test_indented_content_under_a_list_item_is_not_code() -> None:
    text = "- item\n\n    continuation of the item\n"

    assert [line.strip() for line in content_lines(text) if line.strip()] == [
        "- item",
        "continuation of the item",
    ]


def test_every_continuation_paragraph_in_a_list_stays_visible() -> None:
    # List context has to survive the blank lines between paragraphs, or the
    # second one is mistaken for a code block and its links go unchecked.
    text = "- item\n\n    first continuation\n\n    second continuation\n\ntail\n"

    assert [line.strip() for line in content_lines(text) if line.strip()] == [
        "- item",
        "first continuation",
        "second continuation",
        "tail",
    ]


def test_indented_code_at_the_start_of_a_document_is_masked() -> None:
    assert content_lines("    indented code\n\nvisible\n") == ["", "", "visible"]


def test_content_distinguishes_hidden_lines_from_blank_ones() -> None:
    view = content("visible\n\n<!-- hidden -->\n")

    assert view.lines == ("visible", "", "")
    assert view.hidden == frozenset({2})
    assert not view.is_hidden(1)
    # Line 1 is a blank the author typed and ends a block; line 2 only looks
    # blank because a comment covers it.
    assert view.ends_block(1)
    assert not view.ends_block(2)


def test_blank_lines_inside_a_comment_belong_to_the_comment() -> None:
    view = content("visible\n<!-- note\n\nmore note -->\nafter\n")

    assert view.hidden == frozenset({1, 2, 3})
    assert not view.ends_block(2)


def test_blank_lines_inside_a_fence_belong_to_the_fence() -> None:
    view = content("visible\n```\n\nexample\n```\nafter\n")

    assert view.hidden == frozenset({1, 2, 3, 4})
    assert not view.ends_block(2)


def test_a_closing_fence_may_not_carry_trailing_text() -> None:
    # pymdownx.superfences keeps this inside the code block, so anything after
    # it is still fenced and must stay masked.
    view = content("```\nfenced\n``` trailing\nstill fenced\n")

    assert [line for line in view.lines if line.strip()] == []


def test_a_bare_closing_fence_ends_the_block() -> None:
    view = content("```\nfenced\n```\npublished\n")

    assert [line for line in view.lines if line.strip()] == ["published"]


def test_link_extraction_requires_a_closing_parenthesis() -> None:
    assert link_targets("[Node](../roadmap/node.md)") == ("../roadmap/node.md",)
    assert link_targets("[Node](<../roadmap/a b.md>)") == ("../roadmap/a b.md",)
    assert link_targets("[Node](../roadmap/node.md") == ()
    assert link_targets("`[Node](../roadmap/node.md)`") == ()
    assert link_targets("![Figure](../image.png)") == ()


def test_markdown_links_report_visible_links_with_line_numbers() -> None:
    text = "# Title\n\n[One](a.md)\n\n```\n[Two](b.md)\n```\n\n[Three](c.md)\n"

    assert markdown_links(text) == [(3, "a.md"), (9, "c.md")]


def test_anchors_follow_headings_and_explicit_ids(tmp_path: Path) -> None:
    path = tmp_path / "article.md"
    path.write_text(
        "# Roadmap article\n\n## Depends on\n\n## Depends on\n\n## Result {#custom-id}\n",
        encoding="utf-8",
    )

    assert markdown_anchors(path) == {
        "roadmap-article",
        "depends-on",
        "depends-on_1",
        "custom-id",
    }


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

    def issue(target: str) -> str | None:
        problem = local_target_issue(source, target, tmp_path, label="coverage")
        return None if problem is None else problem[0]

    assert issue("../roadmap/node.md") is None
    assert issue("../roadmap/node.md#results") is None
    assert issue("https://example.invalid/page") is None
    assert issue("../roadmap/node.md#absent") == "coverage-anchor-not-found"
    assert issue("../roadmap/absent.md") == "coverage-not-found"
    assert issue("../../outside.md") == "coverage-escapes-blueprint"
    assert issue("mailto:someone@example.invalid") == "unsupported-coverage-link"
    assert issue("//example.invalid/page") == "unsupported-coverage-link"
