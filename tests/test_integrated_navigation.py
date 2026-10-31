"""Isolated router behavior with native Node promises/URLs/abort signals, not a browser substitute."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoform_cli import dag_viewer
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode

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
    script += "\nlet testBodyComplete=false;\n"
    script += "process.once('beforeExit',()=>{if(!testBodyComplete){console.error('Navigation test body did not complete');process.exitCode=1;}});\n"
    script += "(async()=>{\n" + body + "\n})().then(()=>{testBodyComplete=true;},error=>{testBodyComplete=true;console.error(error);process.exitCode=1;});"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def parent_wires(tmp_path: Path):
    title = r"Groups $\mathbb{Z}/n$ and `$literal$`"
    summary = "Every integer retains its authored hypotheses.\n\n" + "Complete mathematical prose, not a preview. " * 18
    definitions = (
        ("a", "node", "proved", "topic", "topic"),
        ("b", "node", "conditional", "topic", "outside-unpublished"),
        ("roadmap", "scope", "planned", "roadmap", None),
        ("0", "node", "stated", "roadmap", "roadmap"),
        ("topic", "scope", "proved", "roadmap", "roadmap"),
        ("self", "scope", "mathlib", "self", "self"),
        ("orphan", "node", "planned", "roadmap", None),
        ("zero-offset-child", "node", "defined", "a", "a"),
        ("literal-zero-child", "node", "planned", "0", "0"),
        ("scope:authored", "scope", "planned", "topic", "topic"),
    )
    nodes = tuple(
        ViewNode(node_id, title if position == 0 else node_id, kind, (node_id,), ((status, 1),),
                 status_key=status, summary=summary if position == 0 else None,
                 lean="Authored.groups Authored.quotient" if position == 0 else None)
        for position, (node_id, kind, status, _scope, _parent) in enumerate(definitions)
    )
    view = GraphView("full", "Authored mathematics", nodes, (ViewEdge("a", "b", 2, 3), ViewEdge("0", "a", 0, 1)))
    metadata = {
        node_id: {"route": f"../roadmap/{node_id}.html#canonical-{position}", "scope": scope, "parent": parent,
                  "data_url": f"scopes/{node_id}.json", "result": kind == "node"}
        for position, (node_id, kind, _status, scope, parent) in enumerate(definitions)
    }
    metadata["topic"].update(area="Authored algebra", navigation="collection")
    links = {node.id: f"scopes/{node.id}.html#node={node.id}" for node in nodes}
    legacy_nodes = []
    for node in nodes:
        entry = {"id": node.id, "kind": node.kind, "status": node.status_key, "title": node.title, "url": links[node.id]}
        entry.update({key: value for key, value in metadata[node.id].items() if value is not None})
        if node.summary:
            entry.update(summary=node.summary, lean=node.lean)
        legacy_nodes.append(entry)
    legacy_nodes[0]["reading_title"] = {
        "format": "inline-parts-v1", "source": title,
        "parts": [["text", "Groups "], ["math", r"\(\mathbb{Z}/n\)"], ["text", " and "], ["code", "$literal$"]],
    }
    legacy = {"schema": "autoform-dag-search/v1", "nodes": legacy_nodes, "relations": [[0, 1, 2, 3], [3, 0, 0, 1]]}
    before = copy.deepcopy(metadata)
    digests: list[str] = []
    output = tmp_path / "index.json"
    dag_viewer.write_search_index(output, view, context_links=links, metadata=metadata,
                                 include_relations=True, sha256_out=digests)
    raw = output.read_bytes()
    assert metadata == before
    return view, legacy, json.loads(raw), raw, digests[0], metadata, links


def test_published_parent_offsets_preserve_all_other_index_fields_and_exact_byte_receipt(parent_wires, tmp_path: Path) -> None:
    view, legacy, compact, raw, digest, metadata, links = parent_wires
    expected = copy.deepcopy(legacy)
    for position, parent in ((0, 4), (3, 2), (4, 2), (5, 5), (7, 0), (8, 3), (9, 4)):
        expected["nodes"][position]["parent"] = parent
    assert compact == expected
    assert "parent" not in compact["nodes"][2] and "parent" not in compact["nodes"][6]
    assert compact["nodes"][1]["parent"] == "outside-unpublished"
    assert compact["nodes"][0]["summary"] == view.nodes[0].summary
    assert len(compact["nodes"][0]["summary"]) > 512
    assert digest == hashlib.sha256(raw).hexdigest()
    assert raw.endswith(b"\n")
    duplicate = tmp_path / "without-receipt.json"
    dag_viewer.write_search_index(duplicate, view, context_links=links, metadata=metadata, include_relations=True)
    assert duplicate.read_bytes() == raw


def test_coalesced_parent_offset_index_matches_legacy_and_wiki_binds_received_not_restored_bytes(parent_wires) -> None:
    _view, legacy, _compact, raw, digest, _metadata, _links = parent_wires
    legacy_text = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    _run("const compactText=" + json.dumps(raw.decode()) + ";\nconst legacyText=" + json.dumps(legacy_text)
         + ";\nconst receivedDigest=" + json.dumps(digest) + ";\n" + r"""
      const {webcrypto}=require('node:crypto'),key=absolute('dependencies/index.json');
      const bytes=new TextEncoder().encode(compactText),before=bytes.slice(),published=[];
      let release,requested;const arrived=new Promise(resolve=>requested=resolve),gate=new Promise(resolve=>release=resolve);
      const app=setup({onIndex(value){published.push(value);},fetch(url){
        if(url!==key)return;
        requested();return gate.then(()=>({ok:true,status:200,url,headers:{get(){return null;}},
          arrayBuffer:async()=>bytes.slice().buffer,text:async()=>{throw new Error('Do not reserialize received index bytes');}}));
      }});app.win.crypto=webcrypto;
      const first=app.nav.loadIndex(),second=app.nav.loadIndex();assert.strictEqual(first,second);
      await arrived;assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
      release();const normalized=await first;assert.strictEqual(await second,normalized);
      assert.equal(published.length,1);assert.strictEqual(published[0],normalized);assert.strictEqual(app.nav.getIndex(),normalized);
      assert.strictEqual(await app.nav.loadIndex(),normalized);
      assert.equal(app.fetches.filter(url=>url===key).length,1);

      const full=setup();full.contents.set(key,JSON.parse(legacyText));const fullIndex=await full.nav.loadIndex();
      assert.deepEqual(normalized,fullIndex,'Only transport parents differ; the complete published index must remain equivalent');
      assert.deepEqual(normalized.nodes.map(node=>node.id),['a','b','roadmap','0','topic','self','orphan',
        'zero-offset-child','literal-zero-child','scope:authored']);
      assert.deepEqual(normalized.nodes.map(node=>node._index),[0,1,2,3,4,5,6,7,8,9]);
      assert.equal(normalized.nodes[0].route,absolute('roadmap/a.html#canonical-0'));
      assert.equal(normalized.nodes[0].data_url,absolute('dependencies/scopes/a.json'));
      assert.deepEqual(normalized.relations,[['a','b',2,3],['0','a',0,1]]);
      assert.equal(normalized.nodes[0].parent,'topic');assert.equal(normalized.nodes[7].parent,'a');
      assert.equal(normalized.nodes[8].parent,'0');assert.equal(normalized.nodes[5].parent,'self');
      assert(!Object.hasOwn(normalized.nodes[6],'parent'),'An uncontained leaf may have a fallback roadmap scope, not a parent');

      async function sha(value){return Buffer.from(await webcrypto.subtle.digest('SHA-256',value)).toString('hex');}
      assert.equal(await sha(bytes),receivedDigest);const legacyDigest=await sha(new TextEncoder().encode(legacyText));
      assert.notEqual(legacyDigest,receivedDigest,'Lossless normalization does not make distinct transport bytes interchangeable');
      const wikiURL=absolute('dependencies/wiki-links.json'),layer={schema:'autoform-wiki-links/v1',
        index_sha256:legacyDigest,availability:'partial',references:[[0,1],[8,9]],unavailable_sources:[6]};
      app.contents.set(wikiURL,layer);const mathBefore=JSON.stringify(normalized);
      const stale=await app.nav.loadWikiLinks();assert.equal(stale.availability,'unavailable');assert.equal(stale.reason,'stale_index');
      layer.index_sha256=receivedDigest;const wiki=await app.nav.loadWikiLinks();
      assert.equal(wiki.availability,'partial');assert.equal(wiki.index_sha256,receivedDigest);
      assert.deepEqual(wiki.references,[['a','b'],['literal-zero-child','scope:authored']]);
      assert.deepEqual(wiki.unavailable_sources,['orphan']);assert.equal(JSON.stringify(normalized),mathBefore);
      assert.deepEqual(bytes,before);assert.equal(app.errors.length,0);assert.equal(full.errors.length,0);
      assert.equal(published.length,1);assert.equal(app.fetches.filter(url=>url===key).length,1);
      app.nav.dispose();full.nav.dispose();
    """)


def test_parent_offsets_do_not_reinterpret_literal_ids_null_or_missing_legacy_containment() -> None:
    _run(r"""
      const payload=index();payload.nodes.push(
        {id:'0',title:'Literal zero',scope:'roadmap',parent:null},
        {id:'ordinal-zero',title:'Ordinal zero',parent:0},
        {id:'literal-zero',title:'Literal zero parent',parent:'0'},
        {id:'forward',title:'Forward parent',parent:7},
        {id:'future',title:'Future parent',scope:'future',parent:'future'},
        {id:'uncontained',title:'Uncontained article',scope:'roadmap'},
        {id:'unknown-parent',title:'Unknown parent',scope:'roadmap',parent:'unpublished'},
        {id:'boolean-parent',title:'Legacy nonnumeric parent',parent:false});
      const before=JSON.stringify(payload),published=[],app=setup({onIndex(value){published.push(value);}});
      app.contents.set(absolute('dependencies/index.json'),payload);const value=await app.nav.loadIndex();
      const byId=new Map(value.nodes.map(node=>[node.id,node]));
      assert.strictEqual(byId.get('0').parent,null);assert.equal(byId.get('ordinal-zero').parent,'roadmap');
      assert.equal(byId.get('literal-zero').parent,'0');assert.equal(byId.get('forward').parent,'future');
      assert.equal(byId.get('future').parent,'future');assert.equal(byId.get('unknown-parent').parent,'unpublished');
      assert.strictEqual(byId.get('boolean-parent').parent,false);
      assert(!Object.hasOwn(byId.get('uncontained'),'parent'));assert(!Object.hasOwn(byId.get('a'),'parent'));
      assert(!Object.hasOwn(byId.get('roadmap'),'parent'));assert.equal(JSON.stringify(payload),before);
      assert.equal(published.length,1);assert.strictEqual(published[0],value);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


@pytest.mark.parametrize("bad_parent", (-1, 0.5, 516, 9007199254740992))
def test_invalid_late_parent_offset_never_publishes_partial_index_and_coalesced_retry_recovers(bad_parent: int | float) -> None:
    _run("const badParent=" + json.dumps(bad_parent) + ";\n" + r"""
      const payload=index();for(let n=0;n<513;n++)payload.nodes.push({id:'parent-'+n,title:'Parent '+n,parent:0});
      payload.nodes.at(-1).parent=badParent;const before=JSON.stringify(payload),published=[];
      const app=setup({onIndex(value){published.push(value);}}),key=absolute('dependencies/index.json');
      app.contents.set(key,payload);const first=app.nav.loadIndex(),second=app.nav.loadIndex();assert.strictEqual(first,second);
      await assert.rejects(first,error=>error.message==='Invalid search parent endpoint');
      assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
      assert.equal(app.errors.length,1);assert.equal(app.errors[0].url,key);assert.equal(JSON.stringify(payload),before);
      assert.equal(app.fetches.filter(url=>url===key).length,1);assert.equal(app.win.location.href,rootSpec.route);
      const corrected=structuredClone(payload);corrected.nodes.at(-1).parent=0;app.contents.set(key,corrected);
      const retry=app.nav.loadIndex();assert.strictEqual(app.nav.loadIndex(),retry);const recovered=await retry;
      assert.equal(recovered.nodes.length,516);assert.equal(recovered.nodes.at(-1).parent,'roadmap');
      assert.equal(published.length,1);assert.strictEqual(published[0],recovered);assert.strictEqual(app.nav.getIndex(),recovered);
      assert.deepEqual(recovered.relations,[['a','b',1,0]]);assert.equal(app.fetches.filter(url=>url===key).length,2);app.nav.dispose();
    """)


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
        assert.deepEqual(paint.requested,[0,1],'The first frame must remain free of the next validation batch');
        paint.release(1);await until(()=>paint.frames.size===1);
        assert.deepEqual(paint.requested,[0,1,2]);assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
        paint.release(2);await until(()=>paint.frames.size===1);
        assert.deepEqual(paint.requested,[0,1,2,3]);assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
        await new Promise(resolve=>setImmediate(resolve));
        assert.deepEqual(paint.requested,[0,1,2,3],'Publication must still await the second actual paint boundary');
        paint.release(3);const result=await pending;assertPaintIndex(app,result,published);
        assert.equal(paint.frames.size,0);assert.equal(paint.timers.size,0);assert.deepEqual(paint.cancelled,[1,3]);
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


@pytest.mark.parametrize("phase", [0, 1])
@pytest.mark.parametrize("completion", ["frame", "timer"])
def test_disposal_while_index_frame_is_pending_never_exposes_a_partial_index(phase: int, completion: str) -> None:
    _run(_PAINT_PEERS + "const phase=" + json.dumps(phase) + ";\nconst completion=" + json.dumps(completion) + ";\n" + r"""
      const published=[],app=setup({onIndex(value){published.push(value);}});paintCatalogue(app);
      const paint=controlledPaint(app);
      try {
        const pending=app.nav.loadIndex();await until(()=>paint.frames.size===1);
        assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
        if(phase===1)paint.release(0);
        const expected=phase===1?[0,1]:[0];app.nav.dispose();
        if(completion==='frame')paint.release(phase);
        else {const timeout=paint.timers.values().next().value;assert(timeout);timeout();}
        await assert.rejects(pending,error=>error.name==='AbortError');
        assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);assert.equal(app.errors.length,0);
        assert.deepEqual(paint.requested,expected);assert.deepEqual(paint.cancelled,[phase]);
        assert.equal(paint.frames.size,0);assert.equal(paint.timers.size,0);
      } finally {app.nav.dispose();paint.restore();}
    """)


def test_stalled_second_index_frame_keeps_the_original_fifty_ms_fallback() -> None:
    _run(_PAINT_PEERS + r"""
      const published=[],app=setup({onIndex(value){published.push(value);}});paintCatalogue(app);
      const paint=controlledPaint(app);
      try {
        const pending=app.nav.loadIndex();await until(()=>paint.frames.size===1);
        const firstTimer=paint.timers.keys().next().value;
        paint.release(0);await until(()=>paint.frames.size===1);
        assert.equal(paint.timers.size,1);assert.strictEqual(paint.timers.keys().next().value,firstTimer,
          'The second frame must not restart or extend the original fifty-ms timer');
        assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
        paint.timers.get(firstTimer)();await until(()=>paint.frames.size===1);
        assert.deepEqual(paint.cancelled,[1]);assert.deepEqual(paint.requested,[0,1,2]);
        paint.release(2);const secondTimer=paint.timers.values().next().value;assert(secondTimer);secondTimer();
        const result=await pending;assertPaintIndex(app,result,published);
        assert.deepEqual(paint.cancelled,[1,3]);assert.equal(paint.frames.size,0);assert.equal(paint.timers.size,0);
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


@pytest.mark.parametrize("published_entrance", ("directory", "index"))
def test_directory_statement_entrances_resolve_only_existing_published_routes_and_fetch_complete_canonical_reading(
    published_entrance: str,
) -> None:
    _run("const entrance=" + json.dumps(published_entrance) + ";\n" + r"""
      const directory=absolute('roadmap/chapter/'),indexed=directory+'index.html';
      const canonical=entrance==='index'?indexed:directory,requested=entrance==='index'?directory:indexed;
      const spec={title:'Complete chapter',route:canonical,data:'../../dependencies/a.json',catalog:'../../dependencies/index.json',scope:'chapter',
        prose:'Complete published theorem, hypotheses, and proof.'};
      const alias={...spec,prose:'Reading notice only'},app=setup({initial:alias,url:requested+'#theorem-a'});
      const catalog=index();catalog.nodes[1]={...catalog.nodes[1],route:canonical+'#theorem-a',scope:'chapter'};
      app.contents.set(absolute('dependencies/index.json'),catalog);app.contents.set(canonical,spec);
      const opened=await app.nav.start();assert(opened);assert.equal(opened.url,canonical+'#theorem-a');
      assert.equal(opened.nodeId,'a');assert.equal(opened.anchor,'theorem-a');assert.equal(opened.focus,true);
      assert.equal(opened.documentURL,canonical);assert.equal(opened.scope,'chapter');
      assert(opened.readingSource.textContent.includes(spec.prose));assert(!opened.readingSource.textContent.includes(alias.prose));
      assert.equal(app.fetches.filter(url=>url===canonical).length,1);
      assert.equal(app.win.history.length,1);assert.equal(app.win.history.state.autoformNavigation.url,opened.url);
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


@pytest.mark.parametrize("fragment", ("theorem-a", "appendix%2Fnotes"))
def test_ordinary_alias_fragment_survives_complete_redirect_chain_and_back_forward(fragment: str) -> None:
    _run("const fragment=" + json.dumps(fragment) + ";\n" + r"""
      const app=setup(),aliasURL=absolute('roadmap/old-a.html'),middle=absolute('roadmap/middle-a.html');
      const notice={...aSpec,route:middle,prose:'First reading notice only'};
      const second={...aSpec,route:aSpec.route,prose:'Second reading notice only'};
      app.contents.set(aliasURL,notice);app.contents.set(middle,second);await app.nav.start();
      const origin=app.win.location.href,opened=await app.nav.navigate(aliasURL+'#'+fragment);assert(opened);
      const canonical=aSpec.route+'#'+fragment;
      assert.equal(opened.url,canonical);assert.equal(opened.anchor,decodeURIComponent(fragment));
      assert.equal(opened.nodeId,fragment==='theorem-a'?'a':null);assert.equal(opened.documentURL,aSpec.route);
      assert(opened.readingSource.textContent.includes('Complete source statement and proof.'));
      assert(!opened.readingSource.textContent.includes('reading notice only'));
      for(const url of [aliasURL,middle,aSpec.route])assert.equal(app.fetches.filter(value=>value===url).length,1);
      assert.equal(app.win.history.length,2);assert.equal(app.win.history.state.autoformNavigation.url,canonical);
      app.state({mode:'graph',readingScroll:86});app.nav.checkpoint();app.win.history.back();
      await until(()=>app.commits.at(-1).url===origin&&app.initial.host.getAttribute('aria-busy')===null);
      app.win.history.forward();await until(()=>app.commits.at(-1).url===canonical&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).anchor,decodeURIComponent(fragment));assert.equal(app.getState().readingScroll,86);
      assert.equal(app.win.history.length,2);assert.equal(app.win.history.state.material,'preserved');
      assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_alias_fragment_does_not_override_explicit_or_legacy_statement_selection() -> None:
    _run(r"""
      const app=setup(),aliasURL=absolute('roadmap/old-a.html');
      app.contents.set(aliasURL,{...aSpec,prose:'Reading notice only'});await app.nav.start();
      const explicit=await app.nav.navigate(aliasURL+'#wrong-section',{nodeId:'a'});
      assert.equal(explicit.url,aSpec.route+'#theorem-a');assert.equal(explicit.nodeId,'a');assert.equal(explicit.anchor,'theorem-a');
      const legacy=await app.nav.navigate(aliasURL+'#node=a&direction=prerequisites');
      assert.equal(legacy.url,aSpec.route+'#theorem-a');assert.equal(legacy.nodeId,'a');assert.equal(legacy.anchor,'theorem-a');
      assert.equal(legacy.parameters.direction,'prerequisites');assert(!legacy.readingSource.textContent.includes('Reading notice only'));
      assert.equal(app.errors.length,0);app.nav.dispose();
    """)


@pytest.mark.parametrize("query", ("?edition=1", "?edition=2"))
def test_directory_route_lookup_preserves_query_identity_and_uses_the_declared_canonical_query(query: str) -> None:
    _run("const query=" + json.dumps(query) + ";\n" + r"""
      const directory=absolute('roadmap/chapter/'),canonical=directory+'index.html?edition=1';
      const spec={title:'Complete edition one',route:canonical,data:'../../dependencies/a.json',scope:'chapter'};
      const app=setup(),catalog=index();catalog.nodes[1]={...catalog.nodes[1],route:canonical+'#theorem-a',scope:'chapter'};
      app.contents.set(absolute('dependencies/index.json'),catalog);app.contents.set(canonical,spec);
      app.contents.set(directory+query,{...spec,prose:'Alias reading notice only'});await app.nav.start();
      const opened=await app.nav.navigate(directory+query+'#theorem-a');assert(opened);
      assert.equal(opened.url,canonical+'#theorem-a');assert.equal(opened.nodeId,'a');assert.equal(opened.anchor,'theorem-a');
      assert(opened.readingSource.textContent.includes('Complete source statement and proof.'));
      assert.equal(app.fetches.filter(url=>url===directory+query).length,query==='?edition=1'?0:1,
        'A different query must not acquire a published route through directory normalization');
      assert.equal(app.fetches.filter(url=>url===canonical).length,1);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_query_only_canonical_contradiction_still_rejects_before_current_source_or_history_changes() -> None:
    _run(r"""
      const directory=absolute('roadmap/chapter/'),canonical=directory+'index.html?edition=1',other=directory+'index.html?edition=2';
      const wrong={title:'Wrong edition',route:other,data:'../../dependencies/a.json',scope:'chapter'};
      const app=setup({fetch(url,_settings,reply){if(url===canonical)return reply(wrong,other);}}),catalog=index();
      catalog.nodes[1]={...catalog.nodes[1],route:canonical+'#theorem-a',scope:'chapter'};
      app.contents.set(absolute('dependencies/index.json'),catalog);await app.nav.start();
      const before={url:app.win.location.href,length:app.win.history.length,commits:app.commits.length,source:app.commits.at(-1).readingSource};
      assert.equal(await app.nav.navigate(directory+'?edition=1#theorem-a'),null);
      assert.equal(app.win.location.href,before.url);assert.equal(app.win.history.length,before.length);
      assert.equal(app.commits.length,before.commits);assert.strictEqual(app.commits.at(-1).readingSource,before.source);
      assert.equal(app.errors.length,1);assert.equal(app.errors[0].message,'Published reading route contradicts the requested article');
      assert.equal(app.initial.host.getAttribute('aria-busy'),null);app.nav.dispose();
    """)


@pytest.mark.parametrize("prefix", ("/", "/project/", "/nested/notes/"))
def test_home_url_keeps_deployment_prefix_and_loads_complete_home_without_implicit_indexed_root(prefix: str) -> None:
    _run("const prefix=" + json.dumps(prefix) + ";\n" + r"""
      const site='https://example.test'+prefix,home=site+'index.html';
      const initial={title:'Current chapter',route:site+'roadmap/chapter/index.html',scope:'chapter',
        data:'../../dependencies.json',catalog:'../../dependencies/index.json'};
      const spec={title:'Site home',route:home,scope:'roadmap',data:'dependencies.json',catalog:'dependencies/index.json',
        prose:'Complete authored introduction at the site home, not a roadmap redirect.'};
      const app=setup({initial,url:initial.route}),catalog=index();catalog.nodes[0].route=home;
      app.contents.set(site+'dependencies/index.json',catalog);app.contents.set(site+'dependencies.json',graph);
      app.contents.set(initial.route,initial);app.contents.set(home,spec);
      assert.equal(app.nav.getHomeURL(),home);await app.nav.start();const opened=await app.nav.navigate(app.nav.getHomeURL());
      assert(opened);assert.equal(opened.url,home);assert.equal(opened.documentURL,home);
      assert.equal(opened.nodeId,null,'Bare Home is not a request to open the indexed roadmap record');
      assert.equal(opened.anchor,null);assert.equal(opened.title,spec.title);assert.equal(opened.scope,'roadmap');
      assert(opened.readingSource.textContent.includes(spec.prose));assert.equal(app.fetches.filter(url=>url===home).length,1);
      assert.equal(app.win.history.length,2);assert.equal(app.win.history.state.autoformNavigation.url,home);
      app.win.history.back();await until(()=>app.commits.at(-1).url===initial.route&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).readingSource.querySelector('h1').textContent,initial.title);
      app.win.history.forward();await until(()=>app.commits.at(-1).url===home&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).nodeId,null);assert.equal(app.nav.getHomeURL(),home);
      assert.equal(app.win.history.state.material,'preserved');assert.equal(app.errors.length,0);app.nav.dispose();
      assert.equal(app.nav.getHomeURL(),home,'The native destination remains usable after disposal');
    """)


def test_home_route_exclusion_keeps_explicit_root_and_real_home_statement_permalinks() -> None:
    _run(r"""
      const home=absolute('index.html'),spec={title:'Site home',route:home,scope:'roadmap',data:'dependencies.json'};
      const app=setup(),catalog=index();catalog.nodes[0].route=home;catalog.nodes[1].route=home+'#theorem-a';
      app.contents.set(absolute('dependencies/index.json'),catalog);app.contents.set(home,spec);await app.nav.start();
      const root=await app.nav.navigateNode('roadmap');assert.equal(root.url,home);assert.equal(root.nodeId,'roadmap');
      const statement=await app.nav.navigate(home+'#theorem-a');assert.equal(statement.url,home+'#theorem-a');
      assert.equal(statement.nodeId,'a');assert.equal(statement.anchor,'theorem-a');assert.equal(statement.focus,true);
      const bare=await app.nav.navigate(app.nav.getHomeURL());assert.equal(bare.url,home);assert.equal(bare.nodeId,null);
      assert.equal(bare.anchor,null);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_home_prefix_uses_global_assets_not_nested_article_folders_named_roadmap_or_dependencies() -> None:
    _run(r"""
      for(const folder of ['roadmap','dependencies']) {
        const initial={title:'Nested article',route:absolute('roadmap/notes/'+folder+'/index.html'),scope:'nested',
          data:absolute('dependencies.json'),catalog:absolute('dependencies/index.json')};
        const app=setup({initial,url:initial.route});assert.equal(app.nav.getHomeURL(),absolute('index.html'));
        assert.equal(app.nav.getIndex(),null);assert.equal(app.fetches.length,0,'A native Home target needs no eager catalogue fetch');
        app.nav.dispose();
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
        ui$mode,ui$listLimit,ui$relationIds,ui$direction,ui$query,ui$transform,ui$activeDirty,ui$reading,ui$overviewOnly,ui$overviewGroup,ui$scopeOverview,ui$coarseOverview,ui$outlineLimit;
      const dom$readingBody=refs.$readingBody,dom$search=refs.$search,dom$error=refs.$error,
        dom$breadcrumb=refs.$breadcrumb,dom$navigation=refs.$navigation,dom$inventoryScroll=refs.$inventoryScroll;
      let dom$outlineToggle;
      const records=new Map(),byId=new Map(),overlays=new Map(),dom$inventoryRows=new Map(),navigation=app.nav;
      const mapAllowed=true,presentStatuses=['planned'],palette={planned:{}},LIST_PAGE=100;
      const updateProjection=()=>{data=contextGraph;},closeSearch=()=>{},setFilters=()=>{},setBrowse=()=>{},setSheet=()=>{},renderNavigation=()=>{},refresh=()=>{};
      const resize=()=>{},center=()=>{},centerIfHidden=()=>{},showError=()=>{},trailList=()=>[],routeTrail=()=>[],sheetLayout=()=>false;
      const requestAnimationFrame=callback=>setImmediate(callback);
      const snapshotForRoute=eval('('+callback('snapshotForRoute')+')');
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
      app.win.history.pushState({autoformNavigation:{version:1,url:aSpec.route+'#older-section',key:'older-native-entry',snapshot:{focus:'search',readingScroll:71,
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
      const host={classList:{toggle(){}}},records=new Map(),byId=new Map(),overlays=new Map();
      let dom$inventoryRows=new Map(),dom$outlineToggle,dom$shelves=null;
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
      const snapshotForRoute=eval('('+callback('snapshotForRoute')+')');
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


def test_navigation_runner_rejects_an_unresolved_async_body_instead_of_passing_without_assertions() -> None:
    with pytest.raises(AssertionError, match="Navigation test body did not complete"):
        _run("await new Promise(()=>{}); assert.fail('Unreachable assertion');")


@pytest.mark.parametrize("kind", ["http_redirect", "declared_alias"])
def test_indexed_canonical_contradiction_preserves_current_source_and_can_retry_corrected_html(kind: str) -> None:
    _run("const kind=" + json.dumps(kind) + ";\n" + r"""
      let contradictory=true;
      const app=setup({fetch(url,_settings,reply){
        if(kind==='http_redirect'&&contradictory&&url===bSpec.route)return reply(aSpec,aSpec.route);
      }});
      await app.nav.start();await app.nav.navigateNode('a');
      if(kind==='declared_alias')app.contents.set(bSpec.route,{...bSpec,route:aSpec.route,title:'Contradictory canonical notice'});
      const state={mode:'graph',requestedNode:'a',readingScroll:91,camera:{x:12,y:24,scale:1.2}};
      app.state(state);const before={url:app.win.location.href,length:app.win.history.length,commits:app.commits.length,
        source:app.commits.at(-1).readingSource,index:app.nav.getIndex()};
      assert.equal(await app.nav.navigateNode('b'),null);
      assert.equal(app.win.location.href,before.url);assert.equal(app.win.history.length,before.length);
      assert.equal(app.commits.length,before.commits);assert.strictEqual(app.commits.at(-1).readingSource,before.source);
      assert.deepEqual(app.getState(),state);assert.strictEqual(app.nav.getIndex(),before.index);
      assert.equal(app.errors.length,1);assert.equal(app.errors[0].url,bSpec.route+'#theorem-b');
      assert.equal(app.initial.host.getAttribute('aria-busy'),null);
      assert.equal(app.fetches.filter(url=>url===bSpec.route).length,1);
      contradictory=false;app.contents.set(bSpec.route,bSpec);
      const corrected=await app.nav.navigateNode('b');assert(corrected);
      assert.equal(corrected.url,bSpec.route+'#theorem-b');assert.equal(corrected.nodeId,'b');
      assert.equal(corrected.documentURL,bSpec.route);assert.equal(corrected.scope,'b');
      assert.equal(corrected.readingSource.querySelector('h1').textContent,bSpec.title);
      assert(corrected.readingSource.textContent.includes('Complete source statement and proof.'));
      assert.equal(app.fetches.filter(url=>url===bSpec.route).length,2,'Contradictory HTML cannot survive in the cache');
      assert.equal(app.errors.length,1);assert.equal(app.win.history.length,before.length+1);
      assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """)


def test_start_ignores_a_versioned_snapshot_carried_from_another_article_url() -> None:
    _run(r"""
      const url=bSpec.route+'#theorem-b',app=setup({initial:bSpec,url});
      const valid={mode:'graph',requestedNode:'b',readingScroll:17};app.state(valid);
      app.win.history.replaceState({material:'preserved',autoformNavigation:{version:1,url:aSpec.route+'#theorem-a',
        key:'foreign-a',snapshot:{mode:'graph',requestedNode:'a',readingScroll:91}}},'',url);
      const opened=await app.nav.start();assert(opened);
      assert.equal(opened.url,url);assert.equal(opened.nodeId,'b');assert.equal(opened.snapshot,null);
      assert.equal(opened.readingSource.querySelector('h1').textContent,bSpec.title);
      assert.deepEqual(app.restored,[]);assert.deepEqual(app.getState(),valid);
      assert.notEqual(app.win.history.state.autoformNavigation.key,'foreign-a');
      assert.equal(app.win.history.state.autoformNavigation.url,url);
      assert.deepEqual(app.win.history.state.autoformNavigation.snapshot,valid);
      assert.equal(app.win.history.state.material,'preserved');assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_popstate_ignores_foreign_url_snapshot_and_keeps_the_destination_article_identity() -> None:
    _run(r"""
      let app;app=setup({commit(bundle){app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});}});
      await app.nav.start();await app.nav.navigateNode('a');
      const url=bSpec.route+'#theorem-b',commits=app.commits.length;
      app.win.history.pushState({material:'preserved',autoformNavigation:{version:1,url:aSpec.route+'#theorem-a',
        key:'foreign-a',snapshot:{mode:'graph',requestedNode:'a',readingScroll:91}}},'',url);
      app.listeners.get('popstate')();
      await until(()=>app.commits.length===commits+1&&app.initial.host.getAttribute('aria-busy')===null);
      const opened=app.commits.at(-1);assert.equal(opened.nodeId,'b');assert.equal(opened.url,url);
      assert.equal(opened.snapshot,null);assert.equal(opened.readingSource.querySelector('h1').textContent,bSpec.title);
      assert.deepEqual(app.restored,[]);assert.equal(app.getState().requestedNode,'b');
      assert.notEqual(app.win.history.state.autoformNavigation.key,'foreign-a');
      assert.equal(app.win.history.state.autoformNavigation.url,url);
      assert.equal(app.win.history.state.autoformNavigation.snapshot.requestedNode,'b');
      assert.equal(app.win.history.state.material,'preserved');assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_postcommit_renderer_failure_reports_verified_canonical_url_and_can_retry_without_a_new_entry() -> None:
    _run(r"""
      let app,rejectOnce=true;
      app=setup({commit(bundle){
        if(bundle.nodeId==='a'&&rejectOnce){rejectOnce=false;throw new Error('Renderer rejected the committed article');}
        app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});
      }});
      await app.nav.start();const length=app.win.history.length;
      const alias=absolute('dependencies/scopes/a.html#node=a'),canonical=aSpec.route+'#theorem-a';
      assert.equal(await app.nav.navigate(alias,{focus:true}),null);
      assert.equal(app.win.location.href,canonical);assert.equal(app.win.history.length,length+1);
      assert.equal(app.commits.at(-1).nodeId,'a');assert.equal(app.errors.length,1);
      assert.equal(app.errors[0].url,canonical);assert.notEqual(app.errors[0].url,alias);
      const failedHistory=structuredClone(app.win.history.state);
      assert.equal(failedHistory.autoformNavigation.snapshot,null);
      app.state({mode:'graph',requestedNode:'roadmap',readingScroll:999});
      assert.equal(app.nav.checkpoint(),null);assert.equal(app.nav.checkpoint('push'),null);
      assert.deepEqual(app.win.history.state,failedHistory);assert.equal(app.win.history.length,length+1);
      const recovered=await app.nav.navigateNode('a');assert(recovered);
      assert.equal(recovered.url,canonical);assert.equal(recovered.nodeId,'a');
      assert.equal(recovered.readingSource.querySelector('h1').textContent,aSpec.title);
      assert.equal(app.getState().requestedNode,'a');assert.equal(app.win.history.length,length+1);
      assert.equal(app.win.history.state.autoformNavigation.snapshot.requestedNode,'a');
      assert.equal(app.errors.length,1);assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """)


@pytest.mark.parametrize("phase", ["commit", "restore"])
def test_failed_committed_view_cannot_cache_previous_article_state_for_back_then_forward(phase: str) -> None:
    _run("const phase=" + json.dumps(phase) + ";\n" + r"""
      let app,rejectOnce=true;const restorations=[];
      app=setup({commit(bundle){
        if(phase==='commit'&&bundle.nodeId==='b'&&rejectOnce){rejectOnce=false;throw new Error('Renderer rejected B');}
        app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});
      },restore(saved,bundle){
        restorations.push({nodeId:bundle.nodeId,saved:structuredClone(saved)});
        if(phase==='restore'&&bundle.nodeId==='b'&&rejectOnce){rejectOnce=false;throw new Error('Restoration rejected B');}
      }});
      await app.nav.start();await app.nav.navigateNode('a');
      const oldA={mode:'graph',requestedNode:'a',readingScroll:91};app.state(oldA);
      const url=bSpec.route+'#theorem-b';
      if(phase==='commit')assert.equal(await app.nav.navigateNode('b'),null);
      else {
        app.win.history.pushState({material:'preserved',autoformNavigation:{version:1,url,key:'failed-b',snapshot:oldA}},'',url);
        app.listeners.get('popstate')();await until(()=>app.errors.length===1&&app.initial.host.getAttribute('aria-busy')===null);
      }
      assert.equal(app.win.location.href,url);assert.equal(app.errors.length,1);assert.equal(app.errors[0].url,url);
      assert.equal(app.win.history.state.autoformNavigation.snapshot,null);
      const failedHistory=structuredClone(app.win.history.state),length=app.win.history.length;
      app.state(oldA);assert.equal(app.nav.checkpoint(),null);assert.equal(app.nav.checkpoint('push'),null);
      assert.deepEqual(app.win.history.state,failedHistory);assert.equal(app.win.history.length,length);
      app.win.history.back();
      await until(()=>app.commits.at(-1).nodeId==='a'&&app.initial.host.getAttribute('aria-busy')===null);
      const count=app.commits.length;app.win.history.forward();
      await until(()=>app.commits.length===count+1&&app.initial.host.getAttribute('aria-busy')===null);
      const recovered=app.commits.at(-1);assert.equal(recovered.nodeId,'b');assert.equal(recovered.url,url);
      assert.equal(recovered.readingSource.querySelector('h1').textContent,bSpec.title);
      assert.equal(recovered.snapshot,null);assert.equal(app.getState().requestedNode,'b');
      assert.equal(app.win.history.state.autoformNavigation.snapshot.requestedNode,'b');
      assert.equal(app.errors.length,1);assert.equal(app.win.history.length,length);
      const restoredB=restorations.filter(entry=>entry.nodeId==='b');
      assert.equal(restoredB.length,phase==='restore'?1:0,'Recovery must not reuse the failed B snapshot');
      if(phase==='restore')assert.deepEqual(restoredB[0].saved,oldA);
      assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """)


def test_failed_current_view_cannot_take_the_native_section_shortcut_with_previous_reading_dom() -> None:
    _run(r"""
      let app,rejectOnce=true;app=setup({commit(bundle){
        if(bundle.nodeId==='b'&&rejectOnce){rejectOnce=false;throw new Error('Renderer rejected B');}
        app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});
      }});
      await app.nav.start();await app.nav.navigateNode('a');assert.equal(await app.nav.navigateNode('b'),null);
      const count=app.commits.length,section=bSpec.route+'#appendix';
      app.win.history.pushState({},'',section);app.listeners.get('popstate')();
      await until(()=>app.commits.length===count+1&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).url,section);
      assert.equal(app.commits.at(-1).readingSource.querySelector('h1').textContent,bSpec.title);
      assert.equal(app.errors.length,1);assert.equal(app.win.location.href,section);app.nav.dispose();
    """)


def test_valid_pending_renderer_readiness_still_allows_current_view_checkpoints() -> None:
    _run(r"""
      let app,release,waiting=false;app=setup({commit(bundle){
        app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});
        if(bundle.nodeId==='b'){waiting=true;return new Promise(resolve=>{release=resolve;});}
      }});
      await app.nav.start();await app.nav.navigateNode('a');const length=app.win.history.length;
      const pending=app.nav.navigateNode('b');await until(()=>waiting);
      const edited={mode:'graph',requestedNode:'b',readingScroll:71,camera:{x:11,y:23,scale:1.4}};
      app.state(edited);assert.deepEqual(app.nav.checkpoint(),edited);
      const firstKey=app.win.history.state.autoformNavigation.key;
      assert.deepEqual(app.nav.checkpoint('push'),edited);assert.equal(app.win.history.length,length+2);
      const pushedKey=app.win.history.state.autoformNavigation.key;assert.notEqual(pushedKey,firstKey);
      assert.equal(app.win.location.href,bSpec.route+'#theorem-b');
      release();const opened=await pending;assert(opened);assert.equal(opened.nodeId,'b');
      assert.equal(app.win.history.state.autoformNavigation.key,pushedKey);
      assert.deepEqual(app.win.history.state.autoformNavigation.snapshot,edited);assert.deepEqual(app.getState(),edited);
      assert.equal(app.errors.length,0);assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """)


def test_failed_history_clear_distrusts_surviving_browser_snapshot_but_keeps_future_private_checkpoints() -> None:
    _run(r"""
      let app,rejectOnce=true,clearFails=true;const restorations=[];
      app=setup({commit(bundle){app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});},
        restore(saved,bundle){
          restorations.push({nodeId:bundle.nodeId,saved:structuredClone(saved)});
          if(bundle.nodeId==='b'&&rejectOnce){rejectOnce=false;throw new Error('Restoration rejected B');}
        }});
      await app.nav.start();await app.nav.navigateNode('a');
      const url=bSpec.route+'#theorem-b',oldA={mode:'graph',requestedNode:'a',readingScroll:91};
      const replace=app.win.history.replaceState;
      app.win.history.replaceState=(state,title,target)=>{
        if(clearFails&&target===url&&state.autoformNavigation&&state.autoformNavigation.snapshot===null){
          clearFails=false;throw new Error('History refused the failed-view snapshot clear');
        }
        return replace(state,title,target);
      };
      app.win.history.pushState({material:'preserved',autoformNavigation:{version:1,url,key:'failed-b',snapshot:oldA}},'',url);
      app.listeners.get('popstate')();
      await until(()=>app.errors.length===1&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(clearFails,false,'The actual failed-view clearing path must encounter the injected exception');
      assert.deepEqual(app.win.history.state.autoformNavigation.snapshot,oldA,'Browser state really survived the failed write');
      assert.equal(app.errors[0].url,url);assert.equal(app.win.location.href,url);assert.equal(app.nav.checkpoint(),null);
      const length=app.win.history.length;
      app.win.history.back();
      await until(()=>app.commits.at(-1).nodeId==='a'&&app.initial.host.getAttribute('aria-busy')===null);
      const count=app.commits.length;app.win.history.forward();
      await until(()=>app.commits.length===count+1&&app.initial.host.getAttribute('aria-busy')===null);
      const recovered=app.commits.at(-1);assert.equal(recovered.nodeId,'b');assert.equal(recovered.url,url);
      assert.equal(recovered.snapshot,null);assert.equal(app.getState().requestedNode,'b');
      assert.equal(recovered.readingSource.querySelector('h1').textContent,bSpec.title);
      assert.equal(restorations.filter(entry=>entry.nodeId==='b').length,1,'The rejected B restore must not recur');
      assert.deepEqual(restorations.find(entry=>entry.nodeId==='b').saved,oldA);
      const edited={mode:'graph',requestedNode:'b',readingScroll:73,camera:{x:15,y:29,scale:1.3}};
      app.state(edited);assert.deepEqual(app.nav.checkpoint(),edited);
      app.win.history.back();
      await until(()=>app.commits.at(-1).nodeId==='a'&&app.initial.host.getAttribute('aria-busy')===null);
      const again=app.commits.length;app.win.history.forward();
      await until(()=>app.commits.length===again+1&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).nodeId,'b');assert.deepEqual(app.commits.at(-1).snapshot,edited);
      assert.deepEqual(app.getState(),edited,'Fresh private snapshots still work after browser fallback is disabled');
      assert.equal(app.errors.length,1);assert.equal(app.win.history.length,length);
      assert.equal(app.win.history.state.material,'preserved');assert.equal(app.win.location.href,url);app.nav.dispose();
    """)


def test_renderer_failure_after_pending_checkpoint_push_cannot_restore_a_under_either_b_entry() -> None:
    _run(r"""
      let app,waiting=false,rejectPending,holdOnce=true;
      app=setup({commit(bundle){
        if(bundle.nodeId==='b'&&holdOnce){
          holdOnce=false;waiting=true;return new Promise((_resolve,reject)=>{rejectPending=reject;});
        }
        app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});
      }});
      await app.nav.start();await app.nav.navigateNode('a');
      const oldA={mode:'graph',requestedNode:'a',readingScroll:91};app.state(oldA);
      const length=app.win.history.length,pending=app.nav.navigateNode('b');await until(()=>waiting);
      const url=bSpec.route+'#theorem-b';assert.equal(app.win.location.href,url);
      const originalKey=app.win.history.state.autoformNavigation.key;
      assert.deepEqual(app.nav.checkpoint(),oldA);
      assert.deepEqual(app.nav.checkpoint('push'),oldA);
      const pushedKey=app.win.history.state.autoformNavigation.key;
      assert.notEqual(pushedKey,originalKey);assert.equal(app.win.history.length,length+2);
      rejectPending(new Error('Delayed renderer rejected B'));assert.equal(await pending,null);
      assert.equal(app.win.location.href,url);assert.equal(app.errors.length,1);assert.equal(app.errors[0].url,url);
      assert.equal(app.win.history.state.autoformNavigation.key,pushedKey);
      assert.equal(app.win.history.state.autoformNavigation.snapshot,null);assert.equal(app.nav.checkpoint(),null);
      const count=app.commits.length;app.win.history.back();
      await until(()=>app.commits.length===count+1&&app.initial.host.getAttribute('aria-busy')===null);
      const first=app.commits.at(-1);assert.equal(first.nodeId,'b');assert.equal(first.url,url);
      assert.equal(first.snapshot,null,'The older pending-checkpoint entry must not supply A state');
      assert.equal(app.getState().requestedNode,'b');assert.deepEqual(app.restored,[]);
      assert.equal(app.win.history.state.autoformNavigation.key,originalKey);
      assert.equal(first.readingSource.querySelector('h1').textContent,bSpec.title);
      const again=app.commits.length;app.win.history.forward();
      await until(()=>app.commits.length===again+1&&app.initial.host.getAttribute('aria-busy')===null);
      const second=app.commits.at(-1);assert.equal(second.nodeId,'b');assert.equal(second.url,url);
      assert.equal(second.snapshot,null);assert.equal(app.getState().requestedNode,'b');assert.deepEqual(app.restored,[]);
      assert.equal(app.win.history.state.autoformNavigation.key,pushedKey);
      assert.equal(second.readingSource.querySelector('h1').textContent,bSpec.title);
      assert.equal(app.errors.length,1);assert.equal(app.win.history.length,length+2);
      assert.equal(app.win.history.state.material,'preserved');assert.equal(app.win.location.href,url);app.nav.dispose();
    """)


@pytest.mark.parametrize("phase", ["sync", "async"])
def test_native_section_restore_failure_quarantines_its_snapshot_and_recovers_on_back_forward(phase: str) -> None:
    _run("const phase=" + json.dumps(phase) + ";\n" + r"""
      let app,failOnce=true;const section=aSpec.route+'#appendix',restorations=[];
      app=setup({commit(bundle){app.state({requestedNode:bundle.nodeId,readingScroll:0});},
        restore(saved,bundle){
          restorations.push({url:bundle.url,saved:structuredClone(saved)});
          if(bundle.url===section&&failOnce){
            failOnce=false;app.state({requestedNode:'b',readingScroll:999});
            const error=new Error('Native section restoration rejected');
            if(phase==='sync')throw error;
            return Promise.reject(error);
          }
        }});
      await app.nav.start();await app.nav.navigateNode('a');
      const saved={requestedNode:'a',readingScroll:71},commits=app.commits.length;
      app.state(saved);app.win.history.pushState({material:'preserved',autoformNavigation:{version:1,
        url:section,key:'native-section',snapshot:saved}},'',section);
      assert.doesNotThrow(()=>app.listeners.get('popstate')());
      await until(()=>app.errors.length===1&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.length,commits,'Failure must occur in the actual native-section shortcut');
      assert.equal(app.errors[0].url,section);assert.equal(app.win.location.href,section);
      assert.equal(app.win.history.state.autoformNavigation.key,'native-section');
      assert.equal(app.win.history.state.autoformNavigation.snapshot,null);
      assert.equal(app.win.history.state.material,'preserved');
      const failedHistory=structuredClone(app.win.history.state),length=app.win.history.length;
      assert.equal(app.nav.checkpoint(),null);assert.equal(app.nav.checkpoint('push'),null);
      assert.deepEqual(app.win.history.state,failedHistory);assert.equal(app.win.history.length,length);
      app.win.history.back();
      await until(()=>app.commits.length===commits+1&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).nodeId,'a');assert.equal(app.getState().requestedNode,'a');
      const recovered=app.commits.length;app.win.history.forward();
      await until(()=>app.win.location.href===section&&app.initial.host.getAttribute('aria-busy')===null);
      await new Promise(resolve=>setImmediate(resolve));
      assert.equal(app.commits.length,recovered,'Healthy ordinary section navigation must stay native');
      assert.equal(restorations.filter(entry=>entry.url===section).length,1,'Rejected snapshot must not be restored twice');
      assert.deepEqual(restorations.find(entry=>entry.url===section).saved,saved);
      assert.equal(app.getState().requestedNode,'a');assert.notEqual(app.getState().readingScroll,999);
      assert.equal(app.win.history.state.autoformNavigation.snapshot,null);assert.equal(app.errors.length,1);
      assert.equal(app.win.history.length,length);assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """)


@pytest.mark.parametrize("phase", ["reentrant_sync", "late_async"])
def test_late_native_section_restore_rejection_cannot_quarantine_a_newer_committed_article(phase: str) -> None:
    _run("const phase=" + json.dumps(phase) + ";\n" + r"""
      let app,rejectRestore,newer;const section=aSpec.route+'#appendix';
      app=setup({commit(bundle){app.state({requestedNode:bundle.nodeId,readingScroll:0});},
        restore(_saved,bundle){if(bundle.url===section){
          if(phase==='reentrant_sync'){newer=app.nav.navigateNode('b');throw new Error('Superseded native restore threw');}
          return new Promise((_resolve,reject)=>{rejectRestore=reject;});
        }}});
      await app.nav.start();await app.nav.navigateNode('a');
      app.win.history.pushState({material:'preserved',autoformNavigation:{version:1,url:section,
        key:'pending-native-section',snapshot:{requestedNode:'a',readingScroll:71}}},'',section);
      assert.doesNotThrow(()=>app.listeners.get('popstate')());
      if(phase==='late_async'){assert.equal(typeof rejectRestore,'function');newer=app.nav.navigateNode('b');}
      const opened=await newer;assert(opened);
      if(phase==='late_async')rejectRestore(new Error('Stale native section restore rejected'));
      await new Promise(resolve=>setImmediate(resolve));
      assert.equal(app.win.location.href,bSpec.route+'#theorem-b');assert.equal(app.getState().requestedNode,'b');
      assert.equal(app.errors.length,0);assert.equal(app.initial.host.getAttribute('aria-busy'),null);
      const edited={requestedNode:'b',readingScroll:83};app.state(edited);
      assert.deepEqual(app.nav.checkpoint(),edited,'Stale rejection must not mark the current B renderer failed');
      assert.deepEqual(app.win.history.state.autoformNavigation.snapshot,edited);
      assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """)


def test_rejected_popstate_history_preparation_reports_canonical_url_and_distrusts_surviving_snapshot() -> None:
    _run(r"""
      let app,rejectOnce=true;const section=aSpec.route+'#appendix',restorations=[];
      app=setup({commit(bundle){app.state({requestedNode:bundle.nodeId,readingScroll:0});},
        restore(saved,bundle){restorations.push({url:bundle.url,saved:structuredClone(saved)});}});
      await app.nav.start();await app.nav.navigateNode('a');
      const saved={requestedNode:'a',readingScroll:71},commits=app.commits.length;app.state(saved);
      app.win.history.pushState({material:'preserved',autoformNavigation:{version:1,url:section,
        key:'history-preparation-failed',snapshot:saved}},'',section);
      const replace=app.win.history.replaceState;
      app.win.history.replaceState=(state,title,url)=>{
        if(url===section&&rejectOnce){rejectOnce=false;throw new Error('History rejected native section preparation');}
        return replace(state,title,url);
      };
      assert.doesNotThrow(()=>app.listeners.get('popstate')());
      assert.equal(rejectOnce,false);assert.equal(app.errors.length,1);assert.equal(app.errors[0].url,section);
      assert.equal(app.win.location.href,section);assert.equal(app.commits.length,commits);
      assert.deepEqual(app.getState(),saved);assert.deepEqual(app.restored,[]);
      assert.deepEqual(app.win.history.state.autoformNavigation.snapshot,saved,'The refused write really left browser state intact');
      assert.equal(app.nav.checkpoint(),null);assert.equal(app.win.history.state.material,'preserved');
      const length=app.win.history.length;app.win.history.back();
      await until(()=>app.commits.length===commits+1&&app.initial.host.getAttribute('aria-busy')===null);
      const returned=app.commits.length;app.win.history.forward();
      await until(()=>app.win.location.href===section&&app.initial.host.getAttribute('aria-busy')===null);
      await new Promise(resolve=>setImmediate(resolve));
      assert.equal(app.commits.length,returned);assert.equal(app.getState().requestedNode,'a');
      assert.equal(restorations.filter(entry=>entry.url===section).length,0,'Uncleared browser snapshot must remain quarantined');
      assert.equal(app.win.history.state.autoformNavigation.snapshot,null);assert.equal(app.errors.length,1);
      const fresh={requestedNode:'a',readingScroll:93};app.state(fresh);assert.deepEqual(app.nav.checkpoint(),fresh);
      app.win.history.back();await until(()=>app.commits.length===returned+1&&app.initial.host.getAttribute('aria-busy')===null);
      app.win.history.forward();await until(()=>app.getState().readingScroll===93);
      assert.deepEqual(app.getState(),fresh,'Fresh private section snapshots remain valid after quarantine');
      assert.equal(app.win.location.href,section);assert.equal(app.win.history.length,length);
      assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """)
