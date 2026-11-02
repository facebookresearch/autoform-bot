"""Bounded production keyboard/focus callbacks; DOM peers are not browser evidence."""

from __future__ import annotations

from tests.test_qa_outline_navigation import _OUTLINE_CALLBACKS, _OUTLINE_PEERS, _run
from tests.test_wiki_workspace_controls import _CLIENT, _CONTROL_PEERS


_SOURCE = _CLIENT.read_text(encoding="utf-8")
_CONSTANTS = "\n".join(
    line for line in _SOURCE.splitlines()
    if line.startswith(("  var SEARCH_PAGE =", "  var clamp ="))
)
_PEERS = (
    _CONTROL_PEERS.replace("buildShell();bindEvents();syncControls();", "")
    .replace("function setFilters(){}", "")
    .replace("function revealAll(){}", "")
    .replace("function focusMap(){}", "")
    .replace("function remember(){}", "function remember(){remembered++;}")
    .replace("function resize(){}", "function resize(){resized++;}")
    + _CONSTANTS + r"""
var ui$selected=null,ui$overviewOnly=false,ui$requestedNode=null,ui$outlineLimit=120;
var alive=true,ui$phone=false,palette={planned:{label:'Planned'}};
var frames=[],remembered=0,resized=0,checkpoints=0,history=[],sheets=[];
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
navigation.checkpoint=()=>checkpoints++;
Element.prototype.select=function(){this.selects=(this.selects||0)+1;};
buildShell();bindEvents();syncControls();
dom$inventoryRows=new Map();
dom$stage.clientWidth=600;dom$stage.clientHeight=400;
dom$inspector.clientHeight=400;
// The whole map mounts its own host in the stage. Its tab stop is the card it has selected, else its svg.
const wholeMap=new Element('div'),mapSvg=new Element('svg'),mapCard=new Element('g');
wholeMap.className='bp-whole-map';mapSvg.setAttribute('tabindex','0');wholeMap.appendChild(mapSvg);dom$stage.appendChild(wholeMap);
mapCard.className='pm-result pm-selected';mapCard.setAttribute('tabindex','0');
const mapStops={'.bp-whole-map .pm-result.pm-selected[tabindex="0"]':mapCard,'.bp-whole-map svg[tabindex="0"]':mapSvg};
dom$stage.querySelector=selector=>{
  assert(selector in mapStops,'Unexpected map selector '+selector);const stop=mapStops[selector];
  return wholeMap.parentElement===dom$stage&&wholeMap.contains(stop)?stop:null;
};
"""
)
_CALLBACKS = [
    "element", "button", "readingLabel", "bindNodeLink", "safeHref", "buildShell", "bindEvents", "syncControls",
    "setBrowse", "setFilters", "trailList", "renderBreadcrumb", "routeTrail", "semanticParent",
    "refreshReadingLabels", "tooltip", "sourceNodes", "inCurrentScope", "focusMap", "mapControl", "clearSelection",
    "revealAll", "statusLabel",
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
      ui$mode='list';syncControls();dom$graphMode.focus();
      emit(dom$graphMode,'click');
      assert.equal(ui$mode,'graph');assert.equal(dom$graphMode.hidden,true);
      assert.equal(dom$stage.hidden,false);
      flushFrames();
      assert.equal(document.activeElement,mapSvg);
      assert.deepEqual(mapSvg.focuses.at(-1),{preventScroll:true});
      assert.deepEqual(modes.at(-1),{mode:'graph',push:true});
    """)


def test_only_the_latest_map_focus_request_can_focus_or_checkpoint() -> None:
    _controls(r"""
      const outside=new Element('button');outside.focus();const before=remembered;
      focusMap();frames.splice(0).forEach(callback=>callback());focusMap();
      // The older request's second frame comes first, and it no longer acts.
      frames.shift()();
      assert.equal(document.activeElement,outside);assert.equal(mapSvg.focuses.length,0);assert.equal(remembered,before);
      flushFrames();
      assert.equal(document.activeElement,mapSvg);assert.deepEqual(mapSvg.focuses,[{preventScroll:true}]);
      assert.equal(remembered,before+1);assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
    """)


def test_pending_back_to_map_focus_cannot_take_a_new_search_focus() -> None:
    _controls(r"""
      ui$mode='list';syncControls();dom$graphMode.focus();
      emit(dom$graphMode,'click');assert.equal(dom$graphMode.hidden,true);assert.equal(ui$mode,'graph');
      frames.splice(0).forEach(callback=>callback());
      dom$search.focus();dom$search.value='Geometry topic 0';
      const before={remembered,bundle:currentBundle,projection:data,index:JSON.stringify(sharedIndex),
        sheetFocuses:dom$sheetHandle.focuses.length,filterFocuses:dom$filterSummary.focuses.length};
      flushFrames();
      assert.equal(document.activeElement,dom$search);assert.equal(dom$search.value,'Geometry topic 0');
      assert.equal(mapSvg.focuses.length,0);assert.equal(dom$sheetHandle.focuses.length,before.sheetFocuses);
      assert.equal(dom$filterSummary.focuses.length,before.filterFocuses);
      assert.equal(remembered,before.remembered);
      assert.strictEqual(currentBundle,before.bundle);assert.strictEqual(data,before.projection);
      assert.equal(JSON.stringify(sharedIndex),before.index);assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
    """)


def test_pending_map_focus_cannot_take_a_new_outside_focus() -> None:
    _controls(r"""
      const outside=new Element('a');document.body.appendChild(outside);dom$graphMode.focus();
      focusMap();frames.splice(0).forEach(callback=>callback());outside.focus();
      const before={remembered,bundle:currentBundle,projection:data,index:JSON.stringify(sharedIndex),
        sheetFocuses:dom$sheetHandle.focuses.length,filterFocuses:dom$filterSummary.focuses.length};
      flushFrames();
      assert.equal(document.activeElement,outside);assert.equal(mapSvg.focuses.length,0);
      assert.equal(dom$sheetHandle.focuses.length,before.sheetFocuses);assert.equal(dom$filterSummary.focuses.length,before.filterFocuses);
      assert.equal(remembered,before.remembered);
      assert.strictEqual(currentBundle,before.bundle);assert.strictEqual(data,before.projection);
      assert.equal(JSON.stringify(sharedIndex),before.index);assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
    """)


def test_self_hiding_back_button_body_or_html_blur_still_focuses_the_actual_map() -> None:
    _controls(r"""
      document.documentElement=new Element('html');
      for(const fallback of [document.body,document.documentElement]){
        ui$mode='list';syncControls();dom$graphMode.focus();emit(dom$graphMode,'click');
        assert.equal(dom$graphMode.hidden,true);assert.equal(ui$mode,'graph');
        // Native hiding of the owner may leave body/html focused before rAF.
        fallback.focus();
        const before={remembered,focuses:mapSvg.focuses.length};flushFrames();
        assert.equal(document.activeElement,mapSvg);assert.equal(mapSvg.focuses.length,before.focuses+1);
        assert.deepEqual(mapSvg.focuses.at(-1),{preventScroll:true});
        assert.equal(remembered,before.remembered+1);
      }
      assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
    """)


def test_visible_owner_or_unowned_hidden_owner_blur_to_body_cancels_pending_map_focus() -> None:
    _controls(r"""
      const outside=new Element('button');document.body.appendChild(outside);
      Element.prototype.blur=function(){if(document.activeElement===this)document.activeElement=document.body;};
      for(const owner of [dom$browseToggle,outside]){
        owner.focus();focusMap();frames.splice(0).forEach(callback=>callback());
        if(owner===outside)outside.hidden=true;
        owner.blur();
        assert.equal(document.activeElement,document.body);
        const before={remembered,focuses:mapSvg.focuses.length,filterFocuses:dom$filterSummary.focuses.length};flushFrames();
        assert.equal(document.activeElement,document.body);assert.equal(mapSvg.focuses.length,before.focuses);
        assert.equal(dom$filterSummary.focuses.length,before.filterFocuses);assert.equal(remembered,before.remembered);
      }
      assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
    """)


def test_a_removed_owned_owner_blurring_to_body_keeps_the_map_focus_eligible() -> None:
    _controls(r"""
      const owner=new Element('button');host.appendChild(owner);owner.focus();
      focusMap();frames.splice(0).forEach(callback=>callback());
      owner.remove();owner.isConnected=false;document.body.focus();
      const before=remembered;flushFrames();
      assert.equal(document.activeElement,mapSvg);assert.deepEqual(mapSvg.focuses,[{preventScroll:true}]);
      assert.equal(remembered,before+1);assert.deepEqual(history,[]);assert.deepEqual(navigationCalls,[]);
    """)


def test_disposed_map_focus_callback_does_not_focus_or_checkpoint() -> None:
    _controls(r"""
      const outside=new Element('button');outside.focus();
      const before={remembered,filterFocuses:dom$filterSummary.focuses.length};
      focusMap();alive=false;flushFrames();
      assert.equal(document.activeElement,outside);assert.equal(mapSvg.focuses.length,0);
      assert.equal(dom$filterSummary.focuses.length,before.filterFocuses);assert.equal(remembered,before.remembered);
    """)


def test_stale_or_hidden_map_focus_request_does_not_steal_later_focus() -> None:
    _controls(r"""
      const outside=new Element('button'),bundle=currentBundle;
      for(const changed of ['bundle','list','generation']){
        currentBundle=bundle;delete bundle.isCurrent;ui$mode='graph';outside.focus();
        const before={remembered,focuses:mapSvg.focuses.length,filterFocuses:dom$filterSummary.focuses.length};
        focusMap();
        if(changed==='bundle')currentBundle={scope:'other'};
        else if(changed==='list')ui$mode='list';
        else bundle.isCurrent=()=>false;
        flushFrames();
        assert.equal(document.activeElement,outside,changed);assert.equal(mapSvg.focuses.length,before.focuses,changed);
        assert.equal(dom$filterSummary.focuses.length,before.filterFocuses,changed);assert.equal(remembered,before.remembered,changed);
      }
    """)


def test_full_phone_reader_suppresses_pending_map_focus_and_its_filter_fallback() -> None:
    _controls(r"""
      const reading=new Element('a'),bundle=currentBundle,projection=data;
      dom$readingBody.appendChild(reading);ui$phone=true;ui$mode='graph';
      host.classList.toggle('bp-dag-sheet-open',true);
      for(const mounted of [true,false]){
        if(mounted)dom$stage.appendChild(wholeMap);else wholeMap.remove();
        host.classList.toggle('bp-dag-sheet-full',false);reading.focus();focusMap();
        // The reader grows to cover the map before the map focus lands.
        host.classList.toggle('bp-dag-sheet-full',true);
        const before={remembered,mapFocuses:mapSvg.focuses.length,
          sheetFocuses:dom$sheetHandle.focuses.length,filterFocuses:dom$filterSummary.focuses.length};
        flushFrames();
        assert.strictEqual(currentBundle,bundle);assert.strictEqual(data,projection);
        assert.equal(ui$mode,'graph');assert.equal(document.activeElement,reading);
        assert.equal(mapSvg.focuses.length,before.mapFocuses);
        assert.equal(dom$sheetHandle.focuses.length,before.sheetFocuses);
        assert.equal(dom$filterSummary.focuses.length,before.filterFocuses);
        assert.equal(remembered,before.remembered);
      }
    """)


def test_full_sheet_class_does_not_suppress_an_uncovered_desktop_map() -> None:
    _controls(r"""
      const reading=new Element('a');dom$readingBody.appendChild(reading);reading.focus();
      host.classList.toggle('bp-dag-sheet-full',true);ui$phone=false;focusMap();
      flushFrames();assert.equal(document.activeElement,mapSvg);
      assert.deepEqual(mapSvg.focuses.at(-1),{preventScroll:true});
    """)


def test_map_focus_lands_on_the_maps_selected_card_before_its_svg() -> None:
    _controls(r"""
      const outside=new Element('button');outside.focus();mapSvg.appendChild(mapCard);
      focusMap();flushFrames();
      assert.equal(document.activeElement,mapCard);assert.deepEqual(mapCard.focuses,[{preventScroll:true}]);
      assert.equal(mapSvg.focuses.length,0);
    """)


def test_a_missing_or_still_hidden_map_leaves_focus_on_the_filter() -> None:
    _controls(r"""
      const outside=new Element('button');
      for(const map of ['missing','unstyled']){
        outside.focus();
        if(map==='missing')wholeMap.remove();
        else{
          // Until its stylesheet loads the map is visibility:hidden, and its svg refuses focus.
          dom$stage.appendChild(wholeMap);mapSvg.focus=function(options){this.focuses.push(options);};
        }
        const before={remembered,filterFocuses:dom$filterSummary.focuses.length};
        focusMap();flushFrames();
        assert.equal(document.activeElement,dom$filterSummary,map);
        assert.equal(dom$filterSummary.focuses.length,before.filterFocuses+1,map);
        assert.deepEqual(dom$filterSummary.focuses.at(-1),{preventScroll:true});assert.equal(remembered,before.remembered+1,map);
      }
      assert.equal(mapSvg.focuses.length,1,'The hidden map was offered the focus first');
    """)


def test_reveal_all_hiding_control_focuses_filter_and_keeps_all_real_statuses() -> None:
    _controls(r"""
      presentStatuses=['planned','fully_proved'];ui$selectedStatuses=new Set(['planned']);
      dom$hiddenButton.hidden=false;dom$hiddenButton.focus();
      const before=JSON.stringify(sharedIndex);
      revealAll();
      assert.equal(dom$hiddenButton.hidden,true);assert.equal(document.activeElement,dom$filterSummary);
      assert.deepEqual(dom$filterSummary.focuses.at(-1),{preventScroll:true});
      assert.deepEqual([...ui$selectedStatuses],presentStatuses);assert.deepEqual(history,['replace']);
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
      dom$browseToggle.focus();assert.equal(emit(document,'keydown',{target:dom$browseToggle,key:'/'}).defaultPrevented,true);
      assert.equal(document.activeElement,dom$search);assert.equal(dom$search.selects,1);
      for(const setting of [{ctrlKey:true},{metaKey:true},{altKey:true},{defaultPrevented:true}]){
        dom$browseToggle.focus();const event=emit(document,'keydown',Object.assign({target:dom$browseToggle,key:'/'},setting));
        assert.equal(event.defaultPrevented,Boolean(setting.defaultPrevented));assert.equal(document.activeElement,dom$browseToggle);
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
      const selected={id:'A',title:'A',kind:'node',status:'planned'},article=new Element('div'),reading=new Element('a');
      article.className='bp-reading-source';article.textContent='Complete authored reading.';
      article.appendChild(reading);dom$readingBody.appendChild(article);ui$selected=selected;ui$phone=true;
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
        const before={history:history.slice(),checkpoints,remembered,
          resized,selected:ui$selected,selects:dom$search.selects||0};
        const event=emit(document,'keydown',Object.assign({target:reading},setting));
        assert.equal(event.defaultPrevented,true);assert.deepEqual(order,['focus','select']);
        assert.equal(document.activeElement,dom$search);assert.equal(dom$search.selects,before.selects+1);
        assert.strictEqual(currentBundle,bundle);assert.strictEqual(data,projection);assert.strictEqual(ui$selected,before.selected);
        assert.equal(window.location.href,href);assert.deepEqual(history,before.history);assert.equal(checkpoints,before.checkpoints);
        assert.equal(resized,before.resized);assert.equal(remembered,before.remembered);
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
        reading.focus();const before={resized,checkpoints,history:history.slice()};
        const event=emit(document,'keydown',Object.assign({target:reading},setting));
        assert.equal(event.defaultPrevented,Boolean(setting.defaultPrevented));assert.equal(document.activeElement,reading);
        assert(host.classList.contains('bp-dag-sheet-full'));assert.equal(main.getAttribute('inert'),'');
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


def test_reader_escape_clears_a_selection_with_one_history_entry_and_none_without() -> None:
    _controls(r"""
      const reading=new Element('a');dom$readingBody.appendChild(reading);reading.focus();const before=JSON.stringify(sharedIndex);
      assert.equal(emit(dom$inspector,'keydown',{target:reading,key:'Escape'}).defaultPrevented,false);
      clearSelection(true,true);assert.equal(checkpoints,0);assert.deepEqual(history,[]);assert.equal(sheets.length,0);
      ui$selected={id:'A',title:'A',kind:'node',status:'planned'};
      assert.equal(emit(dom$inspector,'keydown',{target:reading,key:'Escape'}).defaultPrevented,true);
      assert.equal(ui$selected,null);assert.equal(checkpoints,1);assert.deepEqual(history,['push']);
      assert.deepEqual(sheets,[{open:true,selected:null}],'The desktop reader stays open beside the map');
      assert.equal(document.activeElement,reading);assert.equal(JSON.stringify(sharedIndex),before);
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
