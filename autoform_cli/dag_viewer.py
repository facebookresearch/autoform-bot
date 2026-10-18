"""Static payloads and the dependency-free Autoform graph explorer.

The graph is laid out once, deterministically, while rendering the site. The
browser paints topology on Canvas and places ordinary DOM controls over visible
nodes, so a large graph remains responsive without becoming a bitmap-only UI.
"""

from __future__ import annotations

import heapq
import html
import json
import math
from collections import defaultdict
from dataclasses import replace
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit

from .graph_views import INVENTORY_STATUSES, GraphView, ViewNode
from .lean import declaration_names
from .status import INVENTORY_STATES, MEANINGS, STATES

SCHEMA = "autoform-dag-view/v2"
SEARCH_SCHEMA = "autoform-dag-search/v1"
#: Where the site publishes the viewer, relative to its root.
SCRIPT_PATH = "javascripts/blueprint-dag.js"
STYLE_PATH = "stylesheets/blueprint-dag.css"
#: The most nodes the explorer draws as a map; it only browses a larger view, which gets no layout.
MAX_MAP_NODES = 120

_PALETTE_KEYS = frozenset(state.key for state in (*STATES, *INVENTORY_STATES))
#: A catalog lists its keyword-declared public names; its explorer node and its Lean row
#: in the book show this many and a count.
_CATALOG_LEAN_NAMES = 8
_NODE_WIDTH = 196
_NODE_HEIGHT = 56
_COLUMN_GAP = 116
_ROW_GAP = 24


def _positions(view: GraphView) -> dict[str, dict[str, int]]:
    """Return stable coordinates without a browser force layout."""
    if uses_atlas(view):
        return _atlas_positions(view)
    all_node_ids = [node.id for node in view.nodes]
    known = set(all_node_ids)
    if len(known) != len(all_node_ids):
        raise ValueError("dependency view contains duplicate node ids")
    connected: set[str] = set()
    for edge in view.edges:
        if edge.source not in known or edge.target not in known:
            raise ValueError("dependency view edge names an unknown node")
        if edge.source != edge.target:
            connected.update((edge.source, edge.target))

    # Isolates are inventory, not dependency topology. Rank only the connected
    # graph, then park the isolates in a separate stable grid beside it, which
    # the reader can hide.
    node_ids = [node_id for node_id in all_node_ids if node_id in connected]
    order_index = {node_id: index for index, node_id in enumerate(node_ids)}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    incoming: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    indegree = {node_id: 0 for node_id in node_ids}
    for edge in view.edges:
        if edge.source == edge.target:
            continue
        outgoing[edge.source].append(edge.target)
        incoming[edge.target].append(edge.source)
        indegree[edge.target] += 1

    ready = [(order_index[node_id], node_id) for node_id in node_ids if indegree[node_id] == 0]
    heapq.heapify(ready)
    ranks = {node_id: 0 for node_id in node_ids}
    placed: set[str] = set()
    while ready:
        _, node_id = heapq.heappop(ready)
        if node_id in placed:
            continue
        placed.add(node_id)
        for target in sorted(outgoing[node_id], key=order_index.__getitem__):
            ranks[target] = max(ranks[target], ranks[node_id] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, (order_index[target], target))

    # Fine graphs are acyclic, but a collapsed chapter projection can contain
    # a cycle. Place that remainder in stable source order rather than failing
    # or handing layout to a non-deterministic force simulation.
    for node_id in node_ids:
        if node_id in placed:
            continue
        earlier = [ranks[source] for source in incoming[node_id] if order_index[source] < order_index[node_id]]
        ranks[node_id] = max(earlier, default=-1) + 1

    by_rank: dict[int, list[str]] = defaultdict(list)
    for node_id in node_ids:
        by_rank[ranks[node_id]].append(node_id)

    prior_slots: dict[str, int] = {}
    for rank in sorted(by_rank):
        candidates = by_rank[rank]
        if rank:
            candidates.sort(
                key=lambda node_id: (
                    sum(prior_slots[source] for source in incoming[node_id] if source in prior_slots)
                    / max(1, sum(source in prior_slots for source in incoming[node_id])),
                    order_index[node_id],
                )
            )
        for slot, node_id in enumerate(candidates):
            prior_slots[node_id] = slot

    result: dict[str, dict[str, int]] = {}
    for column, rank in enumerate(sorted(by_rank)):
        for row, node_id in enumerate(by_rank[rank]):
            result[node_id] = {
                "column": column,
                "height": _NODE_HEIGHT,
                "row": row,
                "width": _NODE_WIDTH,
                "x": column * (_NODE_WIDTH + _COLUMN_GAP),
                "y": row * (_NODE_HEIGHT + _ROW_GAP),
            }

    isolated = [node_id for node_id in all_node_ids if node_id not in connected]
    isolate_base = max((position["column"] for position in result.values()), default=-2) + 2
    # One tall column of isolates made a project of 30 chapters fit as
    # unreadable specks, so they fill a widescreen grid beside the ranked DAG.
    cell_aspect = (_NODE_HEIGHT + _ROW_GAP) / (_NODE_WIDTH + _COLUMN_GAP)
    isolate_columns = max(1, math.ceil(math.sqrt(len(isolated) * cell_aspect * (16 / 9))))
    for slot, node_id in enumerate(isolated):
        column = isolate_base + slot % isolate_columns
        row = slot // isolate_columns
        result[node_id] = {
            "column": column,
            "height": _NODE_HEIGHT,
            "row": row,
            "width": _NODE_WIDTH,
            "x": column * (_NODE_WIDTH + _COLUMN_GAP),
            "y": row * (_NODE_HEIGHT + _ROW_GAP),
        }
    return result


def uses_atlas(view: GraphView) -> bool:
    """Draw topic bubbles in regions only when every node is a topic.

    A leaf needs a readable card, so a view holding any leaf, such as a
    chapter with module catalogs beside its sections, is a DAG.
    """
    return (
        (view.overview and any(node.kind == "scope" and node.navigation not in {"collection", "source", "transparent"} for node in view.nodes))
        or
        bool(view.nodes)
        and all(node.kind == "scope" for node in view.nodes)
        and (not view.edges or any(node.area for node in view.nodes))
    )


def _atlas_positions(view: GraphView) -> dict[str, dict[str, int]]:
    """Pack readable topic cards inside authored mathematical regions.

    The project view is an information architecture before it is a dependency
    diagram.  A stable card width keeps titles readable, while a small height
    variation still communicates relative scope without turning text into
    labels inside tiny bubbles.
    """
    card_width = 280 if view.overview else 232
    gap = 16
    region_header = 42

    def card_height(node: ViewNode) -> int:
        if view.overview:
            return 172
        if node.kind == "boundary":
            return 88
        count = max(1, node.item_count)
        return max(88, min(116, round(84 + 4 * math.log2(count + 1))))

    by_area: dict[str, list[ViewNode]] = defaultdict(list)
    for node in view.nodes:
        by_area[node.area or ""].append(node)

    blocks: list[tuple[str, dict[str, dict[str, int]], int, int]] = []
    for area, members in sorted(by_area.items(), key=lambda item: (not item[0], item[0].casefold())):
        # The view carries the source's reading order. Size and title are not
        # reasons to reorder a chapter or its mathematical topic sequence.
        heights = {node.id: card_height(node) for node in members}
        columns = min(3, max(1, math.ceil(math.sqrt(len(members)))))
        local: dict[str, dict[str, int]] = {}
        y = region_header if area else 0
        rows = [members[offset : offset + columns] for offset in range(0, len(members), columns)]
        for row, entries in enumerate(rows):
            row_height = max((heights[node.id] for node in entries), default=88)
            for column, node in enumerate(entries):
                height = heights[node.id]
                x = column * (card_width + gap)
                local[node.id] = {
                    "column": column,
                    "height": height,
                    "row": row,
                    "width": card_width,
                    "x": x,
                    "y": y + (row_height - height) // 2,
                }
            y += row_height + gap
        widest = columns * card_width + max(0, columns - 1) * gap
        blocks.append((area, local, widest, max(region_header + 88, y - gap)))

    total_block_area = sum((width + gap * 2) * (height + gap * 2) for _, _, width, height in blocks)
    atlas_width = max(1_500, round(math.sqrt(total_block_area * 3.2)))
    result: dict[str, dict[str, int]] = {}
    block_x = block_y = block_row_height = 0
    for _area, local, width, height in blocks:
        if block_x and block_x + width > atlas_width:
            block_x = 0
            block_y += block_row_height + gap * 2
            block_row_height = 0
        for node_id, position in local.items():
            shifted = dict(position)
            shifted["x"] += block_x
            shifted["y"] += block_y
            shifted["column"] = shifted["x"]
            result[node_id] = shifted
        block_x += width + gap * 2
        block_row_height = max(block_row_height, height)
    return result


def _atlas_regions(view: GraphView, positions: Mapping[str, Mapping[str, int]]) -> list[dict[str, object]]:
    regions: list[dict[str, object]] = []
    # Areas that differ only in case would otherwise keep the set's hash-seeded order.
    areas = sorted({node.area for node in view.nodes if node.area}, key=lambda area: (area.casefold(), area))
    for area in areas:
        members = [node for node in view.nodes if node.area == area]
        region: dict[str, object] = {"count": len(members), "label": area}
        regions.append(region)
        # A view too large for the map has no layout, but its overview still counts each region.
        if not positions:
            continue
        min_x = min(positions[node.id]["x"] for node in members)
        min_y = min(positions[node.id]["y"] for node in members) - 34
        max_x = max(positions[node.id]["x"] + positions[node.id]["width"] for node in members)
        max_y = max(positions[node.id]["y"] + positions[node.id]["height"] for node in members)
        region.update(height=max_y - min_y + 24, width=max_x - min_x + 24, x=min_x - 12, y=min_y - 12)
    return regions


def _palette() -> dict[str, dict[str, object]]:
    """Name, explain and colour every status, so the explorer's swatches are its legend."""
    return {
        state.key: {
            "label": state.label,
            "meaning": MEANINGS[state.key],
            **({"neutral": True} if state in INVENTORY_STATES else {}),
            "light": {"fill": state.fill, "stroke": state.stroke, "text": state.text},
            "dark": {"fill": state.dark_fill, "stroke": state.dark_stroke, "text": state.dark_text},
        }
        for state in (*STATES, *INVENTORY_STATES)
    }


def _display_status(node: ViewNode) -> str:
    """Return the palette key a node is drawn in: a catalog's inventory status, else its own."""
    inventory = node.catalog == "module" and next(
        (key for key, count in node.status_counts if count and key in INVENTORY_STATUSES),
        None,
    )
    key = inventory or node.status_key or (node.status_counts[0][0] if node.status_counts else "planned")
    return key if key in _PALETTE_KEYS else "planned"


def _write_json(path: Path, payload: Mapping[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def write_payload(
    path: Path,
    view: GraphView,
    *,
    links: Mapping[str, str],
    breadcrumbs: Iterable[tuple[str, str | None]] = (),
    book: str | None = None,
) -> Path:
    """Write the deterministic layout and data one explorer page draws.

    *book* links a chapter's or section's explorer to its textbook page.
    """
    known = frozenset(node.id for node in view.nodes)
    if len(known) != len(view.nodes):
        raise ValueError("dependency view has duplicate node ids")
    if any(edge.source not in known or edge.target not in known for edge in view.edges):
        raise ValueError("dependency view edge names an unknown node")
    map_nodes = tuple(
        node for node in view.nodes
        if not view.overview or (node.kind == "scope" and node.navigation not in {"collection", "source", "transparent"})
    )
    map_ids = frozenset(node.id for node in map_nodes)
    map_view = replace(view, nodes=map_nodes, edges=tuple(
        edge for edge in view.edges if edge.source in map_ids and edge.target in map_ids
    ))
    positions = _positions(map_view) if len(map_nodes) <= MAX_MAP_NODES else {}
    degrees: dict[str, int] = {node.id: 0 for node in view.nodes}
    for edge in view.edges:
        if edge.source in degrees and edge.target in degrees:
            degrees[edge.source] += 1
            degrees[edge.target] += 1

    nodes: list[dict[str, object]] = []
    present_statuses: set[str] = set()
    for node in view.nodes:
        status_key = _display_status(node)
        present_statuses.add(status_key)
        present_statuses.update(key for key, count in node.status_counts if count)
        entry: dict[str, object] = {
            "area": node.area,
            "declaration": node.declaration,
            "id": node.id,
            "internal_dependency_count": node.internal_dependency_count,
            "isolated": degrees[node.id] == 0,
            "kind": node.kind,
            **({"layout": positions[node.id]} if node.id in positions else {}),
            "lean": node.lean,
            # The items the status counts add up to, so a scope's
            # "N items" header agrees with the rows beneath it.
            "member_count": node.item_count,
            "status": status_key,
            # Pairs, not an object, so the legend's state order survives sorted keys.
            "status_counts": node.status_counts,
            "summary": node.summary,
            "title": node.title,
            "url": links.get(node.id),
        }
        if view.overview:
            entry["map"] = node.id in map_ids
        if node.navigation:
            entry["navigation"] = node.navigation
        if node.examples:
            entry["examples"] = node.examples
        if node.catalog == "module" and node.lean:
            names = declaration_names(node.lean)
            shown = names[:_CATALOG_LEAN_NAMES]
            entry.update(lean=", ".join(shown), lean_more=len(names) - len(shown))
        nodes.append(entry)

    edges = [
        {"proof": edge.proof_count, "source": edge.source, "statement": edge.statement_count, "target": edge.target}
        for edge in view.edges
    ]
    ordered_statuses = [
        state.key for state in (*STATES, *INVENTORY_STATES) if state.key in present_statuses
    ]
    payload = {
        "edge_count": sum(edge.dependency_count for edge in view.edges),
        "edges": edges,
        "nodes": nodes,
        "palette": _palette(),
        "present_statuses": ordered_statuses,
        "regions": _atlas_regions(map_view, positions) if uses_atlas(map_view) else [],
        "schema": SCHEMA,
        "title": view.title,
        "view": {
            "breadcrumbs": [
                {"label": label, "url": url}
                for label, url in breadcrumbs
            ],
            "book": book,
            "kind": view.kind,
            "presentation": "atlas" if uses_atlas(view) else "dag",
            "summary": view.summary,
            **({"scope": view.scope} if view.scope is not None else {}),
            **({"overview": True} if view.overview else {}),
            **({"total_count": view.total_count} if view.total_count is not None else {}),
        },
    }
    return _write_json(path, payload)


def write_search_index(
    path: Path,
    view: GraphView,
    *,
    context_links: Mapping[str, str],
    data_links: Mapping[str, str] | None = None,
    article_links: Mapping[str, str] | None = None,
) -> Path:
    """Write a global search index and its compact authored relations.

    Publication passes the complete, uncollapsed graph view. ``relations``
    rows contain source and target indexes into ``nodes``, followed by the
    statement and proof counts from that view. No containment or import edges
    are inferred. An empty array distinguishes a supported edgeless graph
    from an older search index without relationship data.

    ``url`` remains the canonical explorer link. Optional ``data_links`` and
    ``article_links`` provide graph-context JSON and textbook anchors,
    respectively, without changing existing callers' links. Scope counts
    retain their own full-view rollup, never the count of a selected result.
    """
    node_indexes = {node.id: index for index, node in enumerate(view.nodes)}
    if len(node_indexes) != len(view.nodes):
        raise ValueError("dependency search index contains duplicate node ids")
    relation_counts: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
    for edge in view.edges:
        if edge.source not in node_indexes or edge.target not in node_indexes:
            raise ValueError("dependency search index edge names an unknown node")
        if any(type(count) is not int or count < 0 for count in (edge.statement_count, edge.proof_count)):
            raise ValueError("dependency search index edge counts must be nonnegative integers")
        counts = relation_counts[(node_indexes[edge.source], node_indexes[edge.target])]
        counts[0] += edge.statement_count
        counts[1] += edge.proof_count
    relations = [
        [source, target, statement, proof]
        for (source, target), (statement, proof) in sorted(relation_counts.items())
        if statement or proof
    ]
    nodes: list[dict[str, object]] = []
    for node in view.nodes:
        entry = {
            "id": node.id,
            "kind": node.kind,
            "status": _display_status(node),
            "title": node.title,
            "url": context_links.get(node.id),
            "result": node.kind == "node" and bool(node.declaration or node.catalog),
        }
        if node.lean:
            entry["lean"] = node.lean
        if node.summary:
            entry["summary"] = node.summary
        for key in ("declaration", "area", "navigation", "catalog"):
            value = getattr(node, key)
            if value is not None:
                entry[key] = value
        if node.kind == "scope":
            entry.update(
                member_count=node.item_count,
                status_counts=node.status_counts,
                internal_dependency_count=node.internal_dependency_count,
            )
        if data_links is not None and node.id in data_links:
            entry["data_url"] = data_links[node.id]
        if article_links is not None and node.id in article_links:
            entry["article_url"] = article_links[node.id]
        nodes.append(entry)
    return _write_json(path, {"nodes": nodes, "relations": relations, "schema": SEARCH_SCHEMA})


def render_container(
    data_href: str,
    *,
    script_href: str | None = None,
    style_href: str | None = None,
    fallback_links: Iterable[tuple[str, str]] = (),
    fallback_total: int | None = None,
    layout: str = "app",
    search_href: str | None = None,
    navigation_href: str | None = None,
) -> str:
    """Return a self-contained, progressively enhanced explorer host."""
    if layout not in {"app", "embedded"}:
        raise ValueError(f"unsupported dependency explorer layout: {layout}")
    href = html.escape(quote(data_href, safe="/%"), quote=True)
    search = html.escape(quote(search_href, safe="/%"), quote=True) if search_href else ""
    search_attr = f' data-catalog-src="{search}"' if search else ""
    navigation = html.escape(quote(navigation_href, safe="/%"), quote=True) if navigation_href else ""
    navigation_attr = f' data-navigation-src="{navigation}"' if navigation else ""
    script = (
        f'\n<script defer src="{html.escape(quote(script_href, safe="/%"), quote=True)}"></script>'
        if script_href
        else ""
    )
    style = (
        f'<link rel="stylesheet" href="{html.escape(quote(style_href, safe="/%"), quote=True)}">\n'
        if style_href
        else f"<style data-autoform-dag-style>\n{viewer_styles()}\n</style>\n"
    )
    fallback = tuple(
        (label, target)
        for label, target in fallback_links
        if target and _safe_fallback_href(target)
    )
    total = max(len(fallback), fallback_total or 0)
    items = "\n".join(
        "    <li>"
        f'<a href="{html.escape(target, quote=True)}">{html.escape(label)}</a>'
        "</li>"
        for label, target in fallback
    )
    inventory = (
        '  <div class="bp-dag-fallback">\n'
        "    <p><strong>Dependency explorer fallback.</strong> "
        "JavaScript is unavailable or the graph data could not be loaded.</p>\n"
        + (f'    <ul class="bp-dag-fallback-list">\n{items}\n    </ul>\n' if items else "")
        + (
            f"    <p>Showing {len(fallback)} of {total} linked nodes. "
            if total > len(fallback)
            else "    <p>"
        )
        + f'<a href="{href}">Download the complete graph data</a>.</p>\n'
        "  </div>\n"
    )
    return (
        style +
        f'<div class="bp-dag-viewer" data-graph-src="{href}" data-layout="{layout}"{search_attr}{navigation_attr}>\n'
        '  <p class="bp-dag-loading" role="status" hidden>Loading dependency explorer…</p>\n'
        f"{inventory}"
        f"</div>{script}"
    )


def _safe_fallback_href(value: str) -> bool:
    """Allow only generated relative links in the non-JavaScript fallback."""
    if any(ord(character) < 32 for character in value) or value.startswith("//"):
        return False
    parsed = urlsplit(value)
    return not parsed.scheme and not parsed.netloc


def viewer_script() -> str:
    """Return the dependency-free browser runtime with leading space folded."""
    source = (_ASSET_DIRECTORY / "blueprint-dag.js").read_text(encoding="utf-8")
    lines = (line.lstrip() for line in source.splitlines())
    return "\n".join(lines) + "\n"


def viewer_styles() -> str:
    """Return the shared stylesheet used by published and standalone explorers."""
    return (_ASSET_DIRECTORY / "blueprint-dag.css").read_text(encoding="utf-8").strip()


# Published pages put the explorer inside Material's article.md-typeset, whose
# list, code and heading rules reach a specificity of (0,2,2). The rules they
# would override carry a doubled .bp-dag-viewer class, and the status filter is
# a button and popover because Material styles details and summary as an
# admonition. The landing page's card frames the embedded layout and names its
# map, so that layout draws no frame or context row of its own. On phones the
# search box takes the toolbar's first row, so the Status popover opens
# rightwards from the start of the next, and the head stacks above the bottom
# sheet so that neither of its popovers paints under it.
# The shared assets are copied once into every generated site.
_ASSET_DIRECTORY = Path(__file__).with_name("assets")


__all__ = [
    "MAX_MAP_NODES",
    "SCHEMA",
    "SCRIPT_PATH",
    "SEARCH_SCHEMA",
    "STYLE_PATH",
    "render_container",
    "uses_atlas",
    "viewer_script",
    "viewer_styles",
    "write_payload",
    "write_search_index",
]
