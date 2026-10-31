"""Resolve blueprint ``lean:`` declarations to their source location.

Scanning the project's own Lean files keeps two promises at once: a proved node
can link to the line that proves it, and a ``lean:`` name that resolves to
nothing is a validation error rather than a broken link -- the job
``leanblueprint checkdecls`` does for LaTeX blueprints.

The scanner is a lexical pass, not an elaborator. It tracks ``namespace`` and
comment nesting and stops at ``#exit``, which is enough for declarations
written in the ordinary way, and deliberately reports nothing it cannot see
rather than guessing.

Indexing reads the whole Lean root or nothing (the user-facing rules are in
``autoform_cli/reference/validation-and-audit.md``). Besides ``.lean`` files,
the walk reads only ``manifest.json``, to recognize skeleton and review packet
output folders, and ``publication.json``, to recognize rendered sites; every
command skips the same folders, so ``render`` and the other commands index the
same files. ``render`` marks the site it is writing before it copies any file
into it, so a command that indexes during a render skips it too. ``mkdocs
build`` empties ``site/`` and then copies files in name order, so ``site/`` can
hold copies before its ``publication.json``, which is why a command that
indexed during the build must run again. A capture that sees Lean inputs
change is retried; folders the walk skips and files other than ``.lean``
sources may change during a capture.

The command may follow ``@[...]`` attribute lists and the modifiers
``public``, ``private``, ``protected``, ``noncomputable``, ``partial``,
``unsafe``, ``scoped``, and ``local``. ``alias b := a`` records ``b``, and
``alias ⟨mp, mpr⟩ := a_iff`` each of its names that is not ``_``; the ``:=``
must be on the alias's line, and the target on that line or the next nonblank
one.

A name declared more than once selects its one declaration that is neither
``private`` nor a wanted command, since every other module sees the public
one. The rule sees only indexed declarations: a public one behind an unindexed
prefix such as ``open X in``, or written ``«main»`` where the article writes
``main``, leaves a private one as the name's only declaration. In a file that
starts with ``module``, a declaration outside a ``public section`` and without
``public`` is private to Lean but public to the index, so such a name may be
reported as defined more than once where Lean accepts it, or selected where
Lean binds it nowhere outside its file.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote_from_bytes

from . import _directory_binding as directory_binding
from ._fs import directory_path
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
    r"|theorem_wanted|proof_wanted|def_wanted|instance_wanted|alias)\b(?:\s+(.*))?$"
)
# Core command heads and their modifiers cannot supply a declaration's name.
# Compare complete identifiers: `open'`, `Qualified.open` and `«open»` are names.
# These cover the core command parsers and their stock syntax extensions. The
# continuation grammar below also rejects syntax that cannot follow a
# declaration identifier, without assuming every command extension is known.
_COMMAND_HEADS = frozenset(
    {
        "abbrev", "add_decl_doc", "alias", "assert_not_exists", "assert_not_imported",
        "attribute", "aux_def", "axiom", "binder_predicate", "builtin_cbv_simproc",
        "builtin_cbv_simproc_decl", "builtin_dsimproc", "builtin_dsimproc_decl",
        "builtin_grind_propagator", "builtin_initialize", "builtin_simproc",
        "builtin_simproc_decl", "cbv_simproc", "cbv_simproc_decl", "class", "coinductive",
        "declare_bitwise_int_theorems", "declare_bitwise_uint_theorems",
        "declare_command_config_elab", "declare_command_config_elab_legacy",
        "declare_config_elab", "declare_config_elab_legacy", "declare_core_config_elab",
        "declare_eval_bin", "declare_eval_bin_bitwise", "declare_eval_bin_bool_pred",
        "declare_int_theorems", "declare_simp_like_tactic", "declare_sint_simprocs",
        "declare_syntax_cat", "declare_term_config_elab", "declare_uint_simprocs",
        "declare_uint_theorems", "def", "def_eval_config_item", "def_wanted",
        "deprecated_module", "deprecated_syntax", "derive_eval_expr_instance_using_meta_eval",
        "deriving", "docs_to_verso", "dsimproc", "dsimproc_decl", "elab", "elab_rules",
        "elab_stx_quot", "end", "end_local_scope", "ensure_eval_expr_instance",
        "ensure_eval_term_expr_instances", "ensure_eval_term_instance", "example",
        "export", "gen_cnstr_fns", "grind_annotated", "grind_pattern", "grind_propagator",
        "import", "include", "inductive",
        "infix", "infixl", "infixr", "init_grind_norm", "init_quot", "initialize", "irreducible_def",
        "instance", "instance_wanted", "lemma", "macro", "macro_rules",
        "make_elab_grind_config", "make_elab_simp_config", "module",
        "mutual", "namespace", "norm_cast_add_elim", "notation", "omit", "opaque", "open",
        "postfix", "postprocess_traces", "prefix", "prelude", "proof_wanted", "recommended_spelling",
        "register_builtin_option", "register_error_explanation", "register_option",
        "register_grind_attr", "register_label_attr", "register_linter_set", "register_simp_attr",
        "register_sym_dsimp", "register_sym_simp", "register_sym_simp_attr", "register_tactic_tag",
        "register_try?_tactic", "reprove", "run_cmd", "run_elab", "run_meta",
        "run_tac", "seal", "section", "set_library_suggestions", "set_option",
        "show_panel_widgets", "simproc", "simproc_decl", "store_traces_as", "structure",
        "syntax", "tactic_extension", "test_extern", "theorem", "theorem_wanted",
        "unif_hint", "universe", "unlock_limits", "unseal", "variable", "wait_for_cancel_once_command",
        "with_weak_namespace",
    }
)
_COMMAND_MODIFIERS = frozenset(
    {"public", "private", "protected", "meta", "noncomputable", "unsafe", "partial", "nonrec", "local", "scoped"}
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
#: A packet tree still being staged beside its destination, skipped by name,
#: so a stage an older writer left without its manifest is not project source.
_OUTPUT_STAGE = re.compile(r"\A\..+\.autoform-stage-[0-9a-f]{16}\Z")
_MANAGED_OUTPUT_MANIFEST = "manifest.json"
_MANAGED_OUTPUT_MANIFEST_BYTE_LIMIT = 16 * 1024 * 1024
# Skeleton manifests grow with the declaration set, and their sorted schema key
# follows the entries, so their classification bound is intentionally larger.
_SNAPSHOT_ATTEMPTS = 3
_SNAPSHOT_RETRY_DELAY_SECONDS = 0.05
#: Known schemas of the skeleton command's packet and passage manifests.
PACKET_SCHEMA = "autoform-skeleton-packets/v2"
PASSAGE_SCHEMA = "autoform-skeleton-passages/v2"
REVIEW_PACKET_SCHEMA = "autoform-review-packets/v1"
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
#: v3 adds the shared dependency explorer to v2's inventoried site and hashes
#: the blueprint under a label of its own.
PUBLICATION_SCHEMA = "autoform-publication/v3"
#: Sites render may replace: each records a file inventory to check first.
_REPLACEABLE_SCHEMAS = frozenset({"autoform-publication/v2", PUBLICATION_SCHEMA})
_IGNORED_OUTPUT_SCHEMAS = MANAGED_OUTPUT_SCHEMAS | {("packets", REVIEW_PACKET_SCHEMA)}

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
        other files declare: importing a public one would be a clash, and an
        imported private one is tried only after this file's own. A candidate
        only below the alias in its file is not declared yet, so Lean looks
        further out. A ``protected`` candidate is skipped for a one-component
        target unless it is also private; written ``private`` is not consulted
        here, and the module system can make a declaration private without it,
        so the answer is ``None`` again.
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
        given = directory_binding.lexical_absolute_path(lean_root)
        # As a blueprint root is resolved, so paths under both compare by spelling.
        root = directory_path(given) or given.resolve()
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
                None,  # it lists every site file, so it grows with the project
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
        or any(_OUTPUT_STAGE.match(part) for part in relative.parts)
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
        and (kind, schema) in _IGNORED_OUTPUT_SCHEMAS
        and isinstance(value.get(kind), list)
    )


def _is_complete_publication(data: bytes) -> bool:
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        return False
    schema = value.get("schema") if isinstance(value, dict) else None
    return (
        isinstance(value, dict)
        and isinstance(schema, str)
        and schema in {"autoform-publication/v1", *_REPLACEABLE_SCHEMAS}
        and value.get("complete") is True
    )


def _update_source_digest(digest, relative: Path, data: bytes) -> None:
    encoded = os.fsencode(relative.as_posix())
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def _select(occurrences: list[Declaration]) -> Declaration | None:
    """The declaration a name means among all of its declarations, when the index can be sure which.

    A private declaration, including the placeholder a wanted command makes,
    is local to its file, so beside them every other module sees the one
    public declaration. Two or more public ones, or only private and wanted
    ones, leave no single answer.
    """

    public = [item for item in occurrences if not item.private and item.keyword not in _WANTED_KEYWORDS] or occurrences
    return public[0] if len(public) == 1 else None


def _scan(text: str, relative: Path) -> tuple[list[Declaration], int]:
    """The declarations in ``text`` and the number of its lines Lean reads."""
    found: list[Declaration] = []
    namespaces: list[str] = []
    scopes: list[str | None] = []

    masked = _without_lean_comments(text, mask_literals=True)
    lines = masked.splitlines()
    unmasked: list[str] = []

    def literal_line(index: int) -> str:
        """Line ``index`` with its literals back; masking keeps each literal's length, so the offsets agree."""
        if not unmasked:
            kept, offset = _without_lean_comments(text), 0
            for line in masked.splitlines(keepends=True):
                unmasked.append(kept[offset : offset + len(line)])
                offset += len(line)
        return unmasked[index]

    # Masking shortens a comment, so a comment before a field on its line would
    # move the field's column: columns come from the text as written, unless a
    # comment or string holds a line separator other than a line end.
    written = text.splitlines()
    if len(written) == len(lines) + 1 and text.endswith(written[-1]) and written[-1].lstrip().startswith(("--", "/-")):
        # A comment on the last line, with no line end after it, masks to nothing.
        written.pop()
    indents = [len(line) - len(line.lstrip()) for line in (written if len(written) == len(lines) else lines)]
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
            # Lean opens a scope for each component, and `end B` closes one.
            for part in _name_parts(name):
                namespaces.append(part)
                scopes.append(part)
            continue

        section_match = _SECTION.match(line)
        if section_match or _MUTUAL.match(line):
            parts = _name_parts(section_match.group(1)) if section_match else []
            scopes.extend([None] * max(len(parts), 1))
            continue

        end_match = _END.match(line)
        if end_match:
            for _ in range(len(_name_parts(end_match.group(1))) or 1):
                if scopes:
                    closed = scopes.pop()
                    if closed is not None and namespaces:
                        namespaces.pop()
            continue

        declaration_match = _DECLARATION.match(line)
        if declaration_match:
            keyword = declaration_match.group(1)
            body = declaration_match.group(2)
            body_index = number - 1
            name = None
            if not body:
                body_index = next((later for later in range(number, len(lines)) if lines[later].strip()), len(lines))
                if body_index == len(lines):
                    continue
                body = lines[body_index].lstrip()
                if keyword == "alias":
                    if _ALIAS.match(body) is None:
                        continue
                else:
                    name = _continuation_name(body)
                    if name is None:
                        continue
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
                following = next((lines[later] for later in range(body_index + 1, len(lines)) if lines[later].strip()), "")
                found.extend(
                    _aliases(body, following, namespaces, relative, number, protected, private)
                )
                continue
            name = name or _name_token(body)
            if name is None:
                continue
            # `_name_token` stops before the `{` in an explicit universe
            # binder, leaving the syntactic separator behind.
            name = name.removesuffix(".")
            qualified = _qualified(namespaces, name)
            found.append(Declaration(qualified, relative, number, keyword, protected, private))
            # `class abbrev` and `class inductive` read as a class of that name, with no fields.
            if keyword in ("structure", "class") and name not in ("abbrev", "inductive"):
                found.extend(
                    _fields(lines, literal_line, indents, number - 1, body_index, qualified, relative, private)
                )
    return found, len(lines)


#: What may stand before a structure field's name: modifiers and attribute lists.
_FIELD_MODIFIERS = frozenset({"private", "protected", "public", "noncomputable", "partial", "unsafe"})
#: What a structure's header is read by: quoted names, brackets, and what ends it.
_HEADER_TOKEN = re.compile(r"«[^»]*»|\bwhere\b|:=|[()\[\]{}⦃⦄]")


def _fields(
    lines: list[str],
    literal_line: Callable[[int], str],
    indents: list[int],
    keyword_index: int,
    header_index: int,
    structure: str,
    relative: Path,
    private: bool,
) -> list[Declaration]:
    """The fields a ``structure`` or ``class`` declares after its ``where``, each as ``Structure.field``.

    Lean makes each field a projection constant of that name. The fields
    follow ``where``, or the older ``:=``, outside the header's binders. A
    field line starts at the column of the first line after it. A deeper line
    continues a field, and so does a line at that column that Lean reads on
    into; a shallower one, ``deriving`` or a command ends them. A
    bracketed group names each of its fields, a plain field only its first
    name. A field given only a default, ``x := v`` or ``(x := v)``, may set an
    inherited field, which Lean declares no constant for, so it is read only
    when the structure extends nothing. The constructor's ``mk ::`` names no
    field, and on a line of its own it may stand at any column. A parent the
    header names, ``extends toA : A``, is a field as well.
    """

    base = indents[keyword_index]
    header: list[tuple[int, str]] = []
    found = None
    depth = 0
    for index in range(header_index, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if index > header_index and (indents[index] <= base or _command_line(line)) or _EXIT.match(line):
            break
        for token in _HEADER_TOKEN.finditer(line):
            if token.group() in _BINDER_CLOSERS:
                depth += 1
            elif token.group() in _BINDER_CLOSERS.values():
                depth -= 1
            elif token.group() in ("where", ":=") and depth <= 0:
                found = (index, line[token.end() :])
                break
        if found is not None:
            header.append((index, line[: token.start()]))
            break
        header.append((index, line))
    extends, parents = _parents(header)
    # A parent the header names, `extends toA : A`, is a field too.
    fields = [
        Declaration(f"{structure}.{name}", relative, number, "field", False, private)
        for name, number in parents
        if name not in _FIELD_FORBIDDEN
    ]
    if found is None:
        return fields
    where_index, inline = found

    def read(text: str, number: int) -> bool:
        """Record the fields one field line starts; ``False`` once ``deriving`` or a command ends them."""
        constructor = False
        while True:
            words = _SCOPED.sub(" ", _ATTRIBUTE_LIST.sub(" ", text)).split()
            modifiers = set()
            while words and words[0] in _COMMAND_MODIFIERS:
                modifiers.add(words.pop(0))
            text = " ".join(words)
            # Each group of names, with what follows their binders: a type, a default, or neither.
            groups: list[tuple[list[str], str]] = []
            if text[:1] in _BINDER_CLOSERS:
                # `(x y : T)`, `{x : T}`, `[inst : C]`: the names a group starts with are all fields.
                while text[:1] in _BINDER_CLOSERS:
                    end = _bracket_end(text)
                    inner = (text[1 : end - 1] if end > 0 else text[1:]).lstrip()
                    names = []
                    while name := _FIELD_NAME.match(inner):
                        names.append(name.group())
                        inner = inner[name.end() :].lstrip()
                    groups.append((names, _after_binders(inner)))
                    if end < 0:
                        break
                    text = text[end:].lstrip()
                break
            name = _FIELD_NAME.match(text)
            if name is None:
                return True
            if name.group() in _FIELD_ENDS:
                return False
            tail = _after_binders(text[name.end() :])
            if not tail.startswith("::"):
                groups.append(([name.group()], tail))
                break
            # The constructor's name; a field may follow it on its line, but not a second one.
            text = tail[2:].strip()
            if not text or constructor:
                return True
            constructor = True
        for names, tail in groups:
            if not tail.startswith(":") or (tail.startswith(":=") and extends):
                continue
            for member in names:
                if member not in _FIELD_FORBIDDEN:
                    fields.append(
                        Declaration(
                            f"{structure}.{member}", relative, number, "field",
                            "protected" in modifiers, private or "private" in modifiers,
                        )
                    )
        return True

    if inline.strip() and not read(inline.strip(), where_index + 1):
        return fields
    start = where_index + 1
    if not inline.strip():
        # The constructor may stand alone above the fields, even at the
        # structure's own column, and the fields' column is then the next line's.
        first = next((index for index in range(start, len(lines)) if lines[index].strip()), None)
        if first is not None and _constructor_only(lines[first]):
            start = first + 1
    # Each field line with the lines that continue it, such as a type on the
    # next line, a second bracketed group, or the rest of a term at its column.
    entries: list[tuple[int, list[str]]] = []
    column = None
    last = where_index if inline.strip() else None
    depth = _depth(inline)
    # A field written after `where`, past any constructor, sets the field column.
    after = inline
    if (name := _CONSTRUCTOR_NAME.match(inline)) and (rest := _after_binders(inline[name.end() :])).startswith("::"):
        after = rest[2:]
    if after.strip():
        column = len(lines[where_index]) - len(after.lstrip())
    for index in range(start, len(lines)):
        text = lines[index].strip()
        if not text:
            continue
        if _EXIT.match(text):
            break
        indent = indents[index]
        if column is None:
            if indent <= base:
                break
            column = indent
        if indent < column:
            break
        if indent == column and depth <= 0 and last is not None and _DO_END.search(lines[last]):
            # A `do` block at the field column, outside brackets, takes every line after it there.
            break
        if indent == column and depth <= 0 and not (
            last is not None and _continues(lines[last], literal_line, last, text)
        ):
            entries.append((index + 1, [text]))
            depth = _depth(text)
        else:
            if entries:
                entries[-1][1].append(text)
            depth += _depth(text)
        last = index
    # Attribute lists and modifiers may stand on lines of their own above a field.
    modifiers = ""
    for number, parts in entries:
        text = " ".join(parts)
        if _MODIFIER_LINE.fullmatch(text):
            modifiers += f" {text}"
            continue
        if not read(f"{modifiers} {text}".strip(), number):
            break
        modifiers = ""
    return fields


def _parents(header: list[tuple[int, str]]) -> tuple[bool, list[tuple[str, int]]]:
    """Whether a structure's header extends anything, and each parent it names, with its line."""

    for position, (index, text) in enumerate(header):
        extends = re.search(r"\bextends\b", text)
        if extends is not None:
            break
    else:
        return False, []
    named: list[tuple[str, int]] = []
    depth, start = 0, True
    # The parents are separated by the commas outside brackets.
    quoted = False
    for index, text in [(index, text[extends.end() :]), *header[position + 1 :]]:
        for offset, char in enumerate(text):
            if start and not char.isspace():
                if parent := _NAMED_PARENT.match(text, offset):
                    named.append((parent.group(1), index + 1))
                start = False
            if quoted or char == "«":
                quoted = char != "»"
            elif char in _BINDER_CLOSERS:
                depth += 1
            elif char in _BINDER_CLOSERS.values():
                depth -= 1
            elif char == "," and depth == 0:
                start = True
    return True, named


def _name_prefix(name: str) -> str:
    """``name`` without its last component, which a guillemet may quote together with dots."""

    if name.endswith("»") and "«" in name:
        return name[: name.rindex("«")].removesuffix(".")
    return name.rpartition(".")[0]


def _after_binders(text: str) -> str:
    """What follows a field's binders, as in ``label (n : Nat) m : String``: its type, default or ``::``."""

    while True:
        text = text.lstrip()
        if text[:1] in _BINDER_CLOSERS:
            end = _bracket_end(text)
            if end < 0:
                return ""
            text = text[end:]
        elif binder := _FIELD_NAME.match(text):
            text = text[binder.end() :]
        else:
            return text


def _constructor_only(line: str) -> bool:
    """Whether ``line`` holds only a structure's constructor, such as ``private mk (n : Nat) ::``."""

    words = _ATTRIBUTE_LIST.sub(" ", line).split()
    while words and words[0] in _FIELD_MODIFIERS:
        words.pop(0)
    text = " ".join(words)
    name = _FIELD_NAME.match(text)
    return name is not None and _after_binders(text[name.end() :]) == "::"


def _depth(text: str) -> int:
    """How many brackets ``text`` leaves open, so that a term such as ``{ a := 1`` goes on past its line."""

    text = _QUOTED_NAME.sub("", text)
    return sum(map(text.count, "([{⦃⟨")) - sum(map(text.count, ")]}⦄⟩"))


def _command_line(line: str) -> bool:
    """Whether ``line`` starts a command that ends a structure, such as ``@[simp] theorem``."""

    words = _SCOPED.sub(" ", _ATTRIBUTE_LIST.sub(" ", line)).split()
    while words and words[0] in _COMMAND_MODIFIERS:
        words.pop(0)
    return bool(words) and words[0] in _FIELD_ENDS


def _continues(above: str, literal_line: Callable[[int], str], index: int, text: str) -> bool:
    """Whether a line at the field column continues the field above it instead of starting one.

    Lean reads on into the field above after a comma, an arrow, a relation,
    an operator or an open bracket, and a field line starts with neither a
    term keyword nor binders followed by more of a term, as in
    ``(h : 0 < n) → P n``. A command starts no term, so it never continues one.
    """

    # A command cannot go on a term, except `open … in` and `set_option … in`.
    if _command_line(text) and text.split(maxsplit=1)[0] not in ("open", "set_option"):
        return False
    # A masked literal is blank, so a line that seems to read on may end in a
    # literal instead: masking keeps each literal's length.
    if _OPEN_END.search(above) and len(above.rstrip()) == len(literal_line(index).rstrip()):
        return True
    if text.split(maxsplit=1)[0] in _TERM_KEYWORDS:
        return True
    while text[:1] in _BINDER_CLOSERS:
        end = _bracket_end(text)
        if end < 0:
            return False
        text = text[end:].lstrip()
        if text and text[:1] not in _BINDER_CLOSERS and not _FIELD_NAME.match(text):
            return True
    return False


def _bracket_end(text: str) -> int:
    """The index just past the bracket that closes the one ``text`` opens with, or -1 if this line does not close it."""

    closers: list[str] = []
    position = 0
    while position < len(text):
        char = text[position]
        if char == "«":
            end = text.find("»", position + 1)
            if end == -1:
                return -1
            position = end + 1
            continue
        if char in _BINDER_CLOSERS:
            closers.append(_BINDER_CLOSERS[char])
        elif char in _BINDER_CLOSERS.values():
            if not closers or char != closers.pop():
                return -1
            if not closers:
                return position + 1
        position += 1
    return -1


_ATTRIBUTE_LIST = re.compile(r"@\[[^\]]*\]")
#: A line holding only attribute lists and modifiers, or nothing.
_MODIFIER_LINE = re.compile(
    r"\s*(?:(?:@\[[^\]]*\]|public|private|protected|noncomputable|partial|unsafe|scoped|local)\s*)*"
)


def _qualified(namespaces: list[str], name: str) -> str:
    """The full name of ``name`` declared in ``namespaces``; ``_root_.`` leaves them out."""

    return name.removeprefix("_root_.") if name.startswith("_root_.") else ".".join([*namespaces, name])


_NAME_TOKEN = re.compile(r"(?:«[^«»]*»|[^\s:(){}\[\]⦃⦄,«»])+")


def _name_parts(name: str) -> list[str]:
    """The components of ``name``, where a guillemet may quote dots: ``A.«b.c»`` has two."""

    return re.findall(r"«[^»]*»|[^.«»]+", name)


def _name_token(text: str) -> str | None:
    """Read one possibly guillemet-quoted Lean identifier from ``text``."""

    match = _NAME_TOKEN.match(text)
    return match.group() if match and text[match.end() : match.end() + 1] not in ("«", "»") else None


# Lean's isIdFirst/isIdRest (Init.Meta.Defs): ASCII letters plus the listed
# letter-like blocks; later characters also admit digits, primes and subscripts.
_ID_LETTER = (
    r"A-Za-z_\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u017f"
    r"\u0391-\u039f\u03a1-\u03a2\u03a4-\u03a9\u03b1-\u03ba\u03bc-\u03fb"
    r"\u1f00-\u1ffe\u2100-\u214f\U0001d49c-\U0001d59f"
)
_ID_REST = rf"[{_ID_LETTER}0-9'!?\u2080-\u2089\u2090-\u209c\u1d62-\u1d6a\u2c7c]*"
_ID_COMPONENT = rf"(?:«[^«»]*»|[{_ID_LETTER}]{_ID_REST})"
_CONTINUATION_IDENTIFIER = re.compile(rf"{_ID_COMPONENT}(?:\.{_ID_COMPONENT})*")
#: One component of a Lean name, such as a structure field's.
_FIELD_NAME = re.compile(_ID_COMPONENT)
#: A parent that names its projection, ``toA : A`` in ``extends toA : A``.
_NAMED_PARENT = re.compile(rf"({_ID_COMPONENT})\s*:(?![:=])")
_SIGNATURE_WORDS = frozenset({"where", "extends", "requires", "ensures"})
_NAME_FORBIDDEN = _COMMAND_HEADS | _COMMAND_MODIFIERS | {"where", "extends", "by", "do", "in", "let", "match"}
#: ``module`` heads a file, not a command, so a field may have that name.
_FIELD_FORBIDDEN = _NAME_FORBIDDEN - {"module"}
#: A command at the field column ends the structure: a core one, or one of
#: Mathlib's that reads like a field, `notation3:max "x" => 1` or `recall f : T`.
_FIELD_ENDS = (_COMMAND_HEADS - {"module", "prelude", "import"}) | {"notation3", "recall"}
#: Keywords that start a term, never a field.
_TERM_KEYWORDS = frozenset(
    {"by", "calc", "do", "else", "fun", "have", "if", "let", "match", "nofun", "nomatch", "show", "suffices", "then"}
)
_DO_END = re.compile(r"(?:^|\s)do\s*$")
#: The name of a constructor that may open the text after ``where``, as in ``where mk (T) :: a : Nat``.
_CONSTRUCTOR_NAME = re.compile(rf"\s*(?:@\[[^\]]*\]\s*)*(?:(?:private|protected)\s+)*{_ID_COMPONENT}")
#: Mathlib's ``scoped[NS]``, which scopes the command after it to a namespace.
_SCOPED = re.compile(r"^\s*scoped\s*\[[^\]]*\]")
_QUOTED_NAME = re.compile(r"«[^»]*»")
#: The end of a line that a term continues past: a comma, an open bracket, an
#: arrow, a relation, an operator after a space, or the ``in`` of a term's
#: ``open … in``, but not the constructor's ``mk ::`` or a postfix such as
#: ``Type*``, ``ℕ+`` or Mathlib's ``n !``.
_OPEN_END = re.compile(
    r"(?:(?<!:):|[,=(\[{⟨→↔∧∨×∘•≤≥≠≈≡∈∉⊆⊂⊇⊃∣≃≅⟶↦]|=>|->|<\||\|>|(?:^|\s)(?:[-+*/^%<>&|$]+|fun|by|in))\s*$"
)
_BINDER_CLOSERS = {"(": ")", "{": "}", "[": "]", "⦃": "⦄"}


def _continuation_name(text: str) -> str | None:
    """Read a name followed by a possible declaration signature, not another command.

    Only the new keyword-on-its-own-line case uses this conservative guard.
    A name may stand alone or be followed by universe parameters, binders, a
    type or a declaration value. A bracketed binder may continue on later
    lines; source indexing does not elaborate its contents. Attribute lists,
    command modifiers and arbitrary punctuation cannot be identifiers here.
    """

    match = _CONTINUATION_IDENTIFIER.match(text)
    if match is None or match.group() in _NAME_FORBIDDEN:
        return None
    name = match.group()
    rest = text[match.end() :]
    if rest.startswith(".{"):
        # declId's universe list belongs to the signature, not the public name.
        rest = rest[1:]
    while rest.strip():
        rest = rest.lstrip()
        if rest.startswith((":", "|")):
            return name
        if rest[0] in _BINDER_CLOSERS:
            closers: list[str] = []
            position = 0
            while position < len(rest):
                char = rest[position]
                if char == "«":
                    end = rest.find("»", position + 1)
                    if end == -1:
                        return None
                    position = end + 1
                    continue
                if char in _BINDER_CLOSERS:
                    closers.append(_BINDER_CLOSERS[char])
                elif char in _BINDER_CLOSERS.values():
                    if not closers or char != closers.pop():
                        return None
                    if not closers:
                        break
                position += 1
            if closers:
                return name  # This binder can finish on a later line.
            rest = rest[position + 1 :]
            continue
        binder = _CONTINUATION_IDENTIFIER.match(rest)
        if binder is None:
            return None
        if binder.group() in _SIGNATURE_WORDS:
            return name
        if binder.group() in _NAME_FORBIDDEN:
            return None
        rest = rest[binder.end() :]
    return name


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
# _without_lean_comments copies the text between two matches of these patterns
# unread, so each must match every position its branch of the loop reacts to:
# a branch whose trigger is missing never runs, while an extra match only
# copies one character. In code, a comment, literal or interpolation brace can
# start there; inside a literal, its closer (", » or '), an escape, or an
# interpolation.
_CODE_EVENT = re.compile(rf"""--|/-|{_RAW_OPEN.pattern}|["«']""")
_INTERPOLATION_EVENT = re.compile(rf"""--|/-|{_RAW_OPEN.pattern}|["«'{{}}]""")
_LITERAL_EVENT = re.compile(r"""["»'\\{]""")
# Inside a block comment: where a nested comment opens or the current one closes.
# Every match changes the depth, so this pattern can't match anything extra.
_COMMENT_EVENT = re.compile(r"-/|/-")


def _without_lean_comments(text: str, *, mask_literals: bool = False) -> str:
    """Remove nested Lean comments, preserving literals or blanking them for declaration scanning.

    Blanking preserves character positions and newlines, including across
    interpolations. Guillemets delimit identifiers, so those stay intact.
    """

    out: list[str] = []
    stack: list[list] = []  # [closer, interpolated] for a string, [None, depth] for `{…}` in `s!"…"`

    def append(fragment: str, literal: bool = False) -> None:
        # A raw string never reaches the stack, so its branch passes literal=True.
        in_literal = literal or any(frame[0] not in {None, "»"} for frame in stack)
        out.append(re.sub(r"[^\r\n]", " ", fragment) if mask_literals and in_literal else fragment)

    index = 0
    while index < len(text):
        top = stack[-1] if stack else None
        literal = top is not None and top[0] is not None
        events = _LITERAL_EVENT if literal else _CODE_EVENT if top is None else _INTERPOLATION_EVENT
        event = events.search(text, index)
        if event is None:
            append(text[index:])
            break
        append(text[index : event.start()])
        index = event.start()
        char = text[index]
        if literal:
            close, interpolated = top
            if close in {'"', "'"} and char == "\\":
                append(text[index : index + 2])
                index += 2
                continue
            # Append before popping, so blanking covers the closer.
            append(char)
            if char == close:
                stack.pop()
            elif interpolated and char == "{":
                stack.append([None, 1])
            index += 1
            continue
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            if newline < 0:
                break
            append("\n")
            index = newline + 1
            continue
        if text.startswith("/-", index):
            # `/--` and `/-!` open a docstring whose body starts after the marker.
            depth, start = 1, index + (3 if text[index + 2 : index + 3] in {"-", "!"} else 2)
            index = len(text)
            for pair in _COMMENT_EVENT.finditer(text, start):
                depth += 1 if pair.group() == "/-" else -1
                if not depth:
                    index = pair.end()
                    break
            append(" " + "\n" * text.count("\n", start, index))
            continue
        previous = text[index - 1] if index else " "
        # An `r` inside a name, or a name literal's, is part of it. Lean reads no
        # escapes in a raw string, so it ends at the first closer: `r"\"` is one
        # backslash.
        raw = None if previous.isalnum() or previous in "_'!?.`" else _RAW_OPEN.match(text, index)
        if raw:
            found = text.find('"' + raw.group(1), raw.end())
            index = len(text) if found < 0 else found + 1 + len(raw.group(1))
            append(text[raw.start() : index], literal=True)
            continue
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
        append(char)
        index += 1
    return "".join(out)


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
    indexed: bool = True
    """False when the sources were not read, so no name was looked up."""
    links_blueprint: bool = False
    """True when the blueprint's files are in ``ref``'s commit too, so links into it show the published bytes."""
    spellings: Mapping[str, str] = field(default_factory=dict)
    """Committed path of each repository path whose name Git stores under another spelling."""
    root_prefix: str = ""
    """The Lean root's path below the repository's top level, or empty at the top level."""

    def location(self, name: str) -> Declaration | None:
        return self.index.find(name)

    def url(self, name: str) -> str | None:
        """Return a permanent link to *name*, or ``None`` if it cannot be built."""
        declaration = self.index.find(name)
        if declaration is None or not self.repository_url or not self.ref:
            return None
        encoded_ref = quote_from_bytes(os.fsencode(self.ref), safe="")
        relative = declaration.path.as_posix()
        if self.root_prefix:
            relative = f"{self.root_prefix}/{relative}"
        path = "/".join(
            quote_from_bytes(os.fsencode(part), safe="")
            for part in self.spellings.get(relative, relative).split("/")
        )
        return f"{self.repository_url}/blob/{encoded_ref}/{path}#L{declaration.line}"


__all__ = [
    "IndexedSourceSnapshot",
    "Alias",
    "Declaration",
    "LeanSourceError",
    "MANAGED_OUTPUT_SCHEMAS",
    "PACKET_SCHEMA",
    "PASSAGE_SCHEMA",
    "PUBLICATION_MANIFEST",
    "PUBLICATION_SCHEMA",
    "REVIEW_PACKET_SCHEMA",
    "SourceIndex",
    "SourceLinker",
    "bind_project_source_snapshot",
    "declaration_names",
    "index_failure_message",
    "index_project",
    "resolve_lean_root",
    "snapshot_project_sources",
]
