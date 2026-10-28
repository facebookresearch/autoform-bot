"""Actual scene/framing callbacks over captured graphs and isolated DOM peers.

These source checks preserve the independent geometry oracle. They do not
simulate native hit testing, fonts, paint timing, or browser navigation.
"""

from __future__ import annotations

import pytest

from autoform_cli import dag_viewer, graph_views, status
from autoform_cli.graph import load_graph
from tests.test_integrated_dag_layout import _NODE, _ROOT, _run

pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is required for scene source checks")

_ENVIRONMENT = r"""
eval(productionSource.slice(productionSource.indexOf('  var NODE_W ='),productionSource.indexOf('  var clients =')));
eval(productionSource.slice(productionSource.indexOf('  var clamp ='),productionSource.indexOf('  function element(')));
eval(['sceneFrame','sceneDetours','framingNodes','framingViewport','framingPadding','world','screen','fit','freeArea','center','bandInsets','centerIfHidden']
  .map(original).join('\n'));
var data=fixture.data,ui$active=data.nodes,ui$selected=null,mapAllowed=true,atlas=fixture.atlas,
  navigation={},ui$area=null,ui$orientation=fixture.direction,family='sans-serif',restoring=false;
var scene,sceneData,sceneDirection,sceneCompact,sceneFamily,sceneAspect,sceneWiki,sceneReduced;
var dom$stage={clientWidth:fixture.width,clientHeight:fixture.height},dom$inspector={offsetTop:260};
var dom$fit={setAttribute(name,value){this[name]=String(value);}};
var ui$activeIds=new Set(data.nodes.map(node=>node.id)),dom$density={hidden:true};
var host={classList:{contains(){return false;}}},document={activeElement:{dataset:{}}};
var ui$transform={x:0,y:0,scale:1},ui$readableCrop=false;
function sheetLayout(){return fixture.width<=620;}
function currentOverviewGroup(){return fixture.group||null;}
function calculateActive(){} function schedule(){} function remember(){}
function insideFrame(node) {
  const box=screen(node),free=freeArea(),pad=framingPadding();
  return box.x>=free.left+pad.side-1e-6&&box.x+box.w<=free.right-pad.side+1e-6&&
    box.y>=(free.top||0)+pad.top-1e-6&&box.y+box.h<=(free.top||0)+free.band-pad.bottom+1e-6;
}
"""

_AREAS = ["Algebra & Number Theory", "Analysis & Probability", "Discrete Mathematics & Computer Science",
          "Geometry & Topology", "Foundations & Logic"]
_ROOT_WEIGHTS = [(0, 1, 22, 74), (0, 2, 132, 26), (1, 2, 50, 79), (1, 3, 0, 9), (1, 0, 66, 49),
                 (2, 0, 118, 68), (2, 1, 0, 9), (2, 3, 0, 2), (3, 1, 3, 4), (3, 2, 1, 5),
                 (4, 2, 362, 342), (0, 3, 1, 4), (3, 0, 1, 3)]


@pytest.mark.parametrize(("width", "height", "direction"), [(600, 260, "lr"), (390, 520, "tb")])
def test_fit_keeps_the_selected_isolate_whole_when_the_linked_only_frame_fits(
    width: int, height: int, direction: str,
) -> None:
    data = {
        "nodes": [{"id": name, "kind": "node", "width": 196, "height": 100, "isolated": name == "C"}
                  for name in ("A", "B", "C")],
        "edges": [{"source": "A", "target": "B", "statement": 2, "proof": 3}],
    }
    _run(_ENVIRONMENT + r"""
      ui$selected=data.nodes[2];
      const before=JSON.stringify(data),current=geometry(),viewport=framingViewport(),padding=framingPadding();
      assert(sceneFrame(current,['A','B','C'],viewport,padding).cropped,'Fixture must exercise an overfull whole map');
      assert.equal(sceneFrame(current,['A','B'],viewport,padding).cropped,false,'Linked-only frame must fit');
      fit();
      assert(insideFrame(ui$selected),'Fit dropped its selected unlinked result outside the usable viewport');
      assert(ui$transform.scale>=.9);assert.strictEqual(geometry(),current);
      assert.equal(current.nodes.size,3);assert.equal(current.$display.totalPairs,1);
      assert.equal(data.edges[0].statement,2);assert.equal(data.edges[0].proof,3);
      assert.equal(JSON.stringify(data),before,'Selection must not invent links or modify the captured graph');
    """, {"data": data, "width": width, "height": height, "direction": direction, "atlas": False})


@pytest.fixture(scope="module")
def infimum_loss_scene() -> dict:
    """Read the real published chapter metadata, without rendering or running Lean."""
    graph = load_graph(_ROOT / "skills/setup/assets/cabannes-thesis-project/blueprint")
    states = status.derive(graph)
    view = graph_views.scope_views(graph, states).get("infimum-loss") or graph_views.chapter_view(graph, states, "infimum-loss")
    drawn, dimensions = dag_viewer.drawn_view(view), dag_viewer._dimensions(view)
    nodes = [{"id": node.id, "title": node.title, "kind": node.kind, "area": node.area,
              "width": 168 if node.kind == "boundary" else dimensions[node.id]["width"],
              "height": 88 if node.kind == "boundary" else max(100, dimensions[node.id]["height"])} for node in drawn.nodes]
    known = {node["id"] for node in nodes}
    edges = [{"source": edge.source, "target": edge.target, "statement": edge.statement_count, "proof": edge.proof_count}
             for edge in drawn.edges if edge.source in known and edge.target in known]
    assert len(nodes) == 7 and len(edges) == 9
    assert sum(edge["statement"] for edge in edges) == 7 and sum(edge["proof"] for edge in edges) == 2
    return {"nodes": nodes, "edges": edges}


@pytest.mark.parametrize("direction", ["lr", "tb"])
@pytest.mark.parametrize("width", [1336, 662])
def test_real_infimum_loss_chapter_keeps_all_seven_cards_whole_at_readable_desktop_scale(
    infimum_loss_scene: dict, direction: str, width: int,
) -> None:
    _run(_ENVIRONMENT + r"""
      const before=JSON.stringify(data);fit();const current=geometry();
      assert.equal(ui$readableCrop,false);assert(ui$transform.scale>=.9);
      assert.equal(data.nodes.filter(insideFrame).length,7,'The real bundled chapter still crops a card at the reader edge');
      assert.equal(current.$display.totalPairs,9);assert.equal(current.$display.totalRoutes,9);
      assert.equal(current.clusters.length,7,'Compact spacing must not invent a mathematical cluster');
      check(data,'lr');check(data,'tb');assert.equal(JSON.stringify(data),before);
    """, {"data": infimum_loss_scene, "width": width, "height": 810.515625, "direction": direction, "atlas": False})


def test_real_infimum_loss_phone_keeps_a_readable_local_card_without_claiming_every_card_fits(infimum_loss_scene: dict) -> None:
    _run(_ENVIRONMENT + r"""
      const before=JSON.stringify(data);fit();
      assert(ui$transform.scale>=.9);assert(ui$readableCrop,'The phone must honestly retain readable cropping');
      assert(data.nodes.some(node=>node.kind!=='boundary'&&insideFrame(node)),'No real mathematical card remains usable');
      check(data,'lr');check(data,'tb');assert.equal(JSON.stringify(data),before);
    """, {"data": infimum_loss_scene, "width": 390, "height": 650.59375, "direction": "lr", "atlas": False})


def test_pinned_qa_twenty_two_card_shape_uses_the_more_readable_narrow_orientation_without_dropping_edges() -> None:
    # Metadata-only replay of da98220c's tests/test_zz_ui_screenshots.py:
    # Topology's 20 generated results and its two real cross-chapter boundaries.
    # This checks a raw narrow Reading corridor, not the initial native UI's
    # separate isolate shelves, and does not claim that all 22 cards fit.
    names = [f"topology/r{number:02d}" for number in range(20)] + ["boundary:analysis", "boundary:number-theory"]
    pairs = [(20, 3, 1), (20, 11, 1), (0, 2, 1), (0, 3, 1), (1, 2, 1), (1, 3, 1), (1, 4, 1),
             (1, 7, 1), (1, 8, 1), (1, 14, 1), (2, 4, 1), (2, 5, 1), (2, 9, 1), (2, 17, 1), (2, 19, 1),
             (3, 4, 1), (3, 5, 1), (3, 10, 1), (3, 11, 1), (3, 13, 1), (3, 15, 1), (3, 17, 1),
             (4, 13, 1), (5, 7, 1), (5, 8, 1), (5, 9, 1), (5, 14, 1), (5, 15, 1), (9, 10, 1), (9, 13, 1),
             (12, 19, 1), (14, 15, 1), (15, 18, 1), (19, 21, 2)]
    data = {"nodes": [{"id": name, "kind": "node" if index < 20 else "boundary",
                       "width": 196 if index < 20 else 168, "height": 100 if index < 20 else 88}
                      for index, name in enumerate(names)],
            "edges": [{"source": names[a], "target": names[b], "statement": weight, "proof": 0} for a, b, weight in pairs]}
    _run(_ENVIRONMENT + r"""
      const before=JSON.stringify(data),aspect=Math.round(dom$stage.clientWidth/dom$stage.clientHeight*10)/10;
      const ids=framingNodes().map(node=>node.id),viewport=framingViewport(),padding=framingPadding();
      const lr=layoutGraph(data.nodes,data.edges,'lr',aspect,undefined,true),tb=layoutGraph(data.nodes,data.edges,'tb',aspect,undefined,true);
      assert(sceneFrame(tb,ids,viewport,padding).contained>sceneFrame(lr,ids,viewport,padding).contained,
        'The captured 22-card narrow corridor no longer exercises a better readable orientation');
      const current=geometry();assert.equal(normal(current),normal(tb),'The bounded orientation gate still excludes the reviewed 22-card view');
      fit();assert(ui$readableCrop&&ui$transform.scale>=.9);
      assert(data.nodes.some(node=>node.kind!=='boundary'&&insideFrame(node)),'The chosen cropped pose has no usable mathematical card');
      assert.equal(current.$display.totalPairs,34);assert.equal(current.$display.totalRoutes,34);
      assert.equal(data.edges.reduce((sum,edge)=>sum+edge.statement,0),35);
      check(data,'lr');check(data,'tb');assert.equal(JSON.stringify(data),before);
    """, {"data": data, "width": 662, "height": 810.515625, "direction": "lr", "atlas": False})


@pytest.mark.parametrize("direction", ["lr", "tb"])
@pytest.mark.parametrize(("width", "height"), [(1336, 810.515625), (952, 722.328125),
                                             (570, 722.328125), (390, 650.59375)])
def test_actual_atlas_root_uses_real_free_area_and_fits_all_five_cards(width: int, height: float, direction: str) -> None:
    # Exact e2a qualified Atlas authored-area edges. No source taxonomy is inferred.
    data = {
        "nodes": [{"id": "region:" + area, "area": area, "_region": area, "kind": "scope",
                   "width": 264, "height": 148} for area in _AREAS],
        "edges": [{"source": "region:" + _AREAS[a], "target": "region:" + _AREAS[b],
                   "statement": statement, "proof": proof} for a, b, statement, proof in _ROOT_WEIGHTS],
    }
    _run(_ENVIRONMENT + r"""
      const source=JSON.stringify(data),actual=layoutGraph;let calls=0;
      layoutGraph=(...args)=>{calls++;return actual(...args);};
      fit();const current=geometry();assert.equal(calls,2,'Both bounded orientations must be evaluated once');
      assert.equal(ui$readableCrop,false);assert(ui$transform.scale>=.9);
      assert(data.nodes.every(insideFrame),'An authored area is outside the actual free map viewport');
      const frame=sceneFrame(current,data.nodes.map(node=>node.id),framingViewport(),framingPadding());
      assert.equal(frame.contained,5);assert.deepEqual(ui$transform,{x:frame.x,y:frame.y,scale:frame.scale});
      assert.deepEqual(current.clusters.map(cluster=>cluster.members.length).sort(),[1,4]);
      assert.equal(current.$display.totalPairs,13);assert.equal(current.$display.totalRoutes,23);
      assert.equal(current.routes.reduce((sum,route)=>sum+route.$statement,0),756);
      assert.equal(current.routes.reduce((sum,route)=>sum+route.$proof,0),674);
      assert.strictEqual(geometry(),current);assert.equal(calls,2,'Unchanged framing recomputed scene geometry');
      assert.equal(JSON.stringify(data),source,'Fit or orientation selection changed captured graph fields');
      const dimensions=data.nodes.map(node=>({...node,width:compactRegions()?312:node.width,height:compactRegions()?96:node.height}));
      check({nodes:dimensions,edges:data.edges},'lr');check({nodes:dimensions,edges:data.edges},'tb');
    """, {"data": data, "width": width, "height": height, "direction": direction, "atlas": True})


def _algebra_connected() -> dict:
    # Exact 13 non-isolated drawing endpoints and 12 authored pairs from the
    # retained e2a Algebra payload. Shelving/isolate projection is tested elsewhere.
    names = ["scope:Algebra/MathlibExt/big-operators", "scope:Algebra/MathlibExt/lie",
             "scope:Algebra/MathlibExt/mv-polynomial", "scope:Algebra/MathlibExt/polynomial",
             "scope:Algebra/WantedExt/ordered-partition-permutation-polynomials-wanted",
             "scope:Algebra/WantedExt/power-series", "boundary:Analysis", "boundary:Combinatorics",
             "boundary:ComputerScience", "boundary:FieldTheory", "boundary:LinearAlgebra",
             "boundary:NumberTheory", "boundary:RingTheory"]
    counts = [[("fully_proved", 1)], [("fully_proved", 19)], [("fully_proved", 26)], [("fully_proved", 51)],
              [("can_prove", 9)], [("fully_proved", 6), ("can_prove", 17)],
              [("fully_proved", 17), ("can_prove", 1)], [("fully_proved", 9), ("can_prove", 1)],
              [("fully_proved", 1)], [("fully_proved", 1)], [("fully_proved", 1)],
              [("fully_proved", 4), ("can_prove", 5)], [("fully_proved", 1)]]
    weights = [(7, 1, 0, 8), (9, 4, 3, 0), (10, 3, 1, 0), (11, 3, 1, 1), (12, 5, 7, 0),
               (0, 8, 0, 1), (1, 7, 1, 0), (2, 6, 2, 34), (3, 6, 9, 15),
               (3, 7, 1, 1), (3, 11, 7, 4), (3, 2, 1, 1)]
    return {
        "nodes": [{"id": name, "kind": "boundary" if name.startswith("boundary:") else "scope",
                   "width": 196, "height": 100, "status": "can_prove" if any(status == "can_prove" for status, _ in entries)
                   else "fully_proved", "status_counts": entries} for name, entries in zip(names, counts)],
        "edges": [{"source": names[a], "target": names[b], "statement": statement, "proof": proof}
                  for a, b, statement, proof in weights],
    }


def test_actual_algebra_connected_scene_selects_the_orientation_without_perimeter_spaghetti() -> None:
    _run(_ENVIRONMENT + r"""
      const source=JSON.stringify(data),nodes=data.nodes,edges=data.edges,ids=framingNodes().map(node=>node.id);
      assert.equal(nodes.length,13);assert.equal(ids.length,6);assert.equal(edges.length,12);
      const aspect=Math.round(dom$stage.clientWidth/dom$stage.clientHeight*10)/10;
      const lr=layoutGraph(nodes,edges,'lr',aspect,undefined,true),tb=layoutGraph(nodes,edges,'tb',aspect,undefined,true);
      assert.equal(sceneDetours(lr),0,'The reviewed Algebra LR corridor still falls back to a huge perimeter path');
      assert.equal(sceneDetours(tb),0,'The alternate orientation still has huge Algebra return trunks');
      fit();const current=geometry();assert.equal(sceneDetours(current),0);
      assert.equal(current.$display.totalPairs,12);assert.equal(current.$display.shownPairs,11);
      assert.equal(current.$display.totalRoutes,18);assert.equal(current.$display.shownRoutes,11);
      assert.equal(current.routes.reduce((sum,route)=>sum+route.$statement,0),24);
      assert.equal(current.routes.reduce((sum,route)=>sum+route.$proof,0),50);
      assert(data.nodes.some(node=>node.kind!=='boundary'&&insideFrame(node)),'No local Algebra card remains usable');
      const actual=layoutGraph;let calls=0;layoutGraph=(...args)=>{calls++;return actual(...args);};
      assert.strictEqual(geometry(),current);assert.equal(calls,0);
      ui$hovered=nodes[0];assert.strictEqual(geometry(),current);assert.equal(calls,0,'Hover must not reflow a backbone');
      ui$selected=nodes.find(node=>node.id==='scope:Algebra/MathlibExt/polynomial');
      const complete=geometry();assert.equal(calls,2);assert.equal(complete.routes.length,18);
      assert.equal(complete.$display.reduced,false);
      assert.equal(complete.routes.reduce((sum,route)=>sum+(route.kind==='statement'?route.count:0),0),33);
      assert.equal(complete.routes.reduce((sum,route)=>sum+(route.kind==='proof'?route.count:0),0),65);
      assert.equal(JSON.stringify(data),source,'Display mode changed actual IDs, status counts, or typed dependencies');
      check(data,'lr');check(data,'tb');
    """, {"data": _algebra_connected(), "width": 1336, "height": 810.515625, "direction": "lr", "atlas": False})


def test_real_shared_route_parts_have_no_phantom_join_lengths_in_detour_comparison() -> None:
    _run(_ENVIRONMENT + r"""
      const current=layoutGraph(data.nodes,data.edges,'lr',1.6),parts=current.routes.filter(route=>route.parts);
      assert(parts.length,'The actual Algebra fixture must contain perimeter-route parts');
      const lengths=current.routes.map(route=>{
        const sections=route.parts||[{points:route.points}];
        sections.slice(1).forEach((part,index)=>{
          const previous=sections[index].points.at(-1),next=part.points[0];
          assert(Math.hypot(previous.x-next.x,previous.y-next.y)<1e-7,'Shared route sections do not actually join');
        });
        const length=sections.reduce((sum,part)=>sum+part.points.slice(1).reduce((n,point,index)=>
          n+Math.hypot(point.x-part.points[index].x,point.y-part.points[index].y),0),0);
        const from=current.nodes.get(route.source),to=current.nodes.get(route.target),direct=
          Math.hypot(from.x+from.w/2-to.x-to.w/2,from.y+from.h/2-to.y-to.h/2);
        return length>Math.max(480,direct*3);
      });
      assert.equal(sceneDetours(current),lengths.filter(Boolean).length);
      const flat={nodes:current.nodes,routes:current.routes.map(route=>({...route,
        points:route.points||route.parts.flatMap(part=>part.points)}))};
      assert.equal(sceneDetours(flat),sceneDetours(current),'Representing shared sections changes detour classification');
    """, {"data": _algebra_connected(), "width": 1336, "height": 810.515625, "direction": "lr", "atlas": False})


def test_small_selected_result_uses_a_readable_vertical_corridor_and_keeps_both_dependency_types() -> None:
    data = {"nodes": [{"id": name, "kind": "node", "width": 196, "height": 100} for name in ["before", "result", "after"]],
            "edges": [{"source": "before", "target": "result", "statement": 2, "proof": 3},
                      {"source": "result", "target": "after", "statement": 5, "proof": 7}]}
    _run(_ENVIRONMENT + r"""
      const source=JSON.stringify(data);ui$selected=data.nodes[1];
      fit();const current=geometry();assert.equal(ui$readableCrop,false);
      assert(data.nodes.every(insideFrame),'The bounded result corridor still uses a cropped horizontal layout');
      assert(ui$transform.scale>=.9);assert.equal(current.routes.length,4);assert.equal(current.$display.reduced,false);
      check(data,'tb',current);assert.equal(JSON.stringify(data),source);
    """, {"data": data, "width": 240, "height": 640, "direction": "lr", "atlas": False})


def test_framing_prefers_drilled_members_but_explicit_boundary_selection_retains_context() -> None:
    data = {"nodes": [{"id": name, "kind": "boundary" if name == "outside" else "scope", "width": 196, "height": 100}
                      for name in ["member", "sibling", "outside"]], "edges": []}
    _run(_ENVIRONMENT + r"""
      assert.deepEqual(framingNodes().map(node=>node.id),['member']);
      currentOverviewGroup=()=>null;assert.deepEqual(framingNodes().map(node=>node.id),['member','sibling']);
      ui$selected=data.nodes[2];assert.deepEqual(framingNodes().map(node=>node.id),['member','sibling','outside']);
    """, {"data": data, "group": {"members": [{"id": "member"}]}, "width": 500, "height": 640,
           "direction": "lr", "atlas": False})
