"""The warning release before 0.9.0 reads an article's links as the site does: check and audit warn, beside today's
reader, wherever that would change an outcome, and the graph stays as it is. One test for each kind of warning, then
the two cuts that keep reading as the site does cheap: pages that hold no link are not converted, and code is not
highlighted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pymdownx.highlight import HighlightExtension

from autoform_cli import markdown as markdown_module
from autoform_cli.__main__ import main
from autoform_cli.audit import audit_blueprint
from autoform_cli.graph import load_graph
from autoform_cli.markdown import _link_converter, render_html, site_converter, site_links
from autoform_cli.site_reading import _Change, _paired
from tests.blueprint_fixture import _ensure_chapter
from tests.facts_fixture import write_annotated
from tests.test_markdown import LINK_CORPUS, LINK_DESTINATIONS

STOPS = "write it as a link the page shows, or delete it"


def _article(blueprint: Path, relative: str, body: str) -> None:
    _ensure_chapter(blueprint, relative)
    write_annotated(blueprint / "roadmap" / relative, f"---\n---\n{body}")


def _vault(tmp_path: Path, top: str, **others: str) -> Path:
    """A blueprint whose article ``top`` is ``# Top`` and *top*, from line 5, beside ``base`` and ``lemma``."""

    blueprint = tmp_path / "blueprint"
    for name, body in {"base": "# Base\n", "lemma": "# Lemma\n", **others}.items():
        _article(blueprint, f"{name}.md", body)
    _article(blueprint, "top.md", f"# Top\n\n{top}")
    return blueprint


def _warnings(blueprint: Path) -> list[str]:
    """The site-reading warnings, as check prints them; reading them leaves the graph as it is."""

    graph = load_graph(blueprint, site_reading=True)
    plain = load_graph(blueprint)
    assert (graph.nodes, graph.warnings, plain.site_reading_warnings) == (plain.nodes, plain.warnings, ())
    return [f"{node_id}: {warning}" for node_id, warning in graph.site_reading_warnings]


# One warning for each link.


def test_a_link_the_page_shows_as_code_warns_that_it_stops_counting(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, "## Depends on\n\n- `see [Base](base.md) and\n  more`\n")

    assert _warnings(blueprint) == [
        f"top: line 7: the site shows no link here, so from 0.9.0 'base.md' stops counting under '## Depends on'; {STOPS}"
    ]
    assert load_graph(blueprint).nodes["top"].dependencies == ("base",)


@pytest.mark.parametrize("element", ["textarea", "xmp"])
def test_a_link_in_a_raw_text_element_is_none_and_warns_that_it_stops_counting(tmp_path: Path, element: str) -> None:
    # The converter writes the <a>, but a browser shows what follows <textarea> or <xmp> as text.
    written = f"A <{element}>\n\n[Base](base.md)\n\n</{element}>\n\n[Lemma](lemma.md)\n"
    blueprint = _vault(tmp_path, f"## Depends on\n\n{written}")

    assert _warnings(blueprint) == [
        f"top: line 9: the site shows no link here, so from 0.9.0 'base.md' stops counting under '## Depends on'; {STOPS}"
    ]
    assert [link.href for link in site_links(f"# Top\n\n{written}")] == ["lemma.md"]


def test_a_dependency_only_the_site_shows_warns_that_it_will_count(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, "## Depends on\n\n- See [base].\n- [base]: base.md\n")

    assert _warnings(blueprint) == [
        "top: line 8: the site shows a link to 'base.md' under '## Depends on' that autoform does not read; from 0.9.0 "
        "it counts as a dependency"
    ]
    assert load_graph(blueprint).nodes["top"].dependencies == ()


@pytest.mark.parametrize(
    ("target", "problem"),
    [
        ("missing.md", "dependency target does not exist"),
        ("top.md", "dependency on itself"),
        ("cycle.md", "dependency cycle: top -> cycle -> top"),
        ("<https://example.org/x>", "dependency target must be a relative Markdown path"),
    ],
    ids=["missing", "itself", "cycle", "autolink"],
)
def test_a_dependency_only_the_site_shows_that_would_refuse_warns_that_it_will_be_an_error(
    tmp_path: Path, target: str, problem: str
) -> None:
    cycle = "# Cycle\n\n## Depends on\n\n[Top](top.md)\n"
    if target.startswith("<"):
        blueprint, href, line = _vault(tmp_path, f"## Depends on\n\n{target}\n", cycle=cycle), target[1:-1], 7
    else:
        blueprint, href, line = (
            _vault(tmp_path, f"## Depends on\n\n- See [x].\n- [x]: {target}\n", cycle=cycle),
            target,
            8,
        )

    assert _warnings(blueprint) == [
        f"top: line {line}: the site shows a link to {href!r} under '## Depends on' that autoform does not read; "
        f"from 0.9.0 it is an error: {problem}"
    ]


def test_a_wrapped_link_autoform_warns_about_today_gets_no_second_warning(tmp_path: Path) -> None:
    # #325's warning stands for it; the flip makes it the plain error, as suite.md's class (c).
    blueprint = _vault(
        tmp_path, "## Depends on\n\n- [Base\n  theorem](missing.md)\n\n## Sources\n\n- [A\n  paper](missing.md)\n"
    )

    assert _warnings(blueprint) == []
    assert load_graph(blueprint).warnings == ("top: dependency target does not exist: 'missing.md'; not counted",)


def test_an_address_the_site_reads_whole_warns_where_today_s_reading_fails(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, "## Sources\n\n- [Note](../notes/a_(b).md)\n- [Paper](https://x.org/a_(b))\n")
    (blueprint / "notes").mkdir()
    (blueprint / "notes" / "a_(b).md").write_text("# Note\n", encoding="utf-8")

    # Today's reader cuts both at the parenthesis; only the local one is judged, and only today.
    assert _warnings(blueprint) == [
        "top: line 7: the site links '../notes/a_(b).md', autoform reads '../notes/a_(b'; from 0.9.0 autoform reads it "
        "as the site does"
    ]


def test_a_sources_link_only_the_site_shows_warns_only_where_check_or_audit_judges_it(tmp_path: Path) -> None:
    sources = "## Sources\n\n- See [p].\n- [p]: missing.md\n- <a@b.org>\n- <https://example.org/y>\n"
    blueprint = _vault(tmp_path, sources)

    # The autolink to an external source counts from 0.9.0, and neither check nor audit judges it.
    assert _warnings(blueprint) == [
        "top: line 8: the site shows a link to 'missing.md' under '## Sources' that autoform does not read; from "
        "0.9.0 it is an error: source link does not resolve to a file",
        "top: line 9: the site shows a link to 'mailto:a@b.org' under '## Sources' that autoform does not read; from "
        "0.9.0 audit reports it: source link uses unsupported scheme",
    ]


def test_an_informal_link_only_the_site_shows_warns_that_it_will_count_or_warn(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, "## Informally uses\n\n- See [b] and [m].\n- [b]: base.md\n- [m]: missing.md\n")

    assert _warnings(blueprint) == [
        "top: line 8: the site shows a link to 'base.md' under '## Informally uses' that autoform does not read; from "
        "0.9.0 it counts as an informal use",
        "top: line 9: the site shows a link to 'missing.md' under '## Informally uses' that autoform does not read; "
        "from 0.9.0 check warns: informal target does not exist",
    ]


def test_a_link_into_an_unpublished_folder_only_the_site_shows_warns_that_it_will_be_an_error(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, "See [x].\n\n- [x]: hidden/x.md\n")
    _article(blueprint, "hidden/x.md", "# X\n")
    write_annotated(blueprint / "roadmap" / "hidden" / "README.md", "---\npublished: false\n---\n# Hidden\n")

    assert _warnings(blueprint) == [
        "top: line 7: the site shows a link to 'hidden/x.md' that autoform does not read; from 0.9.0 it is an error: "
        "links to 'hidden/x.md', in an unpublished folder render omits"
    ]


def test_an_informal_link_into_an_unpublished_folder_only_the_site_shows_warns_once_that_it_will_be_an_error(
    tmp_path: Path,
) -> None:
    blueprint = _vault(tmp_path, "## Informally uses\n\n- See [x].\n- [x]: hidden/x.md\n")
    _article(blueprint, "hidden/x.md", "# X\n")
    write_annotated(blueprint / "roadmap" / "hidden" / "README.md", "---\npublished: false\n---\n# Hidden\n")

    assert _warnings(blueprint) == [
        "top: line 8: the site shows a link to 'hidden/x.md' under '## Informally uses' that autoform does not read; "
        "from 0.9.0 it is an error: links to 'hidden/x.md', in an unpublished folder render omits"
    ]


def test_a_reference_link_defined_under_a_later_heading_counts_where_the_page_shows_it(tmp_path: Path) -> None:
    # Its line is the definition's, under '## Notes'; the page shows the link under '## Depends on'.
    blueprint = _vault(tmp_path, "## Depends on\n\nSee [base].\n\n## Notes\n\n- [base]: base.md\n")

    assert _warnings(blueprint) == [
        "top: line 11: the site shows a link to 'base.md' under '## Depends on' that autoform does not read; from "
        "0.9.0 it counts as a dependency"
    ]


def test_a_line_where_both_readers_part_on_several_links_pairs_one_and_keeps_the_rest() -> None:
    gone = [_Change("depends on", 7, None, "a.md"), _Change("depends on", 7, None, "b.md")]
    shown = _Change("depends on", 7, "c.md", None)

    assert _paired([*gone, shown]) == [gone[1], _Change("depends on", 7, "c.md", "a.md")]
    assert _paired([*gone, _Change("depends on", 7, "c.md", None, kind="reference")]) == [
        *gone,
        _Change("depends on", 7, "c.md", None, kind="reference"),
    ]


def test_a_coverage_link_only_the_site_shows_warns_in_audit(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, "## Depends on\n\n- See [b].\n- [b]: base.md\n")
    (blueprint / "coverage").mkdir()
    (blueprint / "coverage" / "README.md").write_text("# Coverage\n\nSee [m].\n\n- [m]: missing.md\n", encoding="utf-8")

    warnings = [(w.article_path, w.message) for w in audit_blueprint(blueprint).warnings if w.code == "site-reading"]

    assert warnings == [
        (
            "coverage/README.md",
            "line 5: the site shows a link to 'missing.md' that autoform does not read; from 0.9.0 audit reports it: "
            "coverage link does not resolve to a file",
        ),
        (
            "roadmap/top.md",
            "line 8: the site shows a link to 'base.md' under '## Depends on' that autoform does not read; from 0.9.0 "
            "it counts as a dependency",
        ),
    ]


def test_a_page_the_site_s_converter_cannot_read_warns(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, "## Depends on\n\n" + "- " * 500 + "[Base](base.md)\n")

    [warning] = _warnings(blueprint)

    assert warning.startswith("top: the site's Markdown converter fails on this page: ")
    assert warning.endswith("; from 0.9.0 autoform reads no link on this page")


# One warning for each heading the site reads otherwise, where it moves a link into or out of a section.


@pytest.mark.parametrize(
    ("heading", "written"),
    [("Notes\n---", "'Notes' over '---'"), ("> ## Notes", "'> ## Notes'")],
    ids=["setext", "quoted"],
)
def test_a_heading_only_the_site_reads_warns_once_for_the_links_below_it(
    tmp_path: Path, heading: str, written: str
) -> None:
    top = f"## Depends on\n\n[Base](base.md)\n\n{heading}\n\n[Lemma](lemma.md) and [Lemma again](lemma.md)\n"
    blueprint = _vault(tmp_path, top)

    assert _warnings(blueprint) == [
        f"top: line 9: the site reads {written} as a heading and autoform does not; from 0.9.0 sections follow the "
        "site's headings, so the links below it leave '## Depends on'"
    ]


def test_an_indented_heading_only_autoform_reads_warns_once_for_the_links_below_it(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, "Text.\n\n   ## Depends on\n\n[Base](base.md) and [Lemma](lemma.md)\n")

    assert _warnings(blueprint) == [
        "top: line 7: autoform reads '   ## Depends on' as a heading and the site does not, as it is indented; from "
        "0.9.0 its links stop counting as dependencies"
    ]


def test_a_later_h1_the_site_ends_a_section_at_warns_once_for_the_links_below_it(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, "## Depends on\n\n[Base](base.md)\n\n# Later\n\n[Lemma](lemma.md)\n")

    assert _warnings(blueprint) == [
        "top: line 9: the site ends the section at the later H1 '# Later' and autoform does not; from 0.9.0 sections "
        "follow the site's headings, so the links below it leave '## Depends on'"
    ]


def test_a_heading_the_site_names_otherwise_warns_once_for_the_links_below_it(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, "## Depends on {#deps}\n\n[Base](base.md) and [Lemma](lemma.md)\n")

    assert _warnings(blueprint) == [
        "top: line 5: the site names the heading '## Depends on {#deps}' 'Depends on' and autoform 'depends on "
        "{#deps}'; from 0.9.0 sections follow the site's headings, so the links below it count under '## Depends on'"
    ]


@pytest.mark.parametrize(
    ("top", "warning"),
    [
        (
            "Text.\n\n   ## Depends on\n\nSee [base].\n\n[base]: base.md\n",
            "line 7: autoform reads '   ## Depends on' as a heading and the site does not, as it is indented; from "
            "0.9.0 its links stop counting as dependencies",
        ),
        (
            "## Depends on\n\n[Lemma](lemma.md)\n\nNotes\n---\n\nSee [base].\n\n[base]: base.md\n",
            "line 9: the site reads 'Notes' over '---' as a heading and autoform does not; from 0.9.0 sections follow "
            "the site's headings, so the links below it leave '## Depends on'",
        ),
    ],
    ids=["indented", "setext"],
)
def test_a_reference_link_a_heading_moves_warns_once_for_the_heading(tmp_path: Path, top: str, warning: str) -> None:
    # Where the page shows it is the loader's line for it, not the site's, which is its definition's.
    assert _warnings(_vault(tmp_path, top)) == [f"top: {warning}"]


# No warning where nothing changes.


def test_a_page_both_readers_count_alike_warns_about_nothing(tmp_path: Path) -> None:
    top = (
        "Prose [Lemma](lemma.md), <https://example.org/prose> and\n> ## Notes\n\n"
        '## Depends on\n\n- [Base\n  theorem](base.md)\n- [x <div>y</div> z](lemma.md)\n- <a href="lemma.md">raw</a>\n\n'
        "## Sources\n\n- <https://example.org/a> and [DOI](https://doi.org/10.1/0304-405X(79)90015-1)\n"
    )
    blueprint = _vault(tmp_path, top)

    assert _warnings(blueprint) == []


def test_check_and_audit_give_the_warnings_and_other_readers_do_not(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = _vault(tmp_path, "## Depends on\n\n- See [base].\n- [base]: base.md\n")
    warning = (
        "line 8: the site shows a link to 'base.md' under '## Depends on' that autoform does not read; from 0.9.0 it "
        "counts as a dependency"
    )

    assert main(["check", str(blueprint), "--no-site-warnings"]) == 0
    assert f"warning: top: {warning}\n" in capsys.readouterr().out
    audit = [(w.article_path, w.code, w.message) for w in audit_blueprint(blueprint).warnings]
    assert ("roadmap/top.md", "site-reading", warning) in audit
    assert load_graph(blueprint).site_reading_warnings == ()


# The cuts: a page that holds no link is not converted, and code is not highlighted.


@pytest.mark.parametrize(
    "text",
    ["[a](a.md)\n", "[a]\n\n[a]: a.md\n", "<https://x.org/a>\n", "<a@b.org>\n", "[a]<!-- note -->(a.md)\n"],
    ids=["inline", "reference", "autolink", "mail", "comment"],
)
def test_a_page_that_can_hold_a_link_is_read(text: str) -> None:
    assert len(site_links(f"# T\n\n{text}")) == 1


def test_a_page_that_cannot_hold_a_link_is_not_converted(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(page: str) -> None:
        raise AssertionError("converted")

    monkeypatch.setattr(markdown_module, "_site_tree", fail)

    assert site_links("# T\n\nNo link: [a] and [b] (c), x <y> `z`.\n") == []


def test_the_link_converter_is_the_site_s_but_for_highlighting(monkeypatch: pytest.MonkeyPatch) -> None:
    site, links = site_converter(), _link_converter()

    def processors(converter: object) -> list[object]:
        registries = ("preprocessors", "inlinePatterns", "treeprocessors", "postprocessors")
        found = [getattr(converter, name) for name in registries] + [converter.parser.blockprocessors]
        return [
            [
                (item.name, item.priority, type(registry[item.name]))
                for item in sorted(registry._priority, key=lambda item: -item.priority)
            ]
            for registry in found
        ]

    def highlight(converter: object) -> dict[str, object]:
        [extension] = [e for e in converter.registeredExtensions if isinstance(e, HighlightExtension)]
        return extension.get_pymdownx_highlight_settings()

    assert processors(links) == processors(site)
    assert [(type(e), e.getConfigs()) for e in links.registeredExtensions] == [
        (type(e), e.getConfigs()) for e in site.registeredExtensions
    ]
    assert highlight(links) == {**highlight(site), "use_pygments": False}
    assert highlight(site)["use_pygments"]
    # The links it reads are the site converter's, on pages with fences and indented code too.
    pages = [page for page, _ in LINK_CORPUS + LINK_DESTINATIONS]
    pages.append(
        "```lean\ntheorem x : 1 = 1 := rfl\n```\n\n    [code](c.md)\n\n> ```\n> [q](q.md)\n> ```\n\n[a](a.md)\n"
    )
    unhighlighted = [site_links(page) for page in pages]
    monkeypatch.setattr(markdown_module, "_link_converter", markdown_module._converter)
    assert unhighlighted == [site_links(page) for page in pages]
    assert 'class="highlight"' in render_html("```lean\ntheorem x : 1 = 1 := rfl\n```\n")
