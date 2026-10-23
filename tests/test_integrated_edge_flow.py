"""Execute the production Canvas painter and relation-count helper in bounded Node cells.

Canvas calls are recorded, not replaced by a renderer. Genuine browser visual
verification remains separate; no physics, DOM cloning or animation is faked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is required for focused painter source checks")

_ENVIRONMENT = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(ROOT+'/autoform_cli/assets/blueprint-dag.js','utf8');
function original(name) {
  const start=source.indexOf('function '+name+'(');assert(start>=0);
  for(let end=source.indexOf('}',start);end>=0;end=source.indexOf('}',end+1)) {
    const body=source.slice(start,end+1);
    try {new vm.Script('('+body+')');return body;}catch(error){if(!(error instanceof SyntaxError))throw error;}
  }
  assert.fail('Production function did not parse: '+name);
}
const context={assert,fixture:JSON.parse(fs.readFileSync(0,'utf8'))};vm.createContext(context);
const helpers=['drawRoute','relationCounts'].map(original).join('\n');
"""

_CANVAS = r"""
class Canvas {
  constructor() {
    this.strokes=[];this.fills=[];this.commands=[];this.dash=[];this.stack=[];
    this.globalAlpha=.8;this.lineWidth=9;this.strokeStyle='#111111';this.fillStyle='#222222';
    this.lineCap='butt';this.lineJoin='miter';this.font='17px serif';
  }
  state() {return {globalAlpha:this.globalAlpha,lineWidth:this.lineWidth,strokeStyle:this.strokeStyle,
    fillStyle:this.fillStyle,lineCap:this.lineCap,lineJoin:this.lineJoin,font:this.font,dash:this.dash.slice()};}
  save() {this.stack.push(this.state());}
  restore() {Object.assign(this,this.stack.pop());}
  beginPath() {this.commands=[];}
  moveTo(...args) {this.commands.push(['move',...args]);}
  lineTo(...args) {this.commands.push(['line',...args]);}
  quadraticCurveTo(...args) {this.commands.push(['quadratic',...args]);}
  closePath() {this.commands.push(['close']);}
  setLineDash(dash) {this.dash=dash.slice();}
  stroke() {this.strokes.push({...this.state(),commands:this.commands.slice()});}
  fill() {this.fills.push({...this.state(),commands:this.commands.slice()});}
  fillText() {assert.fail('Numeric weights must not be painted');}
  fillRect() {assert.fail('No numeric label backgrounds');}
  measureText() {assert.fail('Painter must not recompute label geometry');}
}
var ui$transform={x:0,y:0,scale:fixture.scale||1};
var dom$stage={clientWidth:800,clientHeight:700};
var window={matchMedia(){return {matches:!!fixture.reduced};}};
var document={hidden:false,createElement(){assert.fail('Painter cloned DOM');}};
function isDark() {return !!fixture.dark;}
function layoutGraph() {assert.fail('Painter invoked the layout solver');}
function geometry() {assert.fail('Painter recomputed scene routes');}
function world() {assert.fail('Painter reprojected the graph');}
function requestAnimationFrame() {assert.fail('Static flow scheduled animation');}
function setTimeout() {assert.fail('Static flow scheduled a timer');}
function schedule() {assert.fail('Static flow scheduled the whole map');}
function route(points,count=12) {
  const a=points.at(-2),b=points.at(-1),length=Math.hypot(b.x-a.x,b.y-a.y);
  return {source:'original-source',target:'original-target',count,strength:Math.min(4.8,1.2+.55*Math.log2(1+count)),
    kind:fixture.kind||'statement',points,radii:points.map(()=>0),arrow:{dx:(b.x-a.x)/length,dy:(b.y-a.y)/length},
    label:{x:10,y:10,w:30,h:20}};
}
function triangle(paint) {return paint.commands.filter(command=>command[0]==='move'||command[0]==='line').map(command=>command.slice(1));}
function direction(points,dx,dy,scale) {
  const tip=points[1],wings=[points[0],points[2]];
  assert(wings.every(point=>(point[0]-tip[0])*dx+(point[1]-tip[1])*dy<0),'Chevron must point along its actual segment');
  const cross=wings.map(point=>(point[0]-tip[0])*dy-(point[1]-tip[1])*dx);
  assert(cross[0]*cross[1]<0);assert(Math.max(...cross.map(Math.abs))<=4*scale+1e-6);
}
"""


def _run(body: str, fixture: object = None) -> None:
    script = "const ROOT=" + json.dumps(str(_ROOT)) + ";\n" + _ENVIRONMENT
    script += "vm.runInContext(helpers+" + json.dumps(_CANVAS + body) + ",context,{timeout:1000});"
    result = subprocess.run([_NODE, "-e", script], input=json.dumps(fixture or {}), text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stdout[-1000:] + result.stderr[-5000:]


@pytest.mark.parametrize("scale", [.02, .25, .9, 1, 4])
@pytest.mark.parametrize("kind", ["statement", "proof"])
def test_stroke_strength_scales_in_world_space_and_preserves_typed_counts(scale: float, kind: str) -> None:
    _run(r"""
      const ctx=new Canvas(),initial=ctx.state(),weak=route([{x:100,y:200},{x:600,y:200}],1);
      const strong=route(weak.points,1000),before=JSON.stringify(strong);
      drawRoute(ctx,weak,true);const first=ctx.strokes[0];ctx.strokes=[];ctx.fills=[];
      drawRoute(ctx,strong,false);const main=ctx.strokes[0];
      assert.equal(main.lineWidth,strong.strength*fixture.scale);assert(main.lineWidth>first.lineWidth);
      assert(main.lineWidth<=4.8*fixture.scale);assert.equal(main.strokeStyle,'#64748b');
      assert.deepEqual(main.dash,fixture.kind==='proof'?[6*fixture.scale,4*fixture.scale]:[]);
      assert.equal(ctx.fills.length,1,'Every edge retains its terminal arrow');
      assert.equal(JSON.stringify(strong),before);assert.deepEqual(ctx.state(),initial);
    """, {"scale": scale, "kind": kind})


@pytest.mark.parametrize(("dx", "dy"), [(1, 0), (0, 1), (-1, 0), (0, -1)])
@pytest.mark.parametrize("reduced", [False, True])
def test_static_terminal_arrow_and_single_chevron_retain_actual_direction(dx: int, dy: int, reduced: bool) -> None:
    _run(r"""
      const dx=fixture.dx,dy=fixture.dy,center={x:400,y:350};
      const path=[{x:center.x-dx*200,y:center.y-dy*200},{x:center.x+dx*200,y:center.y+dy*200}];
      const input=route(path),ctx=new Canvas();drawRoute(ctx,input,true);
      assert.equal(ctx.strokes.length,2,'One path and one restrained static cue');assert.equal(ctx.fills.length,1);
      const arrow=triangle(ctx.fills[0]),tip=arrow[0],wings=arrow.slice(1);
      assert.deepEqual(tip,[path[1].x,path[1].y]);
      assert(wings.every(point=>(point[0]-tip[0])*dx+(point[1]-tip[1])*dy<0));
      direction(triangle(ctx.strokes[1]),dx,dy,1);
    """, {"dx": dx, "dy": dy, "reduced": reduced})


def test_chevron_follows_the_visible_middle_segment_not_the_terminal_tangent() -> None:
    _run(r"""
      const input=route([{x:100,y:100},{x:160,y:100},{x:160,y:600},{x:220,y:600}]),ctx=new Canvas();
      drawRoute(ctx,input,true);direction(triangle(ctx.strokes[1]),0,1,1);
      assert.equal(input.arrow.dx,1);assert.equal(input.arrow.dy,0);
      assert.deepEqual(triangle(ctx.fills[0])[0],[220,600]);
    """)


def test_long_offscreen_connections_place_only_one_cue_inside_the_current_view() -> None:
    _run(r"""
      const input=route([{x:-500,y:300},{x:1500,y:300}]),ctx=new Canvas();drawRoute(ctx,input,true);
      const cue=triangle(ctx.strokes[1])[1];assert(cue[0]>=12&&cue[0]<=788&&cue[1]>=12&&cue[1]<=688);
      assert.equal(ctx.strokes.length,2);assert.equal(ctx.fills.length,1);
    """)


@pytest.mark.parametrize("dark", [False, True])
def test_cues_reuse_neutral_theme_colors_and_cached_geometry_without_ambient_animation(dark: bool) -> None:
    _run(r"""
      const input=route([{x:100,y:300},{x:600,y:300}]),ctx=new Canvas();
      input.parts=[];drawRoute(ctx,input,true);
      const segments=drawRoute.$flowSegments.get(input.parts);assert(segments);
      ui$transform.x=40;window.matchMedia=()=>({matches:true});document.hidden=true;
      drawRoute(ctx,input,true);assert.strictEqual(drawRoute.$flowSegments.get(input.parts),segments);
      const color=fixture.dark?'#a8b0bb':'#64748b';
      assert(ctx.strokes.every(paint=>paint.strokeStyle===color));assert(ctx.fills.every(paint=>paint.fillStyle===color));
      assert.equal(input.count,12);assert.equal(input.source,'original-source');assert.equal(input.target,'original-target');
    """, {"dark": dark})


@pytest.mark.parametrize("arrays", [False, True])
def test_accessible_relation_counts_preserve_mixed_kinds_directions_and_duplicate_weights(arrays: bool) -> None:
    _run(r"""
      const captured=[['A','T',2,1],['A','T',3,4],['T','A',7,0],['B','T',0,8],['T','C',1,9],['T','T',4,2],['U','V',99,99]];
      const edges=fixture.arrays?captured:captured.map(edge=>({source:edge[0],target:edge[1],statement:edge[2],proof:edge[3]}));
      const before=JSON.stringify(edges);edges.forEach(Object.freeze);Object.freeze(edges);
      const counts=relationCounts(edges,'T');
      assert.deepEqual(Array.from(counts.before),[['A',{statement:5,proof:5}],['B',{statement:0,proof:8}],['T',{statement:4,proof:2}]]);
      assert.deepEqual(Array.from(counts.after),[['A',{statement:7,proof:0}],['C',{statement:1,proof:9}],['T',{statement:4,proof:2}]]);
      assert.equal(JSON.stringify(edges),before);assert.equal(relationCounts(edges,'absent').before.size,0);
      assert.equal(relationCounts(edges,'absent').after.size,0);
    """, {"arrays": arrays})
