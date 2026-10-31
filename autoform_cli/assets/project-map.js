/* Whole-project map (prototype).
 *
 * One continuous map of the whole project. Regions hold the mathematical parts inside them, and parts hold their
 * results; zooming in opens a part in place, so its boundary and its neighbours stay on screen. Data arrives one level
 * at a time from map/data/: index.json (regions), parts/<shard>.json (a part's children, and its results when
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

  var mapBase = new URL(host.getAttribute("data-map") || "data/", location.href);
  var routeBase = new URL(host.getAttribute("data-routes") || "dependencies/index.json", location.href);
  var reduceMotion = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var SVGNS = "http://www.w3.org/2000/svg";
  var ROOT = "#root";
  var RETRY_MS = 5000;              // a part whose file failed to load is asked for again on its own after this long
  var OPEN_W = 140, OPEN_H = 90;   // a part shows its children (as tiles at first) once its box is this big on screen (px)
  var LABEL_MIN_W = 60;            // boxes narrower than this on screen never get a label
  var KID_LABEL_W = 90;            // a part with parts inside opens once its median child is this wide on screen (px)
  var CARD_TEXT_W = 120;           // result cards carry their titles, and their links, once this wide on screen (px)
  var COMPACT_EM = 7.3, META_EM = 6.6, CARD_EM = 7.2, CARD_META_EM = 6.2;  // mean character advances of the label fonts (px)

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
    // Lines break at ordinary spaces only: a no-break space holds its neighbours together.
    var words = text.split(/[ \t\n]+/), lines = [], line = "";
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
  // One request per file (at most the index, search and 256 part files); a request that fails, or hangs for FETCH_MS,
  // is forgotten, so the next use of the file asks again.
  var cache = new Map(), FETCH_MS = 30000;
  function request(url, name, read) {
    var controller = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (controller) controller.abort(); }, FETCH_MS);
    return fetch(url, controller ? {signal: controller.signal} : undefined).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status + " for " + name);
      return read(response);
    }).then(function (data) { clearTimeout(timer); return data; }, function (error) {
      clearTimeout(timer);
      throw controller && controller.signal.aborted ? new Error("loading " + name + " timed out") : error;
    });
  }
  function fetchJSON(relative) {
    if (!cache.has(relative)) {
      cache.set(relative, request(new URL(relative, mapBase), relative, function (response) { return response.json(); })
        .catch(function (error) { cache.delete(relative); throw error; }));
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
      // Parts share files: each holds the parts whose ids hash to it.
      record.retryAt = 0;
      record.loading = fetchJSON("parts/" + record.summary.file).then(function (shard) {
        var data = shard.parts && shard.parts[record.id];
        if (!data) throw new Error("Part " + record.id + " is missing from parts/" + record.summary.file);
        record.data = data;
        (data.children || []).forEach(function (child) { register(child, record.id); });
        (data.results || []).forEach(function (row) {
          model.results.set(row.id, {row: row, home: record.id, box: null});
          if (record.results.indexOf(row.id) < 0) record.results.push(row.id);
        });
        packChildren(record);
        if (record.parent === ROOT) outlineSoon();
        schedule();
        return record;
      }, function (error) {
        record.loading = null; record.retryAt = performance.now() + RETRY_MS;
        throw error;
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
    var widths = record.kids.map(function (id) { return part(id).box ? part(id).box.w : 0; }).sort(function (a, b) { return a - b; });
    record.kidW = widths.length ? widths[widths.length >> 1] : null;
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
  var zoomOut = el("button", "", "−"), zoomIn = el("button", "", "+"), upLevel = el("button", "", "↑ Up"), fitAll = el("button", "", "Whole project");
  zoomOut.setAttribute("aria-label", "Zoom out"); zoomIn.setAttribute("aria-label", "Zoom in");
  upLevel.setAttribute("aria-label", "Up one level"); upLevel.title = "Up one level (Escape)";
  tools.append(zoomOut, zoomIn, upLevel, fitAll);
  top.append(home, trail, searchBox, tools);

  var side = el("aside", "pm-side"); side.setAttribute("aria-label", "Project outline");
  var sideInner = el("div", "pm-side-inner"); side.append(sideInner);
  var stage = el("main", "pm-stage");
  var canvas = svg("svg", {role: "application", "aria-label": "Map of the whole project. Drag, scroll or use the arrow keys to move; Control or Command with scroll, or plus and minus, to zoom."});
  canvas.setAttribute("tabindex", "0");
  var defs = svg("defs");
  // Markers do not inherit the colour of the line that uses them, so a highlighted route has its own.
  defs.innerHTML = ["pm-arrow", "pm-arrow-on"].map(function (id) {
    return '<marker id="' + id + '" class="' + id + '" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,1 L10,5 L0,9 z"/></marker>';
  }).join("");
  // Routes run through the gaps between boxes, so they draw above every box; labels draw last.
  // A picked part's arrows to its neighbours cross other boxes, so they draw above everything but the labels.
  // A title over its box sits on a backdrop; backdrops have a layer of their own below every label, since keep()
  // appends a node only when it first creates it.
  var partLayer = svg("g"), miniLayer = svg("g"), routeLayer = svg("g"), resultLayer = svg("g"), cardRouteLayer = svg("g"), linkLayer = svg("g"),
    backdropLayer = svg("g"), labelLayer = svg("g");
  canvas.append(defs, partLayer, miniLayer, routeLayer, resultLayer, cardRouteLayer, linkLayer, backdropLayer, labelLayer);
  var pin = el("div", "pm-pin"); pin.hidden = true;
  var hint = el("div", "pm-hint", "");
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
    if (reduceMotion) { camera = target; schedule(); rememberCamera(); if (done) done(); return; }
    var start = {x: camera.x, y: camera.y, k: camera.k}, began = performance.now(), duration = 560;
    flight = {frame: 0};
    var step = function (now) {
      var t = Math.min(1, (now - began) / duration), e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      // Interpolate the scale geometrically, so a deep zoom does not rush its last screens.
      camera = {x: start.x + (target.x - start.x) * e, y: start.y + (target.y - start.y) * e,
        k: Math.exp(Math.log(start.k) + (Math.log(target.k) - Math.log(start.k)) * e)};
      render();
      if (t < 1) flight.frame = requestAnimationFrame(step); else { flight = null; rememberCamera(); if (done) done(); }
    };
    flight.frame = requestAnimationFrame(step);
  }
  // Opening a part fits its content, not its whole box, or comes closer when most of its children would be too small
  // to label (the layout module decides); the layout module's camera holds the stage's top-left corner.
  function flyToContent(record, done) {
    var children = {};
    record.kids.forEach(function (id) { var child = part(id); if (child && child.box) children[id] = child.box; });
    var corner = L.focusCamera(record.box, children, size);
    flyTo({x: corner.x + size.w / 2 / corner.z, y: corner.y + size.h / 2 / corner.z, k: corner.z}, done);
  }
  function zoomAt(factor, sx, sy) {
    var range = limits(), k = clamp(camera.k * factor, range.low, range.high);
    var world = factor > 1 ? towardContent(toWorld(sx, sy)) : toWorld(sx, sy);
    camera = {x: world[0] - (sx - size.w / 2) / k, y: world[1] - (sy - size.h / 2) / k, k: k};
    schedule();
  }
  // Zooming in over a gap or a title heads for what is inside, so a drill lands on content, not between parts: the
  // point that stays under the pointer moves just inside the nearest child or card of the deepest part around it.
  function towardContent(at) {
    var record = part(ROOT);
    if (!record.box || !inBox(record.box, at)) return at;
    for (;;) {
      var targets = [];
      record.kids.forEach(function (id) { if (part(id).box) targets.push({box: part(id).box, part: part(id)}); });
      record.results.forEach(function (id) { var entry = model.results.get(id); if (entry && entry.box) targets.push({box: entry.box}); });
      if (!targets.length) return at;
      var hit = targets.find(function (target) { return inBox(target.box, at); });
      if (hit && !hit.part) return at;
      if (hit) { record = hit.part; continue; }
      var near = targets.reduce(function (best, target) { return distance(target.box, at) < distance(best.box, at) ? target : best; });
      var b = near.box, ix = Math.min(12 / camera.k, b.w / 2), iy = Math.min(12 / camera.k, b.h / 2);
      return [clamp(at[0], b.x + ix, b.x + b.w - ix), clamp(at[1], b.y + iy, b.y + b.h - iy)];
    }
  }
  function inBox(b, at) { return at[0] >= b.x && at[0] <= b.x + b.w && at[1] >= b.y && at[1] <= b.y + b.h; }
  function distance(b, at) { return Math.hypot(Math.max(b.x - at[0], 0, at[0] - b.x - b.w), Math.max(b.y - at[1], 0, at[1] - b.y - b.h)); }
  function fitProject(instant) {
    var box = part(ROOT).box, k = Math.min(size.w / box.w, size.h / box.h), target = {x: box.x + box.w / 2, y: box.y + box.h / 2, k: k};
    // A world taller than the stage (many regions on a narrow screen) opens at full width, top-aligned, and pans down.
    if (box.h / box.w > size.h / size.w * 1.05) { k = size.w / box.w; target = {x: box.x + box.w / 2, y: box.y + size.h / 2 / k, k: k}; }
    if (instant) { camera = target; schedule(); } else flyTo(target);
  }

  // ---------------------------------------------------------------- state
  // focusPart pins the reading pane and trail to a part the reader asked for (ROOT for the whole project) until they
  // move the map themselves; then both follow the part that fills the view.
  // picked: a part chosen with one click, shown where it is with its neighbours (a second click goes inside).
  // links: the shown part's neighbours by role (uses, used-by, both); linkHover: the neighbour under the pointer in the panel.
  var state = {selected: null, related: null, focusPart: null, hoverPart: null, facets: null, picked: null, links: null, linkHover: null, error: null};
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
  // Deep zooms put ancestors' edges tens of millions of pixels away, where the compositor's float32 loses whole pixels;
  // geometry is cut to a margin around the stage first. Clamping each coordinate keeps an orthogonal route's visible
  // part exactly and folds the rest onto the margin, out of sight.
  var MARGIN = 2000;
  function cx(x) { return clamp(x, -MARGIN, size.w + MARGIN); }
  function cy(y) { return clamp(y, -MARGIN, size.h + MARGIN); }
  function placeRect(rect, b) {
    var x = cx(b.x), y = cy(b.y);
    rect.setAttribute("x", x); rect.setAttribute("y", y);
    rect.setAttribute("width", Math.max(1, cx(b.x + b.w) - x)); rect.setAttribute("height", Math.max(1, cy(b.y + b.h) - y));
  }
  function onScreen(b) { return b.x + b.w > -40 && b.x < size.w + 40 && b.y + b.h > -40 && b.y < size.h + 40; }
  // A part with parts inside stays one named tile until most of them could carry a name of their own, so the map
  // reveals the next level at the scale where it can be read, not wherever a parent happens to be large.
  function isOpen(record, b) {
    return record.id === ROOT || (!!record.data && b.w >= OPEN_W && b.h >= OPEN_H && (!record.kidW || record.kidW * camera.k >= KID_LABEL_W));
  }
  // The part the view is in: the deepest open part under the centre that fills most of the stage.
  function covers(b) {
    return b.x <= size.w / 2 && b.x + b.w >= size.w / 2 && b.y <= size.h / 2 && b.y + b.h >= size.h / 2 &&
      Math.min(b.w, size.w) * Math.min(b.h, size.h) >= size.w * size.h * 0.45;
  }
  var deepestAtCenter = null;
  // The part the view is in: the pinned title and the empty-stage hint follow it. The panel and trail follow a picked part.
  function viewPart() { return state.focusPart ? part(state.focusPart) || part(ROOT) : deepestAtCenter; }
  function currentPart() { return state.picked && part(state.picked) ? part(state.picked) : viewPart(); }
  function render() {
    if (!part(ROOT) || !part(ROOT).box) return;
    measure();
    used = new Set();
    deepestAtCenter = part(ROOT);
    var onPath = state.selected && model.results.get(state.selected) ? new Set(chainOf(model.results.get(state.selected).home)) : null;
    var labelBoxes = {}, closed = {}, opened = [], communities = [], drawn = new Set();
    (function visit(record) {
      if (!record.box) return;
      var b = screenBox(record.box);
      if (record.id !== ROOT && !onScreen(b)) return;
      if (record.id !== ROOT && !record.data && b.w >= OPEN_W && b.h >= OPEN_H && !(record.retryAt > performance.now())) load(record.id).catch(report);
      var open = isOpen(record, b);
      if (record.id !== ROOT) {
        if (open && covers(b)) deepestAtCenter = record;
        drawPart(record, b, open, onPath);
        drawn.add(record.id);
        if (b.w >= LABEL_MIN_W) labelBoxes[record.id] = record.box;
        if (!open) closed[record.id] = true;
      }
      if (!open) return;
      opened.push(record);
      record.kids.forEach(function (id) { visit(part(id)); });
      if (record.view) communities.push(record);
      // Beside other children, a part's own results have a band of their own to name them.
      if (record.ownBox && record.ownBox.band && record.kids.length && screenBox(record.ownBox).w >= LABEL_MIN_W) labelBoxes[record.id + "#results"] = record.ownBox;
    })(part(ROOT));
    var slots = L.placeLabels(labelBoxes, cornerCamera(), size, closed), labelled = new Set(slots.map(function (slot) { return slot.id; }));
    opened.forEach(function (record) { drawRoutes(record, labelled); });
    communities.forEach(function (record) { if (drawCommunity(record)) drawn.add(record.id + "#results"); });
    drawLinks();
    drawLabels(slots);
    drawPin();
    explainEmpty(drawn);
    keyed.forEach(function (node, key) { if (!used.has(key)) { node.remove(); keyed.delete(key); } });
    updateTrail();
    if (!state.selected) showPart(currentPart());
  }
  function drawPart(record, b, open, onPath) {
    var dim = state.related && !state.related.parts.has(record.id);
    var holder = keep(partLayer, "part:" + record.id, "g", {
      class: "pm-part " + (open ? "pm-open" : "pm-tile pm-tile-" + tileKind(record.summary.status)) + (dim ? " pm-dimmed" : "") +
        (state.focusPart === record.id ? " pm-focus" : "") + (onPath && onPath.has(record.id) ? " pm-on-path" : "") +
        (!state.selected && state.links && state.links.has(record.id) ? " pm-" + state.links.get(record.id) : "") +
        (state.linkHover === record.id ? " pm-link-hover" : "") + (state.picked === record.id ? " pm-picked" : ""),
      tabindex: open ? -1 : 0, role: "button", "data-part": record.id,
      "aria-label": plain(record.summary.title) + ", " + num(record.summary.results) + " results" +
        (record.summary.api_total ? ", " + num(record.summary.api_total) + " used elsewhere" : "")});
    var shape = holder.firstChild || holder.appendChild(svg("rect"));
    placeRect(shape, b);
    shape.setAttribute("rx", clamp(Math.min(b.w, b.h) * 0.05, 1.5, 10));
    if (!holder.querySelector("title")) holder.appendChild(svg("title")).textContent = plain(record.summary.title);
    // Closed, a part shows its parts faintly, sized by their results and tinted by their status, where they will sit
    // once it opens; the names come then.
    if (!open && b.w >= 120 && b.h >= 80) record.kids.forEach(function (id) {
      var kid = part(id);
      if (!kid.box) return;
      var mini = keep(miniLayer, "mini:" + id, "rect", {class: "pm-mini pm-mini-" + tileKind(kid.summary.status), rx: 2});
      placeRect(mini, screenBox(kid.box));
    });
  }
  // An orthogonal route as a screen path, its corners rounded to radius.
  function routePath(points, radius) {
    var pts = [];
    for (var i = 0; i < points.length; i += 2) { var at = toScreen(points[i], points[i + 1]); pts.push([cx(at[0]), cy(at[1])]); }
    // Corners stay tight when the camera is far closer than the route's own level.
    var r = Math.min(radius * camera.k, 14), d = "M" + pts[0][0].toFixed(1) + "," + pts[0][1].toFixed(1);
    for (i = 1; i < pts.length - 1; i++) {
      var p0 = pts[i - 1], p1 = pts[i], p2 = pts[i + 1];
      var l1 = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]), l2 = Math.hypot(p2[0] - p1[0], p2[1] - p1[1]);
      // A leg that clamping folded to nothing keeps its corner, so the path never cuts a diagonal.
      if (!l1 || !l2) { d += "L" + p1[0].toFixed(1) + "," + p1[1].toFixed(1); continue; }
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
  // The routes between an open part's children: the central ones at rest, once both ends carry a label (a route between
  // two anonymous tiles is noise), and every route of the part in focus or under the pointer.
  function drawRoutes(record, labelled) {
    var routes = routesAt.get(record.id) || [];
    // With a part in focus, links elsewhere fade, so its own links and its outlined neighbours stand out.
    var focus = !state.selected && state.focusPart !== ROOT ? state.focusPart : null;
    var elsewhere = !!focus && record.id !== focus && chainOf(record.id).indexOf(focus) < 0;
    routes.forEach(function (route) {
      // With a result selected, its part's own links to its neighbours are beside the point; the pointer still shows them.
      var involved = [state.selected || state.picked ? null : state.focusPart, state.hoverPart].some(function (id) { return id && (route.from === id || route.to === id); });
      var resting = route.rest && labelled.has(route.from) && labelled.has(route.to);
      if ((!resting && !involved) || !route.points || !routeVisible(route.points)) return;
      var dim = state.related ? !(state.related.parts.has(route.from) && state.related.parts.has(route.to)) :
        state.picked ? route.from !== state.hoverPart && route.to !== state.hoverPart : elsewhere && !involved;
      var path = keep(routeLayer, "route:" + record.id + ":" + route.from + ">" + route.to, "path", {
        d: routePath(route.points, route.radius),
        class: "pm-route" + (route.kind === "proof" ? " pm-proof" : "") + (involved ? " pm-involved" : "") + (dim ? " pm-dimmed" : ""),
        "stroke-width": route.tier === 3 ? 3 : route.tier === 2 ? 2 : 1.25,
        "marker-end": arrow(involved), "marker-start": route.both ? arrow(involved) : "none"});
      setText(path.firstChild || path.appendChild(svg("title")), linkWords(route, function (id) { return plain((part(id) || {summary: {title: id}}).summary.title); }));
      if (involved && !route.both) keep(routeLayer, "rflow:" + record.id + ":" + route.from + ">" + route.to, "path", {d: path.getAttribute("d"), class: "pm-route pm-involved pm-flowing"});
    });
  }
  // The picked part's arrows, straight to each neighbour the map has a box for: in from what it builds on, out to what
  // uses it, thicker for more results. Dots flow along each from what is used to what uses it. A pair linked both
  // ways gets two arrows, bowed apart.
  function drawLinks() {
    var record = !state.selected && state.picked ? part(state.picked) : null, links = record && record.data && record.data.links;
    if (!links || !record.box) return;
    var here = screenBox(record.box), rows = links.uses.concat(links.used_by);
    var most = Math.max.apply(null, rows.map(function (row) { return row[3]; }).concat([1]));
    [["uses", links.uses], ["used-by", links.used_by]].forEach(function (pair) {
      pair[1].forEach(function (row) {
        var other = part(row[0]);
        if (!other || !other.box) return;
        var there = screenBox(other.box), ends = pair[0] === "uses" ? [there, here] : [here, there];
        var away = there.x > size.w || there.y > size.h || there.x + there.w < 0 || there.y + there.h < 0;
        var d = bow(ends[0], ends[1]), width = (1.5 + 3 * Math.sqrt(row[3] / most)).toFixed(2);
        var path = keep(linkLayer, "link:" + pair[0] + ":" + row[0], "path", {
          d: d, class: "pm-link" + (state.linkHover === row[0] ? " pm-link-hover" : "") + (away ? " pm-link-away" : ""),
          "stroke-width": width, "marker-end": arrow(true)});
        keep(linkLayer, "flow:" + pair[0] + ":" + row[0], "path", {d: d, class: "pm-link pm-flowing" + (away ? " pm-link-away" : ""), "stroke-width": width});
        var title = plain(record.summary.title), name = plain(row[1]), count = num(row[3]) + (row[3] === 1 ? " result" : " results");
        setText(path.firstChild || path.appendChild(svg("title")),
          pair[0] === "uses" ? title + " uses " + count + " in " + name : count + " in " + name + " use " + title);
      });
    });
  }
  // A gentle curve from one box's edge to the other's, bowed to its left, so a reverse arrow bows the other way. Boxes
  // side by side would leave only a stub in the gap between them, so then the arrow runs between their middles.
  function bow(a, b) {
    var p = edgeToward(a, [b.x + b.w / 2, b.y + b.h / 2]), q = edgeToward(b, [a.x + a.w / 2, a.y + a.h / 2]);
    if (Math.hypot(q[0] - p[0], q[1] - p[1]) < 60) {
      p = edgeToward(inset(a), [b.x + b.w / 2, b.y + b.h / 2]); q = edgeToward(inset(b), [a.x + a.w / 2, a.y + a.h / 2]);
    }
    var dx = q[0] - p[0], dy = q[1] - p[1], length = Math.hypot(dx, dy) || 1, bend = Math.min(90, length * 0.18);
    var m = [(p[0] + q[0]) / 2 + dy / length * bend, (p[1] + q[1]) / 2 - dx / length * bend];
    return "M" + cx(p[0]).toFixed(1) + "," + cy(p[1]).toFixed(1) + " Q" + cx(m[0]).toFixed(1) + "," + cy(m[1]).toFixed(1) +
      " " + cx(q[0]).toFixed(1) + "," + cy(q[1]).toFixed(1);
  }
  function inset(box) { return {x: box.x + box.w * 0.3, y: box.y + box.h * 0.3, w: box.w * 0.4, h: box.h * 0.4}; }
  function edgeToward(box, point) {
    var c = [box.x + box.w / 2, box.y + box.h / 2], dx = point[0] - c[0], dy = point[1] - c[1];
    var s = Math.min(dx ? box.w / 2 / Math.abs(dx) : Infinity, dy ? box.h / 2 / Math.abs(dy) : Infinity, 1);
    return [c[0] + dx * s, c[1] + dy * s];
  }
  // A part's own results: status tiles while small, then cards with their titles and the links between them.
  function drawCommunity(record) {
    var view = record.view, first = view.cards[record.results[0]], shown = 0;
    var text = !!first && first.w * camera.k >= CARD_TEXT_W;
    record.results.forEach(function (id) { if (drawCard(model.results.get(id), view, text)) shown++; });
    if (!text) return shown > 0;
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
        if (involved && !route.both) keep(cardRouteLayer, "cflow:" + route.from + ">" + route.to, "path", {d: path.getAttribute("d"), class: "pm-route pm-involved pm-flowing"});
      });
    return shown > 0;
  }
  function drawCard(entry, view, text) {
    if (!entry || !entry.box) return false;
    var b = screenBox(entry.box);
    if (!onScreen(b) || b.w < 1) return false;
    var row = entry.row, isSelected = row.id === state.selected;
    var dim = state.related ? !state.related.results.has(row.id) : state.facets && row.facet && !state.facets.has(row.facet);
    var holder = keep(resultLayer, "result:" + row.id, "g", {
      class: "pm-result pm-" + group(row.status) + (text ? " pm-card" : "") + (isSelected ? " pm-selected" : "") + (dim ? " pm-dimmed" : ""),
      tabindex: 0, role: "button", "aria-label": plain(row.title) + ", " + statusWords(row.status), "data-result": row.id});
    var card = holder.firstChild || holder.appendChild(svg("rect"));
    placeRect(card, b);
    card.setAttribute("rx", clamp(b.w * 0.04, 1, 8));
    if (!holder.querySelector("title")) holder.appendChild(svg("title")).textContent = plain(row.title);
    if (!text) return true;
    var pad = 10, width = b.w - 2 * pad, cls = dim ? " pm-dimmed" : "";
    var lines = wrap(plain(row.title), Math.max(4, Math.floor(width / CARD_EM)), b.h >= 58 ? 2 : 1);
    lines.forEach(function (line, index) {
      setText(keep(labelLayer, "ct:" + row.id + ":" + index, "text", {x: b.x + pad, y: b.y + pad + 13 + index * 17, class: "pm-card-title" + cls}), line);
    });
    if (b.h >= 52) {
      // The card's fill says it is finished, the common case; the line names any other status.
      var stub = view.stubs[row.id], meta = [tileKind(row.status) === "done" ? "" : statusWords(row.status),
        stub && stub.usedBy ? "used " + num(stub.usedBy) + "× elsewhere" : ""].filter(Boolean).join(" · ");
      if (meta) setText(keep(labelLayer, "cm:" + row.id, "text", {x: b.x + pad, y: b.y + b.h - pad, class: "pm-card-meta" + cls}), clip(meta, Math.max(4, Math.floor(width / CARD_META_EM))));
    }
    return true;
  }
  // Titles and progress in each box's header band, at fixed sizes, where the layout module finds room for them. A
  // closed tile's title wraps down the tile, up to the lines the module allows. A box whose band is still too thin gets its title over its top, on a backdrop in
  // its own fill; nothing inside it is labelled yet, so nothing is covered.
  function drawLabels(slots) {
    slots.forEach(function (slot) {
      var own = /#results$/.test(slot.id), record = part(own ? slot.id.slice(0, -"#results".length) : slot.id);
      if (!record) return;
      var cls = state.related && !state.related.parts.has(record.id) ? " pm-dimmed" : "";
      var count = own ? record.results.length : 0, short = num(count) + (count === 1 ? " result" : " results");
      // A part's own results say whose they are when the line has room, and just how many when it does not.
      var long = short + " directly in " + plain(record.summary.title);
      var title = own ? (!slot.compact && long.length * L.constants.TITLE_EM <= slot.w ? long : short) : plain(record.summary.title);
      if (slot.overlay) {
        var b = screenBox(own ? record.ownBox : record.box), backdrop = keep(backdropLayer, "lb:" + slot.id, "rect", {
          class: "pm-backdrop pm-backdrop-" + (own ? "open" : isOpen(record, b) ? "open" : tileKind(record.summary.status)) + cls, rx: 6});
        placeRect(backdrop, {x: b.x + 1, y: b.y + 1, w: b.w - 2, h: slot.band});
      }
      if (slot.compact) {
        wrap(title, Math.max(3, Math.floor(slot.w / COMPACT_EM)), slot.lines).forEach(function (line, index) {
          setText(keep(labelLayer, "lc:" + slot.id + ":" + index, "text", {x: slot.x, y: slot.baseline + index * L.constants.COMPACT_LINE,
            class: "pm-title pm-compact" + cls}), line);
        });
        return;
      }
      // The title as large as its room allows, from 15 to 30 px: the band of an open part, the top half of a closed
      // tile. At 15 px it keeps the layout module's lines and baseline.
      var box = screenBox(own ? record.ownBox : record.box), shut = !own && !isOpen(record, box);
      var room = shut && !slot.overlay ? box.h / 2 : slot.band, meta = slot.detail !== "title" && !own;
      // A box running off the left of the stage keeps its title on screen, inside the box.
      var x = slot.x, width = slot.w;
      if (slot.x < 12 && slot.x + slot.w - 12 >= 80) { x = 12; width = slot.x + slot.w - 12; }
      function perLine(size) { return Math.max(4, Math.floor(width / (L.constants.TITLE_EM * size / 15))); }
      function stepOf(size) { return size === 15 ? L.constants.LINE : Math.round(size * 1.2); }
      // The last line's descenders stay inside the room, clear of the parts below an open band.
      function mostOf(size) { return size === 15 ? slot.lines : Math.max(1, Math.floor((room - (meta ? 22 : Math.ceil(size / 4))) / stepOf(size))); }
      function fits(size) {
        var n = perLine(size);
        return title.split(/[ \t\n]+/).every(function (word) { return word.length <= n; }) && wrap(title, n, 99).length <= mostOf(size);
      }
      // As large as the room allows, then smaller until the whole title fits; at 15 px a long one is clipped.
      var size = slot.overlay ? 15 : Math.round(clamp(Math.min(room * 0.4, width / 7), 15, 30));
      while (size > 15 && !fits(size)) size--;
      var step = stepOf(size), most = mostOf(size);
      var lines = most > 1 ? wrap(title, perLine(size), most) : [clip(title, perLine(size))];
      var y = size === 15 ? slot.baseline : slot.y + Math.round(size * 1.2);
      lines.forEach(function (line, index) {
        setText(keep(labelLayer, "lt:" + slot.id + ":" + index, "text", {x: x, y: y + index * step,
          class: "pm-title" + (shut ? " pm-halo" : "") + cls, style: size === 15 ? "" : "font-size:" + size + "px"}), line);
      });
      if (!meta) return;
      // How many results, and a bar for how far along they are.
      var metaY = y + (lines.length - 1) * step + 18, words = num(record.summary.results) + (record.summary.results === 1 ? " result" : " results");
      setText(keep(labelLayer, "lm:" + slot.id, "text", {x: x, y: metaY, class: "pm-meta" + (shut ? " pm-halo" : "") + cls}), clip(words, Math.floor(width / META_EM)));
      var barX = x + words.length * META_EM + 8, barW = Math.min(90, x + width - barX);
      if (barW >= 24) progressBar("lp:" + slot.id, barX, metaY - 6, barW, record.summary.status, cls);
    });
  }
  function progressBar(key, x, y, width, status, cls) {
    var counts = mix(status), total = counts.done + counts.progress + counts.planned || 1, at = x;
    ["done", "progress", "planned"].forEach(function (kind) {
      var w = width * counts[kind] / total;
      keep(labelLayer, key + ":" + kind, "rect", {x: at.toFixed(1), y: y.toFixed(1), width: Math.max(0, w).toFixed(1), height: 5, class: "pm-seg pm-seg-" + kind + cls});
      at += w;
    });
  }
  // Zoomed into a gap, the stage would be blank under the pinned title: say where the reader is and how to get out.
  var HINT = "Click a part to see its links, again to go inside; Escape goes back up. Pinch or Control-scroll to zoom.";
  function explainEmpty(drawn) {
    var record = viewPart(), text = state.error || HINT;
    if (!state.error && record && record.id !== ROOT && record.data && !state.selected) {
      var inside = record.kids.some(function (id) { return drawn.has(id); }) || drawn.has(record.id + "#results");
      if (!inside && (record.kids.length || record.results.length)) text = "You are between the parts of " + plain(record.summary.title) + ". Zoom out, or choose a part in the reading pane.";
    }
    if (hint.textContent !== text) hint.textContent = text;
    hint.classList.toggle("pm-hint-strong", text !== HINT);
  }
  // Once the part the view is in has its header band scrolled off, its title stays pinned to the top of the stage.
  function drawPin() {
    var record = viewPart();
    if (!record || record.id === ROOT || !record.box) { pin.hidden = true; return; }
    var b = screenBox(record.box), band = (record.box.band || L.constants.BAND) * (record.box.unit || 1) * camera.k;
    // The title line sits at the top of the band, however tall the band has grown: pin once that line is off the stage.
    if (b.y + Math.min(30, band / 2 + 6) >= 12 || b.y + b.h < 60) { pin.hidden = true; return; }
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
    if (id) state.picked = null;
    state.selected = id;
    state.related = id ? relatedSets(id) : null;
    if (id) showResult(id); else showPart(currentPart());
    if (options.history !== false) remember(options.replace);
    schedule();
  }

  // ---------------------------------------------------------------- arriving somewhere
  // Each navigation takes a number; one whose files arrive after a newer navigation began gives way to it.
  var navEpoch = 0;
  function navigation() { state.error = null; return ++navEpoch; }
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
  // Any part, loaded or not, with its chain loaded: search knows every part's chain.
  function ensurePart(id) {
    if (part(id)) return ensureChain(chainOf(id));
    return loadSearch().then(function () {
      var row = model.search.partById.get(id);
      if (!row) throw new Error("Unknown part " + id);
      return ensureChain(partChainFromSearch(model.search.data.parts.indexOf(row)));
    });
  }
  function goToPart(id, options) {
    var epoch = navigation();
    return ensurePart(id).then(function () { if (epoch === navEpoch) return flyToPart(id, Object.assign({}, options, {epoch: epoch})); });
  }
  // One click picks a part where it is: the reading pane and the map show what it builds on and what uses it. The
  // neighbours' parts load too, so the map can draw an arrow to each.
  function pick(id) {
    navigation();
    state.picked = id; shownPart = null;
    select(null, {history: false});
    return load(id).then(function () {
      shownPart = null; schedule();
      var links = part(id) && part(id).data && part(id).data.links;
      if (!links) return;
      return Promise.all(links.uses.concat(links.used_by).map(function (row) {
        return part(row[0]) && part(row[0]).box ? null : ensurePart(row[0]).then(schedule, function () { /* the pane still lists it */ });
      }));
    });
  }
  function unpick() { if (state.picked) { state.picked = null; shownPart = null; schedule(); } }
  // From a list: the part where it sits among its siblings, picked, with the view widened to take in its neighbours.
  function showInContext(id) {
    var epoch = navigation();
    return ensurePart(id).then(function () {
      if (epoch !== navEpoch) return;
      var record = part(id), parent = part(record.parent);
      state.focusPart = parent ? parent.id : ROOT;
      var picking = pick(id);
      epoch = navEpoch;
      remember();
      return picking.then(function () { if (epoch === navEpoch) flyToNeighbourhood(record, parent); });
    });
  }
  // Never so far out that the picked part is narrower than this on screen (px): far neighbours' arrows run off the stage.
  var PICKED_MIN_W = 100;
  function flyToNeighbourhood(record, parent) {
    var area = parent && parent.id !== ROOT ? L.contentOf(parent.box) : part(ROOT).box, links = record.data && record.data.links;
    var x0 = area.x, y0 = area.y, x1 = area.x + area.w, y1 = area.y + area.h;
    (links ? links.uses.concat(links.used_by) : []).forEach(function (row) {
      var box = part(row[0]) && part(row[0]).box;
      if (!box) return;
      x0 = Math.min(x0, box.x); y0 = Math.min(y0, box.y); x1 = Math.max(x1, box.x + box.w); y1 = Math.max(y1, box.y + box.h);
    });
    var wide = {x: x0, y: y0, w: x1 - x0, h: y1 - y0}, k = Math.max(fitK(wide, 0.92), Math.min(fitK(area), PICKED_MIN_W / record.box.w));
    // Aim at the middle of the neighbourhood, moved only as far as keeps the picked part on the stage.
    var at = centerOf(wide), b = record.box, halfW = size.w / 2 / k, halfH = size.h / 2 / k, margin = 40 / k;
    flyTo({x: clamp(at[0], b.x + b.w + margin - halfW, b.x - margin + halfW), y: clamp(at[1], b.y + b.h + margin - halfH, b.y - margin + halfH), k: k});
  }
  function flyToPart(id, options) {
    options = options || {};
    var epoch = options.epoch || navigation();
    if (state.picked) { state.picked = null; shownPart = null; }
    return ensureChain(chainOf(id)).then(function () {
      if (epoch !== navEpoch) return;
      var record = part(id);
      state.focusPart = id;
      if (options.history !== false) remember(options.replace);
      flyToContent(record);
      showPart(record);
    });
  }
  function arrive(resultId, chain, options) {
    options = options || {};
    var epoch = options.epoch || navigation();
    return ensureChain(chain).then(function () {
      if (epoch !== navEpoch) return;
      var homeId = chain[chain.length - 1], record = part(homeId), entry = model.results.get(resultId);
      if (!entry || !entry.box) throw new Error("Result not found in its part: " + resultId);
      state.focusPart = homeId;
      select(resultId, {history: false});
      if (options.history !== false) remember(options.replace);
      // Close enough that the result's card carries its title, with as much of its part around it as fits. The card
      // sits in the upper third, under the pinned title, so its part's band scrolls off and more of its neighbours
      // show below it. A card the layout left without area gets its part's view instead of an endless zoom.
      if (entry.box.w > 0 && entry.box.h > 0) {
        var at = centerOf(entry.box), k = Math.max(fitK(L.contentOf(record.box)), (CARD_TEXT_W * 1.9) / entry.box.w);
        flyTo({x: at[0], y: at[1] + size.h * 0.2 / k, k: k});
      } else flyToContent(record);
      live.textContent = "Showing " + plain(entry.row.title);
    });
  }
  // A result the map has not loaded yet (from a list in the reading pane): find its path through search.
  function arriveById(resultId, options) {
    var entry = model.results.get(resultId), epoch = navigation();
    options = Object.assign({}, options, {epoch: epoch});
    if (entry) return arrive(resultId, chainOf(entry.home), options);
    return loadSearch().then(function () {
      if (epoch !== navEpoch) return;
      var hit = model.search.byId.get(resultId);
      if (!hit) throw new Error("Unknown result " + resultId);
      return arrive(resultId, partChainFromSearch(hit[2]), options);
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
          var id = typeof row[3] === "string" ? data.parts[row[2]][0] + "/" + row[3] : row[3][0];
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
    var index = model.index, active = document.activeElement, focused = active && sideInner.contains(active) ? active.dataset.part : null;
    var scroll = side.scrollTop;
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
    var key = el("div", "pm-edge-key");
    [["", 1.25, "points to what uses it"], [" pm-proof", 1.25, "used only in proofs"], ["", 3, "more links"]].forEach(function (row) {
      var item = el("span"), sample = svg("svg", {width: 30, height: 10, "aria-hidden": "true"});
      sample.appendChild(svg("path", {d: "M2,5H24", class: "pm-route" + row[0], "stroke-width": row[1], "marker-end": arrow(false)}));
      item.append(sample, document.createTextNode(row[2])); key.append(item);
    });
    sideInner.append(key);
    var outline = el("ul", "pm-outline");
    index.regions.forEach(function (summary) {
      var item = el("li"), button = el("button");
      button.type = "button"; button.dataset.part = summary.id;
      button.append(el("span", "", plain(summary.title)), el("span", "pm-count", num(summary.results)));
      button.addEventListener("click", function () { showInContext(summary.id).catch(report); });
      item.append(button);
      var record = part(summary.id);
      if (record && record.data && record.kids.length) {
        var sub = el("ul");
        record.kids.slice(0, 40).forEach(function (id) {
          var child = part(id), li = el("li"), b = el("button");
          b.type = "button"; b.dataset.part = id;
          b.append(el("span", "", plain(child.summary.title)), el("span", "pm-count", num(child.summary.results)));
          b.addEventListener("click", function () { showInContext(id).catch(report); });
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
          schedule(); rememberCamera();
        });
        box.value = name; label.append(box, document.createTextNode(" " + name)); facets.append(label);
      });
      sideInner.append(facets);
    }
    side.scrollTop = scroll;
    var again = focused && Array.from(sideInner.querySelectorAll("button")).find(function (button) { return button.dataset.part === focused; });
    if (again) again.focus();
  }
  // The outline lists each region's parts once they arrive.
  var outlineTimer = 0;
  function outlineSoon() { clearTimeout(outlineTimer); outlineTimer = setTimeout(renderSide, 400); }
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
    var links = record.id !== ROOT && record.data && record.data.links;
    state.links = links ? new Map() : null;
    if (links) {
      links.uses.forEach(function (row) { state.links.set(row[0], "uses"); });
      links.used_by.forEach(function (row) { state.links.set(row[0], state.links.has(row[0]) ? "both" : "used-by"); });
    }
    schedule();
    readerInner.replaceChildren();
    if (record.id === ROOT) {
      var index = model.index;
      readerInner.append(el("p", "pm-kicker", "Whole project"), el("h2", "", plain(index.title)));
      readerInner.append(el("p", "", num(index.results) + " results in " + num(index.regions.length) + (index.regions.length === 1 ? " region." : " regions.")), bar(index.status));
      var flows = (index.region_edges || []).slice().sort(function (a, b) { return b[2] + b[3] - a[2] - a[3]; });
      if (flows.length) {
        readerInner.append(el("h3", "", "How the regions build on each other"));
        readerInner.append(paged(flows, 8, function (edge) {
          var li = el("li", "pm-flow"), user = part(edge[0]), used = part(edge[1]);
          li.append(el("span", "", plain(user ? user.summary.title : edge[0]) + " uses " + plain(used ? used.summary.title : edge[1])),
            el("small", "", num(edge[2] + edge[3]) + (edge[2] + edge[3] === 1 ? " link" : " links")));
          return li;
        }));
      }
      return;
    }
    var summary = record.summary;
    readerInner.append(el("p", "pm-kicker", summary.parts ? "Part of the library" : "Topic"), el("h2", "", plain(summary.title)));
    readerInner.append(pathLine(chainOf(record.id).slice(0, -1)));
    readerInner.append(el("p", "", num(summary.results) + " results" + (summary.parts ? " in " + num(summary.parts) + " parts" : "") + "."), bar(summary.status));
    if (links) {
      var most = Math.max.apply(null, links.uses.concat(links.used_by).map(function (row) { return row[3]; }).concat([1]));
      var home = part(record.parent) && record.parent !== ROOT ? part(record.parent).summary.title : null;
      readerInner.append(linkList("Builds on", "uses", links.uses, links.uses_total, most, home, function (row) {
        return "Uses " + num(row[3]) + (row[3] === 1 ? " result" : " results") + " in " + plain(row[1]);
      }));
      readerInner.append(linkList("Used by", "used-by", links.used_by, links.used_by_total, most, home, function (row) {
        return num(row[3]) + (row[3] === 1 ? " result" : " results") + " in " + plain(row[1]) + (row[3] === 1 ? " uses it" : " use it");
      }));
      if (links.uses.length || links.used_by.length) readerInner.append(el("p", "pm-empty pm-link-note", "On the map, the parts in view are outlined, and dots flow from what is used to what uses it. Bars count the results involved."));
    }
    if (record.kids.length) { readerInner.append(el("h3", "", "Parts (" + num(record.kids.length) + ")")); readerInner.append(listOfParts(record.kids)); }
    if (summary.api && summary.api.length) {
      // Single results come last, folded: the parts above say more about how this one fits.
      var more = el("details", "pm-more"), list = el("ul", "pm-list");
      more.append(el("summary", "", "Most-used results (" + num(summary.api_total) + ")"));
      summary.api.forEach(function (row) {
        var item = el("li"), button = el("button"); button.type = "button";
        button.append(el("span", "", plain(row[1])), el("small", "", "used " + num(row[2]) + (row[2] === 1 ? " time" : " times")));
        button.addEventListener("click", function () { arriveById(row[0]).catch(report); });
        item.append(button); list.append(item);
      });
      more.append(list);
      if (summary.api_total > summary.api.length) more.append(el("p", "pm-empty", "The " + num(summary.api.length) + " most used are listed."));
      readerInner.append(more);
    }
  }
  // Neighbouring parts for the panel, biggest first, each with a bar for how many results the link involves.
  function linkList(heading, role, rows, total, most, home, describe) {
    var section = el("section", "pm-links pm-" + role), title = el("h3");
    title.append(document.createTextNode(heading + (total ? " · " + num(total) + (total === 1 ? " part" : " parts") : "")));
    section.append(title);
    if (!rows.length) { section.append(el("p", "pm-empty", role === "uses" ? "Nothing outside it." : "Nothing outside uses it.")); return section; }
    var list = el("ul", "pm-list pm-link-list");
    rows.forEach(function (row) {
      var item = el("li"), button = el("button"), name = el("span", "pm-link-name"), meter = el("span", "pm-meter"), fill = el("i");
      button.type = "button"; button.title = describe(row);
      name.append(el("span", "", plain(row[1])));
      // Where a neighbour sits, when that is not beside this part.
      if (row[2] && row[2] !== home) name.append(el("small", "", plain(row[2])));
      fill.style.width = Math.max(4, Math.round(100 * row[3] / most)) + "%"; meter.append(fill);
      button.append(name, meter, el("small", "", num(row[3])));
      button.addEventListener("click", function () { state.linkHover = null; showInContext(row[0]).catch(report); });
      button.addEventListener("mouseenter", function () { state.linkHover = row[0]; schedule(); });
      button.addEventListener("mouseleave", function () { state.linkHover = null; schedule(); });
      item.append(button); list.append(item);
    });
    section.append(list);
    if (total > rows.length) section.append(el("p", "pm-empty", "The " + num(rows.length) + " with the most links are listed."));
    return section;
  }
  function listOfParts(ids) {
    return paged(ids.filter(part), 60, function (id) {
      var record = part(id), item = el("li"), button = el("button"); button.type = "button";
      button.append(el("span", "", plain(record.summary.title)), el("small", "", num(record.summary.results)));
      button.addEventListener("click", function () { showInContext(id).catch(report); });
      item.append(button);
      return item;
    });
  }
  // A list that shows its first items and pages through the rest, so every entry stays reachable.
  function paged(items, step, render) {
    var box = el("div", "pm-paged"), list = el("ul", "pm-list"), more = el("button", "pm-show-more"), shown = 0;
    more.type = "button";
    function page() {
      items.slice(shown, shown + step).forEach(function (item) { list.append(render(item)); });
      shown = Math.min(items.length, shown + step);
      more.textContent = "Show more (" + num(items.length - shown) + " left)";
      more.hidden = shown >= items.length;
      typeset(list);
    }
    more.addEventListener("click", page);
    page();
    box.append(list, more);
    return box;
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
  // A link from published data: http(s) only, no credentials; anything else is dropped.
  function safeLink(value, base) {
    if (typeof value !== "string" || !value || /[\u0000-\u001f\u007f]/.test(value)) return null;
    try { var url = new URL(value, base); return /^https?:$/.test(url.protocol) && !url.username && !url.password ? url.href : null; }
    catch (_error) { return null; }
  }
  // The book's page for a result holds its complete statement, proof and sources; the reading pane shows that block
  // instead of the index's summary. The last PAGES pages stay parsed.
  var pages = new Map(), PAGES = 6, shownResult = 0;
  function pageAt(url) {
    var key = url.split("#")[0];
    if (!pages.has(key)) {
      if (pages.size >= PAGES) pages.delete(pages.keys().next().value);
      pages.set(key, request(key, "the result's page", function (response) { return response.text(); })
        .then(function (text) { return new DOMParser().parseFromString(text, "text/html"); })
        .catch(function (error) { pages.delete(key); throw error; }));
    }
    return pages.get(key);
  }
  // The result's block, cleaned as the explorer cleans what it borrows: no scripts or embeds, no handlers or inline
  // styles, links rebased to the page and kept only when safe, and no ids to clash with this page's. Its heading and
  // dependency list go, since the pane has its own.
  function borrowed(page, id, base) {
    var block = Array.from(page.querySelectorAll(".bp-thmwrapper")).find(function (node) { return node.getAttribute("data-autoform-node-id") === id; });
    if (!block) return null;
    var copy = document.importNode(block, true), source = copy.querySelector(".bp-code-link");
    source = source && safeLink(source.getAttribute("href"), base);
    copy.querySelectorAll(".bp-thmheading, .bp-dependencies, script, iframe, object, embed, base, link, style, form").forEach(function (node) { node.remove(); });
    [copy].concat(Array.from(copy.querySelectorAll("*"))).forEach(function (node) {
      Array.from(node.attributes).forEach(function (attribute) {
        var name = attribute.name.toLowerCase();
        if (/^on/.test(name) || name === "srcdoc" || name === "style" || name === "id") node.removeAttribute(attribute.name);
      });
      ["href", "src"].forEach(function (name) {
        var value = node.getAttribute(name);
        if (value == null) return;
        var safe = safeLink(value, base);
        if (safe) node.setAttribute(name, safe); else node.removeAttribute(name);
      });
    });
    return {block: copy, source: source};
  }
  function showResult(id) {
    var entry = model.results.get(id);
    if (!entry) return;
    shownPart = null;
    var row = entry.row, rel = relationsOf(id), shown = ++shownResult;
    readerInner.replaceChildren();
    readerInner.append(el("p", "pm-kicker", "Result"));
    readerInner.append(withMath(el("h2"), row.title));
    readerInner.append(pathLine(chainOf(entry.home)));
    readerInner.append(el("span", "pm-chip", statusWords(row.status) + (row.facet ? " · " + row.facet : "")));
    var summary = row.summary ? statement(row.summary, row.title) : null;
    if (summary) readerInner.append(summary);
    var lean = el("div", "pm-lean");
    if (row.lean) { lean.append(el("code", "", row.lean)); readerInner.append(lean); }
    var actions = el("div", "pm-actions"), page = row.route && safeLink(row.route, routeBase);
    if (page) { var read = el("a", "", "Open in the book"); read.href = page; actions.append(read); }
    readerInner.append(actions);
    if (page && window.DOMParser) {
      pageAt(page).then(function (parsed) {
        var math = shown === shownResult && state.selected === id && borrowed(parsed, id, page);
        if (!math) return;
        var holder = el("div", "pm-math"); holder.append(math.block);
        if (summary) summary.replaceWith(holder); else (lean.parentNode ? lean : actions).before(holder);
        if (math.source && row.lean) { var link = el("a", "", "Lean source"); link.href = math.source; lean.append(link); }
        typeset(holder);
      }).catch(function () { /* the summary and the link to the book stay */ });
    }
    [["Uses", rel.uses], ["Used by", rel.usedBy]].forEach(function (pair) {
      readerInner.append(el("h3", "", pair[0] + " (" + num(pair[1].length) + ")"));
      if (!pair[1].length) { readerInner.append(el("p", "pm-empty", pair[0] === "Uses" ? "No prerequisites recorded." : "Nothing uses it yet.")); return; }
      readerInner.append(paged(pair[1], 12, function (item) {
        var other = model.results.get(item.id), outside = rel.outside.get(item.id);
        var titleText = other ? other.row.title : outside ? outside[1] : item.id;
        var where = other ? part(other.home).summary.title : outside && outside[2].length ? (part(outside[2][outside[2].length - 1]) || {summary: {title: ""}}).summary.title || "" : "";
        var li = el("li"), button = el("button"); button.type = "button";
        button.append(el("span", "", plain(titleText)), el("small", "", plain(where) + (item.statement ? "" : " · proof")));
        button.addEventListener("click", function () { arriveById(item.id).catch(report); });
        li.append(button);
        return li;
      }));
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
    return {pm: 1, camera: {x: camera.x, y: camera.y, k: camera.k}, selected: state.selected, focusPart: state.focusPart, picked: state.picked,
      chain: state.selected && model.results.get(state.selected) ? chainOf(model.results.get(state.selected).home) :
        state.focusPart && state.focusPart !== ROOT ? chainOf(state.focusPart) : []};
  }
  // What Back restores besides the camera and the selection.
  function viewState() {
    return {query: search.value, facets: state.facets ? Array.from(state.facets) : null, readerScroll: reader.scrollTop, sideScroll: side.scrollTop};
  }
  function remember(replace) {
    var snap = snapshot();
    var hash = snap.selected ? "#result=" + encodeURIComponent(snap.selected) :
      snap.focusPart && snap.focusPart !== ROOT ? "#part=" + encodeURIComponent(snap.focusPart) : location.pathname + location.search;
    // The current entry keeps its camera, so Back returns to exactly where the reader was.
    try { history.replaceState(Object.assign({}, history.state || {}, {pm: 1, camera: {x: camera.x, y: camera.y, k: camera.k}, selected: history.state && history.state.selected, focusPart: history.state && history.state.focusPart, chain: history.state && history.state.chain}, viewState()), ""); } catch (_e) { /* ignore */ }
    // Going nowhere new (Whole project from the whole project, Escape with nothing chosen) is not a new entry.
    var current = history.state, same = current && current.pm && current.selected === snap.selected &&
      current.focusPart === snap.focusPart && current.picked === snap.picked;
    if (replace || same) history.replaceState(Object.assign(snap, viewState()), "", hash); else history.pushState(snap, "", hash);
  }
  window.addEventListener("popstate", function (event) {
    var snap = event.state;
    var epoch = navigation();
    if (!snap || !snap.pm) { state.picked = null; select(null, {history: false}); state.focusPart = ROOT; fitProject(true); live.textContent = "Whole project"; return; }
    ensureChain(snap.chain || []).then(function () {
      if (epoch !== navEpoch) return;
      state.focusPart = snap.focusPart || null;
      state.picked = snap.picked && part(snap.picked) ? snap.picked : null;
      search.value = snap.query || "";
      var facets = snap.facets ? new Set(snap.facets) : null;
      if (String(facets && Array.from(facets)) !== String(state.facets && Array.from(state.facets))) { state.facets = facets; renderSide(); }
      select(snap.selected || null, {history: false});
      reader.scrollTop = snap.readerScroll || 0;
      side.scrollTop = snap.sideScroll || 0;
      var here = snap.selected && model.results.get(snap.selected);
      live.textContent = here ? "Showing " + plain(here.row.title) : state.focusPart && state.focusPart !== ROOT && part(state.focusPart) ?
        "Showing " + plain(part(state.focusPart).summary.title) : "Whole project";
      if (snap.camera) flyTo(snap.camera);
    }).catch(report);
  });
  // Keep the camera of the current entry up to date as the reader moves around.
  var cameraTimer = 0;
  function rememberCamera() {
    clearTimeout(cameraTimer);
    cameraTimer = setTimeout(function () {
      var current = history.state && history.state.pm ? history.state : snapshot();
      try { history.replaceState(Object.assign({}, current, {camera: {x: camera.x, y: camera.y, k: camera.k}, focusPart: state.focusPart}, viewState()), ""); } catch (_e) { /* ignore */ }
    }, 250);
  }
  reader.addEventListener("scroll", rememberCamera);
  side.addEventListener("scroll", rememberCamera);

  // ---------------------------------------------------------------- input
  var drag = null;
  // As in the explorer: scrolling (a two-finger swipe) pans, and Control or Command with scroll zooms. Browsers send a
  // trackpad pinch as a Control scroll.
  canvas.addEventListener("wheel", function (event) {
    event.preventDefault();
    var box = canvas.getBoundingClientRect(), pixels = event.deltaMode === 1 ? 25 : 1;
    release();
    if (event.ctrlKey || event.metaKey) {
      zoomAt(clamp(Math.exp(-event.deltaY * pixels * 0.002), 0.5, 2), event.clientX - box.left, event.clientY - box.top);
    } else {
      camera = {x: camera.x + event.deltaX * pixels / camera.k, y: camera.y + event.deltaY * pixels / camera.k, k: camera.k};
      schedule();
    }
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
    if (resultNode) { navigation(); select(resultNode.getAttribute("data-result")); return; }
    var partNode = target.closest && target.closest("[data-part]");
    if (partNode) {
      var id = partNode.getAttribute("data-part");
      if (state.picked === id) flyToPart(id).catch(report); else pick(id).catch(report);
      return;
    }
    if (state.selected) select(null); else unpick();
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
    else if (event.key === "Escape") escape();
    else return;
    if (event.key !== "0" && event.key !== "Escape") release();
    event.preventDefault(); schedule();
    if (event.key !== "0") rememberCamera();
  });
  zoomIn.addEventListener("click", function () { release(); zoomAt(1.5, size.w / 2, size.h / 2); rememberCamera(); });
  zoomOut.addEventListener("click", function () { release(); zoomAt(1 / 1.5, size.w / 2, size.h / 2); rememberCamera(); });
  function wholeProject() { navigation(); state.focusPart = ROOT; state.picked = null; select(null, {history: false}); remember(); fitProject(); live.textContent = "Whole project"; }
  fitAll.addEventListener("click", wholeProject);
  // One level up from where the reader is: from a result to its part, from a part to the part around it.
  function upOneLevel() {
    if (state.selected) {
      var home = model.results.get(state.selected) && model.results.get(state.selected).home;
      select(null, {history: false});
      if (home) flyToPart(home).catch(report);
      return;
    }
    var here = state.focusPart && state.focusPart !== ROOT && part(state.focusPart) || viewPart();
    if (!here || here.id === ROOT) return;
    if (here.parent === ROOT || !part(here.parent)) wholeProject(); else flyToPart(here.parent).catch(report);
  }
  // Escape lets go of what is chosen first, then goes up a level.
  function escape() { if (state.selected) select(null); else if (state.picked) unpick(); else upOneLevel(); }
  upLevel.addEventListener("click", function () { unpick(); upOneLevel(); });
  document.addEventListener("keydown", function (event) {
    var active = document.activeElement, typing = !!active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName);
    if (event.key === "/" && active !== search && !typing) {
      event.preventDefault(); search.focus(); search.select();
    } else if (event.key === "Escape" && !typing && !event.defaultPrevented) { event.preventDefault(); escape(); }
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
    state.error = "Something went wrong: " + (error && error.message ? error.message : error) + ". Try again, or reload the page.";
    live.textContent = state.error;
    schedule();
    if (window.console) console.error(error);
  }

  // ---------------------------------------------------------------- start
  fetchJSON("index.json").then(function (index) {
    if (!L) throw new Error("the map's layout script did not load");
    model.index = index;
    if (!index.regions.length) {
      readerInner.replaceChildren(el("p", "pm-kicker", "Whole project"), el("h2", "", plain(index.title)),
        el("p", "pm-empty", "Nothing is on the map yet: the roadmap has no results."));
      return;
    }
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
    var start = params.get("result") ? arriveById(params.get("result"), {replace: true})
      : params.get("part") ? goToPart(params.get("part"), {replace: true})
      : Promise.resolve(remember(true));
    start.catch(report);
    schedule();
  }).catch(function (error) {
    readerInner.replaceChildren(el("p", "pm-empty", "The map could not load: " + error.message));
  });
})();
