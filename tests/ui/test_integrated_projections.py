"""The statement list, the search's rows and what the page selects, on a built book's pages with the explorer it ships
(:mod:`tests.explorer_fixture`).

A test that needs an index or a payload the site cannot make serves its own in place of the site's; the page opened is
the ``here`` topic's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.scaffold import scaffold_project
from autoform_cli.ui import graph_pages
from tests.explorer_fixture import ARTICLE, build_site, run_book
from tests.facts_fixture import write_annotated

HERE = "roadmap/here/index.html"
INDEX = "dependencies/index.json"
#: More modules than the 120 the explorer's map was sized for.
MODULES = 121


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("book")
    scaffold_project(project, title="Library")
    blueprint = project / "blueprint"
    roadmap = blueprint / "roadmap"
    (roadmap / "README.md").write_text("# Library\n\nGoal.\n\n## Chapters\n\n- [Here](here/README.md)\n- [There](there/README.md)\n"
                                       "- [Catalogs](catalogs/README.md)\n", encoding="utf-8")
    for topic in ("here", "there"):
        (roadmap / topic).mkdir()
        (roadmap / topic / "README.md").write_text(f"# {topic.title()}\n\nProse.\n", encoding="utf-8")
        for name in ("result", "peer"):
            (roadmap / topic / f"{name}.md").write_text(ARTICLE.format(
                kind="theorem", lean="Demo.main", title=f"{name.title()} {topic}", body="Statement.", uses="None."), encoding="utf-8")
    catalogs = roadmap / "catalogs"
    write_annotated(catalogs / "README.md", "# Existing mathematics\n\nThe module articles describe the existing library.\n")
    write_annotated(blueprint / "sources/ledger.md", "# Declaration ledger\n\nThe modules already in the library.\n")
    for number in range(MODULES):
        progress = "lean: Demo.main\nstatement: formalized\nproof: formalized\n" if number == 0 else ""
        write_annotated(catalogs / f"module-{number:03d}.md", (
            f"---\ncatalog: module\n{progress}---\n\n# Mathematical module {number}\n\n"
            "The mathematical content includes the identity $0 = 0$.\n\n## Sources\n\n- [Declaration ledger](../../sources/ledger.md)\n"))
    write_annotated(catalogs / "note.md", "# A note\n\nA separate narrative, not a declaration or module article.\n")
    write_annotated(catalogs / "source.md", "---\nnavigation: source\n---\n\n# Textbook source\n\nBibliographic provenance.\n")
    coverage = blueprint / "coverage/README.md"
    scope_row = next(line for line in coverage.read_text(encoding="utf-8").splitlines() if line.startswith("| Project scope"))
    coverage.write_text(coverage.read_text(encoding="utf-8").replace(
        scope_row, scope_row + "\n| Modules | INVENTORIED | [Modules](../roadmap/catalogs/README.md) |"), encoding="utf-8")
    return build_site(project)


def row(node_id: str, parent: str | None, *, scope: str | None = None, **extra: object) -> dict[str, object]:
    """An index row: a topic when it is its own *scope* (the default), else a statement on its scope's page."""
    scope = scope or node_id
    route = "../roadmap/index.html" if scope == "roadmap" else f"../roadmap/{scope}/index.html"
    return {"id": node_id, "title": node_id, "kind": "scope" if scope == node_id else "node", "scope": scope, "parent": parent,
            "result": scope != node_id, "status": "planned",
            "route": route + ("" if scope == node_id else "#" + node_id.rsplit("/", 1)[-1]), **extra}


def index(*nodes: dict[str, object], relations: list | None = None) -> dict[str, object]:
    return {"schema": "autoform-dag-search/v1", "nodes": list(nodes), "relations": relations or []}


def payload(site: Path, path: str, nodes: list[dict[str, object]]) -> dict[str, object]:
    """The page's graph payload with *nodes* as its entries."""
    return {**json.loads((site / path).read_text(encoding="utf-8")), "nodes": nodes, "edges": []}


#: The statement list and the search, as the reader uses them.
READ = r"""
const browse = $('.bp-dag-browse > button');
const rows = () => $$('.bp-dag-inventory-row').map(item => item.dataset.inventoryNodeId);
// Shows the statements Browse offers, from its menu or, without source texts, from its one button.
function statements() {
  if (browse.textContent === 'Browse') { click(browse); click($('.bp-dag-browse-popover button')); } else click(browse);
  return rows();
}
// What the search finds for QUERY: each row's id, and the line saying where it lives.
function find(query) {
  const search = $('.bp-dag-search'); search.focus(); search.value = query; emit(search, 'input');
  return $$('.bp-dag-result').map(result => [result.dataset.resultId, (result.querySelector('.bp-dag-result-place') || {}).textContent || '']);
}
async function landed(path) { await waitFor(() => location.pathname === path && !host().hasAttribute('aria-busy')); await tick(200); }
"""


def book(site: Path, body: str, *, page: str = HERE, **options: object) -> object:
    out = run_book(site, page, READ + body, **options)
    return json.loads(out) if out else None


def test_a_topics_list_holds_every_statement_in_it_linked_or_not(site: Path) -> None:
    rows = [row("roadmap", None), row("here", "roadmap"), row("here/T", "here", scope="here"), row("here/A", "here", scope="here"),
            row("here/B", "here", scope="here"), *(row(f"here/P{number}", "here", scope="here") for number in range(6))]
    assert book(site, "return statements();", files={INDEX: index(*rows, relations=[["here/A", "here/B", 1, 0]])}) == [
        "here/T", "here/A", "here/B", *(f"here/P{number}" for number in range(6))]


def _places(current: str) -> list[dict[str, object]]:
    """ATLAS has 93 results titled "Conjecture", and a search showed them as identical rows. These rows put *current*'s
    page, and the statements on it, on the here topic's page, the one the book opens."""
    rows = [row("roadmap", None), row("algebra", "roadmap", title="Algebra"),
            row("algebra/ext", "algebra", title="MathlibExt", navigation="collection"),
            row("algebra/ext/ring", "algebra/ext", title="Ring"), row("algebra/ext/ring/ideal", "algebra/ext/ring", title="Ideal"),
            row("conjecture", "algebra/ext/ring/ideal", scope="algebra/ext/ring/ideal", title="Conjecture"),
            row("lemma", "algebra/ext", scope="algebra/ext", title="Lemma")]
    rename = {current: "here"}
    placed = []
    for node in rows:
        node = {**node, **{key: rename.get(node[key], node[key]) for key in ("id", "parent", "scope") if node.get(key)}}
        if node["scope"] == "here":
            node["route"] = "../roadmap/here/index.html" + ("" if node["id"] == "here" else "#" + node["id"])
        placed.append(node)
    return placed


def test_a_row_says_where_its_result_or_topic_lives(site: Path) -> None:
    found, listed = book(site, r"""
      const found = ['Conjecture', 'Lemma', 'Ring', 'Algebra'].map(query => find(query)[0]);
      $('.bp-dag-search').value = ''; emit($('.bp-dag-search'), 'input');
      statements();
      return [found, $$('.bp-dag-inventory-row').map(item => [item.dataset.inventoryNodeId, item.querySelector('.bp-dag-inventory-meta').textContent])];
    """, files={INDEX: index(*_places("algebra/ext/ring"))})
    # The two nearest topics under the root, and a topic says it is one; a collection's own page is a place for what
    # lives on it.
    assert found == [["conjecture", "Ring › Ideal"], ["lemma", "Algebra › MathlibExt"], ["here", "Section in Algebra"],
                     ["algebra", "Section"]]
    # A list stops at its own page, so its rows name only what lies below.
    assert listed == [["conjecture", "Ideal"]]
    assert book(site, "statements(); return $('.bp-dag-inventory-meta').textContent;",
                files={INDEX: index(*_places("algebra/ext/ring/ideal"))}) == ""


def test_a_list_row_sets_its_summarys_code_and_tex_instead_of_printing_them(site: Path) -> None:
    # Statement lists printed "$u : \mathbb{N} \to \alpha$" and "`mu0`" as typed, after a run-in repeating the title.
    summaries = {"here/periodic": ("Eventually periodic sequences", "Eventually periodic sequences. A sequence "
                                   r"$u : \mathbb{N} \to \alpha$ is eventually periodic, as `mu0` checks, see \(p\)."),
                 # A summary that only shares the title's opening words keeps them.
                 "here/group": ("Group", "Groups act on sets."), "here/lemma": ("Lemma", "Plain prose.")}
    rows = [row("roadmap", None), row("here", "roadmap"),
            *(row(node_id, "here", scope="here", title=title, summary=summary) for node_id, (title, summary) in summaries.items())]
    assert book(site, r"""
      // The rows' formulas wait for the engine, as they do until a row comes into view.
      window.AutoformMath = {refresh() { return Promise.resolve(true); }};
      statements();
      return $$('.bp-result-summary').map(line => line.children.length ?
        Array.from(line.children, part => [part.tagName.toLowerCase(), part.className, part.textContent]) : line.textContent);
    """, files={INDEX: index(*rows)}) == [
        [["span", "", "A sequence "], ["span", "bp-math-pending", r"\(u : \mathbb{N} \to \alpha\)"],
         ["span", "", " is eventually periodic, as "], ["code", "", "mu0"], ["span", "", " checks, see "],
         ["span", "bp-math-pending", r"\(p\)"], ["span", "", "."]],
        [["span", "", "Groups act on sets."]], [["span", "", "Plain prose."]]]


#: The book starts without the index: its first request fails and the one the page makes next is held.
LATE = "net.fail('dependencies/index.json'); net.hold('dependencies/index.json');"


def test_until_the_index_arrives_the_page_offers_only_the_entries_it_lists(site: Path) -> None:
    entries = [{"id": "scope:Atlas", "title": "Entry Atlas", "kind": "scope", "status": "planned"},
               {"id": "scope:Geometry", "title": "Entry Geometry", "kind": "scope", "status": "planned"},
               {"id": "here/result", "title": "Entry F", "kind": "node", "status": "planned"},
               # An overview payload marks the entries its overview leaves out.
               {"id": "page-2", "title": "Entry page 2", "kind": "node", "status": "fully_proved", "map": False}]
    rows = [row("roadmap", None), row("here", "roadmap"), row("Atlas", "here", navigation="source"), row("Geometry", "here"),
            row("here/result", "here", scope="here")]
    assert book(site, r"""
      return [find('entry').map(result => result[0]), $$('.bp-dag-filter-popover input').map(input => input.value)];
    """, files={INDEX: index(*rows), "dependencies/chapters/here.json": payload(site, "dependencies/chapters/here.json", entries)},
        before=LATE) == [["scope:Atlas", "scope:Geometry", "here/result"], ["planned"]]


@pytest.mark.parametrize(("query", "landing"), [
    # Before the index the page's own entries answer the search, and a view's typed id names a record: a topic's view
    # entry its topic, a boundary its scope's entry.
    pytest.param("View Atlas", "/roadmap/there/index.html", id="view-scope"),
    pytest.param("View boundary", "/index.html", id="view-boundary"),
    # An index id that looks typed is still the record's own.
    pytest.param("Typed topic", "/index.html", id="typed-index-id"),
    pytest.param("Typed statement", "/roadmap/here/index.html#peer", id="typed-statement-id"),
])
def test_index_ids_are_the_records_own_and_a_view_id_names_its_record(site: Path, query: str, landing: str) -> None:
    entries = [{"id": "scope:Atlas", "title": "View Atlas", "kind": "scope", "status": "planned"},
               {"id": "boundary:scope:Atlas", "title": "View boundary", "kind": "boundary", "status": "planned"}]
    rows = [row("roadmap", None), row("here", "roadmap"), row("Atlas", "roadmap", title="Atlas topic", route="../roadmap/there/index.html"),
            row("scope:Atlas", "roadmap", title="Typed topic", route="../index.html"),
            row("scope:Geometry", "here", scope="here", title="Typed statement", route="../roadmap/here/index.html#peer")]
    late = query.startswith("View")
    assert book(site, r"""
      const query = QUERY, landing = new URL(LANDING, location.href);
      if (!query.startsWith('View')) await tick(0);
      find(query); click($('.bp-dag-result'));
      net.release('dependencies/index.json');
      await waitFor(() => location.pathname + location.hash === landing.pathname + landing.hash && !host().hasAttribute('aria-busy'));
      return location.pathname + location.hash;
    """.replace("QUERY", json.dumps(query)).replace("LANDING", json.dumps(landing)),
        files={INDEX: index(*rows), "dependencies/chapters/here.json": payload(site, "dependencies/chapters/here.json", entries)},
        before=LATE if late else "") == landing


def test_a_statement_is_selected_from_the_index_whatever_view_its_page_carries(site: Path) -> None:
    # A statement placed directly under the roadmap is published with the project's view, which lists only topics.
    rows = [row("roadmap", None), row("here", "roadmap"), row("top", "roadmap", scope="roadmap", title="Top statement")]
    selected = "return [host().classList.contains('bp-dag-has-selection'), $('.bp-dag-sheet-title').textContent];"
    assert book(site, selected, page="roadmap/index.html", hash="#top", files={INDEX: index(*rows)}) == [True, "Top statement"]
    # A topic is never the selection, even on its own page.
    assert book(site, selected, files={INDEX: index(*rows)})[0] is False
    # Before the index loads, the page's own entries decide.
    early = r"""const state = () => host().classList.contains('bp-dag-has-selection'); return state();"""
    assert book(site, early, hash="#node=here/result", before=LATE) is True
    assert book(site, early, page="roadmap/index.html", hash="#node=top", before=LATE) is False


def test_an_oversized_module_scope_keeps_its_articles_navigable_without_proof_claims(site: Path) -> None:
    published = json.loads((site / graph_pages.SEARCH_INDEX).read_text(encoding="utf-8"))
    context = json.loads((site / "dependencies/chapters/catalogs.json").read_text(encoding="utf-8"))
    records = {node["id"]: node for node in published["nodes"]}
    modules = [f"catalogs/module-{number:03d}" for number in range(MODULES)]
    assert all(records[node_id]["catalog"] == "module" and records[node_id]["result"] is False for node_id in modules)
    assert {records[node_id]["status"] for node_id in modules} == {"inventory_pending", "inventory_checked"}
    for state in ("inventory_pending", "inventory_checked"):
        assert "not a mathematical proof" in context["palette"][state]["meaning"]
    page = (site / "roadmap/catalogs/index.html").read_text(encoding="utf-8")
    assert page.count("The mathematical content includes the identity") == MODULES
    assert page.count('class="bp-reading-source"') == 1 and not (site / "roadmap/catalogs/module-120").exists()
    listed = book(site, r"""
      const shown = [statements(), $('.bp-dag-inventory h2').textContent, $('.bp-dag-inventory-summary').textContent,
        $('.bp-dag-load-more').hidden];
      click($('.bp-dag-load-more'));
      const all = [rows(), $('.bp-dag-inventory-summary').textContent, $('.bp-dag-load-more').hidden,
        document.activeElement.dataset.inventoryNodeId];
      click($$('.bp-dag-inventory-row').at(-1)); await landed('/roadmap/catalogs/index.html');
      const opened = location.hash;
      click($('.bp-dag-browse > button')); click($$('.bp-dag-browse-popover button').find(choice => choice.textContent === 'Source texts'));
      return [shown, all, opened, rows()];
    """, page="roadmap/catalogs/index.html")
    shown, every, opened, sources = listed
    assert shown == [modules[:100], "Statements & articles", f"100 of {MODULES}", False]
    # Paging focuses the first new row; every article stays a real navigation target.
    assert every == [modules, f"{MODULES} articles", True, modules[100]]
    assert opened == "#module-120" and sources == ["catalogs/source"]
