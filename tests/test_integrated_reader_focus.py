"""Execute real reader/history callbacks with native promises and isolated DOM peers."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.test_integrated_projections import _CLIENT, _LOADER, _NODE

pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated reader callback tests")

_ENVIRONMENT = r"""
class Element {
  constructor(tag, id = '') {
    this.tagName = tag; this.id = id; this.dataset = {}; this.children = []; this.scrollTop = 0;
    this.focuses = []; this.jumps = []; this.classes = new Set(); this.attrs = {};
    this.classList = {add: name => this.classes.add(name), contains: name => this.classes.has(name)};
  }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  replaceChildren(...children) {
    if (this.contains(document.activeElement) && document.activeElement !== this) document.activeElement = document.body;
    this.children = children;
  }
  prepend(child) { this.children.unshift(child); }
  querySelector(tag) {
    return this.children.find(child => child.tagName === tag) ||
      this.children.map(child => child.querySelector(tag)).find(Boolean) || null;
  }
  byId(id) { return this.id === id ? this : this.children.map(child => child.byId(id)).find(Boolean) || null; }
  getBoundingClientRect() {
    const top = this.id === 'main' ? 400 - dom$readingBody.scrollTop : 100;
    return {top, bottom: top + 40, left: 0, right: 640, width: 640, height: 40};
  }
  focus(settings) { this.focuses.push(settings); document.activeElement = this; }
  scrollIntoView(settings) { this.jumps.push(settings); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
}
var document = {body: new Element('body'), activeElement: null, title: '',
  createElement: tag => new Element(tag), getElementById: id => dom$readingBody.byId(id)};
document.activeElement = document.body;
var dom$search = new Element('input'), dom$readingBody = new Element('article');
var dom$navigation = new Element('aside'), dom$inventoryScroll = new Element('div'), dom$outlineToggle = new Element('button');
var dom$error = new Element('div'), dom$breadcrumb = new Element('nav');
var hostClasses = new Set(['bp-dag-sheet-open']);
var host = {classList: {contains: name => hostClasses.has(name),
  toggle(name, enabled) { if (enabled) hostClasses.add(name); else hostClasses.delete(name); }},
  closest() { return {querySelector() {return null;}}; }};
var ui$selected, ui$requestedNode = 'A', ui$mode = 'graph', ui$direction = 'both';
var ui$selectedStatuses = new Set(['fully_proved']), ui$showIsolated = true, ui$query = 'saved query', ui$area = null;
var ui$transform = {x: 20, y: 30, scale: 1}, ui$listLimit = 100, ui$relationIds = null, ui$outlineLimit = 120;
var ui$filtersTouched = false, ui$activeDirty = true, ui$reading = true;
var byId = new Map(['A','B'].map(id => [id, {id, status: 'fully_proved'}]));
var overlays = new Map(['A','B'].map(id => {
  const node = new Element('button'); node.dataset.autoformNodeId = id; return [id, node];
}));
ui$selected = byId.get('A');
var alive = true, restoration = 0, restoring = false, checkpointTimer = 0, currentVersion = 1;
var readingReady = Promise.resolve(true), mathPass = Promise.resolve(true), mathRoots = [], frames = [];
var contextGraph, currentBundle, trail, firstResize = false, mapAllowed = true;
var palette = {fully_proved: {}, planned: {}}, presentStatuses = ['fully_proved', 'planned'];
var navigation = {loadIndex() { return Promise.resolve(null); }};
var window = {AutoformMath: {refresh(root) {mathRoots.push(root); return mathPass;}}};
function requestAnimationFrame(callback) {frames.push(callback); return frames.length;}
function updateProjection() {ui$selected = byId.get(ui$requestedNode) || null;}
function setSheet(open) {host.classList.toggle('bp-dag-sheet-open', open);}
function sheetLayout() {return false;}
function closeSearch() {}
function routeTrail() {return [];}
function trailList() {return new Element('ol');}
function renderNavigation() {}
function refresh() {}
function resize() {}
function center() {}
function centerIfHidden() {}
function showError(error) {throw error;}
function element(tag) {return new Element(tag);}
function deferred() {let resolve; const promise = new Promise(done => resolve = done); return {promise, resolve};}
async function queued() {
  for (let n = 0; n < 30 && !frames.length; n++) await Promise.resolve();
  assert(frames.length, 'The real callback must reach its frame');
}
async function frame() {
  await queued(); const pending = frames.splice(0); pending.forEach(callback => callback());
  for (let n = 0; n < 5; n++) await Promise.resolve();
}
function bundle(id, snapshot, focus = true) {
  const heading = new Element('h1'), target = new Element('article', 'main'), source = new Element('div');
  target.dataset.autoformNodeId = id; source.replaceChildren(heading, target);
  const version = currentVersion;
  return {nodeId: id, anchor: 'main', title: 'Complete ' + id, readingSource: source,
    graph: {title: 'Scope ' + id}, snapshot, focus, isCurrent: () => currentVersion === version, heading, target};
}
"""


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    script += "\nObject.assign(sandbox, {clearTimeout});\n"
    script += "const callbacks=['captureState','applyRoute','restoreState'].map(original).join('\\n');\n"
    program = '"use strict";\n' + _ENVIRONMENT
    script += "const pending=vm.runInContext(" + json.dumps(program) + "+constants[0]+callbacks+"
    script += json.dumps("\n(async()=>{\n" + body + "\n})()") + ",sandbox,{timeout:5000,filename:CLIENT});\n"
    script += "Promise.resolve(pending).catch(error=>{console.error(error);process.exitCode=1;});"
    result = subprocess.run([_NODE, "-e", script], input="null", text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("owned_tag", ["a", "h1"])
def test_snapshot_marks_its_owned_anchor_without_jumping_and_restores_reader_intent(owned_tag: str) -> None:
    _run("const owned=new Element(" + json.dumps(owned_tag) + ");\n" + r"""
      dom$readingBody.replaceChildren(owned); owned.focus({});
      dom$readingBody.scrollTop = 211; dom$navigation.scrollTop = 44; dom$inventoryScroll.scrollTop = 9;
      const saved = captureState(); assert.equal(saved.focus, 'reading');
      const math = deferred(); mathPass = math.promise;
      const restoredPage = bundle('A', saved), ready = applyRoute(restoredPage);
      math.resolve(true); await frame(); await ready;
      assert(restoredPage.target.classList.contains('bp-reading-current'));
      assert.equal(restoredPage.target.jumps.length, 0, 'A history restoration must not jump to the anchor');
      assert.equal(restoredPage.heading.focuses.length, 0, 'The old H1 must not override saved reading intent');
      const restoration = restoreState(saved, restoredPage); await frame(); await restoration;
      assert.equal(document.activeElement, dom$readingBody);
      assert.equal(dom$readingBody.focuses.at(-1).preventScroll, true);
      assert.equal(dom$readingBody.scrollTop, 211);
      assert.equal(dom$navigation.scrollTop, 44); assert.equal(dom$inventoryScroll.scrollTop, 9);
      assert.equal(ui$selected.id, 'A');
      assert(mathRoots.every(root => root === dom$readingBody));
    """)


@pytest.mark.parametrize("intent", ["search", "A"])
def test_reader_restoration_does_not_replace_saved_search_or_graph_focus(intent: str) -> None:
    _run("const intent=" + json.dumps(intent) + ";\n" + r"""
      const chosen = intent === 'search' ? dom$search : overlays.get(intent);
      chosen.focus({}); const saved = captureState(); assert.equal(saved.focus, intent);
      const restoredPage = bundle('A', saved), ready = applyRoute(restoredPage);
      await frame(); await ready;
      const restoration = restoreState(saved, restoredPage); await frame(); await restoration;
      assert.equal(document.activeElement, chosen);
      assert.equal(chosen.focuses.at(-1).preventScroll, true);
      assert.equal(dom$readingBody.focuses.length, 0);
      assert.equal(restoredPage.heading.focuses.length, 0);
    """)


def test_stale_delayed_math_callback_cannot_mark_jump_or_focus_the_new_reader() -> None:
    _run(r"""
      const oldMath = deferred(), newMath = deferred(); mathPass = oldMath.promise;
      const older = bundle('A', null, true), oldReady = applyRoute(older);
      currentVersion = 2; mathPass = newMath.promise;
      const newer = bundle('B', null, false), newReady = applyRoute(newer);
      dom$search.focus({}); oldMath.resolve(true); await frame(); await oldReady;
      assert.equal(document.activeElement, dom$search);
      assert.equal(newer.target.classList.contains('bp-reading-current'), false);
      assert.equal(newer.target.jumps.length, 0); assert.equal(newer.heading.focuses.length, 0);
      newMath.resolve(true); await frame(); await newReady;
      assert(newer.target.classList.contains('bp-reading-current'));
      assert.equal(document.activeElement, dom$search);
      assert.equal(newer.heading.focuses.length, 0);
    """)


def test_stale_restore_ticket_cannot_reapply_old_reading_focus_or_scroll() -> None:
    _run(r"""
      const owned = new Element('a'); dom$readingBody.replaceChildren(owned); owned.focus({});
      dom$readingBody.scrollTop = 211; const saved = captureState();
      const older = bundle('A', saved), oldReady = applyRoute(older);
      await frame(); await oldReady;
      const oldRestore = restoreState(saved, older); await queued();
      currentVersion = 2; const math = deferred(); mathPass = math.promise;
      const newer = bundle('B', null, false), newReady = applyRoute(newer);
      dom$readingBody.scrollTop = 65; dom$search.focus({});
      await frame(); await oldRestore;
      assert.equal(document.activeElement, dom$search);
      assert.equal(dom$readingBody.scrollTop, 65);
      assert.equal(dom$readingBody.focuses.length, 0);
      math.resolve(true); await frame(); await newReady;
      assert.equal(document.activeElement, dom$search);
      assert.equal(newer.heading.focuses.length, 0);
    """)


def test_a_selected_statement_opens_its_own_relations() -> None:
    """The reader's separate relation strip is gone; the current statement's disclosure opens instead."""
    # The stub's querySelector matches tag names, so the disclosure is named by its selector.
    _run(r"""
      const page = bundle('A', null), relations = new Element('.bp-dependencies');
      page.target.replaceChildren(relations); const ready = applyRoute(page);
      await frame(); await ready;
      assert(page.target.classList.contains('bp-reading-current'));
      assert.equal(relations.open, true);
    """)
