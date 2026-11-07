"""Static payloads and the prebuilt Autoform graph explorer.

Publication supplies the authoritative topology. The browser's book reads it
for its outline, search and statement lists; its stage shows the whole-project
map, which loads its own data.
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import replace
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote, urlsplit

from ..lean import declaration_names
from ..status import MEANINGS
from .graph_views import FLATTENED_ROLES, GraphView
from .site_index import _ALL_STATES, _display_status, _reading_label, _write_json

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
        entry: dict[str, object] = {
            "area": node.area,
            "id": node.id,
            "kind": node.kind,
            "lean": node.lean,
            "status": status_key,
            "summary": node.summary,
            "title": node.title,
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
        crumb: dict[str, object] = {"label": label, "url": url}
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
#: sheet without rules, is dropped, leaving the page as it was. Written without comments or line breaks, since every
#: page carries it.
BOOT_SCRIPT = "<script>" + "".join(line.strip() for line in """
(function (s) {
  var r = s.parentNode, home = r.parentNode;
  var f = document.querySelector(".bp-book-frame"), b = document.body, ok = false;
  s.remove();
  if (!f) return;
  try { ok = f.querySelector("link").sheet.cssRules.length > 0; } catch (e) {}
  if (!ok) { f.remove(); return; }
  var p = null, k = window.__md_scope && window.__md_scope.pathname;
  try { p = JSON.parse(localStorage.getItem("autoform:book-panes:v1" + (k ? ":" + k : ""))); } catch (e) {}
  if (p && typeof p === "object" && !Array.isArray(p)) {
    var m = function (q) { return Boolean(window.matchMedia && matchMedia(q).matches); };
    var w = !window.matchMedia || m("(min-width: 1101px)"), h = m("(max-width: 720px)");
    if (p.topics >= 160 && p.topics <= 480) f.style.setProperty("--dag-navigation-width", p.topics + "px");
    if (p.reading > 0 && p.reading < 1) {
      f.style.setProperty("--dag-reading-width", "minmax(300px, " + (100 * p.reading).toFixed(2) + "%)");
    }
    ["topics", "reading"].forEach(function (n) {
      var x = Boolean(p[n + "Hidden"]) && !h && (n === "reading" || w), t = f.querySelector(".bp-dag-pane-tab-" + n);
      f.classList.toggle("bp-dag-" + n + "-hidden", x); if (t) t.hidden = !x;
    });
    f.classList.toggle("bp-dag-map-hidden", p.mapHidden === true && !p.readingHidden && !h);
  }
  f.querySelector(".bp-reading-body").appendChild(r); b.classList.add("bp-book");
  window.AutoformBookBoot = {release: function () {
    if (!f.isConnected) return;
    home.prepend(r); f.remove(); b.classList.remove("bp-book");
  }};
  addEventListener("error", function (e) {
    var t = e.target, bundle = /[/]javascripts[/]blueprint-dag[.]js([?#]|$)/;
    var failed = t && t.tagName === "SCRIPT" ? bundle.test(t.src) : bundle.test(e.filename || "");
    if (failed) window.AutoformBookBoot.release();
  }, true);
})(document.currentScript);
""".splitlines()) + "</script>"

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
) -> str:
    """Return the book's frame for a page's first paint, before the explorer starts.

    *trail* holds the head's crumbs as (label HTML, href, or None for the page itself), and *context* the reader's
    "← topic" line (label HTML, href); with *context_on_target* it shows only while the address names a statement on
    the page, as the book shows it for a statement. *sources* says whether the page's topic has source texts, which
    the book's Browse offers beside its statements. The frame takes the viewer's own classes, so the book's rules lay
    it out as the explorer will be; :data:`BOOT_SCRIPT` moves the reading into it, and the explorer replaces it on its
    first commit. It links the explorer's stylesheet, which a script after it waits for, so the frame and the reading
    are styled before they show; the explorer's container links it too, so the book stays styled once the frame goes.
    Without JavaScript it stays hidden.
    """
    crumbs = list(trail)

    def crumb(index: int, label: str, href: str | None) -> str:
        if href is None:
            return f'<li aria-current="page">{label}</li>' if index == len(crumbs) - 1 else f"<li>{label}</li>"
        book = ' class="bp-dag-book"' if index == 0 else ""
        return f'<li><a{book} href="{html.escape(href, quote=True)}" tabindex="-1">{label}</a></li>'

    items = "".join(crumb(index, label, href) for index, (label, href) in enumerate(crumbs))
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
        f'<link rel="stylesheet" href="{html.escape(quote(style_href, safe="/%"), quote=True)}" '
        "data-autoform-dag-style>"
        '<header class="bp-dag-head" aria-hidden="true"><div class="bp-dag-context">'
        f'<nav class="bp-dag-breadcrumb"><ol>{items}</ol></nav>'
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
        f'<div class="bp-dag-inspector-inner">{line}<article class="bp-reading-body md-typeset"></article></div>'
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
) -> str:
    """Return a self-contained, progressively enhanced explorer host.

    ``intro`` is trusted, renderer-supplied HTML for the pre-enhancement and
    linked fallback. The enhanced reader uses its complete authored source,
    not this intro or a graph-node summary.
    """
    href = html.escape(quote(data_href, safe="/%"), quote=True)
    search = html.escape(quote(search_href, safe="/%"), quote=True) if search_href else ""
    search_attr = f' data-catalog-src="{search}"' if search else ""
    page_attr = (
        f' data-page-href="{html.escape(quote(page_href, safe="/%#?=&"), quote=True)}"'
        if page_href else ""
    )
    scope_attr = f' data-scope-id="{html.escape(scope, quote=True)}"' if scope else ""
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
        if style_href else f"<style data-autoform-dag-style>\n{_STYLE}\n</style>\n"
    )
    return (
        style
        + f'<div class="bp-dag-viewer" data-graph-src="{href}"{search_attr}{page_attr}{scope_attr}>\n'
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
    fingerprint = hashlib.sha256(_SCRIPT.encode("utf-8")).hexdigest()
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


_STYLE = _asset_text("blueprint-dag.css").strip()


_SCRIPT = "\n".join(_asset_text(name) for name in (
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
