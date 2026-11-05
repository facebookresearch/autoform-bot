"""Read articles' facts from the literature-links annotations in ``blueprint/links/``.

An annotation (literature-links, docs/companion.md) whose subject is a roadmap
article carries what that article's frontmatter carried, and
:func:`~autoform_cli.graph.load_graph` reads it in the frontmatter's place
(reference/links.md). Nothing here refuses a vault (#325): a file, a line or a
value that cannot be read is left out, and every fact left out is warned about,
counted, with the first few places.

Files are read as literature-links' discovery reads them, with its parser and
its folder-defaults merge, but not its validation, which on a large wiki costs
a third of loading the graph. Loading checks the fields it reads, as it does
frontmatter, and ``check`` adds literature-links' validation (:func:`scan`).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .lean import declaration_names
from .library.pinned_names import _printable
from .status import DEFINITION_DECLARATIONS, declaration_command

#: The folder below the blueprint root that holds the annotations.
LINKS_DIR = "links"
#: Frontmatter that stays beside an annotation: project and folder settings, not facts about an article.
SETTINGS = frozenset({"open_statements", "tracks_lean", "published", "coverage_list_limit"})
#: Where Autoform's own keys go in an annotation's ``extensions``.
NAMESPACE = "org.autoform"
#: The reason for a value left out because it is not one frontmatter could hold; graph.py adds its own to it.
REJECTED = "annotation values Autoform does not accept are left out"
_EXTENSION_KEYS = frozenset(
    {"origin", "not_ready", "discussion", "review_approved", "area", "catalog", "navigation", "mathlib"}
)
#: Extension keys that hold a JSON boolean.
_FLAGS = frozenset({"not_ready", "mathlib"})
_STATUSES = frozenset({"formalized", "stated", "retracted"})
_END_TYPES = frozenset({"pdf", "latex", "html", "lean", "generic"})
#: What ``catalog`` cannot sit beside, as graph's _CATALOG_DECLARATION_KEYS.
_DECLARATION_KEYS = frozenset({"declaration", "mathlib", "mathlib_declaration", "mathlib_file"})
_SEVERAL = "an article's annotations give one key several values, so the first is kept"
_DEFAULTS = "_defaults.json"
_LINES = (".jsonl", ".ndjson")
_SUFFIXES = (".json", *_LINES)
#: How many places a counted warning names.
_SHOWN = 5


class Drops:
    """What loading leaves out, or reads beside ``blueprint/links/``, by reason, in the order the reasons came up."""

    def __init__(self) -> None:
        self.places: dict[str, list[str]] = {}

    def add(self, reason: str, place: str) -> None:
        self.places.setdefault(reason, []).append(place)

    def warnings(self) -> list[str]:
        """One warning for each reason, counting its places and naming the first few, with nothing a file wrote
        able to forge a line."""
        return [
            _printable(f"{reason} ({len(places)}): {', '.join(places[:_SHOWN])}")
            + (f" and {len(places) - _SHOWN} more" if len(places) > _SHOWN else "")
            for reason, places in self.places.items()
        ]


@dataclass(frozen=True, slots=True)
class Links:
    """What ``blueprint/links/`` says about the articles."""

    #: Each annotated article's facts by its resolved path, under frontmatter keys, as text. An article with an
    #: annotation whose facts were all left out maps to an empty dict.
    facts: dict[Path, dict[str, str]]
    #: What was left out, which the graph adds to.
    drops: Drops
    #: A digest of every file in the folder, for the revisions built on the graph.
    sha256: str


def links_exist(blueprint: Path) -> bool:
    """Whether the blueprint holds an entry named exactly ``links``; a case-insensitive disk finds ``Links`` too."""
    try:
        return LINKS_DIR in os.listdir(blueprint)
    except OSError:
        return False


def read_links(blueprint: Path, articles: dict[Path, str]) -> Links | None:
    """Read ``blueprint/links/`` for *articles*, each article's resolved path mapped to its node ID.

    None when there is no such folder.
    """
    drops = Drops()
    folder = blueprint / LINKS_DIR
    if not links_exist(blueprint):
        try:
            spellings = sorted(name for name in os.listdir(blueprint) if name.casefold() == LINKS_DIR)
        except OSError:
            spellings = []
        if not spellings:
            return None
        # Read on a disk that ignores case and not on one that doesn't, it is read on neither (#595).
        drops.add(
            f"a folder spelled other than {LINKS_DIR} is not read; rename it (in Git: git mv through a temporary name)",
            spellings[0],
        )
        return Links({}, drops, hashlib.sha256(b"").hexdigest())
    if os.path.islink(folder) or not folder.is_dir():
        drops.add(f"blueprint/{LINKS_DIR} is not a folder, so no annotation is read", LINKS_DIR)
        return Links({}, drops, hashlib.sha256(b"").hexdigest())
    contents, broken = _walk(folder, drops)
    digest = hashlib.sha256(b"autoform-links/v1\0")
    for relative, data in sorted(contents.items()):
        for part in (relative.encode("utf-8", "surrogateescape"), data):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)

    # Paths are compared as strings, which on a large wiki is much faster than as Path objects.
    project = os.path.normpath(blueprint.parent)
    spelled = {str(path): path for path in articles}
    grouped: dict[Path, list[tuple[dict[str, Any], str]]] = {}
    for annotation, place, base in _annotations(folder, contents, broken, drops):
        subjects = annotation.get("subject")
        ends = subjects if isinstance(subjects, list) else []
        targets = {_local(end.get("uri"), base) for end in ends if isinstance(end, dict)} - {None}
        hits = [spelled[target] for target in targets if target in spelled]
        if len(hits) == 1:
            grouped.setdefault(hits[0], []).append((annotation, base))
        else:
            drops.add(f"annotations about {'several articles' if hits else 'no roadmap article'} are left out", place)
    facts = {
        path: _facts(articles[path], entries, project, drops)
        for path, entries in sorted(grouped.items(), key=lambda item: articles[item[0]])
    }
    return Links(facts, drops, digest.hexdigest())


def scan(blueprint: Path) -> list[str]:
    """literature-links' own report on ``blueprint/links/``, for ``check``: what its validation rejects."""
    folder = blueprint / LINKS_DIR
    if not links_exist(blueprint) or os.path.islink(folder) or not folder.is_dir():
        return []
    from literature_links import discovery

    problems = Drops()
    for problem in discovery.collect([folder], root=folder, exclude=["**/.*"]).problems:
        # A folder of defaults alone holds no annotation yet, which is no problem here.
        if problem.code != "NO_ANNOTATIONS":
            problems.add(f"literature-links: {_relative(problem.message, folder)}", _relative(problem.path, folder))
    return problems.warnings()


def _relative(text: str, folder: Path) -> str:
    """*text* with *folder*'s absolute path written as the folder's name, and a line number after a colon."""
    return re.sub(r" \(line (\d+)\)", r":\1", text.replace(str(folder), LINKS_DIR))


def _walk(folder: Path, drops: Drops) -> tuple[dict[str, bytes], set[str]]:
    """The bytes of every regular file below *folder* that is not hidden, by its path there, and the folders, by the
    same paths, whose ``_defaults.json`` cannot be used.

    Symbolic links are never followed, as discovery never follows them while walking.
    """
    contents: dict[str, bytes] = {}
    broken: set[str] = set()
    top = len(str(folder)) + 1
    pending = [str(folder)]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError as error:
            where = f"{LINKS_DIR}/{directory[top:]}".replace(os.sep, "/")
            drops.add("folders in blueprint/links/ that cannot be listed are left out", f"{where} ({error.strerror})")
            continue
        for entry in children:
            if entry.name.startswith("."):
                continue
            relative = entry.path[top:].replace(os.sep, "/")
            place = f"{LINKS_DIR}/{relative}"
            try:
                regular = entry.is_file(follow_symlinks=False)
                if entry.name == _DEFAULTS and (entry.is_symlink() or not regular):
                    broken.add(directory[top:].replace(os.sep, "/"))
                if entry.is_symlink():
                    drops.add("symbolic links in blueprint/links/ are not followed", place)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif regular:
                    with open(entry.path, "rb") as handle:
                        contents[relative] = handle.read()
                elif entry.name.lower().endswith(_SUFFIXES):
                    drops.add("files in blueprint/links/ that are not regular files are left out", place)
            except OSError as error:
                if entry.name == _DEFAULTS:
                    broken.add(directory[top:].replace(os.sep, "/"))
                drops.add("files in blueprint/links/ that cannot be read are left out", f"{place} ({error.strerror})")
    return contents, broken


def _annotations(
    folder: Path, contents: dict[str, bytes], broken: set[str], drops: Drops
) -> list[tuple[dict[str, Any], str, str]]:
    """Each annotation in *contents*, with its folder defaults applied as discovery applies them:
    ``(annotation, place, the folder its relative references start from)``."""
    from literature_links import companion

    defaults: dict[str, dict[str, Any] | None] = dict.fromkeys(broken)
    for relative, data in contents.items():
        parent, _, name = relative.rpartition("/")
        if name == _DEFAULTS:
            try:
                value = companion.loads(data.decode("utf-8"))
            except (UnicodeError, companion.LoadError) as error:
                value = error
            defaults[parent] = value if isinstance(value, dict) else None
            if defaults[parent] is None:
                drops.add("defaults files that do not hold one JSON object are left out", f"{LINKS_DIR}/{relative}")
    merged: dict[str, dict[str, Any] | None] = {}

    def inherited(parent: str) -> dict[str, Any] | None:
        """The defaults merged for a file in *parent*, outermost first; None if one cannot be used."""
        if parent not in merged:
            parts = parent.split("/") if parent else []
            result: dict[str, Any] | None = {}
            for depth in range(len(parts) + 1):
                value = defaults.get(here := "/".join(parts[:depth]), {})
                if value is None:
                    result = None
                    break
                try:
                    if here in defaults:
                        rebased = companion.rebase(value, "../" * (len(parts) - depth) + _DEFAULTS)
                        result = companion.merge(result, rebased, defaults=True)
                except RecursionError:  # nested deeper than literature-links merges, as collect reports
                    result = None
                    break
            merged[parent] = result
        return merged[parent]

    rows: list[tuple[dict[str, Any], str, str]] = []
    for relative, data in sorted(contents.items()):
        parent, _, name = relative.rpartition("/")
        if name == _DEFAULTS or not name.lower().endswith(_SUFFIXES):
            continue
        place = f"{LINKS_DIR}/{relative}"
        template = inherited(parent)
        if template is None:
            drops.add("annotation files below a defaults file that cannot be used are left out", place)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            drops.add("annotation files that are not UTF-8 are left out", place)
            continue
        base = os.path.dirname(os.path.join(folder, relative))
        # CRLF, CR and LF end lines, as discovery reads them; a JSON file holds one annotation.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = enumerate(text.split("\n"), 1) if name.lower().endswith(_LINES) else [(None, text)]
        for line, row in lines:
            if line is not None and not row.strip(" \t"):
                continue
            where = place if line is None else f"{place}:{line}"
            try:
                annotation = companion.loads(row)
                if template and isinstance(annotation, dict):
                    annotation = companion.merge(template, annotation)
            except companion.LoadError as error:
                drops.add("annotations that are not JSON are left out", f"{where} ({error})")
                continue
            except RecursionError:
                drops.add("annotations nested too deeply to read are left out", where)
                continue
            if not isinstance(annotation, dict):
                drops.add("annotations that are not JSON objects are left out", where)
                continue
            rows.append((annotation, where, base))
    return rows


def _local(uri: Any, base: str) -> str | None:
    """The path a relative reference names from the folder *base*, percent-decoded; None for any other value."""
    if not isinstance(uri, str) or not uri:
        return None
    try:
        split = urlsplit(uri)
    except ValueError:
        return None
    if split.scheme or split.netloc or not split.path:
        return None
    return os.path.normpath(os.path.join(base, unquote(split.path)))


def _facts(node_id: str, entries: list[tuple[dict[str, Any], str]], project: str, drops: Drops) -> dict[str, str]:
    """The frontmatter one article's annotations amount to, leaving out, with a warning, what cannot hold."""
    facts: dict[str, str] = {}
    own: list[tuple[str, Any]] = []
    mathlib_names: list[str] = []

    def text(value: Any, field: str, *, flag: bool = False, number: bool = False) -> str | None:
        # As frontmatter could hold it: a flag is a JSON boolean, and nothing breaks a line.
        if flag:
            written = ("true" if value else "false") if isinstance(value, bool) else None
        elif isinstance(value, str) or (number and type(value) is int):
            written = str(value) if str(value).isprintable() and str(value) else None
        else:
            written = None
        if written is None:
            drops.add(REJECTED, f"{node_id} ({field})")
        return written

    def put(key: str, value: Any, field: str) -> None:
        written = text(value, field, flag=key in _FLAGS, number=key == "discussion")
        if written is not None and facts.setdefault(key, written) != written:
            drops.add(_SEVERAL, f"{node_id} ({field})")

    for annotation, base in entries:
        extensions = annotation.get("extensions", {})
        ours = extensions.get(NAMESPACE, {}) if isinstance(extensions, dict) else None
        if not isinstance(ours, dict):
            drops.add(REJECTED, f"{node_id} (extensions)")
            ours = {}
        for key, value in ours.items():
            if key in _EXTENSION_KEYS:
                put(key, value, key)
            elif key in SETTINGS:
                drops.add(
                    "settings stay in README frontmatter, so in an annotation they are left out", f"{node_id} ({key})"
                )
            else:
                drops.add(f"unknown {NAMESPACE} keys are left out", f"{node_id} ({key})")
        for field, key in (("kind", "declaration"), ("status", "status"), ("id", "article_id")):
            # Only an ID shaped as Autoform's is one; any other names the subject for literature-links alone.
            if field in annotation and (field != "id" or str(annotation[field]).startswith("af_")):
                put(key, annotation[field], field)
        objects = annotation.get("object", [])
        if not isinstance(objects, list):
            drops.add(REJECTED, f"{node_id} (object)")
            objects = []
        for end in objects:
            if not isinstance(end, dict) or not isinstance(end.get("type"), str) or end["type"] not in _END_TYPES:
                drops.add(REJECTED, f"{node_id} (object type)")
                continue
            location = end.get("location")
            if end["type"] != "lean" or location is None:
                continue  # without a location, a whole project: the defaults make an end the annotation leaves out
            name = location.get("declaration") if isinstance(location, dict) else None
            if name is None:
                drops.add("Lean ends that name no declaration are left out", node_id)
            elif text(name, "location.declaration") is None:
                continue
            elif _local(end.get("uri"), base) == project:
                own.append((name, end.get("role")))
            elif "mathlib" in ours:
                mathlib_names.append(name)
                module = location.get("module")
                if module is not None and (written := text(module, "location.module")):
                    # Frontmatter holds one file, the first name's.
                    path = written.replace(".", "/") + ".lean"
                    if facts.setdefault("mathlib_file", path) != path:
                        drops.add(_SEVERAL, f"{node_id} (location.module)")
            else:
                drops.add(
                    f"Lean ends naming another project are left out, unless their annotation sets {NAMESPACE} mathlib",
                    node_id,
                )

    # The declaration that states the article takes its credit: the one with role "states", or the article's only
    # one, with no role. Never the first by position (#601).
    names = list(dict.fromkeys(name for name, _role in own))
    states = list(dict.fromkeys(name for name, role in own if role == "states"))
    credited = len(states) == 1 or (len(names) == 1 and all(role is None for _name, role in own))
    if names:
        facts["lean"] = ", ".join(dict.fromkeys([*(states if len(states) == 1 else []), *names]))
    if mathlib_names:
        facts["mathlib_declaration"] = ", ".join(dict.fromkeys(mathlib_names))
    status = facts.pop("status", None)
    if status in _STATUSES:
        if not declaration_names(facts.get("lean", "")):
            drops.add("a status needs a Lean declaration of this project, so it is left out", node_id)
        elif not credited:
            reason = (
                "a status needs one Lean declaration with role states, or a single one with no role, so it is left out"
            )
            drops.add(reason, node_id)
        elif status == "retracted" and facts.get("mathlib") == "true":
            drops.add(f"status retracted is left out beside {NAMESPACE} mathlib true", node_id)
        else:
            facts["statement"] = "retracted" if status == "retracted" else "formalized"
            # A definition is complete once stated, and an axiom is never proved, so neither's "formalized" asserts a
            # proof (status.py).
            command = declaration_command(facts.get("declaration")).casefold()
            if status == "formalized" and command not in DEFINITION_DECLARATIONS and command != "axiom":
                facts["proof"] = "formalized"
    elif status is not None and status != "not_formalized":
        drops.add("status values Autoform does not read are left out", f"{node_id} ({status})")
    if "catalog" in facts and _DECLARATION_KEYS & facts.keys():
        del facts["catalog"]
        drops.add("catalog is left out beside a kind or a Mathlib link", node_id)
    return facts
