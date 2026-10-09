"""Search the declarations of verified library indexes beside one blueprint."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..search import SearchError, SearchResult, _lean_name, _normalize, _ranks, _words, search_blueprint
from .index import INDEX_SCHEMA, IndexDeclaration, LibraryIndex, shorten
from .locate import canonical_name, read_workspace
from .verify import Difference, load_library

SEARCH_LIBRARY_SCHEMA = "autoform-search/v2"
#: Indexed fields, best first. ``lean`` is the last component of the name and
#: ``qualified_names`` the name and module in full, as in the blueprint search.
#: ``mentions`` are the constants in the type, so a declaration without a
#: docstring is found by what it is about; ``kind`` is last, so "theorem" in a
#: query does not empty the answer. The signature and statement are returned
#: and not matched: binder words such as ``Type`` occur in nearly all of them.
LIBRARY_FIELDS = ("lean", "docstring", "module_doc", "qualified_names", "mentions", "kind")


@dataclass(frozen=True, slots=True)
class LibraryHit:
    declaration: IndexDeclaration
    source_file: str
    module_system: bool
    #: The module header when it is what matched, so twenty hits do not repeat one header.
    module_doc: str | None
    matched_fields: tuple[str, ...]

    @property
    def import_line(self) -> str | None:
        """The line that makes the declaration available; a wanted result has none."""

        # Built from the module name the index parser validated, never copied from the file.
        return f"import {self.declaration.module}" if self.declaration.status == "complete" else None

    def as_dict(self) -> dict[str, object]:
        declaration = self.declaration
        return {
            "auto_named": declaration.auto_named,
            "docstring": declaration.docstring,
            "import": self.import_line,
            "kind": declaration.kind,
            "line": declaration.line,
            "matched_fields": list(self.matched_fields),
            "mentions": list(declaration.mentions),
            "module": declaration.module,
            "module_doc": self.module_doc,
            "module_system": self.module_system,
            "name": declaration.name,
            "signature": declaration.signature,
            "source_file": self.source_file,
            "statement": declaration.statement,
            "status": declaration.status,
        }


@dataclass(frozen=True, slots=True)
class LibraryResult:
    name: str
    revision: str
    index_schema: str
    generator: str
    probe: int
    differences: tuple[Difference, ...]
    total_matches: int
    hits: tuple[LibraryHit, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "revision": self.revision,
            "index_schema": self.index_schema,
            "generator": self.generator,
            "probe": self.probe,
            "differences": [difference.as_dict() for difference in self.differences],
            "total_matches": self.total_matches,
            "hits": [hit.as_dict() for hit in self.hits],
        }


@dataclass(frozen=True, slots=True)
class LibrarySearch:
    blueprint: SearchResult
    libraries: tuple[LibraryResult, ...]

    def as_dict(self) -> dict[str, object]:
        # Every v1 key keeps its meaning; ``source_revision`` still covers the blueprint only.
        return {
            **self.blueprint.as_dict(),
            "schema": SEARCH_LIBRARY_SCHEMA,
            "libraries": [library.as_dict() for library in self.libraries],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def library_argument(value: str) -> tuple[str, Path | None]:
    """Split ``NAME`` or ``NAME=PATH`` as ``--library`` takes it."""

    name, separator, path = value.partition("=")
    if not name or (separator and not path):
        raise ValueError("expected NAME or NAME=INDEX_PATH")
    return name, Path(path) if separator else None


def search_with_libraries(
    project_or_blueprint: str | Path,
    query: str,
    *,
    lean_root: str | Path | None,
    libraries: Sequence[tuple[str, Path | None]],
    states: Sequence[str] = (),
    declarations: Sequence[str] = (),
    limit: int = 20,
) -> LibrarySearch:
    """Search the blueprint and each named library, or refuse the whole call.

    One library that cannot be searched refuses everything, the blueprint
    results included: an answer without it would read as "this result is new".
    """

    if lean_root is None:
        raise SearchError("--library needs --lean-root, which names the Lake project that locks the library")
    names = [canonical_name(name) for name, _path in libraries]
    for (name, _path), canonical in zip(libraries, names):
        if names.count(canonical) > 1:
            raise SearchError(f"library {shorten(name)}: given twice")
    blueprint = search_blueprint(
        project_or_blueprint, query, lean_root=lean_root, states=states, declarations=declarations, limit=limit
    )
    workspace = read_workspace(lean_root)
    words = _words(query)
    results: list[LibraryResult] = []
    for name, index_path in libraries:
        verified = load_library(workspace, name, index_path)
        total, hits = match_index(verified.index, words, blueprint.terms, limit)
        results.append(
            LibraryResult(
                name=name,
                revision=verified.revision,
                index_schema=INDEX_SCHEMA,
                generator=verified.index.header.generator,
                probe=verified.index.header.probe,
                differences=verified.differences,
                total_matches=total,
                hits=hits,
            )
        )
    return LibrarySearch(blueprint, tuple(results))


def match_index(
    index: LibraryIndex, words: tuple[str, ...], terms: tuple[str, ...], limit: int
) -> tuple[int, tuple[LibraryHit, ...]]:
    """Return how many declarations hold every term, and the first ``limit`` of them in order."""

    modules = {module.name: module for module in index.modules}
    # A header is shared by every declaration of its module, so it is folded once.
    headers = {module.name: _normalize(module.module_doc or "") for module in index.modules}
    matches: list[tuple[int, bool, int, bool, str, str, tuple[int, ...]]] = []
    for declaration in index.declarations:
        name = _normalize(declaration.name)
        fields = {
            "lean": _normalize(declaration.name.rpartition(".")[2]),
            "docstring": _normalize(declaration.docstring or ""),
            "module_doc": headers[declaration.module],
            "qualified_names": f"{name}\n{_normalize(declaration.module)}",
            "mentions": _normalize("\n".join(declaration.mentions)),
            "kind": declaration.kind,
            # Not a field of its own: the name between a dot and a space, so a
            # term is looked up as the whole name or as its last components.
            "lean_names": f".{_lean_name(name)} ",
        }
        ranks = _ranks(terms, fields, LIBRARY_FIELDS)
        if ranks is None:
            continue
        matches.append(
            (
                min(ranks),
                _ranks(words, fields, LIBRARY_FIELDS) is None,
                max(ranks),
                declaration.status == "wanted",
                declaration.name,
                declaration.module,
                tuple(sorted(set(ranks))),
            )
        )
    # The best field first, then the words as typed, then the better worst
    # field, then a complete result before a wanted one, then by name.
    matches.sort()
    by_key = {(declaration.name, declaration.module): declaration for declaration in index.declarations}
    hits = []
    for *_order, name, module, ranks in matches[:limit]:
        fields = tuple(LIBRARY_FIELDS[rank] for rank in ranks)
        hits.append(
            LibraryHit(
                declaration=by_key[(name, module)],
                source_file=modules[module].source_file,
                module_system=modules[module].module_system,
                module_doc=modules[module].module_doc if "module_doc" in fields else None,
                matched_fields=fields,
            )
        )
    return len(matches), tuple(hits)


__all__ = [
    "LIBRARY_FIELDS",
    "SEARCH_LIBRARY_SCHEMA",
    "LibraryHit",
    "LibraryResult",
    "LibrarySearch",
    "library_argument",
    "match_index",
    "search_with_libraries",
]
