"""The whole-project map client, run over real map data in a small DOM stand-in (not browser evidence)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from autoform_cli import project_map

_NODE = shutil.which("node")
_ASSETS = Path(__file__).resolve().parents[1] / "autoform_cli/assets"
_DOM = Path(__file__).resolve().with_name("project_map_dom.js")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node runs the map client")


def _library(tmp_path: Path) -> Path:
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
    relations = [("Algebra/groups/cauchy", "Algebra/groups/lagrange", 1, 0), ("Analysis/series/abel", "Algebra/groups/lagrange", 0, 1)]
    project_map.write_project_map(tmp_path / "map", project_map.build_project_map(records, relations))
    return tmp_path / "map"


def _walk(tmp_path: Path, script: str, map_dir: Path | None = None, **env: str) -> str:
    """Run ``script`` against the map at ``map_dir``; ``env`` sets MAP_HASH, MAP_HOLD and MAP_FAIL (see project_map_dom.js)."""
    path = tmp_path / "walk.js"
    path.write_text(script, encoding="utf-8")
    # The page loads the layout module and the controller as one file, in this order.
    client = tmp_path / "project-map.js"
    client.write_text("\n".join((_ASSETS / name).read_text(encoding="utf-8") for name in ("project-map-layout.js", "project-map.js")),
                      encoding="utf-8")
    run = subprocess.run([_NODE, str(_DOM), str(client), str(map_dir or _library(tmp_path)), str(path)],
                         capture_output=True, text=True, timeout=60, env={**os.environ, **env})
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-4000:]
    return run.stdout


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
      const names = usedBy[usedBy.length - 1].querySelectorAll('button').map(b => b.querySelector('span').textContent).sort();
      assert.deepStrictEqual(names, ['Abel summation for ℝ', "Cauchy's theorem"]);
      // Its card carries its title, and the link from Cauchy's theorem, inside the part, is highlighted.
      assert($$('.pm-card').length >= 2, 'cards carry titles at this zoom');
      assert($$('.pm-involved').length >= 1, 'selection highlights its links');
      // Following one pivots the focus; Back restores the previous selection.
      const cauchy = usedBy[usedBy.length - 1].querySelectorAll('button').find(b => b.textContent.includes('Cauchy'));
      emit(cauchy, 'click'); await wait(40);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, "Cauchy's theorem");
      history.back(); await wait(40);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, "Lagrange's theorem");
      // The whole project is one action away, and the trail names where the theorem lives.
      assert.deepStrictEqual($('.pm-trail').querySelectorAll('li').map(li => li.textContent), ['Library', 'Algebra', 'Groups', "Lagrange's theorem"]);
      emit(app.querySelectorAll('button').find(b => b.textContent === 'Whole project'), 'click'); await wait(30);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Library');
      console.log('walk ok');
    """)
    assert "walk ok" in out


def test_zooming_into_a_region_opens_it_in_place(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      assert.strictEqual($$('.pm-result').length, 3, 'the opening shows every result');
      assert.deepStrictEqual($('.pm-trail').querySelectorAll('li').map(li => li.textContent), ['Library']);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Library');
      // Zoom towards Lagrange's theorem.
      const canvas = $('.pm-stage').querySelector('svg'), card = $$('.pm-result')[0].querySelector('rect');
      const x = +card.getAttribute('x') + card.getAttribute('width') / 2, y = +card.getAttribute('y') + card.getAttribute('height') / 2;
      for (let i = 0; i < 3; i++) { emit(canvas, 'wheel', {deltaX: 0, deltaY: -320, ctrlKey: true, clientX: x, clientY: y}); await wait(4); }
      await wait(20);
      // The same page now reads Groups from inside: its results are cards with titles, and the trail says where we are.
      assert.strictEqual(location.hash, '', 'zooming is not navigation');
      assert($$('.pm-card').length >= 1, 'the result carries its title');
      assert.deepStrictEqual($('.pm-trail').querySelectorAll('li').map(li => li.textContent), ['Library', 'Algebra', 'Groups']);
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
      // The tile names what it holds.
      assert($$('.pm-inside').some(line => line.textContent.startsWith('Topic 00\u00a0· Topic 01')), $$('.pm-inside').map(l => l.textContent).join('; '));
      // Closer in, it opens, and its topics carry their names.
      const r = big().querySelector('rect'), x = +r.getAttribute('x') + r.getAttribute('width') / 2, y = +r.getAttribute('y') + r.getAttribute('height') / 2;
      for (let i = 0; i < 4; i++) { emit($('.pm-stage').querySelector('svg'), 'wheel', {deltaX: 0, deltaY: -400, ctrlKey: true, clientX: x, clientY: y}); await wait(10); }
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
      const usedBy = () => reader.querySelectorAll('.pm-paged').at(-1);
      assert.strictEqual(usedBy().querySelectorAll('li').length, 12);
      emit(usedBy().querySelector('.pm-show-more'), 'click');
      assert.strictEqual(usedBy().querySelectorAll('li').length, 20);
      assert(usedBy().querySelector('.pm-show-more').hidden, 'nothing is left to show');
      // Following one and coming Back restores the search box, the reading pane's scroll and the announcement.
      reader.scrollTop = 140;
      emit(usedBy().querySelectorAll('button').find(b => b.textContent.includes('User 15')), 'click'); await wait(40);
      assert.strictEqual(reader.querySelector('h2').textContent, 'User 15');
      search.value = 'something else'; reader.scrollTop = 0;
      history.back(); await wait(40);
      assert.strictEqual(reader.querySelector('h2').textContent, 'Hub lemma');
      assert.strictEqual(search.value, 'hub');
      assert.strictEqual(reader.scrollTop, 140);
      assert.strictEqual($('.pm-sr').textContent, 'Showing Hub lemma');
      emit(app.querySelectorAll('button').find(b => b.textContent === 'Whole project'), 'click'); await wait(30);
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
      const names = role => $('.pm-reader').querySelector('.pm-' + role).querySelectorAll('button')
        .map(button => button.querySelector('.pm-link-name').querySelector('span').textContent);
      // One click picks Analysis where it is: the pane names what it builds on, outlined, with an arrow from it.
      await tap('Analysis');
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, 'Analysis');
      assert.strictEqual(location.hash, '', 'picking is not navigation');
      assert.deepStrictEqual(names('uses'), ['Algebra']);
      assert(tile('Algebra').getAttribute('class').includes('pm-uses') && tile('Analysis').getAttribute('class').includes('pm-picked'));
      assert.strictEqual($$('.pm-link').length, 1);
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
    """.replace("GROUPS", groups), MAP_HOLD=groups)
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
      assert.deepStrictEqual(errors, ['TypeError: the network dropped GROUPS']); errors.length = 0;
      console.log('retry ok');
    """.replace("GROUPS", groups), MAP_FAIL=groups)
    assert "retry ok" in out


def test_history_holds_one_entry_per_place_and_the_camera_a_flight_ends_at(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      // A deep link is one entry, and it shows its result.
      assert.strictEqual(history.length, 1);
      assert.strictEqual($('.pm-reader').querySelector('h2').textContent, "Lagrange's theorem");
      const card = () => ['x', 'y', 'width'].map(name => Math.round(+$('.pm-result.pm-selected').querySelector('rect').getAttribute(name))).join();
      // Going to the same place again is not a new entry.
      const whole = app.querySelectorAll('button').find(b => b.textContent === 'Whole project');
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
      console.log('history ok');
    """, MAP_HASH="#result=" + "Algebra%2Fgroups%2Flagrange")
    assert "history ok" in out


def test_titles_always_draw_above_backdrops(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const canvas = $('.pm-stage').querySelector('svg');
      const check = () => {
        const nodes = []; (function walk(node) { nodes.push(node); node.children.forEach(walk); })(canvas);
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
      const button = () => $('.pm-side').querySelectorAll('button').find(b => b.dataset.part === 'Algebra');
      button().focus();
      net.release('ANALYSIS'); await wait(40);
      // The outline now lists Analysis's parts, and focus is still on Algebra's row.
      assert($('.pm-side').querySelectorAll('button').some(b => b.dataset.part === 'Analysis/series'), 'the outline grew');
      assert.strictEqual(document.activeElement, button());
      console.log('focus ok');
    """.replace("ANALYSIS", analysis), MAP_HOLD=analysis)
    assert "focus ok" in out
