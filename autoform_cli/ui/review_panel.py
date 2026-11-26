"""The `--review` panel and read-back cards."""

from __future__ import annotations

import html
import os
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .._fs import is_within as _is_within
from .._fs import relative as _relative
from ..approvals import approval_statuses
from ..graph import SOURCES_DIR, Graph, GraphValidationError, load_graph
from ..markdown import PAGE_SUFFIXES
from ..readback import Readback, readback_for
from ..review import (
    ReviewArticle,
    ReviewError,
    ReviewFinding,
    ReviewState,
    build_review_bundle,
    readback_errors,
    review_states,
)
from ..skeleton import NodeSkeleton, SkeletonReport, _plural, source_passage
from ..testimony import formula_problems, split_testimony, typesetting_warnings
from .book_order import _book_sources
from .capture import _is_hidden
from .markdown_html import _ONE_LINE, _typed
from .mathjax import CARD_CLASS, CARD_FORMULA_CLASS
from .site_files import _folded, _group_nodes, _site_paths, _written_pages
from .statement_box import _render_rows
from .vault_pages import _is_utf8, _left_out


def _review_disclosures(
    blueprint: Path,
    graph: Graph,
    skeleton: SkeletonReport,
    readbacks: Mapping[tuple[str, str], Readback],
    macros: Mapping[str, object],
    captured: Iterable[str],
    *,
    sources_linked: bool = False,
    unpublished: Iterable[tuple[str, str]] = (),
) -> tuple[dict[str, str], list[str]]:
    """Each Lean-mapped statement ``graph`` publishes, by node id, with its
    review: what a reviewer must trust, what a blind auditor says it means, and
    where its approval stands; and a warning for each formula a current card
    of a published statement shows as typed.

    ``skeleton`` covers the whole vault, a folder that says ``published:
    false`` included, which the copy ``graph`` was loaded from lacks. So the
    evidence is derived from the vault at ``blueprint`` as it is now, which
    refuses a ``skeleton`` extracted from another state, and every article
    ``graph`` publishes must still be the one it was derived from. The
    approval is judged by :func:`~autoform_cli.review.review_states`, as
    ``review check`` judges it, with ``readbacks`` and ``macros`` as the site
    has them. A passage shown must come from a file the site publishes, judged
    by its name in ``captured``, the captured files' names, or with
    ``sources_linked`` from one under ``sources/``, which the site links at the
    verified commit when it does not publish it. A passage from a hidden file,
    or from one of ``unpublished``, the folders capture left out with the
    reason it did, is refused as such.
    """

    changed = "while the site was being rendered; rerun once the blueprint is idle"
    try:
        whole = load_graph(blueprint)
    except GraphValidationError as error:
        reason = f"the blueprint changed {changed}: {'; '.join(error.issues)}"
        raise ReviewError([ReviewFinding("", "review-snapshot-changed", reason)]) from error
    published = {
        node_id
        for node_id, node in whole.nodes.items()
        if not any(_is_within(node.path, folder) for folder in whole.unpublished)
    }
    edited = [
        ReviewFinding(node_id, "review-snapshot-changed", f"the article changed {changed}")
        for node_id in sorted(published | set(graph.nodes))
        if node_id not in graph.nodes
        or node_id not in whole.nodes
        or whole.nodes[node_id].source_sha256 != graph.nodes[node_id].source_sha256
    ]
    # The annotations carry the articles' facts.
    if whole.links_sha256 != graph.links_sha256:
        edited.append(ReviewFinding("", "review-snapshot-changed", f"blueprint/links/ changed {changed}"))
    if edited:
        raise ReviewError(edited)
    bundle = build_review_bundle(whole, skeleton, macros=macros)
    # A link resolved on a disk that ignores case and Unicode form keeps the
    # spelling it was written in; capture refuses two names that differ only
    # in case or Unicode form.
    spellings = {_folded(name): name for name in captured}
    # Render writes its own file where a vault's copy of one of these would go.
    # Planned once: the plan names every page the site writes.
    written_over = frozenset(_site_paths(graph))
    written_pages = _written_pages(written_over, (group for group in _group_nodes(graph) if group in graph.nodes))
    # Of the pages MkDocs would build at one name, the site keeps one and
    # leaves the others out, as _publication picks it: an article's page, or
    # the README.md render builds a view from, over another page, and
    # otherwise the last by name. A leaf is published on its chapter's page,
    # and a page the site leaves out anyway, or one that is not UTF-8, keeps
    # no other out; a page at one of render's own paths, such as the chapter
    # page built from a chapter's README.md, still holds its name.
    articles = {
        _relative(graph.nodes[node_id].path, graph.blueprint_dir).as_posix()
        for node_id in _book_sources(graph).values()
    }
    leaves = {_relative(node.path, graph.blueprint_dir).as_posix() for node in graph.nodes.values()} - articles
    views = {"README.md", "coverage/README.md", f"{SOURCES_DIR}/README.md"}
    at_name: dict[str, list[PurePosixPath]] = {}
    for name in spellings.values():
        page = PurePosixPath(name)
        if (
            page.suffix in PAGE_SUFFIXES
            and name not in leaves
            and (name in written_over or not _left_out(page, written_over, written_pages))
        ):
            url = page.parent / ("index" if page.stem == "README" else page.stem)
            at_name.setdefault(url.as_posix(), []).append(page)
    kept_over = {
        page.as_posix()
        for pages in at_name.values()
        if len(pages) > 1
        for page in sorted(
            (page for page in pages if _is_utf8((graph.blueprint_dir / page.as_posix()).read_bytes())),
            key=lambda page: (page.as_posix() in articles or page.as_posix() in views, page.name),
        )[:-1]
        if page.as_posix() not in articles
    }
    # The copy lacks what capture leaves out: hidden files, the folders that
    # say published: false and a top-level templates/. A passage from one is
    # refused as such, and a locator into a note there, which the vault skips,
    # is skipped in the copy too; one into a note the vault lacks is not, since
    # the vault stops at it. Names are compared as capture lists them, in one
    # Unicode form.
    withheld = [(PurePosixPath(unicodedata.normalize("NFC", folder)), kind) for folder, kind in unpublished]

    def unpublished_at(name: str) -> str | None:
        """The reason capture left out the folder holding ``name``, if it did."""
        composed = PurePosixPath(unicodedata.normalize("NFC", name))
        return next((kind for folder, kind in withheld if _is_within(composed, folder)), None)

    def skipped(target: str, page: Path) -> bool:
        name = _cited_name(target, page, graph.blueprint_dir)
        hidden = _is_hidden(PurePosixPath(name))
        # Before Python 3.14, `Path.is_file` raises under an unreadable directory.
        return (
            name.casefold().endswith(".md")
            and (hidden or unpublished_at(name) is not None)
            and os.path.isfile(blueprint / name)
        )

    # A passage shown must be the one the copy cuts, from a file the site holds.
    for article in bundle.articles:
        if article.node_id not in graph.nodes:
            continue
        # The locator ends in its line range; a file's name may hold a #.
        cited = (article.passage_locator or "").rpartition("#")[0]
        if cited and (kind := unpublished_at(cited)):
            reason = (
                f"cites a passage from {cited}, in a folder that says published: false"
                if kind == "unpublished"
                else f"cites a passage from {cited}, in templates/, which the site leaves out as MkDocs does"
            )
            edited.append(ReviewFinding(article.node_id, "review-source-unpublished", reason))
            continue
        if cited and _is_hidden(PurePosixPath(cited)):
            reason = f"cites a passage from {cited}, a hidden file render does not read"
            edited.append(ReviewFinding(article.node_id, "review-source-unpublished", reason))
            continue
        node = graph.nodes[article.node_id]
        kept = tuple(target for target in node.sources if not skipped(target, node.path))
        if kept != node.sources:
            node = replace(node, sources=kept)
        issues: list[str] = []
        shown = source_passage(node, graph.blueprint_dir, issues=issues)
        if shown == (article.passage, article.passage_locator):
            if article.passage_locator is not None:
                cited = spellings.get(_folded(cited), cited)
                if _left_out(
                    PurePosixPath(cited), written_over, written_pages,
                    sources_linked=sources_linked, kept_over=kept_over,
                ):
                    reason = f"cites a passage from {cited}, a file the site leaves out"
                    edited.append(ReviewFinding(article.node_id, "review-source-unpublished", reason))
            continue
        if shown[0] is None and issues:
            reason = f"cites a passage the site does not publish: {issues[0]}"
            edited.append(ReviewFinding(article.node_id, "review-source-unpublished", reason))
        else:
            edited.append(ReviewFinding(article.node_id, "review-snapshot-changed", f"the cited passage changed {changed}"))
    if edited:
        raise ReviewError(edited)
    states = review_states(whole, bundle, readbacks)
    labels = approval_statuses(states)
    disclosures: dict[str, str] = {}
    warnings: list[str] = []
    for article in bundle.articles:
        # Only what the site publishes: neither the page nor a warning names
        # a statement in a folder it leaves out.
        if article.node_id not in graph.nodes:
            continue
        disclosures[article.node_id] = _review_disclosure(
            article,
            skeleton.node(article.node_id),
            states[article.node_id],
            labels[article.node_id].label if article.node_id in labels else None,
            readbacks,
        )
        for declaration in article.declarations:
            card = readback_for(readbacks, article.node_id, declaration.name, article.article_id)
            if card is not None and not readback_errors(declaration, card):
                warnings.extend(
                    f"{article.node_id}: read-back for {declaration.name}: {warning}"
                    for warning in typesetting_warnings(card.text)
                )
    return disclosures, warnings


def _cited_name(target: str, page: Path, blueprint: Path) -> str:
    """The path in ``blueprint`` that a link ``target`` on ``page`` names, as
    :func:`~autoform_cli.skeleton.source_passage` resolves it, or "" for none."""

    try:
        parsed = urlsplit(target)
    except ValueError:
        return ""
    if parsed.scheme or parsed.netloc or not parsed.path:
        return ""
    spelled = Path(os.path.normpath(page.parent / unquote(parsed.path)))
    try:
        return spelled.relative_to(blueprint.resolve()).as_posix()
    except ValueError:
        return ""


def _review_disclosure(
    article: ReviewArticle,
    record: NodeSkeleton,
    state: ReviewState,
    label: str | None,
    readbacks: Mapping[tuple[str, str], Readback],
) -> str:
    count = len(record.declarations)
    lines_to_trust = sum(item.skeleton_lines for item in record.declarations)
    approval = {
        "incomplete": ("open", "not yet approved"),
        "unapproved": ("open", "ready to approve"),
        "unverifiable": ("drift", "approval cannot be verified because testimony is incomplete or invalid"),
        "drift": ("drift", "the recorded approval is not the current complete review"),
        "approved": (label, label),
    }
    kind, shown = approval[state.status]
    # The summary is not selectable, so the hash to copy into review_approved
    # is a row of the panel.
    # The evidence of the review hash below, which `review check` prints.
    rows = [("Prepared evidence", f"<code>{html.escape(article.legacy_evidence_hash)}</code>")]
    if state.review_hash is not None:
        rows.append(("Review hash", f"<code>{html.escape(state.review_hash)}</code>"))
    if state.approved is not None:
        rows.append(("Approved", f"<code>{_typed(state.approved, one_line=True)}</code>"))
    # Excluded from search: the panel holds packets and testimony, which would
    # fill the index, as #90 keeps large pages out of it.
    parts = [
        '<details class="bp-review" markdown="1" data-search-exclude>',
        f"<summary>Review · {_plural(count, 'skeleton')}, {_plural(lines_to_trust, 'line')} to trust · "
        f'<span class="bp-review-{kind}">{html.escape(shown)}</span></summary>',
        "",
        _render_rows(rows, css_class="bp-skeleton-meta"),
        "",
    ]
    if article.passage is not None:
        locator = article.passage_locator or "cited source"
        parts.extend(
            [
                '<div class="bp-skeleton">',
                f'<div class="bp-skeleton-title">Source passage · {_typed(locator, one_line=True)}</div>',
                f"<pre><code>{_typed(article.passage)}</code></pre>",
                "</div>",
                "",
            ]
        )
    for declaration, prepared in zip(record.declarations, article.declarations):
        # The packet shown is the one whose hash the card records, never a
        # friendlier reconstruction that could hold text the auditor never saw.
        # The approval binds the packet alone, so each other row says so: they
        # describe the declaration as built now, its proof included.
        assumed = ", ".join(f"<code>{_typed(name, one_line=True)}</code>" for name in declaration.assumed)
        axioms = ", ".join(f"<code>{_typed(name, one_line=True)}</code>" for name in declaration.axioms)
        rows = [
            ("Packet", f"<code>{html.escape(prepared.packet_hash)}</code> · what the approval binds"),
            ("Statement hash", f"<code>{html.escape(declaration.statement_hash)}</code> · a record, not bound"),
            ("Assumes", f"{assumed or 'nothing beyond Lean core'} · proof included, not bound"),
            ("Axioms", f"{axioms or 'none'} · proof included, not bound"),
        ]
        packet = _typed(prepared.packet.rstrip("\n"))
        card = readback_for(readbacks, article.node_id, declaration.name, article.article_id)
        errors = () if card is None else readback_errors(prepared, card)
        # The read-back before the packet, as Human Review walks a person
        # through them, so the Lean does not frame the testimony.
        parts.extend(
            [
                '<div class="bp-skeleton">',
                f'<div class="bp-skeleton-title">{_typed(declaration.kind, one_line=True)} '
                f"<code>{_typed(declaration.name, one_line=True)}</code></div>",
                "</div>",
                "",
                _readback_block(card, errors),
                "",
                '<div class="bp-skeleton">',
                f'<pre class="bp-lean"><code>{packet}</code></pre>',
                _render_rows(rows, css_class="bp-skeleton-meta"),
                "</div>",
                "",
            ]
        )
    parts.append("</details>")
    return "\n".join(parts)


#: How render marks a card's formulas for the card's TeX input.
_CARD_DELIMITERS = {"inline": ("\\(", "\\)"), "display": ("\\[", "\\]")}


def _readback_block(card: Readback | None, errors: tuple[str, ...]) -> str:
    """A card as one line of HTML, which the page's Markdown leaves alone.

    The testimony is shown as typed, with line ends kept as character
    references. A current card's formulas are the pieces
    :func:`~autoform_cli.testimony.split_testimony` finds: each that
    :func:`~autoform_cli.testimony.formula_problems` accepts is marked for the
    card's own TeX input, and any other is shown as typed, dollar signs and
    all. An invalid card's are all left as typed.
    """

    if card is None:
        return f'<div class="{CARD_CLASS} bp-readback-missing">No read-back filed for this skeleton yet.</div>'
    pieces = [("text", card.text)] if errors else split_testimony(card.text)
    shown = []
    for kind, piece in pieces:
        if kind == "text":
            shown.append(html.escape(piece))
        elif formula_problems(piece):
            delimiter = "$$" if kind == "display" else "$"
            shown.append(html.escape(delimiter + piece + delimiter))
        else:
            opening, closing = _CARD_DELIMITERS[kind]
            shown.append(f'<span class="{CARD_FORMULA_CLASS}">{opening}{html.escape(piece)}{closing}</span>')
    body = "".join(shown)
    model = f" · {html.escape(card.model)}" if card.model else ""
    status = "invalid · " + "; ".join(errors) if errors else "current"
    return (
        f'<div class="{CARD_CLASS} bp-readback-{"invalid" if errors else "current"}">'
        f'<div class="bp-readback-title">Read-back{model} · <span class="bp-readback-status">{html.escape(status)}</span></div>'
        f'<div class="bp-readback-body">{body}</div></div>'
    ).translate(_ONE_LINE)
