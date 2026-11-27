"""Where reading links as the site does would change what check or audit says, warned about for one release.

From 0.9.0 the graph, check and audit read an article's links with :func:`~autoform_cli.markdown.site_links`, the
site's own converter, and today's reader goes. Until then check and audit read both and warn wherever the two part
on an outcome: a dependency, proof or informal edge that appears, vanishes or changes section; a link only the site
shows that would refuse the vault (a target that does not resolve, a cycle, an unpublished folder); and a
``## Sources`` or coverage link that check or audit would judge otherwise. One warning per link, or one per heading
where the site reads a heading otherwise. A link both readers count alike warns about nothing, nor does one only the
site shows that neither judges, such as an autolink to an external source.
"""

from __future__ import annotations

import os
import re
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ._fs import file_or_unexaminable
from ._fs import is_within as _is_within
from .graph import (
    _INFORMAL_SECTION,
    _PROOF_SECTION,
    _SOURCES_SECTION,
    _STATEMENT_SECTION,
    Node,
    _ParsedNode,
    _resolve_target,
)
from .markdown import SiteLink, SiteReadingError, _is_hidden_path, local_target_issue, markdown_links, site_links

_DEPENDENCY_SECTIONS = (_STATEMENT_SECTION, _PROOF_SECTION)
_SECTIONS = (*_DEPENDENCY_SECTIONS, _INFORMAL_SECTION, _SOURCES_SECTION)
#: Each section as its heading writes it, what a link under it counts as, and what its links are.
_NAMES = {
    _STATEMENT_SECTION: ("Depends on", "a dependency", "dependencies"),
    _PROOF_SECTION: ("Proof depends on", "a proof dependency", "proof dependencies"),
    _INFORMAL_SECTION: ("Informally uses", "an informal use", "informal uses"),
    _SOURCES_SECTION: ("Sources", "a source", "sources"),
}
#: The ``## Sources`` problems check refuses a vault for; audit reports the rest.
_CHECK_REFUSES = frozenset(
    {
        "source-not-found",
        "source-escapes-blueprint",
        "malformed-source-url",
        "malformed-source-link",
        "hidden-source-target",
        "generated-source-target",
    }
)
#: A heading's text as written, past quote markers, ``#`` marks, closing marks and an attribute list.
_HEADING_TEXT = re.compile(r"^[ \t>]*(?:#{1,6}(?!#))?(.*?)(?:[ \t]+#+)?[ \t]*(?:\{[^{}]*\})?[ \t]*$")
_SETEXT = re.compile(r"^[ \t>]*(?:-+|=+)[ \t]*$")
_SETEXT_H1 = re.compile(r"^[ \t>]*=+[ \t]*$")
_H1 = re.compile(r"^[ \t>]*#(?!#)")


def read(text: str) -> list[SiteLink] | SiteReadingError:
    """The links the site shows on the page *text* writes, or why its converter cannot read the page."""

    try:
        return site_links(text)
    except SiteReadingError as error:
        return error


def section_key(name: str | None) -> str | None:
    """A section's name as a reader sees it: blanks collapsed, as a browser draws them, and casefolded."""

    return None if name is None else " ".join(name.split()).casefold()


def differs(parsed: _ParsedNode, links: list[SiteLink] | SiteReadingError) -> bool:
    """Whether the site may count *parsed*'s links otherwise than the loader: a cheap test most pages pass."""

    if isinstance(links, SiteReadingError) or parsed.wrapped_targets or parsed.wrapped_sources:
        return True
    site: dict[str, set[str]] = {section: set() for section in _SECTIONS}
    for link in links:
        if (key := section_key(link.section)) in site:
            site[key].add(link.href)
    loader: dict[str, set[str]] = {section: set() for section in _SECTIONS}
    for section, _, target in parsed.read_links:
        loader[section].add(target)
    return site != loader


@dataclass(frozen=True, slots=True)
class _Change:
    """One link the two readers count otherwise, before it is worded."""

    #: The section the change is in, or "" for a link outside the four.
    section: str
    #: The line it stands on, or None.
    line: int | None
    #: The site's address, or None where the site shows no link.
    href: str | None
    #: The loader's target, or None where it reads no link.
    target: str | None
    #: What 0.9.0 says about the site's link instead of counting it, or None where it counts it.
    problem: str | None = None
    #: Whether the problem is check's error, rather than audit's finding or check's warning.
    error: bool = True


def warnings(
    site_read: Mapping[str, tuple[str, list[SiteLink] | SiteReadingError, bool]],
    parsed: Mapping[str, _ParsedNode],
    nodes: Mapping[str, Node],
    informal_named: Mapping[str, list[str]],
    blueprint: Path,
    canonical_ids: Mapping[Path, str],
    unpublished: tuple[Path, ...],
) -> list[tuple[str, str]]:
    """``(node id, warning)`` for each link in *site_read*, ``(text, links, differs)`` by article, that reading
    links as the site does counts otherwise."""

    omitted = {node_id for node_id, node in nodes.items() if any(_is_within(node.path, f) for f in unpublished)}
    found: list[tuple[str, int, str]] = []
    changed: dict[str, list[_Change]] = {}
    future: dict[str, set[str]] = {}
    for node_id, (text, links, differ) in site_read.items():
        if isinstance(links, SiteReadingError):
            found.append((node_id, 0, f"{links}; from 0.9.0 autoform reads no link on this page"))
            continue
        changes: list[_Change] = []
        if differ:
            changes, future[node_id] = _changes(
                parsed[node_id], nodes[node_id], links, informal_named[node_id], blueprint, canonical_ids, omitted
            )
        if unpublished and node_id not in omitted:
            changes += _unpublished_changes(parsed[node_id], text, links, unpublished)
        if changes:
            changed[node_id] = changes
    # A dependency only the site counts can close a cycle, alone or with other such dependencies.
    graph = {node_id: set(node.dependencies) for node_id, node in nodes.items()} | future
    for node_id, changes in changed.items():
        article = parsed[node_id]
        text, links, _ = site_read[node_id]
        lines = text.splitlines()
        grouped: dict[tuple[object, ...], tuple[int, str, list[tuple[str | None, str | None]]]] = {}
        for change in changes:
            change = _with_cycle(change, article, nodes[node_id], graph, blueprint, canonical_ids)
            moved = None if change.problem else _moved(change, article, links)
            if moved is None:
                found.append((node_id, change.line or 0, _link_warning(change)))
                continue
            key, line, cause = _heading(moved, article, lines)
            grouped.setdefault(key, (line, cause, []))[2].append((moved[1], moved[2]))
        found.extend((node_id, line, _heading_warning(line, cause, moves)) for line, cause, moves in grouped.values())
    order = {node_id: index for index, node_id in enumerate(nodes)}
    found.sort(key=lambda item: (order[item[0]], item[1]))
    return [(node_id, warning) for node_id, _, warning in found]


def _changes(
    article: _ParsedNode,
    node: Node,
    links: list[SiteLink],
    informal: list[str],
    blueprint: Path,
    canonical_ids: Mapping[Path, str],
    omitted: set[str],
) -> tuple[list[_Change], set[str]]:
    """What the site counts otherwise on one article, and the dependencies it counts there."""

    site: dict[str, list[SiteLink]] = {section: [] for section in _SECTIONS}
    for link in links:
        if (key := section_key(link.section)) in site:
            site[key].append(link)
    read: dict[str, list[tuple[int, str]]] = {section: [] for section in _SECTIONS}
    for section, line, target in article.read_links:
        read[section].append((line, target))

    changes: list[_Change] = []
    edges: set[str] = set()
    counts = {
        _STATEMENT_SECTION: node.statement_dependencies,
        _PROOF_SECTION: node.proof_dependencies,
        _INFORMAL_SECTION: informal,
    }
    for section, counted_today in counts.items():
        informally = section == _INFORMAL_SECTION
        label = "informal target" if informally else "dependency target"
        counted = set(counted_today)
        written = {target for _, target in read[section]}
        shown: dict[str, SiteLink] = {}
        for link in site[section]:
            dependency, issue = _resolve_target(article, link.href, blueprint, canonical_ids, label)
            if issue is None and dependency == article.id:
                issue = f"{article.id}: {'informally uses' if informally else 'dependency on'} itself"
            if issue is None and not informally and dependency in omitted and article.id not in omitted:
                issue = f"{article.id}: links to {link.href!r}, in an unpublished folder render omits"
            if issue is None:
                if not informally:
                    edges.add(dependency)
                shown.setdefault(dependency, link)
                continue
            # A link the loader reads too refuses today, or warns: wrapped (#325), or informal.
            if link.href not in written:
                problem = _problem(issue, article.id, link.href)
                changes.append(_Change(section, link.line, link.href, None, problem, not informally))
        # A wrapped dependency the loader does not count today, as it closes a cycle or reaches an unpublished
        # folder, warns already.
        changes += [
            _Change(section, link.line, link.href, None)
            for dep, link in shown.items()
            if dep not in counted and link.href not in article.wrapped_targets
        ]
        for line, target in read[section]:
            dependency, issue = _resolve_target(article, target, blueprint, canonical_ids, label)
            if issue is None and dependency in counted and dependency not in shown:
                counted.discard(dependency)  # one warning for each, at its first link
                changes.append(_Change(section, line, None, target))
    changes = _paired(changes)

    wrapped_sources = set(node.wrapped_sources) - set(node.sources)
    read_sources = [(line, target, target in wrapped_sources) for line, target in read[_SOURCES_SECTION]]
    changes += _target_changes(
        _SOURCES_SECTION,
        site[_SOURCES_SECTION],
        read_sources,
        lambda target: _source_problem(node.path, target, blueprint),
    )
    return changes, edges


def _paired(changes: list[_Change]) -> list[_Change]:
    """*changes*, with a link the site reads one way and the loader another, in one section on one line, as one."""

    gone: dict[tuple[str, int | None], _Change] = {}
    for change in changes:
        if change.href is None:
            gone.setdefault((change.section, change.line), change)
    paired = []
    for change in changes:
        if change.href is not None and change.target is None and change.line is not None:
            other = gone.pop((change.section, change.line), None)
            change = change if other is None else replace(change, target=other.target)
        paired.append(change)
    return [change for change in paired if change.href is not None or gone.get((change.section, change.line))]


def _target_changes(
    section: str,
    links: list[SiteLink],
    read: list[tuple[int, str, bool]],
    judge: Callable[[str], tuple[str, bool] | None],
) -> list[_Change]:
    """The links check or audit judges otherwise once it reads *links*, the site's, not *read*, the loader's
    ``(line, target, wrapped)``: *judge* says what it reports about a target, and whether it is check's error."""

    counted = {target for _, target, _ in read}
    hrefs = {link.href for link in links}
    gone: dict[int, tuple[int, str]] = {}
    for line, target, wrapped in read:
        if not wrapped and target not in hrefs and line not in gone:
            gone[line] = (line, target)
            hrefs.add(target)  # one change for each target, at its first link
    changes: list[_Change] = []
    for link in links:
        if link.href in counted:
            continue  # alike, or wrapped: warned about already (#325)
        before = gone.pop(link.line, None) if link.line is not None else None
        problem = judge(link.href)
        old = before and judge(before[1])
        if problem or old:
            problem, error = problem or (None, True)
            target = before[1] if before else None
            changes.append(_Change(section, link.line, link.href, target, problem, error))
    changes += [_Change(section, line, None, target) for line, target in gone.values() if judge(target)]
    return changes


def _unpublished_changes(
    article: _ParsedNode, text: str, links: list[SiteLink], unpublished: tuple[Path, ...]
) -> list[_Change]:
    """The links outside the dependency sections into an unpublished folder that only the site counts."""

    changes: list[_Change] = []
    read: set[str] | None = None
    for link in links:
        if section_key(link.section) in _DEPENDENCY_SECTIONS:
            continue  # with the dependencies
        try:
            split = urlsplit(link.href)
        except ValueError:
            continue
        spelled = Path(os.path.normpath(article.path.parent / unquote(split.path)))
        if split.scheme or split.netloc or not any(_is_within(spelled, folder) for folder in unpublished):
            continue
        if read is None:
            read = {target for _, target, _ in markdown_links(text)}
        if link.href in read:
            continue  # refused today, or warned about, as a wrapped link (#325)
        problem = f"links to {link.href!r}, in an unpublished folder render omits"
        section = section_key(link.section)
        changes.append(_Change(section if section in _NAMES else "", link.line, link.href, None, problem))
    return changes


def _with_cycle(
    change: _Change,
    article: _ParsedNode,
    node: Node,
    graph: Mapping[str, set[str]],
    blueprint: Path,
    canonical_ids: Mapping[Path, str],
) -> _Change:
    """*change*, with the cycle the dependency only the site counts closes as its problem."""

    if change.problem is not None or change.href is None or change.section not in _DEPENDENCY_SECTIONS:
        return change
    dependency, issue = _resolve_target(article, change.href, blueprint, canonical_ids)
    if issue is not None or dependency in node.dependencies:
        return change
    path = _path(graph, dependency, node.id)
    if path is None:
        return change
    return replace(change, problem=f"dependency cycle: {' -> '.join([node.id, *path])}")


def _source_problem(path: Path, target: str, blueprint: Path) -> tuple[str, bool] | None:
    """What check or audit says about a ``## Sources`` link to *target*, and whether check refuses it."""

    issue = local_target_issue(path, target, blueprint, label="source")
    if issue is None:
        return None
    located = local_target_issue(path, target.partition("#")[0], blueprint, label="source")
    return _problem(issue[1], "", target), located is not None and located[0] in _CHECK_REFUSES


def _problem(issue: str, node_id: str, target: str) -> str:
    """*issue* without the node id it starts with and the target it ends with, which the warning names."""

    return issue.removeprefix(f"{node_id}: ").removesuffix(f": {target!r}")


def _path(graph: Mapping[str, set[str]], start: str, goal: str) -> list[str] | None:
    """The articles from *start* to *goal* along *graph*'s dependencies, both included, or None."""

    before: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current == goal:
            path = [current]
            while (previous := before[path[-1]]) is not None:
                path.append(previous)
            return path[::-1]
        for dependency in sorted(graph.get(current, ())):
            if dependency not in before:
                before[dependency] = current
                queue.append(dependency)
    return None


def _loader_section(article: _ParsedNode, line: int) -> tuple[str | None, tuple[int, str, str | None] | None]:
    """The section the loader reads *line* in, and the heading that opens it, or None before any."""

    opening = None
    for heading in article.read_headings:
        if heading[0] >= line:
            break
        if heading[1] != "h1":
            opening = heading
    return (opening[2] if opening else None), opening


def _moved(change: _Change, article: _ParsedNode, links: list[SiteLink]) -> tuple[int, str | None, str | None] | None:
    """``(line, loader's section, site's section)`` where the readers place *change*'s link under different
    headings, or None where they part on the link itself."""

    if change.href is not None:
        if change.line is not None and change.target is None:
            loader = _loader_section(article, change.line)[0]
            if loader != change.section:
                return change.line, loader, change.section
        return None
    for link in links:
        if link.href == change.target and link.line is not None:
            site = section_key(link.section)
            if site != change.section and _loader_section(article, link.line)[0] == change.section:
                return link.line, change.section, site
    return None


def _heading(moved: tuple[int, str | None, str | None], article: _ParsedNode, lines: list[str]) -> tuple:
    """The heading the readers part on above a moved link: a key for it, its line, and the cause, worded."""

    line, loader, site = moved
    opening = _loader_section(article, line)[1]
    start = opening[0] if opening else 0
    later = [heading[0] for heading in article.read_headings if heading[1] == "h1" and start < heading[0] < line]
    if site is None and later:
        written = lines[later[0] - 1].strip()
        cause = f"the site ends the section at the later H1 '{written}' and autoform does not"
        return ("h1", later[0]), later[0], cause
    shown = None
    for index in range(line - 2, -1, -1):
        text = lines[index]
        if site is None:
            below = lines[index + 1] if index + 1 < len(lines) else ""
            heading = _H1.match(text) is not None or (bool(text.strip()) and _SETEXT_H1.match(below) is not None)
        else:
            heading = bool(text.strip()) and section_key(_HEADING_TEXT.match(text)[1]) == site
        if heading:
            shown = index + 1
            break
    if shown is not None and opening is not None and shown == start and opening[1] == "h2":
        written = lines[shown - 1].strip()
        return (
            ("renamed", shown),
            shown,
            f"the site names the heading '{written}' {_name(site)!r} and autoform {_name(loader)!r}",
        )
    if shown is not None and shown > start:
        written = lines[shown - 1].strip()
        under = lines[shown].strip() if shown < len(lines) and _SETEXT.match(lines[shown]) else ""
        over = f" over '{under}'" if under else ""
        return ("site", shown), shown, f"the site reads '{written}'{over} as a heading and autoform does not"
    if opening is not None and opening[1] == "h2":
        written = lines[start - 1].rstrip()
        indented = ", as it is indented" if written.startswith((" ", "\t")) else ""
        return ("loader", start), start, f"autoform reads '{written}' as a heading and the site does not{indented}"
    return (
        ("site", site),
        line,
        f"the site reads these links under a heading{f' {site!r}' if site else ''} autoform does not",
    )


def _name(section: str | None) -> str:
    """*section* as its heading writes it, where it is one of the four, or as a reader sees it."""

    return _NAMES[section][0] if section in _NAMES else section or ""


def _heading_warning(line: int, cause: str, moves: list[tuple[str | None, str | None]]) -> str:
    """One warning for a heading the readers part on, naming what its links do from 0.9.0."""

    effects = []
    for loader, site in dict.fromkeys(moves):
        if cause.startswith("autoform reads"):
            # The site reads no heading here: its links stay in the section above it.
            if loader in _NAMES and site in _NAMES:
                effects.append(f"count as {_NAMES[site][2]}, not {_NAMES[loader][2]}")
            elif loader in _NAMES:
                effects.append(f"stop counting as {_NAMES[loader][2]}")
            else:
                effects.append(f"count as {_NAMES[site][2]}")
        elif loader in _NAMES and site in _NAMES:
            effects.append(f"move from '## {_NAMES[loader][0]}' to '## {_NAMES[site][0]}'")
        elif loader in _NAMES:
            effects.append(f"leave '## {_NAMES[loader][0]}'")
        else:
            effects.append(f"count under '## {_NAMES[site][0]}'")
    effect = " and ".join(dict.fromkeys(effects))
    if cause.startswith("autoform reads"):
        return f"line {line}: {cause}; from 0.9.0 its links {effect}"
    return f"line {line}: {cause}; from 0.9.0 sections follow the site's headings, so the links below it {effect}"


def _link_warning(change: _Change) -> str:
    """One warning for a link the readers count otherwise, after the line it stands on."""

    at = f"line {change.line}: " if change.line else ""
    under = f" under '## {_NAMES[change.section][0]}'" if change.section in _NAMES else ""
    if change.href is None:
        return (
            f"{at}the site shows no link here, so from 0.9.0 {change.target!r} stops counting{under}; write it as a "
            "link the page shows, or delete it"
        )
    if change.problem is None:
        consequence = f"it counts as {_NAMES[change.section][1]}"
    elif change.section == _INFORMAL_SECTION:
        consequence = f"check warns: {change.problem}"
    elif change.error:
        consequence = f"it is an error: {change.problem}"
    else:
        consequence = f"audit reports it: {change.problem}"
    if change.target is not None:
        tail = "" if change.problem is None else f", and {consequence}"
        return (
            f"{at}the site links {change.href!r}, autoform reads {change.target!r}; from 0.9.0 autoform reads it as "
            f"the site does{tail}"
        )
    return f"{at}the site shows a link to {change.href!r}{under} that autoform does not read; from 0.9.0 {consequence}"


def coverage_warnings(blueprint: Path, unpublished: tuple[Path, ...]) -> list[tuple[Path, str]]:
    """``(file, warning)`` for each link in ``coverage/`` that audit judges otherwise once it reads as the site does."""

    root = blueprint / "coverage"
    found: list[tuple[Path, str]] = []
    paths = sorted(root.rglob("*.md")) if root.is_dir() else []
    for path in paths:
        if not file_or_unexaminable(path) or _is_hidden_path(path, blueprint):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue  # audit reports it
        links = read(text)
        if isinstance(links, SiteReadingError):
            found.append((path, f"{links}; from 0.9.0 audit reads no link on this page"))
            continue

        def judge(target: str, path: Path = path) -> tuple[str, bool] | None:
            issue = local_target_issue(path, target, blueprint, label="coverage", unpublished=unpublished)
            return None if issue is None else (_problem(issue[1], "", target), False)

        changes = _target_changes("", links, markdown_links(text), judge)
        found.extend((path, _link_warning(change)) for change in changes)
    return found
