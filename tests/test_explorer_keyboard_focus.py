"""Bounded production keyboard/focus callbacks; DOM peers are not browser evidence."""

from __future__ import annotations

from tests.test_qa_outline_navigation import _OUTLINE_CALLBACKS, _OUTLINE_PEERS, _run
from tests.test_wiki_workspace_controls import _CLIENT, _CONTROL_PEERS


_CONSTANTS = "\n".join(
    line for line in _CLIENT.read_text(encoding="utf-8").splitlines()
    if line.startswith(("  var SEARCH_PAGE =", "  var clamp ="))
)
_PEERS = (
    _CONTROL_PEERS.replace("buildShell();bindEvents();syncControls();", "")
    .replace("function setFilters(){}", "")
    .replace("function revealAll(){}", "")
    .replace("function focusNodeControl(){}", "")
    .replace("function fit(){}", "function fit(){fitted++;}")
    .replace("function remember(){}", "function remember(){remembered++;}")
    .replace("function schedule(){}", "function schedule(){scheduled++;}")
    .replace("function resize(){}", "function resize(){resized++;}")
    + _CONSTANTS + r"""
var ui$selected=null,ui$scopeOverview=false,ui$overviewOnly=false,ui$direction='both';
var ui$active=[],ui$requestedNode=null,ui$outlineLimit=120,ui$transform={x:0,y:0,scale:1};
var firstResize=false,atlas=false,alive=true,palette={planned:{label:'Planned'}},overlays=new Map();
var frames=[],remembered=0,scheduled=0,fitted=0,resized=0,checkpoints=0,history=[],centered=[],sheets=[];
function requestAnimationFrame(callback){frames.push(callback);}
function flushFrames(){
  let turns=0;
  while(frames.length){assert(turns++<8,'Focus callbacks must finish');frames.splice(0).forEach(callback=>callback());}
}
function writeLocation(mode){history.push(mode);}
function refresh(){dom$hiddenButton.hidden=true;}
function renderInspector(){}
function renderInventory(){}
function setSheet(open,selected){sheets.push({open,selected});}
function sheetLayout(){return false;}
function center(node,scale){centered.push({id:node.id,scale});}
function world(node){return node.box;}
function updateProjection(){}
navigation.checkpoint=()=>checkpoints++;
Element.prototype.select=function(){this.selects=(this.selects||0)+1;};
buildShell();bindEvents();syncControls();
dom$inventoryRows=new Map();
dom$stage.clientWidth=600;dom$stage.clientHeight=400;
dom$nodeLayer.scrollTop=0;dom$nodeLayer.scrollLeft=0;
function card(id,x,y){
  const node={id,title:id,kind:'node',status:'planned',box:{x,y,w:80,h:40}};
  const control=makeNodeControl(node);control.tabIndex=0;
  overlays.set(id,control);dom$nodeLayer.appendChild(control);return {node,control};
}
"""
)
_CALLBACKS = [
    "element", "button", "readingLabel", "safeHref", "buildShell", "bindEvents", "syncControls",
    "setBrowse", "setFilters", "currentOverviewGroup", "renderBreadcrumb", "routeTrail", "semanticParent",
    "refreshReadingLabels", "sourceNodes", "inCurrentScope", "focusNodeControl", "clearSelection",
    "revealAll", "makeNodeControl", "nearest", "zoomAt", "statusLabel",
]


def _controls(body: str) -> None:
    _run(body, _CALLBACKS, _PEERS)


def test_filter_focusout_closes_only_when_focus_leaves_and_never_steals_it() -> None:
    _controls(r"""
      const first=new Element('input'),second=new Element('input'),outside=new Element('button');
      dom$filterPopover.appendChild(first);dom$filterPopover.appendChild(second);
      setFilters(true);first.focus();
      emit(dom$filters,'focusout',{target:first,relatedTarget:second});
      assert.equal(dom$filterPopover.hidden,false);
      outside.focus();const before=dom$filterSummary.focuses.length;
      emit(dom$filters,'focusout',{target:second,relatedTarget:outside});
      assert.equal(dom$filterPopover.hidden,true);
      assert.equal(dom$filterSummary.getAttribute('aria-expanded'),'false');
      assert.equal(document.activeElement,outside);assert.equal(dom$filterSummary.focuses.length,before);
      setFilters(true);first.focus();
      emit(dom$filters,'focusout',{target:first,relatedTarget:null});
      assert.equal(dom$filterPopover.hidden,true);assert.equal(dom$filterSummary.focuses.length,before);
      setFilters(true);first.focus();
      emit(document,'pointerdown',{target:outside});
      assert.equal(dom$filterPopover.hidden,true);
      assert.equal(document.activeElement,first,'Pointerdown must leave the native next focus untouched');
      assert.equal(dom$filterSummary.focuses.length,before);
    """)


def test_filter_escape_returns_focus_once_with_prevent_scroll() -> None:
    _controls(r"""
      const checkbox=new Element('input');dom$filterPopover.appendChild(checkbox);
      setFilters(true);checkbox.focus();
      const event=emit(dom$filters,'keydown',{target:checkbox,key:'Escape'});
      assert.equal(event.defaultPrevented,true);assert.equal(dom$filterPopover.hidden,true);
      assert.equal(document.activeElement,dom$filterSummary);
      assert.deepEqual(dom$filterSummary.focuses,[{preventScroll:true}]);
      assert.equal(emit(dom$filters,'keydown',{key:'Escape'}).defaultPrevented,false);
      assert.equal(dom$filterSummary.focuses.length,1);
    """)


def test_back_to_map_hiding_button_focuses_the_real_map_tabstop_after_paint() -> None:
    _controls(r"""
      const entry=card('A',0,0);ui$active=[entry.node];
      ui$mode='list';syncControls();dom$graphMode.focus();
      emit(dom$graphMode,'click');
      assert.equal(ui$mode,'graph');assert.equal(dom$graphMode.hidden,true);
      assert.equal(dom$stage.hidden,false);
      flushFrames();
      assert.equal(document.activeElement,entry.control);
      assert.deepEqual(entry.control.focuses.at(-1),{preventScroll:true});
      assert.equal(entry.control.tabIndex,0);
      assert.deepEqual(modes.at(-1),{mode:'graph',push:true});
    """)


def test_map_focus_and_native_layer_scroll_never_diverge_from_the_canvas() -> None:
    _controls(r"""
      const entry=card('A',0,0);
      dom$nodeLayer.scrollTop=280;dom$nodeLayer.scrollLeft=75;
      focusNodeControl(entry.node);flushFrames();
      assert.equal(document.activeElement,entry.control);
      assert.deepEqual(entry.control.focuses.at(-1),{preventScroll:true});
      assert.equal(dom$nodeLayer.scrollTop,0);assert.equal(dom$nodeLayer.scrollLeft,0);
      dom$nodeLayer.scrollTop=120;dom$nodeLayer.scrollLeft=45;
      emit(dom$nodeLayer,'scroll');
      assert.equal(dom$nodeLayer.scrollTop,0);assert.equal(dom$nodeLayer.scrollLeft,0);
      assert.equal(document.activeElement,entry.control);
    """)


def test_disposed_map_focus_callback_does_not_focus_scroll_or_checkpoint() -> None:
    _controls(r"""
      const entry=card('A',0,0),outside=new Element('button');outside.focus();
      dom$nodeLayer.scrollTop=25;dom$nodeLayer.scrollLeft=40;
      const before={remembered,scheduled,focuses:entry.control.focuses.length};
      focusNodeControl(entry.node);alive=false;flushFrames();
      assert.equal(document.activeElement,outside);
      assert.equal(entry.control.focuses.length,before.focuses);
      assert.equal(dom$nodeLayer.scrollTop,25);assert.equal(dom$nodeLayer.scrollLeft,40);
      assert.equal(remembered,before.remembered);assert.equal(scheduled,before.scheduled);
    """)


def test_stale_or_hidden_map_focus_request_does_not_steal_later_focus() -> None:
    _controls(r"""
      const entry=card('A',0,0),outside=new Element('button'),bundle=currentBundle;
      for(const changed of ['bundle','list','generation']){
        currentBundle=bundle;delete bundle.isCurrent;ui$mode='graph';outside.focus();
        dom$nodeLayer.scrollTop=25;dom$nodeLayer.scrollLeft=40;
        const before={remembered,scheduled,focuses:entry.control.focuses.length};
        focusNodeControl();
        if(changed==='bundle')currentBundle={scope:'other'};
        else if(changed==='list')ui$mode='list';
        else bundle.isCurrent=()=>false;
        flushFrames();
        assert.equal(document.activeElement,outside);
        assert.equal(entry.control.focuses.length,before.focuses);
        assert.equal(dom$nodeLayer.scrollTop,25);assert.equal(dom$nodeLayer.scrollLeft,40);
        assert.equal(remembered,before.remembered);assert.equal(scheduled,before.scheduled);
      }
    """)


def test_reveal_all_hiding_control_focuses_filter_and_keeps_all_real_statuses() -> None:
    _controls(r"""
      presentStatuses=['planned','fully_proved'];ui$selectedStatuses=new Set(['planned']);
      ui$showIsolated=false;ui$direction='before';dom$hiddenButton.hidden=false;dom$hiddenButton.focus();
      const before=JSON.stringify(sharedIndex);
      revealAll();
      assert.equal(dom$hiddenButton.hidden,true);assert.equal(document.activeElement,dom$filterSummary);
      assert.deepEqual(dom$filterSummary.focuses.at(-1),{preventScroll:true});
      assert.deepEqual([...ui$selectedStatuses],presentStatuses);assert.equal(ui$showIsolated,true);
      assert.equal(ui$direction,'both');assert.equal(fitted,1);assert.deepEqual(history,['replace']);
      assert.equal(JSON.stringify(sharedIndex),before);
    """)


def test_topics_drawer_focuses_current_native_link_with_first_link_fallback() -> None:
    _controls(r"""
      const first=new Element('a'),current=new Element('a');
      first.dataset.navigationNodeId='first';current.dataset.navigationNodeId='current';
      current.setAttribute('aria-current','page');dom$navigationInner.appendChild(first);dom$navigationInner.appendChild(current);
      dom$outlineToggle.focus();emit(dom$outlineToggle,'click');
      assert(host.classList.contains('bp-dag-outline-open'));
      assert.equal(dom$outlineToggle.getAttribute('aria-expanded'),'true');
      assert.equal(document.activeElement,current);
      dom$outlineToggle.focus();emit(dom$outlineToggle,'click');
      assert.equal(dom$outlineToggle.getAttribute('aria-expanded'),'false');assert.equal(document.activeElement,dom$outlineToggle);
      current.removeAttribute('aria-current');emit(dom$outlineToggle,'click');
      assert.equal(document.activeElement,first);
    """)


def test_slash_is_scoped_to_explorer_focus_and_global_command_k_is_preserved() -> None:
    _controls(r"""
      const outside=new Element('button');document.body.appendChild(outside);
      outside.focus();assert.equal(emit(document,'keydown',{target:outside,key:'/'}).defaultPrevented,false);
      assert.equal(document.activeElement,outside);
      dom$fit.focus();assert.equal(emit(document,'keydown',{target:dom$fit,key:'/'}).defaultPrevented,true);
      assert.equal(document.activeElement,dom$search);assert.equal(dom$search.selects,1);
      for(const setting of [{ctrlKey:true},{metaKey:true},{altKey:true},{defaultPrevented:true}]){
        dom$fit.focus();const event=emit(document,'keydown',Object.assign({target:dom$fit,key:'/'},setting));
        assert.equal(event.defaultPrevented,Boolean(setting.defaultPrevented));assert.equal(document.activeElement,dom$fit);
      }
      for(const setting of [{ctrlKey:true},{metaKey:true}]){
        outside.focus();assert.equal(emit(document,'keydown',Object.assign({target:outside,key:'k'},setting)).defaultPrevented,true);
        assert.equal(document.activeElement,dom$search);
      }
      const editor=new Element('input');host.appendChild(editor);editor.focus();
      assert.equal(emit(document,'keydown',{target:editor,key:'/'}).defaultPrevented,false);
      assert.equal(document.activeElement,editor);
      assert.equal(emit(document,'keydown',{target:dom$search,key:'/'}).defaultPrevented,false);
    """)


def test_modified_arrows_remain_native_and_plain_arrow_focuses_actual_neighbor() -> None:
    _controls(r"""
      const a=card('A',0,0),b=card('B',150,0);ui$active=[a.node,b.node];a.control.focus();
      for(const key of ['ArrowLeft','ArrowRight','ArrowUp','ArrowDown']){
        for(const setting of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{defaultPrevented:true}]){
          const event=emit(a.control,'keydown',Object.assign({key},setting));
          assert.equal(event.defaultPrevented,Boolean(setting.defaultPrevented));
          assert.equal(frames.length,0);assert.equal(centered.length,0);assert.equal(document.activeElement,a.control);
        }
      }
      assert.equal(emit(a.control,'keydown',{key:'ArrowRight'}).defaultPrevented,true);flushFrames();
      assert.deepEqual(centered,[{id:'B',scale:.7}]);assert.equal(document.activeElement,b.control);
      assert.deepEqual(b.control.focuses.at(-1),{preventScroll:true});
    """)


def test_empty_escape_does_not_write_history_and_selected_escape_preserves_focus() -> None:
    _controls(r"""
      const a=card('A',0,0);a.control.focus();const before=JSON.stringify(sharedIndex);
      for(let index=0;index<4;index++)assert.equal(emit(a.control,'keydown',{key:'Escape'}).defaultPrevented,false);
      clearSelection(true,true);assert.equal(checkpoints,0);assert.deepEqual(history,[]);assert.equal(sheets.length,0);
      ui$selected=a.node;ui$direction='before';clearSelection(true,true);
      assert.equal(checkpoints,1);assert.deepEqual(history,['push']);assert.equal(ui$selected,null);assert.equal(ui$direction,'both');
      assert.equal(document.activeElement,a.control);assert.deepEqual(a.control.focuses.at(-1),{preventScroll:true});
      assert.equal(JSON.stringify(sharedIndex),before);
    """)


def test_zoom_collapse_focuses_the_actual_parent_group_and_keeps_provenance() -> None:
    _controls(r"""
      const parent=card('view-group:authored',0,0);parent.node._overviewGroup='authored';
      const member=card('member',0,150);member.control.focus();
      const original={members:[member.node],edges:[{source:'member',target:'external',statement:2,proof:3}]};
      const before=JSON.stringify(original);ui$scopeOverview=true;ui$overviewGroup='authored';ui$transform.scale=.85;
      updateProjection=()=>{assert.equal(ui$overviewGroup,null);data.nodes=[parent.node];data._scopeOverview={groups:[original]};};
      zoomAt(.82,150,100);
      assert.equal(checkpoints,1);assert.equal(resized,1);assert.equal(firstResize,true);assert.deepEqual(history,['push']);
      flushFrames();assert.equal(document.activeElement,parent.control);
      assert.deepEqual(parent.control.focuses.at(-1),{preventScroll:true});
      assert.equal(JSON.stringify(original),before);
    """)


def test_outline_more_focuses_first_new_id_not_the_previous_row_count() -> None:
    _run(r"""
      const nodes=[scope('roadmap',null),...Array.from({length:13},(_,number)=>scope('T'+number,'roadmap'))];
      configureOutline(nodes,'T12');ui$outlineLimit=3;const before=JSON.stringify(sharedIndex);renderNavigation();
      assert.deepEqual(outlineLinks().map(link=>link.dataset.navigationNodeId),['T0','T1','T12']);
      const more=dom$navigationInner.querySelector('button');more.focus();emit(more,'click');
      assert.equal(document.activeElement.dataset.navigationNodeId,'T2');
      assert.equal(document.activeElement.href,nodes.find(node=>node.id==='T2').route);
      assert.equal(document.activeElement.parentElement.tagName,'LI');
      assert.equal(ui$outlineLimit,53);assert.equal(remembered,1);
      assert.equal(JSON.stringify(sharedIndex),before);
    """, _OUTLINE_CALLBACKS, _OUTLINE_PEERS)
