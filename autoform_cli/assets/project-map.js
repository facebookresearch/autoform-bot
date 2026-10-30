/* Whole-project map (prototype).
 *
 * One continuous map of the whole project. Regions hold the mathematical parts inside them, and parts hold their
 * results; zooming in opens a part in place, so its boundary and its neighbours stay on screen. Data arrives one level
 * at a time from dependencies/map/: index.json (regions), parts/<file>.json (a part's children, and its results when
 * it holds them) and search.json (titles and Lean names, loaded on first search).
 *
 * Geometry comes from window.AutoformMapLayout (project-map-layout.js, shipped ahead of this file): world units are
 * stage pixels at the opening pose, a part's children are packed when its file loads, prerequisites come first, and
 * routes run only through the gaps between boxes. This file is the controller: camera, drawing, search, selection,
 * history and the reading pane.
 */
(function () {
  "use strict";

  var host = document.querySelector("[data-project-map]");
  if (!host || host.dataset.pmMounted) return;
  host.dataset.pmMounted = "1";
  var L = window.AutoformMapLayout;

  var mapBase = new URL(host.getAttribute("data-map") || "dependencies/map/", location.href);
  var routeBase = new URL(host.getAttribute("data-routes") || "dependencies/index.json", location.href);
  var reduceMotion = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var SVGNS = "http://www.w3.org/2000/svg";
  var ROOT = "#root";
  var OPEN_W = 140, OPEN_H = 90;   // a part shows its children (as tiles at first) once its box is this big on screen (px)
  var LABEL_MIN_W = 130;           // boxes narrower than this on screen never get a label
  var CARD_TEXT_W = 120;           // result cards carry their titles, and their links, once this wide on screen (px)
  var TITLE_EM = 8.4, META_EM = 6.6, CARD_EM = 7.2, CARD_META_EM = 6.2;  // mean character advances of the label fonts (px)

  // ---------------------------------------------------------------- small helpers
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function svg(tag, attrs) {
    var node = document.createElementNS(SVGNS, tag);
    if (attrs) for (var key in attrs) node.setAttribute(key, attrs[key]);
    return node;
  }
  function num(value) { return Number(value || 0).toLocaleString("en-US"); }
  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }
  var DOUBLE = {R: "ℝ", N: "ℕ", Z: "ℤ", Q: "ℚ", C: "ℂ", F: "𝔽", P: "ℙ", A: "𝔸", H: "ℍ", K: "𝕂"};
  var GREEK = {alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε", zeta: "ζ", eta: "η", theta: "θ",
    lambda: "λ", mu: "μ", nu: "ν", xi: "ξ", pi: "π", rho: "ρ", sigma: "σ", tau: "τ", phi: "φ", varphi: "φ", chi: "χ", psi: "ψ",
    omega: "ω", Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Pi: "Π", Sigma: "Σ", Phi: "Φ", Psi: "Ψ", Omega: "Ω",
    infty: "∞", to: "→", le: "≤", ge: "≥", leq: "≤", geq: "≥", ne: "≠", times: "×", cdot: "·", in: "∈", subset: "⊂", ell: "ℓ"};
  // A label for places that cannot typeset (map text): TeX source made readable, never shown with its dollar signs.
  function plain(text) {
    return String(text || "").replace(/\$([^$]*)\$|\\\(([\s\S]*?)\\\)/g, function (_m, a, b) {
      return (a != null ? a : b)
        .replace(/\\mathbb\{([A-Za-z])\}/g, function (_x, letter) { return DOUBLE[letter] || letter; })
        .replace(/\\(?:mathrm|mathcal|mathfrak|operatorname|text|mathbf|mathsf)\{([^}]*)\}/g, "$1")
        .replace(/\\([A-Za-z]+)/g, function (_x, name) { return GREEK[name] || name; })
        .replace(/\^\{([^}]*)\}/g, "^$1").replace(/_\{([^}]*)\}/g, "_$1").replace(/[{}]/g, "");
    }).replace(/`([^`]*)`/g, "$1");
  }
  var DONE = {fully_proved: 1, proved: 1, defined: 1, mathlib: 1};
  var PROGRESS = {can_prove: 1, stated: 1, can_state: 1, conditional: 1, statement_formalized: 1};
  function group(status) { return DONE[status] ? "done" : PROGRESS[status] ? "progress" : "planned"; }
  var STATUS_WORDS = {fully_proved: "fully proved", proved: "proved", defined: "defined", mathlib: "in Mathlib",
    can_prove: "ready to prove", stated: "stated", can_state: "ready to state", conditional: "conditional",
    not_ready: "not ready", planned: "planned"};
  function statusWords(status) { return STATUS_WORDS[status] || String(status || "").replace(/_/g, " "); }
  function mix(status) {
    var out = {done: 0, progress: 0, planned: 0};
    for (var key in status || {}) out[group(key)] += status[key];
    return out;
  }
  function progressText(summary) {
    return num(mix(summary.status).done) + " of " + num(summary.results) + " finished";
  }
  // A closed part's tile is tinted by what most of it is.
  function tileKind(status) {
    var counts = mix(status), total = counts.done + counts.progress + counts.planned;
    if (!total) return "planned";
    return counts.done / total >= 0.9 ? "done" : (counts.done + counts.progress) / total >= 0.5 ? "progress" : "planned";
  }
  function color(kind) {
    return getComputedStyle(document.documentElement).getPropertyValue(kind === "done" ? "--pm-proved" : kind === "progress" ? "--pm-progress" : "--pm-planned").trim() || "#999";
  }
  function wrap(text, width, maxLines) {
    var words = text.split(/\s+/), lines = [], line = "";
    words.forEach(function (word) {
      if (!line) line = word;
      else if ((line + " " + word).length <= width) line += " " + word;
      else { lines.push(line); line = word; }
    });
    if (line) lines.push(line);
    if (lines.length > maxLines) { lines = lines.slice(0, maxLines); lines[maxLines - 1] = clip(lines[maxLines - 1] + "…", width); }
    return lines.map(function (l) { return clip(l, width); });
  }
  function clip(text, width) { return text.length > width ? text.slice(0, Math.max(1, width - 1)) + "…" : text; }

  // ---------------------------------------------------------------- data
  var cache = new Map();
  function fetchJSON(relative) {
    if (!cache.has(relative)) {
      cache.set(relative, fetch(new URL(relative, mapBase)).then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status + " for " + relative);
        return response.json();
      }));
    }
    return cache.get(relative);
  }

  var model = {index: null, parts: new Map(), results: new Map(), search: null, searchLoading: null};
  function part(id) { return model.parts.get(id); }
  function register(summary, parentId) {
    var known = model.parts.get(summary.id);
    if (known) { known.summary = summary; return known; }
    var record = {id: summary.id, summary: summary, parent: parentId, data: null, loading: null, box: null, ownBox: null, view: null, kids: [], results: []};
    model.parts.set(summary.id, record);
    var parent = model.parts.get(parentId);
    if (parent && parent.kids.indexOf(summary.id) < 0) parent.kids.push(summary.id);
    return record;
  }
  function chainOf(id) {
    var out = [];
    for (var at = part(id); at && at.id !== ROOT; at = part(at.parent)) out.unshift(at.id);
    return out;
  }
  function load(id) {
    var record = part(id);
    if (!record || record.id === ROOT) return Promise.resolve(record);
    if (record.data) return Promise.resolve(record);
    if (!record.loading) {
      record.loading = fetchJSON("parts/" + record.summary.file).then(function (data) {
        record.data = data;
        (data.children || []).forEach(function (child) { register(child, record.id); });
        (data.results || []).forEach(function (row) {
          model.results.set(row.id, {row: row, home: record.id, box: null});
          if (record.results.indexOf(row.id) < 0) record.results.push(row.id);
        });
        packChildren(record);
        schedule();
        return record;
      });
    }
    return record.loading;
  }

  // ---------------------------------------------------------------- layout (from the layout module)
  var stageSize = null;      // the stage the world was packed for
  var routesAt = new Map();  // part id -> routes between its children, in world units
  function packRoot() {
    var root = part(ROOT), top = L.packLevels(model.index, stageSize);
    root.box = top.root;
    root.kids.forEach(function (id) { part(id).box = top.boxes[id] || null; });
    routesAt.set(ROOT, L.bundleEdges(top.boxes, model.index.region_edges || [], top.content));
    root.kids.forEach(function (id) { packChildren(part(id)); });
  }
  // A part's children, its own results and the routes between them, once both its box and its file are known.
  function packChildren(record) {
    if (!record || !record.box || !record.data) return;
    var boxes = L.packPart(record.data, record.box, stageSize);
    record.kids.forEach(function (id) { part(id).box = boxes[id] || null; });
    record.ownBox = boxes[record.id + "#results"] || null;
    routesAt.set(record.id, record.kids.length ? L.bundleEdges(boxes, record.data.edges || [], L.contentOf(record.box)) : []);
    var results = record.data.results || [];
    record.view = record.ownBox && results.length ?
      L.layoutCommunity(results, record.data.result_edges || [], record.data.boundary_edges || [], record.ownBox) : null;
    record.results.forEach(function (id) { var entry = model.results.get(id); if (entry) entry.box = record.view ? record.view.cards[id] || null : null; });
    record.kids.forEach(function (id) { if (part(id).data) packChildren(part(id)); });
  }

  // ---------------------------------------------------------------- page shell
  host.textContent = "";
  var app = el("div", "pm-app");
  var top = el("header", "pm-top");
  var home = el("a", "pm-home", "Home"); home.href = host.getAttribute("data-home") || "../";
  var trail = el("ol", "pm-trail"); trail.setAttribute("aria-label", "Where you are on the map");
  var searchBox = el("div", "pm-search");
  var search = el("input"); search.type = "search"; search.placeholder = "Search theorems by name or Lean name"; search.setAttribute("aria-label", "Search theorems by name or Lean name");
  search.setAttribute("autocomplete", "off");
  var hits = el("ul", "pm-hits"); hits.hidden = true; hits.id = "pm-hits"; hits.setAttribute("role", "listbox");
  search.setAttribute("aria-controls", "pm-hits");
  searchBox.append(search, hits);
  var tools = el("div", "pm-tools");
  var zoomOut = el("button", "", "−"), zoomIn = el("button", "", "+"), fitAll = el("button", "", "Whole project");
  zoomOut.setAttribute("aria-label", "Zoom out"); zoomIn.setAttribute("aria-label", "Zoom in");
  tools.append(zoomOut, zoomIn, fitAll);
  top.append(home, trail, searchBox, tools);

  var side = el("aside", "pm-side"); side.setAttribute("aria-label", "Project outline");
  var sideInner = el("div", "pm-side-inner"); side.append(sideInner);
  var stage = el("main", "pm-stage");
  var canvas = svg("svg", {role: "application", "aria-label": "Map of the whole project. Scroll or use plus and minus to zoom, drag or use the arrow keys to move."});
  canvas.setAttribute("tabindex", "0");
  var defs = svg("defs");
  // Markers do not inherit the colour of the line that uses them, so a highlighted route has its own.
  defs.innerHTML = ["pm-arrow", "pm-arrow-on"].map(function (id) {
    return '<marker id="' + id + '" class="' + id + '" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,1 L10,5 L0,9 z"/></marker>';
  }).join("");
  // Routes run through the gaps between boxes, so they draw above every box; labels draw last.
  var partLayer = svg("g"), routeLayer = svg("g"), resultLayer = svg("g"), cardRouteLayer = svg("g"), labelLayer = svg("g");
  canvas.append(defs, partLayer, routeLayer, resultLayer, cardRouteLayer, labelLayer);
  var pin = el("div", "pm-pin"); pin.hidden = true;
  var hint = el("div", "pm-hint", "Scroll to zoom into a region. Search to jump to a theorem.");
  stage.append(canvas, pin, hint);
  var reader = el("aside", "pm-reader"); reader.setAttribute("aria-label", "Reading pane");
  var readerInner = el("div", "pm-reader-inner"); reader.append(readerInner);
  var live = el("p", "pm-sr"); live.setAttribute("aria-live", "polite");
  app.append(top, side, stage, reader, live);
  host.append(app);

  // ---------------------------------------------------------------- camera
  // The camera is the world point at the centre of the stage and the zoom k (screen px per world unit).
  var camera = {x: 0, y: 0, k: 1};
  var size = {w: 1, h: 1};
  function measure() {
    var box = canvas.getBoundingClientRect();
    size.w = Math.max(1, box.width); size.h = Math.max(1, box.height);
  }
  function fitK(box, share) { return Math.min(size.w / box.w, size.h / box.h) * (share || 0.96); }
  function centerOf(box) { return [box.x + box.w / 2, box.y + box.h / 2]; }
  function toScreen(x, y) { return [(x - camera.x) * camera.k + size.w / 2, (y - camera.y) * camera.k + size.h / 2]; }
  function toWorld(sx, sy) { return [(sx - size.w / 2) / camera.k + camera.x, (sy - size.h / 2) / camera.k + camera.y]; }
  function screenBox(box) {
    var a = toScreen(box.x, box.y);
    return {x: a[0], y: a[1], w: box.w * camera.k, h: box.h * camera.k};
  }
  // The layout module's camera: the world point at the stage's top-left corner and the zoom.
  function cornerCamera() { return {x: camera.x - size.w / 2 / camera.k, y: camera.y - size.h / 2 / camera.k, z: camera.k}; }
  function limits() {
    var box = part(ROOT).box;
    return {low: Math.min(size.w / box.w, size.h / box.h) * 0.5, high: 60000};
  }
  var flight = null;
  function flyTo(target, done) {
    var range = limits();
    target = {x: target.x, y: target.y, k: clamp(target.k, range.low, range.high)};
    if (flight) cancelAnimationFrame(flight.frame);
    if (reduceMotion) { camera = target; schedule(); if (done) done(); return; }
    var start = {x: camera.x, y: camera.y, k: camera.k}, began = performance.now(), duration = 560;
    flight = {frame: 0};
    var step = function (now) {
      var t = Math.min(1, (now - began) / duration), e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      // Interpolate the scale geometrically, so a deep zoom does not rush its last screens.
      camera = {x: start.x + (target.x - start.x) * e, y: start.y + (target.y - start.y) * e,
        k: Math.exp(Math.log(start.k) + (Math.log(target.k) - Math.log(start.k)) * e)};
      render();
      if (t < 1) flight.frame = requestAnimationFrame(step); else { flight = null; if (done) done(); }
    };
    flight.frame = requestAnimationFrame(step);
  }
  // A focus fits the part's content, not its whole box, so its header band does not fill the top of the screen.
  function flyToContent(record, done) {
    var content = L.contentOf(record.box), c = centerOf(content);
    flyTo({x: c[0], y: c[1], k: fitK(content)}, done);
  }
  function zoomAt(factor, sx, sy) {
    var range = limits(), k = clamp(camera.k * factor, range.low, range.high);
    var world = toWorld(sx, sy);
    camera = {x: world[0] - (sx - size.w / 2) / k, y: world[1] - (sy - size.h / 2) / k, k: k};
    schedule();
  }
  function fitProject(instant) {
    var box = part(ROOT).box, k = Math.min(size.w / box.w, size.h / box.h), target = {x: box.x + box.w / 2, y: box.y + box.h / 2, k: k};
    // A world taller than the stage (many regions on a narrow screen) opens at full width, top-aligned, and pans down.
    if (box.h / box.w > size.h / size.w * 1.05) { k = size.w / box.w; target = {x: box.x + box.w / 2, y: box.y + size.h / 2 / k, k: k}; }
    if (instant) { camera = target; schedule(); } else flyTo(target);
  }

  // ---------------------------------------------------------------- state
  // focusPart pins the reading pane and trail to a part the reader asked for (ROOT for the whole project) until they
  // move the map themselves; then both follow the part that fills the view.
  var state = {selected: null, related: null, focusPart: null, hoverPart: null, facets: null};
  var pending = false;
  function schedule() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () { pending = false; render(); });
  }
  function release() { if (state.focusPart && !state.selected) state.focusPart = null; }

  // ---------------------------------------------------------------- render
  var keyed = new Map(), used = new Set();
  // Each key always gets the same attributes, so a node reused across frames never keeps a stale one.
  function keep(layer, key, tag, attrs) {
    var node = keyed.get(key);
    if (!node) { node = svg(tag); node.dataset.key = key; keyed.set(key, node); }
    if (node.parentNode !== layer) layer.appendChild(node);
    for (var name in attrs) if (node.getAttribute(name) !== String(attrs[name])) node.setAttribute(name, attrs[name]);
    used.add(key);
    return node;
  }
  function setText(node, text) { if (node.textContent !== text) node.textContent = text; return node; }
  function onScreen(b) { return b.x + b.w > -40 && b.x < size.w + 40 && b.y + b.h > -40 && b.y < size.h + 40; }
  function isOpen(record, b) { return record.id === ROOT || (!!record.data && b.w >= OPEN_W && b.h >= OPEN_H); }
  // The part the view is in: the deepest open part under the centre that fills most of the stage.
  function covers(b) {
    return b.x <= size.w / 2 && b.x + b.w >= size.w / 2 && b.y <= size.h / 2 && b.y + b.h >= size.h / 2 &&
      Math.min(b.w, size.w) * Math.min(b.h, size.h) >= size.w * size.h * 0.45;
  }
  var deepestAtCenter = null;
  function currentPart() { return state.focusPart ? part(state.focusPart) || part(ROOT) : deepestAtCenter; }
  function render() {
    if (!part(ROOT) || !part(ROOT).box) return;
    measure();
    used = new Set();
    deepestAtCenter = part(ROOT);
    var onPath = state.selected && model.results.get(state.selected) ? new Set(chainOf(model.results.get(state.selected).home)) : null;
    var labelBoxes = {}, opened = [], communities = [];
    (function visit(record) {
      if (!record.box) return;
      var b = screenBox(record.box);
      if (record.id !== ROOT && !onScreen(b)) return;
      if (record.id !== ROOT && !record.data && b.w >= OPEN_W && b.h >= OPEN_H) load(record.id);
      var open = isOpen(record, b);
      if (record.id !== ROOT) {
        if (open && covers(b)) deepestAtCenter = record;
        drawPart(record, b, open, onPath);
        if (b.w >= LABEL_MIN_W) labelBoxes[record.id] = record.box;
      }
      if (!open) return;
      opened.push(record);
      record.kids.forEach(function (id) { visit(part(id)); });
      if (record.view) communities.push(record);
    })(part(ROOT));
    opened.forEach(drawRoutes);
    communities.forEach(drawCommunity);
    drawLabels(labelBoxes);
    drawPin();
    keyed.forEach(function (node, key) { if (!used.has(key)) { node.remove(); keyed.delete(key); } });
    updateTrail();
    if (!state.selected) showPart(currentPart());
  }
  function drawPart(record, b, open, onPath) {
    var dim = state.related && !state.related.parts.has(record.id);
    var holder = keep(partLayer, "part:" + record.id, "g", {
      class: "pm-part " + (open ? "pm-open" : "pm-tile pm-tile-" + tileKind(record.summary.status)) + (dim ? " pm-dimmed" : "") +
        (state.focusPart === record.id ? " pm-focus" : "") + (onPath && onPath.has(record.id) ? " pm-on-path" : ""),
      tabindex: open ? -1 : 0, role: "button", "data-part": record.id,
      "aria-label": plain(record.summary.title) + ", " + num(record.summary.results) + " results" +
        (record.summary.api_total ? ", " + num(record.summary.api_total) + " used elsewhere" : "")});
    var shape = holder.firstChild || holder.appendChild(svg("rect"));
    shape.setAttribute("x", b.x); shape.setAttribute("y", b.y); shape.setAttribute("width", Math.max(1, b.w)); shape.setAttribute("height", Math.max(1, b.h));
    shape.setAttribute("rx", clamp(Math.min(b.w, b.h) * 0.05, 1.5, 10));
    if (!holder.querySelector("title")) holder.appendChild(svg("title")).textContent = plain(record.summary.title);
  }
  // An orthogonal route as a screen path, its corners rounded to radius.
  function routePath(points, radius) {
    var pts = [];
    for (var i = 0; i < points.length; i += 2) pts.push(toScreen(points[i], points[i + 1]));
    var r = radius * camera.k, d = "M" + pts[0][0].toFixed(1) + "," + pts[0][1].toFixed(1);
    for (i = 1; i < pts.length - 1; i++) {
      var p0 = pts[i - 1], p1 = pts[i], p2 = pts[i + 1];
      var l1 = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]), l2 = Math.hypot(p2[0] - p1[0], p2[1] - p1[1]);
      if (!l1 || !l2) continue;
      var rr = Math.min(r, l1 / 2, l2 / 2);
      d += "L" + (p1[0] - (p1[0] - p0[0]) / l1 * rr).toFixed(1) + "," + (p1[1] - (p1[1] - p0[1]) / l1 * rr).toFixed(1) +
        "Q" + p1[0].toFixed(1) + "," + p1[1].toFixed(1) + " " +
        (p1[0] + (p2[0] - p1[0]) / l2 * rr).toFixed(1) + "," + (p1[1] + (p2[1] - p1[1]) / l2 * rr).toFixed(1);
    }
    var last = pts[pts.length - 1];
    return d + "L" + last[0].toFixed(1) + "," + last[1].toFixed(1);
  }
  function routeVisible(points) {
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (var i = 0; i < points.length; i += 2) { x0 = Math.min(x0, points[i]); x1 = Math.max(x1, points[i]); y0 = Math.min(y0, points[i + 1]); y1 = Math.max(y1, points[i + 1]); }
    var a = toScreen(x0, y0), b = toScreen(x1, y1);
    return onScreen({x: a[0], y: a[1], w: b[0] - a[0], h: b[1] - a[1]});
  }
  function arrow(on) { return on ? "url(#pm-arrow-on)" : "url(#pm-arrow)"; }
  function linkWords(route, titleOf) {
    var main = route.weights[0] + route.weights[1], back = route.weights[2] + route.weights[3];
    return titleOf(route.to) + " uses " + titleOf(route.from) + ": " + num(main) + (main === 1 ? " link" : " links") +
      (route.both ? "; " + titleOf(route.from) + " uses " + titleOf(route.to) + ": " + num(back) + (back === 1 ? " link" : " links") : "");
  }
  // The routes between an open part's children: the central ones at rest, and every route of the part in focus or under the pointer.
  function drawRoutes(record) {
    var routes = routesAt.get(record.id) || [];
    routes.forEach(function (route) {
      var involved = [state.focusPart, state.hoverPart].some(function (id) { return id && (route.from === id || route.to === id); });
      if ((!route.rest && !involved) || !route.points || !routeVisible(route.points)) return;
      var dim = state.related && !(state.related.parts.has(route.from) && state.related.parts.has(route.to));
      var path = keep(routeLayer, "route:" + record.id + ":" + route.from + ">" + route.to, "path", {
        d: routePath(route.points, route.radius),
        class: "pm-route" + (route.kind === "proof" ? " pm-proof" : "") + (involved ? " pm-involved" : "") + (dim ? " pm-dimmed" : ""),
        "stroke-width": route.tier === 3 ? 3 : route.tier === 2 ? 2 : 1.25,
        "marker-end": arrow(involved), "marker-start": route.both ? arrow(involved) : "none"});
      setText(path.firstChild || path.appendChild(svg("title")), linkWords(route, function (id) { return plain((part(id) || {summary: {title: id}}).summary.title); }));
    });
  }
  // A part's own results: status tiles while small, then cards with their titles and the links between them.
  function drawCommunity(record) {
    var view = record.view, first = view.cards[record.results[0]];
    var text = !!first && first.w * camera.k >= CARD_TEXT_W;
    record.results.forEach(function (id) { drawCard(model.results.get(id), view, text); });
    if (!text) return;
    var selected = state.selected && model.results.get(state.selected) && model.results.get(state.selected).home === record.id ? state.selected : null;
    function titleOf(id) { var entry = model.results.get(id); return plain(entry ? entry.row.title : id); }
    view.routes.concat(selected ? view.implied.filter(function (route) { return route.from === selected || route.to === selected; }) : [])
      .forEach(function (route) {
        if (!route.points || !routeVisible(route.points)) return;
        var involved = selected && (route.from === selected || route.to === selected);
        var path = keep(cardRouteLayer, "croute:" + route.from + ">" + route.to, "path", {
          d: routePath(route.points, route.radius),
          class: "pm-route pm-card-route" + (route.kind === "proof" ? " pm-proof" : "") + (involved ? " pm-involved" : "") + (selected && !involved ? " pm-dimmed" : ""),
          "stroke-width": involved ? 2 : 1.25, "marker-end": arrow(involved), "marker-start": route.both ? arrow(involved) : "none"});
        setText(path.firstChild || path.appendChild(svg("title")), linkWords(route, titleOf));
      });
  }
  function drawCard(entry, view, text) {
    if (!entry || !entry.box) return;
    var b = screenBox(entry.box);
    if (!onScreen(b) || b.w < 1) return;
    var row = entry.row, isSelected = row.id === state.selected;
    var dim = state.related ? !state.related.results.has(row.id) : state.facets && row.facet && !state.facets.has(row.facet);
    var holder = keep(resultLayer, "result:" + row.id, "g", {
      class: "pm-result pm-" + group(row.status) + (text ? " pm-card" : "") + (isSelected ? " pm-selected" : "") + (dim ? " pm-dimmed" : ""),
      tabindex: 0, role: "button", "aria-label": plain(row.title) + ", " + statusWords(row.status), "data-result": row.id});
    var card = holder.firstChild || holder.appendChild(svg("rect"));
    card.setAttribute("x", b.x); card.setAttribute("y", b.y); card.setAttribute("width", Math.max(1, b.w)); card.setAttribute("height", Math.max(1, b.h));
    card.setAttribute("rx", clamp(b.w * 0.04, 1, 8));
    if (!holder.querySelector("title")) holder.appendChild(svg("title")).textContent = plain(row.title);
    if (!text) return;
    var pad = 10, width = b.w - 2 * pad, cls = dim ? " pm-dimmed" : "";
    var lines = wrap(plain(row.title), Math.max(4, Math.floor(width / CARD_EM)), b.h >= 58 ? 2 : 1);
    lines.forEach(function (line, index) {
      setText(keep(labelLayer, "ct:" + row.id + ":" + index, "text", {x: b.x + pad, y: b.y + pad + 13 + index * 17, class: "pm-card-title" + cls}), line);
    });
    if (b.h >= 52) {
      var stub = view.stubs[row.id], meta = statusWords(row.status) + (stub && stub.usedBy ? " · used " + num(stub.usedBy) + "× elsewhere" : "");
      setText(keep(labelLayer, "cm:" + row.id, "text", {x: b.x + pad, y: b.y + b.h - pad, class: "pm-card-meta" + cls}), clip(meta, Math.max(4, Math.floor(width / CARD_META_EM))));
    }
  }
  // Titles and progress in each box's header band, at fixed sizes, where the layout module finds room for them.
  function drawLabels(boxes) {
    L.placeLabels(boxes, cornerCamera(), size).forEach(function (slot) {
      var record = part(slot.id);
      if (!record) return;
      var cls = state.related && !state.related.parts.has(slot.id) ? " pm-dimmed" : "", title = plain(record.summary.title);
      var lines = slot.lines === 2 ? wrap(title, Math.max(4, Math.floor(slot.w / TITLE_EM)), 2) : [clip(title, Math.max(4, Math.floor(slot.w / TITLE_EM)))];
      var y = slot.y + 30;
      lines.forEach(function (line, index) {
        setText(keep(labelLayer, "lt:" + slot.id + ":" + index, "text", {x: slot.x, y: y + index * 20, class: "pm-title" + cls}), line);
      });
      if (slot.detail === "title") return;
      var metaY = y + (lines.length - 1) * 20 + 18;
      setText(keep(labelLayer, "lm:" + slot.id, "text", {x: slot.x, y: metaY, class: "pm-meta" + cls}), clip(progressText(record.summary), Math.floor(slot.w / META_EM)));
      var api = record.summary.api || [];
      if (slot.detail === "full" && api.length) {
        var text = "Used elsewhere: " + api.slice(0, 4).map(function (row) { return plain(row[1]) + " ×" + num(row[2]); }).join(" · ");
        setText(keep(labelLayer, "la:" + slot.id, "text", {x: slot.x, y: metaY + 18, class: "pm-meta" + cls}), clip(text, Math.floor(slot.w / META_EM)));
      }
    });
  }
  // Once the part the view is in has its header band scrolled off, its title stays pinned to the top of the stage.
  function drawPin() {
    var record = currentPart();
    if (!record || record.id === ROOT || !record.box) { pin.hidden = true; return; }
    var b = screenBox(record.box), band = (record.box.band || L.constants.BAND) * (record.box.unit || 1) * camera.k;
    if (b.y + band * 0.6 >= 0 || b.y + b.h < 60) { pin.hidden = true; return; }
    pin.hidden = false;
    if (pin.dataset.part !== record.id) {
      pin.dataset.part = record.id;
      pin.replaceChildren(el("strong", "", plain(record.summary.title)), el("span", "", progressText(record.summary)));
    }
  }

  // ---------------------------------------------------------------- selection and relationships
  function relationsOf(id) {
    var entry = model.results.get(id), owner = entry && part(entry.home);
    var uses = [], usedBy = [], outside = new Map();
    if (!owner || !owner.data) return {uses: uses, usedBy: usedBy, outside: outside};
    (owner.data.outside || []).forEach(function (row) { outside.set(row[0], row); });
    (owner.data.result_edges || []).concat(owner.data.boundary_edges || []).forEach(function (edge) {
      if (edge[0] === id) uses.push({id: edge[1], weight: edge[2] + edge[3], statement: edge[2] > 0});
      else if (edge[1] === id) usedBy.push({id: edge[0], weight: edge[2] + edge[3], statement: edge[2] > 0});
    });
    return {uses: uses, usedBy: usedBy, outside: outside};
  }
  // A selection keeps its relatives and every part they live in at full strength; the rest of the map dims.
  function relatedSets(id) {
    var rel = relationsOf(id), results = new Set([id]), parts = new Set();
    function addChain(chain) { chain.forEach(function (p) { parts.add(p); }); }
    var entry = model.results.get(id);
    if (entry) addChain(chainOf(entry.home));
    rel.uses.concat(rel.usedBy).forEach(function (item) {
      results.add(item.id);
      var other = model.results.get(item.id), outside = rel.outside.get(item.id);
      if (other) addChain(chainOf(other.home)); else if (outside) addChain(outside[2]);
    });
    return {results: results, parts: parts};
  }
  function select(id, options) {
    options = options || {};
    state.selected = id;
    state.related = id ? relatedSets(id) : null;
    if (id) showResult(id); else showPart(currentPart());
    if (options.history !== false) remember(options.replace);
    schedule();
  }

  // ---------------------------------------------------------------- arriving somewhere
  function ensureChain(chain) {
    return chain.reduce(function (promise, id, index) {
      return promise.then(function () {
        if (!part(id)) {
          var row = model.search && model.search.partById.get(id);
          if (!row) throw new Error("Unknown part " + id);
          register({id: id, title: row[1], results: 0, status: {}, parts: 0, api: [], api_total: 0, imports_total: 0, file: row[2]},
            index ? chain[index - 1] : ROOT);
        }
        return load(id);
      });
    }, Promise.resolve());
  }
  function partChainFromSearch(position) {
    var rows = model.search.data.parts, chain = [];
    for (var at = position; at >= 0; at = rows[at][3]) chain.unshift(rows[at][0]);
    return chain;
  }
  function flyToPart(id, options) {
    options = options || {};
    return ensureChain(chainOf(id)).then(function () {
      var record = part(id);
      state.focusPart = id;
      if (options.history !== false) remember(options.replace);
      flyToContent(record);
      showPart(record);
    });
  }
  function arrive(resultId, chain, options) {
    options = options || {};
    return ensureChain(chain).then(function () {
      var homeId = chain[chain.length - 1], record = part(homeId), entry = model.results.get(resultId);
      if (!entry || !entry.box) throw new Error("Result not found in its part: " + resultId);
      state.focusPart = homeId;
      // Close enough that the result's card carries its title, with as much of its part around it as fits.
      var at = centerOf(entry.box), k = Math.max(fitK(L.contentOf(record.box)), (CARD_TEXT_W * 1.9) / entry.box.w);
      select(resultId, {history: false});
      if (options.history !== false) remember(options.replace);
      flyTo({x: at[0], y: at[1], k: k});
      live.textContent = "Showing " + plain(entry.row.title);
    });
  }
  // A result the map has not loaded yet (from a list in the reading pane): find its path through search.
  function arriveById(resultId) {
    var entry = model.results.get(resultId);
    if (entry) return arrive(resultId, chainOf(entry.home));
    return loadSearch().then(function () {
      var hit = model.search.byId.get(resultId);
      if (!hit) throw new Error("Unknown result " + resultId);
      return arrive(resultId, partChainFromSearch(hit[2]));
    });
  }

  // ---------------------------------------------------------------- search
  function loadSearch() {
    if (model.search) return Promise.resolve(model.search);
    if (!model.searchLoading) {
      model.searchLoading = fetchJSON("search.json").then(function (data) {
        var partById = new Map(), byId = new Map();
        data.parts.forEach(function (row) { partById.set(row[0], row); });
        var rows = data.rows.map(function (row) {
          var at = data.parts[row[2]][0], id = row[3].indexOf("/") === 0 || !row[3] || row[3].indexOf(at + "/") === 0 ? row[3] : at + "/" + row[3];
          var entry = [row[0], row[1], row[2], id, String(row[0]).toLowerCase(), String(row[1]).toLowerCase()];
          byId.set(id, entry);
          return entry;
        });
        model.search = {data: data, rows: rows, partById: partById, byId: byId};
        return model.search;
      });
    }
    return model.searchLoading;
  }
  var activeHit = -1, hitRows = [];
  function runSearch() {
    var query = search.value.trim().toLowerCase();
    if (!query) { hits.hidden = true; return; }
    if (!model.search) {
      hits.hidden = false; hits.replaceChildren(el("li", "pm-note", "Loading every theorem's name…"));
      loadSearch().then(runSearch, function () { hits.replaceChildren(el("li", "pm-note", "Search is unavailable.")); });
      return;
    }
    var scored = [];
    for (var i = 0; i < model.search.rows.length && scored.length < 4000; i++) {
      var row = model.search.rows[i], title = row[4], lean = row[5], rank = 0;
      if (title === query || lean === query) rank = 1;
      else if (title.indexOf(query) === 0 || lean.indexOf(query) === 0 || lean.endsWith("." + query)) rank = 2;
      else if (title.indexOf(query) > 0) rank = 3;
      else if (lean.indexOf(query) > 0) rank = 4;
      if (rank) scored.push([rank, row]);
    }
    scored.sort(function (a, b) { return a[0] - b[0] || a[1][0].length - b[1][0].length; });
    hitRows = scored.slice(0, 20).map(function (pair) { return pair[1]; });
    activeHit = hitRows.length ? 0 : -1;
    hits.replaceChildren();
    if (!hitRows.length) hits.append(el("li", "pm-note", "No theorem or Lean name matches."));
    hitRows.forEach(function (row, index) {
      var item = el("li"), button = el("button", "pm-hit");
      button.type = "button"; button.setAttribute("role", "option"); button.setAttribute("aria-selected", String(index === activeHit));
      button.append(el("span", "pm-hit-title", plain(row[0])));
      var meta = el("span", "pm-hit-meta"), chain = partChainFromSearch(row[2]);
      meta.append(document.createTextNode(chain.map(function (id) { return plain(model.search.partById.get(id)[1]); }).join(" / ")));
      if (row[1]) { meta.append(document.createTextNode(" · ")); meta.append(el("code", "", row[1])); }
      button.append(meta);
      button.addEventListener("click", function () { choose(row); });
      item.append(button); hits.append(item);
    });
    hits.hidden = false;
  }
  function choose(row) {
    hits.hidden = true;
    search.value = plain(row[0]);
    arrive(row[3], partChainFromSearch(row[2])).catch(report);
  }
  search.addEventListener("focus", function () { loadSearch().catch(function () {}); });
  search.addEventListener("input", runSearch);
  search.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!hitRows.length) return;
      event.preventDefault();
      activeHit = (activeHit + (event.key === "ArrowDown" ? 1 : hitRows.length - 1)) % hitRows.length;
      hits.querySelectorAll(".pm-hit").forEach(function (button, index) { button.setAttribute("aria-selected", String(index === activeHit)); });
    } else if (event.key === "Enter" && activeHit >= 0) {
      event.preventDefault(); choose(hitRows[activeHit]);
    } else if (event.key === "Escape") { hits.hidden = true; }
  });
  document.addEventListener("pointerdown", function (event) { if (!searchBox.contains(event.target)) hits.hidden = true; });

  // ---------------------------------------------------------------- side panel and reading pane
  function bar(status) {
    var counts = mix(status), total = counts.done + counts.progress + counts.planned || 1, node = el("div", "pm-bar");
    ["done", "progress", "planned"].forEach(function (kind) {
      var span = el("span"); span.style.width = (100 * counts[kind] / total) + "%"; span.style.background = color(kind); node.append(span);
    });
    node.setAttribute("aria-label", num(counts.done) + " finished, " + num(counts.progress) + " in progress, " + num(counts.planned) + " planned");
    node.setAttribute("role", "img");
    return node;
  }
  function renderSide() {
    var index = model.index;
    sideInner.replaceChildren();
    sideInner.append(el("h1", "pm-project", plain(index.title)));
    var stats = el("div", "pm-stats"), counts = mix(index.status);
    [[num(index.results), "results"], [num(index.regions.length), index.regions.length === 1 ? "region" : "regions"], [num(counts.done), "finished"]].forEach(function (pair) {
      var stat = el("div", "pm-stat"); stat.append(el("strong", "", pair[0]), el("span", "", pair[1])); stats.append(stat);
    });
    sideInner.append(stats, bar(index.status));
    var legend = el("div", "pm-legend");
    [["done", "finished"], ["progress", "in progress"], ["planned", "planned"]].forEach(function (pair) {
      var item = el("span"), swatch = el("i"); swatch.style.background = color(pair[0]); item.append(swatch, document.createTextNode(pair[1])); legend.append(item);
    });
    sideInner.append(legend);
    var outline = el("ul", "pm-outline");
    index.regions.forEach(function (summary) {
      var item = el("li"), button = el("button");
      button.type = "button"; button.dataset.part = summary.id;
      button.append(el("span", "", plain(summary.title)), el("span", "pm-count", num(summary.results)));
      button.addEventListener("click", function () { select(null, {history: false}); flyToPart(summary.id).catch(report); });
      item.append(button);
      var record = part(summary.id);
      if (record && record.data && record.kids.length) {
        var sub = el("ul");
        record.kids.slice(0, 40).forEach(function (id) {
          var child = part(id), li = el("li"), b = el("button");
          b.type = "button"; b.dataset.part = id;
          b.append(el("span", "", plain(child.summary.title)), el("span", "pm-count", num(child.summary.results)));
          b.addEventListener("click", function () { select(null, {history: false}); flyToPart(id).catch(report); });
          li.append(b); sub.append(li);
        });
        item.append(sub);
      }
      outline.append(item);
    });
    sideInner.append(outline);
    if (index.facets && index.facets.length) {
      var facets = el("fieldset", "pm-facets");
      facets.append(el("legend", "", "Show results from"));
      index.facets.forEach(function (name) {
        var label = el("label"), box = el("input"); box.type = "checkbox"; box.checked = !state.facets || state.facets.has(name);
        box.addEventListener("change", function () {
          var checked = new Set(Array.from(facets.querySelectorAll("input")).filter(function (b) { return b.checked; }).map(function (b) { return b.value; }));
          state.facets = checked.size === index.facets.length ? null : checked;
          schedule();
        });
        box.value = name; label.append(box, document.createTextNode(" " + name)); facets.append(label);
      });
      sideInner.append(facets);
    }
  }
  var shownPart = null;
  function pathLine(chain) {
    var line = el("p", "pm-path");
    chain.forEach(function (id, index) {
      if (index) line.append(document.createTextNode(" / "));
      var button = el("button", "", plain(part(id) ? part(id).summary.title : id)); button.type = "button";
      button.addEventListener("click", function () { select(null, {history: false}); flyToPart(id).catch(report); });
      line.append(button);
    });
    return line;
  }
  function showPart(record) {
    if (!record || state.selected || shownPart === record.id) return;
    shownPart = record.id;
    readerInner.replaceChildren();
    if (record.id === ROOT) {
      readerInner.append(el("p", "pm-kicker", "Whole project"), el("h2", "", plain(model.index.title)));
      readerInner.append(el("p", "", "Each box is a part of the library, sized by its results, with prerequisites placed first. Arrows run from what is used to what uses it: the main links show at rest, and every link of a part shows under the pointer. Zoom in to open a part in place, or search for a theorem by name."));
      readerInner.append(el("h3", "", "Regions"));
      readerInner.append(listOfParts(model.index.regions.map(function (s) { return s.id; })));
      return;
    }
    var summary = record.summary;
    readerInner.append(el("p", "pm-kicker", summary.parts ? "Part of the library" : "Topic"), el("h2", "", plain(summary.title)));
    readerInner.append(pathLine(chainOf(record.id).slice(0, -1)));
    readerInner.append(el("p", "", num(summary.results) + " results" + (summary.parts ? " in " + num(summary.parts) + " parts" : "") + "."), bar(summary.status));
    readerInner.append(el("h3", "", "Used elsewhere (" + num(summary.api_total) + ")"));
    if (summary.api && summary.api.length) {
      var list = el("ul", "pm-list");
      summary.api.forEach(function (row) {
        var item = el("li"), button = el("button"); button.type = "button";
        button.append(el("span", "", plain(row[1])), el("small", "", "used " + num(row[2]) + (row[2] === 1 ? " time" : " times")));
        button.addEventListener("click", function () { arriveById(row[0]).catch(report); });
        item.append(button); list.append(item);
      });
      readerInner.append(list);
      if (summary.api_total > summary.api.length) readerInner.append(el("p", "pm-empty", "and " + num(summary.api_total - summary.api.length) + " more"));
    } else readerInner.append(el("p", "pm-empty", "Nothing outside uses these results."));
    readerInner.append(el("h3", "", "Builds on"), el("p", "", summary.imports_total ? num(summary.imports_total) + " results from elsewhere in the library." : "Nothing outside it."));
    if (record.kids.length) { readerInner.append(el("h3", "", "Parts")); readerInner.append(listOfParts(record.kids)); }
  }
  function listOfParts(ids) {
    var list = el("ul", "pm-list");
    ids.slice(0, 60).forEach(function (id) {
      var record = part(id); if (!record) return;
      var item = el("li"), button = el("button"); button.type = "button";
      button.append(el("span", "", plain(record.summary.title)), el("small", "", num(record.summary.results)));
      button.addEventListener("click", function () { select(null, {history: false}); flyToPart(id).catch(report); });
      item.append(button); list.append(item);
    });
    return list;
  }
  // Formulas go to the site's MathJax runtime (javascripts/mathjax.js), which reads only the spans marked for it.
  function typeset(node) {
    if (window.AutoformMath && typeof window.AutoformMath.refresh === "function") window.AutoformMath.refresh(node);
  }
  // The explorer's reading of a summary: closed code spans and unambiguous inline formulas; anything else stays literal.
  var MARKUP = /\\([\\`$])|(?<![\\`])(`+)(?!`)([^\n]*?)(?<!`)\2(?!`)|(?<![\\$])\$(?![\s$])((?:\\.|[^\\$\n])+?)(?<!\s)\$(?![$\d])|(?<!\\)\\\(((?:\\[^)]|[^\\\n])+?)\\\)/g;
  function withMath(node, text) {
    text = String(text || "");
    var last = 0, match, parts = [];
    MARKUP.lastIndex = 0;
    while ((match = MARKUP.exec(text))) {
      // An unmatched backtick may own later dollars: keep the whole text literal instead of guessing where code ends.
      if (text.slice(last, match.index).indexOf("`") >= 0) { node.append(document.createTextNode(text)); return node; }
      if (match.index > last) parts.push(document.createTextNode(text.slice(last, match.index)));
      parts.push(match[1] !== undefined ? document.createTextNode(match[1]) : match[2] !== undefined ? el("code", "", match[3].trim()) :
        el("span", "arithmatex", "\\(" + (match[4] !== undefined ? match[4] : match[5]) + "\\)"));
      last = MARKUP.lastIndex;
    }
    if (text.slice(last).indexOf("`") >= 0) { node.append(document.createTextNode(text)); return node; }
    if (last < text.length) parts.push(document.createTextNode(text.slice(last)));
    parts.forEach(function (piece) { node.append(piece); });
    return node;
  }
  function statement(text, title) {
    text = String(text || "");
    // Summaries open with the title as their first sentence; the heading already says it.
    if (title && text.indexOf(title) === 0 && /^[.:]\s/.test(text.slice(title.length))) text = text.slice(title.length + 1).trim();
    return withMath(el("p", "pm-statement"), text);
  }
  function showResult(id) {
    var entry = model.results.get(id);
    if (!entry) return;
    shownPart = null;
    var row = entry.row, rel = relationsOf(id);
    readerInner.replaceChildren();
    readerInner.append(el("p", "pm-kicker", "Result"));
    readerInner.append(withMath(el("h2"), row.title));
    readerInner.append(pathLine(chainOf(entry.home)));
    readerInner.append(el("span", "pm-chip", statusWords(row.status) + (row.facet ? " · " + row.facet : "")));
    if (row.summary) readerInner.append(statement(row.summary, row.title));
    if (row.lean) {
      var lean = el("div", "pm-lean"), code = el("code", "", row.lean), copy = el("button", "pm-button", "Copy");
      copy.type = "button"; copy.addEventListener("click", function () { if (navigator.clipboard) navigator.clipboard.writeText(row.lean); });
      lean.append(code, copy); readerInner.append(lean);
    }
    var actions = el("div", "pm-actions");
    if (row.route) { var read = el("a", "", "Read the statement and proof"); read.href = new URL(row.route, routeBase).href; actions.append(read); }
    readerInner.append(actions);
    [["Uses", rel.uses], ["Used by", rel.usedBy]].forEach(function (pair) {
      readerInner.append(el("h3", "", pair[0] + " (" + num(pair[1].length) + ")"));
      if (!pair[1].length) { readerInner.append(el("p", "pm-empty", pair[0] === "Uses" ? "No prerequisites recorded." : "Nothing uses it yet.")); return; }
      var list = el("ul", "pm-list");
      pair[1].slice(0, 80).forEach(function (item) {
        var other = model.results.get(item.id), outside = rel.outside.get(item.id);
        var titleText = other ? other.row.title : outside ? outside[1] : item.id;
        var where = other ? part(other.home).summary.title : outside && outside[2].length ? (part(outside[2][outside[2].length - 1]) || {summary: {title: ""}}).summary.title || "" : "";
        var li = el("li"), button = el("button"); button.type = "button";
        button.append(el("span", "", plain(titleText)), el("small", "", plain(where) + (item.statement ? "" : " · proof")));
        button.addEventListener("click", function () { arriveById(item.id).catch(report); });
        li.append(button); list.append(li);
      });
      readerInner.append(list);
      if (pair[1].length > 80) readerInner.append(el("p", "pm-empty", "and " + num(pair[1].length - 80) + " more"));
    });
    typeset(readerInner);
  }

  // ---------------------------------------------------------------- trail
  function updateTrail() {
    var here = currentPart();
    var chain = state.selected && model.results.get(state.selected) ? chainOf(model.results.get(state.selected).home)
      : here && here.id !== ROOT ? chainOf(here.id) : [];
    var signature = chain.join("|") + "#" + (state.selected || "");
    if (trail.dataset.signature === signature) return;
    trail.dataset.signature = signature;
    trail.replaceChildren();
    function step(text, go, current) {
      var item = el("li"), node = current ? el("span", "", text) : el("button", "", text);
      if (current) item.setAttribute("aria-current", "location");
      else { node.type = "button"; node.title = text; node.addEventListener("click", go); }
      item.append(node); trail.append(item);
    }
    step(plain(model.index.title), wholeProject, !chain.length && !state.selected);
    chain.forEach(function (id, index) {
      step(plain(part(id).summary.title), function () { select(null, {history: false}); flyToPart(id).catch(report); }, index === chain.length - 1 && !state.selected);
    });
    if (state.selected) step(plain(model.results.get(state.selected).row.title), null, true);
  }

  // ---------------------------------------------------------------- history
  function snapshot() {
    return {pm: 1, camera: {x: camera.x, y: camera.y, k: camera.k}, selected: state.selected, focusPart: state.focusPart,
      chain: state.selected && model.results.get(state.selected) ? chainOf(model.results.get(state.selected).home) :
        state.focusPart && state.focusPart !== ROOT ? chainOf(state.focusPart) : []};
  }
  function remember(replace) {
    var snap = snapshot();
    var hash = snap.selected ? "#result=" + encodeURIComponent(snap.selected) :
      snap.focusPart && snap.focusPart !== ROOT ? "#part=" + encodeURIComponent(snap.focusPart) : location.pathname + location.search;
    // The current entry keeps its camera, so Back returns to exactly where the reader was.
    try { history.replaceState(Object.assign({}, history.state || {}, {pm: 1, camera: {x: camera.x, y: camera.y, k: camera.k}, selected: history.state && history.state.selected, focusPart: history.state && history.state.focusPart, chain: history.state && history.state.chain}), ""); } catch (_e) { /* ignore */ }
    if (replace) history.replaceState(snap, "", hash); else history.pushState(snap, "", hash);
  }
  window.addEventListener("popstate", function (event) {
    var snap = event.state;
    if (!snap || !snap.pm) { select(null, {history: false}); state.focusPart = ROOT; fitProject(true); return; }
    ensureChain(snap.chain || []).then(function () {
      state.focusPart = snap.focusPart || null;
      select(snap.selected || null, {history: false});
      if (snap.camera) flyTo(snap.camera);
    }).catch(report);
  });
  // Keep the camera of the current entry up to date as the reader moves around.
  var cameraTimer = 0;
  function rememberCamera() {
    clearTimeout(cameraTimer);
    cameraTimer = setTimeout(function () {
      var current = history.state && history.state.pm ? history.state : snapshot();
      try { history.replaceState(Object.assign({}, current, {camera: {x: camera.x, y: camera.y, k: camera.k}, focusPart: state.focusPart}), ""); } catch (_e) { /* ignore */ }
    }, 250);
  }

  // ---------------------------------------------------------------- input
  var drag = null;
  canvas.addEventListener("wheel", function (event) {
    event.preventDefault();
    var box = canvas.getBoundingClientRect();
    release();
    zoomAt(Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0022)), event.clientX - box.left, event.clientY - box.top);
    rememberCamera();
  }, {passive: false});
  canvas.addEventListener("pointerdown", function (event) {
    if (event.button !== 0) return;
    drag = {x: event.clientX, y: event.clientY, cx: camera.x, cy: camera.y, moved: false, id: event.pointerId};
  });
  canvas.addEventListener("pointermove", function (event) {
    if (!drag) { hover(event.target); return; }
    var dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    if (!drag.moved) { drag.moved = true; canvas.setPointerCapture(drag.id); canvas.classList.add("pm-dragging"); release(); }
    camera = {x: drag.cx - dx / camera.k, y: drag.cy - dy / camera.k, k: camera.k};
    schedule();
  });
  canvas.addEventListener("pointerleave", function () { hover(null); });
  // The part under the pointer shows all of its routes, not only the central ones.
  function hover(target) {
    var node = target && target.closest ? target.closest("[data-part]") : null, id = node ? node.getAttribute("data-part") : null;
    if (id !== state.hoverPart) { state.hoverPart = id; schedule(); }
  }
  function endDrag(event) {
    if (!drag) return;
    var moved = drag.moved; drag = null; canvas.classList.remove("pm-dragging");
    if (moved) { rememberCamera(); return; }
    activate(event.target);
  }
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", function () { drag = null; canvas.classList.remove("pm-dragging"); });
  function activate(target) {
    var resultNode = target.closest && target.closest("[data-result]");
    if (resultNode) { select(resultNode.getAttribute("data-result")); return; }
    var partNode = target.closest && target.closest("[data-part]");
    if (partNode) { select(null, {history: false}); flyToPart(partNode.getAttribute("data-part")).catch(report); return; }
    if (state.selected) select(null);
  }
  canvas.addEventListener("keydown", function (event) {
    var target = event.target, step = 80;
    if (event.key === "Enter" || event.key === " ") {
      if (target !== canvas) { event.preventDefault(); activate(target); }
      return;
    }
    if (target !== canvas && !/^Arrow/.test(event.key) && "+-=0".indexOf(event.key) < 0 && event.key !== "Escape") return;
    if (event.key === "ArrowLeft") camera.x -= step / camera.k;
    else if (event.key === "ArrowRight") camera.x += step / camera.k;
    else if (event.key === "ArrowUp") camera.y -= step / camera.k;
    else if (event.key === "ArrowDown") camera.y += step / camera.k;
    else if (event.key === "+" || event.key === "=") zoomAt(1.35, size.w / 2, size.h / 2);
    else if (event.key === "-") zoomAt(1 / 1.35, size.w / 2, size.h / 2);
    else if (event.key === "0") wholeProject();
    else if (event.key === "Escape") select(null);
    else return;
    if (event.key !== "0" && event.key !== "Escape") release();
    event.preventDefault(); schedule();
    if (event.key !== "0") rememberCamera();
  });
  zoomIn.addEventListener("click", function () { release(); zoomAt(1.5, size.w / 2, size.h / 2); rememberCamera(); });
  zoomOut.addEventListener("click", function () { release(); zoomAt(1 / 1.5, size.w / 2, size.h / 2); rememberCamera(); });
  function wholeProject() { state.focusPart = ROOT; select(null, {history: false}); remember(); fitProject(); }
  fitAll.addEventListener("click", wholeProject);
  document.addEventListener("keydown", function (event) {
    if (event.key === "/" && document.activeElement !== search && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
      event.preventDefault(); search.focus(); search.select();
    }
  });
  // The world is packed for the stage's shape: a resize refits the camera, and turning between portrait and landscape repacks.
  if (window.ResizeObserver) new ResizeObserver(function () {
    if (!stageSize) return;
    measure();
    if ((size.w >= size.h) !== (stageSize.w >= stageSize.h)) {
      stageSize = {w: size.w, h: size.h};
      packRoot();
      fitProject(true);
    }
    schedule();
  }).observe(stage);
  function report(error) {
    live.textContent = "Something went wrong: " + (error && error.message ? error.message : error);
    if (window.console) console.error(error);
  }

  // ---------------------------------------------------------------- start
  fetchJSON("index.json").then(function (index) {
    if (!L) throw new Error("the map's layout script did not load");
    model.index = index;
    var root = {id: ROOT, summary: {id: ROOT, title: index.title, results: index.results, status: index.status, parts: index.regions.length, api: [], api_total: 0, imports_total: 0},
      parent: null, data: {children: index.regions, edges: index.region_edges}, loading: null, box: null, ownBox: null, view: null, kids: [], results: []};
    model.parts.set(ROOT, root);
    index.regions.forEach(function (summary) { register(summary, ROOT); });
    measure();
    stageSize = {w: size.w, h: size.h};
    packRoot();
    // The map opens on the whole project, which the reading pane describes until the reader goes somewhere.
    state.focusPart = ROOT;
    fitProject(true);
    renderSide();
    var hash = location.hash.slice(1), params = new URLSearchParams(hash);
    var start = params.get("result") ? arriveById(params.get("result")).then(function () { remember(true); })
      : params.get("part") ? loadSearch().then(function () {
          var row = model.search.partById.get(params.get("part"));
          if (!row) throw new Error("Unknown part");
          var chain = partChainFromSearch(model.search.data.parts.indexOf(row));
          return ensureChain(chain).then(function () { return flyToPart(params.get("part"), {replace: true}); });
        })
      : Promise.resolve(remember(true));
    start.catch(report);
    schedule();
    // The outline grows as regions load.
    var outlineTimer = 0;
    var observer = new MutationObserver(function () { clearTimeout(outlineTimer); outlineTimer = setTimeout(renderSide, 400); });
    observer.observe(partLayer, {childList: true});
  }).catch(function (error) {
    readerInner.replaceChildren(el("p", "pm-empty", "The map could not load: " + error.message));
  });
})();
