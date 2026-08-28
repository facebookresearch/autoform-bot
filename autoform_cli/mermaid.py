"""Render a blueprint DAG as Mermaid inside a Markdown page.

Mermaid was chosen over a bespoke SVG because the same fenced block renders in
Obsidian, on GitHub, and on the published site. Colours follow
:mod:`autoform_cli.status`: fill tracks proof progress, stroke tracks statement
progress, exactly as ``leanblueprint`` draws its dependency graph.
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .status import STATES, is_definition

if TYPE_CHECKING:
    from .graph import Graph, Node
    from .graph_views import GraphView, ViewEdge, ViewNode
    from .status import NodeStatus


def node_link(node: Node, output: Path, link_extension: str) -> str:
    """Relative link from the page at *output* to *node*'s source file."""
    return relative_link(node.path, output, link_extension)


def relative_link(target: Path, output: Path, link_extension: str) -> str:
    """Relative link from the page at *output* to *target*, with its suffix swapped."""
    relative = os.path.relpath(target.resolve(), output.resolve().parent)
    return Path(relative).with_suffix(link_extension).as_posix()


def source_links(graph: Graph, output: Path, link_extension: str) -> dict[str, str]:
    """Link every node to its own Markdown file, as the vault sees it."""
    return {
        node_id: node_link(node, output, link_extension)
        for node_id, node in graph.nodes.items()
    }


def _escape(text: str) -> str:
    """Make *text* safe inside a quoted Mermaid label."""
    return text.replace('"', "#quot;").replace("`", "#96;")


def render_diagram(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    output: Path,
    *,
    link_extension: str = ".md",
    links: dict[str, str] | None = None,
    include_classdefs: bool = True,
) -> str:
    """Return the ```mermaid fenced block for *graph*.

    *links* maps node ids to hrefs; without it, nodes link to their own
    Markdown files relative to *output*.

    Set *include_classdefs* to ``False`` for the published site, where the
    init script supplies the palette so it can be swapped for dark mode.
    Mermaid scopes its own styles to the SVG id with ``!important``, so a
    stylesheet cannot recolour a rendered diagram; only re-rendering works.
    Obsidian has no such script, so the vault copy keeps its colours inline.
    """
    ordered = sorted(graph.nodes.values(), key=lambda node: node.id)
    if not ordered:
        return '```mermaid\ngraph LR\n  empty["No nodes yet"]\n```'
    if links is None:
        links = source_links(graph, output, link_extension)

    handles = {node.id: f"n{index}" for index, node in enumerate(ordered)}
    lines = ["```mermaid", "graph LR"]

    for node in ordered:
        handle = handles[node.id]
        label = _escape(node.title)
        # Rectangles introduce data, rounded boxes assert something.
        shape = f'["{label}"]' if is_definition(node) else f'("{label}")'
        lines.append(f"  {handle}{shape}:::{statuses[node.id].key}")

    for node in ordered:
        for dependency in node.statement_dependencies:
            lines.append(f"  {handles[dependency]} --> {handles[node.id]}")
        for dependency in node.proof_dependencies:
            if dependency not in node.statement_dependencies:
                # Dashed: needed only to prove the node, not to state it.
                lines.append(f"  {handles[dependency]} -.-> {handles[node.id]}")

    for node in ordered:
        tooltip = _escape(f"{node.title} — {statuses[node.id].label}")
        lines.append(f'  click {handles[node.id]} "{links[node.id]}" "{tooltip}"')

    if include_classdefs:
        lines.extend(f"  {line}" for line in classdef_lines())
    lines.append("```")
    return "\n".join(lines)


def render_view_diagram(
    view: GraphView,
    *,
    links: dict[str, str] | None = None,
    include_classdefs: bool = True,
) -> str:
    """Return a Mermaid diagram for a project, chapter, focus, or full view."""
    if not view.nodes:
        return '```mermaid\ngraph LR\n  empty["No nodes in this view"]\n```'
    links = links or {}
    handles = {node.id: f"n{index}" for index, node in enumerate(view.nodes)}
    lines = ["```mermaid", "graph LR"]

    for node in view.nodes:
        handle = handles[node.id]
        label = _view_label(node)
        if node.kind == "node":
            shape = f'["{label}"]' if is_definition(node) else f'("{label}")'
            class_name = node.status_key or "planned"
        else:
            shape = f'["{label}"]'
            class_name = "scope" if node.kind == "scope" else "boundary"
        lines.append(f"  {handle}{shape}:::{class_name}")

    for edge in view.edges:
        lines.extend(_view_edge_lines(edge, handles))

    for node in view.nodes:
        handle = handles[node.id]
        if node.focus:
            lines.append(f"  class {handle} focus")
        href = links.get(node.id)
        if href is not None:
            tooltip = _escape(f"{node.title} — {_view_summary(node)}")
            lines.append(f'  click {handle} "{href}" "{tooltip}"')

    if include_classdefs:
        lines.extend(f"  {line}" for line in classdef_lines())
    lines.append("```")
    return "\n".join(lines)


def _view_label(node: ViewNode) -> str:
    title = _escape(node.title)
    if node.kind == "node":
        return title
    prefix = "External chapter: " if node.kind == "boundary" else ""
    return f"{prefix}{title}<br/><small>{_escape(_view_summary(node))}</small>"


def _view_summary(node: ViewNode) -> str:
    item = "item" if node.item_count == 1 else "items"
    counts = " · ".join(
        f"{count} {_STATE_LABELS.get(key, key.replace('_', ' '))}"
        for key, count in node.status_counts
    )
    return f"{node.item_count} {item}" + (f" · {counts}" if counts else "")


def _view_edge_lines(edge: ViewEdge, handles: dict[str, str]) -> list[str]:
    source = handles[edge.source]
    target = handles[edge.target]
    lines = []
    if edge.statement_count:
        label = f"|{edge.statement_count}|" if edge.statement_count > 1 else ""
        lines.append(f"  {source} -->{label} {target}")
    if edge.proof_count:
        if edge.proof_count > 1:
            lines.append(f"  {source} -. {edge.proof_count} .-> {target}")
        else:
            lines.append(f"  {source} -.-> {target}")
    return lines


def classdef_lines(*, dark: bool = False) -> list[str]:
    """Mermaid ``classDef`` declarations for every state, in one palette."""
    states = [
        f"classDef {state.key} "
        f"fill:{state.dark_fill if dark else state.fill},"
        f"stroke:{state.dark_stroke if dark else state.stroke},"
        f"color:{state.dark_text if dark else state.text},stroke-width:2px"
        for state in STATES
    ]
    # Chapter and boundary boxes are the project map, which is the first thing
    # on the landing page, so they take the same greys and blues as the rest of
    # the site rather than the GitHub palette the states used to sit in.
    if dark:
        views = [
            "classDef scope fill:#1C1D1F,stroke:#2D88FF,color:#E4E6EB,stroke-width:2px",
            "classDef boundary fill:#18191A,stroke:#8A8D91,color:#B0B3B8,stroke-width:2px,stroke-dasharray:5 3",
            "classDef focus stroke:#F7B928,stroke-width:4px",
        ]
    else:
        views = [
            "classDef scope fill:#EBF2FE,stroke:#0064E0,color:#050505,stroke-width:2px",
            "classDef boundary fill:#FFFFFF,stroke:#8A8D91,color:#65676B,stroke-width:2px,stroke-dasharray:5 3",
            "classDef focus stroke:#F7B928,stroke-width:4px",
        ]
    return [*states, *views]


def render_legend(statuses: dict[str, NodeStatus]) -> str:
    """Return a legend covering the states actually in use.

    A Markdown table put this in a bordered box with a blank first heading over
    the swatches, and left each column to size itself against its own longest
    cell, so the swatch, the label and the count landed in a different place in
    every row. This is one grid instead: the cells are emitted in reading order
    and the columns line up because the grid, not the content, sets them.
    """
    used = {status.key for status in statuses.values()}
    rows = [state for state in STATES if state.key in used]
    if not rows:
        return ""
    counts = {state.key: 0 for state in STATES}
    for status in statuses.values():
        counts[status.key] += 1
    cells = []
    for state in rows:
        # Colour comes from the stylesheet, so the legend follows the theme.
        cells.append(
            f'<span class="bp-swatch bp-swatch-{state.key}"></span>'
            f'<span class="bp-legend-label">{html.escape(state.label)}</span>'
            f'<span class="bp-legend-count">{counts[state.key]}</span>'
            f'<span class="bp-legend-meaning">{html.escape(_MEANINGS[state.key])}</span>'
        )
    return f'<div class="bp-legend-grid">{"".join(cells)}</div>'


def render_legend_tip(statuses: dict[str, NodeStatus]) -> str:
    """The legend behind a hover icon, for pages that only occasionally need it.

    A graph page carried the legend in a disclosure captioned "What the colours
    mean", which spent a headline-sized row on a question most readers never
    ask, on every page, under every diagram. The same content hangs off an
    icon instead.

    Pure CSS: the icon is a real button, and the note shows on ``:hover`` and
    on ``:focus-within``, so it opens by keyboard as well as by pointer. Not
    used in the vault copy, which is read in Obsidian where this stylesheet
    does not exist and the note would simply never be reachable.
    """
    legend = render_legend(statuses)
    if not legend:
        return ""
    icon = (
        '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
        '<circle cx="8" cy="8" r="7"/>'
        '<path d="M8 7v4.5" /><circle cx="8" cy="4.6" r="0.9" class="bp-legend-dot"/>'
        "</svg>"
    )
    return (
        '<span class="bp-legend-tip">'
        '<button type="button" class="bp-legend-icon" aria-describedby="bp-legend-note">'
        f'{icon}<span class="bp-visually-hidden">What the colours mean</span>'
        "</button>"
        f'<span class="bp-legend-note" id="bp-legend-note" role="tooltip">{legend}</span>'
        "</span>"
    )


_MEANINGS = {
    "mathlib": "Upstreamed into Mathlib.",
    "fully_proved": "Proved, and every prerequisite is fully proved too.",
    "proved": "Proof compiles, but something it rests on is not finished.",
    "defined": "Definition is written in Lean.",
    "can_prove": "Statement is in Lean and every prerequisite is proved — ready to work.",
    "stated": "Statement is in Lean; the proof is not.",
    "can_state": "Prerequisites are stated, so this can be written down.",
    "not_ready": "Needs more blueprint work before it can be attempted.",
    "planned": "Described in the blueprint only.",
}

_STATE_LABELS = {state.key: state.label for state in STATES}


def render_page(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    output: Path,
    *,
    link_extension: str = ".md",
    title: str = "Dependency graph",
    links: dict[str, str] | None = None,
    include_classdefs: bool = True,
) -> str:
    """Return a complete Markdown page holding the diagram and its legend."""
    counts = f"{len(graph.nodes)} nodes · {graph.edge_count} dependencies"
    diagram = render_diagram(
        graph,
        statuses,
        output,
        link_extension=link_extension,
        links=links,
        include_classdefs=include_classdefs,
    )
    legend = render_legend(statuses)
    sections = [
        "---",
        "kind: graph",
        "---",
        "",
        f"# {title}",
        "",
        f"{counts}. Arrows point from a prerequisite to what depends on it; a dashed",
        "arrow marks a prerequisite that only the proof needs. Select a node to open it.",
        "",
        diagram,
        "",
    ]
    if legend:
        sections.extend(["## Legend", "", legend, ""])
    return "\n".join(sections)


__all__ = [
    "node_link",
    "relative_link",
    "render_diagram",
    "render_legend",
    "render_legend_tip",
    "render_page",
    "render_view_diagram",
    "source_links",
]
