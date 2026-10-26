"""Execute the production observer scheduler, resize writes and disposal.

RAF/observer, canvas, accessibility and painter peers are isolated here. These
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
function original(name) {return declarationAt(source.indexOf('function '+name+'('));}
const state=source.match(/\bvar alive =[^;]+;/);assert(state,'Missing mount lifecycle state');
const bind=original('bindEvents'),begin=bind.indexOf('if (window.ResizeObserver)');
const end=bind.indexOf('globalListen(window, "resize", syncSheetAccessibility)',begin);
assert(begin>=0&&end>begin,'Missing actual observer/fallback binding');
const binding=bind.slice(begin,end);new vm.Script(binding);
const marker=source.indexOf('dispose: function () { alive = false');
assert(marker>=0,'Missing actual mount disposal');
const dispose=declarationAt(source.indexOf('function',marker));
const frames=new Map(),cancelled=[],observed=[],listeners=[];
let nextFrame=0,delivering=false;
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
const context={assert,frames,cancelled,observed,listeners,notify,flushFrame,binding,
  ResizeObserver:ObserverPeer,
  requestAnimationFrame(callback){const id=nextFrame++;frames.set(id,callback);return id;},
  cancelAnimationFrame(id){cancelled.push(id);frames.delete(id);},
  clearTimeout(){},
  canvasWrite(){assert(!delivering,'Actual resize wrote canvas during observer delivery');}
};
vm.createContext(context);
vm.runInContext(state[0]+'\n'+['queueResize','resize','compactRegions','sheetLayout','syncSheetAccessibility']
  .map(original).join('\n')+'\nvar dispose='+dispose+';',context,{timeout:1000});
vm.runInContext(`
var window={ResizeObserver,devicePixelRatio:2,matchMedia(){return {matches:false};}};
var classes=new Set(),host={classList:{contains(name){return classes.has(name);},
  toggle(name,on){if(on)classes.add(name);else classes.delete(name);}}};
var document={activeElement:null},navigation=null,dom$pageHeading=null;
var dom$inspectorInner={contains(){return false;},setAttribute(){},removeAttribute(){}},dom$sheetHandle={};
var dom$stage={clientWidth:800,clientHeight:600},writes=[],canvasWidth=0,canvasHeight=0;
var dom$canvas={
  get width(){return canvasWidth;},set width(value){canvasWrite();writes.push(['width',value]);canvasWidth=value;},
  get height(){return canvasHeight;},set height(value){canvasWrite();writes.push(['height',value]);canvasHeight=value;}
};
var ui$area=null,ui$overviewOnly=false,ui$scopeOverview=false,ui$orientation='lr',ui$mode='graph',firstResize=false;
var ui$transform={x:41,y:-73,scale:1.2},data={nodes:[{id:'actual-topic',status:'fully_proved'}],edges:[]};
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
