"""Reading the blueprint once: what render captures and refuses, the folders a copy leaves out and why,
the private copy main's path-based readers read, the graph loaded from it, and its source revision."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath

from .._fs import path_is
from .._fs import relative as _relative
from .._tree_snapshot import (
    TreeChangedError,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
    _update_digest,
    bind_directory_tree,
)
from ..graph import Graph, GraphValidationError, PublicationError, load_graph, unpublished_content, unpublished_node_ids
from ..markdown import markdown_anchors
from .site_files import _GENERATED_FILES, _GENERATED_PATHS


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


#: The reasons capture leaves out a whole folder for, keeping it on record as one entry.
_FOLDER_REASONS = frozenset({"unpublished", "mkdocs-excluded"})
#: Render reads no hidden, local-only, or root generated file. Omitted entries are
#: still recorded, and .autoform is entered, so sensitive files are refused by name.
_PUBLICATION_SELECTION = TreeSelection(
    include=lambda path, _mode: not (
        _is_hidden(path) or _is_sensitive(path) or (len(path.parts) == 1 and path.name in _GENERATED_FILES)
    ),
    descend=lambda path: path.parts[0].casefold() == ".autoform"
    or not (_is_hidden(path) or _is_sensitive(path)),
)


def _captured_files(snapshot: TreeSnapshot, root: Path) -> dict[Path, bytes]:
    """The captured blueprint's files, keyed by their path below ``root``."""

    return {root / name: data for name, data in snapshot.files}


def _unpublished_folders(snapshot: TreeSnapshot, root: Path) -> tuple[Path, ...]:
    """The folders capture left out, below ``root``: each whose README says
    ``published: false``, and a top-level ``templates/``, which MkDocs leaves out."""

    return tuple(root / name for name, kind in snapshot.omitted if kind in _FOLDER_REASONS)


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


def _capture_blueprint(blueprint: Path) -> tuple[TreeSnapshot, frozenset[str]]:
    """Read the blueprint once, refusing what must not be published.

    As two walks by path that must agree. Folders whose README says ``published:
    false`` are left out before anything is refused or published, so no page,
    hash or check sees them; the node IDs in them come beside the snapshot. So
    is a top-level ``templates/``, which MkDocs leaves out.
    """
    try:
        selection = _PUBLICATION_SELECTION
        with bind_directory_tree(blueprint, selection=selection) as tree:
            snapshot = tree.capture()
    except TreeSnapshotError as error:
        try:
            missing = not path_is(blueprint, stat.S_ISDIR)
        except OSError:  # it may be there; the capture error says why it could not be read
            missing = False
        if missing:
            raise PublicationError([f"blueprint directory does not exist: {blueprint}"]) from error
        if isinstance(error, TreeChangedError):
            raise PublicationError(["blueprint changed while it was read; retry"]) from error
        raise PublicationError([f"blueprint could not be captured: {error}"]) from error
    folders = unpublished_content(snapshot.files)
    node_ids = unpublished_node_ids(snapshot.files, folders)
    # MkDocs' default exclude_docs, where an Obsidian vault keeps its note templates.
    excluded = ("templates",) if "templates" in snapshot.directories else ()

    def kept(name: str) -> bool:
        return not any(name == folder or name.startswith(f"{folder}/") for folder in (*folders, *excluded))

    snapshot = replace(
        snapshot,
        directories=tuple(filter(kept, snapshot.directories)),
        # Each folder stays on record as left out, so a link into one keeps only its text.
        omitted=tuple(
            sorted(
                [
                    *(entry for entry in snapshot.omitted if kept(entry[0])),
                    *((f, "unpublished") for f in folders),
                    *((f, "mkdocs-excluded") for f in excluded),
                ]
            )
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
def _private_copy(snapshot: TreeSnapshot, relative: Path) -> Iterator[tuple[Path, Path]]:
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


def _source_revision(snapshot: TreeSnapshot) -> str:
    """Hash every captured directory and file, each field length-framed."""
    digest = hashlib.sha256(b"autoform-markdown-publication/v3\0")
    for relative in sorted(relative or "." for relative in snapshot.directories):
        _update_digest(digest, b"directory", relative, b"")
    for relative, data in sorted(snapshot.files):
        _update_digest(digest, b"file", relative, data)
    return digest.hexdigest()
