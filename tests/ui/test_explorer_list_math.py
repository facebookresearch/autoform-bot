"""Formulas in the statement list of a built book, in jsdom: what a row's summary previews, and how its formulas wait
for MathJax until the row is near the view, a few rows at a time, or are left as typed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli import markdown
from autoform_cli.scaffold import scaffold_project
from tests.explorer_fixture import build_site, run_book
from tests.facts_fixture import write_annotated

#: Summaries, what their row shows, its formulas and its code: only closed code spans and unambiguous formulas are
#: previewed, so a currency, an escaped dollar or an unmatched backtick stays literal.
_PREVIEWS = [
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
]
#: Summaries whose formulas and code the site's own converter, markdown.render_tree, also finds.
_CONVERTED = [
    r"A sequence $u:\mathbb{N}$ and \(p\), use `mu0`.",
    "Use ``$literal$ and `tick` `` before $y$.",
    "Use ```one `` two``` and $x$.",
]


def _theorem(title: str, summary: str) -> str:
    return f"---\ndeclaration: theorem\n---\n\n# {title}\n\n{summary}\n\n## Depends on\n\nThis has no prerequisites.\n"


@pytest.fixture(scope="module")
def book(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A book of three chapters: the summaries above, fourteen statements with a formula each, and three with formulas
    and code."""
    project = tmp_path_factory.mktemp("previews")
    scaffold_project(project, title="Previews")
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text("# Previews\n\nRows of formulas.\n", encoding="utf-8")
    chapters = {
        "summaries": [(f"case-{n:02d}", f"Case {n}", source) for n, (source, *_rest) in enumerate(_PREVIEWS)]
        + [(f"converted-{n}", f"Converted {n}", source) for n, source in enumerate(_CONVERTED)]
        + [("sequence", "A sequence", "A sequence. A term $x$ and `mu0`.")],
        "values": [(f"value-{n:02d}", f"Value {n}", f"Value $x_{{{n}}}$.") for n in range(14)],
        "code": [("first", "First", "$x$ and `$literal$`"), ("second", "Second", "$y$"), ("third", "Third", "$z$")],
    }
    for chapter, entries in chapters.items():
        (roadmap / chapter).mkdir()
        (roadmap / chapter / "README.md").write_text(f"# {chapter.title()}\n\nStatements.\n", encoding="utf-8")
        for slug, title, summary in entries:
            write_annotated(roadmap / chapter / f"{slug}.md", _theorem(title, summary))
    return build_site(project)


#: The site's math service as the list meets it, javascripts/mathjax.js not being loaded: a refresh of the list
#: records, as a pass, the rows whose formulas it is handed, which are those MathJax would read; it then draws them
#: and answers true, or answers as engine.answer says, given those formulas. Any other refresh, of the reading or the
#: trail, answers true at once. args.observer adds an IntersectionObserver, which jsdom lacks: show(rows) reports the
#: rows in view.
_ENGINE = r"""
var engine = {passes: [], answer: null}, observers = [], pages = [];
// Each formula keeps its place, its text replaced by MathJax's output, unless the page dropped it meanwhile.
function draw(maths) { maths.forEach(math => { if (math.isConnected) math.replaceChildren(element(window, 'mjx-container', {}, 'output')); }); }
window.AutoformMath = {refresh(root) {
  if (!root.classList || !root.classList.contains('bp-dag-inventory-list')) return window.Promise.resolve(true);
  const maths = $$('.arithmatex:not(:has(mjx-container))', root);
  engine.passes.push($$('.bp-dag-inventory-row', root).filter(row => maths.some(math => row.contains(math))).map(row => row.dataset.inventoryNodeId));
  if (engine.answer) return engine.answer(maths);
  draw(maths); return window.Promise.resolve(true);
}};
if (args.observer) window.IntersectionObserver = class {
  constructor(callback, options) { Object.assign(this, {callback, options, targets: new Set(), connected: true}); observers.push(this); }
  observe(target) { this.targets.add(target); }
  unobserve(target) { this.targets.delete(target); }
  disconnect() { this.connected = false; this.targets.clear(); }
  show(rows) { this.callback(rows.map(target => ({target, isIntersecting: true})), this); }
};
// Material's instant navigation, which announces each new page to its subscribers.
window.document$ = {subscribe(next) { pages.push(next); }};
"""

_LIST = r"""
const rows = () => $$('.bp-dag-inventory-row'), ids = list => list.map(row => row.dataset.inventoryNodeId);
const control = text => $$('button').find(button => button.textContent === text);
const waiting = row => Boolean(row.querySelector('.bp-math-pending'));
// The chapter has no source texts, so Browse opens its statements at once.
async function openList() { click($('.bp-dag-browse > button')); await tick(0); }
// Each formula of LIST reads as typed: shown and read aloud as its source, which a values row says in its summary.
function typed(list) {
  for (const row of list) {
    const maths = $$('.bp-math-pending, .arithmatex, .bp-math-fallback', row);
    assert(maths.length, row.dataset.inventoryNodeId);
    for (const math of maths) {
      assert.equal(math.className, 'bp-math-fallback', row.dataset.inventoryNodeId);
      assert.equal(math.getAttribute('aria-hidden'), null); assert.equal(math.hidden, false);
    }
    const n = /value-(\d+)$/.exec(row.dataset.inventoryNodeId);
    if (n) assert.equal(row.querySelector('.bp-result-summary').textContent, 'Value \\(x_{' + Number(n[1]) + '}\\).');
  }
}
"""


def _run(site: Path, page: str, body: str, *, observer: bool = False, engine: bool = True) -> None:
    run_book(site, page, _LIST + body, before=_ENGINE if engine else "", args={"observer": observer})


def test_a_summary_previews_only_closed_code_and_unambiguous_formulas(book: Path) -> None:
    table = [[f"summaries/case-{n:02d}", *case[1:]] for n, case in enumerate(_PREVIEWS)]
    table.append(["summaries/sequence", r"A term \(x\) and mu0.", [r"\(x\)"], ["mu0"]])
    _run(book, "roadmap/summaries/", "const table = " + json.dumps(table) + ";\n" + r"""
      await openList();
      for (const [id, text, maths, code] of table) {
        // A summary that opens with its row's title shows only what follows the title.
        const line = rows().find(row => row.dataset.inventoryNodeId === id).querySelector('.bp-result-summary');
        assert.equal(line.textContent, text, id);
        // Until MathJax reads it, a formula's source stays out of what is read aloud.
        assert.deepEqual($$('.bp-math-pending', line).map(math => math.textContent), maths, id);
        assert($$('.bp-math-pending', line).every(math => math.getAttribute('aria-hidden') === 'true'), id);
        assert.deepEqual($$('code', line).map(span => span.textContent), code, id);
      }
    """)


def test_a_summarys_formulas_and_code_are_those_the_sites_converter_finds(book: Path) -> None:
    table = []
    for n, source in enumerate(_CONVERTED):
        tree = markdown.render_tree(source)
        assert tree is not None
        maths = ["".join(node.itertext()) for node in tree.iter() if "arithmatex" in node.get("class", "").split()]
        table.append([f"summaries/converted-{n}", maths, ["".join(node.itertext()) for node in tree.iter("code")]])
    _run(book, "roadmap/summaries/", "const table = " + json.dumps(table) + ";\n" + r"""
      await openList();
      for (const [id, maths, code] of table) {
        const line = rows().find(row => row.dataset.inventoryNodeId === id).querySelector('.bp-result-summary');
        assert.deepEqual($$('.bp-math-pending', line).map(math => math.textContent), maths, id);
        assert.deepEqual($$('code', line).map(span => span.textContent), code, id);
      }
    """)


def test_rows_near_the_view_are_typeset_six_at_most_a_pass_one_pass_at_a_time(book: Path) -> None:
    _run(book, "roadmap/values/", r"""
      await openList(); const all = rows(), observer = observers.at(-1);
      // The list's scroller watches every row with a formula; a row out of view is not read.
      assert.equal(observer.options.root, $('.bp-dag-inventory-scroll')); assert.equal(observer.targets.size, 14);
      observer.callback([{target: all[0], isIntersecting: false}], observer); await tick(100);
      assert.deepEqual(engine.passes, []); assert(all.every(waiting));
      // Rows seen together, or seen twice, wait for one pass; a pass hands MathJax six rows at most and waits for it.
      const held = [];
      engine.answer = maths => new window.Promise(resolve => held.push(() => { draw(maths); resolve(true); }));
      observer.show(all); observer.show(all); await tick(100);
      assert.deepEqual(engine.passes, [ids(all.slice(0, 6))]); assert.equal(all.filter(waiting).length, 8);
      observer.show(all); await tick(100); assert.equal(engine.passes.length, 1);
      while (held.length) { held.shift()(); await tick(100); }
      assert.deepEqual(engine.passes, [ids(all.slice(0, 6)), ids(all.slice(6, 12)), ids(all.slice(12))]);
      assert.equal($$('mjx-container').length, 14); assert.equal(observer.targets.size, 0);
    """, observer=True)


def test_without_an_observer_every_row_waits_for_its_pass_and_rows_gone_from_the_list_are_skipped(book: Path) -> None:
    _run(book, "roadmap/values/", r"""
      await openList(); const all = rows();
      assert.deepEqual(engine.passes, []); assert(all.every(waiting));
      // Rows the list dropped before their pass are not handed to MathJax: the first pass has none left, the next six.
      all.slice(0, 6).forEach(row => row.parentElement.remove()); await tick(100);
      assert.deepEqual(engine.passes, [ids(all.slice(6, 12)), ids(all.slice(12))]);
      assert.equal($$('mjx-container').length, 8);
    """)


def test_a_pass_that_succeeded_leaves_rows_seen_later_to_their_own_passes(book: Path) -> None:
    _run(book, "roadmap/values/", r"""
      await openList(); const all = rows(), observer = observers.at(-1);
      // Six rows are seen and drawn, then the list sits idle past the two seconds a pass may take.
      observer.show(all.slice(0, 6)); await tick(100); await tick(3000);
      // The other eight, seen later, get passes of their own: none is left as typed.
      observer.show(all.slice(6)); await tick(500);
      assert.deepEqual(engine.passes, [ids(all.slice(0, 6)), ids(all.slice(6, 12)), ids(all.slice(12))]);
      assert.equal($$('mjx-container').length, 14); assert.equal($$('.bp-math-fallback').length, 0);
    """, observer=True)


def test_a_slow_pass_that_answers_within_two_seconds_is_drawn_not_given_up(book: Path) -> None:
    _run(book, "roadmap/values/", r"""
      // MathJax answers each pass after a second: slow, not stalled.
      engine.answer = maths => new window.Promise(resolve => window.setTimeout(() => { draw(maths); resolve(true); }, 1000));
      await openList(); const all = rows();
      await tick(800);
      assert.deepEqual(engine.passes, [ids(all.slice(0, 6))]); assert.equal($$('.bp-math-fallback').length, 0);
      await tick(5000);
      assert.deepEqual(engine.passes, [ids(all.slice(0, 6)), ids(all.slice(6, 12)), ids(all.slice(12))]);
      assert.equal($$('mjx-container').length, 14); assert.equal($$('.bp-math-fallback').length, 0);
    """)


@pytest.mark.parametrize("change", ["map", "statement", "pending", "gone"])
def test_a_list_that_went_stale_before_its_pass_shows_its_formulas_as_typed(book: Path, change: str) -> None:
    _run(book, "roadmap/values/", "const change = " + json.dumps(change) + ";\n" + r"""
      await openList(); const all = rows(), observer = observers.at(-1), trail = $('.bp-dag-breadcrumb a');
      observer.show(all);
      if (change === 'map') click(control('Back to map'));
      // A row opens its statement, which shows the map.
      else if (change === 'statement') click(all[3]);
      // A link is followed, and its page has not come yet.
      else if (change === 'pending') { net.hold(new URL(trail.href).pathname.slice(1)); click(trail); }
      // Material swaps the page: the book has gone, and its client with it.
      else { $('.bp-explorer-page').remove(); pages.forEach(next => next()); }
      // The map, a statement's page and the swap stop the list's work at once; a link still on its way, at the pass.
      if (change !== 'pending') { await flush(); typed(all); assert.equal(observer.connected, false); }
      // Even a later report from the list's observer starts nothing.
      observer.show(all); await tick(3000);
      assert.deepEqual(engine.passes, []); assert.equal(observer.connected, false); typed(all);
      if (change === 'pending') assert.equal(location.href, 'https://blueprint.example.invalid/roadmap/values/index.html');
    """, observer=True)


@pytest.mark.parametrize("failure", ["absent", "vanishes", "throws", "rejects", "false", "stalls"])
def test_a_math_service_that_is_missing_fails_or_stalls_leaves_the_formulas_as_typed(book: Path, failure: str) -> None:
    _run(book, "roadmap/values/", "const failure = " + json.dumps(failure) + ";\n" + r"""
      if (failure !== 'absent') engine.answer = () => {
        if (failure === 'throws') throw new Error('MathJax is unavailable');
        if (failure === 'rejects') return window.Promise.reject(new Error('MathJax is unavailable'));
        return failure === 'false' ? window.Promise.resolve(false) : new window.Promise(() => {});
      };
      await openList(); const all = rows();
      // With no service the formulas read as typed at once; a failed pass gives up at once, a stalled one after two seconds.
      if (failure === 'absent') typed(all);
      if (failure === 'vanishes') delete window.AutoformMath;
      await tick(100); if (failure !== 'stalls') typed(all);
      await tick(2000); typed(all);
      const handed = failure === 'absent' ? [] : engine.passes;
      assert.deepEqual(handed, ['absent', 'vanishes'].includes(failure) ? [] : [ids(all.slice(0, 6))]);
      // Nothing is read later either.
      await tick(3000); assert.equal(handed.length, ['absent', 'vanishes'].includes(failure) ? 0 : 1);
    """, engine=failure != "absent")


def test_a_link_followed_during_a_pass_leaves_the_formulas_mathjax_did_not_draw_as_typed(book: Path) -> None:
    _run(book, "roadmap/values/", r"""
      let finish; engine.answer = () => new window.Promise(resolve => { finish = resolve; });
      await openList(); const all = rows(); await tick(100);
      assert.deepEqual(engine.passes, [ids(all.slice(0, 6))]);
      // The pass ends once the reader has followed a link whose page has not come yet: the list is no longer current.
      const trail = $('.bp-dag-breadcrumb a'); net.hold(new URL(trail.href).pathname.slice(1)); click(trail);
      finish(true); await tick(3000);
      assert.equal(engine.passes.length, 1); typed(all);
    """)


def test_leaving_the_list_keeps_what_mathjax_drew_and_the_code_and_shows_the_rest_as_typed(book: Path) -> None:
    _run(book, "roadmap/code/", r"""
      // MathJax draws the first row's formula, then stalls.
      engine.answer = maths => { draw(maths.slice(0, 1)); return new window.Promise(() => {}); };
      await openList(); const all = rows(); await tick(100);
      const drawn = all[0].querySelector('.arithmatex'), output = drawn.querySelector('mjx-container');
      click(control('Back to map')); await tick(0);
      assert.equal(drawn.className, 'arithmatex'); assert.equal(drawn.firstChild, output);
      assert.equal(all[0].querySelector('code').textContent, '$literal$'); typed(all.slice(1));
      // Anything else the map redraws leaves the rows as they are.
      const text = all.map(row => row.textContent);
      click($('.bp-dag-filter-summary')); $('.bp-dag-filter-popover input').click(); await tick(100);
      assert.deepEqual(all.map(row => row.textContent), text); assert.equal(engine.passes.length, 1);
    """)


@pytest.mark.parametrize("outcome", ["resolves", "rejects"])
def test_an_old_lists_pass_and_observer_cannot_touch_the_new_list(book: Path, outcome: str) -> None:
    _run(book, "roadmap/values/", "const outcome = " + json.dumps(outcome) + ";\n" + r"""
      const held = [];
      engine.answer = maths => new window.Promise((resolve, reject) => held.push({finish() { draw(maths); resolve(true); }, reject}));
      await openList(); const first = observers.at(-1); first.show(rows()); await tick(100);
      // A search rebuilds the rows, and their formulas wait for a pass of their own.
      const box = $('.bp-dag-search'); box.value = 'Value'; emit(box, 'input'); await tick(0);
      const fresh = rows(), second = observers.at(-1); second.show(fresh); await tick(100);
      assert.notEqual(second, first); assert.equal(engine.passes.length, 2);
      // The old pass ends, and the old observer reports the new rows.
      if (outcome === 'resolves') held[0].finish(); else held[0].reject(new Error('the old pass failed'));
      first.show(fresh); await tick(100);
      // The new pass still waits: nothing fell back or was read again, and the new observer still watches.
      assert.equal(engine.passes.length, 2); assert.equal($$('.bp-math-fallback').length, 0);
      assert(fresh.slice(0, 6).every(row => row.querySelector('.arithmatex')) && fresh.slice(6).every(waiting));
      assert.equal(second.connected, true);
      // When it ends, the next six rows follow.
      held[1].finish(); await tick(100); assert.deepEqual(engine.passes[2], ids(fresh.slice(6, 12)));
    """, observer=True)
