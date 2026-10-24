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
from dataclasses import replace
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit

from .graph_views import FLATTENED_ROLES, INVENTORY_STATUSES, GraphView, ViewNode
from .lean import declaration_names
from .markdown import FORMULA_CLASS
from .status import INVENTORY_STATES, MEANINGS, STATES

SCHEMA = "autoform-dag-view/v2"
SEARCH_SCHEMA = "autoform-dag-search/v1"
#: Where the site publishes the viewer, relative to its root.
SCRIPT_PATH = "javascripts/blueprint-dag.js"
#: The most nodes the explorer draws as a map; it only browses a larger view, which gets no layout.
MAX_MAP_NODES = 120

_ALL_STATES = (*STATES, *INVENTORY_STATES)
_PALETTE_KEYS = frozenset(state.key for state in _ALL_STATES)
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
    connected: set[str] = set()
    for edge in view.edges:
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
            result[node_id] = _cell(column, row)

    isolated = [node_id for node_id in all_node_ids if node_id not in connected]
    isolate_base = max((position["column"] for position in result.values()), default=-2) + 2
    # One tall column of isolates made a project of 30 chapters fit as
    # unreadable specks, so they fill a widescreen grid beside the ranked DAG.
    cell_aspect = (_NODE_HEIGHT + _ROW_GAP) / (_NODE_WIDTH + _COLUMN_GAP)
    isolate_columns = max(1, math.ceil(math.sqrt(len(isolated) * cell_aspect * (16 / 9))))
    for slot, node_id in enumerate(isolated):
        column = isolate_base + slot % isolate_columns
        row = slot // isolate_columns
        result[node_id] = _cell(column, row)
    return result


def _cell(column: int, row: int) -> dict[str, int]:
    return {
        "column": column,
        "height": _NODE_HEIGHT,
        "row": row,
        "width": _NODE_WIDTH,
        "x": column * (_NODE_WIDTH + _COLUMN_GAP),
        "y": row * (_NODE_HEIGHT + _ROW_GAP),
    }


def drawn_view(view: GraphView) -> GraphView:
    """Return the part of *view* the explorer draws.

    An overview replaces collections and pagination ranges by their contents,
    so it draws those contents, not the wrappers or their relations.
    """
    if not view.overview:
        return view
    nodes = tuple(node for node in view.nodes if node.navigation not in FLATTENED_ROLES)
    ids = frozenset(node.id for node in nodes)
    return replace(view, nodes=nodes, edges=tuple(
        edge for edge in view.edges if edge.source in ids and edge.target in ids
    ))


def uses_atlas(view: GraphView) -> bool:
    """Draw topic cards in regions only when every drawn node is a topic.

    A leaf needs a readable card, so a view holding any leaf, such as a
    chapter with module catalogs beside its sections, is a DAG.
    """
    view = drawn_view(view)
    return (
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
    card_width = 232
    gap = 16
    region_header = 42

    def card_height(node: ViewNode) -> int:
        count = max(1, node.item_count)
        return max(88, min(116, round(84 + 4 * math.log2(count + 1))))

    by_area: dict[str, list[ViewNode]] = defaultdict(list)
    for node in view.nodes:
        by_area[node.area or ""].append(node)

    blocks: list[tuple[str, dict[str, dict[str, int]], int, int]] = []
    for area, members in sorted(by_area.items(), key=lambda item: (not item[0], item[0].casefold())):
        members.sort(key=lambda node: (-node.item_count, node.title.casefold(), node.id))
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
            # An atlas places cards by x and y; column and row only keep the DAG's layout keys.
            shifted["column"] = shifted["x"]
            result[node_id] = shifted
        block_x += width + gap * 2
        block_row_height = max(block_row_height, height)
    return result


def _atlas_regions(view: GraphView, positions: Mapping[str, Mapping[str, int]]) -> list[dict[str, object]]:
    """Frame each area's cards with 12 px of padding.

    Each frame also holds a 34 px label band above its cards, inside the 42 px
    region_header that _atlas_positions leaves for it.
    """
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
        for state in _ALL_STATES
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

    The payload carries only what the explorer reads. On an overview, the
    collections and pagination ranges it replaces ride along with
    ``map: false`` and no layout.
    """
    known = frozenset(node.id for node in view.nodes)
    if len(known) != len(view.nodes):
        raise ValueError("dependency view has duplicate node ids")
    if any(edge.source not in known or edge.target not in known for edge in view.edges):
        raise ValueError("dependency view edge names an unknown node")
    map_view = drawn_view(view)
    map_ids = frozenset(node.id for node in map_view.nodes)
    positions = _positions(map_view) if len(map_view.nodes) <= MAX_MAP_NODES else {}
    degrees: dict[str, int] = {node.id: 0 for node in view.nodes}
    for edge in map_view.edges:
        degrees[edge.source] += 1
        degrees[edge.target] += 1

    nodes: list[dict[str, object]] = []
    present_statuses: set[str] = set()
    for node in view.nodes:
        status_key = _display_status(node)
        present_statuses.add(status_key)
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
        state.key for state in _ALL_STATES if state.key in present_statuses
    ]
    payload = {
        "edge_count": sum(edge.dependency_count for edge in map_view.edges),
        "edges": edges,
        "nodes": nodes,
        "palette": _palette(),
        "present_statuses": ordered_statuses,
        "regions": _atlas_regions(map_view, positions) if uses_atlas(view) else [],
        "schema": SCHEMA,
        "title": view.title,
        "view": {
            "breadcrumbs": [
                {"label": label, "url": url}
                for label, url in breadcrumbs
            ],
            "book": book,
            "presentation": "atlas" if uses_atlas(view) else "dag",
            "summary": view.summary,
        },
    }
    return _write_json(path, payload)


def write_search_index(
    path: Path,
    view: GraphView,
    *,
    context_links: Mapping[str, str],
) -> Path:
    """Write the small global index used to jump into a hierarchical view."""
    nodes: list[dict[str, object]] = []
    for node in view.nodes:
        entry: dict[str, object] = {
            "id": node.id,
            "kind": node.kind,
            "status": _display_status(node),
            "title": node.title,
            "url": context_links.get(node.id),
        }
        # Search matches statements and Lean names as well as titles.
        if node.lean:
            entry["lean"] = node.lean
        if node.summary:
            entry["summary"] = node.summary
        nodes.append(entry)
    return _write_json(path, {"nodes": nodes, "schema": SEARCH_SCHEMA})


def render_container(
    data_href: str,
    *,
    script_href: str | None = None,
    fallback_links: Iterable[tuple[str, str]] = (),
    fallback_total: int | None = None,
    search_href: str | None = None,
    intro: str = "",
) -> str:
    """Return a self-contained, progressively enhanced explorer host.

    ``intro`` is trusted HTML, one ``.bp-dag-intro`` element, that the
    explorer shows in place of its own title: at the top of its hierarchy
    rail, or in its details sheet where phones hide the rail.
    """
    href = html.escape(quote(data_href, safe="/%"), quote=True)
    search = html.escape(quote(search_href, safe="/%"), quote=True) if search_href else ""
    search_attr = f' data-catalog-src="{search}"' if search else ""
    script = (
        f'\n<script defer src="{html.escape(quote(script_href, safe="/%"), quote=True)}"></script>'
        if script_href
        else ""
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
        "<style data-autoform-dag-style>\n"
        f"{_STYLE}\n"
        "</style>\n"
        f'<div class="bp-dag-viewer" data-graph-src="{href}"{search_attr}>\n'
        + (f"  {intro}\n" if intro else "")
        + '  <p class="bp-dag-loading" role="status" hidden>Loading dependency explorer…</p>\n'
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
    """Return the dependency-free browser runtime with leading space folded.

    The result must stay under 66,000 bytes (test_graph_view_rendering). Leading
    indentation, blank lines and whole-line ``//`` comments are stripped, so they
    cost nothing.
    """
    # Keep the embedded source readable in Python without shipping indentation
    # that browsers do not need. Internal spaces and the line breaks between
    # code lines stay intact.
    # Whole-line comments explain the source, not the page, so they stay here.
    lines = (line.lstrip() for line in _SCRIPT.replace("FORMULA_CLASS_JSON", json.dumps(FORMULA_CLASS)).splitlines())
    return "\n".join(line for line in lines if line and not line.startswith("//")) + "\n"


# Published pages put the explorer inside Material's article.md-typeset, whose
# list, code and heading rules reach a specificity of (0,2,2). The rules they
# would override carry a doubled .bp-dag-viewer class, and the status filter is
# a button and popover because Material styles details and summary as an
# admonition. The head stacks above the details drawer and the bottom sheet so
# that neither of its popovers paints under them. Sizes are in px, not rem:
# Material sets its root font to 20px, 22px from 1600px and 24px from 2000px,
# which grew the rail past 400px on a laptop. Beside the 288px rail the 384px
# drawer and its 12px insets leave a node at least 405px, two cards, from
# 1101px up; at 1100px and below, where the rail narrows, the drawer opens over
# it instead, and the covered rail leaves the tab order once the drawer has
# slid over it, so it never goes blank first; its h1 stays, in place and
# transparent, so the page keeps a heading for assistive technology. Above
# phone widths Contents then starts 12px right of the drawer's 396px edge, so
# that no row starts under it. On phones the search box takes the toolbar's
# first row, so the Status popover opens rightwards from the start of the next.
# The explorer hides the page's own heading because the hierarchy rail carries
# it; while phones hide the rail, the heading stays for assistive technology as
# hidden text. Where the rail shows and draws the trail, the head's breadcrumb
# is hidden so that the trail appears once. Above 1100px, where the rail always
# shows, the head is one row of search, controls and the textbook link, which
# comes last in the DOM so that Tab follows the row; at 1100px and below a grid
# draws that link at the end of a first row that keeps the breadcrumb's place,
# so the trail appears there while details cover the rail, and the runtime
# moves the link before the toolbar, so that Tab reaches it after the trail and
# before the controls under it. Above phone widths that row collapses while it
# would be blank, with no link and the trail left to the rail, as on the
# project page; there it opens with the details, and the runtime shifts the map
# back by the row's height, so that the map, and the card just clicked, stay
# where they were. The close button keeps the drawer's top-right corner, which
# on phones lies in the sheet's handle; the handle names the selection there,
# so the sheet keeps its own heading only as hidden text.
_STYLE = r"""
.bp-dag-viewer {
  --dag-border: var(--bp-rule, #d8dade); --dag-bg: var(--bp-surface, #fff);
  --dag-panel: color-mix(in srgb, var(--dag-bg) 94%, #64748b 6%); --dag-map: var(--bp-sunken, #f0f2f5); --dag-error: #b42318;
  --dag-fg: var(--bp-fg, #1c1e21); --dag-muted: var(--bp-muted, #65676b); --dag-link: var(--bp-link, #0064e0);
  --dag-navigation-width: 288px;
  --dag-mono: var(--md-code-font-family, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  --dag-radius-row: 6px; --dag-radius-control: 8px; --dag-radius-panel: var(--bp-radius, 12px);
  --dag-e1: 0 1px 2px rgb(0 0 0 / .06); --dag-e2: 0 8px 24px rgb(0 0 0 / .16); --dag-overlay-border: transparent;
  position: relative; display: grid; grid-template-rows: auto minmax(0, 1fr); width: 100%;
  height: clamp(608px, calc(100svh - 112px), 1024px); min-height: 608px; overflow: hidden;
  border: 1px solid var(--dag-border); border-radius: var(--dag-radius-panel); background: var(--dag-bg); color: var(--dag-fg);
  box-shadow: var(--dag-e1); isolation: isolate;
  font-family: var(--md-text-font-family, ui-sans-serif, system-ui, sans-serif);
}
body.bp-dag-app-page .bp-dag-viewer {
  position: fixed; z-index: 40; inset: 0; width: 100dvw; max-width: none;
  height: 100dvh !important; min-height: 0; margin: 0; border: 0; border-radius: 0; box-shadow: none;
}
body.bp-dag-app-page { overflow: hidden; }
[data-md-color-scheme=slate] .bp-dag-viewer { --dag-overlay-border: var(--dag-border); --dag-map: #18191a; --dag-error: #ff7b72; }
body.bp-dag-app-page .md-header, body.bp-dag-app-page .md-tabs,
body.bp-dag-app-page .md-sidebar, body.bp-dag-app-page .md-footer { display: none; }
body.bp-dag-app-page .md-main__inner { width: 100%; max-width: none; margin: 0; }
body.bp-dag-app-page .md-content { max-width: none; }
body.bp-dag-app-page .md-content__inner { margin: 0; padding: 0; }
body.bp-dag-app-page .md-content__inner::before,
body.bp-dag-app-page .md-content__inner > h1,
body.bp-dag-app-page .md-content__inner > p { display: none; }
.bp-dag-viewer *, .bp-dag-viewer *::before, .bp-dag-viewer *::after { box-sizing: border-box; }
.bp-dag-viewer [hidden] { display: none !important; }
.bp-dag-head { z-index: 13; border-bottom: 1px solid var(--dag-border); background: var(--dag-bg); }
.bp-dag-context { display: flex; align-items: center; min-height: 28px; padding: 0 16px; }
.bp-dag-breadcrumb { min-width: 0; flex: 1; color: var(--dag-muted); font-size: 12px; }
.bp-dag-viewer.bp-dag-viewer :is(.bp-dag-breadcrumb ol, .bp-dag-navigation-path) { display: flex; align-items: center; gap: 6px; margin: 0; padding: 0; list-style: none; }
.bp-dag-viewer.bp-dag-viewer :is(.bp-dag-breadcrumb, .bp-dag-navigation-path) li { min-width: 0; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
:is(.bp-dag-breadcrumb, .bp-dag-navigation-path) li + li::before { content: "/"; margin-right: 6px; color: var(--dag-border); }
:is(.bp-dag-breadcrumb, .bp-dag-navigation-path) li:not(:first-child, [aria-current]) { flex-shrink: 4; }
.bp-dag-breadcrumb [aria-current=page] { color: var(--dag-fg); font-weight: 600; }
@media (min-width: 681px) and (max-width: 1100px) { .bp-dag-viewer:has(.bp-dag-navigation-path) .bp-dag-breadcrumb { visibility: hidden; } }
.bp-dag-book { flex: none; color: var(--dag-link); font-size: 12px; font-weight: 600; text-decoration: none; white-space: nowrap; }
.bp-dag-swatch { flex: none; width: 11px; height: 11px; border: 2px solid var(--dag-swatch-light-stroke); border-radius: 50%; background: var(--dag-swatch-light-fill); }
[data-md-color-scheme=slate] .bp-dag-swatch { border-color: var(--dag-swatch-dark-stroke); background: var(--dag-swatch-dark-fill); }
.bp-dag-toolbar { display: flex; align-items: center; gap: 8px; min-height: 52px; padding: 4px 16px 12px; font-size: 13px; }
@media (min-width: 1101px) {
  .bp-dag-head { display: flex; align-items: center; gap: 12px; padding: 8px 16px; }
  .bp-dag-context { display: contents; }
  .bp-dag-breadcrumb { flex: 0 1 auto; max-width: 32%; }
  .bp-dag-viewer:has(.bp-dag-navigation-path) .bp-dag-breadcrumb { display: none; }
  .bp-dag-toolbar { flex: 1; min-width: 0; min-height: 36px; padding: 0; }
}
.bp-dag-search-wrap { position: relative; flex: 1 1 384px; min-width: 160px; max-width: 672px; }
.bp-dag-search { display: block; width: 100%; min-height: 36px; margin: 0; padding: 7px 36px 7px 32px; border: 1px solid var(--dag-border); border-radius: var(--dag-radius-control); outline: 0; background: var(--dag-panel); color: var(--dag-fg); font: inherit; }
.bp-dag-search-wrap::before {
  content: ""; position: absolute; z-index: 2; left: 11px; top: 50%; width: 14px; height: 14px; margin-top: -7px; background: currentColor; color: var(--dag-muted); pointer-events: none;
  --dag-magnifier: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='%23000' stroke-width='1.75' stroke-linecap='round'%3E%3Ccircle cx='7' cy='7' r='5'/%3E%3Cpath d='m11 11 3.5 3.5'/%3E%3C/svg%3E");
  -webkit-mask: var(--dag-magnifier) center / 14px no-repeat; mask: var(--dag-magnifier) center / 14px no-repeat;
}
.bp-dag-search-wrap::after { content: "/"; position: absolute; z-index: 2; right: 8px; top: 50%; min-width: 20px; height: 20px; margin-top: -10px; padding: 0 5px; border: 1px solid var(--dag-border); border-radius: var(--dag-radius-row); background: var(--dag-bg); color: var(--dag-muted); font-family: var(--dag-mono); font-size: 11px; line-height: 18px; text-align: center; pointer-events: none; }
.bp-dag-search-wrap:focus-within::after { display: none; }
.bp-dag-search-wrap:has(.bp-dag-search:not(:placeholder-shown))::after { display: none; }
.bp-dag-search:focus { border-color: var(--dag-link); box-shadow: 0 0 0 3px color-mix(in srgb, var(--dag-link) 18%, transparent); }
.bp-dag-search-results { position: absolute; z-index: 20; top: calc(100% + 6px); left: 0; right: 0; max-height: min(432px, 60vh); overflow: auto; padding: 6px; border: 1px solid var(--dag-overlay-border); border-radius: var(--dag-radius-panel); background: var(--dag-bg); box-shadow: var(--dag-e2); }
.bp-dag-viewer.bp-dag-viewer :is(.bp-dag-results, .bp-dag-relation-list, .bp-dag-inventory-list) { display: block; margin: 0; padding: 0; list-style: none; }
.bp-dag-viewer.bp-dag-viewer :is(.bp-dag-results, .bp-dag-relation-list, .bp-dag-inventory-list) > li { margin: 0; }
.bp-dag-result { display: flex; align-items: baseline; width: 100%; gap: 10px; padding: 8px 9px; border: 0; border-radius: var(--dag-radius-row); background: transparent; color: var(--dag-fg); text-align: left; cursor: pointer; }
.bp-dag-result:hover, .bp-dag-result:focus-visible { background: color-mix(in srgb, var(--dag-link) 9%, transparent); }
.bp-dag-result:focus-visible, .bp-dag-inventory-row:focus-visible { outline: 3px solid color-mix(in srgb, var(--dag-link) 52%, transparent); outline-offset: -3px; }
.bp-dag-result-title, .bp-dag-inventory-title { flex: 1 1 auto; min-width: 0; overflow: hidden; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.bp-dag-result-meta, .bp-dag-inventory-meta { min-width: 0; overflow: hidden; color: var(--dag-muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.bp-dag-button, .bp-dag-filter-summary, .bp-dag-mode-button, .bp-dag-hidden-button, .bp-dag-whole-search, .bp-dag-load-more { min-height: 36px; padding: 6px 10px; border: 1px solid var(--dag-border); border-radius: var(--dag-radius-control); background: var(--dag-bg); color: var(--dag-fg); font: inherit; font-weight: 600; cursor: pointer; }
.bp-dag-button:hover, .bp-dag-filter-summary:hover, .bp-dag-mode-button:hover, .bp-dag-load-more:hover { border-color: var(--dag-link); color: var(--dag-link); }
.bp-dag-button:focus-visible, .bp-dag-filter-summary:focus-visible, .bp-dag-mode-button:focus-visible, .bp-dag-pivot:focus-visible, .bp-dag-node:focus-visible, .bp-dag-sheet-handle:focus-visible { outline: 3px solid color-mix(in srgb, var(--dag-link) 45%, transparent); outline-offset: 2px; }
.bp-dag-zoom, .bp-dag-modes { display: inline-flex; flex: none; gap: 2px; padding: 2px; border: 1px solid var(--dag-border); border-radius: var(--dag-radius-control); background: var(--dag-bg); }
.bp-dag-zoom .bp-dag-button, .bp-dag-modes .bp-dag-mode-button { min-height: 30px; padding: 4px 10px; border: 0; border-radius: var(--dag-radius-row); background: transparent; color: var(--dag-muted); }
.bp-dag-zoom .bp-dag-button:hover, .bp-dag-modes .bp-dag-mode-button:not([aria-pressed=true], :disabled):hover { background: var(--dag-panel); color: var(--dag-fg); }
.bp-dag-mode-button[aria-pressed=true], .bp-dag-pivot[aria-pressed=true] { border-color: var(--dag-link); background: color-mix(in srgb, var(--dag-link) 10%, var(--dag-bg)); color: var(--dag-link); }
.bp-dag-filters { position: relative; flex: none; }
.bp-dag-filter-summary { display: flex; align-items: center; white-space: nowrap; }
.bp-dag-filter-popover { position: absolute; z-index: 18; top: calc(100% + 6px); right: 0; width: 240px; max-height: 368px; overflow: auto; padding: 10px; border: 1px solid var(--dag-overlay-border); border-radius: var(--dag-radius-panel); background: var(--dag-bg); box-shadow: var(--dag-e2); }
.bp-dag-filter-popover fieldset { display: grid; gap: 3px; margin: 0; padding: 0; border: 0; }
.bp-dag-filter-popover fieldset + fieldset { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--dag-border); }
.bp-dag-filter-popover legend { margin-bottom: 6px; }
.bp-dag-filter-popover legend, .bp-dag-viewer.bp-dag-viewer .bp-dag-navigation-group h2 { color: var(--dag-muted); font-size: 11px; font-weight: 600; letter-spacing: .06em; line-height: 1.3; text-transform: uppercase; }
.bp-dag-viewer mjx-container { text-transform: none; }
.bp-dag-check { display: flex; align-items: center; gap: 7px; min-height: 32px; padding: 3px 5px; border-radius: var(--dag-radius-row); }
.bp-dag-check:hover { background: var(--dag-panel); }
.bp-dag-check input { accent-color: var(--dag-link); }
.bp-dag-check-count { margin-left: auto; color: var(--dag-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.bp-dag-hidden-button, .bp-dag-whole-search { min-height: 26px; margin: 0; padding: 1px 3px; border: 0; background: transparent; color: var(--dag-link); }
.bp-dag-hidden-button { flex: none; margin-left: auto; white-space: nowrap; }
.bp-dag-hidden-button:hover { text-decoration: underline; }
.bp-dag-body { position: relative; display: grid; grid-template-columns: var(--dag-navigation-width) minmax(0, 1fr); min-height: 0; }
.bp-dag-navigation { z-index: 6; min-width: 0; min-height: 0; overflow: auto; border-right: 1px solid var(--dag-border); background: var(--dag-bg); }
.bp-dag-navigation-inner { display: grid; grid-template-columns: minmax(0, 1fr); align-content: start; min-height: 100%; padding: 16px 12px 24px; }
.bp-dag-viewer.bp-dag-viewer .bp-dag-navigation-title { margin: 0 6px; color: var(--dag-fg); font-size: 22px; font-weight: 700; letter-spacing: -.02em; line-height: 1.2; }
.bp-dag-navigation-summary { display: -webkit-box; margin: 9px 6px 15px; overflow: hidden; color: var(--dag-muted); font-size: 13px; line-height: 1.5; -webkit-box-orient: vertical; -webkit-line-clamp: 4; }
.bp-dag-viewer.bp-dag-viewer ol.bp-dag-navigation-path { margin: 0 6px 6px; color: var(--dag-muted); font-size: 12px; line-height: 1.4; }
.bp-dag-viewer.bp-dag-viewer .bp-dag-navigation-path a { color: inherit; text-decoration: none; }
.bp-dag-viewer.bp-dag-viewer .bp-dag-navigation-path a:hover { color: var(--dag-link); }
.bp-dag-navigation-counts { margin: 0 6px 16px; color: var(--dag-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.bp-dag-navigation-group { margin-top: 12px; }
.bp-dag-viewer.bp-dag-viewer .bp-dag-navigation-group h2 { margin: 0 6px 5px; }
.bp-dag-viewer.bp-dag-viewer .bp-dag-navigation-list { display: grid; gap: 3px; margin: 0; padding: 0; list-style: none; }
.bp-dag-viewer.bp-dag-viewer .bp-dag-navigation-list > li { margin: 0; }
.bp-dag-navigation-link { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; width: 100%; gap: 8px; min-height: 36px; padding: 7px 8px; border: 1px solid transparent; border-radius: var(--dag-radius-row); background: transparent; color: var(--dag-fg) !important; font: inherit; font-size: 13px; line-height: 1.25; text-align: left; text-decoration: none !important; cursor: pointer; }
.bp-dag-navigation-link > span:first-child { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bp-dag-navigation-link:hover, .bp-dag-navigation-link:focus-visible { border-color: var(--dag-border); background: var(--dag-panel); }
.bp-dag-navigation-link[aria-current=true] { border-color: color-mix(in srgb, var(--dag-link) 38%, var(--dag-border)); background: color-mix(in srgb, var(--dag-link) 9%, var(--dag-bg)); color: var(--dag-link) !important; }
.bp-dag-navigation-meta { color: var(--dag-muted); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.bp-dag-navigation-more { width: 100%; margin-top: 7px; border: 0; background: transparent; color: var(--dag-link); font: inherit; font-size: 12px; text-align: left; cursor: pointer; }
.bp-dag-main { position: relative; min-width: 0; min-height: 0; overflow: hidden; background: var(--dag-map); }
.bp-dag-stage { position: absolute; inset: 0; overflow: hidden; touch-action: none; cursor: grab; }
.bp-dag-stage[data-dragging=true] { cursor: grabbing; }
.bp-dag-canvas, .bp-dag-node-layer { position: absolute; inset: 0; width: 100%; height: 100%; }
.bp-dag-canvas { display: block; min-height: 0; touch-action: auto; background: transparent; }
.bp-dag-node-layer { overflow: hidden; pointer-events: none; }
.bp-dag-node { position: absolute; top: 0; left: 0; display: grid; align-content: center; width: 196px; height: 56px; padding: 6px 10px; overflow: hidden; border: 1.5px solid var(--dag-node-stroke); border-radius: var(--dag-radius-control); background: var(--dag-node-fill); color: var(--dag-node-text); box-shadow: var(--dag-e1); text-align: left; transform-origin: 0 0; pointer-events: auto; cursor: pointer; will-change: transform; }
.bp-dag-node-title { overflow: hidden; font-size: 14px; font-weight: 600; line-height: 1.18; text-overflow: ellipsis; white-space: nowrap; }
.bp-dag-node-meta { overflow: hidden; color: color-mix(in srgb, var(--dag-node-text) 95%, var(--dag-node-fill)); font-size: 12px; line-height: 1.2; text-overflow: ellipsis; white-space: nowrap; }
.bp-dag-node[data-selected=true] { box-shadow: 0 0 0 2px var(--dag-link), var(--dag-e1); }
.bp-dag-node[data-subdued=true] { opacity: .42; }
.bp-dag-node[data-kind=scope] { border-radius: var(--dag-radius-panel); }
.bp-dag-node[data-kind=boundary] { border-style: dashed; }
.bp-dag-viewer.bp-dag-atlas .bp-dag-node[data-kind=scope] { display: block; padding: 3px; border: 0; border-radius: var(--dag-radius-panel); background: var(--dag-status-ring, var(--dag-node-stroke)); text-align: left; }
.bp-dag-node-core { display: grid; align-content: center; width: 100%; height: 100%; padding: 12px 16px; border-radius: calc(var(--dag-radius-panel) - 3px); background: var(--dag-node-fill); color: var(--dag-node-text); }
.bp-dag-viewer.bp-dag-atlas .bp-dag-node[data-kind=scope] .bp-dag-node-title { display: -webkit-box; overflow: hidden; white-space: normal; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.bp-dag-viewer.bp-dag-atlas .bp-dag-node[data-kind=scope] .bp-dag-node-meta { margin-top: 7px; white-space: normal; }
.bp-dag-viewer.bp-dag-atlas .bp-dag-node-title { font-size: 17px; }
.bp-dag-node-relation { display: none; }
.bp-dag-viewer.bp-dag-atlas .bp-dag-node[data-relation=prerequisite],
.bp-dag-viewer.bp-dag-atlas .bp-dag-node[data-relation=dependent] { box-shadow: 0 0 0 2px color-mix(in srgb, var(--dag-link) 40%, transparent), var(--dag-e1); }
.bp-dag-viewer.bp-dag-atlas .bp-dag-node[data-relation=prerequisite] .bp-dag-node-relation,
.bp-dag-viewer.bp-dag-atlas .bp-dag-node[data-relation=dependent] .bp-dag-node-relation { display: block; margin-top: 6px; opacity: .75; font-size: 12px; font-weight: 600; }
.bp-dag-density { position: absolute; z-index: 4; left: 16px; bottom: 12px; max-width: calc(100% - 32px); color: var(--dag-muted); font-size: 12px; pointer-events: none; }
.bp-dag-empty { position: absolute; z-index: 4; top: 50%; left: 50%; padding: 9px 13px; border: 1px solid var(--dag-border); border-radius: 999px; background: var(--dag-bg); color: var(--dag-muted); box-shadow: var(--dag-e1); font-size: 13px; transform: translate(-50%, -50%); pointer-events: none; }
.bp-dag-inventory { position: absolute; inset: 0; display: grid; grid-template-rows: auto minmax(0, 1fr) auto; overflow: hidden; background: var(--dag-bg); font-size: 13px; }
.bp-dag-inventory-head { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; padding: 12px 16px; border-bottom: 1px solid var(--dag-border); }
.bp-dag-viewer.bp-dag-viewer .bp-dag-inventory-head h2 { margin: 0; font-size: 14px; font-weight: 700; letter-spacing: normal; line-height: 1.3; }
.bp-dag-inventory-summary { color: var(--dag-muted); font-size: 12px; }
.bp-dag-inventory-scroll { overflow: auto; padding: 7px; }
.bp-dag-inventory-row { display: flex; align-items: baseline; width: 100%; gap: 13px; padding: 10px 11px; border: 0; border-radius: var(--dag-radius-row); background: transparent; color: var(--dag-fg); text-align: left; cursor: pointer; }
.bp-dag-inventory-row:hover, .bp-dag-inventory-row:focus-visible { background: var(--dag-panel); }
.bp-dag-inventory-row[aria-current=true] { background: color-mix(in srgb, var(--dag-link) 10%, var(--dag-bg)); box-shadow: inset 3px 0 0 var(--dag-link); }
.bp-dag-load-more { justify-self: center; margin: 9px; }
.bp-dag-inspector { position: absolute; z-index: 10; top: 12px; right: 12px; bottom: 12px; width: min(384px, calc(100% - 24px)); min-width: 0; overflow: auto; border: 1px solid var(--dag-overlay-border); border-radius: var(--dag-radius-panel); font-size: 13px; background: var(--dag-bg); box-shadow: var(--dag-e2); visibility: hidden; opacity: 0; transform: translateX(calc(100% + 24px)); transition: opacity .16s ease, transform .2s ease, visibility 0s linear .2s; }
.bp-dag-viewer.bp-dag-has-selection.bp-dag-sheet-open .bp-dag-inspector { visibility: visible; opacity: 1; transform: translateX(0); transition-delay: 0s; }
.bp-dag-inspector-inner { padding: 16px; }
.bp-dag-sheet-handle { display: none; }
.bp-dag-inspector-header { padding-right: 32px; }
.bp-dag-viewer.bp-dag-viewer .bp-dag-inspector-header h2 { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: normal; line-height: 1.25; }
.bp-dag-viewer.bp-dag-viewer .bp-dag-inspector-inner > h2 { margin: 0 0 8px; font-size: 17px; font-weight: 700; letter-spacing: normal; line-height: 1.25; }
.bp-dag-close { position: absolute; z-index: 3; top: 10px; right: 10px; width: 32px; height: 32px; padding: 0; border: 0; border-radius: var(--dag-radius-control); background: transparent; color: var(--dag-muted); font-size: 22px; line-height: 1; cursor: pointer; }
.bp-dag-close:hover { background: var(--dag-panel); color: var(--dag-fg); }
.bp-dag-kicker { margin: 0 0 6px; color: var(--dag-muted); font-size: 12px; font-weight: 600; }
.bp-dag-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin: 10px 0 12px; }
.bp-dag-status-pill { display: inline-flex; align-items: center; gap: 6px; color: var(--dag-muted); font-size: 12px; }
.bp-dag-status-pill::before { content: ""; width: 9px; height: 9px; border: 2px solid var(--dag-pill-stroke); border-radius: 50%; background: var(--dag-pill-fill); }
.bp-dag-open { display: inline-flex; align-items: center; justify-content: center; min-height: 36px; padding: 6px 12px; border-radius: var(--dag-radius-control); background: var(--dag-link); color: #fff !important; font-size: 13px; font-weight: 600; text-decoration: none !important; }
[data-md-color-scheme=slate] .bp-dag-open { background: #0064e0; }
.bp-dag-overview { color: var(--dag-muted); font-size: 13px; line-height: 1.55; }
.bp-dag-overview strong { color: var(--dag-fg); }
.bp-dag-summary { margin: 13px 0; color: var(--dag-fg); font-size: 14px; line-height: 1.55; }
.bp-dag-lean { margin: 10px 0 0; overflow-wrap: anywhere; color: var(--dag-muted); font-size: 12px; font-family: var(--dag-mono); }
.bp-dag-section { margin-top: 20px; }
.bp-dag-viewer.bp-dag-viewer .bp-dag-section h3 { margin: 0 0 7px; font-size: 13px; font-weight: 600; letter-spacing: normal; line-height: 1.3; }
.bp-dag-pivots { display: flex; gap: 2px; margin-top: 16px; padding: 2px; border: 1px solid var(--dag-border); border-radius: var(--dag-radius-control); }
.bp-dag-pivot { flex: 1 1 auto; min-width: 0; min-height: 30px; padding: 4px 8px; border: 0; border-radius: var(--dag-radius-row); background: transparent; color: var(--dag-muted); font: inherit; font-size: 12px; font-weight: 600; white-space: nowrap; cursor: pointer; }
.bp-dag-pivot:not([aria-pressed=true], :disabled):hover { background: var(--dag-panel); color: var(--dag-fg); }
.bp-dag-pivot:disabled { opacity: .45; cursor: default; }
.bp-dag-relation-button { width: 100%; padding: 6px 4px; overflow: hidden; border: 0; background: transparent; color: var(--dag-link); font: inherit; font-size: 13px; text-align: left; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.bp-dag-relation-button:hover { text-decoration: underline; }
.bp-dag-distribution { display: grid; gap: 5px; margin: 6px 0 0; }
.bp-dag-distribution-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; color: var(--dag-muted); font-size: 12px; }
.bp-dag-distribution-label { display: flex; align-items: center; gap: 6px; min-width: 0; }
.bp-dag-loading, .bp-dag-error { margin: 0; padding: 16px; }
.bp-dag-error { color: var(--dag-error); }
.bp-dag-fallback { min-height: 0; overflow: auto; padding: 0 16px 16px; color: var(--dag-muted); }
.bp-dag-fallback p { max-width: 768px; }
.bp-dag-fallback-list { display: grid; gap: 5px; margin: 13px 0; padding-left: 22px; }
.bp-dag-viewer.bp-dag-enhancing .bp-dag-fallback { display: none; }
.bp-dag-sr-only, body.bp-dag-app-page .md-content__inner > h1 { position: absolute !important; width: 1px !important; height: 1px !important; padding: 0 !important; overflow: hidden !important; clip: rect(0, 0, 0, 0) !important; white-space: nowrap !important; border: 0 !important; }
@media (max-width: 1100px) {
  .bp-dag-viewer { --dag-navigation-width: 248px; }
  .bp-dag-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; padding-top: 8px; }
  .bp-dag-book { grid-area: 1 / 2; align-self: center; margin-right: 16px; }
  .bp-dag-toolbar { grid-column: 1 / -1; }
  .bp-dag-inspector { right: auto; left: 12px; transform: translateX(calc(-100% - 24px)); }
  .bp-dag-viewer.bp-dag-has-selection.bp-dag-sheet-open .bp-dag-breadcrumb { visibility: visible; }
  .bp-dag-viewer.bp-dag-has-selection.bp-dag-sheet-open .bp-dag-navigation { visibility: hidden; transition: visibility 0s .2s; }
  .bp-dag-viewer.bp-dag-has-selection.bp-dag-sheet-open .bp-dag-navigation h1 { visibility: visible; opacity: 0; transition: opacity 0s .2s; }
}
@media (min-width: 681px) and (max-width: 1100px) {
  .bp-dag-viewer.bp-dag-has-selection.bp-dag-sheet-open .bp-dag-inventory { left: calc(408px - var(--dag-navigation-width)); }
  .bp-dag-viewer:has(.bp-dag-navigation-path):not(.bp-dag-has-selection.bp-dag-sheet-open) .bp-dag-head:not(:has(.bp-dag-book)) .bp-dag-context { display: none; }
}
@media (max-width: 900px) {
  .bp-dag-viewer { --dag-navigation-width: 216px; }
  .bp-dag-toolbar { flex-wrap: wrap; }
  .bp-dag-search-wrap { max-width: none; }
}
@media (max-width: 680px) {
  .bp-dag-context { padding-inline: 8px; }
  .bp-dag-head { grid-template-columns: minmax(0, 1fr); }
  .bp-dag-head:has(.bp-dag-book) .bp-dag-context { min-height: 0; }
  .bp-dag-book { grid-area: 2 / 1; justify-self: start; margin: 4px 8px 0; }
  .bp-dag-toolbar { gap: 6px; padding: 6px 8px 8px; }
  .bp-dag-search-wrap { flex-basis: calc(100% - 6px); order: -2; }
  .bp-dag-search-wrap::after { display: none; }
  .bp-dag-search { padding-right: 12px; }
  .bp-dag-filter-popover { left: 0; right: auto; max-width: calc(100vw - 16px); }
  .bp-dag-search, .bp-dag-button, .bp-dag-filter-summary, .bp-dag-mode-button { min-height: 44px; }
  .bp-dag-zoom, .bp-dag-modes { gap: 0; padding: 0; }
  .bp-dag-zoom .bp-dag-button, .bp-dag-modes .bp-dag-mode-button { min-height: 42px; }
  .bp-dag-body { display: block; }
  .bp-dag-navigation { display: none; }
  body.bp-dag-app-page .md-content__inner > h1 { display: block; }
  .bp-dag-main { position: absolute; inset: 0; }
  .bp-dag-inventory { padding-bottom: 60px; }
  .bp-dag-inspector { position: absolute; z-index: 12; top: auto; right: 8px; bottom: 8px; left: 8px; width: auto; max-height: min(58%, 480px); overflow: auto; border: 1px solid var(--dag-overlay-border); border-radius: var(--dag-radius-panel); box-shadow: var(--dag-e2); visibility: visible; opacity: 1; transform: translateY(calc(100% - 52px)); transition: transform .2s ease; }
  .bp-dag-viewer.bp-dag-sheet-open .bp-dag-inspector { transform: translateY(0); }
  .bp-dag-sheet-handle { display: block; position: sticky; z-index: 2; top: 0; width: 100%; min-height: 50px; padding: 10px 48px 8px 12px; overflow: hidden; border: 0; border-bottom: 1px solid var(--dag-border); background: var(--dag-bg); color: var(--dag-fg); font: inherit; font-size: 13px; font-weight: 600; text-align: left; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
  .bp-dag-sheet-handle::before { content: ""; position: absolute; top: 6px; left: 50%; width: 32px; height: 3px; border-radius: 2px; background: var(--dag-border); transform: translateX(-50%); }
  .bp-dag-inspector-inner { padding: 12px; }
  .bp-dag-viewer:not(.bp-dag-sheet-open) .bp-dag-close { display: none; }
  .bp-dag-inspector-header { padding-right: 0; }
  .bp-dag-viewer.bp-dag-viewer .bp-dag-inspector-header h2 { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; }
  .bp-dag-density { bottom: 68px; }
}
@media (prefers-reduced-motion: reduce) {
  .bp-dag-inspector { transition: none; }
  .bp-dag-node { will-change: auto; }
}
""".strip()


_SCRIPT = r"""/* Generated by autoform render. Edits are overwritten. */
(function () {
  "use strict";
  if (window.__AUTOFORM_DAG_VIEWER_V2__) return;
  window.__AUTOFORM_DAG_VIEWER_V2__ = true;

  // The class of the site's formulas: the authored titles and summaries the
  // rail and the details draw carry it, so the site's MathJax typesets them.
  var TEX = FORMULA_CLASS_JSON;
  var NODE_W = 196, NODE_H = 56, MAX_MAP_NODES = 120, LIST_PAGE = 100;
  var SEARCH_PAGE = 50, RELATION_PAGE = 50, MIN_SCALE = .002, MIN_READABLE_SCALE = .8, MIN_ATLAS_SCALE = .68, hostSequence = 0;
  var clamp = function (value, low, high) { return Math.max(low, Math.min(high, value)); };
  var plural = function (count, one, many) { return count.toLocaleString() + " " + (count === 1 ? one : many); };

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(className, text, label) {
    var node = element("button", className, text);
    node.type = "button";
    if (label) node.setAttribute("aria-label", label);
    return node;
  }

  function safeHref(value) {
    if (!value || /[\u0000-\u001f]/.test(value)) return null;
    try {
      var url = new URL(value, window.location.href);
      return /^(https?:)$/.test(url.protocol) && url.origin === window.location.origin ? value : null;
    } catch (_error) { return null; }
  }

  function start(host) {
    if (host.getAttribute("data-dag-ready") === "true") return;
    host.setAttribute("data-dag-ready", "true");
    host.classList.add("bp-dag-enhancing");
    var loading = host.querySelector(".bp-dag-loading"); if (loading) loading.hidden = false;
    document.body.classList.add("bp-dag-app-page");
    var instance = ++hostSequence;
    fetch(host.getAttribute("data-graph-src"), {credentials: "same-origin"}).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (data) { mount(host, data, instance); }).catch(function (error) {
      host.classList.remove("bp-dag-enhancing");
      document.body.classList.remove("bp-dag-app-page");
      if (loading) {
        loading.textContent = "The interactive graph could not be loaded: " + error.message;
        loading.classList.add("bp-dag-error");
      }
    });
  }

  function mount(host, data, instance) {
    if (!data || data.schema !== "autoform-dag-view/v2" || !data.palette ||
        !Array.isArray(data.nodes) || !Array.isArray(data.edges)) throw new Error("unsupported graph payload");
    var shown = data.nodes.filter(function (n) { return n.map !== false; }), ids = new Set(shown.map(function (n) { return n.id; }));
    data.nodes = shown; data.edges = data.edges.filter(function (e) { return ids.has(e.source) && ids.has(e.target); });
    var intro = host.querySelector(".bp-dag-intro"), introHeading = intro && intro.querySelector("h1");
    var introTitle = introHeading ? introHeading.textContent.trim() : ""; if (intro) intro.remove();
    host.textContent = "";
    host.classList.remove("bp-dag-enhancing");
    var view = data.view || {};
    var atlas = view.presentation === "atlas"; host.classList.toggle("bp-dag-atlas", atlas);
    var palette = data.palette || {};
    var byId = new Map(), prerequisites = new Map(), dependents = new Map(), incident = new Map();
    var statusCounts = new Map(), presentStatuses = [];
    data.nodes.forEach(function (node, index) {
      var layout = node.layout || {};
      node._index = index; node.column = layout.column; node.row = layout.row;
      node.x = Number.isFinite(layout.x) ? layout.x : (node.column || 0) * 312;
      node.y = Number.isFinite(layout.y) ? layout.y : (node.row || 0) * 66;
      node.width = Number.isFinite(layout.width) ? layout.width : NODE_W;
      node.height = Number.isFinite(layout.height) ? layout.height : NODE_H;
      node.isolated = node.isolated === true;
      byId.set(node.id, node);
      prerequisites.set(node.id, []); dependents.set(node.id, []); incident.set(node.id, []);
      statusCounts.set(node.status, (statusCounts.get(node.status) || 0) + 1);
    });
    data.edges.forEach(function (edge, index) {
      if (prerequisites.has(edge.target) && byId.has(edge.source)) prerequisites.get(edge.target).push(edge.source);
      if (dependents.has(edge.source) && byId.has(edge.target)) dependents.get(edge.source).push(edge.target);
      if (incident.has(edge.source)) incident.get(edge.source).push(index);
      if (incident.has(edge.target)) incident.get(edge.target).push(index);
    });
    var isolateIndex = 0;
    data.nodes.forEach(function (node) {
      node.isolated = node.isolated || ((prerequisites.get(node.id) || []).length + (dependents.get(node.id) || []).length === 0);
      node._isolateIndex = node.isolated ? isolateIndex++ : -1;
    });
    var mobileIsolateBase = (data.nodes.reduce(function (largest, node) {
      return node.isolated ? largest : Math.max(largest, Number.isFinite(node.column) ? node.column : 0);
    }, -2) + 2) * (NODE_H + 72);
    (data.present_statuses || Object.keys(palette)).forEach(function (key) {
      if (statusCounts.has(key) && presentStatuses.indexOf(key) < 0) presentStatuses.push(key);
    });
    statusCounts.forEach(function (_count, key) {
      if (presentStatuses.indexOf(key) < 0) presentStatuses.push(key);
    });

    var mapAllowed = data.nodes.length <= MAX_MAP_NODES;
    var state = {
      active: [], activeDirty: true, activeIds: new Set(), direction: "both", listLimit: LIST_PAGE, mode: "graph", moved: false,
      orientation: "lr", pointers: new Map(), pinch: null, query: "", searchLimit: 12, selected: null,
      searchCache: [], searchCacheQuery: null, searchError: null, searchLoading: false,
      searchNodes: data.nodes, searchPromise: null, spotlight: null, navigationLimit: atlas ? 120 : 50,
      mapAllowed: mapAllowed, readableCrop: false, introInSheet: sheetLayout(),
      selectedStatuses: new Set(presentStatuses), showIsolated: true,
      transform: {x: 20, y: 20, scale: 1}
    };
    if (!mapAllowed) state.mode = "list";
    var refs = {}, overlays = new Map(), scheduled = false, firstResize = true, family;
    var trail = Array.isArray(view.breadcrumbs) ? view.breadcrumbs : [];

    buildShell(); markIntro();
    bindEvents();
    applyLocation();
    resize();
    redirectLegacyFocus();

    function buildShell() {
      var head = element("header", "bp-dag-head");
      var context = element("div", "bp-dag-context");
      var crumbs = element("nav", "bp-dag-breadcrumb");
      crumbs.setAttribute("aria-label", "Explorer context");
      crumbs.appendChild(trailList(trail.length ? trail : [{label: data.title || "Dependencies"}])); context.appendChild(crumbs);
      head.appendChild(context);

      var toolbar = element("div", "bp-dag-toolbar");
      toolbar.setAttribute("role", "group"); toolbar.setAttribute("aria-label", "Dependency explorer controls");
      var searchWrap = refs.searchWrap = element("div", "bp-dag-search-wrap");
      refs.search = element("input", "bp-dag-search");
      refs.search.type = "search"; refs.search.placeholder = "Search the roadmap";
      refs.search.setAttribute("aria-label", "Search all graph nodes");
      refs.search.setAttribute("aria-controls", "bp-dag-search-results-" + instance);
      refs.searchResults = element("div", "bp-dag-search-results"); refs.searchResults.id = "bp-dag-search-results-" + instance;
      refs.searchResults.setAttribute("role", "region"); refs.searchResults.setAttribute("aria-label", "Search results");
      refs.searchResults.hidden = true;
      searchWrap.appendChild(refs.search); searchWrap.appendChild(refs.searchResults); toolbar.appendChild(searchWrap);

      refs.filters = element("div", "bp-dag-filters");
      refs.filterSummary = button("bp-dag-filter-summary", "Status"); refs.filterSummary.setAttribute("aria-expanded", "false");
      refs.filterPopover = element("div", "bp-dag-filter-popover"); refs.filterPopover.hidden = true;
      refs.filterPopover.id = "bp-dag-filters-" + instance; refs.filterSummary.setAttribute("aria-controls", refs.filterPopover.id);
      var fieldset = element("fieldset");
      fieldset.appendChild(element("legend", "", "Statuses in this view")); refs.statusInputs = new Map();
      presentStatuses.forEach(function (key) {
        var label = element("label", "bp-dag-check"), input = element("input");
        input.type = "checkbox"; input.checked = true; input.value = key; input.addEventListener("change", onStatusChange);
        refs.statusInputs.set(key, input); label.appendChild(input); label.appendChild(swatch(key));
        label.appendChild(document.createTextNode(statusLabel(key)));
        label.appendChild(element("span", "bp-dag-check-count", String(statusCounts.get(key) || 0))); fieldset.appendChild(label);
      });
      var edges = element("fieldset", "bp-dag-isolated"), isolated = element("label", "bp-dag-check");
      edges.appendChild(element("legend", "", "Edges"));
      refs.isolated = element("input"); refs.isolated.type = "checkbox"; isolated.appendChild(refs.isolated);
      isolated.appendChild(document.createTextNode("Include items without edges")); edges.appendChild(isolated);
      edges.hidden = !data.nodes.some(function (node) { return node.isolated; }) ||
        data.nodes.every(function (node) { return node.isolated; });
      refs.filterPopover.appendChild(fieldset); refs.filterPopover.appendChild(edges);
      refs.filters.appendChild(refs.filterSummary); refs.filters.appendChild(refs.filterPopover); toolbar.appendChild(refs.filters);

      var modes = element("div", "bp-dag-modes"); modes.setAttribute("role", "group"); modes.setAttribute("aria-label", "Explorer view");
      refs.graphMode = button("bp-dag-mode-button", mapAllowed ? "Map" : "Map · too many items");
      refs.graphMode.disabled = !mapAllowed; refs.listMode = button("bp-dag-mode-button", "Contents");
      modes.appendChild(refs.graphMode); modes.appendChild(refs.listMode); toolbar.appendChild(modes);

      var zoomGroup = element("div", "bp-dag-zoom"); zoomGroup.setAttribute("role", "group"); zoomGroup.setAttribute("aria-label", "Zoom controls");
      refs.zoomOut = button("bp-dag-button", "−", "Zoom out"); refs.zoomIn = button("bp-dag-button", "+", "Zoom in");
      refs.fit = button("bp-dag-button", "Fit", "Fit shown nodes");
      zoomGroup.appendChild(refs.zoomOut); zoomGroup.appendChild(refs.zoomIn); zoomGroup.appendChild(refs.fit); toolbar.appendChild(zoomGroup);

      refs.hiddenButton = button("bp-dag-hidden-button", ""); toolbar.appendChild(refs.hiddenButton);
      head.appendChild(toolbar);
      // Tab follows where the head draws the link: after the controls in one row, before them in the grid.
      var book = safeHref(view.book);
      if (book) {
        var bookLink = element("a", "bp-dag-book", "Open the textbook page"), narrow = matchMedia("(max-width: 1100px)");
        var placeBook = function () { var had = document.activeElement === bookLink; head.insertBefore(bookLink, narrow.matches ? toolbar : null); if (had) bookLink.focus(); };
        bookLink.href = book; placeBook(); narrow.addEventListener("change", placeBook);
      }
      host.appendChild(head);

      var body = refs.body = element("div", "bp-dag-body"), main = element("div", "bp-dag-main");
      refs.navigation = element("aside", "bp-dag-navigation");
      refs.navigation.setAttribute("aria-label", "Wiki hierarchy");
      refs.navigationInner = element("div", "bp-dag-navigation-inner");
      refs.navigation.appendChild(refs.navigationInner); body.appendChild(refs.navigation);
      renderNavigation();
      refs.stage = element("section", "bp-dag-stage"); refs.stage.setAttribute("role", "region");
      refs.stage.setAttribute("aria-label", "Dependency graph. Drag or scroll to pan, " +
        "use Control or Command plus scroll to zoom, and use arrow keys between node buttons.");
      refs.canvas = element("canvas", "bp-dag-canvas"); refs.canvas.setAttribute("aria-hidden", "true");
      refs.nodeLayer = element("div", "bp-dag-node-layer"); refs.density = element("div", "bp-dag-density"); refs.density.hidden = true;
      refs.empty = element("div", "bp-dag-empty", data.nodes.length ? "No nodes match these controls." :
        "This roadmap has no articles yet."); refs.empty.hidden = true;
      refs.stage.appendChild(refs.canvas); refs.stage.appendChild(refs.nodeLayer);
      refs.stage.appendChild(refs.density); refs.stage.appendChild(refs.empty);
      main.appendChild(refs.stage);

      refs.inventory = element("section", "bp-dag-inventory"); refs.inventory.hidden = true;
      refs.inventory.setAttribute("aria-label", "Complete node inventory");
      var inventoryHead = element("div", "bp-dag-inventory-head"); inventoryHead.appendChild(element("h2", "", "Contents"));
      refs.inventorySummary = element("span", "bp-dag-inventory-summary"); inventoryHead.appendChild(refs.inventorySummary);
      refs.wholeSearch = button("bp-dag-whole-search", "search the whole roadmap");
      refs.inventoryScroll = element("div", "bp-dag-inventory-scroll"); refs.inventoryList = element("ul", "bp-dag-inventory-list");
      refs.inventoryScroll.appendChild(refs.inventoryList); refs.loadMore = button("bp-dag-load-more", "Show more nodes");
      refs.inventory.appendChild(inventoryHead); refs.inventory.appendChild(refs.inventoryScroll); refs.inventory.appendChild(refs.loadMore);
      main.appendChild(refs.inventory); body.appendChild(main);

      refs.inspector = element("aside", "bp-dag-inspector"); refs.inspector.setAttribute("aria-label", "Node inspector");
      refs.sheetHandle = button("bp-dag-sheet-handle", "Graph details", "Toggle graph details");
      refs.sheetHandle.setAttribute("aria-expanded", "false");
      refs.inspectorInner = element("div", "bp-dag-inspector-inner");
      refs.inspectorInner.id = "bp-dag-inspector-content-" + instance;
      refs.sheetHandle.setAttribute("aria-controls", refs.inspectorInner.id);
      refs.inspector.appendChild(refs.sheetHandle);
      refs.inspector.appendChild(refs.inspectorInner); body.appendChild(refs.inspector); host.appendChild(body);
      refs.announcement = element("p", "bp-dag-sr-only"); refs.announcement.setAttribute("aria-live", "polite"); host.appendChild(refs.announcement);
      refs.ctx = refs.canvas.getContext("2d");
      // Reset the probe first: a rejected site font must not inherit an earlier 11px font.
      refs.ctx.font = "10px sans-serif";
      family = getComputedStyle(host).fontFamily; refs.ctx.font = "11px " + family;
      if (refs.ctx.font.indexOf("11px") < 0) family = "sans-serif";
    }

    function trailList(entries, className, tex) {
      var list = element("ol", className);
      entries.forEach(function (entry, index) {
        // The class goes on the element that holds the text: the link, when there is one.
        var href = safeHref(entry.url), item = element("li", href ? "" : tex);
        item.title = entry.label;
        if (href) { var link = element("a", tex, entry.label); link.href = href; link.title = entry.label; item.appendChild(link); }
        else { item.textContent = entry.label; if (!className && index === entries.length - 1) item.setAttribute("aria-current", "page"); }
        list.appendChild(item);
      });
      return list;
    }
    // The site's javascripts/mathjax.js typesets the text marked TEX as it does articles' formulas, after the
    // calling render; before MathJax starts, its first pass reads it.
    function typeset() { document.dispatchEvent(new Event("autoform:typeset")); }
    function renderNavigation() {
      if (!refs.navigationInner) return;
      typeset(); refs.navigationInner.textContent = ""; refs.navigationNodes = new Map();
      if (intro) { if (!state.introInSheet) refs.navigationInner.appendChild(intro); } else {
      // The h1 names the current scope, so the rail's trail lists only its ancestors.
      if (trail.length > 1) refs.navigationInner.appendChild(trailList(trail.slice(0, -1), "bp-dag-navigation-path", TEX));
      refs.navigationInner.appendChild(element("h1", "bp-dag-navigation-title " + TEX, data.title || "Mathematics"));
      if (view.summary) refs.navigationInner.appendChild(element("p", "bp-dag-navigation-summary " + TEX, view.summary));
      refs.navigationInner.appendChild(element("p", "bp-dag-navigation-counts", plural(data.nodes.length, atlas ? "topic" : "item",
        atlas ? "topics" : "items") + " · " + plural(data.edge_count || data.edges.length, "dependency", "dependencies")));
      }

      var visible = data.nodes.slice(0, state.navigationLimit), groups = new Map();
      visible.forEach(function (node) {
        var label = node.area || (atlas ? "Other mathematics" : "Inside this scope");
        if (!groups.has(label)) groups.set(label, []); groups.get(label).push(node);
      });
      groups.forEach(function (nodes, label) {
        var section = element("section", "bp-dag-navigation-group");
        if (groups.size > 1) section.appendChild(element("h2", TEX, label));
        var list = element("ul", "bp-dag-navigation-list");
        nodes.forEach(function (node) {
          var item = element("li"), href = safeHref(node.url), control;
          if (href) { control = element("a", "bp-dag-navigation-link"); control.href = href; }
          else {
            control = button("bp-dag-navigation-link", "");
            control.addEventListener("click", function () { activateNode(node, true); });
          }
          control.dataset.navigationNodeId = node.id;
          control.appendChild(element("span", TEX, node.title));
          // A scope counts its items; a leaf shows its status as a swatch, so the rows end evenly.
          var mark = node.kind === "scope" ? element("span", "bp-dag-navigation-meta", (node.member_count || 0).toLocaleString()) : swatch(node.status);
          if (node.kind !== "scope") { mark.title = statusLabel(node.status); mark.setAttribute("role", "img"); mark.setAttribute("aria-label", mark.title); }
          control.appendChild(mark);
          item.appendChild(control); list.appendChild(item); refs.navigationNodes.set(node.id, control);
        });
        section.appendChild(list); refs.navigationInner.appendChild(section);
      });
      if (visible.length < data.nodes.length) {
        var more = button("bp-dag-navigation-more", "Show " + Math.min(50, data.nodes.length - visible.length) + " more");
        more.addEventListener("click", function () { state.navigationLimit += 50; renderNavigation(); });
        refs.navigationInner.appendChild(more);
      }
      syncNavigationSelection();
    }

    function syncNavigationSelection() {
      if (!refs.navigationNodes) return;
      refs.navigationNodes.forEach(function (control, id) {
        if (state.selected && state.selected.id === id) control.setAttribute("aria-current", "true");
        else control.removeAttribute("aria-current");
      });
    }

    function bindEvents() {
      refs.search.addEventListener("focus", ensureGlobalSearch);
      refs.search.addEventListener("input", function () {
        state.query = refs.search.value.trim(); state.searchLimit = 12; state.listLimit = LIST_PAGE;
        if (state.mode === "list") closeSearch(); else renderSearch();
        renderInventory(true); if (state.query) ensureGlobalSearch();
      });
      refs.search.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { closeSearch(); return; }
        if (event.key === "ArrowDown") {
          if (openResults()) event.preventDefault();
        } else if (event.key === "Enter") {
          var matches = searchMatches(); if (matches.length) { event.preventDefault(); activateNode(matches[0].node, true); }
        }
      });
      refs.filterSummary.addEventListener("click", function () { setFilters(refs.filterPopover.hidden); });
      refs.filters.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !refs.filterPopover.hidden) { event.preventDefault(); setFilters(false); refs.filterSummary.focus(); }
      });
      document.addEventListener("pointerdown", function (event) {
        if (!refs.filters.contains(event.target)) setFilters(false);
        if (!refs.searchWrap.contains(event.target)) closeSearch();
      });
      refs.searchWrap.addEventListener("focusout", function (event) {
        if (event.relatedTarget && !refs.searchWrap.contains(event.relatedTarget)) closeSearch();
      });
      refs.isolated.addEventListener("change", function () {
        state.showIsolated = refs.isolated.checked; state.activeDirty = true;
        writeLocation("replace"); refresh(true);
      });
      refs.graphMode.addEventListener("click", function () { setMode("graph", true); });
      refs.listMode.addEventListener("click", function () { setMode("list", true); });
      refs.zoomIn.addEventListener("click", function () { zoomAt(1.24, refs.stage.clientWidth / 2, refs.stage.clientHeight / 2); });
      refs.zoomOut.addEventListener("click", function () { zoomAt(.8, refs.stage.clientWidth / 2, refs.stage.clientHeight / 2); });
      refs.fit.addEventListener("click", fit); refs.hiddenButton.addEventListener("click", revealAll);
      refs.loadMore.addEventListener("click", function () { state.listLimit += LIST_PAGE; renderInventory(false); });
      refs.wholeSearch.addEventListener("click", openResults);
      refs.sheetHandle.addEventListener("click", function () {
        setSheet(!host.classList.contains("bp-dag-sheet-open"), state.selected);
      });
      refs.inspector.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && state.selected) { event.preventDefault(); clearSelection(true, true); }
      });
      refs.stage.addEventListener("pointerdown", pointerDown); refs.stage.addEventListener("pointermove", pointerMove);
      refs.stage.addEventListener("pointerup", pointerUp); refs.stage.addEventListener("pointercancel", pointerUp);
      refs.stage.addEventListener("wheel", function (event) {
        var zoom = event.ctrlKey || event.metaKey, pixels = event.deltaMode === 1 ? 25 : 1;
        event.preventDefault();
        if (!zoom) { state.transform.x -= event.deltaX * pixels; state.transform.y -= event.deltaY * pixels; schedule(); return; }
        var rect = refs.stage.getBoundingClientRect();
        zoomAt(clamp(Math.exp(-event.deltaY * pixels * .002), .5, 2), event.clientX - rect.left, event.clientY - rect.top);
      }, {passive: false});
      refs.stage.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && state.selected) { clearSelection(true); event.preventDefault(); }
        else if (event.key === "+" || event.key === "=") { zoomAt(1.2, refs.stage.clientWidth / 2, refs.stage.clientHeight / 2); event.preventDefault(); }
        else if (event.key === "-") { zoomAt(.82, refs.stage.clientWidth / 2, refs.stage.clientHeight / 2); event.preventDefault(); }
        else if (event.key === "0") { fit(); event.preventDefault(); }
      });
      window.addEventListener("hashchange", applyLocation);
      window.addEventListener("popstate", applyLocation);
      document.addEventListener("keydown", function (event) {
        var target = event.target, own = target === refs.search;
        if (!own && (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName || "") || target.isContentEditable)) return;
        if ((event.key === "/" && !own) || ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k")) {
          event.preventDefault(); refs.search.focus(); refs.search.select();
        }
      });
      if (window.ResizeObserver) new ResizeObserver(resize).observe(refs.stage); else window.addEventListener("resize", resize);
      window.addEventListener("resize", syncSheetAccessibility);
      new MutationObserver(schedule).observe(document.body, {attributes: true, attributeFilter: ["data-md-color-scheme"]});
    }

    function statusLabel(key) { return palette[key] && palette[key].label ? palette[key].label : key.replace(/_/g, " "); }
    function swatch(key) {
      var entry = palette[key] || palette.planned || {}, mark = element("span", "bp-dag-swatch");
      mark.title = entry.meaning || statusLabel(key);
      ["light", "dark"].forEach(function (scheme) {
        var color = entry[scheme] || {};
        mark.style.setProperty("--dag-swatch-" + scheme + "-fill", color.fill || "transparent");
        mark.style.setProperty("--dag-swatch-" + scheme + "-stroke", color.stroke || "#9ca3af");
      });
      return mark;
    }
    function distribution(title, rows) {
      var section = element("section", "bp-dag-section"), list = element("div", "bp-dag-distribution");
      section.appendChild(element("h3", "", title));
      rows.forEach(function (row) {
        var line = element("div", "bp-dag-distribution-row"), name = element("span", "bp-dag-distribution-label");
        if (row[2]) name.appendChild(swatch(row[2]));
        // The label is flex, so the text goes in a span of its own and stays one item.
        name.appendChild(element("span", TEX, row[0])); line.appendChild(name);
        line.appendChild(element("span", "", String(row[1]))); list.appendChild(line);
      });
      section.appendChild(list); refs.inspectorInner.appendChild(section);
    }
    function isDark() { return document.body.getAttribute("data-md-color-scheme") === "slate"; }
    function colors(node) {
      var entry = palette[node.status] || palette.planned || {};
      return entry[isDark() ? "dark" : "light"] || {fill: "#fff", stroke: "#9ca3af", text: "#374151"};
    }
    function statusRing(node) {
      var entries = (node.status_counts || []).filter(function (entry) { return entry[1] > 0; });
      var total = entries.reduce(function (sum, entry) { return sum + entry[1]; }, 0), cursor = 0, stops = [];
      entries.forEach(function (entry) {
        var color = (palette[entry[0]] || palette.planned || {})[isDark() ? "dark" : "light"] || {};
        var end = cursor + entry[1] / Math.max(1, total) * 360;
        stops.push((color.stroke || "#9ca3af") + " " + cursor + "deg " + end + "deg"); cursor = end;
      });
      return stops.length ? "conic-gradient(" + stops.join(",") + ")" : "var(--dag-node-stroke)";
    }
    function world(node) {
      if (state.orientation === "tb" && !atlas) {
        if (node.isolated) {
          var mobileColumns = refs.stage.clientWidth < 540 ? 2 : 3;
          return {x: (node._isolateIndex % mobileColumns) * (NODE_W + 20),
            y: mobileIsolateBase + Math.floor(node._isolateIndex / mobileColumns) * (NODE_H + 22),
            w: node.width, h: node.height};
        }
        var row = Number.isFinite(node.row) ? node.row : Math.round(node.y / 66);
        var column = Number.isFinite(node.column) ? node.column : Math.round(node.x / 312);
        return {x: row * (NODE_W + 36), y: column * (NODE_H + 72), w: node.width, h: node.height};
      }
      return {x: node.x, y: node.y, w: node.width, h: node.height};
    }
    function screen(node) {
      var box = world(node), scale = state.transform.scale;
      return {x: box.x * scale + state.transform.x, y: box.y * scale + state.transform.y, w: box.w * scale, h: box.h * scale};
    }
    function inViewport(box, pad) {
      return box.x + box.w >= -pad && box.y + box.h >= -pad && box.x <= refs.stage.clientWidth + pad && box.y <= refs.stage.clientHeight + pad;
    }
    function refresh(resetList) {
      if (resetList) state.listLimit = LIST_PAGE;
      syncControls(); renderInspector(); renderInventory(resetList); schedule();
    }
    function syncControls() {
      calculateActive();
      syncNavigationSelection();
      refs.isolated.checked = state.showIsolated;
      refs.statusInputs.forEach(function (input, key) { input.checked = state.selectedStatuses.has(key); });
      var enabled = state.selectedStatuses.size;
      refs.filterSummary.textContent = (enabled === presentStatuses.length ? "Status" : "Status · " + enabled) +
        (state.showIsolated ? "" : " · edges only");
      refs.graphMode.setAttribute("aria-pressed", String(state.mode === "graph"));
      refs.listMode.setAttribute("aria-pressed", String(state.mode === "list"));
      refs.stage.hidden = state.mode !== "graph"; refs.inventory.hidden = state.mode !== "list";
    }

    function collect(start, adjacency) {
      var found = new Set([start]), pending = [start];
      while (pending.length) {
        (adjacency.get(pending.pop()) || []).forEach(function (id) {
          if (!found.has(id)) { found.add(id); pending.push(id); }
        });
      }
      return found;
    }
    function pathSpotlight() {
      if (!state.selected) return null;
      if (state.spotlight) return state.spotlight;
      var before = collect(state.selected.id, prerequisites), after = collect(state.selected.id, dependents);
      var nodes = new Set(before); after.forEach(function (id) { nodes.add(id); });
      state.spotlight = {after: after, before: before, nodes: nodes}; return state.spotlight;
    }
    function calculateActive() {
      if (!state.activeDirty) return;
      var directional = null;
      if (state.selected && state.direction === "prerequisites") directional = collect(state.selected.id, prerequisites);
      if (state.selected && state.direction === "dependents") directional = collect(state.selected.id, dependents);
      state.active = data.nodes.filter(function (node) {
        return state.selectedStatuses.has(node.status) && (state.showIsolated || !node.isolated || node === state.selected) &&
          (!directional || directional.has(node.id));
      });
      state.activeIds = new Set(state.active.map(function (node) { return node.id; }));
      var hidden = data.nodes.length - state.active.length;
      refs.hiddenButton.textContent = "Show " + hidden + " hidden"; refs.hiddenButton.hidden = !hidden;
      refs.empty.hidden = state.active.length !== 0;
      state.activeDirty = false;
    }

    function schedule() {
      if (scheduled || state.mode !== "graph") return;
      scheduled = true; requestAnimationFrame(function () { scheduled = false; draw(); });
    }
    function resize() {
      syncSheetAccessibility();
      if (intro && state.introInSheet !== sheetLayout()) {
        state.introInSheet = !state.introInSheet; renderNavigation(); if (!state.selected) renderInspector(); markIntro();
      }
      if (!refs.canvas || !refs.stage.clientWidth || !refs.stage.clientHeight) return;
      var ratio = Math.min(2, window.devicePixelRatio || 1);
      refs.canvas.width = Math.max(1, Math.round(refs.stage.clientWidth * ratio));
      refs.canvas.height = Math.max(1, Math.round(refs.stage.clientHeight * ratio));
      var nextOrientation = refs.stage.clientWidth <= 620 ? "tb" : "lr";
      var changed = nextOrientation !== state.orientation; state.orientation = nextOrientation;
      if (firstResize || changed) { firstResize = false; fit(); } else if (state.mode === "graph") draw();
    }
    function fit() {
      calculateActive(); var nodes = state.active.length ? state.active : data.nodes; if (!nodes.length) return;
      var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      nodes.forEach(function (node) {
        var box = world(node); minX = Math.min(minX, box.x); minY = Math.min(minY, box.y);
        maxX = Math.max(maxX, box.x + box.w); maxY = Math.max(maxY, box.y + box.h);
      });
      if (atlas) (data.regions || []).forEach(function (region) {
        minX = Math.min(minX, region.x); minY = Math.min(minY, region.y);
        maxX = Math.max(maxX, region.x + region.width); maxY = Math.max(maxY, region.y + region.height);
      });
      var pad = 54, width = Math.max(1, maxX - minX), height = Math.max(1, maxY - minY);
      var fitted = Math.min((refs.stage.clientWidth - pad * 2) / width,
        (refs.stage.clientHeight - pad * 2) / height);
      var readableFloor = atlas ? MIN_ATLAS_SCALE : MIN_READABLE_SCALE;
      state.readableCrop = fitted < readableFloor;
      state.transform.scale = clamp(Math.max(fitted, readableFloor), MIN_SCALE, 1.35);
      state.transform.x = state.readableCrop ? pad - minX * state.transform.scale :
        (refs.stage.clientWidth - width * state.transform.scale) / 2 - minX * state.transform.scale;
      state.transform.y = state.readableCrop ? pad - minY * state.transform.scale :
        (refs.stage.clientHeight - height * state.transform.scale) / 2 - minY * state.transform.scale;
      schedule();
    }
    // Open details cover part of the map; this is the larger part they leave, in stage pixels.
    function freeArea() {
      var band = refs.stage.clientHeight, left = 0, right = refs.stage.clientWidth, drawer = refs.inspector;
      if (host.classList.contains("bp-dag-sheet-open")) {
        var start = drawer.offsetLeft - refs.stage.parentNode.offsetLeft, end = start + drawer.offsetWidth;
        if (sheetLayout()) band = Math.max(NODE_H, Math.min(band, drawer.offsetTop));
        else if (start > right - end) right = Math.max(NODE_W, Math.min(right, start));
        else left = Math.min(Math.max(0, end), right - NODE_W);
      }
      return {band: band, left: left, right: right};
    }
    function center(node, minimumScale) {
      if (!node) return; var box = world(node), free = freeArea(); state.transform.scale = Math.max(state.transform.scale, minimumScale || .82);
      state.transform.x = (free.left + free.right) / 2 - (box.x + box.w / 2) * state.transform.scale;
      state.transform.y = free.band / 2 - (box.y + box.h / 2) * state.transform.scale; schedule();
    }
    // A click leaves the map where it is unless the details it opens cover the node; then it pans just clear.
    function reveal(node) {
      var box = screen(node), free = freeArea(), pad = 12;
      if (free.right < refs.stage.clientWidth && box.x + box.w > free.right) state.transform.x += free.right - pad - box.x - box.w;
      if (free.left > 0 && box.x < free.left) state.transform.x += free.left + pad - box.x;
      if (free.band < refs.stage.clientHeight && box.y + box.h > free.band) state.transform.y += free.band - pad - box.y - box.h;
      schedule();
    }
    function zoomAt(factor, x, y) {
      var old = state.transform.scale, next = clamp(old * factor, MIN_SCALE, 4);
      state.transform.x = x - (x - state.transform.x) * next / old;
      state.transform.y = y - (y - state.transform.y) * next / old; state.transform.scale = next; schedule();
    }

    function draw() {
      calculateActive(); var ctx = refs.ctx, ratio = Math.min(2, window.devicePixelRatio || 1);
      var width = refs.stage.clientWidth, height = refs.stage.clientHeight;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height); drawGrid(ctx, width, height);
      if (atlas) drawRegions(ctx);
      var visible = state.active.filter(function (node) { return inViewport(screen(node), 80); });
      var edgeIndexes = new Set();
      if (atlas && state.selected) {
        (incident.get(state.selected.id) || []).forEach(function (index) { edgeIndexes.add(index); });
      } else {
        visible.forEach(function (node) { (incident.get(node.id) || []).forEach(function (index) { edgeIndexes.add(index); }); });
      }
      if (atlas && !state.selected) edgeIndexes.clear();
      var spotlight = atlas && state.selected ? atlasSpotlight() : pathSpotlight();
      edgeIndexes.forEach(function (index) {
        var edge = data.edges[index]; if (!state.activeIds.has(edge.source) || !state.activeIds.has(edge.target)) return;
        var source = byId.get(edge.source), target = byId.get(edge.target); if (!source || !target) return;
        var a = screen(source), b = screen(target);
        if (!inViewport({x: Math.min(a.x, b.x), y: Math.min(a.y, b.y),
          w: Math.abs(b.x - a.x) + Math.max(a.w, b.w), h: Math.abs(b.y - a.y) + Math.max(a.h, b.h)}, 40)) return;
        var highlighted = !spotlight ||
          (spotlight.before.has(edge.source) && spotlight.before.has(edge.target)) ||
          (spotlight.after.has(edge.source) && spotlight.after.has(edge.target));
        if (edge.statement) drawEdge(ctx, a, b, false, highlighted, edge.statement, edge.proof ? -4 : 0);
        if (edge.proof) drawEdge(ctx, a, b, true, highlighted, edge.proof, edge.statement ? 4 : 0);
      });
      visible.forEach(function (node) { drawPlate(ctx, node, screen(node), !spotlight || spotlight.nodes.has(node.id)); });
      updateOverlays(visible, spotlight);
    }
    function drawRegions(ctx) {
      var regionColors = isDark() ?
        ["#17233a", "#24203b", "#16312d", "#332719", "#2d1f2a", "#1d2d3a", "#2c2b20"] :
        ["#eaf1fb", "#f0ebfb", "#e8f5f0", "#fbf2df", "#f8eaf0", "#e8f3f8", "#f4f2df"];
      (data.regions || []).forEach(function (region, index) {
        var box = {x: region.x * state.transform.scale + state.transform.x,
          y: region.y * state.transform.scale + state.transform.y,
          w: region.width * state.transform.scale, h: region.height * state.transform.scale};
        if (!inViewport(box, 60)) return;
        ctx.save(); ctx.fillStyle = regionColors[index % regionColors.length];
        ctx.strokeStyle = isDark() ? "#35435c" : "#b9c7da"; ctx.lineWidth = 1.2;
        rounded(ctx, box.x, box.y, box.w, box.h, 18); ctx.fill(); ctx.stroke();
        ctx.fillStyle = isDark() ? "#d9e3f2" : "#30445f"; ctx.font = "700 13px " + family;
        ctx.textAlign = "left"; ctx.fillText(region.label + " · " + region.count, box.x + 13, box.y + 21);
        ctx.restore();
      });
    }
    function atlasSpotlight() {
      var before = new Set([state.selected.id]), after = new Set([state.selected.id]);
      (prerequisites.get(state.selected.id) || []).forEach(function (id) { before.add(id); });
      (dependents.get(state.selected.id) || []).forEach(function (id) { after.add(id); });
      var nodes = new Set(before); after.forEach(function (id) { nodes.add(id); });
      return {after: after, before: before, nodes: nodes};
    }
    function drawGrid(ctx, width, height) {
      ctx.save(); ctx.fillStyle = isDark() ? "rgba(255,255,255,.055)" : "rgba(15,23,42,.055)";
      var step = 32, offsetX = ((state.transform.x % step) + step) % step, offsetY = ((state.transform.y % step) + step) % step;
      for (var x = offsetX; x < width; x += step) for (var y = offsetY; y < height; y += step) ctx.fillRect(x, y, 1, 1);
      ctx.restore();
    }
    function rounded(ctx, x, y, w, h, radius) {
      var r = Math.min(radius, w / 2, h / 2); ctx.beginPath(); ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y);
      ctx.quadraticCurveTo(x + w, y, x + w, y + r); ctx.lineTo(x + w, y + h - r);
      ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h); ctx.lineTo(x + r, y + h);
      ctx.quadraticCurveTo(x, y + h, x, y + h - r); ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y); ctx.closePath();
    }
    function drawPlate(ctx, node, box, highlighted) {
      var color = colors(node), tiny = state.transform.scale < .32;
      ctx.save(); ctx.globalAlpha = highlighted ? (tiny ? .82 : .24) : .07; ctx.fillStyle = color.fill;
      ctx.strokeStyle = color.stroke; ctx.lineWidth = tiny ? 1 : 1.25;
      if (atlas && node.kind === "scope") {
        rounded(ctx, box.x, box.y, box.w, box.h, 14 * state.transform.scale); ctx.fill(); ctx.stroke(); ctx.restore(); return;
      }
      if (tiny) ctx.fillRect(box.x, box.y, Math.max(3, box.w), Math.max(3, box.h));
      else { rounded(ctx, box.x, box.y, box.w, box.h, 7 * state.transform.scale); ctx.fill(); ctx.stroke(); }
      ctx.restore();
    }
    function drawEdge(ctx, a, b, dashed, highlighted, count, offset) {
      var horizontal = state.orientation === "lr";
      var ax = horizontal ? a.x + a.w : a.x + a.w / 2 + offset;
      var ay = horizontal ? a.y + a.h / 2 + offset : a.y + a.h;
      var bx = horizontal ? b.x : b.x + b.w / 2 + offset;
      var by = horizontal ? b.y + b.h / 2 + offset : b.y;
      ctx.save(); ctx.globalAlpha = highlighted ? .58 : .065; ctx.strokeStyle = isDark() ? "#a8b0bb" : "#64748b";
      ctx.fillStyle = ctx.strokeStyle; ctx.lineWidth = highlighted ? 1.35 : 1; if (dashed) ctx.setLineDash([5, 5]);
      ctx.beginPath(); ctx.moveTo(ax, ay);
      if (horizontal) { var mx = ax + (bx - ax) * .5; ctx.bezierCurveTo(mx, ay, mx, by, bx, by); }
      else { var my = ay + (by - ay) * .5; ctx.bezierCurveTo(ax, my, bx, my, bx, by); }
      ctx.stroke(); ctx.setLineDash([]);
      if (state.transform.scale > .18) {
        ctx.beginPath();
        if (horizontal) { ctx.moveTo(bx, by); ctx.lineTo(bx - 6, by - 3.5); ctx.lineTo(bx - 6, by + 3.5); }
        else { ctx.moveTo(bx, by); ctx.lineTo(bx - 3.5, by - 6); ctx.lineTo(bx + 3.5, by - 6); }
        ctx.closePath(); ctx.fill();
      }
      if (count > 1 && state.transform.scale > .32) {
        ctx.globalAlpha = highlighted ? .82 : .2; ctx.font = Math.max(11, 10 * state.transform.scale) + "px " + family;
        ctx.textAlign = "center"; ctx.fillText("×" + count, (ax + bx) / 2, (ay + by) / 2 - 4);
      }
      ctx.restore();
    }

    function updateOverlays(visible, spotlight) {
      var candidates = state.transform.scale >= .34 ? visible.slice() : [];
      if (state.selected && state.activeIds.has(state.selected.id) && candidates.indexOf(state.selected) < 0) candidates.push(state.selected);
      candidates.sort(function (a, b) {
        if (a === state.selected) return -1; if (b === state.selected) return 1;
        var ab = screen(a), bb = screen(b); return ab.y - bb.y || ab.x - bb.x || a._index - b._index;
      });
      var keep = new Set(candidates.map(function (node) { return node.id; }));
      overlays.forEach(function (control, id) { if (!keep.has(id)) { control.remove(); overlays.delete(id); } });
      candidates.forEach(function (node, index) {
        var control = overlays.get(node.id);
        if (!control) { control = makeNodeControl(node); overlays.set(node.id, control); refs.nodeLayer.appendChild(control); }
        var box = screen(node), color = colors(node);
        control.style.transform = "translate3d(" + box.x + "px," + box.y + "px,0) scale(" + state.transform.scale + ")";
        control.style.width = node.width + "px"; control.style.height = node.height + "px";
        control.style.setProperty("--dag-node-fill", color.fill); control.style.setProperty("--dag-node-stroke", color.stroke);
        control.style.setProperty("--dag-node-text", color.text); control.dataset.selected = String(node === state.selected);
        if (atlas && node.kind === "scope") control.style.setProperty("--dag-status-ring", statusRing(node));
        var relation = "";
        if (atlas && state.selected && node !== state.selected) {
          if ((prerequisites.get(state.selected.id) || []).indexOf(node.id) >= 0) relation = "prerequisite";
          else if ((dependents.get(state.selected.id) || []).indexOf(node.id) >= 0) relation = "dependent";
        }
        control.dataset.relation = relation;
        var relationLabel = control.querySelector(".bp-dag-node-relation");
        if (relationLabel) relationLabel.textContent = relation === "prerequisite" ? "Prerequisite" : relation === "dependent" ? "Dependent" : "";
        control.dataset.subdued = String(Boolean(spotlight && !spotlight.nodes.has(node.id)));
        control.tabIndex = node === state.selected || (!state.selected && index === 0) ? 0 : -1;
      });
      refs.density.hidden = !state.readableCrop && !(visible.length && state.transform.scale < .34);
      refs.density.textContent = state.readableCrop ? "Readable window · pan or Contents to see all " + state.active.length + " items" :
        (visible.length && state.transform.scale < .34 ? "Zoom in to reveal " + visible.length + " node labels" : "");
    }
    function makeNodeControl(node) {
      var control = button("bp-dag-node", ""); control.dataset.autoformNodeId = node.id; control.dataset.kind = node.kind || "node";
      var scopeMeta = node.kind === "scope" ? plural(node.member_count || 0, "item", "items") :
        node.kind === "boundary" ? "External scope · open" : null;
      control.setAttribute("aria-label", node.title + (node.area ? ", " + node.area : "") + ", " +
        (scopeMeta || statusLabel(node.status)) + (node.isolated ? ", no dependency edges in this view" : ""));
      var content = atlas && node.kind === "scope" ? element("span", "bp-dag-node-core") : control;
      content.appendChild(element("span", "bp-dag-node-title", node.title));
      content.appendChild(element("span", "bp-dag-node-meta", scopeMeta || statusLabel(node.status)));
      content.appendChild(element("span", "bp-dag-node-relation", ""));
      if (content !== control) control.appendChild(content);
      control.addEventListener("click", function (event) { event.stopPropagation(); activateNode(node, false); });
      control.addEventListener("dblclick", function () { var href = safeHref(node.url); if (href) window.location.href = href; });
      control.addEventListener("keydown", function (event) {
        if (/^Arrow(Left|Right|Up|Down)$/.test(event.key)) {
          var next = nearest(node, event.key);
          if (next) {
            event.preventDefault(); selectNode(next, state.selected ? "replace" : "push", false); center(next, .7);
            focusNodeControl(next);
          }
        } else if (event.key === "Escape") { event.preventDefault(); clearSelection(true); }
      });
      return control;
    }
    function nearest(node, key) {
      var from = world(node), fx = from.x + from.w / 2, fy = from.y + from.h / 2, best = null, bestScore = Infinity;
      state.active.forEach(function (candidate) {
        if (candidate === node) return;
        var box = world(candidate), dx = box.x + box.w / 2 - fx, dy = box.y + box.h / 2 - fy;
        if ((key === "ArrowLeft" && dx >= 0) || (key === "ArrowRight" && dx <= 0) ||
            (key === "ArrowUp" && dy >= 0) || (key === "ArrowDown" && dy <= 0)) return;
        var primary = key === "ArrowLeft" || key === "ArrowRight" ? Math.abs(dx) : Math.abs(dy);
        var cross = key === "ArrowLeft" || key === "ArrowRight" ? Math.abs(dy) : Math.abs(dx);
        var score = primary + cross * 2.4; if (score < bestScore) { bestScore = score; best = candidate; }
      });
      return best;
    }

    function pointerDown(event) {
      if (event.target.closest && event.target.closest(".bp-dag-node")) return;
      state.pointers.set(event.pointerId, {x: event.clientX, y: event.clientY, type: event.pointerType});
      state.moved = false; refs.stage.setPointerCapture(event.pointerId); refs.stage.dataset.dragging = "true";
      if (state.pointers.size === 2) state.pinch = pinchState();
    }
    function pinchState() {
      var points = Array.from(state.pointers.values()), dx = points[1].x - points[0].x, dy = points[1].y - points[0].y;
      return {distance: Math.max(1, Math.hypot(dx, dy)), x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2};
    }
    function pointerMove(event) {
      if (!state.pointers.has(event.pointerId)) return;
      var previous = state.pointers.get(event.pointerId); state.pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
      if (state.pointers.size === 1) {
        var dx = event.clientX - previous.x, dy = event.clientY - previous.y;
        if (Math.abs(dx) + Math.abs(dy) > 1) state.moved = true;
        state.transform.x += dx; state.transform.y += dy; schedule();
      } else if (state.pointers.size === 2) {
        var next = pinchState(), rect = refs.stage.getBoundingClientRect();
        state.transform.x += next.x - state.pinch.x; state.transform.y += next.y - state.pinch.y;
        zoomAt(next.distance / state.pinch.distance, next.x - rect.left, next.y - rect.top);
        state.pinch = next; state.moved = true;
      }
    }
    function pointerUp(event) {
      if (!state.pointers.has(event.pointerId)) return;
      var wasSingle = state.pointers.size === 1; state.pointers.delete(event.pointerId);
      if (state.pointers.size < 2) state.pinch = null;
      if (!state.pointers.size) refs.stage.dataset.dragging = "false";
      if (event.type === "pointerup" && wasSingle && !state.moved && state.selected) clearSelection(true);
    }

    function searchScore(node, query) {
      var title = node.title.toLocaleLowerCase(), id = node.id.toLocaleLowerCase();
      var lean = (node.lean || "").toLocaleLowerCase(), summary = (node.summary || "").toLocaleLowerCase();
      if (title === query) return 1; if (id === query) return 2; if (lean === query) return 3;
      if (title.startsWith(query)) return 4; if (id.startsWith(query)) return 5; if (lean.startsWith(query)) return 6;
      if (title.split(/\s+/).some(function (word) { return word.startsWith(query); })) return 7;
      if (title.indexOf(query) >= 0) return 8; if (id.indexOf(query) >= 0) return 9;
      if (lean.indexOf(query) >= 0) return 10; if (summary.indexOf(query) >= 0) return 11;
      var terms = query.split(/\s+/).filter(Boolean), all = [title, id, lean, summary].join(" ");
      return terms.length > 1 && terms.every(function (term) { return all.indexOf(term) >= 0; }) ? 12 : 0;
    }
    function ensureGlobalSearch() {
      var source = host.getAttribute("data-catalog-src");
      if (!source || state.searchPromise || state.searchNodes !== data.nodes) return state.searchPromise;
      state.searchLoading = true;
      state.searchPromise = fetch(source, {credentials: "same-origin"}).then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json().then(function (index) { return {index: index, url: response.url}; });
      }).then(function (loaded) {
        if (!loaded.index || loaded.index.schema !== "autoform-dag-search/v1" || !Array.isArray(loaded.index.nodes))
          throw new Error("unsupported search index");
        loaded.index.nodes.forEach(function (node, index) {
          node._index = index;
          if (node.url) node.url = new URL(node.url, loaded.url).href;
        });
        state.searchNodes = loaded.index.nodes; state.searchCacheQuery = null; state.searchError = null;
        refs.search.placeholder = "Search " + loaded.index.nodes.length.toLocaleString() + " roadmap entries";
      }).catch(function (error) {
        state.searchError = error.message;
      }).then(function () {
        state.searchLoading = false;
        if (refs.searchResults.hidden) return;
        var focused = document.activeElement, inside = refs.searchResults.contains(focused);
        var key = inside && (focused.dataset.resultId || "more");
        renderSearch();
        if (inside) (Array.from(refs.searchResults.querySelectorAll("button")).find(function (control) {
          return (control.dataset.resultId || "more") === key;
        }) || refs.search).focus();
      });
      return state.searchPromise;
    }
    function redirectLegacyFocus() {
      var requested = locationParams().get("node");
      if (!requested || byId.has(requested) || !host.getAttribute("data-catalog-src")) return;
      ensureGlobalSearch().then(function () {
        var target = state.searchNodes.find(function (node) { return node.id === requested; });
        var href = target && safeHref(target.url);
        if (href && new URL(href, window.location.href).href !== window.location.href) window.location.replace(href);
      });
    }
    function rankMatches(nodes, query) {
      return nodes.map(function (node) {
        var rank = searchScore(node, query); return rank ? {node: node, rank: rank} : null;
      }).filter(Boolean).sort(function (a, b) { return a.rank - b.rank || a.node._index - b.node._index; });
    }
    function searchMatches() {
      var query = state.query.toLocaleLowerCase(); if (!query) return [];
      if (query === state.searchCacheQuery) return state.searchCache;
      state.searchCacheQuery = query; state.searchCache = rankMatches(state.searchNodes, query);
      return state.searchCache;
    }
    function renderSearch() {
      refs.searchResults.textContent = ""; if (!state.query) { closeSearch(); return; }
      var matches = searchMatches(), list = element("ul", "bp-dag-results");
      matches.slice(0, state.searchLimit).forEach(function (match) {
        var item = element("li"), choose = button("bp-dag-result", ""); choose.dataset.resultId = match.node.id;
        choose.appendChild(element("span", "bp-dag-result-title", match.node.title));
        choose.appendChild(element("span", "bp-dag-result-meta", match.node.area || statusLabel(match.node.status)));
        choose.addEventListener("click", function () { activateNode(match.node, true); }); item.appendChild(choose); list.appendChild(item);
        choose.addEventListener("keydown", searchResultKeydown);
      });
      refs.searchResults.appendChild(list);
      if (matches.length > state.searchLimit) {
        var remaining = matches.length - state.searchLimit, increment = Math.min(SEARCH_PAGE, remaining);
        var more = button("bp-dag-load-more", "Show " + increment + " more of " + remaining);
        more.addEventListener("click", function () {
          var previous = state.searchLimit;
          state.searchLimit = Math.min(matches.length, state.searchLimit + SEARCH_PAGE); renderSearch();
          var results = refs.searchResults.querySelectorAll(".bp-dag-result");
          if (results[previous]) results[previous].focus();
        });
        more.addEventListener("keydown", searchResultKeydown);
        refs.searchResults.appendChild(more);
      }
      if (!matches.length) refs.searchResults.appendChild(element("p", "bp-dag-overview", "No matching title, statement, article id, or Lean name."));
      if (state.searchLoading) refs.searchResults.appendChild(element("p", "bp-dag-overview", "Searching the whole roadmap…"));
      else if (state.searchError) refs.searchResults.appendChild(element("p", "bp-dag-overview", "Whole-roadmap search is unavailable; showing this view."));
      refs.searchResults.hidden = false;
      refs.announcement.textContent = matches.length ? plural(matches.length, "result", "results") : "No results";
    }
    function searchResultKeydown(event) {
      if (event.key === "Escape") {
        event.preventDefault(); closeSearch(); refs.search.focus(); return;
      }
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      var controls = Array.from(refs.searchResults.querySelectorAll("button"));
      var index = controls.indexOf(event.currentTarget);
      var next = controls[index + (event.key === "ArrowDown" ? 1 : -1)];
      if (next) { event.preventDefault(); next.focus(); }
      else if (event.key === "ArrowUp") { event.preventDefault(); refs.search.focus(); }
    }
    function setFilters(open) { refs.filterPopover.hidden = !open; refs.filterSummary.setAttribute("aria-expanded", String(open)); }
    function closeSearch() { refs.searchResults.hidden = true; }
    function openResults() {
      if (refs.searchResults.hidden && state.query) renderSearch();
      var first = !refs.searchResults.hidden && refs.searchResults.querySelector("button");
      if (first) first.focus(); return Boolean(first);
    }
    function activateNode(node, moveCamera) {
      var local = byId.get(node.id); if (local) node = local;
      var href = safeHref(node.url);
      if (!local) { if (href) window.location.href = href; return; }
      if (node.kind === "boundary" && href) { window.location.href = href; return; }
      if (node.kind === "scope" && state.selected === node && href) { window.location.href = href; return; }
      chooseNode(node, moveCamera);
    }
    function chooseNode(node, moveCamera) {
      closeSearch(); if (!state.selectedStatuses.has(node.status)) state.selectedStatuses.add(node.status);
      if (node.isolated) state.showIsolated = true; state.activeDirty = true;
      state.mode = state.mapAllowed ? "graph" : "list";
      selectNode(node, "push", moveCamera && state.mapAllowed);
      if (state.mapAllowed) focusNodeControl(node);
      else requestAnimationFrame(function () {
        var target = refs.inspectorInner.querySelector(".bp-dag-open") || refs.inspectorInner.querySelector("a, button");
        if (target) target.focus();
      });
    }
    function focusNodeControl(node) {
      requestAnimationFrame(function () { requestAnimationFrame(function () {
        var control = overlays.get(node.id); if (control) control.focus(); else refs.sheetHandle.focus();
      }); });
    }

    function selectNode(node, historyMode, moveCamera) {
      if (!node) return; if (!state.selected || state.selected.id !== node.id) state.direction = "both";
      state.selected = node; state.activeDirty = true; state.spotlight = null;
      setSheet(true, node);
      syncControls(); renderInspector(); renderInventory(false);
      refs.announcement.textContent = "Selected " + node.title + ", " + statusLabel(node.status);
      if (historyMode) writeLocation(historyMode); if (moveCamera) center(node, .82); else reveal(node);
    }
    function clearSelection(updateHistory, returnFocus) {
      var refit = state.direction !== "both", cleared = state.selected;
      state.selected = null; state.direction = "both"; state.activeDirty = true; state.spotlight = null;
      setSheet(false, null);
      syncNavigationSelection(); renderInspector(); renderInventory(false); if (refit) requestAnimationFrame(fit); else schedule();
      if (updateHistory) writeLocation("push");
      var chosen = returnFocus && cleared && (state.mode === "list" ? refs.inventoryRows.get(cleared.id) : overlays.get(cleared.id));
      if (chosen) chosen.focus();
    }
    // From 681px to 1,100px details can open the head's first row; the map shifts back by it, so nothing jumps.
    // Measure both class changes together; callers need no selection-toggle ordering rule.
    function setSheet(open, selected) {
      var top = refs.body.offsetTop; host.classList.toggle("bp-dag-sheet-open", Boolean(open));
      host.classList.toggle("bp-dag-has-selection", Boolean(selected));
      state.transform.y += top - refs.body.offsetTop; schedule();
      refs.sheetHandle.setAttribute("aria-expanded", String(Boolean(open)));
      syncSheetAccessibility();
    }
    function sheetLayout() { return Boolean(window.matchMedia && window.matchMedia("(max-width: 680px)").matches); }
    // The collapsed sheet is inert, so phones name the page with a hidden heading.
    function markIntro() {
      if (!intro) return;
      if (introHeading) introHeading.setAttribute("aria-hidden", String(state.introInSheet));
      if (!refs.pageHeading) { refs.pageHeading = element("h1", "bp-dag-sr-only", introTitle); host.prepend(refs.pageHeading); }
      refs.pageHeading.hidden = !state.introInSheet;
    }
    function syncSheetAccessibility() {
      if (!refs.inspectorInner || !refs.sheetHandle) return;
      var collapsed = !host.classList.contains("bp-dag-sheet-open");
      if (collapsed) {
        if (refs.inspectorInner.contains(document.activeElement)) refs.sheetHandle.focus();
        refs.inspectorInner.setAttribute("inert", ""); refs.inspectorInner.setAttribute("aria-hidden", "true");
      } else {
        refs.inspectorInner.removeAttribute("inert"); refs.inspectorInner.removeAttribute("aria-hidden");
      }
    }
    function setDirection(direction) {
      state.direction = direction; state.activeDirty = true; writeLocation("push"); refresh(true); fit();
    }
    function revealAll() {
      state.selectedStatuses = new Set(presentStatuses); state.showIsolated = true; state.direction = "both";
      state.activeDirty = true; writeLocation("replace"); refresh(true); fit();
    }
    function onStatusChange() {
      state.selectedStatuses = new Set();
      refs.statusInputs.forEach(function (input, key) { if (input.checked) state.selectedStatuses.add(key); });
      if (state.selected && !state.selectedStatuses.has(state.selected.status)) {
        state.selected = null; state.direction = "both"; state.spotlight = null;
        setSheet(false, null);
        refs.announcement.textContent = "Selection cleared because its status is hidden.";
      }
      state.activeDirty = true; writeLocation("replace"); refresh(true);
    }
    function setMode(mode, updateHistory) {
      if (mode === "graph" && !state.mapAllowed) return;
      state.mode = mode; syncControls(); if (mode === "list") renderInventory(true); else schedule();
      if (updateHistory) writeLocation("replace");
    }

    function renderInspector() {
      typeset(); refs.inspectorInner.textContent = ""; refs.sheetHandle.textContent = state.selected ? state.selected.title : introTitle || "Graph details";
      // The handle repeats a node's title; the landing page's title it shows otherwise is printed as typed.
      refs.sheetHandle.classList.toggle(TEX, !!state.selected);
      if (!state.selected) {
        if (intro) { if (state.introInSheet) refs.inspectorInner.appendChild(intro); } else {
          refs.inspectorInner.appendChild(element("h2", TEX, data.title || "Dependencies"));
          if (view.summary) refs.inspectorInner.appendChild(element("p", "bp-dag-summary " + TEX, view.summary));
        }
        var overview = element("p", "bp-dag-overview"); overview.appendChild(element("strong", "", plural(data.nodes.length, atlas ? "topic" : "node", atlas ? "topics" : "nodes")));
        overview.appendChild(document.createTextNode(" · " + plural(data.edge_count || data.edges.length,
          "authored dependency", "authored dependencies") + ".")); refs.inspectorInner.appendChild(overview);
        var internalDependencies = data.nodes.reduce(function (total, node) {
          return total + (node.internal_dependency_count || 0);
        }, 0);
        if (internalDependencies) refs.inspectorInner.appendChild(element("p", "bp-dag-overview",
          plural(internalDependencies, "additional authored dependency lies", "additional authored dependencies lie") + " inside the visible topics."));
        if (atlas && !data.edges.length) refs.inspectorInner.appendChild(element("p", "bp-dag-overview",
          "No dependencies are authored between these topics."));
        if (atlas && (data.regions || []).length) {
          distribution("Mathematical regions", data.regions.map(function (region) { return [region.label, region.count]; }));
        }
        distribution(atlas ? "Topics by least-complete status" : "Nodes by status", presentStatuses.map(function (key) {
          return [statusLabel(key), statusCounts.get(key), key];
        }));
        if (statusCounts.has("inventory_checked") || statusCounts.has("inventory_pending")) refs.inspectorInner.appendChild(element("p", "bp-dag-overview",
          "Inventory statuses are neutral: an inventory records inspection, not a mathematical proof.")); return;
      }
      var node = state.selected, header = element("div", "bp-dag-inspector-header");
      if (node.declaration) header.appendChild(element("p", "bp-dag-kicker " + TEX, node.declaration + (node.area ? " · " + node.area : "")));
      header.appendChild(element("h2", TEX, node.title));
      var close = button("bp-dag-close", "×", "Close details"); close.addEventListener("click", function () {
        // Hiding the details takes focus out of them; it returns to the selection, not to the page.
        setSheet(false, state.selected);
        var chosen = state.selected && (state.mode === "list" ? refs.inventoryRows.get(state.selected.id) : overlays.get(state.selected.id));
        if (chosen) chosen.focus();
      });
      header.appendChild(close); refs.inspectorInner.appendChild(header);
      var inventoryOnly = node.kind === "scope" && (node.status_counts || []).length > 0 && node.status_counts.every(function (entry) {
        return palette[entry[0]] && palette[entry[0]].neutral;
      });
      var actions = element("div", "bp-dag-actions"), pill = element("span", "bp-dag-status-pill", inventoryOnly ? "inventory only" : statusLabel(node.status)), color = colors(node);
      pill.style.setProperty("--dag-pill-fill", color.fill); pill.style.setProperty("--dag-pill-stroke", color.stroke);
      actions.appendChild(pill); refs.inspectorInner.appendChild(actions);
      var href = safeHref(node.url);
      if (href) {
        var open = element("a", "bp-dag-open", node.kind === "scope" || node.kind === "boundary" ? "Explore topic" : "Open article");
        open.href = href; actions.appendChild(open);
      }
      if (node.lean) refs.inspectorInner.appendChild(element("p", "bp-dag-lean",
        "Lean: " + node.lean + (node.lean_more ? " and " + node.lean_more + " more" : "")));
      if (node.summary) refs.inspectorInner.appendChild(element("p", "bp-dag-summary " + TEX, node.summary));
      if (node.kind === "scope" && node.internal_dependency_count) refs.inspectorInner.appendChild(element("p", "bp-dag-overview",
        plural(node.internal_dependency_count, "authored dependency connects", "authored dependencies connect") + " results inside this topic."));
      var before = prerequisites.get(node.id) || [], after = dependents.get(node.id) || [];
      var path = pathSpotlight(), beforeCount = path ? path.before.size - 1 : 0;
      var afterCount = path ? path.after.size - 1 : 0;
      if (before.length || after.length) {
        var pivots = element("div", "bp-dag-pivots"); pivots.setAttribute("role", "group"); pivots.setAttribute("aria-label", "Paths");
        [["All", "both", 1], ["Prerequisites " + beforeCount, "prerequisites", beforeCount],
          ["Dependents " + afterCount, "dependents", afterCount]].forEach(function (entry) {
          var pivot = button("bp-dag-pivot", entry[0]); pivot.disabled = !entry[2];
          pivot.setAttribute("aria-pressed", String(state.direction === entry[1]));
          pivot.addEventListener("click", function () { setDirection(entry[1]); }); pivots.appendChild(pivot);
        });
        refs.inspectorInner.appendChild(pivots);
      }
      appendRelations("Direct prerequisites", before); appendRelations("Direct dependents", after);
      var entries = node.status_counts || [];
      if (entries.length > 1 || (node.member_count || 1) > 1) {
        distribution((node.member_count || 1) + " items in this scope", entries.map(function (entry) {
          return [statusLabel(entry[0]), entry[1], entry[0]];
        }));
      }
    }
    function appendRelations(label, ids) {
      if (!ids.length) return;
      var section = element("section", "bp-dag-section"); section.appendChild(element("h3", "", label + " (" + ids.length + ")"));
      var list = element("ul", "bp-dag-relation-list"), visible = 0;
      var more = button("bp-dag-load-more", "");
      function appendNextBatch() {
        var next = Math.min(visible + RELATION_PAGE, ids.length);
        ids.slice(visible, next).forEach(function (id) {
          var related = byId.get(id); if (!related) return;
          var item = element("li"), choose = button("bp-dag-relation-button " + TEX, related.title);
        choose.addEventListener("click", function () { activateNode(related, true); }); item.appendChild(choose); list.appendChild(item);
        });
        visible = next;
        more.hidden = visible >= ids.length;
        more.textContent = more.hidden ? "" : "Show " + Math.min(RELATION_PAGE, ids.length - visible) + " more";
      }
      more.addEventListener("click", function () { appendNextBatch(); typeset(); }); appendNextBatch();
      section.appendChild(list); section.appendChild(more); refs.inspectorInner.appendChild(section);
    }

    function inventoryNodes() {
      var query = state.query.toLocaleLowerCase();
      var nodes = query ? rankMatches(data.nodes, query).map(function (match) { return match.node; }) : data.nodes;
      return nodes.filter(function (node) { return state.selectedStatuses.has(node.status); });
    }
    function renderInventory(resetScroll) {
      if (!refs.inventoryList) return;
      var nodes = inventoryNodes(), visible = nodes.slice(0, state.listLimit); refs.inventoryList.textContent = "";
      refs.inventoryRows = new Map();
      visible.forEach(function (node) {
        var item = element("li"), row = button("bp-dag-inventory-row", "");
        if (node === state.selected) row.setAttribute("aria-current", "true");
        refs.inventoryRows.set(node.id, row);
        row.appendChild(element("span", "bp-dag-inventory-title", node.title));
        row.appendChild(element("span", "bp-dag-inventory-meta", statusLabel(node.status) + (node.area ? " · " + node.area : "")));
        row.addEventListener("click", function () { activateNode(node, true); }); item.appendChild(row); refs.inventoryList.appendChild(item);
      });
      // A count, or "N of M" while a page or a filter shows fewer.
      var shown = visible.length < nodes.length ? visible : nodes, total = shown === visible ? nodes : data.nodes;
      refs.inventorySummary.textContent = shown.length < total.length ? shown.length.toLocaleString() + " of " +
        total.length.toLocaleString() : plural(total.length, atlas ? "topic" : "item", atlas ? "topics" : "items");
      if (state.query && host.getAttribute("data-catalog-src")) {
        refs.inventorySummary.appendChild(document.createTextNode(" · ")); refs.inventorySummary.appendChild(refs.wholeSearch);
      }
      refs.loadMore.hidden = visible.length >= nodes.length;
      refs.loadMore.textContent = visible.length < nodes.length ? "Show " + Math.min(LIST_PAGE, nodes.length - visible.length) + " more" : "";
      var current = state.selected && refs.inventoryRows.get(state.selected.id);
      if (resetScroll && refs.inventoryScroll) refs.inventoryScroll.scrollTop = 0;
      else if (current) current.scrollIntoView({block: "nearest"});
    }
    function inventoryKey() { return state.mode + " " + Array.from(state.selectedStatuses).sort().join(); }

    function locationParams() { return new URLSearchParams(window.location.hash.replace(/^#/, "")); }
    function applyLocation() {
      var params = locationParams(), node = byId.get(params.get("node")) || null, direction = params.get("direction");
      var listed = inventoryKey();
      state.selected = node;
      state.direction = node && /^(prerequisites|dependents|both)$/.test(direction || "") ? direction : "both";
      var requested = params.get("status");
      if (requested === "-") state.selectedStatuses = new Set();
      else if (requested) state.selectedStatuses = new Set(requested.split(",").filter(function (key) {
        return presentStatuses.indexOf(key) >= 0;
      }));
      else state.selectedStatuses = new Set(presentStatuses);
      state.showIsolated = params.get("isolates") !== "0";
      state.mode = params.get("view") === "list" || !state.mapAllowed ? "list" : "graph";
      state.activeDirty = true; state.spotlight = null;
      if (node) {
        state.selectedStatuses.add(node.status);
        setSheet(true, node);
      } else setSheet(false, null);
      syncControls(); renderInspector(); renderInventory(inventoryKey() !== listed); schedule();
      if (node) setTimeout(function () { center(node, .82); }, 0);
    }
    function writeLocation(method) {
      var params = new URLSearchParams();
      if (state.selected) params.set("node", state.selected.id);
      if (state.selected && state.direction !== "both") params.set("direction", state.direction);
      if (state.selectedStatuses.size !== presentStatuses.length) params.set("status",
        state.selectedStatuses.size ? Array.from(state.selectedStatuses).join(",") : "-");
      if (!state.showIsolated) params.set("isolates", "0");
      if (state.mode === "list") params.set("view", "list");
      var hash = params.toString();
      var target = window.location.pathname + window.location.search + (hash ? "#" + hash : "");
      var current = window.location.pathname + window.location.search + window.location.hash;
      if (target === current) return;
      window.history[method + "State"](null, "", target);
    }

    syncControls();
  }

  function boot() { document.querySelectorAll(".bp-dag-viewer[data-graph-src]").forEach(start); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
"""


__all__ = [
    "MAX_MAP_NODES",
    "SCHEMA",
    "SCRIPT_PATH",
    "SEARCH_SCHEMA",
    "drawn_view",
    "render_container",
    "uses_atlas",
    "viewer_script",
    "write_payload",
    "write_search_index",
]
