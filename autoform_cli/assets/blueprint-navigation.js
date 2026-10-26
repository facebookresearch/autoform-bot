/* Shared book/explorer navigation. Reading HTML is published by Autoform, not made from graph summaries.
 * create({host,onCommit,captureState,restoreState,onIndex,onError}) keeps one renderer-owned shell.
 * onCommit receives a detached readingSource, canonical url, resolved graph/dataURL, nodeId/anchor,
 * scope/title, snapshot, reason and isCurrent(), after URL/history commit. A renderer failure
 * is reported without promising DOM rollback. No math engine is owned or called here.
 * checkpoint() replaces only autoformNavigation:{version:1,url,key,snapshot}; checkpoint('push') records
 * a deliberate view/filter action without changing the canonical permalink. Snapshot fields are opaque.
 * declutterReader(root, representedScopeIds) hides duplicated pure child-scope indexes only in the
 * enhanced reader clone, after the renderer has supplied its actual semantic-outline coverage.
 * readerIsOverview(root) conservatively recognizes that owned, navigation-only reading projection.
 * loadWikiLinks()/getWikiLinks() expose a lazy optional, exact-index-SHA-bound hyperlink layer;
 * onWikiLinks receives canonical IDs and honest coverage, never mathematical dependencies.
 * getHomeURL() names the site's native home page, separately from the indexed roadmap root.
 */
(function () {
  "use strict";
  if (window.AutoformNavigation) return;

  var HISTORY_KEY = "autoformNavigation", GRAPH_SCHEMA = "autoform-dag-view/v2", INDEX_SCHEMA = "autoform-dag-search/v1";
  var WIKI_SCHEMA = "autoform-wiki-links/v1", WIKI_LIMIT = 32 * 1024 * 1024;

  function create(options) {
    options = options || {};
    var host = options.host, doc = options.document || document, win = options.window || window;
    if (!host || typeof options.onCommit !== "function") throw new Error("Navigation needs a host and onCommit callback");
    var origin = new URL(win.location.href).origin, initialURL = win.location.href;
    var initialPage = host.closest(".bp-explorer-page") || doc.querySelector(".bp-explorer-page");
    var source = initialPage && initialPage.querySelector(".bp-reading-source") || doc.querySelector(".bp-reading-source");
    var initialSource = source && source.cloneNode(true);
    var catalogueURL = link(attribute(host, initialPage, "data-catalog-src"), initialURL);
    var wikiURL = catalogueURL && link("wiki-links.json", catalogueURL);
    var initialDataURL = link(attribute(host, initialPage, "data-graph-src"), initialURL);
    var initialCanonical = link(attribute(host, initialPage, "data-page-href"), initialURL) || initialURL;
    var initialScope = attribute(host, initialPage, "data-scope-id") || null;
    var initialTitle = source && source.getAttribute("data-page-title") ||
      initialPage && initialPage.getAttribute("data-page-title") || doc.title || "Mathematics";
    var disposed = false, started = null, epoch = 0, active = null, current = null, currentKey = null, currentFailed = false;
    var scrollChecked = false, scrollOwned = false, historySnapshotsTrusted = true;
    var snapshots = new Map(), keySequence = 0, keySeed = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
    var index = null, indexPromise = null, indexController = null, indexed = new Map(), routes = new Map(), documents = new Set();
    var indexDigest = null, wikiLinks = null, wikiPromise = null, wikiController = null, wikiGeneration = 0;
    var bound = new Map(), htmlCache = new Map(), jsonCache = new Map(), cacheBytes = 0;
    var decluttered = new WeakMap();
    var timeout = bounded(options.timeout, 15000, 1000, 60000);
    var htmlLimit = bounded(options.htmlLimit, 8 * 1024 * 1024, 1024, 32 * 1024 * 1024);
    var jsonLimit = bounded(options.jsonLimit, 32 * 1024 * 1024, 1024, 64 * 1024 * 1024);
    var cacheLimit = bounded(options.cacheBytes, 48 * 1024 * 1024, 1024, 128 * 1024 * 1024);
    var htmlEntries = bounded(options.htmlEntries, 8, 1, 32), jsonEntries = bounded(options.jsonEntries, 16, 1, 64);
    var sitePrefix = prefix(catalogueURL, initialDataURL, initialCanonical), homeURL = link(sitePrefix + "index.html", initialURL);
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
        var victim = htmlCache.size ? htmlCache : jsonCache.size ? jsonCache : snapshots, id = victim.keys().next().value;
        cacheBytes -= victim.get(id).bytes; victim.delete(id);
      }
    }
    function digestUnavailable() { return null; }
    function digestHex(buffer) {
      try {
        var bytes = new Uint8Array(buffer);
        return bytes.byteLength === 32 ? Array.from(bytes).map(function (byte) { return byte.toString(16).padStart(2, "0"); }).join("") : null;
      } catch (_error) { return null; }
    }
    function captureIndexDigest(bytes) {
      // Hash the existing exact input before decoding; retain no JS byte buffers
      // or closure over them. Native WebCrypto's one input copy is bounded.
      try {
        var subtle = win.crypto && win.crypto.subtle;
        if (!subtle || typeof subtle.digest !== "function" || bytes.byteLength > WIKI_LIMIT) return Promise.resolve(null);
        return subtle.digest("SHA-256", bytes).then(digestHex, digestUnavailable);
      } catch (_error) { return Promise.resolve(null); }
    }
    async function fetchText(value, type, parentSignal, maximum) {
      var href = link(value, initialURL); if (!href) throw new Error("Refusing an external data request");
      var key = resource(href), cache = type === "html" ? htmlCache : jsonCache;
      var limit = type === "html" ? htmlLimit : jsonLimit;
      if (Number.isSafeInteger(maximum)) limit = Math.min(limit, maximum);
      var cached = cacheGet(cache, key);
      if (cached) { if (cached.bytes > limit) throw new Error("Published data is too large"); return cached; }
      var controller = new AbortController(), expired = false;
      function abort() { controller.abort(); }
      if (parentSignal) {
        if (parentSignal.aborted) throw abortError();
        parentSignal.addEventListener("abort", abort, {once: true});
      }
      var timer = setTimeout(function () { expired = true; controller.abort(); }, timeout);
      try {
        var response = await (options.fetch || win.fetch.bind(win))(key, {credentials: "same-origin", signal: controller.signal});
        if (!response.ok) throw new Error("HTTP " + response.status);
        var responseURL = link(response.url || key, key); if (!responseURL) throw new Error("Data redirected outside this site");
        if (Number(response.headers.get("content-length")) > limit) throw new Error("Published data is too large");
        var text, length, digest = null, isCatalogue = type === "json" && catalogueURL && key === resource(catalogueURL);
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
          if (isCatalogue) digest = captureIndexDigest(bytes);
          text = new TextDecoder("utf-8", {fatal: true}).decode(bytes); length = total;
          bytes = null; chunks = null;
        } else if (isCatalogue && typeof response.arrayBuffer === "function") {
          var raw = new Uint8Array(await response.arrayBuffer());
          if (raw.byteLength > limit) throw new Error("Published data is too large");
          digest = captureIndexDigest(raw);
          text = new TextDecoder("utf-8", {fatal: true}).decode(raw); length = raw.byteLength; raw = null;
        } else {
          // Older text-only peers remain readable, but cannot prove byte identity.
          text = await response.text(); length = new TextEncoder().encode(text).byteLength;
          if (length > limit) throw new Error("Published data is too large");
        }
        if (parentSignal && parentSignal.aborted) throw abortError();
        var entry = {text: text, url: responseURL, bytes: length, indexDigest: digest};
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
          data_url: link(node.data_url, base)});
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
    async function normalizeIndex(payload, base) {
      if (!payload || payload.schema !== INDEX_SCHEMA || !Array.isArray(payload.nodes)) throw new Error("Unsupported search index");
      var ids = new Map(), byRoute = new Map(), knownDocuments = new Set(), nodes = [];
      async function yieldIndex() {
        // Timer tasks can run back-to-back without a WebKit paint while a
        // large catalogue is normalized. Give visible readers an actual
        // frame; hidden/stalled frames retain a bounded timer fallback.
        await new Promise(function (resolve) {
          var visible = doc.visibilityState !== "hidden" && typeof win.requestAnimationFrame === "function" &&
            typeof win.cancelAnimationFrame === "function";
          var frame = null, timer = setTimeout(done, visible ? 50 : 0), finished = false;
          function done() {
            if (finished) return;
            finished = true; clearTimeout(timer);
            if (frame !== null) win.cancelAnimationFrame(frame);
            resolve();
          }
          if (visible) frame = win.requestAnimationFrame(done);
        });
        if (disposed) throw abortError();
      }
      for (var position = 0; position < payload.nodes.length; position++) {
        var node = payload.nodes[position];
        if (!node || typeof node.id !== "string" || typeof node.title !== "string" || ids.has(node.id))
          throw new Error("Invalid search node");
        var copy = Object.assign({}, node, {_index: position, url: link(node.url, base, true), route: link(node.route, base),
          data_url: link(node.data_url, base)});
        // Parent offsets use the same node order as compact relation endpoints.
        // Decode before publication; absent and legacy string parents stay intact.
        if (typeof node.parent === "number") {
          var parent = Number.isSafeInteger(node.parent) && node.parent >= 0 && payload.nodes[node.parent];
          if (!parent || typeof parent.id !== "string") throw new Error("Invalid search parent endpoint");
          copy.parent = parent.id;
        }
        ids.set(copy.id, copy);
        if (copy.route) { byRoute.set(copy.route, copy); knownDocuments.add(resource(copy.route)); }
        nodes.push(copy);
        if ((position + 1) % 512 === 0) await yieldIndex();
      }
      var relations;
      if (payload.relations !== undefined) {
        if (!Array.isArray(payload.relations)) throw new Error("Invalid search relations");
        relations = [];
        for (var edge = 0; edge < payload.relations.length; edge++) {
          var row = payload.relations[edge];
          if (!Array.isArray(row) || row.length !== 4) throw new Error("Invalid search relation");
          var from = typeof row[0] === "string" ? row[0] : Number.isSafeInteger(row[0]) && nodes[row[0]] && nodes[row[0]].id;
          var to = typeof row[1] === "string" ? row[1] : Number.isSafeInteger(row[1]) && nodes[row[1]] && nodes[row[1]].id;
          if (!ids.has(from) || !ids.has(to) || !Number.isSafeInteger(row[2]) || !Number.isSafeInteger(row[3]) ||
              row[2] < 0 || row[3] < 0 || row[2] + row[3] === 0) throw new Error("Invalid search relation endpoint");
          relations.push([from, to, row[2], row[3]]);
          if ((edge + 1) % 1024 === 0) await yieldIndex();
        }
      }
      if (disposed) throw abortError();
      indexed = ids; routes = byRoute;
      knownDocuments.forEach(function (route) { documents.add(route); });
      return Object.assign({}, payload, {nodes: nodes, relations: relations, url: base});
    }
    function loadIndex() {
      if (index) return Promise.resolve(index);
      if (indexPromise) return indexPromise;
      if (disposed) return Promise.reject(abortError());
      if (!catalogueURL) return Promise.resolve(null);
      indexController = new AbortController();
      indexPromise = fetchText(catalogueURL, "json", indexController.signal).then(async function (loaded) {
        if (disposed) throw abortError();
        var normalized = await normalizeIndex(JSON.parse(loaded.text), loaded.url);
        if (disposed) throw abortError();
        index = normalized; indexDigest = loaded.indexDigest;
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
    function unavailableWiki(reason, digest) {
      return {schema: WIKI_SCHEMA, index_sha256: digest || null, availability: "unavailable",
        references: [], unavailable_sources: [], reason: reason};
    }
    async function normalizeWikiLinks(payload, digest, ids, signal) {
      function invalid() { return unavailableWiki("invalid_layer", digest); }
      if (!payload || payload.schema !== WIKI_SCHEMA || typeof payload.index_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(payload.index_sha256) ||
          ["complete", "partial", "unavailable"].indexOf(payload.availability) < 0 ||
          !Array.isArray(payload.references) || !Array.isArray(payload.unavailable_sources) ||
          payload.reason !== undefined && typeof payload.reason !== "string") return invalid();
      if (payload.index_sha256 !== digest) return unavailableWiki("stale_index", digest);
      if (payload.availability === "complete" && payload.unavailable_sources.length) return invalid();
      function offset(value) { return Number.isSafeInteger(value) && value >= 0 && value < ids.length; }
      async function pause() { await waitFor(new Promise(function (resolve) { setTimeout(resolve, 0); }), signal); }
      // Only this parser-owned sidecar is normalized, never the index or its
      // formal relations. Reuse its arrays instead of retaining another copy.
      for (var position = 0; position < payload.references.length; position++) {
        var row = payload.references[position];
        if (!Array.isArray(row) || row.length !== 2 || !offset(row[0]) || !offset(row[1])) return invalid();
        row[0] = ids[row[0]]; row[1] = ids[row[1]];
        if ((position + 1) % 1024 === 0) await pause();
      }
      for (var source = 0; source < payload.unavailable_sources.length; source++) {
        if (!offset(payload.unavailable_sources[source])) return invalid();
        payload.unavailable_sources[source] = ids[payload.unavailable_sources[source]];
        if ((source + 1) % 512 === 0) await pause();
      }
      if (signal.aborted || disposed) throw abortError();
      var normalized = {schema: WIKI_SCHEMA, index_sha256: digest, availability: payload.availability,
        references: payload.references, unavailable_sources: payload.unavailable_sources};
      // Coverage remains public; arbitrary publisher/provider error text does not.
      if (payload.availability !== "complete") normalized.reason = payload.availability === "partial" ? "partial_sources" : "source_unavailable";
      return normalized;
    }
    function loadWikiLinks() {
      if (disposed) return Promise.resolve(null);
      if (wikiLinks && wikiLinks.availability !== "unavailable") return Promise.resolve(wikiLinks);
      if (wikiPromise) return wikiPromise;
      var generation = ++wikiGeneration, controller = wikiController = new AbortController(), digest = null, expired = false;
      var timer = setTimeout(function () { expired = true; controller.abort(); }, timeout);
      function finish(value) {
        if (disposed || generation !== wikiGeneration) return null;
        if (value.availability === "unavailable" && wikiURL) cacheDelete(jsonCache, resource(wikiURL));
        wikiLinks = value;
        if (typeof options.onWikiLinks === "function") {
          try { options.onWikiLinks(value); } catch (_ignored) { /* Optional observers cannot disable mathematical navigation. */ }
        }
        return disposed || generation !== wikiGeneration ? null : value;
      }
      wikiPromise = (async function () {
        var loadedIndex = await waitFor(loadIndex(), controller.signal);
        if (!loadedIndex || !wikiURL) return finish(unavailableWiki("index_unavailable"));
        digest = await waitFor(indexDigest || Promise.resolve(null), controller.signal);
        if (!digest) return finish(unavailableWiki("index_unverified"));
        var loaded = await waitFor(fetchText(wikiURL, "json", controller.signal, WIKI_LIMIT), controller.signal), payload;
        try { payload = JSON.parse(loaded.text); } catch (_error) { return finish(unavailableWiki("invalid_layer", digest)); }
        // Map insertion order is the original validated offset identity, even
        // if a view reorders the public nodes array after receiving the index.
        var normalized = await normalizeWikiLinks(payload, digest, Array.from(indexed.keys()), controller.signal);
        if (loadedIndex !== index) return null;
        return finish(normalized);
      })().catch(function () {
        return finish(unavailableWiki(expired ? "request_timed_out" : "request_unavailable", digest));
      }).finally(function () {
        clearTimeout(timer);
        if (generation === wikiGeneration) { wikiPromise = null; wikiController = null; }
      });
      return wikiPromise;
    }
    function indexedNode(id) {
      return indexed.get(id) || typeof id === "string" && indexed.get(id.replace(/^(?:scope|boundary):/, "")) || null;
    }
    function legacyHash(hash) {
      var parameters = new URLSearchParams(hash), seen = new Set();
      if (parameters.has("node")) return true;
      var entries = Array.from(parameters);
      return entries.length > 0 && entries.every(function (entry) {
        var key = entry[0], value = entry[1]; if (seen.has(key)) return false; seen.add(key);
        if (key === "view") return value === "graph" || value === "list";
        if (key === "direction") return ["both", "prerequisites", "dependents"].indexOf(value) >= 0;
        if (key === "isolates") return value === "0" || value === "1";
        if (key === "status") return value === "-" || value.split(",").every(function (status) {
          return /^(?:mathlib|fully_proved|proved|defined|conditional|can_prove|stated|can_state|not_ready|planned|inventory_checked|inventory_pending)$/.test(status);
        });
        return false;
      });
    }
    function indexedRoute(value) {
      var exact = routes.get(value); if (exact) return exact;
      // Only route lookup recognizes MkDocs's two directory entrances. Fetch,
      // cache and canonical-source identity still use the exact resources.
      var url = new URL(value);
      if (/\/$/.test(url.pathname)) url.pathname += "index.html";
      else if (/\/index\.html$/.test(url.pathname)) url.pathname = url.pathname.slice(0, -10);
      else return null;
      return routes.get(url.href) || null;
    }
    function selection(url, navigation) {
      var hash = new URL(url).hash.slice(1), parameters = new URLSearchParams(hash), legacy = legacyHash(hash);
      var explicit = navigation.nodeId && indexedNode(navigation.nodeId);
      var home = url === homeURL && !navigation.nodeId && !parameters.has("node");
      var node = explicit || (home ? null : indexedRoute(url)) || indexedNode(parameters.get("node"));
      var nodeId = explicit ? explicit.id : navigation.nodeId || node && node.id || parameters.get("node") || null;
      var anchor = navigation.anchor;
      if (anchor === undefined) {
        try { anchor = hash && !legacy ? decodeURIComponent(hash) : null; } catch (_error) { anchor = hash; }
      }
      return {node: node, nodeId: nodeId, anchor: anchor, parameters: legacy ? Object.fromEntries(parameters) : {},
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
      var requestedHash = new URL(url).hash;
      if (legacyHash(requestedHash.slice(1))) requestedHash = "";
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
        if (page.readingSource && choice.node && choice.node.route && resource(page.url) !== resource(choice.node.route)) {
          cacheDelete(htmlCache, resource(canonical)); cacheDelete(htmlCache, resource(choice.node.route));
          throw new Error("Published reading route contradicts the requested article");
        }
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
        // An ordinary section belongs to the complete alias target. An exact
        // indexed statement permalink or legacy node selection still wins.
        if (requestedHash && !(choice.node && choice.node.route && new URL(choice.node.route).hash)) target.hash = requestedHash;
        if (choice.nodeId && !(choice.node && choice.node.route) && !target.hash) {
          var legacy = new URLSearchParams(); legacy.set("node", choice.nodeId); target.hash = legacy.toString();
        }
        var selected = selection(target.href, Object.assign({}, navigation, {nodeId: choice.nodeId}));
        documents.add(resource(target.href));
        return {url: target.href, documentURL: page.documentURL, title: selected.node && selected.node.result ? selected.node.title : page.title || graph && graph.title || "Mathematics",
          scope: page.scope || selected.node && selected.node.scope || null, readingSource: copyFragment(page.readingSource, page.documentURL),
          graph: graph, dataURL: dataURL, nodeId: selected.nodeId, anchor: selected.anchor, focus: selected.focus,
          mapFirst: navigation.mapFirst === true,
          parameters: choice.parameters, snapshot: navigation.snapshot || null, reason: navigation.reason || "navigate",
          isCurrent: function () { return alive(request); }};
      }
      throw new Error("Too many canonical reading redirects");
    }
    function newKey() { return keySeed + "-" + (++keySequence).toString(36); }
    function entryState() {
      var entry = win.history.state && win.history.state[HISTORY_KEY];
      return entry && entry.version === 1 ? entry : null;
    }
    function entryKey(entry, url) {
      return entry && entry.url === url && typeof entry.key === "string" && entry.key ? entry.key : newKey();
    }
    function copyState(snapshot) {
      return typeof structuredClone === "function" ? structuredClone(snapshot) :
        snapshot == null ? snapshot : JSON.parse(JSON.stringify(snapshot));
    }
    function remember(key, snapshot) {
      if (!key) return;
      cacheDelete(snapshots, key);
      var serialized;
      try {
        serialized = JSON.stringify(snapshot, function (name, value) {
          var original = this[name], prototype = original && typeof original === "object" && Object.getPrototypeOf(original);
          // Unmeasurable opaque values still live in browser history; never retain an unbounded clone.
          if (prototype && prototype !== Object.prototype && !Array.isArray(original)) throw new Error();
          return value;
        });
      } catch (_error) { return; }
      if (typeof serialized !== "string") return;
      var bytes = new TextEncoder().encode(serialized).byteLength;
      if (bytes > 256 * 1024) return;
      // Snapshots share the existing total byte budget with HTML/JSON and keep only 32 recent entries.
      cachePut(snapshots, key, {snapshot: copyState(snapshot), bytes: bytes}, 32);
    }
    function outgoing() {
      if (!current || currentFailed) return null;
      var snapshot = capture(); remember(currentKey, snapshot); return snapshot;
    }
    function historyState(snapshot, url, key) {
      var entry = Object.assign({}, win.history.state || {});
      entry[HISTORY_KEY] = {version: 1, url: url, key: key, snapshot: snapshot}; return entry;
    }
    function capture() {
      return typeof options.captureState === "function" ? options.captureState() : null;
    }
    function saveCurrent() {
      if (!current || currentFailed || current.url !== win.location.href) return;
      try { win.history.replaceState(historyState(outgoing(), current.url, currentKey), "", current.url); }
      catch (error) { report(error, current.url); }
    }
    // Snapshots are renderer-owned data. The router owns only the versioned
    // autoformNavigation history namespace and never changes the permalink here.
    function checkpoint(method) {
      if (disposed || !current || currentFailed || current.url !== win.location.href) return null;
      var mode = method === "push" ? "pushState" : "replaceState";
      try {
        var snapshot = capture(), key = method === "push" ? newKey() : currentKey;
        win.history[mode](historyState(snapshot, current.url, key), "", current.url);
        currentKey = key; remember(key, snapshot); return snapshot;
      }
      catch (error) { report(error, current.url); return null; }
    }
    async function commit(bundle, navigation, request) {
      assertAlive(request);
      var method = navigation.history === undefined ? "push" : navigation.history;
      var push = method !== false && method !== "none" && method !== "replace" && bundle.url !== win.location.href;
      var key = navigation.entryKey || (push ? newKey() : currentKey || entryKey(entryState(), win.location.href));
      if (method !== false && method !== "none") {
        saveCurrent();
        var state = historyState(bundle.snapshot, bundle.url, key);
        win.history[push ? "pushState" : "replaceState"](state, "", bundle.url);
      } else if (bundle.url !== win.location.href) {
        // Replace an old explorer alias, never add an entry during a history restoration.
        win.history.replaceState(historyState(bundle.snapshot, bundle.url, key), "", bundle.url);
      }
      current = bundle; currentKey = key; currentFailed = false;
      if (!scrollChecked) {
        scrollChecked = true;
        if (win.history.scrollRestoration === "auto") { win.history.scrollRestoration = "manual"; scrollOwned = true; }
      }
      // onCommit reconciles a committed route with the renderer; it may have
      // already moved the reader before failing. Do not promise a rollback.
      try {
        await options.onCommit(bundle); assertAlive(request);
        bound.forEach(function (listener, root) {
          if (root.isConnected === false) { root.removeEventListener("click", listener, true); bound.delete(root); }
        });
        initialSource = null; source = null;
        if (bundle.snapshot && typeof options.restoreState === "function") await options.restoreState(bundle.snapshot, bundle);
        assertAlive(request); saveCurrent(); return bundle;
      }
      catch (error) { throw rendererFailure(error, bundle, request, key); }
    }
    function rendererFailure(error, bundle, request, key) {
      if (!alive(request) || current !== bundle) return error;
      // A rejected renderer cannot supply an opaque snapshot for this URL.
      // Keep the committed canonical route, but never poison Back/Forward.
      currentFailed = true;
      snapshots.forEach(function (_entry, id) { cacheDelete(snapshots, id); });
      // Failed pending checkpoints may occupy earlier browser entries.
      // If those cannot be invalidated, use only fresh private snapshots.
      if (currentKey !== key) historySnapshotsTrusted = false;
      try { win.history.replaceState(historyState(null, bundle.url, currentKey), "", bundle.url); }
      catch (_ignored) { historySnapshotsTrusted = false; }
      var failure = new Error(error && error.message || String(error));
      failure.url = bundle.url; return failure;
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
    function declutterReader(root, representedScopeIds) {
      if (!root) return 0;
      // Restore only changes this instance made. Never trust authored marker
      // attributes or unhide something the original source deliberately hid.
      (decluttered.get(root) || []).forEach(function (entry) { entry.node.hidden = entry.hidden; });
      decluttered.delete(root);
      if (disposed || !index || !current || !root.matches || !root.matches(".bp-reading-body") ||
          !bound.has(root) || !host.contains(root) || !Array.isArray(representedScopeIds)) return 0;
      var reading = root.querySelector(".bp-reading-source");
      var owner = reading && indexed.get(reading.getAttribute("data-autoform-node-id"));
      function scope(node) {
        return node && node.kind === "scope" && node.scope === node.id && node.result !== true &&
          ["collection", "transparent", "source"].indexOf(node.navigation) < 0;
      }
      if (!scope(owner) || owner.id !== current.scope) return 0;
      var represented = new Set(representedScopeIds), changed = [], lists = 0;
      var protectedContext = ".bp-node,.bp-thmwrapper,.bp-proof,.bp-lean-source,.footnote,.references," +
        "[role='doc-bibliography'],[role='doc-endnotes']";
      var protectedContent = ".arithmatex,math,mjx-container,code,pre,table,blockquote,img,svg,cite,sup," +
        ".bp-thmwrapper,.bp-source-link,.citation,[data-cites]";
      // These are the publisher's conventional provenance/mathematical
      // section anchors, used only to preserve content, never to select an index.
      var protectedAnchor = /^(?:sources?|references?|bibliograph(?:y|ies)|proof|statement|notation|depends-on)(?:-|$)/i;
      function protectedSection(heading) {
        if (heading.closest(protectedContext) || heading.querySelector(protectedContent) || /[$\\]/.test(heading.textContent)) return true;
        var level = Number(heading.tagName.slice(1)), previous = heading;
        while (previous) {
          if (/^H[1-6]$/.test(previous.tagName) && Number(previous.tagName.slice(1)) <= level) {
            if (protectedAnchor.test(previous.id || "") || /[$\\]/.test(previous.textContent)) return true;
            level = Number(previous.tagName.slice(1)) - 1;
          }
          previous = previous.previousElementSibling;
        }
        return false;
      }
      function directChild(node) {
        var parent = indexed.get(node.parent), seen = new Set();
        while (parent && ["collection", "transparent"].indexOf(parent.navigation) >= 0 && !seen.has(parent.id)) {
          seen.add(parent.id); parent = indexed.get(parent.parent);
        }
        return parent && parent.id === owner.id;
      }
      function pureList(list) {
        if (!/^(?:UL|OL)$/.test(list.tagName) || list.hidden || !list.children.length || list.querySelector(protectedContent)) return false;
        return Array.from(list.children).every(function (item) {
          if (item.tagName !== "LI") return false;
          var holder = item.children.length === 1 && item.children[0].tagName === "P" ? item.children[0] : item;
          if (holder.children.length !== 1 || holder.children[0].tagName !== "A") return false;
          var anchor = holder.children[0], href = link(anchor.getAttribute("href"), current.documentURL);
          var node = href && !new URL(href).hash && routes.get(href);
          if (!scope(node) || !represented.has(node.id) || !directChild(node) || anchor.textContent.trim() !== node.title) return false;
          var remainder = item.cloneNode(true); remainder.querySelector("a").remove();
          return !remainder.textContent.trim();
        });
      }
      function hide(node) { changed.push({node: node, hidden: node.hidden}); node.hidden = true; }
      reading.querySelectorAll("h2,h3,h4,h5,h6").forEach(function (heading) {
        if (heading.hidden || protectedSection(heading)) return;
        var block = [], next = heading.nextElementSibling, level = Number(heading.tagName.slice(1));
        while (next && !(/^H[1-6]$/.test(next.tagName) && Number(next.tagName.slice(1)) <= level) && !next.matches(".bp-book-nav")) {
          block.push(next); next = next.nextElementSibling;
        }
        if (!block.length || !block.every(pureList)) return;
        hide(heading); block.forEach(hide); lists += block.length;
      });
      if (changed.length) decluttered.set(root, changed);
      return lists;
    }
    function readerIsOverview(root) {
      if (disposed || !index || !current || !root || !bound.has(root) || !host.contains(root) ||
          !root.matches(".bp-reading-body") || root.isConnected === false) return false;
      var sources = root.querySelectorAll(".bp-reading-source"), reading = sources[0];
      var owner = reading && indexed.get(reading.getAttribute("data-autoform-node-id"));
      if (sources.length !== 1 || reading.parentElement !== root || reading.hasAttribute("hidden") ||
          reading.hasAttribute("aria-hidden") || reading.hasAttribute("inert") || !owner || owner.kind !== "scope" ||
          owner.scope !== owner.id || owner.id !== current.scope || owner.result === true ||
          ["collection", "transparent", "source"].indexOf(owner.navigation) >= 0 ||
          current.nodeId && indexedNode(current.nodeId) !== owner) return false;
      // Only this instance's proven duplicate indexes may be ignored. An
      // authored hidden attribute is never evidence of navigation-only content.
      var ignored = new Set((decluttered.get(root) || []).filter(function (entry) {
        return !entry.hidden && entry.node.hidden && entry.node.parentElement === reading;
      }).map(function (entry) { return entry.node; }));
      function attributes(node, allowed) {
        return Array.from(node.attributes).every(function (attribute) { return allowed.indexOf(attribute.name) >= 0; });
      }
      function plain(node) { return Array.from(node.childNodes).every(function (child) { return child.nodeType === 3 || child.nodeType === 8; }); }
      function elements(node) {
        var children = Array.from(node.childNodes);
        return children.some(function (child) { return child.nodeType !== 1 && child.nodeType !== 8 &&
          (child.nodeType !== 3 || child.textContent.trim()); }) ? null : children.filter(function (child) { return child.nodeType === 1; });
      }
      function chapters(node) {
        if (node.tagName !== "NAV" || node.getAttribute("class") !== "bp-book-nav" ||
            node.getAttribute("aria-label") !== "Blueprint chapters" || !attributes(node, ["class", "aria-label"])) return false;
        var links = elements(node), directions = [];
        return links && links.length > 0 && links.length <= 2 && links.every(function (anchor) {
          var direction = anchor.getAttribute("class") === "bp-book-nav-link bp-book-nav-previous" ? "previous" :
            anchor.getAttribute("class") === "bp-book-nav-link bp-book-nav-next" ? "next" : null;
          var href = link(anchor.getAttribute("href"), current.documentURL), target = href && routes.get(href), parts = elements(anchor);
          if (anchor.tagName !== "A" || !direction || !target || !attributes(anchor, ["class", "href"]) ||
              directions.indexOf(direction) >= 0 || direction === "previous" && directions.length || !parts || parts.length !== 2) return false;
          directions.push(direction);
          var label = parts[0], title = parts[1], contents = Array.from(label.childNodes), arrow = contents[0];
          return label.tagName === "SPAN" && label.getAttribute("class") === "bp-book-nav-direction" && attributes(label, ["class"]) &&
            contents.length === 2 && arrow.nodeType === 1 && arrow.tagName === "SPAN" && arrow.getAttribute("aria-hidden") === "true" &&
            attributes(arrow, ["aria-hidden"]) && plain(arrow) && arrow.textContent === (direction === "previous" ? "←" : "→") &&
            contents[1].nodeType === 3 && contents[1].textContent === (direction === "previous" ? " Previous" : " Next") &&
            title.tagName === "SPAN" && title.getAttribute("class") === "bp-book-nav-title" && attributes(title, ["class"]) &&
            plain(title) && title.textContent === target.title;
        });
      }
      var headings = 0, navigation = 0;
      return Array.from(reading.childNodes).every(function (node) {
        if (node.nodeType === 8 || node.nodeType === 3 && !node.textContent.trim()) return true;
        if (node.nodeType !== 1) return false;
        if (ignored.has(node)) return !/^H[1-6]$/.test(node.tagName) || !/[$\\]/.test(node.textContent);
        if (node.tagName === "H1" && attributes(node, ["id"]) && plain(node) && !/[$\\]/.test(node.textContent) &&
            node.textContent.trim() === owner.title) return ++headings === 1;
        return chapters(node) && ++navigation === 1;
      }) && headings === 1;
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
            !legacyHash(new URL(href).hash.slice(1))) return;
        event.preventDefault(); event.stopPropagation(); navigate(href, {focus: true});
      }
      root.addEventListener("click", click, true); bound.set(root, click);
    }
    function popstate() {
      if (disposed || !internal(win.location.href)) return;
      // The browser has already changed location, but the mounted DOM still belongs to the outgoing
      // entry. Capture it by its stored key; never rewrite the destination URL to save that state.
      try { outgoing(); } catch (error) { report(error, current && current.url); }
      var entry = entryState(), key = entryKey(entry, win.location.href);
      var cached = cacheGet(snapshots, key), snapshot = cached ? copyState(cached.snapshot) :
        historySnapshotsTrusted && entry && entry.url === win.location.href ? entry.snapshot : null;
      try { win.history.replaceState(historyState(snapshot, win.location.href, key), "", win.location.href); }
      catch (error) { historySnapshotsTrusted = false; report(error, win.location.href); return; }
      var url = new URL(win.location.href);
      if (current && !currentFailed && resource(current.url) === resource(url.href) && url.hash && !routes.has(url.href) &&
          !legacyHash(url.hash.slice(1))) {
        // Native section anchors scroll the existing reading DOM. They must
        // not discard the selected theorem or replace its full reading pane.
        var request = fresh(), anchor;
        try { anchor = decodeURIComponent(url.hash.slice(1)); } catch (_error) { anchor = url.hash.slice(1); }
        currentKey = key; current.url = url.href; current.anchor = anchor; current.isCurrent = function () { return alive(request); };
        host.removeAttribute("aria-busy");
        if (snapshot && typeof options.restoreState === "function") {
          var bundle = current;
          try {
            Promise.resolve(options.restoreState(snapshot, bundle)).catch(function (error) {
              if (alive(request) && current === bundle) report(rendererFailure(error, bundle, request, key), url.href);
            });
          }
          catch (error) { if (alive(request) && current === bundle) report(rendererFailure(error, bundle, request, key), url.href); }
        }
        return;
      }
      navigate(win.location.href, {history: "none", entryKey: key, snapshot: snapshot, reason: "popstate"});
    }
    function start() {
      if (started) return started;
      if (disposed) return Promise.resolve(null);
      win.addEventListener("popstate", popstate);
      if (source) bindReader(source);
      var entry = entryState();
      started = run(initialURL, {history: "replace", entryKey: entryKey(entry, initialURL),
        snapshot: historySnapshotsTrusted && entry && entry.url === initialURL ? entry.snapshot : null, reason: "start"}, true);
      return started;
    }
    function dispose() {
      if (disposed) return;
      disposed = true; epoch++;
      wikiGeneration++; if (wikiController) wikiController.abort();
      host.removeAttribute("aria-busy");
      if (active) active.abort(); if (indexController) indexController.abort();
      if (scrollOwned && win.history.scrollRestoration === "manual") win.history.scrollRestoration = "auto";
      win.removeEventListener("popstate", popstate);
      bound.forEach(function (listener, root) {
        (decluttered.get(root) || []).forEach(function (entry) { entry.node.hidden = entry.hidden; });
        root.removeEventListener("click", listener, true);
      }); bound.clear();
      htmlCache.clear(); jsonCache.clear(); cacheBytes = 0; index = null; indexed.clear(); routes.clear(); documents.clear();
      snapshots.clear(); currentKey = null; current = null; initialSource = null; source = null; indexPromise = null; started = null;
      indexDigest = null; wikiLinks = null; wikiPromise = null; wikiController = null;
      decluttered = new WeakMap();
    }
    return {start: start, navigate: navigate, navigateNode: navigateNode, loadIndex: loadIndex, loadWikiLinks: loadWikiLinks,
      getHomeURL: function () { return homeURL; },
      getWikiLinks: function () { return wikiLinks; },
      getIndex: function () { return index; }, bindReader: bindReader, declutterReader: declutterReader, readerIsOverview: readerIsOverview,
      checkpoint: checkpoint, dispose: dispose};
  }

  window.AutoformNavigation = {create: create};
})();
