"""The site's MathJax: one pinned version, configured by the renderer.

``autoform render`` writes ``javascripts/mathjax.js`` on every build, over the
vault's copy; the script loads the one MathJax version it was written for and
leaves formulas as typed under any other. Project macros come from ``tex-macros.json`` and reach
article formulas only, and the commands that would let one formula change
another, or color a symbol, are turned off.
"""

from __future__ import annotations

import hashlib
import json
import re

from .markdown import FORMULA_CLASS

#: The MathJax release the site loads. :data:`NEUTRALISED` and the read-back
#: testimony allowlist were checked against it, so changing it means checking
#: both again.
MATHJAX_VERSION = "3.2.2"
#: Where that release is loaded from, and the component bundle the site uses.
MATHJAX_BASE = f"https://cdn.jsdelivr.net/npm/mathjax@{MATHJAX_VERSION}/es5"
MATHJAX_BUNDLE = f"{MATHJAX_BASE}/tex-mml-chtml.js"
#: The script ``autoform render`` writes into the site source.
MATHJAX_SCRIPT = "javascripts/mathjax.js"
#: Project macros, in the form of MathJax's ``tex.macros``, kept in the vault.
TEX_MACROS = "tex-macros.json"

#: What article formulas may use: the standard notation, unknown commands in
#: red, the packages authors reach for most, and the project's macros. Of the
#: packages the bundle would load on demand, braket, mhchem and amscd (CD
#: diagrams) are loaded too. The others stay off: cancel, bbox and enclose
#: take a color, as color does; html sets classes, styles and links; unicode
#: picks fonts; action adds tooltips and toggles; newcommand and extpfeil
#: define commands for the formulas after them; verb and bussproofs are not
#: loaded either.
PAGE_PACKAGES = ("base", "ams", "noundefined", "boldsymbol", "mathtools", "braket", "mhchem", "amscd", "configmacros")
#: The extensions the bundle does not carry, loaded before anything is typeset,
#: and the filter that keeps classes, IDs, styles, and links out of the output.
_LOADED = ("ui/safe", "[tex]/boldsymbol", "[tex]/mathtools", "[tex]/braket", "[tex]/mhchem", "[tex]/amscd")
_SAFE = {"allow": {"URLs": "none", "classes": "none", "cssIDs": "none", "styles": "none"}}

#: The commands of the page's packages that change formulas other than their
#: own (a definition, a tag form, a package option, a label), or give a
#: symbol the color a formula writes, like the site's status marks. The
#: articles on a page share one TeX input, so the input maps each to an
#: undefined command, which shows in red and does nothing, however a formula
#: or a macro reaches it. Other definitions, ``\newcommand``, ``\def``,
#: ``\require`` among them, are in no package the page loads.
NEUTRALISED = (
    "label", "mmlToken", "DeclareMathOperator", "DeclarePairedDelimiter", "DeclarePairedDelimiterX",
    "DeclarePairedDelimiterXPP", "DeclarePairedDelimiters", "DeclarePairedDelimitersX",
    "DeclarePairedDelimitersXPP", "newtagform", "renewtagform", "usetagform", "mathtoolsset",
)

#: The ``javascripts/mathjax.js`` ``autoform init`` put in vaults before the
#: renderer wrote its own, by SHA-256. An unedited copy is replaced on the site
#: without a word; an edited one is replaced with a warning, since its edits
#: are lost.
_SHIPPED_CONFIGURATIONS = frozenset({"e2fa0fa73dd367cad2f508055c6b6b45c771547aca6c3baae639ecd872112384"})

_MACRO_NAME = re.compile(r"[A-Za-z]+")


def mathjax_script(kept: bytes | None, macros: bytes | None) -> tuple[str, list[str]]:
    """The ``javascripts/mathjax.js`` the site gets for a vault, and the
    warnings about it: macros in ``tex-macros.json`` it leaves out, or an
    edited copy of the configuration the vault used to keep, whose edits the
    site loses.

    ``kept`` and ``macros`` are the bytes render captured at those two paths,
    ``None`` where it captured none, so the script is built from the
    ``tex-macros.json`` the site publishes beside it.
    """

    issues: list[str] = []
    if kept is not None:
        digest = hashlib.sha256(kept.replace(b"\r\n", b"\n")).hexdigest()
        if digest not in _SHIPPED_CONFIGURATIONS:
            issues.append(
                f"{MATHJAX_SCRIPT}: autoform render writes the site's MathJax configuration over this "
                f"copy, so its edits are lost; move any tex.macros to {TEX_MACROS} and delete it"
            )
    parsed, macro_issues = _tex_macros(macros)
    return _script(parsed), issues + macro_issues


def _tex_macros(data: bytes | None) -> tuple[dict[str, object], list[str]]:
    """The macros in ``data``, a ``tex-macros.json``'s bytes or ``None`` when
    there is none, and what is wrong with the ones it leaves out.

    Each maps a name of letters to a body, to ``[body, arguments]``, or to
    ``[body, arguments, default]``, as MathJax's ``tex.macros`` does, and is
    not one of :data:`NEUTRALISED`. A file that is no such object gives no
    macros.
    """

    if data is None:
        return {}, []
    try:
        macros = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_names)
    except (RecursionError, UnicodeDecodeError, ValueError) as exc:
        return {}, [f"{TEX_MACROS}: not valid JSON: {exc}"]
    if not isinstance(macros, dict):
        return {}, [f"{TEX_MACROS}: must be a JSON object from macro names to definitions"]
    issues: list[str] = []
    kept: dict[str, object] = {}
    for name, definition in macros.items():
        if not _MACRO_NAME.fullmatch(name):
            issues.append(f"{TEX_MACROS}: {name!r} is not a macro name; use letters only")
            continue
        if name in NEUTRALISED or name == "notallowed":
            issues.append(f"{TEX_MACROS}: \\{name} is a command the site turns off; choose another name")
            continue
        if (
            isinstance(definition, list)
            and len(definition) in {2, 3}
            and isinstance(definition[0], str)
            and type(definition[1]) is int
            and all(isinstance(default, str) for default in definition[2:])
        ):
            if not 0 <= definition[1] <= 9:
                issues.append(f"{TEX_MACROS}: \\{name} takes {definition[1]} arguments; use 0 to 9")
                continue
        elif not isinstance(definition, str):
            issues.append(
                f"{TEX_MACROS}: \\{name} must be a body, [body, arguments], or [body, arguments, default]"
            )
            continue
        kept[name] = definition
    return kept, issues


def _unique_names(pairs: list[tuple[str, object]]) -> dict[str, object]:
    found: dict[str, object] = {}
    for name, value in pairs:
        if name in found:
            raise ValueError(f"{name!r} is defined twice")
        found[name] = value
    return found


def _script(macros: dict[str, object]) -> str:
    """The script itself, with everything it is configured by in one object."""

    settings = {
        "version": MATHJAX_VERSION,
        "base": MATHJAX_BASE,
        "bundle": MATHJAX_BUNDLE,
        "load": list(_LOADED),
        "safe": _SAFE,
        "page": {
            "packages": list(PAGE_PACKAGES),
            "macros": {**dict(sorted(macros.items())), **{name: "\\notallowed" for name in NEUTRALISED}},
            "inlineMath": [["$", "$"], ["\\(", "\\)"]],
            "displayMath": [["$$", "$$"], ["\\[", "\\]"]],
        },
        # The input MathJax makes for itself when it starts, which no pass
        # uses, finds nothing, so the document it starts with never holds a
        # formula, whatever its menu does.
        "startup": {
            "packages": ["base"],
            "inlineMath": [],
            "displayMath": [],
            "processEscapes": False,
            "processEnvironments": False,
            "processRefs": False,
        },
        # The formulas the site's Markdown marks, which are all the input
        # reads, and a class pattern nothing matches, so no class in a page
        # makes it read anything else.
        "formulas": FORMULA_CLASS,
        "nothing": "(?!)",
    }
    return _SCRIPT.replace("SETTINGS_JSON", json.dumps(settings, indent=2, ensure_ascii=True))


_SCRIPT = """/* Generated by autoform render. Edits are overwritten. */
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
    console.error(message, error || "");
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
  document.addEventListener("autoform:math-refresh", function (event) {
    refresh(event.detail && event.detail.root || event.target);
  });

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

  // Source metadata survives cloneNode(), unlike the MathItem/menu handlers
  // on rendered glyphs. Never infer TeX from their textContent. The router may
  // clone an SSR reader after its first pass but before its first graph loads.
  function prepare(root) {
    var formulas = Array.from(root.querySelectorAll("." + SETTINGS.formulas));
    if (root.matches && root.matches("." + SETTINGS.formulas)) formulas.unshift(root);
    var jobs = [], intact = true;
    formulas.forEach(function (element) {
      if (!element.isConnected || element.closest("[" + STAGE + "]") || owned.has(element)) return;
      var source;
      if (element.querySelector("mjx-container")) {
        source = element.getAttribute(SOURCE);
        if (!stamp || element.getAttribute(STAMP) !== stamp || source === null) {
          intact = false;
          console.warn("autoform: rendered formula has no trusted retained TeX source; left unchanged");
          return;
        }
      } else {
        source = element.textContent;
        element.setAttribute(SOURCE, source);
        if (stamp) element.setAttribute(STAMP, stamp);
        else element.removeAttribute(STAMP);
      }
      var stage = document.createElement("div"), fresh = element.cloneNode(false);
      stage.setAttribute(STAGE, "");
      stage.setAttribute("aria-hidden", "true");
      stage.style.cssText = "position:fixed;top:0;left:0;visibility:hidden;pointer-events:none;z-index:-1";
      var style = window.getComputedStyle(element);
      ["fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight", "letterSpacing", "direction"].forEach(function (name) {
        stage.style[name] = style[name];
      });
      stage.style.width = element.parentElement.getBoundingClientRect().width + "px";
      fresh.textContent = source;
      fresh.setAttribute(SOURCE, source);
      stage.appendChild(fresh);
      document.body.appendChild(stage);
      jobs.push({element: element, fresh: fresh, stage: stage, source: source});
    });
    return {jobs: jobs, intact: intact};
  }

  // A fresh, safe TeX input for each serialized pass. Connected hidden
  // staging nodes inherit the original formula's metrics; visible glyphs are
  // replaced only after rendering succeeds. Move, never clone, the output so
  // its MathItems and menu's Original Form still refer to the actual nodes.
  function typeset(root) {
    prune();
    var prepared = prepare(root), jobs = prepared.jobs, doc = null;
    if (!jobs.length) return Promise.resolve(prepared.intact);
    return render(jobs.map(function (job) { return job.fresh; }), function (created) { doc = created; }).then(function () {
      if (MathJax.version !== SETTINGS.version) throw new Error("MathJax version changed during a pass");
      var elements = [], discarded = [];
      jobs.forEach(function (job) {
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
      prune();
      return prepared.intact;
    }).catch(function (error) {
      jobs.forEach(function (job) { job.stage.remove(); });
      if (doc) dispose(doc);
      throw error;
    });
  }

  // A document waits, and then starts again, while the menu loads what its
  // saved settings ask for. None is made while a menu is loading, since a
  // menu that asks for what is already loading is never told it has loaded.
  function render(elements, created) {
    return settled().then(function () {
      var mathjax = MathJax._.mathjax.mathjax;
      var options = {
        processHtmlClass: SETTINGS.nothing,
        InputJax: new MathJax._.input.tex_ts.TeX(copy(SETTINGS.page)),
        OutputJax: MathJax.startup.output,
        safeOptions: copy(SETTINGS.safe)
      };
      var doc = mathjax.document(MathJax.startup.document.document, options);
      created(doc);
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

  window.MathJax = {
    loader: {load: SETTINGS.load, paths: {mathjax: SETTINGS.base}, failed: function (error) {
      stop("autoform: MathJax could not load; formulas are left as typed", error);
    }},
    tex: copy(SETTINGS.startup),
    options: {processHtmlClass: SETTINGS.nothing, safeOptions: copy(SETTINGS.safe)},
    startup: {typeset: false, pageReady: ready}
  };

  // A project scaffolded before this file was generated also lists the
  // bundle in mkdocs.yml, after this file. That tag runs before the page is
  // parsed, so by then MathJax is loaded and is not fetched twice; ready()
  // checks its version either way.
  function load() {
    if (window.MathJax.version !== undefined) return;
    var script = document.createElement("script");
    script.src = SETTINGS.bundle;
    script.async = true;
    script.addEventListener("error", function () { stop("autoform: MathJax could not load; formulas are left as typed"); });
    document.head.appendChild(script);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", load);
  else load();
})();
"""


__all__ = [
    "MATHJAX_SCRIPT",
    "MATHJAX_VERSION",
    "NEUTRALISED",
    "TEX_MACROS",
    "mathjax_script",
]
