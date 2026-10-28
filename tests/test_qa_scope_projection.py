"""Real structural projection; source VM peers do not certify native shelves or geometry."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_wiki_owner_projection import _LOADER

_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for actual structural projection tests")

_PEERS = r"""
function record(id,parent='current',extra={}) {
  return Object.assign({id,parent,scope:id,kind:'scope',title:'Authored '+id,status:'planned',
    status_counts:[['planned',1]],member_count:1,route:'https://example.test/'+encodeURIComponent(id)+'.html'},extra);
}
function fixture(count) {
  const records=new Map([record('roadmap',null),record('current','roadmap'),
    record('C1','current',{navigation:'collection',title:'First authored collection'}),
    record('C2','current',{navigation:'collection',title:'Second authored collection'})].map(node=>[node.id,node]));
  const nodes=Array.from({length:count},(_,n)=>{
    const source=record('n'+n,n<count/2?'C1':'C2',{status:n%3?'planned':'fully_proved',
      status_counts:[[n%3?'planned':'fully_proved',1]],summary:'Complete authored summary '+n});records.set(source.id,source);
    return Object.assign({},source,{id:'scope:'+source.id,kind:'scope',layout:{width:208,height:100}});
  });
  return {records,payload:{schema:'autoform-dag-view/v2',title:'Actual scope',nodes,edges:[],
    palette:{planned:{},fully_proved:{}},view:{presentation:'dag',book:'https://example.test/roadmap/index.html'}}};
}
function edge(source,target,statement=2,proof=3) {return {source,target,statement,proof};}
function boundary(f,n) {
  const source=record('outside-'+n,'roadmap');f.records.set(source.id,source);
  const node=Object.assign({},source,{id:'boundary:'+source.id,kind:'boundary'});f.payload.nodes.push(node);return node;
}
function snapshot(f) {return JSON.stringify({payload:f.payload,records:Array.from(f.records)});}
function freeze(value) {if(value&&typeof value==='object'){Object.values(value).forEach(freeze);Object.freeze(value);}return value;}
function conservation(f,result) {
  const groups=result._scopeOverview.groups;
  const ids=groups.flatMap(group=>group.members.map(node=>node.id));
  result.nodes.filter(node=>!node._overviewGroup&&node.kind==='boundary').forEach(node=>ids.push(node.id));
  assert.deepEqual(ids.slice().sort(),f.payload.nodes.map(node=>node.id).sort());
  const originals=result.edges.flatMap(edge=>edge._overviewEdges).concat(groups.flatMap(group=>group.edges));
  assert.equal(originals.length,f.payload.edges.length);assert.equal(new Set(originals).size,originals.length);
  f.payload.edges.forEach(edge=>assert(originals.includes(edge),'Every original typed edge must retain its identity exactly once'));
  result.edges.forEach(edge=>{
    assert.equal(edge.statement,edge._overviewEdges.reduce((sum,original)=>sum+original.statement,0));
    assert.equal(edge.proof,edge._overviewEdges.reduce((sum,original)=>sum+original.proof,0));
  });
  assert(result.nodes.length<=MAX_MAP_NODES);assert(result.nodes.every(node=>!node._overviewGroup||!('area'in node)&&!('_region'in node)));
  assert.equal(result._scopeOverview.originalNodes,f.payload.nodes.length);
  result.$shelves.forEach(group=>{assert(groups.includes(group));assert.equal(group.$shelf,true);assert(!result.nodes.some(node=>node.id===group.id));});
}
"""


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    script += "\nvm.runInContext(production+'\\n'+original('structuralScopeProjection')+"
    script += json.dumps(_PEERS + "\n" + body) + ",context,{timeout:2000});\n"
    result = subprocess.run([_NODE], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def test_small_and_fully_linked_bounded_scopes_keep_the_original_view_exactly() -> None:
    _run(r"""
      const small=fixture(12),full=fixture(33);
      for(let n=1;n<33;n++)full.payload.edges.push(edge('scope:n'+(n-1),'scope:n'+n));
      for(const f of [small,full]) {const before=snapshot(f);assert.equal(structuralScopeProjection(f.payload,f.records,'current'),null);assert.equal(snapshot(f),before);}
    """)


def test_algebra_shaped_scope_keeps_small_real_developments_and_shelves_all_local_isolates() -> None:
    _run(r"""
      const f=fixture(33);f.payload.edges=[edge('scope:n0','scope:n1'),edge('scope:n1','scope:n2',5,7),
        edge('scope:n3','scope:n4'),edge('scope:n4','scope:n5')];
      for(let n=0;n<7;n++){const node=boundary(f,n);f.payload.edges.push(edge(node.id,'scope:n'+(n%6)));}
      f.payload.edges.push(edge('scope:n32','boundary:outside-0',0,11));
      const before=snapshot(f);freeze(f.payload);Array.from(f.records.values()).forEach(freeze);
      const result=structuralScopeProjection(f.payload,f.records,'current');assert(result);conservation(f,result);
      assert.deepEqual(result.nodes.filter(node=>node.kind!=='boundary').map(node=>node.id).sort(),
        ['scope:n0','scope:n1','scope:n2','scope:n3','scope:n4','scope:n5']);
      assert.equal(result.nodes.filter(node=>node.kind==='boundary').length,7);
      assert(result.nodes.every(node=>!node._overviewGroup),'The six genuinely linked topics must not be needlessly collapsed');
      assert.equal(result.$shelves.reduce((sum,group)=>sum+group.members.length,0),27);
      const externalOnly=result.$shelves.find(group=>group.members.some(node=>node.id==='scope:n32'));
      assert(externalOnly.edges.includes(f.payload.edges.at(-1)),'An external dependency is retained but cannot invent local connectivity');
      assert.equal(snapshot(f),before);
    """)


def test_all_unlinked_topics_are_native_shelves_not_a_grid_or_boundary_only_map() -> None:
    _run(r"""
      const f=fixture(31),outside=boundary(f,0);f.payload.edges=[edge(outside.id,'scope:n0')];
      const result=structuralScopeProjection(f.payload,f.records,'current');assert(result);conservation(f,result);
      assert.equal(result.nodes.length,0);assert.equal(result.$shelves.length,2);
      assert.equal(result.$shelves.reduce((sum,group)=>sum+group.members.length,0),31);
      assert(result._scopeOverview.groups.some(group=>group.$boundary&&group.members[0]===outside));
    """)


def test_shelves_follow_nearest_authored_collection_through_transparent_and_math_ancestors_not_names() -> None:
    _run(r"""
      const f=fixture(33);f.records.set('range',record('range','C1',{navigation:'transparent',title:'Number Theory'}));
      f.records.set('inner',record('inner','range',{navigation:'collection',title:'Exact authored inner collection'}));
      f.records.set('topic',record('topic','inner',{title:'A misleading folder-like title'}));
      f.records.get('n20').parent='range';f.records.get('n21').parent='topic';
      const before=snapshot(f),result=structuralScopeProjection(f.payload,f.records,'current');conservation(f,result);
      const first=result.$shelves.find(group=>group.members.some(node=>node.id==='scope:n20'));
      const inner=result.$shelves.find(group=>group.members.some(node=>node.id==='scope:n21'));
      assert.equal(first.$owner,'C1');assert.equal(first.title,f.records.get('C1').title);
      assert.equal(inner.$owner,'inner');assert.equal(inner.title,f.records.get('inner').title);
      assert.equal(snapshot(f),before);assert(!result.nodes.some(node=>node.title==='Number Theory'));
    """)


def test_oversized_shelf_and_its_selected_owner_drill_retain_every_real_member_without_a_layout_overflow() -> None:
    _run(r"""
      const f=fixture(401),result=structuralScopeProjection(f.payload,f.records,'current');conservation(f,result);
      const shelf=result.$shelves.find(group=>group.$owner==='C1');assert(shelf.members.length>MAX_MAP_NODES);
      const selected={...f.payload,nodes:shelf.members.slice(),edges:[]};
      const drilled=structuralScopeProjection(selected,f.records,shelf.$owner,[shelf.id]);assert(drilled);
      conservation({payload:selected,records:f.records},drilled);
      assert.equal(drilled.nodes.length,0);assert.equal(drilled.$shelves.length,1);
      assert.deepEqual(drilled.$shelves[0].members.map(node=>node.id).sort(),shelf.members.map(node=>node.id).sort());
      assert.equal(drilled.$shelves[0].$owner,shelf.$owner);assert(drilled.$shelves[0].id!==shelf.id);
    """)


def test_oversized_connected_component_keeps_all_typed_edges_in_honest_private_provenance() -> None:
    _run(r"""
      const f=fixture(241);for(let n=1;n<241;n++)f.payload.edges.push(edge('scope:n'+(n-1),'scope:n'+n,n,2*n));
      const before=snapshot(f),result=structuralScopeProjection(f.payload,f.records,'current');conservation(f,result);
      assert.equal(result.nodes.length,1);assert.equal(result.$shelves.length,0);
      assert.equal(result._scopeOverview.groups[0].members.length,241);assert.equal(result._scopeOverview.groups[0].edges.length,240);
      assert.equal(result.nodes[0].member_count,241);
      assert.equal(result.nodes[0].status_counts.reduce((sum,pair)=>sum+pair[1],0),241);
      assert.equal(result.nodes[0].internal_dependency_count,f.payload.edges.reduce((sum,edge)=>sum+edge.statement+edge.proof,0));
      assert.equal(snapshot(f),before);assert.equal(result.edges.length,0,'Collapse must not turn internal edges into invented self dependencies');
    """)


def test_too_many_real_components_coarsen_to_existing_collections_before_the_canvas_cap() -> None:
    _run(r"""
      const f=fixture(260);for(let n=0;n<260;n+=2)f.payload.edges.push(edge('scope:n'+n,'scope:n'+(n+1)));
      const result=structuralScopeProjection(f.payload,f.records,'current');conservation(f,result);
      assert.equal(result.nodes.length,2);assert.deepEqual(result.nodes.map(node=>node.$owner).sort(),['C1','C2']);
      assert.deepEqual(result.nodes.map(node=>node.title).sort(),[f.records.get('C1').title,f.records.get('C2').title].sort());
      assert(result._scopeOverview.groups.every(group=>group.members.length===130));assert.equal(result.edges.length,0);
    """)


def test_many_boundaries_bundle_with_original_ids_and_full_directed_statement_proof_weights() -> None:
    _run(r"""
      const f=fixture(200);for(let n=0;n<200;n+=2)f.payload.edges.push(edge('scope:n'+n,'scope:n'+(n+1)));
      for(let n=0;n<40;n++){const outside=boundary(f,n);f.payload.edges.push(edge(outside.id,'scope:n0',n+1,0),edge('scope:n0',outside.id,0,n+2));}
      const result=structuralScopeProjection(f.payload,f.records,'current');conservation(f,result);
      const external=result._scopeOverview.groups.find(group=>group.$boundary);assert(external);assert.equal(external.members.length,40);
      assert(external.members.every(node=>node.id.startsWith('boundary:outside-')));assert.equal(result.nodes.length,101);
      assert.equal(result.edges.length,2);assert.equal(result.edges.find(edge=>edge.source===external.id).statement,820);
      assert.equal(result.edges.find(edge=>edge.target===external.id).proof,860);
    """)


def test_unknown_cyclic_and_source_ancestry_fail_closed_without_rewriting_any_inputs() -> None:
    _run(r"""
      for(const kind of ['missing','cycle','self','source']) {
        const f=fixture(33);
        if(kind==='missing')f.records.get('n0').parent='unpublished';
        if(kind==='cycle'){f.records.get('n0').parent='n1';f.records.get('n1').parent='n0';}
        if(kind==='self')f.records.get('current').parent='current';
        if(kind==='source')f.records.get('C1').navigation='source';
        const before=snapshot(f);assert.equal(structuralScopeProjection(f.payload,f.records,'current'),null);assert.equal(snapshot(f),before);
      }
    """)


def test_literal_canonical_prefixes_record_id_and_reserved_display_names_never_alias_shelf_owners() -> None:
    _run(r"""
      for(const prefix of ['scope','boundary']) {
        const f=fixture(13),literal=record(prefix+':authored','C1'),explicit=record(prefix+':explicit','C1'),ordinary=record('authored','C2');
        f.records.set(literal.id,literal);f.records.set(explicit.id,explicit);f.records.set(ordinary.id,ordinary);f.records.set('view-group:0',record('view-group:0'));
        f.payload.nodes[0]=literal;
        f.payload.nodes[1]={...f.payload.nodes[1],id:'display:literal',_recordId:explicit.id};
        const result=structuralScopeProjection(f.payload,f.records,'current',['view-group:1']);conservation(f,result);
        const owner=result.$shelves.find(group=>group.members.includes(literal));assert.equal(owner.$owner,'C1');
        assert(owner.members.some(node=>node.id==='display:literal'));
        assert(result._scopeOverview.groups.every(group=>!f.records.has(group.id)&&group.id!=='view-group:1'));
        assert(!result.$shelves.some(group=>group.members.includes(ordinary)));
      }
    """)


def test_input_permutation_cannot_change_real_component_or_shelf_membership_or_typed_counts() -> None:
    _run(r"""
      const f=fixture(33);f.payload.edges=[edge('scope:n0','scope:n1'),edge('scope:n1','scope:n2',5,7)];
      const first=structuralScopeProjection(f.payload,f.records,'current');
      const reordered={...f.payload,nodes:f.payload.nodes.slice().reverse(),edges:f.payload.edges.slice().reverse()};
      const second=structuralScopeProjection(reordered,new Map(Array.from(f.records).reverse()),'current');
      function shape(result){return {nodes:result.nodes.map(node=>node.id).sort(),groups:result._scopeOverview.groups.map(group=>
        [group.$owner,group.$shelf,group.members.map(node=>node.id).sort()]),edges:result.edges.map(edge=>[edge.source,edge.target,edge.statement,edge.proof])};}
      assert.deepEqual(shape(first),shape(second));conservation(f,first);conservation({payload:reordered,records:f.records},second);
    """)


def test_duplicate_canonical_aliases_fail_closed_and_native_shelves_keep_existing_index_order() -> None:
    _run(r"""
      const duplicate=fixture(13);duplicate.payload.nodes[1]._recordId='n0';const before=snapshot(duplicate);
      assert.equal(structuralScopeProjection(duplicate.payload,duplicate.records,'current'),null);assert.equal(snapshot(duplicate),before);
      const f=fixture(33);Array.from(f.records.values()).forEach((node,index)=>node._index=100-index);
      const result=structuralScopeProjection(f.payload,f.records,'current');conservation(f,result);
      result.$shelves.forEach(group=>{
        const positions=group.members.map(node=>f.records.get(node.id.replace(/^scope:/,''))._index);
        assert.deepEqual(positions,positions.slice().sort((a,b)=>a-b));
      });
    """)
