"""The router's published data, with blueprint-navigation.js run whole on real pages (:func:`run_router`): the search
index (transport, normalization and its batches), the optional wiki-links layer bound to the index's exact bytes, and
the reads the whole-project map makes through the router."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.ui import site_index
from autoform_cli.ui.graph_views import GraphView, ViewEdge, ViewNode
from tests.explorer_fixture import run_router

#: The published files' addresses; ``settled`` ticks the clock until a request settles, for pauses that wait on it.
_DATA = r"""
const indexURL = absolute('dependencies/index.json'), wikiURL = absolute('dependencies/wiki-links.json');
async function settled(promise, ms = 5000) {
  let done = false; promise.then(() => { done = true; }, () => { done = true; });
  for (let waited = 0; !done && waited < ms; waited += 10) await tick(10);
  assert(done, 'the request never settled'); return promise;
}
async function sha(bytes) { return Buffer.from(await webcrypto.subtle.digest('SHA-256', bytes)).toString('hex'); }
// Bytes as the router's cache counts them, and what it holds after start() and a walk to a, b and a again: three
// graphs and the two pages, as served.
const size = value => new TextEncoder().encode(typeof value === 'string' ? value : JSON.stringify(value)).byteLength;
const walkBytes = () => 3 * size(graph) + size(pageHTML(aSpec)) + size(pageHTML(bSpec));
"""


@pytest.fixture
def parent_wires(tmp_path: Path) -> tuple[dict, bytes, str]:
    """An index as render writes it, with parents as offsets, its exact bytes' digest, and the same rows as a legacy
    index named every parent."""
    title = r"Groups $\mathbb{Z}/n$ and `$literal$`"
    summary = "Every integer retains its authored hypotheses.\n\n" + "Complete mathematical prose, not a preview. " * 18
    definitions = (
        ("a", "node", "proved", "topic", "topic"),
        ("b", "node", "conditional", "topic", "outside-unpublished"),
        ("roadmap", "scope", "planned", "roadmap", None),
        ("0", "node", "stated", "roadmap", "roadmap"),
        ("topic", "scope", "proved", "roadmap", "roadmap"),
        ("self", "scope", "mathlib", "self", "self"),
        ("orphan", "node", "planned", "roadmap", None),
        ("zero-offset-child", "node", "defined", "a", "a"),
        ("literal-zero-child", "node", "planned", "0", "0"),
        ("scope:authored", "scope", "planned", "topic", "topic"),
    )
    nodes = tuple(
        ViewNode(node_id, title if position == 0 else node_id, kind, (node_id,), ((status, 1),),
                 status_key=status, summary=summary if position == 0 else None,
                 lean="Authored.groups Authored.quotient" if position == 0 else None)
        for position, (node_id, kind, status, _scope, _parent) in enumerate(definitions)
    )
    view = GraphView("full", "Authored mathematics", nodes, (ViewEdge("a", "b", 2, 3), ViewEdge("0", "a", 0, 1)))
    metadata = {
        node_id: {"route": f"../roadmap/{node_id}.html#canonical-{position}", "scope": scope, "parent": parent,
                  "data_url": f"scopes/{node_id}.json", "result": kind == "node"}
        for position, (node_id, kind, _status, scope, parent) in enumerate(definitions)
    }
    metadata["topic"].update(area="Authored algebra", navigation="collection")
    links = {node.id: f"scopes/{node.id}.html#node={node.id}" for node in nodes}
    legacy_nodes = []
    for node in nodes:
        # Every row has a route, so none repeats its context url; only containers name their graph data.
        entry = {"id": node.id, "kind": node.kind, "status": node.status_key, "title": node.title}
        entry.update({key: value for key, value in metadata[node.id].items()
                      if value is not None and (key != "data_url" or node.kind == "scope")})
        if node.summary:
            entry.update(summary=node.summary, lean=node.lean)
        legacy_nodes.append(entry)
    legacy_nodes[0]["reading_title"] = {
        "format": "inline-parts-v1", "source": title,
        "parts": [["text", "Groups "], ["math", r"\(\mathbb{Z}/n\)"], ["text", " and "], ["code", "$literal$"]],
    }
    legacy = {"schema": "autoform-dag-search/v1", "nodes": legacy_nodes, "relations": [[0, 1, 2, 3], [3, 0, 0, 1]]}
    digests: list[str] = []
    output = tmp_path / "index.json"
    site_index.write_search_index(output, view, context_links=links, metadata=metadata,
                                 include_relations=True, sha256_out=digests)
    return legacy, output.read_bytes(), digests[0]


def test_coalesced_parent_offset_index_matches_legacy_and_wiki_binds_received_not_restored_bytes(parent_wires) -> None:
    legacy, raw, digest = parent_wires
    legacy_text = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    run_router(_DATA + "const compactText=" + json.dumps(raw.decode()) + ",legacyText=" + json.dumps(legacy_text)
               + ",receivedDigest=" + json.dumps(digest) + ";\n" + r"""
      const bytes=new TextEncoder().encode(compactText),before=bytes.slice(),published=[];
      let release,requested;const arrived=new Promise(resolve=>requested=resolve),gate=new Promise(resolve=>release=resolve);
      const app=setup({onIndex(value){published.push(value);},fetch(url){
        if(url!==indexURL)return;
        requested();return gate.then(()=>({ok:true,status:200,url,headers:{get(){return null;}},
          arrayBuffer:async()=>bytes.slice().buffer,text:async()=>{throw new Error('Do not reserialize received index bytes');}}));
      }});
      const first=app.nav.loadIndex(),second=app.nav.loadIndex();assert.strictEqual(first,second);
      await arrived;assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
      release();const normalized=await first;assert.strictEqual(await second,normalized);
      assert.equal(published.length,1);assert.strictEqual(published[0],normalized);assert.strictEqual(app.nav.getIndex(),normalized);
      assert.strictEqual(await app.nav.loadIndex(),normalized);
      assert.equal(app.fetches.filter(url=>url===indexURL).length,1);

      const full=setup();full.contents.set(indexURL,JSON.parse(legacyText));const fullIndex=await full.nav.loadIndex();
      assert.deepEqual(normalized,fullIndex,'Only transport parents differ; the complete published index must remain equivalent');
      assert.deepEqual(normalized.nodes.map(node=>node.id),['a','b','roadmap','0','topic','self','orphan',
        'zero-offset-child','literal-zero-child','scope:authored']);
      assert.deepEqual(normalized.nodes.map(node=>node._index),[0,1,2,3,4,5,6,7,8,9]);
      assert.equal(normalized.nodes[0].route,absolute('roadmap/a.html#canonical-0'));
      assert.equal(normalized.nodes[0].data_url,null);assert.equal(normalized.nodes[0].url,null);
      assert.equal(normalized.nodes[4].data_url,absolute('dependencies/scopes/topic.json'));
      assert.deepEqual(normalized.relations,[['a','b',2,3],['0','a',0,1]]);
      assert.equal(normalized.nodes[0].parent,'topic');assert.equal(normalized.nodes[7].parent,'a');
      assert.equal(normalized.nodes[8].parent,'0');assert.equal(normalized.nodes[5].parent,'self');
      assert(!Object.hasOwn(normalized.nodes[6],'parent'),'An uncontained leaf may have a fallback roadmap scope, not a parent');

      assert.equal(await sha(bytes),receivedDigest);const legacyDigest=await sha(new TextEncoder().encode(legacyText));
      assert.notEqual(legacyDigest,receivedDigest,'Lossless normalization does not make distinct transport bytes interchangeable');
      const layer={schema:'autoform-wiki-links/v1',index_sha256:legacyDigest,availability:'partial',references:[[0,1],[8,9]],
        unavailable_sources:[6]};
      app.contents.set(wikiURL,layer);const mathBefore=JSON.stringify(normalized);
      const stale=await app.nav.loadWikiLinks();assert.equal(stale.availability,'unavailable');assert.equal(stale.reason,'stale_index');
      layer.index_sha256=receivedDigest;const wiki=await app.nav.loadWikiLinks();
      assert.equal(wiki.availability,'partial');assert.equal(wiki.index_sha256,receivedDigest);
      assert.deepEqual(wiki.references,[['a','b'],['literal-zero-child','scope:authored']]);
      assert.deepEqual(wiki.unavailable_sources,['orphan']);assert.equal(JSON.stringify(normalized),mathBefore);
      assert.deepEqual(bytes,before);assert.equal(app.errors.length,0);assert.equal(full.errors.length,0);
      assert.equal(published.length,1);assert.equal(app.fetches.filter(url=>url===indexURL).length,1);
      app.nav.dispose();full.nav.dispose();
    """)


def test_parent_offsets_do_not_reinterpret_literal_ids_null_or_missing_legacy_containment() -> None:
    run_router(_DATA + r"""
      const payload=index();payload.nodes.push(
        {id:'0',title:'Literal zero',scope:'roadmap',parent:null},
        {id:'ordinal-zero',title:'Ordinal zero',parent:0},
        {id:'literal-zero',title:'Literal zero parent',parent:'0'},
        {id:'forward',title:'Forward parent',parent:7},
        {id:'future',title:'Future parent',scope:'future',parent:'future'},
        {id:'uncontained',title:'Uncontained article',scope:'roadmap'},
        {id:'unknown-parent',title:'Unknown parent',scope:'roadmap',parent:'unpublished'},
        {id:'boolean-parent',title:'Legacy nonnumeric parent',parent:false});
      const published=[],app=setup({onIndex(value){published.push(value);}});
      app.contents.set(indexURL,payload);const value=await app.nav.loadIndex();
      const byId=new Map(value.nodes.map(node=>[node.id,node]));
      assert.strictEqual(byId.get('0').parent,null);assert.equal(byId.get('ordinal-zero').parent,'roadmap');
      assert.equal(byId.get('literal-zero').parent,'0');assert.equal(byId.get('forward').parent,'future');
      assert.equal(byId.get('future').parent,'future');assert.equal(byId.get('unknown-parent').parent,'unpublished');
      assert.strictEqual(byId.get('boolean-parent').parent,false);
      assert(!Object.hasOwn(byId.get('uncontained'),'parent'));assert(!Object.hasOwn(byId.get('a'),'parent'));
      assert(!Object.hasOwn(byId.get('roadmap'),'parent'));
      assert.equal(published.length,1);assert.strictEqual(published[0],value);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_compact_index_endpoints_round_trip_and_invalid_offsets_fail_closed() -> None:
    run_router(_DATA + r"""
      const app=setup(),compact=index(); compact.relations=[[0,1,1,0],['a',2,0,3]];
      app.contents.set(indexURL,compact);
      await app.nav.start();
      assert.deepEqual(app.nav.getIndex().relations,[['roadmap','a',1,0],['a','b',0,3]]);
      assert.equal(app.errors.length,0); app.nav.dispose();
      for(const row of [[-1,1,1,0],[3,1,1,0],[true,1,1,0],[.5,1,1,0],[0,1,-1,0],[0,1,0,0]]) {
        const invalid=setup(),payload=index(); payload.relations=[row];
        invalid.contents.set(indexURL,payload);
        await invalid.nav.start();
        assert.equal(invalid.nav.getIndex(),null); assert.equal(invalid.errors.length,1);
        assert.equal(invalid.errors[0].message,'Invalid search relation endpoint');
        assert.equal(invalid.win.location.href,rootSpec.route);
        invalid.nav.dispose();
      }
    """)


def test_index_links_resolved_once_per_page_match_parsing_each_whole_value() -> None:
    run_router(_DATA + r"""
      // Earlier rows leave the other two fields absent, so a literal "undefined" must not reuse their result.
      const whole=value=>new URL(value,indexURL).href;
      const cases=[['route','a.html#x',1],['route','a.html#y',1],['route','a.html #x',1],['route','a.html#\u00e9',1],
        ['route','a.html#a b',1],['route','a.html#x#y',1],['route','a.html#%zz',1],['route','a.html#',1],['route','#only',1],
        ['route','../roadmap/b.html?q=1#n',1],['route','undefined',1],
        ['route','https://outside.test/p#a',0],['url','https://outside.test/p#b',1],
        ['url','https://outside.test/q#a',1],['route','https://outside.test/q#b',0],
        ['route','javascript:alert(1)#x',0],['url','https://user:pw@outside.test/p#x',0]];
      const payload=index();
      cases.forEach(([field,value],n)=>payload.nodes.push({id:'case-'+n,title:'Case '+n,[field]:value}));
      const app=setup();app.contents.set(indexURL,payload);
      const result=await app.nav.loadIndex();
      cases.forEach(([field,value,kept],n)=>assert.strictEqual(result.nodes[n+3][field],kept?whole(value):null,value));
      app.nav.dispose();
    """)


def test_indexed_legacy_relations_and_bounded_html_cache_remain_compatible() -> None:
    run_router(_DATA + r"""
      const app=setup({htmlEntries:1}); const legacy=index(); legacy.relations=[[1,2,1,0]];
      app.contents.set(indexURL,legacy);
      await app.nav.start(); await app.nav.navigateNode('a'); await app.nav.navigateNode('b'); await app.nav.navigateNode('a');
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);
      assert.equal(app.fetches.filter(url=>url===aSpec.route).length,2);
      assert.equal(app.fetches.filter(url=>url===absolute('dependencies/a.json')).length,1);
      assert.equal(app.fetches.filter(url=>url===indexURL).length,1);
      // Disposed, the router no longer answers Back.
      app.nav.dispose(); const requests=app.fetches.length; await app.back(); await tick(200);
      assert.equal(app.fetches.length,requests); assert.equal(app.commits.length,4);
    """)


def test_the_index_text_does_not_take_the_page_cache_budget() -> None:
    run_router(_DATA + r"""
      // Room for the start (index text, root graph and a snapshot) and for the walk, but not for the text
      // and the walk: a cached text would evict a page once the walk begins, which the fetch counts show.
      const payload=index();for(let n=0;n<64;n++) payload.nodes.push({id:'leaf-'+n,title:'Leaf '+n});
      const walk=walkBytes(),text=size(payload),budget=text+size(graph)+64;
      assert(walk+4*32<=budget&&budget<text+walk,'The walk and its snapshots must fit, and the text with them must not');
      const app=setup({cacheBytes:budget});app.contents.set(indexURL,payload);
      await app.nav.start();await app.nav.navigateNode('a');await app.nav.navigateNode('b');await app.nav.navigateNode('a');
      assert.equal(app.fetches.filter(url=>url===aSpec.route).length,1);
      assert.equal(app.fetches.filter(url=>url===absolute('dependencies/a.json')).length,1);
      assert.equal(app.errors.length,0);app.nav.dispose();
    """)


#: An index of 1,025 rows whose relations name its first, middle and last rows by offset. On ``steppedClock``'s clock a
#: batch ends every 512 rows, so it loads in three batches, with two pauses between.
_PAINT = r"""
function paintCatalogue() {
  const nodes=Array.from({length:1025},(_,n)=>({id:'paint-'+n,title:'Paint item '+n,kind:'node',scope:'roadmap',
    route:'../roadmap/paint-'+n+'.html',status:'planned'}));
  return {schema:'autoform-dag-search/v1',nodes,relations:[[0,1024,2,3],[512,0,0,1]]};
}
function assertPainted(app,result,published) {
  assert.deepEqual(result.nodes.map(node=>node.id),Array.from({length:1025},(_,n)=>'paint-'+n));
  assert.deepEqual(result.relations,[['paint-0','paint-1024',2,3],['paint-512','paint-0',0,1]]);
  assert.equal(published.length,1);assert.strictEqual(published[0],result);assert.strictEqual(app.nav.getIndex(),result);
  assert.equal(app.fetches.filter(url=>url===indexURL).length,1);assert.equal(app.errors.length,0);
}
"""


def test_index_batches_end_on_25_ms_of_work_never_on_a_row_count() -> None:
    run_router(_DATA + r"""
      // A hidden page pauses with a 0 ms timer, which this counts. The router's clock, performance.now(), moves only by
      // what each step costs: step per read of it, pause per pause, parse, decode per decoding, and wait per chunk the
      // network sends, which is not work.
      async function load({rows=0,relations=0,step=0,pause=0,parse=0,decode=0,wait=0,stream=false}) {
        const payload=index();
        for(let n=0;n<rows;n++) payload.nodes.push({id:'leaf-'+n,title:'Leaf '+n});
        if(relations) payload.relations=Array.from({length:relations},(_,n)=>[n%3,(n+1)%3,1,0]);
        const text=JSON.stringify(payload),seen={pauses:0,parsed:null};let now=0;
        const layer=JSON.stringify({schema:'autoform-wiki-links/v1',index_sha256:await sha(new TextEncoder().encode(text)),
          availability:'complete',references:[[0,1]],unavailable_sources:[]});
        const chunked=body=>{const data=new TextEncoder().encode(body),cuts=[0,1,data.length>>1,data.length];let part=0;
          return new Response(new ReadableStream({pull(controller){
            now+=wait;if(++part<cuts.length)controller.enqueue(data.slice(cuts[part-1],cuts[part]));else controller.close();
          }}));};
        const published=[],app=setup({onIndex(value){published.push(value);},fetch(url,_settings,reply){
          if(url===indexURL)return stream?chunked(text):reply(text,url);
          if(url===wikiURL)return chunked(layer);
        }});
        Object.defineProperty(app.doc,'visibilityState',{value:'hidden',configurable:true});
        app.win.performance.now=()=>now+=step;
        const timer=app.win.setTimeout,parseJSON=app.win.JSON.parse,Decoder=app.win.TextDecoder;
        app.win.setTimeout=(callback,delay,...rest)=>{
          if(delay===0){
            // Nothing is published while batches remain.
            assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);seen.pauses++;now+=pause;
          }
          return timer(callback,delay,...rest);
        };
        app.win.JSON.parse=function(value,...rest){if(value===text){seen.parsed=seen.pauses;now+=parse;}return parseJSON.call(this,value,...rest);};
        app.win.TextDecoder=class extends Decoder{decode(...input){now+=decode;return super.decode(...input);}};
        const first=app.nav.loadIndex();assert.strictEqual(app.nav.loadIndex(),first,'callers share one load');
        const result=await settled(first);
        assert.equal(published.length,1);assert.strictEqual(published[0],result);assert.strictEqual(app.nav.getIndex(),result);
        assert.equal(app.fetches.filter(url=>url===indexURL).length,1);
        if(stream){
          // Only the index pauses: the wiki layer's decoding goes straight on.
          const before=seen.pauses;assert.equal((await settled(app.nav.loadWikiLinks())).availability,'complete');
          assert.equal(seen.pauses,before,'the wiki layer paused');
        }
        app.nav.dispose();return {seen,result:JSON.stringify(result)};
      }
      const unbatched={};
      for(const [name,options,pauses,parsed=0] of [
        // A frozen clock never ends a batch. One that passes a slice at each read, which comes every 128 rows or
        // relations, ends a batch at each.
        ['a frozen clock',{rows:1277,relations:384},0],
        ['a slice per read',{rows:1277,relations:384,step:25},13],
        // Four reads make a slice, and the 50 ms of a pause do not count toward the next.
        ['a slice per four reads',{rows:1277,relations:384,step:25/4,pause:50},3],
        ['relations alone',{relations:2050,step:25/4},4],
        // The first slice begins with the parse: a parse as long as a slice ends the first batch at the first read.
        ['a long parse',{rows:1277,relations:384,parse:25},1],
        // A read whose decoding takes a slice pauses before the parse; waiting for the network is not work.
        ['a slow read',{stream:true,decode:25,wait:20},1,1],
        ['a fast read of a slow transfer',{stream:true,decode:24,wait:20},0],
      ]) {
        const {seen,result}=await load(options);
        assert.equal(seen.pauses,pauses,name);assert.equal(seen.parsed,parsed,name);
        // Batches never change the index.
        const key=[options.rows,options.relations].join();assert.equal(result,unbatched[key]=unbatched[key]||result,name);
      }
    """)


def test_a_visible_page_paints_two_frames_in_each_index_pause_with_one_50_ms_fallback() -> None:
    run_router(_DATA + _PAINT + r"""
      // A pause waits for a second frame, so the first can paint, and ends at the latest 50 ms after it began; a window
      // without both frame functions pauses with 0 ms timers. Nothing is published before the last pause ends, and
      // nothing stays scheduled after.
      for(const [frames,end] of [['painted',64],['stalled',100],['first only',100],['no requestAnimationFrame',0],
          ['no cancelAnimationFrame',0]]) {
        const published=[],app=steppedClock(setup({onIndex(value){published.push(value);}}));
        app.contents.set(indexURL,paintCatalogue());
        // Frames asked for that never come, until cancelled.
        const held=new Map(),paint=app.win.requestAnimationFrame,unpaint=app.win.cancelAnimationFrame;let asked=0;
        if(frames==='no requestAnimationFrame')app.win.requestAnimationFrame=undefined;
        if(frames==='no cancelAnimationFrame')app.win.cancelAnimationFrame=undefined;
        if(frames==='stalled'||frames==='first only'){
          app.win.requestAnimationFrame=callback=>{
            if(++asked%2===1&&frames==='first only')return paint(callback);
            held.set('held-'+asked,callback);return 'held-'+asked;
          };
          app.win.cancelAnimationFrame=id=>{if(!held.delete(id))unpaint(id);};
        }
        const loading=app.nav.loadIndex();await flush();
        if(end){await tick(end-1);assert.equal(app.nav.getIndex(),null,frames);assert.equal(published.length,0,frames);}
        await tick(end?1:0);assert.equal(published.length,1,frames);
        assertPainted(app,await loading,published);
        assert.equal(held.size,0,frames+': a frame that never came is cancelled');
        assert.equal(clock.timers.size,0,frames+': nothing stays scheduled');app.nav.dispose();
      }
    """)


def test_disposal_during_an_index_pause_rejects_at_once_and_publishes_nothing() -> None:
    run_router(_DATA + _PAINT + r"""
      // Disposed while a pause waits, for its first frame on a visible page or its timer on a hidden one, the load fails
      // with an AbortError as that pause ends: nothing is published or reported, no frame or pause follows, and nothing
      // stays scheduled.
      for(const page of ['visible','hidden']){
        const published=[],app=steppedClock(setup({onIndex(value){published.push(value);}}));
        app.contents.set(indexURL,paintCatalogue());
        if(page==='hidden')Object.defineProperty(app.doc,'visibilityState',{value:'hidden',configurable:true});
        const paint=app.win.requestAnimationFrame,timer=app.win.setTimeout;let frames=0,pauses=0,failure=null;
        app.win.requestAnimationFrame=callback=>{frames++;return paint(callback);};
        app.win.setTimeout=(callback,delay,...rest)=>{if(delay===0||delay===50)pauses++;return timer(callback,delay,...rest);};
        const loading=app.nav.loadIndex().catch(error=>{failure=error;});
        await flush();assert.equal(pauses,1,page);const before=[frames,pauses];
        app.nav.dispose();await tick(page==='visible'?16:0);
        assert.equal(failure&&failure.name,'AbortError',page);assert.deepEqual([frames,pauses],before,page);
        assert.equal(clock.timers.size,0,page);assert.equal(app.nav.getIndex(),null);assert.equal(published.length,0);
        assert.equal(app.errors.length,0);assert.equal(app.win.location.href,rootSpec.route);await loading;
      }
    """)


def test_an_invalid_index_publishes_nothing_and_a_shared_retry_recovers() -> None:
    run_router(_DATA + r"""
      // An index that fails validation, at once or after batches, is never published: its load fails for every caller,
      // with one error reported, and a retry, which callers share, loads the corrected index.
      const rows=(count,row)=>{const payload=index();for(let n=0;n<count;n++)payload.nodes.push(row(n));return payload;};
      const leaves=rows(1024,n=>({id:'leaf-'+n,title:'Leaf '+n,route:'../roadmap/leaf-'+n+'.html'}));
      const duplicate=structuredClone(leaves);duplicate.nodes.at(-1).id=duplicate.nodes[0].id;
      const children=rows(513,n=>({id:'child-'+n,title:'Child '+n,parent:0}));
      const linked=index();linked.relations=Array.from({length:2050},()=>[1,2,1,0]);
      const missing=structuredClone(linked);missing.relations.at(-1)[1]='missing';
      const parented=value=>assert.equal(value.nodes.at(-1).parent,'roadmap');
      for(const [name,broken,corrected,message,check=()=>{}] of [
        ['an unsupported index',{schema:'wrong',nodes:[]},index(),'Unsupported search index'],
        ['a late duplicate node',duplicate,leaves,'Invalid search node'],
        ...[-1,.5,516,2**53].map(parent=>{const broken=structuredClone(children);broken.nodes.at(-1).parent=parent;
          return ['a late parent offset '+parent,broken,children,'Invalid search parent endpoint',parented];}),
        ['a late relation endpoint',missing,linked,'Invalid search relation endpoint'],
      ]) {
        const published=[],app=steppedClock(setup({onIndex(value){published.push(value);}}));
        app.contents.set(indexURL,broken);
        const first=app.nav.loadIndex();assert.strictEqual(app.nav.loadIndex(),first,name);
        await assert.rejects(settled(first),{message},name);
        assert.equal(app.nav.getIndex(),null,name);assert.equal(published.length,0,name);
        assert.equal(app.errors.length,1,name);assert.equal(app.errors[0].url,indexURL,name);
        assert.equal(app.win.location.href,rootSpec.route,name);
        app.contents.set(indexURL,corrected);
        const retry=app.nav.loadIndex();assert.strictEqual(app.nav.loadIndex(),retry,name);const recovered=await settled(retry);
        assert.deepEqual(recovered.nodes.map(node=>node.id),corrected.nodes.map(node=>node.id),name);check(recovered);
        assert.equal(recovered.relations.length,corrected.relations.length,name);
        assert(recovered.relations.every(row=>JSON.stringify(row)==='["a","b",1,0]'),name);
        assert.equal(published.length,1,name);assert.strictEqual(published[0],recovered,name);
        assert.strictEqual(app.nav.getIndex(),recovered,name);
        assert.equal(app.fetches.filter(url=>url===indexURL).length,2,name);app.nav.dispose();
      }
    """)


#: A router whose index and wiki layer are served as bytes. The index, with a BOM if asked, streams in three chunks,
#: or arrives as one chunk viewing a larger buffer (as WebKit hands a large body over), as an ArrayBuffer, or as text;
#: the layer (``layerState``) streams, unless ``extra.wikiFetch`` answers.
_WIKI = r"""
function streamed(bytes,status=200) {
  // The BOM and the JSON split across chunks, never round-tripped through text.
  return new Response(new ReadableStream({start(controller){
    controller.enqueue(bytes.slice(0,1));controller.enqueue(bytes.slice(1,5));controller.enqueue(bytes.slice(5));controller.close();
  }}),{status});
}
async function wikiApp(extra={}) {
  const catalog=extra.catalog||index(),encoded=new TextEncoder().encode(JSON.stringify(catalog));
  const bytes=extra.bom?new Uint8Array([239,187,191,...encoded]):encoded,hash=await sha(bytes);
  const events=[],indexEvents=[],signals=[];
  const state={layer:{schema:'autoform-wiki-links/v1',index_sha256:hash,availability:'complete',
    references:[[0,1],[1,2],[2,0]],unavailable_sources:[]},status:200};
  const layerResponse=()=>streamed(new TextEncoder().encode(JSON.stringify(state.layer)),state.status);
  const app=setup({initial:extra.initial,timeout:extra.timeout,onIndex(value){indexEvents.push(value);},
    onWikiLinks(value){events.push(value);if(extra.onWikiLinks)extra.onWikiLinks(value);},
    fetch(url,settings,reply){
      if(url===indexURL){
        if(extra.transport==='text')return reply(catalog,url);
        if(extra.transport==='single')return new Response(new ReadableStream({start(controller){
          const padded=new Uint8Array(bytes.length+8);padded.set(bytes,4);
          controller.enqueue(padded.subarray(4,4+bytes.length));controller.close();}}));
        if(extra.transport==='arrayBuffer')return {ok:true,status:200,url,headers:{get(){return null;}},
          arrayBuffer:async()=>bytes.slice().buffer,text:async()=>{throw new Error('Raw bytes must not become text first');}};
        return streamed(bytes);
      }
      if(url===wikiURL){signals.push(settings.signal);return extra.wikiFetch?extra.wikiFetch(url,settings,layerResponse):layerResponse();}
    }});
  if(extra.crypto===false)Object.defineProperty(app.win,'crypto',{value:undefined,configurable:true});
  return Object.assign(app,{layerState:state,events,indexEvents,signals,indexBytes:bytes,indexHash:hash});
}
function assertUnavailable(value) {
  assert(value);assert.equal(value.schema,'autoform-wiki-links/v1');
  assert.equal(value.availability,'unavailable');assert.deepEqual(value.references,[]);
  assert.equal(typeof value.reason,'string');
  assert(!JSON.stringify(value).includes('/Users/'));assert(!JSON.stringify(value).includes('private-provider'));
}
"""


def _wiki(body: str) -> None:
    run_router(_DATA + _WIKI + body)


def test_wiki_layer_is_lazy_coalesced_sha_bound_and_keeps_math_relations() -> None:
    _wiki(r"""
      let release,observed;const requested=new Promise(resolve=>observed=resolve);
      const app=await wikiApp({wikiFetch(_url,_settings,response){observed();return new Promise(resolve=>release=()=>resolve(response()));}});
      assert.equal(app.nav.getWikiLinks(),null);await app.nav.start();
      assert.equal(app.fetches.filter(url=>url===wikiURL).length,0);
      const before=JSON.stringify(app.nav.getIndex().relations),first=app.nav.loadWikiLinks(),second=app.nav.loadWikiLinks();
      assert.strictEqual(first,second,'Concurrent callers must share one optional request');
      await requested;release();const result=await first;
      assert.equal(result.index_sha256,app.indexHash);assert.equal(result.availability,'complete');
      assert.deepEqual(result.references,[['roadmap','a'],['a','b'],['b','roadmap']]);
      assert.deepEqual(result.unavailable_sources,[]);assert.strictEqual(app.nav.getWikiLinks(),result);
      assert.equal(app.events.length,1);assert.strictEqual(app.events[0],result);
      assert.strictEqual(await app.nav.loadWikiLinks(),result);
      assert.equal(app.fetches.filter(url=>url===wikiURL).length,1);
      assert.equal(JSON.stringify(app.nav.getIndex().relations),before);
      assert.equal(app.indexEvents.length,1);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_a_slow_steady_transfer_loads_and_only_a_stall_times_out() -> None:
    _wiki(r"""
      // A body that sends a chunk after each gap on the page's clock; like a real fetch's, it errors once its request
      // aborts.
      function paced(app,gaps,signal) {
        const bytes=new TextEncoder().encode(JSON.stringify(app.layerState.layer)),size=Math.ceil(bytes.length/gaps.length);
        return new Response(new ReadableStream({async start(controller){
          signal.addEventListener('abort',()=>{try{controller.error(new DOMException('Aborted','AbortError'));}catch(_closed){}});
          for(let part=0;part<gaps.length;part++){
            await new Promise(resolve=>app.win.setTimeout(resolve,gaps[part]));
            try{controller.enqueue(bytes.slice(part*size,(part+1)*size));}catch(_cancelled){return;}
          }
          try{controller.close();}catch(_cancelled){}
        }}));
      }
      // Three chunks 600 ms apart take 1.8 s in all, past the 1 s timeout, and still load.
      const steady=await wikiApp({timeout:1000,wikiFetch(_url,settings){return paced(steady,[600,600,600],settings.signal);}});
      await steady.nav.start();assert.equal((await settled(steady.nav.loadWikiLinks())).availability,'complete');steady.nav.dispose();
      // A transfer that stops sending for longer than the timeout is reported as timed out.
      const stalled=await wikiApp({timeout:1000,wikiFetch(_url,settings){return paced(stalled,[0,1600],settings.signal);}});
      await stalled.nav.start();const late=await settled(stalled.nav.loadWikiLinks());
      assertUnavailable(late);assert.equal(late.reason,'request_timed_out');stalled.nav.dispose();
    """)


def test_index_digest_is_the_exact_bytes_received_however_they_arrive() -> None:
    _wiki(r"""
      // The BOM makes the received bytes differ from the decoded text's; a single chunk is a view into a larger buffer.
      for(const transport of ['stream','single','arrayBuffer']){
        const app=await wikiApp({bom:true,transport});await app.nav.start();
        const normalizedBytes=new TextEncoder().encode(new TextDecoder().decode(app.indexBytes));
        assert.notEqual(await sha(normalizedBytes),app.indexHash);
        const result=await app.nav.loadWikiLinks();assert.equal(result.availability,'complete',transport);
        assert.equal(result.index_sha256,app.indexHash,transport);assert.equal(app.nav.getIndex().nodes[0].id,'roadmap',transport);
        assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);assert.equal(app.errors.length,0);app.nav.dispose();
      }
    """)


def test_literal_zero_id_and_reference_rows_are_not_reinterpreted_as_offsets() -> None:
    _wiki(r"""
      const catalog=index();catalog.nodes.push({id:'0',title:'Literal zero',kind:'node',scope:'roadmap',route:'../roadmap/zero.html'});
      const app=await wikiApp({catalog});app.layerState.layer.references=[[0,3],[3,1],[3,1]];
      const result=await app.nav.loadWikiLinks();assert.equal(result.availability,'complete');
      assert.deepEqual(result.references,[['roadmap','0'],['0','a'],['0','a']]);
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);app.nav.dispose();
    """)


def test_view_order_changes_cannot_rebind_original_index_offsets() -> None:
    _wiki(r"""
      const app=await wikiApp();await app.nav.loadIndex();app.nav.getIndex().nodes.reverse();
      const result=await app.nav.loadWikiLinks();assert.equal(result.availability,'complete');
      assert.deepEqual(result.references,[['roadmap','a'],['a','b'],['b','roadmap']]);
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);app.nav.dispose();
    """)


def test_partial_source_coverage_is_retained_without_leaking_publisher_reason() -> None:
    _wiki(r"""
      const app=await wikiApp();Object.assign(app.layerState.layer,{availability:'partial',references:[[1,2]],
        unavailable_sources:[2],reason:'/Users/private-provider/hidden-source.md'});
      const result=await app.nav.loadWikiLinks();assert.equal(result.availability,'partial');
      assert.deepEqual(result.references,[['a','b']]);assert.deepEqual(result.unavailable_sources,['b']);
      assert(!JSON.stringify(result).includes('/Users/'));assert(!JSON.stringify(result).includes('hidden-source.md'));
      assert.equal(app.events.length,1);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_verified_empty_complete_layer_differs_from_missing_or_unavailable() -> None:
    _wiki(r"""
      const app=await wikiApp();app.layerState.layer.references=[];
      const result=await app.nav.loadWikiLinks();assert.equal(result.availability,'complete');
      assert.deepEqual(result.references,[]);assert.equal(result.index_sha256,app.indexHash);
      const unavailable=await wikiApp();Object.assign(unavailable.layerState.layer,{availability:'unavailable',references:[],
        unavailable_sources:[0,1,2],reason:'some source paths were unsupported'});
      const unknown=await unavailable.nav.loadWikiLinks();assertUnavailable(unknown);
      assert.deepEqual(unknown.unavailable_sources,['roadmap','a','b']);app.nav.dispose();unavailable.nav.dispose();
    """)


def test_missing_optional_asset_retries_without_breaking_canonical_navigation() -> None:
    _wiki(r"""
      const app=await wikiApp();await app.nav.start();app.layerState.status=404;
      const missing=await app.nav.loadWikiLinks();assertUnavailable(missing);
      assert.strictEqual(app.nav.getWikiLinks(),missing);assert.equal(app.errors.length,0);
      const moved=await app.nav.navigateNode('b');assert.equal(moved.nodeId,'b');
      assert(moved.readingSource.textContent.includes('Complete source statement and proof.'));
      app.layerState.status=200;const loaded=await app.nav.loadWikiLinks();assert.equal(loaded.availability,'complete');
      assert.equal(app.fetches.filter(url=>url===wikiURL).length,2);assert.equal(app.events.length,2);
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_sha_mismatch_is_unavailable_and_does_not_poison_a_later_valid_retry() -> None:
    _wiki(r"""
      const app=await wikiApp();await app.nav.start();
      app.layerState.layer.index_sha256=await sha(new TextEncoder().encode('Different real index bytes'));
      const stale=await app.nav.loadWikiLinks();assertUnavailable(stale);assert.equal(app.errors.length,0);
      app.layerState.layer.index_sha256=app.indexHash;
      const corrected=await app.nav.loadWikiLinks();assert.equal(corrected.availability,'complete');
      assert.equal(app.fetches.filter(url=>url===wikiURL).length,2);assert.equal(app.events.length,2);
      assert.equal((await app.nav.navigateNode('a')).nodeId,'a');assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_invalid_json_is_unavailable_and_retry_does_not_reuse_corrupt_cache() -> None:
    _wiki(r"""
      let corrupt=true;const app=await wikiApp({wikiFetch(_url,_settings,response){return corrupt?new Response('{'):response();}});
      await app.nav.start();assertUnavailable(await app.nav.loadWikiLinks());
      assert.equal(app.errors.length,0);corrupt=false;
      assert.equal((await app.nav.loadWikiLinks()).availability,'complete');
      assert.equal(app.fetches.filter(url=>url===wikiURL).length,2);assert.equal(app.events.length,2);
      assert.equal((await app.nav.navigateNode('b')).nodeId,'b');assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_malformed_optional_layer_fails_atomically_and_leaves_index_usable() -> None:
    _wiki(r"""
      const damage={schema:raw=>{raw.schema='unsupported';},references:raw=>{raw.references={};},
        row:raw=>{raw.references=[[0,1,2]];},string_offset:raw=>{raw.references=[['0',1]];},
        boolean_offset:raw=>{raw.references=[[false,1]];},negative:raw=>{raw.references=[[-1,1]];},
        too_large:raw=>{raw.references=[[0,3]];},fraction:raw=>{raw.references=[[0,.5]];},
        unavailable_array:raw=>{raw.unavailable_sources={};},unavailable_offset:raw=>{raw.unavailable_sources=[true];},
        availability:raw=>{raw.availability='assumed-complete';},inconsistent_complete:raw=>{raw.unavailable_sources=[1];},
        reason:raw=>{raw.reason={private:'/Users/private-provider/source'};}};
      for(const [kind,change] of Object.entries(damage)){
        const app=await wikiApp();await app.nav.start();change(app.layerState.layer);
        const result=await app.nav.loadWikiLinks();assertUnavailable(result);
        assert.equal(app.events.length,1,kind);assert.strictEqual(app.events[0],result,kind);
        assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);
        assert.equal((await app.nav.navigateNode('b')).nodeId,'b',kind);assert.equal(app.errors.length,0,kind);app.nav.dispose();
      }
    """)


def test_invalid_late_row_never_exposes_partially_normalized_references() -> None:
    _wiki(r"""
      const app=await wikiApp();app.layerState.layer.references=Array.from({length:2050},()=>[0,1]);
      app.layerState.layer.references.push([1,-1]);
      const result=await settled(app.nav.loadWikiLinks());assertUnavailable(result);
      assert.equal(app.events.length,1);assert.deepEqual(app.events[0].references,[]);
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);app.nav.dispose();
    """)


def test_disposal_aborts_optional_request_and_ignores_its_late_result() -> None:
    _wiki(r"""
      let release,observed;const requested=new Promise(resolve=>observed=resolve);
      const app=await wikiApp({wikiFetch(_url,_settings,response){observed();return new Promise(resolve=>release=()=>resolve(response()));}});
      const pending=app.nav.loadWikiLinks();await requested;
      app.nav.dispose();assert.equal(app.signals.length,1);assert.equal(app.signals[0].aborted,true);
      assert.equal(await pending,null);release();await flush();
      assert.equal(app.nav.getWikiLinks(),null);
      assert.equal(app.events.length,0);assert.equal(app.nav.getIndex(),null);
      const count=app.fetches.length;assert.equal(await app.nav.loadWikiLinks(),null);assert.equal(app.fetches.length,count);
    """)


def test_disposal_during_large_reference_normalization_never_publishes_partial_data() -> None:
    _wiki(r"""
      // The transfer has completed when the router is disposed, in the normalizer's first pause.
      let app;app=await wikiApp({wikiFetch(_url,_settings,response){app.win.setTimeout(()=>app.nav.dispose(),0);return response();}});
      app.layerState.layer.references=Array.from({length:2050},()=>[0,1]);
      assert.equal(await settled(app.nav.loadWikiLinks()),null);assert.equal(app.events.length,0);
      assert.equal(app.nav.getWikiLinks(),null);assert.equal(app.nav.getIndex(),null);
    """)


def test_observer_disposal_cannot_return_a_live_layer_to_waiting_callers() -> None:
    _wiki(r"""
      let app;app=await wikiApp({onWikiLinks(){app.nav.dispose();}});
      assert.equal(await app.nav.loadWikiLinks(),null);assert.equal(app.nav.getWikiLinks(),null);
      assert.equal(app.events.length,1);assert.equal(app.nav.getIndex(),null);
      assert.equal(await app.nav.loadWikiLinks(),null);
    """)


def test_missing_crypto_and_legacy_text_responses_keep_main_navigation_honest() -> None:
    _wiki(r"""
      for(const extra of [{crypto:false},{transport:'text'}]) {
        const app=await wikiApp(extra);await app.nav.start();
        const result=await app.nav.loadWikiLinks();assertUnavailable(result);
        assert.equal(app.fetches.filter(url=>url===wikiURL).length,0,'An unverifiable layer must not be used');
        assert.equal((await app.nav.navigateNode('b')).nodeId,'b');assert.equal(app.errors.length,0);
        assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);app.nav.dispose();
      }
      // A page whose catalogue is not on this site has none.
      const absent=await wikiApp({initial:{...rootSpec,catalog:'https://outside.test/dependencies/index.json'}});
      await absent.nav.start();assertUnavailable(await absent.nav.loadWikiLinks());
      assert.deepEqual(absent.fetches,[absolute('dependencies.json')],'only the page\'s graph is read');
      assert.equal((await absent.nav.navigate(aSpec.route,{focus:true})).scope,'a');
      assert.equal(absent.errors.length,0);absent.nav.dispose();
    """)


def test_optional_callback_error_and_oversized_response_never_disable_math_navigation() -> None:
    _wiki(r"""
      const observer=await wikiApp({onWikiLinks(){throw new Error('/Users/private-provider/callback');}});
      const result=await observer.nav.loadWikiLinks();assert.equal(result.availability,'complete');
      assert.equal(observer.errors.length,0);assert.equal((await observer.nav.navigateNode('b')).nodeId,'b');observer.nav.dispose();
      // No size cap: a 65 MiB layer streams in whole.
      const large=await wikiApp({wikiFetch(){const text=JSON.stringify(large.layerState.layer)+' '.repeat(65*1024*1024);
        return new Response(text,{headers:{'content-length':String(Buffer.byteLength(text))}});}});
      await large.nav.start();const loaded=await large.nav.loadWikiLinks();assert.equal(loaded.availability,'complete');
      assert.equal(large.errors.length,0);assert.equal((await large.nav.navigateNode('a')).nodeId,'a');large.nav.dispose();
    """)


#: The map's reads: a read held until released (aborting rejects it, as a real fetch's would) and the map's files.
_MAP_READS = r"""
function held(settings, onRelease) {
  return new Promise((resolve, reject) => {
    settings.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), {once: true});
    onRelease(resolve);
  });
}
const mapFile = name => absolute('dependencies/map/data/' + name);
"""


def test_the_maps_reads_stay_out_of_the_page_cache_and_navigation_never_aborts_one() -> None:
    """The whole-project map keeps every file it reads, so the router keeps no copy: a map read takes none of the
    pages' cache budget, is read afresh each time, and a book navigation never cancels it."""
    run_router(_DATA + _MAP_READS + r"""
      const walk=walkBytes(),budget=walk+4*32+256;
      const part=mapFile('parts/big.json'),big={parts:{x:'y'.repeat(budget-walk)}},signals=[];let release;
      assert(size(big)<budget&&walk+size(big)>budget);
      const app=setup({cacheBytes:budget,fetch(url,settings,reply){ if(url!==part) return; signals.push(settings);
        if(signals.length===1) return held(settings,resolve=>{release=()=>resolve(reply(big,url));}); }});
      app.contents.set(part,big);
      await app.nav.start(); await app.nav.navigateNode('a');
      const reading=app.nav.fetchJSON(part); await until(()=>signals.length===1);
      await app.nav.navigateNode('b');
      assert.equal(signals[0].signal.aborted,false,'A book navigation never aborts a map read');
      release(); assert.deepEqual(await reading,big); assert.equal(signals[0].credentials,'same-origin');
      await app.nav.fetchJSON(part);
      assert.equal(app.fetches.filter(url=>url===part).length,2,'Read afresh: nothing kept a copy');
      await app.nav.navigateNode('a');
      assert.equal(app.fetches.filter(url=>url===aSpec.route).length,1,'Never stored there: the page read before it stays cached');
      assert.equal(app.errors.length,0); app.nav.dispose();
    """)


def test_the_maps_reads_keep_the_routers_origin_rules_and_errors_and_never_reach_on_error() -> None:
    run_router(_DATA + _MAP_READS + r"""
      const bad=mapFile('bad.json'),moved=mapFile('moved.json');
      const app=setup({fetch(url,_settings,reply){
        if(url===bad) return {ok:true,status:200,url,headers:{get(){return null;}},text:async()=>'{not json'};
        if(url===moved) return reply({},'https://outside.test/moved.json');
      }});
      await app.nav.start(); const before=app.fetches.length;
      await assert.rejects(app.nav.fetchJSON('https://outside.test/dependencies/map/data/index.json'),{message:'Refusing an external data request'});
      assert.equal(app.fetches.length,before,'An off-site read sends no request');
      await assert.rejects(app.nav.fetchJSON(moved),{message:'Data redirected outside this site'});
      await assert.rejects(app.nav.fetchJSON(mapFile('missing.json')),{message:'HTTP 404'});
      await assert.rejects(app.nav.fetchJSON(bad),{name:'SyntaxError'});
      assert.equal(app.errors.length,0,'The map shows its own errors; the book reports none of them'); app.nav.dispose();
    """)
