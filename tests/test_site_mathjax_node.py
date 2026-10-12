"""The site's MathJax configuration, run in Node.js on a local MathJax as a
page would run it.

These tests need an unpacked MathJax 3.2.2 package root, the directory holding
``package.json`` and ``es5/``, named by ``AUTOFORM_MATHJAX_DIR``::

    AUTOFORM_MATHJAX_DIR=/path/to/MathJax-3.2.2 pytest tests/test_site_mathjax_node.py

Without Node.js or the variable they skip; a variable that names anything
else fails them, so a run meant to check MathJax cannot pass by skipping.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from autoform_cli.__main__ import main
from autoform_cli.markdown import site_converter
from autoform_cli.publishable import publishable_article
from tests.test_site_math import (
    _BASE,
    _BUNDLE,
    _CARD_PACKAGES,
    _MACROS,
    _PAGE_PACKAGES,
    _PROTECTED,
    _published,
    _render,
    _vault,
)

MATHJAX_VERSION = "3.2.2"
NODE = shutil.which("node")


def mathjax_package() -> Path:
    """The MathJax package root ``AUTOFORM_MATHJAX_DIR`` names, after checking
    that Node.js is installed and the package is MathJax 3.2.2."""

    if NODE is None:
        pytest.skip("Node.js is not on PATH")
    setting = os.environ.get("AUTOFORM_MATHJAX_DIR", "")
    if not setting:
        pytest.skip(f"AUTOFORM_MATHJAX_DIR is not set; set it to an unpacked MathJax {MATHJAX_VERSION} package")
    root = Path(setting)
    if (root / "node-main.js").is_file() and (root.parent / "package.json").is_file():
        pytest.fail(f"AUTOFORM_MATHJAX_DIR names MathJax's es5 directory; set it to the package root, {root.parent}")
    if not (root / "package.json").is_file() or not (root / "es5" / "node-main.js").is_file():
        pytest.fail(f"AUTOFORM_MATHJAX_DIR={setting} is not a MathJax package: no package.json and es5/node-main.js")
    version = json.loads((root / "package.json").read_text(encoding="utf-8")).get("version")
    if version != MATHJAX_VERSION:
        pytest.fail(f"AUTOFORM_MATHJAX_DIR={setting} is MathJax {version}, not {MATHJAX_VERSION}")
    return root


def test_the_protected_formula_forms_are_typeset(tmp_path: Path) -> None:
    """MathJax, run on the script render wrote, typesets both, as it does
    every other formula of an article."""

    reading = site_converter().convert(publishable_article(_PROTECTED)[0])
    page = f"<html><head></head><body><article>{reading}</article></body></html>"

    report = _node_report(tmp_path, _node_script(tmp_path), "new", first=page, second=page)

    assert report["errors"] == []
    (article,) = report["passes"][0]
    assert [math["tex"] for math in article["math"]] == ["a \\leq b", "\n\\sum_{i<n} i\n"]
    assert _LEQ in article["math"][0]["mml"]
    assert all("merror" not in math["mml"] for math in article["math"])


# Runs javascripts/mathjax.js in node against a local MathJax, as a page would,
# and reports what each document it made typeset. argv: the script, a page,
# the next page as Material's instant navigation shows it, "new" or "old":
# whether mkdocs.yml also lists the bundle, after the script, "after" when a
# project script listed after it assigns the configuration Material's
# documentation gives, and what the document MathJax starts with does before
# the first pass, as its menu does with saved settings: "renders" when it
# renders, "loads" when its menu is loading, or "quiet". The bundle's menu
# needs a browser, so each document gets one with what the script uses of it.
_HARNESS = r"""
"use strict";
const fs = require("fs");
const path = require("path");
const [scriptPath, firstPath, secondPath, project, override, startup, mode] = process.argv.slice(2);
const report = {
  injected: [], errors: [], passes: [], inventory: [], version: null, base: null, load: null, startup: null,
  resets: [],
};
global.window = globalThis;
let contentLoaded = null;
global.document = {
  readyState: project === "old" ? "loading" : "complete",
  head: {appendChild: (element) => report.injected.push(element.src)},
  createElement: () => ({}),
  addEventListener: (type, listener) => { if (type === "DOMContentLoaded") contentLoaded = listener; },
};
console.error = (...args) => report.errors.push(args.map(String).join(" "));
let shown = null;
global.document$ = {subscribe: (listener) => { shown = listener; }};

// A MathJax listed in mkdocs.yml before the script, which has started by the
// time the script runs, and would typeset the whole page as it is.
if (mode === "early") {
  global.MathJax = {loader: {load: ["input/tex", "output/chtml"]}, startup: {document: fs.readFileSync(firstPath, "utf8")}};
  const page = global.document;
  delete global.document;
  const early = require(path.join(process.env.AUTOFORM_MATHJAX_DIR, "es5", "node-main.js"));
  const started = global.MathJax;
  global.document = page;
  (0, eval)(fs.readFileSync(scriptPath, "utf8"));
  delete global.document;
  early.init({}).then(() => started.startup.promise).then(() => {
    report.version = started.version;
    report.startup = {math: Array.from(started.startup.document.math).length, subscribed: shown !== null};
  }).catch((error) => {
    report.errors.push("harness: " + error.stack);
  }).finally(() => {
    process.stdout.write(JSON.stringify(report));
  });
  return;
}

(0, eval)(fs.readFileSync(scriptPath, "utf8"));
report.base = MathJax.loader.paths.mathjax;
report.load = MathJax.loader.load.slice();
// The bundle carries these, and the menu, which needs a browser.
MathJax.loader.load.push("input/tex", "input/mml", "output/chtml", "a11y/assistive-mml");
MathJax.startup.document = fs.readFileSync(firstPath, "utf8");
// Every document gets a menu, the one MathJax starts with too, which is not
// listed with the documents of a pass.
let docs = [];
let first = null;
let made = null;
MathJax.startup.ready = () => {
  const original = MathJax._.mathjax.mathjax.document;
  MathJax._.mathjax.mathjax.document = function (root, options) {
    const doc = original.call(this, root, options);
    doc.menu = new Menu();
    if (first === null) first = doc;
    else docs.push(doc);
    // The page's document stops once, as a document does while a component
    // one of its formulas needs is loading, and meanwhile a reader picks
    // another renderer, which the menu loads.
    if (mode === "retry" && docs.length === 1 && report.passes.length === 0) {
      const render = doc.render;
      let stopped = false;
      doc.render = function () {
        if (!stopped) {
          stopped = true;
          Menu.loadingPromises.set("output/svg", new Promise((resolve) => setTimeout(() => {
            Menu.loadingPromises.delete("output/svg");
            made = docs.length;
            resolve();
          }, 40)));
          MathJax._.util.Retries.retryAfter(new Promise((resolve) => setTimeout(resolve, 20)));
        }
        return render.call(this);
      };
    }
    return doc;
  };
  MathJax.startup.defaultReady();
  // When the renderer forgets what it kept, by the documents of the pass made by then.
  if (mode === "spy") {
    const output = MathJax.startup.output;
    const reset = output.reset;
    output.reset = function () {
      report.resets.push(docs.length);
      return reset.apply(this, arguments);
    };
  }
  if (startup === "renders") first.render();
  if (startup === "loads") {
    Menu.loadingPromises.set("output/svg", new Promise((resolve) => setTimeout(() => {
      Menu.loadingPromises.delete("output/svg");
      made = docs.length;
      resolve();
    }, 20)));
  }
};
if (override === "after") {
  window.MathJax = {
    tex: {inlineMath: [["\\(", "\\)"]], displayMath: [["\\[", "\\]"]], processEscapes: true, processEnvironments: true},
    options: {ignoreHtmlClass: ".*|", processHtmlClass: "arithmatex"}
  };
}
// The page has been parsed; MathJax would take its document from here.
delete global.document;
const main = require(path.join(process.env.AUTOFORM_MATHJAX_DIR, "es5", "node-main.js"));
if (contentLoaded) contentLoaded();

// What the script uses of a document's menu: what the menus are loading.
class Menu {}
Menu.loadingPromises = new Map();

// The card a document reads, by its id, or null for the articles.
function cardOf(doc) {
  const element = doc.options.elements && doc.options.elements[0];
  const adaptor = MathJax.startup.adaptor;
  return element && adaptor.hasClass(element, "bp-readback") ? adaptor.getAttribute(element, "id") : null;
}

function describe() {
  return docs.map((doc) => {
    return {
      card: cardOf(doc),
      packages: doc.inputJax[0].parseOptions.options.packages.slice(),
      math: Array.from(doc.math).map((item) => ({tex: item.math, mml: MathJax.startup.toMML(item.root)})),
    };
  });
}

// Each command the page's input knows, with the function that handles it.
function inventory(tex) {
  const handlers = [];
  for (const entry of tex.parseOptions.handlers.get("macro")._configuration) {
    if (!(entry.item.map instanceof Map)) continue;
    for (const [name, macro] of entry.item.map) {
      if (typeof macro.func !== "function") continue;
      if (!handlers.includes(macro.func)) handlers.push(macro.func);
      report.inventory.push(["\\" + name, handlers.indexOf(macro.func)]);
    }
  }
}

main.init({}).then(async () => {
  report.version = MathJax.version;
  if (!shown) return;
  // document$ shows the first page again to each subscriber.
  await shown();
  report.passes.push(describe());
  report.startup = {math: Array.from(first.math).length, made};
  inventory(docs[0].inputJax[0]);
  docs = [];
  const adaptor = MathJax.startup.adaptor;
  const body = adaptor.body(MathJax.startup.document.document);
  for (const child of adaptor.childNodes(body).slice()) adaptor.remove(child);
  const next = adaptor.body(adaptor.parse(fs.readFileSync(secondPath, "utf8"), "text/html"));
  for (const child of adaptor.childNodes(next).slice()) adaptor.append(body, child);
  await shown();
  report.passes.push(describe());
}).catch((error) => {
  report.errors.push("harness: " + error.stack);
}).finally(() => {
  process.stdout.write(JSON.stringify(report));
});
"""

# Articles first, as the page's input reads them, then the cards. Each card
# repeats an article's attack on the cards after it, and its own formulas use
# what only articles may.
_FIRST_PAGE = r"""<html><head></head><body><article>
<p id="a1"><span class="arithmatex">\(\DeclareMathOperator{\leq}{>}\)</span> <span class="arithmatex">\(a \leq b\)</span></p>
<p id="a2"><span class="arithmatex">\(\boldsymbol{x} \cancel{y} \coloneqq \RR \norm{v}\)</span></p>
<p id="a3"><span class="arithmatex">\(\label{shared} \nothere\)</span> <span class="arithmatex">\(p \leq q\)</span></p>
<div class="bp-readback bp-readback-current" id="c1"><div class="bp-readback-title">Read-back $\leq$</div><p><span class="arithmatex">\(a \leq b\)</span> \(raw \leq\) $dollar$</p><p class="arithmatex">\[\DeclareMathOperator{\leq}{>} \label{shared} c \leq d\]</p></div>
<div class="bp-readback bp-readback-current" id="c2"><p><span class="arithmatex">\(e \leq f \label{shared}\)</span> <span class="arithmatex">\(\boldsymbol{x} \cancel{y} \coloneqq \RR\)</span></p></div>
</article></body></html>
"""
_SECOND_PAGE = r"""<html><head></head><body><article>
<p id="a4"><span class="arithmatex">\(g \leq h \label{shared}\)</span></p>
<div class="bp-readback bp-readback-current" id="c3"><p><span class="arithmatex">\(i \leq j\)</span></p></div>
</article></body></html>
"""
_LEQ = "<mo>&#x2264;</mo>"
_OVERRIDDEN = (
    "autoform: a script assigned window.MathJax after javascripts/mathjax.js; it is ignored, and the site's "
    "configuration is kept. Configure MathJax only through javascripts/mathjax.js and tex-macros.json."
)


def _node_report(
    tmp_path: Path,
    script: str,
    project: str,
    first: str = _FIRST_PAGE,
    second: str = _SECOND_PAGE,
    override: str = "none",
    startup: str = "quiet",
    mode: str = "none",
) -> dict:
    files = {"harness.js": _HARNESS, "mathjax.js": script, "first.html": first, "second.html": second}
    return _run_node(tmp_path, files, "mathjax.js", "first.html", "second.html", project, override, startup, mode)


def _run_node(tmp_path: Path, files: dict[str, str], *argv: str) -> dict:
    """Write ``files`` into ``tmp_path``, run the first in node with
    ``argv``, and decode the JSON it prints."""

    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    done = subprocess.run(
        [NODE, next(iter(files)), *argv],
        cwd=tmp_path,
        env=dict(os.environ, AUTOFORM_MATHJAX_DIR=str(mathjax_package())),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize("override", ["none", "after"])
@pytest.mark.parametrize("project", ["new", "old"])
def test_the_rendered_configuration_typesets_each_card_alone(tmp_path: Path, project: str, override: str) -> None:
    """MathJax itself, run on the script render wrote, as a page loads it now
    and as a project scaffolded with the bundle in mkdocs.yml loads it, and
    with a project script after it that assigns a configuration of its own."""

    report = _node_report(tmp_path, _node_script(tmp_path, json.dumps(_MACROS)), project, override=override)

    assert report["errors"] == ([] if override == "none" else [_OVERRIDDEN])
    assert report["injected"] == ([_BUNDLE] if project == "new" else [])
    assert report["base"] == _BASE
    assert report["version"] == "3.2.2"
    assert {"ui/safe", "[tex]/boldsymbol", "[tex]/cancel", "[tex]/mathtools"} <= set(report["load"])
    first, second = report["passes"]
    page, c1, c2 = first
    assert [document["card"] for document in first] == [None, "c1", "c2"]
    assert page["packages"] == _PAGE_PACKAGES
    assert c1["packages"] == c2["packages"] == _CARD_PACKAGES
    # The page's input reads no card; within the articles the attack works,
    # which is why check refuses it.
    assert [math["tex"] for math in page["math"]] == [
        "\\DeclareMathOperator{\\leq}{>}",
        "a \\leq b",
        "\\boldsymbol{x} \\cancel{y} \\coloneqq \\RR \\norm{v}",
        "\\label{shared} \\nothere",
        "p \\leq q",
    ]
    assert "<mo>&gt;</mo>" in page["math"][1]["mml"]
    articles = page["math"][2]["mml"]
    assert 'mathvariant="bold-italic"' in articles
    assert 'notation="updiagonalstrike"' in articles
    assert 'mathvariant="double-struck"' in articles
    assert "‖" in articles or "&#x2016;" in articles
    assert 'mathcolor="red"' not in articles
    assert 'mathcolor="red"' in page["math"][3]["mml"]
    # A card reads its formulas and nothing else, with nothing an article or
    # an earlier card defined.
    assert [math["tex"] for math in c1["math"]] == [
        "a \\leq b",
        "\\DeclareMathOperator{\\leq}{>} \\label{shared} c \\leq d",
    ]
    assert _LEQ in c1["math"][0]["mml"]
    assert [math["tex"] for math in c2["math"]] == ["e \\leq f \\label{shared}", "\\boldsymbol{x} \\cancel{y} \\coloneqq \\RR"]
    assert _LEQ in c2["math"][0]["mml"]
    assert "merror" not in c2["math"][0]["mml"]
    # What only articles may use is an unknown command in a card, shown in red.
    unknown = c2["math"][1]["mml"]
    for command in ("\\boldsymbol", "\\cancel", "\\coloneqq", "\\RR"):
        assert f'<mtext mathcolor="red">{command}</mtext>' in unknown
    # The next page, shown in place, gets new inputs again.
    assert [document["card"] for document in second] == [None, "c3"]
    assert _LEQ in second[0]["math"][0]["mml"]
    assert "merror" not in second[0]["math"][0]["mml"]
    assert _LEQ in second[1]["math"][0]["mml"]
    # Every command that shares its handler with one check refuses is refused too.
    from autoform_cli.mathjax import STATEFUL_TEX

    stateful = {handler for name, handler in report["inventory"] if name in STATEFUL_TEX}
    assert {name for name, handler in report["inventory"] if handler in stateful} <= STATEFUL_TEX
    assert {"\\DeclareMathOperator", "\\DeclarePairedDelimiter", "\\label", "\\newtagform"} <= {
        name for name, _ in report["inventory"]
    }


#: Typesets every command and environment the page's packages define with a
#: color in brackets in each place an option could go, and prints those that
#: color what they draw.
_OPTION_INVENTORY = r"""
"use strict";
const path = require("path");
const [packages] = process.argv.slice(2);
const names = JSON.parse(packages);
const config = {
  loader: {load: ["input/tex-base", ...names.filter((name) => name !== "base").map((name) => `[tex]/${name}`)]},
  tex: {packages: names},
};
require(path.join(process.env.AUTOFORM_MATHJAX_DIR, "es5", "node-main.js")).init(config).then((MathJax) => {
  MathJax.tex2mml("x");
  const tex = MathJax.startup.input[0];
  const commands = new Set();
  for (const kind of ["macro", "environment"]) {
    for (const entry of tex.parseOptions.handlers.get(kind)._configuration) {
      if (!(entry.item.map instanceof Map)) continue;
      for (const [name] of entry.item.map) commands.add(kind === "macro" ? "\\" + name : "env:" + name);
    }
  }
  const colored = new Set();
  for (const command of commands) {
    for (const key of ["mathcolor", "color", "mathbackground", "background"]) {
      const option = `[${key}=#31A24C]`;
      const forms = command.startsWith("env:")
        ? [`\\begin{${command.slice(4)}}${option}{x}{x} x \\end{${command.slice(4)}}`]
        : [0, 1, 2, 3].map((before) => command + "{x}".repeat(before).replace("{x}", before ? "{mi}" : "") + option + "{x}{x}");
      for (const form of forms) {
        let mml = "";
        try { mml = MathJax.tex2mml(form); } catch (error) {}
        if (/(color|background)="#31A24C"/.test(mml)) colored.add(command);
      }
    }
  }
  console.log(JSON.stringify({commands: commands.size, colored: [...colored].sort()}));
});
"""


def test_the_commands_an_option_colors_are_all_refused(tmp_path: Path) -> None:
    """The commands of the page's packages that color what they draw when
    given a color in brackets, found by typesetting each of them, are the
    ones check refuses, wholly or with an option."""

    from autoform_cli.mathjax import ATTRIBUTE_TEX, OPTION_TEX, PAGE_PACKAGES

    report = _run_node(tmp_path, {"inventory.js": _OPTION_INVENTORY}, json.dumps(list(PAGE_PACKAGES)))

    assert report["commands"] > 900
    assert set(report["colored"]) == ATTRIBUTE_TEX | OPTION_TEX


def test_the_rendered_configuration_refuses_another_release(tmp_path: Path) -> None:
    report = _node_report(tmp_path, _node_script(tmp_path).replace('"version": "3.2.2"', '"version": "3.2.1"'), "old")

    assert report["passes"] == []
    assert report["errors"] == [
        "autoform: this page loaded MathJax 3.2.2, but javascripts/mathjax.js was written for 3.2.1; formulas "
        "are left as typed. Load MathJax only through javascripts/mathjax.js in mkdocs.yml."
    ]


def _node_script(tmp_path: Path, macros: str | None = None) -> str:
    """The script render wrote for a vault, or a skip without node and MathJax."""

    mathjax_package()
    return (_render(_vault(tmp_path / "vault", macros=macros)) / "javascripts/mathjax.js").read_text(encoding="utf-8")


def test_each_document_is_made_once_the_one_before_it_is_finished(tmp_path: Path) -> None:
    """A menu can start loading while a document waits, and no document is
    made while a menu is loading, so the next is made only after both."""

    report = _node_report(tmp_path, _node_script(tmp_path, json.dumps(_MACROS)), "new", mode="retry")

    assert report["errors"] == []
    # Only the page's document had been made when the load finished.
    assert report["startup"]["made"] == 1
    assert [document["card"] for document in report["passes"][0]] == [None, "c1", "c2"]
    assert "merror" not in report["passes"][0][0]["math"][2]["mml"]


def test_no_class_makes_the_page_input_read_a_card(tmp_path: Path) -> None:
    """A document made outside MathJax's startup reads an element of class
    mathjax_process wherever it is unless told otherwise, inside a card too."""

    page = (
        '<html><head></head><body><article><p id="a1"><span class="arithmatex">\\(a\\)</span></p>'
        '<div class="bp-readback bp-readback-current" id="c1"><p><span class="mathjax_process">\\(b \\leq c\\)'
        '</span> <span class="arithmatex">\\(d\\)</span></p></div></article></body></html>'
    )

    report = _node_report(tmp_path, _node_script(tmp_path), "new", first=page, second=page)

    assert report["errors"] == []
    article, card = report["passes"][0]
    assert [math["tex"] for math in article["math"]] == ["a"]
    assert [math["tex"] for math in card["math"]] == ["d"]


def test_the_renderer_forgets_the_last_page_before_each_pass(tmp_path: Path) -> None:
    report = _node_report(tmp_path, _node_script(tmp_path), "new", mode="spy")

    assert report["errors"] == []
    assert len(report["passes"]) == 2
    # Once a pass, before its first document is made.
    assert report["resets"] == [0, 0]


def test_a_mathjax_started_before_the_script_typesets_nothing(tmp_path: Path) -> None:
    """A MathJax listed before the script started without the site's
    configuration and would typeset the cards with the articles' input."""

    report = _node_report(tmp_path, _node_script(tmp_path), "new", mode="early")

    assert report["errors"] == [
        "autoform: MathJax was loaded before javascripts/mathjax.js; formulas are left as typed. Load MathJax "
        "only through javascripts/mathjax.js in mkdocs.yml."
    ]
    assert report["version"] == "3.2.2"
    assert report["startup"] == {"math": 0, "subscribed": False}


# Text a page shows outside the formulas its Markdown marked: navigation, a
# table of contents, a lead, a title, and a graph's source, as the site
# prints them, each with TeX check never judged as a formula.
_UNMARKED = r"""<html><head></head><body>
<nav class="md-nav"><a href="#top">\(\DeclareMathOperator{\leq}{>}\) $\gdef\x{1}$</a></nav>
<div class="md-sidebar"><ul><li>$\label{toc}$</li></ul></div>
<article>
<div class="bp-hero"><p class="bp-hero-lead">Lead $\DeclareMathOperator{\leq}{>}$</p></div>
<div class="bp-thmwrapper"><div class="bp-thmheading"><span class="bp-thmtitle">Top `$\DeclareMathOperator{\leq}{>}$`</span></div>
<div class="mermaid">graph TD; a["$\label{m}$"]</div>
<p><span class="arithmatex">\(a \leq b\)</span></p>
</div></article></body></html>
"""


def test_the_page_input_reads_only_the_formulas_markdown_marked(tmp_path: Path) -> None:
    """Check judges the formulas the site's Markdown marks in an article, so
    those are all the page's input reads."""

    report = _node_report(tmp_path, _node_script(tmp_path), "new", first=_UNMARKED, second=_UNMARKED)

    assert report["errors"] == []
    for (page,) in report["passes"]:
        assert [math["tex"] for math in page["math"]] == ["a \\leq b"]
        assert _LEQ in page["math"][0]["mml"]


def test_a_title_or_discussion_shown_as_typed_is_not_typeset(tmp_path: Path) -> None:
    """Check reads a title and a discussion value as Markdown, where backticks
    make code, but the site prints them as typed, backticks and all."""

    blueprint = _vault(tmp_path / "vault")
    top = blueprint / "roadmap/top.md"
    top.write_text(
        top.read_text(encoding="utf-8")
        .replace("# Top", "# Top `$\\DeclareMathOperator{\\leq}{>}$`")
        .replace("discussion: 42", "discussion: `$\\label{x} \\DeclareMathOperator{\\leq}{>}$`"),
        encoding="utf-8",
    )
    chapter = blueprint / "roadmap/README.md"
    chapter.write_text(
        chapter.read_text(encoding="utf-8").replace("main result.", "main result, where $a \\leq b$."),
        encoding="utf-8",
    )
    assert main(["check", str(blueprint)]) == 0
    site = _render(blueprint)
    published = _published(site / "roadmap/README.md")
    assert "Top `$\\DeclareMathOperator{\\leq}{&gt;}$`" in published
    assert "`$\\label{x} \\DeclareMathOperator{\\leq}{&gt;}$`" in published
    page = f"<html><head></head><body><article>{published}</article></body></html>"
    mathjax_package()
    script = (site / "javascripts/mathjax.js").read_text(encoding="utf-8")

    report = _node_report(tmp_path, script, "new", first=page, second=page)

    assert report["errors"] == []
    articles = report["passes"][0][0]
    assert [math["tex"] for math in articles["math"]] == ["a \\leq b"]
    assert _LEQ in articles["math"][0]["mml"]


def test_no_formula_gives_its_output_a_class_style_id_or_link(tmp_path: Path) -> None:
    """Base's \\mmlToken sets any attribute a formula writes. Check refuses it
    in articles, and the testimony validator in cards, but each document's
    filter is what keeps the attributes off the page if one gets through:
    MathJax gives a document made outside its startup the filter's defaults,
    which pass classes that start with mjx-, colors and margins, and links."""

    script = _node_script(tmp_path)
    token = (
        "\\mmlToken{mi}[style='margin-top:-60em;color:red',class='mjx-x',href='https://evil.example/',"
        "id='mjx-x']{x}"
    )
    page = (
        f'<html><head></head><body><article><p><span class="arithmatex">\\({token}\\)</span></p>'
        f'<div class="bp-readback bp-readback-current" id="c1"><p><span class="arithmatex">\\({token}\\)'
        "</span></p></div></article></body></html>"
    )

    report = _node_report(tmp_path, script, "new", first=page, second=page)

    assert report["errors"] == []
    article, card = report["passes"][0]
    assert card["card"] == "c1"
    for document in (article, card):
        assert len(document["math"]) == 1
        mml = document["math"][0]["mml"]
        assert "<mi>x</mi>" in mml, mml


def _articles(*formulas: str) -> str:
    spans = "".join(f'<p><span class="arithmatex">\\({formula}\\)</span></p>' for formula in formulas)
    return f"<html><head></head><body><article>{spans}</article></body></html>"


#: Each tries to make a project macro spell \DeclareMathOperator out of what
#: follows it, and is followed by the formula that shows whether it did.
_SPLICES = (
    "\\op{\\D}{\\leq}{>}",
    "\\opt[\\D]{\\leq}{>}",
    "\\nl DeclareMathOperator{\\leq}{>}",
    "\\op\\D{\\leq}{>}",
)


def test_project_macros_cannot_join_into_a_command(tmp_path: Path) -> None:
    """MathJax expands a macro by splicing strings. A body that ends in a
    single backslash joins the text after it into one command, so it is
    refused; what is left spells nothing new, as MathJax itself shows."""

    mathjax_package()
    from autoform_cli.mathjax import _script

    accepted = {"op": ["#1eclareMathOperator", 1], "opt": ["#1eclareMathOperator", 1, "x"], "nl": "\\\\"}
    blueprint = _vault(tmp_path / "vault", macros=json.dumps(accepted))
    assert main(["check", str(blueprint)]) == 0
    script = (_render(blueprint) / "javascripts/mathjax.js").read_text(encoding="utf-8")
    page = _articles(*(formula for splice in _SPLICES for formula in (splice, "a \\leq b")))

    report = _node_report(tmp_path, script, "new", first=page, second=page)

    assert report["errors"] == []
    shown = report["passes"][0][0]["math"]
    assert [math["tex"] for math in shown[::2]] == list(_SPLICES)
    for splice, math in zip(_SPLICES, shown[1::2], strict=True):
        assert _LEQ in math["mml"] and "&gt;" not in math["mml"], splice

    # The refused forms, written past check, do declare it.
    for macros, splice in (
        ({"bs": "\\"}, "\\bs DeclareMathOperator{\\leq}{>}"),
        ({"L": ["#1DeclareMathOperator", 1, "\\"]}, "\\L{\\leq}{>}"),
    ):
        page = _articles(splice, "a \\leq b")
        report = _node_report(tmp_path, _script(macros), "new", first=page, second=page)
        assert report["errors"] == []
        assert "<mo>&gt;</mo>" in report["passes"][0][0]["math"][1]["mml"], splice


def test_the_document_mathjax_starts_with_holds_no_formula(tmp_path: Path) -> None:
    """MathJax makes a document for the page when it starts, with a menu,
    which renders it when it applies a saved setting. That document finds
    nothing, so every formula is read by a pass, with a new input."""

    report = _node_report(tmp_path, _node_script(tmp_path), "new", startup="renders")

    assert report["errors"] == []
    assert report["startup"]["math"] == 0
    page, c1, c2 = report["passes"][0]
    assert len(page["math"]) == 5
    assert len(c1["math"]) == len(c2["math"]) == 2


def test_no_document_is_made_while_a_menu_is_loading(tmp_path: Path) -> None:
    """A menu that asks for a renderer another menu is loading is never told
    it has loaded, so the documents of a pass wait for the load the first
    menu starts for a saved setting."""

    report = _node_report(tmp_path, _node_script(tmp_path), "new", startup="loads")

    assert report["errors"] == []
    assert report["startup"]["made"] == 0
    assert [document["card"] for document in report["passes"][0]] == [None, "c1", "c2"]
