"""Keyboard focus in the book's controls: the status and Browse menus, Back to map and the focus it hands the map once
it paints, Contents, the "/" and Cmd-K shortcuts and the phone's reader. Each test opens a page of a small book render
published (:mod:`tests.explorer_fixture`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.scaffold import scaffold_project
from tests.explorer_fixture import ARTICLE, build_site, run_book


@pytest.fixture(scope="module")
def book(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A chapter of statements in two states, with a source text, so Browse is a menu and the filter can hide one."""
    project = tmp_path_factory.mktemp("keyboard-focus") / "book"
    scaffold_project(project, title="Keyboard focus book")
    pages = {
        "README.md": "# Keyboard focus book\n\nA small book.\n\n## Chapters\n\n- [Chapter](chapter/README.md)\n",
        "chapter/README.md": "# Chapter\n\nThe chapter's own prose.\n",
        "chapter/a.md": ARTICLE.format(kind="theorem", lean="Book.a", title="Statement A", body="A holds.",
                                       uses="No prerequisites."),
        "chapter/b.md": ARTICLE.format(kind="theorem", lean="Book.b", title="Statement B", body="B holds.",
                                       uses="- [Statement A](a.md)"),
        "chapter/c.md": "---\ndeclaration: lemma\n---\n\n# Statement C\n\nC is only stated.\n",
        "chapter/library/README.md": "---\nnavigation: source\n---\n\n# Library\n\n- [Module](module.md)\n",
        "chapter/library/module.md": "---\ncatalog: module\n---\n\n# Module\n\nAn inventory of one module.\n",
    }
    for name, text in pages.items():
        (project / "blueprint/roadmap" / name).parent.mkdir(parents=True, exist_ok=True)
        (project / "blueprint/roadmap" / name).write_text(text, encoding="utf-8")
    return build_site(project)


#: Ahead of the explorer: every focus call is logged with whether it asked not to scroll, and the page opens on the
#: history entry ``args.saved``, if any.
_BEFORE = r"""
var focusCalls = [];
for (const Kind of [window.HTMLElement, window.SVGElement]) {
  const focus = Kind.prototype.focus;
  Kind.prototype.focus = function (options) { focusCalls.push([this, Boolean(options && options.preventScroll)]); return focus.call(this, options); };
}
if (args.saved) window.history.replaceState(args.saved, '', location.href);
"""

_HELPERS = r"""
const quiet = node => { const calls = focusCalls.filter(call => call[0] === node); return calls.length > 0 && calls.at(-1)[1]; };
const control = text => $$('.bp-dag-toolbar button').find(node => node.textContent === text);
const backToMap = () => control('Back to map'), search = () => $('.bp-dag-search'), summary = () => $('.bp-dag-filter-summary');
const mapStop = () => $('.bp-whole-map svg[tabindex="0"]'), handle = () => $('.bp-dag-sheet-handle');
const key = (node, init) => emit(node, 'keydown', init);
// The statements of the chapter, from Browse.
function list() { click($('.bp-dag-browse > button')); click($('.bp-dag-browse-popover button')); assert.equal(backToMap().hidden, false); }
// Back to map, by keyboard: the button has the focus when it is pressed.
function pressBack() { backToMap().focus(); click(backToMap()); }
function outside(tag = 'button') {
  const node = element(window, tag, tag === 'a' ? {href: 'https://outside.invalid/'} : {}); node.textContent = 'Elsewhere';
  document.body.append(node); return node;
}
// The map, drawn: jsdom gives its stage no size and loads no stylesheet, so the test gives it both.
async function drawMap() {
  mapStop().getBoundingClientRect = () => ({left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, x: 0, y: 0});
  Array.from(document.head.querySelectorAll('link[rel="stylesheet"]')).find(link => /project-map\.css$/.test(link.href))
    .dispatchEvent(new window.Event('load'));
  await waitFor(() => $('.bp-whole-map .pm-result'), 'the map never drew');
}
"""


def _book(site: Path, page: str, body: str, *, hash: str = "", media: dict[str, bool] | None = None,
          map: str | None = None, before: str = "", saved: dict | None = None) -> str:
    """*body* on *page*; *before* runs after :data:`_BEFORE`, and *saved* is the history entry the page opens on."""
    return run_book(site, page, _HELPERS + body, before=_BEFORE + before, args={"saved": saved}, hash=hash, media=media,
                    map=map)


def test_the_status_menu_closes_only_when_focus_leaves_it_and_never_takes_the_focus(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      const [first, second] = $$('.bp-dag-filter-popover input'), away = outside(), popover = $('.bp-dag-filter-popover');
      click(summary()); first.focus(); second.focus();
      assert.equal(popover.hidden, false, 'Focus moved within the menu');
      away.focus();
      assert.equal(popover.hidden, true); assert.equal(summary().getAttribute('aria-expanded'), 'false');
      assert.equal(document.activeElement, away);
      click(summary()); first.focus(); first.blur();
      assert.equal(popover.hidden, true); assert.equal(document.activeElement, document.body);
      // A press elsewhere closes it and leaves the focus to what the press itself focuses.
      click(summary()); first.focus(); emit(away, 'pointerdown');
      assert.equal(popover.hidden, true); assert.equal(document.activeElement, first);
      assert(!focusCalls.some(call => call[0] === summary()));
    """)


def test_escape_closes_the_status_menu_and_returns_the_focus_once_without_scrolling(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      click(summary()); const checkbox = $('.bp-dag-filter-popover input'); checkbox.focus();
      assert(key(checkbox, {key: 'Escape'}).defaultPrevented); assert.equal($('.bp-dag-filter-popover').hidden, true);
      assert.equal(document.activeElement, summary()); assert(quiet(summary()));
      const calls = focusCalls.length;
      assert.equal(key(summary(), {key: 'Escape'}).defaultPrevented, false, 'A closed menu leaves Escape alone');
      assert.equal(focusCalls.length, calls);
    """)


@pytest.mark.parametrize(("hash", "stop"), [("", "svg"), ("#b", "card")], ids=["chapter-map", "selected-statement-card"])
def test_back_to_map_hands_the_focus_to_the_maps_own_tab_stop_once_it_paints(book: Path, hash: str, stop: str) -> None:
    # The map's tab stop is the card it has selected, else the map itself.
    _book(book, "roadmap/chapter/index.html", "const stop=" + json.dumps(stop) + r""";
      if (stop === 'card') await drawMap();
      const target = stop === 'card' ? $('.bp-whole-map .pm-result.pm-selected[tabindex="0"]') : mapStop();
      assert(target); list(); const entries = window.history.length;
      assert.equal(window.history.state.autoformNavigation.snapshot.mode, 'list', 'The entry records the list');
      pressBack();
      assert.equal(backToMap().hidden, true); assert.equal($('.bp-dag-stage').hidden, false);
      assert.equal($('.bp-dag-inventory').hidden, true);
      assert.equal(window.history.length, entries); assert.equal(window.history.state.autoformNavigation.snapshot.mode, 'graph');
      await tick(16); assert.equal(document.activeElement, backToMap(), 'The map takes the focus once it has painted');
      await tick(16); assert.equal(document.activeElement, target); assert(quiet(target));
    """, hash=hash, map="load")


def test_only_the_latest_back_to_map_takes_the_focus_when_its_own_frames_end_and_saves_it(book: Path) -> None:
    saved = json.loads(_book(book, "roadmap/chapter/index.html", r"""
      list(); pressBack(); await tick(16);
      list(); pressBack(); await tick(16);
      // The first press's second frame comes first, and it no longer acts.
      assert.equal(document.activeElement, backToMap());
      await tick(16); assert.equal(document.activeElement, mapStop());
      await tick(100); return window.history.state;
    """, map="load"))
    # The entry it saves gives the map the focus back when the page opens on it again, as a reload or Back does.
    _book(book, "roadmap/chapter/index.html", r"""
      await tick(32); assert.equal(document.activeElement, mapStop()); assert(quiet(mapStop()));
    """, map="load", saved=saved)


def test_a_pending_map_focus_gives_way_to_a_move_of_the_reader_but_not_to_the_explorer_hiding_its_owner(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      const away = outside('a'), elsewhere = outside(), browse = $('.bp-dag-browse > button');
      const splitter = $('.bp-dag-splitter[aria-label="Resize the reading pane"]');
      // A visible owner keeps its boxes; jsdom draws none.
      browse.getClientRects = () => [{}];
      const rows = [
        ['the hidden button lets the focus fall to the page', backToMap, () => backToMap().blur(), mapStop],
        // The reader's splitter has no room beside a narrow reader, and the panes hand its focus to the reader's handle.
        ['the panes hand the owner\'s focus on', () => splitter, () => window.dispatchEvent(new window.Event('resize')), mapStop],
        ['the panes hand on another control\'s focus', backToMap, () => { splitter.focus(); window.dispatchEvent(new window.Event('resize')); }, handle],
        ['the reader moves on after a hand-off', () => splitter, () => { window.dispatchEvent(new window.Event('resize')); search().focus(); }, search],
        ['the reader types a search', backToMap, () => { search().focus(); search().value = 'Statement'; }, search],
        ['the reader follows a link elsewhere', backToMap, () => away.focus(), () => away],
        ['a visible owner lets the focus fall to the page', () => browse, () => browse.blur(), () => document.body],
        ['an owner outside the explorer lets the focus fall to the page', () => elsewhere, () => { elsewhere.hidden = true; elsewhere.blur(); }, () => document.body],
      ];
      for (const [name, owner, move, expected] of rows) {
        list(); owner().focus(); click(backToMap()); move(); await tick(48);
        assert.equal(document.activeElement, expected(), name);
        document.activeElement.blur(); elsewhere.hidden = false;
      }
    """, map="load")


def test_a_pending_map_focus_survives_the_explorer_replacing_its_owner(book: Path) -> None:
    # The index arrives between Back to map and its frames and redraws the status menu, dropping the checkbox that held
    # the focus. The focus falls to the page by the explorer's doing, not the reader's, so the map still takes it.
    _book(book, "roadmap/chapter/index.html", r"""
      // Without the index there is no source text, and Browse shows the statements itself.
      click($('.bp-dag-browse > button')); assert.equal(backToMap().hidden, false);
      click(summary()); const checkbox = $('.bp-dag-filter-popover input'); checkbox.focus();
      // A press on Back to map that leaves the focus on the checkbox, as WebKit's press on a button does.
      emit(backToMap(), 'pointerdown'); click(backToMap());
      net.release('dependencies/index.json'); await tick(0);
      assert.equal(checkbox.isConnected, false); assert.equal(document.activeElement, document.body);
      await tick(48); assert.equal(document.activeElement, mapStop()); assert(quiet(mapStop()));
    """, map="load", before="net.fail('dependencies/index.json'); net.hold('dependencies/index.json');")


@pytest.mark.parametrize("change", ["list", "navigation-started", "page-changed"])
def test_a_map_focus_asked_for_on_a_view_the_reader_left_takes_nothing(book: Path, change: str) -> None:
    _book(book, "roadmap/chapter/index.html", "const change=" + json.dumps(change) + r""";
      list(); pressBack();
      if (change === 'list') list();
      else {
        // The trail's link to the roadmap; a held page has not arrived when the map's frames come.
        if (change === 'navigation-started') net.hold('roadmap/index.html');
        assert(click($('.bp-dag-breadcrumb a[href$="roadmap/index.html"]'))); await flush();
      }
      await tick(48);
      assert.notEqual(document.activeElement, mapStop()); assert.notEqual(document.activeElement, summary());
      if (change === 'list') assert.equal(document.activeElement, backToMap());
    """, map="load")


@pytest.mark.parametrize(("phone", "map_loads", "expected"), [
    (True, True, "button"), (True, False, "button"), (False, True, "map"),
], ids=["phone-map", "phone-no-map-no-filter-fallback", "desktop-with-a-full-sheet-left-over"])
def test_a_full_phone_reader_covering_the_map_keeps_the_pending_map_focus_and_its_fallback(
    book: Path, phone: bool, map_loads: bool, expected: str,
) -> None:
    _book(book, "roadmap/chapter/index.html", "const [phone, expected] = " + json.dumps([phone, expected]) + r""";
      const layout = '(max-width: 720px)';
      // A phone's reader handle cycles peek, half and full.
      if (!phone) { click(handle()); click(handle()); assert(host().classList.contains('bp-dag-sheet-full')); media[layout] = false; }
      list(); pressBack();
      if (phone) { click(handle()); click(handle()); assert(host().classList.contains('bp-dag-sheet-full')); }
      await tick(48);
      assert.equal(document.activeElement, expected === 'map' ? mapStop() : backToMap());
    """, media={"(max-width: 720px)": True}, map="load" if map_loads else None)


@pytest.mark.parametrize("map_state", ["missing", "unstyled"])
def test_a_missing_or_still_unstyled_map_leaves_the_focus_on_the_filter(book: Path, map_state: str) -> None:
    _book(book, "roadmap/chapter/index.html", "const unstyled=" + json.dumps(map_state == "unstyled") + r""";
      // Until its stylesheet loads the map is visibility: hidden, and its tab stop refuses the focus jsdom would give it.
      if (unstyled) mapStop().focus = function () {};
      list(); pressBack(); await tick(48);
      assert.equal(document.activeElement, summary()); assert(quiet(summary()));
    """, map="load" if map_state == "unstyled" else None)


def test_showing_the_filtered_out_rows_focuses_the_filter_and_keeps_every_status(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      list(); const boxes = $$('.bp-dag-filter-popover input'), stated = boxes.find(box => box.value === 'can_state');
      stated.checked = false; emit(stated, 'change');
      const reveal = $('.bp-dag-hidden-button'), entries = window.history.length;
      assert.equal(reveal.hidden, false); assert.equal(reveal.textContent, 'Show 1 filtered out');
      reveal.focus(); click(reveal);
      assert.equal(reveal.hidden, true); assert(boxes.every(box => box.checked));
      assert.equal(document.activeElement, summary()); assert(quiet(summary()));
      assert.equal(window.history.length, entries);
      assert.deepEqual(window.history.state.autoformNavigation.snapshot.statuses.sort(), boxes.map(box => box.value).sort());
    """)


@pytest.mark.parametrize(("page", "hash", "expected"), [
    ("roadmap/chapter/index.html", "#b", "chapter/b"), ("roadmap/index.html", "", "chapter"),
], ids=["current-page-link", "first-link-fallback"])
def test_contents_focuses_the_current_pages_link_or_else_its_first(book: Path, page: str, hash: str, expected: str) -> None:
    _book(book, page, "const expected=" + json.dumps(expected) + r""";
      const toggle = $('.bp-dag-outline-toggle'); toggle.focus(); click(toggle);
      assert(host().classList.contains('bp-dag-outline-open')); assert.equal(toggle.getAttribute('aria-expanded'), 'true');
      assert.equal(document.activeElement.dataset.navigationNodeId, expected);
      toggle.focus(); click(toggle);
      assert(!host().classList.contains('bp-dag-outline-open')); assert.equal(toggle.getAttribute('aria-expanded'), 'false');
      assert.equal(document.activeElement, toggle);
    """, hash=hash)


def test_slash_searches_from_within_the_explorer_and_command_k_from_anywhere(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      const away = outside(), browse = $('.bp-dag-browse > button'), checkbox = $('.bp-dag-filter-popover input');
      search().value = 'abc';
      away.focus(); assert.equal(key(away, {key: '/'}).defaultPrevented, false); assert.equal(document.activeElement, away);
      browse.focus(); assert(key(browse, {key: '/'}).defaultPrevented);
      assert.equal(document.activeElement, search()); assert.deepEqual([search().selectionStart, search().selectionEnd], [0, 3]);
      for (const modifier of [{ctrlKey: true}, {metaKey: true}, {altKey: true}]) {
        browse.focus(); assert.equal(key(browse, {key: '/', ...modifier}).defaultPrevented, false); assert.equal(document.activeElement, browse);
      }
      // A key another handler already took stays its own.
      document.addEventListener('keydown', event => event.preventDefault(), {capture: true, once: true});
      browse.focus(); key(browse, {key: '/'}); assert.equal(document.activeElement, browse);
      for (const modifier of [{ctrlKey: true}, {metaKey: true}]) {
        away.focus(); assert(key(away, {key: 'k', ...modifier}).defaultPrevented); assert.equal(document.activeElement, search());
      }
      // A field takes its own keys; "/" in search is a character of the query.
      checkbox.focus(); assert.equal(key(checkbox, {key: '/'}).defaultPrevented, false); assert.equal(document.activeElement, checkbox);
      search().focus(); assert.equal(key(search(), {key: '/'}).defaultPrevented, false);
    """)


def test_a_full_phone_reader_shortcut_reveals_search_at_half_before_focusing_it(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      const main = $('.bp-dag-main'), source = $('.bp-reading-body .bp-reading-source'), href = location.href, focusAt = [];
      const focus = search().focus; search().focus = function (options) { focusAt.push(host().classList.contains('bp-dag-sheet-full')); return focus.call(this, options); };
      search().value = 'abc';
      for (const shortcut of [{key: '/'}, {key: 'k', ctrlKey: true}, {key: 'k', metaKey: true}]) {
        click(handle()); assert(host().classList.contains('bp-dag-sheet-full')); assert.equal(main.getAttribute('inert'), '');
        const link = $('.bp-reading-body .bp-reading-source a'); link.focus();
        const entries = window.history.length, state = JSON.stringify(window.history.state);
        assert(key(link, shortcut).defaultPrevented);
        assert(host().classList.contains('bp-dag-sheet-open')); assert(!host().classList.contains('bp-dag-sheet-full'));
        assert.equal(main.getAttribute('inert'), null); assert.equal(focusAt.at(-1), false, 'Search shows before it takes the focus');
        assert.equal(document.activeElement, search()); assert.deepEqual([search().selectionStart, search().selectionEnd], [0, 3]);
        assert.equal(location.href, href); assert.equal(window.history.length, entries); assert.equal(JSON.stringify(window.history.state), state);
        assert.equal($('.bp-reading-body .bp-reading-source'), source);
      }
    """, hash="#b", media={"(max-width: 720px)": True})


def test_ignored_shortcuts_leave_a_full_phone_reader_and_its_focus_alone(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      click(handle()); assert(host().classList.contains('bp-dag-sheet-full'));
      const main = $('.bp-dag-main'), link = $('.bp-reading-body .bp-reading-source a'), entries = window.history.length;
      const unchanged = (node, name) => {
        assert.equal(document.activeElement, node, name); assert(host().classList.contains('bp-dag-sheet-full'), name);
        assert.equal(main.getAttribute('inert'), '', name); assert.equal(window.history.length, entries, name);
      };
      for (const shortcut of [{key: '/', ctrlKey: true}, {key: '/', metaKey: true}, {key: '/', altKey: true}, {key: 'k'}]) {
        link.focus(); assert.equal(key(link, shortcut).defaultPrevented, false); unchanged(link, JSON.stringify(shortcut));
      }
      document.addEventListener('keydown', event => event.preventDefault(), {capture: true, once: true});
      link.focus(); key(link, {key: '/'}); unchanged(link, 'taken by another handler');
      // Fields take their own keys: the status filter's checkbox, and a page's own text box or editable block.
      const editable = outside('div'); Object.defineProperty(editable, 'isContentEditable', {value: true}); editable.tabIndex = 0;
      for (const field of [$('.bp-dag-filter-popover input'), outside('textarea'), outside('select'), editable]) {
        for (const shortcut of [{key: '/'}, {key: 'k', ctrlKey: true}, {key: 'k', metaKey: true}]) {
          field.focus(); assert.equal(key(field, shortcut).defaultPrevented, false); unchanged(field, field.tagName);
        }
      }
    """, hash="#b", media={"(max-width: 720px)": True})


def test_escape_in_the_reader_clears_the_selection_with_one_history_entry_and_none_without_one(book: Path) -> None:
    _book(book, "roadmap/chapter/index.html", r"""
      const link = $('.bp-reading-body .bp-reading-source a'), entries = window.history.length, href = location.href;
      assert(host().classList.contains('bp-dag-has-selection'));
      link.focus(); assert(key(link, {key: 'Escape'}).defaultPrevented);
      assert(!host().classList.contains('bp-dag-has-selection'));
      assert(host().classList.contains('bp-dag-sheet-open'), 'The desktop reader stays open beside the map');
      assert.equal(window.history.length, entries + 1); assert.equal(location.href, href);
      assert.equal(document.activeElement, link);
      assert.equal(key(link, {key: 'Escape'}).defaultPrevented, false); assert.equal(window.history.length, entries + 1);
    """, hash="#b")
