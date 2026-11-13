/* A result's local dependency graph: pure, synchronous functions over the map's relations.json.
 *
 * relations.json lists what each result uses by its row in search.json. For one focus, the view is its reduced
 * ancestry: the transitive reduction of everything the focus uses, directly or not, where a result's depth is its
 * distance from the focus. Level 1 draws depth 2 and every direct use of the focus; level 2 adds depth 3; level 3 the
 * whole ancestry. Each level draws at most CAP results, the nearest first. Among what the focus builds on, a hub, a
 * result used by at least H others (the larger of 10 and the in-degree at the 98th percentile), is a chip, never a card.
 * A drawn result that no drawn edge reaches hangs from the last drawn result on a shortest reduced path by a "via n"
 * edge, n counting the results skipped. What the focus feeds into is its direct users, the most used first, up to FEEDS
 * of them. A level that draws nothing the one below it does not adds nothing, as for a result that builds on nothing
 * or one whose whole ancestry level 1 already draws.
 */
(function () {
  "use strict";

  var CAP = 150, FEEDS = 8, HUB_FLOOR = 10, HUB_SHARE = 0.98;
  // World units, as the map's result cards: columns of cards, the focus's column at x = 0 and its uses to the left.
  var CARD_W = 200, CARD_H = 64, COLUMN_GAP = 96, ROW_GAP = 20;

  // Each row's uses (statement links first, then links only in a proof) and its users, as flat arrays, and the hub
  // threshold. A use of no row throws, so no walk below leaves the graph.
  function graph(payload) {
    var n = payload.status.length, start = new Int32Array(n + 1), total = 0, row, k;
    for (row = 0; row < n; row++) { start[row] = total; total += payload.uses[row].length + payload.proof[row].length; }
    start[n] = total;
    var to = new Int32Array(total), proof = new Uint8Array(total), indegree = new Int32Array(n);
    for (row = 0, k = 0; row < n; row++) {
      [payload.uses[row], payload.proof[row]].forEach(function (list, only) {
        list.forEach(function (used) {
          if (!(Number.isInteger(used) && used >= 0 && used < n)) throw new RangeError("row " + row + " uses no row " + used);
          to[k] = used; proof[k] = only; indegree[used]++; k++;
        });
      });
    }
    var userStart = new Int32Array(n + 1), users = new Int32Array(total), fill = new Int32Array(n);
    for (row = 0; row < n; row++) userStart[row + 1] = userStart[row] + indegree[row];
    for (row = 0; row < n; row++) for (k = start[row]; k < start[row + 1]; k++) users[userStart[to[k]] + fill[to[k]]++] = row;
    var sorted = Int32Array.from(indegree).sort();
    return {n: n, start: start, to: to, proof: proof, userStart: userStart, users: users, indegree: indegree,
      hub: Math.max(HUB_FLOOR, n ? sorted[Math.ceil(HUB_SHARE * n) - 1] : 0), statuses: payload.statuses, status: payload.status,
      digest: payload.rows_sha256, captions: payload.captions, caption: payload.caption,
      seen: new Int32Array(n), at: new Int32Array(n), stamp: 0};
  }

  // The focus's ancestry in topological order (the focus first, every result before what it uses) with each row's
  // position in it. An edge back to a result still being explored closes a cycle and is left out, so the published
  // graph need not be acyclic.
  function ancestry(g, focus) {
    var stamp = ++g.stamp, post = [], stack = [focus], next = [g.start[focus]];
    g.seen[focus] = stamp;
    while (stack.length) {
      var top = stack.length - 1, u = stack[top];
      if (next[top] < g.start[u + 1]) {
        var v = g.to[next[top]++];
        if (g.seen[v] !== stamp) { g.seen[v] = stamp; stack.push(v); next.push(g.start[v]); }
        continue;
      }
      post.push(u); stack.pop(); next.pop();
    }
    var order = post.reverse();
    order.forEach(function (row, index) { g.at[row] = index; });
    return order;
  }

  // The transitive reduction of the ancestry, and each result's depth and parent on a shortest path from the focus in
  // it. Positions are indices into `order`. A result's children are taken nearest first, so a child some earlier child
  // reaches is implied by it; the reach of each result is a bitset over the positions after its own.
  function reduce(g, order) {
    var k = order.length, words = (k + 31) >>> 5, reach = new Int32Array(k * words);
    var kids = new Array(k), kind = new Array(k);
    for (var i = k - 1; i >= 0; i--) {
      var u = order[i], base = i * words, list = [];
      for (var e = g.start[u]; e < g.start[u + 1]; e++) {
        var p = g.at[g.to[e]];
        if (p > i) list.push(p * 2 + g.proof[e]);
      }
      list.sort(function (a, b) { return a - b; });
      var keep = [], kinds = [];
      for (var j = 0; j < list.length; j++) {
        var child = list[j] >> 1;
        if (reach[base + (child >>> 5)] & (1 << (child & 31))) continue;
        keep.push(child); kinds.push(list[j] & 1);
        reach[base + (child >>> 5)] |= 1 << (child & 31);
        for (var w = child >>> 5, other = child * words; w < words; w++) reach[base + w] |= reach[other + w];
      }
      kids[i] = keep; kind[i] = kinds;
    }
    var depth = new Int32Array(k).fill(-1), parent = new Int32Array(k).fill(-1), queue = [0];
    depth[0] = 0;
    for (var head = 0; head < queue.length; head++) {
      var at = queue[head];
      kids[at].forEach(function (c) { if (depth[c] < 0) { depth[c] = depth[at] + 1; parent[c] = at; queue.push(c); } });
    }
    return {kids: kids, kind: kind, depth: depth, parent: parent};
  }

  // The view of `focus` (a row) at `level` (1, 2 or 3), at most `cap` results drawn. Rows throughout:
  // nodes [{row, depth}] (depth is the column: 1 for a direct use), edges [user, used, proof] between the focus, the
  // drawn results and the feeds, via [user, used, skipped], chips and feeds (the most used first), the feeds' total,
  // and `total`, every result the focus builds on, hubs too.
  function view(g, focus, level, cap) {
    var order = ancestry(g, checked(g, focus));
    return cut(g, focus, order, reduce(g, order), level, cap || CAP);
  }

  // The three levels' views of `focus` from one walk of its ancestry, and `adds`: whether each level draws anything the
  // one below it does not, a card, an edge, a via edge, a feed or a chip. Level 1 always adds.
  function levels(g, focus, cap) {
    var order = ancestry(g, checked(g, focus)), red = reduce(g, order);
    var views = [1, 2, 3].map(function (level) { return cut(g, focus, order, red, level, cap || CAP); });
    var drawn = views.map(function (v) {
      return JSON.stringify([v.nodes, v.edges.map(String).sort(), v.via.map(String).sort(), v.feeds, v.chips]);
    });
    return {views: views, adds: drawn.map(function (key, index) { return !index || key !== drawn[index - 1]; })};
  }

  // The focus, which must be a row.
  function checked(g, focus) {
    if (!(Number.isInteger(focus) && focus >= 0 && focus < g.n)) throw new RangeError("relations.json has no row " + focus);
    return focus;
  }

  // One level's view, cut from the focus's ancestry in topological order and its reduction.
  function cut(g, focus, order, red, level, cap) {
    var k = order.length, limit = level >= 3 ? Infinity : level + 1;
    var direct = new Set();
    for (var e = g.start[focus]; e < g.start[focus + 1]; e++) direct.add(g.at[g.to[e]]);
    var wanted = [], chips = [];
    for (var p = 1; p < k; p++) {
      if (!direct.has(p) && red.depth[p] > limit) continue;
      if (g.indegree[order[p]] >= g.hub) chips.push(order[p]); else wanted.push(p);
    }
    // Nearest first: direct uses, then by depth, then in search.json's order.
    wanted.sort(function (a, b) { return direct.has(b) - direct.has(a) || red.depth[a] - red.depth[b] || order[a] - order[b]; });
    var drawn = new Set(wanted.slice(0, cap)), edges = [], reached = new Set();
    drawn.forEach(function (q) {
      red.kids[q].forEach(function (c, index) { if (drawn.has(c)) { edges.push([order[q], order[c], red.kind[q][index]]); reached.add(c); } });
    });
    var kept = new Set(red.kids[0]);
    red.kids[0].forEach(function (c, index) { if (drawn.has(c)) { edges.push([focus, order[c], red.kind[0][index]]); reached.add(c); } });
    // A direct use the reduction routes the long way round keeps its own edge from the focus.
    for (e = g.start[focus]; e < g.start[focus + 1]; e++) {
      var used = g.at[g.to[e]];
      if (drawn.has(used) && !kept.has(used)) { edges.push([focus, order[used], g.proof[e]]); reached.add(used); }
    }
    var via = [];
    drawn.forEach(function (q) {
      if (reached.has(q)) return;
      var skipped = 0, up = red.parent[q];
      while (up > 0 && !drawn.has(up)) { skipped++; up = red.parent[up]; }
      via.push([order[up], order[q], skipped]);
    });
    var nodes = Array.from(drawn).sort(function (a, b) { return a - b; }).map(function (q) {
      return {row: order[q], depth: direct.has(q) ? 1 : red.depth[q]};
    });
    // A user the focus also builds on (a cycle) is drawn in its column or listed as a chip, not again among the feeds.
    var chipped = new Set(chips);
    var users = Array.from(new Set(g.users.subarray(g.userStart[focus], g.userStart[focus + 1]))).filter(function (user) {
      return !(chipped.has(user) || g.seen[user] === g.stamp && drawn.has(g.at[user]));
    });
    users.sort(function (a, b) { return g.indegree[b] - g.indegree[a] || a - b; });
    var feeds = users.slice(0, FEEDS);
    return {focus: focus, level: level, nodes: nodes, edges: edges.concat(feeds.map(function (user) {
      for (var link = g.start[user]; link < g.start[user + 1] && g.to[link] !== focus; link++);
      return [user, focus, g.proof[link]];
    })), via: via, chips: chips.sort(function (a, b) { return g.indegree[b] - g.indegree[a] || a - b; }),
    feeds: feeds, feedsTotal: users.length, total: k - 1, hub: g.hub};
  }

  // Cards in columns, right to left: what the focus feeds into, the focus, then each depth. A column taller than
  // `rows` cards wraps into more columns beside it, further out. Barycentre sweeps keep linked cards level, so most
  // edges run straight. Returns {boxes: {row: box}, more: the point under the feeds for "+n more", bounds}.
  function layout(v) {
    var columns = [], links = new Map();
    function column(d) { return columns[d] || (columns[d] = []); }
    function link(a, b) { (links.get(a) || links.set(a, []).get(a)).push(b); (links.get(b) || links.set(b, []).get(b)).push(a); }
    v.nodes.forEach(function (node) { column(node.depth).push(node.row); });
    v.edges.concat(v.via).forEach(function (edge) { link(edge[0], edge[1]); });
    var rows = Math.max(6, Math.ceil(Math.sqrt(v.nodes.length * 1.5))), y = new Map([[v.focus, 0]]), x = new Map();
    v.feeds.forEach(function (row, index) { y.set(row, (index - (v.feeds.length - 1) / 2) * (CARD_H + ROW_GAP)); });
    function place(list) {
      list.forEach(function (row, index) {
        var height = Math.min(rows, list.length - Math.floor(index / rows) * rows);
        y.set(row, (index % rows - (height - 1) / 2) * (CARD_H + ROW_GAP));
      });
    }
    function centre(row) {
      var near = (links.get(row) || []).filter(function (other) { return y.has(other); });
      return near.length ? near.reduce(function (sum, other) { return sum + y.get(other); }, 0) / near.length : Infinity;
    }
    columns.forEach(function (list) { list.sort(function (a, b) { return a - b; }); place(list); });
    // Outward from the focus, back in, and out again; ties keep search.json's order.
    [1, -1, 1].forEach(function (way) {
      var depths = columns.map(function (_list, d) { return d; }).filter(function (d) { return columns[d] && d > 0; });
      if (way < 0) depths.reverse();
      depths.forEach(function (d) {
        var keys = new Map(columns[d].map(function (row) { return [row, centre(row)]; }));
        columns[d].sort(function (a, b) { return keys.get(a) - keys.get(b) || a - b; });
        place(columns[d]);
      });
    });
    var step = CARD_W + COLUMN_GAP, left = 0;
    x.set(v.focus, 0);
    v.feeds.forEach(function (row) { x.set(row, step); });
    columns.forEach(function (list, d) {
      if (!d || !list) return;
      list.forEach(function (row, index) { x.set(row, -step * (left + 1 + Math.floor(index / rows))); });
      left += Math.ceil(list.length / rows);
    });
    var boxes = {}, x0 = 0, y0 = 0, x1 = CARD_W, y1 = CARD_H;
    x.forEach(function (at, row) {
      var box = boxes[row] = {x: at, y: y.get(row) - CARD_H / 2, w: CARD_W, h: CARD_H};
      x0 = Math.min(x0, box.x); y0 = Math.min(y0, box.y); x1 = Math.max(x1, box.x + box.w); y1 = Math.max(y1, box.y + box.h);
    });
    var more = v.feedsTotal > v.feeds.length ? {x: step, y: (v.feeds.length / 2) * (CARD_H + ROW_GAP) + 4} : null;
    if (more) y1 = Math.max(y1, more.y + 24);
    var pad = 40;
    return {boxes: boxes, more: more, bounds: {x: x0 - pad, y: y0 - pad, w: x1 - x0 + 2 * pad, h: y1 - y0 + 2 * pad}};
  }

  window.AutoformLocalGraph = {graph: graph, view: view, levels: levels, layout: layout,
    constants: {CAP: CAP, FEEDS: FEEDS, CARD_W: CARD_W, CARD_H: CARD_H}};
})();
