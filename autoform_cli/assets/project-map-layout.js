/* Multiscale map layout: pure, synchronous functions over the autoform-project-map/v1 payload.
 * World units are stage pixels at the opening pose. A part's children are packed as if the part
 * were fitted to the stage, so every level reads the same at its own zoom; a parent's box never
 * changes once placed, so zooming in reveals its children inside it, whatever loads first. Pack
 * once per load and refit the camera on resize; re-pack only when the stage turns between portrait
 * and landscape. Edges are [user, used, statement, proof]; routes run from the used part to its
 * user, only through the gaps between boxes, so no route crosses a box or its label.
 */
(function () {
  "use strict";

  // Screen pixels at the zoom where a part is fitted to the stage. A header band holds a 15 px
  // title and a 12 px status line, and a box keeps room below it for what zooming in reveals; a
  // box narrower or shorter than MIN_W by MIN_H gets its label deeper in. GAP is wide enough for
  // a few lanes of routes between neighbouring boxes.
  var BAND = 52, BAND2 = 72, PAD = 12, GAP = 16, MIN_W = 150, MIN_H = 120, ASPECT = 1.6, LANE = 5, RADIUS = 6;
  // Mean advance of a 15 px semibold title character, for choosing one or two title lines.
  var TITLE_EM = 8.4;
  // The band height a lone title line needs, and a compact one: a 13 px title in a box at
  // least COMPACT_W wide, so narrow parts are still named.
  var LABEL_BAND = 36, COMPACT_BAND = 20, COMPACT_W = 60;
  // The advance between wrapped title lines, and the extra rows the progress and API details take.
  var LINE = 20, COMPACT_LINE = 16, DETAIL_LINE = 18;
  // The most routes a sibling set shows at rest; the rest appear with their parts.
  var MAX_REST = 18;

  function byKey(a, b) { return a < b ? -1 : a > b ? 1 : 0; }
  function inset(rect, d) {
    return {x: rect.x + d, y: rect.y + d, w: Math.max(0, rect.w - 2 * d), h: Math.max(0, rect.h - 2 * d)};
  }
  // World units per screen pixel at the zoom that fits this content box to the stage.
  function unitsPerPixel(content, stage) { return Math.max(content.w / stage.w, content.h / stage.h); }
  // A box's content area; its margin is the padding beside and below it, where routes may run.
  function contentOf(box) {
    var unit = box.unit, band = box.band === undefined ? BAND : box.band, pad = box.pad === undefined ? PAD : box.pad;
    return {x: box.x + pad * unit, y: box.y + band * unit, w: Math.max(0, box.w - 2 * pad * unit),
      h: Math.max(0, box.h - (band + pad) * unit), margin: pad * unit};
  }
  // A title too long for one line at this width takes two, when the box is tall enough for both.
  function bandFor(title, box) {
    var width = box.w / box.unit - 2 * PAD, height = box.h / box.unit;
    return (title || "").length * TITLE_EM > width && height >= BAND2 + PAD ? BAND2 : BAND;
  }

  // Prerequisites first: a weighted greedy feedback-arc order (Eades, Lin and Smyth), so most link
  // weight runs forward. Ties keep the payload's order, which is the book's reading order.
  function dependencyOrder(items, edges) {
    var rank = items.map(function (item) { return item.id; });
    var alive = new Set(rank), before = new Map(), after = new Map();
    rank.forEach(function (id) { before.set(id, new Map()); after.set(id, new Map()); });
    edges.forEach(function (edge) {
      var user = edge[0], used = edge[1], weight = (edge[2] || 0) + (edge[3] || 0);
      if (user === used || !weight || !alive.has(user) || !alive.has(used)) return;
      after.get(used).set(user, (after.get(used).get(user) || 0) + weight);
      before.get(user).set(used, (before.get(user).get(used) || 0) + weight);
    });
    function live(map) {
      var total = 0;
      map.forEach(function (weight, id) { if (alive.has(id)) total += weight; });
      return total;
    }
    var head = [], tail = [];
    while (alive.size) {
      var picked = null, i;
      for (i = 0; i < rank.length && !picked; i++) {
        if (alive.has(rank[i]) && !live(before.get(rank[i]))) { picked = rank[i]; head.push(picked); }
      }
      if (!picked) {
        for (i = rank.length - 1; i >= 0 && !picked; i--) {
          if (alive.has(rank[i]) && !live(after.get(rank[i]))) { picked = rank[i]; tail.push(picked); }
        }
      }
      if (!picked) {
        var best = -Infinity;
        for (i = 0; i < rank.length; i++) {
          if (!alive.has(rank[i])) continue;
          var delta = live(after.get(rank[i])) - live(before.get(rank[i]));
          if (delta > best) { best = delta; picked = rank[i]; }
        }
        head.push(picked);
      }
      alive.delete(picked);
    }
    return head.concat(tail.reverse());
  }

  // Scale values to fill total, none below floor (water-filling); with no room for every floor,
  // the shares even out instead.
  function fill(values, total, floor) {
    var n = values.length, clamped = new Array(n).fill(false), out = values.slice();
    floor = Math.min(floor, total / n);
    for (var round = 0; round <= n; round++) {
      var free = total, weight = 0, i;
      for (i = 0; i < n; i++) { if (clamped[i]) free -= floor; else weight += values[i]; }
      var scale = weight ? free / weight : 0, changed = false;
      for (i = 0; i < n; i++) {
        if (clamped[i]) { out[i] = floor; continue; }
        out[i] = values[i] * scale;
        if (out[i] < floor) { clamped[i] = true; changed = true; }
      }
      if (!changed) break;
    }
    return out;
  }

  // Ordered strip treemap: rows top to bottom, items left to right in the given order. A row closes
  // when the next item would worsen its worst aspect ratio (against ASPECT, since labels run across)
  // or would leave an item narrower than minW. Areas follow the weights where the minimums allow;
  // when the box cannot give every item a readable size, the minimums go and every item keeps a
  // good shape instead, so zooming in makes it readable.
  function strips(order, weightOf, box, minW, minH, aspect) {
    var total = 0;
    order.forEach(function (id) { total += weightOf(id); });
    var scale = total ? box.w * box.h / total : 0;
    function worst(items, area) {
      var height = area / box.w, ratio = 1;
      items.forEach(function (id) {
        var r = weightOf(id) * scale / height / height / aspect;
        ratio = Math.max(ratio, r, 1 / r);
      });
      return ratio;
    }
    function group(perRow) {
      var rows = [], row = [], rowArea = 0;
      order.forEach(function (id) {
        var area = weightOf(id) * scale;
        if (row.length && (row.length >= perRow || worst(row.concat([id]), rowArea + area) > worst(row, rowArea))) {
          rows.push({ids: row, area: rowArea}); row = []; rowArea = 0;
        }
        row.push(id); rowArea += area;
      });
      if (row.length) rows.push({ids: row, area: rowArea});
      // A last row flatter than the merged one joins the row above.
      if (rows.length > 1) {
        var last = rows[rows.length - 1], prev = rows[rows.length - 2], merged = prev.ids.concat(last.ids);
        if (merged.length <= perRow &&
            worst(merged, prev.area + last.area) < Math.max(worst(prev.ids, prev.area), worst(last.ids, last.area))) {
          rows.splice(rows.length - 2, 2, {ids: merged, area: prev.area + last.area});
        }
      }
      return rows;
    }
    var perRow = Math.max(1, Math.floor(box.w / minW)), rows = group(perRow);
    // Too many rows to give each a readable height: merge the neighbours that suffer least.
    var maxRows = Math.max(1, Math.floor(box.h / minH));
    while (rows.length > maxRows) {
      var best = -1, bestCost = Infinity;
      for (var i = 0; i + 1 < rows.length; i++) {
        if (rows[i].ids.length + rows[i + 1].ids.length > perRow) continue;
        var cost = worst(rows[i].ids.concat(rows[i + 1].ids), rows[i].area + rows[i + 1].area);
        if (cost < bestCost) { bestCost = cost; best = i; }
      }
      if (best < 0) break;
      rows.splice(best, 2, {ids: rows[best].ids.concat(rows[best + 1].ids), area: rows[best].area + rows[best + 1].area});
    }
    function place(rows, floorW, floorH) {
      var heights = fill(rows.map(function (r) { return r.area; }), box.h, floorH);
      var rects = {}, y = box.y;
      rows.forEach(function (r, index) {
        var widths = fill(r.ids.map(weightOf), box.w, floorW), x = box.x;
        r.ids.forEach(function (id, k) {
          rects[id] = {x: x, y: y, w: widths[k], h: heights[index], row: index};
          x += widths[k];
        });
        y += heights[index];
      });
      // Each item's zoom, against this level's, at which its box is big enough for its label; a
      // sliver needs a lot. Their geometric mean weighs every item alike, large or small.
      var worst = 0, logs = 0, count = 0;
      for (var id in rects) {
        var r = rects[id], zoom = Math.max(minW / Math.max(r.w, 1e-9), minH / Math.max(r.h, 1e-9));
        worst = Math.max(worst, zoom); logs += Math.log(zoom); count++;
      }
      return {rects: rects, readable: worst <= 1 / 0.99, zoom: Math.exp(logs / count)};
    }
    var kept = place(rows, minW, minH);
    return kept.readable ? [kept] : [kept, place(group(Infinity), 0, 0)];
  }

  // Strips run across a box, or down it as columns. A layout where every item is readable wins,
  // rows first, since they read like text. Otherwise the one whose items become readable soonest
  // as the camera closes in wins: a wide box with one dominant item would otherwise give it a flat
  // full-width row, and a flat box would give its children slivers.
  function transpose(rect) { return {x: rect.y, y: rect.x, w: rect.h, h: rect.w, row: rect.row}; }
  // Area grows with the square root of a part's statements, so a small part stays a usable box
  // beside a large one; the label carries the count.
  function packOrdered(order, weights, titles, content, unit) {
    var gap = GAP * unit, weightOf = function (id) { return Math.sqrt(Math.max(weights.get(id), 1)); };
    var minW = MIN_W * unit + gap, minH = MIN_H * unit + gap;
    var candidates = strips(order, weightOf, content, minW, minH, ASPECT).map(function (layout) {
      return Object.assign(layout, {columns: false});
    });
    strips(order, weightOf, transpose(content), minH, minW, 1 / ASPECT).forEach(function (layout) {
      var rects = {};
      for (var key in layout.rects) rects[key] = transpose(layout.rects[key]);
      candidates.push(Object.assign(layout, {rects: rects, columns: true}));
    });
    var packed = candidates.filter(function (layout) { return layout.readable; })[0] ||
      candidates.reduce(function (best, layout) { return layout.zoom < best.zoom * 0.98 ? layout : best; });
    var rects = packed.rects, columns = packed.columns;
    // Gaps shrink in a row of small items, so a small box keeps its shape instead of vanishing.
    var margin = new Map();
    order.forEach(function (id) {
      var r = rects[id], d = Math.min(gap, 0.25 * r.w, 0.25 * r.h);
      margin.set(r.row, Math.min(margin.has(r.row) ? margin.get(r.row) : gap, d));
    });
    var boxes = {};
    order.forEach(function (id, index) {
      var r = inset(rects[id], margin.get(rects[id].row) / 2), box = {x: r.x, y: r.y, w: r.w, h: r.h, unit: unit, order: index,
        row: rects[id].row, axis: columns ? "columns" : "rows"};
      // A box too small for a whole band keeps most of its height for its content, so what it
      // holds still opens as the camera closes in; its label waits until the band is tall enough.
      box.band = Math.min(bandFor(titles.get(id), box), 0.4 * r.h / unit);
      box.pad = Math.min(PAD, 0.1 * Math.min(r.w, r.h) / unit);
      boxes[id] = box;
    });
    Object.defineProperty(boxes, "$readable", {value: packed.readable});
    return boxes;
  }

  function summaries(list) {
    return list.map(function (part) { return {id: part.id, title: part.title, weight: part.results}; });
  }
  function weightsOf(items) { return new Map(items.map(function (item) { return [item.id, item.weight]; })); }
  function titlesOf(items) { return new Map(items.map(function (item) { return [item.id, item.title]; })); }

  // The regions, from index.json, at their native size: the opening pose is zoom 1, top-aligned.
  // When the stage is too short for every region at a readable size (a phone with many regions),
  // the world grows downward and the map scrolls. The returned content box is the area to pass to
  // bundleEdges for the region routes.
  function packLevels(index, stage) {
    var regions = summaries(index.regions), order = dependencyOrder(regions, index.region_edges || []);
    var height = stage.h, root, content, packed;
    for (var attempt = 0; attempt <= regions.length; attempt++) {
      root = {x: 0, y: 0, w: stage.w, h: height, unit: 1, depth: 0}; content = Object.assign(inset(root, PAD), {margin: PAD});
      packed = packOrdered(order, weightsOf(regions), titlesOf(regions), content, 1);
      if (packed.$readable) break;
      height += MIN_H + GAP;
    }
    var boxes = {};
    for (var id in packed) boxes[id] = Object.assign({depth: 1, parent: null}, packed[id]);
    return {root: root, content: content, boxes: boxes};
  }

  // A part's children inside its box, when its file loads. A part that also holds results of its
  // own opens with an "own results" box, keyed by the part id with "#results" appended; beside
  // other children it has a band for the page's title for it, and alone it is the part's whole
  // content. The area for this part's routes is contentOf(box).
  function packPart(partData, box, stage) {
    var items = summaries(partData.children || []), own = (partData.results || []).length;
    var id = partData.part.id, ownId = id + "#results", content = contentOf(box), boxes = {};
    var unit = unitsPerPixel(content, stage);
    if (!items.length) {
      if (own) boxes[ownId] = Object.assign({depth: box.depth + 1, parent: id, own: true, unit: unit, order: 0, row: 0, band: 0, pad: 0}, content);
      return boxes;
    }
    var order = dependencyOrder(items, partData.edges || []), weights = weightsOf(items), titles = titlesOf(items);
    if (own) { order.unshift(ownId); weights.set(ownId, own); }
    var packed = packOrdered(order, weights, titles, content, unit);
    for (var child in packed) {
      boxes[child] = Object.assign({depth: box.depth + 1, parent: id}, packed[child]);
      if (child === ownId) boxes[child].own = true;
    }
    return boxes;
  }

  // One route per linked sibling pair, from the used part to its user; a pair linked both ways is
  // one route with a head at each end, its source the heavier side's used part. At rest the map
  // shows the central routes, at most four for every five boxes and never more than MAX_REST:
  // heaviest first, preferring routes that reach boxes no shown route reaches yet. Every route has
  // an orthogonal path through the gaps of area, never in a band.
  function bundleEdges(boxes, edges, area) {
    var pairs = new Map();
    edges.forEach(function (edge) {
      var user = edge[0], used = edge[1];
      if (!boxes[user] || !boxes[used] || user === used) return;
      var a = used < user ? used : user, b = used < user ? user : used, key = a + "\u0000" + b;
      if (!pairs.has(key)) pairs.set(key, {a: a, b: b, ab: [0, 0], ba: [0, 0]});
      var pair = pairs.get(key), side = used === a ? pair.ab : pair.ba;
      side[0] += edge[2] || 0; side[1] += edge[3] || 0;
    });
    var routes = Array.from(pairs.keys()).sort(byKey).map(function (key) {
      var pair = pairs.get(key), forward = pair.ab[0] + pair.ab[1], backward = pair.ba[0] + pair.ba[1];
      var flip = backward > forward, main = flip ? pair.ba : pair.ab, other = flip ? pair.ab : pair.ba;
      var weight = forward + backward;
      return {from: flip ? pair.b : pair.a, to: flip ? pair.a : pair.b, both: forward > 0 && backward > 0,
        weights: [main[0], main[1], other[0], other[1]], kind: main[0] + other[0] > 0 ? "statement" : "proof",
        weight: weight, tier: weight >= 100 ? 3 : weight >= 10 ? 2 : 1, rest: false};
    });
    var byWeight = routes.slice().sort(function (p, q) { return q.weight - p.weight || byKey(p.from + p.to, q.from + q.to); });
    var budget = Math.min(MAX_REST, Math.max(1, Math.round(Object.keys(boxes).length * 0.8))), shown = 0;
    var reached = new Set();
    // Routes between two unreached boxes first, then routes reaching one, then the heaviest others.
    [2, 1, 0].forEach(function (fresh) {
      byWeight.forEach(function (route) {
        var count = (reached.has(route.from) ? 0 : 1) + (reached.has(route.to) ? 0 : 1);
        if (route.rest || shown >= budget || count < fresh) return;
        route.rest = true; shown++; reached.add(route.from); reached.add(route.to);
      });
    });
    if (routes.length) {
      var first = boxes[routes[0].from];
      if (first.axis === "columns") {
        // Route in the transposed picture, where the columns are rows, then turn it back.
        var flipped = {};
        for (var id in boxes) flipped[id] = Object.assign({}, boxes[id], transpose(boxes[id]));
        // Transposed, the left margin is the top one, which is the parent's band: never route there.
        routeThroughGaps(routes, flipped, Object.assign(transpose(area), {margin: area.margin}), true);
        routes.forEach(function (route) {
          for (var k = 0; k < route.points.length; k += 2) {
            var t = route.points[k]; route.points[k] = route.points[k + 1]; route.points[k + 1] = t;
          }
        });
      } else routeThroughGaps(routes, boxes, area);
    }
    return routes;
  }

  // The gaps of a strip layout: a corridor between each pair of rows and below the last, and in
  // each row the gaps between neighbours plus the side margins. A route leaves its source through
  // the side facing its target, runs along corridors and down or up through row gaps, and enters
  // its target the same way. Routes sharing a corridor or gap take separate lanes.
  function routeThroughGaps(routes, boxes, area, noLeftMargin) {
    var ids = Object.keys(boxes), unit = boxes[ids[0]].unit, rows = [];
    ids.forEach(function (id) {
      var box = boxes[id], r = box.row || 0;
      if (!rows[r]) rows[r] = {top: Infinity, bottom: -Infinity, ids: []};
      rows[r].top = Math.min(rows[r].top, box.y); rows[r].bottom = Math.max(rows[r].bottom, box.y + box.h);
      rows[r].ids.push(id);
    });
    rows = rows.filter(Boolean);
    var rowOf = new Map();
    rows.forEach(function (row, r) {
      row.ids.sort(function (p, q) { return boxes[p].x - boxes[q].x; });
      row.ids.forEach(function (id) { rowOf.set(id, r); });
    });
    var margin = area.margin === undefined ? PAD * unit : area.margin;
    var left = area.x - margin / 2, right = area.x + area.w + margin / 2, floor = area.y + area.h + margin / 2;
    // corridor[c] lies above row c (c = 1 .. rows.length - 1) or below the last row (c = rows.length).
    function corridorY(c) { return c < rows.length ? (rows[c - 1].bottom + rows[c].top) / 2 : (rows[c - 1].bottom + floor) / 2; }
    function corridorWidth(c) { return c < rows.length ? rows[c].top - rows[c - 1].bottom : floor - rows[c - 1].bottom; }
    // The passages through row r: the margins and the gaps between neighbours.
    function passages(r) {
      var list = [{x: right, width: margin, key: "R"}];
      if (!noLeftMargin) list.push({x: left, width: margin, key: "L"});
      var row = rows[r].ids;
      for (var k = 0; k + 1 < row.length; k++) {
        var a = boxes[row[k]], b = boxes[row[k + 1]];
        list.push({x: (a.x + a.w + b.x) / 2, width: b.x - a.x - a.w, key: String(k)});
      }
      return list;
    }
    var hSegments = new Map(), vSegments = new Map();
    function use(map, key, from, to, route, width) {
      if (!map.has(key)) map.set(key, {width: width, list: []});
      map.get(key).list.push({lo: Math.min(from, to), hi: Math.max(from, to), route: route});
    }
    routes.forEach(function (route) {
      var a = boxes[route.from], b = boxes[route.to], ra = rowOf.get(route.from), rb = rowOf.get(route.to);
      var ax = a.x + a.w / 2, bx = b.x + b.w / 2, steps = [];
      route.legs = steps;
      if (ra === rb) {
        var row = rows[ra].ids, ia = row.indexOf(route.from), ib = row.indexOf(route.to);
        if (Math.abs(ia - ib) === 1) {
          route.direct = ia < ib ? ["right", "left"] : ["left", "right"];
          return;
        }
        // Same row, not neighbours: along the corridor below the row.
        route.sides = ["bottom", "bottom"];
        steps.push({h: ra + 1, from: ax, to: bx});
        use(hSegments, ra + 1, ax, bx, route, corridorWidth(ra + 1));
        return;
      }
      var down = rb > ra;
      route.sides = down ? ["bottom", "top"] : ["top", "bottom"];
      var c = down ? ra + 1 : ra, x = ax;
      // Walk through the rows between source and target, one corridor and one passage at a time.
      for (var r = down ? ra + 1 : ra - 1; down ? r < rb : r > rb; r += down ? 1 : -1) {
        var options = passages(r), pick = null, cost = Infinity;
        options.forEach(function (p) {
          var d = Math.abs(p.x - x) + Math.abs(p.x - bx);
          if (d < cost - 1e-9) { cost = d; pick = p; }
        });
        steps.push({h: c, from: x, to: pick.x});
        use(hSegments, c, x, pick.x, route, corridorWidth(c));
        var next = down ? c + 1 : c - 1;
        steps.push({v: r + ":" + pick.key, x: pick.x, from: c, to: next});
        use(vSegments, r + ":" + pick.key, corridorY(c), corridorY(next), route, pick.width);
        x = pick.x; c = next;
      }
      steps.push({h: c, from: x, to: bx});
      use(hSegments, c, x, bx, route, corridorWidth(c));
    });
    // Lanes: greedy interval colouring per corridor, centred in it.
    function lanes(map) {
      var offsets = new Map();
      map.forEach(function (entry, key) {
        var list = entry.list.slice().sort(function (p, q) { return p.lo - q.lo || q.route.weight - p.route.weight; });
        var ends = [];
        list.forEach(function (item) {
          var lane = 0;
          while (lane < ends.length && ends[lane] > item.lo - LANE * unit) lane++;
          ends[lane] = item.hi; item.lane = lane;
        });
        var count = ends.length, spacing = Math.min(LANE * unit, entry.width * 0.85 / Math.max(count, 1));
        list.forEach(function (item) { offsets.set(key + "\u0000" + item.route.from + "\u0000" + item.route.to + "\u0000" + item.lo,
          (item.lane - (count - 1) / 2) * spacing); });
      });
      return offsets;
    }
    var hLane = lanes(hSegments), vLane = lanes(vSegments);
    function laneY(c, route, lo) {
      return corridorY(c) + (hLane.get(c + "\u0000" + route.from + "\u0000" + route.to + "\u0000" + lo) || 0);
    }
    function laneX(key, route, lo, x) { return x + (vLane.get(key + "\u0000" + route.from + "\u0000" + route.to + "\u0000" + lo) || 0); }
    // Ports: spread over the middle of each side, ordered by where each route goes next.
    var ports = new Map();
    routes.forEach(function (route) {
      var a = boxes[route.from], b = boxes[route.to];
      var s = route.direct || route.sides, legs = route.legs;
      var nextA = route.direct ? b.y + b.h / 2 : legs.length ? legs[0].to : 0;
      var nextB = route.direct ? a.y + a.h / 2 : legs.length ? legs[legs.length - 1].from : 0;
      [[route.from, s[0], nextA, 0], [route.to, s[1], nextB, 1]].forEach(function (end) {
        var key = end[0] + "\u0000" + end[1];
        if (!ports.has(key)) ports.set(key, []);
        ports.get(key).push({route: route, end: end[3], toward: end[2]});
      });
    });
    ports.forEach(function (list, key) {
      var parts = key.split("\u0000"), box = boxes[parts[0]], side = parts[1];
      list.sort(function (p, q) { return p.toward - q.toward || byKey(p.route.from + p.route.to, q.route.from + q.route.to); });
      list.forEach(function (slot, i) {
        var t = 0.15 + 0.7 * (i + 1) / (list.length + 1);
        var point = side === "top" ? [box.x + box.w * t, box.y] : side === "bottom" ? [box.x + box.w * t, box.y + box.h] :
          side === "left" ? [box.x, box.y + box.h * t] : [box.x + box.w, box.y + box.h * t];
        slot.route[slot.end ? "endPort" : "startPort"] = point;
      });
    });
    routes.forEach(function (route) {
      var s = route.startPort, e = route.endPort, points = [s[0], s[1]];
      if (route.direct) {
        var y = (s[1] + e[1]) / 2;
        points.push(s[0], y, e[0], y);
      } else {
        var x = s[0], y0 = null;
        route.legs.forEach(function (leg) {
          if (leg.h !== undefined) {
            var y1 = laneY(leg.h, route, Math.min(leg.from, leg.to));
            points.push(x, y1);
            y0 = y1;
          } else {
            var lx = laneX(leg.v, route, Math.min(corridorY(leg.from), corridorY(leg.to)), leg.x);
            points.push(lx, y0);
            x = lx;
          }
        });
        points.push(e[0], y0);
      }
      points.push(e[0], e[1]);
      route.points = simplify(points);
      route.radius = RADIUS * unit;
      delete route.startPort; delete route.endPort; delete route.legs; delete route.sides; delete route.direct;
    });
  }

  // Drop repeated and collinear points from an orthogonal polyline.
  function simplify(points) {
    var out = [];
    for (var i = 0; i < points.length; i += 2) {
      var n = out.length;
      if (n >= 2 && Math.abs(out[n - 2] - points[i]) < 1e-6 && Math.abs(out[n - 1] - points[i + 1]) < 1e-6) continue;
      if (n >= 4) {
        var ax = out[n - 4], ay = out[n - 3], bx = out[n - 2], by = out[n - 1];
        if ((Math.abs(ax - bx) < 1e-6 && Math.abs(bx - points[i]) < 1e-6) ||
            (Math.abs(ay - by) < 1e-6 && Math.abs(by - points[i + 1]) < 1e-6)) { out[n - 2] = points[i]; out[n - 1] = points[i + 1]; continue; }
      }
      out.push(points[i], points[i + 1]);
    }
    return out;
  }

  // The result view inside a part, in the explorer's style: prerequisites above their users, each
  // cycle condensed into one layer, links implied by others hidden at rest and kept for selection.
  // Cards keep one size (CARD_W by CARD_H at the zoom where the part is fitted, or smaller when
  // many share the box) and sit in rows of at most `cols`, chosen to fill the box best. Links to
  // results outside the part become per-card stub counts. Returns {cards, routes, implied, stubs}.
  var CARD_W = 200, CARD_H = 64, COL_GAP = 24, ROW_GAP = 40;
  function layoutCommunity(results, resultEdges, boundaryEdges, ownBox) {
    // An own box beside other children keeps its band for its title.
    var box = ownBox.band ? Object.assign(contentOf(ownBox), {unit: ownBox.unit}) : ownBox;
    var ids = results.map(function (result) { return result.id; }), at = new Map();
    ids.forEach(function (id, index) { at.set(id, index); });
    var n = ids.length, after = ids.map(function () { return []; });
    var links = [];
    (resultEdges || []).forEach(function (edge) {
      var user = at.get(edge[0]), used = at.get(edge[1]);
      if (user === undefined || used === undefined || user === used) return;
      after[used].push(user); links.push(edge);
    });
    // Strongly connected components (Tarjan, iterative), numbered in reverse topological order.
    var index = 0, stack = [], onStack = new Array(n).fill(false), low = new Array(n), num = new Array(n).fill(-1);
    var component = new Array(n).fill(-1), components = [];
    for (var root = 0; root < n; root++) {
      if (num[root] >= 0) continue;
      var work = [[root, 0]];
      num[root] = low[root] = index++; stack.push(root); onStack[root] = true;
      while (work.length) {
        var frame = work[work.length - 1], v = frame[0];
        if (frame[1] < after[v].length) {
          var w = after[v][frame[1]++];
          if (num[w] < 0) { num[w] = low[w] = index++; stack.push(w); onStack[w] = true; work.push([w, 0]); }
          else if (onStack[w]) low[v] = Math.min(low[v], num[w]);
          continue;
        }
        work.pop();
        if (work.length) low[work[work.length - 1][0]] = Math.min(low[work[work.length - 1][0]], low[v]);
        if (low[v] === num[v]) {
          var members = [], x;
          do { x = stack.pop(); onStack[x] = false; component[x] = components.length; members.push(x); } while (x !== v);
          components.push(members.sort(function (a, b) { return a - b; }));
        }
      }
    }
    // Layers by longest path from the prerequisites; Tarjan's numbering is reverse topological.
    var count = components.length, layer = new Array(count).fill(0), next = components.map(function () { return new Set(); });
    for (v = 0; v < n; v++) after[v].forEach(function (u) { if (component[u] !== component[v]) next[component[v]].add(component[u]); });
    for (var c = count - 1; c >= 0; c--) next[c].forEach(function (d) { layer[d] = Math.max(layer[d], layer[c] + 1); });
    // Transitive reduction between components: keep c -> d unless d is reached another way.
    var reach = components.map(function () { return new Set(); }), kept = components.map(function () { return new Set(); });
    for (c = 0; c < count; c++) {
      var targets = Array.from(next[c]).sort(function (a, b) { return layer[a] - layer[b] || b - a; });
      targets.forEach(function (d) {
        if (!reach[c].has(d)) kept[c].add(d);
        reach[c].add(d); reach[d].forEach(function (e) { reach[c].add(e); });
      });
    }
    // Rows: each layer's components in payload order, a cycle's members side by side, wrapped at
    // cols without splitting a cycle that fits one row; then barycentre sweeps.
    var byLayer = [];
    for (c = count - 1; c >= 0; c--) {
      if (!byLayer[layer[c]]) byLayer[layer[c]] = [];
      byLayer[layer[c]].push(components[c]);
    }
    byLayer = byLayer.filter(Boolean).map(function (list) { return list.sort(function (a, b) { return a[0] - b[0]; }); });
    // Layered rows keep every layer on rows of its own; folded rows flow the same reading order
    // into rows like text, for a deep map in a wide box. Layered wins unless folding fits a
    // quarter better.
    function layered(cols) {
      var rows = [];
      byLayer.forEach(function (list) {
        var row = [];
        list.forEach(function (members) {
          if (row.length && row.length + members.length > cols && members.length <= cols) { rows.push(row); row = []; }
          members.forEach(function (member) {
            if (row.length === cols) { rows.push(row); row = []; }
            row.push(member);
          });
        });
        if (row.length) rows.push(row);
      });
      return rows;
    }
    var reading = [].concat.apply([], byLayer.map(function (list) { return [].concat.apply([], list); }));
    function folded(cols) {
      var rows = [];
      for (var k = 0; k < reading.length; k += cols) rows.push(reading.slice(k, k + cols));
      return rows;
    }
    var best = null;
    for (var cols = 1; cols <= Math.max(1, Math.min(n, 12)); cols++) {
      [[layered(cols), 1.25, false], [folded(cols), 1, true]].forEach(function (candidate) {
        var rows = candidate[0], width = cols * CARD_W + (cols - 1) * COL_GAP;
        var height = rows.length * CARD_H + (rows.length - 1) * ROW_GAP + 2 * PAD;
        var fit = Math.min(box.w / (width + 2 * PAD), box.h / height), score = fit * candidate[1];
        if (!best || score > best.score + 1e-9) best = {cols: cols, rows: rows, width: width, height: height, fit: fit, score: score, fold: candidate[2]};
      });
      if (cols >= n) break;
    }
    var rowsOf = best.rows, rowOf = new Array(n), slot = new Array(n);
    rowsOf.forEach(function (row, r) { row.forEach(function (member, k) { rowOf[member] = r; slot[member] = k; }); });
    var shown = [];
    links.forEach(function (edge) {
      var user = at.get(edge[0]), used = at.get(edge[1]), cu = component[user], cv = component[used];
      if (cu === cv || kept[cv].has(cu)) shown.push([used, user]);
    });
    for (var sweep = 0; sweep < (best.fold ? 0 : 4); sweep++) {
      var downward = sweep % 2 === 0;
      for (var r = downward ? 1 : rowsOf.length - 2; downward ? r < rowsOf.length : r >= 0; r += downward ? 1 : -1) {
        var row = rowsOf[r], centre = new Map();
        row.forEach(function (member) {
          var near = shown.filter(function (pair) { return downward ? pair[1] === member && rowOf[pair[0]] < r : pair[0] === member && rowOf[pair[1]] > r; })
            .map(function (pair) { return slot[downward ? pair[0] : pair[1]]; });
          centre.set(member, near.length ? near.reduce(function (a, b) { return a + b; }, 0) / near.length : slot[member]);
        });
        row.sort(function (a, b) { return centre.get(a) - centre.get(b) || a - b; });
        row.forEach(function (member, k) { slot[member] = k; });
      }
    }
    // Scale to the box, never past the size cards have when the part is fitted to the stage.
    var scale = Math.min(best.fit, box.unit);
    var x0 = box.x + (box.w - (best.width + 2 * PAD) * scale) / 2 + PAD * scale, y0 = box.y + PAD * scale;
    var cards = {};
    rowsOf.forEach(function (row, r) {
      var indent = (best.cols - row.length) * (CARD_W + COL_GAP) / 2;
      row.forEach(function (member, k) {
        cards[ids[member]] = {x: x0 + (indent + k * (CARD_W + COL_GAP)) * scale, y: y0 + r * (CARD_H + ROW_GAP) * scale,
          w: CARD_W * scale, h: CARD_H * scale, unit: scale, row: r};
      });
    });
    var area = {x: x0, y: y0, w: best.width * scale, h: (rowsOf.length * CARD_H + (rowsOf.length - 1) * ROW_GAP) * scale};
    var direct = [], implied = [];
    links.forEach(function (edge) {
      var user = at.get(edge[0]), used = at.get(edge[1]), cu = component[user], cv = component[used];
      (cu === cv || kept[cv].has(cu) ? direct : implied).push(edge);
    });
    var routes = bundleResults(cards, direct, area), hidden = bundleResults(cards, implied, area);
    var stubs = {};
    (boundaryEdges || []).forEach(function (edge) {
      var inside = at.has(edge[0]) ? edge[0] : at.has(edge[1]) ? edge[1] : null;
      if (inside === null || (at.has(edge[0]) && at.has(edge[1]))) return;
      var entry = stubs[inside] || (stubs[inside] = {uses: 0, usedBy: 0});
      if (inside === edge[0]) entry.uses++; else entry.usedBy++;
    });
    return {cards: cards, routes: routes, implied: hidden, stubs: stubs, cols: best.cols, fold: best.fold};
  }

  // Result routes: one per linked pair, all shown (the reduction already chose them).
  function bundleResults(cards, edges, area) {
    if (!edges.length) return [];
    var routes = bundleEdges(cards, edges, area);
    routes.forEach(function (route) { route.rest = true; });
    return routes;
  }

  // Labels for the boxes on screen at this camera. A box at least MIN_W wide whose band holds a title
  // line gets a full label; one at least COMPACT_W wide gets a compact one (13 px lines);
  // deeper boxes gain theirs as the camera closes in. A box whose band is still too thin, but whose
  // height has room for the line, gets it as an overlay over its top. An overlay covers the top of
  // its box's content, so a descendant's label under it moves just below it, or goes when its box
  // has no room there. Labels come shallowest first. A box in `closed` (an object keyed by id) shows
  // nothing inside, so its title may wrap down the whole box: `lines` is the most it can take.
  function placeLabels(boxes, camera, stage, closed) {
    var labels = [], placed = {};
    Object.keys(boxes).sort(function (a, b) { return (boxes[a].depth || 0) - (boxes[b].depth || 0) || byKey(a, b); }).forEach(function (id) {
      var box = boxes[id];
      var x = (box.x - camera.x) * camera.z, y = (box.y - camera.y) * camera.z;
      var w = box.w * camera.z, h = box.h * camera.z, band = box.band * box.unit * camera.z, pad = box.pad * box.unit * camera.z;
      if (x >= stage.w || y >= stage.h || x + w <= 0 || y + h <= 0 || !box.band) return;
      var compact = w < 0.9 * MIN_W, need = compact ? COMPACT_BAND : 0.9 * LABEL_BAND;
      if (w < COMPACT_W) return;
      var inBand = band >= need;
      if (!inBand && h < (compact ? COMPACT_BAND : LABEL_BAND)) return;
      var label = {id: id, x: x + pad, y: y, w: w - 2 * pad, band: inBand ? band : need, overlay: !inBand, compact: compact,
        lines: !compact && box.band === BAND2 && band >= 0.9 * BAND2 ? 2 : 1,
        detail: !compact && inBand && Math.min(band, h) >= 0.9 * BAND ? (band >= 120 ? "full" : "meta") : "title"};
      for (var up = box.parent; up && boxes[up]; up = boxes[up].parent) {
        var cover = placed[up];
        // An in-band label sits above its box's content, so only an overlay can cover a descendant.
        if (!cover || !cover.overlay) continue;
        var bottom = cover.y + cover.band;
        if (label.y >= bottom || label.y + label.band <= cover.y || label.x >= cover.x + cover.w || label.x + label.w <= cover.x) continue;
        if (bottom + need > y + h) return;
        Object.assign(label, {y: bottom, band: need, overlay: true, lines: 1, detail: "title"});
      }
      if (closed && closed[id]) {
        // The first baseline sits where the page draws it, and the details follow the last line.
        var first = compact ? Math.min(30, label.band / 2 + 5) + 4 : Math.min(30, label.band / 2 + 6) + 5;
        var room = y + h - pad - label.y - DETAIL_LINE * (label.detail === "full" ? 2 : label.detail === "meta" ? 1 : 0);
        label.lines = Math.max(label.lines, 1 + Math.floor((room - first) / (compact ? COMPACT_LINE : LINE)));
      }
      labels.push(label); placed[id] = label;
    });
    return labels;
  }

  // The camera that fits a box to the stage, centred.
  function fitCamera(box, stage) {
    var z = Math.min(stage.w / box.w, stage.h / box.h);
    return {x: box.x + box.w / 2 - stage.w / 2 / z, y: box.y + box.h / 2 - stage.h / 2 / z, z: z};
  }

  // The camera for opening a part: its content fitted to the stage, or up to twice as close when
  // most of its children would be too small to label there. Closer in, the view starts at the
  // content's top-left, where reading starts, leaving TITLE_ROOM pixels above it for the page to
  // pin the part's title, since the band itself has scrolled off.
  var TITLE_ROOM = 44;
  function focusCamera(box, children, stage) {
    var content = contentOf(box), fit = fitCamera(content, stage), needs = [];
    for (var id in children) {
      var child = children[id];
      if (child.band) needs.push(Math.max(0.9 * MIN_W / child.w, 0.9 * LABEL_BAND / (child.band * child.unit)));
    }
    needs.sort(function (a, b) { return a - b; });
    var z = Math.min(needs.length ? needs[Math.floor(needs.length / 2)] : 0, 2 * fit.z);
    if (z <= fit.z) return fit;
    return {x: content.x - PAD / z, y: content.y - TITLE_ROOM / z, z: z};
  }

  var api = {packLevels: packLevels, packPart: packPart, bundleEdges: bundleEdges, layoutCommunity: layoutCommunity,
    placeLabels: placeLabels, fitCamera: fitCamera, focusCamera: focusCamera, contentOf: contentOf, dependencyOrder: dependencyOrder,
    constants: {BAND: BAND, PAD: PAD, GAP: GAP, MIN_W: MIN_W, MIN_H: MIN_H, LINE: LINE, COMPACT_LINE: COMPACT_LINE}};
  if (typeof window !== "undefined") window.AutoformMapLayout = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
