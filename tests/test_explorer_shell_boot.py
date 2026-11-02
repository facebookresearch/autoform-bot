"""Production boot gating and cold-mount order of the explorer shell, over tiny DOM and router seams.

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
def test_cold_mount_seeds_its_route_and_projects_the_loaded_index_before_layout(entry: str) -> None:
    """Execute the real cold-mount prefix and index adoption; the shell, projection and layout are seams."""
    _run(r"""
      const events=[];
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
      const navigation={getIndex(){events.push('index');return index;}};
      const host={querySelector(){return null;},classList:{add(){},remove(){},toggle(){}}};
      const window={location:{href:'https://example.test/roadmap/index.html',origin:'https://example.test'}};
      const start=productionSource.indexOf('  function mount('),end=productionSource.indexOf('    function globalListen(',start);
      assert(start>=0&&end>start,'The real cold-mount boundary must exist');
      const helpers=['safeHref','graphAt','useIndex','requestWikiLinks','useWikiLinks'].map(original).join('\n');
      const seams=`
        function buildShell() {events.push('shell');dom$search={removeAttribute(){}};}
        function bindEvents() {events.push('bind');}
        function updateProjection() {events.push('project');}
        function renderNavigation() {} function refresh() {}
        function renderWikiContext() {events.push('optional-wiki');}
        function applyRoute(bundle) {
          assert.strictEqual(bundle,firstBundle);assert.equal(ui$wikiRequested,true);
          assert.equal(ui$wikiLinks.availability,'unavailable');events.push('route');return Promise.resolve();
        }
        function record(phase) {
          assert.strictEqual(sharedIndex,index,"Cold layout must use the router's validated index");
          assert.equal(ui$requestedNode,fixture.entry,'The first resize must retain the canonical requested route');
          events.push(phase);
        }
        function resize() {record('resize');}
      `;
      const bootstrap=eval('('+productionSource.slice(start,end)+'\nreturn ui$requestedNode;\n'+helpers+'\n'+seams+'})');
      assert.equal(bootstrap(host,payload,1,firstBundle,navigation),fixture.entry);
      const phase=name=>events.indexOf(name);
      assert(phase('shell')<phase('bind')&&phase('bind')<phase('index')&&phase('index')<phase('project'));
      assert(phase('project')<phase('resize')&&phase('resize')<phase('route'));
      assert(events.filter(event=>event==='index').length===1);
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


def test_cold_mount_measures_full_page_framing_and_error_restores_native_reading() -> None:
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
      function mount(){assert(bodyClasses.has('bp-dag-app-page'),'Initial geometry must see the actual full-page viewport');
        events.push('layout');if(fail)throw Error('controlled mount failure');return {ready:Promise.resolve()};}
      eval(original('start'));start(host());
      assert.deepEqual(events,['layout']);assert(bodyClasses.has('bp-dag-app-page'));
      fail=true;clients.clear();bodyClasses.clear();start(host());
      assert.deepEqual(events,['layout','layout']);assert(!bodyClasses.has('bp-dag-app-page'));
      assert(!hostClasses.has('bp-dag-enhancing'));assert(loading.textContent.includes('complete mathematical page remains readable'));
    """, {})
