"""Pinned name lists: the names a library holds at one pinned revision.

A list is a JSON Lines file directly in ``blueprint/libraries/``, such as
``libraries/mathlib.jsonl``, which a generator writes, such as a wiki's own
build. It says that a name exists at a revision, so ``check`` can warn about a
``mathlib_declaration`` name that no list holds. It never earns credit or
status. It is not a library index (:mod:`.index`), which holds a library's whole
keyword-declared API: a list holds the names a project cites.

Lists are read by the library index's strict rules and caps, except that a bad
record is skipped rather than refusing the file, since one record must not turn
every other name into an unknown one. A file whose header breaks a rule is left
out whole. Either way one problem names the file, the other lists still load,
and nothing in the folder stops a command.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..lean import _is_name, _unquoted_parts, declaration_names
from .jsonl import (
    MAX_FILE_BYTES,
    MAX_LINE_BYTES,
    RecordError,
    _decode,
    _field,
    _keys,
    _module_name,
    _record,
    relative_path,
    shorten,
)

SCHEMA = "autoform-pinned-names/v0"
#: The folder below the blueprint root that holds the lists.
FOLDER = "libraries"
#: What the Lean environment says a constant is.
KINDS = (
    "abbrev",
    "axiom",
    "class",
    "constructor",
    "def",
    "inductive",
    "instance",
    "opaque",
    "quot",
    "recursor",
    "structure",
    "theorem",
)
_REVISION = re.compile(r"[0-9a-f]{40}")
_HEADER_KEYS = frozenset({"record", "schema", "generator", "lean_toolchain", "coverage", "packages"})
_PACKAGE_KEYS = frozenset({"repository", "revision", "source_root", "docs"})
_NAME_KEYS = frozenset({"record", "name", "package", "module", "kind", "line", "signature", "deprecated"})


@dataclass(frozen=True, slots=True)
class PinnedPackage:
    #: The header's key, such as ``mathlib`` or ``core``.
    key: str
    #: The https address that browses the repository, without ``.git``.
    repository: str
    revision: str
    #: Where module paths start in the repository: ``""``, or a relative path ending in ``/``.
    source_root: str
    #: The documentation's base address, ending in ``/``, or None.
    docs: str | None


@dataclass(frozen=True, slots=True)
class PinnedName:
    #: As the list writes it.
    name: str
    #: The stem of the list's file, such as ``mathlib``.
    list: str
    package: PinnedPackage
    module: str
    kind: str
    line: int | None
    deprecated: bool
    #: What replaces a deprecated name, when the list names it.
    replacement: str | None


@dataclass(frozen=True, slots=True)
class Lookup:
    #: The record, or None for a name that is unknown or ambiguous.
    record: PinnedName | None
    #: The sorted stems of the lists that hold the name: none means it is
    #: unknown, and two or more without a record mean the lists disagree.
    lists: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PinnedLists:
    """The usable lists in a blueprint, and a problem for each file left out or holding bad records."""

    #: (subject, message): the path of the file, below the blueprint, and what was left out of it.
    problems: tuple[tuple[str, str], ...]
    #: Each name's records, keyed by its components, in the order of the lists' stems.
    names: Mapping[tuple[str, ...], tuple[PinnedName, ...]]

    def lookup(self, name: str) -> Lookup:
        """Find ``name`` as a Lean name, so ``_root_.«Nat».succ`` finds ``Nat.succ``.

        Two lists agree about a name when they pin the same repository at the
        same revision; the record is then the first list's.
        """

        records = self.names.get(_unquoted_parts(name), ()) if _is_name(name) else ()
        sources = {(record.package.repository, record.package.revision) for record in records}
        return Lookup(records[0] if len(sources) == 1 else None, tuple(record.list for record in records))


def load_pinned_lists(blueprint: Path) -> PinnedLists | None:
    """Read the lists in ``blueprint/libraries/``; None when the blueprint root holds no entry named exactly that.

    The root is listed rather than the path tested: a case-insensitive
    filesystem opens ``Libraries/`` as ``libraries/``, while render and every
    case-sensitive filesystem find no such folder. A list is a ``*.jsonl`` file
    directly in it; names that start with ``.`` are skipped, as render skips
    them.
    """

    try:
        if FOLDER not in os.listdir(blueprint):
            return None
    except OSError:
        return None  # nothing shows that the folder exists
    try:
        if not stat.S_ISDIR(os.lstat(blueprint / FOLDER).st_mode):
            return PinnedLists(((FOLDER, "ignored: it is not a folder"),), {})
        entries = os.listdir(blueprint / FOLDER)
    except OSError as error:
        return PinnedLists(((FOLDER, f"ignored: it cannot be read: {error.strerror}"),), {})
    problems: list[tuple[str, str]] = []
    names: dict[tuple[str, ...], tuple[PinnedName, ...]] = {}
    lists = (entry for entry in entries if entry.endswith(".jsonl") and not entry.startswith("."))
    for stem in sorted(entry.removesuffix(".jsonl") for entry in lists):
        subject = _printable(f"{FOLDER}/{stem}.jsonl")
        try:
            records, skipped = _read_list(blueprint / FOLDER / f"{stem}.jsonl", stem)
        except RecordError as error:
            problems.append((subject, _printable(f"ignored: {error}")))
            continue
        if len(skipped) == 1:
            problems.append((subject, _printable(f"skipped a bad record: {skipped[0]}")))
        elif skipped:
            problems.append((subject, _printable(f"skipped {len(skipped)} bad records; the first: {skipped[0]}")))
        for record in records:
            key = _unquoted_parts(record.name)
            names[key] = (*names.get(key, ()), record)
    return PinnedLists(tuple(problems), names)


def name_warnings(
    lists: PinnedLists, node_id: str, mathlib_declaration: str | None, mathlib_file: str | None
) -> list[str]:
    """What check warns about an article's Mathlib keys once the blueprint has lists.

    A ``mathlib_declaration`` name that no list holds, one the lists disagree
    about, or a deprecated one draws a warning. So does a ``mathlib_file`` that
    is not the file of the first name's module, which is the name it is
    written for; with that name unknown or disputed, it is not compared.
    """

    warnings: list[str] = []
    names = list(dict.fromkeys(declaration_names(mathlib_declaration or "")))
    for name in names:
        found = lists.lookup(name)
        if found.record is None and not found.lists:
            warnings.append(f"{node_id}: mathlib_declaration {name} was not found in any pinned name list")
        elif found.record is None:
            files = " and ".join(f"{FOLDER}/{stem}.jsonl" for stem in found.lists)
            warnings.append(f"{node_id}: mathlib_declaration {name} is ambiguous: {files} pin different sources")
        elif found.record.deprecated:
            replacement = found.record.replacement
            advice = f"; use {replacement}" if replacement else ", with no replacement named"
            warnings.append(f"{node_id}: mathlib_declaration {name} is deprecated at its pinned revision{advice}")
    first = lists.lookup(names[0]).record if names and mathlib_file else None
    if first is not None:
        path = first.module.replace(".", "/") + ".lean"
        if mathlib_file.replace("\\", "/") not in (path, first.package.source_root + path):
            warnings.append(f"{node_id}: mathlib_file is {mathlib_file!r}, but {names[0]} is declared in {path}")
    return [_printable(warning) for warning in warnings]


def _read_list(path: Path, stem: str) -> tuple[list[PinnedName], list[str]]:
    """The list's usable records, and why each bad one was skipped; a problem with the whole file raises."""

    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise RecordError("it is not a regular file")
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as error:
        raise RecordError(f"it cannot be read: {error.strerror}") from None
    lines = _decode(data, "it", MAX_FILE_BYTES).split("\n")
    # What follows the last newline may be cut short.
    unterminated = lines.pop()
    if not lines:
        raise RecordError("its header does not end with a newline" if unterminated else "it has no header")
    packages = _header(_line(lines[0], 1))
    records: list[PinnedName] = []
    skipped: list[str] = []
    first_lines: dict[tuple[str, ...], int] = {}
    for number, line in enumerate(lines[1:], start=2):
        try:
            record = _name(_line(line, number), f"line {number}", stem, packages)
            # A Lean environment holds a name once, however its components are quoted.
            key = _unquoted_parts(record.name)
            if key in first_lines:
                raise RecordError(f"line {number} names {shorten(record.name)} again, after line {first_lines[key]}")
        except RecordError as error:
            skipped.append(str(error))
            continue
        first_lines[key] = number
        records.append(record)
    if unterminated:
        skipped.append(f"line {len(lines) + 1} does not end with a newline, so it may be cut short")
    return records, skipped


def _line(line: str, number: int) -> dict[str, object]:
    line = line.removesuffix("\r")
    if not line:
        raise RecordError(f"line {number} is an empty line")
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise RecordError(f"line {number} is longer than {MAX_LINE_BYTES // 1024} KiB")
    return _record(line, f"line {number}")


def _header(record: dict[str, object]) -> dict[str, PinnedPackage]:
    if record.get("record") != "header":
        raise RecordError("it has no header on its first line")
    # The schema is read first, so a later version is named and not reported key by key.
    schema = record.get("schema")
    if schema != SCHEMA:
        raise RecordError(f"unknown schema {shorten(schema) if type(schema) is str else 'value'}")
    where = "header"
    _keys(record, _HEADER_KEYS, where)
    _field(record, "generator", where, _nonempty)
    _field(record, "lean_toolchain", where, _nonempty)
    # Only the names a project cites; other coverages are reserved.
    _field(record, "coverage", where, lambda value: value == "cited")
    packages = _field(record, "packages", where, lambda value: type(value) is dict and bool(value) and all(value))
    return {key: _package(key, value) for key, value in packages.items()}


def _package(key: str, value: object) -> PinnedPackage:
    where = f"package {shorten(key)}"
    if type(value) is not dict:
        raise RecordError(f"{where} is not a JSON object")
    _keys(value, _PACKAGE_KEYS, where)

    def repository(value: object) -> bool:
        return _https(value) and not value.endswith((".git", "/"))

    def revision(value: object) -> bool:
        return type(value) is str and _REVISION.fullmatch(value) is not None

    def source_root(value: object) -> bool:
        return value == "" or (type(value) is str and value.endswith("/") and relative_path(value[:-1]) is not None)

    def docs(value: object) -> bool:
        return value is None or (_https(value) and value.endswith("/"))

    return PinnedPackage(
        key=key,
        repository=_field(value, "repository", where, repository),
        revision=_field(value, "revision", where, revision),
        source_root=_field(value, "source_root", where, source_root),
        docs=_field(value, "docs", where, docs),
    )


def _name(record: dict[str, object], where: str, stem: str, packages: dict[str, PinnedPackage]) -> PinnedName:
    if record.get("record") != "name":
        raise RecordError(f"{where} is not a name record")
    _keys(record, _NAME_KEYS, where)

    def deprecation(value: object) -> bool:
        return value is None or (
            type(value) is dict
            and value.keys() == {"replacement"}
            and (value["replacement"] is None or _is_name(value["replacement"]))
        )

    deprecated = _field(record, "deprecated", where, deprecation)
    _field(record, "signature", where, lambda value: value is None or type(value) is str)
    return PinnedName(
        name=_field(record, "name", where, _is_name),
        list=stem,
        package=packages[_field(record, "package", where, lambda value: type(value) is str and value in packages)],
        module=_field(record, "module", where, _module_name),
        kind=_field(record, "kind", where, lambda value: type(value) is str and value in KINDS),
        line=_field(record, "line", where, lambda value: value is None or (type(value) is int and value >= 1)),
        deprecated=deprecated is not None,
        replacement=None if deprecated is None else deprecated["replacement"],
    )


def _nonempty(value: object) -> bool:
    return type(value) is str and bool(value)


def _https(value: object) -> bool:
    """Whether ``value`` is a plain https address: a host and a path, and no user, query or fragment."""

    # A trailing ? or # leaves an empty query or fragment, which urlsplit does not report.
    if type(value) is not str or not value.isprintable() or any(character in value for character in " ?#"):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme == "https" and bool(parts.hostname) and parts.username is None and bool(parts.path)
    except ValueError:
        return False


def _printable(text: str) -> str:
    """Escape each nonprintable character in ``text``, as check does for project text, so a list cannot forge a line."""

    return "".join(
        character if character.isprintable() else character.encode("unicode_escape").decode("ascii")
        for character in text
    )


__all__ = [
    "FOLDER",
    "KINDS",
    "SCHEMA",
    "Lookup",
    "PinnedLists",
    "PinnedName",
    "PinnedPackage",
    "load_pinned_lists",
    "name_warnings",
]
