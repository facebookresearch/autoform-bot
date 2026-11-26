"""Which of the vault's own files the site publishes: its pages and other files, the warnings about them, and
what it leaves out."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .._fs import is_within as _is_within
from .._fs import relative as _relative
from .._fs import resolve as _resolve
from ..annotations import LINKS_DIR
from ..graph import SOURCES_DIR, Graph, _containers, unpublished_setting_issues
from ..library.pinned_names import FOLDER as PINNED_NAMES_FOLDER
from ..markdown import (
    PAGE_HEADER,
    PAGE_SUFFIXES,
    published_page,
    quoted_destinations,
    statement_and_notes,
    statement_code,
)
from ..readback import READBACKS_DIR
from ..tex_macros import TEX_MACROS
from . import _page_pool, mathjax
from .chapters import _chapter_layout, _chapter_stretches
from .lean_blocks import _lean_fences_as_blocks
from .markdown_html import _LEAN_FENCE_BLOCK, _protect_narrative_markdown
from .mathjax import MATHJAX_SCRIPT
from .page_links import (
    _anchored_links,
    _MissingSource,
    _MissingSourceImage,
    _RepositoryImage,
    _rewrite_links,
    _source_href,
    _SourceBase,
)
from .publishable import publishable_article, shown_as_typed
from .site_files import (
    _IMAGE_SUFFIXES,
    _STATIC_SUFFIXES,
    COVERAGE_PAGE,
    _anchor,
    _folded,
    _group_nodes,
    _group_page,
    _page_url,
    _site_paths,
    _written_pages,
)


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
    coded: list[str] = []
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
                # Code in a statement is Lean or notes the reader gets in place of mathematics (#598). A Lean fence
                # is render's own block by now, so it is found as one.
                if not found and node.catalog is None:
                    code = "a code block" if _LEAN_FENCE_BLOCK.search(parts[0]) else statement_code(parts[0])
                    if code:
                        coded.append(f"{node_id} ({code})")
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
    if coded:
        shown = ", ".join(coded[:3]) + (f" and {len(coded) - 3} more" if len(coded) > 3 else "")
        warnings.append(
            f"statements the site shows as code ({len(coded)}): {shown}; write a statement in English and TeX, and "
            "keep Lean names in the article's facts and implementation note"
        )

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
