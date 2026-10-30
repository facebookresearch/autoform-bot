"""Static payloads and the prebuilt Autoform graph explorer.

Publication supplies the authoritative topology and card dimensions. The
integrated browser caches deterministic dependency-driven geometry per bounded
projection, paints its routes on Canvas and places ordinary DOM controls over
visible nodes, keeping the graph navigable rather than bitmap-only.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import posixpath
import re
from functools import lru_cache
from dataclasses import dataclass, replace
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePath
from urllib.parse import quote, unquote, urlsplit

from markdown.extensions.attr_list import AttrListTreeprocessor
from markdown.inlinepatterns import BACKTICK_RE
from markdown.blockprocessors import HashHeaderProcessor
from pymdownx.arithmatex import (
    RE_BRACKET_BLOCK,
    RE_BRACKET_INLINE,
    RE_DOLLAR_BLOCK,
    RE_SMART_DOLLAR_INLINE,
    RE_TEX_BLOCK,
)

from . import markdown
from .graph_views import FLATTENED_ROLES, INVENTORY_STATUSES, GraphView, ViewNode
from .lean import declaration_names
from .publishable import html_names
from .status import INVENTORY_STATES, MEANINGS, STATES

SCHEMA = "autoform-dag-view/v2"
SEARCH_SCHEMA = "autoform-dag-search/v1"
WIKI_LINKS_SCHEMA = "autoform-wiki-links/v1"
#: A separate optional reference layer must fit the reader's unchanged data limit.
MAX_WIKI_LINK_BYTES = 32 * 1024 * 1024
_SEARCH_INDEX_BYTE_LIMIT = 64 * 1024 * 1024
# Above this size the whole-library index leaves out statement summaries,
# which are a quarter of a large library's index; titles and Lean names stay.
_SEARCH_INDEX_SUMMARY_BUDGET = 32 * 1024 * 1024
_WIKI_BLOCK_MATH = re.compile(f"(?:{RE_DOLLAR_BLOCK}|{RE_BRACKET_BLOCK}|{RE_TEX_BLOCK})", re.DOTALL)
_WIKI_INLINE_MATH = re.compile(f"(?:{RE_SMART_DOLLAR_INLINE}|{RE_BRACKET_INLINE})", re.DOTALL)
_WIKI_INLINE_CODE = re.compile(BACKTICK_RE, re.DOTALL)
_WIKI_HEADING = HashHeaderProcessor.RE
#: Where the site publishes the viewer, relative to its root.
SCRIPT_PATH = "javascripts/blueprint-dag.js"
#: One cached stylesheet for the integrated book and map.
STYLE_PATH = "stylesheets/blueprint-dag.css"
#: The most nodes the explorer draws as a map; it only browses a larger view, which gets no layout.
MAX_MAP_NODES = 120

_ALL_STATES = (*STATES, *INVENTORY_STATES)
_PALETTE_KEYS = frozenset(state.key for state in _ALL_STATES)
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


def _display_status(node: ViewNode) -> str:
    """Return the palette key a node is drawn in: a catalog's inventory status, else its own."""
    inventory = node.catalog == "module" and next(
        (key for key, count in node.status_counts if count and key in INVENTORY_STATUSES),
        None,
    )
    key = inventory or node.status_key or (node.status_counts[0][0] if node.status_counts else "planned")
    return key if key in _PALETTE_KEYS else "planned"


def _json_text(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _write_json(
    path: Path, payload: Mapping[str, object], *, sha256_out: list[str] | None = None,
    byte_count_out: list[int] | None = None,
) -> Path:
    text = _json_text(payload)
    if sha256_out is not None or byte_count_out is not None:
        encoded = text.encode("utf-8")
        if sha256_out is not None:
            sha256_out.append(hashlib.sha256(encoded).hexdigest())
        if byte_count_out is not None:
            byte_count_out.append(len(encoded))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        text,
        encoding="utf-8",
        newline="\n",
    )
    return path


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


@lru_cache(maxsize=4096)
def _reading_label(text: str, *, require_math: bool = True) -> dict[str, object] | None:
    """Safe, derived inline text/code/math parts for reading-only controls.

    Read the same captured label through the site's converter, never a source
    pathname. HTML, links, images and attributes are not a label transport.
    Plain labels keep their existing wire bytes and require no conversion.
    """
    triggers = r"\$|\\\(|\\\[" if require_math else r"\$|\\\(|\\\[|`"
    if len(text) > 16_384 or not re.search(triggers, text):
        return None
    tree = markdown.render_tree(text, allow_html=False)
    if tree is None:
        return None
    body = tree.find("body")
    if body is None:
        return None
    parts: list[list[str]] = []
    has_math = False

    def append(kind: str, value: str | None) -> None:
        if not value:
            return
        if kind == "text" and parts and parts[-1][0] == kind:
            parts[-1][1] += value
        else:
            parts.append([kind, value])

    def read(element: object) -> bool:
        nonlocal has_math
        tag = element.tag
        if tag in {"span", "div"} and element.attrib == {"class": markdown.FORMULA_CLASS}:
            if len(element) or not element.text:
                return False
            has_math = True
            append("math", element.text)
            return True
        if tag == "code" and not element.attrib:
            append("code", "".join(element.itertext()))
            return True
        if tag not in {"body", "p", "em", "strong"} or element.attrib:
            return False
        append("text", element.text)
        for child in element:
            if not read(child):
                return False
            append("text", child.tail)
        return True

    if not read(body) or (require_math and not has_math) or len(parts) > 256:
        return None
    return {"format": "inline-parts-v1", "source": text, "parts": parts}


def write_search_index(
    path: Path,
    view: GraphView,
    *,
    context_links: Mapping[str, str],
    metadata: Mapping[str, Mapping[str, object]] | None = None,
    include_relations: bool = False,
    sha256_out: list[str] | None = None,
    warnings_out: list[str] | None = None,
) -> Path:
    """Write the global search index and optional derived reading/navigation data.

    Existing search fields retain their meanings. Integrated publication can
    add canonical book routes, authored containment, and compact dependencies.
    Parents and relations can reference positions in the emitted node array;
    the reader also accepts older string endpoints. This retains containment
    and every typed edge without repeating long article identifiers.
    ``sha256_out`` optionally receives the exact emitted bytes' digest without
    rereading any source or changing the index's wire representation.
    An index over ``_SEARCH_INDEX_SUMMARY_BUDGET`` bytes is written again
    without statement summaries, so search keeps titles and Lean names.
    ``warnings_out`` reports that omission, and when the emitted bytes reach
    95% of the default browser limit, without rejecting publication.
    """
    positions = {node.id: position for position, node in enumerate(view.nodes)}
    nodes: list[dict[str, object]] = []
    for node in view.nodes:
        entry: dict[str, object] = {
            "id": node.id,
            "kind": node.kind,
            "status": _display_status(node),
            "title": node.title,
            "url": context_links.get(node.id),
        }
        # Search matches statements and Lean names as well as titles.
        if node.lean:
            entry["lean"] = node.lean
        if node.summary:
            entry["summary"] = node.summary
        label = _reading_label(node.title)
        if label is not None:
            entry["reading_title"] = label
        # Preserve authored module articles without calling their inventories
        # concrete declarations or inferring proof progress from them.
        if node.catalog == "module":
            entry["catalog"] = node.catalog
        if metadata is not None:
            details = metadata.get(node.id, {})
            for key in ("route", "scope", "data_url", "parent", "navigation", "area"):
                value = details.get(key)
                if key == "parent" and isinstance(value, str):
                    value = positions.get(value, value)
                if value is not None:
                    entry[key] = value
            if type(details.get("result")) is bool:
                entry["result"] = details["result"]
        nodes.append(entry)
    payload: dict[str, object] = {"nodes": nodes, "schema": SEARCH_SCHEMA}
    if include_relations:
        payload["relations"] = [
            [positions[edge.source], positions[edge.target], edge.statement_count, edge.proof_count]
            for edge in view.edges
        ]
    sizes: list[int] = []
    digests = len(sha256_out) if sha256_out is not None else 0
    written = _write_json(path, payload, sha256_out=sha256_out, byte_count_out=sizes)
    if sizes[0] > _SEARCH_INDEX_SUMMARY_BUDGET and any("summary" in entry for entry in nodes):
        full = sizes.pop()
        for entry in nodes:
            entry.pop("summary", None)
        if sha256_out is not None:
            del sha256_out[digests:]
        written = _write_json(path, payload, sha256_out=sha256_out, byte_count_out=sizes)
        if warnings_out is not None:
            warnings_out.append(
                f"search index leaves out statement summaries: with them it would use {full:,} bytes, over the "
                f"{_SEARCH_INDEX_SUMMARY_BUDGET:,}-byte budget; search still matches titles and Lean names"
            )
    if warnings_out is not None and sizes[0] * 100 >= _SEARCH_INDEX_BYTE_LIMIT * 95:
        warnings_out.append(
            f"search index uses {sizes[0]:,} bytes ({sizes[0] / _SEARCH_INDEX_BYTE_LIMIT:.1%} of the "
            f"{_SEARCH_INDEX_BYTE_LIMIT:,}-byte default browser limit); browser loading fails above that limit"
        )
    return written


@dataclass(frozen=True, slots=True)
class WikiLinks:
    """Derived ordinary article references, never formal prerequisites.

    Endpoints are positions in the unchanged global index. Missing captures
    are distinguished from articles which contain no eligible references.
    """

    references: tuple[tuple[int, int], ...]
    unavailable_sources: tuple[int, ...] = ()
    reason: str | None = None


def _wiki_path(value: str | PurePath) -> str | None:
    """Normalize a captured blueprint-relative name lexically, never on disk."""
    raw = value.as_posix() if isinstance(value, PurePath) else value
    if not isinstance(raw, str) or not raw or raw.startswith("/") or "\\" in raw:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        return None
    normalized = posixpath.normpath(raw)
    parts = normalized.split("/")
    if parts[0] != "roadmap" or any(part.startswith(".") for part in parts):
        return None
    if PurePath(normalized).suffix.lower() not in markdown.PAGE_SUFFIXES:
        return None
    return normalized


def _wiki_route(value: object) -> bool:
    """Only renderer-supplied published local routes establish a target."""
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    try:
        located = urlsplit(value)
        path = unquote(located.path, errors="strict")
    except (ValueError, UnicodeError):
        return False
    return bool(
        path and not located.scheme and not located.netloc and not path.startswith("/")
        and "\\" not in path
        and not any(part.startswith(".") and part not in {".", ".."} for part in path.split("/"))
        and not any(ord(character) < 32 or ord(character) == 127 for character in path)
    )


def _wiki_lines(body: str) -> list[str] | None:
    """Use the site's Markdown/TeX masks; leave unsupported HTML unavailable."""
    lines = body.splitlines()
    lines = lines[markdown.frontmatter_end(lines):]
    if markdown.first_unclosed_fence(lines) is not None:
        return None
    masked = "\n".join(markdown.content_lines("\n".join(lines)))
    for line in masked.splitlines():
        heading = _WIKI_HEADING.match(line)
        if heading and AttrListTreeprocessor.HEADER_RE.search(heading.group("header")):
            # The site applies these attributes and publishes the article as
            # typed text. Captured-source callers cannot claim live links.
            return None
    cuts = [0, *(match.end() for match in re.finditer(r"\n[ \t]*\n", masked)), len(masked)]

    def blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group())

    def inline(pattern: re.Pattern[str], text: str) -> str:
        # Inline spans can contain line breaks, but cannot join separate
        # Markdown paragraphs. Keep the original boundaries after masking.
        return "".join(pattern.sub(blank, text[start:end]) for start, end in zip(cuts, cuts[1:]))

    masked = inline(_WIKI_INLINE_CODE, masked)
    masked = _WIKI_BLOCK_MATH.sub(blank, masked)
    masked = inline(_WIKI_INLINE_MATH, masked)
    # Escaped ticks are text, not delimiters for link_targets' simpler mask.
    masked = re.sub(r"\\`", blank, masked)
    if html_names(masked) or re.search(r"\$\$|\\\[|\\begin\{", masked):
        # Raw HTML changes how surrounding Markdown is read; unclosed display
        # math similarly has no safe range. Do not pretend to read either one.
        return None
    return masked.splitlines()


def _wiki_targets(lines: list[str]) -> list[str] | None:
    """Read links and section boundaries through the site's actual converter.

    The existing masks remove code/math, and raw HTML stays unsupported. ATX
    and Setext headings then share one interpretation, including nested blocks
    and formatted section names; a bibliography cannot become a concept edge.
    """
    tree = markdown.render_tree("\n".join(lines), allow_html=False)
    body = tree.find("body") if tree is not None else None
    if body is None:
        return None
    headings = {f"h{level}" for level in range(1, 7)}
    if any(element.tag in headings and set(element.attrib) - {"id"} for element in body.iter()):
        return None
    targets: list[str] = []

    def read(parent: object) -> None:
        excluded = False
        for element in parent:
            if element.tag in headings:
                if element.tag in {"h1", "h2"}:
                    excluded = "".join(element.itertext()).strip().casefold() in {
                        "sources", "references", "bibliography", "depends on", "proof depends on",
                    }
                continue
            if excluded or element.tag in {"code", "pre", "img"}:
                continue
            if element.tag == "a":
                target = element.get("href")
                if target and not any(child.tag == "img" for child in element.iter()):
                    targets.append(target)
            else:
                read(element)

    read(body)
    return targets


def extract_wiki_links(
    view: GraphView,
    *,
    captured_sources: Mapping[str, bytes] | None = None,
    source_paths: Mapping[str, str | PurePath] | None = None,
    metadata: Mapping[str, Mapping[str, object]] | None = None,
    unavailable_source_ids: Iterable[str] = (),
) -> WikiLinks:
    """Read ordinary Markdown links from supplied captured article bytes.

    No authored path is opened, no title/heading/import becomes a reference,
    and no generated navigation is consulted. Source tracks (including their
    descendants), hidden/unpublished targets, structural wrappers, self-links
    and authored ancestor/descendant placement are not wiki relationships.
    Explicit relative Markdown file targets may include percent escapes and
    fragments; these identify the article, not an invented heading node.
    This first layer reads Markdown links, not raw-HTML anchors. Unsupported
    HTML, unclosed code fences or unclosed display math is reported as an
    unavailable source for outbound extraction, not an unpublished target.
    ``unavailable_source_ids`` carries the publisher's existing source omissions
    without parsing an article again; their valid incoming targets remain live.
    """
    identifiers = tuple(node.id for node in view.nodes)
    positions = {node_id: position for position, node_id in enumerate(identifiers)}
    if len(positions) != len(identifiers):
        raise ValueError("wiki reference index has duplicate node ids")
    details = metadata or {}
    roles = {node.id: details.get(node.id, {}).get("navigation", node.navigation) for node in view.nodes}
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    for node_id in identifiers:
        parent = details.get(node_id, {}).get("parent")
        if isinstance(parent, str) and parent in positions:
            children[parent].append(node_id)
        else:
            roots.append(node_id)

    # Linear tree intervals give placement checks without ancestor sets or
    # repeated path traversal. A malformed cyclic component remains unavailable.
    intervals: dict[str, tuple[int, int]] = {}
    starts: dict[str, int] = {}
    source_tracks: set[str] = set()
    clock = 0
    for root in roots:
        pending = [(root, False, False)]
        while pending:
            node_id, closing, inherited_source = pending.pop()
            if closing:
                intervals[node_id] = (starts[node_id], clock)
                continue
            starts[node_id] = clock
            clock += 1
            source_track = inherited_source or roles[node_id] == "source"
            if source_track:
                source_tracks.add(node_id)
            pending.append((node_id, True, source_track))
            pending.extend((child, False, source_track) for child in reversed(children[node_id]))

    candidates = tuple(
        node.id for node in view.nodes
        if node.kind != "boundary" and node.id not in source_tracks
        and roles[node.id] not in FLATTENED_ROLES | {"source"}
    )
    if captured_sources is None or source_paths is None or metadata is None:
        return WikiLinks(
            (), tuple(positions[node_id] for node_id in candidates),
            "captured article sources and publication paths were not supplied",
        )

    unavailable: set[int] = set()
    paths: dict[str, str] = {}
    owners: dict[str, str | None] = {}
    for node_id in candidates:
        path = _wiki_path(source_paths.get(node_id, ""))
        captured = captured_sources.get(node_id)
        if path is None or not _wiki_route(details.get(node_id, {}).get("route")) or node_id not in intervals:
            unavailable.add(positions[node_id])
            continue
        if not isinstance(captured, bytes):
            unavailable.add(positions[node_id])
            continue
        paths[node_id] = path
        owners[path] = node_id if path not in owners else None

    references: set[tuple[int, int]] = set()
    invalid_targets: set[int] = set()
    omitted_sources = frozenset(unavailable_source_ids)
    reference_bytes = 2
    for source_id in paths:
        if owners[paths[source_id]] is None:
            unavailable.add(positions[source_id])
            continue
        try:
            body = captured_sources[source_id].decode("utf-8").removeprefix("\ufeff")
        except UnicodeError:
            unavailable.add(positions[source_id])
            invalid_targets.add(positions[source_id])
            continue
        if source_id in omitted_sources:
            unavailable.add(positions[source_id])
            continue
        lines = _wiki_lines(body)
        if lines is None:
            unavailable.add(positions[source_id])
            continue
        targets = _wiki_targets(lines)
        if targets is None:
            unavailable.add(positions[source_id])
            continue
        for target in targets:
            if "\\" in target or any(ord(character) < 32 or ord(character) == 127 for character in target):
                continue
            try:
                located = urlsplit(target)
                decoded = unquote(located.path, errors="strict")
            except (ValueError, UnicodeError):
                continue
            if not decoded or located.scheme or located.netloc or decoded.startswith("/"):
                continue
            path = _wiki_path(posixpath.join(posixpath.dirname(paths[source_id]), decoded))
            target_id = owners.get(path) if path is not None else None
            if target_id is None or target_id == source_id:
                continue
            source_interval, target_interval = intervals[source_id], intervals[target_id]
            if (
                source_interval[0] <= target_interval[0] < source_interval[1]
                or target_interval[0] <= source_interval[0] < target_interval[1]
            ):
                continue
            pair = (positions[source_id], positions[target_id])
            if pair not in references:
                reference_bytes += len(str(pair[0])) + len(str(pair[1])) + 3 + bool(references)
                if reference_bytes > MAX_WIKI_LINK_BYTES:
                    return WikiLinks(
                        (), (), f"wiki reference layer exceeds {MAX_WIKI_LINK_BYTES} bytes; no references were published",
                    )
                references.add(pair)
    return WikiLinks(
        tuple(sorted(pair for pair in references if pair[1] not in invalid_targets)),
        tuple(sorted(unavailable)),
    )


def write_wiki_links(
    path: Path,
    links: WikiLinks,
    *,
    index_sha256: str,
    warnings_out: list[str] | None = None,
) -> Path:
    """Publish a SHA-bound optional layer, explicitly omitting an oversized one.

    The search index and its dependencies remain byte-for-byte unchanged. A
    reader must verify its exact index digest before using these offsets.
    """
    if len(index_sha256) != 64 or any(character not in "0123456789abcdef" for character in index_sha256):
        raise ValueError("wiki references require the exact search index SHA-256")
    payload: dict[str, object] = {
        "schema": WIKI_LINKS_SCHEMA,
        "index_sha256": index_sha256,
        "availability": "partial" if links.unavailable_sources else "complete",
        "references": links.references,
        "unavailable_sources": links.unavailable_sources,
    }
    if links.unavailable_sources:
        payload["reason"] = "some captured article sources or publication routes were unavailable or unsupported"
    if links.reason is not None:
        payload.update(availability="unavailable", reason=links.reason)
    if len(_json_text(payload).encode("utf-8")) > MAX_WIKI_LINK_BYTES:
        reason = f"wiki reference layer exceeds {MAX_WIKI_LINK_BYTES} bytes; no references were published"
        payload.update(availability="unavailable", reason=reason, references=(), unavailable_sources=())
        if warnings_out is not None:
            warnings_out.append(reason)
    elif links.reason is not None and warnings_out is not None:
        warnings_out.append(f"wiki reference layer unavailable: {links.reason}")
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
    "SEARCH_SCHEMA",
    "drawn_view",
    "render_container",
    "uses_atlas",
    "viewer_script",
    "write_payload",
    "write_search_index",
]
