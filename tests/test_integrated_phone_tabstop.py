"""Execute production fit/free-area/tabstop helpers over tiny real scene geometry.

These bounded source checks isolate DOM controls; genuine browser center-hit
diagnostics remain required and are not simulated here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is required for focused tabstop source checks")

_LOADER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(ROOT+'/autoform_cli/assets/blueprint-dag.js','utf8');
function original(name) {
  const start=source.indexOf('function '+name+'(');assert(start>=0);
  for(let end=source.indexOf('}',start);end>=0;end=source.indexOf('}',end+1)) {
    const body=source.slice(start,end+1);
    try {new vm.Script('('+body+')');return body;}catch(error){if(!(error instanceof SyntaxError))throw error;}
  }
  assert.fail('Production helper did not parse: '+name);
}
const context={assert};vm.createContext(context);
const constants=source.slice(source.indexOf('  var NODE_W ='),source.indexOf('  var clients ='))+
  source.slice(source.indexOf('  var clamp ='),source.indexOf('  function element('));
const functions=['layoutGraph','geometry','compactRegions','world','screen','inViewport','fit','mapHeight','freeArea','center','updateOverlays','relationCounts','weightWords'].map(original).join('\n');
"""

_ENVIRONMENT = r"""
class Control {
  constructor(id) {this.dataset={autoformNodeId:id};this.style={setProperty(){}};this.tabIndex=-1;this.removed=false;}
  querySelector() {return null;}
  getAttribute(name) {return this[name]===undefined?null:this[name];}
  setAttribute(name,value) {this[name]=String(value);}
  remove() {this.removed=true;}
}
var navigation={},mapAllowed=true,atlas=true,ui$area=null,ui$orientation='tb',family='sans-serif';
var scene,sceneData,sceneDirection,sceneCompact,sceneFamily;
var dom$stage={clientWidth:390,clientHeight:560},dom$ctx={},dom$inspector;
var host={classList:{contains(){return false;}}};
var ui$transform={x:0,y:0,scale:1},ui$selected=null,ui$hovered=null;
var data={nodes:['Analysis','Geometry','Unstarted mathematics'].map((area,index)=>({id:'region:'+area,
  title:area,_region:area,area,kind:'scope',status:'planned',width:312,height:96,_index:index})),
  edges:[{source:'region:Geometry',target:'region:Analysis',statement:0,proof:1}]};
var ui$active=data.nodes,ui$activeIds=new Set(data.nodes.map(node=>node.id));
var prerequisites=new Map(data.nodes.map(node=>[node.id,[]])),dependents=new Map(data.nodes.map(node=>[node.id,[]]));
var byId=new Map(data.nodes.map(node=>[node.id,node])),overlays=new Map(),dom$nodeLayer={appendChild(){},style:{setProperty(){}}},dom$density={};
var document={activeElement:{dataset:{}}};
function sheetLayout(){return true;} function schedule(){} function remember(){} function calculateActive(){}
function colors(){return {fill:'#fff',stroke:'#aaa',text:'#111'};} function statusRing(){return 'transparent';}
function makeNodeControl(node){const control=new Control(node.id);control.setAttribute('aria-label',node.title+', 0 articles');return control;}
function paint() {updateOverlays(ui$active.filter(node=>inViewport(screen(node),80)),null);}
function tabstop(){return Array.from(overlays.values()).find(control=>control.tabIndex===0);}
function contained(node){const box=screen(node),free=freeArea();return box.x>=free.left&&box.x+box.w<=free.right&&
  box.y>=(free.top||0)&&box.y+box.h<=(free.top||0)+free.band;}
"""


def _run(body: str) -> None:
    script = "const ROOT=" + json.dumps(str(_ROOT)) + ";\n" + _LOADER
    script += "vm.runInContext(constants+functions+" + json.dumps(_ENVIRONMENT + body) + ",context,{timeout:1500});"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stdout[-1000:] + result.stderr[-5000:]


def test_initial_phone_tabstop_is_inside_the_real_compact_free_area_after_fit() -> None:
    _run("""
      fit();paint();assert(dom$density.textContent.startsWith('Showing part of the map'),'The real three-area scene must exercise readable cropping');
      const cropped=byId.get('region:Geometry'),box=screen(cropped);
      assert(box.y+box.h/2<52,'The old top-left candidate lies in the actual clipped header band');
      const selected=tabstop();assert(selected);assert(contained(byId.get(selected.dataset.autoformNodeId)),
        'Initial tabstop must be fully visible, not merely inside the80px culling margin');
      assert.equal(selected.dataset.autoformNodeId,'region:Analysis');assert.equal(ui$selected,null);
    """)


def test_manual_pan_keeps_the_existing_focused_control_without_changing_camera() -> None:
    _run("""
      fit();paint();const focused=overlays.get('region:Analysis');document.activeElement=focused;
      ui$transform.x-=2000;const camera=JSON.stringify(ui$transform);paint();
      assert.strictEqual(tabstop(),focused);assert.strictEqual(overlays.get('region:Analysis'),focused);
      assert.equal(focused.removed,false);assert.equal(JSON.stringify(ui$transform),camera);assert.equal(ui$selected,null);
    """)


def test_full_visibility_preference_keeps_selection_and_never_refits_a_saved_camera() -> None:
    _run("""
      fit();ui$selected=byId.get('region:Geometry');const camera=JSON.stringify(ui$transform);paint();
      assert.equal(tabstop().dataset.autoformNodeId,'region:Analysis');
      assert.equal(overlays.get('region:Geometry').dataset.selected,'true');
      // The selection's neighbour names the link's weight by kind in its accessible name; the selection itself does not.
      assert.equal(overlays.get('region:Analysis')['aria-label'],'Analysis, 0 articles, uses Geometry · 1 proof dependency');
      assert.equal(overlays.get('region:Geometry')['aria-label'],'Geometry, 0 articles');
      ui$selected=null;paint();assert.equal(overlays.get('region:Analysis')['aria-label'],'Analysis, 0 articles');ui$selected=byId.get('region:Geometry');paint();
      assert.equal(ui$selected.id,'region:Geometry');assert.equal(JSON.stringify(ui$transform),camera);
      document.activeElement=overlays.get('region:Geometry');paint();
      assert.equal(tabstop().dataset.autoformNodeId,'region:Geometry');assert.equal(JSON.stringify(ui$transform),camera);
    """)


_DESKTOP_OVERVIEW = r"""
dom$stage={clientWidth:1336,clientHeight:812};ui$orientation='lr';sheetLayout=()=>false;
data={nodes:['A','B','C','D','E'].map((area,index)=>({id:'region:'+area,_region:area,area,
  kind:'scope',status:'planned',width:264,height:148,_index:index})),edges:[]};
data.nodes.forEach((node,index)=>data.edges.push({source:node.id,target:data.nodes[(index+1)%5].id,statement:3,proof:0}));
ui$active=data.nodes;ui$activeIds=new Set(data.nodes.map(node=>node.id));byId=new Map(data.nodes.map(node=>[node.id,node]));
const current=geometry();
// A route's conservative bounds may include an outside return corridor.
// Keep the real production card geometry and readable camera, not a fake renderer.
current.routes.forEach(route=>route.bounds={x:-5000,y:-5000,w:10000,h:10000});
"""


def test_small_authored_desktop_overview_fits_its_cards_not_peripheral_route_bounds() -> None:
    _run(_DESKTOP_OVERVIEW + """
      fit();paint();assert(!String(dom$density.textContent||'').startsWith('Showing part of the map'),
        'All five authored areas should fit after the empty reader yields its width');
      assert(ui$transform.scale>=.9,'Do not trade legibility for a repository-wide fit');
      data.nodes.forEach(node=>{const box=screen(node);
        assert(box.x>=36-1e-6&&box.x+box.w<=dom$stage.clientWidth-36+1e-6);
        assert(box.y>=36-1e-6&&box.y+box.h<=dom$stage.clientHeight-36+1e-6);
      });
    """)


def test_ordinary_scope_fit_keeps_peripheral_routes_and_the_readable_floor() -> None:
    _run(_DESKTOP_OVERVIEW + """
      data.nodes.forEach(node=>delete node._region);
      fit();assert.equal(ui$transform.scale,.9,'A large scope must stay readable, not shrink to fit the repository');
      // Its real route bounds do not fit at the floor, so the camera centres a card rather than the whole bounds.
      const first=screen(data.nodes[0]);
      assert(Math.abs(first.x+first.w/2-dom$stage.clientWidth/2)<1e-6&&Math.abs(first.y+first.h/2-dom$stage.clientHeight/2)<1e-6,
        'Normal scope geometry must still account for its real route bounds');
    """)
