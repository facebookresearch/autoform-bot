"""Actual wiki context callbacks with DOM peers, not native UI or math evidence."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated wiki context controls")

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
const names=['element','readingLabel','button','recordId','semanticParent','topicPeers','neighborIds','neighborhood',
  'relationCounts','renderReaderContext','inventoryNodes'];
const constants=['NODE_W','plural'].map(name=>{
  const start=source.indexOf('  var '+name+' =');assert(start>=0);
  const declaration=source.slice(start,source.indexOf('\n',start));new vm.Script(declaration);return declaration;
}).join('\n');
const context={assert};vm.createContext(context);
const production=constants+'\n'+names.map(original).join('\n');
"""

_PEERS = r"""
// Only the DOM and route-dispatch boundary are peers. Containment, neighbour
// selection, weighted relations and event callbacks are production code.
class Element {
  constructor(tag) {this.tagName=tag.toUpperCase();this.children=[];this.listeners=new Map();this.attrs=new Map();this.text='';}
  get textContent() {return this.text+this.children.map(child=>child.textContent).join('');}
  set textContent(value) {this.text=String(value);this.children=[];}
  setAttribute(name,value) {this.attrs.set(name,String(value));}
  getAttribute(name) {return this.attrs.has(name)?this.attrs.get(name):null;}
  appendChild(child) {this.children.push(child);return child;}
  replaceChildren(...children) {this.children=children;this.text='';}
  addEventListener(type,callback) {this.listeners.set(type,callback);}
  all() {return this.children.flatMap(child=>[child,...child.all()]);}
}
const document={createElement:tag=>new Element(tag)};
const dom$readingContext=new Element('div'),dom$readingNeighbors=new Element('div'),calls=[];
const navigation={navigateNode(id,options){calls.push({id,options});}};
var records,sharedIndex,contextGraph,currentBundle,ui$selected=null,ui$requestedNode=null;
var ui$query='',ui$relationIds=null,ui$mode='list',ui$selectedStatuses=new Set(['planned','fully_proved','inventory_pending']);
function record(id,scope,extra={}) {
  return Object.assign({id,scope,parent:id===scope?'roadmap':scope,kind:id===scope?'scope':'node',title:id,
    result:id!==scope,status:'planned',route:'https://example.test/roadmap/'+encodeURIComponent(id)+'/index.html'},extra);
}
const root=record('roadmap','roadmap',{parent:null,title:'Library'});
const owner=record('chapter','chapter',{title:'Authored topic'}),focus=record('F','chapter',{title:'Focal result'});
function configure(nodes,relations=[],id='F') {
  const pool=[root,owner,...nodes];records=new Map(pool.map(node=>[node.id,node]));
  sharedIndex={nodes:pool,relations};currentBundle={nodeId:id,scope:records.get(id)?.scope};
  contextGraph={nodes:nodes.slice(),edges:relations.map(row=>({source:row[0],target:row[1],statement:row[2],proof:row[3]})),
    regions:[],view:{presentation:'dag'}};calls.length=0;
}
function marked(root,name) {return root.all().find(node=>(node.className||'').split(/\s+/).includes(name));}
function links(root) {return root.all().filter(node=>node.tagName==='A');}
function click(link,options={}) {
  const event=Object.assign({button:0,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}},options);
  const callback=link.listeners.get('click');assert(callback,'A canonical link must have its actual click callback');
  callback(event);return event;
}
function snapshot() {return JSON.stringify({nodes:sharedIndex.nodes,relations:sharedIndex.relations,contextGraph});}
"""

_CONTROL_PEERS = r"""
// Shell elements, observers and the mode-dispatch boundary are peers. The
// constructed toolbar, Browse events and control synchronization are production.
class Element {
  constructor(tag) {
    this.tagName=tag.toUpperCase();this.children=[];this.listeners=new Map();this.attrs=new Map();
    this.dataset={};this.hidden=false;this.text='';this.focuses=[];
    this.classList={toggle(){}};this.style={setProperty(){}};
  }
  get textContent() {return this.text+this.children.map(child=>child.textContent).join('');}
  set textContent(value) {this.text=String(value);this.children=[];}
  setAttribute(name,value) {this.attrs.set(name,String(value));}
  getAttribute(name) {return this.attrs.has(name)?this.attrs.get(name):null;}
  appendChild(child) {child.parentElement=this;this.children.push(child);return child;}
  insertBefore(child,before) {child.parentElement=this;this.children.splice(this.children.indexOf(before),0,child);}
  replaceChildren(...children) {this.children=[];this.text='';children.forEach(child=>this.appendChild(child));}
  contains(node) {return node===this||this.children.some(child=>child.contains(node));}
  addEventListener(type,callback) {
    const callbacks=this.listeners.get(type)||[];callbacks.push(callback);this.listeners.set(type,callbacks);
  }
  all() {return this.children.flatMap(child=>[child,...child.all()]);}
  querySelector(tag) {return this.all().find(child=>child.tagName===tag.toUpperCase())||null;}
  getContext() {return {font:''};}
  focus(options) {this.focuses.push(options);document.activeElement=this;}
}
const document=new Element('document');document.body=new Element('body');document.activeElement=document.body;
document.createElement=tag=>new Element(tag);
document.createTextNode=text=>{const node=new Element('text');node.textContent=text;return node;};
const window=new Element('window'),host=new Element('div'),records=new Map(),modes=[],observers=[];
const navigation={bindReader(){}};
var currentBundle={nodeId:'chapter',scope:'chapter',title:'Authored topic',url:'https://example.test/chapter/'};
var instance=1,trail=[],view={},family,data={title:'Authored topic',nodes:[],edges:[]};
var mapAllowed=true,ui$mapAllowed=true,ui$mode='graph',ui$selectedStatuses=new Set(['planned']),ui$showIsolated=true;
var presentStatuses=['planned'],ui$relationIds=new Set(['F']),ui$overviewGroup=null,ui$area=null;
var ui$wikiView=null,sceneData=null,scene=null;
function getComputedStyle() {return {fontFamily:'sans-serif'};}
function globalListen(target,type,callback) {target.addEventListener(type,callback);}
class MutationObserver {observe(){}}
function trailList() {return new Element('ol');}
function renderStatusFields(){}function renderNavigation(){}function calculateActive(){}
function compactRegions(){return false;}function syncNavigationSelection(){}
function ensureGlobalSearch(){}function fit(){}function revealAll(){}function openResults(){}
function pointerDown(){}function pointerMove(){}function pointerUp(){}function resize(){}
function syncSheetAccessibility(){}function schedule(){}function remember(){}
function closeSearch(){}function setFilters(){}
function setMode(mode,push) {
  assert.equal(dom$browsePopover.hidden,true,'Browse choices must close before mode dispatch');
  modes.push({mode,push});ui$mode=mode;syncControls();
}
function emit(target,type,settings={}) {
  const event=Object.assign({target,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}},settings);
  (target.listeners.get(type)||[]).forEach(callback=>callback(event));return event;
}
buildShell();bindEvents();syncControls();
"""


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    script += "vm.runInContext(production+" + json.dumps(_PEERS + "\n" + body) + ",context,{timeout:2000});\n"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def _run_controls(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    script += "const controls=['element','button','safeHref','buildShell','bindEvents','setBrowse','syncControls','currentOverviewGroup'].map(original).join('\\n');\n"
    script += "const start=source.indexOf('    var dom$announcement,');assert(start>=0);\n"
    script += "const domDeclarations=source.slice(start,source.indexOf(';',start)+1);\n"
    program = _CONTROL_PEERS + "\n" + body
    script += "vm.runInContext(constants+'\\n'+domDeclarations+'\\n'+controls+" + json.dumps(program)
    script += ",context,{timeout:2000});\n"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def test_toolbar_keeps_browse_choices_nested_and_back_visible_only_outside_the_graph() -> None:
    _run_controls(r"""
      const toolbar=host.all().find(node=>node.getAttribute('aria-label')==='Dependency explorer controls');
      assert(toolbar);assert.equal(dom$browsePopover.hidden,true);
      assert.equal(dom$browseToggle.getAttribute('aria-expanded'),'false');
      assert.equal(dom$browseToggle.getAttribute('aria-controls'),dom$browsePopover.id);
      assert.equal(dom$browsePopover.getAttribute('aria-label'),'Browse library contents');
      assert.strictEqual(dom$listMode.parentElement,dom$browsePopover);
      assert.strictEqual(dom$sourcesMode.parentElement,dom$browsePopover);
      assert.equal(dom$sourcesMode.textContent,'Source texts');
      assert.equal(dom$listMode.textContent,'Statements in this topic');
      assert.equal(dom$regionsMode,undefined);
      assert(!toolbar.all().some(node=>node.tagName==='BUTTON'&&['Map','Results','Sources','Regions'].includes(node.textContent)));
      assert.equal(dom$graphMode.textContent,'Back to map');assert.equal(dom$graphMode.hidden,true);
      assert.equal(dom$stage.hidden,false);assert.equal(dom$inventory.hidden,true);
      currentBundle.scope='roadmap';ui$mode='list';ui$mapAllowed=false;syncControls();
      assert.equal(dom$listMode.textContent,'All statements');assert.equal(dom$graphMode.hidden,false);
      assert.equal(dom$graphMode.disabled,true);assert.equal(dom$stage.hidden,true);assert.equal(dom$inventory.hidden,false);
      ui$mapAllowed=true;ui$mode='sources';syncControls();
      assert.equal(dom$graphMode.disabled,false);assert.equal(dom$graphMode.hidden,false);
      assert.equal(dom$inventory.querySelector('h2').textContent,'Source tracks');
      ui$mode='graph';syncControls();assert.equal(dom$graphMode.hidden,true);
    """)


def test_browse_toggle_and_escape_close_with_focus_return_from_the_actual_menu() -> None:
    _run_controls(r"""
      dom$browseToggle.focus({});emit(dom$browseToggle,'click');
      assert.equal(dom$browsePopover.hidden,false);assert.equal(dom$browseToggle.getAttribute('aria-expanded'),'true');
      assert.equal(document.activeElement,dom$browseToggle,'Opening Browse must not steal focus');
      dom$listMode.focus({});const escaped=emit(dom$browse,'keydown',{target:dom$listMode,key:'Escape'});
      assert.equal(escaped.defaultPrevented,true);assert.equal(dom$browsePopover.hidden,true);
      assert.equal(dom$browseToggle.getAttribute('aria-expanded'),'false');assert.equal(document.activeElement,dom$browseToggle);
      assert.deepEqual(dom$browseToggle.focuses.at(-1),{preventScroll:true});
      const before=dom$browseToggle.focuses.length;
      assert.equal(emit(dom$browse,'keydown',{key:'Escape'}).defaultPrevented,false);
      assert.equal(dom$browseToggle.focuses.length,before);
      emit(dom$browseToggle,'click');assert.equal(dom$browsePopover.hidden,false);
      emit(dom$browseToggle,'click');assert.equal(dom$browsePopover.hidden,true);
    """)


def test_browse_choices_close_before_native_mode_dispatch_and_clear_statement_relations() -> None:
    _run_controls(r"""
      emit(dom$browseToggle,'click');dom$listMode.focus({});emit(dom$listMode,'click');
      assert.equal(dom$browsePopover.hidden,true);assert.equal(document.activeElement,dom$browseToggle);
      assert.equal(ui$relationIds,null);assert.deepEqual(modes,[{mode:'list',push:true}]);
      assert.equal(dom$graphMode.hidden,false);
      emit(dom$browseToggle,'click');dom$sourcesMode.focus({});emit(dom$sourcesMode,'click');
      assert.equal(dom$browsePopover.hidden,true);assert.equal(document.activeElement,dom$browseToggle);
      assert.deepEqual(modes.at(-1),{mode:'sources',push:true});
      emit(dom$graphMode,'click');assert.deepEqual(modes.at(-1),{mode:'graph',push:true});
      assert.equal(dom$graphMode.hidden,true);
    """)


def test_passive_browse_closes_preserve_the_focus_destination() -> None:
    _run_controls(r"""
      const outside=new Element('button');
      emit(dom$browseToggle,'click');dom$listMode.focus({});
      const before=dom$browseToggle.focuses.length;
      emit(document,'pointerdown',{target:outside});
      assert.equal(dom$browsePopover.hidden,true);assert.equal(dom$browseToggle.getAttribute('aria-expanded'),'false');
      assert.equal(document.activeElement,dom$listMode,'Outside pointerdown precedes the native focus destination');
      assert.equal(dom$browseToggle.focuses.length,before);
      emit(dom$browseToggle,'click');dom$listMode.focus({});
      emit(dom$browse,'focusout',{target:dom$listMode,relatedTarget:dom$sourcesMode});
      assert.equal(dom$browsePopover.hidden,false,'Moving between Browse choices must keep the disclosure open');
      outside.focus({});emit(dom$browse,'focusout',{target:dom$listMode,relatedTarget:outside});
      assert.equal(dom$browsePopover.hidden,true);assert.equal(document.activeElement,outside);
      assert.equal(dom$browseToggle.focuses.length,before);
    """)


def test_leaf_peers_use_exact_scope_and_keep_catalog_status_without_source_or_note_leakage() -> None:
    _run(r"""
      const first=record('A','chapter',{status:'fully_proved'});
      const module=record('M','chapter',{result:false,catalog:'module',status:'inventory_pending'});
      const excluded=[record('note','chapter',{result:false}),record('source','chapter',{navigation:'source',catalog:'module'}),
        record('collection','chapter',{navigation:'collection'}),record('pagination','chapter',{navigation:'transparent'}),
        record('elsewhere','another',{parent:'chapter'})];
      configure([focus,first,module,...excluded]);const before=snapshot(),peers=topicPeers('F');
      assert.strictEqual(peers.owner,owner);assert.equal(peers.title,'More in '+owner.title);
      assert.deepEqual(peers.nodes.map(node=>node.id),['A','M']);
      assert.strictEqual(peers.nodes[0],first);assert.strictEqual(peers.nodes[1],module);
      assert.equal(peers.nodes[0].status,'fully_proved');assert.equal(peers.nodes[1].status,'inventory_pending');
      assert.equal(snapshot(),before,'Structural navigation mutated source nodes or typed relations');
    """)


def test_container_peers_follow_semantic_parents_through_collection_and_pagination() -> None:
    _run(r"""
      const collection=record('collection','collection',{parent:'chapter',navigation:'collection'});
      const pagination=record('pagination','pagination',{parent:'collection',navigation:'transparent'});
      const selected=record('scope:topic','scope:topic',{parent:'pagination'});
      const sibling=record('different/path','different/path',{parent:'collection'});
      const direct=record('direct','direct',{parent:'chapter'});
      const source=record('source','source',{parent:'chapter',navigation:'source'});
      const unrelated=record('scope:topic/similar-path','scope:topic/similar-path',{parent:'roadmap'});
      const resultScope=record('result-scope','result-scope',{parent:'chapter',result:true});
      configure([collection,pagination,selected,sibling,direct,source,unrelated,resultScope,focus],[],selected.id);
      const before=snapshot(),peers=topicPeers(selected.id);
      assert.strictEqual(peers.owner,owner);assert.equal(peers.title,'Other topics in '+owner.title);
      assert.deepEqual(peers.nodes.map(node=>node.id),[sibling.id,direct.id]);
      assert.equal(snapshot(),before,'Containment must not be inferred from paths or rewritten');
    """)


def test_missing_ownership_and_root_topic_sidebar_duplication_produce_no_peer_disclosure() -> None:
    _run(r"""
      configure([focus]);assert.equal(topicPeers('missing'),null);
      assert.equal(topicPeers('roadmap'),null);assert.equal(topicPeers('chapter'),null);
      records.delete('chapter');assert.equal(topicPeers('F'),null);
      configure([focus]);sharedIndex=null;assert.equal(topicPeers('F'),null);
      configure([focus]);renderReaderContext();
      assert.equal(marked(dom$readingNeighbors,'bp-reading-related-topics'),undefined);
      assert.equal(dom$readingNeighbors.children.length,0);
    """)


def test_structural_peers_never_pad_the_focal_graph_or_acquire_dependency_edges() -> None:
    _run(r"""
      const a=record('A','chapter'),b=record('B','chapter');configure([focus,a,b],[['A','B',2,3]]);
      const before=snapshot(),peers=topicPeers('F'),projected=neighborhood('F');
      assert.deepEqual(peers.nodes.map(node=>node.id),['A','B']);
      assert.deepEqual(projected.nodes.map(node=>node.id),['F']);assert.deepEqual(projected.edges,[]);
      assert.equal(projected.nodes[0].isolated,true);assert.deepEqual(neighborIds('F'),{before:[],after:[]});
      const connected=neighborhood('A');assert.deepEqual(connected.nodes.map(node=>node.id).sort(),['A','B']);
      assert.deepEqual(connected.edges,[{source:'A',target:'B',statement:2,proof:3}]);
      assert.equal(snapshot(),before,'Showing peer navigation changed the authoritative graph');
    """)


def test_reader_keeps_exact_weighted_relations_separate_from_closed_structural_details() -> None:
    _run(r"""
      const a=record('A','chapter'),b=record('B','chapter');configure([focus,a,b],[['A','F',2,3],['F','B',0,4]]);
      const before=snapshot();renderReaderContext();
      const relation=marked(dom$readingNeighbors,'bp-reading-links'),details=marked(dom$readingNeighbors,'bp-reading-related-topics');
      assert(relation);assert(details);assert.equal(details.tagName,'DETAILS');assert.equal(details.getAttribute('open'),null);
      assert.equal(details.children[0].tagName,'SUMMARY');assert.equal(details.children[0].textContent,'More in '+owner.title);
      assert(relation.textContent.includes('Prerequisites · 1'));assert(relation.textContent.includes('Used by · 1'));
      const [incoming,outgoing]=links(relation);
      assert.equal(incoming.href,a.route);assert.equal(outgoing.href,b.route);
      assert.equal(incoming.getAttribute('aria-label'),'A · 2 statement dependencies, 3 proof dependencies');
      assert.equal(outgoing.getAttribute('aria-label'),'B · 4 proof dependencies');
      assert.deepEqual(links(details).map(link=>link.href),[a.route,b.route]);
      assert(links(details).every(link=>link.getAttribute('aria-label')===null),'Structural links cannot claim dependency weights');
      assert.equal(snapshot(),before);
    """)


def test_peer_links_and_browse_owner_preserve_modifiers_and_dispatch_canonical_reading_intent() -> None:
    _run(r"""
      const peers=Array.from({length:8},(_,number)=>record('P'+number,'chapter'));configure([focus,...peers]);
      const before=snapshot();renderReaderContext();
      const details=marked(dom$readingNeighbors,'bp-reading-related-topics'),list=marked(details,'bp-reading-topic-list');
      const visible=links(list),browse=marked(details,'bp-reading-topic-browse');
      assert.equal(visible.length,5);assert.equal(list.children.length,5);assert(browse);
      assert.equal(browse.textContent,'Browse '+owner.title);assert.equal(browse.href,owner.route);
      assert.equal(topicPeers('F').nodes.length,8,'The five-link preview must not truncate the peer pool');
      for(const [link,id] of [[visible[0],peers[0].id],[browse,owner.id]]) {
        const count=calls.length;
        for(const settings of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{button:2},{defaultPrevented:true}]) {
          const event=click(link,settings);assert.equal(event.defaultPrevented,Boolean(settings.defaultPrevented));
          assert.equal(calls.length,count,'Modified/default-prevented clicks must remain native');
        }
        assert.equal(click(link).defaultPrevented,true);
        assert.deepEqual(calls.at(-1),{id,options:{history:'push',focus:'reading'}});
      }
      currentBundle={nodeId:owner.id,scope:owner.id};
      assert.deepEqual(inventoryNodes().map(node=>node.id),[focus.id,...peers.map(node=>node.id)],
        'Browsing the real owner must retain every peer in its complete inventory');
      assert.equal(snapshot(),before);
    """)


def test_container_peer_disclosure_has_canonical_scope_links_not_fake_results() -> None:
    _run(r"""
      const selected=record('topic','topic',{parent:'chapter'}),peer=record('other','other',{parent:'chapter'});
      configure([selected,peer],[],selected.id);renderReaderContext();
      const details=marked(dom$readingNeighbors,'bp-reading-related-topics');assert(details);
      assert.equal(details.children[0].textContent,'Other topics in '+owner.title);
      const link=links(details)[0];assert.equal(link.href,peer.route);assert.equal(link.textContent,peer.title);
      assert.equal(click(link).defaultPrevented,true);
      assert.deepEqual(calls.at(-1),{id:peer.id,options:{history:'push',focus:'reading'}});
      assert.deepEqual(sharedIndex.relations,[]);assert.equal(peer.result,false);
    """)
