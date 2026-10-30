"""Multiresolution data for the whole-project map.

The map shows one project at several scales: regions (authored areas, or the
top-level chapters when a project has none), then the mathematical parts inside
them, then individual results. This module turns the records and typed
dependencies that the search index already publishes into small files the map
loads one level at a time, so no reader downloads the whole library to see its
outline.

Collection and pagination scopes organise files, not mathematics. They are
flattened away; a result keeps its collection as a facet.

A part's public API is the set of its results that results outside it use. Each
part lists its most-used exports and counts the rest, and sibling parts carry
the typed links between them.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

SCHEMA = "autoform-project-map/v1"
MAP_DIRECTORY = "dependencies/map"
TOP_EXPORTS = 8
# A part whose subtree holds at most this many results ships them in its own file.
LEAF_RESULTS = 400
# A part with more children than this gets them grouped into linked communities, so no level is a field of tiles.
GROUP_LIMIT = 30
_FLATTENED = frozenset({"collection", "transparent"})
_REGION = "region:"

Relation = tuple[str, str, int, int]


def part_file(part_id: str) -> str:
    """A stable, short file name for a part, so caches and links survive rebuilds."""
    return hashlib.sha256(part_id.encode("utf-8")).hexdigest()[:16] + ".json"


def build_project_map(records: Sequence[Mapping[str, object]], relations: Iterable[Relation]) -> dict[str, object]:
    """Return ``{relative path: payload}`` for the map of one project.

    ``records`` are search-index entries with at least ``id``, ``kind``,
    ``title`` and ``parent`` (an id); results are the entries whose ``result``
    is true (or, without that flag, every non-scope entry). ``relations`` are
    ``(user, used, statement links, proof links)`` between record ids.
    """
    by_id = {str(record["id"]): record for record in records}
    roots = [key for key, record in by_id.items() if record.get("parent") in (None, "")]
    if len(roots) != 1:
        raise ValueError(f"a project map needs exactly one root, found {len(roots)}")
    root = roots[0]
    children: dict[str, list[str]] = defaultdict(list)
    for key, record in by_id.items():
        parent = record.get("parent")
        if parent not in (None, "") and str(parent) in by_id:
            children[str(parent)].append(key)

    def is_result(record: Mapping[str, object]) -> bool:
        flag = record.get("result")
        return flag is True if isinstance(flag, bool) else record.get("kind") != "scope"

    part_parent: dict[str, str] = {}
    home: dict[str, str] = {}
    facet: dict[str, str] = {}

    def walk(node: str, part: str, collection: str | None) -> None:
        for child in sorted(children.get(node, ())):
            record = by_id[child]
            if record.get("kind") == "scope":
                navigation = record.get("navigation")
                if navigation in _FLATTENED:
                    walk(child, part, str(record.get("title")) if navigation == "collection" else collection)
                else:
                    part_parent[child] = part
                    walk(child, child, collection)
            elif is_result(record):
                home[child] = part
                if collection:
                    facet[child] = collection

    walk(root, root, None)

    title = {part: str(by_id[part].get("title") or part) for part in part_parent}
    title[root] = str(by_id[root].get("title") or root)
    chapters = sorted(part for part, parent in part_parent.items() if parent == root)
    area_of = {chapter: str(by_id[chapter]["area"]) for chapter in chapters if by_id[chapter].get("area")}
    if len(set(area_of.values())) >= 2:
        members: dict[str, list[str]] = defaultdict(list)
        for chapter in chapters:
            members[area_of.get(chapter, "Other")].append(chapter)
        for area, inside_area in members.items():
            # An area holding a single chapter adds a level without adding structure: the chapter stands for it.
            if len(inside_area) == 1:
                continue
            region = _REGION + area
            part_parent[region] = root
            title[region] = area
            for chapter in inside_area:
                part_parent[chapter] = region

    def chain(part: str) -> list[str]:
        out: list[str] = []
        while part != root:
            out.append(part)
            part = part_parent[part]
        return out  # nearest first

    def aggregate() -> tuple:
        chains = {result: chain(part) for result, part in home.items()}
        inside: dict[str, list[str]] = defaultdict(list)
        for result, parts in chains.items():
            for part in parts:
                inside[part].append(result)
        exports: dict[str, Counter[str]] = defaultdict(Counter)
        imports: dict[str, set[str]] = defaultdict(set)
        pair: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        internal: dict[str, list[list[object]]] = defaultdict(list)
        boundary: dict[str, list[list[object]]] = defaultdict(list)
        for user, used, statement, proof in relations:
            if user not in chains or used not in chains or user == used:
                continue
            user_parts, used_parts = chains[user], chains[used]
            user_set, used_set = set(user_parts), set(used_parts)
            for part in used_parts:
                if part not in user_set:
                    exports[part][used] += 1
            for part in user_parts:
                if part not in used_set:
                    imports[part].add(used)
            # The aggregated edge belongs to the two siblings just below the common ancestor.
            top_user, top_used = user_parts[::-1], used_parts[::-1]
            depth = 0
            while depth < min(len(top_user), len(top_used)) and top_user[depth] == top_used[depth]:
                depth += 1
            if depth < len(top_user) and depth < len(top_used):
                weights = pair[(top_user[depth], top_used[depth])]
                weights[0] += statement
                weights[1] += proof
            row = [user, used, statement, proof]
            if home[user] == home[used]:
                internal[home[user]].append(row)
            else:
                boundary[home[user]].append(row)
                boundary[home[used]].append(row)
        return chains, inside, exports, imports, pair, internal, boundary

    relations = list(relations)
    chains, inside, exports, imports, pair, internal, boundary = aggregate()
    synthetic: set[str] = set()
    if _group_wide_parts(part_parent, title, inside, pair, synthetic):
        chains, inside, exports, imports, pair, internal, boundary = aggregate()
    status = {part: Counter(str(by_id[result].get("status")) for result in results) for part, results in inside.items()}

    kids: dict[str, list[str]] = defaultdict(list)
    for part, parent in part_parent.items():
        kids[parent].append(part)
    for parent in kids:
        kids[parent].sort(key=lambda part: (-len(inside.get(part, ())), title[part]))

    def summary(part: str) -> dict[str, object]:
        exported = exports.get(part, Counter())
        return {
            "id": part, "title": title[part], "results": len(inside.get(part, ())),
            "status": dict(sorted(status.get(part, Counter()).items())), "parts": len(kids.get(part, ())),
            "api": [[result, str(by_id[result].get("title")), uses] for result, uses in exported.most_common(TOP_EXPORTS)],
            "api_total": len(exported), "imports_total": len(imports.get(part, ())),
            "file": part_file(part),
            **({"grouped": True} if part in synthetic else {}),
        }

    def among(parts: Sequence[str]) -> list[list[object]]:
        members = set(parts)
        return [[user, used, w[0], w[1]] for (user, used), w in sorted(pair.items()) if user in members and used in members]

    def result_row(result: str) -> dict[str, object]:
        record = by_id[result]
        row: dict[str, object] = {"id": result, "title": record.get("title"), "status": record.get("status")}
        for key in ("lean", "summary", "reading_title", "route"):
            if record.get(key):
                row[key] = record[key]
        if result in facet:
            row["facet"] = facet[result]
        return row

    files: dict[str, object] = {}
    for part in part_parent:
        own = sorted(result for result, at in home.items() if at == part)
        body: dict[str, object] = {
            "schema": SCHEMA, "part": summary(part),
            "path": [[step, title[step]] for step in chain(part)[::-1]],
            "children": [summary(child) for child in kids.get(part, ())],
            "edges": among(kids.get(part, ())),
        }
        if own or len(inside.get(part, ())) <= LEAF_RESULTS and not kids.get(part):
            outside: dict[str, list[object]] = {}
            for user, used, _statement, _proof in boundary.get(part, ()):
                other = str(used if home[str(user)] == part else user)
                # The other end's parts, outermost first, so the map can draw to whatever level is open.
                outside[other] = [other, by_id[other].get("title"), chains[other][::-1]]
            body["results"] = [result_row(result) for result in own]
            body["result_edges"] = internal.get(part, [])
            body["boundary_edges"] = boundary.get(part, [])
            body["outside"] = sorted(outside.values(), key=lambda row: str(row[0]))
        files[f"parts/{part_file(part)}"] = body

    regions = kids.get(root, [])
    part_ids = sorted(part_parent)
    position = {part: index for index, part in enumerate(part_ids)}
    files["index.json"] = {
        "schema": SCHEMA, "title": title[root], "results": len(home),
        "status": dict(sorted(Counter(str(by_id[result].get("status")) for result in home).items())),
        "regions": [summary(region) for region in regions], "region_edges": among(regions),
        "facets": sorted(set(facet.values())),
    }
    files["search.json"] = {
        "schema": SCHEMA,
        # Each part with its parent's position (-1 under the root), so search can rebuild any path.
        "parts": [[part, title[part], part_file(part), position.get(part_parent[part], -1)] for part in part_ids],
        # Title, Lean name, the result's own part, and its id (relative to that part when it extends it).
        "rows": [
            [by_id[result].get("title"), by_id[result].get("lean") or "", position[at],
             result[len(at) + 1:] if result.startswith(at + "/") else result]
            for result, at in sorted(home.items())
        ],
    }
    return files


def _group_wide_parts(part_parent: dict[str, str], title: dict[str, str], inside: Mapping[str, Sequence[str]],
                      pair: Mapping[tuple[str, str], Sequence[int]], synthetic: set[str]) -> bool:
    """Group the children of any part wider than GROUP_LIMIT into linked communities, in place.

    Children that link to each other form one community (a weak component of the sibling links), titled by its largest
    member. Children that link to no sibling share one "Standalone topics" group, in reading order. Each group gets a
    stable id under its parent. Returns whether anything was grouped.
    """
    kids: dict[str, list[str]] = defaultdict(list)
    for part, parent in part_parent.items():
        kids[parent].append(part)
    changed = False
    for parent, members in sorted(kids.items()):
        if len(members) <= GROUP_LIMIT:
            continue
        member_set = set(members)
        neighbours: dict[str, set[str]] = defaultdict(set)
        for (user, used), _weights in pair.items():
            if user in member_set and used in member_set:
                neighbours[user].add(used)
                neighbours[used].add(user)
        size = {member: len(inside.get(member, ())) for member in members}
        order = sorted(members, key=lambda member: (-size[member], title[member], member))
        seen: set[str] = set()
        islands: list[list[str]] = []
        loners: list[str] = []
        for member in order:
            if member in seen:
                continue
            if not neighbours.get(member):
                seen.add(member)
                loners.append(member)
                continue
            component, stack = [], [member]
            seen.add(member)
            while stack:
                at = stack.pop()
                component.append(at)
                for other in sorted(neighbours[at]):
                    if other not in seen:
                        seen.add(other)
                        stack.append(other)
            islands.append(sorted(component, key=lambda m: (-size[m], title[m], m)))
        groups = [island for island in islands if len(island) > 1]
        loners.extend(island[0] for island in islands if len(island) == 1)
        loners.sort(key=lambda m: (title[m].casefold(), m))
        # Topics that link to no sibling stay together, in reading order: grouping them by size would invent a structure.
        everything = groups + ([loners] if loners else [])
        if len(everything) >= len(members):
            continue
        for number, group in enumerate(everything, start=1):
            group_id = f"{parent}/~{number}"
            names = [title[m] for m in group[:2]]
            if number <= len(groups):
                if len(group) > 2:
                    title[group_id] = f"{names[0]} and {len(group) - 1} linked topics"
                else:
                    title[group_id] = f"{names[0]} (2 topics)" if names[0] == names[1] else " and ".join(names)
            else:
                title[group_id] = "Standalone topics"
            part_parent[group_id] = parent
            synthetic.add(group_id)
            for member in group:
                part_parent[member] = group_id
        changed = True
    return changed


def write_project_map(directory: Path, files: Mapping[str, object]) -> tuple[Path, ...]:
    """Write ``files`` under ``directory`` as compact UTF-8 JSON, returning the paths written."""
    written: list[Path] = []
    for relative, payload in sorted(files.items()):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        written.append(path)
    return tuple(written)


def records_from_view(view: object, metadata: Mapping[str, Mapping[str, object]]) -> tuple[list[dict[str, object]], list[Relation]]:
    """The map's records and (user, used) relations from the publisher's complete view and its index metadata."""
    from . import dag_viewer  # local import: dag_viewer imports the renderer stack

    records: list[dict[str, object]] = []
    for node in view.nodes:  # type: ignore[attr-defined]
        record: dict[str, object] = {"id": node.id, "kind": node.kind, "title": node.title, "status": dag_viewer._display_status(node)}
        if node.lean:
            record["lean"] = node.lean
        if node.summary:
            record["summary"] = node.summary
        label = dag_viewer._reading_label(node.title)
        if label is not None:
            record["reading_title"] = label
        for key in ("parent", "navigation", "area", "route", "result"):
            value = metadata.get(node.id, {}).get(key)
            if value is not None:
                record[key] = value
        records.append(record)
    # A view edge runs from the dependency to the result that uses it.
    relations = [(edge.target, edge.source, edge.statement_count, edge.proof_count) for edge in view.edges]  # type: ignore[attr-defined]
    return records, relations


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · map</title>
<link rel="stylesheet" href="{style}">
<script>window.MathJax = window.MathJax || {{tex: {{inlineMath: [["$", "$"], ["\\\\(", "\\\\)"]]}}, startup: {{typeset: false}}}};</script>
<script defer src="{mathjax}"></script>
<script defer src="{script}"></script>
</head>
<body class="pm-page">
<div data-project-map data-map="{map_base}" data-routes="{routes}" data-home="{home}">
<noscript><p>The map needs JavaScript. <a href="{home}">Read the book</a> instead.</p></noscript>
</div>
</body>
</html>
"""


def render_page(*, title: str, map_base: str, routes: str, home: str, script: str, style: str, mathjax: str) -> str:
    """The standalone map page; every argument is a link relative to the page."""
    import html as _html

    values = {key: _html.escape(value, quote=True) for key, value in
              {"title": title, "map_base": map_base, "routes": routes, "home": home, "script": script, "style": style, "mathjax": mathjax}.items()}
    return _PAGE.format(**values)
