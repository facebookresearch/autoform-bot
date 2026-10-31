"""Bounded production keyboard/focus callbacks; DOM peers are not browser evidence."""

from __future__ import annotations

from tests.test_qa_outline_navigation import _OUTLINE_CALLBACKS, _OUTLINE_PEERS, _run
from tests.test_wiki_workspace_controls import _CLIENT, _CONTROL_PEERS


_SOURCE = _CLIENT.read_text(encoding="utf-8")
_KINDS_START = _SOURCE.index("  var KINDS =")
_CONSTANTS = "\n".join(
    line for line in _SOURCE.splitlines()
    if line.startswith(("  var SEARCH_PAGE =", "  var clamp ="))
) + "\n" + _SOURCE[_KINDS_START:_SOURCE.index(";", _KINDS_START) + 1]
_PEERS = (
    _CONTROL_PEERS.replace("buildShell();bindEvents();syncControls();", "")
    .replace("function setFilters(){}", "")
    .replace("function revealAll(){}", "")
    .replace("function focusNodeControl(){}", "")
    .replace("function pointerDown(){}", "")
    .replace("function fit(){}", "function fit(){fitted++;}")
    .replace("function remember(){}", "function remember(){remembered++;}")
    .replace("function schedule(){}", "function schedule(){scheduled++;}")
    .replace("function resize(){}", "function resize(){resized++;}")
    + _CONSTANTS + r"""
var ui$selected=null,ui$scopeOverview=false,ui$coarseOverview=false,ui$overviewOnly=false,ui$direction='both';
var ui$active=[],ui$requestedNode=null,ui$outlineLimit=120,ui$transform={x:0,y:0,scale:1};
var ui$pointers=new Map(),ui$moved=false,ui$pinch=null;
var firstResize=false,atlas=false,alive=true,ui$phone=false,palette={planned:{label:'Planned'}},overlays=new Map();
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
function sheetLayout(){return ui$phone;}
function center(node,scale){centered.push({id:node.id,scale});}
function world(node){return node.box;}
function updateProjection(){}
navigation.checkpoint=()=>checkpoints++;
Element.prototype.select=function(){this.selects=(this.selects||0)+1;};
buildShell();bindEvents();syncControls();
dom$inventoryRows=new Map();
dom$stage.clientWidth=600;dom$stage.clientHeight=400;
dom$inspector.clientHeight=400;
dom$nodeLayer.scrollTop=0;dom$nodeLayer.scrollLeft=0;
function card(id,x,y){
  const node={id,title:id,kind:'node',status:'planned',box:{x,y,w:80,h:40}};
  const control=makeNodeControl(node);control.tabIndex=0;
  overlays.set(id,control);dom$nodeLayer.appendChild(control);return {node,control};
}
"""
)
_CALLBACKS = [
    "element", "button", "readingLabel", "bindNodeLink", "safeHref", "buildShell", "bindEvents", "syncControls", "wrapMapKey",
    "setBrowse", "setFilters", "currentOverviewGroup", "trailList", "renderBreadcrumb", "routeTrail", "semanticParent",
    "refreshReadingLabels", "sourceNodes", "inCurrentScope", "focusNodeControl", "clearSelection",
    "revealAll", "makeNodeControl", "finished", "boundaryMeta", "nearest", "zoomAt", "statusLabel",
    "pointerDown", "grab", "pinchState", "positionMapFeedback", "freeArea",
]


def _controls(body: str, *, real_sheet: bool = False) -> None:
    callbacks, peers = _CALLBACKS, _PEERS
    if real_sheet:
        callbacks = [*_CALLBACKS, "setSheet", "syncSheetAccessibility"]
        peers = peers.replace("function setSheet(open,selected){sheets.push({open,selected});}", "")
        peers = peers.replace("function syncSheetAccessibility(){}", "")
        peers += r"""
Element.prototype.prepend=function(...children){
  for(let index=children.length-1;index>=0;index--){
    if(this.firstChild)this.insertBefore(children[index],this.firstChild);else this.appendChild(children[index]);
  }
};
dom$body.offsetTop=100;
"""
    _run(body, callbacks, peers)


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


def test_only_the_latest_map_focus_request_can_focus_or_checkpoint() -> None:
    _controls(r"""
      const first=card('A',0,0),latest=card('B',150,0),outside=new Element('button');outside.focus();
      const before={remembered,scheduled,bundle:currentBundle,projection:data,index:JSON.stringify(sharedIndex)};
      focusNodeControl(first.node);
      // Queue the old request's second rAF before a newer request arrives.
      frames.splice(0).forEach(callback=>callback());focusNodeControl(latest.node);
      dom$nodeLayer.scrollLeft=35;dom$nodeLayer.scrollTop=70;flushFrames();
      assert.equal(document.activeElement,latest.control);assert.equal(first.control.focuses.length,0);
      assert.deepEqual(latest.control.focuses,[{preventScroll:true}]);
      assert.equal(remembered,before.remembered+1);assert.equal(scheduled,before.scheduled+1);
      assert.equal(dom$nodeLayer.scrollLeft,0);assert.equal(dom$nodeLayer.scrollTop,0);
      assert.strictEqual(currentBundle,before.bundle);assert.strictEqual(data,before.projection);
      assert.equal(JSON.stringify(sharedIndex),before.index);assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
    """)


def test_pending_back_to_map_focus_cannot_take_a_new_search_focus() -> None:
    _controls(r"""
      const entry=card('A',0,0);ui$active=[entry.node];ui$mode='list';syncControls();dom$graphMode.focus();
      emit(dom$graphMode,'click');assert.equal(dom$graphMode.hidden,true);assert.equal(ui$mode,'graph');
      frames.splice(0).forEach(callback=>callback());
      dom$search.focus();dom$search.value='Geometry topic 0';
      dom$nodeLayer.scrollLeft=35;dom$nodeLayer.scrollTop=70;
      const before={remembered,scheduled,bundle:currentBundle,projection:data,index:JSON.stringify(sharedIndex),
        sheetFocuses:dom$sheetHandle.focuses.length,filterFocuses:dom$filterSummary.focuses.length};
      flushFrames();
      assert.equal(document.activeElement,dom$search);assert.equal(dom$search.value,'Geometry topic 0');
      assert.equal(entry.control.focuses.length,0);assert.equal(dom$sheetHandle.focuses.length,before.sheetFocuses);
      assert.equal(dom$filterSummary.focuses.length,before.filterFocuses);
      assert.equal(dom$nodeLayer.scrollLeft,35);assert.equal(dom$nodeLayer.scrollTop,70);
      assert.equal(remembered,before.remembered);assert.equal(scheduled,before.scheduled);
      assert.strictEqual(currentBundle,before.bundle);assert.strictEqual(data,before.projection);
      assert.equal(JSON.stringify(sharedIndex),before.index);assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
    """)


def test_pending_map_focus_cannot_take_a_new_outside_focus() -> None:
    _controls(r"""
      const entry=card('A',0,0),outside=new Element('a');document.body.appendChild(outside);dom$fit.focus();
      focusNodeControl(entry.node);frames.splice(0).forEach(callback=>callback());outside.focus();
      dom$nodeLayer.scrollLeft=35;dom$nodeLayer.scrollTop=70;
      const before={remembered,scheduled,bundle:currentBundle,projection:data,index:JSON.stringify(sharedIndex),
        sheetFocuses:dom$sheetHandle.focuses.length,filterFocuses:dom$filterSummary.focuses.length};
      flushFrames();
      assert.equal(document.activeElement,outside);assert.equal(entry.control.focuses.length,0);
      assert.equal(dom$sheetHandle.focuses.length,before.sheetFocuses);assert.equal(dom$filterSummary.focuses.length,before.filterFocuses);
      assert.equal(dom$nodeLayer.scrollLeft,35);assert.equal(dom$nodeLayer.scrollTop,70);
      assert.equal(remembered,before.remembered);assert.equal(scheduled,before.scheduled);
      assert.strictEqual(currentBundle,before.bundle);assert.strictEqual(data,before.projection);
      assert.equal(JSON.stringify(sharedIndex),before.index);assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
    """)


def test_self_hiding_back_button_body_or_html_blur_still_focuses_the_actual_map() -> None:
    _controls(r"""
      const entry=card('A',0,0);ui$active=[entry.node];document.documentElement=new Element('html');
      for(const fallback of [document.body,document.documentElement]){
        ui$mode='list';syncControls();dom$graphMode.focus();emit(dom$graphMode,'click');
        assert.equal(dom$graphMode.hidden,true);assert.equal(ui$mode,'graph');
        // Native hiding of the owner may leave body/html focused before rAF.
        fallback.focus();dom$nodeLayer.scrollLeft=35;dom$nodeLayer.scrollTop=70;
        const before={remembered,scheduled,focuses:entry.control.focuses.length};flushFrames();
        assert.equal(document.activeElement,entry.control);assert.equal(entry.control.focuses.length,before.focuses+1);
        assert.deepEqual(entry.control.focuses.at(-1),{preventScroll:true});
        assert.equal(dom$nodeLayer.scrollLeft,0);assert.equal(dom$nodeLayer.scrollTop,0);
        assert.equal(remembered,before.remembered+1);assert.equal(scheduled,before.scheduled+1);
      }
      assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
    """)


def test_visible_owner_or_unowned_hidden_owner_blur_to_body_cancels_pending_map_focus() -> None:
    _controls(r"""
      const entry=card('A',0,0),outside=new Element('button');document.body.appendChild(outside);
      Element.prototype.blur=function(){if(document.activeElement===this)document.activeElement=document.body;};
      outside.isConnected=true;dom$fit.isConnected=true;
      for(const owner of [dom$fit,outside]){
        owner.focus();focusNodeControl(entry.node);frames.splice(0).forEach(callback=>callback());
        if(owner===outside)outside.hidden=true;
        owner.blur();
        assert.equal(document.activeElement,document.body);
        dom$nodeLayer.scrollLeft=35;dom$nodeLayer.scrollTop=70;
        const before={remembered,scheduled,focuses:entry.control.focuses.length};flushFrames();
        assert.equal(document.activeElement,document.body);assert.equal(entry.control.focuses.length,before.focuses);
        assert.equal(dom$nodeLayer.scrollLeft,35);assert.equal(dom$nodeLayer.scrollTop,70);
        assert.equal(remembered,before.remembered);assert.equal(scheduled,before.scheduled);
      }
      assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
    """)


def test_removed_owned_overlay_body_blur_keeps_the_latest_target_focus_eligible() -> None:
    _controls(r"""
      const owner=card('A',0,0),target=card('B',150,0),bundle=currentBundle,projection=data;
      owner.control.isConnected=true;target.control.isConnected=true;owner.control.focus();
      assert(host.contains(owner.control));focusNodeControl(target.node);frames.splice(0).forEach(callback=>callback());
      owner.control.remove();owner.control.isConnected=false;overlays.delete(owner.node.id);document.body.focus();
      dom$nodeLayer.scrollLeft=35;dom$nodeLayer.scrollTop=70;
      const before={remembered,scheduled};flushFrames();
      assert.equal(document.activeElement,target.control);assert.equal(target.control.focuses.length,1);
      assert.deepEqual(target.control.focuses.at(-1),{preventScroll:true});
      assert.equal(dom$nodeLayer.scrollLeft,0);assert.equal(dom$nodeLayer.scrollTop,0);
      assert.equal(remembered,before.remembered+1);assert.equal(scheduled,before.scheduled+1);
      assert.strictEqual(currentBundle,bundle);assert.strictEqual(data,projection);
      assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
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


def test_full_phone_reader_suppresses_pending_node_fallback_and_missing_node_focus() -> None:
    _controls(r"""
      const entry=card('A',0,0),reading=new Element('a'),bundle=currentBundle,projection=data;
      dom$readingBody.appendChild(reading);ui$phone=true;ui$mode='graph';
      host.classList.toggle('bp-dag-sheet-open',true);
      for(const target of [entry.node,undefined,{id:'not-drawn'}]){
        host.classList.toggle('bp-dag-sheet-full',false);focusNodeControl(target);
        host.classList.toggle('bp-dag-sheet-full',true);reading.focus();
        dom$nodeLayer.scrollTop=25;dom$nodeLayer.scrollLeft=40;
        const before={remembered,scheduled,nodeFocuses:entry.control.focuses.length,
          sheetFocuses:dom$sheetHandle.focuses.length,filterFocuses:dom$filterSummary.focuses.length};
        flushFrames();
        assert.strictEqual(currentBundle,bundle);assert.strictEqual(data,projection);
        assert.equal(ui$mode,'graph');assert.equal(document.activeElement,reading);
        assert.equal(entry.control.focuses.length,before.nodeFocuses);
        assert.equal(dom$sheetHandle.focuses.length,before.sheetFocuses);
        assert.equal(dom$filterSummary.focuses.length,before.filterFocuses);
        assert.equal(dom$nodeLayer.scrollTop,25);assert.equal(dom$nodeLayer.scrollLeft,40);
        assert.equal(remembered,before.remembered);assert.equal(scheduled,before.scheduled);
      }
    """)


def test_full_sheet_class_does_not_suppress_an_uncovered_desktop_map() -> None:
    _controls(r"""
      const entry=card('A',0,0),reading=new Element('a');dom$readingBody.appendChild(reading);reading.focus();
      host.classList.toggle('bp-dag-sheet-full',true);ui$phone=false;focusNodeControl(entry.node);
      flushFrames();assert.equal(document.activeElement,entry.control);
      assert.deepEqual(entry.control.focuses.at(-1),{preventScroll:true});
    """)


def test_same_bundle_projection_replacement_cannot_steal_focus_scroll_or_checkpoint() -> None:
    _controls(r"""
      const entry=card('A',0,0),outside=new Element('a'),bundle=currentBundle;
      host.appendChild(outside);outside.focus();ui$mode='graph';
      focusNodeControl(entry.node);
      data={...data,nodes:[]};overlays.delete(entry.node.id);entry.control.remove();outside.focus();
      dom$nodeLayer.scrollTop=25;dom$nodeLayer.scrollLeft=40;
      const before={remembered,scheduled,focuses:entry.control.focuses.length,sheetFocuses:dom$sheetHandle.focuses.length};
      flushFrames();
      assert.strictEqual(currentBundle,bundle);assert.equal(ui$mode,'graph');
      assert.equal(document.activeElement,outside);assert.equal(entry.control.focuses.length,before.focuses);
      assert.equal(dom$sheetHandle.focuses.length,before.sheetFocuses);
      assert.equal(dom$nodeLayer.scrollTop,25);assert.equal(dom$nodeLayer.scrollLeft,40);
      assert.equal(remembered,before.remembered);assert.equal(scheduled,before.scheduled);
    """)


def test_same_projection_wiki_geometry_and_overlay_refresh_keep_pending_focus_eligible() -> None:
    _controls(r"""
      const entry=card('A',0,0),projection=data,bundle=currentBundle,outside=new Element('a');
      host.appendChild(outside);outside.focus();focusNodeControl(entry.node);
      data.$wikiEdges=[{source:'A',target:'B',count:1}];
      const current=card('A',150,100);entry.control.remove();
      dom$nodeLayer.scrollTop=25;dom$nodeLayer.scrollLeft=40;
      const before={remembered,scheduled};flushFrames();
      assert.strictEqual(data,projection);assert.strictEqual(currentBundle,bundle);
      assert.equal(document.activeElement,current.control);assert.equal(entry.control.focuses.length,0);
      assert.deepEqual(current.control.focuses.at(-1),{preventScroll:true});
      assert.equal(dom$nodeLayer.scrollTop,0);assert.equal(dom$nodeLayer.scrollLeft,0);
      assert.equal(remembered,before.remembered+1);assert.equal(scheduled,before.scheduled+1);
      assert.deepEqual(data.$wikiEdges,[{source:'A',target:'B',count:1}]);
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


def test_full_phone_shortcuts_set_actual_half_and_clear_inert_before_search_focus() -> None:
    _controls(r"""
      const entry=card('A',0,0),article=new Element('div'),reading=new Element('a');
      article.className='bp-reading-source';article.textContent='Complete authored reading.';
      article.appendChild(reading);dom$readingBody.appendChild(article);ui$selected=entry.node;ui$phone=true;
      const main=host.querySelector('.bp-dag-main'),bundle=currentBundle,projection=data;
      const source=article.textContent,beforeIndex=JSON.stringify(sharedIndex),beforeGraph=JSON.stringify(data);
      const href=window.location.href,focus=dom$search.focus,select=dom$search.select;
      let order=[];
      dom$search.focus=function(options){
        assert(host.classList.contains('bp-dag-sheet-open'),'Search keeps the reader open at half');
        assert(!host.classList.contains('bp-dag-sheet-full'),'Actual setSheet must reveal search before focus');
        assert.equal(main.getAttribute('inert'),null);
        assert.equal(dom$inspectorInner.getAttribute('inert'),null);
        order.push('focus');return focus.call(this,options);
      };
      dom$search.select=function(){order.push('select');return select.call(this);};
      for(const setting of [{key:'/'},{key:'k',ctrlKey:true},{key:'k',metaKey:true}]){
        host.classList.toggle('bp-dag-sheet-open',true);host.classList.toggle('bp-dag-sheet-full',true);
        syncSheetAccessibility();assert.equal(main.getAttribute('inert'),'');reading.focus();order=[];
        const before={camera:JSON.stringify(ui$transform),history:history.slice(),checkpoints,remembered,
          scheduled,resized,selected:ui$selected,selects:dom$search.selects||0};
        const event=emit(document,'keydown',Object.assign({target:reading},setting));
        assert.equal(event.defaultPrevented,true);assert.deepEqual(order,['focus','select']);
        assert.equal(document.activeElement,dom$search);assert.equal(dom$search.selects,before.selects+1);
        assert.strictEqual(currentBundle,bundle);assert.strictEqual(data,projection);assert.strictEqual(ui$selected,before.selected);
        assert.equal(window.location.href,href);assert.deepEqual(history,before.history);assert.equal(checkpoints,before.checkpoints);
        assert.equal(JSON.stringify(ui$transform),before.camera);assert.equal(resized,before.resized);
        assert.equal(remembered,before.remembered);assert.equal(scheduled,before.scheduled+1);
        assert.strictEqual(dom$readingBody.firstChild,article);assert.equal(article.textContent,source);
        assert.equal(JSON.stringify(sharedIndex),beforeIndex);assert.equal(JSON.stringify(data),beforeGraph);
        assert.deepEqual(navigationCalls,[]);
      }
    """, real_sheet=True)


def test_ignored_full_phone_shortcuts_keep_full_reader_focus_and_view_state() -> None:
    _controls(r"""
      const reading=new Element('a');dom$readingBody.appendChild(reading);ui$phone=true;
      host.classList.toggle('bp-dag-sheet-open',true);host.classList.toggle('bp-dag-sheet-full',true);
      syncSheetAccessibility();const main=host.querySelector('.bp-dag-main');
      for(const setting of [{key:'/',ctrlKey:true},{key:'/',metaKey:true},{key:'/',altKey:true},
                            {key:'/',defaultPrevented:true},{key:'k'}]){
        reading.focus();const before={camera:JSON.stringify(ui$transform),scheduled,resized,checkpoints,history:history.slice()};
        const event=emit(document,'keydown',Object.assign({target:reading},setting));
        assert.equal(event.defaultPrevented,Boolean(setting.defaultPrevented));assert.equal(document.activeElement,reading);
        assert(host.classList.contains('bp-dag-sheet-full'));assert.equal(main.getAttribute('inert'),'');
        assert.equal(JSON.stringify(ui$transform),before.camera);assert.equal(scheduled,before.scheduled);
        assert.equal(resized,before.resized);assert.equal(checkpoints,before.checkpoints);assert.deepEqual(history,before.history);
      }
      for(const tag of ['input','textarea','select','div']){
        const editor=new Element(tag);editor.isContentEditable=tag==='div';dom$readingBody.appendChild(editor);editor.focus();
        for(const setting of [{key:'/'},{key:'k',ctrlKey:true},{key:'k',metaKey:true}]){
          const event=emit(document,'keydown',Object.assign({target:editor},setting));
          assert.equal(event.defaultPrevented,false);assert.equal(document.activeElement,editor);
          assert(host.classList.contains('bp-dag-sheet-full'));assert.equal(main.getAttribute('inert'),'');
        }
      }
      assert.deepEqual(navigationCalls,[]);assert.deepEqual(history,[]);assert.equal(checkpoints,0);
    """, real_sheet=True)


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


def test_phone_key_keeps_every_real_span_and_open_focus_state_without_intercepting_summary_keys() -> None:
    _controls(r"""
      ui$phone=true;data.edges=[{source:'A',target:'B',statement:2,proof:3}];
      const before=JSON.stringify(data);syncControls();
      const first=dom$mapKey.querySelector('details'),summary=first.querySelector('summary');
      assert.equal(first.className,'bp-map-key-legend');assert.equal(summary.tagName,'SUMMARY');
      assert.deepEqual(first.querySelector('div').children.map(span=>span.className),['bp-map-key-statement','bp-map-key-proof']);
      assert.equal(first.open,false);first.open=true;summary.focus();syncControls();
      const current=dom$mapKey.querySelector('details'),currentSummary=current.querySelector('summary');
      assert.notStrictEqual(current,first);assert.equal(current.open,true);assert.equal(document.activeElement,currentSummary);
      assert.deepEqual(currentSummary.focuses.at(-1),{preventScroll:true});
      assert.equal(currentSummary.listeners.has('keydown'),false,'A native summary must retain Enter/Space handling');
      const reader=new Element('a');dom$readingBody.appendChild(reader);reader.focus();
      host.classList.toggle('bp-dag-sheet-full',true);syncControls();assert.equal(document.activeElement,reader);
      assert.equal(dom$mapKey.querySelector('details').open,true);
      host.classList.toggle('bp-dag-sheet-full',false);ui$phone=false;syncControls();
      assert.equal(dom$mapKey.querySelector('details'),null);
      assert.deepEqual(dom$mapKey.children.map(span=>span.className),['bp-map-key-statement','bp-map-key-proof']);
      assert.equal(JSON.stringify(data),before);
    """)


def test_key_pointer_and_wheel_events_stay_native_while_plain_map_wheel_still_pans() -> None:
    _controls(r"""
      ui$phone=true;data.edges=[{source:'A',target:'B',statement:2,proof:3}];syncControls();
      const legend=dom$mapKey.querySelector('details'),summary=legend.querySelector('summary'),key=legend.querySelector('div').firstChild;
      const before={camera:JSON.stringify(ui$transform),data:JSON.stringify(data),remembered,scheduled};
      for(const target of [summary,key]){
        const press=emit(dom$stage,'pointerdown',{target,button:0,pointerId:9,clientX:50,clientY:60});
        assert.equal(press.defaultPrevented,false);assert.equal(ui$pointers.size,0);assert.equal(ui$moved,false);
        for(const keys of [{},{ctrlKey:true},{metaKey:true}]){
          const wheel=emit(dom$stage,'wheel',Object.assign({target,deltaX:3,deltaY:8,deltaMode:0,clientX:50,clientY:60},keys));
          assert.equal(wheel.defaultPrevented,false);
        }
      }
      assert.equal(JSON.stringify(ui$transform),before.camera);assert.equal(remembered,before.remembered);assert.equal(scheduled,before.scheduled);
      const x=ui$transform.x,y=ui$transform.y;
      const outside=emit(dom$stage,'wheel',{target:dom$stage,deltaX:3,deltaY:8,deltaMode:0});
      assert.equal(outside.defaultPrevented,true);assert.equal(ui$transform.x,x-3);assert.equal(ui$transform.y,y-8);
      assert.equal(remembered,before.remembered+1);assert.equal(scheduled,before.scheduled+1);
      emit(dom$stage,'pointerdown',{target:dom$stage,button:0,pointerId:10,clientX:50,clientY:60});
      assert.deepEqual(ui$pointers.get(10),{x:50,y:60});assert.equal(JSON.stringify(data),before.data);
    """)


def test_map_feedback_tracks_the_real_free_band_and_resets_outside_the_half_phone_reader() -> None:
    _controls(r"""
      ui$phone=true;dom$stage.clientHeight=600;dom$inspector.clientHeight=600;
      const before=JSON.stringify({data,camera:ui$transform});host.classList.toggle('bp-dag-sheet-open',true);
      positionMapFeedback();const band=freeArea().band;
      assert.equal(band,270);assert.equal(dom$stage.clientHeight-parseFloat(dom$mapKey.style.bottom),band-4);
      assert.equal(dom$stage.clientHeight-parseFloat(dom$density.style.bottom),band-34);
      host.classList.toggle('bp-dag-sheet-full',true);positionMapFeedback();
      assert.equal(dom$mapKey.style.bottom,'');assert.equal(dom$density.style.bottom,'');
      host.classList.toggle('bp-dag-sheet-full',false);host.classList.toggle('bp-dag-sheet-open',false);positionMapFeedback();
      assert.equal(dom$mapKey.style.bottom,'');assert.equal(dom$density.style.bottom,'');
      ui$phone=false;host.classList.toggle('bp-dag-sheet-open',true);positionMapFeedback();
      assert.equal(dom$mapKey.style.bottom,'');assert.equal(dom$density.style.bottom,'');
      assert.equal(JSON.stringify({data,camera:ui$transform}),before);
    """)
