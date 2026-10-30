/* Whole-project map (prototype).
 *
 * One continuous map of the whole project. Regions hold the mathematical parts inside them, and parts hold their
 * results; zooming in opens a part in place, so its boundary and its neighbours stay on screen. Data arrives one level
 * at a time from dependencies/map/: index.json (regions), parts/<file>.json (a part's children, and its results when
 * it holds them) and search.json (titles and Lean names, loaded on first search).
 *
 * Layout lives in `layout` below. It is a deterministic stand-in (spiral packing, straight links) that the layout
 * engine replaces without touching the controller.
 */
(function () {
  "use strict";

  var host = document.querySelector("[data-project-map]");
  if (!host || host.dataset.pmMounted) return;
  host.dataset.pmMounted = "1";

  var mapBase = new URL(host.getAttribute("data-map") || "dependencies/map/", location.href);
  var routeBase = new URL(host.getAttribute("data-routes") || "dependencies/index.json", location.href);
  var reduceMotion = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var SVGNS = "http://www.w3.org/2000/svg";
  var ROOT = "#root";
  var OPEN_W = 300, OPEN_H = 190;  // a part opens in place once its box is this big on screen (px)
  var FETCH_SHARE = 0.6;           // and starts loading at this share of that size
  var LABEL_W = 46;                // closed parts narrower than this draw no label
  var RESULT_LABEL_PX = 9;         // results label themselves once their card is this wide
  var WORLD_W = 1600;              // the project's box in world units; its height follows the stage's shape

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
  function color(kind) {
    return getComputedStyle(document.documentElement).getPropertyValue(kind === "done" ? "--pm-proved" : kind === "progress" ? "--pm-progress" : "--pm-planned").trim() || "#999";
  }

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
    var record = {id: summary.id, summary: summary, parent: parentId, data: null, loading: null, box: null, ownBox: null, kids: [], results: []};
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
        layoutApi().packPart(record, record.data);
        schedule();
        return record;
      });
    }
    return record.loading;
  }

  // ---------------------------------------------------------------- layout (stand-in for the layout engine)
  // Boxes: every part is a rectangle {x, y, w, h} with a header band for its title; children share the rest by area.
  var BAND = 0.075, PAD = 0.018;
  function content(box, record) {
    var band = record.id === ROOT ? 0 : Math.max(box.h * BAND, Math.min(box.w, box.h) * 0.06), pad = Math.min(box.w, box.h) * PAD;
    return {x: box.x + pad, y: box.y + band + pad, w: Math.max(1, box.w - 2 * pad), h: Math.max(1, box.h - band - 2 * pad)};
  }
  var stub = {
    // Place a part's children (and a box for its own results) inside its content area, in proportion to their results.
    packPart: function (record) {
      if (!record.box) return;
      var inner = content(record.box, record);
      var items = record.kids.map(function (id) { return {id: id, kind: "part", size: Math.max(1, part(id).summary.results) + 1}; });
      if (record.results.length) items.push({id: OWN + record.id, kind: "own", size: record.results.length + 1});
      squarify(items, inner);
      items.forEach(function (item) {
        if (item.kind === "own") { record.ownBox = item.box; placeResults(record, item.box); return; }
        var child = part(item.id);
        child.box = item.box;
        if (child.data) stub.packPart(child);
      });
    },
  };
  var OWN = "own:";
  function layoutApi() { return window.AutoformMapLayout || stub; }
  // Squarified treemap: rows of near-square boxes, largest first, ties by id, so the same data gives the same map.
  function squarify(items, box) {
    items.sort(function (a, b) { return b.size - a.size || (a.id < b.id ? -1 : 1); });
    var total = items.reduce(function (sum, item) { return sum + item.size; }, 0) || 1, scale = box.w * box.h / total;
    var rest = {x: box.x, y: box.y, w: box.w, h: box.h}, queue = items.slice(), row = [];
    function worst(row, side) {
      var sum = 0, max = 0, min = Infinity;
      row.forEach(function (item) { var a = item.size * scale; sum += a; max = Math.max(max, a); min = Math.min(min, a); });
      return Math.max(side * side * max / (sum * sum), (sum * sum) / (side * side * min));
    }
    function lay(row) {
      var sum = row.reduce(function (s2, item) { return s2 + item.size * scale; }, 0), horizontal = rest.w >= rest.h;
      var thick = sum / (horizontal ? rest.h : rest.w), at = horizontal ? rest.y : rest.x;
      row.forEach(function (item) {
        var span = item.size * scale / thick;
        item.box = horizontal ? {x: rest.x, y: at, w: thick, h: span} : {x: at, y: rest.y, w: span, h: thick};
        at += span;
      });
      if (horizontal) { rest.x += thick; rest.w -= thick; } else { rest.y += thick; rest.h -= thick; }
    }
    while (queue.length) {
      var item = queue[0], side = Math.min(rest.w, rest.h);
      if (!row.length || worst(row.concat([item]), side) <= worst(row, side)) { row.push(queue.shift()); }
      else { lay(row); row = []; }
    }
    if (row.length) lay(row);
    // A small inset keeps neighbours apart.
    items.forEach(function (item) {
      var inset = Math.min(item.box.w, item.box.h) * 0.04;
      item.box = {x: item.box.x + inset, y: item.box.y + inset, w: Math.max(0.1, item.box.w - 2 * inset), h: Math.max(0.1, item.box.h - 2 * inset)};
    });
  }
  // Results in a grid of square cells inside their box.
  function placeResults(record, box) {
    var n = record.results.length, cols = Math.max(1, Math.ceil(Math.sqrt(n * box.w / box.h))), rows = Math.ceil(n / cols);
    var cell = Math.min(box.w / cols, box.h / rows), x0 = box.x + (box.w - cell * cols) / 2, y0 = box.y + (box.h - cell * rows) / 2;
    record.results.slice().sort().forEach(function (id, index) {
      var col = index % cols, line = Math.floor(index / cols);
      model.results.get(id).box = {x: x0 + col * cell + cell * 0.12, y: y0 + line * cell + cell * 0.12, w: cell * 0.76, h: cell * 0.76};
    });
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
  defs.innerHTML = '<marker id="pm-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,1 L10,5 L0,9 z" fill="currentColor"/></marker>';
  var edgeLayer = svg("g"), partLayer = svg("g"), resultLayer = svg("g"), relationLayer = svg("g"), labelLayer = svg("g");
  canvas.append(defs, edgeLayer, partLayer, resultLayer, relationLayer, labelLayer);
  var hint = el("div", "pm-hint", "Scroll to zoom into a region. Search to jump to a theorem.");
  stage.append(canvas, hint);
  var reader = el("aside", "pm-reader"); reader.setAttribute("aria-label", "Reading pane");
  var readerInner = el("div", "pm-reader-inner"); reader.append(readerInner);
  var live = el("p", "pm-sr"); live.setAttribute("aria-live", "polite");
  app.append(top, side, stage, reader, live);
  host.append(app);

  // ---------------------------------------------------------------- camera
  var camera = {x: 0, y: 0, k: 1};
  var size = {w: 1, h: 1};
  function measure() {
    var box = canvas.getBoundingClientRect();
    size.w = Math.max(1, box.width); size.h = Math.max(1, box.height);
  }
  function fitK(box, share) { return Math.min(size.w / box.w, size.h / box.h) * (share || 0.94); }
  function centerOf(box) { return [box.x + box.w / 2, box.y + box.h / 2]; }
  function toScreen(x, y) { return [(x - camera.x) * camera.k + size.w / 2, (y - camera.y) * camera.k + size.h / 2]; }
  function toWorld(sx, sy) { return [(sx - size.w / 2) / camera.k + camera.x, (sy - size.h / 2) / camera.k + camera.y]; }
  function screenBox(box) {
    var a = toScreen(box.x, box.y);
    return {x: a[0], y: a[1], w: box.w * camera.k, h: box.h * camera.k};
  }
  function limits() {
    var root = part(ROOT);
    return {low: fitK(root.box, 0.5), high: fitK(root.box) * 60000};
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
  function flyToBox(box, share, done) { var c = centerOf(box); flyTo({x: c[0], y: c[1], k: fitK(box, share || 0.9)}, done); }
  function zoomAt(factor, sx, sy) {
    var range = limits(), k = clamp(camera.k * factor, range.low, range.high);
    var world = toWorld(sx, sy);
    camera = {x: world[0] - (sx - size.w / 2) / k, y: world[1] - (sy - size.h / 2) / k, k: k};
    schedule();
  }

  // ---------------------------------------------------------------- state
  var state = {selected: null, related: null, focusPart: null, facets: null, hover: null};
  var pending = false;
  function schedule() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () { pending = false; render(); });
  }

  // ---------------------------------------------------------------- render
  var keyed = new Map(), used = new Set();
  function keep(layer, key, tag, attrs) {
    var node = keyed.get(key);
    if (!node) { node = svg(tag); node.dataset.key = key; keyed.set(key, node); }
    if (node.parentNode !== layer) layer.appendChild(node);
    for (var name in attrs) if (node.getAttribute(name) !== String(attrs[name])) node.setAttribute(name, attrs[name]);
    used.add(key);
    return node;
  }
  function onScreen(b) { return b.x + b.w > -40 && b.x < size.w + 40 && b.y + b.h > -40 && b.y < size.h + 40; }
  function isOpen(record, b) { return record.id === ROOT || (b.w > OPEN_W && b.h > OPEN_H && !!record.data); }
  // A thin bar along the header shows how much of the part is finished, in progress and planned.
  function statusStrip(record, b, dim) {
    var counts = mix(record.summary.status), total = counts.done + counts.progress + counts.planned;
    if (!total || b.w < 24) return;
    var x = b.x, height = clamp(b.h * 0.012, 2, 4);
    ["done", "progress", "planned"].forEach(function (kind) {
      var w = b.w * counts[kind] / total;
      keep(partLayer, "strip:" + kind + ":" + record.id, "rect", {x: x, y: b.y, width: Math.max(0, w), height: height,
        fill: color(kind), class: dim ? "pm-dimmed" : "", "pointer-events": "none"});
      x += w;
    });
  }
  var deepestAtCenter = null;
  function render() {
    measure();
    used = new Set();
    deepestAtCenter = part(ROOT);
    var related = state.related, selected = state.selected;
    var drawEdgesFor = [];
    (function visit(record) {
      if (!record.box) return;
      var b = screenBox(record.box);
      if (record.id !== ROOT && !onScreen(b)) return;
      var open = isOpen(record, b);
      if (record.id !== ROOT && !record.data && b.w > OPEN_W * FETCH_SHARE && b.h > OPEN_H * FETCH_SHARE) load(record.id);
      if (record.id !== ROOT) {
        // The part the reader is in: the deepest open part under the centre that fills most of the view.
        if (open && b.x <= size.w / 2 && b.x + b.w >= size.w / 2 && b.y <= size.h / 2 && b.y + b.h >= size.h / 2 &&
            Math.min(b.w, size.w) * Math.min(b.h, size.h) >= size.w * size.h * 0.45) deepestAtCenter = record;
        var dim = related && !related.parts.has(record.id);
        var holder = keep(partLayer, "part:" + record.id, "g", {class: "pm-part" + (open ? " pm-open" : "") + (dim ? " pm-dimmed" : "") +
          (state.focusPart === record.id ? " pm-focus" : ""), tabindex: open ? -1 : 0, role: "button", "data-part": record.id,
          "aria-label": plain(record.summary.title) + ", " + num(record.summary.results) + " results" + (record.summary.api_total ? ", " + num(record.summary.api_total) + " used elsewhere" : "")});
        var shape = holder.firstChild || holder.appendChild(svg("rect"));
        shape.setAttribute("x", b.x); shape.setAttribute("y", b.y); shape.setAttribute("width", Math.max(1, b.w)); shape.setAttribute("height", Math.max(1, b.h));
        shape.setAttribute("rx", clamp(Math.min(b.w, b.h) * 0.04, 2, 12));
        if (!holder.querySelector("title")) holder.appendChild(svg("title")).textContent = plain(record.summary.title);
        statusStrip(record, b, dim);
        labelPart(record, b, open, dim);
      }
      if (open) {
        drawEdgesFor.push(record);
        record.kids.forEach(function (id) { visit(part(id)); });
        record.results.forEach(function (id) { drawResult(model.results.get(id), related, selected); });
      }
    })(part(ROOT));
    drawEdgesFor.forEach(drawEdges);
    drawRelations();
    keyed.forEach(function (node, key) { if (!used.has(key)) { node.remove(); keyed.delete(key); } });
    updateTrail();
    if (!state.selected) showPart(state.focusPart ? part(state.focusPart) : deepestAtCenter);
  }
  function labelPart(record, b, open, dim) {
    var title = plain(record.summary.title);
    if (open) {
      var band = Math.max(b.h * BAND, Math.min(b.w, b.h) * 0.06);
      var fontSize = clamp(band * 0.42, 12, 22);
      keep(labelLayer, "label:" + record.id, "text", {x: b.x + clamp(b.w * 0.012, 8, 18), y: b.y + band * 0.58, "dominant-baseline": "middle",
        class: "pm-label" + (dim ? " pm-dimmed" : ""), "font-size": fontSize}).textContent = clip(title, Math.floor(b.w / (fontSize * 0.56)));
      if (b.w > 420) {
        keep(labelLayer, "sub:" + record.id, "text", {x: b.x + b.w - clamp(b.w * 0.012, 8, 18), y: b.y + band * 0.58, "dominant-baseline": "middle",
          "text-anchor": "end", class: "pm-sub" + (dim ? " pm-dimmed" : "")}).textContent =
          num(record.summary.results) + " results" + (record.summary.api_total ? " · " + num(record.summary.api_total) + " used elsewhere" : "");
      }
      return;
    }
    if (b.w < LABEL_W || b.h < 18) return;
    var fontSize = clamp(Math.min(b.w / 9, b.h / 4.2), 10, 20);
    var lines = wrap(title, Math.max(4, Math.floor((b.w - 12) / (fontSize * 0.56))), b.h > fontSize * 5 ? 3 : b.h > fontSize * 3 ? 2 : 1);
    var lineHeight = fontSize * 1.18, x = b.x + clamp(b.w * 0.06, 5, 14), y0 = b.y + clamp(b.h * 0.12, 6, 18) + fontSize * 0.5;
    lines.forEach(function (line, index) {
      keep(labelLayer, "label:" + record.id + ":" + index, "text", {x: x, y: y0 + index * lineHeight, "dominant-baseline": "middle",
        class: "pm-label" + (dim ? " pm-dimmed" : ""), "font-size": fontSize}).textContent = line;
    });
    var y = y0 + lines.length * lineHeight + 2;
    if (b.h > y - b.y + 16) {
      keep(labelLayer, "sub:" + record.id, "text", {x: x, y: y, "dominant-baseline": "middle", class: "pm-sub" + (dim ? " pm-dimmed" : "")}).textContent =
        num(record.summary.results) + " results" + (record.summary.parts ? " · " + num(record.summary.parts) + " parts" : "");
      y += 18;
    }
    // A big closed part lists its public API: the results other parts use most.
    var api = record.summary.api || [], room = Math.floor((b.y + b.h - y - 10) / 17);
    if (api.length && room >= 2 && b.w > 160) {
      keep(labelLayer, "apihead:" + record.id, "text", {x: x, y: y + 4, "dominant-baseline": "middle", class: "pm-sub" + (dim ? " pm-dimmed" : "")})
        .textContent = "Used elsewhere (" + num(record.summary.api_total) + "):";
      api.slice(0, Math.min(6, room - 1)).forEach(function (row, index) {
        keep(labelLayer, "api:" + record.id + ":" + index, "text", {x: x + 6, y: y + 4 + 17 * (index + 1), "dominant-baseline": "middle",
          class: "pm-api" + (dim ? " pm-dimmed" : "")}).textContent = clip(plain(row[1]), Math.floor((b.w - 40) / 6.6)) + "  ×" + row[2];
      });
    }
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
  function drawResult(entry, related, selected) {
    if (!entry || !entry.box) return;
    var b = screenBox(entry.box);
    if (!onScreen(b) || b.w < 1.2) return;
    var row = entry.row, dim = related ? !related.results.has(row.id) && row.id !== selected : state.facets && row.facet && !state.facets.has(row.facet);
    var holder = keep(resultLayer, "result:" + row.id, "g", {class: "pm-result" + (row.id === selected ? " pm-selected" : "") + (dim ? " pm-dimmed" : ""),
      tabindex: 0, role: "button", "aria-label": plain(row.title) + ", " + statusWords(row.status), "data-result": row.id});
    var card = holder.firstChild || holder.appendChild(svg("rect"));
    card.setAttribute("x", b.x); card.setAttribute("y", b.y); card.setAttribute("width", b.w); card.setAttribute("height", b.h);
    card.setAttribute("rx", clamp(b.w * 0.18, 1, 6)); card.setAttribute("fill", color(group(row.status)));
    if (!holder.querySelector("title")) holder.appendChild(svg("title")).textContent = plain(row.title);
    if (b.w >= RESULT_LABEL_PX * 4) {
      var fontSize = clamp(b.w / 9, 10, 15), lines = wrap(plain(row.title), Math.max(4, Math.floor(b.w / (fontSize * 0.55))), Math.max(1, Math.min(4, Math.floor(b.h / (fontSize * 1.3)))));
      lines.forEach(function (line, index) {
        keep(labelLayer, "rlabel:" + row.id + ":" + index, "text", {x: b.x + b.w / 2, y: b.y + b.h / 2 + (index - (lines.length - 1) / 2) * fontSize * 1.2,
          "text-anchor": "middle", "dominant-baseline": "middle", class: "pm-card-label" + (dim ? " pm-dimmed" : ""), "font-size": fontSize}).textContent = line;
      });
    } else if (b.w >= RESULT_LABEL_PX || row.id === selected || (related && related.results.has(row.id))) {
      keep(labelLayer, "rlabel:" + row.id + ":0", "text", {x: b.x + b.w + 4, y: b.y + b.h / 2, "dominant-baseline": "middle",
        class: "pm-label pm-small" + (dim ? " pm-dimmed" : "")}).textContent = clip(plain(row.title), 40);
    }
  }
  // Where the segment between two boxes' centres leaves each box.
  function clipToBox(from, to, box) {
    var hw = box.w / 2, hh = box.h / 2, dx = to[0] - from[0], dy = to[1] - from[1];
    var t = Math.min(dx ? hw / Math.abs(dx) : Infinity, dy ? hh / Math.abs(dy) : Infinity, 1);
    return [from[0] + dx * t, from[1] + dy * t];
  }
  function edgeEnds(a, b) {
    var sa = screenBox(a), sb = screenBox(b), ca = [sa.x + sa.w / 2, sa.y + sa.h / 2], cb = [sb.x + sb.w / 2, sb.y + sb.h / 2];
    var pa = clipToBox(ca, cb, sa), pb = clipToBox(cb, ca, sb);
    return [pa[0], pa[1], pb[0], pb[1], Math.hypot(pb[0] - pa[0], pb[1] - pa[1])];
  }
  // Links between the parts inside an open part, summed per pair; a pair that uses each other draws one line.
  function drawEdges(record) {
    var rows = record.id === ROOT ? (model.index.region_edges || []) : ((record.data && record.data.edges) || []);
    var pairs = new Map();
    rows.forEach(function (row) {
      var key = row[0] < row[1] ? row[0] + "|" + row[1] : row[1] + "|" + row[0], entry = pairs.get(key);
      if (!entry) { entry = {a: row[0] < row[1] ? row[0] : row[1], b: row[0] < row[1] ? row[1] : row[0], ab: 0, ba: 0}; pairs.set(key, entry); }
      if (row[0] === entry.a) entry.ab += row[2] + row[3]; else entry.ba += row[2] + row[3];
    });
    pairs.forEach(function (pair, key) {
      var a = part(pair.a), b = part(pair.b);
      if (!a || !b || !a.box || !b.box) return;
      var ends = edgeEnds(a.box, b.box);
      if (ends[4] < 6) return;
      var weight = pair.ab + pair.ba, width = clamp(0.6 + Math.log2(1 + weight) * 0.45, 0.8, 6);
      var dim = state.related && !(state.related.parts.has(pair.a) && state.related.parts.has(pair.b));
      // Arrows point from the part that is used to the part that uses it: knowledge flows along the line.
      var bend = Math.min(60, ends[4] * 0.18), mx = (ends[0] + ends[2]) / 2 - (ends[3] - ends[1]) / (ends[4] || 1) * bend,
        my = (ends[1] + ends[3]) / 2 + (ends[2] - ends[0]) / (ends[4] || 1) * bend;
      var line = keep(edgeLayer, "edge:" + key, "path", {d: "M" + ends[0] + "," + ends[1] + "Q" + mx + "," + my + " " + ends[2] + "," + ends[3],
        class: "pm-edge" + (dim ? " pm-dimmed" : ""), "stroke-width": width,
        "marker-start": pair.ab ? "url(#pm-arrow)" : "", "marker-end": pair.ba ? "url(#pm-arrow)" : ""});
      var label = plain(a.summary.title) + " uses " + plain(b.summary.title) + ": " + num(pair.ab) + " links" +
        (pair.ba ? ". " + plain(b.summary.title) + " uses " + plain(a.summary.title) + ": " + num(pair.ba) + " links" : "");
      (line.firstChild || line.appendChild(svg("title"))).textContent = label;
    });
  }

  // ---------------------------------------------------------------- selection and relationships
  // Where a result's line ends: its own dot when its part is open, else the innermost part on its path that is drawn.
  function anchorFor(id, chain) {
    var entry = model.results.get(id);
    if (entry && entry.box) {
      var owner = part(entry.home);
      if (owner && owner.box && isOpen(owner, screenBox(owner.box))) return {box: entry.box, result: true};
    }
    var path = chain || (entry ? chainOf(entry.home) : []), best = null;
    for (var i = 0; i < path.length; i++) {
      var record = part(path[i]);
      if (!record || !record.box) break;
      best = record;
      if (!isOpen(record, screenBox(record.box))) break;
    }
    return best ? {box: best.box, part: best.id} : null;
  }
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
  function drawRelations() {
    if (!state.selected) return;
    var from = anchorFor(state.selected);
    if (!from) return;
    var rel = relationsOf(state.selected);
    [["uses", rel.uses], ["used-by", rel.usedBy]].forEach(function (pair) {
      pair[1].forEach(function (item) {
        var outside = rel.outside.get(item.id), to = anchorFor(item.id, outside ? outside[2] : null);
        if (!to) return;
        var fa = centerOf(from.box), fb = centerOf(to.box), a = toScreen(fa[0], fa[1]), b = toScreen(fb[0], fb[1]);
        var midX = (a[0] + b[0]) / 2, midY = (a[1] + b[1]) / 2 - Math.hypot(b[0] - a[0], b[1] - a[1]) * 0.12;
        keep(relationLayer, "rel:" + pair[0] + ":" + item.id, "path", {d: "M" + a[0] + "," + a[1] + "Q" + midX + "," + midY + " " + b[0] + "," + b[1],
          class: "pm-rel pm-" + pair[0], "stroke-dasharray": item.statement ? "" : "5 4"});
      });
    });
  }
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
    if (id) showResult(id); else showPart(state.focusPart ? part(state.focusPart) : deepestAtCenter);
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
    var chain = chainOf(id);
    return ensureChain(chain).then(function () {
      var record = part(id);
      state.focusPart = id;
      if (options.history !== false) remember(options.replace);
      flyToBox(record.box, 0.9);
      showPart(record);
    });
  }
  function arrive(resultId, chain, options) {
    options = options || {};
    return ensureChain(chain).then(function () {
      var homeId = chain[chain.length - 1], record = part(homeId), entry = model.results.get(resultId);
      if (!entry) throw new Error("Result not found in its part: " + resultId);
      state.focusPart = homeId;
      // Close enough that the part is open and its result cards carry their titles.
      var at = centerOf(entry.box), k = Math.max(fitK(record.box, 0.9), RESULT_LABEL_PX * 5 / entry.box.w);
      k = Math.min(k, fitK(record.box, 0.9) * 4 > k ? k : fitK(record.box, 0.9) * 4);
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
      button.addEventListener("click", function () { flyToPart(summary.id).catch(report); });
      item.append(button);
      var record = part(summary.id);
      if (record && record.data && record.kids.length) {
        var sub = el("ul");
        record.kids.slice(0, 40).forEach(function (id) {
          var child = part(id), li = el("li"), b = el("button");
          b.type = "button"; b.dataset.part = id;
          b.append(el("span", "", plain(child.summary.title)), el("span", "pm-count", num(child.summary.results)));
          b.addEventListener("click", function () { flyToPart(id).catch(report); });
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
      readerInner.append(el("p", "", "Each box is a region of the library, sized by its number of results. Lines show how often one region's results use another's; arrows run from what is used to what uses it. Zoom in to open a region in place, or search for a theorem by name."));
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
      button.addEventListener("click", function () { flyToPart(id).catch(report); });
      item.append(button); list.append(item);
    });
    return list;
  }
  function typeset(node) {
    if (window.MathJax && typeof window.MathJax.typesetPromise === "function") window.MathJax.typesetPromise([node]).catch(function () {});
  }
  function richText(text) {
    var node = el("p", "pm-statement");
    String(text || "").split(/(`[^`]*`)/).forEach(function (piece) {
      if (/^`[^`]*`$/.test(piece)) node.append(el("code", "", piece.slice(1, -1)));
      else node.append(document.createTextNode(piece));
    });
    return node;
  }
  function showResult(id) {
    var entry = model.results.get(id);
    if (!entry) return;
    shownPart = null;
    var row = entry.row, rel = relationsOf(id);
    readerInner.replaceChildren();
    readerInner.append(el("p", "pm-kicker", "Result"));
    var heading = el("h2", "", row.title); readerInner.append(heading);
    readerInner.append(pathLine(chainOf(entry.home)));
    readerInner.append(el("span", "pm-chip", statusWords(row.status) + (row.facet ? " · " + row.facet : "")));
    if (row.summary) readerInner.append(richText(row.summary));
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
    });
    typeset(readerInner);
  }

  // ---------------------------------------------------------------- trail
  function updateTrail() {
    var chain = state.selected && model.results.get(state.selected) ? chainOf(model.results.get(state.selected).home)
      : deepestAtCenter && deepestAtCenter.id !== ROOT ? chainOf(deepestAtCenter.id) : [];
    var signature = chain.join("|") + "#" + (state.selected || "");
    if (trail.dataset.signature === signature) return;
    trail.dataset.signature = signature;
    trail.replaceChildren();
    var rootItem = el("li"), rootButton = el("button", "", plain(model.index.title)); rootButton.type = "button";
    rootButton.addEventListener("click", function () { select(null, {history: false}); state.focusPart = null; remember(); fitProject(); });
    rootItem.append(rootButton); trail.append(rootItem);
    chain.forEach(function (id, index) {
      var item = el("li"), last = index === chain.length - 1 && !state.selected;
      if (last) { item.textContent = plain(part(id).summary.title); item.setAttribute("aria-current", "location"); }
      else {
        var button = el("button", "", plain(part(id).summary.title)); button.type = "button";
        button.addEventListener("click", function () { select(null, {history: false}); flyToPart(id).catch(report); });
        item.append(button);
      }
      trail.append(item);
    });
    if (state.selected) {
      var item = el("li", "", plain(model.results.get(state.selected).row.title)); item.setAttribute("aria-current", "location"); trail.append(item);
    }
  }

  // ---------------------------------------------------------------- history
  function snapshot() {
    return {pm: 1, camera: {x: camera.x, y: camera.y, k: camera.k}, selected: state.selected, focusPart: state.focusPart,
      chain: state.selected && model.results.get(state.selected) ? chainOf(model.results.get(state.selected).home) : state.focusPart ? chainOf(state.focusPart) : []};
  }
  function remember(replace) {
    var snap = snapshot();
    var hash = snap.selected ? "#result=" + encodeURIComponent(snap.selected) : snap.focusPart ? "#part=" + encodeURIComponent(snap.focusPart) : location.pathname + location.search;
    // The current entry keeps its camera, so Back returns to exactly where the reader was.
    try { history.replaceState(Object.assign({}, history.state || {}, {pm: 1, camera: {x: camera.x, y: camera.y, k: camera.k}, selected: history.state && history.state.selected, focusPart: history.state && history.state.focusPart, chain: history.state && history.state.chain}), ""); } catch (_e) { /* ignore */ }
    if (replace) history.replaceState(snap, "", hash); else history.pushState(snap, "", hash);
  }
  window.addEventListener("popstate", function (event) {
    var snap = event.state;
    if (!snap || !snap.pm) { select(null, {history: false}); state.focusPart = null; fitProject(true); return; }
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
      try { history.replaceState(Object.assign({}, current, {camera: {x: camera.x, y: camera.y, k: camera.k}}), ""); } catch (_e) { /* ignore */ }
    }, 250);
  }

  // ---------------------------------------------------------------- input
  var drag = null;
  canvas.addEventListener("wheel", function (event) {
    event.preventDefault();
    var box = canvas.getBoundingClientRect();
    zoomAt(Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0022)), event.clientX - box.left, event.clientY - box.top);
    rememberCamera();
  }, {passive: false});
  canvas.addEventListener("pointerdown", function (event) {
    if (event.button !== 0) return;
    drag = {x: event.clientX, y: event.clientY, cx: camera.x, cy: camera.y, moved: false, id: event.pointerId};
  });
  canvas.addEventListener("pointermove", function (event) {
    if (!drag) return;
    var dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    if (!drag.moved) { drag.moved = true; canvas.setPointerCapture(drag.id); canvas.classList.add("pm-dragging"); }
    camera = {x: drag.cx - dx / camera.k, y: drag.cy - dy / camera.k, k: camera.k};
    schedule();
  });
  function endDrag(event) {
    if (!drag) return;
    var moved = drag.moved; drag = null; canvas.classList.remove("pm-dragging");
    if (moved) { rememberCamera(); return; }
    activate(event.target, event.detail >= 2);
  }
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", function () { drag = null; canvas.classList.remove("pm-dragging"); });
  function activate(target, deep) {
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
    else if (event.key === "0") fitProject();
    else if (event.key === "Escape") select(null);
    else return;
    event.preventDefault(); schedule(); rememberCamera();
  });
  zoomIn.addEventListener("click", function () { zoomAt(1.5, size.w / 2, size.h / 2); rememberCamera(); });
  zoomOut.addEventListener("click", function () { zoomAt(1 / 1.5, size.w / 2, size.h / 2); rememberCamera(); });
  fitAll.addEventListener("click", function () { select(null, {history: false}); state.focusPart = null; remember(); fitProject(); });
  document.addEventListener("keydown", function (event) {
    if (event.key === "/" && document.activeElement !== search && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
      event.preventDefault(); search.focus(); search.select();
    }
  });
  if (window.ResizeObserver) new ResizeObserver(function () { schedule(); }).observe(stage);
  function fitProject(instant) {
    var root = part(ROOT), c = centerOf(root.box), target = {x: c[0], y: c[1], k: fitK(root.box)};
    if (instant) { camera = target; schedule(); } else flyTo(target);
  }
  function report(error) {
    live.textContent = "Something went wrong: " + (error && error.message ? error.message : error);
    if (window.console) console.error(error);
  }

  // ---------------------------------------------------------------- start
  fetchJSON("index.json").then(function (index) {
    model.index = index;
    var root = {id: ROOT, summary: {id: ROOT, title: index.title, results: index.results, status: index.status, parts: index.regions.length, api: [], api_total: 0, imports_total: 0},
      parent: null, data: {children: index.regions, edges: index.region_edges}, loading: null, box: null, ownBox: null, kids: [], results: []};
    model.parts.set(ROOT, root);
    index.regions.forEach(function (summary) { register(summary, ROOT); });
    measure();
    // The project's box takes the stage's shape, quantized so a small resize never reshuffles the map.
    var aspect = size.w / size.h >= 1 ? clamp(Math.round(size.w / size.h * 4) / 4, 1, 2.5) : 0.75;
    root.box = {x: -WORLD_W / 2, y: -WORLD_W / aspect / 2, w: WORLD_W, h: WORLD_W / aspect};
    layoutApi().packPart(root, root.data);
    var c0 = centerOf(root.box);
    camera = {x: c0[0], y: c0[1], k: fitK(root.box)};
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
