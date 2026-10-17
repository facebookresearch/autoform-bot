"""Resolve blueprint ``lean:`` declarations to their source location.

Scanning the project's own Lean files keeps two promises at once: a proved node
can link to the line that proves it, and a ``lean:`` name that resolves to
nothing is a validation error rather than a broken link -- the job
``leanblueprint checkdecls`` does for LaTeX blueprints.

The scanner is a lexical pass, not an elaborator. It tracks ``namespace`` and
comment nesting and stops at ``#exit``, which is enough for declarations
written in the ordinary way, and deliberately reports nothing it cannot see
rather than guessing.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import time
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
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
#: ``section`` in every form that opens one, e.g. ``@[expose] public section``.
_SECTION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*(?:(?:public|private|meta|noncomputable)\s+)*section\b\s*(\S*)"
)
_MUTUAL = re.compile(r"^\s*mutual\s*$")
_END = re.compile(r"^\s*end\b\s*(\S*)")
_EXIT = re.compile(r"^\s*#exit\b")
# The ``*_wanted`` commands are Batteries' ``Batteries.Util.ProofWanted``: each
# states a result whose proof or construction is deferred. ``alias`` is
# Batteries' ``Batteries.Tactic.Alias``.
_DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:public|private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
    r"(theorem|lemma|def|abbrev|instance|structure|class|inductive|opaque|axiom|irreducible_def"
    r"|theorem_wanted|proof_wanted|def_wanted|instance_wanted|alias)\s+(.+)$"
)
#: The wanted commands. Batteries makes each a private placeholder, so none of
#: them binds its name outside its own module.
_WANTED_KEYWORDS = frozenset({"theorem_wanted", "proof_wanted", "def_wanted", "instance_wanted"})
_IGNORED_DIRECTORIES = frozenset(
    {
        ".direnv",
        ".git",
        ".lake",
        ".obsidian",
        ".trash",
        ".venv",
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
#: Render marks each finished site with this manifest. ``mkdocs`` copies it
#: into its own output, so both folders hold rendered copies, never sources.
PUBLICATION_MANIFEST = "publication.json"
PUBLICATION_SCHEMA = "autoform-publication/v2"
#: Render never writes a larger manifest, so the next render can always read it.
PUBLICATION_MANIFEST_MAX_BYTES = 8 * 1024 * 1024

@dataclass(frozen=True, slots=True)
class Alias:
    """What an ``alias`` command points at, as its source writes it."""

    #: The names its target can mean, in the order Lean tries them: in the
    #: current namespace, in each enclosing one, then at the root. Empty when
    #: the target is on neither the alias's line nor the next nonblank one.
    candidates: tuple[str, ...]
    #: Whether the target is one component, such as ``t`` rather than ``A.t``:
    #: Lean then skips a ``protected`` declaration in the namespaces it tries.
    atomic: bool
    #: ``alias ⟨mp, mpr⟩ := t``: each name is a theorem, whatever ``t`` is.
    iff: bool


@dataclass(frozen=True, slots=True)
class Declaration:
    """One Lean declaration found in the project's sources."""

    name: str
    path: Path
    line: int
    keyword: str
    #: Whether ``protected`` is written before the keyword, on its line or above.
    protected: bool = False
    #: Whether ``private`` is written before the keyword, on its line or above.
    private: bool = False
    alias: Alias | None = None


@dataclass(frozen=True, slots=True)
class SourceIndex:
    """The scanner's declarations, by fully qualified name.

    The one each name selects is in ``declarations``, and every occurrence of a
    repeated name in ``duplicates``.
    """

    root: Path
    #: The declaration each name selects: its only one, else its only one that
    #: binds the name outside its module (not private, not wanted). A name
    #: declared more than once without such a unique one selects nothing.
    declarations: dict[str, Declaration]
    source_digest: str
    line_counts: dict[Path, int] = field(default_factory=dict)
    #: Every declaration of each name declared more than once, by path and line.
    duplicates: dict[str, tuple[Declaration, ...]] = field(default_factory=dict)

    def find(self, name: str) -> Declaration | None:
        return self.declarations.get(name)

    def occurrences(self, name: str) -> tuple[Declaration, ...]:
        """Every declaration of ``name``, by path and line."""

        found = self.declarations.get(name)
        return self.duplicates.get(name, () if found is None else (found,))

    def ambiguity(self, name: str) -> str:
        """The ``path:line`` of each declaration of ``name`` when it selects none of them, else ``""``."""

        found = () if name in self.declarations else self.duplicates.get(name, ())
        return ", ".join(f"{item.path.as_posix()}:{item.line}" for item in found)

    def reached(self, declaration: Declaration) -> Declaration | None:
        """The declaration ``declaration`` stands for: itself, or what an alias names through any chain.

        ``None`` when the index cannot be sure what an alias's target is. Each
        step goes to an earlier line of the same file, so a chain always ends.
        """

        while declaration.alias is not None:
            target = self.alias_target(declaration)
            if target is None:
                return None
            declaration = target
        return declaration

    def alias_target(self, declaration: Declaration) -> Declaration | None:
        """The declaration an alias's target names, when the index can be sure which.

        Lean takes the first candidate it can see, so a candidate the index
        lacks may be declared outside the project, and one in another file may
        or may not be imported: either way the answer is ``None``. A candidate
        declared above the alias in its file is the one Lean sees, whatever
        other files declare, since importing one of them would be a clash. A
        candidate only below the alias in its file is not declared yet, so Lean
        looks further out. A ``protected`` candidate is skipped for a
        one-component target, except when it is also private, written or under
        the module system; not knowing which, the answer is ``None`` again.
        """

        assert declaration.alias is not None
        for candidate in declaration.alias.candidates:
            found = self.occurrences(candidate)
            above = [other for other in found if other.path == declaration.path and other.line < declaration.line]
            if above:
                return None if above[0].protected and declaration.alias.atomic else above[0]
            if not found or any(other.path != declaration.path for other in found):
                return None
        return None


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

    def capture(self) -> IndexedSourceSnapshot:
        snapshot = self.tree.capture()
        return _indexed_source_snapshot(self.root, snapshot)

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


def resolve_lean_root(lean_root: str | Path) -> Path:
    """Resolve *lean_root* to the directory it names.

    Raises :class:`LeanSourceError` naming why the root cannot be used, for
    example when it does not exist or lies under a folder that cannot be
    searched.
    """

    try:
        root = directory_binding.lexical_absolute_path(lean_root).resolve()
        metadata = os.stat(root)
    except RuntimeError as error:
        # An unknown ~user, or before Python 3.13 a symbolic link loop.
        raise LeanSourceError("Lean root cannot be resolved") from error
    except (FileNotFoundError, NotADirectoryError) as error:
        raise LeanSourceError("Lean root does not exist or is not a directory") from error
    except PermissionError as error:
        raise LeanSourceError("permission denied while resolving the Lean root") from error
    except (OSError, ValueError) as error:
        if getattr(error, "errno", None) == errno.ELOOP:
            # Python 3.13 and later resolve a symbolic link loop without raising.
            raise LeanSourceError("Lean root cannot be resolved") from error
        raise LeanSourceError("Lean root cannot be inspected") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise LeanSourceError("Lean root does not exist or is not a directory")
    return root


def index_project(
    root: str | Path, *, exclude_roots: Iterable[str | Path] = ()
) -> SourceIndex:
    """Scan ``*.lean`` beneath *root* and index declarations by full name.

    Raises :class:`LeanSourceError` when the root cannot be used or its
    sources cannot be read.
    """
    root_path = resolve_lean_root(root)
    requested_root = directory_binding.lexical_absolute_path(root)
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
) -> IndexedSourceSnapshot:
    """Read each Lean source once and derive its index and revision together.

    A bind or capture that races a concurrent edit is retried a bounded number
    of times.
    """

    bound, snapshot = _open_stable_project_sources(
        root,
        exclude_roots=tuple(exclude_roots),
        limits=limits,
    )
    bound.close()
    return snapshot


def _open_stable_project_sources(
    root: str | Path,
    *,
    exclude_roots: tuple[str | Path, ...],
    limits: TreeCaptureLimits,
) -> tuple[BoundProjectSources, IndexedSourceSnapshot]:
    """Open and capture a Lean root, retrying a bounded number of changed attempts.

    The caller owns the returned binding.  When every attempt changed, the error
    names each changed root-relative path the attempts reported, once, in order.
    """

    changed: TreeChangedError | None = None
    changed_paths: list[str] = []
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        if attempt:
            time.sleep(_SNAPSHOT_RETRY_DELAY_SECONDS * attempt)
        bound: BoundProjectSources | None = None
        try:
            bound = open_project_sources(
                root,
                exclude_roots=exclude_roots,
                limits=limits,
            )
            return bound, bound.capture()
        except BaseException as error:
            if bound is not None:
                bound.close()
            if isinstance(error, TreeChangedError):
                changed = error
                if error.relative is not None and error.relative not in changed_paths:
                    changed_paths.append(error.relative)
                continue
            if isinstance(error, TreeSnapshotError):
                raise LeanSourceError(str(error)) from error
            raise
    lasting = _lasting_root_failure(root)
    if lasting is not None:
        raise LeanSourceError(lasting) from changed
    message = "Lean sources kept changing while they were indexed"
    if changed_paths:
        message += "; changed: " + ", ".join(changed_paths)
    raise LeanSourceError(message) from changed


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
    return BoundProjectSources(root_path, tree)


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

    bound, snapshot = _open_stable_project_sources(
        root,
        exclude_roots=tuple(exclude_roots),
        limits=limits,
    )
    try:
        yield bound, snapshot
    finally:
        bound.close()


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
        record_omitted=False,
        limits=limits,
        # Only Lean inputs and the folders holding them must stay stable.
        unselected_may_change=True,
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
            # A rendered site, and mkdocs' copy of one, holds copies of the
            # blueprint's files. Render marks its stage before copying into it;
            # mkdocs can copy files into site/ before it copies the manifest.
            OpaqueDirectoryMarker(
                PUBLICATION_MANIFEST,
                PUBLICATION_MANIFEST_MAX_BYTES,
                _is_complete_publication,
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
    # The suffix is compared exactly, as Lake's module globs, skeleton and the
    # servers do, so ``A.LEAN`` is not a Lean source even on a case-insensitive
    # volume where ``import A`` could open it.
    if stat.S_ISLNK(mode):
        # Editor lock links (``.#Name.lean``) are never read, so they are skipped;
        # any other ``.lean`` link, live or dangling, is captured and refused.
        return (
            relative.suffix == ".lean"
            and not relative.name.startswith(".#")
        )
    return stat.S_ISDIR(mode) or relative.suffix == ".lean"


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


def _indexed_source_snapshot(root: Path, snapshot: TreeSnapshot) -> IndexedSourceSnapshot:
    # The Lean selection captures only .lean entries outside excluded folders,
    # so every captured entry is a source and the snapshot's own revisions
    # cover exactly the Lean inputs and the folders holding them.
    unsupported = snapshot.unsupported_entries()
    if unsupported:
        relative, reason = unsupported[0]
        raise LeanSourceError(f"unsafe Lean source {relative}: {reason}")

    by_name: dict[str, list[Declaration]] = {}
    line_counts: dict[Path, int] = {}
    source_files: list[tuple[Path, bytes]] = []
    digest = hashlib.sha256(b"autoform-lean-source-index/v1\0")
    for relative_text, data in snapshot.files:
        relative_path = Path(relative_text)
        _update_source_digest(digest, relative_path, data)
        source_files.append((relative_path, data))
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            continue
        found, line_counts[relative_path] = _scan(text, relative_path)
        for declaration in found:
            by_name.setdefault(declaration.name, []).append(declaration)
    source_digest = digest.hexdigest()
    return IndexedSourceSnapshot(
        SourceIndex(
            root=root,
            declarations={
                name: selected for name, same in by_name.items() if (selected := _select(same)) is not None
            },
            source_digest=source_digest,
            line_counts=line_counts,
            duplicates={name: tuple(same) for name, same in by_name.items() if len(same) > 1},
        ),
        source_digest,
        snapshot.generation_revision,
        tuple(source_files),
    )


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


def _is_complete_publication(data: bytes) -> bool:
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        return False
    return (
        isinstance(value, dict)
        and value.get("schema") in {"autoform-publication/v1", PUBLICATION_SCHEMA}
        and value.get("complete") is True
    )


def _update_source_digest(digest, relative: Path, data: bytes) -> None:
    encoded = os.fsencode(relative.as_posix())
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def _select(same: list[Declaration]) -> Declaration | None:
    """The declaration a name means among all of its declarations, if one is sure.

    A private declaration, including the placeholder a wanted command makes,
    is local to its file, so beside them every other module sees the one
    public declaration. Two public ones, or only private and wanted ones,
    leave no single answer.
    """

    public = [item for item in same if not item.private and item.keyword not in _WANTED_KEYWORDS] or same
    return public[0] if len(public) == 1 else None


def _scan(text: str, relative: Path) -> tuple[list[Declaration], int]:
    """The declarations in ``text`` and the number of its lines Lean reads."""
    found: list[Declaration] = []
    namespaces: list[str] = []
    scopes: list[str | None] = []

    lines = _without_lean_comments(text).splitlines()
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if _EXIT.match(line):
            return found, number - 1

        namespace_match = _NAMESPACE.match(line)
        if namespace_match:
            name = _name_token(namespace_match.group(1))
            if name is None:
                continue
            namespaces.append(name)
            scopes.append(name)
            continue

        if _SECTION.match(line) or _MUTUAL.match(line):
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
            # Modifiers may also stand on the lines above the keyword.
            header = [line[: declaration_match.start(1)]]
            for above in range(number - 2, -1, -1):
                if not _MODIFIER_LINE.fullmatch(lines[above]):
                    break
                header.append(lines[above])
            modifiers = _ATTRIBUTE_LIST.sub(" ", " ".join(header)).split()
            protected = "protected" in modifiers
            private = "private" in modifiers
            if keyword == "alias":
                following = next((lines[later] for later in range(number, len(lines)) if lines[later].strip()), "")
                found.extend(
                    _aliases(declaration_match.group(2), following, namespaces, relative, number, protected, private)
                )
                continue
            name = _name_token(declaration_match.group(2))
            if name is None:
                continue
            found.append(Declaration(_qualified(namespaces, name), relative, number, keyword, protected, private))
    return found, len(lines)


_ATTRIBUTE_LIST = re.compile(r"@\[[^\]]*\]")
#: A line holding only attribute lists and modifiers, or nothing.
_MODIFIER_LINE = re.compile(
    r"\s*(?:(?:@\[[^\]]*\]|public|private|protected|noncomputable|partial|unsafe|scoped|local)\s*)*"
)


def _qualified(namespaces: list[str], name: str) -> str:
    """The full name of ``name`` declared in ``namespaces``; ``_root_.`` leaves them out."""

    return name.removeprefix("_root_.") if name.startswith("_root_.") else ".".join([*namespaces, name])


_NAME_TOKEN = re.compile(r"(?:«[^«»]*»|[^\s:(){}\[\]⦃⦄,«»])+")


def _name_token(text: str) -> str | None:
    """Read one possibly guillemet-quoted Lean identifier from ``text``."""

    match = _NAME_TOKEN.match(text)
    return match.group() if match and text[match.end() : match.end() + 1] not in ("«", "»") else None


#: A name as ``alias`` takes one: it starts with a letter, ``_`` or ``«``.
_ALIAS_NAME = r"(?:«[^«»]*»|[^\W\d])(?:«[^«»]*»|[^\s:(){}\[\]⦃⦄,«»⟨⟩])*"
#: What follows ``alias``: one name, or ``⟨mp, mpr⟩`` naming the two directions
#: of an iff, either of which may be ``_``; then ``:=`` and, unless it is on the
#: next line, the target.
_ALIAS = re.compile(
    rf"(?:⟨\s*(?P<mp>{_ALIAS_NAME})\s*,\s*(?P<mpr>{_ALIAS_NAME})\s*⟩|(?P<name>{_ALIAS_NAME}))\s*:="
    rf"(?:\s*(?P<target>{_ALIAS_NAME}))?"
)
_ALIAS_TARGET = re.compile(rf"\s*({_ALIAS_NAME})")


def _aliases(
    text: str, following: str, namespaces: list[str], relative: Path, line: int, protected: bool, private: bool
) -> list[Declaration]:
    """The declarations one ``alias`` command makes.

    ``text`` is what follows the keyword, and ``following`` the next nonblank
    line, where a long alias puts its target.
    """

    match = _ALIAS.match(text)
    if match is None:
        return []
    target = match["target"]
    if target is None and not text[match.end() :].strip():
        next_line = _ALIAS_TARGET.match(following)
        target = next_line.group(1) if next_line else None
    if target is None:
        candidates: tuple[str, ...] = ()
    elif target.startswith("_root_."):
        candidates = (target.removeprefix("_root_."),)
    else:
        parts = [part for namespace in namespaces for part in namespace.split(".")]
        candidates = tuple(".".join([*parts[:depth], target]) for depth in range(len(parts), -1, -1))
    atomic = target is not None and _ONE_COMPONENT.fullmatch(target) is not None
    iff = match["name"] is None
    alias = Alias(candidates, atomic, iff)
    return [
        Declaration(_qualified(namespaces, name), relative, line, "alias", protected, private, alias)
        for name in ([match["mp"], match["mpr"]] if iff else [match["name"]])
        if name != "_"
    ]


_ONE_COMPONENT = re.compile(r"(?:«[^«»]*»|[^.«»])+")


_RAW_OPEN = re.compile(r'r(#*)"')
_CHAR_LITERAL = re.compile(r"'(?:\\.|[^'\\\n])*'")
# Where a comment, literal, or interpolation brace can start. The text between
# two of these positions is copied as one slice, not character by character.
_CODE_EVENT = re.compile(r"""--|/-|r#*"|["«']""")
_INTERPOLATION_EVENT = re.compile(r"""--|/-|r#*"|["«'{}]""")
_COMMENT_EVENT = re.compile(r"-/|/-")
_LITERAL_EVENTS: dict[tuple[str, bool, bool], re.Pattern[str]] = {}


def _literal_event(close: str, interpolated: bool) -> re.Pattern[str]:
    """Where a literal ended by *close* can next close, escape, or interpolate."""

    key = (close[0], close in {'"', "'"}, interpolated)
    pattern = _LITERAL_EVENTS.get(key)
    if pattern is None:
        chars = close[0] + ("\\" if key[1] else "") + ("{" if interpolated else "")
        pattern = _LITERAL_EVENTS[key] = re.compile(f"[{re.escape(chars)}]")
    return pattern


def _without_lean_comments(text: str) -> str:
    """Remove nested Lean comments, keeping strings intact and newlines in place."""

    out: list[str] = []
    stack: list[list] = []  # [closer, interpolated] for a string, [None, depth] for `{…}` in `s!"…"`
    index = 0
    while index < len(text):
        top = stack[-1] if stack else None
        if top is not None and top[0] is not None:
            close, interpolated = top
            event = _literal_event(close, interpolated).search(text, index)
            if event is None:
                out.append(text[index:])
                break
            if event.start() > index:
                out.append(text[index : event.start()])
                index = event.start()
            char = text[index]
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
        event = (_CODE_EVENT if top is None else _INTERPOLATION_EVENT).search(text, index)
        if event is None:
            out.append(text[index:])
            break
        if event.start() > index:
            out.append(text[index : event.start()])
            index = event.start()
        char = text[index]
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
                pair = _COMMENT_EVENT.search(text, index)
                end = len(text) if pair is None else pair.start()
                out.append("\n" * text.count("\n", index, end))
                if pair is None:
                    index = end
                    break
                depth += 1 if pair.group() == "/-" else -1
                index = pair.end()
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
    indexed: bool = True
    """False when the sources were not read, so no name was looked up."""
    links_blueprint: bool = False
    """True when the blueprint's files are in ``ref``'s commit too, so links into it show the published bytes."""
    spellings: Mapping[str, str] = field(default_factory=dict)
    """Committed path of each repository path whose name Git stores under another spelling."""

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
        relative = declaration.path.as_posix()
        path = "/".join(
            quote_from_bytes(os.fsencode(part), safe="")
            for part in self.spellings.get(relative, relative).split("/")
        )
        return f"{self.repository_url}/blob/{self.ref}/{path}#L{declaration.line}"


def build_linker(lean_root: str | Path) -> SourceLinker:
    """Index *lean_root* and resolve repository coordinates."""
    resolved_root = resolve_lean_root(lean_root)
    snapshot, repository_url, ref, linkable_paths = _capture_link_state(resolved_root)
    if repository_url is None:
        repository_url = detect_repository_url(resolved_root)
    return SourceLinker(
        index=snapshot.index,
        repository_url=repository_url,
        ref=ref,
        linkable_paths=linkable_paths,
    )


def _capture_link_state(
    root: Path,
) -> tuple[
    IndexedSourceSnapshot,
    str | None,
    str | None,
    frozenset[Path] | None,
]:
    """Capture source bytes and prove which ones belong to one stable commit."""

    last_snapshot: IndexedSourceSnapshot | None = None
    last_url: str | None = None
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        if attempt:
            time.sleep(_SNAPSHOT_RETRY_DELAY_SECONDS * attempt)
        before_url = detect_repository_url(root)
        before_ref = detect_ref(root)
        snapshot = snapshot_project_sources(root)
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


def verify_repository_snapshot(
    root: str | Path,
    requested_ref: str,
    files: Iterable[tuple[Path, bytes]],
    *,
    required_directories: Iterable[Path] = (),
    reasons: list[str] | None = None,
    spellings: dict[str, str] | None = None,
) -> str | None:
    """Return the commit whose tree holds every captured file, byte for byte.

    A clean checkout that Git converts on the way out (LFS, eol attributes) is
    accepted by hashing the captured bytes through Git's clean filter. Otherwise
    ``None``, with the first reason appended to *reasons*, naming paths relative
    to the repository only. The shared Git runner strips ambient overrides.
    Given *spellings*, a file Git committed under another spelling of its name
    is accepted too, and its committed path recorded there (``_git_tree_oids``).
    """

    def refuse(reason: str) -> None:
        if reasons is not None:
            reasons.append(reason)

    root = Path(root).resolve()
    commit = _git(root, "rev-parse", "--verify", "--end-of-options", f"{requested_ref}^{{commit}}")
    algorithm = _git(root, "rev-parse", "--show-object-format")
    if commit is None or algorithm not in {"sha1", "sha256"}:
        return refuse(f"{requested_ref!r} does not name a commit in a local Git repository")
    prefix = _git(root, "rev-parse", "--show-prefix")
    if prefix:  # tree paths start at the top level, so nothing below could match
        return refuse(
            f"the Lean root is the subdirectory {prefix.rstrip('/')} of its Git repository; "
            "links need the repository top level"
        )
    captured: dict[str, bytes] = {}
    try:
        for path, data in files:
            relative = path.relative_to(root).as_posix()
            if captured.setdefault(relative, data) != data:
                return refuse(f"two captured files claim {relative}")
        directories = [path.relative_to(root).as_posix() for path in required_directories]
    except ValueError:
        return refuse("a captured file or directory is outside the repository")
    blobs = _git_tree_oids(root, commit, tuple(sorted(captured)), spellings)
    if blobs is None:
        return refuse("the commit's tree could not be read")
    for path, data in captured.items():
        if path not in blobs:
            return refuse(f"{path} is not a file in commit {commit[:12]}")
        framed = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
        if hashlib.new(algorithm, framed).hexdigest() != blobs[path] and (
            _git_clean_blob_id(root, path, data) != blobs[path]
        ):
            return refuse(f"{path} differs from commit {commit[:12]}")
    if any(_git(root, "cat-file", "-t", f"{commit}:{directory}") != "tree" for directory in directories):
        return refuse(f"a required directory is not in commit {commit[:12]}")
    return commit


def _git_clean_blob_id(root: Path, path: str, data: bytes) -> str | None:
    """Hash captured bytes as Git would store them at *path* (clean filter, eol)."""

    try:
        result = subprocess.run(
            ["git", "hash-object", "--stdin", f"--path={path}"],
            cwd=str(root),
            input=data,
            capture_output=True,
            env=_git_environment(),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.decode("ascii", "replace").strip()
    return output if result.returncode == 0 and output else None


def _git_tree_oids(
    root: Path,
    commit: str,
    paths: tuple[str, ...],
    spellings: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Read raw blob IDs of regular files at exact paths without Git's quoting layer.

    macOS Git with ``core.precomposeUnicode`` finds a name decomposed on disk
    under the composed spelling it committed, which is not always NFC, and
    prints that spelling. Given *spellings*, such a record is matched to the
    path asked for by NFC form, and the committed path recorded under it.
    """

    result: dict[str, str] = {}
    for start in range(0, len(paths), 128):
        batch = paths[start : start + 128]
        if not batch:
            continue
        try:
            completed = subprocess.run(
                [
                    "git",
                    "ls-tree",
                    "-rz",
                    "--full-tree",
                    commit,
                    "--",
                    *(f":(literal){path}" for path in batch),
                ],
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
        asked = set(batch)
        composed = {} if spellings is None else {unicodedata.normalize("NFC", path): path for path in batch}
        for record in completed.stdout.split(b"\0"):
            if not record:
                continue
            header, separator, encoded_path = record.partition(b"\t")
            fields = header.split()
            if not separator or len(fields) != 3 or fields[0] not in {b"100644", b"100755"}:
                continue
            printed = os.fsdecode(encoded_path)
            path = printed if printed in asked else composed.get(unicodedata.normalize("NFC", printed), printed)
            result[path] = fields[2].decode("ascii")
            if spellings is not None and path != printed:
                spellings[path] = printed
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
    "Alias",
    "Declaration",
    "LeanSourceError",
    "MANAGED_OUTPUT_SCHEMAS",
    "PACKET_SCHEMA",
    "PASSAGE_SCHEMA",
    "PUBLICATION_MANIFEST",
    "PUBLICATION_MANIFEST_MAX_BYTES",
    "PUBLICATION_SCHEMA",
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
    "resolve_lean_root",
    "snapshot_project_sources",
    "strip_lean_comments",
    "verify_repository_snapshot",
]
