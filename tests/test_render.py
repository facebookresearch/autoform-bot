from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import autoform_cli.render as render_module
import autoform_cli._tree_snapshot as tree_snapshot_module
from autoform_cli.lean import _normalize_remote
from autoform_cli.render import PUBLICATION_MANIFEST, PublicationError, render_site
from autoform_cli.runtime import bind_runtime_paths
from autoform_cli.status import STATES


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (project / "Project").mkdir()
    (project / "Project" / "Basic.lean").write_text(
        "namespace Project\n\ndef Base : Nat := 0\n\ntheorem top : True := trivial\n\nend Project\n",
        encoding="utf-8",
    )
    (project / "blueprint" / "README.md").write_text(
        "---\nkind: blueprint\n---\n\n# Overview\n\n- [Roadmap](roadmap/README.md)\n",
        encoding="utf-8",
    )
    (roadmap / "README.md").write_text(
        "---\n---\n\n# Roadmap\n\n"
        "This chapter develops the base object before the main result.\n\n"
        "## Definitions\n\n- [Base](base.md)\n\n"
        "## Results\n\n- [Top](top.md)\n",
        encoding="utf-8",
    )
    (roadmap / "base.md").write_text(
        "---\ndeclaration: def\nstatement: formalized\nlean: Project.Base\n---\n\n"
        "# Base\n\nThe base object.\n\n## Depends on\n\nThis node has no prerequisites.\n",
        encoding="utf-8",
    )
    (roadmap / "top.md").write_text(
        "---\ndeclaration: theorem\nstatement: formalized\nproof: formalized\n"
        "lean: Project.top\ndiscussion: 42\n---\n\n"
        "# Top\n\nThe main result.\n\n## Sources\n\n- [Paper](../sources.md)\n\n"
        "## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    coverage = project / "blueprint" / "coverage" / "README.md"
    coverage.parent.mkdir(exist_ok=True)
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Project scope | MAPPED | Source audit pending |\n",
        encoding="utf-8",
    )
    return project


def _render(tmp_path: Path, **kwargs):
    project = _project(tmp_path)
    report = render_site(
        project / "blueprint",
        tmp_path / "out",
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref="cafe1234",
        **kwargs,
    )
    return project, report


def test_render_writes_a_derived_tree_and_leaves_the_vault_alone(tmp_path: Path) -> None:
    project, report = _render(tmp_path)
    out = tmp_path / "out"

    assert report.nodes == 2
    assert report.linked == 2
    assert report.unresolved == []
    # Progress folded into the Book landing and the Graph; no separate page.
    assert not (out / "progress.md").exists()
    assert not (out / "book.md").exists()
    assert (out / "dependencies.md").is_file()
    assert (out / "dependencies/chapters/roadmap.md").is_file()
    assert (out / "dependencies/nodes/base.md").is_file()
    assert (out / "dependencies/nodes/top.md").is_file()
    assert (out / "dependencies/full.md").is_file()
    assert (out / "stylesheets/blueprint.css").is_file()
    assert (out / "javascripts/blueprint-mermaid.js").is_file()
    assert (out / PUBLICATION_MANIFEST).is_file()
    # Nodes are absorbed into their chapter, not published one page each.
    assert not (out / "roadmap/base.md").exists()
    assert not (out / "roadmap/top.md").exists()
    # The source vault keeps no generated files.
    assert not (project / "blueprint" / "dependencies.md").exists()
    assert "## Depends on" in (project / "blueprint/roadmap/top.md").read_text(encoding="utf-8")

    project_map = (out / "dependencies.md").read_text(encoding="utf-8")
    assert "graph_view: project" in project_map
    assert '"dependencies/chapters/roadmap.html"' in project_map


def test_a_graph_page_hides_its_legend_behind_an_icon(tmp_path: Path) -> None:
    """Every graph page carried a disclosure captioned "What the colours mean".

    That is a headline-sized row, under every diagram, for a question a reader
    asks once. The legend is the same; only its trigger shrank. It has to open
    without script and by keyboard, so it is a real button revealed on
    :focus-within rather than a hover-only span.
    """
    _render(tmp_path)
    page = (tmp_path / "out/dependencies/chapters/roadmap.md").read_text(encoding="utf-8")
    css = (tmp_path / "out/stylesheets/blueprint.css").read_text(encoding="utf-8")

    assert "<summary>What the colours mean</summary>" not in page
    assert '<details class="bp-legend"' not in page
    assert 'class="bp-legend-icon"' in page
    # The legend itself is still there, just not laid out on the page.
    assert 'class="bp-legend-grid"' in page
    assert page.index("bp-legend-icon") < page.index("```mermaid")
    assert "<button" in page and 'aria-describedby="bp-legend-note"' in page
    assert ".bp-legend-tip:focus-within .bp-legend-note" in css


def test_the_structure_page_shows_the_tree_not_the_content(tmp_path: Path) -> None:
    """Auditing layout needs directories; every chapter's file is `README.md`.

    Listing filenames alone puts three indistinguishable `README.md` rows on
    the page, which is exactly the question the reader came to answer.
    """
    _render(tmp_path)
    page = (tmp_path / "out/structure.md").read_text(encoding="utf-8")

    assert "<strong>roadmap/</strong>" in page
    assert "<strong>blueprint/</strong>" in page
    # Files carry their title and status, and link to the statement itself.
    assert "top.md" in page and "bp-tree-title'>Top<" in page
    assert "roadmap/README.md#top" in page
    assert 'bp-swatch-fully_proved"' in page
    # Prose that was never meant to be a node is not an anomaly.
    assert "not in the graph" not in page
    assert "bp-tree-warn" not in page


def test_the_structure_page_names_a_vault_with_no_chapters(tmp_path: Path) -> None:
    """The fault this page exists for: articles heaped directly under roadmap/.

    It parses, `autoform check` passes, and the book publishes as one
    undivided list, so no rendered view of the content reveals it.
    """
    project = tmp_path / "flat"
    roadmap = project / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True)
    (project / "blueprint" / "README.md").write_text("---\n---\n\n# Flat\n", encoding="utf-8")
    (roadmap / "README.md").write_text("---\n---\n\n# Roadmap\n", encoding="utf-8")
    coverage = project / "blueprint/coverage/README.md"
    coverage.parent.mkdir(exist_ok=True)
    coverage.write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
        "| Project scope | MAPPED | Source audit pending |\n",
        encoding="utf-8",
    )
    for name in ("a", "b", "c", "d"):
        (roadmap / f"{name}.md").write_text(
            f"---\ndeclaration: theorem\n---\n\n# Result {name}\n", encoding="utf-8"
        )

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    page = (tmp_path / "out/structure.md").read_text(encoding="utf-8")
    assert "bp-tree-warn" in page
    assert "publishes as one undivided list" in page


def test_the_site_publishes_no_second_copy_of_the_vault(tmp_path: Path) -> None:
    """One address per statement.

    The site used to mirror the authored Markdown under `wiki/`, which gave
    every article a second URL holding the same words. What that was for is
    already covered: an absorbed leaf keeps an anchor on its chapter page, and
    each statement links to its own file in the repository, where the raw text
    comes with history and an edit button.
    """
    _render(tmp_path)
    out = tmp_path / "out"
    chapter = (out / "roadmap/README.md").read_text(encoding="utf-8")

    assert not (out / "wiki").exists()
    assert "Markdown source" not in (out / "SUMMARY.md").read_text(encoding="utf-8")
    # The two things the mirror was there for.
    assert 'id="top"' in chapter
    assert "blueprint/roadmap/top.md" in chapter


def test_the_vault_graph_keeps_a_visible_legend(tmp_path: Path) -> None:
    """Obsidian never loads this stylesheet, so a hover note is unreachable there."""
    from autoform_cli.visualize import export_graph

    project = _project(tmp_path)
    document = export_graph(project / "blueprint").read_text(encoding="utf-8")

    assert "bp-legend-icon" not in document
    assert "## Legend" in document
    assert 'class="bp-legend-grid"' in document


def test_the_site_carries_its_own_mark(tmp_path: Path) -> None:
    """The logo is generated, so a project never has to commit a binary for it.

    Both `logo` and `favicon` in the template name this path, so a build that
    stopped writing it would fall back to Material's default without failing.
    """
    import xml.etree.ElementTree as ElementTree

    _render(tmp_path)
    mark = tmp_path / "out/assets/autoform.svg"

    assert mark.is_file()
    root = ElementTree.fromstring(mark.read_text(encoding="utf-8"))
    assert root.get("viewBox") == "0 0 48 48"
    # Square, so it is not letterboxed in the header or the favicon slot.
    assert root.get("width") == root.get("height")
    assert root.find("{http://www.w3.org/2000/svg}title").text == "Autoform"


def _chapter_map_links(page: Path) -> set[str]:
    return set(re.findall(r'"(dependencies/chapters/[^"]+)\.html"', page.read_text("utf-8")))


def test_the_home_page_project_map_links_to_chapter_pages_that_exist(tmp_path: Path) -> None:
    """The home map and the Graph tab draw the same view but built links apart.

    A project-view node is a chapter, so its id is namespaced `scope:<group>`,
    and only the Graph tab stripped that before naming the page. The home map
    asked for `dependencies/chapters/scope:<group>.html`, which is nothing.
    """
    _render(tmp_path)
    out = tmp_path / "out"

    links = _chapter_map_links(out / "README.md")
    assert links, "the home page should carry a project map"
    assert links == _chapter_map_links(out / "dependencies.md")
    for link in links:
        assert (out / f"{link}.md").is_file(), f"home page links to a missing page: {link}"


def test_the_home_page_project_map_survives_named_chapters(tmp_path: Path) -> None:
    """The flat project exercises the `or 'roadmap'` fallback, not the prefix."""
    project = _project(tmp_path)
    chapter = project / "blueprint" / "roadmap" / "structure"
    chapter.mkdir()
    (chapter / "README.md").write_text(
        "---\n---\n\n# Structure\n\nA named chapter.\n\n## Results\n\n- [Side](side.md)\n",
        encoding="utf-8",
    )
    (chapter / "side.md").write_text(
        "---\ndeclaration: theorem\nstatement: formalized\nlean: Project.top\n---\n\n"
        "# Side\n\nA statement in a named chapter.\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    render_site(project / "blueprint", out, lean_root=project)

    links = _chapter_map_links(out / "README.md")
    assert "dependencies/chapters/structure" in links
    assert not any("scope:" in link for link in links)
    for link in links:
        assert (out / f"{link}.md").is_file(), f"home page links to a missing page: {link}"


def test_a_chapter_places_statements_in_the_authored_narrative(tmp_path: Path) -> None:
    _render(tmp_path)
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")

    assert page.index("This chapter develops") < page.index('class="bp-progress-overview"')
    assert page.index("## Definitions") < page.index('id="base"') < page.index("## Results")
    assert page.index("## Results") < page.index('id="top"')
    assert '<div class="bp-thmwrapper theorem-style-definition bp-fully_proved" id="base"' in page
    assert '<div class="bp-thmwrapper theorem-style-plain bp-fully_proved" id="top"' in page
    assert '<span class="bp-thmcaption">Definition</span><span class="bp-thmlabel">1</span>' in page
    assert '<span class="bp-thmtitle">Top</span>' in page
    assert "The main result." in page
    assert "1 definition · 1 result" in page
    # A node's own subheadings must not compete with the chapter's structure.
    assert "###### Sources" in page
    assert "\n## Sources" not in page
    assert "## Depends on" not in page
    assert "Additional formalization targets" not in page


def test_unplaced_statements_use_an_explicit_fallback_section(tmp_path: Path) -> None:
    project = _project(tmp_path)
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text(
        "# Roadmap\n\nOpening prose.\n\n## Results\n\n- [Top](top.md)\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")

    assert page.index('id="top"') < page.index("## Additional formalization targets")
    assert page.index("## Additional formalization targets") < page.index('id="base"')


def test_authored_slots_override_dependency_order_for_book_flow(tmp_path: Path) -> None:
    project = _project(tmp_path)
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text(
        "# Roadmap\n\n## Reading order\n\n- [Top](top.md)\n- [Base](base.md)\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")

    assert page.index('id="top"') < page.index('id="base"')


def test_cross_references_point_at_anchors_on_the_chapter(tmp_path: Path) -> None:
    _render(tmp_path)
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")

    assert "https://github.com/owner/repo/blob/cafe1234/Project/Basic.lean#L5" in page
    assert '<a class="bp-code-link"' in page
    assert 'aria-label="View Project.top in Lean source"' in page
    assert '<svg class="bp-code-icon"' in page
    assert '<a class="bp-context-link" href="../dependencies/nodes/top.html"' in page
    assert 'aria-label="Open local dependency context for Top"' in page
    assert '<details class="bp-dependencies"><summary>Dependencies</summary>' in page
    assert '<span class="bp-key">Statement uses</span>' in page
    assert 'href="#base">Definition 1 (Base)' in page
    assert 'href="#top">Theorem 1 (Top)' in page
    assert 'href="https://github.com/owner/repo/issues/42">#42' in page


def test_overview_carries_the_counts_without_a_separate_progress_page(tmp_path: Path) -> None:
    """Progress is folded in: the landing page states it, the Graph colours it."""

    _render(tmp_path)
    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")

    # The landing page leads with the figures; a chapter keeps the compact strip.
    assert overview.index("bp-hero-title") < overview.index("bp-hero-figures")
    assert 'class="bp-figure-value"' in overview
    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert "1 definition · 1 result" in chapter
    # No page to send the reader to, so no link out of the summary.
    assert "bp-progress-link" not in overview
    assert not (tmp_path / "out/progress.md").exists()


def test_the_landing_page_is_the_hero_and_the_map_and_nothing_else(tmp_path: Path) -> None:
    """A blueprint's subject is the shape of the project, not its front matter.

    The map used to sit below the authored prose and the status breakdown,
    which put the one thing a visitor comes for last on the page. The prose
    itself was a contents list and links to the roadmap, the coverage notes and
    the dependency view, all of which are tabs.
    """
    _render(tmp_path)
    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")

    # The sidebar would hold a single "Home" entry here, so the page drops it.
    assert "  - navigation" in overview.split("---")[1]
    assert overview.index("bp-hero") < overview.index('class="bp-map"')
    assert "bp-landing-prose" not in overview
    assert "## Contents" not in overview
    # The legend travels with the map instead of becoming a section of its own.
    assert "## Status breakdown" not in overview
    assert overview.index("bp-map-legend") > overview.index("bp-map-head")


def test_book_navigation_is_bottom_only_and_never_crosses_into_project_views(tmp_path: Path) -> None:
    _render(tmp_path)
    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")
    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    dependencies = (tmp_path / "out/dependencies.md").read_text(encoding="utf-8")

    # The landing page is a dashboard, not chapter one, so it carries no strip.
    # This fixture has a single chapter, which leaves nowhere to page to.
    assert "bp-book-nav" not in overview
    assert "bp-book-nav" not in chapter
    graph_page = (tmp_path / "out/dependencies.md").read_text(encoding="utf-8")
    assert "bp-book-nav" not in graph_page
    assert "bp-book-nav" not in dependencies


def test_links_naming_a_node_file_follow_it_onto_the_chapter(tmp_path: Path) -> None:
    """A node stops being a page once its chapter absorbs it, so links move.

    Checked from the coverage page because the landing page no longer prints
    the authored body; the rewrite itself is the same code path either way.
    """
    project = _project(tmp_path)
    coverage = project / "blueprint" / "coverage"
    (coverage / "README.md").write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
        "| Top | MAPPED | In scope: [Top](../roadmap/top.md). |\n",
        encoding="utf-8",
    )
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    published = (tmp_path / "out/coverage/README.md").read_text(encoding="utf-8")
    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")
    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert "[Top](../roadmap/README.md#top)" in published
    assert "bp-book-nav" not in overview
    assert "bp-book-nav" not in chapter


def test_coverage_is_reachable_once_the_landing_page_stops_listing_it(
    tmp_path: Path,
) -> None:
    """Dropping the authored body would otherwise strand the coverage contract.

    Nothing else linked it: it is not a chapter, so the book order never picks
    it up, and the landing page's prose was its only route.
    """
    project = _project(tmp_path)
    coverage = project / "blueprint" / "coverage"
    (coverage / "README.md").write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
        "| Project scope | MAPPED | What counts. |\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    nav = (tmp_path / "out/SUMMARY.md").read_text(encoding="utf-8")
    assert "[Coverage](coverage/README.md)" in nav


def test_a_hoisted_body_keeps_its_other_links_working(tmp_path: Path) -> None:
    """The body moves up a directory, so its relative links must move with it."""
    project = _project(tmp_path)
    (project / "blueprint/sources.md").write_text("# Paper\n", encoding="utf-8")
    nested = project / "blueprint/roadmap/chapter/deep.md"
    nested.parent.mkdir(parents=True)
    (nested.parent / "README.md").write_text("# Chapter\n", encoding="utf-8")
    nested.write_text(
        "---\ndeclaration: theorem\n---\n\n# Deep\n\nBody.\n\n"
        "## Sources\n\n- [Paper](../../sources.md)\n",
        encoding="utf-8",
    )
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    chapter = (tmp_path / "out/roadmap/chapter/README.md").read_text(encoding="utf-8")
    assert "[Paper](../../sources.md)" in chapter


def test_unresolved_declarations_are_reported_not_linked(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/top.md").write_text(
        "---\ndeclaration: theorem\nstatement: formalized\nlean: Project.absent\n---\n"
        "\n# Top\n",
        encoding="utf-8",
    )
    report = render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert report.unresolved == ["top: Project.absent"]
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert "not found in the Lean sources" in page


def test_stale_generated_files_are_not_republished(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/dependencies.html").write_text("stale", encoding="utf-8")
    (project / "blueprint/progress.md").write_text("stale", encoding="utf-8")
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert not (tmp_path / "out/dependencies.html").exists()
    assert (tmp_path / "out/dependencies.md").is_file()
    assert not (tmp_path / "out/progress.md").exists()


def test_nested_authored_page_named_like_generated_output_is_published(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/dependencies.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# Authored dependencies\n\nA theorem.\n",
        encoding="utf-8",
    )

    report = render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert report.nodes == 3
    graph = (tmp_path / "out/dependencies/nodes/dependencies.md").read_text(
        encoding="utf-8"
    )
    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert "Authored dependencies" in graph
    assert "Authored dependencies" in chapter


def test_both_colour_schemes_are_published(tmp_path: Path) -> None:
    _render(tmp_path)
    css = (tmp_path / "out/stylesheets/blueprint.css").read_text(encoding="utf-8")
    script = (tmp_path / "out/javascripts/blueprint-mermaid.js").read_text(encoding="utf-8")

    # Facebook's surface greys and Meta blue, not Material's defaults, and both
    # schemes hang off the theme's own data-md-color-scheme attribute.
    assert "Plus Jakarta Sans" in css and "JetBrains Mono" in css
    assert "--bp-link: #0064E0" in css
    assert "[data-md-color-scheme=slate]" in css
    assert "--bp-link: #2D88FF" in css
    assert "--bp-surface: #242526" in css
    assert "background-color: #18191A" in css
    # The brand sweep is defined once and reused, rather than pasted per rule.
    assert css.count("--bp-sweep:") == 1
    assert css.count("var(--bp-sweep)") >= 3
    for state in STATES:
        assert f".bp-{state.key} .bp-mark {{ color: {state.stroke}; }}" in css
        assert f"[data-md-color-scheme=slate] .bp-{state.key} .bp-mark" in css

    # Material draws its own header and sidebars, so the stylesheet no longer
    # restyles theme chrome. It sets the reading column and nothing else.
    assert ".md-typeset {" in css
    assert ".navbar" not in css
    assert "data-bs-theme" not in css

    # A rendered diagram cannot be restyled, so the script owns both palettes
    # and redraws when the scheme changes.
    assert '"light"' in script and '"dark"' in script
    assert "data-md-color-scheme" in script
    assert "MutationObserver" in script
    assert "bindFunctions" in script
    for state in STATES:
        assert f"classDef {state.key} fill:{state.fill}," in script
        assert f"classDef {state.key} fill:{state.dark_fill}," in script
    # Both palettes ship: the light chapter box and the dark one.
    assert "classDef scope fill:#EBF2FE" in script
    assert "classDef scope fill:#1C1D1F" in script
    assert "classDef boundary" in script
    assert "classDef focus" in script


def test_the_generated_script_is_valid_javascript(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    _render(tmp_path)
    script = tmp_path / "out/javascripts/blueprint-mermaid.js"

    result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("destination", ("same", "child", "parent"))
def test_refuses_overlapping_source_and_output(tmp_path: Path, destination: str) -> None:
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    output = {
        "same": blueprint,
        "child": blueprint / "site-src",
        "parent": project,
    }[destination]

    with pytest.raises(PublicationError, match="must be disjoint"):
        render_site(blueprint, output)


def test_render_is_deterministic_and_records_a_path_free_manifest(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outputs = [tmp_path / "first", tmp_path / "second"]
    for output in outputs:
        render_site(
            project / "blueprint",
            output,
            lean_root=project,
            repository_url="https://github.com/owner/repo",
            ref="a" * 40,
        )

    def files(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    first = files(outputs[0])
    assert first == files(outputs[1])
    manifest = json.loads(first[PUBLICATION_MANIFEST])
    assert manifest == {
        "complete": True,
        "coverage": {
            "complete": False,
            "counts": {"DECOMPOSED": 0, "DEFERRED": 0, "MAPPED": 1, "OUT": 0},
            "schema": "autoform-coverage/v1",
            "source_path": "coverage/README.md",
            "source_sha256": manifest["coverage"]["source_sha256"],
        },
        "dependencies": 1,
        "directories": manifest["directories"],
        "files": manifest["files"],
        "git_ref": "a" * 40,
        "lean_source_revision": manifest["lean_source_revision"],
        "nodes": 3,
        "schema": "autoform-publication/v2",
        "source": "blueprint/roadmap Markdown",
        "source_revision": manifest["source_revision"],
        "views": ["book", "progress", "project", "chapter", "focus", "full"],
    }
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["source_revision"])
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["lean_source_revision"])
    expected_files = {path for path in first if path != PUBLICATION_MANIFEST}
    assert set(manifest["files"]) == expected_files
    assert all(
        digest == hashlib.sha256(first[path]).hexdigest()
        for path, digest in manifest["files"].items()
    )
    expected_directories = sorted(
        {
            parent.as_posix()
            for path in expected_files
            for parent in Path(path).parents
            if parent != Path(".")
        }
    )
    assert manifest["directories"] == expected_directories
    assert str(tmp_path).encode() not in b"".join(first.values())


def test_manifest_records_machine_checkable_coverage_aggregates(tmp_path: Path) -> None:
    project = _project(tmp_path)
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main result | DECOMPOSED | [Roadmap](../roadmap/README.md) |\n"
        "| Corollaries | MAPPED | Source audit pending |\n"
        "| Experiments | OUT | Narrative only |\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"

    render_site(project / "blueprint", output, lean_root=project)

    manifest = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["coverage"] == {
        "complete": False,
        "counts": {"DECOMPOSED": 1, "DEFERRED": 0, "MAPPED": 1, "OUT": 1},
        "schema": "autoform-coverage/v1",
        "source_path": "coverage/README.md",
        "source_sha256": manifest["coverage"]["source_sha256"],
    }
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["coverage"]["source_sha256"])


def test_render_rejects_invalid_coverage_before_touching_output(tmp_path: Path) -> None:
    project = _project(tmp_path)
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text("# Coverage\n\nNo table.\n", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("owned by user\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="coverage contract has no"):
        render_site(project / "blueprint", output, lean_root=project)

    assert sentinel.read_text(encoding="utf-8") == "owned by user\n"
    assert not (output / PUBLICATION_MANIFEST).exists()


def test_render_does_not_read_excluded_obsidian_state(tmp_path: Path) -> None:
    project = _project(tmp_path)
    private = project / "blueprint/.obsidian/private"
    private.mkdir(parents=True)
    (private / "large.bin").write_bytes(b"excluded")
    private.chmod(0)
    try:
        report = render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    finally:
        private.chmod(0o700)

    assert report.nodes == 2
    assert not (tmp_path / "out/.obsidian").exists()


def test_render_refuses_a_contract_truncated_by_a_multiline_comment(tmp_path: Path) -> None:
    project = _project(tmp_path)
    coverage = project / "blueprint/coverage/README.md"
    # The blank line inside the comment used to end the table, so this published
    # `MAPPED: 0` and `complete: true` while the author had declared a MAPPED row.
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Narrative | OUT | Not in scope |\n"
        "<!-- reviewer note\n"
        "\n"
        "more note -->\n"
        "| Main result | MAPPED | Needs roadmap articles |\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"

    with pytest.raises(PublicationError, match="follows hidden content"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not (output / PUBLICATION_MANIFEST).exists()


def test_render_refuses_a_contract_truncated_by_a_fenced_block(tmp_path: Path) -> None:
    project = _project(tmp_path)
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Narrative | OUT | Not in scope |\n"
        "```\n"
        "\n"
        "example\n"
        "```\n"
        "| Main result | MAPPED | Needs roadmap articles |\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"

    with pytest.raises(PublicationError, match="follows hidden content"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not (output / PUBLICATION_MANIFEST).exists()


def test_render_refuses_a_contract_whose_header_layout_is_hidden(tmp_path: Path) -> None:
    project = _project(tmp_path)
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence <!-- | hidden --> |\n"
        "| --- | --- | --- |\n"
        "| Main result | OUT | Not in scope |\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"

    with pytest.raises(PublicationError, match="does not render as a table"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not (output / PUBLICATION_MANIFEST).exists()


def test_render_refuses_decomposition_evidence_with_one_broken_link(tmp_path: Path) -> None:
    project = _project(tmp_path)
    coverage = project / "blueprint/coverage/README.md"
    # One link resolves and one does not. Publishing this would report
    # `coverage.complete: true` over evidence the audit rejects.
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main result | DECOMPOSED | "
        "[Roadmap](../roadmap/README.md) and [Absent](../roadmap/absent.md) |\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"

    with pytest.raises(PublicationError, match="does not resolve to a file"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not (output / PUBLICATION_MANIFEST).exists()


def test_render_refuses_decomposition_evidence_with_a_missing_anchor(tmp_path: Path) -> None:
    project = _project(tmp_path)
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main result | DECOMPOSED | [Results](../roadmap/README.md#absent-section) |\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"

    with pytest.raises(PublicationError, match="fragment does not resolve"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not (output / PUBLICATION_MANIFEST).exists()

    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Main result | DECOMPOSED | [Results](../roadmap/README.md#results) |\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", output, lean_root=project)

    manifest = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["coverage"]["complete"]


def test_render_replaces_only_an_exact_owned_publication(tmp_path: Path) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    render_site(project / "blueprint", output, lean_root=project)
    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["complete"]

    stale = output / "stale.txt"
    stale.write_text("old generated output\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="untracked or missing files.*stale.txt"):
        render_site(project / "blueprint", output, lean_root=project)

    assert stale.read_text(encoding="utf-8") == "old generated output\n"


def test_render_upgrades_an_exact_pre_lean_hash_v2_publication(tmp_path: Path) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    manifest_path = output / PUBLICATION_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("lean_source_revision")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    render_site(project / "blueprint", output, lean_root=project)

    upgraded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{64}", upgraded["lean_source_revision"])


def test_schema_only_manifest_cannot_authorize_deletion(tmp_path: Path) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("user data\n", encoding="utf-8")
    (output / PUBLICATION_MANIFEST).write_text(
        json.dumps(
            {"schema": "autoform-publication/v2", "complete": True},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PublicationError, match="valid file inventory"):
        render_site(project / "blueprint", output, lean_root=project)

    assert sentinel.read_text(encoding="utf-8") == "user data\n"


def test_render_refuses_a_modified_owned_file(tmp_path: Path) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    overview = output / "README.md"
    overview.write_text("changed after publication\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="modified Autoform publication"):
        render_site(project / "blueprint", output, lean_root=project)

    assert overview.read_text(encoding="utf-8") == "changed after publication\n"


def test_failed_staged_render_preserves_the_previous_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    before = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }

    def fail(*args, **kwargs):
        raise RuntimeError("injected render failure")

    monkeypatch.setattr(render_module, "_render_summary_nav", fail)
    with pytest.raises(PublicationError, match="injected render failure") as error:
        render_site(project / "blueprint", output, lean_root=project)

    after = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert after == before
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))
    assert len(workspaces) == 1
    assert str(workspaces[0]) in str(error.value)


def test_source_change_during_render_aborts_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    old_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    original = render_module._render_snapshot

    def mutate_after_render(*args, **kwargs):
        report = original(*args, **kwargs)
        article.write_text(article.read_text(encoding="utf-8") + "\nChanged concurrently.\n")
        return report

    monkeypatch.setattr(render_module, "_render_snapshot", mutate_after_render)
    with pytest.raises(PublicationError, match="blueprint changed during publication"):
        render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).read_bytes() == old_manifest
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))


def test_blueprint_reselection_during_snapshot_is_rejected_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    other_project = _project(tmp_path / "other")
    blueprint = project / "blueprint"
    replacement = other_project / "blueprint"
    retained = tmp_path / "retained-blueprint"
    output = tmp_path / "out"
    published = False
    swapped = False
    original_publish = render_module._publish_staged_site

    def swap_after_root_list(event: str, relative: str) -> None:
        nonlocal swapped
        if not swapped and event == "after-directory-list" and not relative:
            blueprint.rename(retained)
            replacement.rename(blueprint)
            swapped = True

    def record_publish(*args, **kwargs) -> None:
        nonlocal published
        published = True
        original_publish(*args, **kwargs)

    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", swap_after_root_list)
    monkeypatch.setattr(render_module, "_publish_staged_site", record_publish)
    try:
        with bind_runtime_paths(blueprint) as paths:
            with pytest.raises(PublicationError, match="blueprint changed during publication"):
                render_site(
                    paths.blueprint_dir,
                    output,
                    lean_root=project,
                    _expected_blueprint_identity=paths.blueprint_identity,
                    _expected_roadmap_identity=paths.roadmap_identity,
                )
    finally:
        if swapped:
            blueprint.rename(replacement)
            retained.rename(blueprint)

    assert not published
    assert not output.exists()


def test_source_revision_frames_file_names_and_contents_unambiguously(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a").write_bytes(b"X\0b\0Y")
    (second / "a").write_bytes(b"X")
    (second / "b").write_bytes(b"Y")

    assert render_module._source_revision(first) != render_module._source_revision(second)


def test_source_change_during_lean_indexing_aborts_before_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    article = project / "blueprint/roadmap/top.md"
    original = render_module.build_linker

    def mutate_after_index(*args, **kwargs):
        linker = original(*args, **kwargs)
        article.write_text(article.read_text(encoding="utf-8") + "\nChanged while indexing.\n")
        return linker

    monkeypatch.setattr(render_module, "build_linker", mutate_after_index)
    with pytest.raises(PublicationError, match="blueprint changed during publication"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not output.exists()


def test_lean_source_change_during_indexing_aborts_before_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    lean_source = project / "Project/Basic.lean"
    original = render_module.build_linker

    def mutate_after_index(*args, **kwargs):
        linker = original(*args, **kwargs)
        lean_source.write_text(
            "namespace Project\n\ndef Base : Nat := 0\n\nend Project\n",
            encoding="utf-8",
        )
        return linker

    monkeypatch.setattr(render_module, "build_linker", mutate_after_index)
    with pytest.raises(PublicationError, match="Lean sources changed"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not output.exists()


def test_lean_a_b_a_change_during_linker_construction_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    lean_source = project / "Project/Basic.lean"
    stable = lean_source.read_text(encoding="utf-8")
    transient = stable.replace("theorem top", "\n\n\n\n\ntheorem top")
    original = render_module.build_linker

    def expose_transient_generation(*args, **kwargs):
        lean_source.write_text(transient, encoding="utf-8")
        try:
            return original(*args, **kwargs)
        finally:
            lean_source.write_text(stable, encoding="utf-8")

    monkeypatch.setattr(render_module, "build_linker", expose_transient_generation)
    with pytest.raises(PublicationError, match="Lean sources changed"):
        render_site(
            project / "blueprint",
            output,
            lean_root=project,
            repository_url="https://github.com/owner/repo",
            ref="abc",
        )

    assert not output.exists()


def test_git_remote_a_b_a_change_cannot_change_published_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for variable in ("GITHUB_REPOSITORY", "GITHUB_SERVER_URL"):
        monkeypatch.delenv(variable, raising=False)
    project = _project(tmp_path)
    output = tmp_path / "out"
    correct = "https://github.com/correct/source.git"
    wrong = "https://github.com/wrong/source.git"
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "config", "remote.origin.url", correct], cwd=project, check=True
    )
    original = render_module.build_linker

    def expose_transient_remote(*args, **kwargs):
        assert kwargs["detect_missing"] is False
        subprocess.run(
            ["git", "config", "remote.origin.url", wrong], cwd=project, check=True
        )
        try:
            return original(*args, **kwargs)
        finally:
            subprocess.run(
                ["git", "config", "remote.origin.url", correct], cwd=project, check=True
            )

    monkeypatch.setattr(render_module, "build_linker", expose_transient_remote)
    render_site(project / "blueprint", output, lean_root=project, ref="abc123")

    page = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert "github.com/correct/source" in page
    assert "github.com/wrong/source" not in page


def test_source_directory_a_b_a_change_cannot_change_published_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    artifact = b"Source line.\n"
    source = project / "blueprint/sources/nested/book.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(artifact)
    digest = hashlib.sha256(artifact).hexdigest()
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        "---\n"
        "schema: autoform-coverage/v2\n"
        "artifact: sources/nested/book.txt\n"
        f"artifact_sha256: {digest}\n"
        "---\n\n"
        "# Coverage\n\n"
        "| Unit | Area | Lines | Locator | Unit SHA-256 | Coverage | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| unit | Main | 1-1 | Result | {digest} | DECOMPOSED | "
        "[Top](../roadmap/top.md) |\n",
        encoding="utf-8",
    )
    article = project / "blueprint/roadmap/top.md"
    article.write_text(
        article.read_text(encoding="utf-8")
        .replace("discussion: 42", "discussion: 42\nsource_units: [unit]")
        .replace("../sources.md", "../sources/nested/book.txt"),
        encoding="utf-8",
    )
    original = render_module._sources_base
    moved = project / "sources-original"
    decoy = project / "decoy"
    decoy.mkdir()

    def expose_transient_source_directory(*args, **kwargs):
        source.parent.parent.rename(moved)
        source.parent.parent.symlink_to(decoy, target_is_directory=True)
        try:
            return original(*args, **kwargs)
        finally:
            source.parent.parent.unlink()
            moved.rename(source.parent.parent)

    monkeypatch.setattr(render_module, "_sources_base", expose_transient_source_directory)
    output = tmp_path / "out"
    render_site(
        project / "blueprint",
        output,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref="abc",
    )

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert "/blob/abc/blueprint/sources/nested/book.txt" in chapter
    assert "/blob/abc/decoy/" not in chapter


def test_v2_render_rejects_a_case_alias_of_the_sources_directory(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    artifact = b"RAW SOURCE SENTINEL\n"
    source = project / "blueprint/sources/nested/book.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(artifact)
    digest = hashlib.sha256(artifact).hexdigest()
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        "---\n"
        "schema: autoform-coverage/v2\n"
        "artifact: sources/nested/book.txt\n"
        f"artifact_sha256: {digest}\n"
        "---\n\n"
        "# Coverage\n\n"
        "| Unit | Area | Lines | Locator | Unit SHA-256 | Coverage | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| unit | Main | 1-1 | Result | {digest} | DECOMPOSED | "
        "[Top](../roadmap/top.md) |\n",
        encoding="utf-8",
    )
    article = project / "blueprint/roadmap/top.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "discussion: 42", "discussion: 42\nsource_units: [unit]"
        ),
        encoding="utf-8",
    )
    canonical = project / "blueprint/sources"
    canonical.rename(project / "blueprint/Sources")
    if not canonical.exists():
        pytest.skip("filesystem is case-sensitive")

    output = tmp_path / "out"
    with pytest.raises(PublicationError, match="canonical sources directory"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not output.exists()


def test_v2_render_rewrites_case_aliases_of_source_links(tmp_path: Path) -> None:
    project = _project(tmp_path)
    artifact = b"Source line.\n"
    source = project / "blueprint/sources/nested/book.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(artifact)
    digest = hashlib.sha256(artifact).hexdigest()
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        "---\n"
        "schema: autoform-coverage/v2\n"
        "artifact: sources/nested/book.txt\n"
        f"artifact_sha256: {digest}\n"
        "---\n\n"
        "# Coverage\n\n"
        "| Unit | Area | Lines | Locator | Unit SHA-256 | Coverage | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| unit | Main | 1-1 | Result | {digest} | DECOMPOSED | "
        "[Top](../roadmap/top.md) |\n",
        encoding="utf-8",
    )
    article = project / "blueprint/roadmap/top.md"
    article.write_text(
        "---\n"
        "declaration: theorem\n"
        "statement: formalized\n"
        "proof: formalized\n"
        "lean: Project.top\n"
        "source_units: [unit]\n"
        "---\n\n"
        "# Top\n\nThe main result.\n\n"
        "## Sources\n\n"
        "- [Paper](../Sources/NESTED/BOOK.TXT)\n"
        "- ![Scan](../Sources/NESTED/BOOK.TXT)\n"
        "- <../Sources/NESTED/BOOK.TXT>\n"
        "- [Reference][paper]\n\n"
        "[paper]: ../Sources/NESTED/BOOK.TXT\n\n"
        "## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )

    output = tmp_path / "out"
    render_site(
        project / "blueprint",
        output,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref="abc",
    )

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    expected = "https://github.com/owner/repo/blob/abc/blueprint/sources/nested/book.txt"
    assert chapter.count(expected) == 4
    assert "NESTED/BOOK.TXT" not in chapter
    assert not (output / "Sources").exists()
    assert not (output / "sources").exists()


def test_render_rejects_a_case_alias_destination_inside_the_blueprint(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    alias = project / "BLUEPRINT"
    if not alias.exists():
        pytest.skip("filesystem is case-sensitive")

    output = alias / "site"
    with pytest.raises(PublicationError, match="must be disjoint"):
        render_site(blueprint, output, lean_root=project)

    assert not output.exists()
    assert not any(
        path.name.startswith(".autoform-publication-") for path in blueprint.iterdir()
    )


def test_v2_source_links_survive_a_case_alias_of_the_repository_root(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    alias = project.with_name(project.name.upper())
    if not alias.exists():
        pytest.skip("filesystem is case-sensitive")
    artifact = b"Source line.\n"
    source = project / "blueprint/sources/book.md"
    source.parent.mkdir()
    source.write_bytes(artifact)
    digest = hashlib.sha256(artifact).hexdigest()
    (project / "blueprint/coverage/README.md").write_text(
        "---\n"
        "schema: autoform-coverage/v2\n"
        "artifact: sources/book.md\n"
        f"artifact_sha256: {digest}\n"
        "---\n\n"
        "# Coverage\n\n"
        "| Unit | Area | Lines | Locator | Unit SHA-256 | Coverage | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| unit | Main | 1-1 | Result | {digest} | DECOMPOSED | "
        "[Top](../roadmap/top.md) |\n",
        encoding="utf-8",
    )
    article = project / "blueprint/roadmap/top.md"
    article.write_text(
        article.read_text(encoding="utf-8")
        .replace("discussion: 42", "discussion: 42\nsource_units: [unit]")
        .replace("../sources.md", "../sources/book.md"),
        encoding="utf-8",
    )

    output = tmp_path / "out"
    render_site(
        project / "blueprint",
        output,
        lean_root=alias,
        repository_url="https://github.com/owner/repo",
        ref="abc",
    )

    expected = "https://github.com/owner/repo/blob/abc/blueprint/sources/book.md"
    assert expected in (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert not (output / "sources").exists()


def test_private_source_snapshot_is_revalidated_after_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    original = render_module._render_snapshot

    def mutate_snapshot(blueprint_dir, *args, **kwargs):
        roadmap = Path(blueprint_dir) / "roadmap/README.md"
        roadmap.write_text(roadmap.read_text(encoding="utf-8") + "\nSNAPSHOT SUBSTITUTE\n")
        return original(blueprint_dir, *args, **kwargs)

    monkeypatch.setattr(render_module, "_render_snapshot", mutate_snapshot)
    with pytest.raises(PublicationError, match="snapshot changed"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not output.exists()
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))
    assert len(workspaces) == 1
    assert "SNAPSHOT SUBSTITUTE" in (
        workspaces[0] / "source/roadmap/README.md"
    ).read_text(encoding="utf-8")


def test_private_source_snapshot_identity_is_revalidated_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    original = render_module._render_snapshot

    def substitute_snapshot(blueprint_dir, *args, **kwargs):
        report = original(blueprint_dir, *args, **kwargs)
        snapshot = Path(blueprint_dir)
        displaced = snapshot.parent / "original-source"
        snapshot.rename(displaced)
        shutil.copytree(displaced, snapshot)
        return report

    monkeypatch.setattr(render_module, "_render_snapshot", substitute_snapshot)
    with pytest.raises(PublicationError, match="snapshot changed"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not output.exists()
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))
    assert len(workspaces) == 1
    assert (workspaces[0] / "source").is_dir()
    assert (workspaces[0] / "original-source").is_dir()


def test_post_commit_snapshot_substitution_is_reported_as_cleanup_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    original = render_module._publish_staged_site

    def substitute_snapshot_after_publish(*args, **kwargs):
        original(*args, **kwargs)
        snapshot = Path(kwargs["source_snapshot"])
        displaced = snapshot.parent / "original-source"
        snapshot.rename(displaced)
        shutil.copytree(displaced, snapshot)

    monkeypatch.setattr(
        render_module, "_publish_staged_site", substitute_snapshot_after_publish
    )
    report = render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).is_file()
    assert len(report.warnings) == 1
    assert "cleanup was refused" in report.warnings[0]
    workspace = Path(report.warnings[0].rsplit(" at ", 1)[1])
    assert (workspace / "source").is_dir()
    assert (workspace / "original-source").is_dir()


@pytest.mark.parametrize(
    ("target", "retained_name"),
    [("source_snapshot", "source"), ("stage", "site")],
)
def test_post_commit_nested_injection_is_retained_instead_of_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    retained_name: str,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    if target == "stage":
        render_site(project / "blueprint", output, lean_root=project)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "PRECIOUS").write_text("keep\n", encoding="utf-8")
    original = render_module._publish_staged_site

    def inject_after_publish(*args, **kwargs):
        original(*args, **kwargs)
        root = Path(args[0]) if target == "stage" else Path(kwargs[target])
        destination = root / "injected-victim"
        victim.rename(destination)

    monkeypatch.setattr(render_module, "_publish_staged_site", inject_after_publish)

    report = render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).is_file()
    assert len(report.warnings) == 1
    assert "cleanup was refused" in report.warnings[0]
    workspace = Path(report.warnings[0].rsplit(" at ", 1)[1])
    assert (workspace / retained_name / "injected-victim/PRECIOUS").read_text(
        encoding="utf-8"
    ) == "keep\n"


def test_cleanup_claim_restores_a_replacement_injected_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    victim = tmp_path / "victim"
    victim.write_text("PRECIOUS\n", encoding="utf-8")
    original = render_module._read_regular_file_at
    injected = False

    def inject_after_read(parent_descriptor, name, display_path):
        nonlocal injected
        data = original(parent_descriptor, name, display_path)
        if ".autoform-cleanup-" in name and not injected:
            injected = True
            displaced = f"{name}.displaced"
            os.rename(name, displaced, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
            os.rename(victim, name, dst_dir_fd=parent_descriptor)
        return data

    monkeypatch.setattr(render_module, "_read_regular_file_at", inject_after_read)

    report = render_site(project / "blueprint", output, lean_root=project)

    assert injected
    assert len(report.warnings) == 1
    workspace = Path(report.warnings[0].rsplit(" at ", 1)[1])
    retained = [
        path
        for path in workspace.rglob("*")
        if path.is_file() and path.read_text(encoding="utf-8") == "PRECIOUS\n"
    ]
    assert len(retained) == 1


def test_render_cleans_up_a_workspace_containing_a_near_name_max_file(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    filename = "a" * 240 + ".txt"
    (project / "blueprint" / filename).write_text("large name\n", encoding="utf-8")
    output = tmp_path / "out"

    report = render_site(project / "blueprint", output, lean_root=project)

    assert report.warnings == []
    assert (output / filename).read_text(encoding="utf-8") == "large name\n"
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))


def test_source_change_during_stage_sync_aborts_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    article = project / "blueprint/roadmap/top.md"
    original = render_module._sync_tree

    def mutate_after_sync(stage):
        original(stage)
        article.write_text(article.read_text(encoding="utf-8") + "\nChanged during sync.\n")

    monkeypatch.setattr(render_module, "_sync_tree", mutate_after_sync)
    with pytest.raises(PublicationError, match="blueprint changed during publication"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not output.exists()


def test_workspace_path_substitution_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    moved = tmp_path / "owned-workspace-moved-aside"

    def substitute_workspace(blueprint_dir, *args, **kwargs):
        workspace = Path(blueprint_dir).parent
        workspace.rename(moved)
        workspace.mkdir()
        (workspace / "unrelated-user-data.txt").write_text("keep me\n", encoding="utf-8")
        raise RuntimeError("injected render failure")

    monkeypatch.setattr(render_module, "_render_snapshot", substitute_workspace)
    with pytest.raises(PublicationError, match="cleanup was refused"):
        render_site(project / "blueprint", output, lean_root=project)

    replacements = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))
    assert len(replacements) == 1
    assert (replacements[0] / "unrelated-user-data.txt").read_text() == "keep me\n"
    assert moved.is_dir()


def test_stage_substitution_before_publish_is_rejected_and_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    old_manifest = (output / PUBLICATION_MANIFEST).read_bytes()

    other_project = _project(tmp_path / "other")
    substitute = tmp_path / "substitute"
    render_site(other_project / "blueprint", substitute, lean_root=other_project)
    substitute_manifest = (substitute / PUBLICATION_MANIFEST).read_bytes()
    original = render_module._publish_staged_site

    def substitute_stage(stage, *args, **kwargs):
        displaced = stage.parent / "intended-stage"
        stage.rename(displaced)
        substitute.rename(stage)
        return original(stage, *args, **kwargs)

    monkeypatch.setattr(render_module, "_publish_staged_site", substitute_stage)
    with pytest.raises(PublicationError, match="stage changed"):
        render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).read_bytes() == old_manifest
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))
    assert len(workspaces) == 1
    assert (workspaces[0] / "site/publication.json").read_bytes() == substitute_manifest
    assert (workspaces[0] / "intended-stage/publication.json").is_file()


def test_pre_exchange_destination_substitution_retains_unverified_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    old_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    article.write_text(article.read_text(encoding="utf-8") + "\nNew generation.\n")

    other_project = _project(tmp_path / "other")
    substitute = tmp_path / "substitute"
    render_site(other_project / "blueprint", substitute, lean_root=other_project)
    substitute_manifest = (substitute / PUBLICATION_MANIFEST).read_bytes()
    original = render_module._rename_exchange
    exchanges = 0

    def substitute_before_exchange(source_parent, source, target_parent, target):
        nonlocal exchanges
        exchanges += 1
        if exchanges == 1:
            original(target_parent, substitute.name, target_parent, target)
        original(source_parent, source, target_parent, target)

    monkeypatch.setattr(render_module, "_rename_exchange", substitute_before_exchange)
    with pytest.raises(PublicationError, match="recovery material was retained"):
        render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).read_bytes() not in {
        old_manifest,
        substitute_manifest,
    }
    assert (substitute / PUBLICATION_MANIFEST).read_bytes() == old_manifest
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))
    assert len(workspaces) == 1
    assert (workspaces[0] / "site/publication.json").read_bytes() == substitute_manifest


def test_post_commit_verification_failure_retains_previous_site_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    before = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    article = project / "blueprint/roadmap/top.md"
    article.write_text(article.read_text(encoding="utf-8") + "\nNew generation.\n")
    original_inspect = render_module._inspect_destination_at
    original_exchange = render_module._rename_exchange
    destination_inspections = 0
    exchanges = 0

    def substitute_final_state(parent_descriptor, name, display_path):
        nonlocal destination_inspections
        state = original_inspect(parent_descriptor, name, display_path)
        if display_path == output:
            destination_inspections += 1
            if destination_inspections == 4:
                return render_module._DestinationState(
                    state.kind,
                    identity=state.identity,
                    manifest_sha256="0" * 64,
                    directories=state.directories,
                    files=state.files,
                )
        return state

    def track_exchange(*args):
        nonlocal exchanges
        exchanges += 1
        return original_exchange(*args)

    monkeypatch.setattr(render_module, "_inspect_destination_at", substitute_final_state)
    monkeypatch.setattr(render_module, "_rename_exchange", track_exchange)
    with pytest.raises(PublicationError, match="published generation changed"):
        render_site(project / "blueprint", output, lean_root=project)

    assert exchanges == 2
    after = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))


def test_interrupt_after_exchange_retains_previous_site_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    before_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    article.write_text(article.read_text(encoding="utf-8") + "\nNew generation.\n")

    original_exchange = render_module._rename_exchange

    def exchange_then_interrupt(*args):
        original_exchange(*args)
        raise KeyboardInterrupt("injected after exchange")

    monkeypatch.setattr(render_module, "_rename_exchange", exchange_then_interrupt)
    with pytest.raises(PublicationError, match="commit began"):
        render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).read_bytes() == before_manifest
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))
    assert len(workspaces) == 1
    assert (workspaces[0] / "site/publication.json").read_bytes() != before_manifest


def test_interrupt_after_first_install_retains_uncertain_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    original_noreplace = render_module._rename_noreplace

    def install_then_interrupt(*args):
        original_noreplace(*args)
        raise KeyboardInterrupt("injected after install")

    monkeypatch.setattr(render_module, "_rename_noreplace", install_then_interrupt)
    with pytest.raises(PublicationError, match="commit began"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not output.exists()
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))
    assert len(workspaces) == 1
    assert (workspaces[0] / "source").is_dir()
    assert (workspaces[0] / "site/publication.json").is_file()


def test_descriptor_close_failure_after_exchange_retains_previous_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    before_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    article.write_text(article.read_text(encoding="utf-8") + "\nNew generation.\n")

    original_exchange = render_module._rename_exchange
    original_close = render_module.os.close
    exchanged = False
    failed_close = False

    def track_exchange(*args):
        nonlocal exchanged
        original_exchange(*args)
        exchanged = True

    def fail_first_close_after_exchange(descriptor):
        nonlocal failed_close
        original_close(descriptor)
        if exchanged and not failed_close:
            failed_close = True
            raise OSError("injected descriptor close failure")

    monkeypatch.setattr(render_module, "_rename_exchange", track_exchange)
    monkeypatch.setattr(render_module.os, "close", fail_first_close_after_exchange)
    with pytest.raises(PublicationError, match="publication output changed"):
        render_site(project / "blueprint", output, lean_root=project)

    assert failed_close
    assert (output / PUBLICATION_MANIFEST).read_bytes() == before_manifest
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))


def test_post_commit_destination_change_does_not_trigger_a_second_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    before_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    article.write_text(article.read_text(encoding="utf-8") + "\nIntended generation.\n")

    other_project = _project(tmp_path / "other")
    other_article = other_project / "blueprint/roadmap/top.md"
    other_article.write_text(other_article.read_text(encoding="utf-8") + "\nSubstitute.\n")
    substitute = tmp_path / "substitute"
    render_site(other_project / "blueprint", substitute, lean_root=other_project)
    substitute_manifest = (substitute / PUBLICATION_MANIFEST).read_bytes()

    original_exchange = render_module._rename_exchange
    exchanges = 0

    def exchange_then_substitute(source_parent, source, target_parent, target):
        nonlocal exchanges
        exchanges += 1
        original_exchange(source_parent, source, target_parent, target)
        if exchanges == 1:
            original_exchange(target_parent, substitute.name, target_parent, target)

    monkeypatch.setattr(render_module, "_rename_exchange", exchange_then_substitute)
    with pytest.raises(PublicationError, match="recovery material was retained"):
        render_site(project / "blueprint", output, lean_root=project)

    assert exchanges == 1
    assert (output / PUBLICATION_MANIFEST).read_bytes() == substitute_manifest
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))
    assert len(workspaces) == 1
    assert (workspaces[0] / "site/publication.json").read_bytes() == before_manifest
    assert (substitute / PUBLICATION_MANIFEST).read_bytes() not in {
        before_manifest,
        substitute_manifest,
    }


def test_source_change_during_exchange_restores_previous_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    old_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    article.write_text(article.read_text(encoding="utf-8") + "\nSecond generation.\n")
    original_exchange = render_module._rename_exchange
    changed = False

    def change_source_during_exchange(*args, **kwargs):
        nonlocal changed
        original_exchange(*args, **kwargs)
        if not changed:
            changed = True
            article.write_text(
                article.read_text(encoding="utf-8") + "\nChanged during commit.\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(render_module, "_rename_exchange", change_source_during_exchange)

    with pytest.raises(PublicationError, match="blueprint changed during publication"):
        render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).read_bytes() == old_manifest
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))


def test_post_commit_stage_change_never_reenters_live_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    old_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    article.write_text(article.read_text(encoding="utf-8") + "\nIntended generation.\n")

    other_project = _project(tmp_path / "other")
    other_article = other_project / "blueprint/roadmap/top.md"
    other_article.write_text(other_article.read_text(encoding="utf-8") + "\nAttacker.\n")
    substitute = tmp_path / "substitute"
    render_site(other_project / "blueprint", substitute, lean_root=other_project)
    (substitute / "attacker.txt").write_text("must not publish\n", encoding="utf-8")
    substitute_manifest = (substitute / PUBLICATION_MANIFEST).read_bytes()

    original_exchange = render_module._rename_exchange
    exchanges = 0

    def exchange_then_substitute_stage(source_parent, source, target_parent, target):
        nonlocal exchanges
        exchanges += 1
        original_exchange(source_parent, source, target_parent, target)
        if exchanges == 1:
            workspace = next(
                tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*")
            )
            recovery_stage = workspace / "site"
            displaced = workspace / "expected-old-generation"
            recovery_stage.rename(displaced)
            substitute.rename(recovery_stage)

    monkeypatch.setattr(
        render_module,
        "_rename_exchange",
        exchange_then_substitute_stage,
    )
    with pytest.raises(PublicationError, match="recovery material was retained"):
        render_site(project / "blueprint", output, lean_root=project)

    assert exchanges == 1
    assert (output / PUBLICATION_MANIFEST).read_bytes() not in {
        old_manifest,
        substitute_manifest,
    }
    assert not (output / "attacker.txt").exists()


def test_in_repo_staging_never_supplies_lean_source_links(tmp_path: Path) -> None:
    project = _project(tmp_path)
    proof = project / "blueprint/proofs.lean"
    proof.write_text("theorem BlueprintProof : True := trivial\n", encoding="utf-8")
    article = project / "blueprint/roadmap/top.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace("lean: Project.top", "lean: BlueprintProof"),
        encoding="utf-8",
    )

    links = []
    for name in ("aaa-output", "zzz-output"):
        output = project / name
        render_site(
            project / "blueprint",
            output,
            lean_root=project,
            repository_url="https://github.com/owner/repo",
            ref="abc",
        )
        page = (output / "roadmap/README.md").read_text(encoding="utf-8")
        match = re.search(r"https://github.com/owner/repo/blob/abc/[^)]+proofs\.lean#L1", page)
        assert match is not None
        links.append(match.group())

    assert links == [
        "https://github.com/owner/repo/blob/abc/blueprint/proofs.lean#L1",
        "https://github.com/owner/repo/blob/abc/blueprint/proofs.lean#L1",
    ]


def test_render_fsyncs_staged_files_and_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fsync
    synced_modes: list[int] = []

    def record(descriptor: int) -> None:
        synced_modes.append(os.fstat(descriptor).st_mode)
        original(descriptor)

    monkeypatch.setattr(render_module.os, "fsync", record)
    _render(tmp_path)

    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)


def test_publish_fsyncs_both_directories_after_cross_directory_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_rename = render_module._rename_noreplace
    original_sync = os.fsync
    renamed = False
    directory_identities: list[tuple[int, int]] = []

    def rename(*args) -> None:
        nonlocal renamed
        original_rename(*args)
        renamed = True

    def record(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if renamed and stat.S_ISDIR(metadata.st_mode):
            directory_identities.append((metadata.st_dev, metadata.st_ino))
        original_sync(descriptor)

    monkeypatch.setattr(render_module, "_rename_noreplace", rename)
    monkeypatch.setattr(render_module.os, "fsync", record)
    _render(tmp_path)

    assert len(set(directory_identities)) >= 2


def test_failed_stage_inspection_does_not_leak_file_descriptors(tmp_path: Path) -> None:
    descriptor_root = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        pytest.skip("process file descriptors are not inspectable")
    stage = tmp_path / "workspace/site"
    stage.mkdir(parents=True)
    destination = tmp_path / "out"
    expected = render_module._DestinationState("absent")
    before = len(list(descriptor_root.iterdir()))

    for _ in range(40):
        with pytest.raises(PublicationError, match="stage changed"):
            render_module._publish_staged_site(
                stage,
                destination,
                expected,
                render_module._DestinationState("owned"),
                commit_state=render_module._PublicationCommitState(),
                source_blueprint=tmp_path,
                source_snapshot=tmp_path,
                source_snapshot_identity=render_module._directory_path_identity(tmp_path),
                source_revision="0" * 64,
                lean_root=tmp_path,
                lean_source_revision="0" * 64,
                lean_exclusions=(),
            )

    assert len(list(descriptor_root.iterdir())) <= before + 1


def test_unsupported_platform_fails_before_creating_a_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(render_module, "fcntl", None)

    with pytest.raises(PublicationError, match="unavailable on this platform"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))


def test_concurrent_renders_publish_one_generation_without_leaking_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    barrier = threading.Barrier(2)
    original = render_module._publish_staged_site

    def publish_together(*args, **kwargs):
        barrier.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(render_module, "_publish_staged_site", publish_together)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(render_site, project / "blueprint", output, lean_root=project)
            for _ in range(2)
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as error:
                outcomes.append(error)

    assert sum(isinstance(outcome, render_module.RenderReport) for outcome in outcomes) == 1
    failures = [outcome for outcome in outcomes if isinstance(outcome, PublicationError)]
    assert len(failures) == 1
    assert "output directory changed during publication" in str(failures[0])
    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["complete"]
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}out-*"))


def test_non_clean_render_preserves_only_verified_prior_files(tmp_path: Path) -> None:
    project = _project(tmp_path)
    source = project / "blueprint/appendix.txt"
    source.write_text("generated companion asset\n", encoding="utf-8")
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    source.unlink()

    render_site(project / "blueprint", output, lean_root=project, clean=False)

    assert (output / "appendix.txt").read_text(encoding="utf-8") == "generated companion asset\n"
    manifest = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert "appendix.txt" in manifest["files"]


def test_render_refuses_to_overwrite_an_unowned_directory(tmp_path: Path) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("user data\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="non-Autoform output directory"):
        render_site(project / "blueprint", output, lean_root=project)

    assert sentinel.read_text(encoding="utf-8") == "user data\n"


def test_render_refuses_an_output_symlink(tmp_path: Path) -> None:
    project = _project(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("user data\n", encoding="utf-8")
    output = tmp_path / "out"
    output.symlink_to(target, target_is_directory=True)

    with pytest.raises(PublicationError, match="symlink output directory"):
        render_site(project / "blueprint", output, lean_root=project)

    assert sentinel.read_text(encoding="utf-8") == "user data\n"


def test_render_rejects_symlinks_before_cleaning_an_existing_site(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "private.md"
    outside.write_text("secret\n", encoding="utf-8")
    (project / "blueprint" / "linked.md").symlink_to(outside)
    output = tmp_path / "out"
    output.mkdir()
    sentinel = output / "existing.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="refusing symlink.*linked.md"):
        render_site(project / "blueprint", output, lean_root=project)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_render_reports_visible_special_file_before_publication(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable")
    project = _project(tmp_path)
    fifo = project / "blueprint/roadmap/trap.md"
    os.mkfifo(fifo)
    output = tmp_path / "out"

    with pytest.raises(PublicationError, match=r"trap\.md: named pipe"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not output.exists()


@pytest.mark.parametrize(
    "relative",
    ("task_queue.json", ".autoform/agents_status.json", "sources/dispatcher.log", ".env.local"),
)
def test_render_rejects_operational_or_sensitive_inputs(
    tmp_path: Path, relative: str
) -> None:
    project = _project(tmp_path)
    local = project / "blueprint" / relative
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("private\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="local or sensitive.*" + re.escape(relative)):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)


def test_render_omits_benign_hidden_files(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/.gitignore").write_text("site/\n", encoding="utf-8")

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert not (tmp_path / "out/.gitignore").exists()


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:owner/repo.git", "https://github.com/owner/repo"),
        ("https://github.com/owner/repo.git", "https://github.com/owner/repo"),
        ("https://github.com/owner/repo/", "https://github.com/owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "https://github.com/owner/repo"),
        ("/local/path", None),
    ],
)
def test_git_remotes_normalize_to_web_urls(remote: str, expected: str | None) -> None:
    assert _normalize_remote(remote) == expected


def _with_source_notes(tmp_path: Path) -> Path:
    """A project whose `## Sources` list cites a note under `blueprint/sources`."""
    project = _project(tmp_path)
    sources = project / "blueprint" / "sources"
    sources.mkdir()
    (sources / "paper.md").write_text(
        "---\n---\n\n# Paper\n\nTranscribed statements from the paper.\n", encoding="utf-8"
    )
    (project / "blueprint" / "roadmap" / "top.md").write_text(
        "---\ndeclaration: theorem\nstatement: formalized\nproof: formalized\n"
        "lean: Project.top\n---\n\n"
        "# Top\n\nThe main result.\n\n## Sources\n\n"
        "- [Paper](../sources/paper.md#lemma-3)\n\n"
        "## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    return project


def test_source_notes_leave_the_site_for_the_repository(tmp_path: Path) -> None:
    """Publishing them put the same transcription at a URL nothing links to.

    A `## Sources` entry names the paper the statement came from. Rendering
    that note as a site page gave the book a third surface, neither chapter nor
    paper, that every statement pointed at. The note stays in the vault and the
    site links to it in the repository.
    """
    project = _with_source_notes(tmp_path)
    out = tmp_path / "out"

    render_site(
        project / "blueprint",
        out,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref="cafe1234",
    )

    assert not (out / "sources").exists()
    expected = "https://github.com/owner/repo/blob/cafe1234/blueprint/sources/paper.md#lemma-3"
    assert expected in (out / "roadmap/README.md").read_text(encoding="utf-8")


def test_legacy_source_note_alias_uses_its_physical_repository_path(
    tmp_path: Path,
) -> None:
    project = _with_source_notes(tmp_path)
    canonical = project / "blueprint/sources"
    canonical.rename(project / "blueprint/Sources")
    if not canonical.exists():
        pytest.skip("filesystem is case-sensitive")
    out = tmp_path / "out"

    render_site(
        project / "blueprint",
        out,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref="cafe1234",
    )

    expected = "https://github.com/owner/repo/blob/cafe1234/blueprint/Sources/paper.md#lemma-3"
    assert expected in (out / "roadmap/README.md").read_text(encoding="utf-8")
    assert not (out / "Sources").exists()
    assert not (out / "sources").exists()


def test_source_notes_stay_published_when_there_is_nowhere_to_send_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without repository coordinates, dropping the pages would strand the links."""
    # An empty argument falls through to detection, and detection reads the
    # Actions environment, so on CI this test would otherwise be handed the
    # coordinates it is meant to be doing without.
    for variable in ("GITHUB_REPOSITORY", "GITHUB_SERVER_URL", "GITHUB_SHA"):
        monkeypatch.delenv(variable, raising=False)
    project = _with_source_notes(tmp_path)
    out = tmp_path / "out"

    render_site(project / "blueprint", out, lean_root=project, repository_url="", ref="")

    assert (out / "sources/paper.md").is_file()
    assert "github.com" not in (out / "roadmap/README.md").read_text(encoding="utf-8")


def test_permalinks_are_relative_to_the_repository_not_the_vaults_parent(
    tmp_path: Path,
) -> None:
    """A blueprint at <repo>/docs/blueprint was described as <repo>/blueprint.

    Every generated permalink dropped the intermediate directory and 404'd.
    """
    repo = tmp_path / "repo"
    project = repo / "docs"
    project.mkdir(parents=True)
    inner = _project(project)  # writes into <repo>/docs/project/blueprint
    out = tmp_path / "out"

    render_site(
        inner / "blueprint",
        out,
        lean_root=repo,
        repository_url="https://github.com/owner/repo",
        ref="cafe1234",
    )

    chapter = (out / "roadmap/README.md").read_text(encoding="utf-8")
    assert "blob/cafe1234/docs/project/blueprint/roadmap/top.md" in chapter
    assert "blob/cafe1234/blueprint/roadmap/top.md" not in chapter


def test_reference_style_links_are_rewritten_with_the_inline_ones(tmp_path: Path) -> None:
    """`[Paper][paper]` resolves through a definition the rewrite never saw.

    Only inline links were rewritten, so the definition kept naming a source
    page that is no longer published and the rendered link dangled. Placed on
    the chapter page, which is published as authored.
    """
    project = _project(tmp_path)
    sources = project / "blueprint" / "sources"
    sources.mkdir()
    (sources / "paper.md").write_text("---\n---\n\n# Paper\n", encoding="utf-8")
    roadmap = project / "blueprint" / "roadmap" / "README.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8") + "\nGrounded in [Paper][paper].\n\n"
        "[paper]: ../sources/paper.md\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    render_site(
        project / "blueprint",
        out,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref="cafe1234",
    )

    chapter = (out / "roadmap/README.md").read_text(encoding="utf-8")
    expected = "[paper]: https://github.com/owner/repo/blob/cafe1234/blueprint/sources/paper.md"
    assert expected in chapter
    assert "[paper]: ../sources/paper.md" not in chapter


def test_angle_bracket_reference_destinations_with_spaces_are_rewritten(tmp_path: Path) -> None:
    """An angle-bracket destination may contain spaces and must stay whole."""
    project = _project(tmp_path)
    sources = project / "blueprint" / "sources"
    sources.mkdir()
    (sources / "paper note.md").write_text("---\n---\n\n# Paper\n", encoding="utf-8")
    roadmap = project / "blueprint" / "roadmap" / "README.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8")
        + '\nGrounded in [Paper][paper].\n\n[paper]: <../sources/paper note.md> "Source note"\n',
        encoding="utf-8",
    )
    out = tmp_path / "out"

    render_site(
        project / "blueprint",
        out,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref="cafe1234",
    )

    chapter = (out / "roadmap/README.md").read_text(encoding="utf-8")
    assert (
        '[paper]: https://github.com/owner/repo/blob/cafe1234/blueprint/sources/paper%20note.md "Source note"'
        in chapter
    )
    assert "<../sources/paper note.md>" not in chapter


def test_a_fresh_vault_reports_no_work_rather_than_one_ready_item(tmp_path: Path) -> None:
    """The roadmap landing page is not a formalization target.

    Counting every childless article made a freshly scaffolded vault claim
    "0 of 1 items settled, 1 ready now", so the site described work before any
    had been planned.
    """
    from autoform_cli.scaffold import scaffold_project

    project = tmp_path / "project"
    scaffold_project(project, title="Empty", discover_plugin_pin=False)
    out = tmp_path / "out"

    render_site(project / "blueprint", out)

    overview = (out / "README.md").read_text(encoding="utf-8")
    assert "0 of 0 items settled" in overview
    assert '<div class="bp-figure-value">0</div>' in overview


def test_a_directory_link_uses_tree_even_when_the_repo_url_says_blob() -> None:
    """Deriving the directory URL by replacing the first `/blob/` rewrote the
    repository's own path when that happened to contain one."""
    from autoform_cli.render import _SourceBase

    base = _SourceBase("https://git.example/blob/x/repo", "abc", "blueprint/sources")

    assert base.href(("paper.md",)) == (
        "https://git.example/blob/x/repo/blob/abc/blueprint/sources/paper.md"
    )
    assert base.href(()) == "https://git.example/blob/x/repo/tree/abc/blueprint/sources"
