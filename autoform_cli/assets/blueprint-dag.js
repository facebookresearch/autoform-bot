/* Hand-maintained explorer source. Rebuild blueprint-explorer.min.js after edits. */
(function () {
  "use strict";
  if (window.__AUTOFORM_DAG_VIEWER_V2__) return;
  window.__AUTOFORM_DAG_VIEWER_V2__ = true;

  var LIST_PAGE = 100;
  var SEARCH_PAGE = 50, hostSequence = 0;
  var clients = new Map();
  var clamp = function (value, low, high) { return Math.max(low, Math.min(high, value)); };
  var plural = function (count, one, many) { return count.toLocaleString() + " " + (count === 1 ? one : many); };
  var num = function (value) { return value.toLocaleString(); };
  var pressed = function (control, value) { control.setAttribute("aria-pressed", String(value)); };

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
  // provider HTML or replace an interactive link.
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

  // A tab title, a place line or an accessible name cannot typeset, so a formula there reads as tex-plain.js makes it.
  // With parts for the same title, its code keeps its dollars; otherwise only formulas in \( \) are reduced, so a
  // literal $ stays.
  function plainTitle(text, label) {
    var parts = label && label.format === "inline-parts-v1" && label.source === text && Array.isArray(label.parts) && label.parts;
    if (!parts) return text.replace(/\\\(([\s\S]*?)\\\)/g, function (_m, tex) { return window.AutoformTexPlain.math(tex); });
    return parts.map(function (part) {
      return part[0] === "math" ? window.AutoformTexPlain.math(String(part[1]).slice(2, -2)) : String(part[1]);
    }).join("");
  }

  // A title attribute shows text as typed, so a label holding a formula gets
  // no tooltip: it would show the TeX source, and the label itself is typeset.
  function tooltip(node, text) {
    if (text && !node.querySelector(".arithmatex")) node.title = text;
  }

  function refreshReadingLabels(root) {
    if (root && window.AutoformMath && typeof window.AutoformMath.refresh === "function") {
      try { Promise.resolve(window.AutoformMath.refresh(root)).catch(function () {}); } catch (_error) {}
    }
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
    // The whole-project map draws in the stage. Its data sits beside the search index, whose address is relative to the
    // page as loaded: the router may move the address to the canonical page before the first commit.
    var catalog = host.getAttribute("data-catalog-src");
    if (catalog) host.dataset.wholeMap = new URL(catalog, location.href).href;
    var navigation = window.AutoformNavigation.create({
      host: host,
      onCommit: function (bundle) {
        if (!host.isConnected) { navigation.dispose(); return; }
        // The book takes the whole page, not Material's content column. The
        // error path below restores native reading.
        document.body.classList.add("bp-dag-app-page");
        var ready;
        if (!client) {
          client = mount(host, bundle.graph, instance, bundle, navigation); clients.set(host, client); ready = client.ready;
          if (catalog) {
            var map = element("script"); map.src = new URL("../javascripts/project-map.js", host.dataset.wholeMap).href;
            // A map that cannot load, or that fails before it marks its host set up, leaves the stage to a notice; the
            // outline, search and lists still work.
            map.onload = map.onerror = function () { if (!host.querySelector(".bp-whole-map[data-pm-ready]")) client.mapUnavailable(); };
            // The map reads its files through this router, which keeps no copy of them; navigating never aborts them.
            host.autoformFetchJSON = navigation.fetchJSON;
            document.head.appendChild(map);
            host.addEventListener("autoform-map-select", function (event) { navigation.navigateNode(event.detail.id, event.detail); });
          }
        }
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

  function outlineProjection(nodes, currentScope, currentNode, limit, byId) {
    var records = byId || new Map(nodes.map(function (node) { return [node.id, node]; })), children = new Map(), parents = new Map();
    var sourced = new Set();
    function parentOf(node) {
      if (parents.has(node.id)) return parents.get(node.id);
      var parent = records.get(node.parent), seen = new Set([node.id]);
      while (parent && !seen.has(parent.id) && ["transparent", "source"].indexOf(parent.navigation) >= 0) {
        if (parent.navigation === "source") sourced.add(node.id);
        seen.add(parent.id); parent = records.get(parent.parent);
      }
      var id = parent && !seen.has(parent.id) ? parent.id : null; parents.set(node.id, id); return id;
    }
    var root = records.get("roadmap") || nodes.find(function (node) { return node.scope === node.id && !records.has(node.parent); });
    var chain = new Set(), cursor = records.get(currentNode) || records.get(currentScope);
    while (cursor && !chain.has(cursor.id)) { chain.add(cursor.id); cursor = records.get(cursor.parent); }
    nodes.forEach(function (node) {
      if (["transparent", "source"].indexOf(node.navigation) >= 0) return;
      var parent = parentOf(node), top = root ? parent === root.id : parent === null;
      if (node.scope !== node.id && !top && node.id !== currentNode) return;
      var bucket = children.get(parent) || []; bucket.push(node); children.set(parent, bucket);
    });
    var entries = [], seen = new Set(), headings = new Set(), pinned = null, deepest = 3;
    function visit(node, depth) {
      if (seen.has(node.id) || root && node.id === root.id) return;
      seen.add(node.id); entries.push({node: node, depth: Math.min(depth, deepest), collection: node.navigation === "collection"});
      if (chain.has(node.id) || node.navigation === "collection") walk(node.id, depth + 1);
    }
    function walk(id, depth) { (children.get(id) || []).forEach(function (node) { visit(node, depth); }); }
    // As the map groups chapters into sections (project_map.py), Contents heads each run of an area's chapters with the
    // area's name, once the chapters name two areas and for an area of two chapters or more. Only the topics written at
    // the top count: a statement's area only labels it, a collection keeps its own subhead, and the map keeps a source
    // track whole, so the topics lifted out of one stay unheaded. A chapter without an area keeps its place unheaded,
    // where the map gathers two or more of them under "Other", so every heading is a name the project wrote.
    var chapters = children.get(root ? root.id : null) || [], sizes = new Map(), heading = null;
    var areas = chapters.map(function (node) {
      var chapter = node.scope === node.id && node.navigation !== "collection" && !sourced.has(node.id);
      return chapter && node.area ? String(node.area) : "";
    });
    areas.forEach(function (area) { if (area) sizes.set(area, (sizes.get(area) || 0) + 1); });
    var headed = function (area) { return sizes.size >= 2 && sizes.get(area) >= 2; };
    // A heading takes a level, so the outline goes one level deeper under it.
    if (areas.some(headed)) deepest = 4;
    chapters.forEach(function (node, index) {
      var area = headed(areas[index]) ? areas[index] : "";
      if (!area) heading = null;
      else if (!heading || heading.node.title !== area) {
        heading = {node: {id: "region:" + area, title: area}, depth: 0, collection: true};
        headings.add(heading); entries.push(heading);
      }
      // However short the list, the current chapter keeps its heading.
      if (area && chain.has(node.id)) pinned = heading;
      visit(node, area ? 1 : 0);
    });
    var required = new Set(entries.filter(function (entry) { return chain.has(entry.node.id) || entry === pinned; }));
    var remaining = Math.max(0, (limit || 120) - required.size);
    var shown = entries.filter(function (entry) { return required.has(entry) || remaining-- > 0; });
    // A heading the limit parted from its chapters waits for them under "Show more topics".
    return {root: root || null, total: entries.length, entries: shown.filter(function (entry, index) {
      return !headings.has(entry) || (shown[index + 1] || {}).depth > 0;
    })};
  }

  function mount(host, data, instance, firstBundle, navigation) {
    if (!navigation || !firstBundle || !firstBundle.readingSource) throw new Error("A complete mathematical page and navigation are required");
    if (!data || data.schema !== "autoform-dag-view/v2" || !data.palette ||
        !Array.isArray(data.nodes) || !Array.isArray(data.edges)) throw new Error("unsupported graph payload");
    var sharedIndex = null, records = new Map(), currentBundle = firstBundle;
    var ui$wikiLinks = null, ui$wikiBefore = new Map(), ui$wikiAfter = new Map(), ui$wikiRequested = false;
    var ui$wikiGeneration = 0, ui$wikiPending = null, ui$wikiWork = null, ui$wikiRendered = null, ui$wikiNode = null;
    var alive = true, cleanup = [], observers = [], resizeFrame = null, readingReady = Promise.resolve(), restoration = 0, checkpointTimer = 0, restoring = false;
    host.textContent = "";
    host.classList.remove("bp-dag-enhancing");
    var palette, byId, statusCounts, presentStatuses;
    function recordId(node) {
      return records.get(node.id) === node || ["scope", "boundary"].indexOf(node.kind) < 0 ? node.id : node.id.replace(/^(scope|boundary):/, "");
    }
    // What the page lists: an overview payload marks the entries its overview leaves out, and a source track's scope is
    // a source text, not a topic.
    function listed(node) {
      var record = records.get(recordId(node));
      return node.map !== false && !(node.kind === "scope" && record && record.navigation === "source");
    }
    // A statement is selected from the index, since its page may carry another view's payload (one placed directly
    // under the roadmap gets the project's); a topic is never the selection. Before the index, the payload decides.
    function selection(id) {
      var record = id && records.get(id);
      return record ? (record.scope !== record.id ? record : null) : id && byId.get(id) || null;
    }
    function adoptGraph(payload) {
    data = graphAt(payload, currentBundle && currentBundle.dataURL);
    data.nodes = data.nodes.filter(listed);
    palette = data.palette || {}; byId = new Map();
    statusCounts = new Map(); presentStatuses = [];
    data.nodes.forEach(function (node, index) {
      node._index = index;
      byId.set(node.id, node);
      statusCounts.set(node.status, (statusCounts.get(node.status) || 0) + 1);
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
    }
    adoptGraph(data);
    // Renderer-private locals compress independently of public JSON/history field names.
    var ui$listLimit = LIST_PAGE, ui$mode = "graph",
      ui$query = "", ui$searchLimit = 12, ui$selected = null, ui$searchCache = [], ui$searchCacheQuery = null,
      ui$searchError = null, ui$searchLoading = false, ui$searchNodes = data.nodes, ui$searchPromise = null, ui$mathJob = null,
      ui$selectedStatuses = new Set(presentStatuses), ui$reading = Boolean(firstBundle && firstBundle.readingSource),
      ui$overviewOnly = false, ui$filtersTouched = false, ui$relationIds = null,
      ui$requestedNode = firstBundle && firstBundle.nodeId || null, ui$outlineLimit = 120;
    // A project that tracks no Lean (render marks its pages) has no Lean progress to show or Lean names to search for.
    var tracksLean = !host.closest('[data-tracks-lean="false"]');
    var dom$announcement, dom$body, dom$breadcrumb, dom$browse, dom$browseToggle, dom$browsePopover,
      dom$error, dom$filterPopover, dom$filterSummary, dom$filters, dom$graphMode, dom$hiddenButton,
      dom$inspector, dom$inspectorInner, dom$inventory, dom$inventoryList, dom$inventoryRows,
      dom$inventoryScroll, dom$inventorySummary, dom$listMode, dom$loadMore,
      dom$navigation, dom$navigationInner, dom$navigationNodes, dom$outlineToggle, dom$pageHeading,
      dom$readingBody, dom$readingContext, dom$search, dom$searchResults,
      dom$searchWrap, dom$sheetHandle, dom$sourcesMode, dom$stage, dom$statusFieldset, dom$statusInputs,
      dom$wholeSearch;
    host.classList.add("bp-dag-integrated");

    buildShell();
    bindEvents();
    bindLeanCards();
    bindPanes();
    // The router has already validated its index, so the first route, a
    // direct theorem entry included, reads its records. Only the route
    // renders the first view, after projecting with them.
    if (navigation && navigation.getIndex) adoptIndex(navigation.getIndex());
    readingReady = applyRoute(firstBundle);

    function globalListen(target, type, handler, options) {
      target.addEventListener(type, handler, options); cleanup.push(function () { target.removeEventListener(type, handler, options); });
    }
    function remember() {
      if (restoring) return;
      clearTimeout(checkpointTimer); checkpointTimer = setTimeout(function () { if (alive && !restoring) navigation.checkpoint(); }, 80);
    }

    function captureState() {
      var active = document.activeElement, key = active === dom$search ? "search" : dom$readingBody && dom$readingBody.contains(active) ? "reading" :
        dom$stage.contains(active) ? "map" : active && active.dataset.inventoryNodeId;
      return {node: ui$selected && ui$selected.id, requestedNode: ui$requestedNode, mode: ui$mode,
        statuses: Array.from(ui$selectedStatuses), query: ui$query, readingOpen: host.classList.contains("bp-dag-sheet-open"), focus: key,
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
      ui$filtersTouched = true;
      ui$requestedNode = saved.requestedNode === undefined ? saved.node || null : saved.requestedNode;
      updateProjection(); ui$selected = selection(saved.node);
      ui$selectedStatuses = new Set(saved.statuses || presentStatuses);
      ui$mode = ["graph", "list", "sources"].indexOf(saved.mode) >= 0 ? saved.mode : "graph";
      ui$listLimit = Math.max(LIST_PAGE, Number(saved.listLimit) || LIST_PAGE); ui$relationIds = saved.relationIds || null;
      ui$outlineLimit = Math.max(120, Number(saved.outlineLimit) || 120); renderNavigation();
      ui$query = saved.query || ""; dom$search.value = ui$query;
      host.classList.toggle("bp-dag-outline-open", Boolean(saved.outlineOpen));
      if (dom$outlineToggle) dom$outlineToggle.setAttribute("aria-expanded", String(Boolean(saved.outlineOpen)));
      setSheet(saved.readingOpen !== false && 1, ui$selected); refresh(false); resize();
      return readingReady.catch(function () {}).then(function () { return new Promise(function (resolve) { requestAnimationFrame(function () {
        if (!alive || ticket !== restoration || (isCurrent && !isCurrent())) { if (ticket === restoration) restoring = false; resolve(); return; }
        dom$navigation.scrollTop = saved.outlineScroll || 0;
        if (dom$readingBody) dom$readingBody.scrollTop = saved.readingScroll || 0;
        dom$inventoryScroll.scrollTop = saved.inventoryScroll || 0;
        var chosen = saved.focus === "search" ? dom$search : saved.focus === "reading" ? dom$readingBody :
          ui$mode === "graph" ? saved.focus && mapControl() : dom$inventoryRows.get(saved.focus);
        if (chosen === dom$readingBody) chosen.tabIndex = -1;
        if (chosen) chosen.focus({preventScroll: true});
        restoring = false; resolve();
      }); }); });
    }
    function showError(error) {
      dom$announcement.textContent = "The requested view could not be loaded. " + (error.message || error);
      dom$error.textContent = "Could not open this view. " + (error.message || error);
      var href = safeHref(error.url);
      if (href && /(?:\/|\.html?)$/.test(new URL(href, window.location.href).pathname)) {
        dom$error.appendChild(document.createTextNode(" "));
        var fallback = element("a", "", "Open complete article in a new tab"); fallback.href = href;
        fallback.target = "_blank"; fallback.rel = "noopener";
        dom$error.appendChild(fallback);
      }
      dom$error.hidden = false;
    }
    function adoptIndex(index) {
      if (!index || sharedIndex === index) return false;
      sharedIndex = index; records = new Map(); dom$search.removeAttribute("aria-busy");
      index.nodes.forEach(function (node, i) { node._index = i; records.set(node.id, node); });
      ui$searchNodes = index.nodes; ui$searchCacheQuery = null;
      requestWikiLinks();
      return true;
    }
    function useIndex(index) {
      if (!adoptIndex(index)) return;
      if (currentBundle) updateProjection();
      renderNavigation(); refresh(false);
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
      var before = new Map(), after = new Map(), ui$wikiPosition = 0;
      var references = ["complete", "partial"].indexOf(loaded.availability) >= 0 ? loaded.references || [] : [];
      var total = references.length;
      function build() {
        if (!alive || ticket !== ui$wikiGeneration) return;
        var end = Math.min(total, ui$wikiPosition + 1024);
        for (; ui$wikiPosition < end; ui$wikiPosition++) {
          var pair = references[ui$wikiPosition], source = pair[0], target = pair[1];
          if (source === target || !records.has(source) || !records.has(target)) continue;
          if (!after.has(source)) after.set(source, new Set());
          if (!before.has(target)) before.set(target, new Set());
          after.get(source).add(target); before.get(target).add(source);
        }
        if (ui$wikiPosition < total) return new Promise(function (resolve) { setTimeout(resolve, 0); }).then(build);
        if (!alive || ticket !== ui$wikiGeneration) return;
        ui$wikiLinks = loaded; ui$wikiBefore = before; ui$wikiAfter = after;
        ui$wikiPending = null; ui$wikiWork = null; renderWikiContext();
      }
      ui$wikiWork = build(); return ui$wikiWork;
    }
    function wikiNeighbors(id) {
      return {before: Array.from(ui$wikiBefore.get(id) || []), after: Array.from(ui$wikiAfter.get(id) || []),
        availability: ui$wikiLinks && ui$wikiLinks.availability || "unavailable"};
    }
    function applyRoute(bundle) {
      var snapshot = snapshotForRoute(bundle.snapshot, bundle);
      if (snapshot !== bundle.snapshot) bundle = Object.assign({}, bundle, {snapshot: snapshot});
      var isCurrent = bundle.isCurrent;
      if (!alive || (isCurrent && !isCurrent())) return;
      restoration++; restoring = false; clearTimeout(checkpointTimer); currentBundle = bundle; ui$requestedNode = bundle.nodeId || null;
      document.title = plainTitle(bundle.title, (records.get(bundle.nodeId) || {}).reading_title); dom$error.hidden = true;
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
      ui$mode = "graph";
      ui$selectedStatuses = new Set(presentStatuses);
      var legacy = bundle.parameters || {};
      if (legacy.view === "list") ui$mode = "list";
      if (legacy.status && tracksLean) ui$selectedStatuses = new Set(legacy.status.split(",").filter(function (key) { return palette[key]; }));
      if (ui$selected) ui$selectedStatuses.add(ui$selected.status);
      // Decide only after the outline has proved and hidden duplicated child
      // indexes. A canonical scope ID alone is not a request to read a result.
      renderNavigation();
      var record = records.get(bundle.nodeId), statement = Boolean(bundle.anchor || record && record.result);
      // A page whose reader only lists its children leaves the stage to the map, unless a statement or the reading is asked for.
      ui$overviewOnly = Boolean(!statement && (!bundle.focus || bundle.mapFirst) && navigation.readerIsOverview && navigation.readerIsOverview(dom$readingBody));
      host.classList.toggle("bp-dag-overview-only", ui$overviewOnly);
      // A phone opens a topic, a chapter or Home, on the map, and a statement in the reader.
      var place = record || !bundle.nodeId && records.get(bundle.scope);
      var topic = Boolean(!statement && place && place.scope === place.id && place.navigation !== "source" && place.catalog !== "module");
      var mapRequested = Boolean(bundle.mapFirst && (ui$overviewOnly || sheetLayout() && topic));
      var readingRequested = Boolean(bundle.focus && !mapRequested || statement);
      setSheet(bundle.snapshot ? bundle.snapshot.readingOpen !== false && 1 : sheetLayout() && (topic && !bundle.focus || mapRequested) ? false : readingRequested ||
        !ui$overviewOnly && (!sheetLayout() || Boolean(bundle.nodeId)), ui$selected);
      refresh(true); resize(); host.dataset.mapStart = bundle.nodeId || bundle.scope || "";
      if (navigation) navigation.loadIndex().catch(showError);
      readingReady = readingReady.catch(function () {}).then(function () { return new Promise(function (resolve) { requestAnimationFrame(function () {
        if (!alive || (isCurrent && !isCurrent())) { resolve(); return; }
        // A statement's anchor is on its card's heading; the whole card is the current reading.
        var anchored = bundle.anchor && document.getElementById(bundle.anchor);
        var target = anchored && (anchored.closest(".bp-thmwrapper") || anchored);
        if (target && dom$readingBody.contains(target)) {
          target.classList.add("bp-reading-current"); target.setAttribute("aria-current", "true");
          // The statement's own relations list replaces a separate strip, so it opens with the selection.
          var relations = target.querySelector(".bp-dependencies"); if (relations) relations.open = true;
          if (!bundle.snapshot) dom$readingBody.scrollTop += target.getBoundingClientRect().top - dom$readingBody.getBoundingClientRect().top - 24;
        }
        // A hidden reader takes no focus; the map does, as when the map is asked for.
        var readerShown = !host.classList.contains("bp-dag-reading-hidden");
        if (bundle.focus && !mapRequested && readerShown && dom$readingBody && !bundle.snapshot) {
          var readingTarget = dom$readingBody.querySelector(".bp-reading-current .bp-thmheading") ||
            dom$readingBody.querySelector(".bp-reading-current") || dom$readingBody.querySelector("h1");
          if (readingTarget) { readingTarget.tabIndex = -1; readingTarget.focus({preventScroll: true}); }
        } else if (bundle.focus && (mapRequested || !readerShown) && !bundle.snapshot) {
          var control = mapControl(); if (control) control.focus({preventScroll: true});
          // A pick made while the map is missing or not yet drawn leaves focus on the filter, as Back to map does; a page
          // just opened leaves focus where it is.
          if (!mapRequested && bundle.reason !== "start" && (!control || document.activeElement !== control)) {
            (tracksLean ? dom$filterSummary : dom$search).focus({preventScroll: true});
          }
        }
        resolve();
      }); }); });
      return readingReady;
    }
    function routeTrail(bundle) {
      var home = navigation && navigation.getHomeURL && navigation.getHomeURL();
      var atHome = false;
      if (home && bundle.url && !bundle.nodeId) {
        var at = new URL(bundle.url), start = new URL(home);
        atHome = at.origin === start.origin && at.pathname === start.pathname && at.search === start.search;
      }
      if (atHome) return [{label: "Home"}];
      var entries = [], record = records.get(bundle.nodeId), pending = records.get(bundle.scope), seen = new Set();
      if (!pending) entries = (bundle.graph.view && bundle.graph.view.breadcrumbs || []).slice();
      while (pending && !seen.has(pending.id)) {
        seen.add(pending.id);
        // A collection never stands between crumbs, but its own page ends the trail with it, as the map header does.
        var own = pending.id === bundle.scope && pending.navigation === "collection";
        if (own || ["collection", "transparent"].indexOf(pending.navigation) < 0) entries.unshift({label: pending.title, reading_title: pending.reading_title,
          url: pending.id === bundle.scope && (!record || record.id === pending.id) ? null : pending.route || pending.url});
        pending = records.get(semanticParent(pending));
      }
      if (record && record.scope !== record.id) entries.push({label: record.title, reading_title: record.reading_title});
      if (home) entries.unshift({label: "Home", url: home});
      return entries;
    }
    function updateProjection() {
      adoptGraph(currentBundle.graph);
      ui$selected = selection(ui$requestedNode);
      if (!ui$filtersTouched) ui$selectedStatuses = new Set(presentStatuses);
      if (dom$statusFieldset) renderStatusFields();
      renderBreadcrumb();
    }
    // Where a result or topic lives: the nearest topics above it, short of the root or of the page a list belongs to,
    // so rows that share a title, such as ATLAS's 93 "Conjecture"s, tell themselves apart.
    function placeOf(node, stop) {
      var record = records.get(recordId(node)) || node, names = [], seen = new Set();
      var pending = records.get(record.scope === record.id ? semanticParent(record) : record.scope);
      while (pending && pending.id !== stop && !seen.has(pending.id) && semanticParent(pending)) {
        seen.add(pending.id); names.unshift(plainTitle(pending.title, pending.reading_title)); pending = records.get(semanticParent(pending));
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
    function bindNodeLink(link, id, settings) {
      link.addEventListener("click", function (event) {
        if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); navigation.navigateNode(id, settings);
      });
    }
    function renderOutline() {
      var key = currentBundle && currentBundle.scope, tree = outlineProjection(sharedIndex.nodes, key,
        currentBundle && currentBundle.nodeId, ui$outlineLimit, records);
      var list = element("ul", "bp-dag-navigation-list");
      tree.entries.forEach(function (entry) {
        var node = entry.node, item = element("li", "bp-outline-depth-" + entry.depth);
        if (entry.collection) { item.appendChild(readingLabel("span", "bp-outline-collection", node.title, node.reading_title)); list.appendChild(item); return; }
        var link = readingLabel("a", "bp-dag-navigation-link", node.title, node.reading_title);
        link.href = node.route || node.url; tooltip(link, node.title); link.dataset.navigationNodeId = node.id;
        if (node.id === (currentBundle && currentBundle.nodeId || key)) link.setAttribute("aria-current", "page");
        item.appendChild(link); list.appendChild(item);
        bindNodeLink(link, node.id, {history: "push", focus: true});
      });
      dom$navigationInner.appendChild(element("h2", "bp-outline-label", "Contents")); dom$navigationInner.appendChild(list);
      if (tree.entries.length < tree.total) {
        var more = button("bp-dag-navigation-more", "Show more topics");
        more.addEventListener("click", function () {
          var retained = new Set(tree.entries.map(function (entry) { return entry.node.id; }));
          ui$outlineLimit += 50; renderNavigation();
          var first = Array.from(dom$navigationInner.querySelectorAll("[data-navigation-node-id]")).find(function (link) {
            return !retained.has(link.dataset.navigationNodeId);
          }) || dom$navigationInner.querySelector('[aria-current="page"]');
          if (first) first.focus(); remember();
        }); dom$navigationInner.appendChild(more);
      }
      if (navigation.declutterReader) navigation.declutterReader(dom$readingBody, tree.entries.filter(function (entry) {
        return !entry.collection && entry.node.scope === entry.node.id;
      }).map(function (entry) { return entry.node.id; }));
    }
    function renderReaderContext() {
      if (!dom$readingContext) return;
      dom$readingContext.replaceChildren();
      var node = ui$selected, record = records.get(currentBundle && currentBundle.nodeId || ui$requestedNode || (node && node.id)), scope = record && records.get(record.scope);
      if (scope && record.id !== scope.id) {
        var around = readingLabel("a", "bp-reading-around", scope.title, scope.reading_title, "← "); around.href = scope.route || scope.url;
        bindNodeLink(around, scope.id, {history: "push", focus: "reading"}); dom$readingContext.appendChild(around);
      }
      syncSheetAccessibility();
    }

    function buildShell() {
      var head = element("header", "bp-dag-head");
      var context = element("div", "bp-dag-context");
      var crumbs = dom$breadcrumb = element("nav", "bp-dag-breadcrumb");
      crumbs.setAttribute("aria-label", "Breadcrumb");
      renderBreadcrumb(); context.appendChild(crumbs);
      head.appendChild(context);
      // The explorer hides Material's header and its sun or moon, so this clicks that toggle: the one label Material leaves
      // unhidden, which points at the next palette entry, so a three-entry automatic palette steps on as Material's own does.
      // A palette list without toggle keys draws the form but no label, so it gets no switch.
      var schemes = document.querySelector("[data-md-component=palette]");
      if (schemes && schemes.querySelector("label")) {
        var themeLabel = "Switch light or dark theme", flip = button("bp-dag-button bp-dag-theme", "", themeLabel), svg = "http://www.w3.org/2000/svg";
        // A drawn half-disc in the text colour, rather than a half-circle glyph that each fallback font draws at its own size.
        var icon = document.createElementNS(svg, "svg"); icon.setAttribute("viewBox", "0 0 20 20"); icon.setAttribute("aria-hidden", "true");
        [["circle", {cx: 10, cy: 10, r: 7.25, fill: "none", stroke: "currentColor", "stroke-width": 1.5}], ["path", {d: "M10 2.75a7.25 7.25 0 0 1 0 14.5z", fill: "currentColor"}]].forEach(function (shape) {
          var part = document.createElementNS(svg, shape[0]); Object.keys(shape[1]).forEach(function (key) { part.setAttribute(key, shape[1][key]); }); icon.appendChild(part);
        });
        flip.appendChild(icon);
        flip.title = themeLabel; context.appendChild(flip);
        flip.onclick = function () { var next = schemes.querySelector("label:not([hidden])"); if (next) next.click(); };
      }

      var toolbar = element("div", "bp-dag-toolbar");
      toolbar.setAttribute("role", "group"); toolbar.setAttribute("aria-label", "Dependency explorer controls");
      // Its name is its visible word, so speech input can say "Contents"; aria-expanded gives its state. It follows search, which a
      // phone puts on a row of its own, so the tab order runs along the rows instead of from Contents up to search and back down.
      dom$outlineToggle = button("bp-dag-button bp-dag-outline-toggle", "Contents");
      dom$outlineToggle.setAttribute("aria-expanded", "false");
      var searchWrap = dom$searchWrap = element("div", "bp-dag-search-wrap");
      dom$search = element("input", "bp-dag-search");
      dom$search.type = "search"; dom$search.placeholder = tracksLean ? "Search titles and Lean names" : "Search titles";
      dom$search.setAttribute("aria-label", dom$search.placeholder);
      dom$search.setAttribute("aria-controls", "bp-dag-search-results-" + instance);
      dom$searchResults = element("div", "bp-dag-search-results"); dom$searchResults.id = "bp-dag-search-results-" + instance;
      dom$searchResults.setAttribute("role", "region"); dom$searchResults.setAttribute("aria-label", "Search results");
      dom$searchResults.hidden = true;
      searchWrap.append(dom$search, dom$searchResults); toolbar.append(searchWrap, dom$outlineToggle);

      dom$filters = element("div", "bp-dag-filters");
      dom$filterSummary = button("bp-dag-filter-summary", "Filter: all statuses"); dom$filterSummary.setAttribute("aria-expanded", "false");
      dom$filterPopover = element("div", "bp-dag-filter-popover"); dom$filterPopover.hidden = true;
      dom$filterPopover.id = "bp-dag-filters-" + instance; dom$filterSummary.setAttribute("aria-controls", dom$filterPopover.id);
      var fieldset = dom$statusFieldset = element("fieldset"); dom$statusInputs = new Map();
      renderStatusFields();
      dom$filterPopover.append(fieldset);
      dom$filters.append(dom$filterSummary, dom$filterPopover); toolbar.appendChild(dom$filters);

      dom$graphMode = button("bp-dag-button", "Back to map"); dom$graphMode.hidden = true;
      toolbar.appendChild(dom$graphMode);
      dom$browse = element("div", "bp-dag-browse");
      dom$browseToggle = button("bp-dag-button", "Browse", "Browse statements or source texts");
      dom$browseToggle.setAttribute("aria-expanded", "false");
      dom$browsePopover = element("div", "bp-dag-browse-popover"); dom$browsePopover.hidden = true;
      dom$browsePopover.id = "bp-dag-browse-" + instance;
      dom$browsePopover.setAttribute("role", "group"); dom$browsePopover.setAttribute("aria-label", "Browse library contents");
      dom$browseToggle.setAttribute("aria-controls", dom$browsePopover.id);
      dom$listMode = button("bp-dag-button", "Statements in this topic");
      dom$sourcesMode = button("bp-dag-button", "Source texts");
      dom$browsePopover.appendChild(dom$listMode);
      dom$browse.appendChild(dom$browseToggle); dom$browse.appendChild(dom$browsePopover); toolbar.appendChild(dom$browse);

      dom$hiddenButton = button("bp-dag-hidden-button", ""); toolbar.appendChild(dom$hiddenButton);
      head.appendChild(toolbar);
      dom$error = element("div", "bp-dag-error"); dom$error.setAttribute("role", "alert"); dom$error.hidden = true; head.appendChild(dom$error);
      host.appendChild(head);

      var body = dom$body = element("div", "bp-dag-body"), main = element("div", "bp-dag-main");
      dom$navigation = element("aside", "bp-dag-navigation");
      dom$navigation.setAttribute("aria-label", "Contents");
      dom$navigationInner = element("div", "bp-dag-navigation-inner");
      dom$navigation.appendChild(dom$navigationInner); body.appendChild(dom$navigation);
      renderNavigation();
      // The region says what it is in the page's own word; the whole map inside describes its own keys.
      dom$stage = element("section", "bp-dag-stage"); dom$stage.setAttribute("role", "region"); dom$stage.setAttribute("aria-label", "Map");
      main.appendChild(dom$stage);

      dom$inventory = element("section", "bp-dag-inventory"); dom$inventory.hidden = true;
      var inventoryHead = element("div", "bp-dag-inventory-head"), inventoryHeading = inventoryHead.appendChild(element("h2", "", "Contents"));
      // The list is named by its heading, "Statements" or "Source texts", not a fixed label.
      inventoryHeading.id = "bp-dag-inventory-heading-" + instance; dom$inventory.setAttribute("aria-labelledby", inventoryHeading.id);
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
      navigation.bindReader(dom$readingBody);
      // A full phone reader hides the toolbar, so a pill at the bottom drops it back to its peek.
      var mapButton = button("bp-reading-map", "Map", "Show the map"); dom$inspector.appendChild(mapButton);
      mapButton.addEventListener("click", function () { setSheet(false, ui$selected); dom$sheetHandle.focus(); writeLocation("replace"); });
      dom$announcement = element("p", "bp-dag-sr-only"); dom$announcement.setAttribute("aria-live", "polite"); host.appendChild(dom$announcement);
    }

    function trailList(entries, className, whole) {
      var list = element("ol", className);
      // A folded trail keeps its ends, first / … / parent / current, and the "…" names the ancestors it stands for.
      if (!className && !whole && entries.length > 3) entries = [entries[0], {label: "…", hidden: entries.slice(1, -2).map(function (entry) { return plainTitle(entry.label, entry.reading_title); }).join(" / ")}].concat(entries.slice(-2));
      entries.forEach(function (entry, index) {
        var href = safeHref(entry.url), item = href ? element("li") : readingLabel("li", "", entry.label, entry.reading_title);
        if (href) {
          var link = readingLabel("a", !className && index === 0 ? "bp-dag-book" : "", entry.label, entry.reading_title); link.href = href; tooltip(link, entry.label);
          link.addEventListener("click", function (event) {
            if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey ||
                new URL(href).origin !== window.location.origin) return;
            event.preventDefault();
            navigation.navigate(href, {history: "push", focus: true,
              mapFirst: href !== (navigation.getHomeURL && navigation.getHomeURL())});
          });
          item.appendChild(link);
        }
        else if (entry.hidden) item.appendChild(element("span", "bp-dag-sr-only", entry.hidden));
        else if (!className && index === entries.length - 1) item.setAttribute("aria-current", "page");
        // Set once the item holds its label, so a formula in its link leaves both untitled.
        if (entry.hidden) item.title = entry.hidden; else tooltip(item, entry.label);
        list.appendChild(item);
      });
      return list;
    }
    function renderBreadcrumb() {
      if (!dom$breadcrumb || !currentBundle) return;
      var entries = routeTrail(currentBundle); if (!entries.length) entries = [{label: data.title || "Dependencies"}];
      var active = document.activeElement, retained = dom$breadcrumb.contains(active) && active.href;
      // A deep trail shows every crumb, which give way to ellipses as the row narrows, and folds its middle into "…" only
      // when even that cannot fit: a fold that hid the chapter of every Hartshorne result left most of a wide row empty.
      dom$breadcrumb.replaceChildren(trailList(entries, null, !sheetLayout()));
      if (!sheetLayout() && entries.length > 3 && dom$breadcrumb.scrollWidth > dom$breadcrumb.clientWidth + 1) dom$breadcrumb.replaceChildren(trailList(entries));
      refreshReadingLabels(dom$breadcrumb);
      renderBreadcrumb.$width = dom$breadcrumb.clientWidth;
      if (retained) {
        var link = Array.from(dom$breadcrumb.querySelectorAll("a")).find(function (item) { return item.href === retained; });
        if (link) link.focus({preventScroll: true});
        else { dom$breadcrumb.tabIndex = -1; dom$breadcrumb.focus({preventScroll: true}); }
      }
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
        if (ui$mode !== "graph") announceShown();
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
        if (event.key === "Escape" && !dom$filterPopover.hidden) { event.preventDefault(); setFilters(false); }
      });
      dom$browseToggle.addEventListener("click", function () {
        if (sourceNodes().length) setBrowse(dom$browsePopover.hidden);
        else { ui$relationIds = null; setMode("list", true); }
      });
      dom$browse.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !dom$browsePopover.hidden) { event.preventDefault(); setBrowse(false); }
      });
      // Captured, so a press on the whole map, which keeps its own pointer events, still closes them.
      globalListen(document, "pointerdown", function (event) {
        if (!dom$filters.contains(event.target)) setFilters(false, false);
        if (!dom$browse.contains(event.target)) setBrowse(false, false);
        if (!dom$searchWrap.contains(event.target)) closeSearch();
      }, true);
      dom$browse.addEventListener("focusout", function (event) {
        if (event.relatedTarget && !dom$browse.contains(event.relatedTarget)) setBrowse(false, false);
      });
      dom$filters.addEventListener("focusout", function (event) {
        if (!dom$filters.contains(event.relatedTarget)) setFilters(false, false);
      });
      dom$searchWrap.addEventListener("focusout", function (event) {
        if (event.relatedTarget && !dom$searchWrap.contains(event.relatedTarget)) closeSearch();
      });
      dom$graphMode.addEventListener("click", function () { setMode("graph", true); if (ui$mode === "graph") focusMap(); });
      dom$listMode.addEventListener("click", function () { setBrowse(false); ui$relationIds = null; setMode("list", true); });
      if (dom$sourcesMode) dom$sourcesMode.addEventListener("click", function () { setBrowse(false); setMode("sources", true); });
      dom$hiddenButton.addEventListener("click", revealAll);
      // Paging rebuilds the rows and may hide the button, so focus moves to the first new row, as search's paging does.
      dom$loadMore.addEventListener("click", function () {
        var previous = dom$inventoryRows ? dom$inventoryRows.size : 0; ui$listLimit += LIST_PAGE; renderInventory(false);
        var rows = dom$inventoryList.querySelectorAll(".bp-dag-inventory-row"); if (rows[previous]) rows[previous].focus();
      });
      dom$wholeSearch.addEventListener("click", openResults);
      if (dom$outlineToggle) dom$outlineToggle.addEventListener("click", function () {
        var open = !host.classList.contains("bp-dag-outline-open"); host.classList.toggle("bp-dag-outline-open", open);
        dom$outlineToggle.setAttribute("aria-expanded", String(open));
        if (open) {
          var current = dom$navigationInner.querySelector('[aria-current="page"]') || dom$navigationInner.querySelector("a");
          if (current) current.focus();
        }
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
      globalListen(document, "keydown", function (event) {
        if (event.defaultPrevented) return;
        var target = event.target, own = target === dom$search;
        if (!own && (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName || "") || target.isContentEditable)) return;
        if ((event.key === "/" && !own && host.contains(target) && !event.metaKey && !event.ctrlKey && !event.altKey) ||
            ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k")) {
          event.preventDefault();
          if (sheetLayout() && host.classList.contains("bp-dag-sheet-full")) setSheet(1, ui$selected);
          dom$search.focus(); dom$search.select();
        }
      });
      if (window.ResizeObserver) { var ro = new ResizeObserver(queueResize); ro.observe(dom$stage); observers.push(ro); }
      else globalListen(window, "resize", resize);
      globalListen(window, "resize", syncSheetAccessibility);
      if (navigation) [dom$readingBody, dom$navigation, dom$inventoryScroll].forEach(function (pane) { pane.addEventListener("scroll", remember); });
    }
    // A name in a statement's Lean shows the declaration it names in a card while it is hovered or focused. The card is
    // a copy of the one render hid in the name's block; the pointer can move onto it to read or scroll it.
    function bindLeanCards() {
      var card = element("div", "bp-lean-card md-typeset"), shown = null, timer = 0;
      card.id = "bp-lean-card-" + instance; card.setAttribute("role", "tooltip"); card.hidden = true; host.appendChild(card);
      // A click in the card focuses it, so a reader can select its text or drag its scrollbar without closing it.
      card.tabIndex = -1;
      function show(ref) {
        clearTimeout(timer);
        // A name the reader replaced while the pointer rested on it shows nothing.
        if (ref === shown || !ref.isConnected) return;
        var name = ref.getAttribute("data-lean-name"), block = ref.closest(".bp-lean-source");
        var entry = block && Array.from(block.querySelectorAll(".bp-lean-names > [data-lean-name]")).find(function (item) {
          return item.getAttribute("data-lean-name") === name;
        });
        hide();
        if (!entry) return;
        shown = ref; ref.setAttribute("aria-describedby", card.id);
        card.replaceChildren(element("p", "bp-lean-card-name", name));
        Array.from(entry.childNodes).forEach(function (child) { card.appendChild(child.cloneNode(true)); });
        card.hidden = false; card.scrollTop = 0; place();
      }
      // Under the name, or over it when the window has no room below; never past the window's sides.
      function place() {
        var box = shown.getBoundingClientRect(), below = box.bottom + 6 + card.offsetHeight <= window.innerHeight - 8;
        card.style.left = Math.max(8, Math.min(box.left, window.innerWidth - card.offsetWidth - 8)) + "px";
        card.style.top = Math.max(8, below ? box.bottom + 6 : box.top - 6 - card.offsetHeight) + "px";
      }
      function hide() {
        clearTimeout(timer);
        if (shown) shown.removeAttribute("aria-describedby");
        shown = null; card.hidden = true;
      }
      function leave(event) {
        if (shown && (shown.contains(event.relatedTarget) || card.contains(event.relatedTarget))) return;
        clearTimeout(timer); if (shown) timer = setTimeout(hide, 150);
      }
      function nameAt(event) { return event.target.closest(".bp-lean-ref"); }
      // A pointer that only crosses a name on its way elsewhere opens no card.
      dom$readingBody.addEventListener("pointerover", function (event) {
        var ref = nameAt(event);
        if (ref && event.pointerType !== "touch") { clearTimeout(timer); timer = setTimeout(function () { show(ref); }, shown ? 0 : 250); }
      });
      dom$readingBody.addEventListener("pointerout", leave);
      card.addEventListener("pointerover", function () { clearTimeout(timer); });
      card.addEventListener("pointerout", leave);
      dom$readingBody.addEventListener("focusin", function (event) { var ref = nameAt(event); if (ref) show(ref); else hide(); });
      dom$readingBody.addEventListener("focusout", function (event) {
        if (!dom$readingBody.contains(event.relatedTarget) && !card.contains(event.relatedTarget)) hide();
      });
      // A focused name keeps its card as the reader scrolls it into view; a hovered one's closes.
      dom$readingBody.addEventListener("scroll", function () { if (shown && shown === document.activeElement) place(); else hide(); });
      // Escape closes the card, wherever focus is, before it would close the reader.
      globalListen(document, "keydown", function (event) {
        if (event.key === "Escape" && shown) { event.preventDefault(); event.stopPropagation(); hide(); }
      }, true);
      // A press outside the card closes it, or cancels one about to open.
      globalListen(document, "pointerdown", function (event) { if (!card.contains(event.target)) hide(); }, true);
    }

    // Contents and the reading pane can be dragged wider or narrower, or hidden, and the choice holds on every page of the
    // site, scoped as Material scopes its own choices. Dragging a pane nearly shut hides it, as Enter on its splitter does;
    // a tab at the map's edge brings it back.
    function bindPanes() {
      var scope = window.__md_scope && window.__md_scope.pathname, key = "autoform:book-panes:v1" + (scope ? ":" + scope : ""), panes;
      try { panes = JSON.parse(window.localStorage.getItem(key)); } catch (error) { /* Refused storage, or not JSON. */ }
      // A stored value of another shape, or a width out of range, is dropped rather than written into the grid.
      if (!panes || typeof panes !== "object" || Array.isArray(panes)) panes = {};
      if (!(panes.topics >= 160 && panes.topics <= 480)) delete panes.topics;
      if (!(panes.reading > 0 && panes.reading < 1)) delete panes.reading;
      var main = dom$body.querySelector(".bp-dag-main"), context = host.querySelector(".bp-dag-context");
      var paneOf = {topics: dom$navigation, reading: dom$inspector};
      // In tab order Contents' splitter follows Contents, and the reader's comes just before the reader.
      function splitter(label, before) {
        var bar = element("div", "bp-dag-splitter"); bar.tabIndex = 0; bar.title = label + "; Enter hides, double-click or Delete resets";
        bar.setAttribute("role", "separator"); bar.setAttribute("aria-orientation", "vertical"); bar.setAttribute("aria-label", label);
        dom$body.insertBefore(bar, before); return bar;
      }
      var bars = {topics: splitter("Resize Contents", main), reading: splitter("Resize the reading pane", dom$inspector)};
      var tabs = {topics: button("bp-dag-pane-tab bp-dag-pane-tab-topics", "Contents", "Show Contents"),
        reading: button("bp-dag-pane-tab bp-dag-pane-tab-reading", "Reading", "Show the reading pane")};
      // Each tab comes in tab order where it sits: Contents' before the map, the reader's after it.
      main.prepend(tabs.topics); main.append(tabs.reading);
      // A drawn full-screen mark, like the theme switch beside it, so the trail row stays one line high.
      var mapOnly = button("bp-dag-map-only", "", "Map only"), svg = "http://www.w3.org/2000/svg", mark = document.createElementNS(svg, "svg");
      mark.setAttribute("viewBox", "0 0 20 20"); mark.setAttribute("aria-hidden", "true");
      var corners = document.createElementNS(svg, "path");
      corners.setAttribute("d", "M3 7.5V3h4.5M12.5 3H17v4.5M17 12.5V17h-4.5M7.5 17H3v-4.5");
      corners.setAttribute("fill", "none"); corners.setAttribute("stroke", "currentColor"); corners.setAttribute("stroke-width", "1.5");
      mark.appendChild(corners); mapOnly.appendChild(mark);
      context.insertBefore(mapOnly, context.querySelector(".bp-dag-theme"));
      function wide() { return !window.matchMedia || window.matchMedia("(min-width: 1101px)").matches; }
      function hidden(side) { return Boolean(panes[side + "Hidden"]) && !sheetLayout() && (side === "reading" || wide()); }
      function apply() {
        var focused = document.activeElement;
        ["topics", "reading"].forEach(function (side) {
          var hide = hidden(side), lost = hide && (paneOf[side].contains(focused) || bars[side] === focused);
          host.classList.toggle("bp-dag-" + side + "-hidden", hide);
          tabs[side].hidden = !hide;
          if (lost) tabs[side].focus();
        });
        if (panes.topics) host.style.setProperty("--dag-navigation-width", panes.topics + "px");
        else host.style.removeProperty("--dag-navigation-width");
        if (panes.reading) host.style.setProperty("--dag-reading-width", "minmax(300px, " + (100 * panes.reading).toFixed(2) + "%)");
        else host.style.removeProperty("--dag-reading-width");
        var shut = hidden("reading") && (hidden("topics") || !wide());
        pressed(mapOnly, shut);
        mapOnly.title = (shut ? "Show " : "Hide ") + (wide() ? "Contents and the reading pane" : "the reading pane");
        place();
      }
      // Each pane's bounds: Contents 160-480px, the reader 300px up to where the map keeps 160px of the room the two share
      // (the grid keeps that floor too, when Contents or the window changes later). A drawer floats over both and takes none.
      function least(side) { return side === "topics" ? 160 : 300; }
      function most(side) { return side === "topics" ? 480 : dom$inspector.offsetWidth + main.offsetWidth - 160; }
      // Each splitter sits on its pane's inner edge and reports its pane's share of the width and the share's bounds; the
      // overview's collapsed reader and a drawer have none.
      function place() {
        var width = dom$body.clientWidth || 1, focused = document.activeElement;
        bars.topics.hidden = !wide() || sheetLayout() || hidden("topics");
        bars.reading.hidden = sheetLayout() || hidden("reading") || dom$inspector.offsetWidth < 100;
        Object.keys(bars).forEach(function (side) {
          var bar = bars[side], pane = paneOf[side];
          if (bar.hidden) return;
          bar.style.left = (side === "topics" ? pane.offsetWidth : pane.offsetLeft) - bar.offsetWidth / 2 + "px";
          [["now", pane.offsetWidth], ["min", least(side)], ["max", most(side)]].forEach(function (value) {
            bar.setAttribute("aria-value" + value[0], String(Math.round(100 * value[1] / width)));
          });
        });
        // A splitter that a narrower window or an overview hides hands its focus to what stands for its pane there.
        if (bars.topics.hidden && focused === bars.topics) dom$outlineToggle.focus({preventScroll: true});
        if (bars.reading.hidden && focused === bars.reading) dom$sheetHandle.focus({preventScroll: true});
      }
      function save() { try { window.localStorage.setItem(key, JSON.stringify(panes)); } catch (error) { /* A preference only. */ } }
      // A width from the pointer or the keys, kept within the pane's bounds. Returns whether the pane was pulled nearly shut,
      // which hides it when the drag ends.
      function resizeTo(side, size) {
        var kept = clamp(size, least(side), most(side));
        if (side === "topics") panes.topics = Math.round(kept); else panes.reading = kept / dom$body.clientWidth;
        apply();
        return size < (side === "topics" ? 100 : 160);
      }
      function show(side, shown) { panes[side + "Hidden"] = !shown; apply(); save(); }
      Object.keys(bars).forEach(function (side) {
        var bar = bars[side];
        bar.addEventListener("pointerdown", function (event) {
          if (event.button !== 0) return;
          event.preventDefault(); bar.focus({preventScroll: true});
          if (bar.setPointerCapture) bar.setPointerCapture(event.pointerId);
          host.classList.add("bp-dag-resizing");
          // The edge moves as far as the pointer does, wherever on the splitter it was pressed. A pane dragged shut comes back
          // at the width it had before the drag. A drag ends however the pointer is let go, including the browser taking it.
          var shut = false, before = panes[side], events = ["pointermove", "pointerup", "pointercancel", "lostpointercapture"];
          var held = bar.getBoundingClientRect(), grab = event.clientX - held.left - held.width / 2;
          function move(moved) {
            var box = dom$body.getBoundingClientRect(), x = moved.clientX - grab;
            shut = resizeTo(side, side === "topics" ? x - box.left : box.right - x);
          }
          function end() {
            events.forEach(function (type, index) { bar.removeEventListener(type, index ? end : move); });
            host.classList.remove("bp-dag-resizing");
            if (shut) { panes[side + "Hidden"] = true; panes[side] = before; }
            apply(); save();
          }
          events.forEach(function (type, index) { bar.addEventListener(type, index ? end : move); });
        });
        function reset() { delete panes[side]; apply(); save(); }
        bar.addEventListener("dblclick", reset);
        bar.addEventListener("keydown", function (event) {
          var step = event.shiftKey ? 64 : 16, grow = side === "topics" ? "ArrowRight" : "ArrowLeft", shrink = side === "topics" ? "ArrowLeft" : "ArrowRight";
          if (event.key === grow || event.key === shrink) resizeTo(side, paneOf[side].offsetWidth + (event.key === grow ? step : -step));
          else if (event.key === "Home" || event.key === "End") resizeTo(side, event.key === "End" ? Infinity : 0);
          else if (event.key === "Enter") show(side, false);
          // A Mac's delete key is Backspace.
          else if (event.key === "Delete" || event.key === "Backspace") reset();
          else return;
          event.preventDefault(); save();
        });
        // An overview's reader comes back as its collapsed handle, which has no splitter: the handle takes focus.
        tabs[side].addEventListener("click", function () {
          show(side, true); (bars[side].hidden ? dom$sheetHandle : bars[side]).focus({preventScroll: true});
        });
      });
      // Where Contents is a drawer, Map only hides the reader and closes the drawer, leaving the wide layout's Contents alone.
      mapOnly.addEventListener("click", function () {
        var shut = mapOnly.getAttribute("aria-pressed") === "true";
        panes.readingHidden = !shut;
        if (wide()) panes.topicsHidden = !shut;
        else if (!shut && host.classList.contains("bp-dag-outline-open")) dom$outlineToggle.click();
        apply(); save();
      });
      if (window.ResizeObserver) { var ro = new ResizeObserver(place); ro.observe(dom$inspector); ro.observe(dom$navigation); observers.push(ro); }
      globalListen(window, "resize", apply);
      apply();
    }

    function statusLabel(key) { return (palette[key] || {}).label || key.replace(/_/g, " "); }
    function renderStatusFields() {
      dom$statusFieldset.replaceChildren(element("legend", "", "Formalization status"));
      dom$statusInputs.clear();
      presentStatuses.forEach(function (key) {
        var label = element("label", "bp-dag-check"), input = element("input");
        input.type = "checkbox"; input.checked = ui$selectedStatuses.has(key); input.value = key;
        input.addEventListener("change", onStatusChange); dom$statusInputs.set(key, input);
        // The filter doubles as the legend: each status says what it means, in the palette's own words.
        var text = element("span", "bp-dag-check-text", statusLabel(key)), meaning = (palette[key] || {}).meaning, mark = swatch(key);
        // A checkbox is named by its status and count; the meaning is its description, not part of a name heard on every Tab.
        input.setAttribute("aria-label", statusLabel(key) + ", " + plural(statusCounts.get(key) || 0, "statement", "statements"));
        if (meaning) { var described = text.appendChild(element("span", "bp-dag-check-meaning", meaning)); described.id = "bp-dag-meaning-" + instance + "-" + key;
          input.setAttribute("aria-describedby", described.id); }
        mark.removeAttribute("title"); mark.setAttribute("aria-hidden", "true");
        label.appendChild(input); label.appendChild(mark); label.appendChild(text);
        label.appendChild(element("span", "bp-dag-check-count", String(statusCounts.get(key) || 0))); dom$statusFieldset.appendChild(label);
      });
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
    function refresh(resetList) {
      if (resetList) ui$listLimit = LIST_PAGE;
      syncControls(); renderInspector(); renderInventory(resetList);
    }
    function syncControls() {
      if (ui$mode === "graph" && ui$mathJob) stopRowMath();
      syncNavigationSelection();
      dom$statusInputs.forEach(function (input, key) { input.checked = ui$selectedStatuses.has(key); });
      // A link may name statuses this page doesn't have; the summary and the map count the present ones it shows.
      var shown = presentStatuses.filter(function (key) { return ui$selectedStatuses.has(key); }), enabled = shown.length;
      dom$filterSummary.textContent = "Filter: " + (enabled === presentStatuses.length ? "all" : enabled + " of " + presentStatuses.length) + " statuses";
      // The whole map dims the results whose status the filter leaves out.
      if (enabled < presentStatuses.length) host.dataset.mapStatuses = shown.join(" "); else delete host.dataset.mapStatuses;
      dom$graphMode.hidden = ui$mode === "graph";
      dom$listMode.textContent = currentBundle && currentBundle.scope === "roadmap" ? "All statements" : "Statements in this topic";
      var hasSources = sourceNodes().length > 0;
      if (hasSources && dom$sourcesMode.parentElement !== dom$browsePopover) dom$browsePopover.appendChild(dom$sourcesMode);
      if (!hasSources && dom$sourcesMode.parentElement === dom$browsePopover) { setBrowse(false); dom$sourcesMode.remove(); }
      dom$browseToggle.textContent = hasSources ? "Browse" : "Statements";
      dom$browseToggle.setAttribute("aria-label", hasSources ? "Browse statements or source texts" : "Browse statements");
      if (hasSources) {
        dom$browseToggle.setAttribute("aria-controls", dom$browsePopover.id);
        dom$browseToggle.setAttribute("aria-expanded", String(!dom$browsePopover.hidden));
      } else { dom$browsePopover.hidden = true; dom$browseToggle.removeAttribute("aria-controls"); dom$browseToggle.removeAttribute("aria-expanded"); }
      // Browse marks the list on screen, so at most one of its choices reads as pressed; on the map neither does.
      pressed(dom$listMode, ui$mode === "list");
      if (dom$sourcesMode) pressed(dom$sourcesMode, ui$mode === "sources");
      dom$stage.hidden = ui$mode !== "graph"; dom$inventory.hidden = ui$mode === "graph";
      if (ui$mode === "graph") dom$hiddenButton.hidden = true;
      if (navigation) dom$inventory.querySelector("h2").textContent = ui$mode === "sources" ? "Source texts" : "Statements";
    }

    function queueResize() {
      // The breadcrumb's refold and the sheet's accessibility wait for a frame, out of ResizeObserver delivery.
      if (!alive || resizeFrame !== null) return;
      resizeFrame = requestAnimationFrame(function () {
        resizeFrame = null;
        if (alive) resize();
      });
    }
    function resize() {
      syncSheetAccessibility();
      if (!dom$stage.clientWidth || !dom$stage.clientHeight) return;
      if (renderBreadcrumb.$width !== dom$breadcrumb.clientWidth) renderBreadcrumb();
    }

    function searchScore(node, query) {
      var title = node.title.toLocaleLowerCase(), id = node.id.toLocaleLowerCase();
      var lean = (node.lean || "").toLocaleLowerCase(), summary = (node.summary || "").toLocaleLowerCase();
      if (title === query) return 1; if (id === query) return 2; if (lean === query) return 3;
      if (title.startsWith(query)) return 4; if (id.startsWith(query)) return 5; if (lean.startsWith(query)) return 6;
      if (title.split(/\s+/).some(function (word) { return word.startsWith(query); })) return 7;
      if (title.indexOf(query) >= 0) return 8; if (id.indexOf(query) >= 0) return 9;
      if (lean.indexOf(query) >= 0) return 10; if (summary.indexOf(query) >= 0) return 11;
      var terms = query.split(/\s+/).filter(Boolean); if (terms.length < 2) return 0;
      var all = [title, id, lean, summary].join(" ");
      return terms.every(function (term) { return all.indexOf(term) >= 0; }) ? 12 : 0;
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
      // Every row that matches a query matches the query it extends, so typing on ranks only the last matches.
      var pool = ui$searchCacheQuery && query.startsWith(ui$searchCacheQuery) ? ui$searchCache.map(function (match) { return match.node; }) : ui$searchNodes;
      ui$searchCacheQuery = query; ui$searchCache = rankMatches(pool, query);
      return ui$searchCache;
    }
    function renderSearch() {
      dom$searchResults.textContent = ""; if (!ui$query) { closeSearch(); return; }
      var matches = searchMatches(), list = element("ul", "bp-dag-results");
      matches.slice(0, ui$searchLimit).forEach(function (match) {
        var item = element("li"), choose = button("bp-dag-result", ""); choose.dataset.resultId = match.node.id;
        choose.appendChild(readingLabel("span", "bp-dag-result-title", match.node.title, (records.get(recordId(match.node)) || match.node).reading_title));
        if (tracksLean) choose.appendChild(element("span", "bp-dag-result-meta", statusLabel(match.node.status))).title = (palette[match.node.status] || {}).meaning || "";
        var place = placeOf(match.node), topic = (records.get(recordId(match.node)) || match.node).scope === match.node.id;
        if (place || topic) choose.appendChild(element("span", "bp-dag-result-place", topic ? "Section" + (place ? " in " + place : "") : place));
        choose.addEventListener("click", function () { activateNode(match.node, true); }); item.appendChild(choose); list.appendChild(item);
        choose.addEventListener("keydown", searchResultKeydown);
      });
      dom$searchResults.appendChild(list); refreshReadingLabels(list);
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
      if (!matches.length) dom$searchResults.appendChild(element("p", "bp-dag-overview",
        tracksLean ? "No matching title, statement, article id, or Lean name." : "No matching title, statement, or article id."));
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
    function setFilters(open, returnFocus) {
      if (!open && returnFocus !== false && dom$filterPopover.contains(document.activeElement)) dom$filterSummary.focus({preventScroll: true});
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
      var id = recordId(node), record = records.get(id), settings = {history: "push", focus: "reading"};
      if (record && record.scope === record.id && !record.result && record.navigation !== "source" && record.catalog !== "module") settings.mapFirst = true;
      navigation.navigateNode(id, settings); closeSearch();
    }
    // The whole map's own focus: the card it has selected, or else the map.
    function mapControl() {
      return dom$stage.querySelector('.bp-whole-map .pm-result.pm-selected[tabindex="0"]') || dom$stage.querySelector('.bp-whole-map svg[tabindex="0"]');
    }
    // Back to map hands focus to the map once it shows, unless the reader moved focus on, the page changed or a full phone
    // reader covers the map. A map that is missing, or still hidden waiting for its style, leaves focus on the filter.
    function focusMap() {
      var bundle = currentBundle, owner = document.activeElement, owned = host.contains(owner), request = focusMap.$request = {};
      requestAnimationFrame(function () { requestAnimationFrame(function () {
        if (!alive || focusMap.$request !== request || currentBundle !== bundle || ui$mode !== "graph" ||
            (document.activeElement !== owner && !(owned && (owner.hidden || owner.isConnected === false) &&
              (document.activeElement === document.body || document.activeElement === document.documentElement))) ||
            (sheetLayout() && host.classList.contains("bp-dag-sheet-full")) || (bundle && bundle.isCurrent && !bundle.isCurrent())) return;
        var control = mapControl();
        if (control) control.focus({preventScroll: true});
        if (!control || document.activeElement !== control) (tracksLean ? dom$filterSummary : dom$search).focus({preventScroll: true});
        remember();
      }); });
    }
    function mapUnavailable() {
      if (!alive || dom$stage.querySelector(".bp-dag-map-unavailable")) return;
      var note = element("p", "bp-dag-map-unavailable", "The map could not load. Contents, search and the statement lists still work.");
      note.setAttribute("role", "status"); dom$stage.appendChild(note); dom$announcement.textContent = note.textContent;
    }

    function clearSelection(updateHistory, returnFocus) {
      if (!ui$selected) return;
      if (navigation && updateHistory) navigation.checkpoint();
      var cleared = ui$selected;
      ui$selected = null;
      setSheet(navigation && !sheetLayout() && !ui$overviewOnly, null);
      syncNavigationSelection(); renderInspector(); renderInventory(false);
      if (updateHistory) writeLocation("push");
      var chosen = returnFocus && cleared && ui$mode === "list" && dom$inventoryRows.get(cleared.id);
      if (chosen) chosen.focus({preventScroll: true});
    }
    // Measure both class changes together; callers need no selection-toggle ordering rule.
    // A phone reader also has a full position: 1 asks for half, 2 for full, and true keeps full if it is there.
    function setSheet(open, selected) {
      var width = dom$stage.clientWidth, height = dom$stage.clientHeight;
      host.classList.toggle("bp-dag-sheet-open", Boolean(open));
      host.classList.toggle("bp-dag-sheet-full", open > 1 || open === true && host.classList.contains("bp-dag-sheet-full"));
      host.classList.toggle("bp-dag-has-selection", Boolean(selected));
      dom$sheetHandle.setAttribute("aria-expanded", String(Boolean(open)));
      syncSheetAccessibility();
      if (width !== dom$stage.clientWidth || height !== dom$stage.clientHeight) resize();
    }
    function sheetLayout() { return Boolean(window.matchMedia && window.matchMedia("(max-width: 720px)").matches); }
    function syncSheetAccessibility() {
      if (!dom$inspectorInner || !dom$sheetHandle) return;
      var opened = host.classList.contains("bp-dag-sheet-open");
      var collapsed = !opened && (sheetLayout() || ui$overviewOnly);
      // The handle names the page or entry, with its status, rather than a generic "Reading"; its accessible name starts with that title.
      // An open desktop reader over the overview closes with ×.
      var page = currentBundle || data, close = opened && !sheetLayout() && ui$overviewOnly,
        record = records.get(page.nodeId || ui$requestedNode || (ui$selected && ui$selected.id));
      if (!record || !record.result) record = page.readingSource && records.get(page.readingSource.dataset.autoformNodeId);
      var title = record ? record.title : page.title;
      // A chapter's own record carries a rollup status, not its results', so only a statement gets a status mark.
      var handleTitle = close ? "" : readingLabel("span", "bp-dag-sheet-title", title, record && record.reading_title);
      dom$sheetHandle.replaceChildren(close ? "×" : record && record.result && record.status ? swatch(record.status) : "", handleTitle);
      if (handleTitle) refreshReadingLabels(dom$sheetHandle);
      dom$sheetHandle.setAttribute("aria-label", close ? "Close reading" : plainTitle(title, record && record.reading_title) + ", reading");
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
        dom$pageHeading.textContent = plainTitle(currentBundle && currentBundle.title || data.title, (records.get(page.nodeId) || {}).reading_title);
        dom$pageHeading.hidden = !collapsed;
      }
    }
    function revealAll() {
      ui$selectedStatuses = new Set(presentStatuses);
      writeLocation("replace"); refresh(true);
      dom$filterSummary.focus({preventScroll: true});
    }
    // A search or filter that changes the rows says what is left, since the count on screen is not announced. On the map
    // the filter only dims, so it says the filter.
    function announceShown() {
      dom$announcement.textContent = ui$mode === "graph" ? dom$filterSummary.textContent :
        dom$inventorySummary.firstChild ? dom$inventorySummary.firstChild.textContent : "";
    }
    // The map dims the statuses the filter leaves out, so the selection stays.
    function onStatusChange() {
      ui$filtersTouched = true;
      ui$selectedStatuses = new Set();
      dom$statusInputs.forEach(function (input, key) { if (input.checked) ui$selectedStatuses.add(key); });
      writeLocation("replace"); refresh(true); announceShown();
    }
    function setMode(mode, updateHistory) {
      if (mode !== "list") ui$relationIds = null;
      ui$mode = mode; syncControls(); if (mode !== "graph") renderInventory(true);
      if (navigation && sheetLayout()) setSheet(false, ui$selected);
      if (updateHistory) writeLocation("replace");
    }

    // An article's links to other articles, and theirs to it, end the article as "See also" and "Linked from", and scroll
    // with it; an article with neither shows nothing. Where some pages could not be read, a line says the lists may miss some.
    // Being in the article, the links are followed by the reader's own link handling, as the article's links are.
    function renderWikiContext() {
      if (!dom$readingBody) return;
      var previous = dom$readingBody.querySelector(".bp-reading-wiki-links");
      var node = ui$selected, id = currentBundle && currentBundle.nodeId || ui$requestedNode || node && recordId(node);
      if (previous && ui$wikiRendered === ui$wikiLinks && ui$wikiNode === id) return;
      if (previous) previous.remove();
      ui$wikiRendered = ui$wikiLinks; ui$wikiNode = id;
      if (!ui$wikiLinks || !records.has(id)) return;
      var links = wikiNeighbors(id);
      if (!links.before.length && !links.after.length) return;
      var section = element("section", "bp-reading-wiki-links");
      if (links.availability === "partial") section.appendChild(element("p", "bp-reading-wiki-notice", "Some pages could not be read, so these lists may miss some links."));
      [["See also", links.after], ["Linked from", links.before]].forEach(function (pair) {
        if (!pair[1].length) return;
        var list = element("ul", "bp-reading-wiki-list"), shown = 0, more = button("bp-reading-wiki-more", "");
        function appendLinks() {
          var limit = Math.min(pair[1].length, shown + (shown ? 20 : 10));
          pair[1].slice(shown, limit).forEach(function (relatedId) {
            var related = records.get(relatedId), item = element("li"), link = readingLabel("a", "", related.title, related.reading_title);
            link.href = related.route || related.url; item.appendChild(link); list.appendChild(item);
          });
          shown = limit; more.hidden = limit >= pair[1].length;
          more.textContent = "Show " + Math.min(20, pair[1].length - limit) + " more links";
          refreshReadingLabels(list);
        }
        appendLinks();
        // The button stays below the new links, so focus moves to the first of them.
        more.addEventListener("click", function () { var first = shown; appendLinks(); var links = list.querySelectorAll("a"); if (links[first]) links[first].focus(); });
        section.appendChild(element("h2", "", pair[0])); section.appendChild(list); section.appendChild(more);
      });
      dom$readingBody.appendChild(section);
      refreshReadingLabels(section);
    }
    function renderInspector() {
      renderReaderContext(); renderWikiContext(); refreshReadingLabels(dom$readingContext);
    }

    function inventoryNodes() {
      var query = ui$query.toLocaleLowerCase();
      var sources = ui$mode === "sources" ? new Set(sourceNodes().map(function (node) { return node.id; })) : null;
      var relations = ui$relationIds ? new Set(ui$relationIds) : null;
      var pool = sharedIndex ? sharedIndex.nodes.filter(function (node) {
        if (relations) return relations.has(node.id);
        if (sources) return sources.has(node.id);
        return inCurrentScope(node) && (node.result === true || node.catalog === "module");
      }) : data.nodes;
      var nodes = query ? rankMatches(pool, query).map(function (match) { return match.node; }) : pool;
      var shown = nodes.filter(function (node) { return ui$selectedStatuses.has(node.status); }), hidden = nodes.length - shown.length;
      // The list offers back what the filter hides.
      dom$hiddenButton.textContent = "Show " + hidden + " filtered out"; dom$hiddenButton.hidden = !hidden;
      return shown;
    }
    // Preview only closed code spans and unambiguous inline formulas. Unsupported
    // Markdown stays literal; it must not silently turn currency or code into math.
    function pendingMath(source) { var span = element("span", "bp-math-pending", source); span.setAttribute("aria-hidden", "true"); return span; }
    function summaryLine(node) {
      var text = String(node.summary), line = element("span", "bp-result-summary"), title = node.title || "";
      if (title && text.indexOf(title) === 0 && /^[.:]\s/.test(text.slice(title.length))) text = text.slice(title.length + 1).trim();
      var pattern = /\\([\\`$])|(?<![\\`])(`+)(?!`)([^\n]*?)(?<!`)\2(?!`)|(?<![\\$])\$(?![\s$])((?:\\.|[^\\$\n])+?)(?<!\s)\$(?![$\d])|(?<!\\)\\\(((?:\\[^)]|[^\\\n])+?)\\\)/g;
      var last = 0, match;
      while ((match = pattern.exec(text))) {
        // An unmatched backtick may own later dollars. Keep the complete preview
        // literal instead of guessing where code ends.
        if (text.slice(last, match.index).indexOf("`") >= 0) return element("span", "bp-result-summary", text);
        if (match.index > last) line.appendChild(element("span", "", text.slice(last, match.index)));
        line.appendChild(match[1] !== undefined ? element("span", "", match[1]) : match[2] !== undefined ?
          element("code", "", match[3].trim()) : pendingMath("\\(" + (match[4] !== undefined ? match[4] : match[5]) + "\\)"));
        last = pattern.lastIndex;
      }
      if (text.slice(last).indexOf("`") >= 0) return element("span", "bp-result-summary", text);
      if (last < text.length) line.appendChild(element("span", "", text.slice(last)));
      return line;
    }
    // Typesetting a page of 100 summaries at once held the page still for two seconds. A row's formulas become MathJax's
    // to read only as the row comes near the view, after the list has painted, and a pass reads at most six rows, so a
    // pass stays short and a scroll costs a pass or two rather than one per row.
    function typesetRowsInView() {
      stopRowMath();
      if (!alive || ui$mode === "graph") return;
      var lines = Array.prototype.filter.call(dom$inventoryList.querySelectorAll(".bp-dag-inventory-row"), function (row) { return row.querySelector(".bp-math-pending"); });
      if (!lines.length) return;
      if (!window.AutoformMath || typeof window.AutoformMath.refresh !== "function") { fallbackRowMath(lines); return; }
      var job = ui$mathJob = {$bundle: currentBundle, $mode: ui$mode, $rows: lines, $queue: [], $queued: new Set(), $busy: false,
        $timer: null, $deadline: null, $observer: null};
      if (typeof IntersectionObserver !== "function") { job.$queue = lines.slice(); queueRowMath(job, 60); return; }
      var observer = job.$observer = new IntersectionObserver(function (entries) {
        if (!rowMathCurrent(job)) { if (ui$mathJob === job) stopRowMath(); else observer.disconnect(); return; }
        entries.forEach(function (entry) {
          if (entry.isIntersecting && dom$inventoryList.contains(entry.target) && !job.$queued.has(entry.target)) {
            observer.unobserve(entry.target); job.$queued.add(entry.target); job.$queue.push(entry.target);
          }
        });
        queueRowMath(job, 60);
      }, {root: dom$inventoryScroll, rootMargin: "200px 0px"});
      lines.forEach(function (line) { observer.observe(line); });
    }
    function rowMathCurrent(job) {
      return alive && ui$mathJob === job && currentBundle === job.$bundle && ui$mode === job.$mode && ui$mode !== "graph" &&
        (!job.$bundle.isCurrent || job.$bundle.isCurrent());
    }
    function queueRowMath(job, delay) {
      if (!rowMathCurrent(job) || job.$busy || !job.$queue.length || job.$timer !== null) return;
      job.$timer = setTimeout(function () { job.$timer = null; typesetRevealedRows(job); }, delay);
    }
    function fallbackRowMath(rows) {
      rows.forEach(function (line) {
        Array.prototype.forEach.call(line.querySelectorAll(".bp-math-pending, .arithmatex:not(:has(mjx-container))"), function (span) {
          span.className = "bp-math-fallback"; span.removeAttribute("aria-hidden");
        });
      });
    }
    function stopRowMath() {
      var job = ui$mathJob; ui$mathJob = null;
      if (!job) return;
      clearTimeout(job.$timer); clearTimeout(job.$deadline); if (job.$observer) job.$observer.disconnect();
      fallbackRowMath(job.$rows); job.$queue = [];
    }
    function typesetRevealedRows(job) {
      if (!rowMathCurrent(job)) { if (ui$mathJob === job) stopRowMath(); return; }
      if (job.$busy || !job.$queue.length) return;
      var batch = job.$queue.splice(0, 6).filter(function (line) { return line.isConnected && dom$inventoryList.contains(line); });
      if (!batch.length) { queueRowMath(job, 0); return; }
      if (!window.AutoformMath || typeof window.AutoformMath.refresh !== "function") { stopRowMath(); return; }
      batch.forEach(function (line) {
        Array.prototype.forEach.call(line.querySelectorAll(".bp-math-pending"), function (span) { span.className = "arithmatex"; span.removeAttribute("aria-hidden"); });
      });
      job.$busy = true;
      function failed() { if (ui$mathJob === job) stopRowMath(); }
      // An unavailable or stalled engine must leave honest, visible source text.
      job.$deadline = setTimeout(failed, 2000);
      try {
        Promise.resolve(window.AutoformMath.refresh(dom$inventoryList)).then(function (result) {
          clearTimeout(job.$deadline); job.$deadline = null;
          if (result === false) { failed(); return; }
          if (!rowMathCurrent(job)) { if (ui$mathJob === job) stopRowMath(); return; }
          job.$busy = false; queueRowMath(job, 0);
        }, failed);
      } catch (_error) { failed(); }
    }
    function inCurrentScope(node) {
      var scope = currentBundle && currentBundle.scope; if (!scope || scope === "roadmap") return true;
      var cache = inCurrentScope.$cache;
      if (!cache || cache.index !== sharedIndex || cache.scope !== scope) {
        cache = {index: sharedIndex, scope: scope, parents: new Map([[scope, true]])}; inCurrentScope.$cache = cache;
      }
      var current = node, path = [], seen = new Set();
      while (current && current.parent !== scope && !cache.parents.has(current.id) && !seen.has(current.id)) {
        seen.add(current.id); path.push(current.id); current = records.get(current.parent);
      }
      var included = Boolean(current && (current.parent === scope || cache.parents.get(current.id)));
      path.forEach(function (id) { cache.parents.set(id, included); }); return included;
    }
    function sourceNodes() {
      var scope = currentBundle && currentBundle.scope;
      var cached = sourceNodes.$cache;
      if (cached && cached.index === sharedIndex && cached.scope === scope) return cached.nodes;
      var nodes = (sharedIndex ? sharedIndex.nodes : []).filter(function (node) {
        return node.navigation === "source" && inCurrentScope(node);
      });
      sourceNodes.$cache = {index: sharedIndex, scope: scope, nodes: nodes}; return nodes;
    }
    function renderInventory(resetScroll) {
      if (!dom$inventoryList || ui$mode === "graph") return;
      var nodes = inventoryNodes(), visible = nodes.slice(0, ui$listLimit); dom$inventoryList.textContent = "";
      var articles = nodes.some(function (node) { return node.catalog === "module"; });
      // The list says "statements", as Browse and the dashboard do.
      if (navigation && ui$mode !== "sources") dom$inventory.querySelector("h2").textContent = articles ? "Statements & articles" : "Statements";
      dom$inventoryRows = new Map();
      visible.forEach(function (node) {
        var item = element("li"), row = button("bp-dag-inventory-row", "");
        row.dataset.inventoryNodeId = node.id;
        if (ui$selected && node.id === ui$selected.id) row.setAttribute("aria-current", "true");
        dom$inventoryRows.set(node.id, row);
        var rowId = "bp-dag-row-" + instance + "-" + dom$inventoryRows.size;
        // A title's formulas wait for their row to come into view, as its summary's do.
        var titleParts = node.reading_title || (records.get(recordId(node)) || node).reading_title, rowTitle = row.appendChild(readingLabel("span", "bp-dag-inventory-title", node.title, titleParts));
        rowTitle.id = rowId + "-title";
        if (titleParts) Array.prototype.forEach.call(rowTitle.querySelectorAll(".arithmatex"), function (math) { math.replaceWith(pendingMath(math.textContent)); });
        var where = navigation && placeOf(node, currentBundle && currentBundle.scope) || node.area;
        var rowMeta = row.appendChild(element("span", "bp-dag-inventory-meta", [tracksLean ? statusLabel(node.status) : "", where].filter(Boolean).join(" · ")));
        rowMeta.title = tracksLean && (palette[node.status] || {}).meaning || ""; rowMeta.id = rowId + "-meta";
        // A row is named by its title and status line; its summary is the description, so a name is not 400 characters long.
        row.setAttribute("aria-labelledby", rowId + "-title " + rowId + "-meta");
        if (navigation && node.summary) { var summary = row.appendChild(summaryLine(node)); summary.id = rowId + "-summary"; row.setAttribute("aria-describedby", summary.id); }
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
      mapUnavailable: mapUnavailable,
      dispose: function () { alive = false; stopRowMath(); clearTimeout(checkpointTimer);
        if (resizeFrame !== null) cancelAnimationFrame(resizeFrame); resizeFrame = null; cleanup.forEach(function (close) { close(); });
        observers.forEach(function (observer) { observer.disconnect(); }); if (navigation) navigation.dispose(); }};
  }

  function boot() {
    clients.forEach(function (client, host) { if (!host.isConnected) { client.dispose(); clients.delete(host); } });
    document.querySelectorAll(".bp-dag-viewer[data-graph-src]").forEach(start);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
  if (window.document$ && window.document$.subscribe) window.document$.subscribe(boot);
})();
