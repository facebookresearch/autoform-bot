"""Optional wiki references use the real navigation service and Node WebCrypto.

The DOM/network peers exercise transport and lifecycle, not rendered glyphs.
Every expected digest is made from the exact response bytes by native SHA-256.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui.test_integrated_navigation import _HARNESS as _NAVIGATION_PEERS

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for native wiki-link navigation tests")
_CLIENT = Path(__file__).resolve().parents[2] / "autoform_cli/ui/assets/blueprint-navigation.js"

_WIKI_PEERS = r"""
const {webcrypto} = require('node:crypto');
const catalogue = absolute('dependencies/index.json'), wikiURL = absolute('dependencies/wiki-links.json');
async function nativeSHA(bytes) {
  return Buffer.from(await webcrypto.subtle.digest('SHA-256', bytes)).toString('hex');
}
function streamed(bytes, status=200) {
  return new Response(new ReadableStream({start(controller) {
    // Split the BOM and JSON across real stream chunks, without text round-trips.
    controller.enqueue(bytes.slice(0,1)); controller.enqueue(bytes.slice(1,5));
    controller.enqueue(bytes.slice(5)); controller.close();
  }}), {status});
}
async function wikiApp(extra={}) {
  const app=setup(); app.nav.dispose();
  if(extra.crypto!==false) app.win.crypto=webcrypto;
  const catalog=extra.catalog || index();
  const encoded=new TextEncoder().encode(JSON.stringify(catalog));
  const bytes=extra.bom ? new Uint8Array([239,187,191,...encoded]) : encoded;
  const hash=await nativeSHA(bytes), events=[], indexEvents=[], signals=[];
  const state={layer:{schema:'autoform-wiki-links/v1',index_sha256:hash,availability:'complete',
    references:[[0,1],[1,2],[2,0]],unavailable_sources:[]}, status:200};
  if(extra.noCatalogue) app.initial.host.removeAttribute('data-catalog-src');
  function layerResponse() {
    return streamed(new TextEncoder().encode(JSON.stringify(state.layer)),state.status);
  }
  const fetch=async(url,settings)=>{
    app.fetches.push(url);
    if(url===catalogue) {
      if(extra.transport==='text') return app.reply(catalog,url);
      // WebKit hands a large body over as one chunk, here a view into a larger buffer.
      if(extra.transport==='single') return new Response(new ReadableStream({start(controller){
        const padded=new Uint8Array(bytes.length+8);padded.set(bytes,4);
        controller.enqueue(padded.subarray(4,4+bytes.length));controller.close();}}),{status:200});
      if(extra.transport==='arrayBuffer') return {ok:true,status:200,url,headers:{get(){return null;}},
        arrayBuffer:async()=>bytes.slice().buffer,text:async()=>{throw new Error('Raw bytes must not become text first');}};
      return streamed(bytes);
    }
    if(url===wikiURL) {
      signals.push(settings.signal);
      if(extra.wikiFetch) return extra.wikiFetch(url,settings,layerResponse);
      return layerResponse();
    }
    return app.reply(app.contents.get(url),url,app.contents.has(url)?200:404);
  };
  app.nav=app.win.AutoformNavigation.create({host:app.initial.host,window:app.win,document:app.initial.doc,fetch,
    onCommit(bundle){app.commits.push(bundle);},onError(error){app.errors.push(error);},
    onIndex(value){indexEvents.push(value);},onWikiLinks(value){events.push(value);if(extra.onWikiLinks)extra.onWikiLinks(value);},
    captureState(){return app.getState();},restoreState(saved){app.state(saved);},
    jsonLimit:extra.jsonLimit,cacheBytes:extra.cacheBytes,timeout:extra.timeout});
  return Object.assign(app,{layerState:state,events,indexEvents,signals,indexBytes:bytes,indexHash:hash,layerResponse});
}
function assertUnavailable(value) {
  assert(value); assert.equal(value.schema,'autoform-wiki-links/v1');
  assert.equal(value.availability,'unavailable');assert.deepEqual(value.references,[]);
  assert.equal(typeof value.reason,'string');
  assert(!JSON.stringify(value).includes('/Users/'));assert(!JSON.stringify(value).includes('private-provider'));
}
"""


def _run(body: str) -> None:
    script = "const CLIENT=" + json.dumps(str(_CLIENT)) + ";\n" + _NAVIGATION_PEERS + _WIKI_PEERS
    script += "\n(async()=>{\n" + body + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_wiki_layer_is_lazy_coalesced_sha_bound_and_keeps_math_relations() -> None:
    _run(r"""
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
    _run(r"""
      // Like a real fetch, aborting the request's signal errors its body.
      function paced(bytes,gaps,signal) {
        const size=Math.ceil(bytes.length/gaps.length);
        return new Response(new ReadableStream({async start(controller){
          signal.addEventListener('abort',()=>{try{controller.error(new DOMException('Aborted','AbortError'));}catch(_closed){}});
          for(let part=0;part<gaps.length;part++){
            await new Promise(resolve=>setTimeout(resolve,gaps[part]));
            try{controller.enqueue(bytes.slice(part*size,(part+1)*size));}catch(_cancelled){return;}
          }
          try{controller.close();}catch(_cancelled){}
        }}),{status:200});
      }
      const layer=app=>new TextEncoder().encode(JSON.stringify(app.layerState.layer));
      // Three chunks 600 ms apart take 1.8 s in all, past the 1 s timeout, and still load.
      const steady=await wikiApp({timeout:1000,wikiFetch(_url,settings){return paced(layer(steady),[600,600,600],settings.signal);}});
      await steady.nav.start();assert.equal((await steady.nav.loadWikiLinks()).availability,'complete');steady.nav.dispose();
      // A transfer that stops sending for longer than the timeout is reported as timed out.
      const stalled=await wikiApp({timeout:1000,wikiFetch(_url,settings){return paced(layer(stalled),[0,1600],settings.signal);}});
      await stalled.nav.start();const late=await stalled.nav.loadWikiLinks();
      assertUnavailable(late);assert.equal(late.reason,'request_timed_out');stalled.nav.dispose();
    """)


@pytest.mark.parametrize("transport", ("stream", "single", "arrayBuffer"))
def test_index_digest_is_original_bom_bytes_not_decoded_text(transport: str) -> None:
    _run("const transport=" + json.dumps(transport) + ";\n" + r"""
      const app=await wikiApp({bom:true,transport});await app.nav.start();
      const normalizedBytes=new TextEncoder().encode(new TextDecoder().decode(app.indexBytes));
      assert.notEqual(await nativeSHA(normalizedBytes),app.indexHash,'The BOM makes this fidelity assertion meaningful');
      const result=await app.nav.loadWikiLinks();assert.equal(result.availability,'complete');
      assert.equal(result.index_sha256,app.indexHash);assert.equal(app.nav.getIndex().nodes[0].id,'roadmap');
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


_READ_EVENTS = r"""
// Record the index's decode and parse, and each 0 ms timer: without animation frames, a pause is one.
// Each decode advances the clock by SLOW ms. Each read of a body advances it by 20 ms, which is
// waiting for the network, not work.
const text=new TextDecoder().decode(app.indexBytes),events=[];let now=0;app.win.performance={now:()=>now};
const nativeTimer=global.setTimeout,nativeClear=global.clearTimeout,nativeParse=JSON.parse,nativeDecode=TextDecoder.prototype.decode;
const nativeRead=ReadableStreamDefaultReader.prototype.read;
global.setTimeout=(callback,delay,...args)=>{if(delay===0)events.push('pause');return nativeTimer(callback,delay,...args);};
TextDecoder.prototype.decode=function(...args){const value=nativeDecode.apply(this,args);now+=SLOW;if(value===text)events.push('decode');return value;};
ReadableStreamDefaultReader.prototype.read=function(...args){now+=20;return nativeRead.apply(this,args);};
JSON.parse=function(value,...rest){if(value===text)events.push('parse');return nativeParse.call(this,value,...rest);};
function restore(){global.setTimeout=nativeTimer;global.clearTimeout=nativeClear;JSON.parse=nativeParse;
  TextDecoder.prototype.decode=nativeDecode;ReadableStreamDefaultReader.prototype.read=nativeRead;}
"""


@pytest.mark.parametrize("transport", ("stream", "single"))
@pytest.mark.parametrize(("slow", "expected"), ((25, ["decode", "pause", "parse"]), (24, ["decode", "parse"])))
def test_a_slow_index_read_pauses_before_the_parse(transport: str, slow: int, expected: list[str]) -> None:
    _run(f"const transport={json.dumps(transport)},SLOW={slow},expected={json.dumps(expected)};\n" + r"""
      const app=await wikiApp({transport});
    """ + _READ_EVENTS + r"""
      try {
        assert.equal((await app.nav.loadIndex()).nodes.length,3);assert.deepEqual(events,expected);
        // Only the index pauses: the wiki layer's decode goes straight on.
        assert.equal((await app.nav.loadWikiLinks()).availability,'complete');assert.deepEqual(events,expected);
      } finally {restore();}
      assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_a_visible_page_paints_two_frames_between_reading_and_parsing_the_index() -> None:
    _run("const SLOW=25;\nconst app=await wikiApp();\n" + _READ_EVENTS + r"""
      const frames=[];app.initial.doc.visibilityState='visible';app.win.cancelAnimationFrame=()=>{};
      app.win.requestAnimationFrame=callback=>{events.push('frame');frames.push(callback);return frames.length;};
      // Hold the 50 ms fallback, so only the frames can end the pause.
      global.setTimeout=(callback,delay,...args)=>delay===50?{held:true}:nativeTimer(callback,delay,...args);
      global.clearTimeout=token=>{if(!(token&&token.held))nativeClear(token);};
      try {
        const loading=app.nav.loadIndex();await until(()=>events.length===2);
        assert.deepEqual(events,['decode','frame']);frames[0](16);assert.deepEqual(events,['decode','frame','frame']);
        frames[1](32);assert.equal((await loading).nodes.length,3);assert.deepEqual(events,['decode','frame','frame','parse']);
      } finally {restore();}
      assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_a_single_chunk_index_is_hashed_and_decoded_where_it_arrived() -> None:
    _run(r"""
      // The single transport's chunk is a view at offset 4 into a buffer 8 bytes longer than the index.
      const app=await wikiApp({transport:'single'}),hashed=[],decoded=[],nativeDecode=TextDecoder.prototype.decode;
      app.win.crypto={subtle:{digest(algorithm,data){hashed.push(data);return webcrypto.subtle.digest(algorithm,data);}}};
      TextDecoder.prototype.decode=function(input,...rest){decoded.push(input);return nativeDecode.call(this,input,...rest);};
      try {assert.equal((await app.nav.loadIndex()).nodes.length,3);} finally {TextDecoder.prototype.decode=nativeDecode;}
      assert.equal(hashed.length,1);assert.equal(hashed[0].byteOffset,4);
      assert.equal(hashed[0].buffer.byteLength,app.indexBytes.length+8);assert.strictEqual(decoded[0],hashed[0]);
      assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_literal_zero_id_and_reference_rows_are_not_reinterpreted_as_offsets() -> None:
    _run(r"""
      const catalog=index();catalog.nodes.push({id:'0',title:'Literal zero',kind:'node',scope:'roadmap',route:'../roadmap/zero.html'});
      const app=await wikiApp({catalog});app.layerState.layer.references=[[0,3],[3,1],[3,1]];
      const result=await app.nav.loadWikiLinks();assert.equal(result.availability,'complete');
      assert.deepEqual(result.references,[['roadmap','0'],['0','a'],['0','a']]);
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);app.nav.dispose();
    """)


def test_view_order_changes_cannot_rebind_original_index_offsets() -> None:
    _run(r"""
      const app=await wikiApp();await app.nav.loadIndex();app.nav.getIndex().nodes.reverse();
      const result=await app.nav.loadWikiLinks();assert.equal(result.availability,'complete');
      assert.deepEqual(result.references,[['roadmap','a'],['a','b'],['b','roadmap']]);
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);app.nav.dispose();
    """)


def test_partial_source_coverage_is_retained_without_leaking_publisher_reason() -> None:
    _run(r"""
      const app=await wikiApp();Object.assign(app.layerState.layer,{availability:'partial',references:[[1,2]],
        unavailable_sources:[2],reason:'/Users/private-provider/hidden-source.md'});
      const result=await app.nav.loadWikiLinks();assert.equal(result.availability,'partial');
      assert.deepEqual(result.references,[['a','b']]);assert.deepEqual(result.unavailable_sources,['b']);
      assert(!JSON.stringify(result).includes('/Users/'));assert(!JSON.stringify(result).includes('hidden-source.md'));
      assert.equal(app.events.length,1);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_verified_empty_complete_layer_differs_from_missing_or_unavailable() -> None:
    _run(r"""
      const app=await wikiApp();app.layerState.layer.references=[];
      const result=await app.nav.loadWikiLinks();assert.equal(result.availability,'complete');
      assert.deepEqual(result.references,[]);assert.equal(result.index_sha256,app.indexHash);
      const unavailable=await wikiApp();Object.assign(unavailable.layerState.layer,{availability:'unavailable',references:[],
        unavailable_sources:[0,1,2],reason:'some source paths were unsupported'});
      const unknown=await unavailable.nav.loadWikiLinks();assertUnavailable(unknown);
      assert.deepEqual(unknown.unavailable_sources,['roadmap','a','b']);app.nav.dispose();unavailable.nav.dispose();
    """)


def test_missing_optional_asset_retries_without_breaking_canonical_navigation() -> None:
    _run(r"""
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
    _run(r"""
      const app=await wikiApp();await app.nav.start();
      app.layerState.layer.index_sha256=await nativeSHA(new TextEncoder().encode('Different real index bytes'));
      const stale=await app.nav.loadWikiLinks();assertUnavailable(stale);assert.equal(app.errors.length,0);
      app.layerState.layer.index_sha256=app.indexHash;
      const corrected=await app.nav.loadWikiLinks();assert.equal(corrected.availability,'complete');
      assert.equal(app.fetches.filter(url=>url===wikiURL).length,2);assert.equal(app.events.length,2);
      assert.equal((await app.nav.navigateNode('a')).nodeId,'a');assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_invalid_json_is_unavailable_and_retry_does_not_reuse_corrupt_cache() -> None:
    _run(r"""
      let corrupt=true;const app=await wikiApp({wikiFetch(_url,_settings,response){return corrupt ? new Response('{') : response();}});
      await app.nav.start();assertUnavailable(await app.nav.loadWikiLinks());
      assert.equal(app.errors.length,0);corrupt=false;
      assert.equal((await app.nav.loadWikiLinks()).availability,'complete');
      assert.equal(app.fetches.filter(url=>url===wikiURL).length,2);assert.equal(app.events.length,2);
      assert.equal((await app.nav.navigateNode('b')).nodeId,'b');assert.equal(app.errors.length,0);app.nav.dispose();
    """)


@pytest.mark.parametrize("kind", ("schema", "references", "row", "string_offset", "boolean_offset", "negative",
                                  "too_large", "fraction", "unavailable_array", "unavailable_offset", "availability",
                                  "inconsistent_complete", "reason"))
def test_malformed_optional_layer_fails_atomically_and_leaves_index_usable(kind: str) -> None:
    _run("const kind=" + json.dumps(kind) + ";\n" + r"""
      const app=await wikiApp();await app.nav.start();const raw=app.layerState.layer;
      if(kind==='schema')raw.schema='unsupported';
      if(kind==='references')raw.references={};
      if(kind==='row')raw.references=[[0,1,2]];
      if(kind==='string_offset')raw.references=[['0',1]];
      if(kind==='boolean_offset')raw.references=[[false,1]];
      if(kind==='negative')raw.references=[[-1,1]];
      if(kind==='too_large')raw.references=[[0,3]];
      if(kind==='fraction')raw.references=[[0,.5]];
      if(kind==='unavailable_array')raw.unavailable_sources={};
      if(kind==='unavailable_offset')raw.unavailable_sources=[true];
      if(kind==='availability')raw.availability='assumed-complete';
      if(kind==='inconsistent_complete')raw.unavailable_sources=[1];
      if(kind==='reason')raw.reason={private:'/Users/private-provider/source'};
      const result=await app.nav.loadWikiLinks();assertUnavailable(result);
      assert.equal(app.events.length,1);assert.strictEqual(app.events[0],result);
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);
      assert.equal((await app.nav.navigateNode('b')).nodeId,'b');assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_invalid_late_row_never_exposes_partially_normalized_references() -> None:
    _run(r"""
      const app=await wikiApp();app.layerState.layer.references=Array.from({length:2050},()=>[0,1]);
      app.layerState.layer.references.push([1,-1]);
      const result=await app.nav.loadWikiLinks();assertUnavailable(result);
      assert.equal(app.events.length,1);assert.deepEqual(app.events[0].references,[]);
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);app.nav.dispose();
    """)


def test_disposal_aborts_optional_request_and_ignores_its_late_result() -> None:
    _run(r"""
      let release,observed;const requested=new Promise(resolve=>observed=resolve);
      const app=await wikiApp({wikiFetch(_url,_settings,response){observed();return new Promise(resolve=>release=()=>resolve(response()));}});
      const pending=app.nav.loadWikiLinks();await requested;
      app.nav.dispose();assert.equal(app.signals.length,1);assert.equal(app.signals[0].aborted,true);
      assert.equal(await pending,null);release();await new Promise(resolve=>setImmediate(resolve));
      assert.equal(app.nav.getWikiLinks(),null);
      assert.equal(app.events.length,0);assert.equal(app.nav.getIndex(),null);
      const count=app.fetches.length;assert.equal(await app.nav.loadWikiLinks(),null);assert.equal(app.fetches.length,count);
    """)


def test_disposal_during_large_reference_normalization_never_publishes_partial_data() -> None:
    _run(r"""
      let app;app=await wikiApp({wikiFetch(_url,_settings,response){setTimeout(()=>app.nav.dispose(),0);return response();}});
      app.layerState.layer.references=Array.from({length:2050},()=>[0,1]);
      assert.equal(await app.nav.loadWikiLinks(),null);assert.equal(app.events.length,0);
      // Transport has already completed; verify disposal of the in-flight
      // normalizer, not an abort flag on the detached finished request.
      assert.equal(app.nav.getWikiLinks(),null);assert.equal(app.nav.getIndex(),null);
    """)


def test_observer_disposal_cannot_return_a_live_layer_to_waiting_callers() -> None:
    _run(r"""
      let app;app=await wikiApp({onWikiLinks(){app.nav.dispose();}});
      assert.equal(await app.nav.loadWikiLinks(),null);assert.equal(app.nav.getWikiLinks(),null);
      assert.equal(app.events.length,1);assert.equal(app.nav.getIndex(),null);
      assert.equal(await app.nav.loadWikiLinks(),null);
    """)


def test_missing_crypto_and_legacy_text_responses_keep_main_navigation_honest() -> None:
    _run(r"""
      for(const extra of [{crypto:false},{transport:'text'}]) {
        const app=await wikiApp(extra);await app.nav.start();
        const result=await app.nav.loadWikiLinks();assertUnavailable(result);
        assert.equal(app.fetches.filter(url=>url===wikiURL).length,0,'An unverifiable layer must not be used');
        assert.equal((await app.nav.navigateNode('b')).nodeId,'b');assert.equal(app.errors.length,0);
        assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);app.nav.dispose();
      }
      const absent=await wikiApp({noCatalogue:true});await absent.nav.start();
      assertUnavailable(await absent.nav.loadWikiLinks());assert.equal(absent.fetches.includes(wikiURL),false);
      assert.equal((await absent.nav.navigate(aSpec.route,{focus:true})).scope,'a');
      assert.equal(absent.errors.length,0);absent.nav.dispose();
    """)


def test_optional_callback_error_and_oversized_response_never_disable_math_navigation() -> None:
    _run(r"""
      const observer=await wikiApp({onWikiLinks(){throw new Error('/Users/private-provider/callback');}});
      const result=await observer.nav.loadWikiLinks();assert.equal(result.availability,'complete');
      assert.equal(observer.errors.length,0);assert.equal((await observer.nav.navigateNode('b')).nodeId,'b');observer.nav.dispose();
      // No size cap: a 65 MiB layer, past every limit the client had, streams in whole, and the retired jsonLimit option caps nothing.
      const large=await wikiApp({jsonLimit:1024,wikiFetch(){const text=JSON.stringify(large.layerState.layer)+' '.repeat(65*1024*1024);
        return new Response(text,{headers:{'content-length':String(Buffer.byteLength(text))}});}});
      await large.nav.start();const loaded=await large.nav.loadWikiLinks();assert.equal(loaded.availability,'complete');
      assert.equal(large.errors.length,0);assert.equal((await large.nav.navigateNode('a')).nodeId,'a');large.nav.dispose();
    """)
