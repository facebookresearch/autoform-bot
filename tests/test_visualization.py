from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from autoform_cli import mermaid
from autoform_cli.graph import load_graph
from autoform_cli.status import STATES, derive
from autoform_cli.visualize import (
    GENERATED_STRUCTURE_MARKER,
    VisualizationError,
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
    lemma = next(k for k, v in handles.items() if v == "roadmap/foundations/base lemma.md")
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


def test_nested_pages_named_like_generated_outputs_remain_in_the_graph(
    tmp_path: Path,
) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    _write_node(blueprint / "roadmap" / "dependencies.md", "Authored dependencies")
    _write_node(blueprint / "roadmap" / "structure.md", "Authored structure")

    assert main([str(blueprint), "--structure"]) == 0

    graph_page = (blueprint / "dependencies.md").read_text(encoding="utf-8")
    structure_page = (blueprint / "structure.md").read_text(encoding="utf-8")
    assert "Authored dependencies" in graph_page
    assert "Authored structure" in graph_page
    assert "roadmap/dependencies.md" in structure_page
    assert "roadmap/structure.md" in structure_page


def test_nested_custom_graph_output_is_excluded_by_its_exact_path(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = blueprint / "roadmap" / "generated" / "graph.md"

    assert export_graph(blueprint, output) == output
    assert export_graph(blueprint, output) == output

    page = output.read_text(encoding="utf-8")
    assert "Only" in page
    assert "generated/graph" not in page



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


def test_generated_structure_can_be_refreshed(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "first.md", "First")
    output = export_structure(blueprint)
    _write_node(blueprint / "roadmap" / "second.md", "Second")

    assert export_structure(blueprint) == output

    page = output.read_text(encoding="utf-8")
    assert "[First](roadmap/first.md)" in page
    assert "[Second](roadmap/second.md)" in page


def test_custom_structure_omits_default_generated_views(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    assert main([str(blueprint), "--structure"]) == 0
    output = tmp_path / "published" / "structure.md"

    export_structure(blueprint, output)

    page = output.read_text(encoding="utf-8")
    assert "dependencies.md" not in page
    assert "structure.md" not in page



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

    def fail_replace(source: str, destination: str, directory_descriptor: int) -> None:
        assert source.startswith(".dependencies.md.")
        assert destination == "dependencies.md"
        opened = os.fstat(directory_descriptor)
        expected = blueprint.stat()
        assert (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino)
        raise OSError("replace failed")

    monkeypatch.setattr(visualize, "_replace_at", fail_replace)

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

    def fail_replace(source: str, destination: str, directory_descriptor: int) -> None:
        assert source.startswith(".structure.md.")
        assert destination == "structure.md"
        opened = os.fstat(directory_descriptor)
        expected = blueprint.stat()
        assert (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino)
        raise OSError("replace failed")

    monkeypatch.setattr(visualize, "_replace_at", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        export_structure(blueprint)

    assert output.read_text(encoding="utf-8") == previous
    assert list(blueprint.glob(".structure.md.*.tmp")) == []


def test_external_graph_output_rejects_parent_replacement_during_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import visualize

    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output_parent = tmp_path / "output"
    output_parent.mkdir()
    replacement = tmp_path / "replacement-output"
    replacement.mkdir()
    retained = tmp_path / "retained-output"
    output = output_parent / "graph.md"
    original_replace = visualize._replace_at

    def replace_after_parent_swap(
        source: str,
        destination: str,
        directory_descriptor: int,
    ) -> bool:
        output_parent.rename(retained)
        replacement.rename(output_parent)
        (output_parent / source).write_text("planted\n", encoding="utf-8")
        return original_replace(source, destination, directory_descriptor)

    monkeypatch.setattr(visualize, "_replace_at", replace_after_parent_swap)

    with pytest.raises(VisualizationError, match="output directory changed"):
        export_graph(blueprint, output)

    assert not output.exists()
    assert (output_parent / next(output_parent.iterdir()).name).read_text(
        encoding="utf-8"
    ) == "planted\n"


def test_structure_refuses_authored_replacement_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import visualize

    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    structure = export_structure(blueprint)
    original_prepare = visualize._prepare_structure_page

    def replace_after_preflight(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        structure.write_text("# Authored replacement\n", encoding="utf-8")
        return result

    monkeypatch.setattr(visualize, "_prepare_structure_page", replace_after_preflight)

    with pytest.raises(VisualizationError, match="changed after ownership preflight"):
        export_structure(blueprint)

    assert structure.read_text(encoding="utf-8") == "# Authored replacement\n"


def test_structure_restores_a_concurrent_edit_displaced_at_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import visualize

    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    structure = export_structure(blueprint)
    concurrent = f"{GENERATED_STRUCTURE_MARKER}\n\n# Concurrent edit\n"
    original = visualize._replace_at
    changed = False

    def edit_before_exchange(
        source: str,
        destination: str,
        directory_descriptor: int,
    ) -> bool:
        nonlocal changed
        if destination == "structure.md" and not changed:
            changed = True
            structure.write_text(concurrent, encoding="utf-8")
        return original(source, destination, directory_descriptor)

    monkeypatch.setattr(visualize, "_replace_at", edit_before_exchange)

    with pytest.raises(VisualizationError, match="displaced generated output changed"):
        export_structure(blueprint)

    assert structure.read_text(encoding="utf-8") == concurrent


def test_structure_restores_replacement_after_final_expectation_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import visualize

    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    structure = export_structure(blueprint)
    authored = "# Concurrent authored replacement\n"
    original = visualize._verify_output_expectation
    changed = False

    def replace_after_verify(*args, **kwargs):
        nonlocal changed
        state = original(*args, **kwargs)
        if not changed:
            changed = True
            attacker = blueprint / ".attacker"
            attacker.write_text(authored, encoding="utf-8")
            attacker.replace(structure)
        return state

    monkeypatch.setattr(visualize, "_verify_output_expectation", replace_after_verify)

    with pytest.raises(VisualizationError, match="displaced generated output changed"):
        export_structure(blueprint)

    assert structure.read_text(encoding="utf-8") == authored


def test_graph_refuses_concurrent_creation_after_absent_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import visualize

    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "only.md", "Only")
    output = blueprint / "dependencies.md"
    original = visualize.mermaid.render_page

    def create_after_preflight(*args, **kwargs):
        page = original(*args, **kwargs)
        output.write_text("# Concurrent authored file\n", encoding="utf-8")
        return page

    monkeypatch.setattr(visualize.mermaid, "render_page", create_after_preflight)

    with pytest.raises(VisualizationError, match="changed after ownership preflight"):
        export_graph(blueprint)

    assert output.read_text(encoding="utf-8") == "# Concurrent authored file\n"


def test_graph_rejects_source_edit_after_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import visualize

    blueprint = tmp_path / "blueprint"
    article = blueprint / "roadmap" / "only.md"
    _write_node(article, "Old title")
    original = visualize.mermaid.render_page

    def edit_after_render(*args, **kwargs):
        page = original(*args, **kwargs)
        _write_node(article, "New title")
        return page

    monkeypatch.setattr(visualize.mermaid, "render_page", edit_after_render)

    with pytest.raises(VisualizationError, match="blueprint changed"):
        export_graph(blueprint)

    assert not (blueprint / "dependencies.md").exists()


def test_graph_rolls_back_when_source_changes_during_output_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import visualize

    blueprint = tmp_path / "blueprint"
    article = blueprint / "roadmap" / "only.md"
    _write_node(article, "Old title")
    output = export_graph(blueprint)
    previous = output.read_bytes()
    _write_node(article, "Second title")
    original = visualize._replace_at
    changed = False

    def edit_during_replace(source: str, destination: str, descriptor: int) -> bool:
        nonlocal changed
        exchanged = original(source, destination, descriptor)
        if not changed:
            changed = True
            _write_node(article, "Changed during commit")
        return exchanged

    monkeypatch.setattr(visualize, "_replace_at", edit_during_replace)

    with pytest.raises(VisualizationError, match="blueprint changed"):
        export_graph(blueprint)

    assert output.read_bytes() == previous


def test_combined_visualization_rolls_back_both_outputs_on_source_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import visualize

    blueprint = tmp_path / "blueprint"
    article = blueprint / "roadmap" / "only.md"
    _write_node(article, "Old title")
    assert main([str(blueprint), "--structure"]) == 0
    graph = blueprint / "dependencies.md"
    structure = blueprint / "structure.md"
    previous_graph = graph.read_bytes()
    previous_structure = structure.read_bytes()
    _write_node(article, "Second title")
    original = visualize._replace_at
    changed = False

    def edit_during_structure_replace(
        source: str,
        destination: str,
        descriptor: int,
    ) -> bool:
        nonlocal changed
        exchanged = original(source, destination, descriptor)
        if destination == "structure.md" and not changed:
            changed = True
            _write_node(article, "Changed during commit")
        return exchanged

    monkeypatch.setattr(visualize, "_replace_at", edit_during_structure_replace)

    with pytest.raises(SystemExit, match="2"):
        main([str(blueprint), "--structure"])

    assert graph.read_bytes() == previous_graph
    assert structure.read_bytes() == previous_structure


def test_default_graph_write_stays_with_the_selected_blueprint_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import visualize

    blueprint = tmp_path / "blueprint"
    replacement = tmp_path / "replacement"
    retained = tmp_path / "retained"
    _write_node(blueprint / "roadmap" / "selected.md", "Selected")
    _write_node(replacement / "roadmap" / "replacement.md", "Replacement")
    original_replace = visualize._replace_at

    def replace_during_swap(
        source: str,
        destination: str,
        directory_descriptor: int,
    ) -> bool:
        blueprint.rename(retained)
        replacement.rename(blueprint)
        try:
            exchanged = original_replace(source, destination, directory_descriptor)
            assert not (blueprint / destination).exists()
            return exchanged
        finally:
            blueprint.rename(replacement)
            retained.rename(blueprint)

    monkeypatch.setattr(visualize, "_replace_at", replace_during_swap)

    output = export_graph(blueprint)

    assert "Selected" in output.read_text(encoding="utf-8")
    assert not (replacement / "dependencies.md").exists()


def test_structure_inventory_never_enumerates_a_reselected_blueprint_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = tmp_path / "blueprint"
    replacement = tmp_path / "replacement"
    retained = tmp_path / "retained"
    _write_node(blueprint / "roadmap" / "old.md", "Old")
    _write_node(replacement / "roadmap" / "new.md", "New")
    original_rglob = Path.rglob
    attacked = False

    def substitute_during_rglob(path: Path, pattern: str):
        nonlocal attacked
        if path == blueprint:
            attacked = True
            blueprint.rename(retained)
            replacement.rename(blueprint)
            try:
                return iter(tuple(original_rglob(blueprint, pattern)))
            finally:
                blueprint.rename(replacement)
                retained.rename(blueprint)
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", substitute_during_rglob)

    page = export_structure(blueprint).read_text(encoding="utf-8")

    assert not attacked
    assert "[Old](roadmap/old.md)" in page
    assert "new.md" not in page


def test_structure_reports_the_unsafe_snapshot_path(
    tmp_path: Path,
) -> None:
    from autoform_cli.visualize import VisualizationError

    blueprint = tmp_path / "blueprint"
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    _write_node(blueprint / "roadmap" / "result.md", "Result")
    (blueprint / "notes.md").symlink_to(outside)

    with pytest.raises(
        VisualizationError,
        match=r"notes\.md: symbolic links are not supported",
    ):
        export_structure(blueprint)

    assert not (blueprint / "structure.md").exists()


def test_cli_prepares_structure_before_publishing_the_graph(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blueprint = tmp_path / "blueprint"
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    _write_node(blueprint / "roadmap" / "result.md", "Result")
    (blueprint / "notes.md").symlink_to(outside)
    graph_output = blueprint / "dependencies.md"
    graph_output.write_text("previous graph\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="2"):
        main([str(blueprint), "--structure"])

    assert "notes.md: symbolic links are not supported" in capsys.readouterr().err
    assert graph_output.read_text(encoding="utf-8") == "previous graph\n"
    assert not (blueprint / "structure.md").exists()



def test_the_vault_structure_page_warns_about_a_flat_roadmap(tmp_path: Path) -> None:
    """The one fault the file explorer cannot show, as an Obsidian callout."""
    blueprint = tmp_path / "blueprint"
    _write_node(blueprint / "roadmap" / "README.md", "Roadmap")
    for name in ("a", "b", "c", "d"):
        _write_node(blueprint / "roadmap" / f"{name}.md", f"Result {name}", declaration="theorem")

    page = export_structure(blueprint).read_text(encoding="utf-8")

    assert "> [!warning] Every article sits directly under `roadmap/`." in page
