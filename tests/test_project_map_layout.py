"""Check the multiscale map layout's geometry through the production Node module."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_LAYOUT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/project-map-layout.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for map layout tests")

_HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const context = {window: {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(LAYOUT, 'utf8'), context, {timeout: 2000});
const L = context.window.AutoformMapLayout, {MIN_W, MIN_H, GAP} = L.constants;
const DESKTOP = {w: 900, h: 780}, PHONE = {w: 390, h: 700}, EPS = 1e-6;
function summary(id, results) {
  return {id, title: 'Part ' + id, results, status: {}, parts: 0, api: [], api_total: 0, imports_total: 0, file: id + '.json'};
}
// A deterministic spread of sizes, like a book's chapters: a few large, many small.
function sizes(count, largest) { return Array.from({length: count}, (_, k) => Math.max(1, Math.round(largest / (1 + k * k / 3)))); }
function inside(inner, outer) {
  return inner.x >= outer.x - EPS && inner.y >= outer.y - EPS &&
    inner.x + inner.w <= outer.x + outer.w + EPS && inner.y + inner.h <= outer.y + outer.h + EPS;
}
function overlap(a, b) {
  return Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x) > EPS && Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y) > EPS;
}
function disjoint(boxes) {
  const list = Object.values(boxes);
  list.forEach((a, i) => list.slice(i + 1).forEach(b => assert(!overlap(a, b), JSON.stringify([a, b]))));
}
function readingOrder(boxes) {
  // Rows (or columns) first, then position along them.
  return Object.keys(boxes).sort((p, q) => boxes[p].row - boxes[q].row ||
    (boxes[p].axis === 'columns' ? boxes[p].y - boxes[q].y : boxes[p].x - boxes[q].x));
}
// An axis-aligned segment crosses a box's interior.
function crosses(x1, y1, x2, y2, box) {
  const lo = (a, b) => Math.min(a, b), hi = (a, b) => Math.max(a, b);
  return hi(x1, x2) > box.x + EPS && lo(x1, x2) < box.x + box.w - EPS && hi(y1, y2) > box.y + EPS && lo(y1, y2) < box.y + box.h - EPS;
}
function onBorder(x, y, box) {
  const inX = x >= box.x - EPS && x <= box.x + box.w + EPS, inY = y >= box.y - EPS && y <= box.y + box.h + EPS;
  return (inX && (Math.abs(y - box.y) < EPS || Math.abs(y - box.y - box.h) < EPS)) ||
    (inY && (Math.abs(x - box.x) < EPS || Math.abs(x - box.x - box.w) < EPS));
}
function checkRoutes(routes, boxes) {
  routes.forEach(route => {
    const p = route.points, n = p.length;
    assert(n >= 4 && n % 2 === 0, JSON.stringify(route));
    assert(onBorder(p[0], p[1], boxes[route.from]) && onBorder(p[n - 2], p[n - 1], boxes[route.to]), JSON.stringify(route));
    for (let k = 0; k + 3 < n; k += 2) {
      assert(Math.abs(p[k] - p[k + 2]) < EPS || Math.abs(p[k + 1] - p[k + 3]) < EPS, 'Routes run straight along gaps');
      for (const [id, box] of Object.entries(boxes)) {
        assert(!crosses(p[k], p[k + 1], p[k + 2], p[k + 3], box), route.from + ' -> ' + route.to + ' crosses ' + id);
      }
    }
  });
}
"""


def _run(body: str) -> None:
    script = "const LAYOUT=" + json.dumps(str(_LAYOUT)) + ";\n" + _HARNESS + body
    completed = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-6000:]


def test_opening_regions_are_readable_on_desktop_and_phone_and_repeatable() -> None:
    _run(r"""
    const regions = sizes(17, 448).map((results, k) => summary('r' + k, results));
    const index = {regions, region_edges: [['r3', 'r0', 4, 2], ['r9', 'r3', 12, 0], ['r16', 'r9', 0, 5]]};
    for (const stage of [DESKTOP, PHONE]) {
      const packed = L.packLevels(index, stage);
      assert.deepEqual(JSON.parse(JSON.stringify(L.packLevels(index, stage))), JSON.parse(JSON.stringify(packed)));
      assert.equal(packed.root.w, stage.w); assert(packed.root.h >= stage.h);
      const boxes = packed.boxes;
      assert.equal(Object.keys(boxes).length, 17);
      for (const box of Object.values(boxes)) {
        assert(inside(box, packed.content));
        assert(box.w >= 0.99 * MIN_W && box.h >= 0.99 * MIN_H, JSON.stringify(box));
      }
      disjoint(boxes);
    }
    // Seventeen readable regions do not fit one phone screen, so the phone's map scrolls down.
    assert(L.packLevels(index, PHONE).root.h > PHONE.h);
    assert.equal(L.packLevels(index, DESKTOP).root.h, DESKTOP.h);
    """)


def test_prerequisites_come_first_and_ties_keep_the_payload_order() -> None:
    _run(r"""
    // d uses c uses b uses a, listed last-first; e and f are unlinked and keep their order.
    const regions = ['d', 'e', 'c', 'f', 'b', 'a'].map(id => summary(id, 40));
    const edges = [['d', 'c', 3, 1], ['c', 'b', 2, 0], ['b', 'a', 0, 4]];
    const order = L.packLevels({regions, region_edges: edges}, DESKTOP).boxes;
    const reading = readingOrder(order);
    const at = id => reading.indexOf(id);
    assert(at('a') < at('b') && at('b') < at('c') && at('c') < at('d'), reading.join());
    assert(at('e') < at('f'), reading.join());
    // A pair linked both ways puts first the side the heavier links rest on.
    const pair = L.dependencyOrder([summary('x', 1), summary('y', 1)].map(s => ({id: s.id, weight: 1})),
      [['x', 'y', 9, 0], ['y', 'x', 1, 0]]);
    assert.deepEqual(pair, ['y', 'x']);
    """)


def test_children_fill_their_parent_without_moving_it() -> None:
    _run(r"""
    const regions = sizes(6, 300).map((results, k) => summary('r' + k, results));
    const packed = L.packLevels({regions, region_edges: []}, DESKTOP), parent = packed.boxes.r0;
    const before = JSON.stringify(parent);
    const children = sizes(12, 60).map((results, k) => summary('r0/c' + k, results));
    const part = {part: summary('r0', 300), children, edges: [['r0/c5', 'r0/c1', 2, 0]],
      results: [{id: 'r0-own-1'}, {id: 'r0-own-2'}]};
    const kids = L.packPart(part, parent, DESKTOP);
    assert.equal(JSON.stringify(parent), before);
    assert.equal(JSON.stringify(L.packPart(part, parent, DESKTOP)), JSON.stringify(kids));
    assert.equal(Object.keys(kids).length, 13);
    // Beside other children, the own-results box keeps a band for its title, and its cards sit below it.
    const own = kids['r0#results'];
    assert(own && own.own && own.band > 0);
    const ownView = L.layoutCommunity(part.results.map(r => ({id: r.id, title: r.id})), [], [], own);
    for (const card of Object.values(ownView.cards)) assert(inside(card, L.contentOf(own)));
    const content = L.contentOf(parent);
    for (const box of Object.values(kids)) { assert(inside(box, content)); assert.equal(box.depth, parent.depth + 1); }
    disjoint(kids);
    // A part holding only results gives them its whole content.
    const leaf = L.packPart({part: summary('r1', 2), children: [], results: [{id: 'a'}, {id: 'b'}]}, packed.boxes.r1, DESKTOP);
    const only = leaf['r1#results'], inner = L.contentOf(packed.boxes.r1);
    assert(only.own && only.x === inner.x && only.y === inner.y && only.w === inner.w && only.h === inner.h);
    """)


def test_a_flat_part_with_one_dominant_child_keeps_every_box_in_shape() -> None:
    _run(r"""
    const parent = {x: 0, y: 0, w: 700, h: 190, unit: 0.75, band: 52, depth: 2};
    const children = [summary('big', 7680), summary('mid', 1635)].concat(sizes(30, 400).map((r, k) => summary('s' + k, r)));
    const kids = L.packPart({part: summary('p', 9000), children, edges: []}, parent, DESKTOP);
    const content = L.contentOf(parent);
    for (const box of Object.values(kids)) {
      assert(inside(box, content));
      assert(box.w > 0 && box.h > 0 && Math.max(box.w / box.h, box.h / box.w) < 8, JSON.stringify(box));
    }
    disjoint(kids);
    """)


def test_every_part_at_every_depth_keeps_room_for_what_it_holds() -> None:
    _run(r"""
    // A deep, skewed library: wide and flat parents, one dominant child each, results at every depth.
    let seq = 0;
    function make(id, depth) {
      const count = depth >= 6 ? 0 : [12, 9, 7, 5, 4, 3][depth];
      const children = Array.from({length: count}, (_, k) => make(id + '/' + k, depth + 1));
      const own = depth >= 6 || seq++ % 3 === 0 ? Array.from({length: 1 + (seq % 9)}, (_, k) => ({id: id + '#' + k, title: 'r'})) : [];
      const results = own.length + children.reduce((total, child) => total + child.summary.results, 0);
      const edges = children.slice(1).map((child, k) => [child.summary.id, children[k].summary.id, 1, 0]);
      const resultEdges = own.slice(1).map((row, k) => [row.id, own[k].id, 1, 0]);
      return {summary: summary(id, results), data: {part: summary(id, results), children: children.map(c => c.summary),
        edges, results: own, result_edges: resultEdges, boundary_edges: []}, children};
    }
    const regions = Array.from({length: 5}, (_, k) => make('r' + k, 1));
    for (const stage of [DESKTOP, PHONE]) {
      const top = L.packLevels({regions: regions.map(r => r.summary), region_edges: []}, stage);
      const queue = regions.map(r => [r, top.boxes[r.summary.id]]);
      let parts = 0, cards = 0;
      while (queue.length) {
        const [node, box] = queue.shift();
        const content = L.contentOf(box);
        assert(content.w > 0 && content.h > 0, node.summary.id + ' ' + JSON.stringify(box));
        // Fitted to the stage, a part always shows its own label.
        assert(L.placeLabels({[node.summary.id]: box}, L.fitCamera(box, stage), stage).length === 1, node.summary.id);
        const kids = L.packPart(node.data, box, stage);
        const own = kids[node.summary.id + '#results'];
        if (own) {
          assert(own.w > 0 && own.h > 0);
          const view = L.layoutCommunity(node.data.results, node.data.result_edges, [], own);
          for (const row of node.data.results) { const card = view.cards[row.id]; assert(card.w > 0 && card.h > 0 && inside(card, own)); cards++; }
        }
        node.children.forEach(child => queue.push([child, kids[child.summary.id]]));
        parts++;
      }
      assert(parts > 1000 && cards > 1000, parts + ' ' + cards);
    }
    """)


def test_routes_run_only_through_gaps_and_merge_both_directions() -> None:
    _run(r"""
    const axes = new Set();
    for (const parent of [{x: 0, y: 0, w: 900, h: 780, unit: 1, band: 52, depth: 1},
                          {x: 0, y: 0, w: 1400, h: 260, unit: 1, band: 52, depth: 1}]) {
      const children = sizes(14, 90).map((results, k) => summary('c' + k, results));
      const edges = [];
      for (let k = 1; k < 14; k++) edges.push(['c' + k, 'c' + ((k * 5) % k), k % 3, 1]);
      edges.push(['c2', 'c9', 3, 0], ['c9', 'c2', 1, 0], ['c13', 'c0', 0, 1], ['c4', 'c11', 2, 2]);
      const kids = L.packPart({part: summary('p', 1000), children, edges}, parent, DESKTOP);
      const content = L.contentOf(parent), routes = L.bundleEdges(kids, edges, content);
      checkRoutes(routes, kids);
      axes.add(Object.values(kids)[0].axis);
      // No route runs through the parent's band, above its content.
      for (const route of routes) for (let k = 1; k < route.points.length; k += 2) assert(route.points[k] >= content.y - EPS, JSON.stringify(route));
      const pairs = new Set(edges.filter(e => e[0] !== e[1]).map(e => [e[0], e[1]].sort().join()));
      assert.equal(routes.length, pairs.size);
      // c2 uses c9 three times and c9 uses c2 once: one route, from c9 to its heavier user.
      const both = routes.find(r => [r.from, r.to].sort().join() === 'c2,c9');
      assert(both.both && both.from === 'c9' && both.to === 'c2', JSON.stringify(both));
      assert.deepEqual(both.weights, [3, 0, 1, 0]);
      // In a small set every linked box has a route on the map at rest.
      for (const id of Object.keys(kids)) {
        const mine = routes.filter(r => r.from === id || r.to === id);
        if (mine.length) assert(mine.some(r => r.rest), id);
      }
      assert(routes.filter(r => r.rest).length <= Math.round(14 * 0.8));
    }
    assert(axes.has('rows') && axes.has('columns'), [...axes].join());
    // A dense island shows only its central routes at rest; the others wait for their parts.
    const island = sizes(40, 60).map((results, k) => summary('i' + k, results)), links = [];
    for (let a = 0; a < 40; a++) for (let b = 0; b < a; b += 3) links.push(['i' + a, 'i' + b, 1 + (a + b) % 4, 1]);
    const room = {x: 0, y: 0, w: 900, h: 780, unit: 1, band: 52, depth: 1};
    const packed40 = L.packPart({part: summary('island', 2000), children: island, edges: links}, room, DESKTOP);
    const dense = L.bundleEdges(packed40, links, L.contentOf(room));
    checkRoutes(dense, packed40);
    const resting = dense.filter(r => r.rest);
    assert(resting.length >= 10 && resting.length <= 18, resting.length);
    assert(Math.min(...resting.map(r => r.weight)) >= Math.max(...dense.filter(r => !r.rest).map(r => r.weight)) - 4);
    """)


def test_result_view_puts_prerequisites_above_and_hides_implied_links() -> None:
    _run(r"""
    const results = ['c', 'b', 'a', 'd', 'e', 'f'].map(id => ({id, title: id}));
    // c uses b uses a, c also uses a directly; d and e use each other; f stands alone.
    const edges = [['c', 'b', 1, 0], ['b', 'a', 1, 0], ['c', 'a', 1, 0], ['d', 'e', 1, 0], ['e', 'd', 0, 1]];
    const box = {x: 10, y: 20, w: 520, h: 600, unit: 1};
    const view = L.layoutCommunity(results, edges, [['c', 'elsewhere', 1, 0], ['outside', 'a', 0, 1]], box);
    const cards = view.cards;
    assert.equal(Object.keys(cards).length, 6);
    for (const card of Object.values(cards)) assert(inside(card, box));
    disjoint(cards);
    assert(!view.fold && cards.a.row < cards.b.row && cards.b.row < cards.c.row);
    assert.equal(cards.d.row, cards.e.row);
    const drawn = view.routes.map(r => [r.from, r.to].sort().join()), hidden = view.implied.map(r => [r.from, r.to].sort().join());
    assert(drawn.includes('a,b') && drawn.includes('b,c') && drawn.includes('d,e') && !drawn.includes('a,c'), drawn.join(' '));
    assert.deepEqual(hidden, ['a,c']);
    checkRoutes(view.routes, cards);
    assert.deepEqual(JSON.parse(JSON.stringify(view.stubs)), {c: {uses: 1, usedBy: 0}, a: {uses: 0, usedBy: 1}});
    // A long chain in a wide, short box flows into rows like text, still in reading order.
    const chain = Array.from({length: 20}, (_, k) => ({id: 'n' + k, title: 'n' + k}));
    const links = chain.slice(1).map((node, k) => [node.id, 'n' + k, 1, 0]);
    const wide = L.layoutCommunity(chain, links, [], {x: 0, y: 0, w: 1600, h: 300, unit: 1});
    assert(wide.fold);
    const order = Object.keys(wide.cards).sort((p, q) => wide.cards[p].row - wide.cards[q].row || wide.cards[p].x - wide.cards[q].x);
    assert.deepEqual(order, chain.map(node => node.id));
    checkRoutes(wide.routes, wide.cards);
    """)


def test_labels_appear_only_where_they_fit() -> None:
    _run(r"""
    const regions = sizes(5, 400).map((results, k) => summary('r' + k, results));
    const packed = L.packLevels({regions, region_edges: []}, DESKTOP);
    const opening = L.placeLabels(packed.boxes, {x: 0, y: 0, z: 1}, DESKTOP);
    assert.equal(opening.length, 5);
    const children = sizes(8, 40).map((results, k) => summary('r0/c' + k, results));
    const kids = L.packPart({part: summary('r0', 400), children, edges: []}, packed.boxes.r0, DESKTOP);
    const far = L.placeLabels(kids, {x: 0, y: 0, z: 1}, DESKTOP);
    const near = L.placeLabels(kids, L.fitCamera(L.contentOf(packed.boxes.r0), DESKTOP), DESKTOP);
    const full = labels => labels.filter(label => !label.compact).length;
    assert(full(near) > full(far));
    for (const label of near) {
      const box = kids[label.id], camera = L.fitCamera(L.contentOf(packed.boxes.r0), DESKTOP);
      const left = (box.x - camera.x) * camera.z, top = (box.y - camera.y) * camera.z;
      assert(label.x >= left - EPS && label.y >= top - EPS && label.x + label.w <= left + box.w * camera.z + EPS);
      assert(label.w >= 0.9 * MIN_W - 2 * 12 * box.unit * camera.z - EPS);
    }
    // A box too narrow for a full label but at least 60 px wide still gets a compact one.
    const narrow = {x: 0, y: 0, w: 100, h: 90, unit: 1, band: 52, pad: 12, depth: 2};
    const [named] = L.placeLabels({narrow}, {x: 0, y: 0, z: 1}, DESKTOP);
    assert(named && named.compact && named.detail === 'title', JSON.stringify(named));
    assert.equal(L.placeLabels({tiny: Object.assign({}, narrow, {w: 50})}, {x: 0, y: 0, z: 1}, DESKTOP).length, 0);
    // Opening a crowded part comes closer than its fit, at most twice, until most children are labelled.
    const crowd = Array.from({length: 60}, (_, k) => summary('r1/c' + k, 10 + k % 7));
    const many = L.packPart({part: summary('r1', 300), children: crowd, edges: []}, packed.boxes.r1, DESKTOP);
    const fit = L.fitCamera(L.contentOf(packed.boxes.r1), DESKTOP), focus = L.focusCamera(packed.boxes.r1, many, DESKTOP);
    assert(focus.z >= fit.z - EPS && focus.z <= 2 * fit.z + EPS);
    assert(full(L.placeLabels(many, focus, DESKTOP)) > full(L.placeLabels(many, fit, DESKTOP)));
    // A part whose children are readable at its fit opens fitted.
    assert.deepEqual(L.focusCamera(packed.boxes.r0, kids, DESKTOP), L.fitCamera(L.contentOf(packed.boxes.r0), DESKTOP));
    """)
