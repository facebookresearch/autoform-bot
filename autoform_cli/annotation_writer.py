"""Write articles' facts as the literature-links annotations :mod:`autoform_cli.annotations` reads.

``migrate links`` writes through this module, and ``links set`` will; nothing else writes these files. A page
``<folder>/<rel>.md`` below the blueprint is annotated in ``links/<folder>/<rel>.jsonl``, one record holding every
fact, and, when it names Mathlib declarations, in ``links/mathlib/<folder>/<rel>.jsonl``, one record of Mathlib ends.
Folder defaults carry what every end shares: ``links/<folder>/_defaults.json`` names the project, and
``links/mathlib/_defaults.json`` Mathlib, so an end is only its declaration. The two per-article files are the
writer's whole; the templates are written only when absent.
What a record means depends on every defaults file above it, so a caller reloads the graph after writing and
compares (:func:`differences`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ._fs import atomic_write
from .annotations import LINKS_DIR, MATHLIB, NAMESPACE, SETTINGS
from .graph import _FRONTMATTER_KEYS, GraphValidationError, Node, _parse_frontmatter
from .lean import declaration_names
from .status import DEFINITION_DECLARATIONS, declaration_command

#: Below ``links/``, the folder of Mathlib ends, whose defaults name Mathlib.
MATHLIB_DIR = "mathlib"
DEFAULTS = "_defaults.json"
#: The project from ``links/<folder>/``: up through the folder, ``links/`` and the blueprint.
PROJECT_TEMPLATE = {"object": {"type": "lean", "uri": "../../.."}}
#: Mathlib, with no revision: the loader reads none, and nothing would keep one current.
MATHLIB_TEMPLATE = {"object": {"type": "lean", "uri": MATHLIB}}
#: Facts an annotation carries under ``extensions["org.autoform"]``, the flags among them as JSON booleans.
EXTENSION_KEYS = ("area", "catalog", "discussion", "mathlib", "navigation", "not_ready", "origin", "review_approved")
_FLAGS = frozenset({"mathlib", "not_ready"})
#: Every fact the loader reads from frontmatter: what stripping removes, so what a write must record or refuse.
FACT_KEYS = _FRONTMATTER_KEYS - SETTINGS
_RECORDED = frozenset(
    {"article_id", "declaration", "lean", "statement", "proof", "mathlib_declaration", "mathlib_file", *EXTENSION_KEYS}
)
_TRUE = frozenset({"true", "yes"})
#: A module path as ``mathlib_file`` writes one, which reads back the same through ``location.module``.
_MODULE_FILE = re.compile(r"[^./\\\s]+(?:/[^./\\\s]+)*\.lean")


@dataclass(frozen=True, slots=True)
class AnnotationPaths:
    """Where the writer keeps one page's annotations, and the templates they rely on."""

    own: Path
    mathlib: Path
    own_template: Path
    mathlib_template: Path


def annotation_paths(blueprint: Path, page: Path) -> AnnotationPaths:
    """The files for *page*, a Markdown page below *blueprint*, both paths alike resolved or not."""

    relative = page.relative_to(blueprint)
    if relative.suffix != ".md" or len(relative.parts) < 2 or relative.parts[0] in {LINKS_DIR, MATHLIB_DIR}:
        raise ValueError(f"{relative.as_posix()}: not a page the writer annotates")
    stem = relative.with_suffix(".jsonl")
    links = blueprint / LINKS_DIR
    return AnnotationPaths(
        own=links / stem,
        mathlib=links / MATHLIB_DIR / stem,
        own_template=links / relative.parts[0] / DEFAULTS,
        mathlib_template=links / MATHLIB_DIR / DEFAULTS,
    )


class Unencodable(ValueError):
    """Facts no annotation can hold as frontmatter held them; ``reasons`` says which."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


def encode(blueprint: Path, page: Path, facts: Mapping[str, str]) -> dict[Path, bytes | None]:
    """Every writer-owned file's new bytes for *page*'s *facts*, frontmatter keys to values as the loader reads them:
    its own record, its Mathlib record or None to remove one, and each template it needs that does not exist yet."""

    paths = annotation_paths(blueprint, page)
    facts = {key: value for key, value in facts.items() if key not in SETTINGS}
    command = declaration_command(facts.get("declaration")).casefold()
    names = list(dict.fromkeys(declaration_names(facts.get("lean", ""))))
    mathlib_names = list(dict.fromkeys(declaration_names(facts.get("mathlib_declaration", ""))))
    status = _status(facts.get("statement"), facts.get("proof"), command)
    # A fact the loader reads but no annotation records would be stripped and never written.
    reasons = [f"{key}, which the writer does not record" for key in sorted(facts.keys() - _RECORDED)]
    if facts.get("proof") == "formalized" and command == "axiom":
        reasons.append("proof: formalized on an axiom, which no annotation records: it would stop counting as proved")
    if status is not None and not names:
        reasons.append(f"statement: {facts['statement']} without a lean: name, which an annotation's status needs")
    module = None
    if "mathlib_file" in facts:
        if not mathlib_names:
            reasons.append("mathlib_file without mathlib_declaration, whose first Mathlib end would carry the module")
        elif not _MODULE_FILE.fullmatch(facts["mathlib_file"]):
            reasons.append(f"mathlib_file {facts['mathlib_file']!r}, which is not a module's file")
        else:
            module = facts["mathlib_file"].removesuffix(".lean").replace("/", ".")
    if reasons:
        raise Unencodable(reasons)

    # Credit goes to the end with role states: the first name, as frontmatter credits it. A module catalog's status
    # covers every name it inventories, so none has a role.
    states = facts.get("catalog") != "module"
    own: dict[str, object] = {"subject": _subject(page, paths.own)}
    own["object"] = [
        {"location": {"declaration": name}, **({"role": "states"} if states and not index else {})}
        for index, name in enumerate(names)
    ]
    if status is not None:
        own["status"] = status
    if "declaration" in facts:
        own["kind"] = facts["declaration"]
    if "article_id" in facts:
        own["id"] = facts["article_id"]
    ours: dict[str, object] = {}
    for key in EXTENSION_KEYS:
        if key in _FLAGS and key in facts:
            if facts[key].casefold() in _TRUE:  # false is the absence of the flag
                ours[key] = True
        elif key in facts:
            ours[key] = facts[key]
    if ours:
        own["extensions"] = {NAMESPACE: ours}
    changes: dict[Path, bytes | None] = {paths.own: dumps(own), paths.mathlib: None}
    if mathlib_names:
        ends = [
            {"location": {"declaration": name, **({"module": module} if module and not index else {})}}
            for index, name in enumerate(mathlib_names)
        ]
        record: dict[str, object] = {"subject": _subject(page, paths.mathlib), "object": ends}
        if "article_id" in facts:
            record["id"] = facts["article_id"]
        changes[paths.mathlib] = dumps(record)
    templates = [(paths.own_template, PROJECT_TEMPLATE)]
    if mathlib_names:
        templates.append((paths.mathlib_template, MATHLIB_TEMPLATE))
    for path, template in templates:
        if not os.path.lexists(path):
            changes[path] = dumps(template)
    return changes


def _status(statement: str | None, proof: str | None, command: str) -> str | None:
    """The ``status`` the loader reads back as *statement* and *proof*: one value, ``formalized``, for a proved
    theorem and for a stated definition or axiom, neither of which records a proof."""

    if statement == "retracted":
        return "retracted"
    if statement != "formalized":
        return None
    if proof == "formalized" or command in DEFINITION_DECLARATIONS or command == "axiom":
        return "formalized"
    return "stated"


def _subject(page: Path, file: Path) -> list[dict[str, str]]:
    """The page as a subject end, by a relative reference from *file*, in full: an end with its own ``uri`` takes
    nothing from a template."""
    return [{"type": "generic", "uri": quote(Path(os.path.relpath(page, file.parent)).as_posix())}]


def dumps(value: Mapping[str, object]) -> bytes:
    """One line of JSON, keys in the order given, ending in a newline: the only bytes the writer writes."""
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(", ", ": ")) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class Written:
    """One step a write took: the bytes *path* held before, None if the write created it; the SHA-256 of what it
    left, None if it removed the file; and the folders it made for it, outermost first."""

    path: Path
    previous: bytes | None
    sha256: str | None
    folders: tuple[Path, ...] = ()


class AnnotationWriteError(GraphValidationError):
    """A write stopped; ``written`` holds the steps it had already taken."""

    def __init__(self, issues: Sequence[str], written: Sequence[Written] = ()) -> None:
        super().__init__(list(issues))
        self.written = tuple(written)


def write(changes: Mapping[Path, bytes | None], expected: Mapping[Path, str | None]) -> tuple[Written, ...]:
    """Apply *changes*, new bytes or None to remove, each only while its file still holds what the caller decided
    from: the SHA-256 in *expected*, or no file where that is None or missing. Folders are made as needed."""

    done: list[Written] = []
    for path, data in changes.items():
        want = expected.get(path)
        try:
            previous = _read(path)
            if _digest(previous) != want:
                raise OSError("it changed after it was read; run the command again")
            if data is None:
                if previous is not None:
                    _remove_if(path, want)
                    done.append(Written(path, previous, None))
                continue
            folders = _missing_folders(path)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                mode = _new_file_mode() if previous is None else os.stat(path).st_mode & 0o7777
                atomic_write(path, data, mode=mode, check=lambda path=path, want=want: _check(path, want))
            except BaseException:
                _remove_folders(folders)
                raise
            done.append(Written(path, previous, hashlib.sha256(data).hexdigest(), folders))
        except OSError as error:
            raise AnnotationWriteError([f"{path}: not written: {error}"], done) from None
        except BaseException:
            # Interrupted, as by Ctrl-C: nothing half-written stays to be read.
            with contextlib.suppress(AnnotationWriteError):
                undo(done)
            raise
    return tuple(done)


def undo(written: Sequence[Written]) -> None:
    """Undo *written*, last step first, each only while its file still holds what the write left. Raises
    :class:`AnnotationWriteError` carrying the steps still in effect when any could not be undone."""

    issues: list[str] = []
    left: list[Written] = []
    for step in reversed(written):
        try:
            if step.previous is None:
                _remove_if(step.path, step.sha256)
                _remove_folders(step.folders)
            else:
                mode = os.stat(step.path).st_mode & 0o7777 if step.sha256 is not None else _new_file_mode()
                atomic_write(
                    step.path, step.previous, mode=mode, check=lambda step=step: _check(step.path, step.sha256)
                )
        except OSError as error:
            issues.append(f"{step.path}: not undone: {error}")
            left.append(step)
    if issues:
        raise AnnotationWriteError(issues, left[::-1])


def _missing_folders(path: Path) -> tuple[Path, ...]:
    """The folders above *path* that do not exist yet, outermost first."""
    missing: list[Path] = []
    for folder in path.parents:
        if os.path.lexists(folder):
            break
        missing.append(folder)
    return tuple(reversed(missing))


def _remove_folders(folders: Sequence[Path]) -> None:
    """Remove *folders*, innermost first, skipping any never made, until one is not empty: an empty ``links/`` would
    still be read."""
    for folder in reversed(folders):
        try:
            folder.rmdir()
        except FileNotFoundError:
            continue
        except OSError:
            return


def _read(path: Path) -> bytes | None:
    """A regular file's bytes, or None where nothing is; anything else there, a link included, raises."""
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise OSError("not a regular file")
    return path.read_bytes()


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _check(path: Path, want: str | None) -> None:
    if _digest(_read(path)) != want:
        raise OSError("it changed while it was written; run the command again")


def _remove_if(path: Path, want: str | None) -> None:
    _check(path, want)
    path.unlink()


def _new_file_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def frontmatter_facts(node_id: str, text: str) -> dict[str, str]:
    """An article's frontmatter facts, read as the loader reads them, settings left out."""
    metadata, _start, _issues = _parse_frontmatter(node_id, text.splitlines())
    return {key: value for key, value in metadata.items() if key not in SETTINGS}


def strip_facts(content: bytes) -> bytes:
    """*content* without its frontmatter facts. Every other line stays, settings, comments and keys the loader
    ignores beside an annotation among them, each with its own ending; a block left with none of them goes, with
    one blank line after it, unless the body would then start with ``---`` and open a block of its own."""

    lines = content.decode("utf-8").splitlines(keepends=True)
    if (end := _block_end(lines)) is None:
        return content
    kept = [line for line in lines[1:end] if not _is_fact(line)]
    rest = lines[end + 1 :]
    body = rest[1:] if rest and not rest[0].strip() else rest
    if any(line.strip() for line in kept) or (body and body[0].strip() == "---"):
        return "".join([lines[0], *kept, *lines[end:]]).encode("utf-8")
    return "".join(body).encode("utf-8")


def fact_keys(content: bytes) -> set[str]:
    """The keys of the frontmatter lines :func:`strip_facts` removes, read or not."""

    lines = content.decode("utf-8").splitlines(keepends=True)
    end = _block_end(lines) or 0
    return {_key(line) for line in lines[1:end] if _is_fact(line)}


def _block_end(lines: list[str]) -> int | None:
    """The index of the line closing the frontmatter block *lines* open, or None without one."""
    if not lines or lines[0].strip() != "---":
        return None
    return next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)


def _key(line: str) -> str:
    return line.strip().split(":", 1)[0].strip()


def _is_fact(line: str) -> bool:
    return not line.strip().startswith("#") and _key(line) in FACT_KEYS


def node_facts(node: Node) -> dict[str, str]:
    """The frontmatter keys a loaded *node* holds, as text. Its ``area`` may come from ``atlas.json``, which assigns
    one to an article that records none, so what the article's own annotations say is ``read_links``' ``facts``."""

    facts = {
        key: value
        for key in (
            "lean",
            "declaration",
            "catalog",
            "mathlib_declaration",
            "mathlib_file",
            "article_id",
            "origin",
            "discussion",
            "review_approved",
            "area",
            "navigation",
        )
        if (value := getattr(node, key)) is not None
    }
    if node.statement_formalized:
        facts["statement"] = "formalized"
    elif node.statement_retracted:
        facts["statement"] = "retracted"
    if node.proof_formalized:
        facts["proof"] = "formalized"
    if node.mathlib:
        facts["mathlib"] = "true"
    if node.not_ready:
        facts["not_ready"] = "true"
    return facts


def canon(facts: Mapping[str, str]) -> dict[str, object]:
    """*facts* as the round trip compares them. The accepted differences: ``proof: formalized`` on a definition,
    which no annotation records and status never needed; ``lean:`` and ``mathlib_declaration`` as their names, so
    text rewritten with the same names, and a name repeated, compare alike; a flag as true or absent."""

    out: dict[str, object] = {}
    for key, value in facts.items():
        if key in SETTINGS:
            continue
        if key in {"lean", "mathlib_declaration"}:
            if names := list(dict.fromkeys(declaration_names(value))):
                out[key] = names
        elif key in _FLAGS:
            if value.casefold() in _TRUE:
                out[key] = True
        else:
            out[key] = value
    if declaration_command(facts.get("declaration")).casefold() in DEFINITION_DECLARATIONS:
        out.pop("proof", None)
    return out


def differences(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """How the facts *after* read otherwise than *before*, under :func:`canon`: one line per key with both values."""

    old, new = canon(before), canon(after)
    return [
        f"{key}: {_shown(old.get(key))} reads back as {_shown(new.get(key))}"
        for key in sorted(old.keys() | new.keys())
        if old.get(key) != new.get(key)
    ]


def _shown(value: object) -> str:
    if value is None:
        return "nothing"
    if value is True:
        return "true"
    return ", ".join(value) if isinstance(value, list) else str(value)


__all__ = [
    "DEFAULTS",
    "EXTENSION_KEYS",
    "FACT_KEYS",
    "MATHLIB_DIR",
    "MATHLIB_TEMPLATE",
    "PROJECT_TEMPLATE",
    "AnnotationPaths",
    "AnnotationWriteError",
    "Unencodable",
    "Written",
    "annotation_paths",
    "canon",
    "differences",
    "dumps",
    "encode",
    "fact_keys",
    "frontmatter_facts",
    "node_facts",
    "strip_facts",
    "undo",
    "write",
]
