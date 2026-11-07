"""Production boot gating, the page's boot script, cold-mount order, the map's failure notice and resize batching of
the explorer shell, over tiny DOM and router seams.

These are source checks, not substitutes for genuine browser journeys.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoform_cli.ui.dag_viewer import BOOT_SCRIPT

_ROOT = Path(__file__).resolve().parents[2]
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is required for shell boot checks")

_LOADER = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(ROOT+'/autoform_cli/ui/assets/blueprint-dag.js','utf8');
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
const context={assert,URL,productionSource:source,original,fixture:JSON.parse(fs.readFileSync(0,'utf8'))};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  var LIST_PAGE ='),source.indexOf('  var clients =')),context);
"""


def _run(body: str, fixture: object) -> None:
    script = "const ROOT=" + json.dumps(str(_ROOT)) + ";\n" + _LOADER
    script += "vm.runInContext(" + json.dumps(body) + ",context,{timeout:2000});"
    result = subprocess.run([_NODE, "-e", script], input=json.dumps(fixture), text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


@pytest.mark.parametrize("entry", ["roadmap", "resultA"])
def test_cold_mount_adopts_the_loaded_index_and_only_its_route_renders(entry: str) -> None:
    """Execute the real cold-mount prefix and index adoption; the shell, the renderers and the route are seams."""
    _run(r"""
      const events=[];let loaded=true;
      const scopes=Array.from({length:12},(_,number)=>({id:'scope-'+number,title:'Topic '+number,
        scope:'scope-'+number,parent:'roadmap',kind:'scope',navigation:'module',status:'planned',area:number<6?'Algebra':'Topology'}));
      const results=['resultA','resultB'].map((id,number)=>({id,title:id,scope:'scope-'+number*6,parent:'scope-'+number*6,kind:'node',status:'planned',result:true}));
      const index={nodes:[{id:'roadmap',scope:'roadmap',parent:null,kind:'scope',title:'Mathematics',status:'planned'},...scopes,...results],
        relations:[['resultA','resultB',2,1]]};
      const payload={schema:'autoform-dag-view/v2',title:'Mathematics',palette:{planned:{label:'planned'}},
        nodes:scopes.map(node=>({...node,id:'scope:'+node.id,member_count:1,status_counts:[['planned',1]],
          layout:{width:232,height:116}})),edges:[{source:'scope:scope-0',target:'scope:scope-6',statement:2,proof:1}],
        view:{presentation:'atlas',breadcrumbs:[]},regions:[],present_statuses:['planned']};
      const firstBundle={nodeId:fixture.entry,scope:fixture.entry==='roadmap'?'roadmap':'scope-0',
        graph:payload,readingSource:{},dataURL:'https://example.test/dependencies.json'};
      const navigation={getIndex(){events.push('index');return loaded?index:null;}};
      const host={querySelector(){return null;},closest(){return null;},classList:{add(){},remove(){},toggle(){}}};
      const window={location:{href:'https://example.test/roadmap/index.html',origin:'https://example.test'}};
      const start=productionSource.indexOf('  function mount('),end=productionSource.indexOf('    function globalListen(',start);
      assert(start>=0&&end>start,'The real cold-mount boundary must exist');
      const helpers=['safeHref','graphAt','adoptIndex','useIndex','requestWikiLinks','useWikiLinks'].map(original).join('\n');
      const seams=`
        function buildShell() {events.push('shell');dom$search={removeAttribute(){}};}
        function bindEvents() {events.push('bind');}
        function bindLeanCards() {events.push('lean-cards');}
        function bindPanes() {events.push('panes');}
        function updateProjection() {events.push('project');}
        function renderNavigation() {events.push('outline');} function refresh() {events.push('refresh');}
        function resize() {events.push('resize');}
        function renderWikiContext() {events.push('optional-wiki');}
        function applyRoute(bundle) {
          assert.strictEqual(bundle,firstBundle);assert.equal(ui$requestedNode,fixture.entry);
          if(loaded) {
            // The route projects with the router's validated index, a direct theorem entry's record included.
            assert.strictEqual(sharedIndex,index,"The route must see the router's validated index");
            assert.strictEqual(records.get(fixture.entry),index.nodes.find(node=>node.id===fixture.entry));
            assert.strictEqual(ui$searchNodes,index.nodes);
            assert.equal(ui$wikiRequested,true);assert.equal(ui$wikiLinks.availability,'unavailable');
          } else assert.equal(sharedIndex,null);
          events.push('route');return Promise.resolve();
        }
      `;
      const bootstrap=eval('('+productionSource.slice(start,end)+'\nreturn {requestedNode:ui$requestedNode,useIndex};\n'+helpers+'\n'+seams+'})');
      assert.equal(bootstrap(host,payload,1,firstBundle,navigation).requestedNode,fixture.entry);
      // Adopting the index renders nothing and lays nothing out: only the route renders the first view.
      assert.deepEqual(events,['shell','bind','lean-cards','panes','index','optional-wiki','route']);
      // An index that arrives after the mount is adopted and rendered at once.
      events.length=0;loaded=false;const client=bootstrap(host,payload,1,firstBundle,navigation);
      assert.deepEqual(events,['shell','bind','lean-cards','panes','index','route']);
      events.length=0;client.useIndex(index);assert.deepEqual(events,['optional-wiki','project','outline','refresh']);
      events.length=0;client.useIndex(index);assert.deepEqual(events,[]);
      assert.throws(()=>bootstrap(host,payload,1,firstBundle,null),/complete mathematical page and navigation/);
      assert.throws(()=>bootstrap(host,payload,1,null,navigation),/complete mathematical page and navigation/);
      assert.throws(()=>bootstrap(host,payload,1,{graph:payload},navigation),/complete mathematical page and navigation/);
    """, {"entry": entry})


def test_bare_or_navigationless_hosts_do_not_start_an_interactive_client() -> None:
    """Execute production boot gating: unsupported hosts keep their static DOM and give the frame's reading back."""
    _run("""
      const effects=[],window={AutoformNavigation:{create(){effects.push('create');}},AutoformBookBoot:{release(){effects.push('release');}}};
      const host={getAttribute(){return null;},closest(){return null;},
        setAttribute(){effects.push('ready');},classList:{add(){effects.push('enhancing');}}};
      eval(original('releaseFrame'));eval(original('start'));start(host);
      assert.deepEqual(effects,['release'],'A bare graph host must neither fetch nor replace its fallback');
      window.AutoformNavigation=null;host.closest=()=>({});effects.length=0;start(host);
      assert.deepEqual(effects,['release'],'An unavailable router must preserve the complete static reading page');
    """, {})


def test_the_boot_script_lays_the_saved_panes_out_and_gives_the_page_back() -> None:
    """Execute the published boot script over a tiny page: the reading moves into the frame, laid out with the panes the
    book saved for the site, and goes back where it was when the explorer cannot start."""
    _run(r"""
      function page(stored,{pathname='/atlas/',wide=true,phone=false,frame=true,media=true,styled=true}={}){
        const log=[],listeners=[],keys=[],queries={'(min-width: 1101px)':wide,'(max-width: 720px)':phone};
        const classes=()=>{const set=new Set();return {set,add(name){set.add(name);},remove(name){set.delete(name);},
          toggle(name,on){if(on)set.add(name);else set.delete(name);}};};
        const tabs={topics:{hidden:true},reading:{hidden:true}},body={children:[],appendChild(child){this.children.push(child);}};
        const f=frame?{isConnected:true,style:{props:{},setProperty(name,value){this.props[name]=value;}},classList:classes(),
          querySelector(selector){return {link:{sheet:{cssRules:styled?[{}]:[]}},'.bp-reading-body':body,'.bp-dag-pane-tab-topics':tabs.topics,
            '.bp-dag-pane-tab-reading':tabs.reading}[selector]||null;},
          remove(){this.isConnected=false;log.push('frame removed');}}:null;
        // The parser is still inside the reading as its first child runs, so nothing follows the reading yet.
        const home={prepend(node){log.push({prepended:node});}};
        const reading={parentNode:home,nextSibling:null},script={parentNode:reading,remove(){log.push('script removed');}};
        const document={currentScript:script,body:{classList:classes()},
          querySelector(selector){assert.equal(selector,'.bp-book-frame');return f;}};
        const matchMedia=media?query=>{assert(query in queries,'Unknown query '+query);return {matches:queries[query]};}:undefined;
        const window={matchMedia,__md_scope:pathname===null?undefined:{pathname}};
        const localStorage={getItem(key){keys.push(key);if(stored instanceof Error)throw stored;return stored;}};
        new Function('document','window','localStorage','matchMedia','addEventListener',fixture.boot)(
          document,window,localStorage,matchMedia,(type,callback,capture)=>listeners.push({type,callback,capture}));
        return {log,listeners,keys,f,tabs,body,reading,window,bodyClasses:document.body.classList.set};
      }
      // The site's saved panes lay the frame out as the book will: the widths, then a shut Contents pane and its tab.
      let at=page(JSON.stringify({topics:300,reading:0.4,topicsHidden:true}));
      assert.deepEqual(at.keys,['autoform:book-panes:v1:/atlas/']);assert.equal(at.log[0],'script removed');
      assert.deepEqual(at.f.style.props,{'--dag-navigation-width':'300px','--dag-reading-width':'minmax(300px, 40.00%)'});
      assert.deepEqual([...at.f.classList.set],['bp-dag-topics-hidden']);
      assert.equal(at.tabs.topics.hidden,false);assert.equal(at.tabs.reading.hidden,true);
      assert.equal(at.body.children.length,1);assert.strictEqual(at.body.children[0],at.reading);assert(at.bodyClasses.has('bp-book'));
      assert.deepEqual(Object.keys(at.window.AutoformBookBoot),['release']);
      assert.deepEqual(at.listeners.map(listener=>[listener.type,listener.capture]),[['error',true]]);
      // Widths out of the book's bounds are dropped. Between the layouts Contents is a drawer, which nothing saved shuts,
      // and on a phone the reader is a sheet; a browser without media queries has the wide layout.
      at=page(JSON.stringify({topics:900,reading:1.5,topicsHidden:true,readingHidden:true}),{wide:false});
      assert.deepEqual(at.f.style.props,{});assert.deepEqual([...at.f.classList.set],['bp-dag-reading-hidden']);
      assert.equal(at.tabs.topics.hidden,true);assert.equal(at.tabs.reading.hidden,false);
      at=page(JSON.stringify({topics:120,reading:0}));assert.deepEqual(at.f.style.props,{});
      at=page(JSON.stringify({topicsHidden:true,readingHidden:true}),{wide:false,phone:true});
      assert.deepEqual([...at.f.classList.set],[]);assert.equal(at.tabs.topics.hidden,true);assert.equal(at.tabs.reading.hidden,true);
      at=page(JSON.stringify({topicsHidden:true}),{media:false});assert.deepEqual([...at.f.classList.set],['bp-dag-topics-hidden']);
      // A map the reader shut is shut in the frame too, in either wider layout. On a phone it is not, nor beside a shut
      // reader, which the book never keeps, and only a stored true shuts it.
      for(const options of [{},{wide:false}]){
        at=page(JSON.stringify({mapHidden:true}),options);assert.deepEqual([...at.f.classList.set],['bp-dag-map-hidden']);
      }
      for(const [stored,options] of [[{mapHidden:true,readingHidden:true},{}],[{mapHidden:true},{wide:false,phone:true}],[{mapHidden:'yes'},{}]]){
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
      // Only the explorer's bundle failing to load, or failing as it runs, gives the page back.
      const site='https://example.test/atlas/javascripts/',errors=[
        {target:{tagName:'IMG',src:site+'blueprint-dag.js'}},{target:{tagName:'SCRIPT',src:site+'project-map.js'}},{target:null},
        {target:{},filename:site+'project-map.js'},{target:{tagName:'SCRIPT',src:site+'blueprint-dag.js'}},
        {target:{tagName:'SCRIPT',src:site+'blueprint-dag.js?v=2'}},{target:{},filename:site+'blueprint-dag.js'}];
      assert.deepEqual(errors.map(error=>{const at=page(null);at.listeners[0].callback(error);return at.f.isConnected;}),
        [true,true,true,true,false,false,false]);
      // A page without the frame keeps its reading where it is, and a frame whose stylesheet did not load is dropped.
      for(const options of [{frame:false},{styled:false}]){
        at=page(JSON.stringify({topicsHidden:true}),options);
        assert.deepEqual(at.log,options.frame===false?['script removed']:['script removed','frame removed']);
        assert.equal(at.window.AutoformBookBoot,undefined);assert.equal(at.body.children.length,0);
        assert.equal(at.bodyClasses.size,0);assert.equal(at.listeners.length,0);assert.deepEqual(at.keys,[]);
      }
    """, {"boot": BOOT_SCRIPT.removeprefix("<script>").removesuffix("</script>")})


def test_cold_mount_takes_the_full_page_and_error_restores_native_reading() -> None:
    """Run real boot ordering over tiny DOM/router seams; browser containment remains independent."""
    _run("""
      const events=[],bodyClasses=new Set(),hostClasses=new Set(),loading={hidden:true,children:[],classList:{add(){}},
        appendChild(child){this.children.push(child);}};
      const classes=set=>({add(name){set.add(name);},remove(name){set.delete(name);},contains(name){return set.has(name);}});
      const document={body:{classList:classes(bodyClasses)},createElement(tag){return {tagName:tag.toUpperCase(),
        addEventListener(type,callback){this[type]=callback;}};}};
      const reading={},framed={scrollTop:240,contains(node){return node===reading;}};
      const frame={querySelector(selector){return selector==='.bp-reading-body'?framed:null;},remove(){events.push('frame removed');}};
      document.querySelector=selector=>selector==='.bp-book-frame'?frame:null;
      function host(){const attributes=new Map();return {isConnected:true,
        getAttribute(name){return attributes.get(name)||null;},setAttribute(name,value){attributes.set(name,value);},closest(){return {};},
        querySelector(){return loading;},classList:classes(hostClasses)};}
      const clients=new Map();let fail=false,options,reloads=0,committed=reading;
      const window={location:{reload(){reloads++;}},AutoformBookBoot:{release(){events.push('release');}},
        AutoformNavigation:{create(value){options=value;return {
        start(){try{options.onCommit({graph:{},readingSource:committed});}catch(error){options.onError(error);}},dispose(){}};}}};
      function mount(host,data,instance,bundle,navigation,frameScroll){
        assert(bodyClasses.has('bp-dag-app-page'),'The shell mounts into the full-page layout');
        events.push('mount at '+frameScroll);if(fail)throw Error('controlled mount failure');return {ready:Promise.resolve()};}
      eval(original('element'));eval(original('button'));eval(original('releaseFrame'));eval(original('start'));start(host());
      // The book opens the first paint's reading where the reader had scrolled it, then removes the frame it was in.
      assert.deepEqual(events,['mount at 240','frame removed']);assert(bodyClasses.has('bp-dag-app-page'));
      // A page the reader went on to before the first commit opens at its top.
      committed={};clients.clear();events.length=0;start(host());
      assert.deepEqual(events,['mount at 0','frame removed']);committed=reading;
      fail=true;clients.clear();bodyClasses.clear();events.length=0;start(host());
      assert.deepEqual(events,['mount at 240','release']);assert(!bodyClasses.has('bp-dag-app-page'));
      assert(!hostClasses.has('bp-dag-enhancing'));assert(loading.textContent.includes('complete mathematical page remains readable'));
      const [retry]=loading.children;assert.equal(retry.tagName,'BUTTON');assert.equal(retry.textContent,'Try again');
      retry.click();assert.equal(reloads,1);
    """, {})


@pytest.mark.parametrize("outcome", ["error", "load-without-map", "load-before-set-up", "load-with-map"])
def test_a_map_script_that_fails_or_draws_nothing_leaves_one_notice_in_the_stage(outcome: str) -> None:
    """Execute the real start() and notice; the mounted shell around them is a seam."""
    _run(r"""
      const scripts=[],listeners=[],stage={children:[],
        appendChild(child){this.children.push(child);child.remove=()=>this.children.splice(this.children.indexOf(child),1);return child;},
        querySelector(selector){return this.children.find(child=>'.'+child.className===selector)||null;}};
      function node(tag){return {tagName:tag.toUpperCase(),attributes:new Map(),setAttribute(name,value){this.attributes.set(name,value);}};}
      const handed=[],document={body:{classList:{add(){},remove(){}}},querySelector(){return null;},
        head:{appendChild(child){scripts.push(child);handed.push(host.autoformFetchJSON);}},createElement:node};
      const location={href:'https://example.test/project/roadmap/chapter/index.html'};
      const attributes=new Map([['data-catalog-src','../../dependencies/search-index.json']]);let drawn=null;
      const host={isConnected:true,dataset:{},classList:{add(){},remove(){}},closest(){return {};},
        addEventListener(type,callback){listeners.push({type,callback});},
        getAttribute(name){return attributes.get(name)||null;},setAttribute(name,value){attributes.set(name,value);},
        querySelector(selector){
          if(selector==='.bp-whole-map')return drawn;
          return selector==='.bp-whole-map[data-pm-ready]'&&drawn&&drawn.dataset.pmReady?drawn:null;
        }};
      const fetchJSON=()=>{},clients=new Map(),window={AutoformNavigation:{create(options){return {
        start(){options.onCommit({graph:{},readingSource:{}});},dispose(){},fetchJSON};}}};
      let alive=true;const dom$stage=stage,dom$announcement={textContent:''};
      eval(original('element'));eval(original('mapLoading'));eval(original('mapUnavailable'));eval(original('start'));
      function mount(){return {ready:Promise.resolve(),mapLoading,mapUnavailable};}
      start(host);
      const loadingNotes=()=>stage.children.filter(child=>child.className==='bp-dag-map-loading');
      assert.equal(loadingNotes().length,1);assert.equal(loadingNotes()[0].textContent,'Loading the map…');
      assert.equal(loadingNotes()[0].attributes.get('role'),'status');
      assert.equal(scripts.length,1);assert.equal(scripts[0].src,'https://example.test/project/javascripts/project-map.js');
      // The map reads its files through the book's router, which it finds on the book's host as its script starts.
      assert.strictEqual(handed[0],fetchJSON);
      // The map makes its host in the stage as its script starts and marks it set up as the script ends, so a load
      // without the host drew nothing and one without the mark stopped partway.
      if(fixture.outcome.startsWith('load-')&&fixture.outcome!=='load-without-map')drawn={className:'bp-whole-map',dataset:{}};
      if(fixture.outcome==='load-with-map')drawn.dataset.pmReady='1';
      const report=scripts[0][fixture.outcome==='error'?'onerror':'onload'];
      assert.equal(typeof report,'function','The map script reports how it went');report();
      const notices=()=>stage.children.filter(child=>child.className==='bp-dag-map-unavailable');
      assert.equal(notices().length,fixture.outcome==='load-with-map'?0:1);
      if(fixture.outcome==='load-with-map'){
        assert.equal(dom$announcement.textContent,'');assert.equal(loadingNotes().length,1,'A map says when it shows');
        listeners.filter(listener=>listener.type==='autoform-map-shown').forEach(listener=>listener.callback());
      }
      assert.equal(loadingNotes().length,0,"The map, or its notice, takes the place of the loading note");
      if(fixture.outcome!=='load-with-map'){
        const [notice]=notices();assert.equal(notice.tagName,'P');assert.equal(notice.attributes.get('role'),'status');
        assert.equal(notice.textContent,'The map could not load. Contents, search and the statement lists still work.');
        // A status region inserted with its text already in it is often not spoken, so the book's live region says it too.
        assert.equal(dom$announcement.textContent,notice.textContent);
        mapUnavailable();assert.equal(notices().length,1,'A second report keeps the one notice');
      }
    """, {"outcome": outcome})


def test_without_a_resize_observer_a_window_resize_resizes_at_once() -> None:
    """Execute the real fallback binding from bindEvents; the resize itself is a seam."""
    _run(r"""
      const listeners=[],window={},dom$stage={};let resized=0;
      function globalListen(target,type,callback){listeners.push({target,type,callback});}
      function resize(){resized++;}
      function queueResize(){assert.fail('Without an observer no resize waits for a frame');}
      const bind=original('bindEvents'),begin=bind.indexOf('if (window.ResizeObserver)');
      eval(bind.slice(begin,bind.indexOf('globalListen(window, "resize", syncSheetAccessibility)',begin)));
      assert.equal(listeners.length,1);assert.strictEqual(listeners[0].target,window);assert.equal(listeners[0].type,'resize');
      assert.strictEqual(listeners[0].callback,resize);
      listeners[0].callback();assert.equal(resized,1);
    """, {})


def test_resize_notifications_wait_for_one_frame_and_dispose_cancels_a_pending_one() -> None:
    """Execute the real observer binding, resize queue and disposal; the resize itself is a seam."""
    _run(r"""
      const frames=new Map(),cancelled=[],observed=[];let nextFrame=0,delivering=false,resized=0,disposed=0;
      class ResizeObserver {
        constructor(callback){this.callback=callback;this.targets=[];this.disconnections=0;observed.push(this);}
        observe(target){this.targets.push(target);}
        disconnect(){this.disconnections++;}
      }
      const window={ResizeObserver},dom$stage={},dom$breadcrumb={},navigation={dispose(){disposed++;}};
      function requestAnimationFrame(callback){const id=nextFrame++;frames.set(id,callback);return id;}
      function cancelAnimationFrame(id){cancelled.push(id);frames.delete(id);}
      function notify(observer){delivering=true;try{observer.callback([{target:dom$stage}],observer);}finally{delivering=false;}}
      function flushFrame(){const current=Array.from(frames);frames.clear();current.forEach(([id,callback])=>callback(id));}
      function resize(){assert(!delivering,'A resize waits for a frame, out of observer delivery');resized++;}
      function globalListen(){assert.fail('A ResizeObserver needs no window resize listener');}
      function clearTimeout(){} function stopRowMath(){}
      // The mount's lifecycle state, the observer binding from bindEvents and the client's dispose run as published.
      eval(productionSource.match(/\bvar alive =[^;]+;/)[0]);eval(original('queueResize'));
      const bind=original('bindEvents'),begin=bind.indexOf('if (window.ResizeObserver)');
      eval(bind.slice(begin,bind.indexOf('globalListen(window, "resize", syncSheetAccessibility)',begin)));
      const marker=productionSource.indexOf('dispose: function () { alive = false'),from=productionSource.indexOf('function',marker);
      let dispose=null;
      for(let end=productionSource.indexOf('}',from);!dispose;end=productionSource.indexOf('}',end+1)){
        try{dispose=eval('('+productionSource.slice(from,end+1)+')');}catch(error){if(!(error instanceof SyntaxError))throw error;}
      }
      const observer=observed[0];assert.strictEqual(observer.callback,queueResize);assert.deepEqual(observer.targets,[dom$stage,dom$breadcrumb]);
      notify(observer);notify(observer);notify(observer);
      assert.equal(frames.size,1);assert.equal(resized,0);
      flushFrame();assert.equal(resized,1);assert.equal(resizeFrame,null);
      flushFrame();assert.equal(resized,1,'A settled size starts no frame loop');
      notify(observer);const id=resizeFrame,pending=frames.get(id);
      dispose();
      assert.equal(alive,false);assert.deepEqual(cancelled,[id]);assert.equal(frames.size,0);assert.equal(resizeFrame,null);
      assert.equal(observer.disconnections,1);assert.equal(disposed,1);
      pending(id);notify(observer);queueResize();
      assert.equal(frames.size,0);assert.equal(resized,1,'A late frame or notification cannot resize a disposed shell');
    """, {})
