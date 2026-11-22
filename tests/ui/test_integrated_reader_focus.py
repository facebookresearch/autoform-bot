"""Where the book puts the reading and the focus as it opens a page: a statement's anchor, a saved history entry, a route
that a newer one overtook, the menus a page closes, and the reader a phone or an overview folds. Each test opens a page
of a small book render published (:mod:`tests.explorer_fixture`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.scaffold import scaffold_project
from tests.explorer_fixture import ARTICLE, build_site, run_book


@pytest.fixture(scope="module")
def book(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Home with child indexes, a chapter of three statements and a source text, an overview topic and an appendix."""
    project = tmp_path_factory.mktemp("reader-focus") / "book"
    scaffold_project(project, title="Reader focus book")
    pages = {
        "README.md": (
            "# Reader focus book\n\nA small book; [its other statements](chapter/README.md#other-statements) close the chapter.\n\n"
            "## Chapters\n\n- [Chapter](chapter/README.md)\n- [Topic](topic/README.md)\n\n"
            "## Notes\n\nThe chapters build on each other.\n\n### More chapters\n\n- [Appendix](appendix/README.md)\n\n"
            "## Elsewhere\n\n- [Autoform](https://github.com/facebookresearch/autoform-bot)\n"
        ),
        "chapter/README.md": "# Chapter\n\nThe chapter's own prose.\n",
        "chapter/a.md": ARTICLE.format(kind="theorem", lean="Book.a", title="Statement A", body=r"A holds: \(x + 1\).",
                                       uses="No prerequisites."),
        "chapter/b.md": ARTICLE.format(kind="theorem", lean="Book.b", title="Statement B", body="B holds.",
                                       uses="- [Statement A](a.md)"),
        "chapter/library/README.md": "---\nnavigation: source\n---\n\n# Library\n\n- [Module](module.md)\n",
        "chapter/library/module.md": "---\ncatalog: module\n---\n\n# Module\n\nAn inventory of one module.\n",
        "topic/README.md": "# Topic\n\n## Topics\n\n- [Sub one](one/README.md)\n- [Sub two](two/README.md)\n",
        "appendix/README.md": "# Appendix\n\nLast words.\n",
        "appendix/last.md": ARTICLE.format(kind="lemma", lean="Book.last", title="Last result", body="It holds.",
                                           uses="No prerequisites."),
    }
    for name in ("one", "two"):
        pages[f"topic/{name}/README.md"] = f"# Sub {name}\n\nProse of {name}.\n"
        pages[f"topic/{name}/result.md"] = ARTICLE.format(kind="lemma", lean=f"Book.{name}", title=f"Result {name}",
                                                          body="It holds.", uses="No prerequisites.")
    for name, text in pages.items():
        (project / "blueprint/roadmap" / name).parent.mkdir(parents=True, exist_ok=True)
        (project / "blueprint/roadmap" / name).write_text(text, encoding="utf-8")
    return build_site(project)


#: Ahead of the explorer: a typeset of the reading, once holdMath is set, waits until the test resolves it
#: (typesets[n].resolve()), and every focus call is logged with whether it asked not to scroll.
_BEFORE = r"""
var typesets = [], focusCalls = [], holdMath = Boolean(args.hold);
window.AutoformMath = {refresh(root) {
  if (!holdMath || !root.classList.contains('bp-reading-body')) return window.Promise.resolve(true);
  const pass = deferred(); typesets.push(pass); return pass.promise;
}};
for (const Kind of [window.HTMLElement, window.SVGElement]) {
  const focus = Kind.prototype.focus;
  Kind.prototype.focus = function (options) { focusCalls.push([this, Boolean(options && options.preventScroll)]); return focus.call(this, options); };
}
if (args.saved) window.history.replaceState({autoformNavigation: {version: 1, url: location.href, key: 'saved', snapshot: args.saved}}, '', location.href);
"""

_HELPERS = r"""
const reading = () => $('.bp-reading-body'), card = id => $(`.bp-reading-body [data-autoform-node-id="${id}"]`);
// Whether the last focus call on node asked not to scroll, as a focus after a computed scroll must.
const quiet = node => { const calls = focusCalls.filter(call => call[0] === node); return calls.length > 0 && calls.at(-1)[1]; };
// jsdom lays nothing out: the reading pane's top is at 100px, a statement card 400px down the reading, its heading 60px lower.
function lay(statement) {
  const at = top => ({top, bottom: top + 40, left: 0, right: 640, width: 640, height: 40, x: 0, y: top});
  reading().getBoundingClientRect = () => at(100);
  statement.getBoundingClientRect = () => at(400 - reading().scrollTop);
  statement.querySelector('.bp-thmheading').getBoundingClientRect = () => at(460 - reading().scrollTop);
}
async function mounted() { await waitFor(() => host() && reading() && reading().querySelector('.bp-reading-source'), 'the book never mounted'); }
// Microtasks only: what a route does before its next frame.
async function settle(predicate) { for (let n = 0; n < 50 && !predicate(); n++) await flush(); assert(predicate(), 'the route never committed'); }
const outline = id => $(`.bp-dag-navigation-link[data-navigation-node-id="${id}"]`);
async function follow(id) { assert(click(outline(id))); await waitFor(() => !host().hasAttribute('aria-busy')); await tick(200); }
// The reader's place: a link in the reading focused and the panes scrolled, as a scroll event reports it.
async function readAt(scroll) {
  reading().querySelector('.bp-reading-source a').focus(); reading().scrollTop = scroll; emit(reading(), 'scroll');
  $('.bp-dag-navigation').scrollTop = 44; $('.bp-dag-inventory-scroll').scrollTop = 9; await tick(100);
}
"""


def _book(site: Path, page: str, body: str, *, hold: bool = False, saved: dict | None = None, hash: str = "",
          media: dict[str, bool] | None = None, map: str | None = None) -> str:
    """*body* on *page*; with *hold* the first page's typeset waits too, and *saved* is the history entry it opens on."""
    return run_book(site, page, _HELPERS + body, before=_BEFORE, args={"hold": hold, "saved": saved}, hash=hash,
                    media=media, map=map, ready=not hold)


def test_a_statement_anchor_marks_and_opens_the_whole_card_then_focuses_its_heading_after_math(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      await mounted();
      const statement = card('chapter/b'), heading = statement.querySelector('.bp-thmheading');
      const relations = statement.querySelector('.bp-dependencies');
      lay(statement); assert.equal(typesets.length, 1); await tick(32);
      assert(!statement.classList.contains('bp-reading-current'), 'Frames pass, and the move waits for the formulas');
      assert.equal(relations.open, false); assert.equal(document.activeElement, document.body);
      typesets[0].resolve(true); await flush();
      assert.equal(document.activeElement, document.body, 'The formulas typeset, and the move still waits for a frame');
      await tick(16);
      // The anchor is on the card's heading; the whole card is the current reading, and its own relations open.
      assert(statement.classList.contains('bp-reading-current')); assert.equal(statement.getAttribute('aria-current'), 'true');
      assert.equal(relations.open, true);
      assert(!heading.classList.contains('bp-reading-current')); assert.equal(heading.getAttribute('aria-current'), null);
      assert(!card('chapter/a').classList.contains('bp-reading-current'));
      assert.equal(document.activeElement, heading, 'The selected card heading, not an earlier one'); assert.equal(heading.tabIndex, -1);
      assert(quiet(heading));
      assert.equal(reading().scrollTop, 276, 'The anchor scroll measures the card, not its lower heading');
      assert.equal(document.title, 'Statement B');
    """, hash="#b", hold=True)


def test_a_section_anchor_asked_for_from_the_reading_is_marked_and_focused(book: Path) -> None:
    _book(book, "roadmap/index.html", r"""
      const link = $('.bp-reading-body a[href$="#other-statements"]');
      assert(click(link)); await waitFor(() => location.hash === '#other-statements' && !host().hasAttribute('aria-busy'));
      await tick(200);
      // A section is no card: the heading itself is the current reading and takes the focus, not the page's title.
      const section = $('.bp-reading-body #other-statements');
      assert(section.classList.contains('bp-reading-current')); assert.equal(document.activeElement, section); assert(quiet(section));
    """)


def test_a_saved_reader_place_wins_over_the_statement_anchor_on_back(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      await readAt(211); await follow('topic'); assert.equal(document.title, 'Topic');
      holdMath = true; await back(); await waitFor(() => typesets.length === 1, 'Back never reopened the chapter');
      const statement = card('chapter/b'), heading = statement.querySelector('.bp-thmheading');
      lay(statement); typesets[0].resolve(true); await tick(16);
      // The statement is marked and its relations open, but the anchor neither scrolls nor takes the focus.
      assert(statement.classList.contains('bp-reading-current')); assert.equal(statement.querySelector('.bp-dependencies').open, true);
      assert.equal(reading().scrollTop, 0); assert.notEqual(document.activeElement, heading);
      await tick(16);
      assert.equal(reading().scrollTop, 211); assert.equal($('.bp-dag-navigation').scrollTop, 44);
      assert.equal($('.bp-dag-inventory-scroll').scrollTop, 9);
      assert.equal(document.activeElement, reading()); assert.equal(reading().tabIndex, -1); assert(quiet(reading()));
      assert(!focusCalls.some(call => call[0] === heading));
    """, hash="#b")


@pytest.mark.parametrize(("hash", "owner"), [
    # A legacy entry names its owner only as node: another statement of the page, at B's own address.
    ("#b", {"node": "chapter/a"}),
    # A section of the chapter, saved for a statement of another page.
    ("#other-statements", {"node": "topic/one/result", "requestedNode": "topic/one/result"}),
], ids=["another-statement-at-this-ones-address", "another-pages-statement-at-a-section"])
def test_a_foreign_snapshot_cannot_hide_or_refocus_the_new_reading(book: Path, hash: str, owner: dict[str, str]) -> None:
    saved = {**owner, "mode": "sources", "readingOpen": False, "focus": "search", "readingScroll": 211, "outlineScroll": 44}
    _book(book, "roadmap/chapter/index.html", "const statement=" + json.dumps(hash == "#b") + r""";
      await mounted(); if (statement) lay(card('chapter/b'));
      typesets[0].resolve(true); await tick(16); await tick(16);
      assert(host().classList.contains('bp-dag-sheet-open'), 'The closed reader is not this reading\'s');
      assert.equal($('.bp-dag-stage').hidden, false); assert.equal($('.bp-dag-inventory').hidden, true);
      assert.notEqual(document.activeElement, $('.bp-dag-search')); assert.equal($('.bp-dag-navigation').scrollTop, 0);
      assert.notEqual(reading().scrollTop, 211);
      if (statement) {
        assert(card('chapter/b').classList.contains('bp-reading-current'));
        assert.equal(document.activeElement, card('chapter/b').querySelector('.bp-thmheading')); assert.equal(reading().scrollTop, 276);
      } else assert($('.bp-reading-body #other-statements').classList.contains('bp-reading-current'));
    """, hash=hash, hold=True, saved=saved)


def test_a_section_of_the_same_page_keeps_the_snapshot_statement_and_reader_place(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      // A section link of B's page keeps B selected; the entry it adds is saved with the reader's place there.
      window.location.hash = 'other-statements'; await tick(0); await readAt(211);
      await follow('topic'); await back(); await tick(32);
      assert.equal(location.hash, '#other-statements');
      assert(host().classList.contains('bp-dag-has-selection')); assert.equal(document.activeElement, reading());
      assert.equal(reading().scrollTop, 211);
      assert.equal(window.history.state.autoformNavigation.snapshot.requestedNode, 'chapter/b');
    """, hash="#b")


@pytest.mark.parametrize("node", [None, "topic/one/result"], ids=["no-statement", "explicit-null-owner"])
def test_a_snapshot_owned_by_no_statement_restores_the_list_the_closed_reader_and_search(book: Path, node: str | None) -> None:
    # An explicit null owner is no statement, whatever the legacy node field says.
    saved = {"node": node, "requestedNode": None, "mode": "list", "readingOpen": False, "focus": "search",
             "readingScroll": 89, "outlineScroll": 44, "inventoryScroll": 9, "listLimit": 200}
    _book(book, "roadmap/chapter/index.html", r"""
      await tick(32);
      assert.equal($('.bp-dag-inventory').hidden, false); assert.equal($('.bp-dag-stage').hidden, true);
      assert(!host().classList.contains('bp-dag-sheet-open'));
      assert.equal(document.activeElement, $('.bp-dag-search')); assert(quiet($('.bp-dag-search')));
      assert.equal(reading().scrollTop, 89); assert.equal($('.bp-dag-navigation').scrollTop, 44);
      assert.equal($('.bp-dag-inventory-scroll').scrollTop, 9);
    """, saved=saved)


@pytest.mark.parametrize("intent", ["search", "map"])
def test_restoration_returns_the_focus_to_the_saved_search_or_map(book: Path, intent: str) -> None:
    _book(book, "roadmap/chapter/index.html", "const intent=" + json.dumps(intent) + r""";
      const control = () => intent === 'search' ? $('.bp-dag-search') : $('.bp-whole-map svg[tabindex="0"]');
      control().focus(); await follow('topic'); await back(); await tick(32);
      assert.equal(document.activeElement, control()); assert(quiet(control()));
      assert(!focusCalls.some(call => call[0] === reading()));
    """, map="load")


def test_a_section_link_followed_while_the_statement_typesets_cancels_the_move_to_it(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      await mounted(); const statement = card('chapter/b'); lay(statement);
      window.location.hash = 'other-statements'; await tick(0);
      typesets[0].resolve(true); await tick(16); await tick(16);
      assert.equal(reading().querySelector('.bp-reading-current'), null);
      assert.equal(statement.querySelector('.bp-dependencies').open, false);
      assert.equal(document.activeElement, document.body); assert.equal(reading().scrollTop, 0);
    """, hash="#b", hold=True)


def test_a_section_link_followed_while_the_page_restores_cancels_the_restoration(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      await readAt(211); await follow('topic');
      holdMath = true; await back(); await waitFor(() => typesets.length === 1, 'Back never reopened the chapter');
      typesets[0].resolve(true); await tick(16);
      // The restoration waits for its frame; a section link followed now is the reader's newer move.
      window.location.hash = 'other-statements'; await tick(0); await tick(16); await tick(16);
      assert.equal(reading().scrollTop, 0); assert.notEqual(document.activeElement, reading());
    """, hash="#b")


@pytest.mark.parametrize(("toggle", "popover", "control"), [
    (".bp-dag-filter-summary", ".bp-dag-filter-popover", "input"),
    (".bp-dag-browse > button", ".bp-dag-browse-popover", "button"),
], ids=["status-filter", "browse"])
def test_a_page_that_arrives_closes_the_open_menu_and_returns_focus_before_the_reading_takes_it(
    book: Path, toggle: str, popover: str, control: str,
) -> None:
    _book(book, "roadmap/index.html", "const [toggle, popover, control] = " + json.dumps([toggle, popover, control]) + r""";
      net.hold('roadmap/chapter/index.html'); assert(click(outline('chapter')));
      click($(toggle)); assert.equal($(popover).hidden, false); $(popover).querySelector(control).focus();
      net.release('roadmap/chapter/index.html'); await settle(() => document.title === 'Chapter');
      assert.equal($(popover).hidden, true); assert.equal($(toggle).getAttribute('aria-expanded'), 'false');
      assert.equal(document.activeElement, $(toggle)); assert(quiet($(toggle)));
      await tick(16);
      assert.equal(document.activeElement, $('.bp-reading-body h1'), 'The page asked for its reading');
    """)


# How the page opens: "" (a cold start), "outline:ID" (its Contents link) or "search:QUERY" (the first result).
_SHEETS = [
    ("desktop-overview-opens-folded", "roadmap/topic/index.html", False, "", None, True, False, "body"),
    ("desktop-overview-asked-map-first", "roadmap/chapter/index.html", False, "search:Topic", None, True, False, "map"),
    ("desktop-overview-asked-for-its-reading", "roadmap/chapter/index.html", False, "outline:topic", None, False, True, "h1"),
    ("desktop-overview-legacy-list-hash", "roadmap/topic/index.html#view=list", False, "", None, True, False, "body"),
    ("desktop-chapter-asked-map-first-keeps-its-reading", "roadmap/topic/index.html", False, "search:Chapter", None, False, True,
     "h1"),
    ("desktop-saved-open-reader-over-the-overview", "roadmap/topic/index.html", False, "", True, True, True, "search"),
    ("phone-chapter-opens-on-the-map", "roadmap/chapter/index.html", True, "", None, False, False, "body"),
    ("phone-roadmap-opens-on-the-map", "roadmap/index.html", True, "", None, False, False, "body"),
    ("phone-chapter-asked-for-its-reading", "roadmap/index.html", True, "outline:chapter", None, False, True, "h1"),
    ("phone-chapter-asked-map-first", "roadmap/index.html", True, "search:Chapter", None, False, False, "map"),
    ("phone-statement-opens-in-the-reader", "roadmap/chapter/index.html#b", True, "", None, False, True, "statement"),
    ("phone-section-opens-in-the-reader", "roadmap/chapter/index.html#other-statements", True, "", None, False, True, "body"),
    ("phone-source-text-opens-in-the-reader", "roadmap/chapter/library/index.html", True, "", None, False, True, "body"),
    ("phone-saved-open-reader-over-the-map", "roadmap/chapter/index.html", True, "", True, False, True, "search"),
    ("phone-saved-closed-reader-over-a-statement", "roadmap/chapter/index.html#b", True, "", False, False, False, "search"),
]


@pytest.mark.parametrize(("address", "phone", "action", "saved", "overview", "opened", "focus"),
                         [row[1:] for row in _SHEETS], ids=[row[0] for row in _SHEETS])
def test_the_reader_opens_or_folds_as_the_route_and_the_layout_ask(
    book: Path, address: str, phone: bool, action: str, saved: bool | None, overview: bool, opened: bool, focus: str,
) -> None:
    page, _, anchor = address.partition("#")
    node = {"roadmap/topic/index.html": "topic", "roadmap/chapter/index.html": "chapter"}.get(page)
    # A saved entry keeps the reader as it was, and its restoration places the focus.
    snapshot = None if saved is None else {"requestedNode": node, "node": None, "readingOpen": saved, "focus": "search"}
    if snapshot and anchor:
        snapshot["requestedNode"] = "chapter/b"
    _book(book, page, "const [address, action, overview, opened, focus] = " + json.dumps([address, action, overview, opened, focus]) + r""";
      const [kind, value] = action.split(':');
      // The decision is made as the route commits, while the reading's formulas still typeset.
      if (kind) holdMath = true; else await mounted();
      if (kind === 'outline') { outline(value).focus(); assert(click(outline(value))); }
      if (kind === 'search') {
        const search = $('.bp-dag-search'); search.focus(); search.value = value; emit(search, 'input'); await tick(0);
        const result = $(`.bp-dag-result[data-result-id="${value.toLowerCase()}"]`); result.focus(); click(result);
      }
      await waitFor(() => typesets.length === 1, 'the route never committed');
      assert.equal(host().classList.contains('bp-dag-overview-only'), overview, 'overview');
      assert.equal(host().classList.contains('bp-dag-sheet-open'), opened, 'reader');
      typesets[0].resolve(true); await tick(64);
      assert.equal(host().classList.contains('bp-dag-sheet-open'), opened, 'reader once the page settled');
      const expected = {body: document.body, h1: $('.bp-reading-body h1'), search: $('.bp-dag-search'),
        map: $('.bp-whole-map svg[tabindex="0"]'), statement: $('.bp-reading-body #b')}[focus];
      assert.equal(document.activeElement, expected, 'focus');
      if (address.endsWith('#view=list')) assert.equal($('.bp-dag-inventory').hidden, false);
    """, hash="#" + anchor if anchor else "", media={"(max-width: 720px)": phone}, map="load", saved=snapshot,
          hold=not action)


def test_the_book_hides_each_child_index_render_names_with_the_lists_under_it(book: Path) -> None:
    # Render names Chapters and the nested More chapters; Notes holds prose, and Elsewhere lists no child topic.
    _book(book, "roadmap/index.html", r"""
      const source = $('.bp-reading-body .bp-reading-source');
      assert.equal(source.getAttribute('data-reader-indexes'), 'chapters more-chapters');
      const hidden = $$('.bp-reader-index', source).map(node => node.id || node.tagName);
      assert.deepEqual(hidden, ['chapters', 'UL', 'more-chapters', 'UL']);
      assert.equal($$('#elsewhere ~ ul.bp-reader-index', source).length, 0);
    """)
