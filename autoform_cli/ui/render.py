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
import html
import json
import os
import re
import secrets
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from .. import status
from .._fs import is_within as _is_within
from .._fs import relative as _relative
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
    CoverageSummary,
    load_coverage,
    validate_coverage_roles,
)
from ..graph import (
    SOURCES_DIR,
    Graph,
    GraphValidationError,
    PublicationError,
    _containers,
    atlas_omissions,
    load_graph,
    pinned_list_omissions,
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
from ..markdown import (
    PAGE_SUFFIXES,
    blueprint_root,
    directory_path,
    published_page,
)
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
from . import _page_pool, dag_viewer, graph_pages, graph_views, mathjax, site_index
from .book_order import _book_page_order, _book_sources, _containment_chain, _reading_order, _reading_walk, _written
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
from .chapters import _card_positions, _render_chapter
from .coverage_view import _render_coverage_page
from .integrated_page import _first_paint, _integrated_page, _overview_routes, _reader_marks, _source_scopes
from .landing import _render_landing_page
from .markdown_html import (
    _ONE_LINE,
    _as_published,
    _protect_narrative_markdown,
    _typed,
)
from .mathjax import CARD_CLASS, CARD_FORMULA_CLASS, MATHJAX_SCRIPT
from .page_links import (
    _anchored_links,
    _node_links,
    _SourceBase,
    _sources_base,
)
from .site_files import (
    _GENERATED_FILES,
    COVERAGE_PAGE,
    DAG_SCRIPT,
    DAG_STYLE,
    LOGO,
    MERMAID_SCRIPT,
    STRUCTURE_PAGE,
    STYLESHEET,
    _folded,
    _group_nodes,
    _group_page,
    _in_the_way,
    _logo,
    _map_assets,
    _mermaid_script,
    _site_paths,
    _stylesheet,
    _written_pages,
)
from .site_output import RenderReport, _destination_state, _publish, _recover_backup
from .statement_box import _render_rows
from .summary_nav import _render_structure_page, _render_summary_nav
from .titles import (
    _label_html,
    _titled,
)
from .vault_pages import _is_utf8, _left_out, _Publication, _publication

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
