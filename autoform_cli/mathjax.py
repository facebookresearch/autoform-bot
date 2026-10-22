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

  // A MathJax listed in mkdocs.yml before this file started without this
  // configuration and would typeset the page with its own. It is stopped
  // instead, and the formulas stay as typed.
  if (window.MathJax && window.MathJax.version !== undefined) {
    window.MathJax.config.startup.typeset = false;
    window.MathJax.config.startup.pageReady = function () {};
    console.error("autoform: MathJax was loaded before javascripts/mathjax.js; formulas are left as typed. " +
      "Load MathJax only through javascripts/mathjax.js in mkdocs.yml.");
    return;
  }

  // One pass over the page as it is now, with a new TeX input, so no
  // definition or label outlives the pass. The input reads only the formulas
  // the site's Markdown marked, not the navigation or a title the site prints
  // as typed. Each document is finished before the next is made.
  function typeset() {
    var startup = MathJax.startup;
    var root = startup.document.document;
    var adaptor = startup.adaptor;
    var formulas = adaptor.getElements(["." + SETTINGS.formulas], root);
    var steps = [[SETTINGS.page, {processHtmlClass: SETTINGS.nothing}, formulas]];
    // What the renderer kept of the last page, which every renderer can
    // forget, though only CHTML has clearCache.
    startup.output.reset();
    return steps.reduce(function (done, step) {
      return done.then(function () {
        return render(root, step[0], step[1], step[2]);
      });
    }, Promise.resolve());
  }

  // A document waits, and then starts again, while the menu loads what its
  // saved settings ask for. None is made while a menu is loading, since a
  // menu that asks for what is already loading is never told it has loaded.
  function render(root, tex, options, elements) {
    return settled().then(function () {
      var mathjax = MathJax._.mathjax.mathjax;
      options.InputJax = new MathJax._.input.tex_ts.TeX(copy(tex));
      options.OutputJax = MathJax.startup.output;
      options.safeOptions = copy(SETTINGS.safe);
      var doc = mathjax.document(root, options);
      if (elements) doc.options.elements = elements;
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

  // Passes run one at a time, in the order the pages were shown.
  var queue = Promise.resolve();
  function pass() {
    queue = queue.then(typeset).catch(function (error) {
      console.error("autoform: typesetting failed", error);
    });
    return queue;
  }

  function ready() {
    if (MathJax.version !== SETTINGS.version) {
      console.error("autoform: this page loaded MathJax " + MathJax.version + ", but javascripts/mathjax.js " +
        "was written for " + SETTINGS.version + "; formulas are left as typed. Load MathJax only through " +
        "javascripts/mathjax.js in mkdocs.yml.");
      return;
    }
    // Material's instant navigation swaps the page in place, without running
    // this file again, and announces each page on document$, this one too.
    var pages = window.document$;
    if (pages && typeof pages.subscribe === "function") pages.subscribe(pass);
    else pass();
  }

  window.MathJax = {
    loader: {load: SETTINGS.load, paths: {mathjax: SETTINGS.base}},
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
