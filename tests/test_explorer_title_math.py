"""Reading-label source contracts and genuine pinned MathJax output, not browser geometry."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from autoform_cli import dag_viewer
from autoform_cli.graph_views import GraphView, ViewEdge, ViewNode
from autoform_cli.mathjax import mathjax_script
from tests.test_site_mathjax_node import _node_report, mathjax_package

_ROOT = Path(__file__).resolve().parents[1]
_CLIENT = _ROOT / "autoform_cli/assets/blueprint-dag.js"
_NODE = shutil.which("node")
_TITLE = r"Groups $\mathbb{Z}/n$"
_SUMMARY = r"Cyclic **groups** $\ZZ/n$ retain code `$literal$`."
_CODE_TITLE = r"Notation $x$ and `</div><script>window.__titlePwned=1</script>`."


def test_reading_label_separates_real_site_math_from_code_and_preserves_source() -> None:
    assert dag_viewer._reading_label(_TITLE) == {
        "format": "inline-parts-v1", "source": _TITLE,
        "parts": [["text", "Groups "], ["math", r"\(\mathbb{Z}/n\)"]],
    }
    label = dag_viewer._reading_label(_SUMMARY)
    assert label is not None and label["source"] == _SUMMARY
    assert [part for part in label["parts"] if part[0] == "math"] == [["math", r"\(\ZZ/n\)"]]
    assert [part for part in label["parts"] if part[0] == "code"] == [["code", "$literal$"]]
    assert "Cyclic groups " in "".join(part[1] for part in label["parts"] if part[0] == "text")
    code = dag_viewer._reading_label(_CODE_TITLE)
    assert code is not None
    assert [part for part in code["parts"] if part[0] == "code"] == [["code", "</div><script>window.__titlePwned=1</script>"]]
    comparison = dag_viewer._reading_label("Math $a<b>c$")
    assert comparison is not None and comparison["parts"][-1] == ["math", r"\(a<b>c\)"]


@pytest.mark.parametrize("text", [
    "A plain module name", r"Code `$literal$`", "Math $x$ <script>window.__titlePwned=1</script>",
    "Math $x$ [nested link](other.md)", "Math $x$ ![image](image.png)", "Math $x$\n{: .untrusted}",
    r'<div class="arithmatex">\(x\)</div>', r'<span class="arithmatex">\(x\)</span>',
    "$x$ " * 129, "$x$" + "a" * 16_384,
])
def test_plain_unsupported_and_oversized_labels_keep_the_literal_fallback(text: str) -> None:
    assert dag_viewer._reading_label(text) is None


def test_literal_attribute_like_suffix_stays_text_without_becoming_an_attribute() -> None:
    text = "Math $x$ {.untrusted}"
    assert dag_viewer._reading_label(text) == {
        "format": "inline-parts-v1", "source": text,
        "parts": [["text", "Math "], ["math", r"\(x\)"], ["text", " {.untrusted}"]],
    }


def _view(title: str) -> GraphView:
    return GraphView(kind="full", title="Reading labels", nodes=(
        ViewNode("a", title, "node", ("a",), (("planned", 1),), status_key="planned"),
        ViewNode("b", "Plain B", "node", ("b",), (("planned", 1),), status_key="planned"),
    ), edges=(ViewEdge("a", "b", 2, 3),))


def test_optional_reading_title_keeps_plain_index_bytes_relations_and_statuses(tmp_path: Path) -> None:
    links = {"a": "a.html", "b": "b.html"}
    plain, marked = tmp_path / "plain.json", tmp_path / "marked.json"
    dag_viewer.write_search_index(plain, _view("Plain A"), context_links=links, include_relations=True)
    assert plain.read_bytes() == (
        b'{"nodes":[{"id":"a","kind":"node","status":"planned","title":"Plain A","url":"a.html"},'
        b'{"id":"b","kind":"node","status":"planned","title":"Plain B","url":"b.html"}],'
        b'"relations":[[0,1,2,3]],"schema":"autoform-dag-search/v1"}\n'
    )
    digests: list[str] = []
    view = _view(_TITLE)
    dag_viewer.write_search_index(marked, view, context_links=links, include_relations=True, sha256_out=digests)
    payload = json.loads(marked.read_bytes())
    assert payload["nodes"][0]["title"] == _TITLE
    assert payload["nodes"][0]["reading_title"] == dag_viewer._reading_label(_TITLE)
    assert "reading_title" not in payload["nodes"][1]
    assert payload["relations"] == [[0, 1, 2, 3]]
    assert [node["status"] for node in payload["nodes"]] == ["planned", "planned"]
    assert digests == [hashlib.sha256(marked.read_bytes()).hexdigest()]
    assert view.edges == (ViewEdge("a", "b", 2, 3),)


def test_publisher_breadcrumb_parts_preserve_plain_bytes_titles_urls_and_math_source(tmp_path: Path) -> None:
    view = GraphView(kind="full", title="Plain trail", nodes=(), edges=())
    empty, plain, marked = (tmp_path / name for name in ("empty.json", "plain.json", "marked.json"))
    dag_viewer.write_payload(empty, view, links={})
    dag_viewer.write_payload(plain, view, links={}, breadcrumbs=(("Library", "library.html"), ("Plain trail", None)))
    assert plain.read_bytes() == empty.read_bytes().replace(
        b'"breadcrumbs":[]', b'"breadcrumbs":[{"label":"Library","url":"library.html"},{"label":"Plain trail","url":null}]',
    )
    dag_viewer.write_payload(marked, view, links={}, breadcrumbs=((_SUMMARY, "chapter.html"), (_TITLE, None)))
    expected = json.loads(empty.read_bytes())
    expected["view"]["breadcrumbs"] = [
        {"label": _SUMMARY, "url": "chapter.html", "reading_title": dag_viewer._reading_label(_SUMMARY)},
        {"label": _TITLE, "url": None, "reading_title": dag_viewer._reading_label(_TITLE)},
    ]
    assert json.loads(marked.read_bytes()) == expected
    assert view.title == "Plain trail" and view.nodes == () and view.edges == ()


def _fixture() -> dict:
    def node(node_id: str, title: str, parent: str | None, *, scope: bool = False) -> dict:
        result = {"id": node_id, "title": title, "parent": parent,
                  "scope": node_id if scope else "chapter", "kind": "scope" if scope else "node",
                  "result": not scope, "status": "planned", "route": "https://blueprint.example.invalid/" + node_id + ".html"}
        label = dag_viewer._reading_label(title)
        if label is not None:
            result["reading_title"] = label
        return result

    return {"nodes": [
        node("roadmap", "Library", None, scope=True), node("chapter", _TITLE, "roadmap", scope=True),
        node("other-topic", r"Other $\ZZ$", "roadmap", scope=True), node("F", "Focal result", "chapter"),
        node("A", _TITLE, "chapter"), node("B", r"Custom $\ZZ/n$", "chapter"),
        node("C", _SUMMARY, "chapter"), node("D", _CODE_TITLE, "chapter"),
    ], "summary": {"text": _SUMMARY, "label": dag_viewer._reading_label(_SUMMARY)},
        "unsupported": "Raw $x$ <script>window.__titlePwned=1</script>"}


_BOOT = r"""
(function () {
  "use strict";
  function original(name) {
    const start=source.indexOf('function '+name+'(');if(start<0)throw Error('Missing production callback: '+name);
    for(let end=source.indexOf('}',start);end>=0;end=source.indexOf('}',end+1)) {
      try {return eval('('+source.slice(start,end+1)+')');}
      catch(error) {if(!(error instanceof SyntaxError))throw error;}
    }
    throw Error('Unparseable production callback: '+name);
  }
  const pluralStart=source.indexOf('  var plural ='),pluralLine=source.slice(pluralStart,source.indexOf('\n',pluralStart));
  var plural=eval('(function(){'+pluralLine+';return plural;})()');
  var element=original('element'),button=original('button'),readingLabel=original('readingLabel');
  var safeHref=original('safeHref'),graphAt=original('graphAt');
  var refreshReadingLabels=original('refreshReadingLabels'),semanticParent=original('semanticParent');
  var routeTrail=original('routeTrail'),currentOverviewGroup=original('currentOverviewGroup');
  var trailList=original('trailList'),renderBreadcrumb=original('renderBreadcrumb');
  var showError=original('showError');
  var neighborIds=original('neighborIds'),relationCounts=original('relationCounts'),topicPeers=original('topicPeers');
  var renderOutline=original('renderOutline'),renderNavigation=original('renderNavigation');
  var renderReaderContext=original('renderReaderContext'),renderWikiContext=original('renderWikiContext'),renderInspector=original('renderInspector');
  var records=new Map(fixture.nodes.map(node=>[node.id,node])),sharedIndex={nodes:fixture.nodes,relations:[['A','F',2,3],['F','B',0,4]]};
  var currentBundle={nodeId:'F',scope:'chapter'},ui$selected=null,ui$requestedNode='F',ui$outlineLimit=120;
  var ui$overviewGroup=null,ui$scopeOverview=false,ui$area=null,data={};
  var ui$wikiLinks=null,ui$wikiRendered=null,ui$wikiNode=null;
  var dom$navigationInner=element('div','fixture-outline'),dom$navigationNodes=new Map();
  var dom$readingContext=element('div','fixture-context'),dom$readingNeighbors=element('div','fixture-neighbors');
  var dom$readingBody=element('article','fixture-body'),dom$homeLink=element('a','fixture-home');
  var dom$error=element('div','fixture-error'),dom$announcement=element('div','fixture-announcement');dom$error.hidden=true;
  var dom$breadcrumb=element('nav','bp-dag-breadcrumb');dom$breadcrumb.setAttribute('aria-label','Explorer context');
  var root=element('section','fixture-labels');root.id='explorer-title-proof';
  root.appendChild(dom$breadcrumb);
  root.appendChild(dom$navigationInner);root.appendChild(dom$readingContext);root.appendChild(dom$readingNeighbors);
  root.appendChild(dom$readingBody);root.appendChild(dom$homeLink);document.body.appendChild(root);
  root.appendChild(dom$error);root.appendChild(dom$announcement);
  const calls=[],observations=[];
  var navigation={navigateNode(id,options){calls.push({id,options});},navigate(url,options){calls.push({url,options});},declutterReader(){}};
  function check(condition,message) {if(!condition)throw Error('Reading-label proof: '+message);}
  function remember(){}function syncSheetAccessibility(){}function setMode(){throw Error('Reading-label proof should not change display mode');}
  function renderFor(bundle){const previous=currentBundle;try{currentBundle=bundle;renderBreadcrumb();}finally{currentBundle=previous;}}
  const canonicalTrail={nodeId:'C',scope:'chapter',graph:{view:{}}},unchangedRecords=JSON.stringify(fixture.nodes);
  const trailEntries=routeTrail(canonicalTrail),refreshStart=(window.__titleRefreshScopes||[]).length;
  renderFor(canonicalTrail);
  if(window.__titleRefreshScopes)check(window.__titleRefreshScopes.length===refreshStart+1&&
    window.__titleRefreshScopes.at(-1)===dom$breadcrumb,'Breadcrumb replacement did not refresh only its own subtree');
  check(JSON.stringify(fixture.nodes)===unchangedRecords,'Breadcrumb rendering mutated canonical records or label parts');
  renderNavigation();renderInspector();
  const summary=readingLabel('p','fixture-summary',fixture.summary.text,fixture.summary.label);root.appendChild(summary);
  const unsupported=readingLabel('a','fixture-unsupported',fixture.unsupported,null);unsupported.href='https://blueprint.example.invalid/fallback.html';root.appendChild(unsupported);
  refreshReadingLabels(root);
  const outline=dom$navigationInner.querySelector('[data-navigation-node-id="chapter"]');
  // The redesign lists a result's relations once, in the statement itself, so the related title read here is a topic peer's.
  const related=dom$readingNeighbors.querySelector('.bp-reading-topic-list a');
  const ownerSummary=dom$readingNeighbors.querySelector('.bp-reading-related-topics summary');
  const ancestor=dom$breadcrumb.querySelector('a[href="https://blueprint.example.invalid/chapter.html"]');
  const currentCrumb=dom$breadcrumb.querySelector('[aria-current="page"]');
  const originals=[outline,related,ancestor],hrefs=originals.map(link=>link.href);
  outline.focus();
  function exercise(link) {
    const count=calls.length;
    for(const setting of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1}]) {
      const listener=event=>{observations.push(event.defaultPrevented);event.preventDefault();};
      link.addEventListener('click',listener,{once:true});
      link.dispatchEvent(new window.MouseEvent('click',{bubbles:true,cancelable:true,...setting}));
      check(calls.length===count,'Modified canonical click was intercepted');
      check(observations.at(-1)===false,'Modified click did not remain native before the test navigation shield');
    }
    link.dispatchEvent(new window.MouseEvent('click',{bubbles:true,cancelable:true,button:0}));
    check(calls.length===count+1,'Normal canonical click lost its production navigation handler');
  }
  function markupProof() {
    check(!root.querySelector('script,img,iframe,object,style'),'Provider markup became live HTML');
    check(window.__titlePwned===undefined,'Provider code executed');
    check(originals.every((link,n)=>link.isConnected&&link.href===hrefs[n]),'Canonical anchor or href was replaced');
    check(!outline.classList.contains('arithmatex')&&!related.classList.contains('arithmatex'),'An interactive anchor itself was marked');
    check(ownerSummary&&ownerSummary.isConnected&&!ownerSummary.classList.contains('arithmatex'),'The native owner summary was replaced or itself marked');
    check(ancestor&&!ancestor.classList.contains('arithmatex'),'A breadcrumb anchor itself was marked');
    check(currentCrumb&&currentCrumb.isConnected&&!currentCrumb.classList.contains('arithmatex')&&
      !currentCrumb.querySelector('a,button,summary')&&!currentCrumb.hasAttribute('tabindex'),'The current crumb became an interactive control or was replaced');
    check(currentCrumb.querySelector('code').textContent==='$literal$','Current breadcrumb code was interpreted as math');
    check(root.querySelector('code').textContent==='$literal$','Code-literal TeX changed');
    check(unsupported.textContent===fixture.unsupported&&!unsupported.querySelector('.arithmatex'),'Unsupported label did not stay literal');
  }
  window.__explorerTitleProof={root,outline,related,summary,ownerSummary,ancestor,currentCrumb,breadcrumb:dom$breadcrumb,
    trailEntries,renderFor,graphAt,showError,error:dom$error,announcement:dom$announcement,
    calls,observations,readingLabel,markupProof,exercise,
    checkRendered(){markupProof();check(document.activeElement===outline,'Actual typesetting moved anchor focus');
      check(outline.querySelector('mjx-container')&&related.querySelector('mjx-container')&&summary.querySelector('mjx-container')&&ownerSummary.querySelector('mjx-container'),
        'Actual outline, related title, or owner/paragraph summary has no MathJax glyph output');
      check(ancestor.querySelector('mjx-container')&&currentCrumb.querySelector('mjx-container'),
        'Actual linked ancestor or current breadcrumb has no real MathJax glyph output');
      check(!dom$breadcrumb.querySelector('[tabindex],[role="application"]'),'Breadcrumb math acquired a menu tabstop or control keys');
      check(!root.querySelector('code mjx-container'),'Code-literal TeX was typeset');
      check(!outline.querySelector('[tabindex]')&&!related.querySelector('[tabindex]'),'Formula took an inner control tabstop');
      check(!outline.querySelector('[role="application"]')&&!related.querySelector('[role="application"]'),'Formula took control keys');
      check(!ownerSummary.querySelector('[tabindex],[role="application"]'),'Formula took the native summary tabstop or keys');
    }};
  markupProof();
})();
"""


def _boot(fallback: dict | None = None) -> str:
    fixture = _fixture()
    if fallback is not None:
        fixture["fallback"] = fallback
    return "var source=" + json.dumps(_CLIENT.read_text(encoding="utf-8")) + ";\nvar fixture=" + json.dumps(fixture) + ";\n" + _BOOT


def _dom_script(body: str, fallback: dict | None = None) -> None:
    unavailable = pytest.fail if os.environ.get("AUTOFORM_REQUIRE_MATHJAX_TESTS") == "1" else pytest.skip
    if _NODE is None:
        unavailable("Node is required for connected reading-label DOM checks")
    bridge = _ROOT / "node_modules/jsdom/package.json"
    if not bridge.is_file() or json.loads(bridge.read_text()).get("version") != "26.1.0":
        unavailable("Locked jsdom 26.1.0 is required for connected reading-label DOM checks")
    script = "const {JSDOM}=require(" + json.dumps(str(bridge.parent)) + ");\n"
    script += "const dom=new JSDOM('<html><body></body></html>',{url:'https://blueprint.example.invalid/',runScripts:'outside-only',pretendToBeVisual:true});\n"
    script += "const window=dom.window,document=window.document;\n" + _boot(fallback) + "\n" + body + "\ndom.window.close();\n"
    result = subprocess.run([_NODE], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-6000:]


def test_actual_reading_renderers_mark_inner_math_without_replacing_canonical_controls() -> None:
    _dom_script(r"""
      const proof=window.__explorerTitleProof;
      if(!proof.outline.querySelector('.arithmatex')||!proof.related.querySelector('.arithmatex'))throw Error('Production title marking absent');
      if(!proof.summary.querySelector('.arithmatex'))throw Error('Production summary marking absent');
      if(!proof.ownerSummary.querySelector('.arithmatex'))throw Error('Actual More-in owner summary marking absent');
      if(!proof.ancestor.querySelector('.arithmatex')||!proof.currentCrumb.querySelector('.arithmatex'))throw Error('Actual breadcrumb title marking absent');
      if(proof.trailEntries[1].reading_title.source!=='Groups $\\mathbb{Z}/n$'||
        proof.trailEntries.at(-1).reading_title.source!==fixture.summary.text)throw Error('Canonical breadcrumb metadata/source changed');
      if(proof.outline.href!=='https://blueprint.example.invalid/chapter.html'||proof.related.href!=='https://blueprint.example.invalid/A.html')throw Error('Canonical href changed');
      proof.exercise(proof.outline);
      if(proof.calls[0].id!=='chapter')throw Error('Outline navigation did not keep its canonical scope');
      proof.calls.length=0;
      proof.exercise(proof.ancestor);
      if(proof.calls[0].url!=='https://blueprint.example.invalid/chapter.html'||
        JSON.stringify(proof.calls[0].options)!==JSON.stringify({history:'push',focus:true,mapFirst:true}))throw Error('Native breadcrumb route options changed');
    """)


def test_actual_graph_fallback_breadcrumbs_keep_published_parts_and_canonical_urls(tmp_path: Path) -> None:
    path = tmp_path / "fallback.json"
    dag_viewer.write_payload(path, GraphView(kind="full", title="Fallback", nodes=(), edges=()), links={},
                             breadcrumbs=((_SUMMARY, "chapter.html"), (_TITLE, None)))
    _dom_script(r"""
      const proof=window.__explorerTitleProof,before=JSON.stringify(fixture.fallback);
      const graph=proof.graphAt(fixture.fallback,'https://blueprint.example.invalid/graph.json');
      if(JSON.stringify(graph.view.breadcrumbs.map(crumb=>crumb.reading_title))!==
        JSON.stringify(fixture.fallback.view.breadcrumbs.map(crumb=>crumb.reading_title)))throw Error('graphAt dropped published reading parts');
      proof.renderFor({nodeId:'absent',scope:'absent',graph});
      const link=proof.breadcrumb.querySelector('a'),current=proof.breadcrumb.querySelector('[aria-current="page"]');
      if(link.href!=='https://blueprint.example.invalid/chapter.html'||!link.querySelector('.arithmatex')||
        link.querySelector('code').textContent!=='$literal$'||!current.querySelector('.arithmatex'))throw Error('Actual fallback breadcrumb rendering failed');
      if(JSON.stringify(fixture.fallback)!==before)throw Error('Fallback projection mutated original graph payload');
    """, json.loads(path.read_bytes()))


def test_invalid_reading_metadata_keeps_literal_provider_text_and_prefix() -> None:
    _dom_script(r"""
      const proof=window.__explorerTitleProof,text='Groups $x$';
      const label={format:'inline-parts-v1',source:text,parts:[['math','\(x\)']]};
      const cases=[null,{...label,format:'wrong'},{...label,source:'different'},
        {...label,parts:[]},{...label,parts:[['text','not the source']]},
        {...label,parts:[['script','alert(1)']]},{...label,parts:[['math','<script>bad</script>']]},
        {...label,parts:Array.from({length:257},()=>['math','\(x\)'])},
        {...label,parts:[['math','\('+'x'.repeat(32769)+'\)']]}];
      for(const invalid of cases) {
        const node=proof.readingLabel('a','fixture-invalid',text,invalid,'← ');
        if(node.textContent!=='← '+text||node.children.length)throw Error('Invalid provider metadata was interpreted');
      }
      proof.markupProof();
    """)


def test_failed_view_error_offers_only_safe_plain_native_canonical_navigation() -> None:
    _dom_script(r"""
      const proof=window.__explorerTitleProof,failed=fixture.nodes.find(node=>node.id==='B').route;
      const message='<img src=x onerror="window.__errorPwned=1"> <script>bad()</script> $not_error_math$';
      const recordsBefore=JSON.stringify(fixture.nodes),body=proof.root.querySelector('.fixture-body');
      const original=document.createTextNode(fixture.summary.text);body.appendChild(original);
      const bodyBefore=body.innerHTML;
      proof.showError({url:failed,message});
      const link=proof.error.querySelector('a');
      if(proof.error.hidden||!link||link.href!==failed||!link.textContent.trim()||link.childElementCount)
        throw Error('Failed view did not offer a plain canonical fallback');
      if(link.target!=='_blank'||!link.relList.contains('noopener')||
        (link.getAttribute('aria-label')||link.textContent)!=='Open complete article in a new tab')
        throw Error('Fallback did not disclose and guarantee a fresh, isolated native tab');
      if(!proof.error.textContent.includes(message)||!proof.announcement.textContent.includes(message)||
        proof.error.querySelector('img,script,iframe,.arithmatex,mjx-container')||window.__errorPwned)
        throw Error('Error/provider text became active HTML or math');
      if(/current view.*unchanged/i.test(proof.error.textContent+' '+proof.announcement.textContent))
        throw Error('Renderer failure falsely promised that the previous view was unchanged');
      const count=proof.calls.length;
      for(const setting of [{button:0},{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1}]) {
        // Observe the genuine callback/default state before shielding JSDOM's
        // unsupported document navigation; do not supply an SPA callback.
        link.addEventListener('click',event=>{proof.observations.push(event.defaultPrevented);event.preventDefault();},{once:true});
        link.dispatchEvent(new window.MouseEvent('click',{bubbles:true,cancelable:true,...setting}));
        if(proof.observations.at(-1)!==false||proof.calls.length!==count)throw Error('Fallback was intercepted as SPA navigation');
      }
      const unsafe=[null,undefined,false,17,[],{toString(){window.__fallbackURLCoerced=true;return failed;}},
        'https://evil.example.invalid/B.html','//evil.example.invalid/B.html',
        'https://user:secret@blueprint.example.invalid/B.html','https://:secret@blueprint.example.invalid/B.html',
        'javascript:bad()','data:text/html,bad','file:///B.html',
        ...['\x00','\x1f','\t','\n','\r','\x7f'].map(control=>failed+control)];
      for(const url of unsafe){proof.showError({url,message});if(proof.error.querySelector('a'))throw Error('Unsafe fallback href was emitted');}
      if(window.__fallbackURLCoerced||JSON.stringify(fixture.nodes)!==recordsBefore||
        body.firstChild!==original||body.innerHTML!==bodyBefore)throw Error('Failure rendering coerced input or mutated mathematical source');
      proof.markupProof();
    """)


def test_actual_safe_mathjax_typesets_outline_related_and_summary_with_project_macros(tmp_path: Path) -> None:
    mathjax_package()
    script, warnings = mathjax_script(None, json.dumps({"ZZ": r"\mathbb{Z}"}).encode())
    assert warnings == []
    # This is a transparent observation wrapper: every call awaits the real
    # safe service, then checks its actual glyphs/control identity. No math pass
    # is replaced, and JSDOM outside-only alone is not an inertness proof.
    wrapper = r"""
      (function(){
        const actual=window.AutoformMath.refresh;
        window.__titleRefreshScopes=[];
        window.AutoformMath.refresh=function(root){window.__titleRefreshScopes.push(root);return actual(root).then(value=>{
          if(window.__explorerTitleProof)window.__explorerTitleProof.checkRendered();return value;
        });};
      })();
    """
    after = r"""
      window.AutoformMath.refresh(document).then(()=>{
        const proof=window.__explorerTitleProof;proof.checkRendered();proof.exercise(proof.outline);
        proof.exercise(proof.related);proof.exercise(proof.ancestor);
        const count=window.__titleRefreshScopes.length;
        proof.showError({url:fixture.nodes.find(node=>node.id==='B').route,message:'Failure $not_error_math$ <script>bad()</script>'});
        if(window.__titleRefreshScopes.length!==count||proof.error.querySelector('.arithmatex,mjx-container,script'))
          throw Error('Error fallback added a math pass or interpreted provider text');
        const link=proof.error.querySelector('a');
        if(link.target!=='_blank'||!link.relList.contains('noopener'))throw Error('Native error recovery reused the failed document');
      }).catch(error=>window.console.error('reading-label real output proof failed',error.stack));
    """
    script += wrapper + _boot() + after
    first = '<html><head></head><body><p id="earlier"><span class="arithmatex">\\(x^2\\)</span></p><p>Unmarked $not_read$.</p></body></html>'
    second = '<html><head></head><body><p><span class="arithmatex">\\(y^2\\)</span></p></body></html>'
    report = _node_report(tmp_path, script, "new", first=first, second=second, then="append")
    assert report["version"] == "3.2.2" and report["errors"] == [] and report["early"] is True
    first_pass = report["passes"][0]
    math = [item for document in first_pass for item in document["math"]]
    assert any(item["tex"] == r"\mathbb{Z}/n" for item in math)
    custom = [item for item in math if item["tex"] == r"\ZZ/n"]
    assert custom and all("merror" not in item["mml"] and 'mathcolor="red"' not in item["mml"] for item in custom)
    assert not any(item["tex"] in {"literal", "not_read"} for item in math)
    assert all(document["enableMenu"] is False for document in first_pass if any(
        item["tex"] == r"\mathbb{Z}/n" for item in document["math"]
    ))
    assert report["controls"] and all(control["tabstops"] == 0 and control["applications"] == 0 for control in report["controls"])
    assert report["startup"] == 0 and report["unstyled"] == []
