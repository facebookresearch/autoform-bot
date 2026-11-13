"""``autoform move``: rename an article and everything that names it by its path (#601).

An article is named by its path in three places: its file, or its folder for a folder article; the Markdown links in
the vault's pages; and ``blueprint/links/``, whose annotations the loader matches by the page their subject names and
whose files the writer keeps beside the article's path. A move renames all three together, with the implementation note
and read-back cards named by the article's node ID, and keeps its ``article_id``, which carries those named by it, its
review approval and claims. Every change is decided before anything is written, written together, and checked
against the graph loaded again under the new names: if anything then reads otherwise, the whole write is undone.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from . import annotation_writer as writer
from ._repository import run_git
from .annotations import LINKS_DIR, _local, read_links
from .claims import ClaimBoard, ClaimTransportError, _repository_is_remote, author_claim_key
from .graph import (
    ARTICLE_ID_PATTERN,
    ATLAS_SCHEMA,
    IMPLEMENTATION_NOTES_DIR,
    Graph,
    GraphValidationError,
    Node,
    _node_id,
    load_graph,
)
from .markdown import (
    _DESTINATION,
    GENERATED_VIEWS,
    article_parts,
    blueprint_root,
    published_lines,
    published_page,
    retarget_links,
)
from .readback import READBACKS_DIR
from .skeleton import source_passage

MOVE_PLAN_SCHEMA = "autoform-move-plan/v1"
_RENAME = "rename it (in Git: git mv through a temporary name)"
_CLAIM_TIMEOUT_S = 120.0
_SPACE = re.compile(r"[ \t\n\r]*")
_ANNOTATION_SUFFIXES = (".json", ".jsonl", ".ndjson")
#: A Markdown backslash escape, which a destination reads as the punctuation it escapes.
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
#: The facts and flags an article keeps through a move, compared as they are.
_KEPT = (
    "kind",
    "lean",
    "declaration",
    "statement_formalized",
    "statement_retracted",
    "proof_formalized",
    "mathlib",
    "mathlib_declaration",
    "mathlib_file",
    "not_ready",
    "discussion",
    "origin",
    "catalog",
    "recorded_area",
    "navigation",
    "article_id",
    "review_approved",
)
#: The links to other articles, by node ID, which a move renames.
_EDGES = ("dependencies", "statement_dependencies", "proof_dependencies", "informal_dependencies")


class MoveError(GraphValidationError):
    """Nothing was moved, for ``issues``; ``left`` names the files a stopped write could not restore."""

    def __init__(self, issues: Sequence[str], left: Sequence[str] = ()) -> None:
        super().__init__(list(issues))
        self.left = tuple(left)


@dataclass(frozen=True, slots=True)
class MovePlan:
    """What a move changes, below the blueprint, decided before anything is written."""

    old: str
    new: str
    #: The moved article's node ID, old and new.
    top: tuple[str, str]
    #: Each file the move renames, ``(from, to)``.
    renamed: tuple[tuple[str, str], ...]
    #: Each file whose bytes it changes where it stands.
    edited: tuple[str, ...]
    #: What it leaves for someone to check or do.
    warnings: tuple[str, ...]
    #: "checked", or "not checked" for a repository with no origin, moved without ``--repo``, or a dry run that could
    #: not read the claim board.
    claims: str
    blueprint: Path = field(repr=False)
    graph: Graph = field(repr=False)
    changes: Mapping[Path, bytes | None] = field(repr=False)
    expected: Mapping[Path, str | None] = field(repr=False)
    #: Each moved article's node ID, old to new.
    ids: Mapping[str, str] = field(repr=False)
    #: Each moved file's absolute path, old to new.
    moves: Mapping[str, str] = field(repr=False)
    #: Each folder that moves whole, the article's, its mirrors in ``links/`` and its folders of notes and cards named
    #: by node ID, old to new.
    folders: tuple[tuple[str, str], ...] = field(repr=False)
    #: Each annotation file read as an article's that the move writes, by the article's page once moved, which must
    #: be read so again.
    annotations: Mapping[Path, tuple[str, ...]] = field(repr=False)
    #: Each moved file's mode, by its old path, which the writer does not keep.
    modes: Mapping[str, int] = field(repr=False)
    #: The text of each page the move rewrites, as it was, by its old path.
    texts: Mapping[Path, str] = field(repr=False)

    def to_json(self, *, dry_run: bool) -> str:
        return json.dumps(self.as_dict(dry_run=dry_run), sort_keys=True, separators=(",", ":"))

    def as_dict(self, *, dry_run: bool) -> dict[str, object]:
        return {
            "schema": MOVE_PLAN_SCHEMA,
            "dry_run": dry_run,
            "old": self.old,
            "new": self.new,
            "node_ids": [{"from": old, "to": new} for old, new in sorted(self.ids.items())],
            "renamed": [{"from": old, "to": new} for old, new in self.renamed],
            "edited": list(self.edited),
            "warnings": list(self.warnings),
            "claims": self.claims,
        }


def plan_move(
    blueprint_dir: str | Path,
    old: str,
    new: str,
    *,
    repo: str | None = None,
    worker_id: str | None = None,
    dry_run: bool = False,
) -> MovePlan:
    """Decide how moving the article at *old* to *new*, both paths below *blueprint_dir*'s ``roadmap/``, changes the
    blueprint, refusing with :class:`MoveError` for every reason it must not; a *dry_run* that cannot read the claim
    board warns instead, since it writes nothing."""

    blueprint = blueprint_root(blueprint_dir)
    if not blueprint.is_dir():
        raise MoveError([f"blueprint directory does not exist: {blueprint_dir}"])
    roadmap = blueprint / "roadmap"
    source, target = _resolved(old), _resolved(new)
    _check_paths(blueprint, roadmap, old, new, source, target)
    graph = load_graph(blueprint)
    nodes = {node.path: node for node in graph.nodes.values()}
    top = nodes.get(source)
    if top is None:
        raise MoveError([f"{old}: not an article the graph loads"])
    issues: list[str] = []
    taken = {node.id for node in graph.nodes.values()}
    folder: tuple[str, str] | None = None
    moves: dict[str, str] = {}
    if source.name == "README.md":
        folder = (str(source.parent), str(target.parent))
        for path in _files(source.parent, blueprint, issues):
            moves[str(path)] = str(target.parent / path.relative_to(source.parent))
    else:
        moves[str(source)] = str(target)
    ids = {
        node.id: _node_id(Path(moves[str(node.path)]), roadmap)
        for node in graph.nodes.values()
        if str(node.path) in moves
    }
    if clash := sorted(new_id for new_id in ids.values() if new_id in taken and new_id not in ids):
        issues.append(f"{new}: its node ID {clash[0]} is another article's")
    if shaped := sorted(new_id for new_id in ids.values() if ARTICLE_ID_PATTERN.fullmatch(new_id)):
        issues.append(f"{new}: its node ID {shaped[0]} has the form of an article ID; choose another name")
    if issues:
        raise MoveError(issues)

    # The annotation files that move too: a moved folder's mirrors in links/, whole, and each moved article's own and
    # Mathlib files.
    carried, mirrors = _carried(blueprint, source, target, folder, issues)
    folders = () if folder is None else (folder, *mirrors)
    read = read_links(blueprint, {node.path: node.id for node in graph.nodes.values()})
    for page, to in ((Path(old_path), Path(new_path)) for old_path, new_path in moves.items()):
        if page in nodes:
            paths = (writer.annotation_paths(blueprint, page), writer.annotation_paths(blueprint, to))
            pairs = [(paths[0].own, paths[1].own), (paths[0].mathlib, paths[1].mathlib)]
            pairs = [(old_file, new_file) for old_file, new_file in pairs if os.path.lexists(old_file)]
            issues.extend(_link_issue(blueprint, file) for pair in pairs for file in pair if _linked(blueprint, file))
            carried.update(pairs)
    # And the implementation note and read-back cards named by the article's node ID, which name it by its path.
    named, named_folders = _named_by_id(blueprint, top.id, ids[top.id], folder is not None, issues)
    carried.update(named)
    folders = (*folders, *named_folders)
    relocated = {**moves, **{str(path): str(to) for path, to in carried.items()}}
    if issues:
        raise MoveError(issues)

    changes: dict[Path, bytes | None] = {}
    expected: dict[Path, str | None] = {}
    warnings: list[str] = []
    edited: list[str] = []

    def change(path: Path, data: bytes | None, before: bytes | None) -> None:
        changes[path], expected[path] = data, _digest(before)

    # The pages: a moved one is written whole at its new path, any other only where a link changed.
    texts: dict[Path, tuple[str, str]] = {}
    for page in _pages(blueprint):
        to = Path(relocated.get(str(page), str(page)))
        try:
            data = writer._read(page)
        except OSError as error:
            if to != page:
                issues.append(f"{_name(blueprint, page)}: cannot be read: {error}")
            continue
        if data is None:
            continue
        text = data.decode("utf-8", "surrogateescape")
        variants: dict[str, str] = {}
        retargeted = retarget_links(text, _retargeter(page, to, relocated, folders, variants))
        where = _name(blueprint, page)
        issues.extend(
            f"{where}:{line}: a link to {destination} cannot be rewritten in place; edit it by hand first"
            for line, destination in retargeted.stale
        )
        warnings.extend(
            f"{where}:{line}: {destination} is left as written, since no reader counts it (an image, raw HTML, "
            "code, a comment or a definition the graph does not read), and no longer names the same file"
            for line, destination in retargeted.uncounted
        )
        warnings.extend(
            f"{where}:{line}: rewritten, though the page shows no link there: {destination}"
            for line, destination in retargeted.unshown
        )
        # Where each destination naming what moves only through other capitals stands, as the loose reading finds it.
        shown = published_lines(text) if variants else ""
        for match in _DESTINATION.finditer(shown):
            if (destination := match.group(match.lastindex)) in variants:
                line = shown.count("\n", 0, match.start(match.lastindex)) + 1
                warnings.append(
                    f"{where}:{line}: {destination} is left as written, since it names "
                    f"{_name(blueprint, Path(variants[destination]))} only through other capitals, which a disk "
                    "that keeps case does not follow"
                )
        written = retargeted.text.encode("utf-8", "surrogateescape")
        if to != page:
            change(to, written, None)
            change(page, None, data)
        elif written != data:
            change(page, written, data)
            edited.append(where)
        if written != data:
            texts[page] = (text, retargeted.text)
    for path, to in ((Path(old_path), Path(new_path)) for old_path, new_path in moves.items()):
        if path not in changes:
            try:
                data = writer._read(path)
            except OSError as error:
                issues.append(f"{_name(blueprint, path)}: cannot be read: {error}")
                continue
            change(to, data, None)
            change(path, None, data)
    for card in _cards(blueprint):
        try:
            data = writer._read(card)
        except OSError:
            continue
        if data is not None:
            text = data.decode("utf-8", "surrogateescape")
            if retarget_links(text, _retargeter(card, card, relocated, folders)).text != text:
                warnings.append(f"{_name(blueprint, card)}: links to what this moves; read-backs keep their bytes")

    # Every annotation file: each reference to what moves is rewritten, keeping every other byte.
    for path in _annotation_files(blueprint, issues):
        to = carried.get(path, path)
        try:
            data = writer._read(path)
        except OSError as error:
            issues.append(f"{_name(blueprint, path)}: cannot be read: {error}")
            continue
        if data is None:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            if to != path:
                issues.append(f"{_name(blueprint, path)}: not UTF-8, so its references cannot be rebased")
            continue
        rebase = _rebaser(str(path.parent), str(to.parent), relocated, folders)
        # As the loader reads them: one annotation a line, or one a .json file.
        rows = [text] if path.name.lower().endswith(".json") else re.split(r"(\r\n|\r|\n)", text)
        written = "".join(_rebased(row, rebase) for row in rows).encode("utf-8")
        if to != path:
            if os.path.lexists(to):
                issues.append(f"{_name(blueprint, to)}: exists, so {_name(blueprint, path)} cannot move there")
                continue
            change(to, written, None)
            change(path, None, data)
        elif written != data:
            change(path, written, data)
            edited.append(_name(blueprint, path))
    # Whatever else a mirrored folder holds moves with it as it is, and so do read-back cards.
    for path, to in carried.items():
        if path not in changes:
            try:
                data = writer._read(path)
            except OSError as error:
                issues.append(f"{_name(blueprint, path)}: cannot be read: {error}")
                continue
            if data is not None:
                change(to, data, None)
                change(path, None, data)
    # Each annotation file read as an article's, which this moves or rewrites, must be read so at its new path: the
    # article's own and Mathlib files, a file that annotates several articles, and one a moved folder carries.
    annotations: dict[Path, tuple[str, ...]] = {}
    for node in graph.nodes.values():
        files = [blueprint / name for name in (read.files.get(node.path, ()) if read is not None else ())]
        if files and (node.id in ids or any(file in changes for file in files)):
            annotations[Path(moves.get(str(node.path), str(node.path)))] = tuple(
                _name(blueprint, Path(relocated.get(str(file), str(file)))) for file in files
            )
    # Every old path goes last, once each new copy and edit is written, so a stop part way leaves each file in place.
    changes = {path: data for path, data in changes.items() if data is not None} | {
        path: data for path, data in changes.items() if data is None
    }
    modes: dict[str, int] = {}
    for path in relocated:
        try:
            modes[path] = stat.S_IMODE(os.lstat(path).st_mode)
        except OSError as error:
            issues.append(f"{_name(blueprint, Path(path))}: cannot be read: {error}")
    if issues:
        raise MoveError(issues)

    # What depends on the old path and this does not rewrite.
    for old_id, new_id in sorted(ids.items()):
        if graph.nodes[old_id].article_id is None:
            warnings.append(f"{old_id}: has no article ID, so a claim on it does not follow it to {new_id}")
    for node in sorted(graph.nodes.values(), key=lambda node: node.id):
        if node.review_approved and (node.id in ids or node.path in texts):
            retarget = _retargeter(node.path, Path(moves.get(str(node.path), str(node.path))), relocated, folders)
            if _drifts(blueprint, node, texts.get(node.path), retarget, relocated):
                warnings.append(
                    f"{node.id}: its review approval drifts, since this rewrites a link in its title or statement "
                    "or moves the source passage it cites"
                )
    if named := _atlas_listed(blueprint, ids):
        warnings.append(f"atlas.json names {', '.join(named)}: edit it, or run its generator again")
    if (blueprint / "structure.md").exists():
        warnings.append("structure.md lists the articles by path: regenerate it")
    if not any(top.path.is_relative_to(hidden) for hidden in graph.unpublished):
        warnings.append("the site publishes what this moves at new addresses, and nothing redirects the old ones")

    # Each article whose page, implementation note or annotation file this changes: its own and Mathlib files, any
    # other the loader reads as its annotation, such as a file that annotates several articles, and the folder
    # defaults each of those inherits, which the loader merges into its rows.
    owners: dict[Path, set[str]] = {}
    for node in graph.nodes.values():
        paths = writer.annotation_paths(blueprint, node.path)
        read_as = [blueprint / name for name in (read.files.get(node.path, ()) if read is not None else ())]
        inherited = [folder / writer.DEFAULTS for file in read_as for folder in _folders(blueprint / LINKS_DIR, file)]
        notes = blueprint / IMPLEMENTATION_NOTES_DIR
        files = [node.path, paths.own, paths.mathlib, *read_as, *inherited, notes / f"{node.id}.md"]
        if node.article_id is not None:
            files.append(notes / f"{node.article_id}.md")
        for path in files:
            owners.setdefault(path, set()).add(node.id)
    changed = [graph.nodes[node_id] for node_id in sorted(set().union(*(owners.get(path, ()) for path in changes)))]
    targets = {key for node in changed for key in (node.id, node.article_id) if key}
    claims, unread = _claims(blueprint, repo, worker_id, targets, dry_run=dry_run)
    warnings.extend(unread)
    renamed = tuple(
        sorted(
            (_name(blueprint, Path(path)), _name(blueprint, Path(to)))
            for path, to in relocated.items()
            if changes.get(Path(path), b"") is None
        )
    )
    return MovePlan(
        old=_name(blueprint, source),
        new=_name(blueprint, target),
        top=(top.id, ids[top.id]),
        renamed=renamed,
        edited=tuple(sorted(set(edited))),
        warnings=tuple(warnings),
        claims=claims,
        blueprint=blueprint,
        graph=graph,
        changes=changes,
        expected=expected,
        ids=ids,
        moves=relocated,
        folders=folders,
        annotations=annotations,
        modes=modes,
        texts={page: text for page, (text, _) in texts.items()},
    )


@dataclass(frozen=True, slots=True)
class MoveResult:
    """The files a move wrote and removed, below the blueprint."""

    written: tuple[str, ...]
    removed: tuple[str, ...]
    #: Whether an interrupt stopped the removal of the folders the move emptied, once the move stood.
    interrupted: bool = False


def apply_move(plan: MovePlan) -> MoveResult:
    """Write *plan*, all or nothing: if the graph loaded again reads otherwise than before under the new names, or the
    write stops, every step is undone."""

    # The writer records each step here before taking it, so a stop before the check passes, or before what it showed
    # to read otherwise is undone, finds them; a step an undo finished, the writer's own included, stays finished.
    steps: list[writer.Written] = []
    finished: set[writer.Written] = set()
    failed: MoveError | None = None
    try:
        try:
            # Each moved file's two steps are tied by its old path, so the writer's undo, when it cannot put a file back
            # where it was, keeps its new copy; a moved _defaults.json is such a file, not a template the writer made.
            pairs = {path: (old,) for new, old in _origins(plan).items() for path in (new, old)}
            writer.write(plan.changes, plan.expected, steps, finished, pairs, moved=pairs)
            _keep_modes(plan)
            problems = _read_back(plan)
        except GraphValidationError as error:  # a stopped write, a mode not kept, or a graph that no longer loads
            problems = list(error.issues)
        if problems:
            failed = _undone(plan, tuple(steps), problems, finished)
    except BaseException as interrupt:
        # Interrupted, as by Ctrl-C: nothing unchecked stays to be read, and the interrupt says what was not restored.
        # The undo here goes over every step no earlier undo finished, so what it says replaces any earlier note.
        interrupt.__notes__ = _notes(_undone(plan, tuple(steps), [], finished))
        raise
    if failed is not None:
        raise failed
    interrupted = False
    try:
        _remove_emptied(plan)
    except KeyboardInterrupt:
        interrupted = True
    return MoveResult(
        tuple(_name(plan.blueprint, step.path) for step in steps if step.sha256 is not None),
        tuple(_name(plan.blueprint, step.path) for step in steps if step.sha256 is None),
        interrupted,
    )


def _check_paths(blueprint: Path, roadmap: Path, old: str, new: str, source: Path, target: Path) -> None:
    """Refuse a move between *source* and *target* that no blueprint could take, before the graph is loaded."""

    issues: list[str] = []
    if not (source.suffix == ".md" and source.is_relative_to(roadmap)):
        issues.append(f"{old}: not a Markdown article below {roadmap}")
    elif source == roadmap / "README.md":
        issues.append(f"{old}: the roadmap's own README stays where it is")
    if not (target.suffix == ".md" and target.is_relative_to(roadmap)):
        issues.append(f"{new}: not a Markdown path below {roadmap}")
    if issues:
        raise MoveError(issues)
    where = _name(blueprint, target)
    if source == target:
        issues.append(f"{new}: the article is there already")
    elif str(source).casefold() == str(target).casefold() or (os.path.lexists(target) and _same(source, target)):
        issues.append(f"{new}: differs from {old} only in case; {_RENAME}")
    elif os.path.lexists(target):
        issues.append(f"{new}: exists")
    if target == roadmap / "README.md":
        issues.append(f"{new}: the roadmap's own README is no article to move onto")
    if any(part.startswith(".") for part in target.relative_to(roadmap).parts):
        issues.append(f"{new}: a hidden path, which the graph does not load")
    if "=" in where:
        issues.append(f"{new}: a path holding '=' is read as an edit by `autoform links set`; choose another name")
    if any(character in where for character in "#?:|\\"):
        issues.append(
            f"{new}: a link to a path holding '#', '?', ':', '|' or '\\' does not name it; choose another name"
        )
    if not _utf8(where):
        issues.append(f"{new}: not UTF-8, which a link names a file in; choose another name")
    if not unicodedata.is_normalized("NFC", where):
        issues.append(f"{new}: not composed (NFC) as Git records it; choose another spelling")
    # A folder spelled with other capitals than the one there is, which a disk that ignores case would write into.
    folder = roadmap
    for part in target.parent.relative_to(roadmap).parts:
        try:
            names = os.listdir(folder)
        except OSError:
            break
        if part not in names:
            if spelled := next((name for name in names if name.casefold() == part.casefold()), None):
                issues.append(f"{new}: {_name(blueprint, folder / part)} differs only in case from {spelled}; use that")
            break
        folder = folder / part
    # A top folder needs its README, which a folder article moving there as that folder brings.
    chapter = roadmap / target.relative_to(roadmap).parts[0]
    brings = source.name == "README.md" and target.parent == chapter
    if chapter != target and not brings and not (chapter / "README.md").is_file():
        issues.append(f"{new}: {_name(blueprint, chapter)} has no README.md, so the graph reads no chapter there")
    if (source.name == "README.md") != (target.name == "README.md"):
        issues.append(f"{new}: a folder article moves as a folder's README.md, and a file article as any other name")
    if source.name == "README.md" and source.parent != target.parent:
        if target.parent.is_relative_to(source.parent):
            issues.append(f"{new}: inside the folder that moves")
        elif os.path.lexists(target.parent):
            issues.append(f"{_name(blueprint, target.parent)}: exists, so the folder cannot move there")
    elif source.name != "README.md":
        twin = target.with_suffix("") / "README.md"
        if os.path.lexists(twin):
            issues.append(f"{new}: {_name(blueprint, twin)} has its node ID")
        if target == roadmap / "roadmap.md":
            issues.append(f"{new}: its node ID, roadmap, is the roadmap's own README's")
    if issues:
        raise MoveError(issues)


def _resolved(article: str) -> Path:
    try:
        return Path(article).expanduser().resolve()
    except (OSError, RuntimeError) as error:
        raise MoveError([f"{article}: cannot be resolved: {error}"]) from None


def _same(one: Path, other: Path) -> bool:
    try:
        return os.path.samefile(one, other)
    except OSError:
        return False


def _files(folder: Path, blueprint: Path, issues: list[str]) -> list[Path]:
    """Every regular file below *folder*, which moves with it; a symbolic link or anything else there cannot."""

    found: list[Path] = []
    for directory, folders, files in os.walk(folder):
        for name in sorted(folders + files):
            path = Path(directory) / name
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                issues.append(f"{_name(blueprint, path)}: not a regular file or folder, which a move cannot carry")
            elif not _utf8(name):
                issues.append(f"{_name(blueprint, path)}: a name that is not UTF-8, which no link can name")
            elif path.is_file():
                found.append(path)
        folders.sort()
    return found


def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _pages(blueprint: Path) -> list[Path]:
    """Every Markdown page whose links a move rewrites: below the blueprint, apart from ``links/``, read-back cards,
    the views generated at its root and hidden folders other than the implementation notes."""

    pages: list[Path] = []
    for directory, folders, files in os.walk(blueprint):
        top = Path(directory) == blueprint
        folders[:] = sorted(
            name
            for name in folders
            if not (top and name in {LINKS_DIR, READBACKS_DIR})
            and (not name.startswith(".") or (top and name == IMPLEMENTATION_NOTES_DIR))
        )
        pages.extend(
            Path(directory) / name
            for name in sorted(files)
            if name.endswith(".md")
            and not name.startswith(".")
            and not (top and name in GENERATED_VIEWS)
            and not os.path.islink(Path(directory) / name)
        )
    return pages


def _cards(blueprint: Path) -> list[Path]:
    """The read-back cards, whose links a move reports and never rewrites, since each binds its own bytes."""

    folder = blueprint / READBACKS_DIR
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.rglob("*.md") if path.is_file() and not path.is_symlink())


def _annotation_files(blueprint: Path, issues: list[str]) -> list[Path]:
    """Every annotation file the loader reads below ``links/``, defaults included."""

    folder = blueprint / LINKS_DIR
    if not folder.is_dir() or folder.is_symlink():
        return []
    found: list[Path] = []
    for directory, folders, files in os.walk(folder):
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(files):
            path = Path(directory) / name
            if not name.startswith(".") and name.lower().endswith(_ANNOTATION_SUFFIXES) and not path.is_symlink():
                found.append(path)
    return found


def _carried(
    blueprint: Path,
    source: Path,
    target: Path,
    folder: tuple[str, str] | None,
    issues: list[str],
) -> tuple[dict[Path, Path], list[tuple[str, str]]]:
    """The annotation files a folder move carries whole, everything in the folders the writer mirrors it with, and
    those folders, old to new."""

    if folder is None:
        return {}, []
    carried: dict[Path, Path] = {}
    mirrors: list[tuple[str, str]] = []
    old_relative, new_relative = source.parent.relative_to(blueprint), target.parent.relative_to(blueprint)
    for top in (blueprint / LINKS_DIR, blueprint / LINKS_DIR / writer.MATHLIB_DIR):
        mirrored, destination = top / old_relative, top / new_relative
        if not os.path.lexists(mirrored):
            continue
        if linked := [path for path in (mirrored, destination) if _linked(blueprint, path)]:
            issues.extend(_link_issue(blueprint, path) for path in linked)
            continue
        if not mirrored.is_dir():
            continue
        if os.path.lexists(destination):
            issues.append(f"{_name(blueprint, destination)}: exists, so the annotations cannot move there")
            continue
        mirrors.append((str(mirrored), str(destination)))
        for path in _files(mirrored, blueprint, issues):
            carried[path] = destination / path.relative_to(mirrored)
    return carried, mirrors


def _named_by_id(
    blueprint: Path, old: str, new: str, whole: bool, issues: list[str]
) -> tuple[dict[Path, Path], list[tuple[str, str]]]:
    """Each implementation note and read-back card named by the moved article's node ID, *old*, and so by its path,
    old to new under *new*, and the folders that move with a folder article, moved *whole*. Those named by its
    ``article_id`` stay where they are."""

    carried: dict[Path, Path] = {}
    folders: list[tuple[str, str]] = []
    notes, cards = blueprint / IMPLEMENTATION_NOTES_DIR, blueprint / READBACKS_DIR
    # Its note; its folder of cards, whole for a folder article, else only the cards in it, as its folders hold those
    # of articles below its path, which stay; and a folder article's folder of its articles' notes.
    places = [
        (notes / f"{old}.md", notes / f"{new}.md", "note"),
        (cards / old, cards / new, "folder" if whole else "cards"),
    ]
    if whole:
        places.append((notes / old, notes / new, "folder"))
    for here, there, kind in places:
        if not os.path.lexists(here):
            continue
        if linked := [path for path in (here, there) if _linked(blueprint, path)]:
            issues.extend(_link_issue(blueprint, path) for path in linked)
            continue
        if kind == "note":
            files = [here] if here.is_file() else []
            if not files:
                issues.append(f"{_name(blueprint, here)}: not a regular file, which a move cannot carry")
        elif not here.is_dir():
            continue  # no folder of notes or cards, which the readers would not read either
        elif kind == "folder":
            files = _files(here, blueprint, issues)
        else:
            files = []
            for path in sorted(here.iterdir()):
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    issues.append(f"{_name(blueprint, path)}: not a regular file or folder, which a move cannot carry")
                elif path.is_file():
                    files.append(path)
        if not files:
            continue
        # Into a folder already there, a card at a time, as it may hold the cards of articles below the new path.
        taken = [(there / path.name, path) for path in files] if kind == "cards" and there.is_dir() else [(there, here)]
        if blocked := [(new_path, old_path) for new_path, old_path in taken if os.path.lexists(new_path)]:
            issues.extend(
                f"{_name(blueprint, new_path)}: exists, so {_name(blueprint, old_path)} cannot move there"
                for new_path, old_path in blocked
            )
            continue
        if kind == "folder":
            folders.append((str(here), str(there)))
        carried.update((path, there if path == here else there / path.relative_to(here)) for path in files)
    return carried, folders


def _folders(top: Path, file: Path) -> list[Path]:
    """Each folder from *top* down to the one holding *file*, which lies below it."""

    parts = file.relative_to(top).parts[:-1]
    return [top.joinpath(*parts[:end]) for end in range(len(parts) + 1)]


def _linked(blueprint: Path, path: Path) -> bool:
    """Whether *path*, below *blueprint*, is a symbolic link or is reached through one, which the loader never
    follows below ``links/``."""

    parts = path.relative_to(blueprint).parts
    return any(blueprint.joinpath(*parts[: end + 1]).is_symlink() for end in range(len(parts)))


def _link_issue(blueprint: Path, path: Path) -> str:
    return f"{_name(blueprint, path)}: reached through a symbolic link, which a move does not follow"


def _atlas_listed(blueprint: Path, ids: Mapping[str, str]) -> list[str]:
    """The moved node IDs ``atlas.json`` lists under an area, read as the loader reads it."""

    atlas = blueprint / "atlas.json"
    try:
        payload = json.loads(atlas.read_bytes()) if atlas.is_file() and not atlas.is_symlink() else None
    except (OSError, ValueError, RecursionError):
        return []
    areas = payload.get("areas") if isinstance(payload, dict) and payload.get("schema") == ATLAS_SCHEMA else None
    if not isinstance(areas, dict):
        return []
    listed = {
        node_id
        for node_ids in areas.values()
        if isinstance(node_ids, list)
        for node_id in node_ids
        if isinstance(node_id, str)
    }
    return sorted(old_id for old_id in ids if old_id in listed)


def _drifts(
    blueprint: Path,
    node: Node,
    texts: tuple[str, str] | None,
    retarget: Callable[[str], str | None],
    moved: Mapping[str, str],
) -> bool:
    """Whether a move changes what *node*'s review approval hashes: its title, its statement as published (*texts*
    being its page before and after, if the move rewrites it), or the place of the source passage it cites."""

    if node.title is not None and retarget_links(node.title, retarget).text != node.title:
        return True
    if texts is not None and len({article_parts(published_page(text)).statement for text in texts}) > 1:
        return True
    _passage, locator = source_passage(node, blueprint)
    return locator is not None and str(blueprint.resolve() / locator.rpartition("#")[0]) in moved


def _mapped(path: str, moves: Mapping[str, str], folders: Sequence[tuple[str, str]]) -> str:
    """Where *path*, absolute and normalized, is after the move."""

    if path in moves:
        return moves[path]
    for old, new in folders:
        if path == old or path.startswith(old + os.sep):
            return new + path[len(old) :]
    return path


def _variant(path: str, folded: Mapping[str, str], folders: Sequence[tuple[str, str]]) -> str | None:
    """What moves that *path*, absolute and normalized, names only through other capitals, as a disk that ignores case
    reads it: a moved file, which *folded* gives by its path casefolded, or a path in a moved folder. None if nothing
    does, or if *path* is a file of its own, as it can be on a disk that keeps case."""

    lowered, steps = path.casefold(), path.split(os.sep)
    olds = [folded[lowered]] if lowered in folded else []
    olds += [
        os.path.join(old, *steps[old.count(os.sep) + 1 :])
        for old, _new in folders
        if lowered == old.casefold() or lowered.startswith(old.casefold() + os.sep)
    ]
    return next((old for old in olds if not os.path.lexists(path) or _same(Path(path), Path(old))), None)


def _respelled(relative: str, written: str) -> str:
    """*relative* spelled as *written* was: percent-encoded where it was, otherwise only where a Markdown destination
    needs it, where a URL would read a '#' or '?' as the start of a fragment or query, or where a table would end a
    cell at a '|'; a first segment holding ':' gets './' before it, so it reads as no scheme."""

    if "%" in written:
        return quote(relative, safe="/")
    relative = re.sub(r"[\s()<>%#?\\|]", lambda match: quote(match.group()), relative)
    return f"./{relative}" if ":" in relative.partition("/")[0] else relative


def _retargeter(
    page: Path,
    to: Path,
    moves: Mapping[str, str],
    folders: Sequence[tuple[str, str]],
    variants: dict[str, str] | None = None,
) -> Callable[[str], str | None]:
    """What a link in *page*, which moves to *to*, should name instead: the moved file it names, from its new folder.
    One that names what moves only through other capitals stays as written, and goes in *variants* with what it
    names."""

    here, there = str(page.parent), str(to.parent)
    folded = {old.casefold(): old for old in moves}

    def retarget(destination: str) -> str | None:
        head, hashed, fragment = destination.partition("#")
        written, asked, query = head.partition("?")
        # Markdown reads a backslash before punctuation as that punctuation, in a destination as anywhere.
        read = _ESCAPE.sub(r"\1", written)
        try:
            split = urlsplit(read)
        except ValueError:
            return None
        if not read or split.scheme or split.netloc or read.startswith("/") or "\\" in read:
            return None
        spelled = unquote(read)
        named = os.path.normpath(os.path.join(here, spelled))
        moved = _mapped(named, moves, folders)
        if moved == named and (variant := _variant(named, folded, folders)):
            if variants is not None:
                variants[destination] = variant
            return None
        # One that still names its file from where the page goes stays as written, unless it steps out of a folder
        # the move takes away, as `chapter/..` does, which the disk, and so check, resolves only while it is there.
        if os.path.normpath(os.path.join(there, spelled)) == moved and not _through(there, spelled, folders):
            return None
        relative = os.path.relpath(moved, there).replace(os.sep, "/")
        if written.endswith("/") and not relative.endswith("/"):
            relative += "/"
        new = f"{_respelled(relative, written)}{asked}{query}{hashed}{fragment}"
        return None if new == destination else new

    return retarget


def _through(base: str, path: str, folders: Sequence[tuple[str, str]]) -> bool:
    """Whether *path*, read from the folder *base*, steps back out of one of *folders*' old paths with a '..', as any
    path that enters one and ends outside it does: spelled so, or in other capitals that the disk reads as that folder,
    as one that ignores case does. A read-back asks the disk while the emptied folder still stands."""

    steps = path.split("/")
    spelled = (os.path.normpath(os.path.join(base, *steps[:end])) for end in range(1, len(steps)) if steps[end] == "..")
    return any(
        folder == old or (folder.casefold() == old.casefold() and _same(Path(folder), Path(old)))
        for folder in spelled
        for old, _new in folders
    )


def _rebaser(
    base: str, new_base: str, moves: Mapping[str, str], folders: Sequence[tuple[str, str]]
) -> Callable[[str], str]:
    """How an annotation reference written from the folder *base* reads from *new_base*, naming what it named moved:
    as the loader reads one, a path, percent-decoded, with its query and fragment kept as written; an absolute one
    stays absolute."""

    def rebase(uri: str) -> str:
        named = _local(uri, base)
        if named is None:
            return uri
        moved = _mapped(named, moves, folders)
        # One that still names its file from where the annotation goes stays as written.
        if _local(uri, new_base) == moved:
            return uri
        absolute = uri.startswith("/")
        head, hashed, fragment = uri.partition("#")
        written, asked, query = head.partition("?")
        relative = moved if absolute else os.path.relpath(moved, new_base).replace(os.sep, "/")
        if written.endswith("/") and not relative.endswith("/"):
            relative += "/"
        return f"{_respelled(relative, written)}{asked}{query}{hashed}{fragment}"

    return rebase


def _rebased(row: str, rebase: Callable[[str], str]) -> str:
    """*row*, one annotation, with each reference literature-links rebases rebased, every other byte kept; a row the
    loader leaves out, which literature-links cannot read, stays as it is."""

    from literature_links import companion  # on first use, as the loader imports it

    try:
        companion.loads(row)
        spans = _references(row)
    except (ValueError, RecursionError, StopIteration):
        return row
    for start, end in reversed(spans):
        reference = json.loads(row[start:end])
        if (new := rebase(reference)) != reference:
            row = row[:start] + json.dumps(new, ensure_ascii=False) + row[end:]
    return row


def _references(row: str) -> list[tuple[int, int]]:
    """Where *row*, valid JSON, writes each reference literature-links rebases: ``$schema``, and each end's ``uri``
    and ``location.profile``, where a side is one end or a list of them."""

    spans: list[tuple[int, int]] = []
    scalar = json.JSONDecoder().scan_once

    def value(index: int, path: tuple[str | int, ...]) -> int:
        """Read the value at *index*, below *path*; return where it ends."""
        index = _SPACE.match(row, index).end()
        if row.startswith("{", index):
            index = _SPACE.match(row, index + 1).end()
            while not row.startswith("}", index):
                key, index = json.decoder.scanstring(row, index + 1)
                index = _SPACE.match(row, value(_SPACE.match(row, index).end() + 1, (*path, key))).end()
                index = _SPACE.match(row, index + 1).end() if row.startswith(",", index) else index
            return index + 1
        if row.startswith("[", index):
            index, position = _SPACE.match(row, index + 1).end(), 0
            while not row.startswith("]", index):
                index = _SPACE.match(row, value(index, (*path, position))).end()
                index = _SPACE.match(row, index + 1).end() if row.startswith(",", index) else index
                position += 1
            return index + 1
        if row.startswith('"', index):
            end = json.decoder.scanstring(row, index + 1)[1]
            if _is_reference(path):
                spans.append((index, end))
            return end
        return scalar(row, index)[1]

    value(0, ())
    return spans


def _is_reference(path: tuple[str | int, ...]) -> bool:
    """Whether literature-links reads the string at *path* in an annotation as a reference."""

    if path == ("$schema",):
        return True
    if not path or path[0] not in ("subject", "object"):
        return False
    end = path[2:] if len(path) > 1 and isinstance(path[1], int) else path[1:]
    return end in (("uri",), ("location", "profile"))


def _claims(
    blueprint: Path, repo: str | None, worker_id: str | None, targets: set[str], *, dry_run: bool = False
) -> tuple[str, list[str]]:
    """Whether the claims on *targets*, node and article IDs, were checked, under the keys ``autoform claim`` takes
    them by, and what to warn about; refuses on a live claim another worker holds on one, and on a board it cannot
    read but in a *dry_run*, which warns."""

    if repo is None:
        # The origin `autoform claim` takes, its first URL; Git's messages in English tell no origin from a failure.
        origin = run_git(blueprint, "remote", "get-url", "origin", environment={"LC_ALL": "C"})
        repo, reason = os.fsdecode(origin.stdout).strip(), os.fsdecode(origin.stderr).strip()
        if origin.returncode > 0 and ("No such remote" in reason or "not a git repository" in reason):
            return "not checked", []
        if origin.returncode != 0 or not repo:
            reason = reason or f"git exited {origin.returncode}"
            raise MoveError([f"the origin remote could not be read, so claims cannot be checked: {reason}"])
        if not _repository_is_remote(repo):
            # Git reads a relative origin from the top of the working tree.
            top = run_git(blueprint, "rev-parse", "--show-toplevel")
            if top.returncode != 0:
                reason = os.fsdecode(top.stderr).strip() or f"git exited {top.returncode}"
                raise MoveError([f"the checkout could not be found, so claims cannot be checked: {reason}"])
            repo = os.path.join(os.fsdecode(top.stdout).strip(), os.path.expanduser(repo))
    keys = {author_claim_key(target): target for target in targets}
    if not keys:
        return "checked", []
    try:
        with tempfile.TemporaryDirectory(prefix="autoform-move-") as temporary:
            board = ClaimBoard(repo, worker_id or "autoform-move", Path(temporary) / "claims.git")
            leases = board.list(sorted(keys), timeout=_CLAIM_TIMEOUT_S)
    except (ClaimTransportError, ValueError, OSError) as error:
        if dry_run:
            return "not checked", [f"the claim board could not be read, so claims were not checked: {error}"]
        raise MoveError([f"the claim board could not be read, so claims cannot be checked: {error}"]) from None
    hint = "" if worker_id else ", or pass --worker-id if the claim is yours"
    held: list[str] = []
    for lease in leases:
        target = keys.get(lease["_key"], lease["_key"])
        if lease["_malformed"]:
            held.append(f"{target}: its claim cannot be read, so it may be held; move it once the claim is released")
        elif not lease["_expired"] and lease.get("owner") != worker_id:
            held.append(f"{target}: claimed by {lease.get('owner')}; move it once the claim is released{hint}")
    if held:
        raise MoveError(held)
    return "checked", []


def _keep_modes(plan: MovePlan) -> None:
    """Give each moved file the mode it had, which the writer, creating it, does not."""

    for path, to in plan.moves.items():
        try:
            os.chmod(to, plan.modes[path])
        except OSError as error:
            raise MoveError([f"{_name(plan.blueprint, Path(to))}: its mode could not be kept: {error}"]) from None


def _read_back(plan: MovePlan) -> list[str]:
    """How the graph loaded again reads otherwise than before the move, under the new names."""

    graph = load_graph(plan.blueprint)
    old = plan.graph
    problems: list[str] = []
    renamed = {plan.ids.get(node_id, node_id) for node_id in old.nodes}
    missing, extra = sorted(renamed - graph.nodes.keys()), sorted(graph.nodes.keys() - renamed)
    if missing or extra:
        problems.append(f"the articles read back otherwise: missing {missing or 'none'}, new {extra or 'none'}")
    for node in old.nodes.values():
        new_id = plan.ids.get(node.id, node.id)
        other = graph.nodes.get(new_id)
        if other is None:
            continue
        page = Path(_mapped(str(node.path), plan.moves, plan.folders))
        retarget = _retargeter(node.path, page, plan.moves, plan.folders)
        for name in _KEPT:
            if getattr(node, name) != getattr(other, name):
                problems.append(f"{new_id}: {name} reads back as {getattr(other, name)!r}, not {getattr(node, name)!r}")
        for name in _EDGES:
            if tuple(plan.ids.get(edge, edge) for edge in getattr(node, name)) != getattr(other, name):
                problems.append(f"{new_id}: its {name.replace('_', ' ')} read back otherwise")
        # As the loader reads them: each once, and a wrapped one only where no one-line link names it.
        sources = tuple(dict.fromkeys(retarget(target) or target for target in node.sources))
        wrapped = dict.fromkeys(retarget(target) or target for target in node.wrapped_sources)
        if sources != other.sources:
            problems.append(f"{new_id}: its sources read back otherwise")
        if tuple(target for target in wrapped if target not in sources) != other.wrapped_sources:
            problems.append(f"{new_id}: its wrapped sources read back otherwise")
        if node.id != plan.top[0] and plan.ids.get(node.parent, node.parent) != other.parent:
            problems.append(f"{new_id}: its parent reads back as {other.parent}, not {node.parent}")
        for name in ("title", "summary"):
            if getattr(node, name) != getattr(other, name) and not _alike_but_destinations(
                plan.texts.get(node.path), page
            ):
                problems.append(f"{new_id}: its {name} reads back otherwise")
    if plan.annotations:
        links = read_links(plan.blueprint, {node.path: node.id for node in graph.nodes.values()})
        for page, names in sorted(plan.annotations.items()):
            read = links.files.get(page, ()) if links is not None else ()
            if unread := [name for name in names if name not in read]:
                problems.append(f"{', '.join(unread)}: not read as its article's annotation once moved")
    return problems


def _alike_but_destinations(before: str | None, page: Path) -> bool:
    """Whether *page*, written by the move, reads as its text *before* but for link destinations, as the loose reading
    finds them. Its title and summary then differ only where the loader keeps a destination or part of one, as for a
    wrapped link, or the end of one in angle brackets past a ')' it holds, and in where a long summary's clip falls."""

    if before is None:
        return False
    try:
        after = page.read_bytes().decode("utf-8", "surrogateescape")
    except OSError:
        return False
    return _without_destinations(before) == _without_destinations(after)


def _without_destinations(text: str) -> str:
    """*text*, with its line breaks as the site writes them and each destination the loose reading finds left out."""

    lines = published_lines(text)
    pieces: list[str] = []
    end = 0
    for match in _DESTINATION.finditer(lines):
        pieces.append(lines[end : match.start(match.lastindex)])
        end = match.end(match.lastindex)
    return "".join(pieces) + lines[end:]


def _remove_emptied(plan: MovePlan) -> None:
    """Remove the folders the move left empty, innermost first, below the blueprint; one holding anything stays."""

    folders = {Path(path).parent for path in plan.moves}
    # A moved folder's own subfolders, and its mirrors', even those that held no file.
    for old, _new in plan.folders:
        folders.update(Path(directory) for directory, _, _ in os.walk(old))
    for folder in sorted(folders, key=lambda path: len(path.parts), reverse=True):
        while folder != plan.blueprint and folder.is_relative_to(plan.blueprint):
            try:
                folder.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                break
            folder = folder.parent


def _origins(plan: MovePlan) -> dict[Path, Path]:
    """Each moved file's new path, to the old one the move removes."""
    return {Path(new): Path(old) for old, new in plan.moves.items() if plan.changes.get(Path(old), b"") is None}


def _undone(
    plan: MovePlan, steps: Sequence[writer.Written], issues: Sequence[str], finished: set[writer.Written]
) -> MoveError:
    """The error for a write that could not stand, after *issues*, once its *steps* are undone but those in
    *finished*. The writer undoes them in one pass, old paths first, and keeps a moved file's new copy while its old
    path cannot take the file back, so no file is left nowhere; interrupted again, it goes over them once more, and
    the new interrupt says what the error would."""

    try:
        writer.undo(steps, finished)
    except writer.AnnotationWriteError as error:
        return _stuck(plan, steps, finished, issues, error.issues)
    except BaseException as interrupt:
        interrupt.__notes__ = _notes(_stuck(plan, steps, finished, issues, getattr(interrupt, "__notes__", ())))
        raise
    return _stuck(plan, steps, finished, issues, ())


def _notes(error: MoveError) -> list[str]:
    """What *error* says, as an interrupt notes it: its issues, then each file it left."""

    return [*error.issues, *(f"could not restore {name}" for name in error.left)]


def _stuck(
    plan: MovePlan,
    steps: Sequence[writer.Written],
    finished: set[writer.Written],
    issues: Sequence[str],
    notes: Sequence[str],
) -> MoveError:
    """The error naming, after *issues*, each file the undo of *steps* left otherwise than it was, why as the writer's
    *notes* say, or that nothing moved. A step is undone once in *finished*, or once its file holds what it held
    before, as when an interrupt stopped the undo just after it. A new copy kept for an old path that could not take
    its file back holds the file, so it is said to be kept, not left, and one an undo removed all the same is said to
    be lost. Each file put back, and each copy kept, gets the mode its file had, which the writer does not keep;
    interrupted at that, the interrupt says what the error would."""

    origins = _origins(plan)
    copies = {step.path for step in steps if step.previous is None and step.path in origins}
    done = {step for step in steps if step in finished or _holds(step.path, step.previous)}
    stuck = {step.path for step in steps if step not in done and step.path not in copies}
    left: list[str] = []
    said: list[str] = []
    modes: list[tuple[Path, int, str]] = []
    for step in steps:
        name, was = _name(plan.blueprint, step.path), origins.get(step.path)
        old = _name(plan.blueprint, was) if step.path in copies and was is not None else None
        why = next((note.partition(": not undone: ")[2] for note in notes if note.startswith(f"{step.path}: ")), "")
        mode = plan.modes.get(str(step.path)) if step.sha256 is None else None  # a file put back where it was
        if old is not None and was in stuck:
            if step in done:
                said.append(f"{name}: removed, though {old} could not be restored, so it is lost")
                continue
            said.append(f"{name}: kept, since {old} could not be restored")
            mode = plan.modes.get(str(was))
        elif step not in done:
            left.append(name)
            undone = "restored" if old is None else f"removed, beside {old}"
            said.append(f"{name}: could not be {undone}: {why or 'interrupted'}")
            continue
        if mode is not None:
            modes.append((step.path, mode, name))
    unkept: list[str] = []
    try:
        for path, mode, name in modes:
            try:
                os.chmod(path, mode)
            except OSError as error:
                unkept.append(f"{name}: left without its mode: {error}")
    except BaseException as interrupt:
        stopped = "interrupted while restoring modes; some files may have the default mode"
        interrupt.__notes__ = _notes(_said(issues, said, [*unkept, stopped], left))
        raise
    return _said(issues, said, unkept, left)


def _said(issues: Sequence[str], said: Sequence[str], modes: Sequence[str], left: Sequence[str]) -> MoveError:
    """The error giving *issues*, then *said* and *modes*, with the files *left*, or that nothing moved."""

    if said:
        return MoveError([*issues, *said, *modes], left)
    return MoveError([*issues, *modes, "nothing moved"])


def _holds(path: Path, previous: bytes | None) -> bool:
    """Whether *path* holds *previous*, or nothing where that is None."""

    try:
        return writer._read(path) == previous
    except OSError:
        return False


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _name(blueprint: Path, path: Path) -> str:
    return path.relative_to(blueprint).as_posix()


__all__ = ["MOVE_PLAN_SCHEMA", "MoveError", "MovePlan", "MoveResult", "apply_move", "plan_move"]
