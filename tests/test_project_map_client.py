"""The whole-project map client, run over real map data in a small DOM stand-in (not browser evidence)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from autoform_cli import project_map

_NODE = shutil.which("node")
_CLIENT = Path(__file__).resolve().parents[1] / "autoform_cli/assets/project-map.js"
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


def _walk(tmp_path: Path, script: str) -> str:
    path = tmp_path / "walk.js"
    path.write_text(script, encoding="utf-8")
    run = subprocess.run([_NODE, str(_DOM), str(_CLIENT), str(_library(tmp_path)), str(path)],
                         capture_output=True, text=True, timeout=60)
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
      assert($$('.pm-rel').length >= 1, 'selection draws its relationships');
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


def test_titles_with_tex_are_readable_on_the_map(tmp_path: Path) -> None:
    out = _walk(tmp_path, r"""
      const search = $('.pm-search').querySelector('input');
      search.value = 'abel'; emit(search, 'focus'); emit(search, 'input'); await wait(20); emit(search, 'input'); await wait(5);
      const hit = $$('.pm-hit')[0];
      assert.strictEqual(hit.querySelector('.pm-hit-title').textContent, 'Abel summation for ℝ');
      console.log('tex ok');
    """)
    assert "tex ok" in out
