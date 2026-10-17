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
from .status import INVENTORY_STATES, STATES, NodeStatus, State, topological_order


ViewKind = Literal["project", "chapter", "full"]
NodeKind = Literal["scope", "boundary", "node"]

# source, target, proof-only, then each endpoint's direct child of the viewed
# scope, or ``None`` when that endpoint lies outside it.
_ScopedRelation = tuple[str, str, bool, str | None, str | None]

_H1 = re.compile(r"^ {0,3}#[ \t]+(.+?)[ \t]*#*[ \t]*$")
INVENTORY_STATUSES = tuple(state.key for state in INVENTORY_STATES)
#: What the project's views call the root's own articles, since the root's title names the whole project.
ROOT_GROUP_TITLE = "Top level"


@dataclass(frozen=True, slots=True)
class ViewNode:
    """A node as presented in one graph projection."""

    id: str
    title: str
    kind: NodeKind
    members: tuple[str, ...]
    status_counts: tuple[tuple[str, int], ...]
    declaration: str | None = None
    status_key: str | None = None
    complete: bool = False
    catalog: str | None = None
    summary: str | None = None
    lean: str | None = None
    area: str | None = None
    internal_dependency_count: int = 0
    navigation: str | None = None
    examples: tuple[str, ...] = ()

    @property
    def item_count(self) -> int:
        return sum(count for _, count in self.status_counts)


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
    summary: str | None = None
    overview: bool = False
    total_count: int | None = None

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
    edges, internal_counts = _project_edges(graph, children)
    required_scopes = {endpoint.removeprefix("scope:") for edge in edges for endpoint in (edge.source, edge.target)}
    scopes = [*grouped, *(scope for scope in sorted(required_scopes) if scope not in grouped)]
    nodes = tuple(
        _scope_node(
            graph,
            statuses,
            _scope_node_id(group),
            ROOT_GROUP_TITLE if group in {"", "roadmap"} else group_title(graph, group),
            grouped.get(group, ()),
            summary=graph.nodes[group].summary if group in graph.nodes else None,
            area=graph.nodes[group].area if group in graph.nodes else None,
            internal_dependency_count=internal_counts[group],
        )
        for group in scopes
    )
    return GraphView(
        kind="project",
        title="Project dependency map",
        nodes=nodes,
        edges=edges,
        summary=graph.nodes["roadmap"].summary if "roadmap" in graph.nodes else None,
    )


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

    def endpoint(node_id: str) -> str:
        if node_id in inside:
            return node_id
        external = _top_scope(graph, node_id, children)
        boundaries[external].add(node_id)
        return _boundary_node_id(external)

    for source, target, proof_only in _relations(graph):
        if source in inside or target in inside:
            edge_counts[(endpoint(source), endpoint(target))][1 if proof_only else 0] += 1

    nodes = [_theorem_node(graph.nodes[node_id], statuses[node_id]) for node_id in grouped[group]]
    nodes.extend(
        ViewNode(
            id=_boundary_node_id(external),
            title=group_title(graph, external),
            kind="boundary",
            members=tuple(sorted(external_members)),
            status_counts=_status_counts(graph, external_members, statuses),
            status_key=_rollup_status_key(graph, external_members, statuses),
        )
        for external, external_members in sorted(boundaries.items())
    )
    return GraphView(
        kind="chapter",
        title=f"{group_title(graph, group)} dependency map",
        nodes=tuple(nodes),
        edges=_edges(edge_counts),
        scope=group,
        summary=graph.nodes[group].summary if group in graph.nodes else None,
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
    internal_counts = _internal_dependency_counts(graph, children)
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
        internal_counts=internal_counts,
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
    internal_counts = _internal_dependency_counts(graph, children)
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
            internal_counts=internal_counts,
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
    internal_counts: dict[str, int],
    relations: Iterable[_ScopedRelation],
    top_scope: Callable[[str], str],
    include_external: bool,
) -> GraphView:
    # A library or pagination range is organization, not a mathematical
    # concept. Authored roles let every producer expose topics without a
    # renderer guessing from directory names. Source books remain available
    # as provenance, separate from the mathematical overview.
    direct, collections, owners = _navigation_children(graph, scope, children)
    overview = any(graph.nodes[child].navigation in {"collection", "source", "transparent"} for child in children[scope])
    entries = (*direct, *collections)
    members = {child: _leaf_descendants(children, child) for child in entries}
    nodes: list[ViewNode] = []
    for child in entries:
        article = graph.nodes[child]
        if child in children:
            nodes.append(
                _scope_node(
                    graph,
                    statuses,
                    _scope_node_id(child),
                    article.title,
                    members[child],
                    summary=article.summary,
                    area=article.area,
                    internal_dependency_count=internal_counts.get(child, 0),
                    navigation=article.navigation,
                )
            )
        else:
            nodes.append(_theorem_node(article, statuses[child]))

    edge_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    boundaries: dict[str, set[str]] = defaultdict(set)

    def endpoint(node_id: str, child: str | None) -> str:
        if child is None:
            external = top_scope(node_id)
            boundaries[external].add(node_id)
            return _boundary_node_id(external)
        return _scope_node_id(child) if child in children else child

    for source, target, proof_only, source_child, target_child in relations:
        source_child = owners.get(source) if source_child is not None else None
        target_child = owners.get(target) if target_child is not None else None
        if source_child == target_child:  # both outside the scope, or both under one child
            continue
        if (source_child is None or target_child is None) and not include_external:
            continue
        edge_counts[(endpoint(source, source_child), endpoint(target, target_child))][1 if proof_only else 0] += 1

    for external, external_members in sorted(boundaries.items()):
        nodes.append(
            ViewNode(
                id=_boundary_node_id(external),
                title=group_title(graph, external),
                kind="boundary",
                members=tuple(sorted(external_members)),
                status_counts=_status_counts(graph, external_members, statuses),
                status_key=_rollup_status_key(graph, external_members, statuses),
            )
        )
    return GraphView(
        kind="chapter",
        title=f"{graph.nodes[scope].title} dependency map",
        nodes=tuple(nodes),
        edges=_edges(edge_counts),
        scope=scope,
        summary=graph.nodes[scope].summary,
        overview=overview,
        total_count=sum(count for _, count in _status_counts(graph, _leaf_descendants(children, scope), statuses)),
    )


def _navigation_children(
    graph: Graph, scope: str, children: dict[str, tuple[str, ...]]
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]:
    """Flatten only explicitly transparent collections, never mathematical scopes.

    Every original endpoint keeps an owner. Edges mentioning a collection
    itself remain attached to that collection, rather than being attributed
    to a mathematical child without evidence.
    """
    visible: list[str] = []
    collections: list[str] = []
    owners: dict[str, str] = {}

    def own_subtree(node_id: str, owner: str) -> None:
        owners[node_id] = owner
        for child in children.get(node_id, ()):
            own_subtree(child, owner)

    def visit(node_id: str, collection: str | None = None) -> None:
        if graph.nodes[node_id].navigation in {"collection", "transparent"} and node_id in children:
            owner = collection or node_id
            if collection is None:
                collections.append(node_id)
            owners[node_id] = owner
            for child in children[node_id]:
                visit(child, owner)
        else:
            visible.append(node_id)
            own_subtree(node_id, node_id)

    for child in children[scope]:
        visit(child)
    return tuple(visible), tuple(collections), owners


def full_view(graph: Graph, statuses: dict[str, NodeStatus]) -> GraphView:
    """Present every article and dependency, with container status rolled up."""
    view = _node_view(graph, statuses, graph.nodes, containers=container_nodes(graph, statuses))
    return GraphView(
        kind="full",
        title="Full dependency graph",
        nodes=view.nodes,
        edges=view.edges,
        summary=graph.nodes["roadmap"].summary if "roadmap" in graph.nodes else None,
    )


def _node_view(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    selected: Iterable[str],
    *,
    containers: dict[str, ViewNode],
) -> GraphView:
    order_index = {node_id: index for index, node_id in enumerate(topological_order(graph))}
    ordered_ids = sorted(selected, key=order_index.__getitem__)
    selected_ids = frozenset(ordered_ids)
    # A container is a chapter or section page, not a result: drawing it as a
    # theorem gave it the "ready to state" status of an empty statement.
    nodes = tuple(
        containers[node_id] if node_id in containers else _theorem_node(graph.nodes[node_id], statuses[node_id])
        for node_id in ordered_ids
    )
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


def _project_edges(
    graph: Graph,
    children: dict[str, tuple[str, ...]],
) -> tuple[tuple[ViewEdge, ...], dict[str, int]]:
    """Return the relations between top-level scopes and the count inside each one.

    The root scope holds only the articles directly under roadmap/, so the
    relations inside a chapter are that chapter's, never the root's too.
    """
    edge_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    internal_counts: dict[str, int] = defaultdict(int)
    for source, target, proof_only in _relations(graph):
        source_group = _top_scope(graph, source, children)
        target_group = _top_scope(graph, target, children)
        if source_group == target_group:
            internal_counts[source_group] += 1
            continue
        key = (_scope_node_id(source_group), _scope_node_id(target_group))
        edge_counts[key][1 if proof_only else 0] += 1
    return _edges(edge_counts), internal_counts


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
    return ViewNode(
        id=node.id,
        title=node.title,
        kind="node",
        members=(node.id,),
        status_counts=((display_state(node, node_status).key, 1),),
        declaration=node.declaration,
        catalog=node.catalog,
        status_key=display_status_key(node, node_status),
        summary=node.summary,
        lean=node.lean,
        area=node.area,
    )


def is_scope_target(node: Node) -> bool:
    """Whether a scope counts *node* as formalization work.

    Only leaves that declare something or are marked not ready count, so a
    note cannot hold a finished chapter open but deferred work does. A module
    catalog is inventory evidence, never a target.
    """
    return node.catalog is None and (node.formalizable or node.not_ready)


def _inventory_state(node: Node, node_status: NodeStatus) -> State | None:
    """Return a module catalog's neutral inventory state, or None for any other node.

    A catalog is checked once it is fully proved and pending until then. Every
    view names a catalog by this state rather than by its derived one, which
    would present an inventory as mathematical work.
    """
    if node.catalog != "module":
        return None
    checked, pending = INVENTORY_STATES
    return checked if node_status.fully_proved else pending


def display_status_key(node: Node, node_status: NodeStatus) -> str:
    """Return the state a node is coloured in: its own, or planned for a catalog.

    An inventory is deliberately neutral, checked or not: it records source
    accounting, not progress on a mathematical target.
    """
    return "planned" if _inventory_state(node, node_status) else node_status.key


def display_state(node: Node, node_status: NodeStatus) -> State:
    """Return the state a node is named by: a catalog's inventory state, otherwise its own."""
    return _inventory_state(node, node_status) or node_status.state


def kind_label(node: Node) -> str:
    """Name what a node is: a module inventory, or what it declares, or its kind."""
    return "module inventory" if node.catalog == "module" else node.declaration or node.kind


def scope_target_statuses(graph: Graph, statuses: dict[str, NodeStatus]) -> dict[str, NodeStatus]:
    """Select the leaves a whole-graph map's scope boxes count by state, for its legend.

    Containers are drawn as scopes, so they have no row of their own, and a
    catalog counts apart as an inventory.
    """
    containers = _containers(graph)
    return {
        node_id: statuses[node_id]
        for node_id, node in graph.nodes.items()
        if node_id not in containers and is_scope_target(node)
    }


def _containers(graph: Graph) -> frozenset[str]:
    """Index which articles contain others, so loops need not rescan the graph."""
    return frozenset(node.parent for node in graph.nodes.values() if node.parent is not None)


def _is_countable(graph: Graph, node_id: str, containers: frozenset[str]) -> bool:
    """Whether *node_id* is a formalization target the dashboards should count.

    A leaf, and a leaf that declares something. Counting every leaf made a
    freshly scaffolded vault report "0 of 1 targets complete, 1 ready now": the
    roadmap landing page has no children yet, so it counted as an unstarted
    result, and the site claimed work existed before any had been planned. A
    module catalog declares nothing, so it never counts.
    """

    return node_id not in containers and graph.nodes[node_id].formalizable


def _countable(graph: Graph) -> list[str]:
    containers = _containers(graph)
    return [node_id for node_id in graph.nodes if _is_countable(graph, node_id, containers)]


def _module_inventories(
    graph: Graph,
    *,
    containers: frozenset[str] | None = None,
    node_ids: Iterable[str] | None = None,
) -> list[str]:
    """Return inventory leaves separately from mathematical targets."""
    containers = _containers(graph) if containers is None else containers
    candidates = graph.nodes if node_ids is None else node_ids
    return [
        node_id
        for node_id in candidates
        if node_id not in containers and graph.nodes[node_id].catalog == "module"
    ]


def _scope_node(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    view_id: str,
    title: str,
    leaves: Iterable[str],
    *,
    members: tuple[str, ...] | None = None,
    summary: str | None = None,
    area: str | None = None,
    internal_dependency_count: int = 0,
    navigation: str | None = None,
) -> ViewNode:
    """Summarize a container by the formalization targets beneath it.

    The targets are the scope's members. Its status counts, and so its items,
    are those targets by state plus every catalog beneath it, which counts
    apart as a module inventory. In a node-level view the box stands for
    the container itself, so *members* is the container, while its counts
    still describe what lies beneath it.
    """
    leaves = tuple(leaves)
    targets = tuple(node_id for node_id in leaves if is_scope_target(graph.nodes[node_id]))
    return ViewNode(
        id=view_id,
        title=title,
        kind="scope",
        members=targets if members is None else members,
        status_counts=_status_counts(graph, leaves, statuses),
        status_key=_rollup_status_key(graph, leaves, statuses),
        complete=bool(targets) and all(statuses[node_id].fully_proved for node_id in targets),
        summary=summary,
        area=area,
        internal_dependency_count=internal_dependency_count,
        navigation=navigation,
        examples=tuple(graph.nodes[node_id].title for node_id in targets[:3]),
    )


def container_nodes(graph: Graph, statuses: dict[str, NodeStatus]) -> dict[str, ViewNode]:
    """Every container as it appears in a node-level view."""
    children = _containment_children(graph)
    internal_counts = _internal_dependency_counts(graph, children)
    return {
        node_id: _scope_node(
            graph,
            statuses,
            node_id,
            graph.nodes[node_id].title,
            _leaf_descendants(children, node_id),
            members=(node_id,),
            summary=graph.nodes[node_id].summary,
            area=graph.nodes[node_id].area,
            internal_dependency_count=internal_counts.get(node_id, 0),
            navigation=graph.nodes[node_id].navigation,
        )
        for node_id in children
        if node_id in graph.nodes
    }


def _status_counts(
    graph: Graph,
    node_ids: Iterable[str],
    statuses: dict[str, NodeStatus],
) -> tuple[tuple[str, int], ...]:
    """Count scope targets by state, and module inventories apart.

    Only a scope target colours a ring by state, so a prose or note leaf
    colours none. A catalog counts as a checked or pending inventory, never
    under its target state.
    """
    counts = {state.key: 0 for state in STATES}
    inventories = dict.fromkeys(INVENTORY_STATUSES, 0)
    for node_id in node_ids:
        node = graph.nodes[node_id]
        node_status = statuses[node_id]
        inventory = _inventory_state(node, node_status)
        if inventory is not None:
            inventories[inventory.key] += 1
        elif is_scope_target(node):
            counts[node_status.key] += 1
    state_counts = tuple(
        (state.key, counts[state.key]) for state in STATES if counts[state.key]
    )
    return (*state_counts, *((key, count) for key, count in inventories.items() if count))


def inventory_counts(graph: Graph, statuses: dict[str, NodeStatus]) -> dict[str, int]:
    """Count every catalog leaf by inventory status.

    These are the inventories the project view's rings count, for a legend
    beside a map of the whole project.
    """
    children = _containment_children(graph)
    leaves = (node_id for node_id in graph.nodes if node_id not in children)
    return {
        key: count
        for key, count in _status_counts(graph, leaves, statuses)
        if key in INVENTORY_STATUSES
    }


def _internal_dependency_counts(
    graph: Graph,
    children: dict[str, tuple[str, ...]],
) -> dict[str, int]:
    """Count typed relations inside every container in one graph-wide pass."""
    counts = {scope: 0 for scope in children}
    enclosing = {node_id: set(_enclosing_scopes(graph, node_id)) for node_id in graph.nodes}
    for node_id in children:
        enclosing[node_id].add(node_id)
    for source, target, _proof_only in _relations(graph):
        for scope in enclosing[source].intersection(enclosing[target]):
            counts[scope] += 1
    return counts


def _rollup_status_key(
    graph: Graph,
    node_ids: Iterable[str],
    statuses: dict[str, NodeStatus],
) -> str:
    """Use the least-complete descendant as a container's honest status."""
    counts = _status_counts(graph, node_ids, statuses)
    target_counts = tuple(
        (key, count) for key, count in counts if key not in INVENTORY_STATUSES
    )
    return target_counts[-1][0] if target_counts else "planned"


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


__all__ = [
    "GraphView",
    "ViewEdge",
    "ViewNode",
    "chapter_view",
    "container_nodes",
    "display_state",
    "display_status_key",
    "full_view",
    "group_id",
    "group_nodes",
    "group_title",
    "inventory_counts",
    "is_scope_target",
    "kind_label",
    "project_view",
    "scope_target_statuses",
    "scope_view",
    "scope_views",
]
