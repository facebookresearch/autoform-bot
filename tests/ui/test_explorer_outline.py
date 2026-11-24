"""Contents and the breadcrumb trail, on a built book's pages with the explorer it ships (:mod:`tests.explorer_fixture`).

Contents projects the search index's parent tree, so a test that needs a shape the example site cannot make serves its own
index in place of the site's; the page and the explorer stay the site's own. The page opened is the ``here`` topic's, so a
shape names the current topic ``here``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.scaffold import scaffold_project
from autoform_cli.ui import project_map
from tests.explorer_fixture import ARTICLE, ORIGIN, build_site, run_book
from tests.facts_fixture import write_annotated

HERE = "roadmap/here/index.html"


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("book")
    scaffold_project(project, title="Library")
    roadmap = project / "blueprint/roadmap"
    (roadmap / "README.md").write_text("# Library\n\nGoal.\n\n## Chapters\n\n- [Here](here/README.md)\n- [There](there/README.md)\n"
                                       "- [Shelf](shelf/README.md)\n", encoding="utf-8")
    for topic in ("here", "there"):
        (roadmap / topic).mkdir()
        (roadmap / topic / "README.md").write_text(f"# {topic.title()}\n\nProse.\n", encoding="utf-8")
        for name in ("result", "peer"):
            write_annotated(roadmap / topic / f"{name}.md", ARTICLE.format(
                kind="theorem", lean="Demo.main", title=f"{name.title()} {topic}", body="Statement.", uses="None."))
    # A topic whose page only lists its own topics: an overview, which leaves the stage to the map.
    (roadmap / "shelf/leaf").mkdir(parents=True)
    (roadmap / "shelf/README.md").write_text("# Shelf\n\n## Topics\n\n- [Leaf](leaf/README.md)\n", encoding="utf-8")
    (roadmap / "shelf/leaf/README.md").write_text("# Leaf\n\nProse.\n", encoding="utf-8")
    write_annotated(roadmap / "shelf/leaf/s.md", ARTICLE.format(kind="theorem", lean="Demo.main", title="S",
                                                                body="Statement.", uses="None."))
    return build_site(project)


def scope(node_id: str, parent: str | None, **extra: object) -> dict[str, object]:
    """An index row for a topic, routed to its page as render routes one."""
    route = "../roadmap/index.html" if node_id == "roadmap" else f"../roadmap/{node_id}/index.html"
    return {"id": node_id, "title": node_id, "kind": "scope", "scope": node_id, "parent": parent, "result": False,
            "status": "planned", "route": route, **extra}


def page(node_id: str, parent: str, **extra: object) -> dict[str, object]:
    """An index row for a statement or an article written at the roadmap's top."""
    return {**scope(node_id, parent), "kind": "node", "scope": "roadmap", "result": True, **extra}


def index(*nodes: dict[str, object]) -> dict[str, object]:
    return {"schema": "autoform-dag-search/v1", "nodes": list(nodes), "relations": []}


def relabel(nodes: list[dict[str, object]], current: str) -> list[dict[str, object]]:
    """The rows with *current* renamed ``here``, the topic whose page the book opens."""
    name = {current: "here"}
    return [{**node, **{key: name.get(node[key], node[key]) for key in ("id", "parent", "scope") if node.get(key)},
             **({"route": f"../roadmap/here/index.html#{node['id']}"} if node.get("parent") == current and node["kind"] == "node"
                else {"route": "../roadmap/here/index.html"} if node["id"] == current else {})} for node in nodes]


def padded(nodes: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
    """The rows with chapters ahead of the others, so the first 120 rows Contents shows leave *limit* for the rest."""
    return [nodes[0], *(scope(f"f{number:03d}", "roadmap") for number in range(120 - limit)), *nodes[1:]]


#: Contents and the trail as the reader sees them.
READ = r"""
// Each row: its link's node id, or "# " and a heading's text, with its depth.
const outline = () => $$('.bp-dag-navigation-list > li').map(item => {
  const link = item.querySelector('a');
  return [link ? link.dataset.navigationNodeId : '# ' + item.textContent, Number(item.className.split('-').pop())];
});
const trail = () => $$('.bp-dag-breadcrumb li').map(item => [item.textContent, item.querySelector('a') ? item.querySelector('a').href : null]);
// A primary click another handler cancelled first is that handler's: the page keeps its address, history and requests.
async function clickCancelled(link) {
  const before = [location.href, window.history.length, net.log.length];
  window.addEventListener('click', event => event.preventDefault(), {capture: true, once: true});
  click(link); await tick(200);
  assert.deepEqual([location.href, window.history.length, net.log.length], before);
}
"""


def book(site: Path, body: str, *, files: dict[str, object] | None = None, page: str = HERE, before: str = "",
         **options: object) -> object:
    """Run *body* on *page* with *files* served in place of the site's, and return what it returns."""
    out = run_book(site, page, READ + body, before=before, files=files, **options)
    return json.loads(out) if out else None


def shown(site: Path, nodes: list[dict[str, object]], **options: object) -> list[list[object]]:
    """Contents for the rows, without the chapters :func:`padded` adds."""
    rows = book(site, "return outline();", files={"dependencies/index.json": index(*nodes)}, **options)
    return [row for row in rows if not (row[0][0] == "f" and row[0][1:].isdigit())]


@pytest.mark.parametrize("branch_first", [False, True])
def test_contents_walks_the_authored_parent_tree_not_the_index_order(site: Path, branch_first: bool) -> None:
    root, schemes = scope("roadmap", None), scope("schemes", "roadmap")
    rows = [scope("cohomology", "roadmap"), scope("varieties", "schemes"), scope("curves", "roadmap"),
            page("chapter-zero", "roadmap"), scope("coherent", "schemes"), scope("fine", "varieties"), schemes,
            scope("deep", "fine"), scope("here", "deep"), page("not-a-root-page", "varieties", scope="varieties")]
    rows = [root, schemes, *(row for row in rows if row is not schemes)] if branch_first else [root, *rows]
    branch = [["schemes", 0], ["varieties", 1], ["fine", 2], ["deep", 3], ["here", 3], ["coherent", 1]]
    siblings = [["cohomology", 0], ["curves", 0], ["chapter-zero", 0]]
    # The current topic's ancestors open; a statement under another topic is that topic's, so it is not listed.
    assert shown(site, rows) == (branch + siblings if branch_first else siblings + branch)


def test_contents_heads_a_collection_and_looks_through_wrappers_and_source_tracks(site: Path) -> None:
    rows = [scope("roadmap", None), scope("here", "pagination"),
            scope("collection", "roadmap", navigation="collection", title="Authored collection"), page("chapter-zero", "roadmap"),
            scope("schemes", "roadmap"), scope("pagination", "collection", navigation="transparent"),
            scope("source", "collection", navigation="source"), page("reading-note", "schemes", scope="schemes"),
            # A parent cycle through wrappers reaches no topic, so nothing in it is listed and Contents still draws.
            scope("loop", "knot", navigation="transparent"), scope("knot", "loop", navigation="transparent"), scope("tangled", "loop")]
    assert book(site, r"""
      const heading = $('.bp-outline-collection');
      return [outline(), heading.tagName, $$('a, button', heading.parentElement).length];
    """, files={"dependencies/index.json": index(*rows)}) == [
        [["# Authored collection", 0], ["here", 1], ["chapter-zero", 0], ["schemes", 0]], "SPAN", 0]


def test_contents_marks_the_current_page_and_its_links_navigate_in_place(site: Path) -> None:
    rows = [scope("roadmap", None), scope("cohomology", "roadmap"), scope("varieties", "schemes"),
            scope("curves", "roadmap"), page("chapter-zero", "roadmap", route="../roadmap/there/index.html"),
            scope("coherent", "schemes"), scope("fine", "varieties"), scope("schemes", "roadmap"), scope("deep", "fine"),
            scope("here", "deep"), scope("wrapped-topic", "transparent"),
            scope("collection", "roadmap", navigation="collection"), scope("transparent", "collection", navigation="transparent")]
    assert book(site, r"""
      const before = outline(), current = $$('.bp-dag-navigation-link[aria-current="page"]').map(link => link.dataset.navigationNodeId);
      const link = $('[data-navigation-node-id="chapter-zero"]'), fetched = net.log.length;
      // A modified or secondary click is the browser's: the page neither cancels it nor navigates.
      for (const init of [{ctrlKey: true}, {metaKey: true}, {shiftKey: true}, {altKey: true}, {button: 1}])
        assert.equal(click(link, init), false, JSON.stringify(init));
      assert.equal(net.log.length, fetched);
      await clickCancelled(link);
      assert.equal(click(link), true);
      await waitFor(() => location.pathname === '/roadmap/there/index.html' && document.activeElement.tagName === 'H1');
      return [before, current, link.getAttribute('href'), window.history.length, $('.bp-reading-body h1').textContent];
    """, files={"dependencies/index.json": index(*rows)}) == [
        [["cohomology", 0], ["curves", 0], ["chapter-zero", 0], ["schemes", 0], ["varieties", 1], ["fine", 2], ["deep", 3],
         ["here", 3], ["coherent", 1], ["# collection", 0], ["wrapped-topic", 1]],
        ["here"], ORIGIN + "roadmap/there/index.html", 2, "There"]


def test_on_a_statements_address_contents_lists_it_but_not_its_peers(site: Path) -> None:
    rows = [scope("roadmap", None), page("here/result", "here", scope="here", route="../roadmap/here/index.html#result"),
            scope("topic", "roadmap"), page("here/peer", "here", scope="here", route="../roadmap/here/index.html#peer"),
            scope("here", "topic")]
    assert book(site, r"""
      return [outline(), $$('[aria-current="page"]', $('.bp-dag-navigation-inner')).map(link => link.dataset.navigationNodeId)];
    """, files={"dependencies/index.json": index(*rows)}, hash="#result") == [
        [["topic", 0], ["here", 1], ["here/result", 2]], ["here/result"]]


def test_show_more_sections_reveals_the_rest_in_order_and_focuses_the_first_new_link(site: Path) -> None:
    chapter = lambda node_id, area: scope(node_id, "roadmap", area=area)  # noqa: E731
    rows = padded([scope("roadmap", None), chapter("logic", "Foundations"), chapter("sets", "Foundations"),
                   chapter("geometry", "Geometry"), chapter("here", "Geometry")], 2)
    assert book(site, r"""
      const rows = outline(), roots = [];
      window.AutoformMath = {refresh(root) { roots.push(root); return Promise.resolve(true); }};
      $('.bp-dag-navigation-more').focus(); click($('.bp-dag-navigation-more'));
      const focused = document.activeElement;
      await tick(100);
      return [rows.slice(-2), outline().slice(-6), [focused.dataset.navigationNodeId, focused.parentElement.tagName],
        Boolean($('.bp-dag-navigation-more')), roots.map(root => root.className), window.history.state.autoformNavigation.snapshot.outlineLimit];
    """, files={"dependencies/index.json": index(*rows)}) == [
        # A heading the limit parts from its chapters waits with them.
        [["# Geometry", 0], ["here", 1]],
        [["# Foundations", 0], ["logic", 1], ["sets", 1], ["# Geometry", 0], ["geometry", 1], ["here", 1]],
        # The first new link, not a heading, and not the row at the old count; only Contents is typeset again.
        ["logic", "LI"], False, ["bp-dag-navigation-inner"], 170]


def _areas() -> list[dict[str, object]]:
    chapter = lambda node_id, area=None: scope(node_id, "roadmap", **({"area": area} if area else {}))  # noqa: E731
    return [scope("roadmap", None), chapter("logic", "Foundations"), chapter("sets", "Foundations"), chapter("algebra", "Algebra"),
            page("lemma", "roadmap", area="Algebra"), chapter("notes"), chapter("geometry", "Geometry"), chapter("topology", "Geometry"),
            chapter("models", "Foundations"), scope("library", "roadmap", navigation="collection", area="Algebra"),
            scope("manifolds", "topology"), scope("smooth", "manifolds"), scope("lie", "smooth"), scope("groups", "lie")]


@pytest.mark.parametrize(("current", "limit", "expected"), [
    # A one-chapter area and a chapter without one keep their place, and an area that comes back is headed again. A
    # statement or a collection at the top neither counts toward an area nor goes under one. A heading takes a level, so
    # the current page sits one level deeper before the indent stops growing.
    pytest.param("groups", 120, [["# Foundations", 0], ["logic", 1], ["sets", 1], ["algebra", 0], ["lemma", 0], ["notes", 0],
                                 ["# Geometry", 0], ["geometry", 1], ["topology", 1], ["manifolds", 2], ["smooth", 3],
                                 ["lie", 4], ["here", 4], ["# Foundations", 0], ["models", 1], ["# library", 0]], id="deep"),
    pytest.param("roadmap", 120, [["# Foundations", 0], ["logic", 1], ["sets", 1], ["algebra", 0], ["lemma", 0], ["notes", 0],
                                  ["# Geometry", 0], ["geometry", 1], ["topology", 1], ["# Foundations", 0], ["models", 1],
                                  ["# library", 0]], id="root"),
    # However short the list, the current chapter keeps its heading and its own topics, and a heading cut off from its
    # chapters waits.
    pytest.param("topology", 1, [["# Geometry", 0], ["here", 1], ["manifolds", 2]], id="limit-1"),
    pytest.param("topology", 3, [["# Geometry", 0], ["here", 1], ["manifolds", 2]], id="limit-3"),
    pytest.param("topology", 5, [["# Foundations", 0], ["logic", 1], ["# Geometry", 0], ["here", 1], ["manifolds", 2]], id="limit-5"),
])
def test_area_headings_head_each_run_of_an_areas_chapters(site: Path, current: str, limit: int, expected: list) -> None:
    rows = padded(_areas(), limit) if limit < 120 else _areas()
    on_root = current == "roadmap"
    assert shown(site, rows if on_root else relabel(rows, current), page="roadmap/index.html" if on_root else HERE) == expected


@pytest.mark.parametrize(("areas", "expected"), [
    # A chapter without an area ends a run, so its area's next chapter is headed again rather than set under it.
    pytest.param(["Algebra", None, "Algebra", "Geometry", "Geometry"],
                 [["# Algebra", 0], ["c0", 1], ["c1", 0], ["# Algebra", 0], ["c2", 1], ["# Geometry", 0], ["c3", 1], ["c4", 1]],
                 id="interrupted"),
    # Areas head nothing until the chapters name two of them and one holds two chapters.
    pytest.param([None, None], [["c0", 0], ["c1", 0]], id="none"),
    pytest.param(["Algebra", "Algebra"], [["c0", 0], ["c1", 0]], id="one-area"),
    pytest.param(["Algebra", "Algebra", None], [["c0", 0], ["c1", 0], ["c2", 0]], id="one-area-and-none"),
    pytest.param(["Algebra", "Geometry"], [["c0", 0], ["c1", 0]], id="no-area-of-two"),
])
def test_areas_head_chapters_only_where_the_map_would_group_them(site: Path, areas: list, expected: list) -> None:
    rows = [scope("roadmap", None), *(scope(f"c{at}", "roadmap", **({"area": area} if area else {})) for at, area in enumerate(areas))]
    assert shown(site, rows, page="roadmap/index.html") == expected


@pytest.mark.parametrize(("current", "address", "limit", "expected"), [
    # A topic's own topics, through the collection and the part between it and some of them.
    pytest.param("topology", "", 1, [["# Geometry", 0], ["here", 1], ["manifolds", 2], ["# shelf", 2], ["knots", 3], ["braids", 2]],
                 id="topic"),
    # On one of its statements' addresses too, where the page is still the topic's.
    pytest.param("topology", "#lemma", 1, [["# Geometry", 0], ["here", 1], ["manifolds", 2], ["# shelf", 2], ["knots", 3],
                                           ["braids", 2], ["here/lemma", 2]], id="statement"),
    # On the root, every chapter, each under its area's heading, but not a statement at the top.
    pytest.param("roadmap", "", 1, [["# Foundations", 0], ["logic", 1], ["sets", 1], ["# Geometry", 0], ["geometry", 1],
                                    ["topology", 1]], id="root"),
    # Elsewhere the limit holds as before, and a source track, which has no child index, keeps it too.
    pytest.param("other", "", 1, [["# Geometry", 0], ["geometry", 1], ["here", 2]], id="other"),
    pytest.param("source", "", 2, [["# Foundations", 0], ["logic", 1]], id="source-track"),
])
def test_contents_always_lists_the_current_topics_own_topics(site: Path, current: str, address: str, limit: int,
                                                             expected: list) -> None:
    """A child index the reader hides lists the current topic's own topics, so Contents lists them all however short its
    list. Only a topic counts, on a topic's page, as render finds a child index."""
    chapter = lambda node_id, area: scope(node_id, "roadmap", area=area)  # noqa: E731
    rows = [scope("roadmap", None), chapter("logic", "Foundations"), chapter("sets", "Foundations"), chapter("geometry", "Geometry"),
            chapter("topology", "Geometry"), scope("manifolds", "topology"), scope("shelf", "topology", navigation="collection"),
            scope("knots", "shelf"), scope("part", "topology", navigation="transparent"), scope("braids", "part"),
            scope("other", "geometry"), page("axiom", "roadmap"), page("lemma", "topology", scope="topology"),
            scope("source", "roadmap", navigation="source"), scope("lifted", "source")]
    rows = padded(rows, limit)
    if current == "roadmap":
        assert shown(site, rows, page="roadmap/index.html") == expected
        return
    rows = relabel(rows, current)
    if address:
        rows = [{**row, "id": "here/lemma"} if row["id"] == "lemma" else row for row in rows]
    assert shown(site, rows, hash=address) == expected


@pytest.mark.parametrize(
    ("rows", "regions", "headings", "unheaded"),
    [
        pytest.param(
            [("logic", "roadmap", "Foundations", None), ("sets", "roadmap", "Foundations", None),
             ("algebra", "roadmap", "Algebra", None), ("lemma", "roadmap", "Algebra", "statement"),
             ("geometry", "roadmap", "Geometry", None), ("topology", "roadmap", "Geometry", None),
             ("notes", "roadmap", None, None), ("analysis", "roadmap", "Analysis", None),
             ("probability", "roadmap", "Analysis", None), ("tables", "roadmap", None, None)],
            # The map gathers the chapters without an area, with its part for the root's own statements, under "Other".
            {"Foundations": ["logic", "sets"], "Geometry": ["geometry", "topology"], "Analysis": ["analysis", "probability"],
             "Other": ["notes", "roadmap/~results", "tables"]},
            {"Foundations": ["logic", "sets"], "Geometry": ["geometry", "topology"], "Analysis": ["analysis", "probability"]},
            ["algebra", "lemma", "notes", "tables"],
            id="chapters",
        ),
        pytest.param(
            [("a1", "roadmap", "A", None), ("a2", "roadmap", "A", None), ("b1", "roadmap", "B", None), ("b2", "roadmap", "B", None)],
            {"A": ["a1", "a2"], "B": ["b1", "b2"]}, {"A": ["a1", "a2"], "B": ["b1", "b2"]}, [],
            id="two-areas",
        ),
        pytest.param(
            [("a1", "roadmap", "A", None), ("a2", "roadmap", "A", None), ("x", "roadmap", None, None), ("y", "roadmap", None, None)],
            {}, {}, ["a1", "a2", "x", "y"],
            id="one-area",
        ),
        pytest.param(
            [("a1", "roadmap", "A", None), ("a2", "roadmap", "A", None), ("b1", "roadmap", "B", None),
             ("library", "roadmap", "B", "collection"), ("c1", "library", "B", None), ("c2", "library", "B", None),
             ("notes", "roadmap", None, "source"), ("s1", "notes", "A", None), ("s2", "notes", "C", None),
             ("s3", "notes", "C", None)],
            # The map flattens the collection, so its chapters join B, and moves s1, which names a chapter's area, into A.
            # Contents keeps the collection's subhead and leaves the topics it lifts out of the source track unheaded.
            {"A": ["a1", "a2", "s1"], "B": ["b1", "c1", "c2"]}, {"A": ["a1", "a2"]}, ["b1", "# library", "s1", "s2", "s3"],
            id="collection-and-source-track",
        ),
        pytest.param(
            [("logic", "roadmap", "Foundations", None), ("sets", "roadmap", "Foundations", None),
             ("track", "roadmap", None, "source"), ("geometry", "track", "Geometry", None), ("topology", "track", "Geometry", None)],
            {}, {}, ["logic", "sets", "geometry", "topology"],
            id="areas-only-inside-a-source-track",
        ),
    ],
)
def test_area_headings_name_the_regions_the_map_draws(
    site: Path, rows: list[tuple[str, str, str | None, str | None]], regions: dict[str, list[str]], headings: dict[str, list[str]],
    unheaded: list[str],
) -> None:
    """Contents restates project_map.py's region rule in JS: each book pins both, and every heading is a map region."""
    records: list[dict[str, object]] = [{"id": "roadmap", "kind": "scope", "title": "Library", "parent": None, "result": False}]
    for node_id, parent, area, role in rows:
        record: dict[str, object] = {"id": node_id, "title": node_id, "parent": parent, **({"area": area} if area else {})}
        if role == "statement":
            records.append({**record, "kind": "node", "result": True, "status": "fully_proved"})
            continue
        records.append({**record, "kind": "scope", "result": False, **({"navigation": role} if role else {})})
        if role is None:
            records.append({"id": f"{node_id}/result", "kind": "node", "title": "Result", "parent": node_id, "result": True,
                            "status": "fully_proved"})
    files = project_map.build_project_map(records, [])
    drawn = {}
    for region in files["index.json"]["regions"]:
        if region["id"].startswith("region:"):
            part = files[f"parts/{project_map.part_file(region['id'])}"]["parts"][region["id"]]
            drawn[region["title"]] = sorted(child["id"] for child in part["children"])
    assert drawn == regions
    nodes = [{**record, "scope": record["id"] if record["kind"] == "scope" else record["parent"], "status": "planned",
              "route": scope(str(record["id"]), None)["route"]} for record in records]
    headed: dict[str, list[str]] = {}
    found: list[str] = []
    heading = None
    for name, depth in shown(site, nodes, page="roadmap/index.html"):
        if depth == 0:
            heading = name[2:] if name.startswith("# ") and name[2:] in regions else None
            if heading:
                headed[heading] = []
            else:
                found.append(name)
        elif heading and depth == 1:
            headed[heading].append(name)
    assert headed == headings and found == unheaded
    assert all(set(members) <= set(regions[title]) for title, members in headed.items()), "Every heading is one of the map's regions"


#: Gives the trail a width (and the width its crumbs need) and tells the page the window was resized.
SIZE = r"""
const nav = $('.bp-dag-breadcrumb');
function size(width, needed) {
  Object.defineProperty(nav, 'clientWidth', {value: width, configurable: true});
  Object.defineProperty(nav, 'scrollWidth', {value: needed, configurable: true});
  window.dispatchEvent(new window.Event('resize'));
}
"""


@pytest.mark.parametrize(("fold", "focused", "expected"), [
    # Too narrow a row folds the trail; focus stays on the crumb it was on when that crumb is still drawn...
    pytest.param("overflow", "here", "here", id="overflow-keeps-the-parent"),
    # ...and goes to the trail itself when the fold hid it. A phone folds any trail longer than three crumbs.
    pytest.param("phone", "b", "NAV", id="phone-folds-away-the-grandparent"),
])
def test_the_trail_refolds_when_its_width_changes_and_keeps_focus(site: Path, fold: str, focused: str, expected: str) -> None:
    groups = r"Groups $\mathbb{Z}/n$"
    rows = [scope("roadmap", None, title="Library"), scope("a", "roadmap"),
            scope("b", "a", title=groups, reading_title={"format": "inline-parts-v1", "source": groups,
                                                         "parts": [["text", "Groups "], ["math", r"\(\mathbb{Z}/n\)"]]}),
            scope("here", "b"), page("here/result", "here", scope="here", title="Result", route="../roadmap/here/index.html#result")]
    whole, folded, kept, unchanged = book(site, SIZE + r"""
      const roots = [], focusing = [], focus = window.HTMLElement.prototype.focus;
      window.AutoformMath = {refresh(root) { roots.push(root); return Promise.resolve(true); }};
      window.HTMLElement.prototype.focus = function (options) { focusing.push([this, options]); return focus.call(this, options); };
      size(600, 500);
      const whole = trail(), drawn = nav.firstChild, link = $$('a', nav).find(item => item.href.endsWith('/' + args.focused + '/index.html'));
      link.focus(); roots.length = 0;
      // A resize that leaves the trail's width alone draws nothing again.
      size(600, 500);
      const unchanged = nav.firstChild === drawn && document.activeElement === link && !roots.includes(nav);
      if (args.fold === 'phone') media['(max-width: 720px)'] = true;
      size(300, 700);
      const middle = nav.querySelector('li:nth-child(2)'), at = document.activeElement;
      assert.notStrictEqual(at, link); assert.deepEqual(focusing.at(-1), [at, {preventScroll: true}]);
      // The trail is typeset again, once, and the reading is not.
      assert.deepEqual(roots.filter(root => root === nav || root.contains(nav) || nav.contains(root)), [nav]);
      assert(!roots.includes($('.bp-reading-body')));
      return [whole, [trail().map(crumb => crumb[0]), middle.title, middle.querySelector('.bp-dag-sr-only').textContent,
        $$('[aria-current="page"]', nav).map(item => item.textContent)], [at.tagName === 'A' ? at.textContent : at.tagName, at.tabIndex],
        unchanged];
    """.replace("args.focused", json.dumps(focused)).replace("args.fold", json.dumps(fold)),
        files={"dependencies/index.json": index(*rows)}, hash="#result")
    assert whole == [["Home", ORIGIN + "index.html"], ["Library", ORIGIN + "roadmap/index.html"], ["a", ORIGIN + "roadmap/a/index.html"],
                     [r"Groups \(\mathbb{Z}/n\)", ORIGIN + "roadmap/b/index.html"], ["here", ORIGIN + "roadmap/here/index.html"],
                     ["Result", None]]
    # The fold keeps first / … / parent / current, and the ellipsis names what it hides, formulas read as text.
    hidden = "Library / a / Groups ℤ/n"
    assert folded == [["Home", "…" + hidden, "here", "Result"], hidden, hidden, ["Result"]]
    assert kept == ([expected, 0] if expected != "NAV" else ["NAV", -1]) and unchanged


@pytest.mark.parametrize("base", ["", "books/demo/"], ids=["site-at-the-origin", "site-in-a-folder"])
def test_home_leads_every_trail_and_is_the_only_crumb_at_home(site: Path, base: str) -> None:
    # Home is the deployment's home page: for a book deployed in a folder, that folder's index.html, not the origin's.
    on_topic, at_home = book(site, r"""
      const home = $('.bp-dag-breadcrumb li:first-child > a'), fetched = net.log.length;
      const first = [home.className, home.href, $$('.bp-dag-book', host()).length];
      for (const init of [{ctrlKey: true}, {metaKey: true}, {shiftKey: true}, {altKey: true}, {button: 1}])
        assert.equal(click(home, init), false, JSON.stringify(init));
      assert.equal(net.log.length, fetched);
      await clickCancelled(home);
      assert.equal(click(home), true);
      await waitFor(() => location.href === root + 'index.html' && !host().hasAttribute('aria-busy'));
      await tick(200);
      return [first, [trail(), $$('.bp-dag-breadcrumb [aria-current="page"]').length]];
    """, base=base)
    assert on_topic == ["bp-dag-book", ORIGIN + base + "index.html", 1]
    assert at_home == [[["Home", None]], 1]
    # Without the index, the trail the page's payload gives still starts at the deployment's home, and leads there.
    unavailable = book(site, r"""
      await tick(200); const home = trail()[0];
      assert.equal(click($('.bp-dag-book')), true);
      await waitFor(() => location.href === root + 'index.html' && !host().hasAttribute('aria-busy'));
      return home;
    """, base=base, before="net.route = href => href.endsWith('dependencies/index.json') ? respond(href, null, 500) : undefined;")
    assert unavailable == ["Home", ORIGIN + base + "index.html"]


@pytest.mark.parametrize(("current", "address", "expected"), [
    pytest.param("shelf", "", [["shelf", None]], id="the-collections-own-page"),
    pytest.param("shelf", "#thm", [["shelf", ORIGIN + HERE], ["thm", None]], id="a-statement-on-it"),
    # Dynamics/MathlibExt's trail ended at Dynamics while its header said MathlibExt.
    pytest.param("topic", "", [["topic", None]], id="a-topic-inside-it"),
])
def test_a_collection_ends_its_own_trail_and_never_stands_between_crumbs(site: Path, current: str, address: str,
                                                                         expected: list) -> None:
    rows = [scope("roadmap", None), scope("area", "roadmap"), scope("shelf", "area", navigation="collection"), scope("topic", "shelf"),
            page("shelf/thm", "shelf", scope="shelf", title="thm")]
    rows = [{**row, "title": row["id"]} if row["id"] == current else row for row in relabel(rows, current)]
    rows = [{**row, "id": "here/thm", "route": "../roadmap/here/index.html#thm"} if row["id"] == "shelf/thm" else row for row in rows]
    crumbs = book(site, "return trail();", files={"dependencies/index.json": index(*rows)}, hash=address)
    assert crumbs == [["Home", ORIGIN + "index.html"], ["roadmap", ORIGIN + "roadmap/index.html"],
                      ["area", ORIGIN + "roadmap/area/index.html"], *expected]


def test_a_parent_crumb_navigates_in_place_to_the_map_and_home_to_the_reading(site: Path) -> None:
    # On a phone the map and the reader take turns: a crumb above the page asks for the map, Home for its reading.
    assert book(site, r"""
      const open = () => host().classList.contains('bp-dag-sheet-open');
      // A phone folds only a trail longer than three crumbs.
      assert.deepEqual(trail().map(crumb => crumb[0]), ['Home', 'Library', 'Here']);
      assert.equal(click($$('.bp-dag-breadcrumb a').find(link => link.textContent === 'Library')), true);
      await waitFor(() => location.pathname === '/roadmap/index.html' && !host().hasAttribute('aria-busy')); await tick(200);
      const parent = [window.history.length, open()];
      assert.equal(click($('.bp-dag-breadcrumb .bp-dag-book')), true);
      await waitFor(() => location.pathname === '/index.html' && !host().hasAttribute('aria-busy')); await tick(200);
      return [parent, [window.history.length, open()]];
    """, media={"(max-width: 720px)": True}) == [[2, False], [3, True]]


def _payload(site: Path, path: str, crumbs: list[dict[str, object]]) -> dict[str, object]:
    payload = json.loads((site / path).read_text(encoding="utf-8"))
    return {**payload, "view": {**payload["view"], "breadcrumbs": crumbs}}


@pytest.mark.parametrize(("case", "expected"), [
    # A scope the index has no row for draws the trail its page's payload gives, with Home for its Book, and an outside
    # crumb as text.
    pytest.param("missing-owner", [["Home", ORIGIN + "index.html"], ["External source", None],
                                   [r"Parent \(V\)", ORIGIN + "roadmap/there/index.html"], ["Result here", None]]),
    # Without roadmap/README.md the index has no roadmap row, and the payload of the roadmap's page names Book, the roadmap
    # and its explorer's page: the book draws Home and the roadmap's name, current on the page itself and a link on a
    # statement's address.
    pytest.param("roadmap-page", [["Home", ORIGIN + "index.html"], ["Roadmap", None]]),
    pytest.param("roadmap-statement", [["Home", ORIGIN + "index.html"], ["Roadmap", ORIGIN + "roadmap/index.html"], ["top", None]]),
    # Without a statement at its top either, render writes no page for the roadmap: the payload gives it no link, and nor
    # does the book.
    pytest.param("roadmap-without-a-page", [["Home", ORIGIN + "index.html"], ["Roadmap", None], ["intro", None]]),
])
def test_fallback_trails_follow_the_payload_and_stay_the_same_when_redrawn(site: Path, case: str, expected: list) -> None:
    parts = {"format": "inline-parts-v1", "source": "Parent $V$", "parts": [["text", "Parent "], ["math", r"\(V\)"]]}
    if case == "missing-owner":
        rows = [scope("roadmap", None), page("here/result", "missing-owner", scope="here", title="Result here",
                                             route="../roadmap/here/index.html#result")]
        files = {"dependencies/chapters/here.json": _payload(site, "dependencies/chapters/here.json", [
            {"label": "Book", "url": "../../roadmap/index.html"}, {"label": "External source", "url": "https://outside.test/book"},
            {"label": "Parent $V$", "reading_title": parts, "url": "../../roadmap/there/index.html"}])}
        where = {"hash": "#result"}
    else:
        statement = "intro" if case == "roadmap-without-a-page" else "top"
        rows = [page(statement, "missing-roadmap", route=f"../roadmap/index.html#{statement}")]
        files = {"dependencies.json": _payload(site, "dependencies.json", [
            {"label": "Book", "url": "index.html" if statement == "intro" else "roadmap/index.html"},
            {"label": "Roadmap", "url": None if statement == "intro" else "roadmap/index.html"}, {"label": "Top level", "url": None}])}
        where = {"page": "roadmap/index.html", "hash": "" if case == "roadmap-page" else f"#{statement}"}
    drawn = book(site, SIZE + "const first = trail(); size(600, 500); size(700, 500); return [first, trail()];",
                 files={"dependencies/index.json": index(*rows), **files}, **where)
    assert drawn == [expected, expected]


#: Each chapter's link opens an address that fails: whether the notice then offers that address as the complete article.
FAILURES = {
    "missing-page": ("../roadmap/gone/index.html", True),
    "missing-folder-with-query": ("../roadmap/gone/?edition=2#main", True),
    "hostile-message": ("../roadmap/broken/index.html", True),
    "graph-data": ("../dependencies.json", False),
    "data-with-query": ("../scopes/topic.json?edition=2#main", False),
    "encoded-data": ("../scopes/topic%2ejson", False),
    "script": ("../javascripts/blueprint-dag.js", False),
}


def test_a_failed_view_offers_only_a_same_origin_page_as_the_complete_article(site: Path) -> None:
    """The router refuses javascript:, credentials and control characters before a request, so the notice only ever hears
    of same-origin http(s) addresses, the page's own data among them, and of an index row's outside address."""
    rows = [scope("roadmap", None), scope("here", "roadmap"),
            *(scope(name, "roadmap", route=route) for name, (route, _) in FAILURES.items()),
            {**scope("outside", "roadmap"), "route": None, "url": "https://other.example.invalid/roadmap/topic/index.html"}]
    message = '<img src=x onerror="window.pwned=1"> <script>bad()</script> $not_math$'
    notices = book(site, r"""
      const roots = [], notice = $('.bp-dag-error');
      window.AutoformMath = {refresh(root) { roots.push(root); return Promise.resolve(true); }};
      const result = {};
      for (const name of args.names) {
        assert.equal(click($(`[data-navigation-node-id="${name}"]`)), true);
        await waitFor(() => !host().hasAttribute('aria-busy')); await tick(50);
        const link = notice.querySelector('a');
        result[name] = link ? [link.href, link.target, link.rel, link.textContent, link.childElementCount] : null;
        assert.equal(notice.hidden, false);
        assert(!roots.some(root => notice.contains(root) || root.contains(notice)), 'the notice is never typeset');
        if (name === 'hostile-message') {
          // A network error's text stays text, in the notice and the announcement.
          assert(notice.textContent.includes(args.message) && $('.bp-dag-sr-only[aria-live]').textContent.includes(args.message));
          assert(!notice.querySelector('img, script, .arithmatex') && !window.pwned);
          // The complete article opens as the browser opens a link, never in place.
          const fetched = net.log.length;
          for (const init of [{}, {ctrlKey: true}, {metaKey: true}, {shiftKey: true}, {altKey: true}, {button: 1}])
            assert.equal(click(link, init), false, JSON.stringify(init));
          assert.equal(net.log.length, fetched);
        }
      }
      return result;
    """.replace("args.names", json.dumps([*FAILURES, "outside"])).replace("args.message", json.dumps(message)),
        files={"dependencies/index.json": index(*rows)},
        before="net.route = href => href.endsWith('/broken/index.html') ? "
               "window.Promise.reject(new window.TypeError(" + json.dumps(message) + ")) : undefined;")
    offered = {name: [ORIGIN + route[3:], "_blank", "noopener", "Open complete article in a new tab", 0]
               for name, (route, safe) in FAILURES.items() if safe}
    assert notices == {**{name: None for name in [*FAILURES, "outside"]}, **offered}


@pytest.mark.parametrize(("record", "map_first"), [
    # A topic opens on the map, as its page's place; what has a reading opens in the reader: a topic that is itself a
    # result, a source text, a module article and a statement. An overview's page shows which: it leaves the stage to the
    # map only when the map is asked for.
    pytest.param({}, True, id="topic"),
    pytest.param({"result": True}, False, id="result-topic"),
    pytest.param({"navigation": "source"}, False, id="source-text"),
    pytest.param({"catalog": "module"}, False, id="module-article"),
    pytest.param({"kind": "node", "scope": "roadmap", "result": True}, False, id="statement"),
])
def test_a_search_result_asks_for_the_map_only_for_a_topic(site: Path, record: dict, map_first: bool) -> None:
    rows = [scope("roadmap", None), scope("here", "roadmap"), scope("shelf", "roadmap", title="Wanted", **record)]
    assert book(site, r"""
      const search = $('.bp-dag-search'); search.focus(); search.value = 'Wanted'; emit(search, 'input');
      await waitFor(() => $('.bp-dag-result')); click($('.bp-dag-result'));
      await waitFor(() => location.pathname === '/roadmap/shelf/index.html' && !host().hasAttribute('aria-busy')); await tick(200);
      return host().classList.contains('bp-dag-overview-only');
    """, files={"dependencies/index.json": index(*rows)}) is map_first
