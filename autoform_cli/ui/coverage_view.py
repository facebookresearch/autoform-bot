"""The coverage page, and the coverage line in the landing page's hero."""

from __future__ import annotations

import html
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from .. import status
from ..coverage import COVERAGE_DISPOSITIONS, CoverageSummary, contract_table, coverage_target_nodes
from ..graph import Graph, _is_countable
from ..markdown import frontmatter_end, link_targets, only_links, render_tree
from .chapters import _place_environments, _shared_references
from .graph_views import _containment_children, _leaf_descendants
from .markdown_html import _protect_markdown_in_html
from .statement_box import _node_references
from .titles import _HEADING_TAG


# Presentation starts with work expanded into the roadmap, then follows the
# parser's canonical order. Deriving this tuple keeps newly added dispositions
# visible instead of silently dropping them from the landing page.
_COVERAGE_SUMMARY_ORDER = tuple(
    sorted(COVERAGE_DISPOSITIONS, key=lambda disposition: disposition != "DECOMPOSED")
)


# What each disposition means for a source area, in the reader's words.
_COVERAGE_WORDS = {
    "DECOMPOSED": "split into statements",
    "MAPPED": "not yet settled",
    "INVENTORIED": "inventoried as Lean modules",
    "DEFERRED": "left for later",
    "OUT": "out of scope",
}


#: The coverage view's heading for each disposition.
_COVERAGE_HEADINGS = {
    "DECOMPOSED": "Decomposed",
    "MAPPED": "Mapped",
    "INVENTORIED": "Inventoried",
    "DEFERRED": "Deferred",
    "OUT": "Out of scope",
}


def _render_coverage_page(
    page: str,
    *,
    coverage: CoverageSummary,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    containers: frozenset[str],
    links: Mapping[str, str],
    node_sources: dict[Path, str],
    rank: Mapping[str, int],
) -> str | None:
    """The coverage ``page`` with its contract table shown as the coverage
    view, or None when the page holds no table with the contract's rows.

    Each disposition gets a heading with its count, and its rows follow in the
    author's order, each led by its area and its evidence as written. A
    DECOMPOSED row also lists the declarations its links reach, as the role
    checks read them, in book order and with their status. The list replaces
    evidence that is only links, each straight to one of those declarations;
    beside evidence it keeps, which links to where the rest are, it shows the
    project's :attr:`Graph.coverage_list_limit` and counts the rest.
    """

    found = contract_table(page, coverage.entries)
    if found is None:
        return None
    span, rows = found
    lines = page.splitlines()
    # One level below the heading the site shows above the table, whatever its spelling.
    above = render_tree("\n".join(lines[frontmatter_end(lines) : span.start]))
    tags = [str(element.tag) for element in above.iter()] if above is not None else []
    levels = [int(tag[1]) for tag in tags if _HEADING_TAG.fullmatch(tag)]
    level = min(levels[-1] + 1, 6) if levels else 2
    shared = _shared_references(page)

    def authored(text: str) -> str:
        # As on a chapter page: once a "<" makes render parse the page's own
        # Markdown, the rows written from its cells are parsed with it.
        return text if shared is None else _protect_markdown_in_html(text, references=shared, force=True)

    children = _containment_children(graph)
    shown: dict[str, list[str]] = {}
    for entry, (area, _, evidence) in zip(coverage.entries, rows):
        statements: list[str] = []
        listed_only = False
        if entry.disposition == "DECOMPOSED":
            targets = coverage_target_nodes(graph, coverage, entry.evidence, by_path=node_sources)
            reached = dict.fromkeys(
                leaf
                for target in targets
                for leaf in _leaf_descendants(children, target)
                if _is_countable(graph, leaf, containers)
            )
            statements = sorted(reached, key=lambda node_id: (rank.get(node_id, len(rank)), node_id))
            # Each link names one listed statement, no part of a page, and
            # nothing else is written: no image, no other link, no other words.
            written = link_targets(entry.evidence)
            listed_only = (
                bool(statements)
                and len(set(written)) == len(targets)
                and all(_is_countable(graph, node_id, containers) for node_id in targets)
                and not any(urlsplit(target).fragment for target in written)
                and only_links(evidence, len(written))
            )
        # Each cell in a block of its own, read as inline Markdown, as the
        # table read it: a "$" or "*" in one cannot pair with one in the
        # other. As HTML, so an area the author wrote in bold stays bold.
        block = authored(
            f'<p markdown="span"><strong>{area}</strong></p>'
            + ("" if listed_only else f'\n\n<p markdown="span">{evidence}</p>')
        )
        if statements:
            # Evidence the list replaces was the author's own links, so it is
            # listed in full; beside evidence that stays, a long list is cut.
            limit = len(statements) if listed_only else graph.coverage_list_limit
            listed = _node_references(statements[:limit], graph=graph, statuses=statuses, links=links)
            if len(statements) > limit:
                listed += f' <span class="bp-ref-sep">·</span> and {len(statements) - limit:,} more'
            counted = f"{len(statements):,} statement{'' if len(statements) == 1 else 's'}"
            if graph.tracks_lean:
                selected = {node_id: statuses[node_id] for node_id in statements}
                counted += "".join(f" · {count:,} {state.label}" for state, count in status.summarize(selected))
            # One statement is shown open, as the link it replaces was.
            block += (
                f'\n\n<details class="bp-dependencies" data-search-exclude{" open" if len(statements) == 1 else ""}>'
                f'<summary>{html.escape(counted)}</summary><div class="bp-dependency-body">{listed}</div></details>'
            )
        shown.setdefault(entry.disposition, []).append(block)
    view = "\n\n".join(
        "\n\n".join(
            [f"{'#' * level} {_COVERAGE_HEADINGS[disposition]} ({len(blocks):,}) {{#coverage-{disposition.lower()}}}", *blocks]
        )
        for disposition in _COVERAGE_SUMMARY_ORDER
        if (blocks := shown.get(disposition))
    )
    # Every line of the table is a slot, so the page is protected as a whole
    # page with the table in it would be.
    return _place_environments(
        page, -1, {index: "view" if index == span.start else "" for index in span}, summary="",
        environments={"view": view, "": ""}, protect=True,
    )


def _coverage_summary(coverage: CoverageSummary) -> str:
    """Count the declared source areas by disposition, without a percentage.

    Split areas lead, as "all" or "N of M"; the other dispositions follow.
    """

    counts = coverage.counts
    total = len(coverage.entries)
    if not total:
        return ""
    areas = f"{total:,} source {'area' if total == 1 else 'areas'}"
    split = counts["DECOMPOSED"]
    if split == total:
        return f"{areas}, {'all ' if total > 1 else ''}{_COVERAGE_WORDS['DECOMPOSED']}"
    rest = " · ".join(
        f"{counts[disposition]:,} {_COVERAGE_WORDS[disposition]}"
        for disposition in _COVERAGE_SUMMARY_ORDER
        if counts[disposition] and disposition != "DECOMPOSED"
    )
    if not split:
        return f"{areas}: {rest}"
    return f"{split:,} of {areas} {_COVERAGE_WORDS['DECOMPOSED']} · {rest}"
