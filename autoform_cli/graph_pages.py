"""Publish scalable graph projections beside the textbook blueprint.

One Markdown DAG supports several reading scales: chapters collapsed into a
project map, declarations within one chapter with external chapters collapsed
to boundaries, a theorem's one-hop neighborhood, and an optional full graph.
Every projection links back to the same book anchors and never becomes another
source of graph state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from . import mermaid
from .graph import Graph
from .graph_views import (
    GraphView,
    chapter_view,
    focus_views,
    full_view,
    group_nodes,
    project_view,
    scope_view,
)
from .status import NodeStatus


NodeLinks = Callable[[Path], Mapping[str, str]]


def write_graph_pages(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    destination: str | Path,
    *,
    node_links: NodeLinks,
) -> tuple[Path, ...]:
    """Write project, chapter, local, and full graph pages.

    ``node_links`` resolves theorem ids to their published textbook anchors for
    each generated page.  Keeping that callback in the site renderer avoids
    duplicating its URL and chapter-anchor policy here.
    """
    destination = Path(destination).resolve()
    groups = group_nodes(graph)
    local_views = focus_views(graph, statuses)
    project_page = destination / "dependencies.md"
    full_page = destination / "dependencies/full.md"
    chapter_pages = {group: destination / "dependencies/chapters" / f"{group or 'roadmap'}.md" for group in groups}
    containers = [node_id for node_id in graph.nodes if graph.children(node_id)]
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
    article_groups = {node_id: group for group, node_ids in groups.items() for node_id in node_ids}
    focus_pages = {node_id: destination / "dependencies/nodes" / f"{node_id}.md" for node_id in article_groups}
    written: list[Path] = []

    project = project_view(graph, statuses)
    project_item_count = sum(len(node_ids) for node_ids in groups.values())
    project_book_links = node_links(project_page)
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
            statuses=statuses,
            links=project_links,
            heading="Dependency maps",
            lead=(
                f"{project_item_count} item{'s' if project_item_count != 1 else ''} across "
                f"{len(groups)} chapter{'s' if len(groups) != 1 else ''}."
            ),
        )
    )

    for group, chapter_page in chapter_pages.items():
        view = chapter_view(graph, statuses, group)
        links = dict(node_links(chapter_page))
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
            ("Full theorem DAG", _markdown_link(full_page, chapter_page)),
            ("Open textbook chapter", _markdown_link(book_page, chapter_page)),
        )
        written.append(
            _write_page(
                chapter_page,
                view=view,
                statuses=_selected_statuses(statuses, view),
                links=links,
                heading=view.title,
                lead=(
                    f"This map contains the {len(groups[group])} decomposed items in this chapter. "
                    "Dashed chapter boxes stand for external prerequisites or dependents."
                ),
                navigation=navigation,
            )
        )

    for scope in containers:
        if scope in {"roadmap", *chapter_pages}:
            continue
        scope_page = scope_pages[scope]
        view = scope_view(graph, statuses, scope)
        links = dict(node_links(scope_page))
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
                statuses=_selected_statuses(statuses, view),
                links=links,
                heading=view.title,
                lead=(
                    "This map shows the container's direct articles. Nested containers "
                    "are collapsed and clickable; dependency edges are rolled up from their leaves."
                ),
                navigation=_navigation(
                    ("Parent map", _markdown_link(parent_page, scope_page)),
                    ("Full theorem DAG", _markdown_link(full_page, scope_page)),
                ),
            )
        )
    complete = full_view(graph, statuses)
    written.append(
        _write_page(
            full_page,
            view=complete,
            statuses=statuses,
            links=node_links(full_page),
            heading=complete.title,
            lead=(
                f"{len(graph.nodes)} nodes · {graph.edge_count} dependencies. Arrows point from a "
                "prerequisite to what depends on it; dashed arrows are needed only by proofs."
            ),
            navigation=_navigation(("Project map", _markdown_link(project_page, full_page))),
        )
    )

    for node_id, focus_page in focus_pages.items():
        view = local_views[node_id]
        parent = graph.nodes[node_id].parent
        chapter_page = scope_pages.get(parent or article_groups[node_id], chapter_pages[article_groups[node_id]])
        statement_href = _markdown_document_link(node_links(focus_page)[node_id])
        navigation = _navigation(
            ("Project map", _markdown_link(project_page, focus_page)),
            ("Chapter map", _markdown_link(chapter_page, focus_page)),
            ("Full theorem DAG", _markdown_link(full_page, focus_page)),
            ("Open textbook statement", statement_href),
        )
        written.append(
            _write_page(
                focus_page,
                view=view,
                statuses=_selected_statuses(statuses, view),
                links=node_links(focus_page),
                heading=view.title,
                lead=(
                    "This local map shows one dependency hop in either direction. "
                    "The highlighted item is the current focus."
                ),
                navigation=navigation,
            )
        )

    return tuple(written)


def focus_page_path(destination: str | Path, node_id: str) -> Path:
    """Return the generated local-context page for a theorem node."""
    return Path(destination).resolve() / "dependencies/nodes" / f"{node_id}.md"


def _write_page(
    page: Path,
    *,
    view: GraphView,
    statuses: dict[str, NodeStatus],
    links: Mapping[str, str],
    heading: str,
    lead: str,
    navigation: str = "",
    extra: str = "",
) -> Path:
    diagram = mermaid.render_view_diagram(view, links=dict(links), include_classdefs=False)
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
    statuses: dict[str, NodeStatus],
    view: GraphView,
) -> dict[str, NodeStatus]:
    return {node_id: statuses[node_id] for node_id in view.member_ids}


def _navigation(*items: tuple[str, str]) -> str:
    return " · ".join(f"[{label}]({href})" for label, href in items)


def _markdown_link(target: Path, page: Path) -> str:
    return mermaid.relative_link(target, page, ".md")


def _published_link(target: Path, page: Path) -> str:
    return mermaid.relative_link(target, page, ".html")


def _markdown_document_link(href: str) -> str:
    """Turn a raw published-page URL back into a link MkDocs can validate."""
    path, separator, fragment = href.partition("#")
    document = Path(path)
    if document.name == "index.html":
        document = document.parent / "README.md"
    elif document.suffix == ".html":
        document = document.with_suffix(".md")
    return f"{document.as_posix()}{separator}{fragment}"


__all__ = ["focus_page_path", "write_graph_pages"]
