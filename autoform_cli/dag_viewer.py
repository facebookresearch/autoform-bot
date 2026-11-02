"""Static payloads and the prebuilt Autoform graph explorer.

Publication supplies the authoritative topology. The browser's book reads it
for its outline, search and statement lists; its stage shows the whole-project
map, which loads its own data.
"""

from __future__ import annotations

import hashlib
import html
import math
from dataclasses import replace
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit

from .graph_views import FLATTENED_ROLES, GraphView
from .lean import declaration_names
from .site_index import _ALL_STATES, _display_status, _reading_label, _write_json
from .status import INVENTORY_STATES, MEANINGS

SCHEMA = "autoform-dag-view/v2"
#: Where the site publishes the viewer, relative to its root.
SCRIPT_PATH = "javascripts/blueprint-dag.js"
#: One cached stylesheet for the integrated book and map.
STYLE_PATH = "stylesheets/blueprint-dag.css"
#: The most nodes the explorer draws as a map; it only browses a larger view, which gets no layout.
MAX_MAP_NODES = 120

#: A catalog lists its keyword-declared public names; its explorer node and its Lean row
#: in the book show this many and a count.
_CATALOG_LEAN_NAMES = 8
_NODE_WIDTH = 196
_NODE_HEIGHT = 56


def _dimensions(view: GraphView) -> dict[str, dict[str, int]]:
    """Keep card sizing; the one browser engine owns every position and route."""
    atlas = uses_atlas(view)
    return {
        node.id: {
            "width": 232 if atlas else _NODE_WIDTH,
            "height": max(88, min(116, round(84 + 4 * math.log2(max(1, node.item_count) + 1)))) if atlas else _NODE_HEIGHT,
        }
        for node in view.nodes
    }


def drawn_view(view: GraphView) -> GraphView:
    """Return the part of *view* the explorer draws.

    An overview replaces collections and pagination ranges by their contents,
    so it draws those contents, not the wrappers or their relations.
    """
    if not view.overview:
        return view
    nodes = tuple(node for node in view.nodes if node.navigation not in FLATTENED_ROLES)
    ids = frozenset(node.id for node in nodes)
    return replace(view, nodes=nodes, edges=tuple(
        edge for edge in view.edges if edge.source in ids and edge.target in ids
    ))


def uses_atlas(view: GraphView) -> bool:
    """Draw topic cards in regions only when every drawn node is a topic.

    A leaf needs a readable card, so a view holding any leaf, such as a
    chapter with module catalogs beside its sections, is a DAG.
    """
    view = drawn_view(view)
    return (
        bool(view.nodes)
        and all(node.kind == "scope" for node in view.nodes)
        and (not view.edges or any(node.area for node in view.nodes))
    )


def _atlas_regions(view: GraphView) -> list[dict[str, object]]:
    """Count only authored areas; the browser owns their geometry too."""
    # Areas that differ only in case would otherwise keep the set's hash-seeded order.
    areas = sorted({node.area for node in view.nodes if node.area}, key=lambda area: (area.casefold(), area))
    return [{"count": sum(node.area == area for node in view.nodes), "label": area} for area in areas]


def _palette() -> dict[str, dict[str, object]]:
    """Name, explain and colour every status, so the explorer's swatches are its legend."""
    return {
        state.key: {
            "label": state.label,
            "meaning": MEANINGS[state.key],
            **({"neutral": True} if state in INVENTORY_STATES else {}),
            "light": {"fill": state.fill, "stroke": state.stroke, "text": state.text},
            "dark": {"fill": state.dark_fill, "stroke": state.dark_stroke, "text": state.dark_text},
        }
        for state in _ALL_STATES
    }


def write_payload(
    path: Path,
    view: GraphView,
    *,
    links: Mapping[str, str],
    breadcrumbs: Iterable[tuple[str, str | None]] = (),
    book: str | None = None,
) -> Path:
    """Write the deterministic layout and data one explorer page draws.

    *book* links a chapter's or section's explorer to its textbook page.

    The payload carries only what the explorer reads. On an overview, the
    collections and pagination ranges it replaces ride along with
    ``map: false`` and no layout.
    """
    known = frozenset(node.id for node in view.nodes)
    if len(known) != len(view.nodes):
        raise ValueError("dependency view has duplicate node ids")
    if any(edge.source not in known or edge.target not in known for edge in view.edges):
        raise ValueError("dependency view edge names an unknown node")
    map_view = drawn_view(view)
    map_ids = frozenset(node.id for node in map_view.nodes)
    dimensions = _dimensions(map_view) if len(map_view.nodes) <= MAX_MAP_NODES else {}
    degrees: dict[str, int] = {node.id: 0 for node in view.nodes}
    for edge in map_view.edges:
        degrees[edge.source] += 1
        degrees[edge.target] += 1

    nodes: list[dict[str, object]] = []
    present_statuses: set[str] = set()
    for node in view.nodes:
        status_key = _display_status(node)
        present_statuses.add(status_key)
        entry: dict[str, object] = {
            "area": node.area,
            "declaration": node.declaration,
            "id": node.id,
            "internal_dependency_count": node.internal_dependency_count,
            "isolated": degrees[node.id] == 0,
            "kind": node.kind,
            **({"layout": dimensions[node.id]} if node.id in dimensions else {}),
            "lean": node.lean,
            # The items the status counts add up to, so a scope's
            # "N items" header agrees with the rows beneath it.
            "member_count": node.item_count,
            "status": status_key,
            # Pairs, not an object, so the legend's state order survives sorted keys.
            "status_counts": node.status_counts,
            "summary": node.summary,
            "title": node.title,
            "url": links.get(node.id),
        }
        if view.overview:
            entry["map"] = node.id in map_ids
        if node.catalog == "module" and node.lean:
            names = declaration_names(node.lean)
            shown = names[:_CATALOG_LEAN_NAMES]
            entry.update(lean=", ".join(shown), lean_more=len(names) - len(shown))
        nodes.append(entry)

    edges = [
        {"proof": edge.proof_count, "source": edge.source, "statement": edge.statement_count, "target": edge.target}
        for edge in view.edges
    ]
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
        "edge_count": sum(edge.dependency_count for edge in map_view.edges),
        "edges": edges,
        "nodes": nodes,
        "palette": _palette(),
        "present_statuses": ordered_statuses,
        "regions": _atlas_regions(map_view) if uses_atlas(view) else [],
        "schema": SCHEMA,
        "title": view.title,
        "view": {
            "breadcrumbs": trail,
            "book": book,
            "presentation": "atlas" if uses_atlas(view) else "dag",
            "summary": view.summary,
        },
    }
    return _write_json(path, payload)


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
        + f'<a href="{href}">Download the complete graph data</a>.</p>\n'
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
    "blueprint-navigation.js", "blueprint-dag.js",
))


__all__ = [
    "MAX_MAP_NODES",
    "SCHEMA",
    "SCRIPT_PATH",
    "STYLE_PATH",
    "drawn_view",
    "render_container",
    "uses_atlas",
    "viewer_script",
    "write_payload",
]
