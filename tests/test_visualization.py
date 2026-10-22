from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from autoform_cli import graph_pages
from autoform_cli.graph import load_graph
from autoform_cli.status import MEANINGS, STATES, derive
from autoform_cli.visualize import (
    GENERATED_GRAPH_MARKER,
    GENERATED_STRUCTURE_MARKER,
    export_graph,
    export_structure,
    main,
)


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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    "<span class=\"bp-legend-meaning\">Nothing it needs is blocked, so the statement can be written in Lean.</span>"
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
    (blueprint / "roadmap" / "results" / "result.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# Result\n\n## Proof depends on\n\n- [Tool](../tools/tool.md)\n",
        encoding="utf-8",
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
    (blueprint / "roadmap" / "results" / "result.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# Result\n\n## Proof depends on\n\n- [Tool](../tools/tool.md)\n",
        encoding="utf-8",
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
    assert f"| not ready | 1 | {MEANINGS['not_ready']} |" in vault


def test_the_legend_explains_readiness_without_naming_a_policy(tmp_path: Path) -> None:
    """Readiness depends on the project's policy, so the legend words it without naming one."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "stated.md", "Stated", declaration="theorem", statement="formalized")
    _write_node(blueprint / "roadmap" / "unstated.md", "Unstated", declaration="theorem")
    statuses = derive(load_graph(blueprint))

    legend = graph_pages.render_legend(statuses)

    assert [statuses[key].key for key in ("stated", "unstated")] == ["can_prove", "can_state"]
    assert "Statement is in Lean and nothing it needs is blocked, so the proof can start." in legend
    assert "Nothing it needs is blocked, so the statement can be written in Lean." in legend


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


def test_a_roadmap_that_declares_only_itself_has_no_articles(tmp_path: Path) -> None:
    """The project map leaves the roadmap's own article out, so the status table does too."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap", declaration="theorem")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert "The roadmap has no articles yet." in document
    assert "| Status |" not in document


def test_an_empty_roadmap_says_so(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert "The roadmap has no articles yet." in document
    assert "## Chapters" not in document and "## Articles" not in document


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
    from autoform_cli import visualize

    monkeypatch.setattr(visualize, "MAX_MAP_NODES", 1)
    blueprint = tmp_path / "blueprint"
    for name in ("one", "two"):
        _write_node(blueprint / "roadmap" / f"{name}.md", name.title(), declaration="theorem")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert "## Chapters" in document and "## Articles" not in document
    assert "more articles than the 1 this page lists" in document
    # A roadmap with exactly as many articles as the limit still lists them.
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
    from autoform_cli import visualize

    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = blueprint / "dependencies.md"
    output.write_text(_MERMAID_PAGE, encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        assert source.parent == destination.parent == blueprint.resolve()
        raise OSError("replace failed")

    monkeypatch.setattr(visualize, "_replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        export_graph(blueprint)

    assert output.read_text(encoding="utf-8") == _MERMAID_PAGE
    assert list(blueprint.glob(".dependencies.md.*.tmp")) == []



def test_atomic_write_failure_preserves_the_previous_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import visualize

    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = export_structure(blueprint)
    previous = output.read_text(encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        assert source.parent == destination.parent == blueprint.resolve()
        raise OSError("replace failed")

    monkeypatch.setattr(visualize, "_replace", fail_replace)

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
