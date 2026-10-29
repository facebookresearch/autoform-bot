"""Readable derived area overviews conserve the exact local mathematical graph."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/blueprint-dag.js"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated projection checks")


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + r"""
    const fs=require('fs'),vm=require('vm'),assert=require('assert');
    const source=fs.readFileSync(CLIENT,'utf8');
    const start=source.indexOf('  function scopeOverviewProjection('),end=source.indexOf('\n  function mount(',start);
    assert(start>=0&&end>start);const MAX_MAP_NODES=120;
    eval(source.slice(start,end));
    function fixture(){
      const nodes=Array.from({length:60},(_,index)=>({id:'topic-'+index,title:'Authored topic '+index,
        kind:'scope',area:index<54?'Authored area '+Math.floor(index/18):null,
        status:index%2?'stated':'fully_proved',member_count:2,status_counts:[[index%2?'stated':'fully_proved',2]],
        internal_dependency_count:index%3,layout:{width:196,height:100},url:'topic-'+index+'.html'}));
      nodes.push({id:'boundary:Analysis',title:'Analysis',kind:'boundary',member_count:3,status:'stated'});
      const edges=[{source:'topic-0',target:'topic-1',statement:3,proof:5},
        {source:'topic-2',target:'topic-20',statement:7,proof:11},
        {source:'topic-3',target:'topic-21',statement:13,proof:17},
        {source:'topic-20',target:'topic-40',statement:19,proof:23},
        {source:'topic-54',target:'topic-55',statement:29,proof:31},
        {source:'topic-55',target:'boundary:Analysis',statement:37,proof:41}];
      return {schema:'autoform-dag-view/v2',title:'A large mathematical scope',nodes,edges,
        view:{presentation:'dag'},palette:{fully_proved:{},stated:{},inventory_pending:{}}};
    }
    function preserved(input,overview){
      const groups=overview._scopeOverview.groups;
      assert.deepEqual(groups.flatMap(group=>group.members.map(node=>node.id)).sort(),
        input.nodes.filter(node=>node.kind!=='boundary').map(node=>node.id).sort());
      const original=overview.edges.flatMap(edge=>edge._overviewEdges).concat(
        overview.nodes.flatMap(node=>node._overviewEdges||[]));
      const signature=edge=>JSON.stringify([edge.source,edge.target,edge.statement,edge.proof]);
      assert.deepEqual(original.map(signature).sort(),input.edges.map(signature).sort());
      for(const kind of ['statement','proof']) assert.equal(original.reduce((sum,edge)=>sum+(edge[kind]||0),0),
        input.edges.reduce((sum,edge)=>sum+(edge[kind]||0),0));
      assert.equal(overview.nodes.filter(node=>node.kind==='boundary').length,1);
    }
    """ + "\n" + body
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_large_scope_uses_authored_areas_and_keeps_unassigned_topics_explicit() -> None:
    _run(r"""
      const input=fixture(),before=JSON.stringify(input),overview=scopeOverviewProjection(input,null);
      assert.equal(overview.nodes.length,5);preserved(input,overview);
      assert.equal(JSON.stringify(input),before);
      const areas=overview.nodes.filter(node=>node._region);
      assert.deepEqual(areas.map(node=>node.title),['Authored area 0','Authored area 1','Authored area 2']);
      assert(areas.every(node=>node.member_count===36&&node._topics===18));
      assert(areas.every(node=>JSON.stringify(node.status_counts)===JSON.stringify([['fully_proved',18],['stated',18]])));
      const unassigned=overview.nodes.find(node=>node._overviewGroup==='unassigned');
      assert.equal(unassigned.title,'Unassigned topics');assert.equal(unassigned._region,undefined);
      assert.equal(unassigned.area,undefined);assert.equal(unassigned.member_count,12);
      assert.equal(unassigned.status,'inventory_pending');
      // Sixty existing scope-internal links, plus captured internal weights8 and60.
      assert.equal(overview.nodes.reduce((sum,node)=>sum+(node.internal_dependency_count||0),0),128);
      assert(!overview.edges.some(edge=>edge.source===edge.target));
    """)


def test_group_expansion_preserves_native_node_ids_and_every_typed_dependency() -> None:
    _run(r"""
      const input=fixture(),overview=scopeOverviewProjection(input,'area:Authored area 0');
      assert.equal(overview._scopeOverview.selectedGroup,'area:Authored area 0');
      assert.equal(overview.nodes.length,22);preserved(input,overview);
      for(let index=0;index<18;index++) assert(overview.nodes.some(node=>node.id==='topic-'+index));
      assert(overview.edges.some(edge=>edge.source==='topic-0'&&edge.target==='topic-1'&&edge.statement===3&&edge.proof===5));
      assert.equal(scopeOverviewProjection(input,'stale-group')._scopeOverview.selectedGroup,null);
    """)


def test_group_ids_do_not_collide_with_real_ids_and_unknown_counts_are_not_zero() -> None:
    _run(r"""
      const input=fixture();input.nodes[59].id='view-group:0';delete input.nodes[58].member_count;
      const overview=scopeOverviewProjection(input,null);
      const originals=new Set(input.nodes.map(node=>node.id));
      assert(overview.nodes.filter(node=>node._overviewGroup).every(node=>!originals.has(node.id)));
      assert.equal(overview.nodes.find(node=>node._overviewGroup==='unassigned')._unknownCount,true);
      assert.equal(overview._scopeOverview.groups.flatMap(group=>group.members).length,60);
      const globalReserved=new Set(['view-group:0','view-group:1','view-group:2','view-group:3']);
      const globallySafe=scopeOverviewProjection(input,null,globalReserved);
      assert(globallySafe.nodes.filter(node=>node._overviewGroup).every(node=>!globalReserved.has(node.id)),
        'Derived group identifiers must not collide with authored records outside the current slice');
    """)


def test_small_or_unclassified_scopes_do_not_invent_mathematical_areas() -> None:
    _run(r"""
      const input=fixture();input.nodes.forEach(node=>delete node.area);
      assert.equal(scopeOverviewProjection(input,null),null);
      const small=fixture();small.nodes=small.nodes.slice(0,20);small.edges=[];
      assert.equal(scopeOverviewProjection(small,null),null);
      const large=fixture();large.nodes=Array.from({length:121},(_,n)=>({id:'n'+n,kind:'node',area:n%2?'A':'B'}));
      assert.equal(scopeOverviewProjection(large,null),null);
    """)


def test_filtered_flow_paints_cached_kind_weights_without_changing_source_or_cached_routes() -> None:
    _run(r"""
      const points=[{x:0,y:0},{x:100,y:0}],route={source:'group-a',target:'group-b',
        kind:'statement',count:30,strength:4,points,radii:[0,0],bounds:{x:0,y:0,w:100,h:0}};
      const counts={statement:2,proof:3},before=JSON.stringify({counts,route}),shown=scopeOverviewFlow(route,counts,6);
      assert.equal(shown.count,2);assert(shown.strength<route.strength);
      assert.strictEqual(shown.points,points);assert.strictEqual(shown.bounds,route.bounds);
      assert.equal(scopeOverviewFlow(route,{statement:0,proof:0},6),null,
        'A bundled flow between hidden original members must not appear between visible groups');
      assert.strictEqual(scopeOverviewFlow(route,{statement:30,proof:34},6),route);
      assert.strictEqual(scopeOverviewFlow(route,null,6),route);
      const proof=scopeOverviewFlow({...route,kind:'proof',count:34},counts,6);
      assert.equal(proof.count,3);
      assert.equal(JSON.stringify({counts,route}),before);
    """)
