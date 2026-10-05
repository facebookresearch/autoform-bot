"""Static data and a virtualized Canvas viewer for large dependency DAGs."""

from __future__ import annotations

import html
import json
from collections import defaultdict
from collections.abc import Mapping
from heapq import heapify, heappop, heappush
from pathlib import Path
from urllib.parse import quote

from .graph_views import GraphView, INVENTORY_CHECKED_STATUS
from .status import STATES


SCHEMA = "autoform-dag-view/v1"
MAX_LAYOUT_ROWS = 160
MAX_MERMAID_CHARACTERS = 40_000
MAX_MERMAID_EDGE_LINES = 450


def requires_interactive(view: GraphView, mermaid_source: str) -> bool:
    """Fail over before Mermaid's hard parser and edge limits are reached."""
    edge_lines = sum(bool(edge.statement_count) + bool(edge.proof_count) for edge in view.edges)
    return len(mermaid_source) > MAX_MERMAID_CHARACTERS or edge_lines > MAX_MERMAID_EDGE_LINES


def write_payload(
    path: Path,
    view: GraphView,
    *,
    links: Mapping[str, str],
) -> Path:
    """Write deterministic, layout-ready graph data for the browser viewer."""
    order = {node.id: index for index, node in enumerate(view.nodes)}
    if len(order) != len(view.nodes):
        raise ValueError("dependency view contains duplicate node ids")
    incoming: dict[str, set[str]] = {node_id: set() for node_id in order}
    outgoing: dict[str, set[str]] = {node_id: set() for node_id in order}
    for edge in view.edges:
        if edge.source not in order or edge.target not in order:
            raise ValueError("dependency view edge names an unknown node")
        incoming[edge.target].add(edge.source)
        outgoing[edge.source].add(edge.target)

    remaining = {node_id: set(sources) for node_id, sources in incoming.items()}
    ranks: dict[str, int] = {}
    ready = [(order[node_id], node_id) for node_id, sources in remaining.items() if not sources]
    heapify(ready)
    while ready:
        _position, node_id = heappop(ready)
        ranks[node_id] = max((ranks[source] + 1 for source in incoming[node_id]), default=0)
        for target in sorted(outgoing[node_id], key=order.__getitem__):
            remaining[target].discard(node_id)
            if not remaining[target]:
                heappush(ready, (order[target], target))
    if len(ranks) != len(view.nodes):
        raise ValueError("dependency view contains a cycle")

    slots: dict[int, int] = defaultdict(int)
    nodes = []
    for node in view.nodes:
        rank = ranks[node.id]
        slot = slots[rank]
        slots[rank] += 1
        inventory_checked = (
            node.catalog == "module"
            and any(
                key == INVENTORY_CHECKED_STATUS and count
                for key, count in node.status_counts
            )
        )
        status_key = (
            INVENTORY_CHECKED_STATUS
            if inventory_checked
            else node.status_key
            or (node.status_counts[0][0] if node.status_counts else "planned")
        )
        nodes.append(
            {
                "catalog": node.catalog,
                "declaration": node.declaration,
                "id": node.id,
                "kind": node.kind,
                "rank": rank,
                "slot": slot,
                "status": status_key,
                "title": node.title,
                "url": links.get(node.id),
            }
        )

    column_bases: dict[int, int] = {}
    next_column = 0
    for rank in sorted(slots):
        column_bases[rank] = next_column
        next_column += max(1, (slots[rank] + MAX_LAYOUT_ROWS - 1) // MAX_LAYOUT_ROWS)
    for node in nodes:
        slot = node.pop("slot")
        node["column"] = column_bases[node["rank"]] + slot // MAX_LAYOUT_ROWS
        node["row"] = slot % MAX_LAYOUT_ROWS

    palette = {
        state.key: {
            "label": state.label,
            "light": {"fill": state.fill, "stroke": state.stroke, "text": state.text},
            "dark": {
                "fill": state.dark_fill,
                "stroke": state.dark_stroke,
                "text": state.dark_text,
            },
        }
        for state in STATES
    }
    planned = next(state for state in STATES if state.key == "planned")
    palette[INVENTORY_CHECKED_STATUS] = {
        "label": "inventory checked",
        "light": {
            "fill": planned.fill,
            "stroke": planned.stroke,
            "text": planned.text,
        },
        "dark": {
            "fill": planned.dark_fill,
            "stroke": planned.dark_stroke,
            "text": planned.dark_text,
        },
    }
    payload = {
        "edge_count": sum(edge.dependency_count for edge in view.edges),
        "edges": [
            {
                "proof": edge.proof_count,
                "source": edge.source,
                "statement": edge.statement_count,
                "target": edge.target,
            }
            for edge in view.edges
        ],
        "node_count": len(view.nodes),
        "nodes": nodes,
        "palette": palette,
        "schema": SCHEMA,
        "title": view.title,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def render_container(data_href: str, *, script_href: str | None = None) -> str:
    """Return the progressively enhanced host for the full-DAG viewer."""
    href = html.escape(quote(data_href, safe="/"), quote=True)
    script = (
        f'\n<script defer src="{html.escape(quote(script_href, safe="/%"), quote=True)}"></script>'
        if script_href
        else ""
    )
    return (
        f'<div class="bp-dag-viewer" data-graph-src="{href}">\n'
        '  <p class="bp-dag-loading" role="status">Loading the interactive dependency graph…</p>\n'
        '  <noscript>The full graph requires JavaScript. Use the project, chapter, and local maps instead.</noscript>\n'
        f"</div>{script}"
    )


def viewer_script() -> str:
    """Return a dependency-free Canvas viewer for large static DAGs."""
    return _SCRIPT


_SCRIPT = r'''/* Generated by autoform render. Edits are overwritten. */
(function () {
  "use strict";
  if (window.__AUTOFORM_DAG_VIEWER__) return;
  window.__AUTOFORM_DAG_VIEWER__ = true;
  var NODE_W = 188, NODE_H = 28, GAP_X = 84, GAP_Y = 12;
  var clamp = function (value, low, high) { return Math.max(low, Math.min(high, value)); };

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function start(host) {
    var source = host.getAttribute("data-graph-src");
    fetch(source, {credentials: "same-origin"}).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (data) { mount(host, data); }).catch(function (error) {
      host.textContent = "The full dependency graph could not be loaded: " + error.message;
      host.classList.add("bp-dag-error");
    });
  }

  function mount(host, data) {
    if (!data || data.schema !== "autoform-dag-view/v1" ||
        !Array.isArray(data.nodes) || !Array.isArray(data.edges) || !data.palette) {
      throw new Error("unsupported dependency graph data");
    }
    host.textContent = "";
    var toolbar = element("div", "bp-dag-toolbar");
    toolbar.setAttribute("role", "toolbar");
    toolbar.setAttribute("aria-label", "Dependency graph controls");
    var searchLabel = element("label", "bp-dag-search-label", "Search ");
    var search = element("input", "bp-dag-search");
    search.type = "search";
    search.placeholder = "title or article id";
    search.setAttribute("aria-label", "Search graph nodes");
    searchLabel.appendChild(search);
    toolbar.appendChild(searchLabel);

    var status = element("select", "bp-dag-status");
    status.setAttribute("aria-label", "Filter by formalization status");
    var all = element("option", "", "All statuses");
    all.value = "";
    status.appendChild(all);
    Object.keys(data.palette).forEach(function (key) {
      var option = element("option", "", data.palette[key].label);
      option.value = key;
      status.appendChild(option);
    });
    toolbar.appendChild(status);

    var proofLabel = element("label", "bp-dag-proof-label");
    var proof = element("input");
    proof.type = "checkbox";
    proof.checked = true;
    proofLabel.appendChild(proof);
    proofLabel.appendChild(document.createTextNode(" proof-only edges"));
    toolbar.appendChild(proofLabel);
    var neighborhoodLabel = element("label", "bp-dag-proof-label");
    var neighborhood = element("input");
    neighborhood.type = "checkbox";
    neighborhoodLabel.appendChild(neighborhood);
    neighborhoodLabel.appendChild(document.createTextNode(" selected neighborhood"));
    toolbar.appendChild(neighborhoodLabel);

    function button(label, title) {
      var result = element("button", "bp-dag-button", label);
      result.type = "button";
      result.title = title;
      toolbar.appendChild(result);
      return result;
    }
    var zoomOut = button("−", "Zoom out");
    var zoomIn = button("+", "Zoom in");
    var fitButton = button("Fit", "Fit graph to viewport");
    var stats = element("span", "bp-dag-stats");
    stats.setAttribute("aria-live", "polite");
    toolbar.appendChild(stats);

    var canvas = element("canvas", "bp-dag-canvas");
    canvas.tabIndex = 0;
    canvas.setAttribute("role", "application");
    canvas.setAttribute("aria-label", "Interactive mathematical dependency graph. Use search to select any node, arrow relation lists to inspect its neighborhood, drag to pan, and scroll to zoom.");
    var detail = element("div", "bp-dag-detail", "Search for or select a node to inspect its accessible dependency neighborhood.");
    detail.setAttribute("aria-live", "polite");
    host.appendChild(toolbar);
    host.appendChild(canvas);
    host.appendChild(detail);

    var ctx = canvas.getContext("2d");
    var byId = new Map(), grid = new Map(), incident = new Map(), statusCounts = new Map();
    var prerequisites = new Map(), dependents = new Map(), neighbors = new Map();
    var maxRank = 0, maxSlot = 0;
    data.nodes.forEach(function (node) {
      node.x = node.column * (NODE_W + GAP_X);
      node.y = node.row * (NODE_H + GAP_Y);
      maxRank = Math.max(maxRank, node.column);
      maxSlot = Math.max(maxSlot, node.row);
      byId.set(node.id, node);
      grid.set(node.column + ":" + node.row, node);
      incident.set(node.id, []);
      prerequisites.set(node.id, []);
      dependents.set(node.id, []);
      neighbors.set(node.id, new Set([node.id]));
      statusCounts.set(node.status, (statusCounts.get(node.status) || 0) + 1);
    });
    data.edges.forEach(function (edge, index) {
      if (incident.has(edge.source)) incident.get(edge.source).push(index);
      if (incident.has(edge.target)) incident.get(edge.target).push(index);
      if (dependents.has(edge.source)) dependents.get(edge.source).push(edge.target);
      if (prerequisites.has(edge.target)) prerequisites.get(edge.target).push(edge.source);
      if (neighbors.has(edge.source)) neighbors.get(edge.source).add(edge.target);
      if (neighbors.has(edge.target)) neighbors.get(edge.target).add(edge.source);
    });
    var worldW = (maxRank + 1) * (NODE_W + GAP_X);
    var worldH = (maxSlot + 1) * (NODE_H + GAP_Y);
    var transform = {x: 24, y: 24, scale: 1};
    var selected = null, visible = [], dragging = false, moved = false;
    var pointer = {x: 0, y: 0};
    var scheduled = false;

    function dark() { return document.body.getAttribute("data-md-color-scheme") === "slate"; }
    function palette(node) {
      var entry = data.palette[node.status] || data.palette.planned;
      return entry[dark() ? "dark" : "light"];
    }
    function active(node) {
      if (!node) return false;
      if (status.value && node.status !== status.value) return false;
      return !(neighborhood.checked && selected) || neighbors.get(selected.id).has(node.id);
    }
    function schedule() {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(function () { scheduled = false; draw(); });
    }
    function resize() {
      var ratio = window.devicePixelRatio || 1;
      var rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, Math.round(rect.width * ratio));
      canvas.height = Math.max(1, Math.round(rect.height * ratio));
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      schedule();
    }
    function fit() {
      var rect = canvas.getBoundingClientRect();
      transform.scale = clamp(Math.min((rect.width - 48) / worldW, (rect.height - 48) / worldH), 0.0001, 1.2);
      transform.x = (rect.width - worldW * transform.scale) / 2;
      transform.y = (rect.height - worldH * transform.scale) / 2;
      schedule();
    }
    function screen(node) {
      return {x: node.x * transform.scale + transform.x, y: node.y * transform.scale + transform.y,
              w: NODE_W * transform.scale, h: NODE_H * transform.scale};
    }
    function onScreen(box, width, height) {
      return box.x + box.w >= -40 && box.y + box.h >= -40 && box.x <= width + 40 && box.y <= height + 40;
    }
    function drawArrow(a, b, dashed, count, offset) {
      ctx.save();
      ctx.strokeStyle = dark() ? "#8A8D91" : "#65676B";
      ctx.globalAlpha = 0.42;
      ctx.lineWidth = 1;
      if (dashed) ctx.setLineDash([4, 4]);
      var sourceY = a.y + a.h / 2 + offset, targetY = b.y + b.h / 2 + offset;
      ctx.beginPath();
      ctx.moveTo(a.x + a.w, sourceY);
      ctx.lineTo(b.x, targetY);
      ctx.stroke();
      if (transform.scale >= 0.16) {
        var tipX = b.x, tipY = targetY, size = 5;
        ctx.fillStyle = ctx.strokeStyle;
        ctx.beginPath();
        ctx.moveTo(tipX, tipY);
        ctx.lineTo(tipX - size, tipY - size * 0.65);
        ctx.lineTo(tipX - size, tipY + size * 0.65);
        ctx.closePath();
        ctx.fill();
      }
      if (count > 1 && transform.scale >= 0.32) {
        ctx.globalAlpha = 0.9;
        ctx.fillStyle = dark() ? "#E4E6EB" : "#1C1E21";
        ctx.font = Math.max(9, 10 * transform.scale) + "px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("×" + count, (a.x + a.w + b.x) / 2, (sourceY + targetY) / 2 - 3);
      }
      ctx.restore();
    }
    function draw() {
      var ratio = window.devicePixelRatio || 1;
      var width = canvas.width / ratio, height = canvas.height / ratio;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      var boxes = new Map();
      visible = [];
      var strideX = NODE_W + GAP_X, strideY = NODE_H + GAP_Y;
      var minColumn = clamp(Math.floor((-transform.x / transform.scale - NODE_W) / strideX), 0, maxRank);
      var maxColumn = clamp(Math.ceil(((width - transform.x) / transform.scale + NODE_W) / strideX), 0, maxRank);
      var minRow = clamp(Math.floor((-transform.y / transform.scale - NODE_H) / strideY), 0, maxSlot);
      var maxRow = clamp(Math.ceil(((height - transform.y) / transform.scale + NODE_H) / strideY), 0, maxSlot);
      for (var column = minColumn; column <= maxColumn; column++) {
        for (var row = minRow; row <= maxRow; row++) {
          var node = grid.get(column + ":" + row);
          if (!node || !active(node)) continue;
          var box = screen(node);
          boxes.set(node.id, box);
          if (onScreen(box, width, height)) visible.push(node);
        }
      }
      var edgeIndexes = new Set();
      visible.forEach(function (node) {
        (incident.get(node.id) || []).forEach(function (index) { edgeIndexes.add(index); });
      });
      edgeIndexes.forEach(function (index) {
        var edge = data.edges[index];
        if (!active(byId.get(edge.source)) || !active(byId.get(edge.target))) return;
        var a = boxes.get(edge.source), b = boxes.get(edge.target);
        if (!a) a = screen(byId.get(edge.source));
        if (!b) b = screen(byId.get(edge.target));
        var bounds = {x: Math.min(a.x, b.x), y: Math.min(a.y, b.y),
                      w: Math.abs(b.x - a.x) + Math.max(a.w, b.w),
                      h: Math.abs(b.y - a.y) + Math.max(a.h, b.h)};
        if (!onScreen(bounds, width, height)) return;
        if (edge.statement) drawArrow(a, b, false, edge.statement, edge.proof ? -4 : 0);
        if (proof.checked && edge.proof) drawArrow(a, b, true, edge.proof, edge.statement ? 4 : 0);
      });
      var query = search.value.trim().toLowerCase();
      visible.forEach(function (node) {
        var box = boxes.get(node.id), colors = palette(node);
        var matched = query && (node.title.toLowerCase().includes(query) || node.id.toLowerCase().includes(query));
        ctx.save();
        ctx.fillStyle = colors.fill;
        ctx.strokeStyle = matched ? "#F7B928" : colors.stroke;
        ctx.lineWidth = (node === selected || matched) ? 3 : node.kind === "scope" ? 2.5 : 1.5;
        if (node.kind === "boundary") ctx.setLineDash([6, 4]);
        if (transform.scale < 0.18) {
          ctx.fillRect(box.x, box.y, Math.max(3, box.w), Math.max(3, box.h));
          ctx.strokeRect(box.x, box.y, Math.max(3, box.w), Math.max(3, box.h));
          ctx.restore();
          return;
        }
        ctx.fillRect(box.x, box.y, box.w, box.h);
        ctx.strokeRect(box.x, box.y, box.w, box.h);
        if (transform.scale >= 0.42) {
          ctx.save();
          ctx.beginPath();
          ctx.rect(box.x + 4, box.y, Math.max(0, box.w - 8), box.h);
          ctx.clip();
          ctx.fillStyle = colors.text;
          ctx.font = Math.max(10, 12 * transform.scale) + "px sans-serif";
          ctx.textBaseline = "middle";
          ctx.fillText(node.title, box.x + 6, box.y + box.h / 2);
          ctx.restore();
        }
        ctx.restore();
      });
      var activeCount;
      if (neighborhood.checked && selected) {
        activeCount = Array.from(neighbors.get(selected.id)).filter(function (id) { return active(byId.get(id)); }).length;
      } else activeCount = status.value ? (statusCounts.get(status.value) || 0) : data.node_count;
      var statsText = activeCount + " / " + data.node_count + " nodes · " + data.edge_count + " dependencies";
      if (stats.textContent !== statsText) stats.textContent = statsText;
    }
    function zoom(factor, x, y) {
      var old = transform.scale;
      var next = clamp(old * factor, 0.0001, 4);
      transform.x = x - (x - transform.x) * next / old;
      transform.y = y - (y - transform.y) * next / old;
      transform.scale = next;
      schedule();
    }
    function hit(x, y) {
      for (var index = visible.length - 1; index >= 0; index--) {
        var node = visible[index], box = screen(node);
        if (x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h) return node;
      }
      return null;
    }
    function describe(node) {
      detail.textContent = "";
      if (!node) { detail.textContent = "Select a node to inspect it."; return; }
      var strong = element("strong", "", node.title);
      var presentation = node.kind === "boundary" ? "external boundary" :
                         node.kind === "scope" ? "nested scope" : "article";
      var kind = node.catalog === "module" ? "module inventory" : node.declaration;
      var metaText = " · " + presentation + " · " + (data.palette[node.status] || data.palette.planned).label;
      if (kind) metaText += " · " + kind;
      var meta = element("span", "", metaText + " · " + node.id);
      detail.appendChild(strong);
      detail.appendChild(meta);
      if (node.url) {
        var link = element("a", "bp-dag-open", "Open article");
        link.href = node.url;
        detail.appendChild(document.createTextNode(" · "));
        detail.appendChild(link);
      }
      function relations(label, ids) {
        if (!ids.length) return;
        var disclosure = element("details", "bp-dag-relations");
        disclosure.appendChild(element("summary", "", label + " (" + ids.length + ")"));
        var list = element("ul");
        var shown = 0, batch = 100;
        function appendRelations() {
          var limit = Math.min(ids.length, shown + batch);
          ids.slice(shown, limit).forEach(function (id) {
            var related = byId.get(id), item = element("li");
            if (related && related.url) {
              var link = element("a", "", related.title);
              link.href = related.url;
              item.appendChild(link);
            } else item.textContent = related ? related.title : id;
            list.appendChild(item);
          });
          shown = limit;
          if (more) {
            if (shown >= ids.length) more.remove();
            else more.textContent = "Show " + Math.min(batch, ids.length - shown) + " more";
          }
        }
        var more = ids.length > batch ? element("button", "bp-dag-button") : null;
        if (more) {
          more.type = "button";
          more.addEventListener("click", appendRelations);
        }
        appendRelations();
        disclosure.appendChild(list);
        if (more) disclosure.appendChild(more);
        detail.appendChild(disclosure);
      }
      relations("Prerequisites", prerequisites.get(node.id) || []);
      relations("Dependents", dependents.get(node.id) || []);
    }
    function showMatches() {
      var query = search.value.trim().toLowerCase();
      if (!query) {
        describe(selected);
        return;
      }
      var matches = data.nodes.filter(function (node) {
        return node.title.toLowerCase().includes(query) || node.id.toLowerCase().includes(query);
      });
      detail.textContent = "";
      detail.appendChild(element("strong", "", matches.length + " search result" + (matches.length === 1 ? "" : "s")));
      var list = element("ul", "bp-dag-results");
      matches.slice(0, 50).forEach(function (node) {
        var item = element("li"), choose = element("button", "bp-dag-result", node.title);
        choose.type = "button";
        choose.addEventListener("click", function () { center(node); search.value = node.title; });
        item.appendChild(choose);
        list.appendChild(item);
      });
      detail.appendChild(list);
      if (matches.length > 50) {
        detail.appendChild(element("p", "", "Showing the first 50 results; refine the search to reach the rest."));
      }
    }
    function updateRoute(node) {
      var params = new URLSearchParams(window.location.hash.slice(1));
      if (node) params.set("node", node.id);
      else params.delete("node");
      var hash = params.toString();
      history.replaceState(null, "", window.location.pathname + window.location.search + (hash ? "#" + hash : ""));
    }
    function center(node, updateHash) {
      if (!node) return;
      if (status.value && node.status !== status.value) status.value = "";
      var rect = canvas.getBoundingClientRect();
      transform.scale = Math.max(transform.scale, 0.8);
      transform.x = rect.width / 2 - (node.x + NODE_W / 2) * transform.scale;
      transform.y = rect.height / 2 - (node.y + NODE_H / 2) * transform.scale;
      selected = node;
      neighborhood.checked = true;
      describe(node);
      if (updateHash !== false) updateRoute(node);
      schedule();
    }

    canvas.addEventListener("pointerdown", function (event) {
      dragging = true; moved = false; pointer = {x: event.clientX, y: event.clientY};
      canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener("pointermove", function (event) {
      if (!dragging) return;
      var dx = event.clientX - pointer.x, dy = event.clientY - pointer.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
      transform.x += dx; transform.y += dy; pointer = {x: event.clientX, y: event.clientY}; schedule();
    });
    canvas.addEventListener("pointerup", function (event) {
      dragging = false;
      if (moved) return;
      var rect = canvas.getBoundingClientRect();
      var node = hit(event.clientX - rect.left, event.clientY - rect.top);
      selected = node; describe(node);
      if (node) neighborhood.checked = true;
      updateRoute(node);
      schedule();
      if (node && event.detail > 1 && node.url) window.location.href = node.url;
    });
    canvas.addEventListener("wheel", function (event) {
      event.preventDefault();
      var rect = canvas.getBoundingClientRect();
      zoom(event.deltaY < 0 ? 1.16 : 0.86, event.clientX - rect.left, event.clientY - rect.top);
    }, {passive: false});
    canvas.addEventListener("keydown", function (event) {
      if (event.key === "+" || event.key === "=") zoom(1.2, canvas.clientWidth / 2, canvas.clientHeight / 2);
      else if (event.key === "-") zoom(0.8, canvas.clientWidth / 2, canvas.clientHeight / 2);
      else if (event.key === "0") fit();
      else if (event.key === "Enter" && selected && selected.url) window.location.href = selected.url;
      else return;
      event.preventDefault();
    });
    search.addEventListener("input", function () { showMatches(); schedule(); });
    search.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      var query = search.value.trim().toLowerCase();
      center(data.nodes.find(function (node) { return node.title.toLowerCase().includes(query) || node.id.toLowerCase().includes(query); }));
    });
    status.addEventListener("change", function () {
      if (selected && status.value && selected.status !== status.value) {
        selected = null;
        neighborhood.checked = false;
        describe(null);
        updateRoute(null);
      }
      schedule();
    });
    proof.addEventListener("change", schedule);
    neighborhood.addEventListener("change", schedule);
    zoomIn.addEventListener("click", function () { zoom(1.25, canvas.clientWidth / 2, canvas.clientHeight / 2); });
    zoomOut.addEventListener("click", function () { zoom(0.8, canvas.clientWidth / 2, canvas.clientHeight / 2); });
    fitButton.addEventListener("click", fit);
    window.addEventListener("resize", resize);
    new MutationObserver(schedule).observe(document.body, {attributes: true, attributeFilter: ["data-md-color-scheme"]});
    resize(); fit();
    var initialId = new URLSearchParams(window.location.hash.slice(1)).get("node");
    if (initialId && byId.has(initialId)) setTimeout(function () { center(byId.get(initialId), false); }, 0);
  }

  function boot() {
    document.querySelectorAll(".bp-dag-viewer[data-graph-src]").forEach(start);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
'''


__all__ = [
    "MAX_MERMAID_CHARACTERS",
    "MAX_MERMAID_EDGE_LINES",
    "SCHEMA",
    "render_container",
    "requires_interactive",
    "viewer_script",
    "write_payload",
]
