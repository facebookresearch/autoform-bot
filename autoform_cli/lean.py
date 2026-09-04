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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import workspace as workspace_module
from ._tree_snapshot import (
    BoundDirectoryTree,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
)

_LINE_COMMENT = re.compile(r"--.*$")
_NAMESPACE = re.compile(r"^\s*namespace\s+(\S+)")
_SECTION = re.compile(r"^\s*section\b\s*(\S*)")
_END = re.compile(r"^\s*end\b\s*(\S*)")
_DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
    r"(theorem|lemma|def|abbrev|instance|structure|class|inductive|opaque|axiom)\s+"
    r"([^\s:(){}\[\]⦃⦄,]+)"
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
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

# Lean erases the source-level distinction between theorem, lemma, corollary,
# and proposition. Keep that normalization in one place so the lexical audit
# and the kernel-backed CI probe enforce the same authored intent.
DECLARATION_KIND_ALIASES = {
    "abbrev": "abbrev",
    "axiom": "axiom",
    "class": "class",
    "corollary": "theorem",
    "def": "def",
    "definition": "def",
    "inductive": "inductive",
    "instance": "instance",
    "lemma": "theorem",
    "opaque": "opaque",
    "proposition": "theorem",
    "structure": "structure",
    "theorem": "theorem",
}

_DECLARATION_KEYWORDS = {
    "abbrev": frozenset({"abbrev"}),
    "axiom": frozenset({"axiom"}),
    "class": frozenset({"class"}),
    "def": frozenset({"def"}),
    "inductive": frozenset({"inductive"}),
    "instance": frozenset({"instance"}),
    "opaque": frozenset({"opaque"}),
    "structure": frozenset({"structure"}),
    "theorem": frozenset({"lemma", "theorem"}),
}


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
    excluded: tuple[PurePosixPath, ...]

    def capture(self) -> IndexedSourceSnapshot:
        snapshot = self.tree.capture()
        return _indexed_source_snapshot(self.root, snapshot, self.excluded)

    def verify(self) -> None:
        self.tree.verify()

    def close(self) -> None:
        self.tree.close()


def index_project(
    root: str | Path, *, exclude_roots: Iterable[str | Path] = ()
) -> SourceIndex:
    """Scan ``*.lean`` beneath *root* and index declarations by full name."""
    return snapshot_project_sources(root, exclude_roots=exclude_roots).index


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

    root_path = Path(os.path.abspath(Path(root).expanduser()))
    try:
        root_metadata = root_path.stat(follow_symlinks=False)
    except OSError as error:
        raise OSError("Lean source root cannot be inspected safely") from error
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise OSError("Lean source root is not a directory")
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    excluded = tuple(
        candidate
        for value in exclude_roots
        if (
            candidate := _relative_exclusion(
                root_path,
                value,
                root_identity=root_identity,
            )
        )
        is not None
    )
    selection = TreeSelection(
        include=lambda path, mode: _lean_snapshot_includes(path, mode, excluded),
        descend=lambda path: not _lean_path_is_excluded(path, excluded),
        byte_limit=lambda path: (
            _PUBLICATION_MANIFEST_BYTE_LIMIT
            if _is_publication_manifest_name(path.name)
            else None
        ),
        record_omitted=False,
    )
    try:
        tree = BoundDirectoryTree(
            root_path,
            expected_identity=root_identity,
            selection=selection,
        )
    except TreeSnapshotError as error:
        raise OSError(str(error)) from error
    return BoundProjectSources(root_path, tree, excluded)


def _lean_snapshot_includes(
    relative: PurePosixPath,
    mode: int,
    excluded: tuple[PurePosixPath, ...],
) -> bool:
    if _lean_path_is_excluded(relative, excluded):
        return False
    return (
        stat.S_ISDIR(mode)
        or stat.S_ISLNK(mode)
        or relative.suffix.casefold() == ".lean"
        or _is_publication_manifest_name(relative.name)
    )


def _lean_path_is_excluded(
    relative: PurePosixPath,
    excluded: tuple[PurePosixPath, ...],
) -> bool:
    return (
        bool(_IGNORED_DIRECTORIES.intersection(relative.parts))
        or any(part.startswith(_IGNORED_DIRECTORY_PREFIXES) for part in relative.parts)
        or any(relative == prefix or relative.is_relative_to(prefix) for prefix in excluded)
    )


def _indexed_source_snapshot(
    root: Path,
    snapshot: TreeSnapshot,
    excluded: tuple[PurePosixPath, ...],
) -> IndexedSourceSnapshot:
    publication_manifests: dict[
        PurePosixPath,
        list[tuple[str, bytes | None]],
    ] = {}

    def add_manifest(relative: str, kind: str, data: bytes | None = None) -> None:
        path = PurePosixPath(relative)
        if _is_publication_manifest_name(path.name):
            publication_manifests.setdefault(path.parent, []).append((kind, data))

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

    publication_roots: set[PurePosixPath] = set()
    for parent, manifests in sorted(
        publication_manifests.items(),
        key=lambda item: (len(item[0].parts), item[0].as_posix()),
    ):
        if any(parent == root or parent.is_relative_to(root) for root in publication_roots):
            continue
        if len(manifests) != 1:
            raise OSError(f"ambiguous publication manifests in {parent.as_posix()}")
        kind, data = manifests[0]
        if kind != "file" or data is None:
            raise OSError(
                f"publication manifest is not a regular file in {parent.as_posix()}"
            )
        if _is_publication_manifest_bytes(data):
            publication_roots.add(parent)

    def in_publication(relative_text: str) -> bool:
        relative = PurePosixPath(relative_text)
        return any(
            relative == publication or relative.is_relative_to(publication)
            for publication in publication_roots
        )

    unsupported = [
        (relative, reason)
        for relative, reason in snapshot.unsupported_entries()
        if not in_publication(relative)
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
        if any(
            relative == publication or relative.is_relative_to(publication)
            for publication in publication_roots
        ):
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
    return IndexedSourceSnapshot(
        SourceIndex(root=root, declarations=declarations, line_counts=line_counts),
        digest.hexdigest(),
        _lean_generation_revision(snapshot, publication_roots),
    )


def _lean_generation_revision(
    snapshot: TreeSnapshot,
    publication_roots: set[PurePosixPath],
) -> str:
    """Hash only effective Lean inputs and their ancestor directories."""

    def retained_entry(relative_text: str) -> bool:
        relative = PurePosixPath(relative_text)
        return relative.suffix.casefold() == ".lean" and not any(
            relative == publication or relative.is_relative_to(publication)
            for publication in publication_roots
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
        return False
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("schema") in _PUBLICATION_SCHEMAS


def _is_publication_manifest_name(name: str) -> bool:
    return unicodedata.normalize("NFC", name).casefold() == _PUBLICATION_MANIFEST


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
    cursor = Path(os.path.abspath(candidate))
    tail: list[str] = []
    while True:
        try:
            metadata = cursor.stat(follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as error:
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
    result = _canonical_exclusion_tail(root, tuple(reversed(tail)), root_identity)
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
    if not workspace_module._DIRECTORY_BINDING_SUPPORTED:
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
            child_metadata = os.fstat(child)
            if (
                child_metadata.st_dev,
                child_metadata.st_ino,
                child_metadata.st_mode,
            ) != signature:
                os.close(child)
                raise OSError("Lean exclusion path changed while it was selected")
            descriptors.append(child)
            descriptor = child
        return PurePosixPath(*selected)
    finally:
        for opened in reversed(descriptors):
            os.close(opened)


def _canonical_exclusion_tail_portably(
    root: Path,
    parts: tuple[str, ...],
    root_identity: tuple[int, int],
) -> tuple[PurePosixPath, tuple[tuple[str, tuple[int, int, int]], ...]]:
    root_metadata = root.stat(follow_symlinks=False)
    if (root_metadata.st_dev, root_metadata.st_ino) != root_identity:
        raise OSError("Lean source root changed while exclusions were selected")
    current = root
    selected: list[str] = []
    observed: list[tuple[str, tuple[int, int, int]]] = []
    for index, requested in enumerate(parts):
        requested_path = current / requested
        try:
            requested_metadata = requested_path.stat(follow_symlinks=False)
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
            metadata = (current / name).stat(follow_symlinks=False)
            if (metadata.st_dev, metadata.st_ino, metadata.st_mode) == signature:
                matches.append(name)
        if len(matches) != 1:
            raise OSError("Lean exclusion path has no stable directory entry")
        actual = matches[0]
        selected.append(actual)
        actual_path = current / actual
        final = actual_path.stat(follow_symlinks=False)
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
    final_root = root.stat(follow_symlinks=False)
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
    comment_depth = 0

    for number, raw in enumerate(text.splitlines(), start=1):
        line, comment_depth = _strip_comments(raw, comment_depth)
        if not line.strip():
            continue

        namespace_match = _NAMESPACE.match(line)
        if namespace_match:
            name = namespace_match.group(1)
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
            keyword, name = declaration_match.group(1), declaration_match.group(2)
            qualified = ".".join([*namespaces, name])
            found.append(Declaration(qualified, relative, number, keyword))
    return found


def _strip_comments(line: str, depth: int) -> tuple[str, int]:
    """Remove Lean comments from *line*, carrying block-comment depth across."""
    out: list[str] = []
    index = 0
    while index < len(line):
        pair = line[index : index + 2]
        if depth:
            if pair == "-/":
                depth -= 1
                index += 2
                continue
            if pair == "/-":
                depth += 1
                index += 2
                continue
            index += 1
            continue
        if pair == "/-":
            depth += 1
            index += 2
            continue
        out.append(line[index])
        index += 1
    return _LINE_COMMENT.sub("", "".join(out)), depth


def declaration_names(lean: str) -> list[str]:
    """Split a ``lean:`` frontmatter value into individual declaration names."""
    return [name.strip() for name in lean.replace(",", " ").split() if name.strip()]


def declaration_kind(intent: str | None) -> str | None:
    """Return the kernel-checkable kind represented by authored intent."""

    if intent is None:
        return None
    return DECLARATION_KIND_ALIASES.get(intent.strip().casefold())


def declaration_keywords(intent: str | None) -> frozenset[str] | None:
    """Return source keywords accepted for authored declaration intent."""

    kind = declaration_kind(intent)
    return _DECLARATION_KEYWORDS.get(kind) if kind is not None else None


def mathlib_module_name(source_file: str) -> str | None:
    """Map a canonical ``Mathlib/**/*.lean`` source path to its module name."""

    if not source_file or "\\" in source_file:
        return None
    path = PurePosixPath(source_file)
    if path.is_absolute() or path.as_posix() != source_file:
        return None
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    if parts[0] != "Mathlib" and parts[0] != "Mathlib.lean":
        return None
    if not parts[-1].endswith(".lean") or parts[-1] == ".lean":
        return None
    module_parts = [*parts[:-1], parts[-1][: -len(".lean")]]
    if not module_parts or module_parts[0] != "Mathlib":
        return None
    for part in module_parts:
        if not part or not (part[0].isalpha() or part[0] == "_"):
            return None
        if any(not (character.isalnum() or character in "_'") for character in part):
            return None
    return ".".join(module_parts)


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
    """Index *lean_root* and resolve the repository coordinates to link against."""
    resolved_repository_url = repository_url
    resolved_ref = ref
    if detect_missing:
        resolved_repository_url = repository_url or detect_repository_url(lean_root)
        resolved_ref = ref or detect_ref(lean_root)
    return SourceLinker(
        index=(
            source_index
            if source_index is not None
            else index_project(lean_root, exclude_roots=exclude_roots)
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
    "DECLARATION_KIND_ALIASES",
    "IndexedSourceSnapshot",
    "Declaration",
    "SourceIndex",
    "SourceLinker",
    "build_linker",
    "declaration_kind",
    "declaration_keywords",
    "declaration_names",
    "detect_ref",
    "detect_repository_url",
    "index_project",
    "mathlib_module_name",
    "project_source_revision",
    "snapshot_project_sources",
]
