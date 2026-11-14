"""Local dependency connections must leave unrelated result cards readable."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_routes_keep_every_dependency_and_avoid_intermediate_cards() -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("AUTOFORM_REQUIRE_NODE_TESTS") == "1":
            pytest.fail("Node is required for local graph routing")
        pytest.skip("Node is not installed")
    module = Path(__file__).resolve().parents[2] / "autoform_cli/ui/assets/project-map-layout.js"
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const context = {window: {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const boxes = {};
for (let column = 0; column < 5; column++) {
  for (let row = 0; row < 5; row++) {
    boxes[column + ':' + row] = {x: column * 296, y: row * 84, w: 200, h: 64};
  }
}
const input = [
  {key: 'long-straight', from: '0:2', to: '4:2', rest: true},
  {key: 'long-diagonal', from: '0:0', to: '4:4'},
  {key: 'reverse', from: '4:0', to: '1:4'},
  {key: 'same-column', from: '2:0', to: '2:4'},
  {key: 'neighbours', from: '1:1', to: '1:2'},
];
const routes = context.window.AutoformMapLayout.routeColumns(input, boxes);
assert.deepStrictEqual(JSON.parse(JSON.stringify(routes.map(route => route.key))), input.map(route => route.key));
function intersects(points, box) {
  for (let i = 2; i < points.length; i += 2) {
    const x0 = points[i - 2], y0 = points[i - 1], x1 = points[i], y1 = points[i + 1];
    assert(x0 === x1 || y0 === y1, 'route must be orthogonal');
    if (x0 === x1 && x0 > box.x && x0 < box.x + box.w &&
        Math.max(y0, y1) > box.y && Math.min(y0, y1) < box.y + box.h) return true;
    if (y0 === y1 && y0 > box.y && y0 < box.y + box.h &&
        Math.max(x0, x1) > box.x && Math.min(x0, x1) < box.x + box.w) return true;
  }
  return false;
}
for (const route of routes) {
  for (const [id, box] of Object.entries(boxes)) {
    if (id !== route.from && id !== route.to) {
      assert(!intersects(route.points, box), route.key + ' crosses ' + id);
    }
  }
}
assert.strictEqual(context.window.AutoformMapLayout.routeColumns([], boxes).length, 0);
// Two cards side by side in one row: the link between them is one straight segment, with no step where its two ends
// face each other across the gap.
const pair = {a: {x: 0, y: 0, w: 200, h: 64}, b: {x: 296, y: 0, w: 200, h: 64}};
const [straight] = context.window.AutoformMapLayout.routeColumns([{key: 'pair', from: 'a', to: 'b'}], pair);
assert.deepStrictEqual(JSON.parse(JSON.stringify(straight.points)), [200, 32, 296, 32]);
// Six cards in a column link into one card beside it, sideFor(6, 20) tall: the links end on its side in the order of
// the cards they come from, so no two cross at it, and at least 20 apart. The ends are spread 25 apart, from -62.5 to
// 62.5, and the third card's link leaves it at -37.6, just above the second link's end, so the sweep moves that end a
// lane down, to -32.6.
const layout = context.window.AutoformMapLayout, side = layout.sideFor(6, 20);
assert(Math.abs(side - 250) < 1e-9, String(side));
const fan = {focus: {x: 296, y: -side / 2, w: 300, h: side}};
for (let i = 0; i < 6; i++) fan['u' + i] = {x: 0, y: (i - 2) * 84 - 37.6 - 32, w: 200, h: 64};
const into = layout.routeColumns([0, 1, 2, 3, 4, 5].map(i => ({key: 'u' + i, from: 'u' + i, to: 'focus', rest: true})), fan);
const ends = into.map(route => route.points[route.points.length - 1]);
assert(Math.abs(ends[1] + 32.6) < 1e-9, 'ends ' + ends.join());
ends.slice(1).forEach((y, i) => assert(y - ends[i] >= 20 - 1e-9, 'ends ' + ends.join()));
into.forEach(route => assert.strictEqual(route.points[route.points.length - 2], 296, route.key + ' ends on the left side'));
console.log('local routes preserve dependencies and avoid cards');
"""
    result = subprocess.run([node, "-e", script, str(module)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "preserve dependencies and avoid cards" in result.stdout

