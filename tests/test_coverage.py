from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from autoform_cli.coverage import (
    COVERAGE_SCHEMA,
    CoverageRoleIssue,
    load_coverage,
    validate_coverage_roles,
)
from autoform_cli.graph import load_graph


def _article(blueprint: Path, relative: str) -> None:
    path = blueprint / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Roadmap article\n", encoding="utf-8")


def _role_article(blueprint: Path, relative: str, **metadata: str) -> None:
    path = blueprint / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    properties = [*(f"{key}: {value}" for key, value in metadata.items())]
    lines = ["---", *properties, "---", "", "# Roadmap article", ""]
    if metadata.get("catalog") == "module":
        ledger = blueprint / "sources" / f"{path.stem}-ledger.md"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("# Module declaration ledger\n", encoding="utf-8")
        lines.extend(["## Sources", "", f"- [Ledger](../sources/{ledger.name})", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def _contract(blueprint: Path, rows: str) -> Path:
    path = blueprint / "coverage" / "README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        f"{rows}",
        encoding="utf-8",
    )
    return path


def _raw_contract(blueprint: Path, body: str) -> Path:
    """Write a coverage file verbatim, for cases about the table's own shape."""

    path = blueprint / "coverage" / "README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


_HEADER = "| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
_TABLE = "# Coverage\n\n" + _HEADER
_NO_TABLE = (
    "coverage table does not render as a table; check for an HTML comment in "
    "the header or separator, and for a missing blank line above it"
)
_NO_CONTENT = "coverage evidence has no substantive content"
_HTML_HEADER = "<table><thead><tr><th>Area</th><th>Coverage</th><th>Evidence</th></tr></thead>"

# Each row is a coverage file and what load_coverage must report for it: a
# substring of the first reason, or the reasons, lines or (line, reason) pairs.
_REJECTED = [
    # The unclosed <style> swallows the table below it, so a reader sees only the
    # first table. Replacing the row removes the <style> and exposes a table that
    # offers the marker; substituting rows is only sound if it changes nothing else.
    pytest.param(
        "# Coverage\n\n"
        f"{_HTML_HEADER}<tbody><tr><td>A</td><td>OUT</td><td>reason</td></tr></tbody></table>\n\n"
        "Intro with no blank line\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| A<style> | OUT | reason |\n\n"
        f"{_HTML_HEADER}<tbody><tr><td>&#97;utoformcoveragerowmarker0</td><td>OUT</td>"
        "<td>reason</td></tr></tbody></table>\n",
        "changes what else the page renders",
        id="a_row_that_exposes_a_table_cannot_supply_its_own_proof",
    ),
    # A reader sees two contract tables: the hidden fourth column is not a column.
    pytest.param(
        _TABLE + "| Main | OUT | Not in scope |\n\n"
        "<table><thead><tr><th>Area</th><th>Coverage</th><th>Evidence</th>"
        "<th hidden>Notes</th></tr></thead>"
        "<tbody><tr><td>Other</td><td>OUT</td><td>reason</td>"
        "<td hidden>aside</td></tr></tbody></table>\n",
        ["coverage contract has multiple coverage tables"],
        id="a_hidden_column_does_not_disguise_a_second_contract",
    ),
    # The source table renders as a paragraph and the raw-HTML table publishes
    # byte-identical rows, so provenance has to be established, not inferred.
    pytest.param(
        "# Coverage\n\nIntro with no blank line\n" + _HEADER + "| Main | OUT | Not in scope |\n\n"
        f"{_HTML_HEADER}<tbody><tr><td>Main</td><td>OUT</td><td>Not in scope</td></tr></tbody></table>\n",
        [
            "coverage rows are not the rows the page publishes; "
            "the table a reader sees was not produced by these lines"
        ],
        id="identical_rows_cannot_borrow_a_published_table",
    ),
    # Matching on headers alone would let the unpublished rows stand as the contract.
    pytest.param(
        "# Coverage\n\nIntro with no blank line\n" + _HEADER + "| Main | OUT | Not in scope |\n\n"
        f"{_HTML_HEADER}<tbody><tr><td>Other</td><td>OUT</td><td>reason</td></tr></tbody></table>\n",
        "not the rows the page publishes",
        id="unrendered_rows_cannot_borrow_another_published_table",
    ),
    # Self-closing syntax does not apply to a span, so the browser keeps it open.
    pytest.param(
        _TABLE + "| Appendix | OUT | <span hidden />Reason |\n",
        [_NO_CONTENT],
        id="a_self_closing_hidden_element_still_hides_what_follows",
    ),
    # A paragraph runs straight into the header, so no table is published at all.
    pytest.param(
        "# Coverage\n\nIntro without a separating blank line\n" + _HEADER
        + "| Main | OUT | Not in scope |\n",
        [_NO_TABLE],
        id="a_table_with_no_blank_line_above_it_is_rejected",
    ),
    pytest.param(
        "# Coverage\n\nArea | Coverage | Evidence\n--- | --- | ---\nMain | OUT | Not in scope\n",
        ["coverage table must be written with a leading and trailing pipe on every row"],
        id="a_table_written_without_outer_pipes_is_named",
    ),
    pytest.param(
        "# Coverage\n\n> | Area | Coverage | Evidence |\n> | --- | --- | --- |\n> | Main | OUT | Not in scope |\n",
        [(3, "coverage table must be a top-level table; remove the blockquote markers")],
        id="a_blockquoted_table_has_an_actionable_diagnostic",
    ),
    # A browser closes the span and keeps hiding what follows it.
    pytest.param(
        _TABLE + "| Appendix | OUT | <span hidden>reason |\n",
        [_NO_CONTENT],
        id="evidence_hidden_by_a_malformed_element_is_not_evidence",
    ),
    # The column count survives the comment but the delimiter syntax does not.
    pytest.param(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --<!-- note -->- |\n"
        "| Main | OUT | Not in scope |\n",
        [_NO_TABLE],
        id="a_comment_breaking_the_separator_is_rejected",
    ),
    # The blank line belongs to the comment, not to the end of the table.
    pytest.param(
        _TABLE + "| Narrative | OUT | Not in scope |\n<!-- reviewer note\n\nmore note -->\n"
        "| Main theorem | MAPPED | Needs roadmap articles |\n",
        [9],
        id="a_blank_line_inside_a_comment_cannot_shrink_the_contract",
    ),
    pytest.param(
        _TABLE + "| Narrative | OUT | Not in scope |\n```\n\nexample\n```\n"
        "| Main theorem | MAPPED | Needs roadmap articles |\n",
        [10],
        id="a_blank_line_inside_a_fence_cannot_shrink_the_contract",
    ),
    # A four-column row is malformed, but it is still a row somebody declared.
    pytest.param(
        _TABLE + "| A | OUT | Not in scope |\n<!-- reviewer note -->\n"
        "| B | MAPPED | needs roadmap | accidental extra cell |\n",
        [7],
        id="a_malformed_row_after_hidden_content_is_still_reported",
    ),
    # ```` trailing` is not a closing fence for pymdownx.superfences, so the
    # table below it renders inside the code block and publishes nothing.
    pytest.param(
        "# Coverage\n\n```\nnot a contract\n``` trailing\n" + _HEADER
        + "| Main | OUT | Not in scope |\n",
        ["coverage contract has no 'Area | Coverage | Evidence' table"],
        id="a_table_left_inside_a_fence_is_not_a_contract",
    ),
    pytest.param(
        "# Coverage\n\n| Area | Coverage | Evidence <!-- | hidden --> |\n| --- | --- | --- |\n"
        "| Main | OUT | Not in scope |\n",
        [_NO_TABLE],
        id="a_comment_adding_a_pipe_to_the_header_is_rejected",
    ),
    pytest.param(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- <!-- | --> |\n| Main | OUT | Not in scope |\n",
        [_NO_TABLE],
        id="a_comment_adding_a_pipe_to_the_separator_is_rejected",
    ),
    pytest.param(
        _TABLE + "| Appendix | DEFERRED | TODO: choose a milestone |\n",
        ["coverage evidence is a placeholder"],
        id="rejects_placeholder_evidence_that_carries_a_trailing_promise",
    ),
    # A renderer splits cells before stripping comments, so it sees four columns
    # here while masking leaves three. Parse neither; report the disagreement.
    pytest.param(
        _TABLE + "| Appendix | OUT <!-- | --> | narrative only |\n",
        ["an HTML comment changes this coverage row's column layout"],
        id="a_comment_that_changes_a_rows_columns_is_rejected",
    ),
    pytest.param(
        _TABLE + "| Malformed | DECOMPOSED | [Bad](../roadmap/bad%00path.md) |\n"
        "| Unparsable | DECOMPOSED | [Host](http://[oops/a.md) |\n",
        [
            "coverage link contains an invalid path: '../roadmap/bad%00path.md'",
            "coverage link is not a valid URL: 'http://[oops/a.md'",
        ],
        id="malformed_evidence_paths_are_reported_without_raising",
    ),
    pytest.param(
        _TABLE + "| Main theorem | DECOMPOSED | [Nodes](../roadmap/main/README.md |\n",
        ["DECOMPOSED coverage evidence must link to at least one roadmap article"],
        id="an_unclosed_markdown_link_cannot_certify_decomposition",
    ),
    pytest.param(
        _TABLE + "| Main theorem | DECOMPOSED | "
        "[Nodes](../roadmap/main/README.md) and [More](../roadmap/absent.md) |\n",
        ["coverage link does not resolve to a file: '../roadmap/absent.md'"],
        id="every_decomposition_link_must_resolve_not_merely_one",
    ),
    pytest.param(
        _TABLE + "| Main theorem | DECOMPOSED | [Nodes](../roadmap/main/README.md#absent-section) |\n",
        ["coverage link fragment does not resolve: '../roadmap/main/README.md#absent-section'"],
        id="a_decomposition_link_fragment_must_name_a_real_heading",
    ),
]

# Each row is a coverage file the page publishes as written, and the summary
# fields load_coverage must report for it.
_ACCEPTED = [
    # A second <p> implicitly closes the first, so the reader does see this.
    pytest.param(
        _TABLE + "| Appendix | OUT | <p hidden>aside<p>Genuinely out of scope |\n",
        {},
        id="an_implicitly_closed_hidden_paragraph_stops_hiding",
    ),
    # The renderer accepts this one: the rule is about what publishes, not about
    # banning comments near the table.
    pytest.param(
        "# Coverage\n\n| Area | Coverage <!-- note --> | Evidence |\n| --- | --- | --- |\n"
        "| Main | OUT | Not in scope |\n",
        {"areas": ["Main"]},
        id="a_comment_inside_a_header_cell_still_renders_a_table",
    ),
    pytest.param(
        _TABLE + "| A | OUT | Not in scope |\n<!-- reviewer note -->\nSee the roadmap | for details.\n",
        {},
        id="prose_after_a_comment_is_not_mistaken_for_a_row",
    ),
    pytest.param(
        _TABLE + "| Chapter one | MAPPED | Listed in the roadmap; source audit pending |\n",
        {"complete": False},
        id="accepts_a_trailing_status_note_beside_real_evidence",
    ),
    pytest.param(
        _TABLE + "| Appendix | DEFERRED | Unknown-variance case, excluded by agreement |\n",
        {},
        id="a_status_word_joined_to_the_next_by_a_hyphen_is_not_a_marker",
    ),
    pytest.param(
        _TABLE + "| Appendix | OUT | narrative only <!-- agreed with the author --> |\n",
        {"evidence": "narrative only"},
        id="a_comment_that_leaves_the_columns_alone_is_accepted",
    ),
    pytest.param(
        _TABLE + "| Main theorem | DECOMPOSED | [Nodes](../roadmap/main/README.md#roadmap-article) |\n",
        {"complete": True},
        id="a_decomposition_link_fragment_naming_a_real_heading_certifies_decomposition",
    ),
]

_SUMMARY_FIELDS = {
    "areas": lambda summary: [entry.area for entry in summary.entries],
    "evidence": lambda summary: summary.entries[0].evidence,
    "complete": lambda summary: summary.complete,
}


def _load(tmp_path: Path, contract: str):
    blueprint = tmp_path / "blueprint"
    if "../roadmap/main/README.md" in contract:
        _article(blueprint, "main/README.md")
    _raw_contract(blueprint, contract)
    return load_coverage(blueprint)


@pytest.mark.parametrize(("contract", "expected"), _REJECTED)
def test_coverage_contract_is_rejected(tmp_path: Path, contract: str, expected: str | list[object]) -> None:
    summary, issues = _load(tmp_path, contract)

    assert summary is None
    if isinstance(expected, str):
        assert expected in issues[0].reason
    elif isinstance(expected[0], int):
        assert [issue.line for issue in issues] == expected
    elif isinstance(expected[0], tuple):
        assert [(issue.line, issue.reason) for issue in issues] == expected
    else:
        assert [issue.reason for issue in issues] == expected


@pytest.mark.parametrize(("contract", "fields"), _ACCEPTED)
def test_coverage_contract_is_accepted(tmp_path: Path, contract: str, fields: dict[str, object]) -> None:
    summary, issues = _load(tmp_path, contract)

    assert issues == ()
    assert summary is not None
    for name, value in fields.items():
        assert _SUMMARY_FIELDS[name](summary) == value, name


def test_a_synthesised_marker_cannot_defeat_provenance(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # None of these contain the marker literally; rendering produces it. Checking
    # the marker against the source alone would miss every one of them, because
    # the comparison provenance performs is against published cell text.
    for area in (
        "&#97;utoformcoveragerowmarker0",
        "autoform<span></span>coveragerowmarker0",
        "autoform<!--x-->coveragerowmarker0",
    ):
        _raw_contract(
            blueprint,
            "# Coverage\n\n"
            "Intro with no blank line\n"
            "| Area | Coverage | Evidence |\n"
            "| --- | --- | --- |\n"
            f"| {area} | OUT | Not in scope |\n\n"
            "<table><thead><tr><th>Area</th><th>Coverage</th><th>Evidence</th></tr></thead>"
            f"<tbody><tr><td>{area}</td><td>OUT</td><td>Not in scope</td></tr></tbody></table>\n",
        )

        summary, issues = load_coverage(blueprint)

        assert summary is None, area
        assert "not the rows the page publishes" in issues[0].reason, area


def test_a_marker_collision_cannot_defeat_provenance(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # An author who writes the provenance marker as an area would otherwise let
    # an unrelated table answer for their own unrendered one.
    marker = "autoformcoveragerowmarker0"
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "Intro with no blank line\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        f"| {marker} | OUT | Not in scope |\n\n"
        "<table><thead><tr><th>Area</th><th>Coverage</th><th>Evidence</th></tr></thead>"
        f"<tbody><tr><td>{marker}</td><td>OUT</td><td>Not in scope</td></tr></tbody></table>\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert "not the rows the page publishes" in issues[0].reason


def test_a_hidden_table_does_not_make_the_contract_ambiguous(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # A reader sees one contract table, so there is nothing ambiguous here even
    # though the source holds two.
    hidden = (
        "<table><thead><tr><th>Area</th><th>Coverage</th><th>Evidence</th></tr></thead>"
        "<tbody><tr><td>Other</td><td>OUT</td><td>reason</td></tr></tbody></table>"
    )
    for wrapper in (f"<div hidden>\n{hidden}\n</div>", hidden.replace("<table>", "<table hidden>")):
        _raw_contract(
            blueprint,
            "# Coverage\n\n"
            "| Area | Coverage | Evidence |\n"
            "| --- | --- | --- |\n"
            "| Main | OUT | Not in scope |\n\n"
            f"{wrapper}\n",
        )

        summary, issues = load_coverage(blueprint)

        assert issues == (), wrapper
        assert summary is not None, wrapper
        assert [entry.area for entry in summary.entries] == ["Main"], wrapper


def test_the_word_hidden_in_an_attribute_value_is_still_evidence(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # `title="hidden"` hides nothing, and neither does `aria-hidden` for a
    # sighted reader. Matching the word rather than the attribute rejected both.
    for evidence in (
        '<span title="hidden">Out of scope for this thesis</span>',
        '<span aria-hidden="true">Out of scope for this thesis</span>',
    ):
        _contract(blueprint, f"| Appendix | OUT | {evidence} |\n")

        summary, issues = load_coverage(blueprint)

        assert issues == (), evidence
        assert summary is not None, evidence


def test_a_stranded_row_without_outer_pipes_is_reported(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # Python-Markdown accepts rows written without either outer pipe, so a
    # stranded one has to be reported even though it fails the canonical form.
    for row in (
        "| B | MAPPED | needs roadmap",
        "B | MAPPED | needs roadmap |",
        "B | MAPPED | needs roadmap",
    ):
        _raw_contract(
            blueprint,
            "# Coverage\n\n"
            "| Area | Coverage | Evidence |\n"
            "| --- | --- | --- |\n"
            "| A | OUT | Not in scope |\n"
            "<!-- reviewer note -->\n"
            f"{row}\n",
        )

        summary, issues = load_coverage(blueprint)

        assert summary is None, row
        assert [issue.line for issue in issues] == [7], row


def test_evidence_hidden_from_the_reader_is_not_evidence(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # Each of these renders no text a browser will show, so none of them is a
    # reason for a disposition.
    for evidence in (
        "<span hidden>reason</span>",
        "<script>reason</script>",
        "<style>reason</style>",
        "<template>reason</template>",
    ):
        _contract(blueprint, f"| Appendix | OUT | {evidence} |\n")

        summary, issues = load_coverage(blueprint)

        assert summary is None, evidence
        assert [issue.reason for issue in issues] == [
            "coverage evidence has no substantive content"
        ], evidence


def test_evidence_that_renders_nothing_is_not_evidence(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # Each of these carries word characters in the Markdown source and shows the
    # reader nothing at all.
    for evidence in ("[ ](missing.md)", "<span></span>", "&nbsp;", "[]()"):
        _contract(blueprint, f"| Appendix | OUT | {evidence} |\n")

        summary, issues = load_coverage(blueprint)

        assert summary is None, evidence
        assert [issue.reason for issue in issues] == [
            "coverage evidence has no substantive content"
        ], evidence


def test_an_empty_link_cannot_certify_decomposition(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "main/README.md")
    # The destination resolves, but the link publishes no label, so the cell
    # tells a reader nothing about where the work went.
    _contract(blueprint, "| Main theorem | DECOMPOSED | [ ](../roadmap/main/README.md) |\n")

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == ["coverage evidence has no substantive content"]


def test_loads_canonical_coverage_summary_with_stable_json(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "main/README.md")
    path = _contract(
        blueprint,
        "| Main theorem | `DECOMPOSED` | [Nodes](../roadmap/main/README.md) |\n"
        "| Corollaries | MAPPED | Source audit pending |\n"
        "| Experiments | OUT | Narrative only |\n"
        "| Appendix | DEFERRED | Revisit after milestone one |\n",
    )

    first, issues = load_coverage(blueprint)
    second, repeated_issues = load_coverage(blueprint)

    assert issues == repeated_issues == ()
    assert first == second
    assert first is not None
    assert COVERAGE_SCHEMA == "autoform-coverage/v2"
    assert first.schema == COVERAGE_SCHEMA
    assert first.source_path == "coverage/README.md"
    assert first.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first.counts == {
        "MAPPED": 1,
        "DECOMPOSED": 1,
        "INVENTORIED": 0,
        "DEFERRED": 1,
        "OUT": 1,
    }
    assert not first.complete
    assert first.to_json() == second.to_json()
    assert str(tmp_path) not in first.to_json()


def test_complete_means_every_in_scope_area_has_a_terminal_disposition(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "main/README.md")
    _article(blueprint, "inventory.md")
    _contract(
        blueprint,
        "| Main theorem | DECOMPOSED | [Nodes](../roadmap/main/README.md) |\n"
        "| Existing modules | INVENTORIED | [Catalog](../roadmap/inventory.md) |\n"
        "| Appendix | DEFERRED | Explicit later milestone |\n"
        "| Experiments | OUT | Narrative only |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert summary.complete
    assert summary.counts["INVENTORIED"] == 1


def test_coverage_loading_does_not_infer_article_roles(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    article = blueprint / "roadmap" / "theorem.md"
    article.parent.mkdir(parents=True)
    article.write_text(
        "---\ndeclaration: theorem\n---\n\n# Theorem\n",
        encoding="utf-8",
    )
    _contract(
        blueprint,
        "| Existing modules | INVENTORIED | [Article](../roadmap/theorem.md) |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert summary.counts["INVENTORIED"] == 1
    role_issues = validate_coverage_roles(load_graph(blueprint), summary)
    assert role_issues == (
        CoverageRoleIssue(
            5,
            "coverage-role-mismatch",
            "coverage area 'Existing modules' is INVENTORIED, but each roadmap link must "
            "resolve to a catalog leaf or a container with catalog descendants; "
            "roadmap/theorem.md reaches no catalog leaf",
        ),
    )


def test_validate_coverage_roles_accepts_direct_role_leaves(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _role_article(blueprint, "result.md", declaration="theorem")
    _role_article(blueprint, "module.md", catalog="module")
    _contract(
        blueprint,
        "| Main theorem | DECOMPOSED | [Result](../roadmap/result.md) |\n"
        "| Existing module | INVENTORIED | [Module](../roadmap/module.md) |\n",
    )
    summary, syntax_issues = load_coverage(blueprint)

    assert syntax_issues == ()
    assert summary is not None
    assert validate_coverage_roles(load_graph(blueprint), summary) == ()


def test_a_link_with_the_role_does_not_excuse_one_without_it(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _role_article(blueprint, "result.md", declaration="theorem")
    _role_article(blueprint, "module.md", catalog="module")
    _contract(
        blueprint,
        "| Existing module | INVENTORIED | [Module](../roadmap/module.md), [Result](../roadmap/result.md) |\n",
    )
    summary, syntax_issues = load_coverage(blueprint)

    assert syntax_issues == ()
    assert summary is not None
    assert validate_coverage_roles(load_graph(blueprint), summary) == (
        CoverageRoleIssue(
            5,
            "coverage-role-mismatch",
            "coverage area 'Existing module' is INVENTORIED, but each roadmap link must "
            "resolve to a catalog leaf or a container with catalog descendants; "
            "roadmap/result.md reaches no catalog leaf",
        ),
    )


def test_validate_coverage_roles_bounds_contract_findings(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    for index in range(5):
        _role_article(blueprint, f"module-{index}.md", catalog="module")
    _contract(
        blueprint,
        "| Wrong exposition | DECOMPOSED | [One](../roadmap/module-0.md), "
        "[two](../roadmap/module-1.md) |\n",
    )
    summary, syntax_issues = load_coverage(blueprint)

    assert syntax_issues == ()
    assert summary is not None
    assert validate_coverage_roles(load_graph(blueprint), summary) == (
        CoverageRoleIssue(
            5,
            "coverage-role-mismatch",
            "coverage area 'Wrong exposition' is DECOMPOSED, but each roadmap link must "
            "resolve to a declaration leaf or a container with declaration descendants; "
            "roadmap/module-0.md and 1 more reach no declaration leaf",
        ),
        CoverageRoleIssue(
            0,
            "unclassified-inventory",
            "coverage contract leaves 5 catalog articles outside INVENTORIED evidence "
            "(roadmap/module-0.md, roadmap/module-1.md, roadmap/module-2.md and 2 more); "
            "link each inventory area directly or through a containing roadmap article",
        ),
    )


def test_a_decomposed_row_short_of_declarations_warns_unless_it_reaches_only_catalogs(
    tmp_path: Path,
) -> None:
    """Earlier audits passed a DECOMPOSED row whatever it linked; only catalog-only evidence is new."""
    blueprint = tmp_path / "blueprint"
    _role_article(blueprint, "lemma.md", declaration="lemma")
    _role_article(blueprint, "notation.md")
    _role_article(blueprint, "draft.md", not_ready="true")
    _role_article(blueprint, "module.md", catalog="module")
    _contract(
        blueprint,
        "| Section 1 | DECOMPOSED | [Notation](../roadmap/notation.md), [Lemma](../roadmap/lemma.md), "
        "[Draft](../roadmap/draft.md) |\n"
        "| Section 2 | DECOMPOSED | [Module](../roadmap/module.md) |\n"
        "| Section 3 | DECOMPOSED | [Lemma](../roadmap/lemma.md), [Module](../roadmap/module.md) |\n"
        "| Inventory | INVENTORIED | [Module](../roadmap/module.md) |\n",
    )
    summary, syntax_issues = load_coverage(blueprint)

    assert syntax_issues == ()
    assert summary is not None
    assert validate_coverage_roles(load_graph(blueprint), summary) == (
        CoverageRoleIssue(
            5,
            "decomposed-without-declaration",
            "coverage area 'Section 1' is DECOMPOSED, but roadmap/notation.md, roadmap/draft.md "
            "reach no declaration leaf",
        ),
        CoverageRoleIssue(
            6,
            "coverage-role-mismatch",
            "coverage area 'Section 2' is DECOMPOSED, but each roadmap link must resolve to "
            "a declaration leaf or a container with declaration descendants; "
            "roadmap/module.md reaches no declaration leaf",
        ),
        CoverageRoleIssue(
            7,
            "decomposed-without-declaration",
            "coverage area 'Section 3' is DECOMPOSED, but roadmap/module.md reaches no declaration leaf",
        ),
    )


def test_inventoried_evidence_requires_a_roadmap_link(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _contract(blueprint, "| Existing modules | INVENTORIED | See the module ledger |\n")

    summary, issues = load_coverage(blueprint)

    assert summary is None
    reasons = [issue.reason for issue in issues]
    assert reasons == ["INVENTORIED coverage evidence must link to at least one roadmap article"]


def test_rejects_unknown_duplicate_and_malformed_rows(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _contract(
        blueprint,
        "| Main theorem | PARTIAL | Pending |\n"
        "| main THEOREM | MAPPED | Duplicate |\n"
        "| Missing evidence | DEFERRED | |\n"
        "| Too few | OUT |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    reasons = [issue.reason for issue in issues]
    assert any("unknown coverage disposition" in reason for reason in reasons)
    assert any("duplicate coverage area" in reason for reason in reasons)
    assert "coverage evidence is empty" in reasons
    assert "coverage row must have exactly three columns" in reasons


def test_ignores_fenced_tables_and_accepts_escaped_pipes(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    path = blueprint / "coverage/README.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# Coverage\n\n"
        "```markdown\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Example | MAPPED | Placeholder |\n"
        "```\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main theorem | MAPPED | Case A \\| Case B |\n",
        encoding="utf-8",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert len(summary.entries) == 1
    assert summary.entries[0].evidence == "Case A | Case B"


def test_a_fence_that_never_closes_keeps_the_table_below_it_out_of_the_contract(tmp_path: Path) -> None:
    # Left open at the margin, the fence makes the site show the page as typed,
    # so the rows below it never render as a table (#201).
    blueprint = tmp_path / "blueprint"
    path = blueprint / "coverage/README.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# Coverage\n\n```markdown\nexample\n\n"
        "| Area | Coverage | Evidence |\n| --- | --- | --- |\n| Main theorem | OUT | Not in scope |\n",
        encoding="utf-8",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert issues


def test_rejects_placeholder_and_unresolved_decomposed_evidence(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    source = blueprint / "sources" / "README.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Source\n", encoding="utf-8")
    _contract(
        blueprint,
        "| Placeholder | DEFERRED | TODO |\n"
        "| Missing | DECOMPOSED | [Missing](../roadmap/missing.md) |\n"
        "| Source only | DECOMPOSED | [Source](../sources/README.md) |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    reasons = [issue.reason for issue in issues]
    assert "coverage evidence is a placeholder" in reasons
    # A link that does not resolve is reported as such, with the audit's wording,
    # rather than as a generic missing-article complaint.
    assert "coverage link does not resolve to a file: '../roadmap/missing.md'" in reasons
    assert reasons.count("DECOMPOSED coverage evidence has no link to an existing roadmap article") == 1


def test_evidence_validation_ignores_decorated_placeholders_and_fake_links(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "encoded article.md")
    _contract(
        blueprint,
        "| Decorated placeholder | DEFERRED | **TODO.** |\n"
        "| Inline code | DECOMPOSED | `[Fake](../roadmap/encoded%20article.md)` |\n"
        "| Comment | DECOMPOSED | <!-- [Fake](../roadmap/encoded%20article.md) --> |\n"
        "| Encoded | DECOMPOSED | [Real](<../roadmap/encoded%20article.md>) |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    reasons = [issue.reason for issue in issues]
    assert "coverage evidence is a placeholder" in reasons
    # Neither a link inside inline code nor one inside a comment renders, so
    # neither can stand in for evidence: both cells are empty of content.
    assert "coverage evidence has no substantive content" in reasons
    assert "coverage evidence is empty" in reasons
    assert "DECOMPOSED coverage evidence has no link to an existing roadmap article" not in reasons


def test_a_link_labelled_only_with_code_certifies_decomposition(tmp_path: Path) -> None:
    """Issue #302: the page links the declaration's name, so the link counts."""
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "main.md")
    _contract(blueprint, "| Main theorem | DECOMPOSED | Proved in [`Main.thm`](../roadmap/main.md) |\n")

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None and summary.complete


def test_rejects_evidence_whose_only_content_is_a_comment_or_code_span(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _contract(
        blueprint,
        "| Code only | OUT | `TODO` |\n"
        "| Comment only | OUT | <!-- reason removed --> |\n"
        "| Emphasis only | OUT | **__** |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    reasons = [issue.reason for issue in issues]
    assert reasons.count("coverage evidence has no substantive content") == 2
    assert "coverage evidence is empty" in reasons


def test_a_placeholder_used_as_a_marker_is_rejected(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    for evidence in ("TODO: choose a milestone", "TBD - pick a milestone", "**TODO.**", "Pending"):
        _contract(blueprint, f"| Appendix | DEFERRED | {evidence} |\n")

        summary, issues = load_coverage(blueprint)

        assert summary is None, evidence
        assert [issue.reason for issue in issues] == ["coverage evidence is a placeholder"], evidence


def test_a_status_word_opening_a_sentence_is_not_a_placeholder(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # These name something a reader can check. Rejecting them on the first word
    # alone pushed authors toward vaguer wording to satisfy the checker.
    for evidence in ("Pending Mathlib PR 1234", "Unknown provenance, excluded by agreement"):
        _contract(blueprint, f"| Appendix | DEFERRED | {evidence} |\n")

        summary, issues = load_coverage(blueprint)

        assert issues == (), evidence
        assert summary is not None, evidence


@pytest.mark.parametrize("link", ["alias.md", "figure.txt"], ids=["symlink-to-another-folder", "not-markdown"])
def test_a_roadmap_link_that_names_no_markdown_file_in_the_roadmap_is_not_a_roadmap_article(
    tmp_path: Path, link: str
) -> None:
    blueprint = tmp_path / "blueprint"
    (blueprint / "sources").mkdir(parents=True)
    (blueprint / "sources" / "note.md").write_text("# Note\n", encoding="utf-8")
    (blueprint / "roadmap").mkdir()
    (blueprint / "roadmap" / "figure.txt").write_text("figure\n", encoding="utf-8")
    try:
        (blueprint / "roadmap" / "alias.md").symlink_to("../sources/note.md")
    except OSError:
        if link == "alias.md":
            pytest.skip("symlinks are unavailable")
    _contract(blueprint, f"| Linked | DECOMPOSED | [Linked](../roadmap/{link}) |\n")

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "DECOMPOSED coverage evidence has no link to an existing roadmap article"
    ]


def test_a_coverage_link_through_a_link_that_cannot_be_read_does_not_resolve(
    tmp_path: Path, unreadable_link: Callable[[Path, Path | str], None]
) -> None:
    blueprint = tmp_path / "blueprint"
    (blueprint / "sources").mkdir(parents=True)
    (blueprint / "sources" / "note.md").write_text("# Note\n", encoding="utf-8")
    (blueprint / "roadmap").mkdir()
    unreadable_link(blueprint / "roadmap" / "alias.md", "../sources/note.md")
    _contract(blueprint, "| Aliased | DECOMPOSED | [Alias](../roadmap/alias.md) |\n")

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == ["coverage link does not resolve to a file: '../roadmap/alias.md'"]


def test_coverage_of_a_blueprint_behind_a_link_that_cannot_be_read_matches_the_blueprint_itself(
    tmp_path: Path, unreadable_link: Callable[[Path, Path | str], None]
) -> None:
    real = tmp_path / "real" / "blueprint"
    (real / "roadmap").mkdir(parents=True)
    (real / "roadmap" / "result.md").write_text("# Result\n", encoding="utf-8")
    _contract(real, "| Result | DECOMPOSED | [Result](../roadmap/result.md) |\n")
    unreadable_link(tmp_path / "via", tmp_path / "real")

    assert load_coverage(tmp_path / "via" / "blueprint") == load_coverage(real)


def test_reports_stable_unreadable_contract_issue(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    path = blueprint / "coverage/README.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == ["coverage contract cannot be read as UTF-8"]
    assert str(tmp_path) not in issues[0].reason


def test_reports_missing_or_ambiguous_contract_table(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"

    summary, issues = load_coverage(blueprint)
    assert summary is None
    assert [issue.reason for issue in issues] == ["coverage contract is missing"]

    path = blueprint / "coverage" / "README.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Coverage\n\nNo table yet.\n", encoding="utf-8")
    summary, issues = load_coverage(blueprint)
    assert summary is None
    assert "has no 'Area | Coverage | Evidence' table" in issues[0].reason


def _write(blueprint: Path, body: str) -> Path:
    path = blueprint / "coverage" / "README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_a_table_inside_an_html_comment_is_not_the_contract(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write(
        blueprint,
        "# Coverage\n\n"
        "<!--\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Example | OUT | Narrative only |\n"
        "-->\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert "has no 'Area | Coverage | Evidence' table" in issues[0].reason


def test_a_four_space_indented_table_is_not_the_contract(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write(
        blueprint,
        "# Coverage\n\n"
        "    | Area | Coverage | Evidence |\n"
        "    | --- | --- | --- |\n"
        "    | Example | OUT | Narrative only |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert "has no 'Area | Coverage | Evidence' table" in issues[0].reason


def test_a_commented_example_does_not_make_the_contract_ambiguous(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "main/README.md")
    _write(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main theorem | DECOMPOSED | [Nodes](../roadmap/main/README.md) |\n"
        "\n"
        "<!-- For reference, the shape of a deferred row:\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Appendix | DEFERRED | Revisit after milestone one |\n"
        "-->\n",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert [entry.area for entry in summary.entries] == ["Main theorem"]
    assert summary.complete


def test_masking_preserves_the_authors_line_numbers(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write(
        blueprint,
        "# Coverage\n\n"
        "<!-- an aside\n"
        "spanning three lines\n"
        "-->\n"
        "\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Appendix | DEFERRED | |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [(issue.line, issue.reason) for issue in issues] == [(9, "coverage evidence is empty")]


def test_a_comment_between_rows_cannot_shrink_the_contract(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # The comment ends the table for every renderer, so the MAPPED row below it
    # is published by nobody. Dropping it silently would report the contract as
    # complete over a narrower claim than the author wrote.
    _write(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Narrative | OUT | Not a formalization target |\n"
        "<!-- reviewer note -->\n"
        "| Main theorem | MAPPED | Needs roadmap articles |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [(issue.line, issue.reason) for issue in issues] == [
        (
            7,
            "coverage row follows hidden content and would not be published; "
            "move the comment or code block below the table",
        )
    ]


def test_a_comment_that_swallows_rows_reports_each_survivor(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Narrative | OUT | Not a formalization target |\n"
        "<!--\n"
        "| Hidden | MAPPED | Inside the comment |\n"
        "-->\n"
        "| Visible | MAPPED | Below the comment |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    # Only the row that survives masking is actionable; the one inside the
    # comment is already understood to be an example.
    assert [issue.line for issue in issues] == [9]


def test_notes_after_the_table_do_not_need_to_move(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Narrative | OUT | Not a formalization target |\n"
        "<!-- AUTHORING NOTES, no rows below this point -->\n",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert [entry.area for entry in summary.entries] == ["Narrative"]
