"""Project theorem DAGs at the scales used by the published blueprint.

The Markdown graph remains the source of truth.  These views only select or
aggregate its nodes and edges so the same data can answer project-, chapter-,
and theorem-level questions without inventing a second graph format.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Literal

from .graph import Graph, Node
from .status import STATES, NodeStatus, topological_order


ViewKind = Literal["project", "chapter", "focus", "full"]
NodeKind = Literal["scope", "boundary", "node"]

# source, target, proof-only, then each endpoint's direct child of the viewed
# scope, or ``None`` when that endpoint lies outside it.
_ScopedRelation = tuple[str, str, bool, str | None, str | None]

_H1 = re.compile(r"^ {0,3}#[ \t]+(.+?)[ \t]*#*[ \t]*$")
INVENTORY_CHECKED_STATUS = "inventory_checked"


@dataclass(frozen=True, slots=True)
class ViewNode:
    """A node as presented in one graph projection."""

    id: str
    title: str
    kind: NodeKind
    members: tuple[str, ...]
    status_counts: tuple[tuple[str, int], ...]
    declaration: str | None = None
    catalog: str | None = None
    status_key: str | None = None
    focus: bool = False
    catalog: str | None = None

    @property
    def item_count(self) -> int:
        return len(self.members)


@dataclass(frozen=True, slots=True)
class ViewEdge:
    """One projected relation, retaining statement/proof edge semantics."""

    source: str
    target: str
    statement_count: int = 0
    proof_count: int = 0

    @property
    def dependency_count(self) -> int:
        return self.statement_count + self.proof_count


@dataclass(frozen=True, slots=True)
class GraphView:
    """A deterministic, renderer-neutral projection of a blueprint graph."""

    kind: ViewKind
    title: str
    nodes: tuple[ViewNode, ...]
    edges: tuple[ViewEdge, ...]
    scope: str | None = None
    focus: str | None = None
    radius: int | None = None

    @property
    def member_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(member for node in self.nodes for member in node.members))


def group_id(node_id: str) -> str:
    """Return the publication chapter containing *node_id*.

    The renderer publishes the first roadmap path segment as one textbook
    chapter.  Nodes directly under ``roadmap/`` share its root chapter.
    """
    head, separator, _ = node_id.partition("/")
    return head if separator else ""


def group_nodes(graph: Graph) -> dict[str, tuple[str, ...]]:
    """Group articles under their top-level roadmap container."""
    return _group_nodes(graph, _containment_children(graph))


def _group_nodes(graph: Graph, children: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for node_id in topological_order(graph):
        if node_id == "roadmap":
            continue
        if node_id in children:
            continue
        scope = _top_scope(graph, node_id, children)
        grouped.setdefault(scope, [])
        grouped[scope].append(node_id)
    return {group: tuple(node_ids) for group, node_ids in grouped.items()}


def group_title(graph: Graph, group: str) -> str:
    """Use a roadmap page's H1 for a chapter, falling back to its path."""
    if group in graph.nodes:
        return graph.nodes[group].title
    page = graph.blueprint_dir / "roadmap" / group / "README.md" if group else graph.blueprint_dir / "roadmap/README.md"
    if page.is_file():
        for line in page.read_text(encoding="utf-8").splitlines():
            match = _H1.match(line)
            if match is not None:
                return match.group(1).strip()
    return group.replace("-", " ").replace("_", " ").title() if group else "Roadmap"


def project_view(graph: Graph, statuses: dict[str, NodeStatus]) -> GraphView:
    """Collapse every publication chapter to one project-map node."""
    children = _containment_children(graph)
    grouped = _group_nodes(graph, children)
    edges = _project_edges(graph, children)
    required_scopes = {endpoint.removeprefix("scope:") for edge in edges for endpoint in (edge.source, edge.target)}
    scopes = [*grouped, *(scope for scope in sorted(required_scopes) if scope not in grouped)]
    nodes = tuple(
        ViewNode(
            id=_scope_node_id(group),
            title=group_title(graph, group),
            kind="scope",
            members=grouped.get(group, ()),
            status_counts=_status_counts(grouped.get(group, ()), statuses),
            status_key=_rollup_status_key(grouped.get(group, ()), statuses),
        )
        for group in scopes
    )
    return GraphView(kind="project", title="Project dependency map", nodes=nodes, edges=edges)


def chapter_view(graph: Graph, statuses: dict[str, NodeStatus], group: str) -> GraphView:
    """Show one chapter's nodes and collapse every external chapter to a boundary."""
    children = _containment_children(graph)
    if group in graph.nodes and group in children:
        return scope_view(graph, statuses, group)
    grouped = _group_nodes(graph, children)
    group = group or "roadmap"
    if group not in grouped:
        raise KeyError(f"unknown blueprint chapter: {group}")

    inside = frozenset(grouped[group])
    boundaries: dict[str, set[str]] = defaultdict(set)
    edge_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for source, target, proof_only in _relations(graph):
        source_inside = source in inside
        target_inside = target in inside
        if not source_inside and not target_inside:
            continue
        if source_inside and target_inside:
            projected_source, projected_target = source, target
        elif target_inside:
            external = _top_scope(graph, source, children)
            boundaries[external].add(source)
            projected_source, projected_target = _boundary_node_id(external), target
        else:
            external = _top_scope(graph, target, children)
            boundaries[external].add(target)
            projected_source, projected_target = source, _boundary_node_id(external)
        edge_counts[(projected_source, projected_target)][1 if proof_only else 0] += 1

    nodes = [_theorem_node(graph.nodes[node_id], statuses[node_id]) for node_id in grouped[group]]
    nodes.extend(
        ViewNode(
            id=_boundary_node_id(external),
            title=group_title(graph, external),
            kind="boundary",
            members=tuple(sorted(external_members)),
            status_counts=_status_counts(external_members, statuses),
            status_key=_rollup_status_key(external_members, statuses),
        )
        for external, external_members in sorted(boundaries.items())
    )
    return GraphView(
        kind="chapter",
        title=f"{group_title(graph, group)} dependency map",
        nodes=tuple(nodes),
        edges=_edges(edge_counts),
        scope=group,
    )


def scope_view(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    scope: str,
    *,
    include_external: bool = True,
) -> GraphView:
    """Show one container's direct children with deeper subtrees collapsed.

    This is the WikiLean-style zoom unit: a container is clickable, its direct
    children are visible, and authored dependencies are rolled up through the
    containment hierarchy without creating another graph representation.
    """
    children = _containment_children(graph)
    if scope not in graph.nodes or scope not in children:
        raise KeyError(f"unknown blueprint scope: {scope}")
    relations = (
        (source, target, proof_only, _direct_child(graph, scope, source), _direct_child(graph, scope, target))
        for source, target, proof_only in _relations(graph)
    )
    return _scope_view(
        graph,
        statuses,
        scope,
        children=children,
        relations=relations,
        top_scope=lambda node_id: _top_scope(graph, node_id, children),
        include_external=include_external,
    )


def scope_views(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    *,
    include_external: bool = True,
) -> dict[str, GraphView]:
    """Build every container's view while sharing the graph-wide indexes.

    Static publication writes one page per container. Rebuilding the
    containment index and rescanning every relation for each page becomes
    quadratic on a large book, so the bulk path indexes containment once and
    files each relation under only the containers that enclose an endpoint.
    """
    children = _containment_children(graph)
    enclosing: dict[str, dict[str, str]] = {}
    top_scopes: dict[str, str] = {}

    def enclosing_scopes(node_id: str) -> dict[str, str]:
        found = enclosing.get(node_id)
        if found is None:
            found = enclosing[node_id] = _enclosing_scopes(graph, node_id)
        return found

    def top_scope(node_id: str) -> str:
        found = top_scopes.get(node_id)
        if found is None:
            found = top_scopes[node_id] = _top_scope(graph, node_id, children)
        return found

    relations: dict[str, list[_ScopedRelation]] = defaultdict(list)
    for source, target, proof_only in _relations(graph):
        source_scopes = enclosing_scopes(source)
        target_scopes = enclosing_scopes(target)
        for scope, source_child in source_scopes.items():
            relations[scope].append((source, target, proof_only, source_child, target_scopes.get(scope)))
        for scope, target_child in target_scopes.items():
            if scope not in source_scopes:
                relations[scope].append((source, target, proof_only, None, target_child))

    return {
        scope: _scope_view(
            graph,
            statuses,
            scope,
            children=children,
            relations=relations.get(scope, ()),
            top_scope=top_scope,
            include_external=include_external,
        )
        for scope in graph.nodes
        if scope in children
    }


def _scope_view(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    scope: str,
    *,
    children: dict[str, tuple[str, ...]],
    relations: Iterable[_ScopedRelation],
    top_scope: Callable[[str], str],
    include_external: bool,
) -> GraphView:
    direct = children[scope]
    members = {child: _leaf_descendants(children, child) for child in direct}
    nodes: list[ViewNode] = []
    for child in direct:
        article = graph.nodes[child]
        if child in children:
            nodes.append(
                ViewNode(
                    id=_scope_node_id(child),
                    title=article.title,
                    kind="scope",
                    members=members[child],
                    status_counts=_status_counts(members[child], statuses),
                    status_key=_rollup_status_key(members[child], statuses),
                )
            )
        else:
            nodes.append(_theorem_node(article, statuses[child]))

    edge_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    boundaries: dict[str, set[str]] = defaultdict(set)
    for source, target, proof_only, source_child, target_child in relations:
        if source_child is None and target_child is None:
            continue
        if source_child is not None and target_child is not None:
            if source_child == target_child:
                continue
            projected_source = _scope_node_id(source_child) if source_child in children else source_child
            projected_target = _scope_node_id(target_child) if target_child in children else target_child
        elif not include_external:
            continue
        elif target_child is not None:
            external = top_scope(source)
            boundaries[external].add(source)
            projected_source = _boundary_node_id(external)
            projected_target = _scope_node_id(target_child) if target_child in children else target_child
        else:
            external = top_scope(target)
            boundaries[external].add(target)
            projected_source = _scope_node_id(source_child) if source_child in children else source_child
            projected_target = _boundary_node_id(external)
        edge_counts[(projected_source, projected_target)][1 if proof_only else 0] += 1

    for external, external_members in sorted(boundaries.items()):
        nodes.append(
            ViewNode(
                id=_boundary_node_id(external),
                title=group_title(graph, external),
                kind="boundary",
                members=tuple(sorted(external_members)),
                status_counts=_status_counts(external_members, statuses),
                status_key=_rollup_status_key(external_members, statuses),
            )
        )
    return GraphView(
        kind="chapter",
        title=f"{graph.nodes[scope].title} dependency map",
        nodes=tuple(nodes),
        edges=_edges(edge_counts),
        scope=scope,
    )


def focus_view(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    node_id: str,
    *,
    radius: int = 1,
) -> GraphView:
    """Show *node_id* and the undirected dependency neighborhood around it."""
    if node_id not in graph.nodes:
        raise KeyError(f"unknown blueprint node: {node_id}")
    if radius < 0:
        raise ValueError("focus radius must be non-negative")
    return _focus_view(
        graph,
        statuses,
        node_id,
        radius=radius,
        adjacency=_adjacency(graph),
        order_index={candidate: index for index, candidate in enumerate(topological_order(graph))},
    )


def focus_views(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    *,
    radius: int = 1,
) -> dict[str, GraphView]:
    """Build every local view while sharing the graph-wide indexes.

    Programmatic consumers that need all local views can avoid recomputing
    adjacency and topological order for every node. Static publication uses the
    shared hash-addressable explorer instead of emitting one HTML page per node.
    """
    if radius < 0:
        raise ValueError("focus radius must be non-negative")
    adjacency = _adjacency(graph)
    order_index = {candidate: index for index, candidate in enumerate(topological_order(graph))}
    return {
        node_id: _focus_view(
            graph,
            statuses,
            node_id,
            radius=radius,
            adjacency=adjacency,
            order_index=order_index,
        )
        for node_id in graph.nodes
    }


def _focus_view(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    node_id: str,
    *,
    radius: int,
    adjacency: dict[str, set[str]],
    order_index: dict[str, int],
) -> GraphView:
    if node_id not in graph.nodes:
        raise KeyError(f"unknown blueprint node: {node_id}")
    if radius < 0:
        raise ValueError("focus radius must be non-negative")

    selected = {node_id}
    frontier = {node_id}
    for _ in range(radius):
        frontier = set().union(*(adjacency[candidate] for candidate in frontier)) - selected
        selected.update(frontier)
        if not frontier:
            break

    view = _node_view(
        graph,
        statuses,
        sorted(selected, key=order_index.__getitem__),
        ordered=True,
    )
    nodes = tuple(
        ViewNode(
            id=node.id,
            title=node.title,
            kind=node.kind,
            members=node.members,
            status_counts=node.status_counts,
            declaration=node.declaration,
            catalog=node.catalog,
            status_key=node.status_key,
            focus=node.id == node_id,
        )
        for node in view.nodes
    )
    return GraphView(
        kind="focus",
        title=f"{graph.nodes[node_id].title}: local context",
        nodes=nodes,
        edges=view.edges,
        focus=node_id,
        radius=radius,
    )


def full_view(graph: Graph, statuses: dict[str, NodeStatus]) -> GraphView:
    """Present every article and dependency, with container status rolled up."""
    view = _node_view(graph, statuses, graph.nodes)
    children = _containment_children(graph)
    descendants = _leaf_descendant_map(graph, children)
    nodes = tuple(
        ViewNode(
            id=node.id,
            title=node.title,
            kind="scope",
            members=(node.id, *descendants[node.id]),
            status_counts=_status_counts(descendants[node.id], statuses),
            status_key=_rollup_status_key(descendants[node.id], statuses),
        )
        if node.id in children
        else node
        for node in view.nodes
    )
    return GraphView(kind="full", title="Full dependency graph", nodes=nodes, edges=view.edges)


def _node_view(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    selected: Iterable[str],
    *,
    ordered: bool = False,
) -> GraphView:
    ordered_ids = list(dict.fromkeys(node_id for node_id in selected if node_id in graph.nodes))
    selected_ids = frozenset(ordered_ids)
    if not ordered:
        order_index = {node_id: index for index, node_id in enumerate(topological_order(graph))}
        ordered_ids.sort(key=order_index.__getitem__)
    nodes = tuple(_theorem_node(graph.nodes[node_id], statuses[node_id]) for node_id in ordered_ids)
    edge_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for target in ordered_ids:
        node = graph.nodes[target]
        statement = set(node.statement_dependencies)
        for source in node.statement_dependencies:
            if source in selected_ids:
                edge_counts[(source, target)][0] += 1
        for source in node.proof_dependencies:
            if source in selected_ids and source not in statement:
                edge_counts[(source, target)][1] += 1
    return GraphView(kind="full", title="", nodes=nodes, edges=_edges(edge_counts))


def _adjacency(graph: Graph) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {candidate: set() for candidate in graph.nodes}
    for source, target, _ in _relations(graph):
        adjacency[source].add(target)
        adjacency[target].add(source)
    return adjacency


def _project_edges(graph: Graph, children: dict[str, tuple[str, ...]]) -> tuple[ViewEdge, ...]:
    edge_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for source, target, proof_only in _relations(graph):
        source_group = _top_scope(graph, source, children)
        target_group = _top_scope(graph, target, children)
        if source_group == target_group:
            continue
        key = (_scope_node_id(source_group), _scope_node_id(target_group))
        edge_counts[key][1 if proof_only else 0] += 1
    return _edges(edge_counts)


def _relations(graph: Graph):
    """Yield source, target, proof-only for every fine graph relation."""
    for target in graph.nodes.values():
        statement = set(target.statement_dependencies)
        for source in target.statement_dependencies:
            yield source, target.id, False
        for source in target.proof_dependencies:
            if source not in statement:
                yield source, target.id, True


def _edges(counts: dict[tuple[str, str], list[int]]) -> tuple[ViewEdge, ...]:
    return tuple(
        ViewEdge(source, target, statement_count=values[0], proof_count=values[1])
        for (source, target), values in sorted(counts.items())
    )


def _theorem_node(node: Node, node_status: NodeStatus) -> ViewNode:
    display_status = (
        INVENTORY_CHECKED_STATUS
        if node.catalog == "module" and node_status.fully_proved
        else node_status.key
    )
    return ViewNode(
        id=node.id,
        title=node.title,
        kind="node",
        members=(node.id,),
        status_counts=((display_status, 1),),
        declaration=node.declaration,
        catalog=node.catalog,
        status_key=node_status.key,
    )


def _status_counts(
    graph: Graph,
    node_ids: Iterable[str],
    statuses: dict[str, NodeStatus],
) -> tuple[tuple[str, int], ...]:
    counts = {state.key: 0 for state in STATES}
    inventories = 0
    for node_id in node_ids:
        node_status = statuses[node_id]
        if graph.nodes[node_id].catalog == "module" and node_status.fully_proved:
            inventories += 1
        else:
            counts[node_status.key] += 1
    state_counts = tuple(
        (state.key, counts[state.key]) for state in STATES if counts[state.key]
    )
    if inventories:
        return (*state_counts, (INVENTORY_CHECKED_STATUS, inventories))
    return state_counts


def _rollup_status_key(
    graph: Graph,
    node_ids: Iterable[str],
    statuses: dict[str, NodeStatus],
) -> str:
    """Use the least-complete descendant as a container's honest status."""
    counts = _status_counts(graph, node_ids, statuses)
    target_counts = tuple(
        (key, count) for key, count in counts if key != INVENTORY_CHECKED_STATUS
    )
    return target_counts[-1][0] if target_counts else "planned"


def _rollup_status_key(
    node_ids: Iterable[str],
    statuses: dict[str, NodeStatus],
) -> str:
    """Use the least-complete descendant as a container's honest status."""
    counts = _status_counts(node_ids, statuses)
    return counts[-1][0] if counts else "planned"


def _scope_node_id(group: str) -> str:
    return f"scope:{group or 'roadmap'}"


def _boundary_node_id(group: str) -> str:
    return f"boundary:{group or 'roadmap'}"


def _top_scope(graph: Graph, node_id: str, children: dict[str, tuple[str, ...]]) -> str:
    """Return the canonical roadmap scope containing *node_id*."""
    current = node_id
    while graph.nodes[current].parent is not None:
        parent = graph.nodes[current].parent
        if parent == "roadmap":
            return current if current in children else "roadmap"
        current = parent
    # Hand-built/legacy in-memory graphs did not carry containment. Preserve
    # their path-based chapter grouping without weakening Markdown inference.
    return group_id(node_id) or "roadmap"


def _direct_child(graph: Graph, scope: str, node_id: str) -> str | None:
    current = node_id
    while current in graph.nodes and graph.nodes[current].parent is not None:
        if graph.nodes[current].parent == scope:
            return current
        current = graph.nodes[current].parent  # type: ignore[assignment]
    return None


def _enclosing_scopes(graph: Graph, node_id: str) -> dict[str, str]:
    """Map every container enclosing *node_id* to its child on the way down."""
    scopes: dict[str, str] = {}
    current = node_id
    while current in graph.nodes:
        parent = graph.nodes[current].parent
        if parent is None:
            break
        scopes[parent] = current
        current = parent
    return scopes


def _leaf_descendants(children: dict[str, tuple[str, ...]], node_id: str) -> tuple[str, ...]:
    leaves: list[str] = []
    pending = [node_id]
    while pending:
        current = pending.pop()
        contained = children.get(current)
        if contained:
            pending.extend(reversed(contained))
        else:
            leaves.append(current)
    return tuple(leaves)


def _containment_children(graph: Graph) -> dict[str, tuple[str, ...]]:
    """Index direct containment once per view, rejecting hand-built cycles.

    ``load_graph`` only produces forests; checking here keeps every parent walk
    in a view finite for graphs built in memory.
    """
    children: dict[str, list[str]] = {}
    for node in graph.nodes.values():
        if node.parent is not None:
            children.setdefault(node.parent, []).append(node.id)
    acyclic: set[str] = set()
    for node_id in graph.nodes:
        trail: set[str] = set()
        current: str | None = node_id
        while current is not None and current in graph.nodes and current not in acyclic:
            if current in trail:
                raise ValueError("article containment is not a forest")
            trail.add(current)
            current = graph.nodes[current].parent
        acyclic |= trail
    return {parent: tuple(node_ids) for parent, node_ids in children.items()}


def _leaf_descendant_map(
    graph: Graph,
    children: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    """Compute every containment rollup once for full-graph rendering."""
    descendants: dict[str, tuple[str, ...]] = {}

    def visit(node_id: str) -> tuple[str, ...]:
        if node_id in descendants:
            return descendants[node_id]
        contained = children.get(node_id, ())
        result = (
            tuple(leaf for child in contained for leaf in visit(child))
            if contained
            else (node_id,)
        )
        descendants[node_id] = result
        return result

    for node_id in graph.nodes:
        visit(node_id)
    return descendants


__all__ = [
    "GraphView",
    "ViewEdge",
    "ViewNode",
    "chapter_view",
    "focus_view",
    "focus_views",
    "full_view",
    "group_id",
    "group_nodes",
    "group_title",
    "project_view",
    "scope_view",
    "scope_views",
]
