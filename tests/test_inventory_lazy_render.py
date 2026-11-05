"""Actual inventory callbacks with isolated DOM/observer peers, not native UI proof."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated inventory callbacks")

_LOADER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(CLIENT,'utf8');
function original(name) {
  const start=source.indexOf('function '+name+'(');assert(start>=0,'Missing actual callback: '+name);
  for(let end=source.indexOf('}',start);end>=0;end=source.indexOf('}',end+1)) {
    const declaration=source.slice(start,end+1);
    try {new vm.Script('('+declaration+')');return declaration;}
    catch(error) {if(!(error instanceof SyntaxError))throw error;}
  }
  assert.fail('Unparseable actual callback: '+name);
}
const names=['element','readingLabel','refreshReadingLabels','button','bindNodeLink','statusLabel','searchScore','rankMatches','inventoryNodes','inCurrentScope','sourceNodes','renderInventory',
  'setMode','refresh','snapshotForRoute','selection','restoreState','writeLocation','renderReaderContext','topicPeers','semanticParent',
  'placeOf','recordId','pendingMath','summaryLine','searchMatches','adoptIndex','useIndex'];
const constants=['LIST_PAGE','plural','num','pressed'].map(name=>{
  const start=source.indexOf('  var '+name+' =');assert(start>=0);
  const declaration=source.slice(start,source.indexOf('\n',start));new vm.Script(declaration);return declaration;
}).join('\n');
const context={assert,setImmediate,setTimeout,clearTimeout};vm.createContext(context);
const production=constants+'\n'+names.map(original).join('\n');
"""

_PEERS = r"""
const effects=[],created=[];
// These peers record DOM boundaries. Selection, paging, filtering and entry
// callbacks use the production source, including the real inventory builder.
function peer(tag='div') {
  const attrs=new Map(),classes=new Set();let text='';
  const node={tagName:tag.toUpperCase(),dataset:{},children:[],listeners:new Map(),scrollTop:0,writes:0,
    classList:{toggle(name,on){if(on)classes.add(name);else classes.delete(name);},contains(name){return classes.has(name);}},
    setAttribute(name,value){attrs.set(name,String(value));},getAttribute(name){return attrs.get(name)||null;},removeAttribute(name){attrs.delete(name);},
    addEventListener(type,callback){this.listeners.set(type,callback);},
    appendChild(child){this.children.push(child);return child;},replaceChildren(...children){this.children=children;},
    all(){return this.children.flatMap(child=>[child,...child.all()]);},
    querySelector(selector){return this.all().find(child=>child.tagName===selector.toUpperCase())||null;},
    querySelectorAll(selector){return this.all().filter(child=>child.tagName===selector.toUpperCase());},
    scrollIntoView(options){effects.push({kind:'scroll',id:this.dataset.inventoryNodeId,options});dom$inventoryScroll.scrollTop=999;},
    focus(options){document.activeElement=this;effects.push({kind:'focus',id:this.dataset.inventoryNodeId,
      rows:dom$inventoryRows.size,scroll:dom$inventoryScroll.scrollTop,options});}};
  Object.defineProperty(node,'textContent',{get(){return text+this.children.map(child=>child.textContent).join('');},
    set(value){text=String(value);this.children=[];this.writes++;}});created.push(node);return node;
}
const document={activeElement:null,createElement:peer,createTextNode(value){const node=peer('#text');node.textContent=value;return node;}};
const window={};
const host=peer(),dom$inventory=peer('section'),dom$inventoryList=peer('ul'),dom$inventorySummary=peer('span');
dom$inventory.appendChild(peer('h2'));
const dom$loadMore=peer('button'),dom$inventoryScroll=peer(),dom$wholeSearch=peer('button'),dom$hiddenButton=peer('button');
const dom$readingContext=peer(),dom$readingNeighbors=peer(),dom$readingBody=peer('article'),dom$navigation=peer('nav'),dom$search=peer('input');
var dom$inventoryRows=new Map(),dom$outlineToggle=null;
const results=Array.from({length:150},(_,n)=>({id:'result-'+String(n).padStart(3,'0'),title:'Result '+n,
  summary:'Source statement '+n,status:n%2?'fully_proved':'planned',result:true,scope:'roadmap',parent:'roadmap',
  route:'https://example.test/roadmap/result-'+n+'.html',_index:n}));
const indexNodes=results.concat([{id:'module',title:'Module article',catalog:'module',result:false,status:'inventory_pending',scope:'roadmap'},
  ...[0,1].map(n=>({id:'source-'+n,title:'Source '+n,result:false,navigation:'source',status:'planned',scope:'source-'+n})),
  {id:'note',title:'Narrative note',result:false,status:'planned',scope:'roadmap'},
  {id:'roadmap',title:'Library',result:false,status:'planned',scope:'roadmap',route:'https://example.test/roadmap/index.html'}]);
var sharedIndex={nodes:indexNodes,relations:results.slice(1,8).map(node=>[node.id,results[0].id,2,3])};
var records=new Map(indexNodes.map(node=>[node.id,node]));const byId=new Map(records);
var currentBundle={nodeId:'roadmap',scope:'roadmap'},ui$mode='graph',ui$query='',ui$relationIds=null,ui$listLimit=LIST_PAGE;
var ui$selectedStatuses=new Set(['planned','fully_proved','inventory_pending']),ui$selected=null;
var ui$overviewOnly=false,ui$requestedNode=null;
var ui$filtersTouched=false,ui$outlineLimit=120,ui$searchNodes=indexNodes,ui$searchCache=[],ui$searchCacheQuery=null;
var alive=true,restoration=0,restoring=false,checkpointTimer,readingReady=Promise.resolve();
const presentStatuses=['planned','fully_proved','inventory_pending'],tracksLean=true;
const palette={planned:{label:'planned'},fully_proved:{label:'fully proved'},inventory_pending:{label:'inventory pending'}};
const navigation={checkpoint(method){effects.push({kind:'checkpoint',method});},navigateNode(){assert.fail('A list entry changed the canonical route');}};
function syncControls(){dom$inventory.hidden=ui$mode==='graph';}
function renderInspector(){}function sheetLayout(){return false;}function setSheet(){}function refreshReadingLabels(){}function typesetRowsInView(){}var instance=1;
function updateProjection(){}function renderNavigation(){}function resize(){}function requestWikiLinks(){}
// The page's own payload stands in for the list only until the index loads.
Object.defineProperty(globalThis,'data',{get(){assert.fail('An available complete index was replaced by the page payload');}});
function activateNode(node){effects.push({kind:'activate',node});}
function requestAnimationFrame(callback){return setImmediate(callback);}
function rowIds(){return Array.from(dom$inventoryRows.keys());}
"""


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    script += "const running=vm.runInContext(production+" + json.dumps(_PEERS)
    script += "+'\\n(async()=>{'+" + json.dumps(body) + "+'\\n})()',context,{timeout:2000});\n"
    script += "Promise.resolve(running).catch(error=>{console.error(error);process.exitCode=1;});\n"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def test_graph_mode_skips_index_access_rows_and_hidden_dom_mutation() -> None:
    _run(r"""
      const sentinel=peer('li');dom$inventoryList.appendChild(sentinel);dom$inventorySummary.textContent='Retained hidden list';
      const oldRows=dom$inventoryRows,oldWrites=dom$inventoryList.writes,oldCreated=created.length;
      dom$inventoryScroll.scrollTop=47;
      Object.defineProperty(sharedIndex,'nodes',{get(){assert.fail('Graph mode scanned the complete inventory');}});
      renderInventory(true);renderInventory(false);refresh(true);refresh(false);
      assert.equal(created.length,oldCreated);assert.equal(dom$inventoryList.writes,oldWrites);
      assert.strictEqual(dom$inventoryRows,oldRows);assert.deepEqual(dom$inventoryList.children,[sentinel]);
      assert.equal(dom$inventorySummary.textContent,'Retained hidden list');assert.equal(dom$inventoryScroll.scrollTop,47);
      assert.equal(ui$mode,'graph');assert.equal(dom$inventory.hidden,true);
    """)


def test_results_and_sources_entry_run_the_real_builder_and_keep_full_paging() -> None:
    _run(r"""
      const before=JSON.stringify(indexNodes);setMode('list',true);
      assert.equal(dom$inventory.hidden,false);assert.equal(dom$inventoryRows.size,100);
      assert.equal(dom$inventorySummary.textContent,'100 of 151');assert.equal(dom$loadMore.hidden,false);
      assert(!rowIds().some(id=>id.startsWith('source-')||id==='note'||id==='roadmap'));
      ui$listLimit+=LIST_PAGE;renderInventory(false);
      assert.equal(dom$inventoryRows.size,151);assert(rowIds().includes('result-149'));assert(rowIds().includes('module'));
      assert.equal(dom$inventorySummary.textContent,'151 articles');assert.equal(dom$loadMore.hidden,true);
      dom$inventoryRows.get('result-149').listeners.get('click')();
      assert.strictEqual(effects.at(-1).node,records.get('result-149'));
      setMode('graph',true);const oldCreated=created.length;refresh(true);assert.equal(created.length,oldCreated);
      setMode('sources',true);
      assert.deepEqual(rowIds(),['source-0','source-1']);assert.equal(dom$inventorySummary.textContent,'2 sources');
      assert.equal(dom$inventory.hidden,false);assert.equal(JSON.stringify(indexNodes),before);
    """)


def test_show_all_related_enters_list_with_every_original_result() -> None:
    # The redesign shows a result's relations once, in its own dependency disclosure, so no reader strip offers "Show all N";
    # a related list restored from history still enters the list with every original result, built only then.
    _run(r"""
      currentBundle={nodeId:results[0].id,scope:'roadmap'};ui$selected=results[0];
      assert.equal(dom$inventoryRows.size,0);ui$relationIds=results.slice(1,8).map(node=>node.id);setMode('list',true);
      assert.equal(ui$mode,'list');assert.equal(dom$inventory.hidden,false);
      assert.deepEqual(rowIds(),results.slice(1,8).map(node=>node.id));assert.equal(dom$inventoryRows.size,7);
      assert.equal(dom$inventorySummary.textContent,'7 statements');assert.equal(dom$loadMore.hidden,true);
      assert.deepEqual(ui$relationIds,results.slice(1,8).map(node=>node.id));
    """)


def test_saved_results_page_builds_before_restoring_scroll_and_owned_focus() -> None:
    _run(r"""
      const before=JSON.stringify(indexNodes),saved={mode:'list',listLimit:200,node:'result-130',focus:'result-130',
        statuses:presentStatuses,inventoryScroll:1800,outlineScroll:37,readingScroll:71,readingOpen:false,
        camera:{x:12,y:24,scale:1.2}};
      await restoreState(saved,{isCurrent:()=>true});
      assert.equal(ui$mode,'list');assert.equal(ui$listLimit,200);assert.equal(dom$inventoryRows.size,151);
      assert(rowIds().includes('result-130'));assert.equal(dom$inventoryScroll.scrollTop,1800);
      assert.equal(dom$navigation.scrollTop,37);assert.equal(dom$readingBody.scrollTop,71);
      assert.equal(restoring,false);
      const focused=effects.find(event=>event.kind==='focus');
      assert.deepEqual(focused,{kind:'focus',id:'result-130',rows:151,scroll:1800,options:{preventScroll:true}});
      assert.strictEqual(document.activeElement,dom$inventoryRows.get('result-130'));
      refresh(true);assert.equal(ui$listLimit,100);assert.equal(dom$inventoryRows.size,100);assert.equal(dom$inventoryScroll.scrollTop,0);
      assert.equal(JSON.stringify(indexNodes),before);
    """)


def test_deferred_inventory_still_applies_query_and_status_without_truncating_the_index() -> None:
    _run(r"""
      const before=JSON.stringify(indexNodes);ui$query='Result 130';refresh(true);
      assert.equal(dom$inventoryRows.size,0);setMode('list',true);
      assert.deepEqual(rowIds(),['result-130']);assert.equal(dom$inventorySummary.textContent,'1 statement');
      ui$query='';ui$selectedStatuses=new Set(['fully_proved']);refresh(true);
      assert.equal(dom$inventoryRows.size,75);assert.equal(dom$inventorySummary.textContent,'75 statements');
      assert(rowIds().every(id=>records.get(id).status==='fully_proved'));
      assert.equal(sharedIndex.nodes.length,155);assert.equal(JSON.stringify(indexNodes),before);
    """)


def test_show_filtered_out_counts_what_the_status_filter_hides_from_the_browse_list() -> None:
    _run(r"""
      const before=JSON.stringify(indexNodes);ui$selectedStatuses=new Set(['fully_proved']);setMode('list',true);
      // The list holds 150 results and a module article: the 75 proved results show; 75 planned ones and the article do not.
      assert.equal(dom$inventorySummary.textContent,'75 statements');
      assert.equal(dom$hiddenButton.textContent,'Show 76 filtered out');assert.equal(dom$hiddenButton.hidden,false);
      // A search counts only its own matches: result-140 to result-149, five of them planned.
      ui$query='result-14';refresh(true);
      assert.deepEqual(rowIds(),['result-141','result-143','result-145','result-147','result-149']);
      assert.equal(dom$hiddenButton.textContent,'Show 5 filtered out');
      ui$query='';setMode('sources',true);
      assert.deepEqual(rowIds(),[]);assert.equal(dom$hiddenButton.textContent,'Show 2 filtered out');
      ui$selectedStatuses=new Set(presentStatuses);refresh(true);
      assert.deepEqual(rowIds(),['source-0','source-1']);assert.equal(dom$hiddenButton.hidden,true);
      assert.equal(JSON.stringify(indexNodes),before);
    """)


def test_typing_on_ranks_only_the_last_matches_and_every_rank_and_order_stay_the_same() -> None:
    _run(r"""
      // The ranks tie, and the last query's order is not the index order, so a narrowed rank must sort again.
      const rows=[{id:'capri',title:'Capri numbers',summary:'A prime-like sequence.'},
        {id:'zeckendorf',title:'Zeckendorf representation',summary:'Every positive integer is a sum of Fibonacci numbers.'},
        {id:'root',title:'Primitive root',lean:'ZMod.IsPrimitiveRoot',summary:'A generator of the units mod n.'},
        {id:'prime',title:'Prime numbers',lean:'Nat.Prime',summary:'There are infinitely many.'},{id:'pri',title:'Pri',lean:'Nu.lemma'},
        {id:'odos',title:'ΟΔΟΣ'},{id:'odosa',title:'ΟΔΟΣΑ'},{id:'kent',title:'İstanbul'},
        {id:'zeta',title:'Riemann zeta',summary:'Zeckendorf repr. of nothing'}].map(row=>({status:'planned',scope:'roadmap',...row}));
      useIndex({nodes:rows,relations:[]});
      const real=searchScore,keys=matches=>matches.map(match=>match.node.id+':'+match.rank);let scored=0;
      searchScore=function(node,query){scored++;return real(node,query);};
      // Every keystroke ranks as a full rank of every row does; one that extends the last query scores only its matches.
      function type(value,narrows) {
        const last=ui$searchCache.length;ui$query=value.trim();scored=0;const matches=searchMatches(),count=scored;
        assert.deepEqual(keys(matches),keys(rankMatches(ui$searchNodes,ui$query.toLocaleLowerCase())),value);
        assert.equal(count,narrows?last:ui$searchNodes.length,value);return keys(matches);
      }
      assert.deepEqual(type('p',false),['root:4','prime:4','pri:4','capri:8','zeckendorf:8','zeta:11']);
      type('pr',true);assert.deepEqual(type('pri',true),['pri:1','root:4','prime:4','capri:8']);
      // A second word: the one-word query's matches still hold every match.
      assert.deepEqual(type('pri n',true),['capri:8','root:12','prime:12','pri:12']);
      assert.deepEqual(type('pri nu',true),['capri:8','prime:12','pri:12']);
      // Backspace and paste rank every row again.
      type('pri n',false);assert.deepEqual(type('zeckendorf',false),['zeckendorf:2','zeta:11']);type('zeckendorf r',true);
      // A new index starts over, even for a query that extends the last one.
      useIndex({nodes:rows.slice(1).concat([{id:'reps',title:'Zeckendorf representations',status:'planned',scope:'roadmap'}])});
      assert.deepEqual(type('zeckendorf re',false),['zeckendorf:4','reps:4','zeta:11']);
      // Lowercasing is toLocaleLowerCase's: a final capital sigma becomes ς, so ΟΔΟΣΑ does not extend ΟΔΟΣ, and İ holds an i.
      type('Ο',false);type('ΟΔ',true);type('ΟΔΟ',true);assert.deepEqual(type('ΟΔΟΣ',true),['odos:1']);
      assert.deepEqual(type('ΟΔΟΣΑ',false),['odosa:1']);assert.deepEqual(type('σ',false),['odosa:8']);
      assert(type('i',false).includes('kent:4'));
      // The cache holds every match, not the twelve shown, so a query with more matches narrows exactly too.
      useIndex({nodes:Array.from({length:30},(_,n)=>({id:'result-'+n,title:'Result '+n,status:'planned',scope:'roadmap'}))});
      assert.equal(type('result',false).length,30);assert.equal(type('result 1',true).length,12);
      // A one-word query joins no row's fields.
      const join=Array.prototype.join;let joined=0;
      Array.prototype.join=function(...args){joined++;return join.apply(this,args);};
      try {rankMatches(ui$searchNodes,'zeta');assert.equal(joined,0);rankMatches(ui$searchNodes,'pri nu');assert(joined>0);}
      finally {Array.prototype.join=join;}
    """)
