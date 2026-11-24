""""See also" and "Linked from" in a built book, in jsdom: the lists the optional wiki-links layer adds at the end of an
article, or of a statement's card, served to the page as the site serves dependencies/wiki-links.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.scaffold import scaffold_project
from tests.explorer_fixture import build_site, run_book
from tests.facts_fixture import write_annotated

_FOCAL = "https://blueprint.example.invalid/roadmap/focal/index.html"
#: The line a partial layer adds after its lists.
_PARTIAL = "Some pages could not be read, so these lists may miss some links."


@pytest.fixture(scope="module")
def wiki(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A book with a focal article holding statements X and Y, another article, and a chapter of 35 outgoing and 27
    incoming concepts to link them with."""
    project = tmp_path_factory.mktemp("wiki")
    scaffold_project(project, title="Wiki")
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text("# Wiki\n\nThe library's articles.\n", encoding="utf-8")
    statement = "---\ndeclaration: theorem\n---\n\n# {}\n\nA statement.\n\n## Depends on\n\nThis has no prerequisites.\n"
    articles = {
        "focal": ("Focal concept", [("x", "Statement X"), ("y", "Statement Y")]),
        "other": ("Other canonical concept", []),
        "concepts": ("Concepts", [(f"out-{n}", f"Outgoing concept {n}") for n in range(1, 36)]
                     + [(f"in-{n}", f"Incoming concept {n}") for n in range(1, 28)]),
    }
    for folder, (title, statements) in articles.items():
        (roadmap / folder).mkdir()
        (roadmap / folder / "README.md").write_text(f"# {title}\n\nThe article on {title.lower()}.\n", encoding="utf-8")
        for slug, name in statements:
            write_annotated(roadmap / folder / f"{slug}.md", statement.format(name))
    return build_site(project)


#: Serves args.layer as dependencies/wiki-links.json, bound to the exact bytes of the served index as the site binds
#: it, with references and unavailable sources named by article id: [from, to] or [from, to, times]. args.layer.held
#: holds it until layer.release() or layer.fail(); args.layer.answer serves "missing", "invalid" or "stale" instead.
_LAYER = r"""
var layer = {release() {}, fail() {}}, pages = [];
(() => {
  const spec = args.layer; if (!spec) return;
  const bytes = fs.readFileSync(path.join(site, 'dependencies/index.json')), ids = JSON.parse(bytes).nodes.map(node => node.id);
  const at = id => { assert(ids.includes(id), id); return ids.indexOf(id); };
  const references = (spec.references || []).flatMap(([from, to, times = 1]) => Array.from({length: times}, () => [at(from), at(to)]));
  const payload = {schema: 'autoform-wiki-links/v1', index_sha256: require('crypto').createHash('sha256').update(bytes).digest('hex'),
    availability: spec.availability || 'complete', references, unavailable_sources: (spec.unavailable || []).map(at)};
  if (spec.reason) payload.reason = spec.reason;
  if (spec.answer === 'stale') payload.index_sha256 = '0'.repeat(64);
  const href = root + 'dependencies/wiki-links.json';
  const answer = () => spec.answer === 'missing' ? respond(href, null) :
    respond(href, Buffer.from(spec.answer === 'invalid' ? '{"schema": ' : JSON.stringify(payload)));
  net.route = requested => {
    if (requested !== href) return undefined;
    if (!spec.held) return answer();
    return new window.Promise((resolve, reject) => {
      layer.release = () => resolve(answer()); layer.fail = () => reject(new window.TypeError('the network dropped the layer'));
    });
  };
})();
// Material's instant navigation, which announces each new page to its subscribers.
window.document$ = {subscribe(next) { pages.push(next); }};
"""

# The lists at the end of the reading or of a card: each direction's heading, its links and its "Show more" button.
_LISTS = r"""
const reading = () => $('.bp-reading-body'), sections = (scope = reading()) => $$('.bp-reading-wiki-links', scope);
function lists(section) {
  return $$('h2, h5', section).map(heading => ({heading, list: heading.nextElementSibling, more: heading.nextElementSibling.nextElementSibling,
    links: () => $$('a', heading.nextElementSibling)}));
}
const outline = section => lists(section).map(({heading, links}) => [heading.tagName + ' ' + heading.textContent, links().map(link => link.textContent)]);
const card = id => $$('.bp-reading-body .bp-thmwrapper').find(wrapper => wrapper.getAttribute('data-autoform-node-id') === id);
async function linked() { await waitFor(() => sections().length, 'the article ends with no links'); }
"""


def _run(site: Path, body: str, *, layer: dict[str, object], hash: str = "", before: str = "", ready: bool = True) -> None:
    run_book(site, "roadmap/focal/", "const focal = " + json.dumps(_FOCAL) + ";\n" + _LISTS + body, hash=hash,
             before=_LAYER + before, args={"layer": layer}, ready=ready)


def test_see_also_and_linked_from_end_the_article_naming_each_article_once(wiki: Path) -> None:
    # The layer lists a link once per occurrence and may hold an article's link to itself.
    references = [["focal", "concepts/out-1"], ["focal", "concepts/out-1"], ["concepts/in-1", "focal"], ["focal", "focal"],
                  ["focal", "concepts"]]
    _run(wiki, r"""
      await linked(); const [section] = sections();
      assert.equal(section.tagName, 'SECTION'); assert.equal(sections().length, 1);
      // After the article itself, under its own headings.
      assert(reading().firstElementChild.matches('.bp-reading-source')); assert.equal(reading().lastElementChild, section);
      assert.deepEqual(outline(section), [['H2 See also', ['Outgoing concept 1', 'Concepts']], ['H2 Linked from', ['Incoming concept 1']]]);
      assert.deepEqual(lists(section)[0].links().map(link => link.href),
        ['https://blueprint.example.invalid/roadmap/concepts/index.html#out-1', 'https://blueprint.example.invalid/roadmap/concepts/index.html']);
      assert.equal($('.bp-reading-wiki-notice'), null); assert(!section.textContent.includes(' · '));
      // The page redraws its reader as the map's filter changes; the lists stay the lists drawn.
      click($('.bp-dag-filter-summary')); $('.bp-dag-filter-popover input').click(); await tick(100);
      assert.deepEqual(sections(), [section]);
    """, layer={"references": references})


@pytest.mark.parametrize(("references", "headings"), [
    ([["concepts/in-1", "focal"]], ["H2 Linked from"]),
    ([["concepts/in-1", "focal"], ["focal", "concepts/out-1"]], ["H2 See also", "H2 Linked from"]),
    ([["focal", "concepts/out-1"]], ["H2 See also"]),
], ids=["linked-from", "both", "see-also"])
def test_a_partial_layer_says_its_lists_may_miss_links_and_never_why(
    wiki: Path, references: list[list[str]], headings: list[str],
) -> None:
    layer = {"references": references, "availability": "partial", "unavailable": ["focal", "other"],
             "reason": "/private/provider/parse.log"}
    _run(wiki, "const headings = " + json.dumps(headings) + ", partial = " + json.dumps(_PARTIAL) + ";\n" + r"""
      await linked(); const [section] = sections();
      assert.deepEqual(outline(section).map(([heading]) => heading), headings);
      assert.equal(section.lastElementChild.textContent, partial); assert(section.lastElementChild.matches('.bp-reading-wiki-notice'));
      // What a publisher said went wrong stays out of the page.
      assert(!document.documentElement.outerHTML.includes('/private/'));
    """, layer=layer)


@pytest.mark.parametrize("layer", [
    {"references": [["focal", "concepts/out-1"], ["concepts/in-1", "focal"]], "availability": "unavailable",
     "reason": "private-provider-wire:/private/parser/error.log"},
    {"references": []},
    {"references": [["concepts/out-1", "concepts/in-1"]], "availability": "partial", "unavailable": ["focal"]},
    {"references": [["focal", "concepts/out-1"]], "answer": "missing"},
    {"references": [["focal", "concepts/out-1"]], "answer": "invalid"},
    {"references": [["focal", "concepts/out-1"]], "answer": "stale"},
    {"references": [["focal", "concepts/out-1"]], "held": True},
], ids=["unavailable", "empty", "elsewhere", "missing", "invalid", "stale-index", "network"])
def test_a_layer_with_nothing_readable_for_the_article_adds_nothing(wiki: Path, layer: dict[str, object]) -> None:
    _run(wiki, r"""
      layer.fail(); await tick(500);
      assert(net.log.includes('dependencies/wiki-links.json'));
      assert.deepEqual(sections(), []); assert.equal(reading().children.length, 1);
      assert(!document.documentElement.outerHTML.includes('/private/'));
    """, layer=layer)


def test_a_see_also_link_is_followed_as_the_articles_own_links_are(wiki: Path) -> None:
    _run(wiki, r"""
      await linked(); const [link] = lists(sections()[0])[0].links(), entries = window.history.length;
      // A modified click is the browser's: the page leaves it alone.
      assert.equal(click(link, {ctrlKey: true}), false); await tick(200); assert.equal(location.href, focal);
      assert.equal(click(link), true); await tick(200);
      assert.equal(location.href, 'https://blueprint.example.invalid/roadmap/concepts/index.html#out-1');
      assert.equal(window.history.length, entries + 1);
    """, layer={"references": [["focal", "concepts/out-1"], ["concepts/in-1", "focal"]]})


def test_show_more_pages_each_list_on_its_own_and_moves_focus_to_the_first_new_link(wiki: Path) -> None:
    references = [["focal", f"concepts/out-{n}"] for n in range(1, 36)] + [[f"concepts/in-{n}", "focal"] for n in range(1, 12)]
    _run(wiki, r"""
      await linked(); const [outgoing, incoming] = lists(sections()[0]);
      assert.equal(outgoing.links().length, 10); assert.equal(incoming.links().length, 10);
      // Ten, then twenty more at a time; the button stays below the new links, so focus moves to the first of them.
      assert.equal(outgoing.more.textContent, 'Show 20 more links'); click(outgoing.more); await tick(0);
      assert.equal(outgoing.links().length, 30); assert.equal(outgoing.more.textContent, 'Show 5 more links');
      assert.equal(document.activeElement, outgoing.links()[10]);
      click(outgoing.more); await tick(0);
      assert.equal(outgoing.links().length, 35); assert.equal(outgoing.more.hidden, true);
      assert.equal(document.activeElement, outgoing.links()[30]);
      assert.equal(incoming.links().length, 10); assert.equal(incoming.more.textContent, 'Show 1 more link');
      // The two buttons can say the same, so each is described by its own list's heading.
      assert.deepEqual(lists(sections()[0]).map(group => group.more.getAttribute('aria-describedby')), [outgoing.heading.id, incoming.heading.id]);
      assert.notEqual(outgoing.heading.id, incoming.heading.id);
      click(incoming.more); await tick(0); assert.equal(incoming.links().length, 11);
    """, layer={"references": references})


def test_a_statements_lists_end_its_card_and_the_pages_own_lists_end_the_page(wiki: Path) -> None:
    references = [["focal/x", "concepts/out-1"], ["concepts/in-1", "focal/x"], ["focal", "concepts/out-2"], ["focal/x", "focal/y"]]
    _run(wiki, "const partial = " + json.dumps(_PARTIAL) + ";\n" + r"""
      await linked();
      // The selected statement's lists end its own card, under headings a level below the card's; the page's own end
      // the page, after its text.
      const [own, page] = sections();
      assert.equal(card('focal/x').lastElementChild, own); assert.deepEqual(sections(card('focal/y')), []);
      assert.deepEqual(outline(own), [['H5 See also', ['Outgoing concept 1', 'Statement Y']], ['H5 Linked from', ['Incoming concept 1']]]);
      assert.equal(reading().lastElementChild, page); assert.deepEqual(outline(page), [['H2 See also', ['Outgoing concept 2']]]);
      // Each partial line follows the lists it qualifies, and each heading describes only its own section's button.
      for (const section of [own, page]) assert.equal(section.lastElementChild.textContent, partial);
      const described = $$('.bp-reading-wiki-more').map(more => more.getAttribute('aria-describedby'));
      assert.equal(new Set(described).size, described.length);
      // Statement Y, followed from X's lists: its card takes its lists, and the page still ends with its own.
      click(lists(own)[0].links()[1]); await tick(200);
      assert.equal(location.href, 'https://blueprint.example.invalid/roadmap/focal/index.html#y');
      assert.deepEqual(sections(card('focal/x')), []);
      assert.deepEqual(outline(sections(card('focal/y'))[0]), [['H5 Linked from', ['Statement X']]]);
      assert.deepEqual(outline(reading().lastElementChild), [['H2 See also', ['Outgoing concept 2']]]);
      // The page itself: only its own lists, at its end.
      click($$('.bp-dag-breadcrumb a').find(link => link.textContent === 'Focal concept')); await tick(200);
      assert.equal(location.href, 'https://blueprint.example.invalid/roadmap/focal/index.html');
      assert.equal(sections().length, 1); assert.deepEqual(outline(reading().lastElementChild), [['H2 See also', ['Outgoing concept 2']]]);
    """, layer={"references": references, "availability": "partial", "unavailable": ["focal/x"]}, hash="#x")


def test_the_layer_is_asked_for_once_after_the_index_and_shows_before_the_readings_formulas(wiki: Path) -> None:
    # The reading's formulas are typeset when the test says.
    before = r"""
      var typeset = [];
      window.AutoformMath = {refresh(root) { return root.matches('.bp-reading-body') ? new window.Promise(done => typeset.push(done)) : window.Promise.resolve(true); }};
    """
    _run(wiki, r"""
      await linked(); assert(typeset.length > 0, 'the reading was not typeset');
      const asked = net.log.filter(name => name === 'dependencies/wiki-links.json');
      assert.equal(asked.length, 1); assert(net.log.indexOf('dependencies/index.json') < net.log.indexOf(asked[0]));
      // Pages read after it reuse it.
      typeset.splice(0).forEach(done => done(true)); await tick(200);
      click(lists(sections()[0])[0].links()[0]); await waitFor(() => typeset.length); typeset.splice(0).forEach(done => done(true));
      await tick(200); await back(); typeset.splice(0).forEach(done => done(true)); await tick(200);
      assert.equal(location.href, focal); assert.equal(net.log.filter(name => name === 'dependencies/wiki-links.json').length, 1);
    """, layer={"references": [["focal", "concepts/out-1"]]}, before=before, ready=False)


@pytest.mark.parametrize("ending", ["arrives", "fails", "detached", "while-read"])
def test_a_book_that_went_away_leaves_its_reading_as_it_was_whatever_its_layer_does(wiki: Path, ending: str) -> None:
    # Material's instant navigation swaps the page out: the explorer disposes the book it held. A book whose page is
    # only detached is shut down by the router when its layer arrives. A long layer is read a slice at a time.
    times = 1025 if ending == "while-read" else 1
    _run(wiki, "const ending = " + json.dumps(ending) + ";\n" + r"""
      const kept = reading();
      // One timer of the page's own, the next due.
      async function step() {
        await flush(); let due = null;
        for (const timer of clock.timers.values()) if (!due || timer.at < due.at) due = timer;
        if (due) { clock.timers.delete(due.id); clock.now = Math.max(clock.now, due.at); due.callback(...due.rest); }
        await flush();
      }
      if (ending === 'while-read') { layer.release(); await step(); }
      $('.bp-explorer-page').remove();
      if (ending !== 'detached') pages.forEach(next => next());
      const left = kept.innerHTML;
      if (ending === 'fails') layer.fail(); else layer.release();
      await tick(3000);
      assert.equal(kept.innerHTML, left);
      if (ending !== 'while-read') assert.deepEqual(sections(kept), []);
    """, layer={"references": [["focal", "concepts/out-1", times]], "held": True})


def test_a_long_layer_shows_its_lists_whole_once_it_is_read(wiki: Path) -> None:
    references = [["focal", "concepts/out-1", 2048], ["focal", "concepts/out-2"]]
    _run(wiki, "const partial = " + json.dumps(_PARTIAL) + ";\n" + r"""
      const seen = new Set();
      // Read a timer at a time, the article shows no lists, then all of them: never only the first slice's.
      layer.release();
      for (let round = 0; round < 40 && !sections().length; round++) {
        await flush(); let due = null;
        for (const timer of clock.timers.values()) if (!due || timer.at < due.at) due = timer;
        if (due) { clock.timers.delete(due.id); clock.now = Math.max(clock.now, due.at); due.callback(...due.rest); }
        await flush(); seen.add(JSON.stringify(sections().map(outline)));
      }
      assert.deepEqual([...seen].filter(state => state !== '[]').map(JSON.parse), [[[['H2 See also', ['Outgoing concept 1', 'Outgoing concept 2']]]]]);
      assert.equal(sections()[0].lastElementChild.textContent, partial);
    """, layer={"references": references, "availability": "partial", "unavailable": ["focal"], "held": True})


def test_a_redraw_keeps_the_paged_focused_lists_and_another_article_starts_them_over(wiki: Path) -> None:
    references = [[article, f"concepts/out-{n}"] for article in ("focal", "other") for n in range(1, 28)]
    references += [[f"concepts/in-{n}", article] for article in ("focal", "other") for n in range(1, 28)]
    _run(wiki, r"""
      await linked(); const section = sections()[0], [outgoing] = lists(section), more = outgoing.more;
      click(more); await tick(0); assert.equal(outgoing.links().length, 27);
      const focused = outgoing.links()[26]; focused.focus(); reading().scrollTop = 137;
      // The filter redraws the reader; the lists stay as the reader left them: paged, focused and scrolled.
      for (let n = 0; n < 3; n++) {
        click($('.bp-dag-filter-summary')); $('.bp-dag-filter-popover input').click(); await tick(100);
        assert.deepEqual(sections(), [section]); assert.equal(lists(section)[0].more, more); assert.equal(more.hidden, true);
        assert.equal(outgoing.links().length, 27); assert.equal(document.activeElement, focused); assert.equal(reading().scrollTop, 137);
      }
      // No list of the topic's other pages joins them: Contents and the map list those.
      assert.equal(reading().children.length, 2);
      // Another article starts its own lists at their first page.
      click($$('.bp-dag-navigation-link').find(link => link.textContent === 'Other canonical concept')); await tick(200);
      assert.equal(location.href, 'https://blueprint.example.invalid/roadmap/other/index.html');
      const [after, before] = lists(sections()[0]);
      assert.equal(after.links().length, 10); assert.equal(before.links().length, 10); assert.equal(after.more.textContent, 'Show 17 more links');
    """, layer={"references": references})
