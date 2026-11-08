"""Bounded actual-source regressions for the outline and phone navigation policy."""

from __future__ import annotations

import json
import subprocess

import pytest

from autoform_cli import project_map
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
dom$breadcrumb=new Element('nav');
function configureOutline(nodes,id,relations=[]) {
  records.clear();nodes.forEach(node=>records.set(node.id,node));
  sharedIndex={nodes,relations};
  currentBundle={nodeId:id,scope:records.get(id).scope,url:records.get(id).route,title:records.get(id).title,graph:{view:{}}};
}
function outlineLinks() {return dom$navigationInner.all().filter(node=>node.tagName==='A');}
"""
)

_OUTLINE_CALLBACKS = [
    "element", "button", "readingLabel", "bindNodeLink", "safeHref", "outlineProjection", "renderOutline", "renderNavigation",
    "refreshReadingLabels", "tooltip", "semanticParent", "routeTrail", "trailList", "plainTitle", "renderBreadcrumb",
]

_PHONE_PEERS = (
    _READER_PEERS.replace("function sheetLayout() {return false;}", "")
    + r"""
window.matchMedia=()=>({matches:true});
"""
)

_PHONE_CALLBACKS = [
    "captureState", "snapshotForRoute", "applyRoute", "plainTitle", "selection", "restoreState", "setFilters", "setBrowse", "sheetLayout", "mapControl",
]

_INVENTORY_PEERS = _OUTLINE_PEERS + r"""
var ui$selected=null,ui$requestedNode='F',ui$query='',ui$relationIds=null,ui$listLimit=100,ui$mode='graph';
var ui$selectedStatuses=new Set(['planned']),palette={planned:{label:'Planned'}};
dom$readingContext=new Element('div');
dom$inventory=new Element('section');dom$inventory.appendChild(new Element('h2'));
dom$inventoryList=new Element('ul');dom$inventorySummary=new Element('span');
dom$inventoryScroll=new Element('div');dom$loadMore=new Element('button');
// Mode dispatch is a peer; both the Show-all callback and the complete
// inventory renderer are extracted verbatim from production.
function setMode(mode,push) {modes.push({mode,push});ui$mode=mode;renderInventory(true);}
"""

_INVENTORY_CALLBACKS = [
    "element", "button", "readingLabel", "bindNodeLink", "refreshReadingLabels", "renderReaderContext",
    "semanticParent", "sourceNodes", "inCurrentScope", "inventoryNodes", "renderInventory", "statusLabel",
]


def test_qa_runner_rejects_an_unfinished_async_body() -> None:
    with pytest.raises(AssertionError, match="QA callback test body did not complete"):
        _run("await new Promise(()=>{});assert.fail('Must not reach this assertion');", [])


@pytest.mark.parametrize("focus_parent", [True, False])
def test_breadcrumb_refolding_retains_canonical_link_focus_or_the_real_nav_fallback(focus_parent: bool) -> None:
    _run("const focusParent=" + json.dumps(focus_parent) + ";\n" + r"""
      const root=scope('roadmap',null),a=scope('A','roadmap'),b=scope('B','A'),c=scope('C','B');
      const result=page('C/result','C',{scope:'C'});configureOutline([root,a,b,c,result],result.id);
      dom$breadcrumb.clientWidth=600;dom$breadcrumb.scrollWidth=500;renderBreadcrumb();
      const href=(focusParent?c:b).route;
      const old=dom$breadcrumb.querySelectorAll('a').find(link=>link.href===href);assert(old);old.focus();
      const before=JSON.stringify(sharedIndex);sheetLayout=()=>true;dom$breadcrumb.clientWidth=300;renderBreadcrumb();
      const first=dom$breadcrumb.querySelector('.bp-dag-book');assert.equal(first.href,homeURL);
      assert.equal(dom$breadcrumb.firstChild.children.length,4);
      if(focusParent){
        const current=dom$breadcrumb.querySelectorAll('a').find(link=>link.href===href);
        assert(current);assert.notStrictEqual(current,old);assert.equal(document.activeElement,current);
        assert.deepEqual(current.focuses.at(-1),{preventScroll:true});
      }else{
        assert.equal(dom$breadcrumb.querySelectorAll('a').some(link=>link.href===href),false);
        assert.equal(document.activeElement,dom$breadcrumb);assert.equal(dom$breadcrumb.tagName,'NAV');
        assert.equal(dom$breadcrumb.tabIndex,-1);assert.deepEqual(dom$breadcrumb.focuses.at(-1),{preventScroll:true});
      }
      assert.equal(renderBreadcrumb.$width,300);assert.equal(JSON.stringify(sharedIndex),before);
    """, _OUTLINE_CALLBACKS, _OUTLINE_PEERS)


def test_actual_resize_refolds_only_for_breadcrumb_width_changes_and_keeps_height_only_focus_identity() -> None:
    _run(r"""
      const root=scope('roadmap',null),a=scope('A','roadmap'),b=scope('B','A'),c=scope('C','B');
      const result=page('C/result','C',{scope:'C'});configureOutline([root,a,b,c,result],result.id);
      dom$stage={clientWidth:800,clientHeight:600};
      dom$breadcrumb.clientWidth=600;dom$breadcrumb.scrollWidth=500;renderBreadcrumb();
      const firstList=dom$breadcrumb.firstChild,first=dom$breadcrumb.querySelectorAll('a').find(link=>link.href===c.route);
      first.focus();const before=JSON.stringify(sharedIndex),mathCount=mathRoots.length;
      dom$stage.clientHeight=650;resize();
      assert.strictEqual(dom$breadcrumb.firstChild,firstList);assert.equal(document.activeElement,first);
      assert.equal(mathRoots.length,mathCount);
      dom$breadcrumb.clientWidth=300;dom$breadcrumb.scrollWidth=700;resize();
      const secondList=dom$breadcrumb.firstChild,current=dom$breadcrumb.querySelectorAll('a').find(link=>link.href===c.route);
      assert.notStrictEqual(secondList,firstList);assert.equal(document.activeElement,current);
      assert.deepEqual(current.focuses.at(-1),{preventScroll:true});assert.equal(mathRoots.length,mathCount+1);
      assert.strictEqual(mathRoots.at(-1),dom$breadcrumb);assert.equal(renderBreadcrumb.$width,300);
      resize();assert.strictEqual(dom$breadcrumb.firstChild,secondList);assert.equal(document.activeElement,current);
      assert.equal(mathRoots.length,mathCount+1);assert.equal(JSON.stringify(sharedIndex),before);
    """, _OUTLINE_CALLBACKS + ["resize"], _OUTLINE_PEERS.replace("function resize(){}", ""))


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


def test_the_outline_reads_the_index_records_the_mount_keeps_and_projects_the_same_tree() -> None:
    _run(r"""
      const root=scope('roadmap',null),collection=scope('collection','roadmap',{navigation:'collection'});
      const nodes=[root,scope('schemes','roadmap'),scope('varieties','schemes'),scope('fine','varieties'),collection,
        scope('wrapper','collection',{navigation:'transparent'}),scope('wrapped','wrapper'),scope('source','roadmap',{navigation:'source'}),
        page('chapter-zero','roadmap'),page('result','fine',{scope:'fine'}),scope('loop','knot',{navigation:'transparent'}),
        scope('knot','loop',{navigation:'transparent'}),scope('tangled','loop')];
      const known=new Map(nodes.map(node=>[node.id,node]));
      for(const [scopeId,id,limit] of [['roadmap',null,120],['fine','result',120],['wrapped','wrapped',2],['tangled','tangled',120]])
        assert.deepEqual(outlineProjection(nodes,scopeId,id,limit,known),outlineProjection(nodes,scopeId,id,limit));
      // Rendering maps the index by ID only once, in the records the mount keeps for it.
      configureOutline(nodes,'result');const get=records.get;let reads=0;
      records.get=function(id){reads++;return get.call(this,id);};
      renderNavigation();assert(reads>0,'The outline mapped the index again instead of reading its records');
      assert.deepEqual(outlineLinks().map(link=>link.dataset.navigationNodeId),
        outlineProjection(nodes,'fine','result',120).entries.filter(entry=>!entry.collection).map(entry=>entry.node.id));
    """, _OUTLINE_CALLBACKS, _OUTLINE_PEERS)


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
      assert.deepEqual(decluttered.at(-1).ids,
        ['cohomology','curves','schemes','varieties','fine','deep','deepest','coherent','wrapped-topic']);
      assert.strictEqual(decluttered.at(-1).root,dom$readingBody);
      assert.deepEqual(mathRoots,[dom$navigationInner],'Outline refresh must not re-typeset the mathematical article');
      renderBreadcrumb();assert.equal(dom$breadcrumb.querySelector('.bp-dag-book').href,homeURL);
      assert.deepEqual(mathRoots,[dom$navigationInner,dom$breadcrumb]);
      for(const id of ['chapter-zero','varieties']) {
        const link=byId.get(id),record=records.get(id),count=dispatched.length;
        assert.equal(link.href,record.route);
        for(const settings of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{button:2},{defaultPrevented:true}]) {
          const event=emit(link,'click',Object.assign({button:0},settings));
          assert.equal(event.defaultPrevented,Boolean(settings.defaultPrevented));assert.equal(dispatched.length,count);
        }
        assert.equal(emit(link,'click',{button:0}).defaultPrevented,true);
        assert.deepEqual(dispatched.at(-1),{id,options:{history:'push',focus:true}});
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
      assert.deepEqual(dispatched.at(-1),{id:selected.id,options:{history:'push',focus:true}});
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


def test_outline_heads_each_area_run_with_its_title() -> None:
    _run(r"""
      const root=scope('roadmap',null),chapter=(id,area)=>scope(id,'roadmap',area?{area}:{});
      const shape=(nodes,id,limit)=>outlineProjection(nodes,id,id,limit).entries.map(entry=>[entry.node.id,entry.depth,entry.collection]);
      // A one-chapter area and a chapter without an area keep their place, and an area that comes back is headed again.
      // A statement or a collection at the top neither counts toward an area nor goes under one.
      const nodes=[root,chapter('logic','Foundations'),chapter('sets','Foundations'),chapter('algebra','Algebra'),
        page('lemma','roadmap',{area:'Algebra'}),chapter('notes'),chapter('geometry','Geometry'),chapter('topology','Geometry'),
        chapter('models','Foundations'),scope('library','roadmap',{navigation:'collection',area:'Algebra'}),
        scope('manifolds','topology'),scope('smooth','manifolds'),scope('lie','smooth'),scope('groups','lie')];
      const before=JSON.stringify(nodes);
      // The heading takes a level, so the current page sits one level deeper before the indent stops growing.
      assert.deepEqual(shape(nodes,'groups',120),[['region:Foundations',0,true],['logic',1,false],['sets',1,false],
        ['algebra',0,false],['lemma',0,false],['notes',0,false],['region:Geometry',0,true],['geometry',1,false],
        ['topology',1,false],['manifolds',2,false],['smooth',3,false],['lie',4,false],['groups',4,false],
        ['region:Foundations',0,true],['models',1,false],['library',0,true]]);
      assert.deepEqual(outlineProjection(nodes,'roadmap',null,120).entries.filter(entry=>entry.node.id.startsWith('region:'))
        .map(entry=>entry.node.title),['Foundations','Geometry','Foundations']);
      // However short the list, the current chapter keeps its heading, and a heading cut off from its chapters waits.
      assert.deepEqual(shape(nodes,'topology',1),[['region:Geometry',0,true],['topology',1,false]]);
      assert.deepEqual(shape(nodes,'topology',3),[['region:Geometry',0,true],['topology',1,false]]);
      assert.deepEqual(shape(nodes,'topology',5),[['region:Foundations',0,true],['logic',1,false],['sets',1,false],
        ['region:Geometry',0,true],['topology',1,false]]);
      // A chapter without an area ends a run, so its area's next chapter is headed again rather than set under it.
      assert.deepEqual(shape([root,chapter('c0','Algebra'),chapter('c1'),chapter('c2','Algebra'),chapter('c3','Geometry'),
        chapter('c4','Geometry')],'roadmap',120),[['region:Algebra',0,true],['c0',1,false],['c1',0,false],
        ['region:Algebra',0,true],['c2',1,false],['region:Geometry',0,true],['c3',1,false],['c4',1,false]]);
      // Areas head nothing until the chapters name two of them and one holds two chapters.
      for(const areas of [[null,null],['Algebra','Algebra'],['Algebra','Algebra',null],['Algebra','Geometry']])
        assert.deepEqual(shape([root,...areas.map((area,index)=>chapter('c'+index,area))],'roadmap',120),
          areas.map((_,index)=>['c'+index,0,false]));
      assert.equal(JSON.stringify(nodes),before);
    """, ["outlineProjection"], _PROJECTION_PEERS)


def test_area_headings_are_text_and_keep_the_current_link_and_paging_focus() -> None:
    _run(r"""
      const root=scope('roadmap',null),chapter=(id,area)=>scope(id,'roadmap',{area});
      const nodes=[root,chapter('logic','Foundations'),chapter('sets','Foundations'),chapter('geometry','Geometry'),
        chapter('topology','Geometry')];
      configureOutline(nodes,'topology');ui$outlineLimit=1;
      const before=JSON.stringify(sharedIndex);renderNavigation();
      const headings=()=>dom$navigationInner.all().filter(node=>node.className==='bp-outline-collection');
      assert.deepEqual(headings().map(node=>[node.tagName,node.textContent,node.parentElement.className]),
        [['SPAN','Geometry','bp-outline-depth-0']]);
      assert.deepEqual(outlineLinks().map(link=>[link.dataset.navigationNodeId,link.parentElement.className]),
        [['topology','bp-outline-depth-1']]);
      assert.equal(outlineLinks()[0].getAttribute('aria-current'),'page');
      // Show more sections brings back the rest and focuses the first new link, not a heading.
      emit(dom$navigationInner.all().find(node=>node.tagName==='BUTTON'),'click');
      assert.deepEqual(headings().map(node=>node.textContent),['Foundations','Geometry']);
      assert.deepEqual(outlineLinks().map(link=>link.dataset.navigationNodeId),['logic','sets','geometry','topology']);
      assert.equal(document.activeElement,outlineLinks()[0]);
      assert.deepEqual(decluttered.at(-1).ids,['logic','sets','geometry','topology']);
      assert.equal(JSON.stringify(sharedIndex),before);
    """, _OUTLINE_CALLBACKS, _OUTLINE_PEERS)


@pytest.mark.parametrize(
    ("rows", "regions", "headings", "unheaded"),
    [
        pytest.param(
            [("logic", "roadmap", "Foundations", None), ("sets", "roadmap", "Foundations", None),
             ("algebra", "roadmap", "Algebra", None), ("lemma", "roadmap", "Algebra", "statement"),
             ("geometry", "roadmap", "Geometry", None), ("topology", "roadmap", "Geometry", None),
             ("notes", "roadmap", None, None), ("analysis", "roadmap", "Analysis", None),
             ("probability", "roadmap", "Analysis", None), ("tables", "roadmap", None, None)],
            # The map gathers the chapters without an area, with its part for the root's own statements, under "Other".
            {"Foundations": ["logic", "sets"], "Geometry": ["geometry", "topology"], "Analysis": ["analysis", "probability"],
             "Other": ["notes", "roadmap/~results", "tables"]},
            {"Foundations": ["logic", "sets"], "Geometry": ["geometry", "topology"], "Analysis": ["analysis", "probability"]},
            ["algebra", "lemma", "notes", "tables"],
            id="chapters",
        ),
        pytest.param(
            [("a1", "roadmap", "A", None), ("a2", "roadmap", "A", None), ("b1", "roadmap", "B", None), ("b2", "roadmap", "B", None)],
            {"A": ["a1", "a2"], "B": ["b1", "b2"]}, {"A": ["a1", "a2"], "B": ["b1", "b2"]}, [],
            id="two-areas",
        ),
        pytest.param(
            [("a1", "roadmap", "A", None), ("a2", "roadmap", "A", None), ("x", "roadmap", None, None), ("y", "roadmap", None, None)],
            {}, {}, ["a1", "a2", "x", "y"],
            id="one-area",
        ),
        pytest.param(
            [("a1", "roadmap", "A", None), ("a2", "roadmap", "A", None), ("b1", "roadmap", "B", None),
             ("library", "roadmap", "B", "collection"), ("c1", "library", "B", None), ("c2", "library", "B", None),
             ("notes", "roadmap", None, "source"), ("s1", "notes", "A", None), ("s2", "notes", "C", None),
             ("s3", "notes", "C", None)],
            # The map flattens the collection, so its chapters join B, and moves s1, which names a chapter's area, into A.
            # Contents keeps the collection's subhead and leaves the topics it lifts out of the source track unheaded.
            {"A": ["a1", "a2", "s1"], "B": ["b1", "c1", "c2"]}, {"A": ["a1", "a2"]}, ["b1", "library", "s1", "s2", "s3"],
            id="collection-and-source-track",
        ),
        pytest.param(
            [("logic", "roadmap", "Foundations", None), ("sets", "roadmap", "Foundations", None),
             ("track", "roadmap", None, "source"), ("geometry", "track", "Geometry", None), ("topology", "track", "Geometry", None)],
            {}, {}, ["logic", "sets", "geometry", "topology"],
            id="areas-only-inside-a-source-track",
        ),
    ],
)
def test_area_headings_name_the_regions_the_map_draws(
    rows: list[tuple[str, str, str | None, str | None]], regions: dict[str, list[str]], headings: dict[str, list[str]],
    unheaded: list[str],
) -> None:
    """The outline restates project_map.py's region rule in JS: each book pins both, and every heading is a map region."""
    records: list[dict[str, object]] = [{"id": "roadmap", "kind": "scope", "title": "Library", "parent": None, "result": False}]
    for node_id, parent, area, role in rows:
        record: dict[str, object] = {"id": node_id, "title": node_id, "parent": parent, **({"area": area} if area else {})}
        if role == "statement":
            records.append({**record, "kind": "node", "result": True, "status": "fully_proved"})
            continue
        records.append({**record, "kind": "scope", "result": False, **({"navigation": role} if role else {})})
        if role is None:
            records.append({"id": f"{node_id}/result", "kind": "node", "title": "Result", "parent": node_id, "result": True,
                            "status": "fully_proved"})
    files = project_map.build_project_map(records, [])
    drawn = {}
    for region in files["index.json"]["regions"]:
        if region["id"].startswith("region:"):
            part = files[f"parts/{project_map.part_file(region['id'])}"]["parts"][region["id"]]
            drawn[region["title"]] = sorted(child["id"] for child in part["children"])
    assert drawn == regions
    nodes = [{**record, "scope": record["id"] if record["kind"] == "scope" else record["parent"]} for record in records]
    expected = {"regions": regions, "headings": headings, "unheaded": unheaded}
    _run("const nodes=" + json.dumps(nodes) + ",expected=" + json.dumps(expected) + ";\n" + r"""
      const headed={},unheaded=[];let heading=null;
      for(const entry of outlineProjection(nodes,'roadmap',null,500).entries) {
        if(entry.depth===0) {
          heading=entry.node.id.startsWith('region:')?entry.node.title:null;
          if(heading) headed[heading]=[];else unheaded.push(entry.node.id);
        } else if(heading&&entry.depth===1) headed[heading].push(entry.node.id);
      }
      assert.deepEqual(headed,expected.headings);assert.deepEqual(unheaded,expected.unheaded);
      for(const [title,members] of Object.entries(headed))
        assert(members.every(id=>(expected.regions[title]||[]).includes(id)),'Every heading is one of the map\'s regions');
    """, ["outlineProjection"])


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


def test_authored_scope_entry_requests_map_first_without_changing_result_reading_intent() -> None:
    _run(r"""
      const root=scope('roadmap',null),chapter=scope('chapter','roadmap'),result=page('chapter/result','chapter',{scope:'chapter'});
      configureOutline([root,chapter,result],chapter.id);
      const card={id:'scope:'+chapter.id,kind:'scope',title:chapter.title};
      activateNode(card);
      assert.deepEqual(dispatched.at(-1),{id:chapter.id,options:{history:'push',focus:'reading',mapFirst:true}});
      chapter.result=true;activateNode(card);
      assert.deepEqual(dispatched.at(-1),{id:chapter.id,options:{history:'push',focus:'reading'}});
      activateNode(result);
      assert.deepEqual(dispatched.at(-1),{id:result.id,options:{history:'push',focus:'reading'}});
    """, ["recordId", "activateNode"], _OUTLINE_PEERS)


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
      const boundary=Object.assign({},external,{id:'boundary:external',kind:'boundary'});
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
        assert.equal(document.activeElement,mapSvg);
        assert.equal(mapSvg.focuses.length,1);assert.equal(mapSvg.focuses[0].preventScroll,true);
      } else assert.equal(document.activeElement,dom$search);
      if(intent==='anchor') {
        assert(incoming.target.classList.contains('bp-reading-current'));
        assert.equal(dom$readingBody.scrollTop,276,'Explicit native anchors must still use the real scroll calculation');
      } else assert.equal(dom$readingBody.scrollTop,0);
      if(saved) {
        const restored=restoreState(saved,incoming);await frame();await restored;
        assert.equal(hostClasses.has('bp-dag-sheet-open'),opened);
        // A topic is a place on the map, never the selection, even when its view lists it under its own id.
        assert.equal(ui$requestedNode,topic.id);assert.equal(ui$selected,null);
        assert.equal(dom$readingBody.scrollTop,155);assert.equal(dom$navigation.scrollTop,42);assert.equal(dom$inventoryScroll.scrollTop,8);
        assert.equal(document.activeElement,opened?dom$readingBody:dom$search);
        assert.equal(document.activeElement.focuses.at(-1).preventScroll,true);
      }
      assert.equal(JSON.stringify(Array.from(records.values())),before);
      assert.deepEqual(mathRoots,[dom$readingBody]);
    """, _PHONE_CALLBACKS, _PHONE_PEERS)
