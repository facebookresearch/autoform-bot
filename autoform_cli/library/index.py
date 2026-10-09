"""The index a shared Lean library publishes: its records, its bytes, and its digests.

The file is written by another repository, so :func:`parse_index` treats every
byte as untrusted and refuses anything outside the schema.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

#: Provisional until the generator has shown it can produce every field.
INDEX_SCHEMA = "autoform-library-index/v0"
INDEX_FILE = "autoform-library-index.jsonl"
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
#: The files beside the sources whose bytes decide what the library builds against.
TREE_FILES = ("lake-manifest.json", "lakefile.lean", "lakefile.toml", "lean-toolchain")
#: The keyword a declaration is written with.
KINDS = ("abbrev", "class", "def", "inductive", "instance", "lemma", "opaque", "structure", "theorem")
STATUSES = ("complete", "wanted")
_DEPENDENCY_TYPES = ("git", "path")
_MODULE_COMPONENT = re.compile(r"[A-Za-z_][A-Za-z0-9_'!?]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_ECHO_LIMIT = 80
_HEADER_KEYS = frozenset(
    {"record", "schema", "generator", "probe", "package", "source_dirs", "lean_toolchain", "dependencies", "tree"}
)
_MODULE_KEYS = frozenset({"record", "name", "source_file", "sha256", "module_doc", "module_system"})
_DECLARATION_KEYS = frozenset(
    {
        "record",
        "name",
        "module",
        "kind",
        "status",
        "line",
        "signature",
        "statement",
        "docstring",
        "mentions",
        "auto_named",
    }
)


class LibraryIndexError(ValueError):
    """An index file is not one this reader accepts."""


@dataclass(frozen=True, slots=True)
class IndexDependency:
    name: str
    type: str
    rev: str | None


@dataclass(frozen=True, slots=True)
class IndexHeader:
    generator: str
    probe: int
    package: str
    source_dirs: tuple[str, ...]
    lean_toolchain: str
    dependencies: tuple[IndexDependency, ...]
    tree: str

    def as_dict(self) -> dict[str, object]:
        return {
            "record": "header",
            "schema": INDEX_SCHEMA,
            "generator": self.generator,
            "probe": self.probe,
            "package": self.package,
            "source_dirs": list(self.source_dirs),
            "lean_toolchain": self.lean_toolchain,
            "dependencies": [
                {"name": dependency.name, "type": dependency.type, "rev": dependency.rev}
                for dependency in self.dependencies
            ],
            "tree": self.tree,
        }


@dataclass(frozen=True, slots=True)
class IndexModule:
    name: str
    source_file: str
    #: Of the source after each CRLF became LF; see :func:`module_digest`.
    sha256: str
    module_doc: str | None
    #: Whether the file starts with ``module``. A module file cannot import one that does not.
    module_system: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "record": "module",
            "name": self.name,
            "source_file": self.source_file,
            "sha256": self.sha256,
            "module_doc": self.module_doc,
            "module_system": self.module_system,
        }


@dataclass(frozen=True, slots=True)
class IndexDeclaration:
    name: str
    module: str
    kind: str
    status: str
    line: int
    signature: str
    statement: str
    docstring: str | None
    mentions: tuple[str, ...]
    auto_named: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "record": "declaration",
            "name": self.name,
            "module": self.module,
            "kind": self.kind,
            "status": self.status,
            "line": self.line,
            "signature": self.signature,
            "statement": self.statement,
            "docstring": self.docstring,
            "mentions": list(self.mentions),
            "auto_named": self.auto_named,
        }


@dataclass(frozen=True, slots=True)
class LibraryIndex:
    header: IndexHeader
    modules: tuple[IndexModule, ...]
    declarations: tuple[IndexDeclaration, ...]


def dump_index(index: LibraryIndex) -> bytes:
    """Return the one byte string that holds ``index``: the header, then modules, then declarations."""

    records = [
        index.header.as_dict(),
        *(module.as_dict() for module in sorted(index.modules, key=lambda module: module.name)),
        *(
            declaration.as_dict()
            for declaration in sorted(index.declarations, key=lambda item: (item.name, item.module))
        ),
    ]
    return "".join(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for record in records
    ).encode("utf-8")


def module_digest(data: bytes) -> str:
    """SHA-256 of a source file as Git would store it, whatever line endings the checkout has."""

    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def tree_digest(modules: Iterable[tuple[str, str]], files: Iterable[tuple[str, bytes]]) -> str:
    """Digest every module digest and pin file together.

    A declaration's status and printed type can change when another module
    does, so two current indexes can merge, line by line, into a stale one
    whose module digests are all right. Only generation can produce this value.
    """

    digest = hashlib.sha256(b"autoform-library-tree/v0\0")
    for name, sha256 in sorted(modules):
        digest.update(f"module\0{name}\0{sha256}\n".encode("utf-8"))
    for name, data in sorted(files):
        body = data.replace(b"\r\n", b"\n")
        digest.update(f"file\0{name}\0{len(body)}\0".encode("utf-8"))
        digest.update(body)
    return digest.hexdigest()


def relative_path(value: object) -> PurePosixPath | None:
    """Return ``value`` as a path that stays inside the directory it is joined to, or ``None``."""

    if type(value) is not str or not value or any(character in value for character in "\\:\0"):
        return None
    if value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        return None
    return PurePosixPath(value)


def covers(source_dirs: Sequence[str], path: str) -> bool:
    """Whether ``path`` is a library root file ``X.lean`` or lies under ``X/`` for a source directory ``X``."""

    return any(path == f"{directory}.lean" or path.startswith(f"{directory}/") for directory in source_dirs)


def parse_index(data: bytes) -> LibraryIndex:
    """Decode an untrusted index, refusing whatever the schema does not describe."""

    if len(data) > MAX_INDEX_BYTES:
        raise LibraryIndexError(f"index is larger than {MAX_INDEX_BYTES // (1024 * 1024)} MiB")
    if data.startswith(b"\xef\xbb\xbf"):
        raise LibraryIndexError("index starts with a byte-order mark")
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise LibraryIndexError("index is not valid UTF-8") from None
    if not text:
        raise LibraryIndexError("index has no header")
    if not text.endswith("\n"):
        raise LibraryIndexError("index does not end with a newline; its last record may be cut short")

    header: IndexHeader | None = None
    modules: dict[str, IndexModule] = {}
    source_files: set[str] = set()
    declarations: dict[tuple[str, str], IndexDeclaration] = {}
    for number, line in enumerate(text[:-1].split("\n"), start=1):
        line = line.removesuffix("\r")
        if not line:
            raise LibraryIndexError(f"index line {number} is an empty line")
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise LibraryIndexError(f"index line {number} is longer than {MAX_LINE_BYTES // 1024} KiB")
        record = _record(line, number)
        kind = record.get("record")
        if number == 1:
            if kind != "header":
                raise LibraryIndexError("index has no header on its first line")
            header = _header(record)
        elif kind == "module":
            module = _module(record, number, header.source_dirs)
            if module.name in modules:
                raise LibraryIndexError(f"index has two records for module {shorten(module.name)}")
            if module.source_file in source_files:
                raise LibraryIndexError(f"two modules share the source file {shorten(module.source_file)}")
            modules[module.name] = module
            source_files.add(module.source_file)
        elif kind == "declaration":
            declaration = _declaration(record, number)
            key = (declaration.name, declaration.module)
            if key in declarations:
                raise LibraryIndexError(
                    f"index has two records for declaration {shorten(declaration.name)} in {shorten(declaration.module)}"
                )
            declarations[key] = declaration
        else:
            raise LibraryIndexError(f"index line {number} is neither a module nor a declaration record")
    for declaration in declarations.values():
        if declaration.module not in modules:
            raise LibraryIndexError(
                f"declaration {shorten(declaration.name)} names module {shorten(declaration.module)}, which has no record"
            )
    return LibraryIndex(
        header=header,
        modules=tuple(modules[name] for name in sorted(modules)),
        declarations=tuple(declarations[key] for key in sorted(declarations)),
    )


def _record(line: str, number: int) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        record: dict[str, object] = {}
        for key, value in pairs:
            if key in record:
                raise LibraryIndexError(f"index line {number} repeats the key {shorten(key)}")
            record[key] = value
        return record

    def constant(_name: str) -> None:
        raise ValueError("constant")

    try:
        record = json.loads(line, object_pairs_hook=unique, parse_constant=constant)
    except LibraryIndexError:
        raise
    except (RecursionError, ValueError):
        raise LibraryIndexError(f"index line {number} is not JSON") from None
    if type(record) is not dict:
        raise LibraryIndexError(f"index line {number} is not a JSON object")
    try:
        # A \ud800 escape decodes to a lone surrogate, which no UTF-8 text can hold.
        json.dumps(record, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise LibraryIndexError(f"index line {number} holds text that is not valid Unicode") from None
    return record


def shorten(value: object) -> str:
    """Return ``value`` as text cut to a short length, for echoing text that a third party chose."""

    text = str(value)
    return text if len(text) <= _ECHO_LIMIT else text[:_ECHO_LIMIT] + "..."


def _keys(record: dict[str, object], expected: frozenset[str], where: str) -> None:
    for key in sorted(record.keys() - expected):
        raise LibraryIndexError(f"{where}: unexpected key {shorten(key)}")
    for key in sorted(expected - record.keys()):
        raise LibraryIndexError(f"{where}: {key} is missing")


def _field(record: dict[str, object], key: str, where: str, accepts) -> object:
    value = record[key]
    if not accepts(value):
        raise LibraryIndexError(f"{where}: {key} is not a value this schema allows")
    return value


def _text(value: object) -> bool:
    return type(value) is str


def _name(value: object) -> bool:
    return type(value) is str and bool(value)


def _optional_text(value: object) -> bool:
    return value is None or type(value) is str


def _flag(value: object) -> bool:
    return type(value) is bool


def _module_name(value: object) -> bool:
    return type(value) is str and all(_MODULE_COMPONENT.fullmatch(part) for part in value.split("."))


def _header(record: dict[str, object]) -> IndexHeader:
    # The schema is read first, so a later version is named and not reported key by key.
    schema = record.get("schema")
    if schema != INDEX_SCHEMA:
        raise LibraryIndexError(f"unknown index schema {shorten(schema) if type(schema) is str else 'value'}")
    where = "index header"
    _keys(record, _HEADER_KEYS, where)

    def source_dirs(value: object) -> bool:
        return (
            type(value) is list
            and bool(value)
            and all(relative_path(item) is not None for item in value)
            and len(set(value)) == len(value)
        )

    def dependencies(value: object) -> bool:
        if type(value) is not list:
            return False
        for item in value:
            if type(item) is not dict or item.keys() != {"name", "type", "rev"} or not _name(item["name"]):
                return False
            if item["type"] not in _DEPENDENCY_TYPES:
                return False
            locked = type(item["rev"]) is str and _OBJECT_ID.fullmatch(item["rev"]) is not None
            if (item["type"] == "git") != locked or (item["type"] == "path" and item["rev"] is not None):
                return False
        return len({item["name"] for item in value}) == len(value)

    return IndexHeader(
        generator=_field(record, "generator", where, _name),
        probe=_field(record, "probe", where, lambda value: type(value) is int and value >= 0),
        package=_field(record, "package", where, _name),
        source_dirs=tuple(_field(record, "source_dirs", where, source_dirs)),
        lean_toolchain=_field(record, "lean_toolchain", where, _name),
        dependencies=tuple(
            IndexDependency(item["name"], item["type"], item["rev"])
            for item in sorted(_field(record, "dependencies", where, dependencies), key=lambda item: item["name"])
        ),
        tree=_field(record, "tree", where, lambda value: type(value) is str and _SHA256.fullmatch(value) is not None),
    )


def _module(record: dict[str, object], number: int, source_dirs: tuple[str, ...]) -> IndexModule:
    if not _module_name(record.get("name")):
        raise LibraryIndexError(f"index line {number}: module name is not a Lean module name")
    where = f"module {shorten(record['name'])}"
    _keys(record, _MODULE_KEYS, where)

    def source_file(value: object) -> bool:
        return relative_path(value) is not None and value.endswith(".lean") and covers(source_dirs, value)

    return IndexModule(
        name=record["name"],
        source_file=_field(record, "source_file", where, source_file),
        sha256=_field(
            record, "sha256", where, lambda value: type(value) is str and _SHA256.fullmatch(value) is not None
        ),
        module_doc=_field(record, "module_doc", where, _optional_text),
        module_system=_field(record, "module_system", where, _flag),
    )


def _declaration(record: dict[str, object], number: int) -> IndexDeclaration:
    if not _name(record.get("name")):
        raise LibraryIndexError(f"index line {number}: declaration has no name")
    where = f"declaration {shorten(record['name'])}"
    _keys(record, _DECLARATION_KEYS, where)
    return IndexDeclaration(
        name=record["name"],
        module=_field(record, "module", where, _module_name),
        kind=_field(record, "kind", where, lambda value: type(value) is str and value in KINDS),
        status=_field(record, "status", where, lambda value: type(value) is str and value in STATUSES),
        line=_field(record, "line", where, lambda value: type(value) is int and value >= 1),
        signature=_field(record, "signature", where, _text),
        statement=_field(record, "statement", where, _text),
        docstring=_field(record, "docstring", where, _optional_text),
        mentions=tuple(_field(record, "mentions", where, lambda value: type(value) is list and all(map(_name, value)))),
        auto_named=_field(record, "auto_named", where, _flag),
    )


__all__ = [
    "INDEX_FILE",
    "INDEX_SCHEMA",
    "KINDS",
    "MAX_INDEX_BYTES",
    "STATUSES",
    "TREE_FILES",
    "IndexDeclaration",
    "IndexDependency",
    "IndexHeader",
    "IndexModule",
    "LibraryIndex",
    "LibraryIndexError",
    "covers",
    "dump_index",
    "module_digest",
    "parse_index",
    "relative_path",
    "tree_digest",
]
