"""The explorer shell's start: the page's boot script (``dag_viewer.BOOT_SCRIPT``), which must read an address as the
router does; which pages start a client; the cold mount and its failure; the map's notice; resize batching; and the
shipped bundle on the bundled example."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

from autoform_cli.ui import site_index
from autoform_cli.ui.dag_viewer import BOOT_SCRIPT
from tests.dom_fixture import run_dom
from tests.explorer_fixture import build_site, run_book, run_router

_EXAMPLE = Path(__file__).resolve().parents[2] / "skills/setup/assets/cabannes-thesis-project"


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The bundled example, built as it is published."""
    project = tmp_path_factory.mktemp("example") / "project"
    shutil.copytree(_EXAMPLE, project, ignore=shutil.ignore_patterns(".lake", "site", "site-src"))
    return build_site(project)


#: A tiny page the published boot script runs in: *stored* is the book's saved panes, *hash* the address's fragment,
#: *overview* whether render marked the reading, whose topic is *owner* and whose frame has the reader's handle and a
#: hidden heading for a screen reader, and *state* the page's history entry.
_BOOT_PAGE = r"""
      const href='https://example.test/atlas/topic/index.html';
      function page(stored,{pathname='/atlas/',wide=true,phone=false,frame=true,media=true,styled=true,hash='',overview=false,
          owner='topic',state=null,statements=null}={}){
        const log=[],listeners=[],keys=[],changes=[],queries={'(min-width: 1101px)':wide,'(max-width: 720px)':phone};
        const classes=()=>{const set=new Set();return {set,add(name){set.add(name);},remove(name){set.delete(name);},
          toggle(name,on){if(on)set.add(name);else set.delete(name);}};};
        const tabs={topics:{hidden:true},reading:{hidden:true}},body={children:[],appendChild(child){this.children.push(child);}};
        const handle=overview?{textContent:'Topic'}:null,heading=overview?{hidden:true,textContent:'Topic'}:null;
        // statements: [crumb labels, [anchor, label] per card], as render_boot writes them in a template.
        const item=(label,anchor=null)=>({getAttribute:name=>name==='data-anchor'?anchor:null,cloneNode:deep=>{
          assert.equal(deep,true);return label;}});
        const template=statements&&{content:{querySelectorAll(selector){
          if(selector==='li[data-anchor]')return statements[1].map(([anchor,label])=>item(label,anchor));
          assert.equal(selector,'li:not([data-anchor])');return statements[0].map(label=>item(label));}}};
        const trail={items:['Home','Topic'],replaceChildren(...items){this.items=items;}};
        const f=frame?{isConnected:true,style:{props:{},setProperty(name,value){this.props[name]=value;}},classList:classes(),
          attrs:{},setAttribute(name,value){this.attrs[name]=value;},
          querySelector(selector){return {link:{sheet:{cssRules:styled?[{}]:[]}},'.bp-reading-body':body,'.bp-dag-pane-tab-topics':tabs.topics,
            '.bp-dag-pane-tab-reading':tabs.reading,'.bp-dag-sheet-handle':handle,'h1.bp-dag-sr-only':heading,
            'template[data-boot-statements]':template,'.bp-dag-breadcrumb ol':trail}[selector]||null;},
          remove(){this.isConnected=false;log.push('frame removed');}}:null;
        // The parser is still inside the reading as its first child runs, so nothing follows the reading yet.
        const home={prepend(node){log.push({prepended:node});}};
        const reading={parentNode:home,nextSibling:null,hasAttribute:name=>overview&&name==='data-reader-overview',
          getAttribute:name=>name==='data-autoform-node-id'?owner:null};
        const script={parentNode:reading,remove(){log.push('script removed');}};
        const document={currentScript:script,body:{classList:classes()},
          querySelector(selector){assert.equal(selector,'.bp-book-frame');return f;}};
        const matchMedia=media?query=>{assert(query in queries,'Unknown query '+query);
          return {matches:queries[query],addEventListener(type,callback){changes.push({query,type,callback});}};}:undefined;
        const window={matchMedia,__md_scope:pathname===null?undefined:{pathname}};
        const localStorage={getItem(key){keys.push(key);if(stored instanceof Error)throw stored;return stored;}};
        new Function('document','window','localStorage','matchMedia','addEventListener','location','history',fixture.boot)(
          document,window,localStorage,matchMedia,(type,callback,capture)=>listeners.push({type,callback,capture}),
          {hash:hash?'#'+hash:'',href:href+(hash?'#'+hash:'')},{state});
        return {log,listeners,changes,keys,f,tabs,handle,heading,body,reading,window,trail,bodyClasses:document.body.classList.set};
      }
"""


def _boot(body: str, fixture: Mapping[str, object]) -> None:
    """Run *body* with :data:`_BOOT_PAGE` and *fixture*; the page is the tiny one, not jsdom's."""
    boot = BOOT_SCRIPT.removeprefix("<script>").removesuffix("</script>")
    run_dom("const assert=require('assert'),fixture=args;\n" + _BOOT_PAGE + body, args={"boot": boot, **fixture})


def test_the_boot_script_lays_the_saved_panes_out_and_gives_the_page_back() -> None:
    """Execute the published boot script over a tiny page: the reading moves into the frame, laid out with the panes the
    book saved for the site, and goes back where it was when the explorer cannot start."""
    _boot(r"""
      // The site's saved panes lay the frame out as the book will: the widths, then a shut Contents pane and its tab.
      let at=page(JSON.stringify({topics:300,reading:0.4,topicsHidden:true}));
      assert.deepEqual(at.keys,['autoform:book-panes:v1:/atlas/']);assert.equal(at.log[0],'script removed');
      assert.deepEqual(at.f.style.props,{'--dag-navigation-width':'300px','--dag-reading-width':'minmax(300px, 40.00%)'});
      assert.deepEqual([...at.f.classList.set],['bp-dag-topics-hidden']);
      assert.equal(at.tabs.topics.hidden,false);assert.equal(at.tabs.reading.hidden,true);
      assert.equal(at.body.children.length,1);assert.strictEqual(at.body.children[0],at.reading);assert(at.bodyClasses.has('bp-book'));
      assert.deepEqual(Object.keys(at.window.AutoformBookBoot),['release']);
      assert.deepEqual(at.listeners.map(listener=>[listener.type,listener.capture]),[['error',true]]);
      // Widths out of the book's bounds are dropped. Between the layouts Contents is a drawer, which nothing saved shuts;
      // a browser without media queries has the wide layout.
      at=page(JSON.stringify({topics:900,reading:1.5,topicsHidden:true,readingHidden:true}),{wide:false});
      assert.deepEqual(at.f.style.props,{});assert.deepEqual([...at.f.classList.set],['bp-dag-reading-hidden']);
      assert.equal(at.tabs.topics.hidden,true);assert.equal(at.tabs.reading.hidden,false);
      at=page(JSON.stringify({topics:120,reading:0}));assert.deepEqual(at.f.style.props,{});
      at=page(JSON.stringify({topicsHidden:true}),{media:false});assert.deepEqual([...at.f.classList.set],['bp-dag-topics-hidden']);
      // A map the reader shut is shut in the frame too, in either wider layout, but not beside a shut reader, which the
      // book never keeps, and only a stored true shuts it.
      for(const options of [{},{wide:false}]){
        at=page(JSON.stringify({mapHidden:true}),options);assert.deepEqual([...at.f.classList.set],['bp-dag-map-hidden']);
      }
      for(const [stored,options] of [[{mapHidden:true,readingHidden:true},{}],[{mapHidden:'yes'},{}]]){
        at=page(JSON.stringify(stored),options);assert(!at.f.classList.set.has('bp-dag-map-hidden'),JSON.stringify(stored));
      }
      // Anything else saved, or storage the browser refuses, leaves the frame as rendered; the reading moves all the same.
      for(const stored of [null,'[1]','"wide"','not json',new Error('refused')]){
        at=page(stored,{pathname:null});assert.deepEqual(at.keys,['autoform:book-panes:v1']);
        assert.deepEqual(at.f.style.props,{});assert.deepEqual([...at.f.classList.set],[]);
        assert.strictEqual(at.body.children[0],at.reading);assert(at.bodyClasses.has('bp-book'));
      }
      // Given back, the reading is first in its container again, as it was, and the page is Material's; a second release
      // changes nothing.
      at=page(null);at.log.length=0;at.window.AutoformBookBoot.release();
      assert.strictEqual(at.log[0].prepended,at.reading);assert.deepEqual(at.log.slice(1),['frame removed']);
      assert.deepEqual([...at.bodyClasses],[]);
      at.window.AutoformBookBoot.release();assert.equal(at.log.length,2);
      // So is a page turned to a phone's width while it loads; turned back, or once given back, nothing more happens.
      at=page(null,{wide:false});assert.deepEqual(at.changes.map(change=>[change.query,change.type]),[['(max-width: 720px)','change']]);
      at.log.length=0;at.changes[0].callback({matches:false});assert(at.f.isConnected);assert.deepEqual(at.log,[]);
      at.changes[0].callback({matches:true});assert.strictEqual(at.log[0].prepended,at.reading);
      assert.deepEqual(at.log.slice(1),['frame removed']);assert.deepEqual([...at.bodyClasses],[]);
      at.changes[0].callback({matches:true});assert.equal(at.log.length,2);
      // Only the explorer's bundle failing to load, or failing as it runs, gives the page back.
      const site='https://example.test/atlas/javascripts/',failures=[
        {target:{tagName:'IMG',src:site+'blueprint-dag.js'}},{target:{tagName:'SCRIPT',src:site+'project-map.js'}},{target:null},
        {target:{},filename:site+'project-map.js'},{target:{tagName:'SCRIPT',src:site+'blueprint-dag.js'}},
        {target:{tagName:'SCRIPT',src:site+'blueprint-dag.js?v=2'}},{target:{},filename:site+'blueprint-dag.js'}];
      assert.deepEqual(failures.map(error=>{const at=page(null);at.listeners[0].callback(error);return at.f.isConnected;}),
        [true,true,true,true,false,false,false]);
      // A page without the frame keeps its reading where it is. A frame whose stylesheet did not load is dropped, and so
      // is the frame on a phone, whose reader is a sheet the frame cannot open: Material's page keeps the article.
      for(const options of [{frame:false},{styled:false},{phone:true,wide:false}]){
        at=page(JSON.stringify({topicsHidden:true}),options);
        assert.deepEqual(at.log,options.frame===false?['script removed']:['script removed','frame removed']);
        assert.equal(at.window.AutoformBookBoot,undefined);assert.equal(at.body.children.length,0);
        assert.equal(at.bodyClasses.size,0);assert.equal(at.listeners.length,0);assert.equal(at.changes.length,0);
        assert.deepEqual(at.keys,[]);
      }
    """, {})


#: The router on the overview of a topic, 'topic', with an article 'a' on another page: how its cold start reads each
#: fragment the boot script reads.
_TOPIC = r"""
const topicSpec={title:'Topic',route:absolute('roadmap/topic/index.html'),data:'../../dependencies/topic.json',scope:'topic',
  catalog:'../../dependencies/index.json'};
const topicIndex=index();
topicIndex.nodes.push({id:'topic',title:'Topic',kind:'scope',result:false,route:'../roadmap/topic/index.html',data_url:'topic.json',scope:'topic'});
"""


def test_the_boot_script_folds_an_overviews_reader_where_the_books_first_commit_folds_it() -> None:
    """A page render marked an overview first paints with its reader folded where the book's first commit folds it:
    where the router's cold start reads the address as no section, the book's legacy filters included, and stays on the
    page's own topic, which a legacy node naming it moves to the topic's own address. A section or another article keeps
    the reader open, and a reader the history entry saved open is open with the book's ×. A map the reader shut stays
    shut in the frame's classes, where the book's rules ignore it beside a folded reader."""
    statuses = [state.key for state in site_index._ALL_STATES]
    hashes = ["", "view=list&status=fully_proved,inventory_pending", "node=topic", "node=scope:topic", "node=topic&view=list",
              "node=topic&status=any", "node=a", "node=a&view=list", "node=topical", "node", "parts", "&",
              "view=list&status=fully_proved", "isolates=0", "view=graph&direction=both&isolates=1",
              "direction=prerequisites&status=defined,conditional,inventory_checked", "status=-", "node=a&view=list&direction=dependents",
              "retained-section", "view=authored", "status=an_authored_heading", "status=fully_proved,", "view=list&heading=statement",
              "view=list&view=graph", "isolates=maybe", "direction=sideways", "status=mathlib,can_prove,can_state,not_ready,planned",
              "status=proved,stated", "view=list&", "=list", "view", "%76iew=list", "view=LIST", "node=boundary:topic", "node=%74opic",
              "node=", "node=&view=list", *("status=" + key for key in statuses), "status=" + ",".join(statuses)]
    # The router's reading of each fragment: whether its first commit names no section, and no article but the page's
    # own topic, nor asks for one to be read.
    readings = json.loads(run_router(_TOPIC + r"""
      const readings={};
      for(const hash of args.hashes){
        const app=setup({initial:topicSpec,url:topicSpec.route+'#'+hash});
        app.contents.set(absolute('dependencies/index.json'),topicIndex);app.contents.set(absolute('dependencies/topic.json'),graph);
        await app.nav.start();assert.equal(app.commits.length,1,hash);const [bundle]=app.commits;
        readings[hash]=bundle.anchor===null&&!bundle.focus&&[null,'topic'].includes(bundle.nodeId);app.nav.dispose();
      }
      return readings;
    """, args={"hashes": hashes}))
    _boot(r"""
      const folded=(hash,options={})=>{
        const at=page(null,{hash,overview:true,...options}),classes=at.f.classList.set;
        const fold=classes.has('bp-dag-overview-only')&&!classes.has('bp-dag-sheet-open');
        // The page's heading names it to a screen reader exactly while the reader is folded, as the book's does.
        if(at.heading)assert.equal(at.heading.hidden,!fold,'heading at #'+hash);
        return fold;
      };
      assert.equal(folded(''),true);assert.equal(folded('',{overview:false}),false);
      assert.equal(folded('view=list&status=fully_proved,inventory_pending'),true);
      for(const hash of ['node=topic','node=scope:topic','node=topic&view=list','node=topic&status=any'])
        assert.equal(folded(hash),true,hash);
      for(const hash of ['node=a','node=a&view=list','node=topical','node=','node','node=&view=list','parts',
          'view=list&view=graph','status=fully_proved,','&'])
        assert.equal(folded(hash),false,hash);
      // Exactly where the router reads neither a section nor another article.
      for(const [hash,reading] of Object.entries(fixture.readings)){
        assert.equal(folded(hash),reading,hash);assert.equal(folded(hash,{overview:false}),false,hash);
      }
      // The boot and the router each keep a list of the statuses; every status filters for both, alone or together.
      for(const hash of [...fixture.statuses.map(key=>'status='+key),'status='+fixture.statuses.join(',')]){
        assert(fixture.readings[hash],hash);assert.equal(folded(hash),true,hash);
      }
      // The book reopens a reader the page's history entry saved open, and only that entry's.
      const entry=(snapshot,changes={})=>({autoformNavigation:{version:1,url:href,key:'k',snapshot,...changes},other:1});
      for(const [state,expected] of [[entry({readingOpen:true}),false],[entry({}),false],[entry({readingOpen:false}),true],
          [entry(null),true],[entry({readingOpen:true},{url:href+'#parts'}),true],[entry({readingOpen:true},{version:2}),true],
          [{other:1},true],[null,true]])
        assert.equal(folded('',{state}),expected,JSON.stringify(state));
      assert.equal(folded('view=list',{state:entry({readingOpen:true},{url:href+'#view=list'})}),false);
      // Reopened over the overview, the reader has the book's × in place of the handle; elsewhere nothing changes.
      for(const state of [entry({readingOpen:true}),entry({})]){
        const at=page(null,{overview:true,state});
        assert.deepEqual([...at.f.classList.set].sort(),['bp-dag-overview-only','bp-dag-sheet-open']);
        assert.equal(at.handle.textContent,'\u00d7');
      }
      assert.equal(page(null,{overview:true,state:entry({readingOpen:false})}).handle.textContent,'Topic');
      assert.equal(page(null,{overview:true,hash:'parts',state:entry({readingOpen:true},{url:href+'#parts'})}).f.classList.set.size,0);
      assert.equal(page(null,{state:entry({readingOpen:true})}).f.classList.set.size,0);
      const shut=page(JSON.stringify({mapHidden:true}),{overview:true});
      assert.deepEqual([...shut.f.classList.set].sort(),['bp-dag-map-hidden','bp-dag-overview-only']);
      // No frame, no fold: a phone and a page whose frame did not style keep Material's page.
      for(const options of [{phone:true,wide:false},{styled:false}]){
        assert.equal(page(null,{...options,overview:true}).f.classList.set.size,0);
      }
    """, {"readings": readings, "statuses": statuses})


#: Card anchors as render writes them, and fragments as an address carries them with the anchor each names, if any.
_STATEMENT_ANCHORS = ("main", "caf\u00e9", "a b", "100%")
_STATEMENT_ADDRESSES = (
    ("main", "main"), ("caf\u00e9", "caf\u00e9"), ("caf%C3%A9", "caf\u00e9"), ("a b", "a b"), ("a%20b", "a b"),
    ("100%", "100%"), ("100%25", None), ("ma%69n", None), ("caf%c3%a9", None), ("proof", None),
)


def test_the_boot_script_puts_in_the_trail_of_the_statement_the_router_names() -> None:
    """On an address naming a statement card, the boot script swaps in the trail render wrote for it and marks the frame
    for its topic line, and it names the card exactly where the router's route lookup does. The two can't share code,
    so both read the same addresses: an anchor typed raw or percent-encoded as the URL parser writes it names its card,
    and a percent-encoded ASCII letter, lowercase hex or a section names none, so the frame keeps its trail and the
    router selects the page itself."""
    fixture = {"anchors": _STATEMENT_ANCHORS, "addresses": _STATEMENT_ADDRESSES}
    _boot(r"""
      const statements=[['Home','Topic, linked'],fixture.anchors.map(anchor=>[anchor,'Card '+anchor])];
      for(const [typed,expected] of fixture.addresses){
        // The browser holds the address as its URL parser normalises it.
        const at=page(null,{hash:new URL('#'+typed,href).hash.slice(1),statements});
        assert.deepEqual(at.trail.items,expected===null?['Home','Topic']:['Home','Topic, linked','Card '+expected],typed);
        assert.equal(at.f.attrs['data-boot-card'],expected===null?undefined:'',typed);
      }
      // A page without statement cards, or a frame the script drops, keeps the trail render wrote, unmarked.
      const plain=page(null,{hash:'main'}),dropped=page(null,{hash:'main',statements,styled:false});
      assert.deepEqual(plain.trail.items,['Home','Topic']);assert.deepEqual(plain.f.attrs,{});
      assert.deepEqual(dropped.trail.items,['Home','Topic']);assert.deepEqual(dropped.f.attrs,{});
    """, fixture)
    run_router(r"""
      const spec={title:'Cards',route:absolute('roadmap/cards.html'),data:'../dependencies/a.json',scope:'cards'};
      const catalog=index();
      catalog.nodes.push({id:'cards',title:'Cards',kind:'scope',result:false,route:'../roadmap/cards.html',data_url:'a.json',scope:'cards'});
      args.anchors.forEach((anchor,n)=>catalog.nodes.push({id:'card-'+n,title:'Card '+n,kind:'node',result:true,
        url:'scopes/a.html#node=card-'+n,route:'../roadmap/cards.html#'+anchor,data_url:'a.json',scope:'cards'}));
      for(const [typed,expected] of args.addresses){
        const app=setup({initial:spec,url:new URL('roadmap/cards.html#'+typed,base).href});
        app.contents.set(absolute('dependencies/index.json'),catalog);app.contents.set(spec.route,spec);
        await app.nav.start();
        assert.equal(app.commits.length,1,typed);
        assert.equal(app.commits[0].nodeId,expected===null?'cards':'card-'+args.anchors.indexOf(expected),typed);
        app.nav.dispose();
      }
    """, args=fixture)


#: The page's boot script, which jsdom does not run, run as a browser runs it while the reading's first child parses;
#: the frame's stylesheet counts as loaded.
_FRAME = r"""
function bootPage() {
  const frame = document.querySelector('.bp-book-frame');
  Object.defineProperty(frame.querySelector('link'), 'sheet', {value: {cssRules: [{}]}, configurable: true});
  const script = Array.from(document.scripts).find(node => node.textContent.includes('AutoformBookBoot'));
  Object.defineProperty(document, 'currentScript', {value: script, configurable: true});
  try { window.eval(script.textContent); } finally { delete document.currentScript; }
}
bootPage();
"""
#: What a page that does not start a client keeps: its static host, no request, and the frame's reading given back.
_STATIC = r"""
await waitFor(() => document.readyState !== 'loading'); await tick(500);
const viewer = document.querySelector('.bp-dag-viewer[data-graph-src]');
assert(!viewer.hasAttribute('data-dag-ready')); assert(!viewer.classList.contains('bp-dag-enhancing'));
assert.deepEqual(net.log, []); assert.equal($('.bp-book-frame'), null); assert($('.bp-explorer-page > .bp-reading-source'));
assert(!document.body.classList.contains('bp-book')); assert(!document.body.classList.contains('bp-dag-app-page'));
"""


@pytest.mark.parametrize("host", ["outside an explorer page", "without the router"])
def test_bare_or_navigationless_hosts_do_not_start_an_interactive_client(site: Path, host: str) -> None:
    before = {
        "outside an explorer page": "document.body.append(document.querySelector('.bp-dag-viewer[data-graph-src]'));",
        # The bundle's router never takes its place.
        "without the router": "Object.defineProperty(window, 'AutoformNavigation', {get() {}, set() {}, configurable: true});",
    }[host]
    run_book(site, "roadmap/infimum-loss/", _STATIC, before=_FRAME + before, ready=False)


def test_cold_mount_takes_the_full_page_and_error_restores_native_reading(site: Path) -> None:
    # The book opens the first paint's reading where the reader had scrolled it, then removes the frame it was in.
    scrolled = _FRAME + "document.querySelector('.bp-book-frame .bp-reading-body').scrollTop = 240;"
    run_book(site, "roadmap/infimum-loss/", r"""
      assert(document.body.classList.contains('bp-dag-app-page')); assert.equal($('.bp-book-frame'), null);
      const reading = host().querySelector('.bp-reading-body');
      assert.equal(reading.querySelector('.bp-reading-source').dataset.autoformNodeId, 'infimum-loss');
      assert.equal(reading.scrollTop, 240);
    """, before=scrolled)
    # A first commit of another page, as a legacy address naming another topic asks, opens that page at its top.
    run_book(site, "roadmap/infimum-loss/", r"""
      assert.equal($('.bp-book-frame'), null); const reading = host().querySelector('.bp-reading-body');
      assert.equal(reading.querySelector('.bp-reading-source').dataset.autoformNodeId, 'full-supervision');
      assert.equal(reading.scrollTop, 0);
    """, hash="#node=full-supervision", before=scrolled)
    # A first page the book cannot read leaves Material's page, with the reason and a way to try again.
    run_book(site, "roadmap/infimum-loss/", r"""
      await waitFor(() => document.readyState !== 'loading');
      const viewer = document.querySelector('.bp-dag-viewer[data-graph-src]'), notice = viewer.querySelector('.bp-dag-loading');
      await waitFor(() => notice.querySelector('button'), 'the book never gave up');
      assert(!notice.hidden); assert(notice.textContent.includes('The complete mathematical page remains readable.'));
      assert(!viewer.classList.contains('bp-dag-enhancing')); assert(!document.body.classList.contains('bp-dag-app-page'));
      assert.equal($('.bp-book-frame'), null); assert($('.bp-explorer-page > .bp-reading-source'));
      // Try again reloads the page, a navigation jsdom reports it cannot make.
      const retry = notice.querySelector('button'); assert.equal(retry.textContent, 'Try again');
      click(retry); await tick(0); assert.equal(errors.length, 1); assert.match(errors[0], /Not implemented: navigation/);
      errors.length = 0;
    """, before=_FRAME + "net.fail('dependencies/chapters/infimum-loss.json');", ready=False)
    # So does a mount that fails once the book has taken the full page.
    run_book(site, "roadmap/infimum-loss/", r"""
      await waitFor(() => document.readyState !== 'loading');
      const viewer = document.querySelector('.bp-dag-viewer[data-graph-src]');
      await waitFor(() => viewer.classList.contains('bp-dag-integrated') && !$('.bp-book-frame'), 'the book never mounted');
      assert(!document.body.classList.contains('bp-dag-app-page')); assert(!document.body.classList.contains('bp-book'));
      assert(!viewer.classList.contains('bp-dag-enhancing')); assert($('.bp-explorer-page > .bp-reading-source'));
    """, before=_FRAME + "window.AutoformMath = {refresh() { throw new Error('controlled mount failure'); }};", ready=False)


@pytest.mark.parametrize(("page", "graph"), [
    # The roadmap's own graph has no can_state statement, and its trail ends on its name, unlinked.
    ("roadmap/", [False, False]),
    # A chapter's has one, and its trail links the chapter's own crumb to the chapter.
    ("roadmap/infimum-loss/", [True, True]),
], ids=["roadmap", "chapter"])
def test_an_index_that_arrives_after_the_first_commit_renders_the_outline(
    site: Path, page: str, graph: list[bool],
) -> None:
    run_book(site, page, r"""
      // The first request for the index failed, so the book started without it: Contents lists no topic, and the
      // reading's child indexes show. The book asks again, and the index that comes renders the outline.
      const outline = () => $$('.bp-dag-navigation a').map(link => link.textContent.trim());
      // Until then the status filter and the trail are the page's graph's. The index projects the page again: the
      // filter offers every status the index holds, and the trail's last crumb, the page itself, links nowhere.
      const offered = () => $$('.bp-dag-filter-popover input').some(input => input.value === 'can_state');
      const linked = () => Boolean($$('.bp-dag-breadcrumb li').at(-1).querySelector('a'));
      assert.deepEqual(outline(), []); assert(host().classList.contains('bp-dag-index-unavailable'));
      assert.deepEqual([offered(), linked()], args.graph);
      net.release('dependencies/index.json'); await tick(100);
      assert.deepEqual(outline(), ['Full Supervision', 'Infimum Loss milestone']);
      assert(!host().classList.contains('bp-dag-index-unavailable'));
      assert.deepEqual([offered(), linked()], [true, false]);
    """, before="net.fail('dependencies/index.json'); net.hold('dependencies/index.json');", args={"graph": graph})


@pytest.mark.parametrize(("page", "reader"), [("roadmap/", False), ("roadmap/infimum-loss/", False),
                                              ("roadmap/infimum-loss/#theorems-non-ambiguity-determinism", True)])
def test_a_phones_first_route_already_reads_the_index(site: Path, page: str, reader: bool) -> None:
    # The router validated the index before the first commit, and the first route projects with it: on a phone, Home
    # and a chapter open on the map, and a statement's address opens its card in the reader.
    path, _, anchor = page.partition("#")
    run_book(site, path, r"""
      assert.equal(host().classList.contains('bp-dag-sheet-open'), args.reader);
      assert.equal(Boolean($('.bp-reading-current')), args.reader);
    """, hash="#" + anchor if anchor else "", media={"(max-width: 720px)": True}, args={"reader": reader})


#: Map scripts that load but draw nothing, or make their host in the book's stage and stop before marking it set up.
_MAPS = {
    "draws nothing": "",
    "stops before set-up": "var map = document.createElement('div'); map.className = 'bp-whole-map';"
                           " document.querySelector('[data-whole-map] .bp-dag-stage').appendChild(map);",
}


@pytest.fixture(scope="module")
def map_sites(site: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    sites = {}
    for name, script in _MAPS.items():
        sites[name] = shutil.copytree(site, tmp_path_factory.mktemp("map") / "site")
        (sites[name] / "javascripts/project-map.js").write_text(script, encoding="utf-8")
    return sites


@pytest.mark.parametrize("outcome", ["fails", "draws nothing", "stops before set-up", "draws"])
def test_a_map_script_that_fails_or_draws_nothing_leaves_one_notice_in_the_stage(
    site: Path, map_sites: dict[str, Path], outcome: str
) -> None:
    run_book(map_sites.get(outcome, site), "roadmap/infimum-loss/", r"""
      const stage = host().querySelector('.bp-dag-stage'), notes = name => stage.querySelectorAll('.bp-dag-map-' + name);
      const script = Array.from(document.head.querySelectorAll('script')).find(node => /project-map\.js$/.test(node.src));
      assert.equal(script.src, root + 'javascripts/project-map.js');
      if (args.outcome === 'draws') {
        // Once its stylesheet loads, the map reads its files through the book's router; it says when it shows, which
        // ends the loading note.
        emit(Array.from(document.head.querySelectorAll('link')).find(node => /project-map\.css$/.test(node.href)), 'load');
        await tick(50); assert(mapReads.length > 0); assert(mapReads.every(init => init.credentials === 'same-origin' && init.signal));
        assert.equal(notes('unavailable').length, 0); const [loading] = notes('loading');
        assert.equal(loading.textContent, 'Loading the map…'); assert.equal(loading.getAttribute('role'), 'status');
        stage.querySelector('.bp-whole-map').dispatchEvent(new window.CustomEvent('autoform-map-shown', {bubbles: true}));
        assert.equal(notes('loading').length, 0);
      } else {
        // Its notice takes the loading note's place, and the book's live region says it, since a status inserted with
        // its text is often not spoken; another report of the script keeps the one notice.
        assert.equal(notes('loading').length, 0); assert.equal(notes('unavailable').length, 1);
        const [notice] = notes('unavailable'); assert.equal(notice.tagName, 'P'); assert.equal(notice.getAttribute('role'), 'status');
        assert.equal(notice.textContent, 'The map could not load. Contents, search and the statement lists still work.');
        assert.equal(host().querySelector('[aria-live]').textContent, notice.textContent);
        emit(script, 'error'); assert.equal(notes('unavailable').length, 1);
      }
    """, map="fail" if outcome == "fails" else "load", args={"outcome": outcome},
             before="var mapReads = []; net.route = (href, init) => { if (href.includes('/map/data/')) mapReads.push(init); };")


#: A ResizeObserver the test drives, as the browser drives one: notify(target) reports to each observer observing it.
#: Material's instant navigation is document$, whose subscribers boot the page again once its content changed.
_RESIZE = r"""
var observers = [], navigated = () => {};
window.ResizeObserver = class {
  constructor(callback) { this.callback = callback; this.targets = new Set(); observers.push(this); }
  observe(target) { this.targets.add(target); } unobserve(target) { this.targets.delete(target); } disconnect() { this.targets.clear(); }
};
window.document$ = {subscribe(callback) { navigated = callback; }};
function notify(target) { observers.filter(observer => observer.targets.has(target)).forEach(observer => observer.callback([{target}], observer)); }
"""
#: A statement's page, whose trail of four folds its middle once it overflows: the trail's width is *width*, its
#: content's 500 px. *frames* counts the frames the page asks for and *painted* those that came.
_TRAIL = r"""
const crumb = host().querySelector('.bp-dag-breadcrumb'), folded = () => crumb.textContent.includes('…');
let width = 800, frames = 0, painted = 0;
Object.defineProperty(crumb, 'clientWidth', {get: () => width, configurable: true});
Object.defineProperty(crumb, 'scrollWidth', {get: () => 500, configurable: true});
const paint = window.requestAnimationFrame;
window.requestAnimationFrame = callback => { frames++; return paint(time => { painted++; callback(time); }); };
assert.equal(crumb.querySelectorAll('li').length, 4); assert(!folded());
"""


def test_without_a_resize_observer_a_window_resize_refolds_the_trail_at_once(site: Path) -> None:
    run_book(site, "roadmap/infimum-loss/", _TRAIL + r"""
      width = 300; emit(window, 'resize'); assert(folded()); assert.equal(frames, 0);
      width = 800; emit(window, 'resize'); assert(!folded());
    """, hash="#theorems-non-ambiguity-determinism", before="window.ResizeObserver = undefined;")


def test_resize_notifications_refold_once_a_frame_and_none_after_the_host_is_gone(site: Path) -> None:
    run_book(site, "roadmap/infimum-loss/", _TRAIL + r"""
      const stage = host().querySelector('.bp-dag-stage');
      // A burst of reports asks for one frame, and the trail refolds when it comes, out of the observers' delivery.
      width = 300; notify(crumb); notify(stage); notify(crumb); assert.equal(frames, 1); assert(!folded());
      await tick(16); assert(folded()); await tick(100); assert.equal(frames, 1, 'a settled size asks for no more frames');
      // The stage's own reports refold it too.
      width = 800; notify(stage); await tick(16); assert(!folded()); assert.equal(frames, 2);
      // Once Material has replaced the page's content, the pending frame never comes and nothing is observed.
      width = 300; notify(crumb); assert.equal(frames, 3); const seen = painted;
      host().remove(); navigated(); await tick(100);
      assert.equal(painted, seen); assert(!folded()); assert(observers.every(observer => observer.targets.size === 0));
      // Nor does its router answer history any more.
      const requests = net.log.length; emit(window, 'popstate'); await tick(100); assert.equal(net.log.length, requests);
    """, hash="#theorems-non-ambiguity-determinism", before=_RESIZE)


def test_back_between_the_page_swap_and_materials_notice_leaves_the_title_and_the_old_reading(site: Path) -> None:
    run_book(site, "roadmap/", r"""
      assert(click($('.bp-dag-navigation-link[data-navigation-node-id="infimum-loss"]')));
      await waitFor(() => document.title === 'Infimum Loss milestone', 'the chapter never opened'); await tick(200);
      // Material's instant navigation swaps the page's content out, then tells its subscribers. Back in between still
      // reaches the book's router and the browser goes back, but the page it brings is for neither the document nor
      // the book taken out of it: the title and the old reading stay as they were.
      const reading = host().querySelector('.bp-reading-body'), kept = reading.innerHTML;
      $('.bp-explorer-page').remove(); await back();
      assert.equal(location.href, root + 'roadmap/index.html');
      assert.equal(document.title, 'Infimum Loss milestone'); assert.equal(reading.innerHTML, kept);
    """)


def test_the_shipped_bundle_opens_a_searched_statement_on_the_bundled_example_and_back_returns(site: Path) -> None:
    run_book(site, "roadmap/", r"""
      // The explorer has the page, Contents lists the chapters, and the book has asked for the optional wiki layer the
      // index's digest verifies.
      assert(document.body.classList.contains('bp-dag-app-page')); assert.equal(document.title, 'Thesis roadmap');
      assert.deepEqual($$('.bp-dag-navigation a').map(link => link.textContent.trim()), ['Full Supervision', 'Infimum Loss milestone']);
      assert(net.log.includes('dependencies/wiki-links.json'));
      // A Lean name finds its statement, which opens in the reader, at its own address.
      const search = $('.bp-dag-search'); search.focus(); search.value = 'nonAmbiguous_determinism'; emit(search, 'input');
      await tick(100); const results = $$('.bp-dag-search-results button');
      assert.equal(results.length, 1); assert(results[0].textContent.startsWith('Non-ambiguity determinism'));
      click(results[0]); await waitFor(() => $('.bp-reading-current'), 'the statement never opened');
      assert.equal(location.href, root + 'roadmap/infimum-loss/index.html#theorems-non-ambiguity-determinism');
      assert.equal(document.title, 'Non-ambiguity determinism');
      assert($('.bp-reading-current').textContent.includes('CabannesThesis.nonAmbiguous_determinism'));
      // Back returns to the roadmap's page.
      await back(); assert.equal(location.href, root + 'roadmap/index.html'); assert.equal(document.title, 'Thesis roadmap');
      assert.equal($('.bp-reading-current'), null);
      assert.equal(host().querySelector('.bp-reading-body .bp-reading-source').dataset.autoformNodeId, 'roadmap');
    """)
