"""Read-back cards typeset in Node.js by MathJax 3.2.2, with the script render wrote.

These tests need an unpacked MathJax 3.2.2 package root, the directory holding
``package.json`` and ``es5/``, named by ``AUTOFORM_MATHJAX_DIR``::

    AUTOFORM_MATHJAX_DIR=/path/to/MathJax-3.2.2 pytest tests/test_site_mathjax_node.py

Without Node.js or the variable they skip, unless ``AUTOFORM_REQUIRE_MATHJAX_TESTS``
is ``1``, as in CI's MathJax job, which fails them instead; a variable that
names anything else fails them, so a run meant to check MathJax cannot pass by
skipping.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_site_math import _BASE, _BUNDLE, _CARD_PACKAGES, _MACROS, _PAGE_PACKAGES, _render, _vault

MATHJAX_VERSION = "3.2.2"
NODE = shutil.which("node")


def mathjax_package(*, node: bool = True) -> Path:
    """The MathJax package root ``AUTOFORM_MATHJAX_DIR`` names, after checking
    that the package is MathJax 3.2.2 and, with ``node``, that Node.js is
    installed to run it; a browser that is served the package needs no Node."""

    # CI sets AUTOFORM_REQUIRE_MATHJAX_TESTS, so a missing Node or package fails there instead of skipping.
    unavailable = pytest.fail if os.environ.get("AUTOFORM_REQUIRE_MATHJAX_TESTS") == "1" else pytest.skip
    if node and NODE is None:
        unavailable("Node.js is not on PATH")
    setting = os.environ.get("AUTOFORM_MATHJAX_DIR", "")
    if not setting:
        unavailable(f"AUTOFORM_MATHJAX_DIR is not set; set it to an unpacked MathJax {MATHJAX_VERSION} package")
    root = Path(setting)
    if (root / "node-main.js").is_file() and (root.parent / "package.json").is_file():
        pytest.fail(f"AUTOFORM_MATHJAX_DIR names MathJax's es5 directory; set it to the package root, {root.parent}")
    if not (root / "package.json").is_file() or not (root / "es5" / "node-main.js").is_file():
        pytest.fail(f"AUTOFORM_MATHJAX_DIR={setting} is not a MathJax package: no package.json and es5/node-main.js")
    version = json.loads((root / "package.json").read_text(encoding="utf-8")).get("version")
    if version != MATHJAX_VERSION:
        pytest.fail(f"AUTOFORM_MATHJAX_DIR={setting} is MathJax {version}, not {MATHJAX_VERSION}")
    return root


# Runs javascripts/mathjax.js in node against a local MathJax, as a page would,
# and reports what each document it made typeset. argv: the script, a page,
# the next page as Material's instant navigation shows it, "new" or "old":
# whether mkdocs.yml also lists the bundle, after the script, and "after" when
# a project script listed after it assigns the configuration Material's
# documentation gives, or "version" when the bundle is another MathJax release,
# and "explorer" when the next page is instead what the
# dependency explorer draws into the first, after which it asks for a pass.
# The bundle's menu needs a browser, so each document gets one with what the
# script uses of it.
_HARNESS = r"""
"use strict";
const fs = require("fs");
const path = require("path");
const [scriptPath, firstPath, secondPath, project, override, then] = process.argv.slice(2);
const report = {injected: [], errors: [], passes: [], version: null, base: null, load: null, early: null};
global.window = globalThis;
let contentLoaded = null;
const listeners = {};
global.document = {
  readyState: project === "old" ? "loading" : "complete",
  head: {appendChild: (element) => report.injected.push(element.src)},
  createElement: () => ({}),
  addEventListener: (type, listener) => { listeners[type] = listener; if (type === "DOMContentLoaded") contentLoaded = listener; },
};
console.error = (...args) => report.errors.push(args.map(String).join(" "));
let shown = null;
global.document$ = {subscribe: (listener) => { shown = listener; }};

(0, eval)(fs.readFileSync(scriptPath, "utf8"));
// The explorer can ask for a pass before MathJax has started.
if (then === "explorer") report.early = String(listeners["autoform:typeset"]());
report.base = MathJax.loader.paths.mathjax;
report.load = MathJax.loader.load.slice();
// The bundle carries these, and the menu, which needs a browser.
MathJax.loader.load.push("input/tex", "input/mml", "output/chtml", "a11y/assistive-mml");
MathJax.startup.document = fs.readFileSync(firstPath, "utf8");
let docs = [];
let first = null;
MathJax.startup.ready = () => {
  if (override === "version") MathJax.version = "3.2.1";
  const original = MathJax._.mathjax.mathjax.document;
  MathJax._.mathjax.mathjax.document = function (root, options) {
    // What the script asked of the document, before MathJax drops what Node lacks, such as the menu.
    const requested = JSON.parse(JSON.stringify({
      enableMenu: options.enableMenu, renderActions: options.renderActions, skipHtmlTags: options.skipHtmlTags,
    }));
    const doc = original.call(this, root, options);
    doc.requested = requested;
    doc.menu = new Menu();
    if (first === null) first = doc;
    else docs.push(doc);
    return doc;
  };
  MathJax.startup.defaultReady();
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
  return docs.map((doc) => ({
    card: cardOf(doc),
    packages: doc.inputJax[0].parseOptions.options.packages.slice(),
    requested: doc.requested,
    math: Array.from(doc.math).map((item) => ({tex: item.math, mml: MathJax.startup.toMML(item.root)})),
  }));
}

main.init({}).then(async () => {
  report.version = MathJax.version;
  if (!shown) return;
  // document$ shows the first page again to each subscriber.
  await shown();
  report.passes.push(describe());
  docs = [];
  const adaptor = MathJax.startup.adaptor;
  const body = adaptor.body(MathJax.startup.document.document);
  if (then !== "explorer") for (const child of adaptor.childNodes(body).slice()) adaptor.remove(child);
  const next = adaptor.body(adaptor.parse(fs.readFileSync(secondPath, "utf8"), "text/html"));
  for (const child of adaptor.childNodes(next).slice()) adaptor.append(body, child);
  await (then === "explorer" ? listeners["autoform:typeset"]() : shown());
  report.passes.push(describe());
  report.startup = Array.from(MathJax.startup.document.math).length;
  // Each glyph on the page needs its rule in the renderer's one style sheet.
  const sheet = adaptor.textContent(MathJax.startup.output.chtmlStyles);
  const glyphs = new Set(Array.from(adaptor.outerHTML(body).matchAll(/class="(mjx-c[0-9A-F]+)/g), (match) => match[1]));
  report.unstyled = Array.from(glyphs).filter((glyph) => !sheet.includes("." + glyph));
}).catch((error) => {
  report.errors.push("harness: " + error.stack);
}).finally(() => {
  process.stdout.write(JSON.stringify(report));
});
"""

# Articles first, as the page's input reads them, then the cards as render
# writes them. A card defines what the cards after it use, and uses what only
# articles may.
_FIRST_PAGE = r"""<html><head></head><body><article>
<p id="a1"><span class="arithmatex">\(a \leq b\)</span> <span class="arithmatex">\(\RR \norm{v}\)</span></p>
<div class="bp-readback bp-readback-current" id="c1"><div class="bp-readback-title">Read-back $\leq$</div><div class="bp-readback-body"><span class="bp-card-formula">\(a \leq b\)</span> \(raw \leq\) $dollar$&#10;<span class="bp-card-formula">\[\DeclareMathOperator{\leq}{>} \label{shared} c \leq d\]</span></div></div>
<div class="bp-readback bp-readback-current" id="c2"><div class="bp-readback-body"><span class="bp-card-formula">\(e \leq f \label{shared}\)</span> <span class="bp-card-formula">\(\boldsymbol{x} \coloneqq \RR\)</span></div></div>
</article></body></html>
"""
_SECOND_PAGE = r"""<html><head></head><body><article>
<p id="a2"><span class="arithmatex">\(g \leq h\)</span></p>
<div class="bp-readback bp-readback-current" id="c3"><div class="bp-readback-body"><span class="bp-card-formula">\(i \leq j\)</span></div></div>
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
    then: str = "page",
) -> dict:
    """Run the harness on ``script`` and the two pages, and decode the JSON it prints."""

    files = {"harness.js": _HARNESS, "mathjax.js": script, "first.html": first, "second.html": second}
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    done = subprocess.run(
        [NODE, "harness.js", "mathjax.js", "first.html", "second.html", project, override, then],
        cwd=tmp_path,
        env=dict(os.environ, AUTOFORM_MATHJAX_DIR=str(mathjax_package())),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _node_script(tmp_path: Path, macros: str | None = None) -> str:
    """The script render wrote for a vault, or a skip without node and MathJax."""

    mathjax_package()
    return (_render(_vault(tmp_path / "vault", macros=macros)) / "javascripts/mathjax.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("override", ["none", "after"])
@pytest.mark.parametrize("project", ["new", "old"])
def test_the_rendered_configuration_typesets_each_card_alone(tmp_path: Path, project: str, override: str) -> None:
    """MathJax itself, run on the script render wrote, as a page loads it now
    and as a project scaffolded with the bundle in mkdocs.yml loads it, and
    with a project script after it that assigns a configuration of its own."""

    report = _node_report(tmp_path, _node_script(tmp_path, json.dumps(_MACROS)), project, override=override)

    assert report["errors"] == ([] if override == "none" else [_OVERRIDDEN])
    assert report["injected"] == ([_BUNDLE] if project == "new" else [])
    assert (report["base"], report["version"]) == (_BASE, "3.2.2")
    first, second = report["passes"]
    page, c1, c2 = first
    assert [document["card"] for document in first] == [None, "c1", "c2"]
    assert page["packages"] == _PAGE_PACKAGES
    assert c1["packages"] == c2["packages"] == _CARD_PACKAGES
    # The page's input reads no card, and the project's macros work there.
    assert [math["tex"] for math in page["math"]] == ["a \\leq b", "\\RR \\norm{v}"]
    assert "merror" not in page["math"][1]["mml"] and 'mathcolor="red"' not in page["math"][1]["mml"]
    # A card reads its formulas and nothing else, with nothing an earlier card
    # defined.
    assert [math["tex"] for math in c1["math"]] == [
        "a \\leq b",
        "\\DeclareMathOperator{\\leq}{>} \\label{shared} c \\leq d",
    ]
    assert _LEQ in c1["math"][0]["mml"]
    assert [math["tex"] for math in c2["math"]] == ["e \\leq f \\label{shared}", "\\boldsymbol{x} \\coloneqq \\RR"]
    assert _LEQ in c2["math"][0]["mml"]
    assert "merror" not in c2["math"][0]["mml"]
    # What only articles may use is an unknown command in a card, shown in red.
    for command in ("\\boldsymbol", "\\coloneqq", "\\RR"):
        assert f'<mtext mathcolor="red">{command}</mtext>' in c2["math"][1]["mml"]
    # The next page, shown in place, gets new inputs again.
    assert [document["card"] for document in second] == [None, "c3"]
    assert _LEQ in second[0]["math"][0]["mml"] and _LEQ in second[1]["math"][0]["mml"]


def test_another_mathjax_release_leaves_every_formula_as_typed(tmp_path: Path) -> None:
    """The script is written for one MathJax release. Under another, which a
    site that lists a bundle of its own can load, it typesets nothing and says
    why."""

    report = _node_report(tmp_path, _node_script(tmp_path), "new", override="version")

    assert report["errors"] == [
        "autoform: this page loaded MathJax 3.2.1, but javascripts/mathjax.js was written for 3.2.2; formulas "
        "are left as typed. Load MathJax only through javascripts/mathjax.js in mkdocs.yml."
    ]
    assert report["passes"] == []


# A MathJax that mkdocs.yml lists before javascripts/mathjax.js: loaded when
# the script runs, and started afterwards with its own configuration. Reports
# how many formulas it typeset. argv: the script, or "none" for a page without
# it, and the page.
_LOADED_FIRST_HARNESS = r"""
"use strict";
const fs = require("fs");
const path = require("path");
const [scriptPath, pagePath] = process.argv.slice(2);
const report = {errors: [], math: null};
global.window = globalThis;
console.error = (...args) => report.errors.push(args.map(String).join(" "));
const main = require(path.join(process.env.AUTOFORM_MATHJAX_DIR, "es5", "node-main.js"));
const loaded = window.MathJax;
global.document = {readyState: "complete", head: {appendChild: () => {}}, createElement: () => ({}),
  addEventListener: () => {}};
if (scriptPath !== "none") (0, eval)(fs.readFileSync(scriptPath, "utf8"));
delete global.document;
loaded.config.startup.document = fs.readFileSync(pagePath, "utf8");
loaded.config.loader.load.push("input/tex", "output/chtml");
main.init({}).then(() => {
  report.math = Array.from(loaded.startup.document.math).length;
}).catch((error) => {
  report.errors.push("harness: " + error.stack);
}).finally(() => {
  process.stdout.write(JSON.stringify(report));
});
"""


def test_a_mathjax_listed_before_the_script_typesets_nothing(tmp_path: Path) -> None:
    """A bundle listed in mkdocs.yml before javascripts/mathjax.js starts
    without the site's configuration and would typeset every formula on the
    page with its own, a card's too. The script stops it instead, and says
    why."""

    files = {
        "harness.js": _LOADED_FIRST_HARNESS,
        "mathjax.js": _node_script(tmp_path),
        "page.html": '<html><body><p><span class="arithmatex">\\(a \\leq b\\)</span> \\(c\\)</p></body></html>',
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")

    def run(script: str) -> dict:
        done = subprocess.run(
            [NODE, "harness.js", script, "page.html"],
            cwd=tmp_path,
            env=dict(os.environ, AUTOFORM_MATHJAX_DIR=str(mathjax_package())),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    assert run("mathjax.js") == {
        "errors": [
            "autoform: MathJax was loaded before javascripts/mathjax.js; formulas are left as typed. Load MathJax "
            "only through javascripts/mathjax.js in mkdocs.yml."
        ],
        "math": 0,
    }
    # The same MathJax without the script typesets both formulas.
    assert run("none") == {"errors": [], "math": 2}


def test_no_class_makes_an_input_read_more_of_a_card(tmp_path: Path) -> None:
    """A document made outside MathJax's startup reads an element of class
    mathjax_process wherever it is unless told otherwise, inside a card too."""

    page = (
        '<html><head></head><body><article><p id="a1"><span class="arithmatex">\\(a\\)</span></p>'
        '<div class="bp-readback bp-readback-current" id="c1"><div class="bp-readback-body">'
        '<span class="mathjax_process">\\(b \\leq c\\)</span> <span class="bp-card-formula">\\(d\\)</span>'
        "</div></div></article></body></html>"
    )

    report = _node_report(tmp_path, _node_script(tmp_path), "new", first=page, second=page)

    assert report["errors"] == []
    article, card = report["passes"][0]
    assert [math["tex"] for math in article["math"]] == ["a"]
    assert [math["tex"] for math in card["math"]] == ["d"]


def test_no_card_formula_gives_its_output_a_class_style_id_or_link(tmp_path: Path) -> None:
    """Base's \\mmlToken sets any attribute a formula writes. The testimony
    validator refuses it, but a card's own filter is what keeps the attributes
    off the page if one gets through: MathJax gives a document made outside its
    startup the filter's defaults, which pass classes that start with mjx-,
    colors and margins, and links."""

    script = _node_script(tmp_path)
    token = (
        "\\mmlToken{mi}[style='margin-top:-60em;color:red',class='mjx-x',href='https://evil.example/',"
        "id='mjx-x']{x}"
    )
    page = (
        '<html><head></head><body><article><div class="bp-readback bp-readback-current" id="c1">'
        f'<div class="bp-readback-body"><span class="bp-card-formula">\\({token}\\)</span></div></div>'
        "</article></body></html>"
    )

    report = _node_report(tmp_path, script, "new", first=page, second=page)

    assert report["errors"] == []
    _, card = report["passes"][0]
    assert card["card"] == "c1"
    assert len(card["math"]) == 1
    assert "<mi>x</mi>" in card["math"][0]["mml"], card["math"][0]["mml"]


def test_the_explorer_s_titles_and_summaries_are_typeset_as_articles_are(tmp_path: Path) -> None:
    """The dependency explorer draws authored titles and summaries after the
    page's pass, marked with the site's formula class, and asks for another
    pass; that pass reads only what it marked, with the articles' input and
    the project's macros, and an ask before MathJax starts does nothing."""

    drawn = (
        '<html><head></head><body><aside class="bp-dag-navigation"><div class="bp-dag-navigation-inner">'
        '<h1 class="bp-dag-navigation-title arithmatex">Groups $\\mathbb{Z}/n$</h1>'
        '<p class="bp-dag-navigation-summary arithmatex">Cyclic groups $C_n \\cong \\RR$ have \\(\\varphi(n)\\) generators.</p>'
        '<p class="bp-dag-navigation-counts">2 items, and $x$ in text the explorer did not mark</p>'
        '<ol class="bp-dag-navigation-path"><li><a class="arithmatex" href="#up">Up to $t_0$</a></li></ol>'
        '<a class="bp-dag-navigation-link" href="#base"><span class="arithmatex">Order of $g^k$</span></a>'
        '<button class="bp-dag-sheet-handle arithmatex">Handle $h_1$</button>'
        '<button class="bp-dag-relation-button arithmatex">Relation $r^2$</button>'
        '<p class="bp-dag-summary arithmatex">By \\ref{thm:a}, which is no formula, $q$ holds.</p>'
        "</div></aside></body></html>"
    )
    # An article formula whose hidden assistive copy holds a pair of dollar signs.
    first = _FIRST_PAGE.replace(
        '<p id="a1">', '<p id="a1"><span class="arithmatex">\\(\\text{costs \\$5 and \\$6}\\)</span> '
    )

    report = _node_report(
        tmp_path, _node_script(tmp_path, json.dumps(_MACROS)), "new", first=first, second=drawn, then="explorer"
    )

    assert report["errors"] == []
    assert report["early"] == "undefined"
    first, explorer = report["passes"]
    assert [document["card"] for document in first] == [None, "c1", "c2"]
    # A new page input, which finds only the explorer's marked text: the
    # article's formulas and the cards were typeset by the first pass. A
    # formula in a link or a button, or marked on one, gets a page input of
    # its own, in a document without MathJax's menu, which would make it a
    # control inside the control.
    page, link = explorer[:2]
    assert page["card"] is None and page["packages"] == _PAGE_PACKAGES
    # A bare \\ref is no formula, and MathJax's output, the assistive copy
    # included, is not read again.
    assert [math["tex"] for math in page["math"]] == ["\\mathbb{Z}/n", "C_n \\cong \\RR", "\\varphi(n)", "q"]
    assert "merror" not in page["math"][1]["mml"] and 'mathcolor="red"' not in page["math"][1]["mml"]
    assert link["card"] is None and link["packages"] == _PAGE_PACKAGES
    assert sorted(math["tex"] for math in link["math"]) == ["g^k", "h_1", "r^2", "t_0"]
    # The control's document has neither MathJax's menu nor its accessibility explorer.
    skip = {"[+]": ["mjx-container"]}
    assert page["requested"] == {"skipHtmlTags": skip}
    assert link["requested"] == {"enableMenu": False, "renderActions": {"explorable": []}, "skipHtmlTags": skip}
    assert all(document["math"] == [] for document in explorer[2:])
    # MathJax's own document, which a call to MathJax.typesetPromise would use, holds nothing.
    assert report["startup"] == 0
    # The pass added to the page, so the glyphs the first pass drew keep their rules.
    assert report["unstyled"] == []
