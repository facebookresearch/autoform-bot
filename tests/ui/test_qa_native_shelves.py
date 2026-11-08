"""Actual status-swatch, status-filter and activation callbacks with bounded DOM/history boundaries, not glyph evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_qa_outline_navigation import _run as _execute
from tests.test_wiki_workspace_controls import _CONTROL_PEERS, _NODE

pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for bounded swatch and activation callback tests")

_CALLBACKS = [
    "element", "button", "readingLabel", "refreshReadingLabels", "safeHref", "graphAt", "recordId", "listed",
    "adoptGraph", "selection", "updateProjection",
    "statusLabel", "swatch", "solid", "syncControls", "activateNode", "setMode", "refresh",
    "setSheet", "sheetLayout", "syncSheetAccessibility", "plainTitle", "captureState", "snapshotForRoute", "restoreState", "sourceNodes", "inCurrentScope",
]

_PEERS = (
    _CONTROL_PEERS.replace("buildShell();bindEvents();syncControls();", "")
    .replace("function syncSheetAccessibility(){}", "")
    .replace("function setMode(mode,push)", "function unusedControlMode(mode,push)")
    .replace("function remember(){}", "function unusedControlRemember(){}")
    + r"""
// DOM querying/frame scheduling and external history are boundaries.
// Ownership, swatches, events, filtering and restoration are all verbatim
// production callbacks.
Element.prototype.querySelectorAll=function(tag){return this.all().filter(node=>node.tagName===tag.toUpperCase());};
Element.prototype.prepend=function(child){child.parentElement=this;this.children.unshift(child);};
const location=window.location,frames=[],checkpoints=[],writes=[],mathRoots=[];
window.matchMedia=()=>({matches:false});
window.AutoformMath={refresh(root){mathRoots.push(root);return Promise.resolve(true);}};
var palette,byId=new Map(),statusCounts;
var ui$selected=null,ui$requestedNode='chapter';
var ui$query='',ui$relationIds=null,ui$listLimit=100,ui$outlineLimit=120,ui$overviewOnly=false,ui$filtersTouched=false;
var ui$reading=true;
var alive=true,restoring=false,restoration=0,checkpointTimer=0;
var contextGraph,readingReady=Promise.resolve(true);
navigation.checkpoint=()=>checkpoints.push(captureState());
navigation.navigateNode=(id,options)=>navigationCalls.push({id,options});
function remember(){checkpoints.push(captureState());}
function writeLocation(method){writes.push(method);}
function renderBreadcrumb(){}function renderInspector(){}function renderInventory(){}
function requestAnimationFrame(callback){frames.push(callback);return frames.length;}
async function frame(){
  for(let attempt=0;attempt<30&&!frames.length;attempt++)await Promise.resolve();
  assert(frames.length,'The actual restoration must reach a frame');
  frames.splice(0).forEach(callback=>callback());
  for(let attempt=0;attempt<5;attempt++)await Promise.resolve();
}
function fixture(count=13,start=true){
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
  data=contextGraph;palette=colors;ui$requestedNode=owner.id;
  homeURL='https://example.test/project/index.html';
  currentBundle={nodeId:owner.id,scope:owner.id,url:owner.route,title:owner.title,
    dataURL:'https://example.test/project/dependencies/chapter.json',graph:contextGraph,isCurrent:()=>true};
  buildShell();dom$inventoryRows=new Map();
  dom$stage.clientWidth=900;dom$stage.clientHeight=700;dom$inspector.clientHeight=700;
  [dom$readingBody,dom$navigation,dom$inventoryScroll].forEach(pane=>pane.scrollTop=0);
  const reading=document.createElement('p');reading.textContent='Complete captured reading';dom$readingBody.appendChild(reading);
  updateProjection();if(start)syncControls();
  return {root,owner,collection,members,reading};
}
"""
    # The explorer's bundle carries the reducer that reads a title's formulas as text for the reading pane's button.
    + "(function (window) {\n"
    + (Path(__file__).resolve().parents[1] / "autoform_cli/assets/tex-plain.js").read_text(encoding="utf-8")
    + "\n})(window);\n"
)


def _run(body: str) -> None:
    _execute(body, [*_CALLBACKS, "buildShell"], _PEERS)


def test_status_swatches_name_their_meaning_and_carry_both_schemes_colours() -> None:
    _run(r"""
      const state=fixture(),before=JSON.stringify({contextGraph,index:sharedIndex});
      for(const record of state.members.slice(0,5)){
        const mark=swatch(record.status);
        assert.equal(mark.className,'bp-dag-swatch');assert.equal(mark.title,palette[record.status].meaning);
        for(const scheme of ['light','dark']){
          // A finished state's small mark is filled with its stroke; an outlined one keeps its fill.
          assert.equal(mark.style['--dag-swatch-'+scheme+'-fill'],solid(record.status,palette[record.status][scheme]));
          assert.equal(mark.style['--dag-swatch-'+scheme+'-stroke'],palette[record.status][scheme].stroke);
        }
      }
      assert.equal(JSON.stringify({contextGraph,index:sharedIndex}),before);
      assert.strictEqual(dom$readingBody.children[0],state.reading);
    """)


def test_a_status_change_on_the_map_keeps_the_selection_and_says_the_filter() -> None:
    _execute(r"""
      // The shell adopts its page before building, so the status fields have their counts.
      statusCounts=new Map();const state=fixture(),selected=state.members[1];
      ui$requestedNode=selected.id;currentBundle.nodeId=selected.id;updateProjection();setSheet(1,ui$selected);
      assert.equal(selected.status,'fully_proved');assert.equal(dom$hiddenButton.hidden,true,'The map offers nothing to show back');
      const proved=dom$statusInputs.get('fully_proved');proved.checked=false;emit(proved,'change');
      // The map dims the statuses the filter leaves out, so the selection and its reader stay.
      assert.equal(ui$selected?.id,selected.id);
      assert(host.classList.contains('bp-dag-has-selection'));assert(host.classList.contains('bp-dag-sheet-open'));
      assert.equal(host.dataset.mapStatuses,'planned');assert.equal(dom$hiddenButton.hidden,true);
      assert.equal(dom$filterSummary.textContent,'Filter: 1 of 2 statuses');
      assert.equal(dom$announcement.textContent,'Filter: 1 of 2 statuses');assert.deepEqual(writes,['replace']);
      // Adopting the page again, as the index's arrival does, keeps it too.
      updateProjection();assert.equal(ui$selected?.id,selected.id);assert.deepEqual([...ui$selectedStatuses],['planned']);
    """, [*_CALLBACKS, "buildShell", "renderStatusFields", "onStatusChange", "announceShown"],
        _PEERS.replace("function renderStatusFields(){}", ""))


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


def test_a_link_naming_absent_statuses_counts_only_the_present_ones_it_shows() -> None:
    _run(r"""
      fixture();assert.deepEqual(presentStatuses.slice().sort(),['fully_proved','planned']);
      // Home's "Ready to work on" link asks for statuses this page may not have.
      ui$selectedStatuses=new Set(['planned','fully_proved','can_prove']);syncControls();
      assert.equal(dom$filterSummary.textContent,'Filter: all statuses');assert.equal(host.dataset.mapStatuses,undefined);
      ui$selectedStatuses=new Set(['planned','can_prove']);syncControls();
      assert.equal(dom$filterSummary.textContent,'Filter: 1 of 2 statuses');assert.equal(host.dataset.mapStatuses,'planned');
    """)

