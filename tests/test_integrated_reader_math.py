"""The generated safe math service's readiness, source and disposal contract.

These small Node tests exercise its control flow, not a replacement MathJax
parser. The companion WebKit suite uses the actual pinned engine and config.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from autoform_cli.mathjax import mathjax_script


_HARNESS = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const stats = {renders: [], inputs: [], docs: [], errors: [], warnings: [], active: 0, maxActive: 0, resets: 0};
let document;
class Element {
  constructor(tag) {
    this.tagName = tag; this.nodeType = 1; this.ownerDocument = document;
    this.attributes = new Map(); this.childNodes = []; this.parentNode = null; this.style = {}; this.text = "";
    this.listeners = new Map();
  }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  get isConnected() { return this === document || !!(this.parentNode && this.parentNode.isConnected); }
  get textContent() { return this.text + this.childNodes.map(node => node.textContent).join(""); }
  set textContent(value) {
    this.childNodes.forEach(node => node.parentNode = null); this.childNodes = []; this.text = value;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  contains(node) { return node === this || this.childNodes.some(child => child.contains(node)); }
  matches(selector) {
    if (selector[0] === ".") return (this.getAttribute("class") || "").split(/\s+/).includes(selector.slice(1));
    if (selector[0] === "[") return this.attributes.has(selector.slice(1, -1));
    return this.tagName === selector;
  }
  closest(selector) { return this.matches(selector) ? this : this.parentElement && this.parentElement.closest(selector); }
  querySelectorAll(selector) {
    return this.childNodes.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  appendChild(node) { node.remove(); node.parentNode = this; this.childNodes.push(node); return node; }
  remove() {
    if (this.parentNode) this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }
  replaceWith(node) {
    const parent = this.parentNode, index = parent.childNodes.indexOf(this);
    node.remove(); parent.childNodes[index] = node; node.parentNode = parent; this.parentNode = null;
  }
  cloneNode(deep) {
    const clone = new Element(this.tagName); clone.attributes = new Map(this.attributes); clone.text = this.text;
    if (deep) this.childNodes.forEach(child => clone.appendChild(child.cloneNode(true)));
    return clone;
  }
  getBoundingClientRect() { return {width: 640}; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
}
document = new Element("document"); document.nodeType = 9; document.ownerDocument = document;
document.readyState = "loading"; document.createElement = tag => new Element(tag);
document.head = document.appendChild(new Element("head")); document.body = document.appendChild(new Element("body"));
const window = {document, crypto: require("node:crypto").webcrypto,
  getComputedStyle: () => ({fontFamily: "serif", fontSize: "16px", fontWeight: "400",
  fontStyle: "normal", lineHeight: "normal", letterSpacing: "normal", direction: "ltr"})};
PRELOAD_SOURCE
const sandbox = {window, document, console: {error: (...args) => stats.errors.push(args.map(String)),
  warn: (...args) => stats.warnings.push(args.map(String))}, Promise, WeakMap, Array, JSON, Error};
Object.defineProperty(sandbox, "MathJax", {get: () => window.MathJax, set: value => window.MathJax = value});
vm.runInNewContext(CONFIG_JSON, sandbox);
function reader(...sources) {
  const root = new Element("section"); root.setAttribute("class", "bp-reading-source");
  sources.forEach(source => {
    const span = root.appendChild(new Element("span")); span.setAttribute("class", "arithmatex"); span.textContent = source;
  });
  document.body.appendChild(root); return root;
}
function deferred() { let resolve; const promise = new Promise(done => resolve = done); return {promise, resolve}; }
const flush = () => new Promise(resolve => setImmediate(resolve));
function install(version = "3.2.2", options = {}) {
  const config = window.MathJax, pageReady = config.startup.pageReady;
  config.version = version;
  config.startup = {document: {document, menu: {constructor: {loadingPromises: new Map()}}},
    output: {reset() { stats.resets++; }}};
  class TeX {
    constructor(settings) {
      stats.inputs.push(JSON.parse(JSON.stringify(settings))); this.settings = settings;
      if (options.mutate) settings.macros.RR = "POISON";
    }
  }
  config.typesetPromise = () => { throw new Error("unrestricted startup input was used"); };
  config._ = {input: {tex_ts: {TeX}}, mathjax: {mathjax: {
    document(_root, settings) {
      const doc = {options: settings, cleared: false, removed: [],
        menu: {clear() { doc.menuCleared = true; }},
        clear() { doc.cleared = true; },
        clearMathItemsWithin(elements) { doc.removed.push(...elements); return []; },
        render() {
          if (options.fail && options.fail()) throw new Error("deliberate safe-pass failure");
          stats.renders.push(JSON.parse(JSON.stringify(doc.options.elements.map(element => element.textContent))));
          doc.options.elements.forEach(element => {
            const glyph = new Element("mjx-container"); glyph.textContent = "glyphs";
            element.textContent = ""; element.appendChild(glyph);
          });
        }};
      stats.docs.push(doc); return doc;
    },
    handleRetriesFor(callback) {
      stats.active++; stats.maxActive = Math.max(stats.maxActive, stats.active);
      const wait = options.wait ? options.wait() : Promise.resolve();
      return wait.then(callback).finally(() => stats.active--);
    }
  }}};
  return pageReady();
}
(async function () {
BODY_SOURCE
  assert.equal(document.querySelectorAll("[data-autoform-math-stage]").length, 0, "staging nodes leaked");
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
"""


def _run(body: str, *, preload: str = "") -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    script, warnings = mathjax_script(None, json.dumps({"RR": "\\mathbb{R}"}).encode())
    assert warnings == []
    harness = (
        _HARNESS.replace("CONFIG_JSON", json.dumps(script))
        .replace("PRELOAD_SOURCE", preload)
        .replace("BODY_SOURCE", body)
    )
    result = subprocess.run([node, "-"], input=harness, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_safe_refresh_reads_only_canonical_sources_and_keeps_startup_empty() -> None:
    _run(r"""
      const root = reader("\\(x+1\\)", "\\[\\sum_{i=1}^n i\\]");
      const literal = document.body.appendChild(new Element("p")); literal.textContent = "A $5 price and $10 balance";
      assert.deepEqual(Array.from(window.MathJax.tex.inlineMath), []);
      assert.deepEqual(Array.from(window.MathJax.tex.displayMath), []);
      assert.equal(window.MathJax.startup.typeset, false);
      assert.equal(typeof window.AutoformMath.refresh, "function");
      assert.equal(await install(), true);
      assert.deepEqual(stats.renders, [["\\(x+1\\)", "\\[\\sum_{i=1}^n i\\]"]]);
      assert.equal(literal.textContent, "A $5 price and $10 balance");
      assert.equal(stats.inputs[0].macros.label, "\\notallowed");
      assert.equal(stats.inputs[0].macros.DeclareMathOperator, "\\notallowed");
      assert.ok(!stats.inputs[0].packages.includes("newcommand"));
      assert.ok(!stats.inputs[0].packages.includes("html"));
      assert.equal(stats.docs[0].options.processHtmlClass, "(?!)");
      assert.deepEqual(JSON.parse(JSON.stringify(stats.docs[0].options.safeOptions)),
        {allow: {URLs: "none", classes: "none", cssIDs: "none", styles: "none"}});
      assert.equal(root.querySelectorAll("mjx-container").length, 2);
    """)


def test_navigation_requests_before_readiness_share_one_bounded_latest_pass() -> None:
    _run(r"""
      let root = reader("\\(old\\)"); const first = window.AutoformMath.refresh(root);
      for (let n = 0; n < 80; n++) {
        root.remove(); root = reader("\\(page_{" + n + "}\\)");
        assert.equal(window.AutoformMath.refresh(root), first);
      }
      assert.equal(stats.docs.length, 0);
      await install(); assert.equal(await first, true);
      assert.deepEqual(stats.renders, [["\\(page_{79}\\)"]]);
      assert.equal(stats.docs.length, 1);
    """)


def test_inflight_navigation_is_serialized_and_drops_stale_hidden_output() -> None:
    _run(r"""
      const gate = deferred(); let waiting = true;
      let root = reader("\\(first\\)"); const old = root.querySelector(".arithmatex");
      const first = install("3.2.2", {wait: () => waiting ? gate.promise : Promise.resolve()});
      await flush(); assert.equal(stats.active, 1);
      assert.equal(old.textContent, "\\(first\\)", "visible source changed before safe output was ready");
      for (let n = 0; n < 50; n++) {
        root.remove(); root = reader("\\(latest_{" + n + "}\\)"); window.AutoformMath.refresh(root);
      }
      waiting = false; gate.resolve(); assert.equal(await first, true);
      assert.equal(stats.maxActive, 1);
      assert.equal(stats.docs.length, 2);
      assert.equal(stats.docs[0].cleared, true); assert.equal(stats.docs[0].menuCleared, true);
      assert.deepEqual(stats.renders[1], ["\\(latest_{49}\\)"]);
      assert.equal(stats.docs[1].options.elements[0], root.querySelector(".arithmatex"));
    """)


def test_scoped_refresh_preserves_other_live_math_and_skips_owned_output() -> None:
    _run(r"""
      const left = reader("\\(left\\)"), right = reader("\\(right\\)"); await install();
      const leftMath = left.querySelector(".arithmatex"), leftGlyph = left.querySelector("mjx-container");
      assert.equal(await window.AutoformMath.refresh(left), true); assert.equal(stats.docs.length, 1);
      right.remove(); const next = reader("\\(next\\)");
      assert.equal(await window.AutoformMath.refresh(next), true);
      assert.equal(left.querySelector(".arithmatex"), leftMath); assert.equal(left.querySelector("mjx-container"), leftGlyph);
      assert.equal(stats.docs[0].cleared, false); assert.equal(stats.docs[0].removed.length, 1);
      assert.equal(stats.resets, 0, "a scoped pass cleared CSS used by other output");
    """)


def test_ssr_clone_recovers_original_tex_not_glyph_text_and_retires_old_records() -> None:
    _run(r"""
      const original = reader("\\(\\RR\\)", "\\[\\overline{AB}=2\\]"); await install();
      const clone = original.cloneNode(true); original.remove(); document.body.appendChild(clone);
      const copiedGlyph = clone.querySelector("mjx-container");
      assert.equal(clone.querySelector(".arithmatex").textContent, "glyphs");
      assert.equal(await window.AutoformMath.refresh(clone), true);
      assert.deepEqual(stats.renders[1], ["\\(\\RR\\)", "\\[\\overline{AB}=2\\]"]);
      assert.notEqual(clone.querySelector("mjx-container"), copiedGlyph);
      assert.equal(clone.querySelector(".arithmatex").getAttribute("data-autoform-math-source"), "\\(\\RR\\)");
      assert.equal(stats.docs[0].cleared, true); assert.equal(stats.docs[0].menuCleared, true);
      assert.equal(stats.docs[1].cleared, false);
    """)


def test_failed_safe_pass_keeps_visible_source_and_can_recover_on_next_refresh() -> None:
    _run(r"""
      const root = reader("\\(\\RR\\)"); let fail = true;
      assert.equal(await install("3.2.2", {fail: () => fail}), false);
      assert.equal(root.querySelector(".arithmatex").textContent, "\\(\\RR\\)");
      assert.equal(stats.docs[0].cleared, true); assert.equal(stats.docs[0].menuCleared, true);
      fail = false; assert.equal(await window.AutoformMath.refresh(root), true);
      assert.equal(root.querySelectorAll("mjx-container").length, 1);
      assert.equal(stats.errors.length, 1);
    """)


@pytest.mark.parametrize("fallback", ["version", "loader", "script"])
def test_pending_refresh_resolves_false_under_version_or_load_fallback(fallback: str) -> None:
    action = {
        "version": 'await install("9.0.0")',
        "loader": 'window.MathJax.loader.failed(new Error("bundle unavailable"))',
        "script": (
            'document.listeners.get("DOMContentLoaded")(); '
            'document.head.querySelector("script").listeners.get("error")()'
        ),
    }[fallback]
    _run(r"""
      const root = reader("\\(x\\)"), waiting = window.AutoformMath.refresh(root);
      ACTION;
      assert.equal(await waiting, false); assert.equal(await window.AutoformMath.refresh(root), false);
      assert.equal(root.querySelector(".arithmatex").textContent, "\\(x\\)");
      assert.equal(stats.docs.length, 0); assert.equal(stats.errors.length, 1);
    """.replace("ACTION", action))


def test_preloaded_engine_cannot_bypass_safe_configuration() -> None:
    _run(r"""
      const root = reader("\\(x\\)");
      assert.equal(await window.AutoformMath.refresh(root), false);
      assert.equal(window.MathJax.config.startup.typeset, false);
      assert.equal(stats.docs.length, 0); assert.equal(stats.errors.length, 1);
    """, preload='window.MathJax = {version: "3.2.2", config: {startup: {typeset: true}}};')


def test_formula_options_and_project_macros_are_fresh_for_every_navigation() -> None:
    _run(r"""
      const root = reader("\\(\\RR\\)"); await install("3.2.2", {mutate: true});
      root.remove(); const next = reader("\\(\\RR\\)"); await window.AutoformMath.refresh(next);
      assert.equal(stats.inputs.length, 2);
      assert.equal(stats.inputs[0].macros.RR, "\\mathbb{R}"); assert.equal(stats.inputs[1].macros.RR, "\\mathbb{R}");
      assert.notEqual(stats.docs[0].options.InputJax, stats.docs[1].options.InputJax);
    """)


def test_bubbling_refresh_event_uses_connected_scope_or_explicit_root() -> None:
    _run(r"""
      await install(); const first = reader("\\(first\\)");
      document.listeners.get("autoform:math-refresh")({target: first}); await flush();
      const second = reader("\\(second\\)");
      document.listeners.get("autoform:math-refresh")({target: document, detail: {root: second}}); await flush();
      assert.deepEqual(stats.renders, [["\\(first\\)"], ["\\(second\\)"]]);
    """)


def test_rendered_markup_without_retained_source_is_not_reinterpreted() -> None:
    _run(r"""
      const root = reader(""); const span = root.querySelector(".arithmatex");
      const glyph = span.appendChild(new Element("mjx-container")); glyph.textContent = "x";
      assert.equal(await install(), false); assert.equal(span.querySelector("mjx-container"), glyph);
      assert.equal(stats.docs.length, 0); assert.equal(stats.warnings.length, 1);
    """)


def test_fresh_marked_text_overrides_forged_retained_source_and_stamp() -> None:
    _run(r"""
      const root = reader("\\(actual\\)"), formula = root.querySelector(".arithmatex");
      formula.setAttribute("data-autoform-math-source", "\\(forged\\)");
      formula.setAttribute("data-autoform-math-stamp", "authored-nonce");
      assert.equal(await install(), true);
      assert.deepEqual(stats.renders, [["\\(actual\\)"]]);
      const rendered = root.querySelector(".arithmatex"), stamp = rendered.getAttribute("data-autoform-math-stamp");
      assert.match(stamp, /^[0-9a-f]{32}$/); assert.notEqual(stamp, "authored-nonce");
      assert.equal(rendered.getAttribute("data-autoform-math-source"), "\\(actual\\)");
      const fresh = reader("\\(still_actual\\)"), copiedStamp = fresh.querySelector(".arithmatex");
      copiedStamp.setAttribute("data-autoform-math-stamp", stamp);
      copiedStamp.setAttribute("data-autoform-math-source", "\\(also_forged\\)");
      assert.equal(await window.AutoformMath.refresh(fresh), true);
      assert.deepEqual(stats.renders[1], ["\\(still_actual\\)"]);
    """)


@pytest.mark.parametrize("stamp", [None, "authored-nonce", "a" * 32])
def test_forged_glyph_metadata_without_this_runtime_stamp_is_left_unchanged(stamp: str | None) -> None:
    setting = "" if stamp is None else f'formula.setAttribute("data-autoform-math-stamp", {json.dumps(stamp)});'
    _run(r"""
      const root = reader(""); const formula = root.querySelector(".arithmatex");
      const glyph = formula.appendChild(new Element("mjx-container")); glyph.textContent = "visible glyphs";
      formula.setAttribute("data-autoform-math-source", "\\(forged\\)");
      STAMP_SETTING
      assert.equal(await install(), false); assert.equal(formula.querySelector("mjx-container"), glyph);
      assert.equal(formula.textContent, "visible glyphs"); assert.equal(stats.docs.length, 0);
      assert.equal(stats.warnings.length, 1);
    """.replace("STAMP_SETTING", setting))


def test_no_crypto_can_render_fresh_sources_but_never_trusts_glyph_clones() -> None:
    _run(r"""
      const root = reader("\\(actual\\)"); await install();
      const clone = root.cloneNode(true); root.remove(); document.body.appendChild(clone);
      const glyph = clone.querySelector("mjx-container");
      assert.equal(await window.AutoformMath.refresh(clone), false);
      assert.equal(clone.querySelector("mjx-container"), glyph); assert.equal(stats.renders.length, 1);
    """, preload="delete window.crypto;")
