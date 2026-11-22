"""The statement list of a built book, in jsdom: built only when it opens, paged, filtered, restored, and searched."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.scaffold import scaffold_project
from tests.explorer_fixture import ARTICLE, build_site, run_book

#: A theorem with no prerequisites, as ARTICLE writes one; ``proved`` adds its formalized proof.
_PLAIN = "This has no prerequisites."


def _article(title: str, *, lean: str, body: str, proved: bool = False) -> str:
    text = ARTICLE.format(kind="theorem", lean=lean, title=title, body=body, uses=_PLAIN)
    return text.replace("statement: formalized\n", "statement: formalized\nproof: formalized\n") if proved else text


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A book whose chapter holds 150 results, every other one proved, a module article and a note, beside two source
    texts: more than a page of the list."""
    project = tmp_path_factory.mktemp("library")
    scaffold_project(project, title="Library")
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text("# Library\n\nThe whole library.\n", encoding="utf-8")
    (roadmap / "results").mkdir()
    (roadmap / "results/README.md").write_text("# Results\n\nEvery result of the library.\n", encoding="utf-8")
    for n in range(150):
        (roadmap / f"results/result-{n:03d}.md").write_text(
            _article(f"Result {n}", lean=f"Library.result{n}", body=f"Source statement {n}.", proved=n % 2 == 1),
            encoding="utf-8",
        )
    (roadmap / "results/module.md").write_text(
        "---\ncatalog: module\n---\n\n# Module article\n\nThe declarations of a module.\n", encoding="utf-8")
    (roadmap / "results/note.md").write_text("# Narrative note\n\nA note between the results.\n", encoding="utf-8")
    for n in range(2):
        (roadmap / f"source-{n}").mkdir()
        (roadmap / f"source-{n}/README.md").write_text(
            f"---\nnavigation: source\n---\n\n# Source {n}\n\nA source text.\n", encoding="utf-8")
    return build_site(project)


@pytest.fixture(scope="module")
def words(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A book of entries whose search ranks tie, change order as a query grows, or lowercase in Greek and Turkish, beside
    a chapter of thirty results and one more Zeckendorf entry."""
    project = tmp_path_factory.mktemp("words")
    scaffold_project(project, title="Ranks")
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text("# Ranks\n\nWords.\n", encoding="utf-8")
    entries = [
        ("capri", "Capri numbers", None, "A prime-like sequence."),
        ("zeckendorf", "Zeckendorf representation", None, "Every positive integer is a sum of Fibonacci numbers."),
        ("root", "Primitive root", "ZMod.IsPrimitiveRoot", "A generator of the units mod n."),
        ("prime", "Prime numbers", "Nat.Prime", "There are infinitely many."),
        ("pri", "Pri", "Nu.lemma", None), ("odos", "ΟΔΟΣ", None, None), ("odosa", "ΟΔΟΣΑ", None, None),
        ("kent", "İstanbul", None, None), ("zeta", "Riemann zeta", None, "Zeckendorf repr. of nothing"),
    ]
    for slug, title, lean, summary in entries:
        front = "---\ndeclaration: theorem\n" + (f"lean: {lean}\n" if lean else "") + "---\n\n"
        prose = f"{summary}\n\n" if summary else ""
        (roadmap / f"{slug}.md").write_text(f"{front}# {title}\n\n{prose}## Depends on\n\n{_PLAIN}\n", encoding="utf-8")
    (roadmap / "more").mkdir()
    (roadmap / "more/README.md").write_text("# More\n\nThirty more.\n", encoding="utf-8")
    plain = "---\ndeclaration: theorem\n---\n\n# {}\n\n## Depends on\n\n" + _PLAIN + "\n"
    for n in range(30):
        (roadmap / f"more/result-{n}.md").write_text(plain.format(f"Result {n}"), encoding="utf-8")
    (roadmap / "more/reps.md").write_text(plain.format("Zeckendorf representations"), encoding="utf-8")
    return build_site(project)


# What a reader does with the list and sees of it.
_LIST = r"""
const rows = () => $$('.bp-dag-inventory-row').map(row => row.dataset.inventoryNodeId);
const row = id => $$('.bp-dag-inventory-row').find(row => row.dataset.inventoryNodeId === id);
const control = text => $$('button').find(button => button.textContent === text);
const listed = () => !$('.bp-dag-inventory').hidden;
// The count before the list's "search the whole roadmap" link: "100 of 151", "7 statements".
const counted = () => $('.bp-dag-inventory-summary').firstChild.textContent;
const filteredOut = () => $('.bp-dag-hidden-button');
async function browse(choice) {
  click($('.bp-dag-browse > button')); await tick(0);
  click($$('.bp-dag-browse-popover button').find(button => button.textContent === choice)); await tick(0);
}
async function search(value) { const box = $('.bp-dag-search'); box.value = value; emit(box, 'input'); await tick(0); }
// The status filter keeps exactly KEEP, or every status.
async function keep(...statuses) {
  if ($('.bp-dag-filter-popover').hidden) click($('.bp-dag-filter-summary'));
  for (const input of $$('.bp-dag-filter-popover input')) {
    if (input.checked !== (!statuses.length || statuses.includes(input.value))) { input.click(); await tick(0); }
  }
}
"""


def test_the_map_leaves_the_list_unbuilt_and_untouched_whatever_the_query_or_filter(library: Path) -> None:
    run_book(library, "roadmap/", _LIST + r"""
      // Nothing is built for a list the page has not shown.
      assert.equal(listed(), false); assert.deepEqual(rows(), []);
      await browse('All statements'); const built = $$('.bp-dag-inventory-row'), scroll = $('.bp-dag-inventory-scroll');
      scroll.scrollTop = 47; click(control('Back to map')); await tick(200);
      // Back on the map, a search or a filter changes nothing in the hidden list: no row is built, removed or rewritten.
      const changes = [], watch = new window.MutationObserver(records => changes.push(...records));
      for (const part of [scroll, $('.bp-dag-inventory-summary')]) watch.observe(part, {subtree: true, childList: true, attributes: true, characterData: true});
      await search('Result 130'); await keep('fully_proved'); await search(''); await keep(); await flush();
      assert.equal(changes.length, 0); assert.equal(listed(), false);
      assert.deepEqual($$('.bp-dag-inventory-row'), built); assert.equal(counted(), '100 of 151'); assert.equal(scroll.scrollTop, 47);
      watch.disconnect();
    """)


def test_all_statements_pages_a_hundred_results_and_articles_at_a_time_and_source_texts_list_apart(library: Path) -> None:
    run_book(library, "roadmap/", _LIST + r"""
      await browse('All statements');
      assert.equal(listed(), true); assert.equal(rows().length, 100); assert.equal(counted(), '100 of 151');
      // The list holds the results and the module article, never the source texts, the note or the chapters.
      assert(rows().every(id => /^results\/result-\d{3}$/.test(id)), rows().join());
      const more = $('.bp-dag-load-more'); assert.equal(more.hidden, false); assert.equal(more.textContent, 'Show 51 more');
      click(more); await tick(0);
      assert.equal(rows().length, 151); assert(rows().includes('results/result-149')); assert(rows().includes('results/module'));
      assert.equal(counted(), '151 articles'); assert.equal(more.hidden, true);
      // Paging moves focus to the first new row.
      assert.equal(document.activeElement, row('results/result-100'));
      await browse('Source texts');
      assert.deepEqual(rows(), ['source-0', 'source-1']); assert.equal(counted(), '2 sources');
      assert.equal($('.bp-dag-inventory h2').textContent, 'Source texts');
      // A row opens its statement.
      await browse('All statements'); click(row('results/result-149')); await tick(200);
      assert.equal(location.href, 'https://blueprint.example.invalid/roadmap/results/index.html#result-149');
    """)


def test_a_related_list_saved_in_history_reopens_with_exactly_its_statements(library: Path) -> None:
    # No control lists a statement's relations any more (its own dependency list does), but a history entry saved with
    # one still reopens it: a reload restores the entry.
    related = [f"results/result-{n:03d}" for n in range(1, 8)]
    saved = {"node": None, "requestedNode": "roadmap", "mode": "list", "relationIds": related}
    before = ("window.history.replaceState({autoformNavigation: {version: 1, url: location.href, key: 'saved', snapshot: "
              + json.dumps(saved) + "}}, '', location.href);")
    run_book(library, "roadmap/", _LIST + "const related = " + json.dumps(related) + ";\n" + r"""
      assert.equal(listed(), true); assert.deepEqual(rows(), related); assert.equal(counted(), '7 statements');
      assert.equal($('.bp-dag-load-more').hidden, true);
      // The page saves the list it restored as the same related list.
      assert.deepEqual(window.history.state.autoformNavigation.snapshot.relationIds, related);
    """, before=before)


def test_back_rebuilds_the_saved_list_page_before_it_restores_scroll_and_focus(library: Path) -> None:
    run_book(library, "roadmap/", _LIST + r"""
      await browse('All statements'); click(control('Show 51 more')); await tick(0);
      const list = $('.bp-dag-inventory-scroll'), outline = $('.bp-dag-navigation'), reading = $('.bp-reading-body');
      for (const [pane, top] of [[list, 1800], [outline, 37], [reading, 71]]) { pane.scrollTop = top; emit(pane, 'scroll'); }
      await tick(100);
      row('results/result-130').focus(); click(row('results/result-130')); await tick(200);
      assert.equal(location.href, 'https://blueprint.example.invalid/roadmap/results/index.html#result-130');
      // jsdom scrolls nothing itself: the panes move as the other page would move them.
      list.scrollTop = outline.scrollTop = 0;
      const focused = [];
      document.addEventListener('focusin', event => focused.push({id: event.target.dataset.inventoryNodeId, rows: rows().length, listed: listed(), top: list.scrollTop}));
      await back();
      assert.equal(location.href, 'https://blueprint.example.invalid/roadmap/index.html');
      // The saved second page is built first, then scrolled, and only then is its row focused.
      assert.deepEqual(focused, [{id: 'results/result-130', rows: 151, listed: true, top: 1800}]);
      assert.equal(document.activeElement, row('results/result-130')); assert.equal(counted(), '151 articles');
      assert.equal(outline.scrollTop, 37); assert.equal(reading.scrollTop, 71);
      // Restored, the page saves the reader's next scroll again, and a new filter starts the list over at its first page.
      list.scrollTop = 2400; emit(list, 'scroll'); await tick(100);
      assert.equal(window.history.state.autoformNavigation.snapshot.inventoryScroll, 2400);
      await keep('fully_proved'); await keep();
      assert.equal(rows().length, 100); assert.equal(counted(), '100 of 151'); assert.equal(list.scrollTop, 0);
    """)


def test_a_query_and_a_status_filter_chosen_on_the_map_apply_when_the_list_opens(library: Path) -> None:
    run_book(library, "roadmap/", _LIST + r"""
      await search('Result 130'); assert.deepEqual(rows(), []);
      await browse('All statements'); assert.deepEqual(rows(), ['results/result-130']); assert.equal(counted(), '1 statement');
      await search(''); await keep('fully_proved');
      assert.equal(rows().length, 75); assert(rows().every(id => Number(id.slice(-3)) % 2 === 1)); assert.equal(counted(), '75 statements');
      // The filter hides rows, never drops them from the index.
      await keep(); assert.equal(counted(), '100 of 151');
    """)


def test_show_filtered_out_counts_what_the_status_filter_hides_and_shows_it_again(library: Path) -> None:
    run_book(library, "roadmap/", _LIST + r"""
      await keep('fully_proved'); await browse('All statements');
      // The 75 proved results show; the 75 others and the module article do not.
      assert.equal(counted(), '75 statements');
      assert.equal(filteredOut().hidden, false); assert.equal(filteredOut().textContent, 'Show 76 filtered out');
      // A search counts only its own matches: result-140 to result-149, five of them unproved.
      await search('result-14');
      assert.deepEqual(rows(), [141, 143, 145, 147, 149].map(n => 'results/result-' + n));
      assert.equal(filteredOut().textContent, 'Show 5 filtered out');
      await search(''); await browse('Source texts');
      assert.deepEqual(rows(), []); assert.equal(filteredOut().textContent, 'Show 2 filtered out');
      click(filteredOut()); await tick(0);
      assert.deepEqual(rows(), ['source-0', 'source-1']); assert.equal(filteredOut().hidden, true);
      assert.equal(document.activeElement, $('.bp-dag-filter-summary')); assert.equal(document.activeElement.textContent, 'Filter: all statuses');
    """)


def test_typing_on_ranks_every_keystroke_as_ranking_every_entry_does(words: Path) -> None:
    # The index fails once and is slow the second time, so the search starts on the page's own entries.
    before = "net.fail('dependencies/index.json'); net.hold('dependencies/index.json');"
    run_book(words, "roadmap/", r"""
      const box = $('.bp-dag-search');
      // Every match, read through the results' "Show more" button.
      async function results() {
        for (let more; (more = $('.bp-dag-search-results .bp-dag-load-more'));) { click(more); await tick(0); }
        return $$('.bp-dag-search-results .bp-dag-result').map(result => result.dataset.resultId);
      }
      async function type(value) { box.value = value; emit(box, 'input'); await tick(0); return results(); }
      // A keystroke that extends the query ranks only the last matches; '#' matches nothing, so the same query typed
      // after it ranks every entry. Both must show the same results in the same order.
      async function keystroke(value) {
        const typed = await type(value); await type('#'); const ranked = await type(value);
        assert.deepEqual(typed, ranked, value); return ranked;
      }
      emit(box, 'focus');
      assert.deepEqual(await keystroke('zeckendorf r'), []);
      // The index starts the ranking over, though the next query extends the last one.
      net.release('dependencies/index.json'); await tick(50);
      assert.deepEqual(await results(), ['zeckendorf', 'more/reps', 'zeta']);
      assert.deepEqual(await keystroke('zeckendorf re'), ['zeckendorf', 'more/reps', 'zeta']);
      // The ranks tie, and a narrowed query reorders them, so the last matches must be ranked again.
      assert.deepEqual(await keystroke('p'), ['pri', 'prime', 'root', 'capri', 'zeckendorf', 'more/reps', 'roadmap', 'zeta']);
      await keystroke('pr');
      assert.deepEqual(await keystroke('pri'), ['pri', 'prime', 'root', 'capri']);
      // A second word: the one-word query's matches still hold every match.
      assert.deepEqual(await keystroke('pri n'), ['capri', 'pri', 'prime', 'root']);
      assert.deepEqual(await keystroke('pri nu'), ['capri', 'pri', 'prime']);
      // Backspace and paste rank every entry again.
      await keystroke('pri n'); assert.deepEqual(await keystroke('zeckendorf'), ['zeckendorf', 'more/reps', 'zeta']);
      // Lowercasing is toLocaleLowerCase's: a final capital sigma becomes ς, so ΟΔΟΣΑ does not extend ΟΔΟΣ, and İ holds an i.
      for (const value of ['Ο', 'ΟΔ', 'ΟΔΟ']) await keystroke(value);
      assert.deepEqual(await keystroke('ΟΔΟΣ'), ['odos']); assert.deepEqual(await keystroke('ΟΔΟΣΑ'), ['odosa']);
      assert.deepEqual(await keystroke('σ'), ['odosa']); assert.equal((await keystroke('i'))[0], 'kent');
      // The last matches are every match, not the twelve shown, so a query with more matches narrows exactly too.
      assert.equal((await keystroke('result')).length, 30); assert.equal((await keystroke('result 1')).length, 12);
    """, before=before)
