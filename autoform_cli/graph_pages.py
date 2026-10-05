"""Publish scalable graph projections beside the textbook blueprint.

One Markdown DAG supports several reading scales: chapters collapsed into a
project map, declarations within one chapter with external chapters collapsed
to boundaries, a theorem's one-hop neighborhood, and an optional full graph.
Every projection links back to the same book anchors and never becomes another
source of graph state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from urllib.parse import quote

from . import dag_viewer, mermaid
from .graph import Graph
from .graph_views import (
    GraphView,
    chapter_view,
    full_view,
    group_nodes,
    project_view,
    scope_views,
)
from .status import NodeStatus

NodeLinks = Callable[[Path, Iterable[str]], Mapping[str, str]]


def write_graph_pages(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    destination: str | Path,
    *,
    node_links: NodeLinks,
) -> tuple[Path, ...]:
    """Write project, chapter, nested-scope, and shared full-explorer pages.

    ``node_links`` resolves theorem ids to their published textbook anchors for
    each generated page.  Keeping that callback in the site renderer avoids
    duplicating its URL and chapter-anchor policy here.
    """
    destination = Path(destination).resolve()
    groups = group_nodes(graph)
    project_page = destination / "dependencies.md"
    full_page = destination / "dependencies/full.md"
    chapter_pages = {group: destination / "dependencies/chapters" / f"{group or 'roadmap'}.md" for group in groups}
    scope_maps = scope_views(graph, statuses)
    containers = list(scope_maps)
    scope_pages = {
        node_id: (
            project_page
            if node_id == "roadmap"
            else chapter_pages[node_id]
            if node_id in chapter_pages
            else destination / "dependencies/scopes" / f"{node_id}.md"
        )
        for node_id in containers
    }
    scope_pages["roadmap"] = project_page
    written: list[Path] = []

    project = project_view(graph, statuses)
    project_item_count = sum(len(node_ids) for node_ids in groups.values())
    project_book_links = node_links(project_page, _article_link_ids(project))
    project_links = {
        view_node.id: (
            _published_link(
                chapter_pages.get(
                    view_node.id.removeprefix("scope:"),
                    scope_pages[view_node.id.removeprefix("scope:")],
                ),
                project_page,
            )
            if view_node.kind == "scope"
            else project_book_links[view_node.id]
        )
        for view_node in project.nodes
    }
    written.append(
        _write_page(
            project_page,
            view=project,
            statuses=_selected_statuses(graph, statuses, project),
            links=project_links,
            heading="Dependency maps",
            lead=(
                f"{project_item_count} roadmap entr{'y' if project_item_count == 1 else 'ies'} across "
                f"{len(groups)} chapter{'s' if len(groups) != 1 else ''}."
            ),
            site_root=destination,
        )
    )

    for group, chapter_page in chapter_pages.items():
        view = scope_maps[group] if group in scope_maps else chapter_view(graph, statuses, group)
        links = dict(node_links(chapter_page, _article_link_ids(view)))
        for node in view.nodes:
            if node.kind == "boundary":
                external = node.id.removeprefix("boundary:")
                links[node.id] = _published_link(scope_pages[external], chapter_page)
            elif node.kind == "scope":
                nested = node.id.removeprefix("scope:")
                links[node.id] = _published_link(scope_pages[nested], chapter_page)

        book_page = (
            destination / "roadmap/README.md"
            if group in {"", "roadmap"}
            else destination / "roadmap" / group / "README.md"
        )
        navigation = _navigation(
            ("Project map", _markdown_link(project_page, chapter_page)),
            ("Full dependency graph", _markdown_link(full_page, chapter_page)),
            ("Open textbook chapter", _markdown_link(book_page, chapter_page)),
        )
        written.append(
            _write_page(
                chapter_page,
                view=view,
                statuses=_selected_statuses(graph, statuses, view),
                links=links,
                heading=view.title,
                lead=(
                    f"This map contains {len(groups[group])} roadmap entries in this chapter. "
                    "Dashed chapter boxes stand for external prerequisites or dependents."
                ),
                navigation=navigation,
                site_root=destination,
            )
        )

    for scope in containers:
        if scope in {"roadmap", *chapter_pages}:
            continue
        scope_page = scope_pages[scope]
        view = scope_maps[scope]
        links = dict(node_links(scope_page, _article_link_ids(view)))
        for node in view.nodes:
            if node.kind == "scope":
                nested = node.id.removeprefix("scope:")
                links[node.id] = _published_link(scope_pages[nested], scope_page)
            elif node.kind == "boundary":
                external = node.id.removeprefix("boundary:")
                links[node.id] = _published_link(scope_pages[external], scope_page)
        parent = graph.nodes[scope].parent
        parent_page = scope_pages.get(parent or "roadmap", project_page)
        written.append(
            _write_page(
                scope_page,
                view=view,
                statuses=_selected_statuses(graph, statuses, view),
                links=links,
                heading=view.title,
                lead=(
                    "This map shows the container's direct articles. Nested containers "
                    "are collapsed and clickable; dependency edges are rolled up from their leaves."
                ),
                navigation=_navigation(
                    ("Parent map", _markdown_link(parent_page, scope_page)),
                    ("Full dependency graph", _markdown_link(full_page, scope_page)),
                ),
                site_root=destination,
            )
        )
    complete = full_view(graph, statuses)
    # Unlike a projected scope box, every full-view node is a real authored
    # article, including containers whose presentation kind is ``scope``.
    full_links = node_links(full_page, (node.id for node in complete.nodes))
    full_data = full_page.with_suffix(".json")
    dag_viewer.write_payload(full_data, complete, links=full_links)
    written.append(
        _write_page(
            full_page,
            view=complete,
            statuses=statuses,
            links=full_links,
            heading=complete.title,
            lead=(
                f"{len(graph.nodes)} nodes · {graph.edge_count} dependencies. Arrows point from a "
                "prerequisite to what depends on it; dashed arrows are needed only by proofs. "
                "Drag to pan, scroll to zoom, and search to focus a node."
            ),
            navigation=_navigation(
                ("Project map", _markdown_link(project_page, full_page)),
                ("Download graph data", full_data.name),
            ),
            diagram=dag_viewer.render_container(
                full_data.name,
                script_href=_viewer_script_link(full_page, destination),
            ),
            site_root=destination,
        )
    )

    return tuple(written)


def focus_page_path(destination: str | Path, node_id: str) -> Path:
    """Return the shared explorer page used for a theorem's local context."""
    return Path(destination).resolve() / "dependencies/full.md"


def focus_page_href(destination: str | Path, node_id: str, page: str | Path) -> str:
    """Return a published explorer link with durable node focus in the hash."""
    target = focus_page_path(destination, node_id)
    return f"{mermaid.relative_link(target, Path(page), '.html')}#node={quote(node_id, safe='')}"


def _write_page(
    page: Path,
    *,
    view: GraphView,
    statuses: dict[str, NodeStatus],
    links: Mapping[str, str],
    heading: str,
    lead: str,
    site_root: Path,
    navigation: str = "",
    extra: str = "",
    diagram: str | None = None,
) -> Path:
    if diagram is None:
        candidate = mermaid.render_view_diagram(view, links=dict(links), include_classdefs=False)
        if dag_viewer.requires_interactive(view, candidate):
            payload = page.with_suffix(".json")
            dag_viewer.write_payload(payload, view, links=links)
            diagram = dag_viewer.render_container(
                payload.name,
                script_href=_viewer_script_link(page, site_root),
            )
        else:
            diagram = candidate
    sections = [
        "---",
        "kind: graph",
        f"graph_view: {view.kind}",
        "---",
        "",
        f"# {heading}",
        "",
    ]
    if navigation:
        sections.extend([navigation, ""])
    # The legend rides on the lead sentence rather than sitting under the
    # diagram: it answers a question the reader asks once, not on every page.
    tip = mermaid.render_legend_tip(statuses)
    sections.extend([f"{lead} {tip}".rstrip(), "", diagram, ""])
    if extra:
        sections.extend([extra, ""])
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")
    return page


def _selected_statuses(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    view: GraphView,
) -> dict[str, NodeStatus]:
    containers = frozenset(
        node.parent for node in graph.nodes.values() if node.parent is not None
    )
    return {
        node_id: statuses[node_id]
        for node_id in view.member_ids
        if node_id not in containers and graph.nodes[node_id].formalizable
    }


def _article_link_ids(view: GraphView) -> tuple[str, ...]:
    """Return only view nodes whose links come from published articles.

    Scope and boundary links are derived locally by this module. Asking the
    renderer for every graph node on every scope page made a repository-wide
    wiki spend quadratic time constructing links that the diagram never used.
    """
    return tuple(node.id for node in view.nodes if node.kind == "node")


def _article_link_ids(view: GraphView) -> tuple[str, ...]:
    """Return only view nodes whose links come from published articles.

    Scope and boundary links are derived locally by this module. Asking the
    renderer for every graph node on every scope page made a repository-wide
    wiki spend quadratic time constructing links that the diagram never used.
    """
    return tuple(node.id for node in view.nodes if node.kind == "node")


def _navigation(*items: tuple[str, str]) -> str:
    return " · ".join(f"[{label}]({href})" for label, href in items)


def _markdown_link(target: Path, page: Path) -> str:
    return mermaid.relative_link(target, page, ".md")


def _published_link(target: Path, page: Path) -> str:
    return mermaid.relative_link(target, page, ".html")


def _viewer_script_link(page: Path, site_root: Path) -> str:
    """Link the viewer asset without inferring structure from authored ids."""

    return mermaid.relative_link(
        site_root / "javascripts/blueprint-dag.js",
        page,
        ".js",
    )


__all__ = ["focus_page_href", "focus_page_path", "write_graph_pages"]
