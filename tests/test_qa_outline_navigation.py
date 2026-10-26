"""Bounded actual-source regressions for the outline and phone navigation policy."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.test_integrated_reader_focus import _ENVIRONMENT as _READER_PEERS
from tests.test_wiki_workspace_controls import _CLIENT, _CONTROL_PEERS, _LOADER, _NODE

pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated outline/navigation callbacks")


def _run(body: str, names: list[str], peers: str = "") -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _LOADER
    script += "Object.assign(context,{URL,clearTimeout});\n"
    script += "const callbacks=" + json.dumps(names) + ".map(original).join('\\n');\n"
    script += "const start=source.indexOf('    var dom$announcement,');assert(start>=0);\n"
    script += "const domDeclarations=source.slice(start,source.indexOf(';',start)+1);\n"
    script += "let complete=false;process.once('beforeExit',()=>{if(!complete){"
    script += "console.error('QA callback test body did not complete');process.exitCode=1;}});\n"
    program = peers + "\n(async()=>{\n" + body + "\n})()"
    script += "const pending=vm.runInContext(constants+'\\n'+domDeclarations+'\\n'+callbacks+" + json.dumps(program)
    script += ",context,{timeout:3000,filename:CLIENT});\n"
    script += "Promise.resolve(pending).then(()=>{complete=true;},error=>{"
    script += "complete=true;console.error(error);process.exitCode=1;});\n"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


_PROJECTION_PEERS = r"""
function scope(id,parent,extra={}) {
  return Object.assign({id,parent,scope:id,kind:'scope',title:id,result:false,
    status:'planned',route:'https://example.test/project/roadmap/'+encodeURIComponent(id)+'/index.html'},extra);
}
function page(id,parent,extra={}) {
  return Object.assign(scope(id,parent),{scope:'roadmap',kind:'node',result:true},extra);
}
"""

_OUTLINE_PEERS = (
    _CONTROL_PEERS.replace("function renderNavigation(){}", "")
    .replace("function trailList() {return new Element('ol');}", "")
    .replace("buildShell();bindEvents();syncControls();", "")
    .replace("function remember(){}", "function remember(){remembered++;}")
    + _PROJECTION_PEERS + r"""
var ui$outlineLimit=120,ui$requestedNode=null,remembered=0;
const dispatched=[],decluttered=[],mathRoots=[];
navigation.navigateNode=(id,options)=>dispatched.push({id,options});
navigation.declutterReader=(root,ids)=>decluttered.push({root,ids});
window.AutoformMath={refresh(root){mathRoots.push(root);return Promise.resolve(true);}};
dom$navigationInner=new Element('div');dom$readingBody=new Element('article');
dom$homeLink=new Element('a');dom$breadcrumb=new Element('nav');
function configureOutline(nodes,id,relations=[]) {
  records.clear();nodes.forEach(node=>records.set(node.id,node));
  sharedIndex={nodes,relations};
  currentBundle={nodeId:id,scope:records.get(id).scope,url:records.get(id).route,title:records.get(id).title,graph:{view:{}}};
}
function outlineLinks() {return dom$navigationInner.all().filter(node=>node.tagName==='A');}
"""
)

_OUTLINE_CALLBACKS = [
    "element", "button", "readingLabel", "safeHref", "outlineProjection", "renderOutline", "renderNavigation",
    "refreshReadingLabels", "semanticParent", "routeTrail", "currentOverviewGroup", "trailList", "renderBreadcrumb",
]

_PHONE_PEERS = (
    _READER_PEERS.replace("function sheetLayout() {return false;}", "")
    + r"""
window.matchMedia=()=>({matches:true});
const overviewControl=new Element('button');overviewControl.className='bp-dag-node';
overviewControl.dataset.autoformNodeId='topic';overviewControl.setAttribute('tabindex','0');
dom$nodeLayer=new Element('div');dom$nodeLayer.replaceChildren(overviewControl);
dom$nodeLayer.querySelector=selector=>{
  assert.equal(selector,'.bp-dag-node[tabindex="0"]');return overviewControl;
};
"""
)

_PHONE_CALLBACKS = [
    "captureState", "snapshotForRoute", "applyRoute", "restoreState", "setFilters", "setBrowse", "sheetLayout",
]

_INVENTORY_PEERS = _OUTLINE_PEERS + r"""
var ui$selected=null,ui$requestedNode='F',ui$query='',ui$relationIds=null,ui$listLimit=100,ui$mode='graph';
var ui$selectedStatuses=new Set(['planned']),palette={planned:{label:'Planned'}};
dom$readingContext=new Element('div');dom$readingNeighbors=new Element('div');
dom$inventory=new Element('section');dom$inventory.appendChild(new Element('h2'));
dom$inventoryList=new Element('ul');dom$inventorySummary=new Element('span');
dom$inventoryScroll=new Element('div');dom$loadMore=new Element('button');
// Mode dispatch is a peer; both the Show-all callback and the complete
// inventory renderer are extracted verbatim from production.
function setMode(mode,push) {modes.push({mode,push});ui$mode=mode;renderInventory(true);}
"""

_INVENTORY_CALLBACKS = [
    "element", "button", "readingLabel", "refreshReadingLabels", "renderReaderContext", "neighborIds",
    "relationCounts", "topicPeers", "semanticParent", "sourceNodes", "inCurrentScope", "inventoryNodes", "renderInventory", "statusLabel",
]


def test_qa_runner_rejects_an_unfinished_async_body() -> None:
    with pytest.raises(AssertionError, match="QA callback test body did not complete"):
        _run("await new Promise(()=>{});assert.fail('Must not reach this assertion');", [])


@pytest.mark.parametrize("branch_first", [False, True])
def test_outline_walks_authored_parent_tree_not_interleaved_dependency_rows(branch_first: bool) -> None:
    _run("const branchFirst=" + json.dumps(branch_first) + ";\n" + r"""
      const root=scope('roadmap',null),schemes=scope('schemes','roadmap');
      const original=[root,scope('cohomology','roadmap'),scope('varieties','schemes'),scope('curves','roadmap'),
        page('chapter-zero','roadmap'),scope('coherent','schemes'),scope('fine','varieties'),schemes,
        scope('deep','fine'),scope('deepest','deep'),page('not-a-root-page','varieties',{scope:'varieties'})];
      const nodes=branchFirst?[root,schemes,...original.filter(node=>node!==root&&node!==schemes)]:original;
      const before=JSON.stringify(nodes),result=outlineProjection(nodes,'deepest','deepest',120);
      const branch=['schemes','varieties','fine','deep','deepest','coherent'];
      const siblings=['cohomology','curves','chapter-zero'];
      assert.deepEqual(result.entries.map(entry=>entry.node.id),branchFirst?branch.concat(siblings):siblings.concat(branch));
      assert.strictEqual(result.root,root);
      const depths=new Map(result.entries.map(entry=>[entry.node.id,entry.depth]));
      for(const id of siblings.concat('schemes')) assert.equal(depths.get(id),0);
      assert.equal(depths.get('varieties'),1);assert.equal(depths.get('coherent'),1);
      assert.equal(depths.get('fine'),2);assert.equal(depths.get('deep'),3);
      assert(depths.get('deepest')>=3);
      assert(result.entries.every(entry=>entry.node===nodes.find(node=>node.id===entry.node.id)));
      assert.equal(result.entries.some(entry=>entry.node.id==='not-a-root-page'),false);
      assert.equal(JSON.stringify(nodes),before,'Outline projection must not rewrite authored containment or titles');
    """, ["outlineProjection"], _PROJECTION_PEERS)


def test_outline_keeps_collection_subhead_and_root_page_through_transparent_wrapper() -> None:
    _run(r"""
      const root=scope('roadmap',null),collection=scope('authored-collection','roadmap',{navigation:'collection'});
      const transparent=scope('pagination','authored-collection',{navigation:'transparent'});
      const topic=scope('deep/path/topic','pagination');
      const nodes=[root,topic,collection,page('chapter-zero','roadmap'),scope('schemes','roadmap'),transparent,
        scope('source','authored-collection',{navigation:'source'}),page('reading-note','schemes',{scope:'schemes'})];
      const before=JSON.stringify(nodes),result=outlineProjection(nodes,'deep/path/topic','deep/path/topic',120);
      assert.deepEqual(result.entries.map(entry=>entry.node.id),['authored-collection','deep/path/topic','chapter-zero','schemes']);
      const heading=result.entries.find(entry=>entry.node.id===collection.id);
      assert(heading.collection,'The actual authored collection is a subhead, not a made-up area');
      assert.strictEqual(heading.node,collection);
      assert(result.entries.every(entry=>!['pagination','source','reading-note'].includes(entry.node.id)));
      assert.equal(JSON.stringify(nodes),before);
    """, ["outlineProjection"], _PROJECTION_PEERS)


def test_rendered_outline_preserves_depth_current_leaf_and_native_canonical_links() -> None:
    _run(r"""
      const root=scope('roadmap',null),collection=scope('collection','roadmap',{navigation:'collection'});
      const wrapper=scope('transparent','collection',{navigation:'transparent'});
      const nodes=[root,scope('cohomology','roadmap'),scope('varieties','schemes'),scope('curves','roadmap'),
        page('chapter-zero','roadmap'),scope('coherent','schemes'),scope('fine','varieties'),scope('schemes','roadmap'),
        scope('deep','fine'),scope('deepest','deep'),scope('wrapped-topic','transparent'),collection,wrapper];
      configureOutline(nodes,'deepest',[['schemes','varieties',2,3],['varieties','schemes',0,4]]);
      const before=JSON.stringify(sharedIndex);
      renderNavigation();
      const links=outlineLinks();
      assert.deepEqual(links.map(link=>link.dataset.navigationNodeId),
        ['cohomology','curves','chapter-zero','schemes','varieties','fine','deep','deepest','coherent','wrapped-topic']);
      const byId=new Map(links.map(link=>[link.dataset.navigationNodeId,link]));
      assert.equal(byId.get('varieties').parentElement.className,'bp-outline-depth-1');
      assert.equal(byId.get('coherent').parentElement.className,'bp-outline-depth-1');
      assert.equal(byId.get('fine').parentElement.className,'bp-outline-depth-2');
      assert.equal(byId.get('deep').parentElement.className,'bp-outline-depth-3');
      assert.equal(byId.get('deepest').parentElement.className,'bp-outline-depth-3');
      assert.equal(byId.get('chapter-zero').parentElement.className,'bp-outline-depth-0');
      assert.equal(byId.get('deepest').getAttribute('aria-current'),'page');
      assert(links.filter(link=>link.getAttribute('aria-current')==='page').length===1);
      const subhead=dom$navigationInner.all().find(node=>node.className==='bp-outline-collection');
      assert(subhead);assert.equal(subhead.textContent,collection.title);
      assert.equal(subhead.tagName,'SPAN');assert.equal(subhead.all().some(node=>['A','BUTTON'].includes(node.tagName)),false);
      assert.equal(dom$homeLink.href,homeURL);
      assert.deepEqual(decluttered.at(-1).ids,
        ['cohomology','curves','schemes','varieties','fine','deep','deepest','coherent','wrapped-topic']);
      assert.strictEqual(decluttered.at(-1).root,dom$readingBody);
      assert.deepEqual(mathRoots,[dom$navigationInner],'Outline refresh must not re-typeset the mathematical article');
      for(const id of ['chapter-zero','varieties']) {
        const link=byId.get(id),record=records.get(id),count=dispatched.length;
        assert.equal(link.href,record.route);
        for(const settings of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{button:2},{defaultPrevented:true}]) {
          const event=emit(link,'click',Object.assign({button:0},settings));
          assert.equal(event.defaultPrevented,Boolean(settings.defaultPrevented));assert.equal(dispatched.length,count);
        }
        assert.equal(emit(link,'click',{button:0}).defaultPrevented,true);
        assert.deepEqual(dispatched.at(-1),{id,options:{history:'push'}});
      }
      assert.equal(JSON.stringify(sharedIndex),before,'Rendering and selecting the outline must retain every authored field and edge');
    """, _OUTLINE_CALLBACKS, _OUTLINE_PEERS)


def test_outline_keeps_actual_current_result_link_without_turning_its_peers_into_topics() -> None:
    _run(r"""
      const root=scope('roadmap',null),topic=scope('topic','roadmap'),subtopic=scope('subtopic','topic');
      const selected=page('selected-result','subtopic',{scope:'subtopic'});
      const peer=page('peer-result','subtopic',{scope:'subtopic'});
      const nodes=[root,selected,topic,peer,subtopic];
      configureOutline(nodes,selected.id,[[peer.id,selected.id,2,3]]);
      const before=JSON.stringify(sharedIndex);renderNavigation();
      const links=outlineLinks();assert.deepEqual(links.map(link=>link.dataset.navigationNodeId),[topic.id,subtopic.id,selected.id]);
      assert.equal(links.at(-1).getAttribute('aria-current'),'page');
      assert.equal(links.at(-1).href,selected.route);
      assert.equal(links.at(-1).parentElement.className,'bp-outline-depth-2');
      assert.equal(links[0].getAttribute('aria-current'),null);assert.equal(links[1].getAttribute('aria-current'),null);
      assert.deepEqual(decluttered.at(-1).ids,[topic.id,subtopic.id],
        'The complete selected result is reading, not a duplicated scope index to hide');
      assert.equal(emit(links.at(-1),'click',{button:0}).defaultPrevented,true);
      assert.deepEqual(dispatched.at(-1),{id:selected.id,options:{history:'push'}});
      assert.equal(JSON.stringify(sharedIndex),before);
    """, _OUTLINE_CALLBACKS, _OUTLINE_PEERS)


def test_outline_pagination_keeps_current_scope_and_reveals_all_rows_in_sibling_order() -> None:
    _run(r"""
      const nodes=[scope('roadmap',null),...Array.from({length:13},(_,number)=>scope('T'+number,'roadmap'))];
      configureOutline(nodes,'T12');ui$outlineLimit=3;
      const before=JSON.stringify(sharedIndex);renderNavigation();
      assert.deepEqual(outlineLinks().map(link=>link.dataset.navigationNodeId),['T0','T1','T12']);
      assert.equal(outlineLinks().at(-1).getAttribute('aria-current'),'page');
      const more=dom$navigationInner.all().find(node=>node.tagName==='BUTTON');assert(more);
      emit(more,'click');
      assert.equal(ui$outlineLimit,53);assert.equal(remembered,1);
      assert.deepEqual(outlineLinks().map(link=>link.dataset.navigationNodeId),nodes.slice(1).map(node=>node.id));
      assert.equal(dom$navigationInner.all().some(node=>node.tagName==='BUTTON'),false);
      assert.equal(JSON.stringify(sharedIndex),before);
    """, _OUTLINE_CALLBACKS, _OUTLINE_PEERS)


def test_site_home_breadcrumb_is_one_current_item_and_topic_trail_has_a_native_home_link() -> None:
    _run(r"""
      const root=scope('roadmap',null),topic=scope('topic','roadmap');configureOutline([root,topic],'topic');
      homeURL='https://example.test/project/index.html';
      currentBundle.url=homeURL;currentBundle.nodeId=null;currentBundle.scope='roadmap';
      const before=JSON.stringify(sharedIndex);renderBreadcrumb();
      const items=dom$breadcrumb.children[0].children;
      assert.equal(items.length,1);assert.equal(items[0].getAttribute('aria-current'),'page');
      assert.equal(dom$breadcrumb.all().some(node=>node.tagName==='A'),false);
      currentBundle={nodeId:topic.id,scope:topic.id,url:topic.route,title:topic.title,graph:{view:{}}};
      renderBreadcrumb();
      const links=dom$breadcrumb.all().filter(node=>node.tagName==='A');
      assert(links.some(link=>link.href===homeURL),'A topic trail must lead to the actual deployment home');
      assert.equal(dom$breadcrumb.children[0].children.at(-1).getAttribute('aria-current'),'page');
      const link=links.find(link=>link.href===homeURL);
      for(const settings of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{button:2},{defaultPrevented:true}]) {
        const count=navigationCalls.length,event=emit(link,'click',Object.assign({button:0},settings));
        assert.equal(event.defaultPrevented,Boolean(settings.defaultPrevented));assert.equal(navigationCalls.length,count);
      }
      assert.equal(emit(link,'click',{button:0}).defaultPrevented,true);
      assert.equal(navigationCalls.at(-1).url,homeURL);
      assert.deepEqual(mathRoots,[dom$breadcrumb,dom$breadcrumb]);
      assert.equal(JSON.stringify(sharedIndex),before);
    """, _OUTLINE_CALLBACKS, _OUTLINE_PEERS)


def test_repeated_fallback_breadcrumbs_do_not_modify_published_provider_entries() -> None:
    _run(r"""
      const reading={format:'inline-parts-v1',source:'Parent $V$',parts:[['text','Parent '],['math','\\(V\\)']]};
      const entries=[{label:'Parent $V$',reading_title:reading,url:'https://example.test/project/roadmap/parent/index.html'}];
      const leaf=page('leaf','missing-owner',{scope:'missing-owner'});
      records.clear();records.set(leaf.id,leaf);
      currentBundle={nodeId:leaf.id,scope:leaf.scope,url:leaf.route,title:leaf.title,
        graph:{view:{breadcrumbs:entries},nodes:[leaf],edges:[]}};
      const before=JSON.stringify(currentBundle.graph);
      const first=routeTrail(currentBundle),second=routeTrail(currentBundle);
      assert.notStrictEqual(first,entries);assert.notStrictEqual(second,entries);
      assert.deepEqual(first,second,'Refreshing a fallback trail must not append another result or Home entry');
      assert.equal(entries.length,1);assert.strictEqual(entries[0].reading_title,reading);
      renderBreadcrumb();renderBreadcrumb();
      assert.equal(JSON.stringify(currentBundle.graph),before,'Fallback navigation must not mutate captured graph metadata');
    """, _OUTLINE_CALLBACKS, _OUTLINE_PEERS)


@pytest.mark.parametrize("url", [
    "https://example.test/project/scopes/topic.json",
    "https://example.test/project/scopes/topic.json?edition=2#main",
    "https://example.test/project/scopes/topic%2ejson",
    "./scopes/topic.json",
    "https://example.test/project/javascripts/blueprint-explorer.min.js",
    "javascript:alert(1)",
    "https://other.example.test/project/roadmap/topic/index.html",
    "https://user:password@example.test/project/roadmap/topic/index.html",
])
def test_error_notice_never_offers_json_or_unsafe_url_as_a_complete_article(url: str) -> None:
    _run("const url=" + json.dumps(url) + ";\n" + r"""
      dom$announcement=new Element('div');dom$error=new Element('div');
      showError({message:'Fixture fetch failure',url});
      assert.equal(dom$error.hidden,false);
      assert.equal(dom$error.all().some(node=>node.tagName==='A'),false);
    """, ["element", "safeHref", "showError"], _OUTLINE_PEERS)


@pytest.mark.parametrize("url", [
    "https://example.test/project/roadmap/topic/index.html",
    "https://example.test/project/roadmap/topic/?edition=2#main",
    "../roadmap/topic/index.html?edition=2#main",
])
def test_error_notice_keeps_a_safe_canonical_html_new_tab_fallback(url: str) -> None:
    _run("const url=" + json.dumps(url) + ";\n" + r"""
      dom$announcement=new Element('div');dom$error=new Element('div');
      showError({message:'Fixture renderer failure',url});
      const links=dom$error.all().filter(node=>node.tagName==='A');assert.equal(links.length,1);
      assert.equal(new URL(links[0].href,window.location.href).href,new URL(url,window.location.href).href);
      assert.equal(links[0].target,'_blank');assert.equal(links[0].rel,'noopener');
      assert.equal(dom$error.hidden,false);
    """, ["element", "safeHref", "showError"], _OUTLINE_PEERS)


@pytest.mark.parametrize(("intent", "opened"), [
    ("no-node", False), ("roadmap", False), ("topic", False), ("focus", True), ("map-first", False), ("anchor", True),
    ("snapshot-open", True), ("snapshot-closed", False), ("result", True), ("article", True),
    ("chapter-math", False), ("chapter-math-focus", True),
])
def test_phone_topic_boundary_overview_peeks_without_overriding_reading_intent(intent: str, opened: bool) -> None:
    _run("const intent=" + json.dumps(intent) + ",opened=" + json.dumps(opened) + ";\n" + r"""
      const root={id:'roadmap',scope:'roadmap',parent:null,kind:'scope',result:false,title:'Library',
        status:'fully_proved',route:'https://example.test/project/roadmap/index.html'};
      const topic={id:'topic',scope:'topic',parent:'roadmap',kind:'scope',result:false,title:'Authored topic',
        status:'fully_proved',route:'https://example.test/project/roadmap/topic/index.html'};
      const external={id:'external',scope:'external',parent:'roadmap',kind:'scope',result:false,title:'External topic',
        status:'fully_proved',route:'https://example.test/project/roadmap/external/index.html'};
      const boundary=Object.assign({},external,{id:'boundary:external',kind:'boundary',_recordId:'external'});
      byId=new Map([[topic.id,topic],[boundary.id,boundary]]);records=new Map([[root.id,root],[topic.id,topic],[external.id,external]]);
      let id=intent==='no-node'?null:intent==='roadmap'?'roadmap':'topic';
      if(intent.startsWith('chapter-math')) {
        const chapter=Object.assign({},topic,{id:'chapter-seven',scope:'chapter-seven',title:'Authored chapter 7',
          route:'https://example.test/project/roadmap/chapter-seven/index.html'});
        const concrete=['A','B'].map(key=>({id:key,scope:chapter.id,parent:chapter.id,kind:'node',result:true,
          title:key,status:'fully_proved',route:chapter.route+'#'+key}));
        byId=new Map(concrete.map(node=>[node.id,node]));records.set(chapter.id,chapter);
        concrete.forEach(node=>records.set(node.id,node));id=chapter.id;
      }
      if(intent==='result'||intent==='article') {
        id=intent==='result'?'A':'M';
        const concrete={id,scope:topic.id,parent:topic.id,kind:'node',result:intent==='result',
          title:id,status:'fully_proved',route:topic.route+'#'+id};
        if(intent==='article') {
          concrete.catalog='module';concrete.status='inventory_pending';
          palette.inventory_pending={};presentStatuses.push('inventory_pending');
        }
        byId.set(id,concrete);records.set(id,concrete);
      }
      const saved=intent.startsWith('snapshot-')?{node:topic.id,requestedNode:topic.id,mode:'graph',
        statuses:['fully_proved'],readingOpen:opened,focus:opened?'reading':'search',
        camera:{x:145,y:-78,scale:1.1},readingScroll:155,outlineScroll:42,inventoryScroll:8}:null;
      const focus=intent==='focus'||intent==='chapter-math-focus';
      const incoming=bundle(id,saved,focus||intent==='map-first');
      if(intent==='map-first') incoming.mapFirst=true;
      incoming.scope=records.get(id)?.scope||'roadmap';incoming.url=records.get(id)?.route||root.route;
      if(intent!=='anchor') incoming.anchor=null;
      const before=JSON.stringify(Array.from(records.values()));
      dom$search.focus({});const ready=applyRoute(incoming);
      assert.equal(hostClasses.has('bp-dag-sheet-open'),opened,
        'Phone overview intent must be independent of whether the canonical route supplies a scope node ID');
      await frame();await ready;
      assert.equal(dom$readingBody.children[0],incoming.readingSource,'The complete authored article must remain mounted');
      // With no statement anchor, reading focus goes to the page's own heading.
      assert.equal(incoming.pageHeading.focuses.length,focus?1:0);
      if(focus) {
        assert.equal(document.activeElement,incoming.pageHeading);assert.equal(incoming.pageHeading.focuses[0].preventScroll,true);
      } else if(intent==='map-first') {
        assert.equal(document.activeElement,overviewControl);
        assert.equal(overviewControl.focuses.length,1);assert.equal(overviewControl.focuses[0].preventScroll,true);
      } else assert.equal(document.activeElement,dom$search);
      if(intent==='anchor') {
        assert(incoming.target.classList.contains('bp-reading-current'));
        assert.equal(dom$readingBody.scrollTop,276,'Explicit native anchors must still use the real scroll calculation');
      } else assert.equal(dom$readingBody.scrollTop,0);
      if(saved) {
        const restored=restoreState(saved,incoming);await frame();await restored;
        assert.equal(hostClasses.has('bp-dag-sheet-open'),opened);
        assert.equal(ui$requestedNode,topic.id);assert.equal(ui$selected.id,topic.id);
        assert.deepEqual(ui$transform,saved.camera);
        assert.equal(dom$readingBody.scrollTop,155);assert.equal(dom$navigation.scrollTop,42);assert.equal(dom$inventoryScroll.scrollTop,8);
        assert.equal(document.activeElement,opened?dom$readingBody:dom$search);
        assert.equal(document.activeElement.focuses.at(-1).preventScroll,true);
      }
      assert.equal(JSON.stringify(Array.from(records.values())),before);
      assert.deepEqual(mathRoots,[dom$readingBody]);
    """, _PHONE_CALLBACKS, _PHONE_PEERS)
