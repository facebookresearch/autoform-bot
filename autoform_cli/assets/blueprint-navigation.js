/* Shared book/explorer navigation. Reading HTML is published by Autoform, not made from graph summaries.
 * create({host,onCommit,captureState,restoreState,onIndex,onError}) keeps one renderer-owned shell.
 * onCommit receives a detached readingSource, canonical url, resolved graph/dataURL, nodeId/anchor,
 * scope/title, snapshot, reason and isCurrent(). No math engine is owned or called here.
 * checkpoint() replaces only autoformNavigation:{version:1,url,snapshot}; checkpoint('push') records
 * a deliberate view/filter action without changing the canonical permalink. Snapshot fields are opaque.
 */
(function () {
  "use strict";
  if (window.AutoformNavigation) return;

  var HISTORY_KEY = "autoformNavigation", GRAPH_SCHEMA = "autoform-dag-view/v2", INDEX_SCHEMA = "autoform-dag-search/v1";

  function create(options) {
    options = options || {};
    var host = options.host, doc = options.document || document, win = options.window || window;
    if (!host || typeof options.onCommit !== "function") throw new Error("Navigation needs a host and onCommit callback");
    var origin = new URL(win.location.href).origin, initialURL = win.location.href;
    var initialPage = host.closest(".bp-explorer-page") || doc.querySelector(".bp-explorer-page");
    var source = initialPage && initialPage.querySelector(".bp-reading-source") || doc.querySelector(".bp-reading-source");
    var initialSource = source && source.cloneNode(true);
    var catalogueURL = link(attribute(host, initialPage, "data-catalog-src"), initialURL);
    var initialDataURL = link(attribute(host, initialPage, "data-graph-src"), initialURL);
    var initialCanonical = link(attribute(host, initialPage, "data-page-href"), initialURL) || initialURL;
    var initialScope = attribute(host, initialPage, "data-scope-id") || null;
    var initialTitle = source && source.getAttribute("data-page-title") ||
      initialPage && initialPage.getAttribute("data-page-title") || doc.title || "Mathematics";
    var disposed = false, started = null, epoch = 0, active = null, current = null;
    var index = null, indexPromise = null, indexController = null, indexed = new Map(), routes = new Map(), documents = new Set();
    var bound = new Map(), htmlCache = new Map(), jsonCache = new Map(), cacheBytes = 0;
    var timeout = bounded(options.timeout, 15000, 1000, 60000);
    var htmlLimit = bounded(options.htmlLimit, 8 * 1024 * 1024, 1024, 32 * 1024 * 1024);
    var jsonLimit = bounded(options.jsonLimit, 32 * 1024 * 1024, 1024, 64 * 1024 * 1024);
    var cacheLimit = bounded(options.cacheBytes, 48 * 1024 * 1024, 1024, 128 * 1024 * 1024);
    var htmlEntries = bounded(options.htmlEntries, 8, 1, 32), jsonEntries = bounded(options.jsonEntries, 16, 1, 64);
    var sitePrefix = prefix(initialCanonical, catalogueURL, initialDataURL);
    documents.add(resource(initialCanonical));

    function bounded(value, fallback, low, high) {
      return Number.isSafeInteger(value) ? Math.max(low, Math.min(high, value)) : fallback;
    }
    function attribute(first, second, name) { return first.getAttribute(name) || second && second.getAttribute(name); }
    function link(value, base, outside) {
      if (typeof value !== "string" || !value || /[\u0000-\u001f\u007f]/.test(value)) return null;
      try {
        var url = new URL(value, base);
        return /^(https?:)$/.test(url.protocol) && !url.username && !url.password && (outside || url.origin === origin) ? url.href : null;
      } catch (_error) { return null; }
    }
    function resource(value) { var url = new URL(value); url.hash = ""; return url.href; }
    function prefix() {
      for (var n = 0; n < arguments.length; n++) {
        if (!arguments[n]) continue;
        var path = new URL(arguments[n]).pathname, match = /^(.*\/)(?:roadmap|dependencies)(?:\/|\.html|\.json)/.exec(path);
        if (match) return match[1];
      }
      return new URL(initialURL).pathname.replace(/[^/]*$/, "");
    }
    function abortError() { return new DOMException("Navigation superseded", "AbortError"); }
    function fresh() {
      if (active) active.abort();
      active = new AbortController(); epoch++;
      host.setAttribute("aria-busy", "true");
      return {epoch: epoch, signal: active.signal};
    }
    function alive(request) { return !disposed && request.epoch === epoch && !request.signal.aborted; }
    function assertAlive(request) { if (!alive(request)) throw abortError(); }
    function report(error, url) {
      if (!error || typeof error !== "object") error = new Error(String(error));
      if (disposed || error.autoformReported) return;
      error.autoformReported = true; if (!error.url) error.url = url;
      if (typeof options.onError === "function") {
        try { options.onError(error); } catch (_ignored) { /* An error display cannot disable native reading. */ }
      }
    }
    function waitFor(promise, signal) {
      if (signal.aborted) return Promise.reject(abortError());
      return new Promise(function (resolve, reject) {
        function abort() { reject(abortError()); }
        signal.addEventListener("abort", abort, {once: true});
        promise.then(function (value) { signal.removeEventListener("abort", abort); resolve(value); }, function (error) {
          signal.removeEventListener("abort", abort); reject(error);
        });
      });
    }
    function cacheGet(cache, key) {
      var entry = cache.get(key); if (!entry) return null;
      cache.delete(key); cache.set(key, entry); return entry;
    }
    function cacheDelete(cache, key) {
      var entry = cache.get(key); if (entry) { cacheBytes -= entry.bytes; cache.delete(key); }
    }
    function cachePut(cache, key, value, maximum) {
      if (value.bytes > cacheLimit) return;
      if (cache.has(key)) { cacheBytes -= cache.get(key).bytes; cache.delete(key); }
      cache.set(key, value); cacheBytes += value.bytes;
      while (cache.size > maximum) {
        var oldest = cache.keys().next().value; cacheBytes -= cache.get(oldest).bytes; cache.delete(oldest);
      }
      while (cacheBytes > cacheLimit) {
        var victim = htmlCache.size ? htmlCache : jsonCache, id = victim.keys().next().value;
        cacheBytes -= victim.get(id).bytes; victim.delete(id);
      }
    }
    async function fetchText(value, type, parentSignal) {
      var href = link(value, initialURL); if (!href) throw new Error("Refusing an external data request");
      var key = resource(href), cache = type === "html" ? htmlCache : jsonCache;
      var cached = cacheGet(cache, key); if (cached) return cached;
      var controller = new AbortController(), expired = false;
      function abort() { controller.abort(); }
      if (parentSignal) {
        if (parentSignal.aborted) throw abortError();
        parentSignal.addEventListener("abort", abort, {once: true});
      }
      var timer = setTimeout(function () { expired = true; controller.abort(); }, timeout);
      var limit = type === "html" ? htmlLimit : jsonLimit;
      try {
        var response = await (options.fetch || win.fetch.bind(win))(key, {credentials: "same-origin", signal: controller.signal});
        if (!response.ok) throw new Error("HTTP " + response.status);
        var responseURL = link(response.url || key, key); if (!responseURL) throw new Error("Data redirected outside this site");
        if (Number(response.headers.get("content-length")) > limit) throw new Error("Published data is too large");
        var text, length;
        if (response.body && response.body.getReader) {
          var reader = response.body.getReader(), chunks = [], total = 0;
          while (true) {
            var chunk = await reader.read(); if (chunk.done) break;
            total += chunk.value.byteLength;
            if (total > limit) { await reader.cancel(); throw new Error("Published data is too large"); }
            chunks.push(chunk.value);
          }
          var bytes = new Uint8Array(total), offset = 0;
          chunks.forEach(function (part) { bytes.set(part, offset); offset += part.byteLength; });
          text = new TextDecoder("utf-8", {fatal: true}).decode(bytes); length = total;
        } else {
          text = await response.text(); length = new TextEncoder().encode(text).byteLength;
          if (length > limit) throw new Error("Published data is too large");
        }
        if (parentSignal && parentSignal.aborted) throw abortError();
        var entry = {text: text, url: responseURL, bytes: length};
        cachePut(cache, key, entry, type === "html" ? htmlEntries : jsonEntries); return entry;
      } catch (error) {
        if (expired) throw new Error("Published data request timed out");
        throw error;
      } finally {
        clearTimeout(timer); if (parentSignal) parentSignal.removeEventListener("abort", abort);
      }
    }
    function resolvedGraph(payload, base) {
      if (!payload || payload.schema !== GRAPH_SCHEMA || !payload.palette || !Array.isArray(payload.nodes) || !Array.isArray(payload.edges))
        throw new Error("Unsupported graph payload");
      var ids = new Set();
      var nodes = payload.nodes.map(function (node) {
        if (!node || typeof node.id !== "string" || typeof node.title !== "string" || ids.has(node.id))
          throw new Error("Invalid graph node");
        ids.add(node.id);
        return Object.assign({}, node, {url: link(node.url, base, true), route: link(node.route, base),
          data_url: link(node.data_url, base), article_url: link(node.article_url, base, true)});
      });
      payload.edges.forEach(function (edge) {
        if (!edge || !ids.has(edge.source) || !ids.has(edge.target)) throw new Error("Unknown graph endpoint");
      });
      var view = Object.assign({}, payload.view || {});
      view.book = link(view.book, base);
      view.breadcrumbs = (view.breadcrumbs || []).map(function (entry) {
        return Object.assign({}, entry, {url: link(entry.url, base)});
      });
      return Object.assign({}, payload, {nodes: nodes, edges: payload.edges.map(function (edge) { return Object.assign({}, edge); }), view: view});
    }
    async function graphAt(url, request) {
      var loaded = await fetchText(url, "json", request.signal); assertAlive(request);
      try { return resolvedGraph(JSON.parse(loaded.text), loaded.url); }
      catch (error) { cacheDelete(jsonCache, resource(url)); throw error; }
    }
    function normalizeIndex(payload, base) {
      if (!payload || payload.schema !== INDEX_SCHEMA || !Array.isArray(payload.nodes)) throw new Error("Unsupported search index");
      var ids = new Map(), byRoute = new Map();
      var nodes = payload.nodes.map(function (node, position) {
        if (!node || typeof node.id !== "string" || typeof node.title !== "string" || ids.has(node.id))
          throw new Error("Invalid search node");
        var copy = Object.assign({}, node, {_index: position, url: link(node.url, base, true), route: link(node.route, base),
          data_url: link(node.data_url, base), article_url: link(node.article_url, base, true)});
        ids.set(copy.id, copy);
        if (copy.route) byRoute.set(copy.route, copy);
        return copy;
      });
      var relations;
      if (payload.relations !== undefined) {
        if (!Array.isArray(payload.relations)) throw new Error("Invalid search relations");
        relations = payload.relations.map(function (row) {
          if (!Array.isArray(row) || row.length !== 4) throw new Error("Invalid search relation");
          var from = typeof row[0] === "string" ? row[0] : Number.isSafeInteger(row[0]) && nodes[row[0]] && nodes[row[0]].id;
          var to = typeof row[1] === "string" ? row[1] : Number.isSafeInteger(row[1]) && nodes[row[1]] && nodes[row[1]].id;
          if (!ids.has(from) || !ids.has(to) || !Number.isSafeInteger(row[2]) || !Number.isSafeInteger(row[3]) ||
              row[2] < 0 || row[3] < 0 || row[2] + row[3] === 0) throw new Error("Invalid search relation endpoint");
          return [from, to, row[2], row[3]];
        });
      }
      indexed = ids; routes = byRoute;
      byRoute.forEach(function (_node, route) { documents.add(resource(route)); });
      return Object.assign({}, payload, {nodes: nodes, relations: relations, url: base});
    }
    function loadIndex() {
      if (index) return Promise.resolve(index);
      if (indexPromise) return indexPromise;
      if (disposed) return Promise.reject(abortError());
      if (!catalogueURL) return Promise.resolve(null);
      indexController = new AbortController();
      indexPromise = fetchText(catalogueURL, "json", indexController.signal).then(function (loaded) {
        if (disposed) throw abortError();
        index = normalizeIndex(JSON.parse(loaded.text), loaded.url);
        if (typeof options.onIndex === "function") {
          try { options.onIndex(index); } catch (error) { report(error, catalogueURL); }
        }
        return index;
      }).catch(function (error) {
        indexPromise = null; cacheDelete(jsonCache, resource(catalogueURL));
        if (!disposed) report(error, catalogueURL); throw error;
      }).finally(function () { indexController = null; });
      return indexPromise;
    }
    function indexedNode(id) {
      return indexed.get(id) || typeof id === "string" && indexed.get(id.replace(/^(?:scope|boundary):/, "")) || null;
    }
    function selection(url, navigation) {
      var hash = new URL(url).hash.slice(1), parameters = new URLSearchParams(hash);
      var explicit = navigation.nodeId && indexedNode(navigation.nodeId);
      var node = explicit || routes.get(url) || indexedNode(parameters.get("node"));
      var nodeId = explicit ? explicit.id : navigation.nodeId || node && node.id || parameters.get("node") || null;
      var anchor = navigation.anchor;
      if (anchor === undefined) {
        try { anchor = hash && !parameters.has("node") ? decodeURIComponent(hash) : null; } catch (_error) { anchor = hash; }
      }
      return {node: node, nodeId: nodeId, anchor: anchor, parameters: parameters.has("node") ? Object.fromEntries(parameters) : {},
        focus: navigation.focus !== undefined ? Boolean(navigation.focus) : Boolean(node && node.result || parameters.has("node"))};
    }
    function copyFragment(element, base) {
      if (!element || /^(SCRIPT|IFRAME|OBJECT|EMBED)$/i.test(element.tagName)) throw new Error("Invalid mathematical reading element");
      var reading = element.cloneNode(true);
      reading.querySelectorAll("script,iframe,object,embed,base,link,style").forEach(function (node) { node.remove(); });
      [reading].concat(Array.from(reading.querySelectorAll("*"))).forEach(function (node) {
        Array.from(node.attributes).forEach(function (attribute) {
          if (/^on/i.test(attribute.name) || attribute.name.toLowerCase() === "srcdoc") node.removeAttribute(attribute.name);
        });
        ["href", "src", "poster", "action", "xlink:href"].forEach(function (name) {
          var value = node.getAttribute(name); if (!value) return;
          if (value.charAt(0) === "#" && node.namespaceURI === "http://www.w3.org/2000/svg" && !/^(a|image)$/i.test(node.tagName)) return;
          var rebased = link(value, base, true);
          if (!rebased && name === "href" && /^(mailto:|tel:)/i.test(value)) rebased = value;
          if (!rebased && name !== "href" && /^data:image\/[a-z0-9.+-]+[;,]/i.test(value)) rebased = value;
          if (rebased) node.setAttribute(name, rebased); else node.removeAttribute(name);
        });
        if (node.hasAttribute("srcset")) {
          var value = node.getAttribute("srcset"), candidates = [], cursor = 0;
          while (cursor < value.length) {
            while (/[\s,]/.test(value.charAt(cursor)) && cursor < value.length) cursor++;
            var start = cursor; while (cursor < value.length && !/\s/.test(value.charAt(cursor))) cursor++;
            var sourceURL = value.slice(start, cursor), ended = /,$/.test(sourceURL);
            sourceURL = sourceURL.replace(/,+$/, ""); var descriptor = "";
            if (!ended) { start = cursor; while (cursor < value.length && value.charAt(cursor) !== ",") cursor++; descriptor = value.slice(start, cursor).trim(); }
            var absolute = link(sourceURL, base, true);
            if (!absolute && /^data:image\/[a-z0-9.+-]+[;,]/i.test(sourceURL)) absolute = sourceURL;
            if (absolute && (!descriptor || /^(?:\d+w|(?:\d+(?:\.\d+)?|\.\d+)x)$/.test(descriptor))) candidates.push(absolute + (descriptor ? " " + descriptor : ""));
            cursor++;
          }
          if (candidates.length) node.setAttribute("srcset", candidates.join(", ")); else node.removeAttribute("srcset");
        }
      });
      return reading;
    }
    function parsedPage(text, base) {
      var parsed = new DOMParser().parseFromString(text, "text/html"), page = parsed.querySelector(".bp-explorer-page");
      var viewer = page && page.querySelector(".bp-dag-viewer") || parsed.querySelector(".bp-dag-viewer");
      var reading = page && page.querySelector(".bp-reading-source") || parsed.querySelector(".bp-reading-source");
      var canonical = viewer && link(attribute(viewer, page, "data-page-href"), base) || base;
      var heading = reading && reading.querySelector("h1");
      return {url: canonical, documentURL: base, title: reading && reading.getAttribute("data-page-title") ||
        page && page.getAttribute("data-page-title") || heading && heading.textContent.trim() || parsed.title,
        scope: viewer && attribute(viewer, page, "data-scope-id") || null,
        readingSource: reading, dataURL: viewer && link(attribute(viewer, page, "data-graph-src"), base)};
    }
    async function prepare(url, navigation, request, initial) {
      if (catalogueURL) await waitFor(loadIndex().catch(function () { return null; }), request.signal);
      assertAlive(request);
      var choice = selection(url, navigation), canonical = choice.node && choice.node.route || url;
      var seen = new Set(), page, graph, dataURL;
      for (var depth = 0; depth < 5; depth++) {
        assertAlive(request);
        var key = resource(canonical); if (seen.has(key)) throw new Error("Canonical reading link forms a loop"); seen.add(key);
        if (initial && key === resource(initialURL)) {
          page = {url: initialCanonical, documentURL: initialURL, title: initialTitle, scope: initialScope,
            readingSource: initialSource, dataURL: initialDataURL}; initial = false;
        } else {
          var loaded = await fetchText(canonical, "html", request.signal); assertAlive(request);
          page = parsedPage(loaded.text, loaded.url);
        }
        // Alias shells may contain only a reading notice: fetch the declared canonical page first.
        if (resource(page.url) !== resource(page.documentURL)) { canonical = page.url; continue; }
        dataURL = page.dataURL || choice.node && choice.node.data_url;
        graph = dataURL ? (options.initialGraph && key === resource(initialURL) ?
          resolvedGraph(JSON.parse(JSON.stringify(options.initialGraph)), dataURL) : await graphAt(dataURL, request)) : null;
        assertAlive(request);
        if (!page.readingSource) {
          var book = graph && graph.view && graph.view.book;
          if (!book) {
            cacheDelete(htmlCache, resource(canonical));
            throw new Error("This page has no published mathematical reading content");
          }
          canonical = choice.node && choice.node.route || book; continue;
        }
        var target = new URL(page.url), requested = new URL(canonical);
        if (choice.node && choice.node.route) target = new URL(choice.node.route);
        else if (requested.hash && resource(requested.href) === resource(target.href)) target.hash = requested.hash;
        if (choice.nodeId && !(choice.node && choice.node.route) && !target.hash) {
          var legacy = new URLSearchParams(); legacy.set("node", choice.nodeId); target.hash = legacy.toString();
        }
        var selected = selection(target.href, Object.assign({}, navigation, {nodeId: choice.nodeId}));
        documents.add(resource(target.href));
        return {url: target.href, documentURL: page.documentURL, title: selected.node && selected.node.result ? selected.node.title : page.title || graph && graph.title || "Mathematics",
          scope: page.scope || selected.node && selected.node.scope || null, readingSource: copyFragment(page.readingSource, page.documentURL),
          graph: graph, dataURL: dataURL, nodeId: selected.nodeId, anchor: selected.anchor, focus: selected.focus,
          parameters: choice.parameters, snapshot: navigation.snapshot || null, reason: navigation.reason || "navigate",
          isCurrent: function () { return alive(request); }};
      }
      throw new Error("Too many canonical reading redirects");
    }
    function historyState(snapshot, url) {
      var entry = Object.assign({}, win.history.state || {});
      entry[HISTORY_KEY] = {version: 1, url: url, snapshot: snapshot}; return entry;
    }
    function capture() {
      return typeof options.captureState === "function" ? options.captureState() : null;
    }
    function saveCurrent() {
      if (!current || current.url !== win.location.href) return;
      try { win.history.replaceState(historyState(capture(), current.url), "", current.url); }
      catch (error) { report(error, current.url); }
    }
    // Snapshots are renderer-owned data. The router owns only the versioned
    // autoformNavigation history namespace and never changes the permalink here.
    function checkpoint(method) {
      if (disposed || !current || current.url !== win.location.href) return null;
      var mode = method === "push" ? "pushState" : "replaceState";
      try {
        var snapshot = capture(); win.history[mode](historyState(snapshot, current.url), "", current.url); return snapshot;
      }
      catch (error) { report(error, current.url); return null; }
    }
    async function commit(bundle, navigation, request) {
      assertAlive(request);
      var method = navigation.history === undefined ? "push" : navigation.history;
      if (method !== false && method !== "none") {
        saveCurrent();
        var state = historyState(bundle.snapshot, bundle.url);
        win.history[method === "replace" || bundle.url === win.location.href ? "replaceState" : "pushState"](state, "", bundle.url);
      } else if (bundle.url !== win.location.href) {
        // Replace an old explorer alias, never add an entry during a history restoration.
        win.history.replaceState(historyState(bundle.snapshot, bundle.url), "", bundle.url);
      }
      current = bundle;
      await options.onCommit(bundle); assertAlive(request);
      bound.forEach(function (listener, root) {
        if (root.isConnected === false) { root.removeEventListener("click", listener, true); bound.delete(root); }
      });
      if (bundle.reason === "start") initialSource = null;
      if (bundle.snapshot && typeof options.restoreState === "function") await options.restoreState(bundle.snapshot, bundle);
      assertAlive(request); saveCurrent(); return bundle;
    }
    async function run(url, navigation, initial) {
      if (disposed) return null;
      var safe = link(url, initialURL); if (!safe) { var invalid = new Error("Not a same-origin book link"); report(invalid, url); return null; }
      var request = fresh();
      try { return await commit(await prepare(safe, navigation, request, initial), navigation, request); }
      catch (error) {
        if (alive(request)) report(error, safe);
        return null;
      } finally {
        if (!disposed && request.epoch === epoch) host.removeAttribute("aria-busy");
      }
    }
    function navigate(url, navigation) { return run(url, navigation || {}, false); }
    async function navigateNode(id, navigation) {
      if (disposed) return null;
      var request = fresh(), settings = Object.assign({}, navigation || {}, {nodeId: id});
      try {
        await waitFor(loadIndex(), request.signal); assertAlive(request);
        var node = indexedNode(id), url = node && (node.route || node.url);
        if (!url) throw new Error("No published route for this article");
        settings.nodeId = node.id;
        if (settings.focus === undefined) settings.focus = node.result === undefined ? node.kind !== "scope" : Boolean(node.result);
        return await commit(await prepare(url, settings, request, false), settings, request);
      } catch (error) { if (alive(request)) report(error, url || id); return null; }
      finally { if (!disposed && request.epoch === epoch) host.removeAttribute("aria-busy"); }
    }
    function internal(url) {
      var safe = link(url, initialURL); if (!safe) return false;
      var path = new URL(safe).pathname;
      try { path = decodeURIComponent(path); } catch (_error) { return false; }
      if (/\/[^/]+\.[^/]+$/.test(path) && !/\.html?$/i.test(path)) return false;
      return documents.has(resource(safe)) || path.indexOf(sitePrefix + "roadmap/") === 0 ||
        path === sitePrefix + "dependencies.html" || path.indexOf(sitePrefix + "dependencies/") === 0;
    }
    function bindReader(root) {
      if (!root || bound.has(root) || disposed) return;
      function click(event) {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        var anchor = event.target.closest && event.target.closest("a[href]");
        if (!anchor || !root.contains(anchor) || anchor.hasAttribute("download") || anchor.target && anchor.target !== "_self") return;
        var href = anchor.href;
        if (!internal(href)) return;
        // Ordinary in-article section anchors stay native; article permalinks change the graph as well.
        if (current && resource(href) === resource(current.url) && new URL(href).hash && !routes.has(href) &&
            !new URLSearchParams(new URL(href).hash.slice(1)).has("node")) return;
        event.preventDefault(); event.stopPropagation(); navigate(href);
      }
      root.addEventListener("click", click, true); bound.set(root, click);
    }
    function popstate() {
      if (disposed || !internal(win.location.href)) return;
      var entry = win.history.state && win.history.state[HISTORY_KEY];
      var url = new URL(win.location.href);
      if (current && resource(current.url) === resource(url.href) && url.hash && !routes.has(url.href) &&
          !new URLSearchParams(url.hash.slice(1)).has("node")) {
        // Native section anchors scroll the existing reading DOM. They must
        // not discard the selected theorem or replace its full reading pane.
        var request = fresh(), anchor;
        try { anchor = decodeURIComponent(url.hash.slice(1)); } catch (_error) { anchor = url.hash.slice(1); }
        current.url = url.href; current.anchor = anchor; current.isCurrent = function () { return alive(request); };
        host.removeAttribute("aria-busy");
        if (entry && entry.version === 1 && entry.snapshot && typeof options.restoreState === "function") {
          Promise.resolve(options.restoreState(entry.snapshot, current)).catch(function (error) { report(error, url.href); });
        }
        return;
      }
      navigate(win.location.href, {history: "none", snapshot: entry && entry.version === 1 ? entry.snapshot : null, reason: "popstate"});
    }
    function start() {
      if (started) return started;
      if (disposed) return Promise.resolve(null);
      win.addEventListener("popstate", popstate);
      if (source) bindReader(source);
      var entry = win.history.state && win.history.state[HISTORY_KEY];
      started = run(initialURL, {history: "replace", snapshot: entry && entry.version === 1 ? entry.snapshot : null, reason: "start"}, true);
      return started;
    }
    function dispose() {
      if (disposed) return;
      disposed = true; epoch++;
      host.removeAttribute("aria-busy");
      if (active) active.abort(); if (indexController) indexController.abort();
      win.removeEventListener("popstate", popstate);
      bound.forEach(function (listener, root) { root.removeEventListener("click", listener, true); }); bound.clear();
      htmlCache.clear(); jsonCache.clear(); cacheBytes = 0; index = null; indexed.clear(); routes.clear(); documents.clear();
      current = null; initialSource = null; source = null; indexPromise = null; started = null;
    }
    return {start: start, navigate: navigate, navigateNode: navigateNode, loadIndex: loadIndex,
      getIndex: function () { return index; }, bindReader: bindReader, checkpoint: checkpoint, dispose: dispose};
  }

  window.AutoformNavigation = {create: create};
})();
