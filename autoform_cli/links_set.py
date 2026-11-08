"""``autoform links set``: record facts about articles where the project keeps them (#601).

A project whose facts moved into ``blueprint/links/`` has the writer's template
(:data:`~autoform_cli.annotations.WRITER_MARKER`). There an article's facts live in the two files
:mod:`autoform_cli.annotation_writer` owns for it, and an article no annotation covers moves into them when it is
first edited. Elsewhere they stay in frontmatter, edited in place. Every article's edits are decided before anything
is written, written together, and read back from the graph loaded again: if any article then reads otherwise, the
whole write is undone.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import unicodedata
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path

from . import annotation_writer as writer
from .annotation_writer import _block_end, _is_fact, _key
from .annotations import SETTINGS, Links, moved, read_links
from .article_identity import _proposed_id
from .graph import (
    ARTICLE_ID_PATTERN,
    Graph,
    GraphValidationError,
    Node,
    _normalize_value,
    _parse_frontmatter,
    _unquote_scalar,
    load_graph,
)
from .markdown import blueprint_root

#: What an edit names: every fact but the ID, which an article is given and never loses.
KEYS = writer.FACT_KEYS - {"article_id"}
_RENAME = "rename it (in Git: git mv through a temporary name)"
_BY_HAND = "record its facts there by hand"


@dataclass(frozen=True, slots=True)
class Edits:
    """What one call records: each key's value, as frontmatter normalizes it, and the keys it removes."""

    values: Mapping[str, str]
    unset: frozenset[str]

    def apply(self, facts: Mapping[str, str]) -> dict[str, str]:
        edited = {key: value for key, value in facts.items() if key not in self.unset}
        edited.update(self.values)
        return edited


class LinksSetError(GraphValidationError):
    """Nothing was recorded, for ``issues``; ``left`` names the files a stopped write could not restore."""

    def __init__(self, issues: Sequence[str], left: Sequence[str] = ()) -> None:
        super().__init__(list(issues))
        self.left = tuple(left)


@dataclass(frozen=True, slots=True)
class LinksSetResult:
    """The files a call wrote and removed, below the blueprint; neither when every article already read that way."""

    written: tuple[str, ...]
    removed: tuple[str, ...]


def parse_arguments(items: Sequence[str], unset: Sequence[str]) -> tuple[tuple[str, ...], Edits]:
    """Tell the articles from the edits among *items*: an argument holding ``=`` is an edit, ``KEY=VALUE``, and any
    other an article's path. Values are read as frontmatter reads them. Raises :class:`LinksSetError` naming every
    edit that cannot be made."""

    issues: list[str] = []
    articles: list[str] = []
    values: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            articles.append(item)
            continue
        key, value = item.split("=", 1)
        key, value = key.strip(), _unquote_scalar(value.strip()).strip()
        # A file of that name is an article written with '=', and so is a Markdown path whose key is no key; a value
        # such as discussion=https://.../page.md is still an edit.
        if os.path.lexists(item) or (key not in writer.FACT_KEYS | SETTINGS and item.endswith(".md")):
            issues.append(f"{item}: an argument holding '=' is an edit, so no article's path may hold one; {_RENAME}")
        elif problem := _key_problem(key):
            issues.append(problem)
        elif key in values:
            issues.append(f"{key}: given twice")
        elif not value:
            issues.append(f"{key}: an empty value; --unset {key} removes it")
        elif not value.isprintable():
            issues.append(f"{key}: a value with a line break or another unprintable character, which no fact holds")
        elif problem := _value_problem(key, value):
            issues.append(f"{key}={value}: {problem}")
        else:
            values[key] = _normalize_value("", 0, key, value)[0]
    removed: set[str] = set()
    for key in unset:
        if problem := _key_problem(key):
            issues.append(problem)
        elif key in removed:
            issues.append(f"--unset {key}: given twice")
        elif key in values:
            issues.append(f"{key}: both set and unset")
        else:
            removed.add(key)
    if not articles:
        issues.append("no article named; name each by its path")
    if issues:
        raise LinksSetError(issues)
    return tuple(articles), Edits(values, frozenset(removed))


def _key_problem(key: str) -> str | None:
    if key == "article_id":
        return "article_id: an article's ID is never set or removed; naming the article with no edit gives it one"
    if key in SETTINGS:
        return f"{key}: a setting, which stays in README frontmatter"
    if key not in KEYS:
        return f"{key}: not a fact; the facts are {', '.join(sorted(KEYS))}"
    return None


def _value_problem(key: str, value: str) -> str | None:
    """Why frontmatter would refuse *value* for *key*, as the loader says it, or None."""
    problem = _normalize_value("", 0, key, value)[1]
    return None if problem is None else problem.removeprefix(":0: ")


@dataclass(frozen=True, slots=True)
class _Target:
    """An article a call edits: the writer's files that must annotate it afterwards, none where its facts stay in
    frontmatter, and its facts as the graph loaded again must read them."""

    annotations: tuple[Path, ...]
    expected: dict[str, str]


def set_facts(blueprint_dir: str | Path, articles: Sequence[str], edits: Edits) -> LinksSetResult:
    """Record *edits* for every one of *articles*, articles' paths below *blueprint_dir*'s ``roadmap/``, or for
    none."""

    blueprint = blueprint_root(blueprint_dir)
    if not blueprint.is_dir():
        raise LinksSetError([f"blueprint directory does not exist: {blueprint_dir}"])
    roadmap = blueprint / "roadmap"
    pages = list(dict.fromkeys(_resolved(article) for article in articles))
    issues = [f"{page}: not a Markdown article below {roadmap}" for page in pages if not _below(page, roadmap)]
    if issues:
        raise LinksSetError(issues)
    paths = {page: writer.annotation_paths(blueprint, page) for page in pages}
    # Read before the graph is, so a file another writer changes meanwhile is refused, not overwritten.
    before: dict[Path, bytes | None] = {}
    for files in paths.values():
        for path in (files.own, files.mathlib):
            try:
                before[path] = writer._read(path)
            except OSError as error:
                issues.append(f"{_name(blueprint, path)}: {error}")
    if issues:
        raise LinksSetError(issues)
    graph = load_graph(blueprint)
    links = read_links(blueprint, {node.path: node.id for node in graph.nodes.values()})
    nodes = {node.path: node for node in graph.nodes.values()}
    owners = {node.article_id: node.id for node in graph.nodes.values() if node.article_id}

    changes: dict[Path, bytes | None] = {}
    expected: dict[Path, str | None] = {}
    targets: dict[str, _Target] = {}
    for page in pages:
        where = page.relative_to(blueprint).as_posix()
        node = nodes.get(page)
        try:
            if node is None or node.source_sha256 is None:
                raise LinksSetError([f"{where}: not an article the graph loads"])
            try:
                content = page.read_bytes()
            except OSError as error:
                raise LinksSetError([f"{where}: cannot be read: {error.strerror or error}"]) from None
            if hashlib.sha256(content).hexdigest() != node.source_sha256:
                raise LinksSetError([f"{where}: changed while the graph was loaded; run the command again"])
            home, old = _home(blueprint, where, node, content, paths[page], links, nodes, before)
            new = _edited_facts(where, node, content, old, edits, owners)
        except LinksSetError as refused:
            issues.extend(refused.issues)
            continue
        if new == old:
            continue
        annotations: tuple[Path, ...] = ()
        if home == "frontmatter":
            recorded = {key: value for key, value in new.items() if old.get(key) != value}
            changes[page], expected[page] = _edited(content, recorded, old.keys() - new.keys()), node.source_sha256
        else:
            try:
                encoded = writer.encode(blueprint, page, new)
            except writer.Unencodable as error:
                recorded = _unencodable(blueprint, page, {**old, "article_id": new["article_id"]})
                issues.extend(
                    f"{where}: {'as its frontmatter records it, ' if reason in recorded else _made(edits)}{reason}"
                    for reason in error.reasons
                )
                continue
            for path, data in encoded.items():
                if data != before.get(path):
                    changes[path], expected[path] = data, _digest(before.get(path))
            if home == "move" and (bare := writer.strip_facts(content)) != content:
                changes[page], expected[page] = bare, node.source_sha256
            files = (paths[page].own, paths[page].mathlib)
            annotations = tuple(path for path in files if encoded.get(path) is not None)
        facts = edits.apply(_facts(node))
        facts["article_id"] = new["article_id"]
        targets[node.id] = _Target(annotations, facts)
    if issues:
        raise LinksSetError(issues)
    if not changes:
        return LinksSetResult((), ())

    # The writer records each step here before taking it, so a stop before the check passes, or before what it
    # showed to read otherwise is undone, finds them; undoing a step twice is harmless.
    steps: list[writer.Written] = []
    failed: LinksSetError | None = None
    try:
        writer.write(changes, expected, steps)
        if problems := _read_back(graph, targets):
            failed = _undone(blueprint, tuple(steps), problems)
    except GraphValidationError as error:  # a stopped write among them
        raise _undone(blueprint, tuple(steps), error.issues) from None
    except BaseException:
        # Interrupted: nothing unchecked stays to be read, and what could not be restored is said.
        if (stuck := _undone(blueprint, tuple(steps), ["interrupted before what was written was checked"])).left:
            raise stuck from None
        raise
    if failed is not None:
        raise failed
    return LinksSetResult(
        tuple(_name(blueprint, step.path) for step in steps if step.sha256 is not None),
        tuple(_name(blueprint, step.path) for step in steps if step.sha256 is None),
    )


def frontmatter_articles(graph: Graph) -> list[str]:
    """The articles whose facts a project without the writer's template keeps in frontmatter, by node ID; none once
    it has the template, where the loader warns about each article left in frontmatter itself."""

    blueprint = graph.blueprint_dir
    links = read_links(blueprint, {node.path: node.id for node in graph.nodes.values()})
    if moved(blueprint, links):
        return []
    annotated = links.files if links is not None else {}
    return sorted(node.id for node in graph.nodes.values() if node.path not in annotated and _facts(node))


def _resolved(article: str) -> Path:
    try:
        return Path(article).expanduser().resolve()
    except (OSError, RuntimeError) as error:
        raise LinksSetError([f"{article}: cannot be resolved: {error}"]) from None


def _below(page: Path, roadmap: Path) -> bool:
    return page.suffix == ".md" and page.is_relative_to(roadmap)


def _home(
    blueprint: Path,
    where: str,
    node: Node,
    content: bytes,
    files: writer.AnnotationPaths,
    links: Links | None,
    nodes: Mapping[Path, Node],
    before: Mapping[Path, bytes | None],
) -> tuple[str, dict[str, str]]:
    """Where *node*'s facts live, and what they are there. Raises :class:`LinksSetError` where this command must
    not write them: an annotation it does not own, or one holding anything it would not write back."""

    own = {path: _name(blueprint, path) for path in (files.own, files.mathlib)}
    migrated = moved(blueprint, links)
    if links is not None and (annotated := links.files.get(node.path, ())):
        if not migrated:
            raise LinksSetError(
                [
                    f"{where}: annotated in {', '.join(annotated)}, in a project whose facts are not in "
                    f"blueprint/links/; {_BY_HAND}"
                ]
            )
        if foreign := [name for name in annotated if name not in own.values()]:
            raise LinksSetError(
                [f"{where}: annotated in {', '.join(foreign)}, which this command does not write; {_BY_HAND}"]
            )
        old = dict(links.facts[node.path])
        try:
            rendered = writer.encode(blueprint, node.path, old)
        except writer.Unencodable:
            rendered = {}
        # The writer rewrites its files whole, so anything there it would not write back would be lost: an end or a
        # key it does not read, a row about part of the article or about another one.
        held = [
            f"{where}: {name} changed while the graph was loaded; run the command again"
            if _reread(path) != before[path]
            else f"{where}: {name} holds more than this command writes back; edit it by hand"
            if name in annotated
            else f"{where}: {name} exists but does not annotate it; move or remove it"
            for path, name in own.items()
            if before.get(path) is not None and before[path] != rendered.get(path)
        ]
        shared = [
            f"{where}: {name} also annotates {other.id}; move that row out first"
            for other in nodes.values()
            if other.path != node.path
            for name in own.values()
            if name in links.files.get(other.path, ())
        ]
        if held or shared:
            raise LinksSetError(shared or held)
        return "annotation", old
    if content.startswith(b"\xef\xbb\xbf"):
        # The loader reads no frontmatter after the mark.
        raise LinksSetError([f"{where}: starts with a byte-order mark; remove it and run again"])
    old = writer.frontmatter_facts(node.id, content.decode("utf-8"))
    if not migrated:
        return "frontmatter", old
    if not unicodedata.is_normalized("NFC", where):
        # Git on macOS records names composed, so in a clone the annotation would name no article.
        raise LinksSetError([f"{where}: its path is not composed (NFC) as Git records it; {_RENAME}"])
    if taken := [name for path, name in own.items() if before.get(path) is not None]:
        raise LinksSetError([f"{where}: {name} exists but does not annotate it; move or remove it" for name in taken])
    return "move", old


def _edited_facts(
    where: str, node: Node, content: bytes, old: Mapping[str, str], edits: Edits, owners: Mapping[str, str]
) -> dict[str, str]:
    """*old* with *edits* applied and the article's ID kept or given, checked by the loader's rules for frontmatter;
    raises :class:`LinksSetError` naming what breaks them."""

    # A value the loader leaves out would be written back as if it were read: setting or unsetting the key fixes it.
    left_out = [
        f"{where}: its {key} {value!r} is left out when read ({problem}); set or unset {key}"
        for key, value in old.items()
        if key != "article_id"
        and key not in edits.values
        and key not in edits.unset
        and (problem := _value_problem(key, value))
    ]
    if left_out:
        raise LinksSetError(left_out)
    new = edits.apply(old)
    # An ID is kept where the article has a well-formed one, its annotation's or, beside an annotation, its ignored
    # frontmatter's; otherwise it gets the one migrate would give it.
    beside = writer.frontmatter_facts(node.id, content.decode("utf-8", "replace")).get("article_id")
    kept = [given for given in (old.get("article_id"), beside) if given and ARTICLE_ID_PATTERN.fullmatch(given)]
    new["article_id"] = kept[0] if kept else _proposed_id(node.id, node.source_sha256 or "")
    # The loader leaves out an ID another article holds, so keeping it would leave this one without one.
    if (other := owners.get(new["article_id"], node.id)) != node.id:
        raise LinksSetError([f"{where}: its article_id {new['article_id']} is {other}'s too; remove one copy"])
    # Which facts go together, as the loader checks them in frontmatter. Each value is quoted so it reads back as
    # held; a refusal names the edits that made the facts, not a line of a frontmatter that may not exist.
    if problems := _parse_frontmatter(where, ["---", *(f'{key}: "{value}"' for key, value in new.items()), "---"])[2]:
        located = re.compile(rf"\A{re.escape(where)}(?::\d+)?: ")
        raise LinksSetError([f"{where}: {_made(edits)}{located.sub('', problem, count=1)}" for problem in problems])
    return new


def _unencodable(blueprint: Path, page: Path, facts: Mapping[str, str]) -> tuple[str, ...]:
    """Why no annotation could hold *facts*, or nothing."""
    try:
        writer.encode(blueprint, page, facts)
    except writer.Unencodable as error:
        return error.reasons
    return ()


def _made(edits: Edits) -> str:
    """The edits that made an article's facts, to begin a refusal of them with; nothing for none."""
    words = [f"{key}={value}" for key, value in edits.values.items()]
    words += [word for key in sorted(edits.unset) for word in ("--unset", key)]
    return f"with {shlex.join(words)}, " if words else ""


def _read_back(graph: Graph, targets: Mapping[str, _Target]) -> list[str]:
    """How each article reads otherwise from the graph loaded again: an edited one than its edits say, any other
    than before, and an edited one whose annotation is not read as its own."""

    blueprint = graph.blueprint_dir
    reloaded = load_graph(blueprint)
    links = read_links(blueprint, {node.path: node.id for node in reloaded.nodes.values()})
    issues: list[str] = []
    for node_id, node in sorted(graph.nodes.items()):
        where = node.path.relative_to(blueprint).as_posix()
        target = targets.get(node_id)
        after = reloaded.nodes.get(node_id)
        if after is None:
            issues.append(f"{where}: the article no longer loads")
            continue
        lines = writer.differences(target.expected if target else _facts(node), _facts(after))
        read = links.files.get(node.path, ()) if links is not None else ()
        names = [_name(blueprint, path) for path in (target.annotations if target else ())]
        reason = "is not read as its annotation; the warnings about blueprint/links/ say why"
        lines[:0] = [f"{name} {reason}" for name in names if name not in read]
        if lines:
            issues.append(f"{where}: {'; '.join(lines)}")
    return issues


def _facts(node: Node) -> dict[str, str]:
    """A loaded node's facts, with the area the article records: ``atlas.json`` fills an unset one only on the node."""
    facts = writer.node_facts(node)
    facts.pop("area", None)
    if node.recorded_area is not None:
        facts["area"] = node.recorded_area
    return facts


def _edited(content: bytes, values: Mapping[str, str], unset: Set[str]) -> bytes:
    """*content* with *values* recorded in its frontmatter and *unset* removed. A key's line is replaced where it
    stands and a new key goes last; every other line, settings and comments among them, keeps its bytes."""

    text = content.decode("utf-8")
    lines = text.splitlines(keepends=True)
    first = lines[0] if lines else ""
    newline = first[len(first.rstrip("\r\n")) :] or "\n"
    if (end := _block_end(lines)) is None:
        block = "".join(f"{key}: {value}{newline}" for key, value in values.items())
        return f"---{newline}{block}---{newline}{newline}{text}".encode("utf-8")
    pending = dict(values)
    kept: list[str] = []
    for line in lines[1:end]:
        key = _key(line) if _is_fact(line) else None
        if key in unset:
            continue
        if key in pending:
            ending = line[len(line.rstrip("\r\n")) :]
            kept.append(f"{key}: {pending.pop(key)}{ending}")
            continue
        kept.append(line)
    kept.extend(f"{key}: {value}{newline}" for key, value in pending.items())
    return "".join([lines[0], *kept, *lines[end:]]).encode("utf-8")


def _undone(blueprint: Path, steps: Sequence[writer.Written], issues: Sequence[str]) -> LinksSetError:
    """The error for a write that could not stand, once its *steps* are undone, naming any that could not be."""
    try:
        writer.undo(steps)
    except writer.AnnotationWriteError as stuck:
        return LinksSetError([*issues, *stuck.issues], [_name(blueprint, step.path) for step in stuck.written])
    return LinksSetError([*issues, "nothing recorded"])


def _reread(path: Path) -> bytes | None:
    try:
        return writer._read(path)
    except OSError:
        return None


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _name(blueprint: Path, path: Path) -> str:
    return path.relative_to(blueprint).as_posix()


__all__ = ["KEYS", "Edits", "LinksSetError", "LinksSetResult", "frontmatter_articles", "parse_arguments", "set_facts"]
