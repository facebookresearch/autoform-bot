"""The book's global search index and its optional wiki-link layer.

Publication writes ``dependencies/index.json`` (the search index) and, from
captured article bytes, ``dependencies/wiki-links.json``, bound to the index's
exact bytes. The scope payloads and the viewer container stay in dag_viewer,
which imports its state list, palette key, reading labels and JSON writer from
here.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from functools import lru_cache
from dataclasses import dataclass
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePath
from urllib.parse import unquote, urlsplit

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

from .. import markdown
from ..graph import Node
from ..status import DECLARATION_LABELS, INVENTORY_STATES, STATES, declaration_command
from .graph_views import FLATTENED_ROLES, INVENTORY_STATUSES, GraphView, ViewNode
from .publishable import html_names

SEARCH_SCHEMA = "autoform-dag-search/v1"
WIKI_LINKS_SCHEMA = "autoform-wiki-links/v1"
#: Every explorer page downloads the whole search index, so publication notes
#: when it passes this size. Nothing caps how large it may grow.
_SEARCH_INDEX_NOTE_BYTES = 32 * 1024 * 1024
_WIKI_BLOCK_MATH = re.compile(f"(?:{RE_DOLLAR_BLOCK}|{RE_BRACKET_BLOCK}|{RE_TEX_BLOCK})", re.DOTALL)
_WIKI_INLINE_MATH = re.compile(f"(?:{RE_SMART_DOLLAR_INLINE}|{RE_BRACKET_INLINE})", re.DOTALL)
_WIKI_INLINE_CODE = re.compile(BACKTICK_RE, re.DOTALL)
_WIKI_HEADING = HashHeaderProcessor.RE
#: Top-level sections whose links are provenance or bookkeeping, not mathematical references: a scope note says where
#: the rest of a milestone's source went, and Represented elsewhere lists results that other articles hold.
_WIKI_EXCLUDED = frozenset(
    {"sources", "references", "bibliography", "depends on", "proof depends on", "scope note", "represented elsewhere"}
)
_WIKI_EXCLUDED_HEADING = re.compile(r"#{1,2}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*")
_WIKI_SECTION_LINE = re.compile(r" {0,3}#{1,2}(?!#)| {0,3}(?:=+|-+)[ \t]*$")

_ALL_STATES = (*STATES, *INVENTORY_STATES)
_PALETTE_KEYS = frozenset(state.key for state in _ALL_STATES)


def _display_status(node: ViewNode) -> str:
    """Return the palette key a node is drawn in: a catalog's inventory status, else its own."""
    inventory = node.catalog == "module" and next(
        (key for key, count in node.status_counts if count and key in INVENTORY_STATUSES),
        None,
    )
    key = inventory or node.status_key or (node.status_counts[0][0] if node.status_counts else "planned")
    return key if key in _PALETTE_KEYS else "planned"


def _declaration_label(node: Node | ViewNode) -> str:
    """The book's caption for a statement, such as "Theorem", which the map shows too."""
    if node.catalog == "module":
        return "Module"
    # A command with no label reads as written; check warns about it.
    command = declaration_command(node.declaration)
    return DECLARATION_LABELS.get(command.casefold()) or command[:1].upper() + command[1:] or "Node"


def _json_text(payload: Mapping[str, object], *, ascii_only: bool = False) -> str:
    return json.dumps(payload, ensure_ascii=ascii_only, sort_keys=True, separators=(",", ":")) + "\n"


def _write_json(
    path: Path, payload: Mapping[str, object], *, sha256_out: list[str] | None = None,
    byte_count_out: list[int] | None = None, ascii_only: bool = False,
) -> Path:
    text = _json_text(payload, ascii_only=ascii_only)
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


# Unbounded: render reads each title for the search index and again for the
# map, and a book can hold more titles than any fixed size would keep.
@lru_cache(maxsize=None)
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


@lru_cache(maxsize=None)
def _shown_title(text: str) -> str:
    """A title with a Markdown escape as its page's heading shows it: ``foo\\_bar`` reads ``foo_bar``.

    Any other title is returned as written, and so is one with ``reading_title``
    parts, which are bound to its source and already read the escape, one whose
    heading holds a formula, or one whose heading would show nothing.
    """
    if "\\" not in text or len(text) > 16_384 or _reading_label(text) is not None:
        return text
    formulas: list[str] = []
    shown = markdown.page_title(f"# {text}", formula=lambda tex: formulas.append(tex) or tex)
    return text if formulas or not shown else shown


def _summary_led_by(summary: str | None, title: str, shown: str) -> str | None:
    """``summary`` opening with ``shown`` where it opens with ``title`` and then ``.`` or ``:``: the explorer and the
    map drop a summary's opening title by comparing it with the title they are given."""
    if summary and shown != title and summary.startswith(title) and re.match(r"[.:]\s", summary[len(title) :]):
        return shown + summary[len(title) :]
    return summary


def _openable_route(route: object) -> bool:
    """Whether the explorer's ``link()`` surely accepts *route*.

    Render writes relative routes. A route with a control character, a scheme
    or a leading slash may be refused or leave the site, so its row keeps the url.
    """
    if not isinstance(route, str) or not route or re.search(r"[\x00-\x1f\x7f]", route):
        return False
    # The URL parser skips leading spaces before it reads a scheme or a slash.
    return not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:|[/\\]", route.lstrip(" "))


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
    A row whose route the reader accepts omits its context ``url``, and only
    container rows carry ``data_url``, since pages name their own graphs.
    Parents and relations can reference positions in the emitted node array;
    the reader also accepts older string endpoints. This retains containment
    and every typed edge without repeating long article identifiers.
    ``sha256_out`` optionally receives the exact emitted bytes' digest without
    rereading any source or changing the index's wire representation.
    ``warnings_out`` notes when the emitted bytes pass
    ``_SEARCH_INDEX_NOTE_BYTES``, without refusing or trimming anything.
    """
    positions = {node.id: position for position, node in enumerate(view.nodes)}
    nodes: list[dict[str, object]] = []
    for node in view.nodes:
        title = _shown_title(node.title)
        entry: dict[str, object] = {
            "id": node.id,
            "kind": node.kind,
            "status": _display_status(node),
            "title": title,
        }
        # Search matches statements and Lean names as well as titles.
        if node.lean:
            entry["lean"] = node.lean
        if node.summary:
            entry["summary"] = _summary_led_by(node.summary, node.title, title)
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
                if key == "data_url" and node.kind != "scope":
                    # Every published page names its own graph data, so the reader needs no row's data_url.
                    continue
                if key == "parent" and isinstance(value, str):
                    value = positions.get(value, value)
                if value is not None:
                    entry[key] = value
            if type(details.get("result")) is bool:
                entry["result"] = details["result"]
        # Readers open a row's route and fall back to its url, so a row only needs
        # the url when it has no route the reader would accept.
        if not _openable_route(entry.get("route")):
            entry["url"] = context_links.get(node.id)
        nodes.append(entry)
    payload: dict[str, object] = {"nodes": nodes, "schema": SEARCH_SCHEMA}
    if include_relations:
        payload["relations"] = [
            [positions[edge.source], positions[edge.target], edge.statement_count, edge.proof_count]
            for edge in view.edges
        ]
    sizes: list[int] = []
    # ASCII text decodes into a one-byte string, half the memory of the two-byte string one symbol forces.
    written = _write_json(
        path, payload, sha256_out=sha256_out, byte_count_out=sizes if warnings_out is not None else None,
        ascii_only=True,
    )
    if warnings_out is not None and sizes[0] > _SEARCH_INDEX_NOTE_BYTES:
        warnings_out.append(
            f"search index is {sizes[0]:,} bytes; every explorer page downloads it, so first loads slow down "
            f"past {_SEARCH_INDEX_NOTE_BYTES:,} bytes"
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


def _excluded_heading(lines: list[str], n: int) -> bool:
    match = _WIKI_EXCLUDED_HEADING.fullmatch(lines[n])
    return bool(match) and match.group(1).strip().casefold() in _WIKI_EXCLUDED and (n == 0 or not lines[n - 1].strip())


def _no_wiki_targets(lines: list[str]) -> bool:
    """Whether ``_wiki_targets(lines)`` is surely empty, without rendering.

    A link needs "[" or "<". When none comes before the first excluded
    top-level heading, and every later top-level heading is excluded too, every
    link sits in an excluded section. Braces (attribute lists), character
    references and very deep nesting can make the converter refuse an article,
    so those take the full render.
    """
    for line in lines:
        if "{" in line or "&" in line or len(re.match(r"[ \t>]*", line).group().expandtabs(4)) >= 512:
            return False
    tail = next((n for n in range(len(lines)) if _excluded_heading(lines, n)), len(lines))
    if any("[" in line or "<" in line for line in lines[:tail]):
        return False
    return all(_excluded_heading(lines, n) for n in range(tail + 1, len(lines)) if _WIKI_SECTION_LINE.match(lines[n]))


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
    # A later H1 stays in the section the title or an H2 opened, as the loader
    # reads it, and one named as an excluded section, such as a provenance or
    # dependency section, also leaves out the links below it, as the page shows
    # them under it.
    later_titles = set([element for element in body if element.tag == "h1"][1:])

    def read(parent: object) -> None:
        excluded = section_excluded = False
        for element in parent:
            if element.tag in headings:
                if element.tag in {"h1", "h2"}:
                    named = "".join(element.itertext()).strip().casefold() in _WIKI_EXCLUDED
                    if element in later_titles:
                        excluded = section_excluded or named
                    else:
                        excluded = section_excluded = named
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
        # Most articles link only from their Sources: skip the render there.
        targets = [] if _no_wiki_targets(lines) else _wiki_targets(lines)
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
    """Publish a SHA-bound optional layer.

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
    if links.reason is not None and warnings_out is not None:
        warnings_out.append(f"wiki reference layer unavailable: {links.reason}")
    return _write_json(path, payload)


__all__ = [
    "SEARCH_SCHEMA",
    "WIKI_LINKS_SCHEMA",
    "WikiLinks",
    "extract_wiki_links",
    "write_search_index",
    "write_wiki_links",
]
