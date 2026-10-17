from __future__ import annotations

import re
from pathlib import Path

import pytest

from autoform_cli import mermaid
from autoform_cli.graph import load_graph
from autoform_cli.status import STATES, derive
from autoform_cli.visualize import GENERATED_STRUCTURE_MARKER, export_graph, export_structure, main


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


def test_export_writes_a_mermaid_page_linking_to_markdown(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "foundations" / "README.md", "Foundations")
    _write_node(blueprint / "roadmap" / "foundations" / "base lemma.md", "Base lemma")
    _write_node(
        blueprint / "roadmap" / "main.md",
        "Main <result>",
        [("Base lemma", "foundations/base%20lemma.md#statement")],
    )

    output = export_graph(blueprint)
    document = output.read_text(encoding="utf-8")

    assert output == (blueprint / "dependencies.md").resolve()
    assert "```mermaid" in document
    assert "graph LR" in document
    # Handles are assigned in sorted order, so pin the links and the edge by
    # their targets rather than by whichever index a node happens to get.
    handles = dict(re.findall(r'click (n\d+) "([^"]+)"', document))
    # Vault links are percent-encoded, so the space in the file name reads %20.
    lemma = next(k for k, v in handles.items() if v == "roadmap/foundations/base%20lemma.md")
    main = next(k for k, v in handles.items() if v == "roadmap/main.md")
    assert f"  {lemma} --> {main}" in document
    assert "Main <result>" in document


def test_diagram_colours_and_shapes_follow_derived_status(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(
        blueprint / "roadmap" / "base.md",
        "Base",
        declaration="def",
        statement="formalized",
    )
    _write_node(
        blueprint / "roadmap" / "top.md",
        "Top",
        [("Base", "base.md")],
        declaration="theorem",
        statement="formalized",
        proof="formalized",
    )

    document = export_graph(blueprint).read_text(encoding="utf-8")

    # Definitions are rectangles, propositions are rounded.
    assert 'n0["Base"]:::fully_proved' in document
    assert 'n1("Top"):::fully_proved' in document
    # The vault copy carries its palette inline; Obsidian has no init script.
    assert f"classDef fully_proved fill:{_state('fully_proved').fill}" in document
    assert '<span class="bp-swatch bp-swatch-fully_proved">' in document
    assert '<span class="bp-legend-count">2</span>' in document


def test_the_published_graph_defers_its_palette_to_the_theme(tmp_path: Path) -> None:
    """Mermaid scopes its styles to the SVG id, so dark mode must re-render."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only", declaration="theorem")
    graph = load_graph(blueprint)
    output = tmp_path / "dependencies.md"

    published = mermaid.render_page(
        graph, derive(graph), output, links={"only": "x.html#only"}, include_classdefs=False
    )

    assert ":::can_state" in published
    assert "classDef" not in published


def test_proof_only_dependencies_are_dashed(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "tool.md", "Tool")
    (blueprint / "roadmap" / "result.md").write_text(
        "---\n---\n\n# Result\n\n## Proof depends on\n\n- [Tool](tool.md)\n",
        encoding="utf-8",
    )

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert "  n1 -.-> n0" in document
    assert "  n1 --> n0" not in document


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


def test_a_conditional_proof_has_its_own_colour_and_legend_entry(tmp_path: Path) -> None:
    """A proof resting on a sorry must not share the fully proved green."""
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

    assert '("Top"):::conditional' in document
    assert f"classDef conditional fill:{_state('conditional').fill}" in document
    assert '<span class="bp-swatch bp-swatch-conditional">' in document
    assert "Proof compiles, but rests on an open statement without a recorded Lean proof." in document


def test_vault_legends_count_the_deferred_work_their_scope_boxes_count(tmp_path: Path) -> None:
    """A leaf marked not ready that declares nothing yet holds its scope open.

    The scope box counts it, so the legend under the map counts it too.
    """
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "drafts" / "README.md", "Drafts")
    _write_node(blueprint / "roadmap" / "drafts" / "result.md", "Result", declaration="theorem")
    _write_node(blueprint / "roadmap" / "drafts" / "later.md", "Later", not_ready="true")

    vault = export_graph(blueprint).read_text(encoding="utf-8")

    assert '["Drafts<br/><small>2 items · 1 ready to state · 1 not ready</small>"]:::scope' in vault
    assert (
        '<span class="bp-swatch bp-swatch-not_ready"></span>'
        '<span class="bp-legend-label">not ready</span>'
        '<span class="bp-legend-count">1</span>'
    ) in vault.split("## Legend", 1)[1]


def test_the_legend_explains_readiness_without_naming_a_policy(tmp_path: Path) -> None:
    """Readiness depends on the project's policy, so the legend words it without naming one."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "stated.md", "Stated", declaration="theorem", statement="formalized")
    _write_node(blueprint / "roadmap" / "unstated.md", "Unstated", declaration="theorem")
    statuses = derive(load_graph(blueprint))

    legend = mermaid.render_legend(statuses)

    assert [statuses[key].key for key in ("stated", "unstated")] == ["can_prove", "can_state"]
    assert "Statement is in Lean and nothing it needs is blocked, so the proof can start." in legend
    assert "Nothing it needs is blocked, so the statement can be written in Lean." in legend


def test_the_exported_legend_leaves_out_containers(tmp_path: Path) -> None:
    """The roadmap and the chapter are drawn as scope boxes, so only the theorem has a legend row."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "chapter" / "README.md", "Chapter")
    _write_node(blueprint / "roadmap" / "chapter" / "result.md", "Result", declaration="theorem")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert document.count('class="bp-legend-count"') == 1
    assert '<span class="bp-legend-count">1</span>' in document


def test_the_exported_legend_counts_a_prose_leaf_the_map_colours(tmp_path: Path) -> None:
    """The map draws a prose leaf in its derived state, so the legend has its row."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "base.md", "Base", declaration="def", statement="formalized")
    _write_node(blueprint / "roadmap" / "notation.md", "Notation")

    document = export_graph(blueprint).read_text(encoding="utf-8")

    assert '("Notation"):::can_state' in document
    assert (
        '<span class="bp-legend-label">ready to state</span>'
        '<span class="bp-legend-count">1</span>'
    ) in document.split("## Legend", 1)[1]


def test_cli_writes_only_the_graph_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only node")

    assert main([str(blueprint)]) == 0

    output = (blueprint / "dependencies.md").resolve()
    assert output.is_file()
    assert not (blueprint / "structure.md").exists()
    assert capsys.readouterr().out == f"{output}\n"
    assert 'click n0 "roadmap/only.md"' in output.read_text(encoding="utf-8")


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
    _write_node(blueprint / "roadmap" / "only.md", "Only node")
    output = tmp_path / "docs" / "dependencies.md"

    assert main([str(blueprint), "-o", str(output)]) == 0

    assert output.is_file()
    assert capsys.readouterr().out == f"{output.resolve()}\n"
    assert 'click n0 "../blueprint/roadmap/only.md"' in output.read_text(encoding="utf-8")


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



def test_cli_accepts_html_link_extension(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only node")
    output = blueprint / "dependencies.md"

    assert main([str(blueprint), "--output", str(output), "--link-extension", ".html"]) == 0

    assert str(output.resolve()) in capsys.readouterr().out
    assert 'click n0 "roadmap/only.html"' in output.read_text(encoding="utf-8")


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
    graph.write_text("old graph\n", encoding="utf-8")
    structure = blueprint / "structure.md"
    structure.write_text("# Authored structure\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="2"):
        main([str(blueprint), "--structure"])

    assert graph.read_text(encoding="utf-8") == "old graph\n"
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

    assert '("Existing module"):::planned' in graph_page
    assert '"Existing module — inventory checked"' in graph_page
    assert "fully proved" not in graph_page
    assert (
        "[Existing module](roadmap/catalog.md) · module inventory · inventory checked"
        in structure_page
    )


def test_vault_maps_count_every_catalog_as_an_inventory(tmp_path: Path) -> None:
    """A scope box counts a catalog that is not yet checked, as a pending inventory.

    It used to count only checked catalogs, so a scope of unchecked ones read
    as empty on the vault map. The legend under the map lists the inventory
    statuses the boxes count, and the map and the structure page name an
    unchecked catalog by that status, not as ready to state.
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

    assert '["Modules<br/><small>2 items · 1 inventory checked · 1 inventory pending</small>"]:::scope' in vault
    legend = vault.split("## Legend", 1)[1]
    for key, label in (("inventory_checked", "inventory checked"), ("inventory_pending", "inventory pending")):
        assert (
            f'<span class="bp-swatch bp-swatch-{key}"></span>'
            f'<span class="bp-legend-label">{label}</span>'
            '<span class="bp-legend-count">1</span>'
        ) in legend
    assert ":::can_state" not in vault
    assert '("Pending module"):::planned' in vault
    assert '"Pending module — inventory pending"' in vault
    structure = export_structure(blueprint).read_text(encoding="utf-8")
    assert "[Pending module](roadmap/modules/pending.md) · module inventory · inventory pending" in structure


def test_the_vault_structure_page_leaves_implementation_notes_out(tmp_path: Path) -> None:
    """The page is the shape of the book, and notes are not part of the book."""
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

    page = export_structure(blueprint).read_text(encoding="utf-8")

    assert "[Base](roadmap/base.md)" in page
    assert ".implementation-notes" not in page


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
    output.write_text("old graph\n", encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        assert source.parent == destination.parent == blueprint.resolve()
        raise OSError("replace failed")

    monkeypatch.setattr(visualize, "_replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        export_graph(blueprint)

    assert output.read_text(encoding="utf-8") == "old graph\n"
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
