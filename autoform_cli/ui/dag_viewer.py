"""Static payloads and the prebuilt Autoform graph explorer.

Publication supplies the authoritative topology. The browser's book reads it
for its outline, search and statement lists; its stage shows the whole-project
map, which loads its own data.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import replace
from functools import cache
from collections.abc import Iterable, Sequence
from pathlib import Path
from urllib.parse import quote, urlsplit

from ..lean import declaration_names
from ..status import MEANINGS
from .graph_views import FLATTENED_ROLES, GraphView
from .site_index import _ALL_STATES, _display_status, _reading_label, _shown_title, _summary_led_by, _write_json

SCHEMA = "autoform-dag-view/v2"
#: Where the site publishes the viewer, relative to its root.
SCRIPT_PATH = "javascripts/blueprint-dag.js"
#: One cached stylesheet for the integrated book and map.
STYLE_PATH = "stylesheets/blueprint-dag.css"
#: A catalog lists its keyword-declared public names; its Lean row in the book shows this
#: many and a count, and its payload entry carries the same names for search.
_CATALOG_LEAN_NAMES = 8


def drawn_view(view: GraphView) -> GraphView:
    """Return the part of *view* the book lists.

    An overview replaces collections and pagination ranges by their contents,
    so it lists those contents, not the wrappers or their relations.
    """
    if not view.overview:
        return view
    nodes = tuple(node for node in view.nodes if node.navigation not in FLATTENED_ROLES)
    ids = frozenset(node.id for node in nodes)
    return replace(view, nodes=nodes, edges=tuple(
        edge for edge in view.edges if edge.source in ids and edge.target in ids
    ))


def _palette() -> dict[str, dict[str, object]]:
    """Name, explain and colour every status, so the book's swatches are its legend."""
    return {
        state.key: {
            "label": state.label,
            "meaning": MEANINGS[state.key],
            "light": {"fill": state.fill, "stroke": state.stroke},
            "dark": {"fill": state.dark_fill, "stroke": state.dark_stroke},
        }
        for state in _ALL_STATES
    }


def write_payload(
    path: Path,
    view: GraphView,
    *,
    breadcrumbs: Iterable[tuple[str, str | None]] = (),
    book: str | None = None,
) -> Path:
    """Write what the book reads for one scope: its palette and statuses on
    every route, and its list until the search index arrives.

    *book* links a chapter's or section's payload to its textbook page.

    The payload carries only what the book reads: each node's title, kind,
    status, area, Lean names and summary, the palette and the page's trail.
    On an overview, the collections and pagination ranges it replaces ride
    along with ``map: false``.
    """
    known = frozenset(node.id for node in view.nodes)
    if len(known) != len(view.nodes):
        raise ValueError("dependency view has duplicate node ids")
    if any(edge.source not in known or edge.target not in known for edge in view.edges):
        raise ValueError("dependency view edge names an unknown node")
    map_ids = frozenset(node.id for node in drawn_view(view).nodes)

    nodes: list[dict[str, object]] = []
    present_statuses: set[str] = set()
    for node in view.nodes:
        status_key = _display_status(node)
        present_statuses.add(status_key)
        title = _shown_title(node.title)
        entry: dict[str, object] = {
            "area": node.area,
            "id": node.id,
            "kind": node.kind,
            "lean": node.lean,
            "status": status_key,
            "summary": _summary_led_by(node.summary, node.title, title),
            "title": title,
        }
        if view.overview:
            entry["map"] = node.id in map_ids
        if node.catalog == "module" and node.lean:
            entry["lean"] = ", ".join(declaration_names(node.lean)[:_CATALOG_LEAN_NAMES])
        nodes.append(entry)

    ordered_statuses = [
        state.key for state in _ALL_STATES if state.key in present_statuses
    ]
    trail: list[dict[str, object]] = []
    for label, url in breadcrumbs:
        crumb: dict[str, object] = {"label": _shown_title(label), "url": url}
        if (reading := _reading_label(label)) is not None:
            crumb["reading_title"] = reading
        trail.append(crumb)
    payload = {
        # Nothing reads a payload's relations: a statement's page lists its own, and the map has its
        # own data. Both clients still require the list.
        "edges": [],
        "nodes": nodes,
        "palette": _palette(),
        "present_statuses": ordered_statuses,
        "schema": SCHEMA,
        "title": view.title,
        "view": {"breadcrumbs": trail, "book": book},
    }
    return _write_json(path, payload)


#: The first child of a book page's reading. Before the explorer starts it moves the reading into the page's frame
#: (:func:`render_boot`), with Contents' and the reading pane's widths and the shut panes, the map's too, as the book's
#: bindPanes stores them, and marks the body ``bp-book``, so the first paint is the book with the article in its place;
#: the explorer takes the reading from the frame on its first commit. ``AutoformBookBoot.release`` gives the page back,
#: the reading first in its container again, when the explorer cannot start, and the boot script calls it when the
#: explorer's bundle cannot load or fails as it runs. A frame whose stylesheet did not load, which browsers leave as a
#: sheet without rules, is dropped, leaving the page as it was, and so is the frame on a phone, whose reader is a sheet
#: the frame cannot open: Material's page keeps the whole article until the book opens. A page turned to a phone's width
#: while it loads is given back the same way. A page render marks as an overview (``data-reader-overview``) has its
#: reader folded to the handle where the book's first commit folds it: with no fragment, the book's legacy filters
#: (blueprint-navigation.js legacyHash) or a legacy ``node`` naming the page's own topic. Where the page's history
#: entry saved the reader open, the book reopens it, so the frame shows it open with the book's × in place of the
#: handle. While it folds the reader, it shows the page's heading render wrote hidden for a screen reader, as the book
#: names a folded reader. On an address naming a statement card, it puts in the trail render wrote for one
#: (``data-boot-statements``), as the book's first commit draws it: the address names a card where its fragment and
#: the card's anchor read alike once the browser's URL parser has normalised both, as the book's routes compare.
#: Written without comments or line breaks, since every page carries it.
BOOT_SCRIPT = "<script>" + "".join(line.strip() for line in """
(function (s) {
  var r = s.parentNode, home = r.parentNode;
  var f = document.querySelector(".bp-book-frame"), b = document.body, ok = false;
  s.remove();
  if (!f) return;
  var m = function (q) { return Boolean(window.matchMedia && matchMedia(q).matches); };
  var h = window.matchMedia && matchMedia("(max-width: 720px)");
  try { ok = f.querySelector("link").sheet.cssRules.length > 0; } catch (e) {}
  if (!ok || h && h.matches) { f.remove(); return; }
  var p = null, k = window.__md_scope && window.__md_scope.pathname;
  try { p = JSON.parse(localStorage.getItem("autoform:book-panes:v1" + (k ? ":" + k : ""))); } catch (e) {}
  if (p && typeof p === "object" && !Array.isArray(p)) {
    var w = !window.matchMedia || m("(min-width: 1101px)");
    if (p.topics >= 160 && p.topics <= 480) f.style.setProperty("--dag-navigation-width", p.topics + "px");
    if (p.reading > 0 && p.reading < 1) {
      f.style.setProperty("--dag-reading-width", "minmax(300px, " + (100 * p.reading).toFixed(2) + "%)");
    }
    ["topics", "reading"].forEach(function (n) {
      var x = Boolean(p[n + "Hidden"]) && (n === "reading" || w), t = f.querySelector(".bp-dag-pane-tab-" + n);
      f.classList.toggle("bp-dag-" + n + "-hidden", x); if (t) t.hidden = !x;
    });
    f.classList.toggle("bp-dag-map-hidden", p.mapHidden === true && !p.readingHidden);
  }
  var st = f.querySelector("template[data-boot-statements]"), sh = st && location.hash && Array.prototype.find.call(
    st.content.querySelectorAll("li[data-anchor]"), function (e) {
      return new URL("#" + e.getAttribute("data-anchor"), location.href).hash === location.hash;
    });
  if (sh) {
    var sl = f.querySelector(".bp-dag-breadcrumb ol");
    sl.replaceChildren.apply(sl, Array.from(st.content.querySelectorAll("li:not([data-anchor])"), function (e) {
      return e.cloneNode(true);
    }).concat(sh.cloneNode(true)));
  }
  var g = location.hash.slice(1), u = new URLSearchParams(g), q = Array.from(u), y = {};
  var d = u.get("node"), o = r.getAttribute("data-autoform-node-id");
  var l = d !== null ? d === o || d.replace(/^(?:scope|boundary):/, "") === o : q.length > 0 && q.every(function (e) {
    var n = e[0], v = e[1]; if (y[n]) return false; y[n] = 1;
    if (n === "view") return v === "graph" || v === "list";
    if (n === "direction") return ["both", "prerequisites", "dependents"].indexOf(v) >= 0;
    if (n === "isolates") return v === "0" || v === "1";
    return n === "status" && (v === "-" || v.split(",").every(function (t) { return /^(?:STATUSES)$/.test(t); }));
  });
  var a = history.state && history.state.autoformNavigation, c = a && a.version === 1 && a.url === location.href && a.snapshot;
  var i = r.hasAttribute("data-reader-overview") && (!g || l), j = i && Boolean(c && c.readingOpen !== false);
  var z = f.querySelector(".bp-dag-sheet-handle"), e = f.querySelector("h1.bp-dag-sr-only");
  f.classList.toggle("bp-dag-overview-only", i); f.classList.toggle("bp-dag-sheet-open", j); if (j && z) z.textContent = "\\u00d7";
  if (e) e.hidden = !i || j;
  f.querySelector(".bp-reading-body").appendChild(r); b.classList.add("bp-book");
  window.AutoformBookBoot = {release: function () {
    if (!f.isConnected) return;
    home.prepend(r); f.remove(); b.classList.remove("bp-book");
  }};
  if (h && h.addEventListener) h.addEventListener("change", function (e) { if (e.matches) window.AutoformBookBoot.release(); });
  addEventListener("error", function (e) {
    var t = e.target, bundle = /[/]javascripts[/]blueprint-dag[.]js([?#]|$)/;
    var failed = t && t.tagName === "SCRIPT" ? bundle.test(t.src) : bundle.test(e.filename || "");
    if (failed) window.AutoformBookBoot.release();
  }, true);
})(document.currentScript);
""".splitlines()).replace("STATUSES", "|".join(state.key for state in _ALL_STATES)) + "</script>"

_MAP_ONLY_MARK = (
    '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 7.5V3h4.5M12.5 3H17v4.5M17 12.5V17h-4.5M7.5 17H3v-4.5" '
    'fill="none" stroke="currentColor" stroke-width="1.5"></path></svg>'
)
_THEME_MARK = (
    '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.25" fill="none" stroke="currentColor" '
    'stroke-width="1.5"></circle><path d="M10 2.75a7.25 7.25 0 0 1 0 14.5z" fill="currentColor"></path></svg>'
)


def render_boot(
    *,
    style_href: str,
    trail: Iterable[tuple[str, str | None]] = (),
    context: tuple[str, str] | None = None,
    context_on_target: bool = False,
    tracks_lean: bool = True,
    sources: bool = False,
    statements: tuple[Sequence[tuple[str, str | None]], Sequence[tuple[str, str]]] | None = None,
    handle: str | None = None,
    heading: str | None = None,
) -> str:
    """Return the book's frame for a page's first paint, before the explorer starts.

    *trail* holds the head's crumbs as (label HTML, href, or None for the page itself), and *context* the reader's
    "← topic" line (label HTML, href); with *context_on_target* it shows only while the address names a statement on
    the page, as the book shows it for a statement. *sources* says whether the page's topic has source texts, which
    the book's Browse offers beside its statements. *handle*, a title's label HTML, gives an overview page the
    reader's handle, which the book shows in place of a folded reader, and *heading*, the page's plain title, the
    heading a screen reader finds while the reader is folded, as the book gives one. The frame takes the viewer's own
    classes, so the book's rules lay it out as the explorer will be; :data:`BOOT_SCRIPT` moves the reading into it,
    and the explorer replaces it on its first commit. It links the explorer's stylesheet, which a script after it
    waits for, so the frame and the reading are styled before they show; the explorer's container links it too, so
    the book stays styled once the frame goes. Without JavaScript it stays hidden.

    *statements*, on a page with statement cards, holds the trail the book draws on an address naming one of them, in
    *trail*'s form without the statement, and each card's anchor and title's label HTML; :data:`BOOT_SCRIPT` swaps that
    trail in on such an address.
    """
    def crumb(index: int, label: str, href: str | None, last: bool) -> str:
        if href is None:
            return f'<li aria-current="page">{label}</li>' if last else f"<li>{label}</li>"
        book = ' class="bp-dag-book"' if index == 0 else ""
        return f'<li><a{book} href="{html.escape(href, quote=True)}" tabindex="-1">{label}</a></li>'

    def items(crumbs: Sequence[tuple[str, str | None]], last: bool = True) -> str:
        return "".join(
            crumb(index, label, href, last and index == len(crumbs) - 1) for index, (label, href) in enumerate(crumbs)
        )

    swap = ""
    if statements:
        topics, cards = statements
        swap = "<template data-boot-statements>" + items(topics, last=False) + "".join(
            f'<li data-anchor="{html.escape(anchor, quote=True)}" aria-current="page">{label}</li>'
            for anchor, label in cards
        ) + "</template>"
    search = "Search titles and Lean names" if tracks_lean else "Search titles"
    # The controls are the book's buttons, out of the tab order until the book's own replace them. A project that
    # tracks no Lean has no status filter.
    filters = (
        '<div class="bp-dag-filters"><button type="button" class="bp-dag-filter-summary" tabindex="-1">'
        "Filter: all statuses</button></div>"
        if tracks_lean else ""
    )
    line = ""
    if context:
        label, href = context
        target = " data-boot-target" if context_on_target else ""
        line = (
            f'<div class="bp-reading-context" aria-hidden="true"{target}><a class="bp-reading-around" '
            f'href="{html.escape(href, quote=True)}" tabindex="-1">← {label}</a></div>'
        )
    return (
        '<div class="bp-dag-viewer bp-dag-integrated bp-book-frame" hidden data-search-exclude>'
        + (f'<h1 class="bp-dag-sr-only" hidden>{html.escape(heading)}</h1>' if heading is not None else "")
        + f'<link rel="stylesheet" href="{html.escape(quote(style_href, safe="/%"), quote=True)}" '
        "data-autoform-dag-style>"
        '<header class="bp-dag-head" aria-hidden="true"><div class="bp-dag-context">'
        f'<nav class="bp-dag-breadcrumb"><ol>{items(list(trail))}</ol>{swap}</nav>'
        f'<button type="button" class="bp-dag-map-only" tabindex="-1">{_MAP_ONLY_MARK}</button>'
        f'<button type="button" class="bp-dag-button bp-dag-theme" tabindex="-1">{_THEME_MARK}</button></div>'
        '<div class="bp-dag-toolbar"><div class="bp-dag-search-wrap">'
        f'<input class="bp-dag-search" type="search" placeholder="{search}" readonly tabindex="-1"></div>'
        '<button type="button" class="bp-dag-button bp-dag-outline-toggle" tabindex="-1">Contents</button>'
        + filters +
        '<div class="bp-dag-browse"><button type="button" class="bp-dag-button" tabindex="-1">'
        + ("Browse" if sources else "Statements") + "</button></div></div></header>"
        '<div class="bp-dag-body"><aside class="bp-dag-navigation" aria-hidden="true">'
        '<div class="bp-dag-navigation-inner"></div></aside>'
        '<div class="bp-dag-main" aria-hidden="true">'
        '<button type="button" class="bp-dag-pane-tab bp-dag-pane-tab-map" tabindex="-1">Map</button>'
        '<button type="button" class="bp-dag-pane-tab bp-dag-pane-tab-topics" tabindex="-1" hidden>Contents</button>'
        # No stage of its own: a test or script that waits for the book's stage waits for the explorer.
        '<p class="bp-dag-map-loading">Loading the map…</p>'
        '<button type="button" class="bp-dag-pane-tab bp-dag-pane-tab-reading" tabindex="-1" hidden>Reading</button>'
        "</div>"
        '<aside class="bp-dag-inspector bp-reading-pane" aria-label="Mathematical reading">'
        + (
            '<button type="button" class="bp-dag-sheet-handle" aria-hidden="true" tabindex="-1">'
            f'<span class="bp-dag-sheet-title">{handle}</span></button>' if handle is not None else ""
        )
        + f'<div class="bp-dag-inspector-inner">{line}<article class="bp-reading-body md-typeset"></article></div>'
        "</aside></div></div>"
    )


def render_container(
    data_href: str,
    *,
    script_href: str | None = None,
    style_href: str | None = None,
    fallback_links: Iterable[tuple[str, str]] = (),
    fallback_total: int | None = None,
    search_href: str | None = None,
    intro: str = "",
    page_href: str | None = None,
    scope: str | None = None,
    map_path: Sequence[tuple[str, str]] = (),
    map_path_for: str | None = None,
    preloads: Sequence[tuple[str, str]] = (),
) -> str:
    """Return a self-contained, progressively enhanced explorer host.

    ``intro`` is trusted, renderer-supplied HTML for the pre-enhancement and
    linked fallback. The enhanced reader uses its complete authored source,
    not this intro or a graph-node summary.

    ``map_path`` is the map's chain of ``(part id, shard file)`` rows down to
    the part the map opens for ``map_path_for``, the page's place: a hint the
    map checks as its first view walks it. ``preloads`` are ``(href,
    destination)`` pairs the browser may fetch early, at low priority.
    """
    href = html.escape(quote(data_href, safe="/%"), quote=True)
    search = html.escape(quote(search_href, safe="/%"), quote=True) if search_href else ""
    search_attr = f' data-catalog-src="{search}"' if search else ""
    page_attr = (
        f' data-page-href="{html.escape(quote(page_href, safe="/%#?=&"), quote=True)}"'
        if page_href else ""
    )
    scope_attr = f' data-scope-id="{html.escape(scope, quote=True)}"' if scope else ""
    rows = json.dumps([list(row) for row in map_path], ensure_ascii=False, separators=(",", ":"))
    map_attr = (
        f' data-map-path="{html.escape(rows, quote=True)}" data-map-path-for="{html.escape(map_path_for, quote=True)}"'
        if map_path and map_path_for else ""
    )
    script = (
        f'\n<script defer src="{html.escape(quote(script_href, safe="/%"), quote=True)}"></script>'
        if script_href
        else ""
    )
    fallback = tuple(
        (label, target)
        for label, target in fallback_links
        if target and _safe_fallback_href(target)
    )
    total = max(len(fallback), fallback_total or 0)
    items = "\n".join(
        "    <li>"
        f'<a href="{html.escape(target, quote=True)}">{html.escape(label)}</a>'
        "</li>"
        for label, target in fallback
    )
    inventory = (
        '  <div class="bp-dag-fallback" data-search-exclude>\n'
        "    <p>The dependency explorer needs JavaScript and its graph data.</p>\n"
        + (f'    <ul class="bp-dag-fallback-list">\n{items}\n    </ul>\n' if items else "")
        + (
            f"    <p>Showing {len(fallback)} of {total} linked nodes. "
            if total > len(fallback)
            else "    <p>"
        )
        # A scope's payload holds what its page lists; the search index holds every record and relation.
        + f'<a href="{search or href}">Download the complete graph data</a>.</p>\n'
        "  </div>\n"
    )
    style = (
        f'<link rel="stylesheet" href="{html.escape(quote(style_href, safe="/%"), quote=True)}" '\
        'data-autoform-dag-style>\n'
        if style_href else f"<style data-autoform-dag-style>\n{_style()}\n</style>\n"
    )
    # A fetch() is a CORS request even on the same origin, so its preload must say crossorigin to be the one reused
    # (WebKit's memory cache asks again all the same: its preload carries an Origin header a same-origin fetch lacks).
    early = "".join(
        f'<link rel="preload" href="{html.escape(quote(target, safe="/%"), quote=True)}" as="{kind}"'
        f'{" crossorigin" if kind == "fetch" else ""} fetchpriority="low">\n'
        for target, kind in preloads
    )
    return (
        style + early
        + f'<div class="bp-dag-viewer" data-graph-src="{href}"{search_attr}{page_attr}{scope_attr}{map_attr}>\n'
        + (f"  {intro}\n" if intro else "")
        + '  <p class="bp-dag-loading" role="status" hidden data-search-exclude>Loading dependency explorer…</p>\n'
        f"{inventory}"
        f"</div>{script}"
    )


def _safe_fallback_href(value: str) -> bool:
    """Allow only generated relative links in the non-JavaScript fallback."""
    if any(ord(character) < 32 for character in value) or value.startswith("//"):
        return False
    parsed = urlsplit(value)
    return not parsed.scheme and not parsed.netloc


def viewer_script() -> str:
    """Return only a prebuilt browser runtime matching its packaged sources.

    The checked-in bundle is built from readable shared modules with a pinned
    development compiler; consumers never need Node. Its fingerprint matches
    UTF-8/LF source text, independent of checkout line endings. A missing,
    corrupt or stale bundle fails publication
    instead of bypassing the runtime budget with a raw development client.
    """
    built = Path(__file__).with_name("assets") / "blueprint-explorer.min.js"
    fingerprint = hashlib.sha256(_script().encode("utf-8")).hexdigest()
    try:
        contents = built.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError("Explorer bundle is missing or corrupt; run npm run build:explorer before publication.") from error
    if not contents.startswith(f"/* autoform-explorer-source-sha256:{fingerprint} */\n"):
        raise ValueError("Explorer bundle is stale; run npm run build:explorer before publication.")
    if len(contents.encode("utf-8")) >= 145_000:
        raise ValueError("Explorer bundle exceeds the JavaScript budget; run npm run build:explorer before publication.")
    return contents


# Published pages put the explorer inside Material's article.md-typeset, whose
# list, code and heading rules reach a specificity of (0,2,2). The rules they
# would override carry a doubled .bp-dag-viewer class, and the status filter is
# a button and popover because Material styles details and summary as an
# admonition. The head stacks above the details drawer and the bottom sheet so
# that neither of its popovers paints under them. Fixed-pixel controls avoid
# Material's changing rem scale. The integrated reader shares a responsive
# pane with one semantic topic rail. Phones keep
# the same Reading handle, and authored region browsing gives the map that
# pane's width. The integrated client preserves one accessible page heading.
def _asset_text(name: str) -> str:
    """Read packaged, project-neutral viewer assets once per process."""
    return Path(__file__).with_name("assets").joinpath(name).read_text(encoding="utf-8")


@cache
def _style() -> str:
    """The viewer's stylesheet, read on first use, so importing the renderer, as check does, reads no asset."""
    return _asset_text("blueprint-dag.css").strip()


@cache
def _script() -> str:
    """The sources viewer_script checks the bundle against, read on first use and kept, so a test that moves
    __file__ patches this first."""
    return "\n".join(_asset_text(name) for name in (
        "tex-plain.js", "blueprint-navigation.js", "blueprint-dag.js",
    ))


__all__ = [
    "BOOT_SCRIPT",
    "SCHEMA",
    "SCRIPT_PATH",
    "STYLE_PATH",
    "drawn_view",
    "render_boot",
    "render_container",
    "viewer_script",
    "write_payload",
]
