from __future__ import annotations

import html
import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from autoform_cli.markdown import (
    SITE_EXTENSION_CONFIGS,
    SITE_EXTENSIONS,
    ArticleSection,
    article_parts,
    content,
    content_lines,
    directory_path,
    has_substance,
    is_placeholder,
    local_target_issue,
    markdown_anchors,
    markdown_links,
    mask_fences_and_comments,
    outside_fences,
    PublishedTable,
    published_tables,
    render_html,
    rendered_visible_text,
    resolved_path,
    visible_prose,
)

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

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


def test_render_s_line_transforms_read_no_fence_inside_a_comment() -> None:
    """Issue #527: a fence line in a comment hid every later line from render's link rewriting."""
    text = "<!--\n```lean\n-->\nafter\n```\n<!--\n```\nlast\n"

    assert outside_fences(text, lambda line: f"+{line}") == "+<!--\n+```lean\n+-->\n+after\n```\n<!--\n```\n+last\n"


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


def test_markdown_links_report_visible_links_with_line_numbers() -> None:
    text = "# Title\n\n[One](a.md)\n\n```\n[Two](b.md)\n```\n\n[Three](c.md)\n"

    assert markdown_links(text) == [(3, "a.md"), (9, "c.md")]


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
]


@pytest.mark.parametrize(("source", "expected"), LINK_CORPUS)
def test_links_are_the_ones_the_configured_renderer_publishes(source: str, expected: list[str]) -> None:
    published = re.findall(r'<a href="([^"]*)"', render_html(source))

    assert [html.unescape(href) for href in published] == expected
    assert [target for _, target in markdown_links(source)] == expected


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


def test_a_statement_runs_from_the_title_to_the_first_h2() -> None:
    parts = article_parts(
        "---\ndeclaration: theorem\n---\n\n# Title\n\nThe statement.\n\n"
        "## Depends on\n\n- [Base](base.md)\n\n## Sources\n\n- [Paper](paper.md)\n"
    )

    assert parts.statement == "The statement."
    assert parts.sections == (
        ArticleSection("Depends on", "## Depends on", "- [Base](base.md)"),
        ArticleSection("Sources", "## Sources", "- [Paper](paper.md)"),
    )


def test_a_deeper_heading_does_not_end_the_statement() -> None:
    parts = article_parts("# Title\n\nFirst part.\n\n### Remark\n\nSecond part.\n\n## Sources\n")

    assert parts.statement == "First part.\n\n### Remark\n\nSecond part."
    assert [section.title for section in parts.sections] == ["Sources"]


def test_prose_before_the_title_belongs_to_the_statement() -> None:
    assert article_parts("Lead-in.\n\n# Title\n\nBody.\n").statement.split() == ["Lead-in.", "Body."]


@pytest.mark.parametrize(
    "hidden",
    [
        "```\n## Not a section\n```",
        "~~~~\n## Not a section\n~~~~",
        "<!--\n## Not a section\n-->",
        "    ## Not a section",
    ],
)
def test_a_heading_that_is_not_published_ends_nothing(hidden: str) -> None:
    parts = article_parts(f"# Title\n\nBefore.\n\n{hidden}\n\nAfter.\n\n## Sources\n")

    assert parts.statement == f"Before.\n\n{hidden}\n\nAfter."
    assert [section.title for section in parts.sections] == ["Sources"]


def test_an_article_without_sections_is_all_statement() -> None:
    parts = article_parts("# Title\n\nOnly a statement.\n")

    assert (parts.statement, parts.sections) == ("Only a statement.", ())


@pytest.mark.parametrize(
    "text",
    ["", "# Title\n", "---\ndeclaration: theorem\n---\n", "---\nnever closed\n# Title\n\nBody.\n"],
)
def test_an_article_with_no_body_has_an_empty_statement(text: str) -> None:
    parts = article_parts(text)

    assert (parts.statement, parts.sections) == ("", ())


def test_a_section_title_is_read_as_published() -> None:
    parts = article_parts("# Title\r\n\r\nBody.\r\n\r\n## Depends on <!-- edges --> ##\r\n\r\nNone.\r\n")

    assert parts.statement == "Body."
    assert parts.sections == (ArticleSection("Depends on", "## Depends on <!-- edges --> ##", "None."),)


def test_only_atx_headings_end_a_statement() -> None:
    # The graph loader reads only ATX headings, so a setext underline is prose
    # here too; one rule for where a section starts, whatever reads the article.
    parts = article_parts("# Title\n\nBody.\n\nProof\n-----\n\nSteps.\n")

    assert parts.statement == "Body.\n\nProof\n-----\n\nSteps."
    assert parts.sections == ()


def test_a_horizontal_rule_after_the_frontmatter_is_statement_text() -> None:
    assert article_parts("---\nlean: A.b\n---\n\n# Title\n\nAbove.\n\n---\n\nBelow.\n").statement == (
        "Above.\n\n---\n\nBelow."
    )


def test_a_statement_keeps_the_indentation_that_makes_it_code() -> None:
    parts = article_parts("# Title\n\n    theorem draft : True := trivial\n\n## Sources\n\n    cited\n")

    assert parts.statement == "    theorem draft : True := trivial"
    assert parts.sections == (ArticleSection("Sources", "## Sources", "    cited"),)


def test_a_fence_that_never_closes_hides_no_heading() -> None:
    # The page draws a fence without a closing line as plain text and shows
    # the section after it, so the section is read as one.
    parts = article_parts("# Title\n\nBefore.\n\n```\ncode\n\n## Depends on\n\n- [Base](base.md)\n")

    assert parts.statement == "Before.\n\n```\ncode"
    assert parts.sections == (ArticleSection("Depends on", "## Depends on", "- [Base](base.md)"),)


def test_coverage_and_link_checks_read_a_fence_that_never_closes_to_the_end() -> None:
    # Headings and dependencies read it as text, so an article keeps its
    # sections and edges. Coverage files and the link checks keep it open, so
    # nothing after it counts: the site shows a page whose fence is left open
    # at the margin as typed (#201).
    text = "```lean\nexample\n\n[Base](base.md)\n\n| Area | Coverage | Evidence |\n"

    assert mask_fences_and_comments(text.splitlines()) == text.splitlines()
    assert markdown_links(text) == []
    assert not any(line.strip() for line in content(text).lines)


def test_inline_code_starting_with_three_backticks_is_not_a_fence() -> None:
    # Read as an opener, the first line would pair with the later fence and hide the heading.
    lines = ["```x``` is inline code here.", "## Shown", "```", "hidden", "```"]

    assert mask_fences_and_comments(lines) == ["```x``` is inline code here.", "## Shown", "", "", ""]


def test_a_fence_that_never_closes_is_text_only_from_its_own_first_line() -> None:
    lines = ["```", "hidden <!--", "```", "shown", "~~~py", "<!-- a comment", "## Hidden by the comment"]

    # The closed fence stays hidden, and the comment inside the open one counts
    # again once that fence is read as text.
    assert mask_fences_and_comments(lines) == ["", "", "", "shown", "~~~py", "", ""]
    assert mask_fences_and_comments([*lines[:5], "## Shown"])[4:] == ["~~~py", "## Shown"]
    # The checks that read tables keep the fence: a table in its paragraph is not published.
    assert [line for line in content("```\nfenced\n``` trailing\nstill fenced\n").lines if line.strip()] == []


def test_a_fence_closes_only_on_its_own_character() -> None:
    assert mask_fences_and_comments(["~~~", "```", "## Hidden", "~~~", "## Shown"]) == ["", "", "", "", "## Shown"]


def test_a_closed_fence_after_one_that_never_closes_is_still_hidden() -> None:
    lines = ["````py", "## Shown", "```", "hidden", "```", "after"]

    assert mask_fences_and_comments(lines) == ["````py", "## Shown", "", "", "", "after"]


def test_a_fence_that_never_closes_is_reread_with_the_comment_state_it_opened_in() -> None:
    lines = ["<!-- a comment", "ends --> ```", "## Shown"]

    assert mask_fences_and_comments(lines) == ["", " ```", "## Shown"]


def test_a_later_longer_fence_line_closes_a_fence_whose_own_closing_line_is_not_bare() -> None:
    # A known limit: the page needs a closing line of the opener's own length,
    # so it draws this fence as text and shows the heading.
    lines = ["```lean", "x", "``` -- end", "## Depends on", "````", "code", "````"]

    assert mask_fences_and_comments(lines) == ["", "", "", "", "", "code", "````"]


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


def test_an_unclosed_comment_hides_every_heading_after_it() -> None:
    parts = article_parts("# Title\n\nBody.\n\n<!-- todo\n\n## Depends on\n\n- [Base](base.md)\n")

    assert parts.sections == ()


def test_a_comment_opened_on_the_title_line_still_hides_what_it_covers() -> None:
    parts = article_parts("# Title <!-- TODO:\nwrite the statement -->\n\n## Depends on\n\nNone.\n")

    assert parts.statement == "<!-- TODO:\nwrite the statement -->"
    assert content_lines(parts.statement) == ["", ""]


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
    ],
)
def test_is_placeholder_leaves_sentences_alone(visible: str) -> None:
    assert not is_placeholder(visible)


def test_a_dash_joining_two_words_does_not_mark_a_placeholder() -> None:
    assert not is_placeholder("Unknown\u2013known duality holds for every pair.")
    assert not is_placeholder("Unknown-variance case.")
    for marker in ("TODO \u2013 later", "TODO\u2013 later", "TBD\u2014write this", "Unknown: x", "TODO - x"):
        assert is_placeholder(marker), marker


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
