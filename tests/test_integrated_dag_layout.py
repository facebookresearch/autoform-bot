"""Bounded production scene geometry, with an independent collision oracle.

These are source/geometry checks, not substitutes for genuine browser journeys.
The captured fixture records the rejected eleven-node mathematical projection.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")
_FIXTURE = _ROOT / "tests/fixtures/integrated-dag-layout-expanded.json"
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is required for source geometry checks")

_LOADER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(ROOT+'/autoform_cli/assets/blueprint-dag.js','utf8');
function original(name) {
  const start=source.indexOf('function '+name+'(');
  assert(start>=0,'Production function unavailable: '+name);
  for(let end=source.indexOf('}',start);end>=0;end=source.indexOf('}',end+1)) {
    const declaration=source.slice(start,end+1);
    try {new vm.Script('('+declaration+')');return declaration;}
    catch(error) {if(!(error instanceof SyntaxError))throw error;}
  }
  assert.fail('Production function did not parse: '+name);
}
const context={assert,structuredClone,URL,performance,productionSource:source,original,fixture:JSON.parse(fs.readFileSync(0,'utf8'))};
vm.createContext(context);
vm.runInContext(source.match(/var NODE_W =[^;]+;/)[0],context);
const functions=['layoutGraph','geometry','compactRegions'].map(original).join('\n');
"""

_ORACLE = r"""
function rectangleOverlap(a,b,pad=0) {
  return a.x<a.x+a.w && b.x<b.x+b.w && a.x<b.x+b.w+pad && a.x+a.w>b.x-pad && a.y<b.y+b.h+pad && a.y+a.h>b.y-pad;
}
function inside(point,box,pad=0) {
  return point.x>box.x-pad+1e-6 && point.x<box.x+box.w+pad-1e-6 && point.y>box.y-pad+1e-6 && point.y<box.y+box.h+pad-1e-6;
}
function cross(a,b,c) {return (b.x-a.x)*(c.y-a.y)-(b.y-a.y)*(c.x-a.x);}
function segmentCrosses(a,b,c,d) {
  return cross(a,b,c)*cross(a,b,d)<-1e-8 && cross(c,d,a)*cross(c,d,b)<-1e-8;
}
function lineHits(a,b,box,pad=0) {
  if(inside(a,box,pad)||inside(b,box,pad))return true;
  const corners=[{x:box.x-pad,y:box.y-pad},{x:box.x+box.w+pad,y:box.y-pad},
    {x:box.x+box.w+pad,y:box.y+box.h+pad},{x:box.x-pad,y:box.y+box.h+pad}];
  return corners.some((corner,index)=>segmentCrosses(a,b,corner,corners[(index+1)%4]));
}
function check(input,direction,produced) {
  const before=JSON.stringify(input),result=produced||layoutGraph(input.nodes,input.edges,direction);
  assert.equal(JSON.stringify(input),before,'Layout mutated captured graph data');
  const originalRoutes=result.routes;
  result.routes=originalRoutes.map(route=>route.parts?Object.assign({},route,{
    points:route.parts.flatMap(part=>part.points),radii:route.parts.flatMap(part=>part.radii)}):route);
  const boxes=Array.from(result.nodes.values());
  assert.deepEqual(boxes.map(box=>box.id).sort(),input.nodes.map(node=>node.id).sort());
  boxes.forEach((box,index)=>{
    assert([box.x,box.y,box.w,box.h].every(Number.isFinite));
    const node=input.nodes.find(node=>node.id===box.id);
    assert.equal(box.w,node.width);assert.equal(box.h,node.height);
    boxes.slice(index+1).forEach(other=>assert(!rectangleOverlap(box,other,48-1e-6),'Mathematical cards have inadequate separation'));
  });
  const expected=input.edges.flatMap(edge=>['statement','proof'].filter(kind=>edge[kind]>0).map(kind=>JSON.stringify([edge.source,edge.target,kind,edge[kind]]))).sort();
  assert.deepEqual(result.routes.map(route=>JSON.stringify([route.source,route.target,route.kind,route.count])).sort(),expected);
  const adjacent=new Map(input.nodes.map(node=>[node.id,[]]));
  input.edges.forEach(edge=>{if(edge.statement>0||edge.proof>0)adjacent.get(edge.source).push(edge.target);});
  const reachable=new Map(input.nodes.map(node=>{
    const found=new Set([node.id]),pending=[node.id];
    while(pending.length)for(const next of adjacent.get(pending.pop()))if(!found.has(next)){found.add(next);pending.push(next);}
    return [node.id,found];
  }));
  assert.deepEqual(result.clusters.flatMap(cluster=>cluster.members).sort(),input.nodes.map(node=>node.id).sort());
  boxes.forEach(a=>boxes.forEach(b=>assert.equal(a.cluster===b.cluster,reachable.get(a.id).has(b.id)&&reachable.get(b.id).has(a.id),'Structural clusters must be actual SCCs')));
  const labels=[],labelled=result.routes.filter(route=>route.label);
  result.routes.forEach(route=>{
    assert(route.points.length>=2);assert(route.points.every(point=>Number.isFinite(point.x)&&Number.isFinite(point.y)));
    route.points.slice(1).forEach((point,index)=>boxes.forEach(box=>assert(!lineHits(route.points[index],point,box,box.id===route.source||box.id===route.target?0:12),'An authoritative edge crosses a mathematical card')));
    route.radii.forEach((radius,index)=>{
      if(!radius)return;
      const point=route.points[index],before=route.points[index-1],after=route.points[index+1];
      const d1=Math.hypot(point.x-before.x,point.y-before.y),d2=Math.hypot(after.x-point.x,after.y-point.y);
      const p={x:point.x-(point.x-before.x)*radius/d1,y:point.y-(point.y-before.y)*radius/d1};
      const q={x:point.x+(after.x-point.x)*radius/d2,y:point.y+(after.y-point.y)*radius/d2};
      for(let step=0;step<=24;step++) {
        const t=step/24,at={x:(1-t)*(1-t)*p.x+2*t*(1-t)*point.x+t*t*q.x,y:(1-t)*(1-t)*p.y+2*t*(1-t)*point.y+t*t*q.y};
        boxes.forEach(box=>assert(!inside(at,box),'Rounded route enters a mathematical card'));
        labelled.forEach(other=>{
          if(other!==route&&other.label)assert(!inside(at,other.label,1),'Rounded route occludes another count label');
        });
      }
    });
    const tip=route.points.at(-1),prior=route.points.at(-2),length=Math.hypot(tip.x-prior.x,tip.y-prior.y);
    assert(length>0);assert(Math.abs(route.arrow.dx-(tip.x-prior.x)/length)<1e-7);assert(Math.abs(route.arrow.dy-(tip.y-prior.y)/length)<1e-7);
    const target=result.nodes.get(route.target);
    assert(tip.x>=target.x-1e-6&&tip.x<=target.x+target.w+1e-6&&tip.y>=target.y-1e-6&&tip.y<=target.y+target.h+1e-6);
    assert(Math.min(Math.abs(tip.x-target.x),Math.abs(tip.x-target.x-target.w),Math.abs(tip.y-target.y),Math.abs(tip.y-target.y-target.h))<1e-6);
    route.points.forEach(point=>assert(point.x>=route.bounds.x-1e-6&&point.x<=route.bounds.x+route.bounds.w+1e-6&&point.y>=route.bounds.y-1e-6&&point.y<=route.bounds.y+route.bounds.h+1e-6));
    if(route.label) {
      boxes.forEach(box=>assert(!rectangleOverlap(route.label,box,4),'Count label overlaps a card'));
      labels.forEach(label=>assert(!rectangleOverlap(route.label,label,4),'Count labels overlap'));labels.push(route.label);
    }
  });
  result.routes.forEach((route,index)=>{
    labelled.forEach(other=>{
      if(route===other||!other.label)return;
      route.points.slice(1).forEach((point,at)=>assert(!lineHits(route.points[at],point,other.label,4),'Another edge occludes a count label'));
    });
  });
  // Shared flow corridors are intentional; geometry storage and primitive work remain bounded.
  const count=input.nodes.length,lanes=result.routes.length,parts=new Set(originalRoutes.flatMap(route=>route.parts||[]));
  assert.equal(result.work.ticks,count?32:0);assert.equal(result.work.pairs,32*count*(count-1)/2);
  assert(result.work.moves<=96*count*count);assert(result.work.candidates<=15*lanes*count);
  assert(result.work.candidates<=90*count*count);
  assert(result.work.escapes<=4*count*count*(count+1));
  assert(parts.size<=4*count+lanes);
  assert(Array.from(parts).reduce((sum,part)=>sum+part.points.length,0)<=4*count*(3*count+4)+4*lanes);
  return result;
}
function normal(result) {
  return JSON.stringify({nodes:Array.from(result.nodes.values()).sort((a,b)=>a.id<b.id?-1:1),routes:result.routes.map(route=>({source:route.source,target:route.target,kind:route.kind,count:route.count,points:route.points,label:route.label,arrow:route.arrow}))});
}
"""


def _run(body: str, fixture: object, *, timed_body: str | None = None) -> None:
    script = "const ROOT=" + json.dumps(str(_ROOT)) + ";\n" + _LOADER
    script += "context.report=(stage,details={})=>process.stdout.write(JSON.stringify({stage,...details})+'\\n');\n"
    script += "vm.runInContext(functions+" + json.dumps(_ORACLE) + ",context,{timeout:2000});\n"
    if timed_body is not None:
        script += "vm.runInContext(" + json.dumps(timed_body) + ",context,{timeout:2000});\n"
    oracle_budget = 5000 if timed_body is not None else 2000
    script += "vm.runInContext(" + json.dumps(body) + f",context,{{timeout:{oracle_budget}}});"
    result = subprocess.run([_NODE, "-e", script], input=json.dumps(fixture), text=True, capture_output=True,
                            timeout=10 if timed_body is not None else 5)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def _graph(count: int, pairs: list[tuple[int, int, int, int]]) -> dict:
    return {
        "nodes": [{"id": f"N{index:03d}", "width": 180 + index % 3 * 32, "height": 100 + index % 2 * 24,
                   "status": "planned", "route": f"chapter.html#n{index}"} for index in range(count)],
        "edges": [{"source": f"N{source:03d}", "target": f"N{target:03d}", "statement": statement, "proof": proof}
                  for source, target, statement, proof in pairs],
    }


@pytest.mark.parametrize("direction", ["lr", "tb"])
@pytest.mark.parametrize("fixture", [
    _graph(5, [(0, 1, 1, 0), (1, 2, 1, 0), (2, 3, 0, 1), (0, 3, 7, 4)]),
    _graph(6, [(0, 1, 2, 0), (0, 2, 0, 3), (1, 3, 1, 0), (2, 3, 0, 1), (0, 3, 11, 13), (3, 4, 1, 1)]),
    _graph(5, [(0, 1, 3, 2), (1, 2, 0, 5), (2, 0, 7, 0), (2, 3, 1, 0)]),
    _graph(3, [(0, 1, 23, 17), (1, 0, 19, 11)]),
    _graph(3, [(0, 0, 5, 3), (0, 1, 1, 0)]),
    _graph(6, [(0, 1, 1, 0), (2, 3, 0, 1)]),
])
def test_directed_cycles_disconnected_and_skipping_edges_keep_clear_routes(direction: str, fixture: dict) -> None:
    _run(f"check(fixture,{json.dumps(direction)});", fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_sixty_one_isolates_pack_in_two_dimensions_without_changing_connected_ranks(direction: str) -> None:
    fixture = _graph(65, [(0, 1, 2, 0), (0, 2, 0, 3), (1, 3, 5, 7), (2, 3, 11, 0)])
    for node in fixture["nodes"]:
        node.update(area="One authored topic", scope="One authored scope")
    _run(f"""
      const direction={json.dumps(direction)},before=JSON.stringify(fixture),began=performance.now();
      const scene=layoutGraph(fixture.nodes,fixture.edges,direction,1.4);
      assert(performance.now()-began<1000,'Disconnected packing exceeded the unchanged one-second layout budget');
      const packed=check(fixture,direction,scene);
      const connected={{nodes:fixture.nodes.slice(0,4),edges:fixture.edges}},alone=check(connected,direction);
      const isolated=Array.from(packed.nodes.values()).filter(box=>!connected.nodes.some(node=>node.id===box.id));
      assert.equal(isolated.length,61);assert(isolated.every(box=>box.rank===0));
      assert(new Set(isolated.map(box=>Math.round(box.x))).size>=5,'Unlinked topics still occupy one column');
      assert(new Set(isolated.map(box=>Math.round(box.y))).size>=5,'Unlinked topics still occupy one row');
      assert(packed.bounds.w/packed.bounds.h>.5&&packed.bounds.w/packed.bounds.h<3,
        'The bounded component pack must not fit an arbitrarily tall or wide strip');
      assert.deepEqual(connected.nodes.map(node=>packed.nodes.get(node.id).rank),[0,1,1,2]);
      const anchor=packed.nodes.get('N000'),originalAnchor=alone.nodes.get('N000');
      const dx=anchor.x-originalAnchor.x,dy=anchor.y-originalAnchor.y;
      connected.nodes.forEach(node=>{{
        const actual=packed.nodes.get(node.id),original=alone.nodes.get(node.id);
        assert(Math.abs(actual.x-original.x-dx)<1e-7&&Math.abs(actual.y-original.y-dy)<1e-7,
          'Unrelated topics changed a connected component instead of only translating it');
        assert.equal(actual.rank,original.rank);
      }});
      alone.routes.forEach((route,index)=>{{
        const actual=packed.routes[index];assert.equal(actual.points.length,route.points.length);
        actual.points.forEach((point,at)=>assert(Math.abs(point.x-route.points[at].x-dx)<1e-7&&Math.abs(point.y-route.points[at].y-dy)<1e-7));
        assert.deepEqual(actual.radii,route.radii);assert.equal(actual.arrow.dx,route.arrow.dx);assert.equal(actual.arrow.dy,route.arrow.dy);
        assert(Math.abs(actual.arrow.x-route.arrow.x-dx)<1e-7&&Math.abs(actual.arrow.y-route.arrow.y-dy)<1e-7);
      }});
      assert.equal(JSON.stringify(fixture),before);
    """, fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_stage_aspect_packs_weak_components_without_redefining_sccs_or_shared_routes(direction: str) -> None:
    fixture = _graph(24, [(0, 1, 2, 3), (1, 0, 5, 7), (1, 2, 11, 13), (3, 3, 17, 19),
                          (0, 1, 23, 29)])
    _run(f"""
      const direction={json.dumps(direction)};
      const tall=check(fixture,direction,layoutGraph(fixture.nodes,fixture.edges,direction,.5));
      const wide=check(fixture,direction,layoutGraph(fixture.nodes,fixture.edges,direction,2));
      assert(wide.bounds.w/wide.bounds.h>tall.bounds.w/tall.bounds.h,
        'The explicit physical stage aspect must influence disconnected packing');
      assert.equal(wide.nodes.get('N000').cluster,wide.nodes.get('N001').cluster);
      assert.notEqual(wide.nodes.get('N001').cluster,wide.nodes.get('N002').cluster);
      assert.equal(wide.nodes.get('N002').rank,1);assert.equal(wide.nodes.get('N003').rank,0);
      assert.equal(wide.routes.length,10,'Packing must retain duplicate and self-loop typed dependencies');
      const duplicates=wide.routes.filter(route=>route.source==='N000'&&route.target==='N001'&&route.kind==='statement');
      assert.equal(duplicates.length,2);assert.strictEqual(duplicates[0].bounds,duplicates[1].bounds,
        'Packing must retain the existing bounded route-geometry cache');
      const shuffled={{nodes:fixture.nodes.slice().reverse(),edges:fixture.edges.slice().reverse()}};
      assert.equal(normal(wide),normal(check(shuffled,direction,layoutGraph(shuffled.nodes,shuffled.edges,direction,2))),
        'Component placement must not depend on input ordering');
      const connected={{nodes:fixture.nodes.slice(0,3),edges:fixture.edges.filter(edge=>edge.source!=='N003')}};
      assert.equal(normal(layoutGraph(connected.nodes,connected.edges,direction,.5)),
        normal(layoutGraph(connected.nodes,connected.edges,direction,2)),
        'Stage aspect cannot rearrange a single connected mathematical graph');
      const defaultScene=layoutGraph(fixture.nodes,fixture.edges,direction);
      [0,-1,Infinity,NaN,'2',()=>assert.fail('An obsolete label callback was invoked')].forEach(aspect=>
        assert.equal(normal(defaultScene),normal(layoutGraph(fixture.nodes,fixture.edges,direction,aspect))));
    """, fixture)


def test_component_pack_retains_valid_sparse_shared_escape_envelopes() -> None:
    # This captured LR geometry genuinely reuses escapes. The same graph in TB
    # has clear local routes instead, covered by the independent orientation tests.
    direction = "lr"
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    _run(f"""
      const direction={json.dumps(direction)},base=check(fixture,direction);
      const originalParts=base.routes.flatMap(route=>route.parts||[]);
      assert(originalParts.length>new Set(originalParts).size,
        'The existing valid sparse fixture must genuinely exercise shared obstacle escapes');
      const extended={{nodes:fixture.nodes.concat([{{id:'fixture:unlinked',width:208,height:100}}]),edges:fixture.edges}};
      const scene=check(extended,direction);assert.equal(scene.routes.length,33);
      base.routes.forEach((route,index)=>{{
        const actual=scene.routes[index],from=base.nodes.get(route.source),to=scene.nodes.get(route.source);
        const dx=to.x-from.x,dy=to.y-from.y;
        assert.equal(actual.points.length,route.points.length);
        actual.points.forEach((point,at)=>assert(Math.abs(point.x-route.points[at].x-dx)<1e-7&&Math.abs(point.y-route.points[at].y-dy)<1e-7,
          'Component packing changed a valid route instead of translating its complete envelope'));
        if(route.parts) {{
          assert(actual.parts);assert.equal(actual.parts.length,route.parts.length);
          route.parts.forEach((part,at)=>assert.deepEqual(actual.parts[at].radii,part.radii));
        }}
      }});
      const sharedParts=scene.routes.flatMap(route=>route.parts||[]);
      assert(sharedParts.length>new Set(sharedParts).size,'Packing discarded the existing shared escape cache');
      const shuffled={{nodes:extended.nodes.slice().reverse(),edges:extended.edges.slice().reverse()}};
      const replay=check(shuffled,direction);
      assert.equal(normal(scene),normal(replay));
      scene.routes.forEach((route,index)=>assert.deepEqual(route.parts,replay.routes[index].parts));
    """, fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_the_reviewed_four_node_cycle_keeps_every_real_lane_and_valid_geometry(direction: str) -> None:
    fixture = {
        "nodes": [{"id": f"A{number}", "width": 208, "height": 100} for number in range(4)],
        "edges": [{"source": "A0", "target": "A3", "statement": 1, "proof": 1},
                  {"source": "A3", "target": "A0", "statement": 1, "proof": 2},
                  {"source": "A2", "target": "A0", "statement": 0, "proof": 1},
                  {"source": "A2", "target": "A1", "statement": 0, "proof": 1}],
    }
    _run(f"const result=check(fixture,{json.dumps(direction)});assert.equal(result.routes.length,6);", fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_captured_eleven_node_expansion_retains_every_weight_without_card_crossings(direction: str) -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert len(fixture["nodes"]) == 11 and len(fixture["edges"]) == 22
    _run(f"const result=check(fixture,{json.dumps(direction)});assert.equal(result.routes.length,33);", fixture)


@pytest.mark.parametrize(("direction", "width", "height"), [("lr", 264, 148), ("tb", 312, 96)])
def test_captured_five_area_root_keeps_all_cross_area_weights_and_clear_routes(direction: str, width: int, height: int) -> None:
    captured = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    owners = {node["id"]: node["area"] for node in captured["nodes"]}
    areas = list(dict.fromkeys(owners.values()))
    links: dict[tuple[str, str], dict] = {}
    for edge in captured["edges"]:
        source, target = owners[edge["source"]], owners[edge["target"]]
        if source == target:  # Internal dependencies belong to the expanded area, not a root self-loop.
            continue
        aggregate = links.setdefault((source, target), {"source": "region:" + source, "target": "region:" + target,
                                                        "statement": 0, "proof": 0})
        for kind in ("statement", "proof"):
            aggregate[kind] += edge[kind]
    fixture = {"nodes": [{"id": "region:" + area, "width": width, "height": height} for area in areas],
               "edges": list(links.values())}
    assert len(fixture["nodes"]) == 5 and len(fixture["edges"]) == 12
    assert sum(edge[kind] > 0 for edge in fixture["edges"] for kind in ("statement", "proof")) == 19
    for kind, total in (("statement", 98), ("proof", 218)):
        assert sum(edge[kind] for edge in fixture["edges"]) == total
        assert sum(edge[kind] for edge in captured["edges"] if owners[edge["source"]] != owners[edge["target"]]) == total
    _run(f"const result=check(fixture,{json.dumps(direction)});assert.equal(result.routes.length,19);", fixture)


@pytest.mark.parametrize("entry", ["roadmap", "resultA"])
def test_cold_mount_seeds_its_route_and_projects_the_loaded_index_before_layout(entry: str) -> None:
    """Execute the real cold-mount prefix, with DOM seams isolated but the actual projection and engine."""
    _run(r"""
      const events=[],expected=fixture.entry==='roadmap'?['region:Algebra','region:Topology']:['resultA','resultB'];
      const scopes=Array.from({length:12},(_,number)=>({id:'scope-'+number,title:'Topic '+number,
        scope:'scope-'+number,kind:'scope',navigation:'module',status:'planned',area:number<6?'Algebra':'Topology'}));
      const results=['resultA','resultB'].map((id,number)=>({id,title:id,scope:'scope-'+number*6,kind:'node',status:'planned',result:true}));
      const index={nodes:[{id:'roadmap',scope:'roadmap',title:'Mathematics',status:'planned'},...scopes,...results],
        relations:[['resultA','resultB',2,1]]};
      const payload={schema:'autoform-dag-view/v2',title:'Mathematics',palette:{planned:{label:'planned'}},
        nodes:scopes.map(node=>({...node,id:'scope:'+node.id,member_count:1,status_counts:[['planned',1]],
          layout:{width:232,height:116}})),edges:[{source:'scope:scope-0',target:'scope:scope-6',statement:2,proof:1}],
        view:{presentation:'atlas',breadcrumbs:[]},regions:[],present_statuses:['planned']};
      const firstBundle={nodeId:fixture.entry,graph:payload,readingSource:{},dataURL:'https://example.test/dependencies.json'};
      const navigation={getIndex(){events.push('index');return index;}};
      const host={querySelector(){return null;},classList:{add(){},remove(){},toggle(){}}};
      const window={location:{href:'https://example.test/roadmap/index.html',origin:'https://example.test'}};
      const constants=productionSource.slice(productionSource.indexOf('  var NODE_W ='),productionSource.indexOf('  var clients ='));
      eval(constants);
      const start=productionSource.indexOf('  function mount('),end=productionSource.indexOf('    function globalListen(',start);
      assert(start>=0&&end>start,'The real cold-mount boundary must exist');
      const helpers=['safeHref','graphAt','useIndex','updateProjection','neighborhood','neighborIds','regionProjection',
        'geometry','compactRegions'].map(original).join('\n');
      const seams=`
        function buildShell() {events.push('shell');family='sans-serif';dom$stage={clientWidth:900};
          dom$ctx={font:'11px sans-serif',measureText:text=>({width:String(text).length*8})};
          dom$search={};dom$breadcrumb={replaceChildren(){}};}
        function bindEvents() {events.push('bind');}
        function sheetLayout() {return false;} function renderNavigation() {} function renderReaderContext() {}
        function renderInventory() {} function refresh() {} function routeTrail() {return [];} function trailList() {return [];}
        function centerIfHidden() {}
        function applyRoute(bundle) {assert.strictEqual(bundle,firstBundle);events.push('route');return Promise.resolve();}
        function record(phase) {
          assert.strictEqual(sharedIndex,index,"Cold layout must use the router's validated index");
          assert.equal(ui$requestedNode,fixture.entry,'First fit must retain the canonical requested route');
          assert.deepEqual(data.nodes.map(node=>node.id).sort(),expected,'Raw scopes or an unrelated neighborhood reached layout');
          const current=geometry(),verified=check(data,'lr');
          assert.equal(normal(current),normal(verified),'Cold layout must run the production engine and clear-route oracle');
          events.push({phase,nodes:current.nodes.size,lanes:current.routes.length});
        }
        function fit() {record('fit');} function resize() {record('resize');}
      `;
      const bootstrap=eval('('+productionSource.slice(start,end)+'\nreturn ui$requestedNode;\n'+helpers+'\n'+seams+'})');
      assert.equal(bootstrap(host,payload,1,firstBundle,navigation),fixture.entry);
      const phase=name=>events.findIndex(event=>typeof event==='string'?event===name:event.phase===name);
      assert(phase('shell')<phase('bind')&&phase('bind')<phase('index')&&phase('index')<phase('fit'));
      assert(phase('fit')<phase('resize')&&phase('resize')<phase('route'));
      assert(events.filter(event=>event==='index').length===1);
      assert.throws(()=>bootstrap(host,payload,1,firstBundle,null),/complete mathematical page and navigation/);
      assert.throws(()=>bootstrap(host,payload,1,null,navigation),/complete mathematical page and navigation/);
      assert.throws(()=>bootstrap(host,payload,1,{graph:payload},navigation),/complete mathematical page and navigation/);
    """, {"entry": entry})


def test_bare_or_navigationless_hosts_do_not_start_an_interactive_client() -> None:
    """Execute production boot gating: unsupported hosts keep their linked static DOM."""
    _run("""
      const effects=[],window={AutoformNavigation:{create(){effects.push('create');}}};
      const host={getAttribute(){return null;},closest(){return null;},
        setAttribute(){effects.push('ready');},classList:{add(){effects.push('enhancing');}}};
      eval(original('start'));start(host);
      assert.deepEqual(effects,[],'A bare graph host must neither fetch nor replace its fallback');
      window.AutoformNavigation=null;host.closest=()=>({});start(host);
      assert.deepEqual(effects,[],'An unavailable router must preserve the complete static reading page');
    """, {})


def test_cold_mount_measures_full_page_framing_and_error_restores_native_reading() -> None:
    """Run real boot ordering over tiny DOM/router seams; browser containment remains independent."""
    _run("""
      const events=[],bodyClasses=new Set(),hostClasses=new Set(),loading={hidden:true,classList:{add(){}}};
      const classes=set=>({add(name){set.add(name);},remove(name){set.delete(name);},contains(name){return set.has(name);}});
      const document={body:{classList:classes(bodyClasses)}};
      function host(){const attributes=new Map();return {isConnected:true,
        getAttribute(name){return attributes.get(name)||null;},setAttribute(name,value){attributes.set(name,value);},closest(){return {};},
        querySelector(){return loading;},classList:classes(hostClasses)};}
      const clients=new Map();let fail=false,options,hostSequence=0;
      const window={AutoformNavigation:{create(value){options=value;return {
        start(){try{options.onCommit({graph:{},readingSource:{}});}catch(error){options.onError(error);}},dispose(){}};}}};
      function mount(){assert(bodyClasses.has('bp-dag-app-page'),'Initial geometry must see the actual full-page viewport');
        events.push('layout');if(fail)throw Error('controlled mount failure');return {ready:Promise.resolve()};}
      eval(original('start'));start(host());
      assert.deepEqual(events,['layout']);assert(bodyClasses.has('bp-dag-app-page'));
      fail=true;clients.clear();bodyClasses.clear();start(host());
      assert.deepEqual(events,['layout','layout']);assert(!bodyClasses.has('bp-dag-app-page'));
      assert(!hostClasses.has('bp-dag-enhancing'));assert(loading.textContent.includes('complete mathematical page remains readable'));
    """, {})


def test_layout_replays_deterministically_after_input_permutation() -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    _run("""
      const first=check(fixture,'lr'),again=check(fixture,'lr');assert.equal(normal(first),normal(again));
      const shuffled={nodes:fixture.nodes.slice().reverse(),edges:fixture.edges.slice().reverse()};
      assert.equal(normal(first),normal(check(shuffled,'lr')));
    """, fixture)


def test_renderer_geometry_cache_changes_only_for_captured_geometry_inputs() -> None:
    _run("""
      var navigation={},mapAllowed=true,atlas=false,ui$area=null,ui$orientation='lr',family='sans-serif';
      var dom$stage={clientWidth:760},dom$ctx={font:'12px sans-serif',measureText:text=>({width:text.length*8})};
      var data=fixture,scene,sceneData,sceneDirection,sceneCompact,sceneFamily,sceneAspect,calls=0;
      const actual=layoutGraph;layoutGraph=function(...args){calls++;return actual(...args);};
      const first=geometry();assert.strictEqual(geometry(),first);assert.equal(calls,1);
      var ui$transform={x:41,y:-73,scale:1.2},ui$selectedStatuses=new Set(['planned']);
      assert.strictEqual(geometry(),first);assert.equal(calls,1);
      ui$orientation='tb';assert.notStrictEqual(geometry(),first);assert.equal(calls,2);
      data=JSON.parse(JSON.stringify(fixture));geometry();assert.equal(calls,3);
      mapAllowed=false;assert.equal(geometry(),null);assert.equal(calls,3);
    """, _graph(4, [(0, 1, 3, 1), (1, 2, 0, 5)]))


def test_oversized_draw_does_not_dereference_unavailable_geometry() -> None:
    _run("""
      var navigation={},mapAllowed=false,dom$stage={clientWidth:760,clientHeight:740},cleared=0;
      var dom$ctx={setTransform(){},clearRect(){cleared++;}},window={devicePixelRatio:1};
      function calculateActive() {} function drawRoute() {assert.fail('Unavailable geometry reached its painter');}
      eval('('+original('draw')+')')();assert.equal(cleared,1);
    """, None)


def test_the_scene_bound_is_enforced_before_any_layout_work() -> None:
    _run("assert.equal(layoutGraph(fixture.nodes,fixture.edges,'lr'),null);", _graph(121, []))


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_small_variable_rectangles_keep_ports_on_their_real_boundaries(direction: str) -> None:
    fixture = {"nodes": [{"id": "tiny", "width": 2, "height": 1}, {"id": "large", "width": 264, "height": 148}],
               "edges": [{"source": "tiny", "target": "large", "statement": 1, "proof": 2},
                         {"source": "large", "target": "tiny", "statement": 3, "proof": 4}]}
    _run(f"check(fixture,{json.dumps(direction)});", fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_cyclic_links_face_their_actual_target_without_perimeter_detours(direction: str) -> None:
    """Direction comes from the real endpoints, not a DAG order a cycle cannot have."""
    fixture = _graph(2, [(0, 1, 2, 3), (1, 0, 5, 7)])
    _run(f"""
      const direction={json.dumps(direction)},scene=check(fixture,direction);
      scene.routes.forEach(route=>{{
        assert(!route.detoured,'Two connected cards need no whole-scene return corridor');
        const a=scene.nodes.get(route.source),b=scene.nodes.get(route.target),first=route.points[0],tip=route.points.at(-1);
        const dx=b.x+b.w/2-a.x-a.w/2,dy=b.y+b.h/2-a.y-a.h/2;
        assert(first.x>=a.x&&first.x<=a.x+a.w&&first.y>=a.y&&first.y<=a.y+a.h);
        assert(Math.min(Math.abs(first.x-a.x),Math.abs(first.x-a.x-a.w),Math.abs(first.y-a.y),Math.abs(first.y-a.y-a.h))<1e-7,
          'The route must leave a real source boundary');
        const departure=route.points[1];
        assert((departure.x-first.x)*dx+(departure.y-first.y)*dy>0,'Source approach must face its actual target');
        assert(route.arrow.dx*dx+route.arrow.dy*dy>0,'Target approach must flow towards its actual target');
        const travelled=route.points.slice(1).reduce((sum,point,index)=>sum+Math.hypot(point.x-route.points[index].x,point.y-route.points[index].y),0);
        const direct=Math.abs(first.x-tip.x)+Math.abs(first.y-tip.y);
        assert(travelled<=direct+96,'A clear cyclic link should not take a scenic route');
      }});
      assert.equal(scene.routes.length,4);assert.equal(scene.clusters.length,1);
    """, fixture)


def test_weighted_relaxation_is_finite_deterministic_and_not_driven_by_label_measurement() -> None:
    _run("""
      const first=check(fixture,'lr'),again=check(fixture,'lr');assert.equal(normal(first),normal(again));
      assert.equal(normal(first),normal(layoutGraph(fixture.nodes,fixture.edges,'lr',()=>{throw Error('Labels drove geometry');})));
      const stronger=structuredClone(fixture);stronger.edges[0].statement=100;
      const changed=check(stronger,'lr');
      assert.notEqual(JSON.stringify(Array.from(first.nodes.values())),JSON.stringify(Array.from(changed.nodes.values())));
      assert(changed.routes[0].strength>first.routes[0].strength);
      assert(changed.routes.every(route=>route.label===null));
    """, _graph(2, [(0, 1, 1, 0)]))


def test_large_library_aggregates_keep_distinct_ordered_flow_weights() -> None:
    _run("""
      const scene=check(fixture,'lr'),routes=scene.routes.slice().sort((a,b)=>a.count-b.count);
      assert.deepEqual(routes.map(route=>route.count),[100,200,10000]);
      routes.forEach(route=>assert(Number.isFinite(route.strength)&&route.strength>1&&route.strength<=4.8));
      assert(routes[1].strength-routes[0].strength>.15,'Distinct library aggregates should not saturate to identical line weights');
      assert(routes[2].strength-routes[1].strength>.75,'The dominant dependency flow should stand out at a glance');
      assert(routes.every(route=>route.label===null));
    """, _graph(4, [(0, 1, 100, 0), (0, 2, 200, 0), (0, 3, 10000, 0)]))


def test_authoritative_ids_cannot_collide_with_engine_object_prototypes() -> None:
    ids = ["__proto__", "constructor", "toString", "scope:authored", "boundary:authored", "unicode/é"]
    fixture = {
        "nodes": [{"id": node_id, "width": 208, "height": 100} for node_id in ids],
        "edges": [{"source": source, "target": target, "statement": 2, "proof": 3}
                  for source, target in zip(ids, ids[1:])],
    }
    _run("check(fixture,'lr');check(fixture,'tb');", fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
@pytest.mark.skipif(sys.platform != "linux", reason="Map-bound stress runs only on Linux CI, never the overloaded development host")
def test_the_existing_map_bound_handles_a_hundred_twenty_real_nodes(direction: str) -> None:
    _run(f"check(fixture,{json.dumps(direction)});", _graph(120, [(0, target, 1, int(target % 3 == 0)) for target in range(1, 120)]))


@pytest.mark.parametrize(("count", "edges"), [(60, 180), (85, 250)])
@pytest.mark.parametrize("direction", ["lr", "tb"])
@pytest.mark.skipif(sys.platform != "linux", reason="Dense performance regressions run only on Linux CI")
def test_dense_cyclic_scopes_have_bounded_work_and_preserve_all_routes(count: int, edges: int, direction: str) -> None:
    rng = random.Random(442)
    pairs = {(0, 3): (1, 1), (3, 0): (1, 2), (2, 0): (0, 1), (2, 1): (0, 1)}
    while len(pairs) < edges:
        source, target = rng.randrange(count), rng.randrange(count)
        if source == target or (source, target) in pairs:
            continue
        pairs[source, target] = (rng.randrange(6), rng.randrange(1, 6))
    fixture = _graph(count, [(source, target, *pairs[source, target]) for source, target in sorted(pairs)])
    _run(f"""
      report('oracle-start',{{direction:{json.dumps(direction)},nodes:fixture.nodes.length,edges:fixture.edges.length}});
      const oracleBegan=performance.now(),result=check(fixture,{json.dumps(direction)},scene);
      assert.equal(result.routes.length,productionLanes);
      report('oracle-complete',{{milliseconds:performance.now()-oracleBegan,lanes:result.routes.length}});
    """, fixture, timed_body=f"""
      report('production-start',{{direction:{json.dumps(direction)},nodes:fixture.nodes.length,edges:fixture.edges.length}});
      const beforeProduction=JSON.stringify(fixture),began=performance.now();
      const scene=layoutGraph(fixture.nodes,fixture.edges,{json.dumps(direction)}),elapsed=performance.now()-began;
      const productionLanes=scene.routes.length;
      report('production-complete',{{milliseconds:elapsed,lanes:productionLanes}});
      assert(elapsed<1000,'The bounded production layout exceeded one second: '+elapsed+'ms');
      assert.equal(JSON.stringify(fixture),beforeProduction,'Timed production layout mutated captured graph input');
      report('production-input-unchanged');
    """)


def test_timed_scene_is_checked_without_repeating_the_solver() -> None:
    _run("""
      report('oracle-start');check(fixture,'lr',scene);assert.equal(calls,1);
      assert.equal(JSON.stringify(fixture),beforeProduction);report('oracle-complete');
    """, _graph(2, [(0, 1, 3, 2)]), timed_body="""
      var calls=0;const actual=layoutGraph;layoutGraph=(...args)=>{calls++;return actual(...args);};
      const beforeProduction=JSON.stringify(fixture),began=performance.now();
      const scene=layoutGraph(fixture.nodes,fixture.edges,'lr');
      assert(performance.now()-began<1000);assert.equal(calls,1);
      assert.equal(JSON.stringify(fixture),beforeProduction);report('production-input-unchanged');
    """)
