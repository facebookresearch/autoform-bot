from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from autoform_cli.__main__ import main as autoform_main
from autoform_cli.graph import load_graph
from autoform_cli.status import MEANINGS, STATES, derive
from autoform_cli.ui import graph_pages
from autoform_cli.ui.visualize import (
    GENERATED_GRAPH_MARKER,
    GENERATED_STRUCTURE_MARKER,
    export_graph,
    export_structure,
    main,
)
from tests.facts_fixture import write_annotated


def _state(key: str):
    return next(state for state in STATES if state.key == key)


def _write_node(
    path: Path,
    title: str,
    dependencies: list[tuple[str, str]] | None = None,
    **metadata: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if "formalized" in (metadata.get("statement"), metadata.get("proof")):
        # The graph rejects formalized work without a lean: name.
        metadata.setdefault("lean", f"Project.{path.stem}")
    properties = [*(f"{key}: {value}" for key, value in metadata.items())]
    lines = ["---", *properties, "---", "", f"# {title}"]
    if dependencies:
        lines.extend(["", "## Depends on", ""])
        lines.extend(f"- [{label}]({target})" for label, target in dependencies)
    write_annotated(path, "\n".join(lines) + "\n")


#: What the Mermaid exporter wrote for a one-article roadmap, with its run of
#: classDef lines cut to the one the diagram used.
_MERMAID_PAGE = (
    "---\n"
    "kind: graph\n"
    "---\n"
    "\n"
    "# Dependency graph\n"
    "\n"
    "1 nodes · 0 dependencies. Arrows point from a prerequisite to what depends on it; a dashed\n"
    "arrow marks a prerequisite that only the proof needs. Select a node to open it.\n"
    "\n"
    "```mermaid\n"
    "graph LR\n"
    "  n0(\"Only\"):::can_state\n"
    "  click n0 \"roadmap/only.md\" \"Only — ready to state\"\n"
    "  classDef can_state fill:#FFFFFF,stroke:#0082FB,color:#0B4EA2,stroke-width:2px\n"
    "```\n"
    "\n"
    "## Legend\n"
    "\n"
    "<div class=\"bp-legend-grid\"><span class=\"bp-swatch bp-swatch-can_state\"></span>"
    "<span class=\"bp-legend-label\">ready to state</span><span class=\"bp-legend-count\">1</span>"
    "<span class=\"bp-legend-meaning\">Everything its statement uses is in Lean, so the statement can be written in Lean.</span>"
    "</div>\n"
)


def test_export_writes_a_plain_markdown_summary_linking_into_the_vault(tmp_path: Path) -> None:
    """GitHub, Obsidian and an agent read the vault's dependency page alike, so it is plain Markdown."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "foundations" / "README.md", "Foundations")
    _write_node(blueprint / "roadmap" / "foundations" / "base lemma.md", "Base lemma", declaration="lemma")
    _write_node(
        blueprint / "roadmap" / "main.md",
        "Main <result>",
        [("Base lemma", "foundations/base%20lemma.md#statement")],
        declaration="theorem",
    )

    output = export_graph(blueprint)
    document = output.read_text(encoding="utf-8")

    assert output == (blueprint / "dependencies.md").resolve()
    assert document.startswith(f"{GENERATED_GRAPH_MARKER}\n\n# Dependency summary\n")
    for retired in ("```mermaid", "graph LR", "classDef", "<span", "<div"):
        assert retired not in document
    # Vault links are percent-encoded, so the space in the file name reads %20.
    assert "- [Base lemma](roadmap/foundations/base%20lemma.md) · lemma · " in document
    assert "- [Main <result>](roadmap/main.md) · theorem · " in document
    rows = {line.split(" | ")[0]: line for line in document.splitlines() if line.startswith("| ") and "](" in line}
    assert rows["| [Foundations](roadmap/foundations/README.md)"].endswith("| none | Top level |")
    # Main waits on a lemma not yet stated, so it is planned.
    assert "| Top level | 1 | 1 planned | 0 | Foundations | none |" in document
    assert 'uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform dashboard . --site-dir site' in document


def test_the_summary_marks_dependencies_only_a_proof_needs(tmp_path: Path) -> None:
    """The map dashed a prerequisite only a proof needs; the chapter table names it so."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "tools" / "README.md", "Tools")
    _write_node(blueprint / "roadmap" / "tools" / "tool.md", "Tool", declaration="lemma")
    _write_node(blueprint / "roadmap" / "results" / "README.md", "Results")
    write_annotated(
        blueprint / "roadmap" / "results" / "result.md",
        "---\ndeclaration: theorem\n---\n\n# Result\n\n## Proof depends on\n\n- [Tool](../tools/tool.md)\n",
    )

    document = export_graph(blueprint).read_text(encoding="utf-8")

    rows = {line.split(" | ")[0]: line for line in document.splitlines() if line.startswith("| [")}
    assert rows["| [Results](roadmap/results/README.md)"].endswith("| Tools (proof only) | none |")
    assert rows["| [Tools](roadmap/tools/README.md)"].endswith("| none | Results (proof only) |")


def test_a_statement_edge_keeps_a_chapter_pair_from_being_proof_only(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "tools" / "README.md", "Tools | kit")
    _write_node(blueprint / "roadmap" / "tools" / "tool.md", "Tool", declaration="lemma")
    _write_node(blueprint / "roadmap" / "results" / "README.md", "Results")
    write_annotated(
        blueprint / "roadmap" / "results" / "result.md",
        "---\ndeclaration: theorem\n---\n\n# Result\n\n## Proof depends on\n\n- [Tool](../tools/tool.md)\n",
    )
    _write_node(
        blueprint / "roadmap" / "results" / "corollary.md",
        "Corollary",
        [("Tool", "../tools/tool.md")],
        declaration="theorem",
    )

    document = export_graph(blueprint).read_text(encoding="utf-8")

    table = document.split("## Chapters\n\n", 1)[1].split("\n\n", 1)[0]
    rows = table.splitlines()
    # A pipe in a title is escaped, so every row keeps the header's cells.
    widths = {len(re.split(r"(?<!\\)\|", row)) for row in rows}
    assert len(widths) == 1
    results = next(row for row in rows if row.startswith("| [Results]"))
    assert results.endswith("| Tools \\| kit | none |")
    assert next(row for row in rows if row.startswith("| [Tools \\| kit]")).endswith("| none | Results |")


def test_the_summary_counts_each_status_with_its_meaning(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "base.md", "Base", declaration="def", statement="formalized")
    _write_node(
        blueprint / "roadmap" / "top.md",
        "Top",
        [("Base", "base.md")],
        declaration="theorem",
        statement="formalized",
        proof="formalized",
    )
    _write_node(
        blueprint / "roadmap" / "next.md", "Next", [("Top", "top.md")], declaration="theorem", statement="formalized"
    )

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert f"| fully proved | 2 | {MEANINGS['fully_proved']} |" in document
    assert f"| ready to prove | 1 | {MEANINGS['can_prove']} |" in document
    # The ready column counts what a contributor could pick up today.
    assert "| Top level | 3 | 2 fully proved · 1 ready to prove | 1 | none | none |" in document


def test_green_stops_at_an_unproved_prerequisite(tmp_path: Path) -> None:
    """The distinction a flat status field cannot express."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "gap.md", "Gap", declaration="theorem")
    _write_node(
        blueprint / "roadmap" / "top.md",
        "Top",
        [("Gap", "gap.md")],
        declaration="theorem",
        statement="formalized",
        proof="formalized",
    )

    statuses = derive(load_graph(blueprint))

    assert statuses["top"].proved
    assert not statuses["top"].fully_proved
    assert statuses["top"].key == "proved"


def test_a_conditional_proof_has_its_own_status_row(tmp_path: Path) -> None:
    """A proof resting on a sorry must not count as fully proved."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap", open_statements="allowed")
    _write_node(blueprint / "roadmap" / "open.md", "Open", declaration="theorem", statement="formalized")
    _write_node(
        blueprint / "roadmap" / "top.md",
        "Top",
        [("Open", "open.md")],
        declaration="theorem",
        statement="formalized",
        proof="formalized",
    )

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert f"| {_state('conditional').label} | 1 | {MEANINGS['conditional']} |" in document
    assert "- [Top](roadmap/top.md) · theorem · conditionally proved" in document


def test_a_chapter_counts_the_deferred_work_it_holds(tmp_path: Path) -> None:
    """A leaf marked not ready that declares nothing yet holds its chapter open, so the chapter counts it."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "drafts" / "README.md", "Drafts")
    _write_node(blueprint / "roadmap" / "drafts" / "result.md", "Result", declaration="theorem")
    _write_node(blueprint / "roadmap" / "drafts" / "later.md", "Later", not_ready="true")

    vault = export_graph(blueprint).read_text(encoding="utf-8")

    assert "| [Drafts](roadmap/drafts/README.md) | 2 | 1 ready to state · 1 not ready | 1 | none | none |" in vault
    # The status table counts what `autoform check` counts, which leaves the leaf out, and the page says so.
    assert "| not ready |" not in vault
    assert (
        "The Chapters table's Items column also counts 1 article marked not ready with no declaration yet, so "
        "deferred work holds its chapter open; the status table, like `autoform check`, leaves such articles out."
    ) in vault


def test_deferred_work_alone_leaves_no_status_table_to_name(tmp_path: Path) -> None:
    """With only deferred work there is no status table, so the sentence names check alone."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "later.md", "Later", not_ready="true")

    vault = export_graph(blueprint).read_text(encoding="utf-8")

    assert "| Status | Count | Meaning |" not in vault and "the status table" not in vault
    assert "so deferred work holds its chapter open; `autoform check` leaves such articles out." in vault


def test_the_summary_counts_what_check_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The intro and the status table give check's numbers, so a reader can hold the page against its lines (#762).

    The summary called the chapters' items its articles, and its status table
    counted a not-ready leaf with no declaration, where check counts every
    roadmap article and leaves that leaf out of its states.
    """
    blueprint = tmp_path / "blueprint"
    ledger = blueprint / "sources" / "catalog.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("# Catalog declarations\n", encoding="utf-8")
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "drafts" / "README.md", "Drafts")
    _write_node(blueprint / "roadmap" / "drafts" / "result.md", "Result", declaration="theorem")
    _write_node(blueprint / "roadmap" / "drafts" / "later.md", "Later", not_ready="true")
    _write_node(blueprint / "roadmap" / "drafts" / "notation.md", "Notation")
    module = blueprint / "roadmap" / "drafts" / "module.md"
    _write_node(module, "Module", catalog="module")
    with module.open("a", encoding="utf-8") as stream:
        stream.write("\n## Sources\n\n- [Ledger](../../sources/catalog.md)\n")

    assert autoform_main(["check", str(blueprint)]) == 0
    check = capsys.readouterr().out.splitlines()
    document = export_graph(blueprint).read_text(encoding="utf-8")

    ok = next(index for index, line in enumerate(check) if line.startswith("OK: "))
    assert check[ok : ok + 3] == ["OK: 6 articles, 0 dependencies", "    1 ready to state", "    1 module inventory"]
    assert (
        "The roadmap has 6 articles: 1 formalization target, 1 module inventory, and 4 others (the roadmap's own "
        "article, chapters and sections, and articles that declare nothing). It has 1 chapter, with 0 "
        "dependencies between chapters."
    ) in document
    table = document.split("| Status | Count | Meaning |\n| --- | ---: | --- |\n", 1)[1].split("\n\n", 1)[0]
    assert [row.split(" | ")[:2] for row in table.splitlines()] == [
        ["| ready to state", "1"],
        ["| inventory pending", "1"],
    ]
    # The chapter still counts the deferred leaf, so the page says by how many the two differ.
    chapter = "| [Drafts](roadmap/drafts/README.md) | 3 | 1 ready to state · 1 not ready · 1 inventory pending |"
    assert chapter in document
    assert "The Chapters table's Items column also counts 1 article marked not ready" in document
    assert "The Items column totals 3; each item is listed here under its chapter." in document


def test_the_summary_says_a_mathlib_article_restates_mathlib(tmp_path: Path) -> None:
    """`mathlib: true` marks an article that restates an existing Mathlib declaration, not an upstreamed one (#762)."""
    blueprint = tmp_path / "blueprint"
    _write_node(
        blueprint / "roadmap" / "primes.md",
        "Infinitely many primes",
        declaration="theorem",
        mathlib="true",
        mathlib_declaration="Nat.exists_infinite_primes",
    )

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert "| in mathlib | 1 | Restates an existing Mathlib declaration. |" in document
    assert "upstream" not in document.casefold()


@pytest.mark.parametrize("policy", ("forbidden", "allowed"))
def test_every_planned_article_waits_for_a_prerequisite(tmp_path: Path, policy: str) -> None:
    """A planned article's line says what it waits for, so derive never leaves one waiting on nothing (#762).

    A theorem waits for an unstated statement prerequisite under either
    policy, and under the strict one for an unproved proof prerequisite too; a
    definition, whose body is its proof, waits for an unstated one under both.
    """
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    _write_node(roadmap / "README.md", "Roadmap", open_statements=policy)
    _write_node(roadmap / "gap.md", "Gap", declaration="theorem")
    _write_node(roadmap / "open.md", "Open", declaration="theorem", statement="formalized")
    _write_node(roadmap / "uses.md", "Uses", [("Gap", "gap.md")], declaration="theorem")
    write_annotated(
        roadmap / "proof.md",
        "---\ndeclaration: theorem\n---\n\n# Proof\n\n## Proof depends on\n\n- [Open](open.md)\n",
    )
    write_annotated(
        roadmap / "body.md",
        "---\ndeclaration: def\n---\n\n# Body\n\n## Proof depends on\n\n- [Gap](gap.md)\n",
    )

    statuses = derive(load_graph(blueprint))

    planned = sorted(node_id for node_id, node_status in statuses.items() if node_status.key == "planned")
    assert planned == (["body", "proof", "uses"] if policy == "forbidden" else ["body", "uses"])
    assert {node_id: statuses[node_id].waiting_on for node_id in planned} == {
        "body": ("gap",),
        "uses": ("gap",),
        **({"proof": ("open",)} if policy == "forbidden" else {}),
    }


def test_a_listed_article_names_what_it_waits_for(tmp_path: Path) -> None:
    """Each listed article that waits names its prerequisites, in authored order, five and then a count (#762).

    The summary gave a waiting article only its status, so an article marked
    not ready read as missing blueprint work even while it waited on proofs.
    """
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    _write_node(roadmap / "README.md", "Roadmap")
    prerequisites = [(f"P{number}", f"p{number}.md") for number in range(1, 8)]
    for title, target in prerequisites:
        _write_node(roadmap / target, title, declaration="theorem")
    _write_node(roadmap / "big.md", "Big", prerequisites[::-1], declaration="theorem")
    _write_node(roadmap / "one.md", "One", prerequisites[:1], declaration="theorem")
    _write_node(roadmap / "held.md", "Held", prerequisites[:1], declaration="theorem", not_ready="true")
    _write_node(roadmap / "later.md", "Later", not_ready="true")

    lines = export_graph(blueprint).read_text(encoding="utf-8").splitlines()

    first_five = ", ".join(f"[{title}](roadmap/{target})" for title, target in prerequisites[::-1][:5])
    assert f"- [Big](roadmap/big.md) · theorem · planned · waits for {first_five}, and 2 more" in lines
    assert "- [One](roadmap/one.md) · theorem · planned · waits for [P1](roadmap/p1.md)" in lines
    assert "- [Held](roadmap/held.md) · theorem · not ready · waits for [P1](roadmap/p1.md)" in lines
    # Nothing to wait for: a ready article, and one marked not ready whose prerequisites are done.
    assert "- [P1](roadmap/p1.md) · theorem · ready to state" in lines
    assert "- [Later](roadmap/later.md) · article · not ready" in lines
    # The meaning no longer claims the blueprint lacks work; the lines say what each article waits for.
    assert "| not ready | 1 | Marked not ready to attempt yet. |" in lines


def test_the_legend_explains_readiness_without_naming_a_policy(tmp_path: Path) -> None:
    """Readiness depends on the project's policy, so the legend words it without naming one."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "stated.md", "Stated", declaration="theorem", statement="formalized")
    _write_node(blueprint / "roadmap" / "unstated.md", "Unstated", declaration="theorem")
    statuses = derive(load_graph(blueprint))

    legend = graph_pages.render_legend(statuses)

    assert [statuses[key].key for key in ("stated", "unstated")] == ["can_prove", "can_state"]
    assert "Statement is in Lean and nothing it needs is blocked, so the proof can start." in legend
    assert "Everything its statement uses is in Lean, so the statement can be written in Lean." in legend


def test_the_summary_counts_targets_not_the_chapters_that_hold_them(tmp_path: Path) -> None:
    """The roadmap and the chapter hold the theorem, so only the theorem is counted and listed."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "chapter" / "README.md", "Chapter")
    _write_node(blueprint / "roadmap" / "chapter" / "result.md", "Result", declaration="theorem")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert f"| ready to state | 1 | {MEANINGS['can_state']} |" in document
    assert "- [Result](roadmap/chapter/result.md) · theorem · ready to state" in document
    assert "- [Chapter]" not in document and "- [Roadmap]" not in document


def test_the_summary_counts_what_the_explorer_counts(tmp_path: Path) -> None:
    """The explorer's project map counts targets, not prose, and the summary is built from it.

    The Mermaid map drew a prose leaf in its derived state and its legend
    counted it; the structure page is where a prose leaf is listed now.
    """
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "base.md", "Base", declaration="def", statement="formalized")
    _write_node(blueprint / "roadmap" / "notation.md", "Notation")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert f"| fully proved | 1 | {MEANINGS['fully_proved']} |" in document
    assert "Notation" not in document
    assert "[Notation](roadmap/notation.md) · article" in export_structure(blueprint).read_text(encoding="utf-8")


def test_a_link_label_keeps_a_half_open_interval_and_its_math(tmp_path: Path) -> None:
    """An unbalanced bracket in a title would end the link early; math keeps its brackets."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "interval.md", "Interval [a, b)", declaration="def")
    _write_node(blueprint / "roadmap" / "cover.md", "Covers of $[0, 1)$", declaration="theorem")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert "- [Interval \\[a, b)](roadmap/interval.md) · def · " in document
    assert "- [Covers of $[0, 1)$](roadmap/cover.md) · theorem · " in document


def test_a_roadmap_that_declares_only_itself_is_counted_as_check_counts_it(tmp_path: Path) -> None:
    """check counts the roadmap's own article, as a target once it declares something, so the status table does too.

    The project map leaves that article out, so it makes no chapter (#762).
    """
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap", declaration="theorem")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert "The roadmap has only its own article so far." in document
    assert f"| ready to state | 1 | {MEANINGS['can_state']} |" in document
    assert "## Chapters" not in document


def test_an_empty_roadmap_says_so(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert "The roadmap has only its own article so far." in document
    assert "## Chapters" not in document and "## Articles" not in document and "| Status |" not in document


def test_a_flat_roadmap_keeps_its_article_detail(tmp_path: Path) -> None:
    """Every article directly under roadmap/ makes one chapter, so the article list carries the detail."""
    blueprint = tmp_path / "blueprint"
    for name in ("one", "two", "three"):
        _write_node(blueprint / "roadmap" / f"{name}.md", name.title(), declaration="theorem")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert "| Top level | 3 | 3 ready to state | 3 | none | none |" in document
    assert document.count("\n- [") == 3


def test_a_roadmap_too_large_to_list_keeps_its_chapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli.ui import visualize

    monkeypatch.setattr(visualize, "MAX_MAP_NODES", 1)
    blueprint = tmp_path / "blueprint"
    for name in ("one", "two"):
        _write_node(blueprint / "roadmap" / f"{name}.md", name.title(), declaration="theorem")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert "## Chapters" in document and "## Articles" not in document
    assert (
        "The Items column totals 2; this page lists items only when there are at most 1, so it lists none, "
        "and the explorer shows every one."
    ) in document
    # A roadmap with exactly as many items as the limit still lists them.
    monkeypatch.setattr(visualize, "MAX_MAP_NODES", 2)
    assert "## Articles" in export_graph(blueprint).read_text(encoding="utf-8")


def test_the_summary_replaces_the_page_the_mermaid_exporter_wrote(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only", declaration="theorem")
    output = blueprint / "dependencies.md"
    output.write_text(_MERMAID_PAGE, encoding="utf-8")

    export_graph(blueprint)

    assert output.read_text(encoding="utf-8").startswith(f"{GENERATED_GRAPH_MARKER}\n")


def test_a_generated_summary_can_be_refreshed(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "first.md", "First", declaration="theorem")
    output = export_graph(blueprint)
    _write_node(blueprint / "roadmap" / "second.md", "Second", declaration="theorem")

    assert export_graph(blueprint) == output

    assert "- [Second](roadmap/second.md)" in output.read_text(encoding="utf-8")


def test_the_cli_refuses_a_hand_written_dependency_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exporter used to overwrite a hand-written dependencies.md without a word."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = blueprint / "dependencies.md"
    authored = "---\nkind: graph\n---\n\n# Our dependencies\n\nWritten by hand.\n"
    output.write_text(authored, encoding="utf-8")

    with pytest.raises(SystemExit, match="2"):
        main([str(blueprint)])

    assert output.read_text(encoding="utf-8") == authored
    assert "refusing to overwrite authored dependency page" in capsys.readouterr().err


@pytest.mark.parametrize(
    "authored",
    (
        # A fence under the old front matter is not enough without the exporter's sentence.
        "---\nkind: graph\n---\n\n# Ours\n\n```mermaid\ngraph LR\n  a --> b\n```\n",
        # Nor is the sentence without the fence.
        "---\nkind: graph\n---\n\n# Ours\n\n1 nodes · 0 dependencies. Arrows point from a prerequisite to "
        "what depends on it; a dashed\narrow marks a prerequisite that only the proof needs. Select a node to open it.\n",
        f"# Ours\n\n{GENERATED_GRAPH_MARKER}\n",
        f"# Ours\n\n{_MERMAID_PAGE}",
    ),
    ids=("fence-only", "sentence-only", "marker-inside", "old-page-inside"),
)
def test_only_a_generated_page_counts_as_generated(tmp_path: Path, authored: str) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = blueprint / "dependencies.md"
    output.write_text(authored, encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        export_graph(blueprint)

    assert output.read_text(encoding="utf-8") == authored


def test_a_dangling_link_at_the_dependency_page_is_refused(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = blueprint / "dependencies.md"
    output.symlink_to(tmp_path / "missing.md")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        export_graph(blueprint)

    assert output.is_symlink() and not output.exists()


def test_cli_refuses_an_authored_graph_before_replacing_structure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    graph = blueprint / "dependencies.md"
    graph.write_text("# Our dependencies\n", encoding="utf-8")
    structure = blueprint / "structure.md"
    generated = f"{GENERATED_STRUCTURE_MARKER}\n\n# Vault structure\n"
    structure.write_text(generated, encoding="utf-8")

    with pytest.raises(SystemExit, match="2"):
        main([str(blueprint), "--structure"])

    assert graph.read_text(encoding="utf-8") == "# Our dependencies\n"
    assert structure.read_text(encoding="utf-8") == generated
    assert "refusing to overwrite authored dependency page" in capsys.readouterr().err


def test_a_non_utf8_dependency_page_is_refused(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = blueprint / "dependencies.md"
    output.write_bytes(b"\xff\xfeauthored")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        export_graph(blueprint)

    assert output.read_bytes() == b"\xff\xfeauthored"


def test_cli_writes_only_the_graph_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only node", declaration="theorem")

    assert main([str(blueprint)]) == 0

    output = (blueprint / "dependencies.md").resolve()
    assert output.is_file()
    assert not (blueprint / "structure.md").exists()
    assert capsys.readouterr().out == f"{output}\n"
    assert "[Only node](roadmap/only.md)" in output.read_text(encoding="utf-8")


def test_exports_and_the_cli_work_through_a_link_that_cannot_be_read(
    tmp_path: Path, unreadable_link: Callable[[Path, Path | str], None]
) -> None:
    blueprint = tmp_path / "real" / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only node")
    unreadable_link(tmp_path / "via", tmp_path / "real")
    through = tmp_path / "via" / "blueprint"

    export_graph(through)
    export_structure(through)

    assert (blueprint / "dependencies.md").is_file() and (blueprint / "structure.md").is_file()
    assert main([str(through), "--structure"]) == 0


def test_default_cli_leaves_authored_structure_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only node")
    structure = blueprint / "structure.md"
    structure.write_text("# Authored structure\n", encoding="utf-8")

    assert main([str(blueprint)]) == 0

    graph = (blueprint / "dependencies.md").resolve()
    assert structure.read_text(encoding="utf-8") == "# Authored structure\n"
    assert capsys.readouterr().out == f"{graph}\n"



def test_cli_writes_an_explicit_graph_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only node", declaration="theorem")
    output = tmp_path / "docs" / "dependencies.md"

    assert main([str(blueprint), "-o", str(output)]) == 0

    assert output.is_file()
    assert capsys.readouterr().out == f"{output.resolve()}\n"
    assert "[Only node](../blueprint/roadmap/only.md)" in output.read_text(encoding="utf-8")


def test_cli_writes_structure_only_when_requested(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only node")

    assert main([str(blueprint), "--structure"]) == 0

    graph = (blueprint / "dependencies.md").resolve()
    structure = (blueprint / "structure.md").resolve()
    assert graph.is_file()
    assert structure.is_file()
    assert structure.read_text(encoding="utf-8").startswith(GENERATED_STRUCTURE_MARKER)
    assert capsys.readouterr().out == f"{graph}\n"


def test_cli_reports_invalid_blueprint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="2"):
        main([str(tmp_path / "missing")])

    assert "blueprint directory does not exist" in capsys.readouterr().err


def test_cli_refuses_authored_structure_before_replacing_graph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only node")
    graph = blueprint / "dependencies.md"
    graph.write_text(_MERMAID_PAGE, encoding="utf-8")
    structure = blueprint / "structure.md"
    structure.write_text("# Authored structure\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="2"):
        main([str(blueprint), "--structure"])

    assert graph.read_text(encoding="utf-8") == _MERMAID_PAGE
    assert structure.read_text(encoding="utf-8") == "# Authored structure\n"
    assert "refusing to overwrite" in capsys.readouterr().err



def test_cli_refuses_to_alias_graph_and_structure_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only node")
    structure = blueprint / "structure.md"

    with pytest.raises(SystemExit, match="2"):
        main([str(blueprint), "--output", str(structure), "--structure"])

    assert not structure.exists()
    assert "must be different paths" in capsys.readouterr().err



def test_the_vault_gets_a_structure_page_obsidian_can_read(tmp_path: Path) -> None:
    """Obsidian shows the tree but cannot derive a node's state from the file.

    Plain Markdown only: the site stylesheet does not exist in a vault, so the
    HTML grid the published page uses would render as a wall of tags here.
    """
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "part" / "README.md", "Part")
    _write_node(
        blueprint / "roadmap" / "part" / "base.md",
        "Base",
        declaration="def",
        statement="formalized",
    )

    output = export_structure(blueprint)

    assert output == blueprint / "structure.md"
    page = output.read_text(encoding="utf-8")
    assert page.startswith(GENERATED_STRUCTURE_MARKER)
    assert "- **roadmap/**" in page
    assert "    - **part/**" in page
    assert "[Base](roadmap/part/base.md) \u00b7 def \u00b7 fully proved" in page
    assert "<" not in page.split("---", 2)[2]
    # It never lists itself, and never the other generated view.
    assert "structure.md)" not in page
    assert "dependencies.md)" not in page


def test_vault_views_label_checked_catalogs_as_module_inventories(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    ledger = blueprint / "sources/catalog.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("# Catalog declarations\n", encoding="utf-8")
    catalog = blueprint / "roadmap/catalog.md"
    _write_node(
        catalog,
        "Existing module",
        catalog="module",
        lean="Existing.module",
        statement="formalized",
        proof="formalized",
    )
    with catalog.open("a", encoding="utf-8") as stream:
        stream.write("\n## Sources\n\n- [Ledger](../sources/catalog.md)\n")

    graph_page = export_graph(blueprint).read_text(encoding="utf-8")
    structure_page = export_structure(blueprint).read_text(encoding="utf-8")

    assert "- [Existing module](roadmap/catalog.md) · module inventory · inventory checked" in graph_page
    assert f"| inventory checked | 1 | {MEANINGS['inventory_checked']} |" in graph_page
    assert "fully proved" not in graph_page
    assert (
        "[Existing module](roadmap/catalog.md) · module inventory · inventory checked"
        in structure_page
    )


def test_vault_pages_count_every_catalog_as_an_inventory(tmp_path: Path) -> None:
    """A chapter counts a catalog that is not yet checked, as a pending inventory.

    The vault map once counted only checked catalogs, so a chapter of
    unchecked ones read as empty. The status table lists the inventory
    statuses the chapters count, and the summary and the structure page name
    an unchecked catalog by that status, not as ready to state.
    """
    blueprint = tmp_path / "blueprint"
    ledger = blueprint / "sources/catalog.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("# Catalog declarations\n", encoding="utf-8")
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "modules" / "README.md", "Modules")
    checked = blueprint / "roadmap/modules/checked.md"
    pending = blueprint / "roadmap/modules/pending.md"
    _write_node(checked, "Checked module", catalog="module", statement="formalized", proof="formalized")
    _write_node(pending, "Pending module", catalog="module")
    for catalog in (checked, pending):
        with catalog.open("a", encoding="utf-8") as stream:
            stream.write("\n## Sources\n\n- [Ledger](../../sources/catalog.md)\n")

    vault = export_graph(blueprint).read_text(encoding="utf-8")

    assert "| [Modules](roadmap/modules/README.md) | 2 | 1 inventory checked · 1 inventory pending | 0 |" in vault
    for key, label in (("inventory_checked", "inventory checked"), ("inventory_pending", "inventory pending")):
        assert f"| {label} | 1 | {MEANINGS[key]} |" in vault
    assert "ready to state" not in vault
    assert "- [Pending module](roadmap/modules/pending.md) · module inventory · inventory pending" in vault
    structure = export_structure(blueprint).read_text(encoding="utf-8")
    assert "[Pending module](roadmap/modules/pending.md) · module inventory · inventory pending" in structure


def test_the_vault_structure_page_leaves_implementation_notes_and_readback_cards_out(tmp_path: Path) -> None:
    """The page is the shape of the book, and notes and cards are not part of the book."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    article_id = "af_0123456789abcdef01234567"
    _write_node(
        blueprint / "roadmap" / "base.md",
        "Base",
        declaration="def",
        article_id=article_id,
    )
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{article_id}.md").write_text("Mathlib gap.\n", encoding="utf-8")
    cards = blueprint / "readbacks" / article_id
    cards.mkdir(parents=True)
    (cards / f"Base--{'0' * 64}.md").write_text("A card.\n", encoding="utf-8")

    page = export_structure(blueprint).read_text(encoding="utf-8")

    assert "[Base](roadmap/base.md)" in page
    assert ".implementation-notes" not in page and "readbacks" not in page


def test_generated_structure_can_be_refreshed(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "first.md", "First")
    output = export_structure(blueprint)
    _write_node(blueprint / "roadmap" / "second.md", "Second")

    assert export_structure(blueprint) == output

    page = output.read_text(encoding="utf-8")
    assert "[First](roadmap/first.md)" in page
    assert "[Second](roadmap/second.md)" in page



def test_export_structure_refuses_an_unmarked_existing_file(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = blueprint / "structure.md"
    output.write_text("# Authored structure\n", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        export_structure(blueprint)

    assert output.read_text(encoding="utf-8") == "# Authored structure\n"



def test_non_utf8_structure_is_refused_without_replacement(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = blueprint / "structure.md"
    authored = b"\xff\xfeauthored"
    output.write_bytes(authored)

    with pytest.raises(ValueError, match="refusing to overwrite"):
        export_structure(blueprint)

    assert output.read_bytes() == authored



def test_marker_inside_authored_content_does_not_claim_ownership(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = blueprint / "structure.md"
    authored = f"# Authored structure\n\n{GENERATED_STRUCTURE_MARKER}\n"
    output.write_text(authored, encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        export_structure(blueprint)

    assert output.read_text(encoding="utf-8") == authored



def test_atomic_write_failure_preserves_the_previous_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = blueprint / "dependencies.md"
    output.write_text(_MERMAID_PAGE, encoding="utf-8")

    def fail_replace(source: str, destination: Path, **_: object) -> None:
        assert Path(source).parent == destination.parent == blueprint.resolve()
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        export_graph(blueprint)

    assert output.read_text(encoding="utf-8") == _MERMAID_PAGE
    assert list(blueprint.glob(".dependencies.md.*.tmp")) == []



def test_atomic_write_failure_preserves_the_previous_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = export_structure(blueprint)
    previous = output.read_text(encoding="utf-8")

    def fail_replace(source: str, destination: Path, **_: object) -> None:
        assert Path(source).parent == destination.parent == blueprint.resolve()
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        export_structure(blueprint)

    assert output.read_text(encoding="utf-8") == previous
    assert list(blueprint.glob(".structure.md.*.tmp")) == []



def test_the_vault_structure_page_warns_about_a_flat_roadmap(tmp_path: Path) -> None:
    """The one fault the file explorer cannot show, as an Obsidian callout."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    for name in ("a", "b", "c", "d"):
        _write_node(blueprint / "roadmap" / f"{name}.md", f"Result {name}", declaration="theorem")

    page = export_structure(blueprint).read_text(encoding="utf-8")

    assert "> [!warning] Every article sits directly under `roadmap/`." in page
