"""Execute the real engine, fit and overlay functions on the published cards, with DOM seams stubbed."""

from __future__ import annotations

import json
import re
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
const constants = source.slice(source.indexOf('  var NODE_W ='), source.indexOf('  var clients =')) +
  source.slice(source.indexOf('  var clamp ='), source.indexOf('  function element('));
const functions = ['layoutGraph', 'wikiRoutes', 'geometry', 'compactRegions', 'sceneFrame', 'sceneDetours', 'framingNodes', 'framingViewport',
  'framingPadding', 'currentOverviewGroup', 'world', 'screen', 'inViewport', 'fit', 'mapHeight', 'freeArea',
  'center', 'bandInsets', 'updateOverlays'].map(original).join('\n');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const sandbox = {assert, fixture}; vm.createContext(sandbox);
const setup = String.raw`
var navigation = {}, mapAllowed = true, atlas = false, family = 'sans-serif', data, byId, ui$active, ui$activeIds;
var ui$selected = null, ui$orientation = 'lr', ui$area = null, ui$hovered = null, scene, sceneData, sceneDirection, sceneCompact, sceneFamily;
var sceneAspect, sceneWiki, sceneReduced, restoring = false, ui$readableCrop = false, ui$overviewGroup = null, dom$fit = null;
var ui$transform = {x: 0, y: 0, scale: 1}, overlays = new Map(), document = {activeElement: null};
var host = {classList: {contains() { return false; }}}, prerequisites = new Map(), dependents = new Map();
var dom$stage = {clientWidth: fixture.width, clientHeight: fixture.height}, dom$density = {hidden: true, textContent: ''}, dom$inspector = {};
var dom$nodeLayer = {appendChild() {}};
function calculateActive() {} function sheetLayout() { return fixture.width <= 720; } function schedule() {} function remember() {}
function colors() { return {}; } function statusRing() { return ''; }
function makeNodeControl(node) { return {style: {setProperty() {}}, dataset: {}, querySelector() { return null; }, remove() {},
  label: node.title, getAttribute() { return this.label; }, setAttribute(name, value) { this.label = value; }, removeAttribute() {}}; }
`;
const run = String.raw`data = {nodes: fixture.nodes.map((node, index) => ({id: node.id, title: node.id, isolated: node.isolated, _index: index,
  status: 'planned', width: node.layout.width, height: Math.max(100, node.layout.height)})), edges: fixture.edges};
// As resize() does: a narrow stage lays the ranks out top to bottom.
ui$orientation = fixture.width <= 620 ? 'tb' : 'lr';
byId = new Map(data.nodes.map(node => [node.id, node]));
ui$active = data.nodes; ui$activeIds = new Set(data.nodes.map(node => node.id));
fit();
var visible = ui$active.filter(node => inViewport(screen(node), 80));
updateOverlays(visible, null);
var boxes = Object.fromEntries(data.nodes.map(node => [node.id, screen(node)]));
var worlds = Object.fromEntries(data.nodes.map(node => [node.id, world(node)]));
JSON.stringify({scale: ui$transform.scale, boxes, worlds, hidden: dom$density.hidden, note: dom$density.textContent});
`;
process.stdout.write(vm.runInContext(constants + setup + functions + '\n' + run, sandbox));
"""


def _first_view(view: GraphView, width: int, height: int) -> dict:
    payload_nodes = []
    dimensions = dag_viewer._dimensions(view)
    linked = {endpoint for edge in view.edges for endpoint in (edge.source, edge.target)}
    for node in view.nodes:
        payload_nodes.append({"id": node.id, "isolated": node.id not in linked, "layout": dimensions[node.id]})
    edges = [{"source": edge.source, "target": edge.target, "statement": edge.statement_count, "proof": 0} for edge in view.edges]
    script = _HARNESS.replace("CLIENT", json.dumps(str(_CLIENT)), 1)
    completed = subprocess.run(
        [_NODE, "-e", script],
        input=json.dumps({"nodes": payload_nodes, "edges": edges, "width": width, "height": height}),
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


def _centred(box: dict, width: int, height: int) -> bool:
    return abs(box["x"] + box["w"] / 2 - width / 2) < 1e-6 and abs(box["y"] + box["h"] / 2 - height / 2) < 1e-6


def test_a_dense_chapter_frames_its_linked_cards_and_says_part_of_it_is_hidden() -> None:
    """A 63-item chapter opened on an unlinked card, and its note asked for a pan whatever the map showed."""
    chain = ["foundation", "middle", "target"]
    # The unlinked cards come first, so a fit of the whole map would centre one of them.
    view = _view(
        [*(f"auxiliary-{index}" for index in range(60)), *chain], [("foundation", "middle"), ("middle", "target")]
    )
    width, height = 1152, 847
    result = _first_view(view, width, height)
    boxes = result["boxes"]
    # Unlinked cards pack apart from the chain, so the linked cards open readable and whole.
    assert result["scale"] >= 0.9
    assert all(_inside(boxes[node], width, height) for node in chain)
    assert not _inside(boxes["auxiliary-0"], width, height)
    # Cards really are off screen, so the note says so and names the way to all of them.
    assert result["hidden"] is False
    # It counts the cards in view against all of them, and promises no Browse total, since Browse lists statements, not cards.
    assert re.fullmatch(r"Drag to see more: \d+ of 63 cards in view\.", result["note"])


@pytest.mark.parametrize(("width", "scale"), ((900, None), (740, 0.9)))
def test_a_map_that_fits_shows_every_card_and_no_pan_note(width: int, scale: float | None) -> None:
    """The note told readers to pan when every card was already on screen."""
    # At 740px the map fits no higher than about the .9 floor, and still opens whole and centred instead of a card being centred.
    view = _view(["a", "b", "c", "loose"], [("a", "b"), ("b", "c")])
    height = 600
    result = _first_view(view, width, height)
    if scale is None:
        assert result["scale"] > 0.9
    else:
        assert scale <= result["scale"] < 1
    assert all(_inside(box, width, height) for box in result["boxes"].values())
    assert result["hidden"] is True and result["note"] == ""


def test_a_phone_map_that_fits_at_the_floor_shows_its_unlinked_cards_too() -> None:
    """On a phone the whole map fits at or above the .9 floor, yet framing only the pair would push the unlinked card off."""
    view = _view(["a", "b", "loose"], [("a", "b")])
    width, height = 430, 800
    result = _first_view(view, width, height)
    assert result["scale"] >= 0.9
    assert all(_inside(box, width, height) for box in result["boxes"].values())
    assert result["hidden"] is True and result["note"] == ""
    # A narrower phone cannot show all three, so it frames the linked pair and says the rest is off screen.
    narrow = _first_view(view, 390, 560)
    assert _inside(narrow["boxes"]["a"], 390, 560) and _inside(narrow["boxes"]["b"], 390, 560)
    assert not _inside(narrow["boxes"]["loose"], 390, 560)
    assert narrow["hidden"] is False and narrow["note"] == "Drag to see more: 2 of 3 cards in view."


def test_a_long_chain_opens_at_its_leading_edge() -> None:
    """A long chain cannot open readable. Centring its first card left half the stage empty; the view now starts at the chain's
    leading edge, readable, and the note says the rest is off screen."""
    ids = [f"step-{index}" for index in range(33)]
    view = _view(ids, list(zip(ids, ids[1:])))
    width, height = 1152, 847
    result = _first_view(view, width, height)
    boxes = result["boxes"]
    assert result["scale"] == pytest.approx(0.9)
    # It starts 20px in, the framing's side padding, with the first card whole.
    assert boxes["step-0"]["x"] == pytest.approx(20) and _inside(boxes["step-0"], width, height)
    assert result["hidden"] is False and re.fullmatch(r"Drag to see more: \d+ of 33 cards in view\.", result["note"])


@pytest.mark.parametrize(("width", "height"), ((1152, 847), (390, 560)))
def test_a_deep_map_whose_leading_corner_is_bare_opens_on_its_first_card(width: int, height: int) -> None:
    """Hatcher's largest chapter opened, and re-fit, on a leading corner crossed by lines with no whole card in it.
    A short chain into a tall fan leaves that corner bare; the first card in reading order now opens there instead."""
    chain = [f"c{index}" for index in range(6)]
    fan = [f"f{index}" for index in range(24)]
    view = _view([*chain, *fan], [*zip(chain, chain[1:]), *((chain[-1], leaf) for leaf in fan)])
    result = _first_view(view, width, height)
    boxes = result["boxes"]
    assert result["scale"] == pytest.approx(0.9)
    assert _inside(boxes["c0"], width, height)
    assert result["hidden"] is False

def test_the_workspace_map_starts_below_its_title() -> None:
    """The map title is drawn from 20px to about 37px, and a height-limited map started at 36px, under it. The header band is
    44px and the framing starts cards 48px down."""
    view = _view(["hub", *(f"leaf-{index}" for index in range(5))], [("hub", f"leaf-{index}") for index in range(5)])
    width, height = 900, 800
    result = _first_view(view, width, height)
    assert result["scale"] > 0.9
    assert min(box["y"] for box in result["boxes"].values()) == pytest.approx(48)
    # The foot band under the pan note and key is 56px, and the framing keeps the cards above it.
    assert max(box["y"] + box["h"] for box in result["boxes"].values()) == pytest.approx(height - 56)
    assert all(_inside(box, width, height) for box in result["boxes"].values())
    assert result["hidden"] is True
