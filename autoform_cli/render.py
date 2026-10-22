"""Build the published blueprint from the Markdown vault.

The vault stays the source of truth. This module writes a *derived* copy in
which every node page is wrapped in a numbered statement box carrying its
derived status, a link to the Lean code that discharges it, and its place in
the DAG -- the presentation ``leanblueprint`` gives a LaTeX blueprint, driven
from Markdown instead.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from markdown.treeprocessors import Treeprocessor

from . import dag_viewer, graph_pages, graph_views, mermaid, status
from ._tree_snapshot import (
    TreeChangedError,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
    _read_prefix,
    _update_digest,
    bind_directory_tree,
    descriptor_capture_supported,
)
from .coverage import (
    COVERAGE_DISPOSITIONS,
    CoverageSummary,
    load_coverage,
    validate_coverage_roles,
)
from .graph import (
    Graph,
    GraphValidationError,
    Node,
    _implementation_note_issues,
    atlas_omissions,
    load_graph,
    unpublished_content,
    unpublished_node_ids,
)
from .graph_views import _containers, _countable, _is_countable, _module_inventories
from .lean import (
    _REPLACEABLE_SCHEMAS,
    PUBLICATION_MANIFEST,
    PUBLICATION_MANIFEST_MAX_BYTES,
    PUBLICATION_SCHEMA,
    IndexedSourceSnapshot,
    LeanSourceError,
    SourceIndex,
    SourceLinker,
    declaration_names,
    detect_ref,
    detect_repository_url,
    index_failure_message,
    repository_top_level,
    resolve_lean_root,
    snapshot_project_sources,
    verify_repository_snapshot,
)
from .markdown import (
    _BRACKET_ESCAPE,
    _INLINE_MATH,
    GENERATED_VIEWS,
    INLINE_CODE,
    NOTES_BOX,
    PAGE_HEADER,
    PAGE_SUFFIXES,
    RETIRED_VIEWS,
    STATEMENT_BOX,
    _blank,
    blueprint_root,
    boxed,
    converted_alone,
    directory_path,
    mask_fences_and_comments,
    header_title,
    name_text,
    page_title,
    published_page,
    rendered_visible_text,
    site_converter,
    statement_and_notes,
    title_text,
    yaml_string,
)
from .markdown import content_lines as _content_lines
from .markdown import outside_fences as _outside_fences
from .mathjax import MATHJAX_SCRIPT, TEX_MACROS, mathjax_script
from .publishable import publishable_article, shown_as_typed
from .readback import READBACKS_DIR
from .status import is_definition

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
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
#: inline links leaves the destination behind and publishes a dead link.
_LINK_DEFINITION = re.compile(
    r'^(?P<indent>[ ]{0,3})\[(?P<label>[^\]]+)\]:[ \t]*'
    r'(?P<target><[^>\r\n]+>|[^\s]+)(?P<rest>[ \t]+.*)?$'
)
_ARTICLE_SLOT = re.compile(
    rf"^(?P<indent>[ \t]*)[-*+]\s+\[(?=[^\]]){_LINK_LABEL}\]"
    r"\(\s*(?P<target>[^)\s]+)(?:\s+[^)]*)?\)\s*$"
)
_SKIPPED_DIRECTORIES = frozenset({".obsidian", ".trash", ".git"})


#: Transcriptions of the paper being formalised. Vault material, not chapters.
SOURCES_DIR = "sources"
#: Hidden siblings of the output: a site being built, a replaced one, or a backup.
_STAGE_PREFIX = ".autoform-publication-"
#: How a filesystem says it lacks an operation (an atomic exchange, a directory sync).
_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP})
#: Rename refusals that strerror alone would leave puzzling.
_REFUSED_COMMITS = {
    errno.EBUSY: "the output directory is a mount point or in use",
    errno.EXDEV: "the output directory is on another filesystem than its parent "
    "(a mount point or an overlay lower layer)",
}
#: How long a render waits for another render committing into the same parent.
_PUBLICATION_LOCK_WAIT_SECONDS = 30.0
#: Derived views this command rewrites; stale copies must not leak into the site.
_GENERATED_FILES = GENERATED_VIEWS | {graph_pages.PROJECT_PAYLOAD, PUBLICATION_MANIFEST}
#: The files a vault may publish as they are, besides its Markdown pages: a
#: browser shows these, or offers to save them, and runs nothing in them.
#: Anything else may be served as the site's own markup, so it is left out.
_STATIC_SUFFIXES = frozenset(
    {
        ".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".webp",
        ".pdf",
        ".bib", ".csv", ".json", ".lean", ".tex", ".txt",
    }
)
_LOCAL_ONLY_NAMES = frozenset(
    {
        ".autoform",
        "agents_status.json",
        "backend_config.json",
        "credentials.json",
        "dispatcher.log",
        "provider_settings.json",
        "secrets.json",
        "task_queue.json",
    }
)
#: Root entries every site holds; an authored root entry differing only in case would collide.
_GENERATED_ROOT_ENTRIES = frozenset(
    {"SUMMARY.md", graph_pages.PROJECT_PAYLOAD, "dependencies.md", "structure.md", PUBLICATION_MANIFEST}
    | {"assets", "dependencies", "javascripts", "stylesheets"}
)
#: Render reads no hidden, local-only, or root generated file. Omitted entries are
#: still recorded, and .autoform is entered, so sensitive files are refused by name.
_PUBLICATION_SELECTION = TreeSelection(
    include=lambda path, _mode: not (
        _is_hidden(path) or _is_sensitive(path) or (len(path.parts) == 1 and path.name in _GENERATED_FILES)
    ),
    descend=lambda path: path.parts[0].casefold() == ".autoform"
    or not (_is_hidden(path) or _is_sensitive(path)),
)

#: How a ``declaration:`` value is announced in the statement box.
DECLARATION_LABELS = {
    "abbrev": "Abbreviation",
    "axiom": "Axiom",
    "class": "Class",
    "corollary": "Corollary",
    "def": "Definition",
    "definition": "Definition",
    "example": "Example",
    "inductive": "Inductive",
    "instance": "Instance",
    "lemma": "Lemma",
    "proposition": "Proposition",
    "structure": "Structure",
    "theorem": "Theorem",
}

STYLESHEET = "stylesheets/blueprint.css"
MERMAID_SCRIPT = "javascripts/blueprint-mermaid.js"
LIVE_SCRIPT = "javascripts/blueprint-live.js"
DAG_SCRIPT = dag_viewer.SCRIPT_PATH
DAG_STYLE = dag_viewer.STYLE_PATH
LOGO = "assets/autoform.svg"
#: The files render writes into every site, whatever the vault holds there.
_ASSETS = (STYLESHEET, MERMAID_SCRIPT, MATHJAX_SCRIPT, LIVE_SCRIPT, DAG_SCRIPT, DAG_STYLE, LOGO)
#: Paths render writes at fixed places; an authored path differing only in case would collide.
_GENERATED_PATHS = _GENERATED_ROOT_ENTRIES | set(_ASSETS)
#: The explorer's files at paths a blueprint may already use for files of its own.
_EXPLORER_PATHS = (graph_pages.PROJECT_PAYLOAD, DAG_SCRIPT, DAG_STYLE)
_ASSET_DIR = Path(__file__).resolve().parent / "assets"


def _static_asset(name: str) -> str:
    try:
        return (_ASSET_DIR / name).read_text(encoding="utf-8")
    except OSError as error:
        raise PublicationError([f"packaged render asset is unavailable: {name}"]) from error


def _logo() -> str:
    """The Autoform mark: the smallest blueprint there is, drawn as an A.

    Two prerequisites at the base and one result above them is the smallest
    non-trivial dependency graph, and its edges happen to be the strokes of an
    A. So the letter and the thing it stands for are the same drawing.

    It is set white on a gradient tile rather than as line art because the same
    file serves as the 16px favicon, where a thin stroke disappears; a filled
    tile also needs no light and dark variants, since it carries its own
    background onto either header.
    """

    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" \
height="48" role="img" aria-labelledby="autoform-logo-title">
  <title id="autoform-logo-title">Autoform</title>
  <defs>
    <linearGradient id="autoform-sweep" x1="0" y1="0" x2="48" y2="48" \
gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#0082FB"/>
      <stop offset="0.32" stop-color="#0064E0"/>
      <stop offset="0.68" stop-color="#7B3FE4"/>
      <stop offset="1" stop-color="#E0447B"/>
    </linearGradient>
  </defs>
  <rect width="48" height="48" rx="11" fill="url(#autoform-sweep)"/>
  <g fill="#FFFFFF" stroke="#FFFFFF" stroke-width="3.6" stroke-linecap="round"
     stroke-linejoin="round">
    <path d="M12.4 35.2 L24 12.8 L35.6 35.2" fill="none"/>
    <path d="M17.6 25.2 L30.4 25.2" fill="none"/>
    <circle cx="24" cy="12.8" r="4.2" stroke="none"/>
    <circle cx="12.4" cy="35.2" r="3.8" stroke="none"/>
    <circle cx="35.6" cy="35.2" r="3.8" stroke="none"/>
  </g>
</svg>
"""


def _mermaid_script() -> str:
    """Draw authored diagrams, and redraw them whenever the colour scheme changes.

    The site's dependency maps are the explorer, so every diagram this script
    sees is a fence an author wrote. Mermaid injects its own styles with
    ``!important`` scoped to the SVG id, so a stylesheet cannot recolour a
    diagram after the fact. The palette has to go in as ``classDef`` at render
    time, which means owning the render call and repeating it on a theme switch.

    Loose security is what enables ``click`` links, and it also runs a
    click's ``call`` as script and draws a label's markup. So only a graph
    render writes, of class :data:`~autoform_cli.mermaid.GRAPH_CLASS` in raw
    HTML no article may hold, with escaped labels, would be drawn loose; an
    author's diagram is drawn strictly, its labels as text and with no click.
    Render writes no such graph now that the explorer draws its maps, so every
    diagram is drawn strictly. Diagrams are drawn one at a time, since the
    level is Mermaid's global setting, and one that fails leaves the others
    drawn.
    """
    classdefs = json.dumps(
        {scheme: mermaid.classdef_lines(dark=scheme == "dark") for scheme in ("light", "dark")},
        indent=2,
    )
    return f"""/* Generated by autoform render. Edits are overwritten. */
(function () {{
  var CLASSDEFS = {classdefs};
  if (typeof mermaid === "undefined") return;
  // The map sits in a panel of the page's own colour, so the diagram supplies
  // no background of its own and borrows the site's type and rules.
  var FONT = '"Plus Jakarta Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  function variables(mode) {{
    return mode === "dark"
      ? {{ background: "transparent", lineColor: "#8A8D91", primaryTextColor: "#E4E6EB",
           mainBkg: "#242526", clusterBkg: "transparent", edgeLabelBackground: "#242526" }}
      : {{ background: "transparent", lineColor: "#8A8D91", primaryTextColor: "#050505",
           mainBkg: "#FFFFFF", clusterBkg: "transparent", edgeLabelBackground: "#FFFFFF" }};
  }}
  mermaid.initialize({{
    startOnLoad: false, securityLevel: "strict", theme: "neutral",
    fontFamily: FONT, themeVariables: variables("light")
  }});

  var counter = 0;
  var blocks = Array.prototype.map.call(
    document.querySelectorAll(".mermaid"),
    function (element) {{
      return {{
        element: element,
        source: element.textContent,
        generated: element.classList.contains("{mermaid.GRAPH_CLASS}")
      }};
    }}
  );

  function scheme() {{
    return document.body.getAttribute("data-md-color-scheme") === "slate" ? "dark" : "light";
  }}

  // Every pass, a scheme change's too, waits for the one before, since the
  // security level each diagram is drawn at is Mermaid's global setting.
  var queue = Promise.resolve();
  function draw() {{
    var mode = scheme();
    blocks.forEach(function (block) {{
      queue = queue.then(function () {{
        mermaid.initialize({{
          startOnLoad: false,
          securityLevel: block.generated ? "loose" : "strict",
          theme: mode === "dark" ? "dark" : "neutral",
          fontFamily: FONT,
          themeVariables: variables(mode)
        }});
        // The palette is a flowchart's; an author's diagram may be any kind.
        var source = block.generated ? block.source + "\\n" + CLASSDEFS[mode].join("\\n") : block.source;
        return mermaid.render("bp-graph-" + counter++, source).then(function (result) {{
          block.element.innerHTML = result.svg;
          if (result.bindFunctions) result.bindFunctions(block.element);
        }});
      }}).catch(function (error) {{
        console.error("autoform: a diagram could not be drawn", error);
      }});
    }});
  }}

  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", draw);
  }} else {{
    draw();
  }}

  new MutationObserver(function (mutations) {{
    mutations.forEach(function (mutation) {{
      if (mutation.attributeName === "data-md-color-scheme") draw();
    }});
  }}).observe(document.body, {{ attributes: true }});
}})();
"""


@dataclass(slots=True)
class RenderReport:
    """What a render produced, and what it could not resolve."""

    output_dir: Path
    pages: int = 0
    nodes: int = 0
    linked: int = 0
    unresolved: list[str] = field(default_factory=list)
    """A message for each published declaration not found, or defined more than once, in the sources."""
    unchecked: bool = False
    """A published declaration was not looked up because the sources were not read."""
    warnings: list[str] = field(default_factory=list)


class PublicationError(ValueError):
    """The requested static publication could expose or destroy local state."""

    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


def publication_issues(graph: Graph, blueprint: Path) -> list[str]:
    """What ``autoform check`` reports about the site for ``graph``: the
    warnings :func:`_publication` gives for the pages and files render would
    publish, then what would stop render, the inputs it refuses to capture
    and the files in the way of its own. Source notes are read as pages
    wherever the site would link them instead, so the verdict does not depend
    on the clone's remote.
    """

    # The root load_graph took, so every node's path lies below it.
    blueprint = blueprint_root(blueprint)
    try:
        # Capture leaves out the folders that say published: false, as render does.
        snapshot, _unpublished, _nodes = _capture_blueprint(blueprint)
    except PublicationError as exc:
        return [f"{issue}; autoform render stops until this is fixed" for issue in exc.issues]
    files = _captured_files(snapshot, blueprint)
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


def _captured_files(snapshot: TreeSnapshot, root: Path) -> dict[Path, bytes]:
    """The captured blueprint's files, keyed by their path below ``root``."""

    return {root / name: data for name, data in snapshot.files}


def _unpublished_folders(snapshot: TreeSnapshot, root: Path) -> tuple[Path, ...]:
    """The folders capture left out because their README says ``published: false``, below ``root``."""

    return tuple(root / name for name, kind in snapshot.omitted if kind == "unpublished")


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
    #: The site's ``javascripts/mathjax.js``, built from the captured
    #: ``tex-macros.json`` the site publishes.
    script: str
    #: What the site leaves out or shows as typed, which publishes nonetheless.
    warnings: list[str]
    #: The pages shown as typed, by where they are published.
    typed: frozenset[Path]


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
    text :func:`~autoform_cli.publishable.publishable_article` returns for it
    once its links are resolved, and as :func:`~autoform_cli.publishable.shown_as_typed`
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
            node_id: (destination / node.path.relative_to(blueprint), "")
            for node_id, node in graph.nodes.items()
            if node_id in containers or (not node.formalizable and node.catalog is None)
        }
    )
    node_sources = {
        node.path.resolve(): node_id for node_id, node in graph.nodes.items()
    }

    chapters = {page: group for group, page in group_pages.items()}
    warnings: list[str] = []
    typed: set[Path] = set()
    # Render writes its own file where a vault's copy of one of these would go.
    written_over = set(_site_paths(graph))
    # A link to a file the site leaves out keeps only its text, so the strict
    # build finds nothing missing.
    left_out = frozenset(
        source.resolve()
        for source in snapshot
        if source.relative_to(blueprint).as_posix() not in written_over
        and (
            source.relative_to(blueprint).parts[:1] == (READBACKS_DIR,)
            or source.suffix.lower() not in (*PAGE_SUFFIXES, *_STATIC_SUFFIXES)
            or (source.suffix.lower() in PAGE_SUFFIXES and not _is_utf8(snapshot[source]))
        )
    )

    def published(text: str, source: Path, page: Path, *, whole: bool = True) -> tuple[str, tuple[str, ...]]:
        """The page ``text`` is published as, or the part of ``page`` it is
        unless ``whole``, and what keeps it from being published as written.
        A chapter's page is its narrative with render's own markup set between
        its lines, so each stretch between is read too, as it sits there. Its
        notation goes straight to the warnings."""

        dropped: list[str] = []
        page_text, found, notation = _read_page(
            _rewrite_links(
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
            )
        )
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
        label = node_sources.get(source.resolve()) or source.relative_to(blueprint).as_posix()
        warnings.extend(f"{label}: {issue}" for issue in (*found, *notation))
        warnings.extend(
            f"{label}: links to {target!r}, which is hidden or in an unpublished folder, so the site keeps only "
            "the link's text"
            for target in dict.fromkeys(dropped)
        )
        if found and source.parent == blueprint and source.name.lower() == "readme.md":
            # The landing page takes only the title and the lead, and escapes both.
            warnings.append(f"{label}: the landing page shows only its title and lead, as text")
        elif found:
            warnings.append(f"{label}: the site shows it as typed until these are fixed")
            # A leaf's part of its chapter is its box; the chapter itself is not typed.
            if whole:
                typed.add(page)
        return page_text, found

    articles: dict[Path, str] = {}
    statements: dict[str, tuple[str, str]] = {}
    for node_id, node in graph.nodes.items():
        # The captured bytes, the state the graph describes once capture checks
        # it. Check's graph also holds the articles in unpublished folders, which
        # capture leaves out.
        captured = snapshot.get(node.path)
        if captured is None:
            continue
        leaf = (node.formalizable or node.catalog is not None) and node_id not in containers
        text, found = published(captured.decode("utf-8"), node.path, targets[node_id][0], whole=not leaf)
        # Narrative articles remain book pages. Formalizable leaves and module
        # catalogs are consolidated into their containing article with stable anchors.
        if leaf:
            parts = statement_and_notes(text)
            # A flagged leaf's box shows its statement and notes as typed.
            statements[node_id] = (shown_as_typed("\n\n".join(filter(None, parts)), page=False), "") if found else parts
        else:
            articles[node.path.resolve()] = shown_as_typed(text) if found else text

    files: dict[Path, str | bytes] = {}
    for source in sorted(snapshot):
        relative = source.relative_to(blueprint)
        # Source notes leave the site entirely once readers can reach them in
        # the repository, so the book has one reference surface rather than two.
        if sources_base is not None and relative.parts[:1] == (SOURCES_DIR,):
            continue
        # Read-back cards are testimony about a statement, never pages of
        # their own.
        if relative.parts[:1] == (READBACKS_DIR,):
            continue
        # The graph has read atlas.json, and nothing on the site reads it. It may
        # name articles in unpublished folders, so it stays out of the site.
        if relative.as_posix().casefold() == "atlas.json":
            continue
        if source.resolve() in node_sources:
            if source.resolve() in articles:
                files[source] = articles[source.resolve()]
        elif source.suffix.lower() in PAGE_SUFFIXES:
            try:
                written = snapshot[source].decode("utf-8")
            except UnicodeDecodeError:
                warnings.append(
                    f"{relative.as_posix()}: is not UTF-8 text, as a Markdown page must be, so the site leaves it "
                    "out; save it as UTF-8"
                )
                continue
            text, found = published(written, source, destination / relative)
            files[source] = shown_as_typed(text) if found else text
        elif relative.as_posix() in written_over:
            continue
        elif source.suffix.lower() in _STATIC_SUFFIXES:
            files[source] = snapshot[source]
        elif source.suffix.lower() == ".canvas":
            continue  # an Obsidian canvas, which the vault keeps for itself
        else:
            warnings.append(
                f"{relative.as_posix()}: the site leaves out {source.suffix.lower() or 'extensionless'} "
                "files, since a browser could run one as the site's own page; write it as Markdown, "
                "save it as an image (PNG, JPEG, GIF, WebP), a PDF or plain text, "
                "or keep it outside blueprint/"
            )
    if blueprint / TEX_MACROS not in snapshot and any(source.is_relative_to(blueprint / TEX_MACROS) for source in snapshot):
        warnings.append(f"{TEX_MACROS}: is a directory, so the site has no project macros; make it a JSON file")
    script, script_warnings = mathjax_script(snapshot.get(blueprint / MATHJAX_SCRIPT), snapshot.get(blueprint / TEX_MACROS))
    warnings.extend(script_warnings)
    return _Publication(groups, targets, node_sources, files, statements, script, warnings, frozenset(typed))


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _read_page(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """:func:`~autoform_cli.publishable.publishable_article` of ``text``, or,
    for a page nested deeper than the site's converter can read, a finding
    that shows it as typed instead of stopping check or render."""

    try:
        return publishable_article(text)
    except RecursionError:
        finding = "the page nests lists deeper than the site's Markdown converter can read; flatten it"
        return published_page(text), (finding,), ()


def _titled(published: str, authored: bytes | None, page: Path) -> str:
    """``published``, a page an author wrote, under a header that names its
    title, so MkDocs takes no title from the page itself: the theme prints a
    title as it is, and in ``<title>`` reads a reference back as the
    character it names. The title is the text of the page's H1 in
    ``authored``, as render publishes it when nothing is flagged, so a page
    shown as typed keeps it: the first block's, when that block is an H1, as
    MkDocs would take it, else the first H1 below, else the file's name."""

    text = published_page(authored.decode("utf-8")).removeprefix(PAGE_HEADER) if authored else ""
    later = _first_h1(text)
    heading = page_title(text) or (rendered_visible_text(later) if later else None)
    return f"---\n{header_title(heading or _file_title(page))}\n---\n" + published.removeprefix(PAGE_HEADER)


def _file_title(source: Path) -> str:
    """The title MkDocs gives a page with no H1, from its file's name."""

    title = ("index" if source.stem == "README" else source.stem).replace("-", " ").replace("_", " ")
    return title.capitalize() if title.lower() == title else title


def _header_title(text: str) -> str | None:
    """The title :func:`_titled` wrote into the header of the page ``text``,
    as the text a reader sees."""

    lines = text.split("\n", 3)
    if len(lines) < 3 or lines[0] != "---" or not lines[1].startswith("title: ") or lines[2] != "---":
        return None
    # JSON writes a character above U+FFFF as a surrogate pair, where the
    # header has YAML's \U escape; a backslash the title holds is doubled.
    value = re.sub(
        r"(?<!\\)((?:\\\\)*)\\U([0-9A-F]{8})",
        lambda escape: escape[1] + json.dumps(chr(int(escape[2], 16)))[1:-1],
        lines[1].removeprefix("title: "),
    )
    return html.unescape(json.loads(value))


def _site_paths(graph: Graph) -> tuple[str, ...]:
    """Every file render writes into the site for ``graph`` other than the
    blueprint's own, relative to the site's root: the derived pages, the
    publication manifest, and the site's assets."""

    return tuple(
        sorted(
            {
                *(_GENERATED_FILES - RETIRED_VIEWS),
                STRUCTURE_PAGE,
                "SUMMARY.md",
                *(_group_page(group).as_posix() for group in _group_nodes(graph)),
                *graph_pages.graph_page_paths(graph),
                *_ASSETS,
            }
        )
    )


def _in_the_way(blueprint: Path, graph: Graph, snapshot: dict[Path, bytes]) -> list[str]:
    """A captured file below one of :func:`_site_paths`, or at one of its
    folders. Render writes these over the captured files it copied, so it
    names the path before writing anything; capture has already refused a
    symlink or a special file anywhere."""

    captured = {source.relative_to(blueprint).as_posix() for source in snapshot}
    folders = {"/".join(parts[:end]) for parts in (path.split("/") for path in captured) for end in range(1, len(parts))}
    blocked: dict[str, str] = {}
    for relative in _site_paths(graph):
        found = (relative, "a directory") if relative in folders else None
        if found is None:
            parts = relative.split("/")
            found = next(
                (("/".join(parts[:end]), "a file") for end in range(1, len(parts)) if "/".join(parts[:end]) in captured),
                None,
            )
        if found is None or found[0] in blocked:
            continue
        where, kind = found
        purpose = "writes a file" if where == relative else f"needs a folder for {relative}"
        blocked[where] = f"{where}: is {kind}, where autoform render {purpose}; remove it"
    return list(blocked.values())


def render_site(
    blueprint_dir: str | Path,
    output_dir: str | Path,
    *,
    lean_root: str | Path | None = None,
    repository_url: str | None = None,
    ref: str | None = None,
) -> RenderReport:
    """Write deterministic, read-only projections of the Markdown blueprint.

    Authored Markdown remains the only graph authority. Full mathematical reading,
    derived progress, and multiscale graph navigation share one published shell.
    Publication excludes hidden and operational files, rejects symlinks,
    and never embeds timestamps or machine-specific paths. The blueprint and the
    Lean sources are each read once, and every page, link and hash comes from
    those bytes. The site is built beside the output and swapped in whole.
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
    snapshot, unpublished_ids, unpublished_nodes = _capture_blueprint(blueprint)
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
    with _private_copy(snapshot, relative) as (copy_root, copy):
        graph = _load_graph(copy, blueprint, unpublished_ids)
        coverage, coverage_issues = load_coverage(copy)
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
        # Without descriptor capture every Lean capture fails closed. The implicit
        # root is then not read and the site has no code links; an explicit Lean
        # root asks for links, so it still fails. Like every Lean capture, this one
        # skips each folder holding a complete publication.json: a finished site,
        # mkdocs' copy of one, or a replaced site kept as a backup.
        indexed = lean_root is not None or descriptor_capture_supported()
        try:
            lean = (
                snapshot_project_sources(repo_root, exclude_roots=(copy_root,))
                if indexed
                else IndexedSourceSnapshot(SourceIndex(repo_root, {}, ""), revision="")
            )
        except OSError as error:
            hint = "" if descriptor_capture_supported() else "; run without --lean-root to publish without code links"
            raise PublicationError([index_failure_message(error) + hint]) from error
        repository_url = repository_url if repository_url is not None else detect_repository_url(repo_root)
        ref = ref if ref is not None else detect_ref(repo_root)
        sources = SOURCES_DIR in snapshot.directories
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
            indexed=indexed,
            links_blueprint=links_blueprint,
            spellings=spellings,
            root_prefix=root_prefix,
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
                sources=sources,
                source_revision=source_revision,
                lean_source_revision=lean.revision,
                unpublished=_unpublished_folders(snapshot, copy),
                unpublished_nodes=unpublished_nodes,
                files=_captured_files(snapshot, copy),
            ),
        )
    report.warnings[:0] = recovered
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
    sources: bool,
    source_revision: str,
    lean_source_revision: str,
    unpublished: tuple[Path, ...] = (),
    unpublished_nodes: frozenset[str],
    files: dict[Path, bytes],
) -> RenderReport:
    """Write the site into *destination* from the private copy at *blueprint*."""
    # Audit reports coverage-role issues and the atlas.json entries the loader
    # ignored. A valid contract still publishes with them, as it does with
    # MAPPED rows. An issue without a line already names the contract in its
    # reason, so it gets no prefix. Check accepts an entry naming an article
    # in an unpublished folder, so render does not warn about it either.
    coverage_warnings = [
        f"coverage contract line {issue.line}: {issue.reason}"
        if issue.line
        else issue.reason
        for issue in validate_coverage_roles(graph, coverage)
    ]
    coverage_warnings += (f"atlas.json: {omission}" for omission in atlas_omissions(graph, unpublished_nodes))
    statuses = status.derive(graph)
    numbers = _number_nodes(graph)
    used_by = _reverse_edges(graph)
    # Sources leave the site only for a directory the verified commit holds.
    sources_base = _sources_base(blueprint, repo_root, linker) if sources else None
    stops = _in_the_way(blueprint, graph, files)
    if stops:
        raise PublicationError(stops)
    site = _publication(
        graph, blueprint, files, destination=destination, sources_base=sources_base, unpublished=unpublished
    )
    revisions = {"source_revision": source_revision, "lean_source_revision": lean_source_revision}
    # Written first, so a Lean walk by a concurrent render skips this stage;
    # rewritten with the file inventory once every page exists.
    _write_publication_manifest(destination, graph, linker, coverage=coverage, **revisions)

    report = RenderReport(output_dir=destination, warnings=[*coverage_warnings, *site.warnings])
    groups, targets, node_sources = site.groups, site.targets, site.node_sources
    if not linker.indexed:
        # Only the declarations a page would link are lost. A blueprint that
        # names none publishes the same pages as a host that read the sources.
        report.unchecked = any(
            declaration_names(graph.nodes[node_id].lean or "")
            for node_ids in groups.values()
            for node_id in node_ids
        )
        if report.unchecked:
            report.warnings.append(
                "code links were skipped because this platform cannot capture Lean "
                "sources safely (directory-descriptor traversal is unavailable); "
                "render on Linux or macOS to link declarations"
            )
    containers = _containers(graph)
    group_pages = {group: destination / _group_page(group) for group in groups}
    for source, content in site.files.items():
        target = destination / source.relative_to(blueprint)
        # Directories are created on demand, so a directory holding nothing
        # but absorbed nodes leaves no empty shell behind.
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target.write_text(_titled(content, files.get(source), target), encoding="utf-8")
        else:
            target.write_bytes(content)
        report.pages += 1

    overview = destination / "README.md"
    # The author's page, as render publishes it when nothing in it is flagged:
    # the hero escapes its title and lead itself, and a page shown as typed has
    # neither. A disk that ignores case serves readme.md here too.
    authored = next(
        (data for source, data in files.items() if source.parent == blueprint and source.name.lower() == "readme.md"),
        None,
    )
    if overview.is_file() and authored is not None and _is_utf8(authored):
        overview.write_text(
            _render_landing_page(
                published_page(authored.decode("utf-8")),
                graph=graph,
                statuses=statuses,
                coverage=coverage,
                groups=groups,
                group_pages=group_pages,
                page=overview,
                destination=destination,
                targets=targets,
            ),
            encoding="utf-8",
        )

    for group, node_ids in groups.items():
        page = group_pages[group]
        page.parent.mkdir(parents=True, exist_ok=True)
        narrative = page.read_text(encoding="utf-8") if page.is_file() else None
        chapter, linked, unresolved = _render_chapter(
            group,
            node_ids,
            graph=graph,
            statuses=statuses,
            numbers=numbers,
            used_by=used_by,
            linker=linker,
            page=page,
            targets=targets,
            narrative=narrative,
            blueprint=blueprint,
            repo_root=repo_root,
            destination=destination,
            node_sources=node_sources,
            containers=containers,
            statements=site.statements,
            typed=page in site.typed,
        )
        page.write_text(chapter, encoding="utf-8")
        if narrative is None:  # a milestone with no narrative page of its own
            report.pages += 1
        report.nodes += len(node_ids)
        report.linked += linked
        report.unresolved.extend(unresolved)

    book_pages = _book_page_order(
        blueprint,
        destination,
        graph,
    )
    # The landing page is a dashboard, not chapter one. Previous/next belongs
    # to the book, so the strip starts at the contents page.
    _append_book_navigation([p for p in book_pages if p != overview])
    structure = destination / STRUCTURE_PAGE
    structure.write_text(
        _render_structure_page(
            page=structure,
            entry=destination / graph_pages.book_contents(graph, graph_views.group_nodes(graph)),
        ),
        encoding="utf-8",
    )
    report.pages += 1
    (destination / "SUMMARY.md").write_text(
        _render_summary_nav(book_pages, destination=destination, overview=overview),
        encoding="utf-8",
    )

    contexts: dict[str, tuple[Path, str]] = {}
    generated_graph_pages = graph_pages.write_graph_pages(
        graph,
        statuses,
        destination,
        node_links=lambda page, node_ids: _node_links(targets, page, node_ids),
        book_targets=targets,
        contexts_out=contexts,
    )
    report.pages += len(generated_graph_pages)

    # These are the final safe pages, including generated theorem cards and
    # their exact anchors. Never reread an authored article to build the reader.
    page_contexts: dict[Path, tuple[Path, str, str | None]] = {}
    for node_id, (page, anchor) in targets.items():
        payload, scope = contexts[node_id]
        if page not in page_contexts or not anchor:
            page_contexts[page] = (payload, scope, node_id if not anchor else None)
    canonical_pages = {
        destination / source.relative_to(blueprint)
        for source, content in site.files.items()
        if isinstance(content, str) and source.suffix.lower() in PAGE_SUFFIXES
    } | set(group_pages.values()) | {overview}
    for page in canonical_pages:
        if not page.is_file():
            continue
        payload, scope, owner = page_contexts.get(
            page, (destination / graph_pages.PROJECT_PAYLOAD, "roadmap", None)
        )
        page.write_text(
            _integrated_page(
                page.read_text(encoding="utf-8"), page=page, destination=destination,
                payload=payload, scope=scope, owner=owner,
            ),
            encoding="utf-8",
        )

    for relative, contents in (
        (STYLESHEET, _stylesheet()),
        (MERMAID_SCRIPT, _mermaid_script()),
        # Written here, never kept from the vault.
        (MATHJAX_SCRIPT, site.script),
        (LIVE_SCRIPT, _static_asset("blueprint-live.js")),
        (DAG_SCRIPT, dag_viewer.viewer_script()),
        (DAG_STYLE, dag_viewer._STYLE),
        (LOGO, _logo()),
    ):
        asset = destination / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(contents, encoding="utf-8")
    _write_publication_manifest(destination, graph, linker, coverage=coverage, **revisions)
    return report


def _integrated_page(
    text: str, *, page: Path, destination: Path, payload: Path, scope: str, owner: str | None = None,
) -> str:
    """Keep a complete published page readable before moving it into the shared UI.

    Only generated framing is added. The header, mathematical body, statement
    IDs, citations, status markup and shown-as-typed boxes remain verbatim.
    """
    lines = text.splitlines(keepends=True)
    end = 0
    if lines and lines[0].strip() == "---":
        end = next((index + 1 for index in range(1, len(lines)) if lines[index].strip() == "---"), 0)
    header, body = "".join(lines[:end]), "".join(lines[end:])
    node = f' data-autoform-node-id="{html.escape(owner, quote=True)}"' if owner else ""
    title_header = "".join(lines[:2]) + "---\n" if end else text
    title = _header_title(title_header) or rendered_visible_text(_first_h1(text) or "") or _file_title(page)
    explorer = dag_viewer.render_container(
        mermaid.relative_link(payload, page, ".json"),
        script_href=mermaid.relative_link(destination / DAG_SCRIPT, page, ".js"),
        style_href=mermaid.relative_link(destination / DAG_STYLE, page, ".css"),
        search_href=mermaid.relative_link(destination / graph_pages.SEARCH_INDEX, page, ".json"),
        page_href=_as_published(mermaid.relative_link(page, page, ".html")),
        scope=scope,
        fallback_links=(("Read the mathematical page", "#"),),
    )
    return (
        header + '\n<div class="bp-explorer-page" markdown="1">\n'
        + f'<div class="bp-reading-source"{node} data-page-title="{html.escape(title, quote=True)}" markdown="1">\n\n'
        + body + '\n\n</div>\n\n' + explorer + '\n</div>\n'
    )


def _capture_blueprint(blueprint: Path) -> tuple[TreeSnapshot, frozenset[str], frozenset[str]]:
    """Read the blueprint once, refusing what must not be published.

    Through descriptors where the host has them; elsewhere (Windows) as two
    pathname captures that must agree. Folders whose README says ``published:
    false`` are left out before anything is refused or published, so no page,
    hash or check sees them; the article and node IDs in them come beside the snapshot.
    """
    try:
        selection = _PUBLICATION_SELECTION
        with bind_directory_tree(blueprint, selection=selection) as tree:
            snapshot = tree.capture()
    except TreeSnapshotError as error:
        if not blueprint.is_dir():
            raise PublicationError([f"blueprint directory does not exist: {blueprint}"]) from error
        if isinstance(error, TreeChangedError):
            raise PublicationError(["blueprint changed while it was read; retry"]) from error
        raise PublicationError([f"blueprint could not be captured: {error}"]) from error
    folders, article_ids = unpublished_content(snapshot.files)
    node_ids = unpublished_node_ids(snapshot.files, folders)

    def kept(name: str) -> bool:
        return not any(name == folder or name.startswith(f"{folder}/") for folder in folders)

    snapshot = replace(
        snapshot,
        directories=tuple(filter(kept, snapshot.directories)),
        # Each folder stays on record as left out, so a link into one keeps only its text.
        omitted=tuple(
            sorted([*(entry for entry in snapshot.omitted if kept(entry[0])), *((f, "unpublished") for f in folders)])
        ),
        **{
            field: tuple(entry for entry in getattr(snapshot, field) if kept(entry[0]))
            for field in ("files", "symlinks", "special", "identities")
        },
    )
    omitted, symlinks = dict(snapshot.omitted), dict(snapshot.symlinks)
    unsupported = dict(snapshot.unsupported_entries())
    # Published names that differ only in case or Unicode form would merge on a
    # case-insensitive filesystem. The generated paths claim theirs first.
    spellings = {name.casefold(): name for name in _GENERATED_PATHS}
    issues: list[str] = []
    for raw in sorted({*snapshot.directories, *dict(snapshot.files), *unsupported, *omitted} - {""}):
        relative = PurePosixPath(raw)
        folded = "/".join(unicodedata.normalize("NFC", part).casefold() for part in relative.parts)
        if _is_sensitive(relative):
            issues.append(f"refusing local or sensitive publication input: {raw}")
        elif raw in omitted:
            continue
        elif raw in symlinks:
            issues.append(f"refusing symlink in blueprint publication: {raw}")
        elif raw in unsupported:
            issues.append(f"refusing non-regular blueprint publication input: {raw}: {unsupported[raw]}")
        elif (prior := spellings.setdefault(folded, raw)) == raw:
            continue
        elif prior in _GENERATED_PATHS:
            issues.append(
                f"refusing blueprint path {raw}: it differs only in case from the generated "
                f"{prior}, and the two would collide; rename it"
            )
        else:
            issues.append(
                f"refusing blueprint paths {prior} and {raw}: they differ only in case or Unicode "
                "form, and the two would collide; rename one"
            )
    if issues:
        raise PublicationError(issues)
    return snapshot, article_ids, node_ids


def _is_sensitive(relative: PurePosixPath) -> bool:
    name = relative.name.casefold()
    return bool(
        {part.casefold() for part in relative.parts} & _LOCAL_ONLY_NAMES
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith((".key", ".log", ".pem"))
    )


def _is_hidden(relative: Path | PurePosixPath) -> bool:
    return any(part.startswith(".") for part in relative.parts)


@contextmanager
def _private_copy(
    snapshot: TreeSnapshot, relative: Path = Path("blueprint"), prefix: str | tuple[str, ...] = ""
) -> Iterator[tuple[Path, Path]]:
    """Write one captured generation into a private directory for main's path-based readers.

    Nothing else writes there, so every later read, by the loaders and by the
    renderer, sees exactly the captured bytes. Yields the directory and the copy
    at *relative* below it. Files are written under the umask, so published
    copies of them get the modes plain writes would.
    """
    with tempfile.TemporaryDirectory(prefix="autoform-blueprint-") as scratch:
        root = Path(scratch).resolve()
        copy = root / relative
        try:
            for name in snapshot.directories:
                (copy / name).mkdir(parents=True, exist_ok=True)
            for name, data in snapshot.files:
                if name.startswith(prefix):
                    (copy / name).write_bytes(data)
        except OSError as error:
            message = f"could not copy the blueprint into {root}: {error.strerror or error}"
            raise PublicationError([message]) from error
        yield root, copy


def _load_graph(copy: Path, blueprint: Path, unpublished_ids: frozenset[str]) -> Graph:
    """Load the copy's graph, then check the vault's implementation notes as ``load_graph`` would.

    The notes are hidden, so the copy lacks them. Nothing is published or hashed
    from them, so reading them in place changes no output. A note may belong to
    an article in an unpublished folder, which the copy lacks too.
    """
    try:
        graph = load_graph(copy)
    except GraphValidationError as error:
        issues = [issue.replace(str(copy), str(blueprint)) for issue in error.issues]
        raise GraphValidationError(issues) from None
    article_ids = {node.article_id: node.id for node in graph.nodes.values() if node.article_id is not None}
    article_ids.update(dict.fromkeys(unpublished_ids, ""))
    if issues := _implementation_note_issues(blueprint, article_ids):
        raise GraphValidationError(issues)
    return graph


def load_publication_source(blueprint_dir: str | Path) -> tuple[str, Graph]:
    """Read the blueprint once; return its publication revision and the graph of those bytes."""
    blueprint = blueprint_root(blueprint_dir)
    snapshot, unpublished_ids, _nodes = _capture_blueprint(blueprint)
    # The graph reads the articles and the atlas.
    with _private_copy(snapshot, prefix=("roadmap/", "atlas.json")) as (_root, copy):
        return _source_revision(snapshot), _load_graph(copy, blueprint, unpublished_ids)


#: An output render may replace: its identity and its files' digests (none for an empty one).
_State = tuple[tuple[int, int], dict[str, str]]


def _is_os_metadata(name: str) -> bool:
    base = PurePosixPath(name).name
    return base == ".DS_Store" or base.startswith("._")


def _destination_state(path: Path) -> _State | None:
    """The state of an empty directory or an exact Autoform site at *path*.

    ``None`` means nothing is there. Anything render does not own is refused: a
    symlink, a file, a legacy or foreign directory, or a site whose files no
    longer match its manifest. macOS metadata files (Finder's ``.DS_Store`` and
    the ``._*`` files it writes on non-APFS volumes) are disposable and leave
    with the directory that holds them.
    """
    try:
        metadata = os.lstat(path)
        empty = stat.S_ISDIR(metadata.st_mode) and all(
            _is_os_metadata(name) and stat.S_ISREG(os.lstat(path / name).st_mode) for name in os.listdir(path)
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PublicationError([f"could not inspect {path}: {error.strerror or error}"]) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise PublicationError(["refusing symlink output directory"])
    if not stat.S_ISDIR(metadata.st_mode):
        raise PublicationError(["output path exists and is not a directory"])
    identity = (metadata.st_dev, metadata.st_ino)
    if empty:
        return identity, {}
    manifest = None
    try:  # bounded, and read before anything below it
        if stat.S_ISREG(os.lstat(path / PUBLICATION_MANIFEST).st_mode):
            with open(path / PUBLICATION_MANIFEST, "rb") as handle:
                data = _read_prefix(handle, PUBLICATION_MANIFEST_MAX_BYTES + 1)  # read(n) allocates n bytes first
            manifest = json.loads(data) if len(data) <= PUBLICATION_MANIFEST_MAX_BYTES else None
    except (OSError, ValueError, RecursionError):
        pass
    if isinstance(manifest, dict) and manifest.get("schema") == "autoform-publication/v1":
        raise PublicationError(
            [
                f"refusing to replace the legacy autoform-publication/v1 site at {path}: it has no file "
                "inventory to check before deleting it; delete it once by hand and render again"
            ]
        )
    schema = manifest.get("schema") if isinstance(manifest, dict) else None
    if not (isinstance(schema, str) and schema in _REPLACEABLE_SCHEMAS):
        raise PublicationError(
            [
                "refusing to overwrite a non-Autoform output directory or legacy "
                "publication; choose an empty directory or remove it explicitly"
            ]
        )
    if manifest.get("complete") is not True:
        raise PublicationError(["refusing to overwrite an incomplete Autoform publication"])
    expected = manifest.get("files")
    if not isinstance(expected, dict):
        raise PublicationError(["publication manifest has no valid file inventory"])
    expected = {name: digest for name, digest in expected.items() if not _is_os_metadata(name)}
    try:
        with bind_directory_tree(path, expected_identity=identity) as tree:
            snapshot = tree.capture(digests=True)
    except TreeSnapshotError as error:
        raise PublicationError([f"could not inspect the output directory: {error}"]) from error
    files = {
        name: digest.hex()
        for name, digest in snapshot.files
        if name != PUBLICATION_MANIFEST and not _is_os_metadata(name)
    }
    names = {*files, *dict(snapshot.symlinks), *dict(snapshot.special)}
    if names != expected.keys():
        untracked = ", ".join(sorted(names ^ expected.keys()))
        raise PublicationError(
            [f"refusing to overwrite an output directory with untracked or missing files: {untracked}"]
        )
    if sorted(name for name in snapshot.directories if name) != manifest.get("directories"):
        raise PublicationError(
            ["refusing to overwrite an output directory with untracked or missing directories"]
        )
    if files != expected:
        raise PublicationError(["refusing to overwrite a modified Autoform publication"])
    return identity, files


def _publish(
    destination: Path, expected: _State | None, render: Callable[[Path], RenderReport]
) -> RenderReport:
    """Render into a hidden sibling of *destination*, then swap it into place.

    The previous site is deleted only once it is proved to be the exact site
    inspected before rendering; any other outcome keeps it.
    """
    stage = destination.with_name(f"{_STAGE_PREFIX}{secrets.token_hex(8)}")
    backup = _backup(destination)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(stage)  # the umask decides published modes
    except OSError as error:
        raise PublicationError(
            [
                f"could not create a publication stage in {destination.parent}: {error.strerror or error}; "
                "render needs write access to the output directory's parent"
            ]
        ) from error
    staged = _identity(stage)
    try:
        try:
            report = render(stage)
            for path in (*stage.rglob("*"), stage):
                _fsync(path)
        except OSError as error:
            raise PublicationError([f"could not write the site: {error}; output was not changed"]) from error
        with _locked_parent(destination.parent):
            if _destination_state(destination) != expected:
                unchanged = "output was not changed" if expected is None else "previous site was preserved"
                raise PublicationError([f"output directory changed during publication; {unchanged}"])
            try:
                _commit(stage, destination, backup, replace=expected is not None)
                with suppress(PermissionError):  # nor can such a parent be opened to sync
                    _fsync(destination.parent)
            except OSError as error:
                if _identity(stage) == staged and not os.path.lexists(backup):  # nothing moved
                    reason = _REFUSED_COMMITS.get(error.errno, error.strerror or str(error))
                    message = f"could not publish {destination}: {reason}; output was not changed"
                    raise PublicationError([message]) from error
                kept = next((path for path in (backup, stage) if os.path.lexists(path)), None)
                message = f"publishing {destination} may be incomplete: {error.strerror or error}"
                kept_note = f", and {kept} may hold the previous site" if kept else ""
                raise PublicationError([f"{message}; nothing was deleted{kept_note}"]) from error
        if expected is not None:  # the replaced site now sits at the stage's name
            report.warnings.extend(_retire(stage, expected))
        report.output_dir = destination
        return report
    finally:
        if _identity(stage) == staged:  # still the unpublished stage
            with suppress(OSError):
                _remove_tree(stage)


@contextmanager
def _locked_parent(parent: Path) -> Iterator[None]:
    """Hold the output parent's lock, waiting while another render commits beside this one.

    A filesystem without advisory locks (NFS without lockd) or a host without
    them (Windows) proceeds unlocked; the commit still re-checks the output it
    replaces.
    """
    if os.name == "nt":
        yield
        return
    import fcntl

    try:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except PermissionError:  # a parent the user may write but not read cannot hold the lock
        descriptor = None
    except OSError as error:
        message = f"could not open the output parent {parent}: {error.strerror or error}"
        raise PublicationError([message]) from error
    if descriptor is None:
        yield
        return
    try:
        deadline = time.monotonic() + _PUBLICATION_LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise PublicationError(
                        [
                            f"another process kept the output parent {parent} locked for "
                            f"{_PUBLICATION_LOCK_WAIT_SECONDS:g} seconds; output was not changed; retry"
                        ]
                    ) from error
                time.sleep(0.05)
            except OSError:
                break
        yield
    finally:
        os.close(descriptor)  # releases the lock


def _commit(stage: Path, destination: Path, backup: Path, *, replace: bool) -> None:
    """Move *stage* to *destination*; a replaced site ends up at *stage*'s name.

    One atomic exchange where the filesystem has it. Elsewhere (NFS, SMB, 9p,
    exFAT, ...) the old site moves to *backup* first, and the next render moves
    it back if this one dies before the second rename.
    """
    if not replace:
        os.rename(stage, destination)  # atomic; refuses a non-empty directory and a file
        return
    try:
        _exchange(stage, destination)
        return
    except OSError as error:
        if error.errno not in _UNSUPPORTED:
            raise
    os.rename(destination, backup)
    try:
        os.rename(stage, destination)
    except BaseException:
        with suppress(OSError):  # after a Ctrl-C that landed past the install, the new site stays
            os.rename(backup, destination)
        raise
    os.rename(backup, stage)  # no backup outlives the lock


def _exchange(first: Path, second: Path) -> None:
    """Swap two names in one step: RENAME_SWAP on macOS, RENAME_EXCHANGE on Linux."""
    if os.name == "nt":
        raise OSError(errno.ENOSYS, "atomic exchange is unavailable", str(second))
    library = ctypes.CDLL(None, use_errno=True)
    first_bytes, second_bytes = os.fsencode(first), os.fsencode(second)
    try:
        if sys.platform == "darwin":
            result = library.renamex_np(first_bytes, second_bytes, 0x00000002)
        else:
            result = library.renameat2(-100, first_bytes, -100, second_bytes, 2)
    except AttributeError:  # a C library without renameat2 (musl, glibc before 2.28)
        raise OSError(errno.ENOSYS, "atomic exchange is unavailable", str(second)) from None
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(second))


def _backup(destination: Path) -> Path:
    return destination.with_name(f"{_STAGE_PREFIX}backup-{destination.name}")


def _recover_backup(destination: Path) -> list[str]:
    """Finish a two-rename replacement that a killed render left half done.

    Beside a missing or empty output the backup is the site, so it goes back;
    beside an exact site it is the replaced one, so it goes. Anything else is
    left for the user. Returns warnings for the report.
    """
    backup = _backup(destination)
    if not os.path.lexists(backup):
        return []
    with _locked_parent(destination.parent):
        try:
            state = _destination_state(destination)
            if state is None or not state[1]:
                os.rename(backup, destination)  # also replaces an empty directory
                return []
            replaced = _destination_state(backup)
            if replaced is None:
                return []
            retired = destination.with_name(f"{_STAGE_PREFIX}{secrets.token_hex(8)}")
            os.rename(backup, retired)
        except (OSError, PublicationError) as error:
            raise PublicationError(
                [f"could not recover the previous site at {backup}: {error}; restore or remove it by hand"]
            ) from error
    return _retire(retired, replaced)


def _retire(site: Path, expected: _State) -> list[str]:
    """Delete a replaced site that still equals *expected*; otherwise say where it is."""
    try:
        unchanged = _destination_state(site) == expected
    except (OSError, PublicationError):
        unchanged = False
    if not unchanged:
        return [f"the previous site was kept at {site}: it changed during the commit"]
    try:
        _remove_tree(site)
    except OSError as error:
        reason = error.strerror or error
        return [f"the previous site at {site} was only partly deleted: {reason}; remove the rest by hand"]
    return []


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    return metadata.st_dev, metadata.st_ino


def _fsync(path: Path) -> None:
    """Flush one file or directory; some network filesystems cannot sync a directory.

    Windows cannot open a directory to sync it, and flushes only a writable handle.
    """
    if os.name == "nt":
        if path.is_file():
            with open(path, "r+b") as handle:
                os.fsync(handle.fileno())
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in _UNSUPPORTED or not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise
    finally:
        os.close(descriptor)


def _remove_tree(path: Path) -> None:
    """Delete a site or stage, its ``publication.json`` last.

    Until then a leftover of an interrupted deletion still carries the marker
    that keeps Lean walks out of it. The root itself goes with rmdir, so a file
    saved into it meanwhile stops the deletion instead of being lost. An entry
    already gone is skipped: on a volume without native extended attributes,
    macOS deletes ``._X`` along with ``X``.
    """
    with os.scandir(path) as entries:
        children = [entry for entry in entries if entry.name != PUBLICATION_MANIFEST]
    for child in children:
        if not child.is_dir(follow_symlinks=False):
            with suppress(FileNotFoundError):
                os.unlink(child.path)
        elif sys.version_info >= (3, 12):
            shutil.rmtree(child.path, onexc=_skip_missing)
        else:
            shutil.rmtree(child.path, onerror=_skip_missing)
    with suppress(FileNotFoundError):  # a stage that failed early has none
        os.unlink(path / PUBLICATION_MANIFEST)
    os.rmdir(path)


def _skip_missing(_function: object, _path: str, error: BaseException | tuple) -> None:
    """An rmtree error handler (``onexc``, or ``onerror``'s exc_info) that skips vanished entries."""
    exception = error[1] if isinstance(error, tuple) else error
    if not isinstance(exception, FileNotFoundError):
        raise exception


def _sources_base(blueprint: Path, repo_root: Path, linker: SourceLinker) -> "_SourceBase | None":
    """Where `blueprint/sources/` lives in the repository, if it can be linked.

    Source notes are a reader's transcription of the paper being formalised.
    Publishing them put a second copy of the reference on the site, at a URL
    nothing links deliberately and every `## Sources` list pointed at, so the
    book kept handing readers a page that was neither the book nor the paper.
    They stay in the vault and the site links out to them instead.

    Returns ``None`` when there are no repository coordinates to build a link
    from, or the blueprint's files are not in the linked commit. The pages are
    then published as before, because a site with no sources and no way to
    reach them is worse than a redundant page.
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
    return _SourceBase(linker.repository_url, linker.ref, relative, spellings)


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
    """A repository URL for a source note, or for the sources directory itself."""
    return sources_base.href(tail)


def _source_revision(snapshot: TreeSnapshot) -> str:
    """Hash every captured directory and file, each field length-framed."""
    digest = hashlib.sha256(b"autoform-markdown-publication/v3\0")
    for relative in sorted(relative or "." for relative in snapshot.directories):
        _update_digest(digest, b"directory", relative, b"")
    for relative, data in sorted(snapshot.files):
        _update_digest(digest, b"file", relative, data)
    return digest.hexdigest()


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
) -> None:
    paths = sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*"))
    manifest = {
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
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > PUBLICATION_MANIFEST_MAX_BYTES:  # the next render could not read it
        raise PublicationError(
            [f"generated publication manifest exceeds max_file_bytes={PUBLICATION_MANIFEST_MAX_BYTES}"]
        )
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


def _group_nodes(graph: Graph) -> dict[str, list[str]]:
    """Group formalizable leaves and module catalogs under their nearest containing article.

    Graph views deliberately roll a subtree up to its top-level scope. Book
    publication is different: a statement belongs where the author placed its
    nearest narrative container, so nested sections remain real book sections.
    """
    grouped: dict[str, list[str]] = {}
    containers = _containers(graph)
    for node_id in status.topological_order(graph):
        node = graph.nodes[node_id]
        if not (node.formalizable or node.catalog is not None) or node_id in containers:
            continue
        group = node.parent or "roadmap"
        grouped.setdefault(group, []).append(node_id)
    return grouped


def _group_page(group: str) -> Path:
    """Where a milestone's consolidated chapter lives in the output tree."""
    return (
        Path("roadmap/README.md")
        if group in {"", "roadmap"}
        else Path("roadmap") / group / "README.md"
    )


def _book_page_order(blueprint: Path, destination: Path, graph: Graph) -> list[Path]:
    """Follow authored container links to recover the book's page order."""
    ordered: list[Path] = []
    seen_outputs: set[Path] = set()
    visited_sources: set[Path] = set()
    containers = _containers(graph)
    book_sources = {
        node.path.resolve()
        for node in graph.nodes.values()
        if node.id in containers or (not node.formalizable and node.catalog is None)
    }
    pending = [blueprint / "README.md"]
    while pending:
        source = pending.pop().resolve()
        try:
            relative = source.relative_to(blueprint)
        except ValueError:
            continue
        output = (destination / relative).resolve()
        if output.is_file() and output not in seen_outputs:
            seen_outputs.add(output)
            ordered.append(output)
        if source in visited_sources or not source.is_file():
            continue
        visited_sources.add(source)
        linked_sources: list[Path] = []

        def collect(line: str) -> str:
            for match in _MARKDOWN_LINK.finditer(_link_mask(line)):
                raw = line[match.start("target") : match.end("target")]
                bare = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
                path = bare.partition("#")[0].partition("?")[0]
                try:
                    if not path or urlsplit(path).scheme or path.startswith("/"):
                        continue
                    candidate = (source.parent / unquote(path)).resolve()
                    candidate_relative = candidate.relative_to(blueprint)
                except ValueError:  # a target outside the vault, an unbalanced IPv6 bracket, or a decoded NUL
                    continue
                if (
                    not candidate_relative.parts
                    or candidate_relative.parts[0] != "roadmap"
                    or candidate.suffix.lower() != ".md"
                ):
                    continue
                if candidate not in book_sources:
                    continue
                linked_sources.append(candidate)
            return line

        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # the site leaves out a page that is not UTF-8, as check warns
        _outside_fences(text, collect)
        pending.extend(reversed(linked_sources))
    return ordered


def _append_book_navigation(pages: list[Path]) -> None:
    """Add previous/next links to the bottom of Blueprint pages, never global nav."""
    if len(pages) < 2:
        return
    titles = [_page_heading(page.read_text(encoding="utf-8")) or page.stem for page in pages]
    for index, page in enumerate(pages):
        links: list[str] = []
        if index:
            links.append(
                _book_navigation_link(
                    pages[index - 1],
                    page,
                    titles[index - 1],
                    direction="previous",
                )
            )
        if index + 1 < len(pages):
            links.append(
                _book_navigation_link(
                    pages[index + 1],
                    page,
                    titles[index + 1],
                    direction="next",
                )
            )
        navigation = (
            '<nav class="bp-book-nav" aria-label="Blueprint chapters">'
            + "".join(links)
            + "</nav>"
        )
        page.write_text(
            page.read_text(encoding="utf-8").rstrip() + "\n\n" + navigation + "\n",
            encoding="utf-8",
        )


def _book_navigation_link(
    target: Path,
    page: Path,
    title: str,
    *,
    direction: str,
) -> str:
    href = _as_published(mermaid.relative_link(target, page, ".html"))
    label = "Previous" if direction == "previous" else "Next"
    arrow = "←" if direction == "previous" else "→"
    return (
        f'<a class="bp-book-nav-link bp-book-nav-{direction}" '
        f'href="{html.escape(href, quote=True)}">'
        f'<span class="bp-book-nav-direction"><span aria-hidden="true">{arrow}</span> '
        f"{label}</span>"
        f'<span class="bp-book-nav-title">{html.escape(title)}</span>'
        "</a>"
    )


def _next_target(
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    *,
    page: Path,
    destination: Path,
    group_pages: dict[str, Path] | None = None,
    targets: dict[str, tuple[Path, str]] | None = None,
) -> str:
    """Name the result a contributor could pick up right now, with the way in.

    A dashboard that only counts is a scoreboard. The useful question on
    arriving is "what is unblocked, and where do I start reading?", which the
    DAG already answers: the first node in topological order whose
    prerequisites are met and whose proof is still open. Naming it without
    linking to it makes the reader hunt, so the card carries the statement, its
    chapter, and its dependency view.
    """
    containers = _containers(graph)
    links = _anchored_links(targets or {}, page)
    for node_id in status.topological_order(graph):
        node_status = statuses.get(node_id)
        node = graph.nodes[node_id]
        if node_status is None or node_id in containers or not node.formalizable:
            continue
        if node_status.key not in {"can_prove", "can_state"}:
            continue

        chapter_id = node.id.split("/", 1)[0] if "/" in node.id else ""
        chapter_page = (group_pages or {}).get(chapter_id)
        statement = links.get(node_id)
        graph_href = graph_pages.scope_page_href(destination, node.id, page, parent=node.parent)

        title = html.escape(node.title)
        heading = (
            f'<a href="{html.escape(statement, quote=True)}">{title}</a>' if statement else title
        )
        why = (
            "Its prerequisites are ready, so the proof can be written now."
            if node_status.key == "can_prove"
            else "Its prerequisites are ready, so the statement can be written down."
        )
        actions = [f'<a href="{html.escape(graph_href, quote=True)}">Dependencies</a>']
        if chapter_page is not None:
            chapter_title = _page_heading(chapter_page.read_text(encoding="utf-8")) or "chapter"
            href = _as_published(mermaid.relative_link(chapter_page, page, ".html"))
            actions.insert(
                0, f'<a href="{html.escape(href, quote=True)}">{html.escape(chapter_title)}</a>'
            )
        blockers = [
            graph.nodes[dep].title
            for dep in node.dependencies
            if statuses.get(dep) and statuses[dep].key not in {"fully_proved", "mathlib"}
        ]
        rests = (
            f'<div class="bp-next-rests">Rests on {html.escape(", ".join(blockers[:3]))}</div>'
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


STRUCTURE_PAGE = "structure.md"


def _render_structure_page(
    *,
    page: Path,
    entry: Path | None = None,
) -> str:
    """Keep the old structure route as a bounded doorway into the mathematical wiki.

    The explorer owns hierarchy now. Republishing every source path as one raw
    HTML tree made this compatibility page larger than the rest of a small
    site, duplicated the explorer, and became unusable on repository-scale books.
    """
    explorer = mermaid.relative_link(entry or page.parent / "dependencies.md", page, ".md")
    return "\n".join(
        [
            "---",
            "title: Browse the roadmap",
            "search:",
            "  exclude: true",
            "hide:",
            "  - toc",
            "---",
            "",
            "# Browse the roadmap",
            "",
            "The mathematical wiki presents the authored hierarchy and its mathematical",
            "relations without flattening the repository into a file listing.",
            "",
            f"[Open the mathematical wiki]({explorer})",
            "",
        ]
    )


def _render_summary_nav(
    book_pages: list[Path],
    *,
    destination: Path,
    overview: Path,
) -> str:
    """Write one mathematical site nav with the book's real chapters.

    mkdocs.yml cannot know a project's chapters, and hand-listing them would be
    the second navigation manifest the roadmap skill forbids. mkdocs-literate-nav
    reads this file instead, so the tabs come from the same page order the book
    itself uses.
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
    for page in book_pages:
        if page == overview:
            continue
        text = page.read_text(encoding="utf-8")
        named = _header_title(text)
        title = name_text(named) if named else title_text(_first_h1(text) or page.parent.name)
        depth = len(page.relative_to(destination).parts) - 1
        indent = "    " * max(depth, 1)
        lines.append(f"{indent}- [{title}]({page.relative_to(destination).as_posix()})")
    # What counts as done belongs beside the book it qualifies. The landing
    # page used to be the only route to it, which made the page impossible to
    # simplify without stranding it.
    coverage = destination / "coverage/README.md"
    if coverage.is_file():
        title = title_text(_first_h1(coverage.read_text(encoding="utf-8")) or "Coverage")
        lines.append(f"    - [{title}](coverage/README.md)")
    return "\n".join(lines) + "\n"

def _render_landing_page(
    text: str,
    *,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    coverage: CoverageSummary,
    groups: dict[str, list[str]],
    group_pages: dict[str, Path],
    page: Path,
    destination: Path,
    targets: dict[str, tuple[Path, str]] | None = None,
) -> str:
    """The landing page: what this is, how far it has got, and what is next.

    Its complete generated hero and next result remain reading content. The
    shared project graph shell is attached only after all pages are final,
    so landing pages and mathematical chapters use the same viewer once.
    """
    body = _document_body(text)
    heading = _first_h1(text) or ""
    title = rendered_visible_text(heading) or "Blueprint"
    hero = _render_hero(
        title,
        body,
        graph,
        statuses,
        coverage=coverage,
        coverage_href=_as_published(
            mermaid.relative_link(destination / coverage.source_path, page, ".html")
        ),
    )
    next_up = _next_target(
        graph,
        statuses,
        page=page,
        destination=destination,
        group_pages=group_pages,
        targets=targets,
    )
    front = [
        "---",
        # The hero sets the H1 itself, so the page title has to be declared
        # rather than discovered from the first Markdown heading. The theme
        # prints it as it is, so it is written as text.
        f"title: {yaml_string(title_text(heading) or 'Blueprint')}",
        # The sidebar would list this page alone, and the table of contents one
        # or two headings, so both give their width back.
        "hide:",
        "  - navigation",
        "  - toc",
    ]
    scopes = graph_views.group_nodes(graph)
    if scopes:
        book = graph_pages.book_contents(graph, scopes)
        # Without a roadmap contents page the book starts here.
        book_link = (
            ""
            if book == Path("README.md")
            else '<a class="bp-intro-book" href="'
            + html.escape(_as_published(mermaid.relative_link(destination / book, page, ".html")), quote=True)
            + '">Explore all mathematics</a>'
        )
        # The shared shell is attached after all full reading content is final.
        reading = f'<div class="bp-landing-intro">{hero}{book_link}{next_up}</div>'
        return "\n".join([*front, "  - footer", "---", "", reading]).rstrip() + "\n"
    # A roadmap without articles has nothing to explore, so the hero stands
    # over its status breakdown.
    breakdown = mermaid.render_legend(
        graph_views.scope_target_statuses(graph, statuses),
        graph_views.inventory_counts(graph, statuses),
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


def _coverage_summary(coverage: CoverageSummary) -> str:
    """Describe declared source dispositions without inventing a percentage."""

    counts = coverage.counts
    return " · ".join(
        f"{counts[disposition]} {disposition.casefold()}"
        for disposition in _COVERAGE_SUMMARY_ORDER
        if counts[disposition]
    )


def _render_hero(
    title: str,
    body: str,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    *,
    coverage: CoverageSummary,
    coverage_href: str,
) -> str:
    """Open with the project's name, its one-line claim, and where it stands.

    The dashboard this replaces was a paragraph of counts in a bordered box.
    The same numbers set as figures answer "how far along is this?" before the
    reader has finished the title, which is the only question the top of a
    blueprint has to answer.
    """
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
    target_label = "target" if total == 1 else "targets"

    figures = [
        ("Scoped roadmap", f"{share}%", f"{done} of {total} {target_label} complete", False),
        ("Ready now", str(actionable), "unblocked, waiting for an author", False),
    ]
    # The figure counts every catalog, checked or not; a project without
    # catalogs has no inventory to report.
    if inventory_count:
        figures.append(
            (
                "Module inventories",
                str(inventory_count),
                "existing Lean modules inventoried",
                True,
            )
        )
    figures.append(
        ("Chapters", str(len(graph_views.group_nodes(graph))), "top-level milestones", False)
    )
    stats = "".join(
        f'<div class="bp-figure{" bp-figure-neutral" if neutral else ""}">'
        f'<div class="bp-figure-value">{value}</div>'
        f'<div class="bp-figure-label">{html.escape(label)}</div>'
        f'<div class="bp-figure-note">{html.escape(note)}</div></div>'
        for label, value, note, neutral in figures
    )
    coverage_line = (
        '<div class="bp-hero-coverage">'
        '<span class="bp-hero-coverage-label">Declared source coverage:</span> '
        f'<span class="bp-hero-coverage-counts">{html.escape(_coverage_summary(coverage))}</span> '
        f'<a class="bp-hero-coverage-link" href="{html.escape(coverage_href, quote=True)}">'
        "View coverage contract</a>"
        "</div>"
    )
    lead = _lead_sentence(body)
    return (
        '<div class="bp-hero">'
        '<div class="bp-hero-rule"></div>'
        '<div class="bp-hero-kicker">Formalization blueprint</div>'
        f'<h1 class="bp-hero-title">{html.escape(title)}</h1>'
        + (f'<p class="bp-hero-lead">{lead}</p>' if lead else "")
        + f'<div class="bp-hero-figures">{stats}</div>'
        f'<div class="bp-hero-bar" aria-hidden="true">'
        f'<span style="width: {share}%"></span></div>'
        + coverage_line
        + "</div>"
    )


def _lead_sentence(body: str) -> str:
    """The blueprint's own opening sentence, for the hero.

    Written by a human in `blueprint/README.md`, so it says what the project is
    far better than anything derived from the graph could. Prose only: a
    heading or a list is structure rather than a claim.
    """
    for block in body.split("\n\n"):
        line = block.strip()
        if not line or line.startswith(("#", "-", "*", "<", "|", ">", "!")):
            continue
        sentence = line.split(". ")[0].rstrip(".").replace("\n", " ")
        # The hero is raw HTML, so Markdown never runs over it and a title in
        # *emphasis* would otherwise show its asterisks. Escaping first means
        # the substitutions below can only ever produce these two tags.
        safe = html.escape(sentence)
        safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
        safe = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", safe)
        safe = re.sub(r"`([^`]+)`", r"<code>\1</code>", safe)
        return safe + "."
    return ""

def _render_overview_summary(
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    *,
    containers: frozenset[str],
    node_ids: list[str] | None = None,
) -> str:
    """Render the compact, honest progress strip shown at the start of the book."""
    candidates = list(node_ids if node_ids is not None else graph.nodes)
    selected_ids = [
        node_id
        for node_id in candidates
        if _is_countable(graph, node_id, containers)
    ]
    inventory_count = len(
        _module_inventories(graph, containers=containers, node_ids=candidates)
    )
    definitions = sum(is_definition(graph.nodes[node_id]) for node_id in selected_ids)
    results = len(selected_ids) - definitions
    item_parts = []
    if definitions:
        item_parts.append(f"{definitions} definition{'s' if definitions != 1 else ''}")
    if results:
        item_parts.append(f"{results} result{'s' if results != 1 else ''}")
    if inventory_count:
        item_parts.append(
            f"{inventory_count} module "
            f"{'inventory' if inventory_count == 1 else 'inventories'}"
        )
    item_summary = " · ".join(item_parts) or "No decomposed definitions or results yet"

    state_parts = []
    selected_statuses = {node_id: statuses[node_id] for node_id in selected_ids}
    for state, count in status.summarize(selected_statuses):
        state_parts.append(
            f'<span class="bp-progress-state"><span class="bp-swatch '
            f'bp-swatch-{state.key}"></span><strong>{count}</strong> '
            f"{html.escape(state.label)}</span>"
        )
    states = "".join(state_parts)
    return (
        '<div class="bp-progress-overview">'
        '<div class="bp-progress-kicker">Formalization progress</div>'
        f'<div class="bp-progress-total">{item_summary}</div>'
        f'<div class="bp-progress-states">{states}</div>'
        "</div>"
    )


def _page_heading(text: str) -> str | None:
    """The title the page ``text`` is published under, as text: its header's,
    which a page shown as typed keeps, or else its first H1."""

    return _header_title(text) or _first_h1(text)


def _first_h1(text: str) -> str | None:
    for line in mask_fences_and_comments(text.splitlines()):
        heading = _HEADING.match(line)
        if heading is not None and len(heading.group(1)) == 1:
            return heading.group(2).strip()
    return None


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


def _anchor(node_id: str, group: str) -> str:
    """A stable in-page anchor for a node, unique within its chapter."""
    prefix = f"{group}/" if group else ""
    remainder = node_id[len(prefix) :] if prefix and node_id.startswith(prefix) else node_id
    return remainder.replace("/", "-")


def _anchored_links(
    targets: dict[str, tuple[Path, str]],
    page: Path,
    *,
    extension: str = ".html",
) -> Mapping[str, str]:
    """Link every node to its statement on the published chapter page.

    Use ``.md`` for links MkDocs will parse -- it validates and rewrites those
    itself -- and ``.html`` for raw HTML and Mermaid, which it never sees. A
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
                self._resolved_page = self._page.resolve()
            if target.resolve() == self._resolved_page:
                href = ""
            else:
                href = mermaid.relative_link(target, self._page, self._extension)
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


def _as_published(href: str) -> str:
    """MkDocs serves ``README.md`` as ``index.html``.

    Links it parses are rewritten for us, but raw HTML and Mermaid fences never
    reach it, so those have to name the published file themselves.
    """
    # As a string, so a leading ./ that keeps a colon from reading as a scheme stays.
    head, _slash, name = href.rpartition("/")
    if name.lower() in {"readme.html", "index.html"}:
        return f"{head}/index.html" if head else "index.html"
    return href


def _link_mask(line: str) -> str:
    """*line* with its code spans, inline math and bracket escapes blanked.

    The site reads code and math before links and an escaped bracket as text,
    as ``markdown.link_targets`` does for check. Blanking keeps every offset,
    so a link matched in the mask reads its label and target back from *line*.
    """
    return _BRACKET_ESCAPE.sub(_blank, _INLINE_MATH.sub(_blank, INLINE_CODE.sub(_blank, line)))


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
) -> str:
    """Resolve a page's relative links against where it is being published.

    Three things move. Node files stop being pages once their chapter absorbs
    them, so a link naming one becomes an anchor. A node's body is hoisted out
    of its own directory onto the chapter page, so its remaining relative links
    have to be recomputed from there. And source notes are not published at
    all when *sources_base* says where to reach them in the repository.

    The site holds only the blueprint, so an inline link to a file outside it,
    such as a source note's evidence link to a Lean file, keeps its text and
    loses its target instead of dangling.
    """
    # A coverage page can name most of the graph, so one mapping serves the
    # whole text and links each target page it reaches once.
    links = _anchored_links(targets, page, extension=".md")

    def moved_target(raw: str) -> str | None:
        """Where *raw* should point once published, or None to leave it alone."""
        bare = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        # The query and fragment are not part of the file's name: they are
        # kept as written, beside the path render moves.
        located, separator, fragment = bare.partition("#")
        path, asked, query = located.partition("?")
        try:
            if not path or urlsplit(path).scheme or path.startswith("/"):
                return None
            candidate = (source_dir / unquote(path)).resolve()
        except ValueError:
            # An unbalanced IPv6 bracket or a decoded NUL names no file to
            # point at, so the link is published as written, like a URL.
            return None
        # Capture leaves out hidden paths and unpublished folders, so the site has
        # no page there, though check's graph knows the articles in such a folder.
        # A hidden source note is reached through the repository instead.
        inside = candidate.relative_to(blueprint) if _is_within(candidate, blueprint) else None
        if inside is not None and (
            any(_is_within(candidate, folder) for folder in unpublished)
            or (_is_hidden(inside) and inside.parts[:1] != (SOURCES_DIR,))
        ):
            unlinked.add(raw)
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
        if not _is_within(candidate, blueprint):
            # Nothing outside the blueprint is published: "" keeps only the text.
            return ""
        if candidate in left_out:
            # Nor is a file the site leaves out; its image or definition goes too.
            unlinked.add(raw)
            return ""
        relative = candidate.relative_to(blueprint)
        # atlas.json stays off the site, so a link to it keeps only its text.
        if relative.as_posix().casefold() == "atlas.json":
            return ""
        # A repository link to a note the captured blueprint lacks would 404, so
        # it stays site-relative, where a strict build reports the page missing.
        # The capture skips hidden files, so a hidden note keeps its repository
        # link, which resolves once the note is committed, as check warns.
        if (
            sources_base is not None
            and relative.parts[:1] == (SOURCES_DIR,)
            and (os.path.exists(candidate) or _is_hidden(relative))
        ):
            return f"{_source_href(sources_base, relative.parts[1:])}{asked}{query}{separator}{fragment}"
        published = destination / relative
        # Mermaid URLs are percent-encoded because they sit in generated
        # JavaScript. This is Markdown: keep its decoded spelling so an
        # unchanged link stays byte-for-byte unchanged, and ``bracketed`` can
        # protect a moved path containing a space or parenthesis.
        href = unquote(mermaid.relative_link(published, page, candidate.suffix))
        # Decoded, a first segment that holds a colon, such as a folder named
        # ``javascript:x``, would read as the URL's scheme.
        if ":" in href.split("/", 1)[0]:
            href = f"./{href}"
        return f"{href}{asked}{query}{separator}{fragment}"

    unlinked: set[str] = set()

    def bracketed(href: str) -> str:
        # A decoded path, or a query or fragment kept from angle brackets, can
        # hold a space or a parenthesis. CommonMark allows no space in a bare
        # destination, and Python-Markdown needs its parentheses balanced.
        return f"<{href}>" if re.search(r"[\s()]", href) else href

    def rewrite(line: str) -> str:
        # A reference definition carries the destination for every `[x][label]`
        # on the page. Rewriting only inline links leaves it pointing at the
        # unpublished path and the rendered link dangles.
        definition = _LINK_DEFINITION.match(line)
        if definition is not None:
            href = moved_target(definition.group("target"))
            if href == "" and definition.group("target") in unlinked:
                return ""
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
        for match in _MARKDOWN_IMAGE.finditer(_link_mask(line)):
            raw = line[match.start("target") : match.end("target")]
            if moved_target(raw) == "" and raw in unlinked:
                pieces += [line[end : match.start()], line[match.start("label") : match.end("label")]]
                end = match.end()
        line = "".join(pieces) + line[end:]
        pieces = []
        end = 0
        for match in _MARKDOWN_LINK.finditer(_link_mask(line)):
            raw = line[match.start("target") : match.end("target")]
            href = moved_target(raw)
            if href is None or raw in (href, f"<{href}>"):  # nothing moved: keep the link as written
                continue
            label = line[match.start("label") : match.end("label")]
            pieces += [line[end : match.start()], f"[{label}]({bracketed(href)})" if href else label]
            end = match.end()
        return "".join(pieces) + line[end:]

    return _outside_fences(text, rewrite)


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


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


def _number_nodes(graph: Graph) -> dict[str, str]:
    """Number nodes per declaration kind in dependency order, as a blueprint does."""
    counters: dict[str, int] = {}
    numbers: dict[str, str] = {}
    for node_id in status.topological_order(graph):
        label = _declaration_label(graph.nodes[node_id])
        counters[label] = counters.get(label, 0) + 1
        numbers[node_id] = f"{label} {counters[label]}"
    return numbers


def _declaration_label(node: Node) -> str:
    if node.catalog == "module":
        return "Module"
    return DECLARATION_LABELS.get((node.declaration or "").casefold(), "Node")


def _reverse_edges(graph: Graph) -> dict[str, list[str]]:
    used_by: dict[str, list[str]] = {node_id: [] for node_id in graph.nodes}
    for node in graph.nodes.values():
        for dependency in node.dependencies:
            used_by[dependency].append(node.id)
    return {key: sorted(value) for key, value in used_by.items()}


def _render_chapter(
    group: str,
    node_ids: list[str],
    *,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    numbers: dict[str, str],
    used_by: dict[str, list[str]],
    linker: SourceLinker,
    page: Path,
    targets: dict[str, tuple[Path, str]],
    narrative: str | None,
    blueprint: Path,
    repo_root: Path,
    destination: Path,
    node_sources: dict[Path, str],
    containers: frozenset[str],
    statements: dict[str, tuple[str, str]],
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
            numbers=numbers,
            used_by=used_by,
            linker=linker,
            links=links,
            page=page,
            repo_root=repo_root,
            destination=destination,
            statements=statements,
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
    chapter = _place_environments(narrative, lead, slots, summary=chapter_summary, environments=environments)
    placed = set(slots.values())
    remaining = [node_id for node_id in node_ids if node_id not in placed]
    # A catalog inventories Lean that already exists, so it is no formalization target.
    for heading, catalog in (("Additional formalization targets", None), ("Module inventories", "module")):
        section = [environments[node_id] for node_id in remaining if graph.nodes[node_id].catalog == catalog]
        if section:
            chapter = chapter.rstrip() + f"\n\n## {heading}\n\n" + "\n\n".join(section)
    return chapter.rstrip() + "\n", linked, unresolved


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
                node_id = node_sources.get((source_dir / unquote(path)).resolve())
        except ValueError:  # an unbalanced IPv6 bracket or a decoded NUL names no article
            node_id = None
        if node_id is None or node_id not in slotted or node_id in slots.values():
            continue
        slots[index] = node_id
    return lead, slots


def _place_environments(
    narrative: str, lead: int, slots: dict[int, str], *, summary: str, environments: dict[str, str]
) -> str:
    """The chapter page: ``narrative`` with the progress ``summary`` before
    line ``lead`` and each slot line replaced by its statement's environment.
    Each environment is one HTML block, which ends any paragraph, list, or
    span it meets, so each stretch of the narrative reads as check read it."""

    lines = narrative.splitlines()
    output: list[str] = []
    for index, line in enumerate(lines):
        if index == lead:
            output.extend(["", summary.rstrip(), ""])
        if index in slots:
            output.append(environments[slots[index]])
        else:
            output.append(line)
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


class _FenceSafeHtml(HTMLParser):
    """Keep displayed fence markers from opening fences on the second pass.

    HTML entities decode to the exact displayed code/TeX. Raw script/style
    text is not entity-decoded by HTML, so it retains its original spelling.
    """

    _runs = re.compile(r"`{3,}|~{3,}")
    _quoted = re.compile(r"(['\"])(.*?)\1", re.DOTALL)

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    @classmethod
    def escaped(cls, value: str) -> str:
        return cls._runs.sub(lambda match: "".join(f"&#{ord(char)};" for char in match.group()), value)

    def handle_starttag(self, tag, attrs):
        value = self.get_starttag_text()
        # Quoted attribute values also decode entities; names are untouched.
        self.parts.append(self._quoted.sub(
            lambda match: match.group(1) + self.escaped(match.group(2)) + match.group(1), value
        ))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data if self.cdata_elem else self.escaped(data))

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")

    def unknown_decl(self, data):
        self.parts.append(f"<![{data}{']]>' if data.startswith('CDATA[') else ']>'}")


class _PublishedBodyLinks(Treeprocessor):
    """Give parsed Markdown links their final URLs before they become HTML.

    MkDocs does not rewrite links inside raw HTML. Authored raw HTML links are
    not Markdown tree elements, so their spelling remains untouched.
    """

    def __init__(self, md):
        super().__init__(md)
        self.validation_targets: list[tuple[str, str]] = []

    def run(self, root):
        for element in root.iter():
            if element.tag not in {"a", "img"}:
                continue
            key = "href" if element.tag == "a" else "src"
            href = element.get(key, "")
            try:
                parts = urlsplit(href)
            except ValueError:
                self.validation_targets.append((element.tag, href))
                continue
            if not parts.scheme and not parts.netloc:
                # Keep the original parsed link/image and .md destination for
                # MkDocs' configured missing-file/anchor checks on the final
                # page. Raw authored HTML links are not tree elements here.
                self.validation_targets.append((element.tag, href))
                if (element.tag == "a" and not parts.path.startswith("/")
                        and parts.path.lower().endswith(".md") and "\0" not in unquote(parts.path)):
                    path = _as_published(parts.path[:-3] + ".html")
                    element.set("href", urlunsplit(parts._replace(path=path)))


def _body_converter():
    converter = site_converter()
    # The final page assigns heading IDs once, across all its statements.
    # attr_list still keeps an author's explicit {#id} here.
    converter.treeprocessors.deregister("toc")
    converter.treeprocessors.register(_PublishedBodyLinks(converter), "published-body-links", 5)
    return converter


def _protect_markdown_in_html(
    text: str, *, references: Mapping[str, tuple[str, str]] | None = None, force: bool = False,
) -> str:
    """Parse an affected authored chunk before introducing HTML containers.

    HTML extraction precedes arithmatex and indented code. Inside markdown=1,
    r<n can consume the container's closing tag and lose its whole suffix.
    Parsing the fragment first lets the configured Markdown engine distinguish
    code, math, HTML and autolinks and emit the correct escaping for each.
    The vault and generated wrapper/navigation markup are never rewritten.
    """
    if not force and "<" not in text:
        return text
    converter = _body_converter()
    if references:
        converter.references.update(references)
    rendered = converter.convert(text)
    if "```" in rendered or "~~~" in rendered:
        protected = _FenceSafeHtml()
        protected.feed(rendered)
        protected.close()
        rendered = "".join(protected.parts)
    targets = converter.treeprocessors["published-body-links"].validation_targets
    if targets:
        # The visible fragment is raw HTML, which MkDocs does not validate.
        # Native Markdown witnesses retain the original validation behavior,
        # including images, decoded paths, queries, fragments and warning
        # levels. A hidden subtree prevents extra layout or keyboard targets;
        # whitespace-only labels add no visible text.
        witnesses = []
        for kind, href in dict.fromkeys(targets):
            target = f"<{href}>" if "<" not in href and ">" not in href else href
            witnesses.append(f"{'!' if kind == 'img' else ''}[ ]({target})")
        rendered += '\n\n<div class="bp-body-link-validation" hidden markdown="1">\n\n'
        rendered += "\n\n".join(witnesses) + "\n\n</div>"
    return rendered


def _render_environment(
    node: Node,
    *,
    anchor: str,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    numbers: dict[str, str],
    used_by: dict[str, list[str]],
    linker: SourceLinker,
    links: Mapping[str, str],
    page: Path,
    repo_root: Path,
    destination: Path,
    statements: dict[str, tuple[str, str]],
) -> tuple[str, int, list[str]]:
    node_status = statuses[node.id]
    caption, _, number = numbers[node.id].rpartition(" ")
    # The statement as checked: the text the graph parsed, with its links
    # resolved from the chapter page it is published on.
    statement, remainder = statements[node.id]

    # References in either chunk may be used by the other. If one chunk is
    # preconverted, render both with the shared article context: otherwise its
    # consumed definitions would leave the remaining Markdown links unresolved.
    references = {}
    affected = converted_alone(statement, remainder)
    if affected:
        context = _body_converter()
        context.convert("\n\n".join((statement, remainder)))
        references = context.references
    statement, remainder = (
        _protect_markdown_in_html(part, references=references, force=affected) for part in (statement, remainder)
    )

    code_links, implementation_rows, linked, unresolved = _lean_presentation(node, linker)
    source_link = _vault_source_link(node, repo_root=repo_root, linker=linker)
    meta_rows = implementation_rows
    if node_status.key == "conditional":
        # A conditional proof must never read as finished, so the open
        # statements it rests on are named on the statement itself.
        assumed = _node_references(
            node_status.assumes, graph=graph, statuses=statuses, numbers=numbers, links=links
        )
        meta_rows.append(("Assumes", f"{assumed} (open statements without a recorded Lean proof)"))
    if node.discussion:
        meta_rows.append(("Discussion", _discussion_link(node.discussion, linker)))
    meta = _render_rows(meta_rows, css_class="bp-meta")
    dependencies = _dependency_disclosure(
        node,
        graph=graph,
        statuses=statuses,
        numbers=numbers,
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
    # A checked inventory is fully proved, so it keeps the tick.
    mark = "✓" if node_status.key in {"fully_proved", "mathlib"} else "●"

    lines = [
        f'<div class="bp-thmwrapper {style} bp-{display_key}" '
        f'id="{html.escape(anchor, quote=True)}" '
        f'data-autoform-node-id="{html.escape(node.id, quote=True)}" markdown="1">',
        '<div class="bp-thmheading">',
        f'<span class="bp-thmcaption">{html.escape(caption)}</span>'
        f'<span class="bp-thmlabel">{html.escape(number)}</span>'
        f'<span class="bp-thmtitle">{html.escape(node.title)}</span>'
        f"{code_links}"
        f"{source_link}"
        f'<a class="bp-permalink" href="#{html.escape(anchor, quote=True)}">#</a>'
        f'<span class="bp-mark" title="{html.escape(display_label, quote=True)}">'
        f'{mark}<span class="bp-mark-label">{html.escape(display_label)}</span></span>',
        "</div>",
        *boxed(statement, STATEMENT_BOX),
    ]
    if remainder:
        lines.extend(boxed(remainder, NOTES_BOX))
    if meta:
        lines.append(meta)
    if dependencies:
        lines.append(dependencies)
    lines.append("</div>")
    return "\n".join(lines), linked, unresolved


def _lean_presentation(
    node: Node,
    linker: SourceLinker,
) -> tuple[str, list[tuple[str, str]], int, list[str]]:
    """Render direct source icons, falling back to quiet diagnostic metadata.

    A catalog can list hundreds of names, so it gets no icons: its Lean row
    links the first few by name, as its explorer node shows them, and counts
    the rest, which its declaration ledger lists.
    """
    icons: list[str] = []
    fallback: list[str] = []
    linked = 0
    unresolved: list[str] = []
    if not node.lean:
        return "", [], linked, unresolved

    for name in declaration_names(node.lean):
        url = linker.url(name)
        location = linker.location(name)
        code = f"<code>{html.escape(name)}</code>"
        if not linker.indexed:
            # The sources were not read, so "not found" would be false.
            fallback.append(code)
        elif url and node.catalog == "module":
            fallback.append(f'<a href="{html.escape(url, quote=True)}">{code}</a>')
            linked += 1
        elif url:
            label = html.escape(f"View {name} in Lean source", quote=True)
            icons.append(
                f'<a class="bp-code-link" href="{html.escape(url, quote=True)}" '
                f'aria-label="{label}" title="{label}">{_code_icon()}</a>'
            )
            linked += 1
        elif location is not None:
            fallback.append(f'{code} <span class="bp-where">{html.escape(location.path.as_posix())}</span>')
            linked += 1
        elif ambiguous := linker.index.ambiguity(name):
            fallback.append(f'{code} <span class="bp-missing">defined more than once in the Lean sources</span>')
            unresolved.append(
                f"declaration defined more than once in the Lean sources: {node.id}: {name} ({ambiguous})"
            )
        else:
            fallback.append(f'{code} <span class="bp-missing">not found in the Lean sources</span>')
            unresolved.append(f"declaration not found in the Lean sources: {node.id}: {name}")

    code_links = f'<span class="bp-code-links">{"".join(icons)}</span>' if icons else ""
    shown = fallback[: dag_viewer._CATALOG_LEAN_NAMES] if node.catalog == "module" else fallback
    rest = len(fallback) - len(shown)
    more = f" and {rest} more, listed in its declaration ledger" if rest else ""
    rows = [("Lean", " · ".join(shown) + more)] if fallback else []
    return code_links, rows, linked, unresolved


def _code_icon() -> str:
    """A small dependency-free code icon suitable at theorem-heading size."""
    return (
        '<svg class="bp-code-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        '<path d="m8 9-3 3 3 3M16 9l3 3-3 3M14 5l-4 14"/>'
        "</svg>"
    )


def _vault_source_link(node: Node, *, repo_root: Path, linker) -> str:
    """Link a statement to the Markdown article it was authored in.

    The graph view and the published statement are both derived. This is the
    file a reader edits, so it is worth one click from the statement itself.
    """
    if not linker.repository_url or not linker.ref or not linker.links_blueprint:
        return ""
    try:
        relative = node.path.resolve().relative_to(repo_root).as_posix()
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
    numbers: dict[str, str],
    used_by: dict[str, list[str]],
    links: Mapping[str, str],
) -> str:
    """Hide DAG relations behind a native, keyboard-accessible disclosure."""
    rows: list[tuple[str, str]] = []

    def references(node_ids: list[str] | tuple[str, ...]) -> str:
        return _node_references(node_ids, graph=graph, statuses=statuses, numbers=numbers, links=links)

    if node.statement_dependencies:
        rows.append(("Statement uses", references(node.statement_dependencies)))
    proof_only = [other for other in node.proof_dependencies if other not in node.statement_dependencies]
    if proof_only:
        rows.append(("Proof uses", references(proof_only)))
    if used_by[node.id]:
        rows.append(("Used by", references(used_by[node.id])))
    if not rows:
        return ""

    body = _render_rows(rows, css_class="bp-dependency-body")
    return f'<details class="bp-dependencies"><summary>Dependencies</summary>{body}</details>'


def _node_references(
    node_ids: list[str] | tuple[str, ...],
    *,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    numbers: dict[str, str],
    links: Mapping[str, str],
) -> str:
    """Link each node by number and title, coloured by its derived state.

    A catalog is coloured as neutrally as its own environment, checked or not.
    """
    rendered = []
    for other_id in node_ids:
        other = graph.nodes[other_id]
        label = html.escape(f"{numbers[other_id]} ({other.title})")
        key = graph_views.display_status_key(other, statuses[other_id])
        rendered.append(
            f'<a class="bp-ref bp-ref-{key}" '
            f'href="{html.escape(links[other_id], quote=True)}">{label}</a>'
        )
    return " · ".join(rendered)


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


def _stylesheet() -> str:
    """Blueprint styling: amsthm structure over Facebook's product surfaces.

    Both schemes are Facebook's shipped greys with Meta blue and the brand
    gradient, set in Plus Jakarta Sans with JetBrains Mono for code. Dark is not
    light dimmed: it is the palette Facebook uses in dark mode, and it hangs off
    Material's colour scheme attribute so the theme's own toggle drives it.
    """
    light = "\n".join(
        f".bp-{state.key} .bp-mark {{ color: {state.stroke}; }}\n"
        f".bp-{state.key} > .bp-thmcontent {{ border-left-color: {state.stroke}; }}\n"
        f".bp-ref-{state.key}::before, .bp-swatch-{state.key} {{ background: {state.fill}; border-color: {state.stroke}; }}\n"
        f".mermaid g.node.{state.key} > rect, .mermaid g.node.{state.key} > path, "
        f".mermaid g.node.{state.key} > polygon "
        f"{{ fill: {state.fill}; stroke: {state.stroke}; }}"
        for state in status.STATES
    )
    dark = "\n".join(
        f"[data-md-color-scheme=slate] .bp-{state.key} .bp-mark {{ color: {state.dark_stroke}; }}\n"
        f"[data-md-color-scheme=slate] .bp-{state.key} > .bp-thmcontent "
        f"{{ border-left-color: {state.dark_stroke}; }}\n"
        f"[data-md-color-scheme=slate] .bp-ref-{state.key}::before, "
        f"[data-md-color-scheme=slate] .bp-swatch-{state.key} "
        f"{{ background: {state.dark_fill}; border-color: {state.dark_stroke}; }}\n"
        f"[data-md-color-scheme=slate] .mermaid g.node.{state.key} > rect, "
        f"[data-md-color-scheme=slate] .mermaid g.node.{state.key} > path, "
        f"[data-md-color-scheme=slate] .mermaid g.node.{state.key} > polygon "
        f"{{ fill: {state.dark_fill} !important; stroke: {state.dark_stroke} !important; }}\n"
        f"[data-md-color-scheme=slate] .mermaid g.node.{state.key} .nodeLabel, "
        f"[data-md-color-scheme=slate] .mermaid g.node.{state.key} .nodeLabel p "
        f"{{ color: {state.dark_text} !important; fill: {state.dark_text} !important; }}"
        for state in status.STATES
    )
    # The legends beside the explorer's rings list module inventories, so
    # their swatches take the colours the explorer draws inventories in.
    inventory = "\n".join(
        f".bp-swatch-{state.key} {{ background: {state.fill}; border-color: {state.stroke}; }}\n"
        f"[data-md-color-scheme=slate] .bp-swatch-{state.key} "
        f"{{ background: {state.dark_fill}; border-color: {state.dark_stroke}; }}"
        for state in status.INVENTORY_STATES
    )
    serif = '"Plus Jakarta Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
    sans = '"Plus Jakarta Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
    mono = '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace'
    return f"""/* Generated by autoform render. Edits are overwritten. */
/* Material requests 300/400/700 only, and the display sizes here are set in
   600 and 800. Without this the browser synthesises them, which on a
   geometric face reads as smeared rather than bold. */
@import url("https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap");

/* Facebook's product surfaces, not Material's defaults: the greys are the
   ones Facebook ships in dark mode, the blue is Meta blue, and the accent
   sweep is the brand gradient. A blueprint should look like it came from
   here rather than from any documentation generator. */
:root {{
  --bp-fg: #050505;
  --bp-muted: #65676B;
  --bp-rule: #CED0D4;
  --bp-surface: #FFFFFF;
  --bp-sunken: #F0F2F5;
  --bp-link: #0064E0;
  --bp-link-hover: #0082FB;
  --bp-blue: #0064E0;
  --bp-violet: #7B3FE4;
  --bp-magenta: #E0447B;
  --bp-sweep: linear-gradient(95deg, #0082FB 0%, #0064E0 32%, #7B3FE4 68%, #E0447B 100%);
  --bp-radius: 12px;
}}
[data-md-color-scheme=slate] {{
  --bp-fg: #E4E6EB;
  --bp-muted: #B0B3B8;
  --bp-rule: #3E4042;
  --bp-surface: #242526;
  --bp-sunken: #1C1D1F;
  --bp-link: #2D88FF;
  --bp-link-hover: #7FB8FF;
  --bp-blue: #2D88FF;
  --bp-violet: #9B6BFF;
  --bp-magenta: #FF6B9D;
}}

body {{ font-family: {sans}; color: var(--bp-fg); }}
.md-typeset {{
  max-width: 72ch;
  font-family: {sans};
  line-height: 1.7;
  font-size: 0.8rem;
}}
/* One family throughout, separated by weight rather than by a second face.
   Optimistic, Meta's own, is not public; Plus Jakarta Sans is the nearest
   geometric humanist with the range to carry both display and body. */
h1, h2, h3, h4, h5, h6 {{ font-family: {sans}; font-weight: 700; letter-spacing: -0.021em; }}
.md-typeset h1 {{ font-weight: 800; letter-spacing: -0.032em; }}
code, kbd, pre, samp {{ font-family: {mono}; }}
code, pre {{ background: var(--bp-sunken); border-radius: 6px; }}
a, a:visited {{ color: var(--bp-link); }}
a:hover, a:visited:hover {{ color: var(--bp-link-hover); text-decoration: underline; }}

[data-md-color-scheme=slate] body {{ background-color: #18191A; }}
[data-md-color-scheme=slate] .md-main,
[data-md-color-scheme=slate] .md-container {{ background-color: #18191A; }}
[data-md-color-scheme=slate] pre, [data-md-color-scheme=slate] code {{ color: var(--bp-fg); }}

/* The brand sweep as a hairline under the header ties every page together
   without spending any vertical space on decoration. */
.md-header {{ box-shadow: none; border-bottom: 1px solid var(--bp-rule); }}
.md-header::after {{
  content: "";
  display: block;
  height: 3px;
  background: var(--bp-sweep);
}}
[data-md-color-scheme=slate] .md-header,
[data-md-color-scheme=slate] .md-tabs {{ background-color: #242526; }}
.md-tabs {{ border-bottom: 1px solid var(--bp-rule); }}
.md-tabs__link {{ font-weight: 600; opacity: 0.72; }}
.md-tabs__link--active, .md-tabs__link:hover {{ opacity: 1; }}

/* The landing page is a dashboard, not a chapter, so it drops the reading
   column the rest of the book keeps. :has() is how a single generated
   stylesheet can tell the two apart without a second page template; where it
   is unsupported the page simply stays at reading width. */
.md-typeset:has(.bp-landing) {{ max-width: 62rem; }}

.bp-hero {{ margin: 0 0 2.5rem; }}
.bp-hero-rule {{
  height: 4px;
  width: 84px;
  border-radius: 4px;
  background: var(--bp-sweep);
}}
.bp-hero-kicker {{
  margin-top: 1.1rem;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--bp-muted);
}}
.md-typeset .bp-hero-title {{
  margin: 0.35rem 0 0;
  font-size: 2.6rem;
  line-height: 1.08;
  font-weight: 800;
  letter-spacing: -0.035em;
}}
.md-typeset .bp-hero-lead {{
  margin: 0.9rem 0 0;
  max-width: 54ch;
  font-size: 1.05rem;
  line-height: 1.55;
  color: var(--bp-muted);
}}

/* Counts set as figures. The reader should get "how far along?" from the
   shape of the numbers before reading any of the words around them. */
.bp-hero-figures {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 1px;
  margin-top: 2rem;
  background: var(--bp-rule);
  border: 1px solid var(--bp-rule);
  border-radius: var(--bp-radius);
  overflow: hidden;
}}
.bp-figure {{ padding: 1.1rem 1.25rem; background: var(--bp-surface); }}
.bp-figure-value {{
  font-size: 2.1rem;
  font-weight: 800;
  line-height: 1;
  letter-spacing: -0.04em;
  background: var(--bp-sweep);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}}
.bp-figure-neutral .bp-figure-value {{
  background: none;
  color: var(--bp-muted);
}}
.bp-figure-label {{
  margin-top: 0.45rem;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.01em;
}}
.bp-figure-note {{ margin-top: 0.15rem; font-size: 0.7rem; color: var(--bp-muted); }}

.bp-hero-bar {{
  margin-top: 1rem;
  height: 6px;
  border-radius: 6px;
  background: var(--bp-sunken);
  border: 1px solid var(--bp-rule);
  overflow: hidden;
}}
.bp-hero-bar > span {{ display: block; height: 100%; background: var(--bp-sweep); }}

.bp-hero-coverage {{
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.2rem 0.65rem;
  margin-top: 0.75rem;
  font-size: 0.72rem;
  color: var(--bp-muted);
}}
.bp-hero-coverage-label {{ font-weight: 700; color: var(--bp-fg); }}
.bp-hero-coverage-link {{
  margin-left: auto;
  font-weight: 600;
  text-decoration: underline;
  text-underline-offset: 0.15em;
}}

/* On the landing page the hero opens the explorer's hierarchy rail, or its
   details sheet on phones, so it is set to the rail's width. Material's
   typeset rules reach the hero's heading, paragraph and links there, so these
   rules carry a doubled class; the explorer's own rules stay in its script. */
.bp-landing-intro.bp-landing-intro {{ margin: 0 .35rem 1.1rem; }}
.bp-landing-intro.bp-landing-intro .bp-hero {{ margin: 0; }}
.bp-landing-intro.bp-landing-intro .bp-hero-rule {{ width: 56px; height: 3px; }}
.bp-landing-intro.bp-landing-intro .bp-hero-kicker {{ margin-top: 0.8rem; font-size: 0.6rem; }}
.bp-landing-intro.bp-landing-intro .bp-hero-title {{
  margin: 0.3rem 0 0;
  color: var(--bp-fg);
  font-size: 1.5rem;
  font-weight: 800;
  letter-spacing: -0.025em;
  line-height: 1.12;
}}
.bp-landing-intro.bp-landing-intro .bp-hero-lead {{
  max-width: none;
  margin: 0.6rem 0 0;
  font-size: 0.8rem;
  line-height: 1.5;
}}
.bp-landing-intro.bp-landing-intro .bp-hero-figures {{
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin-top: 0.9rem;
}}
.bp-landing-intro.bp-landing-intro .bp-figure {{ padding: 0.55rem 0.6rem; }}
.bp-landing-intro.bp-landing-intro .bp-figure:last-child:nth-child(odd) {{ grid-column: 1 / -1; }}
.bp-landing-intro.bp-landing-intro .bp-figure-value {{ font-size: 1.35rem; }}
.bp-landing-intro.bp-landing-intro .bp-figure-label {{ margin-top: 0.3rem; font-size: 0.66rem; }}
.bp-landing-intro.bp-landing-intro .bp-figure-note {{ font-size: 0.6rem; }}
.bp-landing-intro.bp-landing-intro .bp-hero-bar {{ margin-top: 0.7rem; }}
.bp-landing-intro.bp-landing-intro .bp-hero-coverage {{ margin-top: 0.6rem; font-size: 0.64rem; }}
.bp-landing-intro.bp-landing-intro .bp-intro-book {{
  display: inline-block;
  margin-top: 0.85rem;
  color: var(--bp-link);
  font-size: 0.72rem;
  font-weight: 700;
  text-decoration: none;
}}
.bp-intro-book::after {{ content: " →"; }}
.bp-landing-intro.bp-landing-intro .bp-next-target {{ margin: 0.9rem 0 0; padding: 0.6rem 0.7rem; }}
.bp-landing-intro.bp-landing-intro .bp-next-title {{ font-size: 0.95rem; }}
.bp-landing-intro.bp-landing-intro .bp-next-why,
.bp-landing-intro.bp-landing-intro .bp-next-rests,
.bp-landing-intro.bp-landing-intro .bp-next-actions {{ font-size: 0.7rem; }}
.bp-landing-intro.bp-landing-intro .bp-live-activity {{ margin: 0.9rem 0 0; padding: 0.6rem 0.7rem; }}
.bp-landing-intro.bp-landing-intro .bp-live-row {{ font-size: 0.7rem; }}

/* One grid for the whole legend. The cells are emitted in reading order with
   no row wrapper, so every swatch, label and count sits on the same axis
   however long the text beside it runs. */
.bp-legend-grid {{
  display: grid;
  grid-template-columns: auto auto auto minmax(0, 1fr);
  align-items: baseline;
  gap: 0.5rem 0.9rem;
  font-size: 0.72rem;
}}
.bp-legend-label {{ font-weight: 700; white-space: nowrap; }}
.bp-legend-count {{
  justify-self: end;
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  color: var(--bp-muted);
}}
.bp-legend-meaning {{ color: var(--bp-muted); }}
.md-typeset .bp-legend-grid .bp-swatch {{ align-self: center; }}

/* The book opens with a compact progress summary. It reports only decomposed
   blueprint items, leaving source-wide coverage to the dedicated page. */
.bp-progress-overview {{
  margin: 1.5rem 0 2rem;
  font-family: {sans};
  padding: 1rem 1.15rem;
  border: 1px solid var(--bp-rule);
  border-radius: 6px;
  background: var(--bp-surface);
}}
.bp-progress-kicker {{
  font-family: {mono};
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--bp-muted);
}}
.bp-progress-total {{ margin-top: 0.2rem; font-family: {serif}; font-size: 1.05rem; }}
.bp-progress-states {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem 1rem;
  margin-top: 0.55rem;
  font-size: 0.85rem;
}}
.bp-progress-state {{ display: inline-flex; align-items: center; gap: 0.35rem; }}

/* Reading order belongs to the book itself, not to MkDocs' global navbar. */
.bp-book-nav {{
  display: flex;
  font-family: {sans};
  gap: 1.5rem;
  margin: 3rem 0 1rem;
  padding-top: 1rem;
  border-top: 1px solid var(--bp-rule);
}}
.bp-book-nav-link {{
  display: flex;
  flex-direction: column;
  max-width: 48%;
  text-decoration: none;
}}
.bp-book-nav-link:hover {{ text-decoration: none; }}
.bp-book-nav-next {{ margin-left: auto; text-align: right; }}
.bp-book-nav-direction {{
  font-family: {mono};
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--bp-muted);
}}
.bp-book-nav-title {{ margin-top: 0.15rem; font-family: {serif}; font-size: 1rem; }}

/* Theorem environments, following leanblueprint's amsthm markup. */
.bp-thmwrapper {{ margin: 2rem 0 2.25rem; }}

.bp-thmheading {{
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  font-family: {serif};
  font-weight: 700;
  line-height: 150%;
  color: var(--bp-fg);
}}
.bp-thmlabel {{ margin-left: 0.4rem; margin-right: 0.5rem; }}
.bp-thmtitle::before {{ content: "("; }}
.bp-thmtitle::after {{ content: ")"; }}

/* With sticky tabs Material renders them inside <header>, so the title, the
   repository link and the tabs are one bar. These rules only remove the seam
   between the two rows of that bar. */
.md-header {{ box-shadow: 0 2px 6px rgba(0, 0, 0, 0.35); }}
.md-tabs {{ background-color: transparent; box-shadow: none; }}
.md-tabs__list {{ padding-left: 0.2rem; }}
.md-tabs__item {{ height: 1.9rem; }}
.md-tabs__link {{ font-size: 0.72rem; opacity: 0.9; margin-top: 0; }}
.md-tabs__link--active, .md-tabs__link:hover {{ opacity: 1; }}

/* "Next up" is the one card a visitor reads first, so it gets real hierarchy
   and a way into the mathematics rather than three stacked words. */
.bp-next-target {{
  border: 1px solid var(--bp-rule);
  border-left: 3px solid var(--bp-link);
  border-radius: 6px;
  padding: 0.75rem 1rem;
  margin: 1rem 0 1.5rem;
  background: var(--bp-surface);
}}
.bp-next-kicker {{
  font-family: {sans};
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--bp-muted);
}}
.bp-next-title {{ font-family: {serif}; font-size: 1.15rem; margin-top: 0.15rem; }}
.bp-next-why, .bp-next-rests {{
  font-family: {sans};
  font-size: 0.85rem;
  color: var(--bp-muted);
  margin-top: 0.2rem;
}}
.bp-next-actions {{ font-family: {sans}; font-size: 0.85rem; margin-top: 0.5rem; }}

.bp-live-claimed {{ outline: 2px solid #2D88FF; outline-offset: 2px; }}
.bp-live-badge {{
  margin-left: 0.55rem;
  padding: 0.15rem 0.45rem;
  border: 1px solid #2D88FF;
  border-radius: 999px;
  color: #2D88FF;
  font-family: {sans};
  font-size: 0.68rem;
  font-weight: 700;
}}
.bp-live-activity {{
  margin: 1rem 0 1.5rem;
  padding: 0.9rem 1rem;
  border: 1px solid var(--bp-rule);
  border-radius: 0.6rem;
  background: var(--bp-surface);
}}
.bp-live-activity-title {{ font-weight: 700; margin-bottom: 0.35rem; }}
.bp-live-row, .bp-live-empty, .bp-live-error {{ font-size: 0.85rem; color: var(--bp-muted); }}
.bp-live-error {{ color: #D93025; }}

/* On a graph page the legend hangs off an icon at the end of the lead. It
   opens on hover and on focus, so the button is reachable by keyboard; there
   is no script behind it. */
.bp-legend-tip {{ position: relative; display: inline-block; }}
.bp-legend-icon {{
  display: inline-flex;
  padding: 0;
  border: 0;
  background: none;
  color: var(--bp-muted);
  cursor: help;
  vertical-align: -0.14em;
}}
.bp-legend-icon svg {{
  width: 1em;
  height: 1em;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
}}
.bp-legend-icon .bp-legend-dot {{ fill: currentColor; stroke: none; }}
.bp-legend-icon:hover, .bp-legend-icon:focus-visible {{ color: var(--bp-link); }}
.bp-legend-note {{
  position: absolute;
  z-index: 5;
  left: 0;
  top: calc(100% + 0.5rem);
  width: max-content;
  max-width: min(30rem, 78vw);
  padding: 0.85rem 1rem;
  border: 1px solid var(--bp-rule);
  border-radius: var(--bp-radius);
  background: var(--bp-surface);
  box-shadow: 0 8px 28px rgb(0 0 0 / 22%);
  /* Hidden without display:none, so the description stays announceable. */
  opacity: 0;
  visibility: hidden;
  transition: opacity 90ms ease;
}}
.bp-legend-tip:hover .bp-legend-note,
.bp-legend-tip:focus-within .bp-legend-note {{ opacity: 1; visibility: visible; }}

.bp-visually-hidden {{
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}}

.bp-source-link {{ color: var(--bp-muted); margin-left: 0.3rem; }}
.bp-source-link:hover {{ color: var(--bp-link-hover); text-decoration: none; }}
.bp-source-icon {{ width: 0.95em; height: 0.95em; fill: none; stroke: currentColor;
  stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.12em; }}
.bp-code-links {{ display: inline-flex; flex-wrap: wrap; align-items: center; gap: 0.2rem; margin-left: 0.45rem; }}
.bp-code-link {{
  display: inline-flex;
  align-items: center;
  color: var(--bp-muted);
  text-decoration: none;
}}
.bp-code-link:visited {{ color: var(--bp-muted); }}
.bp-code-link:hover, .bp-code-link:visited:hover {{ color: var(--bp-link-hover); text-decoration: none; }}
.bp-code-icon {{
  width: 1rem;
  height: 1rem;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}}
.bp-permalink {{
  margin-left: 0.5rem;
  font-family: {mono};
  font-weight: 400;
  text-decoration: none;
  color: var(--bp-muted);
  opacity: 0;
  transition: opacity 0.1s ease;
}}
.bp-thmwrapper:hover .bp-permalink {{ opacity: 1; }}

.bp-mark {{ margin-left: auto; font-weight: 400; padding-left: 1rem; }}
.bp-mark-label {{
  margin-left: 0.3rem;
  font-family: {mono};
  font-size: 0.78rem;
  letter-spacing: 0.02em;
  color: var(--bp-muted);
}}

.bp-thmcontent {{
  font-family: {serif};
  font-weight: 400;
  line-height: 1.75;
  margin-left: 0.5rem;
  padding: 0.15rem 0 0.15rem 1rem;
  border-left: 3px solid var(--bp-rule);
}}
.theorem-style-plain > .bp-thmcontent {{ font-style: italic; }}
.theorem-style-definition > .bp-thmcontent {{ font-style: normal; }}
.bp-thmcontent > p:first-child {{ margin-top: 0; }}
.bp-thmcontent > p:last-child {{ margin-bottom: 0; }}

.bp-thmnotes {{
  font-family: {sans};
  font-size: 0.85rem;
  line-height: 1.7;
  margin: 0.6rem 0 0 2rem;
  color: var(--bp-muted);
}}
.bp-thmnotes h6 {{
  font-family: {sans};
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--bp-muted);
  margin: 0.5rem 0 0.15rem;
}}
.bp-thmnotes ul {{ margin: 0; padding-left: 1.1rem; }}

.bp-meta {{
  font-family: {sans};
  font-size: 0.85rem;
  line-height: 1.8;
  margin: 0.6rem 0 0 2rem;
}}
.bp-dependencies {{
  margin: 0.65rem 0 0 2rem;
  font-family: {sans};
  font-size: 0.82rem;
  color: var(--bp-muted);
}}
.bp-dependencies summary {{
  width: fit-content;
  cursor: pointer;
  color: var(--bp-link);
  user-select: none;
}}
.bp-dependencies summary:hover {{ color: var(--bp-link-hover); text-decoration: underline; }}
.bp-dependency-body {{ margin-top: 0.35rem; color: var(--bp-fg); }}
/* A formula paints only within its own band, its box plus the slack its
   glyphs need, which a negative margin gives back. Across, it paints as TeX
   sets it, laps included, but only inside the block that holds it, and a
   display scrolls in its own; so no article formula can cover a card, a mark,
   a label, another line, or the side columns, whatever the renderer or theme.
   Inline formulas become inline blocks to have a box to clip, and the display
   selector outranks MathJax's own sheets, which are added after this one. */
mjx-container {{
  overflow-x: visible !important;
  overflow-y: clip !important;
  position: relative !important;
}}
mjx-container:not([display="true"]) {{
  display: inline-block;
  padding-block: 0.3em;
  margin-block: -0.3em;
}}
:root mjx-container[jax][display="true"] {{
  padding-block: 0.5em;
  margin-block: 0.5em;
}}
div.arithmatex {{ overflow-x: auto; }}
/* Material caps every svg at its container's width, which would shrink a wide
   SVG formula until it cannot be read; it keeps the size TeX set instead. */
mjx-container > svg {{ max-width: none !important; }}
:is(p, h1, h2, h3, h4, h5, h6, ul, ol, td, th):has(mjx-container) {{
  overflow-x: clip;
}}
/* A page or statement published as typed: its text wraps, and nothing in it
   is markup. */
.bp-as-typed-note {{ color: var(--bp-muted); font-size: 0.85em; }}
.bp-as-typed {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
.bp-row {{ display: flex; gap: 0.75rem; }}
.bp-key {{
  flex: 0 0 7.5rem;
  font-family: {mono};
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--bp-muted);
}}
.bp-value {{ flex: 1; }}
.bp-lean {{ font-family: {mono}; }}
.bp-where {{ color: var(--bp-muted); }}
.bp-missing {{ color: #b91c1c; }}
[data-md-color-scheme=slate] .bp-missing {{ color: #ff7b72; }}
.bp-ref::before {{
  content: "";
  display: inline-block;
  width: 0.6rem;
  height: 0.6rem;
  margin-right: 0.35rem;
  border: 1px solid var(--bp-rule);
  border-radius: 2px;
}}
.bp-swatch {{
  display: inline-block;
  width: 0.9rem;
  height: 0.9rem;
  border: 1px solid var(--bp-rule);
  border-radius: 3px;
  vertical-align: middle;
}}

{light}

/* The graph is an SVG Mermaid builds from the fence, so the dark scheme has
   to reach into it rather than restyle a stylesheet it never wrote. */
[data-md-color-scheme=slate] .mermaid .edgePath path,
[data-md-color-scheme=slate] .mermaid .flowchart-link {{ stroke: #8b949e !important; }}
[data-md-color-scheme=slate] .mermaid marker path {{
  fill: #8b949e !important;
  stroke: #8b949e !important;
}}
[data-md-color-scheme=slate] .mermaid .edgeLabel,
[data-md-color-scheme=slate] .mermaid .edgeLabel rect {{
  background: #0d1117 !important;
  fill: #0d1117 !important;
  color: var(--bp-fg) !important;
}}

{dark}
{inventory}
"""


__all__ = [
    "DECLARATION_LABELS",
    "LIVE_SCRIPT",
    "DAG_SCRIPT",
    "LOGO",
    "MERMAID_SCRIPT",
    "PUBLICATION_MANIFEST",
    "PUBLICATION_MANIFEST_MAX_BYTES",
    "PUBLICATION_SCHEMA",
    "PublicationError",
    "STYLESHEET",
    "RenderReport",
    "load_publication_source",
    "render_site",
    "publication_source_revision",
]
