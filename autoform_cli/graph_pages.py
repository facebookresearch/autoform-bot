"""Publish scalable graph data for the integrated mathematical book.

One Markdown DAG supports several reading scales: chapters collapsed into a
project overview, declarations within one chapter with external chapters
collapsed to boundaries, and nested scopes. A search index over every article
jumps to the smallest scope that holds it. Every projection uses the same
explorer, links to the same book anchors, and never becomes another
source of graph state.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from . import dag_viewer, mermaid
from .graph import Graph
from .graph_views import (
    FLATTENED_ROLES,
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
from .markdown import header_title, rendered_visible_text, title_text
from .status import NodeStatus

NodeLinks = Callable[[Path, Iterable[str]], Mapping[str, str]]
#: The project explorer's payload and the search index, relative to the site root.
PROJECT_PAYLOAD = "dependencies.json"
SEARCH_INDEX = "dependencies/index.json"


def book_contents(graph: Graph, groups: Collection[str]) -> Path:
    """The book's contents page, relative to the site, which the Book tab opens.

    Every explorer trail starts there. A roadmap with neither a root article
    nor root leaves has no such page, so its trails start at home.
    """
    return Path("roadmap/README.md" if "roadmap" in graph.nodes or "roadmap" in groups else "README.md")


def graph_page_paths(graph: Graph) -> tuple[str, ...]:
    """Every file :func:`write_graph_pages` writes for ``graph``, relative to
    the site's root: each page, the payload beside it, and the search index."""

    root = Path("/")
    project_page, full_page, chapter_pages, scope_pages = _pages(graph, root, group_nodes(graph))
    pages = {project_page, full_page, *chapter_pages.values(), *scope_pages.values()}
    files = {*pages, *(page.with_suffix(".json") for page in pages), root / SEARCH_INDEX}
    return tuple(sorted(path.relative_to(root).as_posix() for path in files))


def _pages(
    graph: Graph, destination: Path, groups: Collection[str]
) -> tuple[Path, Path, dict[str, Path], dict[str, Path]]:
    """The project, full, chapter, and scope pages for ``graph``, whose
    :func:`group_nodes` are ``groups``, under ``destination``."""

    # _write_page writes each payload beside its page.
    project_page = (destination / PROJECT_PAYLOAD).with_suffix(".md")
    full_page = destination / "dependencies/full.md"
    chapter_pages = {group: destination / "dependencies/chapters" / f"{group or 'roadmap'}.md" for group in groups}
    parents = {node.parent for node in graph.nodes.values() if node.parent is not None}
    scope_pages = {
        node_id: (
            project_page
            if node_id == "roadmap"
            else chapter_pages[node_id]
            if node_id in chapter_pages
            else destination / "dependencies/scopes" / f"{node_id}.md"
        )
        for node_id in graph.nodes
        if node_id in parents
    }
    scope_pages["roadmap"] = project_page
    return project_page, full_page, chapter_pages, scope_pages


def publication_contexts(graph: Graph, destination: str | Path) -> dict[str, tuple[Path, str]]:
    """The existing payload and containing graph scope for each published node.

    Containers identify their own scope; other articles identify their actual
    parent. Authored roles remain available for the client to skip wrappers.
    Paths locate publication artifacts, never mathematical areas.
    """
    destination = Path(destination).resolve()
    project, full, chapters, scopes = _pages(graph, destination, group_nodes(graph))
    known = {project, full, *chapters.values(), *scopes.values()}
    parents = {node.parent for node in graph.nodes.values() if node.parent is not None}
    result: dict[str, tuple[Path, str]] = {}
    for node in graph.nodes.values():
        payload = scopes.get(node.id) or scope_page_path(destination, node.id, parent=node.parent)
        if payload not in known:
            payload = scopes.get(node.parent or "roadmap", project)
        scope = node.id if node.id in parents else node.parent or "roadmap"
        result[node.id] = (payload.with_suffix(".json"), scope)
    return result


def write_graph_pages(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    destination: str | Path,
    *,
    node_links: NodeLinks,
    book_targets: Mapping[str, tuple[Path, str]] | None = None,
) -> tuple[Path, ...]:
    """Write project, chapter, nested-scope, and shared full-explorer pages.

    ``node_links`` resolves theorem ids to their published textbook anchors for
    each generated page.  Keeping that callback in the site renderer avoids
    duplicating its URL and chapter-anchor policy here. With ``book_targets``,
    pages are compatibility entrances to canonical full-text book routes.
    """
    destination = Path(destination).resolve()
    groups = group_nodes(graph)
    root_title = group_title(graph, "roadmap")
    project_page, full_page, chapter_pages, scope_pages = _pages(graph, destination, groups)
    book_root = destination / book_contents(graph, groups)
    search_index = destination / SEARCH_INDEX
    scope_maps = scope_views(graph, statuses)
    containers = scope_maps.keys()
    written: list[Path] = []

    complete = full_view(graph, statuses)
    contexts = publication_contexts(graph, destination)
    context_links: dict[str, str] = {}
    metadata: dict[str, dict[str, object]] = {}
    # One bounded whole-index request, not one whole-graph request per page.
    # These exact links come from the renderer's captured publication targets.
    routes = node_links(search_index, tuple(graph.nodes)) if book_targets is not None else {}
    for node in complete.nodes:
        payload, scope = contexts[node.id]
        original = graph.nodes[node.id]
        metadata[node.id] = {
            "scope": scope,
            "data_url": _search_index_link(payload, search_index),
            "parent": original.parent,
            "navigation": original.navigation,
            "area": original.area,
        }
        if node.id in routes:
            metadata[node.id]["route"] = routes[node.id]
        if node.id not in scope_pages:
            # The book's statement links the same page, so search and the book agree.
            context_links[node.id] = _published_link(payload, search_index) + f"#node={quote(node.id, safe='')}"
            continue
        context_links[node.id] = _published_link(scope_pages[node.id], search_index)
    dag_viewer.write_search_index(
        search_index, complete, context_links=context_links, metadata=metadata, include_relations=True,
    )

    project = project_view(graph, statuses)
    project_item_count = sum(node.item_count for node in project.nodes)
    # Chapter leads count what the rings count: scope targets plus module inventories.
    chapter_item_counts = {node.id: node.item_count for node in project.nodes}

    # A chapter links its own page. The root's own articles are drawn on
    # their Top level page, not the project's.
    scope_targets = {**scope_pages, **chapter_pages}
    if book_targets is not None:
        scope_targets = {
            scope: book_targets.get(scope, (book_root, ""))[0]
            for scope in scope_targets
        }

    def canonical(scope: str | None = None) -> Path | None:
        if book_targets is None:
            return None
        return book_targets.get(scope or "roadmap", (book_root, ""))[0]

    def view_links(view: GraphView, page: Path) -> dict[str, str]:
        """Link *view* from *page*: articles to the book, scopes and boundaries to their explorers."""
        links = dict(node_links(page, _article_link_ids(view)))
        for view_node in view.nodes:
            if view_node.kind != "node":
                links[view_node.id] = _published_link(scope_targets[view_node.id.partition(":")[2]], page)
        return links

    written.append(
        _write_page(
            project_page,
            view=project,
            statuses=_selected_statuses(graph, statuses, project, containers),
            links=view_links(project, project_page),
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
            canonical=canonical(),
        )
    )

    for group, chapter_page in chapter_pages.items():
        view = scope_maps[group] if group in scope_maps else chapter_view(graph, statuses, group)
        links = view_links(view, chapter_page)

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
                    scope_pages=scope_targets,
                    project_page=canonical() or project_page,
                    book_root=book_root,
                ),
                canonical=canonical(group),
            )
        )

    for scope in containers:
        if scope in {"roadmap", *chapter_pages}:
            continue
        scope_page = scope_pages[scope]
        view = scope_maps[scope]
        links = view_links(view, scope_page)
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
                    scope_pages=scope_targets,
                    project_page=canonical() or project_page,
                    book_root=book_root,
                ),
                canonical=canonical(scope),
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
            links=view_links(project, full_page),
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
            canonical=canonical(),
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
    searchable: bool = True,
    canonical: Path | None = None,
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
        page_href=_published_link(canonical, page) if canonical is not None else None,
        scope=view.scope or "roadmap",
    )
    sections = [
        "---",
        "kind: graph",
        f"graph_view: {view.kind}",
        *(() if searchable and canonical is None else ("search:", "  exclude: true")),
        "hide:",
        "  - navigation",
        "  - toc",
        "  - footer",
    ]
    if title_text(heading) != heading:
        # MkDocs would take the title from the heading, whose references the
        # theme reads back as characters in <title>, so it is named here.
        sections.append(header_title(rendered_visible_text(heading)))
    sections.extend(["---", "", f"# {title_text(heading)}", ""])
    if navigation and canonical is None:
        sections.extend([navigation, ""])
    # The legend rides on the lead sentence rather than sitting under the
    # explorer: it answers a question the reader asks once, not on every page.
    tip = mermaid.render_legend_tip(statuses, _view_inventories(view))
    if canonical is not None:
        href = _published_link(canonical, page)
        reading = (
            '<div class="bp-reading-source" markdown="1">\n\n'
            'This compatibility route opens the same mathematical page and its graph.\n\n'
            f'[Continue to the mathematical page]({href})\n\n</div>'
        )
        sections.extend(['<div class="bp-explorer-page" markdown="1">', "", reading, "", explorer, "", "</div>", ""])
    else:
        sections.extend([f"{lead} {tip}".rstrip(), "", explorer, ""])
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
    for node in dag_viewer.drawn_view(view).nodes:
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
        if node_id != scope and graph.nodes[node_id].navigation in FLATTENED_ROLES:
            continue
        target = scope_pages.get(node_id)
        label = graph.nodes[node_id].title if node_id in graph.nodes else node_id
        items.append((label, _published_link(target, page) if target and target != page else None))
    if not lineage:
        # The crumb before already carries the roadmap's title.
        items.append((ROOT_GROUP_TITLE, None))
    return tuple(items)


__all__ = [
    "PROJECT_PAYLOAD",
    "SEARCH_INDEX",
    "graph_page_paths",
    "publication_contexts",
    "scope_page_href",
    "scope_page_path",
    "write_graph_pages",
]
