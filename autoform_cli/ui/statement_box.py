"""A statement's box on its chapter page: heading, status mark, statement and notes, metadata rows, and
the disclosure of what it uses and what uses it."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

from .. import status
from .._fs import relative as _relative
from .._fs import resolve as _resolve
from ..graph import Graph, Node
from ..lean import SourceLinker, declaration_names
from ..markdown import HEADING as _HEADING
from ..markdown import NOTES_BOX, STATEMENT_BOX, boxed, converted_alone
from ..markdown import outside_fences as _outside_fences
from ..status import is_definition
from . import graph_views
from .lean_blocks import _lean_blocks, _lean_presentation, _search_names
from .markdown_html import _convert_body, _protect_markdown_in_html
from .site_index import _declaration_label
from .titles import _display_title, _plain_title, _title_html


def _render_environment(
    node: Node,
    *,
    anchor: str,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    used_by: dict[str, list[str]],
    formalizing: Mapping[str, str],
    linker: SourceLinker,
    links: Mapping[str, str],
    repo_root: Path,
    statements: dict[str, tuple[str, str]],
    reviews: dict[str, str],
) -> tuple[str, int, list[str]]:
    node_status = statuses[node.id]
    caption = _declaration_label(node)
    # The statement as checked: the text the graph parsed, with its links
    # resolved from the chapter page it is published on.
    statement, remainder = statements[node.id]

    # References in either chunk may be used by the other. If one chunk is
    # preconverted, render both with the shared article context: otherwise its
    # consumed definitions would leave the remaining Markdown links unresolved.
    if converted_alone(statement, remainder):
        _, references, _ = _convert_body("\n\n".join((statement, remainder)))
        statement, remainder = (
            _protect_markdown_in_html(part, references=references, force=True) for part in (statement, remainder)
        )
        # Only once converted: inside a container, an author's "<" could close it.
        remainder = _isolate_converted_headings(remainder)
    else:
        remainder = _isolate_notes_headings(remainder)

    code_links, implementation_rows, linked, unresolved = _lean_presentation(node, linker)
    source_link = _vault_source_link(node, repo_root=repo_root, linker=linker)
    meta_rows = implementation_rows
    if mathlib_names := declaration_names(node.mathlib_declaration or ""):
        # As text; check warns about a name no pinned name list holds.
        meta_rows.append(("Mathlib", " · ".join(f"<code>{html.escape(name)}</code>" for name in mathlib_names)))
    if node_status.key == "conditional":
        # A conditional proof must never read as finished, so the open
        # statements it rests on are named on the statement itself.
        assumed = _node_references(node_status.assumes, graph=graph, statuses=statuses, links=links)
        meta_rows.append(("Assumes", f"{assumed} (open statements without a recorded Lean proof)"))
    if node.discussion:
        meta_rows.append(("Discussion", _discussion_link(node.discussion, linker)))
    meta = _render_rows(meta_rows, css_class="bp-meta")
    dependencies = _dependency_disclosure(
        node,
        graph=graph,
        statuses=statuses,
        used_by=used_by,
        links=links,
    )

    # amsthm distinguishes the two: a proposition is set in italics, a
    # definition upright. leanblueprint keeps that distinction on the web.
    display_key = graph_views.display_status_key(node, node_status)
    display_label = graph_views.display_state(node, node_status).label
    style = (
        "theorem-style-definition"
        if node.catalog == "module" or is_definition(node)
        else "theorem-style-plain"
    )
    # A checked inventory is fully proved, so it keeps the tick. A project that
    # tracks no Lean marks no status, and its box keeps the neutral rule.
    mark = "✓" if node_status.key in {"fully_proved", "mathlib"} else "●"
    tracked = graph.tracks_lean
    status_class = f" bp-{display_key}" if tracked else ""
    status_mark = (
        f'<span class="bp-mark" title="{html.escape(display_label, quote=True)}" data-search-exclude>'
        f'{mark}<span class="bp-mark-label">{html.escape(display_label)}</span></span>'
        if tracked
        else ""
    )

    # The statement is headed by its name, with its kind above it as on the map's
    # reader. A title that is only the kind word, or empty, leaves the kind as the name.
    shown = _display_title(node.title, caption)
    kind_line = f'<span class="bp-thmcaption" data-search-exclude>{html.escape(caption)}</span>' if shown else ""
    # The "#" link's only text is "#", so its name says which statement it links to: by its kind when the title reads
    # as nothing but spaces, as formulas such as `$\,$ $\,$` do.
    permalink_label = f"Link to {_plain_title(shown).strip() or caption}"

    lines = [
        f'<div class="bp-thmwrapper {style}{status_class}" '
        f'data-autoform-node-id="{html.escape(node.id, quote=True)}" markdown="1">',
        f'<h4 class="bp-thmheading" id="{html.escape(anchor, quote=True)}">',
        f"{kind_line}"
        f'<span class="bp-thmtitle">{_title_html(shown or caption)}</span>'
        f"{code_links}"
        f"{source_link}"
        f'<a class="bp-permalink" href="#{html.escape(anchor, quote=True)}" '
        f'aria-label="{html.escape(permalink_label, quote=True)}" data-search-exclude>#</a>'
        f"{status_mark}",
        "</h4>",
        *_search_names(node),
        *boxed(statement, STATEMENT_BOX),
        # A review shows a card's Lean in its packet, after the read-back, so the Lean does not frame the testimony.
        *(_lean_blocks(node, linker, links=links, formalizing=formalizing) if node.id not in reviews else []),
    ]
    if remainder:
        lines.extend(boxed(remainder, NOTES_BOX))
    if meta:
        lines.append(meta)
    if dependencies:
        lines.append(dependencies)
    if node.id in reviews:
        lines.extend(["", reviews[node.id], ""])
    lines.append("</div>")
    return "\n".join(lines), linked, unresolved


#: A heading of a card's notes as the body converter writes it, on a line of its own.
_CONVERTED_HEADING = re.compile(r"<h([1-6])>(.*)</h\1>")


def _isolated_heading(level: int, markdown: str, title: str, words: str) -> str:
    """A notes heading that starts no search record but keeps its words in its card's.

    MkDocs Material's search starts a record at every heading with an id and
    ends it with the heading's parent. So the heading gets a parent of its own
    and is kept out of search, and a hidden copy of its words follows that
    parent, in the card's record. Material removes the attribute's value from
    the page; without one, a link check would read it as a data: URL.
    """
    return (
        f'\n\n<div class="bp-notes-heading"{markdown}>\n'
        f'<h{level} data-search-exclude="1"{markdown}>{title}</h{level}>\n</div>\n\n'
        f'<div class="bp-search-text" hidden>{words}</div>\n\n'
    )


def _isolate_notes_headings(text: str) -> str:
    """Isolate each heading of notes the site converts itself, which hold no "<".

    The heading stays Markdown that md_in_html reads in its own element, so it
    looks, links and takes its id as before. Without a "<", none of the
    author's text can close a container.
    """

    def isolate(line: str) -> str:
        match = _HEADING.match(line)
        if match is None:
            return line
        hashes, title = match.groups()
        return _isolated_heading(len(hashes), ' markdown="1"', title, html.escape(title, quote=False))

    return _outside_fences(text, isolate) if text else text


def _isolate_converted_headings(rendered: str) -> str:
    """Isolate each heading of notes render converted first, as their HTML."""

    def isolate(line: str) -> str:
        match = _CONVERTED_HEADING.fullmatch(line)
        if match is None:
            return line
        level, title = match.groups()
        words = html.escape(html.unescape(re.sub(r"<[^>]*>", "", title)), quote=False)
        return _isolated_heading(int(level), "", title, words).strip("\n")

    return "\n".join(isolate(line) for line in rendered.split("\n"))


def _vault_source_link(node: Node, *, repo_root: Path, linker) -> str:
    """Link a statement to the Markdown article it was authored in.

    The graph view and the published statement are both derived. This is the
    file a reader edits, so it is worth one click from the statement itself.
    """
    if not linker.repository_url or not linker.ref or not linker.links_blueprint:
        return ""
    try:
        relative = _relative(_resolve(node.path), repo_root).as_posix()
    except ValueError:
        return ""
    relative = linker.spellings.get(relative, relative)
    href = (
        f"{linker.repository_url}/blob/{quote(linker.ref, safe='')}/"
        f"{quote(relative, safe='/')}"
    )
    label = html.escape(f"Edit the Markdown source for {node.title}", quote=True)
    icon = (
        '<svg class="bp-source-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        '<path d="M4 4h9l7 7v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"/>'
        '<path d="M13 4v7h7"/></svg>'
    )
    return (
        f'<a class="bp-source-link" href="{html.escape(href, quote=True)}" '
        f'aria-label="{label}" title="{label}">{icon}</a>'
    )


def _dependency_disclosure(
    node: Node,
    *,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    used_by: dict[str, list[str]],
    links: Mapping[str, str],
) -> str:
    """Hide DAG relations behind a native, keyboard-accessible disclosure."""
    rows: list[tuple[str, str]] = []

    def references(node_ids: list[str] | tuple[str, ...], *, used_by_row: bool = False) -> str:
        return _node_references(
            node_ids, graph=graph, statuses=statuses, links=links, relation_node=node, used_by_row=used_by_row,
        )

    if node.statement_dependencies:
        rows.append(("Statement uses", references(node.statement_dependencies)))
    proof_only = [other for other in node.proof_dependencies if other not in node.statement_dependencies]
    if proof_only:
        rows.append(("Proof uses", references(proof_only)))
    if used_by[node.id]:
        rows.append(("Used by", references(used_by[node.id], used_by_row=True)))
    if not rows:
        return ""

    # The summary counts each direction that has any, so a reader can tell
    # from the closed line whether opening it is worth it.
    uses = len(dict.fromkeys((*node.statement_dependencies, *node.proof_dependencies)))
    sides = (f"Uses {uses:,}" if uses else "", f"Used by {len(used_by[node.id]):,}" if used_by[node.id] else "")
    label = " · ".join(side for side in sides if side)
    body = _render_rows(rows, css_class="bp-dependency-body")
    return f'<details class="bp-dependencies"><summary>{label}</summary>{body}</details>'


def _node_references(
    node_ids: list[str] | tuple[str, ...],
    *,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    links: Mapping[str, str],
    relation_node: Node | None = None,
    used_by_row: bool = False,
) -> str:
    """Link each node by the name its statement shows, coloured by its derived state when the project tracks Lean.

    A catalog is coloured as neutrally as its own environment, checked or not.
    Dependency rows additionally describe the original pair's typed counts;
    other callers, including assumed statements, retain their original labels.
    """
    rendered = []
    for other_id in node_ids:
        other = graph.nodes[other_id]
        caption = _declaration_label(other)
        label = _title_html(_display_title(other.title, caption) or caption)
        # The dot before a reference shows its status, which a project that tracks no Lean has none of.
        key = graph_views.display_status_key(other, statuses[other_id])
        status_class = f" bp-ref-{key}" if graph.tracks_lean else ""
        relation_attributes = ""
        if relation_node is not None:
            owner, endpoint = (other, relation_node.id) if used_by_row else (relation_node, other_id)
            counts = (
                (owner.statement_dependencies.count(endpoint), "statement"),
                (owner.proof_dependencies.count(endpoint), "proof"),
            )
            weights = ", ".join(
                f"{count} {kind} {'dependency' if count == 1 else 'dependencies'}"
                for count, kind in counts if count
            )
            if weights:
                relation_attributes = f'title="{html.escape(weights, quote=True)}" '
        rendered.append(
            f'<a class="bp-ref{status_class}" '
            f'{relation_attributes}href="{html.escape(links[other_id], quote=True)}">{label}</a>'
        )
    return ' <span class="bp-ref-sep">·</span> '.join(rendered)


def _render_rows(rows: list[tuple[str, str]], *, css_class: str) -> str:
    if not rows:
        return ""
    body = "".join(
        f'<div class="bp-row"><span class="bp-key">{key}</span><span class="bp-value">{value}</span></div>'
        for key, value in rows
    )
    return f'<div class="{css_class}">{body}</div>'


def _discussion_link(discussion: str, linker: SourceLinker) -> str:
    if discussion.startswith(("http://", "https://")):
        url = discussion
        label = discussion
    elif linker.repository_url and discussion.lstrip("#").isdigit():
        number = discussion.lstrip("#")
        url = f"{linker.repository_url}/issues/{number}"
        label = f"#{number}"
    else:
        return html.escape(discussion)
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
