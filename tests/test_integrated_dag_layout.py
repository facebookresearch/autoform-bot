"""Production Dagre adapter geometry, with an independent collision oracle.

These are source/geometry checks, not substitutes for genuine browser journeys.
The captured fixture records the rejected eleven-node mathematical projection.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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
const context={assert,structuredClone,URL,productionSource:source,original,fixture:JSON.parse(fs.readFileSync(0,'utf8'))};
vm.createContext(context);
vm.runInContext(fs.readFileSync(ROOT+'/autoform_cli/assets/blueprint-dagre.vendor.js','utf8'),context);
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
function check(input,direction) {
  const before=JSON.stringify(input),result=layoutGraph(input.nodes,input.edges,direction);
  assert.equal(JSON.stringify(input),before,'Layout mutated captured graph data');
  const boxes=Array.from(result.nodes.values());
  assert.deepEqual(boxes.map(box=>box.id).sort(),input.nodes.map(node=>node.id).sort());
  boxes.forEach((box,index)=>{
    assert([box.x,box.y,box.w,box.h].every(Number.isFinite));
    const node=input.nodes.find(node=>node.id===box.id);
    assert.equal(box.w,node.width);assert.equal(box.h,node.height);
    boxes.slice(index+1).forEach(other=>assert(!rectangleOverlap(box,other,32),'Mathematical cards have inadequate separation'));
  });
  const expected=input.edges.flatMap(edge=>['statement','proof'].filter(kind=>edge[kind]>0).map(kind=>JSON.stringify([edge.source,edge.target,kind,edge[kind]]))).sort();
  assert.deepEqual(result.routes.map(route=>JSON.stringify([route.source,route.target,route.kind,route.count])).sort(),expected);
  const labels=[];
  result.routes.forEach(route=>{
    assert(route.points.length>=2);assert(route.points.every(point=>Number.isFinite(point.x)&&Number.isFinite(point.y)));
    const unrelated=boxes.filter(box=>box.id!==route.source&&box.id!==route.target);
    route.points.slice(1).forEach((point,index)=>unrelated.forEach(box=>assert(!lineHits(route.points[index],point,box,12),'An authoritative edge crosses another card')));
    route.radii.forEach((radius,index)=>{
      if(!radius)return;
      const point=route.points[index],before=route.points[index-1],after=route.points[index+1];
      const d1=Math.hypot(point.x-before.x,point.y-before.y),d2=Math.hypot(after.x-point.x,after.y-point.y);
      const p={x:point.x-(point.x-before.x)*radius/d1,y:point.y-(point.y-before.y)*radius/d1};
      const q={x:point.x+(after.x-point.x)*radius/d2,y:point.y+(after.y-point.y)*radius/d2};
      for(let step=0;step<=24;step++) {
        const t=step/24,at={x:(1-t)*(1-t)*p.x+2*t*(1-t)*point.x+t*t*q.x,y:(1-t)*(1-t)*p.y+2*t*(1-t)*point.y+t*t*q.y};
        boxes.forEach(box=>assert(!inside(at,box),'Rounded route enters a mathematical card'));
        result.routes.forEach(other=>{
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
    if(route.count>1) {
      assert(route.label);boxes.forEach(box=>assert(!rectangleOverlap(route.label,box,4),'Count label overlaps a card'));
      labels.forEach(label=>assert(!rectangleOverlap(route.label,label,4),'Count labels overlap'));labels.push(route.label);
    }
    else assert.equal(route.label,null);
  });
  result.routes.forEach((route,index)=>{
    result.routes.forEach(other=>{
      if(route===other||!other.label)return;
      route.points.slice(1).forEach((point,at)=>assert(!lineHits(route.points[at],point,other.label,4),'Another edge occludes a count label'));
    });
    result.routes.slice(index+1).forEach(other=>{
      route.points.slice(1).forEach((b,at)=>other.points.slice(1).forEach((d,next)=>{
        const a=route.points[at],c=other.points[next];
        if(Math.abs(cross(a,b,c))>1e-6||Math.abs(cross(a,b,d))>1e-6)return;
        const dx=b.x-a.x,dy=b.y-a.y,length=Math.hypot(dx,dy);if(!length)return;
        const p=((c.x-a.x)*dx+(c.y-a.y)*dy)/length,q=((d.x-a.x)*dx+(d.y-a.y)*dy)/length;
        const shared=Math.max(0,Math.min(length,Math.max(p,q))-Math.max(0,Math.min(p,q)));
        assert(shared<=80,'Unrelated routes collapse into a long shared spine');
      }));
    });
  });
  return result;
}
function normal(result) {
  return JSON.stringify({nodes:Array.from(result.nodes.values()).sort((a,b)=>a.id<b.id?-1:1),routes:result.routes.map(route=>({source:route.source,target:route.target,kind:route.kind,count:route.count,points:route.points,label:route.label,arrow:route.arrow}))});
}
"""


def _run(body: str, fixture: object) -> None:
    script = "const ROOT=" + json.dumps(str(_ROOT)) + ";\n" + _LOADER
    script += "vm.runInContext(functions+" + json.dumps(_ORACLE + body) + ",context,{timeout:20000});"
    result = subprocess.run([_NODE, "-e", script], input=json.dumps(fixture), text=True, capture_output=True, timeout=25)
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
      const firstBundle={nodeId:fixture.entry,graph:payload,dataURL:'https://example.test/dependencies.json'};
      const navigation={getIndex(){events.push('index');return index;}};
      const host={querySelector(){return null;},classList:{remove(){},toggle(){}}};
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
        function markIntro() {} function bindEvents() {events.push('bind');}
        function sheetLayout() {return false;} function renderNavigation() {} function renderReaderContext() {}
        function renderInventory() {} function refresh() {} function routeTrail() {return [];} function trailList() {return [];}
        function centerIfHidden() {} function redirectLegacyFocus() {events.push('legacy');}
        function applyRoute(bundle) {assert.strictEqual(bundle,firstBundle);events.push('route');return Promise.resolve();}
        function record(phase) {
          assert.strictEqual(sharedIndex,index,"Cold layout must use the router's validated index");
          assert.equal(ui$requestedNode,fixture.entry,'First fit must retain the canonical requested route');
          assert.deepEqual(data.nodes.map(node=>node.id).sort(),expected,'Raw scopes or an unrelated neighborhood reached layout');
          const current=geometry(),verified=check(data,'lr');
          assert.equal(normal(current),normal(verified),'Cold layout must run the production engine and clear-route oracle');
          events.push({phase,nodes:current.nodes.size,lanes:current.routes.length});
        }
        function fit() {record('fit');} function applyLocation() {record('location');} function resize() {record('resize');}
      `;
      const bootstrap=eval('('+productionSource.slice(start,end)+'\nreturn ui$requestedNode;\n'+helpers+'\n'+seams+'})');
      assert.equal(bootstrap(host,payload,1,firstBundle,navigation),fixture.entry);
      const phase=name=>events.findIndex(event=>typeof event==='string'?event===name:event.phase===name);
      assert(phase('shell')<phase('bind')&&phase('bind')<phase('index')&&phase('index')<phase('fit'));
      assert(phase('fit')<phase('location')&&phase('location')<phase('resize')&&phase('resize')<phase('route'));
      assert(events.filter(event=>event==='index').length===1);
    """, {"entry": entry})


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
      var data=fixture,scene,sceneData,sceneDirection,sceneCompact,sceneFamily,calls=0;
      const actual=layoutGraph;layoutGraph=function(...args){calls++;return actual(...args);};
      const first=geometry();assert.strictEqual(geometry(),first);assert.equal(calls,1);
      var ui$transform={x:41,y:-73,scale:1.2},ui$selectedStatuses=new Set(['planned']);
      assert.strictEqual(geometry(),first);assert.equal(calls,1);
      ui$orientation='tb';assert.notStrictEqual(geometry(),first);assert.equal(calls,2);
      data=JSON.parse(JSON.stringify(fixture));geometry();assert.equal(calls,3);
      mapAllowed=false;assert.equal(geometry(),null);assert.equal(calls,3);
    """, _graph(4, [(0, 1, 3, 1), (1, 2, 0, 5)]))


def test_authoritative_ids_cannot_collide_with_engine_object_prototypes() -> None:
    ids = ["__proto__", "constructor", "toString", "scope:authored", "boundary:authored", "unicode/é"]
    fixture = {
        "nodes": [{"id": node_id, "width": 208, "height": 100} for node_id in ids],
        "edges": [{"source": source, "target": target, "statement": 2, "proof": 3}
                  for source, target in zip(ids, ids[1:])],
    }
    _run("check(fixture,'lr');check(fixture,'tb');", fixture)


@pytest.mark.parametrize("direction", ["lr", "tb"])
def test_the_existing_map_bound_handles_a_hundred_twenty_real_nodes(direction: str) -> None:
    _run(f"check(fixture,{json.dumps(direction)});", _graph(120, [(0, target, 1, int(target % 3 == 0)) for target in range(1, 120)]))
