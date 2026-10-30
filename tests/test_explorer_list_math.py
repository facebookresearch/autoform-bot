"""Actual list-preview callbacks with manual DOM/timer peers, not native math proof."""

from __future__ import annotations

import json

import pytest

from autoform_cli import markdown
from tests.test_qa_outline_navigation import _run
from tests.test_wiki_workspace_controls import _CONTROL_PEERS, _NODE

pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for bounded list-math callbacks")

_CALLBACKS = [
    "element", "summaryLine", "pendingMath", "typesetRowsInView", "rowMathCurrent",
    "queueRowMath", "fallbackRowMath", "stopRowMath", "typesetRevealedRows",
]
_PEERS = (
    _CONTROL_PEERS.replace("buildShell();bindEvents();syncControls();", "")
    + r"""
// Extend the existing DOM peer only for the selectors/connectivity used here.
// An engine-owned container below is a structural sentinel, never glyph evidence.
const simpleSelect=Element.prototype.querySelectorAll;
Element.prototype.querySelectorAll=function(selector){
  const matches=selector.split(',').flatMap(part=>{
    const excluded=part.trim().endsWith(':not(:has(mjx-container))');
    const simple=excluded?part.trim().slice(0,-':not(:has(mjx-container))'.length):part.trim();
    return simpleSelect.call(this,simple).filter(node=>!excluded||!node.querySelector('mjx-container'));
  });
  return this.all().filter(node=>matches.includes(node));
};
const replace=Element.prototype.replaceChildren;
Element.prototype.replaceChildren=function(...children){
  this.children.forEach(child=>child.parentElement=null);replace.call(this,...children);
};
Object.defineProperty(Element.prototype,'isConnected',{get(){
  return this===document||this===document.body||Boolean(this.parentElement&&this.parentElement.isConnected);
}});
document.body.appendChild(host);
dom$inventoryList=new Element('ul');dom$inventoryScroll=new Element('section');host.appendChild(dom$inventoryScroll);
dom$inventoryScroll.appendChild(dom$inventoryList);
var alive=true,ui$mode='list';
const timers=new Map(),timerRecords=new Map(),cleared=[],refreshes=[],sourceMath=new WeakMap();
let nextTimer=0;
function setTimeout(callback,delay){
  const id=nextTimer++,timer={callback,delay};timers.set(id,timer);timerRecords.set(id,timer);return id;
}
function clearTimeout(id){cleared.push(id);timers.delete(id);}
function fireTimer(id){const timer=timers.get(id);assert(timer,'Missing scheduled timer '+id);timers.delete(id);timer.callback();}
function racedTimer(id){assert(timerRecords.has(id));timerRecords.get(id).callback();}
async function settled(){for(let count=0;count<8;count++)await Promise.resolve();}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
var IntersectionObserver=class Observer {
  constructor(callback,options){this.callback=callback;this.options=options;this.targets=new Set();this.removed=[];this.disconnected=false;observers.push(this);}
  observe(row){this.targets.add(row);}
  unobserve(row){this.targets.delete(row);this.removed.push(row);}
  disconnect(){this.disconnected=true;this.targets.clear();}
  deliver(entries){this.callback(entries);}
};
function visible(rows){return rows.map(target=>({target,isIntersecting:true}));}
function rows(){return dom$inventoryList.querySelectorAll('.bp-dag-inventory-row');}
function spans(row){return row.querySelectorAll('.bp-math-pending, .arithmatex, .bp-math-fallback');}
function populate(summaries){
  const nodes=summaries.map((summary,index)=>({id:'chapter/r'+index,parent:'chapter',scope:'chapter',
    title:'Row '+index,summary,result:true,status:'planned',lean:'Fixture.r'+index}));
  sharedIndex={schema:'autoform-dag-search/v1',nodes,relations:nodes.length>1?[['chapter/r0','chapter/r1',2,3]]:[]};
  const wire=JSON.stringify(sharedIndex);records.clear();nodes.forEach(node=>records.set(node.id,node));
  currentBundle={nodeId:'chapter',scope:'chapter',isCurrent:()=>true};
  dom$inventoryList.replaceChildren(...nodes.map(node=>{
    const row=new Element('li');row.className='bp-dag-inventory-row';row.dataset.inventoryNodeId=node.id;
    const line=summaryLine(node);sourceMath.set(row,line.querySelectorAll('.bp-math-pending').map(span=>span.textContent));
    row.appendChild(line);return row;
  }));
  return {nodes,wire};
}
function unchanged(fixture){assert.equal(JSON.stringify(sharedIndex),fixture.wire,'Preview callbacks changed index bytes');}
function literal(rows){
  rows.forEach(row=>{assert.deepEqual(spans(row).map(span=>span.textContent),sourceMath.get(row),'Fallback lost authored formula text');
    spans(row).forEach(span=>{
    assert.equal(span.className,'bp-math-fallback');assert.equal(span.getAttribute('aria-hidden'),null);
    assert.equal(span.hidden,false);assert(span.textContent.length>0,'Fallback lost visible/spoken source text');
  });});
}
function engine(action){
  const processed=new Set();
  window.AutoformMath={refresh(root){
    assert.strictEqual(root,dom$inventoryList);
    const batch=rows().filter(row=>row.querySelector('.arithmatex')&&!processed.has(row));
    batch.forEach(row=>processed.add(row));refreshes.push(batch.map(row=>row.dataset.inventoryNodeId));
    return action? action(root,batch):Promise.resolve(true);
  }};
}
"""
)


def _callbacks(body: str) -> None:
    _run(body, _CALLBACKS, _PEERS)


@pytest.mark.parametrize(("source", "text", "math", "code"), [
    ("$5 and $10", "$5 and $10", [], []),
    (r"Pay \$5 and \$10", "Pay $5 and $10", [], []),
    (r"\$x\$ and $y$", r"$x$ and \(y\)", [r"\(y\)"], []),
    ("Use ``$x$ and `tick` `` before $y$.", r"Use $x$ and `tick` before \(y\).", [r"\(y\)"], ["$x$ and `tick`"]),
    ("Use ```one `` two `$x$` ``` then $y$.", r"Use one `` two `$x$` then \(y\).", [r"\(y\)"], ["one `` two `$x$`"]),
    ("Use ``$x$``` then $y$.", "Use ``$x$``` then $y$.", [], []),
    ("Use ```$x$`` then $y$.", "Use ```$x$`` then $y$.", [], []),
    (r"Use \`literal \$5\` and $x$", r"Use `literal $5` and \(x\)", [r"\(x\)"], []),
    (r"A sequence $u:\mathbb{N}$ and \(p\).", r"A sequence \(u:\mathbb{N}\) and \(p\).", [r"\(u:\mathbb{N}\)", r"\(p\)"], []),
    (r"Do not parse `unfinished $x$ or \(y\).", r"Do not parse `unfinished $x$ or \(y\).", [], []),
    ("Use $x$, then `unfinished", "Use $x$, then `unfinished", [], []),
    ("Use `across\nrows` and $x$", "Use `across\nrows` and $x$", [], []),
])
def test_preview_formats_only_supported_spans_and_keeps_exact_authored_index(
    source: str, text: str, math: list[str], code: list[str],
) -> None:
    _callbacks("const expected=" + json.dumps({"source": source, "text": text, "math": math, "code": code}) + ";\n" + r"""
      const fixture=populate([expected.source]),line=rows()[0].children[0];
      assert.equal(line.textContent,expected.text);
      assert.deepEqual(line.querySelectorAll('.bp-math-pending').map(span=>span.textContent),expected.math);
      assert.deepEqual(line.querySelectorAll('code').map(span=>span.textContent),expected.code);
      line.querySelectorAll('.bp-math-pending').forEach(span=>assert.equal(span.getAttribute('aria-hidden'),'true'));
      assert.equal(fixture.nodes[0].summary,expected.source);unchanged(fixture);
    """)


@pytest.mark.parametrize("source", [
    r"A sequence $u:\mathbb{N}$ and \(p\), use `mu0`.",
    "Use ``$literal$ and `tick` `` before $y$.",
    "Use ```one `` two``` and $x$.",
])
def test_supported_inline_math_and_code_match_the_real_site_converter(source: str) -> None:
    tree = markdown.render_tree(source)
    assert tree is not None
    expected = {
        "source": source,
        "math": ["".join(node.itertext()) for node in tree.iter() if "arithmatex" in node.get("class", "").split()],
        "code": ["".join(node.itertext()) for node in tree.iter("code")],
    }
    _callbacks("const expected=" + json.dumps(expected) + ";\n" + r"""
      const fixture=populate([expected.source]),line=rows()[0].children[0];
      assert.deepEqual(line.querySelectorAll('.bp-math-pending').map(span=>span.textContent),expected.math);
      assert.deepEqual(line.querySelectorAll('code').map(span=>span.textContent),expected.code);unchanged(fixture);
    """)


def test_duplicate_title_prefix_is_display_only_and_pending_source_is_explicitly_hidden() -> None:
    _callbacks(r"""
      const node={title:'A sequence',summary:'A sequence. A term $x$ and `mu0`.'},before=JSON.stringify(node);
      const line=summaryLine(node);assert.equal(line.textContent,'A term \\(x\\) and mu0.');
      const pending=pendingMath('\\(x\\)');assert.equal(pending.className,'bp-math-pending');
      assert.equal(pending.getAttribute('aria-hidden'),'true');assert.equal(pending.textContent,'\\(x\\)');
      assert.equal(JSON.stringify(node),before);
    """)


def test_observation_coalesces_timer_zero_and_each_pass_exposes_at_most_six_rows() -> None:
    _callbacks(r"""
      const fixture=populate(Array.from({length:14},(_,index)=>'Value $x_'+index+'$.'));engine();typesetRowsInView();
      const job=ui$mathJob,observer=job.$observer,all=rows();
      assert.strictEqual(observer.options.root,dom$inventoryScroll);assert.equal(observer.targets.size,14);
      observer.deliver([{target:all[0],isIntersecting:false}]);assert.equal(job.$timer,null);
      observer.deliver(visible(all));assert.equal(job.$timer,0);assert.equal(timers.size,1);
      observer.deliver(visible(all));queueRowMath(job,0);assert.equal(job.$timer,0);assert.equal(timers.size,1);
      assert.equal(job.$queue.length,14);assert.equal(observer.removed.length,14);
      fireTimer(job.$timer);assert.deepEqual(refreshes[0],all.slice(0,6).map(row=>row.dataset.inventoryNodeId));
      assert.equal(job.$busy,true);assert.equal(all.filter(row=>row.querySelector('.bp-math-pending')).length,8);
      queueRowMath(job,0);assert.equal(job.$timer,null,'Busy passes must not schedule duplicate work');
      await settled();fireTimer(job.$timer);await settled();fireTimer(job.$timer);await settled();
      assert.deepEqual(refreshes.map(batch=>batch.length),[6,6,2]);assert.equal(job.$queue.length,0);
      assert.equal(job.$busy,false);assert.equal(job.$timer,null);assert.equal(timers.size,0);unchanged(fixture);
    """)


def test_observer_absence_keeps_the_same_six_row_batch_and_stale_rows_are_not_typeset() -> None:
    _callbacks(r"""
      IntersectionObserver=undefined;
      const fixture=populate(Array.from({length:9},()=>'$x$'));engine();typesetRowsInView();
      const job=ui$mathJob,all=rows();all.slice(0,6).forEach(row=>row.remove());
      assert(all.slice(0,6).every(row=>!row.isConnected));
      fireTimer(job.$timer);assert.equal(refreshes.length,0);assert.equal(job.$queue.length,3);
      fireTimer(job.$timer);await settled();
      assert.deepEqual(refreshes,[all.slice(6).map(row=>row.dataset.inventoryNodeId)]);unchanged(fixture);
      stopRowMath();assert.equal(timers.size,0);
    """)


@pytest.mark.parametrize("changed", ["mode", "route", "generation", "disposed"])
def test_stale_scheduled_jobs_cannot_typeset_and_leave_honest_literal_source(changed: str) -> None:
    _callbacks("const changed=" + json.dumps(changed) + ";\n" + r"""
      const fixture=populate(['Value $x$.','Value \\(y\\).']);engine();typesetRowsInView();
      const job=ui$mathJob,observer=job.$observer;observer.deliver(visible(rows()));const timer=job.$timer;
      if(changed==='mode')ui$mode='sources';else if(changed==='route')currentBundle={nodeId:'other',scope:'other'};
      else if(changed==='generation')currentBundle.isCurrent=()=>false;else alive=false;
      assert.equal(rowMathCurrent(job),false);queueRowMath(job,0);assert.equal(job.$timer,timer);
      fireTimer(timer);assert.equal(refreshes.length,0);assert.equal(ui$mathJob,null);
      assert.equal(observer.disconnected,true);assert.equal(timers.size,0);literal(rows());unchanged(fixture);
    """)


@pytest.mark.parametrize("failure", ["absent", "throws", "rejects", "false", "stalls"])
def test_failed_or_stalled_engine_exposes_literal_source_and_cleans_up(failure: str) -> None:
    _callbacks("const failure=" + json.dumps(failure) + ";\n" + r"""
      const fixture=populate(['Value $x$.','Value \\(y\\).']);
      if(failure==='absent')delete window.AutoformMath;
      else engine(()=>{if(failure==='throws')throw Error('Unavailable engine');
        if(failure==='rejects')return Promise.reject(Error('Unavailable engine'));
        if(failure==='false')return Promise.resolve(false);return new Promise(()=>{});});
      typesetRowsInView();
      const job=ui$mathJob;
      if(job){job.$observer.deliver(visible(rows()));fireTimer(job.$timer);await settled();
        if(failure==='stalls'){assert.equal(timerRecords.get(job.$deadline).delay,2000);fireTimer(job.$deadline);}}
      assert.equal(ui$mathJob,null);if(job)assert.equal(job.$observer.disconnected,true);
      assert.equal(timers.size,0);literal(rows());unchanged(fixture);
    """)


def test_service_disappearing_after_observation_falls_back_before_an_engine_call() -> None:
    _callbacks(r"""
      const fixture=populate(['$x$']);engine();typesetRowsInView();const job=ui$mathJob;
      job.$observer.deliver(visible(rows()));delete window.AutoformMath;fireTimer(job.$timer);
      assert.equal(refreshes.length,0);assert.equal(ui$mathJob,null);assert.equal(job.$observer.disconnected,true);
      assert.equal(timers.size,0);literal(rows());unchanged(fixture);
    """)


def test_same_job_generation_loss_during_engine_completion_cleans_remaining_hidden_rows() -> None:
    _callbacks(r"""
      const fixture=populate(Array.from({length:8},()=>'$x$')),pass=deferred();engine(()=>pass.promise);typesetRowsInView();
      const job=ui$mathJob;job.$observer.deliver(visible(rows()));fireTimer(job.$timer);
      assert.equal(job.$queue.length,2);assert.equal(job.$busy,true);
      currentBundle.isCurrent=()=>false;pass.resolve(true);await settled();
      assert.equal(ui$mathJob,null);assert.equal(job.$observer.disconnected,true);assert.equal(timers.size,0);
      literal(rows());unchanged(fixture);
    """)


def test_stop_restores_unrendered_formulas_but_retains_engine_owned_output_and_code() -> None:
    _callbacks(r"""
      const fixture=populate(['$x$ and `$literal$`','$y$','$z$']);engine();typesetRowsInView();
      const job=ui$mathJob,all=rows(),rendered=spans(all[0])[0],unrendered=spans(all[1])[0];
      const owned=new Element('mjx-container');rendered.className='arithmatex';rendered.removeAttribute('aria-hidden');rendered.appendChild(owned);
      unrendered.className='arithmatex';unrendered.removeAttribute('aria-hidden');
      job.$observer.deliver(visible(all));assert.equal(job.$timer,0);stopRowMath();
      assert.equal(ui$mathJob,null);assert.equal(timers.size,0);assert.equal(job.$observer.disconnected,true);
      assert.equal(rendered.className,'arithmatex');assert.strictEqual(rendered.children[0],owned);
      assert.equal(all[0].querySelector('code').textContent,'$literal$');literal(all.slice(1));
      const before=all.map(row=>row.textContent);stopRowMath();assert.deepEqual(all.map(row=>row.textContent),before);unchanged(fixture);
    """)


@pytest.mark.parametrize("outcome", ["resolve", "reject"])
def test_old_engine_observer_and_timer_completions_do_not_mutate_a_new_job(outcome: str) -> None:
    _callbacks("const outcome=" + json.dumps(outcome) + ";\n" + r"""
      populate(['$old$']);const oldPass=deferred();engine(()=>oldPass.promise);typesetRowsInView();
      const old=ui$mathJob;old.$observer.deliver(visible(rows()));const oldTimer=old.$timer;
      fireTimer(oldTimer);const oldDeadline=old.$deadline;stopRowMath();
      const fixture=populate(['$new$']),newPass=deferred();engine(()=>newPass.promise);typesetRowsInView();
      const current=ui$mathJob;current.$observer.deliver(visible(rows()));fireTimer(current.$timer);
      const state={observer:current.$observer,queue:current.$queue,timer:current.$timer,deadline:current.$deadline,busy:current.$busy};
      if(outcome==='resolve')oldPass.resolve(true);else oldPass.reject(Error('Old failure'));await settled();
      old.$observer.deliver(visible(rows()));racedTimer(oldTimer);racedTimer(oldDeadline);
      assert.strictEqual(ui$mathJob,current);assert.strictEqual(current.$observer,state.observer);
      assert.strictEqual(current.$queue,state.queue);assert.equal(current.$timer,state.timer);
      assert.equal(current.$deadline,state.deadline);assert.equal(current.$busy,state.busy);
      assert.equal(current.$observer.disconnected,false);assert(timers.has(current.$deadline));unchanged(fixture);
      newPass.resolve(true);await settled();stopRowMath();assert.equal(timers.size,0);
    """)


def test_an_old_observer_after_graph_entry_cannot_start_new_list_work() -> None:
    _callbacks(r"""
      const fixture=populate(['$x$']);engine();typesetRowsInView();const job=ui$mathJob;
      ui$mode='graph';job.$observer.deliver(visible(rows()));
      assert.equal(ui$mathJob,null);assert.equal(job.$observer.disconnected,true);assert.equal(refreshes.length,0);
      assert.equal(timers.size,0);literal(rows());unchanged(fixture);
    """)


def test_actual_graph_controls_cancel_the_owned_list_job_without_changing_index() -> None:
    _run(r"""
      buildShell();
      const fixture=populate(['$x$']);engine();typesetRowsInView();const job=ui$mathJob;
      job.$observer.deliver(visible(rows()));ui$mode='graph';syncControls();
      assert.equal(ui$mathJob,null);assert.equal(job.$observer.disconnected,true);assert.equal(timers.size,0);
      assert.equal(refreshes.length,0);literal(rows());unchanged(fixture);
    """, _CALLBACKS + [
        "button", "readingLabel", "safeHref", "buildShell", "syncControls", "wrapMapKey", "currentOverviewGroup", "trailList",
        "renderBreadcrumb", "routeTrail", "semanticParent", "refreshReadingLabels", "sourceNodes", "inCurrentScope",
    ], _PEERS)
