"""Static payloads and the dependency-free Autoform graph explorer.

The graph is laid out once, deterministically, while rendering the site. The
browser paints topology on Canvas and places ordinary DOM controls over visible
nodes, so a large graph remains responsive without becoming a bitmap-only UI.
"""

from __future__ import annotations

import heapq
import html
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import quote

from .graph_views import INVENTORY_CHECKED_STATUS, GraphView
from .status import STATES

SCHEMA = "autoform-dag-view/v2"
MAX_LAYOUT_ROWS = 160
MAX_MERMAID_CHARACTERS = 40_000
MAX_MERMAID_EDGE_LINES = 450

_NODE_WIDTH = 196
_NODE_HEIGHT = 44
_COLUMN_GAP = 116
_ROW_GAP = 22


def requires_interactive(view: GraphView, mermaid_source: str) -> bool:
    """Retain the old threshold helper for third-party integrations."""
    edge_lines = sum(bool(edge.statement_count) + bool(edge.proof_count) for edge in view.edges)
    return len(mermaid_source) > MAX_MERMAID_CHARACTERS or edge_lines > MAX_MERMAID_EDGE_LINES


def _positions(view: GraphView) -> dict[str, dict[str, int]]:
    """Return stable, bounded-row coordinates without a browser force layout."""
    all_node_ids = [node.id for node in view.nodes]
    known = set(all_node_ids)
    if len(known) != len(all_node_ids):
        raise ValueError("dependency view contains duplicate node ids")
    connected: set[str] = set()
    for edge in view.edges:
        if edge.source not in known or edge.target not in known:
            raise ValueError("dependency view edge names an unknown node")
        if edge.source in known and edge.target in known and edge.source != edge.target:
            connected.update((edge.source, edge.target))

    # Isolates are searchable inventory, not visible dependency topology by
    # default. Lay out the connected graph without reserving thousands of
    # blank columns, then park isolates in a separate stable grid that appears
    # only when the reader asks for it.
    node_ids = [node_id for node_id in all_node_ids if node_id in connected]
    order_index = {node_id: index for index, node_id in enumerate(node_ids)}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    incoming: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    indegree = {node_id: 0 for node_id in node_ids}
    for edge in view.edges:
        if edge.source not in indegree or edge.target not in indegree or edge.source == edge.target:
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

    column_bases: dict[int, int] = {}
    next_column = 0
    for rank in sorted(by_rank):
        column_bases[rank] = next_column
        next_column += max(1, (len(by_rank[rank]) + MAX_LAYOUT_ROWS - 1) // MAX_LAYOUT_ROWS)

    result: dict[str, dict[str, int]] = {}
    for rank in sorted(by_rank):
        for slot, node_id in enumerate(by_rank[rank]):
            column = column_bases[rank] + slot // MAX_LAYOUT_ROWS
            row = slot % MAX_LAYOUT_ROWS
            result[node_id] = {
                "column": column,
                "height": _NODE_HEIGHT,
                "order": slot,
                "rank": rank,
                "row": row,
                "width": _NODE_WIDTH,
                "x": column * (_NODE_WIDTH + _COLUMN_GAP),
                "y": row * (_NODE_HEIGHT + _ROW_GAP),
            }

    isolated = [node_id for node_id in all_node_ids if node_id not in connected]
    isolate_base = max((position["column"] for position in result.values()), default=-2) + 2
    for slot, node_id in enumerate(isolated):
        column = isolate_base + slot // MAX_LAYOUT_ROWS
        row = slot % MAX_LAYOUT_ROWS
        result[node_id] = {
            "column": column,
            "height": _NODE_HEIGHT,
            "order": slot,
            "rank": 0,
            "row": row,
            "width": _NODE_WIDTH,
            "x": column * (_NODE_WIDTH + _COLUMN_GAP),
            "y": row * (_NODE_HEIGHT + _ROW_GAP),
        }
    return result


def _palette() -> dict[str, dict[str, object]]:
    palette: dict[str, dict[str, object]] = {
        state.key: {
            "label": state.label,
            "light": {"fill": state.fill, "stroke": state.stroke, "text": state.text},
            "dark": {"fill": state.dark_fill, "stroke": state.dark_stroke, "text": state.dark_text},
        }
        for state in STATES
    }
    planned = next(state for state in STATES if state.key == "planned")
    palette[INVENTORY_CHECKED_STATUS] = {
        "label": "inventory checked",
        "neutral": True,
        "light": {"fill": planned.fill, "stroke": planned.stroke, "text": planned.text},
        "dark": {"fill": planned.dark_fill, "stroke": planned.dark_stroke, "text": planned.dark_text},
    }
    return palette


def write_payload(path: Path, view: GraphView, *, links: Mapping[str, str]) -> Path:
    """Write deterministic v2 graph data while preserving the v1 call shape."""
    positions = _positions(view)
    degrees: dict[str, int] = {node.id: 0 for node in view.nodes}
    for edge in view.edges:
        if edge.source in degrees and edge.target in degrees:
            degrees[edge.source] += 1
            degrees[edge.target] += 1

    palette = _palette()
    nodes: list[dict[str, object]] = []
    present_statuses: set[str] = set()
    for node in view.nodes:
        inventory_checked = node.catalog == "module" and any(
            key == INVENTORY_CHECKED_STATUS and count for key, count in node.status_counts
        )
        status_key = (
            INVENTORY_CHECKED_STATUS
            if inventory_checked
            else node.status_key or (node.status_counts[0][0] if node.status_counts else "planned")
        )
        if status_key not in palette:
            status_key = "planned"
        present_statuses.add(status_key)
        layout = positions[node.id]
        nodes.append(
            {
                "catalog": node.catalog,
                "column": layout["column"],  # v1 readers can still place nodes.
                "declaration": node.declaration,
                "focus": node.focus,
                "id": node.id,
                "isolated": degrees[node.id] == 0,
                "kind": node.kind,
                "layout": layout,
                "member_count": len(node.members),
                "rank": layout["rank"],
                "row": layout["row"],
                "status": status_key,
                "status_counts": {key: count for key, count in node.status_counts},
                "title": node.title,
                "url": links.get(node.id),
            }
        )

    edges = [
        {"proof": edge.proof_count, "source": edge.source, "statement": edge.statement_count, "target": edge.target}
        for edge in view.edges
    ]
    dependency_count = sum(edge.dependency_count for edge in view.edges)
    isolated_count = sum(bool(node["isolated"]) for node in nodes)
    ordered_statuses = [state.key for state in STATES if state.key in present_statuses]
    if INVENTORY_CHECKED_STATUS in present_statuses:
        ordered_statuses.append(INVENTORY_CHECKED_STATUS)
    payload = {
        "counts": {
            "connected": len(nodes) - isolated_count,
            "dependencies": dependency_count,
            "isolated": isolated_count,
            "nodes": len(nodes),
        },
        "edge_count": dependency_count,
        "edges": edges,
        "node_count": len(nodes),
        "nodes": nodes,
        "palette": palette,
        "present_statuses": ordered_statuses,
        "schema": SCHEMA,
        "title": view.title,
        "view": {"focus": view.focus, "kind": view.kind, "radius": view.radius, "scope": view.scope},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def render_container(
    data_href: str,
    *,
    script_href: str | None = None,
    fallback_links: Iterable[tuple[str, str]] = (),
    fallback_total: int | None = None,
) -> str:
    """Return a self-contained, progressively enhanced explorer host."""
    href = html.escape(quote(data_href, safe="/%"), quote=True)
    script = (
        f'\n<script defer src="{html.escape(quote(script_href, safe="/%"), quote=True)}"></script>'
        if script_href
        else ""
    )
    fallback = tuple((label, target) for label, target in fallback_links if target)
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
        f'<div class="bp-dag-viewer" data-graph-src="{href}">\n'
        '  <p class="bp-dag-loading" role="status">Loading dependency explorer…</p>\n'
        f"{inventory}"
        f"</div>{script}"
    )


def viewer_script() -> str:
    """Return the dependency-free browser runtime with leading space folded."""
    # Keep the embedded source readable in Python without shipping indentation
    # that browsers do not need. Internal spaces and line breaks stay intact.
    lines = (line.lstrip() for line in _SCRIPT.splitlines())
    return "\n".join(lines) + "\n"


_STYLE = r"""
.bp-dag-viewer {
  --dag-border: var(--bp-rule, #d8dade); --dag-bg: var(--bp-surface, #fff);
  --dag-panel: color-mix(in srgb, var(--dag-bg) 94%, #64748b 6%);
  --dag-fg: var(--bp-fg, #1c1e21); --dag-muted: var(--bp-muted, #65676b); --dag-link: var(--bp-link, #0064e0);
  position: relative; display: grid; grid-template-rows: auto minmax(0, 1fr); width: 100%;
  height: clamp(38rem, calc(100svh - 7rem), 64rem); min-height: 38rem; overflow: hidden;
  border: 1px solid var(--dag-border); border-radius: 14px; background: var(--dag-bg); color: var(--dag-fg);
  box-shadow: 0 12px 36px rgba(15, 23, 42, .08); isolation: isolate;
  font-family: var(--md-text-font-family, ui-sans-serif, system-ui, sans-serif);
}
.bp-dag-viewer *, .bp-dag-viewer *::before, .bp-dag-viewer *::after { box-sizing: border-box; }
.bp-dag-head { z-index: 8; border-bottom: 1px solid var(--dag-border); background: var(--dag-bg); }
.bp-dag-context { display: flex; align-items: center; gap: .7rem; min-height: 2.65rem; padding: .45rem .75rem .2rem; }
.bp-dag-breadcrumb { min-width: 0; flex: 1; color: var(--dag-muted); font-size: .72rem; }
.bp-dag-breadcrumb ol { display: flex; align-items: center; gap: .35rem; margin: 0; padding: 0; list-style: none; }
.bp-dag-breadcrumb li { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bp-dag-breadcrumb li + li::before { content: "/"; margin-right: .35rem; color: var(--dag-border); }
.bp-dag-breadcrumb [aria-current=page] { color: var(--dag-fg); font-weight: 650; }
.bp-dag-view-kind { flex: none; padding: .18rem .45rem; border: 1px solid var(--dag-border); border-radius: 999px; color: var(--dag-muted); font-size: .64rem; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
.bp-dag-toolbar { display: flex; align-items: center; gap: .42rem; min-height: 3.35rem; padding: .35rem .65rem .6rem; font-size: .76rem; }
.bp-dag-search-wrap { position: relative; flex: 1 1 17rem; min-width: 10rem; max-width: 31rem; }
.bp-dag-search { width: 100%; min-height: 2.35rem; margin: 0; padding: .48rem .7rem .48rem 2rem; border: 1px solid var(--dag-border); border-radius: 9px; outline: 0; background: var(--dag-panel); color: var(--dag-fg); font: inherit; }
.bp-dag-search-wrap::before { content: "⌕"; position: absolute; z-index: 2; left: .7rem; top: .36rem; color: var(--dag-muted); font-size: 1rem; pointer-events: none; }
.bp-dag-search:focus { border-color: var(--dag-link); box-shadow: 0 0 0 3px color-mix(in srgb, var(--dag-link) 18%, transparent); }
.bp-dag-search-results { position: absolute; z-index: 20; top: calc(100% + .35rem); left: 0; right: 0; max-height: min(27rem, 60vh); overflow: auto; padding: .35rem; border: 1px solid var(--dag-border); border-radius: 10px; background: var(--dag-bg); box-shadow: 0 16px 38px rgba(15, 23, 42, .2); }
.bp-dag-search-results[hidden] { display: none; }
.bp-dag-results, .bp-dag-relation-list, .bp-dag-inventory-list { margin: 0; padding: 0; list-style: none; }
.bp-dag-result { display: grid; grid-template-columns: minmax(0, 1fr) auto; width: 100%; gap: .15rem .6rem; padding: .52rem .55rem; border: 0; border-radius: 7px; background: transparent; color: var(--dag-fg); text-align: left; cursor: pointer; }
.bp-dag-result:hover, .bp-dag-result:focus-visible { background: color-mix(in srgb, var(--dag-link) 9%, transparent); }
.bp-dag-result:focus-visible, .bp-dag-inventory-row:focus-visible { outline: 3px solid color-mix(in srgb, var(--dag-link) 52%, transparent); outline-offset: -3px; }
.bp-dag-result-title { overflow: hidden; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.bp-dag-result-id, .bp-dag-result-reason { color: var(--dag-muted); font-size: .66rem; }
.bp-dag-result-id { grid-column: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.bp-dag-result-reason { grid-column: 2; grid-row: 1 / span 2; align-self: center; }
.bp-dag-button, .bp-dag-filter-summary, .bp-dag-mode-button, .bp-dag-pivot, .bp-dag-hidden-button, .bp-dag-load-more { min-height: 2.25rem; padding: .38rem .58rem; border: 1px solid var(--dag-border); border-radius: 8px; background: var(--dag-bg); color: var(--dag-fg); font: inherit; font-weight: 650; cursor: pointer; }
.bp-dag-button:hover, .bp-dag-filter-summary:hover, .bp-dag-mode-button:hover, .bp-dag-pivot:hover, .bp-dag-load-more:hover { border-color: var(--dag-link); color: var(--dag-link); }
.bp-dag-button:focus-visible, .bp-dag-filter-summary:focus-visible, .bp-dag-mode-button:focus-visible, .bp-dag-pivot:focus-visible, .bp-dag-node:focus-visible, .bp-dag-sheet-handle:focus-visible { outline: 3px solid color-mix(in srgb, var(--dag-link) 45%, transparent); outline-offset: 2px; }
.bp-dag-zoom, .bp-dag-modes { display: inline-flex; flex: none; }
.bp-dag-zoom .bp-dag-button, .bp-dag-modes .bp-dag-mode-button { border-radius: 0; margin-left: -1px; }
.bp-dag-zoom :first-child, .bp-dag-modes :first-child { margin-left: 0; border-radius: 8px 0 0 8px; }
.bp-dag-zoom :last-child, .bp-dag-modes :last-child { border-radius: 0 8px 8px 0; }
.bp-dag-mode-button[aria-pressed=true], .bp-dag-pivot[aria-pressed=true] { border-color: var(--dag-link); background: color-mix(in srgb, var(--dag-link) 10%, var(--dag-bg)); color: var(--dag-link); }
.bp-dag-filters { position: relative; flex: none; }
.bp-dag-filter-summary { display: flex; align-items: center; list-style: none; white-space: nowrap; }
.bp-dag-filter-summary::-webkit-details-marker { display: none; }
.bp-dag-filter-popover { position: absolute; z-index: 18; top: calc(100% + .35rem); right: 0; width: 15rem; max-height: 23rem; overflow: auto; padding: .65rem; border: 1px solid var(--dag-border); border-radius: 10px; background: var(--dag-bg); box-shadow: 0 14px 32px rgba(15, 23, 42, .18); }
.bp-dag-filter-popover fieldset { display: grid; gap: .2rem; margin: 0; padding: 0; border: 0; }
.bp-dag-filter-popover legend { margin-bottom: .35rem; color: var(--dag-muted); font-size: .68rem; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
.bp-dag-check { display: flex; align-items: center; gap: .45rem; min-height: 2rem; padding: .2rem .3rem; border-radius: 6px; }
.bp-dag-check:hover { background: var(--dag-panel); }
.bp-dag-check input { accent-color: var(--dag-link); }
.bp-dag-check-count { margin-left: auto; color: var(--dag-muted); font-variant-numeric: tabular-nums; }
.bp-dag-isolated { display: inline-flex; align-items: center; gap: .3rem; flex: none; white-space: nowrap; color: var(--dag-muted); }
.bp-dag-stats { display: inline-flex; align-items: center; justify-content: flex-end; min-width: 9.5rem; margin-left: auto; color: var(--dag-muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
.bp-dag-hidden-button { min-height: 1.6rem; margin: 0; padding: .08rem .2rem; border: 0; background: transparent; color: var(--dag-link); }
.bp-dag-body { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) 20rem; min-height: 0; }
.bp-dag-main { position: relative; min-width: 0; min-height: 0; overflow: hidden; background: var(--dag-bg); }
.bp-dag-stage { position: absolute; inset: 0; overflow: hidden; touch-action: pan-y; cursor: grab; overscroll-behavior-x: contain; }
.bp-dag-stage[data-dragging=true] { cursor: grabbing; }
.bp-dag-canvas, .bp-dag-node-layer { position: absolute; inset: 0; width: 100%; height: 100%; }
.bp-dag-canvas { display: block; }
.bp-dag-node-layer { overflow: hidden; pointer-events: none; }
.bp-dag-node { position: absolute; top: 0; left: 0; display: grid; align-content: center; width: 196px; height: 44px; padding: 5px 9px; overflow: hidden; border: 1.5px solid var(--dag-node-stroke); border-radius: 8px; background: var(--dag-node-fill); color: var(--dag-node-text); box-shadow: 0 2px 5px rgba(15, 23, 42, .08); text-align: left; transform-origin: 0 0; pointer-events: auto; cursor: pointer; will-change: transform; }
.bp-dag-node-title { overflow: hidden; font-size: 12px; font-weight: 680; line-height: 1.18; text-overflow: ellipsis; white-space: nowrap; }
.bp-dag-node-meta { overflow: hidden; opacity: .75; font-size: 9px; line-height: 1.2; text-overflow: ellipsis; white-space: nowrap; }
.bp-dag-node[data-selected=true] { border-width: 3px; box-shadow: 0 0 0 3px color-mix(in srgb, var(--dag-link) 24%, transparent), 0 5px 13px rgba(15, 23, 42, .18); }
.bp-dag-node[data-subdued=true] { opacity: .23; }
.bp-dag-node[data-kind=scope] { border-radius: 13px; }
.bp-dag-node[data-kind=boundary] { border-style: dashed; }
.bp-dag-density, .bp-dag-empty { position: absolute; z-index: 4; left: 50%; border: 1px solid var(--dag-border); border-radius: 999px; background: color-mix(in srgb, var(--dag-bg) 92%, transparent); color: var(--dag-muted); box-shadow: 0 3px 12px rgba(15, 23, 42, .08); pointer-events: none; }
.bp-dag-density { bottom: .7rem; padding: .25rem .55rem; transform: translateX(-50%); font-size: .66rem; }
.bp-dag-empty { top: 50%; padding: .55rem .8rem; transform: translate(-50%, -50%); font-size: .74rem; }
.bp-dag-density[hidden], .bp-dag-empty[hidden] { display: none; }
.bp-dag-inventory { position: absolute; inset: 0; display: grid; grid-template-rows: auto minmax(0, 1fr) auto; overflow: hidden; background: var(--dag-bg); }
.bp-dag-inventory[hidden] { display: none; }
.bp-dag-inventory-head { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; padding: .75rem 1rem; border-bottom: 1px solid var(--dag-border); }
.bp-dag-inventory-head h2 { margin: 0; font-size: .95rem; }
.bp-dag-inventory-summary { color: var(--dag-muted); font-size: .7rem; }
.bp-dag-inventory-scroll { overflow: auto; padding: .45rem; }
.bp-dag-inventory-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; width: 100%; gap: .15rem .8rem; padding: .62rem .7rem; border: 0; border-bottom: 1px solid var(--dag-border); background: transparent; color: var(--dag-fg); text-align: left; cursor: pointer; }
.bp-dag-inventory-row:hover, .bp-dag-inventory-row:focus-visible { background: var(--dag-panel); }
.bp-dag-inventory-title { font-weight: 650; }
.bp-dag-inventory-id { grid-row: 2; overflow: hidden; color: var(--dag-muted); font: .68rem ui-monospace, SFMono-Regular, Consolas, monospace; text-overflow: ellipsis; white-space: nowrap; }
.bp-dag-inventory-meta { grid-column: 2; grid-row: 1 / span 2; align-self: center; color: var(--dag-muted); font-size: .68rem; }
.bp-dag-load-more { justify-self: center; margin: .55rem; }
.bp-dag-load-more[hidden] { display: none; }
.bp-dag-inspector { z-index: 7; min-width: 0; overflow: auto; border-left: 1px solid var(--dag-border); background: var(--dag-panel); }
.bp-dag-inspector-inner { padding: .9rem; }
.bp-dag-sheet-handle { display: none; }
.bp-dag-inspector-header { display: flex; align-items: flex-start; gap: .5rem; }
.bp-dag-inspector-header h2 { flex: 1; margin: 0; font-size: 1rem; line-height: 1.25; }
.bp-dag-close { min-width: 2rem; min-height: 2rem; padding: .2rem; border: 0; background: transparent; color: var(--dag-muted); cursor: pointer; }
.bp-dag-kicker { margin: 0 0 .35rem; color: var(--dag-muted); font-size: .64rem; font-weight: 750; letter-spacing: .06em; text-transform: uppercase; }
.bp-dag-node-id { display: block; margin: .45rem 0; overflow-wrap: anywhere; color: var(--dag-muted); font: .66rem ui-monospace, SFMono-Regular, Consolas, monospace; }
.bp-dag-status-pill { display: inline-flex; align-items: center; gap: .35rem; margin: .3rem 0 .65rem; color: var(--dag-muted); font-size: .7rem; }
.bp-dag-status-pill::before { content: ""; width: .58rem; height: .58rem; border: 2px solid var(--dag-pill-stroke); border-radius: 50%; background: var(--dag-pill-fill); }
.bp-dag-open { display: inline-flex; align-items: center; justify-content: center; min-height: 2.2rem; padding: .4rem .65rem; border-radius: 8px; background: var(--dag-link); color: #fff !important; font-size: .74rem; font-weight: 700; text-decoration: none !important; }
.bp-dag-overview { color: var(--dag-muted); font-size: .76rem; line-height: 1.55; }
.bp-dag-overview strong { color: var(--dag-fg); }
.bp-dag-section { margin-top: 1rem; padding-top: .8rem; border-top: 1px solid var(--dag-border); }
.bp-dag-section h3 { margin: 0 0 .45rem; font-size: .76rem; }
.bp-dag-pivots { display: grid; grid-template-columns: repeat(3, 1fr); gap: .3rem; }
.bp-dag-pivot { min-height: 2.7rem; padding: .3rem; font-size: .66rem; }
.bp-dag-relation-button { width: 100%; padding: .35rem .25rem; overflow: hidden; border: 0; background: transparent; color: var(--dag-link); font: inherit; font-size: .72rem; text-align: left; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
.bp-dag-relation-button:hover { text-decoration: underline; }
.bp-dag-distribution { display: grid; gap: .3rem; margin: .4rem 0 0; }
.bp-dag-distribution-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: .4rem; color: var(--dag-muted); font-size: .68rem; }
.bp-dag-loading, .bp-dag-error { margin: 0; padding: 1rem; }
.bp-dag-error { color: #b42318; }
.bp-dag-fallback { min-height: 0; overflow: auto; padding: 0 1rem 1rem; color: var(--dag-muted); }
.bp-dag-fallback p { max-width: 48rem; }
.bp-dag-fallback-list { display: grid; gap: .3rem; margin: .8rem 0; padding-left: 1.4rem; }
.bp-dag-viewer.bp-dag-enhancing .bp-dag-fallback { display: none; }
.bp-dag-sr-only { position: absolute !important; width: 1px !important; height: 1px !important; padding: 0 !important; overflow: hidden !important; clip: rect(0, 0, 0, 0) !important; white-space: nowrap !important; border: 0 !important; }
@media (max-width: 900px) {
  .bp-dag-viewer { height: max(40rem, calc(100svh - 4.5rem)); }
  .bp-dag-body { grid-template-columns: minmax(0, 1fr) 17rem; }
  .bp-dag-toolbar { flex-wrap: wrap; }
  .bp-dag-search-wrap { max-width: none; }
  .bp-dag-stats { order: 8; width: 100%; min-height: 1.4rem; margin: 0; justify-content: flex-start; }
}
@media (max-width: 680px) {
  .bp-dag-viewer { height: calc(100svh - 1rem); min-height: 34rem; border-radius: 10px; }
  .bp-dag-context { padding-inline: .55rem; }
  .bp-dag-toolbar { gap: .35rem; padding: .35rem .5rem .5rem; }
  .bp-dag-search-wrap { flex-basis: calc(100% - .4rem); order: -2; }
  .bp-dag-search, .bp-dag-button, .bp-dag-filter-summary, .bp-dag-mode-button { min-height: 2.75rem; }
  .bp-dag-isolated { min-height: 2.75rem; }
  .bp-dag-body { display: block; }
  .bp-dag-main { position: absolute; inset: 0; }
  .bp-dag-inspector { position: absolute; z-index: 12; right: .45rem; bottom: .45rem; left: .45rem; max-height: min(58%, 30rem); overflow: auto; border: 1px solid var(--dag-border); border-radius: 13px; box-shadow: 0 -8px 30px rgba(15, 23, 42, .2); transform: translateY(calc(100% - 3.2rem)); transition: transform .2s ease; }
  .bp-dag-viewer.bp-dag-sheet-open .bp-dag-inspector { transform: translateY(0); }
  .bp-dag-sheet-handle { display: block; position: sticky; z-index: 2; top: 0; width: 100%; min-height: 3.1rem; padding: .6rem 2.2rem .45rem .7rem; overflow: hidden; border: 0; border-bottom: 1px solid var(--dag-border); background: var(--dag-panel); color: var(--dag-fg); font: inherit; font-size: .75rem; font-weight: 700; text-align: left; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
  .bp-dag-sheet-handle::before { content: ""; position: absolute; top: .35rem; left: 50%; width: 2rem; height: 3px; border-radius: 2px; background: var(--dag-border); transform: translateX(-50%); }
  .bp-dag-inspector-inner { padding: .75rem; }
  .bp-dag-density { bottom: 3.7rem; }
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

  var NODE_W = 196, NODE_H = 44, MAX_DOM_NODES = 180, LIST_PAGE = 100;
  var SEARCH_PAGE = 50, RELATION_PAGE = 50, MIN_SCALE = .002, hostSequence = 0;
  var clamp = function (value, low, high) { return Math.max(low, Math.min(high, value)); };

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
    var instance = ++hostSequence;
    fetch(host.getAttribute("data-graph-src"), {credentials: "same-origin"}).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (data) { mount(host, data, instance); }).catch(function (error) {
      host.classList.remove("bp-dag-enhancing");
      var loading = host.querySelector(".bp-dag-loading");
      if (loading) {
        loading.textContent = "The interactive graph could not be loaded: " + error.message;
        loading.classList.add("bp-dag-error");
      }
    });
  }

  function mount(host, data, instance) {
    if (!data || data.schema !== "autoform-dag-view/v2" || !data.palette ||
        !Array.isArray(data.nodes) || !Array.isArray(data.edges)) throw new Error("unsupported graph payload");
    host.textContent = "";
    host.classList.remove("bp-dag-enhancing");
    var view = data.view || {kind: "full", scope: null};
    var palette = data.palette || {};
    var byId = new Map(), prerequisites = new Map(), dependents = new Map(), incident = new Map();
    var statusCounts = new Map(), presentStatuses = [];
    data.nodes.forEach(function (node, index) {
      var layout = node.layout || {};
      node._index = index;
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
    data.nodes.forEach(function (node) {
      node.isolated = node.isolated || ((prerequisites.get(node.id) || []).length + (dependents.get(node.id) || []).length === 0);
    });
    (data.present_statuses || Object.keys(palette)).forEach(function (key) {
      if (statusCounts.has(key) && presentStatuses.indexOf(key) < 0) presentStatuses.push(key);
    });
    statusCounts.forEach(function (_count, key) {
      if (presentStatuses.indexOf(key) < 0) presentStatuses.push(key);
    });

    var state = {
      active: [], activeDirty: true, activeIds: new Set(), direction: "both", listLimit: LIST_PAGE, mode: "graph", moved: false,
      orientation: "lr", pointers: new Map(), pinch: null, query: "", searchLimit: 12, selected: null,
      searchCache: [], searchCacheQuery: null, spotlight: null,
      selectedStatuses: new Set(presentStatuses), showIsolated: view.kind !== "full",
      transform: {x: 20, y: 20, scale: 1}
    };
    var refs = {}, overlays = new Map(), scheduled = false, firstResize = true;

    buildShell();
    bindEvents();
    applyLocation(true);
    renderInspector();
    renderInventory(true);
    resize();

    function buildShell() {
      var head = element("header", "bp-dag-head");
      var context = element("div", "bp-dag-context");
      var crumbs = element("nav", "bp-dag-breadcrumb");
      crumbs.setAttribute("aria-label", "Explorer context");
      var crumbList = element("ol");
      crumbList.appendChild(element("li", "", "Roadmap"));
      (view.scope ? String(view.scope).split("/").filter(Boolean) : []).forEach(function (part) {
        crumbList.appendChild(element("li", "", part.replace(/[-_]/g, " ")));
      });
      var current = element("li", "", view.kind === "project" ? "Project" : data.title || "Dependencies");
      current.setAttribute("aria-current", "page");
      crumbList.appendChild(current); crumbs.appendChild(crumbList); context.appendChild(crumbs);
      context.appendChild(element("span", "bp-dag-view-kind", view.kind || "graph")); head.appendChild(context);

      var toolbar = element("div", "bp-dag-toolbar");
      toolbar.setAttribute("role", "toolbar"); toolbar.setAttribute("aria-label", "Dependency explorer controls");
      var searchWrap = element("div", "bp-dag-search-wrap");
      refs.search = element("input", "bp-dag-search");
      refs.search.type = "search"; refs.search.placeholder = "Find a title or article id";
      refs.search.setAttribute("aria-label", "Search all graph nodes");
      refs.search.setAttribute("aria-expanded", "false");
      refs.search.setAttribute("aria-controls", "bp-dag-search-results-" + instance);
      refs.searchResults = element("div", "bp-dag-search-results"); refs.searchResults.id = "bp-dag-search-results-" + instance;
      refs.searchResults.setAttribute("role", "region"); refs.searchResults.setAttribute("aria-label", "Search results");
      refs.searchResults.hidden = true;
      searchWrap.appendChild(refs.search); searchWrap.appendChild(refs.searchResults); toolbar.appendChild(searchWrap);

      var filters = element("details", "bp-dag-filters");
      refs.filterSummary = element("summary", "bp-dag-filter-summary", "Status");
      var filterPopover = element("div", "bp-dag-filter-popover"), fieldset = element("fieldset");
      fieldset.appendChild(element("legend", "", "Statuses in this view")); refs.statusInputs = new Map();
      presentStatuses.forEach(function (key) {
        var label = element("label", "bp-dag-check"), input = element("input");
        input.type = "checkbox"; input.checked = true; input.value = key; input.addEventListener("change", onStatusChange);
        refs.statusInputs.set(key, input); label.appendChild(input); label.appendChild(document.createTextNode(statusLabel(key)));
        label.appendChild(element("span", "bp-dag-check-count", String(statusCounts.get(key) || 0))); fieldset.appendChild(label);
      });
      filterPopover.appendChild(fieldset); filters.appendChild(refs.filterSummary); filters.appendChild(filterPopover);
      toolbar.appendChild(filters);

      refs.isolatedLabel = element("label", "bp-dag-isolated"); refs.isolated = element("input");
      refs.isolated.type = "checkbox"; refs.isolated.checked = state.showIsolated; refs.isolatedLabel.appendChild(refs.isolated);
      refs.isolatedLabel.appendChild(document.createTextNode("Show isolated"));
      refs.isolatedLabel.hidden = !data.nodes.some(function (node) { return node.isolated; }); toolbar.appendChild(refs.isolatedLabel);

      var modes = element("div", "bp-dag-modes"); modes.setAttribute("role", "group"); modes.setAttribute("aria-label", "Explorer view");
      refs.graphMode = button("bp-dag-mode-button", "Graph"); refs.listMode = button("bp-dag-mode-button", "List");
      modes.appendChild(refs.graphMode); modes.appendChild(refs.listMode); toolbar.appendChild(modes);

      var zoomGroup = element("div", "bp-dag-zoom"); zoomGroup.setAttribute("role", "group"); zoomGroup.setAttribute("aria-label", "Zoom controls");
      refs.zoomOut = button("bp-dag-button", "−", "Zoom out"); refs.zoomIn = button("bp-dag-button", "+", "Zoom in");
      refs.fit = button("bp-dag-button", "Fit", "Fit shown nodes");
      zoomGroup.appendChild(refs.zoomOut); zoomGroup.appendChild(refs.zoomIn); zoomGroup.appendChild(refs.fit); toolbar.appendChild(zoomGroup);

      refs.stats = element("span", "bp-dag-stats"); refs.statsText = element("span"); refs.hiddenButton = button("bp-dag-hidden-button", "");
      refs.stats.appendChild(refs.statsText); refs.stats.appendChild(refs.hiddenButton); toolbar.appendChild(refs.stats);
      head.appendChild(toolbar); host.appendChild(head);

      var body = element("div", "bp-dag-body"), main = element("main", "bp-dag-main");
      refs.stage = element("section", "bp-dag-stage"); refs.stage.setAttribute("role", "region");
      refs.stage.setAttribute("aria-label", "Dependency graph. Drag with a mouse to pan, use Control or Command plus scroll to zoom, and use arrow keys between node buttons.");
      refs.canvas = element("canvas", "bp-dag-canvas"); refs.canvas.setAttribute("aria-hidden", "true");
      refs.nodeLayer = element("div", "bp-dag-node-layer"); refs.density = element("div", "bp-dag-density"); refs.density.hidden = true;
      refs.empty = element("div", "bp-dag-empty", "No nodes match these controls."); refs.empty.hidden = true;
      refs.stage.appendChild(refs.canvas); refs.stage.appendChild(refs.nodeLayer); refs.stage.appendChild(refs.density); refs.stage.appendChild(refs.empty);
      main.appendChild(refs.stage);

      refs.inventory = element("section", "bp-dag-inventory"); refs.inventory.hidden = true;
      refs.inventory.setAttribute("aria-label", "Complete node inventory");
      var inventoryHead = element("div", "bp-dag-inventory-head"); inventoryHead.appendChild(element("h2", "", "Node inventory"));
      refs.inventorySummary = element("span", "bp-dag-inventory-summary"); inventoryHead.appendChild(refs.inventorySummary);
      refs.inventoryScroll = element("div", "bp-dag-inventory-scroll"); refs.inventoryList = element("ul", "bp-dag-inventory-list");
      refs.inventoryScroll.appendChild(refs.inventoryList); refs.loadMore = button("bp-dag-load-more", "Show more nodes");
      refs.inventory.appendChild(inventoryHead); refs.inventory.appendChild(refs.inventoryScroll); refs.inventory.appendChild(refs.loadMore);
      main.appendChild(refs.inventory); body.appendChild(main);

      refs.inspector = element("aside", "bp-dag-inspector"); refs.inspector.setAttribute("aria-label", "Node inspector");
      refs.sheetHandle = button("bp-dag-sheet-handle", "Graph details", "Toggle graph details");
      refs.sheetHandle.setAttribute("aria-expanded", "false");
      refs.inspectorInner = element("div", "bp-dag-inspector-inner"); refs.inspector.appendChild(refs.sheetHandle);
      refs.inspector.appendChild(refs.inspectorInner); body.appendChild(refs.inspector); host.appendChild(body);
      refs.announcement = element("p", "bp-dag-sr-only"); refs.announcement.setAttribute("aria-live", "polite"); host.appendChild(refs.announcement);
      refs.ctx = refs.canvas.getContext("2d");
    }

    function bindEvents() {
      refs.search.addEventListener("input", function () {
        state.query = refs.search.value.trim(); state.searchLimit = 12; state.listLimit = LIST_PAGE;
        renderSearch(); renderInventory(true);
      });
      refs.search.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { closeSearch(); return; }
        if (event.key === "ArrowDown") {
          var first = refs.searchResults.querySelector("button"); if (first) { event.preventDefault(); first.focus(); }
        } else if (event.key === "Enter") {
          var matches = searchMatches(); if (matches.length) { event.preventDefault(); chooseNode(matches[0].node, true); }
        }
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
      refs.sheetHandle.addEventListener("click", function () {
        setSheet(!host.classList.contains("bp-dag-sheet-open"));
      });
      refs.stage.addEventListener("pointerdown", pointerDown); refs.stage.addEventListener("pointermove", pointerMove);
      refs.stage.addEventListener("pointerup", pointerUp); refs.stage.addEventListener("pointercancel", pointerUp);
      refs.stage.addEventListener("wheel", function (event) {
        if (!event.ctrlKey && !event.metaKey) return;
        event.preventDefault(); var rect = refs.stage.getBoundingClientRect();
        zoomAt(event.deltaY < 0 ? 1.13 : .885, event.clientX - rect.left, event.clientY - rect.top);
      }, {passive: false});
      refs.stage.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && state.selected) { clearSelection(true); event.preventDefault(); }
        else if (event.key === "+" || event.key === "=") { zoomAt(1.2, refs.stage.clientWidth / 2, refs.stage.clientHeight / 2); event.preventDefault(); }
        else if (event.key === "-") { zoomAt(.82, refs.stage.clientWidth / 2, refs.stage.clientHeight / 2); event.preventDefault(); }
        else if (event.key === "0") { fit(); event.preventDefault(); }
      });
      window.addEventListener("hashchange", function () { applyLocation(false); });
      window.addEventListener("popstate", function () { applyLocation(false); });
      if (window.ResizeObserver) new ResizeObserver(resize).observe(refs.stage); else window.addEventListener("resize", resize);
      new MutationObserver(schedule).observe(document.body, {attributes: true, attributeFilter: ["data-md-color-scheme"]});
    }

    function statusLabel(key) { return palette[key] && palette[key].label ? palette[key].label : key.replace(/_/g, " "); }
    function isDark() { return document.body.getAttribute("data-md-color-scheme") === "slate"; }
    function colors(node) {
      var entry = palette[node.status] || palette.planned || {};
      return entry[isDark() ? "dark" : "light"] || {fill: "#fff", stroke: "#9ca3af", text: "#374151"};
    }
    function world(node) {
      if (state.orientation === "tb") {
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
      refs.isolated.checked = state.showIsolated;
      refs.statusInputs.forEach(function (input, key) { input.checked = state.selectedStatuses.has(key); });
      var enabled = state.selectedStatuses.size;
      refs.filterSummary.textContent = enabled === presentStatuses.length ? "Status" : "Status · " + enabled;
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
      refs.statsText.textContent = state.active.length + " of " + data.nodes.length + " shown" + (hidden ? " / " : "");
      refs.hiddenButton.textContent = hidden ? "+" + hidden + " hidden" : ""; refs.hiddenButton.hidden = hidden === 0;
      refs.empty.hidden = state.active.length !== 0;
      state.activeDirty = false;
    }

    function schedule() {
      if (scheduled || state.mode !== "graph") return;
      scheduled = true; requestAnimationFrame(function () { scheduled = false; draw(); });
    }
    function resize() {
      if (!refs.canvas || !refs.stage.clientWidth || !refs.stage.clientHeight) return;
      var ratio = Math.min(2, window.devicePixelRatio || 1);
      refs.canvas.width = Math.max(1, Math.round(refs.stage.clientWidth * ratio));
      refs.canvas.height = Math.max(1, Math.round(refs.stage.clientHeight * ratio));
      var nextOrientation = refs.stage.clientWidth <= 620 ? "tb" : "lr";
      var changed = nextOrientation !== state.orientation; state.orientation = nextOrientation;
      if (firstResize || changed) { firstResize = false; fit(); } else schedule();
    }
    function fit() {
      calculateActive(); var nodes = state.active.length ? state.active : data.nodes; if (!nodes.length) return;
      var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      nodes.forEach(function (node) {
        var box = world(node); minX = Math.min(minX, box.x); minY = Math.min(minY, box.y);
        maxX = Math.max(maxX, box.x + box.w); maxY = Math.max(maxY, box.y + box.h);
      });
      var pad = 54, width = Math.max(1, maxX - minX), height = Math.max(1, maxY - minY);
      state.transform.scale = clamp(Math.min((refs.stage.clientWidth - pad * 2) / width,
        (refs.stage.clientHeight - pad * 2) / height), MIN_SCALE, 1.35);
      state.transform.x = (refs.stage.clientWidth - width * state.transform.scale) / 2 - minX * state.transform.scale;
      state.transform.y = (refs.stage.clientHeight - height * state.transform.scale) / 2 - minY * state.transform.scale; schedule();
    }
    function center(node, minimumScale) {
      if (!node) return; var box = world(node); state.transform.scale = Math.max(state.transform.scale, minimumScale || .82);
      state.transform.x = refs.stage.clientWidth / 2 - (box.x + box.w / 2) * state.transform.scale;
      state.transform.y = refs.stage.clientHeight / 2 - (box.y + box.h / 2) * state.transform.scale; schedule();
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
      var visible = state.active.filter(function (node) { return inViewport(screen(node), 80); });
      var edgeIndexes = new Set();
      visible.forEach(function (node) { (incident.get(node.id) || []).forEach(function (index) { edgeIndexes.add(index); }); });
      var spotlight = pathSpotlight();
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
        ctx.globalAlpha = highlighted ? .82 : .2; ctx.font = Math.max(9, 10 * state.transform.scale) + "px sans-serif";
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
      var omitted = Math.max(0, candidates.length - MAX_DOM_NODES); candidates = candidates.slice(0, MAX_DOM_NODES);
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
        control.dataset.subdued = String(Boolean(spotlight && !spotlight.nodes.has(node.id)));
        control.tabIndex = node === state.selected || (!state.selected && index === 0) ? 0 : -1;
      });
      refs.density.hidden = omitted === 0 && !(visible.length && state.transform.scale < .34);
      refs.density.textContent = omitted ? "+" + omitted + " node labels hidden at this zoom" :
        (visible.length && state.transform.scale < .34 ? "Zoom in to reveal " + visible.length + " node labels" : "");
    }
    function makeNodeControl(node) {
      var control = button("bp-dag-node", ""); control.dataset.autoformNodeId = node.id; control.dataset.kind = node.kind || "node";
      control.setAttribute("aria-label", node.title + ", " + statusLabel(node.status) + (node.isolated ? ", isolated" : ""));
      control.appendChild(element("span", "bp-dag-node-title", node.title));
      control.appendChild(element("span", "bp-dag-node-meta", statusLabel(node.status) + " · " + node.id));
      control.addEventListener("click", function (event) { event.stopPropagation(); selectNode(node, "push", false); });
      control.addEventListener("dblclick", function () { var href = safeHref(node.url); if (href) window.location.href = href; });
      control.addEventListener("keydown", function (event) {
        if (/^Arrow(Left|Right|Up|Down)$/.test(event.key)) {
          var next = nearest(node, event.key);
          if (next) {
            event.preventDefault(); center(next, .7); selectNode(next, "push", false);
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
      state.moved = false;
      if (event.pointerType !== "touch") {
        refs.stage.setPointerCapture(event.pointerId); refs.stage.dataset.dragging = "true";
      } else if (state.pointers.size === 2) {
        refs.stage.setPointerCapture(event.pointerId); refs.stage.dataset.dragging = "true";
      }
      if (state.pointers.size === 2) state.pinch = pinchState();
    }
    function pinchState() {
      var points = Array.from(state.pointers.values()), dx = points[1].x - points[0].x, dy = points[1].y - points[0].y;
      return {distance: Math.max(1, Math.hypot(dx, dy)), x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2};
    }
    function pointerMove(event) {
      if (!state.pointers.has(event.pointerId)) return;
      var previous = state.pointers.get(event.pointerId); state.pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
      if (event.pointerType === "touch" && state.pointers.size < 2) {
        if (Math.abs(event.clientX - previous.x) + Math.abs(event.clientY - previous.y) > 4) state.moved = true;
        return;
      }
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
      if (event.type === "pointerup" && wasSingle && !state.moved) clearSelection(true);
    }

    function searchScore(node, query) {
      var title = node.title.toLocaleLowerCase(), id = node.id.toLocaleLowerCase();
      if (title === query) return [0, "Exact title"]; if (id === query) return [1, "Exact id"];
      if (title.startsWith(query)) return [2, "Title prefix"]; if (id.startsWith(query)) return [3, "Id prefix"];
      if (title.split(/\s+/).some(function (word) { return word.startsWith(query); })) return [4, "Word prefix"];
      if (title.indexOf(query) >= 0) return [5, "Title match"]; if (id.indexOf(query) >= 0) return [6, "Id match"];
      return null;
    }
    function searchMatches() {
      var query = state.query.toLocaleLowerCase(); if (!query) return [];
      if (query === state.searchCacheQuery) return state.searchCache;
      state.searchCacheQuery = query;
      state.searchCache = data.nodes.map(function (node) {
        var score = searchScore(node, query); return score ? {node: node, rank: score[0], reason: score[1]} : null;
      }).filter(Boolean).sort(function (a, b) { return a.rank - b.rank || a.node._index - b.node._index; });
      return state.searchCache;
    }
    function renderSearch() {
      refs.searchResults.textContent = ""; if (!state.query) { closeSearch(); return; }
      var matches = searchMatches(), list = element("ul", "bp-dag-results");
      matches.slice(0, state.searchLimit).forEach(function (match) {
        var item = element("li"), choose = button("bp-dag-result", "");
        choose.appendChild(element("span", "bp-dag-result-title", match.node.title));
        choose.appendChild(element("span", "bp-dag-result-id", match.node.id));
        choose.appendChild(element("span", "bp-dag-result-reason", match.reason));
        choose.addEventListener("click", function () { chooseNode(match.node, true); }); item.appendChild(choose); list.appendChild(item);
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
      if (!matches.length) refs.searchResults.appendChild(element("p", "bp-dag-overview", "No matching title or article id."));
      refs.searchResults.hidden = false; refs.search.setAttribute("aria-expanded", "true");
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
    function closeSearch() { refs.searchResults.hidden = true; refs.search.setAttribute("aria-expanded", "false"); }
    function chooseNode(node, moveCamera) {
      closeSearch(); if (!state.selectedStatuses.has(node.status)) state.selectedStatuses.add(node.status);
      if (node.isolated) state.showIsolated = true; state.activeDirty = true;
      state.mode = "graph"; selectNode(node, "push", moveCamera);
      focusNodeControl(node);
    }
    function focusNodeControl(node) {
      requestAnimationFrame(function () { requestAnimationFrame(function () {
        var control = overlays.get(node.id); if (control) control.focus(); else refs.sheetHandle.focus();
      }); });
    }

    function selectNode(node, historyMode, moveCamera) {
      if (!node) return; if (!state.selected || state.selected.id !== node.id) state.direction = "both";
      state.selected = node; state.activeDirty = true; state.spotlight = null;
      host.classList.add("bp-dag-has-selection"); setSheet(true);
      syncControls(); renderInspector(); renderInventory(true);
      refs.announcement.textContent = "Selected " + node.title + ", " + statusLabel(node.status);
      if (historyMode) writeLocation(historyMode); if (moveCamera) center(node, .82); else schedule();
    }
    function clearSelection(updateHistory) {
      state.selected = null; state.direction = "both"; state.activeDirty = true; state.spotlight = null;
      host.classList.remove("bp-dag-has-selection");
      setSheet(false);
      renderInspector(); renderInventory(true); schedule(); if (updateHistory) writeLocation("push");
    }
    function setSheet(open) {
      host.classList.toggle("bp-dag-sheet-open", Boolean(open));
      refs.sheetHandle.setAttribute("aria-expanded", String(Boolean(open)));
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
      state.activeDirty = true; writeLocation("replace"); refresh(true);
    }
    function setMode(mode, updateHistory) {
      state.mode = mode; syncControls(); if (mode === "list") renderInventory(true); else schedule();
      if (updateHistory) writeLocation("replace");
    }

    function renderInspector() {
      refs.inspectorInner.textContent = ""; refs.sheetHandle.textContent = state.selected ? state.selected.title : "Graph details";
      if (!state.selected) {
        refs.inspectorInner.appendChild(element("p", "bp-dag-kicker", "Explorer overview"));
        refs.inspectorInner.appendChild(element("h2", "", data.title || "Dependencies"));
        var overview = element("p", "bp-dag-overview"); overview.appendChild(element("strong", "", data.nodes.length + " nodes"));
        overview.appendChild(document.createTextNode(" · " + (data.edge_count || data.edges.length) +
          " dependencies. Select a node to spotlight every path through it.")); refs.inspectorInner.appendChild(overview);
        if (statusCounts.has("inventory_checked")) refs.inspectorInner.appendChild(element("p", "bp-dag-overview",
          "Inventory checked is neutral: it records inspection, not a mathematical proof."));
        refs.inspectorInner.appendChild(element("p", "bp-dag-overview",
          "Canvas carries topology; Graph and List expose the same nodes as keyboard-accessible controls.")); return;
      }
      var node = state.selected, header = element("div", "bp-dag-inspector-header"), titleWrap = element("div");
      titleWrap.appendChild(element("p", "bp-dag-kicker", node.kind === "scope" ? "Scope" :
        node.kind === "boundary" ? "External scope" : (node.declaration || "Roadmap item")));
      titleWrap.appendChild(element("h2", "", node.title)); header.appendChild(titleWrap);
      var close = button("bp-dag-close", "×", "Clear node selection"); close.addEventListener("click", function () { clearSelection(true); });
      header.appendChild(close); refs.inspectorInner.appendChild(header); refs.inspectorInner.appendChild(element("code", "bp-dag-node-id", node.id));
      var pill = element("span", "bp-dag-status-pill", statusLabel(node.status)), color = colors(node);
      pill.style.setProperty("--dag-pill-fill", color.fill); pill.style.setProperty("--dag-pill-stroke", color.stroke);
      refs.inspectorInner.appendChild(pill);
      var href = safeHref(node.url);
      if (href) {
        var open = element("a", "bp-dag-open", node.kind === "scope" || node.kind === "boundary" ? "Open scope" : "Open article");
        open.href = href; refs.inspectorInner.appendChild(open);
      }
      var before = prerequisites.get(node.id) || [], after = dependents.get(node.id) || [];
      var path = pathSpotlight(), beforeCount = path ? path.before.size - 1 : 0;
      var afterCount = path ? path.after.size - 1 : 0;
      var paths = element("section", "bp-dag-section"); paths.appendChild(element("h3", "", "Path spotlight"));
      var pivots = element("div", "bp-dag-pivots");
      [["Prerequisites " + beforeCount, "prerequisites", beforeCount], ["All paths", "both", 1],
        ["Dependents " + afterCount, "dependents", afterCount]].forEach(function (entry) {
        var pivot = button("bp-dag-pivot", entry[0]); pivot.disabled = !entry[2];
        pivot.setAttribute("aria-pressed", String(state.direction === entry[1]));
        pivot.addEventListener("click", function () { setDirection(entry[1]); }); pivots.appendChild(pivot);
      });
      paths.appendChild(pivots); refs.inspectorInner.appendChild(paths);
      appendRelations("Direct prerequisites", before); appendRelations("Direct dependents", after);
      var counts = node.status_counts || {};
      var entries = Array.isArray(counts) ? counts : Object.keys(counts).map(function (key) { return [key, counts[key]]; });
      if (entries.length > 1 || (node.member_count || 1) > 1) {
        var distribution = element("section", "bp-dag-section");
        distribution.appendChild(element("h3", "", (node.member_count || 1) + " items in this scope"));
        var rows = element("div", "bp-dag-distribution");
        entries.forEach(function (entry) {
          var row = element("div", "bp-dag-distribution-row"); row.appendChild(element("span", "", statusLabel(entry[0])));
          row.appendChild(element("span", "", String(entry[1]))); rows.appendChild(row);
        });
        distribution.appendChild(rows); refs.inspectorInner.appendChild(distribution);
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
          var item = element("li"), choose = button("bp-dag-relation-button", related.title);
          choose.addEventListener("click", function () { chooseNode(related, true); }); item.appendChild(choose); list.appendChild(item);
        });
        visible = next;
        more.hidden = visible >= ids.length;
        more.textContent = more.hidden ? "" : "Show " + Math.min(RELATION_PAGE, ids.length - visible) + " more";
      }
      more.addEventListener("click", appendNextBatch); appendNextBatch();
      section.appendChild(list); section.appendChild(more); refs.inspectorInner.appendChild(section);
    }

    function inventoryNodes() {
      var nodes = data.nodes.filter(function (node) { return state.selectedStatuses.has(node.status); });
      if (state.query) return searchMatches().map(function (match) { return match.node; }).filter(function (node) {
        return state.selectedStatuses.has(node.status);
      });
      return nodes;
    }
    function renderInventory(resetScroll) {
      if (!refs.inventoryList) return;
      var nodes = inventoryNodes(), visible = nodes.slice(0, state.listLimit); refs.inventoryList.textContent = "";
      visible.forEach(function (node) {
        var item = element("li"), row = button("bp-dag-inventory-row", "");
        row.appendChild(element("span", "bp-dag-inventory-title", node.title));
        row.appendChild(element("span", "bp-dag-inventory-id", node.id));
        row.appendChild(element("span", "bp-dag-inventory-meta", statusLabel(node.status) + (node.isolated ? " · isolated" : "")));
        row.addEventListener("click", function () { chooseNode(node, true); }); item.appendChild(row); refs.inventoryList.appendChild(item);
      });
      refs.inventorySummary.textContent = visible.length + " of " + nodes.length + " listed · isolates included";
      refs.loadMore.hidden = visible.length >= nodes.length;
      refs.loadMore.textContent = visible.length < nodes.length ? "Show " + Math.min(LIST_PAGE, nodes.length - visible.length) + " more" : "";
      if (resetScroll && refs.inventoryScroll) refs.inventoryScroll.scrollTop = 0;
    }

    function locationParams() { return new URLSearchParams(window.location.hash.replace(/^#/, "")); }
    function applyLocation(initial) {
      var params = locationParams(), node = byId.get(params.get("node")) || null, direction = params.get("direction");
      state.selected = node;
      state.direction = node && /^(prerequisites|dependents|both)$/.test(direction || "") ? direction : "both";
      var requested = params.get("status");
      if (requested === "-") state.selectedStatuses = new Set();
      else if (requested) state.selectedStatuses = new Set(requested.split(",").filter(function (key) {
        return presentStatuses.indexOf(key) >= 0;
      }));
      else state.selectedStatuses = new Set(presentStatuses);
      state.showIsolated = params.has("isolates") ? params.get("isolates") === "1" : view.kind !== "full";
      state.mode = params.get("view") === "list" ? "list" : "graph";
      state.activeDirty = true; state.spotlight = null;
      if (node) {
        state.selectedStatuses.add(node.status); if (node.isolated && !params.has("isolates")) state.showIsolated = true;
        host.classList.add("bp-dag-has-selection"); setSheet(true);
      } else { host.classList.remove("bp-dag-has-selection"); setSheet(false); }
      syncControls(); renderInspector(); renderInventory(true); schedule();
      if (node) setTimeout(function () { center(node, .82); }, initial ? 0 : 0);
    }
    function writeLocation(method) {
      var params = new URLSearchParams();
      if (state.selected) params.set("node", state.selected.id);
      if (state.selected && state.direction !== "both") params.set("direction", state.direction);
      if (state.selectedStatuses.size !== presentStatuses.length) params.set("status",
        state.selectedStatuses.size ? Array.from(state.selectedStatuses).join(",") : "-");
      var defaultIsolates = view.kind !== "full";
      if (state.showIsolated !== defaultIsolates) params.set("isolates", state.showIsolated ? "1" : "0");
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
    "MAX_LAYOUT_ROWS",
    "MAX_MERMAID_CHARACTERS",
    "MAX_MERMAID_EDGE_LINES",
    "SCHEMA",
    "render_container",
    "requires_interactive",
    "viewer_script",
    "write_payload",
]
