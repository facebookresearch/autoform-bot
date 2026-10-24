from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import tracemalloc
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
import html5lib
from mkdocs.utils.meta import get_data

import autoform_cli.audit as audit_module
import autoform_cli.lean as lean_module
import autoform_cli.render as render_module
from autoform_cli import _directory_binding as directory_binding_module
from autoform_cli.__main__ import main
from autoform_cli._tree_snapshot import TreeChangedError
from autoform_cli.audit import audit_blueprint
from autoform_cli.coverage import COVERAGE_DISPOSITIONS
from autoform_cli.dashboard import publication_bound_live_state
from autoform_cli.graph import GraphValidationError, atlas_omissions, load_graph
from autoform_cli.lean import LeanSourceError, _normalize_remote
from autoform_cli.markdown import site_converter
from autoform_cli.render import (
    PUBLICATION_MANIFEST,
    PublicationError,
    _COVERAGE_SUMMARY_ORDER,
    _completion_percentage,
    _document_body,
    _first_h1,
    _protect_markdown_in_html,
    load_publication_source,
    publication_source_revision,
    render_site,
)
from autoform_cli.status import INVENTORY_STATES, STATES, derive
from tests.test_lean_sources import _git_commit


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


_REPOSITORY = "https://github.com/owner/repo"


def _commit_project(project: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    return _git_commit(project, "fixture")


def _render(tmp_path: Path, **kwargs):
    project = _project(tmp_path)
    report = render_site(
        project / "blueprint",
        tmp_path / "out",
        lean_root=project,
        repository_url="https://github.com/owner/repo",
        ref=_commit_project(project),
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
    assert (out / "dependencies.json").is_file()
    assert (out / "dependencies/chapters/roadmap.md").is_file()
    assert (out / "dependencies/chapters/roadmap.json").is_file()
    assert not (out / "dependencies/nodes").exists()
    assert (out / "dependencies/full.md").is_file()
    assert (out / "dependencies/full.json").is_file()
    assert (out / "dependencies/index.json").is_file()
    assert (out / "stylesheets/blueprint.css").is_file()
    assert (out / "javascripts/blueprint-mermaid.js").is_file()
    assert (out / "javascripts/blueprint-live.js").is_file()
    assert (out / "javascripts/blueprint-dag.js").is_file()
    assert (out / PUBLICATION_MANIFEST).is_file()
    # Nodes are absorbed into their chapter, not published one page each.
    assert not (out / "roadmap/base.md").exists()
    assert not (out / "roadmap/top.md").exists()
    # The source vault keeps no generated files.
    assert not (project / "blueprint" / "dependencies.md").exists()
    assert "## Depends on" in (project / "blueprint/roadmap/top.md").read_text(encoding="utf-8")

    graph_pages = [out / "dependencies.md", *(out / "dependencies").rglob("*.md")]
    for graph_page in graph_pages:
        document = graph_page.read_text(encoding="utf-8")
        assert 'class="bp-dag-viewer"' in document
        assert "```mermaid" not in document
        assert "blueprint-mermaid.js" not in document
        assert document.count("blueprint-dag.js") == 1
        assert graph_page.with_suffix(".json").is_file()

    project_page = (out / "dependencies.md").read_text(encoding="utf-8")
    assert "graph_view: project" in project_page
    assert 'data-graph-src="dependencies.json"' in project_page
    assert 'data-catalog-src="dependencies/index.json"' in project_page
    assert "data-search-src" not in project_page  # Material reserves and strips this prefix.
    assert "Explore all nodes" not in project_page
    project_payload = json.loads((out / "dependencies.json").read_text(encoding="utf-8"))
    assert project_payload["title"] == "Roadmap"
    assert {node["url"] for node in project_payload["nodes"]} == {
        "dependencies/chapters/roadmap.html"
    }
    chapter_payload = json.loads(
        (out / "dependencies/chapters/roadmap.json").read_text(encoding="utf-8")
    )
    # The explorer names the scope; the page's own H1 adds what it draws, for the tab and site search.
    assert chapter_payload["title"] == "Top level"
    assert "\n# Top level dependencies\n" in (out / "dependencies/chapters/roadmap.md").read_text(encoding="utf-8")
    assert chapter_payload["view"]["breadcrumbs"][-1] == {"label": "Top level", "url": None}
    full_page = (out / "dependencies/full.md").read_text(encoding="utf-8")
    assert 'data-graph-src="full.json"' in full_page
    assert "search:\n  exclude: true" in full_page
    assert "search:\n  exclude: true" not in project_page
    payload = json.loads((out / "dependencies/full.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "autoform-dag-view/v2"
    assert payload["title"] == "Roadmap"
    assert len(payload["nodes"]) == 1
    assert payload["edge_count"] == 0
    assert all(node["url"] for node in payload["nodes"])
    search_index = json.loads((out / "dependencies/index.json").read_text(encoding="utf-8"))
    assert len(search_index["nodes"]) == 3
    # The roadmap's record opens the explorer named for it, not the root chapter's.
    assert {node["id"]: node["url"] for node in search_index["nodes"]}["roadmap"] == "../dependencies.html"


def test_the_root_explorer_takes_the_roadmap_root_title(tmp_path: Path) -> None:
    """A two-chapter thesis is not "All mathematics": the root explorer is named for its roadmap.

    The root's own articles are its top level, so their topic and page are not
    named for the roadmap a second time.
    """
    project = _project(tmp_path)
    _edit(project / "blueprint/roadmap/README.md", "# Roadmap", "# Learning theory")
    out = tmp_path / "out"

    render_site(project / "blueprint", out, lean_root=project)

    project_payload = json.loads((out / "dependencies.json").read_text(encoding="utf-8"))
    chapter_payload = json.loads((out / "dependencies/chapters/roadmap.json").read_text(encoding="utf-8"))
    assert project_payload["title"] == "Learning theory"
    assert [node["title"] for node in project_payload["nodes"]] == ["Top level"]
    assert chapter_payload["title"] == "Top level"
    crumbs = [crumb["label"] for crumb in chapter_payload["view"]["breadcrumbs"]]
    assert crumbs == ["Book", "Learning theory", "Top level"]


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
    assert page.index("bp-legend-icon") < page.index("bp-dag-viewer")
    assert "<button" in page and 'aria-describedby="bp-legend-note"' in page
    assert ".bp-legend-tip:focus-within .bp-legend-note" in css


def test_the_site_stylesheet_leaves_the_explorer_to_its_own_rules() -> None:
    """The explorer carries its own CSS, and site rules for it filled in what that CSS left unset.

    A leftover canvas rule took one-finger scrolling from the landing map and
    stretched the drawing away from the node buttons on short screens.
    """
    assert ".bp-dag-" not in render_module._stylesheet()


@pytest.mark.parametrize("node_count", (2, 502), ids=("small", "large"))
def test_small_and_large_chapters_use_the_same_json_explorer(
    tmp_path: Path,
    node_count: int,
) -> None:
    blueprint = tmp_path / "blueprint"
    chapter = blueprint / "roadmap/large"
    chapter.mkdir(parents=True)
    (blueprint / "README.md").write_text("# Book\n\n- [Roadmap](roadmap/README.md)\n", encoding="utf-8")
    (blueprint / "roadmap/README.md").write_text("# Roadmap\n\n- [Large](large/README.md)\n", encoding="utf-8")
    (chapter / "README.md").write_text("# Large chapter\n", encoding="utf-8")
    coverage = blueprint / "coverage/README.md"
    coverage.parent.mkdir()
    coverage.write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
        "| Large | MAPPED | Synthetic scale fixture |\n",
        encoding="utf-8",
    )
    for index in range(node_count):
        dependency = (
            f"\n## Depends on\n\n- [Previous](node-{index - 1}.md)\n"
            if index
            else "\n## Depends on\n\nNo prerequisites.\n"
        )
        (chapter / f"node-{index}.md").write_text(
            f"---\ndeclaration: theorem\n---\n\n# Node {index}\n" + dependency,
            encoding="utf-8",
        )

    render_site(blueprint, tmp_path / "out")

    page = (tmp_path / "out/dependencies/chapters/large.md").read_text(encoding="utf-8")
    assert 'class="bp-dag-viewer"' in page
    assert 'data-graph-src="large.json"' in page
    assert "```mermaid" not in page
    assert page.count("blueprint-dag.js") == 1
    payload = json.loads((tmp_path / "out/dependencies/chapters/large.json").read_text(encoding="utf-8"))
    assert len(payload["nodes"]) == node_count
    assert payload["edge_count"] == node_count - 1


def test_the_structure_compatibility_page_opens_the_dependency_explorer(tmp_path: Path) -> None:
    """Hierarchy has one public UI, rather than a second repository-sized tree."""
    _render(tmp_path)
    page = (tmp_path / "out/structure.md").read_text(encoding="utf-8")
    summary = (tmp_path / "out/SUMMARY.md").read_text(encoding="utf-8")

    assert "# Browse the roadmap" in page
    assert "[Open the roadmap explorer](dependencies.md)" in page
    assert "bp-tree" not in page and "data-autoform-node-id" not in page
    assert "Browse all mathematics" not in summary
    assert "Vault structure" not in summary


def test_a_flat_roadmap_gets_the_same_bounded_browse_doorway(tmp_path: Path) -> None:
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
    assert "[Open the roadmap explorer](dependencies.md)" in page
    assert "Result a" not in page


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
    summary = (out / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Markdown source" not in summary
    assert summary.startswith("---\nsearch:\n  exclude: true\n---\n")
    # The two things the mirror was there for.
    assert 'id="top"' in chapter
    assert "blueprint/roadmap/top.md" in chapter


def test_the_vault_summary_keeps_its_statuses_in_plain_markdown(tmp_path: Path) -> None:
    """Obsidian never loads this stylesheet, so the site's legend markup would be inert there."""
    from autoform_cli.visualize import export_graph

    project = _project(tmp_path)
    document = export_graph(project / "blueprint").read_text(encoding="utf-8")

    assert "| Status | Count | Meaning |" in document
    assert "bp-legend" not in document and "<span" not in document


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


def _chapter_explorer_links(page: Path) -> set[str]:
    payload_path = page.parent / "dependencies.json" if page.name == "README.md" else page.with_suffix(".json")
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    return {
        match.group(1)
        for node in payload["nodes"]
        if isinstance(node.get("url"), str)
        and (match := re.fullmatch(r"(dependencies/chapters/.+)\.html", node["url"]))
    }


def test_the_home_page_and_dependency_page_share_one_project_explorer(tmp_path: Path) -> None:
    """The home page and Graph tab must consume the same project payload.

    A project-view node is a chapter, so its id is namespaced `scope:<group>`,
    and only the Graph tab stripped that before naming the page. The old home
    preview asked for `dependencies/chapters/scope:<group>.html`, which is nothing.
    """
    _render(tmp_path)
    out = tmp_path / "out"

    overview = (out / "README.md").read_text(encoding="utf-8")
    dependencies = (out / "dependencies.md").read_text(encoding="utf-8")
    assert 'data-graph-src="dependencies.json"' in overview
    assert 'data-graph-src="dependencies.json"' in dependencies
    assert overview.count("blueprint-dag.js") == 1
    links = _chapter_explorer_links(out / "README.md")
    assert links, "the landing page should carry the project explorer"
    assert links == _chapter_explorer_links(out / "dependencies.md")
    for link in links:
        assert (out / f"{link}.md").is_file(), f"home page links to a missing page: {link}"


def test_the_home_page_project_explorer_survives_named_chapters(tmp_path: Path) -> None:
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

    links = _chapter_explorer_links(out / "README.md")
    assert "dependencies/chapters/structure" in links
    assert not any("scope:" in link for link in links)
    for link in links:
        assert (out / f"{link}.md").is_file(), f"home page links to a missing page: {link}"


def _published_pages(out: Path) -> set[Path]:
    """Where MkDocs, with ``use_directory_urls: false``, publishes each page."""
    return {
        page.with_name("index.html") if page.stem in {"README", "index"} else page.with_suffix(".html")
        for page in out.rglob("*.md")
    }


def test_every_explorer_breadcrumb_reaches_a_published_page(tmp_path: Path) -> None:
    """MkDocs publishes ``README.md`` as ``index.html``.

    Breadcrumbs travel in the JSON payload, which MkDocs never rewrites, so
    the Book crumb that named ``README.html`` was dead on every explorer page.
    """
    project = _project(tmp_path)
    groups = project / "blueprint/roadmap/algebra/groups"
    groups.mkdir(parents=True)
    (groups.parent / "README.md").write_text("# Algebra\n\n- [Groups](groups/README.md)\n", encoding="utf-8")
    (groups / "README.md").write_text("# Groups\n\n- [Lemma](lemma.md)\n", encoding="utf-8")
    (groups / "lemma.md").write_text("---\ndeclaration: lemma\n---\n\n# Lemma\n\nA lemma.\n", encoding="utf-8")
    out = tmp_path / "out"

    def book_crumbs() -> set[Path]:
        """Check that every explorer's crumbs are published, and say where its Book crumb leads."""
        payloads = [out / "dependencies.json", *(out / "dependencies").rglob("*.json")]
        payloads.remove(out / "dependencies/index.json")
        assert out / "dependencies/scopes/algebra/groups.json" in payloads
        crumbs = [
            (payload, crumb["label"], crumb["url"])
            for payload in payloads
            for crumb in json.loads(payload.read_text(encoding="utf-8"))["view"]["breadcrumbs"]
            if crumb["url"] is not None
        ]
        assert sum(label == "Book" for _payload, label, _url in crumbs) == len(payloads)
        published = _published_pages(out)
        dead = [
            (payload.relative_to(out).as_posix(), label, url)
            for payload, label, url in crumbs
            if Path(os.path.normpath(payload.parent / unquote(url.partition("#")[0]))) not in published
        ]
        assert dead == []
        return {
            Path(os.path.normpath(payload.parent / unquote(url))) for payload, label, url in crumbs if label == "Book"
        }

    render_site(project / "blueprint", out, lean_root=project)

    # Every trail starts at the book's contents, which the Book tab opens, not at the home page.
    assert book_crumbs() == {out / "roadmap/index.html"}
    # The app layout hides a section explorer's other links, so it carries one to the section's own page.
    section = json.loads((out / "dependencies/scopes/algebra/groups.json").read_text(encoding="utf-8"))
    assert section["view"]["book"] == "../../../roadmap/algebra/groups/index.html"
    assert out / "roadmap/algebra/groups/index.html" in _published_pages(out)
    # The root article is the contents page without root leaves too. With neither
    # there is no contents page, so trails start at home.
    for name in ("base.md", "top.md"):
        (project / "blueprint/roadmap" / name).unlink()
    render_site(project / "blueprint", out, lean_root=project)
    assert book_crumbs() == {out / "roadmap/index.html"}
    (project / "blueprint/roadmap/README.md").unlink()
    render_site(project / "blueprint", out, lean_root=project)
    assert book_crumbs() == {out / "index.html"}


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


def test_inline_triple_backticks_do_not_hide_the_chapter_structure(tmp_path: Path) -> None:
    # Only a line that starts with the backticks can be mistaken for an opening fence.
    project = _project(tmp_path)
    readme = project / "blueprint/roadmap/README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "This chapter develops",
            "```x``` is inline code here.\n\nThis chapter develops",
        ),
        encoding="utf-8",
    )
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert page.index('class="bp-progress-overview"') < page.index("## Definitions") < page.index('id="base"')
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


def test_a_bullet_with_a_bracket_before_its_link_keeps_its_prose(tmp_path: Path) -> None:
    """A link label could reach back to an earlier "[" on its line, so a cited
    or ticked bullet read as a slot and its statement box replaced the line."""
    project = _project(tmp_path)
    (project / "blueprint/roadmap/README.md").write_text(
        "# Roadmap\n\n- [Hartshorne II.3] constructs [Base](base.md)\n- [x] [Top](top.md)\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")

    assert "- [Hartshorne II.3] constructs [Base](#base)\n- [x] [Top](#top)\n" in page
    assert page.index("## Additional formalization targets") < page.index('id="base"')


def test_a_label_holding_half_open_math_is_still_a_link(tmp_path: Path) -> None:
    """The site reads inline math before links, so the "]" in ``$(0,1]$`` does
    not end a label. Check follows such links, but render left the chapter out
    of the book's page order, did not take the bullet as a slot, and left a
    hoisted link pointing where it was filed."""
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    (blueprint / "sources").mkdir()
    (blueprint / "sources/paper.md").write_text("# Paper\n", encoding="utf-8")
    roadmap = blueprint / "roadmap/README.md"
    roadmap.write_text(roadmap.read_text(encoding="utf-8") + "- [On $(0,1]$](ch/README.md)\n", encoding="utf-8")
    chapter = blueprint / "roadmap/ch"
    (chapter / "theorems").mkdir(parents=True)
    (chapter / "README.md").write_text("# Chapter\n\n- [Half-open $(0,1]$](theorems/leaf.md)\n", encoding="utf-8")
    (chapter / "theorems/leaf.md").write_text(
        "---\ndeclaration: lemma\n---\n\n# Leaf\n\nA lemma.\n\n"
        "## Sources\n\n- [Lemma on $(0,1]$](../../../sources/paper.md)\n",
        encoding="utf-8",
    )

    render_site(blueprint, tmp_path / "out", lean_root=project, repository_url="", ref="")
    page = (tmp_path / "out/roadmap/ch/README.md").read_text(encoding="utf-8")

    assert "bp-book-nav" in page
    assert "Additional formalization targets" not in page
    assert "- [Lemma on $(0,1]$](../../sources/paper.md)\n" in page


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
    ref = json.loads((tmp_path / "out" / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["git_ref"]

    assert f"https://github.com/owner/repo/blob/{ref}/Project/Basic.lean#L5" in page
    assert '<a class="bp-code-link"' in page
    assert 'aria-label="View Project.top in Lean source"' in page
    assert '<svg class="bp-code-icon"' in page
    assert '<a class="bp-context-link" href="../dependencies/chapters/roadmap.html#node=top"' in page
    assert 'aria-label="Open dependency explorer for Top"' in page
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


def test_a_mathlib_article_shows_its_mathlib_declarations_as_text(tmp_path: Path) -> None:
    project = _project(tmp_path)
    article = (
        "---\ndeclaration: theorem\nmathlib: true\n{names}"
        "mathlib_file: Mathlib/Algebra/Group/Defs.lean\n---\n\n# Upstream\n"
    )
    upstream = project / "blueprint/roadmap/upstream.md"
    upstream.write_text(article.format(names=""), encoding="utf-8")
    without = render_site(project / "blueprint", tmp_path / "without", lean_root=project)
    upstream.write_text(article.format(names="mathlib_declaration: add_comm, «a<b&c»\n"), encoding="utf-8")
    shown = render_site(project / "blueprint", tmp_path / "shown", lean_root=project)

    row = (
        '<div class="bp-row"><span class="bp-key">Mathlib</span>'
        '<span class="bp-value"><code>add_comm</code> · <code>«a&lt;b&amp;c»</code></span></div>'
    )
    page = (tmp_path / "shown/roadmap/README.md").read_text(encoding="utf-8")
    assert page.count(row) == 1
    # The row is the page's only change, and --require-declarations ignores it.
    before = (tmp_path / "without/roadmap/README.md").read_text(encoding="utf-8")
    assert page.replace(f'<div class="bp-meta">{row}</div>\n', "") == before
    assert (shown.unresolved, shown.unchecked, shown.warnings) == (
        without.unresolved,
        without.unchecked,
        without.warnings,
    )


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


def test_module_catalog_is_separate_from_formalization_progress(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    sources = project / "blueprint/sources"
    sources.mkdir()
    (sources / "catalog.md").write_text("# Project.Basic declarations\n", encoding="utf-8")
    (project / "blueprint/roadmap/catalog.md").write_text(
        "---\ncatalog: module\nlean: Project.Base\nstatement: formalized\n"
        "proof: formalized\n---\n\n# Existing module\n\n"
        "A checked inventory of an existing Lean module.\n\n"
        "## Sources\n\n- [Declaration ledger](../sources/catalog.md)\n",
        encoding="utf-8",
    )
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        coverage.read_text(encoding="utf-8")
        + "| Existing module | INVENTORIED | [Catalog](../roadmap/catalog.md) |\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "out")

    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")
    assert "2 of 2 targets complete" in overview
    assert "3 of 3 targets complete" not in overview
    assert (
        '<div class="bp-figure bp-figure-neutral"><div class="bp-figure-value">1</div>'
        '<div class="bp-figure-label">Module inventories</div>'
        in overview
    )
    assert not (tmp_path / "out/roadmap/catalog.md").exists()
    project_payload = json.loads((tmp_path / "out/dependencies.json").read_text(encoding="utf-8"))
    roadmap = project_payload["nodes"][0]
    assert roadmap["member_count"] == 3
    assert roadmap["status_counts"] == [["fully_proved", 2], ["inventory_checked", 1]]
    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert '<span class="bp-thmcaption">Module</span><span class="bp-thmlabel">1</span>' in chapter
    assert "A checked inventory of an existing Lean module." in chapter
    assert "1 module inventory" in chapter
    # No slot places the catalog, and it is no formalization target.
    assert chapter.index("## Module inventories") < chapter.index('id="catalog"')
    assert "Additional formalization targets" not in chapter


def test_catalog_slot_with_bracketed_inline_code_is_absorbed(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    sources = project / "blueprint/sources"
    sources.mkdir()
    (sources / "catalog.md").write_text("# Project.Basic declarations\n", encoding="utf-8")
    (project / "blueprint/roadmap/catalog.md").write_text(
        "---\ncatalog: module\nlean: Project.Base\nstatement: formalized\n"
        "proof: formalized\n---\n\n# Existing module on `[0,1]^n`\n\n"
        "A checked inventory with bracketed mathematics in its title.\n\n"
        "## Sources\n\n- [Declaration ledger](../sources/catalog.md)\n",
        encoding="utf-8",
    )
    roadmap = project / "blueprint/roadmap/README.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8")
        + "\n## Existing modules\n\n"
        "- [Existing module on `[0,1]^n`](catalog.md)\n",
        encoding="utf-8",
    )
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        coverage.read_text(encoding="utf-8")
        + "| Existing module | INVENTORIED | [Catalog](../roadmap/catalog.md) |\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "out")

    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert not (tmp_path / "out/roadmap/catalog.md").exists()
    assert "](catalog.md)" not in chapter
    assert 'id="catalog"' in chapter
    assert "A checked inventory with bracketed mathematics in its title." in chapter
    assert chapter.index("## Existing modules") < chapter.index('id="catalog"')
    assert "- [Existing module on" not in chapter
    assert "## Additional formalization targets" not in chapter
    assert "## Module inventories" not in chapter


def test_a_catalog_links_a_few_lean_names_and_counts_the_rest() -> None:
    """A catalog's heading carried one identical code icon per public name, in a
    row that could not wrap, so a real inventory overflowed the page."""
    names = [f"Project.lemma_{index:02d}" for index in range(40)]
    node = SimpleNamespace(id="catalog", lean=", ".join(names), catalog="module")
    linker = SimpleNamespace(
        indexed=True, url=lambda name: f"{_REPOSITORY}/blob/abc/Project.lean#{name}", location=lambda name: None
    )

    icons, rows, linked, unresolved = render_module._lean_presentation(node, linker)

    assert (icons, linked, unresolved) == ("", 40, [])
    [(key, value)] = rows
    assert key == "Lean"
    assert value.count("<code>") == 8
    assert value.startswith(f'<a href="{_REPOSITORY}/blob/abc/Project.lean#Project.lemma_00"><code>')
    assert value.endswith("<code>Project.lemma_07</code></a> and 32 more, listed in its declaration ledger")


def test_partial_module_catalog_is_not_reported_as_dispatchable_work(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sources = project / "blueprint/sources"
    sources.mkdir()
    (sources / "catalog.md").write_text("# Project.Base declarations\n", encoding="utf-8")
    (project / "blueprint/roadmap/catalog.md").write_text(
        "---\ncatalog: module\nlean: Project.Base\nstatement: formalized\n---\n\n"
        "# Existing module\n\nAn incomplete module inventory.\n\n"
        "## Sources\n\n- [Declaration ledger](../sources/catalog.md)\n",
        encoding="utf-8",
    )
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        coverage.read_text(encoding="utf-8")
        + "| Existing module | INVENTORIED | [Catalog](../roadmap/catalog.md) |\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "out")

    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")
    assert (
        '<div class="bp-figure-value">0</div>'
        '<div class="bp-figure-label">Ready now</div>'
    ) in overview


def test_the_module_inventory_figure_counts_inventories_and_hides_at_zero(tmp_path: Path) -> None:
    """The figure counts every catalog, checked or not, so it says inventoried.

    A project without catalogs has nothing to report there, and a 0 beside the
    targets would read as missing work.
    """
    project = _project(tmp_path)
    render_site(project / "blueprint", tmp_path / "plain", lean_root=project)
    assert "Module inventories" not in (tmp_path / "plain/README.md").read_text(encoding="utf-8")

    sources = project / "blueprint/sources"
    sources.mkdir()
    (sources / "catalog.md").write_text("# Project.Base declarations\n", encoding="utf-8")
    (project / "blueprint/roadmap/catalog.md").write_text(
        "---\ncatalog: module\nlean: Project.Base\nstatement: formalized\n---\n\n"
        "# Existing module\n\nA module inventory that is not yet checked.\n\n"
        "## Sources\n\n- [Declaration ledger](../sources/catalog.md)\n",
        encoding="utf-8",
    )
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        coverage.read_text(encoding="utf-8")
        + "| Existing module | INVENTORIED | [Catalog](../roadmap/catalog.md) |\n",
        encoding="utf-8",
    )
    render_site(project / "blueprint", tmp_path / "inventoried", lean_root=project)

    overview = (tmp_path / "inventoried/README.md").read_text(encoding="utf-8")
    assert (
        '<div class="bp-figure-value">1</div>'
        '<div class="bp-figure-label">Module inventories</div>'
        '<div class="bp-figure-note">existing Lean modules inventoried</div>'
    ) in overview
    assert "existing Lean modules checked" not in overview
    # The rail hides the figure and counts the inventory in its line of counts.
    assert re.search(r'<p class="bp-hero-counts">\d+ chapters? · 1 module inventory</p>', overview)


def test_an_unclassified_catalog_warning_names_the_contract_once(tmp_path: Path) -> None:
    """A coverage-role issue with no line already names the contract.

    A catalog that no INVENTORIED row reaches has no contract line to cite,
    and its warning read "coverage contract: coverage contract leaves ...".
    """
    project = _project(tmp_path)
    sources = project / "blueprint/sources"
    sources.mkdir()
    (sources / "catalog.md").write_text("# Project.Base declarations\n", encoding="utf-8")
    (project / "blueprint/roadmap/catalog.md").write_text(
        "---\ncatalog: module\n---\n\n"
        "# Existing module\n\nA module inventory that no coverage row reaches.\n\n"
        "## Sources\n\n- [Declaration ledger](../sources/catalog.md)\n",
        encoding="utf-8",
    )

    report = render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert report.warnings == [
        "coverage contract leaves 1 catalog article outside INVENTORIED evidence "
        "(roadmap/catalog.md); link each inventory area directly or through a containing roadmap article"
    ]


def test_a_topic_of_unchecked_catalogs_counts_each_as_a_pending_inventory(tmp_path: Path) -> None:
    """The atlas counts every catalog that the hero above it counts.

    Scope items counted only checked catalogs, so a topic made of catalogs not
    yet checked read "0 items" under a hero that counted three: a blank status
    treatment, the smallest topic card, and catalog nodes that presented as ready to state.
    The book shows each catalog as the explorer does,
    and a reference to one is drawn as neutrally as the catalog itself.
    """
    blueprint = tmp_path / "blueprint"
    library = blueprint / "roadmap/library"
    library.mkdir(parents=True)
    (blueprint / "README.md").write_text("# Book\n\n- [Roadmap](roadmap/README.md)\n", encoding="utf-8")
    (blueprint / "roadmap/README.md").write_text("# Roadmap\n\n- [Library](library/README.md)\n", encoding="utf-8")
    (library / "README.md").write_text("# Library\n", encoding="utf-8")
    (blueprint / "sources").mkdir()
    (blueprint / "sources/library.md").write_text("# Library declarations\n", encoding="utf-8")
    for name in ("a", "b", "c"):
        (library / f"{name}.md").write_text(
            f"---\ncatalog: module\n---\n\n# Module {name.upper()}\n\n"
            "## Sources\n\n- [Declaration ledger](../../sources/library.md)\n",
            encoding="utf-8",
        )
    with (library / "b.md").open("a", encoding="utf-8") as stream:
        stream.write("\n## Depends on\n\n- [Module A](a.md)\n")
    coverage = blueprint / "coverage/README.md"
    coverage.parent.mkdir()
    coverage.write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
        "| Library | INVENTORIED | [Library](../roadmap/library/README.md) |\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    render_site(blueprint, out)

    overview = (out / "README.md").read_text(encoding="utf-8")
    assert (
        '<div class="bp-figure-value">3</div>'
        '<div class="bp-figure-label">Module inventories</div>'
    ) in overview
    topic = json.loads((out / "dependencies.json").read_text(encoding="utf-8"))["nodes"][0]
    assert topic["id"] == "scope:library"
    assert topic["status_counts"] == [["inventory_pending", 3]]
    assert topic["member_count"] == 3
    # An inventory never sets a scope's own status.
    assert topic["status"] == "planned"
    # Cards keep their labels readable; scope size changes height, not title width.
    assert topic["layout"]["width"] == 232
    assert topic["layout"]["height"] == 92
    assert "3 items across 1 chapter." in (out / "dependencies.md").read_text(encoding="utf-8")
    chapter = json.loads((out / "dependencies/chapters/library.json").read_text(encoding="utf-8"))
    assert {node["id"]: node["status"] for node in chapter["nodes"]} == dict.fromkeys(
        ("library/a", "library/b", "library/c"), "inventory_pending"
    )
    assert chapter["palette"]["inventory_pending"]["label"] == "inventory pending"
    book = (out / "roadmap/library/README.md").read_text(encoding="utf-8")
    for name in ("a", "b", "c"):
        assert f'<div class="bp-thmwrapper theorem-style-definition bp-planned" id="{name}" ' in book
    assert book.count('●<span class="bp-mark-label">inventory pending</span>') == 3
    assert '<a class="bp-ref bp-ref-planned" href="#a">Module 1 (Module A)</a>' in book
    assert "ready to state" not in book
    structure = (out / "structure.md").read_text(encoding="utf-8")
    assert "[Open the roadmap explorer](dependencies.md)" in structure
    assert "Module A" not in structure


def test_the_landing_page_is_one_full_page_explorer_opened_by_its_hero(
    tmp_path: Path,
) -> None:
    """A blueprint's subject is the shape of the project, not its front matter.

    The page stacked a hero, a framed map card with a title of its own, the
    explorer's own title inside the card and a legend under it, so it read as a
    column of headings with the map in a box. The hero now opens the explorer's
    hierarchy rail, and the explorer has the window.
    """
    _render(tmp_path)
    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")

    # The sidebar would hold a single "Home" entry here, so the page drops it.
    front = overview.split("---")[1]
    assert all(f"  - {part}" in front for part in ("navigation", "toc", "footer"))
    assert 'data-graph-src="dependencies.json"' in overview
    assert overview.index('class="bp-dag-viewer"') < overview.index('class="bp-dag-intro bp-landing-intro"')
    intro = overview.split('class="bp-dag-intro bp-landing-intro"', 1)[1]
    assert intro.index("bp-hero-title") < intro.index("bp-hero-figures") < intro.index("bp-intro-book")
    assert '<a class="bp-intro-book" href="roadmap/index.html">Read the book</a>' in intro
    # The hero's title is the page's one heading.
    assert overview.count("<h1") == 1 and "\n# " not in overview
    for retired in ("bp-map", 'class="bp-landing"', "open full-page", "## Status breakdown", "## Contents", "```mermaid"):
        assert retired not in overview
    assert overview.count("blueprint-dag.js") == 1
    assert (tmp_path / "out/dependencies.json").is_file()


def test_the_rail_hero_keeps_two_figures_a_counts_line_and_the_coverage_link(tmp_path: Path) -> None:
    """In the 288px rail the hero stacked a kicker, four gradient tiles whose notes repeated their labels, a bar
    and a coverage sentence before the book link and Next up."""
    _render(tmp_path)
    overview = (tmp_path / "out/README.md").read_text(encoding="utf-8")
    intro = overview.split('class="bp-dag-intro bp-landing-intro"', 1)[1]
    # The chapter and inventory figures stay in the markup, and the rail reads them as one line instead.
    assert intro.index("bp-hero-figures") < intro.index('<p class="bp-hero-counts">') < intro.index("bp-hero-bar")
    counts = re.search(r'<p class="bp-hero-counts">([^<]*)</p>', intro)
    assert counts is not None and re.fullmatch(r"\d+ chapters?( · \d+ module inventor(y|ies))?", counts.group(1))
    css = (tmp_path / "out/stylesheets/blueprint.css").read_text(encoding="utf-8")
    assert (
        ".bp-landing-intro.bp-landing-intro :is(\n  .bp-hero-kicker,\n  .bp-figure:nth-child(n + 3),\n"
        "  .bp-figure-note,\n  .bp-hero-bar,\n  .bp-hero-coverage-label,\n  .bp-hero-coverage-counts\n) { display: none; }"
    ) in css
    # The gradient rule is the narrow hero's one accent; its figures are plain.
    value = css.split(".bp-landing-intro.bp-landing-intro .bp-figure-value {", 1)[1].split("}", 1)[0]
    assert "background: none;" in value and "color: var(--bp-fg);" in value
    assert ".bp-landing-intro.bp-landing-intro .bp-hero-coverage-link { margin-left: 0; }" in css
    assert "last-child:nth-child(odd)" not in css


def test_the_legends_beside_the_explorer_count_what_its_rings_count(tmp_path: Path) -> None:
    """The legend beside a ring counts what the ring counts.

    A leaf marked not ready that declares nothing yet is not one of the hero's
    targets, and a catalog is inventory rather than a target, but the rings
    count both. A legend that left them out disagreed with the map it explains.
    """
    project = _project(tmp_path)
    roadmap = project / "blueprint/roadmap"
    (roadmap / "later.md").write_text(
        "---\nnot_ready: true\n---\n\n# Later\n\nA result still to be decomposed.\n",
        encoding="utf-8",
    )
    sources = project / "blueprint/sources"
    sources.mkdir()
    (sources / "catalog.md").write_text("# Project.Basic declarations\n", encoding="utf-8")
    ledger = "## Sources\n\n- [Declaration ledger](../sources/catalog.md)\n"
    (roadmap / "checked.md").write_text(
        "---\ncatalog: module\nlean: Project.Base\nstatement: formalized\n"
        "proof: formalized\n---\n\n# Checked module\n\n" + ledger,
        encoding="utf-8",
    )
    (roadmap / "pending.md").write_text(
        "---\ncatalog: module\n---\n\n# Pending module\n\n" + ledger,
        encoding="utf-8",
    )
    coverage = project / "blueprint/coverage/README.md"
    coverage.write_text(
        coverage.read_text(encoding="utf-8")
        + "| Existing modules | INVENTORIED | [Checked](../roadmap/checked.md), [Pending](../roadmap/pending.md) |\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    render_site(project / "blueprint", out, lean_root=project)

    ring = json.loads((out / "dependencies.json").read_text(encoding="utf-8"))["nodes"][0]
    assert ring["status_counts"] == [
        ["fully_proved", 2],
        ["not_ready", 1],
        ["inventory_checked", 1],
        ["inventory_pending", 1],
    ]
    rows = [
        f'<span class="bp-swatch bp-swatch-{key}"></span>'
        f'<span class="bp-legend-label">{label}</span>'
        '<span class="bp-legend-count">1</span>'
        for key, label in (
            ("not_ready", "not ready"),
            ("inventory_checked", "inventory checked"),
            ("inventory_pending", "inventory pending"),
        )
    ]
    overview = (out / "README.md").read_text(encoding="utf-8")
    assert "2 of 2 targets complete" in overview
    for legend in (
        (out / "dependencies.md").read_text(encoding="utf-8"),
        (out / "dependencies/chapters/roadmap.md").read_text(encoding="utf-8"),
    ):
        assert all(row in legend for row in rows)
        assert "it records inspection, not a mathematical proof." in legend
    # A swatch without a rule renders blank.
    css = (out / "stylesheets/blueprint.css").read_text(encoding="utf-8")
    for state in INVENTORY_STATES:
        assert f".bp-swatch-{state.key} {{ background: {state.fill}; border-color: {state.stroke}; }}" in css


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
    summary = (tmp_path / "out/SUMMARY.md").read_text(encoding="utf-8")
    assert summary.count("[Dependency explorer](dependencies.md)") == 1
    assert "Full dependency graph" not in summary
    # The Graph tab holds one explorer route, which Home also opens.
    assert "dependencies/full.md" not in summary


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


def test_a_link_to_the_chapters_own_directory_is_published(tmp_path: Path) -> None:
    """``./`` on a chapter page is the page's own directory, which is ``.`` once published."""
    project = _project(tmp_path)
    readme = project / "blueprint/roadmap/README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "This chapter develops",
            "See [this folder](./) and [its results](./#results). This chapter develops",
        ),
        encoding="utf-8",
    )
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert "[this folder](.)" in chapter
    assert "[its results](.#results)" in chapter


def test_a_hoisted_body_can_link_to_the_chapter_it_lands_on(tmp_path: Path) -> None:
    """``../`` from a leaf one level below its chapter names the page its body is published on."""
    project = _project(tmp_path)
    leaf = project / "blueprint/roadmap/chapter/definitions/deep.md"
    leaf.parent.mkdir(parents=True)
    (project / "blueprint/roadmap/chapter/README.md").write_text("# Chapter\n", encoding="utf-8")
    leaf.write_text(
        "---\ndeclaration: def\n---\n\n# Deep\n\nSee [the chapter](../).\n",
        encoding="utf-8",
    )
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    chapter = (tmp_path / "out/roadmap/chapter/README.md").read_text(encoding="utf-8")
    assert "[the chapter](.)" in chapter


def test_render_leaves_an_unpublished_folder_out(tmp_path: Path) -> None:
    project = _project(tmp_path)
    drafts = project / "blueprint" / "roadmap" / "drafts"
    drafts.mkdir()
    (drafts / "README.md").write_text("---\npublished: false\n---\n\n# Drafts\n", encoding="utf-8")
    article_id = "af_0123456789abcdef01234567"
    idea = drafts / "idea.md"
    idea.write_text(f"---\narticle_id: {article_id}\n---\n\n# Idea\n\nAn idea.\n", encoding="utf-8")
    # Its implementation note still belongs to an article, though render reads none of the folder.
    (project / "blueprint" / ".implementation-notes").mkdir()
    (project / "blueprint" / ".implementation-notes" / f"{article_id}.md").write_text("route\n", encoding="utf-8")
    # Nor is anything in it refused, since none of it is published.
    (drafts / "scratch.log").write_text("local\n", encoding="utf-8")
    revision = publication_source_revision(project / "blueprint")

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert (tmp_path / "out" / "roadmap" / "README.md").exists()
    assert not (tmp_path / "out" / "roadmap" / "drafts").exists()
    idea.write_text(idea.read_text(encoding="utf-8") + "\nAn edit.\n", encoding="utf-8")
    assert publication_source_revision(project / "blueprint") == revision


def test_render_leaves_atlas_json_out_of_the_site(tmp_path: Path) -> None:
    """atlas.json may name an article in an unpublished folder, and nothing on the site reads it.

    The explorer takes the published articles' areas from its own payload, and
    the dashboard still accepts the site the graph was read for.
    """
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    drafts = blueprint / "roadmap" / "drafts"
    drafts.mkdir()
    (drafts / "README.md").write_text("---\npublished: false\n---\n\n# Drafts\n", encoding="utf-8")
    (drafts / "idea.md").write_text("# Secret idea\n\nAn idea.\n", encoding="utf-8")
    (blueprint / "atlas.json").write_text(
        json.dumps({"schema": "autoform-atlas/v1", "areas": {"Algebra": ["base", "drafts/idea"]}}),
        encoding="utf-8",
    )
    out = tmp_path / "out"

    render_site(blueprint, out, lean_root=project)

    assert not [path for path in out.rglob("*") if path.name.casefold() == "atlas.json"]
    assert not [path for path in out.rglob("*") if path.is_file() and b"drafts/idea" in path.read_bytes()]
    payload = json.loads((out / "dependencies/chapters/roadmap.json").read_text(encoding="utf-8"))
    assert {node["id"]: node["area"] for node in payload["nodes"]}["base"] == "Algebra"
    manifest = json.loads((out / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert "atlas.json" not in manifest["files"]
    assert manifest["source_revision"] == publication_source_revision(blueprint)
    state = publication_bound_live_state(
        SimpleNamespace(list=lambda: []), blueprint_dir=blueprint, site_dir=out
    )()
    assert state["source_revision"] == manifest["source_revision"]
    render_site(blueprint, out, lean_root=project)  # the site it wrote may be replaced


def test_a_link_to_atlas_json_keeps_only_its_text(tmp_path: Path) -> None:
    """The site leaves atlas.json out, so a page's link to it would break a strict build."""
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    (blueprint / "atlas.json").write_text(
        json.dumps({"schema": "autoform-atlas/v1", "areas": {"Algebra": ["base"]}}), encoding="utf-8"
    )
    readme = blueprint / "roadmap" / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\nSee [the atlas](../atlas.json).\n", encoding="utf-8")
    out = tmp_path / "out"

    render_site(blueprint, out, lean_root=project)

    published = (out / "roadmap" / "README.md").read_text(encoding="utf-8")
    assert "See the atlas." in published
    assert "atlas.json" not in published


def test_render_does_not_warn_about_atlas_entries_for_unpublished_articles(tmp_path: Path) -> None:
    """Check reads the whole vault and accepts these entries; render, which reads less, accepts them too."""
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    drafts = blueprint / "roadmap" / "drafts"
    drafts.mkdir()
    (drafts / "README.md").write_text("---\npublished: false\n---\n\n# Drafts\n", encoding="utf-8")
    (drafts / "idea.md").write_text("# Idea\n\nAn idea.\n", encoding="utf-8")
    (blueprint / "atlas.json").write_text(
        json.dumps({"schema": "autoform-atlas/v1", "areas": {"Algebra": ["drafts", "drafts/idea", "missing"]}}),
        encoding="utf-8",
    )
    missing = "atlas.json: 'missing' under 'Algebra' is ignored, since no roadmap node has that id"

    report = render_site(blueprint, tmp_path / "out", lean_root=project)

    assert [warning for warning in report.warnings if warning.startswith("atlas.json")] == [missing]
    assert [f"atlas.json: {omission}" for omission in atlas_omissions(load_graph(blueprint))] == [missing]


def test_malformed_link_targets_are_published_as_written(tmp_path: Path) -> None:
    """Issue #305: an unbalanced IPv6 bracket or a decoded NUL stopped render in a traceback.

    Neither names a file, so each link is left as written, the way a URL is: in
    a statement, under Sources, and on a chapter page, which render also reads
    for statement slots and for the book's page order.
    """
    project = _project(tmp_path)
    ipv6, nul = "https://[2001:db8::1/paper", "../sources/a%00.md"
    top = project / "blueprint/roadmap/top.md"
    _edit(top, "The main result.", f"The main result, after [the paper]({ipv6}) and [the note]({nul}).")
    _edit(top, "- [Paper](../sources.md)", f"- [Paper]({ipv6})")
    readme = project / "blueprint/roadmap/README.md"
    _edit(readme, "- [Top](top.md)\n", f"- [Top](top.md)\n- [Reprint]({ipv6})\n- [Notes]({nul})\n")

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert f"after [the paper]({ipv6}) and [the note]({nul})." in chapter
    assert f"- [Paper]({ipv6})" in chapter
    assert f"- [Reprint]({ipv6})\n- [Notes]({nul})" in chapter


def test_a_protected_article_preserves_malformed_link_targets(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ipv6, nul = "https://[2001:db8::1/paper", "../sources/a%00.md"
    article = project / "blueprint/roadmap/top.md"
    _edit(article, "The main result.", f"For r<n, see [the paper]({ipv6}) and [the note]({nul}).")
    original = article.read_bytes()

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    tree = html5lib.parse(site_converter().convert(chapter), treebuilder="etree", namespaceHTMLElements=False)
    card = next(el for el in tree.iter() if el.get("id") == "top")
    statement = next(el for el in card.iter() if "bp-thmcontent" in el.get("class", "").split())
    assert [(el.text, el.get("href")) for el in _authored_body_elements(statement) if el.tag == "a"] == [
        ("the paper", ipv6), ("the note", nul),
    ]
    witnesses = [el for el in statement.iter() if "bp-body-link-validation" in el.get("class", "").split()]
    assert len(witnesses) == 1 and "hidden" in witnesses[0].attrib
    assert [el.get("href") for el in witnesses[0].iter("a")] == [ipv6, nul]
    assert " ".join("".join(statement.itertext()).split()) == "For r<n, see the paper and the note."
    assert article.read_bytes() == original


def test_unresolved_declarations_are_reported_not_linked(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/top.md").write_text(
        "---\ndeclaration: theorem\nstatement: formalized\nlean: Project.absent\n---\n"
        "\n# Top\n",
        encoding="utf-8",
    )
    report = render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert report.unresolved == ["declaration not found in the Lean sources: top: Project.absent"]
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert "not found in the Lean sources" in page


def test_a_declaration_beside_an_earlier_private_twin_is_located_at_the_public_one(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "Project" / "A.lean").write_text("private theorem Project.top : True := trivial\n", encoding="utf-8")

    report = render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert report.unresolved == []
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert '<code>Project.top</code> <span class="bp-where">Project/Basic.lean</span>' in page


def test_a_declaration_defined_twice_is_reported_not_located(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    (project / "Project" / "A.lean").write_text("theorem Project.top : True := trivial\n", encoding="utf-8")

    report = render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    message = (
        "declaration defined more than once in the Lean sources: top: Project.top "
        "(Project/A.lean:1, Project/Basic.lean:5)"
    )
    assert report.unresolved == [message]
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert '<code>Project.top</code> <span class="bp-missing">defined more than once in the Lean sources</span>' in page
    arguments = ["render", str(project / "blueprint"), "--output", str(tmp_path / "again"), "--require-declarations"]
    assert main([*arguments, "--lean-root", str(project)]) == 1
    assert f"warning: {message}" in capsys.readouterr().out.splitlines()


def test_stale_generated_files_are_not_republished(tmp_path: Path) -> None:
    project = _project(tmp_path)
    revision = publication_source_revision(project / "blueprint")
    (project / "blueprint/dependencies.html").write_text("stale", encoding="utf-8")
    (project / "blueprint/dependencies.json").write_text("stale", encoding="utf-8")
    (project / "blueprint/progress.md").write_text("stale", encoding="utf-8")
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert not (tmp_path / "out/dependencies.html").exists()
    assert (tmp_path / "out/dependencies.md").is_file()
    assert not (tmp_path / "out/progress.md").exists()
    # The capture never reads a generated name at the root, so no stale copy is hashed.
    assert publication_source_revision(project / "blueprint") == revision


def test_a_nested_article_named_like_a_generated_view_is_published(tmp_path: Path) -> None:
    """Generated views occupy only the publication root.

    Matching their names at any depth dropped an authored chapter's
    ``progress.md`` from the captured blueprint, so a dependency on it failed
    to resolve during render although ``check`` accepted the live tree.
    """
    project = _project(tmp_path)
    chapter = project / "blueprint/roadmap/ch"
    chapter.mkdir()
    (chapter / "README.md").write_text(
        "# Chapter\n\n- [Progress](progress.md)\n- [Uses](uses.md)\n- [Structure](structure.md)\n",
        encoding="utf-8",
    )
    (chapter / "progress.md").write_text(
        "---\ndeclaration: def\n---\n\n# Progress\n\nA definition named like a generated page.\n",
        encoding="utf-8",
    )
    (chapter / "uses.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# Uses\n\nA result.\n\n## Depends on\n\n- [Progress](progress.md)\n",
        encoding="utf-8",
    )
    (chapter / "structure.md").write_text("# Structure\n\nProse named like a generated page.\n", encoding="utf-8")

    report = render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    out = tmp_path / "out"
    assert report.nodes == 4
    search_index = json.loads((out / "dependencies/index.json").read_text(encoding="utf-8"))
    assert {"ch/progress", "ch/structure"} <= {node["id"] for node in search_index["nodes"]}
    assert 'id="progress"' in (out / "roadmap/ch/README.md").read_text(encoding="utf-8")
    assert "Prose named like a generated page." in (out / "roadmap/ch/structure.md").read_text(encoding="utf-8")
    vault_structure = (out / "structure.md").read_text(encoding="utf-8")
    assert "[Open the roadmap explorer](dependencies.md)" in vault_structure
    assert 'data-autoform-node-id="ch/progress"' not in vault_structure


def test_authored_mermaid_survives_while_generated_graph_pages_use_the_explorer(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    (project / "blueprint/notes.md").write_text(
        "# Notes\n\n```mermaid\ngraph LR\n  authored --> diagram\n```\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert "```mermaid" in (tmp_path / "out/notes.md").read_text(encoding="utf-8")
    graph_pages = [
        tmp_path / "out/dependencies.md",
        *(tmp_path / "out/dependencies").rglob("*.md"),
    ]
    assert all("```mermaid" not in page.read_text(encoding="utf-8") for page in graph_pages)


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

    # A rendered diagram cannot be restyled, so the script redraws authored
    # diagrams in Mermaid's theme for each scheme. Generated graphs are the
    # explorer, so it injects no status palette.
    assert '"light"' in script and '"dark"' in script
    assert "data-md-color-scheme" in script
    assert "MutationObserver" in script
    assert "classDef" not in script


_MERMAID_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");

function settle() { return new Promise((resolve) => setImmediate(resolve)); }

async function run(blocks, preloaded) {
  const calls = [], injected = [], observers = [], errors = [], selectors = [];
  let scheme = null, inFlight = 0, mostInFlight = 0;
  const elements = blocks.map((block) => ({className: block[0], textContent: block[1], innerHTML: ""}));
  const mermaid = {
    initialize: (config) => calls.push(
      ["initialize", config.securityLevel, config.htmlLabels, config.flowchart.htmlLabels, config.theme, config.secure,
       config.startOnLoad]
    ),
    // A render settles on a later turn, so two drawn at once would overlap.
    render: (id, text) => {
      calls.push(["render", text]);
      inFlight += 1;
      mostInFlight = Math.max(mostInFlight, inFlight);
      return settle().then(() => {
        inFlight -= 1;
        if (text.indexOf("broken") >= 0) throw new Error("parse error");
        return {svg: "<svg>" + text + "</svg>", bindFunctions: () => calls.push(["bind"])};
      });
    },
  };
  const context = {
    console: {error: (message) => errors.push(message)},
    document: {
      // Only the diagrams match, so a broader selector finds none.
      querySelectorAll: (selector) => { selectors.push(selector); return selector === ".mermaid" ? elements : []; },
      body: {getAttribute: () => scheme},
      createElement: () => ({}),
      head: {appendChild: (element) => injected.push(element)},
    },
    MutationObserver: function (callback) { observers.push(callback); this.observe = () => {}; },
  };
  if (preloaded) context.mermaid = mermaid;
  vm.createContext(context);
  vm.runInContext(source, context);
  if (!preloaded && injected.length) { context.mermaid = mermaid; injected[0].onload(); }
  // A switch to the dark scheme while the first pass is still drawing redraws
  // every diagram, still locked down, once that pass is done.
  scheme = "slate";
  observers.forEach((callback) => callback([{attributeName: "data-md-color-scheme"}]));
  for (let turn = 0; turn < 100; turn++) await settle();
  return {
    calls, errors, mostInFlight, selectors,
    sources: injected.map((element) => element.src),
    drawn: elements.map((element) => element.innerHTML),
  };
}

(async () => {
  console.log(JSON.stringify({
    none: await run([], false),
    loaded: await run(JSON.parse(process.argv[3]), false),
    preloaded: await run([["mermaid", "graph TD\n  c --> d"]], true),
  }));
})();
"""


def test_the_site_draws_authored_diagrams_locked_down_and_only_where_a_page_has_one(tmp_path: Path) -> None:
    """Generated maps are the explorer, so every diagram is one an author wrote.

    Strict mode drops click callbacks and labels draw as plain text, and a page
    without a diagram never loads Mermaid, while a site configuration that
    still loads it globally keeps working. An element of the class render once
    gave its own graphs is drawn the same way, and no palette is added to any
    diagram. Diagrams are drawn one at a time, a scheme change's redraw waits
    for the pass before it, and a diagram that fails leaves the others drawn.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    script = tmp_path / "blueprint-mermaid.js"
    script.write_text(render_module._mermaid_script(), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_MERMAID_HARNESS, encoding="utf-8")
    blocks = [
        ("mermaid bp-graph", "graph LR\n  a --> b"),
        ("mermaid", 'flowchart LR\n  A\n  click A call eval("document.title=1")'),
        ("mermaid", "sequenceDiagram\n  A->>B: hi"),
        ("mermaid", "graph LR\n  broken"),
        ("mermaid", "graph TD\n  later --> drawn"),
    ]

    result = json.loads(
        subprocess.run(
            [node, str(harness), str(script), json.dumps(blocks)], capture_output=True, text=True, check=True
        ).stdout
    )

    assert result["none"] == {
        "calls": [], "errors": [], "mostInFlight": 0, "selectors": [".mermaid"], "sources": [], "drawn": []
    }
    loaded = result["loaded"]
    assert loaded["selectors"] == [".mermaid"]
    assert loaded["sources"] == [render_module.MERMAID_LIBRARY]
    # Each diagram is drawn from its source as written, strictly and with
    # plain-text labels that no directive can turn back into HTML, the whole
    # light pass before the dark one.
    secure = ["secure", "securityLevel", "startOnLoad", "maxTextSize", "suppressErrorRendering", "maxEdges", "htmlLabels"]
    assert loaded["calls"] == [
        call
        for theme in ("neutral", "dark")
        for _classes, diagram in blocks
        for call in (
            ["initialize", "strict", False, False, theme, secure, False],
            ["render", diagram],
            *([] if "broken" in diagram else [["bind"]]),
        )
    ]
    assert loaded["mostInFlight"] == 1
    assert loaded["errors"] == ["autoform: a diagram could not be drawn"] * 2
    assert loaded["drawn"] == ["" if "broken" in diagram else f"<svg>{diagram}</svg>" for _classes, diagram in blocks]
    preloaded = result["preloaded"]
    assert preloaded["sources"] == []
    assert preloaded["calls"][:3] == [
        ["initialize", "strict", False, False, "neutral", secure, False],
        ["render", "graph TD\n  c --> d"],
        ["bind"],
    ]
    assert preloaded["drawn"] == ["<svg>graph TD\n  c --> d</svg>"]


def test_the_generated_script_is_valid_javascript(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    _render(tmp_path)
    for name in ("blueprint-mermaid.js", "blueprint-dag.js"):
        script = tmp_path / "out/javascripts" / name
        result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_render_writes_no_mermaid_graph(tmp_path: Path) -> None:
    """The explorer draws render's maps, so no page render writes holds a
    Mermaid graph, and every diagram on the site is one an author wrote."""

    _render(tmp_path)
    out = tmp_path / "out"

    for name in ("README.md", "dependencies.md", "dependencies/full.md", "dependencies/chapters/roadmap.md"):
        page = (out / name).read_text(encoding="utf-8")
        assert "```mermaid" not in page, name
        assert 'class="mermaid' not in page, name
        assert 'class="bp-dag-viewer"' in page, name


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

    monkeypatch.setattr("autoform_cli.render.snapshot_project_sources", fail_linker)

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
            "counts": {
                "DECOMPOSED": 0,
                "DEFERRED": 0,
                "INVENTORIED": 0,
                "MAPPED": 1,
                "OUT": 0,
            },
            "schema": "autoform-coverage/v2",
            "source_path": "coverage/README.md",
            "source_sha256": manifest["coverage"]["source_sha256"],
        },
        "dependencies": 1,
        "directories": manifest["directories"],
        "files": manifest["files"],
        "git_ref": ref,
        "lean_source_revision": manifest["lean_source_revision"],
        "nodes": 3,
        "schema": "autoform-publication/v3",
        "source": "blueprint/roadmap Markdown",
        "source_revision": manifest["source_revision"],
        "views": ["book", "progress", "project", "chapter", "full"],
    }
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["source_revision"])
    assert manifest["files"] == {
        path: hashlib.sha256(data).hexdigest() for path, data in first.items() if path != PUBLICATION_MANIFEST
    }
    assert str(tmp_path).encode() not in b"".join(first.values())


def test_publication_revision_frames_binary_paths_and_contents() -> None:
    def snapshot(files: tuple[tuple[str, bytes], ...]):
        return render_module.TreeSnapshot(
            directories=("",),
            files=files,
            symlinks=(),
            special=(),
            omitted=(),
            identities=(),
        )

    combined = snapshot((("a", b"x\0b\0y"),))
    split = snapshot((("a", b"x"), ("b", b"y")))

    assert render_module._source_revision(combined) != render_module._source_revision(split)


def test_render_verifies_one_stable_revision_for_both_manifest_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    revision = "a" * 64
    revision_calls = 0
    manifests: list[str] = []
    original_write = render_module._write_publication_manifest

    def source_revision(_snapshot) -> str:
        nonlocal revision_calls
        revision_calls += 1
        return revision

    def write_manifest(*args, **kwargs) -> None:
        manifests.append(kwargs["source_revision"])
        original_write(*args, **kwargs)

    monkeypatch.setattr(render_module, "_source_revision", source_revision)
    monkeypatch.setattr(render_module, "_write_publication_manifest", write_manifest)

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert revision_calls == 1
    assert manifests == [revision, revision]
    published = json.loads((tmp_path / "out" / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert published["source_revision"] == revision


def test_render_uses_one_capture_when_authored_sources_change_and_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    overview = blueprint / "README.md"
    original = overview.read_bytes()
    output = tmp_path / "out"
    original_write = render_module._write_publication_manifest
    original_graph_pages = render_module.graph_pages.write_graph_pages
    changed = False

    def write_manifest(*args, **kwargs) -> None:
        nonlocal changed
        original_write(*args, **kwargs)
        if not changed:  # the first write, before any page exists
            changed = True
            overview.write_bytes(original + b"\nTRANSIENT_ABA\n")

    def write_graph_pages(*args, **kwargs):
        overview.write_bytes(original)
        return original_graph_pages(*args, **kwargs)

    monkeypatch.setattr(render_module, "_write_publication_manifest", write_manifest)
    monkeypatch.setattr(render_module.graph_pages, "write_graph_pages", write_graph_pages)

    render_site(blueprint, output, lean_root=project)

    assert b"TRANSIENT_ABA" not in (output / "README.md").read_bytes()
    manifest = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["source_revision"] == publication_source_revision(blueprint)


def test_anchored_links_resolve_only_entries_that_are_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = tmp_path / "site/current.md"
    targets = {
        f"node-{index}": (tmp_path / f"site/chapter-{index}.md", f"node-{index}")
        for index in range(10_000)
    }
    calls: list[tuple[Path, Path, str]] = []
    original_relative_link = render_module.relative_link

    def relative_link(target: Path, output: Path, extension: str) -> str:
        calls.append((target, output, extension))
        return original_relative_link(target, output, extension)

    monkeypatch.setattr(render_module, "relative_link", relative_link)
    links = render_module._anchored_links(targets, page)

    assert calls == []
    expected = "chapter-9999.html#node-9999"
    assert links["node-9999"] == expected
    assert links["node-9999"] == expected
    assert len(calls) == 1

    # An explorer page copies only the links it draws.
    selected = render_module._node_links(targets, page, ("node-2", "node-2", "node-7"))
    assert selected == {"node-2": "chapter-2.html#node-2", "node-7": "chapter-7.html#node-7"}


def test_the_source_revision_of_a_blueprint_behind_a_link_that_cannot_be_read_is_its_own(
    tmp_path: Path, unreadable_link: Callable[[Path, Path | str], None]
) -> None:
    (tmp_path / "real/blueprint/roadmap").mkdir(parents=True)
    (tmp_path / "real/blueprint/roadmap/README.md").write_bytes(b"# Roadmap\n")
    unreadable_link(tmp_path / "via", tmp_path / "real")

    assert publication_source_revision(tmp_path / "via/blueprint") == publication_source_revision(
        tmp_path / "real/blueprint"
    )


@pytest.mark.parametrize("through_the_link", [True, False], ids=["lean-root-through-the-link", "lean-root-as-itself"])
def test_a_site_rendered_through_a_link_that_cannot_be_read_keeps_its_source_links(
    tmp_path: Path, unreadable_link: Callable[[Path, Path | str], None], through_the_link: bool
) -> None:
    # The blueprint and the Lean root are compared by spelling, so they resolve alike.
    repo = tmp_path / "real" / "repo"
    (repo / "docs").mkdir(parents=True)
    _project(repo / "docs")
    ref = _commit_project(repo)
    unreadable_link(tmp_path / "via", tmp_path / "real")
    via_repo = tmp_path / "via" / "repo"

    render_site(
        via_repo / "docs" / "project" / "blueprint",
        tmp_path / "out",
        lean_root=via_repo if through_the_link else repo,
        repository_url="https://github.com/owner/repo",
        ref=ref,
    )

    pages = "".join(page.read_text(encoding="utf-8") for page in sorted((tmp_path / "out").rglob("*.md")))
    assert f"blob/{ref}/docs/project/blueprint/" in pages


def test_an_output_holding_a_blueprint_behind_a_link_that_cannot_be_read_is_refused(
    tmp_path: Path, unreadable_link: Callable[[Path, Path | str], None]
) -> None:
    _project(tmp_path / "real")
    unreadable_link(tmp_path / "via", tmp_path / "real")
    project = tmp_path / "via" / "project"

    with pytest.raises(PublicationError, match="must be disjoint"):
        render_site(project / "blueprint", project)


def test_the_publication_source_of_a_blueprint_behind_a_link_that_cannot_be_read_is_its_own(
    tmp_path: Path, unreadable_link: Callable[[Path, Path | str], None]
) -> None:
    _project(tmp_path / "real")
    unreadable_link(tmp_path / "via", tmp_path / "real")

    revision, graph = load_publication_source(tmp_path / "via" / "project" / "blueprint")
    direct_revision, direct = load_publication_source(tmp_path / "real" / "project" / "blueprint")

    assert revision == direct_revision
    assert sorted(graph.nodes) == sorted(direct.nodes)


def test_the_source_revision_of_a_fixed_blueprint_is_pinned(tmp_path: Path) -> None:
    """Sites and the dashboard compare this hash, so a framing change would mark every site stale."""
    (tmp_path / "blueprint/roadmap").mkdir(parents=True)
    (tmp_path / "blueprint/roadmap/README.md").write_bytes(b"# Roadmap\n")

    assert publication_source_revision(tmp_path / "blueprint") == (
        "04080a4d2406a4444f585fca13b3006f0e670e2ed699911595417c0e83da3e4b"
    )


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
        "counts": {
            "DECOMPOSED": 1,
            "DEFERRED": 0,
            "INVENTORIED": 0,
            "MAPPED": 1,
            "OUT": 1,
        },
        "schema": "autoform-coverage/v2",
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


def _tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


_V2 = '{"complete": true, "schema": "autoform-publication/v2"}'


def _debris(parent: Path) -> list[Path]:
    return sorted(parent.glob(".autoform-publication-*"))


def _published(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes]]:
    project = _project(tmp_path)
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    return project, output, _tree(output)


def _manifest(output: Path, text: str) -> None:
    (output / PUBLICATION_MANIFEST).write_text(text, encoding="utf-8")


def _edit(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (lambda out: (out / "stale.txt").write_text("user\n"), "untracked or missing files: stale.txt"),
        (lambda out: (out / "README.md").write_text("edited\n"), "modified Autoform publication"),
        (lambda out: (out / "assets/empty").mkdir(), "untracked or missing directories"),
        (lambda out: _manifest(out, _V2), "file inventory"),
        (lambda out: _manifest(out, _V2.replace("v2", "v3")), "file inventory"),
        (lambda out: _manifest(out, _V2.replace("v2", "v1")), "legacy autoform-publication/v1 site at .* once"),
        (lambda out: _manifest(out, '{"complete": true, "schema": {"$ref": "v3"}}'), "non-Autoform output"),
    ],
)
def test_render_replaces_only_an_exact_owned_publication(tmp_path: Path, tamper, message: str) -> None:
    project, output, _site = _published(tmp_path)
    render_site(project / "blueprint", output, lean_root=project)
    tamper(output)
    before = _tree(output)

    with pytest.raises(PublicationError, match=message):
        render_site(project / "blueprint", output, lean_root=project)

    assert _tree(output) == before
    assert not _debris(tmp_path)


def test_render_says_when_the_explorer_replaces_an_authored_file(tmp_path: Path) -> None:
    """A blueprint file at a path the explorer writes is not published, and render says so."""
    project = _project(tmp_path)
    (project / "blueprint/dependencies.json").write_text('{"authored": true}\n', encoding="utf-8")
    script = project / "blueprint/javascripts/blueprint-dag.js"
    script.parent.mkdir()
    script.write_text("/* authored */\n", encoding="utf-8")
    out = tmp_path / "out"

    report = render_site(project / "blueprint", out, lean_root=project)

    assert [warning for warning in report.warnings if "dependency explorer" in warning] == [
        f"{path} is not published: the dependency explorer writes its own file there"
        for path in ("dependencies.json", "javascripts/blueprint-dag.js")
    ]
    assert json.loads((out / "dependencies.json").read_text(encoding="utf-8"))["schema"] == "autoform-dag-view/v2"
    assert "/* authored */" not in (out / "javascripts/blueprint-dag.js").read_text(encoding="utf-8")


def test_render_replaces_an_exact_v2_publication(tmp_path: Path) -> None:
    """A v2 site records the same file inventory, so render checks and replaces it like a v3 one."""
    project, output, _site = _published(tmp_path)
    manifest = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    _manifest(output, json.dumps({**manifest, "schema": "autoform-publication/v2"}))

    render_site(project / "blueprint", output, lean_root=project)

    replaced = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert replaced["schema"] == "autoform-publication/v3"
    assert not _debris(tmp_path)


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


def test_implementation_notes_stay_out_of_the_published_wiki(tmp_path: Path) -> None:
    """Notes are for the next worker, not the reader.

    Editing them changes nothing a reader can see, so it must not mark the
    published site stale either.
    """
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    article_id = "af_0123456789abcdef01234567"
    top = blueprint / "roadmap/top.md"
    top.write_text(
        top.read_text(encoding="utf-8").replace(
            "---\n", f"---\narticle_id: {article_id}\n", 1
        ),
        encoding="utf-8",
    )
    before = publication_source_revision(blueprint)
    (blueprint / "agents.md").write_text("# Agents in the source material\n", encoding="utf-8")
    with_public_page = publication_source_revision(blueprint)
    assert with_public_page != before
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / f"{article_id}.md").write_text("Mathlib gap.\n", encoding="utf-8")

    render_site(blueprint, tmp_path / "out", lean_root=project)

    assert not (tmp_path / "out/.implementation-notes").exists()
    assert (tmp_path / "out/agents.md").is_file()
    assert ".implementation-notes" not in (tmp_path / "out/structure.md").read_text(
        encoding="utf-8"
    )
    assert publication_source_revision(blueprint) == with_public_page


def test_an_orphaned_implementation_note_stops_the_render(tmp_path: Path) -> None:
    """Render and the dashboard read a copy without hidden files, but still check the notes."""
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    notes = blueprint / ".implementation-notes"
    notes.mkdir()
    (notes / "af_0123456789abcdef01234567.md").write_text("Mathlib gap.\n", encoding="utf-8")

    with pytest.raises(GraphValidationError, match="names no roadmap article"):
        render_site(blueprint, tmp_path / "out", lean_root=project)
    with pytest.raises(GraphValidationError, match="names no roadmap article"):
        load_publication_source(blueprint)

    assert not (tmp_path / "out").exists()


def test_the_dashboard_copy_holds_the_atlas_the_graph_reads(tmp_path: Path) -> None:
    """The dashboard loads its graph from a copy of the articles and atlas.json.

    A copy of the roadmap alone lost the areas that atlas.json assigns. The
    catalog's ledger stays out of the copy, since the graph never reads it.
    """
    project = _project(tmp_path)
    blueprint = project / "blueprint"
    (blueprint / "sources").mkdir()
    (blueprint / "sources/catalog.md").write_text("# Project.Base declarations\n", encoding="utf-8")
    (blueprint / "roadmap/catalog.md").write_text(
        "---\ncatalog: module\n---\n\n# Existing module\n\n"
        "## Sources\n\n- [Declaration ledger](../sources/catalog.md)\n",
        encoding="utf-8",
    )
    (blueprint / "atlas.json").write_text(
        json.dumps({"schema": "autoform-atlas/v1", "areas": {"Algebra": ["catalog"]}}),
        encoding="utf-8",
    )

    _revision, graph = load_publication_source(blueprint)

    assert graph.nodes["catalog"].catalog == "module"
    assert graph.nodes["catalog"].area == "Algebra"


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


def test_a_published_link_out_of_the_blueprint_keeps_only_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The site holds only the blueprint, so the link could only dangle.

    Source notes stay published when readers cannot be sent to the repository,
    and they often cite Lean files beside the blueprint. MkDocs reported each
    such link as missing, which failed a strict build.
    """
    for variable in ("GITHUB_REPOSITORY", "GITHUB_SERVER_URL", "GITHUB_SHA"):
        monkeypatch.delenv(variable, raising=False)
    project = _with_source_notes(tmp_path)
    paper = project / "blueprint" / "sources" / "paper.md"
    paper.write_text(
        paper.read_text(encoding="utf-8")
        + "\nEvidence on $[0,1]$: [Top.lean](../../Project/Top.lean#L5) and [`Lean`](../../lean/) for [Top](../roadmap/top.md).\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    render_site(project / "blueprint", out, lean_root=project, repository_url="", ref="")

    published = (out / "sources/paper.md").read_text(encoding="utf-8")
    assert "Evidence on $[0,1]$: Top.lean and `Lean` for [Top](" in published
    assert "../../Project/Top.lean" not in published
    assert "../../lean/" not in published


def test_a_bracket_in_the_text_does_not_widen_a_link(tmp_path: Path) -> None:
    """A link label could reach back to an earlier "[" on its line, so an image
    after a citation was published as text, and a dropped link's half-open
    interval lost its own bracket instead of the link's. Links are read as the
    site reads them: a label's brackets come in pairs; code, math and escaped
    brackets hold no link syntax; and an angle-bracketed target may hold spaces."""
    from autoform_cli.render import _rewrite_links

    blueprint = tmp_path / "blueprint"
    kept = (
        "See [1] and ![outside figure](../../figures/f.png) here.\n"
        "See [note ![fig](../../figures/f.png) too.\n"
        "Write `[Top](../../Project/Top.lean)` for a link.\n"
    )
    text = kept + (
        "Proof of [continuity on $[0,1)$](../../Project/Top.lean#L5).\n"
        "Cited in [Lemma [3]](../../Project/Top.lean).\n"
        "A [half-open \\[0,1) range](../../Project/Top.lean) here.\n"
        "Data in [the table](<../../data/a b.csv>).\n"
    )

    rewritten = _rewrite_links(
        text,
        source_dir=blueprint / "sources",
        page=tmp_path / "out/sources/paper.md",
        blueprint=blueprint,
        destination=tmp_path / "out",
        node_sources={},
        targets={},
    )

    assert rewritten == kept + (
        "Proof of continuity on $[0,1)$.\n"
        "Cited in Lemma [3].\n"
        "A half-open \\[0,1) range here.\n"
        "Data in the table.\n"
    )


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


@pytest.mark.parametrize(
    ("repository_url", "definition"),
    [
        (_REPOSITORY, "[paper]: https://github.com/owner/repo/blob/{ref}/blueprint/sources/paper%20note.md"),
        ("", "[paper]: ../sources/paper%20note.md"),
    ],
)
def test_angle_bracket_reference_destinations_with_spaces_are_rewritten(
    tmp_path: Path, repository_url: str, definition: str
) -> None:
    """An angle-bracket destination may contain spaces and must stay whole.

    Without repository coordinates the note stays on the site, and
    Python-Markdown reads a definition's destination only up to whitespace, in
    angle brackets or not, so its space is encoded.
    """
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
        repository_url=repository_url,
        ref=ref,
    )

    chapter = (out / "roadmap/README.md").read_text(encoding="utf-8")
    assert f'{definition.format(ref=ref)} "Source note"' in chapter
    assert "<../sources/paper note.md>" not in chapter


def _render_statement_link(
    tmp_path: Path, link: str, *, notes: tuple[str, ...] = (), committed: bool = True
) -> tuple[str, str]:
    """Render a project whose statement body carries *link*, with the extra
    source *notes*, from a commit with repository coordinates if *committed*;
    return the chapter and the commit."""
    project = _with_source_notes(tmp_path)
    for name in ("Smith 2019.pdf", "Smith(2019).pdf"):
        (project / "blueprint/sources" / name).write_bytes(b"%PDF-1.4\n")
    for name in notes:
        (project / "blueprint/sources" / name).write_text("---\n---\n\n# Note\n", encoding="utf-8")
    _edit(project / "blueprint/roadmap/top.md", "The main result.", f"The main result. See {link}.")
    commit = _commit_project(project) if committed else ""
    out = tmp_path / "out"

    render_site(
        project / "blueprint", out, lean_root=project, repository_url=_REPOSITORY if committed else "", ref=commit
    )

    return (out / "roadmap/README.md").read_text(encoding="utf-8"), commit


def test_angle_bracket_link_destinations_with_spaces_are_rewritten_whole(tmp_path: Path) -> None:
    """Cut at the space, the link was published as `[Smith](<../sources/Smith)`."""
    chapter, commit = _render_statement_link(tmp_path, "[Smith](<../sources/Smith 2019.pdf>)")

    assert f"[Smith]({_REPOSITORY}/blob/{commit}/blueprint/sources/Smith%202019.pdf)" in chapter


def test_a_site_relative_link_with_a_decoded_space_or_parenthesis_is_bracketed(tmp_path: Path) -> None:
    """Without repository coordinates the note stays on the site under its
    decoded path, whose space or unbalanced `(` a bare destination cannot hold."""
    links = "[Smith](../sources/Smith%202019.pdf) and [x](../sources/a%28b.md)"
    chapter, _ = _render_statement_link(tmp_path, links, committed=False)

    tree = html5lib.parse(site_converter().convert(chapter), treebuilder="etree", namespaceHTMLElements=False)
    card = next(el for el in tree.iter() if el.get("id") == "top")
    statement = next(el for el in card.iter() if "bp-thmcontent" in el.get("class", "").split())
    # Exact destinations and the whole visible sentence guard against decoding
    # '(' as a bare-link delimiter and swallowing the following text.
    assert [(el.text, el.get("href")) for el in _authored_body_elements(statement) if el.tag == "a"] == [
        ("Smith", "../sources/Smith 2019.pdf"), ("x", "../sources/a(b.html"),
    ]
    assert " ".join("".join(statement.itertext()).split()) == "The main result. See Smith and x."


def test_math_that_reads_like_an_unmoved_link_is_left_alone(tmp_path: Path) -> None:
    """Read as links, `$[X,Y](f(p))$` gained angle brackets and a composite
    argument was dropped as a title, though neither destination moves."""
    math = r"$[X,Y](f(p))$ and $[X,Y](f \circ g)$"
    chapter, _ = _render_statement_link(tmp_path, math)

    assert f"See {math}." in chapter


def test_a_bare_link_with_balanced_parentheses_is_rewritten_whole(tmp_path: Path) -> None:
    """Read only to its first `)`, the target named `Smith(2019`, a file the
    blueprint lacks, so the note got a repository URL that 404s, or, kept on
    the site, a link the strict build rejects."""
    chapter, commit = _render_statement_link(tmp_path, "[Smith](../sources/Smith(2019).pdf)")

    assert f"[Smith]({_REPOSITORY}/blob/{commit}/blueprint/sources/Smith%282019%29.pdf)" in chapter


def test_an_unbalanced_bare_link_leaves_the_rest_of_its_line_alone(tmp_path: Path) -> None:
    """Python-Markdown reads `[x]` as text and `[y]` as a link, so both stay
    that way. Had `a(b.md)` counted as balanced, taking the link's own `)`,
    the title after it would run on to `[y]`'s and drop ` and [y](../sources/c.md`."""
    line = "[x](../sources/a(b.md) and [y](../sources/c.md)"
    chapter, commit = _render_statement_link(tmp_path, line, notes=("a(b.md", "c.md"))

    y = f"{_REPOSITORY}/blob/{commit}/blueprint/sources/c.md"
    assert f"The main result. See [x](../sources/a(b.md) and [y]({y})." in chapter


def test_an_escaped_parenthesis_leaves_the_rest_of_its_line_alone(tmp_path: Path) -> None:
    """Python-Markdown reads `\\(` as text, so `[x]` ends at its own `)`.
    Counted as an open parenthesis, `\\(` took that `)`, and the title after it
    ran on to the `)` closing the aside and dropped ` and [y](../sources/c.md)`.
    The escape is not decoded, so `x` names no captured note and is left as
    written, where the strict build reports it."""
    line = r"(see [x](../sources/a\(b.md) and [y](../sources/c.md))"
    chapter, commit = _render_statement_link(tmp_path, line, notes=("a(b.md", "c.md"))

    y = f"{_REPOSITORY}/blob/{commit}/blueprint/sources/c.md"
    assert f"The main result. See {line.replace('../sources/c.md', y)}." in chapter


def test_a_title_with_parentheses_ends_where_its_link_does(tmp_path: Path) -> None:
    """Ended at the first `)`, `"Smith (2019)"` left `")` on the page. A quoted
    title, in either quote and with or without a space before the `)`, may also
    leave a `(` open, as Python-Markdown reads it; counted as open, it would
    carry the match on to a later `)` and drop the text before it."""
    chapter, commit = _render_statement_link(
        tmp_path,
        '[the paper](../sources/paper.md "Smith (2019)")'
        ' (or [its note](../sources/paper.md "Smith (2019" ) for short,'
        " [the note](../sources/paper.md 'Smith (2019') in full)",
    )

    paper = f"{_REPOSITORY}/blob/{commit}/blueprint/sources/paper.md"
    assert f"See [the paper]({paper}) (or [its note]({paper}) for short, [the note]({paper}) in full)." in chapter


def test_a_query_on_a_source_note_link_is_kept_out_of_the_file_name(tmp_path: Path) -> None:
    """Resolved as part of the path, `?plain=1` reached the repository as `paper.md%3Fplain%3D1`."""
    chapter, commit = _render_statement_link(tmp_path, "[the source](../sources/paper.md?plain=1#L3)")

    assert f"[the source]({_REPOSITORY}/blob/{commit}/blueprint/sources/paper.md?plain=1#L3)" in chapter


def test_a_missing_source_note_keeps_the_link_a_strict_build_rejects(tmp_path: Path) -> None:
    """Every path under `sources/` went to the repository, so a missing note got
    a link that 404s. Its site-relative link names a page the site does not
    publish, so `mkdocs build --strict` fails instead of deploying it."""
    chapter, _ = _render_statement_link(tmp_path, "[lemma 3](../sources/lemma-3.md)")

    assert "[lemma 3](../sources/lemma-3.md)" in chapter


def test_a_hidden_source_note_keeps_its_repository_link(tmp_path: Path) -> None:
    """The capture skips hidden files, so the existence check kept a committed
    hidden note's link on the site, which does not publish the note, and the
    strict build failed on a link that resolved in the repository."""
    chapter, commit = _render_statement_link(tmp_path, "[the draft](../sources/.draft.md)", notes=(".draft.md",))

    assert f"[the draft]({_REPOSITORY}/blob/{commit}/blueprint/sources/.draft.md)" in chapter


def test_a_link_to_the_sources_directory_names_its_repository_tree(tmp_path: Path) -> None:
    """The existence check lets the directory through. A check for a file would
    publish `../sources`, which the site lacks and a strict build only logs."""
    chapter, commit = _render_statement_link(tmp_path, "[the notes](../sources/)")

    assert f"[the notes]({_REPOSITORY}/tree/{commit}/blueprint/sources)" in chapter


def test_a_query_on_a_node_link_is_kept_before_its_anchor(tmp_path: Path) -> None:
    """Resolved as part of the path, the query hid the node, and the link kept
    naming a file its chapter absorbs. A page that stays on the site keeps its
    query too."""
    project = _project(tmp_path)
    _edit(
        project / "blueprint/coverage/README.md",
        "Source audit pending",
        "See [Top](../roadmap/top.md?plain=1) and [the overview](../README.md?plain=1).",
    )

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    published = (tmp_path / "out/coverage/README.md").read_text(encoding="utf-8")
    assert "See [Top](../roadmap/README.md?plain=1#top) and [the overview](../README.md?plain=1)." in published


def test_a_query_on_a_book_link_keeps_its_chapter_in_the_book(tmp_path: Path) -> None:
    """Read as part of the path, the query hid the chapter from the book's page order."""
    project = _project(tmp_path)
    _edit(project / "blueprint/README.md", "(roadmap/README.md)", "(roadmap/README.md?plain=1)")

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert "[Roadmap](roadmap/README.md)" in (tmp_path / "out/SUMMARY.md").read_text(encoding="utf-8")


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
    # Nothing is there to explore yet, so the home page mounts no explorer, and its hero keeps every figure.
    assert "bp-dag-viewer" not in overview
    assert "bp-hero-counts" not in overview


def test_a_directory_link_uses_tree_even_when_the_repo_url_says_blob() -> None:
    """Deriving the directory URL by replacing the first `/blob/` rewrote the
    repository's own path when that happened to contain one."""
    from autoform_cli.render import _SourceBase

    base = _SourceBase("https://git.example/blob/x/repo", "abc", "blueprint/sources")

    assert base.href(("paper.md",)) == (
        "https://git.example/blob/x/repo/blob/abc/blueprint/sources/paper.md"
    )
    assert base.href(()) == "https://git.example/blob/x/repo/tree/abc/blueprint/sources"


def test_render_leaves_html_comments_out_of_the_site_but_not_out_of_code(tmp_path: Path) -> None:
    """A comment is a note to the authors, but one in a code block is code:
    the site shows it as typed, as GitHub does, so a statement's box shows
    every character of the statement an approval binds."""

    project = _project(tmp_path)
    top = project / "blueprint" / "roadmap" / "top.md"
    top.write_text(
        top.read_text(encoding="utf-8").replace(
            "The main result.",
            "The main result.<!-- secret note --> Shown.<!-->gone--> Mark one with `<!-- ... -->`.\n\n"
            "```text\nfenced <!-- kept in a fence -->\n```\n\n    indented <!-- kept indented -->",
        ),
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert "The main result. Shown." in page
    assert "secret note" not in page and "gone" not in page
    box = site_converter().convert(get_data(page)[0])
    assert "<code>fenced &lt;!-- kept in a fence --&gt;\n</code>" in box
    assert "<code>indented &lt;!-- kept indented --&gt;\n</code>" in box
    assert "Mark one with <code>&lt;!-- ... --&gt;</code>." in box


def _count_relative_links(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record the target of every `relative_link` call render makes."""
    calls: list[Path] = []
    relative_link = render_module.relative_link

    def counting(target: Path, output: Path, link_extension: str) -> str:
        calls.append(target)
        return relative_link(target, output, link_extension)

    monkeypatch.setattr(render_module, "relative_link", counting)
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

    # A page reads only the nodes it shows, so nothing is linked before then,
    # and reading one node links only its own target page.
    assert calls == resolved == []
    assert links["a/7"] == "a.html#n7"
    assert calls == [chapter]
    assert links["b"] == "b/index.html"
    assert links["here"] == "#self"
    assert dict(links) == {
        **{f"a/{index}": f"a.html#n{index}" for index in range(20)},
        "b": "b/index.html",
        "here": "#self",
    }
    assert sorted(calls) == [chapter, other]
    # Once to compare it with the current page, once inside relative_link.
    assert resolved.count(chapter) <= 2


def test_the_same_link_from_two_folders_reaches_each_folders_file(tmp_path: Path) -> None:
    """Render resolves each linked path once for the whole site, so a link two
    leaves write alike must still lead from each to its own folder's file."""

    project = _project(tmp_path)
    roadmap = project / "blueprint" / "roadmap"
    _edit(roadmap / "README.md", "- [Top](top.md)\n", "- [Top](top.md)\n- [Chapter](ch/README.md)\n")
    chapter = roadmap / "ch"
    chapter.mkdir()
    (chapter / "README.md").write_text("# Chapter\n\n- [One](one/leaf.md)\n- [Two](two/leaf.md)\n", encoding="utf-8")
    for folder in ("one", "two"):
        (chapter / folder / "figures").mkdir(parents=True)
        (chapter / folder / "figures" / "plot.md").write_text(f"# Plot {folder}\n\nA plot.\n", encoding="utf-8")
        (chapter / folder / "leaf.md").write_text(
            f"---\ndeclaration: theorem\n---\n\n# Leaf {folder}\n\nSee [the plot](figures/plot.md).\n",
            encoding="utf-8",
        )

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    page = (tmp_path / "out/roadmap/ch/README.md").read_text(encoding="utf-8")
    assert "[the plot](one/figures/plot.md)" in page
    assert "[the plot](two/figures/plot.md)" in page


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
    assert (
        rewrite("See [X on `[0,1]^n`](x.md).\n")
        == "See [X on `[0,1]^n`](a.md#x).\n"
    )
    assert calls == [chapter]
    calls.clear()
    # Two nodes on the same chapter page cost one link to it, not one each.
    assert rewrite("[X](x.md), [Z](z.md)\n") == "[X](a.md#x), [Z](a.md#z)\n"
    assert calls == [chapter]


def test_the_hierarchical_explorer_indexes_nodes_without_a_full_graph(tmp_path: Path) -> None:
    """Global search jumps into a small scope instead of fitting every node."""
    from autoform_cli.graph_pages import scope_page_path, write_graph_pages

    project = _project(tmp_path)
    graph = load_graph(project / "blueprint")
    destination = tmp_path / "out"
    requested: list[tuple[Path, tuple[str, ...]]] = []

    def node_links(page: Path, node_ids) -> dict[str, str]:
        selected = tuple(node_ids)
        requested.append((page, selected))
        return {node_id: f"roadmap.html#{node_id}" for node_id in selected}

    written = write_graph_pages(graph, derive(graph), destination, node_links=node_links)

    # Each explorer page asks once, and only for the articles its payload draws.
    assert sorted(page for page, _ids in requested) == sorted(written)
    for page, ids in requested:
        payload = json.loads(page.with_suffix(".json").read_text(encoding="utf-8"))
        assert set(ids) == {node["id"] for node in payload["nodes"] if node["kind"] == "node"}
    pages = {
        node_id: scope_page_path(destination, node_id, parent=graph.nodes[node_id].parent)
        for node_id in ("base", "top")
    }
    assert len(set(pages.values())) == 1
    assert pages["base"] == destination / "dependencies/chapters/roadmap.md"
    assert not (destination / "dependencies/nodes").exists()
    index = json.loads((destination / "dependencies/index.json").read_text(encoding="utf-8"))
    assert index["schema"] == "autoform-dag-search/v1"
    assert {node["id"] for node in index["nodes"]} == {"roadmap", "base", "top"}
    assert {node["url"] for node in index["nodes"] if node["id"] in pages} == {
        "chapters/roadmap.html#node=base",
        "chapters/roadmap.html#node=top",
    }


def test_a_root_article_links_the_explorer_page_search_opens(tmp_path: Path) -> None:
    """Without roadmap/README.md a root article has no parent, and its statement
    linked the project overview, which does not draw it, while search opened
    the root chapter's explorer."""
    project = _project(tmp_path)
    (project / "blueprint/roadmap/README.md").unlink()
    out = tmp_path / "out"

    render_site(project / "blueprint", out, lean_root=project)

    index = json.loads((out / "dependencies/index.json").read_text(encoding="utf-8"))
    assert {node["id"]: node["url"] for node in index["nodes"]}["top"] == "chapters/roadmap.html#node=top"
    chapter = (out / "roadmap/README.md").read_text(encoding="utf-8")
    assert '<a class="bp-context-link" href="../dependencies/chapters/roadmap.html#node=top"' in chapter
    # The root leaves' chapter is still the book's contents, where explorer trails start.
    project_view = json.loads((out / "dependencies.json").read_text(encoding="utf-8"))["view"]
    assert project_view["breadcrumbs"][0] == {"label": "Book", "url": "roadmap/index.html"}


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
    # Not mathlib: true, yet its Mathlib name is shown too.
    _edit(project / "blueprint/roadmap/top.md", "discussion: 42\n", "discussion: 42\nmathlib_declaration: add_comm\n")

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
    # The row follows the Lean and Mathlib rows and precedes Discussion.
    meta = top[top.index('<div class="bp-meta">'):top.index('<details class="bp-dependencies">')]
    assert re.findall(r'<span class="bp-key">([^<]+)</span>', meta) == ["Lean", "Mathlib", "Assumes", "Discussion"]

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


@pytest.mark.parametrize(
    ("section", "statement", "chapter"),
    [
        ("chapter", "roadmap/chapter/index.html#deep", "roadmap/chapter/index.html"),
        ("chapter/part", "roadmap/chapter/part/index.html#deep", "roadmap/chapter/index.html"),
    ],
    ids=["chapter", "nested-section"],
)
def test_next_up_links_the_published_statement_and_chapter(
    tmp_path: Path, section: str, statement: str, chapter: str
) -> None:
    """MkDocs publishes a chapter's ``README.md`` as ``index.html``.

    The card is the landing page's way into the book, so a link to
    ``README.html``, or to the top-level chapter of a result placed in a nested
    section, left the reader on a 404.
    """
    project = _project(tmp_path)
    roadmap = project / "blueprint/roadmap"
    parts = section.split("/")
    for depth in range(len(parts)):
        folder = roadmap.joinpath(*parts[: depth + 1])
        folder.mkdir()
        child = f"- [Part]({parts[depth + 1]}/README.md)\n" if depth + 1 < len(parts) else "- [Deep](deep.md)\n"
        (folder / "README.md").write_text(f"# {parts[depth].title()}\n\n{child}", encoding="utf-8")
    roadmap.joinpath(*parts, "deep.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# Deep\n\nA result still to state.\n", encoding="utf-8"
    )
    if len(parts) > 1:
        # An article of its own gives the top-level chapter the page the card links.
        with (roadmap / parts[0] / "README.md").open("a", encoding="utf-8") as readme:
            readme.write("- [Other](other.md)\n")
        (roadmap / parts[0] / "other.md").write_text(
            "---\ndeclaration: theorem\n---\n\n# Other\n\nUses the deep result.\n\n## Depends on\n\n- [Deep](part/deep.md)\n",
            encoding="utf-8",
        )
    out = tmp_path / "out"

    render_site(project / "blueprint", out, lean_root=project)

    landing = (out / "README.md").read_text(encoding="utf-8")
    start = landing.index('<div class="bp-next-target"')
    card = landing[start : landing.index("</div></div>", landing.index("bp-next-actions", start))]
    assert f'data-autoform-node-id="{section}/deep"' in card
    assert f'<div class="bp-next-title"><a href="{statement}">Deep</a></div>' in card
    assert f'<a href="{chapter}">Chapter</a>' in card
    published = _published_pages(out)
    for href in re.findall(r'href="([^"]+)"', card):
        assert Path(os.path.normpath(out / unquote(href.partition("#")[0]))) in published, href


def _statement_box(page: str, node_id: str) -> str:
    start = page.index('<div class="bp-thmcontent"', page.index(f'data-autoform-node-id="{node_id}"'))
    return page[start : page.index("</div>", start)]


def _authored_body_elements(element):
    pending = [element]
    while pending:
        current = pending.pop()
        if "bp-body-link-validation" in current.get("class", "").split():
            assert "hidden" in current.attrib
            continue
        yield current
        pending.extend(reversed(list(current)))


def test_compact_comparisons_keep_later_statements_and_proof_notes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    base = project / "blueprint/roadmap/base.md"
    top = project / "blueprint/roadmap/top.md"
    base.write_text(base.read_text().replace(
        "The base object.", "For $a<b>c$, use [the result][ref].\n\n## Sources\n\n[ref]: <top.md>"
    ))
    top.write_text(top.read_text().replace(
        "The main result.", "Use only r<n.\n\n## Proof sketch\n\nFor $i<ell$, continue the proof."
    ))
    authored = {path: path.read_bytes() for path in (base, top)}

    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    page = (tmp_path / "out/roadmap/README.md").read_text()
    # Use the real configured Markdown converter. Merely finding the cards in
    # generated Markdown misses the silent loss during md_in_html extraction.
    rendered = site_converter().convert(page + '\n<nav id="following-chapter">Next</nav>\n')
    tree = html5lib.parse(rendered, treebuilder="etree", namespaceHTMLElements=False)
    assert [el.get("id") for el in tree.iter() if "bp-thmwrapper" in el.get("class", "").split()] == ["base", "top"]
    assert any(el.get("id") == "following-chapter" for el in tree.iter())
    assert ["".join(el.itertext()) for el in tree.iter() if "arithmatex" in el.get("class", "").split()] == [
        r"\(a<b>c\)", r"\(i<ell\)",
    ]
    assert "Use only r<n." in "".join(tree.itertext())
    assert any(el.tag == "a" and el.get("href") == "#top" and el.text == "the result" for el in tree.iter())
    assert all(path.read_bytes() == original for path, original in authored.items())


@pytest.mark.parametrize(("affected", "defined_in"), [
    ("statement", "notes"), ("notes", "notes"),
    ("statement", "statement"), ("notes", "statement"), ("neither", "notes"),
])
def test_body_protection_shares_references_in_both_directions(
    tmp_path: Path, affected: str, defined_in: str,
) -> None:
    project = _project(tmp_path)
    top = project / "blueprint/roadmap/top.md"
    statement = "See [paper][ref] and ![diagram][figure]."
    notes = "A proof using [support][aux] and ![diagram][figure]."
    if affected == "statement":
        statement = "For r<n, " + statement
    elif affected == "notes":
        notes = "For r < n, continue. " + notes
    definitions = (
        '[ref]: https://example.invalid/paper "Paper title"\n'
        '[figure]: https://example.invalid/figure.svg "Figure title"\n'
        '[aux]: https://example.invalid/support "Support title"'
    )
    source = top.read_text().replace(
        "The main result.", statement + ("\n\n" + definitions if defined_in == "statement" else "")
    ).replace(
        "## Sources\n\n- [Paper](../sources.md)",
        "## Proof sketch\n\n" + notes + "\n\n## Sources\n\n"
        + (definitions if defined_in == "notes" else "Definitions are recorded with the statement."),
    )
    top.write_text(source)
    original = top.read_bytes()
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    chapter = (tmp_path / "out/roadmap/README.md").read_text()
    if affected == "neither":
        # An unaffected article still leaves its ordinary Markdown/definitions
        # for the final site conversion rather than preconverting its chunks.
        assert statement in chapter and '[ref]: https://example.invalid/paper "Paper title"' in chapter
    tree = html5lib.parse(site_converter().convert(chapter), treebuilder="etree", namespaceHTMLElements=False)
    card = next(el for el in tree.iter() if el.get("id") == "top")
    content = next(el for el in card.iter() if "bp-thmcontent" in el.get("class", "").split())
    proof = next(el for el in card.iter() if "bp-thmnotes" in el.get("class", "").split())
    assert [(el.text, el.get("href"), el.get("title")) for el in content.iter("a")] == [
        ("paper", "https://example.invalid/paper", "Paper title"),
    ]
    assert [(el.get("alt"), el.get("src"), el.get("title")) for el in content.iter("img")] == [
        ("diagram", "https://example.invalid/figure.svg", "Figure title"),
    ]
    assert [(el.text, el.get("href"), el.get("title")) for el in proof.iter("a")] == [
        ("support", "https://example.invalid/support", "Support title"),
    ]
    assert [(el.get("alt"), el.get("src"), el.get("title")) for el in proof.iter("img")] == [
        ("diagram", "https://example.invalid/figure.svg", "Figure title"),
    ]
    assert "[paper][ref]" not in "".join(content.itertext())
    assert "[support][aux]" not in "".join(proof.itertext())
    assert top.read_bytes() == original


@pytest.mark.parametrize("authored", [
    "A `r<n` code span and ``a`<b`` followed by prose r<n.",
    "```text\n$a<b$\n<!-- unclosed comment inside code\n```\n\nThen r<n.",
    "~~~~text\na<b\n```\n~~~~~\n\nThen r<n.",
    "An example:\n\n    a<b\n    c<d\n\nThen r<n.",
    "- An example:\n\n        a<b\n        c<d\n\nThen r<n.",
    "> An example:\n>\n>     a<b\n>     c<d\n\nThen r<n.",
    "- A list item.\n\n    A continuation uses $a<b$.\n\nThen r<n.",
    '<span title="a<b" data-example="`">HTML</span> and <!-- r<n --> prose r<n.',
    '<!DOCTYPE html>\n\nThen r<n.',
    '<?processing <!-- inside a processing instruction ?>\n\nThen r<n.',
    '<script>if (a<b) { console.log("<span>"); }</script>\n\nThen r<n.',
    '<https://example.invalid> and <reader@example.invalid>; [paper](<relative.md>).\n\nThen r<n.',
    '<a href="raw.md">Raw HTML link</a> and [paper](<relative.md>); then r<n.',
    '[paper][ref].\n\n[ref]: <relative.md>\n\nThen r<n.',
    r'For \(a<b\), compare r&lt;n and r < n.' + '\n\n$$a<b>c$$\n',
])
def test_body_protection_preserves_code_html_links_and_math(authored: str) -> None:
    protected = _protect_markdown_in_html(authored)
    # Duplicate the body: two comparisons can leave more than one unclosed
    # div, so the parser loses the entire suffix rather than repairing one.
    cards = "".join(
        f'<div class="bp-thmwrapper" id="card-{index}" markdown="1">\n'
        f'<div class="bp-thmcontent" markdown="1">\n\n{protected}\n\n</div>\n</div>\n'
        for index in range(2)
    )
    actual = html5lib.parse(site_converter().convert(cards), treebuilder="etree", namespaceHTMLElements=False)
    bodies = [el for el in actual.iter() if "bp-thmcontent" in el.get("class", "").split()]
    assert len(bodies) == 2
    # MkDocs rewrites parsed Markdown page links; raw HTML links keep their
    # authored destination. These fixtures exercise both on the same page.
    expected = authored.replace(
        "](<relative.md>)", "](<relative.html>)"
    ).replace("[ref]: <relative.md>", "[ref]: <relative.html>")
    reference_body = html5lib.parseFragment(
        site_converter().convert(expected), treebuilder="etree", namespaceHTMLElements=False
    )

    def signature(element):
        # HTML whitespace collapses outside code. Preserve exact code and TeX,
        # as well as actual tag/attribute structure and link destinations.
        return [(el.tag, dict(el.attrib),
                 "".join(el.itertext()) if el.tag in {"code", "pre", "script"}
                 or "arithmatex" in el.get("class", "").split()
                 else " ".join("".join(el.itertext()).split()))
                for el in _authored_body_elements(element) if el is not element]

    for body in bodies:
        assert signature(body) == signature(reference_body)


def test_body_protection_keeps_page_wide_heading_ids() -> None:
    fragment = _protect_markdown_in_html("###### Sources\n\nFor $a<b$, cite the source.")
    explicit = _protect_markdown_in_html("###### Setting {#chosen}\n\nFor $a<b$, continue.")
    # Inner conversion keeps explicit IDs and lets the final converter assign
    # each generated heading ID in its actual chapter-wide namespace.
    assert 'id="sources"' not in fragment
    assert 'id="chosen"' in explicit
    page = "# Sources\n\n" + "".join(
        f'<div class="bp-thmwrapper" markdown="1">\n{body}\n</div>\n'
        for body in (fragment, fragment, explicit)
    )
    tree = html5lib.parse(site_converter().convert(page), treebuilder="etree", namespaceHTMLElements=False)
    assert [el.get("id") for el in tree.iter() if el.tag in {"h1", "h6"}] == [
        "sources", "sources_1", "sources_2", "chosen",
    ]


def test_body_protection_preserves_original_validation_urls_and_link_kinds() -> None:
    body = (
        "For r < n, see [paper](<notes A(b.md?print=1#chosen>) "
        "and ![diagram](<fig A(b.svg?print=1#symbol>). "
        '<a href="raw.md#raw">Raw HTML</a>.'
    )
    protected = _protect_markdown_in_html(body)
    tree = html5lib.parseFragment(
        site_converter().convert(f'<div markdown="1">\n\n{protected}\n\n</div>'),
        namespaceHTMLElements=False,
    )
    witness = next(el for el in tree.iter() if "bp-body-link-validation" in el.get("class", "").split())
    assert "hidden" in witness.attrib
    assert [(el.tag, el.get("href", el.get("src"))) for el in witness.iter() if el.tag in {"a", "img"}] == [
        ("a", "notes A(b.md?print=1#chosen"), ("img", "fig A(b.svg?print=1#symbol"),
    ]
    assert [(el.tag, el.get("href", el.get("src")))
            for el in _authored_body_elements(tree) if el.tag in {"a", "img"}] == [
        ("a", "notes A(b.html?print=1#chosen"), ("img", "fig A(b.svg?print=1#symbol"),
        ("a", "raw.md#raw"),
    ]


@pytest.mark.parametrize(("body", "files", "warning"), [
    ("For r < n, see [missing](missing.md).", {}, "not found among documentation files"),
    ("For r < n, see [malformed](../sources/a%00.md).", {}, "not found among documentation files"),
    ("For r < n, see [target](target.md#missing).", {"target.md": "# Actual\n"}, "anchor '#missing'"),
    ("For r < n, see [self](#missing).", {}, "no such anchor on this page"),
    ("For r < n, see ![image](missing.svg).", {}, "not found among documentation files"),
    (
        "For r < n, see [target](<Some (field).md?print=1#actual>).",
        {"Some (field).md": "# Actual\n"}, None,
    ),
    ("For r < n, see ![image](asset.svg).", {"asset.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>'}, None),
    ('For r < n, see <a href="missing.md">raw HTML</a>.', {}, None),
])
def test_body_protection_retains_native_strict_link_validation(tmp_path: Path, caplog, body, files, warning) -> None:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config
    from mkdocs.exceptions import Abort

    docs = tmp_path / "docs"
    docs.mkdir()
    protected = _protect_markdown_in_html(body)
    wrapper = (
        '# Review\n\n<div class="bp-thmwrapper" id="card" markdown="1">\n'
        '<div class="bp-thmcontent" markdown="1">\n\n'
        + protected + '\n\n</div>\n</div>\n'
    )
    (docs / "index.md").write_text(wrapper)
    for name, contents in files.items():
        (docs / name).write_text(contents)
    config = tmp_path / "mkdocs.yml"
    config.write_text(
        "site_name: Review\nstrict: true\nuse_directory_urls: false\nplugins: []\n"
        "markdown_extensions:\n  - md_in_html\n"
        "validation:\n  links:\n    anchors: warn\n"
    )
    caplog.set_level(logging.WARNING, logger="mkdocs")
    if warning:
        with pytest.raises(Abort, match="strict mode"):
            build(load_config(config_file=str(config)))
        assert any(warning in record.message for record in caplog.records)
    else:
        build(load_config(config_file=str(config)))

    tree = html5lib.parse(site_converter().convert(wrapper), treebuilder="etree", namespaceHTMLElements=False)
    content = next(el for el in tree.iter() if "bp-thmcontent" in el.get("class", "").split())
    expected = html5lib.parseFragment(site_converter().convert(body), namespaceHTMLElements=False)
    # Native validation records do not add visible text, headings, layout or
    # keyboard destinations. Raw HTML links do not get Markdown witnesses.
    assert " ".join("".join(content.itertext()).split()) == " ".join("".join(expected.itertext()).split())
    witnesses = [el for el in content.iter() if "bp-body-link-validation" in el.get("class", "").split()]
    for witness in witnesses:
        assert "hidden" in witness.attrib
        assert " ".join("".join(witness.itertext()).split()) == ""
        assert all(el.get("id") is None for el in witness.iter())
    assert [el.get("id") for el in tree.iter() if el.get("id")] == ["review", "card"]
    if "raw HTML" in body:
        assert witnesses == []
    else:
        assert len(witnesses) == 1


def test_a_statement_keeps_its_subheadings_inside_the_theorem(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/top.md").write_text(
        "---\ndeclaration: theorem\nlean: Project.top\n---\n\n"
        "# Top\n\nThe main result.\n\n### Setting\n\nThe hypotheses.\n\n"
        "## Sources\n\nA citation.\n\n## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    box = _statement_box(page, "top")

    assert "The main result." in box and "The hypotheses." in box
    # Demoted like every other heading a node brings onto the chapter page.
    assert "###### Setting" in box
    assert "\n### Setting" not in page
    assert "A citation." not in box
    assert page.index("###### Setting") < page.index("###### Sources")
    assert "## Depends on" not in page


def test_a_hidden_heading_does_not_cut_a_statement_short(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/top.md").write_text(
        "---\ndeclaration: theorem\nlean: Project.top\n---\n\n"
        "# Top\n\nThe main result.\n\n<!--\n## Draft\n-->\n\nIts second sentence.\n\n"
        "## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    box = _statement_box((tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8"), "top")

    assert "The main result." in box and "Its second sentence." in box


def test_neither_dependency_section_is_republished(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/top.md").write_text(
        "---\ndeclaration: theorem\nlean: Project.top\n---\n\n# Top\n\nThe main result.\n\n"
        "## Depends on\n\nStatement prerequisites.\n\n## Proof depends on\n\n- [Base](base.md)\n\n"
        "## Sources\nA citation.\n",
        encoding="utf-8",
    )
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")

    assert "Statement prerequisites." not in page
    assert "Proof depends on" not in page
    assert "###### Sources" in page and "A citation." in page


def test_prose_above_the_title_is_published_in_the_theorem(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/top.md").write_text(
        "---\ndeclaration: theorem\nlean: Project.top\n---\n\n"
        "A lead-in sentence.\n\n# Top\n\nThe main result.\n\n## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    box = _statement_box((tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8"), "top")

    assert "A lead-in sentence." in box and "The main result." in box


def test_a_statement_written_below_a_section_is_still_published(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/roadmap/top.md").write_text(
        "---\ndeclaration: theorem\nlean: Project.top\n---\n\n"
        "## Sources\n\nA citation.\n\n# Top\n\nThe main result.\n\n## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    page = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")

    assert "The main result." in _statement_box(page, "top")
    assert "## Depends on" not in page


def test_finder_metadata_does_not_block_replacing_an_owned_publication(tmp_path: Path) -> None:
    project, output, _site = _published(tmp_path)
    # ._* is the extended-attribute file macOS writes beside a file on a non-APFS volume.
    finder_files = [output / ".DS_Store", output / "roadmap" / ".DS_Store", output / "._README.md"]
    for path in finder_files:
        path.write_bytes(b"\x00\x00\x00\x01Bud1")

    render_site(project / "blueprint", output, lean_root=project)

    assert not any(path.exists() for path in finder_files)
    assert not _debris(tmp_path)


def test_metadata_files_macos_deletes_with_their_file_do_not_stop_the_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, output, _site = _published(tmp_path)
    finder_files = [output / "._README.md", output / "roadmap" / "._README.md"]
    for path in finder_files:
        path.write_bytes(b"\x00\x05\x16\x07")
    unlink = os.unlink

    def gone_already(path, *, dir_fd=None) -> None:  # the kernel removed it with README.md
        unlink(path, dir_fd=dir_fd)
        if os.path.basename(path).startswith("._"):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)

    monkeypatch.setattr(render_module.os, "unlink", gone_already)
    report = render_site(project / "blueprint", output, lean_root=project)

    assert report.warnings == []
    assert not any(path.exists() for path in finder_files)
    assert not _debris(tmp_path)


def test_a_manifest_that_lists_macos_metadata_still_replaces(tmp_path: Path) -> None:
    # An extended attribute set on a staged file, on a volume without native
    # ones, becomes a ._ file that the manifest then lists.
    project, output, _site = _published(tmp_path)
    manifest = json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    manifest["files"]["._README.md"] = hashlib.sha256(b"\x00\x05\x16\x07").hexdigest()
    _manifest(output, json.dumps(manifest))
    (output / "._README.md").write_bytes(b"\x00\x05\x16\x07")

    render_site(project / "blueprint", output, lean_root=project)

    assert not (output / "._README.md").exists()
    assert not _debris(tmp_path)


@pytest.mark.parametrize("finder", [None, "file", "directory"])
def test_render_installs_into_an_existing_empty_directory(tmp_path: Path, finder: str | None) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    if finder == "file":  # what Finder leaves in a folder emptied by hand
        (output / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    elif finder == "directory":
        (output / ".DS_Store").mkdir()
        with pytest.raises(PublicationError, match="non-Autoform output directory"):
            render_site(project / "blueprint", output, lean_root=project)
        return

    render_site(project / "blueprint", output, lean_root=project)

    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["complete"]
    assert not (output / ".DS_Store").exists()
    assert not _debris(tmp_path)


@pytest.mark.parametrize("portable", [False, True])
def test_checking_the_output_site_hashes_its_files_without_holding_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, portable: bool
) -> None:
    project = _project(tmp_path)
    (project / "blueprint/media").mkdir()
    # Any file the site publishes; a .png is copied as it is, whatever it holds.
    (project / "blueprint/media/plate.png").write_bytes(os.urandom(8 * 1024 * 1024))
    output = tmp_path / "out"
    render_site(project / "blueprint", output, lean_root=project)
    assert (output / "media/plate.png").is_file()
    if portable:
        monkeypatch.setattr(directory_binding_module, "DIRECTORY_BINDING_SUPPORTED", False)

    tracemalloc.start()
    try:
        render_module._destination_state(output)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Read whole, the 8 MiB file would be held at once, as would read(n)'s 8 MiB
    # manifest buffer; chunked reads hold at most two read chunks at a time.
    assert peak < 4 * 1024 * 1024


def test_a_failed_render_leaves_the_previous_site_and_no_debris(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, output, site = _published(tmp_path)
    render_pages = render_module._render_pages

    def fail_after_writing(*args, **kwargs):
        render_pages(*args, **kwargs)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(render_module, "_render_pages", fail_after_writing)

    with pytest.raises(PublicationError, match="No space left on device; output was not changed"):
        render_site(project / "blueprint", output, lean_root=project)

    assert _tree(output) == site
    assert not _debris(tmp_path)


def test_a_manifest_the_next_render_could_not_read_is_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(render_module, "PUBLICATION_MANIFEST_MAX_BYTES", 512)

    with pytest.raises(PublicationError, match="manifest exceeds max_file_bytes=512"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    assert not (tmp_path / "out").exists()
    assert not _debris(tmp_path)


@pytest.mark.parametrize(
    ("refused", "message"),
    [
        ("exchange", "is a mount point or in use; output was not changed"),
        ("backup", "Permission denied; output was not changed"),
        ("install", "Permission denied; output was not changed"),
    ],
)
def test_a_refused_commit_keeps_the_site_and_leaves_no_debris(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused: str, message: str
) -> None:
    project, output, site = _published(tmp_path)
    backup = render_module._backup(output)
    rename = os.rename

    def exchange(first: Path, second: Path) -> None:
        code = errno.EBUSY if refused == "exchange" else errno.ENOTSUP
        raise OSError(code, os.strerror(code))

    def refuse(source, target) -> None:
        if (refused == "backup" and Path(target) == backup) or (
            refused == "install" and Path(target) == output and Path(source) != backup
        ):
            raise OSError(errno.EACCES, os.strerror(errno.EACCES))
        rename(source, target)

    monkeypatch.setattr(render_module, "_exchange", exchange)
    monkeypatch.setattr(render_module.os, "rename", refuse)

    with pytest.raises(PublicationError, match=message):
        render_site(project / "blueprint", output, lean_root=project)

    assert _tree(output) == site
    assert not _debris(tmp_path)


@pytest.mark.parametrize("failure", [OSError(errno.EIO, "Input/output error"), KeyboardInterrupt()])
def test_an_uncertain_commit_keeps_the_previous_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    project, output, site = _published(tmp_path)
    exchange = render_module._exchange

    def swap_then_fail(first: Path, second: Path) -> None:
        exchange(first, second)
        raise failure

    monkeypatch.setattr(render_module, "_exchange", swap_then_fail)

    with pytest.raises((PublicationError, KeyboardInterrupt)) as error:
        render_site(project / "blueprint", output, lean_root=project)

    [kept] = _debris(tmp_path)
    assert _tree(kept) == site
    assert isinstance(failure, KeyboardInterrupt) or f"{kept} may hold the previous site" in str(error.value)


@pytest.mark.parametrize("installed", [True, False])
def test_a_render_that_loses_a_race_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: bool
) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    if installed:
        render_site(project / "blueprint", output, lean_root=project)
    render_pages = render_module._render_pages

    def race(*args, **kwargs):
        report = render_pages(*args, **kwargs)
        output.mkdir(exist_ok=True)
        (output / "README.md").write_text("written meanwhile\n", encoding="utf-8")
        return report

    monkeypatch.setattr(render_module, "_render_pages", race)

    with pytest.raises(PublicationError, match="changed during publication|refusing to overwrite"):
        render_site(project / "blueprint", output, lean_root=project)

    assert (output / "README.md").read_text(encoding="utf-8") == "written meanwhile\n"
    assert not _debris(tmp_path)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root ignores directory write permission",
)
def test_an_unwritable_output_parent_is_reported_not_a_traceback(tmp_path: Path) -> None:
    project = _project(tmp_path)
    parent = tmp_path / "readonly"
    output = parent / "out"
    output.mkdir(parents=True)
    parent.chmod(0o555)
    try:
        with pytest.raises(PublicationError, match="needs write access to the output"):
            render_site(project / "blueprint", output, lean_root=project)
    finally:
        parent.chmod(0o755)

    assert not any(output.iterdir())


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root ignores directory read permission",
)
def test_an_output_parent_that_cannot_be_read_still_publishes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    parent = tmp_path / "dropbox"
    parent.mkdir()
    output = parent / "out"
    parent.chmod(0o300)  # write and search, no read
    try:
        render_site(project / "blueprint", output, lean_root=project)
        render_site(project / "blueprint", output, lean_root=project)
    finally:
        parent.chmod(0o755)

    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["complete"]
    assert not _debris(parent)


def test_a_ctrl_c_after_the_fallback_install_keeps_the_new_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, output, site = _published(tmp_path)
    _edit(project / "blueprint/roadmap/top.md", "The main result.", "Revised.")
    rename = os.rename

    def unsupported(first: Path, second: Path) -> None:
        raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))

    def interrupt_after_install(source, target) -> None:
        rename(source, target)
        if Path(target) == output and Path(source).name.startswith(render_module._STAGE_PREFIX):
            raise KeyboardInterrupt

    monkeypatch.setattr(render_module, "_exchange", unsupported)
    monkeypatch.setattr(render_module.os, "rename", interrupt_after_install)
    with pytest.raises(KeyboardInterrupt):
        render_site(project / "blueprint", output, lean_root=project)
    monkeypatch.undo()

    assert "Revised." in (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert _tree(render_module._backup(output)) == site
    render_site(project / "blueprint", output, lean_root=project)  # retires the backup
    assert not _debris(tmp_path)


@pytest.mark.parametrize("code", sorted(render_module._UNSUPPORTED))
def test_without_an_atomic_exchange_render_replaces_through_a_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    project, output, _site = _published(tmp_path)
    _edit(project / "blueprint/roadmap/top.md", "The main result.", "Revised.")

    def unsupported(first: Path, second: Path) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(render_module, "_exchange", unsupported)
    render_site(project / "blueprint", output, lean_root=project)

    assert "Revised." in (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert not _debris(tmp_path)


def test_the_next_render_finishes_an_interrupted_backup_commit(tmp_path: Path) -> None:
    project, output, site = _published(tmp_path)
    backup = render_module._backup(output)
    coverage = project / "blueprint/coverage/README.md"
    contract = coverage.read_text(encoding="utf-8")
    coverage.write_text("# Coverage\n", encoding="utf-8")
    for empty_output in (False, True):  # killed between the two renames; maybe an empty output since
        output.rename(backup)
        if empty_output:
            output.mkdir()
        with pytest.raises(PublicationError, match="coverage contract"):
            render_site(project / "blueprint", output, lean_root=project)
        assert _tree(output) == site and not backup.exists()

    coverage.write_text(contract, encoding="utf-8")
    shutil.copytree(output, backup)  # killed after the second rename instead
    render_site(project / "blueprint", output, lean_root=project)
    assert _tree(output) == site and not _debris(tmp_path)

    shutil.copytree(output, backup)
    (backup / "README.md").write_text("edited\n", encoding="utf-8")
    with pytest.raises(PublicationError, match="could not recover the previous site at .*by hand"):
        render_site(project / "blueprint", output, lean_root=project)
    assert (backup / "README.md").read_text(encoding="utf-8") == "edited\n" and _tree(output) == site


def test_a_partly_deleted_backup_does_not_block_the_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, output, site = _published(tmp_path)
    shutil.copytree(output, render_module._backup(output))  # killed after the second rename
    remove_tree = render_module._remove_tree
    removed: list[Path] = []

    def busy_once(path: Path) -> None:
        removed.append(path)
        if len(removed) == 1:  # the retired backup
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))
        remove_tree(path)

    monkeypatch.setattr(render_module, "_remove_tree", busy_once)
    report = render_site(project / "blueprint", output, lean_root=project)

    assert (
        f"the previous site at {removed[0]} was only partly deleted: {os.strerror(errno.EBUSY)}; "
        "remove the rest by hand"
    ) in report.warnings
    assert _tree(output) == site


def test_a_file_saved_into_the_replaced_site_during_deletion_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, output, _site = _published(tmp_path)
    rmtree = shutil.rmtree
    saved: list[Path] = []

    def save_meanwhile(path, *args, **kwargs) -> None:  # an editor whose cwd was the old output
        rmtree(path, *args, **kwargs)
        parent = Path(path).parent
        if not saved and parent.name.startswith(render_module._STAGE_PREFIX):
            saved.append(parent / "notes.md")
            saved[0].write_text("draft\n", encoding="utf-8")

    monkeypatch.setattr(render_module.shutil, "rmtree", save_meanwhile)
    report = render_site(project / "blueprint", output, lean_root=project)

    assert saved[0].read_text(encoding="utf-8") == "draft\n"
    assert any(f"{saved[0].parent} was only partly deleted" in warning for warning in report.warnings)


def test_an_output_inside_the_vault_under_another_spelling_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    if not (project / "BLUEPRINT").is_dir():
        pytest.skip("filesystem is case-sensitive")

    with pytest.raises(PublicationError, match="must be disjoint"):
        render_site(project / "blueprint", project / "BLUEPRINT" / "site", lean_root=project)

    assert not (project / "blueprint" / "site").exists()


def test_a_capable_filesystem_swaps_the_site_in_one_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, output, _site = _published(tmp_path)
    renames = []
    rename = os.rename
    monkeypatch.setattr(render_module.os, "rename", lambda *args: (renames.append(args), rename(*args)))

    render_site(project / "blueprint", output, lean_root=project)

    assert renames == []


def test_render_fsyncs_the_new_site_and_its_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fsync = os.fsync
    synced: set[tuple[int, int]] = set()

    def record(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        synced.add((metadata.st_dev, metadata.st_ino))
        fsync(descriptor)

    monkeypatch.setattr(render_module.os, "fsync", record)
    _render(tmp_path)

    output = tmp_path / "out"
    published = [tmp_path, output, *output.rglob("*")]
    assert [path for path in published if (path.stat().st_dev, path.stat().st_ino) not in synced] == []


def test_published_modes_follow_the_umask(tmp_path: Path) -> None:
    project = _project(tmp_path)
    output = tmp_path / "out"
    previous = os.umask(0o027)
    try:
        render_site(project / "blueprint", output, lean_root=project)
    finally:
        os.umask(previous)

    published = [output, *output.rglob("*")]
    assert {stat.S_IMODE(path.stat().st_mode) for path in published if path.is_dir()} == {0o750}
    assert {stat.S_IMODE(path.stat().st_mode) for path in published if path.is_file()} == {0o640}


def test_metadata_only_changes_during_a_render_do_not_fail_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, output, _site = _published(tmp_path)
    render_pages = render_module._render_pages

    def touch(*args, **kwargs):
        report = render_pages(*args, **kwargs)
        for path in (output / "README.md", project / "blueprint/README.md", project / "Project/Basic.lean"):
            os.utime(path)
        return report

    monkeypatch.setattr(render_module, "_render_pages", touch)

    assert render_site(project / "blueprint", output, lean_root=project).warnings == []


def test_renders_into_sibling_outputs_wait_for_each_other_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    barrier = threading.Barrier(2)
    locked_parent = render_module._locked_parent

    def together(parent: Path):
        barrier.wait(timeout=30)
        return locked_parent(parent)

    monkeypatch.setattr(render_module, "_locked_parent", together)
    outputs = [tmp_path / "public/a", tmp_path / "public/b"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda output: render_site(project / "blueprint", output, lean_root=project), outputs))

    assert all((output / PUBLICATION_MANIFEST).is_file() for output in outputs)
    assert not _debris(tmp_path / "public")


def test_a_commit_lock_held_past_the_deadline_names_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    project = _project(tmp_path)
    output = tmp_path / "out"
    monkeypatch.setattr(render_module, "_PUBLICATION_LOCK_WAIT_SECONDS", 0.2)
    holder = os.open(tmp_path, os.O_RDONLY)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(PublicationError, match="kept the output parent .* locked"):
            render_site(project / "blueprint", output, lean_root=project)
    finally:
        os.close(holder)

    assert not output.exists()
    assert not _debris(tmp_path)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root ignores directory read permission",
)
def test_render_accepts_an_ancestor_that_grants_search_but_not_read(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _project(home / "alice")
    output = project / "site-src"
    home.chmod(0o311)
    try:
        render_site(project / "blueprint", output, lean_root=project)
        render_site(project / "blueprint", output, lean_root=project)
    finally:
        home.chmod(0o755)

    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["complete"]
    assert not _debris(project)


@pytest.mark.parametrize(
    ("name", "generated"),
    [
        ("Summary.md", "SUMMARY.md"),
        ("Dependencies.md", "dependencies.md"),
        ("Dependencies.json", "dependencies.json"),
        ("Structure.md", "structure.md"),
        ("Publication.json", PUBLICATION_MANIFEST),
        ("Assets/logo.svg", "assets"),
        ("Stylesheets/site.css", "stylesheets"),
        ("stylesheets/Blueprint.css", "stylesheets/blueprint.css"),
        ("assets/Autoform.svg", "assets/autoform.svg"),
        ("javascripts/Blueprint-dag.js", "javascripts/blueprint-dag.js"),
        ("Progress.md", None),
        ("roadmap/dependencies.md", None),
        ("roadmap/dependencies.json", None),
    ],
)
def test_only_names_that_collide_with_generated_output_are_refused(
    tmp_path: Path, name: str, generated: str | None
) -> None:
    project = _project(tmp_path)
    authored = project / "blueprint" / name
    authored.parent.mkdir(exist_ok=True)
    authored.write_text("# Authored\n", encoding="utf-8")
    output = tmp_path / "out"

    if generated is None:
        render_site(project / "blueprint", output, lean_root=project)
        assert (output / name).is_file()
        return
    with pytest.raises(PublicationError, match=f"only in case from the generated {re.escape(generated)}"):
        render_site(project / "blueprint", output, lean_root=project)
    assert not output.exists()


def test_output_must_have_an_ordinary_final_component(tmp_path: Path) -> None:
    project = _project(tmp_path)

    with pytest.raises(PublicationError, match="ordinary directory"):
        render_site(project / "blueprint", tmp_path / "child" / "..", lean_root=project)

    assert not (tmp_path / "child").exists()


def test_blueprint_paths_that_differ_only_in_case_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    bind = render_module.bind_directory_tree

    @contextmanager
    def with_a_case_alias(*args, **kwargs):  # what a case-sensitive volume can hold
        with bind(*args, **kwargs) as tree:
            snapshot = tree.capture()
        files = (*snapshot.files, ("roadmap/TOP.md", b"# Top\n"))
        yield SimpleNamespace(capture=lambda: dataclasses.replace(snapshot, files=files))

    monkeypatch.setattr(render_module, "bind_directory_tree", with_a_case_alias)

    with pytest.raises(PublicationError, match="roadmap/TOP.md and roadmap/top.md: they differ only in case"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    assert not (tmp_path / "out").exists()


def test_lasting_capture_failures_name_their_cause(tmp_path: Path) -> None:
    project = _project(tmp_path)

    with pytest.raises(PublicationError, match="blueprint directory does not exist"):
        render_site(project / "bluepirnt", tmp_path / "out", lean_root=project)
    with pytest.raises(PublicationError, match="Lean root does not exist or is not a directory"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project / "nope")

    alias = project / "Project/Alias.lean"
    alias.symlink_to(project / "Project/Basic.lean")
    with pytest.raises(PublicationError, match="unsafe Lean source Project/Alias.lean: symbolic links"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    alias.unlink()

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        secret = project / "blueprint/roadmap/secret.md"
        secret.write_text("# Secret\n", encoding="utf-8")
        secret.chmod(0)
        try:
            with pytest.raises(PublicationError, match="permission denied: roadmap/secret.md"):
                render_site(project / "blueprint", tmp_path / "out", lean_root=project)
        finally:
            secret.unlink()

    os.mkfifo(project / "blueprint/roadmap/trap.md")
    with pytest.raises(PublicationError, match=r"trap\.md: named pipe"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    assert not (tmp_path / "out").exists()


def test_a_transient_lean_listing_change_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    open_sources = lean_module.open_project_sources
    calls = []

    def change_once(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise TreeChangedError("directory tree changed while it was captured")
        return open_sources(*args, **kwargs)

    monkeypatch.setattr(lean_module, "open_project_sources", change_once)

    assert render_site(project / "blueprint", tmp_path / "out", lean_root=project).linked == 2
    assert len(calls) == 2
    assert calls[-1]["limits"].max_entries is None  # the Lean walk has no entry cap


def test_a_blueprint_that_changes_while_it_is_read_asks_for_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)

    def changing(*args, **kwargs):
        raise TreeChangedError("directory tree changed while it was captured")

    monkeypatch.setattr(render_module, "bind_directory_tree", changing)

    with pytest.raises(PublicationError, match="blueprint changed while it was read; retry"):
        render_site(project / "blueprint", tmp_path / "out", lean_root=project)
    assert not (tmp_path / "out").exists()


def test_entries_added_removed_or_renamed_during_a_render_publish_the_captured_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, output, previous = _published(tmp_path)
    blueprint = project / "blueprint"
    _edit(blueprint / "roadmap/top.md", "The main result.", "Revised.")
    undisturbed = tmp_path / "undisturbed"
    render_site(blueprint, undisturbed, lean_root=project)
    render_pages = render_module._render_pages

    def churn_then_render(*args, **kwargs):
        (blueprint / "roadmap/added.md").write_text("# Added\n\nSaved mid-render.\n", encoding="utf-8")
        (blueprint / "roadmap/base.md").unlink()
        (blueprint / "roadmap").rename(blueprint / "moved")
        return render_pages(*args, **kwargs)

    monkeypatch.setattr(render_module, "_render_pages", churn_then_render)
    render_site(blueprint, output, lean_root=project)

    # Render replaced the previous site with the generation it captured, byte for byte: the revised article,
    # no added page, Base still published, and nothing under the new directory name.
    assert _tree(output) == _tree(undisturbed) != previous


def test_repository_links_use_the_verified_commit_not_a_mutable_ref(tmp_path: Path) -> None:
    project = _project(tmp_path)
    commit = _commit_project(project)
    output = tmp_path / "out"

    render_site(project / "blueprint", output, lean_root=project, repository_url=_REPOSITORY, ref="HEAD")

    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["git_ref"] == commit
    assert f"/blob/{commit}/Project/Basic.lean#L5" in (output / "roadmap/README.md").read_text(encoding="utf-8")


def test_a_symlinked_lean_root_still_gets_verified_links(tmp_path: Path) -> None:
    project = _project(tmp_path)
    commit = _commit_project(project)
    alias = tmp_path / "project-alias"
    alias.symlink_to(project, target_is_directory=True)
    output = tmp_path / "out"

    report = render_site(project / "blueprint", output, lean_root=alias, repository_url=_REPOSITORY, ref=commit)

    assert report.linked == 2
    assert f"/blob/{commit}/blueprint/roadmap/top.md" in (output / "roadmap/README.md").read_text(encoding="utf-8")


def test_dirty_lean_snapshot_does_not_borrow_the_clean_commit_ref(tmp_path: Path) -> None:
    project = _project(tmp_path)
    commit = _commit_project(project)
    _edit(project / "Project/Basic.lean", "end Project", "end Project\n-- uncommitted")
    output = tmp_path / "out"

    report = render_site(project / "blueprint", output, lean_root=project, repository_url=_REPOSITORY, ref="HEAD")

    assert "/blob/" not in (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["git_ref"] is None
    assert report.warnings == [
        "repository links were omitted because the captured Lean inputs do not match one "
        f"verified local Git commit: Project/Basic.lean differs from commit {commit[:12]}"
    ]


@pytest.mark.parametrize(
    "layout", ["beside", "inside", "implicit"], ids=["blueprint-beside-the-lean-root", "blueprint-inside-it", "no-lean-root"]
)
def test_a_lean_root_below_the_repository_top_level_gets_links_at_repository_paths(
    tmp_path: Path, layout: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = _with_source_notes(repo).rename(repo / "v1")
    # Beside the Lean root, at a path other than the copy's fallback name, blueprint/.
    if layout == "beside":
        (repo / "docs").mkdir()
        blueprint = (project / "blueprint").rename(repo / "docs" / "blueprint")
    else:
        blueprint = project / "blueprint"
    commit = _commit_project(repo)
    output = tmp_path / "out"

    # Without --lean-root the Lean root is the vault's parent, here v1/.
    lean_root = None if layout == "implicit" else project
    report = render_site(blueprint, output, lean_root=lean_root, repository_url=_REPOSITORY, ref="HEAD")

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert report.linked == 2
    assert report.warnings == []
    assert f"/blob/{commit}/v1/Project/Basic.lean#L5" in chapter
    vault = "docs/blueprint" if layout == "beside" else "v1/blueprint"
    assert f"/blob/{commit}/{vault}/roadmap/top.md" in chapter
    assert f"/blob/{commit}/{vault}/sources/paper.md#lemma-3" in chapter
    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["git_ref"] == commit


@pytest.mark.parametrize("nested", [False, True], ids=["sibling", "nested-checkout"])
def test_a_blueprint_outside_the_repository_keeps_its_lean_code_links(tmp_path: Path, nested: bool) -> None:
    project = _project(tmp_path)
    if nested:  # a submodule or a separate clone inside the Lean repository
        subprocess.run(["git", "init", "-q"], cwd=project / "blueprint", check=True)
        _git_commit(project / "blueprint", "notes")
        blueprint = project / "blueprint"
    else:
        blueprint = (project / "blueprint").rename(tmp_path / "notes")
    commit = _commit_project(project)
    output = tmp_path / "out"

    report = render_site(blueprint, output, lean_root=project, repository_url=_REPOSITORY, ref="HEAD")

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert f"/blob/{commit}/Project/Basic.lean#L5" in chapter
    # Render's copy places the vault at blueprint/, which names nothing in the Lean repository's commit.
    assert f"/blob/{commit}/blueprint/" not in chapter
    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["git_ref"] == commit
    assert report.warnings == []


def test_an_uncommitted_blueprint_edit_drops_only_the_markdown_source_links(tmp_path: Path) -> None:
    project = _project(tmp_path)
    commit = _commit_project(project)
    _edit(project / "blueprint/roadmap/top.md", "The main result.", "The main result, revised.")
    output = tmp_path / "out"

    report = render_site(project / "blueprint", output, lean_root=project, repository_url=_REPOSITORY, ref="HEAD")

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert f"/blob/{commit}/Project/Basic.lean#L5" in chapter
    assert f"/blob/{commit}/blueprint/" not in chapter
    assert json.loads((output / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))["git_ref"] == commit
    assert report.warnings == [
        "Markdown source links were omitted because the captured blueprint does not match the verified "
        f"local Git commit: blueprint/roadmap/top.md differs from commit {commit[:12]}"
    ]


@pytest.mark.parametrize("note", ["untracked.md", ""])
def test_an_untracked_source_note_or_directory_gets_no_commit_link(tmp_path: Path, note: str) -> None:
    project = _project(tmp_path)
    _edit(project / "blueprint/roadmap/README.md", "# Roadmap", f"# Roadmap\n\nSee [sources](../sources/{note}).")
    commit = _commit_project(project)
    (project / "blueprint/sources").mkdir()
    if note:
        (project / "blueprint/sources" / note).write_text("# Local note\n", encoding="utf-8")
    output = tmp_path / "out"

    report = render_site(project / "blueprint", output, lean_root=project, repository_url=_REPOSITORY, ref=commit)

    assert f"/{commit}/blueprint/sources" not in (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert not note or (output / "sources" / note).is_file()
    assert report.warnings[0].startswith("Markdown source links were omitted")


def test_markdown_source_permalink_quotes_the_original_repository_path(tmp_path: Path) -> None:
    project = _project(tmp_path)
    roadmap = project / "blueprint/roadmap"
    (roadmap / "top.md").rename(roadmap / "top lemma é.md")
    _edit(roadmap / "README.md", "(top.md)", "(top%20lemma%20%C3%A9.md)")
    commit = _commit_project(project)
    output = tmp_path / "out"

    render_site(project / "blueprint", output, lean_root=project, repository_url=_REPOSITORY)

    page = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert f"/blob/{commit}/blueprint/roadmap/top%20lemma%20%C3%A9.md" in page
    assert ".autoform-publication" not in page and "autoform-blueprint-" not in page


def _print_composed_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `git ls-tree` and `git rev-parse` print e + U+0301 composed, as macOS Git does.

    Elsewhere Git keeps names as written.
    """
    run = subprocess.run

    def composed(command, *args, **kwargs):
        completed = run(command, *args, **kwargs)
        if command[:2] == ["git", "ls-tree"]:
            completed.stdout = completed.stdout.replace("e\u0301".encode(), "\u00e9".encode())
        elif command[:2] == ["git", "rev-parse"] and "--show-prefix" in command:
            completed.stdout = completed.stdout.replace("e\u0301", "\u00e9")
        return completed

    monkeypatch.setattr(lean_module.subprocess, "run", composed)


def test_decomposed_file_names_link_to_the_spelling_git_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    roadmap = project / "blueprint/roadmap"
    # NFD é, as some Cocoa apps write it, and U+212B ANGSTROM SIGN, which NFC would replace but Git keeps.
    name = "Poincare\u0301\u212b"
    (roadmap / "top.md").rename(roadmap / f"{name}.md")
    (project / "Project/Basic.lean").rename(project / f"Project/{name}.lean")
    (project / "blueprint/sources").mkdir()
    (project / "blueprint/sources" / f"{name}.md").write_text("# Notes\n", encoding="utf-8")
    _edit(roadmap / "README.md", "(top.md)", f"({name}.md)")
    _edit(roadmap / "README.md", "# Roadmap", f"# Roadmap\n\nSee [notes](../sources/{name}.md).")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "core.precomposeunicode", "true"], cwd=project, check=True)
    commit = _git_commit(project, "fixture")
    output = tmp_path / "out"
    _print_composed_names(monkeypatch)

    report = render_site(project / "blueprint", output, lean_root=project, repository_url=_REPOSITORY, ref=commit)

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    committed = "Poincar%C3%A9%E2%84%AB"
    assert f"/blob/{commit}/Project/{committed}.lean#L5" in chapter
    assert f"/blob/{commit}/blueprint/roadmap/{committed}.md" in chapter
    assert f"/blob/{commit}/blueprint/sources/{committed}.md" in chapter
    assert report.warnings == []


def test_a_decomposed_lean_root_below_the_top_level_links_the_spelling_git_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    # Spelled as stored, NFD, as `--lean-root .` gives it after `cd` into a folder Finder named.
    project = _project(repo).rename(repo / "Poincare\u0301")
    (project / "Project/Basic.lean").rename(project / "Project/Be\u0301.lean")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "core.precomposeunicode", "true"], cwd=repo, check=True)
    commit = _git_commit(repo, "fixture")
    output = tmp_path / "out"
    _print_composed_names(monkeypatch)

    report = render_site(project / "blueprint", output, lean_root=project, repository_url=_REPOSITORY, ref=commit)

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert f"/blob/{commit}/Poincar%C3%A9/Project/B%C3%A9.lean#L5" in chapter
    assert f"/blob/{commit}/Poincar%C3%A9/blueprint/roadmap/top.md" in chapter
    assert report.warnings == []


def test_a_link_to_a_folder_names_the_spelling_git_committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    blueprint = project / "Poincare\u0301" / "blueprint"  # a folder name decomposed on disk, as Finder writes it
    blueprint.parent.mkdir()
    (project / "blueprint").rename(blueprint)
    (blueprint / "sources").mkdir()
    (blueprint / "sources/paper.md").write_text("# Paper\n", encoding="utf-8")
    (blueprint / "sources/drafts").mkdir()
    (blueprint / "sources/drafts/.gitkeep").write_text("", encoding="utf-8")  # committed, but never captured
    _edit(
        blueprint / "roadmap/README.md",
        "# Roadmap",
        "# Roadmap\n\nSee [the notes](../sources/), [the paper](../sources/paper.md) and [drafts](../sources/drafts/).",
    )
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "core.precomposeunicode", "true"], cwd=project, check=True)
    commit = _git_commit(project, "fixture")
    _print_composed_names(monkeypatch)

    report = render_site(blueprint, tmp_path / "out", lean_root=project, repository_url=_REPOSITORY, ref=commit)

    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert f"/tree/{commit}/Poincar%C3%A9/blueprint/sources" in chapter
    assert f"/blob/{commit}/Poincar%C3%A9/blueprint/sources/paper.md" in chapter
    assert f"/blob/{commit}/Poincar%C3%A9/blueprint/roadmap/top.md" in chapter
    assert f"/{commit}/Poincar%C3%A9/blueprint/sources/drafts" in chapter
    assert "Poincare%CC%81" not in chapter
    assert report.warnings == []


def test_a_source_note_link_names_the_committed_spelling_however_it_was_typed() -> None:
    from autoform_cli.render import _SourceBase

    committed = "blueprint/sources/Poincar\u00e9\u212b.md"
    base = _SourceBase(_REPOSITORY, "abc", "blueprint/sources", {"blueprint/sources/Poincar\u00e9\u00c5.md": committed})

    # Keyed by NFC form, so a decomposed é in the link still finds it.
    assert base.href(("Poincare\u0301\u212b.md",)) == (
        f"{_REPOSITORY}/blob/abc/blueprint/sources/Poincar%C3%A9%E2%84%AB.md"
    )


def test_links_into_the_blueprint_quote_the_ref_and_name_the_committed_spelling(tmp_path: Path) -> None:
    # A ref with a slash stays one path segment, and the path is the spelling Git committed.
    committed = "blueprint/sources/Poincar\u00e9\u212b.md"
    base = render_module._SourceBase(
        _REPOSITORY, "feature/topic", "blueprint/sources", {"blueprint/sources/Poincar\u00e9\u00c5.md": committed}
    )
    assert base.href(("Poincare\u0301\u212b.md",)) == (
        f"{_REPOSITORY}/blob/feature%2Ftopic/blueprint/sources/Poincar%C3%A9%E2%84%AB.md"
    )

    root = tmp_path.resolve()
    article = "blueprint/roadmap/Poincare\u0301\u212b.md"
    linker = SimpleNamespace(
        repository_url=_REPOSITORY,
        ref="feature/topic",
        links_blueprint=True,
        spellings={article: "blueprint/roadmap/Poincar\u00e9\u212b.md"},
    )
    node = SimpleNamespace(path=root / article, title="Poincar\u00e9")

    link = render_module._vault_source_link(node, repo_root=root, linker=linker)

    assert f'href="{_REPOSITORY}/blob/feature%2Ftopic/blueprint/roadmap/Poincar%C3%A9%E2%84%AB.md"' in link


def test_in_repo_outputs_never_supply_lean_source_links(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "blueprint/proofs.lean").write_text("theorem BlueprintProof : True := trivial\n", encoding="utf-8")
    _edit(project / "blueprint/roadmap/top.md", "lean: Project.top", "lean: BlueprintProof")
    commit = _commit_project(project)

    for name in ("aaa-output", "zzz-output"):
        render_site(project / "blueprint", project / name, lean_root=project, repository_url=_REPOSITORY)
        page = (project / name / "roadmap/README.md").read_text(encoding="utf-8")
        assert f"{_REPOSITORY}/blob/{commit}/blueprint/proofs.lean#L1" in page


@pytest.mark.parametrize("schema", ["autoform-publication/v1", "autoform-publication/v2"])
def test_an_older_site_in_the_lean_root_is_never_indexed(tmp_path: Path, schema: str) -> None:
    project = _project(tmp_path)
    (project / "blueprint/proofs.lean").write_text("theorem BlueprintProof : True := trivial\n", encoding="utf-8")
    _edit(project / "blueprint/roadmap/top.md", "lean: Project.top", "lean: BlueprintProof")
    commit = _commit_project(project)
    older = project / "aaa-output"
    render_site(project / "blueprint", older, lean_root=project, repository_url=_REPOSITORY)
    manifest = json.loads((older / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    _manifest(older, json.dumps({**manifest, "schema": schema}))

    render_site(project / "blueprint", project / "zzz-output", lean_root=project, repository_url=_REPOSITORY)

    page = (project / "zzz-output/roadmap/README.md").read_text(encoding="utf-8")
    assert f"{_REPOSITORY}/blob/{commit}/blueprint/proofs.lean#L1" in page


def test_audit_and_render_skip_a_site_and_its_mkdocs_copy_alike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    proofs = project / "blueprint/Proofs.lean"
    proofs.write_text("theorem BlueprintProof : True := trivial\n", encoding="utf-8")
    render_site(project / "blueprint", project / "site-src", lean_root=project)
    # What `mkdocs build` does with the template's docs_dir and site_dir.
    shutil.copytree(project / "site-src", project / "site")
    proofs.write_text("theorem RenamedProof : True := trivial\n", encoding="utf-8")
    render_site(project / "blueprint", project / "site-src", lean_root=project)
    rendered = json.loads((project / "site-src" / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    audited: list[str] = []

    def snapshot_project_sources(root, **options):
        snapshot = lean_module.snapshot_project_sources(root, **options)
        audited.append(snapshot.revision)
        return snapshot

    monkeypatch.setattr(audit_module, "snapshot_project_sources", snapshot_project_sources)
    _edit(project / "blueprint/roadmap/top.md", "lean: Project.top", "lean: BlueprintProof")
    result = audit_blueprint(project / "blueprint", lean_root=project)

    assert audited == [rendered["lean_source_revision"]]
    assert ("lean-target-not-found", "Lean declaration target was not found: BlueprintProof") in {
        (finding.code, finding.reason) for finding in result.findings
    }


def test_a_capture_during_a_render_skips_the_stage_being_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    (project / "blueprint/Proofs.lean").write_text("theorem BlueprintProof : True := trivial\n", encoding="utf-8")
    write_bytes = Path.write_bytes
    captured: list[list[str]] = []

    def write_then_capture(self: Path, data: bytes) -> int:
        # Render writes the site's copy of each file from the bytes it captured.
        result = write_bytes(self, data)
        if self.suffix == ".lean" and any(part.startswith(render_module._STAGE_PREFIX) for part in self.parts):
            snapshot = lean_module.snapshot_project_sources(project)
            captured.append(sorted(path.as_posix() for path, _data in snapshot.source_files))
        return result

    monkeypatch.setattr(Path, "write_bytes", write_then_capture)
    render_site(project / "blueprint", project / "site-src", lean_root=project)

    assert captured == [["Project/Basic.lean", "blueprint/Proofs.lean"]]


def test_a_capture_while_render_rewrites_its_manifest_skips_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    (project / "blueprint/Proofs.lean").write_text("theorem BlueprintProof : True := trivial\n", encoding="utf-8")
    write_bytes, replace = Path.write_bytes, os.replace
    captured: list[list[str]] = []

    def capture() -> None:
        snapshot = lean_module.snapshot_project_sources(project)
        captured.append(sorted(path.as_posix() for path, _data in snapshot.source_files))

    def truncate_capture_then_write(self: Path, data: bytes) -> int:
        if self.name == render_module.PUBLICATION_MANIFEST:
            open(self, "wb").close()  # where an in-place write leaves the file first
            capture()
        return write_bytes(self, data)

    def capture_around_replace(source, target, *args, **kwargs) -> None:
        manifest = Path(target).name == render_module.PUBLICATION_MANIFEST
        if manifest:
            capture()
        replace(source, target, *args, **kwargs)
        if manifest:
            capture()

    monkeypatch.setattr(Path, "write_bytes", truncate_capture_then_write)
    monkeypatch.setattr(render_module.os, "replace", capture_around_replace)
    render_site(project / "blueprint", project / "site-src", lean_root=project)

    assert captured
    assert all(files == ["Project/Basic.lean", "blueprint/Proofs.lean"] for files in captured), captured


def test_an_edit_during_a_render_publishes_the_captured_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    top = project / "blueprint/roadmap/top.md"
    render_pages = render_module._render_pages

    def edit_then_render(*args, **kwargs):
        _edit(top, "The main result.", "Edited mid-render.")
        return render_pages(*args, **kwargs)

    monkeypatch.setattr(render_module, "_render_pages", edit_then_render)
    render_site(project / "blueprint", tmp_path / "out", lean_root=project)

    chapter = (tmp_path / "out/roadmap/README.md").read_text(encoding="utf-8")
    assert "The main result." in chapter and "Edited mid-render." not in chapter
    manifest = json.loads((tmp_path / "out" / PUBLICATION_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["complete"] is True
    assert manifest["source_revision"] != publication_source_revision(project / "blueprint")


def test_a_heading_shown_as_code_is_not_a_page_title() -> None:
    text = "```markdown\n# Not the title\n```\n\n# Title\n\nBody.\n"

    assert _first_h1(text) == "Title"
    assert _document_body(text).startswith("```markdown\n# Not the title\n```")


def test_a_fence_that_never_closes_does_not_hide_the_page_title() -> None:
    # The site and article_parts read a fence without a closing line as text.
    text = "Intro.\n\n```lean\n\n# Title\n\nBody.\n"

    assert _first_h1(text) == "Title"
    assert _document_body(text) == "Intro.\n\n```lean\n\n\nBody."
