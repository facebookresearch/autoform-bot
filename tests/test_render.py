from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autoform_cli.lean import _normalize_remote
from autoform_cli.render import PUBLICATION_MANIFEST, PublicationError, render_site
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
        "git_ref": "a" * 40,
        "nodes": 3,
        "schema": "autoform-publication/v1",
        "source": "blueprint/roadmap Markdown",
        "source_revision": manifest["source_revision"],
        "views": ["book", "progress", "project", "chapter", "focus", "full"],
    }
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["source_revision"])
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


def test_render_cleans_only_an_owned_publication(tmp_path: Path) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    stale = output / "stale.txt"
    stale.write_text("old generated output\n", encoding="utf-8")

    render_site(project / "blueprint", output, lean_root=project)

    assert not stale.exists()
    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["complete"]


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
    scaffold_project(project, title="Empty")
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
