"""Actual wiki affinity geometry; formal SCCs and routes keep their independent oracle."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_integrated_dag_layout import _ORACLE, _graph

_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is required for wiki affinity geometry checks")

_LOADER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(ROOT+'/autoform_cli/assets/blueprint-dag.js','utf8');
function original(name) {
  const start=source.indexOf('function '+name+'(');assert(start>=0,'Missing production function: '+name);
  for(let end=source.indexOf('}',start);end>=0;end=source.indexOf('}',end+1)) {
    const declaration=source.slice(start,end+1);
    try {new vm.Script('('+declaration+')');return declaration;}
    catch(error) {if(!(error instanceof SyntaxError))throw error;}
  }
  assert.fail('Unparseable production function: '+name);
}
const context={assert,structuredClone,performance,fixture:JSON.parse(fs.readFileSync(0,'utf8'))};vm.createContext(context);
const production=source.match(/\bvar NODE_W =[^;]+;/)[0]+'\n'+['layoutGraph','wikiRoutes'].map(original).join('\n');
"""

_AFFINITY_ORACLE = r"""
function weakComponents(nodes,edges,wiki=[]) {
  const adjacent=new Map(nodes.map(node=>[node.id,new Set()]));
  for(const edge of edges.concat(wiki)) {
    if(!(edge.statement>0||edge.proof>0||Number.isFinite(edge.count)&&edge.count>0))continue;
    if(!adjacent.has(edge.source)||!adjacent.has(edge.target))continue;
    adjacent.get(edge.source).add(edge.target);adjacent.get(edge.target).add(edge.source);
  }
  const visited=new Set(),groups=[];
  for(const id of adjacent.keys()) {
    if(visited.has(id))continue;
    const found=[],pending=[id];visited.add(id);
    while(pending.length) {
      const next=pending.pop();found.push(next);
      for(const neighbor of adjacent.get(next))if(!visited.has(neighbor)){visited.add(neighbor);pending.push(neighbor);}
    }
    groups.push(found.sort());
  }
  return groups.sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));
}
function envelope(rectangles) {
  const left=Math.min(...rectangles.map(box=>box.x)),top=Math.min(...rectangles.map(box=>box.y));
  return {x:left,y:top,w:Math.max(...rectangles.map(box=>box.x+box.w))-left,
    h:Math.max(...rectangles.map(box=>box.y+box.h))-top};
}
function formalEnvelopes(input,scene) {
  return weakComponents(input.nodes,input.edges).map(members=>({members,bounds:envelope([
    ...members.map(id=>scene.nodes.get(id)),...scene.routes.filter(route=>members.includes(route.source)).map(route=>route.bounds)
  ])}));
}
function publicScene(scene) {
  return JSON.stringify({nodes:Array.from(scene.nodes).sort((a,b)=>a[0]<b[0]?-1:1),routes:scene.routes,
    clusters:scene.clusters,work:scene.work,bounds:scene.bounds});
}
function normalized(scene) {
  const routes=scene.routes.map(route=>{const copy={...route};delete copy.edgeIndex;return copy;});
  const families=(scene.$wikiFamilies||[]).map(family=>({...family,members:family.members.slice().sort()}))
    .sort((a,b)=>JSON.stringify(a.members).localeCompare(JSON.stringify(b.members)));
  return JSON.stringify({nodes:Array.from(scene.nodes).sort((a,b)=>a[0]<b[0]?-1:1),routes,
    clusters:scene.clusters,bounds:scene.bounds,families,wiki:scene.$wikiEdges||[],work:scene.$wikiWork});
}
function close(actual,expected,message) {assert(Math.abs(actual-expected)<1e-6,message);}
function translatedBox(actual,base,dx,dy) {
  close(actual.x,base.x+dx,'A formal envelope changed its local x coordinate');
  close(actual.y,base.y+dy,'A formal envelope changed its local y coordinate');
  close(actual.w,base.w,'A formal envelope changed width');close(actual.h,base.h,'A formal envelope changed height');
}
function sameFormalGeometry(input,base,clustered) {
  assert.deepEqual(clustered.clusters,base.clusters,'Wiki links changed actual mathematical SCCs or ranks');
  assert.deepEqual(clustered.work,base.work,'Wiki work entered the formal layout counters');
  assert.equal(clustered.routes.length,base.routes.length,'Wiki links added formal dependency routes');
  for(const members of weakComponents(input.nodes,input.edges)) {
    const origin=base.nodes.get(members[0]),moved=clustered.nodes.get(members[0]),dx=moved.x-origin.x,dy=moved.y-origin.y;
    for(const id of members) {
      const before=base.nodes.get(id),after=clustered.nodes.get(id);translatedBox(after,before,dx,dy);
      assert.equal(after.cluster,before.cluster);assert.equal(after.rank,before.rank);assert.equal(after.id,before.id);
    }
    base.routes.forEach((route,index)=>{
      if(!members.includes(route.source))return;
      const actual=clustered.routes[index];
      for(const key of ['source','target','kind','count','strength','flow','label','detoured'])assert.deepEqual(actual[key],route[key]);
      const beforeParts=route.parts||[route],afterParts=actual.parts||[actual];assert.equal(afterParts.length,beforeParts.length);
      beforeParts.forEach((part,at)=>{
        const after=afterParts[at];assert.equal(after.points.length,part.points.length);assert.deepEqual(after.radii,part.radii);
        translatedBox(after.bounds,part.bounds,dx,dy);
        part.points.forEach((point,n)=>{close(after.points[n].x,point.x+dx,'A formal route was deformed');
          close(after.points[n].y,point.y+dy,'A formal route was deformed');});
      });
      translatedBox(actual.bounds,route.bounds,dx,dy);
      close(actual.arrow.x,route.arrow.x+dx,'A formal arrow moved outside its component');
      close(actual.arrow.y,route.arrow.y+dy,'A formal arrow moved outside its component');
      assert.equal(actual.arrow.dx,route.arrow.dx);assert.equal(actual.arrow.dy,route.arrow.dy);
    });
  }
}
function checkAffinity(input,wiki,direction='lr',aspect=1.4) {
  const before=JSON.stringify({input,wiki}),base=layoutGraph(input.nodes,input.edges,direction,aspect);
  const result=layoutGraph(input.nodes,input.edges,direction,aspect,wiki);sameFormalGeometry(input,base,result);
  assert.equal(JSON.stringify({input,wiki}),before,'Affinity layout mutated graph, status, or reference inputs');
  const formal=formalEnvelopes(input,result);
  formal.forEach((family,index)=>formal.slice(index+1).forEach(other=>assert(
    !rectangleOverlap(family.bounds,other.bounds,96-1e-6),'Whole formal route envelopes need their 96px collision gap')));
  const expected=weakComponents(input.nodes,input.edges,wiki),actual=result.$wikiFamilies.map(family=>family.members.slice().sort())
    .sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));
  assert.deepEqual(actual,expected,'Packing families must follow only actual wiki/formal weak connectivity');
  result.$wikiFamilies.forEach((family,index)=>{
    assert([family.bounds.x,family.bounds.y,family.bounds.w,family.bounds.h].every(Number.isFinite));
    const members=formal.filter(component=>component.members.some(id=>family.members.includes(id)));
    assert.equal(family.island_count,members.length);
    members.forEach(component=>{
      assert(component.bounds.x>=family.bounds.x-1e-6&&component.bounds.y>=family.bounds.y-1e-6);
      assert(component.bounds.x+component.bounds.w<=family.bounds.x+family.bounds.w+1e-6);
      assert(component.bounds.y+component.bounds.h<=family.bounds.y+family.bounds.h+1e-6);
    });
    result.$wikiFamilies.slice(index+1).forEach(other=>assert(!rectangleOverlap(family.bounds,other.bounds,256-1e-6),
      'Unrelated wiki families need the declared 256px envelope separation'));
  });
  const n=input.nodes.length,work=result.$wikiWork;
  assert(Object.values(work).every(value=>Number.isFinite(value)&&value>=0));
  assert(work.ticks<=32*n);assert(work.pairs<=32*n*(n-1)/2);assert(work.moves<=96*n*n);
  check(input,direction,result);return result;
}
function centerDistance(scene,a,b) {
  const first=scene.nodes.get(a),last=scene.nodes.get(b);
  return Math.hypot(first.x+first.w/2-last.x-last.w/2,first.y+first.h/2-last.y-last.h/2);
}
function checkWikiRoutes(scene) {
  const before=publicScene(scene),result=wikiRoutes(scene),degrees=new Map(),known=new Map(scene.$wikiEdges.map(edge=>
    [JSON.stringify([edge.source,edge.target]),edge.count]));
  assert.equal(publicScene(scene),before,'Wiki routing changed formal geometry or mathematical routes');
  assert.equal(result.total,scene.$wikiEdges.length);assert.equal(result.routes.length+result.omitted,result.total);
  assert(Object.values(result.work).every(value=>Number.isFinite(value)&&value>=0));
  assert(result.work.attempts<=scene.nodes.size*2);
  assert(result.work.pairs<=result.work.attempts*(1+34*34));
  assert(result.work.obstacles<=result.work.pairs*scene.nodes.size);
  result.routes.forEach(route=>{
    assert.equal(route.kind,'wiki');assert(!('arrow'in route)&&!('flow'in route),'Associations must not become proof arrows');
    assert.equal(route.count,known.get(JSON.stringify([route.source,route.target])));
    assert(route.points.length>=2&&route.points.length<=34);assert(route.points.every(point=>Number.isFinite(point.x)&&Number.isFinite(point.y)));
    assert(route.radii.every(radius=>radius===0));assert(Number.isFinite(route.strength)&&route.strength>0);
    for(const id of [route.source,route.target])degrees.set(id,(degrees.get(id)||0)+1);
    route.points.slice(1).forEach((point,index)=>scene.nodes.forEach(box=>assert(
      !lineHits(route.points[index],point,box,box.id===route.source||box.id===route.target?0:16-1e-6),
      JSON.stringify({problem:'A real wiki segment crosses a mathematical card',source:route.source,target:route.target,
        segment:index,from:route.points[index],to:point,card:box,padding:box.id===route.source||box.id===route.target?0:16}))));
    for(const [point,id] of [[route.points[0],route.source],[route.points.at(-1),route.target]]) {
      const box=scene.nodes.get(id);assert(point.x>=box.x-1e-6&&point.x<=box.x+box.w+1e-6&&point.y>=box.y-1e-6&&point.y<=box.y+box.h+1e-6);
      assert(Math.min(Math.abs(point.x-box.x),Math.abs(point.x-box.x-box.w),Math.abs(point.y-box.y),Math.abs(point.y-box.y-box.h))<1e-6);
    }
    const from=route.points[0],to=route.points.at(-1),direct=Math.hypot(to.x-from.x,to.y-from.y);
    const length=route.points.slice(1).reduce((sum,point,n)=>sum+Math.hypot(point.x-route.points[n].x,point.y-route.points[n].y),0);
    assert(length<=direct*1.8+96+1e-6,'A wiki route exceeded the local backbone detour bound');
  });
  assert(Array.from(degrees.values()).every(count=>count<=4),'The soft backbone exceeded four associations per node');
  return result;
}
"""


def _run(body: str, fixture: object) -> None:
    script = "const ROOT=" + json.dumps(str(_ROOT)) + ";\n" + _LOADER
    script += "vm.runInContext(" + json.dumps('"use strict";\n') + "+production+"
    script += json.dumps(_ORACLE + "\n" + _AFFINITY_ORACLE) + ",context,{timeout:2000});\n"
    script += "vm.runInContext(" + json.dumps(body) + ",context,{timeout:6000});\n"
    result = subprocess.run([_NODE, "-e", script], input=json.dumps(fixture), text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-8000:]


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_empty_invalid_and_internal_only_affinities_preserve_the_original_scene_exactly(direction: str) -> None:
    fixture = _graph(5, [(0, 1, 2, 3), (1, 2, 5, 7), (2, 0, 11, 13)])
    _run(f"""
      const direction={json.dumps(direction)},before=JSON.stringify(fixture),base=layoutGraph(fixture.nodes,fixture.edges,direction,1.4);
      const invalid=[null,{{}},{{source:'missing',target:'N003',count:1}},{{source:'N000',target:'N003',count:0}},
        {{source:'N000',target:'N003',count:-1}},{{source:'N000',target:'N003',count:NaN}},
        {{source:'N000',target:'N003',count:Infinity}},{{source:'N000',target:'N003',count:'2'}},
        {{source:'N000',target:'N003',count:true}},{{source:'N000',target:'N000',count:1}}];
      for(const wiki of [undefined,null,[],{{availability:'unavailable',references:[]}},invalid,
          [{{source:'N000',target:'N002',count:3}},{{source:'N002',target:'N001',count:9}}]]) {{
        assert.equal(publicScene(layoutGraph(fixture.nodes,fixture.edges,direction,1.4,wiki)),publicScene(base));
      }}
      assert.equal(JSON.stringify(fixture),before);check(fixture,direction,base);
    """, fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_true_pair_families_are_closer_than_unrelated_topics_without_title_or_area_clumps(direction: str) -> None:
    fixture = _graph(8, [])
    for node in fixture["nodes"]:
        node.update(width=208, height=100, area="Dynamics", scope="Same authored scope", title="Same visible title")
    _run(f"""
      const wiki=[{{source:'N000',target:'N007',count:1}},{{source:'N002',target:'N006',count:1}}];
      const scene=checkAffinity(fixture,wiki,{json.dumps(direction)});
      for(const [a,b] of [['N000','N007'],['N002','N006']]) {{
        const related=centerDistance(scene,a,b);
        for(const other of fixture.nodes.filter(node=>node.id!==a&&node.id!==b))
          assert(related<centerDistance(scene,a,other.id),JSON.stringify({{problem:'A simple actual wiki pair did not cluster closer than an unrelated topic',
            a,b,other:other.id,related,unrelated:centerDistance(scene,a,other.id),families:scene.$wikiFamilies,nodes:Array.from(scene.nodes.values())}}));
      }}
      assert.equal(scene.routes.length,0);assert.equal(scene.clusters.length,8);assert(scene.clusters.every(cluster=>cluster.rank===0));
      assert.equal(scene.$wikiFamilies.filter(family=>family.members.length===1).length,4);
      const routes=checkWikiRoutes(scene);assert.equal(routes.routes.length,2);assert.equal(routes.omitted,0);
    """, fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_whole_formal_cycles_chains_and_self_loops_translate_with_exact_routes_and_ranks(direction: str) -> None:
    fixture = _graph(10, [(0, 1, 2, 3), (1, 0, 5, 7), (1, 2, 11, 13),
                          (3, 4, 17, 19), (4, 5, 23, 29), (6, 6, 31, 37), (0, 1, 2, 3)])
    _run(f"""
      const wiki=[{{source:'N002',target:'N003',count:3}},{{source:'N005',target:'N006',count:5}}];
      const scene=checkAffinity(fixture,wiki,{json.dumps(direction)});
      assert.equal(scene.nodes.get('N000').cluster,scene.nodes.get('N001').cluster);
      assert.notEqual(scene.nodes.get('N001').cluster,scene.nodes.get('N002').cluster);
      assert.equal(scene.nodes.get('N002').rank,1);assert.equal(scene.nodes.get('N005').rank,2);
      assert(scene.$wikiFamilies.some(family=>JSON.stringify(family.members)===JSON.stringify(fixture.nodes.slice(0,7).map(node=>node.id))));
      checkWikiRoutes(scene);
    """, fixture)


def test_duplicate_directions_use_maximum_and_opposite_directions_sum_without_artificial_attraction() -> None:
    fixture = _graph(6, [(0, 1, 1, 0)])
    _run("""
      const wiki=[{source:'N000',target:'N004',count:2},{source:'N000',target:'N004',count:7},
        {source:'N000',target:'N004',count:7},{source:'N004',target:'N000',count:3},
        {source:'N004',target:'N000',count:1},{source:'N002',target:'N005',count:.5}];
      const scene=checkAffinity(fixture,wiki);
      assert.deepEqual(scene.$wikiEdges,[{source:'N000',target:'N004',count:10},{source:'N002',target:'N005',count:.5}]);
      const equivalent=[{source:'N004',target:'N000',count:3},{source:'N000',target:'N004',count:7},wiki.at(-1)];
      assert.equal(normalized(scene),normalized(checkAffinity(fixture,equivalent)));
    """, fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_record_formal_and_wiki_permutations_preserve_geometry_and_actual_family_members(direction: str) -> None:
    fixture = _graph(12, [(0, 1, 2, 3), (1, 0, 5, 7), (2, 3, 11, 13), (4, 5, 17, 19)])
    _run(f"""
      const direction={json.dumps(direction)},wiki=[{{source:'N001',target:'N002',count:3}},
        {{source:'N003',target:'N006',count:5}},{{source:'N004',target:'N008',count:7}}];
      const scene=checkAffinity(fixture,wiki,direction,2);
      const reversed={{nodes:fixture.nodes.slice().reverse(),edges:fixture.edges.slice().reverse()}};
      assert.equal(normalized(scene),normalized(checkAffinity(reversed,wiki.slice().reverse(),direction,2)));
      const swapped=wiki.map(link=>({{source:link.target,target:link.source,count:link.count}}));
      assert.equal(normalized(scene),normalized(checkAffinity(fixture,swapped,direction,2)));
    """, fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_aspect_changes_only_family_packing_not_formal_or_affinity_membership(direction: str) -> None:
    fixture = _graph(20, [(0, 1, 1, 1), (2, 3, 2, 3)])
    _run(f"""
      const direction={json.dumps(direction)},wiki=[{{source:'N000',target:'N004',count:1}},
        {{source:'N002',target:'N005',count:2}},{{source:'N006',target:'N007',count:3}}];
      const tall=checkAffinity(fixture,wiki,direction,.5),wide=checkAffinity(fixture,wiki,direction,2);
      assert(wide.bounds.w/wide.bounds.h>tall.bounds.w/tall.bounds.h,'Physical aspect did not affect packing of distinct families');
      assert.deepEqual(wide.clusters,tall.clusters);assert.deepEqual(wide.$wikiEdges,tall.$wikiEdges);
      assert.deepEqual(wide.$wikiFamilies.map(family=>family.members).sort(),tall.$wikiFamilies.map(family=>family.members).sort());
    """, fixture)


def test_optional_array_larger_than_the_view_square_is_ignored_without_scanning_any_row() -> None:
    fixture = _graph(3, [])
    _run("""
      const base=layoutGraph(fixture.nodes,fixture.edges,'lr',1.4);
      const oversized=Array.from({length:fixture.nodes.length**2+1},()=>({source:'N000',target:'N001',
        get count(){assert.fail('An over-budget optional layer was scanned');}}));
      assert.equal(publicScene(layoutGraph(fixture.nodes,fixture.edges,'lr',1.4,oversized)),publicScene(base));
    """, fixture)


def test_extreme_finite_affinities_keep_every_counter_coordinate_and_association_finite() -> None:
    fixture = _graph(6, [])
    _run("""
      const wiki=[{source:'N000',target:'N003',count:Number.MAX_VALUE},
        {source:'N003',target:'N000',count:Number.MAX_VALUE},{source:'N001',target:'N004',count:1e300}];
      const scene=checkAffinity(fixture,wiki);
      assert(scene.$wikiEdges.every(edge=>Number.isFinite(edge.count)&&edge.count>0));checkWikiRoutes(scene);
    """, fixture)


def test_real_shared_formal_escape_geometry_is_preserved_when_its_whole_envelope_clusters() -> None:
    fixture = json.loads((_ROOT / "tests/fixtures/integrated-dag-layout-expanded.json").read_text())
    fixture["nodes"].append({"id": "unlinked", "width": 208, "height": 100})
    _run("""
      const wiki=[{source:fixture.nodes[0].id,target:'unlinked',count:4}],scene=checkAffinity(fixture,wiki);
      const parts=scene.routes.flatMap(route=>route.parts||[]);
      assert(parts.length>new Set(parts).size,'The real shared formal escape fixture lost its bounded cache');
      const soft=checkWikiRoutes(scene);assert.equal(soft.total,1);
    """, fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_dense_real_affinities_have_a_card_clear_local_backbone_and_bounded_work(direction: str) -> None:
    fixture = _graph(16, [(0, 1, 1, 1), (1, 0, 2, 3), (2, 3, 5, 7)])
    _run(f"""
      const wiki=[];for(let a=0;a<fixture.nodes.length;a++)for(let b=a+1;b<fixture.nodes.length;b++)
        wiki.push({{source:fixture.nodes[a].id,target:fixture.nodes[b].id,count:1+(a+b)%7}});
      const scene=checkAffinity(fixture,wiki,{json.dumps(direction)}),soft=checkWikiRoutes(scene);
      assert.equal(soft.total,wiki.length);assert(soft.omitted>0,'The dense fixture must exercise honest backbone omissions');
      assert(soft.routes.length<=2*fixture.nodes.length);assert(soft.routes.length>0);
    """, fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
@pytest.mark.skipif(sys.platform != "linux", reason="The 120-node stress runs on Linux CI, not the loaded development host")
def test_map_bound_sparse_affinities_keep_finite_work_and_every_original_route(direction: str) -> None:
    fixture = _graph(120, [(0, 1, 2, 3), (1, 2, 5, 7), (3, 4, 11, 13)])
    _run(f"""
      const wiki=[];for(let n=0;n<fixture.nodes.length-1;n++)
        wiki.push({{source:fixture.nodes[n].id,target:fixture.nodes[n+1].id,count:1}});
      const scene=checkAffinity(fixture,wiki,{json.dumps(direction)});assert.equal(scene.nodes.size,120);
      const soft=checkWikiRoutes(scene);assert(soft.routes.length<=240);
      assert.equal(layoutGraph(fixture.nodes.concat([{{id:'outside-cap',width:208,height:100}}]),fixture.edges,{json.dumps(direction)},1.4,wiki),null);
    """, fixture)
