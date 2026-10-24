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
const names=['element','button','recordId','semanticParent','topicPeers','neighborIds','neighborhood',
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


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    script += "vm.runInContext(production+" + json.dumps(_PEERS + "\n" + body) + ",context,{timeout:2000});\n"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


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
