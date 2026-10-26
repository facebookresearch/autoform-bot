"""Execute the real projection functions with isolated DOM/controller collaborators."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoform_cli.dag_viewer import MAX_MAP_NODES
from autoform_cli import graph_pages, render
from tests.test_integrated_publication import _decoded_relations, _project, _write

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated projection tests")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"

_LOADER = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(CLIENT, 'utf8');
function original(name) {
  const start = source.indexOf('function ' + name + '(');
  assert(start >= 0, 'Production function missing: ' + name);
  // Let Node's parser find the complete declaration, including nested braces,
  // strings, comments and regular expressions. No production text is replaced.
  for (let end = source.indexOf('}', start); end >= 0; end = source.indexOf('}', end + 1)) {
    const declaration = source.slice(start, end + 1);
    try { new vm.Script('"use strict"; (' + declaration + ');'); return declaration; }
    catch (error) { if (!(error instanceof SyntaxError)) throw error; }
  }
  assert.fail('Production function did not parse: ' + name);
}
const constants = source.match(/\bvar NODE_W =[^;]+;/);
assert(constants, 'Production map constants missing');
const functions = [
  'element', 'button', 'readingLabel', 'refreshReadingLabels', 'safeHref', 'graphAt', 'recordId', 'mapNode', 'adoptGraph',
  'neighborIds', 'neighborhood', 'regionProjection', 'collect', 'matchesStatus', 'calculateActive',
  'statusLabel', 'makeNodeControl', 'closeSearch', 'activateNode', 'inventoryNodes', 'inCurrentScope', 'sourceNodes', 'renderInventory'
].map(original).join('\n');
const fixtureText = fs.readFileSync(0, 'utf8');
const sandbox = {assert, URL, fixtureText}; vm.createContext(sandbox);
"""

_ENVIRONMENT = r"""
class Element {
  constructor(tag) { this.tagName = tag; this.attrs = {}; this.dataset = {}; this.children = []; this.listeners = {}; this.text = ''; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.text = String(value); this.children = []; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name] === undefined ? null : this.attrs[name]; }
  appendChild(child) { this.children.push(child); return child; }
  querySelector(tag) { return this.children.find(child => child.tagName === tag) || null; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
}
var document = {createElement: tag => new Element(tag)};
var window = {location: {href: 'https://example.test/math/roadmap/chapter/index.html', origin: 'https://example.test'}};
var location = window.location;
var host = {classList: {toggle() {}}};
var dom$hiddenButton = new Element('button'), dom$empty = new Element('p'), dom$searchResults = new Element('div');
var dom$inventory = new Element('section'), dom$inventoryList = new Element('ul'), dom$inventoryRows;
var dom$inventorySummary = new Element('span'), dom$loadMore = new Element('button'), dom$inventoryScroll = new Element('div');
dom$inventory.appendChild(new Element('h2'));
var data, view, atlas, palette, byId, prerequisites, dependents, incident, statusCounts, presentStatuses;
var mobileIsolateBase, mapAllowed, contextGraph, sharedIndex, currentBundle;
var records, navigation, calls;
var ui$active, ui$activeDirty, ui$activeIds, ui$selected, ui$direction, ui$selectedStatuses, ui$showIsolated;
var ui$query, ui$relationIds, ui$mode, ui$listLimit;
var plural = (count, one, many) => count.toLocaleString() + ' ' + (count === 1 ? one : many);
function leaf(id) {
  return {id, title: id, kind: 'node', status: 'planned', result: true,
    scope: 'chapter', parent: 'chapter', route: '../roadmap/chapter/index.html#' + encodeURIComponent(id)};
}
function scope(id, extra = {}) {
  return Object.assign({id, title: id, kind: 'scope', status: 'planned', result: false,
    scope: id, parent: 'roadmap', route: '../roadmap/' + encodeURIComponent(id) + '/index.html'}, extra);
}
function viewNode(id, extra = {}) {
  return Object.assign({id, title: id, kind: 'node', status: 'planned', member_count: 1,
    status_counts: [['planned', 1]], isolated: false, internal_dependency_count: 0,
    url: '../roadmap/chapter/index.html#' + encodeURIComponent(id)}, extra);
}
function graph(nodes, edges = [], presentation = 'dag') {
  return {schema: 'autoform-dag-view/v2', title: 'Chapter', nodes, edges,
    palette: {planned: {label: 'planned'}, fully_proved: {label: 'fully proved'}},
    present_statuses: ['planned', 'fully_proved'], regions: [],
    edge_count: edges.reduce((count, edge) => count + edge.statement + edge.proof, 0),
    view: {kind: 'chapter', presentation, book: '../roadmap/chapter/index.html', breadcrumbs: []}};
}
function configure(indexNodes, context, relations = []) {
  records = new Map(indexNodes.map(node => [node.id, node]));
  sharedIndex = {schema: 'autoform-dag-search/v1', nodes: indexNodes, relations};
  contextGraph = context; currentBundle = {dataURL: 'https://example.test/math/dependencies/chapters/chapter.json'};
  calls = []; navigation = {navigateNode(id, settings) { calls.push({id, settings}); }};
  ui$active = []; ui$activeDirty = true; ui$activeIds = new Set(); ui$selected = null; ui$direction = 'both';
  ui$selectedStatuses = new Set(['planned', 'fully_proved']); ui$showIsolated = true;
  ui$query = ''; ui$relationIds = null; ui$mode = 'graph'; ui$listLimit = LIST_PAGE;
}
"""


def _run(body: str, *, fixture: object = None) -> None:
    program = "var fixture = JSON.parse(fixtureText);\n" + _ENVIRONMENT + "\n"
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    script += "vm.runInContext(" + json.dumps('"use strict";\n') + "+constants[0]+"
    script += json.dumps(program) + "+functions+" + json.dumps(body)
    script += ", sandbox, {timeout: 5000, filename: CLIENT});"
    result = subprocess.run([_NODE, "-e", script], input=json.dumps(fixture), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_isolated_focal_result_has_no_arbitrary_dependency_peers_and_keeps_the_inventory() -> None:
    _run(r"""
      const input = [leaf('T'), leaf('A'), leaf('B')];
      const authored = {source: 'A', target: 'B', statement: 1, proof: 0};
      configure(input, graph(input.map(node => viewNode(node.id)), [authored]), [['A', 'B', 1, 0]]);
      const before = JSON.stringify({input, contextGraph, sharedIndex});
      const projected = neighborhood('T');
      assert.deepEqual(projected.nodes.map(node => node.id), ['T']);
      assert.deepEqual(projected.edges, []);
      adoptGraph(projected);
      assert.equal(byId.get('T').isolated, true);
      assert(makeNodeControl(byId.get('T')).getAttribute('aria-label').includes('no dependency edges'));
      ui$selected = byId.get('T'); ui$showIsolated = false; ui$activeDirty = true; calculateActive();
      assert.deepEqual(ui$active.map(node => node.id), ['T'], 'The explicitly selected isolated article remains reachable');
      currentBundle.scope = 'chapter'; ui$mode = 'list';
      assert.deepEqual(inventoryNodes().map(node => node.id).sort(), ['A', 'B', 'T']);
      assert.equal(JSON.stringify({input, contextGraph, sharedIndex}), before);
    """)


def test_a_real_connected_neighborhood_keeps_exact_edges_and_connected_controls() -> None:
    _run(r"""
      const input = [leaf('T'), leaf('A'), leaf('B')];
      const authored = {source: 'A', target: 'B', statement: 1, proof: 0};
      configure(input, graph(input.map(node => viewNode(node.id)), [authored]), [['A', 'B', 1, 0]]);
      const before = JSON.stringify({input, contextGraph, sharedIndex});
      const projected = neighborhood('A');
      assert.deepEqual(projected.nodes.map(node => node.id).sort(), ['A', 'B']);
      assert.deepEqual(projected.edges, [authored]);
      adoptGraph(projected);
      for (const id of ['A', 'B']) {
        assert.equal(byId.get(id).isolated, false, id + ' has a real retained dependency');
        assert(!makeNodeControl(byId.get(id)).getAttribute('aria-label').includes('no dependency edges'));
      }
      ui$showIsolated = false; ui$activeDirty = true; calculateActive();
      assert.deepEqual(ui$active.map(node => node.id).sort(), ['A', 'B']);
      assert(data.edges.every(edge => ui$activeIds.has(edge.source) && ui$activeIds.has(edge.target)));
      assert.equal(JSON.stringify({input, contextGraph, sharedIndex}), before);
    """)


def test_zero_count_authored_regions_keep_zero_and_remain_visible_under_normal_filters() -> None:
    _run(r"""
      const index = Array.from({length: 12}, (_, n) => scope('topic' + n));
      const topics = index.map((node, n) => viewNode('scope:' + node.id, {kind: 'scope',
        area: n < 6 ? 'Algebra' : 'Analysis', member_count: 0, status_counts: []}));
      const edge = {source: topics[0].id, target: topics[7].id, statement: 1, proof: 0};
      configure(index, graph(topics, [edge], 'atlas'), [['topic0', 'topic7', 1, 0]]);
      const projected = regionProjection(contextGraph, null);
      assert.equal(projected.nodes.length, 2);
      for (const node of projected.nodes) {
        assert.equal(node.member_count, 0, 'Narrative-only scopes are not formalization targets');
        assert.deepEqual(node.status_counts, []);
      }
      assert.deepEqual(projected.edges, [{source: 'region:Algebra', target: 'region:Analysis', statement: 1, proof: 0}]);
      adoptGraph(projected); calculateActive();
      assert.deepEqual(ui$active.map(node => node.id).sort(), ['region:Algebra', 'region:Analysis']);
      assert.equal(mapAllowed, true);
      const expanded = regionProjection(contextGraph, 'Algebra');
      assert.equal(expanded.nodes.filter(node => node.area === 'Algebra').length, 6);
      assert(expanded.nodes.every(node => node.member_count === 0));
    """)


def test_authoritative_ids_typed_view_ids_and_derived_boundaries_remain_distinct() -> None:
    _run(r"""
      const raw = [leaf('Geometry'), leaf('scope:Geometry'), leaf('boundary:Geometry'),
        scope('Atlas', {navigation: 'source'}), scope('scope:Atlas'), scope('boundary:Atlas'), leaf('F')];
      const external = viewNode('boundary:scope:Atlas', {kind: 'boundary', member_count: 2,
        status_counts: [['planned', 1], ['fully_proved', 1]], internal_dependency_count: 7});
      configure(raw, graph([viewNode('F'), external], [
        {source: external.id, target: 'F', statement: 1, proof: 0}
      ]), [['scope:Atlas', 'F', 1, 0]]);
      for (const node of raw) {
        assert.equal(recordId(node), node.id, 'Index IDs are authoritative even for containers');
        activateNode(node, false); assert.equal(calls.at(-1).id, node.id);
      }
      const synthetic = viewNode('scope:Atlas', {kind: 'scope'});
      assert.equal(recordId(synthetic), 'Atlas');
      assert.equal(mapNode(synthetic), false, 'The synthetic scope denotes the source track');
      assert.equal(mapNode(records.get('scope:Atlas')), true, 'The same-named indexed article is mathematical');
      assert.equal(recordId(viewNode('scope:scope:Atlas', {kind: 'scope'})), 'scope:Atlas');
      assert.equal(recordId(external), 'scope:Atlas');
      const projected = neighborhood('F'), boundary = projected.nodes.find(node => node.id === 'scope:Atlas');
      assert.equal(boundary._recordId, 'scope:Atlas');
      assert.equal(boundary.kind, 'boundary');
      assert.equal(boundary.member_count, 2);
      assert.equal(boundary._unknownCount, false);
      assert.deepEqual(boundary.status_counts, external.status_counts);
      assert.equal(boundary.internal_dependency_count, 7);
      assert.equal(recordId(boundary), 'scope:Atlas', 'The derived copy must not normalize a second time');
      assert.deepEqual(projected.edges, [{source: 'scope:Atlas', target: 'F', statement: 1, proof: 0}]);
      activateNode(boundary, false); assert.equal(calls.at(-1).id, 'scope:Atlas');
    """)


def test_a_thousand_real_neighbors_stay_within_the_map_bound_without_inventing_edges() -> None:
    _run("assert.equal(MAX_MAP_NODES, " + str(MAX_MAP_NODES) + ");\n" + r"""
      const input = [leaf('F'), ...Array.from({length: 1000}, (_, n) => leaf('peer' + n))];
      const relations = input.slice(1).map((node, n) => n < 500 ?
        [node.id, 'F', n % 2 ? 0 : 1, n % 2 ? 1 : 0] : ['F', node.id, n % 2 ? 0 : 1, n % 2 ? 1 : 0]);
      const edges = relations.map(row => ({source: row[0], target: row[1], statement: row[2], proof: row[3]}));
      configure(input, graph(input.map(node => viewNode(node.id)), edges), relations);
      const projected = neighborhood('F'), ids = new Set(projected.nodes.map(node => node.id));
      assert.equal(projected.nodes.length, MAX_MAP_NODES);
      assert(projected.nodes.length <= 120);
      assert.equal(ids.size, projected.nodes.length);
      assert(ids.has('F'));
      assert.equal(projected.edges.length, MAX_MAP_NODES - 1);
      for (const edge of projected.edges) {
        assert(ids.has(edge.source) && ids.has(edge.target));
        assert(relations.some(row => row[0] === edge.source && row[1] === edge.target &&
          row[2] === edge.statement && row[3] === edge.proof), 'Every rendered edge retains exact authored types');
      }
      assert.equal(neighborIds('F').before.length, 500);
      assert.equal(neighborIds('F').after.length, 500);
      adoptGraph(projected); calculateActive();
      assert.equal(mapAllowed, true); assert.equal(ui$active.length, MAX_MAP_NODES);
    """)


def test_an_oversized_module_scope_keeps_its_real_math_articles_navigable_without_proof_claims(tmp_path: Path) -> None:
    project, _group, _explanation = _project(tmp_path)
    blueprint, output = project / "blueprint", tmp_path / "out"
    topic = blueprint / "roadmap/catalogs"
    count = MAX_MAP_NODES + 1
    _write(topic / "README.md", "# Existing mathematics\n\nThe module articles describe the existing mathematical library.\n")
    _write(blueprint / "sources/ledger.md", "# Declaration ledger\n\nThe modules already in the library.\n")
    for index in range(count):
        progress = "lean: Demo.main\nstatement: formalized\nproof: formalized\n" if index == 0 else ""
        _write(topic / f"module-{index:03d}.md", (
            f"---\ncatalog: module\n{progress}---\n\n# Mathematical module {index}\n\n"
            "The mathematical content includes the identity $0 = 0$.\n\n"
            "## Sources\n\n- [Declaration ledger](../../sources/ledger.md)\n"
        ))
    _write(topic / "note.md", "# A note\n\nA separate narrative, not a declaration or module article.\n")
    _write(topic / "source.md", "---\nnavigation: source\n---\n\n# Textbook source\n\nBibliographic provenance.\n")
    coverage = blueprint / "coverage/README.md"
    _write(coverage, coverage.read_text(encoding="utf-8") + "| Modules | INVENTORIED | [Modules](../roadmap/catalogs/README.md) |\n")

    render.render_site(blueprint, output, lean_root=project)

    index = json.loads((output / graph_pages.SEARCH_INDEX).read_text(encoding="utf-8"))
    context = json.loads((output / "dependencies/chapters/catalogs.json").read_text(encoding="utf-8"))
    records = {node["id"]: node for node in index["nodes"]}
    module_ids = {f"catalogs/module-{number:03d}" for number in range(count)}
    assert all(records[node_id]["catalog"] == "module" and records[node_id]["result"] is False for node_id in module_ids)
    assert {records[node_id]["status"] for node_id in module_ids} == {"inventory_pending", "inventory_checked"}
    for status in ("inventory_pending", "inventory_checked"):
        assert context["palette"][status]["neutral"] is True
        assert "not a mathematical proof" in context["palette"][status]["meaning"]
    book = (output / "roadmap/catalogs/README.md").read_text(encoding="utf-8")
    assert book.count("The mathematical content includes the identity") == count
    assert book.count('class="bp-reading-source"') == 1
    assert not (output / "roadmap/catalogs/module-120.md").exists()

    # Isolated projection callbacks receive canonical IDs from navigation,
    # not the publisher's wire offsets; retain an independent fixture decoder.
    nodes = [{**node, "parent": index["nodes"][node["parent"]]["id"]}
             if type(node.get("parent")) is int else node for node in index["nodes"]]
    assert all(node["parent"] == "catalogs" for node in nodes if node["id"] in module_ids)
    _run(r"""
      configure(fixture.index.nodes, fixture.context, fixture.index.relations);
      currentBundle.scope = 'catalogs'; adoptGraph(contextGraph);
      assert.equal(mapAllowed, false, 'The actual oversized scope uses its bounded article inventory');
      ui$mode = 'list'; ui$selectedStatuses = new Set(presentStatuses);
      const articles = inventoryNodes();
      assert.equal(articles.length, fixture.count);
      assert(articles.every(node => node.catalog === 'module' && node.result === false));
      assert(!articles.some(node => ['catalogs', 'catalogs/note', 'catalogs/source'].includes(node.id)));
      assert(!articles.some(node => node.id.startsWith('chapter/')), 'Scoped inventory must not leak other results');
      renderInventory(true);
      assert.equal(dom$inventory.querySelector('h2').textContent, 'Results & articles');
      assert.equal(dom$inventorySummary.textContent, '100 of ' + fixture.count);
      assert.equal(dom$inventoryList.children.length, 100);
      assert.equal(dom$loadMore.hidden, false);
      ui$listLimit = fixture.count; renderInventory(true);
      assert.equal(dom$inventorySummary.textContent, fixture.count + ' articles');
      assert.equal(dom$inventoryList.children.length, fixture.count);
      assert.equal(dom$loadMore.hidden, true);
      const last = articles.at(-1);
      dom$inventoryRows.get(last.id).listeners.click();
      assert.equal(calls.at(-1).id, last.id, 'Every module article remains a real navigation target');
      assert(last.route && last.route.includes('/roadmap/catalogs/index.html#'));
      ui$mode = 'sources';
      assert.deepEqual(inventoryNodes().map(node => node.id), ['catalogs/source']);
    """, fixture={"index": {**index, "nodes": nodes, "relations": _decoded_relations(index)}, "context": context, "count": count})
