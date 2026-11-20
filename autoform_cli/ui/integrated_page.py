"""Each final page in the explorer's frame: its first paint, and the reading MkDocs converts, in which
render finds the child indexes the book hides and the overviews it leaves to the map."""

from __future__ import annotations

import html
import posixpath
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..graph import Graph
from ..markdown import relative_link, rendered_visible_text, site_converter
from . import dag_viewer, graph_pages, graph_views, project_map
from .graph_views import FLATTENED_ROLES
from .markdown_html import _as_published, _protect_markdown_in_html, _PublishedBodyLinks
from .site_files import DAG_SCRIPT, DAG_STYLE, MAP_SCRIPT, MAP_STYLE
from .titles import _file_title, _first_h1, _header_title, _label_html, _plain_title


def _source_scopes(graph: Graph) -> frozenset[str]:
    """The topics whose Browse offers source texts, as blueprint-dag.js sourceNodes finds them: each source track and
    the topics above it, and ``roadmap``, which counts the whole project, when there is a source track at all."""
    scopes: set[str] = set()
    for node_id, node in graph.nodes.items():
        current = node_id if node.navigation == "source" else None
        while current in graph.nodes and current not in scopes:
            scopes.add(current)
            current = graph.nodes[current].parent
    return frozenset(scopes | {"roadmap"}) if scopes else frozenset()


def _first_paint(
    graph: Graph, targets: Mapping[str, tuple[Path, str]], *, page: Path, home: Path, scope: str, owner: str | None,
    source_scopes: frozenset[str], cards: Sequence[str] = (), roadmap: Path | None = None,
) -> dict[str, object]:
    """What the frame of *page*'s first paint shows before the book starts: its head's trail, the reader's "← topic"
    line and whether Browse offers source texts, as the book's routeTrail, renderReaderContext and syncControls give
    them at its first commit (blueprint-dag.js).

    The trail is Home, then the topics above the page's own, each linking its page, the page's own topic, and the
    title of a page in a topic after its topic's. Without roadmap/README.md a page on the roadmap still names the
    roadmap after Home, as one with it does, linking *roadmap*, the page render writes for it, wherever it would link
    the roadmap's own page. A page in a topic says so above the reading; a topic's page says so
    only while its address names one of its statements. Browse offers source texts where the page's topic is in
    *source_scopes* (:func:`_source_scopes`).

    *cards* are the statements whose cards the page holds. For a page with any, the frame also carries the trail the
    book draws on an address naming one of them: Home and the topics, each linking its page, then each card's title.
    """
    def href(node_id: str) -> str | None:
        if node_id not in targets:
            return None
        target, anchor = targets[node_id]
        return _as_published(relative_link(target, page, ".html")) + (f"#{anchor}" if anchor else "")

    def semantic_parent(node_id: str) -> str | None:
        parent, seen = graph.nodes[node_id].parent, set()
        while parent in graph.nodes and graph.nodes[parent].navigation in FLATTENED_ROLES and parent not in seen:
            seen.add(parent)
            parent = graph.nodes[parent].parent
        return parent if parent in graph.nodes else None

    def topics(statement: bool) -> list[tuple[str, str | None]]:
        # Only the page's own topic, on its own page, is not a link, and an address naming a statement links it too.
        crumbs: list[tuple[str, str | None]] = []
        current, seen = scope if scope in graph.nodes else None, set()
        while current is not None and current not in seen:
            seen.add(current)
            node = graph.nodes[current]
            if (current == scope and node.navigation == "collection") or node.navigation not in FLATTENED_ROLES:
                own_page = current == scope and own == scope and not statement
                crumbs.insert(0, (_label_html(node.title), None if own_page else href(current)))
            current = semantic_parent(current)
        if scope == "roadmap" and scope not in graph.nodes:
            # Without roadmap/README.md the roadmap is no node, and the book names it from the page's payload.
            own_page = own == scope and not statement
            link = None if own_page or roadmap is None else _as_published(relative_link(roadmap, page, ".html"))
            crumbs.append((_label_html(graph_views.group_title(graph, "roadmap")), link))
        return crumbs

    sources = scope in source_scopes
    own = owner if owner in graph.nodes else scope
    home_crumb = ("Home", _as_published(relative_link(home, page, ".html")))
    first: dict[str, object] = {"sources": sources}
    if cards:
        first["statements"] = (
            (home_crumb, *topics(True)), tuple((targets[card][1], _label_html(graph.nodes[card].title)) for card in cards),
        )
    if page == home:
        return {"trail": (("Home", None),), **first}
    crumbs = topics(False)
    if own != scope:
        crumbs.append((_label_html(graph.nodes[own].title), None))
    first["trail"] = (home_crumb, *crumbs)
    if scope in graph.nodes and (link := href(scope)) is not None:
        first["context"] = (_label_html(graph.nodes[scope].title), link)
        first["context_on_target"] = own == scope
    return first


#: What is never a child index (:func:`_child_indexes`), in a heading or a list.
_OVERVIEW_PROTECTED_TAGS = frozenset(
    {"math", "mjx-container", "code", "pre", "table", "blockquote", "img", "svg", "cite", "sup"}
)
_OVERVIEW_PROTECTED_CLASSES = frozenset({"arithmatex", "bp-thmwrapper", "bp-source-link", "citation"})
_OVERVIEW_PROTECTED_CONTEXT = frozenset(
    {"bp-node", "bp-thmwrapper", "bp-proof", "bp-lean-source", "footnote", "references"}
)
_OVERVIEW_PROTECTED_ROLES = ("doc-bibliography", "doc-endnotes")
#: The publisher's anchors for a section of sources, proof or notation, under which nothing is a child index.
_OVERVIEW_PROTECTED_ANCHOR = re.compile(
    r"^(?:sources?|references?|bibliograph(?:y|ies)|proof|statement|notation|depends-on)(?:-|$)", re.IGNORECASE
)
_OVERVIEW_NAVIGATION = ("collection", "transparent")
_VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
#: What the book removes from a reading before it reads it (copyFragment).
_READER_REMOVES = frozenset({"script", "iframe", "object", "embed", "base", "link", "style"})
#: What JavaScript's trim() takes off text. str.strip() also takes U+001C to U+001F and U+0085, and leaves U+FEFF.
_TRIMMED = (
    "\t\n\v\f\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


class _Element:
    """An element of a converted reading: its tag, attributes and children, elements and text."""

    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: Iterable[tuple[str, str | None]]) -> None:
        self.tag, self.attrs = tag, {name: value or "" for name, value in attrs}
        self.children: list[_Element | str] = []

    def elements(self) -> list[_Element]:
        return [child for child in self.children if isinstance(child, _Element)]

    def text(self, without: _Element | None = None) -> str:
        return "".join(
            child if isinstance(child, str) else "" if child is without else child.text(without)
            for child in self.children
        )

    def descendants(self) -> Iterator[_Element]:
        for child in self.elements():
            yield child
            yield from child.descendants()

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def level(self) -> int:
        return int(self.tag[1]) if re.fullmatch(r"h[1-6]", self.tag) else 0

    def protected(self) -> bool:
        """Whether something inside the element is content the book never hides as navigation."""
        return any(
            node.tag in _OVERVIEW_PROTECTED_TAGS or node.classes() & _OVERVIEW_PROTECTED_CLASSES
            or "data-cites" in node.attrs
            for node in self.descendants()
        )

    def mathematical(self) -> bool:
        """Whether the element holds a formula, typeset or as TeX."""
        return bool(re.search(r"[$\\]", self.text())) or any(
            node.tag in ("math", "mjx-container") or "arithmatex" in node.classes() for node in self.descendants()
        )


class _Reading(HTMLParser):
    """A converted reading as a tree of :class:`_Element`, with its comments left out."""

    def __init__(self, converted: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("div", ())
        self._open = [self.root]
        self.feed(converted)
        self.close()

    def handle_starttag(self, tag, attrs):
        element = _Element(tag, attrs)
        if tag not in _READER_REMOVES:
            self._open[-1].children.append(element)
        if tag not in _VOID_ELEMENTS:
            self._open.append(element)

    def handle_startendtag(self, tag, attrs):
        if tag not in _READER_REMOVES:
            self._open[-1].children.append(_Element(tag, attrs))

    def handle_endtag(self, tag):
        for depth in range(len(self._open) - 1, 0, -1):
            if self._open[depth].tag == tag:
                del self._open[depth:]
                return

    def handle_data(self, data):
        self._open[-1].children.append(data)


@lru_cache(maxsize=1)
def _reading_converter():
    """The site's converter, with the heading IDs and link destinations the final page gets."""
    converter = site_converter()
    converter.treeprocessors.register(_PublishedBodyLinks(converter), "published-body-links", -1)
    return converter


def _overview_topic(node_id: str, records: Mapping[str, Mapping[str, object]]) -> bool:
    """Whether an index row is a topic with a page of its own, as the book's reader read one."""
    record = records.get(node_id)
    return bool(record) and record.get("kind") == "scope" and record.get("scope") == node_id and \
        record.get("result") is not True and record.get("navigation") not in (*_OVERVIEW_NAVIGATION, "source")


def _published_place(href: str, base: str) -> str:
    """A site-relative link's target, as a path from the site's root; *base* is the linking file's directory."""
    return posixpath.normpath(posixpath.join(base, unquote(href)))


def _overview_routes(records: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    """Each index route without a fragment, as a path from the site's root, to the last row that has it, as the
    book's route map keeps them."""
    base = posixpath.dirname(graph_pages.SEARCH_INDEX)
    routes: dict[str, str] = {}
    for node_id, record in records.items():
        route = record.get("route")
        if isinstance(route, str) and route and "#" not in route:
            routes[_published_place(route, base)] = node_id
    return routes


#: A list item that starts with a link, in Markdown, in a quote too, or as HTML. A page that holds a statement may
#: have a child index only where it has one, so the others are not converted again to look.
_LINKED_ITEM = re.compile(
    r"^(?:[ \t]*>)*[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+\[|<li\b[^>]*>\s*(?:<p\b[^>]*>\s*)?<a\b",
    re.IGNORECASE | re.MULTILINE,
)


def _converted_reading(text: str) -> _Element:
    """The reading MkDocs converts from *text*, a page's Markdown, its frontmatter left out, inside the two
    markdown="1" blocks :func:`_integrated_page` publishes it in. There the HTML render already wrote for a page, as
    for one that holds a < (:func:`_protect_narrative_markdown`), is read as elements, and its headings get IDs."""
    lines = text.splitlines(keepends=True)
    end = 0
    if lines and lines[0].strip() == "---":
        end = next((index + 1 for index in range(1, len(lines)) if lines[index].strip() == "---"), 0)
    converter = _reading_converter()
    converter.reset()
    converter.treeprocessors["published-body-links"].validation_targets = []
    try:
        converted = converter.convert('<div markdown="1">\n<div markdown="1">\n\n' + "".join(lines[end:]) + "\n\n</div>\n\n</div>\n")
    except Exception:
        _reading_converter.cache_clear()
        raise
    reading = _Reading(converted).root
    # The inner block, unless something on the page closed the blocks early.
    for _ in range(2):
        blocks = reading.elements()
        if len(blocks) != 1 or blocks[0].tag != "div":
            break
        reading = blocks[0]
    return reading


def _child_indexes(
    reading: _Element, *, page: Path, destination: Path, owner: str,
    records: Mapping[str, Mapping[str, object]], routes: Mapping[str, str],
) -> list[tuple[_Element, list[_Element]]]:
    """Each section of *reading*, *page*'s, at any depth, that only lists the child topics of its topic *owner*: its
    heading and the lists under it, which the book hides, since Contents lists those topics. *records* are the index's
    rows and *routes* is :func:`_overview_routes`.

    It is the test the book's reader made once the index arrived: a heading whose section, up to the next heading of
    its level or above, holds only lists whose every item is a link to a direct child topic, named as that topic,
    outside sources, proofs, notation and statements. Every direct child counts, where the book counted only those its
    Contents showed, and only a relative link counts, since render cannot know where the site is served.
    """
    index = graph_pages.SEARCH_INDEX
    place = _published_place(_as_published(relative_link(page, destination / index, ".html")), posixpath.dirname(index))
    base = posixpath.dirname(place)

    def child_topic(node_id: str) -> bool:
        parent, seen = records[node_id].get("parent"), set()
        while parent in records and records[parent].get("navigation") in _OVERVIEW_NAVIGATION and parent not in seen:
            seen.add(parent)
            parent = records[parent].get("parent")
        return parent == owner

    def protected_section(siblings: list[_Element], position: int) -> bool:
        heading = siblings[position]
        if heading.protected() or re.search(r"[$\\]", heading.text()):
            return True
        bound = heading.level()
        for previous in reversed(siblings[: position + 1]):
            if 0 < previous.level() <= bound:
                if _OVERVIEW_PROTECTED_ANCHOR.match(previous.attrs.get("id", "")) or previous.mathematical():
                    return True
                bound = previous.level() - 1
        return False

    def child_index(item: _Element) -> bool:
        inner = item.elements()
        holder = inner[0] if len(inner) == 1 and inner[0].tag == "p" else item
        if item.tag != "li" or len(holder.elements()) != 1 or holder.elements()[0].tag != "a":
            return False
        anchor, href = holder.elements()[0], holder.elements()[0].attrs.get("href", "")
        try:
            parts = urlsplit(href)
        except ValueError:
            return False
        # A link with a fragment or a query is to part of a page, and one with a scheme or a host is away from the
        # site; one from the site's root reaches no route either, since render cannot know where the site is served.
        away = parts.scheme or parts.netloc or parts.path.startswith("/")
        node_id = None if away or "#" in href or "?" in href else routes.get(_published_place(href, base))
        return node_id is not None and _overview_topic(node_id, records) and child_topic(node_id) and \
            anchor.text().strip(_TRIMMED) == records[node_id].get("title") and \
            not item.text(without=anchor).strip(_TRIMMED)

    def pure_list(element: _Element) -> bool:
        return element.tag in ("ul", "ol") and "hidden" not in element.attrs and bool(element.elements()) and \
            not element.protected() and all(child_index(item) for item in element.elements())

    indexes: list[tuple[_Element, list[_Element]]] = []

    def visit(parent: _Element) -> None:
        siblings = parent.elements()
        for position, heading in enumerate(siblings):
            # Nothing in a statement, a proof, Lean, a footnote or sources is a child index.
            if heading.classes() & _OVERVIEW_PROTECTED_CONTEXT or heading.attrs.get("role") in _OVERVIEW_PROTECTED_ROLES:
                continue
            visit(heading)
            if heading.level() < 2 or "hidden" in heading.attrs or protected_section(siblings, position):
                continue
            block = []
            for following in siblings[position + 1:]:
                if 0 < following.level() <= heading.level():
                    break
                block.append(following)
            if block and all(pure_list(following) for following in block):
                indexes.append((heading, block))

    visit(reading)
    return indexes


def _reader_marks(
    text: str, *, page: Path, destination: Path, owner: str | None, scope: str,
    records: Mapping[str, Mapping[str, object]], routes: Mapping[str, str], statements: bool = False,
) -> tuple[tuple[tuple[str, int], ...], bool]:
    """What *page*'s reading, whose Markdown MkDocs converts from *text*, gives its frame and the book: its child
    indexes (:func:`_child_indexes`), each as its heading's ID and the number of lists under it, and whether it only
    lists its topic's child topics, an overview, for which the book leaves the stage to the map. *records* are the
    index's rows and *routes* is :func:`_overview_routes`.

    Only the page of the topic its route shows has either. An overview is what the book's reader found once the index
    arrived and the child indexes were hidden: an H1 with the topic's title as plain text, and nothing else at the top
    of the reading. A page that holds a statement (*statements*) is never one. A heading is marked by its ID, so one
    without an ID, with one that another element of the reading has too, or with a space in it, which no valid ID has
    and the book's list of them would split, stays shown.
    """
    if owner is None or owner != scope or not _overview_topic(owner, records):
        return (), False
    if statements and not _LINKED_ITEM.search(text):
        return (), False
    reading = _converted_reading(text)
    indexes = _child_indexes(reading, page=page, destination=destination, owner=owner, records=records, routes=routes)
    named = [element.attrs["id"] for element in reading.descendants() if element.attrs.get("id")]
    marks = tuple(
        (heading.attrs["id"], len(block)) for heading, block in indexes
        if named.count(heading.attrs.get("id", "")) == 1 and not re.search(r"[ \t\n\f\r]", heading.attrs["id"])
    )
    if statements or any(isinstance(child, str) and child.strip(_TRIMMED) for child in reading.children):
        return marks, False
    hidden = {id(element) for heading, block in indexes for element in (heading, *block)}
    title = records[owner].get("title")
    shown = [element for element in reading.elements() if id(element) not in hidden]
    return marks, len(shown) == 1 and shown[0].tag == "h1" and set(shown[0].attrs) <= {"id"} \
        and not shown[0].elements() and not re.search(r"[$\\]", shown[0].text()) \
        and shown[0].text().strip(_TRIMMED) == title


def _integrated_page(
    text: str, *, page: Path, destination: Path, payload: Path, scope: str, owner: str | None = None,
    prepared: bool = False, tracks_lean: bool = True,
    trail: Sequence[tuple[str, str | None]] = (), context: tuple[str, str] | None = None,
    context_on_target: bool = False, sources: bool = False, overview: str | None = None,
    statements: tuple[Sequence[tuple[str, str | None]], Sequence[tuple[str, str]]] | None = None, topic: bool = False,
    map_path: Sequence[tuple[str, str]] = (), map_path_for: str | None = None,
    indexes: Sequence[tuple[str, int]] = (),
) -> str:
    """Keep a complete published page readable before moving it into the shared UI.

    The header and semantic reading content are preserved. Affected Markdown
    is parsed before adding containers, so literal code, formulas, statement
    IDs, citations and shown-as-typed boxes retain their rendered meaning.

    The page opens with the book's frame for its first paint (*trail*,
    *context*, *sources* and *statements*, from :func:`_first_paint`), and its reading
    starts with the script that moves it there, so the first paint is the
    book, not Material's page. On a phone the script drops the frame, and
    Material's page stays until the book opens.

    *overview*, the title's label HTML, marks a page whose reading only lists its topic's child topics
    (:func:`_reader_marks`): its frame and the book fold the reader to a handle with that title, and name the page
    to a screen reader with its plain title while they do.

    *indexes*, from :func:`_reader_marks`, are the reading's child indexes, each its heading's ID with the number of
    lists under it. The frame hides them from the first paint, and the book, which finds them from the reading's
    ``data-reader-indexes``, once it starts.

    *topic* marks a page whose place, its owner or else its scope, the book takes for a topic on an address naming no
    statement. With the cards' result marks, it gives the book what its first commit reads from the search index.

    *map_path*, the map's chain of parts down to the part the map opens for the page's place (*map_path_for*), lets
    the map's first view load that chain without its search file, checking it as it goes.
    """
    lines = text.splitlines(keepends=True)
    end = 0
    if lines and lines[0].strip() == "---":
        end = next((index + 1 for index in range(1, len(lines)) if lines[index].strip() == "---"), 0)
    header, body = "".join(lines[:end]), "".join(lines[end:])
    # Published pages protect authored ranges before inserting renderer-owned
    # statement/navigation HTML. Re-converting that assembled page would
    # consume ordinary Markdown links and reparse existing card/heading IDs.
    # Direct callers still protect their authored body before introducing HTML.
    if not prepared:
        body = _protect_markdown_in_html(body)
    node = f' data-autoform-node-id="{html.escape(owner, quote=True)}"' if owner else ""
    if overview is not None:
        node += ' data-reader-overview=""'
    if topic:
        node += ' data-autoform-topic=""'
    if indexes:
        node += f' data-reader-indexes="{html.escape(" ".join(heading for heading, _ in indexes), quote=True)}"'
    title_header = "".join(lines[:2]) + "---\n" if end else text
    title = _header_title(title_header) or rendered_visible_text(_first_h1(text) or "") or _file_title(page)
    explorer = dag_viewer.render_container(
        relative_link(payload, page, ".json"),
        script_href=relative_link(destination / DAG_SCRIPT, page, ".js"),
        style_href=relative_link(destination / DAG_STYLE, page, ".css"),
        search_href=relative_link(destination / graph_pages.SEARCH_INDEX, page, ".json"),
        page_href=_as_published(relative_link(page, page, ".html")),
        scope=scope,
        map_path=map_path, map_path_for=map_path_for,
        # The map's script, stylesheet and index, which the book asks for only once its first commit is made.
        preloads=(
            (relative_link(destination / MAP_SCRIPT, page, ".js"), "script"),
            (relative_link(destination / MAP_STYLE, page, ".css"), "style"),
            (relative_link(destination / project_map.MAP_DIRECTORY / "index.json", page, ".json"), "fetch"),
        ),
    )
    frame = dag_viewer.render_boot(
        style_href=relative_link(destination / DAG_STYLE, page, ".css"),
        trail=trail, context=context, context_on_target=context_on_target, tracks_lean=tracks_lean, sources=sources,
        statements=statements, handle=overview, heading=None if overview is None else _plain_title(title),
        indexes=indexes,
    )
    # A project that tracks no Lean says so on the container, and the explorer's stylesheet hides its Lean progress.
    lean = "" if tracks_lean else ' data-tracks-lean="false"'
    # The frame stands before the page's container, where Python-Markdown keeps raw HTML as written: inside a
    # markdown="1" block it rebuilds nested raw HTML and can misplace its closing tags.
    return (
        header + "\n" + frame + "\n\n"
        + f'<div class="bp-explorer-page"{lean} markdown="1">\n'
        + f'<div class="bp-reading-source"{node} data-page-title="{html.escape(title, quote=True)}" markdown="1">\n'
        + dag_viewer.BOOT_SCRIPT + "\n\n"
        + body + '\n\n</div>\n\n' + explorer + '\n</div>\n'
    )
