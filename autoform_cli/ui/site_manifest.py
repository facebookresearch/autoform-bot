"""publication.json: the inventory and the manifest write."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import unicodedata
from collections.abc import Collection
from pathlib import Path
from urllib.parse import quote

from .._fs import relative as _relative
from ..annotation_writer import MATHLIB_DIR
from ..annotations import LINKS_DIR
from ..coverage import CoverageSummary
from ..graph import Graph
from ..lean import PUBLICATION_MANIFEST, PUBLICATION_SCHEMA, SourceLinker
from ..markdown import PAGE_SUFFIXES
from ..readback import READBACKS_DIR
from .capture import _FOLDER_REASONS
from .markdown_html import _as_published
from .site_files import _GENERATED_FILES
from .vault_pages import _Publication


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
