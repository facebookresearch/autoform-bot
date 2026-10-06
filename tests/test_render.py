from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import pytest

import autoform_cli._tree_snapshot as tree_snapshot_module
import autoform_cli.render as render_module
from autoform_cli._tree_snapshot import TreeCaptureLimits, TreeSelection
from autoform_cli.coverage import COVERAGE_DISPOSITIONS
from autoform_cli.graph import load_graph
from autoform_cli.lean import LeanSourceError, _normalize_remote
from autoform_cli.render import (
    PUBLICATION_MANIFEST,
    PublicationError,
    _COVERAGE_SUMMARY_ORDER,
    _completion_percentage,
    render_site,
)
from autoform_cli.status import STATES, derive


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


def _commit_project(repository: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Autoform Test",
            "-c",
            "user.email=autoform@example.invalid",
            "commit",
            "-q",
            "--no-gpg-sign",
            "-m",
            "test fixture",
        ],
        cwd=repository,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_head(repository: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _render(tmp_path: Path, **kwargs):
    project = _project(tmp_path)
    ref = _commit_project(project)
    report = render_site(
        project / "blueprint",
        tmp_path / "out",
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref=ref,
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
    assert (out / "javascripts/blueprint-live.js").is_file()
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
    assert 'data-autoform-node-id="base"' in page
    assert 'data-autoform-node-id="top"' in page
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
    project, _report = _render(tmp_path)
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")

    ref = _git_head(project)
    assert f"https://github.com/owner/repo/blob/{ref}/Project/Basic.lean#L5" in page
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
    assert '<div class="bp-figure-label">Scoped roadmap</div>' in overview
    assert "2 of 2 targets complete" in overview
    assert "Declared source coverage:" in overview
    assert "1 mapped" in overview
    assert 'href="coverage/index.html"' in overview
    assert '<div class="bp-hero-bar" aria-hidden="true">' in overview
    assert 'role="img"' not in overview
    assert "items settled" not in overview
    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert "1 definition · 1 result" in chapter
    # Progress stays on the landing page; the only summary link explains scope.
    assert "bp-progress-link" not in overview
    assert not (tmp_path / "out/progress.md").exists()


def test_statement_only_theorems_never_count_as_complete(tmp_path: Path) -> None:
    project = _project(tmp_path)
    roadmap = project / "blueprint/roadmap"
    (project / "Project" / "Blocker.lean").write_text(
        "namespace Project\n\ntheorem blocker : True := trivial\n\nend Project\n", encoding="utf-8"
    )
    blocker = roadmap / "blocker.md"
    blocker.write_text(
        "---\ndeclaration: theorem\nstatement: formalized\nlean: Project.blocker\n---\n\n# Blocker\n",
        encoding="utf-8",
    )
    top = roadmap / "top.md"
    top.write_text(
        "---\ndeclaration: theorem\nstatement: formalized\nlean: Project.top\n---\n\n"
        "# Top\n\nThe main result.\n\n"
        "## Depends on\n\n- [Base](base.md)\n\n"
        "## Proof depends on\n\n- [Blocker](blocker.md)\n",
        encoding="utf-8",
    )

    statuses = derive(load_graph(project / "blueprint"))
    assert statuses["blocker"].key == "can_prove"
    assert statuses["top"].key == "stated"

    render_site(project / "blueprint", tmp_path / "blocked", lean_root=project)
    blocked = (tmp_path / "blocked/README.md").read_text(encoding="utf-8")
    assert "1 of 3 targets complete" in blocked

    blocker.write_text(
        "---\ndeclaration: theorem\nstatement: formalized\nproof: formalized\nlean: Project.blocker\n---\n\n"
        "# Blocker\n",
        encoding="utf-8",
    )
    statuses = derive(load_graph(project / "blueprint"))
    assert statuses["top"].key == "can_prove"

    render_site(project / "blueprint", tmp_path / "ready", lean_root=project)
    ready = (tmp_path / "ready/README.md").read_text(encoding="utf-8")
    assert "2 of 3 targets complete" in ready


def test_completion_requires_every_dependency_to_be_fully_proved(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/base.md").write_text(
        "---\n---\n\n# Base\n\nAn unfinished prerequisite.\n",
        encoding="utf-8",
    )

    statuses = derive(load_graph(project / "blueprint"))
    assert statuses["top"].proved
    assert not statuses["top"].fully_proved

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")
    assert "0 of 1 target complete" in overview
    assert '<div class="bp-figure-value">0%</div>' in overview


def test_definitions_and_mathlib_marked_targets_count_as_complete(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/upstream.md").write_text(
        "---\ndeclaration: theorem\nmathlib: true\n"
        "mathlib_declaration: Nat.add_comm\n"
        "mathlib_file: Mathlib/Data/Nat/Basic.lean\n"
        "---\n\n# Upstream\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")
    assert "3 of 3 targets complete" in overview
    assert '<div class="bp-figure-value">100%</div>' in overview


def test_completion_percentage_reserves_100_for_complete_work() -> None:
    assert _completion_percentage(0, 200) == 0
    assert _completion_percentage(1, 200) == 1
    assert _completion_percentage(199, 200) == 99
    assert _completion_percentage(200, 200) == 100
    assert _completion_percentage(0, 0) == 0


def test_coverage_summary_tracks_every_canonical_disposition() -> None:
    assert set(_COVERAGE_SUMMARY_ORDER) == set(COVERAGE_DISPOSITIONS)
    assert len(_COVERAGE_SUMMARY_ORDER) == len(COVERAGE_DISPOSITIONS)
    assert _COVERAGE_SUMMARY_ORDER[0] == "DECOMPOSED"


def test_single_target_uses_singular_completion_copy(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/top.md").write_text(
        "---\n---\n\n# Top\n\nNarrative context rather than a Lean target.\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")
    assert "1 of 1 target complete" in overview
    assert "1 of 1 targets complete" not in overview


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
    """Dropping the authored body must not strand the coverage contract.

    It is not a chapter, so it needs explicit routes from both the hero's scope
    summary and the generated Book navigation.
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
    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")
    assert "[Coverage](coverage/README.md)" in nav
    assert 'href="coverage/index.html"' in overview


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
    live = (tmp_path / "out/javascripts/blueprint-live.js").read_text(encoding="utf-8")
    packaged_live = (
        Path(__file__).resolve().parent.parent / "autoform_cli/assets/blueprint-live.js"
    ).read_text(encoding="utf-8")

    # Facebook's surface greys and Meta blue, not Material's defaults, and both
    # schemes hang off the theme's own data-md-color-scheme attribute.
    assert "Plus Jakarta Sans" in css and "JetBrains Mono" in css
    assert "--bp-link: #0064E0" in css
    assert "[data-md-color-scheme=slate]" in css
    assert "--bp-link: #2D88FF" in css
    assert "--bp-surface: #242526" in css
    assert "background-color: #18191A" in css
    assert "/__autoform/live.json" in live
    assert 'window.location.hostname !== "127.0.0.1"' in live
    assert "data-autoform-node-id" in live
    assert "setInterval" not in live
    assert "setTimeout(refresh, pollDelayMs)" in live
    assert 'render({ claims: [], error: "Live overlay unavailable" })' in live
    assert live == packaged_live
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


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        (None, "Lean sources could not be indexed"),
        (
            "permission denied: Project/Secret.lean",
            "Lean sources could not be indexed: permission denied: Project/Secret.lean",
        ),
    ],
)
def test_render_translates_source_index_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str | None, message: str
) -> None:
    project = _project(tmp_path)

    def fail_linker(root: Path, **kwargs: object):
        if reason is not None:
            raise LeanSourceError(reason)
        raise OSError(f"private host detail: {root}")

    monkeypatch.setattr("autoform_cli.render.build_linker", fail_linker)

    with pytest.raises(PublicationError) as error:
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert error.value.issues == (message,)
    assert str(tmp_path) not in str(error.value)


def test_render_is_deterministic_and_records_a_path_free_manifest(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ref = _commit_project(project)
    outputs = [tmp_path / "first", tmp_path / "second"]
    for output in outputs:
        render_site(
            project / "blueprint",
            output,
            lean_root=project,
            repository_url="https://github.com/owner/repo",
            ref=ref,
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
        "git_ref": ref,
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


def test_render_rejects_a_v2_publication_without_a_lean_revision(tmp_path: Path) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    manifest_path = output / PUBLICATION_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("lean_source_revision")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    before = manifest_path.read_bytes()
    with pytest.raises(PublicationError, match="invalid source revisions"):
        render_site(project / "blueprint", output, lean_root=project)

    assert manifest_path.read_bytes() == before


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


def test_failed_plan_build_preserves_the_previous_site(
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
    with pytest.raises(RuntimeError, match="injected render failure"):
        render_site(project / "blueprint", output, lean_root=project)

    after = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))


def test_source_change_during_render_aborts_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    old_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    original = render_module._build_publication_plan

    def mutate_after_render(*args, **kwargs):
        report = original(*args, **kwargs)
        article.write_text(article.read_text(encoding="utf-8") + "\nChanged concurrently.\n")
        return report

    monkeypatch.setattr(render_module, "_build_publication_plan", mutate_after_render)
    with pytest.raises(PublicationError, match="blueprint changed during publication"):
        render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).read_bytes() == old_manifest
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))


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

    first_snapshot = render_module._CapturedBlueprint(
        first,
        {PurePosixPath("a"): b"X\0b\0Y"},
        frozenset({PurePosixPath(".")}),
    )
    second_snapshot = render_module._CapturedBlueprint(
        second,
        {PurePosixPath("a"): b"X", PurePosixPath("b"): b"Y"},
        frozenset({PurePosixPath(".")}),
    )
    empty_directory_snapshot = render_module._CapturedBlueprint(
        first,
        {PurePosixPath("a"): b"X\0b\0Y"},
        frozenset({PurePosixPath("."), PurePosixPath("sources")}),
    )

    assert render_module._source_revision(first_snapshot) != render_module._source_revision(
        second_snapshot
    )
    assert render_module._source_revision(
        first_snapshot
    ) != render_module._source_revision(empty_directory_snapshot)


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


def test_source_snapshot_is_never_materialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"

    def fail_materialize(*args, **kwargs):
        raise AssertionError("captured source must stay in memory")

    monkeypatch.setattr(tree_snapshot_module.TreeSnapshot, "materialize", fail_materialize)

    render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).is_file()
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))


def test_temporary_source_directory_substitution_never_enters_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    other = _project(tmp_path / "other")
    foreign_article = other / "blueprint/roadmap/top.md"
    foreign_article.write_text("# FOREIGN BYTES\n", encoding="utf-8")
    foreign_before = foreign_article.read_bytes()
    blueprint = project / "blueprint"
    foreign_blueprint = other / "blueprint"
    held = tmp_path / "held-blueprint"
    output = tmp_path / "out"
    original = render_module._build_publication_plan

    def substitute_source_temporarily(*args, **kwargs):
        blueprint.rename(held)
        foreign_blueprint.rename(blueprint)
        try:
            plan, report = original(*args, **kwargs)
            assert all(b"FOREIGN BYTES" not in data for data in plan.files.values())
            return plan, report
        finally:
            blueprint.rename(foreign_blueprint)
            held.rename(blueprint)

    monkeypatch.setattr(
        render_module,
        "_build_publication_plan",
        substitute_source_temporarily,
    )
    render_site(project / "blueprint", output, lean_root=project)

    assert b"FOREIGN BYTES" not in (output / "roadmap/README.md").read_bytes()
    assert foreign_article.read_bytes() == foreign_before


def test_source_change_during_stage_sync_aborts_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    article = project / "blueprint/roadmap/top.md"
    original = render_module._sync_tree_descriptor

    def mutate_after_sync(stage_descriptor):
        original(stage_descriptor)
        article.write_text(article.read_text(encoding="utf-8") + "\nChanged during sync.\n")

    monkeypatch.setattr(render_module, "_sync_tree_descriptor", mutate_after_sync)
    with pytest.raises(PublicationError, match="blueprint changed during publication"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not output.exists()


def test_workspace_open_failure_removes_the_empty_private_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)

    def fail_open(*_args, **_kwargs):
        raise OSError(errno.EMFILE, "injected descriptor exhaustion")

    monkeypatch.setattr(render_module, "_open_workspace_directory", fail_open)

    with pytest.raises(OSError, match="descriptor exhaustion"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))


def test_partial_stage_write_failure_removes_the_inventoried_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    original_write = render_module.os.write
    failed = False

    def fail_after_partial_write(descriptor: int, data) -> int:
        nonlocal failed
        if not failed and data:
            failed = True
            original_write(descriptor, data[:1])
            raise OSError(errno.ENOSPC, "injected full filesystem")
        return original_write(descriptor, data)

    monkeypatch.setattr(render_module.os, "write", fail_after_partial_write)

    with pytest.raises(OSError, match="full filesystem"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert failed
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))


def test_workspace_path_substitution_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    moved = tmp_path / "owned-workspace-moved-aside"

    def substitute_workspace(*args, **kwargs):
        workspace = next(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
        workspace.rename(moved)
        workspace.mkdir()
        (workspace / "unrelated-user-data.txt").write_text("keep me\n", encoding="utf-8")
        raise RuntimeError("injected render failure")

    monkeypatch.setattr(render_module, "_build_publication_plan", substitute_workspace)
    with pytest.raises(PublicationError, match="cleanup was refused"):
        render_site(project / "blueprint", output, lean_root=project)

    replacements = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
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
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
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
    with pytest.raises(PublicationError, match="workspace was retained"):
        render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).read_bytes() not in {
        old_manifest,
        substitute_manifest,
    }
    assert (substitute / PUBLICATION_MANIFEST).read_bytes() == old_manifest
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
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
    with pytest.raises(PublicationError, match="workspace was retained"):
        render_site(project / "blueprint", output, lean_root=project)

    assert exchanges == 1
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
    assert len(workspaces) == 1
    recovered = {
        path.relative_to(workspaces[0] / "site").as_posix(): path.read_bytes()
        for path in (workspaces[0] / "site").rglob("*")
        if path.is_file()
    }
    assert recovered == before


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

    assert (output / PUBLICATION_MANIFEST).read_bytes() != before_manifest
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
    assert len(workspaces) == 1
    assert (workspaces[0] / "site/publication.json").read_bytes() == before_manifest


@pytest.mark.skipif(os.name == "nt", reason="transactional render is capability-gated")
def test_process_exit_after_exchange_retains_both_complete_generations(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(
        project / "blueprint",
        output,
        lean_root=project,
        repository_url="",
        ref="",
    )
    before_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\nA new publication generation.\n",
        encoding="utf-8",
    )

    script = """
import os
import sys
import autoform_cli.render as render_module

def crash_after_commit(_event: str) -> None:
    os._exit(86)

render_module._publication_commit_checkpoint = crash_after_commit
render_module.render_site(
    sys.argv[1],
    sys.argv[2],
    lean_root=sys.argv[3],
    repository_url="",
    ref="",
)
"""
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(project / "blueprint"),
            str(output),
            str(project),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert crashed.returncode == 86, crashed.stderr
    after_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    assert after_manifest != before_manifest
    assert json.loads(after_manifest)["complete"] is True
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
    assert len(workspaces) == 1
    assert (workspaces[0] / "site" / PUBLICATION_MANIFEST).read_bytes() == before_manifest


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
    with pytest.raises(PublicationError, match="commit began") as error:
        render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).is_file()
    assert "output may have changed" in str(error.value)
    assert "previous site" not in str(error.value)
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
    assert len(workspaces) == 1
    assert {path.name for path in workspaces[0].iterdir()} == {
        render_module._WORKSPACE_MARKER
    }


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
    with pytest.raises(PublicationError, match="commit began"):
        render_site(project / "blueprint", output, lean_root=project)

    assert failed_close
    assert (output / PUBLICATION_MANIFEST).read_bytes() != before_manifest
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
    assert len(workspaces) == 1
    assert (workspaces[0] / "site/publication.json").read_bytes() == before_manifest


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
    with pytest.raises(PublicationError, match="workspace was retained"):
        render_site(project / "blueprint", output, lean_root=project)

    assert exchanges == 1
    assert (output / PUBLICATION_MANIFEST).read_bytes() == substitute_manifest
    workspaces = list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
    assert len(workspaces) == 1
    assert (workspaces[0] / "site/publication.json").read_bytes() == before_manifest
    assert (substitute / PUBLICATION_MANIFEST).read_bytes() not in {
        before_manifest,
        substitute_manifest,
    }


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
                tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*")
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
    with pytest.raises(PublicationError, match="workspace was retained"):
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
    ref = _commit_project(project)

    links = []
    for name in ("aaa-output", "zzz-output"):
        output = project / name
        render_site(
            project / "blueprint",
            output,
            lean_root=project,
            repository_url="https://github.com/owner/repo",
            ref=ref,
        )
        page = (output / "roadmap/README.md").read_text(encoding="utf-8")
        match = re.search(
            rf"https://github.com/owner/repo/blob/{ref}/[^)]+proofs\.lean#L1",
            page,
        )
        assert match is not None
        links.append(match.group())

    assert links == [
        f"https://github.com/owner/repo/blob/{ref}/blueprint/proofs.lean#L1",
        f"https://github.com/owner/repo/blob/{ref}/blueprint/proofs.lean#L1",
    ]


def test_render_supplies_its_publication_marker_to_source_capture(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    prior = project / "prior-site"
    prior.mkdir()
    (prior / PUBLICATION_MANIFEST).write_text(
        json.dumps(
            {
                "schema": "autoform-publication/v1",
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    (prior / "Copied.lean").write_text(
        "def generatedOnly : Nat := 0\n",
        encoding="utf-8",
    )
    incomplete = project / "incomplete-site"
    incomplete.mkdir()
    (incomplete / PUBLICATION_MANIFEST).write_text(
        json.dumps(
            {
                "schema": render_module.PUBLICATION_SCHEMA,
                "complete": False,
            }
        ),
        encoding="utf-8",
    )
    (incomplete / "Visible.lean").write_text(
        "def incompleteOutputSource : Nat := 0\n",
        encoding="utf-8",
    )
    recovery = project / ".autoform-publication-recovery"
    (recovery / "site").mkdir(parents=True)
    (recovery / render_module._WORKSPACE_MARKER).write_bytes(
        render_module._WORKSPACE_MARKER_BYTES
    )
    (recovery / "site/Leaked.lean").write_text(
        "def retainedRecoverySource : Nat := 0\n",
        encoding="utf-8",
    )

    sources = render_module._open_lean_sources(project, exclude_roots=())
    try:
        index = sources.capture().index
    finally:
        sources.close()

    assert index.find("generatedOnly") is None
    assert index.find("incompleteOutputSource") is not None
    assert index.find("retainedRecoverySource") is None


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


def test_render_fsyncs_every_new_output_parent_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    output_parent = tmp_path / "publish" / "nested" / "site-parent"
    output = output_parent / "out"
    original = os.fsync
    synced_directories: set[tuple[int, int]] = set()

    def record(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            synced_directories.add((metadata.st_dev, metadata.st_ino))
        original(descriptor)

    monkeypatch.setattr(render_module.os, "fsync", record)
    render_site(project / "blueprint", output, lean_root=project)

    required = {
        (path.stat().st_dev, path.stat().st_ino)
        for path in (
            tmp_path,
            tmp_path / "publish",
            tmp_path / "publish/nested",
            output_parent,
        )
    }
    assert required <= synced_directories
    assert (output / PUBLICATION_MANIFEST).is_file()


@pytest.mark.parametrize("failure_index", range(1, 5))
def test_nested_output_parent_fsync_failure_prevents_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_index: int,
) -> None:
    project = _project(tmp_path)
    output_parent = tmp_path / "publish" / "nested" / "site-parent"
    output = output_parent / "out"
    original = os.fsync
    directory_syncs = 0

    def fail_at_boundary(descriptor: int) -> None:
        nonlocal directory_syncs
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            directory_syncs += 1
            if directory_syncs == failure_index:
                raise OSError("injected output-parent fsync failure")
        original(descriptor)

    monkeypatch.setattr(render_module.os, "fsync", fail_at_boundary)

    with pytest.raises(PublicationError, match="create and bind the output parent"):
        render_site(project / "blueprint", output, lean_root=project)

    assert directory_syncs == failure_index
    assert output_parent.is_dir()
    assert not output.exists()
    assert not list(output_parent.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))


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


def test_publish_refuses_a_cross_filesystem_stage_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "workspace/site"
    stage.mkdir(parents=True)
    destination = tmp_path / "out"
    original_fstat = render_module.os.fstat
    workspace_identity = render_module._directory_path_identity(stage.parent)
    workspace_descriptor = render_module._open_directory_path(stage.parent)
    workspace_fstats = 0

    def report_another_device(descriptor: int):
        nonlocal workspace_fstats
        metadata = original_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != workspace_identity:
            return metadata
        workspace_fstats += 1
        if workspace_fstats == 1:
            return metadata
        return type("OtherDevice", (), {"st_dev": metadata.st_dev + 1})()

    monkeypatch.setattr(render_module.os, "fstat", report_another_device)
    output_parent = render_module._open_or_create_output_parent(tmp_path)
    try:
        with pytest.raises(PublicationError, match="different filesystems"):
            render_module._publish_staged_site(
                stage,
                destination,
                render_module._DestinationState("absent"),
                render_module._DestinationState("owned", identity=(0, 0)),
                expected_inventory=None,
                staged_inventory=render_module._CleanupInventory((), ()),
                commit_state=render_module._PublicationCommitState(),
                input_guard=lambda: None,
                output_parent=output_parent,
                workspace_identity=workspace_identity,
                workspace_descriptor=workspace_descriptor,
            )
    finally:
        os.close(workspace_descriptor)
        output_parent.close()

    assert stage.is_dir()
    assert not destination.exists()


def test_workspace_descriptor_cleanup_does_not_mask_the_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    destination = tmp_path / "out"
    original_close = render_module.os.close
    fail_close = False
    close_failed = False
    original_build = render_module._build_publication_plan

    def fail_plan(*args, **kwargs):
        nonlocal fail_close
        original_build(*args, **kwargs)
        fail_close = True
        raise RuntimeError("original precommit failure")

    def fail_one_close(descriptor: int) -> None:
        nonlocal close_failed
        original_close(descriptor)
        if fail_close and not close_failed:
            close_failed = True
            raise OSError("injected descriptor cleanup failure")

    monkeypatch.setattr(render_module.os, "close", fail_one_close)
    monkeypatch.setattr(render_module, "_build_publication_plan", fail_plan)

    with pytest.raises(RuntimeError, match="original precommit failure"):
        render_site(project / "blueprint", destination, lean_root=project)

    assert close_failed
    assert not destination.exists()


def test_failed_stage_inspection_does_not_leak_file_descriptors(tmp_path: Path) -> None:
    descriptor_root = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        pytest.skip("process file descriptors are not inspectable")
    stage = tmp_path / "workspace/site"
    stage.mkdir(parents=True)
    destination = tmp_path / "out"
    expected = render_module._DestinationState("absent")
    before = len(list(descriptor_root.iterdir()))
    output_parent = render_module._open_or_create_output_parent(tmp_path)
    workspace_descriptor = render_module._open_directory_path(stage.parent)
    try:
        for _ in range(40):
            with pytest.raises(PublicationError, match="stage changed"):
                render_module._publish_staged_site(
                    stage,
                    destination,
                    expected,
                    render_module._DestinationState("owned"),
                    expected_inventory=None,
                    staged_inventory=render_module._CleanupInventory((), ()),
                    commit_state=render_module._PublicationCommitState(),
                    input_guard=lambda: None,
                    output_parent=output_parent,
                    workspace_identity=render_module._directory_path_identity(stage.parent),
                    workspace_descriptor=workspace_descriptor,
                )
    finally:
        os.close(workspace_descriptor)
        output_parent.close()

    assert len(list(descriptor_root.iterdir())) <= before + 1


def test_unsupported_platform_fails_before_creating_a_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(render_module, "fcntl", None)

    with pytest.raises(PublicationError, match="unavailable on this platform"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))


def test_unsupported_filesystem_fails_before_inspecting_or_changing_live_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    sentinel = output / "PRECIOUS"
    sentinel.write_text("keep\n", encoding="utf-8")
    inspected = False
    original_implementation = render_module._rename_implementation

    def unsupported_exchange(*, exchange: bool):
        function, flag = original_implementation(exchange=exchange)
        if not exchange:
            return function, flag

        def fail(*_args) -> int:
            render_module.ctypes.set_errno(errno.EOPNOTSUPP)
            return -1

        return fail, flag

    def inspect(*args, **kwargs):
        nonlocal inspected
        inspected = True
        return render_module._DestinationState("absent")

    monkeypatch.setattr(render_module, "_rename_implementation", unsupported_exchange)
    monkeypatch.setattr(render_module, "_inspect_destination_at", inspect)

    with pytest.raises(PublicationError, match="unavailable on this filesystem"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not inspected
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
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
    assert "another publication is committing" in str(failures[0])
    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["complete"]
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))


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
        ("https://user:secret@github.com/owner/repo.git", None),
        ("https://ci:token@git.example.com:8443/group/repo.git", None),
        ("https://github.com/owner/repo.git?access_token=secret", None),
        ("https://github.com/owner/repo.git#secret", None),
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
    ref = _commit_project(project)
    out = tmp_path / "out"

    render_site(
        project / "blueprint",
        out,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref=ref,
    )

    assert not (out / "sources").exists()
    expected = f"https://github.com/owner/repo/blob/{ref}/blueprint/sources/paper.md#lemma-3"
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


def test_repository_links_use_the_verified_commit_not_a_mutable_ref(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    commit = _commit_project(project)
    output = tmp_path / "out"

    render_site(
        project / "blueprint",
        output,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref="HEAD",
    )

    manifest = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert manifest["git_ref"] == commit
    assert f"/blob/{commit}/Project/Basic.lean#L5" in chapter
    assert "/blob/HEAD/" not in chapter


def test_dirty_lean_snapshot_does_not_borrow_the_clean_commit_ref(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    commit = _commit_project(project)
    lean = project / "Project/Basic.lean"
    lean.write_text(
        lean.read_text(encoding="utf-8").replace(
            "theorem top",
            "\n\n\n\ntheorem top",
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"

    report = render_site(
        project / "blueprint",
        output,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref=commit,
    )

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    manifest = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert "/blob/" not in chapter
    assert "Project/Basic.lean" in chapter
    assert manifest["git_ref"] is None
    assert report.warnings == [
        "repository links were omitted because the captured blueprint and Lean "
        "inputs do not match one verified local Git commit"
    ]


def test_untracked_source_note_stays_published_instead_of_linking_to_a_404(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    roadmap = project / "blueprint/roadmap/README.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8")
        + "\nUntracked [source](../sources/untracked.md).\n",
        encoding="utf-8",
    )
    commit = _commit_project(project)
    source = project / "blueprint/sources/untracked.md"
    source.parent.mkdir()
    source.write_text("# Local source note\n", encoding="utf-8")
    output = tmp_path / "out"

    report = render_site(
        project / "blueprint",
        output,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref=commit,
    )

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert (output / "sources/untracked.md").read_bytes() == source.read_bytes()
    assert "../sources/untracked.md" in chapter
    assert f"/blob/{commit}/blueprint/sources/untracked.md" not in chapter
    assert report.warnings


def test_untracked_empty_sources_directory_cannot_receive_a_commit_link(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    roadmap = project / "blueprint/roadmap/README.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8")
        + "\nBrowse [all sources](../sources/).\n",
        encoding="utf-8",
    )
    commit = _commit_project(project)
    (project / "blueprint/sources").mkdir()
    output = tmp_path / "out"

    report = render_site(
        project / "blueprint",
        output,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref=commit,
    )

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    manifest = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert f"/tree/{commit}/blueprint/sources" not in chapter
    assert manifest["git_ref"] is None
    assert report.warnings


def test_ref_change_after_capture_cannot_label_old_bytes_with_the_new_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    old_commit = _commit_project(project)
    lean = project / "Project/Basic.lean"
    lean.write_text(
        lean.read_text(encoding="utf-8").replace("theorem top", "\n\ntheorem top"),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "--all"], cwd=project, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Autoform Test",
            "-c",
            "user.email=autoform@example.invalid",
            "commit",
            "-q",
            "--no-gpg-sign",
            "-m",
            "new source generation",
        ],
        cwd=project,
        check=True,
    )
    new_commit = _git_head(project)
    subprocess.run(["git", "reset", "--hard", "-q", old_commit], cwd=project, check=True)

    def advance_ref(_root: Path) -> str:
        subprocess.run(
            ["git", "update-ref", "HEAD", new_commit],
            cwd=project,
            check=True,
        )
        return new_commit

    monkeypatch.setattr(render_module, "detect_ref", advance_ref)
    output = tmp_path / "out"
    report = render_site(
        project / "blueprint",
        output,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
    )

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    manifest = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert f"/blob/{new_commit}/" not in chapter
    assert manifest["git_ref"] is None
    assert report.warnings


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
    ref = _commit_project(repo)
    out = tmp_path / "out"

    render_site(
        inner / "blueprint",
        out,
        lean_root=repo,
        repository_url="https://github.com/owner/repo",
        ref=ref,
    )

    chapter = (out / "roadmap/README.md").read_text(encoding="utf-8")
    assert f"blob/{ref}/docs/project/blueprint/roadmap/top.md" in chapter
    assert f"blob/{ref}/blueprint/roadmap/top.md" not in chapter


def test_markdown_source_permalink_quotes_the_original_repository_path(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    project = _project(repository / "docs with spaces")
    ref = _commit_project(repository)
    out = tmp_path / "out"

    render_site(
        project / "blueprint",
        out,
        lean_root=repository,
        repository_url="https://github.com/owner/repo",
        ref=ref,
    )

    chapter = (out / "roadmap/README.md").read_text(encoding="utf-8")
    assert (
        f"blob/{ref}/docs%20with%20spaces/project/blueprint/roadmap/top.md"
        in chapter
    )
    assert ".autoform-publication-" not in chapter


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
    ref = _commit_project(project)
    out = tmp_path / "out"

    render_site(
        project / "blueprint",
        out,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref=ref,
    )

    chapter = (out / "roadmap/README.md").read_text(encoding="utf-8")
    expected = f"[paper]: https://github.com/owner/repo/blob/{ref}/blueprint/sources/paper.md"
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
    ref = _commit_project(project)
    out = tmp_path / "out"

    render_site(
        project / "blueprint",
        out,
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref=ref,
    )

    chapter = (out / "roadmap/README.md").read_text(encoding="utf-8")
    assert (
        f'[paper]: https://github.com/owner/repo/blob/{ref}/blueprint/sources/paper%20note.md "Source note"'
        in chapter
    )
    assert "<../sources/paper note.md>" not in chapter


def test_a_fresh_vault_reports_no_work_rather_than_one_ready_item(tmp_path: Path) -> None:
    """The roadmap landing page is not a formalization target.

    Counting every childless article made a freshly scaffolded vault claim
    "0 of 1 targets complete, 1 ready now", so the site described work before any
    had been planned.
    """
    from autoform_cli.scaffold import scaffold_project

    project = tmp_path / "project"
    scaffold_project(project, title="Empty")
    out = tmp_path / "out"

    render_site(project / "blueprint", out)

    overview = (out / "README.md").read_text(encoding="utf-8")
    assert "0 of 0 targets complete" in overview
    assert "items settled" not in overview
    assert '<div class="bp-figure-value">0%</div>' in overview
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


def _count_relative_links(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record the target of every `mermaid.relative_link` call."""
    from autoform_cli import mermaid

    calls: list[Path] = []
    relative_link = mermaid.relative_link

    def counting(target: Path, output: Path, link_extension: str) -> str:
        calls.append(target)
        return relative_link(target, output, link_extension)

    monkeypatch.setattr(mermaid, "relative_link", counting)
    return calls


def test_node_links_resolve_each_target_page_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Building a link per node resolved paths on disk once per node on every
    page, which made render time grow with the square of the node count."""
    from autoform_cli.render import _anchored_links

    chapter, other, page = tmp_path / "a.md", tmp_path / "b" / "README.md", tmp_path / "page.md"
    targets = {f"a/{index}": (chapter, f"n{index}") for index in range(20)}
    targets["b"] = (other, "")
    targets["here"] = (page, "self")
    calls = _count_relative_links(monkeypatch)
    resolved: list[Path] = []
    resolve = Path.resolve

    def counting_resolve(self: Path, strict: bool = False) -> Path:
        resolved.append(self)
        return resolve(self, strict)

    monkeypatch.setattr(Path, "resolve", counting_resolve)

    links = _anchored_links(targets, page)

    assert sorted(calls) == [chapter, other]
    # Once to compare it with the current page, once inside relative_link.
    assert resolved.count(chapter) <= 2
    assert links["a/7"] == "a.html#n7"
    assert links["b"] == "b/index.html"
    assert links["here"] == "#self"


def test_rewriting_a_page_links_only_the_nodes_it_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page uses the few node links it contains, so rewriting it should not
    build a link for every node in the graph."""
    from autoform_cli.render import _rewrite_links

    blueprint, destination = tmp_path / "blueprint", tmp_path / "out"
    source_dir = blueprint / "roadmap"
    source_dir.mkdir(parents=True)
    chapter, other, page = destination / "a.md", destination / "b.md", destination / "page.md"
    targets = {"a/x": (chapter, "x"), "a/z": (chapter, "z"), "b/y": (other, "y")}
    node_sources = {
        (source_dir / "x.md").resolve(): "a/x",
        (source_dir / "z.md").resolve(): "a/z",
        (source_dir / "y.md").resolve(): "b/y",
    }
    calls = _count_relative_links(monkeypatch)

    def rewrite(text: str) -> str:
        return _rewrite_links(
            text,
            source_dir=source_dir,
            page=page,
            blueprint=blueprint,
            destination=destination,
            node_sources=node_sources,
            targets=targets,
        )

    assert rewrite("Plain prose.\n") == "Plain prose.\n"
    assert calls == []
    assert rewrite("See [X](x.md).\n") == "See [X](a.md#x).\n"
    assert calls == [chapter]
    calls.clear()
    # Two nodes on the same chapter page cost one link to it, not one each.
    assert rewrite("[X](x.md), [Z](z.md)\n") == "[X](a.md#x), [Z](a.md#z)\n"
    assert calls == [chapter]


def test_a_focus_page_asks_for_its_node_links_once(tmp_path: Path) -> None:
    """Each request builds a link for every node, so asking twice per focus
    page doubled the cost of the largest group of generated pages."""
    from autoform_cli.graph_pages import focus_page_path, write_graph_pages

    project = _project(tmp_path)
    graph = load_graph(project / "blueprint")
    destination = tmp_path / "out"
    requested: list[Path] = []

    def node_links(page: Path) -> dict[str, str]:
        requested.append(page)
        return {node_id: f"roadmap.html#{node_id}" for node_id in graph.nodes}

    write_graph_pages(graph, derive(graph), destination, node_links=node_links)

    for node_id in ("base", "top"):
        page = focus_page_path(destination, node_id)
        assert requested.count(page) == 1
        assert f"roadmap.html#{node_id}" in page.read_text(encoding="utf-8")


def test_a_node_that_is_the_current_page_links_as_a_bare_fragment(tmp_path: Path) -> None:
    """A container article has no anchor of its own, and an empty href would
    drop the link, so its own page links to the top of itself."""
    from autoform_cli.render import _anchored_links

    page = tmp_path / "chapter" / "README.md"

    assert _anchored_links({"chapter": (page, ""), "chapter/x": (page, "x")}, page) == {
        "chapter": "#",
        "chapter/x": "#x",
    }


def _conditional_project(tmp_path: Path, policy: str) -> Path:
    """`_project` with Top proved from an open statement, under the given policy."""
    project = _project(tmp_path)
    roadmap = project / "blueprint" / "roadmap"
    (roadmap / "README.md").write_text(
        f"---\nopen_statements: {policy}\n---\n\n# Roadmap\n\n"
        "## Definitions\n\n- [Base](base.md)\n\n"
        "## Results\n\n- [Open](open.md)\n- [Top](top.md)\n",
        encoding="utf-8",
    )
    (project / "Project" / "Open.lean").write_text(
        "namespace Project\n\ntheorem openStatement : True := sorry\n\nend Project\n", encoding="utf-8"
    )
    (roadmap / "open.md").write_text(
        "---\ndeclaration: theorem\nstatement: formalized\nlean: Project.openStatement\n---\n\n"
        "# Open\n\nA statement whose proof is still sorry.\n\n## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    top = roadmap / "top.md"
    top.write_text(
        top.read_text(encoding="utf-8") + "\n## Proof depends on\n\n- [Open](open.md)\n", encoding="utf-8"
    )
    return project


def test_a_conditional_proof_names_the_open_statements_it_assumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Lean row is the fallback for a declaration without a source link, and
    # on CI the Actions environment would supply the coordinates for one.
    for variable in ("GITHUB_REPOSITORY", "GITHUB_SERVER_URL", "GITHUB_SHA"):
        monkeypatch.delenv(variable, raising=False)
    project = _conditional_project(tmp_path, "allowed")

    render_site(project / "blueprint", tmp_path / "out", lean_root=project, repository_url="", ref="")
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    top = page[page.index('id="top"'):]

    assert '<div class="bp-thmwrapper theorem-style-plain bp-conditional" id="top"' in page
    assert '●<span class="bp-mark-label">conditionally proved</span>' in top
    assert (
        '<span class="bp-key">Assumes</span><span class="bp-value">'
        '<a class="bp-ref bp-ref-can_prove" href="#open">Theorem 1 (Open)</a>'
        " (open statements without a recorded Lean proof)</span>"
    ) in top
    # Only the conditional proof carries the row; the open statement assumes nothing.
    assert page.count('<span class="bp-key">Assumes</span>') == 1
    # The row follows the implementation row and precedes Discussion.
    meta = top[top.index('<div class="bp-meta">'):top.index('<details class="bp-dependencies">')]
    assert re.findall(r'<span class="bp-key">([^<]+)</span>', meta) == ["Lean", "Assumes", "Discussion"]

    css = (tmp_path / "out/stylesheets/blueprint.css").read_text(encoding="utf-8")
    assert ".bp-ref-conditional::before, .bp-swatch-conditional { background: #E9DFFC; border-color: #6B3FCF; }" in css
    assert "[data-md-color-scheme=slate] .bp-ref-conditional::before" in css
    assert ".bp-conditional .bp-mark { color: #6B3FCF; }" in css


def test_the_strict_policy_shows_the_same_proof_as_proved_with_no_assumptions(tmp_path: Path) -> None:
    project = _conditional_project(tmp_path, "forbidden")

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")

    assert '<div class="bp-thmwrapper theorem-style-plain bp-proved" id="top"' in page
    assert '<span class="bp-key">Assumes</span>' not in page


@pytest.mark.parametrize(
    ("dropped", "why"),
    [
        ("proof: formalized\n", "Its prerequisites are ready, so the proof can be written now."),
        (
            "statement: formalized\nproof: formalized\n",
            "Its prerequisites are ready, so the statement can be written down.",
        ),
    ],
)
def test_next_up_explains_readiness_without_naming_a_policy(tmp_path: Path, dropped: str, why: str) -> None:
    project = _project(tmp_path)
    top = project / "blueprint" / "roadmap" / "top.md"
    top.write_text(top.read_text(encoding="utf-8").replace(dropped, ""), encoding="utf-8")

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    landing = (tmp_path / "out/README.md").read_text(encoding="utf-8")

    assert '<div class="bp-next-target" data-autoform-node-id="top">' in landing
    assert f'<div class="bp-next-why">{why}</div>' in landing


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
    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert "Authored dependencies" in chapter


@pytest.mark.parametrize(
    "alias",
    (
        "Dependencies.md",
        "Dependencies.html",
        "Graph.html",
        "Progress.md",
        "Structure.md",
        "Publication.json",
    ),
)
def test_root_case_alias_of_generated_output_is_rejected(
    tmp_path: Path,
    alias: str,
) -> None:
    project = _project(tmp_path)
    authored = project / "blueprint" / alias
    authored.write_text("# Authored page\n", encoding="utf-8")
    output = tmp_path / "out"

    with pytest.raises(
        PublicationError,
        match="noncanonical generated publication filename",
    ):
        render_site(project / "blueprint", output, lean_root=project)

    assert authored.read_text(encoding="utf-8") == "# Authored page\n"
    assert not output.exists()
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))


def test_output_must_have_an_ordinary_final_component(tmp_path: Path) -> None:
    project = _project(tmp_path)

    with pytest.raises(PublicationError, match="ordinary directory"):
        render_site(project / "blueprint", tmp_path / "child" / "..", lean_root=project)

    assert not (tmp_path / "child").exists()


def test_output_parent_binding_uses_one_retained_final_descriptor(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "new" / "nested" / "parent"

    binding = render_module._open_or_create_output_parent(parent)
    try:
        metadata = os.fstat(binding.descriptor)
        assert binding.path == parent.absolute()
        assert binding.identity == (metadata.st_dev, metadata.st_ino)
        assert not hasattr(binding, "descriptors")
        assert not hasattr(binding, "identities")
        binding.verify()
    finally:
        binding.close()


def test_render_refuses_portable_blueprint_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(
        tree_snapshot_module,
        "_DESCRIPTOR_CAPTURE_SUPPORTED",
        False,
    )

    with pytest.raises(PublicationError, match="requires safe directory traversal"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert not (tmp_path / "out").exists()


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
        path.name.startswith(render_module._PUBLICATION_STAGE_PREFIX)
        for path in blueprint.iterdir()
    )


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
    assert not list(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))


def test_blueprint_reselection_during_capture_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    other_project = _project(tmp_path / "other")
    blueprint = project / "blueprint"
    replacement = other_project / "blueprint"
    retained = tmp_path / "retained-blueprint"
    output = tmp_path / "out"
    swapped = False

    def swap_after_root_list(event: str, relative: str) -> None:
        nonlocal swapped
        if not swapped and event == "after-directory-list" and not relative:
            blueprint.rename(retained)
            replacement.rename(blueprint)
            swapped = True

    monkeypatch.setattr(
        tree_snapshot_module,
        "_tree_snapshot_checkpoint",
        swap_after_root_list,
    )
    try:
        with pytest.raises(PublicationError, match="blueprint changed"):
            render_site(blueprint, output, lean_root=project)
    finally:
        if swapped:
            blueprint.rename(replacement)
            retained.rename(blueprint)

    assert not output.exists()


def test_blueprint_a_b_a_change_during_render_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    article = project / "blueprint/roadmap/top.md"
    stable = article.read_text(encoding="utf-8")
    original = render_module._build_publication_plan

    def mutate_and_restore(*args, **kwargs):
        report = original(*args, **kwargs)
        article.write_text(stable + "\nTransient.\n", encoding="utf-8")
        article.write_text(stable, encoding="utf-8")
        return report

    monkeypatch.setattr(render_module, "_build_publication_plan", mutate_and_restore)

    with pytest.raises(PublicationError, match="blueprint changed"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not output.exists()


def test_git_remote_a_b_a_change_cannot_change_published_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in ("GITHUB_REPOSITORY", "GITHUB_SERVER_URL"):
        monkeypatch.delenv(variable, raising=False)
    project = _project(tmp_path)
    ref = _commit_project(project)
    output = tmp_path / "out"
    subprocess.run(
        ["git", "config", "remote.origin.url", "https://github.com/correct/source.git"],
        cwd=project,
        check=True,
    )
    original = render_module.build_linker

    def expose_transient_remote(*args, **kwargs):
        assert kwargs["detect_missing"] is False
        subprocess.run(
            ["git", "config", "remote.origin.url", "https://github.com/wrong/source.git"],
            cwd=project,
            check=True,
        )
        try:
            return original(*args, **kwargs)
        finally:
            subprocess.run(
                [
                    "git",
                    "config",
                    "remote.origin.url",
                    "https://github.com/correct/source.git",
                ],
                cwd=project,
                check=True,
            )

    monkeypatch.setattr(render_module, "build_linker", expose_transient_remote)
    render_site(project / "blueprint", output, lean_root=project, ref=ref)

    page = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert "github.com/correct/source" in page
    assert "github.com/wrong/source" not in page


def test_transient_auto_detected_remote_cannot_label_captured_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in ("GITHUB_REPOSITORY", "GITHUB_SERVER_URL"):
        monkeypatch.delenv(variable, raising=False)
    project = _project(tmp_path)
    ref = _commit_project(project)
    subprocess.run(
        ["git", "config", "remote.origin.url", "https://github.com/correct/source.git"],
        cwd=project,
        check=True,
    )
    original = render_module.detect_repository_url
    first = True

    def transient_remote(root: Path) -> str | None:
        nonlocal first
        if not first:
            return original(root)
        first = False
        subprocess.run(
            ["git", "config", "remote.origin.url", "https://github.com/wrong/source.git"],
            cwd=project,
            check=True,
        )
        try:
            return original(root)
        finally:
            subprocess.run(
                [
                    "git",
                    "config",
                    "remote.origin.url",
                    "https://github.com/correct/source.git",
                ],
                cwd=project,
                check=True,
            )

    monkeypatch.setattr(render_module, "detect_repository_url", transient_remote)
    report = render_site(
        project / "blueprint",
        tmp_path / "out",
        lean_root=project,
        ref=ref,
    )

    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert "github.com/correct/source" not in page
    assert "github.com/wrong/source" not in page
    assert report.warnings


def test_blueprint_capture_enforces_file_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    existing = [
        path.stat().st_size
        for path in (project / "blueprint").rglob("*")
        if path.is_file()
    ]
    maximum = max(existing)
    (project / "blueprint/oversized.bin").write_bytes(b"x" * (maximum + 1))
    monkeypatch.setattr(
        render_module,
        "_PUBLICATION_SNAPSHOT_SELECTION",
        TreeSelection(
            include=render_module._publication_snapshot_includes,
            descend=render_module._publication_snapshot_descends,
            limits=TreeCaptureLimits(
                max_entries=10_000,
                max_depth=128,
                max_file_bytes=maximum,
                max_total_bytes=10 * 1024 * 1024,
            ),
        ),
    )

    with pytest.raises(PublicationError, match="max_file_bytes"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert not (tmp_path / "out").exists()


def test_lean_capture_enforces_file_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(
        render_module,
        "_PUBLICATION_CAPTURE_LIMITS",
        TreeCaptureLimits(
            max_entries=10_000,
            max_depth=128,
            max_file_bytes=8,
            max_total_bytes=1024,
        ),
    )

    with pytest.raises(PublicationError, match="Lean source.*max_file_bytes"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert not (tmp_path / "out").exists()


def test_manifest_read_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    before = (output / PUBLICATION_MANIFEST).read_bytes()
    monkeypatch.setattr(render_module, "_PUBLICATION_MANIFEST_MAX_BYTES", 8)

    with pytest.raises(PublicationError, match="max_file_bytes=8"):
        render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).read_bytes() == before


def test_generated_manifest_is_bounded_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    monkeypatch.setattr(render_module, "_PUBLICATION_MANIFEST_MAX_BYTES", 8)

    with pytest.raises(PublicationError, match="generated publication manifest.*max_file_bytes=8"):
        render_site(project / "blueprint", output, lean_root=project)

    assert not output.exists()
    assert not list(tmp_path.glob(".autoform-publication-*"))


def test_inventory_enumeration_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    for name in ("a", "b", "c"):
        (root / name).write_text(name, encoding="utf-8")
    monkeypatch.setattr(render_module, "_PUBLICATION_MAX_ENTRIES", 2)

    with pytest.raises(PublicationError, match="max_entries=2"):
        render_module._cleanup_inventory(root)


@pytest.mark.parametrize(
    ("target", "retained_name"),
    (("workspace", "."), ("site", "site")),
)
def test_post_commit_nested_injection_is_retained_instead_of_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    retained_name: str,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    if target == "site":
        render_site(project / "blueprint", output, lean_root=project)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "PRECIOUS").write_text("keep\n", encoding="utf-8")
    original = render_module._publish_staged_site

    def inject_after_publish(*args, **kwargs):
        original(*args, **kwargs)
        workspace = Path(args[0]).parent
        root = workspace if target == "workspace" else workspace / target
        victim.rename(root / "injected-victim")

    monkeypatch.setattr(render_module, "_publish_staged_site", inject_after_publish)

    report = render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).is_file()
    assert len(report.warnings) == 1
    workspace = Path(report.warnings[0].rsplit(" at ", 1)[1])
    assert (workspace / retained_name / "injected-victim/PRECIOUS").read_text(
        encoding="utf-8"
    ) == "keep\n"


def test_post_commit_displaced_generation_disappearance_is_not_reported_as_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    original = render_module._publish_staged_site

    def remove_displaced_after_publish(*args, **kwargs):
        original(*args, **kwargs)
        shutil.rmtree(Path(args[0]).parent / "site")

    monkeypatch.setattr(
        render_module,
        "_publish_staged_site",
        remove_displaced_after_publish,
    )

    report = render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).is_file()
    assert len(report.warnings) == 1
    workspace = Path(report.warnings[0].rsplit(" at ", 1)[1])
    assert workspace.is_dir()


def test_cleanup_claim_retains_a_replacement_injected_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    victim = tmp_path / "victim"
    victim.write_text("PRECIOUS\n", encoding="utf-8")
    original = render_module._read_regular_file_at
    injected = False

    def inject_after_read(
        parent_descriptor,
        name,
        display_path,
        *,
        max_bytes,
        ignore_close_errors=False,
    ):
        nonlocal injected
        data = original(
            parent_descriptor,
            name,
            display_path,
            max_bytes=max_bytes,
            ignore_close_errors=ignore_close_errors,
        )
        if ".autoform-cleanup-" in name and not injected:
            injected = True
            displaced = f"{name}.displaced"
            os.rename(
                name,
                displaced,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
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


def test_nested_cleanup_disappearance_is_not_reported_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / ".autoform-publication-test"
    child = workspace / "source"
    child.mkdir(parents=True)
    (child / "a").write_text("a", encoding="utf-8")
    disappearing = child / "b"
    disappearing.write_text("b", encoding="utf-8")
    workspace_identity = render_module._directory_path_identity(workspace)
    child_identity = render_module._directory_path_identity(child)
    inventory = render_module._cleanup_inventory(child)
    original = render_module._cleanup_rename_noreplace

    def remove_sibling(descriptor, source, target):
        original(descriptor, source, target)
        if source == "a" and disappearing.exists():
            os.unlink("b", dir_fd=descriptor)

    monkeypatch.setattr(render_module, "_cleanup_rename_noreplace", remove_sibling)

    assert not render_module._remove_owned_workspace(
        workspace,
        workspace_identity,
        expected_children={"source": {child_identity: inventory}},
    )
    assert workspace.exists()


def test_verified_replacement_cleanup_never_deletes_a_swapped_new_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    previous_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\nNew generation.\n",
        encoding="utf-8",
    )
    original = render_module._publish_staged_site

    def restore_previous_after_verified_commit(stage, destination, *args, **kwargs):
        original(stage, destination, *args, **kwargs)
        published = tmp_path / "published-generation"
        destination.rename(published)
        stage.rename(destination)
        published.rename(stage)

    monkeypatch.setattr(
        render_module,
        "_publish_staged_site",
        restore_previous_after_verified_commit,
    )

    report = render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).read_bytes() == previous_manifest
    assert len(report.warnings) == 1
    workspace = Path(report.warnings[0].rsplit(" at ", 1)[1])
    assert (workspace / "site/publication.json").read_bytes() != previous_manifest


def test_verified_replacement_reports_a_whole_workspace_disappearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    previous_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\nNew generation.\n",
        encoding="utf-8",
    )
    moved_workspace = tmp_path / "moved-publication-workspace"
    original = render_module._publish_staged_site

    def move_workspace_after_verified_commit(stage, destination, *args, **kwargs):
        original(stage, destination, *args, **kwargs)
        stage.parent.rename(moved_workspace)

    monkeypatch.setattr(
        render_module,
        "_publish_staged_site",
        move_workspace_after_verified_commit,
    )

    report = render_site(project / "blueprint", output, lean_root=project)

    assert (output / PUBLICATION_MANIFEST).read_bytes() != previous_manifest
    assert len(report.warnings) == 1
    assert "cleanup was refused" in report.warnings[0]
    assert (moved_workspace / "site/publication.json").read_bytes() == previous_manifest


def test_precommit_cleanup_never_deletes_a_moved_prior_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    previous_manifest = (output / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\nNew generation.\n",
        encoding="utf-8",
    )
    intended_stage = tmp_path / "intended-stage"

    def move_prior_into_workspace(stage, destination, *args, **kwargs):
        stage.rename(intended_stage)
        destination.rename(stage)
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(
        render_module,
        "_publish_staged_site",
        move_prior_into_workspace,
    )

    with pytest.raises(PublicationError, match="cleanup was refused") as error:
        render_site(project / "blueprint", output, lean_root=project)

    workspace = Path(str(error.value).split("cleanup was refused at ", 1)[1].split(";", 1)[0])
    assert not output.exists()
    assert (workspace / "site/publication.json").read_bytes() == previous_manifest
    assert (intended_stage / PUBLICATION_MANIFEST).read_bytes() != previous_manifest


def test_first_install_parent_swap_never_publishes_into_the_replacement_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    publication_parent = tmp_path / "publication-parent"
    publication_parent.mkdir()
    destination = publication_parent / "out"
    replacement_parent = tmp_path / "replacement-parent"
    replacement_parent.mkdir()
    sentinel = replacement_parent / "sentinel"
    sentinel.write_text("keep\n", encoding="utf-8")
    moved_parent = tmp_path / "original-parent"
    original = render_module._rename_noreplace

    def swap_parent_then_install(*args):
        publication_parent.rename(moved_parent)
        replacement_parent.rename(publication_parent)
        original(*args)

    monkeypatch.setattr(render_module, "_rename_noreplace", swap_parent_then_install)

    with pytest.raises(PublicationError, match="original output-parent generation"):
        render_site(project / "blueprint", destination, lean_root=project)

    assert (publication_parent / "sentinel").read_text(encoding="utf-8") == "keep\n"
    assert not destination.exists()
    assert (moved_parent / "out/publication.json").is_file()
    assert len(list(moved_parent.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))) == 1


def test_replacement_parent_swap_never_overwrites_the_replacement_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    publication_parent = tmp_path / "publication-parent"
    publication_parent.mkdir()
    destination = publication_parent / "out"
    render_site(project / "blueprint", destination, lean_root=project)
    previous_manifest = (destination / PUBLICATION_MANIFEST).read_bytes()
    article = project / "blueprint/roadmap/top.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\nNew generation.\n",
        encoding="utf-8",
    )
    replacement_parent = tmp_path / "replacement-parent"
    replacement_destination = replacement_parent / "out"
    replacement_destination.mkdir(parents=True)
    sentinel = replacement_destination / "sentinel"
    sentinel.write_text("keep\n", encoding="utf-8")
    moved_parent = tmp_path / "original-parent"
    original = render_module._rename_exchange

    def swap_parent_then_exchange(*args):
        publication_parent.rename(moved_parent)
        replacement_parent.rename(publication_parent)
        original(*args)

    monkeypatch.setattr(render_module, "_rename_exchange", swap_parent_then_exchange)

    with pytest.raises(PublicationError, match="original output-parent generation"):
        render_site(project / "blueprint", destination, lean_root=project)

    assert (destination / "sentinel").read_text(encoding="utf-8") == "keep\n"
    assert (moved_parent / "out/publication.json").read_bytes() != previous_manifest
    workspace = next(moved_parent.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
    assert (workspace / "site/publication.json").read_bytes() == previous_manifest


def test_explicit_symlink_lean_root_is_canonicalized_once(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ref = _commit_project(project)
    alias = tmp_path / "project-alias"
    alias.symlink_to(project, target_is_directory=True)
    output = tmp_path / "out"

    report = render_site(
        project / "blueprint",
        output,
        lean_root=alias,
        repository_url="https://github.com/owner/repo",
        ref=ref,
    )

    assert report.linked == 2
    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert f"/blob/{ref}/blueprint/roadmap/top.md" in chapter


def test_explicit_macos_var_alias_lean_root_is_canonicalized(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ref = _commit_project(project)
    physical = str(project)
    if not physical.startswith("/private/var/"):
        pytest.skip("macOS /var alias is unavailable")
    alias = Path("/var") / Path(physical).relative_to("/private/var")
    if not alias.exists() or alias.resolve() != project.resolve():
        pytest.skip("macOS /var alias is unavailable")

    report = render_site(
        project / "blueprint",
        tmp_path / "out",
        lean_root=alias,
        repository_url="https://github.com/owner/repo",
        ref=ref,
    )

    assert report.linked == 2


def test_cleanup_indexes_each_inventory_record_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cleanup-tree"
    root.mkdir()
    for index in range(200):
        directory = root / f"directory-{index:03d}"
        directory.mkdir()
        (directory / "file.txt").write_text(str(index), encoding="utf-8")
    inventory = render_module._cleanup_inventory(root)
    expected_visits = len(inventory.directories) + len(inventory.files)
    original = render_module.PurePosixPath
    visits = 0

    def count_path(value: str):
        nonlocal visits
        visits += 1
        return original(value)

    monkeypatch.setattr(render_module, "PurePosixPath", count_path)
    descriptor = render_module._open_directory_path(root)
    try:
        render_module._remove_inventory_contents(descriptor, inventory)
    finally:
        os.close(descriptor)

    assert visits == expected_visits
    assert not any(root.iterdir())


def test_stage_directory_symlink_substitution_never_receives_plan_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    foreign = tmp_path / "foreign-directory"
    foreign.mkdir()
    sentinel = foreign / "SENTINEL"
    sentinel.write_text("untouched\n", encoding="utf-8")
    substituted = False

    def substitute_directory(event: str, relative: str) -> None:
        nonlocal substituted
        if substituted or event != "after-directory-open" or relative != "roadmap":
            return
        workspace = next(tmp_path.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
        roadmap = workspace / "site/roadmap"
        roadmap.rename(workspace / "site/roadmap-owned")
        roadmap.symlink_to(foreign, target_is_directory=True)
        substituted = True

    monkeypatch.setattr(
        render_module,
        "_publication_plan_checkpoint",
        substitute_directory,
    )

    with pytest.raises(PublicationError, match="workspace was retained"):
        render_site(project / "blueprint", output, lean_root=project)

    assert substituted
    assert not output.exists()
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"
    assert list(foreign.iterdir()) == [sentinel]


def test_output_parent_loss_after_verified_commit_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    publication_parent = tmp_path / "publication-parent"
    publication_parent.mkdir()
    output = publication_parent / "out"
    replacement_parent = tmp_path / "replacement-parent"
    replacement_parent.mkdir()
    (replacement_parent / "sentinel").write_text("keep\n", encoding="utf-8")
    moved_parent = tmp_path / "moved-output-parent"
    original = render_module._publish_staged_site

    def move_parent_after_verified_commit(*args, **kwargs):
        original(*args, **kwargs)
        publication_parent.rename(moved_parent)
        replacement_parent.rename(publication_parent)

    monkeypatch.setattr(
        render_module,
        "_publish_staged_site",
        move_parent_after_verified_commit,
    )

    with pytest.raises(PublicationError, match="output location is uncertain") as error:
        render_site(project / "blueprint", output, lean_root=project)

    workspace = next(moved_parent.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
    assert workspace.name in str(error.value)
    assert str(workspace) not in str(error.value)
    assert (moved_parent / "out/publication.json").is_file()
    assert (publication_parent / "sentinel").read_text(encoding="utf-8") == "keep\n"


def test_output_parent_swap_during_cleanup_cannot_return_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    publication_parent = tmp_path / "publication-parent"
    publication_parent.mkdir()
    output = publication_parent / "out"
    replacement_parent = tmp_path / "replacement-parent"
    replacement_parent.mkdir()
    (replacement_parent / "sentinel").write_text("keep\n", encoding="utf-8")
    moved_parent = tmp_path / "moved-output-parent"
    original = render_module._remove_owned_workspace
    swapped = False

    def cleanup_then_swap(*args, **kwargs):
        nonlocal swapped
        cleaned = original(*args, **kwargs)
        assert cleaned
        publication_parent.rename(moved_parent)
        replacement_parent.rename(publication_parent)
        swapped = True
        return cleaned

    monkeypatch.setattr(render_module, "_remove_owned_workspace", cleanup_then_swap)

    with pytest.raises(PublicationError, match="output location is uncertain") as error:
        render_site(project / "blueprint", output, lean_root=project)

    assert swapped
    assert "workspace cleanup completed" in str(error.value)
    assert not output.exists()
    assert (moved_parent / "out/publication.json").is_file()
    assert not list(moved_parent.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
    assert (publication_parent / "sentinel").read_text(encoding="utf-8") == "keep\n"


def test_precommit_parent_loss_does_not_report_a_false_workspace_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    publication_parent = tmp_path / "publication-parent"
    publication_parent.mkdir()
    output = publication_parent / "out"
    replacement_parent = tmp_path / "replacement-parent"
    replacement_parent.mkdir()
    (replacement_parent / "sentinel").write_text("keep\n", encoding="utf-8")
    moved_parent = tmp_path / "moved-output-parent"

    def lose_parent_before_commit(_descriptor: int) -> None:
        publication_parent.rename(moved_parent)
        replacement_parent.rename(publication_parent)
        raise OSError("injected stage sync failure")

    monkeypatch.setattr(render_module, "_sync_tree_descriptor", lose_parent_before_commit)

    with pytest.raises(PublicationError, match="original output-parent generation") as error:
        render_site(project / "blueprint", output, lean_root=project)

    workspace = next(moved_parent.glob(f"{render_module._PUBLICATION_STAGE_PREFIX}*"))
    assert workspace.name in str(error.value)
    assert str(publication_parent / workspace.name) not in str(error.value)
    assert (publication_parent / "sentinel").read_text(encoding="utf-8") == "keep\n"
