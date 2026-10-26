"""Actual native-shelf callbacks with bounded DOM/history boundaries, not glyph evidence."""

from __future__ import annotations

import pytest

from tests.test_qa_outline_navigation import _run as _execute
from tests.test_wiki_workspace_controls import _CONTROL_PEERS, _NODE

pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for bounded shelf callback tests")

_CALLBACKS = [
    "element", "button", "readingLabel", "refreshReadingLabels", "safeHref", "graphAt", "recordId", "mapNode",
    "adoptGraph", "neighborIds", "neighborhood", "scopeOverviewProjection", "structuralScopeProjection",
    "expandedGroupShelves", "boundaryContextProjection",
    "regionProjection", "updateProjection", "currentOverviewGroup", "matchesStatus", "matchesIsolation", "calculateActive",
    "statusLabel", "swatch", "statusRing", "isDark", "renderShelves", "syncControls", "activateNode", "setMode", "refresh",
    "setSheet", "sheetLayout", "syncSheetAccessibility", "captureState", "snapshotForRoute", "restoreState", "sourceNodes", "inCurrentScope",
]

_PEERS = (
    _CONTROL_PEERS.replace("buildShell();bindEvents();syncControls();", "")
    .replace("function calculateActive(){}", "")
    .replace("function renderShelves(){}", "")
    .replace("function syncSheetAccessibility(){}", "")
    .replace("function setMode(mode,push)", "function unusedControlMode(mode,push)")
    .replace("function remember(){}", "function unusedControlRemember(){}")
    + r"""
// DOM querying/frame scheduling and external history/geometry are boundaries.
// Projection, ownership, shelf rendering, events, filtering and restoration
// are all verbatim production callbacks.
Element.prototype.querySelectorAll=function(tag){return this.all().filter(node=>node.tagName===tag.toUpperCase());};
Element.prototype.prepend=function(child){child.parentElement=this;this.children.unshift(child);};
const location=window.location,frames=[],checkpoints=[],writes=[],mathRoots=[];
window.matchMedia=()=>({matches:false});
window.AutoformMath={refresh(root){mathRoots.push(root);return Promise.resolve(true);}};
var atlas,palette,byId=new Map(),prerequisites,dependents,statusCounts;
var ui$active=[],ui$activeIds=new Set(),ui$activeDirty=true,ui$direction='both',ui$selected=null,ui$requestedNode='chapter';
var ui$query='',ui$relationIds=null,ui$listLimit=100,ui$outlineLimit=120,ui$scopeOverview=false,ui$overviewOnly=false,ui$filtersTouched=false;
var ui$spotlight=null,ui$transform={x:35,y:-20,scale:1.1},ui$reading=true;
var ui$wikiProjectionTicket=0,alive=true,restoring=false,restoration=0,checkpointTimer=0,firstResize=false;
var contextGraph,overlays=new Map(),readingReady=Promise.resolve(true);
navigation.checkpoint=()=>checkpoints.push(captureState());
navigation.navigateNode=(id,options)=>navigationCalls.push({id,options});
function remember(){checkpoints.push(captureState());}
function writeLocation(method){writes.push(method);}
function projectWikiView(){}function renderBreadcrumb(){}function renderInspector(){}function renderInventory(){}
function centerIfHidden(){}function focusNodeControl(){}
function requestAnimationFrame(callback){frames.push(callback);return frames.length;}
async function frame(){
  for(let attempt=0;attempt<30&&!frames.length;attempt++)await Promise.resolve();
  assert(frames.length,'The actual restoration must reach a frame');
  frames.splice(0).forEach(callback=>callback());
  for(let attempt=0;attempt<5;attempt++)await Promise.resolve();
}
function fixture(count=13){
  const root={id:'roadmap',scope:'roadmap',parent:null,kind:'scope',result:false,title:'Library',status:'planned'};
  const owner={id:'chapter',scope:'chapter',parent:root.id,kind:'scope',result:false,title:'Authored chapter',status:'planned',
    route:'https://example.test/project/roadmap/chapter/index.html'};
  const collection={id:'collection',scope:'collection',parent:owner.id,kind:'scope',result:false,navigation:'collection',
    title:'Collection $C$',status:'planned',reading_title:{format:'inline-parts-v1',source:'Collection $C$',
      parts:[['text','Collection '],['math','\\(C\\)']]}};
  const members=Array.from({length:count},(_,number)=>{
    const id=number===0?'R0"quoted':'R'+number,title='Result '+number+' $V_'+number+'$';
    return {id,parent:collection.id,scope:owner.id,kind:'node',result:true,title,_index:number,
      status:number%2?'fully_proved':'planned',route:owner.route+'#'+encodeURIComponent(id),
      reading_title:{format:'inline-parts-v1',source:title,parts:[['text','Result '+number+' '],['math','\\(V_'+number+'\\)']]}};
  });
  records.clear();[root,owner,collection,...members].forEach(node=>records.set(node.id,node));
  sharedIndex={nodes:[root,owner,collection,...members],relations:[]};
  const colors={planned:{label:'Planned',meaning:'Authored pending result',
      light:{fill:'#f0f0f0',stroke:'#777777'},dark:{fill:'#202020',stroke:'#aaaaaa'}},
    fully_proved:{label:'Fully proved',meaning:'Authored complete result',
      light:{fill:'#ddffdd',stroke:'#007700'},dark:{fill:'#113311',stroke:'#88ff88'}}};
  contextGraph={schema:'autoform-dag-view/v2',title:owner.title,palette:colors,
    nodes:members.map(node=>Object.assign({},node,{url:node.route,status_counts:[[node.status,1]],member_count:1})),
    edges:[],view:{kind:'chapter',presentation:'dag'}};
  data=contextGraph;palette=colors;view=contextGraph.view;ui$requestedNode=owner.id;
  homeURL='https://example.test/project/index.html';
  currentBundle={nodeId:owner.id,scope:owner.id,url:owner.route,title:owner.title,
    dataURL:'https://example.test/project/dependencies/chapter.json',graph:contextGraph,isCurrent:()=>true};
  buildShell();dom$body.offsetTop=0;dom$inventoryRows=new Map();
  [dom$readingBody,dom$navigation,dom$inventoryScroll,dom$shelves].forEach(pane=>pane.scrollTop=0);
  const reading=document.createElement('p');reading.textContent='Complete captured reading';dom$readingBody.appendChild(reading);
  updateProjection();syncControls();
  return {root,owner,collection,members,reading};
}
function links(){return dom$shelves.querySelectorAll('a');}
function section(){return dom$shelves.all().find(node=>node.className==='bp-scope-shelf');}
function allMemberIds(){return data.$shelves.flatMap(group=>group.members.map(node=>recordId(node)));}
"""
)


def _run(body: str) -> None:
    _execute(body, [*_CALLBACKS, "buildShell"], _PEERS)


def test_shelves_preserve_canonical_anchors_native_modifiers_and_typed_status_swatches() -> None:
    _run(r"""
      const state=fixture(),before=JSON.stringify({contextGraph,index:sharedIndex});
      assert.equal(data.nodes.length,0);assert.equal(dom$shelves.hidden,false);
      assert.deepEqual(allMemberIds(),state.members.map(node=>node.id));
      const visible=links();assert.equal(visible.length,5);
      for(let position=0;position<visible.length;position++){
        const link=visible[position],record=state.members[position],count=navigationCalls.length;
        assert.equal(link.href,record.route);assert.equal(link.dataset.shelfNodeId,record.id);
        const mark=link.all().find(node=>node.className==='bp-dag-swatch');assert(mark);
        assert.equal(mark.title,palette[record.status].meaning);
        for(const scheme of ['light','dark']){
          assert.equal(mark.style['--dag-swatch-'+scheme+'-fill'],palette[record.status][scheme].fill);
          assert.equal(mark.style['--dag-swatch-'+scheme+'-stroke'],palette[record.status][scheme].stroke);
        }
        for(const settings of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{button:2},{defaultPrevented:true}]){
          const event=emit(link,'click',Object.assign({button:0},settings));
          assert.equal(event.defaultPrevented,Boolean(settings.defaultPrevented));assert.equal(navigationCalls.length,count);
        }
        assert.equal(emit(link,'click',{button:0}).defaultPrevented,true);
        assert.deepEqual(navigationCalls.at(-1),{id:record.id,options:{history:'push',focus:'reading'}});
      }
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before);
      assert.strictEqual(dom$readingBody.children[0],state.reading);
    """)


def test_isolate_and_status_filters_hide_native_shelves_and_count_every_hidden_member() -> None:
    _run(r"""
      const state=fixture(),before=JSON.stringify({contextGraph,index:sharedIndex});
      assert.equal(dom$shelves.hidden,false);assert.equal(allMemberIds().length,13);
      ui$showIsolated=false;ui$activeDirty=true;syncControls();
      assert.equal(dom$shelves.hidden,true);assert.equal(links().length,0);
      assert.equal(dom$hiddenButton.textContent,'Show 13 hidden');
      ui$showIsolated=true;ui$selectedStatuses=new Set(['fully_proved']);ui$activeDirty=true;syncControls();
      assert.equal(dom$shelves.hidden,false);assert.equal(dom$hiddenButton.textContent,'Show 7 hidden');
      assert(links().every(link=>records.get(link.dataset.shelfNodeId).status==='fully_proved'));
      ui$selectedStatuses.clear();ui$activeDirty=true;syncControls();
      assert.equal(dom$shelves.hidden,true);assert.equal(dom$hiddenButton.textContent,'Show 13 hidden');
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before);
    """)


def test_expanded_owner_fallback_cannot_reveal_isolates_hidden_by_the_same_member_filter() -> None:
    _run(r"""
      const state=fixture(43);
      contextGraph.nodes.forEach((node,number)=>{
        node.area=number<13?'First area':'Second area';
        node.status=number>=2&&number<13?'fully_proved':'planned';
        node.status_counts=[[node.status,1]];node.isolated=number>=2;
        records.get(node.id).status=node.status;
      });
      contextGraph.edges=[{source:state.members[0].id,target:state.members[1].id,statement:1,proof:0}];
      ui$overviewGroup='area:First area';ui$requestedNode=null;updateProjection();
      assert.equal(data.$shelves[0].members.length,11);assert.equal(currentOverviewGroup().$shelf,false);
      const before=JSON.stringify({contextGraph,index:sharedIndex,members:currentOverviewGroup().members});
      ui$selectedStatuses=new Set(['fully_proved']);ui$showIsolated=false;ui$activeDirty=true;syncControls();
      assert.equal(ui$active.length,0);assert.equal(dom$shelves.hidden,true);assert.equal(links().length,0);
      assert.equal(dom$hiddenButton.textContent,'Show 14 hidden');
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex,members:currentOverviewGroup().members}),before);
      ui$showIsolated=true;ui$activeDirty=true;syncControls();
      assert.equal(dom$shelves.hidden,false);assert.equal(links().length,11);
      assert.equal(dom$hiddenButton.textContent,'Show 3 hidden');
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex,members:currentOverviewGroup().members}),before);
    """)


def test_external_connected_power_series_shelf_topic_keeps_its_canonical_link_and_true_isolation_flag() -> None:
    _run(r"""
      const state=fixture(13),first=contextGraph.nodes[0],canonical='Algebra/WantedExt/power-series';
      const power={...records.get(first.id),id:canonical,title:'Power series',kind:'scope',scope:canonical,result:false,status:'can_prove',
        route:'https://example.test/project/roadmap/Algebra/WantedExt/power-series/index.html'};
      records.delete(first.id);records.set(canonical,power);
      contextGraph.nodes[0]={...power,id:'scope:'+canonical,_recordId:canonical,url:power.route,isolated:false,
        status_counts:[['fully_proved',6],['can_prove',17]],member_count:23};
      contextGraph.palette.can_prove={...contextGraph.palette.planned,label:'Ready to prove'};
      const external={id:'RingTheory',title:'Ring Theory',kind:'scope',scope:'RingTheory',parent:'roadmap',status:'planned',result:false,
        route:'https://example.test/project/roadmap/RingTheory/index.html'};records.set(external.id,external);
      contextGraph.nodes.push({...external,id:'boundary:RingTheory',kind:'boundary',status_counts:[['planned',1]],member_count:1,isolated:false});
      const edge={source:'boundary:RingTheory',target:'scope:'+canonical,statement:7,proof:0};
      contextGraph.edges=[edge];sharedIndex={...sharedIndex,nodes:Array.from(records.values()),relations:[['RingTheory',canonical,7,0]]};
      updateProjection();syncControls();assert.equal(data.nodes.length,0);
      const before=JSON.stringify({contextGraph,index:sharedIndex}),originalShelf=data.$shelves[0],cacheLinks=links();
      assert(originalShelf.members.some(node=>node.id==='scope:'+canonical));
      ui$showIsolated=false;ui$activeDirty=true;syncControls();
      assert.equal(dom$shelves.hidden,false);assert.equal(links().length,1);
      assert.equal(links()[0].dataset.shelfNodeId,canonical);assert.equal(links()[0].href,power.route);
      assert.equal(dom$hiddenButton.textContent,'Show 12 hidden');
      assert.equal(contextGraph.nodes[0].isolated,false);assert.strictEqual(contextGraph.edges[0],edge);
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before);
      assert.deepEqual(data.$shelves[0].members,originalShelf.members);
      const visible=links()[0];renderShelves();renderShelves();assert.strictEqual(links()[0],visible);
      ui$showIsolated=true;ui$activeDirty=true;syncControls();
      assert.equal(dom$hiddenButton.hidden,true);assert.equal(links().length,cacheLinks.length);
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before);
    """)


def test_missing_isolation_flags_use_one_cached_scan_of_actual_typed_endpoints_including_external_edges() -> None:
    _run(r"""
      const state=fixture(13),first=contextGraph.nodes[0],last=contextGraph.nodes.at(-1);
      const actual=[{source:'boundary:captured-input',target:first.id,statement:7,proof:0},
        {source:'not-an-edge',target:last.id,statement:0,proof:0}];contextGraph={...contextGraph,edges:actual};
      let scans=0,rows=0;const forEach=actual.forEach;
      actual.forEach=function(callback,...args){scans++;return forEach.call(this,edge=>{rows++;return callback(edge);},...args);};
      const before=JSON.stringify({contextGraph,index:sharedIndex});ui$showIsolated=false;
      for(let repeat=0;repeat<5;repeat++){
        assert(matchesIsolation(first));assert(!matchesIsolation(last));assert(!matchesIsolation(contextGraph.nodes[1]));
      }
      assert.equal(scans,1);assert.equal(rows,2);assert.strictEqual(matchesIsolation.$cache.graph,contextGraph);
      assert(matchesIsolation({...first,isolated:false}));assert(!matchesIsolation({...first,isolated:true}));assert.equal(scans,1);
      ui$selected=last;assert(matchesIsolation(last));assert.equal(scans,1);ui$selected=null;
      const cached=matchesIsolation.$cache;
      contextGraph={...contextGraph,edges:[{source:last.id,target:first.id,statement:0,proof:3}]};
      assert(matchesIsolation(last));assert.notStrictEqual(matchesIsolation.$cache,cached);
      assert.strictEqual(matchesIsolation.$cache.graph,contextGraph);
      assert.equal(JSON.stringify(sharedIndex),JSON.stringify(JSON.parse(before).index));
      assert.deepEqual(actual.map(edge=>[edge.source,edge.target,edge.statement,edge.proof]),[
        ['boundary:captured-input',first.id,7,0],['not-an-edge',last.id,0,0]]);
    """)


def test_collapsed_connected_development_is_not_an_isolate_when_its_typed_edges_are_private() -> None:
    _run(r"""
      const state=fixture(129);
      contextGraph.edges=state.members.slice(1).map((member,index)=>({source:state.members[index].id,target:member.id,statement:1,proof:0}));
      updateProjection();syncControls();const before=JSON.stringify({contextGraph,index:sharedIndex});
      const group=data._scopeOverview.groups[0],proxy=data.nodes[0];
      assert.equal(data.nodes.length,1);assert.equal(group.members.length,129);assert.equal(group.edges.length,128);
      assert.equal(proxy.internal_dependency_count,128);
      ui$showIsolated=false;ui$activeDirty=true;syncControls();
      assert(matchesIsolation(proxy),'A derived proxy cannot reinterpret a connected original development as an isolate');
      assert.equal(ui$active.length,1);assert.equal(dom$empty.hidden,true);assert.equal(dom$hiddenButton.hidden,true);
      assert(group.edges.every((edge,index)=>edge===contextGraph.edges[index]));
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before);
    """)


def test_collapsed_group_status_and_isolation_must_match_the_same_original_member() -> None:
    _run(r"""
      const state=fixture(43);
      contextGraph.nodes.forEach((node,number)=>{
        node.area=number<13?'First area':'Second area';
        node.status=number>=2&&number<13?'fully_proved':'planned';
        node.status_counts=[[node.status,1]];node.isolated=number>=2;
        records.get(node.id).status=node.status;
      });
      contextGraph.edges=[{source:state.members[0].id,target:state.members[1].id,statement:1,proof:0}];
      ui$overviewGroup=null;ui$requestedNode=null;updateProjection();
      const group=data._scopeOverview.groups.find(group=>group.area==='First area'),proxy=data.nodes.find(node=>node.id===group.id);
      const before=JSON.stringify({contextGraph,index:sharedIndex,members:group.members});
      ui$selectedStatuses=new Set(['fully_proved']);ui$showIsolated=false;ui$activeDirty=true;syncControls();
      assert(matchesStatus(proxy),'The fixture must contain actual fully proved contributors');
      assert(group.members.some(node=>!node.isolated)&&group.members.some(node=>node.status==='fully_proved'));
      assert.equal(ui$activeIds.has(proxy.id),false,'Different contributors cannot independently satisfy the two filters');
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex,members:group.members}),before);
    """)


def test_shelf_cache_retains_link_focus_scroll_and_all_underlying_members() -> None:
    _run(r"""
      const state=fixture(),before=JSON.stringify({contextGraph,index:sharedIndex});
      const link=links()[0],container=section();link.focus({});dom$shelves.scrollTop=71;
      renderShelves();renderShelves();
      assert.strictEqual(links()[0],link);assert.strictEqual(section(),container);
      assert.strictEqual(document.activeElement,link);assert.equal(dom$shelves.scrollTop,71);
      assert.deepEqual(allMemberIds(),state.members.map(node=>node.id));
      ui$selectedStatuses=new Set(['fully_proved']);renderShelves();
      assert(links().every(link=>records.get(link.dataset.shelfNodeId).status==='fully_proved'));
      assert.deepEqual(allMemberIds(),state.members.map(node=>node.id),'A visual filter cannot remove source membership');
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before);
    """)


def test_authored_collection_math_parts_remain_safe_in_group_breadcrumb_and_map_heading() -> None:
    peers=_PEERS.replace("function renderBreadcrumb(){}", "").replace("function trailList() {return new Element('ol');}", "")
    callbacks=[*_CALLBACKS,"buildShell","semanticParent","routeTrail","trailList","renderBreadcrumb"]
    _execute(r"""
      const state=fixture(13),title='Collection $C$ and `x<y`';
      state.collection.title=title;state.collection.reading_title={format:'inline-parts-v1',source:title,
        parts:[['text','Collection '],['math','\\(C\\)'],['text',' and '],['code','x<y']]};
      const before=JSON.stringify({contextGraph,index:sharedIndex});updateProjection();syncControls();
      const group=data.$shelves[0];activateNode({_overviewGroup:group.key});
      assert.equal(currentOverviewGroup().title,title);
      assert.strictEqual(currentOverviewGroup().reading_title,state.collection.reading_title);
      const crumb=dom$breadcrumb.children[0].children.at(-1);
      assert.equal(crumb.getAttribute('aria-current'),'page');
      for(const root of [crumb,dom$mapContext]) {
        const math=root.all().filter(node=>node.className==='arithmatex');assert.equal(math.length,1);
        assert.equal(math[0].textContent,'\\(C\\)');
        const code=root.all().filter(node=>node.tagName==='CODE');assert.equal(code.length,1);assert.equal(code[0].textContent,'x<y');
        assert.equal(root.all().some(node=>['SCRIPT','IMG','IFRAME'].includes(node.tagName)),false);
      }
      assert.equal(crumb.all().some(node=>node.tagName==='A'),false,'The current group label is not a fabricated article link');
      assert(mathRoots.includes(dom$breadcrumb));assert(mathRoots.includes(dom$mapContext));
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before);
      assert.strictEqual(dom$readingBody.children[0],state.reading);
    """,callbacks,peers)


def test_focal_definition_heading_keeps_authored_math_title_and_actual_neighbour_counts() -> None:
    _run(r"""
      const state=fixture(13),definition=state.members[0];
      definition.title='Definition $D$';definition.reading_title={format:'inline-parts-v1',source:definition.title,
        parts:[['text','Definition '],['math','\\(D\\)']]};
      sharedIndex.relations=[[state.members[1].id,definition.id,1,0],[state.members[2].id,definition.id,0,2],
        [definition.id,state.members[3].id,1,1]];
      currentBundle=Object.assign({},currentBundle,{nodeId:definition.id,title:'Generic view label'});
      const before=JSON.stringify(sharedIndex);syncControls();
      assert(dom$mapContext.textContent.startsWith('Definition '),'A definition must be identified by its authored title');
      const math=dom$mapContext.all().filter(node=>node.className==='arithmatex');assert.equal(math.length,1);
      assert.equal(math[0].textContent,'\\(D\\)');
      assert.deepEqual(dom$mapContext.textContent.match(/\b\d+\b/g),['2','1']);
      assert.equal(JSON.stringify(sharedIndex),before);
    """)


@pytest.mark.parametrize("kind", ["source", "module"])
def test_source_or_module_scope_activation_keeps_reading_intent_without_forcing_map_first(kind: str) -> None:
    _execute("const kind="+repr(kind)+";\n"+r"""
      const state=fixture(13),record={id:'reading-'+kind,scope:'reading-'+kind,parent:state.owner.id,kind:'scope',
        title:kind,result:false,status:'planned',route:state.owner.route+'#'+kind};
      if(kind==='source') record.navigation='source';else record.catalog='module';
      records.set(record.id,record);activateNode(record);
      assert.deepEqual(navigationCalls.at(-1),{id:record.id,options:{history:'push',focus:'reading'}});
      const topic={id:'ordinary-topic',scope:'ordinary-topic',parent:state.owner.id,kind:'scope',result:false,status:'planned'};
      records.set(topic.id,topic);activateNode(topic);
      assert.deepEqual(navigationCalls.at(-1),{id:topic.id,options:{history:'push',focus:'reading',mapFirst:true}});
    """,[*_CALLBACKS,"buildShell"],_PEERS)


def test_empty_map_with_real_shelves_is_not_shown_as_an_empty_roadmap() -> None:
    _run(r"""
      const state=fixture();assert.equal(data.nodes.length,0);assert.equal(state.members.length,13);
      assert.equal(dom$shelves.hidden,false);assert.equal(dom$stage.hidden,true);
      assert(host.classList.contains('bp-dag-shelves-only'));
      assert(links().length>0,'The complete scope must still expose native reading links');
      assert(dom$empty.hidden||dom$stage.hidden,'An empty map notice must not be visible over a nonempty scope shelf');
      assert.deepEqual(allMemberIds(),state.members.map(node=>node.id));
    """)


def test_fully_filtered_context_drill_keeps_mixed_scope_members_and_actual_status_ring_counts() -> None:
    _run(r"""
      const state=fixture(13);
      for(let number=0;number<3;number++){
        const record=state.members[number],node=contextGraph.nodes[number];
        const counts=number<2?[['planned',6],['fully_proved',2]]:[['planned',8]];
        Object.assign(record,{kind:'scope',scope:record.id,result:false,status:'planned',status_counts:counts});
        Object.assign(node,{kind:'boundary',_recordId:record.id,scope:record.id,result:false,status:'planned',status_counts:counts.map(pair=>pair.slice())});
      }
      const edges=[{source:state.members[0].id,target:state.members[3].id,statement:1,proof:2},
        {source:state.members[1].id,target:state.members[3].id,statement:2,proof:0},
        {source:state.members[4].id,target:state.members[2].id,statement:0,proof:3}];
      contextGraph.edges=edges;sharedIndex.relations=edges.map(edge=>[edge.source,edge.target,edge.statement,edge.proof]);
      const before=JSON.stringify({contextGraph,index:sharedIndex});
      ui$overviewGroup=null;ui$requestedNode='chapter';updateProjection();syncControls();
      const needed=data._scopeOverview.groups.find(group=>group.$contextRole==='needed');assert(needed);
      activateNode({_overviewGroup:needed.key});
      ui$filtersTouched=true;ui$selectedStatuses=new Set(['fully_proved']);ui$activeDirty=true;syncControls();
      assert.equal(matchesStatus(state.members[0]),true,'An ordinary scope can contain proved results without a proved dominant status');
      assert.equal(matchesStatus(contextGraph.nodes[0]),true,'An ordinary boundary must match its actual counted statuses');
      assert.equal(matchesStatus(contextGraph.nodes[2]),false);
      assert.deepEqual(links().map(link=>link.dataset.shelfNodeId),state.members.slice(0,2).map(node=>node.id));
      for(const link of links()){
        const mark=link.all().find(node=>node.className==='bp-dag-swatch');assert(mark);
        assert(mark.style.background.startsWith('conic-gradient('));
        assert(mark.style.background.includes(palette.planned.light.stroke));
        assert(mark.style.background.includes(palette.fully_proved.light.stroke));
        assert(mark.title.includes('6 '+statusLabel('planned')));assert(mark.title.includes('2 '+statusLabel('fully_proved')));
        const title=link.all().find(node=>node.className==='bp-scope-shelf-title');assert(title);
        assert(title.all().some(node=>node.className==='arithmatex'),'Mixed-status shelf labels retain their publisher-marked title parts');
      }
      assert.deepEqual(currentOverviewGroup().members.map(node=>recordId(node)),state.members.slice(0,2).map(node=>node.id));
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before,'Filtering and status-ring rendering cannot rewrite source counts or weights');
    """)


def test_drilled_authored_area_keeps_linked_cards_and_full_provenance_while_showing_all_local_isolates() -> None:
    _run(r"""
      const state=fixture(41);
      state.members.forEach((node,number)=>node.area=number<15?'Authored area A':'Authored area B');
      contextGraph.nodes.forEach((node,number)=>node.area=state.members[number].area);
      const edges=[{source:state.members[0].id,target:state.members[1].id,statement:2,proof:3},
        {source:state.members[2].id,target:state.members[15].id,statement:1,proof:0}];
      contextGraph.edges=edges;sharedIndex.relations=edges.map(edge=>[edge.source,edge.target,edge.statement,edge.proof]);
      const before=JSON.stringify({contextGraph,index:sharedIndex});
      ui$overviewGroup='area:Authored area A';ui$requestedNode=null;updateProjection();syncControls();
      const group=currentOverviewGroup();assert(group);
      assert.deepEqual(group.members.map(node=>recordId(node)),state.members.slice(0,15).map(node=>node.id));
      assert.deepEqual(data.$shelves.flatMap(shelf=>shelf.members.map(node=>recordId(node))),
        state.members.slice(2,15).map(node=>node.id));
      assert.deepEqual(data.nodes.filter(node=>!node._overviewGroup).map(node=>recordId(node)),
        state.members.slice(0,2).map(node=>node.id));
      assert(data.edges.some(edge=>edge.source===edges[0].source&&edge.target===edges[0].target&&edge.statement===2&&edge.proof===3));
      assert(group.edges.some(edge=>edge.source===edges[1].source&&edge.target===edges[1].target&&edge.statement===1&&edge.proof===0),
        'Moving an externally related local isolate to a shelf must retain its original typed-edge provenance');
      assert.equal(dom$stage.hidden,false,'Locally connected cards must remain on the mathematical map');
      assert.equal(host.classList.contains('bp-dag-shelves-only'),false);
      assert.equal(dom$shelves.hidden,false);assert.equal(links().length,13,
        'Selected-area native shelves use the full 100-entry page, not another five-link preview');
      assert.deepEqual(links().map(link=>link.dataset.shelfNodeId),state.members.slice(2,15).map(node=>node.id));
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before);
      assert.strictEqual(dom$readingBody.children[0],state.reading);
    """)


def test_filtered_area_drill_uses_native_members_instead_of_an_unrelated_active_sibling_card() -> None:
    _run(r"""
      const state=fixture(41);
      state.members.forEach((record,number)=>{
        record.area=number<15?'Authored area A':'Authored area B';
        if(number<2) record.status='fully_proved';
        Object.assign(contextGraph.nodes[number],{area:record.area,status:record.status,status_counts:[[record.status,1]]});
      });
      contextGraph.edges=[{source:state.members[0].id,target:state.members[1].id,statement:1,proof:2}];
      sharedIndex.relations=contextGraph.edges.map(edge=>[edge.source,edge.target,edge.statement,edge.proof]);
      const before=JSON.stringify({contextGraph,index:sharedIndex});
      ui$overviewGroup='area:Authored area A';ui$requestedNode=null;updateProjection();
      ui$filtersTouched=true;ui$selectedStatuses=new Set(['planned']);ui$activeDirty=true;syncControls();
      const group=currentOverviewGroup(),memberIds=new Set(group.members.map(node=>node.id));
      assert(ui$active.length>0,'The unrelated sibling area remains active for this regression');
      assert(ui$active.every(node=>!memberIds.has(node.id)));
      assert.equal(dom$stage.hidden,true,'Filtered-away local cards must not leave the sibling map framed as this drill');
      assert(host.classList.contains('bp-dag-shelves-only'));
      assert(dom$shelves.children.some(node=>node.tagName==='BUTTON'),'A native filtered drill retains its local Back action');
      const expected=state.members.slice(0,15).filter(node=>node.status==='planned').map(node=>node.id);
      assert.deepEqual(links().map(link=>link.dataset.shelfNodeId),expected);
      assert.deepEqual(group.members.map(node=>recordId(node)),state.members.slice(0,15).map(node=>node.id));
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before);
    """)


def test_native_group_paging_back_and_restoration_keep_quoted_link_focus_scroll_and_camera() -> None:
    _run(r"""
      const state=fixture(105),before=JSON.stringify({contextGraph,index:sharedIndex});
      const preview=section().all().find(node=>node.tagName==='BUTTON');assert(preview);
      dom$shelves.scrollTop=37;emit(preview,'click');
      const groupKey=ui$overviewGroup;assert(groupKey);assert.equal(ui$requestedNode,null);
      assert.equal(links().length,100);assert.strictEqual(document.activeElement,links()[0]);
      const more=section().all().find(node=>node.tagName==='BUTTON');assert(more);emit(more,'click');
      assert.equal(ui$listLimit,200);assert.equal(links().length,105);
      assert.deepEqual(links().map(link=>link.dataset.shelfNodeId),state.members.map(node=>node.id));
      const chosen=links().find(link=>link.dataset.shelfNodeId===state.members[0].id);chosen.focus({});
      dom$shelves.scrollTop=221;dom$readingBody.scrollTop=53;dom$navigation.scrollTop=19;dom$inventoryScroll.scrollTop=7;
      ui$transform={x:88,y:-42,scale:1.25};const saved=captureState(),savedBefore=JSON.stringify(saved);
      assert.equal(saved.focus,state.members[0].id);assert.equal(saved.shelfScroll,221);assert.equal(saved.overviewGroup,groupKey);
      const back=dom$shelves.children.find(node=>node.tagName==='BUTTON');assert(back);emit(back,'click');
      assert.equal(ui$overviewGroup,null);assert.equal(links().length,5);assert.equal(writes.at(-1),'push');
      dom$search.focus({});dom$shelves.scrollTop=0;ui$transform={x:1,y:2,scale:0.9};
      const restored=restoreState(saved,currentBundle);await frame();await restored;
      assert.equal(ui$overviewGroup,groupKey);assert.equal(ui$listLimit,200);assert.equal(links().length,105);
      const focused=links().find(link=>link.dataset.shelfNodeId===saved.focus);
      assert.strictEqual(document.activeElement,focused);assert.equal(focused.focuses.at(-1).preventScroll,true);
      assert.equal(dom$shelves.scrollTop,221);assert.equal(dom$readingBody.scrollTop,53);
      assert.equal(dom$navigation.scrollTop,19);assert.equal(dom$inventoryScroll.scrollTop,7);
      assert.deepEqual(ui$transform,saved.camera);assert.equal(restoring,false);
      assert.equal(JSON.stringify(saved),savedBefore);assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before);
      assert.strictEqual(dom$readingBody.children[0],state.reading);
      assert.deepEqual(allMemberIds(),state.members.map(node=>node.id));
    """)
