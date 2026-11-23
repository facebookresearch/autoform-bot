"""Titles with formulas and code: the parts the publisher ships for them, and how the book shows them, in its pages with
the explorer it ships (:mod:`tests.explorer_fixture`) and with the pinned MathJax typesetting them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autoform_cli import markdown
from autoform_cli.scaffold import scaffold_project
from autoform_cli.tex_macros import TEX_MACROS
from autoform_cli.ui import dag_viewer, site_index
from autoform_cli.ui.graph_views import GraphView, ViewEdge, ViewNode
from tests.explorer_fixture import ARTICLE, ORIGIN, build_site, run_book
from tests.ui.test_site_mathjax_node import mathjax_package

_TITLE = r"Groups $\mathbb{Z}/n$"
_ENTITY_TITLE = r"Groups &amp; rings $\mathbb{Z}/n$"
_SUMMARY = r"Cyclic **groups** $\ZZ/n$ retain code `$literal$`."
_CODE_TITLE = r"Notation $x$ and `</div><script>window.__titlePwned=1</script>`."


def test_reading_label_separates_real_site_math_from_code_and_preserves_source() -> None:
    assert site_index._reading_label(_TITLE) == {
        "format": "inline-parts-v1", "source": _TITLE,
        "parts": [["text", "Groups "], ["math", r"\(\mathbb{Z}/n\)"]],
    }
    label = site_index._reading_label(_SUMMARY)
    assert label is not None and label["source"] == _SUMMARY
    assert [part for part in label["parts"] if part[0] == "math"] == [["math", r"\(\ZZ/n\)"]]
    assert [part for part in label["parts"] if part[0] == "code"] == [["code", "$literal$"]]
    assert "Cyclic groups " in "".join(part[1] for part in label["parts"] if part[0] == "text")
    code = site_index._reading_label(_CODE_TITLE)
    assert code is not None
    assert [part for part in code["parts"] if part[0] == "code"] == [["code", "</div><script>window.__titlePwned=1</script>"]]
    comparison = site_index._reading_label("Math $a<b>c$")
    assert comparison is not None and comparison["parts"][-1] == ["math", r"\(a<b>c\)"]


@pytest.mark.parametrize("text", [
    "A plain module name", r"Code `$literal$`", "Math $x$ <script>window.__titlePwned=1</script>",
    "Math $x$ [nested link](other.md)", "Math $x$ ![image](image.png)", "Math $x$\n{: .untrusted}",
    r'<div class="arithmatex">\(x\)</div>', r'<span class="arithmatex">\(x\)</span>',
    "$x$ " * 129, "$x$" + "a" * 16_384,
])
def test_plain_unsupported_and_oversized_labels_keep_the_literal_fallback(text: str) -> None:
    assert site_index._reading_label(text) is None


def test_literal_attribute_like_suffix_stays_text_without_becoming_an_attribute() -> None:
    text = "Math $x$ {.untrusted}"
    assert site_index._reading_label(text) == {
        "format": "inline-parts-v1", "source": text,
        "parts": [["text", "Math "], ["math", r"\(x\)"], ["text", " {.untrusted}"]],
    }


@pytest.mark.parametrize(("reference", "visible"), [
    ("&amp;", "&"), ("&#38;", "&"), ("&#x26;", "&"), ("&lt;", "<"),
    ("&#60;", "<"), ("&NotEqualTilde;", "≂̸"), ("&#x1F600;", "😀"),
])
def test_valid_character_references_keep_math_parts_and_exact_title_source(reference: str, visible: str) -> None:
    text = f"Groups {reference} rings $x$"
    assert site_index._reading_label(text) == {
        "format": "inline-parts-v1", "source": text,
        "parts": [["text", f"Groups {visible} rings "], ["math", r"\(x\)"]],
    }


@pytest.mark.parametrize("text", [
    "Math $x$ &unknownReference;", "Math $x$ &123;",
    "Math $x$ <script>&amp;window.__titlePwned=1</script>",
    'Math $x$ <span title="&amp;">unsafe</span>',
    'Math $x$ <img src="&amp;" onerror="window.__titlePwned=1">',
    "Math $x$ [Groups &amp; rings](other.md)", "Math $x$ ![Groups &amp; rings](image.png)",
    "Math $x$\n{: title='&amp;'}", "Math $x$ <!-- &amp; -->",
])
def test_entities_do_not_make_raw_html_links_images_or_attributes_a_label_transport(text: str) -> None:
    assert site_index._reading_label(text) is None


def test_entity_encoded_tags_remain_single_decoded_text_and_code_references_stay_literal() -> None:
    text = r'Math $x$ &lt;script&gt;window.__titlePwned=1&lt;/script&gt; &amp;lt;b&amp;gt;'
    label = site_index._reading_label(text)
    assert label == {
        "format": "inline-parts-v1", "source": text,
        "parts": [["text", "Math "], ["math", r"\(x\)"],
                  ["text", " <script>window.__titlePwned=1</script> &lt;b&gt;"]],
    }
    tree = markdown.render_tree(text, allow_html=False)
    assert tree is not None and all(element.tag not in {"script", "b", "a", "img"} for element in tree.iter())
    literal = r'Math $x$ `&amp; &lt;script&gt; <script>window.__titlePwned=1</script>`'
    code = site_index._reading_label(literal)
    assert code is not None and code["source"] == literal
    assert [part for part in code["parts"] if part[0] == "code"] == [
        ["code", "&amp; &lt;script&gt; <script>window.__titlePwned=1</script>"],
    ]
    encoded_math = site_index._reading_label("Math &#36;not_math&#36; $x$")
    assert encoded_math is not None
    assert encoded_math["parts"] == [["text", "Math $not_math$ "], ["math", r"\(x\)"]]


def _view(title: str) -> GraphView:
    return GraphView(kind="full", title="Reading labels", nodes=(
        ViewNode("a", title, "node", ("a",), (("planned", 1),), status_key="planned"),
        ViewNode("b", "Plain B", "node", ("b",), (("planned", 1),), status_key="planned"),
    ), edges=(ViewEdge("a", "b", 2, 3),))


def test_optional_reading_title_keeps_plain_index_bytes_relations_and_statuses(tmp_path: Path) -> None:
    links = {"a": "a.html", "b": "b.html"}
    plain, marked = tmp_path / "plain.json", tmp_path / "marked.json"
    site_index.write_search_index(plain, _view("Plain A"), context_links=links, include_relations=True)
    assert plain.read_bytes() == (
        b'{"nodes":[{"id":"a","kind":"node","status":"planned","title":"Plain A","url":"a.html"},'
        b'{"id":"b","kind":"node","status":"planned","title":"Plain B","url":"b.html"}],'
        b'"relations":[[0,1,2,3]],"schema":"autoform-dag-search/v1"}\n'
    )
    digests: list[str] = []
    view = _view(_TITLE)
    site_index.write_search_index(marked, view, context_links=links, include_relations=True, sha256_out=digests)
    payload = json.loads(marked.read_bytes())
    assert payload["nodes"][0]["title"] == _TITLE
    assert payload["nodes"][0]["reading_title"] == site_index._reading_label(_TITLE)
    assert "reading_title" not in payload["nodes"][1]
    assert payload["relations"] == [[0, 1, 2, 3]]
    assert [node["status"] for node in payload["nodes"]] == ["planned", "planned"]
    assert digests == [hashlib.sha256(marked.read_bytes()).hexdigest()]
    assert view.edges == (ViewEdge("a", "b", 2, 3),)


def test_publisher_breadcrumb_parts_preserve_plain_bytes_titles_urls_and_math_source(tmp_path: Path) -> None:
    view = GraphView(kind="full", title="Plain trail", nodes=(), edges=())
    empty, plain, marked = (tmp_path / name for name in ("empty.json", "plain.json", "marked.json"))
    dag_viewer.write_payload(empty, view)
    dag_viewer.write_payload(plain, view, breadcrumbs=(("Library", "library.html"), ("Plain trail", None)))
    assert plain.read_bytes() == empty.read_bytes().replace(
        b'"breadcrumbs":[]', b'"breadcrumbs":[{"label":"Library","url":"library.html"},{"label":"Plain trail","url":null}]',
    )
    dag_viewer.write_payload(marked, view, breadcrumbs=((_SUMMARY, "chapter.html"), (_TITLE, None)))
    expected = json.loads(empty.read_bytes())
    expected["view"]["breadcrumbs"] = [
        {"label": _SUMMARY, "url": "chapter.html", "reading_title": site_index._reading_label(_SUMMARY)},
        {"label": _TITLE, "url": None, "reading_title": site_index._reading_label(_TITLE)},
    ]
    assert json.loads(marked.read_bytes()) == expected
    assert view.title == "Plain trail" and view.nodes == () and view.edges == ()


_ESCAPED = (r"foo\_bar", r"\*bold\*", r"\#1", r"a\\b", r"1. Intro to a\_b", r"foo\_bar {#custom}", r"\$a\$")
_ESCAPED_MATH = r"Cost \$5 for $x$"


@pytest.mark.parametrize("title", _ESCAPED)
def test_an_escaped_title_reads_as_its_page_heading(title: str) -> None:
    """Issue #727: the explorer and the map showed `foo\\_bar` where the page's heading reads `foo_bar`."""
    shown = site_index._shown_title(title)
    assert shown == markdown.page_title(f"# {title}") and shown != title


@pytest.mark.parametrize("title", [
    _TITLE, _ESCAPED_MATH, r"$x\_1$ and y\_2", r"\[x\]", r"1. $x$ and a\_b", r"\q", "Plain A", "", r"<!-- a\_b -->",
])
def test_a_title_with_math_without_an_escape_or_showing_nothing_keeps_its_source(title: str) -> None:
    assert site_index._shown_title(title) == title


@pytest.mark.parametrize("title", [*_ESCAPED, r"`code\_x` and a\_b", r"\q", "Plain A"])
def test_the_index_asks_for_parts_only_with_math_so_an_escape_alone_still_gets_none(title: str) -> None:
    """Issue #727 ships the shown title itself; the index still asks for parts only with math."""
    assert site_index._reading_label(title) is None


def test_index_and_breadcrumbs_ship_an_escaped_title_as_its_page_shows_it(tmp_path: Path) -> None:
    """Issue #727. A formula title keeps the source its parts are bound to, and those parts read its escaped dollar;
    a summary that opens with the title opens with the shown one, which the explorer drops as before."""
    titles = (*_ESCAPED, _ESCAPED_MATH)
    view = GraphView(kind="full", title="Escapes", nodes=tuple(
        ViewNode(f"n{at}", title, "node", (f"n{at}",), (("planned", 1),), status_key="planned",
                 summary=f"{title}: what it says." if at == 0 else None)
        for at, title in enumerate(titles)
    ), edges=())
    index = tmp_path / "index.json"
    site_index.write_search_index(index, view, context_links={})
    nodes = json.loads(index.read_bytes())["nodes"]
    assert [node["title"] for node in nodes] == [*(markdown.page_title(f"# {title}") for title in _ESCAPED), _ESCAPED_MATH]
    assert nodes[0]["summary"] == "foo_bar: what it says."
    assert [node.get("reading_title") for node in nodes[:-1]] == [None] * len(_ESCAPED)
    assert nodes[-1]["reading_title"] == site_index._reading_label(_ESCAPED_MATH)
    assert nodes[-1]["reading_title"]["parts"][0] == ["text", "Cost $5 for "]
    payload = tmp_path / "payload.json"
    dag_viewer.write_payload(payload, view, breadcrumbs=((r"Chapter foo\_bar", "chapter.html"), (_ESCAPED_MATH, None)))
    data = json.loads(payload.read_bytes())
    assert data["view"]["breadcrumbs"] == [
        {"label": "Chapter foo_bar", "url": "chapter.html"},
        {"label": _ESCAPED_MATH, "url": None, "reading_title": site_index._reading_label(_ESCAPED_MATH)},
    ]
    assert [node["title"] for node in data["nodes"]] == [node["title"] for node in nodes]
    assert data["nodes"][0]["summary"] == "foo_bar: what it says."


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A book whose titles carry formulas, a project macro and code: the chapter's own and its statements'."""
    project = tmp_path_factory.mktemp("book")
    scaffold_project(project, title="Library")
    blueprint = project / "blueprint"
    roadmap = blueprint / "roadmap"
    (blueprint / TEX_MACROS).write_text(json.dumps({"ZZ": r"\mathbb{Z}"}), encoding="utf-8")
    (roadmap / "README.md").write_text("# Library\n\nGoal.\n\n## Chapters\n\n- [Groups](chapter/README.md)\n"
                                       "- [Other](other-topic/README.md)\n", encoding="utf-8")
    for topic, title in (("chapter", _TITLE), ("other-topic", r"Other $\ZZ$")):
        (roadmap / topic).mkdir()
        (roadmap / topic / "README.md").write_text(f"# {title}\n\nProse.\n", encoding="utf-8")
    statements = {"chapter/F": ("Focal result", r"A sequence $\ZZ/n$ keeps `$literal$` as code."), "chapter/A": (_ENTITY_TITLE, "S."),
                  "chapter/B": (r"Custom $\ZZ/n$", "S."), "chapter/C": (_SUMMARY, "S."), "chapter/D": (_CODE_TITLE, "S."),
                  "other-topic/X": ("X", "S.")}
    for node_id, (title, body) in statements.items():
        (roadmap / f"{node_id}.md").write_text(ARTICLE.format(kind="theorem", lean="Demo.main", title=title, body=body,
                                                              uses="None."), encoding="utf-8")
    return build_site(project)


def _see_also(site: Path, source: str, target: str) -> dict[str, object]:
    """The site's wiki-link layer, bound to its index, with one article linking another, so the source's page ends with
    "See also"."""
    layer = json.loads((site / "dependencies/wiki-links.json").read_text(encoding="utf-8"))
    ids = [node["id"] for node in json.loads((site / "dependencies/index.json").read_text(encoding="utf-8"))["nodes"]]
    return {**layer, "availability": "complete", "unavailable_sources": [], "references": [[ids.index(source), ids.index(target)]]}


def _rows(*titles: tuple[str, object]) -> dict[str, object]:
    """An index of chapters with these titles and reading parts, the page's own topic among them."""
    nodes = [{"id": "roadmap", "title": "Library", "kind": "scope", "scope": "roadmap", "result": False, "status": "planned",
              "route": "../roadmap/index.html"}]
    for at, (title, label) in enumerate(titles):
        node_id = "chapter" if at == 0 else f"t{at}"
        nodes.append({"id": node_id, "title": title, "kind": "scope", "scope": node_id, "parent": "roadmap", "result": False,
                      "status": "planned", "route": f"../roadmap/{node_id}/index.html",
                      **({"reading_title": label} if label is not None else {})})
    return {"schema": "autoform-dag-search/v1", "nodes": nodes, "relations": []}


#: The labels the book shows a title in: Contents, the trail, See also and a list row's summary.
LABELS = r"""
const outline = $('[data-navigation-node-id="chapter"]'), ancestor = $$('.bp-dag-breadcrumb a').find(link => link.href.endsWith('/chapter/index.html'));
const current = $('.bp-dag-breadcrumb [aria-current="page"]'), related = $('.bp-reading-wiki-list a');
"""


def book(site: Path, body: str, *, page: str = "roadmap/chapter/index.html", **options: object) -> object:
    out = run_book(site, page, body, **options)
    return json.loads(out) if out else None


def test_the_books_labels_mark_the_formulas_inside_its_links_and_keep_code_as_typed(site: Path) -> None:
    assert book(site, LABELS + r"""
      const marked = node => node.querySelectorAll('.arithmatex').length;
      // Formulas are marked inside the links and the current crumb, never the controls themselves, which keep their
      // addresses; code and markup in a title stay text.
      assert(![outline, ancestor, related, current].some(node => node.classList.contains('arithmatex')));
      assert(!$('.bp-dag-viewer script, .bp-dag-viewer img, .bp-dag-viewer iframe') && window.__titlePwned === undefined);
      return [[outline.href, marked(outline)], [related.href, related.textContent, marked(related)], [ancestor.href, marked(ancestor)],
        [current.textContent, marked(current), current.querySelector('code').textContent, $$('a, button, [tabindex]', current).length,
         current.hasAttribute('tabindex')]];
    """, hash="#C", files={"dependencies/wiki-links.json": _see_also(site, "chapter", "chapter/A")}) == [
        [ORIGIN + "roadmap/chapter/index.html", 1],
        # A valid title entity is decoded once.
        [ORIGIN + "roadmap/chapter/index.html#A", r"Groups & rings \(\mathbb{Z}/n\)", 1],
        [ORIGIN + "roadmap/chapter/index.html", 1],
        [r"Cyclic groups \(\ZZ/n\) retain code $literal$.", 1, "$literal$", 0, False]]


def test_entity_spelled_tags_and_code_stay_text_and_a_title_with_raw_tags_stays_literal(site: Path) -> None:
    texts = [r"Math $x$ &lt;script&gt;window.__titlePwned=1&lt;/script&gt; &amp;lt;b&amp;gt;",
             r"Math $x$ `&amp; &lt;script&gt; <script>window.__titlePwned=1</script>`",
             r"Math $x$ <script>&amp;window.__titlePwned=1</script>"]
    shown = book(site, r"""
      return $$('.bp-dag-navigation-link').filter(link => link.dataset.navigationNodeId !== 'chapter').map(link =>
        [link.textContent, $$('.arithmatex', link).length, $$('code', link).map(code => code.textContent),
         $$('script, img, a, b, iframe', link).length]);
    """, files={"dependencies/index.json": _rows(("Here", None), *((text, site_index._reading_label(text)) for text in texts))})
    assert shown == [
        [r"Math \(x\) <script>window.__titlePwned=1</script> &lt;b&gt;", 1, [], 0],
        [r"Math \(x\) &amp; &lt;script&gt; <script>window.__titlePwned=1</script>", 1,
         ["&amp; &lt;script&gt; <script>window.__titlePwned=1</script>"], 0],
        # The publisher gives raw HTML no parts, so it reads as typed.
        [texts[2], 0, [], 0]]


def test_invalid_reading_parts_leave_the_title_as_typed(site: Path) -> None:
    text = "Groups $x$"
    label = {"format": "inline-parts-v1", "source": text, "parts": [["math", r"\(x\)"]]}
    invalid = [{**label, "format": "wrong"}, {**label, "source": "different"}, {**label, "parts": []},
               {**label, "parts": [["text", "not the source"]]}, {**label, "parts": [["script", "alert(1)"]]},
               {**label, "parts": [["math", "<script>bad</script>"]]}, {**label, "parts": [["math", r"\(x\)"]] * 257},
               {**label, "parts": [["math", r"\(" + "x" * 32769 + r"\)"]]}]
    # The page's own topic carries one too, so the reader's way back to it keeps its arrow before the title as typed.
    shown = book(site, r"""
      const around = $('.bp-reading-around');
      return [$$('.bp-dag-navigation-link').filter(link => link.dataset.navigationNodeId !== 'chapter/statement')
        .map(link => [link.textContent, link.children.length]), [around.textContent, around.children.length]];
    """, hash="#statement", files={"dependencies/index.json": {**(rows := _rows((text, invalid[0]), *((text, value) for value in invalid[1:]))),
                                   "nodes": [*rows["nodes"], {"id": "chapter/statement", "title": "S", "kind": "node", "scope": "chapter",
                                                              "parent": "chapter", "result": True, "status": "planned",
                                                              "route": "../roadmap/chapter/index.html#statement"}]}})
    assert shown == [[[text, 0]] * len(invalid), ["← " + text, 0]]


def test_a_fallback_trail_keeps_the_parts_its_payload_publishes(site: Path, tmp_path: Path) -> None:
    published = tmp_path / "payload.json"
    dag_viewer.write_payload(published, GraphView(kind="full", title="Fallback", nodes=(), edges=()),
                             breadcrumbs=(("Book", "../../roadmap/index.html"), (_SUMMARY, "../../roadmap/other-topic/index.html"),
                                          (_TITLE, None)))
    payload = json.loads((site / "dependencies/chapters/chapter.json").read_text(encoding="utf-8"))
    payload["view"]["breadcrumbs"] = json.loads(published.read_text(encoding="utf-8"))["view"]["breadcrumbs"]
    # Without the page's row in the index, the trail is the payload's.
    rows = _rows(("Here", None))
    rows["nodes"] = rows["nodes"][:1]
    assert book(site, r"""
      const link = $$('.bp-dag-breadcrumb a')[1], current = $('.bp-dag-breadcrumb [aria-current="page"]');
      return [link.href, $$('.arithmatex', link).length, link.querySelector('code').textContent, $$('.arithmatex', current).length];
    """, files={"dependencies/index.json": rows, "dependencies/chapters/chapter.json": payload}) == [
        ORIGIN + "roadmap/other-topic/index.html", 1, "$literal$", 1]


@pytest.mark.parametrize(("title", "parts", "shown"), [
    # Parts for the same title reduce its formula; without them, only \( \) formulas are, so a literal $ stays.
    pytest.param(_TITLE, True, "Groups ℤ/n", id="parts"),
    pytest.param(r"Groups \(\mathbb{Z}/n\) at $5", False, "Groups ℤ/n at $5", id="no-parts"),
    pytest.param(r"Spaces $L^p$", True, "Spaces Lᵖ", id="superscript"),
])
def test_the_tab_title_reads_a_statements_formula_as_text(site: Path, title: str, parts: bool, shown: str) -> None:
    rows = _rows(("Here", None))
    rows["nodes"].append({"id": "chapter/S", "title": title, "kind": "node", "scope": "chapter", "parent": "chapter", "result": True,
                          "status": "planned", "route": "../roadmap/chapter/index.html#S",
                          **({"reading_title": site_index._reading_label(title)} if parts else {})})
    assert book(site, "return document.title;", hash="#S", files={"dependencies/index.json": rows}) == shown


#: The site's own javascripts/mathjax.js and the pinned engine, started as a page loads them (as in
#: test_site_mathjax_node.py's harness): MathJax's browser adaptor on the page's DOM, its real menu component loaded.
ENGINE = r"""
global.window = window; global.document = document; global.localStorage = window.localStorage;
for (const name of ['HTMLElement', 'Event', 'MouseEvent']) global[name] = window[name];
document.documentElement.style.fontSize = '16px'; document.body.style.fontSize = '16px';
Object.defineProperty(globalThis, 'MathJax', {configurable: true, get: () => window.MathJax, set: value => { window.MathJax = value; }});
(0, eval)(fs.readFileSync(path.join(site, 'javascripts/mathjax.js'), 'utf8'));
// Declared with var, so the test body, which this runs ahead of, sees them.
var documents = [], configuration = window.MathJax;
configuration.loader.load.push('input/tex', 'input/mml', 'output/chtml', 'a11y/assistive-mml', 'ui/menu');
configuration.startup.document = document;
configuration.startup.ready = () => {
  MathJax.startup.useAdaptor('browserAdaptor', true);
  const made = MathJax._.mathjax.mathjax.document;
  MathJax._.mathjax.mathjax.document = function (root, options) { const doc = made.call(this, root, options); documents.push(doc); return doc; };
  MathJax.startup.defaultReady();
};
var engine = require(path.join(args.mathjax, 'es5', 'node-main.js')).init({}).then(() => MathJax.startup.promise);
"""


def test_the_pinned_mathjax_typesets_the_books_labels_with_project_macros_and_no_menus(site: Path) -> None:
    mathjax = mathjax_package()
    report = book(site, LABELS.replace("const outline", "await engine; await waitFor(() => host() && host().getAttribute('data-dag-ready') === 'true' && !host().hasAttribute('aria-busy') && $('.bp-dag-search'), 'the book never committed');\nconst outline") + r"""
      const links = [outline, ancestor, related], hrefs = links.map(link => link.href);
      outline.focus();
      // The statement list sets its rows' formulas as they come into view, here all at once.
      click($('.bp-dag-browse > button'));
      const summary = $$('.bp-dag-inventory-row').find(item => item.dataset.inventoryNodeId === 'chapter/F').querySelector('.bp-result-summary');
      const labels = [outline, related, summary, ancestor, current];
      await waitFor(() => labels.every(label => label.querySelector('mjx-container')), 'the book never typeset its labels');
      // Typesetting keeps the links, their addresses and the focus, and gives no formula a menu or a tab stop.
      assert(links.every((link, at) => link.isConnected && link.href === hrefs[at]) && document.activeElement === outline);
      assert(!$('code mjx-container'));
      return {typeset: labels.map(label => Boolean(label.querySelector('mjx-container'))),
        controls: labels.map(label => label.querySelectorAll('[tabindex], [role="application"]').length),
        math: documents.flatMap(doc => Array.from(doc.math, item => ({tex: item.math, mml: MathJax.startup.toMML(item.root),
          menu: doc.options.enableMenu, label: Boolean(item.start.node.parentElement && item.start.node.parentElement.closest(
            '.bp-dag-breadcrumb, .bp-dag-navigation-inner, .bp-reading-wiki-list, .bp-dag-inventory-list'))}))),
        version: MathJax.version};
    """, hash="#C", before=ENGINE, ready=False, args={"mathjax": str(mathjax)}, timeout=120,
        files={"dependencies/wiki-links.json": _see_also(site, "chapter", "chapter/A")})
    assert report["version"] == "3.2.2"
    assert report["typeset"] == [True] * 5 and report["controls"] == [0] * 5
    math = report["math"]
    assert any(item["tex"] == r"\mathbb{Z}/n" for item in math)
    # The project's macro is defined where the book typesets.
    custom = [item for item in math if item["tex"] == r"\ZZ/n"]
    assert custom and all("merror" not in item["mml"] and 'mathcolor="red"' not in item["mml"] for item in custom)
    assert not any(item["tex"] in {"literal", "$literal$"} for item in math)
    # A formula in a label is part of a control, so it takes no menu; the article's own formulas keep theirs.
    labels = [item for item in math if item["label"]]
    assert {item["tex"] for item in labels} >= {r"\mathbb{Z}/n", r"\ZZ/n"} and all(item["menu"] is False for item in labels)
