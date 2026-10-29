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
    this.children = children; children.forEach(child => { child.parent = this; });
  }
  prepend(child) { this.children.unshift(child); child.parent = this; }
  closest(selector) {
    for (let node = this; node; node = node.parent) {
      if (selector.startsWith('.') ? node.classes.has(selector.slice(1)) : node.tagName === selector) return node;
    }
    return null;
  }
  querySelector(selector) {
    const parts = selector.split(' ');
    if (parts.length === 2) {
      const parent = this.querySelector(parts[0]); return parent && parent.querySelector(parts[1]);
    }
    const matches = node => selector.startsWith('.') ? node.classes.has(selector.slice(1)) : node.tagName === selector;
    for (const child of this.children) {
      if (matches(child)) return child;
      const nested = child.querySelector(selector); if (nested) return nested;
    }
    return null;
  }
  byId(id) { return this.id === id ? this : this.children.map(child => child.byId(id)).find(Boolean) || null; }
  getBoundingClientRect() {
    const top = this.id === 'main' || this.dataset.autoformNodeId ? 400 - dom$readingBody.scrollTop : 100;
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
var dom$shelves = new Element('section'); dom$shelves.hidden = true;
var dom$nodeLayer = null;
var dom$inventoryRows = new Map();
var dom$error = new Element('div'), dom$breadcrumb = new Element('nav');
var dom$filterPopover = new Element('div'), dom$filterSummary = new Element('button');
dom$filterPopover.hidden = true;
var dom$browsePopover = new Element('div'), dom$browseToggle = new Element('button');
dom$browsePopover.hidden = true;
var hostClasses = new Set(['bp-dag-sheet-open']);
var host = {classList: {contains: name => hostClasses.has(name),
  toggle(name, enabled) { if (enabled) hostClasses.add(name); else hostClasses.delete(name); }},
  closest() { return {querySelector() {return null;}}; }};
var ui$selected, ui$requestedNode = 'A', ui$mode = 'graph', ui$direction = 'both';
var ui$selectedStatuses = new Set(['fully_proved']), ui$showIsolated = true, ui$query = 'saved query', ui$area = null, ui$overviewGroup = null, ui$scopeOverview = false;
var ui$transform = {x: 20, y: 30, scale: 1}, ui$listLimit = 100, ui$relationIds = null, ui$outlineLimit = 120;
var ui$filtersTouched = false, ui$activeDirty = true, ui$reading = true, ui$overviewOnly = false;
var byId = new Map(['A','B'].map(id => [id, {id, title: id, kind: 'node', result: true,
  scope: 'chapter', parent: 'chapter', status: 'fully_proved'}]));
var records = new Map(byId);
var overlays = new Map(['A','B'].map(id => {
  const node = new Element('button'); node.dataset.autoformNodeId = id; return [id, node];
}));
ui$selected = byId.get('A');
var alive = true, restoration = 0, restoring = false, checkpointTimer = 0, currentVersion = 1;
var readingReady = Promise.resolve(true), mathPass = Promise.resolve(true), mathRoots = [], frames = [];
var data, contextGraph, currentBundle, trail, firstResize = false, mapAllowed = true;
var palette = {fully_proved: {}, planned: {}}, presentStatuses = ['fully_proved', 'planned'];
var navigation = {loadIndex() { return Promise.resolve(null); }};
var window = {AutoformMath: {refresh(root) {mathRoots.push(root); return mathPass;}}};
function requestAnimationFrame(callback) {frames.push(callback); return frames.length;}
function updateProjection() {data = contextGraph; ui$selected = byId.get(ui$requestedNode) || null;}
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
  // The default thin fragment exercises the statement-without-heading fallback.
  // Heading tests insert real elements carrying the published .bp-thmheading class.
  const heading = new Element('h1'), target = new Element('article', 'main'), source = new Element('div');
  target.dataset.autoformNodeId = id; source.replaceChildren(heading, target);
  const version = currentVersion;
  // A normal two-result scope is not an authored-area navigation overview.
  // This seam supplies graph context only; callbacks remain verbatim production source.
  return {nodeId: id, scope: 'chapter', anchor: 'main', title: 'Complete ' + id, readingSource: source,
    graph: {schema: 'autoform-dag-view/v2', title: 'Scope ' + id, nodes: Array.from(byId.values()), edges: [],
      palette, view: {kind: 'chapter', presentation: 'dag'}},
    snapshot, focus, isCurrent: () => currentVersion === version, heading, pageHeading: heading, target};
}
function headingBundle(id, snapshot, focus = true) {
  const page = bundle(id, snapshot, focus);
  // Match the publisher: only the h4 has the anchor; the containing div owns the node ID.
  page.target.tagName = 'div'; page.target.id = ''; page.target.classList.add('bp-thmwrapper');
  const statementHeading = new Element('h4', page.anchor), statementBody = new Element('div');
  const relations = new Element('details');
  statementHeading.classList.add('bp-thmheading');
  statementHeading.textContent = 'Statement ' + id;
  statementHeading.getBoundingClientRect = function () {
    const top = 460 - dom$readingBody.scrollTop;
    return {top, bottom: top + 40, left: 0, right: 640, width: 640, height: 40};
  };
  statementBody.textContent = 'Original statement and proof for ' + id + ': \\(x + 1\\)';
  relations.classList.add('bp-dependencies'); relations.open = false;
  page.target.replaceChildren(statementHeading, statementBody, relations);
  return Object.assign(page, {statementHeading, statementBody, relations});
}
"""


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    script += "\nObject.assign(sandbox, {clearTimeout});\n"
    script += "const callbacks=['captureState','snapshotForRoute','applyRoute','restoreState','setFilters','setBrowse'].map(original).join('\\n');\n"
    script += "let testBodyComplete=false;\n"
    script += "process.once('beforeExit',()=>{if(!testBodyComplete){"
    script += "console.error('Reader callback test body did not complete');process.exitCode=1;}});\n"
    program = '"use strict";\n' + _ENVIRONMENT
    script += "const pending=vm.runInContext(" + json.dumps(program) + "+constants[0]+callbacks+"
    script += json.dumps("\n(async()=>{\n" + body + "\n})()") + ",sandbox,{timeout:5000,filename:CLIENT});\n"
    script += "Promise.resolve(pending).then(()=>{testBodyComplete=true;},error=>{"
    script += "testBodyComplete=true;console.error(error);process.exitCode=1;});"
    result = subprocess.run([_NODE, "-e", script], input="null", text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_reader_runner_rejects_an_unfinished_async_test_body() -> None:
    with pytest.raises(AssertionError, match="Reader callback test body did not complete"):
        _run("await new Promise(() => {}); assert.fail('This assertion must never run');")


def test_heading_anchor_marks_and_opens_the_whole_card_then_focuses_after_math() -> None:
    _run(r"""
      const math=deferred();mathPass=math.promise;dom$search.focus({});
      const incoming=headingBundle('A',null,'reading'),source=incoming.readingSource;
      const sourceChildren=source.children.slice(),cardChildren=incoming.target.children.slice();
      const bodyText=incoming.statementBody.textContent,graph=incoming.graph;
      const graphBefore=JSON.stringify(graph),indexBefore=JSON.stringify(Array.from(records.values()));
      assert.equal(document.getElementById(incoming.anchor),null,'The fragment is not mounted before navigation');
      const ready=applyRoute(incoming);
      assert.equal(document.getElementById(incoming.anchor),incoming.statementHeading);
      assert.equal(incoming.statementHeading.closest('.bp-thmwrapper'),incoming.target);
      assert.equal(document.activeElement,dom$search);assert.equal(frames.length,0);
      assert.equal(incoming.target.attrs['aria-current'],undefined);assert.equal(incoming.relations.open,false);
      math.resolve(true);await queued();
      assert.equal(document.activeElement,dom$search,'Math completion still awaits the real navigation frame');
      await frame();await ready;
      assert(incoming.target.classList.contains('bp-reading-current'));
      assert.equal(incoming.target.attrs['aria-current'],'true');assert.equal(incoming.relations.open,true);
      assert.equal(incoming.statementHeading.classList.contains('bp-reading-current'),false);
      assert.equal(incoming.statementHeading.attrs['aria-current'],undefined);
      assert.equal(document.activeElement,incoming.statementHeading);assert.equal(incoming.statementHeading.tabIndex,-1);
      assert.deepEqual(incoming.statementHeading.focuses,[{preventScroll:true}]);
      assert.equal(incoming.target.focuses.length,0);assert.equal(incoming.pageHeading.focuses.length,0);
      assert.equal(dom$readingBody.scrollTop,276,'Anchor scrolling must use the card, not the lower h4 rectangle');
      assert.equal(incoming.statementHeading.jumps.length,0);assert.equal(incoming.target.jumps.length,0);
      assert.equal(dom$readingBody.children[0],source);assert.equal(currentBundle.readingSource,source);
      assert.equal(currentBundle.graph,graph);assert.deepEqual(source.children,sourceChildren);
      assert.deepEqual(incoming.target.children,cardChildren);assert.equal(incoming.statementBody.textContent,bodyText);
      assert.equal(source.querySelector('h1'),incoming.pageHeading);
      assert.equal(JSON.stringify(graph),graphBefore);assert.equal(JSON.stringify(Array.from(records.values())),indexBefore);
      assert.deepEqual(mathRoots,[dom$readingBody]);
    """)


def test_heading_anchor_snapshot_opens_the_card_without_overriding_saved_reader_focus_or_scroll() -> None:
    _run(r"""
      records.get('A').route='/book/a.html#main';
      dom$readingBody.focus({});dom$readingBody.scrollTop=211;
      dom$navigation.scrollTop=44;dom$inventoryScroll.scrollTop=9;ui$transform={x:145,y:-78,scale:1.1};
      const saved=captureState(),savedBefore=JSON.stringify(saved),math=deferred();mathPass=math.promise;
      const incoming=headingBundle('A',saved,'reading');incoming.url='https://example.test/book/a.html#main';
      assert.equal(snapshotForRoute(saved,incoming),saved);
      const ready=applyRoute(incoming);math.resolve(true);await frame();await ready;
      assert.equal(incoming.target.attrs['aria-current'],'true');assert.equal(incoming.relations.open,true);
      assert.equal(incoming.statementHeading.attrs['aria-current'],undefined);
      assert.equal(incoming.statementHeading.focuses.length,0);assert.equal(incoming.pageHeading.focuses.length,0);
      assert.equal(dom$readingBody.scrollTop,0,'The saved route suppresses a fresh anchor jump');
      const restored=restoreState(saved,incoming);await frame();await restored;
      assert.equal(document.activeElement,dom$readingBody);assert.equal(dom$readingBody.focuses.at(-1).preventScroll,true);
      assert.equal(dom$readingBody.scrollTop,211);assert.equal(dom$navigation.scrollTop,44);
      assert.equal(dom$inventoryScroll.scrollTop,9);assert.deepEqual(ui$transform,saved.camera);
      assert.equal(incoming.statementHeading.focuses.length,0);assert.equal(incoming.target.focuses.length,0);
      assert.equal(dom$readingBody.children[0],incoming.readingSource);assert.equal(JSON.stringify(saved),savedBefore);
      assert.deepEqual(mathRoots,[dom$readingBody]);
    """)


def test_foreign_heading_anchor_snapshot_cannot_hide_or_refocus_the_new_card() -> None:
    _run(r"""
      records.get('A').route='/book/a.html#main';records.get('B').route='/book/b.html#main';
      dom$readingBody.focus({});dom$readingBody.scrollTop=211;const saved=captureState();
      saved.readingOpen=false;saved.mode='sources';const savedBefore=JSON.stringify(saved);
      const incoming=headingBundle('B',saved,'reading');incoming.url='https://example.test/book/b.html#main';
      assert.equal(snapshotForRoute(saved,incoming),null);
      const ready=applyRoute(incoming);await frame();await ready;
      assert.notEqual(currentBundle,incoming);assert.equal(currentBundle.snapshot,null);assert.equal(incoming.snapshot,saved);
      assert.equal(hostClasses.has('bp-dag-sheet-open'),true);assert.equal(ui$mode,'graph');
      assert.equal(incoming.target.attrs['aria-current'],'true');assert.equal(incoming.relations.open,true);
      assert.equal(document.activeElement,incoming.statementHeading);assert.equal(dom$readingBody.scrollTop,276);
      assert.equal(incoming.pageHeading.focuses.length,0);assert.equal(incoming.target.focuses.length,0);
      assert.equal(dom$readingBody.children[0],incoming.readingSource);
      const current=captureState(),ticket=restoration;await restoreState(saved,incoming);
      assert.deepEqual(captureState(),current);assert.equal(restoration,ticket);assert.equal(frames.length,0);
      assert.equal(document.activeElement,incoming.statementHeading);assert.equal(JSON.stringify(saved),savedBefore);
    """)


@pytest.mark.parametrize("lifecycle", ["superseded", "disposed"])
def test_stale_heading_anchor_math_cannot_mark_open_scroll_or_focus(lifecycle: str) -> None:
    _run("const lifecycle=" + json.dumps(lifecycle) + ";\n" + r"""
      const oldMath=deferred(),newMath=deferred();mathPass=oldMath.promise;
      const older=headingBundle('A',null,'reading'),oldReady=applyRoute(older);
      let newer,newReady;
      if(lifecycle==='superseded') {
        currentVersion=2;mathPass=newMath.promise;newer=headingBundle('B',null,false);newReady=applyRoute(newer);
      } else alive=false;
      const source=dom$readingBody.children[0];dom$readingBody.scrollTop=65;dom$search.focus({});
      oldMath.resolve(true);await frame();await oldReady;
      assert.equal(older.target.attrs['aria-current'],undefined);assert.equal(older.relations.open,false);
      assert.equal(older.target.classList.contains('bp-reading-current'),false);
      assert.equal(older.statementHeading.focuses.length,0);assert.equal(older.pageHeading.focuses.length,0);
      assert.equal(document.activeElement,dom$search);assert.equal(dom$readingBody.scrollTop,65);
      assert.equal(dom$readingBody.children[0],source);
      if(newer) {
        assert.equal(newer.target.attrs['aria-current'],undefined);assert.equal(newer.relations.open,false);
        newMath.resolve(true);await frame();await newReady;
        assert.equal(newer.target.attrs['aria-current'],'true');assert.equal(newer.relations.open,true);
        assert.equal(newer.statementHeading.focuses.length,0);assert.equal(document.activeElement,dom$search);
        assert.equal(dom$readingBody.scrollTop,276);assert.equal(dom$readingBody.children[0],newer.readingSource);
      }
    """)


def test_stale_heading_anchor_snapshot_cannot_restore_old_focus_or_scroll_into_a_new_source() -> None:
    _run(r"""
      dom$readingBody.focus({});dom$readingBody.scrollTop=211;
      dom$navigation.scrollTop=44;dom$inventoryScroll.scrollTop=9;const saved=captureState(),savedBefore=JSON.stringify(saved);
      const older=headingBundle('A',saved),oldReady=applyRoute(older);await frame();await oldReady;
      const oldRestore=restoreState(saved,older);await queued();
      currentVersion=2;const math=deferred();mathPass=math.promise;
      const newer=headingBundle('B',null,false),newReady=applyRoute(newer);
      dom$readingBody.scrollTop=65;dom$navigation.scrollTop=7;dom$inventoryScroll.scrollTop=8;dom$search.focus({});
      await frame();await oldRestore;
      assert.equal(document.activeElement,dom$search);assert.equal(dom$readingBody.focuses.length,1);
      assert.equal(dom$readingBody.scrollTop,65);assert.equal(dom$navigation.scrollTop,7);assert.equal(dom$inventoryScroll.scrollTop,8);
      assert.equal(newer.target.attrs['aria-current'],undefined);assert.equal(newer.relations.open,false);
      assert.equal(older.statementHeading.focuses.length,0);assert.equal(newer.statementHeading.focuses.length,0);
      math.resolve(true);await frame();await newReady;
      assert.equal(newer.target.attrs['aria-current'],'true');assert.equal(newer.relations.open,true);
      assert.equal(document.activeElement,dom$search);assert.equal(dom$readingBody.scrollTop,276);
      assert.equal(dom$readingBody.children[0],newer.readingSource);assert.equal(JSON.stringify(saved),savedBefore);
    """)


@pytest.mark.parametrize("destination", ["statement_heading", "statement", "page_heading"])
def test_requested_reading_focus_uses_the_marked_statement_after_math_and_anchor_scroll(destination: str) -> None:
    _run("const destination=" + json.dumps(destination) + ";\n" + r"""
      const math=deferred();mathPass=math.promise;dom$search.focus({});
      const incoming=bundle('A',null,'reading'),source=incoming.readingSource;
      const unrelated=new Element('div');unrelated.classList.add('bp-thmheading');source.prepend(unrelated);
      const statementHeading=new Element('div');statementHeading.classList.add('bp-thmheading');
      if(destination==='statement_heading')incoming.target.replaceChildren(statementHeading);
      if(destination==='page_heading')incoming.anchor=null;
      const ready=applyRoute(incoming);
      assert.equal(document.activeElement,dom$search,'Reader focus must await actual math readiness');
      assert.equal(dom$readingBody.scrollTop,0);
      math.resolve(true);await frame();await ready;
      const chosen=destination==='statement_heading'?statementHeading:destination==='statement'?incoming.target:incoming.heading;
      assert.equal(document.activeElement,chosen);assert.equal(chosen.tabIndex,-1);
      assert.deepEqual(chosen.focuses,[{preventScroll:true}]);
      assert.equal(unrelated.focuses.length,0,'An earlier statement heading must not steal the selected statement focus');
      assert.equal(dom$readingBody.scrollTop,destination==='page_heading'?0:276,'Focus must retain the completed anchor scroll');
      assert.equal(incoming.target.classList.contains('bp-reading-current'),destination!=='page_heading');
      if(destination!=='page_heading')assert.equal(incoming.heading.focuses.length,0);
      assert.equal(dom$readingBody.children[0],source);assert.equal(source.querySelector('h1'),incoming.heading);
      assert.equal(document.title,'Complete A');assert.deepEqual(mathRoots,[dom$readingBody]);
    """)


def test_selected_statement_heading_never_overrides_snapshot_reader_focus_or_scroll() -> None:
    _run(r"""
      const owned=new Element('a');dom$readingBody.replaceChildren(owned);owned.focus({});dom$readingBody.scrollTop=211;
      const saved=captureState(),incoming=bundle('A',saved,'reading');
      const statementHeading=new Element('div');statementHeading.classList.add('bp-thmheading');incoming.target.replaceChildren(statementHeading);
      const ready=applyRoute(incoming);await frame();await ready;
      assert.equal(statementHeading.focuses.length,0);assert.equal(incoming.target.focuses.length,0);assert.equal(incoming.heading.focuses.length,0);
      assert.equal(dom$readingBody.scrollTop,0,'Snapshot restoration must not take the new anchor jump');
      const restored=restoreState(saved,incoming);await frame();await restored;
      assert.equal(document.activeElement,dom$readingBody);assert.equal(dom$readingBody.focuses.at(-1).preventScroll,true);
      assert.equal(dom$readingBody.scrollTop,211);assert.equal(statementHeading.focuses.length,0);
      assert.equal(dom$readingBody.children[0],incoming.readingSource);
    """)


def test_stale_math_completion_cannot_focus_an_old_selected_statement_heading() -> None:
    _run(r"""
      const oldMath=deferred(),newMath=deferred();mathPass=oldMath.promise;
      const older=bundle('A',null,'reading'),oldHeading=new Element('div');oldHeading.classList.add('bp-thmheading');older.target.replaceChildren(oldHeading);
      const oldReady=applyRoute(older);currentVersion=2;mathPass=newMath.promise;
      const newer=bundle('B',null,false),newHeading=new Element('div');newHeading.classList.add('bp-thmheading');newer.target.replaceChildren(newHeading);
      const newReady=applyRoute(newer);dom$search.focus({});oldMath.resolve(true);await frame();await oldReady;
      assert.equal(document.activeElement,dom$search);assert.equal(oldHeading.focuses.length,0);assert.equal(newHeading.focuses.length,0);
      assert.equal(newer.target.classList.contains('bp-reading-current'),false);assert.equal(dom$readingBody.scrollTop,0);
      newMath.resolve(true);await frame();await newReady;
      assert.equal(document.activeElement,dom$search);assert.equal(newHeading.focuses.length,0);
      assert(newer.target.classList.contains('bp-reading-current'));assert.equal(dom$readingBody.scrollTop,276);
      assert.equal(dom$readingBody.children[0],newer.readingSource);
    """)


@pytest.mark.parametrize("resource", ["document", "query", "canonical_fragment"])
@pytest.mark.parametrize("legacy", [False, True])
def test_foreign_resource_snapshot_cannot_override_the_new_article(resource: str, legacy: bool) -> None:
    _run("const resource=" + json.dumps(resource) + ",legacy=" + json.dumps(legacy) + ";\n" + r"""
      const routeA = resource === 'canonical_fragment' ? './chapter.html#A' :
        resource === 'query' ? './chapter.html?edition=1#main' : '/book/a.html#main';
      const routeB = resource === 'query' ? 'https://example.test/book/chapter.html?edition=2#main' :
        resource === 'canonical_fragment' ? 'https://example.test/book/chapter.html#B' :
        'https://example.test/book/b.html#main';
      records.get('A').route = routeA;
      // Ordinary sections exercise the resource check; only the theorem case
      // exactly matches B's canonical route and exercises destination identity.
      records.get('B').route = resource === 'canonical_fragment' ? routeB : routeB.replace('#main', '#statement-b');
      const saved = {node: 'A', requestedNode: 'A', mode: 'sources', direction: 'prerequisites',
        statuses: ['fully_proved'], isolates: false, query: 'old A query', area: 'old A area',
        overviewGroup: 'old A group', readingOpen: false, focus: 'A',
        camera: {x: 145, y: -78, scale: 1.1}, readingScroll: 211, outlineScroll: 44,
        inventoryScroll: 9, listLimit: 200, relationIds: ['A'], outlineOpen: true, outlineLimit: 240};
      if (legacy) delete saved.requestedNode;
      const incoming = bundle('B', saved, true); incoming.url = routeB;
      if (resource === 'canonical_fragment') { incoming.anchor = 'B'; incoming.target.id = 'B'; }
      const savedBefore = JSON.stringify(saved), source = incoming.readingSource;
      assert.equal(snapshotForRoute(saved, incoming), null);
      const ready = applyRoute(incoming);
      assert.notEqual(currentBundle, incoming, 'The rejected snapshot must be removed from a cloned bundle');
      assert.equal(currentBundle.snapshot, null); assert.equal(incoming.snapshot, saved);
      assert.equal(hostClasses.has('bp-dag-sheet-open'), true, 'The foreign closed sheet must not hide B');
      assert.equal(hostClasses.has('bp-dag-outline-open'), false);
      assert.equal(ui$requestedNode, 'B'); assert.equal(ui$selected.id, 'B');
      assert.equal(ui$mode, 'graph'); assert.equal(ui$direction, 'both');
      assert.equal(ui$query, ''); assert.equal(ui$area, null); assert.equal(ui$overviewGroup, null);
      assert.equal(ui$relationIds, null); assert.equal(ui$showIsolated, true);
      assert.deepEqual(Array.from(ui$selectedStatuses), presentStatuses);
      assert.deepEqual(ui$transform, {x: 20, y: 30, scale: 1});
      await frame(); await ready;
      assert.equal(dom$readingBody.children[0], source);
      assert.equal(incoming.target.dataset.autoformNodeId, 'B');
      assert.equal(document.title, 'Complete B');
      assert(incoming.target.classList.contains('bp-reading-current'));
      assert.equal(document.activeElement, incoming.target);
      assert.equal(incoming.target.focuses.length, 1);
      assert.equal(incoming.target.focuses[0].preventScroll, true);
      assert.equal(incoming.heading.focuses.length, 0);
      assert.equal(dom$readingBody.scrollTop, 276, 'The real native-anchor calculation must run for B');
      assert.equal(dom$navigation.scrollTop, 0); assert.equal(dom$inventoryScroll.scrollTop, 0);
      assert.equal(overlays.get('A').focuses.length, 0);
      const current = captureState(), ticket = restoration;
      await restoreState(saved, incoming);
      assert.deepEqual(captureState(), current, 'Direct restoration must reject the entire foreign snapshot');
      assert.equal(restoration, ticket); assert.equal(frames.length, 0); assert.equal(restoring, false);
      assert.equal(document.activeElement, incoming.target);
      assert.equal(dom$readingBody.children[0], source);
      assert.equal(JSON.stringify(saved), savedBefore, 'Validation must not mutate history state');
      assert.deepEqual(mathRoots, [dom$readingBody]);
    """)


def test_same_document_section_snapshot_preserves_requested_node_and_reader_intent() -> None:
    _run(r"""
      records.get('A').route = './chapter.html?edition=1#statement-a';
      records.get('B').route = './chapter.html?edition=1#statement-b';
      const owned = new Element('a'); dom$readingBody.replaceChildren(owned); owned.focus({});
      dom$readingBody.scrollTop = 211; dom$navigation.scrollTop = 44; dom$inventoryScroll.scrollTop = 9;
      ui$transform = {x: 145, y: -78, scale: 1.1};
      const saved = captureState(), savedBefore = JSON.stringify(saved);
      assert.equal(saved.requestedNode, 'A'); assert.equal(saved.focus, 'reading');
      const incoming = bundle('A', saved, true);
      incoming.url = 'https://example.test/book/chapter.html?edition=1#appendix';
      incoming.anchor = 'appendix'; incoming.target.id = 'appendix';
      assert.equal(snapshotForRoute(saved, incoming), saved, 'Native sections share a resource despite different fragments');
      assert.equal(snapshotForRoute(saved, Object.assign({}, incoming, {nodeId: 'B'})), saved,
        'A known sibling destination must not reject an ordinary, noncanonical native section');
      const ready = applyRoute(incoming); await frame(); await ready;
      assert.equal(currentBundle, incoming); assert.equal(currentBundle.snapshot, saved);
      assert(incoming.target.classList.contains('bp-reading-current'));
      assert.equal(dom$readingBody.scrollTop, 0, 'A valid snapshot must suppress the new native-anchor jump');
      assert.equal(incoming.heading.focuses.length, 0);
      const restored = restoreState(saved, incoming); await frame(); await restored;
      assert.equal(ui$requestedNode, 'A'); assert.equal(ui$selected.id, 'A');
      assert.equal(document.activeElement, dom$readingBody);
      assert.equal(dom$readingBody.focuses.at(-1).preventScroll, true);
      assert.equal(dom$readingBody.scrollTop, 211);
      assert.equal(dom$navigation.scrollTop, 44); assert.equal(dom$inventoryScroll.scrollTop, 9);
      assert.deepEqual(ui$transform, saved.camera);
      assert.equal(JSON.stringify(saved), savedBefore);
    """)


@pytest.mark.parametrize("requested", [None, "legacy-missing"])
def test_null_or_unknown_requested_snapshot_keeps_legacy_restoration(requested: str | None) -> None:
    _run("const requested=" + json.dumps(requested) + ";\n" + r"""
      records.get('A').route = '/book/a.html#main'; records.get('B').route = '/book/b.html#main';
      const saved = {node: null, requestedNode: requested, mode: 'list', direction: 'both',
        statuses: ['fully_proved'], readingOpen: false, focus: 'search',
        camera: {x: -90, y: 70, scale: 0.8}, readingScroll: 89, outlineScroll: 44,
        inventoryScroll: 9, listLimit: 200};
      const incoming = bundle('B', saved, true); incoming.url = 'https://example.test/book/b.html#main';
      assert.equal(snapshotForRoute(null, incoming), null);
      assert.equal(snapshotForRoute(saved, incoming), saved, 'Unknown owner identity is not proof of a mismatch');
      if (requested === null) {
        const explicitNull = Object.assign({}, saved, {node: 'A'});
        assert.equal(snapshotForRoute(explicitNull, incoming), explicitNull, 'Explicit null must not fall back to saved.node');
      }
      const ready = applyRoute(incoming); await frame(); await ready;
      assert.equal(currentBundle, incoming); assert.equal(hostClasses.has('bp-dag-sheet-open'), false);
      assert.equal(incoming.heading.focuses.length, 0); assert.equal(dom$readingBody.scrollTop, 0);
      const restored = restoreState(saved, incoming); await frame(); await restored;
      assert.equal(ui$requestedNode, requested); assert.equal(ui$selected, null);
      assert.equal(ui$mode, 'list'); assert.equal(ui$listLimit, 200);
      assert.equal(hostClasses.has('bp-dag-sheet-open'), false);
      assert.equal(document.activeElement, dom$search); assert.equal(dom$search.focuses.at(-1).preventScroll, true);
      assert.equal(dom$readingBody.scrollTop, 89);
      assert.equal(dom$navigation.scrollTop, 44); assert.equal(dom$inventoryScroll.scrollTop, 9);
      assert.deepEqual(ui$transform, saved.camera);
      assert.equal(dom$readingBody.children[0], incoming.readingSource);
    """)


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
    _run(r"""
      const page = bundle('A', null), relations = new Element('details');relations.classList.add('bp-dependencies');
      page.target.replaceChildren(relations); const ready = applyRoute(page);
      await frame(); await ready;
      assert(page.target.classList.contains('bp-reading-current'));
      assert.equal(relations.open, true);
    """)


def test_article_commit_closes_transient_status_menu_without_losing_keyboard_focus() -> None:
    _run(r"""
      const input=new Element('input'); dom$filterPopover.replaceChildren(input);
      setFilters(true); input.focus({});
      assert.equal(dom$filterPopover.hidden,false);
      const incoming=bundle('A',null,false),ready=applyRoute(incoming);
      assert.equal(dom$filterPopover.hidden,true);
      assert.equal(dom$filterSummary.attrs['aria-expanded'],'false');
      assert.equal(document.activeElement,dom$filterSummary);
      assert.equal(dom$filterSummary.focuses.at(-1).preventScroll,true);
      await frame(); await ready;
      assert.equal(document.activeElement,dom$filterSummary);
      setFilters(true); input.focus({});
      const selected=bundle('B',null,true),selectedReady=applyRoute(selected);
      assert.equal(document.activeElement,dom$filterSummary);
      await frame(); await selectedReady;
      assert.equal(document.activeElement,selected.target);
      assert.equal(selected.target.focuses.at(-1).preventScroll,true);
      assert.equal(selected.heading.focuses.length,0);
      // Reopening/filtering does not shift a user's input focus or invent history state.
      setFilters(true); input.focus({}); setFilters(true);
      assert.equal(dom$filterPopover.hidden,false); assert.equal(document.activeElement,input);
      dom$search.focus({}); const searchFocuses=dom$search.focuses.length; setFilters(false);
      assert.equal(document.activeElement,dom$search); assert.equal(dom$search.focuses.length,searchFocuses);
    """)


def test_article_commit_closes_browse_and_returns_focus_before_requested_reading_focus() -> None:
    _run(r"""
      const choice=new Element('button');dom$browsePopover.replaceChildren(choice);
      setBrowse(true);choice.focus({});
      const incoming=bundle('A',null,false),ready=applyRoute(incoming);
      assert.equal(dom$browsePopover.hidden,true);assert.equal(dom$browseToggle.attrs['aria-expanded'],'false');
      assert.equal(document.activeElement,dom$browseToggle);assert.equal(dom$browseToggle.focuses.at(-1).preventScroll,true);
      await frame();await ready;assert.equal(document.activeElement,dom$browseToggle);
      setBrowse(true);choice.focus({});
      const selected=bundle('B',null,true),selectedReady=applyRoute(selected);
      assert.equal(dom$browsePopover.hidden,true);assert.equal(document.activeElement,dom$browseToggle);
      await frame();await selectedReady;
      assert.equal(document.activeElement,selected.target);assert.equal(selected.target.focuses.at(-1).preventScroll,true);
      assert.equal(selected.heading.focuses.length,0);
    """)


def test_group_history_keeps_derived_selection_reading_focus_and_saved_camera() -> None:
    _run(r"""
      ui$overviewGroup='area:Authored geometry';ui$scopeOverview=true;
      dom$readingBody.focus({});dom$readingBody.scrollTop=89;
      ui$transform={x:145,y:-78,scale:1.1};
      const saved=captureState();assert.equal(saved.overviewGroup,'area:Authored geometry');
      assert.equal(saved.focus,'reading');
      ui$overviewGroup=null;ui$scopeOverview=false;ui$transform={x:0,y:0,scale:.9};
      const restored=bundle('A',saved),ready=applyRoute(restored);await frame();await ready;
      const pending=restoreState(saved,restored);await frame();await pending;
      assert.equal(ui$overviewGroup,saved.overviewGroup);assert.deepEqual(ui$transform,saved.camera);
      assert.equal(dom$readingBody.scrollTop,89);assert.equal(document.activeElement,dom$readingBody);
      // An actual article navigation clears derived group intent, not its authored math.
      const next=bundle('B',null,true),nextReady=applyRoute(next);await frame();await nextReady;
      assert.equal(ui$overviewGroup,null);assert.equal(document.activeElement,next.target);
      assert.equal(next.heading.focuses.length,0);
      assert(next.target.classList.contains('bp-reading-current'));
    """)


def test_large_scope_landing_never_overrides_explicit_reading_or_saved_sheet_intent() -> None:
    _run(r"""
      const project=updateProjection;
      updateProjection=function(){project();ui$scopeOverview=true;};
      byId.get('A').result=false;
      const landing=bundle('A',null,false);landing.anchor=null;
      const landingReady=applyRoute(landing);
      assert.equal(host.classList.contains('bp-dag-sheet-open'),false);
      await frame();await landingReady;
      for(const intent of ['reading','anchor']) {
        const incoming=bundle('A',null,intent==='reading'?'reading':false);
        if(intent==='reading')incoming.anchor=null;
        const ready=applyRoute(incoming);
        assert.equal(host.classList.contains('bp-dag-sheet-open'),true,
          'A derived overview must not hide an explicitly requested mathematical reading');
        await frame();await ready;
      }
      dom$nodeLayer={querySelector(selector){
        assert.equal(selector,'.bp-dag-node[tabindex="0"]');return overlays.get('A');
      }};
      const graphEntry=bundle('A',null,true);graphEntry.anchor=null;graphEntry.mapFirst=true;
      const graphReady=applyRoute(graphEntry);
      assert.equal(host.classList.contains('bp-dag-sheet-open'),false);
      await frame();await graphReady;
      assert.equal(graphEntry.heading.focuses.length,0,'Map exploration cannot focus an inert reader');
      assert.equal(document.activeElement,overlays.get('A'));
      const anchorEntry=bundle('A',null,true);anchorEntry.mapFirst=true;
      const anchorReady=applyRoute(anchorEntry);
      assert.equal(host.classList.contains('bp-dag-sheet-open'),true,'An explicit heading wins over map preference');
      await frame();await anchorReady;
      const saved={readingOpen:false},incoming=bundle('A',saved,'reading');
      const ready=applyRoute(incoming);
      assert.equal(host.classList.contains('bp-dag-sheet-open'),false,
        'History must retain the saved reading-sheet state');
      await frame();await ready;
    """)


def test_map_intent_on_a_small_pure_overview_never_focuses_the_inert_reader() -> None:
    _run(r"""
      byId.get('A').result=false;
      navigation.readerIsOverview=()=>true;
      dom$nodeLayer={querySelector(selector){
        assert.equal(selector,'.bp-dag-node[tabindex="0"]');return overlays.get('A');
      }};
      const incoming=bundle('A',null,true);incoming.anchor=null;incoming.mapFirst=true;
      incoming.graph.nodes=incoming.graph.nodes.map(node=>({...node,_region:'Authored overview area'}));
      const ready=applyRoute(incoming);
      assert.equal(ui$scopeOverview,false,'This case must exercise the small pure-overview branch');
      assert.equal(ui$overviewOnly,true);
      assert.equal(host.classList.contains('bp-dag-sheet-open'),false);
      await frame();await ready;
      assert.equal(incoming.heading.focuses.length,0);assert.equal(document.activeElement,overlays.get('A'));
      const reading=bundle('A',null,true);reading.anchor=null;
      reading.graph.nodes=incoming.graph.nodes;
      const readingReady=applyRoute(reading);
      assert.equal(ui$overviewOnly,false,'Explicit reading intent still opens the source');
      assert.equal(host.classList.contains('bp-dag-sheet-open'),true);
      await frame();await readingReady;
      // With no statement to land on, the page's own title takes focus.
      assert.equal(document.activeElement,reading.pageHeading);
    """)
