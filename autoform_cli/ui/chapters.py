"""A chapter's page: its layout and stretches, where each statement's card goes, the overview's summary, and the
page put together."""

from __future__ import annotations

import html
import re
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .. import status
from .._fs import resolve as _resolve
from ..graph import Graph, _is_countable
from ..lean import SourceLinker
from ..markdown import HEADING as _HEADING
from ..markdown import content_lines as _content_lines
from ..markdown import title_text, yaml_string
from .markdown_html import _as_fenced, _convert_body, _protect_narrative_markdown
from .page_links import _LINK_LABEL, _anchored_links, _link_mask
from .statement_box import _render_environment


_ARTICLE_SLOT = re.compile(
    rf"^(?P<indent>[ \t]*)[-*+]\s+\[(?=[^\]]){_LINK_LABEL}\]"
    r"\(\s*(?P<target>[^)\s]+)(?:\s+[^)]*)?\)\s*$"
)


def _render_overview_summary(
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    *,
    containers: frozenset[str],
    node_ids: list[str] | None = None,
) -> str:
    """Render a chapter's progress as one line, or nothing for a single statement.

    A lone statement's card already shows its status, and the module
    inventories have their own section, so neither is counted here. A project
    that tracks no Lean has no progress to count.
    """
    if not graph.tracks_lean:
        return ""
    candidates = list(node_ids if node_ids is not None else graph.nodes)
    selected = {
        node_id: statuses[node_id]
        for node_id in candidates
        if _is_countable(graph, node_id, containers)
    }
    if len(selected) < 2:
        return ""
    parts = [
        f'<span class="bp-progress-state" title="{html.escape(status.MEANINGS[state.key], quote=True)}">'
        f'<span class="bp-swatch bp-swatch-{state.key}"></span>'
        f"<strong>{count:,}</strong> {html.escape(state.label)}</span>"
        for state, count in status.summarize(selected)
    ]
    return (
        '<div class="bp-progress-overview">'
        f'<div class="bp-progress-states">{"".join(parts)}</div>'
        "</div>"
    )


def _render_chapter(
    group: str,
    node_ids: list[str],
    *,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    used_by: dict[str, list[str]],
    informally_used_by: dict[str, list[str]],
    formalizing: Mapping[str, str],
    linker: SourceLinker,
    page: Path,
    targets: dict[str, tuple[Path, str]],
    narrative: str | None,
    blueprint: Path,
    repo_root: Path,
    node_sources: dict[Path, str],
    containers: frozenset[str],
    statements: dict[str, tuple[str, str]],
    reviews: dict[str, str],
    typed: bool = False,
) -> tuple[str, int, list[str]]:
    """Render one narrative article with statements at its authored link slots."""
    links = _anchored_links(targets, page)
    linked = 0
    unresolved: list[str] = []
    environments: dict[str, str] = {}
    for node_id in node_ids:
        environment, node_linked, node_unresolved = _render_environment(
            graph.nodes[node_id],
            anchor=targets[node_id][1],
            graph=graph,
            statuses=statuses,
            used_by=used_by,
            informally_used_by=informally_used_by,
            formalizing=formalizing,
            linker=linker,
            links=links,
            repo_root=repo_root,
            statements=statements,
            reviews=reviews,
        )
        environments[node_id] = environment
        linked += node_linked
        unresolved.extend(node_unresolved)

    chapter_summary = _render_overview_summary(
        graph,
        statuses,
        containers=containers,
        node_ids=node_ids,
    )
    if narrative is None:
        title = title_text(graph.nodes[group].title if group in graph.nodes else group.replace("-", " ").capitalize())
        narrative = "\n".join(["---", f"kind: article\ntitle: {yaml_string(title)}", "---", "", f"# {title}"])

    # A narrative shown as typed has no slots or sections render can see, so
    # the summary and every statement follow it.
    lead, slots = (
        (len(narrative.splitlines()), {})
        if typed
        else _chapter_layout(
            group, node_ids, narrative, graph=graph, blueprint=blueprint, node_sources=node_sources, targets=targets
        )
    )
    chapter = _place_environments(
        narrative, lead, slots, summary=chapter_summary, environments=environments, protect=not typed,
    )
    for heading, section in _fallback_sections(node_ids, slots, graph):
        if section:
            chapter = chapter.rstrip() + f"\n\n## {heading}\n\n" + "\n\n".join(environments[n] for n in section)
    return chapter.rstrip() + "\n", linked, unresolved


def _fallback_sections(
    node_ids: Iterable[str], slots: Mapping[int, str], graph: Graph
) -> tuple[tuple[str, list[str]], ...]:
    """The sections after the narrative for cards no authored slot holds, in page order."""
    placed = set(slots.values())
    remaining = [node_id for node_id in node_ids if node_id not in placed]
    # A catalog inventories Lean that already exists, so it is no formalization target.
    return tuple(
        (heading, [node_id for node_id in remaining if graph.nodes[node_id].catalog == catalog])
        for heading, catalog in (("Other statements", None), ("Module inventories", "module"))
    )


def _card_positions(
    groups: Mapping[str, list[str]],
    group_pages: Mapping[str, Path],
    *,
    graph: Graph,
    blueprint: Path,
    node_sources: dict[Path, str],
    targets: dict[str, tuple[Path, str]],
    typed: Collection[Path],
) -> dict[str, int]:
    """Each card's place on its chapter page, as :func:`_render_chapter` lays
    it out: authored slots first, then the fallback sections."""
    positions: dict[str, int] = {}
    for group, node_ids in groups.items():
        page = group_pages[group]
        slots = (
            _chapter_layout(
                group, node_ids, page.read_text(encoding="utf-8"),
                graph=graph, blueprint=blueprint, node_sources=node_sources, targets=targets,
            )[1]
            if page.is_file() and page not in typed
            else {}
        )
        cards = [slots[index] for index in sorted(slots)]
        cards += (node_id for _, section in _fallback_sections(node_ids, slots, graph) for node_id in section)
        positions.update((node_id, position) for position, node_id in enumerate(cards))
    return positions


def _chapter_layout(
    group: str,
    node_ids: Iterable[str],
    narrative: str,
    *,
    graph: Graph,
    blueprint: Path,
    node_sources: dict[Path, str],
    targets: dict[str, tuple[Path, str]],
) -> tuple[int, dict[int, str]]:
    """Where render puts its own markup in the ``narrative`` of ``group``'s
    chapter: the line its progress summary goes before, after the opening
    prose and before the first section (the number of lines when it goes at
    the end), and the slot lines, each a standalone link to one of
    ``node_ids`` that render replaces with the node's statement, by the node.
    Check reads each stretch of the narrative between these lines on its own."""

    lines = narrative.splitlines()
    visible_lines = _content_lines(narrative)
    lead = len(lines)
    seen_h1 = False
    for index, line in enumerate(visible_lines):
        heading = _HEADING.match(line)
        if heading is None:
            continue
        level = len(heading.group(1))
        if level == 1:
            seen_h1 = True
        elif seen_h1 and level == 2:
            lead = index
            break
    source_dir = graph.nodes[group].path.parent if group in graph.nodes else blueprint / "roadmap"
    slotted = set(node_ids)
    anchor_nodes = {targets[node_id][1]: node_id for node_id in slotted if targets[node_id][1]}
    slots: dict[int, str] = {}
    for index, visible in enumerate(visible_lines):
        slot = _ARTICLE_SLOT.match(_link_mask(visible))
        if slot is None:
            continue
        target = visible[slot.start("target") : slot.end("target")]
        path, separator, fragment = target.partition("#")
        try:
            if not path and separator:
                node_id = anchor_nodes.get(fragment)
            elif not path or urlsplit(path).scheme or path.startswith("/"):
                node_id = None
            else:
                node_id = node_sources.get(_resolve(source_dir / unquote(path)))
        except ValueError:  # an unbalanced IPv6 bracket or a decoded NUL names no article
            node_id = None
        if node_id is None or node_id not in slotted or node_id in slots.values():
            continue
        slots[index] = node_id
    return lead, slots


def _shared_references(narrative: str) -> dict[str, tuple[str, str]] | None:
    """The reference definitions each stretch of ``narrative`` is parsed with,
    when a "<" anywhere in it, its title included, makes render parse its
    Markdown before render's HTML sits beside it; None when nothing does."""

    lines = narrative.splitlines()
    # A header ends at its closing "---", as _protect_narrative_markdown reads
    # it; one never closed is no header.
    start = 0
    if lines and lines[0].strip() == "---":
        start = next((index + 1 for index in range(1, len(lines)) if lines[index].strip() == "---"), 0)
    body = "\n".join(lines[start:])
    return _convert_body(body)[1] if "<" in _as_fenced(body) else None


def _place_environments(
    narrative: str, lead: int, slots: dict[int, str], *, summary: str, environments: dict[str, str],
    protect: bool = False,
) -> str:
    """The chapter page: ``narrative`` with the progress ``summary`` before
    line ``lead`` and each slot line replaced by its statement's environment.
    Each environment is one HTML block, which ends any paragraph, list, or
    span it meets, so each stretch of the narrative reads as check read it."""

    lines = narrative.splitlines()
    output: list[str] = []
    pending: list[str] = []
    references = _shared_references(narrative) if protect else None
    affected = references is not None

    def flush() -> None:
        if pending:
            chunk = "\n".join(pending)
            output.append(
                _protect_narrative_markdown(chunk, references=references, force=affected, page_header=not output)
                if protect else chunk
            )
            pending.clear()

    for index, line in enumerate(lines):
        if index == lead:
            flush()
            output.extend(["", summary.rstrip(), ""])
        if index in slots:
            flush()
            output.append(environments[slots[index]])
        else:
            pending.append(line)
    flush()
    chapter = "\n".join(output)
    if lead == len(lines):
        chapter = chapter.rstrip() + "\n\n" + summary.rstrip()
    return chapter


def _chapter_stretches(narrative: str, lead: int, slots: dict[int, str]) -> list[str]:
    """Each stretch of ``narrative`` between the lines where render puts its
    own markup, the slot lines left out, with the lines before it blank so
    it keeps its line numbers."""

    lines = narrative.splitlines()
    breaks = sorted({0, lead, *slots, *(index + 1 for index in slots), len(lines)})
    return [
        "\n" * start + "".join(f"{line}\n" for line in lines[start:end])
        for start, end in zip(breaks, breaks[1:])
        if start not in slots and any(line.strip() for line in lines[start:end])
    ]
