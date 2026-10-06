"""Resolve blueprint ``lean:`` declarations to their source location.

Scanning the project's own Lean files keeps two promises at once: a proved node
can link to the line that proves it, and a ``lean:`` name that resolves to
nothing is a validation error rather than a broken link -- the job
``leanblueprint checkdecls`` does for LaTeX blueprints.

The scanner is a lexical pass, not an elaborator. It tracks ``namespace`` and
comment nesting, which is enough for declarations written in the ordinary way,
and deliberately reports nothing it cannot see rather than guessing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote_from_bytes, urlsplit, urlunsplit

from . import _directory_binding as directory_binding
from ._tree_snapshot import (
    BoundDirectoryTree,
    OpaqueDirectoryMarker,
    TreeCaptureLimits,
    TreeChangedError,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
)

_NAMESPACE = re.compile(r"^\s*namespace\s+(.+)$")
_SECTION = re.compile(
    r"^\s*(?:(?:public|private|noncomputable|unsafe|local)\s+)*section\b\s*(\S*)"
)
_END = re.compile(r"^\s*end\b\s*(\S*)")
_DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:public|private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
    r"(theorem|lemma|def|abbrev|instance|structure|class|inductive|opaque|axiom|irreducible_def)\s+(.+)$"
)
_IGNORED_DIRECTORIES = frozenset(
    {
        ".direnv",
        ".git",
        ".lake",
        ".obsidian",
        ".trash",
        ".venv",
        "lake-packages",
    }
)
# ``Build`` can be a Lean namespace directory, so only this spelling is ignored.
_EXACT_IGNORED_DIRECTORIES = frozenset({"build"})
_MANAGED_OUTPUT_MANIFEST = "manifest.json"
_MANAGED_OUTPUT_MANIFEST_BYTE_LIMIT = 16 * 1024 * 1024
# Skeleton manifests grow with the declaration set, and their sorted schema key
# follows the entries, so their classification bound is intentionally larger.
_SNAPSHOT_ATTEMPTS = 3
_SNAPSHOT_RETRY_DELAY_SECONDS = 0.05
#: Known schemas of the skeleton command's packet and passage manifests.
PACKET_SCHEMA = "autoform-skeleton-packets/v2"
PASSAGE_SCHEMA = "autoform-skeleton-passages/v2"
MANAGED_OUTPUT_SCHEMAS = frozenset(
    {
        ("packets", "autoform-skeleton-packets/v1"),
        ("packets", PACKET_SCHEMA),
        ("passages", "autoform-skeleton-passages/v1"),
        ("passages", PASSAGE_SCHEMA),
    }
)

@dataclass(frozen=True, slots=True)
class Declaration:
    """One Lean declaration found in the project's sources."""

    name: str
    path: Path
    line: int
    keyword: str


@dataclass(frozen=True, slots=True)
class SourceIndex:
    """Every declaration the scanner found, keyed by fully qualified name."""

    root: Path
    declarations: dict[str, Declaration]
    source_digest: str
    line_counts: dict[Path, int] = field(default_factory=dict)

    def find(self, name: str) -> Declaration | None:
        return self.declarations.get(name)


@dataclass(frozen=True, slots=True)
class IndexedSourceSnapshot:
    """One source generation used for both declaration links and its digest."""

    index: SourceIndex
    revision: str
    generation_revision: str = ""
    source_files: tuple[tuple[Path, bytes], ...] = ()


@dataclass(slots=True)
class BoundProjectSources:
    """A retained Lean source root whose captures cannot change path generation."""

    root: Path
    tree: BoundDirectoryTree
    excluded: tuple[PurePosixPath, ...]

    def capture(
        self,
        *,
        names: Iterable[str] | None = None,
    ) -> IndexedSourceSnapshot:
        snapshot = self.tree.capture()
        return _indexed_source_snapshot(
            self.root,
            snapshot,
            self.excluded,
            names=names,
        )

    def verify(self) -> None:
        self.tree.verify()

    def close(self) -> None:
        self.tree.close()


class LeanSourceError(OSError):
    """Lean sources could not be indexed; the reason names only project-relative paths."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            "".join(
                character if character.isprintable() else character.encode("unicode_escape").decode("ascii")
                for character in reason
            )
        )
def index_failure_message(error: OSError) -> str:
    """Describe an indexing failure without exposing host paths."""

    if isinstance(error, LeanSourceError):
        return f"Lean sources could not be indexed: {error}"
    return "Lean sources could not be indexed"


def index_project(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
    names: Iterable[str] | None = None,
) -> SourceIndex:
    """Scan ``*.lean`` beneath *root* and index declarations by full name."""
    requested_root = directory_binding.lexical_absolute_path(root)
    root_path = requested_root.resolve()
    if not root_path.is_dir():
        digest = hashlib.sha256(b"autoform-lean-source-index/v1\0").hexdigest()
        return SourceIndex(root=root_path, declarations={}, source_digest=digest)
    remapped_exclusions = tuple(
        _remap_resolved_root_exclusion(requested_root, root_path, value)
        for value in exclude_roots
    )
    return snapshot_project_sources(
        root_path,
        exclude_roots=remapped_exclusions,
        names=names,
    ).index


def _remap_resolved_root_exclusion(
    requested_root: Path,
    resolved_root: Path,
    value: str | Path,
) -> Path:
    """Keep absolute descendant exclusions attached when a root alias resolves."""

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return candidate
    relative = _relative_to_root_alias(candidate, requested_root)
    return resolved_root / relative if relative is not None else candidate


def _relative_to_root_alias(
    candidate: Path,
    root: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> Path | None:
    """Return a lexical tail when *candidate* uses another spelling of *root*."""

    try:
        return candidate.relative_to(root)
    except ValueError:
        pass
    if not candidate.is_absolute() or not root.is_absolute():
        return None
    candidate_parts = candidate.parts
    root_parts = root.parts
    if len(candidate_parts) < len(root_parts):
        return None
    if tuple(
        unicodedata.normalize("NFC", part).casefold()
        for part in candidate_parts[: len(root_parts)]
    ) != tuple(
        unicodedata.normalize("NFC", part).casefold() for part in root_parts
    ):
        return None
    prefix = Path(*candidate_parts[: len(root_parts)])
    try:
        metadata = prefix.stat()
    except (OSError, ValueError) as error:
        if expected_identity is not None:
            raise TreeChangedError(
                "directory tree changed while exclusions were selected"
            ) from error
        return None
    observed = (metadata.st_dev, metadata.st_ino)
    if expected_identity is not None:
        if observed != expected_identity:
            raise TreeChangedError(
                "directory tree changed while exclusions were selected"
            )
    else:
        try:
            if not prefix.samefile(root):
                return None
        except (OSError, ValueError):
            return None
    return Path(*candidate_parts[len(root_parts) :])


def snapshot_project_sources(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
    limits: TreeCaptureLimits = TreeCaptureLimits(),
    names: Iterable[str] | None = None,
) -> IndexedSourceSnapshot:
    """Read each Lean source once and derive its index and revision together.

    A bind or capture that races a concurrent edit is retried a bounded number
    of times.
    """

    exclusions = tuple(exclude_roots)
    requested_names = None if names is None else tuple(names)
    changed: TreeChangedError | None = None
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        if attempt:
            time.sleep(_SNAPSHOT_RETRY_DELAY_SECONDS * attempt)
        try:
            with bind_project_sources(
                root,
                exclude_roots=exclusions,
                limits=limits,
            ) as bound:
                return bound.capture(names=requested_names)
        except TreeChangedError as error:
            changed = error
        except TreeSnapshotError as error:
            raise LeanSourceError(str(error)) from error
    lasting = _lasting_root_failure(root)
    if lasting is not None:
        raise LeanSourceError(lasting) from changed
    raise LeanSourceError("Lean sources kept changing while they were indexed") from changed


def _lasting_root_failure(root: str | Path) -> str | None:
    """Name a stable root-kind failure after retryable bind errors are exhausted."""

    path = directory_binding.lexical_absolute_path(root)
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return "directory tree cannot be inspected safely: directory root does not exist"
    except NotADirectoryError:
        return "directory tree cannot be inspected safely: directory root is not a directory"
    except (OSError, ValueError):
        return None
    if not stat.S_ISDIR(metadata.st_mode):
        return "directory tree cannot be inspected safely: directory root is not a directory"
    return None


def project_source_revision(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
    limits: TreeCaptureLimits = TreeCaptureLimits(),
) -> str:
    """Hash the exact Lean source set consumed by :func:`index_project`."""
    return snapshot_project_sources(
        root,
        exclude_roots=exclude_roots,
        limits=limits,
    ).revision


@contextmanager
def bind_project_sources(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
    limits: TreeCaptureLimits = TreeCaptureLimits(),
) -> Iterator[BoundProjectSources]:
    """Retain a Lean root while one or more source snapshots are consumed."""

    bound = open_project_sources(
        root,
        exclude_roots=exclude_roots,
        limits=limits,
    )
    try:
        yield bound
    finally:
        bound.close()


def open_project_sources(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
    limits: TreeCaptureLimits = TreeCaptureLimits(),
) -> BoundProjectSources:
    """Open a retained Lean source root; the caller must close it.

    A root that changes while it is bound raises ``TreeChangedError``, which a
    retry may clear.
    """

    root_path = directory_binding.lexical_absolute_path(root)
    try:
        tree = BoundDirectoryTree(root_path, require_descriptor=True)
    except TreeChangedError:
        raise
    except TreeSnapshotError as error:
        raise LeanSourceError(str(error)) from error
    try:
        excluded = _project_exclusions(
            root_path,
            exclude_roots,
            root_identity=tree.identity,
        )
        tree.selection = _lean_tree_selection(excluded, limits=limits)
        tree.verify()
    except BaseException:
        tree.close()
        raise
    return BoundProjectSources(root_path, tree, excluded)


@contextmanager
def bind_project_source_snapshot(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
    limits: TreeCaptureLimits = TreeCaptureLimits(),
) -> Iterator[tuple[BoundProjectSources, IndexedSourceSnapshot]]:
    """Retain and yield one initially stable source generation.

    Initial bind or capture races get the same bounded retry and sanitized
    failure as :func:`snapshot_project_sources`; the successful binding stays
    open so a caller can verify and recapture it after using the evidence.
    """

    exclusions = tuple(exclude_roots)
    changed: TreeChangedError | None = None
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        if attempt:
            time.sleep(_SNAPSHOT_RETRY_DELAY_SECONDS * attempt)
        bound: BoundProjectSources | None = None
        try:
            bound = open_project_sources(root, exclude_roots=exclusions, limits=limits)
            snapshot = bound.capture()
        except TreeChangedError as error:
            changed = error
            if bound is not None:
                bound.close()
            continue
        except TreeSnapshotError as error:
            if bound is not None:
                bound.close()
            raise LeanSourceError(str(error)) from error
        except BaseException:
            if bound is not None:
                bound.close()
            raise
        try:
            yield bound, snapshot
        finally:
            bound.close()
        return
    lasting = _lasting_root_failure(root)
    if lasting is not None:
        raise LeanSourceError(lasting) from changed
    raise LeanSourceError("Lean sources kept changing while they were indexed") from changed


def _project_exclusions(
    root: Path,
    values: Iterable[str | Path],
    *,
    root_identity: tuple[int, int],
) -> tuple[PurePosixPath, ...]:
    excluded: list[PurePosixPath] = []
    for value in values:
        candidate = Path(value).expanduser()
        if any("\0" in part for part in candidate.parts):
            raise OSError("Lean exclusion path cannot be inspected safely")
        if candidate.is_absolute():
            relative = _relative_to_root_alias(
                candidate,
                root,
                expected_identity=root_identity,
            )
            if relative is None:
                continue
            candidate = relative
        relative = PurePosixPath(candidate.as_posix())
        if (
            not relative.parts
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            continue
        excluded.append(relative)
    return tuple(excluded)


def _lean_tree_selection(
    excluded: tuple[PurePosixPath, ...],
    *,
    limits: TreeCaptureLimits = TreeCaptureLimits(),
) -> TreeSelection:
    return TreeSelection(
        include=lambda path, mode: _lean_snapshot_includes(
            path,
            mode,
            excluded,
        ),
        descend=lambda path: not _lean_path_is_excluded(path, excluded),
        byte_limit=lambda path: (
            _MANAGED_OUTPUT_MANIFEST_BYTE_LIMIT
            if _is_managed_output_manifest_name(path.name)
            else None
        ),
        # Omitted marker entries let the immutable snapshot classify nested Git
        # checkouts without consulting live pathnames after capture.
        record_omitted=True,
        limits=limits,
        opaque_markers=(
            # A nested Git checkout is another source tree.  Treat the
            # presence of either a worktree/submodule .git file or a clone's
            # .git directory as a bounded sentinel before visiting children.
            OpaqueDirectoryMarker(
                ".git",
                0,
                lambda _data: False,
                presence_only=True,
            ),
            OpaqueDirectoryMarker(
                _MANAGED_OUTPUT_MANIFEST,
                _MANAGED_OUTPUT_MANIFEST_BYTE_LIMIT,
                _is_managed_output_manifest_bytes,
            ),
        ),
    )


def _lean_snapshot_includes(
    relative: PurePosixPath,
    mode: int,
    excluded: tuple[PurePosixPath, ...],
) -> bool:
    if _lean_path_is_excluded(relative, excluded):
        return False
    if stat.S_ISLNK(mode):
        return (
            relative.suffix.casefold() == ".lean"
            and not relative.name.startswith(".#")
        )
    return (
        stat.S_ISDIR(mode)
        or relative.suffix.casefold() == ".lean"
        or (
            len(relative.parts) > 1
            and _is_managed_output_manifest_name(relative.name)
        )
    )


def _lean_path_is_excluded(
    relative: PurePosixPath,
    excluded: tuple[PurePosixPath, ...],
) -> bool:
    folded_parts = _folded_path(relative)
    return (
        bool(_IGNORED_DIRECTORIES.intersection(folded_parts))
        or bool(_EXACT_IGNORED_DIRECTORIES.intersection(relative.parts))
        or any(
            len(folded_parts) >= len(prefix_parts)
            and folded_parts[: len(prefix_parts)] == prefix_parts
            for prefix in excluded
            if (prefix_parts := _folded_path(prefix))
        )
    )


def _folded_path(path: PurePosixPath) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def _indexed_source_snapshot(
    root: Path,
    snapshot: TreeSnapshot,
    excluded: tuple[PurePosixPath, ...],
    *,
    names: Iterable[str] | None = None,
) -> IndexedSourceSnapshot:
    managed_output_manifests: dict[
        PurePosixPath,
        list[tuple[str, bytes | None]],
    ] = {}

    def add_manifest(relative: str, kind: str, data: bytes | None = None) -> None:
        path = PurePosixPath(relative)
        if len(path.parts) > 1 and _is_managed_output_manifest_name(path.name):
            managed_output_manifests.setdefault(path.parent, []).append((kind, data))

    for relative, data in snapshot.files:
        add_manifest(relative, "file", data)
    for relative, _target in snapshot.symlinks:
        add_manifest(relative, "symlink")
    for relative, _mode in snapshot.special:
        add_manifest(relative, "special")
    for relative in snapshot.placeholders:
        add_manifest(relative, "placeholder")
    for relative in snapshot.directories:
        add_manifest(relative, "directory")

    # An unrecognized manifest marks nothing; its directory stays Lean source.
    ignored_roots: set[PurePosixPath] = {
        PurePosixPath(relative)
        for relative in snapshot.opaque_directories
        if relative
    }
    ignored_roots.update(
        marker.parent
        for relative, kind in snapshot.omitted
        if kind in {"directory", "file"}
        and len((marker := PurePosixPath(relative)).parts) > 1
        and unicodedata.normalize("NFC", marker.name).casefold() == ".git"
    )
    for parent in sorted(
        managed_output_manifests,
        key=lambda path: (len(path.parts), path.as_posix()),
    ):
        if _path_is_within_roots(parent, ignored_roots):
            continue
        manifests = managed_output_manifests[parent]
        if (
            len(manifests) == 1
            and manifests[0][0] == "file"
            and manifests[0][1] is not None
            and _is_managed_output_manifest_bytes(manifests[0][1])
        ):
            ignored_roots.add(parent)

    def in_ignored_root(relative_text: str) -> bool:
        return _path_is_within_roots(PurePosixPath(relative_text), ignored_roots)

    # Editor lock links (``.#Name.lean``) are never read, so they are skipped;
    # any other ``.lean`` link, live or dangling, is refused.
    tolerated_links = frozenset(
        relative
        for relative, kind in (
            *((relative, "symlink") for relative, _target in snapshot.symlinks),
            *snapshot.omitted,
        )
        if kind == "symlink"
        and PurePosixPath(relative).suffix.casefold() == ".lean"
        and not in_ignored_root(relative)
        and PurePosixPath(relative).name.startswith(".#")
    )
    unsupported = [
        (relative, reason)
        for relative, reason in snapshot.unsupported_entries()
        if not in_ignored_root(relative)
        and PurePosixPath(relative).suffix.casefold() == ".lean"
        and relative not in tolerated_links
    ]
    if unsupported:
        relative, reason = unsupported[0]
        raise LeanSourceError(f"unsafe Lean source {relative}: {reason}")

    declarations: dict[str, Declaration] = {}
    line_counts: dict[Path, int] = {}
    source_files: list[tuple[Path, bytes]] = []
    digest = hashlib.sha256(b"autoform-lean-source-index/v1\0")
    wanted_short = (
        None
        if names is None
        else frozenset(_short_name(name) for name in names)
    )
    for relative_text, data in snapshot.files:
        relative = PurePosixPath(relative_text)
        if relative.suffix.casefold() != ".lean" or _lean_path_is_excluded(
            relative,
            excluded,
        ):
            continue
        if _path_is_within_roots(relative, ignored_roots):
            continue
        relative_path = Path(relative.as_posix())
        _update_source_digest(digest, relative_path, data)
        source_files.append((relative_path, data))
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            continue
        line_counts[relative_path] = len(text.splitlines())
        if wanted_short is not None and not _may_declare(text, wanted_short):
            continue
        for declaration in _scan(text, relative_path):
            declarations.setdefault(declaration.name, declaration)
    source_digest = digest.hexdigest()
    return IndexedSourceSnapshot(
        SourceIndex(
            root=root,
            declarations=declarations,
            source_digest=source_digest,
            line_counts=line_counts,
        ),
        source_digest,
        _lean_generation_revision(snapshot, ignored_roots, tolerated_links),
        tuple(source_files),
    )


def _may_declare(text: str, wanted_short: frozenset[str]) -> bool:
    """Reject files that cannot contain a requested declaration."""

    if not wanted_short:
        return False
    for line in _without_lean_comments(text).splitlines():
        match = _DECLARATION.match(line)
        if match is None:
            continue
        name = _name_token(match.group(2))
        if name is not None and _short_name(name.removesuffix(".")) in wanted_short:
            return True
    return False


def _short_name(name: str) -> str:
    """Return the last Lean name component without splitting quoted dots."""

    if name.endswith("»") and "«" in name:
        return name[name.rfind("«") :]
    return name.rsplit(".", 1)[-1]


def _lean_generation_revision(
    snapshot: TreeSnapshot,
    ignored_roots: set[PurePosixPath],
    tolerated_links: frozenset[str],
) -> str:
    """Hash only effective Lean inputs and their ancestor directories."""

    def retained_entry(relative_text: str) -> bool:
        relative = PurePosixPath(relative_text)
        return relative.suffix.casefold() == ".lean" and not _path_is_within_roots(
            relative,
            ignored_roots,
        )

    files = tuple(entry for entry in snapshot.files if retained_entry(entry[0]))
    symlinks = tuple(
        entry
        for entry in snapshot.symlinks
        if retained_entry(entry[0]) and entry[0] not in tolerated_links
    )
    special = tuple(entry for entry in snapshot.special if retained_entry(entry[0]))
    placeholders = tuple(path for path in snapshot.placeholders if retained_entry(path))
    omitted = tuple(
        entry
        for entry in snapshot.omitted
        if retained_entry(entry[0]) and entry[0] not in tolerated_links
    )
    retained_paths = {
        PurePosixPath(relative)
        for relative, _value in (*files, *symlinks, *special, *omitted)
    }
    retained_paths.update(PurePosixPath(relative) for relative in placeholders)
    retained_directories = {PurePosixPath()}
    for path in retained_paths:
        retained_directories.update(path.parents)

    directories = tuple(
        path
        for path in snapshot.directories
        if PurePosixPath(path) in retained_directories
    )
    retained_identity_paths = set(directories)
    retained_identity_paths.update(path.as_posix() for path in retained_paths)
    filtered = TreeSnapshot(
        root_identity=snapshot.root_identity,
        directories=directories,
        files=files,
        symlinks=symlinks,
        special=special,
        placeholders=placeholders,
        omitted=omitted,
        identities=tuple(
            entry for entry in snapshot.identities if entry[0] in retained_identity_paths
        ),
        opaque_directories=(),
    )
    return filtered.generation_revision


def _is_managed_output_manifest_bytes(data: bytes) -> bool:
    if len(data) > _MANAGED_OUTPUT_MANIFEST_BYTE_LIMIT:
        return False
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return False
    if not isinstance(value, dict):
        return False
    kind = value.get("kind")
    schema = value.get("schema")
    return (
        isinstance(kind, str)
        and isinstance(schema, str)
        and (kind, schema) in MANAGED_OUTPUT_SCHEMAS
        and isinstance(value.get(kind), list)
    )


def _is_managed_output_manifest_name(name: str) -> bool:
    return unicodedata.normalize("NFC", name).casefold() == _MANAGED_OUTPUT_MANIFEST


def _path_is_within_roots(
    path: PurePosixPath,
    roots: set[PurePosixPath],
) -> bool:
    return any(candidate in roots for candidate in (path, *path.parents))


def _update_source_digest(digest, relative: Path, data: bytes) -> None:
    encoded = os.fsencode(relative.as_posix())
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def _scan(text: str, relative: Path) -> list[Declaration]:
    found: list[Declaration] = []
    namespaces: list[str] = []
    scopes: list[str | None] = []

    for number, line in enumerate(_without_lean_comments(text).splitlines(), start=1):
        if not line.strip():
            continue

        namespace_match = _NAMESPACE.match(line)
        if namespace_match:
            name = _name_token(namespace_match.group(1))
            if name is None:
                continue
            namespaces.append(name)
            scopes.append(name)
            continue

        section_match = _SECTION.match(line)
        if section_match:
            scopes.append(None)
            continue

        end_match = _END.match(line)
        if end_match:
            if scopes:
                closed = scopes.pop()
                if closed is not None and namespaces:
                    namespaces.pop()
            continue

        declaration_match = _DECLARATION.match(line)
        if declaration_match:
            keyword = declaration_match.group(1)
            name = _name_token(declaration_match.group(2))
            if name is None:
                continue
            # `_name_token` stops before the `{` in an explicit universe
            # binder, leaving the syntactic separator behind.
            name = re.sub(r"\.\{[^}\n]+\}$", "", name).removesuffix(".")
            qualified = ".".join([*namespaces, name])
            found.append(Declaration(qualified, relative, number, keyword))
    return found


_NAME_TOKEN = re.compile(r"(?:«[^«»]*»|[^\s:(){}\[\]⦃⦄,«»])+")


def _name_token(text: str) -> str | None:
    """Read one possibly guillemet-quoted Lean identifier from ``text``."""

    match = _NAME_TOKEN.match(text)
    return match.group() if match and text[match.end() : match.end() + 1] not in ("«", "»") else None


_RAW_OPEN = re.compile(r'r(#*)"')
_CHAR_LITERAL = re.compile(r"'(?:\\.|[^'\\\n])*'")


def _without_lean_comments(text: str) -> str:
    """Remove nested Lean comments, keeping strings intact and newlines in place."""

    out: list[str] = []
    stack: list[list] = []  # [closer, interpolated] for a string, [None, depth] for `{…}` in `s!"…"`
    index = 0
    while index < len(text):
        top = stack[-1] if stack else None
        char = text[index]
        if top is not None and top[0] is not None:
            close, interpolated = top
            if text.startswith(close, index):
                out.append(close)
                index += len(close)
                stack.pop()
                continue
            if close in {'"', "'"} and char == "\\":
                out.append(text[index : index + 2])
                index += 2
                continue
            if interpolated and char == "{":
                stack.append([None, 1])
            out.append(char)
            index += 1
            continue
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            if newline < 0:
                break
            out.append("\n")
            index = newline + 1
            continue
        if text.startswith("/-", index):
            # `/--` and `/-!` open a docstring whose body starts after the marker.
            depth, index = 1, index + (3 if text[index + 2 : index + 3] in {"-", "!"} else 2)
            out.append(" ")
            while index < len(text) and depth:
                pair = text[index : index + 2]
                if pair in {"-/", "/-"}:
                    depth += 1 if pair == "/-" else -1
                    index += 2
                    continue
                if text[index] == "\n":
                    out.append("\n")
                index += 1
            continue
        raw = _RAW_OPEN.match(text, index)
        if raw:
            out.append(raw.group())
            index = raw.end()
            stack.append(['"' + raw.group(1), False])
            continue
        previous = text[index - 1] if index else " "
        if char == '"':
            interpolated = previous == "!" and index >= 2 and (text[index - 2].isalnum() or text[index - 2] == "_")
            stack.append(['"', interpolated])
        elif char == "«":
            stack.append(["»", False])
        elif char == "'" and not (previous.isalnum() or previous in "_'") and _CHAR_LITERAL.match(text, index):
            stack.append(["'", False])
        elif top is not None and char in "{}":
            top[1] += 1 if char == "{" else -1
            if top[1] == 0:
                stack.pop()
        out.append(char)
        index += 1
    return "".join(out)


def strip_lean_comments(text: str) -> str:
    """Remove every line and block comment, docstrings included, from Lean source."""

    return "\n".join(
        line.rstrip() for line in _without_lean_comments(text).splitlines() if line.strip()
    )


_DECLARATION_NAME = re.compile(r"(?:«[^»]*(?:»|$)|[^\s,«])+")


def declaration_names(lean: str) -> list[str]:
    """Split a ``lean:`` frontmatter value into individual declaration names."""

    return _DECLARATION_NAME.findall(lean)


@dataclass(frozen=True, slots=True)
class SourceLinker:
    """Build permalinks into the project's Lean sources."""

    index: SourceIndex
    repository_url: str | None = None
    ref: str | None = None
    linkable_paths: frozenset[Path] | None = None

    def location(self, name: str) -> Declaration | None:
        return self.index.find(name)

    def url(self, name: str) -> str | None:
        """Return a permanent link to *name*, or ``None`` if it cannot be built."""
        declaration = self.index.find(name)
        if (
            declaration is None
            or not self.repository_url
            or not self.ref
            or (
                self.linkable_paths is not None
                and declaration.path not in self.linkable_paths
            )
        ):
            return None
        encoded_ref = quote_from_bytes(os.fsencode(self.ref), safe="")
        path = "/".join(
            quote_from_bytes(os.fsencode(part), safe="")
            for part in declaration.path.parts
        )
        return f"{self.repository_url}/blob/{encoded_ref}/{path}#L{declaration.line}"


def build_linker(
    lean_root: str | Path,
    *,
    repository_url: str | None = None,
    ref: str | None = None,
    names: Iterable[str] | None = None,
    exclude_roots: Iterable[str | Path] = (),
    source_index: SourceIndex | None = None,
    detect_missing: bool = True,
) -> SourceLinker:
    """Index *lean_root* and resolve repository coordinates.

    A supplied source snapshot never inherits a live Git ref; callers must bind
    that ref explicitly if they want permalinks.
    """
    requested_root = directory_binding.lexical_absolute_path(lean_root)
    resolved_root = requested_root.resolve()
    remapped_exclusions = tuple(
        _remap_resolved_root_exclusion(requested_root, resolved_root, value)
        for value in exclude_roots
    )
    if source_index is not None and source_index.root != resolved_root:
        raise ValueError("captured source index belongs to a different Lean root")
    resolved_repository_url = repository_url
    resolved_ref = ref
    linkable_paths: frozenset[Path] | None = None
    index = source_index
    if source_index is None:
        if detect_missing and ref is None:
            snapshot, detected_url, detected_ref, linkable_paths = _capture_link_state(
                resolved_root,
                remapped_exclusions,
                names=names,
            )
            index = snapshot.index
            resolved_repository_url = repository_url or detected_url
            resolved_ref = detected_ref
        else:
            index = index_project(
                resolved_root,
                exclude_roots=remapped_exclusions,
                names=names,
            )
    if detect_missing and resolved_repository_url is None:
        resolved_repository_url = detect_repository_url(resolved_root)
    assert index is not None
    return SourceLinker(
        index=index,
        repository_url=resolved_repository_url,
        ref=resolved_ref,
        linkable_paths=linkable_paths,
    )


def _capture_link_state(
    root: Path,
    exclude_roots: Iterable[str | Path],
    *,
    names: Iterable[str] | None = None,
) -> tuple[
    IndexedSourceSnapshot,
    str | None,
    str | None,
    frozenset[Path] | None,
]:
    """Capture source bytes and prove which ones belong to one stable commit."""

    exclusions = tuple(exclude_roots)
    requested_names = None if names is None else tuple(names)
    last_snapshot: IndexedSourceSnapshot | None = None
    last_url: str | None = None
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        if attempt:
            time.sleep(_SNAPSHOT_RETRY_DELAY_SECONDS * attempt)
        before_url = detect_repository_url(root)
        before_ref = detect_ref(root)
        snapshot = snapshot_project_sources(
            root,
            exclude_roots=exclusions,
            names=requested_names,
        )
        last_snapshot = snapshot
        last_url = before_url
        after_url = detect_repository_url(root)
        after_ref = detect_ref(root)
        if before_url != after_url or before_ref != after_ref:
            continue
        if before_ref is None:
            return snapshot, before_url, None, frozenset()
        commit = _git(root, "rev-parse", "--verify", f"{before_ref}^{{commit}}")
        if commit is None:
            return snapshot, before_url, None, frozenset()
        linkable = _committed_source_paths(root, snapshot, commit)
        if linkable is None:
            return snapshot, before_url, None, frozenset()
        if (
            detect_repository_url(root) == before_url
            and detect_ref(root) == before_ref
        ):
            return snapshot, before_url, commit, linkable
    assert last_snapshot is not None
    return last_snapshot, last_url, None, frozenset()


def _committed_source_paths(
    root: Path,
    snapshot: IndexedSourceSnapshot,
    commit: str,
) -> frozenset[Path] | None:
    """Return captured files whose Git blob is exactly the commit's blob."""

    top_level_text = _git(root, "rev-parse", "--show-toplevel")
    algorithm = _git(root, "rev-parse", "--show-object-format")
    if top_level_text is None or algorithm not in {"sha1", "sha256"}:
        return None
    top_level = Path(top_level_text).resolve()
    try:
        prefix = root.resolve().relative_to(top_level)
    except (OSError, RuntimeError, ValueError):
        return None
    if prefix.parts:
        # SourceLinker URLs are rooted at ``lean_root`` today.  Until that API
        # carries a repository-relative prefix, refusing links is safer than
        # emitting a valid commit with the wrong path.
        return frozenset()
    repo_paths = {
        path: (prefix / path).as_posix()
        for path, _data in snapshot.source_files
    }
    tree_oids = _git_tree_oids(root, commit, tuple(repo_paths.values()))
    if tree_oids is None:
        return None
    linkable: set[Path] = set()
    for path, data in snapshot.source_files:
        framed = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
        if hashlib.new(algorithm, framed).hexdigest() == tree_oids.get(repo_paths[path]):
            linkable.add(path)
    return frozenset(linkable)


def _git_tree_oids(
    root: Path,
    commit: str,
    paths: tuple[str, ...],
) -> dict[str, str] | None:
    """Read raw tree object IDs for exact paths without Git's quoting layer."""

    result: dict[str, str] = {}
    for start in range(0, len(paths), 128):
        batch = paths[start : start + 128]
        if not batch:
            continue
        try:
            completed = subprocess.run(
                ["git", "ls-tree", "-rz", "--full-tree", commit, "--", *batch],
                cwd=str(root),
                capture_output=True,
                env=_git_environment(),
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        for record in completed.stdout.split(b"\0"):
            if not record:
                continue
            header, separator, encoded_path = record.partition(b"\t")
            fields = header.split()
            if not separator or len(fields) != 3 or fields[1] != b"blob":
                continue
            result[os.fsdecode(encoded_path)] = fields[2].decode("ascii")
    return result


def detect_repository_url(root: str | Path) -> str | None:
    """Find the project's web URL from the CI environment or the git remote."""
    repository = (
        os.environ.get("GITHUB_REPOSITORY")
        if _is_github_workspace(root)
        else None
    )
    if repository:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server.rstrip('/')}/{repository}"
    remote = _git(root, "config", "--local", "--get", "remote.origin.url")
    return _normalize_remote(remote) if remote else None


def detect_ref(root: str | Path) -> str | None:
    """Prefer the exact commit so links keep pointing at the reviewed code."""
    github_sha = os.environ.get("GITHUB_SHA") if _is_github_workspace(root) else None
    return github_sha or _git(root, "rev-parse", "HEAD")


def _is_github_workspace(root: str | Path) -> bool:
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if not workspace:
        return False
    try:
        resolved_root = Path(root).resolve()
        resolved_workspace = Path(workspace).resolve()
    except (OSError, RuntimeError):
        return False
    return resolved_root == resolved_workspace or resolved_root.is_relative_to(
        resolved_workspace
    )


def _normalize_remote(remote: str) -> str | None:
    remote = remote.strip()
    if remote.startswith("git@"):
        host, _, path = remote[4:].partition(":")
        if not path:
            return None
        remote = f"https://{host}/{path}"
    try:
        parsed = urlsplit(remote)
    except ValueError:
        return None
    if parsed.scheme == "ssh" and parsed.username == "git" and parsed.hostname:
        host = (
            f"[{parsed.hostname}]"
            if ":" in parsed.hostname
            else parsed.hostname
        )
        try:
            port = parsed.port
        except ValueError:
            return None
        remote = f"https://{host}{f':{port}' if port is not None else ''}{parsed.path}"
        try:
            parsed = urlsplit(remote)
        except ValueError:
            return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not path or path == "/":
        return None
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _git(root: str | Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(root),
            capture_output=True,
            env=_git_environment(),
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


def _git_environment() -> dict[str, str]:
    """Make the explicit working directory the only Git repository selector."""

    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


__all__ = [
    "IndexedSourceSnapshot",
    "Declaration",
    "LeanSourceError",
    "MANAGED_OUTPUT_SCHEMAS",
    "PACKET_SCHEMA",
    "PASSAGE_SCHEMA",
    "SourceIndex",
    "SourceLinker",
    "bind_project_source_snapshot",
    "build_linker",
    "declaration_names",
    "detect_ref",
    "detect_repository_url",
    "index_failure_message",
    "index_project",
    "project_source_revision",
    "snapshot_project_sources",
    "strip_lean_comments",
]
