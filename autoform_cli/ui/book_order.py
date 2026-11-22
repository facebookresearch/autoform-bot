"""The book's reading order: its pages down the containment tree, and every node in the order it is read."""

from __future__ import annotations

import heapq
from collections.abc import Collection, Mapping
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .. import status
from .._fs import relative as _relative
from .._fs import resolve as _resolve
from ..graph import Graph, _containers
from ..markdown import outside_fences as _outside_fences
from ..markdown import published_page
from .page_links import _MARKDOWN_LINK, _link_mask


def _book_sources(graph: Graph) -> dict[Path, str]:
    """The book's pages, containers and narrative articles, by resolved source."""
    containers = _containers(graph)
    return {
        _resolve(node.path): node.id
        for node in graph.nodes.values()
        if node.id in containers or (not node.formalizable and node.catalog is None)
    }


def _book_links(source: Path, blueprint: Path, targets: Collection[Path]) -> list[Path]:
    """The *targets* *source* links outside code fences, in the order it links them, on the page render publishes.

    That page leaves out the HTML comments outside code, so a commented-out link
    places nothing, while a ``<!--`` in a code span stays text, as on the site.
    """
    linked_sources: list[Path] = []

    def collect(line: str) -> str:
        for match in _MARKDOWN_LINK.finditer(_link_mask(line)):
            raw = line[match.start("target") : match.end("target")]
            bare = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
            path = bare.partition("#")[0].partition("?")[0]
            try:
                if not path or urlsplit(path).scheme or path.startswith("/"):
                    continue
                candidate = _resolve(source.parent / unquote(path))
                candidate_relative = _relative(candidate, blueprint)
            except ValueError:  # a target outside the vault, an unbalanced IPv6 bracket, or a decoded NUL
                continue
            if (
                not candidate_relative.parts
                or candidate_relative.parts[0] != "roadmap"
                or candidate.suffix.lower() != ".md"
            ):
                continue
            if candidate not in targets:
                continue
            linked_sources.append(candidate)
        return line

    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []  # the site leaves out a page that is not UTF-8, as check warns
    _outside_fences(published_page(text), collect, joined=True)
    return linked_sources


def _book_page_order(blueprint: Path, destination: Path, graph: Graph, walk: list[Path] | None = None) -> list[Path]:
    """The landing page, then the book in reading order (:func:`_reading_walk`).

    SUMMARY.md follows this order, and Next up and the search index follow the
    same walk, so the book reads one way everywhere. A caller that has walked
    the book already passes *walk*."""
    landing = (destination / "README.md").resolve()
    pages = _written(walk if walk is not None else _reading_walk(blueprint, destination, graph))
    return ([landing] if landing.is_file() else []) + [page for page in pages if page != landing]


def _written(walk: list[Path]) -> list[Path]:
    """The pages of *walk* written so far, resolved."""
    return [_resolve(output) for output in walk if output.is_file()]


def _reading_walk(blueprint: Path, destination: Path, graph: Graph) -> list[Path]:
    """Where the book's pages are published, in reading order, written yet or
    not: down the containment tree from the landing page, each page's children in the order it links them,
    then the ones it does not link. A child the page never links directly
    takes the place of the page's first link into its subtree, to a section
    or a result; a direct link wins over that, so a prose shortcut to a deep
    milestone does not pull its chapter forward. A link to a page elsewhere in
    the tree does not move it. The children a page does not link at all
    follow in dependency order, ties by id (:func:`_dependency_sorted`)."""
    book_sources = _book_sources(graph)
    node_sources = {_resolve(node.path): node.id for node in graph.nodes.values()}
    children: dict[str | None, list[str]] = {}
    for node_id in sorted(graph.nodes):
        if _resolve(graph.nodes[node_id].path) in book_sources:
            children.setdefault(graph.nodes[node_id].parent, []).append(node_id)
    precedence: dict[str, set[str]] | None = None
    ordered: list[Path] = []
    pending: list[tuple[Path, str | None]] = [(blueprint / "README.md", None)]
    while pending:
        source, parent = pending.pop()
        if parent is not None:
            ordered.append(destination / _relative(source, blueprint))
        own = children.get(parent, [])
        linked = [node_sources[path] for path in _book_links(source, blueprint, node_sources)] if source.is_file() else []
        direct: dict[str, int] = {}
        through: dict[str, int] = {}
        for index, node_id in enumerate(linked):
            child = node_id if node_id in own else _child_containing(graph, parent, node_id)
            if child in own:
                (direct if child == node_id else through).setdefault(child, index)
        placed = through | direct
        unlinked = [node_id for node_id in own if node_id not in placed]
        if len(unlinked) > 1:
            if precedence is None:
                precedence = _sibling_precedence(graph)
            unlinked = _dependency_sorted(unlinked, precedence)
        reading = [*sorted(placed, key=placed.__getitem__), *unlinked]
        pending.extend((graph.nodes[node_id].path, node_id) for node_id in reversed(reading))
    return ordered


def _containment_chain(graph: Graph, node_id: str) -> list[str]:
    """*node_id* and its containers, innermost first."""
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = node_id
    while current is not None and current in graph.nodes and current not in seen:
        chain.append(current)
        seen.add(current)
        current = graph.nodes[current].parent
    return chain


def _sibling_precedence(graph: Graph) -> dict[str, set[str]]:
    """For each node, the siblings whose subtrees must come after its own:
    sibling B follows sibling A when something in B's subtree depends on
    something in A's. Each dependency orders the two subtrees that hold its
    ends just below their nearest common container."""
    after: dict[str, set[str]] = {}
    for node in graph.nodes.values():
        dependent = _containment_chain(graph, node.id)
        for dependency_id in node.dependencies:
            if dependency_id not in graph.nodes:
                continue
            prerequisite = _containment_chain(graph, dependency_id)
            shared = set(prerequisite).intersection(dependent)
            below_dependent = next((i for i, item in enumerate(dependent) if item in shared), len(dependent))
            below_prerequisite = next((i for i, item in enumerate(prerequisite) if item in shared), len(prerequisite))
            if below_dependent and below_prerequisite:  # neither end contains the other
                after.setdefault(prerequisite[below_prerequisite - 1], set()).add(dependent[below_dependent - 1])
    return after


def _dependency_sorted(siblings: list[str], precedence: Mapping[str, set[str]]) -> list[str]:
    """*siblings* with each subtree after the subtrees it depends on, ties
    broken by their order in *siblings*. When only cycles are left, the
    earliest sibling on one of them goes next."""
    rank = {node_id: index for index, node_id in enumerate(siblings)}
    later = {node_id: sorted(rank[other] for other in precedence.get(node_id, ()) if other in rank) for node_id in siblings}
    waiting = [0] * len(siblings)
    for successors in later.values():
        for index in successors:
            waiting[index] += 1
    ready = [index for index, count in enumerate(waiting) if not count]
    heapq.heapify(ready)
    done: set[int] = set()

    def on_cycle(start: int) -> bool:
        stack, seen = list(later[siblings[start]]), set()
        while stack:
            index = stack.pop()
            if index == start:
                return True
            if index not in done and index not in seen:
                seen.add(index)
                stack.extend(later[siblings[index]])
        return False

    ordered: list[str] = []
    while len(ordered) < len(siblings):
        if not ready:  # every sibling left waits on one, so some sit on a cycle
            heapq.heappush(ready, next(i for i in range(len(siblings)) if i not in done and on_cycle(i)))
        index = heapq.heappop(ready)
        if index in done:
            continue
        done.add(index)
        ordered.append(siblings[index])
        for successor in later[siblings[index]]:
            waiting[successor] -= 1
            if not waiting[successor] and successor not in done:
                heapq.heappush(ready, successor)
    return ordered


def _child_containing(graph: Graph, parent: str | None, node_id: str) -> str | None:
    """The child of *parent* whose containment subtree holds *node_id*, if any."""
    seen: set[str] = set()
    while node_id in graph.nodes and node_id not in seen:
        seen.add(node_id)
        up = graph.nodes[node_id].parent
        if up == parent:
            return node_id
        if up is None:
            return None
        node_id = up
    return None


def _reading_order(
    graph: Graph,
    targets: Mapping[str, tuple[Path, str]],
    pages: list[Path],
    positions: Mapping[str, int],
) -> list[str]:
    """Every node in the order the book reads it: by its page's place in
    ``pages``, then by its card's place on that page (``positions``; a page's
    own article comes first), then in dependency order. Nodes on a page not
    in ``pages`` follow, one page at a time."""
    page_rank = {page: rank for rank, page in enumerate(pages)}

    def key(node_id: str) -> tuple[int, str, int]:
        page = _resolve(targets[node_id][0]) if node_id in targets else None
        return page_rank.get(page, len(page_rank)), str(page or ""), positions.get(node_id, -1)

    return sorted(status.topological_order(graph), key=key)
