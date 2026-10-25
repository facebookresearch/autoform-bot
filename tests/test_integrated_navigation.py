"""Isolated router behavior with native Node promises/URLs/abort signals, not a browser substitute."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated navigation tests")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-navigation.js"

_HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class Element {
  constructor(tag, attrs = {}, children = [], text = '') {
    this.tagName = tag.toUpperCase(); this.attrs = {...attrs}; this.children = children;
    this.text = text; this.listeners = new Map(); this.namespaceURI = 'http://www.w3.org/1999/xhtml';
    children.forEach(child => child.parent = this);
  }
  get attributes() { return Object.entries(this.attrs).map(([name,value]) => ({name,value})); }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  get href() { return this.attrs.href; } get target() { return this.attrs.target || ''; }
  getAttribute(name) { return this.attrs[name] === undefined ? null : this.attrs[name]; }
  setAttribute(name, value) { this.attrs[name] = value; }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return this.attrs[name] !== undefined; }
  matches(selector) {
    if (selector === 'a[href]') return this.tagName === 'A' && this.hasAttribute('href');
    if (selector.startsWith('.')) return (this.attrs.class || '').split(' ').includes(selector.slice(1));
    return this.tagName.toLowerCase() === selector;
  }
  all() { return this.children.flatMap(child => [child, ...child.all()]); }
  querySelector(selector) { return this.all().find(child => child.matches(selector)) || null; }
  querySelectorAll(selector) {
    return selector === '*' ? this.all() : this.all().filter(child => selector.split(',').some(part => child.matches(part)));
  }
  closest(selector) { return this.matches(selector) ? this : this.parent && this.parent.closest(selector); }
  contains(node) { return node === this || this.all().includes(node); }
  cloneNode() { return new Element(this.tagName, this.attrs, this.children.map(child => child.cloneNode()), this.text); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  removeEventListener(type) { this.listeners.delete(type); }
}
const base = 'https://example.test/project/';
const absolute = path => new URL(path, base).href;
function page(spec) {
  const reading = new Element('div', {'class':'bp-reading-source', 'data-autoform-math-source':'\(L \neq 0\)', 'onclick':'bad()'}, [
    new Element('h1', {}, [], spec.title), new Element('p', {}, [], spec.prose || 'Complete source statement and proof.'),
    new Element('a', {href:'b.html#theorem-b', onclick:'bad()'}, [], 'B theorem'),
    new Element('img', {src:'figures/chart.svg', srcset:'figures/chart.png 1x, figures/chart@2x.png 2x'}),
    new Element('a', {href:'https://outside.test/paper.pdf'}, [], 'Source paper'),
    new Element('a', {href:'javascript:bad()'}, [], 'Bad link'), new Element('script', {}, [], 'bad()')
  ]);
  const host = new Element('div', {'class':'bp-dag-viewer','data-page-href':spec.route,
    'data-graph-src':spec.data,'data-catalog-src':spec.catalog || '../dependencies/index.json','data-scope-id':spec.scope});
  const root = new Element('main', {'class':'bp-explorer-page','data-page-title':spec.title}, [reading,host]);
  const doc = new Element('html', {}, [root]); doc.title = spec.title;
  return {doc,root,host,reading};
}
global.DOMParser = class { parseFromString(text) { return page(JSON.parse(text)).doc; } };
const graph = {schema:'autoform-dag-view/v2', palette:{planned:{}}, title:'Graph',
  nodes:[{id:'a',title:'A theorem',url:'../roadmap/a.html#theorem-a'}, {id:'b',title:'B theorem'}],
  edges:[{source:'a',target:'b',statement:1,proof:0}],view:{book:'../roadmap/index.html'}};
function index() { return {schema:'autoform-dag-search/v1',nodes:[
  {id:'roadmap',title:'Root',kind:'scope',result:false,url:'../dependencies.html',route:'../roadmap/index.html',data_url:'../dependencies.json',scope:'roadmap'},
  {id:'a',title:'A theorem',kind:'node',result:true,url:'scopes/a.html#node=a',route:'../roadmap/a.html#theorem-a',data_url:'a.json',scope:'a',summary:'Not the complete article'},
  {id:'b',title:'B theorem',kind:'node',result:true,url:'scopes/b.html#node=b',route:'../roadmap/b.html#theorem-b',data_url:'b.json',scope:'b'}
],relations:[['a','b',1,0]]}; }
const rootSpec = {title:'Root',route:absolute('roadmap/index.html'),data:'../dependencies.json',scope:'roadmap'};
const aSpec = {title:'A topic',route:absolute('roadmap/a.html'),data:'../dependencies/a.json',scope:'a'};
const bSpec = {title:'B topic',route:absolute('roadmap/b.html'),data:'../dependencies/b.json',scope:'b'};
function setup(extra = {}) {
  const initial = page(extra.initial || rootSpec), listeners = new Map(), commits = [], errors = [], restored = [], fetches = [];
  let state = {mode:'map',readingScroll:0}, pointer = 0;
  const entries = [{url:extra.url || rootSpec.route,state:{material:'preserved'}}];
  const win = {location:{href:entries[0].url},
    addEventListener(type, cb) { listeners.set(type, cb); }, removeEventListener(type) { listeners.delete(type); }};
  win.history = {
    scrollRestoration: extra.scrollRestoration || 'auto',
    get state() { return entries[pointer].state; }, get length() { return entries.length; },
    replaceState(value, _title, url) { entries[pointer] = {url,state:structuredClone(value)}; win.location.href = url; },
    pushState(value, _title, url) { entries.splice(pointer+1); entries.push({url,state:structuredClone(value)}); pointer++; win.location.href=url; },
    back() { pointer--; win.location.href=entries[pointer].url; if (listeners.has('popstate')) listeners.get('popstate')(); },
    forward() { pointer++; win.location.href=entries[pointer].url; if (listeners.has('popstate')) listeners.get('popstate')(); }
  };
  const contents = new Map([
    [absolute('dependencies/index.json'),index()], [absolute('dependencies.json'),graph],
    [absolute('dependencies/a.json'),graph], [absolute('dependencies/b.json'),graph],
    [rootSpec.route,rootSpec], [aSpec.route,aSpec], [bSpec.route,bSpec]
  ]);
  function reply(value, url, status=200) { return {ok:status===200,status,url,headers:{get(){return null;}},
    text:async()=>JSON.stringify(value)}; }
  const fetch = async (url, settings) => { fetches.push(url);
    if (extra.fetch) { const overridden = extra.fetch(url,settings,reply); if (overridden !== undefined) return overridden; }
    return reply(contents.get(url),url,contents.has(url)?200:404);
  };
  global.window=win; global.document=initial.doc;
  vm.runInThisContext(fs.readFileSync(CLIENT,'utf8'));
  const nav=win.AutoformNavigation.create({host:initial.host,window:win,document:initial.doc,fetch,
    onCommit(bundle){commits.push(bundle); if(extra.commit) return extra.commit(bundle);},onError(error){errors.push(error);},
    captureState(){return structuredClone(state);}, restoreState(saved,bundle){state=structuredClone(saved);restored.push(saved);
      if(extra.restore) return extra.restore(saved,bundle);},
    onIndex:extra.onIndex,
    htmlEntries:extra.htmlEntries || 8,jsonEntries:16,cacheBytes:extra.cacheBytes});
  return {nav,win,commits,errors,restored,fetches,contents,reply,initial,listeners,
    state(value){state=value;},getState(){return state;}};
}
async function until(predicate) { for(let n=0;n<100;n++) {if(predicate()) return; await new Promise(resolve=>setImmediate(resolve));}
  throw new Error('Controller did not settle'); }
function addLargeScope(app) {
  const spec={title:'Large scope',route:absolute('roadmap/large.html'),data:'../dependencies/large.json',scope:'large'};
  const catalog=index();catalog.nodes.push({id:'large',title:'Large scope',kind:'scope',result:false,
    url:'../roadmap/large.html',route:'../roadmap/large.html',data_url:'large.json',scope:'large',summary:'Only a summary'});
  app.contents.set(absolute('dependencies/index.json'),catalog);app.contents.set(spec.route,spec);
  app.contents.set(absolute('dependencies/large.json'),{...graph,title:'Large scope',edges:[],
    nodes:Array.from({length:42},(_,n)=>({id:'large-'+n,title:'Scope item '+n,kind:'node',status:'planned',
      area:n<21?'Authored area A':'Authored area B',member_count:1,status_counts:[['planned',1]]}))});
  return spec;
}
"""


_PAINT_PEERS = r"""
function paintCatalogue(app) {
  const nodes=Array.from({length:1025},(_,n)=>({id:'paint-'+n,title:'Paint item '+n,kind:'node',scope:'roadmap',
    route:'../roadmap/paint-'+n+'.html',status:'planned'}));
  const payload={schema:'autoform-dag-search/v1',nodes,relations:[[0,1024,2,3],[512,0,0,1]]};
  app.contents.set(absolute('dependencies/index.json'),payload);return payload;
}
function controlledPaint(app) {
  const nativeTimer=global.setTimeout,nativeClear=global.clearTimeout;
  const frames=new Map(),requested=[],cancelled=[],timers=new Map();let serial=0;
  app.initial.doc.visibilityState='visible';
  app.win.requestAnimationFrame=callback=>{const id=serial++;frames.set(id,callback);requested.push(id);return id;};
  app.win.cancelAnimationFrame=id=>{cancelled.push(id);frames.delete(id);};
  global.setTimeout=(callback,delay,...args)=>{
    if(delay!==50)return nativeTimer(callback,delay,...args);
    const token={controlledPaintTimer:true};timers.set(token,()=>callback(...args));return token;
  };
  global.clearTimeout=token=>{if(timers.has(token))timers.delete(token);else nativeClear(token);};
  return {frames,requested,cancelled,timers,
    release(id){const callback=frames.get(id);assert(callback,'The real normalizer must request this frame');frames.delete(id);callback(16);},
    restore(){global.setTimeout=nativeTimer;global.clearTimeout=nativeClear;frames.clear();timers.clear();}};
}
function assertPaintIndex(app,result,published) {
  assert.equal(result.nodes.length,1025);assert.deepEqual(result.nodes.map(node=>node.id),Array.from({length:1025},(_,n)=>'paint-'+n));
  assert.deepEqual(result.relations,[['paint-0','paint-1024',2,3],['paint-512','paint-0',0,1]]);
  assert.equal(published.length,1);assert.strictEqual(published[0],result);assert.strictEqual(app.nav.getIndex(),result);
  assert.equal(app.fetches.filter(url=>url===absolute('dependencies/index.json')).length,1);assert.equal(app.errors.length,0);
}
"""


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _HARNESS
    script += "\n(async()=>{\n" + body + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_visible_index_batches_await_real_window_frame_boundaries_before_publication() -> None:
    _run(_PAINT_PEERS + r"""
      const published=[],app=setup({onIndex(value){published.push(value);}});paintCatalogue(app);
      const paint=controlledPaint(app);
      try {
        const pending=app.nav.loadIndex();assert.strictEqual(app.nav.loadIndex(),pending);
        await until(()=>paint.frames.size===1);
        assert.deepEqual(paint.requested,[0]);assert.equal(paint.timers.size,1);
        assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
        await new Promise(resolve=>setImmediate(resolve));
        assert.deepEqual(paint.requested,[0],'The next batch must wait for the first actual frame callback');
        paint.release(0);await until(()=>paint.frames.size===1);
        assert.deepEqual(paint.requested,[0,1]);assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
        await new Promise(resolve=>setImmediate(resolve));
        assert.deepEqual(paint.requested,[0,1],'Publication must still wait for the second frame');
        paint.release(1);const result=await pending;assertPaintIndex(app,result,published);
        assert.equal(paint.frames.size,0);assert.equal(paint.timers.size,0);assert.deepEqual(paint.cancelled,[0,1]);
      } finally {app.nav.dispose();paint.restore();}
    """)


@pytest.mark.parametrize("kind", ("absent", "stalled"))
def test_index_frame_fallback_completes_with_absent_or_stalled_window_frames(kind: str) -> None:
    _run(_PAINT_PEERS + "const kind=" + json.dumps(kind) + ";\n" + r"""
      const published=[],app=setup({onIndex(value){published.push(value);}});paintCatalogue(app);
      app.initial.doc.visibilityState='visible';const callbacks=[],cancelled=[],delays=[];
      if(kind==='stalled') {
        app.win.requestAnimationFrame=callback=>{callbacks.push(callback);return callbacks.length-1;};
        app.win.cancelAnimationFrame=id=>cancelled.push(id);
      }
      const nativeTimer=global.setTimeout;let fallbackFired=0;
      global.setTimeout=(callback,delay,...args)=>{
        if(delay===0||delay===50)delays.push(delay);
        return nativeTimer(()=>{if(delay===50)fallbackFired++;callback(...args);},delay);
      };
      try {
        const result=await app.nav.loadIndex();assertPaintIndex(app,result,published);
        assert.deepEqual(delays,kind==='stalled'?[50,50]:[0,0]);
        assert.equal(fallbackFired,kind==='stalled'?2:0);
        if(kind==='stalled') {
          assert.deepEqual(cancelled,[0,1]);callbacks.forEach(callback=>callback(20));
          await new Promise(resolve=>setImmediate(resolve));assert.equal(published.length,1);
          assert.strictEqual(app.nav.getIndex(),result,'Late cancelled frames must not republish or replace the index');
        }
      } finally {app.nav.dispose();global.setTimeout=nativeTimer;}
    """)


def test_hidden_index_batches_never_depend_on_window_animation_frames() -> None:
    _run(_PAINT_PEERS + r"""
      const published=[],app=setup({onIndex(value){published.push(value);}});paintCatalogue(app);
      app.initial.doc.visibilityState='hidden';
      app.win.requestAnimationFrame=()=>{throw new Error('A hidden document must not wait for a paint');};
      app.win.cancelAnimationFrame=()=>{throw new Error('A hidden document has no paint handle');};
      const nativeTimer=global.setTimeout,delays=[];
      global.setTimeout=(callback,delay,...args)=>{if(delay===0||delay===50)delays.push(delay);return nativeTimer(callback,delay,...args);};
      try {const result=await app.nav.loadIndex();assertPaintIndex(app,result,published);assert.deepEqual(delays,[0,0]);}
      finally {app.nav.dispose();global.setTimeout=nativeTimer;}
    """)


def test_disposal_while_index_frame_is_pending_never_exposes_a_partial_index() -> None:
    _run(_PAINT_PEERS + r"""
      const published=[],app=setup({onIndex(value){published.push(value);}});paintCatalogue(app);
      const paint=controlledPaint(app);
      try {
        const pending=app.nav.loadIndex();await until(()=>paint.frames.size===1);
        assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
        app.nav.dispose();paint.release(0);
        await assert.rejects(pending,error=>error.name==='AbortError');
        assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);assert.equal(app.errors.length,0);
        assert.deepEqual(paint.requested,[0]);assert.equal(paint.frames.size,0);assert.equal(paint.timers.size,0);
      } finally {app.nav.dispose();paint.restore();}
    """)


def test_canonical_node_navigation_keeps_complete_ssr_and_rebases_reading() -> None:
    _run(r"""
      const app=setup(); await app.nav.start(); const bundle=await app.nav.navigateNode('a');
      assert.equal(bundle.url,absolute('roadmap/a.html#theorem-a'));
      assert.equal(bundle.title,'A theorem'); assert.equal(bundle.scope,'a'); assert.equal(bundle.anchor,'theorem-a');
      assert(bundle.readingSource.textContent.includes('Complete source statement and proof.'));
      assert(!bundle.readingSource.textContent.includes('Not the complete article'));
      assert.equal(bundle.readingSource.getAttribute('data-autoform-math-source'),'\(L \neq 0\)');
      assert.equal(bundle.readingSource.getAttribute('onclick'),null);
      assert.equal(bundle.readingSource.querySelector('script'),null);
      assert.equal(bundle.readingSource.querySelector('a').href,absolute('roadmap/b.html#theorem-b'));
      assert.equal(bundle.readingSource.querySelector('img').getAttribute('src'),absolute('roadmap/figures/chart.svg'));
      assert.equal(bundle.readingSource.querySelector('img').getAttribute('srcset'),
        absolute('roadmap/figures/chart.png')+' 1x, '+absolute('roadmap/figures/chart@2x.png')+' 2x');
      assert.equal(bundle.readingSource.querySelectorAll('a').at(-1).getAttribute('href'),null);
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);
      const length=app.win.history.length; await app.nav.navigateNode('a'); assert.equal(app.win.history.length,length);
      app.nav.dispose();
    """)


def test_scope_map_first_transport_is_independent_of_explicit_reading_focus() -> None:
    _run(r"""
      const app=setup(),spec=addLargeScope(app);await app.nav.start();
      const mapped=await app.nav.navigateNode('large',{history:'push',focus:'reading',mapFirst:true});
      assert.equal(mapped.url,spec.route);assert.equal(mapped.nodeId,'large');assert.equal(mapped.scope,'large');
      assert.equal(mapped.focus,true);assert.equal(mapped.mapFirst,true);assert.equal(mapped.graph.nodes.length,42);
      assert(mapped.readingSource.textContent.includes('Complete source statement and proof.'));
      assert(!mapped.readingSource.textContent.includes('Only a summary'));
      assert.equal(mapped.readingSource.getAttribute('data-autoform-math-source'),'\(L \neq 0\)');
      const reading=await app.nav.navigateNode('large',{focus:true,mapFirst:false});
      assert.equal(reading.focus,true);assert.equal(reading.mapFirst,false);assert.equal(reading.url,spec.route);
      const result=await app.nav.navigateNode('b');assert.equal(result.focus,true);assert.equal(result.mapFirst,false);
      const strict=await app.nav.navigateNode('large',{focus:true,mapFirst:1});
      assert.equal(strict.mapFirst,false,'A truthy non-boolean cannot acquire map-first intent');
      assert.equal(strict.focus,true);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_reading_links_to_large_scopes_request_reading_focus_and_leave_external_links_native() -> None:
    _run(r"""
      const app=setup(),spec=addLargeScope(app);await app.nav.start();
      const opened=await app.nav.navigateNode('a'),root=opened.readingSource;
      const scopeLink=new Element('a',{href:spec.route},[],'Large scope');root.children.push(scopeLink);scopeLink.parent=root;
      app.nav.bindReader(root);const listener=root.listeners.get('click');
      const event=(target,extra={})=>Object.assign({target,button:0,defaultPrevented:false,
        preventDefault(){this.defaultPrevented=true;},stopPropagation(){this.stopped=true;}},extra);
      const requests=app.fetches.length,commits=app.commits.length;
      for(const settings of [{metaKey:true},{ctrlKey:true},{shiftKey:true},{altKey:true},{button:1}]) {
        const modified=event(scopeLink,settings);listener(modified);assert.equal(modified.defaultPrevented,false);
      }
      const external=root.querySelectorAll('a').find(link=>link.href==='https://outside.test/paper.pdf');
      assert(external);const outside=event(external);listener(outside);assert.equal(outside.defaultPrevented,false);
      assert.equal(app.fetches.length,requests);assert.equal(app.commits.length,commits);
      const ordinary=event(scopeLink);listener(ordinary);assert.equal(ordinary.defaultPrevented,true);assert.equal(ordinary.stopped,true);
      await until(()=>app.commits.at(-1).nodeId==='large');const bundle=app.commits.at(-1);
      assert.equal(bundle.focus,true,'A scope reading link must not inherit an overview-default focus=false');
      assert.equal(bundle.mapFirst,false);assert.equal(bundle.url,spec.route);assert.equal(bundle.graph.nodes.length,42);
      assert(bundle.readingSource.textContent.includes('Complete source statement and proof.'));
      assert.equal(bundle.readingSource.getAttribute('data-autoform-math-source'),'\(L \neq 0\)');
      assert.equal(app.errors.length,0);app.nav.dispose();assert.equal(root.listeners.has('click'),false);
    """)


def test_delayed_old_response_cannot_commit_or_replace_newer_navigation() -> None:
    _run(r"""
      let release, requested=false;
      const app=setup({fetch(url,_settings,reply){if(url===aSpec.route){requested=true;return new Promise(resolve=>release=()=>resolve(reply(aSpec,url)));}}});
      await app.nav.start(); const old=app.nav.navigateNode('a'); await until(()=>requested);
      const newer=await app.nav.navigateNode('b'); release(); await old;
      assert.equal(newer.nodeId,'b'); assert.equal(app.commits.at(-1).nodeId,'b');
      assert.equal(app.win.location.href,absolute('roadmap/b.html#theorem-b')); assert.equal(app.errors.length,0);
      app.nav.dispose();
    """)


def test_checkpoint_and_back_restore_opaque_shell_state_without_new_urls() -> None:
    _run(r"""
      const app=setup(); await app.nav.start(); await app.nav.navigateNode('a');
      app.state({mode:'sources',camera:{x:12,y:24,scale:1.2},readingScroll:80});
      const url=app.win.location.href,length=app.win.history.length;
      app.nav.checkpoint(); assert.equal(app.win.location.href,url); assert.equal(app.win.history.length,length);
      assert.equal(app.win.history.state.material,'preserved');
      await app.nav.navigateNode('b'); app.state({mode:'map',readingScroll:0}); app.win.history.back();
      await until(()=>app.restored.length>0);
      assert.equal(app.win.location.href,url); assert.equal(app.getState().mode,'sources');
      assert.equal(app.getState().readingScroll,80); assert.equal(app.getState().camera.x,12);
      const before=app.win.history.length; app.nav.checkpoint('push');
      // A deliberate action after Back truncates the forward branch before adding its entry.
      assert.equal(app.win.history.length,before); assert.equal(app.win.location.href,url);
      app.nav.dispose();
    """)


def test_back_then_forward_captures_outgoing_edits_without_waiting_for_a_checkpoint() -> None:
    _run(r"""
      const app=setup(); await app.nav.start(); await app.nav.navigateNode('a');
      app.state({mode:'sources',camera:{x:12,y:24,scale:1.2},readingScroll:80,outlineOpen:false});
      await app.nav.navigateNode('b');
      const bKey=app.win.history.state.autoformNavigation.key,length=app.win.history.length;
      const edited={mode:'list',camera:{x:190,y:-55,scale:.9},readingScroll:340,outlineScroll:120,
        query:'changed before Back',outlineOpen:true};
      app.state(edited); // No checkpoint, timeout or new explicit route before native Back.
      app.win.history.back();
      assert.equal(app.win.location.href,absolute('roadmap/a.html#theorem-a'));
      await until(()=>app.commits.at(-1).nodeId==='a' && app.getState().mode==='sources');
      assert.equal(app.getState().readingScroll,80);
      const aKey=app.win.history.state.autoformNavigation.key; assert.notEqual(aKey,bKey);
      app.win.history.forward();
      assert.equal(app.win.location.href,absolute('roadmap/b.html#theorem-b'));
      await until(()=>app.commits.at(-1).nodeId==='b' && app.getState().query===edited.query);
      assert.deepEqual(app.getState(),edited);
      assert.equal(app.win.history.state.autoformNavigation.key,bKey);
      assert.equal(app.win.history.state.material,'preserved'); assert.equal(app.win.history.length,length);
      app.nav.dispose();
    """)


def test_distinct_same_url_views_restore_by_entry_key_not_url() -> None:
    _run(r"""
      const app=setup(); app.state({mode:'graph',area:null,camera:{x:20,y:20,scale:1}});
      await app.nav.start(); const url=app.win.location.href,firstKey=app.win.history.state.autoformNavigation.key;
      app.state({mode:'graph',area:'Geometry',camera:{x:40,y:60,scale:.8}});
      app.nav.checkpoint('push');
      const secondKey=app.win.history.state.autoformNavigation.key,length=app.win.history.length;
      assert.notEqual(firstKey,secondKey); assert.equal(app.win.location.href,url);
      const edited={mode:'graph',area:'Geometry',camera:{x:100,y:200,scale:1.3},readingScroll:90,outlineOpen:true};
      app.state(edited); app.win.history.back();
      await until(()=>app.getState().area===null);
      assert.equal(app.win.location.href,url); assert.equal(app.win.history.state.autoformNavigation.key,firstKey);
      app.win.history.forward(); await until(()=>app.getState().camera.x===100);
      assert.deepEqual(app.getState(),edited); assert.equal(app.win.location.href,url);
      assert.equal(app.win.history.state.autoformNavigation.key,secondKey);
      assert.equal(app.win.history.length,length); assert.equal(app.win.history.state.material,'preserved');
      app.nav.dispose();
    """)


def test_snapshot_byte_bounds_evict_without_empty_cache_errors_and_fall_back_to_history() -> None:
    _run(r"""
      const tiny=setup({cacheBytes:1024}); await tiny.nav.start();
      for (let n=0;n<6;n++) {tiny.state({step:n,padding:'x'.repeat(650)}); assert(tiny.nav.checkpoint('push'));}
      tiny.win.history.back(); await until(()=>tiny.getState().step===4);
      assert.equal(tiny.errors.length,0); tiny.nav.dispose();

      const app=setup(); await app.nav.start(); await app.nav.navigateNode('a'); await app.nav.navigateNode('b');
      app.state({mode:'recent bounded edit',readingScroll:71}); app.win.history.back();
      await until(()=>app.commits.at(-1).nodeId==='a');
      app.win.history.forward(); await until(()=>app.getState().mode==='recent bounded edit');
      // An oversized outgoing snapshot must not retain a previous cache value or exceed the byte cap.
      app.state({mode:'oversized',padding:'x'.repeat(300000)}); app.win.history.back();
      await until(()=>app.commits.at(-1).nodeId==='a');
      app.win.history.forward(); await until(()=>app.commits.at(-1).nodeId==='b');
      assert.equal(app.getState().mode,'recent bounded edit'); assert.equal(app.getState().readingScroll,71);
      assert.equal(app.errors.length,0); app.nav.dispose();
    """)


def test_native_scroll_restoration_is_owned_only_after_commit_and_released_without_overriding_other_owners() -> None:
    _run(r"""
      const app=setup(); assert.equal(app.win.history.scrollRestoration,'auto');
      await app.nav.start(); assert.equal(app.win.history.scrollRestoration,'manual');
      app.nav.dispose(); assert.equal(app.win.history.scrollRestoration,'auto');
      let release; const delayed=setup({commit(){return new Promise(resolve=>{release=resolve;});}});
      const waiting=delayed.nav.start(); await until(()=>delayed.commits.length===1);
      assert.equal(delayed.win.history.scrollRestoration,'manual'); // The renderer's readiness is still pending.
      release(); await waiting; delayed.nav.dispose(); assert.equal(delayed.win.history.scrollRestoration,'auto');
      const existing=setup({scrollRestoration:'manual'}); await existing.nav.start(); existing.nav.dispose();
      assert.equal(existing.win.history.scrollRestoration,'manual');
      const changed=setup(); await changed.nav.start(); changed.win.history.scrollRestoration='auto'; changed.nav.dispose();
      assert.equal(changed.win.history.scrollRestoration,'auto');
      const failure=setup({fetch(_url,_settings,reply){return reply({},rootSpec.route,503);}});
      assert.equal(await failure.nav.start(),null); assert.equal(failure.win.history.scrollRestoration,'auto');
      failure.nav.dispose();
    """)


def test_alias_notice_loads_canonical_article_and_keeps_legacy_selection() -> None:
    _run(r"""
      const alias={title:'Old explorer',route:aSpec.route,data:'../../dependencies/a.json',catalog:'../index.json',scope:'a',prose:'Reading notice only'};
      const app=setup({initial:alias,url:absolute('dependencies/scopes/a.html#node=a')});
      const bundle=await app.nav.start(); assert.equal(bundle.url,absolute('roadmap/a.html#theorem-a'));
      assert.equal(bundle.nodeId,'a'); assert(bundle.readingSource.textContent.includes('Complete source statement'));
      assert(!bundle.readingSource.textContent.includes('Reading notice only')); assert.equal(app.win.history.length,1);
      app.nav.dispose();
      for (const [requested,id,path] of [
        ['scope:Geometry','Geometry','roadmap/geometry.html'],
        ['boundary:Geometry','Geometry','roadmap/geometry.html'],
        ['scope:Geometry','scope:Geometry','roadmap/exact-scope.html'],
        ['boundary:Geometry','boundary:Geometry','roadmap/exact-boundary.html']
      ]) {
        const initial={...rootSpec,data:'dependencies.json',catalog:'dependencies/index.json',prose:'Reading notice only'};
        const scoped=setup({initial,url:absolute('dependencies.html#node='+encodeURIComponent(requested))});
        const source={title:'Geometry',route:absolute(path),data:'../dependencies/geometry.json',scope:id};
        const catalog=index();
        catalog.nodes.push({id:'Geometry',title:'Geometry',kind:'scope',result:false,
          url:'../dependencies.html#node=scope%3AGeometry',route:'../roadmap/geometry.html',
          data_url:'geometry.json',scope:'Geometry'});
        if (id !== 'Geometry') catalog.nodes.push({id,title:'Exact authored ID',kind:'scope',result:false,
          url:'../dependencies.html#node='+encodeURIComponent(id),route:'../'+path,data_url:'geometry.json',scope:id});
        scoped.contents.set(absolute('dependencies/index.json'),catalog);
        scoped.contents.set(source.route,source); scoped.contents.set(absolute('dependencies/geometry.json'),graph);
        const opened=await scoped.nav.start();
        assert.equal(opened.nodeId,id); assert.equal(opened.url,source.route);
        assert(!opened.readingSource.textContent.includes('Reading notice only'));
        const reopened=await scoped.nav.navigateNode(requested);
        assert.equal(reopened.nodeId,id); assert.equal(reopened.url,source.route);
        assert.equal(scoped.win.history.length,1); scoped.nav.dispose();
      }
    """)


def test_failed_reading_request_leaves_url_and_current_shell_unchanged() -> None:
    _run(r"""
      const app=setup({fetch(url,_settings,reply){if(url===aSpec.route)return reply({},url,503);}});
      await app.nav.start(); const length=app.commits.length,url=app.win.location.href;
      assert.equal(await app.nav.navigateNode('a'),null); assert.equal(app.commits.length,length);
      assert.equal(app.win.location.href,url); assert.equal(app.errors.length,1); app.nav.dispose();
    """)


def test_indexed_legacy_relations_and_bounded_html_cache_remain_compatible() -> None:
    _run(r"""
      const app=setup({htmlEntries:1}); const legacy=index(); legacy.relations=[[1,2,1,0]];
      app.contents.set(absolute('dependencies/index.json'),legacy);
      await app.nav.start(); await app.nav.navigateNode('a'); await app.nav.navigateNode('b'); await app.nav.navigateNode('a');
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);
      assert.equal(app.fetches.filter(url=>url===aSpec.route).length,2);
      assert.equal(app.fetches.filter(url=>url===absolute('dependencies/a.json')).length,1);
      assert.equal(app.fetches.filter(url=>url===absolute('dependencies/index.json')).length,1);
      app.nav.dispose(); assert.equal(app.listeners.has('popstate'),false);
    """)


def test_compact_index_endpoints_round_trip_and_invalid_offsets_fail_closed() -> None:
    _run(r"""
      const app=setup(),compact=index(); compact.relations=[[0,1,1,0],['a',2,0,3]];
      app.contents.set(absolute('dependencies/index.json'),compact);
      await app.nav.start();
      assert.deepEqual(app.nav.getIndex().relations,[['roadmap','a',1,0],['a','b',0,3]]);
      assert.equal(app.errors.length,0); app.nav.dispose();
      for(const row of [[-1,1,1,0],[3,1,1,0],[true,1,1,0],[.5,1,1,0],[0,1,-1,0],[0,1,0,0]]) {
        const invalid=setup(),payload=index(); payload.relations=[row];
        invalid.contents.set(absolute('dependencies/index.json'),payload);
        await invalid.nav.start();
        assert.equal(invalid.nav.getIndex(),null); assert.equal(invalid.errors.length,1);
        assert.equal(invalid.errors[0].message,'Invalid search relation endpoint');
        assert.equal(invalid.win.location.href,rootSpec.route);
        invalid.nav.dispose();
      }
    """)


def test_large_index_yields_and_commits_only_after_all_rows_validate() -> None:
    _run(r"""
      const payload=index();
      for(let n=0;n<1024;n++) payload.nodes.push({id:'leaf-'+n,title:'Leaf '+n,route:'../roadmap/leaf-'+n+'.html'});
      const before=JSON.stringify(payload),published=[];
      const app=setup({onIndex(index){published.push(index);}});
      const key=absolute('dependencies/index.json'); app.contents.set(key,payload);
      const nativeTimer=global.setTimeout; let yields=0;
      global.setTimeout=(callback,delay,...args)=>{
        if(delay===0){yields++;assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);}
        return nativeTimer(callback,delay,...args);
      };
      try {
        const first=app.nav.loadIndex(),second=app.nav.loadIndex(); assert.strictEqual(first,second);
        const result=await first; assert.strictEqual(await second,result);
        assert(yields>=2); assert.equal(published.length,1); assert.strictEqual(app.nav.getIndex(),result);
        assert.equal(result.nodes.length,payload.nodes.length); assert.equal(JSON.stringify(payload),before);
        assert.deepEqual(result.relations,[['a','b',1,0]]);
        assert.equal(app.fetches.filter(url=>url===key).length,1);
      } finally {global.setTimeout=nativeTimer;app.nav.dispose();}

      // A failure after earlier batches must not expose a partial index or poison retry.
      const rejected=[],invalid=setup({onIndex(index){rejected.push(index);}}),broken=structuredClone(payload);
      broken.nodes.at(-1).id=broken.nodes[0].id; invalid.contents.set(key,broken);
      await assert.rejects(invalid.nav.loadIndex(),/Invalid search node/);
      assert.equal(invalid.nav.getIndex(),null);assert.equal(rejected.length,0);
      invalid.contents.set(key,payload);const recovered=await invalid.nav.loadIndex();
      assert.equal(recovered.nodes.length,payload.nodes.length);assert.equal(rejected.length,1);
      assert.equal(invalid.fetches.filter(url=>url===key).length,2);invalid.nav.dispose();
    """)


def test_disposal_during_index_batch_never_commits_or_reports_a_partial_index() -> None:
    _run(r"""
      const payload=index();for(let n=0;n<600;n++) payload.nodes.push({id:'leaf-'+n,title:'Leaf '+n});
      const published=[],app=setup({onIndex(index){published.push(index);}});
      app.contents.set(absolute('dependencies/index.json'),payload);
      const nativeTimer=global.setTimeout;let yielded=false;
      global.setTimeout=(callback,delay,...args)=>{
        if(delay===0){yielded=true;assert.equal(app.nav.getIndex(),null);app.nav.dispose();}
        return nativeTimer(callback,delay,...args);
      };
      try {await assert.rejects(app.nav.loadIndex(),error=>error.name==='AbortError');}
      finally {global.setTimeout=nativeTimer;app.nav.dispose();}
      assert(yielded);assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
      assert.equal(app.errors.length,0);assert.equal(app.win.location.href,rootSpec.route);
    """)


def test_relation_only_batches_yield_validate_late_rows_and_honor_disposal() -> None:
    _run(r"""
      for(const kind of ['valid','invalid','disposed']) {
        const payload=index();payload.relations=Array.from({length:2050},()=>[1,2,1,0]);
        if(kind==='invalid') payload.relations.at(-1)[1]='missing';
        const published=[],app=setup({onIndex(index){published.push(index);}});
        app.contents.set(absolute('dependencies/index.json'),payload);
        const nativeTimer=global.setTimeout;let yields=0;
        global.setTimeout=(callback,delay,...args)=>{
          if(delay===0){yields++;assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
            if(kind==='disposed') app.nav.dispose();}
          return nativeTimer(callback,delay,...args);
        };
        try {
          if(kind==='valid') {
            const result=await app.nav.loadIndex();assert(yields>=2);assert.equal(published.length,1);
            assert.equal(result.relations.length,2050);
            assert(result.relations.every(row=>JSON.stringify(row)===JSON.stringify(['a','b',1,0])));
          } else {
            await assert.rejects(app.nav.loadIndex(),error=>kind==='disposed' ? error.name==='AbortError' :
              error.message==='Invalid search relation endpoint');
            assert(yields>=(kind==='disposed'?1:2));assert.equal(app.nav.getIndex(),null);
            assert.equal(published.length,0);assert.equal(app.errors.length,kind==='disposed'?0:1);
          }
        } finally {global.setTimeout=nativeTimer;app.nav.dispose();}
      }
    """)


def test_reader_intercepts_only_plain_site_navigation_and_disposes_listener() -> None:
    _run(r"""
      const app=setup(); await app.nav.start(); const bundle=await app.nav.navigateNode('a');
      const root=bundle.readingSource; app.nav.bindReader(root); const click=root.listeners.get('click');
      function event(target,extra={}) {return Object.assign({target,button:0,defaultPrevented:false,
        preventDefault(){this.defaultPrevented=true;},stopPropagation(){}},extra);}
      const internal=root.querySelector('a'), external=root.querySelectorAll('a')[1];
      let modified=event(internal,{ctrlKey:true}); click(modified); assert.equal(modified.defaultPrevented,false);
      let outside=event(external); click(outside); assert.equal(outside.defaultPrevented,false);
      let ordinary=event(internal); click(ordinary); assert.equal(ordinary.defaultPrevented,true);
      await until(()=>app.commits.at(-1).nodeId==='b'); app.nav.dispose(); assert.equal(root.listeners.has('click'),false);
    """)


def test_an_invalid_index_can_retry_without_a_poisoned_cache() -> None:
    _run(r"""
      const app=setup(); const key=absolute('dependencies/index.json');
      app.contents.set(key,{schema:'wrong',nodes:[]});
      await assert.rejects(app.nav.loadIndex()); app.contents.set(key,index());
      const loaded=await app.nav.loadIndex(); assert.equal(loaded.nodes.length,3);
      assert.equal(app.fetches.filter(url=>url===key).length,2); app.nav.dispose();
    """)


def test_native_section_anchors_do_not_replace_the_selected_article() -> None:
    _run(r"""
      const app=setup(); await app.nav.start(); const bundle=await app.nav.navigateNode('a');
      const root=bundle.readingSource, section=new Element('a',{href:aSpec.route+'#appendix'},[],'Appendix');
      root.children.push(section); section.parent=root; app.nav.bindReader(root);
      const click={target:section,button:0,defaultPrevented:false,
        preventDefault(){this.defaultPrevented=true;},stopPropagation(){}};
      root.listeners.get('click')(click); assert.equal(click.defaultPrevented,false);
      const count=app.commits.length, requests=app.fetches.length;
      app.win.history.pushState({},'',section.href); app.listeners.get('popstate')();
      await new Promise(resolve=>setImmediate(resolve));
      assert.equal(app.commits.length,count); assert.equal(app.fetches.length,requests);
      assert.equal(app.commits.at(-1).nodeId,'a'); assert.equal(bundle.url,section.href); assert(bundle.isCurrent());
      app.nav.checkpoint();
      assert.equal(app.win.history.state.autoformNavigation.url,section.href); app.nav.dispose();
    """)


def test_native_section_intent_cannot_revive_delayed_reader_focus_or_scroll_restoration() -> None:
    _run(r"""
      // Run the real reading callbacks, not a test copy of their asynchronous guards.
      // Other UI collaborators are isolated; this is not a MathJax or browser rendering test.
      const viewer=fs.readFileSync(CLIENT.replace(/blueprint-navigation\.js$/,'blueprint-dag.js'),'utf8');
      function callback(name) {
        const start=viewer.indexOf('    function '+name+'(');
        assert(start>=0);
        const following=/\n    function [A-Za-z_$][A-Za-z0-9_$]*\(/.exec(viewer.slice(start+4));
        assert(following); return viewer.slice(start+4,start+4+following.index).trim();
      }
      const hooks={},app=setup(hooks),calls=[];
      let alive=true,readingReady=Promise.resolve(),restoration=0,currentBundle,contextGraph,data,trail,firstResize,
        restoring=false,checkpointTimer;
      let release,mathReady; function deferMath() { mathReady=new Promise(resolve=>{release=resolve;}); }
      const window=app.win,document=app.initial.doc,host=app.initial.host;
      window.AutoformMath={refresh(){return mathReady;}};
      host.classList={toggle(){}};
      const heading={focus(){calls.push('heading-focus');}};
      const section={classList:{add(){}},scrollIntoView(){calls.push('old-section-scroll');}};
      document.getElementById=()=>section;
      const refs={
        $readingBody:{scrollTop:0,replaceChildren(){},querySelector(){return heading;},contains(node){return node===section;}},
        $search:{value:'',focus(){calls.push('old-search-focus');}},$error:{hidden:true},
        $breadcrumb:{replaceChildren(){}},$navigation:{scrollTop:0},$inventoryScroll:{scrollTop:0}
      };
      // Supply the renderer's actual private lexical collaborators; executed source stays verbatim.
      let ui$selected=null,ui$filtersTouched,ui$area,ui$requestedNode,ui$selectedStatuses,ui$showIsolated,
        ui$mode,ui$listLimit,ui$relationIds,ui$direction,ui$query,ui$transform,ui$activeDirty,ui$reading,ui$overviewOnly,ui$overviewGroup,ui$scopeOverview,ui$outlineLimit;
      const dom$readingBody=refs.$readingBody,dom$search=refs.$search,dom$error=refs.$error,
        dom$breadcrumb=refs.$breadcrumb,dom$navigation=refs.$navigation,dom$inventoryScroll=refs.$inventoryScroll;
      let dom$outlineToggle;
      const records=new Map(),byId=new Map(),overlays=new Map(),dom$inventoryRows=new Map(),navigation=app.nav;
      const mapAllowed=true,presentStatuses=['planned'],palette={planned:{}},LIST_PAGE=100;
      const updateProjection=()=>{data=contextGraph;},closeSearch=()=>{},setFilters=()=>{},setBrowse=()=>{},setSheet=()=>{},renderNavigation=()=>{},refresh=()=>{};
      const resize=()=>{},center=()=>{},centerIfHidden=()=>{},showError=()=>{},trailList=()=>[],routeTrail=()=>[],sheetLayout=()=>false;
      const requestAnimationFrame=callback=>setImmediate(callback);
      const applyRoute=eval('('+callback('applyRoute')+')');
      const restoreState=eval('('+callback('restoreState')+')');
      hooks.commit=bundle=>bundle.nodeId==='a' ? applyRoute(bundle) : undefined;
      let restorePending; hooks.restore=(saved,bundle)=>{restorePending=restoreState(saved,bundle);return restorePending;};
      await app.nav.start(); deferMath();
      const opening=app.nav.navigateNode('a'); await until(()=>app.commits.at(-1).nodeId==='a');
      const mounted=app.commits.at(-1),originalGuard=mounted.isCurrent;
      const commits=app.commits.length,requests=app.fetches.length;
      app.win.history.pushState({},'',aSpec.route+'#appendix'); app.listeners.get('popstate')();
      assert.equal(originalGuard(),false); assert.equal(mounted.isCurrent(),true);
      release(); assert.equal(await opening,null); assert.deepEqual(calls,[]);
      assert.equal(app.win.location.href,aSpec.route+'#appendix');

      // A later native entry with no snapshot must also cancel an older pending scroll/focus restore.
      deferMath(); const rereading=applyRoute(mounted);
      app.win.history.pushState({autoformNavigation:{version:1,snapshot:{focus:'search',readingScroll:71,
        outlineScroll:37,inventoryScroll:29}}},'',aSpec.route+'#older-section');
      app.listeners.get('popstate')(); assert(restorePending);
      const restorationGuard=mounted.isCurrent;
      app.win.history.pushState({},'',aSpec.route+'#latest-section'); app.listeners.get('popstate')();
      assert.equal(restorationGuard(),false); assert.equal(mounted.isCurrent(),true);
      release(); await Promise.all([rereading,restorePending]);
      assert.deepEqual(calls,[]); assert.equal(refs.$readingBody.scrollTop,0);
      assert.equal(refs.$navigation.scrollTop,0); assert.equal(refs.$inventoryScroll.scrollTop,0);
      assert.equal(app.win.location.href,aSpec.route+'#latest-section');
      assert.equal(app.commits.length,commits); assert.equal(app.fetches.length,requests); app.nav.dispose();
    """)


def test_results_restoration_renders_saved_page_before_restoring_scroll_and_focus() -> None:
    _run(r"""
      // Execute both production callbacks: a stub refresh would miss its page reset.
      // DOM collaborators only record ordering; the native browser test covers clipping/hit testing.
      const viewer=fs.readFileSync(CLIENT.replace(/blueprint-navigation\.js$/,'blueprint-dag.js'),'utf8');
      function callback(name) {
        const start=viewer.indexOf('    function '+name+'('); assert(start>=0);
        const following=/\n    function [A-Za-z_$][A-Za-z0-9_$]*\(/.exec(viewer.slice(start+4));
        assert(following); return viewer.slice(start+4,start+4+following.index).trim();
      }
      const calls=[],LIST_PAGE=100,presentStatuses=['planned'],mapAllowed=false;
      let alive=true,restoration=0,restoring=false,checkpointTimer,readingReady=Promise.resolve();
      let ui$filtersTouched,ui$area,ui$requestedNode,ui$selected,ui$selectedStatuses,ui$showIsolated,
        ui$mode='list',ui$listLimit=100,ui$relationIds,ui$outlineLimit,ui$direction,ui$query,ui$activeDirty,ui$transform;
      const host={classList:{toggle(){}}},byId=new Map(),overlays=new Map();
      let dom$inventoryRows=new Map(),dom$outlineToggle;
      const dom$search={value:''},dom$navigation={scrollTop:0},dom$readingBody={scrollTop:0},dom$inventoryScroll={scrollTop:0};
      const updateProjection=()=>{},renderNavigation=()=>{},setSheet=()=>{},resize=()=>{};
      const syncControls=()=>{},renderInspector=()=>{},schedule=()=>{};
      function renderInventory(resetScroll) {
        calls.push(['render',ui$listLimit,resetScroll]); dom$inventoryRows=new Map();
        for(let i=0;i<ui$listLimit;i++) dom$inventoryRows.set('result-'+i,{focus(options){
          calls.push(['focus',i,dom$inventoryScroll.scrollTop,options]);
        }});
        if(resetScroll) dom$inventoryScroll.scrollTop=0;
      }
      const requestAnimationFrame=callback=>setImmediate(callback);
      const refresh=eval('('+callback('refresh')+')'),restoreState=eval('('+callback('restoreState')+')');
      const saved={mode:'list',listLimit:200,inventoryScroll:1800,outlineScroll:37,readingScroll:71,
        focus:'result-150',camera:{x:12,y:24,scale:1.2},readingOpen:false};
      await restoreState(saved,{isCurrent:()=>true});
      assert.deepEqual(calls,[['render',200,false],['focus',150,1800,{preventScroll:true}]]);
      assert.equal(ui$listLimit,200); assert.equal(dom$inventoryScroll.scrollTop,1800);
      assert.equal(dom$navigation.scrollTop,37); assert.equal(dom$readingBody.scrollTop,71);
      assert.deepEqual(ui$transform,saved.camera); assert.equal(restoring,false);
      // A new filter/query still deliberately starts a fresh first page.
      refresh(true); assert.equal(ui$listLimit,100); assert.equal(dom$inventoryScroll.scrollTop,0);
      assert.deepEqual(calls.at(-1),['render',100,true]);
    """)
