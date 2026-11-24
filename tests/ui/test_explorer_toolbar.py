"""The explorer's toolbar, on a built book's pages with the explorer it ships (:mod:`tests.explorer_fixture`): Browse, the
source texts it offers, the status filter and its swatches, and the reader's way back to its topic.

A test that needs an index shape the site cannot make serves its own index in place of the site's; the page opened is the
``here`` topic's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.scaffold import scaffold_project
from tests.explorer_fixture import ARTICLE, ORIGIN, build_site, run_book
from tests.facts_fixture import write_annotated

HERE = "roadmap/here/index.html"
INDEX = "dependencies/index.json"


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("book")
    scaffold_project(project, title="Library")
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text("# Library\n\nGoal.\n\n## Chapters\n\n- [Here](here/README.md)\n- [There](there/README.md)\n",
                                       encoding="utf-8")
    for topic in ("here", "there"):
        (roadmap / topic).mkdir()
        (roadmap / topic / "README.md").write_text(f"# {topic.title()}\n\nProse.\n", encoding="utf-8")
        for name in ("result", "peer"):
            write_annotated(roadmap / topic / f"{name}.md", ARTICLE.format(
                kind="theorem", lean="Demo.main", title=f"{name.title()} {topic}", body="Statement.", uses="None."))
    return build_site(project)


def row(node_id: str, parent: str | None, *, scope: str | None = None, **extra: object) -> dict[str, object]:
    """An index row: a topic when it is its own *scope* (the default), else a statement on its scope's page."""
    scope = scope or node_id
    route = "../roadmap/index.html" if scope == "roadmap" else f"../roadmap/{scope}/index.html"
    return {"id": node_id, "title": node_id, "kind": "scope" if scope == node_id else "node", "scope": scope, "parent": parent,
            "result": scope != node_id, "status": "planned",
            "route": route + ("" if scope == node_id else "#" + node_id.rsplit("/", 1)[-1]),
            **extra}


def index(*nodes: dict[str, object]) -> dict[str, object]:
    return {"schema": "autoform-dag-search/v1", "nodes": list(nodes), "relations": []}


#: The toolbar's controls, and the list a Browse choice shows.
READ = r"""
const browse = $('.bp-dag-browse > button'), popover = $('.bp-dag-browse-popover'), toMap = $$('.bp-dag-toolbar > button').find(control => control.textContent === 'Back to map');
const choice = label => $$('button', popover).find(control => control.textContent === label);
const rows = () => $$('.bp-dag-inventory-row').map(item => item.dataset.inventoryNodeId);
// Shows the list Browse offers under LABEL, from its menu or, without source texts, from its one button.
function list(label) { if (browse.textContent === 'Browse') { click(browse); click(choice(label)); } else click(browse); return rows(); }
"""
#: A topic with a source text under it, so Browse is a menu.
SOURCES = index(row("roadmap", None), row("here", "roadmap"), row("here/result", "here", scope="here"),
                row("track", "here", navigation="source", route="../roadmap/there/index.html"))


def book(site: Path, body: str, *, page: str = HERE, **options: object) -> object:
    out = run_book(site, page, READ + body, **options)
    return json.loads(out) if out else None


def test_browse_nests_its_choices_and_back_to_map_shows_only_off_the_map(site: Path) -> None:
    states = book(site, r"""
      const toolbar = $('[role="group"][aria-label="Dependency explorer controls"]');
      const state = () => [$$('[aria-pressed="true"]', toolbar).map(control => control.textContent), toMap.hidden,
        $('.bp-dag-stage').hidden, $('.bp-dag-inventory').hidden, $('.bp-dag-inventory h2').textContent];
      const closed = [popover.hidden, browse.getAttribute('aria-expanded'), browse.getAttribute('aria-controls') === popover.id,
        popover.getAttribute('aria-label'), $$('button', popover).map(control => control.textContent),
        $$(':scope > button', toolbar).map(control => control.textContent).filter(Boolean)];
      const result = [closed, state()];
      list('Statements in this section'); result.push(state());
      list('Source texts'); result.push(state());
      click(toMap); result.push(state());
      // The root's list is the whole library's.
      click($$('.bp-dag-breadcrumb a').find(link => link.href.endsWith('/roadmap/index.html')));
      await waitFor(() => location.pathname === '/roadmap/index.html' && !host().hasAttribute('aria-busy'));
      result.push(choice('All statements') && true);
      return result;
    """, files={INDEX: SOURCES})
    assert states == [
        # The choices live in the Browse menu, which starts closed; the toolbar has no Map, Results or Sources button.
        [True, "false", True, "Browse library contents", ["Statements in this section", "Source texts"],
         ["Contents", "Back to map"]],
        # On the map nothing reads as pressed; in a list, the choice that shows it does, and Back to map shows.
        [[], True, False, True, "Statements"],
        [["Statements in this section"], False, True, False, "Statements"],
        [["Source texts"], False, True, False, "Source texts"],
        [[], True, False, True, "Statements"],
        True,
    ]


def test_browse_opens_without_taking_focus_and_escape_returns_it(site: Path) -> None:
    assert book(site, r"""
      const focusing = [], focus = window.HTMLElement.prototype.focus;
      window.HTMLElement.prototype.focus = function (options) { focusing.push([this, options]); return focus.call(this, options); };
      browse.focus(); click(browse);
      const opened = [popover.hidden, browse.getAttribute('aria-expanded'), document.activeElement === browse];
      choice('Source texts').focus();
      const escaped = emit(document.activeElement, 'keydown', {key: 'Escape'}).defaultPrevented;
      const closed = [popover.hidden, browse.getAttribute('aria-expanded'), document.activeElement === browse,
        JSON.stringify(focusing.at(-1)[1])];
      // A closed menu leaves Escape to others.
      const count = focusing.length, again = emit(browse, 'keydown', {key: 'Escape'}).defaultPrevented;
      click(browse); const reopened = popover.hidden; click(browse);
      return [opened, escaped, closed, again, focusing.length === count, reopened, popover.hidden];
    """, files={INDEX: SOURCES}) == [[False, "true", True], True, [True, "false", True, '{"preventScroll":true}'], False, True,
                                     False, True]


def test_a_browse_choice_closes_the_menu_and_returns_focus_before_the_list_shows(site: Path) -> None:
    assert book(site, r"""
      const states = [];
      for (const label of ['Statements in this section', 'Source texts']) {
        click(browse); choice(label).focus(); click(choice(label));
        states.push([popover.hidden, document.activeElement === browse, $('.bp-dag-inventory h2').textContent, toMap.hidden]);
      }
      return states;
    """, files={INDEX: SOURCES}) == [[True, True, "Statements", False], [True, True, "Source texts", False]]


def test_closing_browse_from_outside_leaves_focus_where_the_reader_put_it(site: Path) -> None:
    assert book(site, r"""
      const outside = $('.bp-dag-search');
      click(browse); choice('Source texts').focus();
      // A press elsewhere closes the menu before the browser moves focus there.
      emit(outside, 'pointerdown');
      const pressed = [popover.hidden, browse.getAttribute('aria-expanded'), document.activeElement === choice('Source texts')];
      click(browse); choice('Statements in this section').focus();
      // Moving between its choices keeps the menu open; leaving it closes it, and focus stays where it went.
      choice('Source texts').focus(); const within = popover.hidden;
      outside.focus();
      return [pressed, within, popover.hidden, document.activeElement === outside];
    """, files={INDEX: SOURCES}) == [[True, "false", True], False, True, True]


def _deep_chain(depth: int = 1000) -> list[dict[str, object]]:
    chain = [row(f"S{number}", f"S{number - 1}" if number else "here", navigation="source") for number in range(depth)]
    statements = [row(f"L{number}", f"S{depth - 1}", scope="here") for number in range(16)]
    return [row("here", "roadmap"), *reversed(chain), *statements]


#: Ahead of the explorer: counts its reads of a Map, one per step up a parent chain, so ``reads`` measures its work.
_COUNT = r"""
var reads = 0;
const get = window.Map.prototype.get;
window.Map.prototype.get = function (key) { reads++; return get.call(this, key); };
"""


def _authored() -> list[dict[str, object]]:
    return [row("here", "roadmap"), row("here/F", "here", scope="here"), row("collection", "here", navigation="collection"),
            row("here/source-looking-path", "other", scope="here", navigation="source", result=False),
            row("pagination", "collection", navigation="transparent"), row("direct-source", "here", navigation="source"),
            row("other", "roadmap"), row("nested-source", "pagination", navigation="source"),
            row("unknown-source", "missing", scope="here", navigation="source", result=False),
            row("cycle-source", "loop", navigation="source"), row("loop", "cycle-source")]


def _unreached(parent: str) -> list[dict[str, object]]:
    return [row("here", "roadmap"), row("branch", parent), row("source", "branch", navigation="source"),
            row("here/result", "branch", scope="here"), row("loop", "branch")]


@pytest.mark.parametrize(("page", "rows", "sources", "statements"), [
    # Source texts are the source tracks authored below the topic, through collections and parts, not every source on its
    # page or under its path.
    pytest.param(HERE, _authored(), ["direct-source", "nested-source"], ["here/F"], id="authored-below-the-topic"),
    pytest.param("roadmap/index.html", _authored(), ["here/source-looking-path", "direct-source", "nested-source", "unknown-source",
                                                     "cycle-source"], ["here/F"], id="the-root-holds-every-source"),
    # A parent chain that never reaches the topic, broken or circular, claims nothing.
    pytest.param(HERE, _unreached("absent-parent"), None, [], id="missing-parent"),
    pytest.param(HERE, _unreached("loop"), None, [], id="cyclic-parent"),
    # Below the topic membership holds whatever lies above it: an upstream parent the index lacks, or no row for the topic.
    pytest.param(HERE, [row("source", "here", navigation="source"), row("here/theorem", "here", scope="here")], ["source"],
                 ["here/theorem"], id="topic-without-a-row"),
    pytest.param(HERE, [row("here", "unknown-upstream"), row("source", "here", navigation="source"),
                        row("here/theorem", "here", scope="here")], ["source"], ["here/theorem"], id="dangling-upstream"),
    # A thousand-deep track lists whole, deepest first as the index orders it, and its statements belong to the topic.
    pytest.param(HERE, _deep_chain(), "100 of 1,000", [f"L{number}" for number in range(16)], id="deep-track"),
])
def test_the_source_pool_is_the_source_tracks_authored_below_the_topic(site: Path, page: str, rows: list, sources: object,
                                                                         statements: list) -> None:
    listed = book(site, r"""
      // The work of reading the index and drawing the first page, Browse's menu with it.
      const work = reads;
      const offered = browse.textContent === 'Browse' ? list('Source texts') : null;
      const summary = $('.bp-dag-inventory-summary').textContent;
      click(toMap);
      return [offered && offered.length === 100 ? summary : offered, list(location.pathname === '/roadmap/index.html' ?
        'All statements' : 'Statements in this section'), offered && offered.slice(0, 2), work];
    """, page=page, before=_COUNT, files={INDEX: index(row("roadmap", None), *rows)})
    assert listed[:2] == [sources, statements]
    if sources == "100 of 1,000":
        assert listed[2] == ["S999", "S998"]
        # Each source walks up its track to the topic. Remembering where each walk led keeps the work linear in the
        # depth: four times the depth takes about four times the reads, where walking every track whole takes sixteen.
        quarter = book(site, "return reads;", before=_COUNT,
                       files={INDEX: index(row("roadmap", None), *_deep_chain(250))})
        assert listed[3] < 8 * quarter, f"{listed[3]} reads for 1,000 sources, {quarter} for 250"


def test_source_texts_are_authored_content_not_a_search_or_filter_result(site: Path) -> None:
    assert book(site, r"""
      const search = $('.bp-dag-search'); search.value = 'absent query'; emit(search, 'input');
      $$('.bp-dag-filter-popover input').forEach(input => { input.checked = false; emit(input, 'change'); });
      return [browse.textContent, $('.bp-dag-filter-summary').textContent];
    """, files={INDEX: SOURCES}) == ["Browse", "Filter: 0 of 1 statuses"]


def test_without_source_texts_browse_is_one_statements_button_until_the_index_brings_some(site: Path) -> None:
    assert book(site, r"""
      // The first request for the index fails, so the book starts without it; its second, made for the page, is held.
      const before = [browse.textContent, browse.getAttribute('aria-label'), browse.getAttribute('aria-controls'),
        browse.getAttribute('aria-expanded'), browse.type, popover.contains(choice('Source texts') || null)];
      // A keyboard's Enter or Space clicks with no detail; the button shows the list at once, with no empty menu.
      browse.focus(); click(browse, {detail: 0});
      const direct = [popover.hidden, $('.bp-dag-inventory').hidden, $('.bp-dag-inventory h2').textContent,
        document.activeElement === browse];
      click(toMap);
      net.release('dependencies/index.json'); await waitFor(() => browse.textContent === 'Browse');
      const opened = [browse.getAttribute('aria-controls') === popover.id, Boolean(choice('Source texts'))];
      click(browse);
      return [before, direct, opened, popover.hidden, $('.bp-dag-stage').hidden];
    """, files={INDEX: SOURCES}, before="net.fail('dependencies/index.json'); net.hold('dependencies/index.json');") == [
        ["Statements", "Browse statements", None, None, "button", False],
        [True, False, "Statements", True], [True, True], False, False]


def test_back_to_a_page_without_source_texts_closes_browse_and_returns_its_focus(site: Path) -> None:
    files = {INDEX: index(*SOURCES["nodes"], row("there", "roadmap"))}
    assert book(site, r"""
      const focusing = [], focus = window.HTMLElement.prototype.focus;
      window.HTMLElement.prototype.focus = function (options) { focusing.push([this, options]); return focus.call(this, options); };
      // From a page without source texts to one with them, then Back while the menu is open on its Source texts.
      await waitFor(() => browse.textContent === 'Statements');
      click($('[data-navigation-node-id="here"]'));
      await waitFor(() => location.pathname === '/roadmap/here/index.html' && !host().hasAttribute('aria-busy')); await tick(200);
      click(browse); choice('Source texts').focus();
      await back(); await waitFor(() => location.pathname === '/roadmap/there/index.html' && !host().hasAttribute('aria-busy'));
      return [popover.hidden, Boolean(choice('Source texts')), document.activeElement === browse,
        JSON.stringify(focusing.at(-1)[1]), browse.getAttribute('aria-controls'), browse.getAttribute('aria-expanded'),
        $('.bp-dag-stage').hidden];
    """, page="roadmap/there/index.html", files=files) == [True, False, True, '{"preventScroll":true}', None, None, False]


def test_the_reader_leads_back_to_its_topic_and_lists_none_of_the_topics_other_pages(site: Path) -> None:
    """The topic's other pages are in Contents and on the map."""
    assert book(site, r"""
      return [$$('.bp-reading-context a').map(link => [link.textContent, link.href]),
        $$('.bp-reading-related-topics, .bp-reading-links').length];
    """, hash="#result") == [[["← Here", ORIGIN + HERE]], 0]
    # Without the topic's row there is nowhere to lead back to.
    rows = [row("roadmap", None), row("here/result", "here", scope="here")]
    assert book(site, "return $('.bp-reading-context').children.length;", hash="#result", files={INDEX: index(*rows)}) == 0


def _palette(site: Path) -> dict[str, dict]:
    return json.loads((site / "dependencies/chapters/here.json").read_text(encoding="utf-8"))["palette"]


def test_status_swatches_carry_both_schemes_colours_and_the_filter_says_what_each_means(site: Path) -> None:
    rows = [row("roadmap", None), row("here", "roadmap"), row("here/result", "here", scope="here", status="fully_proved"),
            row("here/peer", "here", scope="here", status="planned")]
    swatches = book(site, r"""
      const marks = {};
      for (const input of $$('.bp-dag-filter-popover input')) {
        const mark = input.parentElement.querySelector('.bp-dag-swatch'), style = mark.style;
        marks[input.value] = [['light', 'dark'].flatMap(scheme => ['fill', 'stroke'].map(part => style.getPropertyValue(`--dag-swatch-${scheme}-${part}`))),
          document.getElementById(input.getAttribute('aria-describedby')).textContent, mark.getAttribute('aria-hidden'), mark.title];
      }
      const search = $('.bp-dag-search'); search.value = 'here/'; emit(search, 'input');
      return [marks, $$('.bp-dag-result .bp-dag-swatch').map(mark => mark.title)];
    """, files={INDEX: index(*rows)})
    palette = _palette(site)
    # A finished state's small mark is filled with its stroke, too pale a tint to tell from an outline; an outlined one keeps
    # its fill. The filter names a status and describes it with its meaning, so the mark itself is hidden there.
    expected = {key: [[palette[key][scheme]["fill" if palette[key]["light"]["fill"] == "#FFFFFF" else "stroke"] if part == "fill"
                       else palette[key][scheme]["stroke"] for scheme in ("light", "dark") for part in ("fill", "stroke")],
                      palette[key]["meaning"], "true", ""] for key in ("fully_proved", "planned")}
    assert swatches == [expected, [palette["fully_proved"]["meaning"], palette["planned"]["meaning"]]]
    assert expected["fully_proved"][0][0] == palette["fully_proved"]["light"]["stroke"]
    assert expected["planned"][0][0] == palette["planned"]["light"]["fill"]


def _restated(site: Path, statuses: dict[str, str]) -> dict[str, object]:
    """The here topic's graph payload with its statements' statuses replaced."""
    payload = json.loads((site / "dependencies/chapters/here.json").read_text(encoding="utf-8"))
    nodes = [{**node, "status": statuses.get(node["id"], node["status"])} for node in payload["nodes"]]
    return {**payload, "nodes": nodes, "present_statuses": sorted({node["status"] for node in nodes})}


def test_a_status_change_on_the_map_keeps_the_selection_and_says_the_filter(site: Path) -> None:
    rows = [row("roadmap", None), row("here", "roadmap"), row("here/result", "here", scope="here", status="fully_proved"),
            row("here/peer", "here", scope="here", status="planned")]
    assert book(site, r"""
      const entries = window.history.length, state = () => [host().classList.contains('bp-dag-has-selection'),
        host().classList.contains('bp-dag-sheet-open'), host().dataset.mapStatuses || null, $('.bp-dag-filter-summary').textContent];
      const proved = $$('.bp-dag-filter-popover input').find(input => input.value === 'fully_proved');
      proved.checked = false; emit(proved, 'change');
      // The map dims the statuses the filter leaves out, so the selection and its reader stay; the history entry keeps the
      // filter without a new entry.
      const changed = [...state(), $('.bp-dag-sr-only[aria-live]').textContent, $('.bp-dag-hidden-button').hidden,
        window.history.length === entries, window.history.state.autoformNavigation.snapshot.statuses];
      // The index, which arrives late here, keeps both.
      net.release('dependencies/index.json'); await waitFor(() => $('.bp-dag-navigation-link')); await tick(100);
      return [changed, state()];
    """, files={INDEX: index(*rows), "dependencies/chapters/here.json": _restated(site, {"here/result": "fully_proved",
                                                                                     "here/peer": "planned"})},
        hash="#node=here/result",
        # The book starts without the index: its first request fails and the page's own is held.
        before="net.fail('dependencies/index.json'); net.hold('dependencies/index.json');") == [
        [True, True, "planned", "Filter: 1 of 2 statuses", "Filter: 1 of 2 statuses", True, True, ["planned"]],
        [True, True, "planned", "Filter: 1 of 2 statuses"]]


@pytest.mark.parametrize(("address", "summary", "dimmed"), [
    # Home's "Ready to work on" link asks for statuses this page may not have.
    ("#status=planned,fully_proved,can_prove", "Filter: all statuses", None),
    ("#status=planned,can_prove", "Filter: 1 of 2 statuses", "planned"),
])
def test_a_link_naming_absent_statuses_counts_only_the_present_ones_it_shows(site: Path, address: str, summary: str,
                                                                             dimmed: str | None) -> None:
    rows = [row("roadmap", None), row("here", "roadmap"), row("here/result", "here", scope="here", status="fully_proved"),
            row("here/peer", "here", scope="here", status="planned")]
    assert book(site, "return [$('.bp-dag-filter-summary').textContent, host().dataset.mapStatuses || null];",
                files={INDEX: index(*rows)}, hash=address) == [summary, dimmed]
