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
const helpers=['drawRoute','drawPill','relationCounts','edgeStyle','edgeRole','pill'].map(original).join('\n');
"""

_CANVAS = r"""
class Canvas {
  constructor() {
    this.strokes=[];this.fills=[];this.texts=[];this.commands=[];this.dash=[];this.stack=[];
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
  fillText(text,x,y) {if(!fixture.lit)assert.fail('Numeric weights must not be painted at rest');this.texts.push({text,x,y,font:this.font,fill:this.fillStyle});}
  fillRect() {assert.fail('No numeric label backgrounds');}
  measureText(text) {return {width:text.length*7};}
}
var family='sans-serif';
function rounded(ctx) {ctx.beginPath();}
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
      drawRoute(ctx,weak,-1);const first=ctx.strokes[0];ctx.strokes=[];ctx.fills=[];
      drawRoute(ctx,strong,-1);const main=ctx.strokes[0];
      assert.equal(main.lineWidth,strong.strength*fixture.scale);assert(main.lineWidth>first.lineWidth);
      assert(main.lineWidth<=4.8*fixture.scale);assert.equal(main.strokeStyle,'#85888C');
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
      const input=route(path),ctx=new Canvas();drawRoute(ctx,input,-1);
      assert.equal(ctx.strokes.length,2,'One path and one restrained static cue');assert.equal(ctx.fills.length,1);
      const arrow=triangle(ctx.fills[0]),tip=arrow[0],wings=arrow.slice(1);
      assert.deepEqual(tip,[path[1].x,path[1].y]);
      assert(wings.every(point=>(point[0]-tip[0])*dx+(point[1]-tip[1])*dy<0));
      direction(triangle(ctx.strokes[1]),dx,dy,1);
    """, {"dx": dx, "dy": dy, "reduced": reduced})


def test_chevron_follows_the_visible_middle_segment_not_the_terminal_tangent() -> None:
    _run(r"""
      const input=route([{x:100,y:100},{x:160,y:100},{x:160,y:600},{x:220,y:600}]),ctx=new Canvas();
      drawRoute(ctx,input,-1);direction(triangle(ctx.strokes[1]),0,1,1);
      assert.equal(input.arrow.dx,1);assert.equal(input.arrow.dy,0);
      assert.deepEqual(triangle(ctx.fills[0])[0],[220,600]);
    """)


def test_long_offscreen_connections_place_only_one_cue_inside_the_current_view() -> None:
    _run(r"""
      const input=route([{x:-500,y:300},{x:1500,y:300}]),ctx=new Canvas();drawRoute(ctx,input,-1);
      const cue=triangle(ctx.strokes[1])[1];assert(cue[0]>=12&&cue[0]<=788&&cue[1]>=12&&cue[1]<=688);
      assert.equal(ctx.strokes.length,2);assert.equal(ctx.fills.length,1);
    """)


@pytest.mark.parametrize("dark", [False, True])
def test_cues_reuse_neutral_theme_colors_and_cached_geometry_without_ambient_animation(dark: bool) -> None:
    _run(r"""
      const input=route([{x:100,y:300},{x:600,y:300}]),ctx=new Canvas();
      input.parts=[];drawRoute(ctx,input,-1);
      const segments=drawRoute.$flowSegments.get(input.parts);assert(segments);
      ui$transform.x=40;window.matchMedia=()=>({matches:true});document.hidden=true;
      drawRoute(ctx,input,-1);assert.strictEqual(drawRoute.$flowSegments.get(input.parts),segments);
      const color=fixture.dark?'#A8B0BB':'#85888C';
      assert(ctx.strokes.every(paint=>paint.strokeStyle===color));assert(ctx.fills.every(paint=>paint.fillStyle===color));
      assert.equal(input.count,12);assert.equal(input.source,'original-source');assert.equal(input.target,'original-target');
    """, {"dark": dark})


@pytest.mark.parametrize("dark", [False, True])
def test_only_a_lit_link_names_its_count_in_words_in_its_two_colours(dark: bool) -> None:
    _run(r"""
      // Both lit roles take the text colour: blue is a status colour, and the arrowheads say which way a link points.
      const lit={1:fixture.dark?'#E4E6EB':'#1C1E21',2:fixture.dark?'#E4E6EB':'#1C1E21'};
      [1,2].forEach(role=>{
        const input=route([{x:100,y:300},{x:600,y:300}]),ctx=new Canvas(),label=drawRoute(ctx,input,role,0);
        // The route hands its pill back, so draw() can paint every pill after the last route is stroked.
        assert.equal(ctx.texts.length,0);assert.equal(ctx.strokes.length,1);assert.equal(label.role,role);drawPill(ctx,label,[]);
        assert.equal(ctx.strokes[0].strokeStyle,lit[role]);assert.equal(ctx.strokes[0].lineWidth,input.strength+1);
        assert.equal(ctx.texts.length,1);assert.equal(ctx.texts[0].text,'12 links');assert.equal(ctx.texts[0].fill,lit[role]);
        assert.equal(ctx.texts[0].font,'600 13px sans-serif');
        // The pill takes the cue's place, on the visible straight portion; the terminal arrow remains.
        assert(ctx.texts[0].x>100&&ctx.texts[0].x<600);assert.equal(ctx.strokes.length,2,'The path and the pill outline, no chevron');
        assert.equal(ctx.fills[0].fillStyle,lit[role]);assert.equal(triangle(ctx.fills[0]).length,3);
        assert.equal(ctx.fills[1].fillStyle,fixture.dark?'#18191a':'#f0f2f5');
      });
      const stacked=[-1,1].map(stack=>{const ctx=new Canvas();drawPill(ctx,drawRoute(ctx,route([{x:100,y:300},{x:600,y:300}]),1,stack),[]);return ctx.texts[0].y;});
      assert(stacked[1]-stacked[0]>=20);
      const single=new Canvas();assert.equal(drawRoute(single,route([{x:100,y:300},{x:600,y:300}],1),1,0),null);assert.equal(single.texts.length,0);
      const faded=new Canvas();assert.equal(drawRoute(faded,route([{x:100,y:300},{x:600,y:300}]),0,0),null);
      assert.equal(faded.texts.length,0);assert.equal(faded.strokes[0].globalAlpha,.08);
      // A route with no straight part long enough for the cue still names a lit count, halfway along; at rest it shows no chevron.
      const short=new Canvas(),shortLabel=drawRoute(short,route([{x:100,y:300},{x:170,y:300}]),1,0);
      assert.deepEqual(shortLabel.spots,[{x:135,y:300}]);drawPill(short,shortLabel,[]);
      assert.equal(short.texts.length,1);assert.equal(short.texts[0].x,135);
      const resting=new Canvas();drawRoute(resting,route([{x:100,y:300},{x:170,y:300}]),-1,0);assert.equal(resting.strokes.length,1);
      // The pill takes the route's first spot clear of every card, and the longest spot when none is clear.
      const bent=route([{x:100,y:100},{x:100,y:600},{x:500,y:600}]),card={x:60,y:300,w:80,h:100};
      const first=new Canvas(),spots=drawRoute(first,bent,1,0).spots;assert.equal(spots[0].x,100);assert.equal(spots[1].y,600);
      drawPill(first,{count:12,role:1,scale:1,spots},[card]);assert.equal(first.texts[0].y,600+4.5);
      const blocked=new Canvas();drawPill(blocked,{count:12,role:1,scale:1,spots},[card,{x:200,y:560,w:200,h:80}]);
      assert.equal(blocked.texts[0].x,100);
      // A phone's open reader leaves the map only the band above it, so the pill sits in the visible part of the link.
      const banded=drawRoute(new Canvas(),route([{x:300,y:100},{x:300,y:650}]),1,0,315).spots[0];
      assert(banded.y<=303&&banded.y>=12,'Pill under the reader: '+banded.y);
    """, {"dark": dark, "lit": True})



def test_pills_keep_clear_of_each_other_and_a_cycle_still_colours_by_direction() -> None:
    _run(r"""
      // On ATLAS's roadmap three count pills stacked on one trunk and hid the middle one.
      const pills=[],ctx=new Canvas(),trunk=[{x:100,y:300},{x:700,y:300}];
      const labels=[12,30,7].map(count=>drawRoute(ctx,route(trunk,count),1,0));
      labels.sort((a,b)=>b.count-a.count).forEach(label=>drawPill(ctx,label,[],pills));
      assert.deepEqual(ctx.texts.map(text=>text.text),['30 links','12 links','7 links']);
      for(let i=0;i<pills.length;i++) for(let j=i+1;j<pills.length;j++) {
        const a=pills[i],b=pills[j];
        assert(a.x+a.w<=b.x||b.x+b.w<=a.x||a.y+a.h<=b.y||b.y+b.h<=a.y,'Pills overlap: '+JSON.stringify([a,b]));
      }
      // A route too short to slide along leaves its pill out rather than printing it over another.
      const short=new Canvas(),taken=[{x:105,y:290,w:60,h:20}],label=drawRoute(short,route([{x:100,y:300},{x:170,y:300}]),1,0);
      drawPill(short,label,[],taken);assert.equal(short.texts.length,0);
      // Areas that depend on each other lit every link blue; a previewed card's links follow their own direction.
      const spotlight={subject:'A',before:new Set(['A','B']),after:new Set(['A','B']),nodes:new Set(['A','B'])};
      assert.equal(edgeRole(spotlight,'B','A'),1);assert.equal(edgeRole(spotlight,'A','B'),2);
      // Without a previewed card the path rule stands.
      assert.equal(edgeRole({before:new Set(['A','B']),after:new Set(['A']),nodes:new Set(['A','B'])},'A','B'),1);
    """, {"dark": False, "lit": True})

@pytest.mark.parametrize("scale", [.6, 1, 2])
def test_proof_dashes_keep_butt_caps_so_lit_gaps_stay_open(scale: float) -> None:
    _run(r"""
      [[-1,1],[1,1],[2,3],[1,12]].forEach(([role,count])=>{
        fixture.kind='proof';const dashed=new Canvas();drawRoute(dashed,route([{x:100,y:300},{x:600,y:300}],count),role,0);
        const stroke=dashed.strokes[0];assert.equal(stroke.lineCap,'butt');assert.deepEqual(stroke.dash,[6*fixture.scale,4*fixture.scale]);
        fixture.kind='statement';const solid=new Canvas();drawRoute(solid,route([{x:100,y:300},{x:600,y:300}],count),role,0);
        assert.equal(solid.strokes[0].lineCap,'round');
      });
    """, {"scale": scale, "lit": True})


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
