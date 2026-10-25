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
  const heading = new Element('h1'), target = new Element('article', 'main'), source = new Element('div');
  target.dataset.autoformNodeId = id; source.replaceChildren(heading, target);
  const version = currentVersion;
  // A normal two-result scope is not an authored-area navigation overview.
  // This seam supplies graph context only; callbacks remain verbatim production source.
  return {nodeId: id, scope: 'chapter', anchor: 'main', title: 'Complete ' + id, readingSource: source,
    graph: {schema: 'autoform-dag-view/v2', title: 'Scope ' + id, nodes: Array.from(byId.values()), edges: [],
      palette, view: {kind: 'chapter', presentation: 'dag'}},
    snapshot, focus, isCurrent: () => currentVersion === version, heading, target};
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
      assert.equal(document.activeElement, incoming.heading);
      assert.equal(incoming.heading.focuses.length, 1);
      assert.equal(incoming.heading.focuses[0].preventScroll, true);
      assert.equal(dom$readingBody.scrollTop, 276, 'The real native-anchor calculation must run for B');
      assert.equal(dom$navigation.scrollTop, 0); assert.equal(dom$inventoryScroll.scrollTop, 0);
      assert.equal(overlays.get('A').focuses.length, 0);
      const current = captureState(), ticket = restoration;
      await restoreState(saved, incoming);
      assert.deepEqual(captureState(), current, 'Direct restoration must reject the entire foreign snapshot');
      assert.equal(restoration, ticket); assert.equal(frames.length, 0); assert.equal(restoring, false);
      assert.equal(document.activeElement, incoming.heading);
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
      assert.equal(document.activeElement,selected.heading);
      assert.equal(selected.heading.focuses.at(-1).preventScroll,true);
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
      assert.equal(document.activeElement,selected.heading);assert.equal(selected.heading.focuses.at(-1).preventScroll,true);
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
      assert.equal(ui$overviewGroup,null);assert.equal(document.activeElement,next.heading);
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
      assert.equal(document.activeElement,reading.heading);
    """)
