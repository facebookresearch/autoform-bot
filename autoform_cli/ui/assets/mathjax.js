(function () {
  "use strict";
  var SETTINGS = SETTINGS_JSON;

  // MathJax keeps the option objects it is given, so each input gets its own.
  function copy(value) {
    return JSON.parse(JSON.stringify(value));
  }

  var SOURCE = "data-autoform-math-source", STAMP = "data-autoform-math-stamp", STAGE = "data-autoform-math-stage";
  // Authored/fetched HTML cannot nominate a different source for its marked
  // text. Only this document's unpredictable stamp can identify a glyph clone
  // whose source this runtime captured before rendering. Without WebCrypto,
  // fresh formulas still work but glyph clones are left unchanged.
  var stamp = null;
  if (window.crypto && typeof window.crypto.getRandomValues === "function") {
    try {
      var random = new Uint32Array(4);
      window.crypto.getRandomValues(random);
      stamp = Array.from(random, function (part) { return ("00000000" + part.toString(16)).slice(-8); }).join("");
    } catch (_error) { /* No trusted glyph-clone recovery without an unpredictable stamp. */ }
  }
  var pending = null, running = false, engineReady = false, stopped = false;
  var completion = null, complete = null, successful = true;
  var records = [], owned = new WeakMap();

  function finish(value) {
    var resolve = complete;
    completion = complete = null;
    successful = true;
    if (resolve) resolve(value);
  }

  function stop(message, error) {
    stopped = true;
    pending = null;
    if (error) console.error(message, error);
    else console.error(message);
    if (!running) finish(false);
  }

  // A caller can arrive before the engine, or replace a reader many times
  // while it is loading. Keep one pending scope, not a promise per old page.
  // Disjoint scopes coalesce to the document, still reading only marked TeX.
  function refresh(root) {
    root = root || document;
    if (stopped || (root !== document && (root.nodeType !== 1 || root.ownerDocument !== document))) {
      return Promise.resolve(false);
    }
    if (!pending || root === document || root.contains(pending)) pending = root;
    else if (!pending.contains(root)) pending = document;
    if (!completion) completion = new Promise(function (resolve) { complete = resolve; });
    var result = completion;
    drain();
    return result;
  }

  window.AutoformMath = {refresh: refresh};

  // A MathJax listed in mkdocs.yml before this file started without this
  // configuration and would typeset the page with its own. It is stopped
  // instead, and the formulas stay as typed.
  if (window.MathJax && window.MathJax.version !== undefined) {
    if (window.MathJax.config && window.MathJax.config.startup) {
      window.MathJax.config.startup.typeset = false;
      window.MathJax.config.startup.pageReady = function () {};
    }
    stop("autoform: MathJax was loaded before javascripts/mathjax.js; formulas are left as typed. " +
      "Load MathJax only through javascripts/mathjax.js in mkdocs.yml.");
    return;
  }

  // Dropping MathItems and menu records never removes still-visible output.
  // A scoped pass may leave other formulas in place; retain their documents.
  function dispose(doc) {
    if (doc.menu) doc.menu.clear();
    doc.options.elements = [];
    doc.clear();
  }

  function prune() {
    records = records.filter(function (record) {
      var removed = record.elements.filter(function (element) { return !element.isConnected; });
      record.elements = record.elements.filter(function (element) { return element.isConnected; });
      if (!record.elements.length) { dispose(record.doc); return false; }
      if (removed.length) record.doc.clearMathItemsWithin(removed);
      record.doc.options.elements = record.elements;
      return true;
    });
  }

  // The pinned menu measures offsetWidth before display() mounts its portal.
  // Correct only this safe document's presentation after the real display;
  // never change global prototypes, menu actions, or the mathematical input.
  function placeMenus(doc) {
    var root = doc.menu && doc.menu.menu;
    if (!root || typeof root.display !== "function") return;
    var wrapped = new WeakSet();
    function fit(menu) {
      var html = menu.html;
      if (!html || !html.isConnected || !html.style || typeof html.getBoundingClientRect !== "function") return;
      var rect = html.getBoundingClientRect(), viewport = document.documentElement;
      var width = viewport.clientWidth, height = viewport.clientHeight, margin = 5;
      if (!width || !height || !rect.width || !rect.height) return;
      var x = rect.left, y = rect.top;
      var anchor = menu !== root && menu.baseMenu && menu.anchor && menu.anchor.html;
      if (anchor && typeof anchor.getBoundingClientRect === "function" && rect.right > width - margin) {
        // Keep the parent's normal hover target clear instead of clamping
        // a right-opening submenu back over the parent row.
        x = anchor.getBoundingClientRect().left - rect.width + 6;
      }
      x = Math.max(margin, Math.min(x, width - rect.width - margin));
      y = Math.max(margin, Math.min(y, height - rect.height - margin));
      // Delta coordinates also work when the frame is offset by page scroll.
      if (x !== rect.left) html.style.left = ((parseFloat(html.style.left) || 0) + x - rect.left) + "px";
      if (y !== rect.top) html.style.top = ((parseFloat(html.style.top) || 0) + y - rect.top) + "px";
    }
    function walk(menu, seen) {
      if (!menu || typeof menu !== "object" || seen.has(menu)) return;
      seen.add(menu);
      if (typeof menu.display === "function" && !wrapped.has(menu)) {
        var display = menu.display;
        menu.display = function () {
          var result = display.apply(this, arguments);
          try {
            // Posting can replace annotation/settings submenus dynamically.
            if (this === root) walk(root, new WeakSet());
            fit(this); // isPosted() is still false until upstream post returns.
          } catch (_error) { /* Unknown menu shapes retain upstream behavior. */ }
          return result;
        };
        wrapped.add(menu);
      }
      if (Array.isArray(menu.items)) menu.items.forEach(function (item) { if (item) walk(item.submenu, seen); });
    }
    try { walk(root, new WeakSet()); } catch (_error) { /* Keep unsupported menus unchanged. */ }
  }

  // Source metadata survives cloneNode(), unlike the MathItem/menu handlers
  // on rendered glyphs. Never infer TeX from their textContent. A typeset
  // reader moved elsewhere keeps its owned output; a clone is typeset again.
  function prepare(root) {
    function marked(className) {
      var nodes = Array.from(root.querySelectorAll("." + className));
      if (root.matches && root.matches("." + className)) nodes.unshift(root);
      return nodes;
    }
    var groups = [{card: null, settings: SETTINGS.page, jobs: []}], cards = new Map(), controls = new Map();
    function groupFor(element, group) {
      if (!element.closest("a, button, summary, nav")) return group;
      var controlled = controls.get(group);
      if (!controlled) {
        controlled = {card: group.card, settings: group.settings, control: true, jobs: []};
        controls.set(group, controlled); groups.push(controlled);
      }
      return controlled;
    }
    var formulas = marked(SETTINGS.formulas).filter(function (element) {
      return !SETTINGS.cards || !element.closest("." + SETTINGS.cards);
    }).map(function (element) { return {element: element, group: groups[0]}; });
    if (SETTINGS.cards && SETTINGS.cardFormulas) marked(SETTINGS.cardFormulas).forEach(function (element) {
      var card = element.closest("." + SETTINGS.cards);
      if (!card) return; // Only renderer-marked formulas inside a validated card.
      var group = cards.get(card);
      if (!group) { group = {card: card, settings: SETTINGS.card, jobs: []}; cards.set(card, group); groups.push(group); }
      formulas.push({element: element, group: group});
    });
    var jobs = [], reads = [], intact = true;
    var FONT = ["fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight", "letterSpacing", "direction"];
    // Read every style and width before the first write: a read after a
    // stage is appended forces a style and layout pass of the whole page.
    formulas.forEach(function (marked) {
      var element = marked.element;
      var owner = owned.get(element), controlled = element.closest("a, button, summary, nav");
      if (!element.isConnected || element.closest("[" + STAGE + "]") ||
          owner && (!controlled || owner.options.enableMenu === false)) return;
      var source, rendered = Boolean(element.querySelector("mjx-container"));
      if (rendered) {
        source = element.getAttribute(SOURCE);
        if (!stamp || element.getAttribute(STAMP) !== stamp || source === null) {
          intact = false;
          console.warn("autoform: rendered formula has no trusted retained TeX source; left unchanged");
          return;
        }
      } else source = element.textContent;
      var style = window.getComputedStyle(element);
      reads.push({marked: marked, source: source, rendered: rendered,
        font: FONT.map(function (name) { return style[name]; }), width: element.parentElement.getBoundingClientRect().width});
    });
    reads.forEach(function (read) {
      var element = read.marked.element, source = read.source;
      if (!read.rendered) {
        element.setAttribute(SOURCE, source);
        if (stamp) element.setAttribute(STAMP, stamp);
        else element.removeAttribute(STAMP);
      }
      var stage = document.createElement("div"), fresh = element.cloneNode(false);
      stage.setAttribute(STAGE, "");
      stage.setAttribute("aria-hidden", "true");
      stage.style.cssText = "position:fixed;top:0;left:0;visibility:hidden;pointer-events:none;z-index:-1";
      FONT.forEach(function (name, index) { stage.style[name] = read.font[index]; });
      stage.style.width = read.width + "px";
      fresh.textContent = source;
      fresh.setAttribute(SOURCE, source);
      stage.appendChild(fresh);
      document.body.appendChild(stage);
      var job = {element: element, fresh: fresh, stage: stage, source: source};
      jobs.push(job); groupFor(element, read.marked.group).jobs.push(job);
    });
    return {jobs: jobs, groups: groups.filter(function (group) { return !group.card || group.jobs.length; }), intact: intact};
  }

  // Each scope uses a fresh article input and a separate fresh input for each
  // validated read-back card. Hidden staging is connected; commit only after
  // every group renders, moving the real output and MathItems, never clones.
  function typeset(root) {
    prune();
    var prepared = prepare(root), jobs = prepared.jobs, documents = [];
    if (!jobs.length) return Promise.resolve(prepared.intact);
    return prepared.groups.reduce(function (done, group) {
      return done.then(function () {
        return render(group.jobs.map(function (job) { return job.fresh; }), group.settings, function (doc) {
          group.doc = doc; documents.push(doc);
        }, group.control);
      });
    }, Promise.resolve()).then(function () {
      if (MathJax.version !== SETTINGS.version) throw new Error("MathJax version changed during a pass");
      prepared.groups.forEach(function (group) {
        var doc = group.doc, elements = [], discarded = [];
        group.jobs.forEach(function (job) {
          if (job.element.isConnected && job.element.getAttribute(SOURCE) === job.source) {
            job.element.replaceWith(job.fresh);
            owned.set(job.fresh, doc);
            elements.push(job.fresh);
          } else discarded.push(job.fresh);
          job.stage.remove();
        });
        if (elements.length) {
          if (discarded.length) doc.clearMathItemsWithin(discarded);
          doc.options.elements = elements;
          records.push({doc: doc, elements: elements});
        } else dispose(doc);
      });
      prune();
      return prepared.intact;
    }).catch(function (error) {
      jobs.forEach(function (job) { job.stage.remove(); });
      documents.forEach(dispose);
      throw error;
    });
  }

  // A document waits, and then starts again, while the menu loads what its
  // saved settings ask for. None is made while a menu is loading, since a
  // menu that asks for what is already loading is never told it has loaded.
  function render(elements, settings, created, control) {
    return settled().then(function () {
      var mathjax = MathJax._.mathjax.mathjax;
      var options = {
        processHtmlClass: SETTINGS.nothing,
        InputJax: new MathJax._.input.tex_ts.TeX(copy(settings)),
        OutputJax: MathJax.startup.output,
        safeOptions: copy(SETTINGS.safe)
      };
      // Classify the original ancestry before staging detaches it. A formula
      // inside a control or navigation must not add a tab stop or take its Enter
      // key, even when saved menu settings enable the accessibility explorer.
      if (control) { options.enableMenu = false; options.renderActions = {explorable: []}; }
      var doc = mathjax.document(MathJax.startup.document.document, options);
      created(doc);
      placeMenus(doc);
      doc.options.elements = elements;
      // Do not reset the shared renderer's CSS cache: a scoped refresh may
      // coexist with earlier output, which still needs its generated styles.
      return mathjax.handleRetriesFor(function () {
        doc.render();
      });
    });
  }

  // What the menus are loading, waited for without the promise a menu
  // gives, since a menu that is loading an accessibility component skips
  // redrawing the formulas it moves once anyone has asked for that promise.
  function settled() {
    var menu = MathJax.startup.document.menu;
    var loads = menu && menu.constructor.loadingPromises;
    return Promise.all(loads ? Array.from(loads.values()) : []);
  }

  function drain() {
    if (running || !engineReady || !pending || stopped) return;
    running = true;
    function next() {
      if (!pending || stopped) { running = false; finish(successful && !stopped); return; }
      var root = pending;
      pending = null;
      Promise.resolve().then(function () {
        if (MathJax.version !== SETTINGS.version) throw new Error("MathJax version changed before a pass");
        return typeset(root);
      }).then(function (value) {
        successful = successful && value;
      }).catch(function (error) {
        successful = false;
        console.error("autoform: typesetting failed; formulas are left unchanged", error);
      }).then(next);
    }
    next();
  }

  function ready() {
    if (MathJax.version !== SETTINGS.version) {
      stop("autoform: this page loaded MathJax " + MathJax.version + ", but javascripts/mathjax.js " +
        "was written for " + SETTINGS.version + "; formulas are left as typed. Load MathJax only through " +
        "javascripts/mathjax.js in mkdocs.yml.");
      return;
    }
    // Material's instant navigation swaps the page in place, without running
    // this file again, and announces each page on document$, this one too.
    var pages = window.document$;
    engineReady = true;
    if (pages && typeof pages.subscribe === "function") pages.subscribe(function () { refresh(document); });
    return refresh(document);
  }

  // A script listed after this one could assign its own window.MathJax before
  // MathJax starts, such as the snippet Material's documentation gives, and
  // the page would be typeset without these settings. So window.MathJax is
  // this configuration until MathJax starts and replaces it with itself.
  var configuration = {
    loader: {load: SETTINGS.load, paths: {mathjax: SETTINGS.base}, failed: function (error) {
      stop("autoform: MathJax could not load; formulas are left as typed", error);
    }},
    tex: copy(SETTINGS.startup),
    options: {processHtmlClass: SETTINGS.nothing, safeOptions: copy(SETTINGS.safe)},
    startup: {typeset: false, pageReady: ready}
  };
  var current = configuration;

  try {
    Object.defineProperty(window, "MathJax", {
      configurable: false,
      enumerable: true,
      get: function () {
        return current;
      },
      set: function (value) {
        if (current === configuration && value && value.version !== undefined && value.config === configuration) {
          current = value;
          return;
        }
        console.error("autoform: a script assigned window.MathJax after javascripts/mathjax.js; it is ignored, " +
          "and the site's configuration is kept. Configure MathJax only through javascripts/mathjax.js and " +
          "tex-macros.json.");
      }
    });
  } catch (error) {
    stop("autoform: window.MathJax cannot be set; formulas are left as typed. Load MathJax only " +
      "through javascripts/mathjax.js in mkdocs.yml.", error);
    return;
  }

  // An older project's mkdocs.yml also lists the bundle, after this file.
  // That tag runs before the page is parsed, so by then MathJax is loaded and
  // is not fetched twice; ready() checks its version either way.
  function load() {
    if (window.MathJax.version !== undefined) return;
    var script = document.createElement("script");
    script.src = SETTINGS.bundle;
    // Only the bundle's pinned bytes run; the integrity check needs an anonymous CORS request.
    script.integrity = SETTINGS.integrity;
    script.crossOrigin = "anonymous";
    script.async = true;
    script.addEventListener("error", function () { stop("autoform: MathJax could not load; formulas are left as typed"); });
    document.head.appendChild(script);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", load);
  else load();
})();
