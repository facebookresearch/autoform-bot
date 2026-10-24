"""Execute scoped-overview callbacks with isolated DOM, viewport and frame peers.

These source regressions do not certify native layout, hit targets, or MathJax.
The genuine producer/browser journeys own those checks.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated scoped-overview controls")

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
const names=['element','safeHref','graphAt','scopeOverviewProjection','scopeOverviewFlow','recordId','mapNode','adoptGraph',
  'neighborIds','neighborhood','regionProjection','updateProjection','semanticParent','routeTrail',
  'currentOverviewGroup','trailList','syncNavigationSelection','compactRegions','collect','matchesStatus','calculateActive',
  'syncControls','refresh','activateNode','focusNodeControl','setSheet','writeLocation','setMode','zoomAt','closeSearch'];
const constants=['NODE_W','SEARCH_PAGE','clamp','plural','pressed'].map(name=>{
  const start=source.indexOf('  var '+name+' =');assert(start>=0,'Missing production constant: '+name);
  const declaration=source.slice(start,source.indexOf('\n',start));new vm.Script(declaration);return declaration;
}).join('\n');
const context={assert,structuredClone,URL};vm.createContext(context);
const production=constants+'\n'+names.map(original).join('\n');
"""

_PEERS = r"""
const effects=[],frames=[];
// Only DOM/state boundaries are peers; graph projection and callbacks below
// are the extracted production functions, not copies or rewritten algorithms.
function peer(tag='div') {
  const attributes=new Map(),classes=new Set(),listeners=new Map();
  return {tagName:tag.toUpperCase(),dataset:{},children:[],offsetTop:0,hidden:false,listeners,
    classList:{toggle(name,on){if(on)classes.add(name);else classes.delete(name);},
      contains(name){return classes.has(name);}},
    setAttribute(name,value){attributes.set(name,String(value));},
    getAttribute(name){return attributes.get(name)||null;},removeAttribute(name){attributes.delete(name);},
    addEventListener(type,callback){const callbacks=listeners.get(type)||[];callbacks.push(callback);listeners.set(type,callbacks);},
    appendChild(child){this.children.push(child);return child;},replaceChildren(...children){this.children=children;},
    remove(){this.removed=true;},
    focus(){document.activeElement=this;effects.push({kind:'focus',id:this.dataset.autoformNodeId||'reading'});}};
}
const document={activeElement:null,createElement:peer};
const window={location:{href:'https://example.test/roadmap/chapter/index.html',origin:'https://example.test'}};
function requestAnimationFrame(callback){frames.push(callback);return frames.length;}
function flushFrames(){let turns=0;while(frames.length){assert(++turns<32,'Unbounded control frame chain');frames.shift()();}}
function clickPeer(link,settings={}) {
  const event=Object.assign({target:link,button:0,defaultPrevented:false,
    preventDefault(){this.defaultPrevented=true;}},settings);
  (link.listeners.get('click')||[]).forEach(callback=>callback(event));return event;
}
const host=peer(),dom$body=peer(),dom$sheetHandle=peer('button'),dom$breadcrumb=peer('nav');
const dom$mapContext=peer(),dom$mapKey=peer(),dom$isolated=peer('input'),dom$filterSummary=peer('button');
const dom$graphMode=peer('button'),dom$listMode=peer('button'),dom$sourcesMode=peer('button'),dom$regionsMode=peer('button');
const dom$stage=Object.assign(peer(),{clientWidth:900,clientHeight:700}),dom$inventory=peer(),inventoryHeading=peer('h2');
dom$inventory.querySelector=selector=>{assert.equal(selector,'h2');return inventoryHeading;};
const dom$hiddenButton=peer(),dom$empty=peer(),dom$searchResults=peer();
const dom$statusInputs=new Map([['planned',peer('input')],['fully_proved',peer('input')]]);
var dom$statusFieldset=null,dom$navigationNodes=new Map();
const chapterId='view-group:3';
const nodes=Array.from({length:42},(_,number)=>({id:number===20?'view-group:0':'n'+String(number).padStart(2,'0'),
  title:'Fixture result '+number,kind:'node',area:number<21?'Authored area A':'Authored area B',
  status:number===1||number===21?'fully_proved':'planned',member_count:1,
  status_counts:[[number===1||number===21?'fully_proved':'planned',1]],internal_dependency_count:0,
  layout:{width:208,height:100},url:'result-'+number+'.html'}));
var contextGraph={schema:'autoform-dag-view/v2',title:'Large scope',nodes,
  edges:[{source:'n00',target:'n22',statement:2,proof:3},{source:'n02',target:'n03',statement:5,proof:7}],
  palette:{planned:{},fully_proved:{},inventory_pending:{}},view:{presentation:'dag',scope:chapterId}};
var records=new Map(nodes.map(node=>[node.id,Object.assign({},node,{scope:chapterId,parent:chapterId,result:true})]));
records.set('roadmap',{id:'roadmap',scope:'roadmap',title:'Library',status:'planned',route:'https://example.test/roadmap/index.html'});
records.set(chapterId,{id:chapterId,scope:chapterId,parent:'roadmap',title:'Large scope',status:'planned',route:window.location.href});
['view-group:1','view-group:2'].forEach(id=>records.set(id,{id,scope:id,parent:'roadmap',title:'Source track',status:'planned',navigation:'source'}));
var sharedIndex={nodes:Array.from(records.values()),relations:[]},currentBundle={nodeId:chapterId,scope:chapterId,
  title:'Large scope',graph:contextGraph,dataURL:'https://example.test/dependencies/chapters/chapter.json'};
var data=contextGraph,view,atlas,palette,byId,prerequisites,dependents,statusCounts,presentStatuses,mapAllowed;
var ui$requestedNode=chapterId,ui$overviewGroup=null,ui$scopeOverview=false,ui$area=null,ui$overviewOnly=false;
var ui$selected=null,ui$direction='both',ui$active=[],ui$activeIds=new Set(),ui$activeDirty=true,ui$spotlight=null,ui$hovered=null;
var ui$selectedStatuses=new Set(['fully_proved']),ui$filtersTouched=true,ui$showIsolated=true;
var ui$mode='graph',ui$mapAllowed=true,ui$listLimit=100,ui$relationIds=null,ui$transform={x:0,y:0,scale:.9};
var firstResize=false,trail=[],overlays=new Map();
const navigation={checkpoint(method){effects.push({kind:'checkpoint',method:method||'replace',group:ui$overviewGroup});},
  navigateNode(id,options){effects.push({kind:'navigate',id,options});},
  navigate(url,options){effects.push({kind:'navigate-url',url,options});}};
// Observer/painter/reading boundaries are explicit peers, not native geometry.
function schedule(){effects.push({kind:'paint-request'});}
function remember(){effects.push({kind:'remember'});}
function renderInspector(){}function renderInventory(){}function syncSheetAccessibility(){}
function sheetLayout(){return false;}
function resize(){syncControls();ui$active.forEach(node=>{const control=peer('button');control.dataset.autoformNodeId=node.id;overlays.set(node.id,control);});}
function fit(){effects.push({kind:'fit'});}
function centerIfHidden(node){effects.push({kind:'center',id:node.id});}
function screen(){return {x:0,y:0,w:264,h:148};}
function labels(){return dom$breadcrumb.children[0].children.map(item=>item.title);}
// The map header is a title span and its counts, so read it as its parts' text.
function context(){return dom$mapContext.children.map(child=>typeof child==='string'?child:child.textContent).join('');}
function areaNode(area){return data.nodes.find(node=>node._overviewGroup==='area:'+area);}
updateProjection();syncControls();
"""


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    script += "vm.runInContext(production+" + json.dumps(_PEERS + "\n" + body) + ",context,{timeout:2000});\n"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def test_global_source_ids_do_not_hide_groups_or_alias_the_canonical_scope() -> None:
    _run(r"""
      const before=JSON.stringify(contextGraph),groups=data.nodes.filter(node=>node._overviewGroup);
      assert.equal(groups.length,2);assert.equal(ui$selected,null);
      groups.forEach(group=>{assert(!records.has(group.id));assert.equal(recordId(group),null);assert(mapNode(group));});
      assert.deepEqual(data._scopeOverview.groups.flatMap(group=>group.members.map(node=>node.id)).sort(),nodes.map(node=>node.id).sort());
      assert.equal(groups.reduce((sum,group)=>sum+group.member_count,0),42);
      assert.equal(data.edges[0].statement,2);assert.equal(data.edges[0].proof,3);
      assert.equal(groups.reduce((sum,group)=>sum+group.internal_dependency_count,0),12);
      assert.deepEqual(labels(),['Library','Large scope']);
      // The defensive record boundary also protects an already-derived group
      // carrying a legacy colliding ID, without resurrecting actual sources.
      const collision=Object.assign({},groups[0],{id:'view-group:1'});
      assert.equal(recordId(collision),null);assert(mapNode(collision));
      assert(!mapNode(Object.assign({},collision,{map:false})));
      const source={id:'scope:view-group:1',kind:'scope'};
      assert.equal(recordId(source),'view-group:1');assert(!mapNode(source));
      assert.equal(recordId(nodes[1]),'n01');assert.equal(JSON.stringify(contextGraph),before);
    """)


def test_group_activation_focuses_an_eligible_member_and_keeps_canonical_navigation() -> None:
    _run(r"""
      const before=JSON.stringify(contextGraph),group=areaNode('Authored area A');
      assert.equal(data._scopeOverview.groups[0].members[0].status,'planned');
      ui$selected={id:'stale-selection'};ui$direction='prerequisites';host.classList.toggle('bp-dag-sheet-open',true);
      activateNode(group);flushFrames();
      assert.equal(ui$overviewGroup,'area:Authored area A');assert.equal(ui$requestedNode,null);
      assert.equal(ui$selected,null);assert.equal(ui$direction,'both');
      assert.deepEqual(Array.from(ui$selectedStatuses),['fully_proved']);
      assert(!host.classList.contains('bp-dag-sheet-open'));assert.equal(dom$sheetHandle.getAttribute('aria-expanded'),'false');
      assert.equal(document.activeElement.dataset.autoformNodeId,'n01');assert(ui$activeIds.has('n01'));
      assert(!ui$activeIds.has('n00'));assert(effects.some(event=>event.kind==='center'&&event.id==='n01'));
      assert(!effects.some(event=>event.kind==='focus'&&event.id==='n00'));
      assert(!effects.some(event=>event.kind==='navigate'));
      assert(effects.some(event=>event.kind==='checkpoint'&&event.method==='push'&&event.group==='area:Authored area A'));
      assert.equal(JSON.stringify(contextGraph),before);
      activateNode(byId.get('n01'));
      const entered=effects.find(event=>event.kind==='navigate');
      assert.equal(entered.id,'n01');assert.deepEqual(entered.options,{history:'push',focus:'reading'});
    """)


def test_group_context_and_regions_control_identify_the_current_plane() -> None:
    _run(r"""
      assert.equal(context(),'Large scope');assert.equal(dom$regionsMode.getAttribute('aria-pressed'),'true');
      activateNode(areaNode('Authored area A'));flushFrames();
      assert.equal(currentOverviewGroup().area,'Authored area A');
      assert.deepEqual(labels(),['Library','Large scope','Authored area A']);
      assert.equal(dom$breadcrumb.children[0].children.at(-1).getAttribute('aria-current'),'page');
      assert.equal(context(),'Authored area A');
      assert.equal(dom$regionsMode.getAttribute('aria-pressed'),'false');assert.equal(dom$graphMode.getAttribute('aria-pressed'),'true');
      setMode('regions',true);
      assert.equal(ui$overviewGroup,null);assert.deepEqual(labels(),['Library','Large scope']);
      assert.equal(context(),'Large scope');assert.equal(dom$regionsMode.getAttribute('aria-pressed'),'true');
      assert(effects.some(event=>event.kind==='checkpoint'&&event.method==='replace'&&event.group==='area:Authored area A'));
      assert(effects.some(event=>event.kind==='checkpoint'&&event.method==='push'&&event.group===null),
        'Returning to this scope overview must preserve the expanded group as a Back destination');
      assert(!effects.some(event=>event.kind==='navigate'));
    """)


def test_unassigned_group_uses_a_structural_label_not_an_invented_area() -> None:
    _run(r"""
      contextGraph=structuredClone(contextGraph);contextGraph.nodes[1].area=null;updateProjection();syncControls();
      const group=data.nodes.find(node=>node._overviewGroup==='unassigned');
      assert(group);assert.equal(group.area,undefined);assert.equal(group._region,undefined);
      // On a phone the unassigned bucket is still an area card, so the overview stays compact and keeps its links.
      dom$stage.clientWidth=390;assert(compactRegions());dom$stage.clientWidth=900;
      activateNode(group);flushFrames();
      assert.equal(ui$overviewGroup,'unassigned');assert.equal(currentOverviewGroup().area,null);
      assert.equal(context(),'Unassigned topics');
      assert.deepEqual(labels(),['Library','Large scope','Unassigned topics']);
      assert.equal(document.activeElement.dataset.autoformNodeId,'n01');
      assert.equal(dom$regionsMode.getAttribute('aria-pressed'),'false');
    """)


def test_zoom_out_returns_to_overview_without_changing_members_or_source_rows() -> None:
    _run(r"""
      const before=JSON.stringify(contextGraph);activateNode(areaNode('Authored area A'));flushFrames();
      ui$transform.scale=.9;zoomAt(.8,450,350);
      assert.equal(ui$overviewGroup,null);assert.equal(ui$scopeOverview,true);
      assert.deepEqual(labels(),['Library','Large scope']);assert.equal(context(),'Large scope');
      assert.equal(dom$regionsMode.getAttribute('aria-pressed'),'true');
      assert.equal(data._scopeOverview.groups.flatMap(group=>group.members).length,42);
      assert.equal(JSON.stringify(contextGraph),before);
      assert.equal(effects.filter(event=>event.kind==='checkpoint'&&event.method==='push').length,2);
      assert(!effects.some(event=>event.kind==='navigate'));
    """)


def test_hidden_original_endpoints_and_boundary_status_gate_cached_typed_flows() -> None:
    _run(r"""
      contextGraph=structuredClone(contextGraph);
      contextGraph.nodes.push({id:'boundary:outside',title:'External scope',kind:'boundary',status:'planned',
        status_counts:[['planned',1]],member_count:1,layout:{width:208,height:100}});
      contextGraph.edges.push({source:'n01',target:'n21',statement:11,proof:13},
        {source:'n01',target:'boundary:outside',statement:5,proof:0},
        {source:'boundary:outside',target:'n21',statement:0,proof:7});
      const before=JSON.stringify(contextGraph);updateProjection();calculateActive();
      const source=areaNode('Authored area A'),target=areaNode('Authored area B');
      const index=(a,b)=>data.edges.findIndex(edge=>edge.source===a&&edge.target===b);
      const cross=index(source.id,target.id),out=index(source.id,'boundary:outside'),incoming=index('boundary:outside',target.id);
      assert(cross>=0&&out>=0&&incoming>=0);assert(ui$activeIds.has(source.id)&&ui$activeIds.has(target.id));
      assert.equal(data.edges[cross].statement,13);assert.equal(data.edges[cross].proof,16);
      assert.deepEqual(data._scopeOverview._flows[cross],{statement:11,proof:13},
        'Visible groups cannot include the dependency between their hidden planned members');
      assert.deepEqual(data._scopeOverview._flows[out],{statement:0,proof:0});
      assert.deepEqual(data._scopeOverview._flows[incoming],{statement:0,proof:0});
      assert(!ui$activeIds.has('boundary:outside'));
      const route={kind:'proof',count:7};
      assert.equal(scopeOverviewFlow(route,data._scopeOverview._flows[incoming],data._scopeOverview._flowRange),null);
      const canonicalRange=data._scopeOverview._flowRange,sameData=data,sameNodes=data.nodes,sameEdges=data.edges;
      ui$selectedStatuses.add('planned');ui$activeDirty=true;calculateActive();
      assert.deepEqual(data._scopeOverview._flows[cross],{statement:13,proof:16});
      assert.deepEqual(data._scopeOverview._flows[out],{statement:5,proof:0});
      assert.deepEqual(data._scopeOverview._flows[incoming],{statement:0,proof:7});
      assert(ui$activeIds.has('boundary:outside'));assert.equal(data._scopeOverview._flowRange,canonicalRange);
      assert.strictEqual(scopeOverviewFlow(route,data._scopeOverview._flows[incoming],canonicalRange),route);
      assert.strictEqual(data,sameData);assert.strictEqual(data.nodes,sameNodes);assert.strictEqual(data.edges,sameEdges);
      assert.equal(JSON.stringify(contextGraph),before,'Filtering changed canonical nodes or typed relations');
    """)


def test_clean_active_replays_do_not_rescan_original_rows_or_replace_flow_cache() -> None:
    _run(r"""
      calculateActive();assert.equal(ui$activeDirty,false);
      const flows=data._scopeOverview._flows,range=data._scopeOverview._flowRange,active=ui$activeIds;
      const originals=data.edges.map(edge=>edge._overviewEdges);
      data.edges.forEach(edge=>Object.defineProperty(edge,'_overviewEdges',{
        configurable:true,get(){assert.fail('Clean active replay rescanned original relation rows');}}));
      calculateActive();calculateActive();
      assert.strictEqual(data._scopeOverview._flows,flows);assert.equal(data._scopeOverview._flowRange,range);
      assert.strictEqual(ui$activeIds,active);
      data.edges.forEach((edge,index)=>Object.defineProperty(edge,'_overviewEdges',{
        configurable:true,writable:true,enumerable:true,value:originals[index]}));
      ui$selectedStatuses.add('planned');ui$activeDirty=true;calculateActive();
      assert.notStrictEqual(data._scopeOverview._flows,flows);
      assert.deepEqual(data._scopeOverview._flows[0],{statement:2,proof:3});
      assert.equal(data._scopeOverview._flowRange,range);
    """)


def test_authored_scope_entry_requests_map_first_without_changing_result_reading_intent() -> None:
    _run(r"""
      const scope={id:'scope:'+chapterId,kind:'scope',title:'Large scope'};
      activateNode(scope);
      assert.deepEqual(effects.at(-1),{kind:'navigate',id:chapterId,
        options:{history:'push',focus:'reading',mapFirst:true}});
      records.get(chapterId).result=true;activateNode(scope);
      assert.deepEqual(effects.at(-1),{kind:'navigate',id:chapterId,options:{history:'push',focus:'reading'}});
      activateNode(nodes[1]);
      assert.deepEqual(effects.at(-1),{kind:'navigate',id:'n01',options:{history:'push',focus:'reading'}});
    """)


def test_actual_overview_parent_breadcrumb_returns_locally_and_preserves_modified_clicks() -> None:
    _run(r"""
      const before=JSON.stringify(contextGraph);activateNode(areaNode('Authored area A'));flushFrames();
      const item=dom$breadcrumb.children[0].children.find(item=>item.title==='Large scope'),link=item.children[0];
      assert.equal(link.tagName,'A');assert.equal(link.href,window.location.href);
      const untouched=effects.length;
      for(const settings of [{metaKey:true},{ctrlKey:true},{shiftKey:true},{altKey:true},{button:1},{defaultPrevented:true}]) {
        const event=clickPeer(link,settings);assert.equal(event.defaultPrevented,Boolean(settings.defaultPrevented));
        assert.equal(ui$overviewGroup,'area:Authored area A');assert.equal(effects.length,untouched);
      }
      const event=clickPeer(link);assert.equal(event.defaultPrevented,true);
      assert.equal(ui$overviewGroup,null);assert.deepEqual(labels(),['Library','Large scope']);
      assert.equal(context(),'Large scope');assert.equal(dom$regionsMode.getAttribute('aria-pressed'),'true');
      assert(!effects.some(event=>event.kind==='navigate'||event.kind==='navigate-url'));
      assert(effects.some(event=>event.kind==='checkpoint'&&event.method==='push'&&event.group==='area:Authored area A'));
      const returning=effects.slice(untouched).filter(event=>event.kind==='checkpoint');
      assert.deepEqual(returning.map(event=>[event.method,event.group]),[['replace','area:Authored area A'],['push',null]],
        'The parent breadcrumb must save its expanded plane before pushing this local overview');
      assert.equal(window.location.href,'https://example.test/roadmap/chapter/index.html');
      assert.equal(JSON.stringify(contextGraph),before);
    """)


def test_internal_breadcrumb_soft_navigation_and_off_origin_text_policy() -> None:
    _run(r"""
      const link=dom$breadcrumb.children[0].children[0].children[0];
      assert.equal(link.href,records.get('roadmap').route);
      const event=clickPeer(link);assert.equal(event.defaultPrevented,true);
      assert.deepEqual(effects.at(-1),{kind:'navigate-url',url:records.get('roadmap').route,
        options:{history:'push',focus:true,mapFirst:true}});
      const external=trailList([{label:'External source',url:'https://outside.test/book'}]);
      assert.equal(external.children[0].textContent,'External source');assert.equal(external.children[0].children.length,0,
        'Existing off-origin breadcrumb safety keeps a text entry, not a fabricated native anchor');
      assert.equal(external.children[0].listeners.size,0);
    """)
