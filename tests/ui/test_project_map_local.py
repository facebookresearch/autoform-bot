"""A result's local dependency graph: the rules in project-map-local.js, through the production Node module."""

from __future__ import annotations

import json
import random
from pathlib import Path

from autoform_cli.ui import project_map
from tests.dom_fixture import run_dom

_MODULE = Path(__file__).resolve().parents[2] / "autoform_cli/ui/assets/project-map-local.js"

_HARNESS = r"""
const assert = require('assert');
load(args.module);
const LG = window.AutoformLocalGraph;
// A graph over rows 0 .. n - 1 from {user: [used, ...]}, with {user: [used, ...]} for links only in a proof.
function graph(n, uses, proof = {}) {
  const rows = Array.from({length: n}, (_, row) => row);
  return LG.graph({statuses: ['planned'], status: rows.map(() => 0), uses: rows.map(row => uses[row] || []),
    proof: rows.map(row => proof[row] || [])});
}
const rowsOf = v => v.nodes.map(node => node.row).sort((a, b) => a - b);
const depthOf = v => Object.fromEntries(v.nodes.map(node => [node.row, node.depth]));
const edgesOf = v => v.edges.map(edge => edge.join(' ')).sort();
// The module's arrays come from the page's realm, so they are compared as JSON.
const same = (actual, expected) => assert.strictEqual(JSON.stringify(actual), JSON.stringify(expected));
"""


def _run(body: str, **args: object) -> str:
    return run_dom(_HARNESS + body, args={"module": str(_MODULE), **args})


def test_levels_cut_the_reduced_ancestry_and_draw_every_direct_use_beside_the_focus() -> None:
    out = _run(r"""
      // 0 uses 1, 3 and 4; 1 and 3 use 2; 2 uses 4 and 6; 4 uses 5. The link 0 -> 4 is implied by 0 -> 1 -> 2 -> 4.
      const g = graph(7, {0: [1, 3, 4], 1: [2], 3: [2], 2: [4, 6], 4: [5]});
      const one = LG.view(g, 0, 1), two = LG.view(g, 0, 2), three = LG.view(g, 0, 3);
      // Level 1: depth 2 of the reduction, and the direct use 4 (depth 3 there) in the focus's own column.
      same(rowsOf(one), [1, 2, 3, 4]);
      same(depthOf(one), {1: 1, 2: 2, 3: 1, 4: 1});
      same(edgesOf(one), ['0 1 0', '0 3 0', '0 4 0', '1 2 0', '2 4 0', '3 2 0']);
      // Level 2 adds depth 3; level 3 the rest.
      same(rowsOf(two), [1, 2, 3, 4, 6]);
      same(rowsOf(three), [1, 2, 3, 4, 5, 6]);
      same(edgesOf(three), ['0 1 0', '0 3 0', '0 4 0', '1 2 0', '2 4 0', '2 6 0', '3 2 0', '4 5 0']);
      assert([one, two, three].every(v => v.total === 6 && v.via.length === 0 && v.chips.length === 0));
      // A link only in a proof says so.
      same(edgesOf(LG.view(graph(2, {}, {0: [1]}), 0, 1)), ['0 1 1']);
      console.log('levels ok');
    """)
    assert "levels ok" in out


def test_hubs_are_chips_and_a_via_edge_counts_the_results_it_skips() -> None:
    out = _run(r"""
      // 0 uses 1 and the hub 2; 1 uses 2; 2 uses 3; 3 uses 4. Rows 5 .. 14 use the hub too: 12 users.
      const uses = {0: [1, 2], 1: [2], 2: [3], 3: [4]};
      for (let row = 5; row < 15; row++) uses[row] = [2];
      const g = graph(15, uses);
      // The 98th percentile of 15 in-degrees is the largest, 12, over the floor of 10.
      assert.strictEqual(g.hub, 12);
      const one = LG.view(g, 0, 1), two = LG.view(g, 0, 2), three = LG.view(g, 0, 3);
      // A direct use that is a hub is a chip, never a card.
      same([rowsOf(one), one.chips, one.via], [[1], [2], []]);
      // Below the hub, 3 hangs from 1 by a via edge that skips one result, the hub.
      same([rowsOf(two), two.via], [[1, 3], [[1, 3, 1]]]);
      same([rowsOf(three), edgesOf(three), three.via], [[1, 3, 4], ['0 1 0', '3 4 0'], [[1, 3, 1]]]);
      // `total` counts what the focus builds on, hubs aside.
      assert.strictEqual(three.total, 3);
      // A small graph's threshold is the floor.
      assert.strictEqual(graph(3, {0: [2], 1: [2]}).hub, 10);
      console.log('hubs ok');
    """)
    assert "hubs ok" in out


def test_feeds_into_lists_the_most_used_users_first_up_to_eight() -> None:
    out = _run(r"""
      // Rows 1 .. 10 use 0, and row r is used r times, by rows 11 .. 10 + r.
      const uses = {};
      for (let r = 1; r <= 10; r++) uses[r] = [0];
      for (let j = 1; j <= 10; j++) uses[10 + j] = Array.from({length: 11 - j}, (_, k) => j + k);
      const v = LG.view(graph(21, uses), 0, 1);
      same([v.feeds, v.feedsTotal], [[10, 9, 8, 7, 6, 5, 4, 3], 10]);
      console.log('feeds ok');
    """)
    assert "feeds ok" in out


def test_a_cycle_in_the_published_graph_drops_the_link_that_closes_it() -> None:
    out = _run(r"""
      const v = LG.view(graph(3, {0: [1], 1: [2], 2: [1]}), 0, 3);
      same([rowsOf(v), edgesOf(v), v.via], [[1, 2], ['0 1 0', '1 2 0'], []]);
      console.log('cycle ok');
    """)
    assert "cycle ok" in out


def test_a_level_draws_at_most_its_cap_nearest_first() -> None:
    out = _run(r"""
      // 0 uses 1 .. 5, and each of those ten results of its own: 55 in all.
      const uses = {0: [1, 2, 3, 4, 5]};
      for (let d = 1; d <= 5; d++) uses[d] = Array.from({length: 10}, (_, k) => 5 + (d - 1) * 10 + k + 1);
      const v = LG.view(graph(56, uses), 0, 3, 20);
      assert.strictEqual(v.total, 55);
      // The direct uses first, then the nearest in search.json's order.
      same(rowsOf(v), Array.from({length: 20}, (_, k) => k + 1));
      assert.strictEqual(LG.constants.CAP, 150);
      console.log('cap ok');
    """)
    assert "cap ok" in out


def test_layout_puts_uses_left_of_the_focus_by_depth_and_what_it_feeds_into_right() -> None:
    out = _run(r"""
      const uses = {0: [1, 3, 4], 1: [2], 3: [2], 2: [4, 6], 4: [5]};
      for (let row = 7; row < 17; row++) uses[row] = [0];
      const v = LG.view(graph(17, uses), 0, 3), layout = LG.layout(v), boxes = layout.boxes;
      assert.strictEqual(boxes[0].x, 0);
      // Columns by depth, further out the deeper; the eight feeds in one column to the right, and "+2 more" under them.
      const column = row => boxes[row].x;
      v.nodes.forEach(node => assert(column(node.row) === -(node.depth) * (LG.constants.CARD_W + 96), JSON.stringify(node)));
      assert(v.feeds.every(row => column(row) > 0) && v.feeds.length === 8 && layout.more);
      const list = Object.values(boxes);
      list.forEach((a, i) => list.slice(i + 1).forEach(b => assert(
        Math.min(a.x + a.w, b.x + b.w) <= Math.max(a.x, b.x) || Math.min(a.y + a.h, b.y + b.h) <= Math.max(a.y, b.y), 'cards overlap')));
      list.forEach(box => assert(box.x >= layout.bounds.x && box.y >= layout.bounds.y &&
        box.x + box.w <= layout.bounds.x + layout.bounds.w && box.y + box.h <= layout.bounds.y + layout.bounds.h));
      console.log('layout ok');
    """)
    assert "layout ok" in out


def atlas_shaped(seed: int = 0) -> tuple[list[dict[str, object]], list[tuple[str, str, int, int]]]:
    """Map records and relations shaped like ATLAS on 2026-11-07: about 50,000 results and 141,000 relations, in-degree
    at the median 3 and at the 98th percentile about 27, the most used about 1,500, so about 1,000 hubs.

    1,800 foundations come first, each using two earlier ones: some at random, some as often as they are used already,
    and a core of 30 used far more. Then 50 chapters of 10 sections each. A lemma uses about one earlier lemma near
    it, each lemma about three times in all, and a foundation or two; a theorem uses two or three lemmas and a
    foundation. Six capstones at the end use the last results of every chapter, so a few ancestries span the library.
    """
    rng = random.Random(seed)
    results, foundations, chapters, sections = 50_000, 1_800, 50, 10
    uses: list[set[int]] = [set() for _ in range(results)]
    pool: list[int] = []

    def count(mean: float) -> int:
        return int(mean) + (rng.random() < mean - int(mean))

    def foundation(below: int = foundations) -> int:
        if rng.random() < 0.035:
            return min(below - 1, int(30 * rng.random() ** 2))
        if not pool or rng.random() < 0.5:
            return rng.randrange(below)
        pick = rng.choice(pool)
        return pick if pick < below else rng.randrange(below)

    for row in range(1, foundations):
        for _ in range(2):
            pick = foundation(row)
            uses[row].add(pick)
            pool.append(pick)
    size, lasts = (results - foundations) // chapters, []
    for chapter in range(chapters):
        tokens: list[int] = []
        end = foundations + (chapter + 1) * size if chapter < chapters - 1 else results
        for row in range(foundations + chapter * size, end):
            lemma = rng.random() < 0.53
            near, base = (0.9, 1.3) if lemma else (2.3, 1.4)
            for _ in range(count(near)):
                for _attempt in range(4):
                    if not tokens:
                        break
                    at = rng.randrange(min(12, len(tokens)))
                    if tokens[at] not in uses[row]:
                        uses[row].add(tokens.pop(at))
                        break
            for _ in range(count(base)):
                pick = foundation()
                uses[row].add(pick)
                pool.append(pick)
            if lemma:
                tokens.extend([row] * 3)
        lasts.extend(range(end - 8, end))
    for row in range(results - 6, results):
        uses[row].update(last for last in lasts if last < row)

    def place(row: int) -> tuple[str, str]:
        if row < foundations:
            return "foundations", f"foundations/s{row * sections // foundations}"
        chapter = min(chapters - 1, (row - foundations) // size)
        offset = row - foundations - chapter * size
        return f"c{chapter:02d}", f"c{chapter:02d}/s{min(sections - 1, offset * sections // size)}"

    records: list[dict[str, object]] = [{"id": "roadmap", "kind": "scope", "title": "Synthetic library", "parent": None, "result": False}]
    seen: set[str] = set()
    statuses = ("fully_proved", "proved", "stated", "can_prove", "planned")
    for row in range(results):
        chapter, section = place(row)
        for scope, parent in ((chapter, "roadmap"), (section, chapter)):
            if scope not in seen:
                seen.add(scope)
                records.append({"id": scope, "kind": "scope", "title": scope.replace("/", " "), "parent": parent, "result": False})
        records.append({"id": f"{section}/r{row:05d}", "kind": "node", "title": f"Result {row}", "parent": section,
                        "result": True, "status": statuses[row % len(statuses)]})
    ids = {row: f"{place(row)[1]}/r{row:05d}" for row in range(results)}
    relations = [(ids[user], ids[used], 1, 0) for user in range(results) for used in sorted(uses[user])]
    return records, relations


def test_on_an_atlas_shaped_graph_every_level_keeps_its_rules(tmp_path: Path) -> None:
    files = project_map.build_project_map(*atlas_shaped())
    table = tmp_path / "relations.json"
    table.write_text(json.dumps(files["relations.json"]), encoding="utf-8")
    out = _run(r"""
      const payload = JSON.parse(require('fs').readFileSync(args.table, 'utf8')), g = LG.graph(payload);
      const sorted = Int32Array.from(g.indegree).sort(), at = q => sorted[Math.ceil(q * g.n) - 1];
      console.log(`shape: ${g.n} results, ${g.to.length} relations, in-degree p50 ${at(0.5)}, p98 ${at(0.98)}, max ${sorted[g.n - 1]}, hubs ${sorted.filter(d => d >= g.hub).length}`);
      assert(g.n === 50000 && g.to.length > 135000 && g.to.length < 150000);
      assert(at(0.5) === 3 && g.hub >= 24 && g.hub <= 34 && sorted[g.n - 1] > 1000);
      // The results that use the most (the capstones), the most used (hubs), and 300 others.
      const rows = Array.from({length: g.n}, (_, row) => row), outdegree = row => g.start[row + 1] - g.start[row];
      const most = (key, count) => rows.slice().sort((a, b) => key(b) - key(a)).slice(0, count);
      const random = (seed => () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648)(1);
      const foci = most(outdegree, 6).concat(most(row => g.indegree[row], 3), Array.from({length: 300}, () => Math.floor(random() * g.n)));
      const worst = {1: 0, 2: 0, 3: 0};
      let widest = 0;
      foci.forEach(focus => {
        const views = [1, 2, 3].map(level => {
          const start = process.hrtime.bigint(), v = LG.view(g, focus, level), layout = LG.layout(v);
          worst[level] = Math.max(worst[level], Number(process.hrtime.bigint() - start) / 1e6);
          const drawn = new Set(v.nodes.map(node => node.row)), reached = new Set();
          assert(v.nodes.length <= LG.constants.CAP && v.nodes.length <= v.total);
          v.nodes.forEach(node => assert(g.indegree[node.row] < g.hub, 'a hub is never a card'));
          v.edges.forEach(([user, used]) => {
            if (used === focus) return assert(v.feeds.includes(user));
            assert((user === focus || drawn.has(user)) && drawn.has(used)); reached.add(used);
          });
          v.via.forEach(([user, used, skipped]) => { assert((user === focus || drawn.has(user)) && skipped >= 1); reached.add(used); });
          drawn.forEach(row => assert(reached.has(row), 'every card hangs from an edge or a via edge'));
          assert(Object.keys(layout.boxes).length === v.nodes.length + 1 + v.feeds.length);
          return drawn;
        });
        // Each level holds the one below it, until the cap cuts it.
        if (views[1].size < LG.constants.CAP) views[0].forEach(row => assert(views[1].has(row)));
        if (views[2].size < LG.constants.CAP) views[1].forEach(row => assert(views[2].has(row)));
        widest = Math.max(widest, LG.view(g, focus, 3).total);
      });
      assert(widest > 3000, 'the capstones span the library: ' + widest);
      console.log(`widest ancestry ${widest}; slowest view and layout, ms: level 1 ${worst[1].toFixed(1)}, level 2 ${worst[2].toFixed(1)}, level 3 ${worst[3].toFixed(1)}`);
      console.log('atlas ok');
    """, table=str(table))
    print(out)
    assert "atlas ok" in out
