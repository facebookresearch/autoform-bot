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
const constants = source.match(/\bvar LIST_PAGE =[^;]+;/);
assert(constants, 'Production list page size missing');
const functions = [
  'element', 'readingLabel', 'button', 'bindNodeLink', 'safeHref', 'graphAt', 'recordId', 'listed', 'selection', 'adoptGraph',
  'statusLabel', 'closeSearch', 'activateNode', 'inventoryNodes', 'renderInventory', 'trailList',
  'semanticParent', 'placeOf', 'pendingMath', 'summaryLine', 'refreshReadingLabels', 'tooltip', 'inCurrentScope', 'sourceNodes'
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
  removeAttribute(name) { delete this.attrs[name]; }
  getAttribute(name) { return this.attrs[name] === undefined ? null : this.attrs[name]; }
  appendChild(child) { this.children.push(child); return child; }
  querySelector(tag) { return this.children.find(child => child.tagName === tag) || null; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
}
var document = {createElement: tag => new Element(tag)};
var window = {location: {href: 'https://example.test/math/roadmap/chapter/index.html', origin: 'https://example.test'}};
var location = window.location;
var host = {dataset: {}, classList: {toggle() {}}};
var dom$hiddenButton = new Element('button'), dom$searchResults = new Element('div');
var dom$inventory = new Element('section'), dom$inventoryList = new Element('ul'), dom$inventoryRows;
var dom$inventorySummary = new Element('span'), dom$loadMore = new Element('button'), dom$inventoryScroll = new Element('div');
dom$inventory.appendChild(new Element('h2'));
var data, palette, byId, incident, statusCounts, presentStatuses;
var mobileIsolateBase, contextGraph, sharedIndex, currentBundle;
var records, navigation, calls;
var ui$selected, ui$selectedStatuses;
var ui$query, ui$relationIds, ui$mode, ui$listLimit;
var plural = (count, one, many) => count.toLocaleString() + ' ' + (count === 1 ? one : many);
var num = (value) => value.toLocaleString();
// The list typesets a row's formulas lazily as it scrolls into view; these callbacks run without a browser.
function typesetRowsInView() {}
var instance = 1;
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
  ui$selected = null;
  ui$selectedStatuses = new Set(['planned', 'fully_proved']);
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


def test_an_unlinked_result_keeps_its_place_in_the_statement_list() -> None:
    _run(r"""
      const input = [leaf('T'), leaf('A'), leaf('B')];
      const authored = {source: 'A', target: 'B', statement: 1, proof: 0};
      configure(input, graph(input.map(node => viewNode(node.id)), [authored]), [['A', 'B', 1, 0]]);
      const before = JSON.stringify({input, contextGraph, sharedIndex});
      currentBundle.scope = 'chapter'; ui$mode = 'list';
      assert.deepEqual(inventoryNodes().map(node => node.id).sort(), ['A', 'B', 'T']);
      assert.equal(JSON.stringify({input, contextGraph, sharedIndex}), before);
    """)


def test_a_row_says_where_its_result_or_topic_lives() -> None:
    _run(r"""
      // ATLAS has 93 results titled "Conjecture", and a search showed them as identical rows.
      const root = scope('roadmap', {parent: undefined}), area = scope('algebra', {title: 'Algebra'}),
        shelf = scope('algebra/ext', {title: 'MathlibExt', parent: 'algebra', navigation: 'collection'}),
        topic = scope('algebra/ext/ring', {title: 'Ring', parent: 'algebra/ext'}),
        deep = scope('algebra/ext/ring/ideal', {title: 'Ideal', parent: 'algebra/ext/ring'});
      const result = Object.assign(leaf('Conjecture'), {id: 'algebra/ext/ring/ideal/conjecture', scope: 'algebra/ext/ring/ideal', parent: 'algebra/ext/ring/ideal'});
      configure([root, area, shelf, topic, deep, result], graph([]));
      // The two nearest topics under the root, and a topic says it is one.
      assert.equal(placeOf(result), 'Ring › Ideal');
      assert.equal(placeOf(topic), 'Algebra');
      assert.equal(placeOf(area), '');
      // A collection's own page counts as a place for what lives on it.
      const onShelf = Object.assign(leaf('Lemma'), {id: 'algebra/ext/lemma', scope: 'algebra/ext', parent: 'algebra/ext'});
      configure([root, area, shelf, topic, deep, result, onShelf], graph([]));
      assert.equal(placeOf(onShelf), 'Algebra › MathlibExt');
      // A list stops at its own page, so rows in it name only what lies below.
      assert.equal(placeOf(result, 'algebra/ext/ring'), 'Ideal');
      assert.equal(placeOf(result, 'algebra/ext/ring/ideal'), '');
    """)


def test_a_list_row_sets_its_summarys_code_and_tex_instead_of_printing_them() -> None:
    _run(r"""
      // Statement lists printed "$u : \mathbb{N} \to \alpha$" and "`mu0`" as typed, after a run-in repeating the title.
      const line = summaryLine({title: 'Eventually periodic sequences',
        summary: 'Eventually periodic sequences. A sequence $u : \\mathbb{N} \\to \\alpha$ is eventually periodic, as `mu0` checks, see \\(p\\).'});
      assert.equal(line.className, 'bp-result-summary');
      assert.deepEqual(line.children.map(part => [String(part.tagName).toLowerCase(), part.className || '', part.textContent]), [
        ['span', '', 'A sequence '], ['span', 'bp-math-pending', '\\(u : \\mathbb{N} \\to \\alpha\\)'], ['span', '', ' is eventually periodic, as '],
        ['code', '', 'mu0'], ['span', '', ' checks, see '], ['span', 'bp-math-pending', '\\(p\\)'], ['span', '', '.']]);
      // A summary that only shares the title's opening words keeps them.
      assert.equal(summaryLine({title: 'Group', summary: 'Groups act on sets.'}).textContent, 'Groups act on sets.');
      assert.equal(summaryLine({title: 'Lemma', summary: 'Plain prose.'}).textContent, 'Plain prose.');
    """)


def test_the_page_adopts_only_the_entries_it_lists() -> None:
    _run(r"""
      // An overview payload marks the entries its overview leaves out, and a source track's scope is a source text, not a topic.
      const source = scope('Atlas', {navigation: 'source'}), topic = scope('Geometry');
      const entries = [viewNode('scope:Atlas', {kind: 'scope'}), viewNode('scope:Geometry', {kind: 'scope'}), viewNode('F'),
        viewNode('page-2', {map: false, status: 'fully_proved'})];
      configure([source, topic, leaf('F')], graph(entries));
      adoptGraph(contextGraph);
      assert.deepEqual(data.nodes.map(node => node.id), ['scope:Geometry', 'F']);
      assert.deepEqual(Array.from(byId.keys()), ['scope:Geometry', 'F']);
      assert.equal(contextGraph.nodes.length, 4, 'The payload itself stays as published');
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
      assert.equal(recordId(viewNode('scope:scope:Atlas', {kind: 'scope'})), 'scope:Atlas');
      assert.equal(recordId(external), 'scope:Atlas');
      activateNode(external, false); assert.equal(calls.at(-1).id, 'scope:Atlas');
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
      ui$mode = 'list'; ui$selectedStatuses = new Set(presentStatuses);
      const articles = inventoryNodes();
      assert.equal(articles.length, fixture.count);
      assert(articles.every(node => node.catalog === 'module' && node.result === false));
      assert(!articles.some(node => ['catalogs', 'catalogs/note', 'catalogs/source'].includes(node.id)));
      assert(!articles.some(node => node.id.startsWith('chapter/')), 'Scoped inventory must not leak other results');
      renderInventory(true);
      assert.equal(dom$inventory.querySelector('h2').textContent, 'Statements & articles');
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


def test_a_deep_trail_keeps_first_parent_and_current_and_names_what_the_ellipsis_hides() -> None:
    _run(r"""
      const crumb = (label, url) => ({label, url});
      const trail = [crumb('ATLAS roadmap', 'https://example.test/math/roadmap/index.html'),
        crumb('Algebra', 'https://example.test/math/roadmap/algebra/index.html'),
        crumb('MIT RES.18-011', 'https://example.test/math/roadmap/mit/index.html'),
        crumb('Crystallographic Restriction', 'https://example.test/math/roadmap/crystal/index.html'),
        crumb('Theorem (Crystallographic Restriction)')];
      const items = trailList(trail).children;
      assert.deepEqual(items.map(item => item.title), ['ATLAS roadmap', 'Algebra / MIT RES.18-011',
        'Crystallographic Restriction', 'Theorem (Crystallographic Restriction)']);
      assert.equal(items[1].text, '…');
      assert.equal(items[1].children[0].className, 'bp-dag-sr-only', 'Screen readers hear the hidden ancestors');
      assert.equal(items[1].children[0].textContent, 'Algebra / MIT RES.18-011');
      assert.equal(items[1].getAttribute('aria-current'), null);
      assert.equal(items[3].getAttribute('aria-current'), 'page');
      assert.equal(items[2].children[0].href, trail[3].url);
      // Three entries fit.
      assert.deepEqual(trailList(trail.slice(2)).children.map(item => item.title), trail.slice(2).map(entry => entry.label));
      // A trail that fits its row is drawn whole; the fold is only for one that does not.
      const whole = trailList(trail, null, true).children;
      assert.deepEqual(whole.map(item => item.title), trail.map(entry => entry.label));
      assert.equal(whole[4].getAttribute('aria-current'), 'page');
    """)


def test_a_statement_is_selected_from_the_index_whatever_view_its_page_carries() -> None:
    _run(r"""
      // A statement placed directly under the roadmap is published with the project's view, which lists only topics.
      const root = scope('roadmap', {parent: undefined}), chapter = scope('chapter');
      const top = Object.assign(leaf('top'), {scope: 'roadmap', parent: 'roadmap'});
      configure([root, chapter, top, leaf('A')], graph([viewNode('scope:chapter', {kind: 'scope'}), viewNode('A')]));
      adoptGraph(contextGraph);
      assert.equal(byId.has('top'), false);
      assert.strictEqual(selection('top'), records.get('top'));
      // A statement the page lists is still the index's record, and a topic is never the selection.
      assert.strictEqual(selection('A'), records.get('A'));
      assert.strictEqual(selection('chapter'), null);
      assert.strictEqual(selection(undefined), null);
      // Before the index loads, the page's own entries decide.
      records = new Map(); sharedIndex = null; adoptGraph(graph([viewNode('B')]));
      assert.equal(selection('B').id, 'B');
      assert.strictEqual(selection('top'), null);
    """)

