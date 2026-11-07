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

Committing a site: it is built and synced in a hidden
``.autoform-publication-*`` directory beside the output, then swapped in with
one atomic exchange (``renamex_np`` on macOS, ``renameat2`` on Linux); renders
committing into the same parent directory take turns when it can be locked. On
filesystems without the exchange (NFS, SMB, 9p or WSL2 ``/mnt/c``, exFAT,
sshfs) the previous site is first renamed to
``.autoform-publication-backup-<name>``; if render dies between the two
renames, the next render moves it back before anything else. macOS metadata
files are ignored when an existing site is matched against its inventory.
Render never indexes a leftover staging directory as Lean source. On macOS
``fsync`` does not flush the drive's cache, so a power loss can still lose the
newest site. Render reads the blueprint and the output by path in two walks
that must agree. Native Windows commits through the backup swap without a lock.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import heapq
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
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from functools import lru_cache
from html.parser import HTMLParser
from itertools import islice
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from markdown.treeprocessors import Treeprocessor

from . import _page_pool, dag_viewer, graph_pages, graph_views, project_map, site_index, status
from ._fs import is_within as _is_within
from ._fs import relative as _relative
from ._fs import resolve as _resolve
from ._fs import resolving_once
from ._repository import (
    detect_ref,
    detect_repository_url,
    repository_top_level,
    tracked_files,
    verify_repository_snapshot,
)
from ._tree_snapshot import (
    TreeChangedError,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
    _update_digest,
    bind_directory_tree,
)
from .annotations import LINKS_DIR
from .approvals import approval_statuses
from .coverage import (
    COVERAGE_DISPOSITIONS,
    CoverageSummary,
    contract_table,
    coverage_target_nodes,
    load_coverage,
    validate_coverage_roles,
)
from .graph import (
    Graph,
    GraphValidationError,
    Node,
    atlas_omissions,
    load_graph,
    pinned_list_omissions,
    unpublished_content,
    unpublished_node_ids,
    unpublished_setting_issues,
)
from .graph_views import (
    _containers,
    _containment_children,
    _countable,
    _is_countable,
    _leaf_descendants,
    _module_inventories,
)
from .lean import (
    _CONTINUATION_IDENTIFIER,
    _DECLARATION_KEYWORDS,
    _DECLARATION_MODIFIERS,
    _REPLACEABLE_SCHEMAS,
    PUBLICATION_MANIFEST,
    PUBLICATION_SCHEMA,
    Declaration,
    LeanSourceError,
    SourceLinker,
    bound_names,
    declaration_names,
    declared_name,
    index_failure_message,
    resolve_lean_root,
    snapshot_project_sources,
)
from .library.declarations import _header
from .library.pinned_names import FOLDER as PINNED_NAMES_FOLDER
from .markdown import (
    _BRACKET_ESCAPE,
    _CODE_SPAN,
    _INLINE_MATH,
    FORMULA_CLASS,
    GENERATED_VIEWS,
    NOTES_BOX,
    PAGE_HEADER,
    PAGE_SUFFIXES,
    RETIRED_VIEWS,
    STATEMENT_BOX,
    _blank,
    _blank_code,
    _unquoted,
    blueprint_root,
    boxed,
    converted_alone,
    directory_path,
    frontmatter_end,
    mask_fences_and_comments,
    header_title,
    link_targets,
    markdown_anchors,
    name_text,
    only_links,
    page_title,
    published_page,
    reference_images,
    relative_link,
    render_tree,
    rendered_visible_text,
    site_converter,
    statement_and_notes,
    title_text,
    yaml_string,
)
from .markdown import HEADING as _HEADING
from .markdown import _LINK_DEFINITION as _SITE_LINK_DEFINITION
from .markdown import content_lines as _content_lines
from .markdown import outside_fences as _outside_fences
from .mathjax import CARD_CLASS, CARD_FORMULA_CLASS, MATHJAX_SCRIPT, TEX_MACROS, mathjax_script, project_macros
from .publishable import publishable_article, shown_as_typed
from .readback import READBACKS_DIR, Readback, load_readbacks, readback_for
from .review import (
    ReviewArticle,
    ReviewError,
    ReviewFinding,
    ReviewState,
    build_review_bundle,
    readback_errors,
    review_states,
)
from .scaffold import autoform_revision
from .skeleton import NodeSkeleton, SkeletonReport, _plural, source_passage
from .status import is_definition
from .testimony import formula_problems, split_testimony, typesetting_warnings

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
_SKIPPED_DIRECTORIES = frozenset({".obsidian", ".trash", ".git"})


#: Transcriptions of the paper being formalised. Vault material, not chapters.
SOURCES_DIR = "sources"
#: The coverage contract's page, compared without regard to case, as a disk
#: that ignores case serves it.
COVERAGE_PAGE = "coverage/readme.md"
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
#: The images a page may show.
_IMAGE_SUFFIXES = frozenset({".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".webp"})
#: The files a vault may publish as they are, besides its Markdown pages: a
#: browser shows these, or offers to save them, and runs nothing in them.
#: Anything else may be served as the site's own markup, so it is left out.
_STATIC_SUFFIXES = _IMAGE_SUFFIXES | {".pdf", ".bib", ".csv", ".json", ".lean", ".tex", ".txt"}
#: What the site publishes from sources/ once render verified the commit, which
#: holds the folder's other files: the notes and the images they show.
_PUBLISHED_SOURCE_SUFFIXES = frozenset({*PAGE_SUFFIXES, *_IMAGE_SUFFIXES})
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
    "irreducible_def": "Definition",
    "lemma": "Lemma",
    "opaque": "Definition",
    "proposition": "Proposition",
    "structure": "Structure",
    "theorem": "Theorem",
}

STYLESHEET = "stylesheets/blueprint.css"
MERMAID_SCRIPT = "javascripts/blueprint-mermaid.js"
#: The Mermaid build the script loads for a page with an authored diagram.
MERMAID_LIBRARY = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
LIVE_SCRIPT = "javascripts/blueprint-live.js"
DAG_SCRIPT = dag_viewer.SCRIPT_PATH
DAG_STYLE = dag_viewer.STYLE_PATH
MAP_SCRIPT = "javascripts/project-map.js"
MAP_STYLE = "stylesheets/project-map.css"
MAP_PAGE = "dependencies/map/index.html"
_TO_ROOT = "../" * MAP_PAGE.count("/")
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


def _map_assets(title: str) -> tuple[tuple[str, str], ...]:
    """The whole-project map's script, style and page, for a roadmap called ``title``."""
    return (
        # The reducer and the layout module ship ahead of the controller in one file, so the page makes one request.
        (MAP_SCRIPT, "\n".join(_static_asset(name) for name in ("tex-plain.js", "project-map-layout.js", "project-map.js"))),
        (MAP_STYLE, _static_asset("project-map.css")),
        # One standalone page over the per-level files beside it.
        (MAP_PAGE, project_map.render_page(
            title=title, map_base=_TO_ROOT + project_map.MAP_DIRECTORY + "/",
            routes=_TO_ROOT + graph_pages.SEARCH_INDEX, home=_TO_ROOT, script=_TO_ROOT + MAP_SCRIPT,
            style=_TO_ROOT + MAP_STYLE, mathjax=_TO_ROOT + MATHJAX_SCRIPT,
        )),
    )


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
    """Draw the diagrams authors write, locked down, on the pages that have one.

    The site's dependency maps are the explorer, so every diagram this script
    sees is a fence an author wrote. Mermaid draws it in strict mode, which
    drops click callbacks and sanitizes link URLs, with labels as plain text
    rather than HTML; a link to a URL keeps working. It uses Mermaid's own
    theme for each colour scheme and redraws when the scheme changes. A page
    without a diagram never loads Mermaid: the script fetches the library only
    for a page with a fence, unless the site's own configuration loads it.
    Diagrams are drawn one at a time, since the theme is Mermaid's global
    setting, and one that fails leaves the others drawn.
    """
    return f"""/* Generated by autoform render. Edits are overwritten. */
(function () {{
  var blocks = Array.prototype.map.call(
    document.querySelectorAll(".mermaid"),
    function (element) {{ return {{ element: element, source: element.textContent }}; }}
  );
  if (!blocks.length) return;
  // Diagrams borrow the site's type and draw on the page's own background.
  var FONT = '"Plus Jakarta Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  function variables(mode) {{
    return mode === "dark"
      ? {{ background: "transparent", lineColor: "#8A8D91", primaryTextColor: "#E4E6EB",
           mainBkg: "#242526", clusterBkg: "transparent", edgeLabelBackground: "#242526" }}
      : {{ background: "transparent", lineColor: "#8A8D91", primaryTextColor: "#050505",
           mainBkg: "#FFFFFF", clusterBkg: "transparent", edgeLabelBackground: "#FFFFFF" }};
  }}
  var counter = 0;

  function scheme() {{
    return document.body.getAttribute("data-md-color-scheme") === "slate" ? "dark" : "light";
  }}

  // Every pass, a scheme change's too, waits for the one before, since the
  // theme each diagram is drawn with is Mermaid's global setting.
  var queue = Promise.resolve();
  function draw() {{
    var mode = scheme();
    blocks.forEach(function (block) {{
      queue = queue.then(function () {{
        mermaid.initialize({{
          startOnLoad: false,
          securityLevel: "strict",
          htmlLabels: false,
          flowchart: {{ htmlLabels: false }},
          // A diagram's %%{{init}}%% directive cannot change a secure key at any
          // depth. The list is Mermaid's defaults plus htmlLabels, so labels stay text.
          secure: ["secure", "securityLevel", "startOnLoad", "maxTextSize",
                   "suppressErrorRendering", "maxEdges", "htmlLabels"],
          theme: mode === "dark" ? "dark" : "neutral",
          fontFamily: FONT,
          themeVariables: variables(mode)
        }});
        return mermaid.render("bp-graph-" + counter++, block.source).then(function (result) {{
          block.element.innerHTML = result.svg;
          // Strict mode registers no callbacks; this binds Mermaid's tooltips.
          if (result.bindFunctions) result.bindFunctions(block.element);
        }});
      }}).catch(function (error) {{
        console.error("autoform: a diagram could not be drawn", error);
      }});
    }});
  }}

  function start() {{
    draw();
    new MutationObserver(function (mutations) {{
      mutations.forEach(function (mutation) {{
        if (mutation.attributeName === "data-md-color-scheme") draw();
      }});
    }}).observe(document.body, {{ attributes: true }});
  }}

  if (typeof mermaid !== "undefined") {{
    start();
    return;
  }}
  var library = document.createElement("script");
  library.src = "{MERMAID_LIBRARY}";
  library.onload = start;
  document.head.appendChild(library);
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
    and the files in the way of its own. The files under ``sources/`` are read
    as a render without repository links publishes them, so the verdict does
    not depend on the clone's remote.
    """

    # The root load_graph took, so every node's path lies below it.
    blueprint = blueprint_root(blueprint)
    try:
        # Capture leaves out the folders that say published: false, as render does.
        snapshot, _nodes = _capture_blueprint(blueprint)
    except PublicationError as exc:
        # Render refuses the same inputs, and says so as errors.
        return [f"{issue}; autoform render stops until this is fixed" for issue in exc.issues]
    files = _captured_files(snapshot, blueprint)
    with resolving_once():
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


def _files_left_out(folders: Sequence[Path], copy: Path, blueprint: Path) -> dict[Path, frozenset[str] | None]:
    """The files of *folders*, folders the copy at *copy* leaves out, where the
    copy would hold them, read from *blueprint* itself, as capture skips them:
    each with its heading anchors when it is a Markdown page, so evidence there
    must name a file the folder holds, and a heading it has, as anywhere else."""

    files: dict[Path, frozenset[str] | None] = {}
    for folder in folders:
        original = blueprint / _relative(folder, copy)
        for root, directories, names in os.walk(original):
            # Capture skips hidden paths and does not follow a symbolic link.
            directories[:] = [name for name in directories if not name.startswith(".")]
            for name in names:
                path = Path(root) / name
                if name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                anchors = None
                if path.suffix.casefold() == ".md":
                    try:
                        anchors = frozenset(markdown_anchors(path))
                    except (OSError, UnicodeError):
                        pass
                files[Path(os.path.normpath(folder.resolve() / path.relative_to(original)))] = anchors
    return files


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
    # A link to a file the site leaves out keeps only its text, so the strict
    # build finds nothing missing.
    left_out = frozenset(
        _resolve(source)
        for source in snapshot
        if _relative(source, blueprint).as_posix() not in written_over
        and (
            _relative(source, blueprint).parts[:1] == (READBACKS_DIR,)
            or _relative(source, blueprint).parts[0].casefold() == LINKS_DIR
            or source.suffix.lower() not in (*PAGE_SUFFIXES, *_STATIC_SUFFIXES)
            or (source.suffix.lower() in PAGE_SUFFIXES and not _is_utf8(snapshot[source]))
        )
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
            else f"{label}: links to {target!r}, which is hidden or in an unpublished folder, so the site keeps "
            "only the link's text"
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
    # The captured bytes, the state the graph and any review disclosure
    # describe once capture checks it. Check's graph also holds the
    # articles in unpublished folders, which capture leaves out.
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
            and source.suffix.lower() not in _PUBLISHED_SOURCE_SUFFIXES
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
        elif source.suffix.lower() in PAGE_SUFFIXES:
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
        elif relative.as_posix() in written_over:
            # The project's MathJax script is read into the site's own.
            omitted[source] = "mathjax-config" if relative.as_posix() == MATHJAX_SCRIPT else "generated"
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
    named = [(_relative(source, blueprint).as_posix(), data) for source, data in snapshot.items()]
    warnings.extend(unpublished_setting_issues(named))
    if blueprint / TEX_MACROS not in snapshot and any(
        _is_within(source, blueprint / TEX_MACROS) for source in snapshot
    ):
        warnings.append(f"{TEX_MACROS}: is a directory, so the site has no project macros; make it a JSON file")
    script, script_warnings = mathjax_script(snapshot.get(blueprint / MATHJAX_SCRIPT), snapshot.get(blueprint / TEX_MACROS))
    warnings.extend(script_warnings)
    return _Publication(
        groups, targets, node_sources, files, statements, script, warnings, frozenset(typed),
        frozenset(unavailable_source_ids), linked, omitted,
    )


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
                *graph_pages.graph_page_paths(graph, wiki_links=True),
                *project_map.map_paths(),
                MAP_SCRIPT, MAP_STYLE, MAP_PAGE,
                *_ASSETS,
            }
        )
    )


def _in_the_way(blueprint: Path, graph: Graph, snapshot: dict[Path, bytes]) -> list[str]:
    """A captured file below one of :func:`_site_paths`, or at one of its
    folders. Render writes these over the captured files it copied, so it
    names the path before writing anything; capture has already refused a
    symlink or a special file anywhere."""

    captured = {_relative(source, blueprint).as_posix() for source in snapshot}
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
        # such, as audit reports it; below sources/ it keeps its text, as any link does.
        omitted = _unpublished_folders(snapshot, copy)
        below_sources = tuple(folder for folder in omitted if _relative(folder, copy).parts[:1] == (SOURCES_DIR,))
        coverage, coverage_issues = load_coverage(
            copy,
            unpublished=tuple(folder for folder in omitted if folder not in below_sources),
            accepted=_files_left_out(below_sources, copy, blueprint),
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
        # An article's link to its Markdown source opens that file in the commit, where it is edited, with no
        # line to point at: it needs the article's path there, not the bytes the build may have added to.
        blueprint_files = None
        if linked is not None and _in_working_tree(blueprint, top_level):
            listed = tracked_files(repo_root, linked, blueprint)
            if listed is not None:
                blueprint_files = frozenset(
                    unicodedata.normalize("NFC", (relative / name).as_posix()) for name in listed
                )
        uncommitted = sorted(
            {
                path
                for node in graph.nodes.values()
                if blueprint_files is not None
                and unicodedata.normalize("NFC", path := _relative(_resolve(node.path), copy_root).as_posix())
                not in blueprint_files
            }
        )
        linker = SourceLinker(
            lean.index,
            repository_url,
            linked,
            links_blueprint=links_blueprint,
            blueprint_files=blueprint_files,
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
                unpublished=[name for name, kind in snapshot.omitted if kind == "unpublished"],
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
    elif blueprint_reasons and not lean.source_files:
        report.warnings.append(
            "Markdown source links were omitted because the captured blueprint does not match "
            f"the verified local Git commit: {blueprint_reasons[0]}"
        )
    elif blueprint_reasons and sources:
        report.warnings.append(
            "the files under sources/ are published in the site instead of linked, because the captured "
            f"blueprint does not match the verified local Git commit: {blueprint_reasons[0]}"
        )
    if uncommitted:
        listed = ", ".join(uncommitted[:3]) + (", …" if len(uncommitted) > 3 else "")
        articles, them = ("article is", "it has") if len(uncommitted) == 1 else ("articles are", "they have")
        report.warnings.append(
            f"{len(uncommitted)} {articles} not in commit {linked[:12]}, so {them} no link to the Markdown source: "
            f"{listed}"
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
            entry=destination / graph_pages.book_contents(graph, graph_views.group_nodes(graph)),
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
    generated_graph_pages = graph_pages.write_graph_pages(
        graph,
        statuses,
        destination,
        node_links=lambda page, node_ids: _node_links(targets, page, node_ids),
        book_targets=targets,
        contexts_out=contexts,
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
    for node_id, (page, anchor) in targets.items():
        payload, scope = contexts[node_id]
        if page not in page_contexts or not anchor:
            page_contexts[page] = (payload, scope, node_id if not anchor else None)
    canonical_pages = {
        destination / _relative(source, blueprint)
        for source, content in site.files.items()
        if isinstance(content, str) and source.suffix.lower() in PAGE_SUFFIXES
    } | set(group_pages.values()) | {overview}
    wrapped_files: set[tuple[int, int]] = set()
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
        page.write_text(
            _integrated_page(
                page.read_text(encoding="utf-8"), page=page, destination=destination,
                payload=payload, scope=scope, owner=owner,
                prepared=True, tracks_lean=graph.tracks_lean,
            ),
            encoding="utf-8",
        )

    for relative, contents in (
        (STYLESHEET, _stylesheet()),
        (MERMAID_SCRIPT, _mermaid_script()),
        # Written here, never kept from the vault.
        (MATHJAX_SCRIPT, site.script),
        (LIVE_SCRIPT, _static_asset("blueprint-live.js")),
        (DAG_SCRIPT, explorer_script),
        (DAG_STYLE, dag_viewer._STYLE),
        *_map_assets(graph_views.group_title(graph, "roadmap")),
        (LOGO, _logo()),
    ):
        asset = destination / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(contents, encoding="utf-8")
    inventory = _inventory(graph, site, blueprint, destination, files, left_out, tracked)
    _write_publication_manifest(destination, graph, linker, coverage=coverage, inventory=inventory, **revisions)
    return report


def _integrated_page(
    text: str, *, page: Path, destination: Path, payload: Path, scope: str, owner: str | None = None,
    prepared: bool = False, tracks_lean: bool = True,
) -> str:
    """Keep a complete published page readable before moving it into the shared UI.

    The header and semantic reading content are preserved. Affected Markdown
    is parsed before adding containers, so literal code, formulas, statement
    IDs, citations and shown-as-typed boxes retain their rendered meaning.
    """
    lines = text.splitlines(keepends=True)
    end = 0
    if lines and lines[0].strip() == "---":
        end = next((index + 1 for index in range(1, len(lines)) if lines[index].strip() == "---"), 0)
    header, body = "".join(lines[:end]), "".join(lines[end:])
    # Published pages protect authored ranges before inserting renderer-owned
    # statement/navigation HTML. Re-converting that assembled page would
    # consume ordinary Markdown links and reparse existing card/heading IDs.
    # Direct callers still protect their authored body before introducing HTML.
    if not prepared:
        body = _protect_markdown_in_html(body)
    node = f' data-autoform-node-id="{html.escape(owner, quote=True)}"' if owner else ""
    title_header = "".join(lines[:2]) + "---\n" if end else text
    title = _header_title(title_header) or rendered_visible_text(_first_h1(text) or "") or _file_title(page)
    explorer = dag_viewer.render_container(
        relative_link(payload, page, ".json"),
        script_href=relative_link(destination / DAG_SCRIPT, page, ".js"),
        style_href=relative_link(destination / DAG_STYLE, page, ".css"),
        search_href=relative_link(destination / graph_pages.SEARCH_INDEX, page, ".json"),
        page_href=_as_published(relative_link(page, page, ".html")),
        scope=scope,
    )
    # A project that tracks no Lean says so on the container, and the explorer's stylesheet hides its Lean progress.
    lean = "" if tracks_lean else ' data-tracks-lean="false"'
    return (
        header + f'\n<div class="bp-explorer-page"{lean} markdown="1">\n'
        + f'<div class="bp-reading-source"{node} data-page-title="{html.escape(title, quote=True)}" markdown="1">\n\n'
        + body + '\n\n</div>\n\n' + explorer + '\n</div>\n'
    )


def _capture_blueprint(blueprint: Path) -> tuple[TreeSnapshot, frozenset[str]]:
    """Read the blueprint once, refusing what must not be published.

    As two walks by path that must agree. Folders whose README says ``published:
    false`` are left out before anything is refused or published, so no page,
    hash or check sees them; the node IDs in them come beside the snapshot.
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
    folders = unpublished_content(snapshot.files)
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
    return snapshot, node_ids


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


def _load_graph(copy: Path, blueprint: Path) -> Graph:
    """Load the copy's graph, naming the vault's paths in its errors."""
    try:
        return load_graph(copy)
    except GraphValidationError as error:
        issues = [issue.replace(str(copy), str(blueprint)) for issue in error.issues]
        raise GraphValidationError(issues) from None


def load_publication_source(blueprint_dir: str | Path) -> tuple[str, Graph]:
    """Read the blueprint once; return its publication revision and the graph of those bytes."""
    blueprint = blueprint_root(blueprint_dir)
    snapshot, _nodes = _capture_blueprint(blueprint)
    # The graph reads the articles, the annotations, the atlas and the pinned name lists.
    prefix = ("roadmap/", f"{LINKS_DIR}/", "atlas.json", f"{PINNED_NAMES_FOLDER}/")
    with _private_copy(snapshot, prefix=prefix) as (_root, copy):
        return _source_revision(snapshot), _load_graph(copy, blueprint)


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
    try:  # read before anything below it
        if stat.S_ISREG(os.lstat(path / PUBLICATION_MANIFEST).st_mode):
            with open(path / PUBLICATION_MANIFEST, "rb") as handle:
                manifest = json.loads(handle.read())
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
    On either basis a hidden or unpublished folder is one key, naming nothing
    in it.

    The reasons: ``hidden``, ``unpublished``, ``generated`` (a name render
    writes or has retired), ``mathjax-config`` (read into the site's script),
    ``review-testimony`` (read-back cards), ``graph-input`` (read by the graph
    only), ``not-utf8``, ``canvas``, ``unsupported-type`` and ``not-captured``
    (in the commit, not in the tree render read).
    """

    def url(page: Path, anchor: str = "") -> str:
        relative = _relative(page, destination)
        if relative.suffix.lower() in PAGE_SUFFIXES:
            relative = relative.with_suffix(".html")
        return _as_published(quote(relative.as_posix(), safe="/")) + (f"#{quote(anchor, safe='')}" if anchor else "")

    # Render writes these over a page the blueprint has there; a chapter's page is built from its narrative.
    written = set(_site_paths(graph)) - {_group_page(group).as_posix() for group in site.groups}
    omitted = {_relative(source, blueprint).as_posix(): reason for source, reason in site.omitted.items()}
    omitted.update((relative, "generated") for source in site.files if (relative := _relative(source, blueprint).as_posix()) in written)
    published = {
        relative: url(destination / relative)
        for relative in (_relative(source, blueprint).as_posix() for source in site.files)
        if relative not in written
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
    folders = sorted({unicodedata.normalize("NFC", name) for name, kind in left_out if kind == "unpublished"}, key=len)

    def entry(name: str) -> tuple[str, str]:
        """The key and reason for a file render did not capture: its unpublished or hidden folder's, else its own."""
        parts = name.split("/")
        composed = unicodedata.normalize("NFC", name)
        for folder in folders:
            if composed == folder or composed.startswith(f"{folder}/"):
                return "/".join(parts[: folder.count("/") + 1]), "unpublished"
        for depth, part in enumerate(parts, 1):
            if part.startswith("."):
                return "/".join(parts[:depth]), "hidden"
        # Capture leaves out the top-level names render writes itself.
        return name, "generated" if len(parts) == 1 and name in _GENERATED_FILES else "not-captured"

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
    return {"basis": basis, "published": published, "linked": linked, "omitted": omitted}


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


STRUCTURE_PAGE = "structure.md"


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
    title = rendered_visible_text(heading) or "Blueprint"
    scopes = graph_views.group_nodes(graph)
    book = graph_pages.book_contents(graph, scopes) if scopes else None
    book_href = _as_published(relative_link(destination / book, page, ".html")) if book else None
    sources_href = relative_link(sources_page, page, ".html") if sources_page else None
    # MkDocs serves a README as its folder's index only when it is spelled README.md.
    if sources_page is not None and sources_page.name == "README.md":
        sources_href = _as_published(sources_href)
    hero = _render_hero(
        title,
        body,
        graph,
        statuses,
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
        # prints it as it is, so it is written as text.
        f"title: {yaml_string(title_text(heading) or 'Blueprint')}",
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


#: A heading element's tag.
_HEADING_TAG = re.compile(r"h[1-6]")
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
) -> str:
    """Open with the project's name, its one-line claim, and where it stands.

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
        + f'<h1 class="bp-hero-title">{html.escape(title)}</h1>'
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


def _as_published(href: str) -> str:
    """MkDocs serves ``README.md`` as ``index.html``.

    Links it parses are rewritten for us, but raw HTML never reaches it, so it
    has to name the published file itself.
    """
    # As a string, so a leading ./ that keeps a colon from reading as a scheme stays.
    head, _slash, name = href.rpartition("/")
    if name.lower() in {"readme.html", "index.html"}:
        return f"{head}/index.html" if head else "index.html"
    return href


def _link_mask(line: str) -> str:
    """*line*, or a block's lines joined, with its code spans, inline math and bracket escapes blanked.

    The site reads code and math before links and an escaped bracket as text,
    as check does. Blanking keeps every offset, so a link matched in the mask
    reads its label and target back from *line*.
    """
    return _BRACKET_ESCAPE.sub(_blank, _code_and_math_mask(line))


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

    The site holds only the blueprint, so an inline link to a file outside it,
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
        """Where *raw* should point once published, or None to leave it alone."""
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
        # no page there, though check's graph knows the articles in such a folder.
        # A hidden source note is reached through the repository instead.
        try:
            relative = _relative(candidate, blueprint)
        except ValueError:
            relative = None
        if relative is not None and (
            any(_is_within(candidate, folder) for folder in unpublished)
            or (_is_hidden(relative) and relative.parts[:1] != (SOURCES_DIR,))
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
        if relative is None:
            # Nothing outside the blueprint is published: "" keeps only the text.
            return ""
        # A file the verified commit lacks under sources/, such as a scan kept
        # out of git, has no repository link and no copy on the site, so its link
        # keeps only its text, with a warning, rather than failing a strict build.
        if (
            sources_base is not None
            and relative.parts[:1] == (SOURCES_DIR,)
            and not sources_base.holds(relative, candidate)
        ):
            unlinked.add(raw)
            if image:
                unshown.add(raw)
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
                unlinked.add(raw)
                unshown.add(raw)
                if dropped is not None:
                    dropped.append(_RepositoryImage(raw))
                return ""
            return f"{_source_href(sources_base, relative.parts[1:])}{asked}{query}{separator}{fragment}"
        if candidate in left_out:
            # Nor is a file the site leaves out; its image or definition goes too.
            unlinked.add(raw)
            return ""
        # atlas.json stays off the site, so a link to it keeps only its text.
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

    unlinked: set[str] = set()
    # The image destinations a page cannot show: a file the site links in the
    # repository, or one the verified commit lacks.
    unshown: set[str] = set()

    def rewrite(line: str) -> str:
        # A link, an image and a definition each contain a bracket, which
        # masking never adds, so a line without one has nothing to move.
        if "[" not in line:
            return line
        # A definition whose title is on the next line is one block, and is
        # rewritten a line at a time, as before.
        if "\n" in line and _LINK_DEFINITION.match(line.partition("\n")[0]):
            return "\n".join(map(rewrite, line.split("\n")))
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
            if href == "" and definition.group("target") in unlinked:
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
        for match in _MARKDOWN_IMAGE.finditer(_link_mask(line)):
            # As for a link below, a destination on a later line is published as written.
            if "\n" in line[match.end("label") : match.start("target")]:
                continue
            raw = _inline_destination(line, match)
            if moved_target(raw, image=True) == "" and raw in unlinked:
                pieces += [line[end : match.start()], _kept_text(line[match.start("label") : match.end("label")])]
                end = match.end()
        line = "".join(pieces) + line[end:]
        # So does a reference image of a file a page cannot show. A definition
        # the site links in the repository stays, for a link that uses it.
        for start, text_start, text_end, stop, raw in reversed(reference_images(line, references) if references else []):
            if moved_target(raw, image=True) == "" and raw in unshown:
                line = line[:start] + _kept_text(line[text_start:text_end]) + line[stop:]
        pieces = []
        end = 0
        for match in _MARKDOWN_LINK.finditer(_link_mask(line)):
            # A destination on a later line is published as written, as before:
            # in a quote, the mask keeps the marker in front of it.
            if "\n" in line[match.end("label") : match.start("target")]:
                continue
            raw = _inline_destination(line, match)
            href = moved_target(raw)
            if href is None or raw in (href, f"<{href}>"):  # nothing moved: keep the link as written
                continue
            label = line[match.start("label") : match.end("label")]
            # A title on the next line goes, but a moved link keeps its line, so every later line stays where
            # it was written. A link cut down to its label does not, as a break there could end the paragraph.
            breaks = "\n" * line.count("\n", match.end("label"), match.end())
            pieces += [line[end : match.start()], f"[{label}]({_bracketed(href)}{breaks})" if href else label]
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
    for line in _content_lines(text):
        # The site expands tabs before it reads a definition.
        read = _SITE_LINK_DEFINITION.match(_unquoted(line)[0].expandtabs(4))
        if read is not None:
            definitions[read.group(1).strip().lower()] = read.group(2)
        elif (inner := rewritten.get(line)) is not None and (written := _LINK_DEFINITION.match(inner)):
            definitions[written.group("label").strip().lower()] = written.group("target")
    return definitions


def _kept_text(text: str) -> str:
    """*text*, an image's alternative text, kept where the image stood. Where
    it starts a line, the site would read a leading ``#``, ``>``, list marker
    or rule as block markup, so that character is escaped."""

    marker = _KEPT_TEXT_MARKER.match(text)
    return f"{text[: marker.start('char')]}\\{text[marker.start('char') :]}" if marker else text


def _bracketed(href: str) -> str:
    """*href* as a Markdown link's destination. A decoded path, or a query or
    fragment kept from angle brackets, can hold a space or a parenthesis.
    CommonMark allows no space in a bare destination, and Python-Markdown
    needs its parentheses balanced."""

    return f"<{href}>" if re.search(r"[\s()]", href) else href


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


def _declaration_label(node: Node) -> str:
    if node.catalog == "module":
        return "Module"
    # A command with no label reads as written; check warns about it.
    command = status.declaration_command(node.declaration)
    return DECLARATION_LABELS.get(command.casefold()) or command[:1].upper() + command[1:] or "Node"


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


def _render_chapter(
    group: str,
    node_ids: list[str],
    *,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    used_by: dict[str, list[str]],
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
    return _convert_body(body)[1] if "<" in body else None


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


@lru_cache(maxsize=1)
def _body_converter():
    converter = site_converter()
    # The final page assigns heading IDs once, across all its statements.
    # attr_list still keeps an author's explicit {#id} here.
    converter.treeprocessors.deregister("toc")
    # After unescaping (priority 0), as MkDocs reads links: before it, a
    # destination written `a\_b.md` still holds an escape placeholder.
    converter.treeprocessors.register(_PublishedBodyLinks(converter), "published-body-links", -1)
    return converter


def _convert_body(
    text: str, references: Mapping[str, tuple[str, str]] | None = None,
) -> tuple[str, dict[str, tuple[str, str]], list[tuple[str, str]]]:
    """*text* converted as on the final page, with the reference definitions it
    knew and the links it left for MkDocs to check.

    One converter is reused, as markdown's render_html reuses its own. Its reset
    clears the definitions but not the links collected, and a conversion that
    fails midway leaves it unable to convert the next text.
    """
    converter = _body_converter()
    converter.reset()
    links = converter.treeprocessors["published-body-links"]
    links.validation_targets = []
    if references:
        converter.references.update(references)
    try:
        rendered = converter.convert(text)
    except Exception:
        _body_converter.cache_clear()
        raise
    return rendered, dict(converter.references), links.validation_targets


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
    rendered, _, targets = _convert_body(text, references)
    if "```" in rendered or "~~~" in rendered:
        protected = _FenceSafeHtml()
        protected.feed(rendered)
        protected.close()
        rendered = "".join(protected.parts)
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


def _protect_narrative_markdown(
    text: str, *, references: Mapping[str, tuple[str, str]] | None = None, force: bool = False,
    page_header: bool = True,
) -> str:
    """Protect authored narrative, never an already assembled renderer page.

    Metadata remains metadata. The canonical converter handles only affected
    authored ranges; normal Markdown retains MkDocs' own link/anchor processing.
    """
    if not force and "<" not in text:
        return text
    lines = text.splitlines(keepends=True)
    end = 0
    if page_header and lines and lines[0].strip() == "---":
        end = next((index + 1 for index in range(1, len(lines)) if lines[index].strip() == "---"), 0)
    header, body = "".join(lines[:end]), "".join(lines[end:])
    return header + _protect_markdown_in_html(body, references=references, force=force)


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
    # The "#" link's only text is "#", so its name says which statement it links to.
    permalink_label = f"Link to {shown or caption}"

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


def _review_disclosures(
    blueprint: Path,
    graph: Graph,
    skeleton: SkeletonReport,
    readbacks: Mapping[tuple[str, str], Readback],
    macros: Mapping[str, object],
    captured: Iterable[str],
    *,
    sources_linked: bool = False,
    unpublished: Iterable[str] = (),
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
    or from one of ``unpublished``, the folders capture left out, is refused
    as such.
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
    # The copy lacks what capture leaves out, hidden files and the folders
    # that say published: false. A passage from one is refused as such, and a
    # locator into a note there, which the vault skips, is skipped in the copy
    # too. Names are compared as capture lists them, in one Unicode form.
    withheld = [PurePosixPath(unicodedata.normalize("NFC", folder)) for folder in unpublished]

    def unpublished_at(name: str) -> bool:
        return any(_is_within(PurePosixPath(unicodedata.normalize("NFC", name)), folder) for folder in withheld)

    def skipped(target: str, page: Path) -> bool:
        name = _cited_name(target, page, graph.blueprint_dir)
        hidden = _is_hidden(PurePosixPath(name))
        return name.casefold().endswith(".md") and (hidden or unpublished_at(name))

    # A passage shown must be the one the copy cuts, from a file the site holds.
    for article in bundle.articles:
        if article.node_id not in graph.nodes:
            continue
        # The locator ends in its line range; a file's name may hold a #.
        cited = (article.passage_locator or "").rpartition("#")[0]
        if cited and unpublished_at(cited):
            reason = f"cites a passage from {cited}, in a folder that says published: false"
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
                if _left_out(PurePosixPath(cited), written_over, sources_linked=sources_linked):
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
            card = readback_for(readbacks, article.article_id, declaration.name)
            if card is not None and not readback_errors(declaration, card):
                warnings.extend(
                    f"{article.node_id}: read-back for {declaration.name}: {warning}"
                    for warning in typesetting_warnings(card.text)
                )
    return disclosures, warnings


def _folded(name: str) -> str:
    """``name`` as capture compares names for a collision."""

    return unicodedata.normalize("NFC", name).casefold()


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


def _left_out(cited: PurePosixPath, written_over: Collection[str], *, sources_linked: bool = False) -> bool:
    """Whether the site leaves out the captured file at ``cited``, a path in
    the blueprint as captured, as :func:`_publication` does: a file under
    ``readbacks/`` or ``links/``, the atlas, a file render writes its own over (one of
    ``written_over``, the :func:`_site_paths`), or one that is neither a page
    nor a file the site copies. With ``sources_linked``, every file under
    ``sources/`` counts as published, on the site or at the verified commit."""

    if sources_linked and cited.parts[:1] == (SOURCES_DIR,):
        return False
    return (
        cited.parts[:1] == (READBACKS_DIR,)
        or cited.parts[0].casefold() == LINKS_DIR
        or cited.as_posix().casefold() == "atlas.json"
        or cited.as_posix() in written_over
        or cited.suffix.lower() not in (*PAGE_SUFFIXES, *_STATIC_SUFFIXES)
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
    rows = [("Prepared evidence", f"<code>{html.escape(article.evidence_hash)}</code>")]
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
        card = readback_for(readbacks, article.article_id, declaration.name)
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


#: What the page's Markdown would act on in raw HTML: it ends a line at a
#: carriage return as at a line feed, and expands tabs from its own columns.
#: Character references for them it leaves alone.
_AS_TYPED = str.maketrans({"\r": "&#13;", "\t": "&#9;"})
_ONE_LINE = str.maketrans({"\r": "&#13;", "\n": "&#10;", "\t": "&#9;"})


def _typed(text: str, *, one_line: bool = False) -> str:
    """``text`` as HTML the page's Markdown leaves as typed: escaped, with
    fence markers, carriage returns and tabs as character references, and
    with ``one_line`` its line feeds too."""

    return _FenceSafeHtml.escaped(html.escape(text)).translate(_ONE_LINE if one_line else _AS_TYPED)


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
        if url and node.catalog == "module":
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


#: How many of a statement's Lean names show their declaration under it.
_LEAN_BLOCK_NAMES = 3
#: The most lines a declaration shows open; a longer one folds whole.
_LEAN_BLOCK_LINES = 14
#: The most lines of a declaration the card for a name in it shows.
_LEAN_CARD_LINES = 12
#: The Lean a block marks: comments, strings and keywords, each named by its group, and the names it may link.
#: The declaration keywords and modifiers are the index's own.
_LEAN_TOKEN = re.compile(
    r"(?P<comment>/-.*?-/|--[^\n]*)"
    r'|(?P<string>"(?:\\.|[^"\\])*")'
    rf"|(?<![\w.'!?])(?P<keyword>{_DECLARATION_KEYWORDS}|{_DECLARATION_MODIFIERS}|example"
    r"|where|extends|deriving|fun|λ|let|have|show|from|by|at|with|match|if|then|else|do|return|for|in|calc"
    r"|termination_by|decreasing_by|Type|Prop|Sort|sorry)(?![\w'!?])"
    rf"|(?<![\w.'!?»])(?P<name>{_CONTINUATION_IDENTIFIER.pattern})",
    re.DOTALL,
)


#: What follows a field or an argument being given (``mul_comm := …``, ``(n := 3)``) or declared (``base : Nat``).
_GIVEN = re.compile(r"\s*:=")
_DECLARED = re.compile(r"\s*:(?!=)")
#: What comes before a field's or a named argument's name: ``(n := 3)``, ``{ a := 1, b := 2 }``, ``{ s with b := 2 }``.
_SLOT = re.compile(r"(?:[({,]|\bwith)\s*$")
#: An attribute list still open before a name: ``@[simp, ext``.
_ATTRIBUTE = re.compile(r"@\[[^\]]*$")
#: The packages whose modules Mathlib's documentation covers, from its navigation (2026-11-05), and its page for a
#: constant by full name, an exact lookup.
_DOCUMENTED = frozenset(
    {"Aesop", "Archive", "Batteries", "Counterexamples", "ImportGraph", "Init", "Lake", "Lean", "LeanSearchClient",
     "Mathlib", "Plausible", "ProofWidgets", "Qq", "Std"}
)
_MATHLIB_DOCS = "https://leanprover-community.github.io/mathlib4_docs/find/?pattern={}#doc"


def _lean_blocks(
    node: Node,
    linker: SourceLinker,
    *,
    links: Mapping[str, str],
    formalizing: Mapping[str, str],
) -> list[str]:
    """The statement's first Lean declarations as written, under its statement.

    A theorem or lemma shows its statement and not its proof; anything else shows whole, a definition's
    body included. A long declaration folds. A catalog shows none, as it shows no code icons.

    A name in them that the project declares links to the statement that formalizes it, else to its
    source, and the block carries a card for each, hidden, that the book shows on hover.

    With the build's ``.ilean`` file for the declaration's file (:meth:`SourceLinker.names`), a name links
    exactly the constant Lean resolved it to: a project declaration as above, or a constant of a package
    Mathlib's documentation covers to its page there, with no card. Any other name stays text: a local, a
    binder, a ``variable``, a constructor or field being declared, notation, a constant of another package.
    A project constant the index does not hold, such as a constructor, and every name of a declaration without
    usable data, is looked up among the project's own declarations (:meth:`SourceLinker.resolve`).
    """
    if node.catalog is not None:
        return []
    blocks: list[str] = []
    named: dict[str, Declaration] = {}
    found = filter(None, map(linker.excerpt, declaration_names(node.lean or "")))
    for declaration, source in islice(found, _LEAN_BLOCK_NAMES):
        text, indent = source, 0
        if declaration.keyword in ("theorem", "lemma"):
            # The header leaves out the first line's indent, which the build's columns count.
            text, indent = _header(source, 0), len(source) - len(source.lstrip())
        bound = bound_names(text)
        usages = linker.names(declaration, source)
        exact = None if usages is None else {at - indent: use for at, use in usages.items() if 0 <= at - indent < len(text)}

        def project(target: Declaration, prefix: str, rest: str) -> str:
            named.setdefault(target.name, target)
            href = links[formalizing[target.name]] if target.name in formalizing else linker.url(target.name)
            name, rest = html.escape(target.name, quote=True), _typed(rest)
            if href is None:
                return f'<span class="bp-lean-ref" tabindex="0" data-lean-name="{name}">{_typed(prefix)}</span>{rest}'
            href = html.escape(href, quote=True)
            return f'<a class="bp-lean-ref" href="{href}" data-lean-name="{name}">{_typed(prefix)}</a>{rest}'

        def looked_up(written: str) -> str | None:
            resolved = None if written.split(".", 1)[0] in bound else linker.resolve(written, declaration)
            # A one-letter name is almost always a variable the file declares, which the index cannot see. The card,
            # the link and the formalizing statement go by name, so a declaration the index files under another of
            # its name's declarations, or under none, links nothing rather than the wrong one.
            if resolved is None or len(resolved[1]) < 2 or linker.location(resolved[0].name) != resolved[0]:
                return None
            target, prefix = resolved
            return project(target, prefix, written[len(prefix) :])

        def reference(written: str, at: int) -> str | None:
            if exact is None:
                return looked_up(written)
            return _exact_reference(written, exact.get(at), declaration, linker, project, looked_up)

        code = f'<pre class="bp-lean"><code>{_lean_code(text, reference)}</code></pre>'
        count = text.count("\n") + 1
        if count > _LEAN_BLOCK_LINES:
            summary = f"<code>{_typed(declaration.name, one_line=True)}</code> · {count} lines"
            code = f'<details class="bp-lean-fold"><summary>{summary}</summary>{code}</details>'
        blocks.append(code)
    if not blocks:
        return []
    cards = [card for target in named.values() if (card := _lean_card(target, linker))]
    hidden = ['<div class="bp-lean-names" hidden>', *cards, "</div>"] if cards else []
    return ["", '<div class="bp-lean-source" data-search-exclude="1">', *blocks, *hidden, "</div>", ""]


def _exact_reference(
    written: str,
    found: tuple[int, str, str] | None,
    within: Declaration,
    linker: SourceLinker,
    project: Callable[[Declaration, str, str], str],
    looked_up: Callable[[str], str | None],
) -> str | None:
    """*written*, a name in *within*'s Lean, linked to the constant an ``.ilean`` usage starting at it names
    (:meth:`SourceLinker.names`), or ``None``. The usage must end at a component's end, so dot notation past a name
    (``Nat.succ_le.mp``) leaves the rest as text.

    A constant the project declares in the file the usage names links as :func:`_lean_blocks` links a project
    name, unless it is *within* itself: its own name or a recursive call. A private one is named by its
    declaration. Another constant of a project file, one the index does not hold, such as a constructor, or files
    elsewhere, as a public declaration may share a private one's name, is *looked_up* as without the ``.ilean``
    file. A public constant of a package Mathlib's documentation covers links to its page there, by its full
    name, which its title shows.
    """
    if found is None:
        return None
    length, constant, module = found
    prefix, rest = written[:length], written[length:]
    if rest[:1] not in ("", "."):
        return None
    declared = declared_name(constant)
    target = linker.declared_in(declared, module)
    if target is not None:
        return None if target == within else project(target, prefix, rest)
    if linker.in_project(module):
        return looked_up(written)
    if declared != constant or module.split(".", 1)[0] not in _DOCUMENTED:
        return None
    href = _MATHLIB_DOCS.format(quote(constant, safe="."))
    title = html.escape(constant, quote=True)
    link = f'<a class="bp-lean-doc" href="{html.escape(href, quote=True)}" title="{title}">{_typed(prefix)}</a>'
    return link + _typed(rest)


def _lean_card(declaration: Declaration, linker: SourceLinker) -> str | None:
    """What the book shows on hovering *declaration*'s name: its statement, or its first lines, and its file."""
    excerpt = linker.excerpt(declaration.name)
    if excerpt is None:
        return None
    text = excerpt[1]
    if declaration.keyword in ("theorem", "lemma"):
        text = _header(text, 0)
    lines = text.split("\n")
    if len(lines) > _LEAN_CARD_LINES:
        text = "\n".join([*lines[:_LEAN_CARD_LINES], "  ⋯"])
    where = _typed(f"{declaration.path.as_posix()}:{declaration.line}", one_line=True)
    name = html.escape(declaration.name, quote=True)
    return (
        # Material's search ends an excluded element at the first closing tag of its kind, so each card excludes itself.
        f'<div data-lean-name="{name}" data-search-exclude="1">'
        f'<pre class="bp-lean"><code>{_lean_code(text)}</code></pre><p class="bp-lean-where">{where}</p></div>'
    )


def _lean_code(text: str, reference: Callable[[str, int], str | None] = lambda _written, _at: None) -> str:
    """``text``, Lean, as HTML the page's Markdown leaves as typed, its comments, strings and keywords marked,
    and each name *reference* links linked; *reference* gets the name as written and its offset in ``text``.

    None of these is a reference: tactics, after ``by`` or ``decreasing_by``, to the end; a name in an attribute
    list; a name before ``:=`` in a field's or a named argument's place (after ``(``, ``{``, ``,`` or ``with``,
    or at a line's start unless the line before ends in a result type's colon); a name starting a line before
    ``:``, a field being declared.
    """
    parts: list[str] = []
    end = 0
    tactics = False
    for token in _LEAN_TOKEN.finditer(text):
        tactics = tactics or token.lastgroup == "keyword" and token[0] in ("by", "decreasing_by")
        if token.lastgroup == "name":
            start = text.rfind("\n", 0, token.start()) + 1
            before = text[start : token.start()]
            opens_line = not before.strip()
            slot = opens_line and not text[:start].rstrip().endswith(":") or _SLOT.search(before)
            given = slot and _GIVEN.match(text, token.end()) or opens_line and _DECLARED.match(text, token.end())
            marked = None if tactics or given or _ATTRIBUTE.search(before) else reference(token[0], token.start())
            if marked is None:
                continue
        else:
            marked = f'<span class="bp-lean-{token.lastgroup}">{_typed(token[0])}</span>'
        parts += [_typed(text[end : token.start()]), marked]
        end = token.end()
    return "".join(parts) + _typed(text[end:])


def _search_names(node: Node) -> list[str]:
    """The card's exact Lean names, as hidden text the site's search indexes.

    A verified name shows only as its code icon's label, and search reads text,
    not attributes. A catalog's names stay out: it can list hundreds, and its
    Lean row already shows the first few.
    """
    names = declaration_names(node.lean or "") if node.catalog is None else ()
    if not names:
        return []
    return ['<div class="bp-search-names" hidden>' + " ".join(html.escape(name) for name in names) + "</div>"]


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
    if not linker.repository_url or not linker.ref or linker.blueprint_files is None:
        return ""
    try:
        relative = _relative(_resolve(node.path), repo_root).as_posix()
    except ValueError:
        return ""
    if unicodedata.normalize("NFC", relative) not in linker.blueprint_files:
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


def _title_html(text: str, *, fallback: str | None = None) -> str:
    """A title as HTML whose TeX and code the site typesets, not as the source's dollar signs.

    Only converter-validated text, code and math enter the page; authored HTML,
    destinations and attributes stay literal. A title the converter leaves alone
    shows ``fallback`` as text when given: a page header's visible title, whose
    emphasis and links read as the page's own heading does.
    """
    reading = site_index._reading_label(text, require_math=False)
    if reading is None:
        return html.escape(text if fallback is None else fallback)
    return "".join(
        html.escape(value) if kind == "text" else
        f"<code>{html.escape(value)}</code>" if kind == "code" else
        f'<span class="arithmatex">{html.escape(value)}</span>'
        for kind, value in reading["parts"]
    )


def _display_title(title: str, caption: str) -> str:
    """Drop a kind the caption already shows: 'Theorem (X)' under 'Theorem' reads X.

    Display only; the graph keeps the authored title. A title that is just the
    kind word, or empty, gives "", so the caption alone names the statement, and
    the outer parentheses are only removed when they enclose the whole rest of
    the title.
    """
    if title.casefold() == caption.casefold():
        return ""
    prefix = f"{caption} ("
    if not (title.casefold().startswith(prefix.casefold()) and title.endswith(")")):
        return title
    inner, depth = title[len(prefix) : -1], 0
    for character in inner:
        depth += (character == "(") - (character == ")")
        if depth < 0:
            return title
    return inner if depth == 0 else title


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


def _stylesheet() -> str:
    """Blueprint styling: amsthm structure over Facebook's product surfaces.

    Both schemes are Facebook's shipped greys with Meta blue, set in Plus
    Jakarta Sans with JetBrains Mono for code. Dark is not light dimmed: it is
    the palette Facebook uses in dark mode, and it hangs off Material's colour
    scheme attribute so the theme's own toggle drives it.
    """
    # Dependency dots and swatches are a few pixels wide and carry no word, so
    # every state with a tinted light fill (the four finished states,
    # conditionally proved, not ready and inventory pending) fills them with
    # its stroke: the tints are too pale to tell from an outline there. The
    # light fill decides, so both schemes fill the same states.
    def dot(state: status.State, dark: bool = False) -> str:
        fill, stroke = (state.dark_fill, state.dark_stroke) if dark else (state.fill, state.stroke)
        return f"background: {fill if state.fill == '#FFFFFF' else stroke}; border-color: {stroke};"

    light = "\n".join(
        f".bp-{state.key} .bp-mark {{ color: {state.stroke}; }}\n"
        f".bp-{state.key} > .bp-thmcontent {{ border-left-color: {state.stroke}; }}\n"
        f".bp-ref-{state.key}::before, .bp-swatch-{state.key} {{ {dot(state)} }}"
        for state in status.STATES
    )
    dark = "\n".join(
        f"[data-md-color-scheme=slate] .bp-{state.key} .bp-mark {{ color: {state.dark_stroke}; }}\n"
        f"[data-md-color-scheme=slate] .bp-{state.key} > .bp-thmcontent "
        f"{{ border-left-color: {state.dark_stroke}; }}\n"
        f"[data-md-color-scheme=slate] .bp-ref-{state.key}::before, "
        f"[data-md-color-scheme=slate] .bp-swatch-{state.key} "
        f"{{ {dot(state, dark=True)} }}"
        for state in status.STATES
    )
    # The legends beside the explorer's rings list module inventories, so
    # their swatches take the colours the explorer draws inventories in.
    inventory = "\n".join(
        f".bp-swatch-{state.key} {{ {dot(state)} }}\n"
        f"[data-md-color-scheme=slate] .bp-swatch-{state.key} "
        f"{{ {dot(state, dark=True)} }}"
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
   ones Facebook ships in dark mode and the blue is Meta blue, which light
   mode's links share with the status strokes. The brand gradient stays off
   the figures, the progress bar and the header. Dark mode's link blue is
   lighter than its status blue, so links keep 4.5:1 on every surface.
   A blueprint should look like it came from here rather than from any
   documentation generator. */
:root {{
  --bp-fg: #050505;
  --bp-muted: #65676B;
  --bp-rule: #CED0D4;
  --bp-surface: #FFFFFF;
  --bp-sunken: #F0F2F5;
  --bp-page: #FFFFFF;
  --bp-scroll-shade: rgba(5, 5, 5, 0.22);
  --bp-link: #0064E0;
  --bp-link-hover: #0082FB;
  --bp-blue: #0064E0;
  --bp-radius: 12px;
}}
[data-md-color-scheme=slate] {{
  --bp-fg: #E4E6EB;
  --bp-muted: #B0B3B8;
  --bp-rule: #3E4042;
  --bp-surface: #242526;
  --bp-sunken: #1C1D1F;
  --bp-page: #18191A;
  --bp-scroll-shade: rgba(228, 230, 235, 0.3);
  --bp-link: #4A9AFF;
  --bp-link-hover: #7FB8FF;
  --bp-blue: #2D88FF;
}}
/* Material's own tokens take the same blue, so its links and breadcrumbs
   match the explorer instead of the theme's default indigo.
   The theme sets them on body through data-md-color-primary, and the slate
   link colour through two attributes, so this selector carries two
   attributes and wins by coming later. The bp tokens already switch with
   the scheme, so one rule covers both. The darker and lighter shades follow,
   or the drawer's repository strip and hover tints stay indigo. */
[data-md-color-scheme][data-md-color-primary] {{
  --md-primary-fg-color: var(--bp-blue);
  --md-primary-fg-color--light: color-mix(in srgb, var(--bp-blue) 80%, #FFFFFF);
  --md-primary-fg-color--dark: color-mix(in srgb, var(--bp-blue) 80%, #000000);
  --md-accent-fg-color: var(--bp-link-hover);
  --md-accent-fg-color--transparent: color-mix(in srgb, var(--bp-link-hover) 10%, transparent);
  --md-typeset-a-color: var(--bp-link);
}}
/* White on the accent is 2.1:1 in slate and 3.8:1 in light, and the
   back-to-top button shows focus only by that fill. Slate's accent takes the
   page's near-black text; light's button fills with the link blue. */
[data-md-color-scheme=slate][data-md-color-accent] {{ --md-accent-bg-color: #18191A; }}
[data-md-color-scheme=default] .md-top:is(:hover, :focus) {{ background-color: var(--bp-link); }}

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

.md-header {{ box-shadow: 0 2px 6px rgba(0, 0, 0, 0.35); border-bottom: 1px solid var(--bp-rule); }}
/* The mobile drawer's title bar and repository strip match the header:
   white on slate's brighter blue is 3.5:1. */
[data-md-color-scheme=slate] .md-header,
[data-md-color-scheme=slate] .md-tabs,
[data-md-color-scheme=slate] .md-nav--primary .md-nav__title[for=__drawer],
[data-md-color-scheme=slate] .md-nav__source {{ background-color: #242526; }}
.md-tabs {{ border-bottom: 1px solid var(--bp-rule); }}
.md-tabs__link {{ font-size: 0.72rem; font-weight: 600; opacity: 0.9; margin-top: 0; }}
.md-tabs__link--active, .md-tabs__link:hover {{ opacity: 1; }}

/* The landing page is a dashboard, not a chapter, so it drops the reading
   column the rest of the book keeps. :has() is how a single generated
   stylesheet can tell the two apart without a second page template; where it
   is unsupported the page simply stays at reading width. */
.md-typeset:has(.bp-landing) {{ max-width: 62rem; }}
/* The explorer's reading pane sets its own width. Only running text keeps the
   reading measure, so tables, code and cards use the whole pane. The pane's
   article holds the page's reading source, whose children are the text. */
.bp-reading-body.md-typeset {{ max-width: none; }}
.bp-reading-body > .bp-reading-source > :is(p, ul, ol, dl, blockquote) {{ max-width: 72ch; }}

.bp-hero {{ margin: 0 0 2.5rem; }}
.bp-hero-kicker {{
  margin-top: 1.1rem;
  font-size: 0.68rem;
  font-weight: 600;
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
  color: var(--bp-fg);
}}
.bp-figure-neutral .bp-figure-value {{ color: var(--bp-muted); }}
.bp-figure-label {{
  margin-top: 0.45rem;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.01em;
}}
.bp-figure-note {{ margin-top: 0.15rem; font-size: 0.7rem; color: var(--bp-muted); }}
/* The ready figure opens the list of what it counts; it keeps the figure's
   colours, and an arrow after its label shows it is a link, as on
   "Start reading". Hover or focus underlines the label. */
.md-typeset a.bp-figure-link,
.md-typeset a.bp-figure-link:is(:visited, :hover, :focus-visible) {{ color: var(--bp-fg); text-decoration: none; }}
.bp-figure-link > span {{ display: block; }}
.md-typeset a.bp-figure-link:is(:hover, :focus-visible) .bp-figure-label {{ text-decoration: underline; }}
/* The arrow is decoration, so it stays out of the link's accessible name where
   the browser supports alt text. The figures clip overflow, so the focus ring
   is drawn inside the cell. */
.bp-figure-link .bp-figure-label::after {{ content: " →"; content: " →" / ""; }}
.md-typeset a.bp-figure-link:focus-visible {{ outline-offset: -2px; }}
/* With no figures, or all statements fully proved, this one sentence is the
   hero's status, so it is set at the claim's size. */
.md-typeset .bp-hero-status {{ margin: 1rem 0 0; color: var(--bp-fg); font-size: 1.05rem; font-weight: 600; }}

.bp-hero-bar {{
  margin-top: 1rem;
  height: 6px;
  border-radius: 6px;
  background: var(--bp-sunken);
  border: 1px solid var(--bp-rule);
  overflow: hidden;
}}
.bp-hero-bar > span {{ display: block; height: 100%; }}

.bp-hero-coverage {{
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.2rem 0.65rem;
  margin-top: 0.75rem;
  font-size: 0.72rem;
  color: var(--bp-muted);
}}
.bp-hero-coverage-links {{ margin-left: auto; }}
.bp-hero-coverage-link,
.bp-hero-sources-link {{
  font-weight: 600;
  text-decoration: underline;
  text-underline-offset: 0.15em;
}}
.bp-hero-sources-link {{ margin-left: 0.65rem; }}

/* On the landing page the hero sits in the explorer's reading pane, or its
   sheet on phones, so it is set, like the pane's chapters, in px rather
   than Material's growing rem. Material's typeset rules
   reach the hero's heading, paragraph and links there, so these rules carry
   a doubled class; the explorer's own rules stay in its script. The narrow
   hero keeps two plain figures, the share fully proved and what is ready
   to work on, each with its one-line note; module inventories become one
   muted line under them. */
.bp-landing-intro.bp-landing-intro {{ margin: 0 6px 18px; font-size: 13px; }}
.bp-landing-intro.bp-landing-intro .bp-hero {{ margin: 0; }}
.bp-landing-intro.bp-landing-intro .bp-next-kicker {{
  font-size: 12px;
  font-weight: 500;
  letter-spacing: normal;
  text-transform: none;
}}
.bp-landing-intro.bp-landing-intro .bp-hero-title {{
  margin: 12px 0 0;
  color: var(--bp-fg);
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 1.2;
}}
.bp-landing-intro.bp-landing-intro .bp-hero-lead {{
  max-width: none;
  margin: 10px 0 0;
  color: var(--bp-fg);
  font-size: 16px;
  line-height: 1.5;
}}
.bp-landing-intro.bp-landing-intro .bp-hero-status {{
  margin: 14px 0 0;
  color: var(--bp-fg);
  font-size: 16px;
  font-weight: 600;
}}
.bp-landing-intro.bp-landing-intro .bp-hero-figures {{
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin-top: 14px;
}}
.bp-landing-intro.bp-landing-intro .bp-figure {{ padding: 9px 10px; }}
.bp-landing-intro.bp-landing-intro .bp-figure-value {{
  font-size: 17px;
  font-weight: 700;
}}
.bp-landing-intro.bp-landing-intro .bp-figure-label {{ margin-top: 5px; font-size: 12px; font-weight: 600; }}
.bp-landing-intro.bp-landing-intro .bp-figure-note {{ margin-top: 2px; font-size: 12px; }}
.bp-landing-intro.bp-landing-intro .bp-hero-counts {{
  margin: 8px 0 0;
  color: var(--bp-muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}}
.bp-landing-intro.bp-landing-intro .bp-hero-coverage {{ margin-top: 6px; font-size: 12px; }}
.bp-landing-intro.bp-landing-intro .bp-hero-coverage-links {{ margin-left: 0; }}
.bp-landing-intro.bp-landing-intro .bp-intro-book {{
  display: inline-block;
  margin-top: 10px;
  color: var(--bp-link);
  font-size: 15px;
  font-weight: 600;
  text-decoration: none;
}}
.bp-intro-book::after {{ content: " →"; }}
.bp-landing-intro.bp-landing-intro .bp-next-target {{ margin: 14px 0 0; padding: 10px 12px; }}
.bp-landing-intro.bp-landing-intro .bp-next-title {{ font-size: 14px; font-weight: 600; }}
.bp-landing-intro.bp-landing-intro .bp-next-why,
.bp-landing-intro.bp-landing-intro .bp-next-rests,
.bp-landing-intro.bp-landing-intro .bp-next-actions {{ font-size: 12px; }}
.bp-landing-intro.bp-landing-intro .bp-live-activity {{ margin: 14px 0 0; padding: 10px 12px; }}
.bp-landing-intro.bp-landing-intro .bp-live-row {{ font-size: 12px; }}

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
  font-size: 0.72rem;
  font-weight: 600;
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
/* The heading row is an h4, so search gives each card a record of its own;
   it keeps the look of the div it was, under Material's and the site's h4 rules. */
.bp-thmwrapper > .bp-thmheading {{ margin: 0; font-size: inherit; letter-spacing: inherit; }}
/* The kind sits on a line of its own above the name, as on the map's reader. */
.bp-thmcaption {{ flex-basis: 100%; font-size: 0.72rem; font-weight: 400; line-height: 1.4; color: var(--bp-muted); }}

/* With sticky tabs Material renders them inside <header>, so the title, the
   repository link and the tabs are one bar. These rules only remove the seam
   between the two rows of that bar. */
.md-tabs {{ background-color: transparent; box-shadow: none; }}
.md-tabs__list {{ padding-left: 0.2rem; }}
.md-tabs__item {{ height: 1.9rem; }}

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
  font-weight: 600;
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
/* A keyboard reaches the permalink too, so focus shows it as hover does. */
.bp-thmwrapper:hover .bp-permalink, .bp-permalink:focus-visible {{ opacity: 1; }}

/* The status is a pill on its own line under the title, a dot and a word
   tinted with the status colour, as in the explorer's drawer. An empty item
   the full width of the heading breaks the line before it, so the pill sits
   at the left however long the title runs. */
.bp-thmheading::after {{ content: ""; order: 1; flex-basis: 100%; }}
.bp-mark {{
  order: 2;
  display: inline-flex;
  align-items: center;
  gap: 0.35em;
  margin-top: 0.3rem;
  padding: 0.1em 0.6em;
  border-radius: 999px;
  background: color-mix(in srgb, currentColor 12%, transparent);
  font-family: {sans};
  font-size: 0.72rem;
  font-weight: 600;
  line-height: 1.5;
}}
.bp-mark-label {{ color: var(--bp-fg); }}

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
  font-weight: 600;
  letter-spacing: normal;
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
/* Material styles every details element as a collapsible admonition: a
   bordered box, a tinted summary and a pencil icon. These are quiet lines
   under each statement, so the selectors outrank Material's and take that
   chrome off, keeping only its chevron, which turns when the line opens. */
.md-typeset :is(details.bp-dependencies, details.bp-lean-fold) {{
  margin: 0.65rem 0 0 2rem;
  padding: 0;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  background: none;
  font-family: {sans};
  font-size: 0.72rem;
  color: var(--bp-muted);
}}
.md-typeset :is(details.bp-dependencies, details.bp-lean-fold) > summary {{
  display: flex;
  align-items: center;
  width: fit-content;
  margin: 0;
  padding: 0.2rem 0;
  border: 0;
  background: none;
  font-weight: 600;
  cursor: pointer;
  user-select: none;
}}
.md-typeset :is(details.bp-dependencies, details.bp-lean-fold) > summary::before {{ display: none; }}
.md-typeset :is(details.bp-dependencies, details.bp-lean-fold) > summary::after {{
  position: static;
  flex: none;
  width: 1em;
  height: 1em;
  margin-left: 0.25em;
}}
.md-typeset :is(details.bp-dependencies, details.bp-lean-fold) > summary:hover {{ color: var(--bp-link-hover); text-decoration: underline; }}
@media (pointer: coarse) {{
  .md-typeset :is(details.bp-dependencies, details.bp-lean-fold) > summary {{ min-height: 44px; }}
}}
.bp-dependency-body {{ margin-top: 0.35rem; color: var(--bp-fg); }}
.bp-review {{
  margin: 0.65rem 0 0 2rem;
  font-family: {sans};
  font-size: 0.82rem;
  color: var(--bp-muted);
}}
.bp-review summary {{
  width: fit-content;
  overflow-wrap: anywhere;
  cursor: pointer;
  color: var(--bp-link);
  user-select: none;
}}
.bp-review summary:hover {{ color: var(--bp-link-hover); text-decoration: underline; }}
.bp-review-self-approved {{ color: var(--bp-muted); font-style: italic; }}
.bp-review-open {{ color: var(--bp-muted); }}
/* Each text color reads at 4.5:1 or more on its scheme's background. */
.bp-review-drift {{ color: #946200; font-weight: 600; }}
[data-md-color-scheme=slate] .bp-review-drift {{ color: #F0B429; }}
.bp-skeleton {{ margin-top: 0.6rem; color: var(--bp-fg); }}
.bp-skeleton-title {{ font-weight: 600; margin-bottom: 0.3rem; }}
.bp-lean {{
  margin: 0 0 0.4rem;
  padding: 0.6rem 0.8rem;
  font-family: {mono};
  font-size: 0.78rem;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-x: auto;
  border-left: 3px solid var(--bp-rule);
}}
.bp-skeleton-meta {{ margin-bottom: 0.5rem; }}
/* A statement's Lean declarations, under it, in the page's code colors. */
.bp-lean-source {{ margin: 0.6rem 0 0 2rem; }}
/* Material's code box is the block's only frame. */
.bp-lean-source .bp-lean {{ padding: 0; border: 0; }}
.md-typeset details.bp-lean-fold {{ margin: 0 0 0.4rem; }}
/* A flex summary drops the space between the name and its line count. */
.md-typeset details.bp-lean-fold > summary {{ gap: 0.3em; }}
.bp-lean-keyword {{ color: var(--md-code-hl-keyword-color); }}
.bp-lean-comment {{ color: var(--md-code-hl-comment-color); }}
.bp-lean-string {{ color: var(--md-code-hl-string-color); }}
/* A linked name in Lean: dotted until hovered, so the code still reads as code. */
.md-typeset :is(.bp-lean-ref, .bp-lean-doc) {{ color: inherit; text-decoration: underline dotted; text-underline-offset: 0.2em; }}
.md-typeset :is(.bp-lean-ref, .bp-lean-doc):is(:hover, :focus-visible) {{ color: var(--bp-link-hover); text-decoration-style: solid; }}
/* A card too wide for the page scrolls, with a shade at each edge it can
   scroll toward: covers in the review panel's color scroll with the content
   and hide the shade at the end it has reached. */
.bp-readback {{
  margin: 0.4rem 0 1rem;
  padding: 0.6rem 0.8rem;
  color: var(--bp-fg);
  border-left: 3px solid var(--bp-blue);
  overflow-x: auto;
  background:
    linear-gradient(to right, var(--md-admonition-bg-color, var(--bp-page)) 40%, transparent) left / 1.5rem 100% no-repeat local,
    linear-gradient(to left, var(--md-admonition-bg-color, var(--bp-page)) 40%, transparent) right / 1.5rem 100% no-repeat local,
    radial-gradient(farthest-side at 0 50%, var(--bp-scroll-shade), transparent) left / 0.6rem 100% no-repeat scroll,
    radial-gradient(farthest-side at 100% 50%, var(--bp-scroll-shade), transparent) right / 0.6rem 100% no-repeat scroll;
  background-color: var(--md-admonition-bg-color, var(--bp-page));
}}
.bp-readback-title {{ font-weight: 600; margin-bottom: 0.3rem; }}
.bp-readback-body {{ white-space: pre-wrap; }}
.bp-readback-status {{ font-weight: 400; color: var(--bp-muted); }}
.bp-readback-invalid {{ border-left-color: #B42318; }}
[data-md-color-scheme=slate] .bp-readback-invalid {{ border-left-color: #F97066; }}
.bp-readback-missing {{ border-left-color: var(--bp-rule); font-style: italic; }}
/* A formula paints only within its own band, its box plus the slack its
   glyphs need, which a negative margin gives back. Across, it paints as TeX
   sets it, laps included, but only inside the block that holds it, and a
   display scrolls in its own; so no article formula can cover a card, a mark,
   a label, another line, or the side columns, whatever the renderer or theme.
   A card scrolls instead, so nothing in it is cut. Inline formulas become
   inline blocks to have a box to clip, and the display selector outranks
   MathJax's own sheets, which are added after this one. */
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
:is(p, h1, h2, h3, h4, h5, h6, ul, ol, td, th):has(mjx-container):not(.bp-readback *) {{
  overflow-x: clip;
}}
/* A page or statement published as typed: its text wraps, and nothing in it
   is markup. */
.bp-as-typed-note {{ color: var(--bp-muted); font-size: 0.85em; }}
.bp-as-typed {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
.bp-row {{ display: flex; gap: 0.75rem; }}
.bp-key {{
  flex: 0 0 7.5rem;
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--bp-muted);
}}
.bp-value {{ flex: 1; }}
/* A reference breaks between entries, never inside one; a long one wraps in its own box. Each starts with its status dot, so
   the list drops the separators, which ended every wrapped line and could take a line of their own. A project that tracks
   no Lean has no dots, so its references keep the separators. */
.bp-dependency-body .bp-ref {{ display: inline-block; max-width: 100%; margin-right: 0.5rem; }}
.bp-dependency-body .bp-ref[class*=" bp-ref-"] + .bp-ref-sep {{ display: none; }}
.bp-lean {{ font-family: {mono}; }}
.bp-where {{ color: var(--bp-muted); }}
.bp-missing {{ color: #b91c1c; }}
[data-md-color-scheme=slate] .bp-missing {{ color: #ff7b72; }}
.bp-ref[class*=" bp-ref-"]::before {{
  content: "";
  display: inline-block;
  width: 0.6rem;
  height: 0.6rem;
  margin-right: 0.35rem;
  border: 1px solid var(--bp-rule);
  border-radius: 50%;
}}
.bp-swatch {{
  display: inline-block;
  width: 0.6rem;
  height: 0.6rem;
  border: 2px solid var(--bp-rule);
  border-radius: 50%;
  vertical-align: middle;
}}

{light}

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
    "PUBLICATION_SCHEMA",
    "PublicationError",
    "STYLESHEET",
    "RenderReport",
    "load_publication_source",
    "render_site",
    "publication_source_revision",
]
