"""Warn about library declarations whose docstrings cite the blueprint's sources and that nothing names.

A declaration whose docstring cites the paper a project formalizes usually
proves part of it. ``autoform check --lean-root`` names each such declaration,
or its module, that no implementation note, source page or project Lean file
names, so a skipped library search shows up. The warnings never change the exit
status.

Identifiers match across their usual forms: versions; ``abs``, ``pdf``,
``html``, ``e-print`` and ``src`` URLs, with or without a trailing slash;
old-style identifiers such as ``math.AG/0210149``; arXiv's own DOIs;
``https://doi.org/`` and ``dx.doi.org`` links, percent-encoded or not, up to a
query or fragment; and the labels ``arXiv:``, ``arXiv``, ``doi:`` and ``DOI``,
with the label or the identifier in backticks, emphasis or a table cell. A bare
number cites nothing. Link text is not read, and a linked page over 4 MiB is
skipped. A last-component match counts any identifier with that component in a
project Lean file, comments and strings included, so a generic one such as
``symm`` can hide a warning. A Lean file that imports a module names it; one
that imports only an umbrella module above it does not. A library's sources are
hashed, to verify its index, only when one of its declarations cites a reached
source.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .. import status
from .._fs import resolved_path
from ..graph import IMPLEMENTATION_NOTES_DIR, Graph
from ..lean import _CONTINUATION_IDENTIFIER, resolve_lean_root, snapshot_project_sources
from ..render import SOURCES_DIR
from .index import INDEX_FILE, LibraryIndex, LibraryIndexError, parse_index
from .listing import indexed_packages
from .locate import LibraryError, WorkspaceError, checkout_paths, find_package, read_workspace
from .verify import _index_bytes, load_library

#: A new-style identifier (2307.05244) or an old-style one (math/0210149, math.AG/0210149) after an
#: arxiv.org URL or an "arXiv" label, which Markdown emphasis or a table cell may separate from it.
#: A version suffix stays outside the match.
_ARXIV = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf|html|e-print|src)/|(?<![a-z0-9])arxiv[\s:*_|`]*)"
    r"(?:(?P<new>\d{4}\.\d{4,5})|(?P<archive>[a-z][a-z-]{0,15})(?:\.[a-z]{2})?/(?P<number>\d{7}))(?!\d)",
    re.IGNORECASE,
)
#: A DOI after a doi.org URL or a "DOI" label, which Markdown emphasis, a backtick or a table cell may
#: separate from it. A URL may percent-encode the slash after the registrant, and a DOI ends at a URL's
#: query or fragment, at a closing backtick and at the `]` of link text.
_DOI = re.compile(
    r"(?:doi\.org/|(?<![a-z0-9])doi[\s:*_|`]*)(?P<doi>10\.\d{4,9}(?:/|%2F)[^\s\"'<>`\]?#]+)", re.IGNORECASE
)
_ARXIV_DOI = "10.48550/arxiv."
#: Larger linked files are not pages Roadmap writes about a source.
_PAGE_LIMIT = 4 * 1024 * 1024
#: A «quoted» name component.
_QUOTED = re.compile(r"«([^«»]*)»")


def cited_sources(text: str | None) -> frozenset[str]:
    """The arXiv papers and DOIs ``text`` cites, each spelled one way: ``arXiv 2307.05244`` or ``DOI 10.1000/x``."""

    if not text:
        return frozenset()
    found = set()
    for match in _ARXIV.finditer(text):
        found.add(f"arXiv {_paper(match)}")
    for match in _DOI.finditer(text):
        doi = unquote(match["doi"])
        # A DOI may hold parentheses, so only an unmatched closing one is the sentence's. Emphasis or a
        # table cell can close right after it.
        end, unmatched = len(doi), doi.count(")") - doi.count("(")
        while doi[end - 1] in ".,;:}*_|" or (doi[end - 1] == ")" and unmatched > 0):
            unmatched -= doi[end - 1] == ")"
            end -= 1
        doi = doi[:end]
        # arXiv's own DOIs name the paper its identifier does, with or without a version.
        rest = re.sub(r"v\d+$", "", doi[len(_ARXIV_DOI) :])
        paper = _ARXIV.fullmatch("arXiv:" + rest) if doi.lower().startswith(_ARXIV_DOI) else None
        found.add(f"arXiv {_paper(paper)}" if paper else f"DOI {doi.lower()}")
    return frozenset(found)


def _paper(match: re.Match[str]) -> str:
    return match["new"] or f"{match['archive'].lower()}/{match['number']}"


@dataclass(frozen=True, slots=True)
class _Hit:
    library: str
    module: str
    name: str
    #: The sources it cites that unproved articles reach, the most reached first.
    sources: tuple[str, ...]
    #: Whether its own docstring cites them, rather than only its module's.
    own: bool


def library_citation_warnings(graph: Graph, lean_root: str | Path) -> list[str]:
    """Warn about usable libraries' complete declarations whose docstrings cite a source the blueprint cites.

    A declaration counts as named when an implementation note, a Markdown page under
    ``blueprint/sources/`` or one an article's ``## Sources`` links, or a project Lean file names it in
    full. A project Lean file may also name, by its last component, a declaration whose own docstring
    cites a source, since ``open`` shortens names. A hit only in a module's docstring is reported once
    for the module, which naming the module, or in full one of its declarations citing that source,
    silences.
    """

    citing, pages, containers = _blueprint_sources(graph)
    if not citing:
        return []
    try:
        workspace = read_workspace(lean_root)
        packages = indexed_packages(lean_root)
    except WorkspaceError as error:
        if not os.path.lexists(os.path.join(os.path.expanduser(os.fspath(lean_root)), "lake-manifest.json")):
            return []  # a Lean root without a Lake manifest locks no shared library
        return [
            f"the project's Lake files cannot be used, so no shared library was compared with the blueprint's sources: "
            f"{error}"
        ]
    except (OSError, RuntimeError, ValueError):
        return []  # no Lake workspace to compare, or a Lean root check reports itself

    warnings, hits = [], []
    for package in packages:
        try:
            # Matching is cheap and hashing a library's sources is not, so only a library with a hit is verified.
            index = _unverified_index(workspace, package)
            found = _hits(index, citing, package)
            if not found:
                continue
            load_library(workspace, package, index=index)
        except LibraryError as error:
            warnings.append(
                f"library {package}: its index cannot be used, so its declarations were not compared "
                f"with the blueprint's sources: {error.reason}"
            )
            continue
        hits.extend(found)
    if not hits:
        return warnings

    prose = _prose_names([*_note_texts(graph.blueprint_dir), *{**pages, **_source_pages(graph.blueprint_dir)}.values()])
    lean_names: set[str] = set()
    lean_prefixes: set[str] = set()
    lean_components: set[str] = set()
    if any(hit.name not in prose for hit in hits):
        try:
            snapshot = snapshot_project_sources(resolve_lean_root(lean_root))
        except OSError:
            return warnings  # check's own index build reads the same files, reports why, and fails
        lean_names, lean_prefixes, lean_components = _lean_names(
            data.decode("utf-8", "replace") for _, data in snapshot.source_files
        )

    def in_full(name: str) -> bool:
        # A field after a declaration's name, as in `Lib.foo.symm`, still writes the name in full.
        return name in prose or name in lean_prefixes

    modules: dict[tuple[str, str], list[_Hit]] = defaultdict(list)
    citers: dict[tuple[str, str], list[_Hit]] = defaultdict(list)
    for hit in sorted(hits, key=lambda hit: (hit.library, hit.name)):
        citers[hit.library, hit.module].append(hit)
        if not hit.own:
            modules[hit.library, hit.module].append(hit)
        elif not in_full(hit.name) and hit.name.rpartition(".")[2] not in lean_components:
            warnings.append(
                f"library {hit.library}: {hit.name}'s docstring cites {_reach(hit.sources, citing, containers)}; no "
                "implementation note, source page or project Lean file names it, so reuse it or record why it does "
                "not apply"
            )
    for (library, module), group in sorted(modules.items()):
        # Only the module's exact name, or the full name of one of its declarations citing that source,
        # counts: one generic last component, such as `symm` in any Lean file, would otherwise silence
        # every declaration the module's docstring covers.
        cited = set(group[0].sources)
        if module in prose or module in lean_names or any(
            in_full(hit.name) for hit in citers[library, module] if cited & set(hit.sources)
        ):
            continue
        names = [hit.name for hit in group]
        one = len(names) == 1
        if one:
            members = f"its declaration {names[0]} cites it only there"
        elif len(names) <= 3:
            members = f"its declarations {_joined(names)} cite it only there"
        else:
            members = f"{len(names)} of its declarations cite it only there, such as {_joined(names[:3])}"
        warnings.append(
            f"library {library}: module {module}'s docstring cites {_reach(group[0].sources, citing, containers)}; "
            f"{members}, and no implementation note, source page or project Lean file names the module or "
            f"{'that declaration' if one else 'one of its declarations citing it'}, so reuse "
            f"{'it' if one else 'them'} or record why {'it does' if one else 'they do'} not apply"
        )
    return warnings


def _unverified_index(workspace, package: str) -> LibraryIndex:
    """``package``'s index as its checkout holds it, before ``load_library`` checks it against the sources."""

    try:
        _checkout, root = checkout_paths(workspace, find_package(workspace, package))
        return parse_index(_index_bytes(root / INDEX_FILE))
    except (OSError, LibraryIndexError):
        return load_library(workspace, package).index  # raises the reason `library list` gives


def _hits(index: LibraryIndex, citing: dict[str, set[str]], library: str) -> list[_Hit]:
    def ordered(sources: frozenset[str]) -> tuple[str, ...]:
        return tuple(sorted(sources, key=lambda source: (-len(citing[source]), source)))

    module_sources = {module.name: cited_sources(module.module_doc) & citing.keys() for module in index.modules}
    hits = []
    for declaration in index.declarations:
        # A wanted result is no proof to reuse, and nobody writes an automatic name.
        if declaration.status != "complete" or declaration.auto_named:
            continue
        own = cited_sources(declaration.docstring) & citing.keys()
        if cited := own or module_sources[declaration.module]:
            hits.append(_Hit(library, declaration.module, declaration.name, ordered(cited), bool(own)))
    return hits


def _reach(sources: tuple[str, ...], citing: dict[str, set[str]], containers: frozenset[str]) -> str:
    articles = set().union(*(citing[source] for source in sources))
    more = len(sources) - 1
    cited = sources[0] + (f" and {more} more source{'s' if more > 1 else ''} the blueprint cites" if more else "")
    # The article to name reaches the source named, and is one a declaration would be used in: a leaf
    # before a container.
    example = min(citing[sources[0]], key=lambda article: (article in containers, article))
    if len(articles) == 1:
        return f"{cited}, which unproved article {example} reaches"
    return f"{cited}, which {len(articles)} unproved articles reach, including {example}"


def _joined(names: list[str]) -> str:
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _blueprint_sources(graph: Graph) -> tuple[dict[str, set[str]], dict[Path, str], frozenset[str]]:
    """The sources unproved articles reach, each with those articles; the linked pages' text; the containers.

    An article reaches what its ``## Sources`` links cite, and what a Markdown page under the blueprint
    that a link names cites, since Roadmap records source locations in ``blueprint/sources/``. A proved
    article reaches nothing: it needs no search. Nor does a container once its implementation note may
    be deleted, as audit decides: every formalizable article in its scope, and every leaf marked
    ``not_ready`` beneath it, is proved, and there is at least one. Nor does a module catalog: it
    inventories existing Lean, so it has no proof to search a library for.
    """

    contained: dict[str, list[str]] = defaultdict(list)
    for node in graph.nodes.values():
        if node.parent is not None:
            contained[node.parent].append(node.id)
    derived = status.derive(graph)
    # As audit decides a container's scope is implemented: a leaf marked not ready is a target too.
    has_target: dict[str, bool] = {}
    implemented: dict[str, bool] = {}
    for node_id in sorted(graph.nodes, key=lambda candidate: graph.nodes[candidate].depth, reverse=True):
        has_target[node_id], implemented[node_id] = False, True
        for child_id in contained.get(node_id, ()):
            child = graph.nodes[child_id]
            if child.catalog is None and (child.formalizable or (child.not_ready and child_id not in contained)):
                has_target[node_id] = True
                implemented[node_id] = implemented[node_id] and derived[child_id].proved
            if has_target[child_id]:
                has_target[node_id] = True
                implemented[node_id] = implemented[node_id] and implemented[child_id]

    citing: dict[str, set[str]] = defaultdict(set)
    pages: dict[Path, tuple[str, frozenset[str]]] = {}  # many articles link one source page
    blueprint = graph.blueprint_dir.resolve()
    for node in graph.nodes.values():
        for target in node.sources:
            page = _linked_page(node.path, target, blueprint)
            if page is not None and page not in pages:
                text = _page_text(page)
                pages[page] = (text, cited_sources(text))
            if node.catalog is not None or derived[node.id].proved or (has_target[node.id] and implemented[node.id]):
                continue
            for source in cited_sources(target) | (pages[page][1] if page is not None else frozenset()):
                citing[source].add(node.id)
    return citing, {page: text for page, (text, _sources) in pages.items()}, frozenset(contained)


def _linked_page(article: Path, target: str, blueprint: Path) -> Path | None:
    """The Markdown file under the blueprint a local link in ``article`` names, or ``None`` for any other link."""

    try:
        split = urlsplit(target)
    except ValueError:
        return None
    if split.scheme or split.netloc or not split.path:
        return None
    page = resolved_path(article.parent / unquote(split.path))
    if page is None or not page.is_relative_to(blueprint) or page.suffix.lower() != ".md":
        return None
    return page if os.path.isfile(page) else None


def _source_pages(blueprint: Path) -> dict[Path, str]:
    """The text of every Markdown page under ``blueprint/sources/``, linked or not, since any may name a declaration.

    A record there that a declaration does not apply outlives the implementation notes, which audit asks
    to delete once their article is proved. Links, hidden entries and anything but a regular file are
    skipped, with checks that never raise: a folder that cannot be searched hides its entries.
    """

    root = blueprint / SOURCES_DIR
    if os.path.islink(root):
        return {}
    pages = {}
    for directory, folders, files in os.walk(root):  # never follows a link to a folder
        folders[:] = [folder for folder in folders if not folder.startswith(".")]
        for name in files:
            path = os.path.join(directory, name)
            if name.startswith(".") or not name.lower().endswith(".md"):
                continue
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            pages[Path(path).resolve()] = _page_text(Path(path))
    return pages


def _page_text(path: Path) -> str:
    try:
        if path.stat().st_size > _PAGE_LIMIT:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _note_texts(blueprint: Path) -> list[str]:
    root = blueprint / IMPLEMENTATION_NOTES_DIR
    try:
        paths = sorted(root.iterdir())
    except OSError:
        return []
    texts = []
    for path in paths:
        # Hidden entries, such as an editor's lock symlink, are never notes.
        if path.name.startswith(".") or path.is_symlink() or not path.is_file():
            continue
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def _prose_names(texts: Iterable[str]) -> set[str]:
    """The full Lean names ``texts`` write.

    Prose may follow a name with ``'s``, ``?`` or ``!``, put it in ``_emphasis_``, or quote it, as in
    ``'Nat.foo'``.
    """

    names = set()
    for text in texts:
        for match in _CONTINUATION_IDENTIFIER.finditer(text):
            token = match.group()
            names.update((token, token.rstrip("!?"), token.removesuffix("'s")))
            if len(token) > 2 and token[0] == token[-1] == "_":
                names.add(token.strip("_"))
            if match.start() and text[match.start() - 1] == "'" and token.endswith("'"):
                names.add(token[:-1])
    return names


def _lean_names(texts: Iterable[str]) -> tuple[set[str], set[str], set[str]]:
    """The names Lean ``texts`` write, their dotted prefixes, and their components.

    After ``open Nat`` a file names ``Nat.foo`` as ``foo``, and ``Nat.foo.symm`` names ``Nat.foo`` with a
    field after it. A plain component may be quoted, so ``Nat.«foo»`` names ``Nat.foo`` too.
    """

    names: set[str] = set()
    prefixes: set[str] = set()
    components: set[str] = set()
    for text in texts:
        for token in _CONTINUATION_IDENTIFIER.findall(text):
            plain = _drop_needless_quotes(token)
            names.update((token, plain))
            parts = plain.split(".")
            prefixes.update(".".join(parts[: end + 1]) for end in range(len(parts)))
            components.update(parts)
    return names, prefixes, components


def _drop_needless_quotes(name: str) -> str:
    """``name`` with each ``«x»`` that needs no quotes written as ``x``."""

    def plain(part: re.Match[str]) -> str:
        inner = part[1]
        return inner if "." not in inner and _CONTINUATION_IDENTIFIER.fullmatch(inner) else part[0]

    return _QUOTED.sub(plain, name) if "«" in name else name


__all__ = ["cited_sources", "library_citation_warnings"]
