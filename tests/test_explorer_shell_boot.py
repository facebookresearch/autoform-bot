"""Production boot gating, cold-mount order, the map's failure notice and resize batching of the explorer shell,
over tiny DOM and router seams.

These are source checks, not substitutes for genuine browser journeys.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is required for shell boot checks")

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
      const host={querySelector(){return null;},classList:{add(){},remove(){},toggle(){}}};
      const window={location:{href:'https://example.test/roadmap/index.html',origin:'https://example.test'}};
      const start=productionSource.indexOf('  function mount('),end=productionSource.indexOf('    function globalListen(',start);
      assert(start>=0&&end>start,'The real cold-mount boundary must exist');
      const helpers=['safeHref','graphAt','adoptIndex','useIndex','requestWikiLinks','useWikiLinks'].map(original).join('\n');
      const seams=`
        function buildShell() {events.push('shell');dom$search={removeAttribute(){}};}
        function bindEvents() {events.push('bind');}
        function bindLeanCards() {events.push('lean-cards');}
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
      assert.deepEqual(events,['shell','bind','lean-cards','index','optional-wiki','route']);
      // An index that arrives after the mount is adopted and rendered at once.
      events.length=0;loaded=false;const client=bootstrap(host,payload,1,firstBundle,navigation);
      assert.deepEqual(events,['shell','bind','lean-cards','index','route']);
      events.length=0;client.useIndex(index);assert.deepEqual(events,['optional-wiki','project','outline','refresh']);
      events.length=0;client.useIndex(index);assert.deepEqual(events,[]);
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


def test_cold_mount_takes_the_full_page_and_error_restores_native_reading() -> None:
    """Run real boot ordering over tiny DOM/router seams; browser containment remains independent."""
    _run("""
      const events=[],bodyClasses=new Set(),hostClasses=new Set(),loading={hidden:true,classList:{add(){}}};
      const classes=set=>({add(name){set.add(name);},remove(name){set.delete(name);},contains(name){return set.has(name);}});
      const document={body:{classList:classes(bodyClasses)}};
      function host(){const attributes=new Map();return {isConnected:true,
        getAttribute(name){return attributes.get(name)||null;},setAttribute(name,value){attributes.set(name,value);},closest(){return {};},
        querySelector(){return loading;},classList:classes(hostClasses)};}
      const clients=new Map();let fail=false,options;
      const window={AutoformNavigation:{create(value){options=value;return {
        start(){try{options.onCommit({graph:{},readingSource:{}});}catch(error){options.onError(error);}},dispose(){}};}}};
      function mount(){assert(bodyClasses.has('bp-dag-app-page'),'The shell mounts into the full-page layout');
        events.push('mount');if(fail)throw Error('controlled mount failure');return {ready:Promise.resolve()};}
      eval(original('start'));start(host());
      assert.deepEqual(events,['mount']);assert(bodyClasses.has('bp-dag-app-page'));
      fail=true;clients.clear();bodyClasses.clear();start(host());
      assert.deepEqual(events,['mount','mount']);assert(!bodyClasses.has('bp-dag-app-page'));
      assert(!hostClasses.has('bp-dag-enhancing'));assert(loading.textContent.includes('complete mathematical page remains readable'));
    """, {})


@pytest.mark.parametrize("outcome", ["error", "load-without-map", "load-before-set-up", "load-with-map"])
def test_a_map_script_that_fails_or_draws_nothing_leaves_one_notice_in_the_stage(outcome: str) -> None:
    """Execute the real start() and notice; the mounted shell around them is a seam."""
    _run(r"""
      const scripts=[],stage={children:[],appendChild(child){this.children.push(child);return child;},
        querySelector(selector){return this.children.find(child=>'.'+child.className===selector)||null;}};
      function node(tag){return {tagName:tag.toUpperCase(),attributes:new Map(),setAttribute(name,value){this.attributes.set(name,value);}};}
      const handed=[],document={body:{classList:{add(){},remove(){}}},
        head:{appendChild(child){scripts.push(child);handed.push(host.autoformFetchJSON);}},createElement:node};
      const location={href:'https://example.test/project/roadmap/chapter/index.html'};
      const attributes=new Map([['data-catalog-src','../../dependencies/search-index.json']]);let drawn=null;
      const host={isConnected:true,dataset:{},classList:{add(){},remove(){}},closest(){return {};},addEventListener(){},
        getAttribute(name){return attributes.get(name)||null;},setAttribute(name,value){attributes.set(name,value);},
        querySelector(selector){
          if(selector==='.bp-whole-map')return drawn;
          return selector==='.bp-whole-map[data-pm-ready]'&&drawn&&drawn.dataset.pmReady?drawn:null;
        }};
      const fetchJSON=()=>{},clients=new Map(),window={AutoformNavigation:{create(options){return {
        start(){options.onCommit({graph:{},readingSource:{}});},dispose(){},fetchJSON};}}};
      let alive=true;const dom$stage=stage,dom$announcement={textContent:''};
      eval(original('element'));eval(original('mapUnavailable'));eval(original('start'));
      function mount(){return {ready:Promise.resolve(),mapUnavailable};}
      start(host);
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
      if(fixture.outcome==='load-with-map')assert.equal(dom$announcement.textContent,'');
      if(fixture.outcome!=='load-with-map'){
        const [notice]=notices();assert.equal(notice.tagName,'P');assert.equal(notice.attributes.get('role'),'status');
        assert.equal(notice.textContent,'The map could not load. Topics, search and the statement lists still work.');
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
      const window={ResizeObserver},dom$stage={},navigation={dispose(){disposed++;}};
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
      const observer=observed[0];assert.strictEqual(observer.callback,queueResize);assert.deepEqual(observer.targets,[dom$stage]);
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
