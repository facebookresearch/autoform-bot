"""Publish scalable dependency-explorer projections beside the textbook.

One Markdown DAG supports several reading scales: chapters collapsed into a
project overview, declarations within one chapter with external chapters
collapsed to boundaries, nested scopes, and the full graph. Every projection
uses the same explorer, links back to the same book anchors, and never becomes
another source of graph state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
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
    search_index = destination / "dependencies/index.json"
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

    complete = full_view(graph, statuses)
    context_links: dict[str, str] = {}
    for node in complete.nodes:
        target = (
            chapter_pages["roadmap"]
            if node.id == "roadmap" and "roadmap" in chapter_pages
            else scope_pages.get(node.id)
        )
        if target is None:
            parent = graph.nodes[node.id].parent or "roadmap"
            target = (
                chapter_pages["roadmap"]
                if parent == "roadmap" and "roadmap" in chapter_pages
                else scope_pages.get(parent, project_page)
            )
        context = _published_link(target, search_index)
        if node.id not in scope_pages:
            context += f"#node={quote(node.id, safe='')}"
        context_links[node.id] = context
    dag_viewer.write_search_index(search_index, complete, context_links=context_links)

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
            heading="Mathematics atlas",
            lead=(
                f"{project_item_count} roadmap entr{'y' if project_item_count == 1 else 'ies'} across "
                f"{len(groups)} chapter{'s' if len(groups) != 1 else ''}."
            ),
            navigation="",
            site_root=destination,
            search_href=_search_index_link(search_index, project_page),
            breadcrumbs=(
                ("Book", _published_link(destination / "README.md", project_page)),
                ("All mathematics", None),
            ),
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
            ("Dependency explorer", _markdown_link(project_page, chapter_page)),
            ("Open textbook chapter", _markdown_link(book_page, chapter_page)),
        )
        written.append(
            _write_page(
                chapter_page,
                view=view,
                statuses=_selected_statuses(graph, statuses, view),
                links=links,
                heading=_dependency_heading(view),
                lead=(
                    f"This view contains {len(groups[group])} roadmap entries in this chapter. "
                    "Collapsed chapter nodes stand for external prerequisites or dependents."
                ),
                navigation=navigation,
                site_root=destination,
                search_href=_search_index_link(search_index, chapter_page),
                breadcrumbs=_scope_breadcrumbs(
                    graph,
                    group,
                    page=chapter_page,
                    scope_pages=scope_pages,
                    project_page=project_page,
                ),
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
                heading=_dependency_heading(view),
                lead=(
                    "This view shows the container's direct articles. Nested containers "
                    "are collapsed and clickable; dependency edges are rolled up from their leaves."
                ),
                navigation=_navigation(
                    ("Parent view", _markdown_link(parent_page, scope_page)),
                    ("Dependency explorer", _markdown_link(project_page, scope_page)),
                ),
                site_root=destination,
                search_href=_search_index_link(search_index, scope_page),
                breadcrumbs=_scope_breadcrumbs(
                    graph,
                    scope,
                    page=scope_page,
                    scope_pages=scope_pages,
                    project_page=project_page,
                ),
            )
        )
    # Keep the old full route as a compatibility shell, but never fit every
    # leaf into one unreadable canvas. Its global index redirects legacy
    # ``#node=`` links into the nearest hierarchical scope.
    full_project_book_links = node_links(full_page, _article_link_ids(project))
    full_project_links = {
        view_node.id: (
            _published_link(
                chapter_pages.get(
                    view_node.id.removeprefix("scope:"),
                    scope_pages[view_node.id.removeprefix("scope:")],
                ),
                full_page,
            )
            if view_node.kind == "scope"
            else full_project_book_links[view_node.id]
        )
        for view_node in project.nodes
    }
    written.append(
        _write_page(
            full_page,
            view=project,
            statuses=_selected_statuses(graph, statuses, project),
            links=full_project_links,
            heading="Mathematics atlas",
            lead=(
                f"Browse {len(graph.nodes)} roadmap entries without flattening them into one canvas. "
                "Search jumps directly to the relevant chapter or nested scope."
            ),
            navigation=_navigation(
                ("Dependency explorer", _markdown_link(project_page, full_page)),
                ("Download search index", search_index.name),
            ),
            site_root=destination,
            search_href=_search_index_link(search_index, full_page),
            breadcrumbs=(
                ("Book", _published_link(destination / "README.md", full_page)),
                ("All mathematics", _published_link(project_page, full_page)),
                ("Explorer", None),
            ),
        )
    )

    return tuple(written)


def focus_page_path(destination: str | Path, node_id: str, *, parent: str | None = None) -> Path:
    """Return the smallest published explorer scope that contains a node."""
    destination = Path(destination).resolve()
    scope = parent if parent is not None else node_id.rpartition("/")[0]
    if scope == "":
        return destination / "dependencies.md"
    if scope == "roadmap":
        return destination / "dependencies/chapters/roadmap.md"
    if "/" not in scope:
        return destination / "dependencies/chapters" / f"{scope}.md"
    return destination / "dependencies/scopes" / f"{scope}.md"


def focus_page_href(
    destination: str | Path,
    node_id: str,
    page: str | Path,
    *,
    parent: str | None = None,
) -> str:
    """Return a published explorer link with durable node focus in the hash."""
    target = focus_page_path(destination, node_id, parent=parent)
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
    search_href: str,
    breadcrumbs: tuple[tuple[str, str | None], ...],
    navigation: str = "",
    extra: str = "",
) -> Path:
    # Size used to pick between a Mermaid diagram and the Canvas explorer.
    # Keeping every projection on one payload-and-host path gives readers one
    # interaction model and prevents the two renderers from drifting apart.
    payload = page.with_suffix(".json")
    dag_viewer.write_payload(
        payload,
        replace(view, title=heading),
        links=links,
        breadcrumbs=breadcrumbs,
    )
    explorer = dag_viewer.render_container(
        payload.name,
        script_href=_viewer_script_link(page, site_root),
        fallback_links=(
            (node.title, links[node.id])
            for node in view.nodes[:50]
            if links.get(node.id)
        ),
        fallback_total=len(view.nodes),
        search_href=search_href,
    )
    sections = [
        "---",
        "kind: graph",
        f"graph_view: {view.kind}",
        "hide:",
        "  - navigation",
        "  - toc",
        "  - footer",
        "---",
        "",
        f"# {heading}",
        "",
    ]
    if navigation:
        sections.extend([navigation, ""])
    # The legend rides on the lead sentence rather than sitting under the
    # explorer: it answers a question the reader asks once, not on every page.
    tip = mermaid.render_legend_tip(statuses)
    sections.extend([f"{lead} {tip}".rstrip(), "", explorer, ""])
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


def _dependency_heading(view: GraphView) -> str:
    """Name a projected explorer view without carrying over Mermaid-era copy."""
    subject = view.title.removesuffix(" dependency map")
    return f"{subject} {'dependencies' if view.edges else 'knowledge map'}"


def _article_link_ids(view: GraphView) -> tuple[str, ...]:
    """Return only view nodes whose links come from published articles.

    Scope and boundary links are derived locally by this module. Asking the
    renderer for every graph node on every scope page made a repository-wide
    wiki spend quadratic time constructing links that the projection never used.
    """
    return tuple(node.id for node in view.nodes if node.kind == "node")


def _navigation(*items: tuple[str, str]) -> str:
    return " · ".join(f"[{label}]({href})" for label, href in items)


def _markdown_link(target: Path, page: Path) -> str:
    return mermaid.relative_link(target, page, ".md")


def _published_link(target: Path, page: Path) -> str:
    return mermaid.relative_link(target, page, ".html")


def _viewer_script_link(page: Path, site_root: Path) -> str:
    """Link the viewer asset from an explicit publication root."""

    return mermaid.relative_link(
        site_root / "javascripts/blueprint-dag.js",
        page,
        ".js",
    )


def _search_index_link(search_index: Path, page: Path) -> str:
    return mermaid.relative_link(search_index, page, ".json")


def _scope_breadcrumbs(
    graph: Graph,
    scope: str,
    *,
    page: Path,
    scope_pages: Mapping[str, Path],
    project_page: Path,
) -> tuple[tuple[str, str | None], ...]:
    lineage: list[str] = []
    current: str | None = scope
    while current and current != "roadmap":
        lineage.append(current)
        current = graph.nodes[current].parent if current in graph.nodes else None
    items: list[tuple[str, str | None]] = [
        ("Book", _published_link(project_page.parent / "README.md", page)),
        ("All mathematics", _published_link(project_page, page)),
    ]
    for node_id in reversed(lineage):
        target = scope_pages.get(node_id)
        label = graph.nodes[node_id].title if node_id in graph.nodes else node_id
        items.append((label, _published_link(target, page) if target and target != page else None))
    if not lineage:
        label = graph.nodes[scope].title if scope in graph.nodes else scope.replace("-", " ").title()
        items.append((label, None))
    return tuple(items)


__all__ = ["focus_page_href", "focus_page_path", "write_graph_pages"]
