"""Production wiki reader callbacks in a bounded Node VM, not browser or mathematical evidence."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated wiki projection tests")
_CALLBACKS = ("element", "readingLabel", "refreshReadingLabels", "button", "recordId", "requestWikiLinks", "useWikiLinks", "wikiNeighbors", "renderWikiContext")

_LOADER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(CLIENT,'utf8');
function original(name) {
  const start=source.indexOf('function '+name+'(');assert(start>=0,'Missing production callback: '+name);
  for(let end=source.indexOf('}',start);end>=0;end=source.indexOf('}',end+1)) {
    const declaration=source.slice(start,end+1);
    try {new vm.Script('('+declaration+')');return declaration;}
    catch(error) {if(!(error instanceof SyntaxError))throw error;}
  }
  assert.fail('Unparseable production callback: '+name);
}
const pluralStart=source.indexOf('  var plural =');assert(pluralStart>=0);
const plural=source.slice(pluralStart,source.indexOf('\n',pluralStart));new vm.Script(plural);
const production=plural+'\n'+NAMES.map(original).join('\n'),context={assert,setImmediate};vm.createContext(context);
"""

_DOM = r"""
class Element {
  constructor(tag) {
    this.tagName=tag.toUpperCase();this.children=[];this.attrs=new Map();this.listeners=new Map();
    this.className='';this.text='';this.open=false;this.hidden=false;this.scrollTop=0;this.parentElement=null;this.dataset={};
    this.classList={add:(name)=>{this.className+=' '+name;},
      remove:(name)=>{this.className=this.className.split(/\s+/).filter(part=>part!==name).join(' ');}};
  }
  get textContent() {return this.text+this.children.map(child=>child.textContent).join('');}
  set textContent(value) {this.replaceChildren();this.text=String(value);}
  get open() {return this.attrs.has('open');}
  set open(value) {if(value)this.attrs.set('open','');else this.attrs.delete('open');}
  setAttribute(name,value) {this.attrs.set(name,String(value));}
  getAttribute(name) {return this.attrs.has(name)?this.attrs.get(name):null;}
  appendChild(child) {child.remove();child.parentElement=this;this.children.push(child);return child;}
  insertBefore(child,before) {
    assert(this.children.includes(before));child.remove();child.parentElement=this;
    this.children.splice(this.children.indexOf(before),0,child);return child;
  }
  replaceChildren(...children) {
    this.children.forEach(child=>{child.parentElement=null;});this.children=[];this.text='';
    children.forEach(child=>this.appendChild(child));
  }
  remove() {
    if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(child=>child!==this);
    this.parentElement=null;
  }
  addEventListener(type,callback) {this.listeners.set(type,callback);}
  all() {return this.children.flatMap(child=>[child,...child.all()]);}
  matches(selector) {
    return selector.startsWith('.')?this.className.split(/\s+/).includes(selector.slice(1)):
      this.tagName===selector.toUpperCase();
  }
  querySelector(selector) {return this.all().find(child=>child.matches(selector))||null;}
  querySelectorAll(selector) {return this.all().filter(child=>child.matches(selector));}
  closest(selector) {return this.matches(selector)?this:this.parentElement&&this.parentElement.closest(selector);}
  focus() {document.activeElement=this;}
}
function emit(node,type,settings={}) {
  const event=Object.assign({button:0,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}},settings);
  const callback=node.listeners.get(type);assert(callback,'Expected actual production event callback');
  callback(event);return event;
}
"""

_READER_PEERS = _DOM + r"""
const document={createElement:tag=>new Element(tag)};
const window={};
const dom$readingNeighbors=new Element('div'),dom$readingBody=new Element('div');
const math=new Element('article');math.textContent='Complete authored mathematical page';dom$readingBody.appendChild(math);
dom$readingBody.scrollTop=137;
const structural=new Element('details');structural.className='bp-reading-related-topics';
structural.textContent='Existing structural topic peers';dom$readingNeighbors.appendChild(structural);
var alive=true,ui$wikiLinks=null,ui$wikiBefore=new Map(),ui$wikiAfter=new Map(),ui$wikiUnavailable=new Set();
var ui$wikiGeneration=0,ui$wikiPending=null,ui$wikiWork=null,ui$wikiRendered=null,ui$wikiNode=null;
var ui$wikiRequested=false,ui$selected=null,ui$requestedNode=null,ui$mode='graph',ui$area='Geometry';
var ui$overviewGroup='authored-area',ui$transform={x:17,y:31,scale:1.25},ui$query='retained query';
var ui$selectedStatuses=new Set(['planned','fully_proved']),ui$relationIds=['concept/in-1'];
var navigation={},records=new Map(),sharedIndex,currentBundle={nodeId:'F',scope:'chapter'},contextGraph;
var readingReady=new Promise(()=>{});
const calls=[];let readerRenders=0;
function updateProjection() {throw new Error('Wiki reading must not change the graph projection');}
function projectWikiView() {}
function node(id,title=id) {
  return Object.freeze({id,title,scope:'chapter',parent:id==='chapter'?'roadmap':'chapter',
    kind:id==='chapter'?'scope':'node',status:'planned',result:id!=='chapter',
    route:'https://example.test/roadmap/'+encodeURIComponent(id)+'.html#statement',
    url:'https://example.test/dependencies/chapter.html#node='+encodeURIComponent(id)});
}
function configure(count=2) {
  const pool=[node('F','Focal concept'),node('chapter','Authored chapter')];
  for(let n=1;n<=count;n++)pool.push(node('concept/out-'+n,'Outgoing concept '+n),node('concept/in-'+n,'Incoming concept '+n));
  records=new Map(pool.map(record=>[record.id,record]));
  sharedIndex={schema:'autoform-dag-search/v1',nodes:pool,relations:[['concept/in-1','F',1,0]]};
  contextGraph={nodes:pool.slice(),edges:[{source:'concept/in-1',target:'F',statement:1,proof:0}],view:{presentation:'dag'}};
  navigation.navigateNode=(id,options)=>calls.push({id,options});
}
function layer(references,availability='complete',unavailable_sources=[]) {
  return {schema:'autoform-wiki-links/v1',index_sha256:'d'.repeat(64),availability,references,unavailable_sources};
}
function snapshot(state) {
  return JSON.stringify({state,sharedIndex,contextGraph,records:Array.from(records.values()),
    mode:ui$mode,area:ui$area,group:ui$overviewGroup,camera:ui$transform,query:ui$query,
    statuses:Array.from(ui$selectedStatuses),relationIds:ui$relationIds,bundle:currentBundle,
    reading:dom$readingBody.textContent,scroll:dom$readingBody.scrollTop,structural:structural.textContent});
}
function disclosure() {return dom$readingNeighbors.querySelector('.bp-reading-wiki-links');}
function groups() {return disclosure().querySelectorAll('.bp-reading-wiki-related');}
function headings() {return groups().map(group=>group.querySelector('strong').textContent);}
function markup(root) {
  return JSON.stringify([root,...root.all()].map(node=>({text:node.textContent,attrs:Array.from(node.attrs),
    href:node.href,title:node.title,dataset:node.dataset})));
}
function freeze(value) {
  if(value&&typeof value==='object'){Object.values(value).forEach(freeze);Object.freeze(value);}return value;
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
configure();
"""

_PEERS = _READER_PEERS + r"""
function renderReaderContext() {readerRenders++;}
function renderInspector() {throw new Error('Wiki completion must not rerender mathematical reading');}
"""

_CHUNK_PEERS = _PEERS + r"""
// The timer boundary is controlled; all row processing and generation guards
// come from the extracted production callback and native promises.
const scheduled=[];let yields=0;
function setTimeout(callback,delay) {assert.equal(delay,0);scheduled.push(callback);yields++;return yields;}
async function nextChunk() {
  const callback=scheduled.shift();assert(callback,'Expected a scheduled cooperative chunk');callback();await flush();
}
function largeLayer(target='concept/out-2') {
  return freeze(layer(Array.from({length:2048},()=>['F',target]),'partial',['F']));
}
"""


def _run(body: str, *, callbacks: tuple[str, ...] = _CALLBACKS, peers: str = _PEERS) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ",NAMES=" + json.dumps(callbacks) + ";\n" + _LOADER
    program = peers + "\n(async()=>{\n" + body + "\n})()"
    script += "const deadline=setTimeout(()=>{console.error('Wiki callback test did not settle');process.exitCode=1;},5000);\n"
    script += "Promise.resolve(vm.runInContext(production+" + json.dumps(program)
    script += ",context,{timeout:2000})).then(()=>clearTimeout(deadline),error=>{clearTimeout(deadline);console.error(error);process.exitCode=1;});\n"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def test_projection_uses_canonical_ids_deduplicates_and_never_changes_formal_graph_or_source() -> None:
    _run(r"""
      const state=freeze(layer([['F','concept/out-1'],['F','concept/out-1'],['concept/in-1','F'],
        ['F','F'],['unknown','F'],['F','unknown'],['F','chapter']]));
      const before=snapshot(state);useWikiLinks(state);
      assert.deepEqual(wikiNeighbors('F'),{before:['concept/in-1'],after:['concept/out-1','chapter'],
        availability:'complete',outgoingAvailable:true,incomingComplete:true});
      assert.deepEqual(wikiNeighbors('concept/out-1').before,['F']);
      assert.deepEqual(wikiNeighbors('concept/in-1').after,['F']);
      assert.deepEqual(wikiNeighbors('unknown'),{before:[],after:[],availability:'complete',
        outgoingAvailable:false,incomingComplete:false});
      assert.equal(snapshot(state),before);assert.strictEqual(ui$wikiLinks,state);assert.equal(readerRenders,0);
      assert.equal(disclosure().tagName,'DETAILS');assert.equal(disclosure().open,false);
      assert.equal(disclosure().querySelector('summary').textContent,'Concept links');
      assert.deepEqual(headings(),['Links to other concepts · 2','Linked from · 1']);
      assert.strictEqual(dom$readingNeighbors.children.at(-1),structural);
      const existing=disclosure();useWikiLinks(state);assert.strictEqual(disclosure(),existing);
    """)


@pytest.mark.parametrize("has_outgoing", [False, True])
def test_partial_source_keeps_known_backlinks_and_marks_outgoing_coverage_unknown(has_outgoing: bool) -> None:
    _run("const hasOutgoing=" + json.dumps(has_outgoing) + ";\n" + r"""
      const refs=[['concept/in-1','F']];if(hasOutgoing)refs.push(['F','concept/out-1']);
      const state=freeze({...layer(refs,'partial',['F','not-indexed']),reason:'/private/provider/parse.log'});
      const before=snapshot(state);useWikiLinks(state);
      assert.deepEqual(wikiNeighbors('F'),{before:['concept/in-1'],after:hasOutgoing?['concept/out-1']:[],
        availability:'partial',outgoingAvailable:false,incomingComplete:false});
      assert.deepEqual(Array.from(ui$wikiUnavailable),['F']);
      assert.deepEqual(headings(),['Links to other concepts · '+(hasOutgoing?'1 known':'unavailable'),'Linked from · 1 known']);
      assert(disclosure().textContent.includes('Links from this page could not be read completely.'));
      assert(disclosure().textContent.includes('may be incomplete'));
      assert(!markup(disclosure()).includes(state.reason));assert(!disclosure().textContent.includes(' · 0'));
      assert.equal(groups()[1].querySelector('a').href,records.get('concept/in-1').route);
      assert.equal(snapshot(state),before);assert.equal(readerRenders,0);
    """)


def test_partial_global_incoming_count_is_unknown_when_no_backlink_is_known() -> None:
    _run(r"""
      useWikiLinks(layer([['F','concept/out-1']],'partial',['concept/in-2']));
      assert.deepEqual(headings(),['Links to other concepts · 1','Linked from · unknown']);
      assert.deepEqual(wikiNeighbors('F'),{before:[],after:['concept/out-1'],availability:'partial',
        outgoingAvailable:true,incomingComplete:false});
    """)


def test_unavailable_layer_has_no_references_or_private_error_text_and_empty_complete_layer_has_no_notice() -> None:
    _run(r"""
      const state=layer([['F','concept/out-1'],['concept/in-1','F']],'unavailable');
      state.reason='private-provider-wire:/private/parser/error.log';const before=snapshot(state);useWikiLinks(state);
      assert.deepEqual(wikiNeighbors('F'),{before:[],after:[],availability:'unavailable',
        outgoingAvailable:false,incomingComplete:false});
      assert.deepEqual(headings(),['Links to other concepts · unavailable','Linked from · unavailable']);
      assert.equal(disclosure().querySelectorAll('a').length,0);
      assert(disclosure().textContent.includes('unavailable for this publication'));
      assert(!markup(disclosure()).includes(state.reason));assert.equal(snapshot(state),before);
      useWikiLinks(layer([]));assert.equal(disclosure(),null);assert.strictEqual(dom$readingNeighbors.children[0],structural);
    """)


def test_native_canonical_links_preserve_modifiers_and_dispatch_reading_navigation() -> None:
    _run(r"""
      useWikiLinks(layer([['F','concept/out-1'],['concept/in-1','F']]));const before=snapshot(ui$wikiLinks);
      const outgoing=groups()[0].querySelector('a'),incoming=groups()[1].querySelector('a');
      assert.equal(outgoing.href,records.get('concept/out-1').route);assert.equal(outgoing.textContent,'Outgoing concept 1');
      assert.equal(incoming.href,records.get('concept/in-1').route);
      for(const link of [outgoing,incoming]) {
        for(const settings of [{metaKey:true},{ctrlKey:true},{shiftKey:true},{altKey:true},{button:1},{button:2},{defaultPrevented:true}]) {
          const event=emit(link,'click',settings);assert.equal(calls.length,0);
          assert.equal(event.defaultPrevented,Boolean(settings.defaultPrevented));
        }
      }
      assert.equal(emit(outgoing,'click').defaultPrevented,true);assert.equal(emit(incoming,'click').defaultPrevented,true);
      assert.deepEqual(calls,[{id:'concept/out-1',options:{history:'push',focus:'reading'}},
        {id:'concept/in-1',options:{history:'push',focus:'reading'}}]);
      assert.equal(snapshot(ui$wikiLinks),before);
    """)


def test_show_more_is_local_to_each_direction_and_late_updates_preserve_reader_and_open_disclosure() -> None:
    _run(r"""
      configure(27);const refs=[];for(let n=1;n<=27;n++)refs.push(['F','concept/out-'+n],['concept/in-'+n,'F']);
      const state=freeze(layer(refs)),before=snapshot(state);useWikiLinks(state);
      const [outgoing,incoming]=groups();assert.deepEqual(headings(),['Links to other concepts · 27','Linked from · 27']);
      assert.equal(outgoing.querySelectorAll('a').length,5);assert.equal(incoming.querySelectorAll('a').length,5);
      const more=outgoing.querySelector('button');assert.equal(more.textContent,'Show 20 more links');
      emit(more,'click');assert.equal(outgoing.querySelectorAll('a').length,25);
      assert.equal(incoming.querySelectorAll('a').length,5);assert.equal(more.textContent,'Show 2 more links');
      emit(more,'click');assert.equal(outgoing.querySelectorAll('a').length,27);assert.equal(more.hidden,true);
      emit(incoming.querySelector('button'),'click');assert.equal(incoming.querySelectorAll('a').length,25);
      assert.equal(calls.length,0);assert.equal(snapshot(state),before);
      const previous=disclosure();previous.open=true;useWikiLinks(layer(refs,'partial',['concept/in-27']));
      assert.notStrictEqual(disclosure(),previous);assert.equal(previous.parentElement,null);assert.equal(disclosure().open,true);
      assert.strictEqual(dom$readingNeighbors.children.at(-1),structural);assert.strictEqual(dom$readingBody.children[0],math);
      assert.equal(snapshot(state),before);assert.equal(readerRenders,0);
    """)


@pytest.mark.parametrize("cached", [False, True])
def test_request_waits_for_index_coalesces_and_does_not_wait_for_mathematical_readiness(cached: bool) -> None:
    _run("const cached=" + json.dumps(cached) + ";\n" + r"""
      const state=layer([['F','concept/out-1']]);let loads=0,gets=0,finish;
      navigation.getWikiLinks=()=>{gets++;return cached?state:null;};
      navigation.loadWikiLinks=()=>{loads++;return new Promise(resolve=>{finish=resolve;});};
      const index=sharedIndex;sharedIndex=null;requestWikiLinks();assert.equal(ui$wikiRequested,false);assert.equal(loads,0);
      sharedIndex=index;assert.equal(requestWikiLinks(),undefined);assert.equal(ui$wikiRequested,true);
      assert.equal(loads,1);assert.equal(gets,1);assert.strictEqual(ui$wikiLinks,cached?state:null);
      const previous=disclosure();requestWikiLinks();requestWikiLinks();assert.equal(loads,1);assert.equal(gets,1);
      finish(state);await flush();assert.strictEqual(ui$wikiLinks,state);
      if(cached)assert.strictEqual(disclosure(),previous);
      assert.equal(readerRenders,0);assert(readingReady instanceof Promise);
    """)


@pytest.mark.parametrize("failure", ["missing", "getter", "synchronous", "rejected"])
def test_optional_request_failure_is_guarded_and_renders_generic_unavailability(failure: str) -> None:
    _run("const failure=" + json.dumps(failure) + ";\n" + r"""
      const error=new Error('private-wire:/private/provider/error.log');
      if(failure==='getter')navigation.getWikiLinks=()=>{throw error;};
      if(failure==='synchronous')navigation.loadWikiLinks=()=>{throw error;};
      if(failure==='rejected')navigation.loadWikiLinks=()=>Promise.reject(error);
      assert.doesNotThrow(()=>requestWikiLinks());await flush();
      assert.equal(ui$wikiRequested,true);assert.equal(ui$wikiLinks.availability,'unavailable');
      assert.deepEqual(wikiNeighbors('F').before,[]);assert.deepEqual(wikiNeighbors('F').after,[]);
      assert(!disclosure().textContent.includes(error.message));assert.equal(readerRenders,0);
    """)


def test_nonfunction_optional_getter_does_not_block_a_successful_loader() -> None:
    _run(r"""
      const state=layer([['F','concept/out-1']]);navigation.getWikiLinks=true;
      navigation.loadWikiLinks=()=>Promise.resolve(state);assert.doesNotThrow(()=>requestWikiLinks());await flush();
      assert.strictEqual(ui$wikiLinks,state);assert.deepEqual(wikiNeighbors('F').after,['concept/out-1']);
    """)


@pytest.mark.parametrize("reject", [False, True])
def test_disposed_projection_ignores_late_success_failure_and_direct_updates(reject: bool) -> None:
    _run("const reject=" + json.dumps(reject) + ";\n" + r"""
      let finish,fail;navigation.loadWikiLinks=()=>new Promise((resolve,rejected)=>{finish=resolve;fail=rejected;});
      requestWikiLinks();alive=false;const state=layer([['F','concept/out-1']]);
      if(reject)fail(new Error('late optional failure'));else finish(state);await flush();
      useWikiLinks(state);assert.equal(ui$wikiLinks,null);assert.equal(disclosure(),null);
      ui$wikiRequested=false;requestWikiLinks();assert.equal(ui$wikiRequested,false);assert.equal(readerRenders,0);
    """)


def test_large_projection_yields_in_bounded_chunks_and_publishes_all_maps_atomically() -> None:
    _run(r"""
      const initial=layer([['F','concept/out-1']]);useWikiLinks(initial);assert.equal(yields,0);
      const before=snapshot(initial),oldAfter=ui$wikiAfter,oldBefore=ui$wikiBefore,oldUnavailable=ui$wikiUnavailable;
      const existing=disclosure(),state=largeLayer(),sourceBefore=JSON.stringify(state),work=useWikiLinks(state);
      assert(work instanceof Promise);assert.strictEqual(useWikiLinks(state),work);
      assert.equal(yields,1);assert.equal(scheduled.length,1);assert.strictEqual(ui$wikiLinks,initial);
      assert.strictEqual(ui$wikiAfter,oldAfter);assert.strictEqual(ui$wikiBefore,oldBefore);
      assert.strictEqual(ui$wikiUnavailable,oldUnavailable);assert.strictEqual(disclosure(),existing);
      await nextChunk();assert.equal(yields,2);assert.equal(scheduled.length,1);
      assert.strictEqual(ui$wikiLinks,initial);assert.strictEqual(ui$wikiAfter,oldAfter);
      assert.strictEqual(ui$wikiUnavailable,oldUnavailable);assert.strictEqual(disclosure(),existing);
      await nextChunk();await work;assert.equal(scheduled.length,0);assert.equal(yields,2);
      assert.strictEqual(ui$wikiLinks,state);assert.notStrictEqual(ui$wikiAfter,oldAfter);
      assert.notStrictEqual(ui$wikiBefore,oldBefore);assert.notStrictEqual(ui$wikiUnavailable,oldUnavailable);
      assert.deepEqual(wikiNeighbors('F'),{before:[],after:['concept/out-2'],availability:'partial',
        outgoingAvailable:false,incomingComplete:false});
      assert.deepEqual(Array.from(ui$wikiUnavailable),['F']);assert.equal(JSON.stringify(state),sourceBefore);
      assert.equal(snapshot(initial),before);assert.equal(readerRenders,0);
    """, peers=_CHUNK_PEERS)


@pytest.mark.parametrize("replacement_pending", [False, True])
def test_new_generation_interrupts_pending_work_without_publishing_obsolete_maps(replacement_pending: bool) -> None:
    _run("const replacementPending=" + json.dumps(replacement_pending) + ";\n" + r"""
      const initial=layer([['concept/in-1','F']]);useWikiLinks(initial);const existing=disclosure();
      const obsolete=useWikiLinks(largeLayer('concept/out-1'));
      const latest=replacementPending?largeLayer('concept/out-2'):layer([['F','concept/out-2']]);
      const work=useWikiLinks(latest);assert.strictEqual(ui$wikiLinks,replacementPending?initial:latest);
      await nextChunk();await obsolete;
      assert.strictEqual(ui$wikiLinks,replacementPending?initial:latest);
      if(replacementPending) {
        assert.strictEqual(disclosure(),existing);await nextChunk();assert.strictEqual(ui$wikiLinks,initial);
        await nextChunk();await work;
      }
      assert.strictEqual(ui$wikiLinks,latest);assert.deepEqual(wikiNeighbors('F').after,['concept/out-2']);
      assert.deepEqual(wikiNeighbors('F').before,[]);assert.equal(scheduled.length,0);assert.equal(readerRenders,0);
    """, peers=_CHUNK_PEERS)


def test_disposal_between_chunks_keeps_the_last_committed_maps_and_reader() -> None:
    _run(r"""
      const initial=layer([['F','concept/out-1']]);useWikiLinks(initial);
      const before=snapshot(initial),oldAfter=ui$wikiAfter,oldBefore=ui$wikiBefore,existing=disclosure();
      const work=useWikiLinks(largeLayer());assert.equal(scheduled.length,1);alive=false;
      await nextChunk();await work;assert.strictEqual(ui$wikiLinks,initial);
      assert.strictEqual(ui$wikiAfter,oldAfter);assert.strictEqual(ui$wikiBefore,oldBefore);
      assert.strictEqual(disclosure(),existing);assert.equal(scheduled.length,0);assert.equal(yields,1);
      assert.equal(snapshot(initial),before);assert.equal(readerRenders,0);
    """, peers=_CHUNK_PEERS)


def test_same_article_refresh_retains_open_paged_focused_disclosure_and_new_article_resets_it() -> None:
    peers = _READER_PEERS + r"""
      const dom$readingContext=new Element('div');document.activeElement=null;function syncSheetAccessibility() {}
    """
    callbacks = _CALLBACKS + (
        "neighborIds", "relationCounts", "topicPeers", "semanticParent", "renderReaderContext", "renderInspector",
    )
    _run(r"""
      configure(27);const other=node('G','Other canonical concept');
      records.set(other.id,other);sharedIndex.nodes.push(other);contextGraph.nodes.push(other);
      const refs=[];for(let n=1;n<=27;n++)for(const id of ['F','G'])
        refs.push([id,'concept/out-'+n],['concept/in-'+n,id]);
      const state=freeze(layer(refs));useWikiLinks(state);
      const existing=disclosure(),outgoing=groups()[0],more=outgoing.querySelector('button');
      existing.open=true;emit(more,'click');assert.equal(outgoing.querySelectorAll('a').length,25);
      const focused=outgoing.querySelectorAll('a')[24];focused.focus();const before=snapshot(state);
      for(let n=0;n<3;n++) {
        renderInspector();assert.strictEqual(disclosure(),existing);assert.equal(existing.open,true);
        assert.strictEqual(groups()[0],outgoing);assert.strictEqual(outgoing.querySelector('button'),more);
        assert.equal(outgoing.querySelectorAll('a').length,25);assert.equal(more.textContent,'Show 2 more links');
        assert.strictEqual(outgoing.querySelectorAll('a')[24],focused);assert.strictEqual(document.activeElement,focused);
        assert.strictEqual(existing.parentElement,dom$readingNeighbors);assert.equal(snapshot(state),before);
        assert.strictEqual(dom$readingBody.children[0],math);assert.equal(dom$readingBody.scrollTop,137);
      }
      // The statement's own Uses / Used by lists carry its relations, so no strip precedes the retained concept links.
      assert.equal(dom$readingNeighbors.querySelector('.bp-reading-links'),null);
      assert.strictEqual(dom$readingNeighbors.children[0],existing);
      currentBundle={nodeId:'G',scope:'chapter'};renderInspector();
      assert.notStrictEqual(disclosure(),existing);assert.equal(existing.parentElement,null);assert.equal(disclosure().open,false);
      assert.equal(groups()[0].querySelectorAll('a').length,5);assert.equal(groups()[1].querySelectorAll('a').length,5);
      assert.notStrictEqual(groups()[0].querySelector('button'),more);
      assert.equal(groups()[0].querySelector('button').textContent,'Show 20 more links');
      assert.equal(ui$wikiNode,'G');assert.strictEqual(ui$wikiLinks,state);assert.equal(calls.length,0);
    """, callbacks=callbacks, peers=peers)


def test_start_caches_before_mount_forwards_connected_updates_and_disposes_detached_hosts() -> None:
    peers = _DOM + r"""
      const document={body:new Element('body')},received={wiki:[],index:[],routes:[]};
      const shell=new Element('main'),host=new Element('div');shell.className='bp-explorer-page';shell.appendChild(host);
      host.isConnected=true;var hostSequence=0,clients=new Map();let options,mounts=0,starts=0,disposed=0;
      const nav={start(){starts++;},dispose(){disposed++;}};
      const window={AutoformNavigation:{create(value){options=value;return nav;}}};
      const client={ready:new Promise(()=>{}),useWikiLinks(value){received.wiki.push(value);},
        useIndex(value){received.index.push(value);},applyRoute(value){received.routes.push(value);return this.ready;}};
      function mount(...args) {mounts++;assert.strictEqual(args[4],nav);return client;}
    """
    _run(r"""
      start(host);assert.equal(starts,1);assert.equal(mounts,0);
      const initial={availability:'complete',references:[['F','T']]},index={nodes:[]},bundle={graph:{nodes:[]}};
      options.onWikiLinks(initial);options.onIndex(index);assert.equal(received.wiki.length,0);
      assert.strictEqual(options.onCommit(bundle),client.ready);assert.equal(mounts,1);
      assert.strictEqual(received.wiki[0],initial);assert.strictEqual(received.index[0],index);
      const later={availability:'partial',references:[['U','F']]};options.onWikiLinks(later);
      assert.strictEqual(received.wiki[1],later);
      host.isConnected=false;options.onWikiLinks({availability:'unavailable'});
      assert.equal(disposed,1);assert.equal(received.wiki.length,2);
      options.onCommit(bundle);assert.equal(disposed,2);assert.equal(mounts,1);assert.equal(received.routes.length,0);
    """, callbacks=("start",), peers=peers)
