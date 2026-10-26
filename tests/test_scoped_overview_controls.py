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
const names=['element','readingLabel','refreshReadingLabels','safeHref','graphAt','scopeOverviewProjection','structuralScopeProjection','boundaryContextProjection','expandedGroupShelves','scopeOverviewFlow','recordId','mapNode','adoptGraph',
  'neighborIds','neighborhood','regionProjection','updateProjection','semanticParent','routeTrail',
  'currentOverviewGroup','trailList','renderBreadcrumb','syncNavigationSelection','compactRegions','collect','matchesStatus','calculateActive',
  'syncControls','refresh','activateNode','focusNodeControl','setSheet','writeLocation','setMode','zoomAt','closeSearch','sourceNodes','inCurrentScope'];
const constants=['NODE_W','SEARCH_PAGE','clamp','plural'].map(name=>{
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
    get textContent(){return (this.text||'')+this.children.map(child=>child.textContent||'').join('');},
    set textContent(value){this.text=String(value);this.children=[];},
    classList:{toggle(name,on){if(on)classes.add(name);else classes.delete(name);},
      contains(name){return classes.has(name);}},
    setAttribute(name,value){attributes.set(name,String(value));},
    getAttribute(name){return attributes.get(name)||null;},removeAttribute(name){attributes.delete(name);},
    addEventListener(type,callback){const callbacks=listeners.get(type)||[];callbacks.push(callback);listeners.set(type,callbacks);},
    appendChild(child){this.children.push(child);return child;},replaceChildren(...children){this.children=children;},
    remove(){this.removed=true;},
    focus(){document.activeElement=this;effects.push({kind:'focus',id:this.dataset.autoformNodeId||'reading'});}};
}
const document={activeElement:null,createElement:peer,createTextNode(text){const node=peer('#text');node.textContent=text;return node;}};
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
const dom$graphMode=peer('button'),dom$listMode=peer('button'),dom$sourcesMode=peer('button');
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
  // Focus testing needs a genuinely linked eligible member; unlinked members
  // now belong to native shelves (covered by test_qa_native_shelves.py).
  edges:[{source:'n00',target:'n22',statement:2,proof:3},{source:'n01',target:'n02',statement:5,proof:7}],
  palette:{planned:{},fully_proved:{},inventory_pending:{}},view:{presentation:'dag',scope:chapterId}};
var records=new Map(nodes.map(node=>[node.id,Object.assign({},node,{scope:chapterId,parent:chapterId,result:true})]));
records.set('roadmap',{id:'roadmap',scope:'roadmap',title:'Library',status:'planned',route:'https://example.test/roadmap/index.html'});
records.set(chapterId,{id:chapterId,scope:chapterId,parent:'roadmap',title:'Large scope',status:'planned',route:window.location.href});
['view-group:1','view-group:2'].forEach(id=>records.set(id,{id,scope:id,parent:'roadmap',title:'Source track',status:'planned',navigation:'source'}));
var sharedIndex={nodes:Array.from(records.values()),relations:[]},currentBundle={nodeId:chapterId,scope:chapterId,
  title:'Large scope',graph:contextGraph,dataURL:'https://example.test/dependencies/chapters/chapter.json'};
var data=contextGraph,view,atlas,palette,byId,prerequisites,dependents,statusCounts,presentStatuses,mapAllowed;
var ui$requestedNode=chapterId,ui$overviewGroup=null,ui$scopeOverview=false,ui$area=null,ui$overviewOnly=false;
var ui$selected=null,ui$direction='both',ui$active=[],ui$activeIds=new Set(),ui$activeDirty=true,ui$spotlight=null;
var ui$selectedStatuses=new Set(['fully_proved']),ui$filtersTouched=true,ui$showIsolated=true;
var ui$mode='graph',ui$mapAllowed=true,ui$listLimit=100,ui$relationIds=null,ui$transform={x:0,y:0,scale:.9};
var firstResize=false,trail=[],overlays=new Map();
var ui$wikiProjectionTicket=0,ui$wikiView=null;
const navigation={checkpoint(method){effects.push({kind:'checkpoint',method:method||'replace',group:ui$overviewGroup});},
  navigateNode(id,options){effects.push({kind:'navigate',id,options});},
  navigate(url,options){effects.push({kind:'navigate-url',url,options});}};
// Observer/painter/reading boundaries are explicit peers, not native geometry.
function schedule(){effects.push({kind:'paint-request'});}
function projectWikiView() {}
function remember(){effects.push({kind:'remember'});}
function renderInspector(){}function renderInventory(){}function syncSheetAccessibility(){}
function renderShelves(){}
function geometry(){return {$display:{reduced:false}};}
var dom$shelves=null;
const dom$browsePopover=peer(),dom$browseToggle=peer();
function sheetLayout(){return false;}
function resize(){syncControls();ui$active.forEach(node=>{const control=peer('button');control.dataset.autoformNodeId=node.id;overlays.set(node.id,control);});}
function fit(){effects.push({kind:'fit'});}
function centerIfHidden(node){effects.push({kind:'center',id:node.id});}
function screen(){return {x:0,y:0,w:264,h:148};}
function labels(){return dom$breadcrumb.children[0].children.map(item=>item.title);}
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


def test_group_context_and_parent_return_identify_the_current_plane() -> None:
    _run(r"""
      assert.equal(dom$mapContext.textContent,'Large scope');assert.equal(dom$graphMode.hidden,true);
      activateNode(areaNode('Authored area A'));flushFrames();
      assert.equal(currentOverviewGroup().area,'Authored area A');
      assert.deepEqual(labels(),['Library','Large scope','Authored area A']);
      assert.equal(dom$breadcrumb.children[0].children.at(-1).getAttribute('aria-current'),'page');
      assert.equal(dom$mapContext.textContent,'Authored area A');
      assert.equal(dom$stage.hidden,false);assert.equal(dom$inventory.hidden,true);assert.equal(dom$graphMode.hidden,true);
      setMode('regions',true);
      assert.equal(ui$overviewGroup,null);assert.deepEqual(labels(),['Library','Large scope']);
      assert.equal(dom$mapContext.textContent,'Large scope');assert.equal(dom$graphMode.hidden,true);
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
      activateNode(group);flushFrames();
      assert.equal(ui$overviewGroup,'unassigned');assert.equal(currentOverviewGroup().area,null);
      assert.equal(dom$mapContext.textContent,'Unassigned topics');
      assert.deepEqual(labels(),['Library','Large scope','Unassigned topics']);
      assert.equal(document.activeElement.dataset.autoformNodeId,'n01');
      assert.equal(dom$graphMode.hidden,true);
    """)


def test_zoom_out_returns_to_overview_without_changing_members_or_source_rows() -> None:
    _run(r"""
      const before=JSON.stringify(contextGraph);activateNode(areaNode('Authored area A'));flushFrames();
      ui$transform.scale=.9;zoomAt(.8,450,350);
      assert.equal(ui$overviewGroup,null);assert.equal(ui$scopeOverview,true);
      assert.deepEqual(labels(),['Library','Large scope']);assert.equal(dom$mapContext.textContent,'Large scope');
      assert.equal(dom$graphMode.hidden,true);
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
      assert.equal(dom$mapContext.textContent,'Large scope');assert.equal(dom$graphMode.hidden,true);
      assert(!effects.some(event=>event.kind==='navigate'||event.kind==='navigate-url'));
      assert(effects.some(event=>event.kind==='checkpoint'&&event.method==='push'&&event.group==='area:Authored area A'));
      const returning=effects.slice(untouched).filter(event=>event.kind==='checkpoint');
      assert.deepEqual(returning.map(event=>[event.method,event.group]),[['replace','area:Authored area A'],['push',null]],
        'The parent breadcrumb must save its expanded plane before pushing this local overview');
      assert.equal(window.location.href,'https://example.test/roadmap/chapter/index.html');
      assert.equal(JSON.stringify(contextGraph),before);
    """)


def test_actual_atlas_parent_breadcrumb_returns_to_its_own_scope_and_preserves_native_modifiers() -> None:
    _run(r"""
      contextGraph=structuredClone(contextGraph);contextGraph.view.presentation='atlas';
      contextGraph.nodes=[...contextGraph.nodes.slice(0,7),...contextGraph.nodes.slice(21,28)];
      navigation.checkpoint=function(method) {
        effects.push({kind:'checkpoint',method:method||'replace',area:ui$area,scope:currentBundle.scope});
      };
      for(const scope of [chapterId,'roadmap']) {
        const owner=records.get(scope);window.location.href=owner.route;
        currentBundle={nodeId:scope,scope,title:owner.title,graph:contextGraph};
        contextGraph.title=owner.title;contextGraph.view.scope=scope;
        ui$requestedNode=scope;ui$overviewGroup=null;ui$area='Authored area A';
        updateProjection();syncControls();effects.length=0;
        assert.equal(ui$scopeOverview,false);assert.equal(ui$area,'Authored area A');
        const before=JSON.stringify({contextGraph,records:Array.from(records.values()),relations:sharedIndex.relations});
        const parent=dom$breadcrumb.children[0].children.find(item=>item.title===owner.title),link=parent.children[0];
        assert.equal(link.tagName,'A');assert.equal(link.textContent,owner.title);assert.equal(link.href,owner.route);
        assert.deepEqual(labels(),scope===chapterId?['Library','Large scope','Authored area A']:['Library','Authored area A']);
        for(const settings of [{metaKey:true},{ctrlKey:true},{shiftKey:true},{altKey:true},{button:1},{button:2},{defaultPrevented:true}]) {
          const event=clickPeer(link,settings);assert.equal(event.defaultPrevented,Boolean(settings.defaultPrevented));
          assert.equal(ui$area,'Authored area A');assert.equal(effects.length,0);
        }
        assert.equal(clickPeer(link).defaultPrevented,true);
        assert.equal(ui$area,null);assert.equal(ui$scopeOverview,false);assert.equal(ui$mode,'graph');
        assert.equal(currentBundle.scope,scope);assert.equal(window.location.href,owner.route);
        assert.deepEqual(labels(),scope===chapterId?['Library','Large scope']:['Library']);
        assert.equal(dom$mapContext.textContent,owner.title);assert.equal(dom$graphMode.hidden,true);
        assert(!effects.some(event=>event.kind==='navigate'||event.kind==='navigate-url'),
          'The authored-area parent must collapse locally, including in a non-root scope');
        assert.deepEqual(effects.filter(event=>event.kind==='checkpoint'),[
          {kind:'checkpoint',method:'replace',area:'Authored area A',scope},
          {kind:'checkpoint',method:'push',area:null,scope}
        ],'Back must retain the expanded area before its own scope overview is pushed');
        assert.deepEqual(data.nodes.map(node=>[node._region,node.member_count]),[
          ['Authored area A',7],['Authored area B',7]
        ]);
        assert.equal(contextGraph.nodes.length,14);
        assert.equal(JSON.stringify({contextGraph,records:Array.from(records.values()),relations:sharedIndex.relations}),before);
      }
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
