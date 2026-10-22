"""Execute the real fit and overlay functions on the published layout, with DOM seams stubbed."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoform_cli import dag_viewer
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for first-view tests")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"

_HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(CLIENT, 'utf8');
function original(name) {
  const start = source.indexOf('function ' + name + '(');
  assert(start >= 0, 'Production function missing: ' + name);
  for (let end = source.indexOf('}', start); end >= 0; end = source.indexOf('}', end + 1)) {
    const declaration = source.slice(start, end + 1);
    try { new vm.Script('"use strict"; (' + declaration + ');'); return declaration; }
    catch (error) { if (!(error instanceof SyntaxError)) throw error; }
  }
  assert.fail('Production function did not parse: ' + name);
}
const constants = source.match(/\bvar NODE_W =[^;]+;/)[0] + source.match(/\bvar SEARCH_PAGE =[^;]+;/)[0] + source.match(/\bvar clamp = [^}]+\};/)[0];
const functions = ['world', 'screen', 'fit', 'updateOverlays', 'mapHeight'].map(original).join('\n');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const sandbox = {assert, fixture}; vm.createContext(sandbox);
const setup = String.raw`
var navigation = Boolean(fixture.navigation), atlas = false, data, byId, ui$active, ui$activeIds, ui$selected = null, ui$orientation = 'lr', ui$area = null, ui$hovered = null;
var ui$transform = {x: 0, y: 0, scale: 1}, mobileIsolateBase = 0, overlays = new Map(), document = {activeElement: null};
var host = {classList: {contains() { return false; }}}, prerequisites = new Map(), dependents = new Map();
var dom$stage = {clientWidth: fixture.width, clientHeight: fixture.height}, dom$density = {hidden: true, textContent: ''};
var dom$nodeLayer = {style: {setProperty(name, value) { this[name] = value; }}, appendChild() {}};
function calculateActive() {} function compactRegions() { return false; } function geometry() { return null; }
function sheetLayout() { return false; } function freeArea() { return {}; } function center() {} function schedule() {} function remember() {}
function colors() { return {}; } function statusRing() { return ''; }
function makeNodeControl() { return {style: {setProperty() {}}, dataset: {}, querySelector() { return null; }, remove() {}}; }
`;
const run = String.raw`data = {nodes: fixture.nodes.map(node => Object.assign({}, node, {x: node.layout.x, y: node.layout.y, width: node.layout.width, height: node.layout.height, column: node.layout.column, row: node.layout.row}))};
// As resize() and adoptGraph() do: a narrow stage stacks the ranks, with the unlinked cards in a grid below.
ui$orientation = fixture.width <= 620 ? 'tb' : 'lr'; var isolateIndex = 0;
data.nodes.forEach(node => { node._isolateIndex = node.isolated ? isolateIndex++ : -1; });
mobileIsolateBase = (Math.max(-2, ...data.nodes.filter(node => !node.isolated).map(node => node.column)) + 2) * (NODE_H + 72);
byId = new Map(data.nodes.map(node => [node.id, node]));
ui$active = data.nodes; ui$activeIds = new Set(data.nodes.map(node => node.id));
fit();
var visible = ui$active.filter(node => { var box = screen(node); return box.x + box.w >= 0 && box.x <= fixture.width && box.y + box.h >= 0 && box.y <= fixture.height; });
updateOverlays(visible, null);
var boxes = Object.fromEntries(data.nodes.map(node => [node.id, screen(node)]));
JSON.stringify({scale: ui$transform.scale, boxes, hidden: dom$density.hidden, note: dom$density.textContent, k: dom$nodeLayer.style['--dag-k']});
`;
process.stdout.write(vm.runInContext(constants + setup + functions + '\n' + run, sandbox));
"""


def _first_view(view: GraphView, width: int, height: int, navigation: bool = False) -> dict:
    payload_nodes = []
    positions = dag_viewer._positions(view)
    linked = {endpoint for edge in view.edges for endpoint in (edge.source, edge.target)}
    for node in view.nodes:
        payload_nodes.append({"id": node.id, "isolated": node.id not in linked, "layout": positions[node.id]})
    script = _HARNESS.replace("CLIENT", json.dumps(str(_CLIENT)), 1)
    completed = subprocess.run(
        [_NODE, "-e", script],
        input=json.dumps({"nodes": payload_nodes, "width": width, "height": height, "navigation": navigation}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _view(node_ids: list[str], edges: list[tuple[str, str]]) -> GraphView:
    nodes = tuple(
        ViewNode(
            id=node_id,
            title=node_id,
            kind="node",
            members=(node_id,),
            status_counts=(("planned", 1),),
            declaration="lemma",
            status_key="planned",
        )
        for node_id in node_ids
    )
    return GraphView(
        kind="full",
        title="First view",
        nodes=nodes,
        edges=tuple(ViewEdge(source, target, statement_count=1) for source, target in edges),
    )


def _inside(box: dict, width: int, height: int) -> bool:
    return box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["w"] <= width and box["y"] + box["h"] <= height


def test_a_dense_chapter_opens_centred_on_its_chain_with_the_isolates_below() -> None:
    """A 63-item chapter opened with its 3-node chain in the top-left and ten clipped isolates at the right."""
    chain = ["foundation", "middle", "target"]
    view = _view(
        [*chain, *(f"auxiliary-{index}" for index in range(60))], [("foundation", "middle"), ("middle", "target")]
    )
    width, height = 1152, 847
    result = _first_view(view, width, height)
    boxes = result["boxes"]
    assert all(_inside(boxes[node], width, height) for node in chain)
    left, right = boxes["foundation"]["x"], boxes["target"]["x"] + boxes["target"]["w"]
    assert abs((left + right) / 2 - width / 2) < 1
    assert abs(boxes["middle"]["y"] + boxes["middle"]["h"] / 2 - height / 2) < 1
    chain_bottom = max(boxes[node]["y"] + boxes[node]["h"] for node in chain)
    isolates = [boxes[f"auxiliary-{index}"] for index in range(60)]
    assert min(box["y"] for box in isolates) > chain_bottom
    assert min(box["x"] for box in isolates) == pytest.approx(left)
    # Some isolates really are off screen, so the note says so and names the way to all of them.
    assert result["hidden"] is False
    assert result["note"] == "Showing part of the map. Drag to see more, or open Contents for all 63."
    assert result["k"] == result["scale"]


def test_a_map_that_fits_shows_every_card_and_no_pan_note() -> None:
    """The note told readers to pan when every card was already on screen."""
    # At the .8 floor the chain is wider than the stage less its padding but not wider than the stage.
    view = _view(["a", "b", "c", "loose"], [("a", "b"), ("b", "c")])
    width, height = 700, 600
    result = _first_view(view, width, height)
    assert result["scale"] == pytest.approx(0.8)
    assert all(_inside(box, width, height) for box in result["boxes"].values())
    assert result["hidden"] is True and result["note"] == ""


def test_a_phone_map_that_fits_at_the_floor_shows_its_unlinked_cards_too() -> None:
    """On a phone both cards fit at the .8 floor, yet the note said to pan; framing only the pair would push the rest off."""
    view = _view(["a", "b", "loose-1", "loose-2"], [("a", "b")])
    width, height = 390, 700
    result = _first_view(view, width, height)
    assert result["scale"] == pytest.approx(0.8)
    assert all(_inside(box, width, height) for box in result["boxes"].values())
    assert result["hidden"] is True and result["note"] == ""


def test_a_long_chain_crops_from_its_start_and_is_centred_across() -> None:
    """A cropped map was pinned to the top-left corner with the rest of the stage empty."""
    ids = [f"step-{index}" for index in range(33)]
    view = _view(ids, list(zip(ids, ids[1:])))
    width, height = 1152, 847
    result = _first_view(view, width, height)
    boxes = result["boxes"]
    assert result["scale"] == pytest.approx(0.8)
    assert boxes["step-0"]["x"] == pytest.approx(54)
    assert abs(boxes["step-0"]["y"] + boxes["step-0"]["h"] / 2 - height / 2) < 1
    assert result["hidden"] is False and result["note"].endswith("for all 33.")


def test_the_workspace_map_starts_below_its_title() -> None:
    """The map title is drawn from 20px to about 37px, and a height-limited map started at 36px, under it."""
    view = _view(["hub", *(f"leaf-{index}" for index in range(5))], [("hub", f"leaf-{index}") for index in range(5)])
    width, height = 900, 450
    result = _first_view(view, width, height, navigation=True)
    assert result["scale"] == pytest.approx((height - 56 - 36) / (4 * 80 + 56))
    assert min(box["y"] for box in result["boxes"].values()) == pytest.approx(56)
    assert all(_inside(box, width, height) for box in result["boxes"].values())
    assert result["hidden"] is True
