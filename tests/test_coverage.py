from __future__ import annotations

import hashlib
from pathlib import Path

from autoform_cli.coverage import COVERAGE_SCHEMA, load_coverage


def test_schema_less_v1_preserves_unrelated_page_frontmatter(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _raw_contract(
        blueprint,
        "---\n"
        "title: Coverage\n"
        "tags:\n"
        "  - planning\n"
        "---\n\n"
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Scope | OUT | Explicitly excluded |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None and summary.schema == COVERAGE_SCHEMA


def _article(blueprint: Path, relative: str) -> None:
    path = blueprint / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Roadmap article\n", encoding="utf-8")


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


def test_a_row_that_exposes_a_table_cannot_supply_its_own_proof(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # The unclosed <style> in the candidate row swallows the table below it, so a
    # reader sees only the first table and the candidate's values match it.
    # Replacing the row removes the <style>, exposing a table that then offers the
    # marker. Substituting rows is only sound if it changes nothing else.
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "<table><thead><tr><th>Area</th><th>Coverage</th><th>Evidence</th></tr></thead>"
        "<tbody><tr><td>A</td><td>OUT</td><td>reason</td></tr></tbody></table>\n\n"
        "Intro with no blank line\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| A<style> | OUT | reason |\n\n"
        "<table><thead><tr><th>Area</th><th>Coverage</th><th>Evidence</th></tr></thead>"
        "<tbody><tr><td>&#97;utoformcoveragerowmarker0</td><td>OUT</td>"
        "<td>reason</td></tr></tbody></table>\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert "changes what else the page renders" in issues[0].reason


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


def test_a_hidden_column_does_not_disguise_a_second_contract(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # A reader sees two contract tables here: the hidden fourth column is not a
    # column at all, so it cannot excuse the second table from the count.
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main | OUT | Not in scope |\n\n"
        "<table><thead><tr><th>Area</th><th>Coverage</th><th>Evidence</th>"
        "<th hidden>Notes</th></tr></thead>"
        "<tbody><tr><td>Other</td><td>OUT</td><td>reason</td>"
        "<td hidden>aside</td></tr></tbody></table>\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == ["coverage contract has multiple coverage tables"]


def test_identical_rows_cannot_borrow_a_published_table(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # The source table renders as a paragraph, and the raw-HTML table below it
    # publishes byte-identical rows. Comparing values alone cannot tell these
    # apart, so provenance has to be established rather than inferred.
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "Intro with no blank line\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main | OUT | Not in scope |\n\n"
        "<table><thead><tr><th>Area</th><th>Coverage</th><th>Evidence</th></tr></thead>"
        "<tbody><tr><td>Main</td><td>OUT</td><td>Not in scope</td></tr></tbody></table>\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "coverage rows are not the rows the page publishes; "
        "the table a reader sees was not produced by these lines"
    ]


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


def test_unrendered_rows_cannot_borrow_another_published_table(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # The source table renders as a paragraph, but a raw-HTML table further down
    # publishes the same headers. Matching on headers alone would let the
    # unpublished rows stand as the contract.
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "Intro with no blank line\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main | OUT | Not in scope |\n\n"
        "<table><thead><tr><th>Area</th><th>Coverage</th><th>Evidence</th></tr></thead>"
        "<tbody><tr><td>Other</td><td>OUT</td><td>reason</td></tr></tbody></table>\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    # Provenance is the more fundamental failure: whatever the rows say, these
    # lines did not produce the table on the page.
    assert "not the rows the page publishes" in issues[0].reason


def test_a_self_closing_hidden_element_still_hides_what_follows(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # Self-closing syntax does not apply to non-void elements, so a browser keeps
    # the span open and never draws the text after it.
    _contract(blueprint, "| Appendix | OUT | <span hidden />Reason |\n")

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == ["coverage evidence has no substantive content"]


def test_an_implicitly_closed_hidden_paragraph_stops_hiding(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # A second <p> implicitly closes the first, so the reader does see this.
    _contract(blueprint, "| Appendix | OUT | <p hidden>aside<p>Genuinely out of scope |\n")

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None


def test_a_table_with_no_blank_line_above_it_is_rejected(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # Both structural lines are canonical, but a paragraph runs straight into the
    # header, so the renderer publishes one lazy paragraph and no table at all.
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "Intro without a separating blank line\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main | OUT | Not in scope |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "coverage table does not render as a table; check for an HTML comment in "
        "the header or separator, and for a missing blank line above it"
    ]


def test_a_table_written_without_outer_pipes_is_named(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # The page does publish a contract table here, so reporting "no table" would
    # be a lie. Name the form the audit needs instead.
    _raw_contract(
        blueprint,
        "# Coverage\n\nArea | Coverage | Evidence\n--- | --- | ---\nMain | OUT | Not in scope\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "coverage table must be written with a leading and trailing pipe on every row"
    ]


def test_a_blockquoted_table_has_an_actionable_diagnostic(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "> | Area | Coverage | Evidence |\n"
        "> | --- | --- | --- |\n"
        "> | Main | OUT | Not in scope |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [(issue.line, issue.reason) for issue in issues] == [
        (3, "coverage table must be a top-level table; remove the blockquote markers")
    ]


def test_evidence_hidden_by_a_malformed_element_is_not_evidence(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # A browser closes the span and keeps hiding what follows it.
    _contract(blueprint, "| Appendix | OUT | <span hidden>reason |\n")

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == ["coverage evidence has no substantive content"]


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


def test_a_comment_breaking_the_separator_is_rejected(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # The column count survives the comment but the delimiter syntax does not, so
    # the renderer publishes a paragraph rather than a table.
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --<!-- note -->- |\n"
        "| Main | OUT | Not in scope |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "coverage table does not render as a table; check for an HTML comment in "
        "the header or separator, and for a missing blank line above it"
    ]


def test_a_comment_inside_a_header_cell_still_renders_a_table(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # This one the renderer does accept, so the contract stands. The rule is
    # about what publishes, not about banning comments near the table.
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage <!-- note --> | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main | OUT | Not in scope |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert [entry.area for entry in summary.entries] == ["Main"]


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


def test_prose_after_a_comment_is_not_mistaken_for_a_row(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| A | OUT | Not in scope |\n"
        "<!-- reviewer note -->\n"
        "See the roadmap | for details.\n",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None


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


def test_a_blank_line_inside_a_comment_cannot_shrink_the_contract(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # The blank line belongs to the comment, so it must not be mistaken for the
    # blank line that ends a table -- otherwise the MAPPED row below vanishes.
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Narrative | OUT | Not in scope |\n"
        "<!-- reviewer note\n"
        "\n"
        "more note -->\n"
        "| Main theorem | MAPPED | Needs roadmap articles |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.line for issue in issues] == [9]


def test_a_blank_line_inside_a_fence_cannot_shrink_the_contract(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Narrative | OUT | Not in scope |\n"
        "```\n"
        "\n"
        "example\n"
        "```\n"
        "| Main theorem | MAPPED | Needs roadmap articles |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.line for issue in issues] == [10]


def test_a_malformed_row_after_hidden_content_is_still_reported(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # A four-column row is malformed, but it is still a row somebody declared.
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| A | OUT | Not in scope |\n"
        "<!-- reviewer note -->\n"
        "| B | MAPPED | needs roadmap | accidental extra cell |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.line for issue in issues] == [7]


def test_a_table_left_inside_a_fence_is_not_a_contract(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # ```` trailing` is not a closing fence for pymdownx.superfences, so the
    # table below it renders inside the code block and publishes nothing.
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "```\n"
        "not a contract\n"
        "``` trailing\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main | OUT | Not in scope |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "coverage contract has no 'Area | Coverage | Evidence' table"
    ]


def test_a_comment_adding_a_pipe_to_the_header_is_rejected(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence <!-- | hidden --> |\n"
        "| --- | --- | --- |\n"
        "| Main | OUT | Not in scope |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "coverage table does not render as a table; check for an HTML comment in "
        "the header or separator, and for a missing blank line above it"
    ]


def test_a_comment_adding_a_pipe_to_the_separator_is_rejected(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _raw_contract(
        blueprint,
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- <!-- | --> |\n"
        "| Main | OUT | Not in scope |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "coverage table does not render as a table; check for an HTML comment in "
        "the header or separator, and for a missing blank line above it"
    ]


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
    assert first.schema == COVERAGE_SCHEMA
    assert first.source_path == "coverage/README.md"
    assert first.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first.counts == {"MAPPED": 1, "DECOMPOSED": 1, "DEFERRED": 1, "OUT": 1}
    assert not first.complete
    assert first.to_json() == second.to_json()
    assert str(tmp_path) not in first.to_json()


def test_complete_means_every_in_scope_area_has_a_terminal_disposition(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "main/README.md")
    _contract(
        blueprint,
        "| Main theorem | DECOMPOSED | [Nodes](../roadmap/main/README.md) |\n"
        "| Appendix | DEFERRED | Explicit later milestone |\n"
        "| Experiments | OUT | Narrative only |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert summary.complete


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


def test_rejects_placeholder_evidence_that_carries_a_trailing_promise(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _contract(blueprint, "| Appendix | DEFERRED | TODO: choose a milestone |\n")

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == ["coverage evidence is a placeholder"]


def test_accepts_a_trailing_status_note_beside_real_evidence(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _contract(blueprint, "| Chapter one | MAPPED | Listed in the roadmap; source audit pending |\n")

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert not summary.complete


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


def test_a_comment_that_changes_a_rows_columns_is_rejected(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    # A renderer splits cells before stripping comments, so it sees four columns
    # here while masking leaves three. Parse neither; report the disagreement.
    _contract(blueprint, "| Appendix | OUT <!-- | --> | narrative only |\n")

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "an HTML comment changes this coverage row's column layout"
    ]


def test_a_comment_that_leaves_the_columns_alone_is_accepted(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _contract(blueprint, "| Appendix | OUT | narrative only <!-- agreed with the author --> |\n")

    summary, issues = load_coverage(blueprint)

    assert issues == ()
    assert summary is not None
    assert summary.entries[0].evidence == "narrative only"


def test_malformed_evidence_paths_are_reported_without_raising(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _contract(
        blueprint,
        "| Malformed | DECOMPOSED | [Bad](../roadmap/bad%00path.md) |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "coverage link contains an invalid path: '../roadmap/bad%00path.md'"
    ]


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


def test_an_unclosed_markdown_link_cannot_certify_decomposition(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "main/README.md")
    _contract(blueprint, "| Main theorem | DECOMPOSED | [Nodes](../roadmap/main/README.md |\n")

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "DECOMPOSED coverage evidence must link to at least one roadmap article"
    ]


def test_every_decomposition_link_must_resolve_not_merely_one(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "main/README.md")
    _contract(
        blueprint,
        "| Main theorem | DECOMPOSED | "
        "[Nodes](../roadmap/main/README.md) and [More](../roadmap/absent.md) |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "coverage link does not resolve to a file: '../roadmap/absent.md'"
    ]


def test_a_decomposition_link_fragment_must_name_a_real_heading(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _article(blueprint, "main/README.md")
    _contract(
        blueprint,
        "| Main theorem | DECOMPOSED | [Nodes](../roadmap/main/README.md#absent-section) |\n",
    )

    summary, issues = load_coverage(blueprint)

    assert summary is None
    assert [issue.reason for issue in issues] == [
        "coverage link fragment does not resolve: '../roadmap/main/README.md#absent-section'"
    ]

    _contract(
        blueprint,
        "| Main theorem | DECOMPOSED | [Nodes](../roadmap/main/README.md#roadmap-article) |\n",
    )
    summary, issues = load_coverage(blueprint)
    assert issues == ()
    assert summary is not None
    assert summary.complete
