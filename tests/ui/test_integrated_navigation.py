"""The router, ``blueprint-navigation.js`` run whole on real pages (:func:`tests.explorer_fixture.run_router`): canonical
and alias routes, Home, the reading it hands over, reader links, and the history snapshots it keeps and quarantines."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from autoform_cli.ui import graph_pages
from tests.explorer_fixture import run_router

_HELPERS = r"""
// The math source pageHTML writes on every reading.
const TEX = String.raw`\(L \neq 0\)`;
// A scope the index lists with only a summary, and its 42-row graph: its reading is its published page.
function addLargeScope(app) {
  const spec = {title: 'Large scope', route: absolute('roadmap/large.html'), data: '../dependencies/large.json', scope: 'large'};
  const catalog = index();
  catalog.nodes.push({id: 'large', title: 'Large scope', kind: 'scope', result: false, url: '../roadmap/large.html',
    route: '../roadmap/large.html', data_url: 'large.json', scope: 'large', summary: 'Only a summary'});
  app.contents.set(absolute('dependencies/index.json'), catalog); app.contents.set(spec.route, spec);
  app.contents.set(absolute('dependencies/large.json'), {...graph, title: 'Large scope', edges: [],
    nodes: Array.from({length: 42}, (_, n) => ({id: 'large-' + n, title: 'Scope item ' + n, kind: 'node', status: 'planned',
      area: n < 21 ? 'Authored area A' : 'Authored area B', member_count: 1, status_counts: [['planned', 1]]}))});
  return spec;
}
// The browser lands on an entry that carries state, as Back or Forward onto it would.
function arrive(app, state, url) {
  app.win.history.pushState(state, '', url);
  app.win.dispatchEvent(new app.win.PopStateEvent('popstate', {state}));
}
// A reader follows a link to a section of the page the browser shows: its own fragment navigation fires popstate.
async function follow(app, url) { app.win.location.href = url; await tick(0); }
// The renderer puts a committed reading in the page, where a reader clicks its links.
function mount(app, bundle) { app.initial.root.append(bundle.readingSource); return bundle.readingSource; }
"""

#: Markup with everything the router cleans from a reading: active content it removes, handlers, links it rebases
#: onto the reading's page or drops, links it keeps, and what MathJax and Mermaid drew.
_DIRTY = """\
<p onmouseover="bad()">Prose with <em onclick="bad()">emphasis</em>, <a id="relative" href="c.html#part">a link</a>,
<a id="script" href="javascript:bad()">a script</a>, <a id="outside" href="https://outside.test/paper.pdf">a paper</a>,
<a id="mail" href="mailto:author@example.test">mail</a>, <a id="phone" href="tel:+15550100">a phone</a>,
<a id="data" href="data:image/png;base64,AAAA">data</a>,
<span id="framed" srcdoc="&lt;script&gt;bad()&lt;/script&gt;">framed</span></p>
<iframe src="frame.html"></iframe><object data="object.swf"></object><embed src="embed.swf">
<base href="https://outside.test/"><link rel="stylesheet" href="style.css"><style>p{color:red}</style><script>bad()</script>
<meta http-equiv="refresh" content="0; url=elsewhere.html">
<img id="plot" src="figures/plot.png" srcset="figures/plot.png 1x, figures/plot@2x.png 2x, javascript:bad() 3x,
 data:image/png;base64,AAAA 4x, figures/wide.png 640w, figures/odd.png huge">
<img id="bare" srcset="figures/bare.png, figures/bare@2x.png 2x">
<img id="unsafe" src="javascript:bad()" srcset="javascript:bad() 1x"><img id="inline" src="data:image/png;base64,AAAA">
<video id="clip" src="clip.mp4" poster="javascript:bad()"></video><video id="film" poster="figures/still.png"></video>
<form id="form" action="submit.html"><button id="results" formaction="results.html">Show</button>
<input id="send" type="submit" formaction="javascript:void(0)"></form>
<svg><use id="glyph" href="#symbol"></use><a id="drawn" xlink:href="d.html">a diagram link</a>
<a id="jump" href="#part">a link to a part</a><image id="still" href="#frame"/></svg>
<mjx-container class="MathJax"><a href="e.html" onclick="bad()">typeset</a><style>mjx-c{}</style></mjx-container>
<div class="mermaid"><svg><style>#node rect{fill:#eef}</style><a href="f.html" onclick="bad()">node</a></svg></div>
"""


def _route(body: str, **args: object) -> str:
    return run_router(_HELPERS + body, args=args)


def test_a_body_that_never_finishes_fails_instead_of_passing_without_assertions() -> None:
    with pytest.raises(AssertionError, match="the test body never finished"):
        run_router("await new Promise(() => {}); assert.fail('unreachable');")


def test_canonical_node_navigation_keeps_the_complete_published_reading() -> None:
    _route(r"""
      const app=setup(); await app.nav.start(); const bundle=await app.nav.navigateNode('a');
      assert.equal(bundle.url,absolute('roadmap/a.html#theorem-a'));
      assert.equal(bundle.title,'A theorem'); assert.equal(bundle.scope,'a'); assert.equal(bundle.anchor,'theorem-a');
      assert(bundle.readingSource.textContent.includes('Complete source statement and proof.'));
      assert(!bundle.readingSource.textContent.includes('Not the complete article'));
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);
      const length=app.win.history.length; await app.nav.navigateNode('a'); assert.equal(app.win.history.length,length);
      app.nav.dispose();
    """)


def test_a_fetched_reading_is_copied_clean_with_every_link_rebased_onto_its_page() -> None:
    # The page sits in another folder than the one the browser shows, so only its own address rebases its links. The
    # rules are checked together, so a failure names every rule broken.
    _route(r"""
      const app=setup(),notes=absolute('roadmap/chapter/notes.html'),at=path=>absolute('roadmap/chapter/'+path);
      app.contents.set(notes,{title:'Notes',route:notes,data:'../../dependencies/a.json',scope:'notes',extra:args.dirty});
      await app.nav.start(); const reading=(await app.nav.navigate(notes)).readingSource;
      const $=selector=>reading.querySelector(selector), attribute=(selector,name)=>$(selector)?$(selector).getAttribute(name):'no '+selector;
      const handlers=Array.from(reading.querySelectorAll('*')).flatMap(node=>node.getAttributeNames().filter(name=>/^on/.test(name)));
      const rules=[
        ['the reading itself loses its handler',reading.getAttribute('onclick'),null],
        ['and keeps its other attributes',reading.getAttribute('data-autoform-math-source'),TEX],
        ...['script','iframe','object','embed','base','link','style','meta'].map(tag=>['no '+tag+' is copied, not even from what MathJax or Mermaid drew',$(tag),null]),
        ['no handler is copied',handlers.join(),''],
        ['no srcdoc is copied',attribute('#framed','srcdoc'),null],
        ['a relative link is rebased onto its page',attribute('#relative','href'),at('c.html#part')],
        ['a javascript: link is dropped',attribute('#script','href'),null],
        ['an outside link is left alone',attribute('#outside','href'),'https://outside.test/paper.pdf'],
        ['a mail link is kept',attribute('#mail','href'),'mailto:author@example.test'],
        ['a telephone link is kept',attribute('#phone','href'),'tel:+15550100'],
        ['a data: link is dropped',attribute('#data','href'),null],
        ['an image source is rebased',attribute('#plot','src'),at('figures/plot.png')],
        ['a javascript: source is dropped',attribute('#unsafe','src'),null],
        ['an inline image is kept',attribute('#inline','src'),'data:image/png;base64,AAAA'],
        ['a video source is rebased',attribute('#clip','src'),at('clip.mp4')],
        ['a javascript: poster is dropped',attribute('#clip','poster'),null],
        ['a relative poster is rebased',attribute('#film','poster'),at('figures/still.png')],
        ['a form action is rebased',attribute('#form','action'),at('submit.html')],
        // A button's formaction stands in for its form's action, so it is treated as an action is.
        ['a relative formaction is rebased',attribute('#results','formaction'),at('results.html')],
        ['a javascript: formaction is dropped',attribute('#send','formaction'),null],
        ['an SVG link is rebased',attribute('#drawn','xlink:href'),at('d.html')],
        ['an SVG reference into the drawing itself is kept',attribute('#glyph','href'),'#symbol'],
        // An SVG link or image naming a part of the page is a link to that page, as an HTML one is.
        ['an SVG link to a part of the page is rebased onto it',attribute('#jump','href'),at('notes.html#part')],
        ['an SVG image naming a part of the page is rebased onto it',attribute('#still','href'),at('notes.html#frame')],
        ['unsafe or malformed srcset candidates are dropped and the rest rebased',attribute('#plot','srcset'),
          [at('figures/plot.png')+' 1x',at('figures/plot@2x.png')+' 2x','data:image/png;base64,AAAA 4x',at('figures/wide.png')+' 640w'].join(', ')],
        ['a srcset candidate without a descriptor is kept',attribute('#bare','srcset'),at('figures/bare.png')+', '+at('figures/bare@2x.png')+' 2x'],
        ['a srcset with no candidate left is dropped',attribute('#unsafe','srcset'),null],
        // A copy holds nothing MathJax or Mermaid drew in this page, so it is cleaned throughout.
        ['a link MathJax drew is rebased in a copy',attribute('mjx-container a','href'),at('e.html')],
        ['a link Mermaid drew is rebased in a copy',attribute('.mermaid a','href'),at('f.html')]];
      const broken=rules.filter(([,actual,expected])=>actual!==expected).map(([rule])=>rule);
      assert.deepEqual(broken,[],broken.join('; '));
      // A reading that is itself active content is refused.
      const framed=absolute('roadmap/framed.html');
      app.contents.set(framed,'<!doctype html><main class="bp-explorer-page"><iframe class="bp-reading-source"></iframe>'+
        '<div class="bp-dag-viewer" data-page-href="framed.html" data-graph-src="../dependencies/a.json" data-scope-id="a"></div></main>');
      assert.equal(await app.nav.navigate(framed),null,'an iframe reading is not handed over');
      assert.equal(app.errors.at(-1).message,'Invalid mathematical reading element');
      app.nav.dispose();
    """, dirty=_DIRTY)


def test_the_loaded_page_hands_over_its_own_reading_sanitized_in_place() -> None:
    _route(r"""
      const page={...rootSpec,extra:args.dirty},app=setup({initial:page}),reading=app.initial.reading;
      const drawn=Array.from(reading.querySelectorAll('mjx-container, .mermaid')),before=drawn.map(node=>node.outerHTML);
      const opened=await app.nav.start();
      assert.strictEqual(opened.readingSource,reading);
      assert.equal(reading.getAttribute('onclick'),null); assert.equal(reading.querySelector('em').getAttribute('onclick'),null);
      assert.equal(reading.querySelector('#relative').getAttribute('href'),absolute('roadmap/c.html#part'));
      const active=Array.from(reading.querySelectorAll('script,iframe,object,embed,base,link,style'));
      assert.deepEqual(active.map(node=>drawn.indexOf(node.closest('mjx-container, .mermaid'))),[0,1],'only the drawn styles stay');
      assert.deepEqual(drawn.map(node=>node.outerHTML),before,'what MathJax and Mermaid drew is left as they drew it');
      // Coming back reads a copy, which is cleaned throughout, drawn math included.
      app.contents.set(rootSpec.route,page); await app.nav.navigate(aSpec.route); const again=await app.nav.navigate(rootSpec.route);
      assert.notStrictEqual(again.readingSource,reading); assert(app.fetches.includes(rootSpec.route));
      const copied=again.readingSource.querySelector('mjx-container a');
      assert.equal(copied.getAttribute('onclick'),null); assert.equal(copied.getAttribute('href'),absolute('roadmap/e.html'));
      app.nav.dispose();
    """, dirty=_DIRTY)


def test_the_element_the_address_names_stays_behind_and_its_copy_takes_its_children() -> None:
    _route(r"""
      const app=setup({url:rootSpec.route+'#theorem-main'}),reading=app.initial.reading;
      const glyph=element(app.win,'mjx-container',{},'0=0'),formula=element(app.win,'span',{class:'arithmatex'},glyph);
      const named=element(app.win,'h2',{id:'theorem-main',class:'bp-thmheading',onclick:'bad()'},element(app.win,'span',{},'Main'),formula);
      reading.firstElementChild.after(named);
      const opened=await app.nav.start();
      assert.strictEqual(opened.readingSource,reading,'the reading itself is still handed over');
      const copy=reading.children[1];
      assert.notStrictEqual(copy,named); assert(!app.doc.contains(named),'the named element is left detached');
      assert.deepEqual(Object.fromEntries(Array.from(copy.attributes,item=>[item.name,item.value])),{id:'theorem-main',class:'bp-thmheading'});
      assert.equal(named.childNodes.length,0); assert.strictEqual(formula.parentNode,copy);
      assert(copy.contains(glyph),'its typeset formulas move into the copy');
      app.nav.dispose();
      // A reading that is itself the named element is replaced by its copy where it stood.
      const whole=setup({url:rootSpec.route+'#whole'}),own=whole.initial.reading,heading=own.querySelector('h1'); own.id='whole';
      const moved=(await whole.nav.start()).readingSource;
      assert.notStrictEqual(moved,own); assert(!whole.doc.contains(own)); assert(whole.initial.root.contains(moved));
      assert.equal(moved.getAttribute('class'),'bp-reading-source'); assert.strictEqual(moved.querySelector('h1'),heading);
      whole.nav.dispose();
      // What MathJax drew is left as it drew it, even when the address names a part of it.
      const inner=setup({url:rootSpec.route+'#mjx-eqn:1'}),part=element(inner.win,'mjx-mtd',{id:'mjx-eqn:1'});
      const typeset=element(inner.win,'mjx-container',{},part); inner.initial.reading.append(typeset);
      await inner.nav.start(); assert.strictEqual(part.parentNode,typeset); assert(inner.doc.contains(part));
      inner.nav.dispose();
    """)


def test_scope_map_first_transport_is_independent_of_explicit_reading_focus() -> None:
    _route(r"""
      const app=setup(),spec=addLargeScope(app);await app.nav.start();
      const mapped=await app.nav.navigateNode('large',{history:'push',focus:'reading',mapFirst:true});
      assert.equal(mapped.url,spec.route);assert.equal(mapped.nodeId,'large');assert.equal(mapped.scope,'large');
      assert.equal(mapped.focus,true);assert.equal(mapped.mapFirst,true);assert.equal(mapped.graph.nodes.length,42);
      assert(mapped.readingSource.textContent.includes('Complete source statement and proof.'));
      assert(!mapped.readingSource.textContent.includes('Only a summary'));
      assert.equal(mapped.readingSource.getAttribute('data-autoform-math-source'),TEX);
      const reading=await app.nav.navigateNode('large',{focus:true,mapFirst:false});
      assert.equal(reading.focus,true);assert.equal(reading.mapFirst,false);assert.equal(reading.url,spec.route);
      const result=await app.nav.navigateNode('b');assert.equal(result.focus,true);assert.equal(result.mapFirst,false);
      const strict=await app.nav.navigateNode('large',{focus:true,mapFirst:1});
      assert.equal(strict.mapFirst,false,'A truthy non-boolean cannot acquire map-first intent');
      assert.equal(strict.focus,true);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_reading_links_to_large_scopes_request_reading_focus_and_leave_external_links_native() -> None:
    _route(r"""
      const app=setup(),spec=addLargeScope(app);await app.nav.start();
      const root=mount(app,await app.nav.navigateNode('a')),scopeLink=element(app.win,'a',{href:spec.route},'Large scope');
      root.append(scopeLink);app.nav.bindReader(root);
      const requests=app.fetches.length,commits=app.commits.length;
      for(const settings of [{metaKey:true},{ctrlKey:true},{shiftKey:true},{altKey:true},{button:1}])
        assert.equal(click(scopeLink,settings),false,JSON.stringify(settings));
      const external=root.querySelector('a[href="https://outside.test/paper.pdf"]');
      assert(external);assert.equal(click(external),false);
      assert.equal(app.fetches.length,requests);assert.equal(app.commits.length,commits);
      let reached=false;scopeLink.addEventListener('click',()=>{reached=true;});
      assert.equal(click(scopeLink),true);assert.equal(reached,false,'the router keeps the click it takes');
      await until(()=>app.commits.at(-1).nodeId==='large');const bundle=app.commits.at(-1);
      assert.equal(bundle.focus,true,'A scope reading link must not inherit an overview-default focus=false');
      assert.equal(bundle.mapFirst,false);assert.equal(bundle.url,spec.route);assert.equal(bundle.graph.nodes.length,42);
      assert(bundle.readingSource.textContent.includes('Complete source statement and proof.'));
      assert.equal(bundle.readingSource.getAttribute('data-autoform-math-source'),TEX);
      assert.equal(app.errors.length,0);app.nav.dispose();assert.equal(click(scopeLink),false,'disposal unbinds the reading');
    """)


def test_delayed_old_response_cannot_commit_or_replace_newer_navigation() -> None:
    _route(r"""
      let release, requested=false;
      const app=setup({fetch(url,_settings,reply){if(url===aSpec.route){requested=true;return new Promise(resolve=>release=()=>resolve(reply(aSpec,url)));}}});
      await app.nav.start(); const old=app.nav.navigateNode('a'); await until(()=>requested);
      const newer=await app.nav.navigateNode('b'); release(); await old;
      assert.equal(newer.nodeId,'b'); assert.equal(app.commits.at(-1).nodeId,'b');
      assert.equal(app.win.location.href,absolute('roadmap/b.html#theorem-b')); assert.equal(app.errors.length,0);
      app.nav.dispose();
    """)


def test_a_superseded_renderer_failing_late_cannot_fail_the_view_that_replaced_it() -> None:
    _route(r"""
      // A's renderer is still at work when B replaces it: its late failure is not B's.
      let rejectA;const app=setup({commit(bundle){if(bundle.nodeId==='a')return new Promise((_resolve,reject)=>{rejectA=reject;});}});
      await app.nav.start();const opening=app.nav.navigateNode('a');await until(()=>rejectA);
      assert(await app.nav.navigateNode('b'));rejectA(new Error('The superseded renderer failed late'));
      assert.equal(await opening,null);assert.equal(app.errors.length,0);
      assert.equal(app.win.location.href,bSpec.route+'#theorem-b');
      app.state({mode:'graph',readingScroll:5});assert.deepEqual(app.nav.checkpoint(),{mode:'graph',readingScroll:5});
      app.nav.dispose();
    """)


def test_checkpoint_and_back_restore_opaque_shell_state_without_new_urls() -> None:
    _route(r"""
      const app=setup(); await app.nav.start(); await app.nav.navigateNode('a');
      app.state({mode:'sources',camera:{x:12,y:24,scale:1.2},readingScroll:80});
      const url=app.win.location.href,length=app.win.history.length;
      app.nav.checkpoint(); assert.equal(app.win.location.href,url); assert.equal(app.win.history.length,length);
      assert.equal(app.win.history.state.material,'preserved');
      await app.nav.navigateNode('b'); app.state({mode:'map',readingScroll:0}); await app.back();
      await until(()=>app.restored.length>0);
      assert.equal(app.win.location.href,url); assert.equal(app.getState().mode,'sources');
      assert.equal(app.getState().readingScroll,80); assert.equal(app.getState().camera.x,12);
      const before=app.win.history.length; app.nav.checkpoint('push');
      // A deliberate action after Back truncates the forward branch before adding its entry.
      assert.equal(app.win.history.length,before); assert.equal(app.win.location.href,url);
      app.nav.dispose();
    """)


def test_back_then_forward_captures_outgoing_edits_without_waiting_for_a_checkpoint() -> None:
    _route(r"""
      const app=setup(); await app.nav.start(); await app.nav.navigateNode('a');
      app.state({mode:'sources',camera:{x:12,y:24,scale:1.2},readingScroll:80,outlineOpen:false});
      await app.nav.navigateNode('b');
      const bKey=app.win.history.state.autoformNavigation.key,length=app.win.history.length;
      const edited={mode:'list',camera:{x:190,y:-55,scale:.9},readingScroll:340,outlineScroll:120,
        query:'changed before Back',outlineOpen:true};
      app.state(edited); // No checkpoint, timeout or new explicit route before native Back.
      await app.back();
      assert.equal(app.win.location.href,absolute('roadmap/a.html#theorem-a'));
      await until(()=>app.commits.at(-1).nodeId==='a' && app.getState().mode==='sources');
      assert.equal(app.getState().readingScroll,80);
      const aKey=app.win.history.state.autoformNavigation.key; assert.notEqual(aKey,bKey);
      await app.forward();
      assert.equal(app.win.location.href,absolute('roadmap/b.html#theorem-b'));
      await until(()=>app.commits.at(-1).nodeId==='b' && app.getState().query===edited.query);
      assert.deepEqual(app.getState(),edited);
      assert.equal(app.win.history.state.autoformNavigation.key,bKey);
      assert.equal(app.win.history.state.material,'preserved'); assert.equal(app.win.history.length,length);
      app.nav.dispose();
    """)


def test_distinct_same_url_views_restore_by_entry_key_not_url() -> None:
    _route(r"""
      const app=setup(); app.state({mode:'graph',area:null,camera:{x:20,y:20,scale:1}});
      await app.nav.start(); const url=app.win.location.href,firstKey=app.win.history.state.autoformNavigation.key;
      app.state({mode:'graph',area:'Geometry',camera:{x:40,y:60,scale:.8}});
      app.nav.checkpoint('push');
      const secondKey=app.win.history.state.autoformNavigation.key,length=app.win.history.length;
      assert.notEqual(firstKey,secondKey); assert.equal(app.win.location.href,url);
      const edited={mode:'graph',area:'Geometry',camera:{x:100,y:200,scale:1.3},readingScroll:90,outlineOpen:true};
      app.state(edited); await app.back();
      await until(()=>app.getState().area===null);
      assert.equal(app.win.location.href,url); assert.equal(app.win.history.state.autoformNavigation.key,firstKey);
      await app.forward(); await until(()=>app.getState().camera.x===100);
      assert.deepEqual(app.getState(),edited); assert.equal(app.win.location.href,url);
      assert.equal(app.win.history.state.autoformNavigation.key,secondKey);
      assert.equal(app.win.history.length,length); assert.equal(app.win.history.state.material,'preserved');
      app.nav.dispose();
    """)


def test_snapshot_byte_bounds_evict_without_empty_cache_errors_and_fall_back_to_history() -> None:
    _route(r"""
      const tiny=setup({cacheBytes:1024}); await tiny.nav.start();
      for (let n=0;n<6;n++) {tiny.state({step:n,padding:'x'.repeat(650)}); assert(tiny.nav.checkpoint('push'));}
      await tiny.back(); await until(()=>tiny.getState().step===4);
      assert.equal(tiny.errors.length,0); tiny.nav.dispose();

      const app=setup(); await app.nav.start(); await app.nav.navigateNode('a'); await app.nav.navigateNode('b');
      app.state({mode:'recent bounded edit',readingScroll:71}); await app.back();
      await until(()=>app.commits.at(-1).nodeId==='a');
      await app.forward(); await until(()=>app.getState().mode==='recent bounded edit');
      // An oversized outgoing snapshot must not retain a previous cache value or exceed the byte cap.
      app.state({mode:'oversized',padding:'x'.repeat(300000)}); await app.back();
      await until(()=>app.commits.at(-1).nodeId==='a');
      await app.forward(); await until(()=>app.commits.at(-1).nodeId==='b');
      assert.equal(app.getState().mode,'recent bounded edit'); assert.equal(app.getState().readingScroll,71);
      assert.equal(app.errors.length,0); app.nav.dispose();
    """)


def test_native_scroll_restoration_is_owned_only_after_commit_and_released_without_overriding_other_owners() -> None:
    # jsdom has no scroll restoration: the harness gives history one, which only the router changes here.
    _route(r"""
      const app=setup(); assert.equal(app.win.history.scrollRestoration,'auto');
      await app.nav.start(); assert.equal(app.win.history.scrollRestoration,'manual');
      app.nav.dispose(); assert.equal(app.win.history.scrollRestoration,'auto');
      let release; const delayed=setup({commit(){return new Promise(resolve=>{release=resolve;});}});
      const waiting=delayed.nav.start(); await until(()=>delayed.commits.length===1);
      assert.equal(delayed.win.history.scrollRestoration,'manual'); // The renderer's readiness is still pending.
      release(); await waiting; delayed.nav.dispose(); assert.equal(delayed.win.history.scrollRestoration,'auto');
      const existing=setup({scrollRestoration:'manual'}); await existing.nav.start(); existing.nav.dispose();
      assert.equal(existing.win.history.scrollRestoration,'manual');
      const changed=setup(); await changed.nav.start(); changed.win.history.scrollRestoration='auto'; changed.nav.dispose();
      assert.equal(changed.win.history.scrollRestoration,'auto');
      const failure=setup({fetch(_url,_settings,reply){return reply({},rootSpec.route,503);}});
      assert.equal(await failure.nav.start(),null); assert.equal(failure.win.history.scrollRestoration,'auto');
      failure.nav.dispose();
    """)


def test_alias_notice_loads_canonical_article_and_keeps_legacy_selection() -> None:
    _route(r"""
      const alias={title:'Old explorer',route:aSpec.route,data:'../../dependencies/a.json',catalog:'../index.json',scope:'a',prose:'Reading notice only'};
      const app=setup({initial:alias,url:absolute('dependencies/scopes/a.html#node=a')});
      const bundle=await app.nav.start(); assert.equal(bundle.url,absolute('roadmap/a.html#theorem-a'));
      assert.equal(bundle.nodeId,'a'); assert(bundle.readingSource.textContent.includes('Complete source statement'));
      assert(!bundle.readingSource.textContent.includes('Reading notice only')); assert.equal(app.win.history.length,1);
      app.nav.dispose();
      for (const [requested,id,path] of [
        ['scope:Geometry','Geometry','roadmap/geometry.html'],
        ['boundary:Geometry','Geometry','roadmap/geometry.html'],
        ['scope:Geometry','scope:Geometry','roadmap/exact-scope.html'],
        ['boundary:Geometry','boundary:Geometry','roadmap/exact-boundary.html']
      ]) {
        const initial={...rootSpec,data:'dependencies.json',catalog:'dependencies/index.json',prose:'Reading notice only'};
        const scoped=setup({initial,url:absolute('dependencies.html#node='+encodeURIComponent(requested))});
        const source={title:'Geometry',route:absolute(path),data:'../dependencies/geometry.json',scope:id};
        const catalog=index();
        catalog.nodes.push({id:'Geometry',title:'Geometry',kind:'scope',result:false,
          url:'../dependencies.html#node=scope%3AGeometry',route:'../roadmap/geometry.html',
          data_url:'geometry.json',scope:'Geometry'});
        if (id !== 'Geometry') catalog.nodes.push({id,title:'Exact authored ID',kind:'scope',result:false,
          url:'../dependencies.html#node='+encodeURIComponent(id),route:'../'+path,data_url:'geometry.json',scope:id});
        scoped.contents.set(absolute('dependencies/index.json'),catalog);
        scoped.contents.set(source.route,source); scoped.contents.set(absolute('dependencies/geometry.json'),graph);
        const opened=await scoped.nav.start();
        assert.equal(opened.nodeId,id); assert.equal(opened.url,source.route);
        assert(!opened.readingSource.textContent.includes('Reading notice only'));
        const reopened=await scoped.nav.navigateNode(requested);
        assert.equal(reopened.nodeId,id); assert.equal(reopened.url,source.route);
        assert.equal(scoped.win.history.length,1); scoped.nav.dispose();
      }
    """)


@pytest.mark.parametrize(("entrance", "query"), (("directory", ""), ("index", ""), ("index", "?edition=1")))
def test_a_directory_entrance_is_the_page_it_declares_and_keeps_its_typeset_reading(entrance: str, query: str) -> None:
    # MkDocs serves a directory's page at X/ and X/index.html: one document, so
    # the loaded twin is committed in place, under the address it declares.
    _route(r"""
      const directory=absolute('roadmap/chapter/'),indexed=directory+'index.html',{entrance,query}=args;
      const canonical=(entrance==='index'?indexed:directory)+query,requested=(entrance==='index'?directory:indexed)+query;
      const spec={title:'Complete chapter',route:canonical,data:'../../dependencies/a.json',catalog:'../../dependencies/index.json',scope:'chapter',
        prose:'Complete published theorem, hypotheses, and proof.'};
      const app=setup({initial:spec,url:requested+'#theorem-a'}),typeset=element(app.win,'mjx-container',{class:'MathJax'},element(app.win,'svg'));
      app.initial.reading.append(typeset);
      const catalog=index();catalog.nodes[1]={...catalog.nodes[1],route:canonical+'#theorem-a',scope:'chapter'};
      app.contents.set(absolute('dependencies/index.json'),catalog);app.contents.set(canonical,{...spec,prose:'A second copy'});
      const opened=await app.nav.start();assert(opened);assert.equal(opened.url,canonical+'#theorem-a');
      assert.equal(opened.nodeId,'a');assert.equal(opened.anchor,'theorem-a');assert.equal(opened.focus,true);
      assert.equal(opened.documentURL,canonical);assert.equal(opened.scope,'chapter');
      assert.strictEqual(opened.readingSource,app.initial.reading);assert.strictEqual(opened.readingSource.querySelector('mjx-container'),typeset);
      assert(opened.readingSource.textContent.includes(spec.prose));
      assert.deepEqual(app.fetches.filter(url=>url===canonical||url===requested),[]);
      assert.equal(app.win.history.length,1);assert.equal(app.win.location.href,opened.url);
      assert.equal(app.win.history.state.autoformNavigation.url,opened.url);
      assert.deepEqual(app.nav.getIndex().relations,[['a','b',1,0]]);assert.equal(app.errors.length,0);app.nav.dispose();
    """, entrance=entrance, query=query)


@pytest.mark.parametrize(
    ("requested", "declared"),
    (
        ("roadmap/chapter/", "roadmap/other/index.html"),
        ("roadmap/chapter/", "roadmap/chapter/other.html"),
        ("roadmap/chapter/?edition=2", "roadmap/chapter/index.html?edition=1"),
    ),
)
def test_a_directory_page_declaring_another_page_or_query_still_fetches_it(requested: str, declared: str) -> None:
    # Only the same document at its other address is kept; another page, or the
    # twin under another query, is an alias whose canonical page is fetched.
    _route(r"""
      const requested=absolute(args.requested),declared=absolute(args.declared);
      const spec={title:'Complete chapter',route:declared,data:'../../dependencies/a.json',catalog:'../../dependencies/index.json',scope:'chapter',
        prose:'Complete published theorem, hypotheses, and proof.'};
      const app=setup({initial:{...spec,prose:'Reading notice only'},url:requested});app.contents.set(declared,spec);
      const opened=await app.nav.start();assert(opened);assert.equal(opened.url,declared);assert.equal(opened.documentURL,declared);
      assert(opened.readingSource.textContent.includes(spec.prose));assert(!opened.readingSource.textContent.includes('Reading notice only'));
      assert.equal(app.fetches.filter(url=>url===declared).length,1);assert.equal(app.errors.length,0);app.nav.dispose();
    """, requested=requested, declared=declared)


@pytest.mark.parametrize("fragment", ("theorem-a", "appendix%2Fnotes"))
def test_ordinary_alias_fragment_survives_complete_redirect_chain_and_back_forward(fragment: str) -> None:
    _route(r"""
      const fragment=args.fragment,app=setup(),aliasURL=absolute('roadmap/old-a.html'),middle=absolute('roadmap/middle-a.html');
      const notice={...aSpec,route:middle,prose:'First reading notice only'};
      const second={...aSpec,route:aSpec.route,prose:'Second reading notice only'};
      app.contents.set(aliasURL,notice);app.contents.set(middle,second);await app.nav.start();
      const origin=app.win.location.href,opened=await app.nav.navigate(aliasURL+'#'+fragment);assert(opened);
      const canonical=aSpec.route+'#'+fragment;
      assert.equal(opened.url,canonical);assert.equal(opened.anchor,decodeURIComponent(fragment));
      assert.equal(opened.nodeId,fragment==='theorem-a'?'a':null);assert.equal(opened.documentURL,aSpec.route);
      assert(opened.readingSource.textContent.includes('Complete source statement and proof.'));
      assert(!opened.readingSource.textContent.includes('reading notice only'));
      for(const url of [aliasURL,middle,aSpec.route])assert.equal(app.fetches.filter(value=>value===url).length,1);
      assert.equal(app.win.history.length,2);assert.equal(app.win.history.state.autoformNavigation.url,canonical);
      app.state({mode:'graph',readingScroll:86});app.nav.checkpoint();await app.back();
      await until(()=>app.commits.at(-1).url===origin&&app.initial.host.getAttribute('aria-busy')===null);
      await app.forward();await until(()=>app.commits.at(-1).url===canonical&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).anchor,decodeURIComponent(fragment));assert.equal(app.getState().readingScroll,86);
      assert.equal(app.win.history.length,2);assert.equal(app.win.history.state.material,'preserved');
      assert.equal(app.errors.length,0);app.nav.dispose();
    """, fragment=fragment)


def test_alias_fragment_does_not_override_explicit_or_legacy_statement_selection() -> None:
    _route(r"""
      const app=setup(),aliasURL=absolute('roadmap/old-a.html');
      app.contents.set(aliasURL,{...aSpec,prose:'Reading notice only'});await app.nav.start();
      const explicit=await app.nav.navigate(aliasURL+'#wrong-section',{nodeId:'a'});
      assert.equal(explicit.url,aSpec.route+'#theorem-a');assert.equal(explicit.nodeId,'a');assert.equal(explicit.anchor,'theorem-a');
      const legacy=await app.nav.navigate(aliasURL+'#node=a&direction=prerequisites');
      assert.equal(legacy.url,aSpec.route+'#theorem-a');assert.equal(legacy.nodeId,'a');assert.equal(legacy.anchor,'theorem-a');
      assert.equal(legacy.parameters.direction,'prerequisites');assert(!legacy.readingSource.textContent.includes('Reading notice only'));
      assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_a_section_of_a_page_selects_what_the_page_itself_selects() -> None:
    """A fragment naming a section, not a statement, selects the row the address without it selects: the page's
    owner, at either address of a directory's page and after an alias, so the book's trail, its topic line and the
    map's start are the page's. A legacy node or filter hash keeps its meaning, and a section at Home's address still
    selects nothing, as the address does, even where a row's route is Home."""
    _route(r"""
      const app=setup(),spec=addLargeScope(app),alias=absolute('roadmap/old-large.html');
      app.contents.set(alias,{...spec,prose:'Reading notice only'});await app.nav.start();
      const section=await app.nav.navigate(spec.route+'#notes');
      assert.equal(section.url,spec.route+'#notes');assert.equal(section.nodeId,'large');assert.equal(section.anchor,'notes');
      assert.equal(section.focus,false);assert.equal(section.title,spec.title);
      const moved=await app.nav.navigate(alias+'#notes');
      assert.equal(moved.url,spec.route+'#notes');assert.equal(moved.nodeId,'large');
      assert(!moved.readingSource.textContent.includes('Reading notice only'));
      const legacy=await app.nav.navigate(spec.route+'#node=b');assert.equal(legacy.nodeId,'b');
      const filtered=await app.nav.navigate(spec.route+'#view=list');
      assert.equal(filtered.nodeId,null);assert.deepEqual(filtered.parameters,{view:'list'});
      assert.equal(app.errors.length,0);app.nav.dispose();
      for(const entrance of ['roadmap/','roadmap/index.html']){
        const twin=setup({url:absolute(entrance)+'#notes'}),opened=await twin.nav.start();
        assert.equal(opened.url,rootSpec.route+'#notes',entrance);assert.equal(opened.nodeId,'roadmap',entrance);
        assert.equal(opened.anchor,'notes',entrance);assert.equal(twin.errors.length,0,entrance);twin.nav.dispose();
      }
      const home=absolute('index.html'),start=setup(),catalog=index();catalog.nodes[0].route=home;
      start.contents.set(absolute('dependencies/index.json'),catalog);
      start.contents.set(home,{title:'Site home',route:home,scope:'roadmap',data:'dependencies.json'});await start.nav.start();
      const intro=await start.nav.navigate(home+'#intro');
      assert.equal(intro.url,home+'#intro');assert.equal(intro.nodeId,null);assert.equal(intro.anchor,'intro');
      assert.equal(start.errors.length,0);start.nav.dispose();
    """)


def test_an_old_dependency_page_opened_in_the_book_goes_on_to_the_page_it_names() -> None:
    _route(r"""
      // What graph_pages writes there: a hidden explorer host naming the book page, and a link; no reading, no graph.
      const app=setup(),moved=absolute('dependencies/chapters/a.html');
      app.contents.set(moved,{moved:'../../roadmap/a.html'}); await app.nav.start();
      const opened=await app.nav.navigate(moved);
      assert.equal(opened.url,aSpec.route); assert(opened.readingSource.textContent.includes('Complete source statement'));
      assert.equal(app.fetches.filter(url=>url===moved).length,1);
      // With a node in its hash, the index names the node's page, so the old page is not fetched again.
      const selected=await app.nav.navigate(moved+'#node=b');
      assert.equal(selected.url,absolute('roadmap/b.html#theorem-b')); assert.equal(selected.nodeId,'b');
      assert.equal(app.fetches.filter(url=>url===moved).length,1);
      assert.equal(app.errors.length,0); app.nav.dispose();
    """)


def test_an_old_dependency_page_script_keeps_the_fragment_and_drops_the_query(tmp_path: Path) -> None:
    # jsdom cannot leave a document, so the page's script runs against a location that records where it would go.
    page = graph_pages._write_moved(tmp_path / "dependencies/chapters/a.md", "../../roadmap/a.html")
    script = re.search(r"<script>(.*?)</script>", page.read_text(encoding="utf-8"), re.DOTALL)[1]
    run_router(r"""
      const old='https://example.test/project/dependencies/chapters/a.html', replaced=[];
      for (const suffix of ['', '#node=a%2Fb', '#view=list&status=fully_proved', '?utm=x#node=a', '?utm=x']) {
        const url=new URL(old+suffix);
        new Function('location',args.script)({href:url.href,search:url.search,hash:url.hash,replace(to){replaced.push(to);}});
      }
      // The book reads a node and the old view and filter links from the fragment; a query would only refetch the page.
      const book='https://example.test/project/roadmap/a.html';
      assert.deepEqual(replaced,[book,book+'#node=a%2Fb',book+'#view=list&status=fully_proved',book+'#node=a',book]);
    """, args={"script": script})


@pytest.mark.parametrize("query", ("?edition=1", "?edition=2"))
def test_directory_route_lookup_preserves_query_identity_and_uses_the_declared_canonical_query(query: str) -> None:
    _route(r"""
      const query=args.query,directory=absolute('roadmap/chapter/'),canonical=directory+'index.html?edition=1';
      const spec={title:'Complete edition one',route:canonical,data:'../../dependencies/a.json',scope:'chapter'};
      const app=setup(),catalog=index();catalog.nodes[1]={...catalog.nodes[1],route:canonical+'#theorem-a',scope:'chapter'};
      app.contents.set(absolute('dependencies/index.json'),catalog);app.contents.set(canonical,spec);
      app.contents.set(directory+query,{...spec,prose:'Alias reading notice only'});await app.nav.start();
      const opened=await app.nav.navigate(directory+query+'#theorem-a');assert(opened);
      assert.equal(opened.url,canonical+'#theorem-a');assert.equal(opened.nodeId,'a');assert.equal(opened.anchor,'theorem-a');
      assert(opened.readingSource.textContent.includes('Complete source statement and proof.'));
      assert.equal(app.fetches.filter(url=>url===directory+query).length,query==='?edition=1'?0:1,
        'A different query must not acquire a published route through directory normalization');
      assert.equal(app.fetches.filter(url=>url===canonical).length,1);assert.equal(app.errors.length,0);app.nav.dispose();
    """, query=query)


def test_query_only_canonical_contradiction_still_rejects_before_current_source_or_history_changes() -> None:
    _route(r"""
      const directory=absolute('roadmap/chapter/'),canonical=directory+'index.html?edition=1',other=directory+'index.html?edition=2';
      const wrong={title:'Wrong edition',route:other,data:'../../dependencies/a.json',scope:'chapter'};
      const app=setup({fetch(url,_settings,reply){if(url===canonical)return reply(wrong,other);}}),catalog=index();
      catalog.nodes[1]={...catalog.nodes[1],route:canonical+'#theorem-a',scope:'chapter'};
      app.contents.set(absolute('dependencies/index.json'),catalog);await app.nav.start();
      const before={url:app.win.location.href,length:app.win.history.length,commits:app.commits.length,source:app.commits.at(-1).readingSource};
      assert.equal(await app.nav.navigate(directory+'?edition=1#theorem-a'),null);
      assert.equal(app.win.location.href,before.url);assert.equal(app.win.history.length,before.length);
      assert.equal(app.commits.length,before.commits);assert.strictEqual(app.commits.at(-1).readingSource,before.source);
      assert.equal(app.errors.length,1);assert.equal(app.errors[0].message,'Published reading route contradicts the requested article');
      assert.equal(app.initial.host.getAttribute('aria-busy'),null);app.nav.dispose();
    """)


@pytest.mark.parametrize("prefix", ("/", "/project/", "/nested/notes/"))
def test_home_url_keeps_deployment_prefix_and_loads_complete_home_without_implicit_indexed_root(prefix: str) -> None:
    _route(r"""
      const site='https://example.test'+args.prefix,home=site+'index.html';
      const initial={title:'Current chapter',route:site+'roadmap/chapter/index.html',scope:'chapter',
        data:'../../dependencies.json',catalog:'../../dependencies/index.json'};
      const spec={title:'Site home',route:home,scope:'roadmap',data:'dependencies.json',catalog:'dependencies/index.json',
        prose:'Complete authored introduction at the site home, not a roadmap redirect.'};
      const app=setup({initial,url:initial.route}),catalog=index();catalog.nodes[0].route=home;
      app.contents.set(site+'dependencies/index.json',catalog);app.contents.set(site+'dependencies.json',graph);
      app.contents.set(initial.route,initial);app.contents.set(home,spec);
      assert.equal(app.nav.getHomeURL(),home);await app.nav.start();const opened=await app.nav.navigate(app.nav.getHomeURL());
      assert(opened);assert.equal(opened.url,home);assert.equal(opened.documentURL,home);
      assert.equal(opened.nodeId,null,'Bare Home is not a request to open the indexed roadmap record');
      assert.equal(opened.anchor,null);assert.equal(opened.title,spec.title);assert.equal(opened.scope,'roadmap');
      assert(opened.readingSource.textContent.includes(spec.prose));assert.equal(app.fetches.filter(url=>url===home).length,1);
      assert.equal(app.win.history.length,2);assert.equal(app.win.history.state.autoformNavigation.url,home);
      await app.back();await until(()=>app.commits.at(-1).url===initial.route&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).readingSource.querySelector('h1').textContent,initial.title);
      await app.forward();await until(()=>app.commits.at(-1).url===home&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).nodeId,null);assert.equal(app.nav.getHomeURL(),home);
      assert.equal(app.win.history.state.material,'preserved');assert.equal(app.errors.length,0);app.nav.dispose();
      assert.equal(app.nav.getHomeURL(),home,'The native destination remains usable after disposal');
    """, prefix=prefix)


def test_home_route_exclusion_keeps_explicit_root_and_real_home_statement_permalinks() -> None:
    _route(r"""
      const home=absolute('index.html'),spec={title:'Site home',route:home,scope:'roadmap',data:'dependencies.json'};
      const app=setup(),catalog=index();catalog.nodes[0].route=home;catalog.nodes[1].route=home+'#theorem-a';
      app.contents.set(absolute('dependencies/index.json'),catalog);app.contents.set(home,spec);await app.nav.start();
      const root=await app.nav.navigateNode('roadmap');assert.equal(root.url,home);assert.equal(root.nodeId,'roadmap');
      const statement=await app.nav.navigate(home+'#theorem-a');assert.equal(statement.url,home+'#theorem-a');
      assert.equal(statement.nodeId,'a');assert.equal(statement.anchor,'theorem-a');assert.equal(statement.focus,true);
      const bare=await app.nav.navigate(app.nav.getHomeURL());assert.equal(bare.url,home);assert.equal(bare.nodeId,null);
      assert.equal(bare.anchor,null);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_home_prefix_uses_global_assets_not_nested_article_folders_named_roadmap_or_dependencies() -> None:
    _route(r"""
      for(const folder of ['roadmap','dependencies']) {
        const initial={title:'Nested article',route:absolute('roadmap/notes/'+folder+'/index.html'),scope:'nested',
          data:absolute('dependencies.json'),catalog:absolute('dependencies/index.json')};
        const app=setup({initial,url:initial.route});assert.equal(app.nav.getHomeURL(),absolute('index.html'));
        assert.equal(app.nav.getIndex(),null);assert.equal(app.fetches.length,0,'A native Home target needs no eager catalogue fetch');
        app.nav.dispose();
      }
    """)


def test_failed_reading_request_leaves_url_and_current_shell_unchanged() -> None:
    _route(r"""
      // The failed response carries the article itself: only its status says it failed.
      const app=setup({fetch(url,_settings,reply){if(url===aSpec.route)return reply(aSpec,url,503);}});
      await app.nav.start(); const length=app.commits.length,url=app.win.location.href;
      assert.equal(await app.nav.navigateNode('a'),null); assert.equal(app.commits.length,length);
      assert.equal(app.win.location.href,url); assert.equal(app.errors.length,1); app.nav.dispose();
    """)


def test_a_routed_page_outside_the_roadmap_is_a_site_document_without_its_fragment() -> None:
    _route(r"""
      // Neither page is under roadmap/ or dependencies/, so only the search row's route makes the first one internal.
      const payload=index();payload.nodes.push({id:'guide',title:'Guide',route:'../guide/intro.html#setup'});
      const app=setup();app.contents.set(absolute('dependencies/index.json'),payload);
      await app.nav.start();const root=mount(app,await app.nav.navigateNode('a'));
      const routed=element(app.win,'a',{href:absolute('guide/intro.html')},'Guide'),
        unrouted=element(app.win,'a',{href:absolute('guide/other.html')},'Other');
      root.append(routed,unrouted);app.nav.bindReader(root);
      assert.equal(click(unrouted),false);assert.equal(click(routed),true);app.nav.dispose();
    """)


def test_reader_intercepts_only_plain_site_navigation_and_disposes_listener() -> None:
    _route(r"""
      const app=setup(); await app.nav.start(); const root=mount(app,await app.nav.navigateNode('a')); app.nav.bindReader(root);
      const internal=root.querySelector('a'), external=root.querySelectorAll('a')[1];
      assert.equal(click(internal,{ctrlKey:true}),false); assert.equal(click(external),false);
      assert.equal(click(internal),true);
      await until(()=>app.commits.at(-1).nodeId==='b'); app.nav.dispose(); assert.equal(click(internal),false);
    """)


def test_native_section_anchors_do_not_replace_the_selected_article() -> None:
    _route(r"""
      const app=setup(); await app.nav.start(); const bundle=await app.nav.navigateNode('a'),root=mount(app,bundle);
      const section=element(app.win,'a',{href:aSpec.route+'#appendix'},'Appendix'); root.append(section); app.nav.bindReader(root);
      assert.equal(click(section),false,'the browser follows a section link itself');
      const count=app.commits.length, requests=app.fetches.length;
      await follow(app,section.href);
      assert.equal(app.commits.length,count); assert.equal(app.fetches.length,requests);
      assert.equal(app.commits.at(-1).nodeId,'a'); assert.equal(bundle.url,section.href); assert(bundle.isCurrent());
      app.nav.checkpoint();
      assert.equal(app.win.history.state.autoformNavigation.url,section.href); app.nav.dispose();
    """)


#: A hash on the book's address, and the old explorer's filters it opens with, or None for a section of the page.
_HASHES = (
    ("view=list&status=fully_proved", {"view": "list", "status": "fully_proved"}),
    ("isolates=0", {"isolates": "0"}),
    ("view=graph&direction=both&isolates=1", {"view": "graph", "direction": "both", "isolates": "1"}),
    ("direction=prerequisites&status=defined,conditional,inventory_checked",
     {"direction": "prerequisites", "status": "defined,conditional,inventory_checked"}),
    ("status=-", {"status": "-"}),
    ("node=a&view=list&direction=dependents", {"node": "a", "view": "list", "direction": "dependents"}),
    ("retained-section", None),
    ("view=authored", None),
    ("status=an_authored_heading", None),
    ("status=fully_proved,", None),
    ("view=list&heading=statement", None),
    ("view=list&view=graph", None),
    ("isolates=maybe", None),
    ("direction=sideways", None),
)


def test_only_recognized_legacy_filter_hashes_open_as_filters_and_any_other_hash_is_a_section() -> None:
    _route(r"""
      for(const [hash,parameters] of args.hashes) {
        const app=setup({url:rootSpec.route+'#'+hash}),bundle=await app.nav.start(),node=Boolean(parameters&&parameters.node);
        assert.deepEqual(bundle.parameters,parameters||{},hash);
        assert.equal(bundle.anchor,parameters?(node?'theorem-a':null):decodeURIComponent(hash),hash);
        assert.equal(bundle.focus,node,hash);assert.equal(app.errors.length,0,hash);app.nav.dispose();
      }
    """, hashes=_HASHES)


def test_filter_only_hash_links_and_back_use_the_router_while_native_section_links_do_not() -> None:
    _route(r"""
      const app=setup();await app.nav.start();
      function link(hash) {
        const anchor=element(app.win,'a',{href:rootSpec.route+'#'+hash},'A genuine internal link');
        app.initial.reading.append(anchor);return click(anchor);
      }
      assert.equal(link('view=list&status=-'),true);
      await until(()=>app.commits.length===2);
      assert.deepEqual(app.commits.at(-1).parameters,{view:'list',status:'-'});assert.equal(app.commits.at(-1).anchor,null);
      assert.equal(link('isolates=0'),true);await until(()=>app.commits.length===3);
      assert.deepEqual(app.commits.at(-1).parameters,{isolates:'0'});
      await app.back();await until(()=>app.commits.length===4);
      assert.equal(app.commits.at(-1).reason,'popstate');
      assert.deepEqual(app.commits.at(-1).parameters,{view:'list',status:'-'});
      assert.equal(app.win.location.href,rootSpec.route+'#view=list&status=-');
      const before=app.commits.length,requests=app.fetches.length;
      assert.equal(link('retained-section'),false);assert.equal(link('view=list&heading=authored'),false);
      await follow(app,rootSpec.route+'#retained-section');
      assert.equal(app.commits.length,before);assert.equal(app.fetches.length,requests);
      assert.equal(app.commits.at(-1).anchor,'retained-section');assert.equal(app.errors.length,0);app.nav.dispose();
    """)


@pytest.mark.parametrize("kind", ["http_redirect", "declared_alias"])
def test_indexed_canonical_contradiction_preserves_current_source_and_can_retry_corrected_html(kind: str) -> None:
    _route(r"""
      let contradictory=true;
      const kind=args.kind,app=setup({fetch(url,_settings,reply){
        if(kind==='http_redirect'&&contradictory&&url===bSpec.route)return reply(aSpec,aSpec.route);
      }});
      await app.nav.start();await app.nav.navigateNode('a');
      if(kind==='declared_alias')app.contents.set(bSpec.route,{...bSpec,route:aSpec.route,title:'Contradictory canonical notice'});
      const state={mode:'graph',requestedNode:'a',readingScroll:91,camera:{x:12,y:24,scale:1.2}};
      app.state(state);const before={url:app.win.location.href,length:app.win.history.length,commits:app.commits.length,
        source:app.commits.at(-1).readingSource,index:app.nav.getIndex()};
      assert.equal(await app.nav.navigateNode('b'),null);
      assert.equal(app.win.location.href,before.url);assert.equal(app.win.history.length,before.length);
      assert.equal(app.commits.length,before.commits);assert.strictEqual(app.commits.at(-1).readingSource,before.source);
      assert.deepEqual(app.getState(),state);assert.strictEqual(app.nav.getIndex(),before.index);
      assert.equal(app.errors.length,1);assert.equal(app.errors[0].url,bSpec.route+'#theorem-b');
      assert.equal(app.initial.host.getAttribute('aria-busy'),null);
      assert.equal(app.fetches.filter(url=>url===bSpec.route).length,1);
      contradictory=false;app.contents.set(bSpec.route,bSpec);
      const corrected=await app.nav.navigateNode('b');assert(corrected);
      assert.equal(corrected.url,bSpec.route+'#theorem-b');assert.equal(corrected.nodeId,'b');
      assert.equal(corrected.documentURL,bSpec.route);assert.equal(corrected.scope,'b');
      assert.equal(corrected.readingSource.querySelector('h1').textContent,bSpec.title);
      assert(corrected.readingSource.textContent.includes('Complete source statement and proof.'));
      assert.equal(app.fetches.filter(url=>url===bSpec.route).length,2,'Contradictory HTML cannot survive in the cache');
      assert.equal(app.errors.length,1);assert.equal(app.win.history.length,before.length+1);
      assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """, kind=kind)


def test_start_ignores_a_versioned_snapshot_carried_from_another_article_url() -> None:
    _route(r"""
      const url=bSpec.route+'#theorem-b',app=setup({initial:bSpec,url});
      const valid={mode:'graph',requestedNode:'b',readingScroll:17};app.state(valid);
      app.win.history.replaceState({material:'preserved',autoformNavigation:{version:1,url:aSpec.route+'#theorem-a',
        key:'foreign-a',snapshot:{mode:'graph',requestedNode:'a',readingScroll:91}}},'',url);
      const opened=await app.nav.start();assert(opened);
      assert.equal(opened.url,url);assert.equal(opened.nodeId,'b');assert.equal(opened.snapshot,null);
      assert.equal(opened.readingSource.querySelector('h1').textContent,bSpec.title);
      assert.deepEqual(app.restored,[]);assert.deepEqual(app.getState(),valid);
      assert.notEqual(app.win.history.state.autoformNavigation.key,'foreign-a');
      assert.equal(app.win.history.state.autoformNavigation.url,url);
      assert.deepEqual(app.win.history.state.autoformNavigation.snapshot,valid);
      assert.equal(app.win.history.state.material,'preserved');assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_popstate_ignores_foreign_url_snapshot_and_keeps_the_destination_article_identity() -> None:
    _route(r"""
      let app;app=setup({commit(bundle){app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});}});
      await app.nav.start();await app.nav.navigateNode('a');
      const url=bSpec.route+'#theorem-b',commits=app.commits.length;
      arrive(app,{material:'preserved',autoformNavigation:{version:1,url:aSpec.route+'#theorem-a',
        key:'foreign-a',snapshot:{mode:'graph',requestedNode:'a',readingScroll:91}}},url);
      await until(()=>app.commits.length===commits+1&&app.initial.host.getAttribute('aria-busy')===null);
      const opened=app.commits.at(-1);assert.equal(opened.nodeId,'b');assert.equal(opened.url,url);
      assert.equal(opened.snapshot,null);assert.equal(opened.readingSource.querySelector('h1').textContent,bSpec.title);
      assert.deepEqual(app.restored,[]);assert.equal(app.getState().requestedNode,'b');
      assert.notEqual(app.win.history.state.autoformNavigation.key,'foreign-a');
      assert.equal(app.win.history.state.autoformNavigation.url,url);
      assert.equal(app.win.history.state.autoformNavigation.snapshot.requestedNode,'b');
      assert.equal(app.win.history.state.material,'preserved');assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_postcommit_renderer_failure_reports_verified_canonical_url_and_can_retry_without_a_new_entry() -> None:
    _route(r"""
      let app,rejectOnce=true;
      app=setup({commit(bundle){
        if(bundle.nodeId==='a'&&rejectOnce){rejectOnce=false;throw new Error('Renderer rejected the committed article');}
        app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});
      }});
      await app.nav.start();const length=app.win.history.length;
      const alias=absolute('dependencies/scopes/a.html#node=a'),canonical=aSpec.route+'#theorem-a';
      assert.equal(await app.nav.navigate(alias,{focus:true}),null);
      assert.equal(app.win.location.href,canonical);assert.equal(app.win.history.length,length+1);
      assert.equal(app.commits.at(-1).nodeId,'a');assert.equal(app.errors.length,1);
      assert.equal(app.errors[0].url,canonical);assert.notEqual(app.errors[0].url,alias);
      const failedHistory=structuredClone(app.win.history.state);
      assert.equal(failedHistory.autoformNavigation.snapshot,null);
      app.state({mode:'graph',requestedNode:'roadmap',readingScroll:999});
      assert.equal(app.nav.checkpoint(),null);assert.equal(app.nav.checkpoint('push'),null);
      assert.deepEqual(app.win.history.state,failedHistory);assert.equal(app.win.history.length,length+1);
      const recovered=await app.nav.navigateNode('a');assert(recovered);
      assert.equal(recovered.url,canonical);assert.equal(recovered.nodeId,'a');
      assert.equal(recovered.readingSource.querySelector('h1').textContent,aSpec.title);
      assert.equal(app.getState().requestedNode,'a');assert.equal(app.win.history.length,length+1);
      assert.equal(app.win.history.state.autoformNavigation.snapshot.requestedNode,'a');
      assert.equal(app.errors.length,1);assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """)


@pytest.mark.parametrize("phase", ["commit", "restore"])
def test_failed_committed_view_cannot_cache_previous_article_state_for_back_then_forward(phase: str) -> None:
    _route(r"""
      let app,rejectOnce=true;const phase=args.phase,restorations=[];
      app=setup({commit(bundle){
        if(phase==='commit'&&bundle.nodeId==='b'&&rejectOnce){rejectOnce=false;throw new Error('Renderer rejected B');}
        app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});
      },restore(saved,bundle){
        restorations.push({nodeId:bundle.nodeId,saved:structuredClone(saved)});
        if(phase==='restore'&&bundle.nodeId==='b'&&rejectOnce){rejectOnce=false;throw new Error('Restoration rejected B');}
      }});
      await app.nav.start();await app.nav.navigateNode('a');
      const oldA={mode:'graph',requestedNode:'a',readingScroll:91};app.state(oldA);
      const url=bSpec.route+'#theorem-b';
      if(phase==='commit')assert.equal(await app.nav.navigateNode('b'),null);
      else {
        arrive(app,{material:'preserved',autoformNavigation:{version:1,url,key:'failed-b',snapshot:oldA}},url);
        await until(()=>app.errors.length===1&&app.initial.host.getAttribute('aria-busy')===null);
      }
      assert.equal(app.win.location.href,url);assert.equal(app.errors.length,1);assert.equal(app.errors[0].url,url);
      assert.equal(app.win.history.state.autoformNavigation.snapshot,null);
      const failedHistory=structuredClone(app.win.history.state),length=app.win.history.length;
      app.state(oldA);assert.equal(app.nav.checkpoint(),null);assert.equal(app.nav.checkpoint('push'),null);
      assert.deepEqual(app.win.history.state,failedHistory);assert.equal(app.win.history.length,length);
      await app.back();
      await until(()=>app.commits.at(-1).nodeId==='a'&&app.initial.host.getAttribute('aria-busy')===null);
      const count=app.commits.length;await app.forward();
      await until(()=>app.commits.length===count+1&&app.initial.host.getAttribute('aria-busy')===null);
      const recovered=app.commits.at(-1);assert.equal(recovered.nodeId,'b');assert.equal(recovered.url,url);
      assert.equal(recovered.readingSource.querySelector('h1').textContent,bSpec.title);
      assert.equal(recovered.snapshot,null);assert.equal(app.getState().requestedNode,'b');
      assert.equal(app.win.history.state.autoformNavigation.snapshot.requestedNode,'b');
      assert.equal(app.errors.length,1);assert.equal(app.win.history.length,length);
      const restoredB=restorations.filter(entry=>entry.nodeId==='b');
      assert.equal(restoredB.length,phase==='restore'?1:0,'Recovery must not reuse the failed B snapshot');
      if(phase==='restore')assert.deepEqual(restoredB[0].saved,oldA);
      assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """, phase=phase)


def test_failed_current_view_cannot_take_the_native_section_shortcut_with_previous_reading_dom() -> None:
    _route(r"""
      let app,rejectOnce=true;app=setup({commit(bundle){
        if(bundle.nodeId==='b'&&rejectOnce){rejectOnce=false;throw new Error('Renderer rejected B');}
        app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});
      }});
      await app.nav.start();await app.nav.navigateNode('a');assert.equal(await app.nav.navigateNode('b'),null);
      const count=app.commits.length,section=bSpec.route+'#appendix';
      await follow(app,section);
      await until(()=>app.commits.length===count+1&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).url,section);
      assert.equal(app.commits.at(-1).readingSource.querySelector('h1').textContent,bSpec.title);
      assert.equal(app.errors.length,1);assert.equal(app.win.location.href,section);app.nav.dispose();
    """)


def test_valid_pending_renderer_readiness_still_allows_current_view_checkpoints() -> None:
    _route(r"""
      let app,release,waiting=false;app=setup({commit(bundle){
        app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});
        if(bundle.nodeId==='b'){waiting=true;return new Promise(resolve=>{release=resolve;});}
      }});
      await app.nav.start();await app.nav.navigateNode('a');const length=app.win.history.length;
      const pending=app.nav.navigateNode('b');await until(()=>waiting);
      const edited={mode:'graph',requestedNode:'b',readingScroll:71,camera:{x:11,y:23,scale:1.4}};
      app.state(edited);assert.deepEqual(app.nav.checkpoint(),edited);
      const firstKey=app.win.history.state.autoformNavigation.key;
      assert.deepEqual(app.nav.checkpoint('push'),edited);assert.equal(app.win.history.length,length+2);
      const pushedKey=app.win.history.state.autoformNavigation.key;assert.notEqual(pushedKey,firstKey);
      assert.equal(app.win.location.href,bSpec.route+'#theorem-b');
      release();const opened=await pending;assert(opened);assert.equal(opened.nodeId,'b');
      assert.equal(app.win.history.state.autoformNavigation.key,pushedKey);
      assert.deepEqual(app.win.history.state.autoformNavigation.snapshot,edited);assert.deepEqual(app.getState(),edited);
      assert.equal(app.errors.length,0);assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """)


def test_failed_history_clear_distrusts_surviving_browser_snapshot_but_keeps_future_private_checkpoints() -> None:
    _route(r"""
      let app,rejectOnce=true,clearFails=true;const restorations=[];
      app=setup({commit(bundle){app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});},
        restore(saved,bundle){
          restorations.push({nodeId:bundle.nodeId,saved:structuredClone(saved)});
          if(bundle.nodeId==='b'&&rejectOnce){rejectOnce=false;throw new Error('Restoration rejected B');}
        }});
      await app.nav.start();await app.nav.navigateNode('a');
      const url=bSpec.route+'#theorem-b',oldA={mode:'graph',requestedNode:'a',readingScroll:91};
      const replace=app.win.history.replaceState.bind(app.win.history);
      app.win.history.replaceState=(state,title,target)=>{
        if(clearFails&&target===url&&state.autoformNavigation&&state.autoformNavigation.snapshot===null){
          clearFails=false;throw new Error('History refused the failed-view snapshot clear');
        }
        return replace(state,title,target);
      };
      arrive(app,{material:'preserved',autoformNavigation:{version:1,url,key:'failed-b',snapshot:oldA}},url);
      await until(()=>app.errors.length===1&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(clearFails,false,'The actual failed-view clearing path must encounter the injected exception');
      assert.deepEqual(app.win.history.state.autoformNavigation.snapshot,oldA,'Browser state really survived the failed write');
      assert.equal(app.errors[0].url,url);assert.equal(app.win.location.href,url);assert.equal(app.nav.checkpoint(),null);
      const length=app.win.history.length;
      await app.back();
      await until(()=>app.commits.at(-1).nodeId==='a'&&app.initial.host.getAttribute('aria-busy')===null);
      const count=app.commits.length;await app.forward();
      await until(()=>app.commits.length===count+1&&app.initial.host.getAttribute('aria-busy')===null);
      const recovered=app.commits.at(-1);assert.equal(recovered.nodeId,'b');assert.equal(recovered.url,url);
      assert.equal(recovered.snapshot,null);assert.equal(app.getState().requestedNode,'b');
      assert.equal(recovered.readingSource.querySelector('h1').textContent,bSpec.title);
      assert.equal(restorations.filter(entry=>entry.nodeId==='b').length,1,'The rejected B restore must not recur');
      assert.deepEqual(restorations.find(entry=>entry.nodeId==='b').saved,oldA);
      const edited={mode:'graph',requestedNode:'b',readingScroll:73,camera:{x:15,y:29,scale:1.3}};
      app.state(edited);assert.deepEqual(app.nav.checkpoint(),edited);
      await app.back();
      await until(()=>app.commits.at(-1).nodeId==='a'&&app.initial.host.getAttribute('aria-busy')===null);
      const again=app.commits.length;await app.forward();
      await until(()=>app.commits.length===again+1&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).nodeId,'b');assert.deepEqual(app.commits.at(-1).snapshot,edited);
      assert.deepEqual(app.getState(),edited,'Fresh private snapshots still work after browser fallback is disabled');
      assert.equal(app.errors.length,1);assert.equal(app.win.history.length,length);
      assert.equal(app.win.history.state.material,'preserved');assert.equal(app.win.location.href,url);app.nav.dispose();
    """)


def test_renderer_failure_after_pending_checkpoint_push_cannot_restore_a_under_either_b_entry() -> None:
    _route(r"""
      let app,waiting=false,rejectPending,holdOnce=true;
      app=setup({commit(bundle){
        if(bundle.nodeId==='b'&&holdOnce){
          holdOnce=false;waiting=true;return new Promise((_resolve,reject)=>{rejectPending=reject;});
        }
        app.state({mode:'graph',requestedNode:bundle.nodeId,readingScroll:0});
      }});
      await app.nav.start();await app.nav.navigateNode('a');
      const oldA={mode:'graph',requestedNode:'a',readingScroll:91};app.state(oldA);
      const length=app.win.history.length,pending=app.nav.navigateNode('b');await until(()=>waiting);
      const url=bSpec.route+'#theorem-b';assert.equal(app.win.location.href,url);
      const originalKey=app.win.history.state.autoformNavigation.key;
      assert.deepEqual(app.nav.checkpoint(),oldA);
      assert.deepEqual(app.nav.checkpoint('push'),oldA);
      const pushedKey=app.win.history.state.autoformNavigation.key;
      assert.notEqual(pushedKey,originalKey);assert.equal(app.win.history.length,length+2);
      rejectPending(new Error('Delayed renderer rejected B'));assert.equal(await pending,null);
      assert.equal(app.win.location.href,url);assert.equal(app.errors.length,1);assert.equal(app.errors[0].url,url);
      assert.equal(app.win.history.state.autoformNavigation.key,pushedKey);
      assert.equal(app.win.history.state.autoformNavigation.snapshot,null);assert.equal(app.nav.checkpoint(),null);
      const count=app.commits.length;await app.back();
      await until(()=>app.commits.length===count+1&&app.initial.host.getAttribute('aria-busy')===null);
      const first=app.commits.at(-1);assert.equal(first.nodeId,'b');assert.equal(first.url,url);
      assert.equal(first.snapshot,null,'The older pending-checkpoint entry must not supply A state');
      assert.equal(app.getState().requestedNode,'b');assert.deepEqual(app.restored,[]);
      assert.equal(app.win.history.state.autoformNavigation.key,originalKey);
      assert.equal(first.readingSource.querySelector('h1').textContent,bSpec.title);
      const again=app.commits.length;await app.forward();
      await until(()=>app.commits.length===again+1&&app.initial.host.getAttribute('aria-busy')===null);
      const second=app.commits.at(-1);assert.equal(second.nodeId,'b');assert.equal(second.url,url);
      assert.equal(second.snapshot,null);assert.equal(app.getState().requestedNode,'b');assert.deepEqual(app.restored,[]);
      assert.equal(app.win.history.state.autoformNavigation.key,pushedKey);
      assert.equal(second.readingSource.querySelector('h1').textContent,bSpec.title);
      assert.equal(app.errors.length,1);assert.equal(app.win.history.length,length+2);
      assert.equal(app.win.history.state.material,'preserved');assert.equal(app.win.location.href,url);app.nav.dispose();
    """)


@pytest.mark.parametrize("phase", ["sync", "async"])
def test_native_section_restore_failure_quarantines_its_snapshot_and_recovers_on_back_forward(phase: str) -> None:
    _route(r"""
      let app,failOnce=true;const phase=args.phase,section=aSpec.route+'#appendix',restorations=[];
      app=setup({commit(bundle){app.state({requestedNode:bundle.nodeId,readingScroll:0});},
        restore(saved,bundle){
          restorations.push({url:bundle.url,saved:structuredClone(saved)});
          if(bundle.url===section&&failOnce){
            failOnce=false;app.state({requestedNode:'b',readingScroll:999});
            const error=new Error('Native section restoration rejected');
            if(phase==='sync')throw error;
            return Promise.reject(error);
          }
        }});
      await app.nav.start();await app.nav.navigateNode('a');
      const saved={requestedNode:'a',readingScroll:71},commits=app.commits.length;
      app.state(saved);arrive(app,{material:'preserved',autoformNavigation:{version:1,
        url:section,key:'native-section',snapshot:saved}},section);
      await until(()=>app.errors.length===1&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.length,commits,'Failure must occur in the actual native-section shortcut');
      assert.equal(app.errors[0].url,section);assert.equal(app.win.location.href,section);
      assert.equal(app.win.history.state.autoformNavigation.key,'native-section');
      assert.equal(app.win.history.state.autoformNavigation.snapshot,null);
      assert.equal(app.win.history.state.material,'preserved');
      const failedHistory=structuredClone(app.win.history.state),length=app.win.history.length;
      assert.equal(app.nav.checkpoint(),null);assert.equal(app.nav.checkpoint('push'),null);
      assert.deepEqual(app.win.history.state,failedHistory);assert.equal(app.win.history.length,length);
      await app.back();
      await until(()=>app.commits.length===commits+1&&app.initial.host.getAttribute('aria-busy')===null);
      assert.equal(app.commits.at(-1).nodeId,'a');assert.equal(app.getState().requestedNode,'a');
      const recovered=app.commits.length;await app.forward();
      await until(()=>app.win.location.href===section&&app.initial.host.getAttribute('aria-busy')===null);
      await tick(0);
      assert.equal(app.commits.length,recovered,'Healthy ordinary section navigation must stay native');
      assert.equal(restorations.filter(entry=>entry.url===section).length,1,'Rejected snapshot must not be restored twice');
      assert.deepEqual(restorations.find(entry=>entry.url===section).saved,saved);
      assert.equal(app.getState().requestedNode,'a');assert.notEqual(app.getState().readingScroll,999);
      assert.equal(app.win.history.state.autoformNavigation.snapshot,null);assert.equal(app.errors.length,1);
      assert.equal(app.win.history.length,length);assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """, phase=phase)


@pytest.mark.parametrize("phase", ["reentrant_sync", "late_async"])
def test_late_native_section_restore_rejection_cannot_quarantine_a_newer_committed_article(phase: str) -> None:
    _route(r"""
      let app,rejectRestore,newer;const phase=args.phase,section=aSpec.route+'#appendix';
      app=setup({commit(bundle){app.state({requestedNode:bundle.nodeId,readingScroll:0});},
        restore(_saved,bundle){if(bundle.url===section){
          if(phase==='reentrant_sync'){newer=app.nav.navigateNode('b');throw new Error('Superseded native restore threw');}
          return new Promise((_resolve,reject)=>{rejectRestore=reject;});
        }}});
      await app.nav.start();await app.nav.navigateNode('a');
      arrive(app,{material:'preserved',autoformNavigation:{version:1,url:section,
        key:'pending-native-section',snapshot:{requestedNode:'a',readingScroll:71}}},section);
      if(phase==='late_async'){assert.equal(typeof rejectRestore,'function');newer=app.nav.navigateNode('b');}
      const opened=await newer;assert(opened);
      if(phase==='late_async')rejectRestore(new Error('Stale native section restore rejected'));
      await tick(0);
      assert.equal(app.win.location.href,bSpec.route+'#theorem-b');assert.equal(app.getState().requestedNode,'b');
      assert.equal(app.errors.length,0);assert.equal(app.initial.host.getAttribute('aria-busy'),null);
      const edited={requestedNode:'b',readingScroll:83};app.state(edited);
      assert.deepEqual(app.nav.checkpoint(),edited,'Stale rejection must not mark the current B renderer failed');
      assert.deepEqual(app.win.history.state.autoformNavigation.snapshot,edited);
      assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """, phase=phase)


def test_rejected_popstate_history_preparation_reports_canonical_url_and_distrusts_surviving_snapshot() -> None:
    _route(r"""
      let app,rejectOnce=true;const section=aSpec.route+'#appendix',restorations=[];
      app=setup({commit(bundle){app.state({requestedNode:bundle.nodeId,readingScroll:0});},
        restore(saved,bundle){restorations.push({url:bundle.url,saved:structuredClone(saved)});}});
      await app.nav.start();await app.nav.navigateNode('a');
      const saved={requestedNode:'a',readingScroll:71},commits=app.commits.length;app.state(saved);
      const replace=app.win.history.replaceState.bind(app.win.history);
      app.win.history.replaceState=(state,title,url)=>{
        if(url===section&&rejectOnce){rejectOnce=false;throw new Error('History rejected native section preparation');}
        return replace(state,title,url);
      };
      arrive(app,{material:'preserved',autoformNavigation:{version:1,url:section,key:'history-preparation-failed',snapshot:saved}},section);
      assert.equal(rejectOnce,false);assert.equal(app.errors.length,1);assert.equal(app.errors[0].url,section);
      assert.equal(app.win.location.href,section);assert.equal(app.commits.length,commits);
      assert.deepEqual(app.getState(),saved);assert.deepEqual(app.restored,[]);
      assert.deepEqual(app.win.history.state.autoformNavigation.snapshot,saved,'The refused write really left browser state intact');
      assert.equal(app.nav.checkpoint(),null);assert.equal(app.win.history.state.material,'preserved');
      const length=app.win.history.length;await app.back();
      await until(()=>app.commits.length===commits+1&&app.initial.host.getAttribute('aria-busy')===null);
      const returned=app.commits.length;await app.forward();
      await until(()=>app.win.location.href===section&&app.initial.host.getAttribute('aria-busy')===null);
      await tick(0);
      assert.equal(app.commits.length,returned);assert.equal(app.getState().requestedNode,'a');
      assert.equal(restorations.filter(entry=>entry.url===section).length,0,'Uncleared browser snapshot must remain quarantined');
      assert.equal(app.win.history.state.autoformNavigation.snapshot,null);assert.equal(app.errors.length,1);
      const fresh={requestedNode:'a',readingScroll:93};app.state(fresh);assert.deepEqual(app.nav.checkpoint(),fresh);
      await app.back();await until(()=>app.commits.length===returned+1&&app.initial.host.getAttribute('aria-busy')===null);
      await app.forward();await until(()=>app.getState().readingScroll===93);
      assert.deepEqual(app.getState(),fresh,'Fresh private section snapshots remain valid after quarantine');
      assert.equal(app.win.location.href,section);assert.equal(app.win.history.length,length);
      assert.equal(app.win.history.state.material,'preserved');app.nav.dispose();
    """)
