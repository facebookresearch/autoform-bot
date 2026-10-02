"""The site's MathJax: one pinned version, configured by the renderer.

Two kinds of mathematics share a page. Articles are Markdown and TeX their
authors wrote; read-back cards are testimony a validator accepted, shown beside
the Lean a reviewer compares it with. MathJax keeps TeX state, the macros an
input has defined, the operators it has declared, and the labels it has set,
for every formula that input reads afterwards. So the cards are never typeset
with the page: each pass typesets the articles with a TeX input made for that
pass, which skips the cards, and then each card with a TeX input made for it
alone, which knows only base, ams, and noundefined, the packages the testimony
validator checks against.

The configuration used to be a file in the vault, which a project kept from
the day it was scaffolded, and nothing checked which MathJax the site loaded.
``autoform render`` now writes ``javascripts/mathjax.js`` on every build, over
the vault's copy, and that script loads the one MathJax version it was written
for and refuses any other. A project scaffolded earlier still lists the bundle
in ``mkdocs.yml`` after the script; the script finds it already loaded and
checks its version instead. Macros the old file could carry are read from
``tex-macros.json`` in the vault, and reach article formulas only.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path

#: The MathJax release the site loads. The testimony validator's commands were
#: checked against it, so changing it means checking them again.
MATHJAX_VERSION = "3.2.2"
#: Where that release is loaded from, and the component bundle the site uses.
MATHJAX_BASE = f"https://cdn.jsdelivr.net/npm/mathjax@{MATHJAX_VERSION}/es5"
MATHJAX_BUNDLE = f"{MATHJAX_BASE}/tex-mml-chtml.js"
#: The script ``autoform render`` writes into the site source.
MATHJAX_SCRIPT = "javascripts/mathjax.js"
#: Project macros, in the form of MathJax's ``tex.macros``, kept in the vault.
TEX_MACROS = "tex-macros.json"

#: What article formulas may use: the standard notation, unknown commands in
#: red, the packages authors reach for most, and the project's macros.
PAGE_PACKAGES = ("base", "ams", "noundefined", "boldsymbol", "cancel", "mathtools", "configmacros")
#: What a card's formulas may use: exactly the packages the testimony validator
#: checks against. The project's macros are not among them.
CARD_PACKAGES = ("base", "ams", "noundefined")
#: The extensions the bundle does not carry, loaded before anything is typeset,
#: and the filter that keeps classes, IDs, styles, and links out of the output.
_LOADED = ("ui/safe", "[tex]/boldsymbol", "[tex]/cancel", "[tex]/mathtools")
_SAFE = {"allow": {"URLs": "none", "classes": "none", "cssIDs": "none", "styles": "none"}}

#: The commands that change formulas other than their own: they define or
#: redefine a command, an environment, an operator, a paired delimiter, or a
#: tag form, set a package option, load a package, or set a label, which a
#: second use anywhere later on the page turns into an error. These are the
#: ones the macro maps of MathJax 3.2.2 define, with TeX's own definition
#: primitives, which it does not.
STATEFUL_TEX = frozenset(
    r"""
    \newcommand \renewcommand \newenvironment \renewenvironment \def \let \gdef \edef \xdef \global
    \DeclareMathOperator \DeclarePairedDelimiter \DeclarePairedDelimiterX \DeclarePairedDelimiterXPP
    \DeclarePairedDelimiters \DeclarePairedDelimitersX \DeclarePairedDelimitersXPP \newtagform
    \renewtagform \usetagform \mathtoolsset \require \label
    """.split()
)

#: The commands that give a symbol the attributes a formula writes: base's
#: ``\mmlToken``. The site's filter drops the classes, styles, ids, and links
#: it could set, but not its color or background, with which an article could
#: color a symbol like the site's status marks.
ATTRIBUTE_TEX = frozenset({r"\mmlToken"})

#: The ``javascripts/mathjax.js`` files ``autoform init`` put in vaults before
#: the renderer wrote its own, by SHA-256. An unedited one is replaced on the
#: site without a word; an edited one is refused, since its edits would be lost.
_SHIPPED_CONFIGURATIONS = frozenset(
    {
        # The delimiters only, as first scaffolded.
        "e2fa0fa73dd367cad2f508055c6b6b45c771547aca6c3baae639ecd872112384",
        # ui/safe, and every default package but require.
        "3888a6568e3b187144745c60ff0dfd6701297dba9eb0e53955aa6778b6c6282e",
        # base, ams, and noundefined.
        "c04368a7ebd7bdafe482a4f1769de7bac4021ff8ed3e0bb311ea0e7e982e3dfb",
    }
)

_MACRO_NAME = re.compile(r"[A-Za-z]+")
#: A backslash at the end of a text that no backslash before it escapes.
_SINGLE_BACKSLASH_AT_END = re.compile(r"(?<!\\)(?:\\\\)*\\\Z")
#: A TeX control sequence as MathJax reads one: a backslash and then a run of
#: letters, or any one character.
_TEX_COMMAND = re.compile(r"\\(?:[A-Za-z]+|.)", re.DOTALL)


def stateful_commands(text: str) -> list[str]:
    """The commands in ``text``, by :data:`STATEFUL_TEX`, in order of first use."""

    return list(dict.fromkeys(command for command in _TEX_COMMAND.findall(text) if command in STATEFUL_TEX))


def attribute_commands(text: str) -> list[str]:
    """The commands in ``text``, by :data:`ATTRIBUTE_TEX`, in order of first use."""

    return list(dict.fromkeys(command for command in _TEX_COMMAND.findall(text) if command in ATTRIBUTE_TEX))


def in_the_way(blueprint: Path, relative: str) -> tuple[str, str] | None:
    """What stands where a regular file at ``relative`` in ``blueprint``
    would be: the first path on the way that is not a directory, or the file
    itself when it is not a regular file, with what it is. ``None`` when the
    file is regular or absent."""

    parts = relative.split("/")
    path = blueprint
    for index, part in enumerate(parts):
        path = path / part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return None
        last = index == len(parts) - 1
        if stat.S_ISREG(mode) if last else stat.S_ISDIR(mode):
            continue
        kind = (
            "a symlink"
            if stat.S_ISLNK(mode)
            else "a directory"
            if stat.S_ISDIR(mode)
            else "a file"
            if stat.S_ISREG(mode)
            else "a special file"
        )
        return "/".join(parts[: index + 1]), kind
    return None


def mathjax_script(blueprint_dir: str | Path) -> tuple[str, list[str]]:
    """The ``javascripts/mathjax.js`` the site gets for the vault at
    ``blueprint_dir``, and what refuses it: an invalid ``tex-macros.json``, or
    an edited copy of the configuration the vault used to keep."""

    blueprint = Path(blueprint_dir)
    issues: list[str] = []
    kept = blueprint / MATHJAX_SCRIPT
    # Anything else at that path is refused with the site's other assets.
    if kept.is_file() and in_the_way(blueprint, MATHJAX_SCRIPT) is None:
        digest = hashlib.sha256(kept.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        if digest not in _SHIPPED_CONFIGURATIONS:
            issues.append(
                f"{MATHJAX_SCRIPT}: autoform render writes the site's MathJax configuration, so the "
                f"edits in this copy would be lost; move any tex.macros to {TEX_MACROS} and delete it"
            )
    macros, macro_issues = _tex_macros(blueprint / TEX_MACROS)
    return _script(macros), issues + macro_issues


def _tex_macros(path: Path) -> tuple[dict[str, object], list[str]]:
    """The macros in ``path``, which need not exist, and what is wrong with them.

    Each maps a name of letters to a body, to ``[body, arguments]``, or to
    ``[body, arguments, default]``, as MathJax's ``tex.macros`` does. A body
    may not use a command in :data:`STATEFUL_TEX` or :data:`ATTRIBUTE_TEX`,
    since every article on the site could then reach it through the macro.

    Nor may a body or default end in a single backslash. MathJax expands a
    macro by splicing strings: the body with each argument in place of its
    ``#n``, then the rest of the formula. Where the text before a splice ends
    in a command name and the text after starts with a letter, it puts a
    space between them, so a name never runs on into the letters after it.
    The text before a ``#n`` cannot end in a single backslash, since ``\\#``
    is a character, and neither can an argument an article writes, since the
    backslash would take the next character with it. A body or default can,
    and then it joins the text after it into one command, which neither this
    check nor the article's sees: ``\\`` and then ``label`` is ``\\label``.
    """

    blocked = in_the_way(path.parent, path.name)
    if blocked is not None:
        return {}, [
            f"{TEX_MACROS}: is {blocked[1]}, where autoform reads the project's macros from a file; remove it"
        ]
    if not path.is_file():
        return {}, []
    try:
        macros = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_names)
    except (UnicodeDecodeError, ValueError) as exc:
        return {}, [f"{TEX_MACROS}: not valid JSON: {exc}"]
    if not isinstance(macros, dict):
        return {}, [f"{TEX_MACROS}: must be a JSON object from macro names to definitions"]
    issues: list[str] = []
    for name, definition in macros.items():
        if not _MACRO_NAME.fullmatch(name):
            issues.append(f"{TEX_MACROS}: {name!r} is not a macro name; use letters only")
            continue
        if isinstance(definition, str):
            texts = [definition]
        elif (
            isinstance(definition, list)
            and len(definition) in {2, 3}
            and isinstance(definition[0], str)
            and type(definition[1]) is int
            and all(isinstance(default, str) for default in definition[2:])
        ):
            texts = [definition[0], *definition[2:]]
            if not 0 <= definition[1] <= 9:
                issues.append(f"{TEX_MACROS}: \\{name} takes {definition[1]} arguments; use 0 to 9")
        else:
            issues.append(
                f"{TEX_MACROS}: \\{name} must be a body, [body, arguments], or [body, arguments, default]"
            )
            continue
        stateful = stateful_commands(" ".join(texts))
        if stateful:
            issues.append(
                f"{TEX_MACROS}: \\{name} uses {', '.join(stateful)}, which would change other formulas"
            )
        attributes = attribute_commands(" ".join(texts))
        if attributes:
            issues.append(
                f"{TEX_MACROS}: \\{name} uses {', '.join(attributes)}, which sets a symbol's color and other "
                "attributes; write the symbol itself"
            )
        if any(_SINGLE_BACKSLASH_AT_END.search(text) for text in texts):
            issues.append(
                f"{TEX_MACROS}: \\{name} ends a body or default in a single backslash, which would join the "
                "text after it into one command; double it or remove it"
            )
    return (macros if not issues else {}), issues


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
            "macros": dict(sorted(macros.items())),
            "inlineMath": [["$", "$"], ["\\(", "\\)"]],
            "displayMath": [["$$", "$$"], ["\\[", "\\]"]],
        },
        # Card formulas are written as the testimony renderer writes them, and
        # nothing else in a card is read.
        "card": {
            "packages": list(CARD_PACKAGES),
            "inlineMath": [["\\(", "\\)"]],
            "displayMath": [["\\[", "\\]"]],
            "processEnvironments": False,
            "processRefs": False,
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
        # Read-back cards, which only the card inputs read; their formulas;
        # and a class pattern nothing matches, since a class the page's input
        # is told to process would be read inside a card too.
        "cards": "bp-readback",
        "formulas": "arithmatex",
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
  // configuration and would typeset the cards with its own. It is stopped
  // instead, and the formulas stay as typed.
  if (window.MathJax && window.MathJax.version !== undefined) {
    window.MathJax.config.startup.typeset = false;
    window.MathJax.config.startup.pageReady = function () {};
    console.error("autoform: MathJax was loaded before javascripts/mathjax.js; formulas are left as typed. " +
      "Load MathJax only through javascripts/mathjax.js in mkdocs.yml.");
    return;
  }

  // One pass over the page as it is now. Every TeX input is new: the
  // articles get one, which skips the cards, and then each card gets one that
  // has read nothing else, so no definition, declaration, or label made
  // anywhere on the page reaches a card or outlives the pass. Each document
  // is finished before the next is made: a menu can start loading while one
  // waits, as when a reader picks another renderer, and the next is made
  // only once that load is done too.
  function typeset() {
    var startup = MathJax.startup;
    var root = startup.document.document;
    var steps = [[SETTINGS.page, {ignoreHtmlClass: SETTINGS.cards, processHtmlClass: SETTINGS.nothing}, null]];
    startup.adaptor.getElements(["." + SETTINGS.cards], root).forEach(function (card) {
      steps.push([SETTINGS.card, {ignoreHtmlClass: SETTINGS.cards, processHtmlClass: SETTINGS.formulas}, [card]]);
    });
    // What the renderer kept of the last page, which every renderer can
    // forget, though only CHTML has clearCache.
    startup.output.reset();
    menus = [];
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
      share(doc, options);
      return mathjax.handleRetriesFor(function () {
        doc.render();
      });
    });
  }

  // What the menus are loading, waited for without the promise a menu
  // gives, since a menu that is loading an accessibility component skips
  // redrawing the formulas it moves once anyone has asked for that promise.
  function settled() {
    var loads = shared && shared.menu.constructor.loadingPromises;
    return Promise.all(loads ? Array.from(loads.values()) : []);
  }

  // MathJax gives each document a menu of its own, which changes only that
  // document, but a reader sees one page. So a setting changed in any
  // formula's menu is made in the menu of every document of the pass, once
  // what it loads, such as another renderer, has loaded, and the next pass
  // starts with that renderer. The documents share, with the one MathJax
  // starts with, the renderers their menus load and the defaults a saved
  // setting is told apart from, which that first menu took before it read
  // the saved settings. Only settings are shared; each document keeps its
  // own TeX input. A menu that loads an accessibility component, such as the
  // explorer a screen reader uses, remakes only its own document with the
  // handler that component extends, so the documents the other menus hold
  // are remade with it too.
  var shared = null;
  var menus = [];
  var syncing = false;
  var scheduled = null;
  function share(doc, options) {
    var menu = doc.menu;
    if (!shared || !menu || typeof menu.saveUserSettings !== "function") return;
    menu.defaultSettings = shared.defaults;
    Object.keys(menu.jax).forEach(function (name) {
      if (menu.jax[name] && !shared.jax[name]) shared.jax[name] = menu.jax[name];
    });
    menu.jax = shared.jax;
    var save = menu.saveUserSettings;
    menu.saveUserSettings = function () {
      save.call(menu);
      if (syncing || scheduled === menu) return;
      scheduled = menu;
      var group = menus;
      // After the change in hand is done, and what it loads has loaded.
      Promise.resolve().then(settled).then(sync, sync);
      function sync() {
        scheduled = null;
        syncing = true;
        MathJax.startup.output = menu.document.outputJax;
        try {
          group.forEach(renew);
          group.forEach(function (entry) {
            var other = entry.menu;
            if (other === menu) return;
            Object.keys(menu.settings).forEach(function (name) {
              var variable = other.settings[name] !== menu.settings[name] && other.menu.pool.lookup(name);
              if (!variable) return;
              // A setting that redraws before what it uses is ready stops
              // there; the redraw below finishes it.
              try {
                variable.setValue(menu.settings[name]);
              } catch (error) {
                if (!error.retry) console.error("autoform: a menu setting failed", error);
              }
            });
          });
        } finally {
          syncing = false;
        }
        // A menu redraws its formulas at once, and stops partway, with them
        // gone, when what it uses is not ready, such as the speech engine
        // the explorer needs; each document is finished once it is.
        group.forEach(function (entry) {
          var doc = entry.menu.document;
          MathJax._.mathjax.mathjax.handleRetriesFor(function () {
            doc.render();
          }).catch(function (error) {
            console.error("autoform: typesetting failed", error);
          });
        });
      }
    };
    menus.push({menu: menu, options: options});
  }

  // A document the current handler did not make is remade by it, with the
  // same TeX input and the formulas it found, as a menu remakes its own; the
  // setting that loaded the component then redraws it.
  function renew(entry) {
    var old = entry.menu.document;
    var handler = MathJax.startup.handler;
    if (!handler || old instanceof handler.documentClass) return;
    var options = Object.assign({}, entry.options, {OutputJax: old.outputJax});
    var doc = MathJax._.mathjax.mathjax.document(old.document, options);
    Array.from(old.math).forEach(function (item) {
      doc.math.push(Object.assign(new doc.options.MathItem(), item));
    });
    doc.processed = old.processed;
    doc.menu = entry.menu;
    entry.menu.document = doc;
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
    var menu = MathJax.startup.document.menu;
    if (menu && typeof menu.saveUserSettings === "function") {
      shared = {menu: menu, defaults: menu.defaultSettings, jax: menu.jax};
    }
    // Material's instant navigation swaps the page in place, without running
    // this file again, and announces each page on document$, this one too.
    var pages = window.document$;
    if (pages && typeof pages.subscribe === "function") pages.subscribe(pass);
    else pass();
  }

  // MathJax reads its configuration from window.MathJax when it starts, and
  // a script listed after this one could assign its own there first, such as
  // the snippet Material's documentation gives, which typesets every formula
  // on the page with one input, cards and all. So window.MathJax is this
  // configuration until MathJax starts, which replaces it with MathJax itself
  // holding this configuration, and that is all it can become.
  var configuration = {
    loader: {load: SETTINGS.load, paths: {mathjax: SETTINGS.base}},
    tex: copy(SETTINGS.startup),
    options: {ignoreHtmlClass: SETTINGS.cards, processHtmlClass: SETTINGS.nothing, safeOptions: copy(SETTINGS.safe)},
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
    console.error("autoform: window.MathJax cannot be set; formulas are left as typed. Load MathJax only " +
      "through javascripts/mathjax.js in mkdocs.yml.", error);
    return;
  }

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
    "ATTRIBUTE_TEX",
    "CARD_PACKAGES",
    "MATHJAX_BUNDLE",
    "MATHJAX_SCRIPT",
    "MATHJAX_VERSION",
    "PAGE_PACKAGES",
    "STATEFUL_TEX",
    "TEX_MACROS",
    "attribute_commands",
    "in_the_way",
    "mathjax_script",
    "stateful_commands",
]
