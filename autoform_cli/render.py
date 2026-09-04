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
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable
from urllib.parse import quote, unquote, urlsplit

from . import graph_pages, graph_views, mermaid, status
from . import workspace as workspace_module
from ._tree_snapshot import (
    BoundDirectoryTree,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
    bind_directory_tree,
)
from .coverage import COVERAGE_V2_SCHEMA, CoverageSummary, load_coverage
from .graph import Graph, Node, load_graph
from .lean import (
    BoundProjectSources,
    IndexedSourceSnapshot,
    SourceLinker,
    build_linker,
    declaration_names,
    detect_ref,
    detect_repository_url,
    open_project_sources,
    project_source_revision,
    snapshot_project_sources,
)
from .markdown import INLINE_CODE, content, render_html
from .status import is_definition

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import compatibility
    fcntl = None  # type: ignore[assignment]

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_MARKDOWN_LINK = re.compile(
    r"(?<!!)\[(?P<label>[^\]]*)\]\(\s*"
    r"(?P<target><[^>\r\n]+>|[^)\s]+)(?P<suffix>\s+[^)]*)?\)"
)
_ANY_INLINE_LINK = re.compile(
    r"(?P<image>!?)\[(?P<label>[^\]]*)\]\(\s*"
    r"(?P<target><[^>\r\n]+>|[^)\s]+)(?P<suffix>\s+[^)]*)?\)"
)
_AUTOLINK = re.compile(r"<(?P<target>(?:\.{1,2}/|/)[^<>\s]+)>")
#: A reference-style link definition, `[label]: target "title"`. Markdown
#: resolves `[Paper][paper]` through one of these, so a rewrite that only sees
#: inline links leaves the destination behind and publishes a dead link.
_LINK_DEFINITION = re.compile(
    r'^(?P<indent>[ ]{0,3})\[(?P<label>[^\]]+)\]:[ \t]*'
    r'(?P<target><[^>\r\n]+>|[^\s]+)(?P<rest>[ \t]+.*)?$'
)
_RAW_HTML_LINK_ATTRIBUTES = frozenset(
    {"action", "data", "formaction", "href", "poster", "src", "srcset"}
)
_ARTICLE_SLOT = re.compile(
    r"^(?P<indent>[ \t]*)[-*+]\s+\[[^\]]+\]\(\s*"
    r"(?P<target><[^>\r\n]+>|[^)\s]+)(?:\s+[^)]*)?\)\s*$"
)
_DEPENDENCY_SECTIONS = frozenset({"depends on", "proof depends on"})
_SKIPPED_DIRECTORIES = frozenset({".obsidian", ".trash", ".git"})

#: Transcriptions of the paper being formalised. Vault material, not chapters.
SOURCES_DIR = "sources"
PUBLICATION_MANIFEST = "publication.json"
PUBLICATION_SCHEMA = "autoform-publication/v2"
_PUBLICATION_STAGE_PREFIX = ".autoform-publication-"
#: Derived views this command rewrites; stale copies must not leak into the site.
_GENERATED_FILES = frozenset(
    {
        "dependencies.md",
        "dependencies.html",
        "graph.html",
        "progress.md",
        # The vault keeps its own Obsidian-readable copy; the site builds one.
        "structure.md",
        PUBLICATION_MANIFEST,
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


def _is_generated_path(relative: PurePosixPath | Path) -> bool:
    return len(relative.parts) == 1 and relative.name.casefold() in _GENERATED_FILES


def _publication_snapshot_descends(relative: PurePosixPath) -> bool:
    if relative.parts and relative.parts[0].casefold() == ".autoform":
        return True
    return not (
        any(part.startswith(".") for part in relative.parts)
        or {part.casefold() for part in relative.parts}.intersection(_LOCAL_ONLY_NAMES)
    )


def _publication_snapshot_includes(relative: PurePosixPath, _mode: int) -> bool:
    folded_parts = {part.casefold() for part in relative.parts}
    name = relative.name.casefold()
    return not (
        any(part.startswith(".") for part in relative.parts)
        or folded_parts.intersection(_LOCAL_ONLY_NAMES)
        or _is_generated_path(relative)
        or name.endswith((".key", ".log", ".pem"))
    )


_PUBLICATION_SNAPSHOT_SELECTION = TreeSelection(
    include=_publication_snapshot_includes,
    descend=_publication_snapshot_descends,
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
LOGO = "assets/autoform.svg"


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
    """Render the graph, and re-render it whenever the colour scheme changes.

    Mermaid injects its own styles with ``!important`` scoped to the SVG id, so
    a stylesheet cannot recolour a diagram after the fact. The palette has to
    go in as ``classDef`` at render time, which means owning the render call
    and repeating it on a theme switch.

    Loose security is what enables the ``click`` links; the diagram is
    generated from the project's own blueprint, so nothing third-party is in it.
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
    startOnLoad: false, securityLevel: "loose", theme: "neutral",
    fontFamily: FONT, themeVariables: variables("light")
  }});

  var counter = 0;
  var blocks = Array.prototype.map.call(
    document.querySelectorAll(".mermaid"),
    function (element) {{ return {{ element: element, source: element.textContent }}; }}
  );

  function scheme() {{
    return document.body.getAttribute("data-md-color-scheme") === "slate" ? "dark" : "light";
  }}

  function draw() {{
    var mode = scheme();
    mermaid.initialize({{
      startOnLoad: false,
      securityLevel: "loose",
      theme: mode === "dark" ? "dark" : "neutral",
      fontFamily: FONT,
      themeVariables: variables(mode)
    }});
    blocks.forEach(function (block) {{
      var source = block.source + "\\n" + CLASSDEFS[mode].join("\\n");
      mermaid.render("bp-graph-" + counter++, source).then(function (result) {{
        block.element.innerHTML = result.svg;
        if (result.bindFunctions) result.bindFunctions(block.element);
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
    warnings: list[str] = field(default_factory=list)


class PublicationError(ValueError):
    """The requested static publication could expose or destroy local state."""

    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


class _PublicationRecoveryError(PublicationError):
    """A publication result is uncertain and recovery material must be retained."""


@dataclass(frozen=True, slots=True)
class _DestinationState:
    """The exact destination generation a render is allowed to replace."""

    kind: str
    identity: tuple[int, int] | None = None
    manifest_sha256: str | None = None
    directories: tuple[str, ...] = ()
    files: tuple[tuple[str, str], ...] = ()
    source_revision: str | None = None
    lean_source_revision: str | None = None


@dataclass(frozen=True, slots=True)
class _CleanupInventory:
    directories: tuple[tuple[str, tuple[int, ...]], ...]
    files: tuple[tuple[str, tuple[int, ...], str], ...]


@dataclass(slots=True)
class _PublicationCommitState:
    """Whether the filesystem commit may have run and was fully verified."""

    attempted: bool = False
    verified: bool = False


def render_site(
    blueprint_dir: str | Path,
    output_dir: str | Path,
    *,
    lean_root: str | Path | None = None,
    repository_url: str | None = None,
    ref: str | None = None,
    clean: bool = True,
    _expected_blueprint_identity: tuple[int, int] | None = None,
    _expected_roadmap_identity: tuple[int, int] | None = None,
) -> RenderReport:
    """Atomically publish deterministic projections of one blueprint snapshot.

    Rendering happens beside the destination. The previous publication remains
    live until the new tree is complete, its source revision is still current,
    and the destination generation inspected at startup is still present.
    """
    blueprint = Path(blueprint_dir).expanduser().resolve()
    requested_destination = Path(output_dir).expanduser().absolute()
    destination = requested_destination.parent.resolve() / requested_destination.name
    _require_publication_platform()
    if not workspace_module._DIRECTORY_BINDING_SUPPORTED:
        raise PublicationError(
            ["publication requires descriptor-bound directory traversal on this platform"]
        )
    if destination.is_symlink():
        raise PublicationError(["refusing symlink output directory"])
    if _publication_paths_overlap(destination, blueprint):
        raise PublicationError(
            ["blueprint and output directories must be disjoint; refusing destructive render"]
        )
    expected_children = (
        {"roadmap": _expected_roadmap_identity}
        if _expected_roadmap_identity is not None
        else None
    )
    try:
        with bind_directory_tree(
            blueprint,
            expected_identity=_expected_blueprint_identity,
            expected_children=expected_children,
            selection=_PUBLICATION_SNAPSHOT_SELECTION,
        ) as source_tree:
            source_snapshot = source_tree.capture()
            _validate_publication_snapshot(source_snapshot)
            return _render_bound_site(
                blueprint,
                destination,
                source_tree=source_tree,
                source_snapshot=source_snapshot,
                lean_root=lean_root,
                repository_url=repository_url,
                ref=ref,
                clean=clean,
            )
    except TreeSnapshotError as error:
        raise PublicationError(
            ["blueprint changed during publication; previous site was preserved"]
        ) from error


def _render_bound_site(
    blueprint: Path,
    destination: Path,
    *,
    source_tree: BoundDirectoryTree,
    source_snapshot: TreeSnapshot,
    lean_root: str | Path | None,
    repository_url: str | None,
    ref: str | None,
    clean: bool,
) -> RenderReport:
    """Render and publish bytes captured from one retained blueprint root."""

    with tempfile.TemporaryDirectory(prefix="autoform-render-input-") as temporary:
        inspection_root = Path(temporary) / "blueprint"
        source_snapshot.materialize(inspection_root)
        _, coverage = _load_publication_contract(inspection_root)
    source_root_name = _source_root_name(source_snapshot)
    canonical_source_tails = _canonical_source_tails(source_snapshot)
    if coverage.schema == COVERAGE_V2_SCHEMA:
        _require_canonical_sources_directory(source_snapshot)
    excluded_source_root = (
        Path(SOURCES_DIR) if coverage.schema == COVERAGE_V2_SCHEMA else None
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected_destination = _inspect_destination(destination)
    expected_destination_inventory = (
        None
        if expected_destination.kind == "absent"
        else _cleanup_inventory(
            destination,
            expected_identity=expected_destination.identity,
        )
    )
    if expected_destination_inventory is not None:
        _require_destination_inventory(expected_destination_inventory, expected_destination)
    repo_root = (
        Path(os.path.abspath(Path(lean_root).expanduser()))
        if lean_root is not None
        else blueprint.parent
    )
    workspace, workspace_identity = _create_workspace(destination.parent, destination.name)
    remove_workspace = True
    publication_succeeded = False
    commit_state = _PublicationCommitState()
    report: RenderReport | None = None
    snapshot_identity: tuple[int, int] | None = None
    stage_identity: tuple[int, int] | None = None
    source_cleanup_inventory: _CleanupInventory | None = None
    stage_cleanup_inventory: _CleanupInventory | None = None
    lean_sources: BoundProjectSources | None = None
    active_failure: BaseException | None = None
    try:
        snapshot = workspace / "source"
        publication_snapshot = (
            _snapshot_without_subtree(source_snapshot, PurePosixPath(SOURCES_DIR))
            if excluded_source_root is not None
            else source_snapshot
        )
        publication_snapshot.materialize(snapshot)
        snapshot_identity = _directory_path_identity(snapshot)
        # Every v2 raw artifact is constrained below ``sources/``. Excluding
        # that authority root also purges renamed artifacts carried by an older
        # incremental publication.
        source_revision = _source_revision(snapshot, excluded_root=excluded_source_root)
        source_cleanup_inventory = _cleanup_inventory(
            snapshot,
            expected_identity=snapshot_identity,
        )
        source_generation_revision = source_snapshot.generation_revision

        def require_source_generation() -> None:
            try:
                current = source_tree.capture()
            except TreeSnapshotError as error:
                raise PublicationError(
                    ["blueprint changed during publication; previous site was preserved"]
                ) from error
            if current.generation_revision != source_generation_revision:
                raise PublicationError(
                    ["blueprint changed during publication; previous site was preserved"]
                )

        lean_exclusions = (destination, workspace)
        lean_sources = _open_lean_sources(repo_root, exclude_roots=lean_exclusions)
        lean_snapshot = _capture_bound_lean_source_snapshot(lean_sources)
        lean_source_revision = lean_snapshot.revision
        lean_generation_revision = lean_snapshot.generation_revision
        resolved_repository_url = repository_url or detect_repository_url(repo_root)
        resolved_ref = ref or detect_ref(repo_root)

        def require_lean_generation() -> None:
            _require_bound_lean_source_revision(
                lean_sources,
                lean_generation_revision,
            )

        linker = build_linker(
            repo_root,
            repository_url=resolved_repository_url,
            ref=resolved_ref,
            exclude_roots=lean_exclusions,
            source_index=lean_snapshot.index,
            detect_missing=False,
        )
        require_source_generation()
        _require_snapshot_revision(
            snapshot, source_revision, snapshot_identity, workspace
        )
        require_lean_generation()
        stage = workspace / "site"
        stage_identity = _create_stage_directory(workspace, workspace_identity)
        if not clean and expected_destination.kind == "owned":
            _copy_owned_publication(
                destination,
                stage,
                expected_destination,
                excluded_root=excluded_source_root,
            )
        report = _render_snapshot(
            snapshot,
            stage,
            repo_root=repo_root,
            repo_root_identity=lean_sources.tree.identity,
            linker=linker,
            source_blueprint=blueprint,
            source_revision=source_revision,
            lean_source_revision=lean_source_revision,
            coverage=coverage,
            source_root_name=source_root_name,
            canonical_source_tails=canonical_source_tails,
        )
        stage_cleanup_inventory = _cleanup_inventory(
            stage,
            expected_identity=stage_identity,
        )
        require_source_generation()
        _require_snapshot_revision(
            snapshot, source_revision, snapshot_identity, workspace
        )
        require_lean_generation()
        try:
            _sync_tree(stage)
        except (OSError, PublicationError) as error:
            raise _PublicationRecoveryError(
                [f"publication stage integrity failed; recovery material was retained at {workspace}"]
            ) from error
        require_source_generation()
        _require_snapshot_revision(
            snapshot, source_revision, snapshot_identity, workspace
        )
        require_lean_generation()
        staged = _inspect_destination(stage)
        if (
            staged.kind != "owned"
            or staged.identity != stage_identity
            or staged.source_revision != source_revision
            or staged.lean_source_revision != lean_source_revision
        ):
            raise _PublicationRecoveryError(
                [f"publication stage changed; recovery material was retained at {workspace}"]
            )
        synced_stage_inventory = _cleanup_inventory(
            stage,
            expected_identity=stage_identity,
        )
        if synced_stage_inventory != stage_cleanup_inventory:
            raise _PublicationRecoveryError(
                [f"publication stage changed; recovery material was retained at {workspace}"]
            )
        _require_destination_inventory(stage_cleanup_inventory, staged)
        _publish_staged_site(
            stage,
            destination,
            expected_destination,
            staged,
            commit_state=commit_state,
            source_blueprint=blueprint,
            source_snapshot=snapshot,
            source_snapshot_identity=snapshot_identity,
            source_revision=source_revision,
            lean_root=repo_root,
            lean_source_revision=lean_source_revision,
            lean_exclusions=lean_exclusions,
            excluded_source_root=excluded_source_root,
            expected_coverage=coverage,
            source_guard=require_source_generation,
            lean_guard=require_lean_generation,
        )
        report.output_dir = destination
        publication_succeeded = True
        return report
    except _PublicationRecoveryError:
        remove_workspace = False
        raise
    except BaseException as error:
        active_failure = error
        raise
    finally:
        try:
            if commit_state.attempted and not commit_state.verified:
                remove_workspace = False
            expected_children: dict[
                str,
                dict[tuple[int, int], _CleanupInventory],
            ] = {}
            if snapshot_identity is not None and source_cleanup_inventory is not None:
                expected_children["source"] = {
                    snapshot_identity: source_cleanup_inventory,
                }
            if stage_identity is not None and stage_cleanup_inventory is not None:
                site_inventories = {stage_identity: stage_cleanup_inventory}
                if (
                    expected_destination.identity is not None
                    and expected_destination_inventory is not None
                ):
                    site_inventories[expected_destination.identity] = (
                        expected_destination_inventory
                    )
                expected_children["site"] = site_inventories
            if remove_workspace and not _remove_owned_workspace(
                workspace, workspace_identity, expected_children=expected_children
            ):
                issue = (
                    "publication staging workspace changed; cleanup was refused at "
                    f"{workspace}"
                )
                if commit_state.verified and report is not None:
                    report.warnings.append(issue)
                elif publication_succeeded:
                    raise PublicationError([issue])
                elif active_failure is not None:
                    raise PublicationError(
                        [
                            f"publication failed: {active_failure}; {issue}; "
                            "recovery material was retained"
                        ]
                    ) from active_failure
        finally:
            if lean_sources is not None:
                lean_sources.close()


def _snapshot_without_subtree(
    snapshot: TreeSnapshot,
    excluded: PurePosixPath,
) -> TreeSnapshot:
    def retained(relative: str) -> bool:
        return not _is_excluded_relative(PurePosixPath(relative), excluded)

    return TreeSnapshot(
        root_identity=snapshot.root_identity,
        directories=tuple(path for path in snapshot.directories if retained(path)),
        files=tuple(entry for entry in snapshot.files if retained(entry[0])),
        symlinks=tuple(entry for entry in snapshot.symlinks if retained(entry[0])),
        special=tuple(entry for entry in snapshot.special if retained(entry[0])),
        placeholders=tuple(path for path in snapshot.placeholders if retained(path)),
        omitted=tuple(entry for entry in snapshot.omitted if retained(entry[0])),
        identities=tuple(entry for entry in snapshot.identities if retained(entry[0])),
    )


def _render_snapshot(
    blueprint_dir: str | Path,
    output_dir: str | Path,
    *,
    repo_root: Path,
    repo_root_identity: tuple[int, int],
    linker: SourceLinker,
    source_blueprint: Path,
    source_revision: str,
    lean_source_revision: str,
    coverage: CoverageSummary,
    source_root_name: str,
    canonical_source_tails: dict[tuple[str, ...], tuple[str, ...]],
) -> RenderReport:
    """Write one already-frozen blueprint into an isolated staging tree.

    Authored Markdown remains the only graph authority. The output joins three
    reader surfaces over it: a book, derived progress, and multiscale dependency
    maps. Publication excludes hidden and operational files, rejects symlinks,
    and never embeds timestamps or machine-specific paths.
    """
    blueprint = Path(blueprint_dir).expanduser().resolve()
    destination = Path(output_dir).resolve()
    _validate_publication_tree(blueprint)

    graph = load_graph(blueprint)
    contract_path = blueprint / coverage.source_path
    if hashlib.sha256(_read_regular_file(contract_path)).hexdigest() != coverage.source_sha256:
        raise PublicationError(["coverage contract changed in the publication snapshot"])
    statuses = status.derive(graph)
    # The repository root, not the vault's parent. A blueprint nested at
    # <repo>/docs/blueprint would otherwise be described as <repo>/blueprint,
    # and every generated permalink would 404.
    numbers = _number_nodes(graph)
    used_by = _reverse_edges(graph)
    sources_base = _sources_base(
        source_blueprint,
        repo_root_identity,
        linker,
        source_root_name=source_root_name,
        canonical_source_tails=canonical_source_tails,
    )
    unpublished_source_root = (
        blueprint / SOURCES_DIR if coverage.schema == COVERAGE_V2_SCHEMA else None
    )

    report = RenderReport(output_dir=destination)
    node_paths = {node.path.resolve(): node for node in graph.nodes.values()}
    # Nodes are published as environments on their milestone page, the way a
    # blueprint chapter carries many statements in sequence. Each keeps an
    # anchor so every cross-reference still lands on the statement itself.
    groups = _group_nodes(graph)
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
            if graph.children(node_id) or not node.formalizable
        }
    )
    node_sources = {
        node.path.resolve(): node_id for node_id, node in graph.nodes.items()
    }

    for source in sorted(blueprint.rglob("*")):
        relative = source.relative_to(blueprint)
        if _SKIPPED_DIRECTORIES.intersection(relative.parts) or _is_hidden(relative):
            continue
        if _is_generated_path(relative):
            continue
        # Source notes leave the site entirely once readers can reach them in
        # the repository, so the book has one reference surface rather than two.
        if sources_base is not None and _is_excluded_relative(
            relative, Path(SOURCES_DIR)
        ):
            continue
        target = destination / relative
        # Directories are created on demand below, so a directory holding
        # nothing but absorbed nodes leaves no empty shell behind.
        if source.is_dir():
            continue
        # Narrative articles remain book pages. Only formalizable leaves are
        # consolidated into their containing article with stable anchors.
        article = node_paths.get(source.resolve())
        if article is not None and article.formalizable and not graph.children(article.id):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() == ".md":
            rewritten = _rewrite_links(
                source.read_text(encoding="utf-8"),
                source_dir=source.parent,
                page=target,
                blueprint=blueprint,
                destination=destination,
                node_sources=node_sources,
                targets=targets,
                sources_base=sources_base,
                unpublished_source_root=unpublished_source_root,
            )
            target.write_text(rewritten, encoding="utf-8")
        else:
            shutil.copy2(source, target)
        report.pages += 1

    overview = destination / "README.md"
    if overview.is_file():
        overview.write_text(
            _render_landing_page(
                overview.read_text(encoding="utf-8"),
                graph=graph,
                statuses=statuses,
                groups=groups,
                group_pages=group_pages,
                page=overview,
                destination=destination,
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
            sources_base=sources_base,
            unpublished_source_root=unpublished_source_root,
            source_blueprint=source_blueprint,
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
            blueprint,
            graph,
            statuses,
            page=structure,
            targets=targets,
            sources_base=sources_base,
        ),
        encoding="utf-8",
    )
    report.pages += 1
    (destination / "SUMMARY.md").write_text(
        _render_summary_nav(book_pages, destination=destination, overview=overview),
        encoding="utf-8",
    )

    generated_graph_pages = graph_pages.write_graph_pages(
        graph,
        statuses,
        destination,
        node_links=lambda page: _anchored_links(targets, page),
    )
    report.pages += len(generated_graph_pages)

    for relative, contents in (
        (STYLESHEET, _stylesheet()),
        (MERMAID_SCRIPT, _mermaid_script()),
        (LOGO, _logo()),
    ):
        asset = destination / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(contents, encoding="utf-8")
    if unpublished_source_root is not None:
        _reject_staged_excluded_raw_html_links(destination, Path(SOURCES_DIR))
    _write_publication_manifest(
        destination,
        graph,
        linker,
        coverage=coverage,
        complete=True,
        source_revision=source_revision,
        lean_source_revision=lean_source_revision,
    )
    return report


def _inspect_destination(destination: Path) -> _DestinationState:
    """Return the exact safe generation at *destination*, or fail closed."""
    try:
        parent_descriptor = _open_directory_path(destination.parent)
    except OSError as error:
        raise PublicationError(["could not inspect the output directory safely"]) from error
    try:
        return _inspect_destination_at(parent_descriptor, destination.name, destination)
    finally:
        os.close(parent_descriptor)


def _inspect_destination_at(
    parent_descriptor: int, name: str, display_path: Path
) -> _DestinationState:
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return _DestinationState("absent")
    except OSError as error:
        raise PublicationError(["could not inspect the output directory safely"]) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise PublicationError(["refusing symlink output directory"])
    if not stat.S_ISDIR(metadata.st_mode):
        raise PublicationError(["output path exists and is not a directory"])
    identity = metadata.st_dev, metadata.st_ino
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise PublicationError(["could not inspect the output directory safely"]) from error
    try:
        if _descriptor_identity(descriptor) != identity:
            raise PublicationError(["output directory changed while it was inspected"])
        if not os.listdir(descriptor):
            current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if _descriptor_identity(descriptor) != identity or (
                current.st_dev,
                current.st_ino,
            ) != identity or os.listdir(descriptor):
                raise PublicationError(["output directory changed while it was inspected"])
            return _DestinationState("empty", identity=identity)

        try:
            manifest_bytes = _read_regular_file_at(
                descriptor, PUBLICATION_MANIFEST, display_path / PUBLICATION_MANIFEST
            )
            publication = json.loads(manifest_bytes.decode("utf-8"))
        except OSError as error:
            raise PublicationError(
                ["publication output changed while it was inspected"]
            ) from error
        except (UnicodeError, json.JSONDecodeError, PublicationError):
            publication = None
            manifest_bytes = b""
        if not isinstance(publication, dict) or publication.get("schema") != PUBLICATION_SCHEMA:
            raise PublicationError(
                [
                    "refusing to overwrite a non-Autoform output directory or legacy "
                    "publication; choose an empty directory or remove it explicitly"
                ]
            )
        canonical_manifest = (json.dumps(publication, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        if manifest_bytes != canonical_manifest:
            raise PublicationError(["publication manifest is not in canonical form"])
        if publication.get("complete") is not True:
            raise PublicationError(["refusing to overwrite an incomplete Autoform publication"])
        expected_files = _parse_inventory_files(publication.get("files"))
        expected_directories = _parse_inventory_directories(publication.get("directories"))
        source_revision = publication.get("source_revision")
        lean_source_revision = publication.get("lean_source_revision")
        if (
            not isinstance(source_revision, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_revision) is None
            or (
                lean_source_revision is not None
                and (
                    not isinstance(lean_source_revision, str)
                    or re.fullmatch(r"[0-9a-f]{64}", lean_source_revision) is None
                )
            )
        ):
            raise PublicationError(["publication manifest has invalid source revisions"])
        actual_directories, actual_files = _publication_inventory_descriptor(descriptor)
        if actual_directories != expected_directories:
            raise PublicationError(
                ["refusing to overwrite an output directory with untracked or missing directories"]
            )
        if tuple(path for path, _ in actual_files) != tuple(path for path, _ in expected_files):
            expected_paths = {path for path, _ in expected_files}
            actual_paths = {path for path, _ in actual_files}
            difference = sorted(expected_paths ^ actual_paths)
            raise PublicationError(
                [
                    "refusing to overwrite an output directory with untracked or missing files: "
                    + ", ".join(difference)
                ]
            )
        if actual_files != expected_files:
            raise PublicationError(["refusing to overwrite a modified Autoform publication"])
        if (
            _read_regular_file_at(
                descriptor, PUBLICATION_MANIFEST, display_path / PUBLICATION_MANIFEST
            )
            != manifest_bytes
            or _publication_inventory_descriptor(descriptor)
            != (actual_directories, actual_files)
        ):
            raise PublicationError(["publication output changed while it was inspected"])
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _descriptor_identity(descriptor) != identity or (
            current.st_dev,
            current.st_ino,
        ) != identity:
            raise PublicationError(["output directory changed while it was inspected"])
        return _DestinationState(
            "owned",
            identity=identity,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            directories=expected_directories,
            files=expected_files,
            source_revision=source_revision,
            lean_source_revision=lean_source_revision,
        )
    finally:
        os.close(descriptor)


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise PublicationError(["output path changed while it was inspected"])
    return metadata.st_dev, metadata.st_ino


def _stat_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_path_identity(path: Path) -> tuple[int, int]:
    descriptor = _open_directory_path(path)
    try:
        return _descriptor_identity(descriptor)
    finally:
        os.close(descriptor)


def _require_publication_platform() -> None:
    required_options = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    if (
        fcntl is None
        or any(not hasattr(os, option) for option in required_options)
        or os.open not in os.supports_dir_fd
        or os.rename not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
    ):
        raise PublicationError(
            ["transactional publication is unavailable on this platform"]
        )
    _rename_implementation(exchange=False)
    _rename_implementation(exchange=True)


def _open_directory_path(path: Path) -> int:
    """Open every component without following a symbolic link."""
    absolute = path.absolute()
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _create_workspace(parent: Path, destination_name: str) -> tuple[Path, tuple[int, int]]:
    parent_descriptor = _open_directory_path(parent)
    try:
        for _ in range(128):
            name = (
                f"{_PUBLICATION_STAGE_PREFIX}{destination_name}-"
                f"{secrets.token_hex(8)}"
            )
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                continue
            metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            identity = metadata.st_dev, metadata.st_ino
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_descriptor,
                )
                if _descriptor_identity(descriptor) != identity:
                    raise OSError(errno.ESTALE, "workspace changed during creation")
            except BaseException:
                try:
                    current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == identity:
                        os.rmdir(name, dir_fd=parent_descriptor)
                except OSError:
                    pass
                raise
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            return parent / name, identity
    finally:
        os.close(parent_descriptor)
    raise PublicationError(["could not create a private publication workspace"])


def _create_stage_directory(
    workspace: Path, workspace_identity: tuple[int, int]
) -> tuple[int, int]:
    workspace_descriptor = _open_directory_path(workspace)
    try:
        if _descriptor_identity(workspace_descriptor) != workspace_identity:
            raise PublicationError(["publication workspace changed before staging"])
        os.mkdir("site", mode=0o700, dir_fd=workspace_descriptor)
        metadata = os.stat("site", dir_fd=workspace_descriptor, follow_symlinks=False)
        descriptor = os.open(
            "site",
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=workspace_descriptor,
        )
        try:
            identity = _descriptor_identity(descriptor)
            if identity != (metadata.st_dev, metadata.st_ino):
                raise PublicationError(["publication stage changed during creation"])
            return identity
        finally:
            os.close(descriptor)
    finally:
        os.close(workspace_descriptor)


def _remove_owned_workspace(
    workspace: Path,
    identity: tuple[int, int],
    *,
    expected_children: dict[str, dict[tuple[int, int], _CleanupInventory]],
) -> bool:
    """Remove only inventoried trees in Autoform's random mode-0700 workspace.

    The inventory and atomic per-file claim protect against ordinary concurrent
    path replacement. POSIX has no unlink-if-inode operation, so code running
    as the same user must not be given a path or callback into this private
    workspace while cleanup is active.
    """

    parent_descriptor: int | None = None
    workspace_descriptor: int | None = None
    try:
        parent_descriptor = _open_directory_path(workspace.parent)
        metadata = os.stat(
            workspace.name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if not stat.S_ISDIR(metadata.st_mode) or (
            metadata.st_dev,
            metadata.st_ino,
        ) != identity:
            return False
        workspace_descriptor = os.open(
            workspace.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        if _descriptor_identity(workspace_descriptor) != identity:
            return False
        names = set(os.listdir(workspace_descriptor))
        if not names.issubset(expected_children):
            return False
        for name in names:
            child = os.stat(name, dir_fd=workspace_descriptor, follow_symlinks=False)
            child_identity = (child.st_dev, child.st_ino)
            expected_inventory = expected_children[name].get(child_identity)
            if not stat.S_ISDIR(child.st_mode) or expected_inventory is None:
                return False
            child_descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=workspace_descriptor,
            )
            try:
                if _descriptor_identity(child_descriptor) != child_identity:
                    return False
                if _cleanup_inventory_descriptor(child_descriptor) != expected_inventory:
                    return False
            finally:
                os.close(child_descriptor)
        for name in sorted(names):
            current = os.stat(name, dir_fd=workspace_descriptor, follow_symlinks=False)
            child_identity = (current.st_dev, current.st_ino)
            expected_inventory = expected_children[name].get(child_identity)
            if not stat.S_ISDIR(current.st_mode) or expected_inventory is None:
                return False
            child_descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=workspace_descriptor,
            )
            try:
                _remove_inventory_contents(
                    child_descriptor,
                    expected_inventory,
                )
            finally:
                os.close(child_descriptor)
            os.rmdir(name, dir_fd=workspace_descriptor)
        current = os.stat(
            workspace.name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if (current.st_dev, current.st_ino) != identity:
            return False
        os.rmdir(workspace.name, dir_fd=parent_descriptor)
        return True
    except FileNotFoundError:
        return True
    except (OSError, PublicationError):
        return False
    finally:
        if workspace_descriptor is not None:
            os.close(workspace_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _cleanup_inventory(
    root: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> _CleanupInventory:
    descriptor = _open_directory_path(root)
    try:
        if expected_identity is not None and _descriptor_identity(descriptor) != expected_identity:
            raise PublicationError(["publication tree changed before cleanup inventory"])
        return _cleanup_inventory_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_inventory_descriptor(descriptor: int) -> _CleanupInventory:
    directories: list[tuple[str, tuple[int, ...]]] = []
    files: list[tuple[str, tuple[int, ...], str]] = []

    def visit(current: int, prefix: str) -> None:
        names = tuple(sorted(os.listdir(current)))
        for name in names:
            relative = f"{prefix}/{name}" if prefix else name
            metadata = os.stat(name, dir_fd=current, follow_symlinks=False)
            identity = _stat_signature(metadata)
            if stat.S_ISDIR(metadata.st_mode):
                directories.append((relative, identity))
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
                try:
                    if _stat_signature(os.fstat(child)) != identity:
                        raise OSError(errno.ESTALE, "workspace changed during cleanup inventory")
                    visit(child, relative)
                finally:
                    os.close(child)
                if _stat_signature(
                    os.stat(name, dir_fd=current, follow_symlinks=False)
                ) != identity:
                    raise OSError(errno.ESTALE, "workspace changed during cleanup inventory")
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(errno.ESTALE, "workspace contains an unowned filesystem entry")
            data = _read_regular_file_at(current, name, Path(relative))
            after = os.stat(name, dir_fd=current, follow_symlinks=False)
            if _stat_signature(after) != identity:
                raise OSError(errno.ESTALE, "workspace changed during cleanup inventory")
            files.append((relative, identity, hashlib.sha256(data).hexdigest()))
        if tuple(sorted(os.listdir(current))) != names:
            raise OSError(errno.ESTALE, "workspace changed during cleanup inventory")

    visit(descriptor, "")
    return _CleanupInventory(tuple(sorted(directories)), tuple(sorted(files)))


def _remove_inventory_contents(
    descriptor: int,
    inventory: _CleanupInventory,
    *,
    prefix: str = "",
) -> None:
    directories = dict(inventory.directories)
    files = {
        relative: (identity, digest)
        for relative, identity, digest in inventory.files
    }
    expected_names: set[str] = set()
    prefix_path = PurePosixPath(prefix) if prefix else None
    for relative in (*directories, *files):
        path = PurePosixPath(relative)
        if prefix_path is None:
            expected_names.add(path.parts[0])
            continue
        if path == prefix_path or not path.is_relative_to(prefix_path):
            continue
        expected_names.add(path.relative_to(prefix_path).parts[0])
    names = set(os.listdir(descriptor))
    if names != expected_names:
        raise OSError(errno.ESTALE, "workspace changed during cleanup")
    for name in sorted(names):
        relative = f"{prefix}/{name}" if prefix else name
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if relative in directories:
            if _stat_signature(metadata) != directories[relative]:
                raise OSError(errno.ESTALE, "workspace changed during cleanup")
            child = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            try:
                if _stat_signature(os.fstat(child)) != directories[relative]:
                    raise OSError(errno.ESTALE, "workspace changed during cleanup")
                _remove_inventory_contents(child, inventory, prefix=relative)
            finally:
                os.close(child)
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or (
                current.st_dev,
                current.st_ino,
            ) != (metadata.st_dev, metadata.st_ino):
                raise OSError(errno.ESTALE, "workspace changed during cleanup")
            os.rmdir(name, dir_fd=descriptor)
            continue
        expected_file = files.get(relative)
        if expected_file is None or _stat_signature(metadata) != expected_file[0]:
            raise OSError(errno.ESTALE, "workspace changed during cleanup")
        quarantine = f".autoform-cleanup-{secrets.token_hex(16)}"
        _cleanup_rename_noreplace(descriptor, name, quarantine)
        try:
            claimed = os.stat(
                quarantine,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            data = _read_regular_file_at(descriptor, quarantine, Path(relative))
            final = os.stat(
                quarantine,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if (
                _rename_stable_signature(claimed) != expected_file[0][:-1]
                or _rename_stable_signature(final) != expected_file[0][:-1]
                or hashlib.sha256(data).hexdigest() != expected_file[1]
            ):
                raise OSError(errno.ESTALE, "workspace changed during cleanup")
        except BaseException:
            try:
                _cleanup_rename_noreplace(descriptor, quarantine, name)
            except BaseException:
                pass
            raise
        os.unlink(quarantine, dir_fd=descriptor)
    if os.listdir(descriptor):
        raise OSError(errno.ESTALE, "workspace changed during cleanup")


def _cleanup_rename_noreplace(descriptor: int, source: str, target: str) -> None:
    """Claim a cleanup entry without using the publication commit hooks."""

    function, flag = _rename_implementation(exchange=False)
    result = function(
        descriptor,
        os.fsencode(source),
        descriptor,
        os.fsencode(target),
        flag,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _rename_stable_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _require_destination_inventory(
    inventory: _CleanupInventory,
    state: _DestinationState,
) -> None:
    directories = tuple(path for path, _identity in inventory.directories)
    files = tuple(
        (path, digest)
        for path, _identity, digest in inventory.files
        if path != PUBLICATION_MANIFEST
    )
    manifests = tuple(
        digest
        for path, _identity, digest in inventory.files
        if path == PUBLICATION_MANIFEST
    )
    if state.kind == "empty":
        valid = not directories and not files and not manifests
    else:
        valid = (
            state.kind == "owned"
            and directories == state.directories
            and files == state.files
            and manifests == (state.manifest_sha256,)
        )
    if not valid:
        raise PublicationError(["publication tree changed before cleanup inventory"])


def _parse_inventory_files(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        raise PublicationError(["publication manifest has no valid file inventory"])
    files: list[tuple[str, str]] = []
    for path, digest in value.items():
        if (
            not isinstance(path, str)
            or not _valid_inventory_path(path)
            or path == PUBLICATION_MANIFEST
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise PublicationError(["publication manifest has an invalid file inventory"])
        files.append((path, digest))
    if files != sorted(files):
        raise PublicationError(["publication manifest file inventory is not canonical"])
    return tuple(files)


def _parse_inventory_directories(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PublicationError(["publication manifest has no valid directory inventory"])
    if any(not isinstance(path, str) or not _valid_inventory_path(path) for path in value):
        raise PublicationError(["publication manifest has an invalid directory inventory"])
    if value != sorted(set(value)):
        raise PublicationError(["publication manifest directory inventory is not canonical"])
    return tuple(value)


def _valid_inventory_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and value != "."
        and "\x00" not in value
        and "\\" not in value
        and not path.is_absolute()
        and path.as_posix() == value
        and ".." not in path.parts
    )


def _publication_inventory(root: Path) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    descriptor = _open_directory_path(root)
    try:
        return _publication_inventory_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _publication_inventory_descriptor(
    root_descriptor: int,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    directories: list[str] = []
    files: list[tuple[str, str]] = []

    def visit(descriptor: int, prefix: str) -> None:
        try:
            names = sorted(os.listdir(descriptor))
            for name in names:
                relative = f"{prefix}/{name}" if prefix else name
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise PublicationError(
                        [f"refusing symlink in publication output: {relative}"]
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    directories.append(relative)
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    try:
                        if _descriptor_identity(child) != (metadata.st_dev, metadata.st_ino):
                            raise PublicationError(
                                ["publication output changed while it was inspected"]
                            )
                        visit(child, relative)
                    finally:
                        os.close(child)
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise PublicationError(
                            ["publication output changed while it was inspected"]
                        )
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise PublicationError(
                        [f"refusing non-regular publication output: {relative}"]
                    )
                if relative == PUBLICATION_MANIFEST:
                    continue
                data = _read_regular_file_at(descriptor, name, Path(relative))
                files.append((relative, hashlib.sha256(data).hexdigest()))
            if sorted(os.listdir(descriptor)) != names:
                raise PublicationError(
                    ["publication output changed while it was inspected"]
                )
        except OSError as error:
            raise PublicationError(
                ["publication output changed while it was inspected"]
            ) from error

    visit(root_descriptor, "")
    return tuple(sorted(directories)), tuple(sorted(files))


def _copy_owned_publication(
    source: Path,
    destination: Path,
    state: _DestinationState,
    *,
    excluded_root: Path | None = None,
) -> None:
    """Seed a non-clean render from the exact previously verified generation."""
    for relative in state.directories:
        if _is_excluded_relative(Path(relative), excluded_root):
            continue
        (destination / relative).mkdir(parents=True, exist_ok=True)
    for relative, expected_digest in state.files:
        if _is_excluded_relative(Path(relative), excluded_root):
            continue
        data = _read_regular_file(source / relative)
        if hashlib.sha256(data).hexdigest() != expected_digest:
            raise PublicationError(["publication output changed while it was copied"])
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _snapshot_publication_tree(
    source: Path,
    destination: Path,
    *,
    excluded_root: Path | None = None,
) -> str:
    """Copy and hash the exact source generation used by one render."""
    destination.mkdir(mode=0o700)
    digest = hashlib.sha256(b"autoform-markdown-publication/v2\0")
    for path, relative in _published_source_files(source):
        if _is_excluded_relative(relative, excluded_root):
            continue
        data = _read_regular_file(path)
        _update_source_digest(digest, relative, data)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return digest.hexdigest()


def _update_source_digest(digest, relative: Path, data: bytes) -> None:
    path = os.fsencode(relative.as_posix())
    digest.update(len(path).to_bytes(8, "big"))
    digest.update(path)
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def _read_regular_file(path: Path) -> bytes:
    try:
        parent_descriptor = _open_directory_path(path.parent)
    except OSError as error:
        raise PublicationError(
            [f"could not safely read regular file: {path.name}"]
        ) from error
    try:
        return _read_regular_file_at(parent_descriptor, path.name, path)
    finally:
        os.close(parent_descriptor)


def _read_regular_file_at(parent_descriptor: int, name: str, display_path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise PublicationError(
            [f"could not safely read regular file: {display_path.name}"]
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PublicationError([f"refusing non-regular file: {display_path.name}"])
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise PublicationError([f"file changed while it was read: {display_path.name}"])
        entry = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (entry.st_dev, entry.st_ino) != (after.st_dev, after.st_ino):
            raise PublicationError([f"file changed while it was read: {display_path.name}"])
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _require_source_revision(
    blueprint: Path,
    expected: str,
    *,
    excluded_root: Path | None = None,
    expected_coverage: CoverageSummary | None = None,
) -> None:
    if _source_revision(blueprint, excluded_root=excluded_root) != expected:
        raise PublicationError(["blueprint changed during publication; previous site was preserved"])
    if expected_coverage is not None:
        current, issues = load_coverage(blueprint)
        if issues or current != expected_coverage:
            raise PublicationError(
                ["coverage or source artifact changed during publication; previous site was preserved"]
            )


def _require_snapshot_revision(
    snapshot: Path,
    expected: str,
    expected_identity: tuple[int, int],
    workspace: Path,
) -> None:
    try:
        before_identity = _directory_path_identity(snapshot)
        actual = _source_revision(snapshot)
        after_identity = _directory_path_identity(snapshot)
    except (OSError, PublicationError) as error:
        raise _PublicationRecoveryError(
            [f"publication snapshot changed; recovery material was retained at {workspace}"]
        ) from error
    if (
        before_identity != expected_identity
        or after_identity != expected_identity
        or actual != expected
    ):
        raise _PublicationRecoveryError(
            [f"publication snapshot changed; recovery material was retained at {workspace}"]
        )


def _require_lean_source_revision(
    root: Path, expected: str, *, exclude_roots: Iterable[Path]
) -> None:
    try:
        actual = project_source_revision(root, exclude_roots=exclude_roots)
    except OSError as error:
        raise PublicationError(
            ["Lean sources changed during publication; previous site was preserved"]
        ) from error
    if actual != expected:
        raise PublicationError(
            ["Lean sources changed during publication; previous site was preserved"]
        )


def _capture_lean_source_snapshot(
    root: Path, *, exclude_roots: Iterable[Path]
) -> IndexedSourceSnapshot:
    try:
        return snapshot_project_sources(root, exclude_roots=exclude_roots)
    except OSError as error:
        raise PublicationError(["could not capture a stable Lean source revision"]) from error


def _open_lean_sources(
    root: Path, *, exclude_roots: Iterable[Path]
) -> BoundProjectSources:
    try:
        return open_project_sources(root, exclude_roots=exclude_roots)
    except (OSError, TreeSnapshotError) as error:
        raise PublicationError(["could not capture a stable Lean source revision"]) from error


def _capture_bound_lean_source_snapshot(
    sources: BoundProjectSources,
) -> IndexedSourceSnapshot:
    try:
        return sources.capture()
    except (OSError, TreeSnapshotError) as error:
        raise PublicationError(["could not capture a stable Lean source revision"]) from error


def _require_bound_lean_source_revision(
    sources: BoundProjectSources,
    expected_generation: str,
) -> None:
    try:
        sources.verify()
        actual = sources.capture().generation_revision
        sources.verify()
    except (OSError, TreeSnapshotError) as error:
        raise PublicationError(
            ["Lean sources changed during publication; previous site was preserved"]
        ) from error
    if actual != expected_generation:
        raise PublicationError(
            ["Lean sources changed during publication; previous site was preserved"]
        )


def _sync_tree(root: Path) -> None:
    """Make every staged byte and directory entry durable before publication."""
    root.chmod(0o755)
    directories = [root]
    for entry in sorted(root.rglob("*")):
        metadata = entry.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise PublicationError(["refusing symlink in staged publication"])
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(entry)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise PublicationError(["refusing non-regular staged publication entry"])
        descriptor = os.open(
            entry,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise PublicationError(["staged publication changed while syncing"])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        descriptor = os.open(
            directory,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _publish_staged_site(
    stage: Path,
    destination: Path,
    expected: _DestinationState,
    staged: _DestinationState,
    *,
    commit_state: _PublicationCommitState,
    source_blueprint: Path,
    source_snapshot: Path,
    source_snapshot_identity: tuple[int, int],
    source_revision: str,
    lean_root: Path,
    lean_source_revision: str,
    lean_exclusions: tuple[Path, ...],
    excluded_source_root: Path | None = None,
    expected_coverage: CoverageSummary | None = None,
    source_guard: Callable[[], None] | None = None,
    lean_guard: Callable[[], None] | None = None,
) -> None:
    """Commit *stage* if the destination still matches the inspected generation."""
    parent_descriptor: int | None = None
    stage_parent_descriptor: int | None = None
    try:
        parent_descriptor = _open_directory_path(destination.parent)
        stage_parent_descriptor = _open_directory_path(stage.parent)
        if staged.kind != "owned" or staged.identity is None or _inspect_destination_at(
            stage_parent_descriptor, stage.name, stage
        ) != staged:
            raise _PublicationRecoveryError(
                [f"publication stage changed; recovery material was retained at {stage.parent}"]
            )
        fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
        if _inspect_destination_at(
            parent_descriptor, destination.name, destination
        ) != expected:
            raise PublicationError(
                ["output directory changed during publication; previous site was preserved"]
            )
        if _inspect_destination_at(stage_parent_descriptor, stage.name, stage) != staged:
            raise _PublicationRecoveryError(
                [f"publication stage changed; recovery material was retained at {stage.parent}"]
            )
        if source_guard is None:
            _require_source_revision(
                source_blueprint,
                source_revision,
                excluded_root=excluded_source_root,
                expected_coverage=expected_coverage,
            )
        else:
            source_guard()
        _require_snapshot_revision(
            source_snapshot,
            source_revision,
            source_snapshot_identity,
            stage.parent,
        )
        if lean_guard is None:
            _require_lean_source_revision(
                lean_root, lean_source_revision, exclude_roots=lean_exclusions
            )
        else:
            lean_guard()
        if _inspect_destination_at(
            parent_descriptor, destination.name, destination
        ) != expected:
            raise PublicationError(["publication inputs changed at the commit boundary"])
        if _inspect_destination_at(stage_parent_descriptor, stage.name, stage) != staged:
            raise _PublicationRecoveryError(
                [f"publication stage changed; recovery material was retained at {stage.parent}"]
            )
        commit_state.attempted = True
        if expected.kind == "absent":
            _rename_noreplace(
                stage_parent_descriptor,
                stage.name,
                parent_descriptor,
                destination.name,
            )
        else:
            _rename_exchange(
                stage_parent_descriptor,
                stage.name,
                parent_descriptor,
                destination.name,
            )
            if _inspect_destination_at(
                stage_parent_descriptor, stage.name, stage
            ) != expected:
                raise PublicationError(
                    ["the displaced publication changed during the commit operation"]
                )
        published = _inspect_destination_at(
            parent_descriptor, destination.name, destination
        )
        if published != staged:
            raise PublicationError(
                ["the published generation changed before its final ownership check"]
            )
        if source_guard is None:
            _require_source_revision(
                source_blueprint,
                source_revision,
                excluded_root=excluded_source_root,
                expected_coverage=expected_coverage,
            )
        else:
            source_guard()
        _require_snapshot_revision(
            source_snapshot,
            source_revision,
            source_snapshot_identity,
            stage.parent,
        )
        if lean_guard is None:
            _require_lean_source_revision(
                lean_root, lean_source_revision, exclude_roots=lean_exclusions
            )
        else:
            lean_guard()
        os.fsync(stage_parent_descriptor)
        os.fsync(parent_descriptor)
    except BaseException as publication_error:
        if commit_state.attempted:
            try:
                _rollback_publication_commit(
                    stage,
                    destination,
                    expected,
                    staged,
                    parent_descriptor=parent_descriptor,
                    stage_parent_descriptor=stage_parent_descriptor,
                )
            except BaseException:
                raise _PublicationRecoveryError(
                    [
                        "publication commit began but its final state could not be verified; "
                        f"recovery material was retained at {stage.parent}"
                    ]
                ) from publication_error
            commit_state.attempted = False
        raise publication_error
    finally:
        close_error: BaseException | None = None
        for descriptor in (stage_parent_descriptor, parent_descriptor):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except BaseException as error:
                if close_error is None:
                    close_error = error
        if close_error is not None:
            if commit_state.attempted:
                raise _PublicationRecoveryError(
                    [
                        "publication commit began but its final state could not be verified; "
                        f"recovery material was retained at {stage.parent}"
                    ]
                ) from close_error
            raise close_error
    commit_state.verified = True


def _rollback_publication_commit(
    stage: Path,
    destination: Path,
    expected: _DestinationState,
    staged: _DestinationState,
    *,
    parent_descriptor: int,
    stage_parent_descriptor: int,
) -> None:
    """Restore the pre-commit destination when an input guard fails after rename."""

    if _inspect_destination_at(parent_descriptor, destination.name, destination) != staged:
        raise PublicationError(["published generation changed before rollback"])
    if expected.kind == "absent":
        if _inspect_destination_at(stage_parent_descriptor, stage.name, stage).kind != "absent":
            raise PublicationError(["publication stage changed before rollback"])
        _rename_noreplace(
            parent_descriptor,
            destination.name,
            stage_parent_descriptor,
            stage.name,
        )
    else:
        if _inspect_destination_at(stage_parent_descriptor, stage.name, stage) != expected:
            raise PublicationError(["displaced publication changed before rollback"])
        _rename_exchange(
            stage_parent_descriptor,
            stage.name,
            parent_descriptor,
            destination.name,
        )
    if _inspect_destination_at(parent_descriptor, destination.name, destination) != expected:
        raise PublicationError(["previous publication was not restored"])
    if _inspect_destination_at(stage_parent_descriptor, stage.name, stage) != staged:
        raise PublicationError(["new publication was not retained after rollback"])
    os.fsync(stage_parent_descriptor)
    os.fsync(parent_descriptor)


def _rename_noreplace(
    source_parent: int, source: str, target_parent: int, target: str
) -> None:
    function, flag = _rename_implementation(exchange=False)
    result = function(
        source_parent,
        os.fsencode(source),
        target_parent,
        os.fsencode(target),
        flag,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise PublicationError(
            ["output directory changed during publication; previous site was preserved"]
        )
    raise OSError(error, os.strerror(error), target)


def _rename_exchange(
    source_parent: int, source: str, target_parent: int, target: str
) -> None:
    function, flag = _rename_implementation(exchange=True)
    result = function(
        source_parent,
        os.fsencode(source),
        target_parent,
        os.fsencode(target),
        flag,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _rename_implementation(*, exchange: bool):
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError as error:
        raise PublicationError(["atomic publication is unavailable on this platform"]) from error
    if hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        flag = 0x00000002 if exchange else 0x00000004
    elif hasattr(libc, "renameat2"):
        function = libc.renameat2
        flag = 2 if exchange else 1
    else:
        raise PublicationError(["atomic publication is unavailable on this platform"])
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return function, flag


def _validate_publication_tree(blueprint: Path) -> None:
    """Reject inputs that could leak local state through a public artifact."""
    issues: list[str] = []
    if not blueprint.is_dir():
        return
    for source in sorted(blueprint.rglob("*")):
        relative = source.relative_to(blueprint)
        folded_parts = {part.casefold() for part in relative.parts}
        name = relative.name.casefold()
        if (
            folded_parts.intersection(_LOCAL_ONLY_NAMES)
            or name == ".env"
            or name.startswith(".env.")
            or name.endswith((".key", ".log", ".pem"))
        ):
            issues.append(f"refusing local or sensitive publication input: {relative.as_posix()}")
            continue
        if _is_hidden(relative):
            continue
        if source.is_symlink():
            issues.append(f"refusing symlink in blueprint publication: {relative.as_posix()}")
    if issues:
        raise PublicationError(issues)


def _validate_publication_snapshot(snapshot: TreeSnapshot) -> None:
    """Reject captured entries that could leak local state when materialized."""

    issues: list[str] = []
    paths = [
        *(relative for relative in snapshot.directories if relative),
        *(relative for relative, _data in snapshot.files),
        *(relative for relative, _target in snapshot.symlinks),
        *(relative for relative, _mode in snapshot.special),
        *(relative for relative, _kind in snapshot.omitted),
    ]
    symlinks = {relative for relative, _target in snapshot.symlinks}
    specials = {
        relative: reason for relative, reason in snapshot.unsupported_entries()
        if relative not in symlinks
    }
    for raw_relative in sorted(paths):
        relative = PurePosixPath(raw_relative)
        folded_parts = {part.casefold() for part in relative.parts}
        name = relative.name.casefold()
        if (
            folded_parts.intersection(_LOCAL_ONLY_NAMES)
            or name == ".env"
            or name.startswith(".env.")
            or name.endswith((".key", ".log", ".pem"))
        ):
            issues.append(f"refusing local or sensitive publication input: {raw_relative}")
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        if raw_relative in symlinks:
            issues.append(f"refusing symlink in blueprint publication: {raw_relative}")
        elif raw_relative in specials:
            issues.append(
                "refusing non-regular blueprint publication input: "
                f"{raw_relative}: {specials[raw_relative]}"
            )
    if issues:
        raise PublicationError(issues)


def _load_publication_contract(
    blueprint: Path,
    *,
    expected_blueprint_identity: tuple[int, int] | None = None,
    expected_roadmap_identity: tuple[int, int] | None = None,
) -> tuple[Graph, CoverageSummary]:
    graph = load_graph(
        blueprint,
        _expected_blueprint_identity=expected_blueprint_identity,
        _expected_roadmap_identity=expected_roadmap_identity,
    )
    coverage, coverage_issues = load_coverage(blueprint)
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
    return graph, coverage


def _is_hidden(relative: Path) -> bool:
    return any(part.startswith(".") for part in relative.parts)


def _sources_base(
    blueprint: Path,
    repo_root_identity: tuple[int, int],
    linker: SourceLinker,
    *,
    source_root_name: str,
    canonical_source_tails: dict[tuple[str, ...], tuple[str, ...]],
) -> "_SourceBase | None":
    """Where `blueprint/sources/` lives in the repository, if it can be linked.

    Source notes are a reader's transcription of the paper being formalised.
    Publishing them put a second copy of the reference on the site, at a URL
    nothing links deliberately and every `## Sources` list pointed at, so the
    book kept handing readers a page that was neither the book nor the paper.
    They stay in the vault and the site links out to them instead.

    Returns ``None`` when there are no repository coordinates to build a link
    from. The pages are then published as before, because a site with no
    sources and no way to reach them is worse than a redundant page.
    """
    if not linker.repository_url or not linker.ref:
        return None
    relative_blueprint = _relative_to_bound_directory(blueprint, repo_root_identity)
    if relative_blueprint is None:
        # The vault is outside the repository being linked, so no blob URL
        # describes it. Better no link than one that 404s.
        return None
    relative = (relative_blueprint / source_root_name).as_posix()
    return _SourceBase(
        linker.repository_url,
        linker.ref,
        relative,
        canonical_source_tails,
    )


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
    canonical_tails: dict[tuple[str, ...], tuple[str, ...]] = field(default_factory=dict)

    def href(self, tail: tuple[str, ...]) -> str:
        verb = "blob" if tail else "tree"
        key = _portable_path_parts(PurePosixPath(*tail)) if tail else ()
        canonical_tail = self.canonical_tails.get(key, tail)
        path = "/".join((self.relative, *canonical_tail))
        return f"{self.repository_url}/{verb}/{self.ref}/{quote(path, safe='/')}"


def _source_href(sources_base: _SourceBase, tail: tuple[str, ...]) -> str:
    """A repository URL for a source note, or for the sources directory itself."""
    return sources_base.href(tail)


def _published_source_files(blueprint: Path):
    """Yield the regular authored inputs that contribute to the static site."""
    for source in sorted(blueprint.rglob("*")):
        relative = source.relative_to(blueprint)
        if _SKIPPED_DIRECTORIES.intersection(relative.parts) or _is_hidden(relative):
            continue
        if _is_generated_path(relative) or not source.is_file():
            continue
        yield source, relative


def _source_revision(blueprint: Path, *, excluded_root: Path | None = None) -> str:
    digest = hashlib.sha256(b"autoform-markdown-publication/v2\0")
    for source, relative in _published_source_files(blueprint):
        if _is_excluded_relative(relative, excluded_root):
            continue
        _update_source_digest(digest, relative, _read_regular_file(source))
    return digest.hexdigest()


def _portable_path_parts(path: PurePosixPath | Path) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def _is_excluded_relative(
    relative: PurePosixPath | Path,
    excluded_root: PurePosixPath | Path | None,
) -> bool:
    if excluded_root is None:
        return False
    relative_parts = _portable_path_parts(relative)
    excluded_parts = _portable_path_parts(excluded_root)
    return relative_parts[: len(excluded_parts)] == excluded_parts


def _require_canonical_sources_directory(snapshot: TreeSnapshot) -> None:
    """Reject filesystem aliases that could bypass the v2 publication purge."""

    source_root = _source_root_name(snapshot)
    if source_root != SOURCES_DIR:
        raise PublicationError(
            [
                "v2 publication requires the canonical sources directory spelling; "
                f"found {source_root}"
            ]
        )


def _source_root_name(snapshot: TreeSnapshot) -> str:
    canonical = _portable_path_parts(PurePosixPath(SOURCES_DIR))[0]
    aliases = sorted(
        {
            PurePosixPath(relative).parts[0]
            for relative in snapshot.directories
            if relative
            and len(PurePosixPath(relative).parts) == 1
            and _portable_path_parts(PurePosixPath(relative))[0] == canonical
        }
    )
    if len(aliases) > 1:
        raise PublicationError(
            [
                "source directories collide on a case-insensitive filesystem: "
                + ", ".join(aliases)
            ]
        )
    return aliases[0] if aliases else SOURCES_DIR


def _canonical_source_tails(
    snapshot: TreeSnapshot,
) -> dict[tuple[str, ...], tuple[str, ...]]:
    """Map portable source-link spellings to names captured from the vault."""

    entries = [
        *snapshot.directories,
        *(relative for relative, _data in snapshot.files),
        *(relative for relative, _target in snapshot.symlinks),
        *(relative for relative, _mode in snapshot.special),
        *snapshot.placeholders,
        *(relative for relative, _kind in snapshot.omitted),
    ]
    canonical: dict[tuple[str, ...], tuple[str, ...]] = {}
    for relative in entries:
        parts = PurePosixPath(relative).parts
        if not parts or not _is_excluded_relative(
            PurePosixPath(relative), PurePosixPath(SOURCES_DIR)
        ):
            continue
        tail = parts[1:]
        key = _portable_path_parts(PurePosixPath(*tail)) if tail else ()
        previous = canonical.get(key)
        if previous is not None and previous != tail:
            raise PublicationError(
                [
                    "source paths collide on a case-insensitive filesystem: "
                    f"{'/'.join(previous)}, {'/'.join(tail)}"
                ]
            )
        canonical[key] = tail
    return canonical


def _write_publication_manifest(
    destination: Path,
    graph: Graph,
    linker: SourceLinker,
    *,
    coverage: CoverageSummary,
    complete: bool,
    source_revision: str,
    lean_source_revision: str,
) -> None:
    directories, files = _publication_inventory(destination)
    coverage_manifest: dict[str, object] = {
        "complete": coverage.complete,
        "counts": coverage.counts,
        "schema": coverage.schema,
        "source_path": coverage.source_path,
        "source_sha256": coverage.source_sha256,
    }
    if coverage.schema == COVERAGE_V2_SCHEMA:
        coverage_manifest.update(
            {
                "artifact_path": coverage.artifact_path,
                "artifact_sha256": coverage.artifact_sha256,
                "contract_sha256": coverage.source_sha256,
            }
        )
    manifest = {
        "complete": complete,
        "coverage": coverage_manifest,
        "directories": list(directories),
        "files": dict(files),
        "schema": PUBLICATION_SCHEMA,
        "source": "blueprint/roadmap Markdown",
        "source_revision": source_revision,
        "git_ref": linker.ref,
        "lean_source_revision": lean_source_revision,
        "nodes": len(graph.nodes),
        "dependencies": graph.edge_count,
        "views": ["book", "progress", "project", "chapter", "focus", "full"],
    }
    (destination / PUBLICATION_MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _group_nodes(graph: Graph) -> dict[str, list[str]]:
    """Group formalizable leaves under their nearest containing article.

    Graph views deliberately roll a subtree up to its top-level scope. Book
    publication is different: a statement belongs where the author placed its
    nearest narrative container, so nested sections remain real book sections.
    """
    grouped: dict[str, list[str]] = {}
    for node_id in status.topological_order(graph):
        node = graph.nodes[node_id]
        if not node.formalizable or graph.children(node_id):
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
    book_sources = {
        node.path.resolve()
        for node in graph.nodes.values()
        if graph.children(node.id) or not node.formalizable
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
            for match in _MARKDOWN_LINK.finditer(line):
                raw = match.group("target")
                bare = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
                path = bare.partition("#")[0]
                if not path or urlsplit(path).scheme or path.startswith("/"):
                    continue
                candidate = (source.parent / unquote(path)).resolve()
                try:
                    candidate_relative = candidate.relative_to(blueprint)
                except ValueError:
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

        _outside_fences(source.read_text(encoding="utf-8"), collect)
        pending.extend(reversed(linked_sources))
    return ordered








def _append_book_navigation(pages: list[Path]) -> None:
    """Add previous/next links to the bottom of Blueprint pages, never global nav."""
    if len(pages) < 2:
        return
    titles = [_first_h1(page.read_text(encoding="utf-8")) or page.stem for page in pages]
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


def _inject_after_title(text: str, block: str) -> str:
    """Place a generated overview immediately after the document's first H1."""
    lines = text.splitlines()
    fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        heading = _HEADING.match(line) if fence is None else None
        if heading is not None and len(heading.group(1)) == 1:
            merged = [*lines[: index + 1], "", block.rstrip(), "", *lines[index + 1 :]]
            return "\n".join(merged) + ("\n" if text.endswith("\n") else "")
    return text.rstrip() + "\n\n" + block.rstrip() + "\n"


def _inject_after_lead(text: str, block: str) -> str:
    """Place chapter metadata after its opening prose and before the first section."""
    lines = text.splitlines()
    fence: tuple[str, int] | None = None
    seen_h1 = False
    for index, line in enumerate(lines):
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        heading = _HEADING.match(line) if fence is None else None
        if heading is None:
            continue
        level = len(heading.group(1))
        if level == 1:
            seen_h1 = True
        elif seen_h1 and level == 2:
            merged = [*lines[:index], "", block.rstrip(), "", *lines[index:]]
            return "\n".join(merged) + ("\n" if text.endswith("\n") else "")
    return text.rstrip() + "\n\n" + block.rstrip() + "\n"



def _next_target(
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    *,
    page: Path,
    destination: Path,
    group_pages: dict[str, Path] | None = None,
    targets: dict[str, str] | None = None,
) -> str:
    """Name the result a contributor could pick up right now, with the way in.

    A dashboard that only counts is a scoreboard. The useful question on
    arriving is "what is unblocked, and where do I start reading?", which the
    DAG already answers: the first node in topological order whose
    prerequisites are met and whose proof is still open. Naming it without
    linking to it makes the reader hunt, so the card carries the statement, its
    chapter, and its dependency view.
    """
    for node_id in status.topological_order(graph):
        node_status = statuses.get(node_id)
        node = graph.nodes[node_id]
        if node_status is None or graph.children(node_id) or not node.formalizable:
            continue
        if node_status.key not in {"can_prove", "can_state"}:
            continue

        chapter_id = node.id.split("/", 1)[0] if "/" in node.id else ""
        chapter_page = (group_pages or {}).get(chapter_id)
        statement = (targets or {}).get(node_id)
        if statement is None and chapter_page is not None:
            anchor = node.id.split("/", 1)[1].replace("/", "-") if "/" in node.id else node.id
            statement = f"{mermaid.relative_link(chapter_page, page, '.html')}#{anchor}"
        graph_href = mermaid.relative_link(
            graph_pages.focus_page_path(destination, node.id), page, ".html"
        )

        title = html.escape(node.title)
        heading = (
            f'<a href="{html.escape(statement, quote=True)}">{title}</a>' if statement else title
        )
        why = (
            "Every prerequisite is proved, so the proof can be written now."
            if node_status.key == "can_prove"
            else "Every prerequisite is stated, so this can be written down."
        )
        actions = [f'<a href="{html.escape(graph_href, quote=True)}">Dependencies</a>']
        if chapter_page is not None:
            chapter_title = _first_h1(chapter_page.read_text(encoding="utf-8")) or "chapter"
            href = mermaid.relative_link(chapter_page, page, ".html")
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
            '<div class="bp-next-target">'
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
    blueprint: Path,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    *,
    page: Path,
    targets: dict[str, tuple[Path, str]],
    sources_base: "_SourceBase | None",
) -> str:
    """List the vault as a file tree, so its shape can be checked at a glance.

    The book answers what the project says; nothing answered how it is laid
    out. A real project came back with 71 of 72 articles sitting directly under
    `roadmap/`, which parses cleanly, passes `autoform check`, and publishes a
    book with no chapters -- a fault invisible in every rendered view, because
    every rendered view shows content rather than paths.

    So this shows paths: the directory tree itself, with what the graph made of
    each file beside it. Directories are rows too, because they are what a
    chapter is; without them every `README.md` looks like every other one.
    """
    links = _anchored_links(targets, page, extension=".md")
    by_path = {node.path.resolve(): node for node in graph.nodes.values()}

    def keep(relative: Path) -> bool:
        if _SKIPPED_DIRECTORIES.intersection(relative.parts) or _is_hidden(relative):
            return False
        if _is_generated_path(relative):
            return False
        return not (
            sources_base is not None
            and _is_excluded_relative(relative, Path(SOURCES_DIR))
        )

    files = [p for p in sorted(blueprint.rglob("*.md")) if keep(p.relative_to(blueprint))]
    directories = {p.relative_to(blueprint).parent for p in files}
    directories.discard(Path("."))
    for directory in list(directories):
        for parent in directory.parents:
            if parent != Path("."):
                directories.add(parent)

    def row(indent: int, label: str, kind: str, mark: str, extra: str = "") -> str:
        return (
            f'<span class="bp-tree-path{extra}" '
            f'style="padding-left: {indent * 1.1:.1f}rem">{label}</span>'
            f'<span class="bp-tree-kind">{kind}</span>'
            f'<span class="bp-tree-mark">{mark}</span>'
        )

    rows = [row(0, "<strong>blueprint/</strong>", "vault root", "")]
    for entry in sorted(directories | {p.relative_to(blueprint) for p in files}):
        depth = len(entry.parts)
        if entry in directories:
            rows.append(row(depth, f"<strong>{html.escape(entry.name)}/</strong>", "", ""))
            continue
        source = blueprint / entry
        node = by_path.get(source.resolve())
        name = html.escape(entry.name)
        if node is None:
            # Everything under `roadmap/` is a node or the graph refuses to
            # load, so a file with no node here is prose by construction: the
            # blueprint landing page or the coverage contract.
            rows.append(row(depth, name, "prose", ""))
            continue
        href = links.get(node.id)
        label = f'<a href="{html.escape(href, quote=True)}">{name}</a>' if href else name
        state = statuses[node.id]
        rows.append(
            row(
                depth,
                f"{label} <span class='bp-tree-title'>{html.escape(node.title)}</span>",
                html.escape(node.declaration or node.kind),
                f'<span class="bp-swatch bp-swatch-{state.key}"></span>'
                f'<span class="bp-tree-state">{html.escape(state.label)}</span>',
            )
        )

    article_depths = {len(p.relative_to(blueprint).parts) - 1 for p in by_path}
    warnings = []
    if len(by_path) > 3 and article_depths <= {1}:
        warnings.append(
            "Every article sits directly under <code>roadmap/</code>. Chapters come "
            "from directories, so this vault publishes as one undivided list."
        )
    return "\n".join(
        [
            "---",
            "title: Vault structure",
            "hide:",
            "  - toc",
            "---",
            "",
            "# Vault structure",
            "",
            "Every Markdown file in the blueprint, as the vault holds them. Chapters "
            "come from directories, so the shape of this tree is the shape of the book.",
            "",
            *(f'<div class="bp-tree-warn">{text}</div>' for text in warnings),
            "",
            f'<div class="bp-tree">{"".join(rows)}</div>',
            "",
        ]
    )


def _render_summary_nav(
    book_pages: list[Path],
    *,
    destination: Path,
    overview: Path,
) -> str:
    """Write the site nav as Markdown so the Book tab holds real chapters.

    mkdocs.yml cannot know a project's chapters, and hand-listing them would be
    the second navigation manifest the roadmap skill forbids. mkdocs-literate-nav
    reads this file instead, so the tabs come from the same page order the book
    itself uses.
    """
    lines = [f"- [Home]({overview.relative_to(destination).as_posix()})", "- Book"]
    for page in book_pages:
        if page == overview:
            continue
        title = _first_h1(page.read_text(encoding="utf-8")) or page.parent.name
        depth = len(page.relative_to(destination).parts) - 1
        indent = "    " * max(depth, 1)
        lines.append(f"{indent}- [{title}]({page.relative_to(destination).as_posix()})")
    # What counts as done belongs beside the book it qualifies. The landing
    # page used to be the only route to it, which made the page impossible to
    # simplify without stranding it.
    coverage = destination / "coverage/README.md"
    if coverage.is_file():
        title = _first_h1(coverage.read_text(encoding="utf-8")) or "Coverage"
        lines.append(f"    - [{title}](coverage/README.md)")
    lines.extend(
        [
            "- Graph",
            "    - [Dependency maps](dependencies.md)",
            "    - [Full theorem DAG](dependencies/full.md)",
            f"    - [Vault structure]({STRUCTURE_PAGE})",
        ]
    )
    return "\n".join(lines) + "\n"

def _render_landing_page(
    text: str,
    *,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    groups: dict[str, list[str]],
    group_pages: dict[str, Path],
    page: Path,
    destination: Path,
    targets: dict[str, str] | None = None,
) -> str:
    """The landing page: what this is, how far it has got, and what is next.

    Material renders a table of contents beside every page. On a short
    dashboard it lists one or two headings and steals a third of the width, so
    the page opts out.
    """
    body = _document_body(text)
    title = _first_h1(text) or "Blueprint"
    parts = [
        "---",
        # The hero sets the H1 itself, so the page title has to be declared
        # rather than discovered from the first Markdown heading.
        f"title: {title}",
        # The sidebar lists the open tab's pages, and this tab holds only this
        # page, so on the landing page it is a column of one word. The tabs
        # already carry the reader to Book and Graph, so it goes, and the hero
        # and map get the width instead.
        "hide:",
        "  - navigation",
        "  - toc",
        "---",
        "",
        '<div class="bp-landing" markdown="1">',
        "",
        _render_hero(title, body, graph, statuses),
        "",
        _next_target(
            graph,
            statuses,
            page=page,
            destination=destination,
            group_pages=group_pages,
            targets=targets,
        ),
    ]
    breakdown = mermaid.render_legend(statuses)
    project = graph_views.project_view(graph, statuses)
    if project.nodes:
        # Clicking a chapter on the home page opens that chapter's dependency
        # map: the home map is a preview of the Graph tab, not a second index.
        # A project-view node is a whole chapter, so its id is namespaced
        # `scope:<group>`; the page is named for the group alone. Stripping has
        # to precede the empty-group fallback, or the root chapter asks for
        # `scope:.html` -- a truthy id, and so never the fallback it needs.
        links = {
            node.id: mermaid.relative_link(
                destination
                / "dependencies/chapters"
                / f"{node.id.removeprefix('scope:') or 'roadmap'}.md",
                page,
                ".html",
            )
            for node in project.nodes
        }
        # The map is the subject of this page, not an appendix to it: a reader
        # arriving at a blueprint wants the shape of the project first. It runs
        # the full width, and the legend rides along as its caption rather than
        # as a section of its own further down.
        parts.extend(
            [
                "",
                '<div class="bp-map" markdown="1">',
                '<div class="bp-map-head">',
                '<span class="bp-map-title">Project map</span>',
                '<span class="bp-map-hint">Select a chapter to open its dependencies</span>',
                "</div>",
                "",
                mermaid.render_view_diagram(project, links=links, include_classdefs=False),
                "",
                f'<div class="bp-map-legend" markdown="1">\n\n{breakdown}\n\n</div>'
                if breakdown
                else "",
                "</div>",
            ]
        )
    elif breakdown:
        parts.extend(["", "## Status breakdown", "", breakdown])
    # The authored body is a contents list and links to the roadmap, the
    # coverage notes and the dependency view. The tabs are all three, so
    # repeating them here only pushes the map up the page for nothing. Its
    # opening sentence is already the hero's lead.
    parts.append("</div>")
    return "\n".join(part for part in parts if part is not None).rstrip() + "\n"


#: States that count as finished work for the headline percentage.
_SETTLED_STATES = frozenset({"mathlib", "fully_proved", "proved", "defined", "stated"})
#: States a contributor could pick up today.
_ACTIONABLE_STATES = frozenset({"can_prove", "can_state"})


def _is_countable(graph: Graph, node_id: str) -> bool:
    """Whether *node_id* is a formalization target the dashboards should count.

    A leaf, and a leaf that declares something. Counting every leaf made a
    freshly scaffolded vault report "0 of 1 items settled, 1 ready now": the
    roadmap landing page has no children yet, so it counted as an unstarted
    result, and the site claimed work existed before any had been planned.
    """

    return not graph.children(node_id) and graph.nodes[node_id].formalizable


def _countable(graph: Graph) -> list[str]:
    return [node_id for node_id in graph.nodes if _is_countable(graph, node_id)]


def _render_hero(
    title: str,
    body: str,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
) -> str:
    """Open with the project's name, its one-line claim, and where it stands.

    The dashboard this replaces was a paragraph of counts in a bordered box.
    The same numbers set as figures answer "how far along is this?" before the
    reader has finished the title, which is the only question the top of a
    blueprint has to answer.
    """
    leaves = _countable(graph)
    selected = {node_id: statuses[node_id] for node_id in leaves}
    done = sum(
        count for state, count in status.summarize(selected) if state.key in _SETTLED_STATES
    )
    actionable = sum(
        count for state, count in status.summarize(selected) if state.key in _ACTIONABLE_STATES
    )
    total = len(leaves)
    share = round(100 * done / total) if total else 0

    figures = [
        ("Formalized", f"{share}%", f"{done} of {total} items settled"),
        ("Ready now", str(actionable), "unblocked, waiting for an author"),
        ("Chapters", str(len(graph_views.group_nodes(graph))), "top-level milestones"),
    ]
    stats = "".join(
        f'<div class="bp-figure"><div class="bp-figure-value">{value}</div>'
        f'<div class="bp-figure-label">{html.escape(label)}</div>'
        f'<div class="bp-figure-note">{html.escape(note)}</div></div>'
        for label, value, note in figures
    )
    lead = _lead_sentence(body)
    return (
        '<div class="bp-hero">'
        '<div class="bp-hero-rule"></div>'
        '<div class="bp-hero-kicker">Formalization blueprint</div>'
        f'<h1 class="bp-hero-title">{html.escape(title)}</h1>'
        + (f'<p class="bp-hero-lead">{lead}</p>' if lead else "")
        + f'<div class="bp-hero-figures">{stats}</div>'
        f'<div class="bp-hero-bar" role="img" '
        f'aria-label="{share}% of items formalized">'
        f'<span style="width: {share}%"></span></div>'
        "</div>"
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
    node_ids: list[str] | None = None,
) -> str:
    """Render the compact, honest progress strip shown at the start of the book."""
    selected_ids = [
        node_id
        for node_id in (node_ids if node_ids is not None else graph.nodes)
        if _is_countable(graph, node_id)
    ]
    definitions = sum(is_definition(graph.nodes[node_id]) for node_id in selected_ids)
    results = len(selected_ids) - definitions
    item_parts = []
    if definitions:
        item_parts.append(f"{definitions} definition{'s' if definitions != 1 else ''}")
    if results:
        item_parts.append(f"{results} result{'s' if results != 1 else ''}")
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
        f""
        "</div>"
    )



def _status_phrase(node_statuses: Iterable[status.NodeStatus]) -> str:
    counts = {state.key: 0 for state in status.STATES}
    for node_status in node_statuses:
        counts[node_status.key] += 1
    return " · ".join(f"{counts[state.key]} {state.label}" for state in status.STATES if counts[state.key])


def _markdown_table_cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def _first_h1(text: str) -> str | None:
    for line in text.splitlines():
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
    for line in lines[start:]:
        heading = _HEADING.match(line)
        if heading is not None and len(heading.group(1)) == 1 and not dropped_title:
            dropped_title = True
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _shift_headings(text: str, levels: int) -> str:
    def shift(line: str) -> str:
        heading = _HEADING.match(line)
        if heading is None:
            return line
        level = min(len(heading.group(1)) + levels, 6)
        return f"{'#' * level} {heading.group(2)}"

    return _outside_fences(text, shift)


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
) -> dict[str, str]:
    """Link every node to its statement on the published chapter page.

    Use ``.md`` for links MkDocs will parse -- it validates and rewrites those
    itself -- and ``.html`` for raw HTML and Mermaid, which it never sees. A
    statement on the current page is just a fragment.
    """
    resolved_page = page.resolve()
    links: dict[str, str] = {}
    for node_id, (target, anchor) in targets.items():
        if target.resolve() == resolved_page:
            links[node_id] = f"#{anchor}" if anchor else "#"
        else:
            href = mermaid.relative_link(target, page, extension)
            if extension == ".html":
                href = _as_published(href)
            links[node_id] = f"{href}#{anchor}" if anchor else href
    return links


def _as_published(href: str) -> str:
    """MkDocs serves ``README.md`` as ``index.html``.

    Links it parses are rewritten for us, but raw HTML and Mermaid fences never
    reach it, so those have to name the published file themselves.
    """
    path = Path(href)
    if path.name.lower() in {"readme.html", "index.html"}:
        return (path.parent / "index.html").as_posix()
    return href


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
    unpublished_source_root: Path | None = None,
) -> str:
    """Resolve a page's relative links against where it is being published.

    Three things move. Node files stop being pages once their chapter absorbs
    them, so a link naming one becomes an anchor. A node's body is hoisted out
    of its own directory onto the chapter page, so its remaining relative links
    have to be recomputed from there. And source notes are not published at
    all when *sources_base* says where to reach them in the repository.
    """
    anchored = _anchored_links(targets, page, extension=".md")

    strip_link = object()

    def is_unpublished_target(raw: str) -> bool:
        return unpublished_source_root is not None and _link_targets_excluded_root(
            raw, source_dir=source_dir, excluded_root=unpublished_source_root
        )

    def moved_target(raw: str) -> str | object | None:
        """Where *raw* should point once published, or None to leave it alone."""
        bare = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        path, separator, fragment = bare.partition("#")
        if not path or urlsplit(path).scheme or path.startswith("/"):
            return None
        candidate = (source_dir / unquote(path)).resolve()
        node_id = node_sources.get(candidate)
        if node_id is not None:
            href = anchored[node_id]
            if not targets[node_id][1] and separator:
                href = f"{'' if href == '#' else href}#{fragment}"
            return href
        if not _is_within(candidate, blueprint):
            return None
        relative = candidate.relative_to(blueprint)
        if is_unpublished_target(raw):
            if sources_base is not None:
                return f"{_source_href(sources_base, relative.parts[1:])}{separator}{fragment}"
            return strip_link
        if sources_base is not None and _is_excluded_relative(
            relative, Path(SOURCES_DIR)
        ):
            return f"{_source_href(sources_base, relative.parts[1:])}{separator}{fragment}"
        published = destination / relative
        return f"{mermaid.relative_link(published, page, candidate.suffix)}{separator}{fragment}"

    def replace(match: re.Match[str]) -> str:
        href = moved_target(match.group("target"))
        if href is strip_link:
            return match.group("label")
        suffix = match.group("suffix") or ""
        return match.group(0) if href is None else f"[{match.group('label')}]({href}{suffix})"

    def replace_artifact_link(match: re.Match[str]) -> str:
        if match.group("image") != "!" or not is_unpublished_target(match.group("target")):
            return match.group(0)
        href = moved_target(match.group("target"))
        if href is strip_link:
            return match.group("label")
        if href is None:
            return match.group(0)
        suffix = match.group("suffix") or ""
        return f"{match.group('image')}[{match.group('label')}]({href}{suffix})"

    def replace_autolink(match: re.Match[str]) -> str:
        if not is_unpublished_target(match.group("target")):
            return match.group(0)
        href = moved_target(match.group("target"))
        if href is strip_link:
            return "source artifact"
        if href is None:
            return match.group(0)
        return f"<{href}>"

    _reject_excluded_raw_html_links(text, is_unpublished_target)

    def rewrite(line: str) -> str:
        # A reference definition carries the destination for every `[x][label]`
        # on the page. Rewriting only inline links leaves it pointing at the
        # unpublished path and the rendered link dangles.
        definition = _LINK_DEFINITION.match(line)
        if definition is not None:
            href = moved_target(definition.group("target"))
            if href is strip_link:
                return ""
            if href is not None:
                indent, label = definition.group("indent"), definition.group("label")
                return f"{indent}[{label}]: {href}{definition.group('rest') or ''}"
            return line
        line = _ANY_INLINE_LINK.sub(replace_artifact_link, line)
        line = _AUTOLINK.sub(replace_autolink, line)
        return _MARKDOWN_LINK.sub(replace, line)

    return _outside_fences(text, rewrite)


class _RawHtmlTargetParser(HTMLParser):
    """Collect URL-bearing attributes from authored raw HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.targets: list[tuple[str, str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._collect(tag, attrs)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._collect(tag, attrs)

    def _collect(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            folded_name = name.casefold()
            if value is None or (
                folded_name not in _RAW_HTML_LINK_ATTRIBUTES
                and not folded_name.endswith(":href")
            ):
                continue
            values = (
                tuple(part.strip().split()[0] for part in value.split(",") if part.strip())
                if folded_name == "srcset"
                else (value,)
            )
            self.targets.extend((tag, name, target) for target in values)


def _reject_excluded_raw_html_links(
    text: str, is_excluded, *, markdown: bool = True
) -> None:
    visible_text = text
    if markdown:
        visible_text = INLINE_CODE.sub("", "\n".join(content(text).lines))
    parser = _RawHtmlTargetParser()
    parser.feed(visible_text)
    parser.close()
    for tag, attribute, target in parser.targets:
        if is_excluded(target):
            raise PublicationError(
                [
                    "raw HTML link targets an excluded source; use a Markdown link "
                    f"that Autoform can rewrite ({tag} {attribute})"
                ]
            )


def _link_targets_excluded_root(raw: str, *, source_dir: Path, excluded_root: Path) -> bool:
    bare = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
    split = urlsplit(bare)
    if not split.path or split.scheme or split.netloc:
        return False
    decoded_path = unquote(split.path).replace("\\", "/")
    if decoded_path.startswith("/"):
        relative = Path(PurePosixPath(decoded_path.lstrip("/")))
        return _is_excluded_relative(relative, Path(SOURCES_DIR))
    candidate = (source_dir / decoded_path).resolve()
    root = excluded_root.resolve()
    return _is_excluded_relative(candidate, root)


def _reject_excluded_markdown_links(text: str, is_excluded) -> None:
    """Reject visible Markdown destinations into a purged source tree."""

    parser = _RawHtmlTargetParser()
    try:
        parser.feed(render_html(text))
        parser.close()
    except Exception as error:
        raise PublicationError(["could not inspect Markdown links in staged publication"]) from error
    if any(is_excluded(target) for _, _, target in parser.targets):
        raise PublicationError(["Markdown link targets an excluded source"])

    # Autoform has historically accepted local angle autolinks even though the
    # configured Markdown renderer treats them as raw text. Keep auditing that
    # compatibility syntax while the rendered HTML handles every real link.
    for line in content(text).lines:
        visible = INLINE_CODE.sub("", line)
        if any(is_excluded(match.group("target")) for match in _AUTOLINK.finditer(visible)):
            raise PublicationError(["Markdown link targets an excluded source"])


def _reject_staged_excluded_raw_html_links(
    destination: Path, excluded_source_root: Path
) -> None:
    """Reject links carried forward from an older incremental site."""

    root = destination / excluded_source_root
    for page in sorted(destination.rglob("*")):
        if not page.is_file() or page.suffix.casefold() not in {".htm", ".html", ".md", ".svg"}:
            continue
        try:
            text = _read_regular_file(page).decode("utf-8")
        except UnicodeError as error:
            raise PublicationError(
                [f"could not inspect publication links in {page.relative_to(destination)}"]
            ) from error
        _reject_excluded_raw_html_links(
            text,
            lambda raw, parent=page.parent: _link_targets_excluded_root(
                raw, source_dir=parent, excluded_root=root
            ),
            markdown=page.suffix.casefold() == ".md",
        )
        if page.suffix.casefold() == ".md":
            _reject_excluded_markdown_links(
                text,
                lambda raw, parent=page.parent: _link_targets_excluded_root(
                    raw,
                    source_dir=parent,
                    excluded_root=root,
                ),
            )


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _publication_paths_overlap(first: Path, second: Path) -> bool:
    """Recognize lexical and filesystem aliases before creating render state."""

    return (
        _is_within(first, second)
        or _is_within(second, first)
        or _path_reaches_directory_identity(first, second)
        or _path_reaches_directory_identity(second, first)
    )


def _path_reaches_directory_identity(path: Path, directory: Path) -> bool:
    try:
        target = directory.stat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise PublicationError(["could not verify publication path separation"]) from error
    if not stat.S_ISDIR(target.st_mode):
        return False
    target_identity = (target.st_dev, target.st_ino)
    cursor = path
    while True:
        try:
            metadata = cursor.stat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise PublicationError(["could not verify publication path separation"]) from error
        else:
            if stat.S_ISDIR(metadata.st_mode) and (
                metadata.st_dev,
                metadata.st_ino,
            ) == target_identity:
                return True
        parent = cursor.parent
        if parent == cursor:
            return False
        cursor = parent


def _relative_to_bound_directory(
    path: Path,
    directory_identity: tuple[int, int],
) -> Path | None:
    """Return *path* relative to an ancestor identified independently of spelling."""

    tail: list[str] = []
    cursor = path
    while True:
        try:
            metadata = cursor.stat()
        except OSError as error:
            raise PublicationError(["could not verify repository path containment"]) from error
        if stat.S_ISDIR(metadata.st_mode) and (
            metadata.st_dev,
            metadata.st_ino,
        ) == directory_identity:
            return Path(*reversed(tail))
        parent = cursor.parent
        if parent == cursor:
            return None
        tail.append(cursor.name)
        cursor = parent


def _outside_fences(text: str, transform) -> str:
    """Apply *transform* to every line that is not inside a code fence."""
    fence: tuple[str, int] | None = None
    out: list[str] = []
    for line in text.splitlines():
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            out.append(line)
            continue
        out.append(line if fence is not None else transform(line))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


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
    sources_base: "_SourceBase | None" = None,
    unpublished_source_root: Path | None = None,
    source_blueprint: Path,
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
            blueprint=blueprint,
            repo_root=repo_root,
            destination=destination,
            node_sources=node_sources,
            targets=targets,
            sources_base=sources_base,
            unpublished_source_root=unpublished_source_root,
            source_blueprint=source_blueprint,
        )
        environments[node_id] = environment
        linked += node_linked
        unresolved.extend(node_unresolved)

    chapter_summary = _render_overview_summary(
        graph,
        statuses,
        node_ids=node_ids,
    )
    if narrative is None:
        title = graph.nodes[group].title if group in graph.nodes else group.replace("-", " ").capitalize()
        narrative = "\n".join(["---", f"kind: article\ntitle: {title}", "---", "", f"# {title}"])

    chapter, placed = _place_environments(
        _inject_after_lead(narrative, chapter_summary),
        source_dir=graph.nodes[group].path.parent if group in graph.nodes else blueprint / "roadmap",
        node_sources=node_sources,
        environments=environments,
        targets=targets,
    )
    remaining = [environments[node_id] for node_id in node_ids if node_id not in placed]
    if remaining:
        chapter = chapter.rstrip() + "\n\n## Additional formalization targets\n\n" + "\n\n".join(remaining)
    return chapter.rstrip() + "\n", linked, unresolved


def _place_environments(
    narrative: str,
    *,
    source_dir: Path,
    node_sources: dict[Path, str],
    environments: dict[str, str],
    targets: dict[str, tuple[Path, str]],
) -> tuple[str, set[str]]:
    """Replace standalone leaf links with their environment at the authored position."""
    placed: set[str] = set()
    output: list[str] = []
    anchor_nodes = {
        targets[node_id][1]: node_id for node_id in environments if targets[node_id][1]
    }
    fence: tuple[str, int] | None = None
    for line in narrative.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            output.append(line)
            continue
        slot = _ARTICLE_SLOT.match(line) if fence is None else None
        if slot is None:
            output.append(line)
            continue
        raw_target = slot.group("target")
        target = (
            raw_target[1:-1]
            if raw_target.startswith("<") and raw_target.endswith(">")
            else raw_target
        )
        path, separator, fragment = target.partition("#")
        if not path and separator:
            node_id = anchor_nodes.get(fragment)
        elif not path or urlsplit(path).scheme or path.startswith("/"):
            node_id = None
        else:
            node_id = node_sources.get((source_dir / unquote(path)).resolve())
        if node_id is None or node_id not in environments or node_id in placed:
            output.append(line)
            continue
        output.append(environments[node_id])
        placed.add(node_id)
    return "\n".join(output), placed


def _render_environment(
    node: Node,
    *,
    anchor: str,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    numbers: dict[str, str],
    used_by: dict[str, list[str]],
    linker: SourceLinker,
    links: dict[str, str],
    page: Path,
    blueprint: Path,
    repo_root: Path,
    destination: Path,
    node_sources: dict[Path, str],
    targets: dict[str, tuple[Path, str]],
    sources_base: "_SourceBase | None" = None,
    unpublished_source_root: Path | None = None,
    source_blueprint: Path,
) -> tuple[str, int, list[str]]:
    node_status = statuses[node.id]
    caption, _, number = numbers[node.id].rpartition(" ")
    statement, remainder = _split_body(node.path.read_text(encoding="utf-8"))
    # The body is leaving its own directory for the chapter page, so its
    # relative links have to be recomputed from the chapter's location.
    statement, remainder = (
        _rewrite_links(
            part,
            source_dir=node.path.parent,
            page=page,
            blueprint=blueprint,
            destination=destination,
            node_sources=node_sources,
            targets=targets,
            sources_base=sources_base,
            unpublished_source_root=unpublished_source_root,
        )
        for part in (statement, remainder)
    )

    code_links, implementation_rows, linked, unresolved = _lean_presentation(node, linker)
    context_link = _graph_context_link(node, page=page, destination=destination)
    source_link = _vault_source_link(
        node,
        blueprint=blueprint,
        source_blueprint=source_blueprint,
        repo_root=repo_root,
        linker=linker,
    )
    meta_rows = implementation_rows
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
    style = "theorem-style-definition" if is_definition(node) else "theorem-style-plain"
    mark = "✓" if node_status.key in {"fully_proved", "mathlib"} else "●"

    lines = [
        f'<div class="bp-thmwrapper {style} bp-{node_status.key}" id="{html.escape(anchor, quote=True)}" markdown="1">',
        '<div class="bp-thmheading">',
        f'<span class="bp-thmcaption">{html.escape(caption)}</span>'
        f'<span class="bp-thmlabel">{html.escape(number)}</span>'
        f'<span class="bp-thmtitle">{html.escape(node.title)}</span>'
        f"{code_links}"
        f"{context_link}"
        f"{source_link}"
        f'<a class="bp-permalink" href="#{html.escape(anchor, quote=True)}">#</a>'
        f'<span class="bp-mark" title="{html.escape(node_status.label, quote=True)}">'
        f'{mark}<span class="bp-mark-label">{html.escape(node_status.label)}</span></span>',
        "</div>",
        '<div class="bp-thmcontent" markdown="1">',
        "",
        statement,
        "",
        "</div>",
    ]
    if remainder:
        lines.extend(['<div class="bp-thmnotes" markdown="1">', "", remainder, "", "</div>"])
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
    """Render direct source icons, falling back to quiet diagnostic metadata."""
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
        if url:
            label = html.escape(f"View {name} in Lean source", quote=True)
            icons.append(
                f'<a class="bp-code-link" href="{html.escape(url, quote=True)}" '
                f'aria-label="{label}" title="{label}">{_code_icon()}</a>'
            )
            linked += 1
        elif location is not None:
            fallback.append(f'{code} <span class="bp-where">{html.escape(location.path.as_posix())}</span>')
            linked += 1
        else:
            fallback.append(f'{code} <span class="bp-missing">not found in the Lean sources</span>')
            unresolved.append(f"{node.id}: {name}")

    code_links = f'<span class="bp-code-links">{"".join(icons)}</span>' if icons else ""
    rows = [("Lean", " · ".join(fallback))] if fallback else []
    return code_links, rows, linked, unresolved


def _code_icon() -> str:
    """A small dependency-free code icon suitable at theorem-heading size."""
    return (
        '<svg class="bp-code-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        '<path d="m8 9-3 3 3 3M16 9l3 3-3 3M14 5l-4 14"/>'
        "</svg>"
    )


def _vault_source_link(
    node: Node,
    *,
    blueprint: Path,
    source_blueprint: Path,
    repo_root: Path,
    linker,
) -> str:
    """Link a statement to the Markdown article it was authored in.

    The graph view and the published statement are both derived. This is the
    file a reader edits, so it is worth one click from the statement itself.
    """
    if not linker.repository_url or not linker.ref:
        return ""
    try:
        article = source_blueprint / node.path.resolve().relative_to(blueprint)
        relative = article.relative_to(repo_root).as_posix()
    except ValueError:
        return ""
    href = f"{linker.repository_url}/blob/{linker.ref}/{relative}"
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


def _graph_context_link(node: Node, *, page: Path, destination: Path) -> str:
    """Link a textbook statement to its generated one-hop dependency view."""
    target = graph_pages.focus_page_path(destination, node.id)
    href = mermaid.relative_link(target, page, ".html")
    label = html.escape(f"Open local dependency context for {node.title}", quote=True)
    icon = (
        '<svg class="bp-context-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
        '<circle cx="6" cy="12" r="2.25"/><circle cx="18" cy="6" r="2.25"/>'
        '<circle cx="18" cy="18" r="2.25"/><path d="m8 11 7.8-4M8 13l7.8 4"/>'
        "</svg>"
    )
    return (
        f'<a class="bp-context-link" href="{html.escape(href, quote=True)}" '
        f'aria-label="{label}" title="{label}">{icon}</a>'
    )


def _dependency_disclosure(
    node: Node,
    *,
    graph: Graph,
    statuses: dict[str, status.NodeStatus],
    numbers: dict[str, str],
    used_by: dict[str, list[str]],
    links: dict[str, str],
) -> str:
    """Hide DAG relations behind a native, keyboard-accessible disclosure."""
    rows: list[tuple[str, str]] = []

    def references(node_ids: list[str] | tuple[str, ...]) -> str:
        rendered = []
        for other_id in node_ids:
            other = graph.nodes[other_id]
            label = html.escape(f"{numbers[other_id]} ({other.title})")
            rendered.append(
                f'<a class="bp-ref bp-ref-{statuses[other_id].key}" '
                f'href="{html.escape(links[other_id], quote=True)}">{label}</a>'
            )
        return " · ".join(rendered)

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


def _split_body(text: str) -> tuple[str, str]:
    """Return the node's statement and whatever trailing sections follow it.

    Only the statement belongs inside the theorem environment; ``## Sources``
    and friends are page material that sits after it, the way a blueprint sets
    a statement apart from the prose around it.
    """
    body = _body_without_dependencies(text)
    lines = body.splitlines()
    fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is None and _HEADING.match(line):
            statement = "\n".join(lines[:index]).strip()
            # Many statements now share one chapter page, so a node's own
            # subheadings must not compete with the chapter's structure.
            return statement, _demote_headings("\n".join(lines[index:]).strip())
    return body.strip(), ""


def _demote_headings(text: str) -> str:
    def demote(line: str) -> str:
        heading = _HEADING.match(line)
        if heading is None:
            return line
        level = min(len(heading.group(1)) + 4, 6)
        return f"{'#' * level} {heading.group(2)}"

    return _outside_fences(text, demote)


def _body_without_dependencies(text: str) -> str:
    """Drop the frontmatter, the H1, and the dependency sections.

    The DAG is re-presented in the metadata line, so repeating the raw link
    lists on the page would only duplicate it.
    """
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                start = index + 1
                break

    kept: list[str] = []
    skipping = False
    dropped_title = False
    fence: tuple[str, int] | None = None

    for line in lines[start:]:
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            if not skipping:
                kept.append(line)
            continue
        if fence is not None:
            if not skipping:
                kept.append(line)
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            name = heading.group(2).strip().casefold()
            if level == 1 and not dropped_title:
                dropped_title = True
                skipping = False
                continue
            if level <= 2:
                skipping = level == 2 and name in _DEPENDENCY_SECTIONS
                if skipping:
                    continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


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

/* The map is the page's subject, so it gets a panel of its own and the
   legend rides with it instead of becoming a section further down. */
.bp-map {{
  margin: 2.5rem 0;
  border: 1px solid var(--bp-rule);
  border-radius: var(--bp-radius);
  background: var(--bp-surface);
  overflow: hidden;
}}
.bp-map-head {{
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.75rem;
  padding: 0.9rem 1.25rem;
  border-bottom: 1px solid var(--bp-rule);
  background: var(--bp-sunken);
}}
.bp-map-title {{ font-size: 0.82rem; font-weight: 700; letter-spacing: -0.01em; }}
.bp-map-hint {{ font-size: 0.7rem; color: var(--bp-muted); }}
.bp-map .mermaid {{ margin: 0; padding: 1.75rem 1.25rem; text-align: center; }}
.bp-map-legend {{
  padding: 1rem 1.25rem;
  border-top: 1px solid var(--bp-rule);
  background: var(--bp-sunken);
}}

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
/* The structure page is about paths, so it is set in the mono face and the
   three columns are a grid: filenames stay on their indent, and the states
   line up down the page where a mismatch is easy to spot. */
.bp-tree {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: baseline;
  gap: 0.3rem 1.25rem;
  margin: 1.5rem 0;
  padding: 1rem 1.25rem;
  border: 1px solid var(--bp-rule);
  border-radius: var(--bp-radius);
  background: var(--bp-surface);
  font-family: {mono};
  font-size: 0.68rem;
}}
.bp-tree-path {{ overflow-wrap: anywhere; }}
.bp-tree-title {{ font-family: {sans}; color: var(--bp-muted); margin-left: 0.5rem; }}
.bp-tree-kind {{ color: var(--bp-muted); }}
.bp-tree-mark {{ display: inline-flex; align-items: center; gap: 0.4rem; white-space: nowrap; }}
.bp-tree-state {{ color: var(--bp-muted); }}
.bp-tree-warn {{
  margin: 1rem 0;
  padding: 0.8rem 1rem;
  border: 1px solid var(--bp-rule);
  border-left: 3px solid #F7B928;
  border-radius: var(--bp-radius);
  background: var(--bp-sunken);
  font-size: 0.75rem;
}}

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
.bp-code-links {{ display: inline-flex; align-items: center; gap: 0.2rem; margin-left: 0.45rem; }}
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
.bp-context-link {{
  display: inline-flex;
  align-items: center;
  margin-left: 0.3rem;
  color: var(--bp-muted);
  text-decoration: none;
}}
.bp-context-link:visited {{ color: var(--bp-muted); }}
.bp-context-link:hover, .bp-context-link:visited:hover {{ color: var(--bp-link-hover); text-decoration: none; }}
.bp-context-icon {{
  width: 1rem;
  height: 1rem;
  fill: var(--bp-surface);
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
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
"""


__all__ = [
    "DECLARATION_LABELS",
    "LOGO",
    "MERMAID_SCRIPT",
    "PUBLICATION_MANIFEST",
    "PublicationError",
    "STYLESHEET",
    "RenderReport",
    "render_site",
]
