"""Build the published blueprint from the Markdown vault.

The vault stays the source of truth. This module writes a *derived* copy in
which every node page is wrapped in a statement box carrying its
derived status, a link to the Lean code that discharges it, and its place in
the DAG -- the presentation ``leanblueprint`` gives a LaTeX blueprint, driven
from Markdown instead.

The user-facing rules are the publication contract in
``autoform_cli/reference/runtime-and-publication.md``. The old dependency
pages, the full route among them, redirect to the book, whose global search
index resolves their ``#node=`` links. ``autoform-publication/v3`` records
what v2 records, but the site holds the shared explorer's JSON projections and
script in place of v2's Mermaid dependency maps, and the blueprint hash is taken
under a label of its own, so no v2 ``source_revision`` equals a v3 one.

Repository matching: a clean checkout that Git converts on checkout still
matches its commit. A blueprint outside the repository, or in a submodule or
other checkout nested in it, gets no blueprint links and no warning.
"""

from __future__ import annotations

import hashlib
import heapq
import html
import json
import os
import re
import secrets
import unicodedata
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from .. import status
from .._fs import is_within as _is_within
from .._fs import relative as _relative
from .._fs import resolve as _resolve
from .._fs import resolving_once
from .._repository import (
    detect_ref,
    detect_repository_url,
    repository_top_level,
    tracked_files,
    verify_repository_snapshot,
)
from ..annotation_writer import MATHLIB_DIR
from ..annotations import LINKS_DIR
from ..approvals import approval_statuses
from ..coverage import (
    COVERAGE_DISPOSITIONS,
    CoverageSummary,
    contract_table,
    coverage_target_nodes,
    load_coverage,
    validate_coverage_roles,
)
from ..graph import (
    SOURCES_DIR,
    UNREAD_FACTS,
    Graph,
    GraphValidationError,
    PublicationError,
    _containers,
    _countable,
    _is_countable,
    _module_inventories,
    atlas_omissions,
    load_graph,
    pinned_list_omissions,
    unpublished_setting_issues,
)
from ..lean import (
    PUBLICATION_MANIFEST,
    PUBLICATION_SCHEMA,
    LeanSourceError,
    SourceLinker,
    declaration_names,
    index_failure_message,
    resolve_lean_root,
    snapshot_project_sources,
)
from ..library.pinned_names import FOLDER as PINNED_NAMES_FOLDER
from ..markdown import (
    _BRACKET_ESCAPE,
    _CODE_SPAN,
    _INLINE_MATH,
    FORMULA_CLASS,
    PAGE_HEADER,
    PAGE_SUFFIXES,
    _blank,
    _blank_code,
    _display_formula,
    _unquoted,
    blueprint_root,
    directory_path,
    frontmatter_end,
    mask_fences_and_comments,
    link_targets,
    name_text,
    only_links,
    published_page,
    quoted_destinations,
    reference_images,
    reference_links,
    relative_link,
    render_tree,
    rendered_visible_text,
    statement_and_notes,
    title_text,
    yaml_string,
)
from ..markdown import HEADING as _HEADING
from ..markdown import _LABEL_ALONE, _definition_at
from ..markdown import _LINK_DEFINITION as _SITE_LINK_DEFINITION
from ..markdown import content_lines as _content_lines
from ..markdown import outside_fences as _outside_fences
from ..markdown import text_blocks as _text_blocks
from ..readback import READBACKS_DIR, Readback, load_readbacks, readback_for
from ..review import (
    ReviewArticle,
    ReviewError,
    ReviewFinding,
    ReviewState,
    build_review_bundle,
    readback_errors,
    review_states,
)
from ..scaffold import autoform_revision
from ..skeleton import NodeSkeleton, SkeletonReport, _plural, source_passage
from ..testimony import formula_problems, split_testimony, typesetting_warnings
from ..tex_macros import TEX_MACROS, project_macros
from . import _page_pool, dag_viewer, graph_pages, graph_views, mathjax, site_index, tex_plain
from .capture import (
    _FOLDER_REASONS,
    _capture_blueprint,
    _captured_files,
    _files_left_out,
    _is_hidden,
    _load_graph,
    _private_copy,
    _source_revision,
    _unpublished_folders,
)
from .graph_views import (
    _containment_children,
    _leaf_descendants,
)
from .integrated_page import _first_paint, _integrated_page, _overview_routes, _reader_marks, _source_scopes
from .lean_blocks import _lean_fences_as_blocks
from .markdown_html import (
    _ONE_LINE,
    _as_fenced,
    _as_published,
    _body_converter,
    _convert_body,
    _protect_markdown_in_html,
    _protect_narrative_markdown,
    _typed,
)
from .mathjax import CARD_CLASS, CARD_FORMULA_CLASS, MATHJAX_SCRIPT
from .publishable import publishable_article, shown_as_typed
from .site_files import (
    _GENERATED_FILES,
    _IMAGE_SUFFIXES,
    _PUBLISHED_SOURCE_SUFFIXES,
    _STATIC_SUFFIXES,
    COVERAGE_PAGE,
    DAG_SCRIPT,
    DAG_STYLE,
    LOGO,
    MERMAID_SCRIPT,
    STRUCTURE_PAGE,
    STYLESHEET,
    _anchor,
    _folded,
    _group_nodes,
    _group_page,
    _in_the_way,
    _logo,
    _map_assets,
    _mermaid_script,
    _page_url,
    _site_paths,
    _stylesheet,
    _written_pages,
)
from .site_output import RenderReport, _destination_state, _publish, _recover_backup
from .statement_box import _node_references, _render_environment, _render_rows
from .titles import (
    _HEADING_TAG,
    _file_title,
    _first_h1,
    _header_title,
    _label_html,
    _page_heading,
    _title_html,
    _titled,
)

# Links are matched in a line masked by _link_mask, where brackets in code such
# as ``[0,1]^n`` and in math such as ``$(0,1]$`` are blanks. Any other bracket in
# a label must be paired, so an earlier "[1]" or "[x]" on the line stays text.
_LINK_LABEL = r"(?:[^\[\]]|\[[^\[\]]*\])*"
#: An inline link. A bare destination or a title may hold one level of
#: balanced parentheses, a quoted title may leave a `(` open, and an escaped
#: parenthesis is text, as in Python-Markdown. A rewrite drops the title of a
#: link it moves, so a match that ran past the link's own `)` would drop text.
_MARKDOWN_LINK = re.compile(
    rf"(?<!!)\[(?P<label>{_LINK_LABEL})\]\(\s*"
    r"(?P<target><[^>]+>|(?:\\.|[^\\()\s]|\((?:\\.|[^\\()\s])*\))+)"
    r"""(?:\s+(?:"[^")]*"\s*|'[^')]*'\s*|(?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*))?\)"""
)
#: An inline image, read as an inline link is.
_MARKDOWN_IMAGE = re.compile("!" + _MARKDOWN_LINK.pattern.removeprefix("(?<!!)"))
#: A reference-style link definition, `[label]: target "title"`. Markdown
#: resolves `[Paper][paper]` through one of these, so a rewrite that only sees
#: inline links leaves the destination behind and publishes a dead link. As in
#: Python-Markdown, at most a quoted or parenthesized title follows the target:
#: a line such as `[^1]: Grounded in [Paper](...)` is prose, not a definition.
#: Python-Markdown expands tabs before it reads one, so a tab around the title
#: counts as spaces do.
_LINK_DEFINITION = re.compile(
    r'^(?P<indent>[ ]{0,3})\[(?P<label>[^\]]+)\]:[ \t]*'
    r'(?P<target><[^>\r\n]+>|[^\s]+)'
    r'(?P<rest>[ \t]*(?:(?P<quote>["\'])(?:.*)(?P=quote)[ \t]*|\(.*\)[ \t]*)?)$'
)
#: Quote and list markers, in any order, before a definition the site reads in
#: a quote or a list item, matched once tabs are expanded. The block reader
#: joins a line that carries on a paragraph to it, so a marker on a line of its
#: own opens a quote or an item.
_DEFINITION_CONTAINER = re.compile(r"^(?: {0,3}(?:> ?|(?:[*+-]|\d+\.) +))+")
#: A block marker an image's kept text may start with: a heading or quote
#: marker, a bullet or rule character, or an ordered item's period.
_KEPT_TEXT_MARKER = re.compile(r"^ {0,3}\d*(?P<char>(?<=\d)\.|(?<!\d)[#>*+-])")
_ARTICLE_SLOT = re.compile(
    rf"^(?P<indent>[ \t]*)[-*+]\s+\[(?=[^\]]){_LINK_LABEL}\]"
    r"\(\s*(?P<target>[^)\s]+)(?:\s+[^)]*)?\)\s*$"
)


#: The explorer's files at paths a blueprint may already use for files of its own.
_EXPLORER_PATHS = (graph_pages.PROJECT_PAYLOAD, DAG_SCRIPT, DAG_STYLE)


def publication_issues(graph: Graph, blueprint: Path, *, pool: bool | None = None) -> list[str]:
    """What ``autoform check`` reports about the site for ``graph``: the
    warnings :func:`_publication` gives for the pages and files render would
    publish, then what would stop render, the inputs it refuses to capture
    and the files in the way of its own. The files under ``sources/`` are read
    as a render without repository links publishes them, so the verdict does
    not depend on the clone's remote. ``pool`` lets worker processes read the
    node pages, or not, as check's command line does; left out, the caller's
    setting holds.
    """

    # The root load_graph took, so every node's path lies below it.
    blueprint = blueprint_root(blueprint)
    try:
        # Capture leaves out the folders that say published: false, as render does.
        snapshot, unpublished_nodes = _capture_blueprint(blueprint)
    except PublicationError as exc:
        # Render refuses the same inputs, and says so as errors.
        return [f"{issue}; autoform render stops until this is fixed" for issue in exc.issues]
    files = _captured_files(snapshot, blueprint)
    # Render's graph lacks the articles in those folders, so it writes no page for them.
    graph = replace(
        graph, nodes={node_id: node for node_id, node in graph.nodes.items() if node_id not in unpublished_nodes}
    )
    with resolving_once(), nullcontext() if pool is None else _page_pool.allowed(pool):
        site = _publication(
            graph,
            blueprint,
            files,
            destination=blueprint,
            sources_base=None,
            unpublished=_unpublished_folders(snapshot, blueprint),
        )
    stops = _in_the_way(blueprint, graph, files)
    return [*site.warnings, *(f"{issue}; autoform render stops until this is fixed" for issue in stops)]


@dataclass(frozen=True, slots=True)
class _Publication:
    """What render publishes from a blueprint's own files, and what it reports."""

    groups: dict[str, list[str]]
    #: Where each node is published: its page, and its anchor there.
    targets: dict[str, tuple[Path, str]]
    #: Each article's file, by its canonical path, and the node it is.
    node_sources: dict[Path, str]
    #: The files copied into the site, by their path in the blueprint: each
    #: page's Markdown as published, and any other file's bytes.
    files: dict[Path, str | bytes]
    #: Each formalizable leaf's and module catalog's statement and the
    #: sections after it, as published in its box on its chapter's page.
    statements: dict[str, tuple[str, str]]
    #: The project macros the site's ``javascripts/mathjax.js`` is built
    #: with, from the captured ``tex-macros.json`` the site publishes. Render
    #: builds the script as it writes it, so check reads no packaged file.
    macros: dict[str, object]
    #: What the site leaves out or shows as typed, which publishes nonetheless.
    warnings: list[str]
    #: The pages shown as typed, by where they are published.
    typed: frozenset[Path]
    #: Captured articles whose outgoing links are not published as live links.
    unavailable_source_ids: frozenset[str] = frozenset()
    #: The captured files the site links in the repository instead, and where.
    linked: dict[Path, str] = field(default_factory=dict)
    #: The captured files the site leaves out, and why, as a reason code.
    omitted: dict[Path, str] = field(default_factory=dict)


def _publication(
    graph: Graph,
    blueprint: Path,
    snapshot: dict[Path, bytes],
    *,
    destination: Path,
    sources_base: "_SourceBase | None",
    unpublished: tuple[Path, ...] = (),
) -> _Publication:
    """The pages and files the site for ``graph`` gets from ``snapshot``, the
    bytes render captured by path, and the warnings about them: each page's,
    named by its node or its file, then the MathJax configuration's.

    Every page an author wrote is untrusted, so each is published as the
    text :func:`~autoform_cli.ui.publishable.publishable_article` returns for it
    once its links are resolved, and as :func:`~autoform_cli.ui.publishable.shown_as_typed`
    when that function reports anything: the text checked is the text
    published. Links resolve relative to pages, so ``destination`` need only
    be where the site's files would go; check passes the blueprint itself.
    """

    # Nodes are published as environments on their milestone page, the way a
    # blueprint chapter carries many statements in sequence. Each keeps an
    # anchor so every cross-reference still lands on the statement itself.
    groups = _group_nodes(graph)
    containers = _containers(graph)
    anchors = {
        node_id: _anchor(node_id, group)
        for group, node_ids in groups.items()
        for node_id in node_ids
    }
    group_pages = {group: destination / _group_page(group) for group in groups}
    targets = {
        node_id: (group_pages[group], anchors[node_id])
        for group, node_ids in groups.items()
        for node_id in node_ids
    }
    targets.update(
        {
            node_id: (destination / _relative(node.path, blueprint), "")
            for node_id, node in graph.nodes.items()
            if node_id in containers or (not node.formalizable and node.catalog is None)
        }
    )
    node_sources = {
        _resolve(node.path): node_id for node_id, node in graph.nodes.items()
    }

    chapters = {page: group for group, page in group_pages.items()}
    warnings: list[str] = []
    typed: set[Path] = set()
    unavailable_source_ids: set[str] = set()
    # Render writes its own file where a vault's copy of one of these would go.
    written_over = set(_site_paths(graph))
    # Without a narrative, as a roadmap with no README.md has, render writes the chapter's page itself.
    written_pages = _written_pages(written_over, (group for group in groups if group in graph.nodes))
    # The files the site would copy as they are, by their folded paths: a disk
    # that ignores case writes render's file over a vault's spelled in another case.
    written_files = {
        _folded(path): path for path in written_over if PurePosixPath(path).suffix.lower() in _STATIC_SUFFIXES
    }
    # A link to a file the site leaves out keeps only its text, so the strict
    # build finds nothing missing.
    left_out = frozenset(
        _resolve(source)
        for source in snapshot
        if _relative(source, blueprint).as_posix() not in written_over
        and (
            _relative(source, blueprint).parts[:1] == (READBACKS_DIR,)
            or _relative(source, blueprint).parts[0].casefold() == LINKS_DIR
            # MkDocs builds a page only from a suffix spelled in lower case, as .md.
            or (source.suffix not in PAGE_SUFFIXES and source.suffix.lower() not in _STATIC_SUFFIXES)
            or (source.suffix in PAGE_SUFFIXES and not _is_utf8(snapshot[source]))
            # A page MkDocs would publish where render writes a page of its own.
            or _page_url(_relative(source, blueprint).as_posix()) in written_pages
            or _folded(_relative(source, blueprint).as_posix()) in written_files
        )
    )
    # MkDocs builds each page at its name, README's at its folder's index, and
    # of the pages it would build at one name keeps the last in its own order,
    # index.md over README.md and a.md over a.markdown, with a warning that
    # fails the strict build. The site keeps an article's page over another
    # page, and the README.md it builds the landing page, the coverage view or
    # the Sources entry from, and otherwise the page MkDocs keeps, and leaves
    # the others out.
    views = {blueprint / name for name in ("README.md", "coverage/README.md", f"{SOURCES_DIR}/README.md")}
    at_name: dict[str, dict[str, list[Path]]] = {}
    for source in snapshot:
        page = _relative(source, blueprint)
        node_id = node_sources.get(_resolve(source))
        # A leaf is published on its chapter's page, not at a name of its own.
        if source.suffix in PAGE_SUFFIXES and _resolve(source) not in left_out and (
            node_id is None or not targets[node_id][1]
        ):
            name = (page.parent / ("index" if page.stem == "README" else page.stem)).as_posix()
            at_name.setdefault(_folded(name), {}).setdefault(name, []).append(source)
    kept_over: dict[Path, tuple[Path, str]] = {}
    kept: dict[str, Path] = {}
    for spellings in at_name.values():
        for name, sources in spellings.items():
            # MkDocs' order puts a folder's README and index first, then goes by name.
            *others, kept[name] = sorted(
                sources, key=lambda each: (_resolve(each) in node_sources or each in views, each.name)
            )
            for source in others:
                if _resolve(source) not in node_sources:
                    kept_over[source] = (kept[name], name)
                else:  # every article's page is published
                    warnings.append(
                        f"{_relative(source, blueprint).as_posix()}: MkDocs builds it at {name}.html, as it builds "
                        f"{_relative(kept[name], blueprint).as_posix()}, and keeps one of them, so its strict build "
                        "fails; rename one"
                    )
        # A disk that ignores case writes these pages over one another.
        first, *others = sorted(spellings, key=lambda name: (name.rpartition("/")[2] != "index", name))
        warnings.extend(
            f"{_relative(kept[name], blueprint).as_posix()}: MkDocs builds it at {name}.html and "
            f"{_relative(kept[first], blueprint).as_posix()} at {first}.html, so on a disk that ignores case one is "
            "written over the other; rename one of them"
            for name in others
        )
    left_out |= {_resolve(source) for source in kept_over}
    # Render builds the landing page only from README.md, which the nav's Home
    # entry names. Capture refuses two names that differ only in case, so of the
    # root pages named README in another spelling, the one to rename is the one
    # whose name differs from README.md only in case, else the first.
    if blueprint / "README.md" not in snapshot:
        readmes = sorted(
            source
            for source in snapshot
            if source.parent == blueprint
            and source.stem.casefold() == "readme"
            and source.suffix.lower() in PAGE_SUFFIXES
        )
        renamed = next((source for source in readmes if source.name.casefold() == "readme.md"), None)
        renamed = renamed or next(iter(readmes), None)
        # One MkDocs leaves out for its suffix, as README.MD, has that warning, with the same rename.
        if renamed is not None and any(renamed in sources for names in at_name.values() for sources in names.values()):
            warnings.append(
                f"{_relative(renamed, blueprint).as_posix()}: the site builds its home page only from README.md, and "
                "MkDocs' strict build fails without one; rename it README.md"
            )
    # A chapter's leaves are all published on its page, and pages link the
    # same files again and again, so each page's links are built, and each
    # linked path resolved, once for the whole site.
    page_links: dict[Path, Mapping[str, str]] = {}
    resolved: dict[str, Path] = {}
    reader: _page_pool.Reader | None = None

    def rewrite(text: str, source: Path, page: Path) -> tuple[str, list[str]]:
        """``text`` with its links pointing where the site puts their targets,
        and the links it drops."""

        links = page_links.get(page)
        if links is None:
            links = page_links[page] = _anchored_links(targets, page, extension=".md")
        dropped: list[str] = []
        rewritten = _rewrite_links(
            text,
            source_dir=source.parent,
            page=page,
            blueprint=blueprint,
            destination=destination,
            node_sources=node_sources,
            targets=targets,
            sources_base=sources_base,
            left_out=left_out,
            unpublished=unpublished,
            dropped=dropped,
            links=links,
            resolved=resolved,
        )
        return rewritten, dropped

    def published(text: str, source: Path, page: Path, *, whole: bool = True) -> tuple[str, tuple[str, ...]]:
        """The page ``text`` is published as, or the part of ``page`` it is
        unless ``whole``, and what keeps it from being published as written.
        A chapter's page is its narrative with render's own markup set between
        its lines, so each stretch between is read too, as it sits there. Its
        notation goes straight to the warnings."""

        # Both branches call _read_page from this frame, where the reader
        # measures the recursion headroom its workers match.
        prepared = reader.take(text, source, page) if reader is not None else None
        if prepared is None:
            rewritten, dropped = rewrite(text, source, page)
            page_text, found, notation = _read_page(rewritten)
        else:
            rewritten, dropped, read = prepared
            page_text, found, notation = read if read is not None else _read_page(rewritten)
        group = chapters.get(page) if whole else None
        if group is not None:
            # Without the header, each line is where the author wrote it.
            narrative = page_text.removeprefix(PAGE_HEADER)
            layout = _chapter_layout(
                group,
                groups[group],
                narrative,
                graph=graph,
                blueprint=blueprint,
                node_sources=node_sources,
                targets=targets,
            )
            for stretch in _chapter_stretches(narrative, *layout):
                found += _read_page(stretch)[1]
        found = tuple(dict.fromkeys(found))
        label = node_sources.get(_resolve(source)) or _relative(source, blueprint).as_posix()
        warnings.extend(f"{label}: {issue}" for issue in (*found, *notation))
        warnings.extend(
            f"{label}: shows {target!r}, which the site links in the repository rather than copying, so it keeps "
            "only the image's text"
            if isinstance(target, _RepositoryImage)
            else f"{label}: shows {target!r}, which the verified commit does not hold, so it keeps only the "
            "image's text"
            if isinstance(target, _MissingSourceImage)
            else f"{label}: links to {target!r}, which the verified commit does not hold, so the site does not link it"
            if isinstance(target, _MissingSource)
            else f"{label}: links to {target!r}, which is hidden or in a folder the site leaves out, so the site keeps "
            "only the link's text"
            for target in dict.fromkeys(dropped)
        )
        if found and source.parent == blueprint and source.name == "README.md":
            # The landing page takes only the title and the lead, and escapes both.
            warnings.append(f"{label}: the landing page shows only its title and lead, as text")
        elif found:
            warnings.append(f"{label}: the site shows it as typed until these are fixed")
            # A leaf's part of its chapter is its box; the chapter itself is not typed.
            if whole:
                typed.add(page)
        else:
            # Once, here, so render's converter and MkDocs read the same blocks.
            page_text = _lean_fences_as_blocks(page_text)
            # The graph keeps each link a line read alone finds (#325), so
            # where the site reads past a quote, only this warning says so. A
            # statement render sets on a slot line is raw HTML.
            breaks = layout[1] if group is not None else {}
            warnings.extend(f"{label}: {link.warning}" for link in quoted_destinations(rewritten, breaks=breaks))
        return page_text, found

    articles: dict[Path, str] = {}
    statements: dict[str, tuple[str, str]] = {}
    # The captured bytes, the state the graph and any review disclosure
    # describe once capture checks it.
    pages = [
        (node_id, node, snapshot[node.path])
        for node_id, node in graph.nodes.items()
        if node.path in snapshot
    ]
    reader = _page_pool.reader(
        ((captured.decode("utf-8"), node.path, targets[node_id][0]) for node_id, node, captured in pages),
        rewrite,
        articles=len(pages),
        read=publishable_article,
    )
    try:
        for node_id, node, captured in pages:
            leaf = (node.formalizable or node.catalog is not None) and node_id not in containers
            text, found = published(captured.decode("utf-8"), node.path, targets[node_id][0], whole=not leaf)
            if found:
                unavailable_source_ids.add(node_id)
            else:
                # Only a page shown as written has headings to compare with the loader's.
                warnings.extend(node.site_heading_warnings)
            # Narrative articles remain book pages. Formalizable leaves and module
            # catalogs are consolidated into their containing article with stable anchors.
            if leaf:
                parts = statement_and_notes(text)
                # A flagged leaf's box shows its statement and notes as typed.
                statements[node_id] = (shown_as_typed("\n\n".join(filter(None, parts)), page=False), "") if found else parts
            else:
                articles[_resolve(node.path)] = (
                    shown_as_typed(text) if found
                    else text if node_id in groups
                    else _protect_narrative_markdown(text)
                )
    finally:
        if reader is not None:
            reader.close()

    files: dict[Path, str | bytes] = {}
    linked: dict[Path, str] = {}
    omitted: dict[Path, str] = {}
    for source in sorted(snapshot):
        relative = _relative(source, blueprint)
        # With the commit verified, a source note is a page and its images are
        # copied; the folder's scans and data are reached in the repository.
        if (
            sources_base is not None
            and relative.parts[:1] == (SOURCES_DIR,)
            and source.suffix not in PAGE_SUFFIXES
            and source.suffix.lower() not in _IMAGE_SUFFIXES
        ):
            linked[source] = _source_href(sources_base, relative.parts[1:])
            continue
        # Read-back cards are testimony about a statement, shown in its review
        # panel under --review, never pages of their own.
        if relative.parts[:1] == (READBACKS_DIR,):
            omitted[source] = "review-testimony"
            continue
        # The graph has read atlas.json, and nothing on the site reads it. It may
        # name articles in unpublished folders, so it stays out of the site.
        if relative.as_posix().casefold() == "atlas.json":
            omitted[source] = "graph-input"
            continue
        # The graph has read the pinned name lists too, and nothing on the site reads them.
        if relative.parent.as_posix() == PINNED_NAMES_FOLDER and relative.suffix == ".jsonl":
            omitted[source] = "graph-input"
            continue
        # And the annotations, whose facts the pages show; spelled otherwise, loading warns and reads none.
        if relative.parts[0].casefold() == LINKS_DIR:
            omitted[source] = "graph-input"
            continue
        if _resolve(source) in node_sources:
            if _resolve(source) in articles:
                files[source] = articles[_resolve(source)]
        elif (generated := written_pages.get(_page_url(relative.as_posix()))) is not None:
            omitted[source] = "generated"
            warnings.append(
                f"{relative.as_posix()}: autoform render writes {generated} itself, so the site leaves this page "
                "out; rename or move it"
            )
        elif (kept_page := kept_over.get(source)) is not None:
            # An article's page, or a page render builds a view from, keeps its name.
            keeps = _resolve(kept_page[0]) in node_sources or kept_page[0] in views
            warnings.append(
                f"{relative.as_posix()}: MkDocs would build it at {kept_page[1]}.html, where the site publishes "
                f"{_relative(kept_page[0], blueprint).as_posix()}, so the site leaves it out; "
                f"rename {'it' if keeps else 'one of them'}"
            )
            if sources_base is not None and relative.parts[:1] == (SOURCES_DIR,):
                linked[source] = _source_href(sources_base, relative.parts[1:])
            else:
                omitted[source] = "mkdocs-excluded"
        elif source.suffix in PAGE_SUFFIXES:
            try:
                written = snapshot[source].decode("utf-8")
            except UnicodeDecodeError:
                warnings.append(
                    f"{relative.as_posix()}: is not UTF-8 text, as a Markdown page must be, so the site leaves it "
                    "out; save it as UTF-8"
                )
                if sources_base is not None and relative.parts[:1] == (SOURCES_DIR,):
                    # A link to it is reached in the repository, as the rest of sources/ the site leaves out.
                    linked[source] = _source_href(sources_base, relative.parts[1:])
                else:
                    omitted[source] = "not-utf8"
                continue
            text, found = published(written, source, destination / relative)
            files[source] = (
                shown_as_typed(text) if found
                # Render shows the contract's table as the coverage view, and
                # protects the stretches around it then, as on a chapter page.
                else text if relative.as_posix().casefold() == COVERAGE_PAGE
                else _protect_narrative_markdown(text)
            )
        elif source.suffix.lower() in PAGE_SUFFIXES:
            # MkDocs would copy it as it stands, comments and all, rather than build a page from it.
            omitted[source] = "unsupported-type"
            warnings.append(
                f"{relative.as_posix()}: MkDocs builds a page only from a suffix in lower case, so the site leaves "
                f"this file out; rename it {relative.with_suffix(source.suffix.lower()).name}"
            )
        elif relative.as_posix() in written_over:
            # The project's MathJax script is read into the site's own.
            omitted[source] = "mathjax-config" if relative.as_posix() == MATHJAX_SCRIPT else "generated"
        elif (generated := written_files.get(_folded(relative.as_posix()))) is not None:
            omitted[source] = "generated"
            warnings.append(
                f"{relative.as_posix()}: autoform render writes {generated} itself, so the site leaves this file "
                "out; rename or move it"
            )
        elif source.suffix.lower() in _STATIC_SUFFIXES:
            files[source] = snapshot[source]
        elif source.suffix.lower() == ".canvas":
            omitted[source] = "canvas"  # an Obsidian canvas, which the vault keeps for itself
        else:
            omitted[source] = "unsupported-type"
            warnings.append(
                f"{relative.as_posix()}: the site leaves out {source.suffix.lower() or 'extensionless'} "
                "files, since a browser could run one as the site's own page; write it as Markdown, "
                "save it as an image (PNG, JPEG, GIF, WebP), a PDF or plain text, "
                "or keep it outside blueprint/"
            )
    # A note the site leaves out publishes nothing, whatever it sets; a README's setting is about its folder.
    named = [
        (_relative(source, blueprint).as_posix(), data)
        for source, data in snapshot.items()
        if _resolve(source) not in left_out or source.stem.casefold() == "readme"
    ]
    warnings.extend(unpublished_setting_issues(named))
    if blueprint / TEX_MACROS not in snapshot and any(
        _is_within(source, blueprint / TEX_MACROS) for source in snapshot
    ):
        warnings.append(f"{TEX_MACROS}: is a directory, so the site has no project macros; make it a JSON file")
    macros, script_warnings = mathjax._macros_and_warnings(
        snapshot.get(blueprint / MATHJAX_SCRIPT), snapshot.get(blueprint / TEX_MACROS)
    )
    warnings.extend(script_warnings)
    return _Publication(
        groups, targets, node_sources, files, statements, macros, warnings, frozenset(typed),
        frozenset(unavailable_source_ids), linked, omitted,
    )


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _read_page(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """:func:`~autoform_cli.ui.publishable.publishable_article` of ``text``, or,
    for a page nested deeper than the site's converter can read, a finding
    that shows it as typed instead of stopping check or render."""

    try:
        return publishable_article(text)
    except RecursionError:
        finding = "the page nests lists deeper than the site's Markdown converter can read; flatten it"
        return published_page(text), (finding,), ()


def render_site(
    blueprint_dir: str | Path,
    output_dir: str | Path,
    *,
    lean_root: str | Path | None = None,
    repository_url: str | None = None,
    ref: str | None = None,
    review: Callable[[], SkeletonReport] | None = None,
    ileans: str | Path | None = None,
) -> RenderReport:
    """Write deterministic, read-only projections of the Markdown blueprint.

    Authored Markdown remains the only graph authority. Full mathematical reading,
    derived progress, and multiscale graph navigation share one published shell.
    Publication excludes hidden and operational files, rejects symlinks,
    and never embeds timestamps or machine-specific paths. The blueprint and the
    Lean sources are each read once, and every page, link and hash comes from
    those bytes. The site is built beside the output and swapped in whole.

    With ``review``, every Lean-mapped statement shows its review, judged as
    ``review check`` judges it, from the read-back cards and the
    ``tex-macros.json`` this render captured. ``review`` extracts the
    skeletons, reading the vault and the Lean build once more. It is called
    after the checks of the output, the capture, the graph, the coverage
    contract and the Lean sources, so a render they refuse costs no Lean. The
    review is refused unless the articles loaded here, and the sources they
    cite, are the blueprint the extraction read. It covers the whole vault,
    folders that are not published included, and each article and passage it
    shows is the one the site publishes.

    With ``ileans``, a Lean build's ``lib/lean`` directory, the names in each
    statement's Lean link to exactly the constants that build's ``.ilean``
    files record for them (:meth:`SourceLinker.names`), for each declaration
    whose name its file's record still places where the captured source has it.
    """
    blueprint = blueprint_root(blueprint_dir)
    requested_destination = Path(output_dir).expanduser()
    if requested_destination.name in {"", ".."}:  # the swap below would rename a working directory
        raise PublicationError(["output directory must name one ordinary directory"])
    if requested_destination.is_symlink():
        raise PublicationError(["refusing symlink output directory"])
    # Spelled as the blueprint is, so an output that holds it is refused below.
    destination = directory_path(requested_destination) or requested_destination.resolve()
    if (
        _is_within(destination, blueprint)
        or _is_within(blueprint, destination)
        or _reaches(destination, blueprint)
    ):
        raise PublicationError(
            ["blueprint and output directories must be disjoint; refusing destructive render"]
        )
    recovered = _recover_backup(destination)
    snapshot, unpublished_nodes = _capture_blueprint(blueprint)
    # The repository root, not the vault's parent. A blueprint nested at
    # <repo>/docs/blueprint would otherwise be described as <repo>/blueprint,
    # and every generated permalink would 404.
    try:
        repo_root = resolve_lean_root(lean_root) if lean_root is not None else blueprint.parent
    except LeanSourceError as error:
        raise PublicationError([index_failure_message(error)]) from error
    # Links name paths from the Git top level, which may hold the Lean root, or without one the vault's
    # parent, in a subdirectory.
    top_level = repository_top_level(repo_root) or repo_root
    root_prefix = "/".join(repo_root.relative_to(top_level).parts)
    # Everything below reads a private copy of the captured bytes. The copy keeps
    # the blueprint's place in the repository, so a path below the copy's root is
    # the repository path it had when render read the vault.
    relative = blueprint.relative_to(top_level) if _is_within(blueprint, top_level) else Path("blueprint")
    with _private_copy(snapshot, relative) as (copy_root, copy), resolving_once():
        graph = _load_graph(copy, blueprint)
        # A site built now would show these articles with none of their facts; a failed render keeps the last good site.
        if unread := [warning for warning in graph.warnings if warning.startswith(UNREAD_FACTS)]:
            raise PublicationError([f"render writes no site that would show articles without their facts: {unread[0]}"])
        # Evidence in an unpublished folder below roadmap/ stops render, named as
        # such, as audit reports it; below sources/ or in templates/ it keeps its
        # text, as any link does.
        omitted = _unpublished_folders(snapshot, copy)
        below_roadmap = tuple(folder for folder in omitted if _relative(folder, copy).parts[:1] == ("roadmap",))
        coverage, coverage_issues = load_coverage(
            copy,
            unpublished=below_roadmap,
            accepted=_files_left_out(tuple(folder for folder in omitted if folder not in below_roadmap), copy, blueprint),
        )
        if coverage_issues:
            raise PublicationError(
                [
                    f"coverage contract line {issue.line}: {issue.reason}"
                    if issue.line
                    else f"coverage contract: {issue.reason}"
                    for issue in coverage_issues
                ]
            )
        if coverage is None:
            raise PublicationError(["coverage contract could not be loaded"])
        expected = _destination_state(destination)
        # Like every Lean capture, this one skips each folder holding a complete
        # publication.json: a finished site, mkdocs' copy of one, or a replaced
        # site kept as a backup.
        try:
            lean = snapshot_project_sources(repo_root, exclude_roots=(copy_root,))
        except OSError as error:
            raise PublicationError([index_failure_message(error)]) from error
        repository_url = repository_url if repository_url is not None else detect_repository_url(repo_root)
        ref = ref if ref is not None else detect_ref(repo_root)
        # The commit must hold sources/ only when a file render captured there
        # may be linked in it: a hidden file, or a folder that stays off the
        # site, may be missing from the commit, which then holds no sources/.
        sources = any(name.startswith(f"{SOURCES_DIR}/") for name, _data in snapshot.files)
        reasons: list[str] = []
        blueprint_reasons: list[str] = []
        spellings: dict[str, str] = {}
        commit = None
        links_blueprint = False
        if repository_url and ref:
            commit = verify_repository_snapshot(
                repo_root,
                ref,
                [(repo_root / path, data) for path, data in lean.source_files],
                reasons=reasons,
                spellings=spellings,
            )
        # A code link depends only on the Lean bytes. A link into the blueprint also needs the blueprint's
        # bytes in that commit, which a blueprint outside the repository's working tree never has.
        if commit is not None and _in_working_tree(blueprint, top_level):
            links_blueprint = (
                verify_repository_snapshot(
                    repo_root,
                    commit,
                    [(blueprint / name, data) for name, data in snapshot.files],
                    required_directories=[blueprint / SOURCES_DIR] if sources else [],
                    reasons=blueprint_reasons,
                    spellings=spellings,
                )
                is not None
            )
        # Links name the verified commit, never the requested ref. With no Lean file captured, only the
        # blueprint check compared bytes with the commit.
        linked = commit if lean.source_files or links_blueprint else None
        linker = SourceLinker(
            lean.index,
            repository_url,
            linked,
            links_blueprint=links_blueprint,
            spellings=spellings,
            root_prefix=root_prefix,
            sources=dict(lean.source_files),
            ileans=Path(ileans).expanduser() if ileans is not None else None,
        )
        # The inventory accounts for every file the verified commit holds.
        tracked = tracked_files(repo_root, commit, blueprint) if links_blueprint and commit else None
        # Scans and data leave the site only for a directory the verified commit
        # holds, which it may though render captured nothing there: a committed
        # hidden note or folder that stays off the site. The folder is linked then.
        if not sources and tracked is not None and SOURCES_DIR in snapshot.directories:
            sources = any(name.startswith(f"{SOURCES_DIR}/") for name in tracked)
        sources_base = _sources_base(copy, copy_root, linker, tracked) if sources else None
        reviews: dict[str, str] = {}
        review_warnings: list[str] = []
        if review is not None:
            # After every check above, and the one _render_pages makes of the
            # captured files in its way, so a render they refuse runs no Lean.
            if stops := _in_the_way(copy, graph, _captured_files(snapshot, copy)):
                raise PublicationError(stops)
            # The cards and macros the review judges are the ones this render
            # captured, so the site and publication.json describe one state.
            try:
                cards = load_readbacks(graph.blueprint_dir)
            except ValueError as error:
                raise ReviewError([ReviewFinding("", "readback-unlistable", str(error))]) from error
            macros = project_macros(_captured_files(snapshot, copy).get(copy / TEX_MACROS))
            captured = [name for name, _ in snapshot.files]
            reviews, review_warnings = _review_disclosures(
                blueprint,
                graph,
                review(),
                cards,
                macros,
                captured,
                sources_linked=sources_base is not None,
                unpublished=[(name, kind) for name, kind in snapshot.omitted if kind in _FOLDER_REASONS],
            )
        source_revision = _source_revision(snapshot)
        report = _publish(
            destination,
            expected,
            lambda stage: _render_pages(
                copy,
                stage,
                graph,
                coverage,
                linker,
                repo_root=copy_root,
                sources_base=sources_base,
                source_revision=source_revision,
                lean_source_revision=lean.revision,
                unpublished=_unpublished_folders(snapshot, copy),
                unpublished_nodes=unpublished_nodes,
                files=_captured_files(snapshot, copy),
                reviews=reviews,
                left_out=snapshot.omitted,
                tracked=tracked,
            ),
        )
    report.warnings[:0] = recovered
    report.warnings.extend(review_warnings)
    report.warnings.extend(linker.ilean_warnings())
    authored = {*dict(snapshot.files), *dict(snapshot.omitted)}
    report.warnings.extend(
        f"{path} is not published: the dependency explorer writes its own file there"
        for path in _EXPLORER_PATHS
        if path in authored
    )
    if repository_url and ref and commit is None:
        report.warnings.append(
            "repository links were omitted because the captured Lean inputs "
            "do not match one verified local Git commit" + (f": {reasons[0]}" if reasons else "")
        )
    elif blueprint_reasons:
        report.warnings.append(
            "Markdown source links were omitted because the captured blueprint does not match "
            f"the verified local Git commit: {blueprint_reasons[0]}"
        )
    return report


def _render_pages(
    blueprint: Path,
    destination: Path,
    graph: Graph,
    coverage: CoverageSummary,
    linker: SourceLinker,
    *,
    repo_root: Path,
    sources_base: "_SourceBase | None",
    source_revision: str,
    lean_source_revision: str,
    unpublished: tuple[Path, ...] = (),
    unpublished_nodes: frozenset[str],
    files: dict[Path, bytes],
    reviews: dict[str, str],
    left_out: tuple[tuple[str, str], ...] = (),
    tracked: list[str] | None = None,
) -> RenderReport:
    """Write the site into *destination* from the private copy at *blueprint*:
    *left_out* is what capture did not read, and *tracked* every file the
    verified commit holds below the blueprint, if one was verified."""
    # A missing or stale packaged runtime is a publication failure, not an
    # unhandled CLI exception or a reason to publish a different renderer.
    # Capture it once before writing any staged files; _publish keeps the old site.
    try:
        explorer_script = dag_viewer.viewer_script()
    except ValueError as error:
        raise PublicationError([f"packaged explorer runtime is unavailable: {error}"]) from error
    # Audit reports coverage-role issues, the atlas.json entries the loader
    # ignored and what it left out of the pinned name lists. A valid contract
    # still publishes with them, as it does with MAPPED rows. An issue without
    # a line already names the contract in its reason, so it gets no prefix.
    # Check accepts an entry naming an article in an unpublished folder, so
    # render does not warn about it either.
    coverage_warnings = [
        f"coverage contract line {issue.line}: {issue.reason}"
        if issue.line
        else issue.reason
        for issue in validate_coverage_roles(graph, coverage)
    ]
    coverage_warnings += (f"atlas.json: {omission}" for omission in atlas_omissions(graph, unpublished_nodes))
    coverage_warnings += (f"{subject}: {omission}" for subject, omission in pinned_list_omissions(graph))
    statuses = status.derive(graph)
    used_by = _reverse_edges(graph)
    informally_used_by = _reverse_informal_edges(graph)
    formalizing = _formalizing(graph)
    stops = _in_the_way(blueprint, graph, files)
    if stops:
        raise PublicationError(stops)
    site = _publication(
        graph, blueprint, files, destination=destination, sources_base=sources_base, unpublished=unpublished
    )
    revisions = {"source_revision": source_revision, "lean_source_revision": lean_source_revision}
    version, commit, source = autoform_revision()
    revisions["autoform"] = {"version": version or None, "commit": commit or None, "source": source or None}
    # Written first, so a Lean walk by a concurrent render skips this stage;
    # rewritten with the file inventory once every page exists.
    _write_publication_manifest(destination, graph, linker, coverage=coverage, **revisions)

    report = RenderReport(output_dir=destination, warnings=[*coverage_warnings, *site.warnings])
    groups, targets, node_sources = site.groups, site.targets, site.node_sources
    containers = _containers(graph)
    group_pages = {group: destination / _group_page(group) for group in groups}
    for source, content in site.files.items():
        target = destination / _relative(source, blueprint)
        # Directories are created on demand, so a directory holding nothing
        # but absorbed nodes leaves no empty shell behind.
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target.write_text(_titled(content, files.get(source), target), encoding="utf-8")
        else:
            target.write_bytes(content)
        report.pages += 1

    # One reading order for Next up and the search index.
    walk = _reading_walk(blueprint, destination, graph)
    order = _reading_order(
        graph, targets, _written(walk), _card_positions(
            groups, group_pages, graph=graph, blueprint=blueprint, node_sources=node_sources,
            targets=targets, typed=site.typed,
        ),
    )

    overview = destination / "README.md"
    # The author's page, as render publishes it when nothing in it is flagged:
    # the hero escapes its title and lead itself, and a page shown as typed has
    # neither. Only README.md, the page MkDocs builds the home page from, on
    # every disk.
    authored = next(
        (data for source, data in files.items() if source.parent == blueprint and source.name == "README.md"),
        None,
    )
    if overview.is_file() and authored is not None and _is_utf8(authored):
        overview.write_text(
            _render_landing_page(
                published_page(authored.decode("utf-8")),
                graph=graph,
                statuses=statuses,
                coverage=coverage,
                group_pages=group_pages,
                page=overview,
                destination=destination,
                targets=targets,
                order=order,
                # The page the Sources nav entry opens, when there is one.
                sources_page=next(
                    (
                        destination / relative
                        for source, content in site.files.items()
                        if isinstance(content, str)
                        and (relative := _relative(source, blueprint)).parent == Path(SOURCES_DIR)
                        and relative.name.casefold() == "readme.md"
                    ),
                    None,
                ),
            ),
            encoding="utf-8",
        )

    coverage_page = next(
        (
            destination / relative
            for relative in (_relative(source, blueprint) for source in site.files)
            if relative.as_posix().casefold() == COVERAGE_PAGE
        ),
        None,
    )
    if coverage_page is not None and coverage_page not in site.typed:
        staged = coverage_page.read_text(encoding="utf-8")
        shown = _render_coverage_page(
            staged,
            coverage=coverage,
            graph=graph,
            statuses=statuses,
            containers=containers,
            links=_anchored_links(targets, coverage_page),
            node_sources=node_sources,
            rank={node_id: index for index, node_id in enumerate(order)},
        )
        if shown is None:
            report.warnings.append(
                f"{coverage.source_path}: the page render publishes does not hold the contract's table as audit "
                "reads it, so the site shows the page without the coverage view"
            )
            shown = _protect_narrative_markdown(staged)
        coverage_page.write_text(shown, encoding="utf-8")

    for group, node_ids in groups.items():
        page = group_pages[group]
        page.parent.mkdir(parents=True, exist_ok=True)
        narrative = page.read_text(encoding="utf-8") if page.is_file() else None
        chapter, linked, unresolved = _render_chapter(
            group,
            node_ids,
            graph=graph,
            statuses=statuses,
            used_by=used_by,
            informally_used_by=informally_used_by,
            formalizing=formalizing,
            linker=linker,
            page=page,
            targets=targets,
            narrative=narrative,
            blueprint=blueprint,
            repo_root=repo_root,
            node_sources=node_sources,
            containers=containers,
            statements=site.statements,
            reviews=reviews,
            typed=page in site.typed,
        )
        page.write_text(chapter, encoding="utf-8")
        if narrative is None:  # a milestone with no narrative page of its own
            report.pages += 1
        report.nodes += len(node_ids)
        report.linked += linked
        report.unresolved.extend(unresolved)

    # Pages written since, such as a milestone's, join the book here.
    book_pages = _book_page_order(blueprint, destination, graph, walk)
    structure = destination / STRUCTURE_PAGE
    structure.write_text(
        _render_structure_page(
            page=structure,
            entry=destination / graph_pages.book_contents(graph),
        ),
        encoding="utf-8",
    )
    report.pages += 1
    notes = [
        relative
        for source, content in site.files.items()
        if isinstance(content, str) and (relative := _relative(source, blueprint)).parts[:1] == (SOURCES_DIR,)
    ]
    # literate-nav reads each entry as a Markdown link, which carries no <, >
    # or backtick in its path, so the entry would name a file MkDocs cannot find.
    unnamed = [note for note in notes if re.search(r"[<>`]", note.as_posix())]
    report.warnings.extend(
        f"{note.as_posix()}: the nav cannot name a path that holds <, > or a backtick, so the Sources entry "
        "leaves it out; rename it to list it"
        for note in unnamed
    )
    (destination / "SUMMARY.md").write_text(
        _render_summary_nav(
            book_pages,
            destination=destination,
            overview=overview,
            source_pages=[destination / note for note in notes if note not in unnamed],
        ),
        encoding="utf-8",
    )

    contexts: dict[str, tuple[Path, str]] = {}
    records: dict[str, dict[str, object]] = {}
    map_chains: dict[str, tuple[tuple[str, str], ...]] = {}
    generated_graph_pages = graph_pages.write_graph_pages(
        graph,
        statuses,
        destination,
        node_links=lambda page, node_ids: _node_links(targets, page, node_ids),
        book_targets=targets,
        contexts_out=contexts,
        records_out=records,
        map_out=map_chains,
        captured_sources={node_id: files[node.path] for node_id, node in graph.nodes.items() if node.path in files},
        source_paths={node_id: _relative(node.path, blueprint) for node_id, node in graph.nodes.items()},
        unavailable_source_ids=site.unavailable_source_ids,
        warnings_out=report.warnings,
        book_order=order,
    )
    report.pages += sum(path != destination / graph_pages.WIKI_LINKS for path in generated_graph_pages)

    # These are the final safe pages, including generated theorem cards and
    # their exact anchors. Never reread an authored article to build the reader.
    page_contexts: dict[Path, tuple[Path, str, str | None]] = {}
    page_cards: dict[Path, list[str]] = {}
    for node_id, (page, anchor) in targets.items():
        payload, scope = contexts[node_id]
        if page not in page_contexts or not anchor:
            page_contexts[page] = (payload, scope, node_id if not anchor else None)
        if anchor:
            page_cards.setdefault(page, []).append(node_id)
    canonical_pages = {
        destination / _relative(source, blueprint)
        for source, content in site.files.items()
        if isinstance(content, str) and source.suffix.lower() in PAGE_SUFFIXES
    } | set(group_pages.values())
    wrapped_files: set[tuple[int, int]] = set()
    source_scopes = _source_scopes(graph)
    overview_routes = _overview_routes(records)
    # A page that holds a statement is never an overview, so its reading is converted a second time only where it may
    # hold a child index.
    statement_pages = {group_pages[group] for group, node_ids in groups.items() if node_ids}
    for page in sorted(canonical_pages):
        if not page.is_file():
            continue
        metadata = page.stat()
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in wrapped_files:
            continue
        wrapped_files.add(identity)
        payload, scope, owner = page_contexts.get(
            page, (destination / graph_pages.PROJECT_PAYLOAD, "roadmap", None)
        )
        text = page.read_text(encoding="utf-8")
        indexes, reader_overview = _reader_marks(
            text, page=page, destination=destination, owner=owner, scope=scope, records=records, routes=overview_routes,
            statements=page in statement_pages,
        )
        # The page's place, its owner or else its scope, is a topic as the book's applyRoute reads one on an address
        # naming no statement: its own scope in the search index, and neither a source track nor a module's inventory.
        place = owner or scope
        record = records.get(place)
        topic = (
            record is not None and record["scope"] == place and record["navigation"] != "source"
            and graph.nodes[place].catalog != "module"
        )
        # The map's first view opens the place's part, or the nearest one above a place the map flattens or leaves
        # out, as its search would find it; the page carries the map's chain down to that part, which the map checks
        # as it walks it. The roadmap is the map's root, not a part, so its page gets none: the whole project opens.
        part = next((up for up in _containment_chain(graph, place) if up in map_chains), None)
        page.write_text(
            _integrated_page(
                text, page=page, destination=destination,
                payload=payload, scope=scope, owner=owner,
                prepared=True, tracks_lean=graph.tracks_lean,
                overview=_label_html(graph.nodes[owner].title) if reader_overview else None, topic=topic,
                map_path=map_chains[part] if part else (), map_path_for=place if part else None,
                indexes=indexes,
                **_first_paint(
                    graph, targets, page=page, home=overview, scope=scope, owner=owner, source_scopes=source_scopes,
                    cards=page_cards.get(page, ()), roadmap=group_pages.get("roadmap"),
                ),
            ),
            encoding="utf-8",
        )

    for relative, contents in (
        (STYLESHEET, _stylesheet()),
        (MERMAID_SCRIPT, _mermaid_script()),
        # Written here, never kept from the vault, and built only here, so check reads no packaged file.
        (MATHJAX_SCRIPT, mathjax._script(site.macros)),
        (DAG_SCRIPT, explorer_script),
        (DAG_STYLE, dag_viewer._style()),
        *_map_assets(site_index._shown_title(graph_views.group_title(graph, "roadmap"))),
        (LOGO, _logo()),
    ):
        asset = destination / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(contents, encoding="utf-8")
    inventory = _inventory(graph, site, blueprint, destination, files, left_out, tracked)
    _write_publication_manifest(destination, graph, linker, coverage=coverage, inventory=inventory, **revisions)
    return report


def _sources_base(
    blueprint: Path, repo_root: Path, linker: SourceLinker, tracked: Sequence[str] | None = None
) -> "_SourceBase | None":
    """Where `blueprint/sources/` lives in the repository, if it can be linked.

    Source notes are pages of the site, with the images they show. The rest of
    the folder, scans, extracts and data, is reached in the repository, as are
    a hidden note and the folder itself, so the site does not mirror it.
    *tracked*, every file the commit holds below the blueprint, says which
    files there a link can reach.

    Returns ``None`` when there are no repository coordinates to build a link
    from, or the blueprint's files are not in the linked commit. The folder is
    then published as any other, because a site with no way to reach its scans
    is worse than one that copies them.
    """
    if not linker.repository_url or not linker.ref or not linker.links_blueprint:
        return None
    try:
        relative = (blueprint / SOURCES_DIR).resolve().relative_to(repo_root).as_posix()
    except ValueError:
        # The vault is outside the repository being linked, so no blob URL
        # describes it. Better no link than one that 404s.
        return None
    spellings: dict[str, str] = {}
    for path, committed in linker.spellings.items():
        # A link may name a folder as well as a note, so each leading path maps too.
        parts, committed_parts = path.split("/"), committed.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = unicodedata.normalize("NFC", "/".join(parts[:depth]))
            spellings.setdefault(prefix, "/".join(committed_parts[:depth]))
    held = None
    if tracked is not None:
        # A link may name a folder as well as a file, so each leading path counts too.
        held = frozenset(
            "/".join(parts[:depth])
            for parts in (unicodedata.normalize("NFC", name).split("/") for name in tracked)
            if parts[0] == SOURCES_DIR
            for depth in range(1, len(parts) + 1)
        )
    return _SourceBase(linker.repository_url, linker.ref, relative, spellings, held)


@dataclass(frozen=True, slots=True)
class _SourceBase:
    """Where `blueprint/sources` lives in the repository, as parts.

    Kept as parts rather than a formatted URL because a file link and a
    directory link differ by one path segment -- GitHub serves directories
    under `/tree/`, files under `/blob/`. Deriving one from the other by
    replacing the first `/blob/` in the finished string rewrote the wrong
    segment whenever the repository's own URL contained one.
    """

    repository_url: str
    ref: str
    relative: str
    spellings: Mapping[str, str] = field(default_factory=dict)
    """Committed captured paths and their leading folders, keyed by NFC form, since a link spells a path as typed."""
    held: frozenset[str] | None = None
    """What the commit holds under `sources/`, files and their leading folders, below the blueprint in NFC form;
    ``None`` when Git did not list it."""

    def holds(self, relative: Path, path: Path) -> bool:
        """Whether the commit holds *relative*, a path below the blueprint that the copy would hold at *path*."""
        if self.held is None:
            # The copy has no hidden path, so a hidden one is taken as held, as check warns it must be.
            return _is_hidden(relative) or os.path.exists(path)
        return unicodedata.normalize("NFC", relative.as_posix()) in self.held

    def href(self, tail: tuple[str, ...]) -> str:
        verb = "blob" if tail else "tree"
        parts = "/".join((self.relative, *tail)).split("/")
        # A folder with no captured file below it, such as one holding only a `.gitkeep`, has no entry of its own,
        # so only the leading folders it shares with a captured file take their committed spelling.
        for depth in range(len(parts), 0, -1):
            committed = self.spellings.get(unicodedata.normalize("NFC", "/".join(parts[:depth])))
            if committed is not None:
                parts[:depth] = committed.split("/")
                break
        path = "/".join(parts)
        return f"{self.repository_url}/{verb}/{quote(self.ref, safe='')}/{quote(path, safe='/')}"


def _source_href(sources_base: _SourceBase, tail: tuple[str, ...]) -> str:
    """A repository URL for a file under ``sources/`` the site does not publish, or for the folder itself."""
    return sources_base.href(tail)


def publication_source_revision(blueprint_dir: str | Path) -> str:
    """Return the deterministic source hash stored in ``publication.json``."""

    return _source_revision(_capture_blueprint(blueprint_root(blueprint_dir))[0])


def _write_publication_manifest(
    destination: Path,
    graph: Graph,
    linker: SourceLinker,
    *,
    coverage: CoverageSummary,
    source_revision: str,
    lean_source_revision: str,
    autoform: dict[str, str | None],
    inventory: dict[str, object] | None = None,
) -> None:
    paths = sorted(_relative(path, destination).as_posix() for path in destination.rglob("*"))
    manifest = {
        "autoform": autoform,
        "complete": True,
        "coverage": {
            "complete": coverage.complete,
            "counts": coverage.counts,
            "schema": coverage.schema,
            "source_path": coverage.source_path,
            "source_sha256": coverage.source_sha256,
        },
        "directories": [path for path in paths if (destination / path).is_dir()],
        "files": {
            path: hashlib.sha256((destination / path).read_bytes()).hexdigest()
            for path in paths
            if path != PUBLICATION_MANIFEST and (destination / path).is_file()
        },
        "schema": PUBLICATION_SCHEMA,
        "source": "blueprint/roadmap Markdown",
        "source_revision": source_revision,
        "git_ref": linker.ref,
        "lean_source_revision": lean_source_revision,
        "nodes": len(graph.nodes),
        "dependencies": graph.edge_count,
        "views": ["book", "progress", "project", "chapter", "full"],
    }
    if inventory is not None:
        manifest["inventory"] = inventory
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    # Replaced, never truncated in place: a Lean capture that read an empty
    # manifest would index the stage's copies.
    temporary = destination / f".{PUBLICATION_MANIFEST}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o666)  # the mode write_bytes gives
    try:
        with open(descriptor, "wb") as output:
            output.write(encoded)
        os.replace(temporary, destination / PUBLICATION_MANIFEST)
    finally:
        temporary.unlink(missing_ok=True)


def _inventory(
    graph: Graph,
    site: _Publication,
    blueprint: Path,
    destination: Path,
    captured: Collection[Path],
    left_out: tuple[tuple[str, str], ...],
    tracked: list[str] | None,
) -> dict[str, object]:
    """Where each blueprint file went, by its path in the blueprint: its URL on
    the site, from the site's root; the repository URL the site links instead;
    or why the site leaves it out. Every file the verified commit holds, by
    Git's spelling of its name, when there is one (basis ``commit``), else
    every file render captured and each entry it skipped (basis ``capture``).
    On either basis a hidden folder, or one capture leaves out, is one key,
    naming nothing in it, and so is each place in ``links/`` and
    ``links/mathlib/`` where a left-out folder's annotations are kept, and in
    ``readbacks/`` its cards under node ids.

    The reasons: ``hidden``, ``unpublished``, ``mkdocs-excluded`` (a top-level
    ``templates/``, or a page MkDocs builds at the URL of another it keeps),
    ``generated`` (a name render writes or has retired, or a page at the URL
    of one of render's pages), ``mathjax-config`` (read into the
    site's script), ``review-testimony`` (read-back cards), ``graph-input``
    (read by the graph only), ``not-utf8``, ``canvas``, ``unsupported-type`` and
    ``not-captured`` (in the commit, not in the tree render read).
    """

    def url(page: Path, anchor: str = "") -> str:
        relative = _relative(page, destination)
        if relative.suffix.lower() in PAGE_SUFFIXES:
            relative = relative.with_suffix(".html")
        return _as_published(quote(relative.as_posix(), safe="/")) + (f"#{quote(anchor, safe='')}" if anchor else "")

    omitted = {_relative(source, blueprint).as_posix(): reason for source, reason in site.omitted.items()}
    published = {
        relative: url(destination / relative)
        for relative in (_relative(source, blueprint).as_posix() for source in site.files)
    }
    published.update(
        (relative, url(*site.targets[node_id]))
        for node_id, node in graph.nodes.items()
        if node.path in captured
        and node_id in site.targets
        and (relative := _relative(node.path, blueprint).as_posix()) not in omitted
    )
    linked = {_relative(source, blueprint).as_posix(): href for source, href in site.linked.items()}
    # Shortest first, so a folder inside an unpublished one goes under the outer folder's key.
    folders = dict(
        sorted(
            {(unicodedata.normalize("NFC", name), kind) for name, kind in left_out if kind in _FOLDER_REASONS},
            key=lambda folder: len(folder[0]),
        )
    )

    def entry(name: str) -> tuple[str, str]:
        """The key and reason for a file render did not capture: its left-out or hidden folder's, else its own."""
        parts = name.split("/")
        composed = unicodedata.normalize("NFC", name)
        for folder, reason in folders.items():
            if composed == folder or composed.startswith(f"{folder}/"):
                return "/".join(parts[: folder.count("/") + 1]), reason
        for depth, part in enumerate(parts, 1):
            if part.startswith("."):
                return "/".join(parts[:depth]), "hidden"
        # Capture leaves out the top-level names render writes itself.
        return name, "generated" if len(parts) == 1 and name in _GENERATED_FILES else "not-captured"

    def folded(name: str, reason: str) -> tuple[str, str]:
        """The key and reason for a file the graph or a review reads: an annotation file's name, where the writer
        keeps a left-out folder's annotations, or a card's, under a node id, names an article the site leaves out."""
        parts = name.split("/")
        composed = unicodedata.normalize("NFC", name)
        for place in (f"{LINKS_DIR}/{MATHLIB_DIR}/", f"{LINKS_DIR}/"):
            for folder, kept in folders.items():
                if composed.startswith(f"{place}{folder}/"):
                    return "/".join(parts[: place.count("/") + folder.count("/") + 1]), kept
        # A node id is the article's path below roadmap/.
        for folder, kept in folders.items():
            if folder.startswith("roadmap/") and composed.startswith(f"{READBACKS_DIR}/{folder[len('roadmap/') :]}/"):
                return "/".join(parts[: folder.count("/") + 1]), kept
        return name, reason

    if tracked is None:
        omitted.update(entry(name) for name, _kind in left_out)
        basis = "capture"
    else:
        # Git may spell a captured name in another Unicode form, on macOS.
        spelling = {unicodedata.normalize("NFC", name): name for name in tracked}

        def as_committed(placed: dict[str, str]) -> dict[str, str]:
            return {spelling.get(unicodedata.normalize("NFC", key), key): value for key, value in placed.items()}

        published, linked, omitted = as_committed(published), as_committed(linked), as_committed(omitted)
        placed = {*published, *linked, *omitted}
        omitted.update(entry(name) for name in tracked if name not in placed)
        basis = "commit"
    omitted = dict(folded(name, reason) for name, reason in omitted.items())
    return {"basis": basis, "published": published, "linked": linked, "omitted": omitted}


def _book_sources(graph: Graph) -> dict[Path, str]:
    """The book's pages, containers and narrative articles, by resolved source."""
    containers = _containers(graph)
    return {
        _resolve(node.path): node.id
        for node in graph.nodes.values()
        if node.id in containers or (not node.formalizable and node.catalog is None)
    }


def _book_links(source: Path, blueprint: Path, targets: Collection[Path]) -> list[Path]:
    """The *targets* *source* links outside code fences, in the order it links them, on the page render publishes.

    That page leaves out the HTML comments outside code, so a commented-out link
    places nothing, while a ``<!--`` in a code span stays text, as on the site.
    """
    linked_sources: list[Path] = []

    def collect(line: str) -> str:
        for match in _MARKDOWN_LINK.finditer(_link_mask(line)):
            raw = line[match.start("target") : match.end("target")]
            bare = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
            path = bare.partition("#")[0].partition("?")[0]
            try:
                if not path or urlsplit(path).scheme or path.startswith("/"):
                    continue
                candidate = _resolve(source.parent / unquote(path))
                candidate_relative = _relative(candidate, blueprint)
            except ValueError:  # a target outside the vault, an unbalanced IPv6 bracket, or a decoded NUL
                continue
            if (
                not candidate_relative.parts
                or candidate_relative.parts[0] != "roadmap"
                or candidate.suffix.lower() != ".md"
            ):
                continue
            if candidate not in targets:
                continue
            linked_sources.append(candidate)
        return line

    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []  # the site leaves out a page that is not UTF-8, as check warns
    _outside_fences(published_page(text), collect, joined=True)
    return linked_sources


def _book_page_order(blueprint: Path, destination: Path, graph: Graph, walk: list[Path] | None = None) -> list[Path]:
    """The landing page, then the book in reading order (:func:`_reading_walk`).

    SUMMARY.md follows this order, and Next up and the search index follow the
    same walk, so the book reads one way everywhere. A caller that has walked
    the book already passes *walk*."""
    landing = (destination / "README.md").resolve()
    pages = _written(walk if walk is not None else _reading_walk(blueprint, destination, graph))
    return ([landing] if landing.is_file() else []) + [page for page in pages if page != landing]


def _written(walk: list[Path]) -> list[Path]:
    """The pages of *walk* written so far, resolved."""
    return [_resolve(output) for output in walk if output.is_file()]


def _reading_walk(blueprint: Path, destination: Path, graph: Graph) -> list[Path]:
    """Where the book's pages are published, in reading order, written yet or
    not: down the containment tree from the landing page, each page's children in the order it links them,
    then the ones it does not link. A child the page never links directly
    takes the place of the page's first link into its subtree, to a section
    or a result; a direct link wins over that, so a prose shortcut to a deep
    milestone does not pull its chapter forward. A link to a page elsewhere in
    the tree does not move it. The children a page does not link at all
    follow in dependency order, ties by id (:func:`_dependency_sorted`)."""
    book_sources = _book_sources(graph)
    node_sources = {_resolve(node.path): node.id for node in graph.nodes.values()}
    children: dict[str | None, list[str]] = {}
    for node_id in sorted(graph.nodes):
        if _resolve(graph.nodes[node_id].path) in book_sources:
            children.setdefault(graph.nodes[node_id].parent, []).append(node_id)
    precedence: dict[str, set[str]] | None = None
    ordered: list[Path] = []
    pending: list[tuple[Path, str | None]] = [(blueprint / "README.md", None)]
    while pending:
        source, parent = pending.pop()
        if parent is not None:
            ordered.append(destination / _relative(source, blueprint))
        own = children.get(parent, [])
        linked = [node_sources[path] for path in _book_links(source, blueprint, node_sources)] if source.is_file() else []
        direct: dict[str, int] = {}
        through: dict[str, int] = {}
        for index, node_id in enumerate(linked):
            child = node_id if node_id in own else _child_containing(graph, parent, node_id)
            if child in own:
                (direct if child == node_id else through).setdefault(child, index)
        placed = through | direct
        unlinked = [node_id for node_id in own if node_id not in placed]
        if len(unlinked) > 1:
            if precedence is None:
                precedence = _sibling_precedence(graph)
            unlinked = _dependency_sorted(unlinked, precedence)
        reading = [*sorted(placed, key=placed.__getitem__), *unlinked]
        pending.extend((graph.nodes[node_id].path, node_id) for node_id in reversed(reading))
    return ordered


def _containment_chain(graph: Graph, node_id: str) -> list[str]:
    """*node_id* and its containers, innermost first."""
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = node_id
    while current is not None and current in graph.nodes and current not in seen:
        chain.append(current)
        seen.add(current)
        current = graph.nodes[current].parent
    return chain


def _sibling_precedence(graph: Graph) -> dict[str, set[str]]:
    """For each node, the siblings whose subtrees must come after its own:
    sibling B follows sibling A when something in B's subtree depends on
    something in A's. Each dependency orders the two subtrees that hold its
    ends just below their nearest common container."""
    after: dict[str, set[str]] = {}
    for node in graph.nodes.values():
        dependent = _containment_chain(graph, node.id)
        for dependency_id in node.dependencies:
            if dependency_id not in graph.nodes:
                continue
            prerequisite = _containment_chain(graph, dependency_id)
            shared = set(prerequisite).intersection(dependent)
            below_dependent = next((i for i, item in enumerate(dependent) if item in shared), len(dependent))
            below_prerequisite = next((i for i, item in enumerate(prerequisite) if item in shared), len(prerequisite))
            if below_dependent and below_prerequisite:  # neither end contains the other
                after.setdefault(prerequisite[below_prerequisite - 1], set()).add(dependent[below_dependent - 1])
    return after


def _dependency_sorted(siblings: list[str], precedence: Mapping[str, set[str]]) -> list[str]:
    """*siblings* with each subtree after the subtrees it depends on, ties
    broken by their order in *siblings*. When only cycles are left, the
    earliest sibling on one of them goes next."""
    rank = {node_id: index for index, node_id in enumerate(siblings)}
    later = {node_id: sorted(rank[other] for other in precedence.get(node_id, ()) if other in rank) for node_id in siblings}
    waiting = [0] * len(siblings)
    for successors in later.values():
        for index in successors:
            waiting[index] += 1
    ready = [index for index, count in enumerate(waiting) if not count]
    heapq.heapify(ready)
    done: set[int] = set()

    def on_cycle(start: int) -> bool:
        stack, seen = list(later[siblings[start]]), set()
        while stack:
            index = stack.pop()
            if index == start:
                return True
            if index not in done and index not in seen:
                seen.add(index)
                stack.extend(later[siblings[index]])
        return False

    ordered: list[str] = []
    while len(ordered) < len(siblings):
        if not ready:  # every sibling left waits on one, so some sit on a cycle
            heapq.heappush(ready, next(i for i in range(len(siblings)) if i not in done and on_cycle(i)))
        index = heapq.heappop(ready)
        if index in done:
            continue
        done.add(index)
        ordered.append(siblings[index])
        for successor in later[siblings[index]]:
            waiting[successor] -= 1
            if not waiting[successor] and successor not in done:
                heapq.heappush(ready, successor)
    return ordered


def _child_containing(graph: Graph, parent: str | None, node_id: str) -> str | None:
    """The child of *parent* whose containment subtree holds *node_id*, if any."""
    seen: set[str] = set()
    while node_id in graph.nodes and node_id not in seen:
        seen.add(node_id)
        up = graph.nodes[node_id].parent
        if up == parent:
            return node_id
        if up is None:
            return None
        node_id = up
    return None


def _reading_order(
    graph: Graph,
    targets: Mapping[str, tuple[Path, str]],
    pages: list[Path],
    positions: Mapping[str, int],
) -> list[str]:
    """Every node in the order the book reads it: by its page's place in
    ``pages``, then by its card's place on that page (``positions``; a page's
    own article comes first), then in dependency order. Nodes on a page not
    in ``pages`` follow, one page at a time."""
    page_rank = {page: rank for rank, page in enumerate(pages)}

    def key(node_id: str) -> tuple[int, str, int]:
        page = _resolve(targets[node_id][0]) if node_id in targets else None
        return page_rank.get(page, len(page_rank)), str(page or ""), positions.get(node_id, -1)

    return sorted(status.topological_order(graph), key=key)


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


def _render_structure_page(
    *,
    page: Path,
    entry: Path | None = None,
) -> str:
    """Keep the old structure route as a bounded doorway into the book.

    The explorer owns hierarchy now. Republishing every source path as one raw
    HTML tree made this compatibility page larger than the rest of a small
    site, duplicated the explorer, and became unusable on repository-scale books.
    """
    target = entry or page.parent / "dependencies.md"
    explorer = relative_link(target, page, ".md")
    text = target.read_text(encoding="utf-8") if target.is_file() else ""
    # A header title is visible text already, while an H1 is Markdown, as in
    # the site's nav.
    named = _header_title(text)
    title = name_text(named) if named else title_text(_first_h1(text) or "")
    return "\n".join(
        [
            "---",
            "title: This page moved",
            "search:",
            "  exclude: true",
            "hide:",
            "  - toc",
            "---",
            "",
            "# This page moved",
            "",
            f"The book starts at [{title or 'its contents'}]({explorer}).",
            "",
        ]
    )


def _render_summary_nav(
    book_pages: list[Path],
    *,
    destination: Path,
    overview: Path,
    source_pages: Sequence[Path] = (),
) -> str:
    """Write one mathematical site nav with the book's real chapters.

    mkdocs.yml cannot know a project's chapters, and hand-listing them would be
    the second navigation manifest the roadmap skill forbids. mkdocs-literate-nav
    reads this file instead, so the tabs come from the same page order the book
    itself uses.

    A chapter's README nests under the nearest README above its folder, any
    other page under the nearest README in its own folder or above, and the
    tree is written depth first in book order. The book order walks the
    containment tree (:func:`_reading_walk`), so a page always comes after
    its chapter. Nesting by folder rather than by depth alone keeps each
    level one step below its parent, which literate-nav requires. The source
    notes, *source_pages*, follow the book in a section of their own.
    """
    # SUMMARY.md is a navigation manifest, not a searchable content page. A
    # repository-wide book can make this one generated page larger than every
    # article, so exclude it before Material constructs its transient index.
    lines = [
        "---",
        "search:",
        "  exclude: true",
        "---",
        "",
        f"- [Home]({overview.relative_to(destination).as_posix()})",
        "- Mathematics",
    ]
    pages = [page for page in book_pages if page != overview]
    folder_pages = {page.parent: page for page in pages if page.name.casefold() == "readme.md"}
    children: dict[Path | None, list[Path]] = {}
    for page in pages:
        folders = page.parent.parents if folder_pages.get(page.parent) == page else [page.parent, *page.parent.parents]
        parent = next((folder_pages[folder] for folder in folders if folder in folder_pages), None)
        children.setdefault(parent, []).append(page)
    stack = [(page, 1) for page in reversed(children.get(None, []))]
    while stack:
        page, depth = stack.pop()
        text = page.read_text(encoding="utf-8")
        named = _header_title(text)
        title = name_text(named) if named else title_text(_first_h1(text) or page.parent.name)
        lines.append(f"{'    ' * depth}- [{title}]({_relative(page, destination).as_posix()})")
        stack.extend((child, depth + 1) for child in reversed(children.get(page, [])))
    # What counts as done belongs beside the book it qualifies. The landing
    # page used to be the only route to it, which made the page impossible to
    # simplify without stranding it.
    # Last inside the book: navigation.indexes takes a section's first README
    # page as the page its heading opens, which made Mathematics open the
    # coverage page, and a section of its own showed an empty pane on a narrow
    # screen.
    depth = 2 if children.get(None) else 1
    coverage = destination / "coverage/README.md"
    if coverage.is_file():
        # Named by its title, as a chapter is; "Coverage" when only its file names it.
        named = _header_title(coverage.read_text(encoding="utf-8"))
        title = name_text(named) if named and named != _file_title(coverage) else "Coverage"
        lines.append(f"{'    ' * depth}- [{title}](coverage/README.md)")
    lines += _source_nav([page for page in source_pages if page.is_file()], destination=destination, depth=depth)
    return "\n".join(lines) + "\n"


def _source_nav(notes: list[Path], *, destination: Path, depth: int) -> list[str]:
    """The nav entries for the published source *notes*, at *depth*: the
    folder's README heading the rest, else a section of their own, named as
    the coverage entry is. A subfolder's README heads its folder's notes, as a
    chapter's README heads its pages."""

    if not notes:
        return []
    folder = destination / SOURCES_DIR
    heads = {page.parent: page for page in notes if page.name.casefold() == "readme.md"}
    top = heads.get(folder)
    children: dict[Path | None, list[Path]] = {}
    for page in sorted(notes):
        if page == top:
            continue
        start = page.parent.parent if heads.get(page.parent) == page else page.parent
        above = [start, *(parent for parent in start.parents if _is_within(parent, folder))]
        parent = next((heads[path] for path in above if path in heads and heads[path] != page), None)
        children.setdefault(None if parent == top else parent, []).append(page)

    def title(page: Path) -> str:
        text = page.read_text(encoding="utf-8")
        named = _header_title(text)
        if page.name.casefold() == "readme.md" and named in (None, _file_title(page)):
            # Named by its folder when only its file names it, as Coverage is "Coverage".
            return name_text(page.parent.name)
        return name_text(named) if named else title_text(_first_h1(text) or page.stem)

    def href(page: Path) -> str:
        return _bracketed(_relative(page, destination).as_posix())

    named = _header_title(top.read_text(encoding="utf-8")) if top else None
    label = name_text(named) if top and named and named != _file_title(top) else "Sources"
    lines = [f"{'    ' * depth}- [{label}]({href(top)})" if top else f"{'    ' * depth}- {label}"]
    stack = [(page, depth + 1) for page in reversed(children.get(None, []))]
    while stack:
        page, level = stack.pop()
        entry = f"[{title(page)}]({href(page)})"
        # MkDocs takes a page named README or index for its section's own page,
        # and navigation.indexes makes a heading with no page open the first
        # such child, so one directly under it gets a heading of its own.
        if top is None and level == depth + 1 and page.stem in {"README", "index"} and not children.get(page):
            lines += [f"{'    ' * level}- {_bare_title(title(page))}", f"{'    ' * (level + 1)}- {entry}"]
            continue
        lines.append(f"{'    ' * level}- {entry}")
        stack.extend((child, level + 1) for child in reversed(children.get(page, [])))
    return lines


def _bare_title(title: str) -> str:
    """*title*, written as :func:`~autoform_cli.markdown.name_text` writes it,
    as the title of a nav section with no page. literate-nav reads the line as
    a Markdown list item, so a marker it starts with that would open a list or
    a heading, as in ``1. Background``, is written as a reference too."""

    return re.sub(r"^(\d*)([.+#-])", lambda m: m[0] if m[0] == "." else f"{m[1]}&#{ord(m[2])};", title)


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
# Presentation starts with work expanded into the roadmap, then follows the
# parser's canonical order. Deriving this tuple keeps newly added dispositions
# visible instead of silently dropping them from the landing page.
_COVERAGE_SUMMARY_ORDER = tuple(
    sorted(COVERAGE_DISPOSITIONS, key=lambda disposition: disposition != "DECOMPOSED")
)


def _completion_percentage(done: int, total: int) -> int:
    """Round progress while reserving both endpoints for the exact endpoints."""

    if not total or not done:
        return 0
    if done == total:
        return 100
    return max(1, min(99, round(100 * done / total)))


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


def _anchored_links(
    targets: dict[str, tuple[Path, str]],
    page: Path,
    *,
    extension: str = ".html",
) -> Mapping[str, str]:
    """Link every node to its statement on the published chapter page.

    Use ``.md`` for links MkDocs will parse -- it validates and rewrites those
    itself -- and ``.html`` for raw HTML, which it never sees. A
    statement on the current page is just a fragment. Read only the nodes the
    page shows: links are built when read, and iterating or copying the mapping
    walks the whole graph on every page.
    """
    return _PageLinks(targets, page, extension)


def _node_links(targets: dict[str, tuple[Path, str]], page: Path, node_ids: Iterable[str]) -> dict[str, str]:
    """Link *node_ids* from *page*, as a mapping an explorer page may copy.

    Copying a page's whole :func:`_anchored_links` walks the graph, so doing
    it on every page made render quadratic.
    """
    links = _anchored_links(targets, page)
    return {node_id: links[node_id] for node_id in node_ids}


class _PageLinks(Mapping[str, str]):
    """The links from one page to every node, each built when read.

    A page names a few nodes but *targets* holds the whole graph, and a site
    has a page per node, so building every link up front made rendering
    quadratic in the size of the graph.
    """

    def __init__(self, targets: dict[str, tuple[Path, str]], page: Path, extension: str) -> None:
        self._targets = targets
        self._page = page
        self._resolved_page: Path | None = None
        self._extension = extension
        self._hrefs: dict[Path, str] = {}

    def __getitem__(self, node_id: str) -> str:
        target, anchor = self._targets[node_id]
        # Many nodes share a chapter page, and resolving a path walks the disk,
        # so each target page is linked once. The current page is cached as ""
        # and links as a bare fragment.
        href = self._hrefs.get(target)
        if href is None:
            if self._resolved_page is None:
                self._resolved_page = _resolve(self._page)
            if _resolve(target) == self._resolved_page:
                href = ""
            else:
                href = relative_link(target, self._page, self._extension)
                if self._extension == ".html":
                    href = _as_published(href)
            self._hrefs[target] = href
        return f"{href}#{anchor}" if anchor else href or "#"

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._targets

    def __iter__(self) -> Iterator[str]:
        return iter(self._targets)

    def __len__(self) -> int:
        return len(self._targets)


def _link_mask(line: str, *, quoted: bool = False) -> str:
    """*line*, or a block's lines joined, with its code spans, inline math and bracket escapes blanked.

    The site reads code and math before links and an escaped bracket as text,
    as check does. Blanking keeps every offset, so a link matched in the mask
    reads its label and target back from *line*. With *quoted*, each line's
    quote markers are blanked too, as the site reads a quote's lines without
    them, so a destination on a later line of a quote is found.
    """
    masked = _BRACKET_ESCAPE.sub(_blank, _code_and_math_mask(line))
    if not quoted:
        return masked
    pieces = []
    for piece in masked.split("\n"):
        content = _unquoted(piece)[0]
        pieces.append(" " * (len(piece) - len(content)) + content)
    return "\n".join(pieces)


def _code_and_math_mask(line: str) -> str:
    """*line*, or a block's lines joined, with what the site reads before links blanked: code spans and inline math."""
    return _INLINE_MATH.sub(_blank, _CODE_SPAN.sub(_blank_code, line))


def _inline_destination(line: str, match: re.Match[str]) -> str:
    """The destination of *match*, an inline link or image in *line*, as the site reads it.

    Python-Markdown drops the backslash before a character it escapes, so
    ``base\\_lemma.md`` names ``base_lemma.md``, but not in code or math, which
    it reads first. A reference definition's destination keeps its backslashes
    on the site, so it is read as written.
    """
    start, end = match.span("target")
    escaped = _body_converter().ESCAPED_CHARS
    masked = _code_and_math_mask(line)[start:end]
    dropped = {escape.start() for escape in re.finditer(r"\\(.)", masked) if escape.group(1) in escaped}
    return "".join(char for index, char in enumerate(line[start:end]) if index not in dropped)


class _RepositoryImage(str):
    """An image's destination, among the targets :func:`_rewrite_links` drops,
    that the site links in the repository rather than copying, such as a scan
    under ``sources/`` once render verified the commit."""


class _MissingSource(str):
    """A destination under ``sources/``, among the targets :func:`_rewrite_links`
    drops, that the verified commit does not hold, such as a scan kept out of git."""


class _MissingSourceImage(_MissingSource):
    """The same, as an image's destination."""


def _rewrite_links(
    text: str,
    *,
    source_dir: Path,
    page: Path,
    blueprint: Path,
    destination: Path,
    node_sources: dict[Path, str],
    targets: dict[str, tuple[Path, str]],
    sources_base: "_SourceBase | None" = None,
    left_out: frozenset[Path] = frozenset(),
    unpublished: tuple[Path, ...] = (),
    dropped: list[str] | None = None,
    links: Mapping[str, str] | None = None,
    resolved: dict[str, Path] | None = None,
) -> str:
    """Resolve a page's relative links against where it is being published.

    Three things move. Node files stop being pages once their chapter absorbs
    them, so a link naming one becomes an anchor. A node's body is hoisted out
    of its own directory onto the chapter page, so its remaining relative links
    have to be recomputed from there. And what the site does not publish from
    ``sources/`` (a hidden note, the folder itself, scans and data) is reached
    in the repository when *sources_base* says where; an image of one keeps
    only its text, since a page cannot show a file from there.

    The site holds only the blueprint, so a link or image to a file outside it,
    such as a source note's evidence link to a Lean file, keeps its text and
    loses its target instead of dangling.

    A caller rewriting several texts from one capture may share work between
    them: *links*, which must be :func:`_anchored_links` for this *page*, and
    *resolved*, each path written so far with what it resolved to.
    """
    # A coverage page can name most of the graph, so one mapping serves the
    # whole text and links each target page it reaches once.
    if links is None:
        links = _anchored_links(targets, page, extension=".md")
    if resolved is None:
        resolved = {}
    # A reference image takes its destination from a definition anywhere on the page.
    references = _page_definitions(text)

    def moved_target(raw: str, *, image: bool = False) -> str | None:
        """Where *raw* should point once published, None to leave it alone, or ""
        to keep only the text of each link and image naming it, and drop its definition."""
        bare = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        # The query and fragment are not part of the file's name: they are
        # kept as written, beside the path render moves.
        located, separator, fragment = bare.partition("#")
        path, asked, query = located.partition("?")
        try:
            if not path or urlsplit(path).scheme or path.startswith("/"):
                return None
            written = source_dir / unquote(path)
            # Keyed by the text written: Windows compares paths without
            # regard to case, and a missing file resolves in the case written.
            key = str(written)
            candidate = resolved.get(key)
            if candidate is None:
                candidate = resolved[key] = written.resolve()
        except ValueError:
            # An unbalanced IPv6 bracket or a decoded NUL names no file to
            # point at, so the link is published as written, like a URL.
            return None
        # Capture leaves out hidden paths and unpublished folders, so the site has
        # no page there. A hidden source note is reached through the repository instead.
        try:
            relative = _relative(candidate, blueprint)
        except ValueError:
            relative = None
        if relative is not None and (
            any(_is_within(candidate, folder) for folder in unpublished)
            or (_is_hidden(relative) and relative.parts[:1] != (SOURCES_DIR,))
        ):
            if dropped is not None:
                dropped.append(raw)
            return ""
        node_id = node_sources.get(candidate)
        if node_id is not None:
            href = links[node_id]
            if not targets[node_id][1] and separator:
                href = f"{'' if href == '#' else href}#{fragment}"
            before, hashed, anchor = href.partition("#")  # the query goes before the anchor
            return f"{before}{asked}{query}{hashed}{anchor}"
        if relative is None:
            # Nothing outside the blueprint is published: "" keeps only the text,
            # and its image or definition goes too.
            return ""
        # A file the verified commit lacks under sources/, such as a scan kept
        # out of git, has no repository link and no copy on the site, so its link
        # keeps only its text, with a warning, rather than failing a strict build.
        if (
            sources_base is not None
            and relative.parts[:1] == (SOURCES_DIR,)
            and not sources_base.holds(relative, candidate)
        ):
            if dropped is not None:
                dropped.append(_MissingSourceImage(raw) if image else _MissingSource(raw))
            return ""
        # A note and its images are pages and files of the site; the repository
        # reaches the rest of sources/, a file of a type the site leaves out, and
        # one the commit holds that the checkout lacks.
        if (
            sources_base is not None
            and relative.parts[:1] == (SOURCES_DIR,)
            and (
                _is_hidden(relative)
                or not os.path.exists(candidate)
                or os.path.isdir(candidate)
                or candidate in left_out
                or candidate.suffix.lower() not in _PUBLISHED_SOURCE_SUFFIXES
            )
        ):
            if image:
                # A page cannot show a file from the repository, so its image keeps its text.
                if dropped is not None:
                    dropped.append(_RepositoryImage(raw))
                return ""
            return f"{_source_href(sources_base, relative.parts[1:])}{asked}{query}{separator}{fragment}"
        if candidate in left_out:
            # Nor is a file the site leaves out; its image or definition goes too.
            return ""
        # atlas.json stays off the site, so a link to it keeps only its text, and its definition goes too.
        if relative.as_posix().casefold() == "atlas.json":
            return ""
        published = destination / relative
        # relative_link percent-encodes for raw HTML and explorer payloads.
        # This is Markdown: keep its decoded spelling so an
        # unchanged link stays byte-for-byte unchanged, and ``bracketed`` can
        # protect a moved path containing a space or parenthesis.
        href = unquote(relative_link(published, page, candidate.suffix))
        # Decoded, a first segment that holds a colon, such as a folder named
        # ``javascript:x``, would read as the URL's scheme.
        if ":" in href.split("/", 1)[0]:
            href = f"./{href}"
        return f"{href}{asked}{query}{separator}{fragment}"

    def rewrite(line: str) -> str:
        # A link, an image and a definition each contain a bracket, which
        # masking never adds, so a line without one has nothing to move. A
        # block the site reads as a display formula holds TeX, whatever its
        # brackets look like.
        if "[" not in line or _display_formula(line.split("\n")):
            return line
        # A definition whose target starts on the line after its label moves
        # that target where it stands; one that goes leaves each of its lines
        # their quote markers, so a quote still holds the lines after it.
        lines = line.split("\n")
        if (later := _later_target(lines)) is not None:
            definition, start, stop = later
            href = moved_target(definition.group(2))
            span = definition.group(0).count("\n") + 1
            if href == "":
                first = lines[0].expandtabs(4)
                marker = first[: container.end()] if (container := _DEFINITION_CONTAINER.match(first)) else ""
                kept = [re.sub(r"-(?= *$)", "*", marker)] + [
                    piece[: len(piece) - len(_unquoted(piece)[0])].rstrip() for piece in lines[1:span]
                ]
            elif href:
                href = re.sub(r"\s", lambda space: quote(space.group()), href)
                kept = [lines[0], lines[1][:start] + href + lines[1][stop:], *lines[2:span]]
            else:
                kept = lines[:span]
            return "\n".join([*kept, *([rewrite("\n".join(lines[span:]))] if lines[span:] else [])])
        # A definition whose title is on the next line is one block, and is
        # rewritten a line at a time, as before; one that goes takes its title.
        if "\n" in line and _LINK_DEFINITION.match(line.partition("\n")[0]):
            first, *titles = map(rewrite, line.split("\n"))
            return "\n".join([first, *titles]) if first else "\n" * line.count("\n")
        # The site reads a definition after quote and list markers too, in any
        # order once tabs are expanded, and the lines an item carries on with
        # as one paragraph; one it drops leaves the quote or item empty.
        first, newline, rest = line.partition("\n")
        expanded = first.expandtabs(4)
        container = _DEFINITION_CONTAINER.match(expanded)
        if container is not None and _LINK_DEFINITION.match(inner := expanded[container.end() :]):
            if (written := rewrite(inner)) != inner:
                marker = expanded[: container.end()]
                # A bare `-` on a list's later line would underline the line
                # before it as a heading; `*` keeps the item in the same list.
                first = f"{marker if written else re.sub(r'-(?= *$)', '*', marker)}{written}"
            return f"{first}{newline}{rewrite(rest)}"
        # A reference definition carries the destination for every `[x][label]`
        # on the page. Rewriting only inline links leaves it pointing at the
        # unpublished path and the rendered link dangles.
        definition = _LINK_DEFINITION.match(line)
        # Lines read together are one definition only where the site reads one.
        if definition is not None and "\n" in line and not _SITE_LINK_DEFINITION.match(line):
            definition = None
        if definition is not None:
            href = moved_target(definition.group("target"))
            if href == "":
                # A label over two lines keeps both, so every later line stays where it was written.
                return "\n" * line.count("\n")
            if href:
                indent, label = definition.group("indent"), definition.group("label")
                # Python-Markdown reads a definition's destination only up to
                # whitespace, in angle brackets or not, so whitespace is encoded.
                href = re.sub(r"\s", lambda space: quote(space.group()), href)
                return f"{indent}[{label}]: {href}{definition.group('rest') or ''}"
            return line
        # An image of a file the site leaves out keeps only its alternative text.
        pieces: list[str] = []
        end = 0
        for match in _MARKDOWN_IMAGE.finditer(_link_mask(line, quoted=True)):
            raw = _inline_destination(line, match)
            if moved_target(raw, image=True) == "":
                pieces += [line[end : match.start()], _kept_text(line[match.start("label") : match.end("label")])]
                end = match.end()
        line = "".join(pieces) + line[end:]
        # So does a reference image of a file a page cannot show, or of one whose
        # definition goes. A definition the site links in the repository stays,
        # for a link that uses it.
        for start, text_start, text_end, stop, raw in reversed(reference_images(line, references) if references else []):
            if moved_target(raw, image=True) == "":
                line = line[:start] + _kept_text(line[text_start:text_end]) + line[stop:]
        # A reference link whose definition goes keeps only its text, as an
        # inline link does, escaped as an image's text is.
        for start, text_start, text_end, stop, raw in reversed(reference_links(line, references) if references else []):
            if moved_target(raw) == "":
                line = line[:start] + _kept_text(line[text_start:text_end]) + line[stop:]
        pieces = []
        end = 0
        for match in _MARKDOWN_LINK.finditer(_link_mask(line, quoted=True)):
            raw = _inline_destination(line, match)
            href = moved_target(raw)
            if href is None or raw in (href, f"<{href}>"):  # nothing moved: keep the link as written
                continue
            label = line[match.start("label") : match.end("label")]
            # A destination on a later line keeps the break and quote markers before it, and a title on a
            # later line goes but leaves its breaks, so every later line of a moved link stays where it was
            # written. A link cut down to its label does not, as a break there could end the paragraph.
            lead = line[match.end("label") + 2 : match.start("target")]
            lead = lead if "\n" in lead else ""
            breaks = "\n" * line.count("\n", match.end("target"), match.end())
            pieces += [line[end : match.start()], f"[{label}]({lead}{_bracketed(href)}{breaks})" if href else label]
            end = match.end()
        return "".join(pieces) + line[end:]

    return _outside_fences(text, rewrite, page=True, joined=True)


def _page_definitions(text: str) -> dict[str, str]:
    """The reference definitions the site reads in *text* once
    :func:`_rewrite_links` has rewritten it, by label, each with its target as
    written: one the site reads as written, in a quote too, and one the
    rewrite makes readable, such as a target in angle brackets that holds a
    space. A later definition of a label replaces an earlier one, as on the
    site."""

    # The lines the rewrite reads as definitions, found in the blocks it is
    # given as it finds them, each with the definition it reads there, after
    # any quote or list marker once tabs are expanded.
    rewritten: dict[str, str] = {}

    def collect(line: str) -> str:
        first, newline, rest = line.partition("\n")
        expanded = first.expandtabs(4)
        container = _DEFINITION_CONTAINER.match(expanded)
        if newline and _LINK_DEFINITION.match(first):
            for piece in line.split("\n"):
                collect(piece)
        elif container is not None and _LINK_DEFINITION.match(expanded[container.end() :]):
            rewritten[first] = expanded[container.end() :]
            collect(rest)
        elif _LINK_DEFINITION.match(line) and (not newline or _SITE_LINK_DEFINITION.match(line)):
            rewritten[line] = line
        return line

    _outside_fences(text, collect, page=True, joined=True)
    definitions: dict[str, str] = {}
    lines = _content_lines(text)
    # A label alone starts a definition only where a block does: in a paragraph, a list marker before it is
    # text, and a definition above that took it for its target ends its block below it.
    starts = {first for first, _ in _text_blocks(lines)}
    for index, line in enumerate(lines):
        # The site expands tabs before it reads a definition.
        read = _SITE_LINK_DEFINITION.match(_unquoted(line)[0].expandtabs(4))
        if read is None and index in starts and (later := _later_target(lines[index : index + 3])) is not None:
            read = later[0]
        if read is not None:
            definitions[read.group(1).strip().lower()] = read.group(2)
        elif (inner := rewritten.get(line)) is not None and (written := _LINK_DEFINITION.match(inner)):
            definitions[written.group("label").strip().lower()] = written.group("target")
    return definitions


def _later_target(lines: list[str]) -> tuple[re.Match[str], int, int] | None:
    """The reference definition the site reads where the first of *lines*
    holds only its label, after any quote or list markers, and its target
    starts on the second, with where that target stands in the second line;
    None for any other lines."""

    if len(lines) < 2:
        return None
    first = lines[0].expandtabs(4)
    container = _DEFINITION_CONTAINER.match(first)
    label = first[container.end() :] if container else first
    if not _LABEL_ALONE.match(label):
        return None
    definition = _definition_at([label, *lines[1:3]], (container.group(0) if container else "").count(">"))
    if definition is None:
        return None
    content = _unquoted(lines[1])[0]
    start = len(lines[1]) - len(content.lstrip(" \t"))
    return definition, start, start + len(definition.group(2))


def _kept_text(text: str) -> str:
    """*text*, an image's alternative text or a reference link's, kept where
    the image or link stood. Where it starts a line, the site would read a
    leading ``#``, ``>``, list marker or rule as block markup, so that
    character is escaped."""

    marker = _KEPT_TEXT_MARKER.match(text)
    return f"{text[: marker.start('char')]}\\{text[marker.start('char') :]}" if marker else text


def _bracketed(href: str) -> str:
    """*href* as a Markdown link's destination. A decoded path, or a query or
    fragment kept from angle brackets, can hold a space, a parenthesis or a
    quote. CommonMark allows no space in a bare destination, Python-Markdown
    needs its parentheses balanced, and it reads a quote as opening a title."""

    return f"<{href}>" if re.search(r"[\s()'\"]", href) else href


def _in_working_tree(path: Path, root: Path) -> bool:
    """Whether the repository at *root* can track *path*: below it, and not in a checkout nested there."""
    if not _is_within(path, root):
        return False
    while path != root:
        if (path / ".git").exists():  # a submodule or a separate clone
            return False
        path = path.parent
    return True


def _reaches(path: Path, directory: Path) -> bool:
    """Whether *path* lies inside *directory* under another spelling.

    A case- or normalization-insensitive filesystem opens ``BLUEPRINT/site``
    inside ``blueprint`` although the two paths differ, so compare identities.
    """
    try:
        target = os.stat(directory)
    except OSError:
        return False
    for candidate in (path, *path.parents):
        try:
            metadata = os.stat(candidate)
        except OSError:
            continue
        if (metadata.st_dev, metadata.st_ino) == (target.st_dev, target.st_ino):
            return True
    return False


def _formalizing(graph: Graph) -> dict[str, str]:
    """The statement each ``lean:`` name formalizes: the first that names it, a catalog only if none else does."""
    found: dict[str, str] = {}
    for node in sorted(graph.nodes.values(), key=lambda node: node.catalog is not None):
        for name in declaration_names(node.lean or ""):
            found.setdefault(name, node.id)
    return found


def _reverse_edges(graph: Graph) -> dict[str, list[str]]:
    used_by: dict[str, list[str]] = {node_id: [] for node_id in graph.nodes}
    for node in graph.nodes.values():
        for dependency in node.dependencies:
            used_by[dependency].append(node.id)
    return {key: sorted(value) for key, value in used_by.items()}


def _reverse_informal_edges(graph: Graph) -> dict[str, list[str]]:
    """What informally uses each node: its ``## Informally uses`` links, read from the other end."""
    informally_used_by: dict[str, list[str]] = {node_id: [] for node_id in graph.nodes}
    for node in graph.nodes.values():
        for target in node.informal_dependencies:
            informally_used_by[target].append(node.id)
    return {key: sorted(value) for key, value in informally_used_by.items()}


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


def _left_out(
    cited: PurePosixPath,
    written_over: Collection[str],
    written_pages: Collection[str] = (),
    *,
    sources_linked: bool = False,
    kept_over: Collection[str] = (),
) -> bool:
    """Whether the site leaves out the captured file at ``cited``, a path in
    the blueprint as captured, as :func:`_publication` does: a file under
    ``readbacks/`` or ``links/``, the atlas, a file render writes its own over (one of
    ``written_over``, the :func:`_site_paths`), a page at the URL of one of
    the ``written_pages``, such as ``dependencies/full.markdown``, a page left
    out for another page at its name (one of ``kept_over``), or one that
    is neither a page nor a file the site copies. With ``sources_linked``,
    every file under ``sources/`` counts as published, on the site or at the
    verified commit."""

    if sources_linked and cited.parts[:1] == (SOURCES_DIR,):
        return False
    return (
        cited.parts[:1] == (READBACKS_DIR,)
        or cited.parts[0].casefold() == LINKS_DIR
        or cited.as_posix().casefold() == "atlas.json"
        or cited.as_posix() in written_over
        or _page_url(cited.as_posix()) in written_pages
        or cited.as_posix() in kept_over
        or (cited.suffix not in PAGE_SUFFIXES and cited.suffix.lower() not in _STATIC_SUFFIXES)
    )


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


__all__ = [
    "DAG_SCRIPT",
    "LOGO",
    "MERMAID_SCRIPT",
    "PUBLICATION_MANIFEST",
    "STYLESHEET",
    "RenderReport",
    "render_site",
    "publication_source_revision",
]
