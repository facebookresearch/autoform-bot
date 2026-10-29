"""Actual wiki projection, geometry cache and canvas callbacks; no browser or mathematical evidence."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated wiki cluster projection tests")
_CALLBACKS = (
    "projectWikiLinks", "projectWikiView", "recordId", "mapNode", "regionProjection",
    "geometry", "compactRegions", "layoutGraph", "wikiRoutes", "world", "screen", "inViewport",
    "framingNodes", "framingViewport", "framingPadding", "sceneFrame", "sceneDetours", "freeArea", "sheetLayout", "currentOverviewGroup",
)

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
const constants=['NODE_W','SEARCH_PAGE','clamp'].map(name=>{
  const start=source.indexOf('  var '+name+' =');assert(start>=0);
  const declaration=source.slice(start,source.indexOf('\n',start));new vm.Script(declaration);return declaration;
}).join('\n');
const context={assert,setImmediate,setTimeout,clearTimeout};vm.createContext(context);
const production='"use strict";\n'+constants+'\n'+NAMES.map(original).join('\n');
"""

_PEERS = r"""
function freeze(value) {
  if(value&&typeof value==='object'){Object.values(value).forEach(freeze);Object.freeze(value);}return value;
}
const root={id:'roadmap',title:'Library',scope:'roadmap',parent:null,kind:'scope',navigation:'module',result:false};
const sourceRows=[root,...['A','B','C','D'].map(id=>({id,title:'Concept '+id,parent:'roadmap',scope:'roadmap',
  kind:'node',result:true,status:'planned',route:'https://example.test/roadmap/'+id+'.html'}))];
var records=new Map(sourceRows.map(row=>[row.id,freeze(row)]));
const sourceNodes=sourceRows.slice(1).map(row=>({...row,_recordId:row.id,width:196,height:100}));
var contextGraph=freeze({nodes:sourceNodes,edges:[{source:'A',target:'B',statement:1,proof:1}],view:{presentation:'dag'}});
var data={...contextGraph,nodes:contextGraph.nodes.map(row=>({...row})),edges:contextGraph.edges.map(edge=>({...edge}))};
var sharedIndex=freeze({schema:'autoform-dag-search/v1',nodes:sourceRows,relations:[['A','B',1,1]]});
function layer(references=[['C','D']],availability='complete',unavailable_sources=[]) {
  return freeze({schema:'autoform-wiki-links/v1',index_sha256:'d'.repeat(64),availability,references,unavailable_sources});
}
var ui$wikiLinks=layer(),ui$wikiView=null,ui$wikiProjectionTicket=0,ui$wikiProjectionCache=new WeakMap();
var alive=true,restoring=false,mapAllowed=true,ui$requestedNode='D',ui$area=null,ui$overviewGroup=null,ui$mode='graph';
var ui$orientation='lr',ui$transform={x:23,y:41,scale:1.25},ui$selectedStatuses=new Set(['planned','fully_proved']);
var ui$selected=data.nodes.find(node=>node.id==='D'),ui$active=data.nodes,ui$activeIds=new Set(data.nodes.map(node=>node.id));
var byId=new Map(data.nodes.map(node=>[node.id,node])),navigation={},atlas=false,family='sans-serif';
var scene,sceneData,sceneDirection,sceneCompact,sceneFamily,sceneAspect,sceneWiki,sceneReduced;
const reader={dataset:{},scrollTop:137,textContent:'Complete authored mathematical reading',focuses:0};
const document={activeElement:reader},dom$readingBody=reader,dom$stage={clientWidth:900,clientHeight:640},dom$inspector={offsetTop:0};
const window={location:{href:'https://example.test/roadmap/D.html'},history:{length:4},devicePixelRatio:1};
let syncs=0,schedules=0;
function syncControls() {syncs++;}
function schedule() {schedules++;}
function renderReaderContext() {throw new Error('Wiki geometry must preserve the reader');}
function renderWikiContext() {throw new Error('Wiki geometry must preserve concept-link disclosure');}
function renderInspector() {throw new Error('Wiki geometry must preserve mathematical DOM and focus');}
function snapshot() {
  return JSON.stringify({contextGraph,sharedIndex,records:Array.from(records.values()),layer:ui$wikiLinks,
    nodes:data.nodes,formal:data.edges,statuses:Array.from(ui$selectedStatuses),url:window.location.href,
    history:window.history.length,reading:reader.textContent,scroll:reader.scrollTop});
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const layouts=[],actualLayout=layoutGraph;
layoutGraph=function(...args){layouts.push(args);return actualLayout(...args);};
const held=[],projections=[],actualProjector=projectWikiLinks;
// Hold only the promise boundary; canonical ownership and counts are computed
// by the actual projector before its result is released.
projectWikiLinks=function(...args){
  projections.push(args);
  return actualProjector(...args).then(result=>new Promise((resolve,reject)=>held.push({result,resolve,reject})));
};
async function release() {
  await flush();const pending=held.shift();assert(pending,'Expected held actual projection result');
  pending.resolve(pending.result);await flush();return pending.result;
}
function center(id) {const box=screen(byId.get(id));return {x:box.x+box.w/2,y:box.y+box.h/2};}
function near(actual,expected) {assert(Math.abs(actual-expected)<1e-7,JSON.stringify({actual,expected}));}
"""


def _run(body: str, *, callbacks: tuple[str, ...] = _CALLBACKS, peers: str = _PEERS) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ",NAMES=" + json.dumps(callbacks) + ";\n" + _LOADER
    program = peers + "\n(async()=>{\n" + body + "\n})()"
    script += "const deadline=setTimeout(()=>{console.error('Wiki projection test did not settle');process.exitCode=1;},5000);\n"
    script += "Promise.resolve(vm.runInContext(production+" + json.dumps(program)
    script += ",context,{timeout:3000})).then(()=>clearTimeout(deadline),error=>{clearTimeout(deadline);console.error(error);process.exitCode=1;});\n"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def test_projection_is_atomic_and_repeated_pending_view_uses_one_actual_projector() -> None:
    _run(r"""
      geometry();const before=snapshot(),initial=scene;
      projectWikiView();const cached=ui$wikiProjectionCache.get(contextGraph),pending=Array.from(cached.views.values())[0];
      projectWikiView();assert.equal(projections.length,1);assert.equal(cached.views.size,1);
      assert.strictEqual(Array.from(cached.views.values())[0],pending);
      await flush();assert.equal(held.length,1);assert.equal(ui$wikiView,null);assert.equal(data.$wikiEdges,undefined);
      assert.strictEqual(scene,initial);assert.equal(syncs,0);assert.equal(schedules,0);assert.equal(snapshot(),before);
      const projected=await release();assert.strictEqual(ui$wikiView,projected);assert.strictEqual(data.$wikiEdges,projected.edges);
      assert.deepEqual(projected.edges,[{source:'C',target:'D',count:1}]);assert.equal(layouts.length,4);
      assert(layouts.slice(-2).every(args=>args[4]===data.$wikiEdges));assert.strictEqual(sceneWiki,data.$wikiEdges);
      assert.equal(schedules,1);assert.equal(snapshot(),before);assert.strictEqual(document.activeElement,reader);
    """)


@pytest.mark.parametrize("change", ["data", "layer", "index", "ticket", "disposal"])
def test_pending_projection_cannot_publish_into_another_route_layer_index_or_disposed_client(change: str) -> None:
    _run("const change=" + json.dumps(change) + ";\n" + r"""
      geometry();const before=snapshot(),initial=scene,originalLayer=ui$wikiLinks;
      projectWikiView();await flush();assert.equal(held.length,1);
      if(change==='data') {data={...data};ui$requestedNode='C';}
      if(change==='layer')ui$wikiLinks=layer([['D','C']]);
      if(change==='index')sharedIndex={...sharedIndex};
      if(change==='ticket')ui$wikiProjectionTicket++;
      if(change==='disposal')alive=false;
      const current=snapshot();await release();assert.equal(data.$wikiEdges,undefined);assert.equal(ui$wikiView,null);
      assert.strictEqual(scene,initial);assert.equal(layouts.length,2);assert.equal(syncs,0);assert.equal(schedules,0);
      assert.equal(snapshot(),current);assert.strictEqual(document.activeElement,reader);
      if(change!=='layer')assert.strictEqual(ui$wikiLinks,originalLayer);
      if(change!=='layer'&&change!=='index')assert.equal(snapshot(),before);
    """)


def test_view_cache_is_bounded_coalesces_revisits_and_recomputes_evicted_views() -> None:
    _run(r"""
      ui$mode='list';
      const articles=Array.from({length:13},(_,n)=>freeze({id:'article-'+n,title:'Article '+n,parent:'roadmap',
        scope:'roadmap',kind:'node',result:true,status:'planned'}));
      articles.forEach(row=>records.set(row.id,row));sharedIndex=freeze({...sharedIndex,nodes:[...sharedIndex.nodes,...articles]});
      for(const article of articles) {
        ui$requestedNode=article.id;projectWikiView();await release();
        assert(ui$wikiProjectionCache.get(contextGraph).views.size<=12);
      }
      const cached=ui$wikiProjectionCache.get(contextGraph);assert.equal(cached.views.size,12);
      assert.equal(projections.length,13);assert(!cached.views.has(JSON.stringify(['article-0',null,null])));
      projectWikiView();await flush();assert.equal(projections.length,13);assert.equal(held.length,0);
      ui$requestedNode='article-0';projectWikiView();await release();assert.equal(projections.length,14);
      assert.equal(cached.views.size,12);assert.equal(layouts.length,0);
      assert.deepEqual(data.edges,[{source:'A',target:'B',statement:1,proof:1}]);
      assert.strictEqual(document.activeElement,reader);assert.equal(reader.scrollTop,137);
    """)


@pytest.mark.parametrize("focus", ["reading", "graph"])
def test_optional_geometry_preserves_anchor_screen_pose_with_exactly_one_camera_compensation(focus: str) -> None:
    _run("const focus=" + json.dumps(focus) + ";\n" + r"""
      if(focus==='graph')document.activeElement={dataset:{autoformNodeId:'D'}};
      const focused=document.activeElement;geometry();
      const previous={...scene.nodes.get('D')},camera={...ui$transform},pose=center('D'),before=snapshot();
      projectWikiView();await release();const moved=scene.nodes.get('D');
      assert(previous.x!==moved.x||previous.y!==moved.y,'The fixture must exercise an actual geometry move');
      near(ui$transform.x,camera.x+(previous.x+previous.w/2-moved.x-moved.w/2)*camera.scale);
      near(ui$transform.y,camera.y+(previous.y+previous.h/2-moved.y-moved.h/2)*camera.scale);
      const after=center('D');near(after.x,pose.x);near(after.y,pose.y);assert.equal(ui$transform.scale,camera.scale);
      assert.strictEqual(document.activeElement,focused);assert.equal(snapshot(),before);assert.equal(layouts.length,4);
    """)


@pytest.mark.parametrize("focus", ["reading", "graph"])
def test_cached_projection_restoration_preserves_the_saved_clustered_camera_without_second_compensation(focus: str) -> None:
    _run("const focus=" + json.dumps(focus) + ";\n" + r"""
      if(focus==='graph')document.activeElement={dataset:{autoformNodeId:'D'}};
      const focused=document.activeElement;geometry();projectWikiView();await release();
      const clustered={...scene.nodes.get('D')},saved={...ui$transform},before=snapshot();
      data={...data};delete data.$wikiEdges;ui$wikiView=null;restoring=true;
      geometry();const bare={...scene.nodes.get('D')};
      assert(bare.x!==clustered.x||bare.y!==clustered.y,'Restoration must rebuild the actual bare-to-clustered geometry');
      ui$transform={...saved};projectWikiView();await flush();
      assert.equal(held.length,0);assert.equal(projections.length,1);assert.equal(layouts.length,8);
      assert.deepEqual(scene.nodes.get('D'),clustered);assert.deepEqual(ui$transform,saved);
      assert.strictEqual(document.activeElement,focused);assert.equal(snapshot(),before);
      restoring=false;const existing=scene;assert.strictEqual(geometry(),existing);assert.deepEqual(ui$transform,saved);
      assert.equal(layouts.length,8);
    """)


def test_geometry_cache_ignores_coverage_only_changes_but_updates_changed_or_removed_associations() -> None:
    _run(r"""
      geometry();projectWikiView();await release();const existing=scene,associations=data.$wikiEdges;
      const camera={...ui$transform};ui$wikiLinks=layer([['C','D']],'partial',['A']);
      projectWikiView();await release();assert.equal(ui$wikiView.availability,'partial');
      assert.strictEqual(data.$wikiEdges,associations);assert.strictEqual(geometry(),existing);assert.equal(layouts.length,4);
      assert.deepEqual(ui$transform,camera);
      ui$wikiLinks=layer([['C','D'],['D','C']]);projectWikiView();await release();
      assert.equal(layouts.length,6);assert.notStrictEqual(data.$wikiEdges,associations);
      ui$wikiLinks=layer([],'unavailable');projectWikiView();await release();
      assert.deepEqual(data.$wikiEdges,[]);assert.equal(layouts.length,8);assert.equal(ui$wikiView.availability,'unavailable');
      assert.deepEqual(data.edges,[{source:'A',target:'B',statement:1,proof:1}]);
      assert.strictEqual(document.activeElement,reader);assert.equal(reader.scrollTop,137);
    """)


@pytest.mark.parametrize("availability", ["complete", "unavailable"])
def test_empty_or_unavailable_initial_layer_keeps_the_cached_mathematical_map(availability: str) -> None:
    _run("const availability=" + json.dumps(availability) + ";\n" + r"""
      ui$wikiLinks=layer([],availability);geometry();const before=snapshot(),initial=scene,camera={...ui$transform};
      projectWikiView();await release();assert.equal(ui$wikiView.availability,availability);
      assert.equal(data.$wikiEdges,undefined);assert.strictEqual(geometry(),initial);assert.equal(layouts.length,2);
      assert.equal(syncs,1);assert.equal(schedules,0);assert.deepEqual(ui$transform,camera);assert.equal(snapshot(),before);
    """)


@pytest.mark.parametrize("hidden", ["list", "zero_width", "zero_height"])
def test_hidden_map_publication_never_builds_geometry_or_changes_camera(hidden: str) -> None:
    _run("const hidden=" + json.dumps(hidden) + ";\n" + r"""
      if(hidden==='list')ui$mode='list';
      if(hidden==='zero_width')dom$stage.clientWidth=0;
      if(hidden==='zero_height')dom$stage.clientHeight=0;
      const before=snapshot(),camera={...ui$transform};projectWikiView();await release();
      assert.deepEqual(data.$wikiEdges,[{source:'C',target:'D',count:1}]);
      assert.equal(layouts.length,0);assert.equal(scene,undefined);assert.deepEqual(ui$transform,camera);
      assert.equal(snapshot(),before);assert.strictEqual(document.activeElement,reader);
    """)


def test_root_region_owners_use_exact_canonical_ids_without_colliding_with_real_reserved_names() -> None:
    _run(r"""
      const rows=[root],nodes=[];for(let n=0;n<12;n++) {
        const id='topic-'+n,area=n%2?'Analysis':'Geometry';
        rows.push({id,title:id,parent:'roadmap',scope:id,kind:'scope',navigation:'module',result:false,status:'planned'});
        nodes.push({id:'scope:'+id,title:id,kind:'scope',area,status:'planned',status_counts:[['planned',1]],member_count:1});
      }
      for(const id of ['region:Geometry','view-region:0']) {
        rows.push({id,title:id,parent:'roadmap',scope:'roadmap',kind:'node',result:true,status:'planned'});
        nodes.push({id,title:id,kind:'node',status:'planned',status_counts:[['planned',1]],member_count:1});
      }
      records=new Map(rows.map(row=>[row.id,freeze(row)]));
      const payload=freeze({nodes,edges:[{source:'scope:topic-0',target:'scope:topic-1',statement:0,proof:1}],
        view:{presentation:'atlas'}}),before=JSON.stringify(payload),projected=regionProjection(payload,null);
      const geometry=projected.nodes.find(node=>node._region==='Geometry'),analysis=projected.nodes.find(node=>node._region==='Analysis');
      assert(geometry&&analysis);assert(!records.has(geometry.id));assert(!records.has(analysis.id));
      assert.equal(new Set(projected.nodes.map(node=>node.id)).size,projected.nodes.length);
      for(let n=0;n<12;n++)assert.equal(projected.$wikiOwners.get('topic-'+n),n%2?analysis.id:geometry.id);
      assert.equal(projected.$wikiOwners.get('region:Geometry'),'region:Geometry');
      assert.equal(projected.$wikiOwners.get('view-region:0'),'view-region:0');
      const state=layer([['topic-0','region:Geometry'],['region:Geometry','topic-1']]);
      const result=await actualProjector(projected,records,state,projected.$wikiOwners);
      assert.equal(result.availability,'complete');
      assert(result.edges.some(edge=>edge.source===geometry.id&&edge.target==='region:Geometry'&&edge.count===1));
      assert(result.edges.some(edge=>edge.source==='region:Geometry'&&edge.target===analysis.id&&edge.count===1));
      assert.equal(JSON.stringify(payload),before);
      assert.deepEqual(projected.edges,[{source:geometry.id,target:analysis.id,statement:0,proof:1}]);
    """)


_CANVAS_PEERS = r"""
class Canvas {
  constructor(){this.strokes=[];this.fills=[];this.path=[];this.dash=[];this.stack=[];}
  save(){this.stack.push({dash:this.dash.slice(),alpha:this.globalAlpha,color:this.strokeStyle,width:this.lineWidth});}
  restore(){const saved=this.stack.pop();this.dash=saved.dash;this.globalAlpha=saved.alpha;this.strokeStyle=saved.color;this.lineWidth=saved.width;}
  setTransform(){}clearRect(){}
  setLineDash(dash){this.dash=dash.slice();}
  beginPath(){this.path=[];}
  moveTo(x,y){this.path.push(['move',x,y]);}
  lineTo(x,y){this.path.push(['line',x,y]);}
  quadraticCurveTo(...args){this.path.push(['curve',...args]);}
  closePath(){this.path.push(['close']);}
  stroke(){this.strokes.push({path:this.path.slice(),dash:this.dash.slice(),alpha:this.globalAlpha,color:this.strokeStyle,width:this.lineWidth});}
  fill(){this.fills.push({path:this.path.slice(),alpha:this.globalAlpha,color:this.fillStyle});}
}
function isDark(){return false;}
"""

_DRAW_CALLBACKS = _CALLBACKS + (
    "draw", "drawRoute", "drawPill", "pill", "edgeRole", "edgeStyle", "atlasSpotlight", "spotlightOf",
    "scopeOverviewFlow", "rounded", "wikiOwnerVisible",
)
_DRAW_PEERS = _PEERS + _CANVAS_PEERS + r"""
const dom$ctx=new Canvas(),painted=[],flows=[];let overlayUpdates=0;var ui$hovered=null,host={classList:{toggle(){}}};
function calculateActive(){}function pathSpotlight(){return null;}function mapHeight(){return dom$stage.clientHeight;}
function drawPlate(){}function updateOverlays(){overlayUpdates++;}
const actualDrawRoute=drawRoute;drawRoute=function(ctx,route,...rest){
  painted.push({kind:route.kind,count:route.count});return actualDrawRoute(ctx,route,...rest);
};
const actualFlow=scopeOverviewFlow;scopeOverviewFlow=function(route,counts,range){
  assert.notEqual(route.kind,'wiki');flows.push({kind:route.kind,counts,range});return actualFlow(route,counts,range);
};
function showCurrentTestScene() {
  // Set only the canvas input from the actual bounded scene. This fixture
  // needs both islands on screen; anchor-pose tests keep their original camera.
  const scale=Math.min(1,(dom$stage.clientWidth-80)/scene.bounds.w,(dom$stage.clientHeight-80)/scene.bounds.h);
  ui$transform={scale,x:40-scene.bounds.x*scale,y:40-scene.bounds.y*scale};
}
"""


def test_wiki_painter_is_dotted_arrowless_and_never_creates_formal_direction_cues() -> None:
    peers = _CANVAS_PEERS + r"""
      var ui$transform={x:0,y:0,scale:1.25};const dom$stage={clientWidth:900,clientHeight:640};
    """
    _run(r"""
      const route=Object.freeze({kind:'wiki',strength:2,points:[{x:40,y:140},{x:450,y:140}],radii:[0,0]});
      const ctx=new Canvas();drawRoute(ctx,route,-1);
      assert.equal(ctx.strokes.length,1);assert.equal(ctx.fills.length,0);assert.equal(ctx.stack.length,0);
      assert.deepEqual(ctx.strokes[0].dash,[1.875,6.25]);assert.equal(ctx.strokes[0].alpha,.38);
      assert.equal(drawRoute.$flowSegments,undefined);assert(!ctx.strokes[0].path.some(part=>part[0]==='close'));
      const proof={...route,kind:'proof',arrow:{dx:1,dy:0}},formal=new Canvas(),before=JSON.stringify(proof);
      drawRoute(formal,proof,-1);assert.equal(formal.fills.length,1);assert.equal(formal.strokes.length,2);
      // A dependency link at rest is a full grey line in the redesign; only concept links stay faint.
      assert.deepEqual(formal.strokes[0].dash,[7.5,5]);assert.equal(formal.strokes[0].alpha,1);
      assert(drawRoute.$flowSegments instanceof WeakMap);assert.equal(JSON.stringify(proof),before);
      const cached=drawRoute.$flowSegments,second=new Canvas();drawRoute(second,route,0);
      assert.strictEqual(drawRoute.$flowSegments,cached);assert.equal(second.strokes.length,1);
      assert.equal(second.fills.length,0);assert.equal(second.strokes[0].alpha,.1);
    """, callbacks=("drawRoute", "edgeStyle"), peers=peers)


def test_actual_draw_keeps_wiki_corridors_outside_scoped_formal_flow_counts() -> None:
    _run(r"""
      geometry();projectWikiView();await release();showCurrentTestScene();
      const formalBefore=JSON.stringify(data.edges),wikiBefore=JSON.stringify(ui$wikiLinks),counts={statement:1,proof:1};
      data._scopeOverview={_flows:[counts],_flowRange:2};draw();
      assert.equal(scene.$wikiRoutes.routes.length,1);assert(painted.some(route=>route.kind==='wiki'));
      assert(painted.some(route=>route.kind==='proof'));assert(painted.some(route=>route.kind==='statement'));
      assert.equal(flows.length,scene.routes.length);assert(flows.every(flow=>flow.counts===counts&&flow.range===2));
      assert.equal(overlayUpdates,1);assert.equal(JSON.stringify(data.edges),formalBefore);assert.equal(JSON.stringify(ui$wikiLinks),wikiBefore);
      assert.strictEqual(document.activeElement,reader);assert.equal(reader.scrollTop,137);
    """, callbacks=_DRAW_CALLBACKS, peers=_DRAW_PEERS)


def test_actual_filtered_scope_guard_hides_only_association_corridors_and_halos_without_rescanning() -> None:
    _run(r"""
      const owner=freeze({...records.get('C'),kind:'scope',scope:'C',navigation:'module',result:false});
      const children=[freeze({id:'planned-child',title:'Planned child',parent:'C',scope:'C',kind:'node',status:'planned',result:true}),
        freeze({id:'proved-child',title:'Proved child',parent:'C',scope:'C',kind:'node',status:'fully_proved',result:true})];
      records.set('C',owner);children.forEach(row=>records.set(row.id,row));
      sharedIndex=freeze({...sharedIndex,nodes:Array.from(records.values())});
      const shown=byId.get('C');shown.kind='scope';shown.scope='C';
      shown.status_counts=[['planned',1],['fully_proved',1],['not_ready',0]];
      contextGraph=freeze({...contextGraph,nodes:data.nodes.map(node=>({...node}))});
      ui$wikiLinks=layer([['planned-child','D'],['proved-child','D']]);
      geometry();projectWikiView();await release();showCurrentTestScene();
      const cached=scene,layoutCount=layouts.length,projectorCount=projections.length;
      const before=snapshot(),facts=JSON.stringify(ui$wikiLinks),associations=data.$wikiEdges;
      assert.deepEqual(associations,[{source:'C',target:'D',count:2}]);draw();
      assert(painted.some(route=>route.kind==='wiki'&&route.count===2));
      assert.equal(dom$ctx.fills.filter(fill=>fill.alpha===.035).length,1);
      assert.equal(snapshot(),before);
      ui$selectedStatuses.delete('fully_proved');const filtered=snapshot();
      painted.length=0;dom$ctx.fills=[];dom$ctx.strokes=[];draw();
      assert(!painted.some(route=>route.kind==='wiki'));assert.equal(dom$ctx.fills.filter(fill=>fill.alpha===.035).length,0);
      assert(painted.some(route=>route.kind==='proof'));assert(painted.some(route=>route.kind==='statement'));
      assert.strictEqual(data.$wikiEdges,associations);assert.strictEqual(scene,cached);
      assert.equal(layouts.length,layoutCount);assert.equal(projections.length,projectorCount);
      assert.equal(JSON.stringify(ui$wikiLinks),facts);assert.equal(snapshot(),filtered);
      ui$selectedStatuses.add('fully_proved');ui$activeIds.delete('D');ui$active=ui$active.filter(node=>node.id!=='D');
      painted.length=0;dom$ctx.fills=[];draw();
      assert(!painted.some(route=>route.kind==='wiki'));assert.equal(dom$ctx.fills.filter(fill=>fill.alpha===.035).length,0);
      assert.equal(layouts.length,layoutCount);assert.equal(projections.length,projectorCount);
      assert.equal(JSON.stringify(ui$wikiLinks),facts);assert.strictEqual(document.activeElement,reader);
      assert.equal(reader.scrollTop,137);
    """, callbacks=_DRAW_CALLBACKS, peers=_DRAW_PEERS)
