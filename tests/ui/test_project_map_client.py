"""The whole-project map client on its rendered page, run over real map data in jsdom (not browser evidence)."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from autoform_cli.ui import project_map, render
from autoform_cli.ui.graph_views import GraphView, ViewNode
from tests.dom_fixture import run_dom

_EXAMPLE = Path(__file__).resolve().parents[2] / "skills/setup/assets/cabannes-thesis-project"

# Mounts the map, then gives a walk `app`, `wait`, `$`, `$$`, `emit`, `assert`, `history`, `location` and `net`.
_HARNESS = r"""
const fs = require('fs'), path = require('path'), assert = require('assert');
// Timers and animation frames wait for wait(), which runs them a round at a time in the order they were set, whatever
// their delay; nothing cancels them. matchMedia asks for reduced motion, so the camera jumps instead of flying, and
// performance.now() stays at 0, so a file that failed is never asked for again on its own.
const frames = [];
window.setTimeout = window.requestAnimationFrame = run => frames.push(run);
window.performance.now = () => 0;
window.matchMedia = () => ({matches: true});
// Every element, the map's stage included, is 1200 by 800 pixels.
window.Element.prototype.getBoundingClientRect = () => new window.DOMRect(0, 0, 1200, 800);
async function wait(rounds = 12) {
  for (let round = 0; round < rounds; round++) {
    await new Promise(resolve => setImmediate(resolve));
    for (const run of frames.splice(0)) try { run(); } catch (error) { errors.push(String(error && error.stack || error)); }
  }
}
const app = document.querySelector('[data-project-map]'), location = window.location;
const $ = selector => app.querySelector(selector), $$ = (selector, root = app) => Array.from(root.querySelectorAll(selector));
// The map's files: args.hold waits for net.release(name), args.fail fails once, and net.log lists every request.
// Anything else, such as a result's page in the book, is missing.
const data = new URL(app.dataset.map, location.href).href;
const held = new Map(args.hold ? [[args.hold, []]] : []), failing = new Set(args.fail ? [args.fail] : []);
const net = {log: [], release(name) { (held.get(name) || []).forEach(go => go()); held.delete(name); }};
window.fetch = url => {
  const href = String(url), inside = href.startsWith(data), name = inside ? href.slice(data.length) : href;
  net.log.push(name);
  if (failing.delete(name)) return window.Promise.reject(new window.TypeError('the network dropped ' + name));
  // Answered in the page's realm, as a browser's fetch answers.
  const file = path.join(args.map, name), answer = () => inside && fs.existsSync(file)
    ? {ok: true, status: 200, json: () => window.Promise.resolve(window.JSON.parse(fs.readFileSync(file, 'utf8')))}
    : {ok: false, status: 404};
  return held.has(name) ? new window.Promise(resolve => held.get(name).push(() => resolve(answer()))) : window.Promise.resolve(answer());
};
// jsdom's Back and Forward take two timer hops to reach popstate. Here they take them at once, so they land before
// the timers already waiting, such as a camera write.
function traverse(go) {
  const waiting = frames.splice(0);
  let landed = false;
  window.addEventListener('popstate', () => { landed = true; }, {once: true});
  go();
  for (let hop = 0; hop < 10 && !landed; hop++) frames.splice(0).forEach(run => run());
  assert(landed, 'Back or Forward reached no popstate');
  frames.unshift(...waiting);
}
const history = {back: () => traverse(() => window.history.back()), forward: () => traverse(() => window.history.forward()),
  get length() { return window.history.length; }};
// jsdom has no PointerEvent: a pointer event is a mouse event carrying the pointer's id and type.
function emit(node, type, {pointerId, pointerType, ...init} = {}) {
  const Kind = type === 'wheel' ? window.WheelEvent : type.startsWith('key') ? window.KeyboardEvent
    : type === 'focus' || type === 'blur' ? window.FocusEvent : /^(click|mouse|pointer)/.test(type) ? window.MouseEvent : window.Event;
  const event = new Kind(type, {bubbles: Kind !== window.FocusEvent, cancelable: true, ...init});
  if (type.startsWith('pointer')) Object.assign(event, {pointerId, pointerType});
  node.dispatchEvent(event);
  return event;
}
eval(args.before);
load(args.bundle);
await wait(20);
"""


def _library(tmp_path: Path, *, captions: dict[str, str] | None = None, tracks_lean: bool = True) -> Path:
    records: list[dict[str, object]] = [
        {"id": "roadmap", "kind": "scope", "title": "Library", "parent": None, "result": False},
        {"id": "Algebra", "kind": "scope", "title": "Algebra", "parent": "roadmap", "result": False, "area": "Algebra"},
        {"id": "Algebra/groups", "kind": "scope", "title": "Groups", "parent": "Algebra", "result": False, "navigation": "module"},
        {"id": "Analysis", "kind": "scope", "title": "Analysis", "parent": "roadmap", "result": False, "area": "Analysis"},
        {"id": "Analysis/series", "kind": "scope", "title": "Series", "parent": "Analysis", "result": False, "navigation": "module"},
    ]
    results = [
        ("Algebra/groups/lagrange", "Algebra/groups", "Lagrange's theorem", "Subgroup.card_dvd", "fully_proved"),
        ("Algebra/groups/cauchy", "Algebra/groups", "Cauchy's theorem", "exists_prime_orderOf", "can_prove"),
        ("Analysis/series/abel", "Analysis/series", "Abel summation for $\\mathbb{R}$", "abel_summation", "planned"),
    ]
    for node_id, parent, title, lean, status in results:
        records.append({"id": node_id, "kind": "node", "title": title, "parent": parent, "result": True, "status": status,
                        "lean": lean, "summary": f"{title} holds.", "route": f"../roadmap/{parent}/index.html#{node_id.rsplit('/', 1)[1]}"})
        if captions and node_id in captions:
            records[-1]["caption"] = captions[node_id]
    relations = [("Algebra/groups/cauchy", "Algebra/groups/lagrange", 1, 0), ("Analysis/series/abel", "Algebra/groups/lagrange", 0, 1)]
    project_map.write_project_map(tmp_path / "map", project_map.build_project_map(records, relations, tracks_lean=tracks_lean))
    return tmp_path / "map"


def _walk(tmp_path: Path, script: str, map_dir: Path | None = None, *,
          hash: str = "", hold: str = "", fail: str = "", before: str = "") -> str:
    """Run ``script`` on the rendered map page, opened at ``hash``, over the map at ``map_dir``.

    ``hold`` names a data file held until the script calls ``net.release``, and ``fail`` one whose first request fails.
    ``before`` runs ahead of the map's script.
    """
    assets = dict(render._map_assets("Library"))
    bundle = tmp_path / "project-map.js"
    bundle.write_text(assets[render.MAP_SCRIPT], encoding="utf-8")
    args = {"bundle": str(bundle), "map": str(map_dir or _library(tmp_path)), "hold": hold, "fail": fail, "before": before}
    url = "https://blueprint.example.invalid/" + render.MAP_PAGE + hash
    return run_dom(_HARNESS + script, page=assets[render.MAP_PAGE], url=url, args=args)


def test_overview_search_arrival_pivot_back_and_whole_project(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      // Opening shows every region, each with its title and size.
      const labels = $$('.pm-part').map(node => node.getAttribute('aria-label'));
      assert(labels.includes('Algebra, 2 results, 1 used elsewhere') && labels.includes('Analysis, 1 results'), labels.join('; '));
      // A Lean name reaches its theorem directly, with its statement and Lean name in the reading pane.
      const search = $('.pm-search').querySelector('input');
      search.value = 'Subgroup.card_dvd'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      assert.strictEqual($$('.pm-hit').length, 1);
      emit(search, 'keydown', {key: 'Enter'}); await wait(40);
      const reader = $('.pm-reader');
      assert.strictEqual(reader.querySelector('h2').textContent, "Lagrange's theorem");
      assert.strictEqual(reader.querySelector('.pm-lean').querySelector('code').textContent, 'Subgroup.card_dvd');
      assert.strictEqual(location.hash, '#result=' + encodeURIComponent('Algebra/groups/lagrange'));
      // It lists what uses it, inside and outside its part; the selection lights those links.
      const usedBy = reader.querySelectorAll('.pm-list');
      const names = $$('button', usedBy[usedBy.length - 1]).map(b => b.querySelector('span').textContent).sort();
      assert.deepStrictEqual(names, ['Abel summation for ℝ', "Cauchy's theorem"]);
      // Its card carries its title, and the link from Cauchy's theorem, inside the part, is highlighted.
      assert($$('.pm-card').length >= 2, 'cards carry titles at this zoom');
      assert($$('.pm-involved').length >= 1, 'selection highlights its links');
      // Following one pivots the focus; Back restores the previous selection.
      const cauchy = $$('button', usedBy[usedBy.length - 1]).find(b => b.textContent.includes('Cauchy'));
      emit(cauchy, 'click'); await wait(40);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, "Cauchy's theorem");
      history.back(); await wait(40);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, "Lagrange's theorem");
      // The whole project is one action away, and the trail names where the theorem lives.
      assert.deepStrictEqual($$('li', $('.pm-trail')).map(li => li.textContent), ['Library', 'Algebra', 'Groups', "Lagrange's theorem"]);
      emit($$('button').find(b => b.textContent === 'Whole project'), 'click'); await wait(30);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Library');
      console.log('walk ok');
    """)
    assert "walk ok" in out


def test_zooming_into_a_region_opens_it_in_place(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      assert.strictEqual($$('.pm-result').length, 3, 'the opening shows every result');
      assert.deepStrictEqual($$('li', $('.pm-trail')).map(li => li.textContent), ['Library']);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Library');
      // Zoom towards Lagrange's theorem.
      const canvas = $('.pm-stage').querySelector('svg'), card = $$('.pm-result')[0].querySelector('rect');
      const x = +card.getAttribute('x') + card.getAttribute('width') / 2, y = +card.getAttribute('y') + card.getAttribute('height') / 2;
      for (let i = 0; i < 3; i++) { emit(canvas, 'wheel', {deltaX: 0, deltaY: -320, ctrlKey: true, clientX: x, clientY: y}); await wait(4); }
      await wait(20);
      // The same page now reads Groups from inside: its results are cards with titles, and the trail says where we are.
      assert.strictEqual(location.hash, '', 'zooming is not navigation');
      assert($$('.pm-card').length >= 1, 'the result carries its title');
      assert.deepStrictEqual($$('li', $('.pm-trail')).map(li => li.textContent), ['Library', 'Algebra', 'Groups']);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Groups');
      console.log('zoom ok');
    """)
    assert "zoom ok" in out


def test_scrolling_pans_and_control_scrolling_zooms_with_focus_elsewhere(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const search = $('.pm-search').querySelector('input'), canvas = $('.pm-stage').querySelector('svg');
      emit(search, 'focus');
      const box = () => ['x', 'y', 'width'].map(name => +$$('.pm-part')[0].querySelector('rect').getAttribute(name));
      const [x, y, w] = box();
      emit(canvas, 'wheel', {deltaX: 41, deltaY: 29, clientX: x + 5, clientY: y + 5}); await wait(20);
      const [x2, y2, w2] = box();
      assert(Math.abs(x2 - (x - 41)) < 1 && Math.abs(y2 - (y - 29)) < 1 && Math.abs(w2 - w) < 0.01, [x, y, w, x2, y2, w2].join());
      // A wheel that counts lines, not pixels, moves 25 pixels a line.
      emit(canvas, 'wheel', {deltaX: 0, deltaY: 2, deltaMode: 1, clientX: x2, clientY: y2}); await wait(20);
      assert(Math.abs(box()[1] - (y2 - 50)) < 1, box().join());
      emit(canvas, 'wheel', {deltaX: 0, deltaY: -100, ctrlKey: true, clientX: x2 + w2 / 2, clientY: y2}); await wait(20);
      assert(box()[2] > w2 * 1.1, 'a Control scroll zooms');
      assert.strictEqual(location.hash, '', 'moving the map is not navigation');
      console.log('wheel ok');
    """)
    assert "wheel ok" in out


def test_zooming_into_a_gap_lands_on_the_nearest_content(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const canvas = $('.pm-stage').querySelector('svg');
      const box = id => ['x', 'y', 'width', 'height'].map(name => +$$('.pm-part').find(n => n.getAttribute('data-part') === id).querySelector('rect').getAttribute(name));
      const a = box('Algebra'), b = box('Analysis');
      // Halfway across the gap between the two regions, whichever way they sit.
      const x = a[0] + a[2] < b[0] ? (a[0] + a[2] + b[0]) / 2 : a[0] + a[2] / 2;
      const y = a[0] + a[2] < b[0] ? a[1] + a[3] / 2 : (a[1] + a[3] + b[1]) / 2;
      for (let i = 0; i < 12; i++) { emit(canvas, 'wheel', {deltaX: 0, deltaY: -400, ctrlKey: true, clientX: x, clientY: y}); await wait(4); }
      await wait(30);
      assert($$('.pm-card').length >= 1, 'a drill ends on titled results, not between parts');
      assert(!$('.pm-hint').textContent.includes('between'), $('.pm-hint').textContent);
      console.log('gap ok');
    """)
    assert "gap ok" in out


def _wide(tmp_path: Path) -> Path:
    """Thirty one-result topics beside a chapter twenty times their size: at the opening, each topic would be a sliver."""
    wide = tmp_path / "wide"
    records: list[dict[str, object]] = [
        {"id": "roadmap", "kind": "scope", "title": "Library", "parent": None, "result": False},
        {"id": "Big", "kind": "scope", "title": "Topics", "parent": "roadmap", "result": False},
        {"id": "Huge", "kind": "scope", "title": "Main chapter", "parent": "roadmap", "result": False},
    ]
    for index in range(30):
        records.append({"id": f"Big/t{index:02d}", "kind": "scope", "title": f"Topic {index:02d}", "parent": "Big", "result": False})
        records.append({"id": f"Big/t{index:02d}/r", "kind": "node", "title": f"Result {index:02d}", "parent": f"Big/t{index:02d}",
                        "result": True, "status": "planned"})
    records += [{"id": f"Huge/r{index:03d}", "kind": "node", "title": f"Main {index:03d}", "parent": "Huge", "result": True,
                 "status": "fully_proved"} for index in range(600)]
    project_map.write_project_map(wide, project_map.build_project_map(records, []))
    return wide


def test_a_part_opens_at_the_scale_where_its_parts_can_be_named(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const big = () => $$('.pm-part').find(n => n.getAttribute('data-part') === 'Big');
      const titles = () => $$('.pm-title').map(t => t.textContent);
      // One named tile, not thirty nameless ones.
      assert(big().getAttribute('class').includes('pm-tile'), big().getAttribute('class'));
      assert(titles().includes('Topics'), titles().join('; '));
      assert(!$$('.pm-part').some(n => (n.getAttribute('data-part') || '').startsWith('Big/')), 'no topic is drawn yet');
      // The tile shows its topics faintly, where they will be, with no text but its own title.
      assert.strictEqual($$('.pm-mini').length, 30);
      assert(!$$('.pm-meta').some(t => t.textContent.includes('Topic')), 'no list of contents');
      // Closer in, it opens, and its topics carry their names.
      const r = big().querySelector('rect'), x = +r.getAttribute('x') + r.getAttribute('width') / 2, y = +r.getAttribute('y') + r.getAttribute('height') / 2;
      // Four steps, each twice as close.
      for (let i = 0; i < 4; i++) { emit($('.pm-stage').querySelector('svg'), 'wheel', {deltaX: 0, deltaY: -Math.log(2) / 0.008, ctrlKey: true, clientX: x, clientY: y}); await wait(10); }
      await wait(30);
      assert(big().getAttribute('class').includes('pm-open'), big().getAttribute('class'));
      assert(titles().filter(t => t.startsWith('Topic ')).length >= 4, titles().join('; '));
      console.log('open ok');
    """, _wide(tmp_path))
    assert "open ok" in out


def test_an_empty_roadmap_says_so(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    project_map.write_project_map(empty, project_map.build_project_map(
        [{"id": "roadmap", "kind": "scope", "title": "Fresh roadmap", "parent": None, "result": False}], []))
    out = _walk(tmp_path, r"""
      assert.strictEqual($('.pm-reader').querySelector('.pm-empty').textContent, 'Nothing is on the map yet: the roadmap has no results.');
      assert.strictEqual($$('.pm-part').length, 0);
      console.log('empty ok');
    """, empty)
    assert "empty ok" in out


def test_titles_with_tex_are_readable_on_the_map(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const search = $('.pm-search').querySelector('input');
      search.value = 'abel'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      const hit = $$('.pm-hit')[0];
      assert.strictEqual(hit.querySelector('.pm-hit-title').textContent, 'Abel summation for ℝ');
      console.log('tex ok');
    """)
    assert "tex ok" in out


def test_beside_the_explorer_the_map_reads_formulas_with_the_explorers_reducer(tmp_path: Path) -> None:
    # The explorer's bundle and the map's script both carry tex-plain.js. The book runs its bundle first, so the map's
    # copy leaves the reducer already on window in place.
    explorer = Path(render.__file__).with_name("assets") / "blueprint-explorer.min.js"
    before = (f"load({json.dumps(str(explorer))}); const explorerMath = window.AutoformTexPlain.math;"
              " window.AutoformTexPlain.math = tex => '<' + explorerMath(tex) + '>';")
    out = _walk(tmp_path, r"""
      const search = $('.pm-search').querySelector('input');
      search.value = 'abel'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      assert.strictEqual($$('.pm-hit')[0].querySelector('.pm-hit-title').textContent, 'Abel summation for <ℝ>');
      console.log('one reducer ok');
    """, before=before)
    assert "one reducer ok" in out


def test_list_entries_with_formulas_go_to_the_sites_mathjax_a_list_at_a_time(tmp_path: Path) -> None:
    library = tmp_path / "lists"
    project_map.write_project_map(library, project_map.build_project_map([
        {"id": "roadmap", "kind": "scope", "title": "Library", "parent": None, "result": False},
        {"id": "Algebra", "kind": "scope", "title": "Algebra", "parent": "roadmap", "result": False, "area": "Algebra"},
        {"id": "Analysis", "kind": "scope", "title": "Analysis", "parent": "roadmap", "result": False, "area": "Analysis"},
        {"id": "Algebra/lagrange", "kind": "node", "title": "Lagrange's theorem", "parent": "Algebra", "result": True,
         "status": "planned", "lean": "Subgroup.card_dvd"},
        {"id": "Analysis/abel", "kind": "node", "title": r"Abel summation for $\mathbb{R}$", "parent": "Analysis",
         "result": True, "status": "planned"},
    ], [("Algebra/lagrange", "Analysis/abel", 1, 0)]))
    out = _walk(tmp_path, r"""
      const asked = [], R = ['\\(\\mathbb{R}\\)'];
      window.AutoformMath = {refresh: root => { asked.push(root); return Promise.resolve(true); }};
      const formulas = node => $$('.arithmatex', node).map(span => span.textContent);
      // Picked, Analysis lists the result it exports most, and MathJax gets that list.
      const rect = $$('.pm-part').find(node => node.getAttribute('data-part') === 'Analysis').querySelector('rect');
      emit(rect, 'pointerdown', {button: 0, clientX: 30, clientY: 30, pointerId: 1});
      emit(rect, 'pointerup', {button: 0, clientX: 30, clientY: 30, pointerId: 1}); await wait(40);
      const most = $('.pm-reader').querySelector('.pm-more .pm-list');
      assert.deepStrictEqual(formulas(most), R);
      assert(asked.includes(most), 'the Most-used list goes to MathJax');
      // So do the search hits, as one list.
      const search = $('.pm-search').querySelector('input');
      search.value = 'abel'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      assert.deepStrictEqual(formulas($('.pm-hits')), R);
      assert(asked.includes($('.pm-hits')) && !asked.some(root => root.closest && root.closest('.pm-hit')), 'one pass for the hits');
      // And so does the list of what Lagrange's theorem uses.
      search.value = 'Subgroup.card_dvd'; emit(search, 'input'); await wait(5);
      emit(search, 'keydown', {key: 'Enter'}); await wait(40);
      const uses = $('.pm-reader').querySelector('.pm-list');
      assert.deepStrictEqual(formulas(uses), R);
      assert(asked.some(root => root.contains(uses)), 'the Uses list goes to MathJax');
      console.log('lists ok');
    """, library)
    assert "lists ok" in out


def test_escaped_braces_and_norm_bars_stay_in_titles(tmp_path: Path) -> None:
    braces = tmp_path / "braces"
    project_map.write_project_map(braces, project_map.build_project_map([
        {"id": "roadmap", "kind": "scope", "title": "Notes", "parent": None, "result": False},
        {"id": "set", "kind": "node", "title": r"Braces of $\{$not, and, or$\}$", "parent": "roadmap", "result": True,
         "status": "planned"},
        {"id": "union", "kind": "node", "title": r"Braces of Γ ∪ $\{\gamma\}$", "parent": "roadmap", "result": True,
         "status": "planned"},
        {"id": "norm", "kind": "node", "title": r"Braces of $\|x\| = \lVert{x}\rVert$", "parent": "roadmap", "result": True,
         "status": "planned"},
    ], []))
    out = _walk(tmp_path, r"""
      const search = $('.pm-search').querySelector('input');
      search.value = 'braces'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      assert.deepStrictEqual($$('.pm-hit-title').map(title => title.textContent).sort(),
        ['Braces of {not, and, or}', 'Braces of Γ ∪ {γ}', 'Braces of ‖x‖ = ‖x‖']);
      console.log('braces ok');
    """, braces)
    assert "braces ok" in out


def test_escaped_titles_read_as_their_pages_show_them_on_cards_and_in_search(tmp_path: Path) -> None:
    """Issue #727: cards and search hits showed `foo\\_bar` where the page's heading reads `foo_bar`. The records come
    from records_from_view, as render builds them, so each changed title reaches the map with its backslashes, backticks
    and dollars escaped, and the map's own reading gives the heading's text back: no code, formula or emphasis appears
    that the heading doesn't show. A title with a formula reaches it as written."""
    titles = {"a": r"Escape foo\_bar", "b": r"Escape \*bold\*", "c": r"Escape \#1", "d": r"Escape a\\b", "e": r"Escape \$a\$",
              "f": r"Escape cost \$5 for $x$", "g": r"Escape \`x\` beside \$y\$ and \*z\*", "h": r"Escape a\\b \* c \$"}
    nodes = (ViewNode("roadmap", "Notes", "scope", (), (("planned", 1),)),
             *(ViewNode(key, title, "node", (), (("planned", 1),), status_key="planned") for key, title in titles.items()))
    metadata = {key: {"parent": "roadmap", "result": True} for key in titles}
    escapes = tmp_path / "escapes"
    project_map.write_project_map(escapes, project_map.build_project_map(*project_map.records_from_view(GraphView("full", "Notes", nodes, ()), metadata)))
    shown = sorted(["Escape foo_bar", "Escape *bold*", "Escape #1", "Escape a\\b", "Escape $a$", "Escape cost $5 for x",
                    "Escape `x` beside $y$ and *z*", "Escape a\\b * c $"])
    out = _walk(tmp_path, "const shown = " + json.dumps(shown) + ";" + r"""
      assert.deepStrictEqual($$('.pm-result').map(card => card.querySelector('title').textContent).sort(), shown);
      const search = $('.pm-search').querySelector('input');
      search.value = 'escape'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      assert.deepStrictEqual($$('.pm-hit-title').map(title => title.textContent).sort(), shown);
      console.log('escapes ok');
    """, escapes)
    assert "escapes ok" in out


def test_a_part_as_wide_as_the_stage_is_marked_stage_wide(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const canvas = $('.pm-stage').querySelector('svg'), stageWidth = $('.pm-stage').getBoundingClientRect().width;
      const spans = node => { const r = node.querySelector('rect'); return +r.getAttribute('x') <= 0 && +r.getAttribute('x') + +r.getAttribute('width') >= stageWidth; };
      const wide = node => node.getAttribute('class').includes('pm-stage-wide');
      assert.strictEqual($$('.pm-part').filter(wide).length, 0, 'the whole project draws no part across the stage');
      for (let i = 0; i < 4; i++) { emit(canvas, 'keydown', {key: '+'}); await wait(10); }
      const parts = $$('.pm-part');
      assert(parts.some(spans), 'zoomed in, a part spans the stage');
      assert.deepStrictEqual(parts.map(wide), parts.map(spans));
      console.log('stage-wide ok');
    """)
    assert "stage-wide ok" in out


@pytest.mark.parametrize("tracks_lean", [True, False])
def test_a_project_that_tracks_no_lean_shows_no_progress(tmp_path: Path, tracks_lean: bool) -> None:
    # Two results with a kind and one without; the library holds a finished, a ready and a planned result.
    captions = {"Algebra/groups/lagrange": "Theorem", "Algebra/groups/cauchy": "Lemma"}
    out = _walk(tmp_path, f"const tracked = {str(tracks_lean).lower()};" + r"""
      // Cards are tinted by their status and named with it; or all alike, and named with their kind if they have one.
      const labels = $$('.pm-result').map(node => node.getAttribute('aria-label')).sort();
      assert.deepStrictEqual(labels, tracked
        ? ['Abel summation for ℝ, planned', "Cauchy's theorem, ready to prove", "Lagrange's theorem, fully proved"]
        : ['Abel summation for ℝ', "Cauchy's theorem, Lemma", "Lagrange's theorem, Theorem"]);
      const tinted = $$('.pm-done, .pm-progress, .pm-tile-done, .pm-tile-progress, .pm-mini-done, .pm-mini-progress, .pm-backdrop-done, .pm-backdrop-progress');
      assert.strictEqual(tinted.length > 0, tracked, tinted.map(node => node.getAttribute('class')).join('; '));
      // The outline's finished count, bar and legend, and the reading pane's bar.
      assert.deepStrictEqual($$('.pm-stat').map(stat => stat.querySelector('span').textContent),
        tracked ? ['results', 'sections', 'finished'] : ['results', 'sections']);
      assert.strictEqual($$('.pm-legend').length, tracked ? 1 : 0);
      assert.strictEqual($('.pm-reader').querySelectorAll('.pm-bar').length, tracked ? 1 : 0);
      assert.strictEqual($$('.pm-bar').length, tracked ? 2 : 0);
      // The search box offers Lean names only where the project tracks them, and so does a search that finds nothing.
      const search = $('.pm-search').querySelector('input');
      const offer = tracked ? 'Search theorems by name or Lean name' : 'Search theorems by name';
      assert.deepStrictEqual([search.placeholder, search.getAttribute('aria-label')], [offer, offer]);
      search.value = 'zzz'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      assert.strictEqual($('.pm-hits').textContent, tracked ? 'No theorem or Lean name matches.' : 'No theorem matches.');
      // A selected result's chip says its status; with nothing to say, there is no chip.
      search.value = 'Subgroup.card_dvd'; emit(search, 'input'); await wait(5);
      emit(search, 'keydown', {key: 'Enter'}); await wait(40);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, "Lagrange's theorem");
      assert.deepStrictEqual($$('.pm-chip', $('.pm-reader')).map(chip => chip.textContent), tracked ? ['fully proved'] : []);
      // The header bands draw bars, and a card's line names an unfinished status; or no bars, and the line names only
      // the result's kind.
      assert.strictEqual($$('.pm-seg').length > 0, tracked);
      const lines = $$('.pm-card-meta').map(node => node.textContent);
      assert(lines.length, 'a card line is drawn');
      assert(tracked ? lines.some(line => /ready to prove|planned/.test(line)) : lines.every(line => /^(Theorem|Lemma)( · |$)/.test(line)),
        lines.join('; '));
      // A finished card's fill says so, and its line names no status.
      const finished = $$('.pm-card-meta').find(node => node.dataset.key === 'cm:Algebra/groups/lagrange');
      assert(finished, lines.join('; '));
      assert(finished.textContent.startsWith(tracked ? 'used ' : 'Theorem · used '), finished.textContent);
      if (!tracked) {
        // No status words in the map's labels.
        const said = $$('text').map(node => node.textContent).filter(text => /fully proved|ready to prove|planned|finished/.test(text));
        assert.deepStrictEqual(said, []);
      }
      // Lagrange's local graph names its cards as the map does.
      const rect = $('.pm-result[data-result="Algebra/groups/lagrange"]').querySelector('rect');
      emit(rect, 'pointerdown', {button: 0, pointerId: 1}); emit(rect, 'pointerup', {button: 0, pointerId: 1}); await wait(40);
      assert(!$('.pm-local').hidden);
      assert.deepStrictEqual($$('.pm-result').map(node => node.getAttribute('aria-label')).sort(), labels);
      console.log('progress ok');
    """, _library(tmp_path, captions=captions, tracks_lean=tracks_lean))
    assert "progress ok" in out


def test_search_finds_results_in_a_flat_roadmap(tmp_path: Path) -> None:
    # Results straight under the root live in a generated part whose id they don't extend.
    flat = tmp_path / "flat"
    project_map.write_project_map(flat, project_map.build_project_map([
        {"id": "roadmap", "kind": "scope", "title": "Notes", "parent": None, "result": False},
        {"id": "flat", "kind": "node", "title": "Flat result", "parent": "roadmap", "result": True, "status": "planned"},
        {"id": "other", "kind": "node", "title": "Other result", "parent": "roadmap", "result": True, "status": "planned"},
    ], [("other", "flat", 1, 0)]))
    out = _walk(tmp_path, r"""
      const search = $('.pm-search').querySelector('input');
      search.value = 'flat'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      assert.strictEqual($$('.pm-hit').length, 1);
      emit(search, 'keydown', {key: 'Enter'}); await wait(40);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Flat result');
      assert.strictEqual(location.hash, '#result=flat');
      console.log('flat ok');
    """, flat)
    assert "flat ok" in out


def test_lists_page_through_every_entry_and_back_restores_the_search_and_scroll(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    records: list[dict[str, object]] = [
        {"id": "roadmap", "kind": "scope", "title": "Library", "parent": None, "result": False},
        {"id": "c", "kind": "scope", "title": "Chapter", "parent": "roadmap", "result": False},
        {"id": "c/hub", "kind": "node", "title": "Hub lemma", "parent": "c", "result": True, "status": "fully_proved"},
    ]
    records += [{"id": f"c/u{index:02d}", "kind": "node", "title": f"User {index:02d}", "parent": "c", "result": True,
                 "status": "planned"} for index in range(20)]
    project_map.write_project_map(hub, project_map.build_project_map(records, [(f"c/u{index:02d}", "c/hub", 1, 0) for index in range(20)]))
    out = _walk(tmp_path, r"""
      const search = $('.pm-search').querySelector('input'), reader = $('.pm-reader');
      search.value = 'hub'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      emit(search, 'keydown', {key: 'Enter'}); await wait(40);
      assert.strictEqual(reader.querySelector('h2').textContent, 'Hub lemma');
      assert.strictEqual(search.value, 'hub', 'the box keeps the query as typed');
      // Twenty results use it: the first twelve show, and the rest are one button away.
      const usedBy = () => $$('.pm-paged', reader).at(-1);
      assert.strictEqual(usedBy().querySelectorAll('li').length, 12);
      emit(usedBy().querySelector('.pm-show-more'), 'click');
      assert.strictEqual(usedBy().querySelectorAll('li').length, 20);
      assert(usedBy().querySelector('.pm-show-more').hidden, 'nothing is left to show');
      // Following one and coming Back restores the search box, the reading pane's scroll and the announcement.
      reader.scrollTop = 140;
      emit($$('button', usedBy()).find(b => b.textContent.includes('User 15')), 'click'); await wait(40);
      assert.strictEqual(reader.querySelector('h2').textContent, 'User 15');
      assert.strictEqual(reader.scrollTop, 0, 'a new result opens at its top');
      search.value = 'something else';
      history.back(); await wait(40);
      assert.strictEqual(reader.querySelector('h2').textContent, 'Hub lemma');
      assert.strictEqual(search.value, 'hub');
      assert.strictEqual(reader.scrollTop, 140);
      assert.strictEqual($('.pm-sr').textContent, 'Showing Hub lemma');
      emit($$('button').find(b => b.textContent === 'Whole project'), 'click'); await wait(30);
      assert.strictEqual($('.pm-sr').textContent, 'Whole project');
      console.log('back ok');
    """, hub)
    assert "back ok" in out


def test_one_click_picks_a_part_in_place_and_its_neighbours_are_a_row_away(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      await wait(20);
      const tile = id => $$('.pm-part').find(node => node.getAttribute('data-part') === id);
      const tap = async id => {
        const rect = tile(id).querySelector('rect');
        emit(rect, 'pointerdown', {button: 0, clientX: 30, clientY: 30, pointerId: 1});
        emit(rect, 'pointerup', {button: 0, clientX: 30, clientY: 30, pointerId: 1});
        await wait(40);
      };
      const names = role => $$('button', $('.pm-reader').querySelector('.pm-' + role))
        .map(button => button.querySelector('.pm-link-name').querySelector('span').textContent);
      // One click picks Analysis where it is: the pane names what it builds on, outlined, with an arrow from it.
      await tap('Analysis');
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Analysis');
      assert.strictEqual(location.hash, '', 'picking is not navigation');
      assert.deepStrictEqual(names('uses'), ['Algebra']);
      assert(tile('Algebra').getAttribute('class').includes('pm-uses') && tile('Analysis').getAttribute('class').includes('pm-picked'));
      assert.strictEqual($$('.pm-link').filter(path => !path.getAttribute('class').includes('pm-flowing')).length, 1);
      // Dots flow along it from Algebra, which is used, into Analysis.
      const flows = $$('.pm-flowing'), start = flows[0].getAttribute('d').match(/^M([-\d.]+),([-\d.]+)/).slice(1).map(Number);
      const gap = id => { const r = tile(id).querySelector('rect'), x = +r.getAttribute('x'), y = +r.getAttribute('y');
        return Math.hypot(Math.max(x - start[0], 0, start[0] - x - r.getAttribute('width')), Math.max(y - start[1], 0, start[1] - y - r.getAttribute('height'))); };
      assert.strictEqual(flows.length, 1);
      assert(gap('Algebra') < gap('Analysis'), 'the flow starts at Algebra');
      // Its row picks Algebra where it sits, used by Analysis.
      emit($('.pm-reader').querySelector('.pm-uses').querySelector('button'), 'click'); await wait(60);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Algebra');
      assert.deepStrictEqual(names('used-by'), ['Analysis']);
      // A second click on the picked part goes inside.
      await tap('Algebra');
      assert.strictEqual(location.hash, '#part=Algebra');
      assert.strictEqual($$('.pm-link').length, 0);
      console.log('pick ok');
    """)
    assert "pick ok" in out


def _groups(tmp_path: Path, *, exercises: int = 10, prefix: str = "e") -> Path:
    """A chapter that builds up to Lagrange's theorem through cosets and the index, used by Cauchy, Sylow and Abel
    summation; ten exercises use groups, so groups is a hub. ``exercises`` and ``prefix`` make another build."""
    groups = tmp_path / "groups"
    records: list[dict[str, object]] = [
        {"id": "roadmap", "kind": "scope", "title": "Library", "parent": None, "result": False},
        {"id": "Algebra", "kind": "scope", "title": "Algebra", "parent": "roadmap", "result": False},
        {"id": "Algebra/groups", "kind": "scope", "title": "Groups", "parent": "Algebra", "result": False},
        {"id": "Algebra/exercises", "kind": "scope", "title": "Exercises", "parent": "Algebra", "result": False},
        {"id": "Analysis", "kind": "scope", "title": "Analysis", "parent": "roadmap", "result": False},
    ]
    chapter = [("group", "Group", "fully_proved"), ("subgroup", "Subgroup", "fully_proved"), ("coset", "Coset", "proved"),
               ("index", "Index", "stated"), ("lagrange", "Lagrange's theorem", "can_prove"), ("cauchy", "Cauchy's theorem", "planned"),
               ("sylow", "Sylow's theorems", "planned")]
    records += [{"id": f"Algebra/groups/{name}", "kind": "node", "title": title, "parent": "Algebra/groups", "result": True,
                 "status": status, "route": f"../roadmap/Algebra/groups/index.html#{name}"} for name, title, status in chapter]
    records += [{"id": f"Algebra/exercises/{prefix}{k}", "kind": "node", "title": f"Exercise {k}",
                 "parent": "Algebra/exercises", "result": True, "status": "planned"} for k in range(exercises)]
    records.append({"id": "Analysis/abel", "kind": "node", "title": "Abel summation", "parent": "Analysis", "result": True, "status": "planned"})
    g = "Algebra/groups/"
    relations = [(g + "subgroup", g + "group", 1, 0), (g + "coset", g + "subgroup", 1, 0), (g + "index", g + "coset", 1, 0),
                 (g + "lagrange", g + "index", 1, 0), (g + "lagrange", g + "subgroup", 1, 0), (g + "cauchy", g + "lagrange", 0, 1),
                 (g + "sylow", g + "cauchy", 1, 0), (g + "sylow", g + "lagrange", 1, 0), ("Analysis/abel", g + "lagrange", 1, 0)]
    relations += [(f"Algebra/exercises/{prefix}{k}", g + "group", 1, 0) for k in range(exercises)]
    project_map.write_project_map(groups, project_map.build_project_map(records, relations))
    return groups


_TO_LAGRANGE = r"""
  const search = $('.pm-search').querySelector('input');
  search.value = "Lagrange"; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
  emit(search, 'keydown', {key: 'Enter'}); await wait(40); search.blur();
  const card = id => $(`.pm-result[data-result="Algebra/groups/${id}"]`);
  const click = async id => {
    const rect = card(id).querySelector('rect');
    emit(rect, 'pointerdown', {button: 0, clientX: 30, clientY: 30, pointerId: 1});
    emit(rect, 'pointerup', {button: 0, clientX: 30, clientY: 30, pointerId: 1}); await wait(40);
  };
  const panel = $('.pm-local'), level = () => panel.querySelector('[aria-pressed="true"]').textContent;
  const drawn = () => $$('.pm-result').map(node => node.getAttribute('data-result').split('/').pop()).sort();
  const where = id => ['x', 'y', 'width'].map(name => card(id).querySelector('rect').getAttribute(name)).join();
"""


def test_a_click_on_a_result_opens_its_local_graph_and_escape_and_back_restore_the_map(tmp_path: Path) -> None:
    out = _walk(tmp_path, _TO_LAGRANGE + r"""
      assert(panel.hidden && $$('.pm-part').length > 0);
      const before = where('lagrange'), entries = history.length;
      await click('lagrange');
      // Level 1: the index and the coset it rests on, and the subgroup it uses directly; the map's parts make way. What
      // it feeds into is drawn too, and the reading pane shows the result as a click shows it.
      assert(!panel.hidden && $$('.pm-part').length === 0);
      assert.strictEqual(panel.querySelector('.pm-local-title strong').textContent, "Lagrange's theorem");
      assert.strictEqual(level(), '1');
      assert.deepStrictEqual(drawn(), ['abel', 'cauchy', 'coset', 'index', 'lagrange', 'subgroup', 'sylow']);
      // Groups, four steps back, is not shown at level 1.
      assert.strictEqual(panel.querySelector('.pm-local-count').textContent, 'Showing 3 of 4 results it builds on');
      assert(card('lagrange').getAttribute('class').includes('pm-selected'));
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, "Lagrange's theorem");
      assert.strictEqual(history.length, entries + 1);
      // Cauchy's theorem uses it only in its proof.
      const routes = $$('.pm-card-route').map(path => path.querySelector('title').textContent);
      assert(routes.includes("Cauchy's theorem uses Lagrange's theorem in its proof") && routes.includes("Lagrange's theorem uses Subgroup"), routes.join('; '));
      // Level 3 shows the whole ancestry; groups, used by eleven results, is a chip that opens its own graph.
      emit($$('button', panel).find(b => b.textContent === '3'), 'click'); await wait(10);
      assert.strictEqual(level(), '3');
      assert(!drawn().includes('group'), 'a hub is never a card');
      const chip = panel.querySelector('.pm-hub');
      assert.strictEqual(chip.textContent, 'Group');
      assert.strictEqual(panel.querySelector('.pm-local-count').textContent, 'Showing 4 of 4 results it builds on');
      assert.strictEqual(history.length, entries + 1, 'a level is not a new place');
      // A click on a card inside moves the focus there, at the same level, and Back returns. Index's level 3 draws
      // nothing more than its level 2, which lists Group already, so level 2 is pressed.
      await click('index');
      assert.strictEqual(panel.querySelector('.pm-local-title strong').textContent, 'Index');
      assert.strictEqual(level(), '2');
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Index');
      history.back(); await wait(40);
      assert.strictEqual(panel.querySelector('.pm-local-title strong').textContent, "Lagrange's theorem");
      // Escape goes back to the map as it was: the same camera, and Lagrange's theorem still selected from the search.
      emit(app, 'keydown', {key: 'Escape'}); await wait(30);
      assert(panel.hidden && $$('.pm-part').length > 0);
      assert.strictEqual(where('lagrange'), before);
      assert(card('lagrange').getAttribute('class').includes('pm-selected'));
      // Back reopens the graph, and once more returns to the search's arrival.
      history.back(); await wait(40);
      assert(!panel.hidden && level() === '3');
      history.back(); await wait(40);
      assert(panel.hidden && where('lagrange') === before);
      // The chip opens the hub's own graph.
      await click('lagrange');
      emit($$('button', panel).find(b => b.textContent === '3'), 'click'); await wait(10);
      emit(panel.querySelector('.pm-hub'), 'click'); await wait(40);
      assert.strictEqual(panel.querySelector('.pm-local-title strong').textContent, 'Group');
      assert.strictEqual(panel.querySelector('.pm-local-count').textContent, 'It builds on no other result.');
      // Eleven results use it: the eight most used are drawn, and the rest are counted under them.
      assert($$('text').some(text => text.textContent === '+3 more'));
      console.log('local ok');
    """, _groups(tmp_path))
    assert "local ok" in out


def test_every_way_out_of_a_local_graph_restores_the_map_and_back_draws_the_graph_where_it_was(tmp_path: Path) -> None:
    out = _walk(tmp_path, _TO_LAGRANGE + r"""
      const at = id => card(id) ? where(id) : 'not drawn', before = where('lagrange');
      await click('lagrange');
      const inGraph = at('lagrange');
      const ways = {
        Escape: () => emit(app, 'keydown', {key: 'Escape'}),
        'Back to the map': () => emit(panel.querySelector('.pm-local-close'), 'click'),
        Up: () => emit($$('button', $('.pm-tools')).find(b => b.textContent === '↑ Up'), 'click'),
      };
      for (const [way, leave] of Object.entries(ways)) {
        leave(); await wait(30);
        // The map as it was: the search's camera, with Lagrange's theorem still selected.
        assert(panel.hidden, way);
        assert.strictEqual(where('lagrange'), before, way);
        assert(card('lagrange').getAttribute('class').includes('pm-selected'), way);
        history.back(); await wait(40);
        assert(!panel.hidden, way + ', then Back');
        assert.strictEqual(at('lagrange'), inGraph, way + ', then Back');
      }
      console.log('ways out ok');
    """, _groups(tmp_path))
    assert "ways out ok" in out


def test_on_the_bundled_example_a_result_opens_its_local_graph_a_click_inside_moves_it_and_back_returns(tmp_path: Path) -> None:
    # Hubs, via edges and the cap need a larger library than the example (the synthetic one in test_project_map_local).
    blueprint = tmp_path / "project/blueprint"
    shutil.copytree(_EXAMPLE / "blueprint", blueprint)
    render.render_site(blueprint, tmp_path / "site", lean_root=blueprint.parent)
    out = _walk(tmp_path, r"""
      await wait(20);
      const card = id => $(`.pm-result[data-result="${id}"]`), panel = $('.pm-local');
      const title = () => panel.querySelector('.pm-local-title strong').textContent;
      const where = id => ['x', 'y', 'width'].map(name => card(id).querySelector('rect').getAttribute(name)).join();
      const click = async id => {
        const rect = card(id).querySelector('rect');
        emit(rect, 'pointerdown', {button: 0, pointerId: 1}); emit(rect, 'pointerup', {button: 0, pointerId: 1}); await wait(40);
      };
      const recovery = 'infimum-loss/theorems/supervision-recovery', before = where(recovery);
      await click(recovery);
      // All six results it builds on, across both chapters: two used in its statement, two only in its proof, and
      // full supervision, which it uses directly though a result it uses in its proof already uses it.
      assert.strictEqual(title(), 'Supervision recovery');
      assert.strictEqual(panel.querySelector('.pm-local-count').textContent, 'Showing 6 of 6 results it builds on');
      assert.deepStrictEqual($$('.pm-result').map(node => node.getAttribute('data-result')).sort(), [
        'full-supervision/definitions/full-supervision', 'infimum-loss/definitions/eligibility', 'infimum-loss/definitions/non-ambiguity',
        'infimum-loss/theorems/infimum-loss', 'infimum-loss/theorems/non-ambiguity-determinism',
        'infimum-loss/theorems/supervision-non-ambiguous', recovery]);
      const routes = $$('.pm-card-route').map(path => path.querySelector('title').textContent);
      assert(['Supervision recovery uses Full supervision', 'Supervision recovery uses Infimum loss',
        'Supervision recovery uses Non-ambiguity determinism in its proof'].every(route => routes.includes(route)), routes.join('; '));
      // A click inside moves the graph; Back returns to the result it opened on, and then to the map as it was.
      await click('infimum-loss/theorems/infimum-loss');
      assert.strictEqual(title(), 'Infimum loss');
      history.back(); await wait(40);
      assert.strictEqual(title(), 'Supervision recovery');
      history.back(); await wait(40);
      assert(panel.hidden && card(recovery).getAttribute('class').includes('pm-selected'));
      assert.strictEqual(where(recovery), before);
      console.log('example ok');
    """, tmp_path / "site" / project_map.MAP_DIRECTORY, hash="#result=infimum-loss/theorems/supervision-recovery")
    assert "example ok" in out


def test_keyboard_focus_stays_on_the_graph_after_a_chip_and_on_the_map_after_closing(tmp_path: Path) -> None:
    out = _walk(tmp_path, _TO_LAGRANGE + r"""
      await click('lagrange');
      emit(panel.querySelector('[data-level="3"]'), 'click'); await wait(10);
      // A chip pressed from the keyboard goes with the panel it was in; focus goes to the hub's card.
      const chip = panel.querySelector('.pm-hub');
      chip.focus(); emit(chip, 'click'); await wait(40);
      assert.strictEqual(panel.querySelector('.pm-local-title strong').textContent, 'Group');
      assert.strictEqual(document.activeElement, card('group'));
      // "Back to the map" hides the panel it is in; focus goes to the selection's card on the map.
      const close = panel.querySelector('.pm-local-close');
      close.focus(); emit(close, 'click'); await wait(30);
      assert(panel.hidden);
      assert.strictEqual(document.activeElement, card('lagrange'));
      console.log('focus ok');
    """, _groups(tmp_path))
    assert "focus ok" in out


def test_the_second_press_of_a_double_click_opens_nothing_in_the_graph_the_first_drew(tmp_path: Path) -> None:
    out = _walk(tmp_path, _TO_LAGRANGE + r"""
      await click('lagrange');
      const title = () => panel.querySelector('.pm-local-title strong').textContent;
      // The browser counts the presses on the mousedown between pointerdown and pointerup.
      const press = async (id, detail) => {
        const rect = card(id).querySelector('rect');
        emit(rect, 'pointerdown', {button: 0, pointerId: 1}); emit(rect, 'mousedown', {button: 0, detail});
        emit(rect, 'pointerup', {button: 0, pointerId: 1}); await wait(40);
      };
      await press('index', 2);
      assert.strictEqual(title(), "Lagrange's theorem");
      emit(panel.querySelector('[data-level="3"]'), 'click', {detail: 2}); await wait(10);
      assert.strictEqual(level(), '1');
      // A press of its own still moves the graph.
      await press('index', 1);
      assert.strictEqual(title(), 'Index');
      console.log('double click ok');
    """, _groups(tmp_path))
    assert "double click ok" in out


def test_escape_after_the_stage_turns_finds_the_selection_in_the_new_packing(tmp_path: Path) -> None:
    out = _walk(tmp_path, _TO_LAGRANGE + r"""
      await click('lagrange');
      // The stage turns portrait: the world is packed again for it, and the graph stays open.
      window.Element.prototype.getBoundingClientRect = () => new window.DOMRect(0, 0, 600, 900);
      window.resizeNow(); await wait(20);
      assert(!panel.hidden);
      // Escape lands on Lagrange's theorem where the new packing put it, as a search lands it: across the middle, a
      // third of the way down.
      emit(app, 'keydown', {key: 'Escape'}); await wait(30);
      assert(panel.hidden && card('lagrange').getAttribute('class').includes('pm-selected'));
      const box = card('lagrange').querySelector('rect'), get = name => +box.getAttribute(name);
      const at = [get('x') + get('width') / 2, get('y') + get('height') / 2];
      assert(Math.abs(at[0] - 300) < 2 && Math.abs(at[1] - 270) < 2, at.join());
      console.log('turn ok');
    """, _groups(tmp_path), before="window.ResizeObserver = class { constructor(run) { window.resizeNow = run; } observe() {} };")
    assert "turn ok" in out


def test_every_hub_a_result_builds_on_can_be_opened_past_the_twelfth(tmp_path: Path) -> None:
    hubs, part = tmp_path / "hubs", "Algebra/hubs"
    records: list[dict[str, object]] = [
        {"id": "roadmap", "kind": "scope", "title": "Library", "parent": None, "result": False},
        {"id": "Algebra", "kind": "scope", "title": "Algebra", "parent": "roadmap", "result": False},
        {"id": part, "kind": "scope", "title": "Hubs", "parent": "Algebra", "result": False},
    ]
    # The focus and ten others each use thirteen hubs.
    users = [f"{part}/focus"] + [f"{part}/u{k}" for k in range(10)]
    records += [{"id": node, "kind": "node", "title": node.rsplit("/", 1)[1], "parent": part, "result": True, "status": "planned"}
                for node in users]
    records += [{"id": f"{part}/h{k:02d}", "kind": "node", "title": f"Hub {k:02d}", "parent": part, "result": True,
                 "status": "planned"} for k in range(13)]
    relations = [(user, f"{part}/h{k:02d}", 1, 0) for user in users for k in range(13)]
    project_map.write_project_map(hubs, project_map.build_project_map(records, relations))
    out = _walk(tmp_path, r"""
      await wait(20);
      const rect = $('.pm-result[data-result="Algebra/hubs/focus"]').querySelector('rect');
      emit(rect, 'pointerdown', {button: 0, pointerId: 1}); emit(rect, 'pointerup', {button: 0, pointerId: 1}); await wait(40);
      const panel = $('.pm-local'), chips = () => $$('.pm-hub', panel);
      assert.strictEqual(panel.querySelector('.pm-local-count').textContent, 'Showing 13 of 13 results it builds on');
      assert.strictEqual(chips().length, 12);
      // The thirteenth is a press away, and opens its own graph.
      const more = panel.querySelector('.pm-chips-more');
      assert.strictEqual(more.textContent, '+1 more');
      emit(more, 'click'); await wait(10);
      assert.strictEqual(chips().length, 13);
      assert.strictEqual(document.activeElement, chips()[12]);
      emit(chips()[12], 'click'); await wait(40);
      assert.strictEqual(panel.querySelector('.pm-local-title strong').textContent, 'Hub 12');
      console.log('chips ok');
    """, hubs, hash="#result=Algebra/hubs/focus")
    assert "chips ok" in out


def test_a_result_that_builds_only_on_hubs_counts_them(tmp_path: Path) -> None:
    out = _walk(tmp_path, _TO_LAGRANGE + r"""
      // Subgroup and every exercise use only Group, a hub: the panel lists it and counts it.
      await click('lagrange');
      await click('subgroup');
      const count = () => panel.querySelector('.pm-local-count').textContent, chips = () => $$('.pm-hub', panel).map(chip => chip.textContent);
      assert.deepStrictEqual([count(), chips()], ['Showing 1 of 1 result it builds on', ['Group']]);
      assert.strictEqual($('.pm-sr').textContent, 'Local graph of Subgroup. Showing 1 of 1 result it builds on');
      console.log('hub count ok');
    """, _groups(tmp_path))
    assert "hub count ok" in out


def test_a_level_that_adds_nothing_cannot_be_chosen_and_a_graph_where_none_adds_shows_no_levels(tmp_path: Path) -> None:
    out = _walk(tmp_path, _TO_LAGRANGE + r"""
      const button = n => panel.querySelector(`[data-level="${n}"]`);
      const title = () => panel.querySelector('.pm-local-title strong').textContent;
      await click('lagrange');
      // Lagrange's theorem's level 2 draws what its level 1 does: Group, all that level 3 adds, is four steps back.
      assert.deepStrictEqual([1, 2, 3].map(n => button(n).getAttribute('aria-disabled')), [null, 'true', null]);
      assert.strictEqual(button(2).title, 'Shows nothing more than level 1');
      emit(button(2), 'click'); await wait(10);
      assert.strictEqual(level(), '1');
      // Level 3, chosen here, carries over to Index, whose level 3 adds nothing to its level 2: level 2 is pressed,
      // and the choice comes back with Lagrange's theorem.
      emit(button(3), 'click'); await wait(10);
      await click('index');
      assert.deepStrictEqual([title(), level(), button(3).getAttribute('aria-disabled')], ['Index', '2', 'true']);
      history.back(); await wait(40);
      assert.deepStrictEqual([title(), level()], ["Lagrange's theorem", '3']);
      // Group builds on nothing, and Subgroup only on Group, which every level lists: neither offers levels.
      emit(panel.querySelector('.pm-hub'), 'click'); await wait(40);
      assert.deepStrictEqual([title(), panel.querySelector('.pm-local-levels')], ['Group', null]);
      await click('subgroup');
      assert.deepStrictEqual([title(), panel.querySelector('.pm-local-levels')], ['Subgroup', null]);
      // The choice survives both: Lagrange's theorem, among what Subgroup feeds into, opens at level 3 again.
      await click('lagrange');
      assert.deepStrictEqual([title(), level()], ["Lagrange's theorem", '3']);
      // Pressing 2 where it stands in for 3 makes 2 the choice: back at Lagrange's theorem, whose level 2 adds
      // nothing, level 1 is drawn.
      await click('index');
      emit(button(2), 'click'); await wait(10);
      assert.deepStrictEqual([title(), level()], ['Index', '2']);
      await click('lagrange');
      assert.deepStrictEqual([title(), level()], ["Lagrange's theorem", '1']);
      console.log('adds ok');
    """, _groups(tmp_path))
    assert "adds ok" in out


def test_escape_from_a_local_graph_opened_with_nothing_selected_returns_to_the_part_in_view(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const canvas = $('.pm-stage').querySelector('svg'), trail = () => $$('li', $('.pm-trail')).map(li => li.textContent);
      const card = id => $(`.pm-result[data-result="Algebra/groups/${id}"]`);
      // Zoom into Groups, so the view follows the part it is in, with nothing selected.
      for (let i = 0; i < 12 && !(card('lagrange') && card('lagrange').getAttribute('class').includes('pm-card')); i++) {
        const box = card('lagrange').querySelector('rect'), x = +box.getAttribute('x') + box.getAttribute('width') / 2;
        emit(canvas, 'wheel', {deltaX: 0, deltaY: -200, ctrlKey: true, clientX: x, clientY: +box.getAttribute('y') + 10}); await wait(6);
      }
      await wait(20);
      assert.deepStrictEqual(trail(), ['Library', 'Algebra', 'Groups']);
      const where = () => ['x', 'y', 'width'].map(name => card('lagrange').querySelector('rect').getAttribute(name)).join();
      const before = where(), rect = card('lagrange').querySelector('rect');
      emit(rect, 'pointerdown', {button: 0, clientX: 30, clientY: 30, pointerId: 1});
      emit(rect, 'pointerup', {button: 0, clientX: 30, clientY: 30, pointerId: 1}); await wait(40);
      assert(!$('.pm-local').hidden);
      emit(app, 'keydown', {key: 'Escape'}); await wait(30);
      // Nothing selected, the same camera, and the trail and reading pane on Groups, as before the click.
      assert($('.pm-local').hidden && !$('.pm-result.pm-selected'));
      assert.strictEqual(where(), before);
      assert.deepStrictEqual(trail(), ['Library', 'Algebra', 'Groups']);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Groups');
      console.log('nothing selected ok');
    """, _groups(tmp_path))
    assert "nothing selected ok" in out


def test_without_its_relations_file_a_click_on_a_result_still_selects_it(tmp_path: Path) -> None:
    out = _walk(tmp_path, _TO_LAGRANGE + r"""
      await click('index');
      assert(panel.hidden, 'no local graph without its data');
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Index');
      assert(card('index').getAttribute('class').includes('pm-selected'));
      assert($('.pm-hint').textContent.includes('relations.json'), $('.pm-hint').textContent);
      errors.splice(0);
      console.log('fallback ok');
    """, _groups(tmp_path), fail="relations.json")
    assert "fallback ok" in out


def test_a_relations_file_from_another_build_opens_no_graph_and_the_click_still_selects(tmp_path: Path) -> None:
    # A deploy between the two downloads pairs search.json with relations.json from another build: one with a result
    # fewer, or one with as many results and one renamed.
    for name, other in (("fewer", {"exercises": 9}), ("renamed", {"prefix": "x"})):
        groups = _groups(tmp_path / name)
        shutil.copy(_groups(tmp_path / name / "other", **other) / "relations.json", groups / "relations.json")
        out = _walk(tmp_path / name, _TO_LAGRANGE + r"""
          await click('index');
          assert(panel.hidden, 'no local graph from two builds');
          assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Index');
          assert(card('index').getAttribute('class').includes('pm-selected'));
          const hint = $('.pm-hint').textContent;
          assert(hint.includes('relations.json and search.json are from different builds'), hint);
          errors.splice(0);
          console.log('two builds ok');
        """, groups)
        assert "two builds ok" in out, name


def test_a_part_file_that_arrives_late_does_not_take_the_map_back(tmp_path: Path) -> None:
    groups = "parts/" + project_map.part_file("Algebra/groups")
    assert groups != "parts/" + project_map.part_file("Analysis/series")
    out = _walk(tmp_path, r"""
      const search = $('.pm-search').querySelector('input'), go = async query => {
        search.value = query; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
        emit(search, 'keydown', {key: 'Enter'}); await wait(40);
      };
      // The reading pane keeps the title's TeX for MathJax.
      const ABEL = 'Abel summation for \\(\\mathbb{R}\\)';
      // Lagrange's part is still loading when the reader asks for Abel summation instead.
      await go('Subgroup.card_dvd');
      await go('abel_summation');
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, ABEL);
      net.release('GROUPS'); await wait(40);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, ABEL);
      assert.strictEqual(location.hash, '#result=' + encodeURIComponent('Analysis/series/abel'));
      console.log('race ok');
    """.replace("GROUPS", groups), hold=groups)
    assert "race ok" in out


def test_a_file_that_failed_once_loads_when_asked_again(tmp_path: Path) -> None:
    groups = "parts/" + project_map.part_file("Algebra/groups")
    out = _walk(tmp_path, r"""
      // The opening's request for Groups failed: the map says so where it can be seen.
      assert($('.pm-hint').textContent.startsWith('Something went wrong'), $('.pm-hint').textContent);
      const search = $('.pm-search').querySelector('input');
      search.value = 'Subgroup.card_dvd'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      emit(search, 'keydown', {key: 'Enter'}); await wait(40);
      // Asked again, the file loads, and the warning goes.
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, "Lagrange's theorem");
      assert.strictEqual(net.log.filter(name => name === 'GROUPS').length, 2);
      assert(!$('.pm-hint').textContent.startsWith('Something went wrong'), $('.pm-hint').textContent);
      // The one failure the network caused is the only error logged.
      assert.deepStrictEqual(errors.map(error => error.split('\n')[0]), ['TypeError: the network dropped GROUPS']); errors.length = 0;
      console.log('retry ok');
    """.replace("GROUPS", groups), fail=groups)
    assert "retry ok" in out


def test_history_holds_one_entry_per_place_and_the_camera_a_flight_ends_at(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      // A deep link is one entry, and it shows its result.
      assert.strictEqual(history.length, 1);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, "Lagrange's theorem");
      const card = () => ['x', 'y', 'width'].map(name => Math.round(+$('.pm-result.pm-selected').querySelector('rect').getAttribute(name))).join();
      // Going to the same place again is not a new entry.
      const whole = $$('button').find(b => b.textContent === 'Whole project');
      emit(whole, 'click'); await wait(30);
      const entries = history.length;
      emit(whole, 'click'); await wait(30);
      for (let i = 0; i < 3; i++) { emit($('.pm-stage').querySelector('svg'), 'keydown', {key: 'Escape'}); await wait(5); }
      assert.strictEqual(history.length, entries);
      // Back and Forward return to the camera the flight to Lagrange's theorem ended at.
      history.back(); await wait(40);
      const arrived = card();
      history.forward(); await wait(40);
      history.back(); await wait(40);
      assert.strictEqual(card(), arrived);
      // Back before the camera write lands, then Forward: the entry was pushed before the flight, and still ends there.
      history.forward(); await wait(40);
      emit(whole, 'click'); await wait(30);
      const before = history.length, input = $('.pm-search').querySelector('input');
      input.value = 'Lagrange'; emit(input, 'focus'); emit(input, 'input'); await wait(20); emit(input, 'input'); await wait(5);
      emit(input, 'keydown', {key: 'Enter'});
      for (let i = 0; i < 50 && history.length === before; i++) await new Promise(resolve => setImmediate(resolve));
      history.back(); await wait(40);
      history.forward(); await wait(40);
      assert.strictEqual(card(), arrived);
      console.log('history ok');
    """, hash="#result=" + "Algebra%2Fgroups%2Flagrange")
    assert "history ok" in out


def test_a_title_held_at_the_top_is_drawn_as_in_its_band(tmp_path: Path) -> None:
    sections = ["Sylow theorems and their applications", "Group actions", "Sylow subgroups of finite groups and their normalisers",
                "Solvable and nilpotent groups", "The structure of finite abelian groups and their duals", "Representations of finite groups"]
    records: list[dict[str, object]] = [
        {"id": "roadmap", "kind": "scope", "title": "Library", "parent": None, "result": False},
        {"id": "A", "kind": "scope", "title": "Algebra", "parent": "roadmap", "result": False},
        {"id": "B", "kind": "scope", "title": "Analysis", "parent": "roadmap", "result": False},
    ]
    for index, title in enumerate(sections):
        records.append({"id": f"A/s{index}", "kind": "scope", "title": title, "parent": "A", "result": False})
        records += [{"id": f"A/s{index}/r{n}", "kind": "node", "title": f"Result {n}", "parent": f"A/s{index}", "result": True,
                     "status": "planned"} for n in range(8)]
    records += [{"id": f"B/r{n}", "kind": "node", "title": f"Result {n}", "parent": "B", "result": True, "status": "planned"} for n in range(5)]
    sectioned = tmp_path / "sectioned"
    project_map.write_project_map(sectioned, project_map.build_project_map(records, []))
    out = _walk(tmp_path, r"""
      // Its size, lines and results line; a long title in a one-line band, and one wrapped in a two-line band.
      const drawn = id => {
        const lines = $$('.pm-title').filter(t => (t.dataset.key || '').startsWith('lt:' + id + ':'));
        return {size: (/font-size:(\d+)px/.exec(lines[0].getAttribute('style') || '') || [0, '15'])[1], lines: lines.map(t => t.textContent),
          meta: $$('.pm-meta').some(m => m.dataset.key === 'lm:' + id)};
      };
      const ids = ['A/s0', 'A/s2'], before = ids.map(drawn);
      assert(before.every(title => title.size > 15 && title.meta), JSON.stringify(before));
      // Panned until the top row runs above the stage, under the pinned "Algebra", where their titles are held.
      emit($('.pm-stage').querySelector('svg'), 'wheel', {deltaX: 0, deltaY: 120, clientX: 10, clientY: 10}); await wait(20);
      assert(ids.every(id => $$('.pm-backdrop').some(b => b.dataset.key === 'lb:' + id)), 'the titles are held');
      assert.deepStrictEqual(ids.map(drawn), before);
      console.log('held ok');
    """, sectioned, hash="#part=A")
    assert "held ok" in out


def test_titles_always_draw_above_backdrops(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const canvas = $('.pm-stage').querySelector('svg');
      const check = () => {
        const nodes = []; (function walk(node) { nodes.push(node); Array.from(node.children).forEach(walk); })(canvas);
        const kind = node => (node.getAttribute && node.getAttribute('class') || '');
        const lastBackdrop = nodes.findLastIndex(node => kind(node).includes('pm-backdrop'));
        const firstTitle = nodes.findIndex(node => kind(node).includes('pm-title'));
        assert(lastBackdrop < 0 || firstTitle < 0 || lastBackdrop < firstTitle, 'a backdrop is drawn over a title');
        return lastBackdrop >= 0;
      };
      // Zooming into the topics turns some titles into overlays on backdrops, after others were drawn.
      const big = $$('.pm-part').find(n => n.getAttribute('data-part') === 'Big').querySelector('rect');
      const x = +big.getAttribute('x') + big.getAttribute('width') / 2, y = +big.getAttribute('y') + big.getAttribute('height') / 2;
      let backdrops = check();
      for (let i = 0; i < 10; i++) { emit(canvas, 'wheel', {deltaX: 0, deltaY: -150, ctrlKey: true, clientX: x, clientY: y}); await wait(10); backdrops = check() || backdrops; }
      assert(backdrops, 'some title went over its box');
      console.log('paint ok');
    """, _wide(tmp_path))
    assert "paint ok" in out


def test_the_outline_keeps_focus_when_a_region_arrives(tmp_path: Path) -> None:
    analysis = "parts/" + project_map.part_file("Analysis")
    out = _walk(tmp_path, r"""
      const button = () => $$('button', $('.pm-side')).find(b => b.dataset.part === 'Algebra');
      button().focus();
      net.release('ANALYSIS'); await wait(40);
      // The outline now lists Analysis's parts, and focus is still on Algebra's row.
      assert($$('button', $('.pm-side')).some(b => b.dataset.part === 'Analysis/series'), 'the outline grew');
      assert.strictEqual(document.activeElement, button());
      console.log('focus ok');
    """.replace("ANALYSIS", analysis), hold=analysis)
    assert "focus ok" in out


def test_escape_and_up_go_back_one_level_at_a_time(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const trail = () => $$('li', $('.pm-trail')).map(li => li.textContent), escape = async () => { emit(app, 'keydown', {key: 'Escape'}); await wait(30); };
      const search = $('.pm-search').querySelector('input');
      search.value = 'Subgroup.card_dvd'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      emit(search, 'keydown', {key: 'Enter'}); await wait(40);
      search.blur();
      assert.deepStrictEqual(trail(), ['Library', 'Algebra', 'Groups', "Lagrange's theorem"]);
      // The first Escape lets go of the result; each one after goes up a level.
      await escape();
      assert.deepStrictEqual(trail(), ['Library', 'Algebra', 'Groups']);
      await escape();
      assert.deepStrictEqual(trail(), ['Library', 'Algebra']);
      await escape();
      assert.deepStrictEqual(trail(), ['Library']);
      // The Up button goes from a result to its part, then to the part around that.
      search.value = 'Subgroup.card_dvd'; emit(search, 'input'); await wait(5); emit(search, 'keydown', {key: 'Enter'}); await wait(40);
      const up = $$('button').find(b => b.getAttribute('aria-label') === 'Up one level');
      emit(up, 'click'); await wait(40);
      assert.deepStrictEqual(trail(), ['Library', 'Algebra', 'Groups']);
      emit(up, 'click'); await wait(40);
      assert.deepStrictEqual(trail(), ['Library', 'Algebra']);
      console.log('up ok');
    """)
    assert "up ok" in out


def test_modified_keys_and_a_release_off_the_map_leave_the_view_alone(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const canvas = $('.pm-stage').querySelector('svg');
      const left = () => +$$('.pm-part').find(n => n.getAttribute('data-part') === 'Algebra').querySelector('rect').getAttribute('x');
      const start = left();
      // Command-Left and Alt-Left are the browser's Back, and Control-0 resets its zoom: the map lets them through.
      for (const key of [{key: 'ArrowLeft', metaKey: true}, {key: 'ArrowLeft', altKey: true}, {key: '0', ctrlKey: true}]) {
        const event = emit(canvas, 'keydown', key); await wait(10);
        assert.strictEqual(event.defaultPrevented, false, JSON.stringify(key));
      }
      assert.strictEqual(left(), start);
      emit(canvas, 'keydown', {key: 'ArrowLeft'}); await wait(10);
      assert.notStrictEqual(left(), start, 'a plain arrow still pans');
      // A press released off the map sends no pointerup here; the next move, with no button down, pans nothing.
      const moved = left();
      emit(canvas, 'pointerdown', {button: 0, buttons: 1, pointerType: 'mouse', pointerId: 1, clientX: 300, clientY: 300});
      emit(canvas, 'pointermove', {buttons: 0, pointerType: 'mouse', pointerId: 1, clientX: 360, clientY: 300}); await wait(10);
      assert.strictEqual(left(), moved);
      console.log('keys ok');
    """)
    assert "keys ok" in out


def test_a_pinch_zooms_and_back_from_an_outline_part_keeps_the_pane(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const canvas = $('.pm-stage').querySelector('svg'), reader = $('.pm-reader');
      const width = () => +$$('.pm-part').find(n => n.getAttribute('data-part') === 'Algebra').querySelector('rect').getAttribute('width');
      // Two fingers spreading from 100 to 200 px apart zoom twice as close, and lifting them picks nothing.
      const touch = (type, id, x) => emit(canvas, type, {button: 0, pointerType: 'touch', pointerId: id, clientX: x, clientY: 300});
      const before = width();
      touch('pointerdown', 1, 300); touch('pointerdown', 2, 400); touch('pointermove', 2, 500); await wait(10);
      assert(Math.abs(width() / before - 2) < 0.01, String(width() / before));
      touch('pointerup', 2, 500); touch('pointerup', 1, 300); await wait(10);
      assert.strictEqual($$('.pm-part').filter(n => n.getAttribute('class').includes('pm-picked')).length, 0);
      // Back from a part chosen in the outline returns the reading pane to where it was scrolled.
      emit($$('button').find(b => b.textContent === 'Whole project'), 'click'); await wait(30);
      reader.scrollTop = 120;
      emit($$('button', $('.pm-side')).find(b => b.dataset.part === 'Analysis'), 'click'); await wait(40);
      assert.strictEqual(reader.scrollTop, 0, 'the part opens at the top of its pane');
      history.back(); await wait(40);
      assert.strictEqual(reader.scrollTop, 120);
      console.log('pinch ok');
    """)
    assert "pinch ok" in out


def test_titles_with_formulas_are_typeset_once_by_the_sites_mathjax(tmp_path: Path) -> None:
    library = tmp_path / "formulas"
    project_map.write_project_map(library, project_map.build_project_map([
        {"id": "roadmap", "kind": "scope", "title": "Library", "parent": None, "result": False},
        {"id": "Algebra", "kind": "scope", "title": "Algebra", "parent": "roadmap", "result": False, "area": "Algebra"},
        {"id": "Algebra/order", "kind": "scope", "title": "Groups of order $p^2$", "parent": "Algebra", "result": False,
         "navigation": "module"},
        {"id": "Algebra/order/abel", "kind": "node", "title": r"Abel summation for $\mathbb{R}$", "parent": "Algebra/order",
         "result": True, "status": "planned"},
        {"id": "Algebra/order/lagrange", "kind": "node", "title": "Lagrange's theorem", "parent": "Algebra/order", "result": True,
         "status": "planned"},
        {"id": "Algebra/order/price", "kind": "node", "title": "A $5 bound", "parent": "Algebra/order", "result": True,
         "status": "planned"},
    ], []))
    out = _walk(tmp_path, r"""
      const canvas = $('.pm-stage').querySelector('svg'), layer = $('.pm-tex-layer');
      const show = async () => {
        const search = $('.pm-search').querySelector('input');
        search.value = 'abel'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
        emit(search, 'keydown', {key: 'Enter'}); await wait(40);
      };
      const svgText = () => $$('text').map(node => node.textContent).join(' | ');
      // Without the site's MathJax, a formula is drawn as readable SVG text.
      await show();
      assert(svgText().includes('Abel summation for ℝ'), svgText());
      assert.strictEqual(layer.children.length, 0);
      // With it, the titles with formulas move to HTML for MathJax to set, each once; the others stay SVG text.
      const asked = [];
      window.AutoformMath = {refresh: root => { asked.push(root); return Promise.resolve(true); }};
      emit(canvas, 'keydown', {key: '+'}); await wait(20);
      const card = $$('.pm-tex').find(node => node.classList.contains('pm-card-title'));
      assert(card, 'the card title is HTML');
      assert.deepStrictEqual($$('.arithmatex', card).map(node => node.textContent), ['\\(\\mathbb{R}\\)']);
      assert(!svgText().includes('Abel summation'), svgText());
      assert(svgText().includes("Lagrange's theorem") && svgText().includes('A $5 bound'), svgText());
      assert($$('.pm-tex').every(node => node.querySelector('.arithmatex')), 'only titles with formulas are HTML');
      assert(asked.includes(layer), 'the layer is handed to MathJax');
      // Out of view the title leaves the stage; back in view the same node returns without another pass.
      for (let i = 0; i < 12; i++) { emit(canvas, 'keydown', {key: '-'}); await wait(5); }
      await wait(20);
      assert(!card.parentNode, 'zoomed out, the card title is gone');
      card.appendChild(document.createElement('mjx-container'));
      const passes = asked.length;
      await show();
      assert.strictEqual(card.parentNode, layer);
      assert.strictEqual(asked.filter(root => root === layer).length, asked.slice(0, passes).filter(root => root === layer).length);
      console.log('formulas ok');
    """, library)
    assert "formulas ok" in out
