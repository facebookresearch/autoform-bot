"""The landing page: its hero, lead sentence and next-up card."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from .. import status
from ..coverage import CoverageSummary
from ..graph import Graph, _containers, _countable, _module_inventories
from ..markdown import _LINK_DEFINITION as _SITE_LINK_DEFINITION
from ..markdown import (
    FORMULA_CLASS,
    mask_fences_and_comments,
    name_text,
    relative_link,
    render_tree,
    rendered_visible_text,
    yaml_string,
)
from ..markdown import HEADING as _HEADING
from . import graph_pages, graph_views, tex_plain
from .coverage_view import _coverage_summary
from .markdown_html import _as_published
from .page_links import _anchored_links
from .titles import _HEADING_TAG, _first_h1, _page_heading, _title_html
from .vault_pages import _read_page


def _next_target(
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    *,
    page: Path,
    group_pages: dict[str, Path] | None = None,
    targets: dict[str, tuple[Path, str]] | None = None,
    order: Sequence[str] = (),
) -> str:
    """Name the result a contributor could pick up right now, with the way in.

    A dashboard that only counts is a scoreboard. The useful question on
    arriving is "what is unblocked, and where do I start reading?", which the
    DAG already answers: the first node in reading ``order`` (topological
    without one) whose prerequisites are met and whose proof is still open.
    Naming it without linking to it makes the reader hunt, so the card
    carries the statement, its chapter, and its dependency view. A project
    that tracks no Lean has nothing to pick up in Lean, so it has no card.
    """
    if not graph.tracks_lean:
        return ""
    containers = _containers(graph)
    links = _anchored_links(targets or {}, page)
    for node_id in order or status.topological_order(graph):
        node_status = statuses.get(node_id)
        node = graph.nodes[node_id]
        if node_status is None or node_id in containers or not node.formalizable:
            continue
        if node_status.key not in {"can_prove", "can_state"}:
            continue

        chapter_id = node.id.split("/", 1)[0] if "/" in node.id else ""
        chapter_page = (group_pages or {}).get(chapter_id)
        statement = links.get(node_id)

        title = _title_html(node.title)
        heading = (
            f'<a href="{html.escape(statement, quote=True)}">{title}</a>' if statement else title
        )
        why = (
            "Everything it uses is in Lean, so its Lean proof can start."
            if node_status.key == "can_prove"
            else "Everything its statement uses is in Lean, so its Lean statement can be written now."
        )
        actions: list[str] = []
        if statement:
            # Its page with the node selected, where its old dependency page's redirect leads.
            selected = f"{statement.partition('#')[0]}#node={quote(node.id, safe='')}"
            actions.append(f'<a href="{html.escape(selected, quote=True)}">Dependencies</a>')
        if chapter_page is not None:
            # The chapter's title as authored: its page's header holds the visible text.
            chapter = graph.nodes.get(chapter_id)
            shown = _page_heading(chapter_page.read_text(encoding="utf-8")) or "chapter"
            chapter_title = _title_html(chapter.title, fallback=shown) if chapter is not None else html.escape(shown)
            href = _as_published(relative_link(chapter_page, page, ".html"))
            actions.insert(0, f'<a href="{html.escape(href, quote=True)}">{chapter_title}</a>')
        blockers = [
            graph.nodes[dep].title
            for dep in node.dependencies
            if statuses.get(dep) and statuses[dep].key not in {"fully_proved", "mathlib"}
        ]
        rests = (
            f'<div class="bp-next-rests">Rests on {", ".join(_title_html(title) for title in blockers[:3])}</div>'
            if blockers
            else ""
        )
        return (
            f'<div class="bp-next-target" data-autoform-node-id="{html.escape(node.id, quote=True)}">'
            '<div class="bp-next-kicker">Next up</div>'
            f'<div class="bp-next-title">{heading}</div>'
            f'<div class="bp-next-why">{why}</div>'
            f"{rests}"
            f'<div class="bp-next-actions">{" · ".join(actions)}</div>'
            "</div>"
        )
    return ""


def _render_landing_page(
    text: str,
    *,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    coverage: CoverageSummary,
    group_pages: dict[str, Path],
    page: Path,
    destination: Path,
    targets: dict[str, tuple[Path, str]] | None = None,
    order: Sequence[str] = (),
    sources_page: Path | None = None,
) -> str:
    """The landing page: what this is, how far it has got, and what is next.

    Its complete generated hero and next result remain reading content. The
    shared project graph shell is attached only after all pages are final,
    so landing pages and mathematical chapters use the same viewer once.
    """
    body = _document_body(text)
    heading = _first_h1(text) or ""
    # Where the hero cannot typeset the H1, as beside a link, a formula reads as text, as in the tab.
    shown = rendered_visible_text(heading, formula=tex_plain.visible)
    title = shown or "Blueprint"
    scopes = graph_views.group_nodes(graph)
    book = graph_pages.book_contents(graph) if scopes else None
    book_href = _as_published(relative_link(destination / book, page, ".html")) if book else None
    sources_href = _as_published(relative_link(sources_page, page, ".html")) if sources_page else None
    hero = _render_hero(
        title,
        body,
        graph,
        statuses,
        # An H1 that reads as nothing, such as one of thin spaces, leaves the hero named Blueprint, as the tab is.
        heading=heading if shown else "",
        coverage=coverage,
        coverage_href=_as_published(
            relative_link(destination / coverage.source_path, page, ".html")
        ),
        rail=bool(scopes),
        # The explorer's list view, filtered to the states the figure counts.
        ready_href=f"{book_href}#view=list&status=can_state,can_prove" if book_href else None,
        # Without a roadmap contents page the book starts here.
        book_href=None if book == Path("README.md") else book_href,
        sources_href=sources_href,
    )
    next_up = _next_target(
        graph,
        statuses,
        page=page,
        group_pages=group_pages,
        targets=targets,
        order=order,
    )
    front = [
        "---",
        # The hero sets the H1 itself, so the page title has to be declared
        # rather than discovered from the first Markdown heading. The theme
        # prints it as it is, so it is written as text, a formula included.
        f"title: {yaml_string(name_text(shown) or 'Blueprint')}",
        # The sidebar would list this page alone, and the table of contents one
        # or two headings, so both give their width back.
        "hide:",
        "  - navigation",
        "  - toc",
    ]
    if book:
        # The shared shell is attached after all full reading content is final.
        reading = f'<div class="bp-landing-intro">{hero}{next_up}</div>'
        return "\n".join([*front, "---", "", reading]).rstrip() + "\n"
    # A roadmap without articles has nothing to explore, so the hero stands
    # over its status breakdown.
    breakdown = (
        graph_pages.render_legend(
            graph_views.scope_target_statuses(graph, statuses),
            graph_views.inventory_counts(graph, statuses),
        )
        if graph.tracks_lean
        else ""
    )
    parts = [*front, "---", "", '<div class="bp-landing" markdown="1">', "", hero, "", next_up]
    if breakdown:
        parts.extend(["", breakdown])
    parts.append("</div>")
    return "\n".join(parts).rstrip() + "\n"


#: States a contributor could pick up today.
_ACTIONABLE_STATES = frozenset({"can_prove", "can_state"})


def _completion_percentage(done: int, total: int) -> int:
    """Round progress while reserving both endpoints for the exact endpoints."""

    if not total or not done:
        return 0
    if done == total:
        return 100
    return max(1, min(99, round(100 * done / total)))


def _render_hero(
    title: str,
    body: str,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    *,
    coverage: CoverageSummary,
    coverage_href: str,
    rail: bool = False,
    ready_href: str | None = None,
    book_href: str | None = None,
    sources_href: str | None = None,
    heading: str = "",
) -> str:
    """Open with the project's name, its one-line claim, and where it stands.

    The name is *heading*, the README's H1 as written, typeset as a statement's
    title is, so its formulas and code do not show their source; where the
    reading label refuses the H1, as it does one with a link, it is *title*,
    the H1's visible text with each formula read as text.

    The dashboard this replaces was a paragraph of counts in a bordered box.
    The same numbers set as figures answer "how far along is this?" before the
    reader has finished the title, which is the only question the top of a
    blueprint has to answer. The explorer's reading pane (*rail*) gets the
    two progress figures only, and module inventories as one plain line under
    them. With *ready_href* the ready figure links the list of what it counts,
    *book_href* puts the way into the book right under the claim, and
    *sources_href* links the source notes beside the coverage view. A project
    that tracks no Lean has no progress to show, so its hero keeps the title,
    the way in and the coverage line, and its claim is the README's whole opening
    prose rather than its first sentence.
    """
    tracked = graph.tracks_lean
    leaves = _countable(graph)
    selected = {node_id: statuses[node_id] for node_id in leaves}
    # A target is complete only when it and every prerequisite are proved.
    # ``proved`` alone covers its own proof, definition body, or authored
    # Mathlib marker; ``fully_proved`` also closes the dependency chain.
    done = sum(node_status.fully_proved for node_status in selected.values())
    actionable = sum(
        count for state, count in status.summarize(selected) if state.key in _ACTIONABLE_STATES
    )
    total = len(leaves)
    inventory_count = len(_module_inventories(graph))
    share = _completion_percentage(done, total)
    statement_label = "statement" if total == 1 else "statements"

    # The labels reuse the status words, so the share reads as the fully
    # proved status it counts and the second figure as "ready to state or
    # prove"; each note says in plain words what the number counts. With no
    # statements, or all of them fully proved, one sentence says so instead.
    figures: list[tuple[str, str, str, bool]] = []
    if tracked and done < total:
        figures.append(("Fully proved", f"{share}%", f"{done:,} of {total:,} {statement_label}", False))
        figures.append(
            (
                "Ready to work on",
                f"{actionable:,}",
                f"{'statement' if actionable == 1 else 'statements'} you can state or prove now",
                False,
            )
            if actionable
            else ("Nothing is ready right now", "", "", False)
        )
    # The figure counts every catalog, checked or not; a project without
    # catalogs has no inventory to report. The rail counts it in words.
    if inventory_count and not rail:
        figures.append(
            (
                "Module inventories",
                f"{inventory_count:,}",
                "existing Lean modules inventoried",
                True,
            )
        )

    def figure(label: str, value: str, note: str, neutral: bool) -> str:
        # A link is inline, and Markdown's HTML pass breaks blocks inside one
        # apart, so the linked figure is built from spans the stylesheet stacks.
        link = label == "Ready to work on" and ready_href
        tag = "span" if link else "div"
        inner = (
            (f'<{tag} class="bp-figure-value">{value}</{tag}>' if value else "")
            + f'<{tag} class="bp-figure-label">{html.escape(label)}</{tag}>'
            + (f'<{tag} class="bp-figure-note">{html.escape(note)}</{tag}>' if note else "")
        )
        if link:
            return f'<a class="bp-figure bp-figure-link" href="{html.escape(ready_href, quote=True)}">{inner}</a>'
        return f'<div class="bp-figure{" bp-figure-neutral" if neutral else ""}">{inner}</div>'

    ways_in = f'<a class="bp-intro-book" href="{html.escape(book_href, quote=True)}">Start reading</a>' if book_href else ""
    sentence = (
        "No statements yet." if not total else "Every statement is fully proved." if tracked and done == total else ""
    )
    stats = "".join(figure(*entry) for entry in figures)
    inventories = f"{inventory_count:,} module {'inventory' if inventory_count == 1 else 'inventories'}"
    areas = _coverage_summary(coverage)
    coverage_line = (
        '<div class="bp-hero-coverage">'
        + (f'<span class="bp-hero-coverage-counts">{html.escape(areas)}</span> ' if areas else "")
        # One item, so the two links wrap together.
        + '<span class="bp-hero-coverage-links">'
        + f'<a class="bp-hero-coverage-link" href="{html.escape(coverage_href, quote=True)}">'
        "What the sources cover</a>"
        + (f' <a class="bp-hero-sources-link" href="{html.escape(sources_href, quote=True)}">Source notes</a>' if sources_href else "")
        + "</span></div>"
    )
    leads = _lead_paragraphs(body, whole=not tracked)
    return (
        '<div class="bp-hero">'
        + ("" if rail or not tracked else '<div class="bp-hero-kicker">Formalization blueprint</div>')
        + f'<h1 class="bp-hero-title">{_title_html(heading, fallback=title)}</h1>'
        + "".join(f'<p class="bp-hero-lead">{lead}</p>' for lead in leads)
        # In its own block: Markdown breaks an inline link between blocks apart.
        + (f"<div>{ways_in}</div>" if ways_in else "")
        + (f'<p class="bp-hero-status">{sentence}</p>' if sentence else "")
        + (f'<div class="bp-hero-figures">{stats}</div>' if stats else "")
        + (f'<p class="bp-hero-counts">{inventories}</p>' if rail and inventory_count else "")
        # The bar repeats the share for the wide hero, in the fully proved
        # status colour; with no share to show it goes.
        + (
            '<div class="bp-hero-bar" aria-hidden="true">'
            f'<span class="bp-swatch-fully_proved" style="width: {share}%"></span></div>'
            if tracked and not rail and not sentence
            else ""
        )
        + coverage_line
        + "</div>"
    )


def _lead_paragraphs(body: str, *, whole: bool = False) -> list[str]:
    """The blueprint's own opening prose, for the hero: its first sentence, or
    with *whole* every paragraph from the first until a heading or list.

    Written by a human in `blueprint/README.md`, so it says what the project is
    far better than anything derived from the graph could. Prose only: a
    heading or a list is structure rather than a claim. Each paragraph with
    words is read through the site's own converter, so code and formulas keep
    their contents, a link keeps its words without its target, and an image,
    such as a badge, is dropped.
    """
    lines = mask_fences_and_comments(body.splitlines())
    found: list[str] = []
    if _read_page(body)[1]:
        # Check flags this page, and the landing page then shows only its
        # title and lead, as text: the prose blocks, escaped as typed.
        for block in re.split(r"\n[ \t]*\n", "\n".join(lines)):
            typed = " ".join(block.split())
            if found and re.match(r"#|[-*+]\s|\d+[.)]\s", typed):
                break
            if typed and not typed.startswith(("#", "-", "*", "<", "|", ">", "!", "[")):
                found.append(_lead_html([("text", typed)], whole=whole))
                if not whole:
                    break
        return found
    # Each block is read alone, so raw HTML elsewhere does not hide the lead,
    # with the reference definitions its links may name. Only lines the site's
    # converter reads as definitions, so a footnote never becomes a paragraph.
    definitions = "\n".join(line for line in lines if _SITE_LINK_DEFINITION.match(line))
    for block in re.split(r"\n[ \t]*\n", "\n".join(lines)):
        tree = render_tree(f"{block}\n\n{definitions}", allow_html=False) if block.strip() else None
        page = tree.find("body") if tree is not None else None
        for element in page if page is not None else ():
            if found and (element.tag in {"ul", "ol"} or _HEADING_TAG.fullmatch(element.tag)):
                return found
            parts = _lead_parts(element) if element.tag == "p" else []
            if any(kind in {"code", "math"} or (kind == "text" and value.strip()) for kind, value in parts):
                found.append(_lead_html(parts, whole=whole))
                if not whole:
                    return found
    return found


def _lead_parts(element: object) -> list[tuple[str, str]]:
    """A converted paragraph as text, code, math and emphasis parts."""
    parts: list[tuple[str, str]] = []

    def text(value: str | None) -> None:
        if value:
            # An Obsidian link reads as its alias, or else its target.
            value = re.sub(r"\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]", lambda link: link.group(2) or link.group(1), value)
            parts.append(("text", re.sub(r"\s+", " ", value)))

    def read(node: object) -> None:
        for child in node:
            if child.tag == "code":
                parts.append(("code", "".join(child.itertext())))
            elif child.tag == "span" and child.get("class") == FORMULA_CLASS:
                parts.append(("math", "".join(child.itertext())))
            elif child.tag in {"em", "strong"}:
                parts.append(("open", child.tag))
                text(child.text)
                read(child)
                parts.append(("close", child.tag))
            elif child.tag == "br":
                text(" ")
            elif child.tag != "img":
                # A link, or anything else, keeps its words only: an authored
                # target is relative to blueprint/.
                text(child.text)
                read(child)
            text(child.tail)

    text(element.text)
    read(element)
    return parts


def _lead_html(parts: list[tuple[str, str]], *, whole: bool = False) -> str:
    """The first sentence of ``parts`` as escaped HTML, or with *whole* all of it.

    The hero is raw HTML, so Markdown never runs over it. Escaping every part
    means the only tags are the emphasis, code and formula span built here,
    and a formula is the span the page's MathJax already typesets.

    The sentence ends at the first period in the prose that whitespace and
    anything but a lowercase ASCII letter follow, unless the period ends an
    abbreviation such as "i.e." or "Ch." or a name's initial, or "et al."
    that no capital follows. A period inside code, a formula or a `$$` pair
    the converter left as text never ends it.
    """
    plain, starts = "", []
    for kind, value in parts:
        starts.append(len(plain))
        # Code and formulas stand in as one character no rule below matches.
        plain += value if kind == "text" else "\x01" if kind in {"code", "math"} else ""
    cut = len(plain)
    for stop in () if whole else re.finditer(r"\.\s+(?![a-z])", plain):
        before = plain[: stop.start()]
        words = [word.lstrip("(") for word in before.split()] or [""]
        word, previous = words[-1], words[-2] if len(words) > 1 else ""
        after = plain[stop.end() :]
        # An all-caps word, such as "CH." or "EGA I.", is not an abbreviation.
        abbreviation = not word.isupper() and word.lower() in _LEAD_ABBREVIATIONS
        if (
            before.count("$$") % 2
            or (abbreviation and not (word.lower() == "al" and after[:1].isupper()))
            or _is_initial(word, previous, after)
        ):
            continue
        cut = stop.start()
        break
    kept = plain[:cut].rstrip()
    cut = len(kept if whole else kept.rstrip(".").rstrip())
    begin = len(plain) - len(plain.lstrip())
    shown: list[str] = []
    opened: list[str] = []
    for (kind, value), start in zip(parts, starts):
        if start >= cut:
            break
        if kind == "text":
            shown.append(html.escape(value[max(begin - start, 0) : cut - start]))
        elif kind == "code":
            shown.append(f"<code>{html.escape(value)}</code>")
        elif kind == "math":
            shown.append(f'<span class="{FORMULA_CLASS}">{html.escape(value)}</span>')
        elif kind == "open":
            opened.append(value)
            shown.append(f"<{value}>")
        else:
            opened.pop()
            shown.append(f"</{value}>")
    return "".join(shown) + "".join(f"</{tag}>" for tag in reversed(opened)) + ("" if whole else ".")


#: Words whose period does not end the hero's sentence.
_LEAD_ABBREVIATIONS = frozenset(
    {"i.e", "e.g", "cf", "ch", "thm", "al", "prop", "sec", "eq", "fig", "vs", "resp", "def", "lem", "cor"}
)
#: Words a single capital names rather than abbreviates, as in "Theorem A.".
_LEAD_LABELS = frozenset(
    {"theorem", "lemma", "proposition", "corollary", "conjecture", "definition", "chapter", "part",
     "section", "appendix", "case", "step", "example", "exercise", "figure", "table", "problem"}
)


def _is_initial(word: str, previous: str, after: str) -> bool:
    """Whether ``word`` is a name's initial, as in "Luis H. Gallardo" or "J. S.
    Milne", so its period does not end the sentence. A capital after a
    lowercase word, an all-caps word or a label ("the group G.", "EGA I.",
    "Theorem A.") ends one, unless another initial follows it."""
    previous = previous.rstrip(".")
    if len(word) != 1 or not word.isupper():
        return False
    return bool(
        re.match(r"[A-Z]\.(?:\s|$)", after)
        or not previous
        or (
            previous[:1].isupper()
            and not (len(previous) > 1 and previous.isupper())
            and previous.lower() not in _LEAD_LABELS
        )
    )


def _document_body(text: str) -> str:
    """Drop frontmatter and the first H1 while retaining the document's sections."""
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                start = index + 1
                break

    kept: list[str] = []
    dropped_title = False
    source_lines = lines[start:]
    for line, visible in zip(source_lines, mask_fences_and_comments(source_lines)):
        heading = _HEADING.match(visible)
        if heading is not None and len(heading.group(1)) == 1 and not dropped_title:
            dropped_title = True
            continue
        kept.append(line)
    return "\n".join(kept).strip()
