"""Execute the shipped local overlay against reader move/clone lifecycles.

These bounded DOM/unit checks do not replace the genuine WebKit dashboard
journey. Static publications and their mathematical source stay untouched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_ASSET = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-live.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is required for local-overlay source checks")

_HARNESS = r"""
const vm = require('vm'), assert = require('assert');
const scenario = JSON.parse(process.argv[1]), source = process.argv[2];
const observers = new Set(), timers = new Map(), requests = [], listeners = new Map();
let timerId = 0, mutations = 0;
function changed(target) {
  for (const observer of observers) {
    if (!observer.target || !observer.target.contains(target) || observer.queued) continue;
    observer.queued = true;
    Promise.resolve().then(() => {
      observer.queued = false;
      if (observer.target) { mutations++; observer.callback([]); }
    });
  }
}
class Node {
  constructor(tag, classes='', text='') {
    this.tagName=tag.toUpperCase(); this.className=classes; this.id=''; this.title='';
    this.dataset={}; this.children=[]; this.parentNode=null; this._text=text;
    this.classList={
      contains: name => this.className.split(/\s+/).includes(name),
      add: name => { if (!this.classList.contains(name)) this.className=(this.className+' '+name).trim(); },
      remove: name => { this.className=this.className.split(/\s+/).filter(value=>value!==name).join(' '); }
    };
  }
  matches(selector) {
    if (selector[0]==='.') return this.classList.contains(selector.slice(1));
    if (selector[0]==='#') return this.id===selector.slice(1);
    if (selector==='[data-autoform-node-id]') return this.dataset.autoformNodeId!==undefined;
    return this.tagName.toLowerCase()===selector.toLowerCase();
  }
  querySelectorAll(selector) {
    const result=[];
    for (const child of this.children) {
      if (child.matches(selector)) result.push(child);
      result.push(...child.querySelectorAll(selector));
    }
    return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  contains(node) { return this===node || this.children.some(child=>child.contains(node)); }
  appendChild(node) {
    node.remove(); this.children.push(node); node.parentNode=this; changed(this); return node;
  }
  insertBefore(node,before) {
    assert(this.children.includes(before)); node.remove();
    this.children.splice(this.children.indexOf(before),0,node); node.parentNode=this; changed(this); return node;
  }
  remove() {
    const parent=this.parentNode; if (!parent) return;
    parent.children.splice(parent.children.indexOf(this),1); this.parentNode=null; changed(parent);
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode=null;
    this.children=[]; this._text=''; changed(this); nodes.forEach(node=>this.appendChild(node));
  }
  get textContent() { return this._text+this.children.map(node=>node.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text=String(value); }
  set innerHTML(_value) { assert.fail('The local overlay must not interpret payload text as HTML'); }
  cloneNode(deep) {
    const node=new Node(this.tagName,this.className,this._text);
    node.id=this.id; node.title=this.title; node.dataset={...this.dataset};
    if (deep) this.children.forEach(child=>node.appendChild(child.cloneNode(true)));
    return node;
  }
}
const body=new Node('body'), wrapper=body.appendChild(new Node('div','bp-explorer-page'));
function reading(landing=true) {
  const root=new Node('div','bp-reading-source');
  const section=landing ? root.appendChild(new Node('div','bp-landing-intro')) : root;
  section.appendChild(new Node('h1','bp-hero-title','Complete mathematical exposition'));
  if (scenario.nativeActivityId) {
    const native=section.appendChild(new Node('h2','','A retained native mathematical anchor'));
    native.id='bp-live-activity';
  }
  section.appendChild(new Node('p','','All authored paragraphs, sources, and native links remain.'));
  const theorem=section.appendChild(new Node('div','bp-thmwrapper'));
  theorem.dataset.autoformNodeId='algebra/result';
  theorem.appendChild(new Node('div','bp-thmheading','A complete theorem'));
  theorem.appendChild(new Node('span','arithmatex','\(0=0\)'));
  if (landing) section.appendChild(new Node('div','bp-next-target','Next result'));
  return root;
}
function mathematicalText(node) {
  if (node.classList.contains('bp-live-badge') || node.classList.contains('bp-live-activity')) return '';
  return node._text+node.children.map(mathematicalText).join('');
}
const initial=wrapper.appendChild(reading()), expectedSource=mathematicalText(initial);
let reader=null;
function enhance(next) {
  if (!reader) reader=wrapper.appendChild(new Node('div','bp-reading-body'));
  reader.replaceChildren(next); return next;
}
const document={body,
  querySelectorAll: selector=>body.querySelectorAll(selector), querySelector: selector=>body.querySelector(selector),
  getElementById: id=>body.querySelector('#'+id),
  createElement: tag=>new Node(tag), createTextNode: text=>new Node('#text','',text)
};
const claim={node_id:'algebra/result',title:'Theorem <script>not markup</script>',owner:'worker-a',note:'<img onerror=unsafe>'};
const valid={schema:'autoform-live/v1',claims:[claim]};
const responses=scenario.responses ? [...scenario.responses] : [{status:200,payload:valid}];
const held=[];
function response(entry) {
  return {status:entry.status,ok:entry.status>=200&&entry.status<300,json:()=>Promise.resolve(entry.payload)};
}
const window={location:{hostname:scenario.hostname || '127.0.0.1'},
  setTimeout: (callback,delay)=>{ assert.equal(delay,3000); const id=++timerId; timers.set(id,callback); return id; },
  clearTimeout: id=>timers.delete(id),
  addEventListener: (name,callback)=>{ if (!listeners.has(name)) listeners.set(name,[]); listeners.get(name).push(callback); }
};
const context={window,document,MutationObserver:class {
  constructor(callback) { this.callback=callback; this.target=null; this.queued=false; }
  observe(target,options) { assert.deepEqual(options,{childList:true,subtree:true}); this.target=target; observers.add(this); }
  disconnect() { this.target=null; observers.delete(this); }
},fetch:(url,options)=>{
  requests.push({url,options}); assert.equal(url,'/__autoform/live.json'); assert.equal(options.cache,'no-store');
  const entry=responses.shift() || {status:200,payload:valid};
  if (entry.networkError) return Promise.reject(new Error('network unavailable'));
  if (entry.held) return new Promise(resolve=>held.push(()=>resolve(response(entry))));
  return Promise.resolve(response(entry));
}};
vm.createContext(context);
function execute() { vm.runInContext(source,context,{timeout:1000}); }
async function settle() { for (let i=0;i<8;i++) await new Promise(resolve=>setImmediate(resolve)); }
function dispatch(name,event={}) { (listeners.get(name)||[]).forEach(callback=>callback(event)); }
function panel() { return document.querySelector('.bp-live-activity'); }
function assertPanel(target) {
  assert.equal(document.querySelectorAll('#bp-live-activity').length,1,'one live panel must exist');
  assert.strictEqual(panel().parentNode,target.querySelector('.bp-landing-intro'));
  const next=panel().parentNode.querySelector('.bp-next-target');
  assert(panel().parentNode.children.indexOf(panel())<panel().parentNode.children.indexOf(next));
  assert.equal(mathematicalText(target),expectedSource,'the full mathematical source changed');
}
function poll() {
  assert.equal(timers.size,1); const [id,callback]=timers.entries().next().value; timers.delete(id); callback();
}
(async()=>{
  if (scenario.mode==='active-first') enhance(reading());
  execute(); await settle();
  if (scenario.mode==='inert') {
    assert.equal(document.querySelectorAll('#bp-live-activity').length,scenario.nativeActivityId ? 1 : 0);
    assert.equal(document.querySelectorAll('.bp-live-activity').length,0);
    assert.equal(document.querySelectorAll('.bp-live-badge').length,0);
    assert.equal(observers.size,0); assert.equal(requests.length,scenario.requestCount);
    assert.equal(timers.size,scenario.timerCount || 0);
    assert.equal(mathematicalText(initial),expectedSource);
    console.log('PASS inert'); return;
  }
  if (scenario.mode==='stale') {
    assert.equal(held.length,1); dispatch('pagehide');
    dispatch('pageshow',{persisted:true}); await settle(); assertPanel(initial);
    held[0](); await settle(); assertPanel(initial);
    assert.equal(document.querySelector('.bp-live-row').textContent,claim.title+' — '+claim.owner);
    console.log('PASS stale'); return;
  }
  assertPanel(reader || initial);
  if (scenario.mode==='relocate' || scenario.mode==='active-first') {
    const next=enhance(initial.cloneNode(true)); await settle(); assertPanel(next);
    assert.equal(initial.querySelectorAll('#bp-live-activity').length,0);
    assert.equal(document.querySelectorAll('.bp-live-badge').length,2);
    assert.equal(document.querySelector('.bp-live-badge').title,claim.note);
    assert(panel().textContent.includes(claim.title));
    enhance(reading(false)); await settle();
    assert.equal(document.querySelectorAll('#bp-live-activity').length,0,'hidden SSR landing must not win over the active article');
    const back=enhance(initial.cloneNode(true)); await settle(); assertPanel(back);
    assert.equal(requests.length,1,'soft reader navigation must reuse local state without another request');
    assert.equal(observers.size,1); assert.equal(timers.size,1); assert(mutations<30,'own mutations caused a render loop');
  } else if (scenario.mode==='states') {
    assert(panel().textContent.includes('No active claims')); poll(); await settle();
    assert(panel().textContent.includes('<source failure>')); poll(); await settle();
    assert(panel().textContent.includes('Live overlay unavailable')); assert.equal(document.querySelectorAll('.bp-live-badge').length,0);
    const cached=initial.cloneNode(true);
    poll(); await settle(); assert.equal(panel(),null); assert.equal(timers.size,0);
    enhance(cached); await settle(); assert.equal(panel(),null,'Back resurrected a stale cached overlay');
    assert.equal(document.querySelectorAll('.bp-live-badge').length,0);
    dispatch('pagehide'); await settle(); assert.equal(observers.size,0);
  } else if (scenario.mode==='lifecycle') {
    execute(); await settle(); assert.equal(requests.length,1); assert.equal(timers.size,1); assert.equal(observers.size,1);
    dispatch('pagehide'); await settle(); assert.equal(panel(),null); assert.equal(timers.size,0); assert.equal(observers.size,0);
    assert.equal(document.querySelectorAll('.bp-live-badge').length,0);
    const resumed=enhance(reading()); dispatch('pageshow',{persisted:true}); await settle();
    assertPanel(resumed); assert.equal(requests.length,2); assert.equal(timers.size,1); assert.equal(observers.size,1);
  }
  assert.equal(mathematicalText(initial),expectedSource);
  console.log('PASS '+scenario.mode);
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""


def _run(scenario: dict, *, source: str | None = None) -> None:
    result = subprocess.run(
        [_NODE, "-e", _HARNESS, json.dumps(scenario), source or _ASSET.read_text(encoding="utf-8")],
        capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-5000:]


@pytest.mark.parametrize("mode", ("relocate", "active-first", "lifecycle"))
def test_live_activity_follows_the_complete_reader_without_duplicate_panels(mode: str) -> None:
    _run({"mode": mode})


def test_dashboard_empty_error_and_removed_service_states_stay_local() -> None:
    _run({"mode": "states", "responses": [
        {"status": 200, "payload": {"schema": "autoform-live/v1", "claims": []}},
        {"status": 200, "payload": {"schema": "autoform-live/v1", "claims": [], "error": "<source failure>"}},
        {"status": 503}, {"status": 404},
    ]})


@pytest.mark.parametrize("scenario", [
    {"mode": "inert", "hostname": "deicyde.github.io", "requestCount": 0},
    {"mode": "inert", "hostname": "example.org", "requestCount": 0},
    {"mode": "inert", "responses": [{"status": 404}], "requestCount": 1},
    {"mode": "inert", "nativeActivityId": True, "responses": [{"status": 404}], "requestCount": 1},
    {"mode": "inert", "responses": [{"status": 503}], "requestCount": 1, "timerCount": 1},
    {"mode": "inert", "responses": [{"networkError": True}], "requestCount": 1, "timerCount": 1},
    {"mode": "inert", "responses": [{"status": 200, "payload": {"claims": []}}], "requestCount": 1},
])
def test_public_and_static_preview_pages_do_not_acquire_live_state(scenario: dict) -> None:
    _run(scenario)


def test_a_pre_pagehide_response_cannot_replace_resumed_live_state() -> None:
    _run({"mode": "stale", "responses": [
        {"status": 200, "held": True, "payload": {"schema": "autoform-live/v1", "claims": []}},
    ]})
