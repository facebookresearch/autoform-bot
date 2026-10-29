"""Actual wiki context callbacks with DOM peers, not native UI or math evidence."""

from __future__ import annotations

import copy
import dataclasses
import json
import shutil
import subprocess
from pathlib import Path

import html5lib
import pytest

from autoform_cli import render, status
from autoform_cli.graph import Graph, Node, load_graph

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
const names=['element','readingLabel','button','bindNodeLink','recordId','semanticParent','topicPeers','neighborIds','neighborhood',
  'relationCounts','renderReaderContext','inventoryNodes','sourceNodes','inCurrentScope'];
const constants=['NODE_W','plural','num','pressed'].map(name=>{
  const start=source.indexOf('  var '+name+' =');assert(start>=0);
  const declaration=source.slice(start,source.indexOf('\n',start));new vm.Script(declaration);return declaration;
}).join('\n');
const context={assert,URL};vm.createContext(context);
const production=constants+'\n'+names.map(original).join('\n');
"""

_PEERS = r"""
// Only the DOM and route-dispatch boundary are peers. Containment, neighbour
// selection, weighted relations and event callbacks are production code.
class Element {
  constructor(tag) {this.tagName=tag.toUpperCase();this.children=[];this.listeners=new Map();this.attrs=new Map();this.text='';}
  get textContent() {return this.text+this.children.map(child=>child.textContent).join('');}
  get firstChild() {return this.children[0]||null;}
  set textContent(value) {this.text=String(value);this.children=[];}
  setAttribute(name,value) {this.attrs.set(name,String(value));}
  getAttribute(name) {return this.attrs.has(name)?this.attrs.get(name):null;}
  removeAttribute(name) {this.attrs.delete(name);}
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
function syncSheetAccessibility() {}
"""

_CONTROL_PEERS = r"""
// Shell elements, observers and the mode-dispatch boundary are peers. The
// constructed toolbar, Browse events and control synchronization are production.
class Element {
  constructor(tag) {
    this.tagName=tag.toUpperCase();this.children=[];this.listeners=new Map();this.attrs=new Map();
    this.dataset={};this.hidden=false;this.text='';this.focuses=[];
    this.classes=new Set();
    this.classList={contains:name=>this.classes.has(name),
      toggle:(name,enabled)=>{if(enabled)this.classes.add(name);else this.classes.delete(name);}};
    this.style={setProperty(name,value){this[name]=String(value);}};
  }
  get textContent() {return this.text+this.children.map(child=>child.textContent).join('');}
  get firstChild() {return this.children[0]||null;}
  set textContent(value) {this.text=String(value);this.children=[];}
  setAttribute(name,value) {this.attrs.set(name,String(value));}
  getAttribute(name) {return this.attrs.has(name)?this.attrs.get(name):null;}
  removeAttribute(name) {this.attrs.delete(name);}
  appendChild(child) {if(child.parentElement)child.remove();child.parentElement=this;this.children.push(child);return child;}
  remove() {
    const parent=this.parentElement;if(!parent)return;
    parent.children=parent.children.filter(child=>child!==this);this.parentElement=null;
  }
  insertBefore(child,before) {child.parentElement=this;this.children.splice(this.children.indexOf(before),0,child);}
  // As the DOM does, a string argument becomes a text node.
  replaceChildren(...children) {this.children.forEach(child=>child.parentElement=null);this.children=[];this.text='';this.append(...children);}
  append(...children) {children.forEach(child=>this.appendChild(typeof child==='string'?document.createTextNode(child):child));}
  contains(node) {return node===this||this.children.some(child=>child.contains(node));}
  addEventListener(type,callback) {
    const callbacks=this.listeners.get(type)||[];callbacks.push(callback);this.listeners.set(type,callbacks);
  }
  all() {return this.children.flatMap(child=>[child,...child.all()]);}
  querySelectorAll(selector) {
    return this.all().filter(child=>{
      const tag=selector.match(/^[a-z][a-z0-9-]*/i),kind=selector.match(/^\.([\w-]+)/);
      if(tag&&child.tagName!==tag[0].toUpperCase())return false;
      if(kind&&!(child.className||'').split(/\s+/).includes(kind[1]))return false;
      return Array.from(selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)).every(([,key,expected])=>{
        let value=child.getAttribute(key);
        if(key.startsWith('data-'))value=child.dataset[key.slice(5).replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase())];
        if(key==='tabindex'&&value===null&&child.tabIndex!==undefined)value=String(child.tabIndex);
        return value!==null&&value!==undefined&&(expected===undefined||String(value)===expected);
      });
    });
  }
  querySelector(selector) {return this.querySelectorAll(selector)[0]||null;}
  getContext() {return {font:''};}
  focus(options) {this.focuses.push(options);document.activeElement=this;}
}
const document=new Element('document');document.body=new Element('body');document.activeElement=document.body;
document.createElement=tag=>new Element(tag);
document.createTextNode=text=>{const node=new Element('text');node.textContent=text;return node;};
const window=new Element('window'),host=new Element('div'),records=new Map(),modes=[],observers=[];
window.location={href:'https://example.test/chapter/',origin:'https://example.test'};
const source=Object.assign({id:'chapter/source',scope:'chapter/source',parent:'chapter',kind:'scope',result:false,
  navigation:'source',title:'Authored source',status:'planned',route:'https://example.test/chapter/source/'});
var sharedIndex={nodes:[
  {id:'roadmap',scope:'roadmap',parent:null,kind:'scope',result:false,title:'Authored library',route:'https://example.test/roadmap/'},
  {id:'chapter',scope:'chapter',parent:'roadmap',kind:'scope',result:false,title:'Authored topic',route:'https://example.test/chapter/'},
  source],relations:[]};
sharedIndex.nodes.forEach(node=>records.set(node.id,node));
var homeURL='https://example.test/index.html',navigationCalls=[];
const navigation={bindReader(){},getHomeURL(){return homeURL;},
  navigate(url,options){navigationCalls.push({url,options});}};
var currentBundle={nodeId:'chapter',scope:'chapter',title:'Authored topic',url:'https://example.test/chapter/',graph:{view:{}}};
var instance=1,trail=[],view={},family,data={title:'Authored topic',nodes:[],edges:[]};
var mapAllowed=true,ui$mapAllowed=true,ui$mode='graph',ui$selectedStatuses=new Set(['planned']),ui$showIsolated=true;
var presentStatuses=['planned'],ui$relationIds=new Set(['F']),ui$overviewGroup=null,ui$area=null;
var ui$wikiView=null,ui$mathJob=null,sceneData=null,scene=null;
function getComputedStyle() {return {fontFamily:'sans-serif'};}
function globalListen(target,type,callback) {target.addEventListener(type,callback);}
class MutationObserver {observe(){}}
function renderStatusFields(){}function renderNavigation(){}function calculateActive(){}
function compactRegions(){return false;}function syncNavigationSelection(){}
function ensureGlobalSearch(){}function fit(){}function revealAll(){}function openResults(){}function focusNodeControl(){}
function pointerDown(){}function pointerMove(){}function pointerUp(){}function resize(){}
function syncSheetAccessibility(){}function schedule(){}function remember(){}function sheetLayout(){return false;}
function closeSearch(){}function setFilters(){}
// Scene/shelf rendering is outside these toolbar-control assertions.
function geometry(){return {};}function renderShelves(){}
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
    script += "const controls=['element','button','readingLabel','bindNodeLink','safeHref','buildShell','bindEvents','setBrowse','syncControls','wrapMapKey','currentOverviewGroup',"
    script += "'trailList','renderBreadcrumb','routeTrail','semanticParent','refreshReadingLabels','sourceNodes','inCurrentScope'].map(original).join('\\n');\n"
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
      assert.equal(typeof dom$regionsMode,'undefined');
      assert(!toolbar.all().some(node=>node.tagName==='BUTTON'&&['Map','Results','Sources','Regions','Areas'].includes(node.textContent)));
      assert.equal(dom$graphMode.textContent,'Back to map');assert.equal(dom$graphMode.hidden,true);
      // Browse marks only the list on screen, so on the map no control reads as pressed and in a list exactly one does.
      const pressedNow=()=>toolbar.all().filter(node=>node.getAttribute('aria-pressed')==='true');
      assert.deepEqual(pressedNow(),[]);
      assert.equal(dom$stage.hidden,false);assert.equal(dom$inventory.hidden,true);
      currentBundle.scope='roadmap';ui$mode='list';ui$mapAllowed=false;syncControls();
      assert.equal(dom$listMode.textContent,'All statements');assert.equal(dom$graphMode.hidden,false);
      assert.deepEqual(pressedNow(),[dom$listMode]);
      assert.equal(dom$graphMode.disabled,true);assert.equal(dom$stage.hidden,true);assert.equal(dom$inventory.hidden,false);
      ui$mapAllowed=true;ui$mode='sources';syncControls();
      assert.equal(dom$graphMode.disabled,false);assert.equal(dom$graphMode.hidden,false);
      assert.equal(dom$inventory.querySelector('h2').textContent,'Source texts');assert.deepEqual(pressedNow(),[dom$sourcesMode]);
      ui$mode='graph';syncControls();assert.equal(dom$graphMode.hidden,true);assert.deepEqual(pressedNow(),[]);
    """)



def test_a_collection_page_ends_its_own_trail_and_never_stands_between_crumbs() -> None:
    _run_controls(r"""
      const add=(id,parent,extra={})=>records.set(id,Object.assign({id,scope:id,parent,title:id.toUpperCase(),
        route:'https://example.test/'+id+'/index.html'},extra));
      add('roadmap',null);add('area','roadmap');add('shelf','area',{navigation:'collection'});add('topic','shelf');
      add('shelf/thm','shelf',{scope:'shelf',result:true});
      const crumbs=(nodeId,scope)=>routeTrail({nodeId,scope,graph:{view:{}}}).map(entry=>[entry.label,entry.url||null]);
      // Dynamics/MathlibExt's trail ended at Dynamics while its header said MathlibExt. Every trail starts at the site's home.
      const home=['Home','https://example.test/index.html'];
      assert.deepEqual(crumbs('shelf','shelf'),[home,['ROADMAP','https://example.test/roadmap/index.html'],
        ['AREA','https://example.test/area/index.html'],['SHELF',null]]);
      assert.deepEqual(crumbs('shelf/thm','shelf'),[home,['ROADMAP','https://example.test/roadmap/index.html'],
        ['AREA','https://example.test/area/index.html'],['SHELF','https://example.test/shelf/index.html'],['SHELF/THM',null]]);
      assert.deepEqual(crumbs('topic','topic').map(entry=>entry[0]),['Home','ROADMAP','AREA','TOPIC'],'A topic inside a collection skips it');
    """)


def test_the_map_key_names_concept_links_once_and_only_when_the_map_draws_them() -> None:
    _run_controls(r"""
      var byId=new Map();function wikiOwnerVisible(){return true;}
      const key=()=>dom$mapKey.children.map(node=>node.textContent);
      // "Partial link coverage" printed on maps with no concept links, where it read as missing dependencies.
      ui$wikiView={availability:'partial'};syncControls();assert.deepEqual(key(),[]);
      data.$wikiEdges=[{source:'a',target:'b'}];syncControls();assert.deepEqual(key(),['Concept links, some pages unread']);
      // "Local concept links · All links in articles" read as two kinds of line; one entry now says how many this map draws.
      sceneData=data;scene={$wikiRoutes:{omitted:28,total:40}};ui$wikiView={availability:'complete'};syncControls();
      assert.deepEqual(key(),['Concept links: 12 of 40 on this map']);
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


def test_source_pool_uses_authored_descendants_not_scope_defaults_or_path_prefixes() -> None:
    _run(r"""
      const collection=record('collection','collection',{parent:'chapter',navigation:'collection'});
      const transparent=record('pagination','pagination',{parent:'collection',navigation:'transparent'});
      const direct=record('direct-source','direct-source',{parent:'chapter',navigation:'source',result:false});
      const nested=record('nested-source','nested-source',{parent:'pagination',navigation:'source',result:false});
      const other=record('other','other',{parent:'roadmap'});
      const elsewhere=record('chapter/source-looking-path','chapter',{parent:'other',navigation:'source',result:false});
      const unknown=record('unknown-source','chapter',{parent:'missing',navigation:'source',result:false});
      const cycle=record('cycle-source','cycle-source',{parent:'loop',navigation:'source',result:false});
      const loop=record('loop','loop',{parent:cycle.id});
      configure([focus,collection,elsewhere,transparent,direct,other,nested,unknown,cycle,loop],[],owner.id);
      const before=snapshot();
      const localSources=sourceNodes();assert.deepEqual(localSources.map(node=>node.id),[direct.id,nested.id]);
      assert.strictEqual(sourceNodes(),localSources,'The same published index/scope must reuse its bounded source selection');
      ui$mode='sources';assert.deepEqual(inventoryNodes().map(node=>node.id),[direct.id,nested.id]);
      ui$query='absent query';ui$selectedStatuses.clear();
      assert.strictEqual(sourceNodes(),localSources);
      assert.deepEqual(sourceNodes().map(node=>node.id),[direct.id,nested.id],
        'Source availability is authored content, not a search or status-filter result');
      currentBundle={nodeId:'roadmap',scope:'roadmap'};
      assert.deepEqual(sourceNodes().map(node=>node.id),[elsewhere.id,direct.id,nested.id,unknown.id,cycle.id]);
      assert.equal(snapshot(),before,'Selecting the source pool must not mutate index nodes or typed dependencies');
    """)


def test_deep_source_ancestry_is_memoized_with_linear_record_lookup_work() -> None:
    _run(r"""
      const chain=Array.from({length:1000},(_,number)=>record('S'+number,'S'+number,
        {parent:number?'S'+(number-1):owner.id,navigation:'source',result:false}));
      const leaves=Array.from({length:16},(_,number)=>record('L'+number,owner.id,{parent:chain.at(-1).id}));
      configure([...chain].reverse().concat(leaves),[],owner.id);
      const before=snapshot();let gets=0;
      records.get=function(id){gets++;return Map.prototype.get.call(this,id);};
      const selected=sourceNodes();
      assert.deepEqual(selected.map(node=>node.id),[...chain].reverse().map(node=>node.id));
      assert(gets<=chain.length+8,'A deep-first source chain must not repeat its full ancestry for every descendant');
      const afterSources=gets;assert.strictEqual(sourceNodes(),selected);assert.equal(gets,afterSources);
      ui$mode='list';assert.deepEqual(inventoryNodes().map(node=>node.id),leaves.map(node=>node.id));
      assert(gets<=chain.length+leaves.length+12,'Inventory and source selection must share the validated ancestry cache');
      const afterInventory=gets;inventoryNodes();assert.equal(gets,afterInventory);
      ui$query='irrelevant';ui$selectedStatuses.clear();
      assert.strictEqual(sourceNodes(),selected);assert.equal(gets,afterInventory);
      assert.equal(snapshot(),before,'Memoized ancestry must not annotate or rewrite authored index records');
    """)


@pytest.mark.parametrize("failure", ["missing-parent", "cyclic-parent"])
def test_nonreaching_local_parent_chain_cannot_claim_source_or_result_membership(failure: str) -> None:
    _run("const failure=" + json.dumps(failure) + ";\n" + r"""
      const branch=record('branch','branch',{parent:failure==='cyclic-parent'?'loop':'absent-parent'});
      const source=record('source','source',{parent:branch.id,navigation:'source',result:false});
      const result=record('result',owner.id,{parent:branch.id}),loop=record('loop','loop',{parent:branch.id});
      configure([branch,source,result,loop],[],owner.id);
      const before=snapshot();
      assert.equal(inCurrentScope(source),false);assert.equal(inCurrentScope(result),false);
      assert.deepEqual(sourceNodes(),[]);ui$mode='list';assert.deepEqual(inventoryNodes(),[]);
      assert.equal(snapshot(),before,'A local chain which never reaches the selected scope cannot invent membership');
    """)


@pytest.mark.parametrize("legacy", ["upstream-dangling", "declared-scope-not-indexed"])
def test_declared_local_scope_membership_preserves_legacy_unknown_upstream_ownership(legacy: str) -> None:
    _run("const legacy=" + json.dumps(legacy) + ";\n" + r"""
      const scope=legacy==='declared-scope-not-indexed'?'legacy-scope':owner.id;
      const source=record('source','source',{parent:scope,navigation:'source',result:false});
      const theorem=record('theorem',scope,{parent:scope});configure([source,theorem],[],owner.id);
      currentBundle.scope=scope;if(legacy==='upstream-dangling') owner.parent='unknown-upstream';
      const before=snapshot();
      assert.equal(inCurrentScope(source),true);assert.equal(inCurrentScope(theorem),true);
      assert.deepEqual(sourceNodes().map(node=>node.id),[source.id]);
      ui$mode='list';assert.deepEqual(inventoryNodes().map(node=>node.id),[theorem.id]);
      assert.equal(snapshot(),before,'Membership below a declared scope must not rewrite or reject legacy upstream parents');
    """)


def test_empty_source_scope_makes_browse_a_direct_statement_action_not_an_empty_menu() -> None:
    _run_controls(r"""
      const before=JSON.stringify(sharedIndex);
      sharedIndex=Object.assign({},sharedIndex,{nodes:sharedIndex.nodes.filter(node=>node!==source)});records.delete(source.id);
      syncControls();
      assert(!dom$sourcesMode||!dom$browsePopover.contains(dom$sourcesMode),
        'An empty authored source pool must not expose a Source action');
      assert.equal(dom$browsePopover.hidden,true);
      assert.equal(dom$browseToggle.getAttribute('aria-controls'),null);
      assert.equal(dom$browseToggle.getAttribute('aria-expanded'),null);
      dom$browseToggle.focus({});emit(dom$browseToggle,'click');
      assert.equal(dom$browsePopover.hidden,true,'A single Statements action must not open an empty disclosure');
      assert.deepEqual(modes,[{mode:'list',push:true}]);assert.equal(ui$relationIds,null);
      assert.equal(document.activeElement,dom$browseToggle);
      sharedIndex=Object.assign({},sharedIndex,{nodes:sharedIndex.nodes.concat(source)});records.set(source.id,source);
      ui$mode='graph';syncControls();
      assert(dom$sourcesMode&&dom$browsePopover.contains(dom$sourcesMode)&&!dom$sourcesMode.hidden);
      emit(dom$browseToggle,'click');assert.equal(dom$browsePopover.hidden,false);
      assert.deepEqual(modes,[{mode:'list',push:true}],'Adding real sources restores disclosure, not a second dispatch');
      assert.equal(JSON.stringify(sharedIndex),before);
    """)


def test_losing_scoped_sources_closes_the_owned_menu_and_returns_focus_before_detaching_its_choice() -> None:
    _run_controls(r"""
      emit(dom$browseToggle,'click');dom$sourcesMode.focus({});
      assert.equal(dom$browsePopover.hidden,false);
      sharedIndex=Object.assign({},sharedIndex,{nodes:sharedIndex.nodes.filter(node=>node!==source)});records.delete(source.id);
      syncControls();
      assert.equal(dom$browsePopover.hidden,true);assert.equal(dom$browsePopover.contains(dom$sourcesMode),false);
      assert.equal(document.activeElement,dom$browseToggle);
      assert.deepEqual(dom$browseToggle.focuses.at(-1),{preventScroll:true});
      assert.equal(dom$browseToggle.getAttribute('aria-controls'),null);
      assert.equal(dom$browseToggle.getAttribute('aria-expanded'),null);
      assert.deepEqual(modes,[],'Source disappearance is not a user navigation');
    """)


def test_source_free_direct_statement_button_handles_keyboard_generated_click_without_opening_a_menu() -> None:
    _run_controls(r"""
      sharedIndex=Object.assign({},sharedIndex,{nodes:sharedIndex.nodes.filter(node=>node!==source)});records.delete(source.id);
      syncControls();dom$browseToggle.focus({});
      assert.equal(dom$browseToggle.type,'button');
      assert.equal(dom$browseToggle.getAttribute('aria-controls'),null);
      assert.equal(dom$browseToggle.getAttribute('aria-expanded'),null);
      // Native Enter/Space button activation produces a zero-detail click;
      // the peer does not pretend to synthesize browser default key handling.
      emit(dom$browseToggle,'click',{button:0,detail:0});
      assert.deepEqual(modes,[{mode:'list',push:true}]);assert.equal(ui$relationIds,null);
      assert.equal(dom$browsePopover.hidden,true);assert.equal(document.activeElement,dom$browseToggle);
      assert.equal(dom$browsePopover.contains(dom$sourcesMode),false);
    """)


def test_first_breadcrumb_targets_deployment_home_and_preserves_native_modified_clicks() -> None:
    _run_controls(r"""
      homeURL='https://example.test/deployed/project/index.html';
      sharedIndex=null;records.clear();
      renderBreadcrumb();const home=dom$breadcrumb.querySelector('.bp-dag-book');
      assert(home);assert.equal(home.href,homeURL,
        'Home must lead to the deployment home even when the index is unavailable');
      assert.strictEqual(home.parentElement,dom$breadcrumb.firstChild.firstChild);
      assert.equal(host.querySelectorAll('.bp-dag-book').length,1,'Home must have one real entrance, not a duplicate widget');
      for(const settings of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{button:2},{defaultPrevented:true}]) {
        const count=navigationCalls.length,event=emit(home,'click',Object.assign({button:0},settings));
        assert.equal(event.defaultPrevented,Boolean(settings.defaultPrevented));
        assert.equal(navigationCalls.length,count);
      }
      const event=emit(home,'click',{button:0});assert.equal(event.defaultPrevented,true);
      assert.equal(navigationCalls.at(-1).url,homeURL);
      assert.deepEqual(navigationCalls.at(-1).options,{history:'push',focus:true,mapFirst:false});
      currentBundle={nodeId:null,scope:'roadmap',title:'Authored library',url:homeURL,graph:{view:{}}};
      const entries=routeTrail(currentBundle);
      assert.equal(entries.length,1,'The site home must not pretend to be inside the roadmap trail');
      assert.equal(entries[0].url,null,'The only home crumb is current, not a link to another view');
      renderBreadcrumb();assert.equal(dom$breadcrumb.querySelectorAll('.bp-dag-book').length,0);
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


def _weighted_publisher_fixture(tmp_path: Path) -> tuple[Graph, dict, dict, dict]:
    # Direct Node tuples model original weighted pairs at this private boundary.
    # load_graph de-duplicates ordinary Markdown references; these multiplicities
    # are synthetic arithmetic coverage, not a source-parser or Lean proof claim.
    nodes = {
        "A": Node("A", "A", tmp_path / "A.md", (), declaration="def"),
        "F": Node("F", "F", tmp_path / "F.md", ("A",),
                  statement_dependencies=("A", "A"), proof_dependencies=("A", "A", "A"), declaration="theorem"),
        "B": Node("B", "B", tmp_path / "B.md", ("F",), proof_dependencies=("F",) * 4, declaration="theorem"),
    }
    graph = Graph(tmp_path, nodes)
    numbers = {"A": "Definition 1", "F": "Theorem 1", "B": "Theorem 2"}
    links = {key: "https://example.test/chapter/" + key + ".html" for key in nodes}
    used_by = {key: [other.id for other in nodes.values() if key in other.dependencies] for key in nodes}
    return graph, numbers, links, used_by


def test_reader_keeps_exact_weighted_relations_separate_from_closed_structural_details(tmp_path: Path) -> None:
    graph, numbers, routes, used_by = _weighted_publisher_fixture(tmp_path)
    before = copy.deepcopy((graph, numbers, routes, used_by))
    markup = render._dependency_disclosure(
        graph.nodes["F"], graph=graph, statuses=status.derive(graph), numbers=numbers, used_by=used_by, links=routes,
    )
    tree = html5lib.parseFragment(markup, namespaceHTMLElements=False)
    details = next(tree.iter("details"))
    assert details.get("class") == "bp-dependencies" and details.get("open") is None
    assert "".join(next(tree.iter("summary")).itertext()) == "Uses 1 · Used by 1"
    labels = ["".join(node.itertext()) for node in tree.iter("span") if node.get("class") == "bp-key"]
    assert labels == ["Statement uses", "Used by"], "The shared prerequisite stays in one typed row, not a duplicate panel"
    incoming = [node for node in tree.iter("a") if node.get("href") == routes["A"]]
    outgoing = [node for node in tree.iter("a") if node.get("href") == routes["B"]]
    assert len(incoming) == 2 and len(outgoing) == 1
    for link in incoming:
        assert "".join(link.itertext()) == "Definition 1 (A)"
        assert link.get("aria-label") == "Definition 1 (A) · 2 statement dependencies, 3 proof dependencies"
        assert link.get("title") == link.get("aria-label"), "Suppressed proof-only rows must retain their original proof information"
    assert outgoing[0].get("aria-label") == "Theorem 2 (B) · 4 proof dependencies"
    assert outgoing[0].get("title") == outgoing[0].get("aria-label")
    assert (graph, numbers, routes, used_by) == before
    # The same captured pair counts accompany the actual closed, unweighted
    # structural controls. The redesign adds no second relation panel.
    _run(r"""
      const a=record('A','chapter'),b=record('B','chapter');configure([focus,a,b],[['A','F',2,3],['F','B',0,4]]);
      const before=snapshot();renderReaderContext();
      assert.equal(marked(dom$readingNeighbors,'bp-reading-links'),undefined);
      const details=marked(dom$readingNeighbors,'bp-reading-related-topics');
      assert(details);assert.equal(details.tagName,'DETAILS');assert.equal(details.getAttribute('open'),null);
      assert.deepEqual(dom$readingNeighbors.children,[details]);
      assert.equal(details.children[0].tagName,'SUMMARY');assert.equal(details.children[0].textContent,'More in '+owner.title);
      assert.deepEqual(links(details).map(link=>link.href),[a.route,b.route]);
      assert(links(details).every(link=>link.getAttribute('aria-label')===null),'Structural links cannot claim dependency weights');
      assert.equal(snapshot(),before);
    """)


def test_dependency_annotation_preserves_validated_title_parts_and_default_reference_bytes(tmp_path: Path) -> None:
    graph, numbers, routes, used_by = _weighted_publisher_fixture(tmp_path)
    default = render._node_references(["A"], graph=graph, statuses=status.derive(graph), numbers=numbers, links=routes)
    assert default == '<a class="bp-ref bp-ref-can_state" href="https://example.test/chapter/A.html">Definition 1 (A)</a>'
    graph.nodes["A"] = dataclasses.replace(graph.nodes["A"], title=r'Groups $x$ and `$literal$` "quoted" & rings')
    before = copy.deepcopy(graph)
    kwargs = {"graph": graph, "statuses": status.derive(graph), "numbers": numbers, "links": routes}
    plain = html5lib.parseFragment(render._node_references(["A"], **kwargs), namespaceHTMLElements=False)
    marked = html5lib.parseFragment(
        render._node_references(["A"], **kwargs, relation_node=graph.nodes["F"]), namespaceHTMLElements=False,
    )
    original, annotated = next(plain.iter("a")), next(marked.iter("a"))
    assert original.get("aria-label") is None and original.get("title") is None
    assert original.get("href") == annotated.get("href") == routes["A"]
    assert annotated.get("aria-label") == 'Definition 1 (Groups $x$ and `$literal$` "quoted" & rings) · 2 statement dependencies, 3 proof dependencies'
    assert annotated.get("title") == annotated.get("aria-label")
    assert "".join(original.itertext()) == "".join(annotated.itertext())
    assert [node.text for node in original.iter("code")] == [node.text for node in annotated.iter("code")] == ["$literal$"]
    assert [node.text for node in original.iter("span")] == [node.text for node in annotated.iter("span")] == [r"\(x\)"]
    assert all(node.tag not in {"script", "img", "iframe"} for node in marked.iter())
    assert graph == before


def test_valid_source_shared_statement_and_proof_reference_exposes_both_original_kinds(tmp_path: Path) -> None:
    roadmap = tmp_path / "roadmap"
    roadmap.mkdir()
    (roadmap / "README.md").write_text("# Chapter\n\n- [A](A.md)\n- [F](F.md)\n", encoding="utf-8")
    (roadmap / "A.md").write_text("# A\n\nAn authored base.\n", encoding="utf-8")
    (roadmap / "F.md").write_text(
        "# F\n\nAn authored result.\n\n## Depends on\n\n- [A](A.md)\n- [A again](A.md)\n\n"
        "## Proof depends on\n\n- [A](A.md)\n- [A again](A.md)\n", encoding="utf-8",
    )
    graph = load_graph(tmp_path)
    assert graph.nodes["F"].statement_dependencies == ("A",) and graph.nodes["F"].proof_dependencies == ("A",)
    before = copy.deepcopy(graph)
    markup = render._dependency_disclosure(
        graph.nodes["F"], graph=graph, statuses=status.derive(graph), numbers={"A": "Definition 1", "F": "Theorem 1"},
        used_by={"A": ["F"], "F": []}, links={"A": "A.html", "F": "F.html"},
    )
    tree = html5lib.parseFragment(markup, namespaceHTMLElements=False)
    link = next(tree.iter("a"))
    assert link.get("href") == "A.html"
    assert link.get("aria-label") == "Definition 1 (A) · 1 statement dependency, 1 proof dependency"
    assert "Proof uses" not in "".join(tree.itertext())
    assert graph == before


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
