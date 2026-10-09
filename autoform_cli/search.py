"""Read-only search over the articles of one Markdown blueprint."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .graph import load_graph
from .lean import SourceIndex, index_failure_message, index_project
from .markdown import ArticleParts, article_parts, visible_prose
from .runtime import RuntimeNode, build_runtime_graph, resolve_runtime_paths


SEARCH_SCHEMA = "autoform-search/v1"
#: How many dependents a hit names; ``used_by_count`` is never capped.
_USED_BY_LIMIT = 10
#: Indexed fields, best first. A field's position is the rank its matches sort by.
#: ``lean`` and ``node_id`` hold only the last component of each name, so a
#: chapter's directory or a project's namespace does not make every article
#: beneath it a strong match; ``qualified_names`` holds the names in full.
_FIELDS = ("title", "lean", "node_id", "statement_text", "ancestors", "qualified_names")
#: Words a sentence needs and a search does not. An article that lacks one of
#: them is no less a match. Words that change a statement's meaning, such as
#: ``not`` and ``every``, are not among them.
_FILLER_WORDS = frozenset(
    "a an and are as at be by can do does for from has have if in is it its let of on or such that the then "
    "there these this to we when where which whose with".split()
)
#: Endings cut from a query word, longest first, so one form of a word finds the others.
_WORD_ENDINGS = ("ing", "ed", "es", "s")
#: Punctuation and quoting a sentence or Markdown leaves around a word.
_QUOTING = ".,;:!?\"'`"
#: Characters that can make a title read differently on the page than in its source.
_TITLE_MARKUP = re.compile(r"[*_`\[\]<>&$\\~]")
#: Dashes and quotation marks a typist replaces with the keyboard's own.
_PLAIN_PUNCTUATION = str.maketrans(
    {
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
        **dict.fromkeys("\u2018\u2019", "'"),
        **dict.fromkeys("\u201c\u201d", '"'),
    }
)


class SearchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SearchLeanTarget:
    declaration: str
    source_file: str | None
    line: int | None

    def as_dict(self) -> dict[str, int | str | None]:
        return {
            "declaration": self.declaration,
            "line": self.line,
            "source_file": self.source_file,
        }


@dataclass(frozen=True, slots=True)
class SearchHit:
    node_id: str
    article_id: str | None
    article_path: str
    #: SHA-256 of the article's bytes, which is unchanged by an edit elsewhere.
    article_revision: str
    title: str
    declaration: str | None
    state: str
    #: The statement as authored, so mathematics arrives as the LaTeX it was written in.
    statement_text: str
    lean_targets: tuple[SearchLeanTarget, ...]
    mathlib: bool
    mathlib_declarations: tuple[str, ...]
    mathlib_file: str | None
    source_targets: tuple[str, ...]
    used_by: tuple[str, ...]
    used_by_count: int
    matched_fields: tuple[str, ...]
    #: Another article has this title. Advisory: two chapters may each have a "Main theorem".
    shared_title: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "article_id": self.article_id,
            "article_path": self.article_path,
            "article_revision": self.article_revision,
            "declaration": self.declaration,
            "lean_targets": [target.as_dict() for target in self.lean_targets],
            "matched_fields": list(self.matched_fields),
            "mathlib": self.mathlib,
            "mathlib_declarations": list(self.mathlib_declarations),
            "mathlib_file": self.mathlib_file,
            "node_id": self.node_id,
            "shared_title": self.shared_title,
            "source_targets": list(self.source_targets),
            "state": self.state,
            "statement_text": self.statement_text,
            "title": self.title,
            "used_by": list(self.used_by),
            "used_by_count": self.used_by_count,
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    source_revision: str
    open_statements: bool
    query: str
    terms: tuple[str, ...]
    states: tuple[str, ...]
    declarations: tuple[str, ...]
    limit: int
    total_matches: int
    hits: tuple[SearchHit, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": SEARCH_SCHEMA,
            "source_revision": self.source_revision,
            "open_statements": self.open_statements,
            "query": self.query,
            "terms": list(self.terms),
            "filters": {"declaration": list(self.declarations), "state": list(self.states)},
            "limit": self.limit,
            "total_matches": self.total_matches,
            "hits": [hit.as_dict() for hit in self.hits],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def search_blueprint(
    project_or_blueprint: str | Path,
    query: str,
    *,
    lean_root: str | Path | None = None,
    states: Sequence[str] = (),
    declarations: Sequence[str] = (),
    limit: int = 20,
) -> SearchResult:
    """Return the articles in which every term of ``query`` occurs.

    Nothing is written and nothing is kept between calls: each one reads the
    blueprint again, so a hit describes the Markdown as it is now.
    """

    words = _words(query)
    if not words:
        raise SearchError("search query has no terms")
    terms = tuple(dict.fromkeys(_stem(word) for word in words))
    if limit < 1:
        raise SearchError("search limit must be a positive integer")
    wanted_states = tuple(sorted(set(states)))
    wanted_declarations = tuple(sorted({_normalize(declaration) for declaration in declarations}))
    if "" in wanted_declarations:
        raise SearchError("search declaration kind is empty")

    paths = resolve_runtime_paths(project_or_blueprint)
    graph = load_graph(paths.blueprint_dir)
    # The runtime is built without the Lean root and the sources are indexed
    # here, once, because a hit reports a declaration's line as well as its file.
    runtime = build_runtime_graph(graph, project_root=paths.project_root)
    lean_index = _lean_index(lean_root)
    nodes = {node.id: node for node in runtime.nodes}
    root_page = (paths.blueprint_dir / "roadmap" / "README.md").resolve()
    root = next((node_id for node_id, node in graph.nodes.items() if node.path.resolve() == root_page), None)

    # A kind no article uses would give an empty answer, which reads as "this
    # result is new" when the kind was only misspelled.
    unused = [kind for kind in wanted_declarations if not any(_declares(node, (kind,)) for node in runtime.nodes)]
    if unused:
        in_use = sorted({_normalize(node.declaration) for node in runtime.nodes if node.declaration})
        raise SearchError(
            f"no article declares kind {', '.join(unused)}; this blueprint uses: {', '.join(in_use) or 'none'}"
        )

    used_by: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    title_count: dict[str, int] = {}
    for node in runtime.nodes:
        for dependency in node.dependencies:
            used_by[dependency].append(node.id)
        title = _normalize(node.title)
        title_count[title] = title_count.get(title, 0) + 1

    matches: list[tuple[bool, int, int, SearchHit]] = []
    for node in runtime.nodes:
        if wanted_states and node.status.state not in wanted_states:
            continue
        if wanted_declarations and not _declares(node, wanted_declarations):
            continue
        revision, text = _article(graph.nodes[node.id].path, node)
        parts = article_parts(text)
        fields = _fields(node, nodes, root, parts)
        ranks = _ranks(terms, fields)
        if ranks is None:
            continue
        dependents = sorted(used_by[node.id])
        hit = SearchHit(
            node_id=node.id,
            article_id=node.article_id,
            article_path=node.article_path,
            article_revision=revision,
            title=node.title,
            declaration=node.declaration,
            state=node.status.state,
            statement_text=parts.statement,
            lean_targets=tuple(_lean_target(target.declaration, lean_index) for target in node.lean_targets),
            mathlib=node.mathlib,
            mathlib_declarations=node.mathlib_declarations,
            mathlib_file=node.mathlib_file,
            source_targets=node.source_targets,
            used_by=tuple(dependents[:_USED_BY_LIMIT]),
            used_by_count=len(dependents),
            matched_fields=tuple(_FIELDS[rank] for rank in sorted(set(ranks))),
            shared_title=title_count[_normalize(node.title)] > 1,
        )
        matches.append((min(ranks), _ranks(words, fields) is None, max(ranks), hit))

    # The best field first, and within it the articles holding the words as
    # typed. Among equals, the article whose worst-placed term sits in a
    # better field, then the one more articles depend on.
    matches.sort(key=lambda match: (*match[:3], -match[3].used_by_count, match[3].node_id))
    return SearchResult(
        source_revision=runtime.source_revision,
        open_statements=runtime.open_statements,
        query=query,
        terms=terms,
        states=wanted_states,
        declarations=wanted_declarations,
        limit=limit,
        total_matches=len(matches),
        hits=tuple(match[3] for match in matches[:limit]),
    )


def statement_preview(hit: SearchHit) -> str:
    """Return the start of a hit's statement as a reader sees it, on one line."""

    prose = visible_prose(hit.statement_text)
    return prose if len(prose) <= 200 else prose[:197].rstrip() + "..."


def _normalize(text: str) -> str:
    """Fold ``text`` so spellings a reader takes for the same string compare equal.

    Case, width, ligatures, accents, typographic dashes and quotes, and
    invisible characters such as a soft hyphen all fold away, because the
    person searching types none of them the way a source happened to.
    """

    kept: list[str] = []
    for character in unicodedata.normalize("NFKD", text):
        category = unicodedata.category(character)
        # A mark is an accent only on a letter. On a relation it negates, and
        # an unequal sign must not find an equation.
        accent = category == "Mn" and kept and unicodedata.category(kept[-1]).startswith("L")
        if category != "Cf" and not accent:
            kept.append(character)
    # Recomposing makes a negated relation one character again, so the plain
    # relation is not a substring of it.
    plain = unicodedata.normalize("NFKC", "".join(kept)).translate(_PLAIN_PUNCTUATION)
    return " ".join(plain.casefold().split())


def _ranks(terms: tuple[str, ...], fields: dict[str, str], order: tuple[str, ...] = _FIELDS) -> list[int] | None:
    """Return each term's best field in ``order``, or ``None`` when a term occurs in none."""

    ranks: list[int] = []
    for term in terms:
        rank = next((rank for rank, name in enumerate(order) if term in fields[name]), None)
        # A declaration's whole name, or its last components, names the
        # declaration as its last component does. A namespace alone does not.
        if f".{_lean_name(term)} " in fields["lean_names"]:
            rank = min(order.index("lean"), len(order) if rank is None else rank)
        if rank is None:
            return None
        ranks.append(rank)
    return ranks


def _lean_name(name: str) -> str:
    """Return a folded Lean name as a query term spells it.

    ``_root_.`` and the quoting marks leave it the same name. A term has lost
    the prime, ``?`` or ``!`` a name ends with, so the name loses it as well.
    """

    return name.removeprefix("_root_.").replace("\u00ab", "").replace("\u00bb", "").rstrip(_QUOTING)


def _words(query: str) -> tuple[str, ...]:
    """Split a query into the distinct words an article must contain."""

    if any(unicodedata.category(character) == "Cs" for character in query):
        raise SearchError("search query is not valid Unicode text")
    words: list[str] = []
    for word in _normalize(query).split():
        word = _bare(word)
        if word:
            words.append(word)
    return tuple(dict.fromkeys([word for word in words if word not in _FILLER_WORDS] or words))


def _bare(word: str) -> str:
    """Return a query word without what a sentence or Markdown wrapped around it.

    Matching is by substring, so a term cut at its edges finds no less.
    """

    bare, peeled = word, ""
    # A wrapper may sit inside another, as in (**weak**), so peel until
    # nothing more comes off.
    while bare != peeled:
        peeled = bare
        # A formula pasted with its dollar signs is matched on its content,
        # since the published text delimits mathematics differently.
        bare = bare.strip(_QUOTING).strip("$")
        # Emphasis wraps a word on both sides. On one side a star or an
        # underscore is part of a name, as in C* and foo_.
        if len(bare) > 2 and bare[0] == bare[-1] and bare[0] in "*_":
            bare = bare[1:-1]
        # A bracket that closes one inside the word, as in f(x), belongs to it.
        if bare.count("(") < bare.count(")") and bare.endswith(")"):
            bare = bare[:-1]
        elif bare.count("(") > bare.count(")") and bare.startswith("("):
            bare = bare[1:]
        elif _wrapped(bare):
            bare = bare[1:-1]
    # A relation such as != is not punctuation left over from a sentence.
    return bare if any(character.isalnum() for character in bare) else word.strip("$")


def _wrapped(word: str) -> bool:
    """Whether the first bracket of ``word`` is closed by its last character."""

    if not word.startswith("("):
        return False
    depth = 0
    for index, character in enumerate(word):
        depth += (character == "(") - (character == ")")
        if depth == 0:
            return index == len(word) - 1
    return False


def _stem(word: str) -> str:
    """Cut a common ending from a word of letters, so ``recovered`` finds ``recovers``.

    Matching is by substring, so shortening the query word is enough: the
    article's own words are left as written. A name with a dot, an underscore
    or a digit is searched exactly.
    """

    # ``-ss``, ``-us`` and ``-is`` end a singular: compactness, continuous, basis.
    if not word.isalpha() or word.endswith(("ss", "us", "is")):
        return word
    for ending in _WORD_ENDINGS:
        if not word.endswith(ending):
            continue
        stem = word[: -len(ending)]
        if ending in ("ed", "es") and stem.endswith("i"):
            # topologies and satisfied share a stem with topology and satisfy.
            stem = stem[:-1]
        elif ending in ("ing", "ed") and len(stem) > 4 and stem[-1] == stem[-2]:
            # embedding doubles the last letter of embed.
            stem = stem[:-1]
        if len(stem) >= 4:
            return stem
    if len(word) > 5 and word.endswith("y") and word[-2] not in "aeiou":
        # topology is found in topologies. A short word such as every is kept.
        return word[:-1]
    return word


def _declares(node: RuntimeNode, kinds: tuple[str, ...]) -> bool:
    """Whether ``node`` declares one of ``kinds``, with or without leading modifiers."""

    declaration = _normalize(node.declaration or "")
    return any(declaration == kind or declaration.endswith(f" {kind}") for kind in kinds)


def _article(path: Path, node: RuntimeNode) -> tuple[str, str]:
    """Return an article's revision and text, refusing bytes the graph was not built from."""

    try:
        content = path.read_bytes()
    except OSError:
        raise SearchError(f"{node.id}: article cannot be read") from None
    revision = hashlib.sha256(content).hexdigest()
    if revision != node.source_sha256:
        raise SearchError(
            f"{node.id}: the article changed while the blueprint was being searched; retry after the project is idle"
        )
    return revision, content.decode("utf-8")


@lru_cache(maxsize=4096)
def _title(title: str) -> str:
    """Return a title as the page shows it, since a reader searches for what they read."""

    # Most titles are plain words and need no renderer. One the renderer reads
    # as a block of its own, such as a numbered item, is matched as written.
    if not _TITLE_MARKUP.search(title):
        return title
    return visible_prose(title) or title


def _fields(
    node: RuntimeNode, nodes: dict[str, RuntimeNode], root: str | None, parts: ArticleParts
) -> dict[str, str]:
    """Return what each indexed field of ``node`` holds, folded for matching."""

    # The roadmap's own title is above every article, so it singles none out.
    ancestors: list[str] = []
    parent = node.parent
    while parent is not None and parent != root:
        ancestors.append(_title(nodes[parent].title))
        parent = nodes[parent].parent
    names = [*(target.declaration for target in node.lean_targets), *node.mathlib_declarations]
    return {
        "title": _normalize(_title(node.title)),
        "lean": _normalize("\n".join(name.rpartition(".")[2] for name in names)),
        "node_id": _normalize(node.id.rpartition("/")[2]),
        "statement_text": _normalize(visible_prose(parts.statement)),
        "ancestors": _normalize("\n".join(ancestors)),
        "qualified_names": _normalize("\n".join([node.id, *names])),
        # Not a field of its own: each name between a dot and a space, so that
        # a term is looked up as a whole name or as its last components.
        "lean_names": "".join(f".{_lean_name(_normalize(name))} " for name in names),
    }


def _lean_index(lean_root: str | Path | None) -> SourceIndex | None:
    if lean_root is None:
        return None
    root = Path(lean_root).expanduser().resolve()
    if not root.is_dir():
        raise SearchError("Lean root does not exist or is not a directory")
    try:
        return index_project(root)
    except OSError as error:
        raise SearchError(index_failure_message(error)) from error


def _lean_target(name: str, index: SourceIndex | None) -> SearchLeanTarget:
    declaration = index.find(name) if index is not None else None
    if declaration is None:
        return SearchLeanTarget(name, None, None)
    return SearchLeanTarget(name, declaration.path.as_posix(), declaration.line)


__all__ = [
    "SEARCH_SCHEMA",
    "SearchError",
    "SearchHit",
    "SearchLeanTarget",
    "SearchResult",
    "search_blueprint",
    "statement_preview",
]
