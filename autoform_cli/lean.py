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
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import _directory_binding as directory_binding
from ._tree_snapshot import (
    BoundDirectoryTree,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
)

_NAMESPACE = re.compile(r"^\s*namespace\s+(.+)$")
_SECTION = re.compile(r"^\s*section\b\s*(\S*)")
_END = re.compile(r"^\s*end\b\s*(\S*)")
_DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
    r"(theorem|lemma|def|abbrev|instance|structure|class|inductive|opaque|axiom)\s+(.+)$"
)
_IGNORED_DIRECTORIES = frozenset(
    {
        ".direnv",
        ".git",
        ".lake",
        ".obsidian",
        ".trash",
        ".venv",
        "build",
        "lake-packages",
    }
)
_IGNORED_DIRECTORY_PREFIXES = (".autoform-publication-",)
_PUBLICATION_MANIFEST = "publication.json"
_PUBLICATION_SCHEMAS = frozenset({"autoform-publication/v1", "autoform-publication/v2"})
_PUBLICATION_MANIFEST_BYTE_LIMIT = 1024 * 1024
_MANAGED_OUTPUT_MANIFEST = "manifest.json"
_MANAGED_OUTPUT_MANIFEST_BYTE_LIMIT = 16 * 1024 * 1024
# Skeleton manifests grow with the declaration set, and their sorted schema key
# follows the entries, so their classification bound is intentionally larger.
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_DESCRIPTOR_LISTING_SUPPORTED = os.listdir in getattr(os, "supports_fd", ())
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


@dataclass(slots=True)
class BoundProjectSources:
    """A retained Lean source root whose captures cannot change path generation."""

    root: Path
    tree: BoundDirectoryTree
    exclusion_roots: tuple[Path, ...]

    def capture(self) -> IndexedSourceSnapshot:
        excluded = _project_exclusions(
            self.root,
            self.exclusion_roots,
            root_identity=self.tree.identity,
        )

        def refresh_exclusions() -> tuple[PurePosixPath, ...]:
            return _project_exclusions(
                self.root,
                self.exclusion_roots,
                root_identity=self.tree.identity,
            )

        snapshot = self.tree.capture(
            selection=_lean_tree_selection(
                excluded,
                refresh_exclusions=refresh_exclusions,
            )
        )
        return _indexed_source_snapshot(self.root, snapshot, excluded)

    def verify(self) -> None:
        self.tree.verify()

    def close(self) -> None:
        self.tree.close()


def index_project(
    root: str | Path, *, exclude_roots: Iterable[str | Path] = ()
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
    try:
        relative = candidate.relative_to(requested_root)
    except ValueError:
        return candidate
    return resolved_root / relative


def snapshot_project_sources(
    root: str | Path, *, exclude_roots: Iterable[str | Path] = ()
) -> IndexedSourceSnapshot:
    """Read each Lean source once and derive its index and revision together."""

    with bind_project_sources(root, exclude_roots=exclude_roots) as bound:
        try:
            return bound.capture()
        except TreeSnapshotError as error:
            raise OSError(str(error)) from error


def project_source_revision(
    root: str | Path, *, exclude_roots: Iterable[str | Path] = ()
) -> str:
    """Hash the exact Lean source set consumed by :func:`index_project`."""
    return snapshot_project_sources(root, exclude_roots=exclude_roots).revision


@contextmanager
def bind_project_sources(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
) -> Iterator[BoundProjectSources]:
    """Retain a Lean root while one or more source snapshots are consumed."""

    bound = open_project_sources(root, exclude_roots=exclude_roots)
    try:
        yield bound
    finally:
        bound.close()


def open_project_sources(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
) -> BoundProjectSources:
    """Open a retained Lean source root; the caller must close it."""

    root_path = directory_binding.lexical_absolute_path(root)
    exclusion_paths = tuple(Path(value).expanduser() for value in exclude_roots)
    try:
        tree = BoundDirectoryTree(root_path)
    except TreeSnapshotError as error:
        raise OSError(str(error)) from error
    try:
        excluded = _project_exclusions(
            root_path,
            exclusion_paths,
            root_identity=tree.identity,
        )
        tree.selection = _lean_tree_selection(excluded)
    except BaseException:
        tree.close()
        raise
    return BoundProjectSources(root_path, tree, exclusion_paths)


def _project_exclusions(
    root: Path,
    values: Iterable[str | Path],
    *,
    root_identity: tuple[int, int],
) -> tuple[PurePosixPath, ...]:
    return tuple(
        candidate
        for value in values
        if (
            candidate := _relative_exclusion(
                root,
                value,
                root_identity=root_identity,
            )
        )
        is not None
    )


def _lean_tree_selection(
    excluded: tuple[PurePosixPath, ...],
    *,
    refresh_exclusions: Callable[[], tuple[PurePosixPath, ...]] | None = None,
) -> TreeSelection:
    def is_excluded(path: PurePosixPath) -> bool:
        if _lean_path_is_excluded(path, excluded):
            return True
        if refresh_exclusions is None or not any(
            path != prefix
            and len(path.parts) == len(prefix.parts)
            and _folded_path(path) == _folded_path(prefix)
            for prefix in excluded
        ):
            return False
        return _lean_path_is_excluded(path, refresh_exclusions())

    return TreeSelection(
        include=lambda path, mode: _lean_snapshot_includes(
            path,
            mode,
            excluded,
            is_excluded=is_excluded,
        ),
        descend=lambda path: not is_excluded(path),
        byte_limit=lambda path: (
            _PUBLICATION_MANIFEST_BYTE_LIMIT
            if _is_publication_manifest_name(path.name)
            else (
                _MANAGED_OUTPUT_MANIFEST_BYTE_LIMIT
                if _is_managed_output_manifest_name(path.name)
                else None
            )
        ),
        record_omitted=False,
    )


def _lean_snapshot_includes(
    relative: PurePosixPath,
    mode: int,
    excluded: tuple[PurePosixPath, ...],
    *,
    is_excluded: Callable[[PurePosixPath], bool] | None = None,
) -> bool:
    if (
        is_excluded(relative)
        if is_excluded is not None
        else _lean_path_is_excluded(relative, excluded)
    ):
        return False
    return (
        stat.S_ISDIR(mode)
        or stat.S_ISLNK(mode)
        or relative.suffix.casefold() == ".lean"
        or _is_publication_manifest_name(relative.name)
        or _is_managed_output_manifest_name(relative.name)
    )


def _lean_path_is_excluded(
    relative: PurePosixPath,
    excluded: tuple[PurePosixPath, ...],
) -> bool:
    folded_parts = _folded_path(relative)
    return (
        bool(_IGNORED_DIRECTORIES.intersection(folded_parts))
        or any(
            part.startswith(_IGNORED_DIRECTORY_PREFIXES) for part in folded_parts
        )
        or any(relative == prefix or relative.is_relative_to(prefix) for prefix in excluded)
    )


def _folded_path(path: PurePosixPath) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def _indexed_source_snapshot(
    root: Path,
    snapshot: TreeSnapshot,
    excluded: tuple[PurePosixPath, ...],
) -> IndexedSourceSnapshot:
    publication_manifests: dict[
        PurePosixPath,
        list[tuple[str, bytes | None]],
    ] = {}
    managed_output_manifests: dict[
        PurePosixPath,
        list[tuple[str, bytes | None]],
    ] = {}

    def add_manifest(relative: str, kind: str, data: bytes | None = None) -> None:
        path = PurePosixPath(relative)
        if _is_publication_manifest_name(path.name):
            publication_manifests.setdefault(path.parent, []).append((kind, data))
        if _is_managed_output_manifest_name(path.name):
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

    manifest_groups = (
        ("publication", publication_manifests, _is_publication_manifest_bytes),
        ("managed output", managed_output_manifests, _is_managed_output_manifest_bytes),
    )
    ignored_roots: set[PurePosixPath] = set()
    for parent in sorted(
        publication_manifests.keys() | managed_output_manifests.keys(),
        key=lambda path: (len(path.parts), path.as_posix()),
    ):
        if _path_is_within_roots(parent, ignored_roots):
            continue
        recognized = False
        for _label, groups, recognizes in manifest_groups:
            manifests = groups.get(parent)
            if (
                manifests is not None
                and len(manifests) == 1
                and manifests[0][0] == "file"
                and manifests[0][1] is not None
                and recognizes(manifests[0][1])
            ):
                recognized = True
                break
        if recognized:
            ignored_roots.add(parent)
            continue
        for label, groups, _recognizes in manifest_groups:
            manifests = groups.get(parent)
            if manifests is None:
                continue
            if len(manifests) != 1:
                raise OSError(f"ambiguous {label} manifests in {parent.as_posix()}")
            kind, data = manifests[0]
            if kind != "file" or data is None:
                raise OSError(
                    f"{label} manifest is not a regular file in {parent.as_posix()}"
                )

    def in_ignored_root(relative_text: str) -> bool:
        return _path_is_within_roots(PurePosixPath(relative_text), ignored_roots)

    unsupported = [
        (relative, reason)
        for relative, reason in snapshot.unsupported_entries()
        if not in_ignored_root(relative)
        and PurePosixPath(relative).suffix.casefold() == ".lean"
    ]
    if unsupported:
        relative, reason = unsupported[0]
        raise OSError(f"unsafe Lean source {relative}: {reason}")

    declarations: dict[str, Declaration] = {}
    line_counts: dict[Path, int] = {}
    digest = hashlib.sha256(b"autoform-lean-source-index/v1\0")
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
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            continue
        line_counts[relative_path] = len(text.splitlines())
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
        _lean_generation_revision(snapshot, ignored_roots),
    )


def _lean_generation_revision(
    snapshot: TreeSnapshot,
    ignored_roots: set[PurePosixPath],
) -> str:
    """Hash only effective Lean inputs and their ancestor directories."""

    def retained_entry(relative_text: str) -> bool:
        relative = PurePosixPath(relative_text)
        return relative.suffix.casefold() == ".lean" and not _path_is_within_roots(
            relative,
            ignored_roots,
        )

    files = tuple(entry for entry in snapshot.files if retained_entry(entry[0]))
    symlinks = tuple(entry for entry in snapshot.symlinks if retained_entry(entry[0]))
    special = tuple(entry for entry in snapshot.special if retained_entry(entry[0]))
    placeholders = tuple(path for path in snapshot.placeholders if retained_entry(path))
    omitted = tuple(entry for entry in snapshot.omitted if retained_entry(entry[0]))
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
    )
    return filtered.generation_revision


def _is_publication_manifest_bytes(data: bytes) -> bool:
    if len(data) > _PUBLICATION_MANIFEST_BYTE_LIMIT:
        raise OSError("publication manifest exceeds its inspection bound")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return False
    if not isinstance(value, dict):
        return False
    schema = value.get("schema")
    return isinstance(schema, str) and schema in _PUBLICATION_SCHEMAS


def _is_publication_manifest_name(name: str) -> bool:
    return unicodedata.normalize("NFC", name).casefold() == _PUBLICATION_MANIFEST


def _is_managed_output_manifest_bytes(data: bytes) -> bool:
    if len(data) > _MANAGED_OUTPUT_MANIFEST_BYTE_LIMIT:
        raise OSError("managed output manifest exceeds its inspection bound")
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


def _relative_exclusion(
    root: Path,
    value: str | Path,
    *,
    root_identity: tuple[int, int],
) -> PurePosixPath | None:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    cursor = directory_binding.lexical_absolute_path(candidate)
    tail: list[str] = []
    while True:
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            raise OSError("Lean exclusion path cannot be inspected safely") from error
        else:
            if stat.S_ISDIR(metadata.st_mode) and (
                metadata.st_dev,
                metadata.st_ino,
            ) == root_identity:
                break
        parent = cursor.parent
        if parent == cursor:
            return None
        tail.append(cursor.name)
        cursor = parent
    try:
        result = _canonical_exclusion_tail(root, tuple(reversed(tail)), root_identity)
    except (OSError, ValueError) as error:
        raise OSError("Lean exclusion path cannot be inspected safely") from error
    if any(part in {"", ".", ".."} for part in result.parts):
        return None
    return result


def _canonical_exclusion_tail(
    root: Path,
    parts: tuple[str, ...],
    root_identity: tuple[int, int],
) -> PurePosixPath:
    """Use physical names for existing exclusion components on aliasing filesystems."""

    if not parts:
        return PurePosixPath(".")
    if not (
        directory_binding.DIRECTORY_BINDING_SUPPORTED
        and _DESCRIPTOR_LISTING_SUPPORTED
    ):
        first = _canonical_exclusion_tail_portably(root, parts, root_identity)
        second = _canonical_exclusion_tail_portably(root, parts, root_identity)
        if first != second:
            raise OSError("Lean exclusion path changed while it was selected")
        return first[0]
    descriptors: list[int] = []
    descriptor: int | None = None
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
        descriptors.append(descriptor)
        opened_root = os.fstat(descriptor)
        if (opened_root.st_dev, opened_root.st_ino) != root_identity:
            raise OSError("Lean source root changed while exclusions were selected")
        selected: list[str] = []
        for index, requested in enumerate(parts):
            try:
                requested_metadata = os.stat(
                    requested,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except (FileNotFoundError, NotADirectoryError):
                selected.extend(parts[index:])
                break
            signature = (
                requested_metadata.st_dev,
                requested_metadata.st_ino,
                requested_metadata.st_mode,
            )
            names = tuple(sorted(os.listdir(descriptor)))
            folded = unicodedata.normalize("NFC", requested).casefold()
            matches = []
            for name in names:
                if unicodedata.normalize("NFC", name).casefold() != folded:
                    continue
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino, metadata.st_mode) == signature:
                    matches.append(name)
            if len(matches) != 1:
                raise OSError("Lean exclusion path has no stable directory entry")
            actual = matches[0]
            selected.append(actual)
            if tuple(sorted(os.listdir(descriptor))) != names:
                raise OSError("Lean exclusion path changed while it was selected")
            current = os.stat(actual, dir_fd=descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_mode) != signature:
                raise OSError("Lean exclusion path changed while it was selected")
            if index == len(parts) - 1:
                continue
            if not stat.S_ISDIR(current.st_mode):
                selected.extend(parts[index + 1 :])
                break
            child = os.open(actual, _DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.append(child)
            child_metadata = os.fstat(child)
            if (
                child_metadata.st_dev,
                child_metadata.st_ino,
                child_metadata.st_mode,
            ) != signature:
                raise OSError("Lean exclusion path changed while it was selected")
            descriptor = child
        return PurePosixPath(*selected)
    finally:
        for opened in reversed(descriptors):
            try:
                os.close(opened)
            except OSError:
                pass


def _canonical_exclusion_tail_portably(
    root: Path,
    parts: tuple[str, ...],
    root_identity: tuple[int, int],
) -> tuple[PurePosixPath, tuple[tuple[str, tuple[int, int, int]], ...]]:
    root_metadata = os.lstat(root)
    if (root_metadata.st_dev, root_metadata.st_ino) != root_identity:
        raise OSError("Lean source root changed while exclusions were selected")
    current = root
    selected: list[str] = []
    observed: list[tuple[str, tuple[int, int, int]]] = []
    for index, requested in enumerate(parts):
        requested_path = current / requested
        try:
            requested_metadata = os.lstat(requested_path)
        except (FileNotFoundError, NotADirectoryError):
            selected.extend(parts[index:])
            break
        signature = (
            requested_metadata.st_dev,
            requested_metadata.st_ino,
            requested_metadata.st_mode,
        )
        names = tuple(sorted(entry.name for entry in os.scandir(current)))
        folded = unicodedata.normalize("NFC", requested).casefold()
        matches = []
        for name in names:
            if unicodedata.normalize("NFC", name).casefold() != folded:
                continue
            metadata = os.lstat(current / name)
            if (metadata.st_dev, metadata.st_ino, metadata.st_mode) == signature:
                matches.append(name)
        if len(matches) != 1:
            raise OSError("Lean exclusion path has no stable directory entry")
        actual = matches[0]
        selected.append(actual)
        actual_path = current / actual
        final = os.lstat(actual_path)
        if (
            tuple(sorted(entry.name for entry in os.scandir(current))) != names
            or (final.st_dev, final.st_ino, final.st_mode) != signature
        ):
            raise OSError("Lean exclusion path changed while it was selected")
        observed.append(("/".join(selected), signature))
        if index == len(parts) - 1:
            continue
        if not stat.S_ISDIR(final.st_mode) or _is_reparse_point(final):
            selected.extend(parts[index + 1 :])
            break
        current = actual_path
    final_root = os.lstat(root)
    if (final_root.st_dev, final_root.st_ino) != root_identity:
        raise OSError("Lean source root changed while exclusions were selected")
    return PurePosixPath(*selected), tuple(observed)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


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

    def location(self, name: str) -> Declaration | None:
        return self.index.find(name)

    def url(self, name: str) -> str | None:
        """Return a permanent link to *name*, or ``None`` if it cannot be built."""
        declaration = self.index.find(name)
        if declaration is None or not self.repository_url or not self.ref:
            return None
        path = declaration.path.as_posix()
        return f"{self.repository_url}/blob/{self.ref}/{path}#L{declaration.line}"


def build_linker(
    lean_root: str | Path,
    *,
    repository_url: str | None = None,
    ref: str | None = None,
    exclude_roots: Iterable[str | Path] = (),
    source_index: SourceIndex | None = None,
    detect_missing: bool = True,
) -> SourceLinker:
    """Index *lean_root* and resolve repository coordinates.

    A supplied source snapshot never inherits a live Git ref; callers must bind
    that ref explicitly if they want permalinks.
    """
    root = Path(lean_root).expanduser().resolve()
    if source_index is not None and source_index.root != root:
        raise ValueError("captured source index belongs to a different Lean root")
    resolved_repository_url = repository_url
    resolved_ref = ref
    if detect_missing:
        resolved_repository_url = repository_url or detect_repository_url(root)
        if source_index is None:
            resolved_ref = ref or detect_ref(root)
    return SourceLinker(
        index=(
            source_index
            if source_index is not None
            else index_project(root, exclude_roots=exclude_roots)
        ),
        repository_url=resolved_repository_url,
        ref=resolved_ref,
    )


def detect_repository_url(root: str | Path) -> str | None:
    """Find the project's web URL from the CI environment or the git remote."""
    repository = os.environ.get("GITHUB_REPOSITORY")
    if repository:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server.rstrip('/')}/{repository}"
    remote = _git(root, "config", "--get", "remote.origin.url")
    return _normalize_remote(remote) if remote else None


def detect_ref(root: str | Path) -> str | None:
    """Prefer the exact commit so links keep pointing at the reviewed code."""
    return os.environ.get("GITHUB_SHA") or _git(root, "rev-parse", "HEAD")


def _normalize_remote(remote: str) -> str | None:
    remote = remote.strip()
    if remote.startswith("git@"):
        host, _, path = remote[4:].partition(":")
        if not path:
            return None
        remote = f"https://{host}/{path}"
    elif remote.startswith("ssh://git@"):
        remote = "https://" + remote[len("ssh://git@") :]
    if not remote.startswith(("http://", "https://")):
        return None
    return remote[: -len(".git")] if remote.endswith(".git") else remote.rstrip("/")


def _git(root: str | Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


__all__ = [
    "IndexedSourceSnapshot",
    "Declaration",
    "MANAGED_OUTPUT_SCHEMAS",
    "PACKET_SCHEMA",
    "PASSAGE_SCHEMA",
    "SourceIndex",
    "SourceLinker",
    "build_linker",
    "declaration_names",
    "detect_ref",
    "detect_repository_url",
    "index_project",
    "project_source_revision",
    "snapshot_project_sources",
    "strip_lean_comments",
]
