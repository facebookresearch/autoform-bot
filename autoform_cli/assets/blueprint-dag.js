/* Hand-maintained explorer source. Rebuild blueprint-explorer.min.js after edits. */
(function () {
  "use strict";
  if (window.__AUTOFORM_DAG_VIEWER_V2__) return;
  window.__AUTOFORM_DAG_VIEWER_V2__ = true;

  var NODE_W = 196, NODE_H = 56, MAX_MAP_NODES = 120, LIST_PAGE = 100;
  var SEARCH_PAGE = 50, MIN_SCALE = .002, hostSequence = 0;
  var clients = new Map();
  var clamp = function (value, low, high) { return Math.max(low, Math.min(high, value)); };
  var plural = function (count, one, many) { return count.toLocaleString() + " " + (count === 1 ? one : many); };
  var num = function (value) { return value.toLocaleString(); };
  var pressed = function (control, value) { control.setAttribute("aria-pressed", String(value)); };
  // The book's caption for each declaration (render.DECLARATION_LABELS), so a card reads "Definition · defined" beside "Definition 1".
  var KINDS = new Map(Object.entries({abbrev: "Abbreviation", axiom: "Axiom", class: "Class", corollary: "Corollary", def: "Definition",
    definition: "Definition", example: "Example", inductive: "Inductive", instance: "Instance", lemma: "Lemma", proposition: "Proposition",
    structure: "Structure", theorem: "Theorem"}));

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

  // Only publisher-derived label parts enter reading controls. Never parse
  // provider HTML, replace an interactive link, or alter graph-card geometry.
  function readingLabel(tag, className, text, label, prefix) {
    prefix = prefix || "";
    var root = element(tag, className, prefix + text);
    if (!label || label.format !== "inline-parts-v1" || label.source !== text || !Array.isArray(label.parts) || label.parts.length > 256 || text.length > 16384) return root;
    var valid = label.parts.every(function (part) {
      return Array.isArray(part) && part.length === 2 && ["text", "code", "math"].indexOf(part[0]) >= 0 && typeof part[1] === "string" &&
        (part[0] !== "math" || /^\\\([\s\S]*\\\)$|^\\\[[\s\S]*\\\]$/.test(part[1]));
    });
    if (!valid || !label.parts.some(function (part) { return part[0] === "math"; }) ||
        label.parts.reduce(function (size, part) { return size + part[1].length; }, 0) > 32768) return root;
    root.textContent = "";
    if (prefix) root.appendChild(document.createTextNode(prefix));
    label.parts.forEach(function (part) {
      if (part[0] === "text") root.appendChild(document.createTextNode(part[1]));
      else root.appendChild(element(part[0] === "code" ? "code" : "span", part[0] === "math" ? "arithmatex" : "", part[1]));
    });
    return root;
  }

  function refreshReadingLabels(root) {
    if (root && window.AutoformMath && typeof window.AutoformMath.refresh === "function")
      Promise.resolve(window.AutoformMath.refresh(root)).catch(function () {});
  }

  function safeHref(value) {
    if (typeof value !== "string" || !value || /[\u0000-\u001f\u007f]/.test(value)) return null;
    try {
      var url = new URL(value, window.location.href);
      return /^(https?:)$/.test(url.protocol) && url.origin === window.location.origin && !url.username && !url.password ? value : null;
    } catch (_error) { return null; }
  }

  function start(host) {
    if (host.getAttribute("data-dag-ready") === "true") return;
    // A bare graph host has linked reading/data fallbacks, not a second client.
    if (!window.AutoformNavigation || !host.closest(".bp-explorer-page")) return;
    host.setAttribute("data-dag-ready", "true");
    host.classList.add("bp-dag-enhancing");
    var loading = host.querySelector(".bp-dag-loading"); if (loading) loading.hidden = false;
    var instance = ++hostSequence;
    var client = null, index = null, ui$wikiLinks = null;
    var navigation = window.AutoformNavigation.create({
      host: host,
      onCommit: function (bundle) {
        if (!host.isConnected) { navigation.dispose(); return; }
        // Cold layout must measure the actual full-page shell, not Material's
        // old content column. The error path below restores native reading.
        document.body.classList.add("bp-dag-app-page");
        var ready;
        if (!client) { client = mount(host, bundle.graph, instance, bundle, navigation); clients.set(host, client); ready = client.ready; }
        else ready = client.applyRoute(bundle);
        if (index) client.useIndex(index);
        if (ui$wikiLinks) client.useWikiLinks(ui$wikiLinks);
        return ready;
      },
      onIndex: function (loaded) { index = loaded; if (client) client.useIndex(loaded); },
      onWikiLinks: function (loaded) {
        if (!host.isConnected) { navigation.dispose(); return; }
        ui$wikiLinks = loaded; if (client) client.useWikiLinks(loaded);
      },
      captureState: function () { return client ? client.captureState() : {}; },
      restoreState: function (snapshot, bundle) { if (client) return client.restoreState(snapshot, bundle); },
      onError: function (error) {
        if (client) client.showError(error);
        else {
          host.classList.remove("bp-dag-enhancing"); document.body.classList.remove("bp-dag-app-page");
          if (loading) { loading.textContent = "The interactive map is unavailable: " + error.message + ". The complete mathematical page remains readable."; loading.classList.add("bp-dag-error"); }
        }
      }
    });
    clients.set(host, {dispose: function () { navigation.dispose(); }});
    navigation.start();
  }

  function graphAt(payload, base) {
    if (!payload || payload.schema !== "autoform-dag-view/v2" || !payload.palette ||
        !Array.isArray(payload.nodes) || !Array.isArray(payload.edges)) throw new Error("unsupported graph payload");
    var link = function (value) { try { return value ? safeHref(new URL(value, base || location.href).href) : null; } catch (_error) { return null; } };
    var view = Object.assign({}, payload.view || {}); view.book = link(view.book);
    view.breadcrumbs = (view.breadcrumbs || []).map(function (entry) { return Object.assign({}, entry, {url: link(entry.url)}); });
    return Object.assign({}, payload, {view: view, edges: payload.edges.slice(), nodes: payload.nodes.map(function (node) {
      return Object.assign({}, node, {url: link(node.url)});
    })});
  }

  function relationCounts(edges, id) {
    var before = new Map(), after = new Map();
    edges.forEach(function (edge) {
      var array = Array.isArray(edge), source = array ? edge[0] : edge.source, target = array ? edge[1] : edge.target;
      var statement = (array ? edge[2] : edge.statement) || 0, proof = (array ? edge[3] : edge.proof) || 0;
      if (source !== id && target !== id) return;
      [[before, source, target === id], [after, target, source === id]].forEach(function (entry) {
        if (!entry[2]) return;
        var counts = entry[0].get(entry[1]) || {statement: 0, proof: 0};
        counts.statement += statement; counts.proof += proof; entry[0].set(entry[1], counts);
      });
    });
    return {before: before, after: after};
  }

  // Pure projection onto a complete, bounded display view. Optional endpoints
  // map canonical IDs to existing display IDs (including region replacements).
  // The Promise yields during large scans; callers cache it and guard publication.
  function projectWikiLinks(view, records, state, endpoints) {
    var availability = state && state.availability, empty = {availability: "unavailable", edges: [], unavailableOwners: []};
    if (!state || state.schema !== "autoform-wiki-links/v1" || ["complete", "partial"].indexOf(availability) < 0 ||
        !Array.isArray(state.references) || !Array.isArray(state.unavailable_sources) ||
        availability === "complete" && state.unavailable_sources.length || !view || !Array.isArray(view.nodes) ||
        view.nodes.length > MAX_MAP_NODES) return Promise.resolve(empty);
    var visible = new Map(), owners = new Map(), eligible = new Set(), ambiguous = new Set(), membership = 0;
    function wrapper(record) { return ["source", "collection", "transparent"].indexOf(record.navigation) >= 0; }
    function canonical(node) {
      if (node._overviewGroup || node._region) return null;
      // Match recordId: literal scope:/boundary: names survive by identity or
      // _recordId; only actual display prefixes are removed once.
      var id = node._recordId || (records.get(node.id) === node || ["scope", "boundary"].indexOf(node.kind) < 0 ?
        node.id : node.id.replace(/^(scope|boundary):/, ""));
      return records.has(id) ? id : null;
    }
    function assign(id, owner) {
      if (!id || wrapper(records.get(id))) return;
      if (owners.has(id) && owners.get(id) !== owner) ambiguous.add(id);
      else owners.set(id, owner);
    }
    for (var i = 0; i < view.nodes.length; i++) {
      var node = view.nodes[i];
      if (!node || typeof node.id !== "string" || visible.has(node.id)) return Promise.resolve(empty);
      visible.set(node.id, node);
      var id = canonical(node); assign(id, node.id);
      if (id && !wrapper(records.get(id)) || node._region && !node._overviewGroup) eligible.add(node.id);
    }
    var groups = view._scopeOverview && view._scopeOverview.groups || [];
    if (!Array.isArray(groups) || groups.length > MAX_MAP_NODES) return Promise.resolve(empty);
    for (var g = 0; g < groups.length; g++) {
      var group = groups[g], shown = visible.get(group.id);
      if (!shown || shown._overviewGroup !== group.key) continue;
      if (!Array.isArray(group.members) || (membership += group.members.length) > MAX_MAP_NODES) return Promise.resolve(empty);
      eligible.add(group.id);
      group.members.forEach(function (member) { assign(canonical(member), group.id); });
    }
    var memo = new Map();
    async function resolve(id) {
      if (memo.has(id)) return memo.get(id).owner;
      var chain = [], seen = new Set(), pending = id, suffix = {safe: true, owner: null};
      while (pending !== null && pending !== undefined) {
        if (memo.has(pending)) { suffix = memo.get(pending); break; }
        var record = records.get(pending);
        if (!record || seen.has(pending)) { suffix = {safe: false, owner: null}; break; }
        seen.add(pending); chain.push(record);
        if (record.navigation === "source" || ambiguous.has(pending)) { suffix = {safe: false, owner: null}; break; }
        pending = record.parent;
        if (chain.length % 1024 === 0) await new Promise(function (done) { setTimeout(done, 0); });
      }
      for (var position = chain.length - 1; position >= 0; position--) {
        var current = chain[position], owner = owners.get(current.id), safe = suffix.safe;
        if (!wrapper(current) && endpoints && endpoints.has(current.id)) {
          owner = endpoints.get(current.id);
          if (!eligible.has(owner)) safe = false;
        }
        suffix = {safe: safe, owner: safe ? !wrapper(current) && owner || suffix.owner : null};
        memo.set(current.id, suffix);
        if ((chain.length - position) % 1024 === 0 && position)
          await new Promise(function (done) { setTimeout(done, 0); });
      }
      return suffix.owner;
    }
    return (async function () {
      var references = state.references, missing = state.unavailable_sources, edges = new Map(), pairs = new Map(), unknown = new Set();
      for (var row = 0; row < references.length + missing.length; row++) {
        if (row >= references.length) {
          var missingId = missing[row - references.length], missingRecord = records.get(missingId);
          if (missingRecord && !wrapper(missingRecord)) { var unavailable = await resolve(missingId); if (unavailable) unknown.add(unavailable); }
        } else {
          var pair = references[row], source = pair && records.get(pair[0]), target = pair && records.get(pair[1]);
          if (Array.isArray(pair) && pair.length === 2 && source && target && !wrapper(source) && !wrapper(target)) {
            var from = await resolve(pair[0]), to = await resolve(pair[1]);
            if (from && to && from !== to) {
              var targets = pairs.get(pair[0]);
              if (!targets || !targets.has(pair[1])) {
                if (!targets) { targets = new Set(); pairs.set(pair[0], targets); }
                targets.add(pair[1]);
                var key = JSON.stringify([from, to]), edge = edges.get(key);
                if (edge) edge.count++;
                else edges.set(key, {source: from, target: to, count: 1});
              }
            }
          }
        }
        if ((row + 1) % 1024 === 0 && row + 1 < references.length + missing.length)
          await new Promise(function (done) { setTimeout(done, 0); });
      }
      return {availability: availability, edges: Array.from(edges.values()).sort(function (a, b) {
        return a.source < b.source ? -1 : a.source > b.source ? 1 : a.target < b.target ? -1 : a.target > b.target ? 1 : 0;
      }), unavailableOwners: Array.from(unknown).sort()};
    })();
  }

  // Deterministic, bounded scene geometry; public graph data remains untouched.
  function layoutGraph(nodes, edges, direction, stageAspect, wikiEdges) {
    var boxes = new Map(), routes = [], work = {ticks: 0, pairs: 0, moves: 0, candidates: 0, escapes: 0};
    if (nodes.length > MAX_MAP_NODES) return null;
    if (!nodes.length) return {nodes: boxes, routes: routes, clusters: [], work: work, bounds: {x: 0, y: 0, w: 1, h: 1}};
    var vertical = direction === "tb", gap = 48, ordered = nodes.slice().sort(function (a, b) { return a.id < b.id ? -1 : a.id > b.id ? 1 : 0; });
    var ids = new Map(), vertices = ordered.map(function (node, index) {
      ids.set(node.id, index);
      if (!(node.width > 0 && node.height > 0 && Number.isFinite(node.width) && Number.isFinite(node.height))) throw new Error("Invalid dependency card dimensions");
      return {id: node.id, w: vertical ? node.height : node.width, h: vertical ? node.width : node.height,
        area: node.area, scope: node.scope, x: 0, y: 0, degree: 0};
    }), links = new Map(), adjacency = vertices.map(function () { return []; }), parent = vertices.map(function (_node, index) { return index; });
    function root(index) { while (parent[index] !== index) { parent[index] = parent[parent[index]]; index = parent[index]; } return index; }
    edges.forEach(function (edge, edgeIndex) {
      var a = ids.get(edge.source), b = ids.get(edge.target);
      if (a === undefined || b === undefined) throw new Error("Unknown dependency endpoint");
      ["statement", "proof"].forEach(function (kind) {
        if (edge[kind] > 0) routes.push({source: edge.source, target: edge.target, kind: kind, count: edge[kind], edgeIndex: edgeIndex});
      });
      var weight = Math.max(0, edge.statement || 0) + Math.max(0, edge.proof || 0); if (!weight) return;
      var key = a + ":" + b, link = links.get(key);
      if (link) link.weight += weight;
      else { links.set(key, {a: a, b: b, weight: weight}); adjacency[a].push(b); }
      parent[root(b)] = root(a);
    });
    routes.sort(function (a, b) { return a.source < b.source ? -1 : a.source > b.source ? 1 :
      a.target < b.target ? -1 : a.target > b.target ? 1 : a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : a.count - b.count; });
    adjacency.forEach(function (neighbors) { neighbors.sort(function (a, b) { return a - b; }); });
    var springs = Array.from(links.values()).sort(function (a, b) { return a.a - b.a || a.b - b.b; });
    var islands = [], islandIds = new Map();
    vertices.forEach(function (node, index) {
      var key = root(index), island = islandIds.get(key);
      if (island === undefined) { island = islands.length; islandIds.set(key, island); islands.push({key: node.id, members: [], components: [], links: 0}); }
      node.island = island; islands[island].members.push(index);
    });
    // Ordinary article links are affinities, not directed mathematical edges.
    // Coalesce duplicate observations without changing the formal adjacency.
    var associations = new Map(), familyParents = islands.map(function (_island, index) { return index; });
    function familyRoot(index) { while (familyParents[index] !== index) { familyParents[index] = familyParents[familyParents[index]]; index = familyParents[index]; } return index; }
    (Array.isArray(wikiEdges) && wikiEdges.length <= nodes.length * nodes.length ? wikiEdges : []).forEach(function (edge) {
      if (!edge || !Number.isFinite(edge.count) || edge.count <= 0) return;
      var a = ids.get(edge.source), b = ids.get(edge.target); if (a === undefined || b === undefined || a === b) return;
      var first = Math.min(a, b), last = Math.max(a, b), key = first + ":" + last;
      var found = associations.get(key);
      if (!found) { found = {a: first, b: last, forward: 0, backward: 0}; associations.set(key, found); }
      var kind = a === first ? "forward" : "backward";
      found[kind] = Math.max(found[kind], Math.min(Number.MAX_SAFE_INTEGER / 2, edge.count));
    });
    var wiki = Array.from(associations.values()).sort(function (a, b) { return a.a - b.a || a.b - b.b; });
    wiki.forEach(function (link) {
      link.count = link.forward + link.backward;
      var a = vertices[link.a].island, b = vertices[link.b].island;
      if (a !== b) familyParents[familyRoot(b)] = familyRoot(a);
    });
    var wikiFamilies = [], wikiWork = {ticks: 0, pairs: 0, moves: 0};
    var sequence = 0, stack = [], components = [], visitIndex = [], low = [], onStack = [], componentOf = [];
    function visit(index) {
      visitIndex[index] = low[index] = sequence++; stack.push(index); onStack[index] = true;
      adjacency[index].forEach(function (next) {
        if (visitIndex[next] === undefined) { visit(next); low[index] = Math.min(low[index], low[next]); }
        else if (onStack[next]) low[index] = Math.min(low[index], visitIndex[next]);
      });
      if (low[index] !== visitIndex[index]) return;
      var members = [], next;
      do { next = stack.pop(); onStack[next] = false; componentOf[next] = components.length; members.push(next); } while (next !== index);
      members.sort(function (a, b) { return a - b; });
      components.push({members: members, key: vertices[members[0]].id, rank: 0, indegree: 0, after: new Set()});
    }
    vertices.forEach(function (_node, index) { if (visitIndex[index] === undefined) visit(index); });
    components.forEach(function (component, index) { islands[vertices[component.members[0]].island].components.push(index); });
    springs.forEach(function (link) {
      var a = componentOf[link.a], b = componentOf[link.b];
      if (a !== b && !components[a].after.has(b)) { components[a].after.add(b); components[b].indegree++; }
      islands[vertices[link.a].island].links++;
      vertices[link.a].degree++; vertices[link.b].degree++;
    });
    islands.forEach(function (island) {
      var queue = island.components.filter(function (index) { return !components[index].indegree; });
      while (queue.length) {
        queue.sort(function (a, b) { return components[a].key < components[b].key ? -1 : 1; });
        var first = queue.shift(), component = components[first];
        component.after.forEach(function (next) {
          components[next].rank = Math.max(components[next].rank, component.rank + 1);
          if (!--components[next].indegree) queue.push(next);
        });
      }
    });
    function overlap(a, b, padding) {
      return a.x < b.x + b.w + padding && a.x + a.w > b.x - padding && a.y < b.y + b.h + padding && a.y + a.h > b.y - padding;
    }
    function bounds(points) {
      var x = Infinity, y = Infinity, right = -Infinity, bottom = -Infinity;
      points.forEach(function (point) { x = Math.min(x, point.x); y = Math.min(y, point.y); right = Math.max(right, point.x); bottom = Math.max(bottom, point.y); });
      return {x: x, y: y, w: right - x, h: bottom - y};
    }
    components.forEach(function (component) {
      var size = component.members.length, dimension = component.members.reduce(function (largest, index) { return Math.max(largest, vertices[index].w, vertices[index].h); }, 0);
      var placed = [], position = 0;
      component.members.forEach(function (index, ordinal) {
        var node = vertices[index], clear = false;
        for (var attempt = 0; attempt < 32 && !clear; attempt++) {
          var radius = (dimension + gap) * Math.sqrt(position) * .9, angle = position++ * 2.399963229728653;
          node.x = Math.cos(angle) * radius - node.w / 2; node.y = Math.sin(angle) * radius - node.h / 2;
          clear = !placed.some(function (other) { return overlap(node, other, gap); });
        }
        if (!clear) node.x = Math.max.apply(null, placed.map(function (other) { return other.x + other.w; })) + gap;
        placed.push(node);
      });
      var extent = bounds(placed.reduce(function (all, node) { return all.concat([{x: node.x, y: node.y}, {x: node.x + node.w, y: node.y + node.h}]); }, []));
      component.w = extent.w; component.h = extent.h;
      placed.forEach(function (node) { node.x -= extent.x; node.y -= extent.y; });
    });
    islands.forEach(function (island) {
      var ranks = [], primary = 36;
      island.components.forEach(function (index) { var component = components[index]; (ranks[component.rank] || (ranks[component.rank] = [])).push(component); });
      ranks.forEach(function (rank) {
        rank.sort(function (a, b) { return a.key < b.key ? -1 : 1; });
        var width = Math.max.apply(null, rank.map(function (component) { return component.w; }));
        var height = rank.reduce(function (sum, component) { return sum + component.h + 96; }, -96), secondary = -height / 2;
        rank.forEach(function (component) {
          component.members.forEach(function (index) {
            var node = vertices[index]; node.x += primary + (width - component.w) / 2; node.y += secondary;
            node.seedX = node.x; node.seedY = node.y; node.cluster = componentOf[index]; node.rank = component.rank;
          });
          secondary += component.h + 96;
        });
        primary += width + 144;
      });
    });
    var cohorts = [];
    islands.forEach(function (island) {
      var groups = new Map();
      island.members.forEach(function (index) {
        var node = vertices[index], key = node.area || node.scope || "component:" + root(index), group = groups.get(key) || [];
        group.push(index); groups.set(key, group);
      });
      groups.forEach(function (group) { cohorts.push(group); });
    });
    // Every accepted move preserves separated rectangles; no unbounded collision repair.
    for (var tick = 0; tick < 32; tick++) {
      var forces = vertices.map(function (node) { return {x: (node.seedX - node.x) * .04, y: (node.seedY - node.y) * .025}; });
      springs.forEach(function (link) {
        if (link.a === link.b) return;
        var a = vertices[link.a], b = vertices[link.b], dx = b.x + b.w / 2 - a.x - a.w / 2, dy = b.y + b.h / 2 - a.y - a.h / 2;
        var distance = Math.max(1, Math.hypot(dx, dy)), wanted = (Math.hypot(a.w, a.h) + Math.hypot(b.w, b.h)) / 2 + gap;
        var strength = Math.min(3, 1 + Math.log1p(link.weight) / 8) * (distance - wanted) / distance * .08;
        forces[link.a].x += dx * strength / Math.max(1, a.degree); forces[link.a].y += dy * strength / Math.max(1, a.degree);
        forces[link.b].x -= dx * strength / Math.max(1, b.degree); forces[link.b].y -= dy * strength / Math.max(1, b.degree);
      });
      cohorts.forEach(function (members) {
        var center = members.reduce(function (sum, index) { var node = vertices[index]; return sum + node.y + node.h / 2; }, 0) / members.length;
        members.forEach(function (index) { var node = vertices[index]; forces[index].y += (center - node.y - node.h / 2) * .012; });
      });
      vertices.forEach(function (a, index) {
        vertices.slice(index + 1).forEach(function (b, next) {
          work.pairs++;
          if (a.island !== b.island) return;
          var dx = b.x + b.w / 2 - a.x - a.w / 2, dy = b.y + b.h / 2 - a.y - a.h / 2, distance = Math.max(1, dx * dx + dy * dy);
          var push = Math.min(2, 5000 / distance);
          forces[index].x -= dx / Math.sqrt(distance) * push; forces[index].y -= dy / Math.sqrt(distance) * push;
          forces[index + next + 1].x += dx / Math.sqrt(distance) * push; forces[index + next + 1].y += dy / Math.sqrt(distance) * push;
        });
      });
      vertices.forEach(function (node, index) {
        var dx = Math.max(-12, Math.min(12, forces[index].x)), dy = Math.max(-12, Math.min(12, forces[index].y));
        [[dx, dy], [0, dy], [dx, 0]].some(function (move) {
          var candidate = {x: Math.max(node.seedX - 64, Math.min(node.seedX + 64, node.x + move[0])),
            y: Math.max(node.seedY - 96, Math.min(node.seedY + 96, node.y + move[1])), w: node.w, h: node.h};
          var clear = vertices.every(function (other) { work.moves++; return other === node || other.island !== node.island || !overlap(candidate, other, gap); });
          if (clear) { node.x = candidate.x; node.y = candidate.y; } return clear;
        });
      });
      work.ticks++;
    }
    islands.forEach(function (island) {
      island.extent = bounds(island.members.reduce(function (all, index) { var node = vertices[index]; return all.concat([{x: node.x, y: node.y}, {x: node.x + node.w, y: node.y + node.h}]); }, []));
    });
    function point(at) { return vertical ? {x: at.y, y: at.x} : {x: at.x, y: at.y}; }
    function rectangle(box) { return vertical ? {x: box.y, y: box.x, w: box.h, h: box.w} : {x: box.x, y: box.y, w: box.w, h: box.h}; }
    vertices.forEach(function (node) { boxes.set(node.id, Object.assign({id: node.id, cluster: node.cluster, rank: node.rank}, rectangle(node))); });
    function hits(a, b, box, padding) {
      var lo = [box.x - padding, box.y - padding], hi = [box.x + box.w + padding, box.y + box.h + padding], p = [a.x, a.y], d = [b.x - a.x, b.y - a.y], enter = 0, leave = 1;
      for (var axis = 0; axis < 2; axis++) {
        if (Math.abs(d[axis]) < 1e-9) { if (p[axis] <= lo[axis] || p[axis] >= hi[axis]) return false; continue; }
        var t1 = (lo[axis] - p[axis]) / d[axis], t2 = (hi[axis] - p[axis]) / d[axis];
        enter = Math.max(enter, Math.min(t1, t2)); leave = Math.min(leave, Math.max(t1, t2)); if (enter >= leave - 1e-9) return false;
      }
      return leave > 1e-7 && enter < 1 - 1e-7;
    }
    function simplify(points) {
      var result = [];
      points.forEach(function (at) {
        var previous = result[result.length - 1]; if (previous && Math.hypot(at.x - previous.x, at.y - previous.y) < 1e-7) return;
        while (result.length > 1) {
          var a = result[result.length - 2], b = result[result.length - 1];
          if (Math.abs((b.x - a.x) * (at.y - b.y) - (b.y - a.y) * (at.x - b.x)) > 1e-7 || (b.x - a.x) * (at.x - b.x) + (b.y - a.y) * (at.y - b.y) < 0) break;
          result.pop();
        }
        result.push({x: at.x, y: at.y});
      }); return result;
    }
    function section(points, clearCorners, island) {
      points = simplify(points);
      var radii = points.map(function (at, index) {
        if (!index || index === points.length - 1) return 0;
        var a = points[index - 1], b = points[index + 1], da = Math.hypot(at.x - a.x, at.y - a.y), db = Math.hypot(b.x - at.x, b.y - at.y), radius = Math.min(8, da / 3, db / 3);
        var hull = bounds([{x: at.x - (at.x - a.x) * radius / da, y: at.y - (at.y - a.y) * radius / da}, at,
          {x: at.x + (b.x - at.x) * radius / db, y: at.y + (b.y - at.y) * radius / db}]);
        return !clearCorners && vertices.some(function (node) { return node.island === island && overlap(hull, node, 12); }) ? 0 : radius;
      });
      return {points: points.map(point), radii: radii, bounds: rectangle(bounds(points))};
    }
    function clear(points, route) {
      var island = vertices[ids.get(route.source)].island;
      return points.slice(1).every(function (at, index) {
        return vertices.every(function (node) { work.candidates++; return node.island !== island || !hits(points[index], at, node, node.id === route.source || node.id === route.target ? 0 : 20); });
      });
    }
    function port(node, side, offset, alongY) {
      if (alongY) {
        var verticalAt = {x: node.x + node.w / 2 + Math.max(-node.w / 4, Math.min(node.w / 4, offset)), y: side > 0 ? node.y + node.h : node.y};
        return {at: verticalAt, stub: {x: verticalAt.x, y: verticalAt.y + side * 24}};
      }
      var at = {x: side > 0 ? node.x + node.w : node.x, y: node.y + node.h / 2 + Math.max(-node.h / 4, Math.min(node.h / 4, offset))};
      return {at: at, stub: {x: at.x + side * 24, y: at.y}};
    }
    var escapes = new Map(), reversedParts = new WeakMap();
    // Disjoint 20px-inflated rectangles make each boundary wrap clear. Primary
    // progress is monotone, so at most N wraps; each node/side/kind is cached.
    function escape(index, side, offset) {
      var key = index + ":" + side + ":" + offset, saved = escapes.get(key); if (saved) return saved;
      var island = vertices[index].island, extent = islands[island].extent;
      var start = port(vertices[index], side, offset), current = start.stub, points = [start.at, current], limit = side > 0 ? extent.x + extent.w + 64 : extent.x - 64;
      for (var step = 0; step <= islands[island].members.length; step++) {
        var first = null, nearest = Infinity;
        vertices.forEach(function (node) {
          work.escapes++;
          if (node.island !== island) return;
          var wall = side > 0 ? node.x - 20 : node.x + node.w + 20, distance = (wall - current.x) * side;
          if (current.y > node.y - 20 + 1e-7 && current.y < node.y + node.h + 20 - 1e-7 && distance >= -1e-7 && distance < nearest) { first = node; nearest = distance; }
        });
        if (!first) { points.push({x: limit, y: current.y}); saved = section(points, true); escapes.set(key, saved); return saved; }
        var near = side > 0 ? first.x - 20 : first.x + first.w + 20, far = side > 0 ? first.x + first.w + 20 : first.x - 20;
        var y = current.y - first.y + 20 < first.y + first.h + 20 - current.y ? first.y - 20 : first.y + first.h + 20;
        points.push({x: near, y: current.y}, {x: near, y: y}, {x: far, y: y}); current = points[points.length - 1];
      }
      throw new Error("Dependency escape exceeded its node bound");
    }
    function reversed(part) {
      var saved = reversedParts.get(part); if (saved) return saved;
      saved = {points: part.points.slice().reverse(), radii: part.radii.slice().reverse(), bounds: part.bounds}; reversedParts.set(part, saved); return saved;
    }
    var paths = new Map();
    var flowRange = Math.max(4, Math.log2(1 + routes.reduce(function (largest, route) { return Math.max(largest, route.count); }, 0)));
    routes.forEach(function (route) {
      var a = vertices[ids.get(route.source)], b = vertices[ids.get(route.target)], offset = route.kind === "proof" ? 4 : -4;
      var island = islands[a.island], extent = island.extent, sparse = island.links <= island.members.length * 3;
      // Cyclic aggregations have no global left-to-right order. Use the facing
      // boundaries rather than sending an upstream link around the whole map.
      var dx = b.x + b.w / 2 - a.x - a.w / 2, dy = b.y + b.h / 2 - a.y - a.h / 2;
      var alongY = Math.abs(dy) - (a.h + b.h) / 2 > Math.abs(dx) - (a.w + b.w) / 2;
      var side = (alongY ? dy : dx) < 0 ? -1 : 1, source = port(a, side, offset, alongY), target = port(b, -side, offset, alongY);
      // Blend absolute size and this scene's relative flow: large library
      // aggregates must not all saturate to one indistinguishable stroke.
      var flowSize = Math.log2(1 + route.count);
      route.label = null; route.strength = 1.2 + 1.6 * flowSize / (flowSize + 4) + 2 * flowSize / flowRange;
      route.flow = {source: a.cluster, target: b.cluster, returning: a.rank >= b.rank};
      var key = ids.get(route.source) + ":" + ids.get(route.target) + ":" + route.kind, saved = paths.get(key);
      if (saved) { Object.assign(route, saved); return; }
      var primary = alongY ? "y" : "x", secondary = alongY ? "x" : "y", size = alongY ? "w" : "h";
      var middle = (source.stub[primary] + target.stub[primary]) / 2, above = Math.min(a[secondary], b[secondary]) - 32, below = Math.max(a[secondary] + a[size], b[secondary] + b[size]) + 32;
      function localPoint(first, second) { return alongY ? {x: second, y: first} : {x: first, y: second}; }
      var candidates = [
        [source.at, source.stub, localPoint(middle, source.stub[secondary]), localPoint(middle, target.stub[secondary]), target.stub, target.at],
        [source.at, source.stub, localPoint(source.stub[primary], above), localPoint(target.stub[primary], above), target.stub, target.at],
        [source.at, source.stub, localPoint(source.stub[primary], below), localPoint(target.stub[primary], below), target.stub, target.at]
      ];
      function length(points) { return points.slice(1).reduce(function (sum, at, index) { return sum + Math.hypot(at.x - points[index].x, at.y - points[index].y); }, 0); }
      var chosen = sparse && candidates.sort(function (a, b) { return length(a) - length(b); }).find(function (points) { return clear(points, route); });
      if (chosen) { var local = section(chosen, false, a.island); route.points = local.points; route.radii = local.radii; route.bounds = local.bounds; }
      else {
        // The bounded shared obstacle escapes run on the scene's primary axis.
        side = dx < 0 ? -1 : 1; source = port(a, side, offset); target = port(b, -side, offset);
        var outgoing = escape(ids.get(route.source), side, offset), incoming = reversed(escape(ids.get(route.target), -side, offset));
        var from = outgoing.points[outgoing.points.length - 1], to = incoming.points[0], lane = route.flow.returning ? extent.y + extent.h + 64 + offset : extent.y - 64 + offset;
        var internalFrom = vertical ? {x: from.y, y: from.x} : from, internalTo = vertical ? {x: to.y, y: to.x} : to;
        var trunk = section([internalFrom, {x: internalFrom.x, y: lane}, {x: internalTo.x, y: lane}, internalTo], true);
        route.parts = [outgoing, trunk, incoming]; route.detoured = true;
        route.bounds = bounds(route.parts.reduce(function (all, part) { return all.concat([{x: part.bounds.x, y: part.bounds.y}, {x: part.bounds.x + part.bounds.w, y: part.bounds.y + part.bounds.h}]); }, []));
      }
      var tip = point(target.at), before = point(target.stub); route.arrow = {x: tip.x, y: tip.y, dx: (tip.x - before.x) / 24, dy: (tip.y - before.y) / 24};
      saved = {bounds: route.bounds, arrow: route.arrow};
      if (route.parts) { saved.parts = route.parts; saved.detoured = true; }
      else { saved.points = route.points; saved.radii = route.radii; }
      paths.set(key, saved);
    });
    if (islands.length > 1) {
      // Only genuinely unlinked components are packed. Their complete route
      // envelopes, not individual ranks/cards, determine the separated shelves.
      islands.forEach(function (island) {
        island.points = island.members.reduce(function (all, index) { var box = boxes.get(vertices[index].id); return all.concat([{x: box.x, y: box.y}, {x: box.x + box.w, y: box.y + box.h}]); }, []);
      });
      routes.forEach(function (route) {
        var points = islands[vertices[ids.get(route.source)].island].points;
        points.push({x: route.bounds.x, y: route.bounds.y}, {x: route.bounds.x + route.bounds.w, y: route.bounds.y + route.bounds.h});
      });
      var spacing = 96, area = 0, widest = 0;
      islands.forEach(function (island) {
        island.packing = bounds(island.points); area += (island.packing.w + spacing) * (island.packing.h + spacing); widest = Math.max(widest, island.packing.w);
      });
      var aspect = Number.isFinite(stageAspect) && stageAspect > 0 ? Math.max(.25, Math.min(4, stageAspect)) : 1.4;
      var width = Math.max(widest, Math.sqrt(area * aspect)), x = 0, y = 0, rowHeight = 0;
      islands.slice().sort(function (a, b) { return b.packing.h - a.packing.h || b.packing.w - a.packing.w || (a.key < b.key ? -1 : 1); }).forEach(function (island) {
        if (x && x + island.packing.w > width) { x = 0; y += rowHeight + spacing; rowHeight = 0; }
        island.shift = {x: x - island.packing.x, y: y - island.packing.y};
        x += island.packing.w + spacing; rowHeight = Math.max(rowHeight, island.packing.h);
      });
      if (wiki.some(function (link) { return vertices[link.a].island !== vertices[link.b].island; })) {
        // Relax complete formal route envelopes, never individual proof cards.
        // Families follow actual weak wiki connectivity and receive no invented labels.
        var families = new Map(), islandSprings = new Map();
        islands.forEach(function (island, index) {
          var key = familyRoot(index), family = families.get(key);
          if (!family) { family = {key: island.key, islands: [], springs: []}; families.set(key, family); }
          family.islands.push({index: index, key: island.key, w: island.packing.w, h: island.packing.h, x: 0, y: 0, degree: 0});
        });
        wiki.forEach(function (link) {
          var a = vertices[link.a].island, b = vertices[link.b].island; if (a === b) return;
          var key = Math.min(a, b) + ":" + Math.max(a, b), found = islandSprings.get(key);
          if (found) found.count = Math.min(Number.MAX_SAFE_INTEGER, found.count + link.count);
          else islandSprings.set(key, {a: Math.min(a, b), b: Math.max(a, b), count: link.count});
        });
        islandSprings.forEach(function (link) { families.get(familyRoot(link.a)).springs.push(link); });
        families.forEach(function (family) {
          var members = family.islands, lookup = new Map(members.map(function (member) { return [member.index, member]; }));
          family.springs.sort(function (a, b) { return a.a - b.a || a.b - b.b; });
          family.springs.forEach(function (link) { lookup.get(link.a).degree++; lookup.get(link.b).degree++; });
          members.sort(function (a, b) { return b.degree - a.degree || (a.key < b.key ? -1 : 1); });
          var unit = members.reduce(function (largest, member) { return Math.max(largest, member.w, member.h); }, 0) + spacing;
          var placed = [], position = 0;
          members.forEach(function (member) {
            if (members.length === 2) {
              member.x = placed.length ? placed[0].w + spacing : 0;
              member.y = placed.length ? (placed[0].h - member.h) / 2 : 0;
              placed.push(member); return;
            }
            var clear = false;
            for (var attempt = 0; attempt < 32 && !clear; attempt++) {
              var radius = unit * Math.sqrt(position) * .85, angle = position++ * 2.399963229728653;
              member.x = Math.cos(angle) * radius - member.w / 2; member.y = Math.sin(angle) * radius - member.h / 2;
              clear = !placed.some(function (other) { return overlap(member, other, spacing); });
            }
            if (!clear) member.x = Math.max.apply(null, placed.map(function (other) { return other.x + other.w; })) + spacing;
            placed.push(member);
          });
          if (members.length > 1) for (var tick = 0; tick < 32; tick++) {
            var forces = new Map(members.map(function (member) { return [member.index, {x: 0, y: 0}]; }));
            family.springs.forEach(function (link) {
              var a = lookup.get(link.a), b = lookup.get(link.b), dx = b.x + b.w / 2 - a.x - a.w / 2, dy = b.y + b.h / 2 - a.y - a.h / 2;
              var distance = Math.max(1, Math.hypot(dx, dy));
              var wanted = (Math.hypot(a.w, a.h) + Math.hypot(b.w, b.h)) / 2 + spacing;
              var strength = Math.min(2, 1 + Math.log1p(link.count) / 12) * (distance - wanted) / distance * .12;
              forces.get(link.a).x += dx * strength / Math.max(1, a.degree); forces.get(link.a).y += dy * strength / Math.max(1, a.degree);
              forces.get(link.b).x -= dx * strength / Math.max(1, b.degree); forces.get(link.b).y -= dy * strength / Math.max(1, b.degree);
            });
            members.forEach(function (a, index) { members.slice(index + 1).forEach(function (b) {
              wikiWork.pairs++;
              var dx = b.x + b.w / 2 - a.x - a.w / 2, dy = b.y + b.h / 2 - a.y - a.h / 2, square = Math.max(1, dx * dx + dy * dy);
              var push = Math.min(3, 16000 / square), length = Math.sqrt(square);
              forces.get(a.index).x -= dx / length * push; forces.get(a.index).y -= dy / length * push;
              forces.get(b.index).x += dx / length * push; forces.get(b.index).y += dy / length * push;
            }); });
            members.forEach(function (member) {
              var force = forces.get(member.index), dx = Math.max(-20, Math.min(20, force.x)), dy = Math.max(-20, Math.min(20, force.y));
              [[dx, dy], [0, dy], [dx, 0]].some(function (move) {
                var candidate = {x: member.x + move[0], y: member.y + move[1], w: member.w, h: member.h};
                var clear = members.every(function (other) { wikiWork.moves++; return other === member || !overlap(candidate, other, spacing); });
                if (clear) { member.x = candidate.x; member.y = candidate.y; } return clear;
              });
            });
            wikiWork.ticks++;
          }
          family.bounds = bounds(members.reduce(function (all, member) { return all.concat([{x: member.x, y: member.y}, {x: member.x + member.w, y: member.y + member.h}]); }, []));
        });
        var familyArea = 0, familyWidth = 0;
        families.forEach(function (family) { familyArea += (family.bounds.w + spacing * 2) * (family.bounds.h + spacing * 2); familyWidth = Math.max(familyWidth, family.bounds.w); });
        var familyGap = spacing * 2 + 64;
        var targetWidth = Math.max(familyWidth, Math.sqrt(familyArea * aspect)), fx = 0, fy = 0, shelf = 0;
        Array.from(families.values()).sort(function (a, b) { return b.islands.length - a.islands.length || b.bounds.h - a.bounds.h || (a.key < b.key ? -1 : 1); }).forEach(function (family) {
          if (fx && fx + family.bounds.w > targetWidth) { fx = 0; fy += shelf + familyGap; shelf = 0; }
          family.islands.forEach(function (member) {
            var island = islands[member.index];
            island.shift = {x: fx + member.x - family.bounds.x - island.packing.x, y: fy + member.y - family.bounds.y - island.packing.y};
          });
          wikiFamilies.push({island_count: family.islands.length, members: family.islands.reduce(function (all, member) { return all.concat(islands[member.index].members.map(function (index) { return vertices[index].id; })); }, []).sort(),
            bounds: {x: fx, y: fy, w: family.bounds.w, h: family.bounds.h}});
          fx += family.bounds.w + familyGap; shelf = Math.max(shelf, family.bounds.h);
        });
      }
      // Cached escapes/reversed paths share point, bound and arrow objects.
      // Translate each object once so packing cannot distort a typed route.
      var translated = new WeakSet();
      function translate(at, shift) { if (!translated.has(at)) { at.x += shift.x; at.y += shift.y; translated.add(at); } }
      vertices.forEach(function (node) { translate(boxes.get(node.id), islands[node.island].shift); });
      routes.forEach(function (route) {
        var shift = islands[vertices[ids.get(route.source)].island].shift;
        if (route.parts) route.parts.forEach(function (part) { part.points.forEach(function (at) { translate(at, shift); }); translate(part.bounds, shift); });
        else route.points.forEach(function (at) { translate(at, shift); });
        translate(route.bounds, shift); translate(route.arrow, shift);
      });
      // Translation can round (card.x + card.w) differently from its port.
      // Re-anchor only the endpoint's normal coordinate to the actual boundary.
      function pinPort(at, box, dx, dy) {
        if (Math.abs(dx) > Math.abs(dy)) at.x = dx > 0 ? box.x + box.w : box.x;
        else at.y = dy > 0 ? box.y + box.h : box.y;
      }
      routes.forEach(function (route) {
        var first = route.parts ? route.parts[0].points : route.points, last = route.parts ? route.parts[route.parts.length - 1].points : route.points;
        var source = first[0], next = first[1], target = last[last.length - 1];
        pinPort(source, boxes.get(route.source), next.x - source.x, next.y - source.y);
        pinPort(target, boxes.get(route.target), -route.arrow.dx, -route.arrow.dy);
        route.arrow.x = target.x; route.arrow.y = target.y;
      });
    }
    var all = Array.from(boxes.values()).reduce(function (points, box) { return points.concat([{x: box.x, y: box.y}, {x: box.x + box.w, y: box.y + box.h}]); }, []);
    routes.forEach(function (route) { all.push({x: route.bounds.x, y: route.bounds.y}, {x: route.bounds.x + route.bounds.w, y: route.bounds.y + route.bounds.h}); });
    return {nodes: boxes, routes: routes, clusters: components.map(function (component) { return {id: component.key, rank: component.rank, members: component.members.map(function (index) { return vertices[index].id; })}; }),
      $wikiFamilies: wikiFamilies, $wikiEdges: wiki.map(function (link) { return {source: vertices[link.a].id, target: vertices[link.b].id, count: link.count}; }),
      $wikiWork: wikiWork, work: work, bounds: bounds(all)};
  }

  // Soft article associations use settled geometry, never the proof's ports or
  // rank solver. Keep a bounded local backbone; all links remain in the reader.
  function wikiRoutes(scene) {
    var boxes = scene.nodes, ordered = Array.from(boxes.values()), links = scene.$wikiEdges || [], paths = [], hidden = 0;
    var work = {attempts: 0, pairs: 0, obstacles: 0};
    var counts = new Map();
    function hits(a, b, box, padding) {
      var enter = 0, leave = 1;
      for (var axis of ["x", "y"]) {
        var low = box[axis] - padding, high = box[axis] + box[axis === "x" ? "w" : "h"] + padding;
        var delta = b[axis] - a[axis];
        if (Math.abs(delta) < 1e-9) { if (a[axis] <= low || a[axis] >= high) return false; continue; }
        var first = (low - a[axis]) / delta, last = (high - a[axis]) / delta;
        enter = Math.max(enter, Math.min(first, last)); leave = Math.min(leave, Math.max(first, last));
        if (enter >= leave - 1e-9) return false;
      }
      return leave > 1e-7 && enter < 1 - 1e-7;
    }
    function clear(a, b, source, target) { work.pairs++; return ordered.every(function (box) { work.obstacles++; return !hits(a, b, box, box.id === source || box.id === target ? 0 : 16); }); }
    function boundary(box, other) {
      var x = box.x + box.w / 2, y = box.y + box.h / 2, dx = other.x + other.w / 2 - x, dy = other.y + other.h / 2 - y;
      var scale = Math.min(Math.abs(dx) > 1e-9 ? box.w / 2 / Math.abs(dx) : Infinity, Math.abs(dy) > 1e-9 ? box.h / 2 / Math.abs(dy) : Infinity);
      var at = {x: x + dx * scale, y: y + dy * scale};
      // Pin the normal coordinate rather than accumulating floating-point
      // error just inside the card, which would make a real stroke clip it.
      if (Math.abs(dx) / box.w >= Math.abs(dy) / box.h) at.x = dx < 0 ? box.x : box.x + box.w;
      else at.y = dy < 0 ? box.y : box.y + box.h;
      return at;
    }
    links.slice().sort(function (a, b) { return b.count - a.count || (a.source < b.source ? -1 : a.source > b.source ? 1 : a.target < b.target ? -1 : 1); }).forEach(function (link) {
      if ((counts.get(link.source) || 0) >= 4 || (counts.get(link.target) || 0) >= 4) { hidden++; return; }
      if (work.attempts >= ordered.length * 2) { hidden++; return; }
      work.attempts++;
      var source = boxes.get(link.source), target = boxes.get(link.target); if (!source || !target) return;
      var from = boundary(source, target), to = boundary(target, source), direct = Math.hypot(to.x - from.x, to.y - from.y), route;
      if (clear(from, to, source.id, target.id)) route = [from, to];
      else {
        var points = [from, to], margin = Math.min(320, direct / 2 + 48);
        var nearby = ordered.filter(function (box) { return box.id !== source.id && box.id !== target.id &&
          box.x <= Math.max(from.x, to.x) + margin && box.x + box.w >= Math.min(from.x, to.x) - margin &&
          box.y <= Math.max(from.y, to.y) + margin && box.y + box.h >= Math.min(from.y, to.y) - margin; });
        if (nearby.length > 8) { hidden++; return; }
        nearby.forEach(function (box) {
          points.push({x: box.x - 20, y: box.y - 20}, {x: box.x + box.w + 20, y: box.y - 20},
            {x: box.x - 20, y: box.y + box.h + 20}, {x: box.x + box.w + 20, y: box.y + box.h + 20});
        });
        var distance = points.map(function () { return Infinity; }), before = [], visited = new Set(); distance[0] = 0;
        // Dijkstra over at most 32 nearby corners. Only local segments are candidates,
        // preventing an optional association from circling the entire library.
        for (var step = 0; step < points.length; step++) {
          var next = -1;
          for (var i = 0; i < points.length; i++) if (!visited.has(i) && (next < 0 || distance[i] < distance[next])) next = i;
          if (next < 0 || !Number.isFinite(distance[next]) || distance[next] > direct * 1.8 + 96) break;
          if (next === 1) { route = []; for (var at = 1; at !== undefined; at = before[at]) route.unshift(points[at]); break; }
          visited.add(next);
          for (var candidate = 0; candidate < points.length; candidate++) {
            if (visited.has(candidate)) continue;
            var length = Math.hypot(points[candidate].x - points[next].x, points[candidate].y - points[next].y);
            if (distance[next] + length >= distance[candidate] || distance[next] + length > direct * 1.8 + 96) continue;
            if (clear(points[next], points[candidate], source.id, target.id)) { distance[candidate] = distance[next] + length; before[candidate] = next; }
          }
        }
      }
      if (!route) { hidden++; return; }
      counts.set(source.id, (counts.get(source.id) || 0) + 1); counts.set(target.id, (counts.get(target.id) || 0) + 1);
      var left = Math.min.apply(null, route.map(function (at) { return at.x; })), top = Math.min.apply(null, route.map(function (at) { return at.y; }));
      paths.push({source: source.id, target: target.id, kind: "wiki", count: link.count, points: route, radii: route.map(function () { return 0; }),
        strength: 1 + Math.min(2, Math.log2(1 + link.count) / 3), bounds: {x: left, y: top,
          w: Math.max.apply(null, route.map(function (at) { return at.x; })) - left, h: Math.max.apply(null, route.map(function (at) { return at.y; })) - top}});
    });
    return {routes: paths, omitted: hidden, total: links.length, work: work};
  }

  // A derived overview of a large local scope, not a new mathematical graph.
  // Areas are authored; unassigned topics remain an explicitly structural bucket.
  function scopeOverviewProjection(payload, selectedGroup, reservedIds) {
    var local = payload.nodes.filter(function (node) { return node.kind !== "boundary"; });
    if (local.length <= 40 || payload.nodes.length > MAX_MAP_NODES) return null;
    var areas = new Set(local.map(function (node) { return node.area; }).filter(function (area) { return typeof area === "string" && area.trim(); }));
    if (areas.size < 2) return null;
    var groups = new Map(), owners = new Map(), used = new Set(reservedIds || []);
    payload.nodes.forEach(function (node) { used.add(node.id); });
    local.forEach(function (node) {
      var assigned = typeof node.area === "string" && node.area.trim(), key = assigned ? "area:" + node.area : "unassigned";
      var group = groups.get(key);
      if (!group) { group = {key: key, area: assigned ? node.area : null, members: [], edges: []}; groups.set(key, group); }
      group.members.push(node); owners.set(node.id, group);
    });
    var serial = 0;
    groups.forEach(function (group) {
      do { group.id = "view-group:" + serial++; } while (used.has(group.id));
      used.add(group.id);
    });
    if (!groups.has(selectedGroup)) selectedGroup = null;
    var projected = [], endpoints = new Map(), bundles = new Map();
    groups.forEach(function (group) {
      if (group.key === selectedGroup) {
        group.members.forEach(function (node) { projected.push(node); endpoints.set(node.id, node.id); });
        return;
      }
      var counts = new Map(), unknown = false, items = 0, internal = 0;
      group.members.forEach(function (node) {
        endpoints.set(node.id, group.id);
        (node.status_counts || []).forEach(function (entry) { counts.set(entry[0], (counts.get(entry[0]) || 0) + entry[1]); });
        if (node.member_count === undefined) unknown = true; else items += node.member_count;
        internal += node.internal_dependency_count || 0;
      });
      projected.push({id: group.id, kind: "scope", title: group.area || "Other topics", area: group.area || undefined,
        _region: group.area || undefined, _overviewGroup: group.key, _topics: group.members.length,
        _unknownCount: unknown, member_count: items, status_counts: Array.from(counts.entries()),
        status: group.area ? group.members[0].status : "inventory_pending", internal_dependency_count: internal,
        summary: group.members.slice(0, 3).map(function (node) { return node.title; }).join(" · "),
        layout: {width: 264, height: 148}});
    });
    payload.nodes.forEach(function (node) {
      if (node.kind === "boundary") { projected.push(node); endpoints.set(node.id, node.id); }
    });
    payload.edges.forEach(function (edge) {
      var source = endpoints.get(edge.source), target = endpoints.get(edge.target);
      if (!source || !target) throw new Error("Unknown scoped overview endpoint");
      if (source === target && source !== edge.source) {
        owners.get(edge.source).edges.push(edge); return;
      }
      var key = JSON.stringify([source, target]), bundle = bundles.get(key);
      if (!bundle) { bundle = {source: source, target: target, statement: 0, proof: 0, _overviewEdges: []}; bundles.set(key, bundle); }
      bundle.statement += edge.statement || 0; bundle.proof += edge.proof || 0; bundle._overviewEdges.push(edge);
    });
    groups.forEach(function (group) {
      var node = projected.find(function (candidate) { return candidate.id === group.id; });
      if (node) {
        node._overviewEdges = group.edges;
        node.internal_dependency_count += group.edges.reduce(function (sum, edge) { return sum + (edge.statement || 0) + (edge.proof || 0); }, 0);
      }
    });
    return Object.assign({}, payload, {nodes: projected, edges: Array.from(bundles.values()), regions: [],
      view: Object.assign({}, payload.view, {presentation: "atlas"}),
      _scopeOverview: {groups: Array.from(groups.values()), selectedGroup: selectedGroup, originalNodes: payload.nodes.length}});
  }

  function scopeOverviewFlow(route, counts, flowRange) {
    if (!counts) return route;
    var count = counts[route.kind];
    if (!count) return null;
    if (count === route.count) return route;
    var size = Math.log2(1 + count);
    return Object.assign({}, route, {count: count, strength: 1.2 + 1.6 * size / (size + 4) + 2 * size / flowRange});
  }

  function wikiOwnerVisible(node, statuses) {
    return Boolean(node && (!node.status_counts || node.status_counts.every(function (pair) { return !pair[1] || statuses.has(pair[0]); })));
  }

  function mount(host, data, instance, firstBundle, navigation) {
    if (!navigation || !firstBundle || !firstBundle.readingSource) throw new Error("A complete mathematical page and navigation are required");
    if (!data || data.schema !== "autoform-dag-view/v2" || !data.palette ||
        !Array.isArray(data.nodes) || !Array.isArray(data.edges)) throw new Error("unsupported graph payload");
    var contextGraph = data, sharedIndex = null, records = new Map(), currentBundle = firstBundle;
    var ui$wikiLinks = null, ui$wikiBefore = new Map(), ui$wikiAfter = new Map(), ui$wikiUnavailable = new Set(), ui$wikiRequested = false;
    var ui$wikiGeneration = 0, ui$wikiPending = null, ui$wikiWork = null, ui$wikiRendered = null, ui$wikiNode = null;
    var ui$wikiView = null, ui$wikiProjectionTicket = 0, ui$wikiProjectionCache = new WeakMap();
    var alive = true, cleanup = [], observers = [], frame = 0, readingReady = Promise.resolve(), restoration = 0, checkpointTimer = 0, restoring = false;
    host.textContent = "";
    host.classList.remove("bp-dag-enhancing");
    var view, atlas, palette, byId, prerequisites, dependents, statusCounts, presentStatuses;
    var mapAllowed, scene, sceneData, sceneDirection, sceneCompact, sceneFamily, sceneAspect, sceneWiki;
    function recordId(node) {
      if (node._overviewGroup) return null;
      return node._recordId || (records.get(node.id) === node || ["scope", "boundary"].indexOf(node.kind) < 0 ? node.id : node.id.replace(/^(scope|boundary):/, ""));
    }
    function mapNode(node) {
      var record = records.get(recordId(node));
      return node.map !== false && !(node.kind === "scope" && record && record.navigation === "source");
    }
    function adoptGraph(payload) {
    data = graphAt(payload, currentBundle && currentBundle.dataURL);
    var shown = data.nodes.filter(mapNode), ids = new Set(shown.map(function (n) { return n.id; }));
    data.nodes = shown; data.edges = data.edges.filter(function (e) { return ids.has(e.source) && ids.has(e.target); });
    view = data.view || {}; atlas = view.presentation === "atlas"; host.classList.toggle("bp-dag-atlas", atlas);
    palette = data.palette || {}; byId = new Map(); prerequisites = new Map(); dependents = new Map();
    statusCounts = new Map(); presentStatuses = [];
    data.nodes.forEach(function (node, index) {
      var layout = node.layout || {};
      node._index = index;
      node.width = Number.isFinite(layout.width) ? layout.width : NODE_W;
      node.height = Math.max(100, Number.isFinite(layout.height) ? layout.height : NODE_H);
      node.isolated = node.isolated === true;
      byId.set(node.id, node);
      prerequisites.set(node.id, []); dependents.set(node.id, []);
      statusCounts.set(node.status, (statusCounts.get(node.status) || 0) + 1);
    });
    data.edges.forEach(function (edge) {
      if (prerequisites.has(edge.target) && byId.has(edge.source)) prerequisites.get(edge.target).push(edge.source);
      if (dependents.has(edge.source) && byId.has(edge.target)) dependents.get(edge.source).push(edge.target);
    });
    data.nodes.forEach(function (node) {
      node.isolated = node.isolated || ((prerequisites.get(node.id) || []).length + (dependents.get(node.id) || []).length === 0);
    });
    if (sharedIndex) {
      statusCounts = new Map();
      sharedIndex.nodes.forEach(function (node) { statusCounts.set(node.status, (statusCounts.get(node.status) || 0) + 1); });
    }
    (data.present_statuses || Object.keys(palette)).forEach(function (key) {
      if (statusCounts.has(key) && presentStatuses.indexOf(key) < 0) presentStatuses.push(key);
    });
    statusCounts.forEach(function (_count, key) {
      if (presentStatuses.indexOf(key) < 0) presentStatuses.push(key);
    });

    mapAllowed = data.nodes.length <= MAX_MAP_NODES;
    }
    adoptGraph(data);
    // Renderer-private scene locals compress independently of public JSON/history field names.
    var ui$active = [], ui$activeDirty = true, ui$activeIds = new Set(), ui$direction = "both", ui$listLimit = LIST_PAGE,
      ui$mode = "graph", ui$moved = false, ui$orientation = "lr", ui$pointers = new Map(), ui$pinch = null,
      ui$query = "", ui$searchLimit = 12, ui$selected = null, ui$searchCache = [], ui$searchCacheQuery = null,
      ui$searchError = null, ui$searchLoading = false, ui$searchNodes = data.nodes, ui$searchPromise = null,
      ui$spotlight = null, ui$mapAllowed = mapAllowed, ui$mathQueue = [], ui$mathBusy = false, ui$mathObserver = null,
      ui$selectedStatuses = new Set(presentStatuses), ui$showIsolated = true,
      ui$transform = {x: 20, y: 20, scale: 1}, ui$reading = Boolean(firstBundle && firstBundle.readingSource),
      ui$area = null, ui$overviewGroup = null, ui$scopeOverview = false, ui$overviewOnly = false, ui$filtersTouched = false, ui$relationIds = null,
      ui$hovered = null,
      ui$requestedNode = firstBundle && firstBundle.nodeId || null, ui$outlineLimit = 120;
    if (!mapAllowed) ui$mode = "list";
    var dom$announcement, dom$body, dom$breadcrumb, dom$browse, dom$browseToggle, dom$browsePopover, dom$canvas, dom$ctx, dom$density, dom$edgeFieldset, dom$empty,
      dom$error, dom$filterPopover, dom$filterSummary, dom$filters, dom$fit, dom$graphMode, dom$hiddenButton,
      dom$homeLink, dom$inspector, dom$inspectorInner, dom$inventory, dom$inventoryList, dom$inventoryRows,
      dom$inventoryScroll, dom$inventorySummary, dom$isolated, dom$listMode, dom$loadMore, dom$mapContext, dom$mapKey,
      dom$navigation, dom$navigationInner, dom$navigationNodes, dom$nodeLayer, dom$outlineToggle, dom$pageHeading,
      dom$readingBody, dom$readingContext, dom$readingNeighbors, dom$search, dom$searchResults,
      dom$searchWrap, dom$sheetHandle, dom$sourcesMode, dom$stage, dom$statusFieldset, dom$statusInputs,
      dom$wholeSearch, dom$zoomIn, dom$zoomOut;
    var overlays = new Map(), scheduled = false, firstResize = true, family, hoverTimer;
    host.classList.add("bp-dag-integrated");

    buildShell();
    bindEvents();
    // The router has already validated its index. Project before the first
    // layout, including direct theorem entries, not a transient raw scope.
    if (navigation && navigation.getIndex) useIndex(navigation.getIndex());
    resize();
    readingReady = applyRoute(firstBundle);

    function globalListen(target, type, handler, options) {
      target.addEventListener(type, handler, options); cleanup.push(function () { target.removeEventListener(type, handler, options); });
    }
    function remember() {
      if (restoring) return;
      clearTimeout(checkpointTimer); checkpointTimer = setTimeout(function () { if (alive && !restoring) navigation.checkpoint(); }, 80);
    }

    function captureState() {
      var active = document.activeElement, key = active === dom$search ? "search" : dom$readingBody && dom$readingBody.contains(active) ? "reading" : active && (active.dataset.autoformNodeId || active.dataset.inventoryNodeId);
      return {node: ui$selected && ui$selected.id, requestedNode: ui$requestedNode, mode: ui$mode, direction: ui$direction,
        statuses: Array.from(ui$selectedStatuses), isolates: ui$showIsolated, camera: Object.assign({}, ui$transform),
        query: ui$query, area: ui$area, overviewGroup: ui$overviewGroup, readingOpen: host.classList.contains("bp-dag-sheet-open"), focus: key,
        readingScroll: dom$readingBody ? dom$readingBody.scrollTop : 0, outlineScroll: dom$navigation.scrollTop,
        inventoryScroll: dom$inventoryScroll.scrollTop, listLimit: ui$listLimit, relationIds: ui$relationIds,
        outlineOpen: host.classList.contains("bp-dag-outline-open"), outlineLimit: ui$outlineLimit};
    }
    function snapshotForRoute(saved, bundle) {
      if (!saved || !bundle || !bundle.url) return saved;
      var id = saved.requestedNode === undefined ? saved.node : saved.requestedNode, record = records.get(id);
      if (record && record.route) {
        var expected = new URL(record.route, bundle.url), actual = new URL(bundle.url);
        var destination = records.get(bundle.nodeId);
        if (destination && destination.id !== record.id && destination.route &&
            new URL(destination.route, bundle.url).href === actual.href) return null;
        expected.hash = ""; actual.hash = "";
        if (expected.href !== actual.href) return null;
      }
      return saved;
    }
    function restoreState(saved, bundle) {
      saved = snapshotForRoute(saved, bundle);
      if (!saved) return;
      var ticket = ++restoration, isCurrent = bundle && bundle.isCurrent; restoring = true; clearTimeout(checkpointTimer);
      ui$filtersTouched = true; ui$area = saved.area || null; ui$overviewGroup = saved.overviewGroup || null;
      ui$requestedNode = saved.requestedNode === undefined ? saved.node || null : saved.requestedNode;
      updateProjection(); ui$selected = saved.node && byId.get(saved.node) || null;
      ui$selectedStatuses = new Set(saved.statuses || presentStatuses); ui$showIsolated = saved.isolates !== false;
      ui$mode = ["graph", "list", "sources", "regions"].indexOf(saved.mode) >= 0 ? saved.mode : "graph";
      if (ui$mode === "graph" && !mapAllowed) ui$mode = "list";
      ui$listLimit = Math.max(LIST_PAGE, Number(saved.listLimit) || LIST_PAGE); ui$relationIds = saved.relationIds || null;
      ui$outlineLimit = Math.max(120, Number(saved.outlineLimit) || 120); renderNavigation();
      ui$direction = saved.direction || "both"; ui$query = saved.query || ""; dom$search.value = ui$query;
      host.classList.toggle("bp-dag-outline-open", Boolean(saved.outlineOpen));
      if (dom$outlineToggle) dom$outlineToggle.setAttribute("aria-expanded", String(Boolean(saved.outlineOpen)));
      ui$activeDirty = true; setSheet(saved.readingOpen !== false && 1, ui$selected); refresh(false); resize();
      if (saved.camera) ui$transform = Object.assign({}, saved.camera);
      return readingReady.catch(function () {}).then(function () { return new Promise(function (resolve) { requestAnimationFrame(function () {
        if (!alive || ticket !== restoration || (isCurrent && !isCurrent())) { if (ticket === restoration) restoring = false; resolve(); return; }
        dom$navigation.scrollTop = saved.outlineScroll || 0;
        if (dom$readingBody) dom$readingBody.scrollTop = saved.readingScroll || 0;
        dom$inventoryScroll.scrollTop = saved.inventoryScroll || 0;
        var chosen = saved.focus === "search" ? dom$search : saved.focus === "reading" ? dom$readingBody :
          ui$mode === "graph" ? overlays.get(saved.focus) : dom$inventoryRows.get(saved.focus);
        if (chosen === dom$readingBody) chosen.tabIndex = -1;
        if (chosen) chosen.focus({preventScroll: true});
        restoring = false; resolve();
      }); }); });
    }
    function showError(error) {
      dom$announcement.textContent = "The requested view could not be loaded. " + (error.message || error);
      dom$error.textContent = "Could not open this view. " + (error.message || error);
      var href = safeHref(error.url);
      if (href) {
        dom$error.appendChild(document.createTextNode(" "));
        var fallback = element("a", "", "Open complete article in a new tab"); fallback.href = href;
        fallback.target = "_blank"; fallback.rel = "noopener";
        dom$error.appendChild(fallback);
      }
      dom$error.hidden = false;
    }
    function useIndex(index) {
      if (!index || sharedIndex === index) return;
      sharedIndex = index; records = new Map(); dom$search.removeAttribute("aria-busy");
      index.nodes.forEach(function (node, i) { node._index = i; records.set(node.id, node); });
      ui$searchNodes = index.nodes; ui$searchCacheQuery = null;
      if (currentBundle) updateProjection();
      renderNavigation(); renderReaderContext(); renderInventory(false); refresh(false);
      if (ui$requestedNode) { fit(); centerIfHidden(ui$selected); }
      requestWikiLinks();
    }
    function requestWikiLinks() {
      if (!alive || !sharedIndex || ui$wikiRequested) return;
      ui$wikiRequested = true;
      function unavailable() { return useWikiLinks({availability: "unavailable", references: [], unavailable_sources: []}); }
      try {
        if (typeof navigation.getWikiLinks === "function") useWikiLinks(navigation.getWikiLinks());
        if (typeof navigation.loadWikiLinks !== "function") { unavailable(); return; }
        Promise.resolve(navigation.loadWikiLinks()).then(function (loaded) { return loaded ? useWikiLinks(loaded) : null; }).catch(unavailable);
      } catch (_error) { unavailable(); }
    }
    function useWikiLinks(loaded) {
      if (!alive || !loaded) return;
      if (ui$wikiPending === loaded) return ui$wikiWork;
      var ticket = ++ui$wikiGeneration;
      if (ui$wikiLinks === loaded) { ui$wikiPending = null; ui$wikiWork = null; return; }
      ui$wikiPending = loaded;
      var before = new Map(), after = new Map(), unavailable = new Set(), ui$wikiPosition = 0;
      var references = ["complete", "partial"].indexOf(loaded.availability) >= 0 ? loaded.references || [] : [];
      var sources = loaded.unavailable_sources || [], total = references.length + sources.length;
      function build() {
        if (!alive || ticket !== ui$wikiGeneration) return;
        var end = Math.min(total, ui$wikiPosition + 1024);
        for (; ui$wikiPosition < end; ui$wikiPosition++) {
          if (ui$wikiPosition >= references.length) {
            var id = sources[ui$wikiPosition - references.length]; if (records.has(id)) unavailable.add(id); continue;
          }
          var pair = references[ui$wikiPosition], source = pair[0], target = pair[1];
          if (source === target || !records.has(source) || !records.has(target)) continue;
          if (!after.has(source)) after.set(source, new Set());
          if (!before.has(target)) before.set(target, new Set());
          after.get(source).add(target); before.get(target).add(source);
        }
        if (ui$wikiPosition < total) return new Promise(function (resolve) { setTimeout(resolve, 0); }).then(build);
        if (!alive || ticket !== ui$wikiGeneration) return;
        ui$wikiLinks = loaded; ui$wikiBefore = before; ui$wikiAfter = after; ui$wikiUnavailable = unavailable;
        ui$wikiPending = null; ui$wikiWork = null; renderWikiContext(); projectWikiView();
      }
      ui$wikiWork = build(); return ui$wikiWork;
    }
    function wikiNeighbors(id) {
      var availability = ui$wikiLinks && ui$wikiLinks.availability || "unavailable", known = records.has(id);
      return {before: Array.from(ui$wikiBefore.get(id) || []), after: Array.from(ui$wikiAfter.get(id) || []), availability: availability,
        outgoingAvailable: known && (availability === "complete" || availability === "partial" && !ui$wikiUnavailable.has(id)),
        incomingComplete: known && availability === "complete"};
    }
    function projectWikiView() {
      if (!alive || !sharedIndex || !ui$wikiLinks || !data || !mapAllowed) return;
      var ticket = ++ui$wikiProjectionTicket, captured = data, layer = ui$wikiLinks, index = sharedIndex;
      var cached = ui$wikiProjectionCache.get(contextGraph), key = JSON.stringify([ui$requestedNode, ui$area, ui$overviewGroup]);
      if (!cached || cached.layer !== layer || cached.index !== index) {
        cached = {layer: layer, index: index, views: new Map()}; ui$wikiProjectionCache.set(contextGraph, cached);
      }
      var pending = cached.views.get(key);
      if (!pending) {
        pending = projectWikiLinks(captured, records, layer, captured.$wikiOwners);
        if (cached.views.size >= 12) cached.views.delete(cached.views.keys().next().value);
        cached.views.set(key, pending);
      }
      function publish(projected) {
        if (!alive || ticket !== ui$wikiProjectionTicket || captured !== data || layer !== ui$wikiLinks || index !== sharedIndex) return;
        ui$wikiView = projected;
        var before = data.$wikiEdges || [], after = projected.edges;
        if (JSON.stringify(before) === JSON.stringify(after)) { syncControls(); return; }
        // Keep the selected/keyboard-focus card fixed on screen when optional
        // associations arrive. The mathematical page and its focus stay intact.
        var painting = ui$mode === "graph" && dom$stage.clientWidth && dom$stage.clientHeight;
        var id = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.autoformNodeId;
        var anchor = painting && (byId.get(id) || ui$selected || ui$active.find(function (node) { return inViewport(screen(node), 0); }));
        var box = anchor && scene && scene.nodes.get(anchor.id), previous = box && {x: box.x, y: box.y, w: box.w, h: box.h};
        data.$wikiEdges = after;
        if (painting) geometry();
        var moved = previous && scene && scene.nodes.get(anchor.id);
        if (moved && !restoring && id !== anchor.id) {
          ui$transform.x += (previous.x + previous.w / 2 - moved.x - moved.w / 2) * ui$transform.scale;
          ui$transform.y += (previous.y + previous.h / 2 - moved.y - moved.h / 2) * ui$transform.scale;
        }
        syncControls(); schedule();
      }
      Promise.resolve(pending).then(publish).catch(function () { publish({availability: "unavailable", edges: [], unavailableOwners: []}); });
    }
    function applyRoute(bundle) {
      var snapshot = snapshotForRoute(bundle.snapshot, bundle);
      if (snapshot !== bundle.snapshot) bundle = Object.assign({}, bundle, {snapshot: snapshot});
      var isCurrent = bundle.isCurrent;
      if (!alive || (isCurrent && !isCurrent())) return;
      restoration++; restoring = false; clearTimeout(checkpointTimer); currentBundle = bundle; contextGraph = bundle.graph; ui$area = null; ui$overviewGroup = null; ui$requestedNode = bundle.nodeId || null;
      document.title = bundle.title; dom$error.hidden = true;
      ui$filtersTouched = false; ui$relationIds = null;
      ui$query = ""; dom$search.value = ""; closeSearch(); setFilters(false); setBrowse(false);
      ui$reading = Boolean(bundle.readingSource);
      if (ui$reading && dom$readingBody) {
        dom$readingBody.replaceChildren(bundle.readingSource);
        var oldSource = host.closest(".bp-explorer-page").querySelector(":scope > .bp-reading-source");
        if (oldSource) oldSource.remove();
        if (!dom$readingBody.querySelector("h1")) dom$readingBody.prepend(element("h1", "", bundle.title || bundle.graph.title));
        dom$readingBody.scrollTop = 0;
        readingReady = window.AutoformMath ? Promise.resolve(window.AutoformMath.refresh(dom$readingBody)) : Promise.resolve();
      }
      updateProjection();
      ui$mode = mapAllowed ? "graph" : "list"; ui$direction = "both";
      ui$selectedStatuses = new Set(presentStatuses); ui$showIsolated = true; ui$activeDirty = true;
      var legacy = bundle.parameters || {};
      if (legacy.view === "list") ui$mode = "list";
      if (["prerequisites", "dependents"].indexOf(legacy.direction) >= 0) ui$direction = legacy.direction;
      if (legacy.status) ui$selectedStatuses = new Set(legacy.status.split(",").filter(function (key) { return palette[key]; }));
      if (legacy.isolates === "0") ui$showIsolated = false;
      if (ui$selected) ui$selectedStatuses.add(ui$selected.status);
      // Decide only after the outline has proved and hidden duplicated child
      // indexes. A canonical scope ID alone is not a request to read a result.
      renderNavigation();
      var record = records.get(bundle.nodeId);
      ui$overviewOnly = Boolean(mapAllowed && data.nodes.length && data.nodes.every(function (node) { return node._region; }) &&
        (!bundle.focus || bundle.mapFirst) && !bundle.anchor && !(record && record.result) && navigation.readerIsOverview && navigation.readerIsOverview(dom$readingBody));
      host.classList.toggle("bp-dag-overview-only", ui$overviewOnly);
      var mapRequested = Boolean(bundle.mapFirst && (ui$scopeOverview || ui$overviewOnly) && !bundle.anchor && !(record && record.result));
      var readingRequested = Boolean(bundle.focus && !mapRequested || bundle.anchor || record && record.result);
      setSheet(bundle.snapshot ? bundle.snapshot.readingOpen !== false && 1 : readingRequested ||
        !ui$scopeOverview && !ui$overviewOnly && (!sheetLayout() || Boolean(bundle.nodeId)), ui$selected);
      refresh(true); firstResize = true; resize();
      if (ui$selected && mapAllowed) centerIfHidden(ui$selected);
      if (navigation) navigation.loadIndex().catch(showError);
      readingReady = readingReady.catch(function () {}).then(function () { return new Promise(function (resolve) { requestAnimationFrame(function () {
        if (!alive || (isCurrent && !isCurrent())) { resolve(); return; }
        var target = bundle.anchor && document.getElementById(bundle.anchor);
        var current = target && dom$readingBody.contains(target) ? target : null;
        if (current) {
          target.classList.add("bp-reading-current"); target.setAttribute("aria-current", "true");
          // The statement's own relations list replaces a separate strip, so it opens with the selection.
          var relations = target.querySelector(".bp-dependencies"); if (relations) relations.open = true;
          if (!bundle.snapshot) dom$readingBody.scrollTop += target.getBoundingClientRect().top - dom$readingBody.getBoundingClientRect().top - 24;
        }
        if (bundle.focus && !mapRequested && dom$readingBody && !bundle.snapshot) {
          // Focus lands on the statement the reader chose, which the reader has just scrolled to, not on the page title above it.
          var heading = current && current.querySelector(".bp-thmheading") || current || dom$readingBody.querySelector("h1");
          if (heading) { heading.tabIndex = -1; heading.focus({preventScroll: true}); }
        } else if (bundle.focus && mapRequested && !bundle.snapshot) {
          var control = dom$nodeLayer.querySelector('.bp-dag-node[tabindex="0"]'); if (control) control.focus({preventScroll: true});
        }
        resolve();
      }); }); });
      return readingReady;
    }
    function routeTrail(bundle) {
      var entries = [], record = records.get(bundle.nodeId), pending = records.get(bundle.scope), seen = new Set();
      if (!pending) entries = bundle.graph.view && bundle.graph.view.breadcrumbs || [];
      while (pending && !seen.has(pending.id)) {
        seen.add(pending.id);
        // A collection never stands between crumbs, but its own page ends the trail with it, as the map header does.
        var own = pending.id === bundle.scope && pending.navigation === "collection";
        if (own || ["collection", "transparent"].indexOf(pending.navigation) < 0) entries.unshift({label: pending.title, reading_title: pending.reading_title,
          url: pending.id === bundle.scope && (!record || record.id === pending.id) && !ui$overviewGroup && !ui$area ? null : pending.route || pending.url,
          _overviewParent: pending.id === bundle.scope && Boolean(ui$overviewGroup || ui$area)});
        pending = records.get(semanticParent(pending));
      }
      if (record && record.scope !== record.id) entries.push({label: record.title, reading_title: record.reading_title});
      if (ui$area) entries.push({label: ui$area});
      var group = currentOverviewGroup();
      if (group) entries.push({label: group.area || "Other topics"});
      return entries;
    }
    function currentOverviewGroup() {
      return ui$overviewGroup && data._scopeOverview && data._scopeOverview.groups.find(function (group) { return group.key === ui$overviewGroup; });
    }
    function neighborIds(id) {
      var before = [], after = [];
      ((sharedIndex && sharedIndex.relations) || []).forEach(function (edge) {
        if (edge[1] === id) before.push(edge[0]); if (edge[0] === id) after.push(edge[1]);
      });
      return {before: Array.from(new Set(before)), after: Array.from(new Set(after))};
    }
    function neighborhood(id) {
      var focus = records.get(id); if (!focus || focus.scope === focus.id) return null;
      var links = neighborIds(id), noEdges = !links.before.length && !links.after.length;
      var candidates = Array.from(new Set(links.before.concat(links.after))).filter(function (key) { return key !== id && records.has(key); });
      var contextNodes = new Map(); contextGraph.nodes.forEach(function (node) { contextNodes.set(recordId(node), node); });
      var ids = [id].concat(candidates.slice(0, MAX_MAP_NODES - 1)), known = new Set(ids);
      var nodes = ids.map(function (key) {
        var original = records.get(key) || contextNodes.get(key);
        var scoped = original.scope === original.id || original.kind === "scope" || original.kind === "boundary", exact = contextNodes.get(key);
        return Object.assign({}, exact || original, {id: key, _recordId: key, kind: original.kind === "boundary" || exact && exact.kind === "boundary" ? "boundary" : scoped ? "scope" : "node", url: original.route || original.url, isolated: noEdges && key === id,
          status_counts: scoped ? exact && exact.status_counts || [] : [[original.status, 1]],
          member_count: scoped ? exact && exact.member_count : 1, _unknownCount: scoped && !(exact && exact.member_count !== undefined),
          layout: {width: 208, height: 100}});
      });
      return Object.assign({}, contextGraph, {nodes: nodes, regions: [],
        view: Object.assign({}, contextGraph.view, {presentation: "dag"}),
        edges: (sharedIndex.relations || []).filter(function (edge) { return known.has(edge[0]) && known.has(edge[1]); }).map(function (edge) {
          return {source: edge[0], target: edge[1], statement: edge[2] || 0, proof: edge[3] || 0};
        })});
    }
    function regionProjection(payload, selectedArea) {
      if (!payload.view || payload.view.presentation !== "atlas") return payload;
      var groups = new Map(), owners = new Map();
      payload.nodes.filter(mapNode).forEach(function (node) {
        if (!node.area) return; if (!groups.has(node.area)) groups.set(node.area, []);
        groups.get(node.area).push(node); owners.set(node.id, node.area);
      });
      if (groups.size < 2 || payload.nodes.length < 12) return payload;
      var nodes = [], links = new Map(), replacements = new Map(), reserved = new Set(records.keys()), serial = 0;
      payload.nodes.forEach(function (node) { reserved.add(node.id); });
      groups.forEach(function (members, area) {
        if (area === selectedArea) { members.forEach(function (node) { replacements.set(node.id, node.id); nodes.push(node); }); return; }
        var key = "region:" + area, counts = new Map();
        if (reserved.has(key)) do { key = "view-region:" + serial++; } while (reserved.has(key));
        reserved.add(key);
        members.forEach(function (node) { replacements.set(node.id, key); (node.status_counts || []).forEach(function (pair) {
          counts.set(pair[0], (counts.get(pair[0]) || 0) + pair[1]);
        }); });
        nodes.push({id: key, kind: "scope", title: area, area: area, _region: area, _topics: members.length, status: members[0].status,
          status_counts: Array.from(counts.entries()), member_count: members.reduce(function (sum, node) { return sum + (node.member_count === undefined ? 0 : node.member_count); }, 0),
          summary: members.slice(0, 3).map(function (node) { return node.title; }).join(" · "),
          layout: {width: 264, height: 148}});
      });
      payload.nodes.forEach(function (node) { if (!owners.has(node.id) && mapNode(node)) { replacements.set(node.id, node.id); nodes.push(node); } });
      payload.edges.forEach(function (edge) {
        var a = replacements.get(edge.source), b = replacements.get(edge.target); if (!a || !b || a === b) return;
        var key = a + "\n" + b, found = links.get(key) || {source: a, target: b, statement: 0, proof: 0};
        found.statement += edge.statement || 0; found.proof += edge.proof || 0; links.set(key, found);
      });
      var wikiOwners = new Map();
      payload.nodes.forEach(function (node) { var owner = replacements.get(node.id), id = recordId(node); if (owner && id) wikiOwners.set(id, owner); });
      return Object.assign({}, payload, {nodes: nodes, edges: Array.from(links.values()), regions: [], $wikiOwners: wikiOwners});
    }
    function updateProjection() {
      ui$wikiProjectionTicket++; ui$wikiView = null;
      var focus = neighborhood(ui$requestedNode), eligible = contextGraph.nodes.filter(mapNode), known = new Set(eligible.map(function (node) { return node.id; }));
      var overview = !focus && scopeOverviewProjection(Object.assign({}, contextGraph, {nodes: eligible,
        edges: contextGraph.edges.filter(function (edge) { return known.has(edge.source) && known.has(edge.target); })}), ui$overviewGroup, records.keys());
      var projected = focus || overview || regionProjection(contextGraph, ui$area);
      ui$scopeOverview = Boolean(overview); ui$overviewGroup = overview && overview._scopeOverview.selectedGroup || null;
      host.classList.toggle("bp-dag-scope-overview", ui$scopeOverview);
      host.classList.toggle("bp-dag-area-expanded", Boolean(navigation && ui$area));
      adoptGraph(projected); ui$mapAllowed = mapAllowed; ui$activeDirty = true; ui$spotlight = ui$hovered = null;
      overlays.forEach(function (control) { control.remove(); }); overlays.clear();
      ui$selected = byId.get(ui$requestedNode) || null;
      if (!ui$filtersTouched) ui$selectedStatuses = new Set(presentStatuses);
      if (ui$selected && !ui$selectedStatuses.has(ui$selected.status)) ui$selected = null;
      if (dom$statusFieldset) renderStatusFields();
      renderBreadcrumb();
      projectWikiView();
    }
    // Where a result or topic lives: the nearest topics above it, short of the root or of the page a list belongs to,
    // so rows that share a title, such as ATLAS's 93 "Conjecture"s, tell themselves apart.
    function placeOf(node, stop) {
      var record = records.get(recordId(node)) || node, names = [], seen = new Set();
      var pending = records.get(record.scope === record.id ? semanticParent(record) : record.scope);
      while (pending && pending.id !== stop && !seen.has(pending.id) && semanticParent(pending)) {
        seen.add(pending.id); names.unshift(pending.title); pending = records.get(semanticParent(pending));
      }
      return names.slice(-2).join(" › ");
    }
    function semanticParent(node) {
      var parent = records.get(node.parent), seen = new Set();
      while (parent && ["collection", "transparent"].indexOf(parent.navigation) >= 0 && !seen.has(parent.id)) {
        seen.add(parent.id); parent = records.get(parent.parent);
      }
      return parent && parent.id;
    }
    function renderOutline() {
      var scopes = sharedIndex.nodes.filter(function (node) { return node.scope === node.id && ["collection", "transparent", "source"].indexOf(node.navigation) < 0; });
      var chain = new Set(), key = currentBundle && currentBundle.scope, pending = records.get(key);
      while (pending && !chain.has(pending.id)) { chain.add(pending.id); pending = records.get(semanticParent(pending)); }
      var root = records.get("roadmap") || scopes.find(function (node) { return !semanticParent(node); });
      if (root && dom$homeLink) {
        dom$homeLink.textContent = "Library"; dom$homeLink.href = root.route || root.url;
        dom$homeLink.title = root.title; dom$homeLink.setAttribute("aria-label", "Open " + root.title);
      }
      var visible = scopes.filter(function (node) { var parent = semanticParent(node); return !parent || (root && parent === root.id) || chain.has(parent); });
      var required = new Set(Array.from(chain).slice(0, ui$outlineLimit)), slots = Math.max(0, ui$outlineLimit - required.size);
      var shown = visible.filter(function (node) { return required.has(node.id) || slots-- > 0; });
      var list = element("ul", "bp-dag-navigation-list");
      shown.forEach(function (node) {
        if (root && node.id === root.id) return;
        var item = element("li"), link = readingLabel("a", "bp-dag-navigation-link", node.title, node.reading_title);
        link.href = node.route || node.url; link.title = node.title; link.dataset.navigationNodeId = node.id;
        if (node.id === key) link.setAttribute("aria-current", "page");
        if (chain.has(semanticParent(node)) && root && semanticParent(node) !== root.id) item.className = "bp-outline-child";
        item.appendChild(link); list.appendChild(item);
        link.addEventListener("click", function (event) {
          if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          event.preventDefault(); navigation.navigateNode(node.id, {history: "push"});
        });
      });
      dom$navigationInner.appendChild(element("h2", "bp-outline-label", "Topics")); dom$navigationInner.appendChild(list);
      if (shown.length < visible.length) {
        var more = button("bp-dag-navigation-more", "Show more topics");
        more.addEventListener("click", function () { ui$outlineLimit += 50; renderNavigation(); remember(); }); dom$navigationInner.appendChild(more);
      }
      if (navigation.declutterReader) navigation.declutterReader(dom$readingBody, shown.map(function (node) { return node.id; }));
    }
    function renderReaderContext(retainedWiki) {
      if (!dom$readingContext) return;
      dom$readingContext.replaceChildren();
      if (retainedWiki) Array.from(dom$readingNeighbors.children).forEach(function (child) { if (child !== retainedWiki) child.remove(); });
      else dom$readingNeighbors.replaceChildren();
      var node = ui$selected, record = records.get(currentBundle && currentBundle.nodeId || ui$requestedNode || (node && node.id)), scope = record && records.get(record.scope);
      if (scope && record.id !== scope.id) {
        var around = readingLabel("a", "bp-reading-around", scope.title, scope.reading_title, "← "); around.href = scope.route || scope.url;
        around.addEventListener("click", function (event) { if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
          event.preventDefault(); navigation.navigateNode(scope.id, {history: "push"}); }); dom$readingContext.appendChild(around);
      }
      syncSheetAccessibility();
      if (!record && !node) return;
      // A result's relations show once, in the statement's own Uses / Used by lists; the reader adds only its topic's other pages.
      var peers = topicPeers(record ? record.id : node.id);
      if (peers && peers.nodes.length) {
        var more = element("details", "bp-reading-related-topics");
        more.appendChild(readingLabel("summary", "", peers.owner.title, peers.owner.reading_title,
          peers.title.slice(0, peers.title.length - peers.owner.title.length)));
        var list = element("ul", "bp-reading-topic-list");
        peers.nodes.slice(0, 5).forEach(function (peer) {
          var item = element("li"), link = readingLabel("a", "", peer.title, peer.reading_title); link.href = peer.route || peer.url;
          link.addEventListener("click", function (event) {
            if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault(); navigation.navigateNode(peer.id, {history: "push", focus: "reading"});
          });
          item.appendChild(link); list.appendChild(item);
        });
        more.appendChild(list);
        if (peers.nodes.length > 5) {
          var browse = readingLabel("a", "bp-reading-topic-browse", peers.owner.title, peers.owner.reading_title, "Browse ");
          browse.href = peers.owner.route || peers.owner.url;
          browse.addEventListener("click", function (event) {
            if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault(); navigation.navigateNode(peers.owner.id, {history: "push", focus: "reading"});
          });
          more.appendChild(browse);
        }
        dom$readingNeighbors.appendChild(more);
      }
    }

    // Structural navigation is not a mathematical dependency or wiki backlink.
    function topicPeers(id) {
      var focus = records.get(id); if (!focus || !sharedIndex) return null;
      var scoped = focus.scope === focus.id;
      var owner = records.get(scoped ? semanticParent(focus) : focus.scope);
      if (!owner || scoped && owner.id === "roadmap") return null;
      var peers = sharedIndex.nodes.filter(function (peer) {
        if (peer.id === id || ["collection", "transparent", "source"].indexOf(peer.navigation) >= 0) return false;
        return scoped ? peer.kind === "scope" && peer.scope === peer.id && peer.result !== true && semanticParent(peer) === owner.id :
          (peer.result === true || peer.catalog === "module") && peer.scope === focus.scope;
      });
      return {owner: owner, title: (scoped ? "Other topics in " : "More in ") + owner.title, nodes: peers};
    }

    function buildShell() {
      var head = element("header", "bp-dag-head");
      var context = element("div", "bp-dag-context");
      var crumbs = dom$breadcrumb = element("nav", "bp-dag-breadcrumb");
      crumbs.setAttribute("aria-label", "Explorer context");
      renderBreadcrumb(); context.appendChild(crumbs);
      head.appendChild(context);
      // The explorer hides Material's header and its sun or moon, so this clicks that toggle: the one label Material leaves
      // unhidden, which points at the next palette entry, so a three-entry automatic palette steps on as Material's own does.
      // A palette list without toggle keys draws the form but no label, so it gets no switch.
      var schemes = document.querySelector("[data-md-component=palette]");
      if (schemes && schemes.querySelector("label")) {
        var themeLabel = "Switch light or dark theme", flip = button("bp-dag-button bp-dag-theme", "◐", themeLabel);
        flip.title = themeLabel; context.appendChild(flip);
        flip.onclick = function () { var next = schemes.querySelector("label:not([hidden])"); if (next) next.click(); };
      }

      var toolbar = element("div", "bp-dag-toolbar");
      toolbar.setAttribute("role", "group"); toolbar.setAttribute("aria-label", "Dependency explorer controls");
      dom$outlineToggle = button("bp-dag-button bp-dag-outline-toggle", "Topics", "Toggle topic outline");
      dom$outlineToggle.setAttribute("aria-expanded", "false"); toolbar.appendChild(dom$outlineToggle);
      var searchWrap = dom$searchWrap = element("div", "bp-dag-search-wrap");
      dom$search = element("input", "bp-dag-search");
      dom$search.type = "search"; dom$search.placeholder = "Search titles and Lean names";
      dom$search.setAttribute("aria-label", "Search all graph nodes");
      dom$search.setAttribute("aria-controls", "bp-dag-search-results-" + instance);
      dom$searchResults = element("div", "bp-dag-search-results"); dom$searchResults.id = "bp-dag-search-results-" + instance;
      dom$searchResults.setAttribute("role", "region"); dom$searchResults.setAttribute("aria-label", "Search results");
      dom$searchResults.hidden = true;
      searchWrap.append(dom$search, dom$searchResults); toolbar.appendChild(searchWrap);

      dom$filters = element("div", "bp-dag-filters");
      dom$filterSummary = button("bp-dag-filter-summary", "Filter: all statuses"); dom$filterSummary.setAttribute("aria-expanded", "false");
      dom$filterPopover = element("div", "bp-dag-filter-popover"); dom$filterPopover.hidden = true;
      dom$filterPopover.id = "bp-dag-filters-" + instance; dom$filterSummary.setAttribute("aria-controls", dom$filterPopover.id);
      var fieldset = dom$statusFieldset = element("fieldset"); dom$statusInputs = new Map();
      var edges = dom$edgeFieldset = element("fieldset", "bp-dag-isolated"), isolated = element("label", "bp-dag-check");
      edges.appendChild(element("legend", "", "Links"));
      dom$isolated = element("input"); dom$isolated.type = "checkbox"; isolated.appendChild(dom$isolated);
      isolated.appendChild(document.createTextNode("Show unlinked")); edges.appendChild(isolated);
      renderStatusFields();
      dom$filterPopover.append(fieldset, edges);
      dom$filters.append(dom$filterSummary, dom$filterPopover); toolbar.appendChild(dom$filters);

      dom$graphMode = button("bp-dag-button", "Back to map"); dom$graphMode.hidden = true;
      dom$graphMode.disabled = !mapAllowed; toolbar.appendChild(dom$graphMode);
      dom$browse = element("div", "bp-dag-browse");
      dom$browseToggle = button("bp-dag-button", "Browse", "Browse statements or source texts");
      dom$browseToggle.setAttribute("aria-expanded", "false");
      dom$browsePopover = element("div", "bp-dag-browse-popover"); dom$browsePopover.hidden = true;
      dom$browsePopover.id = "bp-dag-browse-" + instance;
      dom$browsePopover.setAttribute("role", "group"); dom$browsePopover.setAttribute("aria-label", "Browse library contents");
      dom$browseToggle.setAttribute("aria-controls", dom$browsePopover.id);
      dom$listMode = button("bp-dag-button", "Statements in this topic");
      dom$sourcesMode = button("bp-dag-button", "Source texts");
      dom$browsePopover.appendChild(dom$listMode); dom$browsePopover.appendChild(dom$sourcesMode);
      dom$browse.appendChild(dom$browseToggle); dom$browse.appendChild(dom$browsePopover); toolbar.appendChild(dom$browse);

      var zoomGroup = element("div", "bp-dag-zoom"); zoomGroup.setAttribute("role", "group"); zoomGroup.setAttribute("aria-label", "Zoom controls");
      dom$zoomOut = button("bp-dag-button", "−", "Zoom out"); dom$zoomIn = button("bp-dag-button", "+", "Zoom in");
      dom$fit = button("bp-dag-button", "Fit", "Fit shown nodes");
      zoomGroup.append(dom$zoomOut, dom$zoomIn, dom$fit); toolbar.appendChild(zoomGroup);

      dom$hiddenButton = button("bp-dag-hidden-button", ""); toolbar.appendChild(dom$hiddenButton);
      head.appendChild(toolbar);
      dom$error = element("div", "bp-dag-error"); dom$error.setAttribute("role", "alert"); dom$error.hidden = true; head.appendChild(dom$error);
      var bookLink = dom$homeLink = element("a", "bp-dag-book", "Library");
      bookLink.href = safeHref(view.book) || currentBundle.url; context.insertBefore(bookLink, crumbs);
      host.appendChild(head);

      var body = dom$body = element("div", "bp-dag-body"), main = element("div", "bp-dag-main");
      dom$navigation = element("aside", "bp-dag-navigation");
      dom$navigation.setAttribute("aria-label", "Wiki hierarchy");
      dom$navigationInner = element("div", "bp-dag-navigation-inner");
      dom$navigation.appendChild(dom$navigationInner); body.appendChild(dom$navigation);
      renderNavigation();
      dom$stage = element("section", "bp-dag-stage"); dom$stage.setAttribute("role", "region");
      dom$stage.setAttribute("aria-label", "Dependency graph. Drag or scroll to pan, " +
        "use Control or Command plus scroll to zoom, and use arrow keys between node buttons.");
      dom$canvas = element("canvas", "bp-dag-canvas"); dom$canvas.setAttribute("aria-hidden", "true");
      dom$nodeLayer = element("div", "bp-dag-node-layer"); dom$density = element("div", "bp-dag-density"); dom$density.hidden = true;
      dom$mapContext = element("div", "bp-map-context"); dom$mapKey = element("div", "bp-map-key");
      dom$stage.appendChild(dom$mapContext); dom$stage.appendChild(dom$mapKey);
      dom$empty = element("div", "bp-dag-empty", data.nodes.length ? "Nothing matches these filters." :
        "This roadmap has no articles yet."); dom$empty.hidden = true;
      dom$stage.append(dom$canvas, dom$nodeLayer, dom$density, dom$empty);
      main.appendChild(dom$stage);

      dom$inventory = element("section", "bp-dag-inventory"); dom$inventory.hidden = true;
      dom$inventory.setAttribute("aria-label", "Complete node inventory");
      var inventoryHead = element("div", "bp-dag-inventory-head"); inventoryHead.appendChild(element("h2", "", "Contents"));
      dom$inventorySummary = element("span", "bp-dag-inventory-summary"); inventoryHead.appendChild(dom$inventorySummary);
      dom$wholeSearch = button("bp-dag-whole-search", "search the whole roadmap");
      dom$inventoryScroll = element("div", "bp-dag-inventory-scroll"); dom$inventoryList = element("ul", "bp-dag-inventory-list");
      dom$inventoryScroll.appendChild(dom$inventoryList); dom$loadMore = button("bp-dag-load-more", "Show more nodes");
      dom$inventory.append(inventoryHead, dom$inventoryScroll, dom$loadMore);
      main.appendChild(dom$inventory); body.appendChild(main);

      dom$inspector = element("aside", "bp-dag-inspector bp-reading-pane"); dom$inspector.setAttribute("aria-label", "Mathematical reading");
      dom$sheetHandle = button("bp-dag-sheet-handle", "Reading", "");
      dom$sheetHandle.setAttribute("aria-expanded", "false");
      dom$inspectorInner = element("div", "bp-dag-inspector-inner");
      dom$inspectorInner.id = "bp-dag-inspector-content-" + instance;
      dom$sheetHandle.setAttribute("aria-controls", dom$inspectorInner.id);
      dom$inspector.appendChild(dom$sheetHandle);
      dom$inspector.appendChild(dom$inspectorInner); body.appendChild(dom$inspector); host.appendChild(body);
      dom$readingContext = element("div", "bp-reading-context");
      dom$readingBody = element("article", "bp-reading-body md-typeset");
      dom$inspectorInner.appendChild(dom$readingContext);
      dom$inspectorInner.appendChild(dom$readingBody);
      dom$readingNeighbors = element("section", "bp-reading-neighbors");
      dom$inspectorInner.appendChild(dom$readingNeighbors);
      navigation.bindReader(dom$readingBody);
      // A full phone reader hides the toolbar, so a pill at the bottom drops it back to its peek.
      var mapButton = button("bp-reading-map", "Map", "Show the map"); dom$inspector.appendChild(mapButton);
      mapButton.addEventListener("click", function () { setSheet(false, ui$selected); dom$sheetHandle.focus(); writeLocation("replace"); });
      dom$announcement = element("p", "bp-dag-sr-only"); dom$announcement.setAttribute("aria-live", "polite"); host.appendChild(dom$announcement);
      dom$ctx = dom$canvas.getContext("2d");
      // Reset the probe first: a rejected site font must not inherit an earlier 11px font.
      dom$ctx.font = "10px sans-serif";
      family = getComputedStyle(host).fontFamily; dom$ctx.font = "11px " + family;
      if (dom$ctx.font.indexOf("11px") < 0) family = "sans-serif";
    }

    function trailList(entries, className, whole) {
      var list = element("ol", className);
      // A folded trail keeps its ends, first / … / parent / current, and the "…" names the ancestors it stands for.
      if (!className && !whole && entries.length > 3) entries = [entries[0], {label: "…", hidden: entries.slice(1, -2).map(function (entry) { return entry.label; }).join(" / ")}].concat(entries.slice(-2));
      entries.forEach(function (entry, index) {
        var href = safeHref(entry.url), item = href ? element("li") : readingLabel("li", "", entry.label, entry.reading_title);
        item.title = entry.hidden || entry.label;
        if (href) {
          var link = readingLabel("a", "", entry.label, entry.reading_title); link.href = href; link.title = entry.label;
          link.addEventListener("click", function (event) {
            if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey ||
                new URL(href).origin !== window.location.origin) return;
            event.preventDefault();
            if (entry._overviewParent && (ui$scopeOverview && ui$overviewGroup || ui$area)) setMode("regions", true);
            else navigation.navigate(href, {history: "push", focus: true, mapFirst: true});
          });
          item.appendChild(link);
        }
        else if (entry.hidden) item.appendChild(element("span", "bp-dag-sr-only", entry.hidden));
        else if (!className && index === entries.length - 1) item.setAttribute("aria-current", "page");
        list.appendChild(item);
      });
      return list;
    }
    function renderBreadcrumb() {
      if (!dom$breadcrumb || !currentBundle) return;
      var entries = routeTrail(currentBundle); if (!entries.length) entries = [{label: data.title || "Dependencies"}];
      // A deep trail shows every crumb, which give way to ellipses as the row narrows, and folds its middle into "…" only
      // when even that cannot fit: a fold that hid the chapter of every Hartshorne result left most of a wide row empty.
      dom$breadcrumb.replaceChildren(trailList(entries, null, true));
      if (entries.length > 3 && dom$breadcrumb.scrollWidth > dom$breadcrumb.clientWidth + 1) dom$breadcrumb.replaceChildren(trailList(entries));
      refreshReadingLabels(dom$breadcrumb);
    }
    function renderNavigation() {
      if (!dom$navigationInner) return;
      dom$navigationInner.replaceChildren(); dom$navigationNodes = new Map();
      if (sharedIndex) renderOutline();
      refreshReadingLabels(dom$navigationInner);
    }

    function syncNavigationSelection() {
      if (!dom$navigationNodes) return;
      dom$navigationNodes.forEach(function (control, id) {
        if (ui$selected && ui$selected.id === id) control.setAttribute("aria-current", "true");
        else control.removeAttribute("aria-current");
      });
    }

    function bindEvents() {
      dom$search.addEventListener("focus", ensureGlobalSearch);
      dom$search.addEventListener("input", function () {
        ui$query = dom$search.value.trim(); ui$searchLimit = 12; ui$listLimit = LIST_PAGE;
        if (ui$mode === "list") closeSearch(); else renderSearch();
        renderInventory(true); if (ui$query) ensureGlobalSearch();
        remember();
      });
      dom$search.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { closeSearch(); return; }
        if (event.key === "ArrowDown") {
          if (openResults()) event.preventDefault();
        } else if (event.key === "Enter") {
          var matches = searchMatches(); if (matches.length) { event.preventDefault(); activateNode(matches[0].node, true); }
        }
      });
      dom$filterSummary.addEventListener("click", function () { setFilters(dom$filterPopover.hidden); });
      dom$filters.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !dom$filterPopover.hidden) { event.preventDefault(); setFilters(false); dom$filterSummary.focus(); }
      });
      dom$browseToggle.addEventListener("click", function () { setBrowse(dom$browsePopover.hidden); });
      dom$browse.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !dom$browsePopover.hidden) { event.preventDefault(); setBrowse(false); }
      });
      globalListen(document, "pointerdown", function (event) {
        if (!dom$filters.contains(event.target)) setFilters(false);
        if (!dom$browse.contains(event.target)) setBrowse(false, false);
        if (!dom$searchWrap.contains(event.target)) closeSearch();
      });
      dom$browse.addEventListener("focusout", function (event) {
        if (event.relatedTarget && !dom$browse.contains(event.relatedTarget)) setBrowse(false, false);
      });
      dom$searchWrap.addEventListener("focusout", function (event) {
        if (event.relatedTarget && !dom$searchWrap.contains(event.relatedTarget)) closeSearch();
      });
      dom$isolated.addEventListener("change", function () {
        ui$showIsolated = dom$isolated.checked; ui$activeDirty = true;
        writeLocation("replace"); refresh(true);
      });
      dom$graphMode.addEventListener("click", function () { setMode("graph", true); });
      dom$listMode.addEventListener("click", function () { setBrowse(false); ui$relationIds = null; setMode("list", true); });
      if (dom$sourcesMode) dom$sourcesMode.addEventListener("click", function () { setBrowse(false); setMode("sources", true); });
      dom$zoomIn.addEventListener("click", function () { zoomAt(1.24, dom$stage.clientWidth / 2, dom$stage.clientHeight / 2); });
      dom$zoomOut.addEventListener("click", function () { zoomAt(.8, dom$stage.clientWidth / 2, dom$stage.clientHeight / 2); });
      dom$fit.addEventListener("click", fit); dom$hiddenButton.addEventListener("click", revealAll);
      dom$loadMore.addEventListener("click", function () { ui$listLimit += LIST_PAGE; renderInventory(false); });
      dom$wholeSearch.addEventListener("click", openResults);
      if (dom$outlineToggle) dom$outlineToggle.addEventListener("click", function () {
        var open = !host.classList.contains("bp-dag-outline-open"); host.classList.toggle("bp-dag-outline-open", open);
        dom$outlineToggle.setAttribute("aria-expanded", String(open));
        remember();
      });
      // On a phone the handle drags the reader, which snaps to the nearest of peek, half and full.
      var drag = null, dragged = false;
      dom$sheetHandle.addEventListener("pointerdown", function (event) {
        dragged = false; if (!navigation || !sheetLayout()) return;
        drag = [event.clientY, sheetTop()];
        dom$sheetHandle.setPointerCapture(event.pointerId);
      });
      dom$sheetHandle.addEventListener("pointermove", function (event) {
        if (!drag || !dragged && Math.abs(event.clientY - drag[0]) <= 8) return;
        dragged = true; dom$inspector.style.transition = "none";
        dom$inspector.style.transform = "translateY(" + clamp(drag[1] + event.clientY - drag[0], 0, dom$inspector.clientHeight - 56) + "px)";
      });
      function sheetTop() { return dom$inspector.getBoundingClientRect().top - dom$body.getBoundingClientRect().top; }
      function dropSheet() {
        if (!drag) return; drag = null; if (!dragged) return;
        var height = dom$inspector.clientHeight, top = sheetTop();
        dom$inspector.style.transition = dom$inspector.style.transform = "";
        // Full is at 0, half at 45% and peek 56px from the bottom; these are the midpoints between them.
        setSheet(top < height * .225 ? 2 : top < height * .725 - 28 ? 1 : 0, ui$selected); writeLocation("replace");
      }
      dom$sheetHandle.addEventListener("pointerup", dropSheet); dom$sheetHandle.addEventListener("pointercancel", dropSheet);
      dom$sheetHandle.addEventListener("click", function (event) {
        // The drag already placed the sheet; a keyboard click has no detail.
        if (dragged && event.detail) return;
        var open = host.classList.contains("bp-dag-sheet-open");
        // A tap on a phone cycles peek, half and full.
        setSheet(navigation && sheetLayout() ? (open + host.classList.contains("bp-dag-sheet-full") + 1) % 3 : !open, ui$selected);
        if (navigation && !sheetLayout() && host.classList.contains("bp-dag-sheet-open")) {
          dom$readingBody.tabIndex = -1; dom$readingBody.focus({preventScroll: true});
        }
        writeLocation("replace");
      });
      dom$inspector.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && ui$selected) { event.preventDefault(); clearSelection(true, true); }
      });
      dom$stage.addEventListener("pointerdown", pointerDown); dom$stage.addEventListener("pointermove", pointerMove);
      dom$stage.addEventListener("pointerup", pointerUp); dom$stage.addEventListener("pointercancel", pointerUp);
      dom$stage.addEventListener("wheel", function (event) {
        var zoom = event.ctrlKey || event.metaKey, pixels = event.deltaMode === 1 ? 25 : 1;
        event.preventDefault();
        if (!zoom) { ui$transform.x -= event.deltaX * pixels; ui$transform.y -= event.deltaY * pixels; schedule(); remember(); return; }
        var rect = dom$stage.getBoundingClientRect();
        zoomAt(clamp(Math.exp(-event.deltaY * pixels * .002), .5, 2), event.clientX - rect.left, event.clientY - rect.top);
      }, {passive: false});
      dom$stage.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && ui$selected) { clearSelection(true); event.preventDefault(); }
        else if (event.key === "+" || event.key === "=") { zoomAt(1.2, dom$stage.clientWidth / 2, dom$stage.clientHeight / 2); event.preventDefault(); }
        else if (event.key === "-") { zoomAt(.82, dom$stage.clientWidth / 2, dom$stage.clientHeight / 2); event.preventDefault(); }
        else if (event.key === "0") { fit(); event.preventDefault(); }
      });
      globalListen(document, "keydown", function (event) {
        var target = event.target, own = target === dom$search;
        if (!own && (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName || "") || target.isContentEditable)) return;
        if ((event.key === "/" && !own) || ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k")) {
          event.preventDefault(); dom$search.focus(); dom$search.select();
        }
      });
      if (window.ResizeObserver) { var ro = new ResizeObserver(resize); ro.observe(dom$stage); observers.push(ro); }
      else globalListen(window, "resize", resize);
      globalListen(window, "resize", syncSheetAccessibility);
      if (navigation) [dom$readingBody, dom$navigation, dom$inventoryScroll].forEach(function (pane) { pane.addEventListener("scroll", remember); });
      var theme = new MutationObserver(schedule); theme.observe(document.body, {attributes: true, attributeFilter: ["data-md-color-scheme"]}); observers.push(theme);
      if (dom$homeLink) dom$homeLink.addEventListener("click", function (event) {
        if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); navigation.navigate(dom$homeLink.href, {history: "push"});
      });
    }

    function statusLabel(key) { return palette[key] && palette[key].label ? palette[key].label : key.replace(/_/g, " "); }
    function renderStatusFields() {
      dom$statusFieldset.replaceChildren(element("legend", "", "Formalization status"));
      dom$statusInputs.clear();
      presentStatuses.forEach(function (key) {
        var label = element("label", "bp-dag-check"), input = element("input");
        input.type = "checkbox"; input.checked = ui$selectedStatuses.has(key); input.value = key;
        input.addEventListener("change", onStatusChange); dom$statusInputs.set(key, input);
        // The filter doubles as the legend: each status says what it means, in the palette's own words.
        var text = element("span", "bp-dag-check-text", statusLabel(key)), meaning = (palette[key] || {}).meaning;
        if (meaning) text.appendChild(element("span", "bp-dag-check-meaning", meaning));
        label.appendChild(input); label.appendChild(swatch(key)); label.appendChild(text);
        label.appendChild(element("span", "bp-dag-check-count", String(statusCounts.get(key) || 0))); dom$statusFieldset.appendChild(label);
      });
      dom$edgeFieldset.hidden = !data.nodes.some(function (node) { return node.isolated; }) || data.nodes.every(function (node) { return node.isolated; });
    }
    function swatch(key) {
      var entry = palette[key] || palette.planned || {}, mark = element("span", "bp-dag-swatch");
      mark.title = entry.meaning || statusLabel(key);
      ["light", "dark"].forEach(function (scheme) {
        var color = entry[scheme] || {};
        mark.style.setProperty("--dag-swatch-" + scheme + "-fill", solid(key, color) || "transparent");
        mark.style.setProperty("--dag-swatch-" + scheme + "-stroke", color.stroke || "#9ca3af");
      });
      return mark;
    }
    // States with a tinted light fill (finished, conditionally proved, not ready, inventory pending): the tints are too pale to tell from an outline at a few pixels, so small marks fill them with the stroke.
    function solid(key, color) { return ((palette[key] || palette.planned || {}).light || {}).fill === "#FFFFFF" ? color.fill : color.stroke; }
    function isDark() { return document.body.getAttribute("data-md-color-scheme") === "slate"; }
    function colors(node) {
      if (node._region || node._overviewGroup) return isDark() ? {fill: "#242526", stroke: "#737578", text: "#E4E6EB"} : {fill: "#FFFFFF", stroke: "#CED0D4", text: "#1C1E21"};
      var entry = palette[node.status] || palette.planned || {};
      return entry[isDark() ? "dark" : "light"] || {fill: "#fff", stroke: "#9ca3af", text: "#374151"};
    }
    // Another topic's card says how it touches this page and how many of its results it links here, not its own total.
    function boundaryMeta(node) {
      var uses = prerequisites && (prerequisites.get(node.id) || []).length, usedBy = dependents && (dependents.get(node.id) || []).length;
      var count = node.member_count || finished(node)[1], results = plural(count, "result", "results");
      // The count is the other topic's results: those this page needs, or those that build on it.
      return uses && usedBy ? results + ", both ways" : usedBy ? results + " needed here" : uses ? results + (count === 1 ? " builds" : " build") + " on this" :
        plural(count, "linked result", "linked results");
    }
    // A scope's fully proved results of its targets. Module inventories are neutral and stay out of both counts, as in the hero's figure.
    function finished(node) {
      var done = 0, total = 0;
      (node.status_counts || []).forEach(function (entry) { if ((palette[entry[0]] || {}).neutral) return; total += entry[1]; if (/^(fully_proved|mathlib)$/.test(entry[0])) done += entry[1]; });
      return [done, total];
    }
    // A topic's or region's statuses as a progress bar, in legend order since a region merges its topics' counts as it meets them.
    // status.STATES' order: the palette's keys arrive sorted, and regions merge their topics' counts out of order.
    var LEGEND = "mathlib fully_proved proved defined conditional can_prove stated can_state not_ready planned".split(" ");
    function legendRank(key) { var index = LEGEND.indexOf(key); return index < 0 ? LEGEND.length : index; }
    function statusRing(node) {
      var entries = (node.status_counts || []).filter(function (entry) { return entry[1] > 0; })
        .sort(function (a, b) { return legendRank(a[0]) - legendRank(b[0]); });
      var total = entries.reduce(function (sum, entry) { return sum + entry[1]; }, 0), cursor = 0, stops = [];
      entries.forEach(function (entry) {
        var color = (palette[entry[0]] || palette.planned || {})[isDark() ? "dark" : "light"] || {};
        var end = cursor + entry[1] / Math.max(1, total) * 100;
        stops.push((color.stroke || "#9ca3af") + " " + cursor + "% " + end + "%"); cursor = end;
      });
      return stops.length ? "linear-gradient(90deg," + stops.join(",") + ")" : "var(--dag-node-stroke)";
    }
    function compactRegions() {
      return Boolean(navigation && atlas && !ui$area && dom$stage && dom$stage.clientWidth > 0 && dom$stage.clientWidth <= 620 &&
        data.nodes.length && data.nodes.every(function (node) { return node._region || node._overviewGroup; }));
    }
    function geometry() {
      if (!mapAllowed) return null;
      var compact = compactRegions(), aspect = dom$stage.clientWidth / Math.max(1, dom$stage.clientHeight);
      aspect = Math.round((Number.isFinite(aspect) && aspect > 0 ? clamp(aspect, .25, 4) : 1.4) * 10) / 10;
      if (sceneData !== data || sceneDirection !== ui$orientation || sceneCompact !== compact || sceneFamily !== family || sceneAspect !== aspect || sceneWiki !== data.$wikiEdges) {
        var anchor = typeof document !== "undefined" && document.activeElement && document.activeElement.dataset &&
          document.activeElement.dataset.autoformNodeId, previous = !restoring && sceneData === data && scene && scene.nodes.get(anchor);
        scene = layoutGraph(data.nodes.map(function (node) { return {id: node.id, width: compact ? 312 : node.width,
          height: compact ? 96 : Math.max(node.height, node._region ? 148 : 100), area: node.area, scope: node.scope}; }), data.edges, ui$orientation,
          aspect, data.$wikiEdges);
        scene.$wikiRoutes = wikiRoutes(scene);
        var moved = previous && scene.nodes.get(anchor);
        if (moved) { ui$transform.x += (previous.x + previous.w / 2 - moved.x - moved.w / 2) * ui$transform.scale;
          ui$transform.y += (previous.y + previous.h / 2 - moved.y - moved.h / 2) * ui$transform.scale; }
        sceneData = data; sceneDirection = ui$orientation; sceneCompact = compact; sceneFamily = family; sceneAspect = aspect; sceneWiki = data.$wikiEdges;
      }
      return scene;
    }
    function world(node) {
      return geometry().nodes.get(node.id);
    }
    function screen(node) {
      var box = world(node), scale = ui$transform.scale;
      return {x: box.x * scale + ui$transform.x, y: box.y * scale + ui$transform.y, w: box.w * scale, h: box.h * scale};
    }
    function inViewport(box, pad) {
      return box.x + box.w >= -pad && box.y + box.h >= -pad && box.x <= dom$stage.clientWidth + pad && box.y <= dom$stage.clientHeight + pad;
    }
    function refresh(resetList) {
      if (resetList) ui$listLimit = LIST_PAGE;
      syncControls(); renderInspector(); renderInventory(resetList); schedule();
    }
    function syncControls() {
      calculateActive();
      host.classList.toggle("bp-dag-compact-regions", compactRegions());
      if (navigation) {
        var record = records.get(currentBundle && currentBundle.nodeId), links = record && record.result && neighborIds(record.id), group = currentOverviewGroup();
        // A long title gives way before the counts after it.
        dom$mapContext.replaceChildren(element("span", "", links ? record.title : group ? group.area || "Other topics" : ui$area || currentBundle && currentBundle.title || data.title),
          links ? " · uses " + links.before.length + " · used by " + links.after.length : "");
        dom$mapKey.replaceChildren();
        ["statement", "proof"].forEach(function (kind) {
          if (data.edges.some(function (edge) { return edge[kind]; })) dom$mapKey.appendChild(element("span", "bp-map-key-" + kind,
            (kind === "proof" ? "Proof" : "Statement") + (sheetLayout() ? "" : " dependency")));
        });
        // One entry says what the dotted lines are and how many of them this map draws; a map that draws none says nothing about their coverage.
        if (data.$wikiEdges && data.$wikiEdges.length) {
          var local = sceneData === data && scene && scene.$wikiRoutes;
          dom$mapKey.appendChild(element("span", "bp-map-key-wiki", (local && local.omitted ? "Concept links: " + num(local.total - local.omitted) + " of " +
            num(local.total) + (sheetLayout() ? "" : " on this map") : "Concept links") + (ui$wikiView && ui$wikiView.availability === "partial" ? ", some pages unread" : "")));
        }
        if (ui$wikiView && ui$wikiView.availability === "unavailable") dom$mapKey.appendChild(element("span", "bp-map-key-coverage", "Concept links unavailable"));
        if (data.$wikiEdges && data.$wikiEdges.some(function (edge) { return !wikiOwnerVisible(byId.get(edge.source), ui$selectedStatuses) || !wikiOwnerVisible(byId.get(edge.target), ui$selectedStatuses); }))
          dom$mapKey.appendChild(element("span", "bp-map-key-coverage", "Concept links hidden by filter"));
      }
      syncNavigationSelection();
      dom$isolated.checked = ui$showIsolated;
      dom$statusInputs.forEach(function (input, key) { input.checked = ui$selectedStatuses.has(key); });
      var enabled = ui$selectedStatuses.size;
      dom$filterSummary.textContent = "Filter: " + (enabled === presentStatuses.length ? "all" : enabled + " of " + presentStatuses.length) +
        " statuses" + (ui$showIsolated ? "" : " · linked only");
      dom$graphMode.hidden = ui$mode === "graph";
      dom$listMode.textContent = currentBundle && currentBundle.scope === "roadmap" ? "All statements" : "Statements in this topic";
      // Browse marks the list on screen, so at most one of its choices reads as pressed; on the map neither does.
      pressed(dom$listMode, ui$mode === "list");
      if (dom$sourcesMode) pressed(dom$sourcesMode, ui$mode === "sources");
      dom$stage.hidden = ui$mode !== "graph"; dom$inventory.hidden = ui$mode === "graph";
      dom$graphMode.disabled = !ui$mapAllowed;
      // A disabled Back to map says why the map is unavailable.
      if (ui$mapAllowed) dom$graphMode.removeAttribute("title"); else dom$graphMode.title = "Too many statements to draw as a map";
      if (navigation) dom$inventory.querySelector("h2").textContent = ui$mode === "sources" ? "Source tracks" : "Statements";
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
      if (!ui$selected) return null;
      return ui$spotlight || (ui$spotlight = spotlightOf(collect(ui$selected.id, prerequisites), collect(ui$selected.id, dependents)));
    }
    function matchesStatus(node) {
      return (node._region || node._overviewGroup) && node.status_counts && node.status_counts.length ?
        node.status_counts.some(function (pair) { return ui$selectedStatuses.has(pair[0]); }) : ui$selectedStatuses.has(node.status);
    }
    function calculateActive() {
      if (!ui$activeDirty) return;
      var directional = null;
      if (ui$selected && ui$direction === "prerequisites") directional = collect(ui$selected.id, prerequisites);
      if (ui$selected && ui$direction === "dependents") directional = collect(ui$selected.id, dependents);
      ui$active = data.nodes.filter(function (node) {
        return matchesStatus(node) && (ui$showIsolated || !node.isolated || node === ui$selected) &&
          (!directional || directional.has(node.id));
      });
      ui$activeIds = new Set(ui$active.map(function (node) { return node.id; }));
      if (data._scopeOverview) {
        var members = data._scopeOverview.groups.reduce(function (all, group) { return all.concat(group.members); }, []);
        data.nodes.forEach(function (node) { if (node.kind === "boundary") members.push(node); });
        var visibleIds = new Set(members.filter(matchesStatus).map(function (node) { return node.id; }));
        data._scopeOverview._flows = data.edges.map(function (edge) {
          return edge._overviewEdges.reduce(function (counts, original) {
            if (visibleIds.has(original.source) && visibleIds.has(original.target)) {
              counts.statement += original.statement || 0; counts.proof += original.proof || 0;
            }
            return counts;
          }, {statement: 0, proof: 0});
        });
        data._scopeOverview._flowRange = Math.max(4, Math.log2(1 + data.edges.reduce(function (max, edge) {
          return Math.max(max, edge.statement || 0, edge.proof || 0);
        }, 0)));
      }
      var hidden = data.nodes.length - ui$active.length;
      dom$hiddenButton.textContent = "Show " + hidden + " filtered out"; dom$hiddenButton.hidden = !hidden;
      dom$empty.hidden = ui$active.length !== 0;
      ui$activeDirty = false;
    }

    function schedule() {
      if (!alive || scheduled || ui$mode !== "graph") return;
      scheduled = true; frame = requestAnimationFrame(function () { scheduled = false; if (alive) draw(); });
    }
    function resize() {
      syncSheetAccessibility();
      if (!dom$canvas || !dom$stage.clientWidth || !dom$stage.clientHeight) return;
      host.classList.toggle("bp-dag-compact-regions", compactRegions());
      var ratio = Math.min(2, window.devicePixelRatio || 1);
      dom$canvas.width = Math.max(1, Math.round(dom$stage.clientWidth * ratio));
      dom$canvas.height = Math.max(1, Math.round(dom$stage.clientHeight * ratio));
      var nextOrientation = dom$stage.clientWidth <= 620 ? "tb" : "lr";
      var changed = nextOrientation !== ui$orientation; ui$orientation = nextOrientation;
      if (firstResize || changed) { firstResize = false; fit(); } else if (ui$mode === "graph") draw();
    }
    function fit() {
      if (!mapAllowed) return;
      calculateActive(); var all = ui$active.length ? ui$active : data.nodes; if (!all.length) return;
      // Fit a bounded authored-area overview to its cards, not a peripheral
      // return path. Never shrink the repository or relax the readable floor.
      var compact = compactRegions(), pad = 36, side = compact ? 18 : pad,
        top = compact ? 52 : 56, bottom = compact ? 44 : 56, stageW = dom$stage.clientWidth;
      var shown = mapHeight();
      var readableFloor = .9;
      // Unlinked cards are inventory: when the whole map cannot open readable and whole, the first view frames the linked graph.
      var linked = all.filter(function (node) { return !node.isolated; }), box = frame(all);
      if ((box.wide || box.tall) && linked.length) box = frame(linked);
      var scale = ui$transform.scale = box.scale;
      // A map that fits, if only once the padding gives way, is centred whole. A real crop centres the selection or a requested
      // area's first card. With nothing to centre on, any map but a phone's compact overview starts each cropped axis at the graph's
      // leading edge rather than leaving half the stage empty around one card; a scope overview keeps whatever useful local card
      // that leaves in view, since an external boundary first in line is no reason to push the overview off the viewport.
      ui$transform.x = (stageW - box.w * scale) / 2 - box.x * scale;
      ui$transform.y = (shown + top - bottom - box.h * scale) / 2 - box.y * scale;
      var target = ui$selected || box.nodes.find(function (node) { return ui$area && node.area === ui$area && !node._region; });
      if (box.wide || box.tall || compact && box.fitted < readableFloor) {
        if (target) center(target, readableFloor);
        else {
          if (!compact && box.wide) ui$transform.x = side - box.x * scale;
          if (!compact && box.tall) ui$transform.y = top - box.y * scale;
          if (data._scopeOverview) {
            if (!usableLocal()) center(all.find(function (node) { return node._overviewGroup; }) ||
              all.find(function (node) { return node.kind !== "boundary"; }) || all[0], readableFloor);
          } else if (compact) center(box.nodes[0], readableFloor);
          else if (!usableLocal()) lead();
        }
      }
      schedule(); remember();
      // A deep map's leading corner can be bare, or hold only another topic's card, and Fit gave the same empty view.
      // Then the page's own first card, in reading order, opens at the leading edge, centred across.
      function lead() {
        var tb = ui$orientation === "tb", first = box.nodes.filter(function (node) { return node.kind !== "boundary"; }).sort(function (a, b) {
          var p = world(a), q = world(b); return tb ? p.y - q.y || p.x - q.x : p.x - q.x || p.y - q.y;
        })[0];
        if (!first) return;
        var cell = world(first), free = freeArea();
        if (tb) { ui$transform.x = (free.left + free.right) / 2 - (cell.x + cell.w / 2) * scale; ui$transform.y = (free.top || 0) + top - cell.y * scale; }
        else { ui$transform.x = free.left + side - cell.x * scale; ui$transform.y = (free.top || 0) + free.band / 2 - (cell.y + cell.h / 2) * scale; }
      }
      function usableLocal() {
        var free = freeArea(), start = (free.top || 0) + 12, end = (free.top || 0) + free.band - 12;
        return all.some(function (node) {
          if (node.kind === "boundary") return false; var cell = screen(node);
          return cell.x >= free.left + 12 && cell.x + cell.w <= free.right - 12 && cell.y >= start && cell.y + cell.h <= end;
        });
      }
      function frame(nodes) {
        var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        nodes.forEach(function (node) {
          var cell = world(node); minX = Math.min(minX, cell.x); minY = Math.min(minY, cell.y);
          maxX = Math.max(maxX, cell.x + cell.w); maxY = Math.max(maxY, cell.y + cell.h);
        });
        // Route detours are context, not a reason to shrink useful mathematical cards.
        var width = Math.max(1, maxX - minX), height = Math.max(1, maxY - minY), fitted = Math.min((stageW - side * 2) / width, (shown - top - bottom) / height),
          scale = clamp(Math.max(fitted, readableFloor), MIN_SCALE, 1.35);
        // Across, the padding gives way first: cards that fit the stage's full width stay whole.
        // A map scaled to fit its height exactly can overshoot it by a rounding error, which is not a crop.
        return {nodes: nodes, x: minX, y: minY, w: width, h: height, fitted: fitted, scale: scale,
          wide: width * scale > stageW && !compact, tall: height * scale > shown - top - bottom + 1e-6};
      }
    }
    // A phone's open reader leaves the map only the band above it, so the map fits there and counts what falls below as off screen.
    function mapHeight() { return navigation && sheetLayout() && host.classList.contains("bp-dag-sheet-open") ? freeArea().band : dom$stage.clientHeight; }
    // Open details cover part of the map; this is the larger part they leave, in stage pixels.
    function freeArea() {
      var band = dom$stage.clientHeight, left = 0, right = dom$stage.clientWidth, drawer = dom$inspector;
      if (compactRegions() && (!sheetLayout() || !host.classList.contains("bp-dag-sheet-open"))) return {top: 52, band: Math.max(NODE_H, band - 96), left: 18, right: right - 18};
      if (!sheetLayout()) return {band: band, left: left, right: right};
      if (host.classList.contains("bp-dag-sheet-open")) {
        // The workspace sheet slides on a transform, so its top is the stylesheet's 45%, not offsetTop.
        if (sheetLayout()) band = Math.max(NODE_H, Math.min(band, drawer.clientHeight * .45));
      }
      return {band: band, left: left, right: right};
    }
    function center(node, minimumScale) {
      if (!node) return; var box = world(node), free = freeArea(); ui$transform.scale = Math.max(ui$transform.scale, minimumScale || .82);
      ui$transform.x = (free.left + free.right) / 2 - (box.x + box.w / 2) * ui$transform.scale;
      ui$transform.y = (free.top || 0) + free.band / 2 - (box.y + box.h / 2) * ui$transform.scale; schedule();
    }
    function centerIfHidden(node) {
      if (!node) return; var box = screen(node), free = freeArea();
      if (box.x < 12 || box.y < 12 || box.x + box.w > dom$stage.clientWidth - 12 || box.y + box.h > (free.top || 0) + free.band - 12) center(node, .82);
    }
    function zoomAt(factor, x, y) {
      var old = ui$transform.scale, next = clamp(old * factor, MIN_SCALE, 4);
      if (ui$scopeOverview && ui$overviewGroup && factor < 1 && next < .82) {
        navigation.checkpoint(); ui$overviewGroup = null; updateProjection(); firstResize = true; resize(); writeLocation("push"); return;
      }
      if (ui$scopeOverview && !ui$overviewGroup) {
        if (factor > 1 && next >= 1.6) {
          var group = ui$active.find(function (node) { var box = screen(node); return node._overviewGroup &&
            x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h; });
          if (group) { activateNode(group); return; }
        }
        next = Math.max(.9, next);
      }
      ui$transform.x = x - (x - ui$transform.x) * next / old;
      ui$transform.y = y - (y - ui$transform.y) * next / old; ui$transform.scale = next; schedule(); remember();
    }

    function draw() {
      calculateActive(); var ctx = dom$ctx, ratio = Math.min(2, window.devicePixelRatio || 1);
      var width = dom$stage.clientWidth, height = dom$stage.clientHeight;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
      var current = geometry(); if (!current) return;
      var visible = ui$active.filter(function (node) { return inViewport(screen(node), 80); });
      // The area overview stays quiet until a card is selected, hovered or focused; then only its own links show.
      // The compact overview (phones and most tablets) keeps every link: a tap there opens an area and nothing hovers, so nothing could light one.
      var quiet = !compactRegions() && data.nodes.every(function (node) { return node._region || node._overviewGroup; }), subject = ui$selected || ui$hovered;
      // At rest a quiet overview draws no links, so its key says how to light them instead of naming line kinds it does not draw.
      host.classList.toggle("bp-dag-links-resting", Boolean(quiet && !subject && data.edges.length));
      // A hovered card previews its direct neighbours on any map; a selection keeps its own spotlight.
      var spotlight = (atlas || !ui$selected) && subject ? atlasSpotlight(subject) : pathSpotlight();
      var bottom = mapHeight(), labels = [];
      (current.$wikiFamilies || []).forEach(function (family) {
        if (family.island_count < 2 || family.members.some(function (id) { return !ui$activeIds.has(id) || !wikiOwnerVisible(byId.get(id), ui$selectedStatuses); })) return;
        var box = family.bounds;
        var area = {x: box.x * ui$transform.scale + ui$transform.x - 20, y: box.y * ui$transform.scale + ui$transform.y - 20,
          w: box.w * ui$transform.scale + 40, h: box.h * ui$transform.scale + 40};
        if (!inViewport(area, 0)) return;
        ctx.save(); ctx.fillStyle = isDark() ? "#84a7bd" : "#63879b"; ctx.globalAlpha = .035;
        rounded(ctx, area.x, area.y, area.w, area.h, 24); ctx.fill(); ctx.restore();
      });
      // Concept links follow the dependency links' rule: a quiet overview shows only its subject's, and a preview or selection fades the rest.
      if (current.$wikiRoutes) current.$wikiRoutes.routes.forEach(function (route) {
        if (!ui$activeIds.has(route.source) || !ui$activeIds.has(route.target)) return;
        if (!wikiOwnerVisible(byId.get(route.source), ui$selectedStatuses) || !wikiOwnerVisible(byId.get(route.target), ui$selectedStatuses)) return;
        var touches = subject && (route.source === subject.id || route.target === subject.id);
        if (quiet && !touches) return;
        drawRoute(ctx, route, !subject || touches ? -1 : 0);
      });
      current.routes.forEach(function (route) {
          if (!ui$activeIds.has(route.source) || !ui$activeIds.has(route.target) ||
            quiet && !(subject && (route.source === subject.id || route.target === subject.id))) return;
          if (data._scopeOverview) {
            route = scopeOverviewFlow(route, data._scopeOverview._flows[route.edgeIndex], data._scopeOverview._flowRange);
            if (!route) return;
          }
          var bounds = {x: route.bounds.x * ui$transform.scale + ui$transform.x, y: route.bounds.y * ui$transform.scale + ui$transform.y,
            w: route.bounds.w * ui$transform.scale, h: route.bounds.h * ui$transform.scale};
          if (!inViewport(bounds, 40)) return;
          // A scope overview stacks by its status-filtered flows, which can drop one kind of a bundle.
          var painted = route, edge = data._scopeOverview ? data._scopeOverview._flows[route.edgeIndex] : data.edges[route.edgeIndex];
          if (route.parts) {
            var points = [], radii = [];
            route.parts.forEach(function (part) { points = points.concat(part.points); radii = radii.concat(part.radii); });
            painted = Object.assign({}, route, {points: points, radii: radii});
          }
          // A pair linked in both kinds stacks its two count pills.
          var label = drawRoute(ctx, painted, edgeRole(spotlight, route.source, route.target), edge.statement && edge.proof ? route.kind === "proof" ? 1 : -1 : 0, bottom);
          if (label) labels.push(label);
      });
      var cards = visible.map(screen), pills = [];
      // Larger counts choose their spots first.
      labels.sort(function (a, b) { return b.count - a.count; }).forEach(function (label) { drawPill(ctx, label, cards, pills); });
      visible.forEach(function (node) { drawPlate(ctx, node, screen(node), !spotlight || spotlight.nodes.has(node.id)); });
      updateOverlays(visible, spotlight);
    }
    function atlasSpotlight(node) {
      var spot = spotlightOf(new Set([node.id].concat(prerequisites.get(node.id) || [])), new Set([node.id].concat(dependents.get(node.id) || [])));
      spot.subject = node.id; return spot;
    }
    function spotlightOf(before, after) {
      var nodes = new Set(before); after.forEach(function (id) { nodes.add(id); });
      return {after: after, before: before, nodes: nodes};
    }
    // -1 is a link at rest; under a selection 1 is on its prerequisite chain, 2 among its dependents, and 0 fades.
    // A link into a previewed card is a prerequisite and one out of it a dependent, even when the neighbour is both, as in a cycle of areas.
    function edgeRole(spotlight, source, target) {
      if (spotlight && spotlight.subject) { if (target === spotlight.subject && spotlight.before.has(source)) return 1; if (source === spotlight.subject && spotlight.after.has(target)) return 2; }
      return !spotlight ? -1 : spotlight.before.has(source) && spotlight.before.has(target) ? 1 : spotlight.after.has(source) && spotlight.after.has(target) ? 2 : 0;
    }
    // At rest a link is a full grey line; a selection draws its prerequisites in the link blue and its dependents in the text colour.
    function edgeStyle(ctx, role) {
      var dark = isDark(); ctx.globalAlpha = role ? 1 : .08; ctx.lineWidth = role > 0 ? 2.5 : 1.5;
      ctx.strokeStyle = ctx.fillStyle = role > 1 ? dark ? "#E4E6EB" : "#1C1E21" : role > 0 ? dark ? "#2D88FF" : "#0064E0" : dark ? "#A8B0BB" : "#8A8D91";
    }
    // Only a lit link names its count, in words on a pill in the map colour.
    function pill(ctx, count, x, y, scale) {
      var text = count + " links", h = 20 * scale; ctx.font = "600 " + 13 * scale + "px " + family;
      var w = ctx.measureText(text).width + 12 * scale; ctx.fillStyle = isDark() ? "#18191a" : "#f0f2f5"; ctx.lineWidth = 1;
      rounded(ctx, x - w / 2, y - h / 2, w, h, h / 2); ctx.fill(); ctx.stroke();
      ctx.fillStyle = ctx.strokeStyle; ctx.textAlign = "center"; ctx.fillText(text, x, y + 4.5 * scale);
    }
    function rounded(ctx, x, y, w, h, radius) {
      var r = Math.min(radius, w / 2, h / 2); ctx.beginPath(); ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y);
      ctx.quadraticCurveTo(x + w, y, x + w, y + r); ctx.lineTo(x + w, y + h - r);
      ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h); ctx.lineTo(x + r, y + h);
      ctx.quadraticCurveTo(x, y + h, x, y + h - r); ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y); ctx.closePath();
    }
    function drawPlate(ctx, node, box, highlighted) {
      var color = colors(node), tiny = ui$transform.scale < .32;
      ctx.save(); ctx.globalAlpha = highlighted ? (tiny ? .82 : .24) : .07; ctx.fillStyle = color.fill;
      ctx.strokeStyle = color.stroke; ctx.lineWidth = tiny ? 1 : 1.25;
      if (atlas && node.kind === "scope") {
        rounded(ctx, box.x, box.y, box.w, box.h, 14 * ui$transform.scale); ctx.fill(); ctx.stroke(); ctx.restore(); return;
      }
      // A zoomed-out plate is the only status signal left, so a highlighted one is opaque: at .82 over the map neighbouring greens fell under 1.5:1.
      if (tiny) { if (highlighted) ctx.globalAlpha = 1; ctx.fillStyle = solid(node.status, color); ctx.fillRect(box.x, box.y, Math.max(3, box.w), Math.max(3, box.h)); }
      else { rounded(ctx, box.x, box.y, box.w, box.h, 7 * ui$transform.scale); ctx.fill(); ctx.stroke(); }
      ctx.restore();
    }
    // A lit count comes back as a pill for draw() to paint after every route; bottom is the map's visible band.
    function drawRoute(ctx, route, role, stack, bottom) {
      if (!route.points || route.points.length < 2) return;
      var scale = ui$transform.scale, points = route.points.map(function (point) {
        return {x: point.x * scale + ui$transform.x, y: point.y * scale + ui$transform.y};
      });
      // Colour and fade follow the link's role; the width stays the router's weighted strength, a step wider when lit.
      ctx.save(); edgeStyle(ctx, role); ctx.lineWidth = (route.strength + (role > 0 ? 1 : 0)) * scale;
      // Round caps would close a lit proof link's 4px gaps, so its dashes keep butt ends.
      ctx.lineCap = route.kind === "proof" ? "butt" : "round"; ctx.lineJoin = "round";
      if (route.kind === "proof") ctx.setLineDash([6 * scale, 4 * scale]);
      if (route.kind === "wiki") { ctx.globalAlpha = role ? .38 : .1; ctx.strokeStyle = isDark() ? "#84a7bd" : "#63879b"; ctx.setLineDash([1.5 * scale, 5 * scale]); }
      ctx.beginPath(); ctx.moveTo(points[0].x, points[0].y);
      for (var index = 1; index < points.length - 1; index++) {
        var point = points[index], before = points[index - 1], after = points[index + 1], radius = route.radii[index] * scale;
        var d1 = Math.hypot(point.x - before.x, point.y - before.y), d2 = Math.hypot(after.x - point.x, after.y - point.y);
        if (!radius) { ctx.lineTo(point.x, point.y); continue; }
        ctx.lineTo(point.x - (point.x - before.x) * radius / d1, point.y - (point.y - before.y) * radius / d1);
        ctx.quadraticCurveTo(point.x, point.y, point.x + (after.x - point.x) * radius / d2, point.y + (after.y - point.y) * radius / d2);
      }
      var tip = points[points.length - 1], arrow = route.arrow;
      ctx.lineTo(tip.x, tip.y); ctx.stroke(); ctx.setLineDash([]);
      if (route.kind === "wiki") { ctx.restore(); return; }
      ctx.beginPath(); ctx.moveTo(tip.x, tip.y);
      ctx.lineTo(tip.x - (arrow.dx * 10 - arrow.dy * 5) * scale, tip.y - (arrow.dy * 10 + arrow.dx * 5) * scale);
      ctx.lineTo(tip.x - (arrow.dx * 10 + arrow.dy * 5) * scale, tip.y - (arrow.dy * 10 - arrow.dx * 5) * scale);
      ctx.closePath(); ctx.fill();
      // Cached straight portions carry one static direction cue. The solver
      // remains finite/cached; neither this painter nor the cue schedules RAF.
      var cache = drawRoute.$flowSegments || (drawRoute.$flowSegments = new WeakMap()), key = route.parts || route.points;
      var segments = cache.get(key);
      if (!segments) {
        segments = [];
        for (var at = 1; at < route.points.length; at++) {
          var a = route.points[at - 1], b = route.points[at], length = Math.hypot(b.x - a.x, b.y - a.y);
          var start = (route.radii[at - 1] || 0) + (at === 1 ? 24 : 8), end = length - (route.radii[at] || 0) - (at === route.points.length - 1 ? 24 : 8);
          if (end - start < 32) continue;
          var dx = (b.x - a.x) / length, dy = (b.y - a.y) / length;
          segments.push({x: a.x + dx * start, y: a.y + dy * start, dx: dx, dy: dy, length: end - start});
        }
        segments.sort(function (a, b) { return b.length - a.length; }); segments = segments.slice(0, 3); cache.set(key, segments);
      }
      var spots = [], width = dom$stage.clientWidth, height = bottom || dom$stage.clientHeight;
      segments.forEach(function (segment) {
        var x = segment.x * scale + ui$transform.x, y = segment.y * scale + ui$transform.y, dx = segment.dx * segment.length * scale, dy = segment.dy * segment.length * scale;
        var low = 0, high = 1;
        [[x, dx, width], [y, dy, height]].forEach(function (axis) {
          if (Math.abs(axis[1]) < 1e-7) { if (axis[0] < 12 || axis[0] > axis[2] - 12) high = -1; }
          else { var a = (12 - axis[0]) / axis[1], b = (axis[2] - 12 - axis[0]) / axis[1]; low = Math.max(low, Math.min(a, b)); high = Math.min(high, Math.max(a, b)); }
        });
        var length = (high - low) * segment.length * scale;
        if (length >= 12) spots.push({x: x + dx * (low + high) / 2, y: y + dy * (low + high) / 2, dx: segment.dx, dy: segment.dy, length: length});
      });
      spots.sort(function (a, b) { return b.length - a.length; });
      var cue = spots[0];
      // Only a lit link names its count, in words on a pill in the cue's place; its terminal arrow still shows the direction.
      // A route with no straight part long enough for the cue takes its pill halfway along.
      if (route.count > 1 && role > 0) {
        if (!cue) {
          var half = 0;
          points.slice(1).forEach(function (b, i) { half += Math.hypot(b.x - points[i].x, b.y - points[i].y) / 2; });
          points.slice(1).some(function (b, i) {
            var a = points[i], length = Math.hypot(b.x - a.x, b.y - a.y);
            if (half > length) { half -= length; return false; }
            spots.push({x: a.x + (b.x - a.x) * half / (length || 1), y: a.y + (b.y - a.y) * half / (length || 1)}); return true;
          });
        }
        ctx.restore();
        // A pair linked in both kinds starts its two pills 11px apart, and drawPill moves the second clear of the first; each spot keeps its segment's run to slide along.
        return {count: route.count, role: role, scale: scale, spots: spots.map(function (spot) {
          var at = {x: spot.x, y: spot.y + (stack || 0) * 11 * scale}; if (spot.dx !== undefined) { at.dx = spot.dx; at.dy = spot.dy; at.length = spot.length; } return at;
        })};
      }
      if (cue) {
        ctx.lineWidth = Math.min(1.6, route.strength) * scale;
        ctx.beginPath(); ctx.moveTo(cue.x - (cue.dx * 6 + cue.dy * 3) * scale, cue.y - (cue.dy * 6 - cue.dx * 3) * scale);
        ctx.lineTo(cue.x, cue.y); ctx.lineTo(cue.x - (cue.dx * 6 - cue.dy * 3) * scale, cue.y - (cue.dy * 6 + cue.dx * 3) * scale); ctx.stroke();
      }
      ctx.restore(); return null;
    }
    // Pills paint after every route, so no later stroke crosses one; each takes its route's first spot clear of the cards.
    // A pill takes the first spot clear of the cards and of the pills already painted, sliding along its segment a pill at a time;
    // with no room left beside the pills it is left out rather than printed over one.
    function drawPill(ctx, label, cards, pills) {
      var scale = label.scale, h = 20 * scale; ctx.save(); edgeStyle(ctx, label.role);
      ctx.font = "600 " + 13 * scale + "px " + family; var w = ctx.measureText(label.count + " links").width + 12 * scale;
      var candidates = label.spots.slice();
      label.spots.forEach(function (spot) {
        for (var step = 1; spot.dx !== undefined && step * (w + 6) * 2 <= spot.length; step++) [1, -1].forEach(function (side) {
          candidates.push({x: spot.x + side * step * (w + 6) * spot.dx, y: spot.y + side * step * (w + 6) * spot.dy});
        });
      });
      var apart = function (boxes) { return function (spot) {
        return boxes.every(function (box) {
          return spot.x + w / 2 + 4 < box.x || spot.x - w / 2 - 4 > box.x + box.w || spot.y + h / 2 + 4 < box.y || spot.y - h / 2 - 4 > box.y + box.h;
        });
      }; };
      var spot = candidates.find(apart(cards.concat(pills || []))) || candidates.find(apart(pills || []));
      if (spot) { pill(ctx, label.count, spot.x, spot.y, scale); if (pills) pills.push({x: spot.x - w / 2, y: spot.y - h / 2, w: w, h: h}); }
      ctx.restore();
    }

    function updateOverlays(visible, spotlight) {
      var candidates = ui$transform.scale >= .34 ? visible.slice() : [];
      var focusedId = document.activeElement && document.activeElement.dataset.autoformNodeId, focused = byId.get(focusedId);
      if (focused && ui$activeIds.has(focusedId) && candidates.indexOf(focused) < 0) candidates.push(focused);
      if (ui$selected && ui$activeIds.has(ui$selected.id) && candidates.indexOf(ui$selected) < 0) candidates.push(ui$selected);
      candidates.sort(function (a, b) {
        if (a === ui$selected) return -1; if (b === ui$selected) return 1;
        var ab = screen(a), bb = screen(b); return ab.y - bb.y || ab.x - bb.x || a._index - b._index;
      });
      var keep = new Set(candidates.map(function (node) { return node.id; }));
      var free = freeArea(), top = free.top || 0, available = candidates.find(function (node) {
        var box = screen(node);
        return box.x >= free.left && box.x + box.w <= free.right && box.y >= top && box.y + box.h <= top + free.band;
      });
      // Culling includes an 80px paint margin, not a usable keyboard viewport.
      // Keep an owned focus while panning; otherwise start on a contained card.
      var tabId = keep.has(focusedId) ? focusedId : available && available.id || ui$selected && ui$selected.id || candidates[0] && candidates[0].id;
      overlays.forEach(function (control, id) { if (!keep.has(id)) { control.remove(); overlays.delete(id); } });
      var weights = ui$selected && relationCounts(data.edges, ui$selected.id);
      candidates.forEach(function (node, index) {
        var control = overlays.get(node.id);
        if (!control) { control = makeNodeControl(node); overlays.set(node.id, control); dom$nodeLayer.appendChild(control); }
        var box = screen(node), size = world(node), color = colors(node);
        control.style.transform = "translate3d(" + box.x + "px," + box.y + "px,0) scale(" + ui$transform.scale + ")";
        control.style.width = size.w + "px"; control.style.height = size.h + "px";
        control.style.setProperty("--dag-node-fill", color.fill); control.style.setProperty("--dag-node-stroke", color.stroke);
        control.style.setProperty("--dag-node-text", color.text); control.dataset.selected = String(node === ui$selected);
        if (atlas && node.kind === "scope") control.style.setProperty("--dag-status-ring", statusRing(node));
        var relation = "";
        if (atlas && ui$selected && node !== ui$selected) {
          if ((prerequisites.get(ui$selected.id) || []).indexOf(node.id) >= 0) relation = "prerequisite";
          else if ((dependents.get(ui$selected.id) || []).indexOf(node.id) >= 0) relation = "dependent";
        }
        control.dataset.relation = relation;
        // The map shows a link's weight only as width and a lit pill, so a card linked to the selection names it by kind for screen readers.
        if (!control.$label) control.$label = control.getAttribute("aria-label");
        var named = [control.$label];
        if (weights && node !== ui$selected) [["used by ", weights.before], ["uses ", weights.after]].forEach(function (side) {
          var counts = side[1].get(node.id); if (counts) named.push(side[0] + ui$selected.title + " · " + weightWords(counts));
        });
        if (control.getAttribute("aria-label") !== named.join(", ")) control.setAttribute("aria-label", named.join(", "));
        var relationLabel = control.querySelector(".bp-dag-node-relation");
        if (relationLabel) relationLabel.textContent = relation ? (relation === "prerequisite" ? "Used by " : "Uses ") + ui$selected.title : "";
        control.dataset.subdued = String(Boolean(spotlight && !spotlight.nodes.has(node.id)));
        control.tabIndex = node.id === tabId ? 0 : -1;
      });
      // The note asks for a drag only while a shown card's real box reaches past the stage or under a phone's open reader,
      // and for a zoom only while labels are too small. It counts cards, which Browse does not list, so it names no Browse total.
      var bottom = mapHeight(), whole = ui$active.filter(function (node) {
        var box = screen(node); return box.x >= 0 && box.y >= 0 && box.x + box.w <= dom$stage.clientWidth && box.y + box.h <= bottom;
      }).length, cropped = whole < ui$active.length, tiny = visible.length && ui$transform.scale < .34;
      dom$density.hidden = !cropped && !tiny;
      dom$density.textContent = cropped ? "Drag to see more: " + num(whole) + " of " + num(ui$active.length) + " cards in view." :
        tiny ? "Zoom in to reveal " + visible.length + " node labels" : "";
      // A removed card gets no pointerleave or blur, and a late timer can name one from the previous projection.
      if (ui$hovered && !overlays.has(ui$hovered.id)) { ui$hovered = null; schedule(); }
    }
    function makeNodeControl(node) {
      var control = button("bp-dag-node", ""); control.dataset.autoformNodeId = node.id; control.dataset.kind = node.kind || "node";
      if (node._overviewGroup) control.dataset.overviewGroup = node._overviewGroup;
      // A topic or region names its fully proved share, which its bar cannot: statement formalized shares proved's green.
      var count = finished(node), scopeMeta = node.kind === "scope" ? node._unknownCount ? "Topic" : count[1] ?
        num(count[0]) + " of " + num(count[1]) + " fully proved" : plural(node.member_count || 0, "article", "articles") :
        node.kind === "boundary" ? boundaryMeta(node) : null;
      // ATLAS titles such as "Theorem (Crystallographic Restriction)" already name their kind, so the meta does not repeat it.
      var kind = KINDS.get((node.declaration || "").toLowerCase());
      if (kind && (node.title || "").indexOf(kind) === 0) kind = "";
      // A scope overview's group card is an area card, including its bucket of topics with no area.
      var area = node._region || node._overviewGroup;
      var lead = area ? plural(node._topics, "topic", "topics") : scopeMeta ? "" : kind, tail = scopeMeta || statusLabel(node.status);
      var meta = area || !lead ? tail : lead + " · " + tail;
      // A boundary's colour is the only place its linked results' statuses show, so a screen reader hears them as words.
      var linked = node.kind === "boundary" && count[1] ? ", " + (count[0] === count[1] ? count[1] === 1 ? "fully proved" : "all fully proved" :
        num(count[0]) + " of " + num(count[1]) + " fully proved") : "";
      control.setAttribute("aria-label", node.title + (node.area ? ", " + node.area : "") + ", " +
        meta + linked + (node.isolated ? ", no dependency edges in this view" : ""));
      var content = atlas && node.kind === "scope" ? element("span", "bp-dag-node-core") : control;
      content.appendChild(element("span", "bp-dag-node-title", node.title));
      // The kind word or topic count gives way first, so the status or count the line is there to show stays whole.
      var line = content.appendChild(element("span", "bp-dag-node-meta"));
      if (lead) { line.appendChild(element("span", "", lead)); line.appendChild(element("span", "", " · ")); }
      line.appendChild(element("span", "", tail));
      // An area's summary leads with its topic count, which the stylesheet then drops from the meta.
      if (area && node.summary) content.appendChild(element("span", "bp-dag-node-summary", lead + ": " + node.summary));
      content.appendChild(element("span", "bp-dag-node-relation", ""));
      if (content !== control) control.appendChild(content);
      // A drag or pinch that began on the card is not a tap; a keyboard click has no detail and always opens it.
      control.addEventListener("click", function (event) { event.stopPropagation(); if (!ui$moved || !event.detail) activateNode(node); });
      // Resting a mouse on a card for 120 ms, or focusing it from the keyboard, previews its neighbours without selecting it; touch has no hover.
      // Leaving waits as long, so a mouse crossing the gap to another card hands over after its delay instead of flashing the whole map back.
      ["pointerenter", "focus", "pointerleave", "blur"].forEach(function (type, index) {
        control.addEventListener(type, function (event) {
          clearTimeout(hoverTimer);
          if (index - 1 ? index || event.pointerType !== "touch" : control.matches(":focus-visible")) hoverTimer = setTimeout(function () { ui$hovered = index < 2 && node; schedule(); }, index % 2 ? 0 : 120);
        });
      });
      control.addEventListener("keydown", function (event) {
        if (/^Arrow(Left|Right|Up|Down)$/.test(event.key)) {
          var next = nearest(node, event.key);
          if (next) {
            event.preventDefault();
            center(next, .7);
            focusNodeControl(next);
          }
        } else if (event.key === "Escape") { event.preventDefault(); clearSelection(true); }
      });
      return control;
    }
    function nearest(node, key) {
      var from = world(node), fx = from.x + from.w / 2, fy = from.y + from.h / 2, best = null, bestScore = Infinity;
      ui$active.forEach(function (candidate) {
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

    // A press on a card stays a tap until it travels 8px or a second finger lands. Only then does the stage
    // take the pointers, since a captured press would send the tap's click to the stage instead of the card.
    function pointerDown(event) {
      ui$pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
      if (ui$pointers.size === 1) ui$moved = false;
      if (ui$pointers.size === 2) { ui$pinch = pinchState(); grab(); }
    }
    function grab() {
      ui$moved = true; dom$stage.dataset.dragging = "true";
      ui$pointers.forEach(function (_, id) { dom$stage.setPointerCapture(id); });
    }
    function pinchState() {
      var points = Array.from(ui$pointers.values()), dx = points[1].x - points[0].x, dy = points[1].y - points[0].y;
      return {distance: Math.max(1, Math.hypot(dx, dy)), x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2};
    }
    function pointerMove(event) {
      // A press released off the stage never sends its pointerup here; a move with no button down ends it.
      if (!event.buttons) ui$pointers.delete(event.pointerId);
      var previous = ui$pointers.get(event.pointerId);
      if (!previous) return;
      var dx = event.clientX - previous.x, dy = event.clientY - previous.y;
      // The press point stays put until the 8px crossing, so the first pan step carries the whole way travelled.
      if (!ui$moved) { if (Math.hypot(dx, dy) <= 8) return; grab(); }
      ui$pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
      if (ui$pointers.size === 1) {
        ui$transform.x += dx; ui$transform.y += dy; schedule();
      } else if (ui$pointers.size === 2) {
        var next = pinchState(), rect = dom$stage.getBoundingClientRect();
        ui$transform.x += next.x - ui$pinch.x; ui$transform.y += next.y - ui$pinch.y;
        zoomAt(next.distance / ui$pinch.distance, next.x - rect.left, next.y - rect.top);
        ui$pinch = next;
      }
    }
    function pointerUp(event) {
      if (!ui$pointers.has(event.pointerId)) return;
      var wasSingle = ui$pointers.size === 1; ui$pointers.delete(event.pointerId);
      if (ui$pointers.size < 2) ui$pinch = null;
      if (!ui$pointers.size) dom$stage.dataset.dragging = "false";
      // A tap on a card leaves the selection to the card's click.
      if (event.type === "pointerup" && wasSingle && !ui$moved && ui$selected && !event.target.closest(".bp-dag-node")) clearSelection(true);
      // Only a pan or pinch moved the camera; a card's tap records its own history.
      if (ui$moved) remember();
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
      if (sharedIndex) return Promise.resolve(sharedIndex);
      if (ui$searchPromise) return ui$searchPromise;
      ui$searchLoading = true; dom$search.setAttribute("aria-busy", "true");
      ui$searchPromise = navigation.loadIndex().then(function (loaded) {
        useIndex(loaded); ui$searchError = null;
      }).catch(function (error) {
        ui$searchError = error.message;
      }).then(function () {
        ui$searchLoading = false; dom$search.removeAttribute("aria-busy");
        if (dom$searchResults.hidden) return;
        var focused = document.activeElement, inside = dom$searchResults.contains(focused);
        var key = inside && (focused.dataset.resultId || "more");
        renderSearch();
        if (inside) (Array.from(dom$searchResults.querySelectorAll("button")).find(function (control) {
          return (control.dataset.resultId || "more") === key;
        }) || dom$search).focus();
      }).finally(function () { ui$searchPromise = null; });
      return ui$searchPromise;
    }
    function rankMatches(nodes, query) {
      return nodes.map(function (node) {
        var rank = searchScore(node, query); return rank ? {node: node, rank: rank} : null;
      }).filter(Boolean).sort(function (a, b) { return a.rank - b.rank || a.node._index - b.node._index; });
    }
    function searchMatches() {
      var query = ui$query.toLocaleLowerCase(); if (!query) return [];
      if (query === ui$searchCacheQuery) return ui$searchCache;
      ui$searchCacheQuery = query; ui$searchCache = rankMatches(ui$searchNodes, query);
      return ui$searchCache;
    }
    function renderSearch() {
      dom$searchResults.textContent = ""; if (!ui$query) { closeSearch(); return; }
      var matches = searchMatches(), list = element("ul", "bp-dag-results");
      matches.slice(0, ui$searchLimit).forEach(function (match) {
        var item = element("li"), choose = button("bp-dag-result", ""); choose.dataset.resultId = match.node.id;
        choose.appendChild(element("span", "bp-dag-result-title", match.node.title));
        choose.appendChild(element("span", "bp-dag-result-meta", statusLabel(match.node.status))).title = (palette[match.node.status] || {}).meaning || "";
        var place = placeOf(match.node), topic = (records.get(recordId(match.node)) || match.node).scope === match.node.id;
        if (place || topic) choose.appendChild(element("span", "bp-dag-result-place", topic ? "Topic" + (place ? " in " + place : "") : place));
        choose.addEventListener("click", function () { activateNode(match.node, true); }); item.appendChild(choose); list.appendChild(item);
        choose.addEventListener("keydown", searchResultKeydown);
      });
      dom$searchResults.appendChild(list);
      if (matches.length > ui$searchLimit) {
        var remaining = matches.length - ui$searchLimit, increment = Math.min(SEARCH_PAGE, remaining);
        var more = button("bp-dag-load-more", "Show " + num(increment) + " more of " + num(matches.length));
        more.addEventListener("click", function () {
          var previous = ui$searchLimit;
          ui$searchLimit = Math.min(matches.length, ui$searchLimit + SEARCH_PAGE); renderSearch();
          var results = dom$searchResults.querySelectorAll(".bp-dag-result");
          if (results[previous]) results[previous].focus();
        });
        more.addEventListener("keydown", searchResultKeydown);
        dom$searchResults.appendChild(more);
      }
      if (!matches.length) dom$searchResults.appendChild(element("p", "bp-dag-overview", "No matching title, statement, article id, or Lean name."));
      if (ui$searchLoading) dom$searchResults.appendChild(element("p", "bp-dag-overview", "Searching the whole roadmap…"));
      else if (ui$searchError) dom$searchResults.appendChild(element("p", "bp-dag-overview", "Whole-roadmap search is unavailable; showing this view."));
      dom$searchResults.hidden = false;
      dom$announcement.textContent = matches.length ? plural(matches.length, "result", "results") : "No results";
    }
    function searchResultKeydown(event) {
      if (event.key === "Escape") {
        event.preventDefault(); closeSearch(); dom$search.focus(); return;
      }
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      var controls = Array.from(dom$searchResults.querySelectorAll("button"));
      var index = controls.indexOf(event.currentTarget);
      var next = controls[index + (event.key === "ArrowDown" ? 1 : -1)];
      if (next) { event.preventDefault(); next.focus(); }
      else if (event.key === "ArrowUp") { event.preventDefault(); dom$search.focus(); }
    }
    function setFilters(open) {
      if (!open && dom$filterPopover.contains(document.activeElement)) dom$filterSummary.focus({preventScroll: true});
      dom$filterPopover.hidden = !open; dom$filterSummary.setAttribute("aria-expanded", String(open));
    }
    function setBrowse(open, returnFocus) {
      if (!dom$browsePopover) return;
      if (!open && returnFocus !== false && dom$browsePopover.contains(document.activeElement)) dom$browseToggle.focus({preventScroll: true});
      dom$browsePopover.hidden = !open; dom$browseToggle.setAttribute("aria-expanded", String(open));
    }
    function closeSearch() { dom$searchResults.hidden = true; }
    function openResults() {
      if (dom$searchResults.hidden && ui$query) renderSearch();
      var first = !dom$searchResults.hidden && dom$searchResults.querySelector("button");
      if (first) first.focus(); return Boolean(first);
    }
    function activateNode(node) {
      if (node._overviewGroup) {
        navigation.checkpoint(); ui$overviewGroup = node._overviewGroup; ui$requestedNode = null; ui$selected = null; ui$direction = "both";
        updateProjection(); setSheet(false, null); refresh(true); firstResize = true; resize(); writeLocation("push");
        var group = data._scopeOverview.groups.find(function (item) { return item.key === ui$overviewGroup; });
        var member = group && ui$active.find(function (item) { return group.members.some(function (original) { return original.id === item.id; }); });
        if (member) { centerIfHidden(member); focusNodeControl(member); } else dom$sheetHandle.focus();
        return;
      }
      if (node._region) {
        navigation.checkpoint();
        var keyboard = document.activeElement && document.activeElement.dataset.autoformNodeId === node.id;
        ui$area = node._region; ui$requestedNode = null; updateProjection(); setSheet(false, null);
        refresh(true); firstResize = true; resize(); writeLocation("push");
        if (keyboard && ui$active.length) focusNodeControl(ui$active.find(function (item) { return item.area === ui$area && !item._region; }) || ui$active[0]); return;
      }
      var id = recordId(node), record = records.get(id), settings = {history: "push", focus: "reading"};
      if (record && record.scope === record.id && !record.result) settings.mapFirst = true;
      navigation.navigateNode(id, settings); closeSearch();
    }
    function focusNodeControl(node) {
      requestAnimationFrame(function () { requestAnimationFrame(function () {
        var control = overlays.get(node.id); if (control) control.focus(); else dom$sheetHandle.focus(); remember(); schedule();
      }); });
    }

    function clearSelection(updateHistory, returnFocus) {
      if (navigation && updateHistory) navigation.checkpoint();
      var refit = ui$direction !== "both", cleared = ui$selected;
      ui$selected = null; ui$direction = "both"; ui$activeDirty = true; ui$spotlight = null;
      setSheet(navigation && !sheetLayout() && !ui$overviewOnly && !ui$scopeOverview, null);
      syncNavigationSelection(); renderInspector(); renderInventory(false); if (refit) requestAnimationFrame(fit); else schedule();
      if (updateHistory) writeLocation("push");
      var chosen = returnFocus && cleared && (ui$mode === "list" ? dom$inventoryRows.get(cleared.id) : overlays.get(cleared.id));
      if (chosen) chosen.focus();
    }
    // From 681px to 1,100px details can open the head's first row; the map shifts back by it, so nothing jumps.
    // Measure both class changes together; callers need no selection-toggle ordering rule.
    // A phone reader also has a full position: 1 asks for half, 2 for full, and true keeps full if it is there.
    function setSheet(open, selected) {
      var top = dom$body.offsetTop; host.classList.toggle("bp-dag-sheet-open", Boolean(open));
      host.classList.toggle("bp-dag-sheet-full", open > 1 || open === true && host.classList.contains("bp-dag-sheet-full"));
      host.classList.toggle("bp-dag-has-selection", Boolean(selected));
      ui$transform.y += top - dom$body.offsetTop; schedule();
      dom$sheetHandle.setAttribute("aria-expanded", String(Boolean(open)));
      syncSheetAccessibility();
    }
    function sheetLayout() { return Boolean(window.matchMedia && window.matchMedia("(max-width: 720px)").matches); }
    function syncSheetAccessibility() {
      if (!dom$inspectorInner || !dom$sheetHandle) return;
      var opened = host.classList.contains("bp-dag-sheet-open");
      var collapsed = !opened && (sheetLayout() || Boolean(ui$area) || ui$overviewOnly || ui$scopeOverview);
      // The handle names the page or entry, with its status, rather than a generic "Reading"; its accessible name starts with that title.
      // An open desktop reader over an area or the overview closes with ×.
      var close = opened && !sheetLayout() && (ui$area || ui$overviewOnly || ui$scopeOverview), title = (currentBundle || data).title,
        record = records.get(currentBundle && currentBundle.nodeId || ui$requestedNode || (ui$selected && ui$selected.id));
      dom$sheetHandle.replaceChildren(close ? "×" : record && record.status ? swatch(record.status) : "", close ? "" : title);
      dom$sheetHandle.setAttribute("aria-label", close ? "Close reading" : title + ", reading");
      dom$sheetHandle.title = !opened ? "Open reading" : navigation && sheetLayout() && !host.classList.contains("bp-dag-sheet-full") ? "Expand reading" : "Close reading";
      if (collapsed) {
        if (dom$inspectorInner.contains(document.activeElement)) dom$sheetHandle.focus();
        dom$inspectorInner.setAttribute("inert", ""); dom$inspectorInner.setAttribute("aria-hidden", "true");
      } else {
        dom$inspectorInner.removeAttribute("inert"); dom$inspectorInner.removeAttribute("aria-hidden");
      }
      // A phone's full reader covers the map and the list, so they leave the tab order and the accessibility tree while it does.
      var main = host.querySelector && host.querySelector(".bp-dag-main");
      if (main && navigation && sheetLayout() && host.classList.contains("bp-dag-sheet-full")) {
        if (main.contains(document.activeElement)) dom$sheetHandle.focus();
        main.setAttribute("inert", "");
      } else if (main) main.removeAttribute("inert");
      if (navigation) {
        if (!dom$pageHeading) { dom$pageHeading = element("h1", "bp-dag-sr-only"); host.prepend(dom$pageHeading); }
        dom$pageHeading.textContent = currentBundle && currentBundle.title || data.title;
        dom$pageHeading.hidden = !collapsed;
      }
    }
    function revealAll() {
      ui$selectedStatuses = new Set(presentStatuses); ui$showIsolated = true; ui$direction = "both";
      ui$activeDirty = true; writeLocation("replace"); refresh(true); fit();
    }
    function onStatusChange() {
      ui$filtersTouched = true;
      ui$selectedStatuses = new Set();
      dom$statusInputs.forEach(function (input, key) { if (input.checked) ui$selectedStatuses.add(key); });
      if (ui$selected && !ui$selectedStatuses.has(ui$selected.status)) {
        ui$selected = null; ui$direction = "both"; ui$spotlight = null;
        setSheet(navigation && !sheetLayout() && !ui$overviewOnly && !ui$scopeOverview, null);
        dom$announcement.textContent = "Selection cleared because its status is hidden.";
      }
      ui$activeDirty = true; writeLocation("replace"); refresh(true);
    }
    function setMode(mode, updateHistory) {
      if (mode === "graph" && !ui$mapAllowed) return;
      var overviewNavigation = mode === "regions" && Boolean(ui$scopeOverview && ui$overviewGroup || ui$area);
      if (overviewNavigation && updateHistory) navigation.checkpoint();
      if (mode !== "list") ui$relationIds = null;
      if (mode === "regions" && ui$scopeOverview) {
        ui$overviewGroup = null; ui$requestedNode = null; ui$selected = null; ui$direction = "both";
        updateProjection(); setSheet(false, null); mode = "graph"; fit();
      } else if (mode === "regions" && !ui$area && navigation && currentBundle && currentBundle.scope !== "roadmap" && records.has("roadmap")) {
        navigation.navigateNode("roadmap", {history: "push", focus: false}); return;
      }
      if (mode === "regions") { ui$area = null; ui$requestedNode = null; updateProjection(); mode = "graph"; fit(); }
      ui$mode = mode; syncControls(); if (mode !== "graph") renderInventory(true); else schedule();
      if (navigation && sheetLayout()) setSheet(false, ui$selected);
      if (updateHistory) writeLocation(overviewNavigation ? "push" : "replace");
    }

    function renderWikiContext() {
      if (!dom$readingNeighbors) return;
      var previous = dom$readingNeighbors.querySelector(".bp-reading-wiki-links");
      var node = ui$selected, id = currentBundle && currentBundle.nodeId || ui$requestedNode || node && recordId(node);
      if (previous && ui$wikiRendered === ui$wikiLinks && ui$wikiNode === id) return;
      var opened = previous && previous.open && ui$wikiNode === id; if (previous) previous.remove();
      ui$wikiRendered = ui$wikiLinks; ui$wikiNode = id;
      if (!ui$wikiLinks || !records.has(id)) return;
      var links = wikiNeighbors(id);
      if (links.availability === "complete" && !links.before.length && !links.after.length) return;
      var disclosure = element("details", "bp-reading-wiki-links");
      if (opened) disclosure.open = true;
      disclosure.appendChild(element("summary", "", "Concept links"));
      if (links.availability !== "complete") disclosure.appendChild(element("p", "bp-reading-wiki-notice",
        links.availability === "partial" ? "Some pages could not be read. Concept links and backlinks may be incomplete." :
          "Concept links are unavailable for this publication."));
      [["Links to other concepts", links.after, links.outgoingAvailable], ["Linked from", links.before, links.incomingComplete]].forEach(function (pair) {
        var count = pair[2] ? String(pair[1].length) : pair[1].length ? pair[1].length + " known" :
          pair[0] === "Linked from" && links.availability === "partial" ? "unknown" : "unavailable";
        var group = element("div", "bp-reading-wiki-related");
        group.appendChild(element("strong", "", pair[0] + " · " + count));
        if (pair[0] === "Links to other concepts" && links.availability === "partial" && !links.outgoingAvailable)
          group.appendChild(element("p", "bp-reading-wiki-notice", "Links from this page could not be read completely."));
        var list = element("ul", "bp-reading-wiki-list"), ui$wikiLimit = 0;
        var more = button("bp-reading-wiki-more", "");
        function appendLinks() {
          var limit = Math.min(pair[1].length, ui$wikiLimit + (ui$wikiLimit ? 20 : 5));
          pair[1].slice(ui$wikiLimit, limit).forEach(function (relatedId) {
            var related = records.get(relatedId), item = element("li"), link = readingLabel("a", "", related.title, related.reading_title);
            link.href = related.route || related.url;
            link.addEventListener("click", function (event) {
              if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
              event.preventDefault(); navigation.navigateNode(relatedId, {history: "push", focus: "reading"});
            });
            item.appendChild(link); list.appendChild(item);
          });
          ui$wikiLimit = limit; more.hidden = limit >= pair[1].length;
          more.textContent = "Show " + Math.min(20, pair[1].length - limit) + " more links";
          refreshReadingLabels(list);
        }
        appendLinks(); more.addEventListener("click", appendLinks);
        group.appendChild(list); group.appendChild(more); disclosure.appendChild(group);
      });
      var peers = dom$readingNeighbors.querySelector(".bp-reading-related-topics");
      if (peers) dom$readingNeighbors.insertBefore(disclosure, peers); else dom$readingNeighbors.appendChild(disclosure);
      refreshReadingLabels(dom$readingNeighbors);
    }
    function renderInspector() {
      var node = ui$selected, id = currentBundle && currentBundle.nodeId || ui$requestedNode || node && recordId(node);
      var previous = dom$readingNeighbors && dom$readingNeighbors.querySelector(".bp-reading-wiki-links");
      renderReaderContext(previous && ui$wikiNode === id ? previous : null); renderWikiContext();
      refreshReadingLabels(dom$readingContext); refreshReadingLabels(dom$readingNeighbors);
    }
    function weightWords(counts) {
      return [counts.statement && plural(counts.statement, "statement dependency", "statement dependencies"),
        counts.proof && plural(counts.proof, "proof dependency", "proof dependencies")].filter(Boolean).join(", ");
    }

    function inventoryNodes() {
      var query = ui$query.toLocaleLowerCase();
      var pool = sharedIndex ? sharedIndex.nodes.filter(function (node) {
        if (ui$relationIds) return ui$relationIds.indexOf(node.id) >= 0;
        if (ui$mode === "sources") return node.navigation === "source";
        if (ui$mode === "regions") return false;
        var scope = currentBundle && currentBundle.scope;
        if (!scope || scope === "roadmap") return node.result === true || node.catalog === "module";
        var current = node, seen = new Set();
        while (current && !seen.has(current.id)) { if (current.id === scope || current.parent === scope) return node.result === true || node.catalog === "module";
          seen.add(current.id); current = records.get(current.parent); }
        return false;
      }) : (calculateActive(), ui$active);
      var nodes = query ? rankMatches(pool, query).map(function (match) { return match.node; }) : pool;
      return nodes.filter(function (node) { return ui$selectedStatuses.has(node.status); });
    }
    // A summary keeps its article's inline code and TeX. The list sets them as the reader does instead of printing their source,
    // and drops a run-in that only repeats the title.
    function summaryLine(node) {
      var text = String(node.summary), line = element("span", "bp-result-summary"), title = node.title || "";
      if (title && text.indexOf(title) === 0 && /^[.:]\s/.test(text.slice(title.length))) text = text.slice(title.length + 1).trim();
      var pattern = /`([^`\n]+)`|\$([^$\n]+)\$|\\\(([\s\S]+?)\\\)/g, last = 0, match;
      while ((match = pattern.exec(text))) {
        if (match.index > last) line.appendChild(element("span", "", text.slice(last, match.index)));
        // A formula waits under its own class, which MathJax does not read, until its row comes near the view.
        line.appendChild(match[1] !== undefined ? element("code", "", match[1]) :
          element("span", "bp-math-pending", "\\(" + (match[2] !== undefined ? match[2] : match[3]) + "\\)"));
        last = pattern.lastIndex;
      }
      if (last < text.length) line.appendChild(element("span", "", text.slice(last)));
      return line;
    }
    // Typesetting a page of 100 summaries at once held the page still for two seconds. A row's formulas become MathJax's
    // to read only as the row comes near the view, after the list has painted, and a pass reads at most six rows, so a
    // pass stays short and a scroll costs a pass or two rather than one per row.
    function typesetRowsInView() {
      if (ui$mathObserver) ui$mathObserver.disconnect();
      ui$mathQueue = [];
      var lines = Array.prototype.filter.call(dom$inventoryList.querySelectorAll(".bp-result-summary"), function (line) { return line.querySelector(".bp-math-pending"); });
      if (!lines.length) return;
      if (typeof IntersectionObserver !== "function") { ui$mathQueue = lines; setTimeout(typesetRevealedRows, 60); return; }
      var observer = ui$mathObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) { if (entry.isIntersecting) { observer.unobserve(entry.target); ui$mathQueue.push(entry.target); } });
        setTimeout(typesetRevealedRows, 60);
      }, {root: dom$inventoryScroll, rootMargin: "200px 0px"});
      lines.forEach(function (line) { observer.observe(line); });
    }
    function typesetRevealedRows() {
      if (ui$mathBusy || !ui$mathQueue.length || !window.AutoformMath || typeof window.AutoformMath.refresh !== "function") return;
      ui$mathQueue.splice(0, 6).forEach(function (line) {
        Array.prototype.forEach.call(line.querySelectorAll(".bp-math-pending"), function (span) { span.className = "arithmatex"; });
      });
      ui$mathBusy = true;
      Promise.resolve(window.AutoformMath.refresh(dom$inventoryList)).catch(function () {}).then(function () { ui$mathBusy = false; setTimeout(typesetRevealedRows, 0); });
    }
    function renderInventory(resetScroll) {
      if (!dom$inventoryList || ui$mode === "graph") return;
      var nodes = inventoryNodes(), visible = nodes.slice(0, ui$listLimit); dom$inventoryList.textContent = "";
      var articles = nodes.some(function (node) { return node.catalog === "module"; });
      // The list says "statements", as Browse and the dashboard do.
      if (navigation && ui$mode !== "sources") dom$inventory.querySelector("h2").textContent = articles ? "Statements & articles" : "Statements";
      dom$inventoryRows = new Map();
      // A page too big for a map says so in words: a disabled button's tooltip never shows on a touch screen.
      if (navigation && !mapAllowed && ui$mode === "list") {
        var topics = data.nodes.filter(function (node) { return node.kind === "scope"; }).length;
        dom$inventoryList.appendChild(element("li", "bp-dag-inventory-note", topics > 1 ? (currentBundle && currentBundle.title || "This page") + " has " +
          num(topics) + " topics, too many to draw as one map. Open one from Topics, or search." : "Too many statements to draw as one map. Search to find one."));
      }
      visible.forEach(function (node) {
        var item = element("li"), row = button("bp-dag-inventory-row", "");
        row.dataset.inventoryNodeId = node.id;
        if (ui$selected && node.id === ui$selected.id) row.setAttribute("aria-current", "true");
        dom$inventoryRows.set(node.id, row);
        row.appendChild(element("span", "bp-dag-inventory-title", node.title));
        var where = navigation && placeOf(node, currentBundle && currentBundle.scope) || node.area;
        row.appendChild(element("span", "bp-dag-inventory-meta", statusLabel(node.status) + (where ? " · " + where : ""))).title = (palette[node.status] || {}).meaning || "";
        if (navigation && node.summary) row.appendChild(summaryLine(node));
        row.addEventListener("click", function () { activateNode(node, true); }); item.appendChild(row); dom$inventoryList.appendChild(item);
      });
      if (navigation) typesetRowsInView();
      // A count, or "N of M" while a page or a filter shows fewer.
      var shown = visible.length < nodes.length ? visible : nodes, total = nodes;
      dom$inventorySummary.textContent = shown.length < total.length ? num(shown.length) + " of " +
        num(total.length) : plural(total.length, ui$mode === "sources" ? "source" : articles ? "article" : "statement",
          ui$mode === "sources" ? "sources" : articles ? "articles" : "statements");
      if (ui$query && host.getAttribute("data-catalog-src")) {
        dom$inventorySummary.appendChild(document.createTextNode(" · ")); dom$inventorySummary.appendChild(dom$wholeSearch);
      }
      dom$loadMore.hidden = visible.length >= nodes.length;
      dom$loadMore.textContent = visible.length < nodes.length ? "Show " + Math.min(LIST_PAGE, nodes.length - visible.length) + " more" : "";
      var current = ui$selected && dom$inventoryRows.get(ui$selected.id);
      if (resetScroll && dom$inventoryScroll) dom$inventoryScroll.scrollTop = 0;
      else if (current) current.scrollIntoView({block: "nearest"});
    }
    function writeLocation(method) {
      if (navigation.checkpoint) navigation.checkpoint(method);
    }

    syncControls();
    return {ready: readingReady, applyRoute: applyRoute, useIndex: useIndex, useWikiLinks: useWikiLinks, captureState: captureState, restoreState: restoreState, showError: showError,
      dispose: function () { alive = false; ui$wikiProjectionTicket++; ui$wikiProjectionCache = new WeakMap(); clearTimeout(checkpointTimer); cancelAnimationFrame(frame); cleanup.forEach(function (close) { close(); });
        observers.forEach(function (observer) { observer.disconnect(); }); if (navigation) navigation.dispose(); }};
  }

  function boot() {
    clients.forEach(function (client, host) { if (!host.isConnected) { client.dispose(); clients.delete(host); } });
    document.querySelectorAll(".bp-dag-viewer[data-graph-src]").forEach(start);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
  if (window.document$ && window.document$.subscribe) window.document$.subscribe(boot);
})();
