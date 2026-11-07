"""Production wiki reader callbacks in a bounded Node VM, not browser or mathematical evidence."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[2] / "autoform_cli/ui/assets/blueprint-dag.js"
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
const dom$readingBody=new Element('div');
const math=new Element('article');math.textContent='Complete authored mathematical page';dom$readingBody.appendChild(math);
dom$readingBody.scrollTop=137;
var alive=true,ui$wikiLinks=null,ui$wikiBefore=new Map(),ui$wikiAfter=new Map();
var ui$wikiGeneration=0,ui$wikiPending=null,ui$wikiWork=null,ui$wikiRendered=null,ui$wikiNode=null;
var ui$wikiRequested=false,ui$selected=null,ui$requestedNode=null,ui$mode='graph';
var ui$query='retained query';
var ui$selectedStatuses=new Set(['planned','fully_proved']),ui$relationIds=['concept/in-1'];
var navigation={},records=new Map(),sharedIndex,currentBundle={nodeId:'F',scope:'chapter'},contextGraph;
var readingReady=new Promise(()=>{});
const calls=[];let readerRenders=0;
function updateProjection() {throw new Error('Wiki reading must not change the graph projection');}
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
    mode:ui$mode,query:ui$query,
    statuses:Array.from(ui$selectedStatuses),relationIds:ui$relationIds,bundle:currentBundle,
    reading:math.textContent,scroll:dom$readingBody.scrollTop});
}
// The links end the article: a section after its text, a heading, list and button for each direction it has. A page's
// own article is headed h2, a statement's card h5.
function section() {return dom$readingBody.querySelector('.bp-reading-wiki-links');}
function headingNodes() {return section().children.filter(child=>child.tagName==='H2'||child.tagName==='H5');}
function headings() {return headingNodes().map(heading=>heading.textContent);}
function groups() {
  return headingNodes().map(heading=>{
    const parts=heading.parentElement.children,at=parts.indexOf(heading);
    return {heading,list:parts[at+1],more:parts[at+2],querySelectorAll:selector=>parts[at+1].querySelectorAll(selector),
      querySelector:selector=>selector==='button'?parts[at+2]:parts[at+1].querySelector(selector)};
  });
}
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
  return freeze(layer(Array.from({length:2049},()=>['F',target]),'partial',['F']));
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
      assert.deepEqual(wikiNeighbors('F'),{before:['concept/in-1'],after:['concept/out-1','chapter'],availability:'complete'});
      assert.deepEqual(wikiNeighbors('concept/out-1').before,['F']);
      assert.deepEqual(wikiNeighbors('concept/in-1').after,['F']);
      assert.deepEqual(wikiNeighbors('unknown'),{before:[],after:[],availability:'complete'});
      assert.equal(snapshot(state),before);assert.strictEqual(ui$wikiLinks,state);assert.equal(readerRenders,0);
      assert.equal(section().tagName,'SECTION');assert.deepEqual(headings(),['See also','Linked from']);
      assert.strictEqual(dom$readingBody.children[0],math);assert.strictEqual(dom$readingBody.children.at(-1),section());
      assert.equal(section().querySelector('.bp-reading-wiki-notice'),null);assert(!section().textContent.includes(' · '));
      const existing=section();useWikiLinks(state);assert.strictEqual(section(),existing);
    """)


@pytest.mark.parametrize("has_outgoing", [False, True])
def test_partial_source_keeps_known_backlinks_and_marks_outgoing_coverage_unknown(has_outgoing: bool) -> None:
    _run("const hasOutgoing=" + json.dumps(has_outgoing) + ";\n" + r"""
      const refs=[['concept/in-1','F']];if(hasOutgoing)refs.push(['F','concept/out-1']);
      const state=freeze({...layer(refs,'partial',['F','not-indexed']),reason:'/private/provider/parse.log'});
      const before=snapshot(state);useWikiLinks(state);
      assert.deepEqual(wikiNeighbors('F'),{before:['concept/in-1'],after:hasOutgoing?['concept/out-1']:[],availability:'partial'});
      assert.deepEqual(headings(),hasOutgoing?['See also','Linked from']:['Linked from']);
      assert.equal(section().querySelector('.bp-reading-wiki-notice').textContent,'Some pages could not be read, so these lists may miss some links.');
      assert(!markup(section()).includes(state.reason));assert(!section().textContent.includes(' · '));
      assert.equal(groups().at(-1).querySelector('a').href,records.get('concept/in-1').route);
      assert.equal(snapshot(state),before);assert.equal(readerRenders,0);
    """)


def test_a_partial_layer_with_no_known_backlink_lists_only_what_the_article_links_to_and_says_so() -> None:
    _run(r"""
      useWikiLinks(layer([['F','concept/out-1']],'partial',['concept/in-2']));
      assert.deepEqual(headings(),['See also']);assert(section().querySelector('.bp-reading-wiki-notice'));
      assert.deepEqual(wikiNeighbors('F'),{before:[],after:['concept/out-1'],availability:'partial'});
    """)


def test_an_unavailable_or_empty_layer_shows_nothing_and_no_private_error_text() -> None:
    _run(r"""
      const state=layer([['F','concept/out-1'],['concept/in-1','F']],'unavailable');
      state.reason='private-provider-wire:/private/parser/error.log';const before=snapshot(state);useWikiLinks(state);
      assert.deepEqual(wikiNeighbors('F'),{before:[],after:[],availability:'unavailable'});
      assert.equal(section(),null);assert(!markup(dom$readingBody).includes(state.reason));assert.equal(snapshot(state),before);
      useWikiLinks(layer([]));assert.equal(section(),null);assert.deepEqual(dom$readingBody.children,[math]);
      // A partial layer that has nothing for this article draws no line on its own either.
      useWikiLinks(layer([['concept/out-1','concept/in-1']],'partial',['F']));
      assert.equal(section(),null);assert.deepEqual(dom$readingBody.children,[math]);
    """)


def test_see_also_links_are_canonical_links_left_to_the_readers_own_link_handling() -> None:
    """Inside the article, the reader follows these links as it does the article's own; the explorer adds no second handler."""
    _run(r"""
      useWikiLinks(layer([['F','concept/out-1'],['concept/in-1','F']]));const before=snapshot(ui$wikiLinks);
      const outgoing=groups()[0].querySelector('a'),incoming=groups()[1].querySelector('a');
      assert.equal(outgoing.href,records.get('concept/out-1').route);assert.equal(outgoing.textContent,'Outgoing concept 1');
      assert.equal(incoming.href,records.get('concept/in-1').route);
      for(const link of [outgoing,incoming]) assert.equal(link.listeners.has('click'),false);
      assert.equal(calls.length,0);assert.equal(snapshot(ui$wikiLinks),before);
    """)


def test_show_more_is_local_to_each_direction_and_a_late_update_redraws_the_lists_under_the_same_reading() -> None:
    _run(r"""
      configure(35);const refs=[];for(let n=1;n<=35;n++)refs.push(['F','concept/out-'+n]);
      for(let n=1;n<=11;n++)refs.push(['concept/in-'+n,'F']);
      const state=freeze(layer(refs)),before=snapshot(state);useWikiLinks(state);
      const [outgoing,incoming]=groups();assert.deepEqual(headings(),['See also','Linked from']);
      assert.equal(outgoing.querySelectorAll('a').length,10);assert.equal(incoming.querySelectorAll('a').length,10);
      // Ten, then twenty more at a time.
      const more=outgoing.querySelector('button');assert.equal(more.textContent,'Show 20 more links');
      emit(more,'click');assert.equal(outgoing.querySelectorAll('a').length,30);assert.equal(more.hidden,false);
      assert.equal(more.textContent,'Show 5 more links');
      // The button stays below the new links, so focus moves to the first of them.
      assert.strictEqual(document.activeElement,outgoing.querySelectorAll('a')[10]);
      emit(more,'click');assert.equal(outgoing.querySelectorAll('a').length,35);assert.equal(more.hidden,true);
      assert.strictEqual(document.activeElement,outgoing.querySelectorAll('a')[30]);
      assert.equal(incoming.querySelectorAll('a').length,10);
      assert.equal(incoming.querySelector('button').textContent,'Show 1 more link');
      // The two buttons can say the same, so each is described by its own list's heading.
      assert.deepEqual(groups().map(group=>group.more.getAttribute('aria-describedby')),groups().map(group=>group.heading.id));
      assert.notEqual(outgoing.heading.id,incoming.heading.id);
      emit(incoming.querySelector('button'),'click');assert.equal(incoming.querySelectorAll('a').length,11);
      assert.equal(calls.length,0);assert.equal(snapshot(state),before);
      const previous=section();useWikiLinks(layer(refs,'partial',['concept/in-11']));
      assert.notStrictEqual(section(),previous);assert.equal(previous.parentElement,null);
      assert.equal(groups()[0].querySelectorAll('a').length,10);assert(section().querySelector('.bp-reading-wiki-notice'));
      assert.strictEqual(dom$readingBody.children[0],math);assert.strictEqual(dom$readingBody.children.at(-1),section());
      assert.equal(snapshot(state),before);assert.equal(readerRenders,0);
    """)


def test_a_statements_links_end_its_card_and_a_pages_own_links_end_the_page() -> None:
    """A chapter's page holds its statements' cards, each a statement's article; the page's own article is the page."""
    _run(r"""
      const source=new Element('div');source.className='bp-reading-source';source.setAttribute('data-autoform-node-id','chapter');
      dom$readingBody.appendChild(source);
      const card=id=>{const wrapper=new Element('div');wrapper.className='bp-thmwrapper';wrapper.setAttribute('data-autoform-node-id',id);
        wrapper.appendChild(new Element('details'));source.appendChild(wrapper);return wrapper;};
      const focal=card('F'),later=card('concept/out-1');
      const sections=()=>dom$readingBody.querySelectorAll('.bp-reading-wiki-links');
      const levels=section=>section.children.filter(child=>/^H[25]$/.test(child.tagName)).map(child=>child.tagName+' '+child.textContent);
      useWikiLinks(layer([['F','concept/out-1'],['concept/in-1','F'],['chapter','concept/out-2']],'partial',['F']));
      // The selected statement's lists end its own card, not the later card, under headings a level below the card's h4;
      // the page's own end the page, after its text, under h2.
      assert.equal(sections().length,2);
      const [own,page]=sections();
      assert.strictEqual(own.parentElement,focal);assert.strictEqual(focal.children.at(-1),own);
      assert.equal(later.querySelector('.bp-reading-wiki-links'),null);
      assert.deepEqual(levels(own),['H5 See also','H5 Linked from']);
      assert.strictEqual(page.parentElement,dom$readingBody);assert.strictEqual(dom$readingBody.children.at(-1),page);
      assert.deepEqual(levels(page),['H2 See also']);
      // Each partial line follows the lists it qualifies, and each heading describes only its own section's button.
      for(const section of [own,page])
        assert.strictEqual(section.children.at(-1),section.querySelector('.bp-reading-wiki-notice'));
      const described=sections().flatMap(section=>section.querySelectorAll('button').map(more=>more.getAttribute('aria-describedby')));
      assert.equal(new Set(described).size,described.length);
      // Another statement selected: its card takes its lists, and the page still ends with the page's own.
      currentBundle={nodeId:'concept/out-1',scope:'chapter'};renderWikiContext();
      assert.equal(focal.querySelector('.bp-reading-wiki-links'),null);
      assert.deepEqual(levels(later.querySelector('.bp-reading-wiki-links')),['H5 Linked from']);
      assert.deepEqual(levels(dom$readingBody.children.at(-1)),['H2 See also']);
      // The page itself: only its own lists, at its end.
      currentBundle={nodeId:'chapter',scope:'chapter'};renderWikiContext();
      assert.equal(sections().length,1);assert.strictEqual(dom$readingBody.children.at(-1),sections()[0]);
      assert.deepEqual(levels(sections()[0]),['H2 See also']);
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
      const previous=section();requestWikiLinks();requestWikiLinks();assert.equal(loads,1);assert.equal(gets,1);
      finish(state);await flush();assert.strictEqual(ui$wikiLinks,state);
      if(cached)assert.strictEqual(section(),previous);
      assert.equal(readerRenders,0);assert(readingReady instanceof Promise);
    """)


@pytest.mark.parametrize("failure", ["missing", "getter", "synchronous", "rejected"])
def test_optional_request_failure_is_guarded_and_shows_nothing(failure: str) -> None:
    _run("const failure=" + json.dumps(failure) + ";\n" + r"""
      const error=new Error('private-wire:/private/provider/error.log');
      if(failure==='getter')navigation.getWikiLinks=()=>{throw error;};
      if(failure==='synchronous')navigation.loadWikiLinks=()=>{throw error;};
      if(failure==='rejected')navigation.loadWikiLinks=()=>Promise.reject(error);
      assert.doesNotThrow(()=>requestWikiLinks());await flush();
      assert.equal(ui$wikiRequested,true);assert.equal(ui$wikiLinks.availability,'unavailable');
      assert.deepEqual(wikiNeighbors('F').before,[]);assert.deepEqual(wikiNeighbors('F').after,[]);
      assert.equal(section(),null);assert(!dom$readingBody.textContent.includes(error.message));assert.equal(readerRenders,0);
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
      useWikiLinks(state);assert.equal(ui$wikiLinks,null);assert.equal(section(),null);
      ui$wikiRequested=false;requestWikiLinks();assert.equal(ui$wikiRequested,false);assert.equal(readerRenders,0);
    """)


def test_large_projection_yields_in_bounded_chunks_and_publishes_all_maps_atomically() -> None:
    _run(r"""
      const initial=layer([['F','concept/out-1']]);useWikiLinks(initial);assert.equal(yields,0);
      const before=snapshot(initial),oldAfter=ui$wikiAfter,oldBefore=ui$wikiBefore;
      const existing=section(),state=largeLayer(),sourceBefore=JSON.stringify(state),work=useWikiLinks(state);
      assert(work instanceof Promise);assert.strictEqual(useWikiLinks(state),work);
      assert.equal(yields,1);assert.equal(scheduled.length,1);assert.strictEqual(ui$wikiLinks,initial);
      assert.strictEqual(ui$wikiAfter,oldAfter);assert.strictEqual(ui$wikiBefore,oldBefore);assert.strictEqual(section(),existing);
      await nextChunk();assert.equal(yields,2);assert.equal(scheduled.length,1);
      assert.strictEqual(ui$wikiLinks,initial);assert.strictEqual(ui$wikiAfter,oldAfter);assert.strictEqual(section(),existing);
      await nextChunk();await work;assert.equal(scheduled.length,0);assert.equal(yields,2);
      assert.strictEqual(ui$wikiLinks,state);assert.notStrictEqual(ui$wikiAfter,oldAfter);
      assert.notStrictEqual(ui$wikiBefore,oldBefore);
      assert.deepEqual(wikiNeighbors('F'),{before:[],after:['concept/out-2'],availability:'partial'});
      assert.equal(JSON.stringify(state),sourceBefore);
      assert.equal(snapshot(initial),before);assert.equal(readerRenders,0);
    """, peers=_CHUNK_PEERS)


@pytest.mark.parametrize("replacement_pending", [False, True])
def test_new_generation_interrupts_pending_work_without_publishing_obsolete_maps(replacement_pending: bool) -> None:
    _run("const replacementPending=" + json.dumps(replacement_pending) + ";\n" + r"""
      const initial=layer([['concept/in-1','F']]);useWikiLinks(initial);const existing=section();
      const obsolete=useWikiLinks(largeLayer('concept/out-1'));
      const latest=replacementPending?largeLayer('concept/out-2'):layer([['F','concept/out-2']]);
      const work=useWikiLinks(latest);assert.strictEqual(ui$wikiLinks,replacementPending?initial:latest);
      await nextChunk();await obsolete;
      assert.strictEqual(ui$wikiLinks,replacementPending?initial:latest);
      if(replacementPending) {
        assert.strictEqual(section(),existing);await nextChunk();assert.strictEqual(ui$wikiLinks,initial);
        await nextChunk();await work;
      }
      assert.strictEqual(ui$wikiLinks,latest);assert.deepEqual(wikiNeighbors('F').after,['concept/out-2']);
      assert.deepEqual(wikiNeighbors('F').before,[]);assert.equal(scheduled.length,0);assert.equal(readerRenders,0);
    """, peers=_CHUNK_PEERS)


def test_disposal_between_chunks_keeps_the_last_committed_maps_and_reader() -> None:
    _run(r"""
      const initial=layer([['F','concept/out-1']]);useWikiLinks(initial);
      const before=snapshot(initial),oldAfter=ui$wikiAfter,oldBefore=ui$wikiBefore,existing=section();
      const work=useWikiLinks(largeLayer());assert.equal(scheduled.length,1);alive=false;
      await nextChunk();await work;assert.strictEqual(ui$wikiLinks,initial);
      assert.strictEqual(ui$wikiAfter,oldAfter);assert.strictEqual(ui$wikiBefore,oldBefore);
      assert.strictEqual(section(),existing);assert.equal(scheduled.length,0);assert.equal(yields,1);
      assert.equal(snapshot(initial),before);assert.equal(readerRenders,0);
    """, peers=_CHUNK_PEERS)


def test_same_article_refresh_keeps_the_paged_focused_lists_and_a_new_article_resets_them() -> None:
    peers = _READER_PEERS + r"""
      const dom$readingContext=new Element('div');document.activeElement=null;function syncSheetAccessibility() {}
    """
    callbacks = _CALLBACKS + ("bindNodeLink", "renderReaderContext", "renderInspector")
    _run(r"""
      configure(27);const other=node('G','Other canonical concept');
      records.set(other.id,other);sharedIndex.nodes.push(other);contextGraph.nodes.push(other);
      const refs=[];for(let n=1;n<=27;n++)for(const id of ['F','G'])
        refs.push([id,'concept/out-'+n],['concept/in-'+n,id]);
      const state=freeze(layer(refs));useWikiLinks(state);
      const existing=section(),outgoing=groups()[0],more=outgoing.more;
      emit(more,'click');assert.equal(outgoing.querySelectorAll('a').length,27);
      const focused=outgoing.querySelectorAll('a')[26];focused.focus();const before=snapshot(state);
      for(let n=0;n<3;n++) {
        renderInspector();assert.strictEqual(section(),existing);
        assert.strictEqual(groups()[0].list,outgoing.list);assert.strictEqual(groups()[0].more,more);
        assert.equal(outgoing.querySelectorAll('a').length,27);assert.equal(more.hidden,true);
        assert.strictEqual(outgoing.querySelectorAll('a')[26],focused);assert.strictEqual(document.activeElement,focused);
        assert.strictEqual(existing.parentElement,dom$readingBody);assert.equal(snapshot(state),before);
        assert.strictEqual(dom$readingBody.children[0],math);assert.equal(dom$readingBody.scrollTop,137);
      }
      // No list of the topic's other pages joins them: Contents and the map list those.
      assert.deepEqual(dom$readingBody.children,[math,existing]);
      currentBundle={nodeId:'G',scope:'chapter'};renderInspector();
      assert.notStrictEqual(section(),existing);assert.equal(existing.parentElement,null);
      assert.equal(groups()[0].querySelectorAll('a').length,10);assert.equal(groups()[1].querySelectorAll('a').length,10);
      assert.notStrictEqual(groups()[0].more,more);assert.equal(groups()[0].more.textContent,'Show 17 more links');
      assert.equal(ui$wikiNode,'G');assert.strictEqual(ui$wikiLinks,state);assert.equal(calls.length,0);
    """, callbacks=callbacks, peers=peers)


def test_start_caches_before_mount_forwards_connected_updates_and_disposes_detached_hosts() -> None:
    peers = _DOM + r"""
      const document={body:new Element('body'),querySelector(){return null;}},received={wiki:[],index:[],routes:[]};
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
