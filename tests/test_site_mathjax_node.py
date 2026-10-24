"""Read-back cards typeset in Node.js by MathJax 3.2.2, with the script render wrote.

These tests need an unpacked MathJax 3.2.2 package root, the directory holding
``package.json`` and ``es5/``, named by ``AUTOFORM_MATHJAX_DIR``::

    AUTOFORM_MATHJAX_DIR=/path/to/MathJax-3.2.2 pytest tests/test_site_mathjax_node.py

Run ``npm ci --ignore-scripts`` first: the pinned development-only jsdom
26.1.0 bridge supplies a genuine connected browser DOM to MathJax's official
HTML adaptor. The mathematical engine is still the checked MathJax archive,
not a stub or another version. Node verifies TeX isolation/output semantics;
actual menu placement and browser geometry remain WebKit's responsibility.

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
_ROOT = Path(__file__).resolve().parents[1]


def mathjax_package(*, node: bool = True) -> Path:
    """The MathJax package root ``AUTOFORM_MATHJAX_DIR`` names, after checking
    that the package is MathJax 3.2.2 and, for Node tests, its DOM bridge exists."""

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
    bridge = _ROOT / "node_modules/jsdom/package.json"
    if node and (not bridge.is_file() or json.loads(bridge.read_text(encoding="utf-8")).get("version") != "26.1.0"):
        pytest.fail("The native DOM test bridge requires locked jsdom 26.1.0; run npm ci --ignore-scripts")
    return root


# Runs javascripts/mathjax.js in node against a local MathJax, as a page would,
# and reports what each document it made typeset. argv: the script, a page,
# the next page as Material's instant navigation shows it, "new" or "old":
# whether mkdocs.yml also lists the bundle, after the script, and "after" when
# a project script listed after it assigns the configuration Material's
# documentation gives. This bridge verifies real TeX/output semantics in a
# native connected DOM, not the browser menu's geometry or presentation.
_HARNESS = r"""
"use strict";
const fs = require("fs"), path = require("path"), assert = require("assert");
const {webcrypto} = require("crypto");
const {JSDOM} = require(path.join(process.env.AUTOFORM_DOM_BRIDGE_DIR, "jsdom"));
const [scriptPath, firstPath, secondPath, project, override, then] = process.argv.slice(2);
const report = {injected: [], errors: [], passes: [], version: null, base: null, load: null, early: null};
const dom = new JSDOM(fs.readFileSync(firstPath, "utf8"), {
  url: "https://blueprint.example.invalid/", runScripts: "outside-only", pretendToBeVisual: true
});
const nativeWindow = dom.window;
global.window = nativeWindow;
global.document = nativeWindow.document;
// The real menu component uses the browser's storage global. Bind the genuine
// origin-scoped JSDOM object; missing capabilities must not be hidden in logs.
global.localStorage = nativeWindow.localStorage;
assert.strictEqual(global.localStorage, nativeWindow.localStorage);
// Pinned MenuStore tests HTMLElement; ContextMenu tests Event/MouseEvent.
// Use only these required native interfaces, not a blanket window export or
// substitute predicates that would hide an unsupported DOM bridge.
for (const name of ["HTMLElement", "Event", "MouseEvent"]) {
  assert.strictEqual(typeof nativeWindow[name], "function", "Missing native DOM interface: " + name);
  global[name] = nativeWindow[name];
  assert.strictEqual(global[name], nativeWindow[name]);
}
// Real CSS inheritance, not synthetic layout getters. Node does not prove
// browser geometry; it still gives the engine finite inherited font metrics.
document.documentElement.style.fontSize = "16px";
document.body.style.fontSize = "16px";
document.body.style.fontFamily = '"Times New Roman", serif';
Object.defineProperty(nativeWindow, "crypto", {value: webcrypto, configurable: true});
// Components use Node's global MathJax; the generated browser configuration
// uses window.MathJax. This bridge forwards to the same protected property,
// never replaces its guard or supplies a fake mathematical runtime.
Object.defineProperty(globalThis, "MathJax", {
  configurable: true, get: () => nativeWindow.MathJax,
  set: value => { nativeWindow.MathJax = value; }
});
console.error = (...args) => report.errors.push(args.map(String).join(" "));
nativeWindow.console.error = console.error;
let shown = null;
global.document$ = nativeWindow.document$ = {subscribe: listener => { shown = listener; }};
const append = document.head.appendChild.bind(document.head);
document.head.appendChild = element => {
  if (element.tagName === "SCRIPT") report.injected.push(element.src);
  return append(element);
};
let docs = [], initialDocument = null;
function cardOf(doc) {
  const element = doc.options.elements && doc.options.elements[0];
  const card = element && element.closest(".bp-readback");
  return card ? card.id : null;
}
function describe() {
  return docs.map(doc => ({
    card: cardOf(doc),
    packages: doc.inputJax[0].parseOptions.options.packages.slice(),
    enableMenu: doc.options.enableMenu,
    math: Array.from(doc.math).map(item => ({tex: item.math, mml: MathJax.startup.toMML(item.root)}))
  }));
}

function glyphStyles() {
  // Adaptive CHTML adds insertRule() entries without changing textContent.
  // Require a connected stylesheet's actual glyph/font ::before content.
  const elements=Array.from(document.querySelectorAll('style,link[rel="stylesheet"]'));
  const sheets=Array.from(document.styleSheets), sources=[], rules=[];
  assert(elements.length<=32 && sheets.length<=32,'Stylesheet scan bound exceeded');
  let count=0;
  function scan(list,source,depth=0) {
    assert(depth<=8,'Stylesheet nesting bound exceeded');
    for(const rule of Array.from(list)) {
      assert(++count<=10000,'CSS rule scan bound exceeded');
      const content=rule.style&&rule.style.getPropertyValue('content').trim();
      if(rule.selectorText && content && !['none','normal','""',"''"].includes(content)) {
        for(const selector of rule.selectorText.split(',').map(value=>value.trim())) {
          if(selector.endsWith('::before')) rules.push({source,selector,elementSelector:selector.slice(0,-8),content});
        }
      }
      if(rule.cssRules) scan(rule.cssRules,source,depth+1);
    }
  }
  sheets.forEach((sheet,index)=>{
    const element=elements.find(node=>node.sheet===sheet);
    assert(element&&element.isConnected,'Stylesheet has no connected DOM source');
    sources.push({index,id:element.id,href:sheet.href||null,connected:element.isConnected,
      disabled:!!sheet.disabled,cssRules:sheet.cssRules.length});
    if(!sheet.disabled) scan(sheet.cssRules,index);
  });
  const nodes=Array.from(document.querySelectorAll('mjx-c')), variants=new Map();
  assert(nodes.length<=2000,'Glyph scan bound exceeded');
  nodes.forEach(node=>{
    const classes=Array.from(node.classList), glyph=classes.find(name=>/^mjx-c[0-9A-F]+$/.test(name));
    const font=classes.find(name=>name.startsWith('TEX-'));
    if(!glyph) {
      // CHTML deliberately inserts classless helper parts for stretched
      // delimiters. They are not direct hex/font glyphs covered by this oracle.
      assert(node.isConnected&&node.parentElement&&['MJX-BEG','MJX-EXT','MJX-MID','MJX-END'].includes(node.parentElement.tagName),
        'Unrecognized classless CHTML glyph helper');
      return;
    }
    // The default CHTML font deliberately has no TEX-* class. Preserve that
    // actual identity; only a real explicit variant requires a font selector.
    assert(node.isConnected&&glyph,'Live glyph lacks its actual glyph identity');
    const key=classes.join(' ');if(variants.has(key))return;
    const covering=rules.filter(rule=>rule.selector.includes('.'+glyph)&&(!font||rule.selector.includes('.'+font))&&node.matches(rule.elementSelector));
    variants.set(key,{glyph,font,classes,rules:covering});
  });
  return {sources,variants:Array.from(variants.values()),
    missing:Array.from(variants.values()).filter(glyph=>!glyph.rules.length).map(glyph=>glyph.glyph)};
}

(async () => {
  // Real DOM lifecycle: a new project runs the configuration after parsing;
  // an older project also lists the bundle and initializes before DOM ready.
  if (project === "new" && document.readyState !== "complete") {
    await new Promise(resolve => nativeWindow.addEventListener("load", resolve, {once: true}));
  }
  (0, eval)(fs.readFileSync(scriptPath, "utf8"));
  const early = then === "append" ? nativeWindow.AutoformMath.refresh(document) : null;
  const configuration = nativeWindow.MathJax;
  report.base = configuration.loader.paths.mathjax;
  report.load = configuration.loader.load.slice();
  // Node's core/safe handler has no MenuMathDocumentMixin. Load the real
  // pinned component, which registers MenuHandler at startup just as the
  // browser bundle does; never supply synthetic menu options or handlers.
  configuration.loader.load.push("input/tex", "input/mml", "output/chtml", "a11y/assistive-mml", "ui/menu");
  configuration.startup.document = document;
  configuration.startup.ready = () => {
    // node-main preloads LiteDOM even with a native document present. Select
    // its official browser HTML adaptor before defaultReady creates the real
    // startup document; retain MathJax's TeX/output/safe handler constructors.
    MathJax.startup.useAdaptor("browserAdaptor", true);
    assert.strictEqual(typeof MathJax._.ui.menu.MenuHandler.MenuHandler, "function",
      "The real pinned MenuHandler component must be loaded");
    const original = MathJax._.mathjax.mathjax.document;
    MathJax._.mathjax.mathjax.document = function (root, options) {
      const doc = original.call(this, root, options);
      assert.strictEqual(typeof doc.addMenu, "function", "The native handler needs the real menu mixin");
      assert(doc.menu instanceof MathJax._.ui.menu.Menu.Menu, "A genuine document menu must own its handlers");
      assert.strictEqual(typeof doc.options.enableMenu, "boolean", "Menu options must come from the real mixin");
      if (initialDocument === null) initialDocument = doc;
      else docs.push(doc);
      return doc;
    };
    MathJax.startup.defaultReady();
  };
  if (override === "after") nativeWindow.MathJax = {
    tex: {inlineMath: [["\\(", "\\)"]], displayMath: [["\\[", "\\]"]], processEscapes: true, processEnvironments: true},
    options: {ignoreHtmlClass: ".*|", processHtmlClass: "arithmatex"}
  };
  const main = require(path.join(process.env.AUTOFORM_MATHJAX_DIR, "es5", "node-main.js"));
  await main.init({});
  await MathJax.startup.promise;
  if (early) report.early = await early;
  report.version = MathJax.version;
  assert.strictEqual(MathJax.startup.adaptor.document, document, "MathJax must use the genuine native DOM");
  assert.strictEqual(MathJax.config, configuration, "The generated configuration must survive actual engine initialization");
  assert(shown, "The generated document subscription must initialize");
  // Material's subscription is fire-and-forget; await the actual scoped
  // readiness API, which coalesces with the refresh that event requests.
  shown();
  assert.strictEqual(await nativeWindow.AutoformMath.refresh(document), true);
  report.passes.push(describe());
  report.initialTabstops = document.querySelectorAll('mjx-container[tabindex="0"]').length;
  assert.strictEqual(document.querySelectorAll("[data-autoform-math-stage]").length, 0);

  docs = [];
  const next = new nativeWindow.DOMParser().parseFromString(fs.readFileSync(secondPath, "utf8"), "text/html");
  if (then === "promote") {
    const button = document.createElement("button");
    button.appendChild(document.querySelector(".arithmatex"));
    document.body.appendChild(button);
    assert.strictEqual(await nativeWindow.AutoformMath.refresh(button), true);
  } else {
    if (then === "append") document.body.append(...Array.from(next.body.childNodes));
    else { document.body.replaceChildren(...Array.from(next.body.childNodes)); shown(); }
    assert.strictEqual(await nativeWindow.AutoformMath.refresh(document), true);
  }
  report.passes.push(describe());
  assert.strictEqual(document.querySelectorAll("[data-autoform-math-stage]").length, 0);
  report.startup = Array.from(MathJax.startup.document.math).length;
  report.glyphStyles = glyphStyles();
  report.unstyled = report.glyphStyles.missing;
  report.controls = Array.from(document.querySelectorAll("a, button")).filter(control => control.querySelector("mjx-container")).map(control => ({
    tag: control.tagName, tabstops: control.querySelectorAll("[tabindex]").length,
    applications: control.querySelectorAll('[role="application"]').length
  }));
})().catch(error => {
  report.errors.push("harness: " + error.stack);
}).finally(() => {
  dom.window.close();
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
        env=dict(os.environ, AUTOFORM_MATHJAX_DIR=str(mathjax_package()), AUTOFORM_DOM_BRIDGE_DIR=str(_ROOT / "node_modules")),
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


def test_scoped_marked_controls_keep_their_keys_and_earlier_glyph_styles(tmp_path: Path) -> None:
    """The current scoped API, not a retired viewer event, typesets native controls with real MathJax."""
    drawn = (
        '<html><head></head><body><section><h1 class="arithmatex">Groups $\\mathbb{Z}/n$</h1>'
        '<p class="arithmatex">Cyclic groups $C_n \\cong \\RR$ have \\(\\varphi(n)\\) generators.</p>'
        '<p>Unmarked $x$ stays text.</p>'
        '<a class="arithmatex" href="#up">Up to $t_0$</a>'
        '<a href="#base"><span class="arithmatex">Order of $g^k$</span></a>'
        '<button class="arithmatex">Handle $h_1$</button>'
        '<button><span class="arithmatex">Relation $r^2$</span></button>'
        '</section></body></html>'
    )
    report = _node_report(tmp_path, _node_script(tmp_path, json.dumps(_MACROS)), "new", second=drawn, then="append")

    assert report["errors"] == [] and report["early"] is True
    assert [document["card"] for document in report["passes"][0]] == [None, "c1", "c2"]
    page, controls = report["passes"][1]
    assert page["packages"] == controls["packages"] == _PAGE_PACKAGES
    assert [math["tex"] for math in page["math"]] == ["\\mathbb{Z}/n", "C_n \\cong \\RR", "\\varphi(n)"]
    assert sorted(math["tex"] for math in controls["math"]) == ["g^k", "h_1", "r^2", "t_0"]
    assert controls["enableMenu"] is False
    assert report["controls"] == [
        {"tag": "A", "tabstops": 0, "applications": 0},
        {"tag": "A", "tabstops": 0, "applications": 0},
        {"tag": "BUTTON", "tabstops": 0, "applications": 0},
        {"tag": "BUTTON", "tabstops": 0, "applications": 0},
    ]
    assert report["startup"] == 0 and report["unstyled"] == []
    assert "merror" not in page["math"][1]["mml"] and 'mathcolor="red"' not in page["math"][1]["mml"]


def test_an_existing_rendered_formula_moved_into_a_control_loses_its_inner_tabstop(tmp_path: Path) -> None:
    """Trusted retained source permits a real ownership transition, without cloning rendered handlers."""
    report = _node_report(tmp_path, _node_script(tmp_path, json.dumps(_MACROS)), "new", then="promote")
    assert report["errors"] == []
    page, controlled = report["passes"][1]
    assert page["math"] == [] and page["packages"] == _PAGE_PACKAGES
    assert page["enableMenu"] is True and report["initialTabstops"] > 0
    assert controlled["enableMenu"] is False
    assert [math["tex"] for math in controlled["math"]] == ["a \\leq b"]
    assert report["controls"] == [{"tag": "BUTTON", "tabstops": 0, "applications": 0}]
    assert report["startup"] == 0 and report["unstyled"] == []


def test_a_control_inside_a_readback_card_keeps_the_cards_restricted_input(tmp_path: Path) -> None:
    drawn = (
        '<html><head></head><body><div class="bp-readback bp-readback-current" id="c3">'
        '<a href="#card"><span class="bp-card-formula">\\(\\boldsymbol{x} \\RR\\)</span></a>'
        '</div></body></html>'
    )
    report = _node_report(tmp_path, _node_script(tmp_path, json.dumps(_MACROS)), "new", second=drawn, then="append")
    assert report["errors"] == []
    page, card = report["passes"][1]
    assert page["math"] == []
    assert card["card"] == "c3" and card["packages"] == _CARD_PACKAGES
    assert card["enableMenu"] is False
    for command in ("\\boldsymbol", "\\RR"):
        assert f'<mtext mathcolor="red">{command}</mtext>' in card["math"][0]["mml"]
    assert report["controls"] == [{"tag": "A", "tabstops": 0, "applications": 0}]
    assert report["startup"] == 0 and report["unstyled"] == []
