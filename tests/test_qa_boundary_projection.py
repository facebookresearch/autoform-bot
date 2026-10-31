"""Actual boundary context projection, composed with real structural/wiki helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_qa_scope_projection import _PEERS
from tests.test_wiki_owner_projection import _LOADER

_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for actual boundary projection tests")

_BOUNDARY_PEERS = r"""
function contextFixture(count=6) {
  const f=fixture(2);for(let n=0;n<count;n++)boundary(f,n);return f;
}
function groupFor(result,role) {return result._scopeOverview.groups.find(group=>group.$contextRole===role);}
function originals(payload) {
  function leaves(edge){return edge._overviewEdges?edge._overviewEdges.flatMap(leaves):[edge];}
  return payload.edges.flatMap(leaves).concat((payload._scopeOverview?.groups||[]).flatMap(group=>(group.edges||[]).flatMap(leaves)));
}
function contextConservation(input,result) {
  const before=originals(input),after=originals(result);assert.equal(new Set(before).size,before.length);
  assert.equal(after.length,before.length);assert.equal(new Set(after).size,after.length);
  before.forEach(edge=>assert(after.includes(edge),'Every canonical typed edge identity must be retained once'));
  const expected=new Set(input.nodes.filter(node=>!node._overviewGroup).map(node=>node.id));
  (input._scopeOverview?.groups||[]).forEach(group=>group.members.forEach(node=>expected.add(node.id)));
  const members=result._scopeOverview.groups.flatMap(group=>group.members.map(node=>node.id));
  assert.equal(new Set(members).size,members.length,'No original node may gain a second context membership');
  assert.deepEqual(members.slice().sort(),Array.from(expected).sort());
  result.edges.forEach(edge=>{
    assert(edge._overviewEdges.every(original=>!original._overviewEdges),'Provenance must be canonical leaves, never nested wrappers');
    assert.equal(edge.statement,edge._overviewEdges.reduce((sum,original)=>sum+(original.statement||0),0));
    assert.equal(edge.proof,edge._overviewEdges.reduce((sum,original)=>sum+(original.proof||0),0));
    assert(result.nodes.some(node=>node.id===edge.source));assert(result.nodes.some(node=>node.id===edge.target));
  });
  assert(result.nodes.length<=input.nodes.length);
}
"""


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    program = _PEERS + _BOUNDARY_PEERS + "\n(async()=>{\n" + body + "\n})()"
    script += "\nlet finished=false;const deadline=setTimeout(()=>{console.error('Boundary test did not complete');process.exitCode=1;},5000);\n"
    script += "process.once('beforeExit',()=>{if(!finished){console.error('Boundary test did not complete');process.exitCode=1;}});\n"
    script += "Promise.resolve(vm.runInContext(production+'\\n'+original('structuralScopeProjection')+'\\n'+"
    script += "original('boundaryContextProjection')+" + json.dumps(program) + ",context,{timeout:2000})).then("
    script += "()=>{finished=true;clearTimeout(deadline);},error=>{finished=true;clearTimeout(deadline);console.error(error);process.exitCode=1;});\n"
    result = subprocess.run([_NODE], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def test_small_disconnected_or_already_grouped_boundaries_do_not_create_a_context_projection() -> None:
    _run(r"""
      const small=contextFixture(2),disconnected=contextFixture(3),grouped=contextFixture(3);
      grouped.payload.nodes.filter(node=>node.kind==='boundary').forEach(node=>node._overviewGroup='existing');
      for(const f of [small,disconnected,grouped]) {
        const before=snapshot(f);assert.equal(boundaryContextProjection(f.payload),null);assert.equal(snapshot(f),before);
      }
    """)


def test_actual_directions_make_three_small_context_groups_with_exact_members_and_typed_counts() -> None:
    _run(r"""
      const f=contextFixture();f.payload.edges=[
        edge('boundary:outside-0','scope:n0',2,3),edge('boundary:outside-1','scope:n0',5,7),
        edge('scope:n0','boundary:outside-2',11,13),edge('scope:n0','boundary:outside-3',17,19),
        edge('boundary:outside-4','scope:n0',23,29),edge('scope:n0','boundary:outside-4',31,37),
        edge('boundary:outside-5','scope:n0',41,43),edge('scope:n0','boundary:outside-5',47,53)];
      const before=snapshot(f);freeze(f.payload);const result=boundaryContextProjection(f.payload);assert(result);contextConservation(f.payload,result);
      for(const [role,title,ids] of [['needed','Needed here',[0,1]],['builds','Builds on this',[2,3]],['shared','Shared context',[4,5]]]) {
        const group=groupFor(result,role);assert.equal(group.title,title);assert.equal(group.$boundary,true);
        assert.deepEqual(group.members.map(node=>node.id),ids.map(n=>'boundary:outside-'+n));
        const proxy=result.nodes.find(node=>node.id===group.id);assert.equal(proxy.kind,'boundary');assert.equal(proxy._overviewGroup,group.key);
        assert.deepEqual(proxy.layout,{width:168,height:88});assert(!('area'in proxy)&&!('_region'in proxy));
      }
      assert.equal(result.nodes.length,5);assert.equal(result.edges.find(edge=>edge.source===groupFor(result,'needed').id).statement,7);
      assert.equal(result.edges.find(edge=>edge.target===groupFor(result,'builds').id).proof,32);assert.equal(snapshot(f),before);
    """)


def test_bidirectional_boundary_is_owned_once_not_duplicated_into_incoming_and_outgoing_aliases() -> None:
    _run(r"""
      const f=contextFixture(3);for(let n=0;n<3;n++)f.payload.edges.push(
        edge('boundary:outside-'+n,'scope:n0'),edge('scope:n0','boundary:outside-'+n,5,7));
      const result=boundaryContextProjection(f.payload);contextConservation(f.payload,result);
      assert(groupFor(result,'shared'));assert(!groupFor(result,'needed'));assert(!groupFor(result,'builds'));
      assert.equal(groupFor(result,'shared').members.length,3);assert.equal(result.nodes.filter(node=>node.$contextRole).length,1);
      assert.equal(result.edges.length,2);
    """)


def test_context_only_nodes_keep_original_ids_and_edges_without_inventing_local_connections() -> None:
    _run(r"""
      const f=contextFixture(6);f.payload.edges=[edge('boundary:outside-0','scope:n0'),edge('scope:n0','boundary:outside-1'),
        edge('boundary:outside-2','scope:n0'),edge('scope:n0','boundary:outside-2'),
        edge('boundary:outside-3','boundary:outside-4'),edge('boundary:outside-5','boundary:outside-0')];
      const result=boundaryContextProjection(f.payload);contextConservation(f.payload,result);
      for(let n=3;n<6;n++) {
        const original=f.payload.nodes.find(node=>node.id==='boundary:outside-'+n);
        assert(result.nodes.includes(original));assert(!original._overviewGroup);
        assert(!result.edges.some(edge=>edge.source===original.id&&edge.target.startsWith('scope:')));
      }
      assert(result.edges.some(edge=>edge.source==='boundary:outside-3'&&edge.target==='boundary:outside-4'));
    """)


def test_internal_context_edges_remain_flat_private_provenance_not_new_self_dependencies() -> None:
    _run(r"""
      const f=contextFixture(3),internal=edge('boundary:outside-0','boundary:outside-1',17,19);
      f.payload.edges=[edge('boundary:outside-0','scope:n0'),edge('boundary:outside-1','scope:n0'),
        edge('scope:n0','boundary:outside-2'),{...internal,_overviewEdges:[internal]}];
      const result=boundaryContextProjection(f.payload);contextConservation(f.payload,result);
      const needed=groupFor(result,'needed');assert.deepEqual(needed.edges,[internal]);
      assert(!result.edges.some(edge=>edge.source===edge.target));
      assert.equal(result.nodes.find(node=>node.id===needed.id).internal_dependency_count,36);
    """)


def test_existing_boundary_proxy_groups_selected_state_and_private_provenance_remain_unchanged() -> None:
    _run(r"""
      const f=contextFixture(3),member=record('boundary:older','roadmap',{kind:'boundary'});
      const old={id:'existing',key:'existing-key',title:'Existing context',members:[member],edges:[],$boundary:true};
      const proxy={id:old.id,kind:'boundary',_overviewGroup:old.key,title:old.title};f.payload.nodes.push(proxy);
      const canonical=edge(member.id,'scope:n0'),wrapped={...canonical,source:proxy.id,_overviewEdges:[canonical]};
      f.payload.edges=[wrapped,edge('boundary:outside-0','scope:n0'),edge('scope:n0','boundary:outside-1'),
        edge('boundary:outside-2','scope:n0'),edge('scope:n0','boundary:outside-2')];
      f.payload._scopeOverview={groups:[old],selectedGroup:old.key,originalNodes:6,_flows:['stale'],_flowRange:99};
      const before=snapshot(f),result=boundaryContextProjection(f.payload);contextConservation(f.payload,result);
      assert(result.nodes.includes(proxy));assert.strictEqual(result._scopeOverview.groups[0],old);
      assert.equal(result._scopeOverview.selectedGroup,old.key);assert.equal(result._scopeOverview.originalNodes,6);
      assert(!('_flows'in result._scopeOverview));assert(!('_flowRange'in result._scopeOverview));
      assert.equal(snapshot(f),before);
    """)


def test_nested_provenance_is_flat_and_private_shelf_edges_inform_real_shared_context_without_new_canvas_edges() -> None:
    _run(r"""
      const f=contextFixture(3),visible=edge('boundary:outside-0','scope:n0'),hidden=record('hidden','C1');
      const privateLeaf=edge(hidden.id,'boundary:outside-0',5,7),privateWrap={...privateLeaf,_overviewEdges:[privateLeaf]};
      const shelf={id:'shelf',key:'shelf-key',title:'Original shelf',members:[hidden],edges:[privateWrap],$shelf:true};
      const wrap={...visible,_overviewEdges:[visible]},outer={...visible,_overviewEdges:[wrap]};
      f.payload.edges=[outer,edge('boundary:outside-1','scope:n0'),edge('scope:n0','boundary:outside-2')];
      f.payload._scopeOverview={groups:[shelf],selectedGroup:shelf.key,originalNodes:6};f.payload.$shelves=[shelf];
      const before=snapshot(f),result=boundaryContextProjection(f.payload);contextConservation(f.payload,result);
      const kept=result.$shelves[0];assert(result._scopeOverview.groups.includes(kept));assert.notStrictEqual(kept,shelf);
      assert.deepEqual(kept.edges,[privateLeaf]);assert.strictEqual(kept.members,shelf.members);
      assert.equal(groupFor(result,'shared').members[0].id,'boundary:outside-0');
      assert(!result.edges.some(edge=>edge.source===hidden.id));assert.equal(snapshot(f),before);
    """)


def test_structural_shelf_composition_retains_all_original_nodes_edges_and_existing_shelf_identity() -> None:
    _run(r"""
      const f=fixture(33);for(let n=0;n<3;n++)boundary(f,n);
      f.payload.edges=[edge('scope:n0','scope:n1'),edge('boundary:outside-0','scope:n0'),
        edge('scope:n32','boundary:outside-0'),edge('boundary:outside-1','scope:n1'),edge('scope:n1','boundary:outside-2')];
      const before=snapshot(f),structural=structuralScopeProjection(f.payload,f.records,'current');assert(structural);
      const result=boundaryContextProjection(structural);assert(result);contextConservation(structural,result);
      assert.strictEqual(result.$shelves,structural.$shelves);assert(result.$shelves.every(group=>result._scopeOverview.groups.includes(group)));
      assert.equal(groupFor(result,'shared').members[0].id,'boundary:outside-0');
      assert.deepEqual(new Set(originals(result)),new Set(f.payload.edges));
      assert.equal(result.$shelves.reduce((sum,group)=>sum+group.members.length,0),31);assert.equal(snapshot(f),before);
    """)


def test_canonical_wiki_owners_follow_the_single_context_replacement_without_changing_formal_edges() -> None:
    _run(r"""
      const f=contextFixture(3);f.payload.edges=[edge('boundary:outside-0','scope:n0'),edge('boundary:outside-1','scope:n0'),edge('scope:n0','boundary:outside-2')];
      f.payload.$wikiOwners=new Map([['outside-0','boundary:outside-0'],['outside-1','boundary:outside-1'],['outside-2','boundary:outside-2']]);
      const before=Array.from(f.payload.$wikiOwners),result=boundaryContextProjection(f.payload),math=JSON.stringify(result.edges);
      assert.equal(result.$wikiOwners.get('outside-0'),groupFor(result,'needed').id);
      assert.equal(result.$wikiOwners.get('outside-1'),groupFor(result,'needed').id);assert.deepEqual(Array.from(f.payload.$wikiOwners),before);
      const state={schema:'autoform-wiki-links/v1',index_sha256:'d'.repeat(64),availability:'complete',
        references:[['outside-0','n0'],['outside-1','n0'],['n0','outside-2']],unavailable_sources:[]};
      const wiki=await projectWikiLinks(result,f.records,state,result.$wikiOwners);assert.equal(wiki.availability,'complete');
      assert.deepEqual(wiki.edges,[{source:groupFor(result,'needed').id,target:'scope:n0',count:2},
        {source:'scope:n0',target:groupFor(result,'builds').id,count:1}].sort((a,b)=>a.source<b.source?-1:a.source>b.source?1:a.target<b.target?-1:1));
      assert.equal(JSON.stringify(result.edges),math);contextConservation(f.payload,result);
    """)


def test_explicit_canonical_aliases_duplicate_views_and_duplicate_provenance_fail_closed() -> None:
    _run(r"""
      for(const kind of ['canonical','view','provenance']) {
        const f=contextFixture(3);f.payload.edges=[edge('boundary:outside-0','scope:n0')];
        if(kind==='canonical'){f.payload.nodes[2]._recordId='same';f.payload.nodes[3]._recordId='same';}
        if(kind==='view')f.payload.nodes[3].id=f.payload.nodes[2].id;
        if(kind==='provenance')f.payload.edges.push(f.payload.edges[0]);
        const before=snapshot(f);assert.equal(boundaryContextProjection(f.payload),null);assert.equal(snapshot(f),before);
      }
    """)


def test_reserved_proxy_ids_do_not_collide_and_unknown_member_counts_stay_explicit() -> None:
    _run(r"""
      const f=contextFixture(3);f.payload.nodes[0]={...f.payload.nodes[0],id:'view-context:1',_recordId:'n0'};
      const hidden=record('hidden'),old={id:'view-context:2',key:'old',members:[hidden],edges:[]};
      f.payload._scopeOverview={groups:[old],selectedGroup:null};
      f.payload.nodes[2]._unknownCount=true;f.payload.nodes[2].member_count=7;delete f.payload.nodes[3].member_count;
      f.payload.edges=[edge('boundary:outside-0','view-context:1'),edge('boundary:outside-1','view-context:1'),edge('view-context:1','boundary:outside-2')];
      const result=boundaryContextProjection(f.payload,['view-context:0']);contextConservation(f.payload,result);
      const proxy=result.nodes.find(node=>node.id===groupFor(result,'needed').id);assert.equal(proxy._unknownCount,true);assert.equal(proxy.member_count,7);
      assert(result.nodes.filter(node=>node.$contextRole).every(node=>!['view-context:0','view-context:1','view-context:2'].includes(node.id)));
    """)


def test_invalid_or_cyclic_provenance_cannot_publish_a_partial_context_view() -> None:
    _run(r"""
      for(const kind of ['cycle','unknown','weight']) {
        const f=contextFixture(3),original=edge('boundary:outside-0','scope:n0');f.payload.edges=[original];
        if(kind==='cycle')original._overviewEdges=[original];
        if(kind==='unknown')original.target='missing';
        if(kind==='weight')original.statement=-1;
        assert.equal(boundaryContextProjection(f.payload),null);
      }
    """)
