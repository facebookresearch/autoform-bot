"""Execute the production observer scheduler, resize writes and disposal.

RAF/observer/timer, canvas, accessibility and painter peers are isolated here. These
checks establish delivery/lifecycle behavior, not native WebKit layout or paint.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is required for observer source checks")

_HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(CLIENT,'utf8');
function declarationAt(start) {
  assert(start>=0,'Missing actual production callback');
  for(let end=source.indexOf('}',start);end>=0;end=source.indexOf('}',end+1)) {
    const candidate=source.slice(start,end+1);
    try {new vm.Script('('+candidate+')');return candidate;}
    catch(error) {if(!(error instanceof SyntaxError))throw error;}
  }
  assert.fail('Unparseable actual production callback');
}
function original(name,owner) {
  const offset=typeof owner==='string'?source.indexOf('function '+owner+'('):0;
  assert(offset>=0,'Missing production callback owner');
  return declarationAt(source.indexOf('function '+name+'(',offset));
}
const state=source.match(/\bvar alive =[^;]+;/);assert(state,'Missing mount lifecycle state');
const bind=original('bindEvents'),begin=bind.indexOf('if (window.ResizeObserver)');
const end=bind.indexOf('globalListen(window, "resize", syncSheetAccessibility)',begin);
assert(begin>=0&&end>begin,'Missing actual observer/fallback binding');
const binding=bind.slice(begin,end);new vm.Script(binding);
const marker=source.indexOf('dispose: function () { alive = false');
assert(marker>=0,'Missing actual mount disposal');
const dispose=declarationAt(source.indexOf('function',marker));
const frames=new Map(),timers=new Map(),cancelled=[],observed=[],listeners=[];
let nextFrame=0,nextTimer=0,delivering=false;
class ObserverPeer {
  constructor(callback){this.callback=callback;this.targets=[];this.disconnections=0;observed.push(this);}
  observe(target){this.targets.push(target);}
  disconnect(){this.disconnections++;}
}
function notify(observer) {
  delivering=true;
  try {observer.callback([{target:observer.targets[0]}],observer);}
  finally {delivering=false;}
}
function flushFrame() {
  const current=Array.from(frames);frames.clear();
  current.forEach(([id,callback])=>callback(id));
}
function flushTimers() {
  const current=Array.from(timers.values());timers.clear();current.forEach(callback=>callback());
}
const context={assert,frames,timers,cancelled,observed,listeners,notify,flushFrame,flushTimers,binding,original,productionSource:source,
  ResizeObserver:ObserverPeer,
  requestAnimationFrame(callback){const id=nextFrame++;frames.set(id,callback);return id;},
  cancelAnimationFrame(id){cancelled.push(id);frames.delete(id);},
  setTimeout(callback){const id=nextTimer++;timers.set(id,callback);return id;},
  clearTimeout(id){timers.delete(id);},
  canvasWrite(){assert(!delivering,'Actual resize wrote canvas during observer delivery');}
};
vm.createContext(context);
vm.runInContext(state[0]+'\n'+['queueResize','schedule','resize','setSheet','compactRegions','sheetLayout','syncSheetAccessibility','positionMapFeedback','stopRowMath','fallbackRowMath']
  .map(original).join('\n')+'\nvar dispose='+dispose+';',context,{timeout:1000});
vm.runInContext(`
var window={ResizeObserver,devicePixelRatio:2,matchMedia(){return {matches:false};}};
var classes=new Set(),host={classList:{contains(name){return classes.has(name);},
  toggle(name,on){if(on)classes.add(name);else classes.delete(name);}}};
var document={activeElement:null},navigation=null,dom$pageHeading=null;
var dom$inspectorInner={contains(){return false;},setAttribute(){},removeAttribute(){}},dom$sheetHandle={replaceChildren(){},setAttribute(){},focus(){}};
// The redesign's handle names the page, so its accessibility pass reads the route and records.
var currentBundle=null,records=new Map(),ui$requestedNode=null,ui$selected=null,ui$mathJob=null,scene=null;
function readingLabel(){return '';} function refreshReadingLabels(){} function swatch(){return '';}
var dom$stage={clientWidth:800,clientHeight:600},writes=[],canvasWidth=0,canvasHeight=0;
var dom$breadcrumb={clientWidth:600},dom$mapKey=null,dom$density=null;
function renderBreadcrumb(){renderBreadcrumb.$width=dom$breadcrumb.clientWidth;}
renderBreadcrumb.$width=dom$breadcrumb.clientWidth;
var dom$canvas={
  get width(){return canvasWidth;},set width(value){canvasWrite();writes.push(['width',value]);canvasWidth=value;},
  get height(){return canvasHeight;},set height(value){canvasWrite();writes.push(['height',value]);canvasHeight=value;}
};
var ui$area=null,ui$overviewOnly=false,ui$scopeOverview=false,ui$coarseOverview=false,ui$orientation='lr',ui$mode='graph',firstResize=false;
var ui$transform={x:41,y:-73,scale:1.2},scheduled=false,data={nodes:[{id:'actual-topic',status:'fully_proved'}],edges:[]};
var ui$wikiProjectionTicket=0,ui$wikiProjectionCache=new WeakMap(),drawCalls=0,fitCalls=0,onDraw=null;
function draw(){drawCalls++;if(onDraw)onDraw();}
function fit(){fitCalls++;}
function globalListen(target,type,callback){listeners.push({target,type,callback});}
function install(){eval(binding);}
`,context,{timeout:1000});
"""


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _HARNESS
    script += "vm.runInContext(" + json.dumps(body) + ",context,{timeout:1000});"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def test_notifications_coalesce_to_one_frame_outside_observer_delivery() -> None:
    _run(r"""
      install();assert.equal(observed.length,1);const observer=observed[0];
      assert.strictEqual(observer.callback,queueResize);assert.strictEqual(observer.targets[0],dom$stage);
      const before=JSON.stringify({data,camera:ui$transform});
      notify(observer);notify(observer);notify(observer);
      assert.equal(frames.size,1);assert.equal(resizeFrame,0,'A valid zero RAF handle must still coalesce');
      assert.equal(writes.length,0);assert.equal(drawCalls,0);
      flushFrame();assert.equal(resizeFrame,null);assert.equal(frames.size,0);
      assert.deepEqual(writes,[['width',1600],['height',1200]]);assert.equal(drawCalls,1);
      assert.equal(JSON.stringify({data,camera:ui$transform}),before);
      flushFrame();assert.equal(drawCalls,1,'A stable size must not start a permanent frame loop');
    """)


def test_callback_clears_its_handle_before_resize_and_can_rearm_once() -> None:
    _run(r"""
      install();notify(observed[0]);
      onDraw=()=>{assert.equal(resizeFrame,null);queueResize();queueResize();};
      flushFrame();assert.equal(drawCalls,1);assert.equal(frames.size,1);assert.equal(resizeFrame,1);
      onDraw=null;flushFrame();assert.equal(drawCalls,2);assert.equal(frames.size,0);assert.equal(resizeFrame,null);
      dom$stage.clientHeight=610;notify(observed[0]);notify(observed[0]);
      assert.equal(frames.size,1);flushFrame();assert.equal(dom$canvas.height,1220);assert.equal(drawCalls,3);
      assert.equal(frames.size,0);
    """)


def test_disposal_cancels_owned_resize_and_late_callbacks_cannot_write() -> None:
    _run(r"""
      install();notify(observed[0]);const pending=frames.get(resizeFrame),observer=observed[0];
      let cleaned=0,disposed=0;cleanup.push(()=>cleaned++);navigation={dispose(){disposed++;}};frame=99;
      dispose();assert.equal(alive,false);assert.equal(resizeFrame,null);assert.equal(frames.size,0);
      assert.deepEqual(cancelled,[99,0]);assert.equal(cleaned,1);assert.equal(disposed,1);
      assert.equal(observer.disconnections,1);
      pending(0);notify(observer);queueResize();flushFrame();
      assert.equal(writes.length,0);assert.equal(drawCalls,0);assert.equal(frames.size,0);
    """)


def test_initial_resize_and_no_observer_window_fallback_remain_synchronous() -> None:
    _run(r"""
      firstResize=true;resize();assert.equal(fitCalls,1);assert.equal(firstResize,false);
      assert.equal(dom$canvas.width,1600);assert.equal(dom$canvas.height,1200);assert.equal(frames.size,0);
      window.ResizeObserver=null;install();assert.equal(observed.length,0);assert.equal(listeners.length,1);
      assert.strictEqual(listeners[0].target,window);assert.equal(listeners[0].type,'resize');
      assert.strictEqual(listeners[0].callback,resize);
      dom$stage.clientHeight=610;listeners[0].callback();
      assert.equal(dom$canvas.height,1220);assert.equal(drawCalls,1);assert.equal(frames.size,0);
    """)


def test_controlled_tablet_sheet_resize_settles_before_checkpoint_and_trailing_observer() -> None:
    _run(r"""
      // Actual five-cycle metadata from navigation_overview_site: three
      // topics per authored area, two typed prerequisites per adjacent pair.
      // Effective 570/952x722 viewports reproduce the completed native failure.
      eval(productionSource.slice(productionSource.indexOf('  var NODE_W ='),productionSource.indexOf('  var clients =')));
      eval(productionSource.slice(productionSource.indexOf('  var clamp ='),productionSource.indexOf('  function element(')));
      eval(['layoutGraph','wikiRoutes','geometry','sceneFrame','sceneDetours','framingNodes','framingViewport','framingPadding',
        'freeArea','world','screen','fit','center','currentOverviewGroup','matchesStatus','matchesIsolation','calculateActive',
        'captureState','writeLocation','element'].map(original).join('\n'));
      eval(original('remember','mount'));
      const areas=['Algebra','Analysis','Discrete mathematics','Geometry','Logic'];
      data={title:'Mathematical map',nodes:areas.map(area=>({id:'region:'+area,area,_region:area,kind:'scope',
        width:264,height:148,status:'planned',status_counts:[['planned',3]]})),
        edges:areas.map((area,index)=>({source:'region:'+areas[(index+4)%5],target:'region:'+area,statement:2,proof:0}))};
      const before=JSON.stringify(data);let saved=null,method=null;
      navigation={checkpoint(value){method=value;saved=captureState();return saved;}};
      var currentBundle={title:'Mathematical map',scope:'roadmap',nodeId:'roadmap'},dom$body={offsetTop:178},dom$inspector={offsetTop:0};
      dom$stage={get clientWidth(){return classes.has('bp-dag-sheet-open')?570:952;},clientHeight:722};
      dom$sheetHandle={setAttribute(name,value){this[name]=value;},replaceChildren(){},focus(){}};
      document.createElement=tag=>({tagName:tag.toUpperCase()});host.prepend=()=>{};
      var atlas=true,mapAllowed=true,family='sans-serif',scene,sceneData,sceneDirection,sceneCompact,sceneFamily,sceneAspect,sceneWiki,sceneReduced;
      var ui$active=data.nodes,ui$activeDirty=true,ui$activeIds=new Set(),ui$direction='both',ui$overviewGroup=null;
      var ui$selected=null,ui$selectedStatuses=new Set(['planned']),ui$showIsolated=true,ui$requestedNode='roadmap';
      var ui$query='',ui$listLimit=100,ui$relationIds=null,ui$outlineLimit=120,ui$readableCrop=false,ui$overviewOnly=true;
      var dom$hiddenButton={},dom$empty={},dom$fit={setAttribute(name,value){this[name]=value;}},dom$search={};
      var dom$readingBody={contains(){return false;},scrollTop:0},dom$navigation={scrollTop:0},dom$inventoryScroll={scrollTop:0},dom$shelves={scrollTop:0};
      const actualLayout=layoutGraph;let layouts=0,painted=null;
      layoutGraph=(...args)=>{layouts++;return actualLayout(...args);};
      draw=()=>{drawCalls++;const box=world(data.nodes[0]);painted=[ui$transform.scale,ui$transform.scale,
        box.x*ui$transform.scale+ui$transform.x,box.y*ui$transform.scale+ui$transform.y];};
      fit();flushFrame();assert.equal(layouts,2);install();
      setSheet(true,null);assert.equal(ui$orientation,'tb');assert.equal(dom$canvas.width,1140);
      // Five compact cards and four unchanged 48px gaps fill the caption/key-clear band.
      assert(Math.abs(ui$transform.scale-(722-72)/(5*88+4*48))<1e-9);
      flushFrame();assert.equal(layouts,4);notify(observed[0]);
      setSheet(false,null);
      assert.equal(ui$orientation,'lr','The owning click must settle the actual new orientation');
      assert.equal(dom$canvas.width,1904);assert.equal(layouts,6);
      assert(Math.abs(ui$transform.scale-1.1888888888888889)<1e-9);assert.equal(ui$readableCrop,false);
      writeLocation('replace');assert.equal(method,'replace');assert.deepEqual(saved.camera,ui$transform);
      const settled=JSON.stringify(ui$transform);flushFrame();
      const expected=[1.188889,1.188889,133.600006,48];
      expected.forEach((value,index)=>assert(Math.abs(painted[index]-value)<2e-5));
      assert.deepEqual(saved.camera,ui$transform);assert.equal(JSON.stringify(ui$transform),settled);
      notify(observed[0]);flushFrame();assert.equal(JSON.stringify(ui$transform),settled);
      assert.equal(layouts,6,'Trailing observer delivery must reuse the settled scene, not refit it');
      assert.equal(timers.size,1);flushTimers();assert.deepEqual(saved.camera,ui$transform);
      assert.equal(frames.size,0);assert.equal(resizeFrame,null);assert.equal(JSON.stringify(data),before);
      const current=geometry();assert.equal(current.clusters.length,1);assert.equal(current.clusters[0].members.length,5);
      assert.equal(current.$display.totalPairs,5);assert.equal(current.$display.totalRoutes,5);
      assert.equal(current.routes.reduce((sum,route)=>sum+(route.$statement||route.count),0),10);
    """)


def test_same_size_sheet_change_and_late_observer_preserve_restored_camera() -> None:
    _run(r"""
      var dom$body={offsetTop:178};dom$sheetHandle={setAttribute(name,value){this[name]=value;},replaceChildren(){},focus(){}};
      const saved={x:41,y:-73,scale:1.2},before=JSON.stringify(data);restoring=true;
      setSheet(true,null);setSheet(false,null);
      assert.equal(writes.length,0,'Unchanged dimensions must not add redundant synchronous resize work');
      assert.deepEqual(ui$transform,saved);assert.equal(fitCalls,0);
      install();notify(observed[0]);flushFrame();
      assert.deepEqual(ui$transform,saved);assert.equal(fitCalls,0);assert.equal(frames.size,0);
      assert.equal(JSON.stringify(data),before);
    """)
