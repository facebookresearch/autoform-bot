"""Publish scalable dependency-explorer projections beside the textbook.

One Markdown DAG supports several reading scales: chapters collapsed into a
project overview, declarations within one chapter with external chapters
collapsed to boundaries, and nested scopes. A search index over every article
jumps to the smallest scope that holds it. Every projection uses the same
explorer, links back to the same book anchors, and never becomes another
source of graph state.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from . import dag_viewer, mermaid
from .graph import Graph
from .graph_views import (
    INVENTORY_STATUSES,
    ROOT_GROUP_TITLE,
    GraphView,
    chapter_view,
    full_view,
    group_nodes,
    group_title,
    is_scope_target,
    project_view,
    scope_views,
)
from .status import NodeStatus

NodeLinks = Callable[[Path, Iterable[str]], Mapping[str, str]]
#: The project explorer's payload and the search index, relative to the site root.
PROJECT_PAYLOAD = "dependencies.json"
SEARCH_INDEX = "dependencies/index.json"
NAVIGATION_INDEX = "dependencies/navigation.json"


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
    root_title = group_title(graph, "roadmap")
    # _write_page writes each payload beside its page.
    project_page = (destination / PROJECT_PAYLOAD).with_suffix(".md")
    full_page = destination / "dependencies/full.md"
    # The book's contents, which the Book tab opens; every explorer trail starts
    # there. A roadmap with neither a root article nor root leaves has no such
    # page, so its trails start at home.
    book_root = destination / ("roadmap/README.md" if "roadmap" in graph.nodes or "roadmap" in groups else "README.md")
    search_index = destination / SEARCH_INDEX
    chapter_pages = {group: destination / "dependencies/chapters" / f"{group or 'roadmap'}.md" for group in groups}
    scope_maps = scope_views(graph, statuses)
    containers = scope_maps.keys()
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
        if node.id not in scope_pages:
            # The book's statement links the same page, so search and the book agree.
            context_links[node.id] = scope_page_href(
                destination, node.id, search_index, parent=graph.nodes[node.id].parent
            )
            continue
        context_links[node.id] = _published_link(scope_pages[node.id], search_index)
    dag_viewer.write_search_index(search_index, complete, context_links=context_links)
    navigation_index = destination / NAVIGATION_INDEX
    navigation_nodes = []
    for node in complete.nodes:
        if node.id not in containers or node.navigation == "transparent":
            continue
        parent = graph.nodes[node.id].parent
        while parent and graph.nodes[parent].navigation in {"collection", "transparent"}:
            parent = graph.nodes[parent].parent
        current: str | None = node.id
        source_branch = False
        while current:
            if graph.nodes[current].navigation == "source":
                source_branch = True
                break
            current = graph.nodes[current].parent
        navigation_nodes.append({
            "id": node.id, "title": node.title, "parent": parent,
            "url": context_links[node.id], "count": node.item_count,
            "source": source_branch,
            "navigation": node.navigation or "topic",
        })
    navigation_index.write_text(json.dumps(
        {"schema": "autoform-dag-navigation/v1", "nodes": navigation_nodes},
        ensure_ascii=False, separators=(",", ":"),
    ) + "\n", encoding="utf-8")

    project = project_view(graph, statuses)
    project_item_count = sum(node.item_count for node in project.nodes)
    # Chapter leads count what the rings count: scope targets plus module inventories.
    chapter_item_counts = {node.id: node.item_count for node in project.nodes}

    def project_links(page: Path) -> dict[str, str]:
        """Link the project view from *page*: chapters to their explorers, articles to the book."""
        book_links = node_links(page, _article_link_ids(project))
        links = {}
        for view_node in project.nodes:
            scope = view_node.id.removeprefix("scope:")
            links[view_node.id] = (
                _published_link(chapter_pages.get(scope) or scope_pages[scope], page)
                if view_node.kind == "scope"
                else book_links[view_node.id]
            )
        return links

    written.append(
        _write_page(
            project_page,
            view=project,
            statuses=_selected_statuses(graph, statuses, project, containers),
            links=project_links(project_page),
            heading=root_title,
            lead=(
                f"{project_item_count} item{'s' if project_item_count != 1 else ''} across "
                f"{len(groups)} chapter{'s' if len(groups) != 1 else ''}."
            ),
            navigation="",
            site_root=destination,
            search_href=_search_index_link(search_index, project_page),
            breadcrumbs=(
                ("Book", _published_link(book_root, project_page)),
                (root_title, None),
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
            book_root
            if group in {"", "roadmap"}
            else destination / "roadmap" / group / "README.md"
        )
        navigation = _navigation(
            ("Dependency explorer", _markdown_link(project_page, chapter_page)),
            ("Open textbook chapter", _markdown_link(book_page, chapter_page)),
        )
        # The root's map draws every chapter beside the root's own articles.
        item_count = (
            project_item_count
            if group == "roadmap" and group in scope_maps
            else chapter_item_counts[f"scope:{group or 'roadmap'}"]
        )
        written.append(
            _write_page(
                chapter_page,
                view=view,
                # Script hides the navigation that carries this link, so the explorer carries it.
                book=_published_link(book_page, chapter_page),
                statuses=_selected_statuses(graph, statuses, view, containers),
                links=links,
                heading=_dependency_heading(view, ROOT_GROUP_TITLE if group in {"", "roadmap"} else None),
                lead=(
                    f"This map covers {item_count} item{'s' if item_count != 1 else ''} in this chapter. "
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
                    book_root=book_root,
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
                book=_published_link(destination / "roadmap" / scope / "README.md", scope_page),
                statuses=_selected_statuses(graph, statuses, view, containers),
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
                    book_root=book_root,
                ),
            )
        )
    # Keep the old full route as a compatibility shell, but never fit every
    # leaf into one unreadable canvas. Its global index redirects legacy
    # ``#node=`` links into the nearest hierarchical scope.
    written.append(
        _write_page(
            full_page,
            view=project,
            statuses=_selected_statuses(graph, statuses, project, containers),
            links=project_links(full_page),
            heading=root_title,
            lead=(
                f"Search {len(graph.nodes)} roadmap entries without flattening them into one canvas; "
                "a result opens its chapter or nested scope."
            ),
            navigation=_navigation(
                ("Dependency explorer", _markdown_link(project_page, full_page)),
                ("Download search index", search_index.name),
            ),
            site_root=destination,
            search_href=_search_index_link(search_index, full_page),
            # It repeats the project page's title, so site search would list both.
            searchable=False,
            breadcrumbs=(
                ("Book", _published_link(book_root, full_page)),
                (root_title, _published_link(project_page, full_page)),
                ("Explorer", None),
            ),
        )
    )

    return tuple(written)


def scope_page_path(destination: str | Path, node_id: str, *, parent: str | None = None) -> Path:
    """Return the smallest published explorer scope that contains a node.

    A node without a parent sits at the root, so the root chapter holds it.
    """
    destination = Path(destination).resolve()
    scope = parent if parent is not None else (node_id.rpartition("/")[0] or "roadmap")
    if scope == "roadmap":
        return destination / "dependencies/chapters/roadmap.md"
    if "/" not in scope:
        return destination / "dependencies/chapters" / f"{scope}.md"
    return destination / "dependencies/scopes" / f"{scope}.md"


def scope_page_href(
    destination: str | Path,
    node_id: str,
    page: str | Path,
    *,
    parent: str | None = None,
) -> str:
    """Link a node's scope page from *page*, selecting the node in the hash."""
    target = scope_page_path(destination, node_id, parent=parent)
    return f"{mermaid.relative_link(target, Path(page), '.html')}#node={quote(node_id, safe='')}"


def _write_page(
    page: Path,
    *,
    view: GraphView,
    book: str | None = None,
    statuses: dict[str, NodeStatus],
    links: Mapping[str, str],
    heading: str,
    lead: str,
    site_root: Path,
    search_href: str,
    breadcrumbs: tuple[tuple[str, str | None], ...],
    navigation: str = "",
    extra: str = "",
    searchable: bool = True,
) -> Path:
    # Every projection is one payload and one explorer host, so readers get one
    # interaction model and no second renderer can drift from it.
    payload = page.with_suffix(".json")
    dag_viewer.write_payload(
        payload,
        replace(view, title=heading),
        links=links,
        breadcrumbs=breadcrumbs,
        book=book,
    )
    explorer = dag_viewer.render_container(
        quote(payload.name),
        script_href=_viewer_script_link(page, site_root),
        style_href=mermaid.relative_link(site_root / dag_viewer.STYLE_PATH, page, ".css"),
        fallback_links=(
            (node.title, links[node.id])
            for node in view.nodes[:50]
            if links.get(node.id)
        ),
        fallback_total=len(view.nodes),
        search_href=search_href,
        navigation_href=_search_index_link(site_root / NAVIGATION_INDEX, page),
    )
    sections = [
        "---",
        "kind: graph",
        f"graph_view: {view.kind}",
        *(() if searchable else ("search:", "  exclude: true")),
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
    tip = mermaid.render_legend_tip(statuses, _view_inventories(view))
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
    containers: Collection[str],
) -> dict[str, NodeStatus]:
    """Select the leaves the explorer's rings count, for the legend beside them.

    ``containers`` holds every node with children. The caller passes its
    scope maps' keys, since rebuilding the set would walk the whole graph
    for every page.
    """
    return {
        node_id: statuses[node_id]
        for node_id in view.member_ids
        if node_id not in containers and is_scope_target(graph.nodes[node_id])
    }


def _view_inventories(view: GraphView) -> dict[str, int]:
    """Count the module inventories the explorer's rings and nodes show, for the legend."""
    counts = dict.fromkeys(INVENTORY_STATUSES, 0)
    for node in view.nodes:
        for key, count in node.status_counts:
            if key in counts:
                counts[key] += count
    return counts


def _dependency_heading(view: GraphView, subject: str | None = None) -> str:
    """Name an explorer page for *subject*, by default the view's scope, and for what it draws.

    The views keep their Mermaid-era "<scope> dependency map" titles, so that suffix goes.
    """
    subject = subject or view.title.removesuffix(" dependency map")
    return f"{subject} {'knowledge map' if dag_viewer.uses_atlas(view) else 'dependencies'}"


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
    # MkDocs publishes README.md as index.html. It rewrites the links it
    # parses, but never these, which reach the explorer as JSON.
    if target.name == "README.md":
        target = target.with_name("index.md")
    return mermaid.relative_link(target, page, ".html")


def _viewer_script_link(page: Path, site_root: Path) -> str:
    """Link the viewer asset from an explicit publication root."""

    return mermaid.relative_link(
        site_root / dag_viewer.SCRIPT_PATH,
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
    book_root: Path,
) -> tuple[tuple[str, str | None], ...]:
    lineage: list[str] = []
    current: str | None = scope
    while current and current != "roadmap":
        lineage.append(current)
        current = graph.nodes[current].parent if current in graph.nodes else None
    items: list[tuple[str, str | None]] = [
        ("Book", _published_link(book_root, page)),
        (group_title(graph, "roadmap"), _published_link(project_page, page)),
    ]
    for node_id in reversed(lineage):
        if node_id != scope and graph.nodes[node_id].navigation in {"collection", "transparent"}:
            continue
        target = scope_pages.get(node_id)
        label = graph.nodes[node_id].title if node_id in graph.nodes else node_id
        items.append((label, _published_link(target, page) if target and target != page else None))
    if not lineage:
        # The crumb before already carries the roadmap's title.
        items.append((ROOT_GROUP_TITLE, None))
    return tuple(items)


__all__ = ["PROJECT_PAYLOAD", "SEARCH_INDEX", "scope_page_href", "scope_page_path", "write_graph_pages"]
