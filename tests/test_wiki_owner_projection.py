"""Actual pure wiki owner projection with bounded source VM fixtures."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated wiki owner projection tests")

_LOADER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(CLIENT,'utf8');
function original(name) {
  const start=source.indexOf('function '+name+'(');assert(start>=0,'Missing production helper: '+name);
  for(let end=source.indexOf('}',start);end>=0;end=source.indexOf('}',end+1)) {
    const declaration=source.slice(start,end+1);
    try {new vm.Script('('+declaration+')');return declaration;}
    catch(error) {if(!(error instanceof SyntaxError))throw error;}
  }
  assert.fail('Unparseable production helper: '+name);
}
const constants=source.match(/\bvar NODE_W =[^;]+;/);assert(constants);
const context={assert,setTimeout,setImmediate};vm.createContext(context);
const production=constants[0]+'\n'+original('projectWikiLinks');
"""

_PEERS = r"""
function record(id,parent='root',extra={}) {
  return Object.assign({id,parent,scope:id,kind:'scope',title:'Uninterpreted title '+id,status:'planned'},extra);
}
function index(...nodes) {return new Map([record('root',null),...nodes].map(node=>[node.id,node]));}
function layer(references,availability='complete',unavailable_sources=[]) {
  return {schema:'autoform-wiki-links/v1',index_sha256:'d'.repeat(64),availability,references,unavailable_sources};
}
function view(nodes,extra={}) {
  return Object.assign({nodes,edges:[{source:'A',target:'B',statement:2,proof:3}],
    view:{presentation:'atlas',book:'https://example.test/book',breadcrumbs:[]}},extra);
}
function frozen(value) {
  if(value&&typeof value==='object') {Object.values(value).forEach(frozen);Object.freeze(value);}return value;
}
function snapshot(graph,records,state,endpoints) {
  return JSON.stringify({graph,records:Array.from(records),state,endpoints:endpoints&&Array.from(endpoints)});
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
"""


def _run(body: str, *, peers: str = _PEERS) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    program = peers + "\n(async()=>{\n" + body + "\n})()"
    script += "const deadline=setTimeout(()=>{console.error('Owner projection did not settle');process.exitCode=1;},5000);\n"
    script += "Promise.resolve(vm.runInContext(production+" + json.dumps(program)
    script += ",context,{timeout:2000})).then(()=>clearTimeout(deadline),error=>{clearTimeout(deadline);console.error(error);process.exitCode=1;});\n"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def test_nearest_actual_parent_owners_deduplicate_directed_pairs_and_keep_all_inputs_immutable() -> None:
    _run(r"""
      const records=index(record('A'),record('B'),record('inner','A'),record('a1','inner'),record('a2','A'),
        record('b1','B'),record('external'));
      const graph=frozen(view([records.get('A'),records.get('inner'),records.get('B')]));
      const state=frozen(layer([['a1','b1'],['a1','b1'],['a2','b1'],['b1','a1'],['a1','a2'],
        ['a1','external'],['a1','not-indexed']]));
      Array.from(records.values()).forEach(frozen);const before=snapshot(graph,records,state);
      const result=await projectWikiLinks(graph,records,state);
      assert.deepEqual(result,{availability:'complete',edges:[
        {source:'A',target:'B',count:1},{source:'B',target:'inner',count:1},
        {source:'inner',target:'A',count:1},{source:'inner',target:'B',count:1}
      ],unavailableOwners:[]});
      assert.equal(snapshot(graph,records,state),before);assert.equal(graph.edges[0].statement,2);assert.equal(graph.edges[0].proof,3);
      assert(result.edges.every(edge=>!('statement'in edge)&&!('proof'in edge)));
    """)


def test_collapsed_overview_uses_exact_group_members_and_expansion_restores_leaf_owners() -> None:
    _run(r"""
      const records=index(record('A'),record('B'),record('a1','A',{kind:'node'}),record('a2','A',{kind:'node'}));
      const group={id:'view-group:7',key:'area:Authored A',members:[records.get('a1'),records.get('a2')]};
      const state=frozen(layer([['a1','B'],['a1','B'],['a2','B'],['B','a1'],['a1','a2']]));
      const metadata={_scopeOverview:{groups:[group],selectedGroup:null}};
      const collapsed=frozen(view([{id:group.id,_overviewGroup:group.key,_region:'Authored A'},records.get('B')],metadata));
      const before=snapshot(collapsed,records,state);
      assert.deepEqual(await projectWikiLinks(collapsed,records,state),{availability:'complete',edges:[
        {source:'B',target:group.id,count:1},{source:group.id,target:'B',count:2}
      ],unavailableOwners:[]});
      assert.equal(snapshot(collapsed,records,state),before);
      const expanded=view([records.get('a1'),records.get('a2'),records.get('B')],
        {_scopeOverview:{groups:[group],selectedGroup:group.key}});
      assert.deepEqual((await projectWikiLinks(expanded,records,state)).edges,[
        {source:'B',target:'a1',count:1},{source:'a1',target:'B',count:1},
        {source:'a1',target:'a2',count:1},{source:'a2',target:'B',count:1}
      ]);
    """)


def test_regions_require_explicit_canonical_replacements_and_never_catch_unrepresented_same_area_records() -> None:
    _run(r"""
      const records=index(record('A','root',{area:'Authored A'}),record('B','root',{area:'Authored B'}),
        record('a','A',{kind:'node',area:'Authored A'}),record('b','B',{kind:'node'}),
        record('external','root',{area:'Authored A'}),record('region:A'));
      const graph=frozen(view([{id:'region:A',kind:'scope',_region:'Authored A'},
        {id:'region:B',kind:'scope',_region:'Authored B'}]));
      const state=frozen(layer([['a','b'],['external','b'],['region:A','b']]));
      assert.deepEqual((await projectWikiLinks(graph,records,state)).edges,[]);
      const endpoints=new Map([['A','region:A'],['B','region:B']]),before=snapshot(graph,records,state,endpoints);
      assert.deepEqual(await projectWikiLinks(graph,records,state,endpoints),{availability:'complete',
        edges:[{source:'region:A',target:'region:B',count:1}],unavailableOwners:[]});
      assert.equal(snapshot(graph,records,state,endpoints),before);
    """)


@pytest.mark.parametrize("prefix", ["scope", "boundary"])
def test_literal_prefixed_canonical_identity_and_record_id_are_not_display_aliases(prefix: str) -> None:
    _run("const prefix=" + json.dumps(prefix) + ";\n" + r"""
      const literal=prefix+':authored',records=index(record('authored'),record(literal),record('B'));
      const state=layer([[literal,'B'],['authored','B']]);
      assert.deepEqual((await projectWikiLinks(view([records.get(literal),records.get('B')]),records,state)).edges,
        [{source:literal,target:'B',count:1}]);
      const explicit={id:'display:literal',kind:prefix,_recordId:literal};
      assert.deepEqual((await projectWikiLinks(view([explicit,records.get('B')]),records,state)).edges,
        [{source:explicit.id,target:'B',count:1}]);
      const alias={id:literal,kind:prefix};
      assert.deepEqual((await projectWikiLinks(view([alias,records.get('B')]),records,state)).edges,
        [{source:literal,target:'B',count:1}]);
      const literalOnly=layer([[literal,'B']]);
      assert.deepEqual((await projectWikiLinks(view([alias,records.get('B')]),records,literalOnly)).edges,[]);
    """)


def test_reserved_group_ids_are_never_treated_as_canonical_owners() -> None:
    _run(r"""
      const records=index(record('A'),record('B'),record('a','A',{kind:'node'}),record('view-group:0'));
      const group={id:'view-group:0',key:'area:Authored A',members:[records.get('a')]};
      const graph=view([{id:group.id,_overviewGroup:group.key},records.get('B')],{_scopeOverview:{groups:[group]}});
      assert.deepEqual((await projectWikiLinks(graph,records,layer([['a','B'],['view-group:0','B']]))).edges,
        [{source:group.id,target:'B',count:1}]);
    """)


def test_missing_cyclic_and_source_ancestry_fail_closed_while_structural_wrappers_are_walked() -> None:
    _run(r"""
      const records=index(record('A'),record('B'),record('collection','A',{navigation:'collection'}),
        record('transparent','collection',{navigation:'transparent'}),record('a','transparent',{kind:'node'}),
        record('source','A',{navigation:'source'}),record('hidden','source',{kind:'node'}),
        record('missing','not-indexed',{kind:'node'}),record('x','y'),record('y','x'),record('cycle','x',{kind:'node'}),
        record('self','self',{kind:'node'}));
      const graph=view([records.get('A'),records.get('B'),records.get('hidden')]);
      const state=layer([['a','B'],['collection','B'],['transparent','B'],['source','B'],['hidden','B'],
        ['missing','B'],['cycle','B'],['self','B'],['B','hidden']]);
      assert.deepEqual((await projectWikiLinks(graph,records,state)).edges,[{source:'A',target:'B',count:1}]);
      const withSource=view([records.get('A'),records.get('B'),records.get('source')]);
      assert.deepEqual((await projectWikiLinks(withSource,records,layer([['a','B']]),
        new Map([['A','source']]))).edges,[],'A replacement cannot turn a source wrapper into a displayed concept endpoint');
    """)


def test_partial_coverage_keeps_known_pairs_and_maps_missing_sources_to_their_nearest_owners() -> None:
    _run(r"""
      const records=index(record('A'),record('B'),record('a','A',{kind:'node'}),record('external'));
      const graph=view([records.get('A'),records.get('B')]);
      const state=frozen(layer([['a','B'],['a','B'],['B','a']],'partial',['a','a','external','not-indexed']));
      assert.deepEqual(await projectWikiLinks(graph,records,state),{availability:'partial',
        edges:[{source:'A',target:'B',count:1},{source:'B',target:'A',count:1}],unavailableOwners:['A']});
    """)


def test_unavailable_stale_or_invalid_layers_do_not_project_references_or_copy_wire_reasons() -> None:
    _run(r"""
      const records=index(record('A'),record('B')),graph=view([records.get('A'),records.get('B')]);
      const state={...layer([['A','B']],'unavailable',['A']),reason:'stale_index:/private/provider/state'};
      const before=snapshot(graph,records,state),empty={availability:'unavailable',edges:[],unavailableOwners:[]};
      assert.deepEqual(await projectWikiLinks(graph,records,state),empty);assert.equal(snapshot(graph,records,state),before);
      assert.deepEqual(await projectWikiLinks(graph,records,{...layer([['A','B']]),schema:'wrong'}),empty);
      assert.deepEqual(await projectWikiLinks(graph,records,layer([['A','B']],'complete',['A'])),empty);
      assert.deepEqual(await projectWikiLinks(graph,records,null),empty);
    """)


def test_bounded_view_invalid_replacements_and_ambiguous_display_ids_never_add_nodes() -> None:
    _run(r"""
      const all=Array.from({length:MAX_MAP_NODES+1},(_,n)=>record('n'+n)),records=index(...all);
      const state=layer([['n0','n1']]);
      assert.deepEqual(await projectWikiLinks(view(all),records,state),{availability:'unavailable',edges:[],unavailableOwners:[]});
      const graph=view([records.get('n0'),records.get('n1')]);
      assert.deepEqual((await projectWikiLinks(graph,records,state,new Map([['n0','unrepresented']]))).edges,[]);
      assert.deepEqual((await projectWikiLinks(view([records.get('n0'),records.get('n0')]),records,state)).edges,[]);
      assert.equal(graph.nodes.length,2);assert.equal(records.size,MAX_MAP_NODES+2);
    """)


def test_output_is_invariant_under_record_view_reference_and_mapping_insertion_order() -> None:
    _run(r"""
      const records=index(record('A'),record('B'),record('a','A'),record('b','B'));
      const graph=view([records.get('A'),records.get('B')]),refs=[['a','b'],['b','a'],['a','b']];
      const mappings=new Map([['a','A'],['b','B']]);
      const normal=await projectWikiLinks(graph,records,layer(refs,'partial',['b','a']),mappings);
      const reversed=await projectWikiLinks(view(graph.nodes.slice().reverse()),new Map(Array.from(records).reverse()),
        layer(refs.slice().reverse(),'partial',['a','b']),new Map(Array.from(mappings).reverse()));
      assert.deepEqual(reversed,normal);assert.deepEqual(normal.unavailableOwners,['A','B']);
    """)


def test_large_reference_scan_yields_before_completion_and_returns_one_atomic_bounded_result() -> None:
    peers = _PEERS + r"""
      const scheduled=[];let yields=0;
      function setTimeout(callback,delay) {assert.equal(delay,0);scheduled.push(callback);yields++;}
      async function nextChunk() {const callback=scheduled.shift();assert(callback);callback();await flush();}
    """
    _run(r"""
      const records=index(record('A'),record('B')),graph=frozen(view([records.get('A'),records.get('B')]));
      const state=frozen(layer(Array.from({length:2048},()=>['A','B']),'partial',['A']));
      const before=snapshot(graph,records,state);let settled=false,result;
      const pending=projectWikiLinks(graph,records,state).then(value=>{settled=true;result=value;});
      await flush();assert.equal(settled,false);assert.equal(yields,1);assert.equal(scheduled.length,1);
      await nextChunk();assert.equal(settled,false);assert.equal(yields,2);assert.equal(scheduled.length,1);
      await nextChunk();await pending;
      assert.deepEqual(result,{availability:'partial',edges:[{source:'A',target:'B',count:1}],unavailableOwners:['A']});
      assert.equal(scheduled.length,0);assert.equal(snapshot(graph,records,state),before);
    """, peers=peers)


def test_large_unrepresented_scan_yields_without_retaining_global_pair_copies() -> None:
    peers = _PEERS + r"""
      const scheduled=[];
      function setTimeout(callback,delay) {assert.equal(delay,0);scheduled.push(callback);}
      async function nextChunk() {const callback=scheduled.shift();assert(callback);callback();await flush();}
    """
    _run(r"""
      const outside=Array.from({length:2049},(_,n)=>record('outside-'+n));
      const records=index(record('B'),...outside),graph=frozen(view([records.get('B')]));
      const state=frozen(layer(outside.map(node=>[node.id,'B']))),before=snapshot(graph,records,state);
      // Constructor peers measure retained containers, without rewriting the
      // helper or any Map/Set prototype. The source/index stays native.
      const NativeMap=Map,NativeSet=Set,known=new NativeSet(records.keys()),maps=[];let noncanonicalSetAdds=0;
      globalThis.Map=class extends NativeMap {constructor(...args){super(...args);maps.push(this);}};
      globalThis.Set=class extends NativeSet {
        add(value){if(typeof value==='string'&&!known.has(value))noncanonicalSetAdds++;return super.add(value);}
      };
      let settled=false;const pending=projectWikiLinks(graph,records,state).then(result=>{settled=true;return result;});
      await flush();assert.equal(settled,false);assert.equal(scheduled.length,1);
      await nextChunk();assert.equal(settled,false);assert.equal(scheduled.length,1);
      await nextChunk();assert.deepEqual(await pending,{availability:'complete',edges:[],unavailableOwners:[]});
      assert.equal(noncanonicalSetAdds,0,'Unrepresented pairs must not become serialized global Set keys');
      const retainedTargets=maps.reduce((sum,map)=>sum+Array.from(map.values()).reduce((n,value)=>
        n+(value instanceof NativeSet?value.size:0),0),0);
      assert.equal(retainedTargets,0,'No dedup targets should be retained when no reference contributes to the view');
      assert.equal(scheduled.length,0);assert.equal(snapshot(graph,records,state),before);
    """, peers=peers)
